#!/usr/bin/env python3
"""Music Web UI backend backed by the same CLI/core modules as prepare."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.utils import secure_filename

PREPARE_APP = os.environ.get("PREPARE_APP", "/prepare_app")
if PREPARE_APP not in sys.path:
    sys.path.insert(0, PREPARE_APP)

from common import AUDIO_EXTENSIONS, KEEP_REMIXES_MARKER, is_excluded, keeps_remixes, safe_component as _safe_component
from download_music import find_track_sources
from intake import IntakeError, inspect_file, list_incoming, publish_incoming, resolve_incoming
from metadata import (
    JUNK_ALBUM_RE,
    SINGLE_EP_RE,
    album_info,
    artist_album_entries,
    artist_search,
    best_cover_url,
    deezer_artist_image,
    extract_title_from_stem,
    find_named_dir,
    popular_album_entries,
    slug,
    title_on_disk,
    title_variants,
    verified_album_info,
)
from runtime import dedupe_navidrome_media_files, trigger_navidrome_rescan
from tags import _get_tracknum, get_tags

from mutagen import File as MutagenFile

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024**3)))

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
INCOMING_DIR = Path(os.environ.get("INCOMING_DIR", "/incoming"))
LASTFM_KEY = os.environ.get("LASTFM_KEY") or os.environ.get("LASTFM_APIKEY", "")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-operation")
_analysis_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-analysis")
_library_cache: dict = {"data": None, "ts": 0}
_analysis_store: dict[str, dict] = {}
_CACHE_TTL = 60


@app.before_request
def require_auth():
    # Access is gated upstream by nginx + oauth2-proxy (Google SSO). This container
    # is not published on the host directly, so only trusted proxy traffic reaches here.
    return None


def _new_job(label: str, hidden: bool = False) -> str:
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "label": label,
            "status": "running",
            "hidden": hidden,
            "log": deque(maxlen=2000),
            "created": time.time(),
        }
    return job_id


def _append(job_id: str, line: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["log"].append(str(line))


def _finish(job_id: str, ok: bool):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "done" if ok else "error"


def _submit(label: str, worker, *args, hidden: bool = False) -> str:
    job_id = _new_job(label, hidden=hidden)

    def wrapped():
        try:
            worker(job_id, *args)
        except Exception as exc:
            _append(job_id, f"[ERROR] {exc}")
            _finish(job_id, False)

    _executor.submit(wrapped)
    return job_id


def _invalidate_library():
    _library_cache["ts"] = 0


def _run_command(job_id: str, command: list[str], rescan: bool = False):
    import subprocess

    display_command = list(command)
    for index, argument in enumerate(display_command[:-1]):
        if argument in {"--lastfm-key", "--password"}:
            display_command[index + 1] = "***"
    _append(job_id, "$ " + " ".join(display_command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in iter(process.stdout.readline, ""):
        _append(job_id, line.rstrip())
    process.wait()
    ok = process.returncode == 0
    if rescan and ok:
        scan_ok, message = trigger_navidrome_rescan("music-web")
        _append(job_id, f"Navidrome: {message}")
        ok = scan_ok
    _invalidate_library()
    _finish(job_id, ok)


def _run_download(
    job_id: str, artist: str, album: str, catalog_source: str = "",
    allow_partial: bool = False, keep_remixes: bool = False,
):
    command = [
        sys.executable, f"{PREPARE_APP}/download_music.py",
        "--album", artist, album,
        "--out", str(MUSIC_DIR),
    ]
    if catalog_source:
        command.extend(["--catalog-source", catalog_source])
    if allow_partial:
        command.append("--allow-partial")
    if keep_remixes:
        command.append("--keep-remixes")
    _run_command(job_id, command, rescan=True)


def _run_download_track(job_id: str, artist: str, title: str, album: str):
    artist_dir = find_named_dir(MUSIC_DIR, artist) or MUSIC_DIR / _safe_component(artist, "Unknown Artist")
    if album:
        album_dir = find_named_dir(artist_dir, album) or artist_dir / _safe_component(album, "Singles")
    else:
        album_dir = artist_dir / "Singles"
    _run_command(job_id, [
        sys.executable, f"{PREPARE_APP}/download_music.py",
        "--track", artist, title,
        "--out", str(album_dir),
    ], rescan=True)


def _run_replace_track(job_id: str, artist: str, title: str, file_path: str, source: str, source_url: str):
    _run_command(job_id, [
        sys.executable, f"{PREPARE_APP}/download_music.py",
        "--replace-track", artist, title, file_path,
        "--source", source, "--source-url", source_url,
    ], rescan=True)


def _run_prepare(job_id: str, flags: list[str]):
    _run_command(job_id, [
        sys.executable, f"{PREPARE_APP}/prepare_music.py", str(MUSIC_DIR), *flags,
    ])


def _run_intake(job_id: str, names: list[str] | None, bypass: bool):
    results = publish_incoming(INCOMING_DIR, MUSIC_DIR, names=names, bypass=bypass)
    for result in results:
        _append(job_id, json.dumps(result, ensure_ascii=False))
    ok = bool(results) and not any(item["status"] == "error" for item in results)
    if ok:
        scan_ok, message = trigger_navidrome_rescan("music-web-intake")
        _append(job_id, f"Navidrome: {message}")
        ok = scan_ok
    _invalidate_library()
    _finish(job_id, ok)


def _disk_titles(album_dir: Path) -> tuple[list[tuple[int, str]], set[str]]:
    files = []
    variants = set()
    paths = [
        path for path in sorted(album_dir.iterdir())
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
    ] if album_dir.is_dir() else []
    for fallback_number, path in enumerate(paths, 1):
        title = extract_title_from_stem(path.stem)
        match = re.match(r"^(\d+)", path.stem)
        filename_number = int(match.group(1)) if match else None
        tagged_number = None
        variants.update(title_variants(title))
        try:
            media = MutagenFile(str(path), easy=False)
            tagged_number = _get_tracknum(media) if media else None
            tagged_title = get_tags(media).get("title", "") if media else ""
            variants.update(title_variants(str(tagged_title)))
        except Exception:
            pass
        files.append((tagged_number or filename_number or fallback_number, title))
    return files, variants


def _find_track_file(album_dir: Path, title: str) -> Path | None:
    target = slug(title)
    for path in sorted(album_dir.iterdir()):
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        if slug(extract_title_from_stem(path.stem)) == target:
            return path
        try:
            media = MutagenFile(str(path), easy=False)
            tagged_title = get_tags(media).get("title", "") if media else ""
        except Exception:
            tagged_title = ""
        if tagged_title and slug(tagged_title) == target:
            return path
    return None


def _album_tracks(
    artist: str, album: str, catalog_source: str = ""
) -> tuple[list[str], dict]:
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    info = verified_album_info(
        artist, clean_album, LASTFM_KEY, preferred_source=catalog_source or None
    )
    return info.get("tracks", []), info


def _catalog_selection_payload(info: dict) -> dict:
    return {
        "selection_required": True,
        "error": info.get("error") or "catalog selection required",
        "catalogs": [
            {"source": source, "tracks": tracks}
            for source, tracks in (info.get("catalog_choices") or {}).items()
        ],
    }


def _build_library() -> list:
    now = time.time()
    if _library_cache["data"] is not None and now - _library_cache["ts"] < _CACHE_TTL:
        return _library_cache["data"]
    artists = []
    if not MUSIC_DIR.is_dir():
        return artists
    for artist_dir in sorted(MUSIC_DIR.iterdir(), key=lambda path: path.name.casefold()):
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        albums = []
        for album_dir in sorted(artist_dir.iterdir(), key=lambda path: path.name.casefold()):
            if not album_dir.is_dir() or album_dir.name.startswith("."):
                continue
            tracks = sum(
                path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
                for path in album_dir.iterdir()
            )
            if tracks:
                albums.append({
                    "name": album_dir.name, "tracks": tracks,
                    "keep_remixes": keeps_remixes(album_dir),
                })
        if albums:
            artists.append({
                "name": artist_dir.name,
                "albums": albums,
                "total_tracks": sum(album["tracks"] for album in albums),
            })
    _library_cache.update(data=artists, ts=now)
    return artists


def _run_analysis(job_id: str):
    store = _analysis_store[job_id]
    results = []
    checked = 0
    for artist_dir in sorted(MUSIC_DIR.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name.startswith(".") or is_excluded(artist_dir):
            continue
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir() or is_excluded(album_dir):
                continue
            _, disk_slugs = _disk_titles(album_dir)
            if not disk_slugs:
                continue
            checked += 1
            album = re.sub(r"^\d{4}\s*-\s*", "", album_dir.name)
            store["progress"] = {"checked": checked, "current": f"{artist_dir.name} / {album}"}
            _append(job_id, f"[{checked}] {artist_dir.name} / {album}")
            tracks, _ = _album_tracks(artist_dir.name, album)
            if not tracks:
                _append(job_id, "  → no tracklist")
                continue
            missing = [title for title in tracks if not title_on_disk(title, disk_slugs)]
            if missing:
                results.append({
                    "artist": artist_dir.name,
                    "album": album,
                    "album_dir": album_dir.name,
                    "missing": missing,
                    "on_disk": len(tracks) - len(missing),
                    "total": len(tracks),
                })
                store["results"] = results
                _append(job_id, f"  → {len(missing)}/{len(tracks)} missing")
    store["progress"] = {"checked": checked, "current": ""}
    _finish(job_id, True)


@app.route("/tools/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/tools/api/health")
def health():
    return jsonify({"status": "ok", "music_dir": MUSIC_DIR.is_dir(), "incoming_dir": INCOMING_DIR.is_dir()})


@app.route("/tools/api/search/artists")
def search_artists_route():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    artists = artist_search(query, LASTFM_KEY)
    with ThreadPoolExecutor(max_workers=6) as pool:
        deezer_images = list(pool.map(lambda a: deezer_artist_image(a.get("name", "")), artists))
    return jsonify([
        {
            "name": artist.get("name", ""),
            "listeners": int(artist.get("listeners", 0) or 0),
            "image": deezer_images[i] or best_cover_url(artist.get("image", []), "medium"),
        }
        for i, artist in enumerate(artists)
    ])


@app.route("/tools/api/artist/<path:artist>/albums")
def artist_albums(artist: str):
    entries = artist_album_entries(artist, LASTFM_KEY, limit=200)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    existing = {
        slug(path.name) for path in artist_dir.iterdir() if path.is_dir()
    } if artist_dir else set()

    # Same "popular" cutoff used when downloading (popular_album_entries), so the
    # default view matches what --all-albums would fetch - but here we still return
    # everything else too, just flagged, so the UI can reveal the long tail on demand
    # instead of it silently vanishing.
    top_playcount = max((item["playcount"] for item in entries), default=0)
    relative_threshold = int(top_playcount * 0.05)
    threshold = max(500, relative_threshold) if top_playcount >= 500 else relative_threshold

    filtered = [item for item in entries if not JUNK_ALBUM_RE.search(item["name"])]
    filtered.sort(key=lambda item: item["playcount"], reverse=True)
    studio_count = 0
    result = []
    for item in filtered:
        is_single = bool(SINGLE_EP_RE.search(item["name"]))
        popular = item["playcount"] >= threshold and (is_single or studio_count < 15)
        if popular and not is_single:
            studio_count += 1
        result.append({
            "name": item["name"],
            "playcount": item["playcount"],
            "image": best_cover_url(item["images"], "medium"),
            "on_disk": slug(item["name"]) in existing,
            "popular": popular,
        })
    return jsonify(result)


@app.route("/tools/api/album/local-tracks")
def album_local_tracks():
    # Filesystem-only listing, no catalog lookup - used for simply expanding an
    # album row in the Library tab, which shouldn't have to wait on Last.fm/
    # MusicBrainz/Deezer/iTunes just to show tracks that are already on disk.
    # Missing-track detection (which genuinely needs the catalog) stays on the
    # "?" check button / album_tracks() below, not on a plain click.
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    album_dir = find_named_dir(artist_dir, clean_album) if artist_dir else None
    disk_files, _ = _disk_titles(album_dir) if album_dir else ([], set())
    return jsonify([
        {"num": number, "title": title, "on_disk": True}
        for number, title in sorted(disk_files)
    ])


@app.route("/tools/api/album/tracks")
def album_tracks():
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    album_dir = find_named_dir(artist_dir, clean_album) if artist_dir else None
    disk_files, disk_slugs = _disk_titles(album_dir) if album_dir else ([], set())
    catalog_source = request.args.get("source", "").strip()
    tracks, info = _album_tracks(artist, clean_album, catalog_source)
    if info.get("selection_required"):
        return jsonify(_catalog_selection_payload(info)), 409
    if not tracks:
        if info.get("error") and not disk_files:
            return jsonify({"error": str(info["error"])}), 502
        return jsonify([
            {"num": number, "title": title, "on_disk": True}
            for number, title in disk_files
        ])
    return jsonify([
        {"num": index, "title": title, "on_disk": title_on_disk(title, disk_slugs)}
        for index, title in enumerate(tracks, 1)
    ])


@app.route("/tools/api/library")
def library():
    return jsonify(_build_library())


@app.route("/tools/api/album/keep-remixes", methods=["POST"])
def album_keep_remixes():
    body = request.get_json(silent=True) or {}
    artist = str(body.get("artist", "")).strip()
    album = str(body.get("album", "")).strip()
    keep = bool(body.get("keep"))
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    album_dir = find_named_dir(artist_dir, album) if artist_dir else None
    if not album_dir:
        return jsonify({"error": "album not found on disk"}), 404
    marker = album_dir / KEEP_REMIXES_MARKER
    if keep:
        marker.touch(exist_ok=True)
    else:
        marker.unlink(missing_ok=True)
    _invalidate_library()
    return jsonify({"keep_remixes": keep})


@app.route("/tools/api/track/sources")
def track_sources():
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album) if album else None
    expected_duration = None
    if album:
        artist_dir = find_named_dir(MUSIC_DIR, artist)
        album_dir = find_named_dir(artist_dir, clean_album) if artist_dir else None
        track_path = _find_track_file(album_dir, title) if album_dir else None
        if track_path:
            media = MutagenFile(str(track_path), easy=False)
            if media is not None and media.info is not None:
                expected_duration = float(media.info.length)
    sources = find_track_sources(artist, title, clean_album, expected_duration)
    return jsonify(sources)


@app.route("/tools/api/track/replace", methods=["POST"])
def track_replace():
    body = request.get_json(silent=True) or {}
    artist = str(body.get("artist", "")).strip()
    album = str(body.get("album", "")).strip()
    title = str(body.get("title", "")).strip()
    source = str(body.get("source", "")).strip()
    source_url = str(body.get("source_url", "")).strip()
    if not artist or not album or not title or not source_url:
        return jsonify({"error": "artist, album, title and source_url required"}), 400
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    album_dir = find_named_dir(artist_dir, clean_album) if artist_dir else None
    track_path = _find_track_file(album_dir, title) if album_dir else None
    if not track_path:
        return jsonify({"error": "track not found on disk"}), 404
    label = f"{artist} — {title} (replace)"
    return jsonify({
        "job_id": _submit(label, _run_replace_track, artist, title, str(track_path), source, source_url),
        "label": label,
    })


@app.route("/tools/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    artist = str(body.get("artist", "")).strip()
    album = str(body.get("album", "")).strip()
    catalog_source = str(body.get("catalog_source", "")).strip()
    allow_partial = bool(body.get("allow_partial"))
    keep_remixes = bool(body.get("keep_remixes"))
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    info = verified_album_info(
        artist, album, LASTFM_KEY, preferred_source=catalog_source or None
    )
    if info.get("selection_required"):
        return jsonify(_catalog_selection_payload(info)), 409
    if info.get("error") and not info.get("tracks"):
        return jsonify({"error": str(info["error"])}), 422

    selected_source = catalog_source or str(info.get("selected_source") or "")
    label = f"{artist} — {album}"
    return jsonify({
        "job_id": _submit(
            label, _run_download, artist, album, selected_source, allow_partial, keep_remixes
        ),
        "label": label,
        "catalog_source": selected_source,
    })


@app.route("/tools/api/download/track", methods=["POST"])
def download_track():
    body = request.get_json(silent=True) or {}
    artist = str(body.get("artist", "")).strip()
    title = str(body.get("title", "")).strip()
    album = str(body.get("album", "")).strip()
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    label = f"{artist} — {title}"
    return jsonify({"job_id": _submit(label, _run_download_track, artist, title, album), "label": label})


@app.route("/tools/api/jobs")
def list_jobs():
    with _jobs_lock:
        jobs = [
            {
                "id": job["id"],
                "label": job["label"],
                "status": job["status"],
                "created": job["created"],
                "log_tail": list(job["log"])[-30:],
            }
            for job in sorted(_jobs.values(), key=lambda item: -item["created"])
            if not job["hidden"]
        ]
    return jsonify(jobs)


@app.route("/tools/api/jobs/<job_id>")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        payload = {
            "id": job["id"],
            "label": job["label"],
            "status": job["status"],
            "created": job["created"],
            "log": list(job["log"]),
        }
    return jsonify(payload)


@app.route("/tools/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            return jsonify({"error": "running jobs cannot be deleted"}), 409
        _jobs.pop(job_id, None)
    return jsonify({"ok": True})


@app.route("/tools/api/jobs/clear", methods=["POST"])
def clear_jobs():
    with _jobs_lock:
        finished = [job_id for job_id, job in _jobs.items() if job["status"] != "running"]
        for job_id in finished:
            del _jobs[job_id]
    return jsonify({"cleared": len(finished)})


@app.route("/tools/api/prepare/preview")
def prepare_preview():
    return jsonify({"job_id": _submit("prepare dry-run", _run_prepare, [], hidden=True)})


@app.route("/tools/api/navidrome/dupes", methods=["GET", "POST"])
def navidrome_dupes():
    # Fast enough (a couple seconds even across the whole library) to run inline
    # rather than through the job queue - GET checks, POST applies the fix.
    fix = request.method == "POST"
    duplicates = dedupe_navidrome_media_files(fix)
    if duplicates is None:
        return jsonify({"error": "NAVIDROME_DB_PATH not configured/mounted"}), 501
    if fix and duplicates:
        trigger_navidrome_rescan("music-web")
    return jsonify({
        "duplicates": [{"path": path, "count": count} for path, count in duplicates],
        "fixed": fix,
    })


@app.route("/tools/api/prepare/fix", methods=["POST"])
def prepare_fix():
    body = request.get_json(silent=True) or {}
    flags = ["--fix"]
    mapping = {
        "encoding_only": "--encoding-only",
        "artists_only": "--artists-only",
        "album_only": "--album-only",
        "variants_only": "--variants-only",
        "tracknums_only": "--tracknums-only",
        "singles_only": "--singles-only",
    }
    selected = [flag for key, flag in mapping.items() if body.get(key)]
    if len(selected) > 1:
        return jsonify({"error": "only one cleanup scope may be selected"}), 400
    flags.extend(selected)
    label = "prepare " + " ".join(flags)
    return jsonify({"job_id": _submit(label, _run_prepare, flags, hidden=True), "label": label})


@app.route("/tools/api/upload", methods=["POST"])
def upload():
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "no file field"}), 400
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    staged = []
    for uploaded in files:
        original = Path(uploaded.filename or "").name
        suffix = Path(original).suffix.casefold()
        if not original or suffix not in AUDIO_EXTENSIONS:
            staged.append({"name": original, "status": "error", "error": "unsupported extension"})
            continue
        filename = secure_filename(original) or f"upload-{uuid.uuid4().hex}{suffix}"
        filename = f"{Path(filename).stem}{suffix}"
        destination = INCOMING_DIR / filename
        if destination.exists():
            destination = INCOMING_DIR / f"{Path(filename).stem}-{uuid.uuid4().hex[:8]}{suffix}"
        temporary = INCOMING_DIR / f".upload-{uuid.uuid4().hex}{suffix}"
        try:
            uploaded.save(temporary)
            os.replace(temporary, destination)
            try:
                details = inspect_file(destination)
                staged.append({**details, "status": "ready", "error": ""})
            except IntakeError as exc:
                staged.append({"name": destination.name, "status": "error", "error": str(exc)})
        finally:
            temporary.unlink(missing_ok=True)
    return jsonify({"staged": staged, "saved": [item["name"] for item in staged if item["status"] != "error"]})


@app.route("/tools/api/incoming")
def incoming():
    return jsonify(list_incoming(INCOMING_DIR))


@app.route("/tools/api/incoming/publish", methods=["POST"])
def publish_staged():
    body = request.get_json(silent=True) or {}
    names = body.get("names")
    if names is not None and (
        not isinstance(names, list) or not all(isinstance(name, str) for name in names)
    ):
        return jsonify({"error": "names must be a list of filenames"}), 400
    bypass = bool(body.get("bypass", False))
    label = "Publish staged uploads" + (" (bypass)" if bypass else "")
    return jsonify({"job_id": _submit(label, _run_intake, names, bypass), "label": label})


@app.route("/tools/api/incoming/<path:name>", methods=["GET", "DELETE"])
def incoming_file(name: str):
    try:
        path = resolve_incoming(INCOMING_DIR, name)
    except IntakeError as exc:
        return jsonify({"error": str(exc)}), 404
    if request.method == "DELETE":
        path.unlink()
        return jsonify({"ok": True})
    return send_from_directory(INCOMING_DIR, path.name, as_attachment=True)


@app.route("/tools/api/library/analyze", methods=["POST"])
def library_analyze():
    job_id = _new_job("Library analysis", hidden=True)
    _analysis_store[job_id] = {"results": [], "progress": {}}
    _analysis_executor.submit(_run_analysis, job_id)
    return jsonify({"job_id": job_id})


@app.route("/tools/api/library/analysis/<job_id>")
def library_analysis_result(job_id: str):
    store = _analysis_store.get(job_id)
    if not store:
        return jsonify({"error": "not found"}), 404
    with _jobs_lock:
        job = _jobs.get(job_id, {})
        status = job.get("status", "running")
        log_tail = list(job.get("log", []))[-3:]
    return jsonify({
        "status": status,
        "progress": store.get("progress", {}),
        "results": store.get("results", []),
        "log_tail": log_tail,
    })


@app.route("/tools/api/rescan", methods=["POST"])
def rescan():
    ok, message = trigger_navidrome_rescan("music-web-manual")
    return jsonify({"ok": ok, "message": message}), 200 if ok else 502


_APP_CONFIG_RE = re.compile(r'window\.__APP_CONFIG__\s*=\s*"((?:[^"\\]|\\.)*)"')


@app.route("/tools/oauth-handoff")
def oauth_handoff():
    """Hands a Google-authenticated session off to the Tempo Android app.

    Navidrome computes a real Subsonic token/salt for the ExtAuth'd user and
    embeds it in the index page it serves for that same Remote-User header
    (see server/serve_index.go upstream). Re-fetch that page server-side,
    pull the token out, and bounce the browser to a custom URI scheme that
    Tempo registers an intent-filter for.
    """
    remote_user = request.headers.get("Remote-User", "")
    if not remote_user:
        return "No authenticated session", 401

    req = urllib.request.Request(
        "http://navidrome:4533/app/", headers={"Remote-User": remote_user}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"Could not reach Navidrome: {e}", 502

    m = _APP_CONFIG_RE.search(html)
    if not m:
        return "Could not find Navidrome auth config", 502

    try:
        config = json.loads(json.loads('"' + m.group(1) + '"'))
    except Exception as e:
        return f"Could not parse Navidrome auth config: {e}", 502

    auth = config.get("auth") or {}
    if not auth.get("subsonicToken"):
        return "Navidrome did not return a Subsonic token for this session", 502

    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    params = urllib.parse.urlencode({
        "server": f"{scheme}://{request.host}",
        "username": auth["username"],
        "token": auth["subsonicToken"],
        "salt": auth["subsonicSalt"],
    })
    server_id = request.args.get("server_id", "").strip()
    if server_id:
        try:
            params += "&" + urllib.parse.urlencode({
                "server_id": str(uuid.UUID(server_id)),
            })
        except ValueError:
            return "Invalid server identifier", 400

    server_name = request.args.get("server_name", "").strip()
    if server_name:
        params += "&" + urllib.parse.urlencode({
            "server_name": server_name[:100],
        })
    return redirect(f"tempo://oauth-callback?{params}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8091, debug=False, threaded=True)
