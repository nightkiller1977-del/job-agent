"""Phase 0.1 — receipt verification with freshness-aware evidence.

A submit *click* is not an application. Before an adapter may report success,
it must observe evidence that the ATS accepted THIS application attempt.

The freshness baseline is deliberately Python-owned. A DOM-owned marker is not
safe: normal navigation, SPA body replacement, or a renderer remount destroys
it. ``capture_receipt_evidence`` therefore snapshots the confirmation URL,
first recognized DOM signal, and recognized-signal occurrence count before the
submit boundary. ``verify_receipt(..., baseline=...)`` polls until evidence is
new relative to that immutable snapshot or the retry budget expires.

The module is import-light and fake-able: page access is limited to ``page.url``
and ``page.evaluate``.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass


_URL_CONFIRM_RE = re.compile(
    r"(?:^|[/?=&#_-])"
    r"(thank[-_]?you|thanks|confirmation|confirmed|"
    r"application[-_]?(?:received|submitted|complete)|"
    r"successfully[-_]?(?:applied|submitted))"
    r"(?:$|[/?=&#_-])"
)


# One source list is embedded into both the first-signal matcher and the count
# matcher so the two cannot silently drift apart. The patterns are deliberately
# sentence/line anchored. End punctuation is optional only at end-of-body.
_PATTERN_SOURCES_JS = r"""[
    String.raw`(?:^|[.!?\n]\s*)(application\s+(?:(?:has\s+been|was)\s+)?(?:successfully\s+)?(?:submitted|received|sent|complete))(?=$|[.!?](?:\s|$)|\n)`,
    String.raw`(?:^|[.!?\n]\s*)(your\s+application\s+(?:has\s+been|was|is)\s+(?:successfully\s+)?(?:submitted|received|sent|complete))(?=$|[.!?](?:\s|$)|\n)`,
    String.raw`(?:^|[.!?\n]\s*)(thank(?:s|\s+you)\s+for\s+applying\s+to\s+.{1,80}?)(?=$|[!?](?:\s|$)|\.(?:\s|$)|\n)`,
    String.raw`(?:^|[.!?\n]\s*)(thank(?:s|\s+you)\s+for\s+applying)(?=$|[.!?](?:\s|$)|\n)`,
    String.raw`(?:^|[.!?\n]\s*)(thank\s+you\s+for\s+your\s+application)(?=$|[.!?](?:\s|$)|\n)`,
    String.raw`(?:^|[.!?\n]\s*)(we(?:'ve|\s+have|ve)\s+received\s+your\s+application)\b`,
]"""

_REFERENCE_SOURCE_JS = r"""(confirmation|reference|application)\s*(number|id|no\.?|#)\s*[:#]?\s*([a-z0-9][a-z0-9-]{3,})"""

_RECEIPT_JS = (
    r"""() => {
    // sentinel: acceptance-matcher harness (fakes recognize this line)
    const body = (document.body && document.body.innerText || '');
    const patternSources = """
    + _PATTERN_SOURCES_JS
    + r""";
    for (const src of patternSources) {
        const m = body.match(new RegExp(src, 'i'));
        if (m) return 't:' + m[1].toLowerCase().slice(0, 60);
    }
    const ref = body.match(new RegExp(String.raw`"""
    + _REFERENCE_SOURCE_JS
    + r"""`, 'i'));
    if (ref) return 'ref:' + ref[3];
    return null;
}"""
)

_COUNT_JS = (
    r"""() => {
    // sentinel: acceptance-count harness (used by freshness snapshots)
    const body = (document.body && document.body.innerText || '');
    const patternSources = """
    + _PATTERN_SOURCES_JS
    + r""";
    let n = 0;
    for (const src of patternSources) {
        const m = body.match(new RegExp(src, 'gi'));
        if (m) n += m.length;
    }
    const refs = body.match(new RegExp(String.raw`"""
    + _REFERENCE_SOURCE_JS
    + r"""`, 'gi'));
    if (refs) n += refs.length;
    return n;
}"""
)


@dataclass(frozen=True)
class ReceiptEvidence:
    """Immutable evidence snapshot captured on the Python side."""

    url_signal: str | None
    dom_signal: str | None
    match_count: int | None
    dom_available: bool

    @property
    def signal(self) -> str:
        return self.url_signal or self.dom_signal or ""

    @property
    def verified(self) -> bool:
        return bool(self.signal)


def _url_signal(page) -> str | None:
    try:
        url = (getattr(page, "url", "") or "").lower()
    except Exception:
        return None
    if url and _URL_CONFIRM_RE.search(url):
        return f"url:{url[:80]}"
    return None


async def _dom_signal(page) -> tuple[bool, str | None]:
    try:
        raw = await page.evaluate(_RECEIPT_JS)
    except Exception:
        return False, None
    return True, str(raw) if raw else None


async def _match_count(page) -> int | None:
    """Return recognized DOM occurrence count, or None when unavailable.

    Returning zero on evaluator failure would fail open: one stale baseline
    receipt could later look like a new 0→1 occurrence.
    """
    try:
        raw = await page.evaluate(_COUNT_JS)
        return int(raw or 0)
    except Exception:
        return None


async def capture_receipt_evidence(page) -> ReceiptEvidence:
    """Capture one immutable receipt-evidence snapshot. Never raises."""
    url_signal = _url_signal(page)
    dom_available, dom_signal = await _dom_signal(page)
    match_count = await _match_count(page) if dom_available else None
    return ReceiptEvidence(url_signal, dom_signal, match_count, dom_available)


def _coerce_legacy_baseline(baseline) -> ReceiptEvidence | None:
    if baseline is None:
        return None
    if isinstance(baseline, ReceiptEvidence):
        return baseline
    try:
        ok, sig = baseline
    except Exception:
        return ReceiptEvidence(None, None, None, False)
    sig = str(sig or "")
    if not ok:
        return ReceiptEvidence(None, None, 0, True)
    if sig.startswith("url:"):
        return ReceiptEvidence(sig, None, 0, True)
    return ReceiptEvidence(None, sig or None, None, False)


def _fresh_signal(current: ReceiptEvidence, baseline: ReceiptEvidence) -> str:
    if current.url_signal and current.url_signal != baseline.url_signal:
        return current.url_signal
    if not current.dom_signal:
        return ""
    if not baseline.dom_available:
        return ""
    if not baseline.dom_signal:
        return current.dom_signal
    if baseline.match_count is None:
        return ""
    if not current.dom_available:
        return ""
    if current.dom_signal != baseline.dom_signal:
        return current.dom_signal
    if current.match_count is None:
        return ""
    if current.match_count > baseline.match_count:
        return current.dom_signal
    return ""


async def verify_receipt(
    page,
    retries: int = 0,
    delay: float = 0.4,
    sleep=None,
    baseline: ReceiptEvidence | tuple[bool, str] | None = None,
) -> tuple[bool, str]:
    """Return ``(verified, signal)`` after optional polling.

    With a baseline, freshness is evaluated on EVERY poll. A stale-but-valid
    banner therefore does not stop the retry loop while a genuine async
    confirmation is still pending.
    """
    sleep = sleep or asyncio.sleep
    base = _coerce_legacy_baseline(baseline)

    # Backward compatibility for the pre-existing DOM harness. If it obtains
    # the old (ok, signal) tuple by calling verify_receipt immediately before
    # mutation, recover the exact Python-side snapshot saved on the Page object.
    # Production submit paths use capture_receipt_evidence directly.
    if base is not None and not isinstance(baseline, ReceiptEvidence):
        saved = getattr(page, "_receipt_last_evidence", None)
        try:
            legacy_sig = str(baseline[1] or "")
        except Exception:
            legacy_sig = ""
        if isinstance(saved, ReceiptEvidence) and saved.signal == legacy_sig:
            base = saved

    for attempt in range(retries + 1):
        current = await capture_receipt_evidence(page)
        if base is None:
            try:
                setattr(page, "_receipt_last_evidence", current)
            except Exception:
                pass
            signal = current.signal
        else:
            signal = _fresh_signal(current, base)
        if signal:
            return True, signal
        if attempt < retries:
            try:
                await sleep(delay)
            except Exception:
                return False, ""
    return False, ""
