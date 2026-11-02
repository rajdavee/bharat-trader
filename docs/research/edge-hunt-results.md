# Edge hunt: the results so far

The verdicts of the pre-registered edge hunt ([the brief](edge-hunt.md),
issue #74), published whatever they are. Every number below comes from the
executor's scorecards (`trader/lab/edge-hunt/<id>/scorecard.md`, written by
`lab.py hunt run`) and the outcome it wrote into each hypothesis file in
[`trader-lab/library/edge-hunt/`](../../trader-lab/library/edge-hunt/). The
registered content of every file still matches its manifest hash: a run only
filled `status` and `outcome`.

As of 2026-11-02: **10 of 15 registrations decided, all REJECT. One of those
(eh-08) is VOID: it tested nothing. No holdout has been opened. No WATCH.**

Education and research tooling, not financial advice. Nothing here placed an
order, and a REJECT here is a result, not a bug to fix.

## The table

Validation-window numbers (out of sample for the parameter choice), after the
lab's dated 2026 charges and its slippage model. The finalist is the grid
point with the best primary metric on train; validation then screens it. The
deflated Sharpe ratio (DSR) is charged for every trial counted (in brackets).
PBO is the probability of backtest overfitting over the grid. "Expectancy" is
the mean return a trade after charges (for the portfolio engines a trade is
one stock's holding between rebalances, so it is not comparable across
engines). Max DD is the validation drawdown of capital.

| id | verdict | gates passed | gates failed | trades | expectancy a trade | net Sharpe | active Sharpe | DSR (trials) | PBO | max DD | charges a year |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `eh-01-xs-momentum` | REJECT | 8 of 11 | active drawdown (24.8% > 20%); DSR; PBO | 228 | 8.80% | 0.87 | 0.38 | 0.26 (16) | 0.74 | 35.2% | 0.64% |
| `eh-02-low-volatility` | REJECT | 6 of 12 | active drawdown (31.7%); DSR; PBO; 1.5x costs; neighbourhood; active Sharpe | 79 | 12.60% | 0.81 | -0.29 | 0.14 (16) | 0.69 | 27.5% | 0.32% |
| `eh-03-index-trend` | REJECT | 9 of 11 | out-of-sample trades (5 < 30); DSR | 5 | 8.01% | 0.59 | -0.42 | 0.48 (12) | 0.40 | 29.6% | 0.29% |
| `eh-04-turn-of-month` | REJECT | 4 of 10 | expectancy; profit factor; drawdown (30.2%); DSR; 1.5x costs; net Sharpe | 59 | -0.05% | -0.07 | -0.59 | 0.17 (12) | 0.39 | 30.2% | 0.33% |
| `eh-05-vrp-condor` | not decided | – | – | – | – | – | – | – | – | – | – |
| `eh-06-pead` | REJECT | 6 of 10 | active drawdown (62.2%); DSR; 1.5x costs; mean abnormal return (-0.41% an event) | 390 | 1.61% | 0.65 | -0.52 | 0.61 (12) | 0.27 | 26.9% | 1.58% |
| `eh-07-preholiday-expiry` | REJECT | 4 of 10 | expectancy; profit factor; DSR; PBO; 1.5x costs; net Sharpe | 68 | -0.02% | -0.07 | -0.75 | 0.09 (20) | 0.74 | 12.3% | 0.39% |
| `eh-08-event-crush` | REJECT, annotated VOID | 2 of 10 | everything that needs a trade (0 trades) | 0 | 0 | 0 | – | not measured (8) | 1.0 | 0% | 0% |
| `eh-09-event-runup` | not decided (4 of 6 trials logged, 0 trades) | – | – | 0 | – | – | – | – | – | – | – |
| `eh-10-st-reversal` | REJECT | 5 of 10 | profit factor; drawdown (47.5%); DSR; PBO; neighbourhood | 642 | 0.79% | 0.40 | -0.27 | 0.18 (543) | 0.53 | 47.4% | 4.62% |
| `eh-11-pairs` | REJECT | 7 of 10 | out-of-sample trades (14 < 30); drawdown (11.5% > 10%); DSR | 14 | 1.63% | 0.66 | – | 0.57 (8) | 0.49 | 11.3% | 0.13% |
| `eh-12-intraday-momentum` | blocked | – | – | – | – | – | – | – | – | – | – |
| `eh-13-overnight-drift` | REJECT | 3 of 10 | expectancy; profit factor; drawdown (46.9%); DSR; 1.5x costs; neighbourhood; net Sharpe | 1224 | -0.01% | -0.15 | -0.71 | 0.19 (6) | 0.00 | 45.5% | 6.92% |
| `eh-14-trend-day-buying` | waiting | – | – | – | – | – | – | – | – | – | – |
| `eh-15-event-crush-r` | registered, not run | – | – | – | – | – | – | – | – | – | – |

