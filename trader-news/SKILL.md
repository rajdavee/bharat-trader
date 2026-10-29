---
name: trader-news
description: >-
  NSE News Monitor for Indian stocks. Tracks what the company told the
  exchange and what the press is saying for every NSE stock you watch or
  hold: NSE corporate announcements (results, board meeting outcomes,
  dividend, bonus, split, buyback, rights, QIP and fund raising, acquisition,
  merger and demerger, order wins, credit ratings, SEBI and regulatory
  orders, promoter pledge, SAST and insider disclosures, management and
  auditor changes), upcoming NSE board meetings, Yahoo Finance India
  headlines, optional Nifty 50 and macro news (RBI, repo rate, inflation,
  budget, FII flows) and how far each stock is from the previous close. Shows
  only what is new since the last check, newest first, in IST, tags each item
  and never repeats one. Use when the user says "any news on Reliance", "kya
  news hai TCS pe", "why is HDFCBANK falling", "why is this stock moving",
  "news on my watchlist", "morning briefing", "pre-market news", "check NSE
  announcements", "any corporate action on Infosys", "when are SBIN results",
  "board meeting date", "RBI policy news", "Nifty news", or after
  trader-scanner finds intraday setups.
argument-hint: "[NSE symbols, space or comma separated]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
SEBI order or a promoter pledge. Before a setup becomes a trade, this skill
reads the feeds that carry those first, for every stock on the watchlist,
and tells you only what is new. It also prints how far each stock has moved
today, so a filing and a move sit side by side.

`<skill dir>` below is this skill's folder in your agent's skills folder
(where this SKILL.md lives). The tool is standard library Python, no keys:

```bash
python3 "<skill dir>/news.py"                                 # watchlist + plan + portfolio
python3 "<skill dir>/news.py" --tickers RELIANCE TCS --hours 12
python3 "<skill dir>/news.py" --tickers RELIANCE,HDFCBANK,M&M  # commas work too
python3 "<skill dir>/news.py" --tickers SBIN --market          # plus Nifty 50 / macro headlines
python3 "<skill dir>/news.py" --no-headlines --json            # NSE filings only, machine readable
```

Symbols are NSE symbols (`RELIANCE`, `M&M`, `BAJAJ-AUTO`; a trailing `.NS`
is dropped). An index works in Yahoo form for headlines and the move:
`^NSEI` (Nifty 50), `^NSEBANK` (Bank Nifty).

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (`positions` keyed by symbol) |
| NSE corporate announcements | `nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE&from_date=..&to_date=..` |
| NSE board meetings | `nseindia.com/api/corporate-board-meetings?index=equities&symbol=RELIANCE`; future dates print as `UPCOMING` on top |
| Yahoo Finance headline RSS | `feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` (thin for many NSE names; an empty feed is normal) |
| Yahoo Finance chart endpoint | last price against the previous close (unofficial, delayed) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Filings beat headlines.** An NSE announcement is what the company told
  the exchange under SEBI LODR. If a headline and a filing disagree, the
  filing wins. Open the PDF link before telling the user what it says.
- **Headlines are leads, not facts.** Quote the source and link it. Never
  summarise a paywalled story you could not read: say so.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: results, board meeting, dividend, bonus, split, buyback, rights, fund
  raising, deal, order win, credit rating, regulatory, pledge, insider, block
  deal, management, auditor, macro. NSE items marked `!` carry a tag that can
  move the price on its own (results, board meeting, fund raising, deal,
  order win, credit rating, regulatory, pledge, auditor, buyback, bonus,
  split, rights). The standard "Regulation 30 of SEBI (LODR)" wording alone
  does not count as regulatory.
- **UPCOMING board meeting** on or before the trade day means results or a
  corporate action can land mid-trade or overnight. Say it first.
- **MOVING** marks a stock 3 % or more from the previous close (`--move-pct`).
  A move with no filing and no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "<skill dir>/news.py"`. Exit code 1 means nothing
   new. `--all` shows items already seen, `--hours 72` widens the window
   (default 48), `--json` gives everything machine readable, `--dry-run`
   leaves the state alone, `--market` adds Nifty 50 and macro headlines.
2. Read the items that matter: every `UPCOMING` board meeting, every `!`
   filing, anything tagged results, regulatory, pledge, auditor or insider,
   and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "board
   meets tomorrow for Q2 results: an overnight gap can jump past any stop,
   so this stays an intraday trade, flat by 15:10").
4. If a filing changes the picture, say so before trader-entry and
   trader-exit: results inside the trade, a SEBI order, a QIP at a discount,
   a pledge invocation. Never act on a headline alone.

During the session, run it every 15 minutes with your agent's loop or
scheduler, or once each morning before the 09:00 pre-open.

## Example

Thursday 2026-10-29, checked at 16:30 IST for RELIANCE (real data):

```
RELIANCE   ₹1,167.40 +0.81% vs previous close ₹1,158.00 at 15:29 IST

UPCOMING RELIANCE board meeting 2026-10-30: Financial Results/Fund Raising

! 2026-10-29 08:55 IST  RELIANCE    NSE           Other Restructuring: Voluntary strike off of Hamleys (Franchising) Limited
      tags: deal  https://nsearchives.nseindia.com/corporate/...pdf
```

The brief: "Reliance's board meets tomorrow (30 Oct) on Q2 results and fund
raising, so any position is closed today by 15:10; no overnight hold. This
morning's filing is a subsidiary strike-off, small for the stock." When a
symbol has no news, say so rather than padding the brief.

## If a source fails

- NSE answers 401/403 or an HTML page when it blocks a request: the tool
  prints a `note:` line and carries on with the other feeds. Wait a few
  minutes before trying again; never loop on it.
- A 429 (too many requests) is retried twice with a pause, then reported.
- An empty Yahoo feed (common for RELIANCE, HDFCBANK) is not an error.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline or a filing.
- Never hammers a server: one request per feed per symbol, a pause between
  calls, backoff on 429.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing in this pack places an order except trader-broker, after
trader-risk passes that exact order and the user types an explicit yes for
it. Paper trading is the default.
