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

## Known quirks

- **Index symbols** (`^NSEI`, `^NSEBANK`, `^INDIAVIX`) go to Yahoo without `.NS`.
- **`M&M`** and other symbols with `&` must be URL-encoded (`M%26M.NS`).
- **Yahoo's 09:15 bar reports zero volume** for NSE stocks; volume checks leave it out.
- **Yahoo leaves late bars empty** (often 15:15–15:25) and occasionally drops one mid-session; the Time Machine keeps sessions that are complete up to your flat-by time.
- **Yahoo rate-limits** with HTTP 429 after heavy use; the tools back off and retry, then report it.
- **NSE needs a browser-like User-Agent** and a `Referer` of `https://www.nseindia.com/`. No cookies are needed today.
- **Corporate announcements without a date window** return thousands of rows; the tools always send `from_date`/`to_date`.
- **Old-format results XBRL** places nine-month figures in contexts dated like the quarter; the parser picks the shortest plain period ending on the reporting date.
- **Banks** use a separate XBRL template (bank flag `B`); the analyst shows NII, operating profit and provisions instead of EBITDA.
- **The historical bulk-deals JSON caps at 70 rows**; the CSV form returns all of them. NSE's bulk list repeats block trades; they are kept once, labelled block.
- **PIT holding percentages** in the XBRL are fractions (0.3049 = 30.49 %). Some filers enter the per-share price as the value; those rows are flagged and left out of totals.

- **Option chain v3** returns only the expiry asked for. A wrong expiry returns HTTP 200 with no rows and spot 0. `openInterest` and `totalTradedVolume` are in contracts (x lot = units), while the bhavcopy's open interest is in shares. `optionType` is always null. NSE's `impliedVolatility` disagrees between calls and puts and is 0 for skipped strikes; `trader-options` computes its own.
- **Spot after 15:30.** The cash index stops at 15:30 while equity derivatives trade until 15:40, so the chain's `underlyingValue` is stale against option prices in that window.

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
