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
import json
import time
import argparse
import subprocess
import urllib.request
import urllib.parse
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
_JUNK_FLAC_KEYS = {"album artist", "album_artist", "discnumber", "disc"}

# TXXX frame descriptions injected by yt-dlp — contain YouTube-specific data
_JUNK_TXXX_YOUTUBE: set[str] = {
    "purl",         # podcast/playlist URL
    "comment",      # YouTube video URL
    "synopsis",     # YouTube video description
    "description",  # YouTube video description
    "episode_id",
    "episode_sort",
    "season_number",
}

# FLAC vorbis comment keys injected by yt-dlp / container extraction
_JUNK_FLAC_YOUTUBE: set[str] = {
    "comment", "description", "synopsis", "purl",
    "episode_id", "episode_sort", "season_number",
    # mp4 container metadata that leaks into vorbis comments via ffmpeg
    "major_brand", "minor_version", "compatible_brands",
}

# YouTube content categories that appear in TCON/genre — not real music genres
_BONUS_TRACK_RE = _re.compile(
    r'\b(acoustic|live|remix|edit|radio.?edit|instrumental|karaoke|cover|'
    r'reprise|interlude|skit|demo|dj\s|bonus|version|remaster)\b',
    _re.IGNORECASE,
)

_JUNK_GENRE_RE = _re.compile(
    r'^(people\s*&\s*blogs?|film\s*&\s*animation|gaming|howto\s*&\s*style|'
    r'news\s*&\s*politics|nonprofits?\s*&\s*activism|science\s*&\s*technology|'
    r'sports|travel\s*&\s*events|education|entertainment|autos?\s*&\s*vehicles?)$',
    _re.IGNORECASE,
)

# ── variant track detection ───────────────────────────────────────────────────

_VARIANT_SUFFIX_RE = _re.compile(
    r'\s*[\(\[](instrumental|revisited|remix|remixed|version|edit|live|acoustic|'
    r'demo|extended|radio edit|reprise|interlude|intro|outro|feat\.|ft\.|'
    r'\d{4})\b.*?[\)\]]',
    _re.IGNORECASE
)
_VARIANT_DASH_RE = _re.compile(
    r'\s+-\s+(Instrumental|Interlude\s*\d*|Intro|Outro)\s*$',
    _re.IGNORECASE
)
# Stricter: no year-only matches — for standalone detection where no original exists
_STANDALONE_VARIANT_RE = _re.compile(
    r'\s*[\(\[](instrumental|revisited|remix|remixed|interlude|intro|outro)\b.*?[\)\]]'
    r'|\s+-\s+(Instrumental|Interlude\s*\d*|Intro|Outro)\s*$',
    _re.IGNORECASE
)
# Variants that alter content — fmt-swap must NOT be applied for these
_CONTENT_ALTERING_RE = _re.compile(
    r'\b(instrumental|live|acoustic|demo|remix|remixed|revisited|reprise|interlude|intro|outro)\b',
    _re.IGNORECASE
)
_REMASTER_RE = _re.compile(r'\b(remaster(?:ed)?)\b', _re.IGNORECASE)
_FORMAT_PRIORITY = {'.flac': 0, '.wav': 1, '.ape': 2, '.m4a': 3, '.mp3': 4,
                    '.aac': 5, '.ogg': 6, '.opus': 7, '.wma': 8}

def _variant_base(stem: str) -> str:
    if ' - ' in stem:
        stem = stem.split(' - ', 1)[1]
    stem = _VARIANT_SUFFIX_RE.sub('', stem)
    stem = _VARIANT_DASH_RE.sub('', stem)
    return stem.strip().lower()

def _is_variant_stem(stem: str) -> bool:
    check = stem.split(' - ', 1)[1] if ' - ' in stem else stem
    return bool(_VARIANT_SUFFIX_RE.search(check) or _VARIANT_DASH_RE.search(check))

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
        # TXXX frames whose description shadows a standard tag or is yt-dlp junk
        if frame_type == "TXXX":
            desc = key[5:].lower() if key.startswith("TXXX:") else ""
            if desc in _JUNK_TXXX_DESCS or desc in _JUNK_TXXX_YOUTUBE:
                junk.append((key, _frame_text(f.tags[key])))
            continue
        if frame_type == "COMM":
            text = _frame_text(f.tags[key])
            if _is_spam_comment(text):
                junk.append((key, text))
            continue
        # PRIV frames from Windows Media Player — cause album-split bugs in Navidrome
        # (WMCollectionID / WMCollectionGroupID are GUIDs used for album grouping)
        if frame_type == "PRIV":
            owner = key[5:] if key.startswith("PRIV:") else ""
            if owner.startswith("WM/") or owner in ("AverageLevel", "PeakValue"):
                junk.append((key, f"WMP private frame [{owner}]"))
            continue
        # TCON (genre) containing a YouTube content category, not a music genre
        if frame_type == "TCON":
            text = _frame_text(f.tags[key]).strip()
            if _JUNK_GENRE_RE.match(text):
                junk.append((key, text))
            continue
        # USLT (unsynchronized lyrics) — always yt-dlp artefact; audio may be wrong
        if frame_type == "USLT":
            junk.append((key, _frame_text(f.tags[key])[:80]))
            continue
        # TPOS (disc/side number) — causes album splits in Navidrome, remove unconditionally
        if frame_type == "TPOS":
            junk.append((key, _frame_text(f.tags[key])))
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


