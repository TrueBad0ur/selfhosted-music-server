#!/usr/bin/env python3
"""
Music library metadata checker and fixer.

Usage:
    python3 prepare_music.py /path/to/music              # dry-run, report only
    python3 prepare_music.py /path/to/music --fix        # apply fixes
    python3 prepare_music.py /path/to/music --fix --encoding-only
    python3 prepare_music.py /path/to/music --fix --artists-only

Requires: pip install mutagen
"""

import os
import sys
import argparse
from pathlib import Path

try:
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".wav", ".m4b"}

# Directories to skip for album/albumartist enforcement — flat dumps without Artist/Album structure
EXCLUDE_DIRS = {"All", "All-Rap", "Garazh", "ReverseDungeon", "Classics", "TexnoFunk"}

def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in path.parts)

# ── encoding ──────────────────────────────────────────────────────────────────

MOJIBAKE_THRESHOLD = 0.9  # fraction of alpha chars that must be non-ASCII latin-1

def looks_like_mojibake(s: str) -> bool:
    """Detect cp1251 bytes decoded as latin-1 (e.g. Àêóñòè÷ instead of Акусти).
    Only flags strings where >=90% of alphabetic chars are non-ASCII latin-1,
    to avoid false positives on words like Motörhead or Prélude.
    """
    if not s:
        return False
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return False
    try:
        fixed = raw.decode("cp1251")
    except UnicodeDecodeError:
        return False

    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return False
    suspicious = [c for c in alpha if ord(c) >= 0x80]
    if len(suspicious) / len(alpha) < MOJIBAKE_THRESHOLD:
        return False

    has_cyrillic      = any("\u0400" <= c <= "\u04ff" for c in fixed)
    orig_has_cyrillic = any("\u0400" <= c <= "\u04ff" for c in s)
    return has_cyrillic and not orig_has_cyrillic

def fix_mojibake(s: str) -> str:
    return s.encode("latin-1").decode("cp1251")

def _is_wrongendian_ascii(c: str) -> bool:
    """U+XX00 where XX is printable ASCII — all-ASCII UTF-16LE read as BE."""
    code = ord(c)
    return (code & 0xFF) == 0x00 and 0x20 < (code >> 8) <= 0x7E

def _is_wrongendian_cyrillic(c: str) -> bool:
    """U+XX04 where 0x04XX is a Cyrillic codepoint — Cyrillic UTF-16LE read as BE."""
    code = ord(c)
    if (code & 0xFF) != 0x04:
        return False
    swapped = ((code & 0xFF) << 8) | (code >> 8)  # = 0x0400 | high_byte
    return 0x0400 <= swapped <= 0x04FF

def _is_wrongendian(c: str) -> bool:
    return _is_wrongendian_ascii(c) or _is_wrongendian_cyrillic(c)

def looks_like_utf16le_as_be(s: str) -> bool:
    """Detect UTF-16LE text read as UTF-16BE.
    Examples: 'Chop Suey!' → '䌀栀漀瀀 匀甀攀礀℀'
              'Animal ДжZ' → 'Animal ᐄ㘄Z'
    """
    if not s:
        return False
    chars = [c for c in s if c != " "]
    if not chars:
        return False
    # Even a single wrong-endian Cyrillic char is definitive evidence
    if any(_is_wrongendian_cyrillic(c) for c in chars):
        return True
    # For all-ASCII case require ≥80% of non-space chars to be wrong-endian
    hits = sum(1 for c in chars if _is_wrongendian_ascii(c))
    return hits / len(chars) >= 0.8

def fix_utf16le_as_be(s: str) -> str:
    result = []
    for c in s:
        if _is_wrongendian(c):
            code = ord(c)
            result.append(chr(((code & 0xFF) << 8) | (code >> 8)))
        else:
            result.append(c)
    return "".join(result)

# ── tag helpers ───────────────────────────────────────────────────────────────

