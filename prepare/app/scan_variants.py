import os
import re as _re
from collections import defaultdict
from pathlib import Path

from common import AUDIO_EXTENSIONS, is_excluded, keeps_remixes, _FORMAT_PRIORITY

_VARIANT_SUFFIX_RE = _re.compile(
    r'\s*[\(\[](instrumental|revisited|remix|remixed|version|edit|live|acoustic|'
    r'demo|extended|radio edit|reprise|interlude|intro|outro|feat\.|ft\.|'
    r'\d{4})\b.*?[\)\]]',
    _re.IGNORECASE
)
# Same as above minus remix/remixed - used for albums marked to keep their
# remixes, so a remix track is never grouped with (and dropped in favor of)
# its original.
_VARIANT_SUFFIX_NO_REMIX_RE = _re.compile(
    r'\s*[\(\[](instrumental|revisited|version|edit|live|acoustic|'
    r'demo|extended|radio edit|reprise|interlude|intro|outro|feat\.|ft\.|'
    r'\d{4})\b.*?[\)\]]',
    _re.IGNORECASE
)
_VARIANT_DASH_RE = _re.compile(
    r'\s+-\s+(Instrumental|Interlude\s*\d*|Intro|Outro)\s*$',
    _re.IGNORECASE
)
_CONTENT_ALTERING_RE = _re.compile(
    r'\b(instrumental|live|acoustic|demo|remix|remixed|revisited|reprise|interlude|intro|outro)\b',
    _re.IGNORECASE
)
_REMASTER_RE = _re.compile(r'\b(remaster(?:ed)?)\b', _re.IGNORECASE)


def _variant_base(stem: str, keep_remixes: bool = False) -> str:
    suffix_re = _VARIANT_SUFFIX_NO_REMIX_RE if keep_remixes else _VARIANT_SUFFIX_RE
    if ' - ' in stem:
        stem = stem.split(' - ', 1)[1]
    stem = suffix_re.sub('', stem)
    stem = _VARIANT_DASH_RE.sub('', stem)
    return stem.strip().lower()


def _is_variant_stem(stem: str, keep_remixes: bool = False) -> bool:
    suffix_re = _VARIANT_SUFFIX_NO_REMIX_RE if keep_remixes else _VARIANT_SUFFIX_RE
    check = stem.split(' - ', 1)[1] if ' - ' in stem else stem
    return bool(suffix_re.search(check) or _VARIANT_DASH_RE.search(check))


def scan_variants(root: Path, fix: bool):
    """Report (and optionally delete) variant tracks (Instrumental, Revisited, etc.)."""
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

        keep_remixes = keeps_remixes(p)

        groups = defaultdict(list)
        for fname in audio_files:
            bn = _variant_base(Path(fname).stem, keep_remixes)
            if bn:
                groups[bn].append(fname)

        album_issues = []

        for base, group in sorted(groups.items()):
            if len(group) == 1:
                continue
            variants  = [f for f in group if _is_variant_stem(Path(f).stem, keep_remixes)]
            originals = [f for f in group if not _is_variant_stem(Path(f).stem, keep_remixes)]

            if variants and originals:
                best_var_fmt  = min(_FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99) for f in variants)
                best_orig_fmt = min(_FORMAT_PRIORITY.get(Path(f).suffix.lower(), 99) for f in originals)
                def _check(f, regex):
                    s = Path(f).stem
                    return regex.search(s.split(' - ', 1)[1] if ' - ' in s else s)
                content_altering = [f for f in variants if _check(f, _CONTENT_ALTERING_RE)]
                is_remaster      = any(_check(f, _REMASTER_RE) for f in variants)
                if not content_altering and (best_var_fmt < best_orig_fmt or is_remaster):
                    album_issues.append((f"variant '{base}' [prefer remaster]", originals, variants))
                else:
                    album_issues.append((f"variant '{base}'", variants, originals))
            elif len(originals) > 1:
                handled = set()

                by_lower = defaultdict(list)
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

                by_track = defaultdict(list)
                for f in originals:
                    if f in handled:
                        continue
                    stem = Path(f).stem.lower()
                    track = stem.split(' - ', 1)[1] if ' - ' in stem else stem
                    by_track[track].append(f)
                for track, track_files in by_track.items():
                    if len(track_files) <= 1:
                        continue
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