Every REJECT failed before the holdout, so **every holdout is still
sealed**: none of these ideas has been looked at on its last window, and none
will be under the same id. The gates are the ones written in each file
before the run ([edge-hunt.md](edge-hunt.md), rule 4).

## What it means

- **The deflated Sharpe ratio failed everywhere.** It needs 0.95; the best
  was 0.61 (PEAD), then 0.57 (pairs) and 0.48 (index trend). The DSR asks
  how likely the true Sharpe ratio is above zero once you allow for the best
  of N tries looking good by luck, for a short sample and for fat tails.
  Below 0.95 means: what we saw is not distinguishable from the luckiest of
  the trials we ran. This is the one gate no hypothesis passed, so the plain
  reading is that **none of these ten showed an edge that can be told apart
  from luck after counting the trials.** It is not proof that no such edge
  exists; validation windows of 3 to 6 years are short for low-turnover ideas.
- **Beating cash is not beating the index.** Six of the decided finalists
  made money on validation in absolute terms (eh-01, 02, 03, 06, 10 and 11
  had a positive net Sharpe), but every active Sharpe except eh-01's is negative:
  holding the benchmark did better. A long-only idea is worth something only
  if it beats what you would hold anyway.
- **Charges decide the fast ideas.** Overnight drift (eh-13) pays 6.9% a year
  in charges against an edge of a few basis points a session, and its
  validation expectancy is negative after them. Short-term reversal (eh-10)
  pays 4.6% a year, PEAD (eh-06) 1.6% and fails at 1.5x costs. The slow
  ideas (momentum, low volatility, index trend, pairs) pay 0.1% to 0.6% a
  year, so costs are not what killed them: drawdown, the DSR and overfitting
  odds did.
- **Calendar effects are gone or too small.** Turn of the month (eh-04) and
  pre-holiday/expiry (eh-07) have negative validation expectancy after
  charges for their best train point.
- **Overfitting odds are high where the grid was rich.** PBO is 0.74 for
  momentum and pre-holiday, 0.69 for low volatility, 0.53 for reversal: the
  best train point usually does worse than the median out of sample.

The priors in the brief expected about 1.5 survivors from 14, with zero a
quite likely outcome. With 10 decided and 0 survivors, the result so far is
inside that expectation, not a surprise.

## The near miss: eh-03, and why it is not rerun

`eh-03-index-trend` (a 3/6/12-month time-series momentum blend, long or flat
NIFTY futures) failed only 2 of 11 gates: it made **5 trades** in a 5-year
validation window (2017 to 2021) where 30 are needed, and its DSR is 0.48. Expectancy 8.0% a
trade, profit factor 3.3, drawdown 29.6% against NIFTY's 40.1%, survives 2x
costs, 75% of its neighbours positive. It also has a negative active Sharpe
(-0.42): long-or-flat NIFTY earned less than holding NIFTY.

