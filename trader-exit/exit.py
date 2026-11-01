#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. NSE cash market, prices in rupees, times
in IST. Standard library only. Long side only. It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write
    python3 exit.py plan --symbol HDFCBANK --entry 692.70 --or-high 692.00 --or-low 687.85
    python3 exit.py plan --stop atr:1.5 --offline HDFCBANK.json   # ATR needs the session's bars
    python3 exit.py plan --symbol RELIANCE --entry 1452.30 --setup level --stop pct:1 --tick 0.10
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round to the NEAREST tick, half up):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

The tick is "tick_size" in trader/config.json ("auto": by NSE price band) or --tick.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under the larger of 0.05% of the price and two ticks (too tight to
survive the spread), an entry.json that has not triggered (unless you give
--entry), a short (longs only), a day NSE is shut.

flat_by (default 15:10 IST, or "flat_by" in the config): out by then, win or
lose. It is never later than 5 minutes before continuous trading ends for the
stock (an F&O stock ends at 15:15 from 2026-08-03 and 15:30 from 2026-12-07,
then the Closing Auction Session; see SESSION_RULES) or before the MIS
square-off of "broker_profile" (zerodha, upstox, dhan, angel, groww, custom).
A later one is pulled in, with a note. On a special session (Muhurat
trading, from "special_sessions" in the config) it moves to 5 minutes before
that session's close.

Charges: with "include_charges" (default true) it estimates the round-trip
charges (brokerage, STT, exchange fee, SEBI fee, stamp duty, GST) for the
quantity you give with --qty, or the most your max loss per trade allows
(capped by max_order_value), and shows reward:risk gross and net of them.
The rates live in config "charges": check them against your broker.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import copy
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; bharat-trader/0.2; +https://github.com/rajdavee/bharat-trader)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
MIN_RISK_TICKS = 2                 # and never less than two ticks
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

DEFAULTS = {
    "capital": 100000, "risk_per_trade_pct": 1.0, "max_loss_per_trade": 1000, "max_daily_loss": 3000,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "max_position_pct": 50, "max_order_value": 50000,
    "min_order_value": 500, "cash_buffer_pct": 10, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10", "exchange": "NSE",
    "product": "MIS", "tick_size": "auto", "broker": "paper", "paper": True, "blocked": [], "allow_short": False,
    "allow_fno": False, "include_charges": True,
    "charges": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
                "exchange_txn_pct": 0.00307, "sebi_per_crore": 10, "stamp_buy_pct": 0.003,
                "stamp_delivery_pct": 0.015, "gst_pct": 18},
    "extra_holidays": [], "special_sessions": [],
}

# NSE Capital Market trading holidays, from NSE's holiday master. 2027 is not published yet.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading timing announced separately by NSE)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)


# ---------------------------------------------------------------- dated market rules (same in entry, exit, alerts, risk, timemachine, scanner, broker, lab)
# Every row holds from its effective_from date (IST) until the next row starts.
# Times are IST. Segments: "EQ" a cash-market stock with no derivatives, "EQ_FNO"
# a cash-market stock in NSE's F&O list (the Closing Auction Session applies to
# it from 2026-08-03), "DERIV" futures and options. The sources are in
# docs/data-sources.md; when NSE changes a timing, add a row, never edit one.

