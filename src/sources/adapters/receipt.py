"""Phase 0.1 — receipt verification with freshness-aware evidence.

A submit *click* is not an application. Before any adapter may report `submitted`,
it must observe a confirmation that the ATS accepted the application. This
module centralises that check so GenericAtsAdapter and BrowserUseRecovery agree
on what "receipt verified" means.

Freshness (added after the visible-stale defect):
    A whole-page scan cannot tell whether an "Application submitted" phrase was
    already on the page (a dashboard widget, a help panel, a previous-application
    notice) or appeared because of THIS attempt. `verify_receipt(page, baseline=...)`
    accepts a baseline captured BEFORE the submit boundary and reports fresh
    evidence only when the page's occurrence-count of matching acceptance
    elements has grown relative to the baseline.

    Callers capture a baseline by calling verify_receipt() with no baseline
    argument; the call side-effects `document.body.dataset.receiptBaselineCount`
    with the observed count, and returns the normal (ok, sig) tuple. A later
    verify_receipt(..., baseline=(ok, sig)) reads that stored count and
    compares it to the current count. This keeps the API a 2-tuple (backwards
    compatible with call sites that don't pass baseline).

Kept import-light (no runtime playwright import) and fake-able: only calls
`page.url` and `page.evaluate(...)`, both of which the adapter test fakes
provide.
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

# Tightened acceptance patterns. Each requires the phrase to be anchored to a
# sentence boundary (start of body OR after [.!\n]) AND followed by sentence-end
# punctuation. This rejects the phrase when it appears embedded in a longer
# descriptive sentence — job-description text, instructional text, form-field
# labels, negation clauses — without needing fragile negative lookarounds.
# Freshness handling below is the second line of defense for any stale match
# the raw regex still catches.
_RECEIPT_JS = r"""() => {
    const body = (document.body && document.body.innerText || '');
    const patterns = [
        // "Application (has been/was) (successfully) (submitted|received|sent|complete)."
        /(?:^|[.!\n]\s*)(application\s+(?:(?:has\s+been|was)\s+)?(?:successfully\s+)?(?:submitted|received|sent|complete))\s*[.!\n]/i,
        // "Your application (has been/was/is) (successfully) (submitted|received|sent)."
        /(?:^|[.!\n]\s*)(your\s+application\s+(?:has\s+been|was|is)\s+(?:successfully\s+)?(?:submitted|received|sent))\s*[.!\n]/i,
        // "Thanks/Thank you for applying to <specific target>."
        /(?:^|[.!\n]\s*)(thank(?:s|\syou)?\s+for\s+applying\s+to\s+[^.!?\n]{1,80})[.!\n]/i,
        // "We('ve/have) received your application"
        /(?:^|[.!\n]\s*)(we(?:'ve|\shave|ve)\s+received\s+your\s+application)\b/i,
    ];
    for (const pat of patterns) {
        const m = body.match(pat);
        if (m) return 't:' + m[1].toLowerCase().slice(0, 60);
    }
    // Reference id fallback (unchanged from prior implementation).
    const ref = body.match(/(confirmation|reference|application)\s*(number|id|no\.?|#)\s*[:#]?\s*([a-z0-9][a-z0-9-]{3,})/i);
    if (ref) return 'ref:' + ref[3];
    return null;
}"""

# Count matching acceptance-element occurrences on the current page. Used by
# the freshness gate: an increase between baseline and post-submit means a
# new matching element appeared (fresh evidence attributable to this attempt).
# Uses the same patterns as _RECEIPT_JS with the global flag so all occurrences
# are counted, not just the first.
_COUNT_JS = r"""() => {
    // sentinel: acceptance-count harness (used by verify_receipt freshness gate)
    const body = (document.body && document.body.innerText || '');
    const patterns = [
        /(?:^|[.!\n]\s*)(application\s+(?:(?:has\s+been|was)\s+)?(?:successfully\s+)?(?:submitted|received|sent|complete))\s*[.!\n]/gi,
        /(?:^|[.!\n]\s*)(your\s+application\s+(?:has\s+been|was|is)\s+(?:successfully\s+)?(?:submitted|received|sent))\s*[.!\n]/gi,
        /(?:^|[.!\n]\s*)(thank(?:s|\syou)?\s+for\s+applying\s+to\s+[^.!?\n]{1,80})[.!\n]/gi,
        /(?:^|[.!\n]\s*)(we(?:'ve|\shave|ve)\s+received\s+your\s+application)\b/gi,
    ];
    let n = 0;
    for (const pat of patterns) {
        const m = body.match(pat);
        if (m) n += m.length;
    }
    return n;
}"""

_STORE_COUNT_JS = (
    "(n) => { if (document.body) { document.body.dataset.receiptBaselineCount = String(n); } }"
)
_READ_COUNT_JS = (
    "() => { if (document.body) { return parseInt(document.body.dataset.receiptBaselineCount || '0', 10); } return 0; }"
)


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


async def _count_matches(page) -> int:
    try:
        raw = await page.evaluate(_COUNT_JS)
        return int(raw or 0)
    except Exception:
        return 0


async def _store_count(page, n: int) -> None:
    try:
        await page.evaluate(_STORE_COUNT_JS, n)
    except Exception:
        pass


async def _read_stored_count(page) -> int:
    try:
        raw = await page.evaluate(_READ_COUNT_JS)
        return int(raw or 0)
    except Exception:
        return 0


async def verify_receipt(
    page,
    retries: int = 0,
    delay: float = 0.4,
    sleep=None,
    baseline: tuple[bool, str] | None = None,
) -> tuple[bool, str]:
    """Return (verified, signal).

    Without `baseline`: behaves as before — a single (or polled) check for
    URL/text/reference-id acceptance evidence. Side-effects the page's
    `document.body.dataset.receiptBaselineCount` with the currently-observed
    match count so a later post-submit call can compare.

    With `baseline` (an earlier return value from verify_receipt): applies the
    freshness gate. The evidence is treated as fresh (verified True) only when
    the current match count is strictly greater than the count observed at
    baseline capture (i.e. a new acceptance element appeared). If baseline
    itself had no receipt, any current receipt counts as fresh.

    Never raises. `baseline` defaults to None so existing call sites remain
    valid without migration.
    """
    sleep = sleep or asyncio.sleep
    ok, sig = await _check_once(page)
    attempt = 0
    while not ok and attempt < retries:
        await sleep(delay)
        ok, sig = await _check_once(page)
        attempt += 1

    if not ok:
        # Even without a fired signal, record 0 as the baseline count so a
        # later post-submit call with baseline=(False, "") knows the page
        # had no matches at capture time.
        if baseline is None:
            await _store_count(page, 0)
        return False, ""

    # A receipt is currently observed.
    if baseline is None:
        # Baseline-capture call: stash the current match count for the later
        # post-submit call, and return normally.
        await _store_count(page, await _count_matches(page))
        return ok, sig

    # Freshness gate: baseline was captured before the submit boundary.
    baseline_ok, _baseline_sig = baseline
    if not baseline_ok:
        # Baseline had NO receipt; any current receipt is fresh evidence
        # attributable to this attempt.
        return ok, sig
    # Baseline had a receipt. Require the current match count to strictly
    # exceed the baseline count — i.e. a NEW matching element appeared —
    # so an identical stale panel that persists across submit does not
    # verify a new attempt.
    baseline_count = await _read_stored_count(page)
    current_count = await _count_matches(page)
    if current_count > baseline_count:
        return ok, sig
    return False, ""
