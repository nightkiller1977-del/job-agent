from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# 1/2: session health truth + verified/owned reauth result.
path = Path("src/session_watchdog.py")
text = path.read_text()
text = replace_once(
    text,
    '''@dataclass(frozen=True)\nclass ReauthPreflightResult:\n    health: dict[str, SessionHealth]\n    refreshed_sources: frozenset[str]\n    notified_sources: frozenset[str]\n''',
    '''@dataclass(frozen=True)\nclass ReauthPreflightResult:\n    health: dict[str, SessionHealth]\n    refreshed_sources: frozenset[str]\n    notified_sources: frozenset[str]\n    attempted_sources: frozenset[str] = frozenset()\n''',
    "preflight result dataclass",
)
text = replace_once(
    text,
    '''        is_expired = (age_hours >= _EXPIRED_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 0)\n        is_stale = (age_hours >= _STALE_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 4)\n''',
    '''        if src == "linkedin":\n            # LinkedIn login validity is determined by auth-bearing cookies, not\n            # by the export file's mtime. File age still makes the session stale\n            # so heartbeat can refresh it proactively.\n            is_expired = cookie_expiry_hours is not None and cookie_expiry_hours <= 0\n        else:\n            is_expired = age_hours >= _EXPIRED_HOURS\n        is_stale = (age_hours >= _STALE_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 4)\n''',
    "LinkedIn expiry rule",
)
text = replace_once(
    text,
    '''    manager = ReauthManager(config or {})\n    refreshed: set[str] = set()\n    failed_forced: set[str] = set()\n\n    for source in ordered_sources:\n        if source not in candidates:\n            continue\n        success = False\n''',
    '''    manager = ReauthManager(config or {})\n    attempted: set[str] = set()\n    refreshed: set[str] = set()\n    failed_forced: set[str] = set()\n\n    for source in ordered_sources:\n        if source not in candidates:\n            continue\n        attempted.add(source)\n        success = False\n''',
    "attempt ownership",
)
text = replace_once(
    text,
    '''    if candidates:\n        health = {item.source: item for item in check_session_health(ordered_sources)}\n\n    notified: set[str] = set()\n''',
    '''    if candidates:\n        health = {item.source: item for item in check_session_health(ordered_sources)}\n        # An automated login is not a verified refresh until its durable session\n        # state survives the post-attempt health check. Export failures are\n        # intentionally non-fatal in BaseScraper, so the boolean alone is not\n        # sufficient evidence. A stale session is still usable; missing/expired is not.\n        refreshed = {\n            source\n            for source in refreshed\n            if (item := health.get(source)) is not None\n            and item.status not in {"expired", "missing"}\n        }\n\n    notified: set[str] = set()\n''',
    "verified refresh filter",
)
text = replace_once(
    text,
    '''    return ReauthPreflightResult(\n        health=health,\n        refreshed_sources=frozenset(refreshed),\n        notified_sources=frozenset(notified),\n    )\n''',
    '''    return ReauthPreflightResult(\n        health=health,\n        refreshed_sources=frozenset(refreshed),\n        notified_sources=frozenset(notified),\n        attempted_sources=frozenset(attempted),\n    )\n''',
    "preflight result return",
)
path.write_text(text)


# 3: one durable warning-delivery gate for Telegram and desktop.
path = Path("src/notifier.py")
text = path.read_text()
text = replace_once(
    text,
    '''    key = f"warn:{dedupe_key}" if dedupe_key else f"warn:{title}:{detail}"\n    if not notification_dedupe_active(key, dedupe_seconds):\n        _send_telegram(f"⚠️ [Job Agent WARNING] {title}\\nDetail: {detail}")\n        record_notification_dedupe(key)\n\n    if desktop:\n        _desktop_notify(f"🟡 {title}", detail or title, subtitle="Job Agent WARNING")\n''',
    '''    key = f"warn:{dedupe_key}" if dedupe_key else f"warn:{title}:{detail}"\n    if not notification_dedupe_active(key, dedupe_seconds):\n        _send_telegram(f"⚠️ [Job Agent WARNING] {title}\\nDetail: {detail}")\n        if desktop:\n            _desktop_notify(f"🟡 {title}", detail or title, subtitle="Job Agent WARNING")\n        record_notification_dedupe(key)\n''',
    "warning durable delivery gate",
)
path.write_text(text)


# 4/5: carry preflight ownership into apply and reload all same-run rows.
path = Path("src/orchestrator.py")
text = path.read_text()
text = replace_once(
    text,
    '''        if blocked:\n            console.print("\\n[yellow]Session-blocked (skipping in this run):[/yellow]")\n''',
    '''        preflight_attempted_sources: set[str] = set()\n\n        if blocked:\n            console.print("\\n[yellow]Session-blocked (skipping in this run):[/yellow]")\n''',
    "preflight ownership accumulator",
)
text = replace_once(
    text,
    '''                    preflight_result = await preflight_session_check_with_reauth(\n                        list(blocked_sources),\n                        self.config,\n                        force_reauth=force_reauth_sources,\n                    )\n                    for refreshed_source in preflight_result.refreshed_sources:\n                        self._unblock_session_jobs_after_reauth(refreshed_source)\n''',
    '''                    preflight_result = await preflight_session_check_with_reauth(\n                        list(blocked_sources),\n                        self.config,\n                        force_reauth=force_reauth_sources,\n                    )\n                    preflight_attempted_sources.update(preflight_result.attempted_sources)\n                    for refreshed_source in preflight_result.refreshed_sources:\n                        self._unblock_session_jobs_after_reauth(refreshed_source)\n                    if preflight_result.refreshed_sources:\n                        # Source unblocking mutates durable rows for both the blocked\n                        # set and jobs that were already classified ready. Reload the\n                        # existing ready set so the one-shot session_prepared marker\n                        # is visible to the later circuit/preflight guards.\n                        ready = [\n                            self.state.get_job(job["job_id"]) or job\n                            for job in ready\n                        ]\n''',
    "preflight ownership propagation and ready reload",
)
text = replace_once(
    text,
    '''        reauthed_this_run: set[str] = set()  # P3: reauth each source at most once per run\n''',
    '''        reauthed_this_run: set[str] = set(preflight_attempted_sources)  # includes unattended preflight ownership\n''',
    "apply reauth guard seed",
)
text = replace_once(
    text,
    '''            notify_warning(\n                "Apply run: nothing submitted",\n                f"0 submitted, {skipped_count} blocked, {len(blocked)} need session prep. "\n                f"Run: python src/main.py prepare-sessions",\n            )\n''',
    '''            notify_warning(\n                "Apply run: nothing submitted",\n                f"0 submitted, {skipped_count} blocked, {len(blocked)} need session prep. "\n                f"Run: python src/main.py prepare-sessions",\n                dedupe_key="apply_nothing_submitted",\n                dedupe_seconds=21600,\n            )\n''',
    "stable nothing-submitted warning",
)
path.write_text(text)
