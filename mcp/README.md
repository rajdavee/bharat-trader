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
| `toolkit.py` | the 47 tools (including the co-trader workflow layer: `trade_brief`, `best_for_regime`, `strategy_design`, `lab_run_spec` with the trial ledger, `compare_strategies`, `archive_status`), the `trader/` resources and the 16 prompts (5 workflows, 5 co-trader prompts, 6 agent personas). Each tool calls a skill's own function, or its own command line with `--json`, through `ui/api.py`'s loader: one implementation of every number |

Tools: `market_status`, `scan_daily`, `scan_orb`, `news`, `entry_check`,
`exit_plan`, `timemachine_run`, `size_trade`, `risk_check`, `risk_scenarios`,
`paper_orders_show`, `paper_order_draft`, `paper_order_confirm`,
`portfolio_status`, `paper_mtm`, `options_chain`, `options_summary`, `results_analyse`,
`insider_deals`, `insider_disclosures`, `config_get`, `lab_list`, `lab_run`,
`lab_report`, `lab_forward`, `strategy_build`, `strategy_price`,
`broker_status`, `broker_holdings`, `broker_positions`, `doctor`,
`persona_scorecards`, `regime_now`, `journal_summary`, `journal_stats`,
`counterfactual_report`, `cotrader_candidates`, `cotrader_explain`,
`calibration_report`.

Prompts: `morning_briefing`, `intraday_plan`, `options_income_scan`,
`weekly_review`, `research_strategy`, and one per agent persona
(`persona_intraday_trader`, `persona_options_seller`, `persona_swing_trader`,
`persona_researcher`, `persona_risk_officer`, `persona_journal_coach`: the
persona's instructions plus your current context; see
[docs/agents.md](../docs/agents.md)).

Safety in one paragraph: `paper_order_confirm` needs trader-risk PASS
recorded today for that exact order (its fingerprint) and the user's own
typed yes, asked through elicitation when the client supports it; every write
lands in `<workdir>/trader/`; tool calls are rate limited; stdout carries only
JSON-RPC; every result says "Education and tooling, not financial advice."

The full guide (install, every tool, the security model, the protocol
details, good questions to ask): [docs/mcp.md](../docs/mcp.md).
