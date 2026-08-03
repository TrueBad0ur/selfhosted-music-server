#!/usr/bin/env python3
"""Music Web UI backend backed by the same CLI/core modules as prepare."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

PREPARE_APP = os.environ.get("PREPARE_APP", "/prepare_app")
if PREPARE_APP not in sys.path:
    sys.path.insert(0, PREPARE_APP)

from common import AUDIO_EXTENSIONS, is_excluded
from intake import IntakeError, inspect_file, list_incoming, publish_incoming, resolve_incoming
from metadata import (
    album_info,
    artist_search,
    best_cover_url,
    extract_title_from_stem,
    find_named_dir,
    popular_album_entries,
    slug,
    title_on_disk,
    title_variants,
)
from runtime import trigger_navidrome_rescan
from tags import _get_tracknum, get_tags

from mutagen import File as MutagenFile

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024**3)))

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
INCOMING_DIR = Path(os.environ.get("INCOMING_DIR", "/incoming"))
LASTFM_KEY = os.environ.get("LASTFM_KEY") or os.environ.get("LASTFM_APIKEY", "")
WEB_USERNAME = os.environ.get("WEB_USERNAME", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-operation")
_analysis_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-analysis")
_library_cache: dict = {"data": None, "ts": 0}
_analysis_store: dict[str, dict] = {}
_CACHE_TTL = 60


@app.before_request
def require_auth():
    if request.path == "/api/health" or not WEB_USERNAME:
        return None
    auth = request.authorization
    if not auth or auth.username != WEB_USERNAME or auth.password != WEB_PASSWORD:
        return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Music"'})
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


def _run_download(job_id: str, artist: str, album: str):
    _run_command(job_id, [
        sys.executable, f"{PREPARE_APP}/download_music.py",
        "--album", artist, album,
        "--out", str(MUSIC_DIR),
    ], rescan=True)


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


def _safe_component(value: str, fallback: str) -> str:
    value = re.sub(r"[\\/\x00-\x1f]", "_", value).strip().strip(".")
    return value or fallback


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


def _album_tracks(artist: str, album: str) -> tuple[list[str], dict]:
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    info = album_info(artist, clean_album, LASTFM_KEY)
    return info.get("tracks", []), info


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
                albums.append({"name": album_dir.name, "tracks": tracks})
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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "music_dir": MUSIC_DIR.is_dir(), "incoming_dir": INCOMING_DIR.is_dir()})


@app.route("/api/search/artists")
def search_artists_route():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    return jsonify([
        {
            "name": artist.get("name", ""),
            "listeners": int(artist.get("listeners", 0) or 0),
            "image": best_cover_url(artist.get("image", []), "medium"),
        }
        for artist in artist_search(query, LASTFM_KEY)
    ])


@app.route("/api/artist/<path:artist>/albums")
def artist_albums(artist: str):
    studios, singles = popular_album_entries(artist, LASTFM_KEY)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    existing = {
        slug(path.name) for path in artist_dir.iterdir() if path.is_dir()
    } if artist_dir else set()
    return jsonify([
        {
            "name": item["name"],
            "playcount": item["playcount"],
            "image": best_cover_url(item["images"], "medium"),
            "on_disk": slug(item["name"]) in existing,
        }
        for item in studios + singles
    ])


@app.route("/api/album/tracks")
def album_tracks():
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    clean_album = re.sub(r"^\d{4}\s*-\s*", "", album)
    artist_dir = find_named_dir(MUSIC_DIR, artist)
    album_dir = find_named_dir(artist_dir, clean_album) if artist_dir else None
    disk_files, disk_slugs = _disk_titles(album_dir) if album_dir else ([], set())
    tracks, info = _album_tracks(artist, clean_album)
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


@app.route("/api/library")
def library():
    return jsonify(_build_library())


@app.route("/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    artist, album = str(body.get("artist", "")).strip(), str(body.get("album", "")).strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    label = f"{artist} — {album}"
    return jsonify({"job_id": _submit(label, _run_download, artist, album), "label": label})


@app.route("/api/download/track", methods=["POST"])
def download_track():
    body = request.get_json(silent=True) or {}
    artist = str(body.get("artist", "")).strip()
    title = str(body.get("title", "")).strip()
    album = str(body.get("album", "")).strip()
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    label = f"{artist} — {title}"
    return jsonify({"job_id": _submit(label, _run_download_track, artist, title, album), "label": label})


@app.route("/api/jobs")
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


@app.route("/api/jobs/<job_id>")
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


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            return jsonify({"error": "running jobs cannot be deleted"}), 409
        _jobs.pop(job_id, None)
    return jsonify({"ok": True})


@app.route("/api/jobs/clear", methods=["POST"])
def clear_jobs():
    with _jobs_lock:
        finished = [job_id for job_id, job in _jobs.items() if job["status"] != "running"]
        for job_id in finished:
            del _jobs[job_id]
    return jsonify({"cleared": len(finished)})


@app.route("/api/prepare/preview")
def prepare_preview():
    return jsonify({"job_id": _submit("prepare dry-run", _run_prepare, [], hidden=True)})


@app.route("/api/prepare/fix", methods=["POST"])
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
    return jsonify({"job_id": _submit(label, _run_prepare, flags), "label": label})


@app.route("/api/upload", methods=["POST"])
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


@app.route("/api/incoming")
def incoming():
    return jsonify(list_incoming(INCOMING_DIR))


@app.route("/api/incoming/publish", methods=["POST"])
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


@app.route("/api/incoming/<path:name>", methods=["GET", "DELETE"])
def incoming_file(name: str):
    try:
        path = resolve_incoming(INCOMING_DIR, name)
    except IntakeError as exc:
        return jsonify({"error": str(exc)}), 404
    if request.method == "DELETE":
        path.unlink()
        return jsonify({"ok": True})
    return send_from_directory(INCOMING_DIR, path.name, as_attachment=True)


@app.route("/api/library/analyze", methods=["POST"])
def library_analyze():
    job_id = _new_job("Library analysis", hidden=True)
    _analysis_store[job_id] = {"results": [], "progress": {}}
    _analysis_executor.submit(_run_analysis, job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/library/analysis/<job_id>")
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


@app.route("/api/rescan", methods=["POST"])
def rescan():
    ok, message = trigger_navidrome_rescan("music-web-manual")
    return jsonify({"ok": ok, "message": message}), 200 if ok else 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8091, debug=False, threaded=True)
