import tempfile
import subprocess
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import download_music


class DownloadTests(unittest.TestCase):
    def test_safe_component_blocks_path_traversal(self):
        self.assertEqual(download_music.safe_component("../A/B", "x"), "_A_B")

    def test_download_isolated_and_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "Artist - Track.mp3"

            def fake_run(command, **kwargs):
                if "--dump-single-json" in command:
                    return MagicMock(
                        returncode=0,
                        stdout=(
                            '{"entries":[{"id":"official-id","title":"Track",'
                            '"channel":"Artist","uploader":"Artist"}]}'
                        ),
                        stderr="",
                    )
                self.assertEqual(command[1], "https://www.youtube.com/watch?v=official-id")
                self.assertNotIn("--ignore-errors", command)
                self.assertIn("youtube:player_client=android,web", command)
                output = next(item for item in command if "%(id)s.%(ext)s" in item)
                temp_dir = Path(output).parent
                (temp_dir / "id.mp3").write_bytes(b"audio")
                return MagicMock(returncode=0, stderr="")

            with patch("download_music.subprocess.run", side_effect=fake_run):
                self.assertTrue(download_music.ytdlp_download("Artist", "Track", destination))
            self.assertEqual(destination.read_bytes(), b"audio")
            self.assertFalse(any(path.name.startswith(".download-") for path in destination.parent.iterdir()))

    def test_candidate_ranking_rejects_wrong_or_variant_video(self):
        wrong = {"title": "What is Apathy? How To Break Past It.", "channel": "Doctor"}
        wrong_artist = {"title": "What It's All About", "channel": "Somebody Else"}
        remix = {"title": "What It's All About (Remix)", "channel": "Apathy"}
        exact = {"title": "What It's All About", "channel": "Apathy"}
        long_track = {"title": "Long Track", "channel": "Artist", "duration": 1200}
        self.assertLess(download_music._youtube_candidate_score("Artist", "Long Track", long_track), 0)
        self.assertGreater(
            download_music._youtube_candidate_score("Artist", "Long Track", long_track, 1201),
            0,
        )
        short_lyric = {"title": "GRAI - Доня (lyric video)", "channel": "GRAI"}
        combined = {"title": "GRAI - Марево + Песня мертвой воды", "channel": "GRAI", "duration": 447}
        self.assertLess(download_music._youtube_candidate_score("Apathy", "What It's All About", wrong), 0)
        self.assertLess(download_music._youtube_candidate_score("Apathy", "What It's All About", wrong_artist), 0)
        self.assertLess(download_music._youtube_candidate_score("Apathy", "What It's All About", remix), 0)
        self.assertGreater(download_music._youtube_candidate_score("Apathy", "What It's All About", exact), 0)
        self.assertGreater(download_music._youtube_candidate_score("Грай", "Доня", short_lyric), 0)
        self.assertLess(download_music._youtube_candidate_score("Грай", "Марево", combined, 124), 0)
        self.assertGreater(
            download_music._youtube_candidate_score(
                "Дискотека Авария", "Дискотека Авария",
                {"title": "Дискотека Авария", "channel": "Дискотека Авария"},
            ),
            0,
        )
        self.assertLess(
            download_music._youtube_candidate_score(
                "Дискотека Авария", "Дискотека Авария",
                {"title": "Что стало с группой Дискотека Авария"},
            ),
            0,
        )

    def test_topic_candidate_uses_detailed_artist_and_album_metadata(self):
        search = {
            "entries": [{
                "id": "topic-id", "title": "Track", "duration": 101,
                "channel": "Release - Topic",
            }],
        }
        detailed = {
            "id": "topic-id", "title": "Track", "track": "Track",
            "artist": "Artist, Guest", "artists": ["Artist", "Guest"],
            "album": "Album", "duration": 100, "channel": "Release - Topic",
        }

        def fake_run(command, **kwargs):
            payload = detailed if "-J" in command else search
            return MagicMock(
                returncode=0, stdout=__import__("json").dumps(payload), stderr="",
            )

        with patch("download_music.subprocess.run", side_effect=fake_run):
            candidates = download_music._youtube_candidates(
                "Artist", "Track", "Album", expected_duration=100,
            )
        self.assertEqual([item["id"] for item in candidates], ["topic-id"])
        self.assertEqual(candidates[0]["artist"], "Artist, Guest")

    def test_topic_candidate_rejects_wrong_detailed_album(self):
        search = {
            "entries": [{
                "id": "topic-id", "title": "Track", "duration": 100,
                "channel": "Release - Topic",
            }],
        }
        detailed = {
            "id": "topic-id", "title": "Track", "artist": "Artist",
            "album": "Different Album", "duration": 100,
            "channel": "Release - Topic",
        }

        def fake_run(command, **kwargs):
            payload = detailed if "-J" in command else search
            return MagicMock(
                returncode=0, stdout=__import__("json").dumps(payload), stderr="",
            )

        with patch("download_music.subprocess.run", side_effect=fake_run):
            candidates = download_music._youtube_candidates(
                "Artist", "Track", "Album", expected_duration=100,
            )
        self.assertEqual(candidates, [])

    def test_soundcloud_is_used_only_as_validated_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "Artist - Track.mp3"

            def fake_run(command, **kwargs):
                if "--dump-single-json" in command and command[1].startswith("ytsearch"):
                    return MagicMock(returncode=0, stdout='{"entries":[]}', stderr="")
                if "--dump-single-json" in command and command[1].startswith("https://music.youtube.com/search"):
                    return MagicMock(returncode=0, stdout='{"entries":[]}', stderr="")
                if "--dump-single-json" in command and command[1].startswith("scsearch"):
                    return MagicMock(
                        returncode=0,
                        stdout=(
                            '{"entries":[{"id":"sc-id","title":"Track",'
                            '"uploader":"Artist","webpage_url":"https://soundcloud.test/track"}]}'
                        ),
                        stderr="",
                    )
                self.assertEqual(command[1], "https://soundcloud.test/track")
                output = next(item for item in command if "%(id)s.%(ext)s" in item)
                (Path(output).parent / "id.mp3").write_bytes(b"audio")
                return MagicMock(returncode=0, stderr="")

            with patch("download_music.subprocess.run", side_effect=fake_run):
                self.assertTrue(download_music.ytdlp_download("Artist", "Track", destination))
            self.assertEqual(destination.read_bytes(), b"audio")

    def test_youtube_search_uses_album_context(self):
        def fake_run(command, **kwargs):
            entries = []
            if command[1].endswith("Artist Track Album"):
                entries = [{"id": "album-result", "title": "Track", "channel": "Artist"}]
            return MagicMock(returncode=0, stdout=__import__("json").dumps({"entries": entries}))

        with patch("download_music.subprocess.run", side_effect=fake_run):
            candidates = download_music._youtube_candidates("Artist", "Track", "Album")
        self.assertEqual([item["id"] for item in candidates], ["album-result"])

    def test_failed_download_returns_false(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("download_music.subprocess.run", return_value=MagicMock(returncode=1, stderr="failed")), \
             patch("download_music._zaycev_candidates", return_value=[]), \
             patch("download_music._pesnime_candidates", return_value=[]):
            self.assertFalse(
                download_music.ytdlp_download("Artist", "Track", Path(temp) / "track.mp3")
            )

    def test_zaycev_search_returns_only_exact_downloadable_full_track(self):
        search = {
            "tracksInfo": {
                "42": {
                    "track": "Track", "artistName": "Artist", "duration": "03:20",
                    "downloadEnabled": True,
                },
                "43": {
                    "track": "Track remix", "artistName": "Artist", "duration": "03:20",
                    "downloadEnabled": True,
                },
            },
        }
        def request(path, data=None):
            if path.startswith("pages/search/tracks"):
                return search
            if path == "track/filezmeta":
                return {"tracks": [{"download": "hash"}]}
            if path == "track/download/hash":
                return "https://cdn.test/track.mp3"
            return {}
        with patch("download_music._zaycev_request", side_effect=request):
            candidates = download_music._zaycev_candidates("Artist", "Track", expected_duration=200)
        self.assertEqual([item["id"] for item in candidates], ["42"])
        self.assertEqual(candidates[0]["_source"], "ZAYCEV.NET")

    def test_pesnime_search_returns_exact_long_track(self):
        page = r'''self.__next_f.push([1,"{\"list\":[{\"id\":6881393,\"artist\":\"Artist\",\"title\":\"Long Track\",\"version\":\"\",\"duration\":1201,\"bitrate\":320,\"download\":\"https://s1dw.pesni.me/audio.mp3\"}]}" ])'''
        with patch("download_music._pesnime_request", return_value=page):
            candidates = download_music._pesnime_candidates(
                "Artist", "Long Track", expected_duration=1200,
            )
        self.assertEqual([item["id"] for item in candidates], ["6881393"])
        self.assertEqual(candidates[0]["_source"], "PESNI.ME")

    def test_direct_audio_source_is_decoded_and_duration_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp3"
            destination = Path(temp) / "destination.mp3"
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.2", "-q:a", "9", str(source),
            ], check=True)
            payload = source.read_bytes()

            class Response(io.BytesIO):
                headers = {"Content-Length": str(len(payload))}
                def __enter__(self): return self
                def __exit__(self, *_args): self.close()

            candidate = {
                "id": "42", "title": "Track", "channel": "Artist",
                "_download_url": "https://cdn.test/track.mp3",
                "_source": "ZAYCEV.NET", "_direct_audio": True,
            }
            with patch("download_music._youtube_candidates", return_value=[]), \
                 patch("download_music._youtube_music_candidates", return_value=[]), \
                 patch("download_music._soundcloud_candidates", return_value=[]), \
                 patch("download_music._zaycev_candidates", return_value=[candidate]), \
                 patch("download_music.urllib.request.urlopen", return_value=Response(payload)):
                self.assertTrue(download_music.ytdlp_download("Artist", "Track", destination))
            self.assertGreater(destination.stat().st_size, 0)

    def test_cli_catalog_choice_prints_all_tracks_and_selects_number(self):
        choices = {
            "lastfm": ["One", "Two"],
            "deezer": ["First", "Second", "Third"],
        }
        stdin = MagicMock()
        stdin.isatty.return_value = True
        with patch("download_music.sys.stdin", stdin),              patch("builtins.input", return_value="2"),              patch("sys.stdout", new_callable=io.StringIO) as output:
            selected = download_music._choose_catalog_source(choices)
        self.assertEqual(selected, "deezer")
        text = output.getvalue()
        self.assertIn("[1] lastfm", text)
        self.assertIn("1. One", text)
        self.assertIn("[2] deezer", text)
        self.assertIn("3. Third", text)

    def test_album_is_published_only_after_every_track_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "Artist" / "Album"
            args = SimpleNamespace(
                out=temp, artist="Artist", album="Album", dest=str(destination),
                delay=0, dry_run=False, lastfm_key="key", allow_partial=False, keep_remixes=False,
            )
            info = {
                "artist": "Artist", "name": "Album", "year": "2020",
                "tracks": ["First", "Second"], "cover_url": "", "error": None,
            }

            def fake_download(_artist, _title, path, _dry_run=False, **_kwargs):
                self.assertTrue(path.parent.parent.parent.name.startswith(".album-"))
                path.write_bytes(b"audio")
                return True

            with patch("download_music.verified_album_info", return_value=info), \
                 patch("download_music.ytdlp_download", side_effect=fake_download), \
                 patch("download_music._write_canonical_tags", return_value=True):
                self.assertTrue(download_music.cmd_download_album(args))

            self.assertTrue((destination / "Artist - First.mp3").is_file())
            self.assertTrue((destination / "Artist - Second.mp3").is_file())
            self.assertFalse(any(path.name.startswith(".album-") for path in destination.parent.iterdir()))

    def test_failed_album_discards_all_staged_tracks(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "Artist" / "Album"
            args = SimpleNamespace(
                out=temp, artist="Artist", album="Album", dest=str(destination),
                delay=0, dry_run=False, lastfm_key="key", allow_partial=False, keep_remixes=False,
            )
            info = {
                "artist": "Artist", "name": "Album", "year": "",
                "tracks": ["First", "Second"], "cover_url": "", "error": None,
            }

            def fake_download(_artist, title, path, _dry_run=False, **_kwargs):
                if title == "Second":
                    return False
                path.write_bytes(b"audio")
                return True

            with patch("download_music.verified_album_info", return_value=info), \
                 patch("download_music.ytdlp_download", side_effect=fake_download), \
                 patch("download_music._write_canonical_tags", return_value=True):
                self.assertFalse(download_music.cmd_download_album(args))

            self.assertFalse(destination.exists())
            self.assertFalse(any(path.name.startswith(".album-") for path in destination.parent.iterdir()))

    def test_album_destination_is_already_cleanup_canonical(self):
        with tempfile.TemporaryDirectory() as temp:
            args = SimpleNamespace(
                out=temp, artist="Artist", album="Album", dest="",
                delay=0, dry_run=True, lastfm_key="key", allow_partial=False, keep_remixes=False,
            )
            info = {
                "artist": "Artist", "name": "Album", "year": "2024",
                "tracks": ["Track"], "track_durations": [60],
                "cover_url": "", "error": None, "verified_by": ["a", "b"],
            }
            with patch("download_music.verified_album_info", return_value=info), \
                 patch("download_music.ytdlp_download", return_value=True) as downloader:
                self.assertTrue(download_music.cmd_download_album(args))
            self.assertEqual(downloader.call_args.args[2].parent, Path(temp) / "Artist" / "Album")
            self.assertFalse((Path(temp) / "Artist" / "2024 - Album").exists())

    def test_extra_album_file_is_preserved_after_verified_tracklist(self):
        with tempfile.TemporaryDirectory() as temp:
            album = Path(temp)
            (album / "Artist - One.mp3").write_bytes(b"audio")
            (album / "Artist - Bonus.mp3").write_bytes(b"audio")
            with patch("download_music._write_canonical_tags", return_value=True) as writer:
                self.assertTrue(
                    download_music._normalize_complete_album(
                        album, ["One"], "Artist", "Album", "2020", None, ""
                    )
                )
            calls = {call.args[2]: call.args[5] for call in writer.call_args_list}
            self.assertEqual(calls, {"One": 1, "Bonus": 2})

    def test_cover_embedding_replaces_existing_cover(self):
        from mutagen.mp3 import MP3

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "track.mp3"
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=0.1",
                "-q:a", "9", str(path),
            ], check=True)
            cover = b"\xff\xd8\xff\xe0test-jpeg"
            self.assertTrue(download_music.embed_cover(path, cover))
            frames = MP3(path).tags.getall("APIC")
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].data, cover)

    def test_youtube_access_detects_bot_check(self):
        blocked = MagicMock(
            returncode=1,
            stderr="ERROR: [youtube] xyz: Sign in to confirm you're not a bot. Use --cookies...",
        )
        with patch("download_music.subprocess.run", return_value=blocked):
            ok, error = download_music.check_youtube_access()
        self.assertFalse(ok)
        self.assertIn("VPN", error)

    def test_youtube_access_ok_on_success(self):
        with patch("download_music.subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            ok, error = download_music.check_youtube_access()
        self.assertTrue(ok)
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
