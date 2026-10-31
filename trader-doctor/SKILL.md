---
name: trader-doctor
description: >-
  The data self-test for bharat-trader. Probes every free source the pack
  reads, once each and politely: NSE's equity list, market status, holiday
  master, corporate announcements, board meetings, results (integrated
  filings), bulk and block deals, SEBI PIT disclosures, the option chain v3
  and its contract info, F&O lot sizes, the F&O stock list and India VIX
  history; Yahoo's chart, spark and headlines feed for RELIANCE.NS and
  ^NSEI. Checks each answer's shape (the fields the tools read), freshness
  against the NSE calendar in IST (a weekend or holiday answer from the last
  session is fine) and units, then prints one table: ok, slow, changed, down
  or blocked, since when, which tools are affected and the fallback in use.
  Use when the user says "is NSE down", "is Yahoo working", "why is the
  scanner empty", "the option chain is not loading", "check the data
  sources", "data health check", "NSE API blocked", "is the data stale",
  "run the doctor", "self-test", or "kya NSE ka data aa raha hai".
argument-hint: "[--only nse|yahoo] [--json] [--offline DIR]"
---

# trader-doctor

Every number in bharat-trader comes from free, public, unofficial endpoints:
NSE's website APIs and archives, and Yahoo Finance. None of them promise to
stay the same. When one changes, the tools that read it go quiet or wrong.
The doctor tells you which source broke, since when, what it breaks and what
the tools fall back to.

```
python3 trader-doctor/doctor.py                 # every source, one request each
python3 trader-doctor/doctor.py --only nse      # only NSE (or --only yahoo)
python3 trader-doctor/doctor.py --json          # one JSON document (schema bharat-trader/doctor/1)
python3 trader-doctor/doctor.py --offline DIR   # replay recorded answers, no network
python3 trader-doctor/doctor.py --record DIR    # save each live answer for --offline
```

It takes about 30 seconds: 15 NSE requests 1.5 seconds apart and 4 Yahoo
requests. It writes only `trader/doctor.json` (the history; `--no-write`
leaves it alone) and never places an order.

## What a status means

| status | meaning |
| --- | --- |
| ok | answered, in the shape the tools read, fresh, units sane |
| slow | ok, but took longer than 4 seconds |
| changed | answered, but a field the tools need is gone, a number is in another unit, the data is stale, or the address is gone (HTTP 404) |
| down | no answer within 10 seconds, or HTTP 5xx |
| blocked | HTTP 401, 403 or 429, or a web page instead of data (a bot filter) |
| skipped | not tried: no recorded answer offline, or NSE already blocked this run |

The exit code is 0 when nothing is changed, down or blocked, else 1 (2 for a
usage error), so a cron job or CI can alert on it.

## Freshness, in IST

The doctor knows the NSE calendar (weekends and the 2026 holiday list). Live
data (market status, the option chain, Yahoo's charts) should be from the
latest session that has opened: today after 09:15 on a trading day, else the
last trading day. In the first half hour of a session yesterday's answer is
still accepted. End-of-day files (bulk and block deals, PIT, India VIX
history) may lag one session. So on a Sunday, Friday's answers are fresh; on
Tuesday at 11:00 they are stale.

## What each source feeds

| source | affects | fallback |
| --- | --- | --- |
| NSE equity list | scanner universe | your existing `trader/universe.txt` |
| NSE market status | `alerts clock --verify`, UI | the built-in NSE calendar (always the tools' clock) |
| NSE holiday master | every tool's calendar | the built-in 2026 table; the doctor says when NSE's list differs |
| corporate announcements | news | Yahoo headlines only |
| board meetings | news, analyst | none: results dates are not flagged |
| results (integrated filings) | analyst | none |
| bulk.csv, block.csv | insider deals | `insider.py deals --days N` (the history API) |
| bulk and block deals history | insider deals --days | none |
| SEBI PIT | insider | none |
| option contract info, option chain v3 | options | none: options stops |
| fo_mktlots.csv | options lot sizes | none: no lot sizes |
| F&O stock list (master-quote) | scanner, entry, exit, timemachine, risk | `trader/fno_stocks.json` cache, else every stock is treated as an F&O stock (the earlier close) |
| India VIX history | options IV rank | none: IV rank is left out |
| Yahoo chart and spark | scanner, entry, exit, timemachine, alerts, UI | query1 when query2 answers 429 or 5xx (the doctor tries it and says so) |
| Yahoo headlines RSS | news | NSE announcements only |

## History: since when

`trader/doctor.json` keeps, per source, its status, since when it has had
that status, when it was last ok and when it was last checked, plus the last
30 runs. "since" in the table is when the current status started; a failing
row also says when the source was last ok here.

## Politeness

NSE: a browser User-Agent, `Accept: application/json`, the
`https://www.nseindia.com/` Referer, gzip, 1.5 seconds between calls, a
10-second timeout. On HTTP 403 or 429 it waits 5 seconds and tries once more;
if NSE blocks again it stops asking NSE for the rest of the run (those rows
say "skipped"). Yahoo: half a second between calls. Run it when something
looks wrong, not in a loop.

## Offline

`--offline DIR` reads `DIR/<source id>.json` (or `.json.gz`), one recorded
answer each: `{"url", "status", "error", "elapsed", "body"}`. Give it more
than once and a later folder wins, so a folder with one broken answer can sit
on top of a folder of good ones. `--record DIR` writes that format from a
live run. `--now "YYYY-MM-DD HH:MM"` judges freshness at another IST time.
The test fixtures in `tests/fixtures/trader-doctor/` are real recorded
answers: ok, changed, down and blocked.

## Also in

- The local UI: a "Data" badge in the header shows the last result; click it
  to run the self-test (`GET /api/doctor`, `POST /api/doctor/run`).
- The MCP server: the read-only `doctor` tool.

## Limits

- One probe per source, on a liquid name (RELIANCE, NIFTY). A source can work
  for RELIANCE and still fail for one small stock.
- "ok" means the shape the tools read today is there. It cannot prove every
  number is right.
- Broker APIs are not probed yet (v0.2 ships the paper broker only).

Education and tooling, not financial advice.
