import subprocess
import tempfile
import unittest
from pathlib import Path

from common import KEEP_REMIXES_MARKER
from scan_variants import scan_variants


def _make_track(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.05", "-q:a", "9", str(path),
    ], check=True)


class VariantRemixTests(unittest.TestCase):
    def test_remix_dropped_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album = root / "Artist" / "Album"
            _make_track(album / "Artist - Song.mp3")
            _make_track(album / "Artist - Song (Remix).mp3")

            scan_variants(root, True)

            remaining = sorted(p.name for p in album.glob("*.mp3"))
            self.assertEqual(remaining, ["Artist - Song.mp3"])

    def test_remix_kept_with_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album = root / "Artist" / "Album"
            _make_track(album / "Artist - Song.mp3")
            _make_track(album / "Artist - Song (Remix).mp3")
            (album / KEEP_REMIXES_MARKER).touch()

            scan_variants(root, True)

            remaining = sorted(p.name for p in album.glob("*.mp3"))
            self.assertEqual(remaining, ["Artist - Song (Remix).mp3", "Artist - Song.mp3"])

    def test_non_remix_variant_still_dropped_with_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album = root / "Artist" / "Album"
            _make_track(album / "Artist - Song.mp3")
            _make_track(album / "Artist - Song (Live).mp3")
            (album / KEEP_REMIXES_MARKER).touch()

            scan_variants(root, True)

            remaining = sorted(p.name for p in album.glob("*.mp3"))
            self.assertEqual(remaining, ["Artist - Song.mp3"])


if __name__ == "__main__":
    unittest.main()
