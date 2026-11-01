# Strategy Lab

`trader-lab` is how this pack looks for a strategy with an edge, honestly. A
strategy earns a place in your playbook only with evidence that survives
realistic costs and out-of-sample testing, and every report says what that
evidence does NOT prove. The lab never promises a profit and never places an
order.

```bash
python3 trader-lab/lab.py list
python3 trader-lab/lab.py run orb15-vwap          # fetches bars from Yahoo, caches them in trader/lab/data/
python3 trader-lab/lab.py run orb15-vwap --data tests/fixtures/trader-lab   # offline, on the test data
python3 trader-lab/lab.py report orb15-vwap
python3 trader-lab/lab.py compare orb15-vwap vwap-reclaim gap-fade pdh-breakout rsi2-nifty50 breakout20-swing
python3 trader-lab/lab.py forward orb15-vwap      # the paper forward test
python3 trader-lab/lab.py run nifty-weekly-condor-16d --archive tests/fixtures/trader-lab/archive   # options, offline
```

## Why most backtests lie

| the lie | what the lab does about it |
| --- | --- |
| **Look-ahead.** The rule used a number it could not have known yet: today's close to buy at today's open, a day's high before the day ended. | Every indicator at bar *i* uses bars up to *i* only. A signal on a bar's close fills at the next bar's open. A test scrambles every future bar and checks no past signal changes. |
| **No costs.** Intraday edges are often smaller than STT, brokerage, GST and stamp duty. | Every trade pays the pack's own `round_trip_charges` (the same function trader-budget sizes with) plus slippage, and the run repeats at 1.5x and 2x costs. On a tight intraday stop, charges can be a third of the risk or more. |
| **Perfect fills.** A stop that fills exactly at the stop, a gap that never happened. | Market and stop fills lose `slippage_bps` (3) or one tick, whichever is bigger. A bar that opens through the stop fills at the open. A bar touching both stop and target counts as the stop. |
| **Best of many tries.** Try 50 variants and one looks brilliant by luck. | The deflated Sharpe ratio counts every grid point, every `trials_tried` you declare and every earlier version of the spec you ran. The probability of backtest overfitting measures how often the in-sample winner is a loser out of sample. |
| **One lucky parameter.** Works at 15 minutes, fails at 10 and 20. | The scorecard reports the neighbourhood around the spec's point, not the best point, and a gate needs half of it to make money. |
| **Fitted and tested on the same data.** | Walk-forward: parameters are chosen on one window and scored only on the next, unseen one. Gates read the out-of-sample numbers. |
| **Gates moved after the fact.** | Gates live in the spec, written before the run. The spec's hash goes into the results and the status; a changed spec is a new trial. |
| **Rules the account could not follow.** | MIS exits by the safe flat-by from the dated session rules (the closing auction for F&O stocks, your broker's square-off), entries stop at the cutoff, shares are sized like trader-budget, and trader-risk's day limits apply to the portfolio. |
| **Survivorship.** Today's index names, tested over years they were not all in it. | Not fixed: the library's universe is ten names liquid today. The scorecard says so. |

## The spec

Plain JSON, nothing executed. See `trader-lab/SKILL.md` for the vocabulary
and `trader-lab/library/` for six worked specs. A spec holds:

- `universe`, `timeframe` (`5m` intraday MIS or `1d` swing CNC), `side`
  (`long`: the pack sizes and checks buys only)
- `params` and a `grid` around them (at most 36 points)
- `entry`, `stop`, `target`, `exit` (`when`, `max_bars`, `flat_by`), `window`, `entries_per_symbol_per_day`
- `sizing` (trader-budget config keys), `costs` (slippage), `gates`, `walk_forward`, `trials_tried`, `seed`

The validator walks the spec against a whitelist. Unknown names, anything that
looks like code, nesting deeper than 10, more than 300 rule parts, numbers that
are not finite, a short side, a 5m spec that is not MIS: refused with the path
of the problem.

## The backtest

Bar by bar over every symbol's bars in time order.

1. At a bar's close, a flat stock with an entry signal, inside the entry
   window (from `no_entries_before`, 09:30, to the cutoff: `no_entries_after`,
   14:45, and at least 15 minutes before the safe flat-by), under the day's
   trade count and loss limits, gets a pending order. The expected fill is the
   close plus slippage; the stop and target are set from it and rounded to the
   tick (stop down). A target closer than `min_reward_risk` (1.5) is skipped,
   as trader-risk would block it. Shares: trader-budget's four limits
   (`size_qty`, checked against `size.py` in `tests/test_repo.py`), with the
   cash after the buffer net of open positions.
2. The next bar of that stock opens: the order fills at the open plus
   slippage, unless the open is already through the stop or the target, or the
   bar is at or past the flat-by, or (5m) it is the next day.
3. In a position, each bar: a pending exit fills at the open less slippage; at
   or past the flat-by (MIS) it exits at the open; an open through the stop
   fills at the open; a low at or under the stop fills at the stop less
   slippage; a high at the target fills at the target. Stop before target in
   the same bar. At the close, `exit.when` or `max_bars` books an exit for the
   next open.
4. Charges: `round_trip_charges(entry, exit, qty, cfg)` times the cost
   multiplier. R = net P&L / (shares x (entry - stop)).

The portfolio limits come from `sizing` the way `size.py init` derives them:
1 lakh, 1% risk, 3 trades a day gives a ₹1,000 max loss, ₹3,000 a day, a 50%
position cap and a 10% cash buffer. The day's planned losses (the loss at the
stop of each entry) may not pass the daily limit, as in trader-risk.

