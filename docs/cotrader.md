# The co-trader

"What are the best trades right now, and how sure are we?" `trader-cotrader`
answers that question from evidence, and most days the honest answer is
**none**. On 2.3 years of real NSE data every built-in trader-lab strategy
was rejected and a 500-trial strategy discovery found no finalist. So the
co-trader's first job is to say plainly when there is no validated edge, why,
and what would change it. Sitting out is shown as a decision, not a failure.

When something does qualify, each candidate comes with its evidence tier, a
probability with its 95% range and sample size, the expected result after
charges, a second opinion, what would invalidate it, what the evidence does
NOT show, and a trader-risk verdict. Every probability is logged and later
checked against what happened.

Education and tooling, not financial advice. It never places an order.

```bash
python3 trader-cotrader/cotrader.py today                      # PROMOTED + WATCH, or why there is nothing
python3 trader-cotrader/cotrader.py today --tier research      # also RESEARCH ideas: unproven, study and paper only
python3 trader-cotrader/cotrader.py today --json --no-write    # the numbers, nothing written
python3 trader-cotrader/cotrader.py explain 2026-09-21-t-sma20-watch-AXISBANK
python3 trader-cotrader/cotrader.py calibration --no-demote    # how well past probabilities matched outcomes
python3 trader-cotrader/cotrader.py log --limit 20
```

Offline and pinned: `--data DIR` (bar files), `--archive DIR` (default
`trader/archive` or `$BT_ARCHIVE`), `--no-archive`, `--offline` (no network),
`--now "2026-09-18 16:00"` (IST; no later bar is seen), `--workdir DIR`.

## Where candidates come from

The co-trader has no strategies of its own. It reads trader-lab's runs
(`trader/lab/<name>/`): the spec, the backtest's trades after charges, and
the verdict. The verdict sets the **evidence tier**:

| tier | lab verdict | meaning | shown |
| --- | --- | --- | --- |
| PROMOTED | PROMOTE | every pre-registered gate passed and at least 20 paper forward trades made money | by default |
| WATCH | WATCH | every gate passed; the paper forward test is still running. Paper only | by default |
| RESEARCH | REJECT, DEMOTE, a library spec never run, or demoted by the co-trader for poor calibration | historical numbers that did NOT pass validation: "unproven, for study/paper only" | only with `--tier research` |

The verdict is re-checked on today's bars by the lab's own forward test
(`lab.forward`, without writing), so a PROMOTE whose forward record decayed
is a DEMOTE here too. A RESEARCH idea is never eligible for a live order:
its plan is tagged `cotrader-research:<strategy>` and trader-risk blocks such
an entry whenever the config is live (`"paper": false`).

