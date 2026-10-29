#!/usr/bin/env python3
"""Quarterly results reader for trader-analyst (NSE). Standard library only.

Reads the quarterly financial results a company filed with NSE (the XBRL
behind each results announcement) and lays out what changed, quarter by
quarter: revenue from operations, EBITDA (approximate), margins, net profit,
diluted EPS, growth against the same quarter a year earlier (YoY) and the
quarter before (QoQ), and the trailing twelve months. It also reads the board
meetings calendar to say when the next results are due.

    python3 facts.py RELIANCE
    python3 facts.py TCS INFY --quarters 8
    python3 facts.py HDFCBANK --standalone
    python3 facts.py TCS --json
    python3 facts.py RELIANCE TCS INFY HDFCBANK --metrics trader/metrics.json

Sources (public NSE pages, no key or login; a browser-like User-Agent and the
nseindia.com Referer are sent):
  https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=X
      &period_ended=all&type=Integrated%20Filing-%20Financials
      results filed from the March 2025 quarter on (SEBI "integrated filing")
  https://www.nseindia.com/api/corporates-financial-results?index=equities
      &symbol=X&period=Quarterly
      the older results list (stops at the December 2024 quarter)
  https://nsearchives.nseindia.com/corporate/xbrl/...xml   each filing's XBRL
  https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=X

How a quarter is read:
  - Each XBRL holds the quarter and, from the second quarter on, the
    year-to-date period too. The tool uses the context without dimensions
    whose period ends on the reporting date and lasts about three months, so
    the numbers are the quarter's, never the year-to-date.
  - Amounts are filed in rupees and shown in crore (1 crore = 1,00,00,000).
  - Consolidated by default (what the press reports); --standalone for the
    listed entity alone. A company that files no consolidated results is
    shown standalone, with a note.
  - Net profit is the profit attributable to owners of the parent when the
    filing has it (consolidated), else profit for the period.
  - EBITDA is an approximation: profit before tax + finance costs +
    depreciation and amortisation - other income. Exceptional items stay
    inside it; the tool notes them.
  - Banks file a different template (interest earned, provisions). For them
    revenue is total income, and the tool shows net interest income and
    operating profit before provisions instead of EBITDA.
  - Indian financial year runs April to March: Q1 = Apr-Jun, Q2 = Jul-Sep,
    Q3 = Oct-Dec, Q4 = Jan-Mar. The quarter ending 30 Sep 2026 is Q2 FY27.

--metrics FILE merges one row per symbol into a JSON file. --offline DIR reads
saved responses instead of the network (results-SYMBOL.json,
results-legacy-SYMBOL.json, board-SYMBOL.json and each XBRL by its file name).
"""

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
CRORE = 10000000.0
PAISE = Decimal("0.01")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

INTEGRATED_URL = ("https://www.nseindia.com/api/integrated-filing-results?index=equities"
                  "&symbol=%s&period_ended=all&type=Integrated%%20Filing-%%20Financials")
LEGACY_URL = ("https://www.nseindia.com/api/corporates-financial-results?index=equities"
              "&symbol=%s&period=Quarterly")
BOARD_URL = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=%s"

