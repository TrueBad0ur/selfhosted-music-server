import os
import subprocess
import tempfile
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import APIC

from common import AUDIO_EXTENSIONS, is_excluded
from tags import _extract_year, _get_tracknum, _set_tracknum, _set_year, get_tags, set_tag


def _read_source_tags(path: Path) -> dict:
    info: dict = {}
    try:
        media = MutagenFile(str(path), easy=False)
        if media is None:
            return info
        info.update(get_tags(media))
        info["track_number"] = _get_tracknum(media)
        info["year"] = _extract_year(media)
        type_name = type(media).__name__
        if type_name == "MP3" and media.tags:
            covers = media.tags.getall("APIC")
            if covers:
                info["cover_data"] = covers[0].data
                info["cover_mime"] = covers[0].mime
        elif type_name == "MP4" and media.tags:
            covers = media.tags.get("covr")
            if covers:
                is_png = covers[0].imageformat == covers[0].FORMAT_PNG
                info["cover_data"] = bytes(covers[0])
                info["cover_mime"] = "image/png" if is_png else "image/jpeg"
    except Exception:
        pass
    return info


def _transcode_to_mp3(source: Path) -> Path | None:
    """Transcode `source` to a sibling .mp3 (same stem), copying over its tags
    and cover art, then remove the original. Returns the new path, or None on
    failure - the original is left untouched in that case."""
    destination = source.with_suffix(".mp3")
    if destination.exists():
        print(f"      [SKIP] destination already exists: {destination.name}")
        return None

    old_tags = _read_source_tags(source)
    with tempfile.TemporaryDirectory(prefix=".transcode-", dir=source.parent) as temp_name:
        temp_path = Path(temp_name) / destination.name
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source),
             "-codec:a", "libmp3lame", "-q:a", "0", str(temp_path)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not temp_path.exists():
            print(f"      [ERROR] transcode failed: {result.stderr[:200] if result.stderr else 'unknown error'}")
            return None
        temp_path.replace(destination)

    try:
        media = MutagenFile(str(destination), easy=False)
        for key in ("artist", "albumartist", "album", "title"):
            if old_tags.get(key):
                set_tag(media, key, old_tags[key])
        if old_tags.get("track_number") is not None:
            _set_tracknum(media, old_tags["track_number"])
        if old_tags.get("year"):
            _set_year(media, old_tags["year"])
        media.save()
        if old_tags.get("cover_data"):
            if media.tags is None:
                media.add_tags()
            media.tags.delall("APIC")
            media.tags.add(APIC(
                encoding=3, mime=old_tags.get("cover_mime", "image/jpeg"),
                type=3, desc="Cover", data=old_tags["cover_data"],
            ))
            media.save()
    except Exception as exc:
        print(f"      [ERROR] tag copy failed for {destination.name}: {exc}")

    source.unlink()
    return destination


def scan_mixed_formats(root: Path, fix: bool) -> int:
    """Find album folders mixing .m4a with .mp3/.flac tracks and transcode the
    .m4a ones to .mp3. Navidrome (at least this build) splits such a folder
    into two separate albums purely by container format, even though every
    tag (artist/album/etc.) matches - so the fix is to make the whole album
    one consistent format, not just a tag/DB-side patch."""
    albums_found = 0

    for dirpath, dirnames, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            dirnames.clear()
            continue

        audio_files = [p / f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS]
        m4a_files = [f for f in audio_files if f.suffix.lower() == ".m4a"]
        other_files = [f for f in audio_files if f.suffix.lower() in (".mp3", ".flac")]
        if not m4a_files or not other_files:
            continue

        albums_found += 1
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        print(f"\n  {rel}")
        for f in m4a_files:
            print(f"      [!] mixed format: {f.name} ({len(m4a_files)}x.m4a vs {len(other_files)}x other)")
            if fix:
                new_path = _transcode_to_mp3(f)
                if new_path:
                    print(f"      [✓] transcoded → {new_path.name}")

    print(f"\n{'─'*60}")
    print(f"  Albums with mixed formats: {albums_found}")
    if fix:
        print(f"  Mode: FIX applied")
    else:
        print(f"  Mode: DRY-RUN (use --fix to apply changes)")
    return albums_found
