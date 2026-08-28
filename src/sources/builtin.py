"""
BuiltIn (builtin.com) scraper.

Discovery is a pure httpx fetch of builtin.com's plain server-rendered HTML —
no browser needed. Each job-detail page embeds two structured JSON blobs:
a `Builtin.jobPostInit({...})` JS-call argument (companyName, howToApply, id)
and a schema.org `application/ld+json` JobPosting (title, description,
jobLocation, baseSalary). BuiltIn never hosts applications itself — every
listing's `howToApply` points off-site — so apply() re-fetches the detail
page and delegates to JobrightScraper.apply_external_ats_job(), the same
Workday/Greenhouse/Lever/etc. filler already used by indeed.py/themuse.py.

Cloudflare bot-management is active on this site: every request uses a
realistic User-Agent plus the existing self._delay() pacing, and any response
that looks like a Cloudflare challenge page is treated as "no data" rather
than saved as a job.
"""
from __future__ import annotations

import html as _html_mod
import json
import re
from datetime import datetime

import httpx
from rich.console import Console

from .base import BaseScraper, JobExpiredError
from src.resume_helper import PDFTextLayerError

console = Console()

BUILTIN_BASE = "https://builtin.com"
BUILTIN_JOBS_URL = "https://builtin.com/jobs"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_PAGES = 10                    # hard cap on listing pages regardless of max_jobs
_MAX_CONSECUTIVE_CHALLENGES = 3    # abort remaining detail fetches after this many blocked in a row

