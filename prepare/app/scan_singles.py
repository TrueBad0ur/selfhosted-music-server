import os
import re as _re
import time
from pathlib import Path

from common import AUDIO_EXTENSIONS, is_excluded
from lastfm import _lastfm_track_name
from scan_duplicates import _safe_dirname


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
        if len(rel.parts) != 2:
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

        moves: list[tuple[Path, Path, Path]] = []
        for fn in sorted(audio_files):
            fpath  = p / fn
            stem   = Path(fn).stem
            title = stem.split(' - ', 1)[1] if ' - ' in stem else stem
            title = _re.sub(r'^\d+[\s.\-]+', '', title).strip()
            lfm_name = _lastfm_track_name(artist, title, lastfm_key)
            time.sleep(0.2)
            single_name = _safe_dirname(lfm_name if lfm_name else title)
            dest_dir  = p.parent / single_name
            dest_file = dest_dir / fn
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
            remaining = list(p.iterdir())
            if not remaining:
                p.rmdir()
                print(f"      [✓] removed empty Singles/")
            else:
                print(f"      [!] Singles/ not empty after move: {[x.name for x in remaining]}")

    return folders_done
