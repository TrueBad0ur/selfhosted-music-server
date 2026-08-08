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
from common import KEEP_REMIXES_MARKER, safe_component, scan_stale_staging_dirs, staging_lock
from metadata import (
    artist_album_entries,
    extract_title_from_stem,
    find_named_dir,
    katakana_to_romaji,
    relaxed_title_variants,
    slug,
    title_variants,
    track_info,
    verified_album_info,
)
from process_file import process_file
from tags import set_tag

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
_TITLE_MATCH_NOISE_RE = re.compile(
    r"\((?:feat(?:uring)?|ft\.?|official|remaster(?:ed)?|"
    r"lyric(?:\s+video)?|audio|visualizer|cover\w*|кавер\w*)[^)]*\)",
    re.IGNORECASE,
)
_UNREQUESTED_VARIANTS = (
    "remix", "instrumental", "karaoke", "cover", "acapella", "slowed",
    "sped up", "nightcore", "live", "feat", "trailer", "full album",
    "first two tracks", "medley", "mashup",
)
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")
_REMASTER_TITLE_RE = re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"\s*[\(\[][\d./\-]+[\)\]]\s*$")


def _find_artist_folder(music_root: Path, requested: str, canonical: str) -> Path:
    return (
        find_named_dir(music_root, requested)
        or find_named_dir(music_root, canonical)
        or find_named_dir(music_root, requested, fuzzy=True)
        or music_root / safe_component(canonical, "Unknown Artist")
    )


def _romaji_hit(title: str, candidate_title: str) -> bool:
    """Whether a katakana/kanji catalog title plausibly romanizes to a romaji
    video title. Katakana has a mechanical, dictionary-free reading (unlike
    kanji), so this can genuinely confirm identity rather than just guess from
    duration - "サンバ・ソレイユ" -> "sanbasoreiyu" is recognizably close to
    "sambasoleil" via fuzzy match even though the exact spelling differs (r/l,
    vowel-length choices are inherently ambiguous when romanizing a loanword)."""
    if not _CJK_RE.search(title) or _CJK_RE.search(candidate_title):
        return False
    romaji_wanted = slug(katakana_to_romaji(title))
    found = slug(candidate_title)
    if not romaji_wanted or not found:
        return False
    return (
        romaji_wanted == found
        or romaji_wanted in found
        or SequenceMatcher(None, romaji_wanted, found).ratio() >= 0.75
    )


def _best_downloaded_mp3(temp_dir: Path) -> Path | None:
    candidates = [path for path in temp_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".mp3"]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _title_match_key(value: str) -> str:
    # Strip trailing date/year groups only (not any parenthetical) - a romaji
    # gloss like "白い午後 (Shiroi Gogo)" must survive, since it's the only thing
    # that can ever match a romaji catalog title.
    value = _TITLE_MATCH_NOISE_RE.sub("", value)
    while True:
        stripped = _TRAILING_PAREN_RE.sub("", value)
        if stripped == value:
            break
        value = stripped
    return slug(value)


