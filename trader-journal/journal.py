#!/usr/bin/env python3
"""trader-journal: the trade journal, its statistics, the rule-break log and the counterfactual ledger.

    python3 journal.py sync                              # rebuild trader/journal.json from every source
    python3 journal.py list                              # closed trades, newest first
    python3 journal.py show J20261026-1a2b3c             # one trade: fills, charges, R, MAE/MFE, rule notes
    python3 journal.py stats --by strategy               # expectancy, win rate with its interval, by group
    python3 journal.py review --week 2026-W44            # the weekly review (trader/journal/weekly-2026-W44.md)
    python3 journal.py counterfactual                    # what the skipped and blocked trades did next
    python3 journal.py import tradebook.csv --broker zerodha
    python3 journal.py note J20261026-1a2b3c "chased the breakout" --tag fomo

One entry per closed trade: an equity position from flat to flat, or an F&O strategy (every leg, rolls
included) as ONE entry. Sources:
  paper      trader/state/trades.json (trader-broker's paper fills, equity and F&O, expiry settlement)
  live       a broker's trade book saved by `ticket.py orders --broker NAME --json`, or the tradebook CSV
             or JSON downloaded from Zerodha, Upstox, Dhan, Angel One or Groww (journal.py import); a live
             fill is "ticketed" when its broker order id is in trader/state/shadow.json or broker.json
  context    trader/state/broker.json (each yes, its max loss and stop), trader/state/personas.json (the
             persona and setup that drafted the order), trader/state/risk.json (verdicts, captured into
             trader/journal/risk-log.jsonl on every sync because risk.json is overwritten), trader/regime/
             NIFTY.json (trader-regime history) and bars (trader-lab's loader: --bars DIR, the local
             archive, or Yahoo) for MAE/MFE and the counterfactual replays

Each entry: symbols, strategy/setup, persona, regime (NIFTY's trend and volatility as of the close
BEFORE the entry, plus the day's expiry flag), entry and exit times and prices, quantity and lots, gross
P&L, charges, net P&L, R multiple (net over the max loss recorded with the yes), MAE/MFE (from bars),
holding time, rule notes and your own notes and tags (trader/journal/notes.json).

Statistics: expectancy with an approximate 95% interval, win rate with its Wilson 95% interval, profit
factor, average win and loss, equity curve after charges, drawdown, streaks; by strategy, persona,
regime, time of day, weekday and instrument. Behaviour: risk overrides (a PASS that carried a
behaviour warning), trades after a circuit-breaker lock, revenge trades, oversizing, entries outside
the window, exits after flat-by, fills with no ticket, the overtrading index and the cost drag.

Counterfactual ledger: every co-trader candidate not taken (trader/cotrader/log.jsonl, schema
bharat-trader/cotrader-log/1) and every trader-risk BLOCK of your own order is replayed on the bars
after it with trader-lab's no-look-ahead fill rules (fill at the next bar's open plus slippage, skip if
it opens through the stop or target, stop first inside a bar, out at the flat-by or after the maximum
holding at the next open, net of charges), so "the tools saved you X and cost you Y" comes with counts
and caveats. Needs trader-lab next to this folder (its bar loader, slippage and charges).

Analytics only: it reads records and writes under trader/journal*. It never places, sizes or confirms
an order. Education and tooling, not financial advice.
"""

import argparse
import csv
import datetime
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP

SCHEMA = "bharat-trader/journal/1"
COTRADER_LOG_SCHEMA = "bharat-trader/cotrader-log/1"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
PAISE = Decimal("0.01")
MIN_SAMPLE = 20
Z95 = 1.959963984540054
NOT_SENT = ("why", "plan", "note")
BROKERS = ("zerodha", "upstox", "dhan", "angel", "groww", "kite", "paper")
GROUPS = ("strategy", "persona", "regime", "tod", "weekday", "instrument")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# the time-of-day buckets (trader-regime's defaults: open drive to 10:00, morning to 11:30, midday to 13:30,
# the closing window the last 30 minutes of continuous trading)
TOD = (("09:15", "open drive"), ("10:00", "morning"), ("11:30", "midday"), ("13:30", "afternoon"),
       ("15:00", "closing window"), ("15:30", "after the close"))
DEFAULTS = {"capital": 100000, "max_loss_per_trade": 1000, "max_trades_per_day": 3, "max_order_value": 50000,
            "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
            "max_consecutive_losses": 3, "max_daily_drawdown_pct": 3, "revenge_window_min": 15, "product": "MIS"}
DAILY_HOLD = 15          # bars a daily counterfactual is held at most when its plan gives no maximum
BEHAVIOUR_RULES = ("revenge", "breaker", "cooling-off", "trades a day", "loss", "window")


class JournalError(Exception):
    pass


# ---------------------------------------------------------------- money, time, files

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


def signed_inr(x):
    return ("+" if x is not None and x > 0.004 else "") + inr(x or 0)


def r2(x):
    return float(Decimal(repr(float(x))).quantize(PAISE, rounding=ROUND_HALF_UP))


def _f(x):
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_now(text=None):
    """--now text (naive = IST) or the clock, as a naive IST datetime."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise JournalError("--now wants a time like \"2026-10-30 16:00\" (IST)")
    return dt if dt.tzinfo is None else dt.astimezone(IST).replace(tzinfo=None)


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}


def ist_time(text, utc_z=True):
    """A fill time in any broker's spelling -> naive IST datetime, or None.
    ISO with an offset (a Z is UTC), "2026-10-26 10:10:05", "26-10-2026 10:10", "26/10/2026", "26-Oct-2026 10:10"."""
    s = str(text or "").strip()
    if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "-"):
        return None
    iso = s.replace("Z", "+00:00") if utc_z else s.rstrip("Z")
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("/", "-") if re.match(r"^\d{4}/", iso) else iso)
        return dt if dt.tzinfo is None else dt.astimezone(IST).replace(tzinfo=None)
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[-/ ]([A-Za-z]{3}|\d{1,2})[-/ ](\d{4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?)?$",
                 s, re.I)
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower()) if m.group(2).isalpha() else int(m.group(2))
    if not mon:
        return None
    hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
    if m.group(7):
        hh = hh % 12 + (12 if m.group(7).upper() == "PM" else 0)
    try:
        return datetime.datetime(int(m.group(3)), mon, int(m.group(1)), hh, mm, ss)
    except ValueError:
        return None


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def hhmm_min(text):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(text or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def read_json(path, default=None):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise JournalError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_jsonl(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def fingerprint(order):
    """Same function as trader-risk and trader-broker: identifies the exact order."""
    core = {k: v for k, v in order.items() if k not in NOT_SENT and v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Paths(object):
    def __init__(self, workdir="."):
        self.workdir = os.path.abspath(workdir or ".")
        self.trader = os.path.join(self.workdir, "trader")

    def t(self, *parts):
        return os.path.join(self.trader, *parts)

    @property
    def journal(self):
        return self.t("journal.json")

    def j(self, *parts):
        return os.path.join(self.trader, "journal", *parts)


# ---------------------------------------------------------------- sibling skills (optional)

def sibling(folder, script, name):
    """A sibling skill's script loaded by path (cached), or None when it is not installed next to this one."""
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


def lab_api():
    return sibling("trader-lab", "lab.py", "bt_journal_lab")


def regime_api():
    return sibling("trader-regime", "regime.py", "bt_journal_regime")


# ---------------------------------------------------------------- instruments

OPT_RE = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2}(?:[A-Z]{3}|[1-9OND]\d{2}))(\d+(?:\.\d+)?)(CE|PE)$")
FUT_RE = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2}[A-Z]{3})FUT$")
KEY_RE = re.compile(r"^([A-Z0-9&\-]+?)-(\d{4}-\d{2}-\d{2})-(?:(FUT)|(\d+(?:\.\d+)?)-(CE|PE))$")
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX")


def instrument_of(symbol, segment=None, underlying=None, option_type=None, strike=None, expiry=None):
    """(key, underlying, segment EQ / OPT / FUT) for a fill's symbol in any spelling: the paper broker's
    contract key (NIFTY-2026-11-03-22700-PE), an exchange trading symbol (NIFTY26N0322700PE,
    NIFTY26NOVFUT), or the parts a broker API gives (underlying, expiry, strike, option type)."""
    s = re.sub(r"\s+", "", str(symbol or "").upper())
    if s.endswith("-EQ") or s.endswith("-BE"):
        s = s[:-3]
    m = KEY_RE.match(s)
    if m:
        return s, m.group(1), "FUT" if m.group(3) else "OPT"
    ot = str(option_type or "").upper()
    if ot in ("CE", "PE") and underlying and expiry:
        k = _f(strike)
        sk = (str(int(k)) if k is not None and k == int(k) else repr(k)) if k is not None else "0"
        u = str(underlying).upper()
        return "%s-%s-%s-%s" % (u, expiry, sk, ot), u, "OPT"
    m = OPT_RE.match(s)
    if m:
        return s, m.group(1), "OPT"
    m = FUT_RE.match(s)
    if m:
        return s, m.group(1), "FUT"
    seg = str(segment or "").upper()
    if seg in ("OPT", "FUT"):
        return s, str(underlying or s).upper(), seg
    return s, s, "EQ"


def instrument_class(e):
    if e["kind"] == "fno":
        segs = set(e.get("segments") or [])
        index = e["underlying"] in INDEX_NAMES
        if segs == {"FUT"}:
            return "index futures" if index else "stock futures"
        return "index options" if index else "stock options"
    prod = e.get("product") or ""
    return "equity delivery (CNC)" if prod == "CNC" else "equity intraday (%s)" % (prod or "MIS")


# ---------------------------------------------------------------- fills: the paper log

def paper_fills(trades_doc):
    """trader/state/trades.json -> normalised fills (oldest first)."""
    rows = trades_doc.get("trades") if isinstance(trades_doc, dict) else trades_doc
    out = []
    for n, t in enumerate(rows or []):
        if not isinstance(t, dict):
            continue
        side = str(t.get("side") or "").upper()
        qty = _f(t.get("qty"))
        price = _f(t.get("price"))
        at = ist_time(t.get("at"))
        if side not in ("BUY", "SELL", "SETTLE") or qty is None or price is None or at is None:
            continue
        key, under, seg = instrument_of(t.get("symbol"), t.get("segment") or ("OPT" if t.get("exchange") == "NFO"
                                                                               else None), t.get("underlying"))
        if str(t.get("exchange") or "").upper() == "NFO" and seg == "EQ":
            seg = str(t.get("segment") or "OPT").upper()
        if t.get("underlying"):
            under = str(t["underlying"]).upper()
        out.append({
            "id": "paper:%d" % n, "at": stamp(at), "symbol": key, "underlying": under, "segment": seg,
            "exchange": str(t.get("exchange") or ("NFO" if seg != "EQ" else "NSE")).upper(),
            "product": str(t.get("product") or ("NRML" if seg != "EQ" else "MIS")).upper(), "side": side,
            "qty": int(qty), "price": price, "value": _f(t.get("value")), "charges": _f(t.get("charges")) or 0.0,
            "charges_estimated": False, "fingerprint": t.get("fingerprint"), "basket_id": t.get("basket_id"),
            "strategy_id": t.get("strategy_id"), "leg": t.get("leg"), "source": "paper", "paper": True,
            "realised_pnl": _f(t.get("realised_pnl")), "trade_id": None, "broker_order_id": None})
    return out


