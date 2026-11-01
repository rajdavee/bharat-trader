# The tax estimate

> **An estimate for discussion with your chartered accountant, not tax
> advice.** It classifies your trades, adds them up the way the law and
> ICAI's guidance describe, and shows every rule with its section, source
> and effective date. It does not compute slab tax, surcharge, cess,
> rebates or deductions: they depend on your whole income. Laws, forms and
> guidance change; the rules below were read on 1 November 2026.

`trader-tax` turns a financial year (FY, 1 April to 31 March) of trades into
the numbers a CA asks for: income per head, the turnover for the audit
check, what can be set off and what carries forward, the special-rate
amounts, the advance-tax dates and the ITR-3 schedule each number goes to.
It reads; it never places, sizes or confirms an order.

```bash
python3 trader-tax/tax.py import tradebook.csv --broker zerodha   # your broker's trade book (trader-journal's importer)
python3 trader-tax/tax.py report --fy 2025-26
python3 trader-tax/tax.py export --fy 2025-26                    # trader/tax/FY2025-26/
python3 trader-tax/tax.py rules --fy 2026-27
python3 trader-tax/tax.py report --fy 2026-27 --book paper       # practice, on paper fills
```

## Inputs

- **Trades.** The live book is your broker's trade book, imported with
  `tax.py import` or `journal.py import` (Zerodha Console tradebook, Kite,
  Upstox, Dhan, Angel One, Groww; see [journal.md](journal.md)). `--book
  paper` reads the paper broker's fills instead: a practice report,
  labelled so. Charges come from the file when it has them (Dhan) and are
  otherwise estimated with trader-broker's model and marked; use your
  contract notes' figures for the return.
