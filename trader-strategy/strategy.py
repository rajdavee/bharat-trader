#!/usr/bin/env python3
"""Option strategy builder for trader-strategy (NSE F&O). Standard library only.

Builds a multi-leg option position from a real option chain and works out what
it can make and lose: the payoff at expiry and at any date before it (Black-76,
each leg at its own IV), breakevens, max profit and max loss (and whether
either is unlimited), the net premium, net Greeks, the same curve AFTER
charges (2026 F&O STT, brokerage, exchange, SEBI, stamp, GST, STT on exercise
for in-the-money longs at expiry), a margin figure with its method and error
bound, and the probability of profit two ways: the textbook lognormal at IV
next to a simulation that bootstraps the underlying's own returns.

    python3 strategy.py list
    python3 strategy.py build iron-condor NIFTY --expiry 03-Nov-2026 --wings 200 --width 100 --lots 1
    python3 strategy.py build short-strangle NIFTY --delta 0.16
    python3 strategy.py build calendar NIFTY --expiry 03-Nov-2026 --far-expiry 01-Dec-2026
    python3 strategy.py price NIFTY --legs "-1 CE ATM+200, +1 CE ATM+300, -1 PE ATM-200, +1 PE ATM-300"
    python3 strategy.py build bull-call-spread RELIANCE --json
    python3 strategy.py build iron-fly NIFTY --write               # -> trader/strategy.json
    python3 strategy.py build iron-fly NIFTY --orders              # -> trader/orders.json (a proposal)
    python3 strategy.py build iron-condor NIFTY --orders --persona options-seller   # tagged for its scorecard

Where the chain comes from, in order: --options FILE (trader-options' JSON,
schema bharat-trader/options/1; repeat it for a second expiry), then
trader/options.json if it is the same underlying and expiry, then
trader-options' own script when that skill sits next to this one (it fetches
NSE's chain, or reads saved files with --offline DIR). Daily and five-minute
history for the simulation comes from Yahoo Finance's chart API (free,
unofficial), or from DIR/history-<yahoo symbol>-1d.json with --offline.

Every public function below is pure (no network, no files) and returns plain
dicts, lists and numbers, so another program (an MCP server, a strategy lab,
the local UI) can import this file and call them: list_templates,
template_specs, parse_legs, resolve_legs, evaluate, payoff_curve, breakevens,
extremes, margin_estimate, textbook_probability, simulated_probability,
draft_orders, black76_price, black76_greeks, round_trip_charges.

Nothing here places an order. --orders writes a PROPOSAL to trader/orders.json;
trader-risk checks it and trader-broker asks for a typed yes, paper by default.
"""

import argparse
import bisect
import datetime as _dt
import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

datetime = _dt          # the shared charges block below says datetime.date

SCHEMA = "bharat-trader/strategy/1"
OPTIONS_SCHEMA = "bharat-trader/options/1"
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
PAISE = Decimal("0.01")
INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
YAHOO_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
               "MIDCPNIFTY": "NIFTY_MID_SELECT.NS", "NIFTYNXT50": "^NSMIDCP"}
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
YAHOO_UA = "Mozilla/5.0 (compatible; bharat-trader/0.2; +https://github.com/rajdavee/bharat-trader)"
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
EXPIRY_TIME = _dt.time(15, 30)          # options settle at 15:30 IST on expiry day
MIN_YEARS = 1.0 / (365 * 24)            # under an hour: Greeks are not meaningful
MINUTES_A_YEAR = 525600.0
OPTION_TICK = Decimal("0.05")
DEFAULT_RATE = 0.0557
DEFAULT_SEED = 7
DEFAULT_PATHS = 10000
IV_SCENARIOS = (-5.0, 0.0, 5.0)         # vol points, for the simulation's IV-shift scenarios
TAIL = 0.05                             # the tail in "5% tail loss"

# SPAN-style margin estimate. NIFTY's price scan was 2,143.94 on spot 23,053.10 (9.30%) and its vol
# scan 4 vol points in NSE's SPAN file of 30-Oct-2026 (nsccl.20261030.s.zip). The stock row is not
# from a file: F&O stocks' scans run about 12% to 25%, so 20% is a middle and the error bound is wider.
# Exposure (ELM): 2% of notional on short index options and index futures (practice, not a circular),
# 3.5% for stocks (NSE ael file, "OTH" rows), plus 2% on short index options on their expiry day
# (SEBI, 20-Nov-2024). Verify against your broker's basket margin before trading.
SPAN_DEFAULTS = {
    "index": {"scan_pct": 9.3, "vol_scan": 4.0, "elm_pct": 2.0, "error_pct": 15,
              "source": "NSE SPAN file 30-Oct-2026: NIFTY priceScan 2143.94 / spot 23053.10, volScan 0.04"},
    "stock": {"scan_pct": 20.0, "vol_scan": 10.0, "elm_pct": 3.5, "error_pct": 35,
              "source": "typical F&O stock scan (12% to 25%), not read from a SPAN file; ELM from NSE's ael file"},
}
EXPIRY_DAY_ELM_PCT = 2.0
EXTREME_MOVE, EXTREME_WEIGHT = 3.0, 0.30

NSE_HOLIDAYS = {  # Capital Market segment (F&O follows it), NSE holiday master; Diwali Laxmi Pujan is a Sunday
    "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
    "2026-11-24", "2026-12-25",
}
CALENDAR_YEARS = (2026,)


class StrategyError(Exception):
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


def fnum(x, places=2):
    """23020.7 -> '23,020.70'; None -> '-'."""
    if x is None:
        return "-"
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, _, frac = format(abs(d), "f").partition(".")
    return sign + _group(whole) + ("." + frac if frac else "")


def money(x):
    """inr() with a word for the unbounded."""
    if x is None:
        return "-"
    if x == float("inf"):
        return "unlimited"
    if x == float("-inf"):
        return "unlimited"
    return inr(x)


def _r(x, places=2):
    if x is None or (isinstance(x, float) and (math.isinf(x) or math.isnan(x))):
        return None
    return round(x, places)


def strike_text(k):
    return fnum(k, 0) if float(k).is_integer() else fnum(k, 2)


# ---------------------------------------------------------------- dates

def now_ist():
    return _dt.datetime.now(IST).replace(tzinfo=None)


def parse_date(s):
    """'03-Nov-2026', '03-NOV-2026', '03-11-2026', '2026-11-03' -> date (or None)."""
    if not s:
        return None
    if isinstance(s, _dt.datetime):
        return s.date()
    if isinstance(s, _dt.date):
        return s
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(s):
    """'2026-10-30T15:40:00', '2026-10-30 15:40', '30-Oct-2026 15:40:00' -> naive IST datetime (or None)."""
    if not s:
        return None
    if isinstance(s, _dt.datetime):
        return s.replace(tzinfo=None) if s.tzinfo is None else s.astimezone(IST).replace(tzinfo=None)
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt)
        except ValueError:
            continue
    d = parse_date(s)
    return _dt.datetime.combine(d, EXPIRY_TIME) if d else None


def nse_date(d):
    return d.strftime("%d-%b-%Y")


def expiry_moment(expiry):
    return _dt.datetime.combine(parse_date(expiry), EXPIRY_TIME)


def years_between(a, b):
    """Calendar minutes from a to b / 525,600 (never negative)."""
    return max(0.0, (b - a).total_seconds() / 60.0 / MINUTES_A_YEAR)


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS


def sessions_between(start, end):
    """Whole NSE sessions after start's date up to and including end's date (weekdays minus holidays)."""
    n, d = 0, start.date() + _dt.timedelta(days=1)
    while d <= end.date():
        if is_trading_day(d):
            n += 1
        d += _dt.timedelta(days=1)
    return n