# ---------------------------------------------------------------- fills: broker tradebook imports

def _norm(h):
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


# Each broker's documented trade-book layout (column -> our field). Headers are matched case- and
# punctuation-blind ("Trade Date", "trade_date"). Sources, read 1 Nov 2026:
#   zerodha  Console > Reports > Tradebook, CSV (support.zerodha.com, "Where can the trades that are taken
#            for a particular period be seen?"): symbol, isin, trade_date, exchange, segment, series,
#            trade_type, auction, quantity, price, trade_id, order_id, order_execution_time (+ expiry_date)
#   kite     the Kite Connect trades API (kite.trade/docs/connect/v3/orders/#trades): tradingsymbol,
#            exchange, product, transaction_type, quantity, average_price, fill_timestamp, trade_id, order_id
#   upstox   Upstox API v2 historical trades (upstox.com/developer/api-documentation/get-historical-trades):
#            exchange, segment, option_type, quantity, amount, trade_id, trade_date, transaction_type,
#            scrip_name, strike_price, expiry, price, isin, symbol, instrument_token; the web Trade report
#            (Reports > Trade) carries the same fields
#   dhan     DhanHQ v2 trade history (dhanhq.co/docs/v2/statements/): orderId, exchangeTradeId,
#            transactionType, exchangeSegment, productType, tradingSymbol, customSymbol, tradedQuantity,
#            tradedPrice, sebiTax, stt, brokerageCharges, serviceTax, exchangeTransactionCharges,
#            stampDuty, exchangeTime, drvExpiryDate, drvOptionType, drvStrikePrice (charges per trade)
#   angel    Angel One SmartAPI trade book (smartapi.angelbroking.com/docs/Orders, getTradeBook):
#            exchange, producttype, tradingsymbol, instrumenttype, strikeprice, optiontype, expirydate,
#            transactiontype, fillprice, fillsize, orderid, fillid, filltime (time only: the file's date
#            comes from --date or a trade_date column)
#   groww    Groww Trade API trade list (groww.in/trade-api/docs): trading_symbol, exchange, segment,
#            product, transaction_type, quantity, price, groww_order_id, exchange_trade_id,
#            trade_date_time (UTC), trade_status
LAYOUTS = {
    "zerodha": {"symbol": ["symbol", "tradingsymbol"], "date": ["trade_date"], "time": ["order_execution_time"],
                "side": ["trade_type", "transaction_type"], "qty": ["quantity"], "price": ["price", "average_price"],
                "exchange": ["exchange"], "segment": ["segment"], "trade_id": ["trade_id"],
                "order_id": ["order_id"], "expiry": ["expiry_date"], "product": ["product"]},
    "kite": {"symbol": ["tradingsymbol"], "time": ["fill_timestamp", "exchange_timestamp", "order_timestamp"],
             "side": ["transaction_type"], "qty": ["quantity"], "price": ["average_price", "price"],
             "exchange": ["exchange"], "product": ["product"], "trade_id": ["trade_id"], "order_id": ["order_id"]},
    "upstox": {"symbol": ["symbol", "trading_symbol", "scrip_name"], "date": ["trade_date"],
               "time": ["trade_time", "exchange_timestamp"], "side": ["transaction_type", "trade_type", "side"],
               "qty": ["quantity"], "price": ["price", "average_price"], "exchange": ["exchange"],
               "segment": ["segment"], "trade_id": ["trade_id"], "order_id": ["order_id"],
               "expiry": ["expiry"], "strike": ["strike_price"], "option_type": ["option_type"],
               "product": ["product"]},
    "dhan": {"symbol": ["tradingsymbol", "trading_symbol", "customsymbol", "custom_symbol"],
             "time": ["exchangetime", "exchange_time", "createtime", "updatetime"],
             "side": ["transactiontype", "transaction_type"], "qty": ["tradedquantity", "traded_quantity"],
             "price": ["tradedprice", "traded_price"], "segment": ["exchangesegment", "exchange_segment"],
             "product": ["producttype", "product_type"], "trade_id": ["exchangetradeid", "exchange_trade_id"],
             "order_id": ["orderid", "order_id"], "expiry": ["drvexpirydate", "drv_expiry_date"],
             "strike": ["drvstrikeprice", "drv_strike_price"], "option_type": ["drvoptiontype", "drv_option_type"],
             "charges": ["sebitax", "stt", "brokeragecharges", "servicetax", "exchangetransactioncharges",
                         "stampduty", "sebi_tax", "brokerage_charges", "service_tax",
                         "exchange_transaction_charges", "stamp_duty"]},
    "angel": {"symbol": ["tradingsymbol"], "date": ["trade_date", "date"], "time": ["filltime", "fill_time"],
              "side": ["transactiontype", "transaction_type"], "qty": ["fillsize", "fill_size"],
              "price": ["fillprice", "fill_price"], "exchange": ["exchange"], "product": ["producttype"],
              "trade_id": ["fillid"], "order_id": ["orderid"], "expiry": ["expirydate"], "strike": ["strikeprice"],
              "option_type": ["optiontype"], "segment": ["instrumenttype"]},
    "groww": {"symbol": ["trading_symbol", "symbol"], "time": ["trade_date_time", "created_at"],
              "side": ["transaction_type"], "qty": ["quantity"], "price": ["price"], "exchange": ["exchange"],
              "segment": ["segment"], "product": ["product"], "trade_id": ["exchange_trade_id", "groww_trade_id"],
              "order_id": ["groww_order_id", "exchange_order_id"], "status": ["trade_status"]},
}
SIGNATURES = (("dhan", ("tradedquantity", "tradedprice")), ("angel", ("fillsize", "fillprice")),
              ("groww", ("trade_date_time",)), ("upstox", ("instrument_token",)),
              ("zerodha", ("order_execution_time",)), ("upstox", ("scrip_name", "transaction_type")),
              ("kite", ("fill_timestamp",)), ("zerodha", ("trade_type", "trade_date")))


def detect_broker(headers):
    hs = {_norm(h) for h in headers}
    for name, need in SIGNATURES:
        if all(n in hs for n in need):
            return name
    return None


