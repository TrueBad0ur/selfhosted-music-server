import re as _re
from difflib import SequenceMatcher

from metadata import artist_album_entries, lastfm_request, popular_album_entries


def _title_slug(stem: str) -> str:
    """Normalise a filename stem to a comparable slug for Last.fm matching."""
    value = stem.split(" - ", 1)[1] if " - " in stem else stem
    value = _re.sub(r"^\d+[\s.\-]+", "", value)
    return _re.sub(r"[^\w]", "", value.casefold())


def _lastfm_track_name(artist: str, track: str, api_key: str) -> str | None:
    """Return the corrected track name from Last.fm, or None on failure."""
    data = lastfm_request(
        "track.getInfo",
        {"artist": artist, "track": track, "autocorrect": "1"},
        api_key,
    )
    return data.get("track", {}).get("name") or None


def _lastfm_tracklist(artist: str, album: str, api_key: str) -> dict[str, tuple[int, str]]:
    """Return {title_slug: (rank, original_name)} from Last.fm."""
    data = lastfm_request(
        "album.getInfo",
        {"artist": artist, "album": album, "autocorrect": "1"},
        api_key,
    )
    tracks = data.get("album", {}).get("tracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    result = {}
    for fallback_rank, track in enumerate(tracks, 1):
        name = track.get("name", "")
        rank = track.get("@attr", {}).get("rank", fallback_rank)
        if name and str(rank).isdigit():
            result[_re.sub(r"[^\w]", "", name.casefold())] = (int(rank), name)
    return result


def _match_to_tracklist(
    slug: str, tracklist: dict[str, tuple[int, str]]
) -> tuple[int, str] | None:
    """Return (rank, name) for the best track-list match."""
    entry = tracklist.get(slug)
    if entry:
        return entry
    for remote_slug, (rank, name) in tracklist.items():
        if remote_slug and slug and (remote_slug in slug or slug in remote_slug):
            return rank, name
    best_ratio, best_entry = 0.0, None
    for remote_slug, entry in tracklist.items():
        if remote_slug:
            ratio = SequenceMatcher(None, slug, remote_slug).ratio()
            if ratio > best_ratio:
                best_ratio, best_entry = ratio, entry
    return best_entry if best_ratio >= 0.82 else None


def _lastfm_artist_albums(artist: str, api_key: str) -> list[str]:
    return [
        entry["name"]
        for entry in artist_album_entries(artist, api_key, limit=100)
    ]


def _lastfm_artist_popular_albums(
    artist: str, api_key: str, studio_limit: int = 15
) -> tuple[list[str], list[str]]:
    """Return canonical popular studio album and single/EP names."""
    studios, singles = popular_album_entries(artist, api_key, studio_limit)
    return (
        [entry["name"] for entry in studios],
        [entry["name"] for entry in singles],
    )