def _youtube_candidate_score(
    artist: str,
    title: str,
    candidate: dict,
    expected_duration: float | None = None,
    alt_artist: str | None = None,
) -> float:
    candidate_title = str(candidate.get("title") or "")
    wanted = _title_match_key(title)
    found = _title_match_key(candidate_title)
    artist_key = slug(artist)
    artist_keys = title_variants(artist)
    # alt_artist covers the resolved catalog name being in a different script
    # than the artist's real channel name (kanji vs romaji).
    if alt_artist:
        artist_keys = artist_keys | title_variants(alt_artist)
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
    is_remaster = bool(_REMASTER_TITLE_RE.search(candidate_title))
    if not duration and not (candidate.get("channel") or candidate.get("uploader")) \
            and found_without_artist != wanted:
        return -1
    if duration and float(duration) >= 600 and not expected_duration:
        return -1
    # A remaster/reissue can legitimately differ in length from the original;
    # ytdlp_download() applies a looser post-download check for those instead.
    if duration and expected_duration and not is_remaster:
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

    candidate_channel = str(candidate.get("channel") or "")
    channel_owner_slug = slug(re.sub(r"\s*-\s*Topic$", "", candidate_channel, flags=re.IGNORECASE))
    romaji_wanted = slug(katakana_to_romaji(title)) if _CJK_RE.search(title) else ""
    romaji_hit = bool(romaji_wanted) and (
        romaji_wanted == found_without_artist
        or romaji_wanted in found_without_artist
        or SequenceMatcher(None, romaji_wanted, found_without_artist).ratio() >= 0.78
    )
    if _CJK_RE.search(title) and not _CJK_RE.search(candidate_title) and romaji_hit:
        # Katakana has a mechanical romaji reading (kanji doesn't), so a real
        # text match is possible and safer than the duration-only fallback below.
        score = 110.0
    elif (
        _CJK_RE.search(title)
        and not _CJK_RE.search(candidate_title)
        and candidate_channel.endswith(" - Topic")
        and channel_owner_slug in artist_keys
    ):
        # No romaji reading available (kanji, or katakana that didn't match) -
        # fall back to duration + artist, but only the artist's OWN auto-generated
        # "<Artist> - Topic" channel (1:1 per track via Content ID) is trustworthy
        # here; a shared channel like a compilation's "Release - Topic" can pool
        # different same-duration songs, so it's left to fail the normal text
        # match below (see _youtube_candidates' cjk_topic_matches for that path).
        if not (duration and expected_duration):
            return -1
        tight_ratio = float(duration) / expected_duration
        if tight_ratio < 0.92 or tight_ratio > 1.08:
            return -1
        score = 70.0
    elif len(wanted) <= 4:
        # Short titles need a tight match, but a suffix match (not just exact)
        # handles duets where only the requested artist's prefix got stripped.
        if found_without_artist == wanted:
            score = 120.0
        elif found_without_artist.endswith(wanted):
            score = 105.0
        else:
            return -1
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
        str(candidate.get(key) or "")
        for key in ("title", "channel", "uploader", "artist", "artists", "creator")
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
    # An artist's own channel is rarely named the exact bare artist slug - it's
    # usually suffixed ("Uma2rman Band", "ArtistVEVO", "Artist Official"). A
    # prefix match against any known spelling of the artist still reliably
    # identifies channel ownership without needing an exact string match, and
    # matters: without this bonus a same-scoring reupload (e.g. a lyrics-video
    # aggregator channel) can outrank the artist's own upload on tie-breaking
    # search-result order alone.
    if channel_owner_slug and any(channel_owner_slug.startswith(k) for k in artist_keys if k):
        score += 15.0
    return score


