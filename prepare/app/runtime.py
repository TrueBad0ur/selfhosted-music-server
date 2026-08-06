"""Runtime integration helpers shared by CLI and Web."""

import json
import os
import sqlite3
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _navidrome_request(endpoint: str, params: dict, client: str = "music-tools", timeout: float = 15) -> dict:
    base_url = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
    username = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not username or not password:
        return {}
    query = urllib.parse.urlencode({
        **params, "u": username, "p": password, "v": "1.16.1", "c": client, "f": "json",
    })
    with urllib.request.urlopen(f"{base_url}/rest/{endpoint}?{query}", timeout=timeout) as response:
        return json.loads(response.read()).get("subsonic-response", {})


def find_duplicate_navidrome_tracks() -> list[tuple[str, list[str]]]:
    """Find media_file rows that share the exact same on-disk path within an
    album - Navidrome should never have more than one, but overlapping rescans
    fired close together right after a fresh publish can race each other into
    inserting the same path twice (each row is individually valid - the file it
    points at genuinely exists - so a normal rescan doesn't notice or merge
    them; the fix is a direct, careful DB edit while Navidrome is stopped, not
    something this tool does automatically as a side effect of a routine scan).
    """
    try:
        album_ids: list[str] = []
        offset = 0
        while True:
            data = _navidrome_request(
                "getAlbumList2", {"type": "alphabeticalByName", "size": 500, "offset": offset}
            )
            albums = data.get("albumList2", {}).get("album", [])
            if not albums:
                break
            album_ids.extend(album["id"] for album in albums)
            offset += len(albums)
            if len(albums) < 500:
                break
    except Exception:
        return []

    def _album_duplicates(album_id: str) -> list[tuple[str, list[str]]]:
        try:
            data = _navidrome_request("getAlbum", {"id": album_id})
        except Exception:
            return []
        songs = data.get("album", {}).get("song", [])
        by_path: dict[str, list[str]] = {}
        for song in songs:
            path = song.get("path", "")
            if path:
                by_path.setdefault(path, []).append(song.get("id", ""))
        return [(path, ids) for path, ids in by_path.items() if len(ids) > 1]

    duplicates: list[tuple[str, list[str]]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(_album_duplicates, album_ids):
            duplicates.extend(result)
    return duplicates


def dedupe_navidrome_media_files(fix: bool = False) -> list[tuple[str, int]] | None:
    """Detect (and, if fix, remove) media_file rows sharing the same (path,
    folder_id) - keeps the lowest id, matching the SQL used to fix this by hand
    the first time this happened. Returns None if NAVIDROME_DB_PATH isn't mounted
    (this feature is opt-in - it needs read-write access to Navidrome's own
    database file, not just its HTTP API).

    Runs via Python's sqlite3 with a generous busy_timeout instead of stopping
    the navidrome container: SQLite's WAL mode is explicitly designed to let a
    second process write safely while another connection (Navidrome itself) is
    open on the same file - the busy_timeout just makes this process wait for
    the lock instead of failing immediately if Navidrome is mid-write.
    """
    db_path = os.environ.get("NAVIDROME_DB_PATH", "")
    if not db_path or not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        duplicates = conn.execute(
            "SELECT path, COUNT(*) FROM media_file GROUP BY path, folder_id HAVING COUNT(*) > 1"
        ).fetchall()
        if fix and duplicates:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "DELETE FROM media_file WHERE id NOT IN "
                "(SELECT MIN(id) FROM media_file GROUP BY path, folder_id)"
            )
            conn.commit()
        return duplicates
    finally:
        conn.close()


def trigger_navidrome_rescan(client: str = "music-tools") -> tuple[bool, str]:
    if not os.environ.get("NAVIDROME_USER") or not os.environ.get("NAVIDROME_PASSWORD"):
        return False, "NAVIDROME_USER/NAVIDROME_PASSWORD are not configured"
    try:
        response = _navidrome_request("startScan", {}, client=client, timeout=10)
        if response.get("status") != "ok":
            return False, json.dumps({"subsonic-response": response})
        return True, "scan requested"
    except Exception as exc:
        return False, str(exc)