def get_tags(f) -> dict:
    """Return a normalized dict: artist, albumartist, album, title."""
    tags = {}
    t = type(f).__name__

    if t == "MP3":
        id3 = f.tags
        if id3 is None:
            return tags
        def _get(key):
            frame = id3.get(key)
            return str(frame) if frame else None
        tags["artist"]       = _get("TPE1")
        tags["albumartist"]  = _get("TPE2")
        tags["album"]        = _get("TALB")
        tags["title"]        = _get("TIT2")
        tags["tracknumber"]  = _get("TRCK")

    elif t == "FLAC":
        def _get(key):
            v = f.get(key)
            return v[0] if v else None
        tags["artist"]       = _get("artist")
        tags["albumartist"]  = _get("albumartist")
        tags["album"]        = _get("album")
        tags["title"]        = _get("title")
        tags["tracknumber"]  = _get("tracknumber")

    elif t == "MP4":
        def _get(key):
            v = f.tags.get(key) if f.tags else None
            return v[0] if v else None
        tags["artist"]       = _get("\xa9ART")
        tags["albumartist"]  = _get("aART")
        tags["album"]        = _get("\xa9alb")
        tags["title"]        = _get("\xa9nam")

    else:
        # OGG, Opus, etc — vorbis comment style
        if not f.tags:
            return tags
        def _get(key):
            v = f.tags.get(key)
            return v[0] if v else None
        tags["artist"]       = _get("artist")
        tags["albumartist"]  = _get("albumartist")
        tags["album"]        = _get("album")
        tags["title"]        = _get("title")
        tags["tracknumber"]  = _get("tracknumber")

    return {k: v for k, v in tags.items() if v}

def set_tag(f, key: str, value):
    """Write a tag back. value can be str or list of str (for multi-value artist)."""
    t = type(f).__name__

    if t == "MP3":
        from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TRCK
        mapping = {"title": TIT2, "artist": TPE1, "albumartist": TPE2, "album": TALB, "tracknumber": TRCK}
        frame_cls = mapping.get(key)
        if frame_cls:
            if f.tags is None:
                f.add_tags()
            text = value if isinstance(value, list) else [value]
            f.tags[frame_cls.__name__] = frame_cls(encoding=3, text=text)

    elif t == "FLAC":
        f[key] = value

    elif t == "MP4":
        mapping = {"title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART", "album": "\xa9alb"}
        mp4_key = mapping.get(key)
        if mp4_key:
            if f.tags is None:
                f.add_tags()
            f.tags[mp4_key] = value if isinstance(value, list) else [value]

    else:
        if f.tags is None:
            f.add_tags()
        f.tags[key] = value if isinstance(value, list) else [value]

# ── checks ────────────────────────────────────────────────────────────────────

def check_encoding(tags: dict) -> dict:
    """Return {field: fixed_value} for encoding issues (mojibake or UTF-16LE-as-BE)."""
    fixes = {}
    for field, value in tags.items():
        if not value:
            continue
        if looks_like_utf16le_as_be(value):
            fixes[field] = fix_utf16le_as_be(value)
        elif looks_like_mojibake(value):
            fixes[field] = fix_mojibake(value)
    return fixes

import re as _re

# ── bad-char cleanup ──────────────────────────────────────────────────────────

_BAD_CHARS = {"\ufffd"}  # Unicode replacement character (□)

def has_bad_chars(s: str) -> bool:
    return any(c in _BAD_CHARS for c in s)

def strip_bad_chars(s: str) -> str:
    return "".join(c for c in s if c not in _BAD_CHARS).strip()

def check_bad_chars(tags: dict) -> dict:
    """Return {field: cleaned_value} for any fields containing broken-encoding chars."""
    return {f: strip_bad_chars(v) for f, v in tags.items() if v and has_bad_chars(v)}

