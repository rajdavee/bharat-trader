---
name: trader-scanner
description: >-
  Chart Scanner for Indian intraday traders. Scans NSE equities for setups:
  opening-range breakouts, bull flags, 20-day closing-high breakouts,
  reclaims of the 20-day average, oversold and oversold bounces. Builds the
  universe from NSE's EQUITY_L.csv (EQ series only, so MIS-able cash
  stocks), runs a pre-market scan of daily closes with a rupee liquidity
  filter (price and median daily turnover in crore) and a NIFTY 50 / BANK
  NIFTY / INDIA VIX context line, and an intraday opening-range breakout
  scan on 5-minute bars in IST (09:15 open), then writes
  trader/watchlist.json for trader-news and trader-entry. Free Yahoo data
  (SYMBOL.NS), bar by bar. Use when the user says "scan NSE", "scan the
  market", "intraday setups", "find me setups for today", "what is breaking
  out", "opening range breakout", "ORB scan", "Nifty stocks breaking out",
  "bull flags", "oversold stocks", "pre-market scan", "build my watchlist",
  "what should I look at today", or at the start of a trading day before
  /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of an intraday trade on the NSE. It
reads a couple of thousand free charts and lists the ones that match a rule.
That is all it does. A scan is a list of charts that match a rule, not a list
of stocks to buy, and a setup on a chart is not a prediction. SEBI's own
studies find that most individual intraday traders in Indian equities lose
money; the scanner does not change that. It only saves you from flipping
through 2,000 charts by hand.

`<skill folder>` below is this skill's folder (where this SKILL.md lives,
inside your agent's skills folder).

