---
name: trader-lab
description: >-
  Strategy Lab for NSE stocks: the honest way to look for a strategy with an
  edge. Writes a strategy as a plain JSON spec (entry and exit rules from a
  small safe vocabulary: SMA, EMA, RSI, ATR, VWAP, opening range, previous-day
  high and low, gap %, crosses, time windows), backtests it bar by bar on real
  NSE bars with no look-ahead (signal on the close, fill at the next open plus
  slippage), full Indian charges (brokerage, STT, exchange, SEBI, stamp duty,
  GST), the closing auction and MIS square-off, trader-budget's sizing and
  trader-risk's day limits. Then tests it the hard way: walk-forward out of
  sample, the parameter neighbourhood, Monte Carlo reshuffles, the deflated
  Sharpe ratio, the probability of backtest overfitting, India VIX and NIFTY
  trend regimes, and 1.5x and 2x costs. Pre-registered gates give REJECT,
  WATCH (paper forward test) or PROMOTE, and auto-DEMOTE when the forward
  record decays. Use when the user says "backtest this", "does ORB work",
  "find me a profitable strategy", "test my strategy", "is this strategy
  overfitted", "VWAP strategy backtest", "RSI 2 on NIFTY 50 stocks", "walk
  forward", "paper test this strategy", "which intraday strategy works in
  India", or "strategy lab". It never promises profit and never places an order.
argument-hint: "[list | run NAME | report NAME | compare A B | forward NAME]"
---

# trader-lab

A strategy earns a place in the playbook only with evidence that survives
realistic costs and out-of-sample testing. This skill gathers that evidence
and says, every time, what it does NOT prove. Most backtests lie: they peek
at the future, ignore charges, and report the best of many tries. The lab is
built to stop each of those. Expect most strategies to be rejected. That is
the lab working, not failing.

All amounts are rupees, all times IST, the market is the NSE cash segment,
long only (the pack sizes and checks buys). `<skill dir>` is this skill's
folder.

```bash
python3 "<skill dir>/lab.py" list                          # the built-in library and your runs
python3 "<skill dir>/lab.py" show orb15-vwap               # a spec, as JSON
python3 "<skill dir>/lab.py" validate my-spec.json         # check a spec before running it
python3 "<skill dir>/lab.py" run orb15-vwap                # fetch bars, run everything, write trader/lab/orb15-vwap/
python3 "<skill dir>/lab.py" run orb15-vwap --data DIR     # offline bars: DIR/SYMBOL-5m.csv(.gz), SYMBOL-1d.csv(.gz)
python3 "<skill dir>/lab.py" report orb15-vwap             # the plain-words scorecard
python3 "<skill dir>/lab.py" compare orb15-vwap gap-fade rsi2-nifty50
python3 "<skill dir>/lab.py" forward orb15-vwap            # paper forward test: today's signals -> draft plans
```

`--json` on run, report, compare and forward prints the numbers for a UI or
another tool. `--now "2026-10-30 15:40"` pins the clock (IST); bars not complete by then are not seen. `--out DIR`
moves the lab folder (default `trader/lab`).

## What it reads and writes

| file | what |
| --- | --- |
| `library/*.json` (this folder) | the built-in specs: strategies to test, not promises |
| `trader/lab/<name>/spec.json` | the spec as it was run. Edit this copy to try a variant: every edit counts as another trial |
| `trader/lab/<name>/results.json` | every number, every trade, schema `bharat-trader/lab-results/1` |
| `trader/lab/<name>/scorecard.md` | the plain-words scorecard |
| `trader/lab/<name>/equity.csv` | date, P&L, equity, drawdown %, trades, out-of-sample flag |
| `trader/lab/<name>/status.json` | the verdict, its history and every spec version run |
| `trader/lab/<name>/forward.json` | the rolling paper forward record |
| `trader/lab/<name>/forward/<date>/plan-<SYM>.json` | draft plans for trader-budget (`forward`) |
| `trader/lab/data/` | Yahoo bars cached as CSV, refreshed once a day |

## The built-in library

| name | bars | idea |
| --- | --- | --- |
| `orb15-vwap` | 5m MIS | first close above the 15-minute opening range, above VWAP, 09:30-11:30; stop the range midpoint; 2R |
| `vwap-reclaim` | 5m MIS | a close back above VWAP, 10:00-14:00, still above yesterday's close; stop under the last 6 bars' low; 2R |
| `gap-fade` | 5m MIS | gap down 0.75% to 4%, a close back above the day's open; stop under the day's low; target yesterday's close |
| `pdh-breakout` | 5m MIS | first close above yesterday's high, above VWAP, 09:30-13:00; stop 1.5 ATR; 2R |
| `rsi2-nifty50` | 1d CNC | above the 200-day average, RSI(2) under 10; out on a close above the 5-day average or after 10 days |
| `breakout20-swing` | 1d CNC | a close above the 20-day high; stop 2 ATR; out on a close under the 10-day low or after 30 days |

Each one names its universe (ten liquid NIFTY 50 stocks), its parameters,
the grid around them and its gates. Copy one, change it, run it.

## A spec

```json
{"name": "orb15-vwap", "timeframe": "5m", "product": "MIS", "side": "long",
 "universe": ["RELIANCE", "HDFCBANK", "ICICIBANK"],
 "params": {"or_minutes": 15, "target_r": 2},
 "entry": {"all": [["close", "crosses_above", {"or_high": "$or_minutes"}],
                   ["close", ">", "vwap"], {"time_between": ["09:30", "11:30"]}]},
 "stop": {"price": {"or_mid": "$or_minutes"}}, "target": {"r": "$target_r"},
 "exit": {"flat_by": "auto"}, "grid": {"or_minutes": [15, 30], "target_r": [1.5, 2, 3]},
 "gates": {"min_oos_trades": 30}}
```

