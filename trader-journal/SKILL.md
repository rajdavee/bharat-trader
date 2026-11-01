---
name: trader-journal
description: >-
  The trade journal and the counterfactual ledger for NSE trading. Builds
  one entry per closed trade (an equity position from flat to flat, or a
  whole F&O strategy such as an iron condor, rolls included, as ONE entry)
  from the paper broker's fills, your live broker's trade book and the
  tradebook CSV or JSON you download from Zerodha, Upstox, Dhan, Angel One
  or Groww, each with its persona, strategy, market regime, entry and exit,
  quantity and lots, gross P&L, charges, net P&L, R multiple, MAE/MFE from
  bars, holding time, rule notes and your own notes and tags. Statistics
  after charges: expectancy, win rate with its Wilson interval, profit
  factor, average win and loss, equity curve, drawdown and streaks, by
  strategy, persona, regime, time of day, weekday and instrument. Behaviour:
  risk overrides, trades after a circuit-breaker lock, revenge trades,
  oversizing, entries outside the window, late exits, fills with no
  ticket, the overtrading index and the cost drag. The counterfactual
  ledger follows every co-trader candidate you did not take and every
  trader-risk BLOCK of your own order to its outcome on the bars ("the
  tools saved you ₹X and cost you ₹Y", with counts and caveats). Writes
  the weekly review for the journal coach. Use when the user says "trade
  journal", "my trading journal", "weekly review", "what went wrong this
  week", "am I overtrading", "revenge trading", "my win rate", "expectancy
  by strategy", "import my Zerodha tradebook", "Upstox trade report",
  "did the risk check save me money", "what if I had taken that trade",
  "meri trading review karo", "kitna charges diya", or "journal mein note
  likho". Analytics only: it never places, sizes or confirms an order.
argument-hint: "[sync | list | show ID | stats [--by strategy|persona|regime|tod|weekday|instrument] | review --week 2026-W44 | counterfactual | import FILE --broker NAME | note ID \"...\"]"
---

# trader-journal

A journal turns a pile of fills into answers: which setups pay after
charges, which rules you break and what that costs, and whether the
guard-rails earn their keep. It describes your own past trades; a pattern
in a few trades can be chance, so every rate comes with its range and the
number of trades behind it.

`<skill dir>` is this skill's folder. Times are IST, money in rupees.

```bash
python3 "<skill dir>/journal.py" sync                         # rebuild trader/journal.json
python3 "<skill dir>/journal.py" list                         # closed trades, newest first
python3 "<skill dir>/journal.py" show J20261026-b1b26f        # fills, charges, R, MAE/MFE, rule notes
python3 "<skill dir>/journal.py" stats --by strategy          # or persona, regime, tod, weekday, instrument
python3 "<skill dir>/journal.py" review --week 2026-W44       # trader/journal/weekly-2026-W44.md
python3 "<skill dir>/journal.py" counterfactual               # the skipped and blocked plans, followed
python3 "<skill dir>/journal.py" import tradebook.csv --broker zerodha
python3 "<skill dir>/journal.py" note J20261026-b1b26f "waited for the retest" --tag followed-plan
```

`--now "2026-11-02 16:00"` pins the clock, `--bars DIR` reads bars offline
(trader-lab's `SYMBOL-5m.csv(.gz)` / `SYMBOL-1d.csv(.gz)`), `--online`
lets the bar loader fetch from Yahoo, `--json` on every command.

## Where the trades come from

| source | what |
| --- | --- |
| `trader/state/trades.json` | paper fills from trader-broker: equity, F&O basket legs, rolls, expiry settlement |
| `trader/journal/imports/<broker>.json` | fills imported with `journal.py import` (a re-import replaces, never doubles) |
| `trader/state/broker.json` | every yes: the max loss (1R), stop and target of the ticket a fill came from |
| `trader/state/shadow.json` | live orders sent by `ticket.py place`: a live fill with one of these order ids is "ticketed" |
| `trader/state/personas.json` | the persona and setup (`lab:orb15-vwap`) that drafted each order |
| `trader/state/risk.json` | the last verdicts: captured into `trader/journal/risk-log.jsonl` at every sync (risk.json is overwritten) |
| `trader/portfolio.json` | each paper F&O strategy's name (iron-condor) |
| `trader/regime/NIFTY.json` | trader-regime history; without it, NIFTY and India VIX daily bars through trader-regime |
| `trader/cotrader/log.jsonl` | the co-trader's candidates (schema `bharat-trader/cotrader-log/1`) |
| bars | trader-lab's loader: `--bars DIR`, the local archive (`trader/archive`), the lab cache, Yahoo with `--online` |

**Imports.** `import FILE --broker NAME` reads CSV or JSON in each broker's
documented layout, matching column names case- and punctuation-blind:

| broker | file | columns used |
| --- | --- | --- |
| zerodha | Console > Reports > Tradebook (CSV) | symbol, trade_date, exchange, segment, trade_type, quantity, price, trade_id, order_id, order_execution_time |
| kite | Kite Connect trades (JSON) | tradingsymbol, transaction_type, quantity, average_price, fill_timestamp, trade_id, order_id |
| upstox | API v2 historical trades or the web Trade report | symbol, trade_date, transaction_type, quantity, price, segment, expiry, strike_price, option_type, trade_id |
| dhan | DhanHQ trade history (CSV or JSON) | tradingSymbol, transactionType, tradedQuantity, tradedPrice, exchangeSegment, productType, exchangeTime, and its charges: sebiTax, stt, brokerageCharges, serviceTax, exchangeTransactionCharges, stampDuty |
| angel | SmartAPI trade book | tradingsymbol, transactiontype, fillsize, fillprice, filltime (a time only: pass `--date`), orderid, fillid |
| groww | Trade API trade list (JSON) | trading_symbol, transaction_type, quantity, price, trade_date_time (UTC), trade_status (non-executed rows are skipped) |

The broker is detected from the columns when `--broker` is left out.
trader-broker's own `ticket.py orders --broker NAME --json` output imports
the same way. Charges a file does not carry are estimated with
trader-broker's charge model for that broker and marked as estimates. A
file with dates but no times (Upstox's API) gets no time-of-day bucket and
no window checks.

## What an entry holds

Symbol(s), strategy and setup, persona, regime (NIFTY's trend and
volatility bucket as of the close BEFORE the entry, so an intraday trade
is never labelled with its own day; and the day's expiry flag), entry and
exit times and average prices, quantity and lots, gross P&L, charges, net
P&L, R multiple (net over the max loss recorded with your yes), MAE and
MFE (worst and best excursion between the fills, from 5-minute bars for
an intraday trade, daily bars otherwise; none for options), holding time,
time-of-day bucket, weekday, instrument class, rule notes, your notes and
tags. IDs (`J20261026-b1b26f`) are stable across syncs, so notes stay put.

## Statistics and behaviour

`stats` gives expectancy (with an approximate 95% range), win rate with
its Wilson 95% interval, profit factor, average win and loss, the equity
curve after charges, the deepest drawdown and the longest streaks; `--by`
splits them. Under 20 trades it says so.

Rule notes, each with its time: **override** (placed although trader-risk
warned on a behaviour rule, e.g. `"revenge_trade": "warn"`), **after-
breaker** (opened after `max_consecutive_losses` losses in a row or the
daily drawdown limit that day), **revenge** (the same stock within
`revenge_window_min` of a loss on it), **oversized** (max loss over
`max_loss_per_trade`, or with no stop on record a position over
`max_order_value`), **outside-window** (before `no_entries_before` or after
`no_entries_after`), **late-exit** (an MIS position closed after
`flat_by`), **off-ticket** (a fill trader-risk never saw), **overtrading**
(entries past `max_trades_per_day`). The overtrading index is entries a
traded day over the plan (1.00 = the plan); the cost drag is charges as a
share of the gross profit.

## The counterfactual ledger

Every co-trader candidate you did not take (status ready: skipped; status
blocked: blocked by risk) and every trader-risk BLOCK of your own entry is
replayed with trader-lab's no-look-ahead rules: in at the first bar
starting at or after the decision, at its open plus slippage (skipped if
it opens through the stop or the target), stop first inside a bar, a gap
through either fills at the open, out at the flat-by (5-minute plans) or
after the maximum holding at the next open (daily plans), net of
trader-lab's charges on the plan's own quantity. A plan you took anyway
that day counts as taken, an F&O basket as not replayable, a plan with no
bars or still open as unresolved. The result: "not taking them saved you
₹X (n would have lost) and cost you ₹Y (m would have won)", per group,
with the caveats printed every time.

## The weekly review

`review --week 2026-W44` (or `this`, `last`) writes
`trader/journal/weekly-2026-W44.md`: the numbers with their ranges, by
persona, strategy and regime, every rule break with its time, overtrading
and costs, the week's counterfactual ledger, a note for the journal coach
(process only under 20 trades; one rule-shaped change a week) and what the
journal does not show. The journal-coach persona (`agents/journal-coach.md`)
reads it.

From an AI assistant, the MCP server offers `journal_summary`,
`journal_stats` and `counterfactual_report` (read only) and the resources
`trader/journal.json` and `trader/journal/weekly-*.md`. The UI's Journal
page shows the same numbers. See `docs/journal.md`.

## Limits

Paper fills are simulated; a counterfactual replay is a hypothetical fill
on bars, not an order book. MAE/MFE needs bars covering the trade. Lots
are shown only when a lot size is on record. Persona and strategy come
from the tag on the drafted order: untagged fills show as unattributed.

Analytics only: it reads records and writes under `trader/journal.json`
and `trader/journal/`. It never places, sizes or confirms an order.
Education and tooling, not financial advice.
