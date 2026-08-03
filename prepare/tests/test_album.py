import tempfile
import unittest
from pathlib import Path

from album import scan_dirs


class AlbumDirectoryMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artist = self.root / "Artist"
        self.artist.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_identical_audio_is_removed_only_after_hash_match(self):
        destination = self.artist / "Album"
        source = self.artist / "2025 - Album (Deluxe Edition)"
        destination.mkdir()
        source.mkdir()
        payload = b"not-tags-same-audio"
        (destination / "track.mp3").write_bytes(payload)
        (source / "track.mp3").write_bytes(payload)

        self.assertEqual(scan_dirs(self.root, True), 1)
        self.assertFalse(source.exists())
        self.assertEqual((destination / "track.mp3").read_bytes(), payload)

    def test_different_audio_with_same_name_is_preserved(self):
        destination = self.artist / "Album"
        source = self.artist / "2025 - Album (Deluxe Edition)"
        destination.mkdir()
        source.mkdir()
        (destination / "track.mp3").write_bytes(b"original-audio")
        (source / "track.mp3").write_bytes(b"deluxe-audio")

        self.assertEqual(scan_dirs(self.root, True), 1)
        self.assertFalse(source.exists())
        self.assertEqual((destination / "track.mp3").read_bytes(), b"original-audio")
        alternates = [path for path in destination.glob("track *.mp3") if path.name != "track.mp3"]
        self.assertEqual(len(alternates), 1)
        self.assertEqual(alternates[0].read_bytes(), b"deluxe-audio")

    def test_dry_run_does_not_modify_directories(self):
        source = self.artist / "2024 - Album"
        source.mkdir()
        self.assertEqual(scan_dirs(self.root, False), 1)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
