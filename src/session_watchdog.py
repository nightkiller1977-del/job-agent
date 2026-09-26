"""
Session Watchdog — proactive session health monitoring and automated recovery.

Three layers of protection:
  1. Health check: inspect cookie age before each apply run and classify each
     source as healthy / stale / expired / missing.
  2. Heartbeat: silently visit each source in the background to extend cookie
     lifetime without a full login — run nightly via scheduler.
  3. Deep-link notification: delegates to notifier.py which reads Telegram
     credentials from AI Commander's settings-v3.json automatically.
     (platform-specific: ~/Library/Application Support/ai-command-center on macOS,
      ~/.config/ai-command-center on Linux — resolved via secret_store._commander_dir())
     No duplicate credential configuration needed — configure Telegram once
     in AI Commander and job-agent picks it up automatically.

Reauth staging is resolved per host, never hard-coded to one OS: Terminal.app via
osascript on macOS, a detected terminal emulator on a POSIX desktop, `cmd.exe` on
Windows. A host with no terminal — the headless scheduler run this agent normally
executes as — still gets the escalation over Telegram/desktop with the manual
`prepare-sessions` command, because a missing terminal must never swallow the
session failure it was supposed to report.

macOS URL handler (register once):
  bash scripts/install-jobagent-url-handler.sh
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

_log = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSIONS_DIR = Path(__file__).parent.parent / "state" / "sessions"

# How old a session export JSON can be before it's considered stale (warn)
_STALE_HOURS = 20

# How old before it's treated as expired (block)
_EXPIRED_HOURS = 48

# Only auth-bearing LinkedIn cookies determine whether the login session is
# usable. Tracking cookies such as lidc/UserMatchHistory expire much sooner and
# must never flip an otherwise-valid li_at session to expired.
LINKEDIN_AUTH_COOKIE_NAMES = frozenset({"li_at", "liap", "li_rm"})

# Sources that support background heartbeat visits
_HEARTBEAT_SOURCES = {"linkedin", "indeed", "jobright"}

# Sources that need a visible browser for the user to complete login
_HUMAN_SOURCES = {"linkedin", "usajobs"}

# CLI-supported prepare-sessions source choices. Queue/source labels from
# JobSpy-backed providers such as glassdoor, google, and ziprecruiter must not
# be rewritten to jobright because prepare_sessions filters stored jobs by exact
# source. Unsupported labels intentionally omit the source filter.
_PREPARE_SESSION_SOURCES = {"linkedin", "usajobs", "jobright", "indeed"}
_PREPARE_SESSION_SOURCE_ALIASES = {
    "linkedin-saved": "linkedin",
}

# POSIX-desktop terminal emulators tried in order, as (executable, flag that
# introduces the command argv). Presence is probed with shutil.which() rather
# than assumed. Debian's `x-terminal-emulator` alternative comes first so the
# user's own configured default wins before we start guessing at specific
# emulators. The flags differ per emulator and are not interchangeable:
# gnome-terminal/kitty use `--`, xfce4-terminal needs `-x` to consume the rest
# of argv, and the rest follow xterm's `-e`.
_TERMINAL_EMULATORS: tuple[tuple[str, str], ...] = (
    ("x-terminal-emulator", "-e"),
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("kitty", "--"),
    ("alacritty", "-e"),
    ("xterm", "-e"),
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SessionHealth:
    source: str
    status: str          # healthy | stale | expired | missing
    age_hours: float
    session_path: Path
    detail: str = ""


@dataclass(frozen=True)
class StagingResult:
    """Outcome of trying to open a terminal running `prepare-sessions`.

    `supported` False means this host/session can never open a terminal window
    (headless scheduler run, or no terminal emulator installed). That is a
    capability gap, not a transient error: callers must escalate over a
    messaging channel instead of retrying a launch that cannot succeed.
    """
    staged: bool
    supported: bool
    detail: str = ""


@dataclass(frozen=True)
class ReauthPreflightResult:
    health: dict[str, SessionHealth]
    refreshed_sources: frozenset[str]
    notified_sources: frozenset[str]
    attempted_sources: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_session_health(sources: list[str] | None = None) -> list[SessionHealth]:
    """Inspect cookie file ages and return health status for each source."""
    all_sources = sources or ["linkedin", "indeed", "jobright", "usajobs"]
    results: list[SessionHealth] = []

    for src in all_sources:
        paths = [
            SESSIONS_DIR / f"{src}_chromium.json",
            SESSIONS_DIR / f"{src}.json",
        ]
        found: Optional[Path] = None
        for p in paths:
            if p.exists():
                found = p
                break

        if found is None:
            results.append(SessionHealth(
                source=src, status="missing",
                age_hours=float("inf"),
                session_path=SESSIONS_DIR / f"{src}_chromium.json",
                detail="No session file — run prepare-sessions once to seed cookies.",
            ))
            continue

        age_sec = time.time() - found.stat().st_mtime
        age_hours = age_sec / 3600

        if src == "linkedin":
            has_auth_cookie, cookie_expiry_hours = _linkedin_auth_cookie_state(found)
            if not has_auth_cookie:
                results.append(SessionHealth(
                    source=src,
                    status="expired",
                    age_hours=age_hours,
                    session_path=found,
                    detail="LinkedIn session has no recognized authentication cookie.",
                ))
                continue
        else:
            cookie_expiry_hours = None

        if src == "linkedin":
            # LinkedIn login validity is determined by auth-bearing cookies, not
            # by the export file's mtime. File age still makes the session stale
            # so heartbeat can refresh it proactively.
            is_expired = cookie_expiry_hours is not None and cookie_expiry_hours <= 0
        else:
            is_expired = age_hours >= _EXPIRED_HOURS
        is_stale = (age_hours >= _STALE_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 4)

        if is_expired:
            status = "expired"
            if cookie_expiry_hours is not None and cookie_expiry_hours <= 0:
                detail = f"LinkedIn cookies expired {abs(cookie_expiry_hours):.1f}h ago."
            else:
                detail = f"Session is {age_hours:.0f}h old — cookies very likely invalid."
        elif is_stale:
            status = "stale"
            if cookie_expiry_hours is not None and cookie_expiry_hours <= 4:
                detail = f"LinkedIn cookies expire in {cookie_expiry_hours:.1f}h — heartbeat recommended."
            else:
                detail = f"Session is {age_hours:.0f}h old — heartbeat recommended."
        else:
            status = "healthy"
            detail = f"Session is {age_hours:.1f}h old — looks good."

        results.append(SessionHealth(
            source=src, status=status,
            age_hours=age_hours,
            session_path=found,
            detail=detail,
        ))

    return results


def _linkedin_auth_cookie_state(session_path: Path) -> tuple[bool, Optional[float]]:
    """Return whether LinkedIn auth cookies exist and their earliest expiry."""
    try:
        data = json.loads(session_path.read_text())
        auth_cookies = [
            cookie
            for cookie in data.get("cookies", [])
            if "linkedin" in str(cookie.get("domain", "")).lower()
            and str(cookie.get("name", "")) in LINKEDIN_AUTH_COOKIE_NAMES
        ]
        if not auth_cookies:
            return False, None
        expiries = [
            float(cookie["expires"])
            for cookie in auth_cookies
            if float(cookie.get("expires", -1) or -1) > 0
        ]
        if not expiries:
            return True, None
        return True, (min(expiries) - time.time()) / 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, None


def _parse_linkedin_expiry(session_path: Path) -> Optional[float]:
    """Extract the earliest LinkedIn authentication-cookie expiry."""
    _has_auth_cookie, expiry_hours = _linkedin_auth_cookie_state(session_path)
    return expiry_hours


def print_health_table(results: list[SessionHealth]) -> None:
    table = Table(title="Session Health", show_lines=True)
    table.add_column("Source", style="bold")
    table.add_column("Status")
    table.add_column("Age")
    table.add_column("Detail")

    STATUS_COLOR = {
        "healthy": "green",
        "stale": "yellow",
        "expired": "red",
        "missing": "red",
    }
    for r in results:
        color = STATUS_COLOR.get(r.status, "white")
        age_str = f"{r.age_hours:.1f}h" if r.age_hours < float("inf") else "—"
        table.add_row(r.source, f"[{color}]{r.status}[/{color}]", age_str, r.detail)

    console.print(table)


# ---------------------------------------------------------------------------
# Heartbeat — silently extend session lifetime
# ---------------------------------------------------------------------------

async def run_heartbeat(sources: list[str] | None = None, config: dict | None = None) -> dict[str, bool]:
    """Visit each source in the background to keep cookies alive.

    Only runs for sources whose session is healthy or stale (not missing/expired).
    Returns {source: refreshed_ok}.
    """
    cfg = config or {}
    targets = sources or list(_HEARTBEAT_SOURCES)
    results: dict[str, bool] = {}

    health = {h.source: h for h in check_session_health(targets)}

    for src in targets:
        h = health.get(src)
        if h and h.status == "missing":
            _log.info("heartbeat.skip source=%s reason=missing", src)
            results[src] = False
            continue
        if h and h.status == "expired":
            _log.info("heartbeat.skip source=%s reason=expired", src)
            results[src] = False
            _send_deep_link_notification(
                src,
                f"{src.capitalize()} session expired ({h.age_hours:.0f}h old). Tap to refresh:",
            )
            continue

        try:
            ok = await _heartbeat_source(src, cfg)
            results[src] = ok
            if ok:
                _log.info("heartbeat.success source=%s", src)
            else:
                _log.warning("heartbeat.failed source=%s", src)
                _send_deep_link_notification(
                    src,
                    f"{src.capitalize()} heartbeat failed. Session may need manual refresh:",
                )
        except Exception as exc:
            _log.error("heartbeat.error source=%s error=%s", src, exc)
            results[src] = False

    return results


async def _heartbeat_source(source: str, config: dict) -> bool:
    """Silently load the source's homepage to refresh cookies."""
    HEARTBEAT_URLS = {
        "linkedin": "https://www.linkedin.com/feed/",
        "indeed":   "https://www.indeed.com/",
        "jobright": "https://jobright.ai/",
    }
    url = HEARTBEAT_URLS.get(source)
    if not url:
        return False

    try:
        from .sources.base import SESSIONS_DIR
        from playwright.async_api import async_playwright

        session_file = SESSIONS_DIR / f"{source}_chromium.json"
        if not session_file.exists():
            return False

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                storage_state=str(session_file),
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            state = await ctx.storage_state()
            tmp = session_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(session_file)

            await browser.close()
        return True
    except Exception as exc:
        _log.warning("heartbeat._heartbeat_source source=%s error=%s", source, exc)
        return False