```bash
python3 "<skill folder>/scan.py" universe                              # -> trader/universe.txt
python3 "<skill folder>/scan.py" daily                                 # before 09:15 IST -> trader/watchlist.json
python3 "<skill folder>/scan.py" daily --symbols RELIANCE,HDFCBANK,M&M # or --watchlist FILE, --limit 200
python3 "<skill folder>/scan.py" orb                                   # after 09:35 IST -> trader/watchlist.json
python3 "<skill folder>/scan.py" orb --at 10:15                        # cut the clock at 10:15 IST
python3 "<skill folder>/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price` (₹, default 50), `--min-turnover` (₹; daily default
10 crore = 100000000, orb default 0), `--or-minutes 15`, `--max-confirm`
(stage 2: daily 300, orb 200), `--now "2026-10-29 10:15"` (IST) to pin the
clock, `--offline PATH` to read saved files, `--no-index`, `--no-write`,
`--show N`, `--json`. Exit code 0 when the scan found something, 1 when it
found nothing or could not run (the reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name\|ISIN` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `exchange`, `universe`, `scanned`, `failed`, `index`, `hits[]`. Symbols are NSE symbols (`M&M`, not `M&M.NS`). trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | optional: `min_price`, `min_turnover`, `no_entries_after` (14:45), `extra_holidays`, `special_sessions`. Missing keys take the defaults |

## The universe

`universe` downloads `EQUITY_L.csv` from nsearchives.nseindia.com (NSE's
list of listed equities: SYMBOL, NAME OF COMPANY, SERIES, ISIN NUMBER, ...)
and keeps a row when:

1. Its SERIES is `EQ` (`--series EQ`). BE and BZ are trade-for-trade: every
   trade must be delivered, so they cannot be bought and sold the same day
   (no MIS). On the Oct 29 2026 file that is 2,368 EQ of 2,620 rows.
2. Its symbol is a plain NSE symbol: capital letters, digits, `&` and `-`,
   up to 20 characters (M&M, BAJAJ-AUTO, 3MINDIA).

## The daily scan (before the open)

Run it before 09:15 IST (or after the 15:30 close). Yahoo's spark endpoint
returns daily closes for 20 symbols a request, so 2,368 symbols take 119
requests with a polite 0.3 s pause. Today's daily bar is left out until
15:30.

The setups, computed from daily closes:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Liquidity, in rupees, so you are not stuck in a thin stock at square-off:

- last close at least `min_price` (₹50), and
- median daily turnover (close x volume) over the last 20 sessions at least
  `min_turnover` (₹10 crore).

Spark has no daily volumes, so turnover is checked in two steps: a cheap cut
on the last session's turnover from the spark meta (half the floor),
then every chart that matched a setup gets its full daily chart (one request
each, at most `--max-confirm`, most traded first). That chart also decides
the setups: spark's daily series skips a session for a few names now and
then, and the full chart wins. When the cap is hit the output says PARTIAL.

It also prints one line of index context from the last two complete daily
closes: NIFTY 50 (`^NSEI`), BANK NIFTY (`^NSEBANK`) and INDIA VIX
(`^INDIAVIX`). A gap-down Nifty with VIX jumping is a different morning from
a quiet one; the scanner only reports it.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the 09:15 session: the
09:15, 09:20 and 09:25 five-minute bars. A hit is the first completed
5-minute bar to CLOSE above the range high, at that bar's close time (the
09:35 bar closes at 09:40). The pre-open call auction (09:00 to 09:08) has no
5-minute bars and is not part of the range.

1. Stage 1, every symbol: spark closes of today's 5-minute bars. A chart
   goes on only if a completed bar after the range closed at or above the
   highest close of the range. That is a necessary condition (the range high
   is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the full chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low
   = the lowest low. At most `--max-confirm` (200) charts, most rupees traded
   first; PARTIAL says how many were skipped.

A chart is usable only when every 5-minute bar from 09:15 to the cutoff is
there; a missing bar means a halt, a circuit or a thin stock. Null closes are
dropped, a bar off the 5-minute grid (the live tick, like 10:49:45) is
dropped, and a bar counts only once it is complete. Before 09:35, on a
weekend or NSE holiday, after the 15:30 close, or after `no_entries_after`
(14:45: an expired setup, close to the broker's MIS square-off) it prints one
plain line and writes nothing.

## Calendar

- IST all year (UTC+05:30, no daylight saving). Normal session 09:15 to
  15:30, Monday to Friday.
- NSE trading holidays for 2026 (Capital Market segment) are built in. The
  2027 list is not published yet: for a year with no table the scanner warns
  and treats weekdays as open. Add holidays with `extra_holidays`.
- Muhurat trading (Diwali, 2026-11-08, a Sunday) is announced separately by
  NSE. Add it as a special session when NSE publishes the timing:
  `"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]`
  (example timing only). The opening range then starts at that open.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars
  are 5-minute bars, bar by bar, never tick by tick. query2 first, query1 on
  a 429 or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 2,301 of 2,368 charts (67
  failed or had no data)". Never round a partial scan up to "the whole market".
- An EQ stock can still hit its price band (circuit) or be moved to the T2T
  segment; the scanner does not know the bands. Check before trading.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before 09:15 IST: `scan.py daily`. Tell the user the index line, the
   counts (fetched, liquid, scanned, hits) and the top few by setup.
3. After 09:35 IST (10:15 is the classic): `scan.py orb`, ideally over the
   daily watchlist (`--watchlist trader/watchlist.json`) for speed. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is
   moving it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick
   for them.

## Example: Thu 2026-10-29

Real Yahoo data for 61 liquid NSE names, saved in
`tests/fixtures/trader-scanner/`. The pre-market scan at 09:00 IST (closes
through Wed Oct 28) with the default floors:

```
python3 scan.py daily --offline daily-v8-2026-10-29.json.gz --now "2026-10-29 09:00"
Fetched 61 of 62 charts (1 failed or had no data). 56 passed the liquidity filter (₹50.00+ and a median daily turnover of ₹10.00 cr+); 56 scanned.
13 charts matched a setup: 2 20-day closing-high breakout, 5 bull flag, 4 reclaimed the 20-day average, 1 oversold (RSI under 30), 2 oversold bounce.
  LT             ₹3,801.30  RSI  56.4  ₹744.63 cr  20-day closing-high breakout, reclaimed the 20-day average
  M&M            ₹2,869.90  RSI  43.2  ₹722.00 cr  reclaimed the 20-day average
```

YESBANK, IDEA and SUZLON are under ₹50; 20MICRONS (₹1.61 cr a day) and
21STCENMGM are under the turnover floor, so their setups are not listed.

The opening-range scan at 10:15 IST the same day:

```
python3 scan.py orb --offline wall5m-2026-10-29.json.gz --now "2026-10-29 10:15"
Fetched 61 of 62 charts (1 failed or had no data); 58 usable (3 under the price / turnover floor); 43 candidates, 43 confirmed with real highs and lows.
32 of 58 charts closed above their opening-range high by 10:15 IST:
  09:35  HDFCBANK         ₹682.50  range 678.30 to 682.45  +0.01% over
  09:40  RELIANCE       ₹1,174.30  range 1,156.20 to 1,170.50  +0.32% over
```

RELIANCE's range is the 09:15 to 09:25 bars, high 1,170.50, low 1,156.20;
the 09:35 bar closed at 1,174.30, 0.32 % over the high, at 09:40. If the user
picks it, the agent hands it to `/trader-news RELIANCE`, then
`/trader-entry RELIANCE`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how
  many were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data live, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
