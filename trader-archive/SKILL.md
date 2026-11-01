---
name: trader-archive
description: >-
  A local NSE market data archive that the other skills read offline: years
  of daily history instead of Yahoo's 60 sessions. Backfills NSE's equity
  and F&O bhavcopies (UDiFF from 2024, the legacy format before, one schema
  for both), every NSE index's daily close, India VIX, F&O lot sizes, ban
  lists, futures settlement prices and corporate actions (split and bonus
  adjustment factors), politely and resumably, skipping weekends and NSE
  holidays, back to 1994 for equities and 2000 for F&O. The long history:
  continuous NIFTY/BANKNIFTY (or any F&O stock) futures with a roll log,
  back- and ratio-adjusted; point-in-time NIFTY 50/Next 50/100/200/500
  constituents rebuilt from NSE Indices' change announcements, so
  cross-sectional backtests are survivorship-free; results (earnings) dates
  with the minute they came out, for event studies. Adds 1-minute and 5-minute candles from your broker's history
  API (Upstox with your token; Kite or Dhan on their data plans) merged with
  Yahoo's recent bars, and optional option-chain snapshots during market
  hours. Checks itself: checksums, gaps against the trading calendar,
  duplicates, outliers, survivorship. Use when the user says "download NSE
  history", "bhavcopy archive", "backfill 3 years of data", "10 saal ka data",
  "F&O bhavcopy", "continuous futures", "rollover adjusted NIFTY futures",
  "Nifty 50 constituents in 2015", "survivorship free universe", "results
  dates", "earnings calendar history", "expired options data",
  "historical option chain", "split adjusted prices", "India VIX history",
  "1 minute data from Upstox", "is my data complete", "data gaps",
  "purana data download karo", or "update the archive". It never places an
  order.
argument-hint: "[backfill --years 3 | update | status | verify | export --symbol X | intraday --symbols X | snapshot-chain | continuous | members | results | expired-options | capture-chains]"
---

# trader-archive

Backtests need history, and Yahoo keeps only about 60 sessions of 5-minute
bars. This skill builds your own archive of NSE's official end-of-day files
on your machine, then answers every read from disk: no network at read time.
trader-lab and trader-timemachine use it automatically when it is there.

All dates are IST trading days. `<skill dir>` is this skill's folder.

```bash
python3 "<skill dir>/archive.py" backfill --years 3                     # everything, newest day first
python3 "<skill dir>/archive.py" backfill --years 3 --datasets eq,fo,vix  # only some datasets
python3 "<skill dir>/archive.py" update                                 # from the last archived day to today
python3 "<skill dir>/archive.py" status                                 # files, rows, size, first and last day
python3 "<skill dir>/archive.py" verify                                 # checksums, gaps, duplicates, outliers
python3 "<skill dir>/archive.py" export --symbol RELIANCE --from 2024-01-01 --out RELIANCE-1d.csv
python3 "<skill dir>/archive.py" intraday --symbols RELIANCE,NIFTY --interval 1m --from 2026-08-01
python3 "<skill dir>/archive.py" intraday --symbols TCS --interval 5m --yahoo --no-broker
python3 "<skill dir>/archive.py" snapshot-chain --symbols NIFTY,BANKNIFTY --every 15
python3 "<skill dir>/archive.py" backfill --from 2010-01-01 --datasets all     # the long history (hours; resumable)
python3 "<skill dir>/archive.py" continuous --underlying NIFTY --method back --roll days:1
python3 "<skill dir>/archive.py" members --index NIFTY50 --date 2015-06-30    # who was in the index that day
python3 "<skill dir>/archive.py" members --report                             # coverage and gaps of the rebuild
python3 "<skill dir>/archive.py" results --symbol INFY --from 2015-01-01      # results dates, after-close flag
python3 "<skill dir>/archive.py" expired-options --broker upstox --underlying NIFTY --from 2026-08-01
python3 "<skill dir>/archive.py" capture-chains --symbols NIFTY,BANKNIFTY --every 5 --sessions 0   # background
```

`--workdir DIR` (default `trader`) puts the archive in `DIR/archive`; `--root`
or `$BT_ARCHIVE` point at the folder itself. `--json` prints machine-readable
output. A backfill can be stopped at any moment (Ctrl-C, a dropped line, NSE
blocking): the next run of the same command carries on.

## Datasets