# ---------------------------------------------------------------------------
# Deep-link notification
# ---------------------------------------------------------------------------

def _resolve_tailscale_ip() -> Optional[str]:
    """Resolve this Mac's personal-tailnet IPv4 address for the noVNC bridge link.

    Tries TAILSCALE_BIN (override), then the standard Tailscale.app CLI
    location, then common Homebrew paths, then a bare `tailscale` on PATH.
    Returns None — never a guessed/fallback address — if none resolve, so
    callers skip the noVNC link rather than send a dead one.
    """
    candidates = [
        os.environ.get("TAILSCALE_BIN", ""),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
        "tailscale",
    ]
    for binary in candidates:
        if not binary:
            continue
        try:
            result = subprocess.run(
                [binary, "ip", "-4"], capture_output=True, timeout=5, text=True,
            )
            ip = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            if result.returncode == 0 and ip:
                return ip
        except Exception:
            continue
    return None


def _novnc_link() -> Optional[str]:
    """Build the tappable noVNC URL for remote reauth over the personal tailnet.

    Returns None when Tailscale isn't resolvable so the caller can omit the
    link entirely instead of sending one that won't connect. Port is
    NOVNC_PORT (default 6080) — the websockify bridge set up alongside this,
    bound explicitly to the Tailscale interface, never 0.0.0.0.
    """
    ip = _resolve_tailscale_ip()
    if not ip:
        return None
    port = os.environ.get("NOVNC_PORT", "6080")
    return f"http://{ip}:{port}/vnc.html?autoconnect=true&resize=scale"


