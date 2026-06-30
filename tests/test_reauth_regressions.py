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

@pytest.mark.asyncio
async def test_regression_jobright_20260626_230722():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 23:07:22 UTC"""
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
async def test_regression_linkedin_20260626_230723():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 23:07:23 UTC"""
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
async def test_regression_usajobs_20260626_230724():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 23:07:24 UTC"""
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
async def test_regression_jobright_20260626_230946():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 23:09:46 UTC"""
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
async def test_regression_linkedin_20260626_230947():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 23:09:47 UTC"""
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
async def test_regression_usajobs_20260626_230947():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 23:09:47 UTC"""
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
async def test_regression_jobright_20260626_231031():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 23:10:31 UTC"""
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
async def test_regression_linkedin_20260626_231032():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 23:10:32 UTC"""
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
async def test_regression_usajobs_20260626_231032():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 23:10:32 UTC"""
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
async def test_regression_jobright_20260626_231327():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-26 23:13:27 UTC"""
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
async def test_regression_linkedin_20260626_231328():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-26 23:13:28 UTC"""
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
async def test_regression_usajobs_20260626_231329():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-26 23:13:29 UTC"""
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
async def test_regression_jobright_20260627_001155():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 00:11:55 UTC"""
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
async def test_regression_linkedin_20260627_001156():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 00:11:56 UTC"""
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
async def test_regression_usajobs_20260627_001156():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 00:11:56 UTC"""
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
async def test_regression_jobright_20260627_001348():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 00:13:48 UTC"""
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
async def test_regression_linkedin_20260627_001348():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 00:13:48 UTC"""
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
async def test_regression_usajobs_20260627_001349():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 00:13:49 UTC"""
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
async def test_regression_jobright_20260627_002135():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 00:21:35 UTC"""
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
async def test_regression_linkedin_20260627_002136():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 00:21:36 UTC"""
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
async def test_regression_usajobs_20260627_002137():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 00:21:37 UTC"""
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
async def test_regression_jobright_20260627_010350():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 01:03:50 UTC"""
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
async def test_regression_linkedin_20260627_010351():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 01:03:51 UTC"""
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
async def test_regression_usajobs_20260627_010351():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 01:03:51 UTC"""
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
async def test_regression_jobright_20260627_010531():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 01:05:31 UTC"""
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
async def test_regression_linkedin_20260627_010532():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 01:05:32 UTC"""
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
async def test_regression_usajobs_20260627_010532():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 01:05:32 UTC"""
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
async def test_regression_jobright_20260627_061154():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 06:11:54 UTC"""
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
async def test_regression_linkedin_20260627_061156():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 06:11:56 UTC"""
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
async def test_regression_usajobs_20260627_061156():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 06:11:56 UTC"""
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
async def test_regression_jobright_20260627_195243():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 19:52:43 UTC"""
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
async def test_regression_linkedin_20260627_195244():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 19:52:44 UTC"""
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
async def test_regression_usajobs_20260627_195245():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 19:52:45 UTC"""
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
async def test_regression_jobright_20260627_195450():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 19:54:50 UTC"""
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
async def test_regression_linkedin_20260627_195451():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 19:54:51 UTC"""
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
async def test_regression_usajobs_20260627_195452():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 19:54:52 UTC"""
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
async def test_regression_jobright_20260627_195539():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 19:55:39 UTC"""
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
async def test_regression_linkedin_20260627_195540():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 19:55:40 UTC"""
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
async def test_regression_usajobs_20260627_195541():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 19:55:41 UTC"""
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
async def test_regression_jobright_20260627_203923():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 20:39:23 UTC"""
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
async def test_regression_linkedin_20260627_203924():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 20:39:24 UTC"""
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
async def test_regression_usajobs_20260627_203925():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 20:39:25 UTC"""
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
async def test_regression_jobright_20260627_204509():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 20:45:09 UTC"""
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
async def test_regression_linkedin_20260627_204510():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 20:45:10 UTC"""
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
async def test_regression_usajobs_20260627_204511():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 20:45:11 UTC"""
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
async def test_regression_jobright_20260627_213538():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 21:35:38 UTC"""
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
async def test_regression_linkedin_20260627_213539():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 21:35:39 UTC"""
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
async def test_regression_usajobs_20260627_213540():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 21:35:40 UTC"""
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
async def test_regression_jobright_20260627_220405():
    """Auto-generated regression: jobright — _auto_login returned True after session expiry — corrected 2026-06-27 22:04:05 UTC"""
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
async def test_regression_linkedin_20260627_220406():
    """Auto-generated regression: linkedin — _auto_login returned True after session expiry — corrected 2026-06-27 22:04:06 UTC"""
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
async def test_regression_usajobs_20260627_220406():
    """Auto-generated regression: usajobs — 2FA required — corrected 2026-06-27 22:04:06 UTC"""
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
