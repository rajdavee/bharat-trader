# Strategy discovery

`trader-discover` lets the machine propose strategies, and then does its
best to prove each one is luck. This page is the method: why most strategies
a search finds are noise, what the tool does about it, and what it still
cannot tell you.

```bash
python3 trader-archive/archive.py backfill --years 3                            # the history, on your disk
python3 trader-discover/discover.py run --budget 500 --universe nifty50 --timeframe 1d
python3 trader-discover/discover.py report nifty50-1d-s7-b500
python3 trader-lab/lab.py forward d-breakout-1a2b3c4d                            # a finalist's paper forward test
```

## Why most found strategies are noise

Test one rule on ten years of prices and a Sharpe ratio of 1 means
something. Test 500 rules and keep the best, and a Sharpe of 1 is roughly
what the best of 500 coin-flips would show. Bailey and López de Prado call
this selection bias under multiple testing; Harvey, Liu and Zhu found that
most published "factors" in finance would not pass a hurdle that allows for
how many had been tried. The trap has several doors, and López de Prado's
"10 reasons most machine learning funds fail" names the ones that matter
here:

- **Research through backtesting.** Running backtest after backtest until one
  looks good is a search, whether or not anyone counts it.
- **Leakage between training and testing.** Trades that start in one window
  and end in the next, and indicators that overlap the boundary, let the test
  window leak into the training window.
- **One historical path.** A walk-forward or a single holdout is one draw of
  history, not a distribution.
- **Backtest overfitting.** The more knobs and the more tries, the better the
  best backtest looks and the worse it does next.

Costs make it worse in India: STT, exchange charges, GST and stamp duty eat a
rule that trades often. A rule that "works" before charges and not after is
not a rule.

## What a run does

### 1. Declare the search before running it

`plan.json` is written before the first trial and never changed after:

