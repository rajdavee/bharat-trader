# bharat-trader MCP server

Connect your own MCP-compatible AI assistant to bharat-trader. Standard
library only, stdio by default, optional streamable HTTP on `127.0.0.1`.
Paper trading only: there is no live-order tool.

```bash
python3 mcp/server.py --workdir ~/trading                   # stdio: what an MCP client launches
python3 mcp/server.py --offline tests/fixtures/mcp          # saved real data, no network
python3 mcp/server.py --workdir ~/trading --http 8765       # http://127.0.0.1:8765/mcp
python3 mcp/server.py --tool-reference                      # the tool table, from the schemas
```

Client configuration (the usual command + args shape):

```json
{
  "mcpServers": {
    "bharat-trader": {
      "command": "python3",
      "args": ["/path/to/bharat-trader/mcp/server.py", "--workdir", "/path/to/trading-folder"]
    }
  }
}
```

| file | what it is |
| --- | --- |
| `server.py` | JSON-RPC 2.0 over stdio and streamable HTTP; MCP revision 2026-07-28 plus the handshake-based 2025-11-25, 2025-06-18 and 2025-03-26; lifecycle, tools, resources, prompts, pagination, cancellation, progress, logging, elicitation, rate limit |
| `toolkit.py` | the 21 tools, the `trader/` resources and the 5 prompts. Each tool calls a skill's own function, or its own command line with `--json`, through `ui/api.py`'s loader: one implementation of every number |

Tools: `market_status`, `scan_daily`, `scan_orb`, `news`, `entry_check`,
`exit_plan`, `timemachine_run`, `size_trade`, `risk_check`,
`paper_orders_show`, `paper_order_draft`, `paper_order_confirm`,
`portfolio_status`, `options_chain`, `options_summary`, `results_analyse`,
`insider_deals`, `insider_disclosures`, `config_get`.

Prompts: `morning_briefing`, `intraday_plan`, `options_income_scan`,
`weekly_review`, `research_strategy`.

Safety in one paragraph: `paper_order_confirm` needs trader-risk PASS
recorded today for that exact order (its fingerprint) and the user's own
typed yes, asked through elicitation when the client supports it; every write
lands in `<workdir>/trader/`; tool calls are rate limited; stdout carries only
JSON-RPC; every result says "Education and tooling, not financial advice."

The full guide (install, every tool, the security model, the protocol
details, good questions to ask): [docs/mcp.md](../docs/mcp.md).