SESSION_RULES = [
    {"effective_from": "2000-01-01",
     "pre_open": {"start": "09:00", "market_orders_until": "09:08", "random_close": ["09:07", "09:08"],
                  "matching": ["09:08", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:30", "DERIV": "15:30"},
     "cas": None,
     "post_close": ["15:40", "16:00"],
     "source": "NSE/CMTR/74969 (1 Jul 2026, 'current' pre-open times); NSE/FAOP/74467 (29 May 2026, "
               "derivatives close moved from 15:30)"},
    {"effective_from": "2026-08-03",
     "pre_open": {"start": "09:00", "market_orders_until": "09:08", "random_close": ["09:07", "09:08"],
                  "matching": ["09:08", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:15", "DERIV": "15:40"},
     "cas": {"start": "15:15", "orders_from": "15:20", "limit_only_from": "15:25", "random_close": ["15:28", "15:30"],
             "end": "15:35"},
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular 16 Jan 2026 para 4.2 and 6.1 (NSE/CMTR/72394); NSE/FAOP/74467; go-live NSE/CMTR/75479 "
               "and NSE/FAOP/75472 (30 Jul 2026)"},
    {"effective_from": "2026-09-07",
     "pre_open": {"start": "09:00", "market_orders_until": "09:05", "random_close": ["09:08", "09:10"],
                  "matching": ["09:10", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:15", "DERIV": "15:40"},
     "cas": {"start": "15:15", "orders_from": "15:20", "limit_only_from": "15:25", "random_close": ["15:28", "15:30"],
             "end": "15:35"},
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular 16 Jan 2026 para 5 and 6.2 (NSE/CMTR/72394); NSE/CMTR/74969 (1 Jul 2026); "
               "NSE/CMTR/76187 (4 Sep 2026)"},
    {"effective_from": "2026-12-07",
     "pre_open": {"start": "09:00", "market_orders_until": "09:05", "random_close": ["09:08", "09:10"],
                  "matching": ["09:10", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:30", "DERIV": "15:45"},
     "cas": {"start": "15:30", "orders_from": "15:31", "limit_only_from": "15:36", "random_close": ["15:38", "15:40"],
             "end": "15:40"},
     "expiring_index_derivatives_end": "15:30",
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/23843/2026 (15 Oct 2026) para 4.2 and 6.1; "
               "Zerodha bulletin 16 Oct 2026"},
]

# Brokers' MIS auto square-off, IST, by segment. A date before a broker's first
# row uses that first row. Brokers move these without much notice: check yours.
BROKER_SQUARE_OFF = {
    "zerodha": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:25", "DERIV": "15:26",
         "source": "Zerodha product update 10 Aug 2026 (CAS stocks were 15:10)"},
        {"effective_from": "2026-08-10", "EQ_FNO": "15:12", "EQ": "15:25", "DERIV": "15:26",
         "source": "support.zerodha.com intraday-auto-square-off-timings (read 1 Nov 2026)"}],
    "upstox": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:05", "EQ": "15:20", "DERIV": "15:20",
         "source": "upstox.com announcement 4 Sep 2026 (old times)"},
        {"effective_from": "2026-09-11", "EQ_FNO": "15:10", "EQ": "15:25", "DERIV": "15:25",
         "source": "upstox.com announcement 4 Sep 2026, effective 11 Sep 2026"}],
    "dhan": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:10", "DERIV": "15:25",
         "source": "dhan.co support: what-are-the-dhan-intraday-auto-square-off-timings (read 1 Nov 2026)"}],
    "angel": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:10", "DERIV": "15:20",
         "source": "smartapi.angelone.in forum post 19209 (CAS changes from 3 Aug 2026)"}],
    "groww": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:05", "EQ": "15:20", "DERIV": "15:20",
         "source": "groww.in/p/risk-policy (read 1 Nov 2026)"}],
}
BROKER_PROFILES = tuple(sorted(BROKER_SQUARE_OFF)) + ("custom",)
SEGMENTS = ("EQ", "EQ_FNO", "DERIV")
RULES_DEFAULT_FLAT_BY = "15:10"   # the config's default flat_by
SQUARE_OFF_MARGIN_MIN = 5    # flat by at least this long before the broker's square-off
ENTRY_LEAD_MIN = 15          # and stop new entries this long before the flat-by
FNO_LIST_URL = "https://www.nseindia.com/api/master-quote"
FNO_LIST_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
                                  "Gecko) Chrome/130 Safari/537.36",
                    "Accept": "application/json", "Referer": "https://www.nseindia.com/"}


class RulesError(ValueError):
    pass


def _rule_time(text, what="time"):
    try:
        h, m = str(text).strip().split(":")
        return datetime.time(int(h), int(m))
    except (ValueError, TypeError):
        raise RulesError("%s wants an IST time like 15:10, not %r" % (what, text))


def _rule_day(d):
    return d if isinstance(d, str) else d.isoformat()


def _rule_minus(t, minutes):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 3), t) - datetime.timedelta(minutes=minutes)
    return dt.time()


def _dated_row(rows, d):
    day = _rule_day(d)
    pick = rows[0]
    for row in rows:
        if row["effective_from"] <= day:
            pick = row
    return pick


def session_for(d, segment="EQ"):
    """The NSE session rules that hold on date d for one segment, as datetime.time values:
    pre_open (start, order entry end), open, continuous_end (the last continuous-trading
    minute), close (when the segment's day is over: the auction's end for EQ_FNO under CAS),
    cas (dict or None), post_close (start, end), rule_from and source."""
    seg = str(segment or "EQ").upper()
    if seg not in SEGMENTS:
        raise RulesError("segment is one of %s, not %r" % (", ".join(SEGMENTS), segment))
    row = _dated_row(SESSION_RULES, d)
    cts_end = _rule_time(row["continuous_end"][seg])
    cas = None
    if seg == "EQ_FNO" and row.get("cas"):
        cas = {k: ([_rule_time(x) for x in v] if isinstance(v, list) else _rule_time(v))
               for k, v in row["cas"].items()}
    po = row["pre_open"]
    return {"date": _rule_day(d), "segment": seg, "rule_from": row["effective_from"], "source": row["source"],
            "pre_open": (_rule_time(po["start"]), _rule_time(po["random_close"][1])),
            "pre_open_market_orders_until": _rule_time(po["market_orders_until"]),
            "open": _rule_time(row["open"]), "continuous_end": cts_end,
            "close": cas["end"] if cas else cts_end, "cas": cas,
            "post_close": tuple(_rule_time(x) for x in row["post_close"])}