| dataset | source (nsearchives / nseindia) | stored as |
| --- | --- | --- |
| `eq` | `content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` (UDiFF, from 2024-01-02); before that `content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip` | `eq/YYYY/YYYY-MM-DD.csv.gz`: symbol, series, isin, OHLC, last, prev close, volume, turnover, trades |
| `fo` | `content/fo/BhavCopy_NSE_FO_0_0_0_...` (UDiFF); before 2024 `content/historical/DERIVATIVES/.../foDDMONYYYYbhav.csv.zip` | `fo/YYYY/...`: symbol, kind (IDO, IDF, STO, STF), expiry, strike, CE/PE, OHLC, settle, underlying, OI in shares, OI change, volume in contracts, turnover, lot |
| `index` | `content/indices/ind_close_all_DDMMYYYY.csv` | every NSE index, India VIX included, with P/E, P/B, yield |
| `vix` | `api/historicalOR/vixhistory` (70 rows a call: a quarter at a time) | `vix/YYYY.csv.gz` |
| `lots` | `content/fo/fo_mktlots.csv` (today only; NSE keeps no history) | one snapshot a day; from 2024 each F&O row carries its own lot |
| `ban` | `archives/fo/sec_ban/fo_secban_DDMMYYYY.csv` | the ban list per day |
| `settle` | `archives/nsccl/sett/FOSett_prce_DDMMYYYY.csv` | futures MTM settlement prices |
| `ca` | `api/corporates-corporateActions` (a year a call) | raw JSON per year; splits and bonuses become price factors |
| `indexhist` | `api/historicalOR/indicesHistory` (an index a quarter a call), only before 2012-02-21 when the daily index files start | `indexhist/<index>/YYYY.csv.gz`; `--indices` picks them (default 22 main ones, or `all`) |
| `results` | `api/corporate-board-meetings` and `api/corporates-financial-results` (a month a call each, from 2010) | `results/YYYY/YYYY-MM_board.json.gz`, `..._filings.json.gz` |
| `members` | niftyindices.com press-release list and its index-change PDFs; today's `ind_nifty50list.csv` (Next 50, 100, 200, 500); `symbolchange.csv`, `EQUITY_L.csv` | `members/`: each announcement's text, the lists, the reference files |

The first three datasets are opt-in (`--datasets all`, or name them). What NSE
serves, verified 2026-11-01: equity bhavcopies from 1994-11-03, F&O from
2000-06-12, daily index files from 2012-02-21, India VIX history from
2010-07-19 (nothing earlier from NSE), corporate actions and results filings
from 2010. Days before a dataset exists cost no call.

Each dataset-year folder has a `manifest.json`: every file's source URL,
`fetched_at` (IST), row count, size and SHA-256. `calendar.json` remembers the
weekdays NSE had no session (a weekday whose bhavcopy is missing well after
the day). Expect about 0.2 MB a day for `eq`, 0.6 MB for `fo`: roughly 0.6 GB
for three years of everything, and about 90 minutes for a first 3-year
backfill at the default pace.

## Politeness

A browser User-Agent and NSE's Referer, `--pause` seconds between NSE calls
(default 1.5, never below 1.0), a 20 s then 40 s wait after HTTP 401, 403 or
429, and a stop after 3 blocks in a row (exit code 3) with everything fetched
so far kept. Weekends and NSE holidays are never asked for. The data is for
your own use: NSE's terms restrict redistribution, so do not publish the
archive.

## The read API (offline)

Import `archive.py` (by path) and call, with `root=` the archive folder:

