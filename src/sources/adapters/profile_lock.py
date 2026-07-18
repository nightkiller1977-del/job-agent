"""Phase 0.4 — enforce single-Chrome-per-profile in code, not just docs.

ExternalApplySession binds the `jobright` Chrome profile, which JobrightScraper also
uses. Two Chrome processes on one profile cause the ProcessSingleton / "database is
locked" failure CLAUDE.md warns about. This is an exclusive, cross-process file lock
on the profile directory: acquire before launch, release in a finally.

Implemented with O_CREAT|O_EXCL (no third-party dep). A lock whose owning PID is dead
is treated as stale and reclaimed, so a crashed run does not wedge the profile forever.
"""
from __future__ import annotations

import contextvars
import os
import time
from pathlib import Path

# Lock paths held by the CURRENT logical operation (task/callers' context). Borrowing
# is allowed only when the acquiring code runs within the holder's context — a PID
# match alone is not enough, or two independent concurrent operations in one process
# would both "own" the profile and race (the second's _clear_profile_locks could
# pkill the first's live Chrome).
_HELD_IN_CONTEXT: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "profile_locks_held", default=frozenset()
)


# An empty lock file is tolerated for this many quick cycles (a create->write TOCTOU
# window) before being treated as a crashed-mid-create lock and reclaimed.
_EMPTY_GRACE_CYCLES = 3
_EMPTY_POLL = 0.05


class ProfileLockError(RuntimeError):
    """Raised when the profile is held by another live process."""


class ProfileLock:
    """Exclusive cross-process lock on a Chrome profile dir.

    REENTRANT within a logical operation: when the CURRENT context already holds
    this lock (e.g. ExternalApplySession holds it and BaseScraper._start_browser
    acquires again in the same call chain), acquire() is a borrow — depth-counted
    per lock path — and only the outermost release() removes the file. Two
    INDEPENDENT concurrent operations in the same process do NOT borrow from each
    other (context-scoped, not PID-scoped); the second waits/refuses like any
    foreign owner. Cross-process exclusivity is unchanged.
    """

    # per-process reentrancy depth, keyed by resolved lock path
    _depth: dict = {}

    def __init__(self, profile_dir: Path | str, *, timeout: float = 0.0, poll: float = 0.5):
        self.lock_path = Path(profile_dir).with_suffix(".applylock")
        self.timeout = timeout
        self.poll = poll
        self._acquired = False
        self._borrowed = False  # reentrant borrow: don't unlink on release
        self._ctx_token = None  # contextvar token held by the OUTERMOST acquisition

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

    def acquire(self) -> "ProfileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        empty_seen = 0
        key = str(self.lock_path.resolve())
        while True:
            if self._try_create():
                self._acquired = True
                ProfileLock._depth[key] = ProfileLock._depth.get(key, 0) + 1
                # mark this lock as held by the current logical operation so only
                # code in OUR call chain may borrow it
                self._ctx_token = _HELD_IN_CONTEXT.set(_HELD_IN_CONTEXT.get() | {key})
                return self
            owner = self._read_owner_pid()
            if owner == os.getpid() and key in _HELD_IN_CONTEXT.get():
                # reentrant: OUR call chain already holds this profile — borrow it.
                # (Same PID but a different concurrent operation does NOT match the
                # context and falls through to wait/refuse like a foreign owner.)
                self._acquired = True
                self._borrowed = True
                ProfileLock._depth[key] = ProfileLock._depth.get(key, 0) + 1
                return self
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
                time.sleep(_EMPTY_POLL)
                continue
            empty_seen = 0             # a live owner holds it
            if time.time() >= deadline:
                raise ProfileLockError(
                    f"profile locked by live PID {owner}: {self.lock_path} "
                    f"(refusing a second Chrome on this profile)"
                )
            time.sleep(self.poll)

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
        except FileNotFoundError:
            pass
        finally:
            if self._ctx_token is not None:
                try:
                    _HELD_IN_CONTEXT.reset(self._ctx_token)
                except ValueError:
                    pass  # released from a different context than acquire — best effort
                self._ctx_token = None
            self._acquired = False
            self._borrowed = False

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
