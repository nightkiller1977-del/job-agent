"""
StatusWatcher — polls state/agent_status.json and reacts to new failures.

Detects new error-level alerts and reauth failures written by the agent,
calls AgentCommander to diagnose them, and optionally triggers an auto-fix.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .commander import AgentCommander
from .notifier import _load_status

log = logging.getLogger(__name__)

_KNOWN_SOURCES = ("linkedin", "jobright", "indeed", "usajobs")


class StatusWatcher:
    """Poll agent_status.json, detect new failures, and optionally auto-fix them."""

    def __init__(
        self,
        config: dict,
        poll_interval: int = 30,
        auto_fix: bool = True,
    ) -> None:
        self.config = config
        self.poll_interval = poll_interval
        self.auto_fix = auto_fix
        self._commander = AgentCommander(config)
        self._seen_alert_keys: set[str] = set()
        self._seen_reauth_keys: set[str] = set()

    # ── Key helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _alert_key(alert: dict) -> str:
        return f"{alert.get('ts', '')}:{alert.get('title', '')}"

    @staticmethod
    def _reauth_key(event: dict) -> str:
        return f"{event.get('ts', '')}:{event.get('source', '')}:{event.get('outcome', '')}"

    # ── Source detection ──────────────────────────────────────────────────────

    def _extract_source(self, text: str) -> str | None:
        """Scan text for a known source name. Return first match or None."""
        lowered = text.lower()
        for src in _KNOWN_SOURCES:
            if src in lowered:
                return src
        return None

    # ── Core polling logic ────────────────────────────────────────────────────

    async def _check_and_react(self) -> list[dict]:
        """
        Load agent_status.json, find new events, diagnose/fix each one.
        Returns a list of action dicts describing what was done.
        """
        status = _load_status()
        actions: list[dict] = []

        # ── Error alerts ──────────────────────────────────────────────────────
        alerts: list[dict] = status.get("alerts", [])
        for alert in alerts:
            if alert.get("level") != "error":
                continue

            key = self._alert_key(alert)
            if key in self._seen_alert_keys:
                continue
            self._seen_alert_keys.add(key)

            title = alert.get("title", "")
            detail = alert.get("detail", "")
            combined = f"{title} {detail}"
            source = self._extract_source(combined)

            log.info(
                "watcher.alert_detected source=%s title=%r ts=%s",
                source,
                title,
                alert.get("ts"),
            )

            if source is None:
                actions.append(
                    {
                        "type": "alert",
                        "source": None,
                        "action": "logged_only",
                        "result": "no_source_identified",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
                continue

            # Diagnose the source
            try:
                diagnosis = self._commander.diagnose(source)
            except Exception as exc:
                log.warning("diagnose(%s) raised: %s", source, exc)
                actions.append(
                    {
                        "type": "alert",
                        "source": source,
                        "action": "diagnose_failed",
                        "result": str(exc),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
                continue

            source_info: dict = (diagnosis.get("sources") or {}).get(source, {})
            fixable: bool = bool(source_info.get("fixable", False))

            if fixable and self.auto_fix:
                log.info("watcher.fix_start source=%s", source)
                try:
                    fix_result = await self._commander.attempt_fix(source)
                    success = fix_result.get("success", False)
                    log.info(
                        "watcher.fix_result source=%s success=%s strategy=%s",
                        source, success, fix_result.get("strategy", "unknown"),
                    )
                    actions.append(
                        {
                            "type": "alert",
                            "source": source,
                            "action": "auto_fix_attempted",
                            "result": fix_result,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                except Exception as exc:
                    log.warning("watcher.fix_error source=%s error=%s", source, exc)
                    actions.append(
                        {
                            "type": "alert",
                            "source": source,
                            "action": "auto_fix_failed",
                            "result": str(exc),
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    )
            else:
                reason = "not_fixable" if not fixable else "auto_fix_disabled"
                log.info("watcher.fix_skipped source=%s reason=%s", source, reason)
                actions.append(
                    {
                        "type": "alert",
                        "source": source,
                        "action": "detected_no_fix",
                        "result": reason,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )

        # ── Reauth events ─────────────────────────────────────────────────────
        reauth_events: list[dict] = status.get("reauth_events", [])
        for event in reauth_events:
            outcome = event.get("outcome", "")
            if outcome not in ("failed", "timeout"):
                continue

            key = self._reauth_key(event)
            if key in self._seen_reauth_keys:
                continue
            self._seen_reauth_keys.add(key)

            log.warning(
                "watcher.reauth_event source=%s mode=%s outcome=%s detail=%r ts=%s",
                event.get("source"),
                event.get("mode"),
                outcome,
                event.get("detail", ""),
                event.get("ts"),
            )
            actions.append(
                {
                    "type": "reauth",
                    "source": event.get("source"),
                    "action": "logged",
                    "result": outcome,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

        return actions

    # ── Public interface ──────────────────────────────────────────────────────

    async def watch(self) -> None:
        """Poll indefinitely. Sleeps poll_interval seconds between checks."""
        log.info("StatusWatcher started — polling every %ds", self.poll_interval)
        while True:
            try:
                await self._check_and_react()
            except Exception as exc:
                log.warning("StatusWatcher loop error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def watch_once(self) -> list[dict]:
        """Single poll iteration — returns list of actions taken. Used in tests."""
        return await self._check_and_react()

    def get_seen_counts(self) -> dict:
        """Return alert/reauth seen counts for diagnostics."""
        return {
            "alerts_seen": len(self._seen_alert_keys),
            "reauth_events_seen": len(self._seen_reauth_keys),
        }
