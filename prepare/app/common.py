import hashlib
import os
import re as _re
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".wav", ".m4b"}

EXCLUDE_DIRS = {"All", "All-Rap", "Garazh", "ReverseDungeon", "Classics", "TexnoFunk"}

_FORMAT_PRIORITY = {'.flac': 0, '.wav': 1, '.ape': 2, '.m4a': 3, '.mp3': 4,
                    '.aac': 5, '.ogg': 6, '.opus': 7, '.wma': 8}

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
