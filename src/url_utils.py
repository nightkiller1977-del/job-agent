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