def check_title_artist_prefix(tags: dict, stem: str = "") -> str | None:
    """Return cleaned title if:
    - title tag has 'Artist - Title' prefix matching artist tag, or
    - title tag is missing and filename stem has 'Artist - Title' pattern.
    """
    title = tags.get("title", "")
    artist = tags.get("artist", "")
    source = title if title else stem
    if not source or " - " not in source:
        return None
    prefix, _, rest = source.partition(" - ")
    if not rest:
        return None
    if artist and prefix.strip().lower() == artist.strip().lower():
        return rest.strip()
    return None

# ── watermark cleanup ────────────────────────────────────────────────────────
# Matches [muzmo.ru], [zaycev.net], [mp3load.net], etc. — anything like [word.ext]
_WATERMARK_RE = _re.compile(r"\s*[\[\(][\w.-]+\.[a-zA-Z]{2,}[\]\)]", _re.IGNORECASE)
# Matches http(s)://muzmo.ru, http://zaycev.net, etc. — URL-form watermarks in junk frames
_WATERMARK_URL_RE = _re.compile(r"https?://[\w.-]+\.[\w]{2,}", _re.IGNORECASE)

# ID3 frames deleted only if they contain a watermark URL
_JUNK_FRAME_KEYS = {"WXXX", "TCOP"}

# Known spam cover art hashes (MD5) — e.g. muzmo.ru banner embedded as APIC
_SPAM_COVER_MD5 = {
    "dfb57101e4ec83f5cae72bc0f28d155f",  # muzmo.ru 85873-byte banner
}

# TXXX frame descriptions that shadow standard tags and confuse players/servers
_JUNK_TXXX_DESCS = {
    "album artist",
    "albumartist",
    "album_artist",
    "totaltracks",
    "totaldiscs",
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
}

# Non-standard vorbis comment keys in FLAC that shadow standard fields
# "album artist" (with space) is non-standard — standard is "albumartist"
_JUNK_FLAC_KEYS = {"album artist", "album_artist"}

def _frame_text(frame) -> str:
    """Extract text content from an ID3 frame."""
    if hasattr(frame, "url"):
        return frame.url
    if hasattr(frame, "text"):
        t = frame.text
        return str(t[0]) if isinstance(t, list) else str(t)
    return str(frame)

_SPAM_COMMENT_RE = _re.compile(
    r"(^collected\s+by\b"           # "Collected by LeXiKC"
    r"|@[\w.-]+\.\w{2,}"            # email address
    r"|www\."                        # www. domain
    r"|^[\s0-9a-fA-F]{20,}$"        # hex/numeric garbage (iTunNORM, ReplayGain)
    r"|ya\s*music\b"                 # YA Music app
    r"|itunes\b"                     # iTunes
    r"|только\s+для\s+ознакомления"  # "for review only" in Russian (often mojibake too)
    r")",
    _re.IGNORECASE,
)

def _is_spam_comment(text: str) -> bool:
    """Return True if a COMM frame text looks like spam/ads rather than real metadata."""
    t = text.strip()
    if not t:
        return True
    # Check decoded mojibake too
    decoded = t
    try:
        decoded = t.encode("latin-1").decode("cp1251")
    except Exception:
        pass
    for s in (t, decoded):
        if _SPAM_COMMENT_RE.search(s):
            return True
        if _WATERMARK_URL_RE.search(s):
            return True
    return False

def check_id3_junk_frames(f) -> list:
    """Return list of (key, reason) to delete: watermark URL frames and rogue TXXX tags."""
    if type(f).__name__ != "MP3" or not f.tags:
        return []
    junk = []
    for key in list(f.tags.keys()):
        frame_type = key.split(":")[0]
        # TXXX frames whose description shadows a standard tag
        if frame_type == "TXXX":
            desc = key[5:].lower() if key.startswith("TXXX:") else ""
            if desc in _JUNK_TXXX_DESCS:
                junk.append((key, _frame_text(f.tags[key])))
            continue
        if frame_type == "COMM":
            text = _frame_text(f.tags[key])
            if _is_spam_comment(text):
                junk.append((key, text))
            continue
        if frame_type not in _JUNK_FRAME_KEYS:
            continue
        text = _frame_text(f.tags[key])
        if _WATERMARK_URL_RE.search(text):
            junk.append((key, text))
    return junk

