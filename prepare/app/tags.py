import sys

try:
    from mutagen.id3 import TDRC, TRCK, TIT2, TPE1, TPE2, TALB
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)


def _frame_text(frame) -> str:
    """Extract text content from an ID3 frame."""
    if hasattr(frame, "url"):
        return frame.url
    if hasattr(frame, "text"):
        t = frame.text
        return str(t[0]) if isinstance(t, list) else str(t)
    return str(frame)


def get_tag_values(f, key: str) -> list[str]:
    """Return every stored value for a normalized tag key."""
    t = type(f).__name__
    if t == "MP3":
        mapping = {
            "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
            "title": "TIT2", "tracknumber": "TRCK",
        }
        frame = f.tags.get(mapping.get(key, "")) if f.tags else None
        raw = frame.text if frame is not None and hasattr(frame, "text") else None
    elif t == "FLAC":
        raw = f.get(key)
    elif t == "MP4":
        mapping = {
            "artist": "\xa9ART", "albumartist": "aART", "album": "\xa9alb",
            "title": "\xa9nam", "tracknumber": "trkn",
        }
        raw = f.tags.get(mapping.get(key, "")) if f.tags else None
    else:
        raw = f.tags.get(key) if f.tags else None
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(value) for value in values if value is not None and str(value)]


def get_tags(f) -> dict:
    """Return a normalized dict: artist, albumartist, album, title."""
    tags = {}
    for key in ("artist", "albumartist", "album", "title", "tracknumber"):
        values = get_tag_values(f, key)
        if values:
            tags[key] = "; ".join(values) if key == "artist" else values[0]

    return {k: v for k, v in tags.items() if v}


def set_tag(f, key: str, value):
    """Write a tag back. value can be str or list of str (for multi-value artist)."""
    t = type(f).__name__

    if t == "MP3":
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
        if f.tags is None:
            f.add_tags()
        f.tags["TRCK"] = TRCK(encoding=3, text=[str(num)])
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
        if f.tags is None:
            f.add_tags()
        f.tags.add(TDRC(encoding=0, text=[year]))
    elif t == "FLAC":
        f["date"] = [year]
        if "year" in f:
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
        return year_val if len(year_val) > len(date_val) else date_val
    elif t == "MP4" and f.tags:
        return str((f.tags.get("\xa9day") or [""])[0])
    return ""
