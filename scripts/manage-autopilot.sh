#!/usr/bin/env bash
# Manage background launchd autopilot services for job-agent
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DISCOVER_PLIST="com.jobagent.discover.plist"
APPLY_PLIST="com.jobagent.apply.plist"

# Superseded by discover/apply above but scheduled at the same times
# (07:00 / 23:00) — if left installed they fire alongside the current jobs and
# cause duplicate discover/apply runs and Playwright browser contention.
LEGACY_LABELS=("com.jobagent.daily" "com.jobagent.night")

remove_legacy_daemons() {
  # Target removal by label, not by plist presence: the problem is a loaded
  # launchd service, which can outlive (or predate) its plist file on disk —
  # e.g. removed by hand, or installed from a different checkout. `launchctl
  # list <label>` succeeds iff the service is currently loaded regardless of
  # whether $LAUNCHD_DIR/<label>.plist still exists, so this stays idempotent.
  local removed=0
  for label in "${LEGACY_LABELS[@]}"; do
    if launchctl list "$label" >/dev/null 2>&1; then
      echo "Removing legacy daemon $label (superseded by discover/apply, same schedule)..."
      launchctl remove "$label" 2>/dev/null || true
      removed=1
    fi
    local plist="$LAUNCHD_DIR/$label.plist"
    if [ -f "$plist" ]; then
      rm -f "$plist"
      removed=1
    fi
  done
  return $removed
}

action="${1:-status}"

case "$action" in
  install)
    echo "Installing launchd daemons to $LAUNCHD_DIR..."
    mkdir -p "$LAUNCHD_DIR"
    remove_legacy_daemons || true
    # Rewrite both placeholders: __PROJECT_DIR__ to this checkout, and
    # __SOPS_AGE_KEY_FILE__ to this user's actual $HOME — see SECRETS.md Phase 2
    # for why launchd needs the latter set explicitly: secret_store._read_store()
    # only forwards SOPS_AGE_KEY_FILE to `sops -d` when it's already present in
    # the process env, and launchd jobs don't inherit a login shell's exports.
    # Harmless if secrets.enc.env / the age key don't exist yet — sops
    # decryption just fails closed to the existing plaintext central-store
    # fallback.
    sed -e "s|__PROJECT_DIR__|$REPO_DIR|g" \
        -e "s|__SOPS_AGE_KEY_FILE__|$HOME/.config/aicc/age.key|g" \
        "$REPO_DIR/launchd/$DISCOVER_PLIST" > "$LAUNCHD_DIR/$DISCOVER_PLIST"
    sed -e "s|__PROJECT_DIR__|$REPO_DIR|g" \
        -e "s|__SOPS_AGE_KEY_FILE__|$HOME/.config/aicc/age.key|g" \
        "$REPO_DIR/launchd/$APPLY_PLIST" > "$LAUNCHD_DIR/$APPLY_PLIST"

    if launchctl load -w "$LAUNCHD_DIR/$DISCOVER_PLIST" && launchctl load -w "$LAUNCHD_DIR/$APPLY_PLIST"; then
      echo "✓ Installed and successfully loaded for $REPO_DIR:"
      echo "  - Discovery pass: 07:00 AM daily"
      echo "  - Apply pass:     11:00 PM daily"
    else
      echo "⚠ Warning: Plists copied to $LAUNCHD_DIR, but launchctl load returned an error. Check launchctl permissions."
    fi
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
