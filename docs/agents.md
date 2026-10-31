# Agent team

`agents/` holds six personas: plain-markdown instructions for an agent that
runs the bharat-trader skills like a disciplined desk. They add no new
trading logic. Every number still comes from a skill, every order is a paper
proposal, and every ticket still needs trader-risk PASS and the user's own
typed yes. Education and tooling, not financial advice.

## Who does what

```
researcher ──verdicts──▶ intraday-trader ─┐
   ▲  (trader-lab)       swing-trader     ├─tagged paper drafts──▶ risk-officer ──PASS / VETO──▶ you (typed yes)
   │                     options-seller  ─┘   (--persona NAME)      (trader-risk,                  │
   │                                                                 scenarios, breakers,          ▼
   └──────── hypotheses ◀──── journal-coach ◀──── trader/state/trades.json, broker.json ◀── paper fills
```

| persona | mandate | may draft | hard stops |
| --- | --- | --- | --- |
| intraday-trader | ORB, VWAP reclaim and momentum rules from the lab library, MIS | paper | WATCH or PROMOTE verdicts only; two losses in a row ends its day; flat by `flat_by` |
| options-seller | iron condor, iron fly, credit spreads on NIFTY and BANKNIFTY at IV rank 50 or more | paper | defined risk only; max loss and margin limits; no new positions on expiry day, out by 14:30 |
| swing-trader | daily lab rules held in delivery (CNC), filings as reasons to skip | paper | WATCH or PROMOTE only; no entry 5 sessions before results; at most 5 open |
| researcher | hypotheses with a kill condition, lab specs, after-charges scorecards | never | gates written before the run; no verdict talk under 30 out-of-sample trades |
| risk-officer | trader-risk on every ticket, the scenario grid, behavioural breakers, the kill switch | never | may veto a PASS, never pass a BLOCK; the kill switch only with the user's agreement |
| journal-coach | weekly review from the paper logs | never | one process change a week; under 20 trades, process only |

The traders trade only rules the Strategy Lab has evidence for (see
[lab.md](lab.md)). A REJECT is reported as loudly as a WATCH. No persona
projects a return or calls a backtest a forecast.

## The persona file

```markdown
---
name: options-seller
description: NSE index options seller for bharat-trader. ... Paper only, never a live order.
tools: Read, Grep, Glob, Bash
skills: [trader-alerts, trader-options, trader-strategy, trader-news, trader-risk, trader-broker]
mcp-tools: [market_status, options_summary, options_chain, strategy_build, ...]
metadata:
  title: Options seller
  role: trader
  may-draft: paper
  version: "0.2"
---

# Options seller
## Mandate
## Daily routine (IST)    each step: the command line, then [the MCP tool]
## Hard limits
## Hand-offs
## Never
## How you report
```

The frontmatter is plain YAML (no anchors, no multi-line values), so any
framework's parser reads it. `tools` names generic file and shell tools;
rename them to your framework's if it differs. `tests/test_agents.py` parses
every file and checks every skill, MCP tool, command, subcommand, flag, lab
spec, strategy template and config key it names against the repo.

## Running them

**Sub-agent definitions.** Put the files where your framework looks for
agent definitions and keep the skills installed next to them; the command
lines are relative to the bharat-trader folder. Ask for the persona by name
or let the framework pick it from the description ("aaj ka intraday plan",
"iron condor on Nifty", "meri trading review karo").

**MCP.** The server publishes a prompt per persona:

| prompt | persona |
| --- | --- |
| `persona_intraday_trader` | intraday-trader |
| `persona_journal_coach` | journal-coach |
| `persona_options_seller` | options-seller |
| `persona_researcher` | researcher |
| `persona_risk_officer` | risk-officer |
| `persona_swing_trader` | swing-trader |

Each takes an optional `focus` ("RELIANCE", "Thursday's expiry") and returns
the persona's instructions followed by the current context, read from your
`trader/` folder without network: the time and NSE session, your limits, the
Strategy Lab verdicts, and the persona's own paper scorecard with its latest
flags. The assistant then follows the routine with the tools in brackets.

## The track record

**Tagging.** `--persona NAME` on `trader-budget/size.py` (plan and weights
modes) and on `trader-strategy/strategy.py build ... --orders`, or the
`persona` argument of the MCP tools `paper_order_draft` and `lab_forward`,
writes `"persona": NAME` into each order's `plan` note. The note is never
sent to a broker and is not part of the order fingerprint trader-risk and
trader-broker match on, so tagging changes no size, check or verdict. Each
tagged proposal is also appended to `trader/state/personas.json` (the last
2,000), because `trader/orders.json` is replaced by the next proposal.

**Scorecards.** `persona_scorecards` (MCP), `GET /api/agents` and the UI's
Agents page rebuild every idea from `trader/state/trades.json`, read only:

- a fill whose order fingerprint a persona tagged opens (or adds to) that
  persona's idea; a fill that reduces a position closes the oldest lots
  first, the same idea first, so a stop, target or flatten ticket is
  credited to the idea it closes;
- P&L per idea: sells minus buys minus every charge of both sides, once the
  position is flat;
- per persona: closed trades, wins, losses, win rate, net P&L after charges,
  charges paid, average win and loss, profit factor, expectancy, max
  drawdown, the current and longest losing streak, most entries in a day,
  tickets drafted, confirmed and declined, and the lab rules traded;
- flags: **overtrading** (more entries in a day than `max_trades_per_day`),
  **revenge** (a new entry within 30 minutes of a losing exit the same day),
  **late-exit** (an MIS idea closed after `flat_by`), **off-ticket** (closed
  by a fill with no ticket).

Fills nobody tagged show as unattributed. Under 20 closed trades the
scorecard says it is far too few to tell skill from luck, and even a long
paper record is simulated fills, not a forecast.

## The offline demo

`tests/test_agents.py` runs the intraday trader's routine end to end on
saved real data through the MCP server, with nothing placed:

```bash
python3 -m unittest tests.test_agents.Mcp.test_offline_flow_scan_plan_lab_risk_paper -v
```

scan (`scan_orb`) → lab check (`lab_run`, `lab_report`: a WATCH rule) →
plan and tagged paper draft (`lab_forward` with `draft` and `persona`) →
risk (`risk_check` with `record`) → the ticket (`paper_orders_show`) →
the persona's scorecard (`persona_scorecards`: one draft, nothing placed).
The same by hand, against the saved answers:

```bash
python3 mcp/server.py --offline "$PWD/tests/fixtures/mcp" --workdir /tmp/bt-demo
```

then ask your assistant for the `persona_intraday_trader` prompt.

## Limits

- Personas are instructions, not enforcement. The enforcement is
  trader-risk, the paper broker's typed-yes check and the absence of any
  live-order tool; a persona that ignored its file would still hit those.
- The risk officer's extra breakers (two losses in a row for a persona, the
  revenge flag) are judgement on top of trader-risk's own BLOCKs, read from
  the scorecard.
- Option structures are drafted, checked and filled on paper as one basket
  (one ticket, one yes, every leg or none); `ticket.py mtm` marks them and
  `ticket.py settle` settles them at expiry. See [options.md](options.md).