def _rows_of(text):
    """CSV or JSON text -> (list of dicts with normalised keys, "csv" or "json")."""
    s = text.lstrip("﻿").strip()
    if s[:1] in ("[", "{"):
        try:
            doc = json.loads(s)
        except ValueError as e:
            raise JournalError("not valid JSON: %s" % e)
        rows = doc
        if isinstance(doc, dict):
            data = doc.get("data")
            if isinstance(data, dict):
                data = data.get("trade_list") or data.get("trades") or data.get("data")
            rows = doc.get("trades") or doc.get("trade_list") or data or []
        if not isinstance(rows, list):
            raise JournalError("no list of trades in the JSON")
        return [{_norm(k): v for k, v in r.items()} for r in rows if isinstance(r, dict)], "json"
    lines = s.splitlines()
    # a report may carry a title block above the header: start at the first line naming a known column
    known = {c for lay in LAYOUTS.values() for cols in lay.values() for c in cols}
    start = 0
    for i, line in enumerate(lines[:30]):
        cells = {_norm(c) for c in next(csv.reader([line]), [])}
        if len(cells & known) >= 3:
            start = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for r in reader:
        row = {_norm(k): (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k is not None}
        if any(v not in (None, "") for v in row.values()):
            out.append(row)
    return out, "csv"


def _pick(row, names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "NA", "N/A"):
            return v
    return None


def _neutral_live(rows):
    """trader-broker's broker-neutral trades (ticket.py orders --broker X --json): trade_id, broker_order_id,
    symbol, exchange, side, qty, price, product, time."""
    return bool(rows) and all(k in rows[0] for k in ("broker_order_id", "side", "qty", "price"))


def parse_tradebook(text, broker=None, day=None):
    """A broker's trade book (CSV or JSON) -> (fills, broker, notes). Each fill keeps the broker's trade id
    so a re-import replaces instead of doubling."""
    rows, fmt = _rows_of(text)
    if not rows:
        raise JournalError("no trades in the file")
    notes = []
    name = (broker or "").lower() or None
    if name == "kite" and "order_execution_time" in rows[0]:
        name = "zerodha"
    if _neutral_live(rows):
        lay = {"symbol": ["symbol"], "time": ["time"], "side": ["side"], "qty": ["qty"], "price": ["price"],
               "exchange": ["exchange"], "product": ["product"], "trade_id": ["trade_id"],
               "order_id": ["broker_order_id"]}
        name = name or "live"
    else:
        found = detect_broker(rows[0].keys())
        if name is None:
            name = found
        if name is None:
            raise JournalError("could not tell the broker from the columns (%s): pass --broker zerodha, upstox, "
                               "dhan, angel or groww" % ", ".join(sorted(rows[0].keys())[:12]))
        if name not in LAYOUTS:
            raise JournalError("unknown broker %r: zerodha, kite, upstox, dhan, angel or groww" % name)
        if found and found != name and not (found, name) in (("zerodha", "kite"), ("kite", "zerodha")):
            notes.append("the columns look like %s's layout, read as %s as asked" % (found, name))
        lay = LAYOUTS[name]
    fills, skipped = [], 0
    for n, r in enumerate(rows):
        status = str(_pick(r, lay.get("status", [])) or "").upper()
        if status and status not in ("EXECUTED", "COMPLETE", "TRADED", "FILLED"):
            skipped += 1
            continue
        side = str(_pick(r, lay["side"]) or "").strip().upper()
        side = {"B": "BUY", "S": "SELL", "BUY": "BUY", "SELL": "SELL"}.get(side[:4] if side else "", side)
        qty, price = _f(_pick(r, lay["qty"])), _f(_pick(r, lay["price"]))
        tval = _pick(r, lay.get("time", []))
        dval = _pick(r, lay.get("date", []))
        at = None
        if tval and re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", str(tval).strip()):
            d = ist_time(dval) if dval else (ist_time(day) if day else None)
            if d is not None:
                parts = [int(x) for x in str(tval).strip().split(":")] + [0]
                at = d.replace(hour=parts[0], minute=parts[1], second=parts[2])
        elif tval:
            at = ist_time(tval, utc_z=True)
        timed = at is not None
        if at is None and dval:
            at = ist_time(dval)
            timed = at is not None and bool(re.search(r"\d:\d", str(dval)))
        if side not in ("BUY", "SELL") or not qty or qty <= 0 or price is None or at is None:
            skipped += 1
            continue
        seg_raw = str(_pick(r, lay.get("segment", [])) or "").upper()
        exch = str(_pick(r, lay.get("exchange", [])) or "").upper()
        if not exch and "_" in seg_raw:
            exch = {"NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO"}.get(seg_raw, seg_raw.split("_")[0])
        fno = exch in ("NFO", "BFO") or any(x in seg_raw for x in ("FO", "FNO", "OPT", "FUT", "DERIV"))
        expiry = _pick(r, lay.get("expiry", []))
        exp_iso = None
        if expiry:
            e = ist_time(expiry)
            exp_iso = e.date().isoformat() if e else None
        key, under, seg = instrument_of(_pick(r, lay["symbol"]), "OPT" if fno else None,
                                        None, _pick(r, lay.get("option_type", [])),
                                        _pick(r, lay.get("strike", [])), exp_iso)
        if fno and seg == "EQ":
            seg = "FUT" if "FUT" in seg_raw or str(key).endswith("FUT") else "OPT"
        if not fno and seg != "EQ":
            fno = True
        charges, est = None, True
        if lay.get("charges"):
            parts = [_f(r.get(c)) for c in lay["charges"] if r.get(c) not in (None, "")]
            if parts:
                charges, est = r2(sum(p for p in parts if p is not None)), False
        product = str(_pick(r, lay.get("product", [])) or "").upper()
        product = {"INTRADAY": "MIS", "I": "MIS", "DELIVERY": "CNC", "D": "CNC", "MARGIN": "NRML",
                   "CARRYFORWARD": "NRML"}.get(product, product)
        if not product:
            product = "NRML" if fno else ""
        tid = str(_pick(r, lay.get("trade_id", [])) or "%s-%d" % (stamp(at), n))
        fills.append({"id": "%s:%s" % (name, tid), "at": stamp(at), "symbol": key, "underlying": under,
                      "segment": seg if fno else "EQ", "exchange": exch or ("NFO" if fno else "NSE"),
                      "product": product, "side": side, "qty": int(qty), "price": price,
                      "value": r2(qty * price), "charges": charges, "charges_estimated": est,
                      "fingerprint": None, "basket_id": None, "strategy_id": None, "leg": None, "source": name,
                      "paper": False, "realised_pnl": None, "trade_id": tid, "time_known": timed,
                      "broker_order_id": str(_pick(r, lay.get("order_id", [])) or "") or None})
    if skipped:
        notes.append("%d row%s skipped (no side, quantity, price or time, or not executed)" % (
            skipped, "" if skipped == 1 else "s"))
    return fills, name, notes


def estimate_charges(fills, cfg):
    """Charges for imported fills the broker file does not carry: trader-broker's paper charge model
    (your broker_profile's brokerage, STT, exchange, SEBI, stamp, GST), marked as an estimate."""
    broker = sibling("trader-broker", "ticket.py", "bt_journal_broker")
    for f in fills:
        if f.get("charges") is not None:
            continue
        if broker is None:
            f["charges"], f["charges_estimated"] = 0.0, True
            continue
        prof = dict(cfg or {})
        if f["source"] in ("zerodha", "kite", "upstox", "dhan", "angel", "groww") and not prof.get("broker_profile"):
            prof["broker_profile"] = "zerodha" if f["source"] == "kite" else f["source"]
        product = f.get("product") or ("NRML" if f["segment"] != "EQ" else "MIS")
        try:
            ch = broker.side_charges(f["side"], f["price"], f["qty"], prof,
                                     product if f["segment"] == "EQ" else product, f["segment"], f["at"][:10])
            f["charges"] = r2(ch["total"])
        except Exception:     # a broker-side model that cannot price this fill: say so, do not guess
            f["charges"] = 0.0
        f["charges_estimated"] = True
    return fills


# ---------------------------------------------------------------- context: persona tags, yes records, risk log

def persona_index(personas_doc):
    """Order fingerprint -> {persona, setup, strategy, idea} from trader/state/personas.json."""
    index = {}
    drafts = personas_doc.get("drafts") if isinstance(personas_doc, dict) else None
    for d in drafts if isinstance(drafts, list) else []:
        orders = [o for o in (d.get("orders") or []) if isinstance(o, dict)] if isinstance(d, dict) else []
        for o in orders:
            plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
            setup = str(plan.get("setup") or "")
            index[fingerprint(o)] = {"persona": str(d.get("persona") or plan.get("persona") or ""),
                                     "setup": setup, "strategy": setup[4:] if setup.startswith("lab:") else None}
    return index


def placed_index(broker_doc):
    """fingerprint -> the placed record (max loss, stop, target, the order with its plan note)."""
    out, by_order = {}, {}
    for r in (broker_doc or {}).get("orders") or []:
        if not isinstance(r, dict):
            continue
        if r.get("status") == "placed" and r.get("fingerprint"):
            out.setdefault(r["fingerprint"], r)
        if r.get("broker_order_id"):
            by_order[str(r["broker_order_id"])] = r
    return out, by_order


def capture_risk(paths, now=None):
    """Append trader/state/risk.json's verdicts (with each checked order's plan from trader/orders.json)
    to trader/journal/risk-log.jsonl, once per check. risk.json holds only the last check."""
    risk = read_json(paths.t("state", "risk.json"))
    if not isinstance(risk, dict) or not risk.get("results"):
        return 0
    key = str(risk.get("checked_at") or "") + "|" + "|".join(str(r.get("fingerprint")) for r in risk["results"])
    log_path = paths.j("risk-log.jsonl")
    if any(r.get("key") == key for r in read_jsonl(log_path)):
        return 0
    odoc = read_json(paths.t("orders.json")) or {}
    orders = odoc.get("orders", odoc) if isinstance(odoc, dict) else odoc
    by_fp = {}
    for o in orders if isinstance(orders, list) else []:
        if isinstance(o, dict):
            by_fp[fingerprint(o)] = o
    # now_ist is the check's own clock (--now when pinned); checked_at is the wall clock in UTC
    at = ist_time(risk.get("now_ist"), utc_z=False) or ist_time(risk.get("checked_at"))
    rows = []
    for r in risk["results"]:
        if not isinstance(r, dict):
            continue
        row = {k: r.get(k) for k in ("index", "symbol", "side", "qty", "verdict", "kind", "reasons", "warnings",
                                     "fingerprint", "basket_id")}
        o = by_fp.get(r.get("fingerprint"))
        if o is not None:
            row["order"] = o
        rows.append(row)
    rec = {"key": key, "checked_at": stamp(at) if at else None, "date": risk.get("date"),
           "paper": risk.get("paper"), "results": rows, "captured_at": stamp(now or parse_now())}
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return 1


def risk_records(paths):
    """Every captured check, plus the current risk.json when it was not captured yet."""
    recs = read_jsonl(paths.j("risk-log.jsonl"))
    return sorted(recs, key=lambda r: str(r.get("checked_at") or ""))


def verdicts_by_fp(recs):
    out = {}
    for rec in recs:
        for r in rec.get("results") or []:
            if r.get("fingerprint"):
                out.setdefault(r["fingerprint"], []).append(dict(r, checked_at=rec.get("checked_at")))
    return out


# ---------------------------------------------------------------- regime tags

def load_regimes(paths, bars_dir=None, offline=True):
    """{date: {"class", "vol", "expiry"}} for NIFTY: trader/regime/NIFTY.json (trader-regime history), else
    worked out here with trader-regime from NIFTY and India VIX daily bars (--bars DIR or the archive)."""
    doc = read_json(paths.t("regime", "NIFTY.json")) or {}
    days = doc.get("days") if isinstance(doc, dict) else None
    if isinstance(days, dict) and days:
        return {k: {"class": v.get("class") or v.get("trend"), "vol": v.get("vol"), "expiry": v.get("expiry")}
                for k, v in days.items() if isinstance(v, dict)}, "trader/regime/NIFTY.json"
    rg, lab = regime_api(), lab_api()
    if rg is None or lab is None:
        return {}, None
    try:
        nifty = load_series(lab, "^NSEI", "1d", paths, bars_dir, offline)
    except Exception:
        return {}, None
    if not nifty:
        return {}, None
    try:
        vix = load_series(lab, "^INDIAVIX", "1d", paths, bars_dir, offline)
    except Exception:
        vix = None
    regs = rg.daily_regimes(nifty, vix, "NIFTY", "index")
    return ({k: {"class": v["trend"]["class"], "vol": v["vol"]["bucket"], "expiry": rg.expiry_flag(v)}
             for k, v in regs.items()}, "trader-regime from NIFTY daily bars")


def regime_tag(regimes, day):
    """The regime as of the close BEFORE `day` (no look-ahead for an intraday entry): "trend|low"."""
    if not regimes:
        return "unknown"
    prev = [d for d in regimes if d < day]
    if not prev:
        return "unknown"
    r = regimes[max(prev)]
    return "%s|%s" % (r.get("class") or "unknown", r.get("vol") or "unknown")


# ---------------------------------------------------------------- bars (trader-lab's loader)

_BARS = {}


def load_series(lab, sym, tf, paths, bars_dir=None, offline=True):
    """[{"date", "time", "o", "h", "l", "c", "v"}] from --bars DIR, the archive, the lab cache or Yahoo."""
    k = (sym, tf, bars_dir, paths.workdir, offline)
    if k in _BARS:
        return _BARS[k]
    bars = None
    try:
        if bars_dir:
            for d in [x for x in str(bars_dir).split(",") if x]:
                if lab.find_file(d, sym, tf):
                    bars, _ = lab.load_bars(sym, tf, data_dir=d)
                    break
        else:
            arch = os.environ.get("BT_ARCHIVE") or paths.t("archive")
            bars, _ = lab.load_bars(sym, tf, cache_dir=paths.t("lab", "cache"), fetch=not offline, archive=arch)
    except Exception:
        bars = None
    _BARS[k] = bars
    return bars


def excursions(e, paths, bars_dir, offline):
    """MAE and MFE in rupees (and per share) for an equity entry, from 5-minute bars for an intraday trade or
    daily bars otherwise; None when no bars cover the trade."""
    lab = lab_api()
    if lab is None or e["kind"] != "equity":
        return None
    t0, t1 = e["opened_at"], e["closed_at"]
    intraday = t0[:10] == t1[:10]
    bars = load_series(lab, e["underlying"], "5m" if intraday else "1d", paths, bars_dir, offline)
    if not bars:
        return None
    if intraday:
        m0 = hhmm_min(t0[11:16]) // 5 * 5
        m1 = hhmm_min(t1[11:16])
        span = [b for b in bars if b["date"] == t0[:10] and b["time"] and m0 <= hhmm_min(b["time"]) < max(m1, m0 + 1)]
    else:
        span = [b for b in bars if t0[:10] <= b["date"] <= t1[:10]]
    if not span:
        return None
    lo, hi = min(b["l"] for b in span), max(b["h"] for b in span)
    q, px = e["qty"], e["entry_price"]
    if e["side"] == "long":
        adverse, favour = lo - px, hi - px
    else:
        adverse, favour = px - hi, px - lo
    return {"mae": r2(min(adverse, 0) * q), "mfe": r2(max(favour, 0) * q),
            "mae_per_share": r2(min(adverse, 0)), "mfe_per_share": r2(max(favour, 0)),
            "bars": len(span), "timeframe": "5m" if intraday else "1d"}


# ---------------------------------------------------------------- entries

def _cash(f):
    """The fill's cash flow before charges: a buy pays, a sell receives; a settlement pays or receives the
    intrinsic value (qty is the signed position it closes)."""
    v = f["value"] if f.get("value") is not None else f["qty"] * f["price"]
    if f["side"] == "BUY":
        return -abs(v)
    if f["side"] == "SELL":
        return abs(v)
    return abs(v) if f["qty"] > 0 else -abs(v)


def _delta(f):
    if f["side"] == "BUY":
        return f["qty"]
    if f["side"] == "SELL":
        return -f["qty"]
    return -f["qty"]


def _group_key(f):
    if f.get("strategy_id"):
        return ("strategy", f["source"], f["strategy_id"])
    if f["segment"] != "EQ":
        return ("fno", f["source"], f["underlying"])
    return ("equity", f["source"], f["symbol"])


def _split(f, qty):
    """Part of a fill (a sell that closes a long and opens a short)."""
    share = float(qty) / f["qty"]
    g = dict(f, qty=qty, value=r2((f["value"] if f.get("value") is not None else f["qty"] * f["price"]) * share),
             charges=r2((f["charges"] or 0) * share))
    return g


def build_entries(fills):
    """Fills -> (closed entries, open positions). An equity entry runs from flat to flat; an F&O entry is
    every fill of one paper strategy (rolls included) or, for imported fills, every contract of one
    underlying from flat to flat."""
    fills = sorted(fills, key=lambda f: (f["at"], 1 if f["side"] == "SETTLE" else 0, f["id"]))
    books = {}
    closed = []
    for f in fills:
        k = _group_key(f)
        book = books.get(k)
        if book is None:
            book = books[k] = {"pos": {}, "fills": []}
        pending = [f]
        while pending:
            g = pending.pop(0)
            d = _delta(g)
            held = book["pos"].get(g["symbol"], 0)
            if k[0] == "equity" and held and d and (held > 0) != (held + d > 0) and held + d != 0:
                first = _split(g, abs(held))
                pending.insert(0, _split(g, g["qty"] - abs(held)))
                g, d = first, -held
            book["pos"][g["symbol"]] = held + d
            book["fills"].append(g)
            if all(v == 0 for v in book["pos"].values()):
                closed.append((k, book["fills"]))
                books[k] = book = {"pos": {}, "fills": []}
    still = [(k, b["fills"], {s: q for s, q in b["pos"].items() if q}) for k, b in books.items() if b["fills"]]
    return closed, still


def entry_id(key, fills):
    first = fills[0]
    blob = "|".join([key[0], key[1], str(key[2]), first["at"], first["id"]])
    return "J%s-%s" % (first["at"][:10].replace("-", ""), hashlib.sha256(blob.encode("utf-8")).hexdigest()[:6])


def _avg(fills):
    q = sum(f["qty"] for f in fills)
    return r2(sum(f["qty"] * f["price"] for f in fills) / q) if q else None


def make_entry(key, fills, ctx):
    kind = "equity" if key[0] == "equity" else "fno"
    first, last = fills[0], fills[-1]
    gross = r2(sum(_cash(f) for f in fills))
    charges = r2(sum(f["charges"] or 0 for f in fills))
    net = r2(gross - charges)
    e = {"id": entry_id(key, fills), "kind": kind, "source": first["source"], "paper": bool(first.get("paper")),
         "underlying": first["underlying"], "symbols": sorted({f["symbol"] for f in fills}),
         "opened_at": first["at"], "closed_at": last["at"], "gross": gross, "charges": charges, "net": net,
         "charges_estimated": any(f.get("charges_estimated") for f in fills), "fills": fills,
         "segments": sorted({f["segment"] for f in fills}), "product": first.get("product") or ""}
    opening_side = first["side"]
    if kind == "equity":
        e["side"] = "long" if opening_side == "BUY" else "short"
        opens = [f for f in fills if f["side"] == opening_side]
        closes = [f for f in fills if f["side"] != opening_side]
        e["qty"] = sum(f["qty"] for f in opens)
        e["entry_price"], e["exit_price"] = _avg(opens), _avg(closes)
        e["lots"] = None
        e["symbol"] = first["symbol"]
    else:
        legs = {}
        for f in fills:
            legs.setdefault(f["symbol"], 0)
        e["side"] = None
        e["symbol"] = first["underlying"]
        e["legs"] = len(legs)
        units = max(abs(f["qty"]) for f in fills)
        e["qty"] = units
        lot = ctx.get("lot_sizes", {}).get(first["underlying"])
        e["lots"] = int(units // lot) if lot and units % lot == 0 else None
        premium = sum(_cash(f) for f in fills if f["at"] == first["at"])
        e["opened_for"] = "credit" if premium > 0 else "debit"
        e["entry_price"], e["exit_price"] = r2(abs(premium)), None
    t0, t1 = ist_time(e["opened_at"]), ist_time(e["closed_at"])
    e["holding_minutes"] = int((t1 - t0).total_seconds() // 60)
    # context: who drafted it, the yes record (max loss, stop), the strategy
    fps = [f.get("fingerprint") for f in fills if f.get("fingerprint")]
    tag = next((ctx["personas"][fp] for fp in fps if fp in ctx["personas"]), None)
    placed = None
    for f in fills:
        p = ctx["placed"].get(f.get("fingerprint")) if f.get("fingerprint") else None
        if p is None and f.get("broker_order_id"):
            p = ctx["by_order"].get(str(f["broker_order_id"]))
        if p is not None:
            placed = p
            break
    e["persona"] = (tag or {}).get("persona") or "unattributed"
    plan = ((placed or {}).get("order") or {}).get("plan") if isinstance((placed or {}).get("order"), dict) else None
    setup = (tag or {}).get("setup") or ((plan or {}).get("setup") if isinstance(plan, dict) else "") or ""
    strategy = (tag or {}).get("strategy")
    if kind == "fno":
        st = ctx["strategies"].get(first.get("strategy_id")) or {}
        strategy = strategy or st.get("name")
        e["strategy_id"] = first.get("strategy_id")
    e["setup"] = setup or None
    e["strategy"] = strategy or (setup[4:] if setup.startswith("lab:") else setup) or "untagged"
    risk = None
    if placed is not None:
        risk = _f(placed.get("basket_max_loss")) if kind == "fno" else _f(placed.get("max_loss"))
        if kind == "equity":
            e["stop"], e["target"] = _f(placed.get("stop")), _f(placed.get("target"))
    e["risk"] = r2(risk) if risk else None
    e["r_multiple"] = round(net / risk, 3) if risk else None
    open_fills = [f for f in fills if f["side"] == opening_side and f["at"] == first["at"]] if kind == "equity" \
        else [f for f in fills if f["at"] == first["at"]]
    e["ticketed"] = all(f.get("fingerprint") or (f.get("broker_order_id") and str(f["broker_order_id"])
                                                 in ctx["by_order"]) for f in open_fills)
    day = e["opened_at"][:10]
    e["regime"] = regime_tag(ctx["regimes"], day)
    r = ctx["regimes"].get(day) or {}
    e["expiry_flag"] = r.get("expiry")
    e["time_known"] = all(f.get("time_known", True) for f in fills)
    e["tod"] = tod_bucket(e["opened_at"][11:16]) if e["time_known"] else "unknown"
    e["weekday"] = WEEKDAYS[t0.weekday()]
    e["instrument"] = instrument_class(e)
    e["outcome"] = "win" if net > 0.004 else ("loss" if net < -0.004 else "scratch")
    return e


def tod_bucket(hhmm):
    m = hhmm_min(hhmm)
    if m is None:
        return "unknown"
    if m < hhmm_min("09:15"):
        return "before the open"
    name = TOD[0][1]
    for start, label in TOD:
        if m >= hhmm_min(start):
            name = label
    return name


# ---------------------------------------------------------------- behaviour: rule breaks

def config_of(paths):
    cfg = dict(DEFAULTS)
    c = read_json(paths.t("config.json")) or {}
    if isinstance(c, dict):
        cfg.update({k: v for k, v in c.items() if v is not None})
    return cfg


def behaviour(entries, cfg, verdicts):
    """Rule notes on each entry (in place) and the rule-break log, overtrading index and cost drag."""
    log = []
    cap = int(_f(cfg.get("max_trades_per_day")) or 0)
    capital = _f(cfg.get("capital")) or 0
    max_loss = _f(cfg.get("max_loss_per_trade"))
    max_value = _f(cfg.get("max_order_value"))
    streak_n = int(_f(cfg.get("max_consecutive_losses")) or 0)
    dd_pct = _f(cfg.get("max_daily_drawdown_pct"))
    win = _f(cfg.get("revenge_window_min")) or 0
    nb, na = hhmm_min(cfg.get("no_entries_before")), hhmm_min(cfg.get("no_entries_after"))
    flat = hhmm_min(cfg.get("flat_by"))
    order = sorted(entries, key=lambda e: e["opened_at"])
    per_day = {}

    def flag(e, kind, text):
        e.setdefault("rule_notes", []).append({"kind": kind, "text": text})
        log.append({"kind": kind, "id": e["id"], "at": e["opened_at"] if kind != "late-exit" else e["closed_at"],
                    "symbol": e["symbol"], "net": e["net"], "text": text})

    for e in order:
        e["rule_notes"] = []
    for e in order:
        day, t = e["opened_at"][:10], e["opened_at"]
        per_day[day] = per_day.get(day, 0) + 1
        if cap and per_day[day] > cap:
            flag(e, "overtrading", "entry %d of the day on %s, over your max_trades_per_day of %d"
                 % (per_day[day], day, cap))
        before = sorted([x for x in order if x["closed_at"][:10] == day and x["closed_at"] <= t and x is not e],
                        key=lambda x: x["closed_at"])
        streak = 0
        for x in reversed(before):
            if x["net"] < -0.004:
                streak += 1
            else:
                break
        realised = sum(x["net"] for x in before)
        if streak_n and streak >= streak_n:
            flag(e, "after-breaker", "opened at %s IST after %d losing trades in a row today: trader-risk's "
                 "breaker locks new entries until tomorrow (max_consecutive_losses %d)" % (t[11:16], streak, streak_n))
        elif dd_pct and capital and realised < 0 and -realised >= capital * dd_pct / 100.0 - 0.01:
            flag(e, "after-breaker", "opened at %s IST when the day was already down %s, past your daily drawdown "
                 "limit of %s%% of capital: the breaker locks new entries" % (t[11:16], inr(-realised), _num(dd_pct)))
        if win:
            t0 = ist_time(t)
            for x in before:
                if x["underlying"] == e["underlying"] and x["net"] < -0.004:
                    gap = (t0 - ist_time(x["closed_at"])).total_seconds() / 60.0
                    if 0 <= gap <= win:
                        flag(e, "revenge", "re-entered %s %d minutes after a %s loss on it (%s IST): a revenge trade "
                             "(revenge_window_min %s)" % (e["underlying"], int(gap), inr(-x["net"]),
                                                          x["closed_at"][11:16], _num(win)))
                        break
        if e.get("risk") and max_loss and e["risk"] > max_loss + 0.5:
            flag(e, "oversized", "risked %s at the stop with charges, over your max_loss_per_trade of %s"
                 % (inr(e["risk"]), inr(max_loss)))
        elif e["kind"] == "equity" and not e.get("risk") and max_value and e["entry_price"] and \
                e["qty"] * e["entry_price"] > max_value + 0.5:
            flag(e, "oversized", "position of %s, over your max_order_value of %s (and no stop on record)"
                 % (inr(e["qty"] * e["entry_price"]), inr(max_value)))
        m = hhmm_min(t[11:16]) if e.get("time_known", True) else None
        if e["product"] != "CNC" and m is not None and ((nb is not None and m < nb) or (na is not None and m > na)):
            flag(e, "outside-window", "opened at %s IST, outside your entry window %s to %s"
                 % (t[11:16], cfg.get("no_entries_before"), cfg.get("no_entries_after")))
        mc = hhmm_min(e["closed_at"][11:16]) if e.get("time_known", True) else None
        if e["product"] == "MIS" and flat is not None and mc is not None and mc > flat \
                and e["closed_at"][:10] == e["opened_at"][:10]:
            flag(e, "late-exit", "an intraday (MIS) position closed at %s IST, after your flat_by %s"
                 % (e["closed_at"][11:16], cfg.get("flat_by")))
        if not e["ticketed"]:
            flag(e, "off-ticket", "opened by a fill with no ticket: trader-risk never checked it (a manual or "
                 "broker-side order)")
        for f in e["fills"]:
            for v in verdicts.get(f.get("fingerprint")) or []:
                warn = [w for w in v.get("warnings") or [] if str(w).split(":")[0].strip() in BEHAVIOUR_RULES]
                if v.get("verdict") == "PASS" and warn and f["at"] == e["opened_at"]:
                    flag(e, "override", "placed although trader-risk warned: %s" % warn[0])
                    break
            else:
                continue
            break
    days = sorted(per_day)
    over = {d: n for d, n in per_day.items() if cap and n > cap}
    avg = (sum(per_day.values()) / float(len(days))) if days else 0.0
    gross = sum(e["gross"] for e in entries)
    win_gross = sum(e["gross"] for e in entries if e["gross"] > 0)
    charges = sum(e["charges"] for e in entries)
    counts = {}
    for r in log:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    breaks = [e for e in entries if e["rule_notes"]]
    return {
        "log": sorted(log, key=lambda r: r["at"]), "counts": counts,
        "rule_break_trades": len(breaks), "rule_break_net": r2(sum(e["net"] for e in breaks)),
        "clean_net": r2(sum(e["net"] for e in entries if not e["rule_notes"])),
        "overtrading": {"days": len(days), "entries": sum(per_day.values()), "avg_per_day": round(avg, 2),
                        "plan_per_day": cap or None, "index": round(avg / cap, 2) if cap and days else None,
                        "days_over_plan": over, "max_in_a_day": max(per_day.values()) if per_day else 0},
        "cost_drag": {"charges": r2(charges), "gross": r2(gross),
                      "pct_of_gross": round(100.0 * charges / gross, 1) if gross > 0 else None,
                      "pct_of_winning_gross": round(100.0 * charges / win_gross, 1) if win_gross > 0 else None,
                      "per_trade": r2(charges / len(entries)) if entries else None,
                      "words": cost_words(charges, gross)},
    }


def cost_words(charges, gross):
    if not charges:
        return "no charges recorded"
    if gross <= 0:
        return "the trades lost %s before charges; charges of %s made it worse" % (inr(-gross), inr(charges))
    share = charges / gross
    if share >= 1:
        return "charges of %s were more than the %s gross profit: the account lost money to costs" % (
            inr(charges), inr(gross))
    if share >= 1 / 3.0:
        return "charges took %.0f%% of the gross profit (over a third: too many or too small trades)" % (100 * share)
    return "charges took %.0f%% of the gross profit" % (100 * share)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- statistics

def wilson(k, n, z=Z95):
    """Wilson score interval for k successes in n: (lo, hi) as fractions."""
    if n <= 0:
        return None, None
    p = float(k) / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)) / den
    return round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)


