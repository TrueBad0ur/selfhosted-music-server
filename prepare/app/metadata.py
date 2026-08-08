"""Shared metadata and library matching helpers used by CLI and Web."""

from __future__ import annotations

import json
import html
import re
import time
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "SelfhostedMusicServer/1.0"
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"

JUNK_ALBUM_RE = re.compile(
    r"\b(live|concert|unplugged|greatest\s*hits?|best\s*of|collection|anthology|"
    r"compilation|rarities|b[- ]?sides?|bootleg|mixtape|mix\s*tape|sessions?|"
    r"presents|soundtrack|tribute|remix(?:es|ed)?|demo(?:s)?|remaster(?:ed)?)\b",
    re.IGNORECASE,
)
SINGLE_EP_RE = re.compile(r"\b(single|ep|e\.p\.?)\b", re.IGNORECASE)
_COMPILATION_ALBUM_RE = re.compile(
    r"\b(сборник|избранное|антология|best\s*of|greatest\s*hits?|collection|"
    r"anthology|compilation)\b",
    re.IGNORECASE,
)
YEAR_PREFIX_RE = re.compile(r"^\d{4}\s*-\s*")
_TITLE_VARIANT_NOISE_RE = re.compile(
    r"\((?:official(?:\s+(?:track|audio|video))?|remaster(?:ed)?)[^)]*\)",
    re.IGNORECASE,
)
_TITLE_CREDIT_RE = re.compile(r"\((?:feat(?:uring)?\.?|ft\.?)\s*[^)]*\)", re.IGNORECASE)
_TITLE_KARAOKE_MIX_RE = re.compile(r"\b(караоке|karaoke)\s+(?:микс|mix)\b", re.IGNORECASE)
_DZH_DIGRAPH_RE = re.compile(r"дж", re.IGNORECASE)

_CYR_LAT = {
    "а":"a", "б":"b", "в":"v", "г":"g", "д":"d", "е":"e", "ё":"yo",
    "ж":"zh", "з":"z", "и":"i", "й":"y", "к":"k", "л":"l", "м":"m",
    "н":"n", "о":"o", "п":"p", "р":"r", "с":"s", "т":"t", "у":"u",
    "ф":"f", "х":"kh", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"sch", "ъ":"",
    "ы":"y", "ь":"", "э":"e", "ю":"yu", "я":"ya",
}

# Katakana is a phonetic syllabary with a mechanical 1:1 reading (unlike kanji),
# so it transliterates the same way _CYR_LAT handles Cyrillic. Digraphs (きゃ-type,
# and foreign-sound extensions like ファ/ティ) are checked before single characters.
_KATAKANA_DIGRAPHS = {
    "キャ":"kya","キュ":"kyu","キョ":"kyo","シャ":"sha","シュ":"shu","ショ":"sho",
    "チャ":"cha","チュ":"chu","チョ":"cho","ニャ":"nya","ニュ":"nyu","ニョ":"nyo",
    "ヒャ":"hya","ヒュ":"hyu","ヒョ":"hyo","ミャ":"mya","ミュ":"myu","ミョ":"myo",
    "リャ":"rya","リュ":"ryu","リョ":"ryo","ギャ":"gya","ギュ":"gyu","ギョ":"gyo",
    "ジャ":"ja","ジュ":"ju","ジョ":"jo","ビャ":"bya","ビュ":"byu","ビョ":"byo",
    "ピャ":"pya","ピュ":"pyu","ピョ":"pyo","ファ":"fa","フィ":"fi","フェ":"fe",
    "フォ":"fo","ティ":"ti","ディ":"di","トゥ":"tu","ドゥ":"du","ウィ":"wi",
    "ウェ":"we","ウォ":"wo","ヴァ":"va","ヴィ":"vi","ヴェ":"ve","ヴォ":"vo",
    "チェ":"che","ジェ":"je","シェ":"she",
}
_KATAKANA_SINGLE = {
    "ア":"a","イ":"i","ウ":"u","エ":"e","オ":"o",
    "カ":"ka","キ":"ki","ク":"ku","ケ":"ke","コ":"ko",
    "ガ":"ga","ギ":"gi","グ":"gu","ゲ":"ge","ゴ":"go",
    "サ":"sa","シ":"shi","ス":"su","セ":"se","ソ":"so",
    "ザ":"za","ジ":"ji","ズ":"zu","ゼ":"ze","ゾ":"zo",
    "タ":"ta","チ":"chi","ツ":"tsu","テ":"te","ト":"to",
    "ダ":"da","ヂ":"ji","ヅ":"zu","デ":"de","ド":"do",
    "ナ":"na","ニ":"ni","ヌ":"nu","ネ":"ne","ノ":"no",
    "ハ":"ha","ヒ":"hi","フ":"fu","ヘ":"he","ホ":"ho",
    "バ":"ba","ビ":"bi","ブ":"bu","ベ":"be","ボ":"bo",
    "パ":"pa","ピ":"pi","プ":"pu","ペ":"pe","ポ":"po",
    "マ":"ma","ミ":"mi","ム":"mu","メ":"me","モ":"mo",
    "ヤ":"ya","ユ":"yu","ヨ":"yo",
    "ラ":"ra","リ":"ri","ル":"ru","レ":"re","ロ":"ro",
    "ワ":"wa","ヲ":"wo","ン":"n","ヴ":"vu","ヶ":"ke",
}


