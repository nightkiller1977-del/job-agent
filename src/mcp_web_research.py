from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .policy_agent import PolicyAgent
from .web_research_agent import WebResearchAgent


SERVER_INFO = {"name": "job-agent-web-research", "version": "0.1.0"}
TOOL_NAME = "web_research.query"


def _jsonrpc_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


class WebResearchMcpServer:
    """Minimal stdio MCP-compatible JSON-RPC server.

    The server exposes one capability: web_research.query. Requests are routed
    through PolicyAgent before WebResearchAgent performs any network research.
    """

    def __init__(self, agent: WebResearchAgent | None = None):
        self.agent = agent or WebResearchAgent(PolicyAgent())

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return _jsonrpc_response(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {}},
                },
            )

        if method == "tools/list":
            return _jsonrpc_response(
                request_id,
                {
                    "tools": [
                        {
                            "name": TOOL_NAME,
                            "description": "Policy-gated web research for repair and diagnostic agents.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "requester": {"type": "string"},
                                    "query": {"type": "string"},
                                    "domains": {"type": "array", "items": {"type": "string"}},
                                    "purpose": {"type": "string"},
                                },
                                "required": ["requester", "query"],
                            },
                        }
                    ]
                },
            )

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != TOOL_NAME:
                return _jsonrpc_response(request_id, error={"code": -32602, "message": f"unknown tool: {name}"})
            result = await self.agent.research(
                requester=str(arguments.get("requester") or "unknown"),
                query=str(arguments.get("query") or ""),
                domains=arguments.get("domains") or None,
                purpose=str(arguments.get("purpose") or "repair"),
            )
            return _jsonrpc_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result.to_dict(), indent=2),
                        }
                    ],
                    "isError": not result.approved_by_policy,
                },
            )

        # Notifications like initialized have no id; do not send a response.
        if request_id is None:
            return None
        return _jsonrpc_response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


async def run_stdio() -> None:
    server = WebResearchMcpServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = await server.handle(request)
        except Exception as exc:  # defensive: keep server alive for caller
            response = _jsonrpc_response(None, error={"code": -32000, "message": str(exc)})
        if response is not None:
            print(json.dumps(response), flush=True)


def main() -> None:
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
