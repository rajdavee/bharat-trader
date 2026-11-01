---
name: trader-regime
description: >-
  Labels the market regime for NIFTY, BANKNIFTY or any NSE stock, in plain
  words: trending up, trending down, ranging or no clear trend (ADX 14,
  Kaufman efficiency ratio, the 20/50 EMAs and their slope, the 200-day
  average), the volatility bucket (India VIX percentile over a year,
  realised vol 20 days vs a year, IV rank), the day's gap in ATRs, market
  breadth, weekly and monthly expiry today or tomorrow from the exchange's
  own expiry list, results days from NSE board meetings, RBI policy and
  Union Budget days, holidays and special sessions, the closing-auction
  window, and the time-of-day bucket (open drive, midday, closing window).
  No look-ahead: each day uses data up to its own close. trader-lab splits
  every backtest by these regimes. Use when the user says "what is the
  market regime", "is NIFTY trending or range-bound", "trending ya
  sideways", "is volatility high today", "VIX percentile", "expiry day
  kal hai kya", "is it results day for RELIANCE", "RBI policy day",
  "regime history", "does my strategy work in this market", or "market ka
  mood kya hai". Analytics only: it never places an order.
argument-hint: "[now [SYMBOL...] | history SYMBOL --from --to | explain SYMBOL] [--json]"
---

# trader-regime

Strategies work in some markets and fail in others. This skill says which
market it is, so a strategy is chosen for *now*, and so the lab can show how
a strategy did in each kind of market. A regime describes the market so far:
it does not predict the next move.

`<skill dir>` is this skill's folder. Times are IST.

```bash
python3 "<skill dir>/regime.py" now                         # NIFTY and BANKNIFTY
python3 "<skill dir>/regime.py" now RELIANCE --json         # one stock, machine-readable
python3 "<skill dir>/regime.py" history NIFTY --from 2025-01-01 --to 2026-10-30
python3 "<skill dir>/regime.py" explain NIFTY               # every input, its value and its threshold
python3 "<skill dir>/regime.py" now --data DIR --now "2026-10-30 16:00"   # offline, pinned clock
```

A read looks like (NIFTY after the close of Friday 30 October 2026):

```
NIFTY: no clear trend (ADX 33), high vol (VIX 15.3, 71st pct)
  close 23,053.10 on 2026-10-30 (Yahoo ^NSEI (2 years, unofficial)); ADX 32.8, ER 0.044, below the 200-day average (-4.11%)
  vol: high by India VIX percentile; realised 14.96% (20d) vs 13.67% (1y); IV rank 32.8 (India VIX (NIFTY 30-day implied vol))
  expiry: next weekly 2026-11-03, 2 sessions away (archive F&O bhavcopy 2026-10-30)
  closing auction (F&O stocks): 15:15-15:35; DERIV continuous trading ends 15:40
  breadth 2026-10-30: 618 up, 2058 down (broad decline)
```

## What it measures

| part | inputs | default rule |
| --- | --- | --- |
| trend | ADX(14) and ±DI (Wilder), efficiency ratio(10), EMA 20 and 50, EMA 20's slope over 5 sessions, close vs the 200-day average | direction up when close > EMA 50, EMA 20 > EMA 50 and the slope is up (down the mirror); **trend** when there is a direction and ADX ≥ 25 or ER ≥ 0.30; **range** when ADX < 20 and ER < 0.20, or no direction and ADX < 25; else **transition** (counted as range) |
| volatility | India VIX percentile over 252 sessions (indices); the stock's own 20-day realised vol percentile (stocks); realised vol 20d vs 1y; IV rank | low below the 33rd percentile, high from the 67th; unknown with fewer than 60 sessions |
| gap | the day's open vs the previous close, in ATR(14) of the days before | gap from 0.25 ATR, big gap from 0.75 ATR |
| breadth | advances vs declines in the day's equity bhavcopy (EQ series) | broad advance from 65% advancing, broad decline below 35% |
| expiry | the expiries listed in the archived F&O bhavcopy (futures expiries are the monthlies); without it the dated weekday rule (Tuesday from 1 Sep 2025, Thursday from 20 Nov 2024; a holiday moves it to the trading day before) | expiry today, or on the next session |
| events | `events.json` (RBI policy decision days and Union Budgets, each with its source), your own in `trader/regime-config.json`, board meetings for results (NSE, cached a day in `trader/regime/board/`) | today, next session, first session after results |
| sessions | NSE holidays, special and short sessions, the dated SESSION_RULES (closing auction for F&O stocks from 3 Aug 2026; 15:30 to 15:40 from 7 Dec 2026) | holiday before the next session, special session |
| time of day | the moment `now`, against the segment's session | pre-open, open drive 09:15-10:00, morning to 11:30, midday to 13:30, afternoon, closing window (the last 30 minutes of continuous trading), closing auction, post-close |