def katakana_to_romaji(value: str) -> str:
    """Mechanically transliterate katakana runs to romaji; non-katakana characters
    (kanji, latin, punctuation) pass through unchanged since they can't be."""
    result = []
    chars = value
    i = 0
    while i < len(chars):
        digraph = chars[i:i + 2]
        if digraph in _KATAKANA_DIGRAPHS:
            result.append(_KATAKANA_DIGRAPHS[digraph])
            i += 2
            continue
        char = chars[i]
        if char == "ッ" and i + 1 < len(chars):
            # Sokuon: doubles the consonant of the NEXT mora (っか -> "kka").
            nxt = chars[i + 1:i + 3]
            nxt_romaji = _KATAKANA_DIGRAPHS.get(nxt) or _KATAKANA_SINGLE.get(chars[i + 1], "")
            if nxt_romaji and nxt_romaji[0] not in "aiueon":
                result.append(nxt_romaji[0])
            i += 1
            continue
        if char == "ー" and result and result[-1]:
            # Long vowel mark: repeats the previous mora's final vowel.
            result.append(result[-1][-1])
            i += 1
            continue
        if char == "・":
            result.append(" ")
            i += 1
            continue
        if char == "ン":
            # ん before a labial consonant (b/p/m) is conventionally romanized as
            # "m", not "n" (サンバ -> "samba", not "sanba" - a real phonetic
            # assimilation rule, not a stylistic choice).
            nxt = chars[i + 1:i + 2]
            nxt_romaji = _KATAKANA_SINGLE.get(nxt, "")
            result.append("m" if nxt_romaji[:1] in ("b", "p", "m") else "n")
            i += 1
            continue
        result.append(_KATAKANA_SINGLE.get(char, char))
        i += 1
    return "".join(result)


def slug(value: str, strip_year: bool = True) -> str:
    value = YEAR_PREFIX_RE.sub("", value) if strip_year else value
    return re.sub(r"[^\w]", "", value.casefold())


def translit_slug(value: str, yot: str = "y") -> str:
    value = YEAR_PREFIX_RE.sub("", value).casefold()
    return re.sub(
        r"[^\w]", "",
        "".join(yot if char == "й" else _CYR_LAT.get(char, char) for char in value),
    )


def extract_title_from_stem(stem: str) -> str:
    if " - " in stem:
        return stem.split(" - ", 1)[1]
    if "_-_" in stem:
        raw = re.sub(r"_\d+$", "", stem.split("_-_", 1)[1])
        return raw.replace("_", " ")
    return re.sub(r"^\d+[.\s-]+", "", stem)


def title_variants(value: str) -> set[str]:
    variants = set()
    candidates = {value, _TITLE_VARIANT_NOISE_RE.sub("", value).strip()}
    abbreviation = re.fullmatch(r"\s*([\w.]+)\s*\(([^)]+)\)\s*", value)
    if abbreviation:
        prefix, expansion = abbreviation.groups()
        initials = "".join(word[0] for word in re.findall(r"\w+", expansion))
        if slug(prefix) == slug(initials):
            candidates.add(expansion.strip())
    candidates.update(_TITLE_KARAOKE_MIX_RE.sub(r"\1", candidate) for candidate in list(candidates))
    for candidate in candidates:
        variants.add(slug(candidate))
        transliterated = translit_slug(candidate)
        if transliterated:
            variants.add(transliterated)
            variants.add(translit_slug(candidate, yot="j"))
            variants.add(translit_slug(candidate, yot="i"))
        # "дж" is the common Cyrillic digraph for the English "j" sound (as in
        # "джаz"/"jazz", "джинсы"/"jeans"), which a per-character transliteration
        # ("d"+"zh") misses entirely - it never produces anything resembling "jazz".
        dzh_variant = translit_slug(_DZH_DIGRAPH_RE.sub("j", candidate))
        if dzh_variant:
            variants.add(dzh_variant)
        katakana_variant = slug(katakana_to_romaji(candidate))
        if katakana_variant:
            variants.add(katakana_variant)
    return {item for item in variants if item}


def relaxed_title_variants(value: str) -> set[str]:
    """Match optional featured-artist credits only when an album resolves them unambiguously."""
    variants = set(title_variants(value))
    variants.update(title_variants(_TITLE_CREDIT_RE.sub("", value).strip()))
    return variants


def title_on_disk(title: str, disk_slugs: set[str]) -> bool:
    return bool(title_variants(title) & disk_slugs)


_FUZZY_NAME_THRESHOLD = 0.84


def find_named_dir(root: Path, name: str, fuzzy: bool = False) -> Path | None:
    """`fuzzy=True` also accepts a close same-script spelling variant (e.g.
    "Uma2rman" vs a folder named "Uma2rmaH" - Last.fm itself is inconsistent
    about which spelling it returns from different endpoints), picking the
    single closest match above a strict similarity threshold. Only safe to
    opt into for ARTIST-level lookups: album names within one artist are
    often legitimately near-identical ("Best" vs "Best II"), so callers
    resolving an album directory must leave this off."""
    exact = root / name
    if exact.is_dir():
        return exact
    if not root.is_dir():
        return None
    targets = {slug(name), translit_slug(name), translit_slug(name, yot="j")} - {""}
    name_slug = slug(name)
    best_fuzzy: tuple[float, Path] | None = None
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        dir_slug = slug(directory.name)
        variants = {dir_slug, translit_slug(directory.name), translit_slug(directory.name, yot="j")}
        if variants & targets:
            return directory
        if fuzzy and len(name_slug) >= 4 and len(dir_slug) >= 4:
            ratio = SequenceMatcher(None, name_slug, dir_slug).ratio()
            if ratio >= _FUZZY_NAME_THRESHOLD and (best_fuzzy is None or ratio > best_fuzzy[0]):
                best_fuzzy = (ratio, directory)
    return best_fuzzy[1] if best_fuzzy else None


def best_cover_url(images: list[dict], preferred: str | None = None) -> str:
    sizes = (preferred,) if preferred else ("extralarge", "large", "medium", "small")
    for size in sizes:
        for image in images or []:
            url = image.get("#text", "")
            if image.get("size") == size and url and LASTFM_PLACEHOLDER not in url:
                return url
    return ""


