# Data sources

Everything is free and public. Nothing needs an account or an API key. None
of these are official APIs with stability guarantees: when one changes, open a
[data source issue](https://github.com/rajdavee/bharat-trader/issues/new?template=data_source.yml).

| source | used by | what |
| --- | --- | --- |
| `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | scanner | Every NSE-listed equity. EQ series kept by default; BE/BZ are trade-for-trade (no intraday). |
| Yahoo Finance chart `query2.finance.yahoo.com/v8/finance/chart/SYMBOL.NS` | scanner, entry, exit, timemachine, alerts, news | 1-minute and 5-minute bars (60 days of 5-minute), daily bars, last price. |
| Yahoo Finance spark `query2.finance.yahoo.com/v7/finance/spark` | scanner | Daily closes for many symbols per request (batches of 20). |
| Yahoo Finance RSS `feeds.finance.yahoo.com/rss/2.0/headline?s=SYMBOL.NS` | news | Headlines. Coverage of Indian stocks is patchy; some symbols return an empty feed. |
| `nseindia.com/api/corporate-announcements` | news | Exchange filings (LODR Regulation 30): results, dividends, buybacks, orders, ratings… |
| `nseindia.com/api/corporate-board-meetings` | news, analyst | Upcoming board meetings, used to flag results dates. |
| `nseindia.com/api/marketStatus` | alerts | Cross-check of whether the cash market is open. |
| `nseindia.com/api/corporates-financial-results` | analyst | Quarterly results list up to the Dec-2024 quarter, with XBRL links. |
| `nseindia.com/api/integrated-filing-results` | analyst | SEBI integrated filings (financials) for later quarters, with XBRL links. |
| `nsearchives.nseindia.com/corporate/xbrl/*.xml` | analyst, insider | The XBRL documents themselves (Ind-AS results, banking template, PIT disclosures). |
| `nsearchives.nseindia.com/content/equities/bulk.csv`, `block.csv` | insider | The latest day's bulk and block deals. |
| `nseindia.com/api/historicalOR/bulk-block-short-deals` | insider | Bulk and block deals over a date range (`&csv=true` for every row). |
| `nseindia.com/api/corporates-pit-gg` | insider | SEBI PIT insider-trading disclosures (one row per filing; details are in the linked XBRL). |
| `nseindia.com/api/option-chain-contract-info?symbol=X` | options | The exchange's expiry list and strikes for one underlying. |
| `nseindia.com/api/option-chain-v3?type=Indices\|Equity&symbol=X&expiry=DD-Mon-YYYY` | options | One expiry's option chain. OI and volume in contracts. |
| `nsearchives.nseindia.com/content/fo/fo_mktlots.csv` | options | F&O lot size per underlying per expiry month. |
| `nseindia.com/api/historicalOR/vixhistory`, `nseindia.com/api/allIndices` | options | India VIX history (70 rows a call) and the live value. |
| `nseindia.com/api/holiday-master?type=trading` | maintainers | Source of the holiday table shipped in the tools. |
| `nseindia.com/api/master-quote` | scanner, entry, exit, timemachine (cached in `trader/fno_stocks.json`; read by alerts, risk, broker) | NSE's F&O underlying list: a JSON array of stock symbols (218 on 2026-11-01). These stocks close in the Closing Auction Session. Fetched at most once a day, only by a run that is online anyway. |

## Self-test

`python3 trader-doctor/doctor.py` probes 19 of the sources above, one request
each, and prints one table. Not probed: the older results and PIT lists,
`allIndices`, and the XBRL documents (links found in the other answers). The same check is the **Data** badge in the UI header (click it to run)
and the `doctor` MCP tool.

```
Data self-test, Sun 2026-11-01 11:41 IST. NSE closed (weekend); fresh data is from Fri 2026-10-30 or later.
source                              status        since             affects                                       fallback
NSE equity list (EQUITY_L.csv)      ok 0.5s       2026-11-01 10:41  scanner universe                              -
NSE market status                   down          2026-11-01 11:41  alerts clock --verify, UI                     the built-in NSE calendar (the tools' own clock)
    no answer: timed out after 10 s; last ok 2026-11-01 10:41
...
Yahoo chart (RELIANCE.NS)           down          2026-11-01 11:41  scanner, entry, exit, timemachine, alerts, UI  query1: answering
    no answer: timed out after 10 s; last ok 2026-11-01 10:41
...
17 ok, 0 slow, 0 changed, 2 down, 0 blocked.
```

- **Status.** `ok`; `slow` (over 4 s); `changed` (a field the tools read is
  gone, a number is out of its sane range, e.g. NIFTY at 230 instead of
  23,000, the data is stale, or HTTP 404); `down` (no answer in 10 s, or 5xx);
  `blocked` (401, 403, 429, or a web page instead of data). Exit code 1 when
  any source is changed, down or blocked.
- **Shape.** Each answer is checked for the fields the tools read: e.g.
  `marketState[].tradeDate`, `records.data[].CE.openInterest`,
  `EOD_CLOSE_INDEX_VAL`, the EQUITY_L and deals CSV columns, the chart's
  `meta.gmtoffset` (19800) and `currency` (INR).
- **Freshness, in IST.** Live answers should be from the latest session that
  has opened (today after 09:15 on a trading day, else the last trading day;
  yesterday's is still fine in the first half hour). End-of-day files (bulk
  and block deals, PIT, VIX history) may lag one session. A weekend or holiday
  answer from the last session is fresh. The holiday master is compared with
  the tools' own 2026 table, so a new or moved NSE holiday shows up as
  `changed`.
- **Since when.** `trader/doctor.json` keeps each source's status, since when,
  the last ok time and the last 30 runs.
- **Politeness.** NSE's headers as above, 1.5 s between NSE calls, one retry
  after 5 s on 403 or 429, then no more NSE calls that run.
- **Offline.** `--offline DIR` replays recorded answers (`--record DIR` saves
  them); `tests/fixtures/trader-doctor/` holds real ok, changed, down and
  blocked answers.

## Known quirks

- **Index symbols** (`^NSEI`, `^NSEBANK`, `^INDIAVIX`) go to Yahoo without `.NS`.
- **`M&M`** and other symbols with `&` must be URL-encoded (`M%26M.NS`).
- **Yahoo's 09:15 bar reports zero volume** for NSE stocks; volume checks leave it out.
- **Yahoo's late bars for F&O stocks are the closing auction.** From 2026-08-03 an F&O stock stops continuous trading at 15:15; Yahoo then shows a near-empty 15:15 bar, an empty 15:20 bar and one auction print in the 15:25 bar (RELIANCE, 2026-10-29). The tools end those stocks' sessions at continuous trading's end (see [market rules](#market-rules-dated)). Yahoo also occasionally drops a bar mid-session; the Time Machine keeps sessions that are complete up to your flat-by time.
- **Yahoo rate-limits** with HTTP 429 after heavy use; the tools back off and retry, then report it.
- **NSE needs a browser-like User-Agent** and a `Referer` of `https://www.nseindia.com/`. No cookies are needed today.
- **Corporate announcements without a date window** return thousands of rows; the tools always send `from_date`/`to_date`.
- **Old-format results XBRL** places nine-month figures in contexts dated like the quarter; the parser picks the shortest plain period ending on the reporting date.
- **Banks** use a separate XBRL template (bank flag `B`); the analyst shows NII, operating profit and provisions instead of EBITDA.
- **The historical bulk-deals JSON caps at 70 rows**; the CSV form returns all of them. NSE's bulk list repeats block trades; they are kept once, labelled block.
- **PIT holding percentages** in the XBRL are fractions (0.3049 = 30.49 %). Some filers enter the per-share price as the value; those rows are flagged and left out of totals.
- **Option chain v3** returns only the expiry asked for. A wrong expiry returns HTTP 200 with no rows and spot 0. `openInterest` and `totalTradedVolume` are in contracts (x lot = units), while the bhavcopy's open interest is in shares. `optionType` is always null. NSE's `impliedVolatility` disagrees between calls and puts and is 0 for skipped strikes; `trader-options` computes its own.
- **Spot after 15:30.** The cash index stops at 15:30 while equity derivatives trade until 15:40, so the chain's `underlyingValue` is stale against option prices in that window.

## Market rules (dated)

Session timings changed three times in 2026. The tools carry them as a dated
table, `SESSION_RULES` (the same block in entry, exit, alerts, risk,
timemachine, scanner and broker; `tests/test_repo.py` keeps the copies equal).
Each row holds from its date until the next. A new NSE or SEBI change is a new
row, never an edit. Times are IST. "F&O stock" means a stock in NSE's F&O
list; other cash stocks keep 09:15-15:30.

| from | pre-open (order entry; market orders until) | F&O stocks: continuous trading ends | Closing Auction Session (F&O stocks) | other cash stocks | futures and options | post-close | source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| before 2026-08-03 | 09:00-09:08, random close 09:07-09:08 | 15:30 | none | 09:15-15:30 | to 15:30 | 15:40-16:00 | [NSE/CMTR/74969](https://nsearchives.nseindia.com/content/circulars/CMTR74969.zip) (1 Jul 2026, "current" pre-open); [NSE/FAOP/74467](https://nsearchives.nseindia.com/content/circulars/FAOP74467.zip) (29 May 2026, F&O close moved from 15:30) |
| 2026-08-03 | unchanged | **15:15** | 15:15 reference price; 15:20 limit and market orders; 15:25 limit only; random close 15:28-15:30; matching to 15:35 | 09:15-15:30 | **to 15:40** | 15:50-16:00 | SEBI circular of 16 Jan 2026, para 4.2 and 6.1, sent with [NSE/CMTR/72394](https://nsearchives.nseindia.com/content/circulars/CMTR72394.zip) (19 Jan 2026); [NSE/FAOP/74467](https://nsearchives.nseindia.com/content/circulars/FAOP74467.zip); go-live [NSE/CMTR/75479](https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf) and [NSE/FAOP/75472](https://nsearchives.nseindia.com/content/circulars/FAOP75472.pdf) (30 Jul 2026) |
| 2026-09-07 | **09:00-09:10; market orders until 09:05; random close 09:08-09:10; matching 09:10-09:12** | 15:15 | as above | 09:15-15:30 | to 15:40 | 15:50-16:00 | SEBI circular of 16 Jan 2026, para 5 and 6.2 ([NSE/CMTR/72394](https://nsearchives.nseindia.com/content/circulars/CMTR72394.zip)); [NSE/CMTR/74969](https://nsearchives.nseindia.com/content/circulars/CMTR74969.zip); [NSE/CMTR/76187](https://nsearchives.nseindia.com/content/circulars/CMTR76187.pdf) (4 Sep 2026, live from Monday 7 Sep) |
| 2026-12-07 | as above | **15:30** | **15:30 reference price; 15:31 limit and market orders; 15:36 limit only; random close 15:38-15:40** | 09:15-15:30 | **to 15:45** (expiring index contracts stop at 15:30 on expiry day) | 15:50-16:00 | SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/23843/2026 of 15 Oct 2026, para 4.2 and 6.1; [Zerodha bulletin, 16 Oct 2026](https://zerodha.com/marketintel/bulletin/461085/changes-to-closing-auction-session-cas-from-december-7-2026) |

Not modelled yet: the expiry-day 15:30 stop for expiring index contracts
(it is in the table for the options tools), and the transition of unexecuted
iceberg orders into CAS from 2027-02-08 (para 4.8 of the October circular).
No stop-loss orders are accepted in the auction; the exit plan says so.

### Broker MIS square-off

Brokers square off intraday (MIS) positions on their own, usually for a fee,
and earlier for F&O stocks. Pick yours with `broker_profile`; the tools then
flat by 5 minutes (`square_off_margin_min`) before the earlier of that time and
continuous trading's end. As read on 2026-11-01:

| profile | F&O stocks | other stocks | F&O contracts | from | source |
| --- | --- | --- | --- | --- | --- |
| `zerodha` | 15:12 (15:10 from 3 Aug) | 15:25 | 15:26 | 2026-08-10 | [support.zerodha.com: intraday auto square-off timings](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings); [product update, 10 Aug 2026](https://zerodha.com/z-connect/updates/track-closing-auction-session-on-kite-web) |
| `upstox` | 15:10 (15:05 before) | 15:25 (15:20 before) | 15:25 (15:20 before) | 2026-09-11 | [Upstox announcement, 4 Sep 2026](https://upstox.com/announcements/revised-timings/important-update-intraday-square-off-timings-are-changing/) |
| `dhan` | 15:10 | 15:10 | 15:25 | 2026-08-03 | [Dhan support: intraday auto square-off timings](https://dhan.co/support/general/market-session-status-and-timing/what-are-the-dhan-intraday-auto-square-off-timings/) |
| `angel` | 15:10 | 15:10 | 15:20 | 2026-08-03 | [SmartAPI forum notice, CAS changes from 3 Aug 2026](https://smartapi.angelone.in/smartapi/forum/post/19209) (Angel One's [support page](https://www.angelone.in/support/your-orders/square-off) still says 15:15 for equity; the earlier time is kept) |
| `groww` | 15:05 | 15:20 | 15:20 | 2026-08-03 | [Groww risk policy](https://groww.in/p/risk-policy) |
| `custom` | your `square_off` | | | | |

None of these brokers had announced times for the 2026-12-07 CAS change by
2026-11-01. Until they do, the tools keep the times above: for an F&O stock
they are earlier than the new 15:30 end of continuous trading, so they stay on
the safe side.

### F&O charges

`round_trip_charges(..., segment="OPT" | "FUT", on=date)` adds options and
futures to the shared charges helper (budget, risk, broker, exit,
timemachine). Equity (`segment` "EQ", the default) is unchanged and still
reads `charges` from the config. F&O rates are a dated table, `FNO_CHARGES`;
`fno_charges` in the config overrides any of them.

| levy | options | futures | from | source |
| --- | --- | --- | --- | --- |
| STT, sell | 0.15% of the premium (0.10% to 31 Mar 2026) | 0.05% of the traded value (0.02% to 31 Mar 2026) | 2026-04-01 | Finance Act 2026; [NSE/FATAX/73524](https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf) (31 Mar 2026) |
| STT, option exercised | 0.15% of the intrinsic (settlement) value, paid by the buyer (0.125% to 31 Mar 2026) | | 2026-04-01 | same |
| NSE transaction charge + IPFT | 0.03553% of the premium (₹3,553 a crore) | 0.00183% of the traded value (₹183 a crore) | 2024-10-01 | [NSE/FA/73061](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf) (27 Feb 2026: from 1 Mar 2026 the split is ₹3,552.99 + ₹0.01 and ₹182.99 + ₹0.01, the total unchanged) |
| SEBI turnover fee | ₹10 a crore | ₹10 a crore | | [Zerodha charges](https://zerodha.com/charges) |
| Stamp duty, buy | 0.003% of the premium | 0.002% of the traded value | | [Zerodha charges](https://zerodha.com/charges) |
| GST | 18% of brokerage + exchange + SEBI | same | | same |
| Brokerage (a typical discount broker) | ₹20 per executed order | lower of 0.03% and ₹20 per order | | same; set yours in `fno_charges` |

An exercised option has no sell order: the exercise leg pays STT only.

## Calendar

The 2026 NSE trading holidays ship in the tools. NSE publishes each year's
list in December; until a release adds it, put the dates in
`extra_holidays` in `trader/config.json`. Muhurat trading and other special
sessions go in `special_sessions` with the timings NSE announces.

## Tick sizes

NSE assigns each stock a tick by price band and reviews it periodically. With
`tick_size: "auto"`, the tools round to the coarsest tick the price band can
have (₹0.05 under ₹1,000, ₹0.10 under ₹5,000, ₹0.50 under ₹10,000, ₹1 under
₹20,000, ₹5 above). A multiple of a coarse tick is always valid on a finer
grid, so these prices are accepted even near a band edge. The risk check
accepts anything on the finest plausible grid (₹0.01 under ₹250, otherwise
₹0.05). Set a number to override both.
