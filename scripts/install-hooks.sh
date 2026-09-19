#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$(git rev-parse --git-path hooks/pre-push)"
mkdir -p "$(dirname "$HOOK_PATH")"
cp "$REPO_ROOT/scripts/hooks/pre-push" "$HOOK_PATH"
chmod +x "$HOOK_PATH"
echo "Git hooks installed."
