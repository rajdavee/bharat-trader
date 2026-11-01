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
  record decays. Options too: short strangles, iron condors, iron flies,
  credit spreads and calendars on years of NSE end-of-day option chains from
  the local archive (strikes by delta or distance, IV rank and VIX filters,
  profit target, stop, cash settlement with STT on exercise), scored on
  return on margin, tail losses and breaches. Use when the user says
  "backtest this", "does ORB work", "backtest a short strangle", "iron condor
  backtest", "is option selling profitable", "expiry day iron fly",
  "find me a profitable strategy", "test my strategy", "is this strategy
  overfitted", "VWAP strategy backtest", "RSI 2 on NIFTY 50 stocks", "walk
  forward", "paper test this strategy", "which intraday strategy works in
  India", "strategy lab", "edge hunt", "run EH-03", "momentum portfolio
  backtest", "turn of the month NIFTY futures", "pairs trading backtest" or
  "post results drift". Also runs the pre-registered edge-hunt hypotheses with
  the v2 engines (index and stock futures with rolls and margin,
  cross-sectional portfolios on point-in-time universes, calendar and
  results-date events, cointegrated pairs) and opens each sealed holdout once.
  Combines the WATCH and PROMOTE survivors into a strategy portfolio
  ("combine my strategies", "portfolio of strategies", "risk parity",
  "are these strategies correlated", "how much capital to each strategy"):
  correlation on their out-of-sample days, risk parity and capped Kelly
  sizing, a walk-forward test of the mix and auto-pilot bucket proposals.
  It never promises profit and never places an order.
argument-hint: "[list | run NAME | report NAME | compare A B | forward NAME | hunt list | hunt run EH-ID | portfolio build NAME]"
---

# trader-lab

A strategy earns a place in the playbook only with evidence that survives
realistic costs and out-of-sample testing. This skill gathers that evidence
and says, every time, what it does NOT prove. Most backtests lie: they peek
at the future, ignore charges, and report the best of many tries. The lab is
built to stop each of those. Expect most strategies to be rejected. That is
the lab working, not failing.

