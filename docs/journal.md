# The trade journal and the counterfactual ledger

`trader-journal` turns fills into a record you can learn from: one entry
per closed trade, the numbers after every charge with the range each one
still allows, the rules you broke and what that cost, and what the trades
you did *not* take would have done. It reads; it never places, sizes or
confirms an order.

```bash
python3 trader-journal/journal.py sync                       # rebuild trader/journal.json
python3 trader-journal/journal.py list
python3 trader-journal/journal.py show J20261026-b1b26f
python3 trader-journal/journal.py stats --by regime
python3 trader-journal/journal.py review --week 2026-W44
python3 trader-journal/journal.py counterfactual
python3 trader-journal/journal.py import tradebook.csv --broker zerodha
python3 trader-journal/journal.py note J20261026-b1b26f "moved my stop" --tag broke-plan
```

## Why a journal, and why ranges

SEBI's studies keep finding that most individual traders lose money, and
that the ones who trade most lose most. A journal is the cheapest defence:
it shows which setups pay *after charges* and which habits cost money. But
a journal of 14 trades is a tiny sample. Two wins in 14 is a 14% win rate
whose 95% Wilson interval runs from 4% to 40%: the trades so far cannot tell
a 10% strategy from a 35% one. So every rate here comes with its interval
and its count, and under 20 trades the journal says it is far too few to
judge.

- **Win rate** with the Wilson score interval (it behaves at 0 and 100%,
  unlike the textbook ±1.96·√(p(1−p)/n)).
- **Expectancy** (net P&L a trade after charges) with an approximate 95%
  range, mean ± 1.96·sd/√n, and the average **R multiple** (net over the
  max loss recorded with your yes) where one is on record.
- **Profit factor**, average win and loss, the **equity curve after
  charges**, the deepest **drawdown** and the longest win and loss
  **streaks**.
- **Breakdowns** by strategy, persona, regime, time of day, weekday and
  instrument.

## Entries

An equity entry runs from flat to flat in one symbol (scale-ins and partial
exits included; a sell that flips a long to a short is split). An F&O entry
is a whole paper strategy, every leg, roll and settlement with the same
strategy id, so an iron condor that was opened, rolled and closed is one
entry with one net P&L, equal to the paper broker's own realised figure for
that strategy. Imported F&O fills (no strategy id) group by underlying from
flat to flat.

Each entry carries: symbols, strategy and setup (from the persona tag on
the drafted order, `trader/state/personas.json`, or the paper strategy's
name), persona, regime, entry and exit times and prices, quantity and lots,
gross P&L, charges (estimated and marked so when a broker file has none),
net P&L, R multiple, MAE and MFE, holding time, time-of-day bucket,
weekday, instrument class, whether it was ticketed (passed through
trader-risk), rule notes, and your own notes and tags
(`trader/journal/notes.json`). Entry IDs are a hash of the first fill, so
they stay the same across syncs and your notes stay attached.

**Regime.** NIFTY's trend class and volatility bucket from trader-regime
(`trader/regime/NIFTY.json`, or worked out from NIFTY and India VIX daily
bars) *as of the close before the entry*: an intraday trade is never
labelled with a regime that its own day's close decided. The day's expiry
flag is kept beside it (the expiry calendar is known in advance).

