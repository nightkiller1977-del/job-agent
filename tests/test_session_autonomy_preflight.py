from unittest.mock import AsyncMock, patch

import pytest

import src.reauth as reauth_mod
import src.session_watchdog as sw


def _health(source: str, status: str) -> sw.SessionHealth:
    return sw.SessionHealth(
        source=source,
        status=status,
        age_hours=1.0,
        session_path=sw.SESSIONS_DIR / f"{source}_chromium.json",
        detail=status,
    )


@pytest.mark.asyncio
async def test_attempt_automated_never_uses_human_fallback():
    mgr = reauth_mod.ReauthManager({})
    with patch.object(
        mgr,
        "_reauth_automated",
        new_callable=AsyncMock,
        return_value=False,
    ) as automated, patch.object(
        mgr,
        "_reauth_human",
        new_callable=AsyncMock,
    ) as human:
        result = await mgr.attempt_automated("usajobs")

    assert result is False
    automated.assert_awaited_once_with("usajobs", escalate=False)
    human.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_automated_unknown_source_returns_false():
    assert await reauth_mod.ReauthManager({}).attempt_automated("unknown") is False


@pytest.mark.asyncio
async def test_successful_reauth_reports_refreshed_without_human_notification(monkeypatch):
    health_calls = iter([
        [_health("linkedin", "expired")],
        [_health("linkedin", "healthy")],
    ])
    monkeypatch.setattr(sw, "check_session_health", lambda sources: next(health_calls))
    attempt = AsyncMock(return_value=True)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    notifications = []
    monkeypatch.setattr(
        sw,
        "_send_deep_link_notification",
        lambda source, message: notifications.append(source),
    )

    result = await sw.preflight_session_check_with_reauth(["linkedin"], {})

    assert result.refreshed_sources == frozenset({"linkedin"})
    assert result.notified_sources == frozenset()
    assert notifications == []
    attempt.assert_awaited_once_with("linkedin")


@pytest.mark.asyncio
async def test_failed_forced_reauth_notifies_exactly_once(monkeypatch):
    monkeypatch.setattr(
        sw,
        "check_session_health",
        lambda sources: [_health("linkedin", "healthy")],
    )
    attempt = AsyncMock(return_value=False)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    notifications = []
    monkeypatch.setattr(
        sw,
        "_send_deep_link_notification",
        lambda source, message: notifications.append(source),
    )

    result = await sw.preflight_session_check_with_reauth(
        ["linkedin"],
        {},
        force_reauth={"linkedin"},
    )

    assert result.refreshed_sources == frozenset()
    assert result.notified_sources == frozenset({"linkedin"})
    assert notifications == ["linkedin"]
    attempt.assert_awaited_once_with("linkedin")


@pytest.mark.asyncio
async def test_source_attempted_once_when_unhealthy_forced_and_duplicated(monkeypatch):
    monkeypatch.setattr(
        sw,
        "check_session_health",
        lambda sources: [_health("linkedin", "expired")],
    )
    attempt = AsyncMock(return_value=False)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    monkeypatch.setattr(sw, "_send_deep_link_notification", lambda source, message: None)

    await sw.preflight_session_check_with_reauth(
        ["linkedin", "linkedin"],
        {},
        force_reauth={"linkedin"},
    )

    attempt.assert_awaited_once_with("linkedin")
