# Navidrome Self-Hosted

Self-hosted music server based on [Navidrome](https://www.navidrome.org/) with a web uploader and [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) integration for library analysis and similar track discovery.

## Repository Structure

```
.
├── application/     # Single-machine setup (Navidrome + AudioMuse-AI on the same host)
├── external-setup/     # Split setup (Navidrome and AudioMuse-AI on separate machines)
└── prepare/            # Music metadata checker and fixer (run before adding to library)
```

---

## Installation (Internal Setup)

### 1. Prepare the music library

Before adding tracks to Navidrome, fix metadata issues (encoding, multi-artist tags, missing albums):

```bash
cd prepare
cp .env.example .env
# set MUSIC_DIR=/path/to/your/music
```

Dry-run — check only, no changes:
```bash
docker compose run --rm prepare
```

Apply fixes:
```bash
docker compose run --rm prepare /music --fix
```

See [prepare/](#music-metadata-prepare-script) for details.

### 2. Configure the stack

```bash
cd application
cp .env.example .env
```

Fill in `.env`:

```env
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=yourpassword
LASTFM_APIKEY=your_key
LASTFM_SECRET=your_secret
POSTGRES_PASSWORD=yourdbpassword
```

### 3. Download the AudioMuse-AI plugin

Download `audiomuseai.ndp` from [NeptuneHub/AudioMuse-AI-NV-plugin releases](https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases) and place it in:

```
application/data/plugins/audiomuseai.ndp
```

### 4. Start

```bash
./start.sh
```

`start.sh` will:
- Check that `.env` exists
- Create all required data directories
- Pull base Docker images
- Build the patched AudioMuse-AI image (fixes an upstream RQ bug)
- Start all services

### 5. Open Navidrome and configure the plugin

1. Open `http://<server>:4533`, log in, create an admin account on first run
2. Go to **Settings → Plugins → AudioMuse-AI** → enable → set URL: `http://audiomuse-flask:8000`

### 6. Run Analysis

1. Open `http://<server>:8000`
2. Click **Analysis and Clustering → Start Analysis** — scans audio features for all tracks (takes time, runs in background)
3. When analysis is complete, click **Clustering** — groups tracks and creates Navidrome playlists
4. **Instant Mix / Radio / Similar Artists** will appear on tracks in Navidrome

> After adding new tracks: only run **Analysis** again — it processes only new files. Clustering is optional and only needed to refresh playlist groupings.

### Tuning Mix length

The Instant Mix pulls tracks from the cluster of the current track. If a mix loops after ~5 tracks, adjust these parameters at `http://<server>:8000`:

| Parameter | Effect |
|---|---|
| **TOP Playlist Number** | Number of clusters created. Fewer clusters → larger clusters → more tracks per mix. Try 8–10 for a medium library. |
| **Max Distance** | Similarity radius for mix candidates. Increase (e.g. 0.7–0.8) to include more tracks. |
| **Min Clusters / Max Clusters** | K-Means range. Lower `Min Clusters` for larger clusters on a small library. |
| **Max Songs Per Cluster** | Hard cap per cluster. `0` = unlimited (recommended). |

After changing parameters, re-run **Clustering** (Analysis does not need to be repeated).

---

## Internal Setup

All services run on a single machine.

### Services

| Service | Port | Description |
|---|---|---|
| Navidrome | 4533 | Music server |
| AudioMuse-AI | 8000 | Analysis and clustering UI |
| Uploader | 8091 | Web uploader |

### Directory Structure

```
application/
├── docker-compose.yml
├── .env.example
├── start.sh
├── audiomuse-patch/    # Dockerfile for patched AudioMuse-AI image
├── data/               # Navidrome data (DB, plugins)
│   └── plugins/        # Navidrome plugin files (.ndp)
├── music/              # Music library
├── cache/              # Navidrome cache
├── audiomuse/          # AudioMuse-AI persistent data
│   ├── postgres/
│   ├── redis/
│   ├── temp-flask/
│   └── temp-worker/
└── uploader/
```

> **Proxmox note:** AudioMuse-AI requires AVX2. Set the VM CPU type to **`host`** in Hardware → Processors.

---

## External Setup

Use this when Navidrome and AudioMuse-AI run on separate machines (e.g. NAS for music, separate VM for AI processing).

### Overview

| Machine | Services |
|---|---|
| Music machine | Navidrome, Uploader |
| AI machine | AudioMuse-AI (flask + worker), PostgreSQL, Redis |

AudioMuse-AI requires AVX2. The music machine does not.

### Music machine — docker-compose.yml

Create the following directory structure:

```
navidrome/
├── docker-compose.yml
├── .env
├── data/
│   └── plugins/        # place audiomuseai.ndp here
├── music/
├── cache/
└── uploader/
```

`docker-compose.yml`:

```yaml
version: "3.8"

services:
  navidrome:
    image: deluan/navidrome:pr-5044
    container_name: navidrome
    user: "1000:1000"
    restart: always
    ports:
      - "4533:4533"
    volumes:
      - ./data:/data
      - ./music:/music:ro
      - ./cache:/cache
    environment:
      ND_MUSICFOLDER: /music
      ND_DATAFOLDER: /data
      ND_CACHEFOLDER: /cache
      ND_LOGLEVEL: info
      ND_DEFAULTTHEME: Spotify-ish
      ND_PLUGINS_ENABLED: "true"
      ND_PLUGINS_AUTORELOAD: "true"
      ND_AGENTS: "audiomuseai,lastfm,spotify,deezer"
      ND_LASTFM_ENABLED: "true"
      ND_LASTFM_APIKEY: "${LASTFM_APIKEY}"
      ND_LASTFM_SECRET: "${LASTFM_SECRET}"
      ND_COVERARTPRIORITY: "folder,embedded,external"
      ND_MUSICBRAINZ_ENABLED: "true"
      ND_MUSICBRAINZ_COVERART_ENABLED: "true"

  uploader:
    image: python:3.11-slim
    container_name: navidrome_uploader
    working_dir: /app
    restart: always
    user: "1000:1000"
    volumes:
      - ./uploader:/app
      - ./music/All/All:/uploads
    command: python3 /app/uploader.py
    ports:
      - "8091:8091"
```

`.env`:
```env
LASTFM_APIKEY=your_key
LASTFM_SECRET=your_secret
```

Plugin configuration in Navidrome UI: **Settings → Plugins → AudioMuse-AI** → set URL to `http://<AI_MACHINE_IP>:8000`.

---

### AI machine — docker-compose.yml

Create the following directory structure:

```
audiomuse/
├── docker-compose.yml
├── .env
├── audiomuse-patch/
│   └── Dockerfile
└── audiomuse/
    ├── postgres/
    ├── redis/
    ├── temp-flask/
    └── temp-worker/
```

`audiomuse-patch/Dockerfile` (applies the upstream RQ bug fix — see [Known Issues](#audiomuse-ai-attributeerror-job-object-has-no-attribute-get_id)):
```dockerfile
FROM ghcr.io/neptunehub/audiomuse-ai:latest
RUN grep -rl 'job\.get_id()' /app/ | xargs sed -i 's/job\.get_id()/job.id/g'
```

`docker-compose.yml`:

```yaml
version: "3.8"

services:
  redis:
    image: redis:7-alpine
    container_name: audiomuse-redis
    volumes:
      - ./audiomuse/redis:/data
    restart: unless-stopped

  postgres:
    image: postgres:15-alpine
    container_name: audiomuse-postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-audiomuse}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-audiomusepassword}
      POSTGRES_DB: ${POSTGRES_DB:-audiomusedb}
    volumes:
      - ./audiomuse/postgres:/var/lib/postgresql/data
    restart: unless-stopped

  audiomuse-flask:
    build: ./audiomuse-patch
    container_name: audiomuse-ai-flask
    ports:
      - "8000:8000"
    environment:
      SERVICE_TYPE: "flask"
      MEDIASERVER_TYPE: "navidrome"
      NAVIDROME_URL: "http://${NAVIDROME_HOST}:4533"
      NAVIDROME_USER: "${NAVIDROME_USER}"
      NAVIDROME_PASSWORD: "${NAVIDROME_PASSWORD}"
      POSTGRES_USER: ${POSTGRES_USER:-audiomuse}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-audiomusepassword}
      POSTGRES_DB: ${POSTGRES_DB:-audiomusedb}
      POSTGRES_HOST: "postgres"
      POSTGRES_PORT: "5432"
      REDIS_URL: "redis://redis:6379/0"
      AI_MODEL_PROVIDER: "${AI_MODEL_PROVIDER:-NONE}"
      CLAP_ENABLED: "${CLAP_ENABLED:-true}"
      TEMP_DIR: "/app/temp_audio"
    volumes:
      - ./audiomuse/temp-flask:/app/temp_audio
    depends_on:
      - redis
      - postgres
    restart: unless-stopped

  audiomuse-worker:
    build: ./audiomuse-patch
    container_name: audiomuse-ai-worker
    environment:
      SERVICE_TYPE: "worker"
      MEDIASERVER_TYPE: "navidrome"
      NAVIDROME_URL: "http://${NAVIDROME_HOST}:4533"
      NAVIDROME_USER: "${NAVIDROME_USER}"
      NAVIDROME_PASSWORD: "${NAVIDROME_PASSWORD}"
      POSTGRES_USER: ${POSTGRES_USER:-audiomuse}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-audiomusepassword}
      POSTGRES_DB: ${POSTGRES_DB:-audiomusedb}
      POSTGRES_HOST: "postgres"
      POSTGRES_PORT: "5432"
      REDIS_URL: "redis://redis:6379/0"
      AI_MODEL_PROVIDER: "${AI_MODEL_PROVIDER:-NONE}"
      CLAP_ENABLED: "${CLAP_ENABLED:-true}"
      TEMP_DIR: "/app/temp_audio"
    volumes:
      - ./audiomuse/temp-worker:/app/temp_audio
    depends_on:
      - redis
      - postgres
    restart: unless-stopped
```

`.env`:
```env
# IP of the Navidrome machine
NAVIDROME_HOST=192.168.1.XXX
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=yourpassword

POSTGRES_PASSWORD=yourdbpassword

# AI provider: NONE, OLLAMA, OPENAI, GEMINI, MISTRAL
AI_MODEL_PROVIDER=NONE

# CLAP text search (requires AVX2, set false to disable)
CLAP_ENABLED=true
```

Start:
```bash
docker compose build && docker compose up -d
```

> **Proxmox note:** AudioMuse-AI requires AVX2. Set the VM CPU type to **`host`** in Hardware → Processors.

---

## AudioMuse-AI Plugin for Navidrome

The plugin adds Instant Mix, Radio, and Similar Artists features to Navidrome.

### Installation

1. Download `audiomuseai.ndp` from [NeptuneHub/AudioMuse-AI-NV-plugin releases](https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases)
2. Place it in `./data/plugins/`
3. Restart the stack — the plugin is loaded automatically

### Configuration in Navidrome UI

Navidrome → Settings → Plugins → AudioMuse-AI → enable → set URL:
- **Internal setup:** `http://audiomuse-flask:8000`
- **External setup:** `http://<AI_MACHINE_IP>:8000`

### First Run

1. Open `http://<server>:8000`
2. **Analysis and Clustering → Start Analysis** — scans audio features for the entire library (takes time depending on library size)
3. Once analysis is complete, run **Clustering** — groups tracks and creates playlists in Navidrome
4. Instant Mix will appear on any track in Navidrome

---

## Last.fm Integration

Last.fm is used for artist metadata: biography, similar artists, and artist images.

Get a free API key at [last.fm/api/account/create](https://www.last.fm/api/account/create). For the registration form, any values work for homepage and callback URL (e.g. `http://localhost`).

Add to `.env`:

```env
LASTFM_APIKEY=your_api_key
LASTFM_SECRET=your_secret
```

Cover art priority is configured as `folder,embedded,external` — Navidrome tries local sources first, then external (MusicBrainz Cover Art Archive + Last.fm).

---

## Music Metadata — Prepare Script

Located in `prepare/`. Checks and fixes common metadata issues before adding tracks to Navidrome.

### What it fixes

| Issue | Detection | Fix |
|---|---|---|
| Broken encoding (cp1251 as latin-1) | ≥90% of alpha chars are non-ASCII | Re-encodes field |
| Site watermarks in tags | `[muzmo.ru]`, `[zaycev.net]`, etc. | Strips from all fields |
| Broken chars (□ U+FFFD) | Replacement character in tag values | Strips from all fields |
| Multiple artists in one field | `;` / `/` / `,` / `feat.` separators | Splits into multi-value tag |
| Unknown artist | Tag is empty or "Unknown Artist" | Extracts from filename or directory path |
| Missing album | Tag is empty or "Unknown Album" | Uses track title as album name |

### Usage

```bash
cd prepare
cp .env.example .env       # set MUSIC_DIR
```

Dry-run (report only):
```bash
docker compose run --rm prepare
```

Apply all fixes:
```bash
docker compose run --rm prepare /music --fix
```

Fix only encoding:
```bash
docker compose run --rm prepare /music --fix --encoding-only
```

Fix only artist tags:
```bash
docker compose run --rm prepare /music --fix --artists-only
```

Fix only missing album tags:
```bash
docker compose run --rm prepare /music --fix --album-only
```

After updating `prepare_music.py`, rebuild the image before running:
```bash
docker compose build --no-cache && docker compose run --rm prepare /music
```

---

## Yandex Cloud S3 Sync

`sync.sh` syncs the local music library to the `bucket-name` bucket in Yandex Cloud Object Storage using `rclone`.

- Storage class: **ICE** (cold storage, lowest cost)
- Logs are written to `logs/s3_sync-<date>.log`
- One-way sync: local → S3, skips already uploaded files (no extra PUT requests)
- Syncs directory by directory to keep memory usage low

### sync.conf

Paths and settings are stored in `scripts/sync.conf` (not tracked by git). Copy the example and edit:

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

### rclone Setup

```bash
sudo apt install rclone
```

Create `~/.config/rclone/rclone.conf`:

```ini
[yandex]
type = s3
provider = Other
access_key_id = <Access Key ID>
secret_access_key = <Secret Key>
region = ru-central1
endpoint = storage.yandexcloud.net
```

### Verify Access

```bash
rclone lsd yandex:
```

### Download from S3 (initial setup on a new machine)

Install rclone, configure `~/.config/rclone/rclone.conf` as above, then:

```bash
mkdir -p /home/user/navidrome/application/music
rclone copy yandex:bucket-name /home/user/navidrome/application/music --progress
sync && echo 3 > /proc/sys/vm/drop_caches
ls /home/user/navidrome/application/music
```

For large libraries run in a tmux/screen session:

```bash
screen -S music-download
rclone copy yandex:bucket-name /home/user/navidrome/application/music --progress
# detach: Ctrl+A D
# reattach: screen -r music-download
```

### Manual Sync

```bash
bash scripts/sync.sh
```

### Scheduled Sync via cron

Run sync as your user (rclone reads `~/.config/rclone/rclone.conf`):

```bash
crontab -e
# every Wednesday at 10:00
0 10 * * 3 /home/user/navidrome/application/scripts/sync.sh
```

### Page cache cleanup

The sync script no longer calls `drop_caches` directly (requires root). Set up a separate root cron to flush page cache daily:

```bash
sudo crontab -e
# every day at 04:00
0 4 * * * sync && echo 3 > /proc/sys/vm/drop_caches
```

---

## Clearing Navidrome Cache

The `cache/` directory is written by Navidrome running as its internal user (not your host user), so `rm -rf cache/*` will fail with a permission error. Use `sudo` or delete via Docker:

```bash
docker compose stop navidrome
sudo rm -rf ./cache/*
sudo chown -R 1000:1000 ./cache
docker compose start navidrome
```

> After `sudo rm`, the `cache/` directory is owned by root. Navidrome runs as `1000:1000` and will fail to start with `permission denied` unless ownership is restored.

### Removing stale database entries (without deleting the database)

After running the prepare script (renaming files, fixing tags), Navidrome may retain stale entries: duplicate paths, wrong album groupings, or missing files. Use the cleanup script:

```bash
cd application
python3 scripts/cleanup_db.py
```

The script removes:
- Duplicate path entries (e.g. old `(pesni.fm)` filenames after rename)
- Entries where `album_artist` in DB doesn't match the artist folder in the file path
- Stale album entries no longer referenced by any track
- Entries for files marked as missing

All output is printed so you can see exactly what was deleted. After running — trigger **Scan** in Navidrome. Users, playlists, and settings are not affected.

---

### Rebuilding the library database

If you renamed or moved files (e.g. after running the prepare script), Navidrome will show the old entries as grey/missing even after a rescan — because the file paths are stored in `navidrome.db`. Clearing the cache does not fix this.

To force a full reindex from scratch:

```bash
docker compose stop navidrome
rm ./data/navidrome.db
docker compose start navidrome
```

> ⚠️ **WARNING: This deletes the entire Navidrome database!** All users, passwords, play counts, ratings, starred tracks, and playlists will be permanently lost. You will need to recreate all accounts after restart. Only do this on a fresh setup or if you are prepared to reconfigure everything from scratch.

> **Recommended workflow:** run the prepare script on your music library *before* starting Navidrome for the first time, or before adding new files to an already-running instance.

---

## Known Issues & Fixes

### AudioMuse-AI: `AttributeError: 'Job' object has no attribute 'get_id'`

RQ (the job queue library) dropped `job.get_id()` in newer versions; the correct attribute is `job.id`. This is an upstream bug in AudioMuse-AI (unfiled as of March 2026).

**Fix:** The `audiomuse-patch/Dockerfile` patches all affected files automatically during image build. The `start.sh` script builds this image before starting the stack. No manual action needed.

---

### Navidrome: `sql: Scan error on column index 67, name "probe_data": converting NULL to string is unsupported`

Occurs when opening an album in the PR-5044 build if the database was created on an older version. The `probe_data` column was added without a default value migration.

**Fix:** Run once after upgrading:

```bash
docker exec -it navidrome sqlite3 /data/navidrome.db "UPDATE media_file SET probe_data = '' WHERE probe_data IS NULL;"
docker compose restart navidrome
```

---

### AudioMuse-AI: SIGILL (exit code 132) on start

The ML libraries require AVX2 CPU instructions. In a Proxmox VM, the default CPU type does not expose AVX2.

**Fix:** In Proxmox → VM → Hardware → Processors, set CPU type to **`host`**. This passes through the host CPU flags including AVX2.

---

### S3 sync runs out of memory

`s3cmd` loads the full file list into RAM, which causes OOM on large libraries.

**Fix:** Use `rclone` with per-directory sync as provided in `sync.sh`. Rclone streams file lists and uses constant memory regardless of library size.

---

## Environment Variables

| Variable | Description |
|---|---|
| `NAVIDROME_USER` | Navidrome username (required) |
| `NAVIDROME_PASSWORD` | Navidrome password (required) |
| `NAVIDROME_HOST` | IP of the Navidrome machine — external setup only (required) |
| `POSTGRES_PASSWORD` | Database password (default: `audiomusepassword`) |
| `AI_MODEL_PROVIDER` | AI provider: `NONE`, `OLLAMA`, `OPENAI`, `GEMINI`, `MISTRAL` |
| `CLAP_ENABLED` | Text search by description, requires AVX2 (default: `true`) |
| `LASTFM_APIKEY` | Last.fm API key for artist metadata and cover art |
| `LASTFM_SECRET` | Last.fm API secret |