def _prepare_sessions_source(source: str) -> Optional[str]:
    """Return a CLI-supported prepare-sessions source for a queue source label."""
    normalized = (source or "").strip().lower()
    if not normalized:
        return None
    if normalized in _PREPARE_SESSION_SOURCES:
        return normalized
    return _PREPARE_SESSION_SOURCE_ALIASES.get(normalized)


def _venv_activate_parts() -> list[str]:
    """Shell tokens that activate the project venv for the current platform.

    POSIX shells source `.venv/bin/activate`; `cmd.exe` uses `call` so control
    returns to the chained command instead of stopping at the batch file.
    """
    if sys.platform == "win32":
        return ["call", r".venv\Scripts\activate.bat"]
    return ["source", ".venv/bin/activate"]


def _shell_quote(value: str) -> str:
    """Quote one argument for the shell that will actually run the command.

    `shlex.quote` is POSIX-only: it wraps anything containing a backslash in
    single quotes, and `cmd.exe` does not treat single quotes as quoting. Since
    every Windows path contains backslashes, using it there produced a command
    that always failed — `cd 'C:\\Users\\me\\job-agent'` — not just one that
    broke on spaces. cmd quotes with double quotes, inside which `&`, `|` and
    `^` are literal.
    """
    if sys.platform == "win32":
        return '"' + str(value).replace('"', '""') + '"'
    return shlex.quote(str(value))


