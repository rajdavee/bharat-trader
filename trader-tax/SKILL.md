---
name: trader-tax
description: >-
  A financial-year tax ESTIMATE of your NSE trading, to take to your
  chartered accountant: every trade classified as intraday equity
  (speculative business income), futures and options (non-speculative
  business income) or delivery shares (short-term capital gain at 20%
  under s.111A, long-term at 12.5% above Rs 1.25 lakh under s.112A, with
  the 31 Jan 2018 grandfathering), charges allocated the way each head
  allows (STT deductible for business, not for capital gains), turnover
  computed the ICAI Guidance Note way (the absolute sum of profits and
  losses, the options-premium question explained) with the s.44AB audit
  check and the s.44AD presumptive interplay in plain words, current-year
  set-off and brought-forward losses (speculative 4 years, other business
  and capital losses 8 years), what carries forward and until when, the
  special-rate amounts with their rates, the advance-tax dates and the
  ITR-3 schedule each number goes to; CSV and markdown export to
  trader/tax/FY2025-26/. FY 2025-26 under the Income-tax Act, 1961 and
  tax year 2026-27 under the Income-tax Act, 2025, every rule dated with
  its section and source. Use when the user says "tax report", "F&O tax",
  "intraday tax", "speculative income", "capital gains on my shares",
  "LTCG STCG", "tax P&L for my CA", "ITR-3 kaise bharun", "F&O turnover",
  "do I need a tax audit", "44AB audit", "carry forward my F&O loss",
  "advance tax kab bharna hai", "mera tax kitna banega" or "tax ka hisaab
  nikalo". Not tax advice; it never places, sizes or confirms an order.
argument-hint: "[report --fy 2025-26 [--book live|paper|all] | export --fy 2025-26 | rules --fy 2026-27 | years | import FILE --broker NAME]"
---

# trader-tax

> **An estimate for discussion with your CA, not tax advice.** Every rule
> carries its section, its source and the date it took effect; laws, forms
> and ICAI guidance change. Slab tax, surcharge, cess, rebates and
> deductions are not computed: they depend on your whole income.

`<skill dir>` is this skill's folder. Money in rupees; a financial year
(FY) runs 1 April to 31 March.

```bash
python3 "<skill dir>/tax.py" report --fy 2025-26                 # income per head, turnover, audit, set-off
python3 "<skill dir>/tax.py" report --fy 2025-26 --book paper    # a practice report on paper fills
python3 "<skill dir>/tax.py" export --fy 2025-26                 # trader/tax/FY2025-26/: report.md + CSVs
python3 "<skill dir>/tax.py" rules --fy 2026-27                  # the rules used, with sections and sources
python3 "<skill dir>/tax.py" years                               # the financial years that have fills
python3 "<skill dir>/tax.py" import tradebook.csv --broker zerodha   # trader-journal's importer
```

`--tradebook FILE [--broker NAME]` reads one more broker file without
storing it, `--holdings FILE` replaces `trader/tax/holdings.csv`, `--now`
pins the clock, `--json` on every command.

## Where the numbers come from

| source | what |
| --- | --- |
| `trader/journal/imports/<broker>.json` | your broker's trade book, imported with `tax.py import` or `journal.py import` (Zerodha, Kite, Upstox, Dhan, Angel One, Groww: trader-journal's importers) |
| `trader/state/trades.json` | the paper broker's fills, only with `--book paper` or `all`: a practice report, labelled so |
| `trader/tax/holdings.csv` | lots bought before your trade book starts: `symbol, qty, buy_date, buy_price, fmv_2018, buy_charges` (`fmv_2018`: the highest price on 31 Jan 2018, for shares bought before 1 Feb 2018) |
| `trader/tax/carry-forward.csv` | losses your filed returns carried (Schedule CFL): `fy, kind, amount` with kind `speculative`, `non_speculative`, `stcl` or `ltcl` (`filed_on_time` no drops a row) |
| `trader/tax/expenses.csv` | `fy, head, amount, note`: internet, software, advisory; head `speculative`, `non_speculative` or `both` (split by turnover) |
| `trader/tax/profile.json` | `cash_over_5pct` (default false: broker trading is digital), `presumptive_years` (FYs you used s.44AD), `carry_forward_as_of` |

Charges come from the broker file when it carries them (Dhan) and are
estimated with trader-broker's model otherwise (marked; use your contract
notes for the return). Years after the last one in `carry-forward.csv`
are chained here from the trade book.

