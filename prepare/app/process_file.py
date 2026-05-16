import os
import re as _re
import sys
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TDRC
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)

from common import AUDIO_EXTENSIONS, EXCLUDE_DIRS, is_excluded, _FORMAT_PRIORITY
from encoding import check_encoding
from tags import get_tags, set_tag, _frame_text
from checks import (
    check_bad_chars, check_watermarks, check_id3_junk_frames,
    check_spam_covers, check_flac_junk_tags, check_title_artist_prefix,
    check_artists, artist_from_path, artist_from_filename,
    strip_watermarks, strip_bad_chars, _UNKNOWN_ARTIST,
)
from album import clean_album_dirname

_TITLE_MEDIA_SUFFIX_RE = _re.compile(
    r'\s*[\(\[]\s*(?:official\s+)?(?:music\s+)?(?:lyric\s+)?(?:video|audio|mv|hd|4k|vevo)\s*[\)\]]',
    _re.IGNORECASE,
)


def process_file(path: Path, fix: bool, check_enc: bool, check_art: bool, check_alb: bool):
    issues = []
    applied = []

    try:
        f = MutagenFile(str(path), easy=False)
    except Exception as e:
        if path.suffix.lower() == ".mp3" and "sync" in str(e).lower():
            try:
                id3 = ID3(str(path))
                class _FakeMP3:
                    def __init__(self, tags): self.tags = tags
                    def save(self): id3.save()
                    def add_tags(self): pass
                f = _FakeMP3(id3)
                type(f).__name__ = "MP3"
            except Exception:
                print(f"  [SKIP] {path.name}  (corrupted MPEG frame)")
                return
        else:
            print(f"  [ERROR] Cannot read: {e}")
            return

    if f is None:
        return

    tags = get_tags(f)
    if not tags:
        issues.append("no tags found")

    tags = dict(tags)

    bad_fixes = check_bad_chars(tags)
    for field, fixed in bad_fixes.items():
        issues.append(f"bad chars [{field}]: '{tags[field]}' → '{fixed}'")
        if fix:
            set_tag(f, field, fixed)
            applied.append(f"stripped bad chars [{field}]")
        tags[field] = fixed

    wm_fixes = check_watermarks(tags)
    for field, fixed in wm_fixes.items():
        issues.append(f"watermark [{field}]: '{tags[field]}' → '{fixed}'")
        if fix:
            set_tag(f, field, fixed)
            applied.append(f"stripped watermark [{field}]")
        tags[field] = fixed

    for frame_key, frame_text in check_id3_junk_frames(f):
        short = frame_text[:60].replace("\n", " ")
        issues.append(f"junk frame [{frame_key}]: '{short}'")
        if frame_key.startswith("USLT"):
            issues.append("[?] verify audio content — embedded lyrics suggest wrong yt-dlp download")
        if fix:
            del f.tags[frame_key]
            applied.append(f"deleted junk frame [{frame_key}]")

    for flac_key, flac_val in check_flac_junk_tags(f):
        short = flac_val[:60]
        issues.append(f"junk vorbis key [{flac_key}]: '{short}'")
        if fix:
            has_standard = bool((f.get("albumartist") or [None])[0])
            if not has_standard and flac_val:
                f["albumartist"] = [flac_val]
                applied.append(f"migrated [{flac_key}] → albumartist='{flac_val}'")
            del f[flac_key]
            applied.append(f"deleted junk vorbis key [{flac_key}]")

    for kind, ref, size in check_spam_covers(f):
        issues.append(f"spam cover [{ref}]: {size} bytes")
        if fix:
            if kind == "MP3_APIC":
                del f.tags[ref]
                applied.append(f"deleted spam cover [{ref}]")
            elif kind == "FLAC_PIC":
                pics = [p for i, p in enumerate(f.pictures) if i != ref]
                f.clear_pictures()
                for p in pics:
                    f.add_picture(p)
                applied.append(f"deleted spam cover [picture #{ref}]")
            elif kind == "MP4_COVER":
                covers = list(f.tags.get("covr", []))
                covers.pop(ref)
                f.tags["covr"] = covers
                applied.append(f"deleted spam cover [covr #{ref}]")

    if check_enc:
        enc_fixes = check_encoding(tags)
        for field, fixed in enc_fixes.items():
            issues.append(f"encoding [{field}]: '{tags[field]}' → '{fixed}'")
            if fix:
                set_tag(f, field, fixed)
                applied.append(f"fixed encoding [{field}]")

    if check_art:
        artist_val = tags.get("artist", "")
        if not artist_val or artist_val.strip().lower() in _UNKNOWN_ARTIST:
            guessed = artist_from_path(path) or artist_from_filename(path.stem)
            if guessed:
                source = "path" if artist_from_path(path) else "filename"
                issues.append(f"unknown artist → '{guessed}' (from {source})")
                tags["artist"] = guessed
                if fix:
                    set_tag(f, "artist", guessed)
                    applied.append(f"set artist to '{guessed}'")

        split = check_artists(tags)
        if split:
            issues.append(f"multi-artist: '{tags.get('artist')}' → {split}")
            if fix:
                set_tag(f, "artist", split)
                applied.append(f"split artist into {split}")
                if not tags.get("albumartist"):
                    set_tag(f, "albumartist", split[0])
                    applied.append(f"set albumartist to '{split[0]}'")

        clean_title = check_title_artist_prefix(tags, path.stem)
        if clean_title:
            current_title = tags.get("title") or path.stem
            issues.append(f"title: '{current_title}' → '{clean_title}'")
            if fix:
                set_tag(f, "title", clean_title)
                applied.append(f"set title to '{clean_title}'")
            tags["title"] = clean_title

        raw_title = tags.get("title", "")
        if raw_title and _TITLE_MEDIA_SUFFIX_RE.search(raw_title):
            stripped_title = _TITLE_MEDIA_SUFFIX_RE.sub("", raw_title).strip()
            if stripped_title and stripped_title != raw_title:
                issues.append(f"title media suffix: '{raw_title}' → '{stripped_title}'")
                if fix:
                    set_tag(f, "title", stripped_title)
                    applied.append(f"stripped title suffix: '{stripped_title}'")
                tags["title"] = stripped_title

    if type(f).__name__ == "MP3" and f.tags is not None:
        tdrc = f.tags.get("TDRC")
        tdrl = f.tags.get("TDRL")
        tyer = f.tags.get("TYER")
        year_src = tdrl or tyer
        if not tdrc and year_src:
            year_val = str(_frame_text(year_src))[:4]
            issues.append(f"date: missing TDRC, copying from {'TDRL' if tdrl else 'TYER'}: '{year_val}'")
            if fix:
                f.tags.add(TDRC(encoding=0, text=[year_val]))
                if tdrl:
                    del f.tags["TDRL"]
                applied.append(f"set TDRC='{year_val}' (removed {'TDRL' if tdrl else 'TYER'})")

    if check_alb:
        excluded_dir = next((p for p in path.parts if p in EXCLUDE_DIRS), None)
        if excluded_dir:
            correct_album = excluded_dir
            correct_albumartist = None
        else:
            correct_album = clean_album_dirname(strip_watermarks(strip_bad_chars(path.parent.name)))
            correct_albumartist = path.parent.parent.name or None

        current_album = tags.get("album", "")
        if current_album != correct_album:
            issues.append(f"album: '{current_album or '<empty>'}' → '{correct_album}'")
            if fix:
                set_tag(f, "album", correct_album)
                applied.append(f"set album to '{correct_album}'")

        if correct_albumartist:
            current_albumartist = tags.get("albumartist", "")
            if current_albumartist != correct_albumartist:
                issues.append(f"albumartist: '{current_albumartist or '<empty>'}' → '{correct_albumartist}'")
                if fix:
                    set_tag(f, "albumartist", correct_albumartist)
                    applied.append(f"set albumartist to '{correct_albumartist}'")

            current_artist = tags.get("artist", "")
            if (current_artist != correct_albumartist
                    and not current_artist.startswith(correct_albumartist)):
                issues.append(f"artist: '{current_artist or '<empty>'}' → '{correct_albumartist}'")
                if fix:
                    set_tag(f, "artist", correct_albumartist)
                    applied.append(f"set artist to '{correct_albumartist}'")

    new_stem = strip_watermarks(path.stem)
    new_path = path.with_name(new_stem + path.suffix)
    if new_stem != path.stem:
        issues.append(f"filename watermark: '{path.name}' → '{new_path.name}'")

    if issues:
        print(f"\n  {path}")
        for issue in issues:
            print(f"      [{'✓' if fix and issue in [f'fixed encoding [{k}]' for k in check_encoding(tags)] else '!'}] {issue}")
        if fix:
            if applied:
                try:
                    f.save()
                    for a in applied:
                        print(f"      [✓] {a}")
                except Exception as e:
                    print(f"      [ERROR] save failed: {e}")
            if new_stem != path.stem:
                try:
                    path.rename(new_path)
                    print(f"      [✓] renamed to '{new_path.name}'")
                except Exception as e:
                    print(f"      [ERROR] rename failed: {e}")


