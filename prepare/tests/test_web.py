import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mutagen.easyid3 import EasyID3

ROOT = Path(__file__).resolve().parents[2]
WEB_FILE = ROOT / "application" / "web" / "web.py"


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        os.environ.update({
            "MUSIC_DIR": str(root / "music"),
            "INCOMING_DIR": str(root / "incoming"),
            "PREPARE_APP": str(ROOT / "prepare" / "app"),
            "WEB_USERNAME": "",
            "WEB_PASSWORD": "",
            "LASTFM_KEY": "test",
        })
        (root / "music").mkdir()
        (root / "incoming").mkdir()
        spec = importlib.util.spec_from_file_location("music_web_test", WEB_FILE)
        cls.web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.web)
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.web._executor.shutdown(wait=True)
        cls.web._analysis_executor.shutdown(wait=True)
        cls.temp.cleanup()

    def test_health(self):
        response = self.client.get("/tools/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_download_validates_payload(self):
        self.assertEqual(self.client.post("/tools/api/download", json={}).status_code, 400)
        resolved = {
            "tracks": ["One"], "error": None, "selection_required": False,
            "selected_source": "lastfm",
        }
        with patch.object(self.web, "verified_album_info", return_value=resolved),              patch.object(self.web, "_submit", return_value="job") as submit:
            response = self.client.post(
                "/tools/api/download", json={"artist": "A", "album": "B"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job_id"], "job")
        submit.assert_called_once_with(
            "A — B", self.web._run_download, "A", "B", "lastfm"
        )

    def test_download_returns_catalog_choices_and_accepts_selection(self):
        conflict = {
            "tracks": [], "error": "catalog selection required",
            "selection_required": True,
            "catalog_choices": {
                "lastfm": ["One", "Two"],
                "deezer": ["First", "Second", "Third"],
            },
        }
        with patch.object(self.web, "verified_album_info", return_value=conflict):
            response = self.client.post(
                "/tools/api/download", json={"artist": "A", "album": "B"}
            )
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertTrue(payload["selection_required"])
        self.assertEqual(payload["catalogs"][0]["tracks"], ["One", "Two"])

        selected = {
            "tracks": ["First", "Second", "Third"], "error": None,
            "selection_required": False, "selected_source": "deezer",
        }
        with patch.object(self.web, "verified_album_info", return_value=selected),              patch.object(self.web, "_submit", return_value="job") as submit:
            response = self.client.post(
                "/tools/api/download",
                json={"artist": "A", "album": "B", "catalog_source": "deezer"},
            )
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once_with(
            "A — B", self.web._run_download, "A", "B", "deezer"
        )

    def test_download_worker_keeps_key_out_of_arguments(self):
        with patch.object(self.web, "_run_command") as runner:
            self.web._run_download("job", "Artist", "Album")
        command = runner.call_args.args[1]
        self.assertNotIn("--lastfm-key", command)
        self.assertNotIn(self.web.LASTFM_KEY, command)

    def test_failed_mutation_does_not_trigger_rescan(self):
        process = MagicMock()
        process.stdout = io.StringIO("failed\n")
        process.returncode = 1
        with patch("subprocess.Popen", return_value=process), \
             patch.object(self.web, "trigger_navidrome_rescan") as rescan, \
             patch.object(self.web, "_finish"):
            self.web._run_command("job", ["false"], rescan=True)
        rescan.assert_not_called()

    def test_prepare_rejects_multiple_scopes(self):
        response = self.client.post(
            "/tools/api/prepare/fix",
            json={"encoding_only": True, "artists_only": True},
        )
        self.assertEqual(response.status_code, 400)

    def test_incoming_path_traversal_is_rejected(self):
        response = self.client.get("/tools/api/incoming/..%2Fsecret.mp3")
        self.assertEqual(response.status_code, 404)

    def test_upload_rejects_non_audio(self):
        from io import BytesIO
        response = self.client.post(
            "/tools/api/upload",
            data={"file": (BytesIO(b"no"), "bad.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["staged"][0]["status"], "error")

    def test_disk_tracks_use_tag_number_instead_of_999(self):
        album = self.web.MUSIC_DIR / "Artist" / "Single Album"
        album.mkdir(parents=True)
        path = album / "Artist - Only Track.mp3"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05", "-q:a", "9", str(path),
        ], check=True)
        tags = EasyID3(path)
        tags["title"] = ["Only Track"]
        tags["tracknumber"] = ["1"]
        tags.save()

        tracks, _ = self.web._disk_titles(album)
        self.assertEqual(tracks, [(1, "Only Track")])

    def test_disk_tracks_without_number_get_sequential_fallback(self):
        album = self.web.MUSIC_DIR / "Artist" / "Fallback Album"
        album.mkdir(parents=True)
        for title in ("First", "Second"):
            path = album / f"Artist - {title}.mp3"
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.05", "-q:a", "9", str(path),
            ], check=True)
            tags = EasyID3(path)
            tags["title"] = [title]
            tags.save()

        tracks, _ = self.web._disk_titles(album)
        self.assertEqual([number for number, _ in tracks], [1, 2])


if __name__ == "__main__":
    unittest.main()