All amounts are rupees, all times IST. Equity specs trade the NSE cash segment,
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
python3 "<skill dir>/lab.py" forward-report                # forward tests vs the backtest's range, decay alerts
python3 "<skill dir>/lab.py" forward-report --demote NAME  # apply a DEMOTE a decay alert recommends
python3 "<skill dir>/lab.py" run nifty-weekly-strangle-16d --archive trader/archive   # an options spec, EOD chains
python3 "<skill dir>/lab.py" forward nifty-weekly-strangle-16d                       # -> a draft paper basket
```

Bars come from the local trader-archive first when `trader/archive` exists
(years of split- and bonus-adjusted daily bars, broker intraday candles,
NIFTY and India VIX; `--archive DIR`, `--no-archive`), else from Yahoo.

`--json` on run, report, compare and forward prints the numbers for a UI or
another tool. `--now "2026-10-30 15:40"` pins the clock (IST); bars not complete by then are not seen. `--out DIR`
moves the lab folder (default `trader/lab`).

## What it reads and writes

| file | what |
| --- | --- |
| `library/*.json` (this folder) | the built-in specs: strategies to test, not promises |
| `library/edge-hunt/*.json` (this folder) | pre-registered edge-hunt hypotheses (not runnable specs; `lab.py list` skips them): see docs/research/edge-hunt.md |
| `trader/lab/<name>/spec.json` | the spec as it was run. Edit this copy to try a variant: every edit counts as another trial |
| `trader/lab/<name>/results.json` | every number, every trade, schema `bharat-trader/lab-results/1` |
| `trader/lab/<name>/scorecard.md` | the plain-words scorecard |
| `trader/lab/<name>/equity.csv` | date, P&L, equity, drawdown %, trades, out-of-sample flag |
| `trader/lab/<name>/status.json` | the verdict, its history and every spec version run |
| `trader/lab/<name>/forward.json` | the rolling paper forward record |
| `trader/lab/<name>/forward/<date>/plan-<SYM>.json` | draft plans for trader-budget (`forward`) |
| `trader/lab/<name>/forward/<date>/orders-<expiry>.json`, `strategy-<expiry>.json` | an options spec's draft paper basket (`forward`) |
| `trader/lab/data/` | Yahoo bars cached as CSV, refreshed once a day |
| `trader/lab/portfolio/<name>/` | a strategy portfolio: `portfolio.json`, `scorecard.md`, `equity.csv`, `proposal-autopilot.json`, `proposal-risk.json` (`portfolio build`) |

## The built-in library

| name | bars | idea |
| --- | --- | --- |
| `orb15-vwap` | 5m MIS | first close above the 15-minute opening range, above VWAP, 09:30-11:30; stop the range midpoint; 2R |
| `vwap-reclaim` | 5m MIS | a close back above VWAP, 10:00-14:00, still above yesterday's close; stop under the last 6 bars' low; 2R |
| `gap-fade` | 5m MIS | gap down 0.75% to 4%, a close back above the day's open; stop under the day's low; target yesterday's close |
| `pdh-breakout` | 5m MIS | first close above yesterday's high, above VWAP, 09:30-13:00; stop 1.5 ATR; 2R |
| `rsi2-nifty50` | 1d CNC | above the 200-day average, RSI(2) under 10; out on a close above the 5-day average or after 10 days |
| `breakout20-swing` | 1d CNC | a close above the 20-day high; stop 2 ATR; out on a close under the 10-day low or after 30 days |
| `nifty-weekly-strangle-16d` | options | NIFTY weekly short strangle at 16 delta, 6 days out; take half the credit, stop at 2x |
| `nifty-weekly-condor-16d` | options | the same shorts with 100-point wings (defined risk) |
| `nifty-expiry-iron-fly` | options | expiry day: sell the ATM straddle at the open, 200-point wings, hold to the 15:30 settlement |
| `banknifty-monthly-put-spread` | options | BANKNIFTY monthly bull put spread, 25 delta short, 500 points wide, 30 days out |
| `nifty-ivr-strangle` | options | the weekly strangle only when NIFTY's 30-day IV ranks 50 or more over 126 sessions |

Each one names its universe (ten liquid NIFTY 50 stocks) or underlying, its
parameters, the grid around them and its gates. Copy one, change it, run it.

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

### The forward-test dashboard

`lab.py forward-report` (read only; `engines/fwdtest.py`) compares each
strategy's forward record with what its backtest predicted: the
out-of-sample trades give the expectation, and the predicted range of the
forward mean R for n trades is `mu ± 1.96 · sd · sqrt(1/n + 1/N)` (the win
rate likewise). It says inside, below or above (from 5 trades), runs a lower
CUSUM control chart (k 0.5, h 4), and raises a **decay alert** once there are
10+ forward trades and the running mean has sat below the range for 5 trades
in a row or the chart alarmed. A PROMOTE or WATCH strategy with an alert gets
the recommendation DEMOTE, applied only by `--demote NAME` (the lab's own
status, like the co-trader's calibration demotion). It also shows the
co-trader's Brier score, the verdict timeline and the trades and sessions to
PROMOTE. Thresholds: `"forward_tests"` in `trader/config.json`. Quote the
ranges and n; a few weeks of paper trades prove little either way. See
docs/forward-tests.md.

## Options strategies

A spec with `"kind": "options"` runs on the local trader-archive's end-of-day
F&O bhavcopies (`archive.py backfill --years 2 --datasets fo,index,vix,lots`;
`--archive DIR`), never on Yahoo. The code is in this skill's `options/`
helper folder; the legs, strike picking, Black-76 and the SPAN-style margin
come from trader-strategy (its folder must sit next to this one), the market
regime split from trader-regime.

```json
{"name": "nifty-weekly-condor-16d", "kind": "options", "underlying": "NIFTY",
 "structure": "iron-condor", "expiry": "weekly",
 "params": {"delta": 0.16, "wing": 100, "target": 50, "stop": 2},
 "strikes": {"by": "delta", "value": "$delta"}, "wings": {"points": "$wing"},
 "entry": {"days_to_expiry": 6, "weekdays": ["Tue", "Wed"], "iv_rank_min": 30, "vix_max": 25},
 "exit": {"profit_target_pct": "$target", "stop_loss_mult": "$stop", "days_before_expiry": 0},
 "lots": 1, "capital": 500000, "costs": {"slippage_pct": 2, "slippage_ticks": 1},
 "grid": {"delta": [0.12, 0.16, 0.2], "wing": [100, 200]}}
```

- `structure`: short-strangle, short-straddle, iron-condor, iron-fly,
  bull-put-spread, bear-call-spread, calendar (`"type": "CE"|"PE"`),
  long-straddle, long-strangle. `expiry`: weekly (every listed expiry) or
  monthly (the expiries with a future).
- `strikes`: `{"by": "delta", "value": 0.16}` (on the entry day's chain, IV
  solved from the settle prices), `"pct_otm"`, `"points"` from ATM, or `"atm"`.
- `entry.days_to_expiry`: the cycle starts on the first session at or under
  that many calendar days before its expiry (an allowed weekday); the IV rank
  and India VIX filters are read there, once a cycle. `fill`: close (the day's
  settle prices) or open (the day's opening prices, strikes centred on the
  index's open: the expiry-day fly).
- `exit`: `profit_target_pct` of the premium, `stop_loss_mult` of it (both
  checked at each close), `days_before_expiry`; otherwise cash settlement at
  the final settlement price, with STT on exercise for in-the-money longs.
- R for an options trade is net over the margin it blocked, so the gates read
  return on margin. The scorecard adds return on margin, worst week, the 5%
  and 1% tail loss, losing streaks, time in drawdown and the share of
  expiries with a short strike breached.

`forward` on an options spec settles its paper positions since the backtest
and, for a WATCH or PROMOTE spec whose entry fires on the latest session,
writes a draft basket: trader-strategy's `strategy.json` body and an
`orders.json` proposal (paper, a LIMIT a leg at the close). Copy them to
`trader/strategy.json` and `trader/orders.json`, run trader-risk's
`check.py` (the legs are ONE basket) and confirm the basket ticket yourself
with trader-broker. Nothing is placed by the lab.

Limits (docs/lab.md): end-of-day only, no intraday stops or adjustments,
untraded strikes priced by Black-76 at the day's smile IV (flagged),
survivorship of strikes, margin an estimate.

From an AI assistant, the MCP server (`mcp/server.py`) offers the same as
`lab_list`, `lab_run`, `lab_report` and `lab_forward`; `lab_forward` with
`draft: SYMBOL` hands a plan to `paper_order_draft` and nothing else; for an
options spec `draft: NIFTY` writes the basket and runs trader-risk on it.

## The edge hunt (v2 engines)

The files in `library/edge-hunt/` are pre-registered hypotheses
(docs/research/edge-hunt.md): each fixes its data, parameter grid, trial
budget, train, validation and sealed holdout dates and gates before any run.
`lab.py hunt` runs them exactly as registered with the v2 engines in this
skill's `engines/` helper folder.

```bash
python3 "<skill dir>/lab.py" hunt list                             # the 14 hypotheses, status and verdict
python3 "<skill dir>/lab.py" hunt run EH-03 --dry-run              # file hash, data coverage, trial count; runs nothing
python3 "<skill dir>/lab.py" hunt run eh-03-index-trend            # train + validation for the grid, then the holdout once
python3 "<skill dir>/lab.py" hunt status                           # trials, holdout openings, the programme's BH cut
python3 "<skill dir>/lab.py" hunt run EH-ID --data DIR             # a plain data folder instead of the archive
```

- The file must match its manifest hash; an edited file is refused (a change
  is a new hypothesis with a new id). Only `status` and `outcome` are written
  back.
- Every grid point is run on train and on validation (each window closed at
  its last session) and logged once in `trader/lab/edge-hunt/<id>/trials.jsonl`.
  The deflated Sharpe is charged for the grid + the file's `prior_trials` +
  the same family's other edge-hunt runs + `--extra-trials`.
- The finalist is the best point on train; validation screens it with the
  lab's gates (and the file's overrides): trades, expectancy after charges,
  profit factor, drawdown on the registered basis, deflated Sharpe, PBO over
  the grid, 1.5x costs, the neighbourhood, the primary metric. Walk-forward
  by year, 2x costs and a regime split are reported.
- Only a finalist that passes opens the holdout: `holdout.json` is written
  before a single holdout number exists, and a second opening is refused.
  The verdict is REJECT or WATCH (a candidate for the paper forward test).
- Engines: **futures** (continuous near-month series rolled the session
  before expiry, lots by date, SPAN-style margin, the dated 2026 futures
  charges per order, roll costs, overnight or intraday holding, daily MTM),
  **portfolio** (12-1 and 6-1 momentum, low volatility, short-term reversal on
  a point-in-time universe; weekly to semi-annual rebalances; top-N or
  quantile; equal or inverse-volatility weights; CNC charges with STT 0.1% on
  both sides, stamp and a DP charge per sell; turnover; the equal-weighted
  universe and a NIFTY total-return proxy as benchmarks; a capacity note),
  **events** (turn of the month, pre-holiday, expiry week, overnight,
  results-date drift), **pairs** (correlation and Engle-Granger on train
  only, z-score bands, futures legs), **options** (this skill's `options/`
  package: the VIX-timed NIFTY iron condor with its untimed baseline and
  sizing by margin and a 1% tail loss; the results-day iron fly and the
  pre-results straddle on F&O stocks, with implied against realised moves
  from `results_dates`; trend-day option buying, model-priced by Black-76 for
  train and validation, the live captured chain for the holdout, which the
  executor waits for instead of opening early; realised vol by
  close-to-close, Parkinson and Garman-Klass and a HAR-RV forecast fitted on
  train only). Quality is not built (no dated fundamentals).
- Data: trader-archive's read API (`load_continuous` with its roll log,
  `members`, `results_dates`, `load_daily_many`, `load_index`, `load_vix`).
  Without point-in-time membership or results dates for the span a run is
  blocked, not run on a biased universe; the archive's membership caveat
  (NIFTY 100/200/500 approximate before about 2018) goes into the scorecard.

From an AI assistant: `edge_hunt_status` and `edge_hunt_run` (time budget,
progress, resumable, `dry_run`).

## The strategy portfolio

`lab.py portfolio` combines the WATCH and PROMOTE strategies (this skill's
`engines/portfolio_mix.py`; docs/lab.md has the method):

```bash
python3 "<skill dir>/lab.py" portfolio list                     # portfolios, and the strategies that can go in one
python3 "<skill dir>/lab.py" portfolio build my-mix             # every WATCH / PROMOTE strategy
python3 "<skill dir>/lab.py" portfolio build my-mix --strategies A B C --scheme risk-parity --capital 300000
python3 "<skill dir>/lab.py" portfolio build study --strategies A B --research   # other verdicts: studied, never proposed
python3 "<skill dir>/lab.py" portfolio report my-mix [--json]
```

- **Streams**: each strategy's daily returns after charges on days its
  parameters never saw: a lab run's out-of-sample days and paper forward
  record, an edge-hunt finalist's validation and holdout days
  (`trader/lab/edge-hunt/<id>/returns.json`). Only the sessions they all
  share are used; the overlap and each stream's coverage are reported.
- **Correlation**: Pearson, Spearman, downside, stress (the mix's worst
  days) and weekly; near-duplicates (0.7 or more) are flagged as the same
  bet.
- **Sizing**: equal capital, inverse volatility, risk parity and capped
  quarter Kelly on shrunk means, with a per-strategy cap (0.6), no leverage
  and a 10% volatility target that only scales down. The proposal uses
  `--scheme` (default risk parity), chosen before the run.
- **Walk-forward**: weights fitted on the 252 sessions before each monthly
  rebalance only; CAGR, volatility, Sharpe, Sortino, max drawdown, worst
  month, time in drawdown, diversification ratio, risk shares, block
  bootstrap bands.
- **Proposals, never applied**: `trader/lab/portfolio/<name>/proposal-autopilot.json`
  (an `autopilot.strategies` capital-bucket block) and `proposal-risk.json`
  (trader-risk's `max_daily_drawdown_pct`, never looser than yours, and a
  portfolio drawdown at which to pause). The user copies them into
  `trader/config.json` by hand.

Say the overlap and the number of strategies with every number: two or
three strategies give little diversification, and correlations rise in a
crash. From an AI assistant: `portfolio_report` (read only).

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
