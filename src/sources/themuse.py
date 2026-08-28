"""
TheMuse scraper.

Discovery uses TheMuse's official public JSON API — no browser needed
(https://www.themuse.com/developers/api/v2). TheMuse never hosts applications
itself (every listing is `type: "external"`); apply() opens the job's TheMuse
landing page in a one-off browser session to resolve the click-tracked
"Apply on Company Site" redirect, then delegates to
JobrightScraper.apply_external_ats_job() — the same Workday/Greenhouse/Lever/etc.
filler already used by indeed.py and linkedin.py for external-ATS applies.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime

import httpx
from rich.console import Console

from .base import BaseScraper, JobExpiredError
from src.resume_helper import PDFTextLayerError

console = Console()

THEMUSE_API_URL = "https://www.themuse.com/api/public/jobs"

# Same ATS-host list indeed.py/jobright.py already keep locally — no shared
# constant exists in the repo today, so each source keeps its own copy.
_ATS_HOSTS = [
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "taleo.net",
    "icims.com", "smartrecruiters.com", "bamboohr.com", "ashbyhq.com",
    "workable.com", "brassring.com", "successfactors.com",
    "myworkday.com", "jobs.lever.co", "apply.workable.com",
    "recruitingbypaycor.com", "paylocity.com", "ultipro.com",
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _infer_remote_type(title: str, location: str, description: str) -> str:
    """Mirrors src.discovery.ats_api._infer_remote_type (kept local to avoid a
    cross-package import from src/sources/ into src/discovery/ for one helper)."""
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


class TheMuseScraper(BaseScraper):
    name = "themuse"

    # ------------------------------------------------------------------
    # scrape — pure HTTP, no browser
    # ------------------------------------------------------------------

    async def scrape(self) -> list[dict]:
        keywords_raw = (self.search_settings.get("keywords") or "").strip()
        terms = [w.lower() for w in re.split(r"\s+", keywords_raw) if w]
        api_key = os.environ.get("THEMUSE_API_KEY", "").strip()

        console.print(f"[blue]TheMuse:[/blue] Fetching jobs (max {self.max_jobs})…")

        jobs: list[dict] = []
        seen_ids: set[str] = set()
        page = 0
        page_count = 1  # unknown until the first response; loop below self-corrects

        async with httpx.AsyncClient(timeout=15.0) as client:
            while page < page_count and len(jobs) < self.max_jobs:
                params: dict = {"page": page, "descending": "true"}
                if api_key:
                    params["api_key"] = api_key
                try:
                    resp = await client.get(THEMUSE_API_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    if status == 429:
                        console.print("[yellow]TheMuse: rate limited (429) — stopping with partial results.[/yellow]")
                    else:
                        console.print(f"[red]TheMuse: HTTP {status} on page {page}: {exc}[/red]")
                    break
                except Exception as exc:
                    console.print(f"[red]TheMuse: request failed on page {page}: {exc}[/red]")
                    break

                results = data.get("results") or []
                if not results:
                    break
                page_count = data.get("page_count", page_count)

                for item in results:
                    job = self._map_job(item, terms)
                    if not job or job["job_id"] in seen_ids:
                        continue
                    seen_ids.add(job["job_id"])
                    jobs.append(job)
                    if len(jobs) >= self.max_jobs:
                        break

                # Trust the API's own echoed page number over our local counter
                # in case its indexing surprises us (0- vs 1-based, etc.).
                page = data.get("page", page) + 1
                if page < page_count and len(jobs) < self.max_jobs:
                    await self._delay()

        console.print(f"[blue]TheMuse:[/blue] {len(jobs)} job(s) found.")
        return jobs

    def _map_job(self, item: dict, terms: list[str]) -> dict | None:
        url = ((item.get("refs") or {}).get("landing_page") or "").strip()
        if not url:
            return None

        title = item.get("name", "")
        company = (item.get("company") or {}).get("name", "")
        locations = item.get("locations") or []
        location_str = ", ".join(l.get("name", "") for l in locations if l.get("name")) or "Not specified"
        description = _html_to_text(item.get("contents") or "")

        if terms:
            haystack = f"{title} {description}".lower()
            if not any(term in haystack for term in terms):
                return None

        return {
            "job_id": self._make_job_id(url),
            "source": "themuse",
            "title": title,
            "company": company,
            "location": location_str,
            "salary_raw": "",  # TheMuse's public API has no structured salary field
            "remote_type": _infer_remote_type(title, location_str, description),
            "url": url,  # TheMuse's own landing page — apply() revisits this
            "description": description,
            "discovered_at": datetime.utcnow().isoformat(),
            "themuse_id": item.get("id"),
        }

    # ------------------------------------------------------------------
    # apply — resolve ATS URL via a one-off browser session, then delegate
    # ------------------------------------------------------------------

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        console.print(f"\n[blue]TheMuse Apply:[/blue] {job.get('title')} @ {job.get('company')}")
        self.last_apply_status = "started"
        self.last_apply_detail = ""

        ext_url = await self._resolve_ats_url(job)
        return await self._apply_via_ats(job, ext_url, auto_submit=auto_submit)

    async def _resolve_ats_url(self, job: dict) -> str:
        """Open TheMuse's own landing page and resolve the real company ATS URL.

        TheMuse never hosts applications (every listing's type is "external").
        The apply CTA routes through a click-tracked /job/redirect/<id> path, so
        we load the page, then either find a direct ATS anchor or click the apply
        control and capture wherever it lands (same tab or a new one).
        """
        url = job.get("url", "")
        page = await self._start_browser()
        ext_url = ""
        try:
            await self._safe_goto(page, url, timeout=30000)
            await self._delay(2, 3)

            page_text = await self._safe_evaluate(page, "document.body.innerText", default="")
            if any(p in page_text.lower() for p in [
                "no longer accepting applications", "position has been filled",
                "this job is no longer available",
            ]):
                raise JobExpiredError("TheMuse job no longer active")

            ext_url = await self._extract_apply_url(page)

            if not ext_url:
                console.print("[blue]TheMuse:[/blue] No direct ATS link found — clicking apply CTA…")
                before_pages = set(p.url for p in page.context.pages if p.url)
                before_url = page.url
                clicked = await self._safe_evaluate(page, """
                () => {
                    const btn = Array.from(document.querySelectorAll('a, button'))
                        .find(el => /apply (now|on (the )?company site|on (the )?employer site)/i.test((el.innerText || '').trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """, default=False)
                if clicked:
                    await self._delay(2, 3)
                    for p in page.context.pages:
                        try:
                            if p.url and p.url not in before_pages and "themuse.com" not in p.url:
                                ext_url = p.url
                                break
                        except Exception:
                            continue
                    if not ext_url and page.url != before_url and "themuse.com" not in page.url:
                        ext_url = page.url  # same-tab redirect resolved

        except JobExpiredError:
            await self._close_browser()
            raise
        except Exception as exc:
            console.print(f"[red]TheMuse: error resolving ATS URL: {exc}[/red]")
        finally:
            await self._close_browser()

        return ext_url

    async def _extract_apply_url(self, page) -> str:
        """Find the external company ATS URL on a TheMuse landing page.
        Returns empty string if none found in the static DOM (a click-through
        redirect is then attempted by the caller)."""
        return await self._safe_evaluate(page, f"""
        () => {{
            const ATS = {_ATS_HOSTS!r};
            for (const a of document.querySelectorAll('a[href]')) {{
                const h = a.href || '';
                if (!h.includes('themuse.com') && ATS.some(p => h.includes(p))) return h;
            }}
            return '';
        }}
        """, default="")

    async def _apply_via_ats(self, job: dict, ext_url: str, *, auto_submit: bool) -> bool:
        """Delegate to Jobright's battle-tested ATS filler. Split out from
        apply()/_resolve_ats_url() so the delegation contract (status/detail/
        ats_url/analytics propagation, and PDFTextLayerError passthrough) is
        unit-testable without a real or stubbed browser — mirrors
        LinkedInScraper._apply_external_ats.
        """
        if not ext_url:
            return self._set_apply_outcome(
                "themuse_no_ats_url",
                f"Could not resolve an external company ATS URL from {job.get('url', '')}.",
            )

        from .jobright import JobrightScraper

        console.print(f"[blue]TheMuse:[/blue] Handing off to ATS: {ext_url[:80]}")
        try:
            jr = JobrightScraper(self.config)
            result = await jr.apply_external_ats_job(job, ext_url, auto_submit=auto_submit)
            self.last_apply_status = jr.last_apply_status
            self.last_apply_detail = jr.last_apply_detail
            self.last_apply_ats_url = getattr(jr, "last_apply_ats_url", "") or ext_url
            self._apply_analytics = getattr(jr, "_apply_analytics", None) or {}
            return result
        except PDFTextLayerError:
            # Let this propagate to orchestrator.apply_approved(), which pauses
            # the whole apply loop for self-healing on a genuinely unreadable
            # generated PDF — don't collapse it into a per-job False outcome.
            raise
        except Exception as exc:
            return self._set_apply_outcome("themuse_external_apply_error", str(exc))
