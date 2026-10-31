# The agent team

Six personas that run the bharat-trader skills as a disciplined team. Each file
is one persona: a short frontmatter block plus plain-markdown instructions
(mandate, routine with the exact commands and MCP tools, hard limits,
hand-offs, a never-do list and a report format).

| persona | role | trades? |
| --- | --- | --- |
| [intraday-trader](intraday-trader.md) | ORB, VWAP and momentum on liquid NSE stocks, only Strategy Lab rules with a WATCH or PROMOTE verdict, flat by `flat_by` | paper drafts |
| [options-seller](options-seller.md) | defined-risk credit structures on NIFTY and BANKNIFTY when IV rank is high, with margin, Greeks and expiry-day discipline | paper drafts |
| [swing-trader](swing-trader.md) | daily-bar lab rules held in delivery, with results, deals and insider filings as reasons to skip | paper drafts |
| [researcher](researcher.md) | hypotheses into trader-lab specs, honest after-charges scorecards | never |
| [risk-officer](risk-officer.md) | trader-risk on every ticket, the scenario grid, breakers, the kill switch; the final veto | never (blocks only) |
| [journal-coach](journal-coach.md) | the weekly review from the paper logs: rule breaks, overtrading, revenge trades | never |

No persona can place a live order. Every ticket still needs trader-risk PASS,
the risk officer, and the user's own typed yes for that one ticket. Education
and tooling, not financial advice: no persona promises a profit.

## Use them

**With an agent framework that supports sub-agents or skills.** Copy (or
symlink) the files into the folder your framework reads agent definitions
from, next to the skills. The frontmatter keys are the common ones:

```yaml
name: intraday-trader          # the file name, lower case
description: ...               # when to use it, with Indian trigger phrases
tools: Read, Grep, Glob, Bash  # the file and shell tools it needs (map to your framework's names)
skills: [trader-scanner, ...]  # the bharat-trader skills it runs
mcp-tools: [scan_orb, ...]     # the same steps as MCP tools
metadata: {title, role, may-draft, ...}
```

Frameworks that only read `name` and `description` ignore the rest. The
body is the system prompt.

**Over MCP.** `mcp/server.py` publishes one prompt per persona,
`persona_intraday_trader`, `persona_options_seller`, ... Each returns the
persona's instructions plus the current context from your `trader/` folder
(the NSE session, your limits, the lab verdicts, the persona's paper
scorecard), with an optional `focus` argument. The read-only tool
`persona_scorecards` returns every persona's paper track record.

## The track record

Tag a proposal with the persona that drafted it:

```bash
python3 trader-budget/size.py --write --persona intraday-trader
python3 trader-strategy/strategy.py build iron-condor NIFTY --orders --persona options-seller
```

or pass `persona` to the MCP tools `paper_order_draft` and `lab_forward`.
The tag is a note in the order's `plan` (never sent, not part of the
trader-risk fingerprint) and is logged in `trader/state/personas.json`. The
scorecards rebuild each persona's ideas from `trader/state/trades.json`:
closed trades, net P&L after every charge, profit factor, expectancy,
drawdown, losing streaks and behaviour flags. See them on the UI's Agents
page (`g t`), or with the MCP tool. A short paper record says almost nothing
about skill; compare it with the rule's lab scorecard.

More: [docs/agents.md](../docs/agents.md).
