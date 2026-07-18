"""Phase 2b — one notification dispatcher.

A single entry point in front of the project's notification channels, with the three
properties the plan/review require:

  - FAIL-OPEN: a channel that errors is logged and skipped; a notification failure
    never changes an application result (dispatch() never raises).
  - DEDUPLICATED: repeated notices for the same event/attempt within a TTL are
    suppressed, so a retry loop can't spam the human.
  - THREE CLASSES: FYI, HUMAN_ACTION_REQUIRED, SUBMISSION_BLOCKED — each channel
    subscribes only to the classes it cares about.

This module does not rewrite notifier.py / reauth.py / session_watchdog.py; the
default channels delegate to the existing `notify_*` fan-out (status-file + Telegram
+ macOS). Migrating those modules' call sites onto this dispatcher is a follow-up
(deferred to avoid clobbering concurrent edits to those files).
"""
from __future__ import annotations

import hashlib
import logging
import time
from enum import Enum
from typing import Callable, Iterable

_log = logging.getLogger("job_agent.notifications")


class NoticeClass(str, Enum):
    FYI = "fyi"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    SUBMISSION_BLOCKED = "submission_blocked"


# Map a run event (from events.py / AttemptPhase outcomes) to a notice class.
# Returns None for events that are not worth notifying about.
_HUMAN_OUTCOMES = {
    "review_ready", "needs_answer", "needs_session", "needs_session_prep",
    "needs_hydration", "submission_unverified", "submit_in_progress",
    "login_required",   # generic adapter's pre-fill login wall needs a human
    # terminal 'Needs Human / Structural' outcomes: the application was NOT submitted
    "submit_not_found", "form_not_reached",
}
_BLOCKED_OUTCOMES = {
    "submit_denied_by_policy", "profile_locked", "credentials_missing",
    "captcha",           # a captcha/bot wall stops the attempt — surface it
}


def _is_auth_outcome(o: str) -> bool:
    return o.endswith("_login_required") or o.endswith("_session_expired") or o in (
        "session_expired", "credentials_missing",
    )


def notice_for(event: str, outcome: str = "") -> NoticeClass | None:
    o = (outcome or "").lower()
    if event in ("profile_locked",) or o in _BLOCKED_OUTCOMES:
        return NoticeClass.SUBMISSION_BLOCKED
    # An expired/locked ATS session needs a human to re-auth (prepare-sessions).
    if _is_auth_outcome(o) or o in _HUMAN_OUTCOMES:
        return NoticeClass.HUMAN_ACTION_REQUIRED
    if event == "attempt_finished" and o == "applied":
        return NoticeClass.FYI
    return None


class Channel:
    """A notification sink. `accepts` is the set of classes it handles."""

    def __init__(self, name: str, accepts: Iterable[NoticeClass], send: Callable[[dict], None]):
        self.name = name
        self._accepts = frozenset(accepts)
        self._send = send

    def accepts_class(self, cls: NoticeClass) -> bool:
        return cls in self._accepts

    def send(self, notice: dict) -> None:
        self._send(notice)


class Dispatcher:
    def __init__(self, channels: list[Channel] | None = None,
                 dedup_ttl: float = 900.0, now: Callable[[], float] = time.time):
        self.channels = list(channels) if channels is not None else _default_channels()
        self.dedup_ttl = dedup_ttl
        self._now = now
        self._seen: dict[str, float] = {}

    @staticmethod
    def _key(cls: NoticeClass, title: str, message: str, key: str | None) -> str:
        base = key or f"{cls.value}|{title}|{message}"
        return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()

    def dispatch(self, cls: NoticeClass, title: str, message: str = "",
                 key: str | None = None) -> dict:
        """Route a notice to accepting channels. Never raises. Returns a summary
        {deduped, sent, failed}."""
        dk = self._key(cls, title, message, key)
        now = self._now()
        # evict expired entries so a long-lived dispatcher's cache doesn't grow
        # unbounded (every distinct posting key would otherwise persist forever).
        if self._seen:
            self._seen = {k: t for k, t in self._seen.items() if (now - t) < self.dedup_ttl}
        last = self._seen.get(dk)
        if last is not None and (now - last) < self.dedup_ttl:
            return {"deduped": True, "sent": [], "failed": []}
        self._seen[dk] = now

        notice = {"cls": cls, "title": title, "message": message, "key": key}
        sent: list[str] = []
        failed: list[str] = []
        for ch in self.channels:
            try:
                if not ch.accepts_class(cls):
                    continue
                ch.send(notice)
                sent.append(ch.name)
            except Exception as e:  # FAIL-OPEN — never propagate a notification error
                _log.warning("notification channel %r failed: %s", getattr(ch, "name", "?"), e)
                failed.append(getattr(ch, "name", "?"))
        return {"deduped": False, "sent": sent, "failed": failed}

    def dispatch_event(self, event: str, outcome: str, title: str, message: str = "",
                       key: str | None = None) -> dict | None:
        """Convenience: map a run event -> class and dispatch, or skip if the event
        is not notify-worthy."""
        cls = notice_for(event, outcome)
        if cls is None:
            return None
        return self.dispatch(cls, title, message, key=key)


# --------------------------------------------------------------------------- #
# default channels — delegate to the existing notify_* fan-out (lazy import so
# importing this module never drags in the notifier's dependencies).
# --------------------------------------------------------------------------- #
def _default_channels() -> list[Channel]:
    def _info(n: dict) -> None:
        from src.notifier import notify_info
        notify_info(n["title"], n["message"])

    def _warn(n: dict) -> None:
        from src.notifier import notify_warning
        notify_warning(n["title"], n["message"])

    def _error(n: dict) -> None:
        from src.notifier import notify_error
        notify_error(n["title"], n["message"])

    return [
        Channel("notify_info", {NoticeClass.FYI}, _info),
        Channel("notify_warning", {NoticeClass.HUMAN_ACTION_REQUIRED}, _warn),
        Channel("notify_error", {NoticeClass.SUBMISSION_BLOCKED}, _error),
    ]
