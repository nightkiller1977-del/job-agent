"""ACES-399 — privacy-safe forensic evidence capture for apply attempts.

Purely observational: this module gathers bounded, allowlisted signals about
*where* an apply attempt got to (auth wall? form ever reached? submit control
present? validation errors? receipt confirmed?) so failures can be classified
from measured evidence instead of guesswork. It must never change what an
attempt does.

Hard rules (ACES-399 handoff):
  - Capture is best-effort: every public entry point here swallows its own
    exceptions and degrades to `capture_incomplete=True` rather than raising
    into (or altering) an apply attempt.
  - No raw HTML, response bodies, form values, credentials, cookies, tokens,
    resume contents, full URLs/query strings, or arbitrary visible DOM text
    ever crosses into a forensic event. `sanitize_forensic_fields` is an
    ALLOWLIST (only known fields, validated per-field) — stricter than
    `events.py`'s existing denylist-based `_sanitize`, as required.
  - Screenshots stay out of scope; nothing here captures or stores images.
  - Writes go through the existing `RunLog.emit()` (src/events.py) — no new
    persistence system.
  - Host/vendor helpers reuse `sources.adapters.generic.detect_vendor` and the
    existing hostname-only extraction (moved here from session.py, not
    reimplemented) rather than adding a new URL parser.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any

from .attempt import AttemptPhase
from ...challenge_detect import HAS_VISIBLE_CHALLENGE_FRAME_JS

# --------------------------------------------------------------------------- #
# URL helpers (reused, not reinvented — see module docstring)
# --------------------------------------------------------------------------- #


def host_of(url: str) -> str:
    """Hostname only — no scheme, path, query, fragment, or credentials.

    Moved here from adapters/session.py's private `_host()` so the same,
    single implementation is shared by the session boundary and the
    generic/vendor_cta rich-evidence call sites (do not write a second one).
    """
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


_LOGIN_PATH_HINTS = ("/login", "/signin", "/sign-in", "/sso", "/authenticate", "/auth")
_APPLY_PATH_HINTS = ("/apply", "/application", "/applications/new")
_CONFIRMATION_PATH_HINTS = (
    "/confirmation", "/thank-you", "/thankyou", "/thanks", "/submitted", "/success",
)
_JOB_PATH_HINTS = ("/job/", "/jobs/", "/career", "/posting", "/req/", "/position", "/opening")


def path_class_of(url: str) -> str:
    """Classify a URL's path into the bounded enum: apply/login/job/confirmation/unknown.

    Path only — the caller never receives the query string or fragment back."""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except Exception:
        return "unknown"
    if any(h in path for h in _LOGIN_PATH_HINTS):
        return "login"
    if any(h in path for h in _APPLY_PATH_HINTS):
        return "apply"
    if any(h in path for h in _CONFIRMATION_PATH_HINTS):
        return "confirmation"
    if any(h in path for h in _JOB_PATH_HINTS):
        return "job"
    return "unknown"


def http_class_of(status: Any) -> str:
    """Map a nav status to the bounded enum: 2xx/3xx/4xx/5xx/none/unknown."""
    if status is None:
        return "none"
    try:
        s = int(status)
    except Exception:
        return "unknown"
    if 200 <= s < 300:
        return "2xx"
    if 300 <= s < 400:
        return "3xx"
    if 400 <= s < 500:
        return "4xx"
    if 500 <= s < 600:
        return "5xx"
    return "unknown"


def redirect_count_of(resp: Any) -> int:
    """Top-level navigation redirect count only — walks Playwright's existing
    request.redirected_from chain for the main-frame response. Never the
    request URLs themselves, just a bounded integer count."""
    count = 0
    try:
        req = getattr(resp, "request", None)
        for _ in range(20):  # bounded walk; a redirect chain this long is not real
            if req is None:
                break
            prev = req.redirected_from
            if prev is None:
                break
            count += 1
            req = prev
    except Exception:
        return 0
    return count


# --------------------------------------------------------------------------- #
# Bounded, read-only page probe
# --------------------------------------------------------------------------- #

# Single evaluate() call so one bounded round-trip covers auth/form/submit/
# validation signals. Returns booleans and a short list of already-known HTML
# input `type` values only — never labels, names, values, or free text.
#
# __HAS_VISIBLE_CHALLENGE_FRAME__ is a placeholder token, not JS — spliced in
# below via .replace() rather than an f-string, since the surrounding JS has
# enough of its own literal { } that hand-escaping all of them for an
# f-string would be error-prone. See src/challenge_detect.py: a bare
# iframe[src*="recaptcha"] match previously false-positived on invisible v3/
# Enterprise scoring anchors present on ordinary, unblocked pages.
_PROBE_JS = r"""() => {
    // sentinel: aces-399 forensic-probe harness
    try {
        const body = (document.body && document.body.innerText || '').toLowerCase();
        const captcha = (__HAS_VISIBLE_CHALLENGE_FRAME__)
            || /verify you are human|checking your browser|cloudflare/i.test(body);
        const password = !!document.querySelector('input[type="password"]');
        const loginText = /(sign in|log in|login|create account|forgot password|sso|single sign-on)/i.test(body);
        const controls = Array.from(document.querySelectorAll(
            'input:not([type="hidden"]), textarea, select')).slice(0, 60);
        const kinds = controls.map(el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'textarea') return 'textarea';
            if (tag === 'select') return 'select';
            return (el.getAttribute('type') || 'text').toLowerCase();
        });
        const fillable = controls.filter(el => {
            const t = (el.getAttribute('type') || '').toLowerCase();
            return !['submit', 'button'].includes(t);
        });
        const submitPresent = !!document.querySelector(
            'button[type="submit"], input[type="submit"], button[type="button"]');
        const invalidPresent = !!document.querySelector(
            '[aria-invalid="true"], .is-invalid, .field-error, .error-message, input:invalid');
        return {
            captcha, password, loginText, kinds,
            formPresent: fillable.length >= 2,
            submitPresent, invalidPresent,
        };
    } catch (e) {
        return null;
    }
}""".replace("__HAS_VISIBLE_CHALLENGE_FRAME__", HAS_VISIBLE_CHALLENGE_FRAME_JS)

# Only HTML input `type` attribute values map through — anything else (an
# arbitrary label, id, or name an attacker could control) is silently dropped,
# never forwarded.
_CONTROL_KIND_MAP = {
    "text": "text_input", "email": "text_input", "tel": "text_input",
    "search": "text_input", "url": "text_input", "number": "text_input",
    "date": "text_input", "month": "text_input", "week": "text_input",
    "password": "password_input",
    "textarea": "textarea",
    "select": "select", "select-one": "select", "select-multiple": "select",
    "checkbox": "checkbox",
    "radio": "radio",
    "file": "file_input",
}

_PROBE_DEFAULTS: dict = {
    "form_present": False,
    "submit_control_present": False,
    "validation_errors_present": False,
    "control_kinds": [],
    "auth_state": "unknown",
    "capture_incomplete": True,
}


async def probe_page_evidence(page: Any, timeout: float = 1.5) -> dict:
    """Bounded, read-only page probe. Never raises and never gates the real
    apply flow — call it AFTER the action it describes, not before/during."""
    try:
        raw = await asyncio.wait_for(page.evaluate(_PROBE_JS), timeout=timeout)
    except Exception as exc:
        out = dict(_PROBE_DEFAULTS)
        out["capture_error_class"] = type(exc).__name__[:64]
        return out
    if not raw:
        out = dict(_PROBE_DEFAULTS)
        out["capture_error_class"] = "EmptyProbeResult"
        return out

    kinds: list[str] = []
    try:
        for k in (raw.get("kinds") or [])[:60]:
            norm = _CONTROL_KIND_MAP.get(str(k).lower())
            if norm and norm not in kinds:
                kinds.append(norm)
            if len(kinds) >= 10:
                break
    except Exception:
        kinds = []

    if raw.get("captcha"):
        auth_state = "bot_challenge"
    elif raw.get("password") and raw.get("loginText"):
        auth_state = "redirected_to_signin"
    elif raw.get("password"):
        auth_state = "logged_out"
    elif raw.get("formPresent"):
        auth_state = "logged_in"
    else:
        auth_state = "unknown"

    return {
        "form_present": bool(raw.get("formPresent")),
        "submit_control_present": bool(raw.get("submitPresent")),
        "validation_errors_present": bool(raw.get("invalidPresent")),
        "control_kinds": kinds,
        "auth_state": auth_state,
        "capture_incomplete": False,
    }


# --------------------------------------------------------------------------- #
# Strict allowlist sanitizer for the `forensic_phase` evidence contract
# --------------------------------------------------------------------------- #

_PATH_CLASS_VALUES = frozenset({"apply", "login", "job", "confirmation", "unknown"})
_AUTH_STATE_VALUES = frozenset(
    {"logged_in", "logged_out", "redirected_to_signin", "bot_challenge", "unknown"}
)
_HTTP_CLASS_VALUES = frozenset({"2xx", "3xx", "4xx", "5xx", "none", "unknown"})
_CONTROL_KIND_VALUES = frozenset(_CONTROL_KIND_MAP.values())
_FAILURE_REASON_VALUES = frozenset({
    "none", "captcha", "login_wall", "cta_not_found", "form_not_found",
    "submit_not_found", "validation_blocked", "navigation_error",
    "timeout", "unknown",
})
_PHASE_VALUES = frozenset(p.value for p in AttemptPhase)

# Opaque identifiers (attempt_id, job_id): letters/digits/underscore/hyphen only.
# This alone rejects an email (contains "@"), a URL (contains "://" and "/"),
# and a formatted phone number (contains spaces/parens/"+") without needing a
# denylist for any of those shapes specifically.
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
# Low-cardinality identifiers (source/vendor/adapter): short lowercase-ish tokens.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
# Bare hostname only — no scheme, path, query, "@", or ":" (port) survives.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
# Exception class names are short identifiers, never free text.
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_MISSING = object()


def _valid_id(v: Any) -> Any:
    s = str(v) if v is not None else ""
    return s if _ID_RE.match(s) else _MISSING


def _valid_token(v: Any) -> Any:
    s = str(v) if v is not None else ""
    return s if _TOKEN_RE.match(s) else _MISSING


def _valid_host(v: Any) -> Any:
    s = str(v) if v is not None else ""
    if s == "":
        return ""
    return s if _HOST_RE.match(s) else _MISSING


def _valid_enum(values: frozenset):
    def _check(v: Any) -> Any:
        s = str(v) if v is not None else ""
        return s if s in values else _MISSING
    return _check


def _valid_bool(v: Any) -> Any:
    return bool(v) if isinstance(v, (bool, int)) and not isinstance(v, str) else False


def _valid_control_kinds(v: Any) -> Any:
    if not isinstance(v, (list, tuple)):
        return []
    out: list[str] = []
    for item in v:
        s = str(item)
        if s in _CONTROL_KIND_VALUES and s not in out:
            out.append(s)
        if len(out) >= 10:
            break
    return out


def _valid_redirect_count(v: Any) -> Any:
    try:
        n = int(v)
    except Exception:
        return 0
    return max(0, min(n, 50))


def _valid_error_class(v: Any) -> Any:
    s = str(v) if v is not None else ""
    return s if _ERROR_CLASS_RE.match(s) else "UnknownError"


# Every allowed field name maps to its validator. This dict IS the allowlist:
# a key absent here can never appear in a sanitized forensic_phase event.
_VALIDATORS = {
    "attempt_id": _valid_id,
    "job_id": _valid_id,
    "phase": _valid_enum(_PHASE_VALUES),
    "source": _valid_token,
    "vendor": _valid_token,
    "adapter": _valid_token,
    "host": _valid_host,
    "path_class": _valid_enum(_PATH_CLASS_VALUES),
    "auth_state": _valid_enum(_AUTH_STATE_VALUES),
    "form_present": _valid_bool,
    "submit_control_present": _valid_bool,
    "validation_errors_present": _valid_bool,
    "control_kinds": _valid_control_kinds,
    "http_class": _valid_enum(_HTTP_CLASS_VALUES),
    "redirect_count": _valid_redirect_count,
    "failure_reason_code": _valid_enum(_FAILURE_REASON_VALUES),
    "capture_incomplete": _valid_bool,
    "capture_error_class": _valid_error_class,
}

ALLOWED_FORENSIC_FIELDS = frozenset(_VALIDATORS)


def sanitize_forensic_fields(fields: dict) -> dict:
    """Allowlist sanitizer for the `forensic_phase` evidence contract.

    Only keys in `_VALIDATORS` can survive, and each survivor is re-validated
    against its own type/enum/regex — an unexpected key, an out-of-vocabulary
    enum value, an email/phone/URL-shaped id, or a full URL passed as `host`
    is dropped rather than forwarded. Never raises: a validator failure drops
    that one field instead of propagating.
    """
    if not isinstance(fields, dict):
        return {}
    out: dict = {}
    for key, validator in _VALIDATORS.items():
        if key not in fields:
            continue
        try:
            val = validator(fields[key])
        except Exception:
            continue
        if val is _MISSING:
            continue
        out[key] = val
    return out


# --------------------------------------------------------------------------- #
# RunLog emission — the only place this module writes anything
# --------------------------------------------------------------------------- #


def emit_forensic_phase(run_log: Any, **fields) -> dict | None:
    """Best-effort `forensic_phase` event. Never raises; a capture failure is
    recorded as `capture_incomplete=True` rather than propagated, and never
    alters the caller's control flow (no return value is meant to be branched
    on beyond "was anything written")."""
    if run_log is None:
        return None
    try:
        safe = sanitize_forensic_fields(fields)
    except Exception as exc:  # pragma: no cover - sanitize_forensic_fields already swallows
        safe = sanitize_forensic_fields({
            "attempt_id": fields.get("attempt_id"),
            "job_id": fields.get("job_id"),
            "phase": fields.get("phase") or AttemptPhase.UNKNOWN.value,
            "capture_incomplete": True,
            "capture_error_class": type(exc).__name__,
        })
    try:
        return run_log.emit("forensic_phase", **safe)
    except Exception:
        return None


def emit_universal_attempt_started(run_log: Any, *, attempt_id: str, job_id: str,
                                   source: str) -> None:
    """Outer-boundary event: every apply attempt (registry or legacy path) gets
    one of these, independent of whether a live Page/rich evidence exists.
    Uses RunLog's own existing sanitizer like every other plain event in this
    codebase — these fields are already low-cardinality identifiers, not raw
    evidence, so the stricter forensic_phase allowlist is not required here."""
    if run_log is None:
        return
    try:
        run_log.emit(
            "apply_attempt_started",
            attempt_id=str(attempt_id or ""), job_id=str(job_id or ""),
            source=str(source or ""), rich_evidence_available=False,
        )
    except Exception:
        pass


def emit_universal_attempt_finished(run_log: Any, *, attempt_id: str, job_id: str,
                                    source: str, status: str, applied: bool) -> None:
    """Outer-boundary completion event — always emitted, even when the inner
    apply() call raised, so every attempt_id started above also has a final
    status. See emit_universal_attempt_started for why the strict allowlist
    sanitizer is not used for this event type."""
    if run_log is None:
        return
    try:
        run_log.emit(
            "apply_attempt_finished",
            attempt_id=str(attempt_id or ""), job_id=str(job_id or ""),
            source=str(source or ""), status=str(status or "unknown"),
            applied=bool(applied), rich_evidence_available=False,
        )
    except Exception:
        pass