- **`trader/tax/holdings.csv`**: lots bought before your trade book starts
  (`symbol, qty, buy_date, buy_price, fmv_2018, buy_charges`). For shares
  bought before 1 Feb 2018, `fmv_2018` is the highest price on NSE on
  31 Jan 2018 (in that day's bhavcopy, column HIGH).
- **`trader/tax/carry-forward.csv`**: the losses your filed returns carry
  (Schedule CFL): `fy, kind, amount`, kind one of `speculative`,
  `non_speculative`, `stcl`, `ltcl`; `filed_on_time` `no` marks a loss that
  cannot be carried. Years after the latest one are worked out from the
  trade book (said so in the report).
- **`trader/tax/expenses.csv`**: `fy, head, amount, note` for internet,
  software, advisory and similar costs incurred for the trading; head
  `speculative`, `non_speculative` or `both` (split by turnover). Keep the
  bills; your CA decides what is allowable.
- **`trader/tax/profile.json`**: `{"cash_over_5pct": false,
  "presumptive_years": ["2022-23"], "carry_forward_as_of": "2024-25"}`.

## How it classifies

1. **Equity, per stock per day.** The quantity both bought and sold that day
   is intraday (speculative), whatever the product (MIS or CNC): it is
   settled without delivery. The net is delivery: a net buy opens a lot, a
   net sell is matched FIFO against your lots (`holdings.csv` first, then the
   trade book's). A sale with no lot is left out and listed under "needs
   your input". Buy-today-sell-tomorrow (BTST) counts as delivery; some CAs
   treat it otherwise.
2. **F&O, per contract, FIFO.** Each opening lot a fill closes is one
   squared-off trade with its own difference. A position still open on
   31 March counts in the year it is squared off. An option the broker file
   never shows closing is taken as expired worthless on its expiry date and
   flagged (an in-the-money expiry has a settlement on your contract note).
3. **Holding period.** Listed shares are long-term when held *more than*
   12 months: bought 5 Nov 2024, sold 5 Nov 2025 is short-term; sold 6 Nov
   2025 is long-term.
4. **Charges.** Business heads deduct every charge, STT included. For
   capital gains STT is not deductible; brokerage, exchange, SEBI and GST on
   the buy are part of the cost, on the sale transfer expenses; stamp duty
   is part of the cost.

## Turnover and the audit check

ICAI's Guidance Note on Tax Audit, para 5.11 (Revised 2026, the 11th
edition, issued 31 Jul 2026 for audits of FY 2025-26):

- speculative (intraday): "the aggregate of both favourable and
  unfavourable differences";
- derivatives: (i) "the total of favourable and unfavourable differences in
  case of squared off transactions"; (ii) "premium received on sale of
  options is also to be included in turnover. However, where the premium
  received is included for determining net profit for transactions, then
  such net profit should not be separately included"; (iv) an open position
  counts in the year it is squared off;
- delivery: the sale value, where the shares are business stock.

The report's turnover is the absolute sum of each squared-off trade's
profit or loss (a loss adds like a profit). For a squared-off option the
premium is already inside that difference, so it is not added again: the
reading most CAs and broker tax reports use since the 2022 edition. The
report also prints the higher figure with the premium received on option
sales added, in case your CA reads (ii) the other way. Delivery shares held
as investments are capital gains, not business turnover; if your CA treats
them as stock-in-trade, their sale value (printed) becomes turnover and the
gains business income.

Then, in words: over Rs 10 crore (Rs 1 crore when cash receipts or payments
exceed 5%), an audit is required. Under it, a loss or a profit under 6% of
turnover needs an audit only if you used the presumptive scheme (s.44AD) in
one of the five previous years and your total income exceeds the basic
exemption limit. Whether F&O and intraday trading may use s.44AD at all is
debated; it is your CA's call.

## Set-off and carry-forward, in the order applied

1. Capital gains: a short-term loss against short-term and then long-term
   gains; a long-term loss only against long-term gains.
2. Business: an F&O loss against intraday income. Never the reverse: a
   speculative loss only offsets speculative income.
3. Across heads: an F&O loss left over against capital gains (it can also go
   against your other income except salary, which the report does not see).
4. Brought-forward losses, oldest first: speculative for 4 years against
   speculative income; other business losses for 8 years against business
   income (F&O first, then intraday); short-term capital losses for 8 years
   against any capital gain; long-term only against long-term. A lapsed or
   late-filed loss is listed with the reason.
5. What is left carries forward, with the last year it can be used.

The order between gains taxed at different rates is the one that leaves
the least at the higher rate; your CA can choose otherwise where the law
allows.

## The rules, dated, with their sources

Read on 1 Nov 2026. 1961 Act sections apply to FY 2025-26 and earlier; the
Income-tax Act, 2025 applies from tax year 2026-27 (1 Apr 2026).

| what | rule | 1961 Act | 2025 Act | effective | source |
| --- | --- | --- | --- | --- | --- |
| Intraday equity | speculative transaction: settled otherwise than by delivery; a speculation business is distinct from other business | s.43(5), s.28 Explanation 2 | s.66(31), s.26(3) | long-standing | the Acts' text (1) (2) |
| F&O | a derivative traded on a recognised exchange is not speculative | s.43(5) proviso (d) | s.66(31)(a), s.66(33) | from AY 2006-07 | (1) (2) |
| Holding period | listed shares long-term when held more than 12 months | s.2(42A) | s.2(101)(b) | long-standing (12 months for listed shares) | (1) (2) |
| STCG rate | 15% on transfers before 23 Jul 2024; 20% from 23 Jul 2024 | s.111A | s.196 | 23 Jul 2024 | Finance (No. 2) Act, 2024 (3); (2) |
| LTCG rate | 10% before 23 Jul 2024; 12.5% from 23 Jul 2024, above the yearly exemption | s.112A | s.198 | 23 Jul 2024 | (3); (2) |
| LTCG exemption | Rs 1 lakh a year; Rs 1.25 lakh from FY 2024-25 (for the whole year) | s.112A | s.198(2)(a) | FY 2024-25 | (3); (2) |
| Grandfathering | cost of shares bought before 1 Feb 2018: higher of actual cost and the lower of the 31 Jan 2018 highest price and the sale value | s.55(2)(ac) | s.90(7)-(8) | FY 2018-19 | Finance Act, 2018; (2) |
| STT, business | deductible when the income is business income | s.36(1)(xv) | s.32(k) | FY 2008-09 | (1) (2) |
| STT, capital gains | not deductible | s.48 proviso | s.72(3)(b) | long-standing | (1) (2) |
| Current-year set-off | within a head; business loss not against salary; capital loss not against other heads | s.70, s.71, s.71(2A), s.71(3) | s.108, s.109 | | (1) (2) |
| Speculative loss | only against speculative income; 4 years | s.73 | s.113 | | (1) (2) |
| Other business loss | against business income; 8 years | s.72 | s.112 | | (1) (2) |
| Capital loss | STCL against any gain, LTCL against LTCG; 8 years | s.74 | s.111 | | (1) (2) |
| Loss return | carried forward only if the return is filed by the due date | s.80, s.139(3) | s.121, s.263(1) | | (1) (2) |
| Losses from before 1 Apr 2026 | keep the repealed Act's rules and periods | | s.536(2)(m), (n) | 1 Apr 2026 | (2) |
| Tax audit | business turnover over Rs 1 crore; Rs 10 crore when cash receipts and payments are each 5% or less | s.44AB(a) | s.63 Table Sl. 1 | Rs 10 crore from FY 2020-21 / 2021-22 | Finance Acts 2020, 2021; (2) |
| Audit after presumptive | used 44AD in the last 5 years, now lower profit, income above the basic exemption | s.44AB(e), s.44AD(4), (5) | s.58(7), (8), s.63 Sl. 2 | FY 2016-17 | Finance Act, 2016; (2) |
| Presumptive limit | Rs 2 crore; Rs 3 crore when cash received is 5% or less; 6% of digital turnover | s.44AD | s.58(2) Table Sl. 1 | Rs 3 crore from FY 2023-24 | Finance Act, 2023; (2) |
| Turnover method | absolute differences; option premium only where not in the net profit; open positions in the year closed | s.44AB | s.63 | 11th edition, Jul 2026 | (4) |
| Advance tax | due when the year's tax is Rs 10,000 or more; 15% / 45% / 75% / 100% by 15 Jun / Sep / Dec / Mar; capital gains arising later go in the remaining instalments | s.208, s.211, s.234C | s.404, s.408 | | (1) (2) |
| Return form | ITR-3 (business income) | AY 2026-27: CBDT Notification 47/2026, 30 Mar 2026 | tax year 2026-27: not notified on 1 Nov 2026 | | (5) |
| F&O STT | options sold 0.15% of premium, futures 0.05%, exercise 0.15% (was 0.1%, 0.02%, 0.125%) | | | 1 Apr 2026 | Finance Act, 2026; trader-broker's dated table |

Sources:

1. Income-tax Act, 1961, as amended: incometaxindia.gov.in (the Act's
   section pages; the site blocked automated reads on 1 Nov 2026, so
   section numbers were checked against the 2025 Act's transition table and
   ICAI's Guidance Note).
2. Income-tax Act, 2025 (Act 30 of 2025, assent 21 Aug 2025, in force
   1 Apr 2026): the Income-tax (No. 2) Bill, 2025 as passed by Lok Sabha,
   [prsindia.org](https://prsindia.org/billtrack/the-income-tax-no2-bill-2025)
   (read 1 Nov 2026). Sections quoted: 2(101), 3, 26(3), 32(k), 58, 63,
   66(31), (33), 72(3), 90(7)-(8), 108-113, 121, 196, 198, 263, 404, 408, 536.
3. Finance (No. 2) Act, 2024: the new STCG and LTCG rates for transfers on
   or after 23 Jul 2024 and the Rs 1.25 lakh exemption; unchanged by the
   Finance Act, 2026 (Budget coverage, Feb 2026).
4. ICAI, Guidance Note on Tax Audit under Section 44AB of the Income-tax
   Act, 1961 (Revised 2026, Eleventh Edition, foreword dated 31 Jul 2026),
   para 5.11 and 5.12. Its preface says it is the last edition under the
   1961 Act and that guidance under the 2025 Act will follow.
5. CBDT Notification 47/2026 dated 30 Mar 2026 (ITR-3 for AY 2026-27),
   [taxguru.in summary](https://taxguru.in/income-tax/cbdt-notifies-revised-itr-3-ay-2026-27.html).

## ITR-3: where each number goes (AY 2026-27 layout)

| number | schedule |
| --- | --- |
| Intraday: turnover, gross profit, expenses, income | Part A-P&L (speculative business) and Schedule BP, part B |
| F&O: turnover, profit, expenses | Part A-P&L and Schedule BP, part A (the AY 2026-27 form reportedly has separate F&O fields) |
| Charges and expenses | Part A-P&L expenses |
| STCG on STT-paid shares | Schedule CG part A, item A2 (s.111A); Schedule SI |
| LTCG on STT-paid shares | Schedule 112A, scrip by scrip, with the 31 Jan 2018 value for older lots; Schedule CG part B, item B4; Schedule SI |
| Set-off | Schedule CYLA (this year), Schedule BFLA (brought forward), Schedule CFL (to carry forward) |
| Audit details | Part A-GEN |
| Advance tax paid | Schedule IT |

Item labels follow the AY 2025-26 and 2026-27 forms; check them in the
utility you file with.

## What could not be verified (check with your CA)

- The official 1961 Act pages and the ITR-3 PDF on incometaxindia.gov.in
  refused automated reads (HTTP 403) on 1 Nov 2026. The ITR-3 schedule and
  item names above are from the AY 2025-26 / 2026-27 forms as reported, not
  read from the PDF.
- How ICAI's para 5.11(b)(ii) applies to options: secondary sources
  disagree on whether the 2025 and 2026 editions "reinstated" the premium.
  The 2026 text quoted above is primary; the report shows both figures.
- Whether s.44AD may be used for F&O or intraday trading.
- The due date for a non-audit ITR-3: reported as 31 August from AY 2026-27
  (Finance Act, 2026, amending s.139(1)), against 31 July in the 2025 Act as
  passed (s.263(1)); audit cases 31 October, with a CBDT extension reported
  for AY 2026-27. The report does not print a due date.
- The 2025 Act's ITR forms and ICAI's audit guidance for tax year 2026-27:
  not out on 1 Nov 2026.
- The 2025 Act's section numbers were read from the Bill as passed by Lok
  Sabha (the Rajya Sabha passed it unamended); a later amendment (the Finance
  Act, 2026 or the Income-tax Rules, 2026) could renumber a clause.

## Not handled

Corporate actions (splits, bonuses, demergers: adjust `holdings.csv`
yourself), IPO, ESOP, gifted or off-market shares (STT on acquisition),
mutual funds and ETFs, dividends, buy-backs (capital gains from FY 2026-27),
commodity and currency derivatives, unabsorbed depreciation, non-residents
and companies, slab tax, surcharge, cess, rebates and Chapter VI-A
deductions.

## Elsewhere

The UI's **Tax** page (under Review) has the FY picker, the heads, the
turnover and audit check, the carry-forward table and the export button
(`GET /api/tax`, `POST /api/tax/export`). The MCP server's `tax_summary`
tool gives an assistant the same report, read only, with a note to present
it as an estimate for your CA.
