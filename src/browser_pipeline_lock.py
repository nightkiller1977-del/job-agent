"""Shared advisory lock for anything that drives the Playwright browser profile.

discover/apply/prepare-sessions/heartbeat (src/main.py) AND the commander's
auto-reauth path (AgentCommander.attempt_fix — called both from the
`commander fix` CLI subcommand and from StatusWatcher's poll loop) all launch
browser contexts against the same profile. They must all contend for the SAME
lock name, or e.g. a scheduled apply run holding the lock doesn't stop the
watcher's auto-fix from launching a second Chromium context at the same time —
recreating the browser contention this lock exists to prevent.

Each attempt_fix() call takes and releases the lock itself — the watcher's
infinite poll loop is never held under it, only the individual browser-driving
attempt within one iteration.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
BROWSER_PIPELINE_LOCK = "browser-pipeline"


@contextlib.contextmanager
def pipeline_lock(name: str = BROWSER_PIPELINE_LOCK):
    """Non-blocking advisory lock. Yields True if acquired, False if another
    holder currently has it. Callers should skip their browser-driving work
    on False rather than block — this guards interactive/scheduled/auto-fix
    work that shouldn't queue indefinitely behind each other."""
    lock_dir = PROJECT_ROOT / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        _log.info("pipeline_lock.busy name=%s lock=%s", name, lock_path)
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