## How each trade is classified

- **Intraday equity**: the quantity of a stock both bought and sold on the
  same day (CNC or MIS), settled without delivery: speculative business
  income (s.43(5), s.28 Explanation 2; 2025 Act s.66(31), s.26(3)).
- **F&O**: futures and options on a recognised exchange are not
  speculative (s.43(5) proviso (d); 2025 Act s.66(31)(a)): non-speculative
  business income. Matched FIFO per contract. An option a broker file
  never shows closing is taken as expired worthless on its expiry date and
  listed under "needs your input" (an in-the-money expiry has a settlement
  on your contract note). A position open on 31 March counts in the year it
  is squared off.
- **Delivery**: shares held as investments, matched FIFO against your lots
  (holdings first). Held more than 12 months: long-term (s.2(42A); 2025 Act
  s.2(101)). Short-term gain at 15% before 23 Jul 2024 and 20% from it
  (s.111A / s.196); long-term at 10% then 12.5% above the yearly exemption
  (Rs 1 lakh, Rs 1.25 lakh from FY 2024-25) (s.112A / s.198). Shares bought
  before 1 Feb 2018: cost = higher of actual cost and the lower of the 31 Jan
  2018 value and the sale value (s.55(2)(ac) / s.90(7)).
- **Charges**: business heads deduct every charge, STT included
  (s.36(1)(xv) / s.32(k)). Capital gains may not deduct STT (s.48 /
  s.72(3)(b)); the other charges are part of the cost (buy) or transfer
  expenses (sell).

## Turnover and the audit check

Turnover follows ICAI's Guidance Note on Tax Audit (Revised 2026, para
5.11): the absolute sum of each squared-off trade's profit or loss, for F&O
and for intraday (a loss adds to turnover like a profit). The Note also
names the premium received on selling options, but not separately where it
is already in the net profit; for a squared-off option it is, so it is not
added, and the report shows the higher figure in case your CA adds it.
Delivery shares held as investments are not business turnover.

The audit check, in words: over Rs 10 crore (Rs 1 crore if cash receipts
or payments exceed 5%) an audit is required (s.44AB(a) / 2025 Act s.63).
Under it, a loss or a profit under 6% of turnover requires an audit only if
you used the presumptive scheme in one of the last five years and your
total income exceeds the basic exemption (s.44AB(e) with s.44AD(4), (5) /
s.58(7), (8)). Whether F&O and intraday trading may use s.44AD at all is
debated: that is your CA's call.

## Set-off and carry-forward

This year: a short-term capital loss against any capital gain, a long-term
loss only against long-term gains; an F&O loss against intraday income and
then against capital gains (and your other income except salary, which the
report does not see); a speculative loss only against speculative income.
Brought forward, oldest first: speculative losses for 4 years against
speculative income only, other business losses for 8 years against business
income, capital losses for 8 years. A loss carries forward only if the
return is filed by its due date (s.80 / 2025 Act s.121). Losses from FY
2025-26 and earlier keep the 1961 Act's rules after 1 Apr 2026 (s.536).

## The report

Income per head (gross, charges, expenses, net), turnover and the audit
status with its reasons, every set-off step, the carry-forward table with
"usable until", what lapsed, the taxable amounts per head, the special-rate
amounts with their rates (before the basic-exemption shortfall, surcharge
and cess), the four advance-tax dates with your realised results to each
date, the ITR-3 schedules (Schedule BP, CG, 112A, CYLA, BFLA, CFL, SI, IT,
Part A-P&L, Part A-GEN), what needs your input and the rules table.
`export` writes `report.md`, `summary.csv`, `trades.csv`,
`carry-forward.csv` and `tax.json` to `trader/tax/FY2025-26/`.

From an AI assistant, the MCP server's `tax_summary` tool (read only) gives
the same report; the UI's Tax page (under Review) has the FY picker. Full
rules table with links: `docs/tax.md`.

## Limits

An estimate from your fills: corporate actions (splits, bonuses,
demergers), IPO and off-market shares, mutual funds, dividends, buy-backs,
commodity and currency derivatives, unabsorbed depreciation and
non-resident rules are not handled. The ITR-3 for tax year 2026-27 and
ICAI's audit guidance under the 2025 Act were not out on 1 Nov 2026: the
current layout and method are shown. Reporting only: it reads records and
writes under `trader/tax/`. It never places, sizes or confirms an order.
Not tax advice.
