"""Phase 0.4 — enforce single-Chrome-per-profile in code, not just docs.

ExternalApplySession binds the `jobright` Chrome profile, which JobrightScraper also
uses. Two Chrome processes on one profile cause the ProcessSingleton / "database is
locked" failure CLAUDE.md warns about. This is an exclusive, cross-process file lock
on the profile directory: acquire before launch, release in a finally.

Implemented with O_CREAT|O_EXCL (no third-party dep). A lock whose owning PID is dead
is treated as stale and reclaimed, so a crashed run does not wedge the profile forever.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


# An empty lock file is tolerated for this many quick cycles (a create->write TOCTOU
# window) before being treated as a crashed-mid-create lock and reclaimed.
_EMPTY_GRACE_CYCLES = 3
_EMPTY_POLL = 0.05


class ProfileLockError(RuntimeError):
    """Raised when the profile is held by another live process."""


class ProfileLock:
    def __init__(self, profile_dir: Path | str, *, timeout: float = 0.0, poll: float = 0.5):
        self.lock_path = Path(profile_dir).with_suffix(".applylock")
        self.timeout = timeout
        self.poll = poll
        self._acquired = False

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
        while True:
            if self._try_create():
                self._acquired = True
                return self
            owner = self._read_owner_pid()
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
        try:
            if self._read_owner_pid() == os.getpid():
                os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        finally:
            self._acquired = False

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
