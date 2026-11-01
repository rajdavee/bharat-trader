# Edge hunt: the research brief and the pre-registration

This page is written **before** any edge-hunt run. It says what will be
tested, on which data, with which parameters, how many trials, which gates,
and what kills each idea, so that the later runs (issue #74) cannot move the
goalposts after seeing the numbers. Each idea is a file in
[`trader-lab/library/edge-hunt/`](../../trader-lab/library/edge-hunt/); the
schema is at the end of this page and `tests/test_edge_hunt.py` checks every
file against it.

Registered on 2026-11-01. 14 hypotheses, 184 grid points in all.

Education and research tooling, not financial advice. Nothing here is a
promise of profit; most of these ideas are expected to fail, and the brief
says so with a number.

## The rules of this programme

1. **One registration, one run.** A hypothesis runs exactly as its file
   says: the universe, the dates, the grid, the gates. A change after the
   first run is a new hypothesis with a new id, and its trials are added to
   the count of the family it came from.
2. **The trial count is honest.** Each file's `trial_budget` is its grid
   size. `prior_trials` counts what this repo already tried on the same kind
   of rule (the five options library specs, the 527 discovery trials, the
   intraday library), so the deflated Sharpe ratio charges for them too.
3. **Train, validate, then open the holdout once.** Parameters are chosen
   on the train window only (walk-forward inside it is fine). The validation
   window screens and feeds the deflated Sharpe ratio and the probability of
   backtest overfitting. The holdout is opened once, for the single
   configuration that survived validation, and its numbers are published
   whatever they are. Positions are closed at each window's last session
   (purged), and an embargo of at least 14 calendar days sits between the
   windows.
4. **The lab's gates, plus a span.** Every hypothesis carries trader-lab's
   default gates ([lab.md](../lab.md#gates)): 30 out-of-sample trades,
   positive expectancy after charges, profit factor 1.2, drawdown 10% of
   capital, deflated Sharpe 0.95, PBO below 0.5, survival at 1.5x costs,
   half the grid neighbourhood positive. Overrides are written in the file
   with a reason (a long-only stock portfolio is judged on its return over
   its own universe, not on a 10% drawdown of capital). The minimum span is
   10 years where the data exists; a shorter span is declared as an
   exception and caps the verdict at WATCH.
5. **Programme-level false discoveries.** Fourteen hypotheses are fourteen
   chances to be fooled. The holdout p-values (a stationary bootstrap of the
   primary metric, as in [discovery.md](../discovery.md)) are judged
   together with Benjamini-Hochberg at q = 0.10 across all registered
   hypotheses, not one at a time.
6. **Survivors are only candidates.** A pass is WATCH: it goes to the paper
   forward test (#74, #76) with its own capital bucket. Every paper or live
   order still needs trader-risk PASS and the user's typed yes. Nothing here
   trades on its own.
7. **Publish the failures.** Every scorecard, REJECTs included, goes into
   `docs/research/edge-hunt-results.md` (#74).

## What we already know

**This repo's own evidence** (on real NSE data, after 2026 charges):

- All six equity library strategies (opening-range breakout, VWAP reclaim,
  gap fade, previous-day-high breakout, RSI(2), 20-day breakout) are
  REJECTED. Intraday edges are smaller than the charges ([lab.md](../lab.md)).
- All five options library strategies are REJECTED on 703 sessions of F&O
  bhavcopies (Jan 2024 to Oct 2026). The weekly 16-delta strangle wins 82%
  of the time and still loses out of sample: one close past the stop
  (7 April 2025) erased months of premium. The expiry-day iron fly came
  closest, 6 of 8 gates, failing the out-of-sample profit factor and the
  deflated Sharpe ratio.
- Strategy discovery ran 527 daily rule trials on about 2.3 years and found
  0 finalists; White's Reality Check p = 0.84 ([discovery.md](../discovery.md)).

**SEBI's studies of individual F&O traders** (the people this pack is for):

| study | period | what it found |
| --- | --- | --- |
| SEBI, Jan 2023 | FY22 | 89% of individual F&O traders lost money, average loss about ₹1.1 lakh. |
| SEBI press release 37/2024, 23 Sep 2024 | FY22 to FY24 | 93% of over 1 crore individuals lost; aggregate losses over ₹1.8 lakh crore including costs; 91.5% of option traders lost against 60% in futures. In FY24 proprietary traders and FPIs made gross profits of about ₹33,000 crore and ₹28,000 crore, 96% and 97% of it from algorithmic trading, while individuals lost about ₹61,000 crore before costs. |
| SEBI, 7 Jul 2025 | FY25 (and Dec 2024 to May 2025 after the Nov 2024 measures) | 91% lost; net losses up 41% to about ₹1,05,603 crore; unique traders down 20% to about 67 lakh after the measures. |
| SEBI, Aug 2026 (as summarised on Zerodha's TradingQnA; primary PDF not read) | FY25 to FY26 | Option buyers only: about 93% of traders, about 90% lost, average loss ₹1.3 lakh. Majorly option sellers: about 2% of traders, about 44% lost, but the average loss of a losing seller was ₹51.7 lakh. Only 0.5% of traders active from FY22 to FY26 were profitable in all five years; 15.4% of trader-quarters were profitable. |

Read plainly: **buying options loses often and a little; selling options
loses rarely and a lot**. Both lose on average for individuals. The money is
made by algorithmic proprietary desks and FPIs, mostly with speed, cost and
inventory advantages a retail account does not have. A retail edge, if any,
has to come from patience (risk premia held for months), from structure
(rules that are cheap to follow and boring), or from behaviour that large
players cannot or will not trade against. Fast intraday ideas start at a
severe disadvantage.

**Markets change after rules change.** Since 20 Nov 2024 each exchange has
one weekly index expiry (NSE: NIFTY, now on Tuesdays), BANKNIFTY options are
monthly only, contract sizes went to ₹15 to 20 lakh, premiums are collected
upfront, and expiry days carry 2% extra exposure margin. From 1 April 2026
STT on options sales rose from 0.1% to 0.15% of the premium and on futures
sales from 0.02% to 0.05%. Options history before Nov 2024 comes from a
different market; every index-options hypothesis keeps its holdout after
the change (stock options were always monthly and changed less).

**Published anomalies decay.** McLean and Pontiff (2016) found US anomaly
returns 26% lower out of sample and 58% lower after publication; Jacobs and
Müller (2020) found the reliable post-publication decline mostly in the US,
not in other countries. Indian evidence is thinner, older and often in
journals with weak review. The priors below assume decay.

## 2026 costs, computed with the pack's own charges

`round_trip_charges` and `fno_round_trip_charges` from trader-budget (the
dated table effective 1 Apr 2026), NIFTY at 23,053.10 and BANKNIFTY at
54,686.95 (closes of 30 Oct 2026), lots of 65 and 30. Slippage: 1 bp a side
on index futures, 3 bp a side on stocks and stock futures (the lab's
default), and 2% of the premium plus a tick a leg on options (the lab's
options default).

| trade | charges | + slippage | round trip |
| --- | --- | --- | --- |
| NIFTY future, 1 lot (₹14.98 lakh) | ₹894.65 (0.060%) | 0.020% | **0.080%** of notional |
| BANKNIFTY future, 1 lot (₹16.41 lakh) | ₹975.04 (0.059%) | 0.020% | **0.079%** |
| stock future, ₹7 lakh | ₹443.08 (0.063%) | 0.060% | **0.123%** |
| stocks, delivery (CNC), ₹1 lakh | ₹269.68 (0.270%) | 0.060% | **0.330%** |
| stocks, intraday (MIS), ₹1 lakh | ₹82.68 (0.083%) | 0.060% | **0.143%** |
| long ATM NIFTY option, 120 to 150 | ₹69.45 | ₹357.50 | **5.5%** of the premium paid |
| NIFTY iron condor, 40/40 short, 15/15 long, expiring worthless | ₹105.26 | ₹156.00 | **8.0%** of the credit |
| NIFTY expiry iron fly, 60/60 short, 8/8 long, closed at 30/30 and 1/1 | ₹206.30 | ₹287.30 | **7.3%** of the credit |
| stock straddle, 40+40 to 46+46, lot 500 | ₹200.76 | ₹1,820.00 | **5.1%** of the premium paid |

What that does to a year (round trips a year x the cost; turnover assumed
from each rule's design):

| rule | drag a year |
| --- | --- |
| cross-sectional momentum, monthly, about 35% one-way turnover a month (CNC) | 1.4% of capital |
| momentum, semi-annual like NSE's index, about 60% turnover a rebalance | 0.4% |
| low volatility, semi-annual, about 25% turnover a rebalance | 0.2% |
| short-term reversal, weekly, about 80% turnover a week (CNC) | **13.7%** |
| post-earnings drift, capital cycling about 6 times a year (CNC) | 2.0% |
| index futures trend, about 8 flips and 12 rolls a year | 1.6% of notional |
| index futures intraday momentum, trading 60% of sessions | **12.0%** |
| index futures overnight hold, every session | **19.9%** |
| index futures turn of the month, 12 a year | 1.0% |
| stock futures pairs, 2 legs, about 20 round trips a year | 4.9% |

The numbers were produced with the helper functions themselves; the test
`tests/test_edge_hunt.py` recomputes the futures and CNC round trips so this
table cannot drift from the code.

## The data: what we have and what the runs wait for

| data | today in trader-archive | needed from |
| --- | --- | --- |
| EOD equity bhavcopies | the archive reads UDiFF (from 2024) and legacy files; the maintainer's local backfill covers 2024-01-01 to 2026-10-30 | #71: backfill to 2010 with corporate-action adjustment |
| EOD F&O bhavcopies (futures and options settle, OI, lots) | 703 sessions, 2024-01-01 to 2026-10-30 | #71: legacy files back to 2010 |
| index closes and India VIX | 2024 to 2026 locally, older on request | #71 |
| continuous NIFTY/BANKNIFTY futures | not built | #71 (roll rules, back-adjusted series, roll log) |
| point-in-time index membership | not available: NIFTY 50 of 2026 applied to all history | #71 (survivorship-free cross sections) |
| results (board meeting) dates history | not available | #71 |
| minute bars, index and stocks | Upstox v3 candles from Jan 2022 with a token; Yahoo 60 sessions of 5m | available now (token) |
| minute bars, expired futures and options | not verified from any free source | #71 research, else #76 forward capture |
| fundamentals (ROE, leverage, earnings stability) | not available | not planned: quality is described, not registered |
| engine: futures, cross-sectional portfolios, pairs, events | the lab backtests single-stock rules and EOD options only | #72 |
| engine: VRP timing, event straddles, intraday expiry plays | | #73 |

## The candidate edges

Each entry gives the mechanism, the evidence, the instrument and holding,
turnover and drag, the data, what would falsify it, and a prior: our honest
probability that it passes every gate **and** the sealed holdout. A prior
of 0.10 means we expect nine in ten such ideas to die, which is roughly the
point of the exercise. Feasibility is the chance the data and engine needed
are ready and trustworthy in this sprint. The run order is prior x
feasibility.

### 1. Cross-sectional momentum, monthly (`eh-01-xs-momentum`)

- **Mechanism.** Behavioural: under-reaction to news and slow diffusion,
  then herding; partly a reward for crash risk. Momentum crashes sharply
  when a falling market rebounds (Daniel and Moskowitz 2016).
- **Evidence.** Strong globally (Jegadeesh and Titman 1993; decades of
  replications). India: Sehgal and Balakrishnan (2002) and Sehgal and Jain
  (2011) found continuation at 6 to 12 months; the IIM Ahmedabad factor
  library (Agarwalla, Jacob and Varma, 2013 working paper, updated monthly)
  reported a momentum factor averaging about 21.9% a year from 1994 to 2014,
  the strongest Indian factor. NSE's Nifty200 Momentum 30 (launched Aug 2020,
  base 1 Apr 2005; score = the average of the z-scores of 6- and 12-month
  returns divided by volatility; weight = free-float cap x score;
  semi-annual) is reported at about 4 percentage points a year over its
  parent since 2005, but most of that span is a back-test, and from its peak
  on 27 Sep 2024 it fell about 32% by Mar 2025, worse than its parent.
  Large index flows around its rebalances (about ₹16,000 crore reported in
  Dec 2025) make the public version crowded.
- **Instrument.** NSE stocks, delivery (CNC), long only. Universe: NIFTY 200
  point-in-time. Benchmark: the equal-weighted point-in-time universe.
- **Holding.** One or three months. Drag about 1.4% a year (monthly) or less.
- **Data.** 10+ years of adjusted EOD prices and PIT membership: #71.
  Engine: cross-sectional portfolios, #72.
- **Falsified by.** Holdout active return over the equal-weighted universe
  at or below zero after charges; or the gain coming from one or two months.
- **Prior 0.30, feasibility 0.50.** The best-documented anomaly here, but a
  long-only retail version only captures the winner side, and 2024-25 shows
  the crash risk.

### 2. Low volatility (`eh-02-low-volatility`)

- **Mechanism.** Behavioural and structural: lottery preference and
  benchmark-bound managers who cannot lever up, so low-risk stocks are
  underpriced per unit of risk (Frazzini and Pedersen 2014; Blitz and van
  Vliet 2007; Ang, Hodrick, Xing and Zhang 2006).
- **Evidence.** Global, robust and slower to decay than most. India: NSE
  Indices' factor research reports the low-volatility index ahead of NIFTY
  200 in about 96% of rolling windows since 2005 with about 5% a year of
  alpha (again mostly back-tested before launch); Indian studies on NSE data
  (2010 to 2020) find the effect at 5 to 10 year horizons and weak at 6
  months to 3 years.
- **Quality** (high ROE, low leverage, stable earnings; Asness, Frazzini and
  Pedersen 2019) overlaps heavily with low volatility in India but needs
  dated fundamentals we do not have. It is **not registered**; low volatility
  is the testable proxy.
- **Instrument.** Stocks, CNC, long only, NIFTY 100 or 200 PIT.
- **Holding.** A quarter or half a year. Drag about 0.2% a year.
- **Data.** #71 (long history, PIT). Engine #72.
- **Falsified by.** Holdout Sharpe ratio not above the universe's, or the
  drawdown no smaller than the universe's.
- **Prior 0.25, feasibility 0.50.** Its edge is risk-adjusted, not raw
  return; judged on Sharpe and drawdown relative to the universe.

### 3. Trend on index futures (`eh-03-index-trend`)

- **Mechanism.** Behavioural (under-reaction then over-reaction) and
  hedging demand; trend pays most in long bad markets.
- **Evidence.** Moskowitz, Ooi and Pedersen (2012): the past 12-month
  return predicts the next month across 58 futures, 1985 to 2009; Hurst,
  Ooi and Pedersen (2017) extend it over a century. The Sharpe comes mostly
  from diversifying across many markets; one equity index has a much weaker
  signal. India: trend rules on NIFTY closes (2005 to 2012) studied in an
  Indian finance journal, with mixed results; no strong peer-reviewed
  Indian futures study found.
- **Instrument.** NIFTY and BANKNIFTY near-month futures, continuous
  series, long/flat or long/short, monthly decisions. Notional = capital
  (no leverage).
- **Holding.** Weeks to months. About 8 flips and 12 rolls a year: 1.6%.
- **Data.** NIFTY and BANKNIFTY futures EOD back to 2005 and a roll log:
  #71. Engine: futures, #72.
- **Falsified by.** Holdout Sharpe no better than buy-and-hold, or a
  drawdown no smaller than buy-and-hold's.
- **Prior 0.15, feasibility 0.70.** In India's rising market long/flat is
  mostly "buy and hold with smaller crashes"; the short side has fought
  drift.

### 4. Turn of the month (`eh-04-turn-of-month`)

- **Mechanism.** Structural: salaries, SIP inflows (large in India,
  mostly in the first days of the month), fund flows and window dressing.
- **Evidence.** Ariel (1987); Lakonishok and Smidt (1988); McConnell and Xu
  (2008) found the effect in 30 of 35 countries. India: Karmakar and
  Chakraborty (2003) found it; Archana, Safeer and Kevin (2014) found it
  "minimally visible, not significant"; a 2023 paper in the Review of
  Finance and Banking revisits it. Mixed.
- **Instrument.** NIFTY and BANKNIFTY futures, long from the close a day or
  two before month end to a few sessions into the new month; flat otherwise.
- **Holding.** 4 to 6 sessions, 12 times a year: drag about 1%.
- **Data.** Index futures EOD (#71) or the index itself as a proxy now.
- **Falsified by.** Holdout mean return in the window not above the mean of
  the other sessions, or net of charges at or below zero.
- **Prior 0.12, feasibility 0.80.** Cheap to trade, few knobs, but small and
  noisy: 12 trades a year means a long sample is needed to see anything.

### 5. Volatility risk premium, timed, defined risk (`eh-05-vrp-condor`)

- **Mechanism.** Risk premium: buyers pay for crash insurance (Carr and Wu
  2009; Bakshi and Kapadia 2003). The premium is the reward for the crash
  the seller carries.
- **Evidence.** Global and persistent for index options. India: India VIX
  has on average sat above later NIFTY realised volatility; a 2025 study
  using NIFTY and India VIX from Apr 2021 to Jul 2025 reports a positive,
  significant variance risk premium. Jain, Varma and Agarwalla (Journal of
  Futures Markets, 2018-19) find Indian single-stock implied volatility far
  above realised. But this repo's own library tests of strangles and
  condors (2024 to 2026) all REJECT, and SEBI's sellers lose rarely but
  hugely.
- **The twist being tested.** Sell only when the premium is wide (India VIX
  minus the 20-day realised volatility above a threshold), monthly NIFTY
  iron condors (defined risk, so no 7-April-2025 blow-up beyond the wings).
- **Holding.** 3 to 5 weeks, one a month. Charges and slippage about 8% of
  the credit.
- **Data.** F&O EOD now (2024 to 2026); #71 for 2010 onwards. Engine: VRP
  timing filter, #73.
- **Falsified by.** Holdout return on margin at or below zero, or the
  timed version no better than the untimed one (then the filter is noise).
- **Prior 0.11, feasibility 0.80.** The premium is real; whether a retail
  defined-risk structure keeps it after 8% costs and wings is doubtful, and
  our own tests say no so far.

### 6. Post-earnings-announcement drift (`eh-06-pead`)

- **Mechanism.** Behavioural under-reaction to earnings news; limits to
  arbitrage in smaller names (Bernard and Thomas 1989; Chordia et al. 2009
  show most of it sits in illiquid stocks where costs eat it).
- **Evidence.** India: studies on 2002 to 2011 data find drift into the
  third month after a surprise, others find good news priced fast and bad
  news drifting for about a week; journals of moderate review.
- **The surprise.** We have no consensus estimates, so the surprise is the
  stock's 3-session abnormal return around the results date (Brandt,
  Kishore, Santa-Clara and Venkatachalam's "earnings announcement return").
- **Instrument.** Stocks, CNC, long only, NIFTY 500 PIT with a liquidity
  floor (median traded value at least ₹10 crore).
- **Holding.** 20 to 60 sessions. Drag about 2% a year.
- **Data.** Results dates history and PIT membership: #71. Engine: events,
  #72.
- **Falsified by.** Holdout mean abnormal return of the top bucket at or
  below the universe after charges.
- **Prior 0.15, feasibility 0.45.**

### 7. Pre-holiday and expiry week (`eh-07-preholiday-expiry`)

- **Mechanism.** Behavioural (pre-holiday optimism, short covering) and
  structural (expiry-week hedging and pinning).
- **Evidence.** Ariel (1990) found high returns before US holidays; Indian
  evidence is scattered and mixed. Vipul (2005) found expiration-day effects
  in India significant but small. Expiry rules have changed several times
  (Thursday, then Tuesday; one weekly index since Nov 2024).
- **Instrument.** NIFTY and BANKNIFTY futures, long a session or two before
  an NSE holiday, or the expiry week.
- **Holding.** 1 to 4 sessions, about 12 to 15 a year a variant (the
  monthly expiry week only, not every weekly one): drag about 1%.
- **Data.** Index futures EOD and the dated holiday and expiry calendars
  (the pack's holiday tables, #71 for history).
- **Falsified by.** Holdout mean return in the window at or below other
  sessions' mean, or net at or below zero.
- **Prior 0.06, feasibility 0.80.**

### 8. Results-day volatility crush, defined risk (`eh-08-event-crush`)

- **Mechanism.** Risk premium on event risk plus retail demand for lottery
  payoffs before results.
- **Evidence.** Dubinsky, Johannes, Kaeck and Seeger (2019) measure the
  priced jump on US announcement days; Indian single-stock options carry a
  large implied-over-realised premium (Jain, Varma and Agarwalla). No
  Indian study of results-day straddles was found.
- **Instrument.** Short iron fly on F&O stocks: sold at the close before the
  results, bought back one or two sessions later. Stock option wings are
  thin; slippage is the main risk.
- **Holding.** 1 to 2 sessions, a few hundred events a year across the
  universe. Costs about 7 to 10% of the credit, more on illiquid strikes.
- **Data.** Stock options EOD (#71) and results dates (#71). Engine #73.
- **Falsified by.** Holdout return on margin at or below zero, or the
  realised move exceeding the implied move on more than half the events.
- **Prior 0.10, feasibility 0.40.**

### 9. Pre-results volatility run-up, long straddle (`eh-09-event-runup`)

- **Mechanism.** Implied volatility rises into a known event; buying early
  and selling before the announcement collects the rise without carrying
  the event (Gao, Xing and Zhang 2018, JFQA, on US stocks).
- **Evidence.** US only; not tested in India. Theta works against it.
- **Instrument.** Long ATM straddle on F&O stocks, bought 3 to 10 sessions
  before the results date, sold at the close before it.
- **Holding.** 3 to 10 sessions. Costs about 5% of the premium paid,
  mostly slippage.
- **Data.** As 8.
- **Falsified by.** Holdout mean return on premium at or below zero.
- **Prior 0.08, feasibility 0.40.** One of the few option-buying ideas with
  a mechanism; the spread on stock options may eat all of it.

### 10. Short-term reversal (`eh-10-st-reversal`)

- **Mechanism.** Liquidity provision: last week's losers bounce as the
  price pressure that pushed them fades (Jegadeesh 1990; Lehmann 1990).
- **Evidence.** Strong historically, but concentrated in small, illiquid
  stocks and it decays fast; Indian studies find short-horizon reversal and
  intraday reversal within minutes (Tripathi and Aggarwal). Discovery's 527
  daily trials (with RSI(2)-type rules) found nothing on large caps.
- **Instrument.** NIFTY 100 PIT; long-only CNC, or long-short stock futures.
- **Holding.** 5 to 10 sessions. Drag 10 to 14% a year: the gross effect
  must be enormous to survive.
- **Data.** #71 PIT and history; stock futures engine #72.
- **Falsified by.** Holdout net return at or below zero (expected).
- **Prior 0.04, feasibility 0.60.** Registered mostly to show it fails.

### 11. Pairs in stock futures (`eh-11-pairs`)

- **Mechanism.** Relative mispricing between close substitutes mean-reverts.
- **Evidence.** Gatev, Goetzmann and Rouwenhorst (2006) found it profitable
  in the US from 1962; Do and Faff (2010) found it fading after 2002.
  Indian claims of 30%+ a year come from theses and project write-ups
  without serious review. Pairs break when one company's story changes.
- **Instrument.** Same-sector F&O stock futures, cointegrated over a
  formation window; enter at a z-score, exit at the mean, stop at 4.
- **Holding.** Days to weeks. Drag about 5% a year a pair.
- **Data.** Stock futures EOD (#71). Engine: pairs, #72.
- **Falsified by.** Holdout net at or below zero, or more than a third of
  pairs hitting the stop.
- **Prior 0.05, feasibility 0.40.**

### 12. Intraday market momentum (`eh-12-intraday-momentum`)

- **Mechanism.** Late-day trading by hedgers and infrequent rebalancers
  continues the morning's move (Gao, Han, Li and Zhou 2018, JFE: SPY's first
  half-hour return predicts the last half-hour, 1993 to 2013, about 6.7% a
  year).
- **Evidence.** 12 of 16 developed markets in later work; in Asia-Pacific
  mostly China and Japan, absent in Hong Kong and Singapore (Limkriangkrai,
  Chai and Zheng 2023). **No peer-reviewed Indian study was found.**
- **Instrument.** NIFTY and BANKNIFTY futures, the last half-hour
  (15:00 to 15:29), only when the first half-hour moved more than a
  threshold.
- **Holding.** 30 minutes. Trading every session costs about 20% a year of
  notional; trading 60% of them, 12%. The US gross effect was about 6.7%.
- **Data.** Index minute bars from Jan 2022 (Upstox, token); futures minute
  bars need #71 research or a spot-plus-basis proxy. Span about 4.8 years:
  a declared exception to the 10-year rule.
- **Falsified by.** Holdout net at or below zero, or the predictive slope
  insignificant.
- **Prior 0.04, feasibility 0.45.**

### 13. Overnight drift (`eh-13-overnight-drift`)

- **Mechanism.** Overnight returns carry the risk of news while the market
  is shut; intraday returns carry retail and arbitrage flows (Cliff, Cooper
  and Gulen 2008; Lou, Polk and Skouras 2019).
- **Evidence.** Strong in the US. NIFTY: secondary analyses (not peer
  reviewed) put most of NIFTY's points since 2000 in the overnight gap and a
  negative intraday sum. We will measure it ourselves.
- **Instrument.** NIFTY and BANKNIFTY futures, bought in the last minutes,
  sold at the next open.
- **Holding.** Overnight. Every session: about 20% of notional a year in
  charges against a gross overnight return in single digits. Dead on
  arithmetic unless the conditioned versions are very selective.
- **Data.** Futures open and close (EOD bhavcopies, #71).
- **Falsified by.** Holdout net at or below zero (expected).
- **Prior 0.02, feasibility 0.80.** Registered because the claim is popular
  and should be checked after 2026 charges.

### 14. Trend-day option buying (`eh-14-trend-day-buying`)

- **Mechanism.** On a true trend day realised volatility exceeds what the
  morning's premium priced; a cheap-volatility day (low India VIX) breaking
  its opening range is the bet.
- **Evidence.** None credible. SEBI: about 90% of option buyers lose.
  Option buying pays the variance risk premium that idea 5 collects.
- **Instrument.** NIFTY weekly ATM option, bought on an opening-range break,
  out by 15:15.
- **Holding.** Hours. Costs about 4 to 6% of the premium a round trip.
- **Data.** Historical intraday option prices are not available free; the
  train and validation windows use model prices (Black-76 from index minute
  bars and India VIX, flagged as model-priced), and the holdout is the
  **live captured chain** from 16 Nov 2026 (#76): sealed by construction.
- **Falsified by.** Holdout net at or below zero.
- **Prior 0.03, feasibility 0.15.**

### Not registered

- **Futures basis / cash-futures arbitrage.** The basis earns roughly the
  money-market rate (it is what arbitrage funds earn), not an edge, and a
  retail round trip pays CNC STT on both stock legs plus futures STT: about
  0.33% + 0.08% against a monthly carry of about 0.5%. Basis is used as a
  signal input (the futures premium) where a hypothesis needs it.
- **Quality.** No dated fundamentals; see 2.

## The ranked shortlist (the run order for #74)

| rank | id | family | prior | feasibility | prior x feasibility | grid | span (years) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | eh-01-xs-momentum | momentum-xs | 0.30 | 0.50 | 0.150 | 16 | 15.8 |
| 2 | eh-02-low-volatility | low-volatility | 0.25 | 0.50 | 0.125 | 16 | 15.8 |
| 3 | eh-03-index-trend | trend-ts | 0.15 | 0.70 | 0.105 | 12 | 21.8 |
| 4 | eh-04-turn-of-month | calendar | 0.12 | 0.80 | 0.096 | 12 | 21.8 |
| 5 | eh-05-vrp-condor | vrp-selling | 0.11 | 0.80 | 0.088 | 36 | 16.8 |
| 6 | eh-06-pead | pead | 0.15 | 0.45 | 0.068 | 12 | 15.8 |
| 7 | eh-07-preholiday-expiry | calendar | 0.06 | 0.80 | 0.048 | 8 | 21.8 |
| 8 | eh-08-event-crush | event-vol | 0.10 | 0.40 | 0.040 | 8 | 14.8 |
| 9 | eh-09-event-runup | event-vol | 0.08 | 0.40 | 0.032 | 6 | 14.8 |
| 10 | eh-10-st-reversal | reversal | 0.04 | 0.60 | 0.024 | 16 | 15.8 |
| 11 | eh-11-pairs | pairs | 0.05 | 0.40 | 0.020 | 8 | 15.8 |
| 12 | eh-12-intraday-momentum | intraday-momentum | 0.04 | 0.45 | 0.018 | 12 | 4.8 |
| 13 | eh-13-overnight-drift | overnight | 0.02 | 0.80 | 0.016 | 6 | 21.8 |
| 14 | eh-14-trend-day-buying | option-buying | 0.03 | 0.15 | 0.005 | 16 | 5.4 |

If the priors are about right, the expected number of survivors is the sum
of the priors: about 1.5 out of 14, and zero is a quite likely outcome
(the chance all fail, if independent, is about 19%). The long-horizon,
low-turnover, risk-premium ideas lead; fast intraday ideas and option
buying trail. Programme-level FDR (rule 5) applies to whichever survive.

## The pre-registration schema

One JSON file a hypothesis in `trader-lab/library/edge-hunt/<id>.json`,
plus `manifest.json`, which holds the SHA-256 of each file's registered
content (the canonical JSON, keys sorted, without `status` and `outcome`).
The runs may change `status` and fill `outcome`; any other change breaks the
hash, and the test fails. These files are not trader-lab specs (`lab.py
list` does not read the subfolder): #72 and #73 turn each into runnable
specs, and the run must cite the registration hash.

| field | type | rule |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `id` | string | `eh-NN-slug`, the file name |
| `rank` | int | 1..N, unique, ordered by `prior.probability x feasibility.score`, highest first |
| `title`, `hypothesis` | string | the claim, with its direction |
| `family` | enum | momentum-xs, low-volatility, trend-ts, calendar, vrp-selling, pead, event-vol, reversal, pairs, intraday-momentum, overnight, option-buying |
| `mechanism` | object | `kind` (risk-premium, behavioural, structural; a list) and `text` |
| `evidence` | list | at least 2 of `{source, year, market, finding}` |
| `instrument` | object | `segment` (EQ-CNC, EQ-MIS, FUT, OPT, FUT+EQ), `underlying`, `side` |
| `universe` | object | `name`, `point_in_time` (bool), `rule` |
| `holding_period` | string | |
| `turnover` | object | `round_trips_per_year` (number), `charge_drag_pct_per_year` (number, from the table above), `basis` |
| `data` | object | `needs` (list), `available_now` (list), `blocked_by` (issue numbers) |
| `span` | object | `train`, `validation`, `holdout`: `{from, to}` ISO dates, in that order, not overlapping, with at least `embargo_days` calendar days between them |
| `parameters` | object | each name to a list of fixed values; no value may repeat |
| `trial_budget` | int | the product of the parameter list lengths (each point run once), at most 36 |
| `prior_trials` | object | `count` and `basis`: earlier trials of this kind in this repo, counted into the deflated Sharpe ratio |
| `gates` | object | `lab_defaults: true`; `overrides` (only trader-lab's gate names); `extra` (from the documented list below); `min_span_years` (10); `span_exception` (text, required when the span is shorter) |
| `primary_metric` | enum | oos_net_sharpe, oos_active_sharpe, oos_expectancy_r, oos_return_on_margin, oos_mean_event_return |
| `holdout_rule` | object | `opened_once: true` and `pass_if` |
| `kill_criteria` | list | at least 2 |
| `prior` | object | `probability` 0..1 and `reason` |
| `feasibility` | object | `score` 0..1 and `reason` |
| `contamination` | string | what of this data has already been looked at |
| `registered_on` | date | |
| `status` | enum | registered, running, rejected, watch, promoted, withdrawn; registered at first |
| `outcome` | null or object | filled by #74 |

`gates.extra` names: `drawdown_basis` (capital, active, vs_buy_and_hold),
`max_drawdown_vs_benchmark` (a multiple of the benchmark's drawdown),
`min_oos_months`, `min_holdout_trades`, `fdr_q`, `verdict_cap` (WATCH).

## What this brief does not show

- **The priors are judgement**, informed by the sources and this repo's
  earlier failures, not measurements. They are written down so they can be
  scored against the results (a Brier score over the 14 outcomes in #74).
- **Most Indian evidence is old or weakly reviewed**, and the NSE index
  numbers are largely back-tests before launch. Secondary summaries are
  marked as such; we did not read every primary paper in full.
- **Survivorship and look-ahead** are only fixed once #71 gives
  point-in-time membership and dated results; until then a run is refused
  for the cross-sectional ideas.
- **The 2024 to 2026 F&O sessions have been looked at** by the v0.3 options
  library runs. Options holdouts that include them are not virgin data; each
  file says what was seen. Idea 14's holdout is truly unseen (future data).
- **Charges are a typical discount broker's**, income tax is left out, and
  slippage is a model. Bigger accounts move prices more.
- **Passing every gate is evidence, not a guarantee.** The past is not the
  future.

## Sources

Global:

- N. Jegadeesh and S. Titman, "Returns to Buying Winners and Selling
  Losers", *Journal of Finance* 48(1), 1993.
- K. Daniel and T. Moskowitz, "Momentum Crashes", *Journal of Financial
  Economics* 122(2), 2016.
- T. Moskowitz, Y. H. Ooi and L. H. Pedersen, "Time Series Momentum",
  *Journal of Financial Economics* 104(2), 2012 (SSRN 2089463).
- B. Hurst, Y. H. Ooi and L. H. Pedersen, "A Century of Evidence on
  Trend-Following Investing", *Journal of Portfolio Management* 44(1), 2017.
- N. Jegadeesh, "Evidence of Predictable Behavior of Security Returns",
  *Journal of Finance* 45(3), 1990; B. Lehmann, "Fads, Martingales, and
  Market Efficiency", *Quarterly Journal of Economics* 105(1), 1990.
- A. Ang, R. Hodrick, Y. Xing and X. Zhang, "The Cross-Section of
  Volatility and Expected Returns", *Journal of Finance* 61(1), 2006;
  A. Frazzini and L. H. Pedersen, "Betting Against Beta", *Journal of
  Financial Economics* 111(1), 2014; D. Blitz and P. van Vliet, "The
  Volatility Effect", *Journal of Portfolio Management* 34(1), 2007;
  C. Asness, A. Frazzini and L. H. Pedersen, "Quality Minus Junk", *Review
  of Accounting Studies* 24, 2019.
- V. Bernard and J. Thomas, "Post-Earnings-Announcement Drift: Delayed
  Price Response or Risk Premium?", *Journal of Accounting Research* 27,
  1989; T. Chordia, A. Goyal, G. Sadka, R. Sadka and L. Shivakumar,
  "Liquidity and the Post-Earnings-Announcement Drift", *Financial
  Analysts Journal* 65(4), 2009; M. Brandt, R. Kishore, P. Santa-Clara and
  M. Venkatachalam, "Earnings Announcements are Full of Surprises", working
  paper, 2008.
- L. Gao, Y. Han, S. Z. Li and G. Zhou, "Market Intraday Momentum",
  *Journal of Financial Economics* 129(2), 2018; M. Limkriangkrai, D. Chai
  and G. Zheng, "Market Intraday Momentum: APAC Evidence", *Pacific-Basin
  Finance Journal* 80, 2023.
- M. Cliff, M. Cooper and H. Gulen, "Return Differences between Trading
  and Non-Trading Hours: Like Night and Day", working paper, 2008;
  D. Lou, C. Polk and S. Skouras, "A Tug of War: Overnight versus Intraday
  Expected Returns", *Journal of Financial Economics* 134(1), 2019.
- R. Ariel, "A Monthly Effect in Stock Returns", *Journal of Financial
  Economics* 18, 1987; R. Ariel, "High Stock Returns before Holidays",
  *Journal of Finance* 45(5), 1990; J. Lakonishok and S. Smidt, "Are
  Seasonal Anomalies Real? A Ninety-Year Perspective", *Review of Financial
  Studies* 1(4), 1988; J. McConnell and W. Xu, "Equity Returns at the Turn
  of the Month", *Financial Analysts Journal* 64(2), 2008.
- P. Carr and L. Wu, "Variance Risk Premiums", *Review of Financial
  Studies* 22(3), 2009; G. Bakshi and N. Kapadia, "Delta-Hedged Gains and
  the Negative Market Volatility Risk Premium", *Review of Financial
  Studies* 16(2), 2003.
- C. Gao, Y. Xing and X. Zhang, "Anticipating Uncertainty: Straddles around
  Earnings Announcements", *Journal of Financial and Quantitative Analysis*
  53(6), 2018; A. Dubinsky, M. Johannes, A. Kaeck and N. Seeger, "Option
  Pricing of Earnings Announcement Risks", *Review of Financial Studies*
  32(2), 2019.
- E. Gatev, W. Goetzmann and K. G. Rouwenhorst, "Pairs Trading: Performance
  of a Relative-Value Arbitrage Rule", *Review of Financial Studies* 19(3),
  2006; B. Do and R. Faff, "Does Simple Pairs Trading Still Work?",
  *Financial Analysts Journal* 66(4), 2010.
- R. D. McLean and J. Pontiff, "Does Academic Research Destroy Stock Return
  Predictability?", *Journal of Finance* 71(1), 2016; H. Jacobs and
  S. Müller, "Anomalies across the Globe: Once Public, No Longer
  Existent?", *Journal of Financial Economics* 135(1), 2020.
- C. Harvey, Y. Liu and H. Zhu, "... and the Cross-Section of Expected
  Returns", *Review of Financial Studies* 29(1), 2016; the
  multiple-testing sources in [discovery.md](../discovery.md#sources).

India:

- SEBI, "Analysis of Profit and Loss of Individual Traders dealing in Equity
  F&O Segment", Jan 2023 (FY22); SEBI press release 37/2024, 23 Sep 2024
  (FY22 to FY24), sebi.gov.in; SEBI study of 7 Jul 2025 (FY25), as reported
  by Business Standard and Moneylife on 7 to 8 Jul 2025; SEBI study of Aug
  2026 (FY25 to FY26), as summarised on tradingqna.com (primary not read).
- S. K. Agarwalla, J. Jacob and J. R. Varma, "Four Factor Model in Indian
  Equities Market", IIM Ahmedabad W.P. 2013-09-05, and the monthly-updated
  data library at iima.ac.in (Indian Fama-French and momentum factors).
- S. Jain, J. R. Varma and S. K. Agarwalla, "Indian Equity Options: Smile,
  Risk Premiums, and Efficiency", *Journal of Futures Markets*, 2018-19
  (author note on jrvarma.in, 2018).
- S. Sehgal and I. Balakrishnan, "Contrarian and Momentum Strategies in the
  Indian Capital Market", *Vikalpa* 27(1), 2002; S. Sehgal and S. Jain,
  "Short-Term Momentum Patterns in Stock and Sectoral Returns: Evidence from
  India", *Journal of Advances in Management Research* 8(1), 2011.
- NSE Indices: Nifty200 Momentum 30 methodology and factsheet; press
  release of 25 Aug 2020 (launch); momentum strategy white paper (2026) and
  factor research (fetch timed out on 2026-11-01; figures from secondary
  summaries: Value Research, Business Standard 14 Dec 2025, fund
  documents). Nifty100 Low Volatility 30 methodology.
- Vipul, "Futures and Options Expiration-Day Effects: The Indian Evidence",
  *Journal of Futures Markets* 25(11), 2005.
- M. Karmakar and M. Chakraborty (2003) and Archana, M. Safeer and S. Kevin
  (2014), turn-of-the-month studies, as cited in "Indian Stock Market
  Regularity: The Turn of the Month Effect", *Review of Finance and
  Banking* 15, Dec 2023.
- V. Tripathi and S. Aggarwal, "Prior Return Effect in Indian Stock Market:
  An Intra-day Analysis", *International Journal of Financial Management*.
- A study of the NIFTY variance risk premium and volatility selling, Apr
  2021 to Jul 2025 (informaticsjournals.com, SDMIMD journal; authors not
  verified).
- Post-earnings drift on NSE: "Post-Earning Announcement Drift and
  Value-Glamour Anomalies in NSE Listed Firms" (Trends Economics and
  Management, Brno); an event study of 469 Indian firms, Dec 2002 to Dec
  2011 (University of Nottingham eprint 26569).
- The 2026 charges: Finance Act 2026, NSE/FATAX/73524 (31 Mar 2026),
  NSE/FA/73061; the pack's table in trader-budget (see
  [data-sources.md](../data-sources.md)).