def minutes_left_today(t):
    """Minutes of trading left in t's session (09:15 to 15:30 IST), 0 on a closed day."""
    if not is_trading_day(t.date()):
        return 0
    open_, close = _dt.datetime.combine(t.date(), _dt.time(9, 15)), _dt.datetime.combine(t.date(), EXPIRY_TIME)
    start = max(t, open_)
    return max(0, int((close - start).total_seconds() // 60))


# ---------------------------------------------------------------- Black-76 (same maths as trader-options)

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(F, K, T, sigma):
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    return d1, d1 - vt


def black76_price(F, K, T, r, sigma, kind):
    """Black-76 price of a European option on forward F ("CE" or "PE"); T years, r and sigma decimals.
    At T or sigma of zero (or F of zero) it returns the discounted intrinsic."""
    kind = kind.upper()
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0 or F <= 0:
        return df * max(0.0, (F - K) if kind == "CE" else (K - F))
    d1, d2 = _d1d2(F, K, T, sigma)
    if kind == "CE":
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def black76_greeks(F, K, T, r, sigma, kind):
    """Per unit: delta dPrice/dF, gamma per point, theta per CALENDAR day, vega per 1 vol point.
    None values under an hour to expiry."""
    kind = kind.upper()
    if sigma is None or T < MIN_YEARS or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    df = math.exp(-r * T)
    d1, _ = _d1d2(F, K, T, sigma)
    pdf = _npdf(d1)
    sqt = math.sqrt(T)
    price = black76_price(F, K, T, r, sigma, kind)
    delta = df * _ncdf(d1) if kind == "CE" else -df * _ncdf(-d1)
    gamma = df * pdf / (F * sigma * sqt)
    theta_year = -df * F * pdf * sigma / (2.0 * sqt) + r * price
    vega = df * F * pdf * sqt / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta_year / 365.0, "vega": vega}


# ---------------------------------------------------------------- charges: equity (same rules as budget, risk, broker)

CHARGES_DEFAULTS = {
    "brokerage_pct": 0.03,
    "brokerage_cap": 20,
    "stt_sell_pct": 0.025,
    "stt_delivery_pct": 0.1,
    "exchange_txn_pct": 0.00307,
    "sebi_per_crore": 10,
    "stamp_buy_pct": 0.003,
    "stamp_delivery_pct": 0.015,
    "gst_pct": 18,
}


def round_trip_charges(entry, exit, qty, cfg=None, segment=None, on=None, exercised=False):
    """Estimated charges for buying qty at entry and selling qty at exit (one round trip), in rupees.

    segment "OPT" or "FUT": the dated F&O rates below (prices are premiums for options; for an
    exercised option, exit is the intrinsic value and STT is stt_exercise_pct of it). "EQ" (the
    default): brokerage min(turnover x brokerage_pct, brokerage_cap) per order, STT on the sell side
    (MIS) or both sides (CNC), exchange, SEBI, stamp on the buy side, GST on brokerage + exchange + SEBI.
    Each part is rounded half up to the paisa; total is the sum of the parts."""
    seg = str(segment or (cfg.get("segment") if isinstance(cfg, dict) else None) or "EQ").upper()
    if seg != "EQ":
        return fno_round_trip_charges(entry, exit, qty, cfg, seg, on, exercised)
    c = cfg or {}
    ch = dict(CHARGES_DEFAULTS)
    if "brokerage_pct" in c:
        ch.update(c)
    else:
        ch.update(c.get("charges") or {})
    product = str((c.get("product") if "brokerage_pct" not in c else None) or "MIS").upper()

    def dec(x):
        return Decimal(repr(float(x or 0)))

    pct = Decimal("0.01")
    q = dec(qty)
    buy, sell = dec(entry) * q, dec(exit) * q
    turnover = buy + sell
    bp = dec(ch.get("brokerage_pct")) * pct
    cap = ch.get("brokerage_cap")

    def brok(value):
        b = value * bp
        return min(b, dec(cap)) if cap is not None else b

    brokerage = brok(buy) + brok(sell)
    if product == "CNC":
        stt = turnover * dec(ch.get("stt_delivery_pct")) * pct
        stamp = buy * dec(ch.get("stamp_delivery_pct")) * pct
    else:
        stt = sell * dec(ch.get("stt_sell_pct")) * pct
        stamp = buy * dec(ch.get("stamp_buy_pct")) * pct
    exchange = turnover * dec(ch.get("exchange_txn_pct")) * pct
    sebi = turnover * dec(ch.get("sebi_per_crore")) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * dec(ch.get("gst_pct")) * pct

    def paisa(x):
        return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    out = {"brokerage": paisa(brokerage), "stt": paisa(stt), "exchange": paisa(exchange), "sebi": paisa(sebi),
           "stamp": paisa(stamp), "gst": paisa(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}


# ---------------------------------------------------------------- F&O charges, dated (same in budget, risk, broker, exit, timemachine, strategy)
# segment "OPT" (rates on the premium) or "FUT" (on the traded value). Each row holds
# from its effective_from date until the next. Exchange = NSE transaction charge plus
# the IPFT contribution (one number on a contract note). Sources: docs/data-sources.md.
# "fno_charges": {"OPT": {...}, "FUT": {...}} in the config overrides any rate.
FNO_CHARGES = [
    {"effective_from": "2024-10-01",
     "OPT": {"brokerage_flat": 20, "stt_sell_pct": 0.1, "stt_exercise_pct": 0.125, "exchange_txn_pct": 0.03553,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
     "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.02, "exchange_txn_pct": 0.00183,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.002, "gst_pct": 18},
     "source": "NSE/FA/64323 (27 Sep 2024) and NSE/FA/73061 (27 Feb 2026, total outflow); STT up to 31 Mar 2026 "
               "per NSE/FATAX/73524"},
    {"effective_from": "2026-04-01",
     "OPT": {"brokerage_flat": 20, "stt_sell_pct": 0.15, "stt_exercise_pct": 0.15, "exchange_txn_pct": 0.03553,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
     "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.05, "exchange_txn_pct": 0.00183,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.002, "gst_pct": 18},
     "source": "Finance Act 2026, NSE/FATAX/73524 (31 Mar 2026); NSE/FA/73061"},
]


def fno_rates(segment, on=None, cfg=None):
    """The dated F&O rates for "OPT" or "FUT" on date `on` (default today), with the config's overrides."""
    seg = str(segment).upper()
    if seg not in ("OPT", "FUT"):
        raise ValueError("segment is EQ, OPT or FUT, not %r" % segment)
    day = on if isinstance(on, str) else (on or datetime.date.today()).isoformat()
    row = FNO_CHARGES[0]
    for r in FNO_CHARGES:
        if r["effective_from"] <= day:
            row = r
    rates = dict(row[seg])
    over = ((cfg or {}).get("fno_charges") or {}).get(seg) if isinstance(cfg, dict) else None
    if isinstance(over, dict):
        rates.update({k: v for k, v in over.items() if v is not None})
    return rates


def fno_round_trip_charges(entry, exit, qty, cfg=None, segment="OPT", on=None, exercised=False):
    """Buy qty at entry, sell at exit, for options (prices are premiums) or futures.

    brokerage  OPT: brokerage_flat per executed order. FUT: lower of brokerage_pct and brokerage_cap per order
    stt        stt_sell_pct on the sell side. OPT with exercised=True: no sell order; exit is the
               intrinsic value per unit and STT is stt_exercise_pct of it (paid by the buyer)
    exchange   exchange_txn_pct on buy + sell turnover (the exercise leg has none)
    sebi       sebi_per_crore rupees per crore of that turnover
    stamp      stamp_buy_pct on the buy side
    gst        gst_pct on brokerage + exchange + sebi
    Each part is rounded half up to the paisa; total is the sum of the parts."""
    seg = str(segment).upper()
    ch = fno_rates(seg, on, cfg)

    def dec(x):
        return Decimal(repr(float(x or 0)))

    pct = Decimal("0.01")
    q = dec(qty)
    buy, sell = dec(entry) * q, dec(exit) * q
    exercise = bool(exercised) and seg == "OPT"
    traded = buy + (Decimal(0) if exercise else sell)

    def brok(value):
        if seg == "OPT":
            return dec(ch.get("brokerage_flat")) if value > 0 else Decimal(0)
        b = value * dec(ch.get("brokerage_pct")) * pct
        cap = ch.get("brokerage_cap")
        return min(b, dec(cap)) if cap is not None else b

    brokerage = brok(buy) + (Decimal(0) if exercise else brok(sell))
    stt = sell * dec(ch.get("stt_exercise_pct" if exercise else "stt_sell_pct")) * pct
    exchange = traded * dec(ch.get("exchange_txn_pct")) * pct
    sebi = traded * dec(ch.get("sebi_per_crore")) / Decimal(10000000)
    stamp = buy * dec(ch.get("stamp_buy_pct")) * pct
    gst = (brokerage + exchange + sebi) * dec(ch.get("gst_pct")) * pct

    def paisa(x):
        return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    out = {"brokerage": paisa(brokerage), "stt": paisa(stt), "exchange": paisa(exchange), "sebi": paisa(sebi),
           "stamp": paisa(stamp), "gst": paisa(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}

# ---------------------------------------------------------------- end of F&O charges


# ---------------------------------------------------------------- the chain (trader-options' JSON)

def check_options_doc(doc, where="options JSON"):
    """A trader-options analysis (schema bharat-trader/options/1), or StrategyError."""
    if not isinstance(doc, dict) or doc.get("schema") != OPTIONS_SCHEMA:
        raise StrategyError("%s is not trader-options output (schema %s)" % (where, OPTIONS_SCHEMA))
    for k in ("underlying", "expiry", "forward", "rows", "t_years"):
        if doc.get(k) in (None, []):
            raise StrategyError("%s has no %s" % (where, k))
    return doc


def _row(a, strike):
    for x in a["rows"]:
        if abs(x["strike"] - strike) < 1e-9:
            return x
    return None


def strike_step(a):
    """The usual gap between listed strikes near the money (50 for NIFTY weeklies, 10 for RELIANCE)."""
    ks = [x["strike"] for x in a["rows"]]
    if len(ks) < 2:
        return 1.0
    atm = a.get("atm") or a["forward"]
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - atm))
    near = ks[max(0, i - 6): i + 7]
    gaps = sorted(round(b - a_, 6) for a_, b in zip(near, near[1:]) if b > a_)
    if not gaps:
        return 1.0
    counts = {}
    for g in gaps:
        counts[g] = counts.get(g, 0) + 1
    return min(counts, key=lambda g: (-counts[g], g))


def quoted(side):
    return bool(side) and side.get("price") is not None


def nearest_strike(a, target, kind=None):
    """The listed strike nearest `target` (one with a usable price on `kind`'s side if any is near)."""
    rows = a["rows"]
    if kind:
        s = kind.lower()
        good = [x for x in rows if quoted(x.get(s))]
        if good:
            rows = good
    return min(rows, key=lambda x: (abs(x["strike"] - target), x["strike"]))["strike"]


def smile_iv(a, kind, strike):
    """IV (%) for a strike with no IV of its own: linear between the nearest quoted strikes on the
    out-of-the-money side, else the ATM IV."""
    pts = []
    F = a["forward"]
    for x in a["rows"]:
        s = "pe" if x["strike"] < F else "ce"
        side = x.get(s)
        if side and side.get("iv") is not None:
            pts.append((x["strike"], side["iv"]))
    if not pts:
        return a.get("atm_iv"), "atm"
    ks = [p[0] for p in pts]
    i = bisect.bisect_left(ks, strike)
    if i == 0:
        return pts[0][1], "smile-edge"
    if i >= len(pts):
        return pts[-1][1], "smile-edge"
    (k0, v0), (k1, v1) = pts[i - 1], pts[i]
    w = (strike - k0) / (k1 - k0) if k1 > k0 else 0.0
    return v0 + w * (v1 - v0), "smile"


# ---------------------------------------------------------------- legs: specs and templates

def _spec(sign, kind, strike, lots=1, expiry="near", price=None):
    """A leg before strikes are chosen. strike: {"by": "strike"|"offset"|"delta"|"price"|"atm", "value": x}."""
    return {"side": "BUY" if sign > 0 else "SELL", "lots": lots, "kind": kind, "strike": strike,
            "expiry": expiry, "price": price}


def _off(x):
    return {"by": "offset", "value": float(x)}


TEMPLATES = [
    ("bull-call-spread", "debit", "buy a call, sell a higher call: profit capped above the short strike"),
    ("bear-call-spread", "credit", "sell a call, buy a higher call: keep the credit below the short strike"),
    ("bull-put-spread", "credit", "sell a put, buy a lower put: keep the credit above the short strike"),
    ("bear-put-spread", "debit", "buy a put, sell a lower put: profit capped below the short strike"),
    ("long-straddle", "debit", "buy the ATM call and put: needs a big move either way"),
    ("short-straddle", "credit", "sell the ATM call and put: unlimited risk, profits if it stays put"),
    ("long-strangle", "debit", "buy an OTM call and an OTM put: cheaper than a straddle, needs a bigger move"),
    ("short-strangle", "credit", "sell an OTM call and an OTM put: unlimited risk, wider profit zone"),
    ("iron-condor", "credit", "short strangle with bought wings: defined risk, profits in a range"),
    ("iron-fly", "credit", "short straddle with bought wings: defined risk, profits near the strike"),
    ("call-butterfly", "debit", "buy 1 lower call, sell 2 middle calls, buy 1 upper call (--skip for broken wing)"),
    ("put-butterfly", "debit", "buy 1 upper put, sell 2 middle puts, buy 1 lower put (--skip for broken wing)"),
    ("call-ratio-spread", "mixed", "buy 1 call, sell N higher calls (--ratio): unlimited risk above"),
    ("put-ratio-spread", "mixed", "buy 1 put, sell N lower puts (--ratio): large risk below"),
    ("covered-call", "mixed", "long future (or the shares) plus a short OTM call"),
    ("protective-put", "debit", "long future (or the shares) plus a long OTM put"),
    ("calendar", "debit", "sell the near expiry, buy the far expiry, same strike (--type CE or PE)"),
]
TEMPLATE_NAMES = [t[0] for t in TEMPLATES]
ALIASES = {"iron-butterfly": "iron-fly", "ironfly": "iron-fly", "condor": "iron-condor",
           "broken-wing-call-butterfly": "call-butterfly", "broken-wing-put-butterfly": "put-butterfly",
           "call-calendar": "calendar", "put-calendar": "calendar", "straddle": "long-straddle",
           "strangle": "long-strangle", "covered-call-future": "covered-call"}


def list_templates():
    """[{"name", "kind" (debit|credit|mixed), "about"}] for every built-in template."""
    return [{"name": n, "kind": k, "about": a} for n, k, a in TEMPLATES]


def template_name(name):
    n = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    n = ALIASES.get(n, n)
    if n not in TEMPLATE_NAMES:
        raise StrategyError("no template %r. Templates: %s" % (name, ", ".join(TEMPLATE_NAMES)))
    return n


def template_specs(name, params, a):
    """The legs of a template, before strikes are picked. a: the near expiry's analysis.

    params (all optional): strike (the centre or the long strike; default ATM), wings (points from
    ATM to the short strikes of strangles and condors, the OTM option of covered calls and protective
    puts; default the 1-SD expected move), width (points between a short strike and its wing, or
    between spread strikes; default 2 strikes), delta (pick the short strikes by delta instead of
    wings, e.g. 0.16), skip (extra points on one wing: broken-wing butterfly), ratio (short options
    per long in a ratio spread, default 2), type (CE or PE, for calendar), lots (default 1).
    """
    name = template_name(name)
    p = dict(params or {})
    lots = int(p.get("lots") or 1)
    if lots < 1:
        raise StrategyError("lots is a whole number of 1 or more")
    step = strike_step(a)
    em = (a.get("expected_move") or {}).get("move") or a["forward"] * 0.02
    atm = a.get("atm") or nearest_strike(a, a["forward"])
    centre = float(p["strike"]) if p.get("strike") not in (None, "") else atm
    width = float(p.get("width") or 2 * step)
    wings = float(p["wings"]) if p.get("wings") not in (None, "") else max(step, round(em / step) * step)
    delta = float(p["delta"]) if p.get("delta") not in (None, "") else None
    skip = float(p.get("skip") or 0)
    ratio = int(p.get("ratio") or 2)
    if width <= 0 or wings < 0 or skip < 0 or ratio < 1:
        raise StrategyError("width must be above 0; wings, skip and ratio cannot be negative")
    if delta is not None and not 0 < delta < 1:
        raise StrategyError("delta is between 0 and 1, like 0.16")

    def at(x):
        return {"by": "strike", "value": float(x)}

    def short_k(kind, sgn):
        if delta is not None:
            return {"by": "delta", "value": delta if kind == "CE" else -delta}
        return at(centre + sgn * wings) if p.get("strike") not in (None, "") else _off(sgn * wings)

    def wing(kind, sgn, short):
        """The bought wing `width` beyond a short strike chosen by delta or wings."""
        return {"by": "beyond", "value": sgn * width, "of": short}

    L = lots
    if name == "bull-call-spread":
        return [_spec(1, "CE", at(centre), L), _spec(-1, "CE", at(centre + width), L)]
    if name == "bear-call-spread":
        return [_spec(-1, "CE", at(centre), L), _spec(1, "CE", at(centre + width), L)]
    if name == "bull-put-spread":
        return [_spec(-1, "PE", at(centre), L), _spec(1, "PE", at(centre - width), L)]
    if name == "bear-put-spread":
        return [_spec(1, "PE", at(centre), L), _spec(-1, "PE", at(centre - width), L)]
    if name in ("long-straddle", "short-straddle"):
        s = 1 if name.startswith("long") else -1
        return [_spec(s, "CE", at(centre), L), _spec(s, "PE", at(centre), L)]
    if name in ("long-strangle", "short-strangle"):
        s = 1 if name.startswith("long") else -1
        return [_spec(s, "CE", short_k("CE", 1), L), _spec(s, "PE", short_k("PE", -1), L)]
    if name == "iron-condor":
        sc, sp = short_k("CE", 1), short_k("PE", -1)
        return [_spec(1, "PE", wing("PE", -1, sp), L), _spec(-1, "PE", sp, L),
                _spec(-1, "CE", sc, L), _spec(1, "CE", wing("CE", 1, sc), L)]
    if name == "iron-fly":
        return [_spec(1, "PE", at(centre - width), L), _spec(-1, "PE", at(centre), L),
                _spec(-1, "CE", at(centre), L), _spec(1, "CE", at(centre + width), L)]
    if name == "call-butterfly":
        return [_spec(1, "CE", at(centre - width), L), _spec(-1, "CE", at(centre), 2 * L),
                _spec(1, "CE", at(centre + width + skip), L)]
    if name == "put-butterfly":
        return [_spec(1, "PE", at(centre + width), L), _spec(-1, "PE", at(centre), 2 * L),
                _spec(1, "PE", at(centre - width - skip), L)]
    if name == "call-ratio-spread":
        return [_spec(1, "CE", at(centre), L), _spec(-1, "CE", at(centre + width), ratio * L)]
    if name == "put-ratio-spread":
        return [_spec(1, "PE", at(centre), L), _spec(-1, "PE", at(centre - width), ratio * L)]
    if name == "covered-call":
        return [_spec(1, "FUT", None, L), _spec(-1, "CE", short_k("CE", 1), L)]
    if name == "protective-put":
        return [_spec(1, "FUT", None, L), _spec(1, "PE", short_k("PE", -1), L)]
    if name == "calendar":
        kind = str(p.get("type") or "CE").upper()
        if kind not in ("CE", "PE"):
            raise StrategyError("calendar --type is CE or PE")
        return [_spec(-1, kind, at(centre), L, "near"), _spec(1, kind, at(centre), L, "far")]
    raise StrategyError("template %s has no legs" % name)


LEG_RE = re.compile(r"^\s*([+-]?)\s*(\d+)\s*(?:x\s*)?(CE|PE|CALL|PUT|FUT|FUTURE)\b\s*([^@=]*?)\s*"
                    r"(?:@\s*([0-9A-Za-z-]+))?\s*(?:=\s*([0-9.]+))?\s*$", re.I)


def parse_strike(text):
    """'23000' | 'ATM' | 'ATM+200' | 'ATM-150' | 'D25' / 'D0.25' (delta) | 'P50' (premium) -> strike spec."""
    t = str(text or "").strip().upper().replace(" ", "")
    if t in ("", "ATM"):
        return {"by": "offset", "value": 0.0}
    m = re.match(r"^ATM([+-]\d+(\.\d+)?)$", t)
    if m:
        return {"by": "offset", "value": float(m.group(1))}
    m = re.match(r"^D(\d+(\.\d+)?)$", t)
    if m:
        v = float(m.group(1))
        return {"by": "delta", "value": v / 100.0 if v >= 1 else v}
    m = re.match(r"^P(\d+(\.\d+)?)$", t)
    if m:
        return {"by": "price", "value": float(m.group(1))}
    try:
        v = float(t.replace(",", ""))
    except ValueError:
        raise StrategyError("strike %r: use 23000, ATM, ATM+200, D25 (delta 0.25) or P50 (premium ₹50)" % text)
    if v <= 0:
        raise StrategyError("a strike is above 0")
    return {"by": "strike", "value": v}


def parse_legs(text):
    """'-1 CE ATM+200, +1 CE ATM+300 @01-Dec-2026, -2 PE D25 =41.5, +1 FUT' -> leg specs.

    Each leg: sign (+ buy, - sell), lots, CE/PE/FUT, a strike (see parse_strike; none for FUT),
    optional @EXPIRY (DD-Mon-YYYY or YYYY-MM-DD; default the near expiry) and optional =PRICE (your
    own premium per unit instead of the chain's)."""
    specs = []
    for part in re.split(r"[;,\n]+", str(text or "")):
        if not part.strip():
            continue
        m = LEG_RE.match(part)
        if not m:
            raise StrategyError("leg %r: write it like '-1 CE 23000' or '+2 PE ATM-200 @01-Dec-2026'" % part.strip())
        sign = -1 if m.group(1) == "-" else 1
        lots = int(m.group(2))
        if lots < 1:
            raise StrategyError("leg %r: lots is 1 or more" % part.strip())
        kind = {"CALL": "CE", "PUT": "PE", "FUTURE": "FUT"}.get(m.group(3).upper(), m.group(3).upper())
        strike = None if kind == "FUT" else parse_strike(m.group(4))
        if kind == "FUT" and m.group(4).strip():
            raise StrategyError("leg %r: a future has no strike" % part.strip())
        expiry = "near"
        if m.group(5):
            d = parse_date(m.group(5))
            if d is None:
                raise StrategyError("leg %r: expiry %r is not a date" % (part.strip(), m.group(5)))
            expiry = d.isoformat()
        price = float(m.group(6)) if m.group(6) else None
        specs.append(_spec(sign, kind, strike, lots, expiry, price))
    if not specs:
        raise StrategyError("no legs: --legs \"-1 CE ATM+200, +1 CE ATM+300\"")
    if len(specs) > 12:
        raise StrategyError("at most 12 legs")
    return specs


def _pick(a, kind, spec, chosen):
    """A strike spec -> a listed strike in analysis a."""
    by, v = spec["by"], spec.get("value")
    if by == "strike":
        k = nearest_strike(a, v)
        if abs(k - v) > 1e-6:
            raise StrategyError("%s is not a listed strike for %s %s (nearest %s)" % (
                strike_text(v), a["underlying"], a["expiry_nse"] if a.get("expiry_nse") else a["expiry"],
                strike_text(k)))
        return k
    if by == "offset":
        return nearest_strike(a, (a.get("atm") or a["forward"]) + v, kind)
    if by == "beyond":
        base = _pick(a, kind, spec["of"], chosen)
        return nearest_strike(a, base + v, kind)
    s = kind.lower()
    best = None
    for x in a["rows"]:
        side = x.get(s)
        if not quoted(side):
            continue
        if by == "delta":
            if side.get("delta") is None:
                continue
            gap = abs(abs(side["delta"]) - abs(v))        # D16 means 0.16 for a call, -0.16 for a put
        elif by == "price":
            gap = abs(side["price"] - v)
        else:
            raise StrategyError("unknown strike rule %r" % by)
        if best is None or gap < best[0]:
            best = (gap, x["strike"])
    if best is None:
        raise StrategyError("no quoted %s strike to pick by %s" % (kind, by))
    return best[1]


def _market_for(markets, expiry, near):
    if expiry in (None, "near"):
        return markets[near]
    if expiry == "far":
        later = sorted(e for e in markets if e > near)
        if not later:
            raise StrategyError("a calendar needs a second, later expiry (--far-expiry)")
        return markets[later[0]]
    if expiry not in markets:
        raise StrategyError("no chain loaded for expiry %s" % expiry)
    return markets[expiry]


def resolve_legs(specs, markets, near=None, fill="mid", lot_size=None):
    """Leg specs + chains -> priced legs.

    markets: {expiry ISO: trader-options analysis}. near: the expiry that "near" means (default the
    earliest loaded). fill: "mid" (the chain's price: bid/ask mid, else LTP) or "touch" (pay the ask
    to buy, take the bid to sell). Each leg carries its premium, the IV used (the chain's own at that
    strike, else interpolated on the smile, else ATM), the forward and time of its expiry, and its
    Greeks per unit."""
    if not markets:
        raise StrategyError("no option chain loaded")
    near = near or min(markets)
    legs = []
    for sp in specs:
        a = _market_for(markets, sp.get("expiry"), near)
        lot = lot_size or a.get("lot_size")
        if not lot:
            raise StrategyError("no lot size for %s %s (fo_mktlots.csv did not have it)" % (
                a["underlying"], a["expiry"]))
        F, T, r = a["forward"], a["t_years"], a.get("rate", DEFAULT_RATE)
        leg = {"side": sp["side"], "lots": int(sp["lots"]), "lot_size": int(lot), "qty": int(sp["lots"]) * int(lot),
               "instrument": "FUT" if sp["kind"] == "FUT" else "OPT", "option_type": None if sp["kind"] == "FUT"
               else sp["kind"], "strike": None, "expiry": a["expiry"], "forward": F, "t_years": T, "rate": r}
        notes = []
        if sp["kind"] == "FUT":
            leg.update({"price": _r(F, 2), "price_source": "forward", "bid": None, "ask": None, "iv": None,
                        "iv_source": None, "oi": None})
            notes.append("priced at the parity forward of this expiry: a weekly has no future, so use the "
                         "month's future (or shares for a stock) trading near this price")
            g = {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        else:
            k = _pick(a, sp["kind"], sp["strike"], legs)
            row = _row(a, k)
            side = (row or {}).get(sp["kind"].lower()) or {}
            iv, iv_src = side.get("iv"), "chain"
            if iv is None:
                iv, iv_src = smile_iv(a, sp["kind"], k)
                notes.append("no IV of its own at this strike: %s IV %.2f%% used" % (iv_src, iv or 0))
            if iv is None:
                raise StrategyError("no IV anywhere on the %s chain to price %s %s" % (a["expiry"], strike_text(k),
                                                                                          sp["kind"]))
            price, src = side.get("price"), side.get("price_source")
            if fill == "touch":
                if sp["side"] == "BUY" and (side.get("ask") or 0) > 0:
                    price, src = side["ask"], "ask"
                elif sp["side"] == "SELL" and (side.get("bid") or 0) > 0:
                    price, src = side["bid"], "bid"
            if price is None:
                price, src = black76_price(F, k, T, r, iv / 100.0, sp["kind"]), "model"
                notes.append("no tradeable quote: priced by Black-76 at %.2f%% IV; a real fill may differ a lot" % iv)
            g = black76_greeks(F, k, T, r, iv / 100.0, sp["kind"])
            leg.update({"strike": k, "price": _r(price, 4), "price_source": src,
                        "bid": side.get("bid"), "ask": side.get("ask"), "iv": _r(iv, 2), "iv_source": iv_src,
                        "oi": side.get("oi")})
            if (side.get("oi") or 0) == 0 and side:
                notes.append("no open interest at this strike: thin market")
        if sp.get("price") is not None:
            leg["price"], leg["price_source"] = float(sp["price"]), "given"
        leg["greeks"] = {k2: _r(v, 6) for k2, v in g.items()}
        leg["notes"] = notes
        legs.append(leg)
    return legs


def leg_label(leg):
    if leg["instrument"] == "FUT":
        return "%s %d FUT %s" % (leg["side"], leg["lots"], nse_date(parse_date(leg["expiry"])))
    return "%s %d %s %s %s" % (leg["side"], leg["lots"], strike_text(leg["strike"]), leg["option_type"],
                               nse_date(parse_date(leg["expiry"])))


# ---------------------------------------------------------------- valuation

def _sign(leg):
    return 1 if leg["side"] == "BUY" else -1


def leg_value(leg, x, h, ref_forward, iv_shift=0.0):
    """Value per unit of one leg at horizon h (years after the chain's time) with the near expiry's
    forward at x. Another expiry's forward moves in proportion (carry): x x F_leg / F_near."""
    F = x * leg["forward"] / ref_forward if ref_forward else x
    if leg["instrument"] == "FUT":
        return F
    T = leg["t_years"] - h
    if T <= 1e-9:
        return max(0.0, F - leg["strike"]) if leg["option_type"] == "CE" else max(0.0, leg["strike"] - F)
    sigma = max(0.005, (leg["iv"] + iv_shift) / 100.0)
    return black76_price(F, leg["strike"], T, leg["rate"], sigma, leg["option_type"])


def leg_charges(leg, value, settles, cfg, on):
    """Round-trip charges of one leg closed at `value` per unit. settles=True: the leg reaches its
    expiry: an in-the-money long is exercised (STT on the intrinsic value), anything else expires
    with no closing order (an assigned short pays no STT on settlement)."""
    seg = "FUT" if leg["instrument"] == "FUT" else "OPT"
    q = leg["qty"]
    if seg == "FUT":
        if leg["side"] == "BUY":
            return round_trip_charges(leg["price"], value, q, cfg, seg, on)["total"]
        return round_trip_charges(value, leg["price"], q, cfg, seg, on)["total"]
    if leg["side"] == "BUY":
        if settles:
            if value > 0:
                return round_trip_charges(leg["price"], value, q, cfg, seg, on, True)["total"]
            return round_trip_charges(leg["price"], 0, q, cfg, seg, on)["total"]
        return round_trip_charges(leg["price"], value, q, cfg, seg, on)["total"]
    if settles:
        return round_trip_charges(0, leg["price"], q, cfg, seg, on)["total"]
    return round_trip_charges(value, leg["price"], q, cfg, seg, on)["total"]


class Position(object):
    """Priced legs and how to value them. h is years from the chain's time; settle marks the legs
    that have reached their expiry by h."""

    def __init__(self, legs, cfg=None, on=None):
        self.legs = legs
        self.cfg = cfg or {}
        self.on = on
        self.near = min(l["expiry"] for l in legs)
        near_leg = next(l for l in legs if l["expiry"] == self.near)
        self.ref = near_leg["forward"]
        self.t_near = near_leg["t_years"]
        self._tables = {}

    def gross(self, x, h, iv_shift=0.0):
        return sum(_sign(l) * (leg_value(l, x, h, self.ref, iv_shift) - l["price"]) * l["qty"] for l in self.legs)

    def charges(self, x, h, iv_shift=0.0):
        total = 0.0
        for l in self.legs:
            v = leg_value(l, x, h, self.ref, iv_shift)
            total += leg_charges(l, v, l["t_years"] - h <= 1e-9, self.cfg, self.on)
        return total

    def net(self, x, h, iv_shift=0.0):
        return self.gross(x, h, iv_shift) - self.charges(x, h, iv_shift)

    def charges_fast(self, x, h, iv_shift=0.0):
        """charges() read off a table on a wide grid (exact at its points; charges move slowly)."""
        key = (round(h, 10), round(iv_shift, 6))
        tab = self._tables.get(key)
        if tab is None:
            xs = wide_grid(self)
            tab = (xs, [self.charges(v, h, iv_shift) for v in xs])
            self._tables[key] = tab
        xs, cs = tab
        if x <= xs[0]:
            return cs[0]
        if x >= xs[-1]:
            return cs[-1]
        i = bisect.bisect_left(xs, x)
        x0, x1 = xs[i - 1], xs[i]
        w = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
        return cs[i - 1] + w * (cs[i] - cs[i - 1])

    def strikes(self):
        return sorted({l["strike"] for l in self.legs if l["strike"] is not None})


def wide_grid(pos, n=400):
    """Prices from a quarter of the forward to three times it (geometric), plus every strike."""
    lo, hi = pos.ref * 0.25, pos.ref * 3.0
    xs = [lo * (hi / lo) ** (i / float(n - 1)) for i in range(n)]
    return sorted(set(xs + pos.strikes() + [pos.ref]))


def display_grid(pos, sigma, n=241):
    """The chart's x range: the strikes and the forward, +/- about 3.5 standard deviations to the near expiry."""
    ks = pos.strikes() + [pos.ref]
    sd = pos.ref * max(sigma or 0.12, 0.05) * math.sqrt(max(pos.t_near, 1.0 / 365))
    lo, hi = max(pos.ref * 0.05, min(ks) - 3.5 * sd), max(ks) + 3.5 * sd
    pad = 0.04 * (hi - lo)
    lo, hi = max(0.01, lo - pad), hi + pad
    xs = [lo + (hi - lo) * i / float(n - 1) for i in range(n)]
    return sorted(set([round(v, 2) for v in xs] + [k for k in ks if lo <= k <= hi]))


def payoff_curve(pos, h_curve, iv_shift=0.0, sigma=None, xs=None):
    """{"x", "expiry", "expiry_net", "at", "at_net"}: P&L in rupees on the chart's grid, at the near
    expiry and at horizon h_curve (years), gross and after charges."""
    xs = xs or display_grid(pos, sigma)
    out = {"x": xs, "expiry": [], "expiry_net": [], "at": [], "at_net": []}
    for x in xs:
        g = pos.gross(x, pos.t_near, iv_shift)
        out["expiry"].append(round(g, 2))
        out["expiry_net"].append(round(g - pos.charges(x, pos.t_near, iv_shift), 2))
        g2 = pos.gross(x, h_curve, iv_shift)
        out["at"].append(round(g2, 2))
        out["at_net"].append(round(g2 - pos.charges(x, h_curve, iv_shift), 2))
    return out


def breakevens(fn, xs, tol=0.01):
    """Where fn crosses zero on the grid xs, refined by bisection to `tol` points."""
    out = []
    ys = [fn(x) for x in xs]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y0 == 0 and (not out or abs(out[-1] - x0) > tol):
            out.append(x0)
            continue
        if (y0 < 0 < y1) or (y0 > 0 > y1):
            a, b, fa = x0, x1, y0
            for _ in range(60):
                m = 0.5 * (a + b)
                fm = fn(m)
                if (fa < 0) == (fm < 0):
                    a, fa = m, fm
                else:
                    b = m
                if b - a < tol:
                    break
            out.append(round(0.5 * (a + b), 2))
    return out


def _best(vals, ref):
    """(max, min) of [(value, x)], ties (to the paisa) going to the x nearest ref: a flat stretch is
    reported where it starts, not at an underlying of 0."""
    hi = max(vals, key=lambda v: (round(v[0], 2), -abs(v[1] - ref)))
    lo = min(vals, key=lambda v: (round(v[0], 2), abs(v[1] - ref)))
    return hi, lo


def extremes_after_charges(pos, fn, gross, xs):
    """Max profit and loss of the after-charges curve. Whether a side is unlimited comes from the gross
    curve: STT on exercise grows with the intrinsic value, so the net slope far out is never exactly
    zero, but that is not an unlimited loss. Searched over the chart's range (the strikes +/- about 3.5
    standard deviations): far beyond it the STT on a deep in-the-money exercise keeps growing."""
    pts = pos.strikes() + [pos.ref] + list(xs) + [p for p in (gross.get("at_profit"), gross.get("at_loss"))
                                                         if p is not None]
    pts = sorted(set(pts))
    vals = [(fn(x), x) for x in pts]
    hi, lo = _best(vals, pos.ref)
    return {"max_profit": None if gross["unlimited_profit"] else _r(hi[0], 2),
            "max_loss": None if gross["unlimited_loss"] else _r(lo[0], 2)}


def extremes(pos, fn):
    """Max profit and max loss of fn(x) at the near expiry over every underlying price from 0 up.

    With one expiry the payoff is straight between strikes, so its extremes sit at 0, at a strike
    or far out; the slope far out says whether a side is unlimited. With two expiries the far leg
    is priced (Black-76), the grid is fine and the result is marked approximate.
    -> {"max_profit", "max_loss", "at_profit", "at_loss", "unlimited_profit", "unlimited_loss", "exact"}"""
    single = len({l["expiry"] for l in pos.legs}) == 1
    pts = [0.0] + pos.strikes() + [pos.ref]
    if not single:
        pts += wide_grid(pos, 600)
    pts = sorted(set(pts))
    top = max(pts + [pos.ref]) * 2.0
    vals = [(fn(x), x) for x in pts]
    far1, far2 = fn(top), fn(top * 1.5)
    slope = (far2 - far1) / (top * 0.5)
    hi, lo = _best(vals, pos.ref)
    unit = 0.05 * min(l["qty"] for l in pos.legs)      # one naked option's slope is its whole qty
    unl_p = slope > unit
    unl_l = slope < -unit
    return {"max_profit": None if unl_p else _r(max(hi[0], far1), 2),
            "max_loss": None if unl_l else _r(min(lo[0], far1), 2),
            "at_profit": None if unl_p else hi[1], "at_loss": None if unl_l else lo[1],
            "unlimited_profit": unl_p, "unlimited_loss": unl_l, "exact": single}


def net_greeks(legs):
    """Position Greeks: delta in units of the underlying (₹ per 1 point), gamma per point,
    theta ₹ per calendar day, vega ₹ per vol point."""
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    missing = False
    for l in legs:
        for k in out:
            v = (l.get("greeks") or {}).get(k)
            if v is None:
                missing = True
                continue
            out[k] += _sign(l) * v * l["qty"]
    res = {k: _r(v, 4 if k == "gamma" else 2) for k, v in out.items()}
    lot = legs[0]["lot_size"] if legs else 1
    res["delta_lots"] = _r(out["delta"] / lot, 3) if lot else None
    res["complete"] = not missing
    return res


def net_premium(legs):
    """Premium received (credit, +) or paid (debit, -), options only; futures carry no premium."""
    amt = sum(-_sign(l) * l["price"] * l["qty"] for l in legs if l["instrument"] == "OPT")
    return {"amount": _r(amt, 2), "kind": "credit" if amt > 0 else ("debit" if amt < 0 else "zero"),
            "paid": _r(sum(l["price"] * l["qty"] for l in legs if l["instrument"] == "OPT" and l["side"] == "BUY"), 2),
            "received": _r(sum(l["price"] * l["qty"] for l in legs if l["instrument"] == "OPT"
                               and l["side"] == "SELL"), 2)}


def spread_cost(legs):
    """Rupees lost against the chain's price by crossing to the touch (buy at ask, sell at bid)."""
    total = 0.0
    for l in legs:
        if l["instrument"] != "OPT" or l["price_source"] not in ("mid", "ltp"):
            continue
        touch = l["ask"] if l["side"] == "BUY" else l["bid"]
        if touch:
            total += abs(touch - l["price"]) * l["qty"]
    return _r(total, 2)


def defined_risk(legs):
    """True when every short option is covered by a long of the same type, same or later expiry,
    strike for strike in quantity (spreads, condors, flies, calendars), and there is no future."""
    if any(l["instrument"] == "FUT" for l in legs):
        return False
    for kind in ("CE", "PE"):
        shorts = sorted((l for l in legs if l["option_type"] == kind and l["side"] == "SELL"), key=lambda l: l["expiry"])
        for s in shorts:
            cover = sum(l["qty"] for l in legs if l["option_type"] == kind and l["side"] == "BUY"
                        and l["expiry"] >= s["expiry"])
            short_qty = sum(l["qty"] for l in legs if l["option_type"] == kind and l["side"] == "SELL"
                            and l["expiry"] <= s["expiry"])
            if cover < short_qty:
                return False
    return True


# ---------------------------------------------------------------- margin

def span_style(pos, kind, scan=None, asof=None):
    """What a broker would block: a SPAN-style estimate for the whole position.

    With a short option or a future: scanning risk (the worst loss over 16 scenarios on the WHOLE
    position, so bought wings cut it: price moves of 0, +/-1/3, +/-2/3, +/-1 x the price scan, each with
    IV up and down by the vol scan, and +/-3 x the scan at 30% weight) + exposure (ELM) on the notional
    of EVERY short option and future, hedged or not (exposure is per short leg; the hedge benefit comes
    through SPAN only) + 2% more on index shorts on their expiry day + premium paid on bought options.
    Only bought options: the premium paid, nothing else (no SPAN is collected on a long option)."""
    p = dict(SPAN_DEFAULTS[kind])
    p.update({k: v for k, v in (scan or {}).items() if v is not None})
    F = pos.ref
    ps = F * p["scan_pct"] / 100.0
    vs = p["vol_scan"]
    paid = sum(l["price"] * l["qty"] for l in pos.legs if l["instrument"] == "OPT" and l["side"] == "BUY")
    received = sum(l["price"] * l["qty"] for l in pos.legs if l["instrument"] == "OPT" and l["side"] == "SELL")
    assumptions = {"price_scan": _r(ps, 2), "scan_pct": p["scan_pct"], "vol_scan_points": vs,
                   "elm_pct": p["elm_pct"], "source": p["source"]}
    if not any(l["instrument"] == "FUT" or l["side"] == "SELL" for l in pos.legs):
        return {"method": "premium", "scan_risk": 0.0, "worst_scenario": None, "exposure": 0.0,
                "expiry_day_elm": 0.0, "premium_paid": _r(paid, 2), "premium_received": 0.0, "total": _r(paid, 2),
                "error_pct": 0, "low": _r(paid, 2), "high": _r(paid, 2), "assumptions": assumptions}
    base = pos.gross(F, 0.0)
    worst, at = 0.0, None
    for m in (0.0, 1 / 3.0, -1 / 3.0, 2 / 3.0, -2 / 3.0, 1.0, -1.0):
        for dv in (vs, -vs):
            loss = base - pos.gross(F + m * ps, 0.0, dv)
            if loss > worst:
                worst, at = loss, {"move": _r(m * ps, 2), "vol": dv}
    for m in (EXTREME_MOVE, -EXTREME_MOVE):
        loss = (base - pos.gross(F + m * ps, 0.0)) * EXTREME_WEIGHT
        if loss > worst:
            worst, at = loss, {"move": _r(m * ps, 2), "vol": 0.0, "weight": EXTREME_WEIGHT}
    exposure, extra = 0.0, 0.0
    day = asof.date().isoformat() if asof else None
    for l in pos.legs:
        notional = l["forward"] * l["qty"]
        if l["instrument"] == "FUT" or l["side"] == "SELL":
            exposure += notional * p["elm_pct"] / 100.0
        if kind == "index" and l["instrument"] == "OPT" and l["side"] == "SELL" and day == l["expiry"]:
            extra += notional * EXPIRY_DAY_ELM_PCT / 100.0
    total = worst + exposure + extra + paid
    return {"method": "span-style", "scan_risk": _r(worst, 2), "worst_scenario": at, "exposure": _r(exposure, 2),
            "expiry_day_elm": _r(extra, 2), "premium_paid": _r(paid, 2), "premium_received": _r(received, 2),
            "total": _r(total, 2), "error_pct": p["error_pct"],
            "low": _r(total * (1 - p["error_pct"] / 100.0), 2), "high": _r(total * (1 + p["error_pct"] / 100.0), 2),
            "assumptions": assumptions}


def margin_estimate(pos, kind, max_loss, scan=None, asof=None, max_profit=None):
    """Margin required (the headline: what a broker would block, estimated) and, apart from it, the
    exact max loss and the return on margin.

    required     the SPAN-style estimate (span_style), +/- error_pct; only bought options: the premium
    max_loss     the most the position can lose at expiry (positive rupees), exact for one expiry; None
                 when unlimited. It is NOT the margin: a broker blocks SPAN + exposure, usually far more
                 for a hedged credit spread
    return_on_margin   max profit at expiry / required, in %, None when either is unlimited or missing
    broker_basket stays None until the broker's own basket margin is read (attach_broker_margin)."""
    span = span_style(pos, kind, scan, asof)
    req = span["total"]
    loss = _r(max(0.0, -max_loss), 2) if max_loss is not None else None
    rom = _r(100.0 * max_profit / req, 2) if max_profit is not None and req and req > 0 else None
    err = span["error_pct"]
    if span["method"] == "premium":
        label = "Margin required (premium paid up front, exact)"
        explain = ("Only bought options: you pay the premium up front and no SPAN or exposure margin is "
                   "blocked. The premium is also the most you can lose.")
    else:
        label = "Margin required (estimate, +/-%d%%)" % err
        explain = ("A SPAN-style estimate of what a broker blocks, +/-%d%%: the worst of 16 price and IV "
                   "scenarios on the whole position (price scan %s%% of the forward, IV scan %s points), so "
                   "bought wings cut it; plus %s%% exposure on the notional of every short option and future, "
                   "hedged or not%s; plus premium paid. Brokers differ on whether premium received offsets it. "
                   "Not the exchange's SPAN file: check your broker's basket margin." % (
                       err, fnum(span["assumptions"]["scan_pct"], 1), fnum(span["assumptions"]["vol_scan_points"], 0),
                       fnum(span["assumptions"]["elm_pct"], 1),
                       ", and 2% more on index shorts expiring today" if span["expiry_day_elm"] else ""))
    return {"label": label, "required": req, "low": span["low"], "high": span["high"], "error_pct": err,
            "method": span["method"], "explain": explain, "defined_risk": defined_risk(pos.legs),
            "max_loss": loss, "max_loss_unlimited": max_loss is None,
            "return_on_margin": rom, "span_style": span, "broker_basket": None,
            "broker_note": "no broker connected: check the broker's basket margin before any real order"}


# ---------------------------------------------------------------- probability

def _quantiles(n):
    nd = statistics.NormalDist()
    return [nd.inv_cdf((i + 0.5) / n) for i in range(n)]


def _summary(pnls, n_paths=None):
    xs = sorted(pnls)
    n = len(xs)
    k = max(1, int(round(n * TAIL)))
    return {"pop": _r(100.0 * sum(1 for v in xs if v > 0) / n, 2), "ev": _r(sum(xs) / n, 2),
            "tail_5pct": _r(xs[k - 1], 2), "tail_5pct_avg": _r(sum(xs[:k]) / k, 2),
            "median": _r(xs[n // 2], 2), "paths": n_paths or n}


def textbook_probability(pos, sigma, h=None, iv_shift=0.0, n=2000):
    """Lognormal at IV (the textbook): the near expiry's price at horizon h is F x exp(-s^2 t / 2 + s
    sqrt(t) z), z standard normal, s = sigma (decimal). PoP = share of the distribution where the P&L
    after charges is above 0; also the gross PoP, the expected value and the 5% tail (the 5th
    percentile and the average of the worst 5%) after charges. Midpoint quantiles, so it is
    deterministic."""
    h = pos.t_near if h is None else h
    t = max(h, 1e-9)
    s = max(sigma, 1e-4)
    xs = [pos.ref * math.exp(-0.5 * s * s * t + s * math.sqrt(t) * z) for z in _quantiles(n)]
    gross = [pos.gross(x, h, iv_shift) for x in xs]
    net = [g - pos.charges_fast(x, h, iv_shift) for g, x in zip(gross, xs)]
    out = _summary(net)
    out["pop_gross"] = _r(100.0 * sum(1 for v in gross if v > 0) / len(gross), 2)
    out.update({"method": "lognormal at IV %.2f%% to %s (forward-neutral: no drift); %d quantile points" % (
        100 * s, "expiry" if abs(h - pos.t_near) < 1e-9 else "the horizon", n), "sigma": _r(100 * s, 2)})
    del out["paths"]
    return out


def returns_from_closes(closes):
    """Log returns between consecutive closes (missing values skipped)."""
    cs = [c for c in closes if c]
    return [math.log(b / a) for a, b in zip(cs, cs[1:]) if a > 0 and b > 0]


def simulated_probability(pos, daily, steps, intraday=None, intraday_steps=0, h=None, seed=DEFAULT_SEED,
                          paths=DEFAULT_PATHS, iv_shift=0.0, scale_to=None):
    """Bootstrap: each path adds `steps` daily log returns and `intraday_steps` five-minute ones,
    drawn with replacement from the underlying's own history (demeaned: no drift is assumed), to the
    forward. scale_to (annual vol, decimal) rescales the returns to that volatility (an IV-shift
    scenario keeps the history's shape, takes the market's level). Seeded: the same inputs give the
    same answer. P&L after charges at the horizon, as in textbook_probability."""
    h = pos.t_near if h is None else h
    rng = random.Random(seed)
    d = list(daily or [])
    i5 = list(intraday or [])
    if not d and not (i5 and intraday_steps):
        return None
    if d:
        m = sum(d) / len(d)
        d = [x - m for x in d]
    if i5:
        m5 = sum(i5) / len(i5)
        i5 = [x - m5 for x in i5]
    realised = statistics.pstdev(d) * math.sqrt(252) if len(d) > 2 else None
    if scale_to and realised:
        k = scale_to / realised
        d = [x * k for x in d]
        if i5:
            i5 = [x * k for x in i5]
    nd = len(d)
    n5 = len(i5)
    xs = []
    for _ in range(paths):
        total = 0.0
        for _ in range(steps if nd else 0):
            total += d[int(rng.random() * nd)]
        for _ in range(intraday_steps if n5 else 0):
            total += i5[int(rng.random() * n5)]
        xs.append(pos.ref * math.exp(total))
    gross = [pos.gross(x, h, iv_shift) for x in xs]
    net = [g - pos.charges_fast(x, h, iv_shift) for g, x in zip(gross, xs)]
    out = _summary(net, paths)
    out["pop_gross"] = _r(100.0 * sum(1 for v in gross if v > 0) / len(gross), 2)
    out["realised_vol"] = _r(100 * realised, 2) if realised else None
    out["scaled_to_vol"] = _r(100 * scale_to, 2) if scale_to else None
    return out


# ---------------------------------------------------------------- the whole evaluation

def warnings_for(legs, kind, asof, cfg, on):
    """STT on exercise for longs that may end in the money, physical settlement for stock options,
    thin quotes, expiry day."""
    out = []
    for l in legs:
        if l["instrument"] != "OPT":
            continue
        itm_now = (l["forward"] > l["strike"]) if l["option_type"] == "CE" else (l["forward"] < l["strike"])
        if l["side"] == "BUY":
            probe = l["forward"] * (1.02 if l["option_type"] == "CE" else 0.98)
            intrinsic = max(l["strike"] * 0.02, abs(probe - l["strike"])) if not itm_now else \
                abs(l["forward"] - l["strike"])
            ex = round_trip_charges(l["price"], intrinsic, l["qty"], cfg, "OPT", on, True)
            sold = round_trip_charges(l["price"], intrinsic, l["qty"], cfg, "OPT", on)
            rate = fno_rates("OPT", on, cfg)
            out.append("STT on exercise: %s, if it ends in the money at expiry it is exercised and STT of "
                       "%s%% is charged on the whole intrinsic value, even when the trade lost money overall "
                       "(%s points in the money: STT %s, round-trip charges %s). Sold before 15:30 on expiry day "
                       "instead: STT %s%% of the premium, charges %s." % (
                           leg_label(l), fnum(rate["stt_exercise_pct"], 3).rstrip("0").rstrip("."), fnum(intrinsic),
                           inr(ex["stt"]), inr(ex["total"]), fnum(rate["stt_sell_pct"], 3).rstrip("0").rstrip("."),
                           inr(sold["total"])))
        if kind == "stock":
            out.append("%s: stock options settle by DELIVERY. In the money at expiry means %s shares "
                       "(about %s) to %s, and brokers raise margins from a few days before expiry." % (
                           leg_label(l), fnum(l["qty"], 0), inr(l["strike"] * l["qty"]),
                           "take" if (l["option_type"] == "CE") == (l["side"] == "BUY") else "give"))
    if asof:
        if any(l["expiry"] == asof.date().isoformat() for l in legs):
            out.append("expiry day: gamma is extreme, Greeks are unreliable in the last hours, and index "
                       "shorts expiring today need 2% more margin")
    for l in legs:
        for n in l.get("notes") or []:
            if "thin" in n or "no tradeable" in n:
                out.append("%s: %s" % (leg_label(l), n))
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def horizon_years(at, asof, near_expiry):
    """--at 'today' | 'expiry' | a date | 'YYYY-MM-DD HH:MM' -> (years after asof, label)."""
    end = expiry_moment(near_expiry)
    if at in (None, "", "expiry"):
        return years_between(asof, end), "expiry"
    if at == "today":
        return 0.0, "now"
    t = parse_time(at)
    if t is None:
        raise StrategyError("--at is today, expiry, a date (2026-11-02) or 'YYYY-MM-DD HH:MM'")
    if t.time() == _dt.time(0, 0):
        t = _dt.datetime.combine(t.date(), EXPIRY_TIME)
    if t < asof:
        raise StrategyError("--at %s is before the chain's time %s" % (at, asof))
    if t > end:
        raise StrategyError("--at %s is after the near expiry %s" % (at, nse_date(end.date())))
    return years_between(asof, t), t.isoformat(timespec="minutes")


def evaluate(legs, markets, cfg=None, name="custom", params=None, at=None, iv_shift=0.0, history=None,
             seed=DEFAULT_SEED, paths=DEFAULT_PATHS, scan=None, now=None, simulate=True):
    """Everything about one position: the strategy.json body (schema bharat-trader/strategy/1).

    legs: resolve_legs() output. markets: {expiry: trader-options analysis}. cfg: trader/config.json
    (F&O charge overrides in "fno_charges"). at: the curve's horizon ("today", a date; default the
    near expiry). iv_shift: vol points added to every leg's IV for valuations before expiry.
    history: {"daily": [closes], "intraday": [closes], "source", "from", "to"} for the simulation.
    """
    if not legs:
        raise StrategyError("no legs")
    cfg = cfg or {}
    near = min(l["expiry"] for l in legs)
    a = markets[near]
    under = a["underlying"]
    kind = a.get("kind") or ("index" if under in INDICES else "stock")
    asof = parse_time(a.get("asof")) or now or now_ist()
    on = asof.date().isoformat()
    pos = Position(legs, cfg, on)
    sigma = (a.get("atm_iv") or 12.0) / 100.0
    h, h_label = horizon_years(at, asof, near)
    xs = display_grid(pos, sigma)
    curve = payoff_curve(pos, h, iv_shift, sigma, xs)
    tn = pos.t_near
    gross_be = breakevens(lambda x: pos.gross(x, tn), xs)
    net_be = breakevens(lambda x: pos.net(x, tn), xs)
    at_be = breakevens(lambda x: pos.net(x, h, iv_shift), xs)
    ext = extremes(pos, lambda x: pos.gross(x, tn))
    ext_net = extremes_after_charges(pos, lambda x: pos.net(x, tn), ext, xs)
    fwd = pos.ref
    ch_spot = pos.charges(fwd, tn)
    margin = margin_estimate(pos, kind, None if ext["unlimited_loss"] else ext["max_loss"], scan, asof,
                             None if ext["unlimited_profit"] else ext["max_profit"])
    prem = net_premium(legs)
    shift = []
    for i, b in enumerate(gross_be):
        nb = min(net_be, key=lambda v: abs(v - b)) if net_be else None
        shift.append({"gross": b, "after_charges": nb, "shift": _r(nb - b, 2) if nb is not None else None})

    prob = {"textbook": textbook_probability(pos, sigma, h, iv_shift), "simulated": None, "scenarios": [],
            "horizon": h_label}
    notes = []
    if simulate and history and history.get("daily"):
        start = asof
        end = asof + _dt.timedelta(minutes=h * MINUTES_A_YEAR)
        steps = sessions_between(start, end)
        left = minutes_left_today(start) if end.date() > start.date() else max(0, int((end - max(
            start, _dt.datetime.combine(start.date(), _dt.time(9, 15)))).total_seconds() // 60))
        i5_steps = left // 5
        daily = returns_from_closes(history["daily"])
        intraday = returns_from_closes(history.get("intraday") or [])
        if not intraday:
            steps += 1 if left > 180 else 0
            i5_steps = 0
        sim = simulated_probability(pos, daily, steps, intraday, i5_steps, h, seed, paths, iv_shift)
        if sim:
            sim["method"] = ("bootstrap: %d daily and %d five-minute returns per path, drawn with replacement "
                             "from %s (demeaned), %d paths, seed %d" % (
                                 steps, i5_steps, history.get("source") or "the underlying's history",
                                 paths, seed))
            sim["sessions"] = steps
            sim["intraday_steps"] = i5_steps
            sim["history"] = {"daily_returns": len(daily), "intraday_returns": len(intraday),
                              "from": history.get("from"), "to": history.get("to"), "source": history.get("source")}
            prob["simulated"] = sim
            for dv in IV_SCENARIOS:
                target = max(0.01, sigma + dv / 100.0)
                sc = simulated_probability(pos, daily, steps, intraday, i5_steps, h, seed, max(2000, paths // 2),
                                           iv_shift + dv, target)
                if sc:
                    prob["scenarios"].append({"iv_shift": dv, "vol": _r(100 * target, 2), "pop": sc["pop"],
                                              "ev": sc["ev"], "tail_5pct": sc["tail_5pct"],
                                              "tail_5pct_avg": sc["tail_5pct_avg"]})
    elif simulate:
        prob["simulated_skipped"] = "no price history (the simulation needs the underlying's daily closes)"
        notes.append("no price history: the simulated probability needs the underlying's daily closes")
    else:
        prob["simulated_skipped"] = "not asked for (--no-sim)"
    years = {parse_date(l["expiry"]).year for l in legs} | {asof.year}
    if any(y not in CALENDAR_YEARS for y in years):
        notes.append("no NSE holiday table for %s: the simulation counts weekdays as sessions" % (
            ", ".join(str(y) for y in sorted(years) if y not in CALENDAR_YEARS)))
    if len({l["expiry"] for l in legs}) > 1:
        notes.append("two expiries: 'at expiry' means the near one (%s); the later leg is valued by Black-76 "
                     "at its own IV%s, so its curve is a model, not a certainty" % (
                         nse_date(parse_date(near)), " %+.1f points" % iv_shift if iv_shift else ""))

    lot = legs[0]["lot_size"]
    out = {
        "schema": SCHEMA,
        "name": name,
        "underlying": under,
        "kind": kind,
        "lot_size": lot,
        "asof": asof.isoformat(timespec="seconds"),
        "spot": a.get("spot"),
        "forward": _r(fwd, 2),
        "atm": a.get("atm"),
        "atm_iv": a.get("atm_iv"),
        "expiries": sorted({l["expiry"] for l in legs}),
        "near_expiry": near,
        "days_to_expiry": (parse_date(near) - asof.date()).days,
        "params": params or {},
        "legs": [{k: l[k] for k in ("side", "lots", "lot_size", "qty", "instrument", "option_type", "strike",
                                    "expiry", "price", "price_source", "bid", "ask", "iv", "iv_source", "forward",
                                    "t_years", "greeks", "oi", "notes")} for l in legs],
        "net_premium": prem,
        "spread_cost": spread_cost(legs),
        "greeks": net_greeks(legs),
        "expiry": {
            "breakevens": gross_be, "breakevens_after_charges": net_be, "breakeven_shift": shift,
            "max_profit": ext["max_profit"], "max_loss": ext["max_loss"],
            "unlimited_profit": ext["unlimited_profit"], "unlimited_loss": ext["unlimited_loss"],
            "max_profit_at": ext["at_profit"], "max_loss_at": ext["at_loss"],
            "max_profit_after_charges": ext_net["max_profit"], "max_loss_after_charges": ext_net["max_loss"],
            "exact": ext["exact"],
            "reward_risk": _r(ext["max_profit"] / -ext["max_loss"], 2) if ext["max_profit"] is not None and
            ext["max_loss"] not in (None, 0) and ext["max_loss"] < 0 else None,
        },
        "charges": {"at_forward_expiry": _r(ch_spot, 2),
                    "at_forward_expiry_detail": charges_detail(pos, fwd, tn),
                    "rates_from": fno_rates("OPT", on, cfg),
                    "note": "round trip per leg: an in-the-money long at expiry is exercised (STT on the intrinsic "
                            "value), out-of-the-money options expire with no closing order, an assigned short pays "
                            "no STT on settlement; before expiry every leg is closed at its value"},
        "curve": dict(curve, horizon=h_label, iv_shift=iv_shift, breakevens_at=at_be,
                      spot=a.get("spot"), forward=_r(fwd, 2),
                      expected_move=(a.get("expected_move") or {})),
        "margin": margin,
        "probability": prob,
        "warnings": warnings_for(legs, kind, asof, cfg, on) + list(a.get("warnings") or []),
        "notes": notes,
        "sources": {"chain": (a.get("sources") or {}).get("chain"), "chain_time": a.get("asof"),
                    "model": "Black-76 on each expiry's parity forward; IV per leg from the chain",
                    "history": (history or {}).get("source")},
    }
    out["orders"] = draft_orders(out)
    return out


def charges_detail(pos, x, h):
    """Charges per leg at underlying x and horizon h: [{"leg", "total"}]."""
    rows = []
    for l in pos.legs:
        v = leg_value(l, x, h, pos.ref)
        rows.append({"leg": leg_label(l), "value": _r(v, 2),
                     "total": _r(leg_charges(l, v, l["t_years"] - h <= 1e-9, pos.cfg, pos.on), 2)})
    return rows


# ---------------------------------------------------------------- persona tag (same in budget and strategy)
# An optional tag naming the agent persona that drafted a proposal (agents/<name>.md). It rides in each
# order's "plan" note, which is never sent to a broker and is not part of the trader-risk fingerprint,
# and the tagged proposal is logged in trader/state/personas.json so the persona scorecard can credit
# the paper fills to it after trader/orders.json is replaced. It changes no size, no check, no verdict.

PERSONA_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
PERSONA_LOG_MAX = 2000


def persona_name(text):
    """The persona tag, lower case (e.g. intraday-trader), or None. ValueError for a bad name."""
    if text is None or not str(text).strip():
        return None
    name = str(text).strip().lower()
    if len(name) > 40 or not PERSONA_RE.match(name):
        raise ValueError("--persona is a short lowercase name such as intraday-trader (the agents/ file name)")
    return name


def tag_orders(orders, persona):
    """Write the tag into each order's plan note. The broker-neutral order is untouched."""
    for o in orders:
        plan = dict(o.get("plan") or {})
        plan["persona"] = persona
        o["plan"] = plan
    return orders


def log_persona_orders(orders_path, persona, orders, source, created_at):
    """Append the tagged proposal to state/personas.json next to the orders file. -> its path."""
    base = os.path.dirname(os.path.abspath(orders_path))
    path = os.path.join(base, "state", "personas.json")
    doc = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError:
            doc = None
    rows = doc.get("drafts") if isinstance(doc, dict) and isinstance(doc.get("drafts"), list) else []
    rows.append({"at": created_at, "persona": persona, "source": source, "orders": orders})
    out = {"note": "Proposals tagged with the persona that drafted them, for the persona scorecard. "
                   "Read only by the scorecard; nothing here is ever sent to a broker.",
           "drafts": rows[-PERSONA_LOG_MAX:]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path

# ---------------------------------------------------------------- end of persona tag


# ---------------------------------------------------------------- orders (proposals only)

def tick_price(x, side):
    """A premium on the 0.05 tick: up for a buy limit, down for a sell limit (never a worse price
    than the one priced)."""
    d = Decimal(repr(float(x))) / OPTION_TICK
    n = d.to_integral_value(rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR)
    return float((n * OPTION_TICK).quantize(PAISE))


def draft_orders(doc, product="NRML"):
    """The legs as broker-neutral order proposals: exchange NFO, product NRML (or MIS), a LIMIT at the
    priced premium on the tick. tradingsymbol and instrument_token are left None for the broker
    adapter to fill from its own instrument list (each broker spells weekly symbols differently)."""
    product = str(product or "NRML").upper()
    if product not in ("NRML", "MIS"):
        raise StrategyError("product is NRML (carry forward) or MIS (intraday)")
    out = []
    idx = doc.get("kind") == "index"
    for i, l in enumerate(doc["legs"], 1):
        opt = l["instrument"] == "OPT"
        out.append({
            "symbol": doc["underlying"], "exchange": "NFO", "segment": "OPT" if opt else "FUT",
            "instrument": ("OPTIDX" if idx else "OPTSTK") if opt else ("FUTIDX" if idx else "FUTSTK"),
            "expiry": l["expiry"], "strike": l["strike"], "option_type": l["option_type"],
            "tradingsymbol": None, "instrument_token": None,
            "side": l["side"], "lots": l["lots"], "lot_size": l["lot_size"], "qty": l["qty"],
            "product": product, "order_type": "LIMIT", "price": tick_price(l["price"], l["side"]),
            "validity": "DAY", "leg": i, "tag": "bt-strategy",
            "why": "%s leg %d of %d: %s at %s (%s)" % (doc["name"], i, len(doc["legs"]), leg_label(l),
                                                       fnum(l["price"]), l["price_source"]),
        })
    return out


def orders_document(doc, product="NRML", created_at=None):
    """trader/orders.json body for a strategy: a proposal, paper, every leg one order."""
    return {"created_by": "trader-strategy", "created_at": created_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "status": "proposed", "paper": True, "basket": True,
        "note": "Proposals only. Nothing is placed until trader-risk passes each order and you say yes to it. "
                "Multi-leg: place every leg or none.",
        "source": "trader/strategy.json (%s %s %s)" % (doc["name"], doc["underlying"], doc["near_expiry"]),
        "orders": draft_orders(doc, product)}


# ---------------------------------------------------------------- loading chains and history (files, network)

def load_json(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise StrategyError("cannot read %s: %s" % (path, e))


def options_module(here=None):
    """trader-options' script when that skill sits next to this one (a sibling folder), else None."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(here or __file__)))
    path = os.path.join(base, "trader-options", "options.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("bt_strategy_options", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_markets(symbol, expiries, offline=None, save=None, now=None, rate=None, config="trader/config.json"):
    """{expiry ISO: analysis} from trader-options (network, or saved files in an --offline folder).
    expiries: [wanted or None (nearest), ...]."""
    op = options_module()
    if op is None:
        raise StrategyError("trader-options is not installed next to this skill: run it with --write and pass "
                            "--options trader/options.json")
    now = now or now_ist()
    try:
        fetcher = op.Fetcher(offline=offline, save=save)
        info = op.fetch_contract_info(fetcher, symbol)
        try:
            lots = op.fetch_lots(fetcher)
        except op.OptionsError:
            lots = {}
        if rate is None:
            r, src = op.risk_free_rate(config)
        else:
            r, src = (rate / 100.0 if rate > 1 else rate), "--rate"
        out = {}
        for want in expiries:
            e = op.pick_expiry(info["expiries"], want, now)
            chain = op.fetch_chain(fetcher, symbol, e)
            ts = op.parse_nse_time(chain.get("timestamp"))
            a = op.analyse(chain, op.lot_size(lots, symbol, e), r, now=max(now, ts) if ts else now, rate_source=src)
            out[a["expiry"]] = a
        return out
    except op.OptionsError as e:
        raise StrategyError(str(e))


def yahoo_symbol(symbol):
    return YAHOO_INDEX.get(symbol, symbol + ".NS")


def parse_history(text):
    """A Yahoo v8 chart answer -> {"closes", "times", "from", "to"} (None closes dropped)."""
    try:
        res = json.loads(text)["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StrategyError("the price history is not a Yahoo chart answer")
    ts = res.get("timestamp") or []
    cl = (((res.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    pairs = [(t, c) for t, c in zip(ts, cl) if c]
    if not pairs:
        return {"closes": [], "times": [], "from": None, "to": None}

    def day(t):
        return _dt.datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M")
    return {"closes": [c for _, c in pairs], "times": [t for t, _ in pairs], "from": day(pairs[0][0]),
            "to": day(pairs[-1][0])}


def _history_text(ysym, rng, interval, offline_dirs):
    name = "history-%s-%s.json" % (ysym, interval)
    for d in offline_dirs or []:
        for n in (name, name + ".gz"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                if n.endswith(".gz"):
                    with gzip.open(p, "rb") as fh:
                        return fh.read().decode("utf-8")
                with open(p, encoding="utf-8") as fh:
                    return fh.read()
    if offline_dirs:
        return None
    path = "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym), rng, interval)
    delay = 2.0
    for attempt in range(3):
        for host in YAHOO_HOSTS:
            try:
                req = urllib.request.Request(host + path, headers={"User-Agent": YAHOO_UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    return None
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        time.sleep(delay)
        delay *= 2
    return None


def load_history(symbol, offline_dirs=None, before=None):
    """Daily closes (1 year) and five-minute closes (5 days) of the underlying, up to `before`
    (naive IST). None when neither can be had."""
    ysym = yahoo_symbol(symbol)
    out = {"source": None}
    for interval, rng, key in (("1d", "1y", "daily"), ("5m", "5d", "intraday")):
        text = _history_text(ysym, rng, interval, offline_dirs)
        if not text:
            out[key] = []
            continue
        h = parse_history(text)
        closes = h["closes"]
        if before is not None:
            cut = before.replace(tzinfo=IST).timestamp()
            closes = [c for c, t in zip(h["closes"], h["times"]) if t <= cut]
        out[key] = closes
        if key == "daily":
            out["from"], out["to"] = h["from"], h["to"]
    if not out.get("daily"):
        return None
    out["source"] = "Yahoo Finance %s daily closes %s to %s" % (ysym, (out.get("from") or "")[:10],
                                                               (out.get("to") or "")[:10])
    return out


# ---------------------------------------------------------------- rendering

# ---------------------------------------------------------------- the broker's basket margin (shared block)
# The same block in trader-strategy and trader-risk (tests/test_broker_v2.py keeps them identical). Read only:
# the broker's own basket margin comes from trader-broker's `ticket.py margin-check`, kept for the day in
# trader/state/broker_margin.json and shown next to our SPAN-style estimate, with the difference.

BROKER_MARGIN_FILE = os.path.join("trader", "state", "broker_margin.json")
MARGIN_TOKENS = {"kite": "BT_KITE_ACCESS_TOKEN", "upstox": "BT_UPSTOX_ACCESS_TOKEN", "dhan": "BT_DHAN_ACCESS_TOKEN",
                 "angel": "BT_ANGEL_ACCESS_TOKEN"}


def margin_key(rows):
    """One basket, broker-neutral: rows of (side, underlying, expiry, strike, CE/PE/FUT, qty). The same function in
    trader-broker, trader-strategy and trader-risk, so each finds the broker's figure for its own legs."""
    parts = []
    for side, und, exp, strike, kind, qty in rows:
        kind = str(kind or "").upper()
        k = "-" if kind == "FUT" or strike in (None, "") else "%g" % float(strike)
        parts.append("%s|%s|%s|%s|%s|%d" % (str(side).upper(), str(und).upper(), str(exp)[:10], k, kind,
                                            int(round(abs(float(qty or 0))))))
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def margin_broker(cfg, env=None, want=None):
    """The live broker whose basket margin to read: the one asked for, else "margin_broker" in the config, else the
    config broker, and only when its token is in this shell. None otherwise: then nothing is fetched."""
    env = os.environ if env is None else env
    b = str(want or (cfg or {}).get("margin_broker") or (cfg or {}).get("broker") or "").strip().lower()
    b = {"zerodha": "kite", "angelone": "angel"}.get(b, b)
    return b if b in MARGIN_TOKENS and env.get(MARGIN_TOKENS[b]) else None


def broker_margin_lookup(key, ours, path=None, today=None):
    """Today's broker basket margin for this basket (trader/state/broker_margin.json), next to our estimate."""
    try:
        with open(path or BROKER_MARGIN_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    hit = None
    for c in (doc.get("checks") or []) if isinstance(doc, dict) else []:
        if c.get("key") == key and (today is None or c.get("date") == today) and c.get("total") is not None:
            hit = c
    if hit is None:
        return None
    total = float(hit["total"])
    out = {"broker": hit.get("broker"), "title": hit.get("title") or hit.get("broker"), "at": hit.get("at"),
           "total": round(total, 2), "hedge_benefit": hit.get("hedge_benefit"), "note": hit.get("note"),
           "our_estimate": round(float(ours), 2) if ours else None, "difference": None, "difference_pct": None,
           "read_only": True}
    if ours:
        d = total - float(ours)
        out["difference"] = round(d, 2)
        out["difference_pct"] = round(100.0 * d / float(ours), 1)
    return out


def broker_margin_fetch(legs, ours, broker, path=None, here=None, timeout=60):
    """Ask trader-broker (a sibling folder) for the broker's basket margin, read only: ticket.py margin-check in a
    child process, which saves it for broker_margin_lookup. -> (ok, words)."""
    import subprocess
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.abspath(here or __file__)))
    script = os.path.join(root, "trader-broker", "ticket.py")
    if not os.path.isfile(script):
        return False, "trader-broker is not installed next to this skill"
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(legs, fh)
        argv = [sys.executable, script, "margin-check", "--broker", broker, "--legs-json", tmp, "--cache",
                path or BROKER_MARGIN_FILE, "--json"]
        if ours:
            argv += ["--estimate", "%.2f" % float(ours)]
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return False, "could not run ticket.py margin-check (%s)" % e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        return False, lines[-1] if lines else "ticket.py margin-check failed"
    return True, "read"


def broker_margin_words(bm, money):
    """One plain line: the broker's basket margin next to our estimate."""
    if not bm:
        return None
    s = "Broker basket margin (%s, %s IST): %s" % (bm["title"], str(bm.get("at") or "")[11:16], money(bm["total"]))
    if bm.get("difference") is not None:
        s += " vs our SPAN-style estimate %s: %s%s (%+.1f%%)" % (
            money(bm["our_estimate"]), "+" if bm["difference"] >= 0 else "-", money(abs(bm["difference"])),
            bm["difference_pct"])
    return s + ". The broker's figure is what it blocks; read only, nothing was sent."

# ---------------------------------------------------------------- end of the shared block


def attach_broker_margin(doc, cfg, args, now):
    """The broker's own basket margin next to ours, read only: today's figure from ticket.py margin-check, or (when a
    broker token is in this shell) one fresh read-only call through trader-broker. Never changes our estimate."""
    m = doc.get("margin") or {}
    if getattr(args, "no_broker_margin", False) or not doc.get("legs"):
        return None
    rows = [(l["side"], doc["underlying"], l["expiry"], l.get("strike"),
             "FUT" if l["instrument"] == "FUT" else l.get("option_type"), l["qty"]) for l in doc["legs"]]
    key = margin_key(rows)
    path = getattr(args, "broker_margin_file", None) or BROKER_MARGIN_FILE
    today = now.date().isoformat()
    ours = m.get("required")
    bm = broker_margin_lookup(key, ours, path, today)
    want = getattr(args, "broker_margin", None)
    if bm is None:
        b = margin_broker(cfg, want=want)
        if b:
            ok, why = broker_margin_fetch(doc.get("orders") or draft_orders(doc), ours, b, path)
            bm = broker_margin_lookup(key, ours, path, today) if ok else None
            if not ok:
                m["broker_error"] = "could not read it from %s: %s" % (b, why)
        elif want:
            m["broker_error"] = "no %s token in this shell (eval \"$(python3 trader-broker/ticket.py login %s)\")" % (
                want, want)
    if bm:
        m["broker_basket"] = bm
        m["broker_note"] = broker_margin_words(bm, inr)
    return bm


def render(doc):
    L = []
    w = L.append
    w("%s on %s, expiry %s (%d days), chain of %s" % (doc["name"], doc["underlying"], ", ".join(
        nse_date(parse_date(e)) for e in doc["expiries"]), doc["days_to_expiry"], doc["asof"].replace("T", " ")[:16]))
    w("Spot %s | forward %s | ATM %s, IV %s%% | lot %s" % (
        fnum(doc["spot"]), fnum(doc["forward"]), strike_text(doc["atm"]) if doc["atm"] else "-",
        fnum(doc["atm_iv"]) if doc["atm_iv"] is not None else "-", doc["lot_size"]))
    for x in doc["warnings"]:
        if x.startswith("STT") or x.startswith("expiry day") or "DELIVERY" in x:
            w("WARNING: " + x)
    w("")
    w("Legs (premium per unit; qty in units)")
    for i, l in enumerate(doc["legs"], 1):
        if l["instrument"] == "FUT":
            w("  %d. %-4s %d lot FUT %s  @ %s (forward)  qty %s" % (
                i, l["side"], l["lots"], nse_date(parse_date(l["expiry"])), fnum(l["price"]), fnum(l["qty"], 0)))
            continue
        g = l["greeks"]
        w("  %d. %-4s %d lot %s %s %s  @ %s (%s)  IV %s%%  delta %s  qty %s" % (
            i, l["side"], l["lots"], strike_text(l["strike"]), l["option_type"], nse_date(parse_date(l["expiry"])),
            fnum(l["price"]), l["price_source"], fnum(l["iv"]), "%+.2f" % g["delta"] if g.get("delta") is not None
            else "-", fnum(l["qty"], 0)))
    p = doc["net_premium"]
    w("")
    w("Net premium: %s %s" % (inr(abs(p["amount"])), "received (credit)" if p["kind"] == "credit" else
                              "paid (debit)" if p["kind"] == "debit" else ""))
    if doc["spread_cost"]:
        w("Crossing the bid/ask on every leg would cost %s more." % inr(doc["spread_cost"]))
    e = doc["expiry"]
    w("At expiry: max profit %s, max loss %s%s" % (
        "unlimited" if e["unlimited_profit"] else money(e["max_profit"]),
        "unlimited" if e["unlimited_loss"] else money(e["max_loss"]) + (
            " (if %s went to 0)" % doc["underlying"] if e["max_loss_at"] == 0 else ""),
        "" if e["exact"] else " (approximate: two expiries)"))
    w("  after charges: max profit %s, max loss %s" % (
        "unlimited" if e["unlimited_profit"] else money(e["max_profit_after_charges"]),
        "unlimited" if e["unlimited_loss"] else money(e["max_loss_after_charges"])))
    w("Breakevens at expiry: %s; after charges: %s" % (
        ", ".join(fnum(b) for b in e["breakevens"]) or "none",
        ", ".join(fnum(b) for b in e["breakevens_after_charges"]) or "none"))
    shifts = [s for s in e["breakeven_shift"] if s["shift"] is not None]
    if shifts:
        w("  charges move each breakeven by %s points" % ", ".join("%+.2f" % s["shift"] for s in shifts))
    w("Charges if held to expiry with the underlying at the forward: %s" % inr(doc["charges"]["at_forward_expiry"]))
    g = doc["greeks"]
    w("Net Greeks: delta %s units (%s lots), gamma %s, theta %s a day, vega %s a vol point" % (
        fnum(g["delta"]), fnum(g["delta_lots"], 3) if g["delta_lots"] is not None else "-",
        fnum(g["gamma"], 4), inr(g["theta"]), inr(g["vega"])))
    m = doc["margin"]
    if m["method"] == "premium":
        w("Margin required: %s, the premium paid up front (exact)." % inr(m["required"]))
    else:
        w("Margin required (estimate, +/-%d%%): about %s (%s to %s): scan risk %s, exposure %s%s, premium paid %s." % (
            m["error_pct"], inr(m["required"]), inr(m["low"]), inr(m["high"]), inr(m["span_style"]["scan_risk"]),
            inr(m["span_style"]["exposure"]), (", expiry-day ELM %s" % inr(m["span_style"]["expiry_day_elm"]))
            if m["span_style"]["expiry_day_elm"] else "", inr(m["span_style"]["premium_paid"])))
    w("Max loss at expiry: %s%s%s" % (
        "unlimited" if m["max_loss_unlimited"] else inr(m["max_loss"]), "" if m["method"] == "premium" else
        " (not the margin)",
        "; return on margin %s%% (max profit / margin required)" % fnum(m["return_on_margin"])
        if m["return_on_margin"] is not None else ""))
    if m.get("broker_basket"):
        w(broker_margin_words(m["broker_basket"], inr))
    elif m.get("broker_error"):
        w("Broker basket margin: %s." % m["broker_error"])
    pr = doc["probability"]
    t = pr["textbook"]
    w("")
    w("Probability of profit after charges, at %s:" % pr["horizon"])
    w("  textbook (lognormal at IV %s%%): %s%%, expected value %s, 5%% tail %s (worst 5%% average %s)" % (
        fnum(t["sigma"]), fnum(t["pop"]), inr(t["ev"]), inr(t["tail_5pct"]), inr(t["tail_5pct_avg"])))
    s = pr["simulated"]
    if s:
        w("  simulated (bootstrap of %s's own returns, realised vol %s%%): %s%%, expected value %s, 5%% tail %s "
          "(worst 5%% average %s)" % (doc["underlying"], fnum(s["realised_vol"]), fnum(s["pop"]), inr(s["ev"]),
                                      inr(s["tail_5pct"]), inr(s["tail_5pct_avg"])))
        for sc in pr["scenarios"]:
            w("    returns scaled to IV %+.0f points (%s%%): PoP %s%%, EV %s, 5%% tail %s" % (
                sc["iv_shift"], fnum(sc["vol"]), fnum(sc["pop"]), inr(sc["ev"]), inr(sc["tail_5pct"])))
    else:
        w("  simulated: not run, %s" % pr.get("simulated_skipped", "no price history"))
    for x in doc["warnings"]:
        if not (x.startswith("STT") or x.startswith("expiry day") or "DELIVERY" in x):
            w("Note: " + x)
    for x in doc["notes"]:
        w("Note: " + x)
    w("")
    w("Probabilities are models, not promises. Education and tooling, not financial advice. Nothing was placed.")
    return "\n".join(L) + "\n"


def render_list():
    L = ["Strategy templates (strategy.py build NAME SYMBOL ...):", ""]
    for t in list_templates():
        L.append("  %-18s %-7s %s" % (t["name"], t["kind"], t["about"]))
    L += ["", "Custom legs: strategy.py price SYMBOL --legs \"-1 CE ATM+200, +1 CE ATM+300\"",
          "  strike: 23000 | ATM | ATM+200 | D25 (delta 0.25) | P50 (premium 50); @01-Dec-2026 for another expiry;",
          "  =120.5 to use your own premium."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- command line

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="the strategy templates")

    def common(p):
        p.add_argument("--expiry", help="DD-Mon-YYYY from trader-options' expiries (default: the nearest)")
        p.add_argument("--far-expiry", help="the second expiry of a calendar")
        p.add_argument("--options", action="append", metavar="FILE",
                       help="trader-options JSON (repeat for a second expiry); default trader/options.json if it fits")
        p.add_argument("--fill", choices=("mid", "touch"), default="mid",
                       help="mid: the chain's price (default); touch: buy at the ask, sell at the bid")
        p.add_argument("--at", help="curve horizon: today, expiry (default) or a date / 'YYYY-MM-DD HH:MM'")
        p.add_argument("--iv-shift", type=float, default=0.0, help="vol points added to every leg's IV")
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--paths", type=int, default=DEFAULT_PATHS)
        p.add_argument("--no-sim", action="store_true", help="skip the simulated probability (no history fetch)")
        p.add_argument("--scan-pct", type=float, help="SPAN-style price scan, %% of the forward")
        p.add_argument("--vol-scan", type=float, help="SPAN-style vol scan, vol points")
        p.add_argument("--elm-pct", type=float, help="exposure margin, %% of notional")
        p.add_argument("--rate", type=float, help="risk-free rate for a fetched chain (default: config)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--product", default="NRML", help="NRML (default) or MIS for --orders")
        p.add_argument("--json", action="store_true")
        p.add_argument("--broker-margin", metavar="BROKER", type=str.lower,
                       choices=("kite", "zerodha", "upstox", "dhan", "angel", "angelone"),
                       help="also show this broker's own basket margin (read only; needs its token in this shell; "
                            "default: the config broker when its token is set)")
        p.add_argument("--no-broker-margin", action="store_true", help="do not read the broker's basket margin")
        p.add_argument("--broker-margin-file", default=BROKER_MARGIN_FILE, help=argparse.SUPPRESS)
        p.add_argument("--write", nargs="?", const="trader/strategy.json", metavar="PATH",
                       help="save the strategy to trader/strategy.json")
        p.add_argument("--orders", nargs="?", const="trader/orders.json", metavar="PATH",
                       help="write the legs as a PROPOSAL to trader/orders.json (trader-risk, then a typed yes)")
        p.add_argument("--replace", action="store_true", help="let --orders replace other proposals in that file")
        p.add_argument("--persona", help="with --orders: tag the legs with the agent persona drafting them, e.g. "
                                         "options-seller (a note for the persona scorecard, never sent)")
        p.add_argument("--now", help="'YYYY-MM-DD HH:MM' IST (default: now)")
        p.add_argument("--offline", action="append", metavar="DIR", help="read saved chains and history from DIR")
        p.add_argument("--save", metavar="DIR", help="save fetched chains to DIR")

    b = sub.add_parser("build", help="build a template")
    b.add_argument("template")
    b.add_argument("symbol")
    for flag, t, h in (("--strike", float, "centre or long strike (default ATM)"),
                       ("--wings", float, "points from ATM to the short strikes (default the expected move)"),
                       ("--width", float, "points from a short strike to its wing (default 2 strikes)"),
                       ("--delta", float, "pick short strikes by delta, e.g. 0.16"),
                       ("--skip", float, "extra points on one butterfly wing (broken wing)"),
                       ("--ratio", int, "short options per long in a ratio spread (default 2)"),
                       ("--lots", int, "lots per leg (default 1)")):
        b.add_argument(flag, type=t, help=h)
    b.add_argument("--type", help="CE or PE for a calendar")
    common(b)
    pr = sub.add_parser("price", help="price custom legs")
    pr.add_argument("symbol", nargs="?")
    pr.add_argument("--legs", required=True, help="'-1 CE ATM+200, +1 CE ATM+300 @01-Dec-2026, -1 PE D25 =41.5'")
    common(pr)
    return ap


def _symbol(s):
    sym = (s or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    sym = {"NIFTY50": "NIFTY", "NIFTY 50": "NIFTY", "BANK NIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY"}.get(sym, sym)
    if not SYMBOL_RE.match(sym):
        raise StrategyError("%r is not an NSE F&O symbol (e.g. NIFTY, BANKNIFTY, RELIANCE)" % s)
    return sym


def read_config(path):
    if not path or not os.path.isfile(path):
        return {}
    doc = load_json(path)
    return doc if isinstance(doc, dict) else {}


def write_json(path, payload):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def write_orders(path, doc, product="NRML", replace=False, persona=None):
    """Write the proposal; an existing file with other orders is kept unless replace is True.
    persona: an optional agent persona tag (see the persona tag block): logged for its scorecard."""
    if os.path.isfile(path) and not replace:
        try:
            old = load_json(path)
        except StrategyError:
            old = None
        orders = old.get("orders") if isinstance(old, dict) else old
        if orders and not (isinstance(old, dict) and old.get("created_by") == "trader-strategy"):
            raise StrategyError("%s already holds %d other proposed order(s); pass --replace to overwrite them"
                                % (path, len(orders)))
    try:
        persona = persona_name(persona)
    except ValueError as e:
        raise StrategyError(str(e))
    body = orders_document(doc, product)
    if persona:
        tag_orders(body["orders"], persona)
    write_json(path, body)
    if persona:
        log_persona_orders(path, persona, body["orders"], body["source"], body["created_at"])
    return body


def gather_markets(sym, args, now, wanted):
    """The chains the command needs: --options files, trader/options.json, else trader-options.
    wanted: expiries (DD-Mon-YYYY or ISO; the first may be None = the nearest). -> (markets, near)."""
    want_iso = []
    for w in wanted:
        d = parse_date(w) if w else None
        if w and d is None:
            raise StrategyError("expiry %r is not a date (DD-Mon-YYYY)" % w)
        iso = d.isoformat() if d else None
        if iso not in want_iso or iso is None:
            want_iso.append(iso)
    markets = {}
    files = list(args.options or [])
    if not files and os.path.isfile("trader/options.json"):
        files = ["trader/options.json"]
    for f in files:
        a = check_options_doc(load_json(f), f)
        if a["underlying"] == sym:
            markets[a["expiry"]] = a
    if args.options and not markets:
        raise StrategyError("--options holds no %s chain" % sym)
    need = []
    for i, w in enumerate(want_iso):
        if w is None and i == 0:
            if not markets:
                need.append(None)
        elif w not in markets:
            need.append(w)
    if need:
        if args.options:
            raise StrategyError("no chain for %s in --options; run trader-options chain %s --expiry ... --write" % (
                ", ".join(nse_date(parse_date(x)) for x in need if x) or "the nearest expiry", sym))
        off = None
        for d in args.offline or []:
            if os.path.isfile(os.path.join(d, "contract-info-%s.json" % sym)):
                off = d
                break
        if args.offline and off is None:
            raise StrategyError("offline: no contract-info-%s.json in %s" % (sym, ", ".join(args.offline)))
        markets.update(fetch_markets(sym, need, off, args.save, now, args.rate, args.config))
    near = want_iso[0] if want_iso[0] in markets else min(markets)
    return markets, near


def run(args):
    now = parse_time(args.now) if args.now else now_ist()
    if args.now and now is None:
        raise StrategyError("--now is 'YYYY-MM-DD HH:MM'")
    if args.cmd == "price" and not args.symbol:
        if os.path.isfile("trader/options.json"):
            args.symbol = load_json("trader/options.json").get("underlying")
        if not args.symbol:
            raise StrategyError("which underlying? strategy.py price NIFTY --legs ...")
    sym = _symbol(args.symbol)
    wanted = [args.expiry]
    if args.cmd == "build":
        if template_name(args.template) == "calendar" and not args.far_expiry:
            raise StrategyError("a calendar needs --far-expiry (and --expiry for the near one)")
        if args.far_expiry:
            wanted.append(args.far_expiry)
    else:
        wanted += sorted({s["expiry"] for s in parse_legs(args.legs) if s["expiry"] not in ("near", "far")})
    markets, near = gather_markets(sym, args, now, wanted)
    cfg = read_config(args.config)
    if args.cmd == "build":
        name = template_name(args.template)
        params = {k: getattr(args, k) for k in ("strike", "wings", "width", "delta", "skip", "ratio", "lots", "type")
                  if getattr(args, k) is not None}
        specs = template_specs(name, params, markets[near])
    else:
        name, params = "custom", {"legs": args.legs}
        specs = parse_legs(args.legs)
    legs = resolve_legs(specs, markets, near, args.fill)
    history = None
    if not args.no_sim:
        asof = parse_time(markets[near].get("asof"))
        history = load_history(sym, args.offline, asof)
    scan = {"scan_pct": args.scan_pct, "vol_scan": args.vol_scan, "elm_pct": args.elm_pct}
    doc = evaluate(legs, markets, cfg, name, params, args.at, args.iv_shift, history, args.seed, args.paths,
                   scan, now, not args.no_sim)
    doc["generated_at"] = now.isoformat(timespec="seconds")
    doc["orders"] = draft_orders(doc, args.product)
    attach_broker_margin(doc, cfg, args, now)
    if args.write:
        write_json(args.write, doc)
    if args.orders:
        write_orders(args.orders, doc, args.product, args.replace, args.persona)
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(doc))
        if args.write:
            print("Wrote %s." % args.write)
        if args.orders:
            print("Wrote %d proposed order(s) to %s: run trader-risk next; nothing is placed without your typed yes."
                  % (len(doc["orders"]), args.orders))
    return 0


def utf8_console():
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
    if args.cmd == "list":
        sys.stdout.write(render_list())
        return 0
    try:
        return run(args)
    except StrategyError as exc:
        print("strategy: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
