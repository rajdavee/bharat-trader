# Architecture

bharat-trader is a set of **agent skills**. Each skill is a folder with:

- `SKILL.md`: instructions an AI agent reads to know when and how to use the tool;
- one Python script: the tool itself, standard library only, runnable by hand.

The scripts never talk to each other directly. They hand off through JSON
files in a `trader/` folder inside your working directory, so every step can
be inspected, edited, or rerun on its own.

```
             ┌────────────┐   watchlist.json   ┌─────────┐
universe.txt │  scanner   │ ─────────────────▶ │  news   │
             └────────────┘                    └─────────┘
                    │                               │
                    ▼                               ▼
             ┌────────────┐    entry.json    ┌────────────┐   plan.json   ┌──────────────┐
             │   entry    │ ───────────────▶ │    exit    │ ────────────▶ │ timemachine  │
             └────────────┘                  └────────────┘               └──────────────┘
                                                   │ plan.json                   │ timemachine.json
                                                   ▼                             ▼
             ┌────────────┐   orders.json    ┌────────────┐  verdicts   ┌──────────────┐
config.json ▶│   budget   │ ───────────────▶ │    risk    │ ──────────▶ │    broker    │──▶ portfolio.json
             └────────────┘                  └────────────┘             └──────────────┘     trades log
                                                                               │
                                                   prices.json ◀──── alerts ◀──┘ (stop, target, flat-by)

  on a schedule:  news · analyst (quarterly results) · insider (bulk/block deals, SEBI PIT)
  research:       lab (backtest, walk-forward, gates) ──▶ lab/<name>/forward/<date>/plan-*.json ──▶ budget
  data:           archive (bhavcopies, VIX, intraday) ──▶ lab, timemachine, regime, cotrader, autopilot
  co-trader:      regime ─┐
                  lab ────┼─▶ cotrader (candidates, or sit out) ──▶ cotrader/plans/<ID>.json ──▶ budget
                  discover┘        │ log.jsonl ──▶ calibration ──▶ lab status (DEMOTE)
                  autopilot (paper only): cotrader ▶ budget ▶ risk ▶ broker (machine yes) ▶ lab forward record
  review:         journal (trades, rule breaks, counterfactuals) · tax (financial-year estimate)
```

## Files in `trader/`

| file | written by | read by |
| --- | --- | --- |
| `config.json` | `trader-budget init` (or you) | everything |
| `universe.txt` | scanner | scanner |
| `watchlist.json` | scanner | news, entry, insider |
| `entry.json` | entry | exit |
| `plan.json` | exit | timemachine, budget, alerts |
| `timemachine.json` | timemachine | you |
| `orders.json` | budget, strategy (`--orders`) | risk, broker |
| `prices.json` | alerts | budget, risk, broker |
| `portfolio.json` | broker | budget, risk, insider |
| `options.json` | options (`--write`) | strategy builder, UI (schema in `trader-options/SKILL.md`) |
| `lab/<name>/` | lab (`run`, `forward`): spec, results, scorecard, equity, status, forward record, draft plans | you, UI; the draft plans go to budget (`size.py --plan`) |
| `strategy.json` | strategy (`--write`, UI) | you, UI, MCP (schema in `trader-strategy/SKILL.md`) |
| `archive/` | archive (`backfill`, `update`, `intraday`, `snapshot-chain`) | lab, timemachine, regime, cotrader, autopilot, journal (or `$BT_ARCHIVE`) |
| `regime/<SYMBOL>.json` | regime (`history`) | lab, cotrader, journal, UI, MCP |
| `discover/<run>/` | discover: the declared search, every trial, the holdout log, the report | you, MCP; finalists go to `lab/<name>/` |
| `cotrader.json`, `cotrader/` | cotrader: today's candidates, a plan per candidate, the prediction log, demotions | UI, MCP, autopilot, journal (counterfactuals) |
| `autopilot/` | autopilot: state, lock, timeline, daily reports, per-strategy records, replays | UI, MCP; its trades also go to `lab/<name>/forward.json` |
| `journal.json`, `journal/` | journal (`sync`, `review`, `note`) | UI, MCP, tax |
| `tax/` | tax (`export`), and your holdings, carry-forward and profile files | you, your CA |
| `state/lab-trials.jsonl` | the MCP server's `lab_run` and `lab_run_spec` | the lab's deflated Sharpe (the family's trial count) |

Two front ends sit on top and reuse the skills rather than reimplementing
them: the local web UI (`ui/serve.py`, [ui.md](ui.md)) and the MCP server
for your own AI assistant (`mcp/server.py`, [mcp.md](mcp.md)). Both load the
skill scripts through `ui/api.py`, and both write only into `trader/`.

## The order shape

Orders are broker-neutral so adapters can map them to any Indian broker:

```json
{
  "symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 22,
  "order_type": "LIMIT", "price": 1452.35,
  "stop_loss": {"order_type": "SL-M", "trigger_price": 1441.50},
  "target":    {"order_type": "LIMIT", "price": 1474.05},
  "validity": "DAY"
}
```

Brokers no longer offer bracket orders for equity, so the stop and target are
linked legs placed after the entry fills; when one fills the other is
cancelled. Where the broker has a native linked order (Kite GTT OCO, Upstox
multi-leg GTT, Dhan Super Order), a live ticket can send the stop and target
as one such order under the same gates (see [brokers](brokers.md)).

## Safety chain

1. `trader-budget` proposes. It never places anything.
2. `trader-risk` checks the exact order against your limits: PASS or BLOCK.
3. `trader-broker` shows a plain-language ticket only for a PASS, and records
   your typed yes word for word.
4. Paper is the default broker. A live broker (Upstox, Zerodha Kite, Dhan,
   Angel One: `trader-broker/adapters/`) reads any time; it sends an order only from
   `ticket.py place`, after six gates: the config broker, `"paper": false`,
   `live_enabled` for that broker, trader-risk PASS for that exact order
   today, your recorded yes, and today's static-IP preflight. See
   [brokers](brokers.md).
5. `trader-autopilot` runs steps 1 to 3 on its own, on paper only: it
   refuses a live config before every step, its yes is recorded with
   `"actor": "autopilot"`, and `ticket.py place` refuses any yes the user
   did not type. The co-trader's RESEARCH ideas are blocked by trader-risk
   whenever the config is live. See [autopilot](autopilot.md).

## The local UI

`ui/serve.py` is a small standard-library web server on `127.0.0.1`. It does
not compute anything itself: `ui/api.py` holds one table of routes, each
calling the skill scripts (loaded from their folders) on the same `trader/`
files. Writes go through the skills' own commands (`check.py`, `ticket.py
confirm|decline|fill`, `alerts.py quote --write`), so the safety chain above is
unchanged. See [ui.md](ui.md).

## Why each folder duplicates small helpers

A user can install a single skill by copying its folder. Shared code would
break that. The IST clock, the NSE calendar, `inr()` and the charges model are
copied into the scripts that need them, and `tests/test_repo.py` fails if the
copies disagree.
