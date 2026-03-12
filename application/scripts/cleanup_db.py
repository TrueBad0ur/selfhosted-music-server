#!/usr/bin/env python3
"""
Clean up stale entries in Navidrome's database after metadata fixes.

Run from application/ directory:
    python3 scripts/cleanup_db.py

Then trigger a Scan in Navidrome.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "navidrome.db"

if not Path(DB_PATH).exists():
    print(f"ERROR: database not found at {DB_PATH}")
    print("Run from the application/ directory.")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("── Duplicate paths (same file, multiple DB entries) ─────────────────")
c.execute("SELECT path FROM media_file GROUP BY path HAVING COUNT(*) > 1")
dups = [row[0] for row in c.fetchall()]
if dups:
    for path in dups:
        correct_album = path.split('/')[-2]
        c.execute("SELECT id, album FROM media_file WHERE path = ? AND album != ?", (path, correct_album))
        for id_, album in c.fetchall():
            print(f"  [DEL] {path}\n        album='{album}' (expected '{correct_album}')")
            c.execute("DELETE FROM media_file WHERE id = ?", (id_,))
else:
    print("  none found")

print()
print("── Wrong album_artist (doesn't match artist folder in path) ─────────")
c.execute("SELECT id, path, album_artist FROM media_file")
rows = c.fetchall()
wrong_artist_deleted = 0
for id_, path, db_albumartist in rows:
    parts = path.split('/')
    if len(parts) < 3:
        continue
    correct = parts[-3]
    if db_albumartist and db_albumartist != correct:
        print(f"  [DEL] {path}\n        album_artist='{db_albumartist}' (expected '{correct}')")
        c.execute("DELETE FROM media_file WHERE id = ?", (id_,))
        wrong_artist_deleted += 1
if wrong_artist_deleted == 0:
    print("  none found")

print()
print("── Stale album entries (not referenced by any media_file) ───────────")
c.execute("""
    SELECT id, name, album_artist FROM album
    WHERE id NOT IN (
        SELECT DISTINCT album_id FROM media_file WHERE album_id IS NOT NULL
    )
""")
stale_albums = c.fetchall()
if stale_albums:
    for id_, name, album_artist in stale_albums:
        print(f"  [DEL] album='{name}' artist='{album_artist}'")
        c.execute("DELETE FROM album WHERE id = ?", (id_,))
else:
    print("  none found")

print()
print("── Missing files (marked missing=1 in DB) ───────────────────────────")
c.execute("SELECT path FROM media_file WHERE missing = 1")
missing = c.fetchall()
if missing:
    for (path,) in missing:
        print(f"  [DEL] {path}")
    c.execute("DELETE FROM media_file WHERE missing = 1")
else:
    print("  none found")

conn.commit()
conn.close()
print()
print("Done. Run Scan in Navidrome to reindex.")