def check_flac_junk_tags(f) -> list[tuple[str, str]]:
    """Return list of (key, value) for non-standard / yt-dlp junk vorbis comment keys."""
    if type(f).__name__ != "FLAC" or not f.tags:
        return []
    junk: list[tuple[str, str]] = []
    for key in list(f.keys()):
        kl = key.lower()
        val = (f[key] or [""])[0]
        if kl in _JUNK_FLAC_KEYS or kl in _JUNK_FLAC_YOUTUBE:
            junk.append((key, val))
            continue
        # 'year' is redundant when 'date' is present, and often contains a full
        # ISO timestamp (from yt-dlp) that causes Navidrome to split the album
        if kl == "year" and (f.get("date") or [None])[0]:
            junk.append((key, val))
            continue
        # genre containing a YouTube content category
        if kl == "genre" and _JUNK_GENRE_RE.match(val.strip()):
            junk.append((key, val))
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
        if frame_key.startswith("USLT"):
            issues.append("[?] verify audio content — embedded lyrics suggest wrong yt-dlp download")
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

    # ── date tag normalization: TDRL/TYER → TDRC (MP3 only) ──────────────────
    # Navidrome uses TDRC for album year in album_id computation.
    # TDRL (release date) or TYER (old year tag) won't be picked up → album split.
    if type(f).__name__ == "MP3" and f.tags is not None:
        tdrc = f.tags.get("TDRC")
        tdrl = f.tags.get("TDRL")
        tyer = f.tags.get("TYER")
        year_src = tdrl or tyer
        if not tdrc and year_src:
            year_val = str(_frame_text(year_src))[:4]
            issues.append(f"date: missing TDRC, copying from {'TDRL' if tdrl else 'TYER'}: '{year_val}'")
            if fix:
                from mutagen.id3 import TDRC as _TDRC
                f.tags.add(_TDRC(encoding=0, text=[year_val]))
                if tdrl:
                    del f.tags["TDRL"]
                applied.append(f"set TDRC='{year_val}' (removed {'TDRL' if tdrl else 'TYER'})")

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
                    and not current_artist.startswith(correct_albumartist)):
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

_META_PAREN_KEYWORDS = _re.compile(
    r'\b(deluxe|explicit|clean|remaster(?:ed)?|'
    r'(?:standard|bonus|special|limited|anniversary|collector.?s)\s+(?:edition|tracks?)|'
    r'bonus\s+tracks?|компиляция|сборник)\b',
    _re.IGNORECASE
)

def _is_meta_paren(content: str) -> bool:
    """Return True if paren content is release metadata that should be stripped."""
    c = content.strip()
    if _META_PAREN_KEYWORDS.search(c):
        return True
    if _re.match(r'^\d{4}\s*,', c):   # "2013, Bomba Music, Germany"
        return True
    if _re.fullmatch(r'\d{4}(\s+\w+)*', c):  # "2015" or "2015 Digital Remaster"
        return True
    return False

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
    # Strip "- Single" / "- EP" suffix (Last.fm appends these to single/EP release names)
    s = _re.sub(r'\s*-\s*(Single|EP)\s*$', '', s, flags=_re.IGNORECASE)

    # Strip trailing paren/bracket groups that are release metadata only.
    # Preserves meaningful content like (Chapter 1), (THE MIXTAPE), (Vol. 2).
    changed = True
    while changed:
        m = _re.search(r'\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$', s)
        if m and _is_meta_paren(m.group(1)):
            s = s[:m.start()]
            changed = True
        else:
            changed = False

    # Strip trailing punctuation left after paren removal (e.g. "A Life By Design-")
    s = _re.sub(r'[\s\-.,]+$', '', s)
    return s if s else name


def _extract_year(f) -> str | None:
    """Return 4-digit year string from a mutagen file object, or None."""
    t = type(f).__name__
    candidates = []
    if t == "MP3" and f.tags:
        for frame_name in ("TDRC", "TDRL", "TYER"):
            frame = f.tags.get(frame_name)
            if frame:
                candidates.append(str(frame)[:4])
    elif t == "FLAC":
        d = (f.get("date") or [""])[0]
        if d:
            candidates.append(d[:4])
    elif t == "MP4" and f.tags:
        d = str((f.tags.get("\xa9day") or [""])[0])
        if d:
            candidates.append(d[:4])
    else:
        if f.tags:
            d = (f.tags.get("date") or f.tags.get("year") or [""])[0] if hasattr(f.tags, "get") else ""
            if d:
                candidates.append(str(d)[:4])
    for c in candidates:
        if c.isdigit() and 1900 <= int(c) <= 2100:
            return c
    return None

def _set_year(f, year: str):
    """Write year tag to a mutagen file object (does NOT call f.save())."""
    t = type(f).__name__
    if t == "MP3":
        from mutagen.id3 import TDRC as _TDRC
        if f.tags is None:
            f.add_tags()
        f.tags.add(_TDRC(encoding=0, text=[year]))
    elif t == "FLAC":
        f["date"] = [year]
        if "year" in f:   # remove redundant/conflicting non-standard 'year' key
            del f["year"]
    elif t == "MP4":
        if f.tags is None:
            f.add_tags()
        f.tags["\xa9day"] = [year]
    else:
        if f.tags is None:
            f.add_tags()
        f.tags["date"] = [year]

def _raw_date(f) -> str:
    """Return the raw date string stored in the tag (before year extraction).

    For FLAC, both 'date' and 'year' vorbis keys are checked — 'year' may contain
    a full ISO timestamp when 'date' has already been normalised to YYYY.
    """
    t = type(f).__name__
    if t == "MP3" and f.tags:
        for frame_name in ("TDRC", "TDRL", "TYER"):
            frame = f.tags.get(frame_name)
            if frame:
                return str(frame)
    elif t == "FLAC":
        date_val = (f.get("date") or [""])[0]
        year_val = (f.get("year") or [""])[0]
        # Return the longer one — full ISO timestamp is more problematic than bare YYYY
        return year_val if len(year_val) > len(date_val) else date_val
    elif t == "MP4" and f.tags:
        return str((f.tags.get("\xa9day") or [""])[0])
    return ""