def mean_ci(xs, z=Z95):
    n = len(xs)
    if n == 0:
        return None, None, None
    m = sum(xs) / float(n)
    if n < 2:
        return m, None, None
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    half = z * sd / math.sqrt(n)
    return m, m - half, m + half


def stats(entries, capital=None):
    es = sorted(entries, key=lambda e: e["closed_at"])
    nets = [e["net"] for e in es]
    n = len(nets)
    wins = [x for x in nets if x > 0.004]
    losses = [x for x in nets if x < -0.004]
    lo, hi = wilson(len(wins), n)
    m, mlo, mhi = mean_ci(nets)
    rs = [e["r_multiple"] for e in es if e.get("r_multiple") is not None]
    rm, rlo, rhi = mean_ci(rs)
    eq, run, peak, dd, dd_at = [], 0.0, 0.0, 0.0, None
    cur = longest_w = longest_l = 0
    kind = None
    for e in es:
        run += e["net"]
        peak = max(peak, run)
        if peak - run > dd:
            dd, dd_at = peak - run, e["closed_at"]
        eq.append({"at": e["closed_at"], "id": e["id"], "net": e["net"], "equity": r2(run)})
        k = e["outcome"]
        if k == "scratch":
            continue
        cur = cur + 1 if k == kind else 1
        kind = k
        if k == "win":
            longest_w = max(longest_w, cur)
        else:
            longest_l = max(longest_l, cur)
    gw, gl = sum(wins), -sum(losses)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "scratches": n - len(wins) - len(losses),
        "win_rate": round(float(len(wins)) / n, 4) if n else None, "win_rate_ci": [lo, hi] if n else None,
        "net": r2(sum(nets)), "gross": r2(sum(e["gross"] for e in es)), "charges": r2(sum(e["charges"] for e in es)),
        "expectancy": r2(m) if m is not None else None,
        "expectancy_ci": [r2(mlo), r2(mhi)] if mlo is not None else None,
        "expectancy_r": round(rm, 3) if rm is not None else None,
        "expectancy_r_ci": [round(rlo, 3), round(rhi, 3)] if rlo is not None else None, "r_trades": len(rs),
        "avg_win": r2(gw / len(wins)) if wins else None, "avg_loss": r2(-gl / len(losses)) if losses else None,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "best": max(nets) if nets else None, "worst": min(nets) if nets else None,
        "max_drawdown": r2(dd), "max_drawdown_at": dd_at,
        "max_drawdown_pct": round(100.0 * dd / capital, 2) if capital else None,
        "longest_win_streak": longest_w, "longest_loss_streak": longest_l,
        "current_streak": {"kind": kind, "length": cur} if kind else None,
        "small_sample": n < MIN_SAMPLE, "equity": eq,
        "verdict": sample_words(n),
    }