def _youtube_candidate_details(video_id: str) -> dict:
    command = [
        "yt-dlp", "-J", f"https://www.youtube.com/watch?v={video_id}",
        "--no-playlist", "--extractor-args", "youtube:player_client=android,web",
        "--quiet", "--no-warnings",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {}


def _youtube_candidates(
    artist: str,
    title: str,
    album: str | None = None,
    expected_duration: float | None = None,
    alt_artist: str | None = None,
) -> list[dict]:
    queries = [f"{artist} {title}"]
    queries.append(f"{artist} {title} {album}" if album else f"{artist} {title} audio")
    # Resolved catalog artist and title can be in different scripts (kanji artist,
    # romaji title) - a mixed-script query finds worse results, so also try the
    # caller's originally-typed artist name if it differs.
    if alt_artist and slug(alt_artist) != slug(artist):
        queries.append(f"{alt_artist} {title}")
        if album:
            queries.append(f"{alt_artist} {title} {album}")
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
        entries = data.get("entries") or []
        # Duration-only fallback for a CJK title on a non-artist-owned "- Topic"
        # channel (e.g. a compilation's "Release - Topic") - only safe when exactly
        # one candidate in this batch qualifies, since two same-duration tracks by
        # a prolific artist can't be told apart without a text match.
        cjk_topic_matches = [
            e.get("id") for e in entries
            if e.get("id") and expected_duration and e.get("duration")
            and _CJK_RE.search(title)
            and not _CJK_RE.search(str(e.get("title") or ""))
            and abs(float(e["duration"]) - expected_duration) <= max(3.0, expected_duration * 0.05)
            and str(e.get("channel") or "").endswith(" - Topic")
        ]
        cjk_topic_unique_id = cjk_topic_matches[0] if len(cjk_topic_matches) == 1 else None
        for candidate in entries:
            video_id = candidate.get("id")
            if not video_id or video_id in seen:
                continue
            score = _youtube_candidate_score(artist, title, candidate, expected_duration, alt_artist)
            duration = candidate.get("duration")
            candidate_title = str(candidate.get("title") or "")
            is_cjk_topic_candidate = video_id == cjk_topic_unique_id
            # A romaji match is a real text confirmation, so it's trusted on any
            # "- Topic" channel - the Content ID fetch below confirms the artist.
            romaji_hit = _romaji_hit(title, candidate_title)
            exact_catalog_topic = (
                score < 0
                and expected_duration
                and duration
                and (
                    _title_match_key(candidate_title) == _title_match_key(title)
                    or romaji_hit
                    or is_cjk_topic_candidate
                )
                and abs(float(duration) - expected_duration)
                    <= max(3.0, expected_duration * 0.05)
                and str(candidate.get("channel") or "").endswith(" - Topic")
            )
            if exact_catalog_topic:
                detailed = _youtube_candidate_details(str(video_id))
                # Album name has the same script problem as the title (Content ID
                # album is often English/romaji) - skip the check when we already
                # trust the CJK/romaji signal above.
                album_matches = (
                    not album
                    or is_cjk_topic_candidate
                    or romaji_hit
                    or slug(str(detailed.get("album") or "")) == slug(album)
                )
                if album_matches:
                    detailed_score = _youtube_candidate_score(
                        artist, title, detailed, expected_duration, alt_artist
                    )
                    if detailed_score >= 0:
                        candidate = detailed
                        score = detailed_score
                    elif is_cjk_topic_candidate:
                        # Batch-unique duration match (cjk_topic_matches) plus the
                        # Content ID artist field naming the artist explicitly - two
                        # independent confirmations without needing a text match.
                        detailed_artist = slug(
                            str(detailed.get("artist") or detailed.get("creator") or "")
                        )
                        target_artist_keys = title_variants(artist) | (
                            title_variants(alt_artist) if alt_artist else set()
                        )
                        if detailed_artist and detailed_artist in target_artist_keys:
                            candidate = detailed
                            score = 90.0
            if score >= 0:
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
    alt_artist: str | None = None,
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
        score = _youtube_candidate_score(artist, title, candidate, expected_duration, alt_artist)
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
    alt_artist: str | None = None,
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
        score = _youtube_candidate_score(artist, title, candidate, expected_duration, alt_artist)
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
    alt_artist: str | None = None,
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
            score = _youtube_candidate_score(artist, title, candidate, expected_duration, alt_artist)
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
    alt_artist: str | None = None,
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
        score = _youtube_candidate_score(artist, title, candidate, expected_duration, alt_artist)
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


def _fetch_candidate(
    candidate: dict, destination: Path, expected_duration: float | None = None, relaxed: bool = False,
) -> tuple[bool, str, bool]:
    """Download a single candidate into an isolated temp dir and atomically
    publish it to `destination` if it passes validation. Returns
    (success, last_error, duration_only_mismatch) - the latter tells the
    caller a relaxed-tolerance retry might still be worth it."""
    last_error = ""
    duration_only_mismatch = False
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
                return False, str(exc), False
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
                return False, "timeout", False
            except FileNotFoundError:
                return False, "yt-dlp is not installed", False

        downloaded = _best_downloaded_mp3(temp_dir)
        if result.returncode != 0 or downloaded is None:
            last_error = result.stderr[:300] if result.stderr else "no MP3 produced"
            return False, last_error, False
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
                return False, "downloaded audio has invalid duration", False
            # A remaster is allowed a wider gap from the catalog duration.
            # The 10s floor (not a flat 8s) covers encoder padding plus
            # catalog-duration imprecision on obscure/bootleg discographies.
            if expected_duration:
                is_remaster = bool(_REMASTER_TITLE_RE.search(str(candidate.get("title") or "")))
                if relaxed:
                    tolerance = expected_duration * 0.30
                else:
                    tolerance = expected_duration * 0.30 if is_remaster else max(10.0, expected_duration * 0.05)
                if abs(actual_duration - expected_duration) > tolerance:
                    last_error = (
                        f"downloaded duration {actual_duration:.1f}s does not match "
                        f"catalog {expected_duration:.1f}s"
                    )
                    return False, last_error, not relaxed
            if not _audio_decodes(downloaded):
                return False, "downloaded audio does not decode completely", False

        if destination.exists():
            print("  [SKIP] completed by another worker")
            return True, "", False
        downloaded.replace(destination)
        source_name = candidate.get("channel") or candidate.get("uploader") or candidate.get("id")
        relaxed_note = "; relaxed duration match, only candidate" if relaxed else ""
        print(
            f"  ({destination.stat().st_size // 1024} KB; "
            f"{candidate['_source']}: {source_name}{relaxed_note})"
        )
        return True, "", False


def ytdlp_download(
    artist: str,
    title: str,
    destination: Path,
    dry_run: bool = False,
    album: str | None = None,
    expected_duration: float | None = None,
    alt_artist: str | None = None,
) -> bool:
    """Search all sources and download the first relevant candidate, atomically
    publishing it to `destination`."""
    destination = Path(destination)
    if destination.exists():
        print(f"  [SKIP] already exists: {destination.name}")
        return True
    if dry_run:
        print(f"  [DRY] would search yt-dlp: {artist} {title}")
        return True

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"  ↓ {destination.name}", end="", flush=True)

    # Try each source in turn, stopping at the first success - only fall back to
    # a second, looser-tolerance pass over everything already found if nothing
    # succeeded AND at least one candidate was rejected purely on duration
    # (scoring already confirmed title/artist, so that alone shouldn't turn the
    # only evidence available into a total failure).
    finders = (
        _youtube_candidates, _youtube_music_candidates,
        _soundcloud_candidates, _zaycev_candidates, _pesnime_candidates,
    )
    had_candidates = False
    all_candidates: list[dict] = []
    last_error = ""
    duration_only_mismatch = False
    for finder in finders:
        candidates = finder(artist, title, album, expected_duration, alt_artist=alt_artist)
        all_candidates.extend(candidates)
        for candidate in candidates:
            had_candidates = True
            ok, last_error, mismatch = _fetch_candidate(candidate, destination, expected_duration, relaxed=False)
            duration_only_mismatch = duration_only_mismatch or mismatch
            if ok:
                return True

    if duration_only_mismatch:
        for candidate in all_candidates:
            ok, last_error, _ = _fetch_candidate(candidate, destination, expected_duration, relaxed=True)
            if ok:
                return True

    reason = "no relevant result" if not had_candidates else "all relevant results failed"
    print(
        f"  [ERROR] {reason} on YouTube, YouTube Music, SoundCloud, "
        "ZAYCEV.NET and PESNI.ME"
    )
    if last_error:
        print(f"    {last_error}")
    return False


