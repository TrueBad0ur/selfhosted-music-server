#!/usr/bin/env python3
"""Last.fm/MusicBrainz metadata plus yt-dlp download backend."""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import json
from difflib import SequenceMatcher
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import APIC
from metadata import (
    artist_album_entries,
    extract_title_from_stem,
    find_named_dir,
    relaxed_title_variants,
    slug,
    title_variants,
    track_info,
    verified_album_info,
)
from process_file import process_file
from tags import set_tag

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
_INVALID_COMPONENT = re.compile(r"[\\/\x00-\x1f]")
_TITLE_MATCH_NOISE_RE = re.compile(
    r"\((?:feat(?:uring)?|ft\.?|official|remaster(?:ed)?|"
    r"lyric(?:\s+video)?|audio|visualizer)[^)]*\)",
    re.IGNORECASE,
)
_UNREQUESTED_VARIANTS = (
    "remix", "instrumental", "karaoke", "cover", "acapella", "slowed",
    "sped up", "nightcore", "live", "feat", "trailer", "full album",
    "first two tracks", "medley", "mashup",
)


def safe_component(value: str, fallback: str) -> str:
    """Return a filesystem-safe single path component."""
    cleaned = _INVALID_COMPONENT.sub("_", value).strip().strip(".")
    return cleaned or fallback


def _find_artist_folder(music_root: Path, requested: str, canonical: str) -> Path:
    return find_named_dir(music_root, requested) or find_named_dir(music_root, canonical) or (
        music_root / safe_component(canonical, "Unknown Artist")
    )


def _best_downloaded_mp3(temp_dir: Path) -> Path | None:
    candidates = [path for path in temp_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".mp3"]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _title_match_key(value: str) -> str:
    return slug(_TITLE_MATCH_NOISE_RE.sub("", value))


def _youtube_candidate_score(
    artist: str,
    title: str,
    candidate: dict,
    expected_duration: float | None = None,
) -> float:
    candidate_title = str(candidate.get("title") or "")
    wanted = _title_match_key(title)
    found = _title_match_key(candidate_title)
    artist_key = slug(artist)
    artist_keys = title_variants(artist)
    artist_prefix = next(
        (variant for variant in sorted(artist_keys, key=len, reverse=True) if found.startswith(variant)),
        "",
    )
    found_without_artist = (
        found if found == wanted else found[len(artist_prefix):] if artist_prefix else found
    )
    if not wanted or not found_without_artist:
        return -1

    if slug(title) == slug(artist) and found_without_artist != wanted:
        return -1

    duration = candidate.get("duration")
    if not duration and not (candidate.get("channel") or candidate.get("uploader")) \
            and found_without_artist != wanted:
        return -1
    if duration and float(duration) >= 600 and not expected_duration:
        return -1
    if duration and expected_duration:
        ratio = float(duration) / expected_duration
        if ratio < 0.70 or ratio > 1.35:
            return -1

    if " + " in candidate_title and "+" not in title:
        return -1

    wanted_text = re.sub(r"[^\w]+", " ", title.casefold()).strip()
    found_text = re.sub(r"[^\w]+", " ", candidate_title.casefold()).strip()
    for marker in _UNREQUESTED_VARIANTS:
        pattern = rf"(?<!\w){re.escape(marker)}(?!\w)"
        if re.search(pattern, found_text) and not re.search(pattern, wanted_text):
            return -1

    if len(wanted) <= 4:
        if found_without_artist != wanted:
            return -1
        score = 120.0
    elif found_without_artist == wanted:
        score = 120.0
    elif wanted in found_without_artist:
        score = 100.0
    else:
        similarity = SequenceMatcher(None, wanted, found_without_artist).ratio()
        if similarity < 0.78:
            return -1
        score = similarity * 90.0

    source_text = " ".join(
        str(candidate.get(key) or "") for key in ("title", "channel", "uploader")
    )
    source_keys = title_variants(source_text)
    artist_matches = any(
        artist_variant in source_variant
        for artist_variant in artist_keys
        for source_variant in source_keys
    )
    if not artist_matches:
        return -1
    score += 25.0
    if artist_key and slug(str(candidate.get("channel") or "")) == artist_key:
        score += 15.0
    return score


