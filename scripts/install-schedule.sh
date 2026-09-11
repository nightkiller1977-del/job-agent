#!/usr/bin/env bash
# Install the job-agent daily schedule on ANY computer:
#   macOS → launchd (delegates to scripts/manage-autopilot.sh)
#   Linux → systemd user timers (renders systemd/*.service|*.timer templates)
#
# Both platforms run the same two jobs on the same schedule:
#   07:00  discover --no-review
#   23:00  apply --auto-submit
#
# Usage: scripts/install-schedule.sh [install|uninstall|status]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
action="${1:-install}"

if [ "$(uname -s)" = "Darwin" ]; then
  case "$action" in
    install)   exec "$REPO_DIR/scripts/manage-autopilot.sh" install ;;
    uninstall) exec "$REPO_DIR/scripts/manage-autopilot.sh" uninstall ;;
    *)         exec "$REPO_DIR/scripts/manage-autopilot.sh" status ;;
  esac
fi

# ── Linux / systemd ─────────────────────────────────────────────────────────
if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: neither macOS launchd nor systemd found — install a cron entry manually:" >&2
  echo "  0 7  * * *  cd $REPO_DIR && .venv/bin/python3 src/main.py discover --no-review >> state/discover.log 2>&1" >&2
  echo "  0 23 * * *  cd $REPO_DIR && .venv/bin/python3 src/main.py apply --auto-submit  >> state/apply.log 2>&1" >&2
  exit 1
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(jobagent-discover jobagent-apply)

# Same placeholder resolution as manage-autopilot.sh: exported value wins, else
# a sibling aicc-secrets checkout, else empty (resolver treats empty as unset).
SOPS_KEY="${SOPS_AGE_KEY_FILE:-$HOME/.config/aicc/age.key}"
SECRETS_DIR="${AICC_SECRETS_DIR:-}"
if [ -z "$SECRETS_DIR" ] && [ -d "$REPO_DIR/../aicc-secrets" ]; then
  SECRETS_DIR="$(cd "$REPO_DIR/../aicc-secrets" && pwd)"
fi

# Pin scheduled runs to the branch checked out at install time (override with
# an exported JOBAGENT_RUNTIME_BRANCH). scripts/run-scheduled.sh fails closed
# if the checkout drifts off this branch — dev work belongs in a worktree.
RUNTIME_BRANCH="${JOBAGENT_RUNTIME_BRANCH:-$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")}"

case "$action" in
  install)
    mkdir -p "$UNIT_DIR" "$REPO_DIR/state"
    for unit in "${UNITS[@]}"; do
      for ext in service timer; do
        sed -e "s|__PROJECT_DIR__|$REPO_DIR|g" \
            -e "s|__SOPS_AGE_KEY_FILE__|$SOPS_KEY|g" \
            -e "s|__AICC_SECRETS_DIR__|$SECRETS_DIR|g" \
            -e "s|__RUNTIME_BRANCH__|$RUNTIME_BRANCH|g" \
            "$REPO_DIR/systemd/$unit.$ext" > "$UNIT_DIR/$unit.$ext"
      done
    done
    systemctl --user daemon-reload
    systemctl --user enable --now jobagent-discover.timer jobagent-apply.timer
    echo "✓ Installed. Next runs:"
    systemctl --user list-timers 'jobagent-*' --no-pager
    # Without linger, user timers only run while a session for this user is
    # open. Enabling it makes the schedule truly unattended.
    if command -v loginctl >/dev/null 2>&1 && ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
      echo
      echo "NOTE: run 'loginctl enable-linger $USER' once so timers fire without an open login session."
    fi
    ;;
  uninstall)
    systemctl --user disable --now jobagent-discover.timer jobagent-apply.timer 2>/dev/null || true
    for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit.service" "$UNIT_DIR/$unit.timer"; done
    systemctl --user daemon-reload
    echo "✓ Uninstalled."
    ;;
  status|*)
    systemctl --user list-timers 'jobagent-*' --no-pager || true
    echo
    systemctl --user status jobagent-discover.service jobagent-apply.service --no-pager -n 5 2>/dev/null || true
    ;;
esac
