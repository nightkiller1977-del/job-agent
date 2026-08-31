#!/usr/bin/env bash
# One-time migration for existing checkouts: config.json used to be tracked in
# git; as of the public-repo cleanup it's gitignored (personal — copy from
# config.example.json). A plain `git pull`/merge that removes a path from the
# index also deletes it from disk when the local file is unmodified, which is
# the common case for a config.json nobody has edited outside of what's
# committed — so upgrading in place can silently delete your real target
# roles/compensation thresholds and leave the app running on defaults.
#
# Usage:
#   scripts/migrate-config-json.sh backup   # run BEFORE pulling/merging this change
#   scripts/migrate-config-json.sh restore  # run AFTER, only if config.json is now missing
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$HOME/.config/job-agent"
BACKUP_FILE="$BACKUP_DIR/config.json.backup"

action="${1:-}"

case "$action" in
  backup)
    if [ ! -f "$REPO_DIR/config.json" ]; then
      echo "No config.json at $REPO_DIR/config.json — nothing to back up."
      exit 0
    fi
    mkdir -p "$BACKUP_DIR"
    cp "$REPO_DIR/config.json" "$BACKUP_FILE"
    echo "✓ Backed up config.json to $BACKUP_FILE"
    echo "  Safe to pull/merge now."
    ;;
  restore)
    if [ -f "$REPO_DIR/config.json" ]; then
      echo "config.json already present at $REPO_DIR/config.json — nothing to restore."
      exit 0
    fi
    if [ ! -f "$BACKUP_FILE" ]; then
      echo "No backup found at $BACKUP_FILE — run 'backup' before pulling next time." >&2
      echo "If this is a fresh clone, copy config.example.json to config.json instead." >&2
      exit 1
    fi
    cp "$BACKUP_FILE" "$REPO_DIR/config.json"
    echo "✓ Restored config.json from $BACKUP_FILE"
    ;;
  *)
    echo "Usage: $0 {backup|restore}"
    exit 1
    ;;
esac