def _prepare_sessions_command(source: str) -> tuple[str, Optional[str]]:
    """Build a safe shell command for terminal staging.

    The second tuple item is the normalized prepare-sessions source, or None
    when no safe source filter should be sent.
    """
    project_dir = Path(__file__).parent.parent
    prepare_source = _prepare_sessions_source(source)
    # `cd` alone does not switch drive on Windows, so a checkout on D: would
    # silently leave cmd in the C: working directory.
    cd_parts = ["cd", "/d"] if sys.platform == "win32" else ["cd"]
    parts = [
        *cd_parts,
        _shell_quote(str(project_dir)),
        "&&",
        *_venv_activate_parts(),
        "&&",
        "python",
        "src/main.py",
        "prepare-sessions",
    ]
    if prepare_source:
        parts.extend(["--source", _shell_quote(prepare_source)])
    return " ".join(parts), prepare_source


def _terminal_launch_argv(cmd: str) -> Optional[list[str]]:
    """Full argv that opens a terminal window running ``cmd``, or None when this
    platform/session cannot host one.

    Availability is probed with ``shutil.which()`` rather than assumed, so a host
    with no terminal emulator installed reports None up front instead of failing
    at exec time with ``[Errno 2] No such file or directory``.

    A POSIX run with neither DISPLAY nor WAYLAND_DISPLAY is a headless
    scheduler run: there is no desktop to draw a window on, so staging is
    reported unsupported and the caller escalates over a messaging channel.
    """
    if sys.platform == "darwin":
        osascript = shutil.which("osascript")
        if not osascript:
            return None
        return [osascript, "-e", f'tell application "Terminal" to do script {json.dumps(cmd)}']

    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not comspec:
            return None
        # `start "" cmd /k` opens a new console that stays up once the command
        # finishes, so the human can read the result the way Terminal.app behaves.
        return [comspec, "/c", "start", "", "cmd", "/k", cmd]

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    # `source` is a bash/zsh builtin, so the staged command needs bash — without
    # it the window would open only to fail on venv activation.
    shell = shutil.which("bash")
    if not shell:
        return None
    for exe, command_flag in _TERMINAL_EMULATORS:
        resolved = shutil.which(exe)
        if resolved:
            return [resolved, command_flag, shell, "-lc", cmd]
    return None