# Fixed per-source headers for the two direct-audio (non yt-dlp) sources - kept
# here so a reconstructed candidate (find_track_sources → download_replacement,
# crossing a web request/CLI boundary) doesn't need the caller to carry them.
_DIRECT_AUDIO_HEADERS = {
    "ZAYCEV.NET": {"User-Agent": "Mozilla/5.0", "Referer": "https://zaycev.net/"},
    "PESNI.ME": {"User-Agent": "Mozilla/5.0", "Referer": "https://hit.pesni.me/"},
}


def find_track_sources(
    artist: str, title: str, album: str | None = None,
    expected_duration: float | None = None, alt_artist: str | None = None,
    limit: int = 8,
) -> list[dict]:
    """List candidate sources for a track (best first, already ranked/filtered
    by each finder) without downloading anything - used to let a user pick a
    different source than whatever ytdlp_download picked originally."""
    finders = (
        _youtube_candidates, _youtube_music_candidates,
        _soundcloud_candidates, _zaycev_candidates, _pesnime_candidates,
    )
    seen = set()
    result = []
    for finder in finders:
        for candidate in finder(artist, title, album, expected_duration, alt_artist=alt_artist):
            key = (candidate["_source"], candidate["_download_url"])
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "source": candidate["_source"],
                "title": candidate.get("title") or "",
                "channel": candidate.get("channel") or candidate.get("uploader") or "",
                "duration": candidate.get("duration"),
                "url": candidate["_download_url"],
                "direct_audio": bool(candidate.get("_direct_audio")),
            })
            if len(result) >= limit:
                return result
    return result