- the **rule families** and their **coarse grids** (the space: 918 daily and
  1,398 five-minute rules over trader-lab's vocabulary),
- the **fixed trial budget** and the **seed**: the trials are a seeded random
  sample of the space without replacement (or the first N in grid order with
  `--search grid`), listed one by one in the plan,
- the **split dates**, every **gate** and the **data fingerprint** (each
  symbol's bar count, first and last date and last close).

A resumed run uses this plan and the bars up to its last day; if the bars
changed under it (re-adjusted, re-fetched), it refuses rather than mix two
experiments.

### 2. Split by time, purge and embargo

After the warm-up the longest indicator needs (200 sessions for daily bars):

| window | share | what it is for |
| --- | --- | --- |
| train | 50% | the first screen |
| validation | 25% | the second screen, the robustness checks and the multiple-testing statistics |
| holdout | about 25% | opened once per finalist, after everything else |

Between train and validation, and between validation and holdout, sit
**embargo** sessions (5 for daily bars, 1 for 5-minute bars) where no
signal counts. A trade still open at its window's last day is **purged**
(dropped), so no trade straddles two windows. This is López de Prado's purged
and embargoed split, cut down to one train / validation / test chain:
combinatorial purged cross-validation over many folds is out of reach for a
stdlib tool on a laptop at this budget.

Every trial's backtest stops signalling at the last validation day. The
holdout is not merely unscored: it is never run until a finalist needs it.

### 3. Run and log every trial

Each trial is a trader-lab spec, backtested by trader-lab's own engine: a
signal on a bar's close fills at the next bar's open plus slippage, Indian
charges on every trade, trader-budget's sizing, trader-risk's day limits, MIS
flat by the safe flat-by. Each one is appended to `trials.jsonl` as it
finishes: spec hash, params, the train and validation numbers, purged
trades and the daily P&L. A crash loses at most the trial that was running.

### 4. Screen

- train: 30 or more trades, expectancy above 0R after charges;
- validation: 15 or more trades, above 0R after charges;
- the 40 best by validation Sharpe go on (a cap on the work below).

### 5. Robustness

- **Costs at 1.5x and 2x** (charges and slippage): still above 0R at 1.5x
  over train and validation.
- **Parameter neighbourhood:** every numeric parameter one grid step up and
  down. A real effect rarely lives on one point of a grid. Half the
  neighbours (two at least) must be above 0R. Each neighbour run is logged
  and counted as a trial.
- **Regime consistency:** trades split by trader-regime's NIFTY trend (trend
  or range) x volatility bucket, as of the close before each signal. Half the
  cells with 10 or more trades must be above 0R, and the largest one too.

### 6. Multiple testing, with the true trial count

N is every trial run: the declared budget, the neighbour checks and the
trials of your earlier discovery runs on the same timeframe in the same
folder. Every statistic below uses N.

- **Reality Check / SPA** (White 2000; Hansen 2005). For the whole search:
  could the best trial's studentised mean daily return have come from a
  family of trials none of which beats doing nothing? The null is simulated
  with the stationary bootstrap (Politis and Romano 1994; mean block about
  T^(1/3) sessions), re-centring each trial on its own mean unless it is
  clearly bad (Hansen's consistent version). Under p = 0.05, or no trial
  passes.
- **Benjamini-Hochberg FDR** (1995). Each trial's own one-sided bootstrap
  p-value of a mean above zero; the cut at q = 0.10 over all N (trials
  without a p-value count as p = 1). This controls the expected share of
  false discoveries among the survivors. Trading rules on the same stocks are
  positively correlated, the case where BH still holds (Benjamini and
  Yekutieli 2001); it is a cut, not a proof.
- **Deflated Sharpe ratio** (Bailey and López de Prado 2014): the probability
  that the true Sharpe beats the best Sharpe N luck-only trials would show,
  allowing for skew and fat tails, at 0.95 or more. The luck spread is the
  variance of the trials' Sharpe ratios, measured robustly (the median
  absolute deviation), so a cluster of genuinely different trials (a real
  edge and its variants, or rules sunk by charges) does not move the hurdle;
  and never below the variance of a single T-day Sharpe estimate, 1/(T-1).
- **Probability of backtest overfitting** (Bailey, Borwein, López de Prado
  and Zhu 2015) by combinatorially symmetric cross-validation over the whole
  trial matrix (sessions x trials, 8 blocks, 70 splits): how often the
  in-sample best ranks below the median out of sample. Under 0.5 for the
  search, or no trial passes.
- **Haircut Sharpe** (Harvey and Liu 2015), Bonferroni form, shown beside
  the gates: how much of a Sharpe the multiple testing takes away.

Not built: Romano and Wolf's stepwise multiple testing (StepM) and the
Harvey-Liu-Zhu BHY haircut over a published-factor prior; the BH cut and the
SPA test cover the same ground with what a stdlib tool can do honestly.

### 7. The holdout, once

Only a trial that passed everything above sees the holdout: 15 or more
trades, above 0R after charges (the 1.5x-cost result is shown too). The
opening is appended to `holdout.jsonl` with its time; a re-run or a re-decide
reuses the record and never looks again.

### 8. Hand-off to the lab

Each finalist becomes a trader-lab spec in `trader/lab/<name>/`: its rules,
its neighbourhood as the lab's grid, `trials_tried` set to N - 1 (so the
lab's own deflated Sharpe charges for the whole search), and a description
naming the run and the holdout. trader-lab then runs it with its own
walk-forward and gates. Its verdict is the status: WATCH at best. A WATCH
needs the lab's paper forward test (`lab.py forward NAME`) before any
PROMOTE, and every paper order still needs trader-risk PASS and the user's
typed yes.

## The reading

`report.md` opens with one paragraph: N trials, how many passed the training
screen, M survived validation, how many passed robustness and the
multiple-testing controls, K survived the holdout, and what that means. Then
the split, the whole-search tests, a scorecard for every validation survivor
(every gate with its number and its threshold), and what the run does not
show. `results.json` holds every trial's status, stage and reason.

## How the controls are tested

`tests/test_discover.py` runs the whole pipeline on synthetic daily bars:

- **No edge.** A driftless random walk, six stocks, 700 sessions, with no
  charges and no slippage (so only the statistics stand in the way): 20
  seeds, 40 trials each, and at least 19 must give zero finalists (they all
  do). With charges, 10 more seeds, all zero.
- **A planted edge.** The same walk, but after a close above the prior 20
  sessions' high the next five sessions drift up 0.6% a day. With charges,
  three seeds of 60 trials: each run must find finalists, and every finalist
  must be a breakout rule.
- The bootstrap is checked on its own: on pure noise its 5% test fires about
  5% of the time; it finds one real column among 20.
- CSCV matches trader-lab's own implementation; the holdout is opened once;
  a cancelled run resumes to the same answer as an uninterrupted one; a
  changed history is refused.

And on real history: the lab's fixtures (ten NIFTY 50 stocks, two years of
daily bars, 60 sessions of 5-minute bars) give zero finalists.

