"""Auto-generated regression tests — written by ReauthManager on each successful self-heal."""
import pytest

@pytest.mark.asyncio
async def test_regression_jobright_20260626_214734():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 21:47:34 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "jobright" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("jobright", "_auto_login returned True after session expiry")
    assert exc.source == "jobright"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("jobright", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_linkedin_20260626_214735():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 21:47:35 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "linkedin" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("linkedin", "_auto_login returned True after session expiry")
    assert exc.source == "linkedin"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("linkedin", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_usajobs_20260626_214736():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 21:47:36 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "usajobs" in HUMAN_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("usajobs", "2FA required")
    assert exc.source == "usajobs"
    assert "2FA required" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_human", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("usajobs", "2FA required")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_jobright_20260626_214831():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 21:48:31 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "jobright" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("jobright", "_auto_login returned True after session expiry")
    assert exc.source == "jobright"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("jobright", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_linkedin_20260626_214832():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 21:48:32 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "linkedin" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("linkedin", "_auto_login returned True after session expiry")
    assert exc.source == "linkedin"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("linkedin", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_usajobs_20260626_214832():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 21:48:32 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "usajobs" in HUMAN_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("usajobs", "2FA required")
    assert exc.source == "usajobs"
    assert "2FA required" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_human", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("usajobs", "2FA required")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_jobright_20260626_214907():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 21:49:07 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "jobright" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("jobright", "_auto_login returned True after session expiry")
    assert exc.source == "jobright"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("jobright", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_linkedin_20260626_214908():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 21:49:08 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "linkedin" in AUTOMATED_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("linkedin", "_auto_login returned True after session expiry")
    assert exc.source == "linkedin"
    assert "_auto_login returned True after session expiry" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("linkedin", "_auto_login returned True after session expiry")
        mock.assert_called_once()
    assert result is True

@pytest.mark.asyncio
async def test_regression_usajobs_20260626_214908():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 21:49:08 UTC"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "usajobs" in HUMAN_SOURCES

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("usajobs", "2FA required")
    assert exc.source == "usajobs"
    assert "2FA required" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={})
    with patch.object(mgr, "_reauth_human", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("usajobs", "2FA required")
        mock.assert_called_once()
    assert result is True
