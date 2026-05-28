#!/bin/bash
# Migrate the spacepit sample bank to an external drive.
# Uses rsync (resumable, preserves metadata) + symlink (so all tools keep working).
#
# Usage:
#   ./tools/migrate-bank.sh /Volumes/T7
#
# After running, the bank lives at /Volumes/T7/spacepit-sample-bank,
# and ~/projects/spacepit-sample-bank is a symlink to it.
# All capture / pack / dashboard tools keep working unchanged.

set -e

TARGET_DRIVE="${1:-/Volumes/T7}"
BANK_SOURCE="$HOME/projects/spacepit-sample-bank"
BANK_DEST="$TARGET_DRIVE/spacepit-sample-bank"

# --- sanity checks ---
if [[ ! -d "$BANK_SOURCE" ]]; then
  echo "ERROR: source bank not found: $BANK_SOURCE"
  exit 1
fi

if [[ -L "$BANK_SOURCE" ]]; then
  echo "$BANK_SOURCE is already a symlink — already migrated."
  ls -la "$BANK_SOURCE"
  exit 0
fi

if [[ ! -d "$TARGET_DRIVE" ]]; then
  echo "ERROR: target drive not mounted: $TARGET_DRIVE"
  echo "Available volumes:"
  ls /Volumes
  exit 1
fi

# disk space check
BANK_SIZE_KB=$(du -sk "$BANK_SOURCE" | awk '{print $1}')
AVAIL_KB=$(df -k "$TARGET_DRIVE" | tail -1 | awk '{print $4}')
if (( AVAIL_KB < BANK_SIZE_KB * 2 )); then
  echo "ERROR: not enough free space on $TARGET_DRIVE"
  echo "  bank size:      $(du -sh "$BANK_SOURCE" | awk '{print $1}')"
  echo "  available:      $(df -h "$TARGET_DRIVE" | tail -1 | awk '{print $4}')"
  exit 1
fi

echo "=== Migration plan ==="
echo "  source: $BANK_SOURCE  ($(du -sh "$BANK_SOURCE" | awk '{print $1}'))"
echo "  dest:   $BANK_DEST"
echo "  free on target: $(df -h "$TARGET_DRIVE" | tail -1 | awk '{print $4}')"
echo
read -p "Proceed? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "aborted."
  exit 0
fi

# --- rsync (preserves perms, timestamps, resumable) ---
echo "Copying with rsync..."
mkdir -p "$BANK_DEST"
rsync -av --progress "$BANK_SOURCE/" "$BANK_DEST/"

# --- verify counts match ---
SRC_COUNT=$(find "$BANK_SOURCE" -type f | wc -l | tr -d ' ')
DST_COUNT=$(find "$BANK_DEST" -type f | wc -l | tr -d ' ')
echo
echo "File counts: source=$SRC_COUNT  dest=$DST_COUNT"
if (( SRC_COUNT != DST_COUNT )); then
  echo "WARN: counts differ. rsync may have errored. Check manually."
  echo "Source preserved — no symlink created."
  exit 1
fi

# --- backup source folder name, then symlink ---
BACKUP="${BANK_SOURCE}.bak.$(date +%s)"
mv "$BANK_SOURCE" "$BACKUP"
ln -s "$BANK_DEST" "$BANK_SOURCE"

echo
echo "✓ Migration complete."
echo "  bank lives at: $BANK_DEST"
echo "  symlink:        $BANK_SOURCE → $BANK_DEST"
echo "  backup:         $BACKUP"
echo
echo "All scripts (gmpack, gmcap, dashboard, etc.) keep working — they hit the symlink."
echo "If everything works for a few sessions, you can delete the backup:"
echo "    rm -rf $BACKUP"