_JOB_LINK_RE = re.compile(r'data-alias="(/job/[^"?#]+)"')
_JOBPOSTINIT_MARKER = "Builtin.jobPostInit("
_LDJSON_RE = re.compile(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', re.S)
_CF_TITLE_RE = re.compile(r"<title>\s*(Just a moment|Attention Required)", re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_CLOSE_RE = re.compile(r"</(p|div|li|br|h[1-6])\s*>", re.I)
_WS_RE = re.compile(r"[ \t\xa0]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _BLOCK_CLOSE_RE.sub("\n", raw_html)
    text = _TAG_RE.sub("", text)
    text = _html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _infer_remote_type(title: str, location: str, description: str = "") -> str:
    """Mirrors src.discovery.ats_api._infer_remote_type (kept local to avoid a
    cross-package import from src/sources/ into src/discovery/ for one helper —
    same convention followed in themuse.py)."""
    text = f"{title} {location} {description}".lower()
    no_remote = any(p in text for p in ["no remote", "not remote", "not a remote", "cannot be remote"])
    if "remote" in text and "hybrid" not in text and not no_remote:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    if any(w in text for w in ["onsite", "on-site", "on site", "in office", "in-office"]) or no_remote:
        return "onsite"
    if "remote" in text and not no_remote:
        return "remote"
    return "unknown"


def _extract_job_post_init(html_text: str) -> dict | None:
    """Extract the JSON argument of Builtin.jobPostInit({...}) via a balanced-
    brace scan — a non-greedy regex would truncate at the first nested '}'
    since the payload contains nested objects."""
    idx = html_text.find(_JOBPOSTINIT_MARKER)
    if idx == -1:
        return None
    start = html_text.find("{", idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(html_text)):
        ch = html_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_ld_json_posting(html_text: str) -> dict:
    """Return the schema.org JobPosting dict from an embedded ld+json blob, or
    {} if none is found. Drupal HTML-entity-encodes the '+' in the type
    attribute (application/ld&#x2B;json) — both forms are matched."""
    for match in _LDJSON_RE.finditer(html_text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "@graph" in data:
            candidates = data.get("@graph") or []
        else:
            candidates = [data]
        if isinstance(candidates, dict):
            candidates = [candidates]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _format_location(job_location) -> str:
    if not job_location:
        return "Not specified"
    entries = job_location if isinstance(job_location, list) else [job_location]
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address") or {}
        city = address.get("addressLocality", "")
        region = address.get("addressRegion", "")
        country = address.get("addressCountry", "")
        piece = ", ".join(p for p in [city, region] if p) or country
        if piece and piece not in parts:
            parts.append(piece)
    return "; ".join(parts) if parts else "Not specified"


def _format_salary(base_salary) -> str:
    if not isinstance(base_salary, dict):
        return ""
    value = base_salary.get("value") or {}
    currency = base_salary.get("currency", "")
    min_val = value.get("minValue")
    max_val = value.get("maxValue")
    unit = value.get("unitText", "")

    def _fmt(n):
        return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)

    if min_val is None and max_val is None:
        return ""
    if min_val is not None and max_val is not None:
        rng = f"{_fmt(min_val)} - {_fmt(max_val)}"
    else:
        rng = _fmt(min_val if min_val is not None else max_val)

    label = " ".join(p for p in [currency, rng] if p)
    return f"{label}/{unit}" if unit else label


def _looks_like_cf_challenge(resp: httpx.Response) -> bool:
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if _CF_TITLE_RE.search(resp.text[:2000]):
        return True
    # A genuine listing/detail page always contains one of these markers; a
    # short body missing both is itself the signal of a substituted
    # challenge/error page.
    if len(resp.text) < 3000 and _JOBPOSTINIT_MARKER not in resp.text and "/job/" not in resp.text:
        return True
    return False


class BuiltInScraper(BaseScraper):
    name = "builtin"

    # ------------------------------------------------------------------
    # scrape — pure HTTP, no browser
    # ------------------------------------------------------------------

    async def scrape(self) -> list[dict]:
        console.print(f"[blue]BuiltIn:[/blue] Fetching jobs (max {self.max_jobs})…")

        detail_urls: list[str] = []
        seen: set[str] = set()

        async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0, follow_redirects=True) as client:
            for page_num in range(1, _MAX_PAGES + 1):
                if len(detail_urls) >= self.max_jobs:
                    break
                resp = await self._safe_get(client, f"{BUILTIN_JOBS_URL}?page={page_num}")
                await self._delay()
                if resp is None:
                    break
                fresh = [u for u in self._extract_job_links(resp.text) if u not in seen]
                if not fresh:
                    break
                seen.update(fresh)
                detail_urls.extend(fresh)

            jobs: list[dict] = []
            consecutive_challenges = 0
            for url in detail_urls[: self.max_jobs]:
                resp = await self._safe_get(client, url)
                await self._delay()
                if resp is None:
                    consecutive_challenges += 1
                    if consecutive_challenges >= _MAX_CONSECUTIVE_CHALLENGES:
                        console.print("[yellow]BuiltIn: repeated blocked responses — stopping early.[/yellow]")
                        break
                    continue
                consecutive_challenges = 0
                job = self._parse_detail_page(url, resp.text)
                if job:
                    jobs.append(job)

        console.print(f"[blue]BuiltIn:[/blue] {len(jobs)} job(s) found.")
        return jobs

    async def _safe_get(self, client: httpx.AsyncClient, url: str):
        try:
            resp = await client.get(url)
        except Exception as exc:
            console.print(f"[red]BuiltIn: request failed for {url}: {exc}[/red]")
            return None
        if resp.status_code >= 400:
            console.print(f"[red]BuiltIn: HTTP {resp.status_code} for {url}[/red]")
            return None
        if _looks_like_cf_challenge(resp):
            console.print(f"[yellow]BuiltIn: looks like a bot-check page for {url} — skipping.[/yellow]")
            return None
        return resp

    def _extract_job_links(self, html_text: str) -> list[str]:
        links: list[str] = []
        for path in _JOB_LINK_RE.findall(html_text):
            url = f"{BUILTIN_BASE}{path}"
            if url not in links:
                links.append(url)
        return links

    def _parse_detail_page(self, url: str, html_text: str) -> dict | None:
        post_init = _extract_job_post_init(html_text) or {}
        job_blob = post_init.get("job") if isinstance(post_init.get("job"), dict) else post_init
        ld = _extract_ld_json_posting(html_text)

        title = ld.get("title") or job_blob.get("title") or ""
        company = job_blob.get("companyName") or (ld.get("hiringOrganization") or {}).get("name") or ""
        if not title or not company:
            return None

        location_str = _format_location(ld.get("jobLocation"))
        description = _html_to_text(ld.get("description") or "")
        salary_raw = _format_salary(ld.get("baseSalary"))
        how_to_apply = (job_blob.get("howToApply") or "").strip()

        return {
            "job_id": self._make_job_id(url),
            "source": "builtin",
            "title": title,
            "company": company,
            "location": location_str,
            "salary_raw": salary_raw,
            "remote_type": _infer_remote_type(title, location_str, description),
            "url": url,
            "description": description,
            "discovered_at": datetime.utcnow().isoformat(),
            # Stashed for apply(): orchestrator._classify_apply_readiness()
            # already reads extra_json.ats_url for pre-apply routing.
            "ats_url": how_to_apply,
            "builtin_job_id": job_blob.get("id"),
            "is_easy_apply": job_blob.get("isEasyApply"),
        }

    # ------------------------------------------------------------------
    # apply — re-fetch fresh, then delegate to Jobright
    # ------------------------------------------------------------------

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        console.print(f"\n[blue]BuiltIn Apply:[/blue] {job.get('title')} @ {job.get('company')}")
        self.last_apply_status = "started"
        self.last_apply_detail = ""

        ext_url = await self._resolve_ats_url(job)
        return await self._apply_via_ats(job, ext_url, auto_submit=auto_submit)

    async def _resolve_ats_url(self, job: dict) -> str:
        """Re-fetch the job-detail page fresh (howToApply may have changed
        since scrape time). Falls back to the ats_url stashed in extra_json at
        scrape time if the re-fetch fails or is blocked."""
        url = job.get("url", "")
        stashed = self._stashed_ats_url(job)

        async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
            except Exception as exc:
                console.print(f"[yellow]BuiltIn: re-fetch failed ({exc}) — using stashed ATS URL.[/yellow]")
                return stashed

            if resp.status_code == 404:
                raise JobExpiredError(f"BuiltIn job no longer available: {url}")
            if resp.status_code >= 400 or _looks_like_cf_challenge(resp):
                console.print("[yellow]BuiltIn: re-fetch blocked — using stashed ATS URL.[/yellow]")
                return stashed

            post_init = _extract_job_post_init(resp.text) or {}

        job_blob = post_init.get("job") if isinstance(post_init.get("job"), dict) else post_init
        fresh_url = (job_blob.get("howToApply") or "").strip()
        return fresh_url or stashed

    @staticmethod
    def _stashed_ats_url(job: dict) -> str:
        """Mirrors orchestrator._classify_apply_readiness()'s exact defensive
        parsing of extra_json (a JSON string column value once round-tripped
        through the DB, a plain dict when read straight off a freshly-scraped
        job before it's ever been persisted)."""
        extra = job.get("extra_json") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}
        if isinstance(extra, dict) and extra.get("ats_url"):
            return extra["ats_url"].strip()
        # A freshly-scraped-but-not-yet-persisted job still has ats_url at the
        # top level (state_manager.upsert_job() only moves it into extra_json
        # once written to the DB).
        return (job.get("ats_url") or "").strip()

    async def _apply_via_ats(self, job: dict, ext_url: str, *, auto_submit: bool) -> bool:
        """Delegate to Jobright's battle-tested ATS filler — mirrors
        TheMuseScraper._apply_via_ats / LinkedInScraper._apply_external_ats."""
        if not ext_url:
            return self._set_apply_outcome(
                "builtin_no_ats_url",
                f"Could not resolve an external company ATS URL from {job.get('url', '')}.",
            )

        from .jobright import JobrightScraper

        console.print(f"[blue]BuiltIn:[/blue] Handing off to ATS: {ext_url[:80]}")
        try:
            jr = JobrightScraper(self.config)
            result = await jr.apply_external_ats_job(job, ext_url, auto_submit=auto_submit)
            self.last_apply_status = jr.last_apply_status
            self.last_apply_detail = jr.last_apply_detail
            self.last_apply_ats_url = getattr(jr, "last_apply_ats_url", "") or ext_url
            self._apply_analytics = getattr(jr, "_apply_analytics", None) or {}
            return result
        except PDFTextLayerError:
            raise
        except Exception as exc:
            return self._set_apply_outcome("builtin_external_apply_error", str(exc))
