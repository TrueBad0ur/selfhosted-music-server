import re as _re
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".wav", ".m4b"}

EXCLUDE_DIRS = {"All", "All-Rap", "Garazh", "ReverseDungeon", "Classics", "TexnoFunk"}

_FORMAT_PRIORITY = {'.flac': 0, '.wav': 1, '.ape': 2, '.m4a': 3, '.mp3': 4,
                    '.aac': 5, '.ogg': 6, '.opus': 7, '.wma': 8}

def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in path.parts)
