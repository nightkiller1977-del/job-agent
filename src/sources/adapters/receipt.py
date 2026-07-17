"""Phase 0.1 — receipt verification.

A submit *click* is not an application. Before any adapter may report `submitted`,
it must observe a confirmation that the ATS accepted the application. This module
centralises that check so GenericAtsAdapter and BrowserUseRecovery agree on what
"receipt verified" means.

Kept import-light (no runtime playwright import) and fake-able: it only calls
`page.url` and `page.evaluate(...)`, both of which the adapter test fakes provide.
"""
from __future__ import annotations

# Substrings that, in the post-submit URL, indicate the ATS routed to a
# confirmation page. Kept conservative — these are near-universal across vendors.
_URL_CONFIRM_TOKENS = (
    "thank", "confirmation", "confirmed", "submitted", "success",
    "complete", "applied", "application-received",
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


async def verify_receipt(page) -> tuple[bool, str]:
    """Return (verified, signal). `verified` is True only when a confirmation
    URL or confirmation copy/reference id is observed. Never raises."""
    # 1. URL-based confirmation (cheapest, and robust to SPA re-render).
    url = ""
    try:
        url = (getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    if url and any(tok in url for tok in _URL_CONFIRM_TOKENS):
        return True, f"url:{url[:80]}"

    # 2. Confirmation copy / reference id in the rendered body.
    try:
        signal = await page.evaluate(_RECEIPT_JS)
    except Exception:
        signal = None
    if signal:
        return True, str(signal)

    return False, ""