def scan_album_years(root: Path, fix: bool) -> int:
    """Detect year-tag mismatches within album folders that cause Navidrome album splits.

    Two failure modes are handled:
    1. Different years across files (e.g. 2003 vs 2009) — normalise to earliest.
    2. Same year but different raw strings (e.g. '2023' vs '2023-03-28T00:00:00+03:00')
       — normalise all to bare 'YYYY' so Navidrome sees one album.
    """
    albums_fixed: int = 0

    for dirpath, _, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            continue

        audio_paths: list[Path] = [
            p / fn for fn in filenames
            if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if len(audio_paths) < 2:
            continue

        # (path, extracted_year, raw_date_string, mutagen_obj)
        entries: list[tuple[Path, str | None, str, object]] = []
        for fpath in audio_paths:
            try:
                f = MutagenFile(str(fpath), easy=False)
                if f is None:
                    continue
                year = _extract_year(f)
                raw  = _raw_date(f)
                entries.append((fpath, year, raw, f))
            except Exception:
                pass

        years: set[str] = {y for _, y, _, _ in entries if y}
        if not years:
            continue

        # Canonical = earliest valid year
        canonical: str = min(years, key=int)

        # Check both failure modes:
        # mode 1 — multiple different years
        year_conflict = len(years) > 1
        # mode 2 — raw date string doesn't equal bare year (e.g. full ISO timestamp)
        raw_conflict  = any(
            raw and raw != canonical
            for _, year, raw, _ in entries
            if year == canonical  # only check files already at the canonical year
        )

        if not year_conflict and not raw_conflict:
            continue

        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p

        print(f"\n  {rel}")
        if year_conflict:
            print(f"      [!] year split: {sorted(years)} → '{canonical}'")
        else:
            print(f"      [!] date format mismatch → normalising to '{canonical}'")

        for fpath, year, raw, f in entries:
            needs_fix = (year != canonical) or (raw and raw != canonical)
            if not needs_fix:
                continue
            print(f"      [!] {fpath.name}: '{raw or year or '?'}' → '{canonical}'")
            if fix:
                try:
                    _set_year(f, canonical)
                    f.save()
                    print(f"      [✓] fixed")
                except Exception as e:
                    print(f"      [ERROR] {e}")

        albums_fixed += 1

    return albums_fixed


# ── track number helpers ──────────────────────────────────────────────────────

def _title_slug(stem: str) -> str:
    """Normalise a filename stem to a comparable slug for Last.fm matching."""
    s = stem
    if ' - ' in s:
        s = s.split(' - ', 1)[1]
    s = _re.sub(r'^\d+[\s.\-]+', '', s)   # strip leading track number
    return _re.sub(r'[^\w]', '', s.lower())

def _get_tracknum(f) -> int | None:
    """Return current track number (int) or None."""
    t = type(f).__name__
    if t == "MP3" and f.tags:
        frame = f.tags.get("TRCK")
        if frame:
            v = str(frame).split("/")[0].strip()
            return int(v) if v.isdigit() else None
    elif t == "FLAC":
        v = (f.get("tracknumber") or [""])[0]
        if v:
            v = str(v).split("/")[0].strip()
            return int(v) if v.isdigit() else None
    elif t == "MP4" and f.tags:
        tn = f.tags.get("trkn")
        if tn:
            return tn[0][0] if isinstance(tn[0], tuple) else int(tn[0])
    else:
        if f.tags:
            v = (f.tags.get("tracknumber") or [""])[0] if hasattr(f.tags, "get") else None
            if v:
                return int(str(v).split("/")[0]) if str(v).split("/")[0].isdigit() else None
    return None

def _set_tracknum(f, num: int):
    """Write track number tag (does NOT call f.save())."""
    t = type(f).__name__
    if t == "MP3":
        from mutagen.id3 import TRCK as _TRCK
        if f.tags is None:
            f.add_tags()
        f.tags["TRCK"] = _TRCK(encoding=3, text=[str(num)])
    elif t == "FLAC":
        f["tracknumber"] = [str(num)]
    elif t == "MP4":
        if f.tags is None:
            f.add_tags()
        f.tags["trkn"] = [(num, 0)]
    else:
        if f.tags is None:
            f.add_tags()
        f.tags["tracknumber"] = [str(num)]

def _lastfm_track_name(artist: str, track: str, api_key: str) -> str | None:
    """Return the corrected track name from Last.fm (autocorrect), or None on failure."""
    params = urllib.parse.urlencode({
        "method": "track.getInfo",
        "api_key": api_key,
        "artist": artist,
        "track": track,
        "autocorrect": "1",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{params}", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode())
        return data.get("track", {}).get("name") or None
    except Exception:
        return None


def _lastfm_tracklist(artist: str, album: str, api_key: str) -> dict[str, tuple[int, str]]:
    """Return {title_slug: (rank, original_name)} from Last.fm, or {} on failure."""
    params = urllib.parse.urlencode({
        "method": "album.getInfo",
        "api_key": api_key,
        "artist": artist,
        "album": album,
        "autocorrect": "1",
        "format": "json",
    })
    url = f"https://ws.audioscrobbler.com/2.0/?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return {}
    tracks = data.get("album", {}).get("tracks", {}).get("track", [])
    if not tracks:
        return {}
    if isinstance(tracks, dict):
        tracks = [tracks]
    result = {}
    for tr in tracks:
        name = tr.get("name", "")
        rank = tr.get("@attr", {}).get("rank")
        if name and rank:
            result[_re.sub(r'[^\w]', '', name.lower())] = (int(rank), name)
    return result

def _match_to_tracklist(slug: str, tracklist: dict[str, tuple[int, str]]) -> tuple[int, str] | None:
    """Return (rank, name) for the best match in tracklist, or None."""
    entry = tracklist.get(slug)
    if entry:
        return entry
    # Partial slug match (handles minor title differences)
    for lfm_slug, (rank, name) in tracklist.items():
        if lfm_slug and slug and (lfm_slug in slug or slug in lfm_slug):
            return (rank, name)
    return None

def _lastfm_artist_albums(artist: str, api_key: str) -> list[str]:
    params = urllib.parse.urlencode({
        "method": "artist.getTopAlbums",
        "artist": artist,
        "limit": "100",
        "autocorrect": "1",
        "api_key": api_key,
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{params}", timeout=10
        ) as resp:
            data = json.loads(resp.read())
        albums = data.get("topalbums", {}).get("album", [])
        return [a["name"] for a in albums if a["name"] not in ("[unknown]", "")]
    except Exception as e:
        print(f"  [Last.fm error] {e}")
        return []


def scan_track_numbers(root: Path, fix: bool, lastfm_key: str) -> int:
    """Set track number tags from Last.fm. Falls back to 1 when no data."""
    albums_done: int = 0
    to_download: list = []  # (artist, track_name, out_dir)

    for dirpath, _, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) != 2:   # only root/artist/album level
            continue

        audio_paths: list[Path] = sorted(
            p / fn for fn in filenames if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        )
        if not audio_paths:
            continue

        if (p / '.skip').exists():
            print(f'  [SKIP] {rel} (.skip marker)')
            continue

        # (path, mutagen_obj, existing_track_number)
        file_data: list[tuple[Path, object, int | None]] = []
        for fpath in audio_paths:
            try:
                f = MutagenFile(str(fpath), easy=False)
                if f is None:
                    continue
                file_data.append((fpath, f, _get_tracknum(f)))
            except Exception:
                pass
        if not file_data:
            continue

        existing_nums: list[int | None] = [trck for _, _, trck in file_data]
        valid_set: set[int] = {n for n in existing_nums if n and n > 0}
        all_valid: bool = (
            None not in existing_nums and
            len(valid_set) == len(file_data) and
            min(valid_set) == 1 and
            max(valid_set) == len(file_data)
        )
        if all_valid:
            continue

        artist = p.parent.name
        album  = clean_album_dirname(p.name)

        tracklist = _lastfm_tracklist(artist, album, lastfm_key)
        time.sleep(0.25)

        if not tracklist:
            # Assign sequential numbers 1..n sorted by filename so all_valid passes next run
            n = len(file_data)
            seq = [(fpath, f, i, trck) for i, (fpath, f, trck) in enumerate(file_data, 1)]
            needs_change = [(fpath, f, rank, trck) for fpath, f, rank, trck in seq if rank != trck]
            if not needs_change:
                continue
            albums_done += 1
            print(f"\n  {rel}")
            print(f"      [no Last.fm data] → sequential 1–{n}")
            for fpath, f, rank, trck in needs_change:
                print(f"      [!] {fpath.name}: {trck or '?'} → {rank}")
                if fix:
                    try:
                        _set_tracknum(f, rank)
                        f.save()
                    except Exception as e:
                        print(f"      [ERROR] {e}")
            continue

        # Match every file to a Last.fm track
        # assignments: (fpath, f, assigned_rank, existing_trck)
        assignments = []
        for fpath, f, existing_trck in file_data:
            # Prefer tag title for slug (handles transliterated filenames)
            t = type(f).__name__
            if t == "MP3" and f.tags:
                tag_title = _frame_text(f.tags.get("TIT2") or "")
            elif t == "FLAC":
                tag_title = (f.get("title") or [""])[0]
            elif t == "MP4" and f.tags:
                tag_title = str((f.tags.get("\xa9nam") or [""])[0])
            else:
                tag_title = ""
            slug = _title_slug(tag_title) if tag_title else _title_slug(fpath.stem)
            match = _match_to_tracklist(slug, tracklist)
            # Fallback: try stem slug if tag didn't match
            if match is None and tag_title:
                match = _match_to_tracklist(_title_slug(fpath.stem), tracklist)
            rank  = match[0] if match else None
            assignments.append((fpath, f, rank, existing_trck))

        # Covered ranks = all Last.fm ranks that have a matching file
        covered   = {rank for _, _, rank, _ in assignments if rank is not None}
        lfm_ranks = {rank for rank, _ in tracklist.values()}
        missing   = lfm_ranks - covered   # in Last.fm but no file found

        # Rank-to-name map for warnings
        rank_to_name = {rank: name for _, (rank, name) in tracklist.items()}

        # Decide: use Last.fm numbers directly, OR renumber sequentially
        # Renumber when tracks are missing so there are no gaps (e.g. 2,4,5 → 1,2,3)
        if missing:
            # Sort matched files by Last.fm rank; unmatched go at the end
            sorted_asgn = sorted(assignments, key=lambda x: (x[2] is None, x[2] or 9999))
            final = [(fpath, f, new_rank, existing_trck)
                     for new_rank, (fpath, f, _, existing_trck)
                     in enumerate(sorted_asgn, 1)]
        else:
            # All Last.fm tracks have a file — use exact Last.fm numbers
            # Unmatched files (not in Last.fm): keep existing number to avoid false changes
            final = [(fpath, f, rank if rank is not None else existing_trck, existing_trck)
                     for fpath, f, rank, existing_trck in assignments]

        changes = [(fpath, f, rank, trck) for fpath, f, rank, trck in final if rank != trck]

        # Only print album if there's something to report
        if not missing and not changes:
            continue

        albums_done += 1
        print(f"\n  {rel}")

        real_missing = 0
        if missing:
            for rank in sorted(missing):
                name = rank_to_name.get(rank, '?')
                if _BONUS_TRACK_RE.search(name):
                    print(f"      [MISSING/BONUS] track {rank}: '{name}' (skipped — acoustic/remix/bonus)")
                else:
                    real_missing += 1
                    print(f"      [MISSING] track {rank}: '{name}'")
                    if fix:
                        to_download.append((artist, name, str(p)))
            print(f"      [renumber] {len(missing)} track(s) missing → renumbering 1–{len(final)}")

        for fpath, f, rank, existing_trck in changes:
            print(f"      [!] {fpath.name}: {existing_trck or '?'} → {rank}")
            if fix and real_missing == 0:
                try:
                    _set_tracknum(f, rank)
                    f.save()
                except Exception as e:
                    print(f"      [ERROR] {e}")
        if fix and real_missing > 0 and changes:
            print("      [SKIP renumber] downloading missing tracks first — re-run to apply")


    if fix and to_download:
        print(f"\n[DOWNLOAD] Downloading {len(to_download)} missing track(s)...")
        for dl_artist, dl_name, dl_out in to_download:
            print(f"  → {dl_artist} — {dl_name}")
            sys.stdout.flush()
            result = subprocess.run(
                ["python3", "/app/download_music.py",
                 "--track", dl_artist, dl_name,
                 "--out", dl_out,
                 "--lastfm-key", lastfm_key],
            )
            if result.returncode != 0:
                print(f"  [ERROR] download failed for '{dl_name}' (exit {result.returncode})")
        print("[DOWNLOAD] Done.")
    return albums_done


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
            if not new.exists():
                old.rename(new)
                print(f"      [✓] renamed")
            else:
                # Target exists — merge files and subdirs from old into new
                merged: int = 0
                skipped: int = 0
                for src in sorted(old.iterdir()):
                    dst = new / src.name
                    if src.is_dir():
                        if not dst.exists():
                            src.rename(dst)
                            print(f"      [merge] moved subdir '{src.name}'")
                            merged += 1
                        else:
                            # Subdir exists in target — move its contents recursively
                            sub_merged = 0
                            for sub_src in sorted(src.iterdir()):
                                sub_dst = dst / sub_src.name
                                if not sub_dst.exists():
                                    sub_src.rename(sub_dst)
                                    sub_merged += 1
                                else:
                                    sub_src.unlink() if sub_src.is_file() else None
                            if not any(src.iterdir()):
                                src.rmdir()
                            print(f"      [merge] merged subdir '{src.name}' ({sub_merged} item(s))")
                            merged += sub_merged
                    else:
                        if not dst.exists():
                            src.rename(dst)
                            merged += 1
                        else:
                            src_pri = _FORMAT_PRIORITY.get(src.suffix.lower(), 99)
                            dst_pri = _FORMAT_PRIORITY.get(dst.suffix.lower(), 99)
                            if src_pri < dst_pri:
                                dst.unlink()
                                src.rename(dst)
                                print(f"      [merge] replaced '{dst.name}' with better format")
                                merged += 1
                            else:
                                src.unlink()
                                print(f"      [merge] dropped '{src.name}' (worse/equal format)")
                                skipped += 1
                remaining = list(old.iterdir())
                if not remaining:
                    old.rmdir()
                    print(f"      [✓] merged {merged} file(s) into '{new.name}'" +
                          (f", dropped {skipped}" if skipped else ""))
                else:
                    print(f"      [!] '{old.name}' still has {len(remaining)} item(s) after merge")

    return len(renames)


def scan_variants(root: Path, fix: bool):
    """Report (and optionally delete) variant tracks (Instrumental, Revisited, etc.)."""
    from collections import defaultdict

    albums_found = 0
    files_deleted = 0

    for dirpath, dirnames, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            dirnames.clear()
            continue

        audio_files = [f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS]
        if not audio_files:
            continue

        groups = defaultdict(list)
        for fname in audio_files:
            bn = _variant_base(Path(fname).stem)
            if bn:
                groups[bn].append(fname)

        album_issues = []  # list of (label, to_delete, to_keep)

        for base, group in sorted(groups.items()):
            if len(group) == 1:
                continue
            variants  = [f for f in group if _is_variant_stem(Path(f).stem)]
            originals = [f for f in group if not _is_variant_stem(Path(f).stem)]

            if variants and originals:
                best_var_fmt  = min(_FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99) for f in variants)
                best_orig_fmt = min(_FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99) for f in originals)
                def _check(f, regex):
                    s = Path(f).stem
                    return regex.search(s.split(' - ', 1)[1] if ' - ' in s else s)
                content_altering = [f for f in variants if _check(f, _CONTENT_ALTERING_RE)]
                is_remaster      = any(_check(f, _REMASTER_RE) for f in variants)
                # Prefer variant when: no content alteration AND (better format OR explicit remaster)
                if not content_altering and (best_var_fmt < best_orig_fmt or is_remaster):
                    album_issues.append((f"variant '{base}' [prefer remaster]", originals, variants))
                else:
                    album_issues.append((f"variant '{base}'", variants, originals))
            elif len(originals) > 1:
                from collections import defaultdict as _dd
                handled = set()

                # Pass 1: full lowercase stem — catches capitalization dups and format dups
                by_lower = _dd(list)
                for f in originals:
                    by_lower[Path(f).stem.lower()].append(f)
                for lower_stem, stem_files in by_lower.items():
                    if len(stem_files) <= 1:
                        continue
                    handled.update(stem_files)
                    exts = [Path(f).suffix.lower() for f in stem_files]
                    if len(set(exts)) > 1:
                        ordered = sorted(stem_files,
                            key=lambda f: _FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99))
                        album_issues.append((f"format dup '{lower_stem}'", ordered[1:], ordered[:1]))
                    else:
                        ordered = sorted(stem_files)
                        album_issues.append((f"case dup '{lower_stem}'", ordered[1:], ordered[:1]))

                # Pass 2: track part only (after " - ") — catches "Artist feat X - Track"
                # vs "Artist - Track" where artist prefix differs but track name is same
                by_track = _dd(list)
                for f in originals:
                    if f in handled:
                        continue
                    stem = Path(f).stem.lower()
                    track = stem.split(' - ', 1)[1] if ' - ' in stem else stem
                    by_track[track].append(f)
                for track, track_files in by_track.items():
                    if len(track_files) <= 1:
                        continue
                    # Only flag when one artist name is a prefix of another
                    # (e.g. "Metox" ⊂ "Metox feat Horus") — skips genuinely different artists
                    artists = [Path(f).stem.lower().split(' - ', 1)[0].strip()
                               if ' - ' in Path(f).stem else '' for f in track_files]
                    has_prefix = any(
                        a2.startswith(a1) and a1 != a2
                        for i, a1 in enumerate(artists)
                        for j, a2 in enumerate(artists) if i != j
                    )
                    if not has_prefix:
                        continue
                    exts = [Path(f).suffix.lower() for f in track_files]
                    if len(set(exts)) > 1:
                        ordered = sorted(track_files,
                            key=lambda f: _FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99))
                        album_issues.append((f"format dup '{track}'", ordered[1:], ordered[:1]))
                    else:
                        # Keep longest stem — more complete artist info (feat. version)
                        ordered = sorted(track_files, key=lambda f: -len(Path(f).stem))
                        album_issues.append((f"artist dup '{track}'", ordered[1:], ordered[:1]))


        if not album_issues:
            continue

        albums_found += 1
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        print(f"\n  {rel}")

        for label, to_delete, to_keep in album_issues:
            is_remaster_keep = 'prefer remaster' in label
            print(f"      [{label}]")
            for f in to_keep:
                if is_remaster_keep:
                    stem = Path(f).stem
                    clean = _VARIANT_SUFFIX_RE.sub('', stem)
                    clean = _VARIANT_DASH_RE.sub('', clean).strip()
                    if clean != stem:
                        print(f"        [keep→rename] {f} → {clean + Path(f).suffix}")
                    else:
                        print(f"        [keep] {f}")
                else:
                    print(f"        [keep] {f}")
            for f in to_delete:
                print(f"        [!]    {f}")
                if fix:
                    try:
                        (p / f).unlink()
                        print(f"        [✓]    deleted")
                        files_deleted += 1
                    except Exception as e:
                        print(f"        [ERROR] {e}")
            if fix and is_remaster_keep:
                for f in to_keep:
                    old_path = p / f
                    stem = Path(f).stem
                    clean = _VARIANT_SUFFIX_RE.sub('', stem)
                    clean = _VARIANT_DASH_RE.sub('', clean).strip()
                    if clean == stem:
                        continue
                    new_path = p / (clean + Path(f).suffix)
                    if new_path.exists():
                        print(f"        [SKIP] rename: '{new_path.name}' already exists")
                    else:
                        try:
                            old_path.rename(new_path)
                            print(f"        [✓]    renamed → '{new_path.name}'")
                        except Exception as e:
                            print(f"        [ERROR] rename: {e}")

    print(f"\n{'─'*60}")
    print(f"  Albums with variants: {albums_found}")
    if fix:
        print(f"  Files deleted: {files_deleted}")
    print(f"  Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")