Option strategies (`"kind": "options"`, backtested by trader-lab on the
archive's end-of-day F&O chains) are scored from their lab history like any
other; there R is the result per rupee of margin, as the lab reports it. A
WATCH or PROMOTE options spec's live trigger is the basket trader-lab's
options forward test drafts through trader-strategy when its entry fires on
the latest session: the candidate shows the structure, expiry, legs, margin
and max loss after charges, and trader-risk checks the basket as one
position (blocked unless your config allows F&O). Draft to paper writes it
to `trader/strategy.json` and `trader/orders.json`; one typed yes covers the
whole basket. A RESEARCH options spec shows its stats only (the lab drafts no
basket for it), and the time machine has no replay for a basket. Options
need the local trader-archive.

## Does the strategy fit today's market?

Every past trade is labelled with the market regime as of the close before
its signal (trader-regime: NIFTY 50 trend or range x the India VIX bucket,
low, normal or high; the same cell the lab's regime gate uses). Only trades
that closed before today's signal count, so a run on a past date never sees
later trades. The trades are the lab's out-of-sample walk-forward trades when
there are 30 or more, else its whole-sample trades (said so in the evidence
line: in-sample trades flatter).

If today's cell holds 10 or more past trades and they lose money on average
after charges, the strategy **does not fit today** and is passed over with
the numbers ("regime mismatch: 43 past trades in NIFTY trending · high vol
average -0.16R after charges"). With fewer than 10 the cell cannot rule it
out; the probability below leans on the strategy's overall record instead,
and says so.

## How a probability is made

"Hit" means a past trade that **made money after charges** (brokerage, STT,
exchange, SEBI, stamp duty, GST, and slippage on both sides, as the lab
charges them).

1. **The cell's own rate.** k hits of n trades in today's regime cell, with
   the Wilson score 95% interval:

   centre = (p + z²/2n) / (1 + z²/n), half-width = z √(p(1-p)/n + z²/4n²) / (1 + z²/n), z = 1.96.

   7 hits of 10 gives 39.7% to 89.2%: ten trades say very little.
2. **Shrinkage (empirical Bayes).** A thin cell is pulled toward the
   strategy's overall rate with a Beta prior worth m pseudo-trades,
   m = min(20, all trades): Beta(m·P, m·(1-P)), where P is the overall rate
   Laplace-smoothed, (all wins + 1) / (all trades + 2), so four winners out of
   four never read as a certainty. After the
   cell's k of n the posterior is Beta(k + m·P, n - k + m·(1-P)); the
   probability shown is its mean, (k + m·P) / (n + m), and the 95% range is
   its 2.5% and 97.5% quantiles (the regularised incomplete beta function,
   inverted by bisection). The candidate says how much weight the cell got:
   n / (n + m). Three hits of four with P = 40% shows 11/24 = 45.8%, not 75%.
3. **Expected R after charges**, shrunk the same way: (sum of the cell's R +
   m x the overall mean R) / (n + m), with a normal 95% range from the cell's
   spread over √(n + m) (the overall spread when the cell has under two
   trades). Approximate: the prior's pseudo-trades are not real trades. R is
   the net result divided by the planned risk (entry minus stop, times the
   quantity). A candidate needs this above 0R; a trigger without it is
   "passed over: no edge to offer".
4. **A second opinion.** trader-timemachine replays the plan 2,000 times on
   the stock's own bars before the signal (seed 19): the rest of the day from
   past sessions' 5-minute bars (intraday rules, to the flat-by), or the
   holding period drawn from the last 250 daily bars (daily rules; stop and
   target only, not the strategy's own exit rule). Its "green" share (target
   first, or above the entry at the end) and its average R after charges are
   shown next to the history. When the green share is outside the history's
   95% range, or the replay's average R after charges is on the other side
   of zero, the candidate carries a **DISAGREES** line. Two methods on the
   same past disagreeing is a reason for caution, not a tie-breaker.

### What the probability means, and what it does not

It means: about that share of this rule's past trades, in this kind of
market, made money after charges, and the range is where the true rate of
such trades plausibly sits given how few there were. It does **not** mean
this trade has that chance: markets change, today's news and events are not
in it, fills and charges are models, the universe is today's liquid stocks
applied to the past (survivorship bias), and a WATCH or RESEARCH rule has
not finished proving itself. Each candidate lists these under "What the
evidence does NOT show". Never quote the number without its range and n.

## The plan, the size and the risk check

The lab's own plan for the signal (a LIMIT at the signal bar's close, the
spec's stop and target; a 3R cap when the spec has no target) is sized by
trader-budget with your `trader/config.json` (the loss at the stop with
charges inside your max loss per trade; the position cap; your cash) and
checked by trader-risk exactly as an order would be, without writing
anything: PASS or BLOCK with every reason. A daily signal fills at the next
session's open, so its risk preview is taken at that session's
`no_entries_before`. The charges at the target are shown too. The plan is
saved as `trader/cotrader/plans/<ID>.json`.

## Ranking

    score = EV - 0.25 x (EV 95% range width) - 0.5 x max(ρ, 0) x max(EV, 0)

EV is the shrunk expected R after charges: value per unit of risk. The width
term prefers evidence that is tighter. ρ is the highest correlation of the
stock's daily returns over 60 sessions with an open position on the paper
account or with a candidate already ranked above (the same stock counts as
1), so two bets on the same move do not both rank high. Candidates are
ranked greedily, PROMOTED before WATCH before RESEARCH, then by score; ties
(to 4 decimals) go to the more liquid stock (median daily traded value over
20 sessions). Candidates trader-risk passed come before blocked ones. The
score is a ranking index, not an expected result.

## No edge

When no candidate is ready, the output's first line is "No trade today:
sitting out is the decision, not a failure", followed by why, with numbers:

- no validated strategy (how many lab runs, and their verdicts);
- regime mismatch per strategy, and the regimes where it did have an edge;
- no live trigger on the latest bars;
- triggers with no edge after charges in this regime;
- candidates your own rules blocked (with the first reason);
- the kill switch or a circuit breaker locking new entries;

and what would change it: a strategy passing the lab's gates and its paper
forward test (`lab.py run`, `lab.py forward`, or a trader-discover
finalist), a move into a regime where an eligible strategy had an edge, a
fresh signal on the next bars, the next session's reset of the locks, or
looking at the RESEARCH ideas for study.

## Calibration

A probability is only worth something if, over many predictions, the things
called 40% happen about 40% of the time. So every candidate's probability is
logged once, the first time it appears, in `trader/cotrader/log.jsonl` (id,
time, strategy and spec hash, tier, p and its range, sample sizes, regime,
the plan's levels and quantity). Re-running the same day never logs it
again, so the log cannot be padded.

`calibration` finds each outcome, in this order:

1. **Paper fills**: a BUY of the stock on the plan's day in
   `trader/state/trades.json` and the sells after it with realised P&L.
2. **The lab's forward record**: the strategy's own simulated trade on that
   signal in `trader/lab/<name>/forward.json` (its real exit rule).
3. **The bars after the signal**: filled at the first bar's open (skipped if
   it opens through the stop or the target), then stop first, target, and
   out at the flat-by (intraday) or after the maximum holding at the close
   (daily), net of charges on the logged quantity.

It then scores the resolved predictions overall, per strategy and per tier:

- **Brier score**: the mean of (p - outcome)². Lower is better. Next to it,
  the reference a constant guess at the base rate would score,
  base x (1 - base), and the skill 1 - Brier / reference.
- **Log loss**: -mean(y ln p + (1 - y) ln(1 - p)), with p clipped to
  [1e-6, 1 - 1e-6]. It punishes confident misses hard.
- **Reliability bins**: predictions grouped by p in tenths; for each, how
  many, the mean predicted p, the share that made money and its Wilson 95%
  range. The UI draws them against the diagonal of perfect calibration.

**Demotion.** A PROMOTED or WATCH strategy with 20 or more resolved
predictions whose mean predicted probability lies above the Wilson 95% range
of its observed hit rate is overconfident beyond chance. `calibration`
demotes it through trader-lab's own mechanism: its `status.json` verdict
becomes DEMOTE (the lab keeps a DEMOTE until the spec changes), and
`trader/cotrader/demoted.json` records why, so it shows only as RESEARCH from
then on. `--no-demote` reports without changing anything; the UI and the MCP
server never demote.

On the tests' real log (38 predictions made day by day from December 2025 to
October 2026 on ten NIFTY 50 stocks), the predictions averaged 48% and 29%
made money after charges: overconfident, and the reason these numbers are
tracked. Each strategy had fewer than 20 outcomes, too few to demote.

## Paper trading a candidate

The candidate's plan takes the same road as every order:

```bash
cp trader/cotrader/plans/2026-09-21-t-sma20-watch-AXISBANK.json trader/plan.json
python3 trader-budget/size.py --write                 # trader/orders.json
python3 trader-risk/check.py                          # PASS or BLOCK, recorded
python3 trader-broker/ticket.py show                  # the ticket, and your own typed yes
```

The UI's **Co-trader** page has "Draft to paper" (POST `/api/cotrader/draft`:
the plan, sized and risk-checked, nothing placed); from an AI assistant,
`paper_order_draft` with `candidate: ID`. A daily plan can be drafted only on
its own session day.

## In the UI and over MCP

- **Today**: the top ready PROMOTED or WATCH candidates with their odds,
  range and n, or the no-edge headline and its first reasons.
- **Co-trader** (`#/cotrader`): candidate cards with the tier badge, the
  probability with its range and n, the expected R after charges, the
  replay, the risk verdict, why and why not, the invalidation, what the
  evidence does not show, similar past trades and the draft button; the
  no-edge card; every strategy's status; the calibration chart and tables.
  "Show RESEARCH ideas" re-runs with `--tier research`.
- Routes: `GET /api/cotrader`, `POST /api/cotrader/run`,
  `POST /api/cotrader/draft` ([ui.md](ui.md)).
- MCP: `cotrader_candidates`, `cotrader_explain`, `calibration_report`
  ([mcp.md](mcp.md)).

## Settings

Fixed in code and written into every `cotrader.json` (`settings`): the prior
worth 20 trades, 10 trades for a regime cell to rule a strategy out, 30
out-of-sample trades to prefer the out-of-sample record, the 0.25 width and
0.5 correlation penalties, 60 sessions for correlation, 20 for liquidity,
2,000 replays with seed 19, 250 daily bars for a daily replay (20 sessions
when a rule has no maximum holding), five similar trades, 20 outcomes before
calibration can demote, ten reliability bins.

## Sources

- Wilson, E. B. (1927). Probable inference, the law of succession, and
  statistical inference. *JASA* 22(158).
- Brown, Cai and DasGupta (2001). Interval estimation for a binomial
  proportion. *Statistical Science* 16(2): why Wilson, not the normal
  interval, for small n.
- Efron and Morris (1975). Data analysis using Stein's estimator and its
  generalizations. *JASA* 70(350): shrinking small samples toward the group.
- Brier, G. W. (1950). Verification of forecasts expressed in terms of
  probability. *Monthly Weather Review* 78(1).
- Murphy, A. H. (1973). A new vector partition of the probability score.
  *Journal of Applied Meteorology* 12: reliability and the reference score.

Education and tooling, not financial advice.