## Robustness

**Walk-forward.** Sessions after the warm-up (the longest indicator lookback)
are cut into rolling windows: `in_sample` sessions (20 for 5m, 120 for 1d),
then `out_of_sample` (10 or 40), rolled forward by the out-of-sample length.
In each window the grid point with the best in-sample net P&L, among points
with 5 or more in-sample trades, is scored on the next window. Every grid
point is run once over the whole history (the engine is causal, so no point
sees the future); a window's trades are the ones signalled inside it.

**Grid neighbourhood.** The points within one grid step of the spec's point
on every axis. The gate needs `min_neighbourhood_positive` (half) of them to
make money after charges.

**Monte Carlo** (`monte_carlo.runs`, 1000, seeded by `seed`): the
out-of-sample trades reshuffled (the drawdown the same trades could have
given in another order) and bootstrapped with replacement (the spread of
totals and the share of draws that lose money, plus equity bands).

**Deflated Sharpe ratio** (Bailey and López de Prado, *The Deflated Sharpe
Ratio*, 2014). Daily net returns on capital over the tested sessions (zero on
days with no exit). SR is per session; skew and kurtosis from population
moments.

    PSR(SR*) = Φ( (SR − SR*) √(T − 1) / √(1 − γ₃ SR + (γ₄ − 1)/4 SR²) )
    SR*      = √V · ( (1 − γ) Φ⁻¹(1 − 1/N) + γ Φ⁻¹(1 − 1/(N e)) ),  γ = 0.5772…

N = the grid points + `trials_tried` + earlier spec versions run in this lab
folder. V = the variance of the grid points' Sharpe ratios (with one point,
the null variance 1/(T − 1)). DSR = PSR(SR*). The gate wants 0.95.

**Probability of backtest overfitting** (Bailey, Borwein, López de Prado and
Zhu, *The Probability of Backtest Overfitting*, 2015), by combinatorially
symmetric cross-validation: the sessions x grid-points matrix of daily net
P&L is cut into 8 blocks (4 or 2 for short samples). For each of the 70 ways
to take half the blocks as in-sample, the in-sample best point (by Sharpe) is
ranked out of sample; ω = rank / (N + 1), λ = ln(ω / (1 − ω)). PBO is the
share of splits with λ ≤ 0. It needs a grid of at least 2 points; without one
the gate fails ("not measured").

**Regimes.** Each trade is labelled by the previous session's India VIX close
(buckets `regimes.vix`, default under 13, 13-16, 16-20, 20 and up) and the
NIFTY 50 trend (up: close above its 50-day average and that average higher
than 10 sessions ago; down: the mirror; else ranging).

