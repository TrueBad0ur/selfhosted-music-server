import os
import re as _re
from pathlib import Path
from mutagen import File as MutagenFile

from common import AUDIO_EXTENSIONS, EXCLUDE_DIRS, is_excluded, _FORMAT_PRIORITY
from tags import _extract_year, _set_year, _raw_date

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
        if is_excluded(p):
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

    for old, new in renames:
        print(f"\n  dir: {old.relative_to(root)}")
        print(f"      [!] '{old.name}' → '{new.name}'")
        if fix:
            if not new.exists():
                old.rename(new)
                print(f"      [✓] renamed")
            else:
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