def _youtube_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
) -> list[dict]:
    queries = [f"{artist} {title}"]
    queries.append(f"{artist} {title} {album}" if album else f"{artist} {title} audio")
    ranked = []
    seen = set()
    for query in dict.fromkeys(queries):
        command = [
            "yt-dlp", f"ytsearch10:{query}",
            "--dump-single-json", "--flat-playlist",
            "--extractor-args", "youtube:player_client=android,web",
            "--quiet", "--no-warnings",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45)
            data = json.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            continue
        for candidate in data.get("entries") or []:
            video_id = candidate.get("id")
            score = _youtube_candidate_score(artist, title, candidate, expected_duration)
            if video_id and video_id not in seen and score >= 0:
                seen.add(video_id)
                candidate = dict(candidate)
                candidate["_download_url"] = f"https://www.youtube.com/watch?v={video_id}"
                candidate["_source"] = "YouTube"
                ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def _soundcloud_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
) -> list[dict]:
    command = [
        "yt-dlp",
        f"scsearch10:{artist} {title}",
        "--dump-single-json", "--flat-playlist", "--no-cache-dir",
        "--quiet", "--no-warnings",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        data = json.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []

    ranked = []
    for candidate in data.get("entries") or []:
        download_url = candidate.get("webpage_url")
        score = _youtube_candidate_score(artist, title, candidate, expected_duration)
        if download_url and score >= 0:
            candidate = dict(candidate)
            candidate["_download_url"] = download_url
            candidate["_source"] = "SoundCloud"
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def _youtube_music_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
) -> list[dict]:
    query = f"{artist} {title}"
    if album:
        query += f" {album}"
    search_url = "https://music.youtube.com/search?" + urllib.parse.urlencode({"q": query})
    command = [
        "yt-dlp", search_url, "--dump-single-json", "--flat-playlist",
        "--playlist-end", "20", "--quiet", "--no-warnings",
        "--extractor-args", "youtube:player_client=android,web",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        data = json.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []
    ranked = []
    for candidate in data.get("entries") or []:
        video_id = candidate.get("id")
        if not video_id or candidate.get("ie_key") != "Youtube":
            continue
        score = _youtube_candidate_score(artist, title, candidate, expected_duration)
        if score < 0:
            continue
        candidate = dict(candidate)
        candidate["_download_url"] = f"https://www.youtube.com/watch?v={video_id}"
        candidate["_source"] = "YouTube Music"
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def _zaycev_request(path: str, data: dict | None = None) -> dict | str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://zaycev.net/",
    }
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"https://zaycev.net/api/external/{path}", data=payload, headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return json.loads(body) if response.headers.get_content_type() == "application/json" else body
    except Exception:
        return {}


def _zaycev_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
) -> list[dict]:
    ranked = []
    seen = set()
    normalized_query = re.sub(r"[^\w]+", " ", f"{artist} {title}").strip()
    normalized_title = re.sub(r"[^\w]+", " ", title).strip()
    queries = [normalized_query, normalized_title, f"{artist} {title}"]
    for search_text in dict.fromkeys(queries):
        query = urllib.parse.urlencode({"q": search_text, "page": 1, "limit": 20})
        data = _zaycev_request(f"pages/search/tracks?{query}")
        if not isinstance(data, dict):
            continue
        for track_id, item in (data.get("tracksInfo") or {}).items():
            if track_id in seen or not item.get("downloadEnabled"):
                continue
            duration_match = re.fullmatch(r"(\d+):(\d{2})", str(item.get("duration") or ""))
            duration = (
                int(duration_match.group(1)) * 60 + int(duration_match.group(2))
                if duration_match else None
            )
            candidate = {
                "id": str(track_id),
                "title": str(item.get("track") or ""),
                "channel": str(item.get("artistName") or ""),
                "uploader": str(item.get("artistName") or ""),
                "duration": duration,
            }
            score = _youtube_candidate_score(artist, title, candidate, expected_duration)
            if score >= 0:
                seen.add(track_id)
                ranked.append((score, candidate))
        if ranked:
            break
    ranked.sort(key=lambda item: item[0], reverse=True)

    result = []
    for _, candidate in ranked:
        metadata = _zaycev_request("track/filezmeta", {"trackIds": [int(candidate["id"])]})
        tracks = metadata.get("tracks") if isinstance(metadata, dict) else []
        download_hash = tracks[0].get("download") if tracks else ""
        download_url = _zaycev_request(f"track/download/{download_hash}") if download_hash else ""
        if not isinstance(download_url, str) or not download_url.startswith("https://"):
            continue
        candidate["_download_url"] = download_url
        candidate["_source"] = "ZAYCEV.NET"
        candidate["_direct_audio"] = True
        result.append(candidate)
    return result


