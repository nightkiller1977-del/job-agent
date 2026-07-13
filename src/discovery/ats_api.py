import hashlib
import logging
import re
import urllib.parse
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger("job-agent.discovery.ats_api")

def canonicalize_url(url: str) -> str:
    """Removes tracking query parameters, source tags, referrals, and trailing slashes from URLs."""
    if not url:
        return ""

    try:
        parsed = urllib.parse.urlparse(url)
        # Parse query parameters into a list of tuples
        query_params = urllib.parse.parse_qsl(parsed.query)

        # Define tracking/source parameters to remove
        params_to_strip = {
            "source", "utm_source", "utm_medium", "utm_campaign", "utm_content",
            "ref", "lever-source", "gh_src", "gh_jid", "ashby_source", 
            "subscription_id", "s", "referred_by"
        }

        # Keep only non-tracking parameters
        cleaned_params = [
            (k, v) for k, v in query_params 
            if k.lower() not in params_to_strip
        ]

        # Reconstruct the query string
        new_query = urllib.parse.urlencode(cleaned_params)

        # Reassemble the URL with cleaned query and stripped fragment
        cleaned_url = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            new_query,
            ""  # Strip fragment
        ))

        # Strip trailing slash
        if cleaned_url.endswith("/"):
            cleaned_url = cleaned_url[:-1]

        return cleaned_url
    except Exception as e:
        logger.warning(f"Failed to canonicalize URL {url}: {e}")
        return url

def _infer_remote_type(title: str, location: str, description: str) -> str:
    """Helper to infer remote status based on title, location, and description text."""
    text = f"{title} {location} {description}".lower()

    # Check for negative remote indicators (e.g. "not remote", "no remote work")
    no_remote = any(phrase in text for phrase in ["no remote", "not remote", "not a remote", "cannot be remote"])

    if "remote" in text and "hybrid" not in text and not no_remote:
        return "remote"
    elif "hybrid" in text:
        return "hybrid"
    elif any(w in text for w in ["onsite", "on-site", "on site", "in office", "in-office"]) or no_remote:
        return "onsite"
    elif "remote" in text and not no_remote:
        return "remote"
    return "unknown"

def _extract_salary_from_text(text: str) -> str:
    """Helper to extract salary range from HTML or plain text job description using regex."""
    if not text:
        return ""
    # Strip HTML tags
    clean_text = re.sub(r'<[^>]*>', ' ', text)

    patterns = [
        r'\$[0-9]{3}(?:,[0-9]{3})*(?:\s*[kK])?\s*(?:-|to)\s*\$[0-9]{3}(?:,[0-9]{3})*(?:\s*[kK])?',
        r'\$[0-9]{2,3}\s*[kK]\s*(?:-|to)\s*\$[0-9]{2,3}\s*[kK]',
        r'\$[0-9]{3}(?:,[0-9]{3})*\s*/\s*yr',
        r'salary(?:\s*range)?:\s*\$[0-9]{3}(?:,[0-9]{3})*(?:\s*[kK])?'
    ]
    for pattern in patterns:
        matches = re.findall(pattern, clean_text, re.IGNORECASE)
        if matches:
            return matches[0].strip()
    return ""