def _json_request(url: str, timeout: float = 15) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_error = "request failed"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(0.35)
    return {"error": last_error}


def _text_request(url: str, timeout: float = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(2 * 1024 * 1024).decode("utf-8", "replace")
        except Exception:
            if attempt == 0:
                time.sleep(0.25)
    return ""


def lastfm_request(method: str, params: dict, api_key: str, timeout: float = 15) -> dict:
    query = urllib.parse.urlencode({"method": method, "api_key": api_key, "format": "json", **params})
    return _json_request(f"{LASTFM_BASE}?{query}", timeout)


def _credit_matches_artist(credit: list, target: str) -> bool:
    """A release's artist-credit can be spelled differently than the artist
    entity itself (e.g. Uma2rman is credited as the stylized "Uma2rmaH" on
    some releases) - match against either the credited-as name or the
    underlying artist's canonical name, not just the former."""
    if not credit:
        return False
    entry = credit[0]
    credited_name = entry.get("name", "")
    canonical_name = (entry.get("artist") or {}).get("name", "")
    target_slug = slug(target)
    return slug(credited_name) == target_slug or slug(canonical_name) == target_slug


def musicbrainz_album_info(artist: str, album: str) -> dict:
    query = urllib.parse.urlencode({"query": f'artist:"{artist}" AND release:"{album}"', "limit": 5, "fmt": "json"})
    data = _json_request(f"{MUSICBRAINZ_BASE}/release?{query}")
    releases = []
    for release in data.get("releases", []):
        credit = release.get("artist-credit") or []
        if not _credit_matches_artist(credit, artist):
            continue
        exact_name = slug(release.get("title", "")) == slug(album)
        release_date = str(release.get("date") or "")
        is_special_edition = bool(release.get("disambiguation"))
        releases.append((not exact_name, is_special_edition, release_date or "9999", release))

    if not releases:
        # A stylized/mixed-script per-release credit (e.g. Uma2rman is
        # credited as "Uma2rmaH" on some releases) means MusicBrainz's own
        # `artist:` text search can miss the release even under the artist's
        # canonical name - resolve the artist to their MBID via alias-aware
        # artist search, then retry with `arid:`, which matches every release
        # regardless of how that specific release spells the credit.
        artist_query = urllib.parse.urlencode({"query": artist, "limit": 1, "fmt": "json"})
        artist_data = _json_request(f"{MUSICBRAINZ_BASE}/artist?{artist_query}")
        artist_candidates = artist_data.get("artists") or []
        if artist_candidates and int(artist_candidates[0].get("score") or 0) >= 90:
            artist_mbid = str(artist_candidates[0].get("id") or "")
            if artist_mbid:
                retry_query = urllib.parse.urlencode(
                    {"query": f'arid:{artist_mbid} AND release:"{album}"', "limit": 5, "fmt": "json"}
                )
                retry_data = _json_request(f"{MUSICBRAINZ_BASE}/release?{retry_query}")
                for release in retry_data.get("releases", []):
                    exact_name = slug(release.get("title", "")) == slug(album)
                    release_date = str(release.get("date") or "")
                    is_special_edition = bool(release.get("disambiguation"))
                    releases.append((not exact_name, is_special_edition, release_date or "9999", release))

    # Prefer the plain/standard release over a deluxe/gift/special edition
    # when both otherwise tie (exact title match, same date) - special
    # editions often bundle short reprise/interlude fragments as separate
    # "tracks" that were never released or uploaded on their own, which just
    # produces a wall of unfindable-track errors during download.
    ordered_releases = sorted(releases, key=lambda item: (item[0], item[1], item[2]))
    earliest_date = next(
        (date for _, _, date, _ in ordered_releases if re.match(r"(?:19|20)\d{2}", date)),
        "",
    )
    for _, _, release_date, release in ordered_releases:
        time.sleep(1.05)
        params = urllib.parse.urlencode({"inc": "recordings+artist-credits", "fmt": "json"})
        detail = _json_request(f"{MUSICBRAINZ_BASE}/release/{release['id']}?{params}")
        if detail.get("error"):
            time.sleep(1.05)
            detail = _json_request(f"{MUSICBRAINZ_BASE}/release/{release['id']}?{params}")
        tracks = []
        for medium_index, medium in enumerate(detail.get("media", []), 1):
            for track in medium.get("tracks", []):
                position = track.get("position", 0)
                length_ms = track.get("length") or (track.get("recording") or {}).get("length")
                duration = float(length_ms) / 1000 if length_ms else None
                credit = (
                    track.get("artist-credit")
                    or (track.get("recording") or {}).get("artist-credit")
                    or []
                )
                artist_names = [
                    str(item.get("name") or (item.get("artist") or {}).get("name") or "").strip()
                    for item in credit if isinstance(item, dict)
                ]
                tracks.append((
                    medium_index,
                    int(position) if str(position).isdigit() else 0,
                    track.get("title", ""),
                    duration,
                    [name for name in artist_names if name] or [artist],
                ))
        ordered = [item for item in sorted(tracks) if item[2]]
        titles = [title for _, _, title, _, _ in ordered]
        if titles:
            year_match = re.match(r"((?:19|20)\d{2})", earliest_date or release_date)
            return {
                "tracks": titles,
                "durations": [duration for _, _, _, duration, _ in ordered],
                "artists": [artists for _, _, _, _, artists in ordered],
                "year": year_match.group(1) if year_match else "",
            }
    return {"tracks": [], "durations": [], "artists": [], "year": ""}


def artist_search(artist: str, api_key: str, limit: int = 12) -> list[dict]:
    data = lastfm_request("artist.search", {"artist": artist, "limit": limit}, api_key)
    return data.get("results", {}).get("artistmatches", {}).get("artist", [])


_DEEZER_NO_PICTURE_HASH = "d41d8cd98f00b204e9800998ecf8427e"  # md5("") - Deezer's "no photo" placeholder


def deezer_artist_image(artist: str) -> str:
    """Look up an artist photo on Deezer's editorially-curated catalog.

    Last.fm artist images are community-uploaded and occasionally get replaced with
    unrelated spam (betting-site QR codes etc.) with no moderation. Deezer's artist
    photos come from its own catalog, so they're used as the preferred source instead.
    """
    query = urllib.parse.urlencode({"q": artist, "limit": 5})
    data = _json_request(f"https://api.deezer.com/search/artist?{query}")
    results = [
        r for r in (data.get("data") or [])
        if _DEEZER_NO_PICTURE_HASH not in (r.get("picture_medium") or "")
    ]
    if not results:
        return ""
    exact = [r for r in results if slug(r.get("name", "")) == slug(artist)]
    candidates = exact or results
    best = max(candidates, key=lambda r: r.get("nb_fan", 0))
    return best.get("picture_medium") or ""


def artist_top_tracks(artist: str, api_key: str, limit: int = 20) -> list[str]:
    """Return the artist's top tracks by playcount, for use as a stand-in tracklist
    when an informal "best of"/"Сборник"-style album has no real catalog release.

    Last.fm's per-user scrobble tagging means the same song often shows up several
    times under near-identical spellings (e.g. "Я Это Ты", "Я - Это Ты", "Ты это я")
    - fetch extra and dedupe by slug so the compilation doesn't repeat one song
    under multiple names while missing others.
    """
    data = lastfm_request(
        "artist.getTopTracks", {"artist": artist, "limit": limit * 3, "autocorrect": "1"}, api_key
    )
    tracks = data.get("toptracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    trailing_bracket_re = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
    seen = set()
    result = []
    for track in tracks:
        name = track.get("name") or ""
        if not name:
            continue
        bare_name = name
        while True:
            stripped = trailing_bracket_re.sub("", bare_name)
            if stripped == bare_name:
                break
            bare_name = stripped
        key = slug(bare_name)
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= limit:
            break
    return result


def artist_album_entries(artist: str, api_key: str, limit: int = 50) -> list[dict]:
    data = lastfm_request(
        "artist.getTopAlbums", {"artist": artist, "limit": limit, "autocorrect": "1"}, api_key
    )
    entries = []
    for album in data.get("topalbums", {}).get("album", []):
        name = album.get("name", "")
        if not name or name == "[unknown]":
            continue
        try:
            playcount = int(album.get("playcount", 0))
        except (TypeError, ValueError):
            playcount = 0
        entries.append({"name": name, "playcount": playcount, "images": album.get("image", [])})
    return entries


def popular_album_entries(artist: str, api_key: str, studio_limit: int = 15) -> tuple[list[dict], list[dict]]:
    entries = artist_album_entries(artist, api_key)
    if not entries:
        return [], []
    top_playcount = max(item["playcount"] for item in entries)
    relative_threshold = int(top_playcount * 0.05)
    # The 500-play floor filters noise for well-known artists (garbage entries hiding
    # among real hits), but for a niche artist whose own top album never reaches 500
    # plays, it demands more plays than their most popular release has - excluding
    # every album, including the top one. Only apply the floor once it's reachable.
    threshold = max(500, relative_threshold) if top_playcount >= 500 else relative_threshold
    filtered = [item for item in entries if item["playcount"] >= threshold and not JUNK_ALBUM_RE.search(item["name"])]
    studios = [item for item in filtered if not SINGLE_EP_RE.search(item["name"])][:studio_limit]
    singles = [item for item in filtered if SINGLE_EP_RE.search(item["name"])]
    return studios, singles


def _tracks_from_album(album: dict) -> list[str]:
    tracks = album.get("tracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    return [track.get("name", "") for track in tracks if track.get("name")]


def split_artist_credit(value: str | list[str]) -> list[str]:
    """Return stable individual artist names from a catalog credit."""
    values = value if isinstance(value, list) else [value]
    result = []
    for raw in values:
        raw = str(raw)
        featured = re.findall(
            r"\((?:feat(?:uring)?\.?|ft\.?)\s+([^)]+)\)", raw, flags=re.IGNORECASE
        )
        main = re.sub(
            r"\s+\((?:feat(?:uring)?\.?|ft\.?)\s+[^)]+\)\s*$", "", raw,
            flags=re.IGNORECASE,
        )
        for credit in [main, *featured]:
            for name in re.split(r"\s*(?:&|,|;|\sfeat(?:uring)?\.?\s|\sft\.?\s)\s*", credit, flags=re.IGNORECASE):
                name = name.strip()
                if name and name.casefold() not in {item.casefold() for item in result}:
                    result.append(name)
    return result


def _lastfm_track_artists(album: dict, fallback: str) -> list[list[str]]:
    tracks = album.get("tracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    result = []
    for track in tracks:
        credit = track.get("artist") or fallback
        if isinstance(credit, dict):
            credit = credit.get("name") or fallback
        result.append(split_artist_credit(str(credit)))
    return result


def album_info(artist: str, album: str, api_key: str, delay: float = 0) -> dict:
    data = lastfm_request("album.getInfo", {"artist": artist, "album": album, "autocorrect": "1"}, api_key)
    if delay:
        time.sleep(delay)
    info = data.get("album", {})
    tracks = _tracks_from_album(info)
    track_artists = _lastfm_track_artists(info, artist)
    cover_url = best_cover_url(info.get("image", []))

    if not tracks or not cover_url:
        search = lastfm_request("album.search", {"album": album, "limit": 10}, api_key)
        for candidate in search.get("results", {}).get("albummatches", {}).get("album", []):
            if slug(candidate.get("artist", "")) != slug(artist):
                continue
            cover_url = cover_url or best_cover_url(candidate.get("image", []))
            if not tracks and candidate.get("name") != album:
                alternate = lastfm_request(
                    "album.getInfo", {"artist": artist, "album": candidate.get("name", ""), "autocorrect": "1"}, api_key
                ).get("album", {})
                tracks = _tracks_from_album(alternate)
                track_artists = _lastfm_track_artists(alternate, artist)
            if tracks and cover_url:
                break

    canonical_artist = info.get("artist")
    if isinstance(canonical_artist, dict):
        canonical_artist = canonical_artist.get("name")
    published = info.get("wiki", {}).get("published", "") or ""
    year_match = re.search(r"\b(?:19|20)\d{2}\b", published)
    canonical_name = info.get("name") or album
    mb_info = musicbrainz_album_info(artist, canonical_name)
    selected_tracks = tracks or mb_info["tracks"]
    return {
        "name": canonical_name,
        "artist": canonical_artist or artist,
        "year": mb_info["year"] or (year_match.group() if year_match else ""),
        "tracks": selected_tracks,
        "track_source": "lastfm" if tracks else "musicbrainz",
        "catalogs": {"lastfm": tracks, "musicbrainz": mb_info["tracks"]},
        "catalog_durations": {"lastfm": [], "musicbrainz": mb_info["durations"]},
        "catalog_artists": {
            "lastfm": track_artists,
            "musicbrainz": mb_info.get("artists", []),
        },
        "cover_url": cover_url,
        "error": data.get("error"),
    }


def itunes_album_info(artist: str, album: str) -> dict:
    query = urllib.parse.urlencode({"term": f"{artist} {album}", "entity": "album", "limit": 25})
    search = _json_request(f"https://itunes.apple.com/search?{query}")
    match = next(
        (
            item for item in search.get("results", [])
            if slug(item.get("artistName", "")) == slug(artist)
            and slug(item.get("collectionName", "")) == slug(album)
        ),
        None,
    )
    if not match:
        return {"tracks": [], "durations": [], "artists": [], "year": "", "cover_url": ""}
    lookup = _json_request(
        "https://itunes.apple.com/lookup?" +
        urllib.parse.urlencode({"id": match.get("collectionId"), "entity": "song"})
    )
    songs = [item for item in lookup.get("results", []) if item.get("wrapperType") == "track"]
    songs.sort(key=lambda item: (int(item.get("discNumber") or 0), int(item.get("trackNumber") or 0)))
    release_date = str(match.get("releaseDate") or "")
    year_match = re.match(r"((?:19|20)\d{2})", release_date)
    cover = str(match.get("artworkUrl100") or "").replace("100x100bb", "1200x1200bb")
    return {
        "tracks": [str(item.get("trackName")) for item in songs if item.get("trackName")],
        "durations": [
            float(item.get("trackTimeMillis")) / 1000 if item.get("trackTimeMillis") else None
            for item in songs if item.get("trackName")
        ],
        "artists": [
            split_artist_credit(str(item.get("artistName") or artist))
            for item in songs if item.get("trackName")
        ],
        "year": year_match.group(1) if year_match else "",
        "cover_url": cover,
    }


def deezer_album_info(artist: str, album: str) -> dict:
    query = urllib.parse.urlencode({"q": f'artist:"{artist}" album:"{album}"', "limit": 25})
    search = _json_request(f"https://api.deezer.com/search/album?{query}")
    candidates = [
        item for item in search.get("data", [])
        if slug(item.get("title", "")) == slug(album)
    ]
    match = next(
        (
            item for item in candidates
            if slug((item.get("artist") or {}).get("name", "")) == slug(artist)
        ),
        None,
    )
    detail = {}
    if not match:
        requested_artist = slug(artist)
        for candidate in candidates:
            candidate_detail = _json_request(
                f"https://api.deezer.com/album/{candidate.get('id')}"
            )
            songs = list((candidate_detail.get("tracks") or {}).get("data") or [])
            track_artist_slugs = {
                slug((song.get("artist") or {}).get("name", ""))
                for song in songs
            }
            if requested_artist and requested_artist in track_artist_slugs:
                match = candidate
                detail = candidate_detail
                break
    if not match:
        return {"tracks": [], "durations": [], "artists": [], "year": "", "cover_url": ""}
    if not detail:
        detail = _json_request(f"https://api.deezer.com/album/{match.get('id')}")
    tracks_page = detail.get("tracks") or {}
    songs = list(tracks_page.get("data") or [])
    next_url = tracks_page.get("next")
    expected_count = int(detail.get("nb_tracks") or 0)
    if expected_count > len(songs):
        tracks_page = _json_request(
            f"https://api.deezer.com/album/{match.get('id')}/tracks?limit=100"
        )
        songs = list(tracks_page.get("data") or [])
        next_url = tracks_page.get("next")
    visited_pages = set()
    while next_url and next_url not in visited_pages:
        visited_pages.add(next_url)
        tracks_page = _json_request(next_url)
        songs.extend(tracks_page.get("data") or [])
        next_url = tracks_page.get("next")
    songs.sort(key=lambda item: (int(item.get("disk_number") or 0), int(item.get("track_position") or 0)))
    release_date = str(detail.get("release_date") or "")
    year_match = re.match(r"((?:19|20)\d{2})", release_date)
    return {
        "tracks": [str(item.get("title")) for item in songs if item.get("title")],
        "durations": [
            float(item.get("duration")) if item.get("duration") else None
            for item in songs if item.get("title")
        ],
        "artists": [
            split_artist_credit([
                str(contributor.get("name")) for contributor in item.get("contributors") or []
                if contributor.get("name")
            ] or str((item.get("artist") or {}).get("name") or artist))
            for item in songs if item.get("title")
        ],
        "year": year_match.group(1) if year_match else "",
        "cover_url": str(detail.get("cover_xl") or detail.get("cover_big") or ""),
    }


def _html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def fourwords_album_info(artist: str, album: str) -> dict:
    """Read a public catalog page used for releases absent from global APIs."""
    empty = {"tracks": [], "durations": [], "artists": [], "year": "", "cover_url": ""}
    query = urllib.parse.urlencode({"search": album})
    search_url = f"https://www.4words.ru/search?{query}"
    search = _text_request(search_url)
    if "/album/" not in search:
        time.sleep(0.25)
        search = _text_request(search_url)
    candidates = re.findall(
        r"<a\s+href=['\"](https://www\.4words\.ru/album/\d+)['\"]>(.*?)</a>",
        search,
        re.IGNORECASE | re.DOTALL,
    )
    detail_url = ""
    for url, body in candidates:
        title_match = re.search(
            r"viewSearch__title.*?<p>(.*?)</p>", body, re.IGNORECASE | re.DOTALL
        )
        subtitle_match = re.search(
            r"viewSearch__subtitle.*?<p>(.*?)</p>", body, re.IGNORECASE | re.DOTALL
        )
        title = _html_text(title_match.group(1)) if title_match else ""
        subtitle = _html_text(subtitle_match.group(1)) if subtitle_match else ""
        candidate_artist = subtitle.split("•", 1)[0].strip()
        if slug(title) == slug(album) and slug(candidate_artist) == slug(artist):
            detail_url = url
            break
    if not detail_url:
        return empty

    detail = _text_request(detail_url)
    if "tracklist__songTitle" not in detail:
        time.sleep(0.25)
        detail = _text_request(detail_url)
    header = re.search(
        r'<meta\s+property="og:title"\s+content="([^"]+)"', detail, re.IGNORECASE
    )
    if not header:
        return empty
    header_text = _html_text(header.group(1))
    if " — " not in header_text:
        return empty
    detail_artist, detail_album = header_text.rsplit(" — ", 1)
    detail_album = re.sub(r"\s*\(альбом\)\s*$", "", detail_album, flags=re.IGNORECASE)
    if slug(detail_artist) != slug(artist) or slug(detail_album) != slug(album):
        return empty

    track_rows = re.findall(
        r'tracklist__position[^>]*>\s*<p>\s*(\d+)\s*</p>.*?'
        r'tracklist__songTitle[^>]*>\s*<p>(.*?)</p>.*?'
        r'tracklist__duration[^>]*>\s*<p>\s*(\d+):(\d+)\s*</p>',
        detail,
        re.IGNORECASE | re.DOTALL,
    )
    ordered = sorted(
        (int(position), _html_text(title), int(minutes) * 60 + int(seconds))
        for position, title, minutes, seconds in track_rows
        if _html_text(title)
    )
    if not ordered or [position for position, _, _ in ordered] != list(range(1, len(ordered) + 1)):
        return empty
    date_position = detail.casefold().find("дата выпуска:")
    date_block = detail[date_position:date_position + 600] if date_position >= 0 else ""
    year_match = re.search(r"\b((?:19|20)\d{2})\b", date_block)
    cover_match = re.search(
        r'<meta\s+property="og:image"\s+content="([^"]+)"', detail, re.IGNORECASE
    )
    return {
        "tracks": [title for _, title, _ in ordered],
        "durations": [duration for _, _, duration in ordered],
        "artists": [[artist] for _ in ordered],
        "year": year_match.group(1) if year_match else "",
        "cover_url": html.unescape(cover_match.group(1)) if cover_match else "",
    }


def _tracklist_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    if len(left) == len(right):
        matched = sum(
            1 for left_title, right_title in zip(left, right)
            if relaxed_title_variants(left_title) & relaxed_title_variants(right_title)
        )
        return matched / len(left)
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    longer_index = 0
    for shorter_title in shorter:
        while longer_index < len(longer):
            longer_title = longer[longer_index]
            longer_index += 1
            if relaxed_title_variants(shorter_title) & relaxed_title_variants(longer_title):
                break
        else:
            return 0.0
    return len(shorter) / len(longer)


def verified_album_info(
    artist: str,
    album: str,
    api_key: str,
    delay: float = 0,
    preferred_source: str | None = None,
) -> dict:
    """Prefer catalog consensus, but allow one exact catalog as a fallback."""
    info = album_info(artist, album, api_key, delay)
    itunes = itunes_album_info(info.get("artist") or artist, info.get("name") or album)
    deezer = deezer_album_info(info.get("artist") or artist, info.get("name") or album)
    fourwords = fourwords_album_info(info.get("artist") or artist, info.get("name") or album)
    catalogs = dict(info.get("catalogs") or {})
    catalogs.update({
        "itunes": itunes["tracks"], "deezer": deezer["tracks"],
        "fourwords": fourwords["tracks"],
    })
    catalog_durations = dict(info.get("catalog_durations") or {})
    catalog_durations.update({
        "itunes": itunes["durations"], "deezer": deezer["durations"],
        "fourwords": fourwords["durations"],
    })
    catalog_artists = dict(info.get("catalog_artists") or {})
    catalog_artists.update({
        "itunes": itunes.get("artists", []), "deezer": deezer.get("artists", []),
        "fourwords": fourwords.get("artists", []),
    })
    catalogs = {name: tracks for name, tracks in catalogs.items() if tracks}

    priority = ("lastfm", "deezer", "itunes", "musicbrainz", "fourwords")
    info["catalog_choices"] = {
        source: catalogs[source] for source in priority if source in catalogs
    }
    info["selection_required"] = False
    info["single_source"] = False
    info["selected_by_user"] = False
    consensus = []
    selected_source = ""

    if preferred_source and preferred_source.casefold() == "artist-top-tracks":
        top_tracks = artist_top_tracks(info.get("artist") or artist, api_key)
        if not top_tracks:
            info["tracks"] = []
            info["error"] = f"no top tracks found for {info.get('artist') or artist!r}"
            info["verified_by"] = []
            return info
        info["tracks"] = top_tracks
        info["selected_source"] = "artist-top-tracks"
        info["verified_by"] = ["artist-top-tracks"]
        info["single_source"] = True
        info["selected_by_user"] = True
        info["compilation_fallback"] = True
        return info

    if preferred_source:
        selected_source = next(
            (
                source for source in priority
                if source in catalogs and source.casefold() == preferred_source.casefold()
            ),
            "",
        )
        if not selected_source:
            available = ", ".join(info["catalog_choices"]) or "none"
            info["tracks"] = []
            info["error"] = (
                f"catalog source {preferred_source!r} is unavailable; available: {available}"
            )
            info["verified_by"] = []
            return info
        consensus = [selected_source]
        info["tracks"] = catalogs[selected_source]
        info["selected_by_user"] = True
    else:
        for source in priority:
            tracks = catalogs.get(source)
            if not tracks:
                continue
            agreeing = [
                other for other, other_tracks in catalogs.items()
                if other != source and _tracklist_similarity(tracks, other_tracks) >= 0.70
            ]
            if agreeing:
                consensus = [source, *agreeing]
                selected_source = max(
                    consensus,
                    key=lambda name: (len(catalogs[name]), -priority.index(name)),
                )
                info["tracks"] = catalogs[selected_source]
                break

        if not consensus and len(catalogs) == 1:
            selected_source = next(source for source in priority if source in catalogs)
            consensus = [selected_source]
            info["tracks"] = catalogs[selected_source]
            info["single_source"] = True
        elif not consensus and catalogs:
            counts = ", ".join(
                f"{name}={len(tracks)}" for name, tracks in info["catalog_choices"].items()
            )
            info["tracks"] = []
            info["error"] = f"album catalog selection required: {counts}"
            info["verified_by"] = []
            info["selection_required"] = True
            return info
        elif not consensus:
            top_tracks = (
                artist_top_tracks(info.get("artist") or artist, api_key)
                if _COMPILATION_ALBUM_RE.search(album)
                else []
            )
            if top_tracks:
                info["tracks"] = top_tracks
                info["selected_source"] = "artist-top-tracks"
                info["verified_by"] = ["artist-top-tracks"]
                info["single_source"] = True
                info["compilation_fallback"] = True
                return info
            info["tracks"] = []
            info["error"] = "no catalog track lists"
            info["verified_by"] = []
            return info

    info["selected_source"] = selected_source
    years = [
        year for year in (
            info.get("year", ""), itunes.get("year", ""), deezer.get("year", ""),
            fourwords.get("year", ""),
        ) if re.fullmatch(r"(?:19|20)\d{2}", str(year))
    ]
    if years:
        info["year"] = min(years)
    track_durations = []
    for track_title in info["tracks"]:
        duration = None
        for source in [*consensus, *priority]:
            source_tracks = catalogs.get(source) or []
            source_durations = catalog_durations.get(source) or []
            for candidate_title, candidate_duration in zip(source_tracks, source_durations):
                if relaxed_title_variants(track_title) & relaxed_title_variants(candidate_title):
                    duration = candidate_duration
                    break
            if duration:
                break
        track_durations.append(duration)
    info["track_durations"] = track_durations
    track_artists = []
    artist_priority = ("musicbrainz", "itunes", "deezer", "lastfm", "fourwords")
    for track_title in info["tracks"]:
        credits = []
        for source in artist_priority:
            source_tracks = catalogs.get(source) or []
            source_artists = catalog_artists.get(source) or []
            for candidate_title, candidate_artists in zip(source_tracks, source_artists):
                if relaxed_title_variants(track_title) & relaxed_title_variants(candidate_title):
                    for name in candidate_artists or []:
                        if name and name.casefold() not in {item.casefold() for item in credits}:
                            credits.append(name)
                    break
        track_artists.append(credits or [info.get("artist") or artist])
    info["track_artists"] = track_artists
    info["cover_url"] = (
        info.get("cover_url") or deezer.get("cover_url") or itunes.get("cover_url")
        or fourwords.get("cover_url")
    )
    info["error"] = None
    info["verified_by"] = list(dict.fromkeys(consensus))
    return info


def track_info(artist: str, title: str, api_key: str, delay: float = 0) -> dict:
    data = lastfm_request(
        "track.getInfo", {"artist": artist, "track": title, "autocorrect": "1"}, api_key
    )
    if delay:
        time.sleep(delay)
    track = data.get("track", {})
    track_artist = track.get("artist", {})
    if isinstance(track_artist, dict):
        track_artist = track_artist.get("name")
    return {
        "title": track.get("name") or title,
        "artist": track_artist or artist,
        "album": track.get("album", {}).get("title"),
    }


def _clean_single_album(value: str) -> str:
    value = re.sub(r"\s*-\s*Single\s*$", "", value, flags=re.IGNORECASE).strip()
    return _TITLE_CREDIT_RE.sub("", value).strip()


def _duration_matches(expected: float | None, candidate: float | None) -> bool:
    if not expected or not candidate:
        return True
    return abs(float(expected) - float(candidate)) <= max(8.0, float(expected) * 0.05)


def _track_candidate_matches(
    candidate: dict, artists: list[str], title: str, duration: float | None,
) -> bool:
    if not (relaxed_title_variants(candidate.get("title", "")) & relaxed_title_variants(title)):
        return False
    if not _duration_matches(duration, candidate.get("duration")):
        return False
    wanted = {slug(name) for name in split_artist_credit(artists) if slug(name)}
    found = {
        slug(name) for name in split_artist_credit(candidate.get("artists") or [])
        if slug(name)
    }
    return bool(wanted & found)


def _itunes_track_candidates(artists: list[str], title: str) -> list[dict]:
    query = urllib.parse.urlencode({
        "term": f"{' '.join(artists)} {title}", "entity": "song", "limit": 25,
        "country": "RU",
    })
    data = _json_request(f"https://itunes.apple.com/search?{query}")
    result = []
    for item in data.get("results", []):
        release_date = str(item.get("releaseDate") or "")
        year_match = re.match(r"((?:19|20)\d{2})", release_date)
        artwork = str(item.get("artworkUrl100") or "").replace("100x100bb", "1200x1200bb")
        credited = split_artist_credit(str(item.get("artistName") or ""))
        featured = re.findall(
            r"\((?:feat(?:uring)?\.?|ft\.?)\s+([^)]+)\)",
            str(item.get("trackName") or ""), flags=re.IGNORECASE,
        )
        for name in split_artist_credit(featured):
            if name.casefold() not in {credit.casefold() for credit in credited}:
                credited.append(name)
        result.append({
            "source": "itunes",
            "title": str(item.get("trackName") or ""),
            "artists": credited,
            "album": _clean_single_album(str(item.get("collectionName") or "")),
            "duration": float(item.get("trackTimeMillis")) / 1000 if item.get("trackTimeMillis") else None,
            "cover_url": artwork,
            "year": year_match.group(1) if year_match else "",
        })
    return result


def _deezer_track_candidates(artists: list[str], title: str) -> list[dict]:
    query = urllib.parse.urlencode({"q": f"{' '.join(artists)} {title}", "limit": 25})
    data = _json_request(f"https://api.deezer.com/search?{query}")
    result = []
    for item in data.get("data", []):
        album = item.get("album") or {}
        credits = [
            str(contributor.get("name")) for contributor in item.get("contributors") or []
            if contributor.get("name")
        ]
        result.append({
            "source": "deezer",
            "title": str(item.get("title") or ""),
            "artists": split_artist_credit(credits or str((item.get("artist") or {}).get("name") or "")),
            "album": _clean_single_album(str(album.get("title") or "")),
            "duration": float(item.get("duration")) if item.get("duration") else None,
            "cover_url": str(album.get("cover_xl") or album.get("cover_big") or ""),
            "year": "",
        })
    return result


def _musicbrainz_track_candidates(artists: list[str], title: str) -> list[dict]:
    query = urllib.parse.urlencode({
        "query": f'artist:"{artists[0]}" AND recording:"{title}"',
        "limit": 25, "fmt": "json",
    })
    data = _json_request(f"{MUSICBRAINZ_BASE}/recording?{query}")
    result = []
    for item in data.get("recordings", []):
        credits = [
            str(credit.get("name") or (credit.get("artist") or {}).get("name") or "")
            for credit in item.get("artist-credit") or [] if isinstance(credit, dict)
        ]
        releases = item.get("releases") or []
        release = min(releases, key=lambda value: str(value.get("date") or "9999"), default={})
        release_date = str(release.get("date") or "")
        year_match = re.match(r"((?:19|20)\d{2})", release_date)
        result.append({
            "source": "musicbrainz",
            "title": str(item.get("title") or ""),
            "artists": split_artist_credit(credits),
            "album": _clean_single_album(str(release.get("title") or "")),
            "duration": float(item.get("length")) / 1000 if item.get("length") else None,
            "cover_url": "",
            "year": year_match.group(1) if year_match else "",
        })
    return result


def resolve_track_metadata(
    artists: list[str], title: str, duration: float | None = None,
) -> dict:
    """Resolve incomplete upload tags only when two independent catalogs agree."""
    if not artists or not title:
        return {}
    candidates = []
    for finder in (_itunes_track_candidates, _deezer_track_candidates, _musicbrainz_track_candidates):
        candidates.extend(
            candidate for candidate in finder(artists, title)
            if _track_candidate_matches(candidate, artists, title, duration)
        )
    priority = {"musicbrainz": 0, "itunes": 1, "deezer": 2}
    for candidate in sorted(candidates, key=lambda item: priority[item["source"]]):
        agreeing = [
            other for other in candidates
            if other["source"] != candidate["source"]
            and relaxed_title_variants(other["title"]) & relaxed_title_variants(candidate["title"])
            and relaxed_title_variants(other["album"]) & relaxed_title_variants(candidate["album"])
            and _duration_matches(other.get("duration"), candidate.get("duration"))
        ]
        if not agreeing:
            continue
        group = [candidate, *agreeing]
        credits = []
        for item in sorted(group, key=lambda value: priority[value["source"]]):
            for name in item.get("artists") or []:
                if name and name.casefold() not in {credit.casefold() for credit in credits}:
                    credits.append(name)
        album_names = [item["album"] for item in group if item.get("album")]
        album = min(album_names, key=len) if album_names else title
        cover_url = next((item["cover_url"] for item in group if item.get("cover_url")), "")
        year = next((item["year"] for item in group if item.get("year")), "")
        return {
            "title": min((item["title"] for item in group if item.get("title")), key=len),
            "artists": credits or artists,
            "album": album,
            "cover_url": cover_url,
            "year": year,
            "verified_by": list(dict.fromkeys(item["source"] for item in group)),
        }
    return {}