def _pesnime_request(query: str) -> str:
    search_slug = re.sub(r"[^\w]+", "-", query.casefold(), flags=re.UNICODE).strip("-")
    url = "https://hit.pesni.me/search/" + urllib.parse.quote(search_slug)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://hit.pesni.me/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(5 * 1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return ""


def _pesnime_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
) -> list[dict]:
    """Return exact downloadable tracks embedded in Pesni.me search results."""
    body = _pesnime_request(f"{artist} {title}")
    if not body:
        body = _pesnime_request(title)
    # Next.js serializes result objects inside escaped RSC strings.
    body = html.unescape(body.replace(r'\"', '"').replace(r"\/", "/"))
    item_pattern = re.compile(
        r'\{"id":(?P<id>\d+),"artist":"(?P<artist>[^"]*)",'
        r'"title":"(?P<title>[^"]*)","version":"(?P<version>[^"]*)",'
        r'"duration":(?P<duration>\d+).*?'
        r'"download":"(?P<download>https://[^"]+)"',
        re.DOTALL,
    )
    ranked = []
    seen = set()
    for match in item_pattern.finditer(body):
        track_id = match.group("id")
        if track_id in seen:
            continue
        candidate = {
            "id": track_id,
            "title": match.group("title"),
            "channel": match.group("artist"),
            "uploader": match.group("artist"),
            "duration": float(match.group("duration")),
            "_download_url": match.group("download"),
            "_source": "PESNI.ME",
            "_direct_audio": True,
            "_headers": {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://hit.pesni.me/",
            },
        }
        score = _youtube_candidate_score(artist, title, candidate, expected_duration)
        if score >= 0:
            seen.add(track_id)
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def _audio_decodes(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def ytdlp_download(
    artist: str,
    title: str,
    destination: Path,
    dry_run: bool = False,
    album: str | None = None,
    expected_duration: float | None = None,
) -> bool:
    """Download into an isolated temp directory and atomically publish the MP3."""
    destination = Path(destination)
    if destination.exists():
        print(f"  [SKIP] already exists: {destination.name}")
        return True
    if dry_run:
        print(f"  [DRY] would search yt-dlp: {artist} {title}")
        return True

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"  ↓ {destination.name}", end="", flush=True)
    last_error = ""
    had_candidates = False
    finders = (
        _youtube_candidates, _youtube_music_candidates,
        _soundcloud_candidates, _zaycev_candidates, _pesnime_candidates,
    )
    for finder in finders:
        for candidate in finder(artist, title, album, expected_duration):
            had_candidates = True
            with tempfile.TemporaryDirectory(prefix=".download-", dir=destination.parent) as temp_name:
                temp_dir = Path(temp_name)
                if candidate.get("_direct_audio"):
                    downloaded = temp_dir / f"{candidate.get('id') or 'track'}.mp3"
                    try:
                        request = urllib.request.Request(
                            candidate["_download_url"],
                            headers=candidate.get("_headers") or {
                                "User-Agent": "Mozilla/5.0", "Referer": "https://zaycev.net/",
                            },
                        )
                        with urllib.request.urlopen(request, timeout=45) as response, downloaded.open("wb") as output:
                            remaining = int(response.headers.get("Content-Length") or 0)
                            while True:
                                chunk_size = min(1024 * 1024, remaining) if remaining else 1024 * 1024
                                chunk = response.read(chunk_size)
                                if not chunk:
                                    break
                                output.write(chunk)
                                if remaining:
                                    remaining -= len(chunk)
                                    if remaining <= 0:
                                        break
                        result = subprocess.CompletedProcess([], 0, "", "")
                    except Exception as exc:
                        last_error = str(exc)
                        continue
                else:
                    command = [
                        "yt-dlp", candidate["_download_url"],
                        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
                        "--output", str(temp_dir / "%(id)s.%(ext)s"),
                        "--add-metadata", "--no-playlist", "--no-cache-dir",
                        "--extractor-args", "youtube:player_client=android,web",
                        "--quiet", "--no-warnings",
                    ]
                    if not expected_duration:
                        command.extend(["--match-filter", "duration < 600"])
                    try:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
                    except subprocess.TimeoutExpired:
                        last_error = "timeout"
                        continue
                    except FileNotFoundError:
                        last_error = "yt-dlp is not installed"
                        break

                downloaded = _best_downloaded_mp3(temp_dir)
                if result.returncode != 0 or downloaded is None:
                    last_error = result.stderr[:300] if result.stderr else "no MP3 produced"
                    continue
                if candidate.get("_direct_audio") or expected_duration:
                    try:
                        media = MutagenFile(str(downloaded), easy=False)
                        actual_duration = (
                            float(media.info.length)
                            if media is not None and media.info is not None else None
                        )
                    except Exception:
                        actual_duration = None
                    if not actual_duration or (not expected_duration and actual_duration >= 600):
                        last_error = "downloaded audio has invalid duration"
                        continue
                    if expected_duration and abs(actual_duration - expected_duration) > max(8.0, expected_duration * 0.05):
                        last_error = (
                            f"downloaded duration {actual_duration:.1f}s does not match "
                            f"catalog {expected_duration:.1f}s"
                        )
                        continue
                    if not _audio_decodes(downloaded):
                        last_error = "downloaded audio does not decode completely"
                        continue

                if destination.exists():
                    print("  [SKIP] completed by another worker")
                    return True
                downloaded.replace(destination)
                source_name = candidate.get("channel") or candidate.get("uploader") or candidate.get("id")
                print(
                    f"  ({destination.stat().st_size // 1024} KB; "
                    f"{candidate['_source']}: {source_name})"
                )
                return True

    reason = "no relevant result" if not had_candidates else "all relevant results failed"
    print(
        f"  [ERROR] {reason} on YouTube, YouTube Music, SoundCloud, "
        "ZAYCEV.NET and PESNI.ME"
    )
    if last_error:
        print(f"    {last_error}")
    return False


