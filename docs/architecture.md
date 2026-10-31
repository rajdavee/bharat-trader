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
| `orders.json` | budget | risk, broker |
| `prices.json` | alerts | budget, risk, broker |
| `portfolio.json` | broker | budget, risk, insider |

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
cancelled.

## Safety chain

1. `trader-budget` proposes. It never places anything.
2. `trader-risk` checks the exact order against your limits: PASS or BLOCK.
3. `trader-broker` shows a plain-language ticket only for a PASS, and records
   your typed yes word for word.
4. In v0.1 the broker is paper only.

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
