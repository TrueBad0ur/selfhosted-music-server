import hashlib
import re as _re
from pathlib import Path

from tags import _frame_text

# ── bad chars ─────────────────────────────────────────────────────────────────

_BAD_CHARS = {"�"}

def has_bad_chars(s: str) -> bool:
    return any(c in _BAD_CHARS for c in s)

def strip_bad_chars(s: str) -> str:
    return "".join(c for c in s if c not in _BAD_CHARS).strip()

def check_bad_chars(tags: dict) -> dict:
    """Return {field: cleaned_value} for any fields containing broken-encoding chars."""
    return {f: strip_bad_chars(v) for f, v in tags.items() if v and has_bad_chars(v)}

# ── watermarks ────────────────────────────────────────────────────────────────

_WATERMARK_RE = _re.compile(
    r"\s*[\[\(](?!(?:feat|ft)\.)[\w.-]+\.[a-zA-Z]{2,}[\]\)]",
    _re.IGNORECASE,
)
_WATERMARK_URL_RE = _re.compile(r"https?://[\w.-]+\.[\w]{2,}", _re.IGNORECASE)

def strip_watermarks(s: str) -> str:
    return _WATERMARK_RE.sub("", s).strip()

def check_watermarks(tags: dict) -> dict:
    """Return {field: cleaned_value} for any fields containing site watermarks."""
    return {f: strip_watermarks(v) for f, v in tags.items()
            if v and isinstance(v, str) and _WATERMARK_RE.search(v)}

# ── title/artist prefix ───────────────────────────────────────────────────────

def check_title_artist_prefix(tags: dict, stem: str = "") -> str | None:
    """Return cleaned title if title tag has 'Artist - Title' prefix matching artist tag."""
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

# ── ID3 junk frames ───────────────────────────────────────────────────────────

_JUNK_FRAME_KEYS = {"WXXX", "TCOP"}

_SPAM_COVER_MD5 = {
    "dfb57101e4ec83f5cae72bc0f28d155f",  # muzmo.ru 85873-byte banner
}

_JUNK_TXXX_DESCS = {
    "album artist", "albumartist", "album_artist",
    "totaltracks", "totaldiscs",
    "replaygain_track_gain", "replaygain_track_peak",
    "replaygain_album_gain", "replaygain_album_peak",
}

_JUNK_TXXX_YOUTUBE: set[str] = {
    "purl", "comment", "synopsis", "description",
    "episode_id", "episode_sort", "season_number",
}

_JUNK_FLAC_KEYS = {"album artist", "album_artist", "discnumber", "disc"}

_JUNK_FLAC_YOUTUBE: set[str] = {
    "comment", "description", "synopsis", "purl",
    "episode_id", "episode_sort", "season_number",
    "major_brand", "minor_version", "compatible_brands",
}

_JUNK_GENRE_RE = _re.compile(
    r'^(people\s*&\s*blogs?|film\s*&\s*animation|gaming|howto\s*&\s*style|'
    r'news\s*&\s*politics|nonprofits?\s*&\s*activism|science\s*&\s*technology|'
    r'sports|travel\s*&\s*events|education|entertainment|autos?\s*&\s*vehicles?)$',
    _re.IGNORECASE,
)

_SPAM_COMMENT_RE = _re.compile(
    r"(^collected\s+by\b"
    r"|@[\w.-]+\.\w{2,}"
    r"|www\."
    r"|^[\s0-9a-fA-F]{20,}$"
    r"|ya\s*music\b"
    r"|itunes\b"
    r"|только\s+для\s+ознакомления"
    r")",
    _re.IGNORECASE,
)