def _safe_dirname(name: str) -> str:
    """Strip characters that are invalid in directory names."""
    return _re.sub(r'[<>:"/\\|?*]', '', name).strip(' .')


def _dup_score(fp: Path, artist_dir: str) -> tuple:
    """Lower score = better file to keep.
    Priority: format > bitrate > standard naming > file size."""
    fmt = _FORMAT_PRIORITY.get(fp.suffix.lower(), 99)
    f = MutagenFile(str(fp), easy=False)
    bitrate = 0
    if f and hasattr(f, "info"):
        bitrate = getattr(f.info, "bitrate", 0) or 0
    stem = fp.stem
    import re as _re2
    has_dup_suffix = bool(_re2.search(r'[_(]\d+\)?$', stem))
    non_standard   = 0 if (" - " in stem and not has_dup_suffix) else 1
    return (fmt, -bitrate, non_standard, -fp.stat().st_size)


def scan_duplicates(root: Path, fix: bool) -> int:
    """Detect duplicate tracks within an album (same title slug, multiple files).
    Keeps the file with best format + highest bitrate; skips if durations diverge > 10%."""
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

        audio_files: list[Path] = sorted(
            p / fn for fn in filenames if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        )
        if len(audio_files) < 2:
            continue

        # Group by title slug (from tag, fall back to stem)
        groups: dict[str, list[Path]] = {}
        for fpath in audio_files:
            f = MutagenFile(str(fpath), easy=False)
            if f is None:
                continue
            t = type(f).__name__
            if t == "MP3" and f.tags:
                title = _frame_text(f.tags.get("TIT2") or "") or fpath.stem
            elif t == "FLAC":
                title = (f.get("title") or [""])[0] or fpath.stem
            elif t == "MP4" and f.tags:
                title = str((f.tags.get("\xa9nam") or [""])[0]) or fpath.stem
            else:
                title = fpath.stem
            slug = _title_slug(title)
            groups.setdefault(slug, []).append(fpath)

        dups = {slug: paths for slug, paths in groups.items() if len(paths) > 1}
        if not dups:
            continue

        albums_found += 1
        print(f"\n  {rel}")

        artist_dir = p.parent.name
        for slug, paths in dups.items():
            ranked = sorted(paths, key=lambda fp: _dup_score(fp, artist_dir))
            keep   = ranked[0]
            delete = ranked[1:]

            # Duration sanity check: if any file's duration differs > 10% from keeper — warn, don't delete
            keep_dur = getattr(getattr(MutagenFile(str(keep), easy=False), "info", None), "length", 0) or 0

            print(f"      [DUP] keep: {keep.name}")
            for dp in delete:
                dp_dur = getattr(getattr(MutagenFile(str(dp), easy=False), "info", None), "length", 0) or 0
                dur_ok = keep_dur == 0 or dp_dur == 0 or abs(keep_dur - dp_dur) / keep_dur < 0.10
                if not dur_ok:
                    print(f"            [SKIP] {dp.name} — duration mismatch ({dp_dur:.0f}s vs {keep_dur:.0f}s), verify manually")
                    continue
                print(f"            drop: {dp.name}")
                if fix:
                    dp.unlink()
                    print(f"            [deleted]")

    return albums_found


