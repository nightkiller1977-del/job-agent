from __future__ import annotations

import json

import pytest

from src.mcp_web_research import TOOL_NAME, WebResearchMcpServer
from src.policy_agent import PolicyAgent
from src.web_research_agent import WebResearchAgent


class FakeSearchProvider:
    def __init__(self):
        self.calls = []

    async def search(self, query: str, *, domains: list[str], max_results: int):
        self.calls.append({"query": query, "domains": domains, "max_results": max_results})
        return [
            {
                "title": "Playwright auth guide",
                "url": "https://playwright.dev/python/docs/auth",
                "snippet": "Use isolated browser contexts and stored auth state.",
            }
        ]


class TestPolicyAgent:
    def test_authorizes_scoped_repair_research(self):
        policy = PolicyAgent({"policy": {"web_research": {"allowed_domains": ["playwright.dev"]}}})

        decision = policy.authorize_web_research(
            requester="repair-agent",
            query="Playwright auth state expired session repair",
            domains=["playwright.dev"],
        )

        assert decision.allowed is True
        assert decision.scope["requester"] == "repair-agent"
        assert decision.scope["allowed_domains"] == ["playwright.dev"]

    def test_denies_secret_like_queries(self):
        policy = PolicyAgent()

        decision = policy.authorize_web_research(
            requester="repair-agent",
            query="debug ANTHROPIC_API_KEY=abc123",
        )

        assert decision.allowed is False
        assert "secrets" in decision.reason

    def test_denies_domains_outside_scope(self):
        policy = PolicyAgent({"policy": {"web_research": {"allowed_domains": ["docs.github.com"]}}})

        decision = policy.authorize_web_research(
            requester="repair-agent",
            query="pytest monkeypatch example",
            domains=["example.com"],
        )

        assert decision.allowed is False
        assert "outside allowed scope" in decision.reason

    def test_denies_explicitly_blocked_domain(self):
        policy = PolicyAgent()

        decision = policy.authorize_web_research(
            requester="repair-agent",
            query="look up pasted stack trace",
            domains=["pastebin.com"],
        )

        assert decision.allowed is False
        assert "denied domain" in decision.reason


@pytest.mark.asyncio
async def test_web_research_routes_through_policy_before_provider_call():
    provider = FakeSearchProvider()
    policy = PolicyAgent({"policy": {"web_research": {"allowed_domains": ["playwright.dev"], "max_results": 3}}})
    agent = WebResearchAgent(policy_agent=policy, search_provider=provider)

    result = await agent.research(
        requester="repair-agent",
        query="Playwright auth state expired session repair",
        domains=["playwright.dev"],
    )

    assert result.approved_by_policy is True
    assert result.allowed_domains == ["playwright.dev"]
    assert provider.calls == [
        {
            "query": "Playwright auth state expired session repair",
            "domains": ["playwright.dev"],
            "max_results": 3,
        }
    ]
    assert "Playwright auth guide" in result.summary


@pytest.mark.asyncio
async def test_web_research_does_not_call_provider_when_policy_denies():
    provider = FakeSearchProvider()
    agent = WebResearchAgent(policy_agent=PolicyAgent(), search_provider=provider)

    result = await agent.research(
        requester="repair-agent",
        query="debug password=supersecret",
    )

    assert result.approved_by_policy is False
    assert result.policy_reason == "query appears to contain secrets"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_mcp_lists_web_research_tool():
    server = WebResearchMcpServer(agent=WebResearchAgent(search_provider=FakeSearchProvider()))

    response = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    tool_names = [tool["name"] for tool in response["result"]["tools"]]
    assert TOOL_NAME in tool_names


@pytest.mark.asyncio
async def test_mcp_tool_call_returns_policy_gated_result():
    policy = PolicyAgent({"policy": {"web_research": {"allowed_domains": ["playwright.dev"]}}})
    agent = WebResearchAgent(policy_agent=policy, search_provider=FakeSearchProvider())
    server = WebResearchMcpServer(agent=agent)

    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {
                    "requester": "repair-agent",
                    "query": "Playwright auth state expired session repair",
                    "domains": ["playwright.dev"],
                },
            },
        }
    )

    assert response["result"]["isError"] is False
    body = json.loads(response["result"]["content"][0]["text"])
    assert body["approved_by_policy"] is True
    assert body["allowed_domains"] == ["playwright.dev"]
    assert body["sources"][0]["url"].startswith("https://playwright.dev")
