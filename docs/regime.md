# The market regime engine

`trader-regime` labels each trading day for NIFTY, BANKNIFTY or any NSE stock:
is the market trending or ranging, calm or nervous, gapping, near an expiry
or an event? A strategy that works in one kind of market often fails in
another; the label lets you, the lab and other skills ask "does this have an
edge in the market we have *now*?".

A regime describes the market so far. It is not a forecast.

## Quick start

```bash
python3 trader-regime/regime.py now                               # NIFTY and BANKNIFTY
python3 trader-regime/regime.py now RELIANCE TCS --json           # stocks, machine-readable
python3 trader-regime/regime.py history NIFTY --from 2025-01-01 --to 2026-10-30
python3 trader-regime/regime.py explain NIFTY                     # every input and threshold
python3 trader-regime/regime.py now --data tests/fixtures/trader-lab,tests/fixtures/trader-regime --now "2026-10-30 16:00"
```

The last line is the offline demo on saved real data. Its NIFTY read
(Friday 30 October 2026, the same numbers live from Yahoo on 1 November):

```
NIFTY: no clear trend (ADX 33), high vol (VIX 15.3, 71st pct)
  close 23,053.10 on 2026-10-30 (^NSEI-1d.csv.gz); ADX 32.8, ER 0.044, below the 200-day average (-4.11%)
  vol: high by India VIX percentile; realised 14.96% (20d) vs 13.67% (1y); IV rank 32.8 (India VIX (NIFTY 30-day implied vol))
  expiry: next weekly 2026-11-03, 2 sessions away (the dated weekday rule: no archived F&O bhavcopy for the day)
  closing auction (F&O stocks): 15:15-15:35; DERIV continuous trading ends 15:40
```

ADX 33 says the index has been moving strongly, but the close sits below the
50-day EMA while the 20-day EMA is still rising: the averages disagree on a
direction, so the label is "no clear trend" (counted as range in splits).

## The method

All indicators run on daily bars, oldest first. The value for day D uses
bars up to D's close and nothing later.

### Trend

| input | definition |
| --- | --- |
| ADX(14), +DI, -DI | Wilder (1978). True range, +DM and -DM summed over the first 14 bars, then smoothed S = S - S/14 + x. DI = 100 x DM / TR. DX = 100 x abs(+DI - -DI) / (+DI + -DI). ADX starts as the mean of the first 14 DX, then (ADX x 13 + DX) / 14 |
| efficiency ratio(10) | Kaufman: abs(close - close 10 sessions ago) / the sum of the 10 absolute daily changes. 1 is a straight line, near 0 is chop |
| EMA 20, EMA 50 | alpha 2 / (n + 1), seeded with the simple mean of the first n closes |
| EMA 20 slope | % change of the 20-day EMA over 5 sessions |
| 200-day average | simple mean of 200 closes; reported as above or below, and the distance |

Direction **up** when the close is above the 50-day EMA, the 20-day EMA is
above the 50-day EMA and the slope is positive; **down** for the mirror
image; else none. Then:

- **trending up / down**: a direction, and ADX >= 25 or ER >= 0.30;
- **ranging**: ADX < 20 and ER < 0.20, or no direction and ADX < 25;
- **no clear trend** (`transition`): anything else, such as a strong ADX
  with the averages disagreeing. Two-way splits count it as range.

### Volatility

- **Indices**: India VIX's percentile over the last 252 sessions (the share
  of closes strictly below today's, the same as trader-options' IV
  percentile). Low below 33.3, high from 66.7. VIX is NIFTY's 30-day
  implied vol; for BANKNIFTY it is the market's, not the bank index's own.
- **Stocks**: the stock's own 20-day realised vol (sample standard deviation
  of log returns x sqrt 252), as a percentile of its last 252 values.
- Realised vol 20 days against 252 days (above 1: busier than usual).
- IV rank: India VIX's (close - year low) / (year high - year low) for the
  indices; for a stock with `--iv`, an approximate ATM implied vol per
  archived day, straddle / (0.8 x spot x sqrt(T)) at the strike nearest spot
  of the first expiry at least 5 days out (Brenner and Subrahmanyam, 1988),
  on settlement prices, ranked over a year (20 days at least). Cached in
  `archive/.cache/regime-iv/`.
- Fewer than 60 sessions of history: the bucket is unknown.

### Gap and breadth

The gap is the day's open against the previous close, in ATR(14) (Wilder) of
the sessions before: a gap from 0.25 ATR, a big gap from 0.75 ATR. Breadth
counts EQ-series stocks in the day's archived equity bhavcopy closing above
and below their previous close: broad advance from 65% advancing, broad
decline at 35% or less.

### Calendar

- **Expiry.** The archived F&O bhavcopy of the day (or the latest one up to
  7 days back) lists every live contract of the underlying: futures expiries
  are the monthlies, option expiries without a future are weeklies. A stock
  in the cash file but not the F&O file has no expiry. Without the archive,
  the dated weekday rule (`EXPIRY_RULES`): Thursday from 20 Nov 2024 (SEBI's
  one weekly index per exchange; NSE kept NIFTY), Tuesday from 1 Sep 2025;
  monthly on the month's last such weekday, weekly for NIFTY only; a holiday
  moves an expiry to the trading day before (Tue 10 Nov 2026 -> Mon 9 Nov).
  Before 20 Nov 2024 the expiry is unknown without the archive.
- **Events.** `trader-regime/events.json`: RBI Monetary Policy Committee
  decision days (the last day of each meeting, from RBI's published
  schedules, including the August 2025 meeting RBI moved a day earlier) and
  Union Budget days, each row with its source. Your own events go in
  `trader/regime-config.json` under `events`.
