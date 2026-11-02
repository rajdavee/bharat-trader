# Edge hunt: the final results

The verdicts of the pre-registered edge hunt ([the brief](edge-hunt.md),
issue #74), published whatever they are. Every number below comes from the
executor's scorecards (`trader/lab/edge-hunt/<id>/scorecard.md`, written by
`lab.py hunt run`) and the outcome it wrote into each hypothesis file in
[`trader-lab/library/edge-hunt/`](../../trader-lab/library/edge-hunt/). The
registered content of every file still matches its manifest hash: a run only
filled `status` and `outcome`.

As of 2026-11-03: **13 of 15 registrations decided, all REJECT. One of those
(eh-08) is VOID: it tested nothing, and eh-15 re-ran it properly. eh-12 is
blocked on data and eh-14 waits for its live holdout. No holdout has been
opened. No WATCH. Benjamini-Hochberg keeps 0.**

**In plain words: none of the pre-registered ideas showed an edge that can
be told apart from luck, once Indian 2026 charges are paid and every trial
is counted.**

Education and research tooling, not financial advice. Nothing here placed an
order, and a REJECT here is a result, not a bug to fix.

## The table

Validation-window numbers (out of sample for the parameter choice), after the
lab's dated 2026 charges and its slippage model. The finalist is the grid
point with the best primary metric on train; validation then screens it. The
deflated Sharpe ratio (DSR) is charged for every trial counted (in brackets).
PBO is the probability of backtest overfitting over the grid. "Expectancy" is
the mean return a trade after charges (for the portfolio engines a trade is
one stock's holding between rebalances, for the option engines it is the
return on the trade's margin or premium, so it is not comparable across
engines). Max DD is the validation drawdown of capital.

| id | verdict | gates passed | gates failed | trades | expectancy a trade | net Sharpe | active Sharpe | DSR (trials) | PBO | max DD | charges a year |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `eh-01-xs-momentum` | REJECT | 8 of 11 | active drawdown (24.8% > 20%); DSR; PBO | 228 | 8.80% | 0.87 | 0.38 | 0.26 (16) | 0.74 | 35.2% | 0.64% |
| `eh-02-low-volatility` | REJECT | 6 of 12 | active drawdown (31.7%); DSR; PBO; 1.5x costs; neighbourhood; active Sharpe | 79 | 12.60% | 0.81 | -0.29 | 0.14 (16) | 0.69 | 27.5% | 0.32% |
| `eh-03-index-trend` | REJECT | 9 of 11 | out-of-sample trades (5 < 30); DSR | 5 | 8.01% | 0.59 | -0.42 | 0.48 (12) | 0.40 | 29.6% | 0.29% |
| `eh-04-turn-of-month` | REJECT | 4 of 10 | expectancy; profit factor; drawdown (30.2%); DSR; 1.5x costs; net Sharpe | 59 | -0.05% | -0.07 | -0.59 | 0.17 (12) | 0.39 | 30.2% | 0.33% |
| `eh-05-vrp-condor` | REJECT | 4 of 10 | expectancy; profit factor; DSR; 1.5x costs; neighbourhood (0%); return on margin | 42 | -0.98% | -0.62 | – | 0.0001 (82) | 0.21 | 4.9% | 0.08% |
| `eh-06-pead` | REJECT | 6 of 10 | active drawdown (62.2%); DSR; 1.5x costs; mean abnormal return (-0.41% an event) | 390 | 1.61% | 0.65 | -0.52 | 0.61 (12) | 0.27 | 26.9% | 1.58% |
| `eh-07-preholiday-expiry` | REJECT | 4 of 10 | expectancy; profit factor; DSR; PBO; 1.5x costs; net Sharpe | 68 | -0.02% | -0.07 | -0.75 | 0.09 (20) | 0.74 | 12.3% | 0.39% |
| `eh-08-event-crush` | REJECT, annotated VOID | 2 of 10 | everything that needs a trade (0 trades) | 0 | 0 | 0 | – | not measured (8) | 1.0 | 0% | 0% |
| `eh-09-event-runup` | REJECT | 2 of 10 | expectancy; profit factor; drawdown (102.4%); DSR; PBO; 1.5x costs; neighbourhood (0%); mean abnormal return (-2.02% an event) | 756 | -2.02% | -1.37 | – | 0.00 (18) | 0.64 | 102.4% | 8.08% |
| `eh-10-st-reversal` | REJECT | 5 of 10 | profit factor; drawdown (47.5%); DSR; PBO; neighbourhood | 642 | 0.79% | 0.40 | -0.27 | 0.18 (543) | 0.53 | 47.4% | 4.62% |
| `eh-11-pairs` | REJECT | 7 of 10 | out-of-sample trades (14 < 30); drawdown (11.5% > 10%); DSR | 14 | 1.63% | 0.66 | – | 0.57 (8) | 0.49 | 11.3% | 0.13% |
| `eh-12-intraday-momentum` | blocked (no minute bars) | – | – | – | – | – | – | – | – | – | – |
| `eh-13-overnight-drift` | REJECT | 3 of 10 | expectancy; profit factor; drawdown (46.9%); DSR; 1.5x costs; neighbourhood; net Sharpe | 1224 | -0.01% | -0.15 | -0.71 | 0.19 (6) | 0.00 | 45.5% | 6.92% |
| `eh-14-trend-day-buying` | waiting (live holdout from 16 Nov 2026) | – | – | – | – | – | – | – | – | – | – |
| `eh-15-event-crush-r` | REJECT | 2 of 10 | expectancy; profit factor; drawdown (100.9%); DSR; PBO; 1.5x costs; neighbourhood (0%); return on margin | 1566 | -4.02% | -1.05 | – | 0.00 (26) | 0.57 | 100.9% | not measured (capital gone) |

