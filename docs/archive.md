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
picks datasets; `--from`/`--to` pick dates; `--pause` (default 1.5 s, never
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
`load_ban`, `load_settle`, `load_lots`, `trading_days`, `adjustment_factors`
and `chain_snapshots`. Nothing in the read API touches the network.

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
on today's stocks only is biased towards the survivors. Exit code 1 on any
problem; warnings do not fail it.

NSE's terms restrict redistribution of its data. The archive is for your own
use: do not publish it.