def sample_words(n):
    if n == 0:
        return "No closed trades yet: nothing to judge."
    if n < MIN_SAMPLE:
        return ("%d closed trade%s: far too few to tell skill from luck (want %d or more); read the ranges, not "
                "the point numbers." % (n, "" if n == 1 else "s", MIN_SAMPLE))
    return ("%d closed trades. A record of the past, not a forecast: the ranges show how much is still chance."
            % n)


def breakdown(entries, by):
    if by not in GROUPS:
        raise JournalError("--by wants one of %s" % ", ".join(GROUPS))
    groups = {}
    for e in entries:
        groups.setdefault(str(e.get(by) or "unknown"), []).append(e)
    rows = []
    for k, es in groups.items():
        s = stats(es)
        s.pop("equity")
        s["group"] = k
        rows.append(s)
    rows.sort(key=lambda r: (-r["trades"], r["group"]))
    return rows


# ---------------------------------------------------------------- the counterfactual ledger

def read_cotrader_log(path):
    """The co-trader's prediction log (schema bharat-trader/cotrader-log/1) -> plans to follow. The adapter
    keeps only what a replay needs, so a later schema with more fields still reads."""
    out = []
    for r in read_jsonl(path):
        if str(r.get("schema") or COTRADER_LOG_SCHEMA).split("/")[1:2] != ["cotrader-log"]:
            continue
        need = ("id", "date", "symbol", "entry", "stop", "target")
        if any(r.get(k) in (None, "") for k in need):
            continue
        out.append({"source": "co-trader", "id": str(r["id"]), "symbol": str(r["symbol"]).upper(),
                    "date": str(r["date"]), "time": r.get("entry_time") or "09:15", "side": str(r.get("side") or "BUY").upper(),
                    "entry": _f(r["entry"]), "stop": _f(r["stop"]), "target": _f(r["target"]),
                    "qty": int(_f(r.get("qty")) or 0), "timeframe": r.get("timeframe") or "1d",
                    "flat_by": r.get("flat_by"), "max_bars": r.get("max_bars"),
                    "status": str(r.get("status") or ""), "strategy": r.get("strategy"), "tier": r.get("tier"),
                    "p": r.get("p"), "p_lo": r.get("p_lo"), "p_hi": r.get("p_hi"), "regime": r.get("regime"),
                    "product": "MIS" if (r.get("timeframe") == "5m") else "CNC"})
    return out


def blocked_orders(recs):
    """Your own entries trader-risk BLOCKed, with the plan captured at the check (once per order)."""
    seen, out = set(), []
    for rec in recs:
        for r in rec.get("results") or []:
            if r.get("verdict") != "BLOCK" or r.get("kind") not in (None, "entry"):
                continue
            fp = r.get("fingerprint")
            if fp in seen:
                continue
            seen.add(fp)
            o = r.get("order") or {}
            plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
            entry = _f(o.get("price")) or _f(o.get("trigger_price")) or _f(plan.get("entry"))
            stop = _f((o.get("stop_loss") or {}).get("trigger_price")) or _f(plan.get("stop"))
            target = _f((o.get("target") or {}).get("price")) or _f(plan.get("target"))
            at = rec.get("checked_at") or ""
            product = str(o.get("product") or "MIS").upper()
            out.append({"source": "risk block", "id": fp, "symbol": str(r.get("symbol") or o.get("symbol") or "").upper(),
                        "date": at[:10] or rec.get("date"), "time": at[11:16] or "09:15",
                        "side": str(r.get("side") or o.get("side") or "BUY").upper(), "entry": entry, "stop": stop,
                        "target": target, "qty": int(_f(r.get("qty")) or _f(o.get("qty")) or 0),
                        "timeframe": "5m" if product == "MIS" else "1d", "flat_by": None, "max_bars": None,
                        "status": "blocked", "reasons": r.get("reasons") or [], "basket": bool(r.get("basket_id")),
                        "product": product, "exchange": str(o.get("exchange") or "NSE").upper()})
    return out