eh-15's "charges a year" is a share of the average equity, and its equity
went through zero, so the figure has no meaning (the scorecard prints 0.00%).
The charges were still paid: its expectancy and every gate are after them.

Every REJECT failed before the holdout, so **every holdout is still
sealed**: none of these ideas has been looked at on its last window, and none
will be under the same id. The gates are the ones written in each file
before the run ([edge-hunt.md](edge-hunt.md), rule 4).

## The last three runs (2 to 3 Nov 2026)

- **eh-05, the VIX-timed NIFTY iron condor** (36 grid points, 82 trials with
  the 46 earlier option-library trials). Finalist: 0.10-delta shorts, wings
  1.5% of spot, sold only when India VIX is 4 points over 20-day realised
  volatility, held to expiry. It won 81% of its 42 validation trades and still
  lost: profit factor 0.53, -0.98% a trade on margin, net Sharpe -0.62, and
  **0 of 16 neighbouring grid points** made money on validation. Drawdown was
  small (4.9%) because the size was small. This is the classic short-premium
  shape: many small wins, a few losses that eat them, then the charges.
- **eh-09, the long straddle into results** (6 grid points, 18 trials: 8
  from eh-08's family plus 4 set aside from an older engine revision). It made
  money on train (Sharpe 0.34) and lost the whole capital on validation
  (2020 to mid-2023): -2.0% a trade, 37% winners, 8.1% a year in charges,
  every validation year negative. Implied volatility before results is
  priced in; buying it and paying the round trip does not pay.
- **eh-15, the results-day short iron fly** (eh-08 re-run on the fixed
  engine, 8 grid points, 26 trials). With real spots the trades existed
  (4,066 on train, 1,566 on validation) and both windows lost the whole
  capital: 24% winners on validation, profit factor 0.15. The gap moves on
  results days are larger than the premium the fly collects, at least on the
  strikes and lots that actually traded.

## What it means

- **The deflated Sharpe ratio failed everywhere.** It needs 0.95; the best
  was 0.61 (PEAD), then 0.57 (pairs) and 0.48 (index trend); the three option
  ideas were at 0.00. The DSR asks how likely the true Sharpe ratio is above
  zero once you allow for the best of N tries looking good by luck, for a
  short sample and for fat tails. Below 0.95 means: what we saw is not
  distinguishable from the luckiest of the trials we ran. This is the one
  gate no hypothesis passed. It is not proof that no such edge exists;
  validation windows of 3 to 6 years are short for low-turnover ideas.
- **Beating cash is not beating the index.** Six finalists made money on
  validation in absolute terms (eh-01, 02, 03, 06, 10 and 11 had a positive
  net Sharpe), but every active Sharpe except eh-01's is negative: holding
  the benchmark did better. A long-only idea is worth something only if it
  beats what you would hold anyway.
- **Charges decide the fast ideas.** Overnight drift (eh-13) pays 6.9% a year
  in charges against an edge of a few basis points a session; the results-day
  straddle (eh-09) pays 8.1%; short-term reversal (eh-10) 4.6%; PEAD (eh-06)
  1.6% and fails at 1.5x costs. The slow ideas (momentum, low volatility,
  index trend, pairs) pay 0.1% to 0.6% a year, so costs are not what killed
  them: drawdown, the DSR and overfitting odds did.
- **Selling options was not free money, and neither was buying them.** The
  condor (eh-05) and the results fly (eh-15) sold premium and lost; the
  results straddle (eh-09) bought it and lost. All three have 0% of their
  neighbouring grid points positive on validation: not one nearby parameter
  set worked, which is what "no edge" looks like.
- **Calendar effects are gone or too small.** Turn of the month (eh-04) and
  pre-holiday/expiry (eh-07) have negative validation expectancy after
  charges for their best train point.
- **Overfitting odds are high where the grid was rich.** PBO is 0.74 for
  momentum and pre-holiday, 0.69 for low volatility, 0.64 for the results
  straddle, 0.53 for reversal: the best train point usually does worse than
  the median out of sample.

## The priors, scored

The brief wrote a prior for each idea before any run: the probability that
it passes every gate **and** its sealed holdout. They summed to about 1.5
expected survivors out of 14, with a 19% chance (if independent) that none
would survive.

| | value |
| --- | --- |
| decided registrations scored (eh-08's void run replaced by eh-15) | 12 |
| survivors expected by the priors over these 12 | 1.43 |
| survivors | **0** |
| chance of 0 survivors under the priors (independent) | 21% |
| Brier score of the priors (mean of prior squared, every outcome 0) | 0.021 |
| Brier score of "every idea fails" (prior 0 everywhere) | 0.000 |
| still undecided (priors 0.04 and 0.03) | eh-12, eh-14 |

Zero survivors was the single most likely count under the priors, so the
result is inside what was expected, not a surprise. But the priors were
**too optimistic**: every outcome was a fail, so a forecaster who said "all
fail" would have scored better. The biggest misses were the ideas the
literature likes most: momentum (prior 0.30), low volatility (0.25), index
trend and PEAD (0.15 each). A future registration should start lower, and
the brief's priors are not edited after the fact.

## Benjamini-Hochberg, the programme-level cut

Rule 5 of the brief judges the holdout p-values of all registrations together
at q = 0.10, with a sealed holdout counted as p = 1. The final count is 14
registrations (the 15 files less eh-08's void run; eh-15 takes its place),
**0 holdout p-values** (no finalist earned a holdout), and so **0 kept**.
eh-12 and eh-14 are inside the 14 as p = 1 while undecided; even if one of
them earned a WATCH later, it would have to survive this cut against the 13
others. `lab.py hunt status` prints the same line:

```bash
python3 trader-lab/lab.py hunt status
```

## The near misses, and why they are not re-tested with tweaks

Two ideas came closest, and both are recorded as near misses, not as
"almost working".

- **eh-03 index trend** (a 3/6/12-month time-series momentum blend, long or
  flat NIFTY futures) failed only 2 of 11 gates: it made **5 trades** in a
  5-year validation window (2017 to 2021) where 30 are needed, and its DSR is
  0.48. Expectancy 8.0% a trade, profit factor 3.3, drawdown 29.6% against
  NIFTY's 40.1%, survives 2x costs, 75% of its neighbours positive. But its
  active Sharpe is -0.42: long-or-flat NIFTY earned less than holding NIFTY.
  Five trades cannot tell skill from luck whatever the parameters.
- **eh-01 cross-sectional momentum** (the top 30 of the point-in-time NIFTY
  200 by volatility-adjusted 12-1 momentum, rebalanced quarterly, long only) is the only idea with a positive net Sharpe (0.87) **and**
  a positive active Sharpe (0.38): the only one that beat its benchmark on
  validation. It failed on the DSR (0.26 with 16 trials), the overfitting
  odds (PBO 0.74: the best train point usually lands below the median out of
  sample) and a 24.8% drawdown against the benchmark where 20% was allowed.

Neither is rerun with a faster signal, a longer window, a looser drawdown
limit or a lower trade gate. Each of those is a new trial chosen **after
seeing these numbers**, which is exactly the overfitting the programme is
built to stop: the DSR would have to charge for it, and the holdout would no
longer be a clean test of the idea that was registered. Moving a gate until
an idea passes is how most published backtests are made, and why most of
them fail live. If a better-specified trend or momentum idea is worth
testing, it is registered as a new hypothesis (below), with eh-03's 12 or
eh-01's 16 trials added to its family's count.

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

Under the brief's rule 8, eh-08's REJECT **stays in its file** exactly as
written (its hash still matches);
[`annotations.json`](../../trader-lab/library/edge-hunt/annotations.json)
marks it **VOID** with the reason, the fixes and the evidence, and VOID runs
are left out of the Benjamini-Hochberg cut. **`eh-15-event-crush-r`**
re-registered the identical test under a new id with eh-08's 8 trials in
`prior_trials`, ran on the fixed engine on 2026-11-03 and was **REJECTED**
on real trades (above). The executor leaves eh-08 out of eh-15's family
count, so those 8 trials are counted once, not twice.

## The two that are not decided

- **eh-12 intraday momentum: blocked.** It needs index or index-futures
  minute bars from 2022 to 2026, and the archive has only end-of-day files.
  What would unblock it: an Upstox minute-bar backfill with your own token
  (Upstox's v3 historical candles start in January 2022 for minute
  intervals; see [archive.md](../archive.md), "Intraday from your broker"),
  then `lab.py hunt run eh-12-intraday-momentum --dry-run` to confirm the
  coverage. Its prior was 0.04.
- **eh-14 trend-day option buying: waiting.** Its train and validation use
  model prices, but its holdout can only be the **live option chain captured
  from 16 Nov 2026** by the capture job ([forward-tests.md](../forward-tests.md)):
  real bids and asks nobody has seen yet. It runs once that window exists
  and has ended; until then its verdict is open and capped at WATCH. Its
  prior was 0.03.

## What this means for a trader

- **Most retail strategies lose, and these results agree.** SEBI's own F&O
  studies found 89% to 93% of individual F&O traders lost money (FY22 to
  FY25; [the brief](edge-hunt.md) has the sources). Twelve ideas with real
  academic support, tested honestly on 15 to 22 years of NSE data, produced nothing that clears the bar after charges. A strategy sold
  to you as "backtested" almost never says how many tries it took.
- **The tools' main value is saying no.** A REJECT before you trade is
  capital you keep. The lab, the co-trader's "no trade today" and the risk
  checks exist to stop bad trades more than to find good ones.
- **The only road to PROMOTE is still the paper forward test.** A strategy
  earns live trading in this pack only after a WATCH verdict and weeks of a
  paper record inside its predicted range ([forward-tests.md](../forward-tests.md),
  [lab.md](../lab.md)). Nothing in this programme reached even WATCH, so
  nothing is promoted, and the live co-pilot still has nothing to propose.
- **Low-cost, slow, diversified holding was hard to beat.** In this
  programme, holding the benchmark beat almost every active idea after
  charges.

## How to propose a new hypothesis

New ideas are welcome, under the same rules as these 15. The order matters:

1. **Register first, before any run.** Open an issue, then a pull request
   adding one file to `trader-lab/library/edge-hunt/` in the
   [schema of the brief](edge-hunt.md#the-pre-registration-schema): the
   mechanism (why it should pay, and who is on the other side), the grid,
   train, validation and sealed holdout dates, the gates, kill criteria, a
   prior and `contamination` (what data you or this repo have already seen).
   Its hash goes into `manifest.json`; `tests/test_edge_hunt.py` checks it.
   Nothing about it is run until the registration is merged.
2. **Every trial is counted.** Each grid point is one trial; `prior_trials`
   counts earlier tries of the same kind (in this repo or yours, honestly);
   `--extra-trials` adds any you ran elsewhere. The deflated Sharpe is
   charged for all of them.
3. **Family accounting.** An idea in an existing family (`momentum-xs`,
   `trend-ts`, `event-vol`, `vrp-selling` ...) is charged that family's
   earlier edge-hunt trials too: a new momentum variant starts with eh-01's
   16 on its bill, a trend variant with eh-03's 12. Re-running a decided idea
   is allowed only as a re-registration of an annotated (VOID) run under rule
   8, never to tweak a REJECT.
4. **It joins the programme's Benjamini-Hochberg cut**, so every new
   registration raises the bar for all the others a little. A good idea has
   to be worth that.
5. **The verdict is published whatever it is**, in this page's table.

## What this does NOT show

- It does not show that these effects never existed in India, only that the
  registered versions did not clear the gates on 2010s to 2020s data after
  2026 charges. Several ideas are thin on trades in validation (eh-03, eh-11).
- The windows are all the same past. A REJECT on validation says nothing
  about the sealed holdouts, which stay unopened.
- Charges are a typical discount broker's, slippage is a model, income tax is
  left out, and option runs are end of day (a stop is seen at the close); a
  larger account would move prices more.
- The option ideas were tested on the strikes and lots that actually traded
  in the archive; a few contract-days with only a reconstructed lot were
  skipped (counted in each scorecard).
- The priors were judgement, and the Brier score above grades that judgement,
  not the market.