def _stage_prepare_sessions(source: str) -> StagingResult:
    """Open a terminal window on this host and start `prepare-sessions`.

    `staged` is True only when the launcher exited cleanly. A launcher that
    exists but fails is a transient error and intentionally does not count as a
    successful notification cycle, so the next watchdog pass may retry staging
    instead of waiting 12 hours.

    `supported` is False when the host has no terminal to launch at all. That is
    not retryable, so the caller must escalate to a messaging channel rather than
    silently dropping the alert.
    """
    cmd, prepare_source = _prepare_sessions_command(source)
    if prepare_source != (source or "").strip().lower():
        _log.info(
            "session_watchdog.stage_source_mapped source=%s prepare_source=%s",
            source,
            prepare_source or "all",
        )
    argv = _terminal_launch_argv(cmd)
    if argv is None:
        _log.info(
            "session_watchdog.stage_terminal_unsupported source=%s platform=%s",
            source,
            sys.platform,
        )
        return StagingResult(staged=False, supported=False, detail="no_terminal_available")
    try:
        result = subprocess.run(argv, capture_output=True, timeout=10, text=True)
    except Exception as exc:
        _log.warning("session_watchdog.stage_terminal_failed source=%s error=%s", source, exc)
        return StagingResult(staged=False, supported=True, detail=str(exc)[:200])
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        _log.warning(
            "session_watchdog.stage_terminal_failed source=%s returncode=%s error=%s",
            source,
            result.returncode,
            stderr,
        )
        return StagingResult(staged=False, supported=True, detail=stderr[:200])
    return StagingResult(staged=True, supported=True)


def _send_deep_link_notification(source: str, message: str) -> None:
    """Stage reauth where a terminal exists, then send one durable, rate-limited
    human escalation.

    On a host that can open a terminal, staging happens before delivery so a
    failed launch does not send a link to a flow that is not ready. A failed
    stage also does not consume the dedupe window, allowing the next watchdog
    pass to retry.

    On a host that cannot open a terminal at all — the headless scheduler case,
    which is how this agent actually runs — there is nothing to retry. The
    escalation is still delivered over Telegram and the desktop, carrying the
    manual remediation command, and the missing terminal is recorded as a
    secondary condition so a staging gap never masks the primary session
    failure.
    """
    prepare_source = _prepare_sessions_source(source)
    deep_link = "jobagent://prepare-sessions"
    if prepare_source:
        deep_link += f"?source={prepare_source}"
    novnc_link = _novnc_link()
    link_lines = [deep_link]
    if novnc_link:
        link_lines.append(f"From your phone (personal Tailscale): {novnc_link}")
    full_msg = f"{message}\n\n" + "\n".join(link_lines)

    try:
        from .notifier import (
            _desktop_notify,
            _send_telegram,
            notification_dedupe_active,
            record_notification_dedupe,
            record_secondary_condition,
        )

        key = f"deep_link:{source}"
        if notification_dedupe_active(key, 12 * 3600):
            return

        staging = _stage_prepare_sessions(source)
        if staging.supported and not staging.staged:
            return
        if not staging.supported:
            manual_cmd, _ = _prepare_sessions_command(source)
            link_lines.append(f"No terminal on this host — run manually:\n  {manual_cmd}")
            full_msg = f"{message}\n\n" + "\n".join(link_lines)

        # Deliver before any status-file bookkeeping. `_save_status` does not
        # catch write errors, so recording the secondary condition first meant a
        # full or unwritable disk raised into the handler below and dropped the
        # alert entirely — recreating the exact failure this function exists to
        # prevent. The desktop channel gets the full text too, so an operator on
        # a graphical host still sees the manual command when Telegram is
        # unconfigured or unreachable.
        _send_telegram(full_msg)
        _desktop_notify(f"{source} session needs refresh", full_msg)
        record_notification_dedupe(key)
        if not staging.supported:
            try:
                record_secondary_condition(
                    "session_recovery_required",
                    "terminal_staging_unavailable",
                    "prepare_sessions_terminal",
                    dedupe_key=f"session-stage:{source}",
                )
            except Exception as exc:  # noqa: BLE001 — evidence must not undo delivery
                _log.warning(
                    "session_watchdog.secondary_condition_failed source=%s error=%s", source, exc
                )
    except Exception as exc:
        _log.warning("session_watchdog.notify_failed source=%s error=%s", source, exc)
        console.print(f"[yellow]Session alert ({source}):[/yellow] {message}\n{full_msg}")