def check_spam_covers(f) -> list:
    """Return list of keys/indices to delete for known spam cover art (by MD5 hash).
    Returns list of frame keys (MP3) or 'FLAC_PIC:{i}' / 'MP4_COVER:{i}' sentinels."""
    import hashlib
    t = type(f).__name__
    junk = []

    if t == "MP3" and f.tags:
        for key in list(f.tags.keys()):
            if key.startswith("APIC"):
                data = f.tags[key].data
                if hashlib.md5(data).hexdigest() in _SPAM_COVER_MD5:
                    junk.append(("MP3_APIC", key, len(data)))

    elif t == "FLAC":
        for i, pic in enumerate(f.pictures):
            if hashlib.md5(pic.data).hexdigest() in _SPAM_COVER_MD5:
                junk.append(("FLAC_PIC", i, len(pic.data)))

    elif t == "MP4" and f.tags:
        covers = f.tags.get("covr", [])
        for i, c in enumerate(covers):
            if hashlib.md5(bytes(c)).hexdigest() in _SPAM_COVER_MD5:
                junk.append(("MP4_COVER", i, len(bytes(c))))

    return junk


def check_flac_junk_tags(f) -> list:
    """Return list of (key, value) for non-standard vorbis comment keys to delete."""
    if type(f).__name__ != "FLAC" or not f.tags:
        return []
    junk = []
    for key in list(f.keys()):
        if key.lower() in _JUNK_FLAC_KEYS:
            junk.append((key, (f[key] or [""])[0]))
    return junk

def strip_watermarks(s: str) -> str:
    return _WATERMARK_RE.sub("", s).strip()

def check_watermarks(tags: dict) -> dict:
    """Return {field: cleaned_value} for any fields containing site watermarks."""
    return {f: strip_watermarks(v) for f, v in tags.items()
            if v and isinstance(v, str) and _WATERMARK_RE.search(v)}

# ── unknown-artist fallback from filename ─────────────────────────────────────

_UNKNOWN_ARTIST = {"unknown artist", "unknown", "неизвестный исполнитель", "неизвестный"}

def artist_from_filename(stem: str) -> str | None:
    """Extract artist from 'Artist - Title' filename stem.
    Returns None if first part is purely numeric or pattern not found."""
    m = _re.match(r"^(.+?)\s+-\s+.+$", stem)
    if not m:
        return None
    candidate = m.group(1).strip()
    if _re.fullmatch(r"\d+", candidate):
        return None
    return candidate

_YEAR_DIR = _re.compile(r"^\d{4}[\s\-\[]")  # dir names like "1984 Love..." or "1984 - ..."

def artist_from_path(path: Path) -> str | None:
    """Walk up the directory tree (skipping the immediate parent as album dir)
    and return the first ancestor that doesn't look like a year-prefixed album dir."""
    parts = list(path.parents)
    # parts[0] = immediate parent (album), start from parts[1]
    for p in parts[1:5]:
        name = p.name
        if not name:
            break
        if _YEAR_DIR.match(name) or _re.fullmatch(r"\d+", name):
            continue
        return name
    return None

# ── multi-artist detection ────────────────────────────────────────────────────

def _extract_feat_from_parens(s: str):
    """Split 'Artist (feat. X)' → main='Artist', feats=['X'].
    Returns (main, feats) or (s, []) if no feat paren found."""
    m = _re.search(r"\(\s*(?:feat\.?|ft\.?)\s*([^)]+)\)", s, _re.IGNORECASE)
    if not m:
        return s, []
    main = s[:m.start()].strip()
    feats = [p.strip() for p in _re.split(r"\s*[,&]\s*", m.group(1)) if p.strip()]
    return main, feats

