"""Phase 0.1 — receipt verification.

A submit *click* is not an application. Before any adapter may report `submitted`,
it must observe a confirmation that the ATS accepted the application. This module
centralises that check so GenericAtsAdapter and BrowserUseRecovery agree on what
"receipt verified" means.

Kept import-light (no runtime playwright import) and fake-able: it only calls
`page.url` and `page.evaluate(...)`, both of which the adapter test fakes provide.
"""
from __future__ import annotations

import asyncio
import re

# Confirmation tokens matched as DELIMITED url segments (not raw substrings), so a
# job title like `/jobs/customer-success-manager` or `/jobs/applied-scientist` is
# never mistaken for a receipt. Bare "success"/"applied"/"submitted" are excluded on
# purpose; confirmation routes almost always carry thank-you/confirmation/received.
_URL_CONFIRM_RE = re.compile(
    r"(?:^|[/?=&#_-])"
    r"(thank[-_]?you|thanks|confirmation|confirmed|"
    r"application[-_]?(?:received|submitted|complete)|"
    r"successfully[-_]?(?:applied|submitted))"
    r"(?:$|[/?=&#_-])"
)

# JS run on the live page: looks for confirmation copy or an application/reference
# id in the visible body. Returns a short signal string, or null.
_RECEIPT_JS = r"""() => {
    const body = (document.body && document.body.innerText || '').toLowerCase();
    const patterns = [
        /thank you for (applying|your application)/,
        /your application (has been|was)? ?(received|submitted|sent)/,
        /application (successfully )?(received|submitted|complete)/,
        /we(?:'|’|)ve received your application/,
        /successfully (applied|submitted)/,
        /thanks for applying/,
    ];
    for (const p of patterns) { const m = body.match(p); if (m) return 't:' + m[0].slice(0, 60); }
    const ref = body.match(/(confirmation|reference|application)\s*(number|id|no\.?|#)\s*[:#]?\s*([a-z0-9][a-z0-9-]{3,})/i);
    if (ref) return 'ref:' + ref[3];
    return null;
}"""


async def _check_once(page) -> tuple[bool, str]:
    # 1. URL-based confirmation (cheapest, and robust to SPA re-render).
    url = ""
    try:
        url = (getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    if url and _URL_CONFIRM_RE.search(url):
        return True, f"url:{url[:80]}"

    # 2. Confirmation copy / reference id in the rendered body.
    try:
        signal = await page.evaluate(_RECEIPT_JS)
    except Exception:
        signal = None
    if signal:
        return True, str(signal)
    return False, ""


async def verify_receipt(page, retries: int = 0, delay: float = 0.4, sleep=None) -> tuple[bool, str]:
    """Return (verified, signal). `verified` is True only when a confirmation URL or
    confirmation copy/reference id is observed. Never raises.

    ATS submissions often confirm asynchronously (a follow-up request or SPA render
    after the click returns), so the submit path polls up to `retries` times before
    declaring the result ambiguous. `retries=0` (the default) does a single check —
    used where an immediate answer is wanted."""
    sleep = sleep or asyncio.sleep
    ok, sig = await _check_once(page)
    attempt = 0
    while not ok and attempt < retries:
        await sleep(delay)
        ok, sig = await _check_once(page)
        attempt += 1
    return ok, sig
