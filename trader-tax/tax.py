#!/usr/bin/env python3
"""trader-tax: a financial-year tax ESTIMATE of your trading, to discuss with a chartered accountant.

    python3 tax.py report --fy 2025-26                 # income per head, turnover and audit check, set-off
    python3 tax.py report --fy 2025-26 --book paper    # a practice report on the paper broker's fills
    python3 tax.py export --fy 2025-26                 # trader/tax/FY2025-26/: report.md and CSVs
    python3 tax.py rules --fy 2026-27                  # every rule used, with its section, source and date
    python3 tax.py years                               # the financial years that have fills
    python3 tax.py import tradebook.csv --broker zerodha   # trader-journal's importer (one shared store)

What it does, per trade (Income-tax Act, 1961 up to FY 2025-26; Income-tax Act, 2025 from tax year
2026-27, which began on 1 April 2026):
  intraday equity   bought and sold the same day, settled without delivery: speculative business
                    income (s.43(5), s.28 Explanation 2 / 2025 Act s.66(31), s.26(3))
  F&O               futures and options on a recognised exchange: non-speculative business income
                    (s.43(5) proviso (d) / 2025 Act s.66(31)(a) and (33))
  delivery          shares held as investments: short-term capital gain at 20% (s.111A / s.196) when
                    held 12 months or less, long-term at 12.5% above Rs 1.25 lakh a year (s.112A /
                    s.198), with the 31 Jan 2018 grandfathering (s.55(2)(ac) / s.90(7)-(8))
  charges           business: every charge, STT included, is an expense (s.36(1)(xv) / s.32(k));
                    capital gains: STT is not deductible (s.48 / s.72(3)(b)); brokerage, exchange,
                    SEBI and GST are transfer expenses or part of the cost, stamp duty part of the cost

Then: turnover the way ICAI's Guidance Note on Tax Audit computes it (the absolute sum of each
squared-off trade's profit or loss), the s.44AB / s.63 audit check with the s.44AD / s.58 interplay
explained, intra-head and inter-head set-off, brought-forward losses (speculative: 4 years; other
business and capital losses: 8 years), what carries forward, the special-rate amounts with their
rates, the advance-tax dates and the ITR-3 schedule each number goes to.

NOT tax advice. Slab tax, surcharge, cess, rebates and deductions are out of scope (too personal).
Every rule carries its section, source and effective date; check them with a CA before filing.

Inputs: trader-journal's fills (paper: trader/state/trades.json; live: trader/journal/imports/), and
your own files in trader/tax/: holdings.csv (lots bought before your trade book starts, with buy
dates and the 31 Jan 2018 value), carry-forward.csv (losses from earlier returns), expenses.csv
(internet, software, advisory) and profile.json (cash share, past s.44AD use). Reporting only: it
never places, sizes or confirms an order.
"""

import argparse
import csv
import datetime
import importlib.util
import io
import json
import math
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP

SCHEMA = "bharat-trader/tax/1"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
PAISE = Decimal("0.01")
FIRST_FY = 2018          # FY 2018-19: s.112A began (gains on listed shares were exempt before)
BOOKS = ("live", "paper", "all")
DISCLAIMER = ("An ESTIMATE for discussion with your chartered accountant, not tax advice. Rules are dated with "
              "their sources; laws, forms and ICAI guidance change. Slab tax, surcharge, cess, rebates and "
              "deductions are not computed.")


class TaxError(Exception):
    pass


# ---------------------------------------------------------------- money, dates, files

