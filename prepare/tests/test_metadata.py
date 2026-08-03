import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metadata
from checks import strip_watermarks
from common import is_excluded


class MetadataTests(unittest.TestCase):
    def test_slug_and_transliteration_match(self):
        self.assertEqual(metadata.slug("2020 - Hello, World!"), "helloworld")
        variants = metadata.title_variants("На чёрный день")
        self.assertIn("nachyornyyden", variants)
        self.assertIn("nachyornyjden", variants)
        self.assertIn("nachyornyiden", variants)

    def test_title_variants_ignore_known_media_and_credit_suffixes(self):
        expected = metadata.slug("Come Dream With Me")
        self.assertIn(expected, metadata.title_variants("Come Dream With Me (Official Track)"))
        paradise = metadata.slug("Gangsta's Paradise")
        self.assertNotIn(paradise, metadata.title_variants("Gangsta's Paradise (feat. L.V.)"))
        self.assertIn(paradise, metadata.relaxed_title_variants("Gangsta's Paradise (feat. L.V.)"))

    def test_title_variants_match_abbreviation_expansion_and_karaoke_mix(self):
        self.assertTrue(
            metadata.title_variants("ЧП (Четверо парней)")
            & metadata.title_variants("Четверо парней")
        )
        self.assertTrue(
            metadata.title_variants("Если хочешь остаться (караоке микс)")
            & metadata.title_variants("Если хочешь остаться (караоке)")
        )

    def test_feature_credit_is_not_mistaken_for_domain_watermark(self):
        self.assertEqual(
            strip_watermarks("Hodokete,Ambivalence (feat.ACCAMER)"),
            "Hodokete,Ambivalence (feat.ACCAMER)",
        )
        self.assertEqual(strip_watermarks("Track (spam.example)"), "Track")

    def test_find_named_dir_handles_year_and_case(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = root / "Artist"
            expected.mkdir()
            self.assertEqual(metadata.find_named_dir(root, "artist"), expected)

    def test_popular_filter_is_shared_and_deterministic(self):
        entries = [
            {"name": "Studio One", "playcount": 10000, "images": []},
            {"name": "Small", "playcount": 499, "images": []},
            {"name": "Live in Town", "playcount": 9000, "images": []},
            {"name": "Song - Single", "playcount": 1000, "images": []},
        ]
        with patch("metadata.artist_album_entries", return_value=entries):
            studios, singles = metadata.popular_album_entries("Artist", "key")
        self.assertEqual([item["name"] for item in studios], ["Studio One"])
        self.assertEqual([item["name"] for item in singles], ["Song - Single"])

    def test_album_info_falls_back_to_musicbrainz(self):
        with patch("metadata.lastfm_request", side_effect=[{"album": {"name": "A", "artist": "X"}}, {"results": {}}]), \
             patch("metadata.musicbrainz_album_info", return_value={"tracks": ["One", "Two"], "durations": [60, 70], "year": "2020"}):
            info = metadata.album_info("X", "A", "key")
        self.assertEqual(info["tracks"], ["One", "Two"])
        self.assertEqual(info["year"], "2020")

    def test_musicbrainz_uses_earliest_release_year_when_detail_falls_back(self):
        search = {
            "releases": [
                {
                    "id": "original", "title": "Album", "date": "2006",
                    "artist-credit": [{"name": "Artist"}],
                },
                {
                    "id": "reissue", "title": "Album", "date": "2016-08-05",
                    "artist-credit": [{"name": "Artist"}],
                },
            ],
        }
        reissue = {
            "media": [{"tracks": [{"position": 1, "title": "Track", "length": 60000}]}],
        }
        with patch("metadata._json_request", side_effect=[search, {}, reissue]), \
             patch("metadata.time.sleep"):
            info = metadata.musicbrainz_album_info("Artist", "Album")
        self.assertEqual(info["tracks"], ["Track"])
        self.assertEqual(info["year"], "2006")

    def test_deezer_album_follows_all_track_pages(self):
        search = {"data": [{"id": 42, "title": "Album", "artist": {"name": "Artist"}}]}
        detail = {
            "release_date": "2021-01-01", "cover_xl": "cover", "nb_tracks": 2,
            "tracks": {
                "data": [{"id": 1, "title": "One", "duration": 60, "disk_number": 1, "track_position": 1}],
            },
        }
        page_two = {
            "data": [
                {"id": 1, "title": "One", "duration": 60, "disk_number": 1, "track_position": 1},
                {"id": 2, "title": "Two", "duration": 70, "disk_number": 1, "track_position": 2},
            ],
        }
        with patch("metadata._json_request", side_effect=[search, detail, page_two]):
            info = metadata.deezer_album_info("Artist", "Album")
        self.assertEqual(info["tracks"], ["One", "Two"])
        self.assertEqual(info["durations"], [60.0, 70.0])

    def test_verified_album_requires_catalog_consensus(self):
        base = {
            "artist": "Artist", "name": "Album", "year": "2020",
            "tracks": ["One", "Two"], "cover_url": "", "error": None,
            "catalogs": {"lastfm": ["One", "Two"], "musicbrainz": ["One", "Two", "Three"]},
            "catalog_durations": {"lastfm": [], "musicbrainz": [60, 70, 80]},
        }
        itunes = {"tracks": ["One", "Two"], "durations": [61, 71], "year": "2019", "cover_url": "itunes-cover"}
        deezer = {"tracks": [], "durations": [], "year": "", "cover_url": ""}
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=itunes), \
             patch("metadata.deezer_album_info", return_value=deezer), \
             patch("metadata.fourwords_album_info", return_value=deezer):
            info = metadata.verified_album_info("Artist", "Album", "key")
        self.assertEqual(info["tracks"], ["One", "Two"])
        self.assertEqual(info["verified_by"], ["lastfm", "itunes"])
        self.assertEqual(info["year"], "2019")
        self.assertEqual(info["cover_url"], "itunes-cover")
        self.assertEqual(info["track_durations"], [61, 71])

    def test_verified_album_reports_conflicting_catalogs(self):
        base = {
            "artist": "Artist", "name": "Album", "year": "",
            "tracks": ["One", "Two"], "cover_url": "", "error": None,
            "catalogs": {"lastfm": ["One", "Two"], "musicbrainz": ["Other"]},
            "catalog_durations": {"lastfm": [], "musicbrainz": [60]},
        }
        itunes = {"tracks": ["Different", "Edition", "Here"], "durations": [1, 2, 3], "year": "", "cover_url": ""}
        deezer = {"tracks": [], "durations": [], "year": "", "cover_url": ""}
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=itunes), \
             patch("metadata.deezer_album_info", return_value=deezer), \
             patch("metadata.fourwords_album_info", return_value=deezer):
            info = metadata.verified_album_info("Artist", "Album", "key")
        self.assertEqual(info["tracks"], [])
        self.assertIn("verification failed", info["error"])

    def test_verified_album_allows_catalogs_to_omit_feature_credits(self):
        base = {
            "artist": "Auram", "name": "Lost Time", "year": "",
            "tracks": ["Burn (feat. Wrenn)", "Find You", "Lost Time (feat. Beau Young Prince)"],
            "cover_url": "", "error": None,
            "catalogs": {
                "lastfm": ["Burn (feat. Wrenn)", "Find You", "Lost Time (feat. Beau Young Prince)"],
            },
            "catalog_durations": {"lastfm": []},
        }
        itunes = {"tracks": [], "durations": [], "year": "", "cover_url": ""}
        deezer = {
            "tracks": ["Burn", "Find You", "Lost Time"],
            "durations": [224, 208, 200], "year": "2016", "cover_url": "cover",
        }
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=itunes), \
             patch("metadata.deezer_album_info", return_value=deezer), \
             patch("metadata.fourwords_album_info", return_value=itunes):
            info = metadata.verified_album_info("Auram", "Lost Time", "key")
        self.assertEqual(info["verified_by"], ["lastfm", "deezer"])
        self.assertEqual(info["tracks"], base["tracks"])
        self.assertEqual(info["track_durations"], [224, 208, 200])

    def test_fourwords_album_info_parses_exact_album(self):
        search = """
        <a href='https://www.4words.ru/album/5632'>
          <div class='viewSearch__title'><p>Иголка</p></div>
          <div class='viewSearch__subtitle'><p>Виктория Дайнеко &bull; 2008</p></div>
        </a>
        """
        detail = """
        <meta property="og:title" content="Виктория Дайнеко — Иголка (альбом)" />
        <meta property="og:image" content="https://example.test/cover.jpg" />
        <div>дата выпуска:</div><div class="albumHeader-info__right"><p>6 Марта 2008</p></div>
        <div class="tracklist__position"><p>1</p></div>
        <div class="tracklist__songTitle"><p><a href="#">Иголка</a></p></div>
        <div class="tracklist__duration"><p>03:43</p></div>
        <div class="tracklist__position"><p>2</p></div>
        <div class="tracklist__songTitle"><p>Что теряю я</p></div>
        <div class="tracklist__duration"><p>03:12</p></div>
        """
        with patch("metadata._text_request", side_effect=[search, detail]):
            info = metadata.fourwords_album_info("Виктория Дайнеко", "Иголка")
        self.assertEqual(info["tracks"], ["Иголка", "Что теряю я"])
        self.assertEqual(info["durations"], [223, 192])
        self.assertEqual(info["year"], "2008")

    def test_verified_album_uses_fourwords_as_independent_catalog(self):
        tracks = ["Иголка", "Что теряю я"]
        base = {
            "artist": "Виктория Дайнеко", "name": "Иголка", "year": "",
            "tracks": tracks, "cover_url": "", "error": None,
            "catalogs": {"lastfm": tracks, "musicbrainz": []},
            "catalog_durations": {"lastfm": [], "musicbrainz": []},
        }
        empty = {"tracks": [], "durations": [], "year": "", "cover_url": ""}
        fourwords = {
            "tracks": tracks, "durations": [223, 192], "year": "2008", "cover_url": "cover",
        }
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=empty), \
             patch("metadata.deezer_album_info", return_value=empty), \
             patch("metadata.fourwords_album_info", return_value=fourwords):
            info = metadata.verified_album_info("Виктория Дайнеко", "Иголка", "key")
        self.assertEqual(info["verified_by"], ["lastfm", "fourwords"])
        self.assertEqual(info["year"], "2008")
        self.assertEqual(info["track_durations"], [223, 192])

    def test_verified_album_prefers_one_track_supersequence(self):
        short = ["One", "Two", "Four"]
        complete = ["One", "Two", "Inserted", "Four"]
        base = {
            "artist": "Artist", "name": "Album", "year": "2006",
            "tracks": short, "cover_url": "", "error": None,
            "catalogs": {"lastfm": short, "musicbrainz": complete},
            "catalog_durations": {
                "lastfm": [], "musicbrainz": [60, 70, 80, 90],
            },
        }
        empty = {"tracks": [], "durations": [], "year": "", "cover_url": ""}
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=empty), \
             patch("metadata.deezer_album_info", return_value=empty), \
             patch("metadata.fourwords_album_info", return_value=empty):
            info = metadata.verified_album_info("Artist", "Album", "key")
        self.assertEqual(info["verified_by"], ["lastfm", "musicbrainz"])
        self.assertEqual(info["tracks"], complete)
        self.assertEqual(info["track_durations"], [60, 70, 80, 90])

    def test_verified_album_keeps_per_track_artist_credits(self):
        tracks = ["Solo", "Collaboration"]
        base = {
            "artist": "Alpha", "name": "Album", "year": "2024",
            "tracks": tracks, "cover_url": "", "error": None,
            "catalogs": {"lastfm": tracks, "musicbrainz": tracks},
            "catalog_durations": {"lastfm": [], "musicbrainz": [60, 70]},
            "catalog_artists": {
                "lastfm": [["Alpha"], ["Alpha"]],
                "musicbrainz": [["Alpha"], ["Alpha", "Beta"]],
            },
        }
        empty = {"tracks": [], "durations": [], "artists": [], "year": "", "cover_url": ""}
        with patch("metadata.album_info", return_value=base), \
             patch("metadata.itunes_album_info", return_value=empty), \
             patch("metadata.deezer_album_info", return_value=empty), \
             patch("metadata.fourwords_album_info", return_value=empty):
            info = metadata.verified_album_info("Alpha", "Album", "key")
        self.assertEqual(info["track_artists"], [["Alpha"], ["Alpha", "Beta"]])

    def test_track_metadata_requires_two_catalogs_and_merges_artists(self):
        itunes = [{
            "source": "itunes", "title": "IVL", "artists": ["MACAN", "SCIRENA"],
            "album": "IVL", "duration": 200, "cover_url": "cover", "year": "2023",
        }]
        deezer = [{
            "source": "deezer", "title": "IVL", "artists": ["MACAN"],
            "album": "IVL", "duration": 200, "cover_url": "", "year": "",
        }]
        with patch("metadata._itunes_track_candidates", return_value=itunes), \
             patch("metadata._deezer_track_candidates", return_value=deezer), \
             patch("metadata._musicbrainz_track_candidates", return_value=[]):
            info = metadata.resolve_track_metadata(["MACAN"], "IVL", 200)
        self.assertEqual(info["artists"], ["MACAN", "SCIRENA"])
        self.assertEqual(info["album"], "IVL")
        self.assertEqual(info["verified_by"], ["itunes", "deezer"])

    def test_bypass_and_hidden_directories_are_excluded(self):
        self.assertTrue(is_excluded(Path("/music/All/All")))
        self.assertTrue(is_excluded(Path("/music/Artist/Album/.download-123")))
        self.assertFalse(is_excluded(Path("/music/Artist/Album")))



if __name__ == "__main__":
    unittest.main()