def _read_replaceable_tags(path: Path) -> dict:
    """Snapshot the tags/cover a replacement download needs to restore -
    read before the file is backed up/overwritten."""
    from tags import get_tags, _get_tracknum, _extract_year

    info: dict = {}
    try:
        media = MutagenFile(str(path), easy=False)
        if media is None:
            return info
        tags = get_tags(media)
        info["artist"] = tags.get("artist", "")
        info["albumartist"] = tags.get("albumartist", "")
        info["album"] = tags.get("album", "")
        info["title"] = tags.get("title", "")
        info["track_number"] = _get_tracknum(media)
        info["year"] = _extract_year(media)
        if type(media).__name__ == "MP3" and media.tags:
            covers = media.tags.getall("APIC")
            if covers:
                info["cover_data"] = covers[0].data
                info["cover_mime"] = covers[0].mime
    except Exception:
        pass
    return info


def download_replacement(artist: str, title: str, destination: Path, source: str, source_url: str) -> bool:
    """Replace an existing on-disk track with a different source, preserving
    its current tags/cover/track number. The old file is only discarded once
    the new one has passed the same validation a normal download would."""
    destination = Path(destination)
    if not destination.is_file():
        print(f"  [ERROR] file not found: {destination}")
        return False

    old_tags = _read_replaceable_tags(destination)
    candidate = {
        "_download_url": source_url,
        "_source": source or "unknown",
        "_direct_audio": source in _DIRECT_AUDIO_HEADERS,
        "title": old_tags.get("title") or title,
    }
    if candidate["_direct_audio"]:
        candidate["_headers"] = _DIRECT_AUDIO_HEADERS[source]

    backup = destination.with_name(destination.name + ".replace-bak")
    destination.replace(backup)
    try:
        old_media = MutagenFile(str(backup), easy=False)
        expected_duration = (
            float(old_media.info.length)
            if old_media is not None and old_media.info is not None else None
        )
        ok, last_error, _ = _fetch_candidate(candidate, destination, expected_duration, relaxed=True)
        if not ok:
            print(f"  [ERROR] {last_error}")
            backup.replace(destination)
            return False
    except Exception:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise

    if old_tags.get("title"):
        _write_canonical_tags(
            destination, old_tags.get("artist") or artist, old_tags["title"],
            old_tags.get("album"), old_tags.get("year"), old_tags.get("track_number"),
            album_artist=old_tags.get("albumartist"),
        )
    if old_tags.get("cover_data"):
        embed_cover(destination, old_tags["cover_data"], old_tags.get("cover_mime", "image/jpeg"))
    backup.unlink(missing_ok=True)
    print(f"  [✓] replaced {destination.name} ({destination.stat().st_size // 1024} KB)")
    return True


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
    allow_missing: bool = False,
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
            if allow_missing:
                print(f"  [INFO] track {track_number} not on disk (partial album): {track_title}")
                continue
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


def _print_catalog_choices(choices: dict[str, list[str]], artist: str = "", album: str = "") -> list[str]:
    sources = list(choices)
    label = f" for {artist} — {album}" if artist or album else ""
    print(f"\n  Available catalog track lists{label}:")
    for source_index, source in enumerate(sources, 1):
        tracks = choices[source]
        print(f"\n  [{source_index}] {source} — {len(tracks)} track(s)")
        for track_index, title in enumerate(tracks, 1):
            print(f"      {track_index:>2}. {title}")
    return sources


def _choose_catalog_source(choices: dict[str, list[str]], artist: str = "", album: str = "") -> str:
    sources = _print_catalog_choices(choices, artist, album)
    if not sources:
        return ""
    if not sys.stdin.isatty():
        print(
            "\n  [ERROR] catalog choice requires an interactive terminal; "
            "rerun with --catalog-source SOURCE"
        )
        return ""
    while True:
        answer = input(
            f"\n  Select catalog [1-{len(sources)}] or q to cancel: "
        ).strip()
        if answer.casefold() in {"q", "quit", "cancel"}:
            return ""
        if answer.isdigit() and 1 <= int(answer) <= len(sources):
            return sources[int(answer) - 1]
        matched = next(
            (source for source in sources if source.casefold() == answer.casefold()),
            "",
        )
        if matched:
            return matched
        print("  Invalid selection.")