def scan_filename_prefixes(root: Path, fix: bool) -> int:
    """Detect audio files whose filename artist prefix differs from the artist folder name."""
    albums_found: int = 0

    for dirpath, _, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) != 2:
            continue

        artist_dir: str = p.parent.name
        audio_files: list[str] = [
            fn for fn in filenames
            if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        issues: list[tuple[str, str]] = []
        for fn in sorted(audio_files):
            stem = Path(fn).stem
            ext  = Path(fn).suffix
            if ' - ' not in stem:
                continue
            prefix, title = stem.split(' - ', 1)
            if _re.fullmatch(r'\s*\d+\s*', prefix):
                continue
            if prefix == artist_dir:
                continue
            feat_match = _re.match(
                r'^(.+?)\s*(?:,|feat\.?|ft\.?|featuring|and|&)\s+(.+)$',
                prefix, _re.IGNORECASE
            )
            if feat_match and feat_match.group(1) == artist_dir:
                feat_part = feat_match.group(2)
                new_name  = f"{artist_dir} - {title} (feat. {feat_part}){ext}"
            else:
                new_name = f"{artist_dir} - {title}{ext}"
            issues.append((fn, new_name))

        if not issues:
            continue

        albums_found += 1
        print(f"\n  {rel}")
        for old_name, new_name in issues:
            print(f"      [!] '{old_name}'")
            print(f"          → '{new_name}'")
            if fix:
                old_path = p / old_name
                new_path = p / new_name
                if not new_path.exists():
                    try:
                        old_path.rename(new_path)
                        print(f"          [✓] renamed")
                    except Exception as e:
                        print(f"          [ERROR] {e}")
                else:
                    src_pri = _FORMAT_PRIORITY.get(old_path.suffix.lower(), 99)
                    dst_pri = _FORMAT_PRIORITY.get(new_path.suffix.lower(), 99)
                    if src_pri < dst_pri:
                        new_path.unlink()
                        old_path.rename(new_path)
                        print(f"          [✓] replaced existing with better format")
                    else:
                        old_path.unlink()
                        print(f"          [✓] dropped (target already exists, same/better format)")

    return albums_found