def _dedup(seq: list) -> list:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _split_and_expand(raw_parts: list) -> list:
    """For each part extract embedded feat-parens, flatten and deduplicate."""
    result = []
    for part in raw_parts:
        main, feats = _extract_feat_from_parens(part)
        result.append(main)
        result.extend(feats)
    return _dedup(result)

def check_artists(tags: dict) -> list:
    """Detect multiple artists using cascade separators (;, /, ,, feat/ft outside parens)."""
    artist = tags.get("artist", "")
    if not artist:
        return []

    # 1. semicolon
    if ";" in artist:
        parts = [p.strip() for p in artist.split(";") if p.strip()]
        if len(parts) > 1:
            return _split_and_expand(parts)

    # 2. slash
    if "/" in artist:
        parts = [p.strip() for p in artist.split("/") if p.strip()]
        if len(parts) > 1:
            return _split_and_expand(parts)

    # 3. comma
    if "," in artist:
        parts = [p.strip() for p in artist.split(",") if p.strip()]
        if len(parts) > 1:
            return _split_and_expand(parts)

    # 4. feat / ft — only when not inside parentheses
    no_parens = _re.sub(r"\([^)]*\)", "", artist)
    m = _re.search(r"\s+(?:feat\.?|ft\.?)\s+", no_parens, _re.IGNORECASE)
    if m:
        sep_pat = _re.compile(r"\s+(?:feat\.?|ft\.?)\s+", _re.IGNORECASE)
        parts = [p.strip() for p in sep_pat.split(no_parens) if p.strip()]
        if len(parts) > 1:
            return parts

    return []


# ── main ──────────────────────────────────────────────────────────────────────