- **Results.** NSE's board meetings for the stock (`corporate-board-meetings`,
  fetched at most once a day and cached in `trader/regime/board/`): a meeting
  whose purpose mentions results marks a results day; the session before
  and the session after are flagged too.
- **Sessions.** NSE holidays (the 2026 table, plus the closed days the
  archive learned), special and short sessions (Muhurat, Budget weekends,
  drills), a holiday before the next session, and the dated SESSION_RULES
  for the close: continuous trading to 15:15 and the closing auction 15:15 to
  15:35 for F&O stocks from 3 Aug 2026, derivatives to 15:40; from 7 Dec 2026
  continuous trading to 15:30, the auction to 15:40, derivatives to 15:45.

### Time of day

`time_bucket(now, segment)`: pre-market, pre-open (09:00-09:15), **open
drive** 09:15-10:00, **morning** to 11:30, **midday** to 13:30,
**afternoon**, the **closing window** (the last 30 minutes of the segment's
continuous trading), the **closing auction** (F&O stocks under the CAS) and
post-close. Indices use the derivatives session, stocks the F&O-stock one.
During market hours `now` adds an intraday read from 5-minute bars that have
closed by now (the archive's, else Yahoo's): the move from the previous
close, the range in ATRs and the intraday efficiency ratio.

## No look-ahead

- Every indicator is computed forward: the value at day D depends on bars
  0..D only. The tests change every bar after a date (prices x2, VIX x3) and
  check that every earlier day's record is byte-for-byte the same.
- `now` during market hours takes trend and volatility from the last
  complete session (yesterday's close); only the gap (today's open), the
  calendar and the intraday read are today's.
- The calendar uses what was known in advance: that day's own expiry list,
  scheduled events, announced board meetings. A board meeting announced late
  or moved is a small leak in old history; NSE shows the final date.
- In the lab, a trade takes the regime of the close **before** its signal day,
  with the signal day's own expiry flag.

## Storage

- `trader/regime/SYMBOL.json`: `{date: compact record}` with the hash of the
  thresholds that made it (a different config starts the file over). Read by
  the MCP server as `bharat-trader://trader/regime/NIFTY.json`.
- `archive/regime/YYYY/YYYY-MM-DD.csv.gz`: one row per symbol a day (trend,
  direction, ADX, ER, 200-day side, vol bucket, VIX percentile, realised vol,
  IV rank, gap, expiry flags, events, the words), with a manifest entry like
  every archive file. `load_table(symbol, start, end, root)` reads it back.

## Thresholds

`regime.py`'s `DEFAULTS`, each overridable in `trader/regime-config.json`
(unknown keys are refused):

| key | default | |
| --- | --- | --- |
| `adx_period`, `er_period` | 14, 10 | indicator windows |
| `ema_fast`, `ema_slow`, `slope_sessions`, `sma_long` | 20, 50, 5, 200 | averages and the slope |
| `adx_trend`, `adx_range` | 25, 20 | ADX levels for trend and range |
| `er_trend`, `er_range` | 0.30, 0.20 | efficiency ratio levels |
| `vol_lookback`, `vol_min_history` | 252, 60 | percentile window and the minimum history |
| `vol_low_pct`, `vol_high_pct` | 33.3, 66.7 | bucket edges |
| `rv_short`, `atr_period` | 20, 14 | realised vol and ATR windows |
| `gap_atr`, `gap_big_atr` | 0.25, 0.75 | gap sizes in ATRs |
| `breadth_strong` | 0.65 | advancing share for a broad advance |
| `open_drive_end`, `morning_end`, `midday_end`, `closing_window_min` | 10:00, 11:30, 13:30, 30 | time buckets |
| `events` | [] | your own dated events |

## In the lab, the UI and MCP

- **trader-lab** labels every trade (whole sample and out of sample) by
  NIFTY's trend or range x the VIX bucket x the expiry flag, with trades,
  mean R and its 95% interval; cells under `min_regime_trades` (10) are too
  few to judge. The opt-in gate `require_regime_edge: 1` asks for at least
  that many out-of-sample trades in today's regime with a mean above the
  expectancy gate. See [lab.md](lab.md).
- **UI**: `GET /api/regime?symbols=NIFTY,BANKNIFTY` (see [ui.md](ui.md)); the
  Today page shows NIFTY's regime in its Market card.
- **MCP**: the read-only tool `regime_now` and the `trader/regime/`
  resources (see [mcp.md](mcp.md)).
- **From code**: load `regime.py` by path and call `daily_regimes(bars,
  vix_bars, symbol)`, `regime_on(...)`, `expiry_info(...)`,
  `calendar_flags(...)`, `time_bucket(...)`, `regime_key(...)`,
  `label_trades(...)`, `split_by_regime(...)`, `edge_in_regime(...)`.

## Limits

- Labels are on the past. Today's label can flip tomorrow as a new bar
  arrives (an earlier day's never does).
- Thresholds are conventions (ADX 25 is Wilder's), not laws: change them and
  the labels change. `explain` shows how close each input is to its edge.
- Few trades per regime cell say almost nothing, and the 95% interval
  (mean +/- 1.96 standard errors) is itself rough for small samples.
- India VIX measures NIFTY's implied vol only. A stock's IV rank is an
  approximation from end-of-day settlement prices, not a full model.
- Yahoo's daily bars are unofficial; the archive's NSE files are the better
  source. Holidays before 2026 come only from the archive's learned
  calendar; without it, an old holiday counts as a session for "next
  session" and expiry shifts.
- The rule fallback for expiries holds from 20 Nov 2024; NSE's own list in
  the archived bhavcopy always wins.

Education and tooling, not financial advice. Nothing here places an order.
