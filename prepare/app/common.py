import contextlib
import hashlib
import os
import re as _re
import shutil
import time
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".wav", ".m4b"}

EXCLUDE_DIRS = {"All", "All-Rap", "Garazh", "ReverseDungeon", "Classics", "TexnoFunk"}

# A process killed before it could clean up its own .album-*/.download-*/.lock-*
# path (container restart, SIGKILL) is the one failure mode both
# scan_stale_staging_dirs and staging_lock guard against - one constant so the
# "how long is too long to wait" answer stays the same in both places.
STALE_STAGING_MAX_AGE = 1800

_FORMAT_PRIORITY = {'.flac': 0, '.wav': 1, '.ape': 2, '.m4a': 3, '.mp3': 4,
                    '.aac': 5, '.ogg': 6, '.opus': 7, '.wma': 8}

KEEP_REMIXES_MARKER = ".keep-remixes"
PLAYLIST_FOLDER_MARKER = ".playlist"

_INVALID_COMPONENT = _re.compile(r"[\\/\x00-\x1f]")


def keeps_remixes(album_dir: Path) -> bool:
    """True if this album folder is marked to keep its remix tracks -
    scan_variants/scan_duplicates otherwise always treat a remix as a
    disposable variant of its original."""
    return (album_dir / KEEP_REMIXES_MARKER).exists()


def is_playlist_folder(folder: Path) -> bool:
    """True if this folder is marked as a loose collection of unrelated
    singles (e.g. anime OP/ED themes grouped by series) rather than one
    physical album - like the built-in EXCLUDE_DIRS playlist folders, its
    name is forced onto `album` but each track's own artist/albumartist and
    year are left alone, and it's skipped entirely by year/track-number
    normalization, which otherwise assume every file in a folder belongs to
    the same release."""
    return (folder / PLAYLIST_FOLDER_MARKER).exists()


def safe_component(value: str, fallback: str) -> str:
    """Return a filesystem-safe single path component."""
    cleaned = _INVALID_COMPONENT.sub("_", value or "").strip().strip(".")
    return cleaned or fallback

def file_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_content_hash(path: Path) -> str:
    """Hash audio content, ignoring mutable ID3v2/ID3v1 data in MP3 files."""
    if path.suffix.casefold() != ".mp3":
        return file_content_hash(path)
    size = path.stat().st_size
    start, end = 0, size
    with path.open("rb") as stream:
        header = stream.read(10)
        if len(header) == 10 and header[:3] == b"ID3":
            tag_size = ((header[6] & 0x7f) << 21 | (header[7] & 0x7f) << 14
                        | (header[8] & 0x7f) << 7 | (header[9] & 0x7f))
            start = min(size, 10 + tag_size)
        if size >= 128:
            stream.seek(-128, os.SEEK_END)
            if stream.read(3) == b"TAG":
                end -= 128
        stream.seek(start)
        digest = hashlib.sha256()
        remaining = max(0, end - start)
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()

def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS or part.startswith(".") for part in path.parts)


def scan_stale_staging_dirs(root: Path, fix: bool, max_age_seconds: float = STALE_STAGING_MAX_AGE) -> int:
    """Find (and, if fix, remove) leftover .album-*/.download-*/.lock-* paths from
    a download that was killed before its own cleanup could run. is_excluded()
    already keeps every OTHER scan from touching dot-prefixed paths, so nothing
    ever reports these - they just sit there as real audio files a library
    scanner (Navidrome, not this codebase) can still index as phantom albums.
    """
    now = time.time()
    found = 0
    for pattern in (".album-*", ".download-*", ".lock-*"):
        for stale_path in root.rglob(pattern):
            try:
                age = now - stale_path.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            found += 1
            rel = stale_path.relative_to(root)
            print(f"  [!] stale staging path: {rel} ({age/60:.0f}m old)")
            if fix:
                try:
                    if stale_path.is_dir():
                        shutil.rmtree(stale_path)
                    else:
                        stale_path.unlink()
                    print(f"      [deleted]")
                except OSError as exc:
                    print(f"      [ERROR] {exc}")
    return found


@contextlib.contextmanager
def staging_lock(lock_path: Path, max_age_seconds: float = STALE_STAGING_MAX_AGE):
    """Exclusive marker-file lock; steals a lock older than max_age_seconds,
    left by a process that died before releasing it. Raises FileExistsError if
    a live process still holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock_path.stat().st_mtime if lock_path.exists() else 0
        if age <= max_age_seconds:
            raise
        with contextlib.suppress(OSError):
            lock_path.unlink()
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()