# ---------------------------------------------------------------------------
# Preflight gate used by orchestrator.apply_approved
# ---------------------------------------------------------------------------

def preflight_session_check(sources: list[str]) -> dict[str, SessionHealth]:
    """Called before apply_approved — returns health map.

    Expired/missing sources emit deep-link notifications immediately so the
    user can act while other jobs are being processed.
    """
    health_map = {h.source: h for h in check_session_health(sources)}
    for src, h in health_map.items():
        if h.status in {"expired", "missing"}:
            _send_deep_link_notification(
                src,
                f"[Job Agent] {src.capitalize()} session {h.status} — apply will skip {src} jobs. Tap to fix:",
            )
    return health_map


async def preflight_session_check_with_reauth(
    sources: list[str],
    config: dict | None = None,
    *,
    force_reauth: set[str] | None = None,
) -> ReauthPreflightResult:
    """Attempt unattended recovery once per source, then own any human escalation."""
    from .reauth import AUTOMATED_SOURCES, ReauthManager

    ordered_sources = list(dict.fromkeys(source for source in sources if source))
    forced = set(force_reauth or ())
    health = {item.source: item for item in check_session_health(ordered_sources)}
    candidates = {
        source
        for source in ordered_sources
        if source in AUTOMATED_SOURCES
        and (
            source in forced
            or source not in health
            or health[source].status in {"expired", "missing"}
        )
    }
    manager = ReauthManager(config or {})
    attempted: set[str] = set()
    refreshed: set[str] = set()
    failed_forced: set[str] = set()

    for source in ordered_sources:
        if source not in candidates:
            continue
        attempted.add(source)
        success = False
        try:
            success = await manager.attempt_automated(source)
        except Exception as exc:
            _log.warning("preflight.reauth.error source=%s error=%s", source, exc)
        if success:
            refreshed.add(source)
        elif source in forced:
            failed_forced.add(source)

    if candidates:
        health = {item.source: item for item in check_session_health(ordered_sources)}
        # An automated login is not a verified refresh until its durable session
        # state survives the post-attempt health check. Export failures are
        # intentionally non-fatal in BaseScraper, so the boolean alone is not
        # sufficient evidence. A stale session is still usable; missing/expired is not.
        refreshed = {
            source
            for source in refreshed
            if (item := health.get(source)) is not None
            and item.status not in {"expired", "missing"}
        }

    notified: set[str] = set()
    for source in ordered_sources:
        item = health.get(source)
        needs_human = (
            source in failed_forced
            or item is None
            or item.status in {"expired", "missing"}
        )
        if not needs_human:
            continue
        _send_deep_link_notification(
            source,
            f"[Job Agent] {source.capitalize()} session unavailable after automated recovery. Tap to fix:",
        )
        notified.add(source)

    return ReauthPreflightResult(
        health=health,
        refreshed_sources=frozenset(refreshed),
        notified_sources=frozenset(notified),
        attempted_sources=frozenset(attempted),
    )
