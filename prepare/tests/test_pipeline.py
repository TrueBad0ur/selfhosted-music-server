import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen import File as MutagenFile

from album import scan_nested_track_dirs
from prepare_music import run_full_cleanup
from process_file import process_file


class FullCleanupPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.singles = self.root / "Artist" / "Singles"
        self.singles.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _single(self) -> Path:
        path = self.singles / "Artist - Track.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Artist"]
        media["albumartist"] = ["Artist"]
        media["album"] = ["Singles"]
        media["title"] = ["Track"]
        media.save()
        return path

    def test_single_is_relocated_before_final_metadata_and_second_preview_is_clean(self):
        self._single()
        no_collection_issues = (
            patch("prepare_music.scan_album_years", return_value=0),
            patch("prepare_music.scan_duplicates", return_value=0),
            patch("prepare_music.scan_variants", return_value=0),
            patch("prepare_music.scan_track_numbers", return_value=0),
            patch("scan_singles._lastfm_track_name", return_value="Track"),
            patch("scan_singles.time.sleep", return_value=None),
        )
        with no_collection_issues[0], no_collection_issues[1], no_collection_issues[2], \
             no_collection_issues[3], no_collection_issues[4], no_collection_issues[5]:
            run_full_cleanup(self.root, True, "key")
            destination = self.root / "Artist" / "Track" / "Artist - Track.flac"
            self.assertTrue(destination.is_file())
            self.assertFalse(self.singles.exists())
            media = MutagenFile(destination)
            self.assertEqual(media.get("album"), ["Track"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                run_full_cleanup(self.root, False, "key")
            self.assertNotIn("[!]", output.getvalue())

    def test_accidental_title_subdirectory_is_flattened_but_disc_directory_is_kept(self):
        album = self.root / "Artist" / "Album"
        broken = album / "Artist - Prescription" / "Oxymoron.flac"
        broken.parent.mkdir(parents=True)
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(broken),
        ], check=True)
        media = MutagenFile(broken)
        media["artist"] = ["Artist"]
        media["album"] = ["Album"]
        media["title"] = ["Prescription/Oxymoron"]
        media.save()
        disc = album / "LP1" / "01 - Track.mp3"
        disc.parent.mkdir()
        disc.write_bytes(b"disc-audio")

        self.assertEqual(scan_nested_track_dirs(self.root, True), 1)
        flattened = album / "Artist - Prescription⧸Oxymoron.flac"
        self.assertTrue(flattened.is_file())
        self.assertFalse(broken.parent.exists())
        self.assertTrue(disc.is_file())

    def test_album_may_have_same_name_as_artist(self):
        album = self.root / "Paramore" / "Paramore"
        album.mkdir(parents=True)
        path = album / "Paramore - Still Into You.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Paramore"]
        media["albumartist"] = ["Paramore"]
        media["album"] = ["Paramore"]
        media["title"] = ["Still Into You"]
        media.save()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, True, True, library_root=self.root)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(scan_nested_track_dirs(self.root, False), 0)

    def test_ambiguous_nested_album_is_not_rewritten(self):
        nested = self.root / "Artist" / "Outer" / "Inner"
        nested.mkdir(parents=True)
        path = nested / "Artist - Track.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Artist"]
        media["albumartist"] = ["Artist"]
        media["album"] = ["Inner"]
        media["title"] = ["Track"]
        media.save()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, True, True, library_root=self.root)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(scan_nested_track_dirs(self.root, False), 0)

    def test_disc_album_name_is_preserved(self):
        disc = self.root / "Artist" / "Album Part A" / "Album (CD 1)"
        disc.mkdir(parents=True)
        path = disc / "01 - Track.flac"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", str(path),
        ], check=True)
        media = MutagenFile(path)
        media["artist"] = ["Artist"]
        media["albumartist"] = ["Artist"]
        media["album"] = ["Album (CD 1)"]
        media["title"] = ["Track"]
        media.save()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process_file(path, False, False, True, True, library_root=self.root)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
