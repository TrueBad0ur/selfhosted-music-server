# Navidrome Self-Hosted

Self-hosted music server based on [Navidrome](https://www.navidrome.org/) with [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) integration for library analysis, instant mix, and similar track discovery.

## Repository Structure

```
.
├── application/    # Docker stack (single-machine setup)
└── prepare/        # Music metadata checker and fixer
```

---

## Quick Start

### 1. Prepare music metadata

```bash
cd prepare
cp .env.example .env   # set MUSIC_DIR=/path/to/your/music
docker compose run --rm prepare /music         # dry-run
docker compose run --rm prepare /music --fix   # apply fixes
```

### 2. Configure the stack

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

### 3. Add the AudioMuse-AI plugin

Download `audiomuseai.ndp` from [AudioMuse-AI-NV-plugin releases](https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases) → place in `application/data/plugins/`.

### 4. Start

```bash
cd application
./start.sh
```

| Service | Port |
|---|---|
| Navidrome | 4533 |
| AudioMuse-AI | 8000 |
| Uploader | 8091 |

### 5. Configure plugin in Navidrome

**Settings → Plugins → AudioMuse-AI** → enable → URL: `http://audiomuse-flask:8000`

### 6. Run Analysis

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

Directory structure:
```
navidrome/
├── docker-compose.yml
├── .env
├── data/plugins/   # place audiomuseai.ndp here
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

Plugin URL in Navidrome: `http://<AI_MACHINE_IP>:8000`

### AI machine

Directory structure:
```
audiomuse/
├── docker-compose.yml
├── .env
├── audiomuse-patch/Dockerfile
└── audiomuse/{postgres,redis,temp-flask,temp-worker}/
```

`audiomuse-patch/Dockerfile`:
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
    depends_on: [redis, postgres]
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
    depends_on: [redis, postgres]
    restart: unless-stopped
```

`.env`:
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

## Music Metadata — Prepare Script

Checks and fixes common metadata issues before adding tracks to Navidrome.

| Issue | Fix |
|---|---|
| Broken encoding (cp1251 as latin-1) | Re-encodes field |
| Site watermarks in tags/filename (`[muzmo.ru]`) | Strips from tags and renames file |
| Broken chars (□ U+FFFD) | Strips from all fields |
| Multiple artists in one field (`;` `/` `,` `feat.`) | Splits into multi-value tag |
| Artist prefix in title tag (`Artist - Title`) | Strips prefix, sets correct title |
| Unknown artist | Extracts from directory path or filename |
| Wrong album / albumartist | Forces from directory structure |

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
docker compose build --no-cache && docker compose run --rm prepare /music
```

### Directory structure for album/albumartist detection

The script enforces tags from the path:
```
music/Artist/Album/track.mp3  →  album=Album, albumartist=Artist
```

Flat playlist folders (not enforced): `All`, `Garazh`, `ReverseDungeon`, `Classics`, `TexnoFunk` — tracks in these get `album` set to the folder name to avoid polluting real albums.

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
| `LASTFM_APIKEY` | Last.fm API key |
| `LASTFM_SECRET` | Last.fm API secret |