IV rank: for NIFTY and BANKNIFTY, India VIX's rank over a year (VIX is
NIFTY's 30-day implied vol, so for BANKNIFTY it is the market's, not the
bank index's own). For a stock, `--iv` estimates an ATM implied vol for each
archived day from the F&O bhavcopy's settlement prices (straddle / (0.8 x
spot x sqrt(T)), Brenner and Subrahmanyam) and ranks it over a year; it is an
approximation and needs at least 20 archived days.

## No look-ahead

Every daily value at day D uses bars up to D's close only: the averages, ADX
and the percentiles run forward over the history, so changing a later bar
cannot change an earlier day (the tests mutate the future and check). The
calendar is what was known in advance: the expiry list in that day's own F&O
bhavcopy, scheduled events, announced board meetings. `now` during market
hours takes trend and volatility from the last complete session and adds
today's calendar, the gap from today's open, the time bucket and an intraday
read from 5-minute bars complete by now.

## Data

The local trader-archive when present (`trader/archive`, `--archive DIR`,
`$BT_ARCHIVE`; `--no-archive` to skip it): NIFTY and Bank Nifty from NSE's
index files, India VIX, split-adjusted stocks, the F&O expiry lists and
breadth. Else Yahoo Finance daily bars for two years (unofficial, free). With
`--data DIR`, offline files `SYMBOL-1d.csv(.gz)` (`^NSEI`, `^NSEBANK`,
`^INDIAVIX`, and `board-SYMBOL.json`). `--offline` never touches the network.
NSE calls use a browser User-Agent and NSE's Referer, 1.5 s apart, and wait
once after HTTP 403 or 429.

## Storage

`history` writes `trader/regime/SYMBOL.json` (one compact record a day, with
the hash of the thresholds that made it) and, when the archive is there, an
archive table `archive/regime/YYYY/YYYY-MM-DD.csv.gz` (one row per symbol,
with a manifest like every archive dataset). `--no-store` prints only.

## Thresholds

Every threshold has a default in `regime.py` (`DEFAULTS`); put any of them in
`trader/regime-config.json` to change it, plus `"events": [{"date",
"kind", "name", "source"}]` for your own dated events (global events, a
company's AGM). `explain` prints each input, its value and its rule.

## For other skills

Load `regime.py` by path and call the pure functions: `indicators(bars)`,
`classify(ind, i, vix)`, `daily_regimes(bars, vix_bars, symbol)`,
`regime_on(symbol, day, bars, vix_bars)`, `expiry_info(underlying, day,
root)`, `calendar_flags(...)`, `time_bucket(now, segment)`,
`regime_key(record)`, `label_trades(trades, regimes)`,
`split_by_regime(trades, regimes)` and `edge_in_regime(...)`. trader-lab uses
them for its regime split and its opt-in gate `require_regime_edge`.

## Limits

Regimes are labels on the past, not forecasts; they change after the fact
as new bars arrive (the label for today can flip tomorrow, never the label
for yesterday). The thresholds are conventions, not laws. A regime cell in a
backtest with few trades says almost nothing: the lab marks cells under
`min_regime_trades` as too few to judge. See `docs/regime.md`.

Analytics only: it reads public data and writes under `trader/`. It never
places, sizes or confirms an order. Education and tooling, not financial
advice.
