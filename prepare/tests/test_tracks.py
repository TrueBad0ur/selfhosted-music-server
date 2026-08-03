import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen import File as MutagenFile

from scan_tracks import scan_track_numbers


class TrackNumberTests(unittest.TestCase):
    def test_missing_track_is_reported_but_never_downloaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            path = album / "Artist - Track One.flac"
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.05", str(path),
            ], check=True)
            media = MutagenFile(path)
            media["artist"] = ["Artist"]
            media["album"] = ["Album"]
            media["title"] = ["Track One"]
            media["tracknumber"] = ["9"]
            media.save()

            tracklist = {
                "trackone": (1, "Track One"),
                "tracktwo": (2, "Track Two"),
            }
            with patch("scan_tracks._lastfm_tracklist", return_value=tracklist), \
                 patch("scan_tracks.time.sleep", return_value=None):
                scan_track_numbers(root, True, "key")

            self.assertEqual(list(album.glob("*.flac")), [path])
            self.assertEqual(MutagenFile(path).get("tracknumber"), ["1"])


if __name__ == "__main__":
    unittest.main()
