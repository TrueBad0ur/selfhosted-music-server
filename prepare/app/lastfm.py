import json
import re as _re
import urllib.request
import urllib.parse
from difflib import SequenceMatcher


def _title_slug(stem: str) -> str:
    """Normalise a filename stem to a comparable slug for Last.fm matching."""
    s = stem
    if ' - ' in s:
        s = s.split(' - ', 1)[1]
    s = _re.sub(r'^\d+[\s.\-]+', '', s)
    return _re.sub(r'[^\w]', '', s.lower())


def _lastfm_track_name(artist: str, track: str, api_key: str) -> str | None:
    """Return the corrected track name from Last.fm (autocorrect), or None on failure."""
    params = urllib.parse.urlencode({
        "method": "track.getInfo",
        "api_key": api_key,
        "artist": artist,
        "track": track,
        "autocorrect": "1",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{params}", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode())
        return data.get("track", {}).get("name") or None
    except Exception:
        return None


def _lastfm_tracklist(artist: str, album: str, api_key: str) -> dict[str, tuple[int, str]]:
    """Return {title_slug: (rank, original_name)} from Last.fm, or {} on failure."""
    params = urllib.parse.urlencode({
        "method": "album.getInfo",
        "api_key": api_key,
        "artist": artist,
        "album": album,
        "autocorrect": "1",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{params}", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return {}
    tracks = data.get("album", {}).get("tracks", {}).get("track", [])
    if not tracks:
        return {}
    if isinstance(tracks, dict):
        tracks = [tracks]
    result = {}
    for tr in tracks:
        name = tr.get("name", "")
        rank = tr.get("@attr", {}).get("rank")
        if name and rank:
            result[_re.sub(r'[^\w]', '', name.lower())] = (int(rank), name)
    return result


def _match_to_tracklist(slug: str, tracklist: dict[str, tuple[int, str]]) -> tuple[int, str] | None:
    """Return (rank, name) for the best match in tracklist, or None."""
    entry = tracklist.get(slug)
    if entry:
        return entry
    for lfm_slug, (rank, name) in tracklist.items():
        if lfm_slug and slug and (lfm_slug in slug or slug in lfm_slug):
            return (rank, name)
    best_ratio, best_entry = 0.0, None
    for lfm_slug, (rank, name) in tracklist.items():
        if not lfm_slug:
            continue
        r = SequenceMatcher(None, slug, lfm_slug).ratio()
        if r > best_ratio:
            best_ratio, best_entry = r, (rank, name)
    if best_ratio >= 0.82:
        return best_entry
    return None


def _lastfm_artist_albums(artist: str, api_key: str) -> list[str]:
    params = urllib.parse.urlencode({
        "method": "artist.getTopAlbums",
        "artist": artist,
        "limit": "100",
        "autocorrect": "1",
        "api_key": api_key,
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{params}", timeout=10
        ) as resp:
            data = json.loads(resp.read())
        albums = data.get("topalbums", {}).get("album", [])
        return [a["name"] for a in albums if a["name"] not in ("[unknown]", "")]
    except Exception as e:
        print(f"  [Last.fm error] {e}")
        return []
