#!/usr/bin/env bash
# Fail-closed launcher for the scheduled discover/apply runs. Both platform
# schedulers call this ONE script (systemd/*.service templates and
# launchd/*.plist templates render __PROJECT_DIR__/scripts/run-scheduled.sh),
# so the guard logic is defined once and works on any machine or cloud host —
# no per-machine unit edits, no symlinks.
#
# Guard: a scheduled run must never execute whatever branch a dev session
# happened to leave checked out. The installer captures the branch at install
# time (JOBAGENT_RUNTIME_BRANCH, rendered into the unit/plist environment);
# if the checkout has drifted, the run REFUSES (exit 1) rather than running
# unreviewed code unattended — the same fail-closed posture as the
# submission ledger. An empty/unset JOBAGENT_RUNTIME_BRANCH skips the guard
# (manual invocation, cron fallback).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cmd="${1:?usage: run-scheduled.sh <discover|apply>}"

expected="${JOBAGENT_RUNTIME_BRANCH:-}"
if [ -n "$expected" ] && command -v git >/dev/null 2>&1 && [ -e "$REPO_DIR/.git" ]; then
  actual="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
  if [ "$actual" != "$expected" ]; then
    echo "REFUSING scheduled '$cmd': checkout is on '$actual', expected '$expected'." >&2
    echo "Switch $REPO_DIR back to '$expected' (dev work belongs in a worktree)," >&2
    echo "or re-run scripts/install-schedule.sh to re-pin the expected branch." >&2
    exit 1
  fi
fi

cd "$REPO_DIR"
case "$cmd" in
  discover) exec .venv/bin/python3 src/main.py discover --no-review ;;
  apply)    exec .venv/bin/python3 src/main.py apply --auto-submit ;;
  *) echo "unknown command: $cmd (expected discover|apply)" >&2; exit 2 ;;
esac
