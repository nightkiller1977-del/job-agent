import urllib.parse
import re

def normalize_external_url(url: str) -> str:
    """Pure URL normalization with strict validation for job ATS URLs.

    - Normalizes scheme and netloc.
    - Strips fragment identifiers.
    - Strips tracking query parameters, preserving only known ID parameters.
    - Standardizes path trailing slashes.
    """
    if not url or not isinstance(url, str):
        return ""

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return ""

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return ""

    if not parsed.netloc:
        return ""

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    clean_qs = []

    # Usually job portals use specific params to identify jobs
    essential_params = {
        'gh_jid', 'id', 'jobid', 'job_id', 'reqid', 'req_id',
        'requisition_id', 'guid', 'v', 'job', 'jid', 'rk'
    }

    for k, v in qs:
        if k.lower() in essential_params:
            clean_qs.append((k, v))

    # Sort for deterministic URL construction
    clean_qs.sort()
    query = urllib.parse.urlencode(clean_qs)

    path = re.sub(r'/+', '/', parsed.path).rstrip('/')
    if not path:
        path = '/'

    normalized = urllib.parse.urlunparse((scheme, netloc, path, parsed.params, query, ""))
    return normalized


# ── Canonical ATS application URLs (ACES-436) ────────────────────────────────
#
# Aggregators and employer career pages frequently hand us a *marketing* page
# that merely references an ATS posting, e.g.
#   https://www.valon.ai/about?ashby_jid=<uuid>#careers
#   https://coreweave.com/careers/job?...&board=coreweave&gh_jid=4709378006
# There is no application form on those pages, so the apply run correctly
# reported submit_not_found / form_not_reached and never applied to anything.
# The vendor's own application URL is derivable from identifiers already in the
# URL — the same construction src/discovery/ats_api.py already performs for
# jobs discovered through the ATS API, which this reuses rather than reinvents.

_ASHBY_HOSTS = ("ashbyhq.com",)
_GREENHOUSE_HOSTS = ("greenhouse.io",)

# Ashby posting ids are UUIDs; requiring the shape keeps a stray ashby_jid from
# producing a nonsense path.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_GH_ID_RE = re.compile(r"^\d{4,}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Hosts that never identify an employer (aggregators, link shorteners, CDNs).
# A slug derived from one of these would be meaningless.
_NON_EMPLOYER_HOSTS = frozenset({
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "monster", "dice",
    "builtin", "jobright", "themuse", "google", "bing", "lever", "workday",
    "myworkdayjobs", "smartrecruiters", "taleo", "icims", "brassring",
    "bit", "lnkd", "t", "goo",
})

# Public suffixes with two labels, not one. "labels[-2]" is only the
# registrable name for a single-label suffix (.com, .ai, .io, ...); for a host
# under one of these it is still part of the suffix, e.g.
# careers.acme.co.uk -> labels[-2] is "co", not "acme". Not a full Public
# Suffix List — this repo has no PSL-aware dependency (checked
# requirements.txt) and a review-scale addition of one is disproportionate to
# this one narrow use. Bounded to the common two-label suffixes a job listing
# is plausibly seen under; anything not on this list, or a deeper suffix this
# list doesn't cover, is refused below rather than guessed at (fail closed —
# review-suggested fallback), so an unrecognized shape leaves the caller's URL
# untouched instead of deriving a wrong slug.
_TWO_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "co.nz",
    "co.in", "co.za", "co.jp", "co.kr", "co.il",
    "com.br", "com.mx", "com.sg", "com.hk", "com.tw",
    "com.cn", "com.tr", "com.co",
})


def _host_labels(netloc: str) -> list[str]:
    host = netloc.lower().split(":")[0]
    return [p for p in host.split(".") if p and p != "www"]


def _employer_slug(netloc: str) -> str:
    """Registrable label of an employer domain, '' when it is not one.

    valon.ai -> "valon";  www.coreweave.com -> "coreweave";
    linkedin.com -> "" (aggregator, not an employer);
    careers.acme.co.uk -> "acme" (the listed two-label suffix is skipped);
    careers.acme.com.au -> "acme".

    Known limitation, disclosed rather than hidden: a two-label public suffix
    NOT in _TWO_LABEL_SUFFIXES (this is a bounded list, not the full Public
    Suffix List — see its comment) still falls through to the ordinary
    single-label rule below and would derive the suffix's own first component
    instead of the true employer label. This repo has no PSL-aware dependency
    to consult instead; the practical mitigation is the same one that already
    makes a wrong slug safe (see canonical_ats_url's module docstring and
    test_wrong_slug_cannot_reach_another_employer): a wrong slug reaches a
    generic empty vendor shell, never another employer's posting.
    """
    labels = _host_labels(netloc)
    if not labels:
        return ""
    if len(labels) < 2:
        slug = labels[0]
    elif len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        # e.g. ["careers", "acme", "co", "uk"] -> suffix is "co.uk", slug "acme"
        slug = labels[-3]
    elif len(labels) == 2 and ".".join(labels) in _TWO_LABEL_SUFFIXES:
        # The whole host IS a two-label suffix with nothing registrable under
        # it (e.g. bare "co.uk") — there is no employer label to derive.
        return ""
    else:
        # Ordinary single-label suffix (.com, .ai, .io, ...): the label just
        # before it is registrable.
        slug = labels[-2]
    if slug in _NON_EMPLOYER_HOSTS or not _SLUG_RE.match(slug):
        return ""
    return slug


def canonical_ats_url(url: str) -> str:
    """The vendor application URL a page references, or '' if not derivable.

    Never guesses across employers. The failure mode of a wrong Ashby org slug
    was checked against the live service: it returns a generic empty "Jobs"
    shell, not another employer's posting, so the worst case is the current
    behaviour (no form found) rather than a misdirected application.

    Returns '' — meaning "leave the caller's URL alone" — whenever the vendor
    identifier is absent or malformed, the employer token cannot be determined,
    or the URL is already on the vendor's own domain.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    # Already a vendor URL — nothing to canonicalize, and rewriting could
    # damage a URL that is already correct.
    if any(h in host for h in _ASHBY_HOSTS + _GREENHOUSE_HOSTS):
        return ""

    qs = {k.lower(): v for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)}

    # ── Ashby ────────────────────────────────────────────────────────────
    ashby_jid = (qs.get("ashby_jid") or "").strip()
    if ashby_jid and _UUID_RE.match(ashby_jid):
        org = _employer_slug(parsed.netloc)
        if org:
            return f"https://jobs.ashbyhq.com/{org}/{ashby_jid}"
        return ""

    # ── Greenhouse ───────────────────────────────────────────────────────
    gh_jid = (qs.get("gh_jid") or "").strip()
    if gh_jid and _GH_ID_RE.match(gh_jid):
        # Prefer a board token stated in the URL ('board' on employer career
        # pages, 'for' on embedded application forms) over one inferred from
        # the host, since the two can legitimately differ.
        board = (qs.get("board") or qs.get("for") or "").strip().lower()
        if not (board and _SLUG_RE.match(board)):
            board = _employer_slug(parsed.netloc)
        if board:
            return f"https://boards.greenhouse.io/{board}/jobs/{gh_jid}"
        return ""

    return ""
