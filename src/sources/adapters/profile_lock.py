"""Phase 0.4 — enforce single-Chrome-per-profile in code, not just docs.

ExternalApplySession binds the `jobright` Chrome profile, which JobrightScraper also
uses. Two Chrome processes on one profile cause the ProcessSingleton / "database is
locked" failure CLAUDE.md warns about. This is an exclusive, cross-process file lock
on the profile directory: acquire before launch, release in a finally.

Implemented with O_CREAT|O_EXCL (no third-party dep). A lock whose owning PID is dead
is treated as stale and reclaimed, so a crashed run does not wedge the profile forever.

Ownership model (same-process reentrancy):
  - Each OUTERMOST acquisition mints an operation token, records it in the global
    `_OWNERS[key]` registry, and adds it to the caller's `_OP_IDS` contextvar.
  - A borrow is allowed only when `_OWNERS[key]` is one of the tokens carried by the
    current call chain — so nested acquires (ExternalApplySession -> _start_browser)
    borrow, while an INDEPENDENT concurrent operation in the same process waits or
    refuses like a foreign owner.
  - Release clears the GLOBAL registry entry, so it works from any task/context
    (e.g. the shielded teardown task); a stale token left in some context is harmless
    because the registry no longer maps the key to it.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import time
import uuid
from pathlib import Path

# operation tokens carried by the current call chain (copied into child tasks)
_OP_IDS: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "profile_lock_op_ids", default=frozenset()
)
# key -> operation token of the current outermost holder (process-global)
_OWNERS: dict = {}

# An empty lock file is tolerated for this many quick cycles (a create->write TOCTOU
# window) before being treated as a crashed-mid-create lock and reclaimed.
_EMPTY_GRACE_CYCLES = 3
_EMPTY_POLL = 0.05


class ProfileLockError(RuntimeError):
    """Raised when the profile is held by another live process."""


class ProfileLock:
    """Exclusive cross-process lock on a Chrome profile dir.

    REENTRANT within a logical operation (see module docstring): nested acquires in
    the holder's call chain borrow — depth-counted per lock path — and only the
    outermost release() removes the file. Independent concurrent operations in the
    same process do NOT borrow from each other. Cross-process exclusivity unchanged.

    `acquire()` is the synchronous form (blocking waits); `acquire_async()` performs
    the same protocol with `await asyncio.sleep(...)` waits so contention never
    blocks the event loop.
    """

    # per-process reentrancy depth, keyed by resolved lock path
    _depth: dict = {}

    def __init__(self, profile_dir: Path | str, *, timeout: float = 0.0, poll: float = 0.5):
        self.lock_path = Path(profile_dir).with_suffix(".applylock")
        self.timeout = timeout
        self.poll = poll
        self._acquired = False
        self._borrowed = False  # reentrant borrow: don't unlink on release

    def _read_owner_pid(self) -> int | None:
        try:
            return int(Path(self.lock_path).read_text().strip() or "0") or None
        except Exception:
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user
        return True

    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True

    def _steal(self) -> None:
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    def _mark_acquired(self, key: str, *, borrowed: bool) -> None:
        self._acquired = True
        self._borrowed = borrowed
        ProfileLock._depth[key] = ProfileLock._depth.get(key, 0) + 1
        if not borrowed:
            # mint an operation token: the global registry names the owner; the
            # contextvar lets only OUR call chain (incl. child tasks, which copy
            # the context) borrow this lock.
            op = uuid.uuid4().hex
            _OWNERS[key] = op
            _OP_IDS.set(_OP_IDS.get() | {op})

    def _acquire_attempts(self):
        """Drive the acquisition protocol; yields sleep durations between attempts.
        Returns (having set acquired state) or raises ProfileLockError. The caller
        decides HOW to sleep (time.sleep vs asyncio.sleep)."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        empty_seen = 0
        key = str(self.lock_path.resolve())
        while True:
            if self._try_create():
                self._mark_acquired(key, borrowed=False)
                return
            owner = self._read_owner_pid()
            if owner == os.getpid() and _OWNERS.get(key) in _OP_IDS.get():
                # reentrant: OUR call chain holds this profile — borrow it. Same PID
                # but a different concurrent operation doesn't carry the owner token
                # and falls through to wait/refuse like a foreign owner.
                self._mark_acquired(key, borrowed=True)
                return
            if owner is not None and not self._pid_alive(owner):
                self._steal()          # owner PID is dead -> reclaim
                empty_seen = 0
                continue
            if owner is None:
                # Empty/unreadable: another process may have just created the file and
                # not yet written its PID (a create->write TOCTOU). Give it a few short
                # grace cycles before assuming a crash-mid-create and reclaiming — so we
                # don't steal a lock that is about to become valid.
                empty_seen += 1
                if empty_seen >= _EMPTY_GRACE_CYCLES:
                    self._steal()
                    empty_seen = 0
                    continue
                yield _EMPTY_POLL
                continue
            empty_seen = 0             # a live owner holds it
            if time.time() >= deadline:
                raise ProfileLockError(
                    f"profile locked by live PID {owner}: {self.lock_path} "
                    f"(refusing a second Chrome on this profile)"
                )
            yield self.poll

    def acquire(self) -> "ProfileLock":
        for delay in self._acquire_attempts():
            time.sleep(delay)
        return self

    async def acquire_async(self) -> "ProfileLock":
        """Same protocol as acquire(), but waits with asyncio.sleep so contention
        never blocks the event loop."""
        for delay in self._acquire_attempts():
            await asyncio.sleep(delay)
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        key = str(self.lock_path.resolve())
        depth = ProfileLock._depth.get(key, 1) - 1
        ProfileLock._depth[key] = depth
        try:
            # only the OUTERMOST holder removes the file; inner borrows just decrement.
            if depth <= 0 and self._read_owner_pid() == os.getpid():
                os.unlink(self.lock_path)
                ProfileLock._depth.pop(key, None)
                # clearing the GLOBAL registry revokes ownership from any context
                # still carrying the token — works from any task (shielded teardown).
                _OWNERS.pop(key, None)
        except FileNotFoundError:
            pass
        finally:
            self._acquired = False
            self._borrowed = False

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