It is not rerun with a faster signal, a longer window or a lower trade gate.
Each of those is a new trial chosen after seeing this one's numbers, which is
exactly the overfitting the programme is built to stop; the DSR would have to
charge for it, and the holdout would no longer be a clean test of the idea
that was registered. A near miss is recorded as a near miss. If a
better-specified trend idea is worth testing, it is registered as a new
hypothesis with its own grid, with eh-03's 12 trials added to the trend-ts
family's count, and it would need the trades to exist (a 5-trade validation
cannot pass whatever the parameters).

## The eh-08 void, and eh-15

`eh-08-event-crush` (a short iron fly on F&O stocks over results) ran on
2026-11-02 and the executor recorded **REJECT, 8 of 10 gates failed**. Its
trial log (`trader/lab/edge-hunt/eh-08-event-crush/trials.jsonl`) shows 0
trades in train and 0 in validation in all 8 trials. The cause was a data
bug, not the idea: the F&O stock bhavcopies before July 2024 carry no
underlying price, and the event-vol engine (revision 1) took the spot only
from them, so no at-the-money iron fly existed on any results day from 2012
to mid-2024. #88 fixed it (spot from the stock's equity close, else the near
future); #90 added the dated lot sizes those files also lack.

What was done, under the brief's rule 8:

- eh-08's REJECT **stays in its file**, exactly as written; its hash still
  matches.
- [`annotations.json`](../../trader-lab/library/edge-hunt/annotations.json)
  marks it **VOID**, with the reason, the fixes (#88, #90) and the evidence
  (8 trials logged, trade counts 0 in every one, the finalist's all-zero
  validation numbers). VOID runs are left out of the Benjamini-Hochberg cut.
- **`eh-15-event-crush-r`** is a new registration with the identical test
  (grid, windows, gates, kill criteria, prior), a new id, rank 15, and
  `prior_trials` 8: eh-08's trials are charged to it. Its `contamination`
  says what the void run revealed: that the data was missing, and no returns.
  The executor leaves eh-08 out of eh-15's family count, so those 8 trials
  are counted once, not twice.

## Still to run

- **eh-05 VRP condor**: crashed on a missing NIFTY spot (fixed in #88); the
  lot sizes it needed now exist (#90). Ready to run, 36 grid points, 82 trials
  counted (46 prior).
- **eh-09 event run-up**: interrupted at trial 4 of 6 when the machine ran
  out of memory. Its 4 logged trials (engine revision 1, the same no-spot
  bug, 0 trades) were run by an older engine: it needs
  `lab.py hunt run eh-09 --rerun-superseded`, which keeps them in
  `trials-superseded.jsonl` and still charges them to the trial count.
- **eh-15 event crush, re-registered**: to run after eh-09, on the fixed
  engine, charged 8 prior trials plus the event-vol family's.
- **eh-12 intraday momentum**: blocked; no historical index minute bars of
  the length it needs.
- **eh-14 trend-day option buying**: train and validation on model prices;
  its holdout is the live option chain captured from 16 Nov 2026, so it waits
  for that window to exist and end.

The runs are heavy (each option hunt reads years of F&O chains), so they run
one at a time under the memory guard (`--max-mem-mb`).

## Benjamini-Hochberg, the programme-level cut

Rule 5 of the brief judges the holdout p-values of all registrations together
at q = 0.10, with a sealed holdout counted as p = 1. Today that is 14
registrations (the 15 files less eh-08's void run; eh-15 takes its place),
**0 holdout p-values**, and so **0 kept**. A future WATCH would have to
survive this cut against the 13 others, every one of them a p of 1 if it
stays sealed. `lab.py hunt status` prints the same line.

## What this does NOT show

- It does not show that these effects never existed in India, only that the
  registered versions did not clear the gates on 2010s to 2020s data after
  2026 charges. Several ideas are thin on trades in validation (eh-03, eh-11).
- The windows are all the same past. A REJECT on validation says nothing
  about the sealed holdouts, which stay unopened.
- Charges are a typical discount broker's, slippage is a model and income
  tax is left out; a larger account would move prices more.
- The priors were judgement. Their Brier score is due once every
  registration is decided.