def _write_canonical_tags(
    path: Path,
    artist: str | list[str],
    title: str,
    album: str | None = None,
    year: str | None = None,
    track_number: int | None = None,
    album_artist: str | None = None,
) -> bool:
    try:
        media = MutagenFile(str(path), easy=False)
        if media is None:
            raise ValueError("unsupported or unreadable audio")
        set_tag(media, "artist", artist)
        primary_artist = artist[0] if isinstance(artist, list) and artist else str(artist)
        set_tag(media, "albumartist", album_artist or primary_artist)
        set_tag(media, "title", title)
        if album:
            set_tag(media, "album", album)
        if track_number is not None:
            from tags import _set_tracknum
            _set_tracknum(media, track_number)
        if year and hasattr(media, "tags"):
            from tags import _set_year
            _set_year(media, year)
        media.save()
        # Use exactly the same cleanup rules as prepare_music.py.
        with contextlib.redirect_stdout(io.StringIO()):
            process_file(path, True, True, True, True)
        return True
    except Exception as exc:
        print(f"  [ERROR] tag normalization failed for {path.name}: {exc}")
        return False


def embed_cover(path: Path, cover_data: bytes, mime: str = "image/jpeg") -> bool:
    try:
        media = MutagenFile(str(path), easy=False)
        if type(media).__name__ != "MP3":
            return False
        if media.tags is None:
            media.add_tags()
        media.tags.delall("APIC")
        media.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))
        media.save()
        return True
    except Exception as exc:
        print(f"  [WARN] cover embed failed for {path.name}: {exc}")
        return False


def _download_cover(url: str) -> tuple[bytes | None, str]:
    if not url:
        return None, ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(10 * 1024 * 1024 + 1)
            mime = response.headers.get_content_type()
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("cover exceeds 10 MiB")
        if not mime.startswith("image/"):
            raise ValueError(f"unexpected content type: {mime}")
        return data, mime
    except Exception as exc:
        print(f"  [WARN] cover download failed: {exc}")
        return None, ""


def _disk_title_slugs(directory: Path, relaxed: bool = False) -> set[str]:
    result = set()
    variant_fn = relaxed_title_variants if relaxed else title_variants
    if not directory.is_dir():
        return result
    for path in directory.iterdir():
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS:
            title = ""
            try:
                media = MutagenFile(str(path), easy=True)
                title = (media.get("title") or [""])[0] if media else ""
            except Exception:
                pass
            result.update(variant_fn(str(title) if title else extract_title_from_stem(path.stem)))
    return result


def _album_file_candidates(directory: Path) -> list[tuple[Path, set[str]]]:
    candidates = []
    if not directory.is_dir():
        return candidates
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        title = ""
        try:
            media = MutagenFile(str(path), easy=True)
            title = (media.get("title") or [""])[0] if media else ""
        except Exception:
            pass
        variants = set(title_variants(str(title) if title else extract_title_from_stem(path.stem)))
        candidates.append((path, variants))
    return candidates


