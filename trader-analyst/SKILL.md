---
name: trader-analyst
description: >-
  The Results Analyst for NSE stocks. Reads the quarterly financial results a
  company filed with NSE (the XBRL behind each results announcement, Ind-AS
  and the bank template) and sums up what changed: revenue from operations,
  EBITDA (approximate), operating and net margin, net profit and diluted EPS,
  quarter by quarter, each against the same quarter last year (YoY) and the
  quarter before (QoQ), with the trailing twelve months, all in ₹ crore on the
  Indian financial year (Q1 = Apr-Jun). Also checks the NSE board meetings
  calendar for the next results date. Use when the user says "Reliance
  results", "how did TCS do this quarter", "analyse HDFC Bank's numbers",
  "Q2 results", "quarterly results", "is revenue growing", "what changed this
  quarter", "compare margins", "profit up or down YoY", "when are Infosys
  results", "does it report this week", "results season", "sum up the
  earnings call", or before trading a stock around its results.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Results move stocks more than anything else on the calendar, and the press
release puts the best number first. This skill reads the numbers the company
actually filed with NSE, lines them up quarter by quarter, and says plainly
what changed. For a day trader it answers two questions before a trade: what
did the last results say, and is the next board meeting inside this trade?

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "<this skill folder>/facts.py" --help
```

(`<this skill folder>` is wherever this skill sits in your agent's skills
folder.)

## What it reads and writes

- **Results list.** NSE's integrated filing list
  (`/api/integrated-filing-results`, SEBI's integrated filing, from the March
  2025 quarter on) and the older results list
  (`/api/corporates-financial-results`, up to the December 2024 quarter). Each
  entry links an XBRL file on `nsearchives.nseindia.com`.
- **Each quarter's XBRL.** The filing holds the quarter and, from Q2 on, the
  year to date. The tool picks the period that ends on the reporting date and
  lasts three months, so it never mixes in year-to-date numbers.
- **Board meetings** (`/api/corporate-board-meetings`): the next meeting whose
  purpose mentions financial results, and whether it falls this week.
- **Writes** `trader/metrics.json` with `--metrics`: one row per company
  (latest quarter, YoY growth, margins, TTM revenue, profit and EPS, next
  results date). `metrics.example.json` in this folder is a real run for
  HDFCBANK, INFY, M&M, RELIANCE, SBIN and TCS on 2026-10-29.
- **Earnings calls.** The tool does not fetch transcripts. If the user pastes
  one (or the investor presentation), read it and sum up what management said
  changed: guidance, margins, demand, anything they declined to answer.

NSE needs no cookie for these pages; the tool sends a browser-like
User-Agent and the nseindia.com Referer, waits between requests and backs off
when NSE says it is busy.

## How to read the numbers

- **Amounts in ₹ crore** (1 crore = 1,00,00,000), Indian digit grouping
  (₹3,11,850 cr). EPS in rupees.
- **Indian financial year.** April to March. Q1 = Apr-Jun, Q2 = Jul-Sep,
  Q3 = Oct-Dec, Q4 = Jan-Mar. The quarter ending 30 Sep 2026 is Q2 FY27.
- **Consolidated by default** (what the papers quote). `--standalone` shows
  the listed company alone. A company with no consolidated results is shown
  standalone, and the tool says so.
- **Net profit** is profit attributable to owners of the parent when the
  filing has it, else profit for the period.
- **EBITDA is an approximation**: profit before tax + finance costs +
  depreciation and amortisation - other income. Companies define their own
  EBITDA in the press release; say which one you quote.
- **Banks** (HDFCBANK, SBIN, ICICIBANK...) file a different template. Revenue
  is total income; the tool shows net interest income (interest earned -
  interest expended) and operating profit before provisions instead of
  EBITDA, plus provisions.

## The rules

- **Filed numbers first.** The table comes from the XBRL the company filed.
  Use the press release or presentation for colour (guidance, segments), and
  say which numbers came from where.
- **Compare like with like.** Growth is against the same quarter last year.
  QoQ is shown but many businesses are seasonal (retail in the festive
  quarter, cement in the monsoon).
- **Flag one-offs.** The tool notes exceptional items, a large share of other
  income in profit (this quarter or the year-ago one, e.g. a stake sale), and
  EPS that moved differently from profit because the share count changed
  (bonus or split; NSE does not restate old EPS).
- **Say when data is missing.** No year-ago filing means no YoY, not zero.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`):

   ```bash
   python3 "<this skill folder>/facts.py" RELIANCE
   python3 "<this skill folder>/facts.py" TCS INFY HDFCBANK --metrics trader/metrics.json
   ```

   `--quarters 8` shows two years, `--standalone` the standalone numbers,
   `--json` everything machine-readable, `--no-calendar` skips the board
   meetings lookup.
2. Open the latest filing (the XBRL link, or the results PDF on the
   company's NSE page) when the numbers need context, and read it.
3. Write the summary: one line on the quarter (revenue, profit, EPS, growth),
   one on margins, one on anything unusual (a one-off, a template note, data
   missing), and the next results date. Plain words, real numbers, no
   adjectives the numbers do not support.
4. If the next results fall inside the planned trade (an overnight or
   delivery hold), say it before `/trader-exit` sets the stop: a stop does not
   protect against a gap, the stock can open past it.

## Example

`facts.py TCS` (run 2026-10-29): Q2 FY27 (Jul-Sep 2026) revenue ₹73,188 cr,
+11.2% YoY and +1.3% QoQ; net profit ₹13,884 cr (+15.0% YoY); diluted EPS
₹38.37 (+15.0%); EBITDA (approx.) ₹18,815 cr, margin 25.7%. The tool notes
the year-ago quarter had exceptional items, so profit growth is not quite
like for like.

`facts.py RELIANCE` (same day): Q1 FY27 revenue ₹3,11,850 cr (+25.4% YoY),
net profit ₹20,946 cr (-22.4% YoY). The tool flags that other income was 41%
of profit before tax a year earlier, so the profit fall is mostly a high
base. The board meets on 30-Oct-2026 for the September quarter results:
"tomorrow, this week". The summary ends: "Results are due tomorrow. An
intraday trade that is flat by 15:10 is not exposed to them; anything held
overnight is."

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filing does not have, or fills a missing
  quarter.
- Never hammers NSE: one request at a time, a pause between them.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/trader-broker`, after `/trader-budget` sizes
it, `/trader-risk` passes it and the user types an explicit yes for that
order.