# XBRL element names (local part; the prefix is in-capmkt for integrated
# filings and in-bse-fin for the older ones, the names are the same).
INDAS = {
    "revenue": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "expenses": ["Expenses"],
    "employee_cost": ["EmployeeBenefitExpense"],
    "finance_costs": ["FinanceCosts"],
    "dna": ["DepreciationDepletionAndAmortisationExpense"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax"],
    "pbt": ["ProfitBeforeTax"],
    "tax": ["TaxExpense"],
    "pat": ["ProfitLossForPeriod"],
    "owners_profit": ["ProfitOrLossAttributableToOwnersOfParent"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsLossPerShareFromContinuingOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations"],
}
BANK = {
    "revenue": ["Income"],
    "interest_earned": ["InterestEarned"],
    "interest_expended": ["InterestExpended"],
    "other_income": ["OtherIncome"],
    "operating_expenses": ["OperatingExpenses"],
    "ppop": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "exceptional": ["ExceptionalItems"],
    "pbt": ["ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense"],
    "pat": ["ProfitLossForThePeriod", "ProfitLossFromOrdinaryActivitiesAfterTax"],
    "owners_profit": ["ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "eps_basic": ["BasicEarningsPerShareAfterExtraordinaryItems",
                  "BasicEarningsPerShareBeforeExtraordinaryItems"],
    "eps_diluted": ["DilutedEarningsPerShareAfterExtraordinaryItems",
                    "DilutedEarningsPerShareBeforeExtraordinaryItems"],
}
QUARTER_OF_MONTH = {6: 1, 9: 2, 12: 3, 3: 4}
MONTHS = {1: "Apr-Jun", 2: "Jul-Sep", 3: "Oct-Dec", 4: "Jan-Mar"}


class FactsError(Exception):
    pass


# ---------------------------------------------------------------- formatting

def _group(whole):
    """'12345678' -> '1,23,45,678' (Indian grouping: thousand, lakh, crore)."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian digit grouping), -1234 -> '-₹1,234.00'."""
    d = Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = format(abs(d), "f").split(".")
    return "%s₹%s.%s" % (sign, _group(whole), frac)


def num(x, places=0):
    """Indian-grouped number: num(73188) -> '73,188', num(-1234.5, 1) -> '-1,234.5'."""
    if x is None:
        return "-"
    q = Decimal(1).scaleb(-places) if places else Decimal(1)
    d = Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    text = format(abs(d), "f")
    whole, _, frac = text.partition(".")
    return sign + _group(whole) + ("." + frac if frac else "")


def crore(rupees, places=None):
    """Rupees -> '₹73,188 cr' (whole crore from ₹100 cr up, else 2 decimals)."""
    if rupees is None:
        return "-"
    v = rupees / CRORE
    if places is None:
        places = 0 if abs(v) >= 100 else 2
    s = num(v, places)
    return (("-₹" + s[1:]) if s.startswith("-") else ("₹" + s)) + " cr"


def cr(rupees):
    """Rupees -> crore as a float rounded to 2 places (for JSON)."""
    return None if rupees is None else round(rupees / CRORE, 2)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v


def _m(v):
    return "-" if v is None else "%.1f%%" % v


# ---------------------------------------------------------------- dates

def fiscal(end):
    """date (or ISO string) of a quarter end -> 'Q2 FY27' (Indian FY, April to March)."""
    if isinstance(end, str):
        end = _dt.date.fromisoformat(end[:10])
    q = QUARTER_OF_MONTH.get(end.month)
    if q is None:
        return "%s" % end.isoformat()
    fy = end.year + 1 if end.month >= 4 else end.year
    return "Q%d FY%02d" % (q, fy % 100)


def months_label(end):
    if isinstance(end, str):
        end = _dt.date.fromisoformat(end[:10])
    q = QUARTER_OF_MONTH.get(end.month)
    return "%s %d" % (MONTHS[q], end.year) if q else end.isoformat()


def nse_date(s):
    """'30-SEP-2026', '17-Jul-2026 19:50:03', '16-Jan-2025 20:20' -> datetime (or None)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt)
        except ValueError:
            continue
    return None


def today_ist():
    return _dt.datetime.now(IST).date()


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite GET with retries for nseindia.com; or reads saved files with offline=DIR."""

    def __init__(self, offline=None, pause=0.4, retries=3):
        self.offline, self.pause, self.retries, self._last = offline, pause, retries, 0.0

    def raw(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        delay = 1.0
        for attempt in range(self.retries + 1):
            gap = time.time() - self._last
            if gap < self.pause:
                time.sleep(self.pause - gap)
            self._last = time.time()
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/", "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise FactsError("NSE has nothing at %s" % url)
                raise FactsError("HTTP %s for %s" % (exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FactsError("network error for %s: %s" % (url, exc))
        raise FactsError("gave up on %s" % url)

    def json(self, url, name):
        data = self.raw(url, name)
        try:
            return json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise FactsError("NSE did not return JSON for %s (blocked or down? try again later)" % url)


# ---------------------------------------------------------------- filings list

def filings(integrated, legacy):
    """Merge both NSE lists into one list of quarter filings, newest first.

    Each item: {"end", "nature" (consolidated|standalone), "audited", "filed",
    "xbrl", "bank", "source"}. One item per (end, nature): the integrated
    filing wins over the old list, and a later filing (a revision) over an
    earlier one.
    """
    items = []
    rows = integrated.get("data", []) if isinstance(integrated, dict) else (integrated or [])
    for r in rows:
        end, url = nse_date(r.get("qe_Date")), (r.get("xbrl") or "").strip()
        if not end or not url.lower().endswith(".xml"):
            continue
        filed = nse_date(r.get("broadcast_Date") or r.get("creation_Date"))
        items.append({
            "end": end.date().isoformat(),
            "nature": "consolidated" if (r.get("consolidated") or "").lower() == "consolidated"
                      else "standalone",
            "audited": r.get("audited"), "filed": filed.isoformat(sep=" ") if filed else None,
            "xbrl": url, "bank": "BANKING" in url.upper(), "source": "integrated",
            "revision": (r.get("type_Sub") or "Original") != "Original", "rank": 2})
    for r in legacy or []:
        end, url = nse_date(r.get("toDate")), (r.get("xbrl") or "").strip()
        start = nse_date(r.get("fromDate"))
        if not end or not url.lower().endswith(".xml"):
            continue
        if start and (end - start).days > 100:     # a half-year or year row, not a quarter
            continue
        filed = nse_date(r.get("filingDate") or r.get("broadCastDate"))
        flag = (r.get("bank") or "N").upper()
        items.append({
            "end": end.date().isoformat(),
            "nature": "consolidated" if (r.get("consolidated") or "").lower() == "consolidated"
                      else "standalone",
            "audited": r.get("audited"), "filed": filed.isoformat(sep=" ") if filed else None,
            "xbrl": url, "bank": flag not in ("N", "") or "BANKING" in url.upper(),
            "source": "legacy", "revision": False, "rank": 1})
    best = {}
    for it in items:
        key = (it["end"], it["nature"])
        cur = best.get(key)
        if cur is None or (it["rank"], it["filed"] or "") > (cur["rank"], cur["filed"] or ""):
            best[key] = it
    out = sorted(best.values(), key=lambda i: (i["end"], i["nature"]), reverse=True)
    for it in out:
        it.pop("rank", None)
    return out


# ---------------------------------------------------------------- XBRL

def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _number(text):
    if text is None:
        return None
    t = text.strip().replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return text.strip()
    return v


def parse_xbrl(data):
    """XBRL bytes -> {"start","end","days","facts":{name: value},"meta":{...},"quarterly":bool}.

    Picks the dimension-free context that ends on the reporting date with the
    shortest span (the quarter, not year-to-date)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FactsError("could not read the XBRL (%s)" % exc)
    contexts = {}
    facts = {}
    for el in root:
        name = _local(el.tag)
        if name == "context":
            cid = el.get("id")
            start = end = instant = None
            dims = False
            for sub in el.iter():
                n = _local(sub.tag)
                if n == "startDate":
                    start = (sub.text or "").strip()
                elif n == "endDate":
                    end = (sub.text or "").strip()
                elif n == "instant":
                    instant = (sub.text or "").strip()
                elif n in ("scenario", "segment"):
                    dims = True
            contexts[cid] = {"start": start, "end": end or instant, "duration": bool(start),
                             "dims": dims}
        elif el.get("contextRef"):
            facts.setdefault(name, []).append((el.get("contextRef"), el.text))
    meta = {}
    for key in ("Symbol", "NameOfTheCompany", "NameOfBank", "NatureOfReportStandaloneConsolidated",
                "LevelOfRounding", "LevelOfRoundingUsedInFinancialStatements",
                "WhetherResultsAreAuditedOrUnaudited", "DateOfStartOfReportingPeriod",
                "DateOfEndOfReportingPeriod", "ReportingQuarter",
                "DateOfBoardMeetingWhenFinancialResultsWereApproved"):
        if key in facts:
            meta[key] = (facts[key][0][1] or "").strip()
    durations = {cid: c for cid, c in contexts.items() if c["duration"] and not c["dims"]}
    if not durations:
        raise FactsError("no period in the XBRL")
    rep_end = meta.get("DateOfEndOfReportingPeriod") or max(c["end"] for c in durations.values())

    def span(c):
        return (_dt.date.fromisoformat(c["end"]) - _dt.date.fromisoformat(c["start"])).days + 1

    ending = [(span(c), cid) for cid, c in durations.items() if c["end"] == rep_end]
    if not ending:
        ending = [(span(c), cid) for cid, c in durations.items()]
    ending.sort()
    days = ending[0][0]
    # Some older filings label the year-to-date context with the quarter's
    # dates. Among contexts with the same span, the quarter is the one with
    # the smallest income (YTD >= quarter), and "OneD" is the usual quarter id.
    tied = [cid for d, cid in ending if d == days]

    def size(cid):
        for key in ("RevenueFromOperations", "Income", "InterestEarned"):
            for c, t in facts.get(key, []):
                v = _number(t)
                if c == cid and isinstance(v, float):
                    return abs(v)
        return float("inf")

    tied.sort(key=lambda cid: (size(cid), cid != "OneD", cid))
    chosen = tied[0]
    same = [chosen]
    values = {}
    for name, entries in facts.items():
        for cid in same:
            hit = [t for c, t in entries if c == cid]
            if hit:
                values[name] = _number(hit[0])
                break
    return {"start": durations[chosen]["start"], "end": durations[chosen]["end"], "days": days,
            "quarterly": 80 <= days <= 100, "facts": values, "meta": meta}


def _first(facts, names):
    for n in names:
        v = facts.get(n)
        if isinstance(v, float):
            return v
    return None


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100.0, 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole * 100.0, 1)


def quarter_row(parsed, filing=None):
    """Parsed XBRL -> one normalised quarter (amounts in rupees)."""
    f = parsed["facts"]
    bank = "InterestEarned" in f or bool(filing and filing.get("bank"))
    m = BANK if bank else INDAS
    row = {k: _first(f, names) for k, names in m.items()}
    row["template"] = "bank" if bank else "ind-as"
    row["end"], row["start"], row["days"] = parsed["end"], parsed["start"], parsed["days"]
    row["fiscal"] = fiscal(parsed["end"])
    row["months"] = months_label(parsed["end"])
    row["quarterly"] = parsed["quarterly"]
    row["net_profit"] = row["owners_profit"] if row.get("owners_profit") is not None else row["pat"]
    row["ebitda"] = None
    if bank:
        if row["interest_earned"] is not None and row["interest_expended"] is not None:
            row["nii"] = row["interest_earned"] - row["interest_expended"]
        else:
            row["nii"] = None
    else:
        parts = (row["pbt"], row["finance_costs"], row["dna"], row["other_income"])
        if all(v is not None for v in parts):
            row["ebitda"] = row["pbt"] + row["finance_costs"] + row["dna"] - row["other_income"]
    row["ebitda_margin"] = margin(row["ebitda"], row["revenue"])
    row["ppop_margin"] = margin(row.get("ppop"), row["revenue"]) if bank else None
    row["net_margin"] = margin(row["net_profit"], row["revenue"])
    meta = parsed["meta"]
    row["company"] = meta.get("NameOfTheCompany") or meta.get("NameOfBank")
    row["nature"] = (meta.get("NatureOfReportStandaloneConsolidated") or "").lower() or \
        (filing or {}).get("nature")
    row["audited"] = meta.get("WhetherResultsAreAuditedOrUnaudited") or (filing or {}).get("audited")
    if filing:
        row["xbrl"], row["filed"], row["source"] = filing["xbrl"], filing["filed"], filing["source"]
    return row


# ---------------------------------------------------------------- analysis

def _months_apart(a, b):
    a, b = _dt.date.fromisoformat(a), _dt.date.fromisoformat(b)
    return (a.year - b.year) * 12 + (a.month - b.month)


def analyse(rows, quarters=4):
    """Normalised quarter rows (any order) -> {"rows": newest first with YoY/QoQ, "ttm", "notes"}."""
    rows = sorted([r for r in rows if r], key=lambda r: r["end"], reverse=True)
    by_end = {r["end"]: r for r in rows}
    keys = ("revenue", "ebitda", "net_profit", "eps_diluted", "nii", "ppop")
    for i, r in enumerate(rows):
        yoy = next((o for o in rows if _months_apart(r["end"], o["end"]) == 12), None)
        qoq = rows[i + 1] if i + 1 < len(rows) and _months_apart(r["end"], rows[i + 1]["end"]) == 3 \
            else None
        for k in keys:
            r[k + "_yoy"] = pct(r.get(k), yoy.get(k)) if yoy else None
            r[k + "_qoq"] = pct(r.get(k), qoq.get(k)) if qoq else None
        r["yoy_end"] = yoy["end"] if yoy else None
    ttm = {}
    last4 = rows[:4]
    if len(last4) == 4 and _months_apart(last4[0]["end"], last4[3]["end"]) == 9:
        ttm = {"from": last4[3]["start"], "to": last4[0]["end"]}
        for k in ("revenue", "ebitda", "net_profit", "eps_diluted"):
            vals = [r.get(k) for r in last4]
            ttm[k] = round(sum(vals), 2) if all(v is not None for v in vals) else None
        prev = [by_end.get(_shift_year(r["end"])) for r in last4]
        if all(prev):
            for k in ("revenue", "net_profit"):
                vals = [p.get(k) for p in prev]
                if all(v is not None for v in vals) and ttm.get(k) is not None:
                    ttm[k + "_yoy"] = pct(ttm[k], sum(vals))
    return {"rows": rows[:quarters], "all_rows": rows, "ttm": ttm, "notes": notes_for(rows)}


def _shift_year(end):
    d = _dt.date.fromisoformat(end)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:      # 29 Feb
        return d.replace(year=d.year - 1, day=28).isoformat()


def notes_for(rows):
    notes = []
    if not rows:
        return notes
    cur = rows[0]
    if cur["template"] == "bank":
        notes.append("Bank results template: revenue is total income (interest earned + other "
                     "income); EBITDA does not apply. NII = interest earned - interest expended; "
                     "operating profit is before provisions.")
    exc = cur.get("exceptional")
    if exc:
        notes.append("%s has exceptional items of %s (before tax); they are inside profit%s."
                     % (cur["fiscal"], crore(exc),
                        "" if cur["template"] == "bank" else " and the EBITDA approximation"))
    oi, pbt = cur.get("other_income"), cur.get("pbt")
    if cur["template"] != "bank" and oi and pbt and pbt > 0 and oi / pbt >= 0.25:
        notes.append("Other income is %.0f%% of profit before tax in %s: a large part of profit "
                     "is not from operations." % (oi / pbt * 100, cur["fiscal"]))
    prev = next((o for o in rows[1:] if _months_apart(cur["end"], o["end"]) == 12), None)
    if prev and prev.get("exceptional"):
        notes.append("The year-ago quarter (%s) had exceptional items of %s, so the YoY change in "
                     "profit is not like for like." % (prev["fiscal"], crore(prev["exceptional"])))
    if prev and prev["template"] != "bank" and prev.get("other_income") and prev.get("pbt") \
            and prev["pbt"] > 0 and prev["other_income"] / prev["pbt"] >= 0.25:
        notes.append("Other income was %.0f%% of profit before tax in the year-ago quarter (%s, "
                     "%s), so the YoY change in profit is not like for like." % (
                         prev["other_income"] / prev["pbt"] * 100, prev["fiscal"],
                         crore(prev["other_income"])))
    ey, py = cur.get("eps_diluted_yoy"), cur.get("net_profit_yoy")
    if ey is not None and py is not None and abs(ey - py) > 20:
        notes.append("EPS moved %s YoY but net profit %s: the share count changed (bonus, split or "
                     "new shares?). NSE does not restate old EPS, so EPS growth is not comparable."
                     % (_p(ey), _p(py)))
    if not cur.get("quarterly", True):
        notes.append("The newest filing covers %d days, not a quarter: read it before comparing."
                     % cur["days"])
    if cur.get("revenue") is None:
        notes.append("No revenue line was found in this filing's template; the table shows what "
                     "could be read.")
    return notes


def changes(result):
    """Plain sentences on the newest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    r = rows[0]
    out = []
    head = "%s (%s, ended %s)" % (r["fiscal"], r["months"], r["end"])
    rev_name = "total income" if r["template"] == "bank" else "revenue"
    s = "%s: %s %s" % (head, rev_name, crore(r.get("revenue")))
    bits = []
    if r.get("revenue_yoy") is not None:
        bits.append("%s YoY" % _p(r["revenue_yoy"]))
    if r.get("revenue_qoq") is not None:
        bits.append("%s QoQ" % _p(r["revenue_qoq"]))
    out.append(s + (" (%s)" % ", ".join(bits) if bits else "") + ".")
    s = "Net profit %s" % crore(r.get("net_profit"))
    if r.get("net_profit_yoy") is not None:
        s += " (%s YoY)" % _p(r["net_profit_yoy"])
    if r.get("eps_diluted") is not None:
        s += ", diluted EPS %s" % inr(r["eps_diluted"])
        if r.get("eps_diluted_yoy") is not None:
            s += " (%s YoY)" % _p(r["eps_diluted_yoy"])
    out.append(s + ".")
    if r["template"] == "bank":
        if r.get("nii") is not None:
            s = "Net interest income %s" % crore(r["nii"])
            if r.get("nii_yoy") is not None:
                s += " (%s YoY)" % _p(r["nii_yoy"])
            if r.get("provisions") is not None:
                s += "; provisions %s" % crore(r["provisions"])
            out.append(s + ".")
    elif r.get("ebitda") is not None:
        s = "EBITDA (approx.) %s, margin %s" % (crore(r["ebitda"]), _m(r["ebitda_margin"]))
        prev = next((o for o in result.get("all_rows", rows) if o["end"] == r.get("yoy_end")), None)
        if prev and prev.get("ebitda_margin") is not None and r["ebitda_margin"] is not None:
            s += " (%+.1f points YoY)" % (r["ebitda_margin"] - prev["ebitda_margin"])
        s += "; net margin %s" % _m(r.get("net_margin"))
        out.append(s + ".")
    if r.get("yoy_end") is None:
        out.append("No filing for the same quarter a year earlier was found, so there is no YoY.")
    return out


def _clip(text, n):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


def next_results(meetings, today):
    """Board meetings list -> {"date","days","this_week","purpose","last"} for results meetings."""
    found = []
    for m in meetings or []:
        text = " ".join(str(m.get(k) or "") for k in ("bm_purpose", "bm_desc")).lower()
        if "result" not in text:
            continue
        d = nse_date(m.get("bm_date"))
        if d:
            found.append((d.date(), m))
    if not found:
        return None
    up = sorted(((d, m) for d, m in found if d >= today), key=lambda x: x[0])
    past = sorted(((d, m) for d, m in found if d < today), key=lambda x: x[0], reverse=True)
    out = {"next": None, "last": past[0][0].isoformat() if past else None}
    if up:
        d, m = up[0]
        purpose = m.get("bm_purpose") or ""
        if "result" not in purpose.lower():     # older rows have purpose and text swapped
            purpose = m.get("bm_desc") or purpose
        week_start = today - _dt.timedelta(days=today.weekday())
        out["next"] = {"date": d.isoformat(), "days": (d - today).days,
                       "this_week": week_start <= d <= week_start + _dt.timedelta(days=6),
                       "purpose": _clip(" ".join(purpose.split()), 180)}
    return out


# ---------------------------------------------------------------- rendering

def render(symbol, result):
    rows = result["rows"]
    name = result.get("company") or symbol
    out = ["%s - %s. %s results, ₹ crore except EPS. Source: NSE results XBRL." % (
        symbol, name, (result.get("nature") or "").capitalize()), ""]
    bank = rows and rows[0]["template"] == "bank"
    if bank:
        out.append("%-8s %-10s %10s %7s %7s %9s %9s %10s %7s %7s %9s %7s" % (
            "quarter", "ended", "income", "YoY", "QoQ", "NII", "op.profit", "net profit",
            "YoY", "margin", "EPS dil", "YoY"))
    else:
        out.append("%-8s %-10s %10s %7s %7s %9s %7s %10s %7s %7s %9s %7s" % (
            "quarter", "ended", "revenue", "YoY", "QoQ", "EBITDA*", "margin", "net profit",
            "YoY", "margin", "EPS dil", "YoY"))
    for r in rows:
        if bank:
            mid = "%9s %9s" % (num(cr(r.get("nii"))), num(cr(r.get("ppop"))))
        else:
            mid = "%9s %7s" % (num(cr(r.get("ebitda"))), _m(r.get("ebitda_margin")))
        out.append("%-8s %-10s %10s %7s %7s %s %10s %7s %7s %9s %7s" % (
            r["fiscal"], r["end"], num(cr(r.get("revenue"))), _p(r.get("revenue_yoy")),
            _p(r.get("revenue_qoq")), mid, num(cr(r.get("net_profit"))),
            _p(r.get("net_profit_yoy")), _m(r.get("net_margin")),
            "-" if r.get("eps_diluted") is None else "%.2f" % r["eps_diluted"],
            _p(r.get("eps_diluted_yoy"))))
    t = result.get("ttm") or {}
    if t:
        out.append("")
        s = "Trailing 4 quarters (%s to %s): revenue %s" % (t["from"], t["to"], crore(t.get("revenue")))
        if t.get("revenue_yoy") is not None:
            s += " (%s)" % _p(t["revenue_yoy"])
        s += ", net profit %s" % crore(t.get("net_profit"))
        if t.get("net_profit_yoy") is not None:
            s += " (%s)" % _p(t["net_profit_yoy"])
        if t.get("eps_diluted") is not None:
            s += ", diluted EPS %s (sum of quarters)" % inr(t["eps_diluted"])
        out.append(s + ".")
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    for n in result.get("notes", []):
        out.append("  - NOTE: " + n)
    cal = result.get("calendar")
    out.append("")
    if cal and cal.get("next"):
        n = cal["next"]
        when = "today" if n["days"] == 0 else ("tomorrow" if n["days"] == 1 else "in %d days" % n["days"])
        out.append("Next results: board meeting %s (%s%s). %s" % (
            n["date"], when, ", this week" if n["this_week"] else "", n["purpose"]))
    elif cal is not None:
        out.append("Next results: no board meeting for results announced yet%s." % (
            " (last one %s)" % cal["last"] if cal and cal.get("last") else ""))
    if rows and rows[0].get("xbrl"):
        out.append("Latest filing: %s, filed %s (%s)  %s" % (
            rows[0]["fiscal"], rows[0].get("filed") or "?", rows[0].get("audited") or "?",
            rows[0]["xbrl"]))
    if not bank and rows:
        out.append("* EBITDA is an approximation: profit before tax + finance costs + "
                   "depreciation & amortisation - other income.")
    return "\n".join(out) + "\n"


def metrics_row(symbol, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result.get("ttm") or {}
    cal = (result.get("calendar") or {}).get("next") or {}
    return {"symbol": symbol, "name": result.get("company"), "nature": result.get("nature"),
            "quarter": cur.get("fiscal"), "period_end": cur.get("end"),
            "revenue_cr": cr(cur.get("revenue")), "net_profit_cr": cr(cur.get("net_profit")),
            "eps_diluted": cur.get("eps_diluted"),
            "revenue_yoy": cur.get("revenue_yoy"), "net_profit_yoy": cur.get("net_profit_yoy"),
            "eps_yoy": cur.get("eps_diluted_yoy"), "ebitda_margin": cur.get("ebitda_margin"),
            "net_margin": cur.get("net_margin"), "ttm_revenue_cr": cr(t.get("revenue")),
            "ttm_net_profit_cr": cr(t.get("net_profit")), "ttm_eps": t.get("eps_diluted"),
            "next_results": cal.get("date"), "template": cur.get("template"),
            "source": "NSE results XBRL"}


# ---------------------------------------------------------------- one company

def company(symbol, fetcher, quarters=4, standalone=False, today=None, calendar=True):
    """Fetch and analyse one NSE symbol."""
    q = urllib.parse.quote(symbol, safe="")
    integrated = fetcher.json(INTEGRATED_URL % q, "results-%s.json" % symbol)
    try:
        legacy = fetcher.json(LEGACY_URL % q, "results-legacy-%s.json" % symbol)
    except FactsError:
        legacy = []
    items = filings(integrated, legacy)
    if not items:
        raise FactsError("NSE lists no quarterly results for %s (check the symbol; SME and "
                         "newly listed companies may have none)" % symbol)
    want = "standalone" if standalone else "consolidated"
    notes = []
    chosen = [i for i in items if i["nature"] == want]
    if not chosen:
        other = "consolidated" if standalone else "standalone"
        chosen = [i for i in items if i["nature"] == other]
        notes.append("%s files no %s results; showing %s." % (symbol, want, other))
        want = other
    need = chosen[:quarters + 4]
    rows, missing = [], []
    for it in need:
        try:
            data = fetcher.raw(it["xbrl"], os.path.basename(urllib.parse.urlparse(it["xbrl"]).path))
            parsed = parse_xbrl(data)
        except FactsError as exc:
            missing.append("%s (%s)" % (fiscal(it["end"]), exc))
            continue
        rows.append(quarter_row(parsed, it))
    result = analyse(rows, quarters=quarters)
    result["symbol"], result["nature"] = symbol, want
    result["company"] = next((r["company"] for r in rows if r.get("company")), None)
    result["notes"] = notes + result["notes"]
    if missing:
        result["notes"].append("Could not read: " + "; ".join(missing[:4]))
    if rows and rows[0]["template"] == "bank" and rows[0].get("revenue") is None:
        result["notes"].append("This bank filing uses a template the tool cannot read yet.")
    result["calendar"] = None
    if calendar:
        try:
            meetings = fetcher.json(BOARD_URL % q, "board-%s.json" % symbol)
            result["calendar"] = next_results(meetings, today or today_ist()) or {}
        except FactsError as exc:
            result["notes"].append("Board meetings calendar unavailable (%s)." % exc)
    return result


def _jsonable(result):
    out = dict(result)
    out.pop("all_rows", None)
    return out


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. RELIANCE TCS HDFCBANK")
    ap.add_argument("--quarters", type=int, default=5, help="quarters to show (default 5)")
    ap.add_argument("--standalone", action="store_true", help="standalone instead of consolidated")
    ap.add_argument("--no-calendar", action="store_true", help="skip the board meetings lookup")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--today", help="YYYY-MM-DD (IST) for the calendar; default today")
    ap.add_argument("--offline", metavar="DIR", help="read saved responses from DIR")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.symbols:
        ap.print_help()
        return 2
    today = _dt.date.fromisoformat(args.today) if args.today else None
    fetcher = Fetcher(offline=args.offline)
    results, rows, failed = [], [], 0
    for s in args.symbols:
        sym = s.strip().upper()
        if sym.endswith(".NS"):
            sym = sym[:-3]
        if not SYMBOL_RE.match(sym):
            print("facts: %r is not an NSE symbol (e.g. RELIANCE, M&M, BAJAJ-AUTO)" % s,
                  file=sys.stderr)
            failed += 1
            continue
        try:
            result = company(sym, fetcher, quarters=max(1, args.quarters),
                             standalone=args.standalone, today=today,
                             calendar=not args.no_calendar)
        except FactsError as exc:
            print("facts: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
            continue
        result["changes"] = changes(result)
        results.append(result)
        rows.append(metrics_row(sym, result))
        if not args.json:
            sys.stdout.write(render(sym, result))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps([_jsonable(r) for r in results], indent=2, ensure_ascii=False))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            try:
                with open(args.metrics, encoding="utf-8") as fh:
                    existing = {r["symbol"]: r for r in json.load(fh).get("companies", [])
                                if r.get("symbol")}
            except (OSError, ValueError):
                existing = {}
        for r in rows:
            existing[r["symbol"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": (today or today_ist()).isoformat(), "unit": "INR crore",
                       "companies": sorted(existing.values(), key=lambda r: r["symbol"])},
                      fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