def replay(plan, bars, cfg, lab):
    """Follow one plan on the bars after it with trader-lab's fill rules (no look-ahead):
    entry at the first bar starting at or after the decision (5m: that day from its time; 1d: the first
    session on or after its date), at the open plus slippage; skipped if that open is through the stop or
    target. Then stop first, target next (a gap through either fills at the open, not on the entry bar);
    5m: out at the first bar at or after the flat-by at its open less slippage (or the day's last close);
    1d: after the maximum holding, out at the next bar's open. Net of trader-lab's charges."""
    costs = dict(lab.COST_DEFAULTS)
    long_ = plan["side"] != "SELL"
    sgn = 1 if long_ else -1
    stop, target = plan["stop"], plan["target"]
    if not plan["entry"] or not stop or plan["qty"] <= 0:
        return {"result": "no plan", "why": "no entry, stop or quantity on record"}
    intraday = plan["timeframe"] == "5m"
    if intraday:
        t0 = hhmm_min(plan["time"]) or 555
        flat = hhmm_min(plan.get("flat_by") or cfg.get("flat_by") or "15:10")
        idx = [i for i, b in enumerate(bars) if b["date"] == plan["date"] and b["time"] and hhmm_min(b["time"]) >= t0]
    else:
        idx = [i for i, b in enumerate(bars) if b["date"] >= plan["date"]]
    if not idx:
        return {"result": "no bars", "why": "no bars on or after %s %s" % (plan["date"], plan["time"] if intraday else "")}
    i0 = idx[0]
    b0 = bars[i0]
    if intraday and hhmm_min(b0["time"]) >= flat:
        return {"result": "skipped", "why": "the next bar is at or after the flat-by"}
    slip = lambda p: lab.slippage(p, costs)
    fill = lab.round_paise(b0["o"] + sgn * slip(b0["o"]))
    if (long_ and (fill <= stop or (target and fill >= target))) or \
            (not long_ and (fill >= stop or (target and fill <= target))):
        return {"result": "skipped", "why": "opened through the stop or target (%s)" % fill, "entry_bar": b0["date"]}
    hold = int(_f(plan.get("max_bars")) or DAILY_HOLD)
    exit_px, reason, held, pending, j = None, None, 0, None, i0
    for j in idx:
        b = bars[j]
        fresh = j == i0
        if pending:
            exit_px, reason = b["o"] - sgn * slip(b["o"]), pending
            break
        if intraday and b["date"] != plan["date"]:
            pb = bars[j - 1]
            exit_px, reason = pb["c"] - sgn * slip(pb["c"]), "flat (no bar at the flat-by)"
            break
        if intraday and hhmm_min(b["time"]) >= flat:
            exit_px, reason = b["o"] - sgn * slip(b["o"]), "flat-by"
            break
        o, h, l = b["o"], b["h"], b["l"]
        if not fresh and (o <= stop if long_ else o >= stop):
            exit_px, reason = o - sgn * slip(o), "stop (gap)"
            break
        if (l <= stop) if long_ else (h >= stop):
            exit_px, reason = stop - sgn * slip(stop), "stop"
            break
        if target and not fresh and (o >= target if long_ else o <= target):
            exit_px, reason = o, "target (gap)"
            break
        if target and ((h >= target) if long_ else (l <= target)):
            exit_px, reason = target, "target"
            break
        held += 1
        if not intraday and held >= hold:
            pending = "max holding (%d bars)" % hold
    if exit_px is None:
        if intraday:
            b = bars[idx[-1]]
            exit_px, reason = b["c"] - sgn * slip(b["c"]), "flat (no bar at the flat-by)"
        else:
            return {"result": "open", "why": "still open at the end of the bars (%s)" % bars[idx[-1]]["date"],
                    "entry": fill, "entry_bar": b0["date"]}
    exit_px = lab.round_paise(max(exit_px, 0.05))
    q = plan["qty"]
    gross = r2((exit_px - fill) * q * sgn)
    ccfg = dict(cfg, product=plan.get("product") or ("MIS" if intraday else "CNC"))
    buy, sell = (fill, exit_px) if long_ else (exit_px, fill)
    ch = r2(lab.round_trip_charges(buy, sell, q, ccfg)["total"])
    xb = bars[j]
    return {"result": "replayed", "entry": fill, "exit": exit_px, "reason": reason, "gross": gross, "charges": ch,
            "net": r2(gross - ch), "entry_bar": "%s %s" % (b0["date"], b0["time"]) if intraday else b0["date"],
            "exit_bar": "%s %s" % (xb["date"], xb["time"]) if intraday else xb["date"]}


def counterfactual(paths, entries, cfg, bars_dir=None, offline=True, since=None, until=None):
    """Every co-trader candidate not taken and every risk BLOCK of your own order, followed to its outcome."""
    lab = lab_api()
    plans = read_cotrader_log(paths.t("cotrader", "log.jsonl")) + blocked_orders(risk_records(paths))
    if since:
        plans = [p for p in plans if p["date"] >= since]
    if until:
        plans = [p for p in plans if p["date"] <= until]
    opened = {}
    for e in entries:
        opened.setdefault((e["underlying"], e["opened_at"][:10]), []).append(e)
    rows = []
    for p in plans:
        row = {k: p.get(k) for k in ("source", "id", "symbol", "date", "time", "side", "entry", "stop", "target",
                                     "qty", "timeframe", "status", "strategy", "tier", "p", "p_lo", "p_hi",
                                     "regime", "reasons")}
        taken = [e for e in opened.get((p["symbol"], p["date"]), []) if e["opened_at"][11:16] >= (p["time"] or "")
                 or p["source"] == "co-trader"]
        if taken:
            row.update(result="taken", why="you traded %s that day (%s)" % (p["symbol"], taken[0]["id"]),
                       taken=taken[0]["id"])
        elif p.get("basket"):
            row.update(result="not replayable", why="an F&O basket: option bars are not replayed here")
        elif lab is None:
            row.update(result="no bars", why="trader-lab is not installed next to trader-journal")
        else:
            bars = load_series(lab, p["symbol"], p["timeframe"], paths, bars_dir, offline)
            row.update(replay(p, bars, cfg, lab) if bars else {"result": "no bars", "why": "no %s bars for %s"
                                                               % (p["timeframe"], p["symbol"])})
        row["group"] = ("co-trader skipped" if p["source"] == "co-trader" and p["status"] == "ready" else
                        "co-trader blocked by risk" if p["source"] == "co-trader" else "your orders blocked by risk")
        rows.append(row)
    return ledger_summary(rows)


def ledger_summary(rows):
    groups = {}
    for name in ("co-trader skipped", "co-trader blocked by risk", "your orders blocked by risk", "all"):
        mine = rows if name == "all" else [r for r in rows if r["group"] == name]
        done = [r for r in mine if r.get("result") == "replayed"]
        losers = [r for r in done if r["net"] < -0.004]
        winners = [r for r in done if r["net"] > 0.004]
        counts = {}
        for r in mine:
            counts[r.get("result")] = counts.get(r.get("result"), 0) + 1
        saved, cost = r2(-sum(r["net"] for r in losers)), r2(sum(r["net"] for r in winners))
        lo, hi = wilson(len(winners), len(done))
        groups[name] = {"plans": len(mine), "counts": counts, "replayed": len(done), "would_have_lost": len(losers),
                        "would_have_won": len(winners), "saved": saved, "cost": cost, "net_effect": r2(saved - cost),
                        "win_rate_ci": [lo, hi] if done else None,
                        "words": ledger_words(saved, cost, len(losers), len(winners), len(done))}
    return {"rows": rows, "groups": groups, "caveats": CAVEATS}


def ledger_words(saved, cost, nl, nw, n):
    if not n:
        return "nothing to replay yet"
    return ("not taking them saved you %s (%d would have lost) and cost you %s (%d would have won): net %s in your "
            "favour" if saved >= cost else "not taking them saved you %s (%d would have lost) and cost you %s (%d "
            "would have won): net %s against you") % (inr(saved), nl, inr(cost), nw, inr(abs(saved - cost)))


CAVEATS = [
    "Hypothetical fills: trader-lab's rules (next bar's open plus slippage, stop first inside a bar), not real "
    "order books. A real limit order may not have filled at all.",
    "Daily bars cannot tell whether the stop or the target came first inside one day: the stop is assumed.",
    "Quantities are the plan's own (the co-trader's sizing or your order), charges trader-lab's model.",
    "A few replays say little: read the counts and the interval, not the rupee total.",
    "A skipped trade that would have won is not a mistake by itself: the rules exist for the trades you cannot "
    "tell apart in advance.",
]


# ---------------------------------------------------------------- notes

def load_notes(paths):
    doc = read_json(paths.j("notes.json")) or {}
    return doc.get("notes") if isinstance(doc.get("notes"), dict) else {}


def add_note(paths, eid, text, tags, now):
    if not re.match(r"^J\d{8}-[0-9a-f]{6}$", eid or ""):
        raise JournalError("not a journal entry ID: %r (journal.py list shows them)" % eid)
    text = str(text or "").strip()
    if len(text) > 2000:
        raise JournalError("a note is at most 2,000 characters")
    tags = [t.strip().lower() for t in tags or [] if t and t.strip()]
    for t in tags:
        if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,31}$", t):
            raise JournalError("a tag is a short word (letters, digits, - and _), not %r" % t)
    if not text and not tags:
        raise JournalError("say what to note: text, --tag, or both")
    doc = read_json(paths.j("notes.json")) or {}
    notes = doc.get("notes") if isinstance(doc.get("notes"), dict) else {}
    cur = notes.setdefault(eid, {"notes": [], "tags": []})
    if text:
        cur["notes"].append({"at": stamp(now), "text": text})
    for t in tags:
        if t not in cur["tags"]:
            cur["tags"].append(t)
    write_json(paths.j("notes.json"), {"note": "Your own notes and tags on journal entries (journal.py note).",
                                        "notes": notes})
    return cur


# ---------------------------------------------------------------- imports

def imported_fills(paths):
    out = []
    folder = paths.j("imports")
    if not os.path.isdir(folder):
        return out
    for f in sorted(os.listdir(folder)):
        if f.endswith(".json"):
            doc = read_json(os.path.join(folder, f)) or {}
            out.extend(x for x in doc.get("fills") or [] if isinstance(x, dict))
    return out


def do_import(paths, path, broker, day, cfg):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as e:
        raise JournalError("could not read %s: %s" % (path, e))
    fills, name, notes = parse_tradebook(text, broker, day)
    estimate_charges(fills, cfg)
    target = paths.j("imports", "%s.json" % name)
    doc = read_json(target) or {}
    have = {f["id"]: f for f in doc.get("fills") or [] if isinstance(f, dict)}
    new = sum(1 for f in fills if f["id"] not in have)
    for f in fills:
        have[f["id"]] = f
    write_json(target, {"schema": "bharat-trader/journal-import/1", "broker": name,
                        "fills": sorted(have.values(), key=lambda f: (f["at"], f["id"]))})
    return {"broker": name, "read": len(fills), "new": new, "file": target, "notes": notes,
            "estimated": sum(1 for f in fills if f.get("charges_estimated"))}


# ---------------------------------------------------------------- the journal