The vocabulary (nothing in a spec is ever executed as code):

- values: numbers, `"$param"`, `open high low close volume`, `vwap prev_high
  prev_low prev_close day_open day_high day_low gap_pct`, `{"sma": n}`
  `{"ema": n}` `{"rsi": n}` `{"atr": n}` `{"highest": n}` `{"lowest": n}`
  (with `"of"` a field), `{"or_high": minutes}` `{"or_low": m}` `{"or_mid": m}`,
  `{"add"|"sub"|"mul"|"div": [a, b]}`; any of them with `"shift": k` (k bars back)
- conditions: `[a, op, b]` with `> >= < <= crosses_above crosses_below`,
  `{"all": [...]}`, `{"any": [...]}`, `{"not": ...}`, `{"time_between": ["09:30", "11:30"]}` (the bar's close time)
- stop: `{"atr": n, "mult": k}`, `{"pct": x}` or `{"price": value, "buffer_ticks": t}`; target: the same or `{"r": k}`
- exit: `when` (a condition: out at the next open), `max_bars`, `flat_by` ("auto" or a time; never later than the safe flat-by)
- `sizing`: trader-budget config keys (capital, risk_per_trade_pct, max_trades_per_day, broker_profile...)
- `costs`: `slippage_bps` (3) and `slippage_ticks` (1) a side
- `trials_tried`: variants you tried before this one. Be honest: the deflated Sharpe charges for each

Anything else is refused with the path of the problem: unknown names, code,
deep nesting, more than 36 grid points, a short side, a daily spec on MIS.

## How it tests (docs/lab.md has the method)

1. **Backtest, bar by bar.** A signal on a bar's close fills at the next
   bar's open plus slippage. Stops fill at the stop less slippage, at the
   open if a bar gaps through; a bar touching both stop and target counts as
   the stop. Charges on every trade with the shared `round_trip_charges`.
   Shares as trader-budget sizes them (the loss at the stop, charges
   included, inside the max loss; the position cap; the cash after the
   buffer). One position per stock. MIS exits at the safe flat-by from the
   dated session rules (15:10 for F&O stocks while the closing auction
   starts at 15:15, earlier with a broker profile), and no entries after the
   cutoff. trader-risk's day limits for the portfolio: trades a day, the day's
   planned loss, the daily loss limit, the 1.5 reward:risk minimum.
2. **Walk-forward.** Choose the grid point with the best in-sample net (among
   points with 5 or more in-sample trades) on a rolling window (20 sessions for
   5m, 120 for 1d), score it on the next unseen window (10 or 40), roll on.
   Out-of-sample numbers are the joined unseen windows.
3. **Neighbourhood, not the best point.** The share of grid points around the
   spec's own point that make money after charges.
4. **Monte Carlo** (seeded): the out-of-sample trades reshuffled for the
   drawdown range, and bootstrapped for the range of outcomes.
5. **Deflated Sharpe ratio** and **probability of backtest overfitting**
   (CSCV over the grid). Every earlier spec version counts as a trial.
6. **Regimes**: by India VIX and NIFTY 50 trend the day before.
7. **Costs** at 1.5x and 2x (charges and slippage).

## Gates and the verdict

Set in the spec's `gates` before the run (defaults, conservative): at least
30 out-of-sample trades, out-of-sample expectancy above 0R after charges, an
out-of-sample profit factor of 1.2, max drawdown 10% of capital, deflated
Sharpe 0.95, PBO under 0.5, still above 0R at 1.5x costs, and half the
parameter neighbourhood in profit.

- **REJECT**: any gate failed. Say which, in one line each.
- **WATCH**: every gate passed. Paper forward test it: `lab.py forward NAME`
  each session (after the close for daily specs, during the day for 5m).
- **PROMOTE**: WATCH plus 20 paper forward trades that made money.
- **DEMOTE**: a promoted strategy whose last 30 forward trades average 0R or
  less, or whose forward drawdown passes 1.5x the out-of-sample one.

## The paper forward test

`lab.py forward NAME` loads the latest bars, settles the strategy's signals
since the backtest's last day into `forward.json` (the lab's own simulated
fills on bars it never saw), re-checks the verdict and, for WATCH or PROMOTE
only, writes a draft plan per fresh signal in trader-exit's plan shape. It
never sizes, checks or places anything. The user's path from a plan:

```bash
python3 <trader-budget dir>/size.py --plan trader/lab/orb15-vwap/forward/2026-10-30/plan-SBIN.json --write
python3 <trader-risk dir>/check.py
python3 <trader-broker dir>/ticket.py show        # the paper ticket and the user's yes
```

A daily spec's plan is dated for the next session's open. A spec without a
target gets a 3R cap on the order legs; its real exit is its rule, so set an
alert with trader-alerts.

## Options strategies

Not in this version. A spec with `"kind": "options"` goes to `OPTIONS_HOOK`,
which trader-strategy will register (docs/lab.md describes the contract);
until then it is refused.

From an AI assistant, the MCP server (`mcp/server.py`) offers the same as
`lab_list`, `lab_run`, `lab_report` and `lab_forward`; `lab_forward` with
`draft: SYMBOL` hands a plan to `paper_order_draft` and nothing else.

## How to talk about results

Read the verdict line and the "What this does NOT prove" list to the user.
Never call a strategy profitable, never project returns, never say a PROMOTE
is safe. A REJECT is useful information: it saved real money.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
