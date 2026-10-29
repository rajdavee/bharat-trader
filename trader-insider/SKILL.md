---
name: trader-insider
description: >-
  The Insider and Big-Money Tracker for NSE. Reads the public disclosures
  that show who is buying and selling: today's (or any day's) bulk and block
  deals with client names, and SEBI PIT insider trading disclosures by
  promoters, promoter group, directors and designated employees (who, what,
  how many shares, value in ₹ crore, market purchase or pledge, how fast it
  was disclosed). Flags deals and disclosures in stocks you hold
  (trader/portfolio.json) or watch (trader/watchlist.json), highlights
  well-known investors you list, and alerts once per new item. Use when the
  user says "what are promoters buying", "promoter buying", "insider trading
  in Reliance", "insider buying this week", "bulk deals today", "block deals
  today", "who bought Swiggy in a bulk deal", "big deals today", "did any big
  investor buy", "what is Kacholia buying", "track promoter selling", "PIT disclosures",
  "SEBI insider disclosures", "alert me on bulk deals in my stocks", or "run the
  insider skill".
argument-hint: "deals|insider|watch [--symbol X]"
---

# trader-insider

Two kinds of public disclosure show money moving in a stock:

- **Bulk and block deals.** A bulk deal is one client trading more than 0.5%
  of a company's shares in a day; a block deal is a single trade of ₹10 crore
  or more in the exchange's block window. NSE publishes both after the close,
  with the client's name, quantity and price.
- **SEBI PIT disclosures.** Under the SEBI (Prohibition of Insider Trading)
  Regulations, 2015, promoters, directors, designated employees and their
  immediate relatives must tell the company within 2 trading days when they
  trade more than ₹10 lakh of its shares in a quarter, and the company must
  tell the exchange within 2 trading days of hearing. NSE publishes each
  filing.

This skill reads both, sorts them, flags what touches your stocks, and turns
the filings into plain rows. **These are public disclosures, not inside
information.** Every item is after the fact: a deal is printed after the
close; a PIT trade can be four or more trading days old when it appears.
Reading them fast is useful context. It is not an edge, and it is never a
reason to trade on its own. Say that every time.

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "<this skill folder>/insider.py" --help
```

(`<this skill folder>` is wherever this skill sits in your agent's skills
folder.)

## What it reads and writes

- `https://nsearchives.nseindia.com/content/equities/bulk.csv` and
  `.../block.csv`: the latest day's deals (columns Date, Symbol, Security
  Name, Client Name, Buy/Sell, Quantity Traded, Trade Price / Wght. Avg.
  Price, Remarks).
- `https://www.nseindia.com/api/historicalOR/bulk-block-short-deals`
  (`optionType=bulk_deals` or `block_deals`, `from`/`to` as DD-MM-YYYY,
  `csv=true`) for earlier days. The JSON form of this page stops at 70 rows,
  so the tool asks for the CSV.
- `https://www.nseindia.com/api/corporates-pit-gg?index=equities` with
  `from_date`/`to_date` (and `symbol`): PIT filings from May 2026 on, one row
  per filing with a link to its XBRL. The tool opens each XBRL for the people,
  category, instrument, buy / sell / pledge, quantity, value, mode
  (market purchase, off market, ESOP, gift, inter-se transfer, pledge),
  trade dates, holdings before and after.
- `https://www.nseindia.com/api/corporates-pit` with the same parameters: the
  older PIT list (filings up to early May 2026), one transaction per row.
- Reads `trader/portfolio.json` (positions) and `trader/watchlist.json`
  (hits) to tag items HELD or WATCH, and `trader/config.json`:
  `insider_watch_clients` (a list of names, matched case-insensitively inside
  the client name; tagged NOTABLE) and `insider_alert_min_cr` (default 25).
- Writes `trader/insider-seen.json` in `watch` mode: the ids already shown, so
  each item alerts once.

Value is quantity x price for deals and the value the filer reported for PIT
transactions, in ₹ crore with Indian digit grouping.

## Commands

