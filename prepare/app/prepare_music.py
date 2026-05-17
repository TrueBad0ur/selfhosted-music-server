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

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)

from common import AUDIO_EXTENSIONS, is_excluded
from process_file import process_file, scan_filename_prefixes
from album import scan_album_years, scan_dirs
from scan_tracks import scan_track_numbers
from scan_variants import scan_variants
from scan_duplicates import scan_duplicates
from scan_singles import scan_singles
from lastfm import _lastfm_artist_albums


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
    parser.add_argument("--variants-only",  action="store_true", help="Only check variant tracks")
    parser.add_argument("--tracknums-only", action="store_true", help="Set track numbers from Last.fm")
    parser.add_argument("--singles-only",   action="store_true", help="Dissolve Singles folders")
    parser.add_argument("--lastfm-key",     default="e4f9f2118dc2d6185af3ca25c13b7e70", help="Last.fm API key")
    parser.add_argument("--download-album", nargs="+", metavar="ARG",
                        help='Download album: --download-album "Artist" "Album" or with --all-albums')
    parser.add_argument("--all-albums",     action="store_true",
                        help="With --download-album: download every album for the artist")
    parser.add_argument("--list-albums",    metavar="ARTIST",
                        help="List all available albums for an artist on Last.fm")
    args = parser.parse_args()

    if args.list_albums:
        albums = _lastfm_artist_albums(args.list_albums, args.lastfm_key)
        if not albums:
            print(f"No albums found for '{args.list_albums}' on Last.fm")
            sys.exit(1)
        print(f"Albums for '{args.list_albums}' ({len(albums)}):")
        for i, alb in enumerate(albums, 1):
            print(f"  {i:>3}. {alb}")
        sys.exit(0)

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
