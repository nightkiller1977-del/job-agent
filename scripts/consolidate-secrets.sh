#!/usr/bin/env bash
# Phase 1 migration: populate the central AI Commander secrets store from the
# per-app .env files, WITHOUT overwriting anything already in the store.
#
#   process/shell env  →  project .env  →  central store  (see SECRETS.md)
#
# This is safe and idempotent: it only ADDS keys that are missing (or empty) in the
# central store, never changing an existing non-empty value. Run it once (and again
# after adding new keys to an app's .env). YOU run this — it touches real secret
# values on your machine; it prints key NAMES only, never values.
#
# Usage:  bash scripts/consolidate-secrets.sh [--dry-run]

set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

STORE_DIR="${AICC_SECRETS_DIR:-$HOME/Library/Application Support/ai-command-center}"
STORE="$STORE_DIR/.env"

# Source .env files to pull keys from (add more here as needed).
SOURCES=(
  "$HOME/Dev/Projects/job-agent/.env"
  "$HOME/Dev/Projects/email-agent/.env"
)

mkdir -p "$STORE_DIR"
[[ -f "$STORE" ]] || { [[ $DRY_RUN -eq 1 ]] || : > "$STORE"; }

# Return 0 if KEY has a non-empty value in the store.
store_has() {
  local key="$1"
  [[ -f "$STORE" ]] || return 1
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=[^[:space:]].*" "$STORE"
}

added=()
for src in "${SOURCES[@]}"; do
  [[ -f "$src" ]] || { echo "• skip (absent): $src"; continue; }
  echo "• scanning: $src"
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"                 # ltrim
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    line="${line#export }"
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ -z "$key" ]] && continue
    # strip surrounding whitespace on value (leave inner quotes intact)
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    [[ -z "$val" ]] && continue                             # skip empty source values
    if store_has "$key"; then continue; fi                  # never overwrite
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "  would add: $key"
    else
      printf '%s=%s\n' "$key" "$val" >> "$STORE"
      echo "  added: $key"
    fi
    added+=("$key")
  done < "$src"
done

if [[ $DRY_RUN -eq 0 ]]; then
  chmod 600 "$STORE"
  echo "✓ store secured (chmod 600): $STORE"
fi
echo "✓ ${#added[@]} key(s) $([[ $DRY_RUN -eq 1 ]] && echo 'would be added' || echo 'added'). Names only; no values printed to logs."
echo "  Next: encrypt with SOPS+age (Phase 2) — see SECRETS.md."
