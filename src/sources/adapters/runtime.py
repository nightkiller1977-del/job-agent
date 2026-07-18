"""Process-scoped shared runtime for the adapter apply path (production wiring).

One `main.py apply` invocation is one process, so process-level singletons give
every `ExternalApplySession` constructed during the batch the SAME RunLog (one
run_id for the whole batch — attempts aggregate as one run), the SAME Dispatcher
(dedup state survives across jobs/retries), and the SAME reauth router.

Kept lazy so importing this module never drags in notifier/reauth dependencies;
everything degrades to None (feature-off) rather than raising.
"""
from __future__ import annotations

_run_log = None
_dispatcher = None
_router = None


def get_run_log():
    """Shared per-invocation RunLog (one run_id per batch)."""
    global _run_log
    if _run_log is None:
        try:
            from ...events import RunLog
            _run_log = RunLog(agent="external_apply")
        except Exception:
            return None
    return _run_log


def get_dispatcher():
    """Shared fail-open notification dispatcher (dedup state spans the batch)."""
    global _dispatcher
    if _dispatcher is None:
        try:
            from ...notifications import Dispatcher
            _dispatcher = Dispatcher()
        except Exception:
            return None
    return _dispatcher


def get_reauth_router(config):
    """Shared re-auth router wired to the real ReauthManager."""
    global _router
    if _router is None:
        try:
            from .auth_routing import ManagerReauthRouter
            _router = ManagerReauthRouter(config)
        except Exception:
            return None
    return _router


def reset() -> None:
    """Test hook: drop the singletons."""
    global _run_log, _dispatcher, _router
    _run_log = _dispatcher = _router = None