def gather(paths, bars_dir=None, offline=True, capture=True, now=None):
    """Read every source and build the journal (nothing written but the risk-log capture)."""
    if capture:
        capture_risk(paths, now)
    cfg = config_of(paths)
    fills = paper_fills(read_json(paths.t("state", "trades.json")) or {})
    imports = imported_fills(paths)
    # a live fill the paper log also holds (shadow mode) is not counted twice: paper and live are separate books
    fills += imports
    pf = read_json(paths.t("portfolio.json")) or {}
    strategies = pf.get("strategies") if isinstance(pf.get("strategies"), dict) else {}
    lot_sizes = {}
    for p in pf.get("fno_positions") or []:
        if isinstance(p, dict) and _f(p.get("lot_size")):
            lot_sizes[str(p.get("underlying") or "").upper()] = int(_f(p["lot_size"]))
    sdoc = read_json(paths.t("strategy.json")) or {}
    for leg in (sdoc.get("legs") or []) if isinstance(sdoc, dict) else []:
        if isinstance(leg, dict) and _f(leg.get("lot_size")):
            lot_sizes.setdefault(str(sdoc.get("underlying") or leg.get("underlying") or "").upper(),
                                 int(_f(leg["lot_size"])))
    for st in strategies.values():
        if isinstance(st, dict) and _f(st.get("lot_size")):
            lot_sizes.setdefault(str(st.get("underlying") or "").upper(), int(_f(st["lot_size"])))
    placed, by_order = placed_index(read_json(paths.t("state", "broker.json")) or {})
    for r in (read_json(paths.t("state", "shadow.json")) or {}).get("orders") or []:
        if isinstance(r, dict) and r.get("broker_order_id"):
            by_order.setdefault(str(r["broker_order_id"]), r)
    regimes, regime_src = load_regimes(paths, bars_dir, offline)
    ctx = {"personas": persona_index(read_json(paths.t("state", "personas.json")) or {}), "placed": placed,
           "by_order": by_order, "strategies": strategies, "regimes": regimes, "lot_sizes": lot_sizes}
    closed, still = build_entries(fills)
    entries = [make_entry(k, fs, ctx) for k, fs in closed]
    notes = load_notes(paths)
    for e in entries:
        n = notes.get(e["id"]) or {}
        e["notes"], e["tags"] = n.get("notes") or [], n.get("tags") or []
        x = excursions(e, paths, bars_dir, offline)
        e["mae"], e["mfe"] = (x["mae"], x["mfe"]) if x else (None, None)
        e["excursion"] = x
    recs = risk_records(paths)
    beh = behaviour(entries, cfg, verdicts_by_fp(recs))
    entries.sort(key=lambda e: e["closed_at"], reverse=True)
    open_rows = [{"group": "%s %s" % (k[0], k[2]), "source": k[1], "since": fs[0]["at"],
                  "positions": pos} for k, fs, pos in still]
    return {"schema": SCHEMA, "as_of": stamp(now or parse_now()), "config": {k: cfg.get(k) for k in DEFAULTS},
            "entries": entries, "open": open_rows, "behaviour": beh,
            "stats": stats(entries, _f(cfg.get("capital"))),
            "sources": {"paper_fills": sum(1 for f in fills if f["source"] == "paper"),
                        "imported_fills": len(imports), "risk_checks": len(recs),
                        "regime": regime_src or "none (run trader-regime history NIFTY, or pass --bars)"},
            "disclaimer": "A journal describes your own past trades. Patterns in a few trades can be chance. "
                          "Education and tooling, not financial advice."}


def lean(doc):
    """journal.json without each fill's full record (kept small for the UI and MCP)."""
    out = dict(doc)
    out["entries"] = []
    for e in doc["entries"]:
        x = dict(e)
        x["fills"] = [{k: f.get(k) for k in ("at", "symbol", "side", "qty", "price", "charges", "leg", "source",
                                             "charges_estimated")} for f in e["fills"]]
        out["entries"].append(x)
    return out


def sync(paths, bars_dir=None, offline=True, now=None):
    doc = lean(gather(paths, bars_dir, offline, True, now))
    write_json(paths.journal, doc)
    return doc


def week_bounds(text, now):
    if text in (None, "", "this", "current"):
        y, w, _ = now.date().isocalendar()
    elif text == "last":
        y, w, _ = (now.date() - datetime.timedelta(days=7)).isocalendar()
    else:
        m = re.match(r"^(\d{4})-?W(\d{1,2})$", str(text).strip(), re.I)
        if not m:
            raise JournalError("--week wants an ISO week like 2026-W44 (or this / last)")
        y, w = int(m.group(1)), int(m.group(2))
    jan4 = datetime.date(y, 1, 4)
    mon = jan4 - datetime.timedelta(days=jan4.weekday()) + datetime.timedelta(weeks=w - 1)
    return "%d-W%02d" % (y, w), mon, mon + datetime.timedelta(days=6)


def review(paths, week, bars_dir=None, offline=True, now=None):
    """The weekly review (markdown), the journal-coach persona's input: trader/journal/weekly-YYYY-Www.md."""
    now = now or parse_now()
    doc = gather(paths, bars_dir, offline, True, now)
    label, mon, sun = week_bounds(week, now)
    a, b = mon.isoformat(), sun.isoformat()
    mine = [e for e in doc["entries"] if a <= e["closed_at"][:10] <= b]
    cfg = config_of(paths)
    s = stats(mine, _f(cfg.get("capital")))
    beh = behaviour(mine, cfg, verdicts_by_fp(risk_records(paths))) if mine else doc["behaviour"]
    cf = counterfactual(paths, doc["entries"], cfg, bars_dir, offline, a, b)
    L = ["# Weekly review %s (%s to %s)" % (label, mon.strftime("%a %d %b %Y"), sun.strftime("%a %d %b %Y")), "",
         "From trader-journal at %s IST. Paper and imported live trades closed this week, after every charge. "
         "The journal-coach persona reads this file; it suggests one process change, never a stock." % doc["as_of"]
         .replace("T", " "), ""]
    L += ["## The numbers", ""]
    if not mine:
        L += ["No trades closed this week.", ""]
    else:
        lo, hi = s["win_rate_ci"]
        L += ["| | |", "| --- | --- |",
              "| closed trades | %d (%d won, %d lost, %d flat) |" % (s["trades"], s["wins"], s["losses"], s["scratches"]),
              "| win rate | %s (95%% range %s to %s, Wilson) |" % (_pct(s["win_rate"]), _pct(lo), _pct(hi)),
              "| net P&L after charges | %s |" % signed_inr(s["net"]),
              "| gross P&L / charges | %s / %s |" % (signed_inr(s["gross"]), inr(s["charges"])),
              "| expectancy a trade | %s%s |" % (signed_inr(s["expectancy"]), (" (95%% range %s to %s)" % (
                  signed_inr(s["expectancy_ci"][0]), signed_inr(s["expectancy_ci"][1]))) if s["expectancy_ci"] else ""),
              "| average R | %s |" % (("%+.2fR over %d trades with a recorded max loss" % (s["expectancy_r"],
                                                                                        s["r_trades"]))
                                       if s["expectancy_r"] is not None else "no max loss on record"),
              "| profit factor | %s |" % (s["profit_factor"] if s["profit_factor"] is not None else "n/a (no losses)"),
              "| max drawdown in the week | %s |" % inr(s["max_drawdown"]),
              "| longest losing streak | %d |" % s["longest_loss_streak"], "",
              "> %s" % s["verdict"], ""]
        for by, title in (("persona", "By persona"), ("strategy", "By strategy"), ("regime", "By market regime "
                                                                                           "(NIFTY, the close before)")):
            L += ["### %s" % title, "", "| %s | trades | win rate (95%% range) | net after charges | expectancy |" % by,
                  "| --- | --- | --- | --- | --- |"]
            for r in breakdown(mine, by):
                L.append("| %s | %d | %s (%s to %s) | %s | %s |" % (r["group"], r["trades"], _pct(r["win_rate"]),
                                                                  _pct(r["win_rate_ci"][0]), _pct(r["win_rate_ci"][1]),
                                                                  signed_inr(r["net"]), signed_inr(r["expectancy"])))
            L.append("")
    L += ["## Rule breaks", ""]
    if beh["log"]:
        for r in beh["log"]:
            L.append("- %s %s IST, %s (%s): %s; net %s" % (r["at"][:10], r["at"][11:16], r["symbol"], r["kind"],
                                                         r["text"], signed_inr(r["net"])))
        L += ["", "Trades with a rule break: %d, net %s. Trades with none: net %s." % (
            beh["rule_break_trades"], signed_inr(beh["rule_break_net"]), signed_inr(beh["clean_net"])), ""]
    else:
        L += ["None found in the records.", ""]
    o, c = beh["overtrading"], beh["cost_drag"]
    L += ["## Overtrading and costs", "",
          "- %d entries on %d days, %.2f a day against a plan of %s a day%s." % (
              o["entries"], o["days"], o["avg_per_day"], o["plan_per_day"] or "no limit",
              (" (overtrading index %.2f; 1.00 is the plan)" % o["index"]) if o["index"] is not None else ""),
          "- Days over plan: %s." % (", ".join("%s (%d)" % kv for kv in sorted(o["days_over_plan"].items())) or "none"),
          "- Charges %s: %s." % (inr(c["charges"]), c["words"]), ""]
    g = cf["groups"]
    L += ["## Counterfactual ledger (plans dated this week)", ""]
    for name in ("co-trader skipped", "co-trader blocked by risk", "your orders blocked by risk"):
        x = g[name]
        if not x["plans"]:
            continue
        L.append("- %s: %d plan%s, %d replayed; %s." % (name, x["plans"], "" if x["plans"] == 1 else "s",
                                                       x["replayed"], x["words"]))
    if not any(g[n]["plans"] for n in g if n != "all"):
        L.append("- No co-trader candidates or blocked orders this week.")
    L += ["", "Caveats: " + " ".join(CAVEATS[:3]), ""]
    L += ["## For the journal coach", "",
          "- %s" % ("Under %d closed trades: review process only, not results." % MIN_SAMPLE if s["trades"] < MIN_SAMPLE
                    else "Enough trades to look at results, still with the ranges above."),
          "- Most frequent rule break: %s." % (max(beh["counts"].items(), key=lambda kv: kv[1])[0]
                                               if beh["counts"] else "none"),
          "- Write ONE process change for next week as a rule the risk officer can enforce.", "",
          "## What this does not show", "",
          "- Why a trade was taken: add notes (journal.py note ID \"...\").",
          "- Whether a pattern is skill or luck: a week is a tiny sample.",
          "- The future: it is a record of the past, not a forecast.", "",
          "Education and tooling, not financial advice.", ""]
    text = "\n".join(L)
    path = paths.j("weekly-%s.md" % label)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"week": label, "from": a, "to": b, "path": path, "text": text, "stats": s, "behaviour": beh,
            "counterfactual": {k: v for k, v in cf.items() if k != "rows"}}


def _pct(x):
    return "n/a" if x is None else "%.0f%%" % (100.0 * x)


# ---------------------------------------------------------------- summaries for the UI and MCP

def summary(paths, bars_dir=None, offline=True, now=None, recent=10, write=False):
    """Read-only overview: headline stats (with intervals), this week, rule breaks, the ledger totals."""
    now = now or parse_now()
    doc = gather(paths, bars_dir, offline, capture=write, now=now)
    label, mon, sun = week_bounds(None, now)
    week = [e for e in doc["entries"] if mon.isoformat() <= e["closed_at"][:10] <= sun.isoformat()]
    s = dict(doc["stats"])
    ws = stats(week)
    ws.pop("equity")
    cf = counterfactual(paths, doc["entries"], config_of(paths), bars_dir, offline)
    return {"as_of": doc["as_of"], "stats": s, "this_week": dict(ws, week=label), "behaviour": doc["behaviour"],
            "by": {g: breakdown(doc["entries"], g) for g in GROUPS},
            "counterfactual": {"groups": cf["groups"], "caveats": cf["caveats"],
                               "rows": cf["rows"][-50:]},
            "entries": lean(doc)["entries"][:recent] if recent else lean(doc)["entries"], "open": doc["open"],
            "sources": doc["sources"], "disclaimer": doc["disclaimer"]}


# ---------------------------------------------------------------- command line

def _entry_line(e):
    r = (" %+.2fR" % e["r_multiple"]) if e.get("r_multiple") is not None else ""
    what = e["symbol"] if e["kind"] == "equity" else "%s %s (%d legs)" % (e["underlying"], e["strategy"], e["legs"])
    flags = (" [%s]" % ", ".join(sorted({n["kind"] for n in e["rule_notes"]}))) if e.get("rule_notes") else ""
    return "%s  %s %s  %-28s net %s%s%s" % (e["id"], e["opened_at"][:10], e["opened_at"][11:16], what,
                                            signed_inr(e["net"]), r, flags)


