from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class PolicyDecision:
    """Authorization result returned by PolicyAgent."""

    allowed: bool
    reason: str
    scope: dict[str, Any] = field(default_factory=dict)


class PolicyAgent:
    """Central authorization gate for risky agent capabilities.

    Web research is intentionally treated as a risky capability because it can
    retrieve untrusted prompt-injection content from the public internet. Other
    agents should ask this policy gate before invoking WebResearchAgent, and the
    returned scope is the only scope the research agent may use.
    """

    DEFAULT_ALLOWED_DOMAINS = {
        "docs.github.com",
        "github.com",
        "raw.githubusercontent.com",
        "pypi.org",
        "python.org",
        "playwright.dev",
        "modelcontextprotocol.io",
    }

    DEFAULT_DENIED_DOMAINS = {
        "pastebin.com",
        "hastebin.com",
        "0bin.net",
    }

    SECRET_MARKERS = (
        "api_key",
        "apikey",
        "anthropic_api_key",
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "ssh-rsa",
        "BEGIN PRIVATE KEY",
    )

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        policy_config = self.config.get("policy", {})
        web_config = policy_config.get("web_research", {})

        self.allowed_domains = set(web_config.get("allowed_domains") or self.DEFAULT_ALLOWED_DOMAINS)
        self.denied_domains = set(web_config.get("denied_domains") or self.DEFAULT_DENIED_DOMAINS)
        self.default_max_results = int(web_config.get("max_results", 5))
        self.max_query_chars = int(web_config.get("max_query_chars", 500))

    def authorize_web_research(
        self,
        *,
        requester: str,
        query: str,
        domains: list[str] | None = None,
        purpose: str = "repair",
    ) -> PolicyDecision:
        query = (query or "").strip()
        requester = (requester or "unknown").strip()
        requested_domains = [self._normalize_domain(d) for d in (domains or []) if d]

        if not query:
            return PolicyDecision(False, "empty query")

        if len(query) > self.max_query_chars:
            return PolicyDecision(False, "query too long")

        if self._contains_secret(query):
            return PolicyDecision(False, "query appears to contain secrets")

        denied = sorted(d for d in requested_domains if self._domain_denied(d))
        if denied:
            return PolicyDecision(False, f"denied domain requested: {', '.join(denied)}")

        if requested_domains:
            not_allowed = sorted(d for d in requested_domains if not self._domain_allowed(d))
            if not_allowed:
                return PolicyDecision(False, f"domain outside allowed scope: {', '.join(not_allowed)}")
            scoped_domains = requested_domains
        else:
            scoped_domains = sorted(self.allowed_domains)

        return PolicyDecision(
            True,
            "approved",
            {
                "requester": requester,
                "purpose": purpose,
                "query": query,
                "allowed_domains": scoped_domains,
                "max_results": self.default_max_results,
            },
        )

    def validate_web_research_result(self, result: dict[str, Any]) -> PolicyDecision:
        """Validate and redact the result before another agent can consume it."""
        text = str(result.get("summary") or "")
        if self._contains_secret(text):
            redacted = self._redact_secret_markers(text)
            return PolicyDecision(True, "approved with redaction", {"summary": redacted})
        return PolicyDecision(True, "approved", {})

    def _contains_secret(self, value: str) -> bool:
        lowered = value.lower()
        return any(marker.lower() in lowered for marker in self.SECRET_MARKERS)

    def _redact_secret_markers(self, value: str) -> str:
        redacted = value
        for marker in self.SECRET_MARKERS:
            redacted = redacted.replace(marker, "[REDACTED]")
            redacted = redacted.replace(marker.upper(), "[REDACTED]")
        return redacted

    def _normalize_domain(self, domain_or_url: str) -> str:
        raw = domain_or_url.strip().lower()
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.netloc or parsed.path
        return host.split("@")[ -1 ].split(":")[0].strip("/")

    def _domain_allowed(self, domain: str) -> bool:
        return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in self.allowed_domains)

    def _domain_denied(self, domain: str) -> bool:
        return any(domain == denied or domain.endswith(f".{denied}") for denied in self.denied_domains)
