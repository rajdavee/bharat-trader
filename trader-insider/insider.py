#!/usr/bin/env python3
"""Insider and big-money tracker for trader-insider (NSE). Standard library only.

Reads three kinds of public NSE disclosures and flags the ones in stocks you
hold or watch:

  deals    bulk deals (one client trading more than 0.5% of a company's shares
           in a day) and block deals (a single large trade in the block window)
  insider  SEBI PIT disclosures: trades by promoters, directors, designated
           employees and their relatives, filed under Regulation 7
  watch    both, alerting once per new item (seen ids in trader/insider-seen.json)

    python3 insider.py deals
    python3 insider.py deals --symbol STLTECH
    python3 insider.py deals --days 7 --min-value-cr 50
    python3 insider.py deals --client "jhunjhunwala"
    python3 insider.py insider --days 7 --promoters-only
    python3 insider.py insider --symbol RELIANCE --days 365
    python3 insider.py watch

Sources (public NSE pages, no key; a browser-like User-Agent and the
nseindia.com Referer are sent):
  https://nsearchives.nseindia.com/content/equities/bulk.csv     the latest day
  https://nsearchives.nseindia.com/content/equities/block.csv    the latest day
  https://www.nseindia.com/api/historicalOR/bulk-block-short-deals
      ?optionType=bulk_deals|block_deals&from=DD-MM-YYYY&to=DD-MM-YYYY&csv=true
      (the JSON form stops at 70 rows; the CSV form returns them all)
  https://www.nseindia.com/api/corporates-pit-gg?index=equities
      &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY[&symbol=X]
      PIT filings from May 2026 on: one row per filing with its XBRL; the
      people, quantities and values are inside the XBRL
  https://www.nseindia.com/api/corporates-pit?index=equities&from_date=..&to_date=..
      the older PIT list (up to early May 2026), one row per transaction

Value is quantity x price for deals, and the value the filer reported for PIT
transactions. Amounts in ₹ crore (1 crore = 1,00,00,000).

Reads trader/portfolio.json (positions) and trader/watchlist.json (hits) to
flag your stocks, and trader/config.json key insider_watch_clients (a list of
names, matched case-insensitively inside the client name) for notable buyers
and sellers. --offline DIR reads saved responses instead of the network.
"""

import argparse
import csv
import datetime as _dt
import gzip
import io
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

BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
HIST_URL = ("https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
            "?optionType=%s&from=%s&to=%s&csv=true")
PIT_URL = "https://www.nseindia.com/api/corporates-pit-gg?index=equities&from_date=%s&to_date=%s"
PIT_LEGACY_URL = "https://www.nseindia.com/api/corporates-pit?index=equities&from_date=%s&to_date=%s"
NEW_PIT_FROM = _dt.date(2026, 5, 1)       # NSE moved PIT filings to the XBRL list here

# Well-known market participants whose deals people follow. Edit in
# trader/config.json -> "insider_watch_clients".
DEFAULT_WATCH_CLIENTS = [
    "JHUNJHUNWALA", "RADHAKISHAN DAMANI", "DERIVE TRADING", "ASHISH KACHOLIA",
    "VIJAY KEDIA", "KEDIA SECURITIES", "DOLLY KHANNA", "MUKUL MAHAVIR AGRAWAL",
    "SUNIL SINGHANIA", "ABAKKUS", "PORINJU", "NEMISH SHAH", "MADHUSUDAN KELA",
    "AKASH BHANSHALI", "ENAM", "LIFE INSURANCE CORPORATION OF INDIA",
    "GOVERNMENT PENSION FUND GLOBAL", "SMALLCAP WORLD FUND", "GOLDMAN SACHS",
    "MORGAN STANLEY", "SOCIETE GENERALE", "BOFA SECURITIES", "PLUTUS WEALTH",
]
DEFAULT_ALERT_CR = 25.0


class InsiderError(Exception):
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


def qty(n):
    """6601900 -> '66,01,900'."""
    if n is None:
        return "-"
    n = int(round(n))
    return ("-" if n < 0 else "") + _group(str(abs(n)))


