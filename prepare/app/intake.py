"""Staged upload inspection and publication into the music library."""

from __future__ import annotations

import re
import os
import shutil
import uuid
import urllib.request
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import APIC

from album import clean_album_dirname
from checks import (
    _UNKNOWN_ARTIST,
    artist_from_filename,
    check_artists,
    strip_bad_chars,
    strip_watermarks,
)
from common import AUDIO_EXTENSIONS, audio_content_hash
from metadata import (
    extract_title_from_stem,
    find_named_dir,
    relaxed_title_variants,
    resolve_track_metadata,
    slug,
)
from process_file import process_file
from tags import get_tags, set_tag, _set_year

_INVALID_COMPONENT = re.compile(r"[\\/\x00-\x1f]")
BYPASS_RELATIVE_DIR = Path("All") / "All"
_SOURCE_FILENAME_ALBUM_RE = re.compile(r"_-_|_\d{5,}$|_$")


class IntakeError(ValueError):
    pass


def _has_cover(media) -> bool:
    if type(media).__name__ == "MP3":
        return bool(media.tags and media.tags.getall("APIC"))
    if type(media).__name__ == "FLAC":
        return bool(media.pictures)
    if type(media).__name__ == "MP4":
        return bool(media.tags and media.tags.get("covr"))
    return False


def _needs_enrichment(path: Path, album: str, title: str, artists: list[str], media) -> bool:
    source_album = slug(album) == slug(path.stem)
    title_album = bool(relaxed_title_variants(album) & relaxed_title_variants(title))
    return (
        source_album
        or bool(_SOURCE_FILENAME_ALBUM_RE.search(album))
        or not _has_cover(media)
        or (title_album and len(artists) <= 1)
    )


def _download_cover(url: str) -> tuple[bytes | None, str]:
    if not url:
        return None, ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(10 * 1024 * 1024 + 1)
            mime = response.headers.get_content_type()
        if len(data) > 10 * 1024 * 1024 or not mime.startswith("image/"):
            return None, ""
        return data, mime
    except Exception:
        return None, ""


def _apply_enriched_tags(path: Path, details: dict) -> None:
    media = MutagenFile(str(path), easy=False)
    if media is None:
        raise IntakeError("unsupported or unreadable audio")
    set_tag(media, "artist", details["artists"])
    set_tag(media, "albumartist", details["artist"])
    set_tag(media, "album", details["album"])
    set_tag(media, "title", details["title"])
    if details.get("year"):
        _set_year(media, details["year"])
    media.save()
    cover_data, cover_mime = _download_cover(details.get("cover_url", ""))
    if cover_data and type(media).__name__ == "MP3":
        media = MutagenFile(str(path), easy=False)
        media.tags.delall("APIC")
        media.tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover_data))
        media.save()


def safe_component(value: str, fallback: str) -> str:
    value = strip_watermarks(strip_bad_chars(value or ""))
    value = _INVALID_COMPONENT.sub("_", value).strip().strip(".")
    return value or fallback


def _unique_destination(directory: Path, filename: str, source: Path) -> tuple[Path, bool]:
    candidate = directory / filename
    if not candidate.exists():
        return candidate, False
    if candidate.is_file():
        if audio_content_hash(candidate) == audio_content_hash(source):
            return candidate, True
    stem, suffix = Path(filename).stem, Path(filename).suffix
    index = 2
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate, False
        index += 1