def _apply_keep_remixes_marker(destination_dir: Path, keep_remixes: bool) -> None:
    """Only ever sets the marker, never clears it - a plain re-download/fill
    run (no --keep-remixes) must not silently undo a choice made earlier."""
    if keep_remixes:
        (destination_dir / KEEP_REMIXES_MARKER).touch(exist_ok=True)


def cmd_download_album(args) -> bool:
    """Serializes concurrent attempts at the same album (a web click racing a
    manual CLI run, or two clicks before the button visibly disabled) - without
    this, each one independently sees 0 tracks on disk and redownloads the
    whole album, since the per-track "already exists" check only protects
    against a sequential rerun, not a concurrent one."""
    output_root = Path(args.out)
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / f".lock-{slug(args.artist)}--{slug(args.album)}"
    try:
        with staging_lock(lock_path):
            return _cmd_download_album_locked(args)
    except FileExistsError:
        print(f"\n[ERROR] another download for {args.artist} — {args.album} is already in progress")
        return False


def _cmd_download_album_locked(args) -> bool:
    output_root = Path(args.out)
    print(f"\nLooking up album: {args.artist} — {args.album}")
    catalog_source = getattr(args, "catalog_source", "")
    info = verified_album_info(
        args.artist, args.album, args.lastfm_key, args.delay, catalog_source or None
    )
    if info.get("selection_required"):
        selected_source = _choose_catalog_source(info.get("catalog_choices") or {}, args.artist, args.album)
        if not selected_source:
            return False
        print(f"\n  Selected catalog: {selected_source}")
        info = verified_album_info(
            args.artist, args.album, args.lastfm_key, args.delay, selected_source
        )
    if info.get("error") and not info.get("tracks"):
        print(f"  [ERROR] metadata lookup failed: {info['error']}")
        return False
    if not info.get("tracks"):
        print("  [ERROR] no track listing found on Last.fm or MusicBrainz")
        return False

    seen_titles = set()
    keep_indices = []
    for index, title in enumerate(info["tracks"]):
        key = slug(title)
        if key in seen_titles:
            continue
        seen_titles.add(key)
        keep_indices.append(index)
    if len(keep_indices) != len(info["tracks"]):
        print(f"  [WARN] catalog listing has duplicate track title(s); ignoring the repeat(s)")
        info["tracks"] = [info["tracks"][i] for i in keep_indices]
        if info.get("track_durations"):
            info["track_durations"] = [
                info["track_durations"][i] for i in keep_indices if i < len(info["track_durations"])
            ]
        if info.get("track_artists"):
            info["track_artists"] = [
                info["track_artists"][i] for i in keep_indices if i < len(info["track_artists"])
            ]

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
    # Tags/filenames must match the actual folder name, not the (possibly
    # different-script) catalog-resolved artist_name - same rule process_file.py
    # enforces elsewhere. artist_name is still used below for search/matching.
    folder_artist = destination_dir.parent.name

    print(f"  Album: {album_name} ({info.get('year') or 'year unknown'}) by {artist_name}")
    print(f"  Tracks: {len(info['tracks'])}")
    sources = ", ".join(info.get("verified_by") or [])
    if info.get("selected_by_user"):
        selected_catalog = info.get("selected_source") or sources
        print(f"  Selected catalog: {selected_catalog}")
    elif info.get("single_source"):
        print(f"  Metadata source: {sources} (single-source fallback)")
    else:
        print(f"  Verified by: {sources}")
    print(f"  Destination: {destination_dir}")
    existing = _disk_title_slugs(destination_dir)
    existing_relaxed = _disk_title_slugs(destination_dir, relaxed=True)
    relaxed_tracks = [relaxed_title_variants(title) for title in info["tracks"]]
    durations = list(info.get("track_durations") or [])
    durations.extend([None] * (len(info["tracks"]) - len(durations)))
    track_artists = list(info.get("track_artists") or [])
    track_artists.extend([[folder_artist]] * (len(info["tracks"]) - len(track_artists)))
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
            destination_dir, info["tracks"], folder_artist, album_name,
            info.get("year", ""), cover_data, cover_mime,
            track_artists,
        ):
            print("\n[ERROR] album exists but failed final metadata validation")
            return False
        _apply_keep_remixes_marker(destination_dir, args.keep_remixes)
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
            / safe_component(folder_artist, "Unknown Artist")
            / safe_component(album_name, "Unknown Album")
        )
        staging_dir.mkdir(parents=True)
        for track_number, track_title, expected_duration in missing_tracks:
            filename = (
                f"{safe_component(folder_artist, 'Unknown Artist')} - "
                f"{safe_component(track_title, 'Untitled')}.mp3"
            )
            staged_path = staging_dir / filename
            try:
                if not ytdlp_download(
                    artist_name,
                    track_title,
                    staged_path,
                    album=album_name,
                    expected_duration=expected_duration,
                    alt_artist=args.artist,
                ):
                    failures += 1
                    continue
                if not _write_canonical_tags(
                    staged_path, track_artists[track_number - 1], track_title, album_name,
                    info.get("year"), track_number, album_artist=folder_artist,
                ):
                    failures += 1
                    continue
                if cover_data and not embed_cover(staged_path, cover_data, cover_mime):
                    failures += 1
                    continue
                staged_files.append((staged_path, destination_dir / filename))
            finally:
                # Applies after every track, success or failure - a failed
                # search is often a sign YouTube is already rate-limiting this
                # IP, so skipping the pause on failure (as before) only made
                # the very next request hit the same limit again.
                if args.delay:
                    time.sleep(args.delay)

        if failures and not args.allow_partial:
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

    if failures:
        print(f"\n[WARN] publishing partial album: {failures} track(s) failed and were skipped")

    if not _normalize_complete_album(
        destination_dir, info["tracks"], folder_artist, album_name,
        info.get("year", ""), cover_data, cover_mime,
        track_artists, allow_missing=bool(failures),
    ):
        print("\n[ERROR] album published but failed final metadata validation")
        return False
    _apply_keep_remixes_marker(destination_dir, args.keep_remixes)
    print(f"\nDownloaded: {len(staged_files)}; failed: {failures}; total: {len(info['tracks'])}")
    return True


