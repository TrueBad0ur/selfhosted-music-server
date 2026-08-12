#!/usr/bin/env python3
"""Music library metadata checker, fixer, downloader and staged intake CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from album import AlbumMergeError, scan_album_years, scan_dirs, scan_nested_track_dirs
from common import AUDIO_EXTENSIONS, KEEP_REMIXES_MARKER, is_excluded, scan_stale_staging_dirs
from intake import list_incoming, publish_incoming
from lastfm import _lastfm_artist_albums, _lastfm_artist_popular_albums
from metadata import find_named_dir, slug
from process_file import process_file, scan_filename_prefixes
from runtime import dedupe_navidrome_media_files, find_duplicate_navidrome_tracks, trigger_navidrome_rescan
from scan_duplicates import scan_duplicates
from scan_formats import scan_mixed_formats
from scan_singles import scan_singles
from scan_tracks import scan_track_numbers
from scan_variants import scan_variants

APP_DIR = Path(__file__).resolve().parent


def scan(root: Path, fix: bool, check_enc: bool, check_art: bool, check_alb: bool) -> int:
    count = scan_stale_staging_dirs(root, fix)
    if count:
        print(f"\nStale staging paths: {count} {'removed' if fix else 'found'}")

    if check_art:
        count = scan_filename_prefixes(root, fix)
        if count:
            print(f"\nFilename prefix mismatches: {count} {'fixed' if fix else 'found'}")

    if check_alb:
        count = scan_dirs(root, fix)
        if count:
            print(f"\nDirectories: {count} {'renamed' if fix else 'to rename'}")
        count = scan_album_years(root, fix)
        if count:
            print(f"\nYear mismatches: {count} album(s) {'fixed' if fix else 'found'}")

    found = 0
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        if is_excluded(directory):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if not is_excluded(directory / name)
        ]
        for filename in sorted(filenames):
            if Path(filename).suffix.casefold() not in AUDIO_EXTENSIONS:
                continue
            found += 1
            process_file(
                Path(dirpath) / filename, fix, check_enc, check_art, check_alb,
                library_root=root,
            )
    print(f"\nScanned: {found} files")
    print(f"Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")
    return found


def run_full_cleanup(root: Path, fix: bool, lastfm_key: str) -> None:
    """Run structural, per-file, then collection-wide cleanup in dependency order."""
    count = scan_stale_staging_dirs(root, fix)
    if count:
        print(f"\nStale staging paths: {count} {'removed' if fix else 'found'}")

    count = scan_nested_track_dirs(root, fix)
    if count:
        print(f"\nNested track paths: {count} {'flattened' if fix else 'to flatten'}")

    count = scan_dirs(root, fix)
    if count:
        print(f"\nDirectories: {count} {'renamed' if fix else 'to rename'}")

    # Resolve provisional Singles paths before deriving album tags from paths.
    scan_singles(root, fix, lastfm_key)

    count = scan_filename_prefixes(root, fix)
    if count:
        print(f"\nFilename prefix mismatches: {count} {'fixed' if fix else 'found'}")

    count = scan_mixed_formats(root, fix)
    if count:
        print(f"\nMixed-format albums: {count} {'transcoded' if fix else 'found'}")

    # scan() owns the common per-file normalization. Directory stages are disabled
    # here because they have already run against the final layout.
    found = 0
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        if is_excluded(directory):
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if not is_excluded(directory / name)]
        for filename in sorted(filenames):
            if Path(filename).suffix.casefold() not in AUDIO_EXTENSIONS:
                continue
            found += 1
            process_file(
                Path(dirpath) / filename, fix, True, True, True,
                library_root=root,
            )
    print(f"\nScanned: {found} files")
    print(f"Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")

    count = scan_album_years(root, fix)
    if count:
        print(f"\nYear mismatches: {count} album(s) {'fixed' if fix else 'found'}")
    scan_duplicates(root, fix)
    scan_variants(root, fix)
    scan_track_numbers(root, fix, lastfm_key)

    # Fixed directly via NAVIDROME_DB_PATH when mounted (SQLite's own locking
    # makes this safe without stopping the service); otherwise a read-only
    # report via the Subsonic API with manual-fix instructions.
    db_duplicates = dedupe_navidrome_media_files(fix)
    if db_duplicates is not None:
        if db_duplicates:
            print(f"\nNavidrome catalog duplicates: {len(db_duplicates)} path(s) with multiple entries")
            for path, count in db_duplicates:
                print(f"  [!] {path}  ({count} entries)")
            print(f"  Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")
    else:
        duplicates = find_duplicate_navidrome_tracks()
        if duplicates:
            print(f"\nNavidrome catalog duplicates: {len(duplicates)} path(s) with multiple entries")
            for path, ids in duplicates:
                print(f"  [!] {path}  ({len(ids)} entries: {', '.join(ids)})")
            print(
                "  Fix: stop the navidrome container, then in its sqlite DB run:\n"
                "    DELETE FROM media_file WHERE id NOT IN\n"
                "      (SELECT MIN(id) FROM media_file GROUP BY path, folder_id);\n"
                "  then restart navidrome and trigger a rescan."
            )


def _run_downloader(root: Path, artist: str, album: str, key: str) -> bool:
    result = subprocess.run([
        sys.executable,
        str(APP_DIR / "download_music.py"),
        "--album", artist, album,
        "--out", str(root),
        "--lastfm-key", key,
    ])
    return result.returncode == 0


def _navidrome_dupes_command(fix: bool) -> int:
    duplicates = dedupe_navidrome_media_files(fix)
    if duplicates is None:
        print("ERROR: NAVIDROME_DB_PATH not configured/mounted - can't reach Navidrome's database")
        return 1
    if not duplicates:
        print("No Navidrome catalog duplicates found")
        return 0
    print(f"Navidrome catalog duplicates: {len(duplicates)} path(s) with multiple entries")
    for path, count in duplicates:
        print(f"  [!] {path}  ({count} entries)")
    print(f"Mode: {'FIX applied' if fix else 'DRY-RUN (use --fix to apply changes)'}")
    return 0


def _download_command(args, root: Path) -> int:
    artist = args.download_album[0]
    failures = []
    if args.all_albums:
        studios, singles = _lastfm_artist_popular_albums(artist, args.lastfm_key)
        if not studios and not singles:
            print(f"No popular albums found for '{artist}'")
            return 1
        artist_dir = find_named_dir(root, artist, fuzzy=True)
        existing = {
            slug(directory.name)
            for directory in artist_dir.iterdir()
            if directory.is_dir()
        } if artist_dir else set()
        albums = [name for name in studios + singles if slug(name) not in existing]
        print(
            f"'{artist}': {len(studios)} studio + {len(singles)} EP/singles; "
            f"{len(albums)} to download"
        )
    else:
        if len(args.download_album) < 2:
            albums = _lastfm_artist_albums(artist, args.lastfm_key)
            if not albums:
                print(f"No albums found for '{artist}'")
                return 1
            for index, album in enumerate(albums, 1):
                print(f"  {index:>3}. {album}")
            return 0
        albums = [" ".join(args.download_album[1:])]

    successes = 0
    for album in albums:
        print(f"\n→ Downloading: {artist} — {album}")
        if not _run_downloader(root, artist, album, args.lastfm_key):
            failures.append(album)
        else:
            successes += 1
    scan_ok = True
    if successes:
        scan_ok, message = trigger_navidrome_rescan("prepare-download")
        print(f"Navidrome: {message}")
    if failures:
        print(f"\nFailed albums ({len(failures)}): {', '.join(failures)}")
        return 1
    return 0 if scan_ok else 1


def _intake_command(args, root: Path) -> int:
    incoming = Path(args.ingest)
    if not incoming.is_dir():
        print(f"ERROR: incoming directory not found: {incoming}")
        return 1
    if not args.fix:
        entries = list_incoming(incoming)
        for entry in entries:
            if entry["status"] == "ready":
                print(
                    f"[READY] {entry['name']} → {entry['relative_destination']} "
                    f"(artists: {', '.join(entry['artists'])})"
                )
            else:
                print(f"[ERROR] {entry['name']}: {entry['error']}")
        print(f"Incoming: {len(entries)} file(s); use --fix to publish")
        return 1 if any(entry["status"] == "error" for entry in entries) else 0

    results = publish_incoming(incoming, root, names=args.name or None)
    for result in results:
        if result["status"] == "error":
            print(f"[ERROR] {result['name']}: {result['error']}")
        else:
            print(f"[{result['status'].upper()}] {result['name']} → {result['destination']}")
    failed = sum(result["status"] == "error" for result in results)
    if not failed and results:
        ok, message = trigger_navidrome_rescan("prepare-intake")
        print(f"Navidrome: {message}")
        if not ok:
            return 1
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Music library directory or audio file")
    parser.add_argument("--fix", action="store_true", help="Apply changes")
    parser.add_argument("--encoding-only", action="store_true")
    parser.add_argument("--artists-only", action="store_true")
    parser.add_argument("--album-only", action="store_true")
    parser.add_argument("--variants-only", action="store_true")
    parser.add_argument("--formats-only", action="store_true")
    parser.add_argument("--tracknums-only", action="store_true")
    parser.add_argument("--singles-only", action="store_true")
    parser.add_argument("--navidrome-dupes-only", action="store_true")
    parser.add_argument(
        "--keep-remixes", action="store_true",
        help="mark 'path' (an album directory) so cleanup keeps its remix tracks",
    )
    parser.add_argument(
        "--unset-keep-remixes", action="store_true",
        help="unmark 'path' (an album directory) so cleanup goes back to always "
             "dropping its remix tracks",
    )
    parser.add_argument(
        "--lastfm-key",
        default=os.environ.get("LASTFM_KEY") or os.environ.get("LASTFM_APIKEY", ""),
    )
    parser.add_argument("--download-album", nargs="+", metavar="ARG")
    parser.add_argument("--all-albums", action="store_true")
    parser.add_argument("--list-albums", metavar="ARTIST")
    parser.add_argument("--ingest", metavar="DIR", help="Inspect/publish staged uploads")
    parser.add_argument("--name", action="append", help="Publish only this staged filename")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.path)
    if not root.exists():
        print(f"ERROR: path not found: {root}")
        return 1

    if args.keep_remixes or args.unset_keep_remixes:
        if not root.is_dir():
            print(f"ERROR: not an album directory: {root}")
            return 1
        marker = root / KEEP_REMIXES_MARKER
        if args.keep_remixes:
            marker.touch(exist_ok=True)
            print(f"Marked: {root} will keep its remix tracks")
        else:
            marker.unlink(missing_ok=True)
            print(f"Unmarked: {root} remixes are cleaned up as usual")
        return 0
    if args.ingest:
        return _intake_command(args, root)
    if args.list_albums:
        albums = _lastfm_artist_albums(args.list_albums, args.lastfm_key)
        for index, album in enumerate(albums, 1):
            print(f"  {index:>3}. {album}")
        return 0 if albums else 1
    if args.download_album:
        return _download_command(args, root)
    if args.navidrome_dupes_only:
        return _navidrome_dupes_command(args.fix)

    selected = any((
        args.encoding_only, args.artists_only, args.album_only,
        args.variants_only, args.tracknums_only, args.singles_only, args.formats_only,
    ))
    run_all = not selected
    check_enc = run_all or args.encoding_only
    check_art = run_all or args.artists_only
    check_alb = run_all or args.album_only

    print(f"Scanning: {root}")
    print(f"Mode: {'FIX' if args.fix else 'DRY-RUN'}")
    if root.is_file():
        process_file(root, args.fix, check_enc, check_art, check_alb)
    elif run_all:
        try:
            run_full_cleanup(root, args.fix, args.lastfm_key)
        except AlbumMergeError as exc:
            print(f"\nERROR: {exc}")
            return 1
    elif args.singles_only:
        scan_singles(root, args.fix, args.lastfm_key)
    elif args.variants_only:
        scan_variants(root, args.fix)
    elif args.formats_only:
        scan_mixed_formats(root, args.fix)
    elif args.tracknums_only:
        scan_track_numbers(root, args.fix, args.lastfm_key)
    else:
        try:
            scan(root, args.fix, check_enc, check_art, check_alb)
        except AlbumMergeError as exc:
            print(f"\nERROR: {exc}")
            return 1

    if args.fix:
        ok, message = trigger_navidrome_rescan("prepare-fix")
        print(f"Navidrome: {message}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