def inspect_file(path: Path) -> dict:
    path = Path(path)
    if path.suffix.casefold() not in AUDIO_EXTENSIONS:
        raise IntakeError(f"unsupported extension: {path.suffix or '<none>'}")
    try:
        media = MutagenFile(str(path), easy=False)
    except Exception as exc:
        raise IntakeError(f"unreadable audio: {exc}") from exc
    if media is None:
        raise IntakeError("unsupported or unreadable audio")

    tags = get_tags(media)
    raw_artist = str(tags.get("artist", "")).strip()
    album_artist = str(tags.get("albumartist", "")).strip()
    split_artists = check_artists({"artist": raw_artist})
    primary = album_artist or (split_artists[0] if split_artists else raw_artist)
    if not primary or primary.casefold() in _UNKNOWN_ARTIST:
        primary = artist_from_filename(path.stem) or ""
    title = str(tags.get("title", "")).strip() or extract_title_from_stem(path.stem)
    album = str(tags.get("album", "")).strip() or "Singles"

    if not primary:
        raise IntakeError("artist is missing in tags and filename")
    if not title:
        raise IntakeError("title is missing in tags and filename")

    artists = split_artists or [primary]
    enriched = {}
    if _needs_enrichment(path, album, title, artists, media):
        duration = float(media.info.length) if getattr(media, "info", None) else None
        enriched = resolve_track_metadata(artists, title, duration)
        if enriched:
            title = enriched.get("title") or title
            album = enriched.get("album") or album
            artists = enriched.get("artists") or artists

    artist_component = safe_component(primary, "Unknown Artist")
    album_component = safe_component(clean_album_dirname(album), "Singles")
    title_component = safe_component(title, "Untitled")
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "artist": primary,
        "artists": artists,
        "album": album_component,
        "title": title,
        "year": enriched.get("year", ""),
        "cover_url": enriched.get("cover_url", ""),
        "verified_by": enriched.get("verified_by", []),
        "enriched": bool(enriched),
        "relative_destination": str(
            Path(artist_component) / album_component /
            f"{artist_component} - {title_component}{path.suffix.casefold()}"
        ),
    }


def list_incoming(incoming_dir: Path) -> list[dict]:
    incoming_dir = Path(incoming_dir)
    if not incoming_dir.is_dir():
        return []
    result = []
    for path in sorted(incoming_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            result.append({**inspect_file(path), "status": "ready", "error": ""})
        except IntakeError as exc:
            result.append({
                "name": path.name,
                "size": path.stat().st_size,
                "status": "error",
                "error": str(exc),
            })
    return result


def resolve_incoming(incoming_dir: Path, name: str) -> Path:
    if not name or Path(name).name != name or name.startswith("."):
        raise IntakeError("invalid incoming filename")
    root = Path(incoming_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise IntakeError("incoming file not found")
    return path


def _copy_atomic(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".incoming-{uuid.uuid4().hex}{destination.suffix}"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def publish_file(source: Path, music_root: Path, bypass: bool = False) -> dict:
    source = Path(source)
    music_root = Path(music_root)
    if bypass:
        if source.suffix.casefold() not in AUDIO_EXTENSIONS:
            raise IntakeError(f"unsupported extension: {source.suffix or '<none>'}")
        directory = music_root / BYPASS_RELATIVE_DIR
        filename = safe_component(source.name, f"upload{source.suffix.casefold()}")
        details = {"artist": "All", "album": "All", "title": source.stem}
    else:
        details = inspect_file(source)
        requested_artist = safe_component(details["artist"], "Unknown Artist")
        artist_dir = find_named_dir(music_root, requested_artist) or music_root / requested_artist
        directory = artist_dir / safe_component(details["album"], "Singles")
        filename = (
            f"{safe_component(artist_dir.name, 'Unknown Artist')} - "
            f"{safe_component(details['title'], 'Untitled')}{source.suffix.casefold()}"
        )

    destination, duplicate = _unique_destination(directory, filename, source)
    if duplicate:
        source.unlink()
        return {
            "name": source.name,
            "status": "duplicate",
            "destination": str(destination.relative_to(music_root)),
            "bypass": bypass,
        }

    _copy_atomic(source, destination)
    if not bypass:
        if details.get("enriched"):
            _apply_enriched_tags(destination, details)
        process_file(destination, True, True, True, True, library_root=music_root)
        if not destination.exists():
            matches = list(directory.glob(f"{Path(filename).stem}*{destination.suffix}"))
            if matches:
                destination = matches[0]
        if not destination.exists():
            raise IntakeError("normalization did not produce a destination file")
    source.unlink()
    return {
        "name": source.name,
        "status": "published",
        "destination": str(destination.relative_to(music_root)),
        "bypass": bypass,
        "artist": details["artist"],
        "album": details["album"],
    }


def publish_incoming(
    incoming_dir: Path,
    music_root: Path,
    names: list[str] | None = None,
    bypass: bool = False,
) -> list[dict]:
    if names is None:
        names = [
            path.name for path in Path(incoming_dir).iterdir()
            if path.is_file() and not path.name.startswith(".")
        ]
    results = []
    for name in names:
        try:
            source = resolve_incoming(incoming_dir, name)
            results.append(publish_file(source, music_root, bypass=bypass))
        except Exception as exc:
            results.append({"name": name, "status": "error", "error": str(exc), "bypass": bypass})
    return results