**Market regimes** (with `trader-regime` next to `trader-lab`). Every trade
is also labelled with trader-regime's read of NIFTY 50 as of the close
before its signal day: trend or range (ADX, efficiency ratio, the 20/50 EMAs;
"no clear trend" counts as range) x the India VIX percentile bucket (low,
normal, high over the last 252 sessions) x the signal day's own expiry flag
(expiry day, day before expiry, no expiry; expiry unknown before 20 Nov 2024
without the archive). Each cell shows trades, the mean R with a 95% interval
(mean +/- 1.96 standard errors) and net P&L, for the whole sample and out of
sample; a cell under `min_regime_trades` (10) is marked too few to judge. The
scorecard ends the list with today's regime. The regime thresholds are
trader-regime's defaults; see [regime.md](regime.md).

**Costs.** The out-of-sample windows (same chosen points) and the whole
sample again at 1.5x and 2x charges and slippage.

## Gates

| gate | default |
| --- | --- |
| `min_oos_trades` | 30 out-of-sample trades |
| `min_oos_expectancy_r` | out-of-sample mean R above 0 after charges |
| `min_oos_profit_factor` | 1.2 |
| `max_drawdown_pct` | 10% of capital, whole sample |
| `min_dsr` | 0.95 |
| `max_pbo` | below 0.5 |
| `survive_cost_multiplier` | out-of-sample expectancy still above the minimum at 1.5x costs |
| `min_neighbourhood_positive` | 0.5 |
| `min_forward_trades` | 20 paper forward trades before a PROMOTE |
| `demote_window`, `demote_below_r`, `demote_drawdown_mult` | the last 30 forward trades at 0R or less, or a forward drawdown past 1.5x the out-of-sample one, demotes |
| `require_regime_edge` | 0 (off). Set 1 for the gate **edge in the current regime**: the out-of-sample trades taken in today's NIFTY regime (trend or range x VIX bucket) must number at least `min_regime_trades` (10) and average above `min_oos_expectancy_r`. Fails as "not measured" without trader-regime or NIFTY bars |

Verdicts: **REJECT** (any gate failed), **WATCH** (every gate passed: paper
forward test it), **PROMOTE** (WATCH plus `min_forward_trades` forward trades
with a positive mean R and a profit factor of at least 1), **DEMOTE** (a
PROMOTE whose rolling forward record decayed). A WATCH whose forward record
fails goes to REJECT. `status.json` keeps the history.

## The paper forward test

`lab.py forward NAME` runs the spec (with the parameters the last
walk-forward window chose) over the latest bars. Trades signalled after the
backtest's last day are settled into `forward.json`: the lab's simulated
fills on bars it never saw, the same engine and costs. The verdict is decided
again. For a WATCH or PROMOTE, each signal on the latest bar becomes a draft
plan in `trader/lab/<name>/forward/<date>/plan-<SYM>.json`, in trader-exit's
plan shape with `"setup": "lab:<name>"`. From there it is the pack's normal
path: `size.py --plan FILE --write` (trader-budget), `check.py`
(trader-risk), `ticket.py show` and the user's typed yes (trader-broker).
The lab itself never sizes, checks or places anything.

## What a run does NOT prove

The scorecard lists these every time, with the run's own numbers: the past is
not the future; a short sample covers one or two market moods; out of sample
means later bars of the same history, not the future; survivorship; fills are
a model; charges are a typical discount broker's and income tax is left out;
Yahoo's bars are free and unofficial; bigger orders move prices; and passing
every gate is evidence, not a guarantee.

## The fixture results (honestly)

On the test data (ten liquid NIFTY 50 stocks; 60 sessions of 5-minute bars,
5 Aug to 30 Oct 2026; two years of daily bars) all six library strategies
are REJECTED. That is the expected outcome for public, well-known setups
after Indian charges, and the lab is doing its job by saying so.

## Options strategies

