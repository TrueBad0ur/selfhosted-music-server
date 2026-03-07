# Navidrome Self-Hosted

Self-hosted music server based on [Navidrome](https://www.navidrome.org/) with a web uploader and [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) integration for library analysis and similar track discovery.

## Repository Structure

```
.
├── internal-setup/     # Single-machine setup (Navidrome + AudioMuse-AI on the same host)
└── external-setup/     # Split setup (Navidrome and AudioMuse-AI on separate machines)
```

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
internal-setup/
├── docker-compose.yml
├── .env.example
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

### Quick Start

```bash
cd internal-setup
cp .env.example .env
# fill in NAVIDROME_USER and NAVIDROME_PASSWORD
docker compose up -d
```

> **Proxmox note:** AudioMuse-AI requires AVX2. Set the VM CPU type to **`host`** in Hardware → Processors.

---

## External Setup

Navidrome runs on the main music machine, AudioMuse-AI runs on a separate machine accessible by IP.

### Services

| File | Machine | Services |
|---|---|---|
| `docker-compose-main.yml` | Music machine | Navidrome, Uploader |
| `docker-compose-ai.yml` | AI machine | AudioMuse-AI, PostgreSQL, Redis |

### Directory Structure

```
external-setup/
├── docker-compose-main.yml
├── docker-compose-ai.yml
├── .env.example            # for the AI machine
├── data/                   # Navidrome data (on music machine)
│   └── plugins/
├── music/
├── cache/
├── audiomuse/              # AudioMuse-AI data (on AI machine)
└── uploader/
```

### Quick Start

**Music machine:**
```bash
cd external-setup
docker compose -f docker-compose-main.yml up -d
```

**AI machine:**
```bash
cd external-setup
cp .env.example .env
# fill in NAVIDROME_HOST, NAVIDROME_USER, NAVIDROME_PASSWORD
docker compose -f docker-compose-ai.yml up -d
```

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

## Yandex Cloud S3 Sync

`sync.sh` syncs the local music library to the `BUCKET-NAME` bucket in Yandex Cloud Object Storage using `rclone`.

- Storage class: **ICE** (cold storage, lowest cost)
- Logs are written to `logs/s3_sync-<date>.log`
- One-way sync: local → S3, skips already uploaded files (no extra PUT requests)
- Syncs directory by directory to keep memory usage low

### rclone Setup

```bash
sudo apt install rclone
```

# type in command line - rclone config
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
# create the music directory first
mkdir -p /home/user/navidrome/external-setup/music

# download everything from the bucket
rclone copy yandex:BUCKET-NAME /home/user/navidrome/external-setup/music --progress

# verify top-level directories were downloaded
ls /home/user/navidrome/external-setup/music
```

`--progress` shows real-time transfer speed and ETA. For large libraries run in a screen/tmux session so it survives SSH disconnects:

```bash
screen -S music-download
rclone copy yandex:BUCKET-NAME /home/user/navidrome/external-setup/music --progress
# detach: Ctrl+A D
# reattach later: screen -r music-download
```

### Manual Sync

```bash
bash sync.sh
```

### Scheduled Sync via cron

```bash
crontab -e
# every night at 3:00
0 3 * * * /home/user/navidrome/external-setup/sync.sh
```

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