def _normalize_complete_album(
    directory: Path,
    tracks: list[str],
    artist: str,
    album: str,
    year: str,
    cover_data: bytes | None,
    cover_mime: str,
    track_artists: list[list[str]] | None = None,
) -> bool:
    candidates = _album_file_candidates(directory)
    used = set()
    for track_number, track_title in enumerate(tracks, 1):
        wanted = title_variants(track_title)
        match = next(
            (
                path for path, variants in candidates
                if path not in used and wanted & variants
            ),
            None,
        )
        if match is None:
            print(f"  [ERROR] final validation cannot resolve track {track_number}: {track_title}")
            return False
        credited_artists = (
            track_artists[track_number - 1]
            if track_artists and track_number <= len(track_artists)
            else [artist]
        )
        if not _write_canonical_tags(
            match, credited_artists, track_title, album, year, track_number,
            album_artist=artist,
        ):
            return False
        if cover_data and match.suffix.casefold() == ".mp3":
            if not embed_cover(match, cover_data, cover_mime):
                return False
        used.add(match)

    extras = [path for path, _ in candidates if path not in used]
    for offset, path in enumerate(sorted(extras), 1):
        title = extract_title_from_stem(path.stem)
        try:
            media = MutagenFile(str(path), easy=True)
            title = str((media.get("title") or [title])[0]) if media else title
        except Exception:
            pass
        track_number = len(tracks) + offset
        print(f"  [INFO] extra track {track_number}: {title}")
        if not _write_canonical_tags(
            path, [artist], title, album, year, track_number, album_artist=artist
        ):
            return False
        if cover_data and path.suffix.casefold() == ".mp3":
            if not embed_cover(path, cover_data, cover_mime):
                return False
    return True


def cmd_list_artist(args) -> bool:
    albums = artist_album_entries(args.artist, args.lastfm_key, limit=100)
    if not albums:
        print(f"No albums found for '{args.artist}'")
        return False
    for index, album in enumerate(albums, 1):
        print(f"  {index:>3}. {album['name']}")
    return True


