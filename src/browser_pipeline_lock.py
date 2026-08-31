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

import asyncio
import contextlib
import fcntl
import logging
import time
from pathlib import Path

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
BROWSER_PIPELINE_LOCK = "browser-pipeline"

# Default bound for pipeline_lock_wait() — how long a scheduled one-shot run
# (discover/apply) will wait for a busy lock before giving up. Those only fire
# once via launchd (23:00/07:00), so silently skipping on contention costs a
# full day; a bounded wait is worth it. Contrast with plain pipeline_lock(),
# used by interactive/continuously-polling callers (prepare-sessions,
# heartbeat, commander auto-fix) where an immediate skip-and-retry-later is
# the right behavior instead.
DEFAULT_WAIT_TIMEOUT_SECONDS = 600
DEFAULT_WAIT_POLL_SECONDS = 10


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


@contextlib.asynccontextmanager
async def pipeline_lock_wait(
    name: str = BROWSER_PIPELINE_LOCK,
    timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS,
):
    """Like pipeline_lock(), but retries (via asyncio.sleep, so it doesn't
    block the event loop) for up to timeout_seconds before giving up. Yields
    True as soon as the lock is acquired — held for the duration of the
    caller's `async with` block, same as pipeline_lock() — or False once the
    deadline passes without acquiring it."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        with pipeline_lock(name) as acquired:
            if acquired:
                yield True
                return
        if time.monotonic() >= deadline:
            _log.warning(
                "pipeline_lock.wait_timed_out name=%s timeout_seconds=%s",
                name, timeout_seconds,
            )
            yield False
            return
        await asyncio.sleep(poll_seconds)
