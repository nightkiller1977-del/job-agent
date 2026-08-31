#!/bin/bash
# Automatic nightly job-agent run: discover new jobs, then apply to approved.
# Invoked by the com.jobagent.night launchd agent at 23:00 local.
# Logs to state/agent.night.log. Secrets come from .env (loaded by main.py)
# and from the launchd agent's EnvironmentVariables (DASHBOARD_URL / SYNC_SECRET).
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PROJECT_DIR}/.venv/bin/python3"
LOG="${PROJECT_DIR}/state/agent.night.log"

cd "${PROJECT_DIR}" || exit 1

echo "==================== NIGHT RUN $(date '+%Y-%m-%d %H:%M:%S') ====================" >> "${LOG}"

# 1) Discover + score new jobs across all sources (no terminal review in cron).
echo "--- discover ---" >> "${LOG}"
"${PY}" src/main.py discover --no-review >> "${LOG}" 2>&1

# 2) Apply to everything currently approved, submitting automatically.
#    A per-job failure no longer aborts the batch (see orchestrator apply loop).
echo "--- apply --auto-submit ---" >> "${LOG}"
"${PY}" src/main.py apply --auto-submit >> "${LOG}" 2>&1

echo "==================== NIGHT RUN DONE $(date '+%Y-%m-%d %H:%M:%S') ====================" >> "${LOG}"
