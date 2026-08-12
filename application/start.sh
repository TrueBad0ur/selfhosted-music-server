#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in the values."
    exit 1
fi

echo "==> Creating data directories..."
mkdir -p audiomuse/postgres audiomuse/redis audiomuse/temp-flask audiomuse/temp-worker
mkdir -p data cache

echo "==> Pulling base images..."
docker compose pull --ignore-buildable

echo "==> Building patched AudioMuse-AI image..."
docker compose build --pull audiomuse-flask audiomuse-worker

echo "==> Starting all services..."
docker compose up -d

echo ""
echo "Services started:"
echo "  Navidrome:    http://localhost:4533"
echo "  AudioMuse-AI: http://localhost:8000"
echo "  Web UI:       http://localhost:8082/tools/  (behind Google SSO, via nginx)"
