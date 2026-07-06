# Web Research Agent MCP

The Web Research Agent is exposed as a stdio MCP server. It must remain behind `PolicyAgent`; callers should not bypass policy and call the search provider directly.

Example local MCP server entry:

```json
{
  "mcpServers": {
    "job-agent-web-research": {
      "command": "python",
      "args": ["-m", "src.mcp_web_research"]
    }
  }
}
```

Exposed tool:

- `web_research.query`

Required arguments:

- `requester`: the calling agent name, such as `repair-agent`
- `query`: the repair or diagnostic research question

Optional arguments:

- `domains`: allowed research domains requested by the caller. Policy may deny the request if the domain is outside scope.
- `purpose`: defaults to `repair`

Recommended flow:

1. Repair or diagnostic agent requests research.
2. `PolicyAgent` authorizes the web scope.
3. `WebResearchAgent` searches only inside that scope.
4. `PolicyAgent` validates/redacts the result.
5. The calling agent receives the approved result.
