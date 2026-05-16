# Navidrome Self-Hosted

## TODO

- [ ] **AcoustID fingerprinting** — identify tracks by audio content via MusicBrainz/AcoustID API. Flag files where actual audio does not match tagged artist. Requires `fpcalc` (chromaprint) in Dockerfile + AcoustID API key.
- [ ] **Refactoring** - split all there python functions into separate files
---


Self-hosted music server based on [Navidrome](https://www.navidrome.org/) with [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) integration for library analysis, instant mix, and similar track discovery.

## Repository Structure

```
.
├── application/            # Docker stack (Navidrome + AudioMuse-AI)
└── prepare/            # Metadata checker, fixer, and downloader (single Docker image)
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

### Tuning mix length

If mix loops after ~5 tracks, adjust at `http://<server>:8000`:

| Parameter | Tip |
|---|---|
| **TOP Playlist Number** | Fewer clusters → larger → more tracks per mix. Try 8–10. |
| **Max Distance** | Increase (e.g. 0.7–0.8) to widen similarity radius. |
| **Min Clusters** | Lower for larger clusters on a small library. |
| **Max Songs Per Cluster** | `0` = unlimited (recommended). |

After changing parameters re-run **Clustering** (Analysis not needed).

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
```

### How it works

1. **Last.fm** `album.getInfo` → fetches the official track list (title, artist, year)
2. Compares against files already in `--dest` using slug-normalized matching (ignores case, punctuation, spaces) — existing tracks are skipped
3. For each missing track: searches YouTube with `ytsearch5:Artist Title`, filters results to `duration < 600s` (avoids full-album uploads), downloads the first match as MP3 at best quality via ffmpeg
4. Renames the downloaded file to `Artist - Title.mp3`
5. Writes correct `artist`, `title`, `album`, `year` ID3 tags via mutagen

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
