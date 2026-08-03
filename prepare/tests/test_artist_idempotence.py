import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from mutagen import File as MutagenFile

from process_file import process_file
from tags import get_tags


class ArtistIdempotenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.album = Path(self.temp.name) / "Initial D" / "Compilation" / "CD1"
        self.album.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _flac(self) -> Path:
        path = self.album / "01. John Desire - Chemical Love.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["John Desire"]
        media["albumartist"] = ["Initial D"]
        media["album"] = ["Compilation CD1"]
        media["title"] = ["Chemical Love"]
        media.save()
        return path

    def test_apply_then_preview_is_idempotent_for_multivalue_flac_artist(self):
        path = self._flac()
        process_file(path, True, False, False, True)
        media = MutagenFile(path)
        self.assertEqual(media.get("artist"), ["John Desire", "Initial D"])
        self.assertEqual(get_tags(media)["artist"], "John Desire; Initial D")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, False, True)
        self.assertEqual(output.getvalue(), "")

    def test_existing_native_multivalue_artist_needs_no_fix(self):
        path = self._flac()
        media = MutagenFile(path)
        media["artist"] = ["John Desire", "Initial D"]
        media.save()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, True, True)
        self.assertNotIn("multi-artist", output.getvalue())
        self.assertNotIn("artist:", output.getvalue())

    def test_case_only_duplicate_is_removed_and_not_readded_by_albumartist(self):
        album = Path(self.temp.name) / "MERCENARY" / "First Breath"
        album.mkdir(parents=True)
        path = album / "MERCENARY - Watching Me.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Mercenary", "MERCENARY"]
        media["albumartist"] = ["MERCENARY"]
        media["album"] = ["First Breath"]
        media["title"] = ["Watching Me"]
        media.save()

        process_file(path, True, False, True, True)
        self.assertEqual(MutagenFile(path).get("artist"), ["Mercenary"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, True, True)
        self.assertEqual(output.getvalue(), "")

    def test_album_title_fragments_are_not_kept_as_artists(self):
        album_name = "Work One; Work Two"
        album = Path(self.temp.name) / "Conductor" / album_name
        album.mkdir(parents=True)
        path = album / "Conductor - Movement.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Conductor", "Work One", "Work Two"]
        media["albumartist"] = [album_name]
        media["album"] = ["Wrong Nested Folder"]
        media["title"] = ["Movement"]
        media.save()

        process_file(
            path, True, False, True, True,
            library_root=Path(self.temp.name),
        )
        media = MutagenFile(path)
        self.assertEqual(media.get("artist"), ["Conductor"])
        self.assertEqual(media.get("albumartist"), ["Conductor"])
        self.assertEqual(media.get("album"), [album_name])


if __name__ == "__main__":
    unittest.main()
