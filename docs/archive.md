# The local market data archive

`trader-archive` keeps years of NSE history on your own machine, so the lab,
the time machine and anything you write can read it offline. Yahoo keeps
about 60 sessions of 5-minute bars; the archive keeps as much as you backfill.

## Quick start

```bash
python3 trader-archive/archive.py backfill --years 1          # about 30 minutes, newest day first
python3 trader-archive/archive.py status
python3 trader-archive/archive.py verify
python3 trader-archive/archive.py update                      # each evening after about 19:00 IST
```

Stop a backfill any time: the next run of the same command skips what is
already stored and carries on. `--datasets eq,fo,index,vix,lots,ban,settle,ca`
picks datasets (`all` adds the long-history `indexhist,results,members`); `--from`/`--to` pick dates; `--pause` (default 1.5 s, never
below 1.0 s) sets the gap between NSE calls.

## What is stored

```
trader/archive/
  calendar.json                    weekdays NSE had no session (learned from missing bhavcopies)
  eq/2026/2026-10-30.csv.gz        the equity bhavcopy, one schema for UDiFF and legacy
  eq/2026/manifest.json            per file: source URL, fetched_at (IST), rows, bytes, SHA-256, format
  fo/...  index/...  ban/...  settle/...  lots/...
  vix/2026.csv.gz                  India VIX by year
  ca/2026.json.gz                  corporate actions by year
  intraday/1m/RELIANCE/2026-10.csv.gz   broker and Yahoo candles, with their source
  chain/NIFTY/2026-10-30/1000_2026-11-03.json.gz   option-chain snapshots
  .cache/                          derived per-symbol indexes (safe to delete)
```

Formats verified on 2026-11-01:

- **UDiFF** bhavcopies exist from **2024-01-02**. The legacy `cm...bhav.csv`
  and `fo...bhav.csv` files exist up to **2024-07-05**. The archive asks for
  UDiFF from 2024 and the legacy file before, and tries the other format for
  the first half of 2024. Both become the same columns.
- F&O open interest is in **shares**, volume in **contracts**, turnover in
  rupees (the legacy file's lakh are converted). Legacy files carry no
  underlying price or lot size: `load_fo_chain_eod` takes spot from the index
  or equity archive.
- `FOSett_prce` rows for `OPTIDX`/`OPTSTK` have no expiry: they are the
  underlying's settlement price.
- Lot sizes: NSE publishes only today's `fo_mktlots.csv`, so the archive keeps
  a daily snapshot. From 2024 every F&O bhavcopy row also carries its lot.
- India VIX history comes 70 rows a call (fetched a quarter at a time); older
  days are also in the index files.

Size and time: about 0.2 MB a day for equities and 0.6 MB for F&O, so roughly
0.6 GB and 90 minutes for a first 3-year backfill of everything.

## The long history (2010 and earlier)

```bash
python3 trader-archive/archive.py backfill --from 2010-01-01 --datasets all     # everything, resumable (see the estimate)
python3 trader-archive/archive.py backfill --from 2010-01-01 --datasets eq,fo   # just the bhavcopies
python3 trader-archive/archive.py continuous --underlying BANKNIFTY --method ratio --roll days:1 --out bn.csv
python3 trader-archive/archive.py members --index NIFTY500 --date 2014-03-03
python3 trader-archive/archive.py members --report
python3 trader-archive/archive.py results --symbol INFY --from 2012-01-01
```

What NSE serves, probed live on 2026-11-01:

| data | oldest | how | notes |
| --- | --- | --- | --- |
| equity bhavcopy | 1994-11-03 | `content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip` | no ISIN and no trade count before 2011; the same file name all the way back |
| F&O bhavcopy | 2000-06-12 | `content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip` | the option-type column is `OPTIONTYPE` until 2007, `OPTION_TYP` after |
| daily index closes | 2012-02-21 | `content/indices/ind_close_all_DDMMYYYY.csv` | older names: S&P CNX Nifty, CNX Nifty, CNX Bank, CNX 500 ... read as today's |
| index history before that | 2010 and earlier | `api/historicalOR/indicesHistory?indexType=NIFTY 50&from=..&to=..` | an index a quarter a call (about 60 rows; the cap is 70): the `indexhist` dataset |
| India VIX | 2010-07-19 | `api/historicalOR/vixhistory` | NSE answers an empty list for anything earlier: VIX before July 2010 is a gap |
| ban list, settlement prices | 2010 or earlier | as for recent days | the 2010-2012 ban title spells the month in full |
| corporate actions | 2010 or earlier | `api/corporates-corporateActions`, a year a call | 2010 subjects read "Fv Split Rs.10 To Re.1" (handled) |
| board meetings, results filings | 2010-01 | `api/corporate-board-meetings`, `api/corporates-financial-results` | a month a call; January 2010 alone is about 1.9 MB of JSON |
| index-change announcements | 1998 | niftyindices.com press-release page and `Press_Release/ind_prsDDMMYYYY.pdf` | about 280 relevant PDFs since 2010 |

Days before a dataset exists cost no call. Weekdays without a session (older
holidays) are learned from the missing equity file, as before.

SIZE_ESTIMATE

### Continuous futures

`load_continuous(underlying, method, start, end, roll)` (and the `continuous`
command) picks one contract a day from the F&O bhavcopies:

- `--roll days:N`: the last day on a contract is N sessions before its
  expiry; `days:0` holds it through expiry day, `days:1` moves on expiry day.
- `--roll oi` / `--roll volume`: move the day after the next contract's open
  interest (or volume) first passes the near one's; by expiry at the latest.
  The switch uses only what was known at that day's close.
- `--method none`: the held contract's own prices, with the roll log (both
  contracts' prices on the old one's last day, the gap, the ratio).
