# breakout20-swing: 20-day breakout swing

**Verdict: REJECT: not good enough for the playbook on this evidence.**

Why: 7 of 8 gates failed: out-of-sample expectancy after charges; out-of-sample profit factor; max drawdown (whole sample); deflated Sharpe ratio; probability of backtest overfitting; survives 1.5x costs; parameter neighbourhood.

Data: 10 NSE stocks (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, BHARTIARTL, ITC, LT, AXISBANK), 1d bars, 2024-10-30 to 2026-10-30, 496 sessions (the first 41 warm up the indicators). CNC, delivery. Spec 34d92273443526c4.

## The numbers, after charges and slippage

| | whole sample | out of sample (walk-forward) |
| --- | --- | --- |
| trades | 77 | 50 |
| win rate | 25% | 18% |
| expectancy a trade | -0.326R | -0.148R |
| profit factor | 0.62 | 0.81 |
| net P&L | -₹16,484.65 | -₹5,457.75 |
| charges paid | ₹4,899.85 | ₹3,281.59 |
| max drawdown | 17.44% of capital | 14.69% of capital |
| Sharpe (annualised) | -0.96 | -0.35 |

R is a trade's net result over the rupees it risked at the stop. Expectancy is the average R per trade: +0.10R means about ₹100.00 on a ₹1,000.00 risk, before luck.

## Gates (written in the spec before the run)

| gate | needs | got | |
| --- | --- | --- | --- |
| enough out-of-sample trades | >= 30 | 50 | pass |
| out-of-sample expectancy after charges | > 0R | -0.148R | **FAIL** |
| out-of-sample profit factor | >= 1.2 | 0.805 | **FAIL** |
| max drawdown (whole sample) | <= 10% of capital | 17.44% | **FAIL** |
| deflated Sharpe ratio | >= 0.95 | 0.0723 | **FAIL** |
| probability of backtest overfitting | < 0.5 | 0.8714 | **FAIL** |
| survives 1.5x costs | out-of-sample expectancy > 0R | -0.243R | **FAIL** |
| parameter neighbourhood | >= 50% of nearby grid points make money | 0% | **FAIL** |

## Robustness

- Walk-forward: 8 windows of 120 sessions to choose the parameters, each scored on the next 40. Out of sample: 50 trades, -0.148R a trade, -₹5,457.75 net.
  - 2025-06-25 to 2025-08-20, chose lookback=10, stop_atr=3: 11 trades, -0.507R
  - 2025-08-21 to 2025-10-17, chose lookback=10, stop_atr=2: 7 trades, +1.319R
  - 2025-10-20 to 2025-12-16, chose lookback=40, stop_atr=1.5: 7 trades, -0.338R
  - 2025-12-17 to 2026-02-12, chose lookback=20, stop_atr=1.5: 4 trades, +1.621R
  - 2026-02-13 to 2026-04-16, chose lookback=20, stop_atr=1.5: 6 trades, -1.110R
  - 2026-04-17 to 2026-06-15, chose lookback=20, stop_atr=1.5: 10 trades, -0.625R
  - 2026-06-16 to 2026-08-11, chose lookback=40, stop_atr=3: 5 trades, -0.448R
  - 2026-08-12 to 2026-10-08, chose lookback=40, stop_atr=3: 0 trades, +0.000R
- Parameter grid: 9 points; 0% of them make money after charges, 0% of the 9 around the spec's point (median -0.299R there). A strategy that works only at one point is fitted to noise.
- Monte Carlo (1000 runs, seed 16, out-of-sample trades): the same trades in another order draw down ₹13,988.64 at the median and ₹19,986.39 in the worst 5% (seen: ₹14,692.52). Drawn with replacement, the total lands between -₹24,637.45 and ₹15,029.23 nine times in ten; 73% of draws lose money.
- Deflated Sharpe ratio 0.07 (Sharpe a session -0.061 over 455 sessions, 9 trials counted; luck alone would show about 0.022). It is the probability the Sharpe is above zero after allowing for the trials, skew and fat tails.
- Probability of backtest overfitting 87% (70 splits of 8 blocks): how often the grid's best in-sample point was no better than the median out of sample.
- Costs: out-of-sample expectancy -0.148R at 1x, -0.243R at 1.5x, -0.297R at 2x.
- By India VIX the day before: VIX 13 to 16: 30 trades, -0.435R; VIX 16 to 20: 10 trades, -0.814R; VIX 20 and up: 8 trades, -0.601R; VIX under 13: 29 trades, +0.030R.
- By NIFTY 50 trend the day before: NIFTY ranging: 13 trades, +0.555R; NIFTY trending down: 30 trades, -0.557R; NIFTY trending up: 31 trades, -0.400R; trend unknown: 3 trades, -1.069R.
- Signals not taken: too small to size (cash) 31.
- Paper forward record: 0 trades, +0.000R a trade, ₹0.00 net.

## What this does NOT prove

- It does not predict the future. It shows how these exact rules would have traded 496 sessions (2024-10-30 to 2026-10-30) of daily bars, with the costs modelled below.
- Out of sample here means later bars of the SAME history the parameters were not chosen on. It is not the future; a paper forward test is the next real check.
- The universe (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, BHARTIARTL, ITC, LT, AXISBANK) is today's liquid names, picked after the fact: stocks that fell out of favour are missing (survivorship bias).
- Fills are a model: the next bar's open plus 3 bps (at least 1 tick), stops at the stop less the same. Real fills meet spreads, queues, circuit limits and your broker's own square-off.
- Charges are a typical discount broker's (brokerage, STT, exchange, SEBI, stamp duty, GST). Check yours. Income tax on trading profits is not included.
- Yahoo Finance bars are free and unofficial: they can lag, skip bars and miss the closing auction.
- Sizes stay inside a ₹1,00,000.00 capital with ₹1,000.00 max loss a trade; much bigger orders would move the price.
- A pass on every gate is evidence that survived some tests, not a guarantee. Most strategies that pass a backtest still fail live.

Education and tooling, not financial advice. Nothing here placed an order.
