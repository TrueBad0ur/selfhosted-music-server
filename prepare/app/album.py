import os
import re as _re
from pathlib import Path
from mutagen import File as MutagenFile

from common import AUDIO_EXTENSIONS, audio_content_hash, is_excluded, is_playlist_folder
from tags import _extract_year, _set_year, _raw_date, get_tags

_YEAR_DIR_PREFIX_RE = _re.compile(
    r'^(?:'
    r'\[\d{4}[.\-]\d{2}[.\-]\d{2}\]\s*'               # [YYYY.MM.DD] prefix
    r'|\d{4}(?!\.\d{2})(?:\s*\[\d{4}\])?\s*[-\.]\s*'  # YYYY - or YYYY.
    r')'
)

_META_PAREN_KEYWORDS = _re.compile(
    r'\b(deluxe|explicit|clean|remaster(?:ed)?|video|audio|official|'
    r'(?:standard|bonus|special|limited|anniversary|collector.?s)\s+(?:edition|tracks?)|'
    r'bonus\s+tracks?|компиляция|сборник)\b',
    _re.IGNORECASE
)

_PURE_DISC_DIR_RE = _re.compile(
    r"(?:[\(\[]\s*(?:disc|cd|lp)\s*\d+\s*[\)\]]|\b(?:disc|cd|lp)\s*\d+\b|^(?:CD|LP)\d+$)",
    _re.IGNORECASE,
)


def _is_meta_paren(content: str) -> bool:
    c = content.strip()
    if _META_PAREN_KEYWORDS.search(c):
        return True
    if _re.match(r'^\d{4}\s*,', c):
        return True
    if _re.fullmatch(r'\d{4}(\s+\w+)*', c):
        return True
    return False


def clean_album_dirname(name: str) -> str:
    """Strip year prefix and trailing release metadata from album directory names."""
    s = _YEAR_DIR_PREFIX_RE.sub('', name)
    if not s.strip() or _re.fullmatch(r'\d{4}(\s*[-–]\s*\d{4})?', s.strip()):
        return name
    s = _re.sub(r'\s*-\s*\d+[xX]?(LP|CD|EP)\s*$', '', s, flags=_re.IGNORECASE)
    s = _re.sub(r'\s*-\s*(Single|EP)\s*$', '', s, flags=_re.IGNORECASE)
    s = ' '.join(s.replace('~', ' ').split())
    changed = True
    while changed:
        m = _re.search(r'\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$', s)
        if m and _is_meta_paren(m.group(1)):
            s = s[:m.start()]
            changed = True
        else:
            changed = False
    s = _re.sub(r'[\s\-.,]+$', '', s)
    return s if s else name