def crore(rupees):
    """Rupees -> '₹53.39 cr' (2 decimals; whole crore from ₹1,000 cr)."""
    if rupees is None:
        return "-"
    v = Decimal(str(rupees / CRORE)).quantize(Decimal(1) if abs(rupees) >= 1000 * CRORE else PAISE,
                                              rounding=ROUND_HALF_UP)
    sign = "-" if v < 0 else ""
    whole, _, frac = format(abs(v), "f").partition(".")
    return "%s₹%s%s cr" % (sign, _group(whole), "." + frac if frac else "")


def to_number(text):
    """'66,44,519' -> 6644519.0; '-', '' -> None."""
    if text is None:
        return None
    t = str(text).strip().replace(",", "")
    if t in ("", "-", "NA", "Nil", "nil"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- dates

def today_ist():
    return _dt.datetime.now(IST).date()


def nse_date(s):
    """'28-OCT-2026', '29-Oct-2026 16:20:38', '2026-10-28' -> datetime (or None)."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt)
        except ValueError:
            continue
    return None


def ddmmyyyy(d):
    return d.strftime("%d-%m-%Y")


def weekdays_between(a, b):
    """Weekdays after a up to and including b (exchange holidays not removed)."""
    if a is None or b is None or b < a:
        return None
    n, d = 0, a
    while d < b:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def window(days, today, start=None, end=None):
    """(from, to) dates: the last `days` calendar days ending today, or explicit."""
    if start or end:
        s = nse_date(start).date() if start else today - _dt.timedelta(days=max(1, days) - 1)
        e = nse_date(end).date() if end else today
        if s is None or e is None:
            raise InsiderError("dates are DD-MM-YYYY")
        return s, e
    return today - _dt.timedelta(days=max(1, days) - 1), today


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite GET with retries for NSE; or reads saved files with offline=DIR."""

    def __init__(self, offline=None, pause=0.4, retries=3):
        self.offline, self.pause, self.retries, self._last = offline, pause, retries, 0.0

    def raw(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise InsiderError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        delay = 1.0
        for attempt in range(self.retries + 1):
            gap = time.time() - self._last
            if gap < self.pause:
                time.sleep(self.pause - gap)
            self._last = time.time()
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, text/csv, */*",
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
                raise InsiderError("HTTP %s for %s" % (exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise InsiderError("network error for %s: %s" % (url, exc))
        raise InsiderError("gave up on %s" % url)

    def json(self, url, name):
        data = self.raw(url, name)
        try:
            return json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise InsiderError("NSE did not return JSON for %s (blocked or down? try later)" % url)


# ---------------------------------------------------------------- your stocks

def load_json(path, default):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def my_stocks(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json"):
    """-> {"held": set(symbols), "watched": set(symbols)} from trader/ files (missing = empty)."""
    held, watched = set(), set()
    p = load_json(portfolio, {})
    pos = p.get("positions") if isinstance(p, dict) else None
    if isinstance(pos, dict):
        held = {str(s).upper() for s, v in pos.items() if not isinstance(v, dict) or v.get("qty", 1)}
    elif isinstance(pos, list):
        held = {str(x.get("symbol", "")).upper() for x in pos if isinstance(x, dict)}
    w = load_json(watchlist, {})
    hits = w.get("hits") if isinstance(w, dict) else (w if isinstance(w, list) else None)
    for h in hits or []:
        sym = h.get("symbol") if isinstance(h, dict) else h
        if sym:
            watched.add(str(sym).upper().replace(".NS", ""))
    for sym in (w.get("symbols") or []) if isinstance(w, dict) else []:
        watched.add(str(sym).upper().replace(".NS", ""))
    held.discard("")
    return {"held": held, "watched": watched - held}


def watch_clients(config="trader/config.json"):
    cfg = load_json(config, {})
    names = cfg.get("insider_watch_clients") if isinstance(cfg, dict) else None
    if not isinstance(names, list):
        names = DEFAULT_WATCH_CLIENTS
    return [" ".join(str(n).upper().split()) for n in names if str(n).strip()]


def alert_threshold(config="trader/config.json"):
    cfg = load_json(config, {})
    try:
        return float(cfg.get("insider_alert_min_cr", DEFAULT_ALERT_CR))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_ALERT_CR


def notable(name, clients):
    n = " ".join(str(name or "").upper().split())
    return next((c for c in clients if c and c in n), None)


def tag(item, mine, clients=()):
    tags = []
    sym = item.get("symbol")
    if sym in mine.get("held", ()):
        tags.append("HELD")
    elif sym in mine.get("watched", ()):
        tags.append("WATCH")
    who = item.get("client") or item.get("person")
    hit = notable(who, clients)
    if hit:
        tags.append("NOTABLE")
    item["tags"] = tags
    return item


# ---------------------------------------------------------------- deals

def _norm(h):
    return re.sub(r"[^a-z]", "", (h or "").lower())


COLS = {
    "date": "date", "symbol": "symbol", "securityname": "name", "clientname": "client",
    "buysell": "side", "quantitytraded": "qty", "tradepricewghtavgprice": "price",
    "remarks": "remarks",
}


def parse_deals(text, kind):
    """bulk.csv / block.csv / historical CSV text -> list of deals."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    head = [COLS.get(_norm(h)) for h in rows[0]]
    if "symbol" not in head or "qty" not in head:
        raise InsiderError("unexpected %s deals file (columns: %s)" % (kind, ", ".join(rows[0])))
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        d = {k: (r[i].strip() if i < len(r) else "") for i, k in enumerate(head) if k}
        when = nse_date(d.get("date"))
        q, p = to_number(d.get("qty")), to_number(d.get("price"))
        if not d.get("symbol") or q is None or p is None:
            continue
        value = q * p
        out.append({
            "kind": kind, "date": when.date().isoformat() if when else d.get("date"),
            "symbol": d["symbol"].upper(), "name": d.get("name"),
            "client": " ".join((d.get("client") or "").split()),
            "side": (d.get("side") or "").upper(), "qty": q, "price": p,
            "value": round(value, 2), "value_cr": round(value / CRORE, 2),
            "remarks": None if d.get("remarks") in (None, "", "-") else d.get("remarks")})
    return out


def deal_id(d):
    return "deal:%s:%s:%s:%s:%d:%.2f" % (d["date"], d["symbol"], d["client"].upper(), d["side"],
                                         int(d["qty"]), d["price"])


def dedupe_deals(deals):
    """One row per (date, symbol, client, side, qty, price). NSE's bulk list repeats
    some block-window trades; the block row is kept."""
    best = {}
    for d in deals:
        k = deal_id(d)
        if k not in best or (d["kind"] == "block" and best[k]["kind"] != "block"):
            best[k] = d
    return list(best.values())


def filter_deals(deals, symbol=None, client=None, min_value_cr=None, side=None):
    out = []
    c = " ".join(client.upper().split()) if client else None
    for d in deals:
        if symbol and d["symbol"] != symbol:
            continue
        if c and c not in d["client"].upper():
            continue
        if min_value_cr is not None and d["value_cr"] < min_value_cr:
            continue
        if side and d["side"] != side:
            continue
        out.append(d)
    return out


def counterparties(deals):
    """Block-style pairs: same date, symbol, quantity and price, one BUY and one SELL."""
    groups = {}
    for d in deals:
        groups.setdefault((d["date"], d["symbol"], int(d["qty"]), d["price"]), []).append(d)
    pairs = []
    for (date, sym, q, p), ds in sorted(groups.items()):
        buys = [d for d in ds if d["side"] == "BUY"]
        sells = [d for d in ds if d["side"] == "SELL"]
        if len(buys) == 1 and len(sells) == 1 and buys[0]["client"] != sells[0]["client"]:
            pairs.append({"date": date, "symbol": sym, "qty": q, "price": p,
                          "buyer": buys[0]["client"], "seller": sells[0]["client"],
                          "value": buys[0]["value"], "kind": buys[0]["kind"]})
    return pairs


def fetch_deals(fetcher, start=None, end=None, kinds=("bulk", "block")):
    """Today's files (start None) or the historical CSV for a date range."""
    deals, notes = [], []
    for kind in kinds:
        if start is None:
            url, name = (BULK_URL, "bulk.csv") if kind == "bulk" else (BLOCK_URL, "block.csv")
        else:
            opt = "%s_deals" % kind
            f, t = ddmmyyyy(start), ddmmyyyy(end)
            url, name = HIST_URL % (opt, f, t), "hist-%s-%s-%s.csv" % (opt, f, t)
        try:
            text = fetcher.raw(url, name).decode("utf-8-sig", "replace")
            if text.lstrip().startswith(("<", "{")):
                raise InsiderError("NSE sent a page, not the CSV (busy or blocked)")
            deals.extend(parse_deals(text, kind))
        except InsiderError as exc:
            notes.append("%s deals unavailable: %s" % (kind, exc))
    return dedupe_deals(deals), notes


# ---------------------------------------------------------------- PIT

def _local(tag_):
    return tag_.rsplit("}", 1)[-1] if "}" in tag_ else tag_


PIT_FIELDS = {
    "NameOfThePerson": "person", "CategoryOfPerson": "category",
    "TypeOfInstrument": "instrument", "TypeOfInstrumentOthers": "instrument_other",
    "SecuritiesAcquiredOrDisposedTransactionType": "type",
    "SecuritiesAcquiredOrDisposedNumberOfSecurity": "qty",
    "SecuritiesAcquiredOrDisposedValueOfSecurity": "value",
    "ModeOfAcquisitionOrDisposal": "mode",
    "SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity": "before_qty",
    "SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding": "before_pct",
    "SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity": "after_qty",
    "SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding": "after_pct",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate": "trade_from",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate": "trade_to",
    "DateOfIntimationToCompany": "intimated",
    "ExchangeOnWhichTheTradeWasExecuted": "exchange",
}
NUMERIC = {"qty", "value", "before_qty", "before_pct", "after_qty", "after_pct"}


def parse_pit_xbrl(data):
    """A PIT XBRL (Regulation 7 disclosure) -> (header dict, [transactions])."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsiderError("could not read the PIT XBRL (%s)" % exc)
    head, by_ctx, order = {}, {}, []
    for el in root:
        ctx = el.get("contextRef")
        if not ctx:
            continue
        name, text = _local(el.tag), (el.text or "").strip()
        if name in PIT_FIELDS:
            if ctx not in by_ctx:
                by_ctx[ctx] = {}
                order.append(ctx)
            key = PIT_FIELDS[name]
            by_ctx[ctx][key] = to_number(text) if key in NUMERIC else text
        elif name in ("Symbol", "NameOfTheCompany", "DisclosureUnderRegulation", "DateOfFiling",
                      "ISINCode", "RevisedFilling"):
            head[name] = text
    return head, [by_ctx[c] for c in order]


def finish_pit(t, symbol, company, filed, regulation, url, source):
    """Fill the derived fields of one PIT transaction."""
    t = dict(t)
    t["symbol"], t["company"], t["regulation"], t["url"], t["source"] = (
        (symbol or "").upper(), company, regulation, url, source)
    t["filed"] = filed.isoformat(sep=" ") if isinstance(filed, _dt.datetime) else filed
    for k in ("trade_from", "trade_to", "intimated"):
        d = nse_date(t.get(k))
        t[k] = d.date().isoformat() if d else None
    if t.get("instrument") and t.get("instrument_other") and "other" in t["instrument"].lower():
        t["instrument"] = t["instrument_other"]
    if (t.get("instrument") or "").lower() in ("equity shares", "equity share"):
        t["instrument"] = "Equity"
    t["person"] = " ".join((t.get("person") or "").split())
    t["type"] = (t.get("type") or "").strip()
    q, v = t.get("qty"), t.get("value")
    t["value_cr"] = round(v / CRORE, 2) if v else None
    t["price"] = round(v / q, 2) if q and v else None
    t["promoter"] = "promoter" in (t.get("category") or "").lower()
    t["market"] = (t.get("mode") or "").lower() in ("market purchase", "market sale")
    t["suspect_value"] = bool(q and v and v / q < 1 and t["type"] in ("Buy", "Sell"))
    fd = nse_date(t["filed"])
    td = nse_date(t["trade_to"] or t["trade_from"])
    t["weekdays_to_disclose"] = weekdays_between(td.date(), fd.date()) if fd and td else None
    t["late"] = t["weekdays_to_disclose"] is not None and t["weekdays_to_disclose"] > 4
    return t


def pit_id(t):
    return "pit:%s:%s:%s:%s:%s:%s" % (t["symbol"], t["person"].upper(), t["type"],
                                      int(t.get("qty") or 0), int(t.get("value") or 0),
                                      t.get("trade_to") or t.get("trade_from"))


def dedupe_pit(rows):
    """The same transaction can arrive twice (a filing posted twice, or repeated inside
    one filing). Keep the latest filed copy."""
    best = {}
    for t in rows:
        k = pit_id(t)
        if k not in best or (t.get("filed") or "") > (best[k].get("filed") or ""):
            best[k] = t
    return sorted(best.values(), key=lambda t: (t.get("filed") or "", t["symbol"]), reverse=True)


def legacy_pit_rows(data):
    """Rows of the older corporates-pit list (one transaction each)."""
    out = []
    for r in (data or {}).get("data", []) if isinstance(data, dict) else []:
        t = {"person": r.get("acqName"), "category": r.get("personCategory"),
             "instrument": r.get("secType"), "type": r.get("tdpTransactionType"),
             "qty": to_number(r.get("secAcq")), "value": to_number(r.get("secVal")),
             "mode": r.get("acqMode"), "before_qty": to_number(r.get("befAcqSharesNo")),
             "before_pct": to_number(r.get("befAcqSharesPer")),
             "after_qty": to_number(r.get("afterAcqSharesNo")),
             "after_pct": to_number(r.get("afterAcqSharesPer")),
             "trade_from": r.get("acqfromDt"), "trade_to": r.get("acqtoDt"),
             "intimated": r.get("intimDt"), "exchange": r.get("exchange")}
        out.append(finish_pit(t, r.get("symbol"), r.get("company"), nse_date(r.get("date")),
                              "Regulation " + (r.get("anex") or "7(2)"), r.get("xbrl"), "legacy"))
    return out


def fetch_pit(fetcher, start, end, symbol=None, mine=None, max_filings=60, cache=None):
    """PIT transactions filed between start and end (dates). -> (rows, notes)."""
    notes, rows = [], []
    f, t = ddmmyyyy(start), ddmmyyyy(end)
    suffix = ("-" + symbol) if symbol else ""
    q = "&symbol=" + urllib.parse.quote(symbol, safe="") if symbol else ""
    listing = []
    if end >= NEW_PIT_FROM:
        try:
            data = fetcher.json(PIT_URL % (f, t) + q, "pit-%s-%s%s.json" % (f, t, suffix))
            listing = data.get("data", []) if isinstance(data, dict) else []
        except InsiderError as exc:
            notes.append("PIT filings list unavailable: %s" % exc)
    if start < NEW_PIT_FROM + _dt.timedelta(days=7):
        try:
            data = fetcher.json(PIT_LEGACY_URL % (f, t) + q, "pit-legacy-%s-%s%s.json" % (f, t, suffix))
            rows.extend(legacy_pit_rows(data))
        except InsiderError as exc:
            notes.append("older PIT list unavailable: %s" % exc)
    mine = mine or {"held": set(), "watched": set()}
    mystocks = mine["held"] | mine["watched"]
    listing = [r for r in listing if r.get("xmlFileName")]
    listing.sort(key=lambda r: (r.get("symbol") not in mystocks,
                                -(nse_date(r.get("broadcastDateTime")) or _dt.datetime.min).timestamp()
                                if nse_date(r.get("broadcastDateTime")) else 0))
    skipped = max(0, len(listing) - max_filings)
    failed = 0
    cache = cache if cache is not None else {}
    for r in listing[:max_filings]:
        url = r["xmlFileName"]
        filed = nse_date(r.get("broadcastDateTime"))
        if url in cache:
            head, txs = cache[url]["head"], cache[url]["txs"]
        else:
            try:
                head, txs = parse_pit_xbrl(fetcher.raw(url, os.path.basename(urllib.parse.urlparse(url).path)))
            except InsiderError:
                failed += 1
                continue
            cache[url] = {"head": head, "txs": txs}
        for tx in txs:
            tx = dict(tx)
            for k in ("before_pct", "after_pct"):
                # XBRL "pure": 0.3049 means 30.49%; a few filers type the percent itself
                if isinstance(tx.get(k), float) and 0 <= tx[k] <= 1:
                    tx[k] = round(tx[k] * 100, 4)
            rows.append(finish_pit(tx, r.get("symbol") or head.get("Symbol"),
                                   r.get("companyName") or head.get("NameOfTheCompany"), filed,
                                   r.get("regulation") or head.get("DisclosureUnderRegulation"),
                                   url, "xbrl"))
    if skipped:
        notes.append("%d more filings in the window were not opened (raise --max-filings, or add "
                     "--symbol)." % skipped)
    if failed:
        notes.append("%d filings could not be read." % failed)
    s0, e0 = start.isoformat(), end.isoformat()
    rows = [x for x in rows if not x.get("filed") or s0 <= x["filed"][:10] <= e0]
    if symbol:
        rows = [x for x in rows if x["symbol"] == symbol]
    return dedupe_pit(rows), notes


def net_by_symbol(rows):
    """Net market buying (+) or selling (-) value per symbol, promoters and others apart."""
    out = {}
    for t in rows:
        if not t["market"] or not t.get("value") or t["suspect_value"]:
            continue
        if not (t.get("instrument") or "Equity").lower().startswith("equity"):
            continue
        sign = 1 if t["type"] == "Buy" else (-1 if t["type"] == "Sell" else 0)
        if not sign:
            continue
        o = out.setdefault(t["symbol"], {"promoter": 0.0, "other": 0.0, "count": 0})
        o["promoter" if t["promoter"] else "other"] += sign * t["value"]
        o["count"] += 1
    return out


# ---------------------------------------------------------------- rendering

def _flags(item):
    return ("  [%s]" % ", ".join(item["tags"])) if item.get("tags") else ""


def render_deals(deals, pairs, notes, title):
    out = [title, ""]
    if not deals:
        out.append("No deals match.")
    else:
        out.append("%-10s %-5s %-12s %-4s %13s %12s %13s  %s" % (
            "date", "kind", "symbol", "side", "quantity", "price", "value", "client"))
        for d in deals:
            out.append("%-10s %-5s %-12s %-4s %13s %12s %13s  %s%s" % (
                d["date"], d["kind"], d["symbol"], d["side"], qty(d["qty"]), inr(d["price"]),
                crore(d["value"]), d["client"], _flags(d)))
    if pairs:
        out.append("")
        out.append("Matched trades (one buyer, one seller, same quantity and price):")
        for p in pairs:
            out.append("  %s %s: %s sold %s shares to %s at %s (%s)." % (
                p["date"], p["symbol"], p["seller"], qty(p["qty"]), p["buyer"], inr(p["price"]),
                crore(p["value"])))
    for n in notes:
        out.append("NOTE: " + n)
    out.append("")
    out.append("Bulk deal: one client trading over 0.5% of a company's shares in a day. Block "
               "deal: a single trade of ₹10 crore or more in the block window. Public, after "
               "the fact; a deal is not a reason to trade.")
    return "\n".join(out) + "\n"


def describe_pit(t):
    equity = (t.get("instrument") or "Equity").lower().startswith("equity")
    parts = ["%s %s %s" % (t["type"].upper() or "?", qty(t.get("qty")), t.get("instrument") or "")]
    if t["suspect_value"]:
        parts.append("value %s as filed (looks like a per-share price)" % inr(t["value"]))
    elif t.get("value"):
        parts.append(crore(t["value"]) + (" at ~%s/%s" % (inr(t["price"]), "share" if equity else "unit")
                                         if t.get("price") else ""))
    parts.append("via %s" % (t.get("mode") or "?"))
    when = t.get("trade_from") or "?"
    if t.get("trade_to") and t["trade_to"] != t.get("trade_from"):
        when += " to " + t["trade_to"]
    parts.append("traded " + when)
    if t.get("weekdays_to_disclose") is not None:
        n = t["weekdays_to_disclose"]
        parts.append(("disclosed the same day" if n == 0 else
                      "disclosed %d weekday%s later" % (n, "" if n == 1 else "s")) +
                     (" (late?)" if t["late"] else ""))
    if t.get("before_pct") is not None and t.get("after_pct") is not None and \
            (t["before_pct"] or t["after_pct"]):
        parts.append("holding %.2f%% -> %.2f%%" % (t["before_pct"], t["after_pct"]))
    return "%-11s %s (%s): %s%s" % (t["symbol"], t["person"], t.get("category") or "?",
                                    ", ".join(p.strip() for p in parts), _flags(t))


def render_pit(rows, notes, title):
    out = [title, ""]
    if not rows:
        out.append("No disclosures match.")
    day = None
    for t in rows:
        d = (t.get("filed") or "")[:10]
        if d != day:
            day = d
            out.append("Filed %s:" % (d or "?"))
        out.append("  " + describe_pit(t))
    net = net_by_symbol(rows)
    if net:
        out.append("")
        out.append("Net market buying (+) / selling (-) by value, from these disclosures:")
        for sym, n in sorted(net.items(), key=lambda kv: -abs(kv[1]["promoter"] + kv[1]["other"])):
            bits = []
            if n["promoter"]:
                bits.append("promoter side %s" % crore(n["promoter"]))
            if n["other"]:
                bits.append("others %s" % crore(n["other"]))
            out.append("  %-11s %s" % (sym, ", ".join(bits)))
    for n in notes:
        out.append("NOTE: " + n)
    out.append("")
    out.append("SEBI PIT Regulations 2015: insiders tell the company within 2 trading days of a "
               "trade worth over ₹10 lakh in a quarter, and the company tells the exchange within "
               "2 trading days. Pledges, gifts, ESOPs and inter-se transfers are not market "
               "trades. Public disclosures, after the fact.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- watch

def load_seen(path):
    s = load_json(path, {})
    return s if isinstance(s, dict) and isinstance(s.get("seen"), list) else {"seen": [], "last_check": None}


def save_seen(path, state, keep=5000):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    state["seen"] = state["seen"][-keep:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def alertable_deal(d, min_cr):
    return bool(d.get("tags")) or d["value_cr"] >= min_cr


def alertable_pit(t, min_cr):
    if t.get("tags"):
        return True
    if t["market"] and (t["promoter"] or "director" in (t.get("category") or "").lower()):
        return True
    return bool(t.get("value_cr") and t["value_cr"] >= min_cr and not t["suspect_value"])


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--watchlist", default="trader/watchlist.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--today", help="YYYY-MM-DD (IST); default today")
        p.add_argument("--offline", metavar="DIR", help="read saved responses from DIR")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("deals", help="bulk and block deals")
    p.add_argument("--symbol")
    p.add_argument("--client", help="part of the client name, e.g. \"kacholia\"")
    p.add_argument("--min-value-cr", type=float, help="only deals worth at least this many crore")
    p.add_argument("--side", choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("--kind", choices=["bulk", "block", "all"], default="all")
    p.add_argument("--days", type=int, help="the last N calendar days (default: the latest files)")
    p.add_argument("--from", dest="start", help="DD-MM-YYYY")
    p.add_argument("--to", dest="end", help="DD-MM-YYYY")
    p.add_argument("--mine", action="store_true", help="only stocks you hold or watch")
    p.add_argument("--limit", type=int, default=60)
    common(p)

    p = sub.add_parser("insider", help="SEBI PIT insider trading disclosures")
    p.add_argument("--symbol")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--from", dest="start", help="DD-MM-YYYY")
    p.add_argument("--to", dest="end", help="DD-MM-YYYY")
    p.add_argument("--promoters-only", action="store_true")
    p.add_argument("--market-only", action="store_true", help="market purchases and sales only")
    p.add_argument("--mine", action="store_true", help="only stocks you hold or watch")
    p.add_argument("--max-filings", type=int, default=60, help="PIT filings to open (default 60)")
    common(p)

    p = sub.add_parser("watch", help="new deals and disclosures since the last check, once each")
    p.add_argument("--days", type=int, default=3, help="PIT window in days (default 3)")
    p.add_argument("--seen", default="trader/insider-seen.json")
    p.add_argument("--min-value-cr", type=float, help="alert on any item at least this big "
                   "(default insider_alert_min_cr in config, else %g)" % DEFAULT_ALERT_CR)
    p.add_argument("--all", action="store_true", help="alert on every new item, not just notable")
    p.add_argument("--dry-run", action="store_true", help="do not update the seen file")
    p.add_argument("--max-filings", type=int, default=60)
    common(p)
    return ap


def _symbol(s):
    if not s:
        return None
    sym = s.strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    if not SYMBOL_RE.match(sym):
        raise InsiderError("%r is not an NSE symbol (e.g. RELIANCE, M&M, BAJAJ-AUTO)" % s)
    return sym


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except InsiderError as exc:
        print("insider: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    today = _dt.date.fromisoformat(args.today) if args.today else today_ist()
    fetcher = Fetcher(offline=args.offline)
    mine = my_stocks(args.portfolio, args.watchlist)
    clients = watch_clients(args.config)

    if args.cmd == "deals":
        sym = _symbol(args.symbol)
        kinds = ("bulk", "block") if args.kind == "all" else (args.kind,)
        start = end = None
        if args.days or args.start or args.end:
            start, end = window(args.days or 1, today, args.start, args.end)
        deals, notes = fetch_deals(fetcher, start, end, kinds)
        if start is not None and not deals and notes and args.days:
            notes.append("falling back to the latest bulk.csv / block.csv")
            deals, more = fetch_deals(fetcher, None, None, kinds)
            notes.extend(more)
        deals = filter_deals(deals, sym, args.client, args.min_value_cr,
                             args.side.upper() if args.side else None)
        for d in deals:
            tag(d, mine, clients)
        if args.mine:
            deals = [d for d in deals if {"HELD", "WATCH"} & set(d["tags"])]
        pairs = counterparties(deals)
        deals.sort(key=lambda d: (not d["tags"], -d["value"]))
        shown = deals[:args.limit] if args.limit else deals
        if len(deals) > len(shown):
            notes.append("%d more deals not shown (--limit)." % (len(deals) - len(shown)))
        dates = sorted({d["date"] for d in deals})
        span = (dates[0] if dates[0] == dates[-1] else "%s to %s" % (dates[0], dates[-1])) \
            if dates else (("%s to %s" % (start, end)) if start else "latest day")
        if args.json:
            print(json.dumps({"deals": shown, "pairs": pairs, "notes": notes,
                              "total": len(deals)}, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render_deals(shown, pairs, notes,
                                          "NSE bulk and block deals, %s: %d deals, value = "
                                          "quantity x price" % (span, len(deals))))
        return 0

    if args.cmd == "insider":
        sym = _symbol(args.symbol)
        start, end = window(args.days, today, args.start, args.end)
        rows, notes = fetch_pit(fetcher, start, end, sym, mine, args.max_filings)
        if args.promoters_only:
            rows = [t for t in rows if t["promoter"]]
        if args.market_only:
            rows = [t for t in rows if t["market"]]
        for t in rows:
            tag(t, mine, clients)
        if args.mine:
            rows = [t for t in rows if {"HELD", "WATCH"} & set(t["tags"])]
        if args.json:
            print(json.dumps({"from": start.isoformat(), "to": end.isoformat(), "rows": rows,
                              "net": net_by_symbol(rows), "notes": notes},
                             indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render_pit(rows, notes,
                                        "SEBI PIT disclosures filed %s to %s%s: %d transactions" % (
                                            start, end, (" for " + sym) if sym else "", len(rows))))
        return 0

    # watch
    min_cr = args.min_value_cr if args.min_value_cr is not None else alert_threshold(args.config)
    state = load_seen(args.seen)
    seen = set(state["seen"])
    deals, notes = fetch_deals(fetcher)
    start, end = window(args.days, today)
    pit, more = fetch_pit(fetcher, start, end, None, mine, args.max_filings)
    notes.extend(more)
    new_deals, new_pit = [], []
    for d in deals:
        tag(d, mine, clients)
        i = deal_id(d)
        if i not in seen and (args.all or alertable_deal(d, min_cr)):
            new_deals.append(d)
    for t in pit:
        tag(t, mine, clients)
        i = pit_id(t)
        if i not in seen and (args.all or alertable_pit(t, min_cr)):
            new_pit.append(t)
    new_deals.sort(key=lambda d: (not d["tags"], -d["value"]))
    new_pit.sort(key=lambda t: (not t["tags"], -(t.get("value") or 0)))
    if not args.dry_run:
        state["seen"].extend(deal_id(d) for d in new_deals)
        state["seen"].extend(pit_id(t) for t in new_pit)
        state["last_check"] = _dt.datetime.now(IST).isoformat(timespec="seconds")
        save_seen(args.seen, state)
    if args.json:
        print(json.dumps({"deals": new_deals, "insider": new_pit, "notes": notes},
                         indent=2, ensure_ascii=False))
        return 0
    if not new_deals and not new_pit:
        print("Nothing new since the last check.")
    for d in new_deals:
        print("ALERT %s deal %s: %s %s %s %s shares at %s (%s)%s" % (
            d["kind"], d["date"], d["symbol"], d["client"],
            "bought" if d["side"] == "BUY" else "sold", qty(d["qty"]), inr(d["price"]),
            crore(d["value"]), _flags(d)))
    for t in new_pit:
        print("ALERT insider filed %s: %s" % ((t.get("filed") or "?")[:16], describe_pit(t)))
    for n in notes:
        print("NOTE: " + n)
    return 0


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