- `--method back`: everything before a roll shifted by the gap; differences
  are real, old price levels are not.
- `--method ratio`: everything before a roll multiplied by new / old;
  percentage returns are real, levels are not.

The newest contract always shows real prices. An untraded contract on a day
falls back to its settlement price, and the issue list says so. Expiry days
come from the data, so the weekday changes (BANKNIFTY moved from Thursday to
Wednesday to the last Tuesday) need no table.

### Point-in-time index membership

NSE Indices sells historical constituent files; it gives away the
announcements. `backfill --datasets members` downloads the press-release
list, every index-change PDF in range (only its text is kept, so a better
parser later needs no download), today's NIFTY 50, Next 50, 100, 200 and 500
lists, NSE's symbol changes and its list of companies. `members(index, date)`
then takes today's list and undoes every later change, newest first, and
maps each stock back to the symbol it traded under that day (SESAGOA, then
SSLT, now VEDL).

How good is it? `members --report` (and `verify`) count, per index: the
compositions whose size is not 50, 100, 200 or 500, the changes whose company
name could not be matched to a symbol (announcements before about 2012 give
names only; renamed companies are the usual miss), the "included then but not
in the later list" inconsistencies, and members that did not trade on the
first day of their composition. Each one is a place where a cross-section
on that day is not exact. Corporate events that NSE Indices handled without a
press release on the list are not seen. A test that shows its universe came
from `members()` is survivorship-free to the extent the report shows; a test
on today's NIFTY 50 is not survivorship-free at all.

### Results dates

`results_dates(symbol, start, end)` gives one event per reported period: the
day and minute the filing was disseminated (`announced_at`), whether that was
after the 15:30 close, the first session that could react (`event_session`),
the period it covers, and the board meeting behind it. A results board
meeting with no filing in the feed is kept with the time unknown. Dividends
are never price-adjusted in the archive: an event study around ex-dividend
days must add them back itself.

### Intraday option history

Researched 2026-11-01:

- NSE publishes no intraday option history at all.
- **Upstox** has an expired-instruments API (expiries, expired option and
  future contracts, and minute candles of expired contracts with OI) on its
  **Upstox Plus** plan. `archive.py expired-options --broker upstox` uses it
  with your token: every expiry in range, the strikes either side of that
  day's close, calls and puts, into `optx/upstox/<UNDERLYING>/<EXPIRY>/`.
- **Dhan** has a rolling expired-options API (ATM to ATM+-10 for index
  options, minute bars with IV, OI and spot, 30 days a call, about five years
  back) on its **Data API** plan. `--broker dhan` stores it under
  `optx/dhan/<UNDERLYING>/WEEK1/`.