def scan_album_years(root: Path, fix: bool) -> int:
    """Detect year-tag mismatches within album folders that cause Navidrome album splits."""
    albums_fixed: int = 0

    for dirpath, _, filenames in os.walk(root):
        p = Path(dirpath)
        if is_excluded(p) or is_playlist_folder(p):
            continue
        audio_paths: list[Path] = [
            p / fn for fn in filenames
            if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if len(audio_paths) < 2:
            continue
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
        canonical: str = min(years, key=int)
        year_conflict = len(years) > 1
        raw_conflict  = any(
            raw and raw != canonical
            for _, year, raw, _ in entries
            if year == canonical
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


class AlbumMergeError(RuntimeError):
    pass


def _conflict_destination(dst: Path, source_label: str) -> Path:
    label = _re.sub(r"[^\w .()\[\]-]+", "_", source_label).strip(" .") or "alternate"
    candidate = dst.with_name(f"{dst.stem} [{label}]{dst.suffix}")
    index = 2
    while candidate.exists():
        candidate = dst.with_name(f"{dst.stem} [{label} {index}]{dst.suffix}")
        index += 1
    return candidate


def _merge_entry(src: Path, dst: Path, source_label: str) -> tuple[int, int]:
    """Merge one entry without discarding files whose audio content differs."""
    if not dst.exists():
        src.rename(dst)
        return 1, 0
    if src.is_dir() and dst.is_dir():
        moved = duplicates = 0
        for child in sorted(src.iterdir()):
            child_moved, child_duplicates = _merge_entry(child, dst / child.name, source_label)
            moved += child_moved
            duplicates += child_duplicates
        if not any(src.iterdir()):
            src.rmdir()
        return moved, duplicates
    if src.is_file() and dst.is_file():
        if audio_content_hash(src) == audio_content_hash(dst):
            src.unlink()
            return 0, 1
        alternate = _conflict_destination(dst, source_label)
        src.rename(alternate)
        print(f"      [merge] preserved different audio as '{alternate.name}'")
        return 1, 0
    alternate = _conflict_destination(dst, source_label)
    src.rename(alternate)
    print(f"      [merge] preserved type conflict as '{alternate.name}'")
    return 1, 0


def scan_nested_track_dirs(root: Path, fix: bool) -> int:
    """Flatten path separators accidentally embedded in track titles."""
    planned: list[tuple[Path, Path, Path]] = []
    for dirpath, _, filenames in os.walk(root):
        directory = Path(dirpath)
        if is_excluded(directory):
            continue
        for filename in filenames:
            source = directory / filename
            if source.suffix.casefold() not in AUDIO_EXTENSIONS:
                continue
            try:
                relative = source.relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) <= 3:
                continue
            album_dir = root / relative.parts[0] / relative.parts[1]
            nested_parts = list(relative.parts[2:-1])
            destination_dir = album_dir
            if nested_parts and _PURE_DISC_DIR_RE.search(nested_parts[0]):
                destination_dir = album_dir / nested_parts.pop(0)
                if not nested_parts:
                    continue
            reconstructed = "⧸".join([*nested_parts, relative.name])
            try:
                title = get_tags(MutagenFile(str(source), easy=False)).get("title", "")
            except Exception:
                title = ""
            reconstructed_title = "⧸".join([*nested_parts, source.stem])
            artist_prefix = f"{relative.parts[0]} - "
            if reconstructed_title.casefold().startswith(artist_prefix.casefold()):
                reconstructed_title = reconstructed_title[len(artist_prefix):]
            normalize = lambda value: _re.sub(r"[^\w]", "", value.casefold())
            if not title or normalize(title) != normalize(reconstructed_title):
                continue
            planned.append((source, destination_dir / reconstructed, album_dir))

    errors: list[str] = []
    for source, destination, album_dir in planned:
        print(f"\n  nested: {source.relative_to(root)}")
        print(f"      [!] → {destination.relative_to(root)}")
        if not fix:
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if audio_content_hash(source) == audio_content_hash(destination):
                    source.unlink()
                    print("      [✓] removed verified duplicate")
                else:
                    alternate = _conflict_destination(destination, "nested")
                    source.rename(alternate)
                    print(f"      [✓] preserved different audio as '{alternate.name}'")
            else:
                source.rename(destination)
                print("      [✓] flattened")
            parent = source.parent
            while parent != album_dir and parent.is_relative_to(album_dir):
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except OSError as exc:
            message = f"{source}: {exc}"
            errors.append(message)
            print(f"      [ERROR] {message}")
    if errors:
        raise AlbumMergeError(f"nested track cleanup failed for {len(errors)} item(s)")
    return len(planned)


def scan_dirs(root: Path, fix: bool) -> int:
    """Report (and optionally rename) album directories with year prefixes or release junk."""
    renames = []
    for dirpath, _, _ in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root or is_excluded(p):
            continue
        clean = clean_album_dirname(p.name)
        if clean != p.name and clean:
            renames.append((p, p.parent / clean))

    errors: list[str] = []
    for old, new in renames:
        if not old.exists():
            continue
        print(f"\n  dir: {old.relative_to(root)}")
        print(f"      [!] '{old.name}' → '{new.name}'")
        if fix:
            try:
                if not new.exists():
                    old.rename(new)
                    print(f"      [✓] renamed")
                    continue
                merged = duplicates = 0
                for src in sorted(old.iterdir()):
                    try:
                        item_merged, item_duplicates = _merge_entry(src, new / src.name, old.name)
                        merged += item_merged
                        duplicates += item_duplicates
                    except OSError as exc:
                        message = f"{src}: {exc}"
                        errors.append(message)
                        print(f"      [ERROR] {message}")
                remaining = list(old.iterdir())
                if not remaining:
                    old.rmdir()
                    print(f"      [✓] merged {merged} item(s) into '{new.name}'" +
                          (f", removed {duplicates} verified duplicate(s)" if duplicates else ""))
                else:
                    print(f"      [!] '{old.name}' still has {len(remaining)} item(s) after merge")
            except OSError as exc:
                message = f"{old}: {exc}"
                errors.append(message)
                print(f"      [ERROR] {message}")

    if errors:
        raise AlbumMergeError(f"album directory merge failed for {len(errors)} item(s)")
    return len(renames)
