import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.easyid3 import EasyID3

from intake import list_incoming, publish_incoming


class IntakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.incoming = root / "incoming"
        self.music = root / "music"
        self.incoming.mkdir()
        self.music.mkdir()
        self.resolver = patch("intake.resolve_track_metadata", return_value={})
        self.resolver.start()

    def tearDown(self):
        self.resolver.stop()
        self.temp.cleanup()

    def _audio(self, name="source.mp3"):
        path = self.incoming / name
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.15",
            "-q:a", "9", str(path),
        ], check=True)
        tags = EasyID3(path)
        tags["artist"] = ["Alpha feat. Beta"]
        tags["albumartist"] = ["Alpha"]
        tags["album"] = ["Demo Album"]
        tags["title"] = ["Test / Track"]
        tags.save()
        return path

    def test_clean_publish_routes_and_splits_artists(self):
        source = self._audio()
        entries = list_incoming(self.incoming)
        self.assertEqual(entries[0]["status"], "ready")
        self.assertEqual(entries[0]["artists"], ["Alpha", "Beta"])

        results = publish_incoming(self.incoming, self.music)
        self.assertEqual(results[0]["status"], "published")
        destination = self.music / results[0]["destination"]
        self.assertTrue(destination.is_file())
        self.assertFalse(source.exists())
        self.assertEqual(destination.parts[-3:-1], ("Alpha", "Demo Album"))
        tags = EasyID3(destination)
        self.assertEqual(tags["albumartist"], ["Alpha"])
        self.assertEqual(tags["album"], ["Demo Album"])

    def test_bypass_preserves_file_and_routes_to_excluded_folder(self):
        source = self._audio("raw.mp3")
        original = source.read_bytes()
        results = publish_incoming(self.incoming, self.music, bypass=True)
        destination = self.music / results[0]["destination"]
        self.assertEqual(destination.parts[-3:-1], ("All", "All"))
        self.assertEqual(destination.read_bytes(), original)

    def test_duplicate_is_not_copied_twice(self):
        first = self._audio("one.mp3")
        duplicate = self.incoming / "two.mp3"
        shutil.copy2(first, duplicate)
        first_result = publish_incoming(self.incoming, self.music, names=["one.mp3"])
        second_result = publish_incoming(self.incoming, self.music, names=["two.mp3"])
        self.assertEqual(first_result[0]["status"], "published")
        self.assertEqual(second_result[0]["status"], "duplicate")

    def test_suspicious_source_album_is_enriched_before_routing(self):
        source = self._audio("Alpha_Beta_-_Track_12345678.mp3")
        tags = EasyID3(source)
        tags["album"] = ["Alpha_Beta_-_Track_12345678"]
        tags.save()
        resolved = {
            "title": "Track", "artists": ["Alpha", "Beta"], "album": "Track",
            "year": "2024", "cover_url": "https://cover.test/image.jpg",
            "verified_by": ["itunes", "deezer"],
        }
        with patch("intake.resolve_track_metadata", return_value=resolved), \
             patch("intake._download_cover", return_value=(b"jpeg", "image/jpeg")):
            result = publish_incoming(self.incoming, self.music)[0]
        destination = self.music / result["destination"]
        self.assertEqual(destination.parts[-3:-1], ("Alpha", "Track"))
        tags = EasyID3(destination)
        self.assertEqual(tags["artist"], ["Alpha", "Beta"])
        self.assertEqual(tags["album"], ["Track"])


if __name__ == "__main__":
    unittest.main()
