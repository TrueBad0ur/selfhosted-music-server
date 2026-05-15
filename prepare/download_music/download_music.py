#!/usr/bin/env python3
"""
Music downloader: Last.fm metadata + yt-dlp download backend.

Usage:
  python3 download_music.py --artist "Rammstein" --list-albums --lastfm-key KEY
  python3 download_music.py --album "Rammstein" "Mutter" --out /music --lastfm-key KEY
  python3 download_music.py --album "Rammstein" "Mutter" --dest /music/Rammstein/Mutter --lastfm-key KEY
  python3 download_music.py --track "Rammstein" "Du hast" --out /tmp --lastfm-key KEY

Options:
  --artist ARTIST           List artist albums from Last.fm
  --album ARTIST ALBUM      Download full album (all missing tracks)
  --track ARTIST TITLE      Search and download one track
  --out DIR                 Output directory (default: ./downloads)
  --dest DIR                Exact destination folder for --album (skips auto subdir creation)
  --delay SECONDS           Delay between requests (default: 0.5)
  --lastfm-key KEY          Last.fm API key (or set LASTFM_KEY env var)
  --dry-run                 Show what would be downloaded without actually downloading
  --list-albums             With --artist: only list albums, don't download
"""

import argparse
import subprocess
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TDRC
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    print("[WARN] mutagen not installed — tags won't be fixed. Run: pip install mutagen")

LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}

# ── Last.fm ────────────────────────────────────────────────────────────────────

def lastfm(method, params, key):
    p = {"method": method, "api_key": key, "format": "json", **params}
    url = LASTFM_BASE + "?" + urllib.parse.urlencode(p)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def lastfm_artist_albums(artist, key, delay=0.5):
    data = lastfm("artist.getTopAlbums", {"artist": artist, "limit": 50, "autocorrect": "1"}, key)
    albums = data.get("topalbums", {}).get("album", [])
    time.sleep(delay)
    return [{"name": a["name"], "mbid": a.get("mbid", "")} for a in albums
            if a["name"] not in ("[unknown]", "")]

def lastfm_album_info(artist, album, key, delay=0.5):
    data = lastfm("album.getInfo", {"artist": artist, "album": album, "autocorrect": "1"}, key)
    time.sleep(delay)
    alb = data.get("album", {})
    tracks = alb.get("tracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    return {
        "name": alb.get("name"),
        "artist": alb.get("artist"),
        "year": (lambda s: m.group() if (m := re.search(r"\b(19|20)\d{2}\b", s)) else "")(alb.get("wiki", {}).get("published", "") or ""),
        "tracks": [t["name"] for t in tracks],
    }

def lastfm_track_info(artist, title, key, delay=0.5):
    data = lastfm("track.getInfo", {"artist": artist, "track": title, "autocorrect": "1"}, key)
    time.sleep(delay)
    track = data.get("track", {})
    album = track.get("album", {})
    return {
        "title": track.get("name", title),
        "artist": track.get("artist", {}).get("name", artist),
        "album": album.get("title"),
    }

# ── Tags ───────────────────────────────────────────────────────────────────────

def fix_tags(path, artist=None, title=None, album=None, year=None):
    if not HAS_MUTAGEN:
        return
    try:
        f = MutagenFile(str(path), easy=False)
        if f is None:
            return
        t = type(f).__name__
        changed = False

        if t == "MP3":
            if f.tags is None:
                from mutagen.id3 import ID3
                f.add_tags()
            for key in list(f.tags.keys()):
                if key.startswith(("WOAS", "WOAR", "WOAF", "COMM")):
                    del f.tags[key]
                    changed = True
            if artist: f.tags["TPE1"] = TPE1(encoding=3, text=[artist]); changed = True
            if title:  f.tags["TIT2"] = TIT2(encoding=3, text=[title]);  changed = True
            if album:  f.tags["TALB"] = TALB(encoding=3, text=[album]);  changed = True
            if year:   f.tags["TDRC"] = TDRC(encoding=0, text=[year]);   changed = True

        elif t == "FLAC":
            if artist: f["artist"] = [artist]; changed = True
            if title:  f["title"]  = [title];  changed = True
            if album:  f["album"]  = [album];  changed = True
            if year:   f["date"]   = [year];   changed = True

        if changed:
            f.save()
    except Exception as e:
        print(f"  [WARN] tag fix failed: {e}")

# ── yt-dlp ─────────────────────────────────────────────────────────────────────

