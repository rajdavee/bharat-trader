---
name: trader-discover
description: >-
  Strategy discovery for NSE stocks with strict overfitting controls: lets the
  machine propose rules, honestly. Searches trader-lab's safe rule vocabulary
  (RSI dips, breakouts, moving-average crosses, gap plays, pullbacks, opening
  ranges, VWAP reclaims, yesterday's high, time windows, stops in ATR, targets
  in R, maximum holding, the stock's own trend filter) with a FIXED trial
  budget declared up front, on daily or 5-minute bars from the local archive.
  Splits history by time into train, validation and an untouched holdout with
  purging and an embargo, logs every trial, and counts every one: deflated
  Sharpe with the true trial count, probability of backtest overfitting over
  the trial matrix, a Reality Check / SPA bootstrap p-value against doing
  nothing, a Benjamini-Hochberg false-discovery cut, cost stress, parameter
  neighbourhood and regime consistency. Survivors become trader-lab specs that
  still need a paper forward test. Use when the user says "find me a
  strategy", "discover a profitable strategy", "search for an edge", "which
  rules work on NIFTY 50", "auto-find intraday setups", "mine strategies",
  "kya koi strategy kaam karti hai", "backtest many ideas" or "is my strategy
  search overfitted". Usually the honest answer is none. It never promises
  profit and never places an order.
argument-hint: "[run --budget N --universe nifty50 --timeframe 1d|5m | resume RUN | report RUN | list]"
---

# trader-discover

Try enough rules on the same history and some will look brilliant by luck
alone. That is how most "found" strategies are made, and why most of them
fail live. This skill searches anyway, but it writes the search down before
it starts, counts every try, and makes each survivor beat the luck of the
whole search on data it never saw. Expect zero finalists most of the time.
That is the tool working: it just saved you from trading noise.

All amounts are rupees, all times IST, long only, NSE cash segment.
`<skill dir>` is this skill's folder. It needs trader-lab next to it (the
backtest, charges and sizing are the lab's own) and uses trader-regime and
trader-archive through the lab when they are installed.

```bash
python3 "<skill dir>/discover.py" run --budget 500 --universe nifty50 --timeframe 1d          # search, then decide
python3 "<skill dir>/discover.py" run --budget 200 --universe nifty50 --timeframe 5m --seed 3
python3 "<skill dir>/discover.py" run --budget 100 --universe RELIANCE,TCS,INFY --data DIR    # offline bars
python3 "<skill dir>/discover.py" resume nifty50-1d-s7-b500                                  # after a pause or a stop
python3 "<skill dir>/discover.py" report nifty50-1d-s7-b500                                  # the plain-words report
python3 "<skill dir>/discover.py" list                                                       # your runs
```

Bars come from the local trader-archive (`trader/archive`, `--archive DIR`,
`--no-archive`) or Yahoo through trader-lab's loader. `--max-seconds N`
(default 3600) is the time budget: the run pauses, saved, and `resume`
carries on with the same plan on the same bars. The process lowers its own
CPU priority and prints progress with an estimate of the time left.
`--json` on every command prints the numbers for a UI or another tool.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/discover/<run>/plan.json` | the declaration, written before the first trial: every trial (family, params, choices), the seed, the split dates, every gate, the data fingerprint |
| `trader/discover/<run>/trials.jsonl` | one line per trial as it ran: spec hash, params, train and validation numbers, purged trades, daily P&L |
| `trader/discover/<run>/holdout.jsonl` | each holdout opening, once per finalist, with its time |
| `trader/discover/<run>/results.json` | every trial's status and reason, the run-level tests, full scorecards of the validation survivors (schema `bharat-trader/discover-results/1`) |
| `trader/discover/<run>/report.md` | the plain-words report |
| `trader/lab/<finalist>/` | each finalist as a trader-lab spec, run by the lab itself |

## The search space

Rule families over trader-lab's vocabulary, each on a coarse grid of values
(the space has 918 daily and 1,398 five-minute points):

| bars | family | the rule |
| --- | --- | --- |
| 1d | `rsi-dip` | RSI(2, 3, 5) under 5 to 25, optional trend filter (above the 50 or 200-day average), out on a close above the 5-day average or after 5 to 20 sessions |
| 1d | `breakout` | a close above the prior 10, 20 or 50-day high; stop 1.5 to 3 ATR; target 2 to 4R; out under the 10-day low or after 10 to 40 sessions |
| 1d | `ma-cross` | EMA 5/10/20 crossing above the 50/100/200-day average; out on the cross back |
| 1d | `gap-down` | a 1 to 3% gap down that closes above its open; out after 3 to 10 sessions |
| 1d | `pullback` | a new 3 to 10-day closing low above the 50/100/200-day average |
| 5m | `orb` | a close above the 15 to 60-minute opening range, in a time window, optional VWAP filter, stop at the range's middle, low or 1.5 ATR, 1.5 to 3R |
| 5m | `vwap-reclaim` | a close back above VWAP in a window, above yesterday's close or the day's open, stop under the last 6 or 12 bars |
| 5m | `pdh-break` | a close above yesterday's high, stop 1 to 2 ATR, 1.5 to 3R |
| 5m | `gap-fade` | a 0.5 to 1.5% gap down reclaiming the day's open; target yesterday's close or 2 to 3R |
| 5m | `rsi-bounce` | RSI(7, 14) crossing back above 20 to 35, optional VWAP or day-open filter |

Every 5-minute rule is MIS, flat by the safe flat-by. `--search random`
(default) draws the budget at random from the space, without replacement,
with the seed; `--search grid` takes it in order. Option structures join the
space when trader-lab backtests options. Market-wide regime filters are
measured (below), not traded: the rule vocabulary filters on the stock's own
trend.

## How it decides (docs/discovery.md has the method and the sources)

1. **Declare.** plan.json fixes the trial list, the seed, the split and every
   gate before the first trial runs.
2. **Split by time.** After the indicators' warm-up: train (50%), validation
   (25%), holdout (the rest), with an embargo of 5 sessions (1 for 5-minute
   bars) after train and after validation. A trade still open at a window's
   end is dropped (purged). Trial backtests stop signalling at the last
   validation day: the holdout is never seen.
3. **Screen.** Train: 30 trades or more, expectancy above 0R after charges.
   Validation: 15 or more, above 0R. At most 40 survivors go on.
4. **Robustness.** Costs at 1.5x and 2x (above 0R at 1.5x); every one-step
   parameter neighbour run (and counted as a trial), half of them above 0R;
   NIFTY trend x volatility regimes (trader-regime, as of the close before
   each signal), half of the regimes with 10 or more trades above 0R, the
   biggest one too.
5. **Multiple testing, with the true trial count N** (search trials,
   neighbours and your earlier runs on the same timeframe): the deflated
   Sharpe ratio at 0.95 or more; a Benjamini-Hochberg cut at q = 0.10 over all
   N bootstrap p-values; for the whole search, a Reality Check / SPA p-value
   under 0.05 and a probability of backtest overfitting under 0.5. The
   Harvey-Liu haircut Sharpe is shown beside it.
6. **Holdout, once.** For each candidate still standing, the holdout is
   opened once and logged: 15 or more trades, above 0R after charges. A
   re-run reuses the logged result; it never looks again.
7. **To the lab.** Each finalist is written to `trader/lab/<name>/` with its
   neighbourhood as the grid and `trials_tried` set to N - 1, and trader-lab
   runs it with its own walk-forward and gates. WATCH at best: a paper forward
   test (`lab.py forward NAME`) is required before any PROMOTE.

Everything else is REJECTED with the stage and the reason, in results.json.

`--costs none` removes charges and slippage: a statistics check only (the
tests use it to show the controls, not the costs, reject noise). Its
finalists never reach the lab.

## How to talk about results

Read the report's first paragraph: "N trials, M survived validation, K
survived holdout and multiple testing", then its "What this does NOT show"
list. Zero finalists is the common, useful answer. A finalist is a WATCH in
the lab: evidence that survived some tests, never a promise; quote its
sample size, its 95% interval and its after-charges numbers, and go through
the lab's paper forward test, trader-risk and the user's typed yes before any
paper order.

From an AI assistant, the MCP server (`mcp/server.py`) offers
`discover_strategies` (budgeted, with progress, cancellable, resumable) and
`discover_report`. The UI's Strategy Lab page lists discovery runs.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
