from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .policy_agent import PolicyAgent


@dataclass(frozen=True)
class ResearchResult:
    requester: str
    query: str
    allowed_domains: list[str]
    summary: str
    sources: list[dict[str, str]]
    approved_by_policy: bool
    policy_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requester": self.requester,
            "query": self.query,
            "allowed_domains": self.allowed_domains,
            "summary": self.summary,
            "sources": self.sources,
            "approved_by_policy": self.approved_by_policy,
            "policy_reason": self.policy_reason,
        }


class SearchProvider(Protocol):
    async def search(self, query: str, *, domains: list[str], max_results: int) -> list[dict[str, str]]:
        ...


class DuckDuckGoHtmlSearchProvider:
    """Small dependency-free search provider used by the MCP tool.

    It is deliberately scoped by site: filters from PolicyAgent. Tests inject a
    fake provider, so unit tests never touch the network.
    """

    endpoint = "https://duckduckgo.com/html/"

    async def search(self, query: str, *, domains: list[str], max_results: int) -> list[dict[str, str]]:
        scoped_query = query
        if domains:
            scoped_query = f"{query} " + " OR ".join(f"site:{domain}" for domain in domains)

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(self.endpoint, params={"q": scoped_query})
            response.raise_for_status()

        return self._parse_results(response.text, max_results=max_results)

    def _parse_results(self, html: str, *, max_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        # DuckDuckGo lite/html result anchors use result__a. Keep parsing simple
        # because this provider is a fallback capability, not a browser scraper.
        pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
        for url, title_html in pattern.findall(html):
            title = re.sub(r"<[^>]+>", "", title_html)
            title = re.sub(r"\s+", " ", title).strip()
            results.append({"title": title, "url": url, "snippet": ""})
            if len(results) >= max_results:
                break
        return results


class WebResearchAgent:
    """Policy-gated capability service for web research.

    Callers should not use this as a free-form coordinator. They submit a query,
    PolicyAgent authorizes scope, this service researches only inside that scope,
    then PolicyAgent gets a final validation pass before results are returned.
    """

    def __init__(
        self,
        policy_agent: PolicyAgent | None = None,
        search_provider: SearchProvider | None = None,
    ):
        self.policy_agent = policy_agent or PolicyAgent()
        self.search_provider = search_provider or DuckDuckGoHtmlSearchProvider()

    async def research(
        self,
        *,
        requester: str,
        query: str,
        domains: list[str] | None = None,
        purpose: str = "repair",
    ) -> ResearchResult:
        decision = self.policy_agent.authorize_web_research(
            requester=requester,
            query=query,
            domains=domains,
            purpose=purpose,
        )
        if not decision.allowed:
            return ResearchResult(
                requester=requester,
                query=query,
                allowed_domains=[],
                summary="",
                sources=[],
                approved_by_policy=False,
                policy_reason=decision.reason,
            )

        scope = decision.scope
        sources = await self.search_provider.search(
            scope["query"],
            domains=list(scope["allowed_domains"]),
            max_results=int(scope["max_results"]),
        )
        summary = self._summarize_sources(scope["query"], sources)
        result_dict = {"summary": summary, "sources": sources}
        validation = self.policy_agent.validate_web_research_result(result_dict)
        if validation.scope.get("summary"):
            summary = validation.scope["summary"]

        return ResearchResult(
            requester=scope["requester"],
            query=scope["query"],
            allowed_domains=list(scope["allowed_domains"]),
            summary=summary,
            sources=sources,
            approved_by_policy=validation.allowed,
            policy_reason=validation.reason,
        )

    def _summarize_sources(self, query: str, sources: list[dict[str, str]]) -> str:
        if not sources:
            return f"No approved web results found for: {query}"
        lines = [f"Approved web research for: {query}"]
        for idx, source in enumerate(sources, start=1):
            title = source.get("title") or "Untitled"
            url = source.get("url") or ""
            snippet = source.get("snippet") or ""
            lines.append(f"{idx}. {title} — {url}" + (f" — {snippet}" if snippet else ""))
        return "\n".join(lines)