def _is_spam_comment(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
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
        if frame_type == "PRIV":
            owner = key[5:] if key.startswith("PRIV:") else ""
            if owner.startswith("WM/") or owner in ("AverageLevel", "PeakValue"):
                junk.append((key, f"WMP private frame [{owner}]"))
            continue
        if frame_type == "TCON":
            text = _frame_text(f.tags[key]).strip()
            if _JUNK_GENRE_RE.match(text):
                junk.append((key, text))
            continue
        if frame_type == "USLT":
            junk.append((key, _frame_text(f.tags[key])[:80]))
            continue
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
    """Return list of (kind, ref, size) for known spam cover art."""
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
        if kl == "year" and (f.get("date") or [None])[0]:
            junk.append((key, val))
            continue
        if kl == "genre" and _JUNK_GENRE_RE.match(val.strip()):
            junk.append((key, val))
    return junk

# ── artist detection ──────────────────────────────────────────────────────────

_UNKNOWN_ARTIST = {"unknown artist", "unknown", "неизвестный исполнитель", "неизвестный"}
_YEAR_DIR = _re.compile(r"^\d{4}[\s\-\[]")

def artist_from_filename(stem: str) -> str | None:
    """Extract artist from 'Artist - Title' filename stem."""
    m = _re.match(r"^(.+?)\s+-\s+.+$", stem)
    if not m:
        return None
    candidate = m.group(1).strip()
    if _re.fullmatch(r"\d+", candidate):
        return None
    return candidate

def artist_from_trailing_parens(stem: str) -> str | None:
    """Extract artist from a '... - Title (Artist)' filename stem - the
    convention used by rips (e.g. AniTousen anime OP/ED collections) that
    credit the performer in trailing parens rather than a leading prefix,
    which artist_from_filename() can't handle (it would grab the episode/
    season label before the first ' - ' instead)."""
    m = _re.search(r"\(([^()]+)\)\s*$", stem)
    if not m:
        return None
    candidate = m.group(1).strip()
    return candidate or None

def artist_from_path(path: Path) -> str | None:
    """Walk up the directory tree to find artist name."""
    parts = list(path.parents)
    for p in parts[1:5]:
        name = p.name
        if not name:
            break
        if _YEAR_DIR.match(name) or _re.fullmatch(r"\d+", name):
            continue
        return name
    return None

def _extract_feat_from_parens(s: str):
    """Split 'Artist (feat. X)' → main='Artist', feats=['X']."""
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
        key = x.casefold()
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out

_NAME_CONTENT_RE = _re.compile(r"\w")


def _is_name_fragment(value: str) -> bool:
    """A split piece with no letters/digits at all (e.g. a stray "." left
    over when an artist's own stylized name ends in a period, like "алёна
    швец.") isn't a second artist - without this filter it gets kept as one,
    and every later re-join/re-split pass (get_tags() joins multi-value
    artist with "; ", check_artists() splits on ";") re-manufactures it
    forever instead of ever collapsing back to the single real name."""
    return bool(_NAME_CONTENT_RE.search(value))


def clean_artist_string(artist: str) -> str:
    """Strip junk (letterless/digitless) segments out of a single semicolon-
    joined artist string, e.g. "алёна швец; ." -> "алёна швец". Separate from
    check_artists(): that only fires when 2+ *real* names remain, so a lone
    junk segment tacked onto one real artist would never get cleaned there -
    it isn't "multiple artists", just one artist with debris attached."""
    if ";" not in artist:
        return artist
    parts = [p.strip() for p in artist.split(";") if p.strip() and _is_name_fragment(p)]
    return "; ".join(parts) if parts else artist


def _split_and_expand(raw_parts: list) -> list:
    result = []
    for part in raw_parts:
        main, feats = _extract_feat_from_parens(part)
        result.append(main)
        result.extend(feats)
    return _dedup(p for p in result if _is_name_fragment(p))

def check_artists(tags: dict) -> list:
    """Detect multiple artists using cascade separators (;, /, ,, feat/ft outside parens)."""
    artist = tags.get("artist", "")
    if not artist:
        return []
    if ";" in artist:
        parts = [p.strip() for p in artist.split(";") if p.strip() and _is_name_fragment(p)]
        if len(parts) > 1:
            return _split_and_expand(parts)
    if "/" in artist:
        parts = [p.strip() for p in artist.split("/") if p.strip() and _is_name_fragment(p)]
        if len(parts) > 1:
            return _split_and_expand(parts)
    if "," in artist:
        parts = [p.strip() for p in artist.split(",") if p.strip() and _is_name_fragment(p)]
        if len(parts) > 1:
            return _split_and_expand(parts)
    no_parens = _re.sub(r"\([^)]*\)", "", artist)
    m = _re.search(r"\s+(?:feat\.?|ft\.?)\s+", no_parens, _re.IGNORECASE)
    if m:
        sep_pat = _re.compile(r"\s+(?:feat\.?|ft\.?)\s+", _re.IGNORECASE)
        parts = [p.strip() for p in sep_pat.split(no_parens) if p.strip() and _is_name_fragment(p)]
        if len(parts) > 1:
            return parts
    return []