## Limits

- **It is all the past.** The holdout is later bars of the same history, not
  the future. The paper forward test in trader-lab is the next real check.
- **Survivorship.** `nifty50` is NIFTY 50 as NSE Indices published it on
  2026-11-01, applied to the whole history. The archive has no dated index
  membership, so stocks that left the index are missing and stocks that joined
  late are tested before they were members. It flatters every result.
- **Only the trials it saw.** Ideas you tried by hand, in another folder or
  in your head are not counted, and every one of them makes a survivor less
  likely to be real.
- **The bootstrap's assumptions.** Blocks of a few sessions capture short
  dependence; a regime the history never had is not in the resamples.
- **Fills and charges are models** (trader-lab's): next-bar open plus
  slippage, a typical discount broker's charges, no market impact beyond
  that. Income tax is not included.
- **Market regimes are measured, not traded.** trader-lab's vocabulary filters
  on the stock's own trend; a NIFTY-regime filter would need a new rule in the
  lab. Option structures join the search when the lab backtests options.
- **Small samples.** Two years of daily bars on ten stocks gives a validation
  window of a few dozen trades a rule; most rules fail the minimum trade
  counts, which is the right answer for that much data.

## Sources

- D. H. Bailey and M. López de Prado, "The Deflated Sharpe Ratio: Correcting
  for Selection Bias, Backtest Overfitting and Non-Normality", *Journal of
  Portfolio Management* 40(5), 2014.
- D. H. Bailey, J. M. Borwein, M. López de Prado and Q. J. Zhu, "The
  Probability of Backtest Overfitting", *Journal of Computational Finance*
  20(4), 2017 (working paper 2015).
- D. H. Bailey and M. López de Prado, "The Sharpe Ratio Efficient Frontier",
  *Journal of Risk* 15(2), 2012 (the probabilistic Sharpe ratio).
- M. López de Prado, *Advances in Financial Machine Learning*, Wiley, 2018:
  purged and embargoed cross-validation (chapter 7) and the dangers of
  backtesting (chapters 11 and 12).
- M. López de Prado, "The 10 Reasons Most Machine Learning Funds Fail",
  *Journal of Portfolio Management* 44(6), 2018.
- H. White, "A Reality Check for Data Snooping", *Econometrica* 68(5), 2000.
- P. R. Hansen, "A Test for Superior Predictive Ability", *Journal of
  Business and Economic Statistics* 23(4), 2005.
- D. N. Politis and J. P. Romano, "The Stationary Bootstrap", *Journal of the
  American Statistical Association* 89(428), 1994.
- Y. Benjamini and Y. Hochberg, "Controlling the False Discovery Rate",
  *Journal of the Royal Statistical Society B* 57(1), 1995; Y. Benjamini and
  D. Yekutieli, "The Control of the False Discovery Rate in Multiple Testing
  under Dependency", *Annals of Statistics* 29(4), 2001.
- C. R. Harvey and Y. Liu, "Backtesting", *Journal of Portfolio Management*
  42(1), 2015; C. R. Harvey, Y. Liu and H. Zhu, "... and the Cross-Section of
  Expected Returns", *Review of Financial Studies* 29(1), 2016.

Education and tooling, not financial advice.