def segment_of(symbol, fno_stocks):
    """EQ_FNO when the stock is in the F&O list. With no list at all, EQ_FNO too:
    its earlier close is the safe side to be wrong on."""
    if fno_stocks is None:
        return "EQ_FNO"
    return "EQ_FNO" if str(symbol or "").strip().upper() in fno_stocks else "EQ"


def square_off_for(d, segment, cfg):
    """(time, "zerodha: source") of the broker's MIS auto square-off, or (None, "") with no profile."""
    cfg = cfg or {}
    name = str(cfg.get("broker_profile") or "").strip().lower()
    if not name:
        return None, ""
    seg = str(segment or "EQ").upper()
    if name == "custom":
        custom = cfg.get("square_off") or {}
        if not isinstance(custom, dict) or not custom.get(seg):
            raise RulesError('broker_profile "custom" needs "square_off": {"EQ": "15:20", "EQ_FNO": "15:10", '
                             '"DERIV": "15:25"} (your broker\'s MIS square-off times, IST)')
        return _rule_time(custom[seg], "square_off %s" % seg), "custom: square_off in your config"
    if name not in BROKER_SQUARE_OFF:
        raise RulesError("broker_profile is one of %s, not %r" % (", ".join(BROKER_PROFILES), name))
    row = _dated_row(BROKER_SQUARE_OFF[name], d)
    return _rule_time(row[seg]), "%s: %s" % (name, row["source"])


def safe_flat_by(d, segment, cfg):
    """(flat_by time, note). The latest safe time is the earlier of continuous trading's
    end and the broker's square-off, each less the margin (5 minutes). flat_by "auto"
    takes it; flat_by (15:10 when missing) later than it is pulled in, with a note."""
    cfg = cfg or {}
    margin = int(cfg.get("square_off_margin_min", SQUARE_OFF_MARGIN_MIN) or 0)
    s = session_for(d, segment)
    safe, why = _rule_minus(s["continuous_end"], margin), "continuous trading ends %s" % s["continuous_end"].strftime("%H:%M")
    sq, src = square_off_for(d, segment, cfg)
    if sq is not None and _rule_minus(sq, margin) < safe:
        safe, why = _rule_minus(sq, margin), "%s squares off MIS at %s" % (src.split(":")[0], sq.strftime("%H:%M"))
    want = cfg.get("flat_by") or RULES_DEFAULT_FLAT_BY
    if want == "auto":
        return safe, "%s (%d min before: %s)" % (safe.strftime("%H:%M"), margin, why)
    t = _rule_time(want, "flat_by")
    if t > safe:
        return safe, "flat_by %s is too late on %s for this stock: %s, so flat by %s" % (
            t.strftime("%H:%M"), _rule_day(d), why, safe.strftime("%H:%M"))
    return t, ""


def entry_cutoff(d, segment, cfg):
    """The last time for a new entry: no_entries_after, but at least ENTRY_LEAD_MIN before the safe flat-by."""
    cfg = cfg or {}
    flat, _ = safe_flat_by(d, segment, cfg)
    late = _rule_minus(flat, ENTRY_LEAD_MIN)
    want = cfg.get("no_entries_after")
    t = _rule_time(want, "no_entries_after") if want else late
    return min(t, late)


def parse_fno_list(text):
    """NSE master-quote JSON (a list of F&O underlying symbols) -> set of symbols."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise RulesError("the NSE F&O list is not JSON")
    if isinstance(doc, dict):
        doc = doc.get("symbols") or doc.get("data") or []
    syms = {str(x).strip().upper() for x in doc if isinstance(x, str) and x.strip()}
    if len(syms) < 50:
        raise RulesError("the NSE F&O list has only %d symbols: not trusting it" % len(syms))
    return syms


def fetch_fno_stocks(timeout=20):
    """NSE's F&O underlying list, live. One polite request; raises RulesError on failure."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(FNO_LIST_URL, headers=FNO_LIST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_fno_list(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as e:
        raise RulesError("could not fetch the NSE F&O list (%s)" % (getattr(e, "reason", None) or e))