```bash
python3 "<this skill folder>/insider.py" deals                        # latest day
python3 "<this skill folder>/insider.py" deals --symbol STLTECH
python3 "<this skill folder>/insider.py" deals --days 7 --min-value-cr 50
python3 "<this skill folder>/insider.py" deals --client "kacholia" --days 30
python3 "<this skill folder>/insider.py" deals --mine                 # only your stocks
python3 "<this skill folder>/insider.py" insider --days 7 --promoters-only
python3 "<this skill folder>/insider.py" insider --symbol RELIANCE --days 365
python3 "<this skill folder>/insider.py" insider --days 3 --market-only
python3 "<this skill folder>/insider.py" watch                        # new since last check
```

`--json` on any command prints everything machine-readable. `insider`
opens at most `--max-filings` XBRL files (default 60, your stocks first) so a
busy week stays polite; add `--symbol` for one company's full history.

## The rules

- **Quote the disclosure.** Every row comes from NSE's file or the filing's
  XBRL, with its link in `--json`. Third-party trackers can be off; the
  filing wins.
- **A pledge is not a sale, a gift is not a purchase.** Only market purchases
  and market sales count toward "net buying". Pledge creation, invocation and
  revocation, ESOP allotments, inter-se transfers between promoter entities,
  preferential allotments and inheritance are shown, but say what they are.
  An invoked pledge can mean a lender sold the shares.
- **Check the value.** Some filers type the per-share price in the value
  field. When value / quantity is under ₹1 the tool says "looks like a
  per-share price" and leaves it out of the totals.
- **Matched trades.** In a block deal, or a bulk deal with one buyer and one
  seller at the same quantity and price, one party sold to the other. Say so
  ("SERUM INSTITUTE sold 6,01,900 STLTECH shares to POONAWALLA AVIATION, same
  group"), it is often a transfer, not new money.
- **Arbitrage desks are noise.** High-frequency and arbitrage firms (they buy
  and sell almost the same quantity the same day) top the bulk-deal list on
  busy stocks. A buy and a sell by the same client is not a view.
- **Disclosure speed.** Each PIT row shows weekdays from the trade to the
  exchange filing; more than 4 (2 + 2 trading days) is marked "late?"
  (exchange holidays are not removed, so check before calling it late).

## Steps

1. Run what the user asked: `deals` for bulk/block, `insider` for promoter
   and director trades, both for "what is smart money doing in X".
2. Lead with items tagged HELD or WATCH, then NOTABLE clients, then the
   biggest values. For PIT: promoter market purchases and sales first, then
   directors, then pledges.
3. Summarise in plain words with the numbers: who, buy or sell, how many
   shares, value in ₹ crore, price, when traded, when disclosed, holding
   before and after.
4. If the user wants to act on it, it is a separate decision: the trade still
   goes `/trader-budget` (size) -> `/trader-risk` (checks) -> `/trader-broker`
   (ticket), and nothing is placed without the user's typed yes.
5. For alerts, run `watch` on a schedule (after 18:00 IST for the day's deals;
   PIT filings arrive through the day). Each new item is printed once.

## Example

`insider.py deals` on 2026-10-29 (the 28-Oct files): STLTECH block deal,
SERUM INSTITUTE OF INDIA sold 6,01,900 shares to POONAWALLA AVIATION at
₹887.00 (₹53.39 cr), a matched transfer inside one group; CARTRADE bulk
deals by HRTI PRIVATE LIMITED on both sides, an arbitrage desk.

`insider.py insider --days 4 --promoters-only`: WESTLIFE promoter Horizon
Impex bought 10,65,000 shares in the market on 27-28 Oct for ₹63.90 cr
(about ₹600 a share), holding 30.49% -> 31.18%, disclosed a day later.
PAISALO promoter group created a pledge on 1,03,63,523 shares: a pledge,
not a sale.

## What it never does

- Never calls a disclosure inside information, or suggests anyone traded on
  it. These are public filings, read after the fact.
- Never tells the user to copy a trade. Never invents a value the filing does
  not have.
- Never places an order. It reads public files only.

## Safety

Education and tooling, not financial advice. Bulk deals and PIT disclosures
are public information published after the trade; acting on them is the
user's own decision. Any trade still goes `/trader-budget` -> `/trader-risk`
-> `/trader-broker`, paper by default, and nothing is placed without the
user's typed yes for that order.
