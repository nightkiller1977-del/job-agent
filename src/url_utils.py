import urllib.parse
import re

_UNSAFE_SCHEMES = ("javascript:", "data:", "mailto:", "tel:", "blob:", "file:", "about:")


def normalize_nav_url(raw: str, base: str = "") -> str:
    """Resolve a possibly-relative URL to an absolute http(s) URL safe to navigate to.

    Fixes the `net::ERR_NAME_NOT_RESOLVED at https://content/acom/...` class of bug:
    a relative path pulled from JSON (e.g. __NEXT_DATA__.externalApplyLink) that is
    not auto-resolved the way an anchor's .href is. Applies urljoin against the page
    URL, then enforces an http(s) scheme with a real hostname.

    Returns "" for empty, unsafe-scheme (javascript:/data:/...), or hostless inputs.
    """
    if not raw or not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.lower().startswith(_UNSAFE_SCHEMES):
        return ""
    try:
        resolved = urllib.parse.urljoin(base or "", raw)
        parsed = urllib.parse.urlparse(resolved)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.netloc or "." not in parsed.netloc:
        # reject hostless / single-label hosts like "content" (the acom bug)
        return ""
    return resolved


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