def scan_singles(root: Path, fix: bool, lastfm_key: str) -> int:
    """Dissolve Singles folders — each track gets its own Artist/TrackName/ directory."""
    folders_done: int = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p.name != "Singles":
            continue
        if is_excluded(p):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) != 2:   # only root/artist/Singles
            continue

        artist: str = p.parent.name
        audio_files: list[str] = [
            fn for fn in filenames
            if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        folders_done += 1
        print(f"\n  {rel}  ({len(audio_files)} track(s))")

        # (src_path, dest_dir, dest_path)
        moves: list[tuple[Path, Path, Path]] = []
        for fn in sorted(audio_files):
            fpath  = p / fn
            stem   = Path(fn).stem
            ext    = Path(fn).suffix

            # Extract title part (strip "Artist - " prefix and leading track number)
            title = stem.split(' - ', 1)[1] if ' - ' in stem else stem
            title = _re.sub(r'^\d+[\s.\-]+', '', title).strip()

            # Ask Last.fm for the canonical single name
            lfm_name = _lastfm_track_name(artist, title, lastfm_key)
            time.sleep(0.2)
            single_name = _safe_dirname(lfm_name if lfm_name else title)

            dest_dir  = p.parent / single_name
            dest_file = dest_dir / fn   # keep original filename; prefix scan handles it later
            moves.append((fpath, dest_dir, dest_file))

            source_label = f"Last.fm: {lfm_name!r}" if lfm_name else "filename"
            print(f"      '{fn}'")
            print(f"      → {artist}/{single_name}/  [{source_label}]")

        if fix:
            for src, dest_dir, dest_file in moves:
                dest_dir.mkdir(exist_ok=True)
                if dest_file.exists():
                    print(f"      [SKIP] {dest_file.name} already exists in target")
                else:
                    try:
                        src.rename(dest_file)
                        print(f"      [✓] moved → {dest_dir.name}/")
                    except Exception as e:
                        print(f"      [ERROR] {e}")
            # Remove empty Singles dir
            remaining = list(p.iterdir())
            if not remaining:
                p.rmdir()
                print(f"      [✓] removed empty Singles/")
            else:
                print(f"      [!] Singles/ not empty after move: {[x.name for x in remaining]}")

    return folders_done


def scan_filename_prefixes(root: Path, fix: bool) -> int:
    """Detect audio files whose filename artist prefix differs from the artist folder name.

    e.g. "30 Seconds to Mars - A Beautiful Lie.mp3" inside
         "Thirty Seconds to Mars/A Beautiful Lie/" should be renamed to
         "Thirty Seconds to Mars - A Beautiful Lie.mp3".
    """
    albums_found: int = 0

    for dirpath, _, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) != 2:   # only root/artist/album level
            continue

        artist_dir: str = p.parent.name
        audio_files: list[str] = [
            fn for fn in filenames
            if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        # (old_filename, new_filename)
        issues: list[tuple[str, str]] = []
        for fn in sorted(audio_files):
            stem = Path(fn).stem
            ext  = Path(fn).suffix
            if ' - ' not in stem:
                continue
            prefix, title = stem.split(' - ', 1)
            # Skip track-number prefixes like "01", "02 "
            if _re.fullmatch(r'\s*\d+\s*', prefix):
                continue
            if prefix == artist_dir:
                continue
            # For feat/collab prefixes like "Eminem feat D12" or "Eminem, Elton John"
            # — move the guest artist into the title as (feat. X)
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
                    # Conflict: keep better format, delete worse
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


def scan(root: Path, fix: bool, check_enc: bool, check_art: bool, check_alb: bool):
    found: int = 0

    if check_art:
        prefix_count: int = scan_filename_prefixes(root, fix)
        if prefix_count:
            print(f"\n{'─'*60}")
            print(f"  Filename prefix mismatches: {prefix_count} albums {'fixed' if fix else 'found'}")
            print(f"{'─'*60}\n")

    if check_alb:
        dir_count: int = scan_dirs(root, fix)
        if dir_count:
            print(f"\n{'─'*60}")
            print(f"  Directories: {dir_count} {'renamed' if fix else 'to rename'}")
            print(f"{'─'*60}\n")

        year_count: int = scan_album_years(root, fix)
        if year_count:
            print(f"\n{'─'*60}")
            print(f"  Year mismatches: {year_count} albums {'fixed' if fix else 'found'}")
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
    parser.add_argument("--fix",            action="store_true", help="Apply fixes (default: dry-run)")
    parser.add_argument("--encoding-only",  action="store_true", help="Only check encoding")
    parser.add_argument("--artists-only",   action="store_true", help="Only check multi-artist tags")
    parser.add_argument("--album-only",     action="store_true", help="Only check missing album tags")
    parser.add_argument("--variants-only",  action="store_true", help="Only check variant tracks (Instrumental, Revisited, etc.)")
    parser.add_argument("--tracknums-only", action="store_true", help="Set track numbers from Last.fm")
    parser.add_argument("--singles-only",   action="store_true", help="Dissolve Singles folders into per-track directories")
    parser.add_argument("--lastfm-key",     default="e4f9f2118dc2d6185af3ca25c13b7e70", help="Last.fm API key")
    parser.add_argument("--download-album", nargs="+", metavar="ARG",
                        help='Download album: --download-album "Artist" "Album" or with --all-albums')
    parser.add_argument("--all-albums",     action="store_true",
                        help="With --download-album: download every album for the artist")
    args = parser.parse_args()

    if args.download_album:
        artist = args.download_album[0]
        root = Path(args.path)
        if args.all_albums:
            albums = _lastfm_artist_albums(artist, args.lastfm_key)
            if not albums:
                print(f"No albums found for '{artist}' on Last.fm")
                sys.exit(1)
            print(f"Found {len(albums)} album(s) for '{artist}'")
            for alb in albums:
                print(f"\n→ Downloading: {artist} — {alb}")
                sys.stdout.flush()
                subprocess.run([
                    "python3", "/app/download_music.py",
                    "--album", artist, alb,
                    "--out", str(root),
                    "--lastfm-key", args.lastfm_key,
                ])
        else:
            if len(args.download_album) < 2:
                albums = _lastfm_artist_albums(artist, args.lastfm_key)
                if not albums:
                    print(f"No albums found for '{artist}' on Last.fm")
                    sys.exit(1)
                print(f"Albums available for '{artist}' ({len(albums)}):")
                for i, alb in enumerate(albums, 1):
                    print(f"  {i:>3}. {alb}")
                sys.exit(0)
            album = " ".join(args.download_album[1:])
            print(f"→ Downloading: {artist} — {album}")
            sys.stdout.flush()
            subprocess.run([
                "python3", "/app/download_music.py",
                "--album", artist, album,
                "--out", str(root),
                "--lastfm-key", args.lastfm_key,
            ])
        sys.exit(0)

    run_all         = not (args.encoding_only or args.artists_only or args.album_only
                           or args.variants_only or args.tracknums_only or args.singles_only)
    check_enc       = run_all or args.encoding_only
    check_art       = run_all or args.artists_only
    check_alb       = run_all or args.album_only
    check_variants  = args.variants_only
    check_tracknums = args.tracknums_only
    check_singles   = args.singles_only

    root = Path(args.path)
    if not root.exists():
        print(f"ERROR: path not found: {root}")
        sys.exit(1)

    if root.is_file():
        process_file(root, args.fix, check_enc, check_art, check_alb)
    elif check_singles and not run_all:
        print(f"Scanning: {root}")
        print(f"Checks: Singles folders (Last.fm single names)")
        print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}\n{'─'*60}")
        n = scan_singles(root, args.fix, args.lastfm_key)
        print(f"\n{'─'*60}")
        print(f"  Singles folders processed: {n}")
        print(f"  Mode: {'FIX applied' if args.fix else 'DRY-RUN (use --fix to apply changes)'}")
    elif check_variants and not run_all:
        print(f"Scanning: {root}")
        print(f"Checks: variants")
        print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}\n{'─'*60}")
        scan_variants(root, args.fix)
    elif check_tracknums and not run_all:
        print(f"Scanning: {root}")
        print(f"Checks: track numbers (Last.fm)")
        print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}\n{'─'*60}")
        n = scan_track_numbers(root, args.fix, args.lastfm_key)
        print(f"\n{'─'*60}")
        print(f"  Albums processed: {n}")
        print(f"  Mode: {'FIX applied' if args.fix else 'DRY-RUN (use --fix to apply changes)'}")
    else:
        checks = []
        if check_enc:   checks.append("encoding")
        if check_art:   checks.append("artists")
        if check_alb:   checks.append("album")
        if run_all:     checks += ["variants", "track-numbers", "singles", "duplicates"]
        print(f"Scanning: {root}")
        print(f"Checks: {' '.join(checks)}")
        print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}\n{'─'*60}")
        scan(root, args.fix, check_enc, check_art, check_alb)
        if run_all:
            scan_duplicates(root, args.fix)
            scan_variants(root, args.fix)
            scan_track_numbers(root, args.fix, args.lastfm_key)
            n = scan_singles(root, args.fix, args.lastfm_key)
            if n:
                print(f"\n{'─'*60}")
                print(f"  Singles folders processed: {n}")
            print(f"\n{'─'*60}")
            print(f"  Mode: {'FIX applied' if args.fix else 'DRY-RUN (use --fix to apply changes)'}")

if __name__ == "__main__":
    main()