def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian digit grouping), -1234 -> '-₹1,234.00'."""
    d = Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = format(abs(d), "f").split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%s₹%s.%s" % (sign, whole, frac)


def r2(x):
    return float(Decimal(repr(float(x))).quantize(PAISE, rounding=ROUND_HALF_UP))


def _f(x):
    try:
        v = float(str(x).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_now(text=None):
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise TaxError("--now wants a time like \"2026-11-01 10:00\" (IST)")
    return dt if dt.tzinfo is None else dt.astimezone(IST).replace(tzinfo=None)


def parse_date(text):
    """'2017-06-15', '15-06-2017', '15/06/2017', '15-Jun-2017' -> date, or None."""
    s = str(text or "").strip()[:19]
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y",
                "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s if "%H" in fmt else s[:11].strip(), fmt).date()
        except ValueError:
            continue
    return None


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_csv(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        return [{re.sub(r"[^a-z0-9]+", "_", (k or "").strip().lower()).strip("_"): (v or "").strip()
                 for k, v in r.items()} for r in csv.DictReader(fh)]


class Paths(object):
    def __init__(self, workdir="."):
        self.workdir = os.path.abspath(workdir or ".")
        self.trader = os.path.join(self.workdir, "trader")

    def t(self, *parts):
        return os.path.join(self.trader, *parts)

    def tax(self, *parts):
        return os.path.join(self.trader, "tax", *parts)


# ---------------------------------------------------------------- financial years

def fy_label(start_year):
    return "%d-%02d" % (start_year, (start_year + 1) % 100)


def parse_fy(text, oldest=FIRST_FY):
    """'2025-26', 'FY2025-26', '2025' -> 2025 (the April it starts)."""
    m = re.match(r"^\s*(?:FY\s*)?(\d{4})(?:\s*-\s*(\d{2}|\d{4}))?\s*$", str(text or ""), re.I)
    if not m:
        raise TaxError("--fy wants a financial year like 2025-26")
    y = int(m.group(1))
    if m.group(2) and int(m.group(2)[-2:]) != (y + 1) % 100:
        raise TaxError("%s is not a financial year (April to March): try %s" % (text, fy_label(y)))
    if y < oldest:
        raise TaxError("FY %s is before FY 2018-19: listed shares' long-term gains were exempt then (s.10(38)); "
                       "this estimate starts at FY 2018-19" % fy_label(y))
    return y


def fy_of(day):
    return day.year if day.month >= 4 else day.year - 1


def fy_bounds(y):
    return datetime.date(y, 4, 1), datetime.date(y + 1, 3, 31)


# ---------------------------------------------------------------- the rules, dated, with sources
# Read 1 Nov 2026. Primary texts: the Income-tax Act, 1961 as amended; the Finance (No. 2) Act, 2024
# (rates from 23 Jul 2024); the Income-tax Act, 2025 (Act 30 of 2025, the Bill as passed by Lok Sabha on
# 11 Aug 2025, prsindia.org), in force from 1 Apr 2026; ICAI's Guidance Note on Tax Audit under s.44AB
# (Revised 2026, 11th edition, 31 Jul 2026), para 5.11; CBDT Notification 47/2026 (30 Mar 2026, ITR-3
# for AY 2026-27). docs/tax.md has the full table with links and what could not be verified.

ACTS = [
    {"effective_from": "2018-04-01", "act": "Income-tax Act, 1961", "year_word": "previous year (FY)",
     "s": {"speculative": "s.43(5), s.28 Explanation 2", "derivative": "s.43(5) proviso (d)",
           "stcg": "s.111A", "ltcg": "s.112A", "grandfather": "s.55(2)(ac)", "holding": "s.2(42A)",
           "stt_business": "s.36(1)(xv)", "stt_cg": "s.48 (proviso: no deduction for STT)",
           "intra_head": "s.70", "inter_head": "s.71, s.71(2A) (not against salary), s.71(3)",
           "cf_business": "s.72 (8 years)", "cf_speculative": "s.73 (4 years)", "cf_capital": "s.74 (8 years)",
           "return_for_loss": "s.80 with s.139(3)", "audit": "s.44AB(a) and (e)",
           "presumptive": "s.44AD (and s.44AD(4), (5))", "advance_tax": "s.208, s.211 (interest s.234B, s.234C)",
           "return": "s.139(1)"},
     "form": "ITR-3",
     "source": "Income-tax Act, 1961 (incometaxindia.gov.in)"},
    {"effective_from": "2026-04-01", "act": "Income-tax Act, 2025", "year_word": "tax year",
     "s": {"speculative": "s.66(31), s.26(3)", "derivative": "s.66(31)(a), s.66(33)",
           "stcg": "s.196", "ltcg": "s.198", "grandfather": "s.90(7)-(8)", "holding": "s.2(101)",
           "stt_business": "s.32(k)", "stt_cg": "s.72(3)(b)",
           "intra_head": "s.108", "inter_head": "s.109 (not against salary), s.109(2)",
           "cf_business": "s.112 (8 tax years)", "cf_speculative": "s.113 (4 tax years)",
           "cf_capital": "s.111 (8 tax years)", "return_for_loss": "s.121 with s.263(1)",
           "audit": "s.63 (Table Sl. 1 and 2)", "presumptive": "s.58 (and s.58(7), (8))",
           "advance_tax": "s.404, s.408", "return": "s.263(1)",
           "transition": "s.536(2)(m), (n): losses brought forward from before 1 Apr 2026 keep the 1961 Act's "
                         "rules and periods"},
     "form": "ITR-3 for tax year 2026-27 (not notified yet on 1 Nov 2026; the AY 2026-27 layout is shown)",
     "source": "Income-tax Act, 2025 (Act 30 of 2025), in force 1 Apr 2026; Bill as passed by Lok Sabha, "
               "prsindia.org"},
]

RATES = [
    {"effective_from": "2018-04-01", "stcg_pct": 15.0, "ltcg_pct": 10.0,
     "source": "s.111A as amended by the Finance Act, 2008; s.112A inserted by the Finance Act, 2018"},
    {"effective_from": "2024-07-23", "stcg_pct": 20.0, "ltcg_pct": 12.5,
     "source": "Finance (No. 2) Act, 2024: transfers on or after 23 Jul 2024; unchanged by the Finance Act, "
               "2026 and carried into the 2025 Act (s.196, s.198)"},
]

# the s.112A exemption is a yearly amount: FY 2024-25 is Rs 1.25 lakh for the whole year (both halves together)
LTCG_EXEMPTION = [{"from_fy": 2018, "amount": 100000.0, "source": "s.112A(1), Finance Act, 2018"},
                  {"from_fy": 2024, "amount": 125000.0, "source": "Finance (No. 2) Act, 2024 (whole of FY 2024-25)"}]

AUDIT = [{"from_fy": 2018, "turnover": 10000000.0, "digital_turnover": 10000000.0,
          "source": "s.44AB(a): Rs 1 crore"},
         {"from_fy": 2020, "turnover": 10000000.0, "digital_turnover": 50000000.0,
          "source": "s.44AB(a) proviso, Finance Act, 2020: Rs 5 crore when cash is 5% or less"},
         {"from_fy": 2021, "turnover": 10000000.0, "digital_turnover": 100000000.0,
          "source": "s.44AB(a) proviso, Finance Act, 2021: Rs 10 crore when cash receipts and payments are each "
                    "5% or less; 2025 Act s.63 Table Sl. 1 keeps both limits"}]
PRESUMPTIVE = [{"from_fy": 2018, "limit": 20000000.0, "digital_limit": 20000000.0, "source": "s.44AD: Rs 2 crore"},
               {"from_fy": 2023, "limit": 20000000.0, "digital_limit": 30000000.0,
                "source": "s.44AD, Finance Act, 2023: Rs 3 crore when cash received is 5% or less; 2025 Act "
                          "s.58(2) Table Sl. 1"}]

TURNOVER_SOURCE = ("ICAI, Guidance Note on Tax Audit under Section 44AB (Revised 2026, 31 Jul 2026), para 5.11: "
                   "(a) speculative: the aggregate of favourable and unfavourable differences; (b) derivatives: (i) "
                   "the total of favourable and unfavourable differences on squared-off trades, (ii) premium "
                   "received on sale of options is also included, but not separately where it is already in the "
                   "net profit, (iv) an open position counts in the year it is squared off; (c) delivery: the sale "
                   "value. Solely to decide whether s.44AB applies.")

ADVANCE = [("06-15", 15), ("09-15", 45), ("12-15", 75), ("03-15", 100)]

# STT on delivery shares (0.1% of the value, buy and sell) is split out of a fill's charges for capital
# gains, where it is not deductible. Rate: Finance (No. 2) Act, 2004 Chapter VII as amended (from 1 Jun 2013).
STT_DELIVERY_PCT = 0.1


def rule_at(table, day):
    key = day.isoformat() if isinstance(day, datetime.date) else str(day)
    pick = table[0]
    for r in table:
        if key >= r["effective_from"]:
            pick = r
    return pick


def by_fy(table, y):
    pick = table[0]
    for r in table:
        if y >= r["from_fy"]:
            pick = r
    return pick


def act_for(y):
    return rule_at(ACTS, datetime.date(y, 4, 1))


def rules_for(y):
    """Every rule for FY y, as rows: what, rule, section, source, effective."""
    a = act_for(y)
    s = a["s"]
    start, end = fy_bounds(y)
    rate_rows = [r for r in RATES if r["effective_from"] <= end.isoformat()]
    rate_rows = [r for r in rate_rows if r is rate_rows[-1] or rate_rows[rate_rows.index(r) + 1]["effective_from"]
                 > start.isoformat()]
    ex = by_fy(LTCG_EXEMPTION, y)
    au = by_fy(AUDIT, y)
    pr = by_fy(PRESUMPTIVE, y)
    rows = [
        {"what": "Intraday equity (no delivery)", "rule": "speculative business income, a business distinct from "
         "other business", "section": s["speculative"], "source": a["source"], "effective": a["effective_from"]},
        {"what": "Futures and options on a recognised exchange", "rule": "not speculative: non-speculative business "
         "income", "section": s["derivative"], "source": a["source"], "effective": a["effective_from"]},
        {"what": "Listed shares held 12 months or less", "rule": "short-term capital asset", "section": s["holding"],
         "source": a["source"], "effective": a["effective_from"]},
    ]
    for r in rate_rows:
        rows.append({"what": "Short-term gain on STT-paid listed shares", "rule": "%s%% (transfers from %s)" % (
            _pct(r["stcg_pct"]), r["effective_from"]), "section": s["stcg"], "source": r["source"],
            "effective": r["effective_from"]})
        rows.append({"what": "Long-term gain on STT-paid listed shares", "rule": "%s%% above the yearly exemption "
                     "(transfers from %s)" % (_pct(r["ltcg_pct"]), r["effective_from"]), "section": s["ltcg"],
                     "source": r["source"], "effective": r["effective_from"]})
    rows += [
        {"what": "Long-term exemption", "rule": "%s a year (FY %s)" % (inr(ex["amount"]), fy_label(y)),
         "section": s["ltcg"], "source": ex["source"], "effective": fy_label(ex["from_fy"])},
        {"what": "Shares bought before 1 Feb 2018", "rule": "cost = higher of actual cost and the lower of the 31 Jan "
         "2018 value (that day's highest price) and the sale value", "section": s["grandfather"],
         "source": a["source"], "effective": "2018-04-01"},
        {"what": "STT on business trades", "rule": "deductible as an expense", "section": s["stt_business"],
         "source": a["source"], "effective": a["effective_from"]},
        {"what": "STT on capital gains", "rule": "not deductible", "section": s["stt_cg"], "source": a["source"],
         "effective": a["effective_from"]},
        {"what": "Speculative loss", "rule": "set off only against speculative income; carried forward 4 years",
         "section": s["cf_speculative"], "source": a["source"], "effective": a["effective_from"]},
        {"what": "Other business loss (F&O)", "rule": "against any income except salary this year; carried forward "
         "8 years against business income", "section": "%s; %s" % (s["inter_head"], s["cf_business"]),
         "source": a["source"], "effective": a["effective_from"]},
        {"what": "Capital loss", "rule": "short-term against any capital gain, long-term only against long-term; not "
         "against other heads; 8 years", "section": "%s; %s" % (s["intra_head"], s["cf_capital"]),
         "source": a["source"], "effective": a["effective_from"]},
        {"what": "Carrying a loss forward", "rule": "only when the return is filed by the due date",
         "section": s["return_for_loss"], "source": a["source"], "effective": a["effective_from"]},
        {"what": "Turnover for the audit check", "rule": "absolute sum of each squared-off trade's profit or loss "
         "(F&O and intraday); delivery shares held as investments are not business turnover",
         "section": s["audit"], "source": TURNOVER_SOURCE, "effective": "AY 2026-27 audits (11th edition)"},
        {"what": "Tax audit by turnover", "rule": "over %s; %s when cash receipts and payments are each 5%% or less"
         % (inr(au["turnover"]), inr(au["digital_turnover"])), "section": s["audit"], "source": au["source"],
         "effective": fy_label(au["from_fy"])},
        {"what": "Presumptive scheme", "rule": "6%% of digital turnover deemed profit, up to %s (%s when cash is 5%% "
         "or less); declaring less after using it in the last 5 years means an audit if income exceeds the basic "
         "exemption" % (inr(pr["limit"]), inr(pr["digital_limit"])), "section": s["presumptive"],
         "source": pr["source"], "effective": fy_label(pr["from_fy"])},
        {"what": "Advance tax", "rule": "due when the year's tax is Rs 10,000 or more: 15% by 15 Jun, 45% by 15 Sep, "
         "75% by 15 Dec, 100% by 15 Mar", "section": s["advance_tax"], "source": a["source"],
         "effective": a["effective_from"]},
        {"what": "Return form", "rule": a["form"], "section": s["return"],
         "source": "CBDT Notification 47/2026 (30 Mar 2026) for AY 2026-27" if y == 2025 else a["source"],
         "effective": a["effective_from"]},
    ]
    if "transition" in s:
        rows.append({"what": "Losses from FY 2025-26 or earlier", "rule": s["transition"], "section": "s.536",
                     "source": a["source"], "effective": "2026-04-01"})
    return rows


def _pct(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- fills: through trader-journal when it is installed

def sibling(folder, script, name):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), folder, script)
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def journal_api():
    return sibling("trader-journal", "journal.py", "bt_tax_journal")


def imported_fills(paths):
    """trader/journal/imports/*.json: the fills trader-journal's importer stored (read directly, so this works
    without trader-journal installed)."""
    out = []
    folder = paths.t("journal", "imports")
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.endswith(".json"):
                doc = read_json(os.path.join(folder, f)) or {}
                out.extend(x for x in doc.get("fills") or [] if isinstance(x, dict))
    return out


def load_fills(paths, book="live", tradebooks=None, notes=None):
    """The fills of one book. live: imported broker trade books (plus --tradebook files, parsed and not
    stored); paper: trader-broker's simulated fills; all: both (a practice mix, labelled so)."""
    notes = notes if notes is not None else []
    jr = journal_api()
    fills = []
    if book in ("live", "all"):
        fills += [f for f in imported_fills(paths) if not f.get("paper")]
        for path, broker in tradebooks or []:
            if jr is None:
                raise TaxError("reading a broker file needs trader-journal next to this folder (its importers)")
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    text = fh.read()
            except OSError as e:
                raise TaxError("could not read %s: %s" % (path, e))
            try:
                got, name, more = jr.parse_tradebook(text, broker)
            except jr.JournalError as e:
                raise TaxError(str(e))
            jr.estimate_charges(got, jr.config_of(jr.Paths(paths.workdir)))
            have = {f["id"] for f in fills}
            fills += [f for f in got if f["id"] not in have]
            notes.extend("%s: %s" % (os.path.basename(path), n) for n in more)
    if book in ("paper", "all"):
        if jr is None:
            notes.append("paper fills need trader-journal next to this folder; none read")
        else:
            fills += jr.paper_fills(read_json(paths.t("state", "trades.json")) or {})
    if any(f.get("charges_estimated") for f in fills):
        notes.append("some charges are estimates (the broker file carried none): use your contract notes' "
                     "figures for the return")
    return fills


# ---------------------------------------------------------------- instruments and expiry

OPT_RE = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})((?:[A-Z]{3})|(?:[1-9OND]\d{2}))(\d+(?:\.\d+)?)(CE|PE)$")
FUT_RE = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})([A-Z]{3})FUT$")
KEY_RE = re.compile(r"^([A-Z0-9&\-]+?)-(\d{4}-\d{2}-\d{2})-(?:(FUT)|(\d+(?:\.\d+)?)-(CE|PE))$")
MON3 = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                    "DEC"), 1)}
WEEKLY_MON = {"O": 10, "N": 11, "D": 12}


def expiry_of(symbol):
    """The last possible expiry date of a contract: exact for a dated key or a weekly symbol, the month's last
    day for a monthly symbol (NIFTY25NOVFUT, NIFTY25NOV26000CE); None when it cannot be read."""
    s = str(symbol or "").upper()
    m = KEY_RE.match(s)
    if m:
        return parse_date(m.group(2))
    m = OPT_RE.match(s) or FUT_RE.match(s)
    if not m:
        return None
    yy, code = int(m.group(2)), m.group(3)
    try:
        if code in MON3:
            nxt = datetime.date(2000 + yy + (1 if code == "DEC" else 0), MON3[code] % 12 + 1, 1)
            return nxt - datetime.timedelta(days=1)
        mon = WEEKLY_MON.get(code[0]) or int(code[0])
        return datetime.date(2000 + yy, mon, int(code[1:]))
    except ValueError:
        return None


def option_type(symbol):
    s = str(symbol or "").upper()
    return "CE" if s.endswith("CE") else ("PE" if s.endswith("PE") else None)


# ---------------------------------------------------------------- classification

def _day(f):
    return f["at"][:10]


def _charges_split(f, qty):
    return (f.get("charges") or 0.0) * (float(qty) / f["qty"]) if f["qty"] else 0.0


def broker_api():
    return sibling("trader-broker", "ticket.py", "bt_tax_broker")


def _delivery_part(fills, share, cfg):
    """(STT, other charges) on the delivery share of a day's buys or sells. An estimated charge (the broker file
    had none) is re-priced as delivery (CNC) with trader-broker's model; a real one is split by the STT rate."""
    stt = other = 0.0
    b = broker_api()
    for f in fills:
        q = f["qty"] * share
        total = _charges_split(f, q)
        if f.get("charges_estimated") and b is not None:
            prof = dict(cfg or {})
            if f.get("source") in ("zerodha", "kite", "upstox", "dhan", "angel", "groww") and \
                    not prof.get("broker_profile"):
                prof["broker_profile"] = "zerodha" if f["source"] == "kite" else f["source"]
            try:
                ch = b.side_charges(f["side"], f["price"], q, prof, "CNC", "EQ", f["at"][:10])
                stt += ch["stt"]
                other += ch["total"] - ch["stt"]
                continue
            except Exception:      # a model that cannot price it: fall back to the split below
                pass
        s_ = min(total, q * f["price"] * STT_DELIVERY_PCT / 100.0)
        stt += s_
        other += total - s_
    return stt, other


def classify(fills, holdings=None, as_of=None, cfg=None):
    """Fills (+ opening lots) -> (rows, open_lots, open_fno, problems). One row per realised piece:
    kind intraday / futures / options / stcg / ltcg, with the date it counts on, gross, charges, net and the
    numbers its head needs."""
    as_of = as_of or datetime.date.today()
    rows, problems = [], []
    eq = [f for f in fills if f.get("segment") == "EQ"]
    fno = [f for f in fills if f.get("segment") in ("OPT", "FUT")]
    lots = {}
    for h in holdings or []:
        lots.setdefault(h["symbol"], []).append(dict(h))
    for v in lots.values():
        v.sort(key=lambda x: x["buy_date"])
    # equity: per symbol per day, the quantity both bought and sold is intraday; the net is delivery
    by_sym_day = {}
    for f in sorted(eq, key=lambda f: (f["at"], f["id"])):
        by_sym_day.setdefault((f["symbol"], _day(f)), []).append(f)
    for (sym, day) in sorted(by_sym_day, key=lambda k: (k[1], k[0])):
        fs = by_sym_day[(sym, day)]
        buys = [f for f in fs if f["side"] == "BUY"]
        sells = [f for f in fs if f["side"] == "SELL"]
        bq, sq = sum(f["qty"] for f in buys), sum(f["qty"] for f in sells)
        bv, sv = sum(f["qty"] * f["price"] for f in buys), sum(f["qty"] * f["price"] for f in sells)
        bch, sch = sum(f.get("charges") or 0 for f in buys), sum(f.get("charges") or 0 for f in sells)
        iq = min(bq, sq)
        if iq:
            ab, as_ = bv / bq, sv / sq
            gross = iq * (as_ - ab)
            ch = bch * iq / bq + sch * iq / sq
            rows.append({"kind": "intraday", "symbol": sym, "date": day, "qty": iq, "buy_price": r2(ab),
                         "sell_price": r2(as_), "gross": r2(gross), "charges": r2(ch), "net": r2(gross - ch),
                         "turnover": r2(abs(gross)), "buy_value": r2(iq * ab), "sell_value": r2(iq * as_),
                         "estimated": any(f.get("charges_estimated") for f in fs)})
        if bq > sq:
            q = bq - sq
            stt, other = _delivery_part(buys, float(q) / bq, cfg)
            lots.setdefault(sym, []).append({"symbol": sym, "qty": q, "buy_date": day, "buy_price": bv / bq,
                                             "buy_costs": other, "buy_stt": stt, "fmv_2018": None,
                                             "source": "trade book"})
        elif sq > bq:
            q = sq - bq
            stt, other = _delivery_part(sells, float(q) / sq, cfg)
            rows += _sell_delivery(sym, day, q, sv / sq, stt, other, lots, problems)
    # F&O: FIFO per contract; each matched piece is one squared-off trade (ICAI: its difference is turnover)
    pos = {}
    for f in sorted(fno, key=lambda f: (f["at"], 1 if f["side"] == "SETTLE" else 0, f["id"])):
        book = pos.setdefault(f["symbol"], [])
        if f["side"] == "SETTLE":
            # the paper broker's expiry settlement: qty is the signed position it closes, price the intrinsic value
            side = "SELL" if f["qty"] > 0 else "BUY"
            g = dict(f, side=side, qty=abs(f["qty"]), settled=True)
        else:
            g = dict(f)
        _fno_fill(g, book, rows)
    open_fno = []
    for sym, book in sorted(pos.items()):
        if not book:
            continue
        exp = expiry_of(sym)
        seg = book[0]["segment"]
        q = sum(x["qty"] * (1 if x["side"] == "BUY" else -1) for x in book)
        if exp and exp < as_of and seg == "OPT":
            # an option no broker file shows closing: it expired. Worthless is assumed (the common case); an
            # in-the-money expiry has a settlement (and STT) on your contract note: add it as a SETTLE fill.
            g = {"id": "expiry:%s" % sym, "at": exp.isoformat() + "T15:30:00", "symbol": sym, "segment": "OPT",
                 "underlying": book[0].get("underlying"), "side": "SELL" if q > 0 else "BUY", "qty": abs(q),
                 "price": 0.0, "charges": 0.0, "expired": True}
            _fno_fill(g, book, rows)
            problems.append((exp.isoformat(), "%s expired with %d open; taken as worthless on %s. If it expired in the money, its "
                            "settlement is on your contract note: add it (journal.py import) and re-run" % (
                                sym, abs(q), exp.isoformat())))
        else:
            open_fno.append({"symbol": sym, "qty": q, "since": book[0]["at"][:10],
                             "expiry": exp.isoformat() if exp else None,
                             "note": "open: counts in the year it is squared off (ICAI GN para 5.11(b)(iv))"})
            if exp and exp < as_of:
                problems.append((exp.isoformat(), "%s: an expired future with no closing trade in the file; its "
                                 "final settlement is missing (add it from your contract note)" % sym))
    open_lots = [dict(l, buy_price=r2(l["buy_price"])) for v in lots.values() for l in v if l["qty"] > 0]
    rows.sort(key=lambda r: (r["date"], r["kind"], r["symbol"]))
    return rows, open_lots, open_fno, problems


def _fno_fill(g, book, rows):
    """Match one F&O fill against the open lots of its contract (FIFO); open the rest."""
    left = g["qty"]
    seg = g.get("segment") or "OPT"
    while left > 0 and book and book[0]["side"] != g["side"]:
        lot = book[0]
        q = min(left, lot["qty"])
        long_ = lot["side"] == "BUY"
        buy_p, sell_p = (lot["price"], g["price"]) if long_ else (g["price"], lot["price"])
        gross = q * (sell_p - buy_p)
        ch = _charges_split(lot["fill"], q) + _charges_split(g, q)
        sold_premium = q * lot["price"] if (seg == "OPT" and not long_) else 0.0
        rows.append({"kind": "options" if seg == "OPT" else "futures", "symbol": g["symbol"], "date": _day(g),
                     "opened": lot["fill"]["at"][:10], "qty": q, "side": "long" if long_ else "short",
                     "buy_price": r2(buy_p), "sell_price": r2(sell_p), "gross": r2(gross), "charges": r2(ch),
                     "net": r2(gross - ch), "turnover": r2(abs(gross)), "premium_sold": r2(sold_premium),
                     "expired": bool(g.get("expired")), "settled": bool(g.get("settled")),
                     "estimated": bool(g.get("charges_estimated") or lot["fill"].get("charges_estimated"))})
        lot["qty"] -= q
        left -= q
        if lot["qty"] == 0:
            book.pop(0)
    if left > 0:
        book.append({"side": g["side"], "qty": left, "price": g["price"], "fill": dict(g, qty=g["qty"]),
                     "segment": seg, "at": g["at"], "underlying": g.get("underlying")})


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return datetime.date(y, m, day)
        except ValueError:
            continue
    return d


def long_term(buy, sell):
    """Listed shares are long-term when held MORE than 12 months (s.2(42A) / 2025 Act s.2(101)(b))."""
    return sell > add_months(buy, 12)


def _sell_delivery(sym, day, qty, price, sell_stt_total, sell_other, lots, problems):
    """A delivery sale matched FIFO against the lots held (opening holdings first by buy date)."""
    out = []
    sd = parse_date(day)
    queue = lots.get(sym) or []
    left = qty
    while left > 0 and queue:
        lot = queue[0]
        q = min(left, lot["qty"])
        share = float(q) / lot["qty"]
        bd = parse_date(lot["buy_date"])
        cost_actual = q * lot["buy_price"]
        costs = (lot.get("buy_costs") or 0.0) * share
        stt_buy = (lot.get("buy_stt") or 0.0) * share
        sale = q * price
        sell_part = float(q) / qty
        sell_stt = sell_stt_total * sell_part
        sell_exp = sell_other * sell_part
        lt = long_term(bd, sd)
        cost = cost_actual
        gf = None
        if lt and bd < datetime.date(2018, 2, 1):
            fmv = lot.get("fmv_2018")
            if fmv is None:
                problems.append((day, "%s bought %s: the 31 Jan 2018 value (that day's highest price) is missing from "
                                "holdings.csv, so the actual cost is used (grandfathering not applied)" % (
                                    sym, lot["buy_date"])))
            else:
                cost = max(cost_actual, min(q * fmv, sale))
                gf = {"fmv_2018": fmv, "actual_cost": r2(cost_actual), "cost_used": r2(cost)}
        gain = sale - cost - costs - sell_exp
        out.append({"kind": "ltcg" if lt else "stcg", "symbol": sym, "date": day, "bought": lot["buy_date"],
                    "qty": q, "buy_price": r2(lot["buy_price"]), "sell_price": r2(price), "sale_value": r2(sale),
                    "cost": r2(cost), "expenses": r2(costs + sell_exp), "stt_not_deductible": r2(stt_buy + sell_stt),
                    "gross": r2(sale - cost_actual), "charges": r2(costs + sell_exp + stt_buy + sell_stt),
                    "net": r2(gain), "grandfathered": gf, "lot_source": lot.get("source") or "holdings.csv",
                    "held_days": (sd - bd).days})
        lot["qty"] -= q
        lot["buy_costs"] = (lot.get("buy_costs") or 0.0) - costs
        lot["buy_stt"] = (lot.get("buy_stt") or 0.0) - stt_buy
        left -= q
        if lot["qty"] <= 0:
            queue.pop(0)
    if left > 0:
        problems.append((day, "%s: sold %d on %s with no lot to match (bought before the trade book starts?): add it to "
                        "trader/tax/holdings.csv with its buy date and price; left out of the capital gains" % (
                            sym, left, day)))
    return out


# ---------------------------------------------------------------- your own files in trader/tax/

def load_holdings(path):
    """holdings.csv: symbol, qty, buy_date, buy_price[, fmv_2018][, buy_charges]."""
    out = []
    for n, r in enumerate(read_csv(path), 2):
        sym = (r.get("symbol") or r.get("tradingsymbol") or "").upper()
        q, p, d = _f(r.get("qty") or r.get("quantity")), _f(r.get("buy_price") or r.get("price")), \
            parse_date(r.get("buy_date") or r.get("date"))
        if not sym or not q or q <= 0 or p is None or d is None:
            raise TaxError("%s line %d: needs symbol, qty, buy_date and buy_price" % (path, n))
        out.append({"symbol": sym, "qty": int(q), "buy_date": d.isoformat(), "buy_price": p,
                    "fmv_2018": _f(r.get("fmv_2018") or r.get("fmv_31jan2018")),
                    "buy_costs": _f(r.get("buy_charges")) or 0.0, "buy_stt": 0.0, "source": "holdings.csv"})
    return out


KINDS = ("speculative", "non_speculative", "stcl", "ltcl")


def load_carry_forward(path):
    """carry-forward.csv: fy (the year the loss arose), kind (speculative, non_speculative, stcl, ltcl), amount
    still unabsorbed (from Schedule CFL of your last return)."""
    out = []
    for n, r in enumerate(read_csv(path), 2):
        kind = (r.get("kind") or "").lower().replace("-", "_").replace(" ", "_")
        kind = {"speculation": "speculative", "business": "non_speculative", "fno": "non_speculative",
                "short_term": "stcl", "long_term": "ltcl"}.get(kind, kind)
        amt = _f(r.get("amount"))
        if kind not in KINDS or not amt or amt <= 0:
            raise TaxError("%s line %d: kind is one of %s and amount is the loss left (positive)" % (
                path, n, ", ".join(KINDS)))
        out.append({"fy": parse_fy(r.get("fy"), 2000) if _fy_ok(r.get("fy")) else _bad_fy(path, n), "kind": kind,
                    "amount": abs(amt), "filed_on_time": (r.get("filed_on_time") or "yes").lower()
                    not in ("no", "n", "false", "0")})
    return out


def _fy_ok(text):
    return bool(re.match(r"^\s*(?:FY\s*)?\d{4}", str(text or ""), re.I))


def _bad_fy(path, n):
    raise TaxError("%s line %d: fy like 2023-24" % (path, n))


def load_expenses(path, y):
    """expenses.csv: fy, head (speculative, non_speculative or both), amount, note."""
    out = []
    for n, r in enumerate(read_csv(path), 2):
        if not _fy_ok(r.get("fy")) or parse_fy(r.get("fy")) != y:
            continue
        head = (r.get("head") or "both").lower().replace("-", "_")
        amt = _f(r.get("amount"))
        if head not in ("speculative", "non_speculative", "both") or amt is None or amt < 0:
            raise TaxError("%s line %d: head is speculative, non_speculative or both; amount 0 or more" % (path, n))
        out.append({"head": head, "amount": amt, "note": r.get("note") or ""})
    return out


# ---------------------------------------------------------------- the report

def _sum(rows, key="net"):
    return r2(sum(r.get(key) or 0 for r in rows))


def head_totals(rows):
    pos = [r for r in rows if r["net"] > 0]
    return {"trades": len(rows), "gross": _sum(rows, "gross"), "charges": _sum(rows, "charges"),
            "net": _sum(rows), "profits": _sum(pos), "losses": r2(-sum(r["net"] for r in rows if r["net"] < 0))}


def setoff(cur, bf, y):
    """Current-year and brought-forward set-off. cur: speculative, non_speculative (business, after expenses),
    stcg / stcl / ltcg / ltcl (gains and losses separately, positive numbers), the STCG and LTCG split by rate.
    Returns the steps in words, what is taxable, what carries forward and what lapsed."""
    steps = []
    spec, nons = cur["speculative"], cur["non_speculative"]
    stg = list(cur["stcg_by_rate"])       # [[rate, amount]], gains only
    ltg = list(cur["ltcg_by_rate"])
    stl, ltl = cur["stcl"], cur["ltcl"]

    def take(buckets, amt):
        """Reduce gain buckets (highest rate first) by amt; returns what was used."""
        used = 0.0
        for b in sorted(buckets, key=lambda b: -b[0]):
            u = min(b[1], amt - used)
            b[1] -= u
            used += u
        return used

    # 1. within the capital gains head (s.70(2)-(3) / s.108(2)): STCL against any gain, LTCL only long-term
    if stl:
        u = take(stg, stl)
        u2 = take(ltg, stl - u)
        if u + u2:
            steps.append("Short-term capital loss %s set off against short-term gains %s and long-term gains %s" % (
                inr(u + u2), inr(u), inr(u2)))
        stl = r2(stl - u - u2)
    if ltl:
        u = take(ltg, ltl)
        if u:
            steps.append("Long-term capital loss %s set off against long-term gains" % inr(u))
        ltl = r2(ltl - u)
    # 2. within business (s.70(1) / s.108(1)): an F&O loss may absorb intraday profit; never the reverse (s.73)
    if nons < 0 < spec:
        u = min(-nons, spec)
        spec, nons = r2(spec - u), r2(nons + u)
        steps.append("F&O (non-speculative) loss %s set off against speculative (intraday) income" % inr(u))
    if spec < 0 < nons:
        steps.append("Speculative loss %s is NOT set off against F&O income: only against speculative income "
                     "(s.73(1) / s.113(1))" % inr(-spec))
    # 3. across heads (s.71 / s.109): a remaining F&O loss against capital gains (never salary)
    if nons < 0:
        left = -nons
        u = take(stg, left)
        u2 = take(ltg, left - u)
        if u + u2:
            steps.append("F&O loss %s set off against capital gains (short-term %s, long-term %s); the rest can also "
                         "go against your other income except salary, which this report does not see" % (
                             inr(u + u2), inr(u), inr(u2)))
        nons = r2(nons + u + u2)
    # 4. brought-forward losses, oldest first
    lapsed, used_bf, cf = [], [], []
    for b in sorted(bf, key=lambda b: (b["fy"], b["kind"])):
        years = 4 if b["kind"] == "speculative" else 8
        last = b["fy"] + years
        amt = b["amount"]
        if not b.get("filed_on_time", True):
            lapsed.append(dict(b, why="the return for FY %s was not filed by its due date: the loss cannot be carried "
                                      "forward (s.80 / s.121)" % fy_label(b["fy"])))
            continue
        if y > last:
            lapsed.append(dict(b, why="past its %d years (usable to FY %s)" % (years, fy_label(last))))
            continue
        if b["fy"] >= y:
            continue
        used = 0.0
        if b["kind"] == "speculative" and spec > 0:
            used = min(amt, spec)
            spec = r2(spec - used)
        elif b["kind"] == "non_speculative":
            u1 = min(amt, max(nons, 0))
            nons = r2(nons - u1)
            u2 = min(amt - u1, max(spec, 0))
            spec = r2(spec - u2)
            used = u1 + u2
        elif b["kind"] == "stcl":
            used = take(stg, amt)
            used += take(ltg, amt - used)
        elif b["kind"] == "ltcl":
            used = take(ltg, amt)
        if used:
            used_bf.append({"fy": fy_label(b["fy"]), "kind": b["kind"], "used": r2(used)})
            steps.append("Brought-forward %s from FY %s: %s used" % (KIND_WORDS[b["kind"]], fy_label(b["fy"]),
                                                                    inr(used)))
        if amt - used > 0.004:
            cf.append({"fy": fy_label(b["fy"]), "kind": b["kind"], "amount": r2(amt - used),
                       "usable_until": fy_label(last)})
    # 5. this year's losses that carry forward
    if spec < 0:
        cf.append({"fy": fy_label(y), "kind": "speculative", "amount": r2(-spec), "usable_until": fy_label(y + 4)})
        spec = 0.0
    if nons < 0:
        cf.append({"fy": fy_label(y), "kind": "non_speculative", "amount": r2(-nons), "usable_until": fy_label(y + 8)})
        nons = 0.0
    if stl > 0.004:
        cf.append({"fy": fy_label(y), "kind": "stcl", "amount": r2(stl), "usable_until": fy_label(y + 8)})
    if ltl > 0.004:
        cf.append({"fy": fy_label(y), "kind": "ltcl", "amount": r2(ltl), "usable_until": fy_label(y + 8)})
    return {"steps": steps, "brought_forward_used": used_bf, "lapsed": [
        {"fy": fy_label(b["fy"]), "kind": b["kind"], "amount": r2(b["amount"]), "why": b["why"]} for b in lapsed],
        "carry_forward": cf, "speculative": r2(spec), "non_speculative": r2(nons),
        "stcg_by_rate": [[r, r2(a)] for r, a in stg if a > 0.004], "ltcg_by_rate": [[r, r2(a)] for r, a in ltg if a > 0.004]}


KIND_WORDS = {"speculative": "speculative business loss", "non_speculative": "non-speculative business loss",
              "stcl": "short-term capital loss", "ltcl": "long-term capital loss"}


def by_rate(rows, key):
    """Gains (positive pieces) and losses per rate in force on each sale date."""
    gains, losses = {}, 0.0
    for r in rows:
        rate = rule_at(RATES, parse_date(r["date"]))[key]
        if r["net"] > 0:
            gains[rate] = gains.get(rate, 0.0) + r["net"]
        else:
            losses += -r["net"]
    return [[k, r2(v)] for k, v in sorted(gains.items())], r2(losses)


def turnover(rows, y):
    intr = [r for r in rows if r["kind"] == "intraday"]
    fut = [r for r in rows if r["kind"] == "futures"]
    opt = [r for r in rows if r["kind"] == "options"]
    t_i, t_f, t_o = _sum(intr, "turnover"), _sum(fut, "turnover"), _sum(opt, "turnover")
    prem = _sum(opt, "premium_sold")
    delivery = _sum([r for r in rows if r["kind"] in ("stcg", "ltcg")], "sale_value")
    total = r2(t_i + t_f + t_o)
    return {"intraday": t_i, "futures": t_f, "options": t_o, "total": total, "options_premium_sold": prem,
            "total_with_premium": r2(total + prem), "delivery_sale_value": delivery,
            "method": "The absolute sum of each squared-off trade's profit or loss: an intraday stock counts once a day "
                      "(the day's buys against its sells), an F&O trade once per opening lot it closes (FIFO). A "
                      "loss adds to turnover just like a profit.",
            "premium_words": "ICAI's Guidance Note also names the premium received on selling options, but not "
                             "separately where it is already in the net profit; for a squared-off option it is, so "
                             "it is not added here. If your CA adds it, turnover is %s." % inr(total + prem),
            "source": TURNOVER_SOURCE}


def audit_check(t, business_net, y, profile):
    au, pr = by_fy(AUDIT, y), by_fy(PRESUMPTIVE, y)
    a = act_for(y)
    cash = bool((profile or {}).get("cash_over_5pct"))
    limit = au["turnover"] if cash else au["digital_turnover"]
    used_44ad = [str(x) for x in (profile or {}).get("presumptive_years") or []]
    recent = [x for x in used_44ad if _fy_ok(x) and y - 5 <= parse_fy(x) < y]
    total = t["total"]
    words, status = [], "no audit by turnover"
    six = 0.06 * total
    words.append("Business turnover %s (F&O %s, intraday %s), computed the ICAI way." % (
        inr(total), inr(t["futures"] + t["options"]), inr(t["intraday"])))
    if total > limit:
        status = "audit required"
        words.append("It is over %s, so a tax audit is required (%s)%s." % (
            inr(limit), a["s"]["audit"], "" if cash else ", the limit when cash receipts and payments are each 5% "
                                                       "or less (trading through a broker is all digital)"))
    else:
        words.append("It is under the %s limit (%s), so turnover alone does not require an audit." % (
            inr(limit), a["s"]["audit"]))
        if business_net < six:
            if recent:
                status = "audit likely"
                words.append("You used the presumptive scheme in FY %s, within the last five years. Declaring a "
                             "loss or under 6%% of turnover (%s) now means an audit if your total income is over "
                             "the basic exemption limit (%s)." % (", ".join(recent), inr(six), a["s"]["presumptive"]))
            else:
                words.append("Your business result (%s) is under 6%% of turnover (%s). Since the Finance Act, 2016 "
                             "that alone does not require an audit; it does if you used the presumptive scheme in "
                             "any of the last five years and your total income is over the basic exemption limit "
                             "(%s). Set presumptive_years in trader/tax/profile.json if you did." % (
                                 inr(business_net), inr(six), a["s"]["presumptive"]))
        if total <= pr["digital_limit"] and business_net >= six:
            words.append("Turnover is within the presumptive limit (%s): your CA may discuss %s (6%% of digital "
                         "turnover as deemed profit); whether F&O and intraday trading may use it is debated." % (
                             inr(pr["digital_limit"]), a["s"]["presumptive"]))
    words.append("Delivery shares held as investments are capital gains, not business turnover. If your CA "
                 "treats them as stock-in-trade, their sale value (%s) is turnover too and the gains become "
                 "business income." % inr(t["delivery_sale_value"]))
    return {"status": status, "limit": limit, "turnover": total, "six_pct": r2(six), "presumptive_recent": recent,
            "words": words, "source": au["source"]}


def advance_schedule(rows, y, as_of):
    out = []
    for mmdd, pct in ADVANCE:
        yr = y + 1 if mmdd.startswith("03") else y
        due = datetime.date(yr, int(mmdd[:2]), int(mmdd[3:]))
        upto = [r for r in rows if parse_date(r["date"]) <= due]
        biz = _sum([r for r in upto if r["kind"] in ("intraday", "futures", "options")])
        cg = _sum([r for r in upto if r["kind"] in ("stcg", "ltcg")])
        out.append({"due": due.isoformat(), "cumulative_pct": pct, "status": "past" if due < as_of else "upcoming",
                    "business_net_to_date": biz, "capital_gains_to_date": cg})
    return {"dates": out, "words": [
        "Advance tax is due when the year's tax after TDS is Rs 10,000 or more. Pay %s by each date." % (
            ", ".join("%d%% by %s" % (p, d["due"]) for (m, p), d in zip(ADVANCE, out))),
        "A capital gain (or dividend) that arises after a date can be paid in the remaining instalments without "
        "interest for that date (s.234C proviso); business income from trading has no such relief.",
        "The numbers to date are realised results only; your CA works out the tax with your other income."]}


ITR = [
    ("intraday", "Part A-P&L (turnover, gross profit and expenses of the speculative business) and Schedule BP "
                 "part B (income from speculative business)"),
    ("futures", "Part A-P&L and Schedule BP part A (non-speculative business); the AY 2026-27 form reportedly "
                "has separate F&O fields"),
    ("options", "Part A-P&L and Schedule BP part A (non-speculative business)"),
    ("charges", "Part A-P&L expenses: brokerage, STT, exchange and SEBI fees, stamp duty, GST; and your "
                "expenses.csv items"),
    ("stcg", "Schedule CG part A item A2 (STT-paid shares, 111A / 196); Schedule SI (special rate)"),
    ("ltcg", "Schedule 112A (scrip by scrip, with the 31 Jan 2018 value for older lots), then Schedule CG part B "
             "item B4; Schedule SI"),
    ("setoff", "Schedule CYLA (this year's set-off), Schedule BFLA (brought-forward), Schedule CFL (to carry "
               "forward)"),
    ("turnover", "Part A-GEN (audit details) and Part A-P&L turnover"),
    ("advance_tax", "Schedule IT (advance tax paid)"),
]


def year_figures(paths, rows, y, bf):
    """One financial year's heads, turnover and set-off from the classified rows."""
    start, end = fy_bounds(y)
    yr = [r for r in rows if start <= parse_date(r["date"]) <= end]
    intr = [r for r in yr if r["kind"] == "intraday"]
    fno = [r for r in yr if r["kind"] in ("futures", "options")]
    st = [r for r in yr if r["kind"] == "stcg"]
    lt = [r for r in yr if r["kind"] == "ltcg"]
    expenses = load_expenses(paths.tax("expenses.csv"), y)
    t = turnover(yr, y)
    tot = t["total"] or 1.0
    exp_spec = sum(e["amount"] for e in expenses if e["head"] == "speculative") + \
        sum(e["amount"] * t["intraday"] / tot for e in expenses if e["head"] == "both" and t["total"])
    exp_non = sum(e["amount"] for e in expenses if e["head"] == "non_speculative") + \
        sum(e["amount"] * (t["futures"] + t["options"]) / tot for e in expenses if e["head"] == "both" and t["total"])
    if any(e["head"] == "both" for e in expenses) and not t["total"]:
        exp_non += sum(e["amount"] for e in expenses if e["head"] == "both")
    spec_h, non_h = head_totals(intr), head_totals(fno)
    spec_h.update(expenses=r2(exp_spec), income=r2(spec_h["net"] - exp_spec), turnover=t["intraday"])
    non_h.update(expenses=r2(exp_non), income=r2(non_h["net"] - exp_non), turnover=r2(t["futures"] + t["options"]),
                 futures=head_totals([r for r in fno if r["kind"] == "futures"]),
                 options=head_totals([r for r in fno if r["kind"] == "options"]))
    stg, stl = by_rate(st, "stcg_pct")
    ltg, ltl = by_rate(lt, "ltcg_pct")
    st_h, lt_h = head_totals(st), head_totals(lt)
    st_h.update(stt_not_deductible=_sum(st, "stt_not_deductible"))
    lt_h.update(stt_not_deductible=_sum(lt, "stt_not_deductible"),
                grandfathered=sum(1 for r in lt if r.get("grandfathered")))
    so = setoff({"speculative": spec_h["income"], "non_speculative": non_h["income"], "stcg_by_rate": stg,
                 "ltcg_by_rate": ltg, "stcl": stl, "ltcl": ltl}, bf, y)
    return {"yr": yr, "t": t, "spec_h": spec_h, "non_h": non_h, "st_h": st_h, "lt_h": lt_h, "stg": stg, "ltg": ltg,
            "so": so, "expenses": expenses}


def chain_carry_forward(paths, rows, y, bf, profile, notes):
    """Losses brought into FY y. carry-forward.csv holds what your filed returns carried (Schedule CFL); the
    years after the last one it covers (profile.json "carry_forward_as_of", else its latest fy) are worked out
    here from the trade book, year by year."""
    base = profile.get("carry_forward_as_of")
    base = parse_fy(base, 2000) if base and _fy_ok(base) else (max(b["fy"] for b in bf) if bf else None)
    if base is None:
        first = min((parse_date(r["date"]) for r in rows), default=None)
        base = fy_of(first) - 1 if first else y - 1
    done = []
    for z in range(max(base + 1, FIRST_FY), y):
        so = year_figures(paths, rows, z, bf)["so"]
        bf = [{"fy": parse_fy(c["fy"], 2000), "kind": c["kind"], "amount": c["amount"], "filed_on_time": True}
              for c in so["carry_forward"]]
        done.append(fy_label(z))
    if done:
        notes.append("Losses brought into FY %s include FY %s worked out here from the trade book (no filed return "
                     "on record for %s): they carry forward only if that return is filed by its due date." % (
                         fy_label(y), ", ".join(done), "it" if len(done) == 1 else "them"))
    return bf


def report(paths, fy=None, book="live", tradebooks=None, holdings_path=None, now=None):
    now = now or parse_now()
    as_of = now.date()
    y = fy if fy is not None else fy_of(as_of)
    if isinstance(y, str):
        y = parse_fy(y)
    if y < FIRST_FY:
        raise TaxError("FY %s is before FY 2018-19" % fy_label(y))
    if book not in BOOKS:
        raise TaxError("--book is live, paper or all")
    notes = []
    fills = load_fills(paths, book, tradebooks, notes)
    holdings = load_holdings(holdings_path or paths.tax("holdings.csv"))
    cfg = read_json(paths.t("config.json")) or {}
    rows, open_lots, open_fno, problems = classify(fills, holdings, as_of, cfg if isinstance(cfg, dict) else {})
    start, end = fy_bounds(y)
    later_fno = [r for r in rows if r["kind"] in ("futures", "options") and parse_date(r["date"]) > end
                 and parse_date(r["opened"]) <= end]
    a = act_for(y)
    profile = read_json(paths.tax("profile.json")) or {}
    bf = chain_carry_forward(paths, rows, y, load_carry_forward(paths.tax("carry-forward.csv")), profile, notes)
    f = year_figures(paths, rows, y, bf)
    yr, t, spec_h, non_h, st_h, lt_h, so, expenses = (f["yr"], f["t"], f["spec_h"], f["non_h"], f["st_h"], f["lt_h"],
                                                      f["so"], f["expenses"])
    ex = by_fy(LTCG_EXEMPTION, y)["amount"]
    lt_total = sum(a_ for _, a_ in so["ltcg_by_rate"])
    exempt = min(ex, lt_total)
    special, left = [], exempt
    for rate, amt in sorted(so["stcg_by_rate"], key=lambda b: -b[0]):
        special.append({"section": a["s"]["stcg"], "what": "short-term gains on STT-paid shares", "rate_pct": rate,
                        "amount": r2(amt), "at_rate": r2(amt * rate / 100.0)})
    for rate, amt in sorted(so["ltcg_by_rate"], key=lambda b: -b[0]):
        # the yearly exemption goes to the higher-rate part first (FY 2024-25: sales from 23 Jul 2024)
        e = min(left, amt)
        left -= e
        special.append({"section": a["s"]["ltcg"], "what": "long-term gains on STT-paid shares above the exemption",
                        "rate_pct": rate, "amount": r2(amt - e), "exempt": r2(e), "at_rate": r2((amt - e) * rate / 100)})
    taxable = {"speculative": so["speculative"], "non_speculative": so["non_speculative"],
               "business_total": r2(so["speculative"] + so["non_speculative"]),
               "stcg_special": r2(sum(x["amount"] for x in special if x["section"] == a["s"]["stcg"])),
               "ltcg_total": r2(lt_total), "ltcg_exempt": r2(exempt), "ltcg_taxable": r2(lt_total - exempt)}
    au = audit_check(t, r2(spec_h["income"] + non_h["income"]), y, profile)
    trades = [dict(r, head=HEADS[r["kind"]]) for r in yr]
    needs = [text for day, text in problems if start.isoformat() <= day[:10] <= end.isoformat()]
    if not fills:
        needs.insert(0, "no %s fills found: import your broker's tradebook (tax.py import FILE --broker zerodha) or "
                        "use --book paper for a practice report" % book)
    if book != "live":
        notes.insert(0, "PRACTICE: %s fills are simulated; a return reports your real broker's trades only" % (
            "paper" if book == "paper" else "paper and live"))
    if y >= 2026:
        notes.append("Tax year %s falls under the Income-tax Act, 2025: the same treatment with new section numbers. "
                     "ICAI has not yet issued its tax-audit guidance under the 2025 Act (its 2026 Guidance Note says "
                     "it will) and the return forms for tax year %s were not notified on 1 Nov 2026; the 1961 Act's "
                     "turnover method and the AY 2026-27 ITR-3 layout are shown." % (fy_label(y), fy_label(y)))
    if y == 2024:
        notes.append("FY 2024-25 has two rates: sales before 23 Jul 2024 at 15% / 10%, from 23 Jul 2024 at 20% / "
                     "12.5%; the Rs 1.25 lakh exemption covers the whole year.")
    if later_fno:
        notes.append("%d F&O trade%s opened in FY %s closed after 31 Mar: counted in the year closed (ICAI GN para "
                     "5.11(b)(iv))." % (len(later_fno), "" if len(later_fno) == 1 else "s", fy_label(y)))
    return {"schema": SCHEMA, "fy": fy_label(y), "period": {"from": start.isoformat(), "to": end.isoformat()},
            "act": a["act"], "year_word": a["year_word"], "as_of": now.strftime("%Y-%m-%dT%H:%M:%S"), "book": book,
            "complete": as_of > end, "disclaimer": DISCLAIMER,
            "heads": {"speculative": spec_h, "non_speculative": non_h, "stcg": dict(st_h, gains_by_rate=f["stg"]),
                      "ltcg": dict(lt_h, gains_by_rate=f["ltg"])},
            "turnover": t, "audit": au, "setoff": so, "taxable": taxable, "special_rate": special,
            "special_rate_note": "At the special rate only: before any shortfall of the basic exemption (resident "
                                 "individuals), surcharge and 4% cess. Business income is taxed at your slab rate, "
                                 "which this report does not compute.",
            "advance_tax": advance_schedule(yr, y, as_of), "itr": [{"what": k, "where": v} for k, v in ITR],
            "itr_form": a["form"], "expenses": expenses, "trades": trades,
            "open": {"delivery_lots": [l for l in open_lots if parse_date(l["buy_date"]) <= end],
                     "fno": [o for o in open_fno if o["since"] <= end.isoformat()]},
            "needs_input": needs, "notes": notes, "rules": rules_for(y),
            "sources": {"fills": len(fills), "holdings": len(holdings), "carry_forward": len(bf),
                        "expenses": len(expenses)}}


HEADS = {"intraday": "speculative business", "futures": "non-speculative business",
         "options": "non-speculative business", "stcg": "short-term capital gains", "ltcg": "long-term capital gains"}


def years(paths, book="all"):
    fills = load_fills(paths, book, None, [])
    return sorted({fy_label(fy_of(parse_date(f["at"]))) for f in fills if parse_date(f["at"])}, reverse=True)


# ---------------------------------------------------------------- words and exports

def markdown(d):
    h, s, t = d["heads"], d["setoff"], d["taxable"]
    L = ["# Tax estimate, FY %s (%s)" % (d["fy"], d["act"]), "",
         "> **%s**" % d["disclaimer"], "",
         "Book: %s. Period %s to %s%s. As of %s." % (d["book"], d["period"]["from"], d["period"]["to"],
                                                  "" if d["complete"] else " (the year is not over)", d["as_of"]), ""]
    for n in d["notes"]:
        L.append("- %s" % n)
    L += ["", "## Income per head", "", "| head | trades | gross | charges | expenses | net |", "|---|---:|---:|---:|---:|---:|"]
    sp, ns = h["speculative"], h["non_speculative"]
    L.append("| Speculative business (intraday equity) | %d | %s | %s | %s | %s |" % (
        sp["trades"], inr(sp["gross"]), inr(sp["charges"]), inr(sp["expenses"]), inr(sp["income"])))
    L.append("| Non-speculative business (F&O) | %d | %s | %s | %s | %s |" % (
        ns["trades"], inr(ns["gross"]), inr(ns["charges"]), inr(ns["expenses"]), inr(ns["income"])))
    for k, label in (("stcg", "Short-term capital gains (STT-paid shares)"), ("ltcg", "Long-term capital gains")):
        x = h[k]
        L.append("| %s | %d | %s | %s | | %s |" % (label, x["trades"], inr(x["gross"]), inr(x["charges"]), inr(x["net"])))
    L += ["", "Capital gains: STT paid (%s) is not deductible; other charges are part of the cost or transfer "
          "expenses." % inr(h["stcg"]["stt_not_deductible"] + h["ltcg"]["stt_not_deductible"]), "",
          "## Turnover and the audit check", "",
          "| part | turnover |", "|---|---:|",
          "| Intraday equity | %s |" % inr(d["turnover"]["intraday"]),
          "| Futures | %s |" % inr(d["turnover"]["futures"]),
          "| Options | %s |" % inr(d["turnover"]["options"]),
          "| **Business turnover** | **%s** |" % inr(d["turnover"]["total"]), "",
          d["turnover"]["method"], "", d["turnover"]["premium_words"], "",
          "**%s.**" % d["audit"]["status"].capitalize(), ""]
    L += ["- %s" % w for w in d["audit"]["words"]]
    L += ["", "## Set-off and carry-forward", ""]
    L += ["- %s" % w for w in s["steps"]] or ["- Nothing to set off."]
    if s["lapsed"]:
        L += ["", "Lapsed:"] + ["- FY %s %s %s: %s" % (x["fy"], KIND_WORDS[x["kind"]], inr(x["amount"]), x["why"])
                               for x in s["lapsed"]]
    L += ["", "| loss | from FY | amount | usable until FY |", "|---|---|---:|---|"]
    L += ["| %s | %s | %s | %s |" % (KIND_WORDS[c["kind"]], c["fy"], inr(c["amount"]), c["usable_until"])
          for c in s["carry_forward"]] or ["| none | | | |"]
    L += ["", "Carrying a loss forward needs the return filed by its due date.", "",
          "## Taxable amounts", "", "| | amount |", "|---|---:|",
          "| Speculative business income (slab rate) | %s |" % inr(t["speculative"]),
          "| Non-speculative business income (slab rate) | %s |" % inr(t["non_speculative"]),
          "| Long-term gains before the exemption | %s |" % inr(t["ltcg_total"]),
          "| Long-term exemption used | %s |" % inr(t["ltcg_exempt"]), "",
          "| special rate | section | amount | rate | at that rate |", "|---|---|---:|---:|---:|"]
    L += ["| %s | %s | %s | %s%% | %s |" % (x["what"], x["section"], inr(x["amount"]), _pct(x["rate_pct"]),
                                           inr(x["at_rate"])) for x in d["special_rate"]] or ["| none | | | | |"]
    L += ["", d["special_rate_note"], "", "## Advance tax", "", "| due | cumulative | business net to date | capital "
          "gains to date | |", "|---|---:|---:|---:|---|"]
    L += ["| %s | %d%% | %s | %s | %s |" % (x["due"], x["cumulative_pct"], inr(x["business_net_to_date"]),
                                           inr(x["capital_gains_to_date"]), x["status"])
          for x in d["advance_tax"]["dates"]]
    L += [""] + ["- %s" % w for w in d["advance_tax"]["words"]]
    L += ["", "## Where each number goes (%s)" % d["itr_form"], ""] + ["- **%s**: %s" % (x["what"], x["where"])
                                                                       for x in d["itr"]]
    if d["needs_input"]:
        L += ["", "## Needs your input", ""] + ["- %s" % x for x in d["needs_input"]]
    L += ["", "## Rules used (dated, with sources)", "", "| what | rule | section | source | effective |",
          "|---|---|---|---|---|"]
    L += ["| %s | %s | %s | %s | %s |" % (r["what"], r["rule"], r["section"], r["source"], r["effective"])
          for r in d["rules"]]
    L += ["", "> %s" % d["disclaimer"], ""]
    return "\n".join(L)


def _csv(rows, cols):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) if not isinstance(r.get(c), (dict, list)) else json.dumps(r.get(c)) for c in cols])
    return buf.getvalue()


def export(paths, d):
    folder = paths.tax("FY%s" % d["fy"])
    note = "# %s\n" % d["disclaimer"]
    h, t = d["heads"], d["taxable"]
    summary = [
        {"item": "speculative business income (intraday)", "amount": h["speculative"]["income"], "itr": "Schedule BP B"},
        {"item": "non-speculative business income (F&O)", "amount": h["non_speculative"]["income"], "itr": "Schedule BP A"},
        {"item": "short-term capital gains, net", "amount": h["stcg"]["net"], "itr": "Schedule CG A2"},
        {"item": "long-term capital gains, net", "amount": h["ltcg"]["net"], "itr": "Schedule 112A / CG B4"},
        {"item": "business turnover (ICAI)", "amount": d["turnover"]["total"], "itr": "Part A-GEN / P&L"},
        {"item": "taxable speculative after set-off", "amount": t["speculative"], "itr": "Schedule CYLA / BFLA"},
        {"item": "taxable non-speculative after set-off", "amount": t["non_speculative"], "itr": "Schedule CYLA / BFLA"},
        {"item": "short-term gains at the special rate", "amount": t["stcg_special"], "itr": "Schedule SI"},
        {"item": "long-term gains taxable above the exemption", "amount": t["ltcg_taxable"], "itr": "Schedule SI"},
    ]
    files = {
        "report.md": markdown(d),
        "summary.csv": note + _csv(summary, ["item", "amount", "itr"]),
        "trades.csv": note + _csv(d["trades"], ["date", "kind", "head", "symbol", "qty", "opened", "bought",
                                                "buy_price", "sell_price", "gross", "charges", "net", "turnover",
                                                "premium_sold", "sale_value", "cost", "expenses",
                                                "stt_not_deductible", "grandfathered", "held_days", "expired"]),
        "carry-forward.csv": note + _csv(d["setoff"]["carry_forward"], ["fy", "kind", "amount", "usable_until"]),
        "tax.json": json.dumps(d, indent=1, sort_keys=True) + "\n",
    }
    for name, text in files.items():
        write_text(os.path.join(folder, name), text)
    return {"folder": folder, "files": sorted(files)}


def summary_view(d):
    """The small version for the UI and the MCP tool: no per-trade rows."""
    out = {k: v for k, v in d.items() if k != "trades"}
    out["trade_count"] = len(d["trades"])
    out["trades_preview"] = d["trades"][:20]
    return out


def print_report(d):
    h, t = d["heads"], d["taxable"]
    print("Tax ESTIMATE, FY %s (%s), book: %s" % (d["fy"], d["act"], d["book"]))
    print("  %s" % d["disclaimer"])
    for n in d["notes"]:
        print("  note: %s" % n)
    print("\nIncome per head")
    print("  speculative (intraday)      %14s  over %d trades, charges %s" % (
        inr(h["speculative"]["income"]), h["speculative"]["trades"], inr(h["speculative"]["charges"])))
    print("  non-speculative (F&O)       %14s  over %d trades, charges %s" % (
        inr(h["non_speculative"]["income"]), h["non_speculative"]["trades"], inr(h["non_speculative"]["charges"])))
    print("  short-term capital gains    %14s" % inr(h["stcg"]["net"]))
    print("  long-term capital gains     %14s  (%d grandfathered)" % (inr(h["ltcg"]["net"]), h["ltcg"]["grandfathered"]))
    print("\nTurnover (ICAI): %s  [intraday %s, futures %s, options %s]" % (
        inr(d["turnover"]["total"]), inr(d["turnover"]["intraday"]), inr(d["turnover"]["futures"]),
        inr(d["turnover"]["options"])))
    print("Audit: %s" % d["audit"]["status"])
    for w in d["audit"]["words"]:
        print("  - %s" % w)
    print("\nSet-off")
    for w in d["setoff"]["steps"] or ["nothing to set off"]:
        print("  - %s" % w)
    for c in d["setoff"]["carry_forward"]:
        print("  carry forward: %s from FY %s, %s, usable until FY %s" % (KIND_WORDS[c["kind"]], c["fy"],
                                                                         inr(c["amount"]), c["usable_until"]))
    for c in d["setoff"]["lapsed"]:
        print("  lapsed: %s from FY %s, %s: %s" % (KIND_WORDS[c["kind"]], c["fy"], inr(c["amount"]), c["why"]))
    print("\nTaxable: business %s (slab rate); long-term %s, exempt %s" % (
        inr(t["business_total"]), inr(t["ltcg_total"]), inr(t["ltcg_exempt"])))
    for x in d["special_rate"]:
        print("  %s %s at %s%%: %s" % (x["section"], inr(x["amount"]), _pct(x["rate_pct"]), inr(x["at_rate"])))
    print("  (%s)" % d["special_rate_note"])
    print("\nAdvance tax: " + "; ".join("%s %d%% (%s)" % (x["due"], x["cumulative_pct"], x["status"])
                                        for x in d["advance_tax"]["dates"]))
    for x in d["needs_input"]:
        print("needs your input: %s" % x)


# ---------------------------------------------------------------- command line

def _common(p):
    p.add_argument("--workdir", default=".", help="the folder holding trader/ (default: here)")
    p.add_argument("--now", help="pin the clock, IST, e.g. \"2026-11-01 10:00\"")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _report_args(p):
    p.add_argument("--fy", help="financial year, e.g. 2025-26 (default: the current one)")
    p.add_argument("--book", choices=BOOKS, default="live", help="live (default): imported broker trade books; "
                                                                  "paper: a practice report; all: both")
    p.add_argument("--tradebook", action="append", default=[], metavar="FILE",
                   help="also read this broker file (not stored); repeat for several")
    p.add_argument("--broker", help="the broker of --tradebook files (detected from the columns when left out)")
    p.add_argument("--holdings", help="opening lots CSV (default trader/tax/holdings.csv)")
    _common(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="A financial-year tax ESTIMATE of your trading for discussion with "
                                             "a CA (trader-tax). Not tax advice; places no order.")
    sub = ap.add_subparsers(dest="cmd")
    _report_args(sub.add_parser("report", help="income per head, turnover and audit check, set-off, special rates"))
    _report_args(sub.add_parser("export", help="write report.md, summary.csv, trades.csv, carry-forward.csv and "
                                               "tax.json to trader/tax/FY<year>/"))
    p = sub.add_parser("rules", help="the rules used for a year, with sections, sources and dates")
    p.add_argument("--fy")
    _common(p)
    p = sub.add_parser("years", help="the financial years that have fills")
    p.add_argument("--book", choices=BOOKS, default="all")
    _common(p)
    p = sub.add_parser("import", help="import a broker trade book with trader-journal's importer")
    p.add_argument("file")
    p.add_argument("--broker")
    p.add_argument("--date", help="for a file with times but no dates (Angel One)")
    _common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    paths = Paths(a.workdir)
    try:
        now = parse_now(a.now)
        if a.cmd in ("report", "export"):
            d = report(paths, parse_fy(a.fy) if a.fy else None, a.book,
                       [(f, a.broker) for f in a.tradebook], a.holdings, now)
            if a.cmd == "export":
                res = export(paths, d)
                print(json.dumps(res, indent=1) if a.json else "Wrote %s: %s\n%s" % (
                    res["folder"], ", ".join(res["files"]), DISCLAIMER))
            elif a.json:
                print(json.dumps(d, indent=1, sort_keys=True))
            else:
                print_report(d)
        elif a.cmd == "rules":
            y = parse_fy(a.fy) if a.fy else fy_of(now.date())
            rows = rules_for(y)
            if a.json:
                print(json.dumps({"fy": fy_label(y), "act": act_for(y)["act"], "rules": rows,
                                  "disclaimer": DISCLAIMER}, indent=1))
            else:
                print("Rules for FY %s (%s)" % (fy_label(y), act_for(y)["act"]))
                for r in rows:
                    print("- %s: %s [%s; %s; from %s]" % (r["what"], r["rule"], r["section"], r["source"],
                                                         r["effective"]))
                print(DISCLAIMER)
        elif a.cmd == "years":
            ys = years(paths, a.book)
            print(json.dumps({"years": ys}) if a.json else ("\n".join(ys) or "no fills yet"))
        elif a.cmd == "import":
            jr = journal_api()
            if jr is None:
                raise TaxError("importing needs trader-journal next to this folder")
            try:
                res = jr.do_import(jr.Paths(paths.workdir), a.file, a.broker, a.date, jr.config_of(jr.Paths(paths.workdir)))
            except jr.JournalError as e:
                raise TaxError(str(e))
            print(json.dumps(res, indent=1) if a.json else "Imported %d fills (%d new) from %s into %s" % (
                res["read"], res["new"], res["broker"], res["file"]))
    except TaxError as e:
        print("tax.py: %s" % e, file=sys.stderr)
        return 1
    return 0


def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