def print_stats(s, title="All closed trades"):
    print("%s: %d (%d won, %d lost, %d flat)." % (title, s["trades"], s["wins"], s["losses"], s["scratches"]))
    if not s["trades"]:
        return
    lo, hi = s["win_rate_ci"]
    print("  win rate %s (95%% range %s to %s, Wilson)" % (_pct(s["win_rate"]), _pct(lo), _pct(hi)))
    print("  net after charges %s (gross %s, charges %s)" % (signed_inr(s["net"]), signed_inr(s["gross"]),
                                                             inr(s["charges"])))
    ci = s["expectancy_ci"]
    print("  expectancy %s a trade%s" % (signed_inr(s["expectancy"]), (" (95%% range %s to %s)" % (
        signed_inr(ci[0]), signed_inr(ci[1]))) if ci else ""))
    if s["expectancy_r"] is not None:
        print("  average %+.2fR over %d trades with a recorded max loss" % (s["expectancy_r"], s["r_trades"]))
    print("  average win %s, average loss %s, profit factor %s" % (
        inr(s["avg_win"]) if s["avg_win"] is not None else "n/a", inr(s["avg_loss"]) if s["avg_loss"] is not None
        else "n/a", s["profit_factor"] if s["profit_factor"] is not None else "n/a"))
    print("  max drawdown %s; longest streaks: %d wins, %d losses" % (inr(s["max_drawdown"]), s["longest_win_streak"],
                                                                     s["longest_loss_streak"]))
    print("  %s" % s["verdict"])


def _common(p):
    p.add_argument("--workdir", default=".", help="the folder holding trader/ (default: here)")
    p.add_argument("--now", help="pin the clock, IST, e.g. \"2026-10-30 16:00\"")
    p.add_argument("--bars", help="offline bars: a folder of SYMBOL-5m.csv(.gz) / SYMBOL-1d.csv(.gz) (comma "
                                  "separated for several)")
    p.add_argument("--online", action="store_true", help="fetch missing bars from Yahoo (default: offline, "
                                                         "the archive and the lab cache only)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def main(argv=None):
    ap = argparse.ArgumentParser(description="The trade journal, its statistics, rule breaks and the "
                                             "counterfactual ledger (trader-journal). Read only on orders.")
    sub = ap.add_subparsers(dest="cmd")
    for name, about in (("sync", "rebuild trader/journal.json from every source"),
                        ("list", "closed trades, newest first"),
                        ("counterfactual", "follow every skipped or blocked plan to its outcome")):
        p = sub.add_parser(name, help=about)
        _common(p)
        if name == "list":
            p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("show", help="one entry: fills, charges, R, MAE/MFE, rule notes and your notes")
    p.add_argument("id")
    _common(p)
    p = sub.add_parser("stats", help="expectancy, win rate with its Wilson interval, profit factor, drawdown")
    p.add_argument("--by", choices=GROUPS, help="break down by strategy, persona, regime, tod, weekday or instrument")
    _common(p)
    p = sub.add_parser("review", help="write the weekly review to trader/journal/weekly-YYYY-Www.md")
    p.add_argument("--week", nargs="?", const="this", default="this", help="ISO week like 2026-W44, this or last")
    _common(p)
    p = sub.add_parser("import", help="import a broker trade book (CSV or JSON)")
    p.add_argument("file")
    p.add_argument("--broker", choices=("zerodha", "kite", "upstox", "dhan", "angel", "groww"),
                   help="whose layout (detected from the columns when left out)")
    p.add_argument("--date", help="the trade date for files whose rows carry only a time (Angel One)")
    _common(p)
    p = sub.add_parser("note", help="add your note and tags to an entry")
    p.add_argument("id")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--tag", action="append", default=[], help="a tag, e.g. fomo or followed-plan (repeatable)")
    _common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        now = parse_now(a.now)
        paths = Paths(a.workdir)
        off = not a.online
        if a.cmd == "sync":
            doc = sync(paths, a.bars, off, now)
            if a.json:
                print(json.dumps({k: doc[k] for k in ("as_of", "stats", "behaviour", "sources")}, indent=1,
                                 ensure_ascii=False))
                return 0
            print("Journal rebuilt: %d closed trades, %d open, from %d paper and %d imported fills; %d risk checks "
                  "on record. Wrote %s." % (len(doc["entries"]), len(doc["open"]), doc["sources"]["paper_fills"],
                                            doc["sources"]["imported_fills"], doc["sources"]["risk_checks"],
                                            os.path.relpath(paths.journal, paths.workdir)))
            print_stats(doc["stats"])
            if doc["behaviour"]["log"]:
                print("Rule breaks: %s." % ", ".join("%s %d" % kv for kv in sorted(doc["behaviour"]["counts"].items())))
            return 0
        if a.cmd == "import":
            res = do_import(paths, a.file, a.broker, a.date, config_of(paths))
            if a.json:
                print(json.dumps(res, indent=1, ensure_ascii=False))
                return 0
            print("Imported %d %s fills (%d new) into %s.%s" % (
                res["read"], res["broker"], res["new"], os.path.relpath(res["file"], paths.workdir),
                (" Charges estimated for %d (the file has none), marked as estimates." % res["estimated"])
                if res["estimated"] else ""))
            for n in res["notes"]:
                print("  note: %s" % n)
            print("Run journal.py sync to rebuild the journal.")
            return 0
        if a.cmd == "note":
            cur = add_note(paths, a.id, a.text, a.tag, now)
            print(json.dumps(cur, indent=1, ensure_ascii=False) if a.json else
                  "Noted on %s: %d note%s, tags %s." % (a.id, len(cur["notes"]), "" if len(cur["notes"]) == 1 else "s",
                                                        ", ".join(cur["tags"]) or "none"))
            return 0
        if a.cmd == "review":
            res = review(paths, a.week, a.bars, off, now)
            if a.json:
                print(json.dumps({k: v for k, v in res.items() if k != "text"}, indent=1, ensure_ascii=False))
            else:
                print(res["text"])
                print("Wrote %s." % os.path.relpath(res["path"], paths.workdir))
            return 0
        doc = gather(paths, a.bars, off, True, now)
        if a.cmd == "list":
            rows = doc["entries"][:max(1, a.limit)]
            if a.json:
                print(json.dumps(lean(dict(doc, entries=rows))["entries"], indent=1, ensure_ascii=False))
                return 0
            if not rows:
                print("No closed trades yet. Paper fills are in trader/state/trades.json; import a broker trade "
                      "book with journal.py import FILE --broker NAME.")
            for e in rows:
                print(_entry_line(e))
            if doc["open"]:
                print("Still open: %s." % ", ".join(o["group"] for o in doc["open"]))
            return 0
        if a.cmd == "show":
            e = next((x for x in doc["entries"] if x["id"] == a.id), None)
            if e is None:
                raise JournalError("no entry %s (journal.py list shows the IDs)" % a.id)
            if a.json:
                print(json.dumps(e, indent=1, ensure_ascii=False))
                return 0
            print(_entry_line(e))
            print("  %s, %s, persona %s, strategy %s, regime %s, %s" % (e["instrument"], e["source"], e["persona"],
                                                                        e["strategy"], e["regime"], e["tod"]))
            print("  opened %s, closed %s (%d minutes)" % (e["opened_at"].replace("T", " "),
                                                           e["closed_at"].replace("T", " "), e["holding_minutes"]))
            if e["kind"] == "equity":
                print("  %s %d at %s, out at %s" % (e["side"], e["qty"], e["entry_price"], e["exit_price"]))
            print("  gross %s, charges %s%s, net %s%s" % (signed_inr(e["gross"]), inr(e["charges"]),
                                                          " (estimated)" if e["charges_estimated"] else "",
                                                          signed_inr(e["net"]), (", %+.2fR on a max loss of %s" % (
                                                              e["r_multiple"], inr(e["risk"]))) if e["risk"] else ""))
            if e.get("mae") is not None:
                print("  MAE %s, MFE %s (%d %s bars)" % (inr(e["mae"]), inr(e["mfe"]), e["excursion"]["bars"],
                                                         e["excursion"]["timeframe"]))
            for f in e["fills"]:
                print("    %s %s %d %s at %s, charges %s (%s)" % (f["at"].replace("T", " "), f["side"], f["qty"],
                                                                  f["symbol"], f["price"], inr(f["charges"] or 0),
                                                                  f.get("leg") or f["source"]))
            for n in e["rule_notes"]:
                print("  rule: %s: %s" % (n["kind"], n["text"]))
            for n in e["notes"]:
                print("  note %s: %s" % (n["at"].replace("T", " "), n["text"]))
            if e["tags"]:
                print("  tags: %s" % ", ".join(e["tags"]))
            return 0
        if a.cmd == "stats":
            if a.by:
                rows = breakdown(doc["entries"], a.by)
                if a.json:
                    print(json.dumps(rows, indent=1, ensure_ascii=False))
                    return 0
                print("By %s (net after charges; win rate with its 95%% Wilson range):" % a.by)
                for r in rows:
                    print("  %-30s %3d trades  win %s (%s to %s)  net %s  expectancy %s" % (
                        r["group"], r["trades"], _pct(r["win_rate"]), _pct(r["win_rate_ci"][0]),
                        _pct(r["win_rate_ci"][1]), signed_inr(r["net"]), signed_inr(r["expectancy"])))
                print(sample_words(len(doc["entries"])))
                return 0
            if a.json:
                print(json.dumps({"stats": doc["stats"], "behaviour": doc["behaviour"]}, indent=1, ensure_ascii=False))
                return 0
            print_stats(doc["stats"])
            b = doc["behaviour"]
            o, c = b["overtrading"], b["cost_drag"]
            print("  overtrading index %s (%.2f entries a day against a plan of %s)" % (
                o["index"] if o["index"] is not None else "n/a", o["avg_per_day"], o["plan_per_day"] or "no limit"))
            print("  cost drag: %s" % c["words"])
            for r in b["log"]:
                print("  rule break %s %s %s: %s" % (r["at"].replace("T", " ")[:16], r["symbol"], r["kind"], r["text"]))
            return 0
        if a.cmd == "counterfactual":
            cf = counterfactual(paths, doc["entries"], config_of(paths), a.bars, off)
            if a.json:
                print(json.dumps(cf, indent=1, ensure_ascii=False))
                return 0
            for name in ("co-trader skipped", "co-trader blocked by risk", "your orders blocked by risk"):
                g = cf["groups"][name]
                print("%s: %d plans (%s); %s." % (name, g["plans"], ", ".join(
                    "%s %d" % kv for kv in sorted(g["counts"].items(), key=lambda kv: str(kv[0]))) or "none", g["words"]))
            for r in cf["rows"]:
                if r.get("result") == "replayed":
                    print("  %s %s %s: in %s, out %s (%s): net %s" % (r["date"], r["symbol"], r["group"], r["entry"],
                                                                      r["exit"], r["reason"], signed_inr(r["net"])))
            print("Caveats:")
            for c in cf["caveats"]:
                print("  - %s" % c)
            return 0
    except JournalError as e:
        print("journal.py: %s" % e, file=sys.stderr)
        return 1
    return 0


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
