import os
import re as _re
from pathlib import Path
from mutagen import File as MutagenFile

from common import AUDIO_EXTENSIONS, is_excluded, keeps_remixes, _FORMAT_PRIORITY
from tags import _frame_text

_DUP_DELETE_VARIANT_RE = _re.compile(
    r'\b(radio[\s.]?(?:edit|mix|version)|live(?:\s+(?:at|in|from|in\s+concert))?|remaster(?:ed)?|'
    r'elements\s+live|in\s+concert|remix(?:ed)?)\b',
    _re.IGNORECASE,
)
# Same as above minus remix/remixed - used for albums marked to keep their
# remixes, so a remix is never preferentially dropped over a duration-mismatched
# original.
_DUP_DELETE_VARIANT_NO_REMIX_RE = _re.compile(
    r'\b(radio[\s.]?(?:edit|mix|version)|live(?:\s+(?:at|in|from|in\s+concert))?|remaster(?:ed)?|'
    r'elements\s+live|in\s+concert)\b',
    _re.IGNORECASE,
)


def _dup_title_slug(title: str) -> str:
    """Normalise a plain TITLE tag (not "Artist - Title", so no splitting on
    " - " like lastfm._title_slug does - that would drop the song name and
    keep only a trailing qualifier, e.g. "Decode - Live at Red Rocks").
    "?" is kept since it's sometimes the only thing distinguishing two
    otherwise-identical titles (The Wall's "In The Flesh?" vs "In The Flesh")."""
    value = _re.sub(r"^\d+[\s.\-]+", "", title)
    return _re.sub(r"[^\w?]", "", value.casefold())


def _safe_dirname(name: str) -> str:
    """Strip characters that are invalid in directory names.

    "?" is deliberately kept: this is a Linux filesystem, where it's a perfectly
    valid character, and stripping it loses real information (e.g. Pink Floyd's
    "In The Flesh?" vs the later reprise "In The Flesh" become indistinguishable).
    """
    return _re.sub(r'[<>:"/\\|*]', '', name).strip(' .')


def _dup_score(fp: Path, artist_dir: str) -> tuple:
    """Lower score = better file to keep.
    Priority: format > bitrate > standard naming > file size."""
    fmt = _FORMAT_PRIORITY.get(fp.suffix.lower(), 99)
    f = MutagenFile(str(fp), easy=False)
    bitrate = 0
    if f and hasattr(f, "info"):
        bitrate = getattr(f.info, "bitrate", 0) or 0
    stem = fp.stem
    has_dup_suffix = bool(_re.search(r'[_(]\d+\)?$', stem))
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
            slug = _dup_title_slug(title)
            groups.setdefault(slug, []).append(fpath)

        dups = {slug: paths for slug, paths in groups.items() if len(paths) > 1}
        if not dups:
            continue

        albums_found += 1
        print(f"\n  {rel}")

        dup_variant_re = _DUP_DELETE_VARIANT_NO_REMIX_RE if keeps_remixes(p) else _DUP_DELETE_VARIANT_RE
        artist_dir = p.parent.name
        for slug, paths in dups.items():
            ranked = sorted(paths, key=lambda fp: _dup_score(fp, artist_dir))
            keep   = ranked[0]
            delete = ranked[1:]

            keep_dur = getattr(getattr(MutagenFile(str(keep), easy=False), "info", None), "length", 0) or 0

            print(f"      [DUP] keep: {keep.name}")
            for dp in delete:
                dp_dur = getattr(getattr(MutagenFile(str(dp), easy=False), "info", None), "length", 0) or 0
                dur_ok = keep_dur == 0 or dp_dur == 0 or abs(keep_dur - dp_dur) / keep_dur < 0.10
                if not dur_ok:
                    keep_is_edit = bool(dup_variant_re.search(keep.stem))
                    dp_is_edit   = bool(dup_variant_re.search(dp.stem))
                    if keep_is_edit and not dp_is_edit:
                        print(f"      [DUP] keep: {dp.name}")
                        print(f"            drop (variant): {keep.name}")
                        if fix:
                            keep.unlink()
                            print(f"            [deleted]")
                    else:
                        print(f"            [SKIP] {dp.name} — duration mismatch ({dp_dur:.0f}s vs {keep_dur:.0f}s), verify manually")
                    continue
                print(f"            drop: {dp.name}")
                if fix:
                    dp.unlink()
                    print(f"            [deleted]")

    return albums_found
