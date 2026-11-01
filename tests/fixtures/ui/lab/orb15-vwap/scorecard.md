# orb15-vwap: 15-minute opening-range breakout, above VWAP

**Verdict: REJECT: not good enough for the playbook on this evidence.**

Why: 6 of 8 gates failed: out-of-sample expectancy after charges; out-of-sample profit factor; max drawdown (whole sample); deflated Sharpe ratio; survives 1.5x costs; parameter neighbourhood.

Data: 10 NSE stocks (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, BHARTIARTL, ITC, LT, AXISBANK), 5m bars, 2026-08-05 to 2026-10-30, 60 sessions (the first 1 warm up the indicators). MIS, intraday, flat by the safe flat-by. Spec e51ea86b214ec56a.

## The numbers, after charges and slippage

| | whole sample | out of sample (walk-forward) |
| --- | --- | --- |
| trades | 149 | 65 |
| win rate | 30% | 45% |
| expectancy a trade | -0.463R | -0.039R |
| profit factor | 0.50 | 0.82 |
| net P&L | -₹13,830.62 | -₹1,992.19 |
| charges paid | ₹7,281.48 | ₹3,114.90 |
| max drawdown | 14.80% of capital | 4.14% of capital |
| Sharpe (annualised) | -6.61 | -1.61 |

R is a trade's net result over the rupees it risked at the stop. Expectancy is the average R per trade: +0.10R means about ₹100.00 on a ₹1,000.00 risk, before luck.

## Gates (written in the spec before the run)

| gate | needs | got | |
| --- | --- | --- | --- |
| enough out-of-sample trades | >= 30 | 65 | pass |
| out-of-sample expectancy after charges | > 0R | -0.039R | **FAIL** |
| out-of-sample profit factor | >= 1.2 | 0.819 | **FAIL** |
| max drawdown (whole sample) | <= 10% of capital | 14.80% | **FAIL** |
| deflated Sharpe ratio | >= 0.95 | 0.0001 | **FAIL** |
| probability of backtest overfitting | < 0.5 | 0.2143 | pass |
| survives 1.5x costs | out-of-sample expectancy > 0R | -0.202R | **FAIL** |
| parameter neighbourhood | >= 50% of nearby grid points make money | 0% | **FAIL** |

## Robustness

- Walk-forward: 3 windows of 20 sessions to choose the parameters, each scored on the next 10. Out of sample: 65 trades, -0.039R a trade, -₹1,992.19 net.
  - 2026-09-03 to 2026-09-17, chose or_minutes=30, target_r=3: 19 trades, -0.181R
  - 2026-09-18 to 2026-10-01, chose or_minutes=30, target_r=3: 23 trades, -0.245R
  - 2026-10-05 to 2026-10-16, chose or_minutes=30, target_r=3: 23 trades, +0.286R
- Parameter grid: 6 points; 0% of them make money after charges, 0% of the 6 around the spec's point (median -0.384R there). A strategy that works only at one point is fitted to noise.
- Monte Carlo (1000 runs, seed 16, out-of-sample trades): the same trades in another order draw down ₹3,786.04 at the median and ₹5,361.46 in the worst 5% (seen: ₹4,490.09). Drawn with replacement, the total lands between -₹6,771.68 and ₹2,807.67 nine times in ten; 75% of draws lose money.
- Deflated Sharpe ratio 0.00 (Sharpe a session -0.417 over 59 sessions, 6 trials counted; luck alone would show about 0.110). It is the probability the Sharpe is above zero after allowing for the trials, skew and fat tails.
- Probability of backtest overfitting 21% (70 splits of 8 blocks): how often the grid's best in-sample point was no better than the median out of sample.
- Costs: out-of-sample expectancy -0.039R at 1x, -0.202R at 1.5x, -0.313R at 2x.
- By India VIX the day before: VIX 13 to 16: 52 trades, -0.161R; VIX 16 to 20: 12 trades, -0.408R; VIX under 13: 85 trades, -0.655R.
- By NIFTY 50 trend the day before: NIFTY ranging: 24 trades, -0.428R; NIFTY trending down: 89 trades, -0.297R; NIFTY trending up: 36 trades, -0.897R.
- Signals not taken: already traded this stock today 22, max trades a day 88, too small to size (cash) 62, too small to size (min order) 2.
- Paper forward record: 0 trades, +0.000R a trade, ₹0.00 net.

## What this does NOT prove

- It does not predict the future. It shows how these exact rules would have traded 60 sessions (2026-08-05 to 2026-10-30) of 5-minute bars, with the costs modelled below.
- 60 sessions is a short sample: one or two market moods. A few trades either way change the numbers a lot.
- Out of sample here means later bars of the SAME history the parameters were not chosen on. It is not the future; a paper forward test is the next real check.
- The universe (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, BHARTIARTL, ITC, LT, AXISBANK) is today's liquid names, picked after the fact: stocks that fell out of favour are missing (survivorship bias).
- Fills are a model: the next bar's open plus 3 bps (at least 1 tick), stops at the stop less the same. Real fills meet spreads, queues, circuit limits and your broker's own square-off.
- Charges are a typical discount broker's (brokerage, STT, exchange, SEBI, stamp duty, GST). Check yours. Income tax on trading profits is not included.
- Yahoo Finance bars are free and unofficial: they can lag, skip bars and miss the closing auction.
- Sizes stay inside a ₹1,00,000.00 capital with ₹1,000.00 max loss a trade; much bigger orders would move the price.
- A pass on every gate is evidence that survived some tests, not a guarantee. Most strategies that pass a backtest still fail live.

Education and tooling, not financial advice. Nothing here placed an order.
