"""Shared browser-side predicate: is a captcha/challenge iframe on this page
actually a visible, blocking challenge — not an invisible reCAPTCHA v3/
Enterprise scoring widget that every v3-protected page embeds regardless of
whether a real challenge is ever shown?

The naive `document.querySelector('iframe[src*="captcha"]')` check — until
now duplicated independently across four places (GenericAtsAdapter's own
blocker detection, the ACES-399 forensic probe, and the ACES-402/403
discovery spikes) — matches ANY matching iframe, including that invisible
anchor. A page can be fully loaded and genuinely unblocked while still
"failing" that check, because reCAPTCHA v3/Enterprise silently mounts an
invisible scoring iframe on ordinary pages that were never gated at all.

One shared, tested predicate instead of four independent copies that can
silently drift apart from each other — which is exactly how this went
unnoticed: nobody re-derives four implementations of the same idea in sync.

Filtering rule (a candidate iframe only counts as a real, visible challenge
if BOTH hold):
  - its own `src` does not carry `size=invisible` (the reCAPTCHA v3/
    Enterprise convention for an anchor iframe that scores silently and is
    never meant to be seen or interacted with);
  - it is not hidden via CSS (visibility/display) and has real screen area
    — a genuine v2 checkbox renders at roughly 304x78; a full-page
    interstitial is far larger; an invisible anchor/badge iframe a site
    doesn't intend to show is typically 0px or a few px in each dimension.
Neither signal alone is reliable (a real challenge could theoretically be
briefly zero-size before layout, and `size=invisible` is a convention, not a
guarantee across every provider) — both together is deliberately
conservative in the same direction: err toward NOT calling a page blocked,
since the existing behavior it replaces already erred the other way.
"""
from __future__ import annotations

# The set of iframe(s) treated as *candidates* for a captcha/challenge
# widget — unchanged from the four previous independent implementations, so
# this fix is purely about which candidates count as a real, visible
# challenge, not about which iframes get looked at in the first place.
CAPTCHA_IFRAME_SELECTOR = (
    'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="turnstile" i]'
)

# A self-contained JS boolean expression (no outer-scope references) — safe
# to splice verbatim into any page.evaluate() script, either standalone
# (`await page.evaluate(HAS_VISIBLE_CHALLENGE_FRAME_JS)`) or as a
# sub-expression inside a larger arrow function body.
HAS_VISIBLE_CHALLENGE_FRAME_JS = f"""Array.from(document.querySelectorAll('{CAPTCHA_IFRAME_SELECTOR}')).some((el) => {{
    const src = el.getAttribute('src') || '';
    if (/[?&]size=invisible\\b/i.test(src)) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 60 && rect.height >= 60;
}})"""