def cmd_download_track(args) -> bool:
    info = track_info(args.artist, args.title, args.lastfm_key, args.delay)
    artist = info["artist"]
    title = info["title"]
    output_dir = Path(args.out)
    destination = output_dir / (
        f"{safe_component(artist, 'Unknown Artist')} - {safe_component(title, 'Untitled')}.mp3"
    )
    if not ytdlp_download(
        artist, title, destination, args.dry_run, info.get("album"), alt_artist=args.artist
    ):
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
    parser.add_argument(
        "--catalog-source",
        default="",
        metavar="SOURCE",
        help="select a catalog track list when available catalogs conflict",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-albums", action="store_true")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="publish an album even if some tracks failed to download, instead of "
             "discarding the whole batch",
    )
    parser.add_argument(
        "--keep-remixes",
        action="store_true",
        help="mark this album so cleanup keeps its remix tracks instead of always "
             "dropping them in favor of the original",
    )
    parser.add_argument(
        "--list-track-sources", nargs=2, metavar=("ARTIST", "TITLE"),
        help="print candidate sources for a track as JSON, without downloading",
    )
    parser.add_argument(
        "--replace-track", nargs=3, metavar=("ARTIST", "TITLE", "FILE"),
        help="replace an existing track file with a different source (see --source/--source-url)",
    )
    parser.add_argument("--source", default="", metavar="NAME", help="source name for --replace-track")
    parser.add_argument("--source-url", default="", metavar="URL", help="source URL for --replace-track")
    args = parser.parse_args()

    if args.out and Path(args.out).is_dir():
        scan_stale_staging_dirs(Path(args.out), fix=True)

    if args.list_track_sources:
        artist, title = args.list_track_sources
        sources = find_track_sources(artist, title)
        print(json.dumps(sources, ensure_ascii=False))
        return 0
    if args.replace_track:
        artist, title, file_path = args.replace_track
        if not args.source_url:
            parser.error("--source-url required with --replace-track")
        return 0 if download_replacement(artist, title, Path(file_path), args.source, args.source_url) else 1

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
