# Navidrome Self-Hosted

## TODO

- [ ] **AcoustID fingerprinting** — identify tracks by audio content via MusicBrainz/AcoustID API. Flag files where actual audio does not match tagged artist. Requires `fpcalc` (chromaprint) in Dockerfile + AcoustID API key.
- [x] **Refactoring** - split all there python functions into separate files
---


Self-hosted music server based on [Navidrome](https://www.navidrome.org/) with [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) integration for library analysis, instant mix, and similar track discovery, plus a metadata-cleanup/download pipeline and a small web UI for searching, downloading, and fixing the library from a browser.

## Repository Structure

```
.
├── application/
│   ├── web/                 # Flask web UI (search/download/library/cleanup) at /tools/
│   └── ...                  # Docker stack (Navidrome + AudioMuse-AI)
└── prepare/                 # Metadata checker, fixer, and downloader (single Docker image)
    ├── app/                 # All Python source
    └── tests/                # unittest suite
```

---

## Quick Start

### 1. Download music (optional)

```bash
cd prepare
docker compose run --rm prepare /music --download-album "Rammstein" "Mutter"
```

See [Downloading Music](#downloading-music--download_musicpy) for full details.

### 2. Prepare music metadata

```bash
cd prepare
cp .env.example .env   # set MUSIC_DIR=/path/to/your/music
docker compose run --rm prepare /music         # dry-run
docker compose run --rm prepare /music --fix   # apply fixes
```

### 3. Configure the stack

```bash
cd application
cp .env.example .env
```

`.env`:
```env
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=yourpassword
LASTFM_APIKEY=your_key
LASTFM_SECRET=your_secret
POSTGRES_PASSWORD=yourdbpassword
```

### 4. Add the AudioMuse-AI plugin

Download `audiomuseai.ndp` from [AudioMuse-AI-NV-plugin releases](https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases) → place in `application/data/plugins/`.

### 5. Start

```bash
cd application
./start.sh
```

| Service | Port |
|---|---|
| Navidrome | 4533 |
| AudioMuse-AI | 8000 |
| Uploader | 8091 |

### 6. Configure plugin in Navidrome

**Settings → Plugins → AudioMuse-AI** → enable → URL: `http://audiomuse-flask:8000`

### 7. Run Analysis

1. Open `http://<server>:8000`
2. **Start Analysis** — scans audio features (runs in background)
3. **Clustering** — groups tracks, creates Navidrome playlists
4. Instant Mix / Radio / Similar Artists appear on tracks

> After adding new tracks: re-run **Analysis** only. Clustering is optional.

### Analysis & Clustering Parameters

Open `http://<server>:8000` to configure. Values below are tested on a ~6500 track library.

**Analysis Parameters**

| Parameter | Value |
|---|---|
| Number of Recent Albums | 5 |
| Top N Moods | 6 |

**Clustering Parameters**

| Parameter | Value | Notes |
|---|---|---|
| Clustering Algorithm | K-Means | most stable for 5k+ tracks |
| TOP Playlist Number | 20 | ~330 tracks/playlist |
| Clustering Runs | 10 | quality/speed balance |
| Max Distance | 0.65 | similarity radius |
| Max Songs Per Cluster | 0 | unlimited |
| PCA Components Min | 10 | |
| PCA Components Max | 40 | |
| Min Songs Per Genre for Stratification | 25 | |
| Stratified Sampling Target Percentile | 75 | |
| Use Embeddings for Clustering | false | uses standard audio features (BPM, key, energy); true only when CLAP_ENABLED=true in .env and Analysis re-run |

**Score Weights** (each group sums to 1.0)

| Parameter | Value |
|---|---|
| Diversity Score Weight | 0.4 |
| Purity Score Weight | 0.6 |
| Other Feature Diversity Weight | 0.4 |
| Other Feature Purity Weight | 0.6 |
| Silhouette Score Weight | 0.4 |
| Davies-Bouldin Score Weight | 0.3 |
| Calinski-Harabasz Score Weight | 0.3 |

**K-Means Specific**

| Parameter | Value |
|---|---|
| Min Clusters | 15 |
| Max Clusters | 50 |

> After changing clustering parameters re-run **Clustering** (Analysis not needed).
> If mixes are too diverse

> **CLAP** (Contrastive Language-Audio Pretraining) — Microsoft neural model that semantically
> understands audio content. With CLAP, clustering is based on actual sound rather than just
> tags/metadata. Requires AVX2 CPU support and `CLAP_ENABLED=true` in `application/.env`.
> The UI checkbox "Use Embeddings for Clustering" is independent — when CLAP is disabled it
> falls back to standard audio features. To enable: set `CLAP_ENABLED=true`, restart the stack,
> re-run **Analysis** to recompute embeddings, then re-run **Clustering**.
> If mixes are too diverse — raise Purity Score Weight to 0.7, lower Max Clusters to 35.

---

## External Setup

When Navidrome and AudioMuse-AI run on separate machines.

> **Proxmox:** AudioMuse-AI requires AVX2 — set VM CPU type to `host` in Hardware → Processors.

### Music machine

Use the same `application/docker-compose.yml` but remove the `audiomuse-flask`, `audiomuse-worker`, `redis`, and `postgres` services — keep only `navidrome` and `uploader`.

In Navidrome plugin settings, point the AudioMuse-AI URL to the AI machine: `http://<AI_MACHINE_IP>:8000`

`.env` only needs:
```env
LASTFM_APIKEY=your_key
LASTFM_SECRET=your_secret
```

### AI machine

Use the same `application/docker-compose.yml` but remove `navidrome` and `uploader` — keep only `audiomuse-flask`, `audiomuse-worker`, `redis`, and `postgres`.

Set `NAVIDROME_URL` to point at the music machine:
```env
NAVIDROME_HOST=192.168.1.XXX
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=yourpassword
POSTGRES_PASSWORD=yourdbpassword
AI_MODEL_PROVIDER=NONE
CLAP_ENABLED=true
```

```bash
docker compose build && docker compose up -d
```


---

## Android Client — Tempo

[TrueBad0ur/tempo](https://github.com/TrueBad0ur/tempo) is a personal fork of the (now
unmaintained) [CappielloAntonio/tempo](https://github.com/CappielloAntonio/tempo) Subsonic
client. The only change: a 4th **Web** button in the bottom nav, next to Home/Library/
Download, that opens `<your-server-address>/tools/` (the library web UI in `application/web/`)
in a Chrome Custom Tab — no server address is hardcoded, it reuses whatever server is already
configured in the app. See that repo's `BUILDING.md` for how to build and sign it.

---

## Web UI — application/web/

A small Flask app (`application/web/web.py` + `static/index.html`) served at `/tools/`, behind
the same Google SSO as the rest of the stack. Tabs:

- **Search** — search an artist, browse their popular albums (Last.fm), download any of them.
- **Library** — browse what's already on disk; per-album "check missing" against the catalog and
  fill in gaps.
- **Downloads** — live log of running/finished download jobs.
- **Cleanup** — run `prepare_music.py` preview/fix from the browser, plus a **Navidrome catalog
  duplicates** panel (see below).
- **Upload** — drag-and-drop staged uploads into the library (goes through the same tag-cleanup
  pipeline as `intake.py`).

Any album download (Search or Library) has a per-row **"Allow partial"** checkbox — same meaning
as `download_music.py --allow-partial`.

```bash
cd application
docker compose build web && docker compose up -d web
```

### Navidrome catalog duplicates

Overlapping rescans fired close together right after a fresh publish can race Navidrome's own
scanner into inserting the same track path twice — each row is individually valid (the file
really exists), so a normal rescan doesn't notice or merge them. Detected/fixed via
`prepare_music.py --navidrome-dupes-only [--fix]` or the Cleanup tab's button; fixing writes
directly to Navidrome's SQLite DB (`NAVIDROME_DB_PATH`, mounted read-write) using its own
WAL-mode locking, so the service doesn't need to be stopped.

---

## Music Metadata — prepare_music.py

Checks and fixes common metadata issues before adding tracks to Navidrome.

### Checks

| Issue | Fix |
|---|---|
| Broken encoding (cp1251 as latin-1) | Re-encodes field |
| Site watermarks in tags/filename (`[muzmo.ru]`) | Strips from tags and renames file |
| Broken chars (□ U+FFFD) | Strips from all fields |
| Multiple artists in one field (`;` `/` `,` `feat.`) | Splits into multi-value tag |
| Artist prefix in title tag (`Artist - Title`) | Strips prefix, sets correct title |
| Unknown artist | Extracts from directory path or filename |
| Wrong album / albumartist | Forces from directory structure |
| Artist tag case mismatch (e.g. `ПОРНОфИЛЬМЫ` vs folder `Порнофильмы`) | Forces exact folder name |
| Junk ID3 frames (COMM spam, USLT embedded lyrics, TPOS disc number) | Deletes frames |
| Variant filenames (duplicate with `(1)` suffix etc.) | Flags for manual review |
| Duplicate tracks in album (same title slug, different file) | Keeps better quality (format → bitrate), deletes worse |
| Wrong track numbers (validated against Last.fm tracklist) | Renumbers from Last.fm order |
| Missing tracks (in Last.fm tracklist but not on disk) | Downloads automatically on `--fix` |
| Bonus/live/remix tracks missing from Last.fm | Flagged as `[MISSING/BONUS]`, not downloaded |

> **USLT** (embedded lyrics) always indicates a lyric-video yt-dlp download — verify the audio content.

### Usage

```bash
cd prepare
cp .env.example .env   # set MUSIC_DIR

docker compose run --rm prepare /music                   # dry-run (all checks)
docker compose run --rm prepare /music --fix             # apply all fixes
docker compose run --rm prepare /music --fix --encoding-only
docker compose run --rm prepare /music --fix --artists-only
docker compose run --rm prepare /music --fix --album-only
```

After editing `prepare_music.py`, rebuild:
```bash
cd prepare && docker compose build && docker compose run --rm prepare /music
```

### Missing track download

When `--fix` is passed and Last.fm reports a missing track, the script downloads it automatically after scanning all albums:

```
[DOWNLOAD] Downloading 2 missing track(s)...
  → Порнофильмы — Это пройдёт
  → Порнофильмы — Доброе сердце
[DOWNLOAD] Done.
```

Renumbering for that album is deferred — re-run `--fix` after the download to apply correct track numbers.

Bonus/live/remix/acoustic tracks are never downloaded (flagged as `[MISSING/BONUS]`).

### Downloading new albums

```bash
# Download a specific album
docker compose run --rm prepare /music --download-album "Порнофильмы" "Как В Последний Раз"

# Download all albums for an artist
docker compose run --rm prepare /music --download-album "Порнофильмы" --all-albums
```

Album folders are created automatically as `Artist/YYYY - Album/`. After downloading, run `--fix` to normalize tags.

### Skipping an album

Place a `.skip` file inside an album folder to exclude it from track-number checks and downloads entirely:

```bash
touch "/music/Artist/Album/.skip"
```

Useful when the local files intentionally diverge from the Last.fm tracklist (e.g. manually curated version, local recordings).

### Keeping remix tracks

By default, cleanup (`scan_variants`/`scan_duplicates`) always drops a remix track in favor of the plain original. Place a `.keep-remixes` file inside an album folder to opt that album out — its remixes are then left alone (never grouped with, or preferred against, the original) while every other variant type (live, instrumental, demo, etc.) is still cleaned up as usual:

```bash
touch "/music/Artist/Album/.keep-remixes"
# or via CLI:
docker compose run --rm prepare /music/Artist/Album --keep-remixes
docker compose run --rm prepare /music/Artist/Album --unset-keep-remixes
```

When downloading a new album, pass `--keep-remixes` to `download_music.py` (or check "remixes" in the web UI, both in Search and per-album in Library) to set the marker automatically after publish.

### Directory structure for album/albumartist detection

```
music/
├── Artist/
│   └── Album/
│       └── track.mp3    →  album=Album, albumartist=Artist
├── All/                 ← playlist folder
└── Singles/             ← tracks with no identified album
```

Tracks in playlist folders get `album` forced to the folder name to avoid polluting real albums. To add more playlist folders edit `EXCLUDE_DIRS` in `prepare_music.py`.

---

## Downloading Music — download_music.py

Downloads tracks from YouTube via [yt-dlp](https://github.com/yt-dlp/yt-dlp), using [Last.fm](https://www.last.fm/api) for the official track listing.

### Prerequisites

```bash
pip install yt-dlp mutagen
apt install ffmpeg   # required by yt-dlp for audio conversion
```

If `yt-dlp` is installed to `~/.local/bin`, add it to PATH:
```bash
export PATH=$PATH:~/.local/bin
```

### Setup

```bash
cd prepare/download_music
cp .env.example .env
```

`.env`:
```env
MUSIC_DIR=/path/to/your/music
LASTFM_KEY=your_lastfm_api_key
```

Get a free Last.fm API key at: https://www.last.fm/api/account/create

### Usage

```bash
export LASTFM_KEY=your_key   # or pass --lastfm-key each time

# List all albums for an artist
python3 download_music.py --artist "Rammstein" --list-albums

# Download a full album (auto-creates music/Artist/Year - Album/ folder)
python3 download_music.py --album "Rammstein" "Mutter" --out /music

# Download into an exact folder (skips tracks already present)
python3 download_music.py --album "Rammstein" "Mutter" --dest /music/Rammstein/Mutter

# Download a single track into a folder
python3 download_music.py --track "Rammstein" "Du hast" --out /music/Rammstein/Mutter

# Dry-run — show what would be downloaded without doing anything
python3 download_music.py --album "Rammstein" "Mutter" --dest /music/Rammstein/Mutter --dry-run

# Publish whatever tracks succeed even if some fail, instead of discarding the whole album
python3 download_music.py --album "Rammstein" "Mutter" --out /music --allow-partial

# Pick a specific catalog when sources disagree on the tracklist
python3 download_music.py --album "Rammstein" "Mutter" --out /music --catalog-source musicbrainz
```

### How it works

1. Tracklist is verified against multiple catalogs (Last.fm, MusicBrainz, Deezer, iTunes) and requires either agreement between two of them or an explicit `--catalog-source` pick — avoids publishing a wrong/incomplete tracklist from a single bad source.
2. Compares against files already in `--dest` using slug-normalized matching (ignores case, punctuation, spaces, transliteration) — existing tracks are skipped.
3. For each missing track: searches YouTube (+YouTube Music, SoundCloud, Zaycev, Pesni.me), scores candidates on title/artist/duration match, downloads the best one as MP3 via yt-dlp/ffmpeg. Cyrillic and Japanese katakana titles are transliterated to romaji for matching against romanized upload titles; kanji titles fall back to duration + the artist's own YouTube "Topic" channel.
4. All tracks stage into a hidden temp folder first — the whole album is only moved into place once **every** track succeeds (pass `--allow-partial` to publish whatever did succeed instead). A lock file prevents two concurrent runs (a web click + a manual CLI run) from both redownloading the same album.
5. Writes canonical `artist`, `title`, `album`, `year`, `albumartist` ID3 tags — albumartist and filenames are derived from the artist's actual folder name, not the catalog's spelling, so a folder like "Junko Ohashi" never ends up with files tagged "大橋純子".

### After downloading

Always run `prepare_music.py --fix` after downloading a batch — it normalizes album/albumartist tags from the folder structure and strips any leftover YouTube metadata:

```bash
cd prepare
docker compose run --rm prepare /music --fix
```

Then trigger a rescan in Navidrome (Settings → Scan Library), or via API:
```bash
curl "http://localhost:4533/rest/startScan?u=admin&p=PASSWORD&v=1.16.1&c=myapp&f=json"
```

### File naming

Files are saved as `Artist - Title.mp3` inside the destination folder. The folder structure Navidrome uses for grouping is:

```
music/
└── ArtistName/
    └── AlbumName/          # or "YYYY - AlbumName" when year is known
        └── Artist - Title.mp3
```

### Skip logic

A track is skipped if a file whose name (after stripping the `Artist - ` prefix) slug-matches the Last.fm track title. This means `Rammstein - Du Hast.mp3` and `rammstein - du hast.flac` are both treated as present — the script won't re-download.

### Known limitations

- YouTube search may not find very obscure tracks or ones with unusual titles
- Tracks longer than 10 minutes are filtered out (catches full-album uploads misidentified as singles) — this can miss legitimate long tracks like live recordings
- The script downloads one track at a time sequentially (no parallel downloads)
- Kanji-titled tracks with no katakana and no candidate on the artist's own YouTube "Topic" channel can't be matched at all (no kanji→romaji dictionary) — those need a manual `--catalog-source` retry or are simply unavailable under a matchable title

### Docker

Downloading is built into the prepare container:

```bash
cd prepare
docker compose run --rm prepare /music --download-album "Rammstein" "Mutter"
```

---

## Yandex Cloud S3 Sync

`scripts/sync.sh` syncs the music library to Yandex Cloud Object Storage (ICE storage class) using `rclone`. Syncs directory by directory to keep memory usage low.

### Setup

```bash
sudo apt install rclone
```

`~/.config/rclone/rclone.conf`:
```ini
[yandex]
type = s3
provider = Other
access_key_id = <Access Key ID>
secret_access_key = <Secret Key>
region = ru-central1
endpoint = storage.yandexcloud.net
```

```bash
rclone lsd yandex:   # verify access
```

```bash
cp scripts/sync.conf.example scripts/sync.conf
nano scripts/sync.conf
```

```bash
LOGFILE="/home/user/navidrome/application/logs/s3_sync-$(date).log"
RCLONE="/usr/bin/rclone"
REMOTE="yandex"
BUCKET="bucket-name"
SRC="/home/user/navidrome/application/music/"
```

### Run

```bash
bash scripts/sync.sh
```

Cron (as user):
```bash
crontab -e
0 10 * * 3 /home/user/navidrome/application/scripts/sync.sh
```

Page cache flush (as root):
```bash
sudo crontab -e
0 4 * * * sync && echo 3 > /proc/sys/vm/drop_caches
```

### Download from S3

```bash
rclone copy yandex:bucket-name /path/to/music --progress
```

For large libraries use `screen` or `tmux`.

---

## Navidrome Maintenance

### Clear cache

`cache/` is owned by Navidrome's internal user — `rm` requires sudo:

```bash
docker compose stop navidrome
sudo rm -rf ./cache/*
sudo chown -R 1000:1000 ./cache
docker compose start navidrome
```

### Clean up stale DB entries

After metadata fixes Navidrome may retain stale records (duplicate paths, wrong album groupings). Run from `application/`:

```bash
python3 scripts/cleanup_db.py
```

Removes: duplicate paths, entries with wrong `album_artist`, orphaned album records, missing-file entries. Then trigger **Scan** in Navidrome.

### Rebuild library database

If cleanup isn't enough — full reindex:

```bash
docker compose stop navidrome
rm ./data/navidrome.db
docker compose start navidrome
```

> ⚠️ **Deletes all users, passwords, play counts, ratings, and playlists.** Only use on a fresh setup.

---

## Known Issues & Fixes

### AudioMuse-AI: `AttributeError: 'Job' object has no attribute 'get_id'`

Upstream RQ bug. Fixed automatically by `audiomuse-patch/Dockerfile` during image build.

---

### Navidrome: `converting NULL to string is unsupported` (probe_data)

Occurs on PR-5044 build when database was created on an older version.

```bash
docker exec -it navidrome sqlite3 /data/navidrome.db \
  "UPDATE media_file SET probe_data = '' WHERE probe_data IS NULL;"
docker compose restart navidrome
```

---

### AudioMuse-AI: SIGILL (exit code 132)

Requires AVX2. In Proxmox: VM → Hardware → Processors → CPU type: `host`.

---

## Environment Variables

| Variable | Description |
|---|---|
| `NAVIDROME_USER` | Navidrome username |
| `NAVIDROME_PASSWORD` | Navidrome password |
| `NAVIDROME_HOST` | IP of Navidrome machine (external setup only) |
| `POSTGRES_PASSWORD` | DB password (default: `audiomusepassword`) |
| `AI_MODEL_PROVIDER` | `NONE`, `OLLAMA`, `OPENAI`, `GEMINI`, `MISTRAL` |
| `CLAP_ENABLED` | Text search, requires AVX2 (default: `true`) |
| `LASTFM_APIKEY` | Last.fm API key (Navidrome integration) |
| `LASTFM_SECRET` | Last.fm API secret (Navidrome integration) |
| `LASTFM_KEY` | Last.fm API key (download_music.py) |