- **Kite** historical data (Kite Connect's paid plan) serves live contracts
  only, not expired ones.

Neither paid plan was available while building this, so the request paths
and the parsers follow the brokers' documentation and are tested on
documented answer shapes, not recorded ones. Without such a plan, run
`capture-chains` in the background to build forward data yourself:

```bash
python3 trader-archive/archive.py capture-chains --symbols NIFTY,BANKNIFTY --every 5 --expiries 2 --sessions 0
```

It sleeps until 09:15 IST on a trading day, snapshots each symbol's nearest
expiries every `--every` minutes (minimum 3) until 15:30, sleeps to the next
session, and writes a heartbeat to `archive/chain/capture.json` (rounds,
snapshots, the last and next times, the last error) that a scheduler or the
auto-pilot can read. A second copy on the same archive refuses to start. An
NSE block pauses it for 15 minutes rather than ending it. `chain_series()`
turns a day's snapshots into rows.

## Adjustment for splits and bonuses

`load_daily(..., adjusted=True)` (the default) multiplies prices before each
ex-date by the factor and divides volumes by it: a bonus a:b gives b / (a + b),
a split from face value F1 to F2 gives F2 / F1. NSE's previous close is not
adjusted on the ex-date (JSW Dulux, 22 Oct 2026: 2,994.80 then 302.20), so
`verify` checks every factor against the real prices. Dividends, rights
issues, demergers and bonus preference shares are not adjusted; `verify`
lists unexplained moves over 20%.

## Intraday from your broker

```bash
eval "$(python3 trader-broker/ticket.py login upstox)"        # sets BT_UPSTOX_ACCESS_TOKEN in this shell
python3 trader-archive/archive.py intraday --symbols RELIANCE,NIFTY --interval 1m --from 2026-01-01
python3 trader-archive/archive.py intraday --symbols RELIANCE --interval 5m --yahoo
```

The call goes through trader-broker's adapter (`trader-broker/adapters/`
must sit next to `trader-archive/`). Upstox's v3 historical candles start in
January 2022 for minute intervals, a calendar month a call; the instrument
key comes from the ISIN in your equity archive. Kite and Dhan history need
their paid data plans: without a token they are skipped with a note. A month
fetched in full is not asked again. With `--yahoo`, Yahoo's recent bars fill
only the slots the broker does not have; disagreements over 0.5% are
counted. Broker candles may be adjusted by the broker for corporate actions
and are stored as given.

## Option-chain snapshots

```bash
python3 trader-archive/archive.py snapshot-chain --symbols NIFTY,BANKNIFTY --expiries 2 --every 15
```

Runs only during market hours (09:15 to 15:30 IST on a trading day; `--force`
overrides) and saves NSE's option chain v3 for the nearest expiries. Keep
the terminal open, or schedule it. These snapshots feed later options
backtests: NSE does not publish intraday chain history.

## Reading it from Python

```python
import importlib.util
spec = importlib.util.spec_from_file_location("archive", "trader-archive/archive.py")
archive = importlib.util.module_from_spec(spec); spec.loader.exec_module(archive)
bars = archive.load_daily("RELIANCE", "2024-01-01", "2026-10-30", root="trader/archive")
chain = archive.load_fo_chain_eod("NIFTY", "2026-10-30", root="trader/archive")
```

Also `load_daily_many`, `load_intraday`, `load_vix`, `load_index`,
`load_ban`, `load_settle`, `load_lots`, `trading_days`, `adjustment_factors`,
`chain_snapshots`, `chain_series`, `load_continuous`, `members`,
`membership`, `results_dates` and `load_option_intraday`. Nothing in the read
API touches the network.

```python
cont = archive.load_continuous("NIFTY", "back", "2012-01-01", "2026-10-30", roll="days:1", root="trader/archive")
universe = archive.members("NIFTY 500", "2015-06-30", root="trader/archive")     # who was in it then
events = archive.results_dates("INFY", "2012-01-01", root="trader/archive")
```

## Who uses it

- **trader-lab** reads daily and 5-minute bars, NIFTY and India VIX from
  `trader/archive` when present (`--archive DIR`, `--no-archive`), then falls
  back to Yahoo.
- **trader-timemachine** adds the archive's older 5-minute sessions of the
  stock to its pool; Yahoo still supplies the plan's own day.

## Verify and data quality

`verify` re-hashes every file, compares row counts, finds duplicate rows and
broken OHLC, lists trading days missing from each dataset against the
calendar, checks splits and bonuses against prices, flags unexplained moves,
notes short sessions (Muhurat, drill days), reports intraday gaps, and gives a
survivorship note: stocks listed at the start but gone at the end. A backtest
on today's stocks only is biased towards the survivors; with the `members`
dataset on file, take index universes from `members(index, date)` instead.
With the long history, `verify` also checks every split and bonus across the
whole span against the archive's own closes on either side of the ex-date
(not NSE's previous-close field), reports the membership rebuild (wrong
sizes, unmatched names, members not trading), the NIFTY and BANKNIFTY rolls
(gaps over 2%, days the contract chain was broken) and missing results
months. Exit code 1 on any problem; warnings do not fail it.

NSE's terms restrict redistribution of its data. The archive is for your own
use: do not publish it.