| function | returns |
| --- | --- |
| `load_daily(symbol, start, end, adjusted=True)` | daily bars `{date, o, h, l, c, v, series, isin, factor}`; adjusted for splits and bonuses by default |
| `load_daily_many(symbols, ...)` | the same for several stocks, one pass over the files |
| `load_intraday(symbol, interval, start, end)` | 1m or 5m bars with their source; 5m is built from 1m when needed |
| `load_fo_chain_eod(underlying, date)` | the day's futures and options of one underlying, with spot and lot |
| `load_vix(start, end)`, `load_index(name, start, end)` | India VIX; any index (NIFTY, BANKNIFTY, ^NSEI, "Nifty 50") |
| `load_ban(date)`, `load_settle(date)`, `load_lots(date)` | ban list, settlement prices, lot sizes |
| `trading_days(start, end, expected=False)` | the sessions the archive holds (or the calendar's) |
| `adjustment_factors(symbol)` | the split and bonus events behind the adjustment |
| `chain_snapshots(underlying, date)` | the option-chain snapshots taken that day |
| `chain_series(underlying, date, expiry=None)` | those snapshots as rows: time, strike, CE/PE, LTP, OI, volume, IV, bid, ask |
| `load_continuous(underlying, method, start, end, roll="days:0")` | continuous futures: `bars` (with the contract held), `rolls` (the roll log), `issues`; method `none`, `back` or `ratio` |
| `members(index, date)` | point-in-time constituents of NIFTY 50 / NEXT 50 / 100 / 200 / 500, as traded that day |
| `membership(index)` | the rebuilt history: every composition with its dates, issues, unresolved names |
| `results_dates(symbol, start, end)` | results events: the day, the minute out, after the close or not, the first session that could react |
| `load_option_intraday(underlying, expiry, strike, type)` | expired-option candles fetched with `expired-options` |

## Adjustment

A bonus a:b multiplies earlier prices by b / (a + b) (1:1 halves them); a
split from face value F1 to F2 by F2 / F1. Volumes are divided by the same
factor. Bonus preference shares (NCRPS), dividends, rights issues and
demergers are not adjusted: `verify` lists any unexplained move over 20% so
you can see them. NSE's own previous close is not adjusted on an ex-date;
`verify` checks every factor against the real prices that day.

## Intraday

`intraday` uses trader-broker's adapter for your broker (its `adapters/`
folder must sit next to this skill) and your token from the environment
(`BT_UPSTOX_ACCESS_TOKEN`: `eval "$(python3 trader-broker/ticket.py login upstox)"`).
Upstox keeps minute candles from January 2022, a month per call. Kite and
Dhan need their paid data plans; without a token they are skipped with a
note. `--yahoo` merges Yahoo's last 60 sessions (5m) or 7 (1m): a Yahoo bar
only fills a slot the broker does not have, and disagreements over 0.5% are
counted. Broker candles may already be adjusted by the broker for corporate
actions; the archive stores them as given. The gap report shows days with
missing or short sessions; Muhurat and drill sessions are left out.

## The long history

The helper modules live in `history/` inside this skill (loaded by path, standard
library only): `pdftext.py` reads the announcement PDFs, `members.py` rebuilds
membership, `continuous.py` builds continuous futures, `events.py` the results
dates, `expired.py` the broker requests for expired options.

- **Continuous futures**: `roll` is `days:N` (the last day on a contract is N
  sessions before its expiry; `days:0` holds through expiry day), `oi` or
  `volume` (switch the day after the next contract's OI or volume passes the
  near one's; still by expiry at the latest). `back` shifts everything before
  a roll by the gap on the old contract's last day; `ratio` multiplies by
  new / old. The newest contract always shows real prices. Every roll is
  logged with both prices.
- **Point-in-time membership**: NSE Indices publishes no free historical
  constituent file, so membership on a past day is today's list with every
  later announced change undone. Announcements before about 2012 name
  companies, not symbols: names are matched through later announcements,
  NSE's list of companies and today's index files; renamed companies that
  cannot be matched are listed as gaps, never guessed. `members --report`
  shows every composition whose size is not the index's size, and members
  that did not trade on the day.
- **Results dates**: one event per reported period, from the filing's
  broadcast time; results out after 15:30 IST react on the next session. A
  results board meeting with no filing in the feed is kept, time unknown.
  Dividends are not price-adjusted anywhere in the archive.
- **Expired options**: NSE publishes no intraday option history. Upstox's
  expired-instruments API (Upstox Plus plan) and Dhan's rolling-option API
  (Data API plan) serve minute candles of expired contracts with your own
  token; `expired-options` stores them under `optx/`. Without such a plan,
  run `capture-chains`: a background loop that snapshots the option chain
  every few minutes through each session (minimum 3 minutes), writes a
  heartbeat to `chain/capture.json`, refuses to run twice on one archive, and
  waits out NSE blocks instead of hammering. Forward data only.

## Verify and the quality report

`verify` re-hashes every file, compares row counts, finds duplicate rows,
checks OHLC sanity, lists trading days missing from each dataset against the
calendar, checks splits and bonuses against prices, flags unexplained moves,
notes short sessions, reports intraday gaps, and counts the stocks listed at
the start but gone at the end (survivorship: a backtest on today's stocks
only is biased towards survivors; with `members` on file, pick index
universes point in time instead). With the long history it also checks
every split and bonus across the span against the closes either side of the
ex-date, the rebuilt membership (sizes, unmatched names, members not
trading), the NIFTY and BANKNIFTY rolls (gaps over 2%, broken chains) and
missing results months. Exit code 1 on any problem; these are warnings.

## Safety

Data infrastructure only: it reads public files and writes under the archive
folder. It never places, sizes or confirms an order. Education and tooling,
not financial advice.
