import os
import re as _re
import sys
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path
from mutagen import File as MutagenFile

from common import AUDIO_EXTENSIONS, is_excluded
from tags import _get_tracknum, _set_tracknum, _frame_text
from album import clean_album_dirname
from lastfm import _lastfm_tracklist, _match_to_tracklist, _title_slug

_BONUS_TRACK_RE = _re.compile(
    r'\b(acoustic|live|remix|edit|radio.?edit|instrumental|karaoke|cover|'
    r'reprise|interlude|skit|demo|dj\s|bonus|version|remaster)\b',
    _re.IGNORECASE,
)


def scan_track_numbers(root: Path, fix: bool, lastfm_key: str) -> int:
    """Set track number tags from Last.fm. Falls back to sequential when no data."""
    albums_done: int = 0
    to_download: list = []

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

        audio_paths: list[Path] = sorted(
            p / fn for fn in filenames if Path(fn).suffix.lower() in AUDIO_EXTENSIONS
        )
        if not audio_paths:
            continue

        if (p / '.skip').exists():
            print(f'  [SKIP] {rel} (.skip marker)')
            continue

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

        assignments = []
        for fpath, f, existing_trck in file_data:
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
            if match is None and tag_title:
                match = _match_to_tracklist(_title_slug(fpath.stem), tracklist)
            rank  = match[0] if match else None
            assignments.append((fpath, f, rank, existing_trck))

        covered   = {rank for _, _, rank, _ in assignments if rank is not None}
        lfm_ranks = {rank for rank, _ in tracklist.values()}
        missing   = lfm_ranks - covered

        rank_to_name = {rank: name for _, (rank, name) in tracklist.items()}

        unmatched_slugs: list[tuple[str, Path]] = []
        for _fp, _f, _rank, _ in assignments:
            if _rank is not None:
                continue
            _t = type(_f).__name__
            if _t == 'MP3' and _f.tags:
                _tt = _frame_text(_f.tags.get('TIT2') or '')
            elif _t == 'FLAC':
                _tt = (_f.get('title') or [''])[0]
            elif _t == 'MP4' and _f.tags:
                _tt = str((_f.tags.get('\xa9nam') or [''])[0])
            else:
                _tt = ''
            _slug = _title_slug(_tt) if _tt else _title_slug(_fp.stem)
            unmatched_slugs.append((_slug, _fp))

        if missing:
            sorted_asgn = sorted(assignments, key=lambda x: (x[2] is None, x[2] or 9999))
            final = [(fpath, f, new_rank, existing_trck)
                     for new_rank, (fpath, f, _, existing_trck)
                     in enumerate(sorted_asgn, 1)]
        else:
            final = [(fpath, f, rank if rank is not None else existing_trck, existing_trck)
                     for fpath, f, rank, existing_trck in assignments]

        changes = [(fpath, f, rank, trck) for fpath, f, rank, trck in final if rank != trck]

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
                    missing_slug = _title_slug(name)
                    near = next(
                        (fp for sl, fp in unmatched_slugs
                         if SequenceMatcher(None, missing_slug, sl).ratio() >= 0.82),
                        None,
                    )
                    if near:
                        canonical = near.parent / f"{artist} - {name}{near.suffix}"
                        print(f"      [NEAR MATCH] track {rank}: '{name}' ≈ '{near.name}'")
                        print(f"               → rename: '{near.name}' → '{canonical.name}'")
                        if fix:
                            try:
                                near.rename(canonical)
                                print(f"               [✓] renamed")
                            except Exception as e:
                                print(f"               [ERROR] {e}")
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
