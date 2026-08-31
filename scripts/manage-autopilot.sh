#!/usr/bin/env bash
# Manage background launchd autopilot services for job-agent
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DISCOVER_PLIST="com.jobagent.discover.plist"
APPLY_PLIST="com.jobagent.apply.plist"

action="${1:-status}"

case "$action" in
  install)
    echo "Installing launchd daemons to $LAUNCHD_DIR..."
    mkdir -p "$LAUNCHD_DIR"
    sed -e "s|/Users/alarkins/Dev/Projects/job-agent|$REPO_DIR|g" "$REPO_DIR/launchd/$DISCOVER_PLIST" > "$LAUNCHD_DIR/$DISCOVER_PLIST"
    sed -e "s|/Users/alarkins/Dev/Projects/job-agent|$REPO_DIR|g" "$REPO_DIR/launchd/$APPLY_PLIST" > "$LAUNCHD_DIR/$APPLY_PLIST"
    launchctl load -w "$LAUNCHD_DIR/$DISCOVER_PLIST" 2>/dev/null || true
    launchctl load -w "$LAUNCHD_DIR/$APPLY_PLIST" 2>/dev/null || true
    echo "✓ Installed and loaded for $REPO_DIR:"
    echo "  - Discovery pass: 07:00 AM daily"
    echo "  - Apply pass:     11:00 PM daily"
    ;;
  uninstall)
    echo "Unloading and removing launchd daemons..."
    launchctl unload "$LAUNCHD_DIR/$DISCOVER_PLIST" 2>/dev/null || true
    launchctl unload "$LAUNCHD_DIR/$APPLY_PLIST" 2>/dev/null || true
    rm -f "$LAUNCHD_DIR/$DISCOVER_PLIST" "$LAUNCHD_DIR/$APPLY_PLIST"
    echo "✓ Uninstalled autopilot launchd daemons."
    ;;
  status)
    cd "$REPO_DIR"
    .venv/bin/python3 src/main.py autopilot-status --verbose
    ;;
  *)
    echo "Usage: $0 {install|uninstall|status}"
    exit 1
    ;;
esac
