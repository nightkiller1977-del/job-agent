#!/usr/bin/env bash
# Fires a "push blocked" desktop notification. Takes the detail message as
# $1 — this must never be interpolated into the AppleScript source text:
# git branch names can contain '"' and AppleScript operators, which would
# let a malicious/local branch name run arbitrary AppleScript via osascript.
# The message is instead passed as a process argument (argv) into a fixed,
# constant AppleScript program.
set -uo pipefail

detail="${1:?usage: notify-push-blocked.sh <detail>}"

if command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical -- "Job Agent: push blocked" "$detail"
elif command -v osascript >/dev/null 2>&1; then
  osascript - "$detail" <<'APPLESCRIPT' 2>/dev/null
on run argv
  display notification (item 1 of argv) with title "Job Agent" subtitle "push blocked"
end run
APPLESCRIPT
fi

exit 0
