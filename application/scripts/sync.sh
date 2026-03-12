#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Local config (not in git) — copy sync.conf.example to sync.conf and edit
CONF="$SCRIPT_DIR/sync.conf"
if [ ! -f "$CONF" ]; then
    echo "ERROR: $CONF not found. Copy sync.conf.example and fill in the values."
    exit 1
fi
# shellcheck source=sync.conf
source "$CONF"

mkdir -p "$(dirname "$LOGFILE")"
exec >> "$LOGFILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') START ==="

if ! command -v "$RCLONE" &>/dev/null; then
    echo "ERROR: rclone not found at $RCLONE"
    exit 1
fi

ERRORS=0

for DIR in "$SRC"*/; do
    SUBDIR=$(basename "$DIR")
    echo "--- Syncing: $SUBDIR"
    "$RCLONE" copy "$DIR" "$REMOTE:$BUCKET/$SUBDIR" \
        --s3-storage-class ICE \
        --transfers 4 \
        --checkers 8 \
        --low-level-retries 3
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        echo "ERROR: sync failed for '$SUBDIR' with exit code $STATUS"
        ERRORS=$((ERRORS + 1))
    fi
done

if [ $ERRORS -ne 0 ]; then
    echo "ERROR: $ERRORS subdirectory/subdirectories failed"
else
    echo "OK: all directories synced successfully"
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') END ==="
echo