def ytdlp_download(artist, title, dest_path, dry_run=False):
    """Search YouTube and download track as mp3 via yt-dlp."""
    query = f"{artist} {title}"
    if dry_run:
        print(f"  [DRY] would search yt-dlp: {query}")
        return True
    dest_path = Path(dest_path)
    if dest_path.exists():
        print(f"  [SKIP] already exists: {dest_path.name}")
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = str(dest_path.parent / "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        f"ytsearch5:{query}",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
        "--output", tmp_out,
        "--add-metadata", "--no-playlist",
        "--match-filter", "duration < 600",
        "--max-downloads", "1",
        "--quiet", "--no-warnings",
    ]
    existing_mp3s = set(dest_path.parent.glob("*.mp3"))
    print(f"  ↓ {dest_path.name}", end="", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        new_mp3s = [f for f in dest_path.parent.glob("*.mp3") if f not in existing_mp3s and f != dest_path]
        if new_mp3s:
            newest = max(new_mp3s, key=lambda f: f.stat().st_mtime)
            newest.rename(dest_path)
        if dest_path.exists():
            print(f"  ({dest_path.stat().st_size // 1024} KB)")
            return True
        print(f"  [ERROR] not found after download")
        if result.stderr:
            print(f"    {result.stderr[:200]}")
        return False
    except subprocess.TimeoutExpired:
        print(f"  [ERROR] timeout")
        return False
    except FileNotFoundError:
        print(f"  [ERROR] yt-dlp not found — install with: pip install yt-dlp")
        return False

# ── Commands ───────────────────────────────────────────────────────────────────

def _slug_norm(s):
    return re.sub(r'[^\w]', '', s.lower())

def cmd_list_artist(args):
    print(f"\nSearching Last.fm for artist: {args.artist}")
    albums = lastfm_artist_albums(args.artist, args.lastfm_key, args.delay)
    if not albums:
        print("  No albums found.")
        return
    print(f"  Found {len(albums)} albums:")
    for i, a in enumerate(albums, 1):
        print(f"  {i:2}. {a['name']}")

def cmd_download_album(args):
    key = args.lastfm_key
    out = Path(args.out)
    AUDIO = {'.mp3', '.flac', '.m4a', '.ogg', '.opus'}

    print(f'\nLooking up album: {args.artist} — {args.album}')
    info = lastfm_album_info(args.artist, args.album, key, args.delay)
    if not info.get('name'):
        print('  Album not found on Last.fm.')
        return

    album_name = info['name']
    artist_name = info['artist']
    year = info.get('year', '')
    lfm_tracks = info['tracks']
    folder_name = f'{year} - {album_name}' if year else album_name
    dest_dir = Path(args.dest) if args.dest else out / artist_name / folder_name

    print(f'  Album: {album_name} ({year}) by {artist_name}')
    print(f'  Tracks: {len(lfm_tracks)}')
    print(f'  Destination: {dest_dir}')
    if args.dry_run:
        for t in lfm_tracks:
            print(f'    [DRY] {t}')
        return

    dest_dir.mkdir(parents=True, exist_ok=True)

    def disk_set():
        return {_slug_norm(f.stem.split(' - ', 1)[-1])
                for f in dest_dir.iterdir() if f.suffix.lower() in AUDIO}

    ok = 0
    existing = disk_set()
    for track_title in lfm_tracks:
        if _slug_norm(track_title) in existing:
            print(f'  [SKIP] {track_title}')
            continue
        dest = dest_dir / f'{artist_name} - {track_title}.mp3'
        if ytdlp_download(artist_name, track_title, dest, args.dry_run):
            fix_tags(dest, artist=artist_name, title=track_title, album=album_name, year=year)
            ok += 1
        time.sleep(args.delay)
    print(f'\nDownloaded: {ok}/{len(lfm_tracks)} tracks → {dest_dir}')

def cmd_download_track(args):
    key = args.lastfm_key
    out = Path(args.out)

    print(f"\nSearching: {args.artist} — {args.title}")
    info = lastfm_track_info(args.artist, args.title, key, args.delay)
    print(f"  Title:  {info.get('title', args.title)}")
    print(f"  Artist: {info.get('artist', args.artist)}")
    print(f"  Album:  {info.get('album', 'unknown')}")

    dest = out / f"{info.get('artist', args.artist)} - {info.get('title', args.title)}.mp3"
    if ytdlp_download(info.get('artist', args.artist), info.get('title', args.title), dest, args.dry_run):
        fix_tags(dest, artist=info.get('artist'), title=info.get('title'), album=info.get('album'))

# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Last.fm metadata + yt-dlp music downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--artist", metavar="ARTIST")
    parser.add_argument("--album", nargs=2, metavar=("ARTIST", "ALBUM"))
    parser.add_argument("--track", nargs=2, metavar=("ARTIST", "TITLE"))
    parser.add_argument("--out", default="./downloads", metavar="DIR")
    parser.add_argument("--dest", default="", metavar="DIR")
    parser.add_argument("--delay", type=float, default=0.5, metavar="SEC")
    parser.add_argument("--lastfm-key", default=os.environ.get("LASTFM_KEY", ""), metavar="KEY")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-albums", action="store_true")

    args = parser.parse_args()

    if not args.lastfm_key and (args.artist or args.album or args.track):
        parser.error("--lastfm-key required (or set LASTFM_KEY env var)")

    if args.artist and not args.album and not args.track:
        cmd_list_artist(args)
    elif args.album:
        args.artist, args.album = args.album
        cmd_download_album(args)
    elif args.track:
        args.artist, args.title = args.track
        cmd_download_track(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
