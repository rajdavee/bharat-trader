# nifty-expiry-iron-fly: NIFTY expiry-day iron fly

**Verdict: REJECT: not good enough for the playbook on this evidence.**

Why: 2 of 8 gates failed: out-of-sample profit factor; deflated Sharpe ratio.

Data: NIFTY end-of-day option chains (NSE F&O bhavcopy settle prices), 2024-01-01 to 2026-10-30, 703 sessions, archive 2024-01-01 to 2026-10-30 (703 EOD chains). Rules: iron-fly the ATM strike on NIFTY weekly expiries, 1 lot; wings 200 points beyond the shorts; enter at the open on expiry day; else held to cash settlement. Capital ₹5,00,000.00. Spec 4709a8071c44abc6.

## The numbers, after charges and slippage

| | whole sample | out of sample (walk-forward) |
| --- | --- | --- |
| trades | 148 | 114 |
| win rate | 57% | 55% |
| expectancy a trade | +0.003R | +0.002R |
| profit factor | 1.18 | 1.14 |
| net P&L | ₹40,610.55 | ₹31,791.00 |
| charges paid | ₹16,250.61 | ₹12,508.10 |
| max drawdown | 6.61% of capital | 8.15% of capital |
| Sharpe (annualised) | 0.47 | 0.37 |

## Options numbers (after charges and slippage)

| | whole sample | out of sample |
| --- | --- | --- |
| positions | 148 | 114 |
| average margin blocked | ₹1,19,303.06 | ₹1,29,404.18 |
| return on margin, total | 34.04% | 24.57% |
| return on margin, a year | 12.20% | 11.46% |
| return on margin, a trade | 0.25% | 0.20% |
| worst week (marked to market) | -₹12,853.19 | -₹18,960.34 |
| tail loss: 5th percentile trade | -₹6,859.64 | -₹9,073.91 |
| tail loss: 1st percentile trade | -₹8,196.21 | -₹14,234.25 |
| average of the worst 5% of trades | -₹8,265.08 | -₹13,583.15 |
| max consecutive losses | 5 | 4 |
| time in drawdown | 82.36% | 87.22% |
| max drawdown, marked to market | 6.61% of capital | 8.15% of capital |
| expiries with a short strike breached | 41.22% | 43.86% |

R for an options trade is its net result over the margin it blocked (the SPAN-style estimate, the larger of the entry day's and the expiry day's): the expectancy is the average return on margin a trade. Exits: expiry settlement 148.

## Gates (written in the spec before the run)

| gate | needs | got | |
| --- | --- | --- | --- |
| enough out-of-sample trades | >= 30 | 114 | pass |
| out-of-sample expectancy after charges | > 0R | 0.002R | pass |
| out-of-sample profit factor | >= 1.2 | 1.144 | **FAIL** |
| max drawdown (whole sample) | <= 10% of capital | 6.61% | pass |
| deflated Sharpe ratio | >= 0.95 | 0.667 | **FAIL** |
| probability of backtest overfitting | < 0.5 | 0.2571 | pass |
| survives 1.5x costs | out-of-sample expectancy > 0R | 0.001R | pass |
| parameter neighbourhood | >= 50% of nearby grid points make money | 100% | pass |

## Robustness

- Walk-forward: 9 windows of 120 sessions to choose the parameters, each scored on the next 60. Out of sample: 114 trades, +0.002R a trade, ₹31,791.00 net.
  - 2024-06-26 to 2024-09-19, chose wing=300: 13 trades, +0.002R
  - 2024-09-20 to 2024-12-17, chose wing=300: 12 trades, -0.006R
  - 2024-12-18 to 2025-03-12, chose wing=100: 12 trades, +0.001R
  - 2025-03-13 to 2025-06-12, chose wing=300: 14 trades, -0.001R
  - 2025-06-13 to 2025-09-08, chose wing=300: 12 trades, -0.009R
  - 2025-09-09 to 2025-12-04, chose wing=100: 13 trades, -0.002R
  - 2025-12-05 to 2026-03-02, chose wing=300: 13 trades, -0.003R
  - 2026-03-04 to 2026-06-03, chose wing=300: 13 trades, +0.019R
  - 2026-06-04 to 2026-08-27, chose wing=300: 12 trades, +0.016R
