import subprocess
import tempfile
import unittest
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3

from album import scan_album_years
from common import PLAYLIST_FOLDER_MARKER
from process_file import process_file


def _make_track(path: Path, title: str, year: str, artist: str | None = "Some Singer"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.05", "-q:a", "9", str(path),
    ], check=True)
    tags = EasyID3(path)
    tags["title"] = [title]
    if artist is not None:
        tags["artist"] = [artist]
    tags["date"] = [year]
    tags.save()


class PlaylistFolderTests(unittest.TestCase):
    def test_album_forced_to_folder_name_without_touching_albumartist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "SomeAnime"
            path = folder / "[AniTousen] TV ED01 - Song Title (Some Singer).mp3"
            _make_track(path, "Song Title", "2020")
            (folder / PLAYLIST_FOLDER_MARKER).touch()

            process_file(path, True, False, False, True, library_root=root)

            media = MutagenFile(path, easy=False)
            self.assertEqual(str(media.tags.get("TALB")), "SomeAnime")
            self.assertIsNone(media.tags.get("TPE2"))

    def test_year_normalization_skips_marked_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "SomeAnime"
            _make_track(folder / "a.mp3", "One", "2010")
            _make_track(folder / "b.mp3", "Two", "2020")
            (folder / PLAYLIST_FOLDER_MARKER).touch()

            fixed = scan_album_years(root, True)

            self.assertEqual(fixed, 0)
            self.assertEqual(str(MutagenFile(folder / "a.mp3").tags.get("TDRC")), "2010")
            self.assertEqual(str(MutagenFile(folder / "b.mp3").tags.get("TDRC")), "2020")

    def test_missing_artist_guessed_from_trailing_parens_not_root_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "SomeAnime"
            path = folder / "[AniTousen] TV OP01 - Akeboshi (LiSA).mp3"
            _make_track(path, "Akeboshi", "2021", artist=None)
            (folder / PLAYLIST_FOLDER_MARKER).touch()

            process_file(path, True, False, True, True, library_root=root)

            media = MutagenFile(path, easy=False)
            self.assertEqual(str(media.tags.get("TPE1")), "LiSA")


if __name__ == "__main__":
    unittest.main()