**MAE and MFE.** The maximum adverse and favourable excursion between the
fills, in rupees for the whole position: from 5-minute bars for an
intraday trade, daily bars otherwise, through trader-lab's loader (`--bars
DIR`, the local archive, the lab cache; Yahoo only with `--online`). None
for options, and none when no bars cover the trade: nothing is invented.

## Sources

| file | read for |
| --- | --- |
| `trader/state/trades.json` | paper fills (equity, F&O basket legs, rolls, expiry settlement) |
| `trader/journal/imports/*.json` | imported broker fills (each keeps the broker's trade id: re-importing replaces) |
| `trader/state/broker.json` | the max loss, stop and target recorded with each yes |
| `trader/state/shadow.json` | live orders sent through `ticket.py place`, so a live fill can be matched to its ticket |
| `trader/state/personas.json` | the persona and setup that drafted each order |
| `trader/state/risk.json` | captured at every sync into `trader/journal/risk-log.jsonl` (with each checked order's plan), because risk.json holds only the last check; the UI's and the MCP server's recorded risk checks are captured too |
| `trader/portfolio.json` | paper F&O strategy names and lot sizes |
| `trader/cotrader/log.jsonl` | co-trader candidates for the ledger |
| `trader/config.json` | your limits, for the rule checks |

### Broker trade books

`import FILE --broker NAME` reads CSV or JSON in each broker's documented
layout; column names match case- and punctuation-blind, and the broker is
detected from the columns when `--broker` is left out.

| broker | download | notes |
| --- | --- | --- |
| Zerodha | Console > Reports > Tradebook, CSV: `symbol, isin, trade_date, exchange, segment, series, trade_type, auction, quantity, price, trade_id, order_id, order_execution_time` | times in IST |
| Kite | the Kite Connect trades API (`tradingsymbol, transaction_type, quantity, average_price, fill_timestamp`) | |
| Upstox | API v2 historical trades or the web Trade report (`symbol, trade_date, transaction_type, quantity, price, segment, expiry, strike_price, option_type, trade_id`) | dates only: no time-of-day bucket and no window checks |
| Dhan | DhanHQ trade history (`tradingSymbol, transactionType, tradedQuantity, tradedPrice, exchangeSegment, productType, exchangeTime`) | carries each trade's charges, used as is |
| Angel One | SmartAPI trade book (`tradingsymbol, transactiontype, fillsize, fillprice, filltime, orderid, fillid`) | `filltime` is a time only: pass `--date` |
| Groww | Trade API trade list, JSON (`trading_symbol, transaction_type, quantity, price, trade_date_time, trade_status`) | times in UTC (converted); rows that did not execute are skipped |

`ticket.py orders --broker NAME --json` (trader-broker's broker-neutral
trade book) imports the same way. Charges a file does not carry are worked
out with trader-broker's charge model for that broker's profile and marked
as estimates. The layouts come from each broker's documentation (pages
named in `journal.py`); the test files under `tests/fixtures/trader-journal/
imports/` are built from those layouts, not downloaded from a real account,
so check the first import of your own file against the broker's contract
note.

## The rule-break log

Each rule note carries its time and the trade's net P&L, and the log
compares trades with a break against trades without.

| note | when |
| --- | --- |
| override | placed although trader-risk warned on a behaviour rule (for example with `"revenge_trade": "warn"`) |
| after-breaker | opened after `max_consecutive_losses` losing trades in a row that day, or once the day's loss reached `max_daily_drawdown_pct` of capital: the lock trader-risk applies |
| revenge | the same stock re-entered within `revenge_window_min` minutes of a loss on it |
| oversized | the max loss recorded with the yes over `max_loss_per_trade`, or with no stop on record a position over `max_order_value` |
| outside-window | an intraday entry before `no_entries_before` or after `no_entries_after` |
| late-exit | an MIS position closed after `flat_by` |
| off-ticket | opened by a fill trader-risk never saw (a manual or broker-side order) |
| overtrading | an entry past `max_trades_per_day` |

The **overtrading index** is entries per traded day over your plan
(`max_trades_per_day`): 1.00 is the plan, above it you trade more than you
meant to. The **cost drag** is charges as a share of the gross profit (and
of the winners' gross); over a third usually means too many or too small
trades.

Paper trades through the tools rarely break a rule, because trader-risk
blocks them first. The breaks show up where the tools were bypassed: the
trade the risk check blocked and you then placed at the broker yourself.

## The counterfactual ledger

"Did the tools save me money?" is a fair question, and the honest answer
needs the trades that never happened. The ledger follows:

- every **co-trader candidate you did not take**: `ready` ones are
  *skipped*, `blocked` ones are *blocked by risk* (read through a small
  adapter from `trader/cotrader/log.jsonl`, schema
  `bharat-trader/cotrader-log/1`);
- every **trader-risk BLOCK of your own entry**, with the plan captured at
  the check.

Each plan is replayed on the bars after it with trader-lab's fill rules
and no look-ahead: in at the first bar starting at or after the decision,
at its open plus slippage (the larger of 3 basis points and one tick),
skipped if that open is already through the stop or the target; then the
stop first inside any bar, a gap through the stop or target fills at the
open (not on the entry bar), out at the flat-by for a 5-minute plan, or
after the maximum holding at the next bar's open for a daily one (15 bars
unless the plan says otherwise); net of trader-lab's charges on the plan's
own quantity. A plan you traded anyway that day counts as *taken*, an F&O
basket as *not replayable*, a plan whose bars end before it closes as
*open*.

Per group: the number of plans, replayed, taken and unresolved; **saved**
(what the replays that lost would have lost), **cost** (what the replays
that won would have made), the net, and the win rate of the replays with
its interval. The caveats print every time:

- hypothetical fills on bars, not an order book; a real limit may never
  have filled;
- daily bars cannot order the stop and the target inside one day (the stop
  is assumed);
- a few replays say little;
- a skipped trade that would have won is not a mistake by itself.

## The weekly review

`journal.py review --week 2026-W44` (or `this`, `last`) writes
`trader/journal/weekly-2026-W44.md`, the journal-coach persona's input: the
week's numbers with their ranges, the breakdowns by persona, strategy and
regime, every rule break with its time, overtrading and costs, the week's
counterfactual ledger, a note for the coach (process only under 20 trades;
one rule-shaped change a week) and what the journal does not show. The
MCP server lists it as a resource
(`bharat-trader://trader/journal/weekly-2026-W44.md`).

## In the UI and over MCP

The UI's **Journal** page shows the stats tiles with their intervals, the
equity curve, the breakdown table (switch the grouping), the rule-break
list with the overtrading index and the cost drag, the counterfactual
summary, and every closed trade; a trade's details open in a dialog where
you can add notes and tags (`POST /api/journal/note`). The Today page has a
small "This week" card. Routes: `GET /api/journal` (`?lite=1` for the week
only).

MCP tools (read only): `journal_summary`, `journal_stats` (`by`, `week`)
and `counterfactual_report` (`group`, `since`, `until`, `limit`). The
`weekly_review` prompt and the journal-coach persona use them.

## Limits

- Paper fills are simulated; so are the counterfactual replays.
- A journal describes your past, not your future, and a pattern in a
  handful of trades can be chance.
- Persona and strategy are only as good as the tag on the drafted order:
  untagged and broker-side trades show as unattributed / untagged.
- Lots are shown only when a lot size is on record (the paper portfolio or
  trader-strategy's file).

Education and tooling, not financial advice.