def load_fno_stocks(cfg=None, cache_path=None, fetch=False, today=None):
    """(set of F&O stock symbols or None, note). The config's "fno_stocks" list wins.
    Else the cache (trader/fno_stocks.json), refreshed from NSE at most once a day when
    fetch is True. None means unknown: callers then treat every stock as EQ_FNO."""
    cfg = cfg or {}
    own = cfg.get("fno_stocks")
    if isinstance(own, list) and own:
        return {str(x).strip().upper() for x in own}, "F&O list from fno_stocks in your config"
    today = _rule_day(today or datetime.date.today())
    cached, fetched = None, None
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            cached, fetched = {str(x).upper() for x in doc.get("symbols") or []} or None, doc.get("fetched")
        except (OSError, ValueError, AttributeError):
            cached = None
    if fetch and (cached is None or fetched != today):
        try:
            syms = fetch_fno_stocks()
        except RulesError as e:
            if cached is not None:
                return cached, "F&O list cached %s (%s)" % (fetched, e)
            return None, "%s: treating every stock as an F&O stock (earlier close)" % e
        if cache_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump({"source": FNO_LIST_URL, "fetched": today, "symbols": sorted(syms)}, fh, indent=0)
                    fh.write("\n")
            except OSError:
                pass
        return syms, "F&O list from NSE, %s (%d stocks)" % (today, len(syms))
    if cached is not None:
        return cached, "F&O list cached %s (%d stocks)" % (fetched, len(cached))
    return None, "no F&O list (trader/fno_stocks.json): treating every stock as an F&O stock (earlier close)"

# ---------------------------------------------------------------- end of dated market rules


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- money and numbers

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


def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().replace("₹", "").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def paise(x):
    """Round half up to the paisa."""
    return D(x).quantize(PAISE, rounding=ROUND_HALF_UP)


# NSE sets each stock's tick from its price band and reviews it periodically.
# "auto" rounds to the coarsest tick the band can have: a multiple of it is a
# valid price on any finer grid too. A number in tick_size overrides it.
TICK_BANDS = ((1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))


def tick_for(price, setting="auto"):
    """The tick to round a price of this size to."""
    if setting not in (None, "", "auto"):
        return float(setting)
    for top, tick in TICK_BANDS:
        if float(price) < top:
            return tick
    return 5.00


def tick_floor(x, tick):
    """Round DOWN to a multiple of the tick (a long's stop: away from the entry)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(PAISE)


def tick_ceil(x, tick):
    return ((D(x) / tick).to_integral_value(rounding=ROUND_CEILING) * tick).quantize(PAISE)


def tick_round(x, tick):
    """Round to the NEAREST multiple of the tick, half up (targets, limits, entries)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick).quantize(PAISE)


def clean_price(x, tick):
    """A Yahoo float (692.7000122070312) to the exact tick it stands for."""
    return tick_round(paise(x), tick)


def exact(x):
    """A raw rupee amount, Indian grouping, at least two decimals: 689.925 -> '₹689.925', 1163.35 -> '₹1,163.35'."""
    d = D(x)
    s = format(d.normalize(), "f")
    places = len(s.split(".")[1]) if "." in s else 0
    if places <= 2:
        return inr(d)
    head = inr(d.quantize(Decimal(1), rounding=ROUND_FLOOR) if d >= 0 else d.quantize(Decimal(1)))
    return head[:-3] + "." + s.split(".")[1]


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def round_trip_charges(entry, exit, qty, cfg, segment=None, on=None, exercised=False):
    """Estimated charges for buying qty at entry and selling at exit, in rupees, to the paisa.

    Intraday (product MIS): brokerage on each order (brokerage_pct of its value, capped at
    brokerage_cap), STT on the sell side (stt_sell_pct), exchange transaction charge
    (exchange_txn_pct of the turnover), SEBI fee (sebi_per_crore per Rs 1 crore of turnover),
    stamp duty on the buy side (stamp_buy_pct), GST (gst_pct) on brokerage + exchange + SEBI.
    Delivery (CNC): STT stt_delivery_pct on both sides, stamp stamp_delivery_pct on the buy.
    Each part is rounded half up to the paisa from its unrounded value; total = sum of the parts."""
    seg = str(segment or (cfg.get("segment") if isinstance(cfg, dict) else None) or "EQ").upper()
    if seg != "EQ":
        return fno_round_trip_charges(entry, exit, qty, cfg, seg, on, exercised)
    ch = dict(DEFAULTS["charges"])
    ch.update((cfg or {}).get("charges") or {})
    pct = Decimal("0.01")
    q = D(qty, "qty")
    buy, sell = D(entry) * q, D(exit) * q
    turnover = buy + sell
    cap = D(ch["brokerage_cap"])
    bp = D(ch["brokerage_pct"]) * pct
    brokerage = min(buy * bp, cap) + min(sell * bp, cap)
    delivery = str((cfg or {}).get("product") or "MIS").upper() == "CNC"
    if delivery:
        stt = turnover * D(ch["stt_delivery_pct"]) * pct
        stamp = buy * D(ch["stamp_delivery_pct"]) * pct
    else:
        stt = sell * D(ch["stt_sell_pct"]) * pct
        stamp = buy * D(ch["stamp_buy_pct"]) * pct
    exchange = turnover * D(ch["exchange_txn_pct"]) * pct
    sebi = turnover * D(ch["sebi_per_crore"]) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * D(ch["gst_pct"]) * pct
    out = {"brokerage": paise(brokerage), "stt": paise(stt), "exchange": paise(exchange), "sebi": paise(sebi),
           "stamp": paise(stamp), "gst": paise(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}


# ---------------------------------------------------------------- F&O charges, dated (same in budget, risk, broker, exit, timemachine, lab)
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


# ---------------------------------------------------------------- time (IST, fixed +05:30, no DST)

def ist_now():
    return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)