A spec with `"kind": "options"` (trader-lab's SKILL.md has the fields) is
backtested day by day on the local trader-archive's end-of-day F&O
bhavcopies. The engine is trader-lab's `options/` helper package; the legs
come from trader-strategy's templates and strike picking, the margin from its
SPAN-style estimate, the regime split from trader-regime. `lab.OPTIONS_HOOK`
still overrides the engine when something registers one.

**The data.** Every session's board of the underlying from the archive
(`load_fo_chain_eod`: settle, close, open, volume, OI and lot per contract,
the futures' settle prices), trimmed to expiries within 100 days and
strikes within 25% of the spot; the index's own open and close; India VIX.
Expiries, holiday shifts (a Tuesday expiry moved to Monday, as on 19 Oct and
9 Nov 2026) and lot sizes (75 then 65 for NIFTY) are read from the files,
never computed by rule.

**The chain on a day.** For each expiry: the parity forward
F = K + e^(rT)(C - P), the median over the 5 traded strikes nearest the spot
(else the future's settle, else the spot); T in calendar days to 15:30 on
the expiry day; IV solved per contract by bisection on Black-76 from its
settle price; Black-76 deltas. Strikes that did not trade that day carry no
price of their own, so they cannot be picked.

**Entries.** One decision per expiry cycle, on the first session at or under
`days_to_expiry` calendar days before it (an allowed weekday), when the
session before was further out. The IV rank (the underlying's own 30-day
constant-maturity ATM IV, total variance interpolated between the two
expiries around 30 days, ranked with trader-regime's formula over
`iv_rank.lookback` sessions) and India VIX filters are read on that close;
a cycle that fails them is skipped, not retried. `max_open` positions at a
time (1). Fill: the settle price less (or plus, for a buy) `slippage_pct` of
the premium and `slippage_ticks` ticks a leg; `fill: open` uses the day's
opening prices with the strikes centred on the index's open.

**Each later close.** Every leg is marked at that day's settle price when the
contract traded; otherwise at Black-76 with that day's smile IV at its strike
(linear between the nearest quoted out-of-the-money strikes), and the mark
is counted as model-priced. Exits, in order: expiry settlement on the expiry
day; the profit target (`profit_target_pct` of the premium); the stop (a loss
of `stop_loss_mult` times the premium); `days_before_expiry`. Early exits
fill at the close with slippage.

**Settlement.** On the expiry day the near legs settle in cash at the final
settlement price: the expiring future's settle (NSE's FSP) on monthly
expiries, else the index close. No slippage, no closing order: an
in-the-money long is exercised and pays STT on its intrinsic value at the
exercise rate (0.125% before 1 April 2026, 0.15% from then), everything else
expires. Each side of a leg is charged with the dated F&O table on its own
date (brokerage, STT, exchange, SEBI, stamp, GST).

**Margin and R.** trader-strategy's SPAN-style estimate of the whole position
at entry (16 price and IV scenarios plus exposure margin on every short leg)
and again for the expiry day itself when the position is still open then
(2% more exposure on short index options expiring that day). The larger is
the margin it blocks. R = net / margin, so expectancy and the gates read as
return on margin a trade.

**Robustness.** The same machinery as equity specs: the grid, walk-forward
(120 sessions to choose, the next 60 scored), the parameter neighbourhood,
Monte Carlo, the deflated Sharpe ratio, PBO, 1.5x and 2x costs, the VIX and
NIFTY-trend splits, trader-regime's market regime split and its opt-in gate,
plus an IV-rank split. Extra numbers: return on margin (total, a year, a
trade), the worst week and the marked-to-market drawdown and time in
drawdown (open positions valued at each close), the 5% and 1% tail of the
positions' net, the average of the worst 5%, the longest losing streak, and
the share of expiries where the index closed beyond a short strike while the
position was open (for a straddle or fly, beyond the strike plus or minus the
premium a unit).

**Forward test.** `lab.py forward NAME` re-runs the spec with the latest
walk-forward parameters, settles the positions opened after the backtest's
last day into `forward.json` and, for a WATCH or PROMOTE spec whose entry
fires on the latest session, prices the basket through trader-strategy
(`evaluate`) and writes its `strategy.json` body and an `orders.json`
proposal to `trader/lab/NAME/forward/<date>/`. trader-risk checks the legs as
one basket; trader-broker asks for the user's yes. The lab places nothing.

**The library on real data (honestly).** All five built-in specs, run on a
local backfill of NSE's F&O bhavcopies from 1 Jan 2024 to 30 Oct 2026 (703
sessions, the June 2024 election day and the April 2025 tariff fall
included), one lot, ₹5,00,000 capital, default costs (2% of the premium and
a tick a leg), `--now "2026-11-01 12:00"`. Net is after charges and
slippage; RoM is return on the average margin blocked; OOS is the
walk-forward's unseen windows.

| spec | positions (OOS) | net | RoM a year (OOS) | win rate | OOS profit factor | worst position | worst week | max drawdown, MTM | breached | DSR | PBO | gates | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| nifty-weekly-strangle-16d | 147 (114) | ₹53,881 | 11.1% (-4.8%) | 82% | 0.92 | -₹44,482 | -₹42,819 | 13.7% | 23% | 0.62 | 0.47 | 3/8 | REJECT |
| nifty-weekly-condor-16d | 147 (114) | -₹48,894 | -21.6% (-8.3%) | 76% | 0.88 | -₹6,583 | -₹6,430 | 11.7% | 29% | 0.00 | 0.11 | 2/8 | REJECT |
| nifty-expiry-iron-fly | 148 (114) | ₹40,611 | 12.2% (11.5%) | 57% | 1.14 | -₹12,853 | -₹12,853 | 6.6% | 41% | 0.67 | 0.26 | 6/8 | REJECT |
| banknifty-monthly-put-spread | 33 (26) | -₹11,693 | -9.4% (-24.4%) | 73% | 0.59 | -₹12,130 | -₹10,203 | 3.7% | 36% | 0.03 | 0.31 | 2/8 | REJECT |
| nifty-ivr-strangle | 26 (43) | ₹44,165 | 9.9% (-6.9%) | 92% | 0.81 | -₹31,895 | -₹31,895 | 6.6% | 12% | 0.65 | 0.47 | 4/8 | REJECT |

Every one is REJECTED. The strangles win four or five times in six and still
lose out of sample: a single close past the stop (7 April 2025: a ₹5,046
credit became a ₹44,482 loss, because the stop is only seen at the close)
erases months of premium. The condor's 100-point wings cost more than they
save after charges. The expiry-day fly comes closest (6 of 8 gates) but
fails the out-of-sample profit factor and the deflated Sharpe ratio. The
monthly spread has too few trades to judge. The IV-rank strangle's
out-of-sample count is higher than its whole-sample count because the
walk-forward chose lower IV-rank thresholds in some windows.

**Limits, plainly.**

- End-of-day only. A stop is seen once a day at the close: the intraday loss
  before it can be far larger, and an expiry-day fly has no stop at all.
  There are no adjustments, rolls or intraday hedges in v1.
- Survivorship of strikes: only strikes that traded on the entry day can be
  picked, and an untraded strike on a later day is a model price.
- Settle prices are closing prices, not fills; the slippage is a model.
- The margin is an estimate on 2026 scan parameters, not what a broker
  blocked on the day, and return on margin moves with it.
- Short premium has rare, large losses. Two or three years of history may
  not hold the day that matters.

## Using it from code

Everything is a pure function that the UI and an MCP server can call:
`validate_spec(doc)`, `Series(symbol, timeframe, bars)`, `load_bars(...)`,
`backtest(spec, data)`, `walk_forward_splits(...)`, `monte_carlo(...)`,
`deflated_sharpe(...)`, `pbo_cscv(...)`, `regime_split(...)`, `lab_regimes(context)`, `regime_gate(...)`,
`evaluate_gates(...)`, `decide(...)`, `run_lab(doc, data, context)`,
`scorecard(results)`, `forward(doc, data, out)`, `compare_rows(out, names)`; for options specs
`load_options_data(spec, archive)` and the `options/` package (`options_api()`: `validate`,
`load_chains`, `backtest`, `run`, `options_metrics`).
Nothing writes a file except `save_run` and `forward(..., write=True)`.

The MCP server (`mcp/server.py`, see [mcp.md](mcp.md)) exposes the lab as
`lab_list`, `lab_run` (progress notifications, cancellation, a `max_seconds`
budget), `lab_report` and `lab_forward`, all calling these functions. A
forward signal becomes a paper ticket only through `lab_forward` with
`draft: SYMBOL`, which hands the plan to `paper_order_draft`: trader-budget
sizes it, trader-risk checks it, and the user's own typed yes is still
needed. `--now` (and the MCP server's pinned offline clock) also hides bars
that were not complete at that time, so a saved file replays as it was seen
then.