- Parameter grid: 4 points; 100% of them make money after charges, 100% of the 3 around the spec's point (median +0.003R there). A strategy that works only at one point is fitted to noise.
- Monte Carlo (1000 runs, seed 16, out-of-sample trades): the same trades in another order draw down ₹49,798.15 at the median and ₹79,578.05 in the worst 5% (seen: ₹40,733.60). Drawn with replacement, the total lands between -₹71,903.52 and ₹1,22,391.15 nine times in ten; 30% of draws lose money.
- Deflated Sharpe ratio 0.67 (Sharpe a session 0.029 over 703 sessions, 4 trials counted; luck alone would show about 0.013). It is the probability the Sharpe is above zero after allowing for the trials, skew and fat tails.
- Probability of backtest overfitting 26% (70 splits of 8 blocks): how often the grid's best in-sample point was no better than the median out of sample.
- Costs: out-of-sample expectancy +0.002R at 1x, +0.001R at 1.5x, -0.001R at 2x.
- By India VIX the day before: VIX 13 to 16: 63 trades, +0.001R; VIX 16 to 20: 23 trades, +0.012R; VIX 20 and up: 9 trades, +0.021R; VIX under 13: 53 trades, -0.003R.
- By NIFTY 50 trend the day before: NIFTY ranging: 24 trades, -0.001R; NIFTY trending down: 50 trades, +0.002R; NIFTY trending up: 62 trades, +0.001R; trend unknown: 12 trades, +0.020R.
- By IV rank on the entry day: IV rank 33 to 66: 27 trades, +0.007R; IV rank 66 and up: 6 trades, +0.040R; IV rank under 33: 89 trades, -0.002R; IV rank unknown: 26 trades, +0.005R.
- By market regime (NIFTY 50 trend or range x India VIX bucket x expiry flag (trader-regime), as of the close before each signal day; R a trade with a 95% interval; under 10 trades is too few to judge):
  - range · low vol · expiry day: 23 trades, -0.002R (-0.01R to 0.01R)
  - trend · high vol · expiry day: 19 trades, +0.008R (-0.01R to 0.02R)
  - range · high vol · expiry day: 17 trades, -0.003R (-0.02R to 0.01R)
  - trend · low vol · expiry day: 15 trades, +0.001R (-0.01R to 0.02R)
  - range · normal vol · expiry day: 14 trades, +0.008R (-0.01R to 0.03R)
  - trend · normal vol · expiry day: 12 trades, -0.005R (-0.02R to 0.01R)
  - unknown · unknown vol · expiry unknown: 10 trades, +0.021R (0.00R to 0.04R)
  - range · high vol · expiry unknown: 6 trades, -0.001R (-0.04R to 0.04R), too few to judge
  - range · low vol · expiry unknown: 5 trades, -0.012R (-0.04R to 0.02R), too few to judge
  - trend · low vol · expiry unknown: 5 trades, +0.003R (-0.03R to 0.03R), too few to judge
  - trend · normal vol · expiry unknown: 5 trades, -0.006R (-0.02R to 0.01R), too few to judge
  - range · normal vol · no expiry: 3 trades, +0.014R (-0.04R to 0.07R), too few to judge
  - trend · high vol · no expiry: 3 trades, +0.003R (-0.05R to 0.06R), too few to judge
  - range · low vol · no expiry: 2 trades, -0.017R (-0.08R to 0.04R), too few to judge
  - range · unknown vol · expiry unknown: 2 trades, +0.016R (-0.05R to 0.08R), too few to judge
  - trend · high vol · day before expiry: 2 trades, +0.062R (0.00R to 0.12R), too few to judge
  - trend · low vol · no expiry: 2 trades, -0.025R (-0.07R to 0.02R), too few to judge
  - range · normal vol · expiry unknown: 1 trades, +0.019R, too few to judge
  - trend · high vol · expiry unknown: 1 trades, +0.009R, too few to judge
  - trend · normal vol · no expiry: 1 trades, -0.012R, too few to judge
  - The market now (2026-10-30): NIFTY: no clear trend (ADX 33), high vol (VIX 15.3, 71st pct).
- Signals not taken: no legs: no chain for 2025-09-25 on 2025-09-24 1.
- Paper forward record: 0 trades, +0.000R a trade, ₹0.00 net.

## What this does NOT prove

- It does not predict the future. It shows how these exact rules would have traded 703 sessions (2024-01-01 to 2026-10-30) of NIFTY end-of-day option chains, with the costs modelled below.
- End-of-day only: entries and exits happen at a session's settle (or open) prices. A stop is checked once a day at the close, so the intraday loss before it could be much larger; there are no adjustments, rolls or hedges during the day.
- Strikes that did not trade on a day are priced by Black-76 at that day's smile IV (flagged), and strike selection only sees strikes that traded: deep strikes that never traded are invisible (survivorship of strikes).
- Fills are a model: the settle price less 2% of the premium and 1 tick a leg. Real spreads on far strikes and on expiry day are often wider, and a large order moves them.
- Margin is a SPAN-style estimate (NSE's 2026 scan on today's parameters), not the margin a broker blocked on those days; return on margin moves with it.
- Short options carry tail risk that a couple of years may not show: one gap like June 2024's election day can exceed many months of premium.
- Charges are a typical discount broker's with the STT rates in force on each trade date. Income tax is not included.
- A pass on every gate is evidence that survived some tests, not a guarantee. Most strategies that pass a backtest still fail live.

Education and tooling, not financial advice. Nothing here placed an order.