async def fetch_greenhouse_jobs(board_token: str, company_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch and normalize job postings from the Greenhouse public job board API.

    URL: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    logger.info(f"Fetching Greenhouse jobs for board: {board_token}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Greenhouse jobs for {board_token}: {e}")
        return []

    jobs = data.get("jobs", [])
    normalized = []
    company = company_name or board_token.replace("-", " ").replace("_", " ").title()
    seen_urls = set()

    for job in jobs:
        raw_url = job.get("absolute_url") or f"https://boards.greenhouse.io/{board_token}/jobs/{job.get('id')}"
        job_url = canonicalize_url(raw_url)

        # Local deduplication
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        # Standardize job ID via 16-char MD5 hash of its unique canonical URL
        job_id = hashlib.md5(job_url.encode()).hexdigest()[:16]

        title = job.get("title", "")
        location_name = job.get("location", {}).get("name", "") if job.get("location") else ""
        content = job.get("content", "")

        remote_type = _infer_remote_type(title, location_name, content)
        salary_raw = _extract_salary_from_text(content)

        normalized.append({
            "job_id": job_id,
            "source": "greenhouse",
            "title": title,
            "company": company,
            "location": location_name,
            "salary_raw": salary_raw,
            "remote_type": remote_type,
            "url": job_url,
            "description": content,
            "status": "discovered"
        })

    return normalized

async def fetch_lever_jobs(company_id: str, company_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch and normalize job postings from the Lever public postings API.

    URL: https://api.lever.co/v0/postings/{company_id}?mode=json
    """
    url = f"https://api.lever.co/v0/postings/{company_id}?mode=json"
    logger.info(f"Fetching Lever jobs for company ID: {company_id}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Lever jobs for {company_id}: {e}")
        return []

    normalized = []
    company = company_name or company_id.replace("-", " ").replace("_", " ").title()
    seen_urls = set()

    for item in data:
        raw_url = item.get("hostedUrl") or f"https://jobs.lever.co/{company_id}/{item.get('id')}"
        job_url = canonicalize_url(raw_url)

        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        job_id = hashlib.md5(job_url.encode()).hexdigest()[:16]

        title = item.get("title", "")
        cats = item.get("categories", {})
        location_name = cats.get("location", "")
        desc = item.get("descriptionHtml") or item.get("description", "")

        all_locations = cats.get("allLocations", [])
        location_str = ", ".join(all_locations) if all_locations else location_name

        remote_type = _infer_remote_type(title, location_str, desc)

        salary_obj = item.get("salary", {})
        salary_raw = ""
        if salary_obj and isinstance(salary_obj, dict):
            min_val = salary_obj.get("min")
            max_val = salary_obj.get("max")
            curr = salary_obj.get("currency", "USD")
            if min_val is not None and max_val is not None:
                salary_raw = f"{curr} {min_val} - {max_val}"
            elif min_val is not None:
                salary_raw = f"{curr} {min_val}"
            elif max_val is not None:
                salary_raw = f"{curr} {max_val}"

        if not salary_raw:
            salary_raw = _extract_salary_from_text(desc)

        normalized.append({
            "job_id": job_id,
            "source": "lever",
            "title": title,
            "company": company,
            "location": location_str,
            "salary_raw": salary_raw,
            "remote_type": remote_type,
            "url": job_url,
            "description": desc,
            "status": "discovered"
        })

    return normalized

async def fetch_ashby_jobs(board_name: str, company_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch and normalize job postings from the Ashby public posting API.

    URL: https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true"
    logger.info(f"Fetching Ashby jobs for board: {board_name}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Ashby jobs for {board_name}: {e}")
        return []

    jobs = data.get("jobs", [])
    normalized = []
    company = company_name or board_name.replace("-", " ").replace("_", " ").title()
    seen_urls = set()

    for job in jobs:
        raw_url = job.get("jobUrl") or job.get("applyUrl") or f"https://jobs.ashbyhq.com/{board_name}/{job.get('id')}"
        job_url = canonicalize_url(raw_url)

        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        job_id = hashlib.md5(job_url.encode()).hexdigest()[:16]

        title = job.get("title", "")
        location_name = job.get("location", "")
        desc = job.get("descriptionHtml") or job.get("descriptionPlain") or ""

        remote_type = _infer_remote_type(title, location_name, desc)

        salary_raw = ""
        comp_info = job.get("compensation", {})
        if comp_info and isinstance(comp_info, dict):
            summary = comp_info.get("compensationTierSummary")
            if summary:
                salary_raw = summary

        if not salary_raw:
            salary_raw = _extract_salary_from_text(desc)

        normalized.append({
            "job_id": job_id,
            "source": "ashby",
            "title": title,
            "company": company,
            "location": location_name,
            "salary_raw": salary_raw,
            "remote_type": remote_type,
            "url": job_url,
            "description": desc,
            "status": "discovered"
        })

    return normalized