def ist_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)


def parse_now(text):
    """--now -> naive IST. A time without an offset is IST."""
    if not text:
        return ist_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (ist_now().date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-10-26 10:10" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants a 24-hour IST time like 15:10, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:10, 14:35 (24-hour IST), or 2:35 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a 24-hour IST time like 10:10, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:10, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:10, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def market_day(d, cfg=None, segment="EQ"):
    """What NSE does on day d: {"open": bool, "start", "end" (times), "kind", "reason", "warning"}.
    On a regular day "end" is the end of continuous trading for the segment (an F&O
    stock stops at 15:15 from 2026-08-03 to 2026-12-06; see SESSION_RULES).
    A special session in the config (Muhurat trading) wins over a weekend or holiday."""
    cfg = cfg or {}
    iso = d.isoformat()
    for s in cfg.get("special_sessions") or []:
        if isinstance(s, dict) and str(s.get("date")) == iso:
            start = parse_hhmm(s.get("open"), "special_sessions open")
            end = parse_hhmm(s.get("close"), "special_sessions close")
            if end <= start:
                raise PlanError("special session %s closes before it opens" % iso)
            return {"open": True, "start": start, "end": end, "kind": "special",
                    "reason": "special session %s to %s IST" % (hm(start), hm(end)), "warning": None}
    if d.weekday() >= 5:
        return {"open": False, "kind": "closed", "reason": "a %s" % d.strftime("%A"), "warning": None}
    extra = set(str(x) for x in (cfg.get("extra_holidays") or []))
    if iso in NSE_HOLIDAYS or iso in extra:
        return {"open": False, "kind": "closed", "reason": "an NSE holiday (%s)" % NSE_HOLIDAYS.get(iso, "extra_holidays"),
                "warning": None}
    warning = None
    if d.year not in CALENDAR_YEARS:
        warning = ("the NSE holiday calendar for %d is unknown here: treating %s as a normal trading day. "
                   "Add holidays to extra_holidays in the config" % (d.year, iso))
    s = session_for(d, segment)
    return {"open": True, "start": s["open"], "end": s["continuous_end"], "kind": "regular", "reason": "",
            "warning": warning, "segment": s["segment"], "cas": s["cas"]}


# ---------------------------------------------------------------- stop and target rules

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=Decimal("0.05")):
    """The stop, the target and the risk, exact to the tick. Raises PlanError with the reason."""
    tick = D(tick, "tick_size")
    if tick <= 0:
        raise PlanError("tick_size must be above 0")
    e = clean_price(entry, tick)
    if e <= 0:
        raise PlanError("the entry price must be above ₹0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = clean_price(or_high, tick), clean_price(or_low, tick)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (inr(oh), inr(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (inr(oh), inr(ol), exact(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (inr(e), num(stop_param), exact(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = tick_floor(raw, tick)
    if stop != D(raw):
        what = "rounded down to the %s tick" % inr(tick)
        detail = (detail + ", " + what if detail else "%s %s" % (exact(raw), what))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (inr(stop), label, inr(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % inr(stop))
    risk = e - stop
    floor_risk = max(e * MIN_RISK_PCT / 100, tick * MIN_RISK_TICKS)
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than the minimum %s (0.05%% of the price or %d ticks of "
                        "%s, whichever is larger): too tight to survive the spread, normal wiggle would stop you "
                        "out. Widen it." % (inr(risk), inr(floor_risk.quantize(PAISE, rounding=ROUND_CEILING)),
                                            MIN_RISK_TICKS, inr(tick)))
    if target_rule == "r":
        target = tick_round(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = tick_round(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (inr(target), inr(e)))
    rr = ((target - e) / risk).quantize(PAISE, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick}


def max_loss_of(cfg):
    """max_loss_per_trade if the config sets it, else capital x risk_per_trade_pct / 100."""
    if cfg.get("_explicit_max_loss"):
        return D(cfg["max_loss_per_trade"])
    return D(cfg["capital"]) * D(cfg["risk_per_trade_pct"]) / 100


def estimate_qty(entry, risk, cfg):
    """The most shares the max loss per trade allows at this risk, capped by max_order_value. At least 1."""
    q = int(max_loss_of(cfg) / D(risk))
    mov = cfg.get("max_order_value")
    if mov:
        q = min(q, int(D(mov) / D(entry)))
    return max(1, q)


def charges_block(c, qty, cfg, qty_source):
    """Round-trip charges at the target and at the stop, and reward:risk net of them."""
    e, s, t, risk = c["entry"], c["stop"], c["target"], c["risk"]
    at_t = round_trip_charges(e, t, qty, cfg)
    at_s = round_trip_charges(e, s, qty, cfg)
    gross_reward = (t - e) * qty
    gross_risk = risk * qty
    net_reward = gross_reward - D(at_t["total"])
    net_risk = gross_risk + D(at_s["total"])
    rr_net = (net_reward / net_risk).quantize(PAISE, rounding=ROUND_HALF_UP)
    return {"qty": qty, "qty_source": qty_source, "product": str(cfg.get("product") or "MIS").upper(),
            "at_target": at_t, "at_stop": at_s,
            "reward_gross": float(gross_reward), "reward_net": float(net_reward.quantize(PAISE)),
            "risk_gross": float(gross_risk), "risk_net": float(net_risk.quantize(PAISE)),
            "reward_risk_net": float(rr_net)}


# ---------------------------------------------------------------- bars for ATR

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_symbol(sym):
    return sym.upper() + ".NS"


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(0.5)
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None, now=None):
    ysym = yahoo_symbol(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, sym.upper() + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = ((now or ist_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), ysym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now=None, cfg=None, segment="EQ"):
    """Closed, on-grid bars of one NSE session (IST), oldest first. The live tick is dropped."""
    step = INTERVALS[interval]
    s = market_day(day, cfg, segment)
    if not s["open"]:
        return []
    open_dt = datetime.datetime.combine(day, s["start"])
    close_dt = datetime.datetime.combine(day, s["end"])
    if now is None and chart.get("data_time"):
        now = ist_of(chart["data_time"])
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        lt = ist_of(b["t"])
        if lt.date() != day or not (open_dt <= lt < close_dt):
            continue
        if now and lt + datetime.timedelta(seconds=step) > now:
            continue
        slot = int((lt - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    """trader/config.json over the defaults. Missing keys (or a missing file) take the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_explicit_max_loss"] = False
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise PlanError("%s is not a JSON object" % path)
        for k, v in doc.items():
            if v is None:
                continue
            if k == "charges" and isinstance(v, dict):
                cfg["charges"].update({ck: cv for ck, cv in v.items() if cv is not None})
            else:
                cfg[k] = v
        cfg["_explicit_max_loss"] = doc.get("max_loss_per_trade") is not None
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time, segment="EQ"):
    """(HH:MM, note). Must fall inside the session; on a special session the
    config's flat-by moves to 5 minutes before that session's close. On a regular
    day "auto" (or a time past it) becomes the safe flat-by: 5 minutes before the
    earlier of continuous trading's end and the broker_profile's MIS square-off."""
    want = explicit or cfg.get("flat_by") or DEFAULTS["flat_by"]
    what = "--flat-by" if explicit else "flat_by in the config"
    note = ""
    s = market_day(day, cfg, segment)
    if s["open"] and s["kind"] == "regular":
        if want != "auto":
            t = parse_hhmm(want, "--flat-by" if explicit else "flat_by")
            if not (s["start"] < t < s["end"]):
                raise PlanError("%s %s is outside the %s to %s IST session on %s" % (
                    what, hm(t), hm(s["start"]), hm(s["end"]), day))
        t, why = safe_flat_by(day, segment, dict(cfg, flat_by=want))
        if "too late" in why:
            note = why
    else:
        t = parse_hhmm(DEFAULTS["flat_by"] if want == "auto" else want, "--flat-by" if explicit else "flat_by")
    if s["open"] and s["kind"] != "regular":
        if not (s["start"] < t < s["end"]):
            if explicit or s["kind"] == "regular":
                raise PlanError("%s %s is outside the %s to %s IST session on %s" % (
                    "--flat-by" if explicit else "flat_by in the config", hm(t), hm(s["start"]), hm(s["end"]), day))
            t = (datetime.datetime.combine(day, s["end"]) - datetime.timedelta(minutes=5)).time()
            note = "moved to 5 minutes before the close of the %s" % s["reason"]
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (hm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this planner does longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: this planner does longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if not TICKER.match(info["symbol"]):
        raise PlanError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO; no .NS)" % info["symbol"])
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now):
    info = gather(a)
    cfg = read_config(a.config)
    tick = D(a.tick if a.tick is not None else tick_for(info["entry"], cfg.get("tick_size", DEFAULTS["tick_size"])),
             "tick_size")
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = now.date()
    cache = os.path.join(os.path.dirname(a.config) or ".", "fno_stocks.json")
    # the F&O list comes from the config or the cache; it is fetched only when this run goes online anyway
    online = stop_rule == "atr" and not a.offline
    fset, _ = load_fno_stocks(cfg, cache, fetch=online, today=now.date())
    seg = segment_of(info["symbol"], fset)
    md = market_day(day, cfg, seg)
    if not md["open"]:
        raise PlanError("NSE is shut on %s %s, %s: no session to plan for" % (day.strftime("%a"), day, md["reason"]))
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline, now)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None, cfg,
                            seg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, tick)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time, seg)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(clean_price(info["or_high"], tick)), "low": float(clean_price(info["or_low"], tick)),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "exchange": "NSE", "side": "buy",
            "date": day.isoformat(), "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick_size": float(tick), "stop_detail": c["stop_detail"],
            "created_at": (now - datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    if a.qty is not None and a.qty < 1:
        raise PlanError("--qty must be 1 or more")
    include = cfg.get("include_charges", True) and not a.no_charges
    if include:
        if a.qty is not None:
            qty, qsrc = a.qty, "--qty"
        else:
            qty, qsrc = estimate_qty(c["entry"], c["risk"], cfg), "estimate"
        plan["charges"] = charges_block(c, qty, cfg, qsrc)
        plan["reward_risk_net"] = plan["charges"]["reward_risk_net"]
    warnings = []
    if md.get("warning"):
        warnings.append(md["warning"][0].upper() + md["warning"][1:] + ".")
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    if md.get("cas"):
        plan["closing_auction"] = {"continuous_end": hm(md["end"]), "start": hm(md["cas"]["start"]),
                                   "end": hm(md["cas"]["end"]), "fno_list": fset is not None}
    if str(cfg.get("product") or "MIS").upper() == "MIS" and flat_by > "15:15" and md["kind"] == "regular":
        warnings.append("Flat-by %s is late for MIS: brokers start their own square-off around 15:15 to 15:25 "
                        "(and may charge for it)." % flat_by)
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    if include and plan["charges"]["reward_net"] <= 0:
        warnings.append("Charges at the target (%s) eat the whole reward: this trade cannot pay at this size."
                        % inr(plan["charges"]["at_target"]["total"]))
    if day != now.date():
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), now.date().isoformat()))
    return plan, warnings


MSG = {
    "en": {
        "setup_orb": "opening-range breakout",
        "setup_level": "close above a level",
        "at": " at {time} IST",
        "bar": " (bar {n})",
        "head": "Exit plan: {sym} (NSE) long, {day}, {setup} on {bars} bars",
        "bars_5m": "5-minute",
        "bars_1m": "1-minute",
        "entry": "  Entry         {entry}{when}",
        "stop": "  Stop          {stop}  {label}{detail}",
        "target": "  Target        {target}  {label}",
        "risk": "  Risk a share  {risk} ({pct}% of the entry)",
        "rr_net": "  Reward:risk   {gross} gross, {net} net of charges",
        "charges": "  Charges       {at_target} at the target, {at_stop} at the stop, round trip on {qty} shares "
                   "({product}, {source})",
        "qty_flag": "your --qty",
        "qty_max": "the most your max loss per trade allows",
        "rr_gross": "  Reward:risk   {gross} (charges not included)",
        "flat_by": "  Flat by       {time} IST, win or lose: no overnight hold",
        "auction": "  F&O stock     continuous trading ends {end}, then the closing auction {start}-{stop} IST: no "
                   "stop-loss orders there{assumed}",
        "assumed": " (no F&O list yet, so assumed)",
        "never_move": "Set before the entry. If price hits the stop, you are out; never move it further away.",
        "gap": "A stop is a plan, not a guarantee: a gap or a fast market can fill past it.",
        "note": "Note: {text}",
        "wrote": "Wrote {path}.",
        "next": "Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was "
                "placed.",
        "not_today": "Note: this plan is for {date}, not today ({today}).",
    },
    "hi-en": {
        "setup_orb": "opening-range breakout",
        "setup_level": "level ke upar close",
        "at": ", {time} IST",
        "bar": " (bar {n})",
        "head": "Exit plan: {sym} (NSE) long, {day}, {setup}, {bars} bars pe",
        "bars_5m": "5-minute",
        "bars_1m": "1-minute",
        "entry": "  Entry         {entry}{when}",
        "stop": "  Stop          {stop}  {label}{detail}",
        "target": "  Target        {target}  {label}",
        "risk": "  Risk / share  {risk} (entry ka {pct}%)",
        "rr_net": "  Reward:risk   {gross} gross, charges ke baad {net}",
        "charges": "  Charges       target pe {at_target}, stop pe {at_stop}, {qty} shares ka round trip "
                   "({product}, {source})",
        "qty_flag": "aapka --qty",
        "qty_max": "jitna aapka max loss per trade allow karta hai",
        "rr_gross": "  Reward:risk   {gross} (charges shaamil nahi)",
        "flat_by": "  Flat by       {time} IST, profit ho ya loss: overnight hold nahi",
        "auction": "  F&O stock     continuous trading {end} pe khatam, phir closing auction {start}-{stop} IST: "
                   "wahan stop-loss order nahi lagte{assumed}",
        "assumed": " (F&O list abhi nahi hai, isliye maan liya)",
        "never_move": "Yeh entry se pehle set kijiye. Price stop pe aaye to aap bahar; stop ko kabhi aur door mat "
                      "kijiye.",
        "gap": "Stop ek plan hai, guarantee nahi: gap ya tez market mein fill stop se aage bhi ho sakta hai.",
        "note": "Dhyan dein: {text}",
        "wrote": "{path} mein save kiya.",
        "next": "Aage: /trader-timemachine ise replay karega, phir /trader-budget aapke max loss ke hisaab se size "
                "nikalega. Kuch bhi place nahi hua.",
        "not_today": "Dhyan dein: yeh plan {date} ka hai, aaj ({today}) ka nahi.",
    },
}


# ---------------------------------------------------------------- message catalogue helper (same in every skill with MSG)
LANGS = ("en", "hi-en")


def pick_lang(flag=None, cfg=None):
    """The output language: --lang beats the config's "lang"; "hi-en" is Hinglish (Roman script), else English."""
    for v in (flag, cfg.get("lang") if isinstance(cfg, dict) else None):
        v = str(v or "").strip().lower().replace("_", "-")
        if v == "hinglish":
            v = "hi-en"
        if v in LANGS:
            return v
    return "en"


def t(key, lang="en", **kw):
    """MSG[lang][key] filled with kw (str.format); the English text when a language or key is missing."""
    text = (MSG.get(lang) or {}).get(key)
    if text is None:
        text = MSG["en"][key]
    return text.format(**kw) if kw else text
# ---------------------------------------------------------------- end of message catalogue helper


def describe(plan, lang="en"):
    day = datetime.date.fromisoformat(plan["date"])
    setup = t("setup_" + plan["setup"], lang) if plan["setup"] in ("orb", "level") else plan["setup"]
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISE, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = t("at", lang, time=plan["entry_time"])
        if plan.get("entry_bar") is not None:
            when += t("bar", lang, n=plan["entry_bar"])
    out = [t("head", lang, sym=plan["symbol"], day=day.strftime("%a %Y-%m-%d"), setup=setup,
             bars=t("bars_5m" if plan["interval"] == "5m" else "bars_1m", lang))]
    out.append(t("entry", lang, entry=inr(plan["entry"]), when=when))
    out.append(t("stop", lang, stop=inr(plan["stop"]), label=plan["stop_label"],
                 detail=(": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append(t("target", lang, target=inr(plan["target"]), label=plan["target_label"]))
    out.append(t("risk", lang, risk=inr(plan["risk_per_share"]), pct=pct))
    ch = plan.get("charges")
    if ch:
        out.append(t("rr_net", lang, gross=ratio(plan["reward_risk"]), net=ratio(ch["reward_risk_net"])))
        out.append(t("charges", lang, at_target=inr(ch["at_target"]["total"]), at_stop=inr(ch["at_stop"]["total"]),
                     qty=ch["qty"], product=ch["product"],
                     source=t("qty_flag" if ch["qty_source"] == "--qty" else "qty_max", lang)))
    else:
        out.append(t("rr_gross", lang, gross=ratio(plan["reward_risk"])))
    out.append(t("flat_by", lang, time=plan["flat_by"]))
    cas = plan.get("closing_auction")
    if cas:
        out.append(t("auction", lang, end=cas["continuous_end"], start=cas["start"], stop=cas["end"],
                     assumed="" if cas.get("fno_list") else t("assumed", lang)))
    out.append(t("never_move", lang))
    out.append(t("gap", lang))
    return out


def lang_of(a):
    """--lang, else the config's "lang" (a missing or unreadable config is English)."""
    cfg = None
    path = getattr(a, "config", None) or "trader/config.json"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = None
    return pick_lang(getattr(a, "lang", None), cfg)


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2, ensure_ascii=False))
        return 0
    lang = lang_of(a)
    for line in describe(plan, lang):
        print(line)
    for w in warnings:
        print(t("note", lang, text=w))
    if a.write:
        print(t("wrote", lang, path=a.write))
    print(t("next", lang))
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    lang = lang_of(a)
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan), lang):
        print(line)
    today = parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print(t("not_today", lang, date=plan["date"], today=today))
    return 0


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol", help="NSE symbol, e.g. HDFCBANK (no .NS)")
    p.add_argument("--entry", type=float, help="entry price in rupees (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, 24-hour IST, e.g. 10:10")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--tick", type=float, help="tick size in rupees (default: config tick_size, auto by price band)")
    p.add_argument("--qty", type=int, help="shares for the charges estimate (default: what your max loss allows)")
    p.add_argument("--no-charges", action="store_true", help="leave the charges estimate out")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-26 10:10"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lang", choices=LANGS, metavar="LANG", help="en or hi-en (Hinglish); default: the config's \"lang\", else en")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="only its \"lang\" is read")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lang", choices=LANGS, metavar="LANG", help="en or hi-en (Hinglish); default: the config's \"lang\", else en")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except (PlanError, RulesError) as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