def process_file(path: Path, fix: bool, check_enc: bool, check_art: bool, check_alb: bool):
    issues = []
    applied = []

    try:
        f = MutagenFile(str(path), easy=False)
    except Exception as e:
        # Corrupted MPEG frame but ID3 tags may still be readable
        if path.suffix.lower() == ".mp3" and "sync" in str(e).lower():
            try:
                from mutagen.id3 import ID3
                id3 = ID3(str(path))
                # Wrap in a minimal MP3-like object so the rest of the code works
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

    tags = dict(tags)  # make mutable for downstream updates

    # ── bad chars (replacement character □) ───────────────────────────────────
    bad_fixes = check_bad_chars(tags)
    for field, fixed in bad_fixes.items():
        issues.append(f"bad chars [{field}]: '{tags[field]}' → '{fixed}'")
        if fix:
            set_tag(f, field, fixed)
            applied.append(f"stripped bad chars [{field}]")
        tags[field] = fixed

    # ── watermarks ([muzmo.ru] etc.) ──────────────────────────────────────────
    wm_fixes = check_watermarks(tags)
    for field, fixed in wm_fixes.items():
        issues.append(f"watermark [{field}]: '{tags[field]}' → '{fixed}'")
        if fix:
            set_tag(f, field, fixed)
            applied.append(f"stripped watermark [{field}]")
        tags[field] = fixed

    # ── junk ID3 frames (COMM/USLT/WXXX/TCOP with URL spam) ───────────────────
    for frame_key, frame_text in check_id3_junk_frames(f):
        short = frame_text[:60].replace("\n", " ")
        issues.append(f"junk frame [{frame_key}]: '{short}'")
        if fix:
            del f.tags[frame_key]
            applied.append(f"deleted junk frame [{frame_key}]")

    # ── junk FLAC vorbis keys (non-standard "album artist" with space) ─────────
    for flac_key, flac_val in check_flac_junk_tags(f):
        short = flac_val[:60]
        issues.append(f"junk vorbis key [{flac_key}]: '{short}'")
        if fix:
            # Migrate to standard key if albumartist is missing
            has_standard = bool((f.get("albumartist") or [None])[0])
            if not has_standard and flac_val:
                f["albumartist"] = [flac_val]
                applied.append(f"migrated [{flac_key}] → albumartist='{flac_val}'")
            del f[flac_key]
            applied.append(f"deleted junk vorbis key [{flac_key}]")

    # ── spam cover art (known watermark images by MD5) ────────────────────────
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

    # ── encoding ──────────────────────────────────────────────────────────────
    if check_enc:
        enc_fixes = check_encoding(tags)
        for field, fixed in enc_fixes.items():
            issues.append(f"encoding [{field}]: '{tags[field]}' → '{fixed}'")
            if fix:
                set_tag(f, field, fixed)
                applied.append(f"fixed encoding [{field}]")

    # ── multi-artist ──────────────────────────────────────────────────────────
    if check_art:
        artist_val = tags.get("artist", "")
        # unknown artist → try filename, then path
        if not artist_val or artist_val.strip().lower() in _UNKNOWN_ARTIST:
            guessed = artist_from_path(path) or artist_from_filename(path.stem)
            if guessed:
                source = "path" if artist_from_path(path) else "filename"
                issues.append(f"unknown artist → '{guessed}' (from {source})")
                tags["artist"] = guessed  # update for downstream checks (album detection)
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

        # ── title with artist prefix ("Artist - Title" in title tag) ──────────
        # Runs after artist is resolved (guessed from path if was unknown)
        clean_title = check_title_artist_prefix(tags, path.stem)
        if clean_title:
            current_title = tags.get("title") or path.stem
            issues.append(f"title: '{current_title}' → '{clean_title}'")
            if fix:
                set_tag(f, "title", clean_title)
                applied.append(f"set title to '{clean_title}'")
            tags["title"] = clean_title

    # ── album + albumartist forced from path ──────────────────────────────────
    if check_alb:
        excluded_dir = next((p for p in path.parts if p in EXCLUDE_DIRS), None)
        if excluded_dir:
            # Files in flat playlist dirs: set album = the playlist dir name
            # so they don't bleed into real albums with the same name
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

            # Force artist from folder name, but keep "Artist feat. X" values intact
            # Only force when artist doesn't already start with the correct artist name
            current_artist = tags.get("artist", "")
            if (current_artist != correct_albumartist
                    and not current_artist.lower().startswith(correct_albumartist.lower())):
                issues.append(f"artist: '{current_artist or '<empty>'}' → '{correct_albumartist}'")
                if fix:
                    set_tag(f, "artist", correct_albumartist)
                    applied.append(f"set artist to '{correct_albumartist}'")

    # ── filename watermark ────────────────────────────────────────────────────
    new_stem = strip_watermarks(path.stem)
    new_path = path.with_name(new_stem + path.suffix)
    if new_stem != path.stem:
        issues.append(f"filename watermark: '{path.name}' → '{new_path.name}'")

    # ── output ────────────────────────────────────────────────────────────────
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

# ── album directory name cleanup ──────────────────────────────────────────────

_YEAR_DIR_PREFIX_RE = _re.compile(r'^\d{4}(\s*\[\d{4}\])?\s*[-\.]\s*')