def cmd_download_album(args) -> bool:
    output_root = Path(args.out)
    print(f"\nLooking up album: {args.artist} — {args.album}")
    info = verified_album_info(args.artist, args.album, args.lastfm_key, args.delay)
    if info.get("error") and not info.get("tracks"):
        print(f"  [ERROR] metadata lookup failed: {info['error']}")
        return False
    if not info.get("tracks"):
        print("  [ERROR] no track listing found on Last.fm or MusicBrainz")
        return False

    artist_name = info["artist"]
    album_name = info["name"]
    folder_name = album_name
    if args.dest:
        destination_dir = Path(args.dest)
    else:
        artist_dir = _find_artist_folder(output_root, args.artist, artist_name)
        destination_dir = find_named_dir(artist_dir, album_name) or (
            artist_dir / safe_component(folder_name, "Unknown Album")
        )

    print(f"  Album: {album_name} ({info.get('year') or 'year unknown'}) by {artist_name}")
    print(f"  Tracks: {len(info['tracks'])}")
    print(f"  Verified by: {', '.join(info.get('verified_by') or [])}")
    print(f"  Destination: {destination_dir}")
    existing = _disk_title_slugs(destination_dir)
    existing_relaxed = _disk_title_slugs(destination_dir, relaxed=True)
    relaxed_tracks = [relaxed_title_variants(title) for title in info["tracks"]]
    durations = list(info.get("track_durations") or [])
    durations.extend([None] * (len(info["tracks"]) - len(durations)))
    track_artists = list(info.get("track_artists") or [])
    track_artists.extend([[artist_name]] * (len(info["tracks"]) - len(track_artists)))
    missing_tracks = []
    for track_number, track_title in enumerate(info["tracks"], 1):
        exact_match = slug(track_title) in existing or title_variants(track_title) & existing
        relaxed = relaxed_tracks[track_number - 1]
        relaxed_is_unique = sum(bool(relaxed & other) for other in relaxed_tracks) == 1
        if exact_match or (relaxed_is_unique and relaxed & existing_relaxed):
            print(f"  [SKIP] {track_title}")
            continue
        missing_tracks.append((track_number, track_title, durations[track_number - 1]))

    if not missing_tracks:
        cover_data, cover_mime = _download_cover(info.get("cover_url", ""))
        if not _normalize_complete_album(
            destination_dir, info["tracks"], artist_name, album_name,
            info.get("year", ""), cover_data, cover_mime,
            track_artists,
        ):
            print("\n[ERROR] album exists but failed final metadata validation")
            return False
        print(f"\nDownloaded: 0; failed: 0; total: {len(info['tracks'])}")
        return True
    if args.dry_run:
        for _, track_title, _ in missing_tracks:
            ytdlp_download(artist_name, track_title, destination_dir / "dry-run.mp3", True)
        print(f"\nWould download: {len(missing_tracks)}; total: {len(info['tracks'])}")
        return True

    destination_dir.parent.mkdir(parents=True, exist_ok=True)
    cover_data, cover_mime = _download_cover(info.get("cover_url", ""))
    staged_files = []
    failures = 0
    with tempfile.TemporaryDirectory(prefix=".album-", dir=destination_dir.parent) as temp_name:
        staging_dir = (
            Path(temp_name)
            / safe_component(artist_name, "Unknown Artist")
            / safe_component(album_name, "Unknown Album")
        )
        staging_dir.mkdir(parents=True)
        for track_number, track_title, expected_duration in missing_tracks:
            filename = (
                f"{safe_component(artist_name, 'Unknown Artist')} - "
                f"{safe_component(track_title, 'Untitled')}.mp3"
            )
            staged_path = staging_dir / filename
            if not ytdlp_download(
                artist_name,
                track_title,
                staged_path,
                album=album_name,
                expected_duration=expected_duration,
            ):
                failures += 1
                continue
            if not _write_canonical_tags(
                staged_path, track_artists[track_number - 1], track_title, album_name,
                info.get("year"), track_number, album_artist=artist_name,
            ):
                failures += 1
                continue
            if cover_data and not embed_cover(staged_path, cover_data, cover_mime):
                failures += 1
                continue
            staged_files.append((staged_path, destination_dir / filename))
            if args.delay:
                time.sleep(args.delay)

        if failures:
            print(
                f"\n[ERROR] album not published; staged files discarded: "
                f"{len(staged_files)} ready, {failures} failed"
            )
            return False

        collisions = [final for _, final in staged_files if final.exists()]
        if collisions:
            print(f"\n[ERROR] album not published; destination appeared concurrently: {collisions[0]}")
            return False

        destination_dir.mkdir(parents=True, exist_ok=True)
        for staged_path, final_path in staged_files:
            staged_path.replace(final_path)

    if not _normalize_complete_album(
        destination_dir, info["tracks"], artist_name, album_name,
        info.get("year", ""), cover_data, cover_mime,
        track_artists,
    ):
        print("\n[ERROR] album published but failed final metadata validation")
        return False
    print(f"\nDownloaded: {len(staged_files)}; failed: 0; total: {len(info['tracks'])}")
    return True


def cmd_download_track(args) -> bool:
    info = track_info(args.artist, args.title, args.lastfm_key, args.delay)
    artist = info["artist"]
    title = info["title"]
    output_dir = Path(args.out)
    destination = output_dir / (
        f"{safe_component(artist, 'Unknown Artist')} - {safe_component(title, 'Untitled')}.mp3"
    )
    if not ytdlp_download(artist, title, destination, args.dry_run, info.get("album")):
        return False
    return args.dry_run or _write_canonical_tags(destination, artist, title, info.get("album"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artist", metavar="ARTIST")
    parser.add_argument("--album", nargs=2, metavar=("ARTIST", "ALBUM"))
    parser.add_argument("--track", nargs=2, metavar=("ARTIST", "TITLE"))
    parser.add_argument("--out", default="./downloads", metavar="DIR")
    parser.add_argument("--dest", default="", metavar="DIR")
    parser.add_argument("--delay", type=float, default=0.5, metavar="SEC")
    parser.add_argument(
        "--lastfm-key",
        default=os.environ.get("LASTFM_KEY") or os.environ.get("LASTFM_APIKEY", ""),
        metavar="KEY",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-albums", action="store_true")
    args = parser.parse_args()

    if not args.lastfm_key and (args.artist or args.album or args.track):
        parser.error("--lastfm-key required (or set LASTFM_KEY)")
    if args.artist and not args.album and not args.track:
        ok = cmd_list_artist(args)
    elif args.album:
        args.artist, args.album = args.album
        ok = cmd_download_album(args)
    elif args.track:
        args.artist, args.title = args.track
        ok = cmd_download_track(args)
    else:
        parser.print_help()
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