def clean_album_dirname(name: str) -> str:
    """Strip year prefix and all trailing parenthetical groups from album directory names.

    '2005 - Перевал'                                                         → 'Перевал'
    '2003 [2013] - Дорога сна'                                               → 'Дорога сна'
    '2022 - Бордерлайн (deluxe edition)'                                     → 'Бордерлайн'
    '1988 - Князь Тишины (2013, Bomba Music, Germany)'                       → 'Князь Тишины'
    '1995 - Крылья (2013, Bomba Music) - 2LP'                                → 'Крылья'
    '1999 - Серебряный Век (Лучшие Песни 1991-1997) (Компиляция, Dana, RUS)' → 'Серебряный Век'
    '2004. Черновики (Александр Васильев)'                                   → 'Черновики'
    '1994. Пыльная быль (2002)'                                              → 'Пыльная быль'
    '2004 - 2013'                                                            → '2004 - 2013' (unchanged)
    """
    s = _YEAR_DIR_PREFIX_RE.sub('', name)

    # If the whole name was a year-range (e.g. "2004 - 2013"), leave it unchanged
    if not s.strip() or _re.fullmatch(r'\d{4}(\s*[-–]\s*\d{4})?', s.strip()):
        return name

    # Strip trailing disc suffix first so it doesn't block paren removal
    # e.g. "Яблокитай (2022 RM, ...) - 2CD" → "Яблокитай (2022 RM, ...)"
    s = _re.sub(r'\s*-\s*\d+[xX]?(LP|CD|EP)\s*$', '', s, flags=_re.IGNORECASE)

    # Strip ALL trailing paren/bracket groups
    changed = True
    while changed:
        changed = False
        m = _re.search(r'\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$', s)
        if m:
            s = s[:m.start()].rstrip()
            changed = True

    s = s.strip()
    return s if s else name


def scan_dirs(root: Path, fix: bool) -> int:
    """Report (and optionally rename) album directories with year prefixes or release junk.
    Processes deepest directories first to avoid path invalidation."""
    renames = []
    for dirpath, _, _ in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root or is_excluded(p):
            continue
        clean = clean_album_dirname(p.name)
        if clean != p.name and clean:
            renames.append((p, p.parent / clean))

    for old, new in renames:
        print(f"\n  dir: {old.relative_to(root)}")
        print(f"      [!] '{old.name}' → '{new.name}'")
        if fix:
            if new.exists():
                print(f"      [ERROR] target already exists, skipping")
            else:
                old.rename(new)
                print(f"      [✓] renamed")

    return len(renames)


def scan(root: Path, fix: bool, check_enc: bool, check_art: bool, check_alb: bool):
    found = 0

    if check_alb:
        dir_count = scan_dirs(root, fix)
        if dir_count:
            print(f"\n{'─'*60}")
            print(f"  Directories: {dir_count} {'renamed' if fix else 'to rename'}")
            print(f"{'─'*60}\n")

    for dirpath, _, filenames in os.walk(root):
        for fname in sorted(filenames):
            ext = Path(fname).suffix.lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            found += 1
            process_file(Path(dirpath) / fname, fix, check_enc, check_art, check_alb)

    print(f"\n{'─'*60}")
    print(f"  Scanned: {found} files")
    print(f"  Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")

def main():
    parser = argparse.ArgumentParser(description="Music metadata checker/fixer")
    parser.add_argument("path", help="Directory or file to scan")
    parser.add_argument("--fix",           action="store_true", help="Apply fixes (default: dry-run)")
    parser.add_argument("--encoding-only", action="store_true", help="Only check encoding")
    parser.add_argument("--artists-only",  action="store_true", help="Only check multi-artist tags")
    parser.add_argument("--album-only",    action="store_true", help="Only check missing album tags")
    args = parser.parse_args()

    run_all   = not (args.encoding_only or args.artists_only or args.album_only)
    check_enc = run_all or args.encoding_only
    check_art = run_all or args.artists_only
    check_alb = run_all or args.album_only

    root = Path(args.path)
    if not root.exists():
        print(f"ERROR: path not found: {root}")
        sys.exit(1)

    if root.is_file():
        process_file(root, args.fix, check_enc, check_art, check_alb)
    else:
        print(f"Scanning: {root}")
        print(f"Checks: {'encoding ' if check_enc else ''}{'artists ' if check_art else ''}{'album' if check_alb else ''}")
        print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}\n{'─'*60}")
        scan(root, args.fix, check_enc, check_art, check_alb)

if __name__ == "__main__":
    main()