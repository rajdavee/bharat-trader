#!/usr/bin/env python3
"""trader-regime: what kind of market is it today? Trend or range, the volatility bucket, the gap,
expiry and event days, and the time of day, for NIFTY, BANKNIFTY and any NSE stock.

    python3 regime.py now                                  # NIFTY and BANKNIFTY, in plain words
    python3 regime.py now RELIANCE --json                  # one stock, machine-readable
    python3 regime.py history NIFTY --from 2025-01-01 --to 2026-10-30   # every day, stored
    python3 regime.py explain NIFTY                        # every input, its value and its threshold

A strategy that works in a trending, quiet market often fails in a choppy, nervous one. This
skill labels each trading day so the lab can split its results by regime, and so you (or a
co-trader) can ask "does this setup have an edge in the market we have now?".

Per day, from daily bars up to that day's close and nothing later (no look-ahead):
  trend      ADX(14) (Wilder), Kaufman's efficiency ratio (10), the 20 and 50 EMAs and the 20 EMA's
             slope over 5 sessions, the close against the 200-day average -> trending up, trending
             down, ranging or no clear trend
  volatility India VIX's percentile over the last 252 sessions (indices); the stock's own 20-day
             realised vol percentile (stocks); realised vol 20 days vs 252 days; IV rank from India
             VIX, or for a stock an approximate ATM IV from the archived F&O bhavcopy (--iv)
  gap        the day's open against the previous close, in ATR(14) of the days before
  breadth    advances and declines in the day's equity bhavcopy (when the archive has it)
  calendar   weekly and monthly expiry today or on the next session (the exchange's expiry list in
             the archived F&O bhavcopy, else the dated weekday rule), results day from NSE board
             meetings, RBI policy and Union Budget days (events.json, with sources), holidays and
             special sessions, and the closing-auction window from the dated SESSION_RULES
  time       open drive 09:15-10:00, morning, midday, afternoon, closing window and the closing
             auction (F&O stocks), from the dated session rules; intraday reads use bars complete by now

Data: the local trader-archive (trader/archive, --archive DIR) when present, else Yahoo Finance
daily bars (2 years, unofficial and free), or --data DIR offline (SYMBOL-1d.csv(.gz), ^NSEI,
^NSEBANK, ^INDIAVIX). Thresholds: DEFAULTS below, overridden by trader/regime-config.json.

Pure functions for other skills: indicators, classify, daily_regimes, regime_on, time_bucket,
expiry_info, regime_key, label_trades, split_by_regime. Analytics only: it never places an order.
Education and tooling, not financial advice.
"""

import argparse
import bisect
import csv
import datetime
import gzip
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

SCHEMA = "bharat-trader/regime/1"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
PAISE = Decimal("0.01")
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130 Safari/537.36")
YAHOO_UA = "Mozilla/5.0 (compatible; bharat-trader/0.3; +https://github.com/rajdavee/bharat-trader)"
BOARD_URL = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=%s"
PAUSE = 1.5            # seconds between network calls
BACKOFF = 20.0         # seconds to wait once after HTTP 403/429
CLOSE_MIN = 15 * 60 + 30
INDEX_YAHOO = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "VIX": "^INDIAVIX"}
INDEX_ARCHIVE = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"}
DEFAULT_SYMBOLS = ("NIFTY", "BANKNIFTY")

# Every threshold, with its default. trader/regime-config.json overrides any of them.
DEFAULTS = {
    "adx_period": 14,          # Wilder's ADX
    "er_period": 10,           # Kaufman's efficiency ratio: net move / sum of daily moves
    "ema_fast": 20,
    "ema_slow": 50,
    "slope_sessions": 5,       # the fast EMA's slope: % change over this many sessions
    "sma_long": 200,           # the 200-day average
    "adx_trend": 25.0,         # ADX at or above: a trend (when the averages agree on a direction)
    "adx_range": 20.0,         # ADX below this and ER below er_range: a range
    "er_trend": 0.30,          # or ER at or above this with a direction: a trend
    "er_range": 0.20,
    "vol_lookback": 252,       # sessions for the VIX and realised-vol percentiles (about a year)
    "vol_min_history": 60,     # fewer sessions than this: the bucket is unknown
    "vol_low_pct": 33.3,       # percentile below: low vol
    "vol_high_pct": 66.7,      # percentile at or above: high vol
    "rv_short": 20,            # realised vol window, sessions
    "atr_period": 14,
    "gap_atr": 0.25,           # |open - previous close| at least this many ATRs: a gap
    "gap_big_atr": 0.75,       # at least this many: a big gap
    "breadth_strong": 0.65,    # advancing share at or above: broad advance (below 1 - this: broad decline)
    "open_drive_end": "10:00",
    "morning_end": "11:30",
    "midday_end": "13:30",
    "closing_window_min": 30,  # the last N minutes of continuous trading
    "events": [],              # your own dated events: {"date", "kind", "name", "source"}
}

# NSE Capital Market trading holidays, from NSE's holiday master. 2027 is not published yet. Older years
# need no table when the archive is there: its calendar.json holds the weekdays NSE had no session.
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
# Sessions on a weekend or a holiday (verified on NSE's archive), and the short ones.
EXTRA_SESSIONS = {
    "2023-11-12": "Muhurat trading (Sunday)",
    "2024-01-20": "special Saturday session",
    "2024-03-02": "Saturday session with a disaster-recovery drill",
    "2024-05-18": "Saturday session with a disaster-recovery drill",
    "2025-02-01": "Union Budget (Saturday)",
    "2026-02-01": "Union Budget (Sunday)",
    "2026-11-08": "Muhurat trading",
}
SHORT_SESSIONS = {
    "2023-11-12": "Muhurat trading, about an hour",
    "2024-03-02": "disaster-recovery drill, two short windows",
    "2024-05-18": "disaster-recovery drill, two short windows",
    "2024-11-01": "Muhurat trading, about an hour",
    "2025-10-21": "Muhurat trading, about an hour",
    "2026-11-08": "Muhurat trading, about an hour",
}

# The expiry weekday when the archive has no F&O bhavcopy for the day (the exchange's own list wins).
# A contract due on a holiday expires on the previous trading day. Monthly contracts (index and stock)
# expire on the month's last such weekday; weekly ones every week, NIFTY only (one weekly index per
# exchange since 20 Nov 2024). Before the first row the expiry is unknown without the archive.
EXPIRY_RULES = [
    {"effective_from": "2024-11-20", "weekday": 3, "weekly": ["NIFTY"],
     "source": "SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132 (1 Oct 2024): one weekly index expiry per "
               "exchange from 20 Nov 2024; NSE kept NIFTY's weekly on Thursday"},
    {"effective_from": "2025-09-01", "weekday": 1, "weekly": ["NIFTY"],
     "source": "NSE: all equity derivatives expire on Tuesday from 1 Sep 2025 (live NSE expiry lists checked "
               "1 Nov 2026: every expiry a Tuesday or the trading day before a Tuesday holiday)"},
]
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class RegimeError(Exception):
    pass


# ---------------------------------------------------------------- dated market rules (same in entry, exit, alerts, risk, timemachine, scanner, broker, lab, regime)
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


def num(x, places=2):
    """23053.1 -> '23,053.10' (Indian grouping, no rupee sign: index points)."""
    return inr(x).replace("₹", "") if places == 2 else str(round(x, places))


def _r(x, places=2):
    return None if x is None else round(float(x), places)


# ---------------------------------------------------------------- config

def load_config(path=None):
    """DEFAULTS with trader/regime-config.json (or `path`) on top. Unknown keys are an error."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise RegimeError("%s is not valid JSON (%s)" % (path, e))
        if not isinstance(doc, dict):
            raise RegimeError("%s must be a JSON object" % path)
        for k, v in doc.items():
            if k not in DEFAULTS:
                raise RegimeError("%s: unknown key %r (known: %s)" % (path, k, ", ".join(sorted(DEFAULTS))))
            if k == "events":
                if not isinstance(v, list) or not all(isinstance(e, dict) and re.match(r"^\d{4}-\d\d-\d\d$",
                                                                                        str(e.get("date", "")))
                                                      for e in v):
                    raise RegimeError("%s: events is a list of {\"date\": \"YYYY-MM-DD\", \"kind\", \"name\", "
                                      "\"source\"}" % path)
            elif isinstance(DEFAULTS[k], str):
                _rule_time(v, k)
            elif not isinstance(v, (int, float)) or isinstance(v, bool):
                raise RegimeError("%s: %s is a number" % (path, k))
            cfg[k] = v
    return cfg


def config_hash(cfg):
    keep = {k: v for k, v in cfg.items() if k != "events"}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def builtin_events():
    try:
        with open(os.path.join(HERE, "events.json"), encoding="utf-8") as fh:
            return json.load(fh).get("events") or []
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- indicators (each value uses bars up to its own index)

def ema(xs, n):
    """Exponential average, alpha 2/(n+1), seeded with the simple average of the first n values."""
    out = [None] * len(xs)
    if len(xs) < n or n < 1:
        return out
    a = 2.0 / (n + 1)
    e = sum(xs[:n]) / float(n)
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = a * xs[i] + (1 - a) * e
        out[i] = e
    return out


def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def true_ranges(h, l, c):
    """TR from the second bar on (index 0 is None): max(high - low, |high - prev close|, |low - prev close|)."""
    out = [None]
    for i in range(1, len(c)):
        out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def atr(h, l, c, n):
    """Wilder's ATR: the mean of the first n true ranges, then (previous x (n - 1) + TR) / n."""
    tr = true_ranges(h, l, c)
    out = [None] * len(c)
    if len(c) <= n:
        return out
    a = sum(tr[1:n + 1]) / float(n)
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / float(n)
        out[i] = a
    return out


def adx(h, l, c, n):
    """Wilder's ADX with +DI and -DI: ([adx], [plus_di], [minus_di]). DI from bar n, ADX from bar 2n - 1."""
    size = len(c)
    pdi, mdi, ax = [None] * size, [None] * size, [None] * size
    if size <= n:
        return ax, pdi, mdi
    tr = true_ranges(h, l, c)
    pdm, mdm = [None], [None]
    for i in range(1, size):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = [None] * size
    for i in range(n, size):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        p = 100.0 * s_p / s_tr if s_tr > 0 else 0.0
        m = 100.0 * s_m / s_tr if s_tr > 0 else 0.0
        pdi[i], mdi[i] = p, m
        dx[i] = 100.0 * abs(p - m) / (p + m) if p + m > 0 else 0.0
    first = 2 * n - 1
    if size > first:
        a = sum(dx[n:first + 1]) / float(n)
        ax[first] = a
        for i in range(first + 1, size):
            a = (a * (n - 1) + dx[i]) / float(n)
            ax[i] = a
    return ax, pdi, mdi


def efficiency_ratio(c, n):
    """Kaufman: |close - close n bars ago| / sum of |daily changes| over those n bars (0 = chop, 1 = straight)."""
    out = [None] * len(c)
    for i in range(n, len(c)):
        path = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i] - c[i - n]) / path if path > 0 else 0.0
    return out


def realised_vol(c, n):
    """Annualised close-to-close vol in %: sample stdev of the last n log returns x sqrt(252) x 100."""
    out = [None] * len(c)
    rets = [None] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 and c[i] > 0 else 0.0 for i in range(1, len(c))]
    for i in range(n, len(c)):
        w = rets[i - n + 1:i + 1]
        m = sum(w) / n
        var = sum((x - m) ** 2 for x in w) / (n - 1) if n > 1 else 0.0
        out[i] = math.sqrt(var) * math.sqrt(252) * 100.0
    return out


def percentile_rank(window, cur):
    """% of the values in window strictly below cur (trader-options' IV percentile)."""
    xs = [x for x in window if x is not None]
    if not xs:
        return None
    return 100.0 * sum(1 for x in xs if x < cur) / len(xs)


def range_rank(window, cur):
    """(cur - low) / (high - low) x 100 over window (trader-options' IV rank)."""
    xs = [x for x in window if x is not None]
    if not xs or max(xs) <= min(xs):
        return None
    return 100.0 * (cur - min(xs)) / (max(xs) - min(xs))


def indicators(bars, cfg=None):
    """Every indicator column for daily bars [{"date", "o", "h", "l", "c"}], oldest first."""
    cfg = cfg or DEFAULTS
    h, l, c = [float(b["h"]) for b in bars], [float(b["l"]) for b in bars], [float(b["c"]) for b in bars]
    ax, pdi, mdi = adx(h, l, c, int(cfg["adx_period"]))
    rv = realised_vol(c, int(cfg["rv_short"]))
    return {"date": [b["date"] for b in bars], "o": [float(b["o"]) for b in bars], "h": h, "l": l, "c": c,
            "adx": ax, "plus_di": pdi, "minus_di": mdi, "er": efficiency_ratio(c, int(cfg["er_period"])),
            "ema_fast": ema(c, int(cfg["ema_fast"])), "ema_slow": ema(c, int(cfg["ema_slow"])),
            "sma_long": sma(c, int(cfg["sma_long"])), "atr": atr(h, l, c, int(cfg["atr_period"])),
            "rv": rv, "rv_long": realised_vol(c, min(int(cfg["vol_lookback"]), max(len(c) - 1, 2)))
            if len(c) > int(cfg["vol_lookback"]) else [None] * len(c)}


# ---------------------------------------------------------------- classification

def classify_trend(adx_v, er_v, close, ema_f, ema_s, slope, cfg=None):
    """(label, direction) from one day's inputs. label: trend_up, trend_down, range, transition or unknown."""
    cfg = cfg or DEFAULTS
    if None in (adx_v, er_v, ema_f, ema_s, slope):
        return "unknown", None
    if close > ema_s and ema_f > ema_s and slope > 0:
        d = "up"
    elif close < ema_s and ema_f < ema_s and slope < 0:
        d = "down"
    else:
        d = None
    if d and (adx_v >= cfg["adx_trend"] or er_v >= cfg["er_trend"]):
        return "trend_" + d, d
    if adx_v < cfg["adx_range"] and er_v < cfg["er_range"]:
        return "range", d
    if d is None and adx_v < cfg["adx_trend"]:
        return "range", d
    return "transition", d


def vol_bucket(pct, cfg=None):
    cfg = cfg or DEFAULTS
    if pct is None:
        return "unknown"
    if pct < cfg["vol_low_pct"]:
        return "low"
    if pct >= cfg["vol_high_pct"]:
        return "high"
    return "normal"


def gap_type(open_, prev_close, prev_atr, cfg=None):
    """{"type": "gap up" / "gap down" / "big gap up" / "big gap down" / "no gap", "pct", "atr_mult"}."""
    cfg = cfg or DEFAULTS
    if None in (open_, prev_close, prev_atr) or prev_close <= 0 or prev_atr <= 0:
        return {"type": "unknown", "pct": None, "atr_mult": None}
    g = open_ - prev_close
    m = abs(g) / prev_atr
    side = "up" if g > 0 else "down"
    t = ("big gap " + side) if m >= cfg["gap_big_atr"] else (("gap " + side) if m >= cfg["gap_atr"] else "no gap")
    return {"type": t, "pct": _r(100.0 * g / prev_close, 2), "atr_mult": _r(m, 2)}


def _window(col, i, n):
    return [x for x in col[max(0, i - n + 1):i + 1] if x is not None]


def classify(ind, i, vix=None, cfg=None, kind="index"):
    """The day i regime (trend, vol, gap) from indicator columns (indicators()) and an optional
    {date: India VIX close} map. Uses only values at or before i."""
    cfg = cfg or DEFAULTS
    c = ind["c"][i]
    s_n = int(cfg["slope_sessions"])
    ef = ind["ema_fast"]
    slope = 100.0 * (ef[i] / ef[i - s_n] - 1) if i >= s_n and ef[i] is not None and ef[i - s_n] else None
    label, d = classify_trend(ind["adx"][i], ind["er"][i], c, ef[i], ind["ema_slow"][i], slope, cfg)
    sl = ind["sma_long"][i]
    trend = {"label": label, "class": "trend" if label.startswith("trend_") else ("unknown" if label == "unknown"
                                                                                    else "range"),
             "direction": d, "adx": _r(ind["adx"][i], 1), "plus_di": _r(ind["plus_di"][i], 1),
             "minus_di": _r(ind["minus_di"][i], 1), "er": _r(ind["er"][i], 3), "ema_fast": _r(ef[i]),
             "ema_slow": _r(ind["ema_slow"][i]), "ema_fast_slope_pct": _r(slope, 3), "sma_long": _r(sl),
             "above_sma_long": None if sl is None else c > sl,
             "vs_sma_long_pct": None if sl is None else _r(100.0 * (c / sl - 1), 2)}
    look, need = int(cfg["vol_lookback"]), int(cfg["vol_min_history"])
    rv = ind["rv"][i]
    rv_hist = _window(ind["rv"], i, look)
    rv_long = ind["rv_long"][i]
    vol = {"rv20": _r(rv), "rv_long": _r(rv_long), "rv_ratio": _r(rv / rv_long, 2) if rv and rv_long else None,
           "rv_pct": _r(percentile_rank(rv_hist, rv), 1) if rv is not None and len(rv_hist) >= need else None,
           "vix": None, "vix_pct": None, "vix_rank": None, "iv_rank": None, "iv_source": None}
    if vix:
        day = ind["date"][i]
        dates = vix["dates"]
        k = bisect.bisect_right(dates, day) - 1
        if k >= 0 and dates[k] == day:
            vals = vix["values"][max(0, k - look + 1):k + 1]
            cur = vix["values"][k]
            vol["vix"] = _r(cur)
            if len(vals) >= need:
                vol["vix_pct"] = _r(percentile_rank(vals, cur), 1)
                vol["vix_rank"] = _r(range_rank(vals, cur), 1)
                if kind == "index":
                    vol["iv_rank"], vol["iv_source"] = vol["vix_rank"], "India VIX (NIFTY 30-day implied vol)"
    if kind == "index" and vol["vix_pct"] is not None:
        vol["basis"], vol["bucket"] = "India VIX percentile", vol_bucket(vol["vix_pct"], cfg)
    else:
        vol["basis"], vol["bucket"] = "20-day realised vol percentile", vol_bucket(vol["rv_pct"], cfg)
    prev_atr = ind["atr"][i - 1] if i >= 1 else None
    gap = gap_type(ind["o"][i], ind["c"][i - 1] if i >= 1 else None, prev_atr, cfg)
    return {"date": ind["date"][i], "close": _r(c), "trend": trend, "vol": vol, "gap": gap,
            "atr": _r(ind["atr"][i])}


def vix_map(vix_bars):
    vix_bars = sorted((b for b in vix_bars or [] if b.get("c") is not None), key=lambda b: b["date"])
    return {"dates": [b["date"] for b in vix_bars], "values": [float(b["c"]) for b in vix_bars]} if vix_bars else None


# ---------------------------------------------------------------- the trading calendar

def _day(d):
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    try:
        return datetime.datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise RegimeError("a date is YYYY-MM-DD, not %r" % (d,))


def is_session(d, closed=None):
    k = _day(d).isoformat()
    if k in EXTRA_SESSIONS:
        return True
    if k in NSE_HOLIDAYS or (closed and k in closed):
        return False
    return _day(d).weekday() < 5


def next_session(d, closed=None, step=1):
    """The next (step=1) or previous (step=-1) NSE session after d, from the calendar (known in advance)."""
    x = _day(d)
    for _ in range(30):
        x += datetime.timedelta(days=step)
        if is_session(x, closed) and x.isoformat() not in SHORT_SESSIONS:
            return x
    return None


def _rule_row(day):
    k = _day(day).isoformat()
    pick = None
    for row in EXPIRY_RULES:
        if row["effective_from"] <= k:
            pick = row
    return pick


def _shift_back(d, closed):
    while not is_session(d, closed) or d.isoformat() in SHORT_SESSIONS:
        d -= datetime.timedelta(days=1)
    return d


def rule_expiries(underlying, day, closed=None, months=3):
    """The expiry dates by the dated weekday rule, as known on `day`: {"weekly", "monthly", "source"} or None
    when no rule covers the day (then only the archive's F&O bhavcopy knows)."""
    d0 = _day(day)
    out_w, out_m, src, why = set(), set(), None, {}
    y, m = d0.year, d0.month
    for k in range(months):
        mm, yy = (m - 1 + k) % 12 + 1, y + (m - 1 + k) // 12
        last = datetime.date(yy + mm // 12, mm % 12 + 1, 1) - datetime.timedelta(days=1)
        first = datetime.date(yy, mm, 1)
        cur = first
        while cur <= last:
            row = _rule_row(cur)
            if row and cur.weekday() == row["weekday"]:
                if underlying.upper() in row["weekly"]:
                    out_w.add(_shift_back(cur, closed))
                    why[_shift_back(cur, closed)] = row["source"]
                src = row["source"]
            cur += datetime.timedelta(days=1)
        lastrow = _rule_row(last)
        if lastrow:
            x = last
            while x.weekday() != lastrow["weekday"]:
                x -= datetime.timedelta(days=1)
            if _rule_row(x):
                out_m.add(_shift_back(x, closed))
                why[_shift_back(x, closed)] = _rule_row(x)["source"]
    if src is None and not out_m:
        return None
    keep = lambda s: sorted(x.isoformat() for x in s if x >= d0)
    up = sorted(x for x in why if x >= d0)
    return {"weekly": keep(out_w), "monthly": keep(out_m), "source": "rule: " + (why[up[0]] if up else src)}


def archive_expiries(underlying, day, root, api=None):
    """The expiries listed in the archived F&O bhavcopy of `day` (or the latest one up to 7 days before):
    {"weekly", "monthly", "source"} or None. Futures expire monthly, so a futures expiry is a monthly one;
    an option expiry with no future is a weekly."""
    api = api or archive_api()
    if api is None or not root or not os.path.isdir(os.path.join(root, "fo")):
        return None
    d0 = _day(day)
    store = api.Store(root)
    for back in range(8):
        d = d0 - datetime.timedelta(days=back)
        p = store.path("fo", d)
        if os.path.exists(p):
            break
    else:
        return None
    key = ("fo", p, os.path.getsize(p))
    if key not in _CACHE:
        rows = {}
        with open(p, "rb") as fh:
            lines = gzip.decompress(fh.read()).decode("utf-8").splitlines()[1:]
        for line in lines:
            parts = line.split(",", 3)
            if len(parts) >= 3:
                rows.setdefault(parts[0], set()).add((parts[1], parts[2]))
        _CACHE.clear() if len(_CACHE) > 64 else None
        _CACHE[key] = rows
    got = _CACHE[key].get(underlying.upper())
    if not got:      # the file is there and the symbol is not: no F&O contracts, no expiry
        return {"weekly": [], "monthly": [], "source": "archive F&O bhavcopy %s (no contracts)" % d.isoformat()}
    fut = {e for k, e in got if k in ("IDF", "STF")}
    opt = {e for k, e in got if k in ("IDO", "STO")}
    keep = lambda s: sorted(x for x in s if x >= d0.isoformat())
    return {"weekly": keep(opt | fut), "monthly": keep(fut), "source": "archive F&O bhavcopy %s" % d.isoformat()}


_CACHE = {}


def expiry_info(underlying, day, root=None, closed=None, api=None):
    """Expiry flags for one underlying on one day: {"today", "next_session", "next_expiry", "next_kind",
    "sessions_to_expiry", "source"}. today / next_session: "weekly", "monthly" or None."""
    d0 = _day(day)
    lst = archive_expiries(underlying, d0, root, api) if root else None
    if lst is None:
        lst = rule_expiries(underlying, d0, closed)
        if lst is not None and underlying.upper() not in INDEX_YAHOO:
            lst["source"] += " (if the stock is in NSE's F&O list)"
    if lst is None:
        return {"today": None, "next_session": None, "next_expiry": None, "next_kind": None,
                "sessions_to_expiry": None, "source": "unknown: no archived F&O bhavcopy and no expiry rule for this date",
                "known": False}
    allx = sorted(set(lst["weekly"]) | set(lst["monthly"]))
    kind = lambda e: "monthly" if e in lst["monthly"] else "weekly"
    nxt = next_session(d0, closed)
    today = kind(d0.isoformat()) if d0.isoformat() in allx else None
    tom = kind(nxt.isoformat()) if nxt and nxt.isoformat() in allx else None
    up = [e for e in allx if e >= d0.isoformat()]
    n_sess = None
    if up:
        n_sess, x = 0, d0
        while x.isoformat() < up[0] and n_sess < 60:
            x = next_session(x, closed)
            n_sess += 1
    return {"today": today, "next_session": tom, "next_expiry": up[0] if up else None,
            "next_kind": kind(up[0]) if up else None, "sessions_to_expiry": n_sess, "source": lst["source"],
            "known": True}


def results_days(meetings):
    """The board-meeting dates whose purpose mentions results: sorted list of YYYY-MM-DD."""
    out = set()
    for m in meetings or []:
        if m.get("date") and re.search(r"result", str(m.get("purpose") or ""), re.I):
            out.add(m["date"])
    return sorted(out)


def calendar_flags(symbol, day, kind="index", root=None, closed=None, events=(), meetings=None, api=None):
    """Everything about the date itself, known before the session opens."""
    d0 = _day(day)
    k = d0.isoformat()
    nxt = next_session(d0, closed)
    underlying = symbol.upper()
    exp = expiry_info(underlying, d0, root, closed, api)
    ev = [dict(e) for e in events if e.get("date") == k]
    ev_next = [dict(e) for e in events if nxt and e.get("date") == nxt.isoformat()]
    res = results_days(meetings) if meetings is not None else None
    prev = next_session(d0, closed, -1)
    out = {"weekday": WEEKDAYS[d0.weekday()], "expiry": exp, "events": ev, "events_next_session": ev_next,
           "next_session": nxt.isoformat() if nxt else None,
           "holiday_before_next_session": None, "special_session": EXTRA_SESSIONS.get(k),
           "short_session": SHORT_SESSIONS.get(k), "results_today": None, "results_next_session": None,
           "after_results": None}
    if nxt and (nxt - d0).days > 1:
        gaps = [(d0 + datetime.timedelta(days=j)).isoformat() for j in range(1, (nxt - d0).days)]
        hol = [NSE_HOLIDAYS.get(g) or (closed or {}).get(g) for g in gaps
               if _day(g).weekday() < 5 and (g in NSE_HOLIDAYS or (closed and g in closed))]
        hol = [h for h in hol if h]
        if hol:
            out["holiday_before_next_session"] = "; ".join(hol)
    if res is not None and kind == "stock":
        out["results_today"] = k in res
        out["results_next_session"] = bool(nxt) and nxt.isoformat() in res
        out["after_results"] = bool(prev) and prev.isoformat() in res
    try:
        seg = "DERIV" if kind == "index" else "EQ_FNO"
        s = session_for(d0, seg)
        cas = session_for(d0, "EQ_FNO")["cas"]
        out["session"] = {"segment": seg, "open": s["open"].strftime("%H:%M"),
                          "continuous_end": s["continuous_end"].strftime("%H:%M"),
                          "closing_auction": None if not cas else "%s-%s" % (cas["start"].strftime("%H:%M"),
                                                                            cas["end"].strftime("%H:%M")),
                          "rule_from": s["rule_from"]}
    except RulesError:
        out["session"] = None
    return out


# ---------------------------------------------------------------- time of day

def _hm(t):
    return t.hour * 60 + t.minute


def time_bucket(when, segment="EQ_FNO", cfg=None):
    """The intraday bucket of a moment (naive IST datetime): {"bucket", "from", "to", "note"}.
    Buckets: closed, pre-market, pre-open, open drive, morning, midday, afternoon, closing window,
    closing auction (F&O stocks under the CAS), post-close. The ends come from the dated SESSION_RULES."""
    cfg = cfg or DEFAULTS
    d = when.date()
    if not is_session(d):
        return {"bucket": "closed", "from": None, "to": None, "note": "no NSE session today"}
    s = session_for(d, segment)
    m = _hm(when.time())
    po0, po1 = _hm(s["pre_open"][0]), _hm(s["open"])
    op = _hm(s["open"])
    end = _hm(s["continuous_end"])
    win = end - int(cfg["closing_window_min"])
    marks = [("open drive", op, _minutes(cfg["open_drive_end"])), ("morning", _minutes(cfg["open_drive_end"]),
                                                                   _minutes(cfg["morning_end"])),
             ("midday", _minutes(cfg["morning_end"]), _minutes(cfg["midday_end"])),
             ("afternoon", _minutes(cfg["midday_end"]), win), ("closing window", win, end)]
    clock = lambda x: "%02d:%02d" % (x // 60, x % 60)
    if m < po0:
        return {"bucket": "pre-market", "from": None, "to": clock(po0), "note": ""}
    if m < po1:
        return {"bucket": "pre-open", "from": clock(po0), "to": clock(po1), "note": "pre-open call auction"}
    for name, a, b in marks:
        if a <= m < b:
            note = ""
            if name == "closing window" and s["cas"]:
                note = "continuous trading ends %s; the closing auction runs %s-%s" % (
                    clock(end), s["cas"]["start"].strftime("%H:%M"), s["cas"]["end"].strftime("%H:%M"))
            return {"bucket": name, "from": clock(a), "to": clock(b), "note": note}
    if s["cas"] and m < _hm(s["cas"]["end"]):
        return {"bucket": "closing auction", "from": s["cas"]["start"].strftime("%H:%M"),
                "to": s["cas"]["end"].strftime("%H:%M"), "note": "no continuous trading in F&O stocks"}
    return {"bucket": "post-close", "from": clock(_hm(s["close"])), "to": None, "note": ""}


def _minutes(text):
    t = _rule_time(text)
    return t.hour * 60 + t.minute


def intraday_read(bars, now, prev_close=None, atr_v=None):
    """Today's bars complete by `now` (5-minute bars: start + 5 min <= now) -> {"bars", "open", "last",
    "move_pct", "range_atr", "er"} or None."""
    day, m = now.date().isoformat(), now.hour * 60 + now.minute
    seen = []
    for b in bars or []:
        if b["date"] != day or not b.get("time"):
            continue
        hh, mm = int(b["time"][:2]), int(b["time"][3:5])
        if hh * 60 + mm + 5 <= m:
            seen.append(b)
    if not seen:
        return None
    seen.sort(key=lambda b: b["time"])
    cs = [float(b["c"]) for b in seen]
    hi, lo = max(float(b["h"]) for b in seen), min(float(b["l"]) for b in seen)
    path = sum(abs(cs[j] - cs[j - 1]) for j in range(1, len(cs)))
    er = abs(cs[-1] - float(seen[0]["o"])) / (path + abs(cs[0] - float(seen[0]["o"]))) if path or cs[0] != seen[0]["o"] else 0.0
    return {"bars": len(seen), "through": seen[-1]["time"], "open": float(seen[0]["o"]), "last": cs[-1],
            "move_pct": _r(100.0 * (cs[-1] / prev_close - 1), 2) if prev_close else None,
            "range_atr": _r((hi - lo) / atr_v, 2) if atr_v else None, "er": _r(er, 2)}


# ---------------------------------------------------------------- whole histories and one day

def daily_regimes(bars, vix_bars=None, symbol="NIFTY", kind=None, cfg=None, root=None, closed=None,
                  events=None, meetings=None, breadth=None, iv=None, api=None, start=None, end=None):
    """{date: regime record} for every daily bar (oldest first input). A day's record uses bars up to and
    including that day only. breadth: {date: breadth dict}; iv: {date: atm iv} for a stock's IV rank."""
    cfg = cfg or DEFAULTS
    kind = kind or ("index" if symbol.upper() in INDEX_YAHOO else "stock")
    events = list(builtin_events() if events is None else events) + list(cfg.get("events") or [])
    bars = sorted(bars, key=lambda b: b["date"])
    ind = indicators(bars, cfg)
    vx = vix_map(vix_bars) if vix_bars else None
    ivd = sorted(iv.items()) if iv else []
    out = {}
    for i, b in enumerate(bars):
        if (start and b["date"] < str(start)) or (end and b["date"] > str(end)):
            continue
        rec = classify(ind, i, vx, cfg, kind)
        rec["symbol"], rec["kind"], rec["schema"] = symbol.upper(), kind, SCHEMA
        if ivd and kind == "stock":
            hist = [v for d, v in ivd if d <= b["date"]][-int(cfg["vol_lookback"]):]
            if len(hist) >= 20 and ivd and [d for d, _ in ivd if d <= b["date"]][-1] == b["date"]:
                rec["vol"]["iv"] = _r(hist[-1], 2)
                rec["vol"]["iv_rank"] = _r(range_rank(hist, hist[-1]), 1)
                rec["vol"]["iv_source"] = "approximate ATM IV from the archived F&O bhavcopy (%d sessions)" % len(hist)
        rec["breadth"] = (breadth or {}).get(b["date"])
        rec["calendar"] = calendar_flags(symbol, b["date"], kind, root, closed, events, meetings, api)
        rec["words"] = words(rec)
        out[b["date"]] = rec
    return out


def regime_on(symbol, day, bars, vix_bars=None, **kw):
    """The regime of one day from bars that may run past it: later bars are cut off first."""
    k = _day(day).isoformat()
    cut = [b for b in bars if b["date"] <= k]
    got = daily_regimes(cut, [b for b in vix_bars or [] if b["date"] <= k] or None, symbol, start=k, end=k, **kw)
    return got.get(k)


def regime_key(rec, expiry=True):
    """The lab's split key: "trend|low|expiry day" (trend or range, the vol bucket, the expiry flag)."""
    if not rec:
        return "unknown|unknown|unknown" if expiry else "unknown|unknown"
    t = rec["trend"]["class"]
    v = rec["vol"]["bucket"]
    if not expiry:
        return "%s|%s" % (t, v)
    return "%s|%s|%s" % (t, v, expiry_flag(rec))


def expiry_flag(rec):
    e = (rec.get("calendar") or {}).get("expiry") or {}
    if not e.get("known"):
        return "expiry unknown"
    if e.get("today"):
        return "expiry day"
    if e.get("next_session"):
        return "day before expiry"
    return "no expiry"


def label_trades(trades, regimes, date_key="signal_date"):
    """Each trade's regime as of the close BEFORE its signal day (no look-ahead for intraday signals), with
    the signal day's own calendar (expiry is known in advance). -> [(trade, key, record)]."""
    dates = sorted(regimes)
    out = []
    for t in trades:
        d = t[date_key]
        k = bisect.bisect_left(dates, d)
        prev = regimes[dates[k - 1]] if k > 0 else None
        same = regimes.get(d)
        if prev is None:
            out.append((t, "unknown|unknown|" + (expiry_flag(same) if same else "expiry unknown"), None))
            continue
        flag = expiry_flag(same) if same else expiry_flag({"calendar": {"expiry": expiry_info(prev["symbol"], d)}})
        out.append((t, "%s|%s|%s" % (prev["trend"]["class"], prev["vol"]["bucket"], flag), prev))
    return out


def _ci95(rs):
    n = len(rs)
    if n < 2:
        return None
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1))
    h = 1.96 * sd / math.sqrt(n)
    return [round(m - h, 4), round(m + h, 4)]


def split_by_regime(trades, regimes, min_trades=10):
    """Trades grouped by regime key: [{"regime", "trend", "vol", "expiry", "trades", "net", "expectancy_r",
    "ci95_r", "win_rate", "enough"}], biggest group first. ci95_r is mean R +/- 1.96 standard errors."""
    groups = {}
    for t, key, _ in label_trades(trades, regimes):
        groups.setdefault(key, []).append(t)
    rows = []
    for key, ts in groups.items():
        rs = [float(x["r"]) for x in ts]
        nets = [float(x["net"]) for x in ts]
        tr, vb, ex = key.split("|")
        rows.append({"regime": "%s · %s vol · %s" % (tr, vb, ex), "key": key, "trend": tr, "vol": vb, "expiry": ex,
                     "trades": len(ts), "net": round(sum(nets), 2), "expectancy_r": round(sum(rs) / len(rs), 4),
                     "ci95_r": _ci95(rs), "win_rate": round(sum(1 for x in nets if x > 0) / float(len(ts)), 4),
                     "enough": len(ts) >= min_trades})
    rows.sort(key=lambda r: (-r["trades"], r["key"]))
    return rows


def edge_in_regime(trades, regimes, current, min_trades=10, min_r=0.0):
    """The opt-in lab gate: trades whose regime (trend x vol, as of the close before) matches `current`
    -> {"regime", "trades", "expectancy_r", "ci95_r", "pass", "why"}."""
    want = "|".join(current.split("|")[:2])
    ts = [t for t, key, _ in label_trades(trades, regimes) if "|".join(key.split("|")[:2]) == want]
    rs = [float(t["r"]) for t in ts]
    m = sum(rs) / len(rs) if rs else None
    ok = len(ts) >= min_trades and m is not None and m > min_r
    why = ("%d trades in %s (%s), mean %.3fR" % (len(ts), want.replace("|", " · "), "enough" if len(ts) >= min_trades
                                               else "fewer than %d" % min_trades, m)) if rs else \
        "no trades in %s" % want.replace("|", " · ")
    return {"regime": want, "trades": len(ts), "expectancy_r": None if m is None else round(m, 4),
            "ci95_r": _ci95(rs), "pass": ok, "why": why}


# ---------------------------------------------------------------- plain words

def words(rec):
    """'NIFTY: trending up (ADX 27), low vol (VIX 34th pct), weekly expiry tomorrow'."""
    t, v = rec["trend"], rec["vol"]
    parts = []
    adx_s = ("ADX %d" % round(t["adx"])) if t["adx"] is not None else "ADX n/a"
    parts.append({"trend_up": "trending up (%s)", "trend_down": "trending down (%s)", "range": "ranging (%s)",
                  "transition": "no clear trend (%s)", "unknown": "trend unknown (too little history, %s)"}
                 [t["label"]] % adx_s)
    if v["bucket"] != "unknown":
        if v["basis"].startswith("India VIX"):
            parts.append("%s vol (VIX %s, %s pct)" % (v["bucket"], round(v["vix"], 1), ordinal(v["vix_pct"])))
        else:
            parts.append("%s vol (20-day realised %s%%, %s pct)" % (v["bucket"], round(v["rv20"], 1), ordinal(v["rv_pct"])))
    else:
        parts.append("vol unknown")
    cal = rec.get("calendar") or {}
    e = cal.get("expiry") or {}
    if e.get("today"):
        parts.append("%s expiry today" % e["today"])
    elif e.get("next_session"):
        parts.append("%s expiry %s" % (e["next_session"], "tomorrow" if _is_tomorrow(rec["date"], cal) else
                                       "next session (%s)" % cal.get("next_session")))
    if rec["gap"]["type"] not in ("no gap", "unknown"):
        parts.append("%s %+.2f%%" % (rec["gap"]["type"], rec["gap"]["pct"]))
    for ev in cal.get("events") or []:
        parts.append("%s today" % ev.get("name"))
    for ev in cal.get("events_next_session") or []:
        parts.append("%s next session" % ev.get("name"))
    if cal.get("results_today"):
        parts.append("results day (board meeting)")
    elif cal.get("results_next_session"):
        parts.append("results next session")
    elif cal.get("after_results"):
        parts.append("first session after results")
    if cal.get("holiday_before_next_session"):
        parts.append("holiday before the next session (%s)" % cal["holiday_before_next_session"])
    if cal.get("special_session"):
        parts.append("special session: %s" % cal["special_session"])
    return "%s: %s" % (rec["symbol"], ", ".join(parts))


def _is_tomorrow(day, cal):
    n = cal.get("next_session")
    return bool(n) and (_day(n) - _day(day)).days == 1


def ordinal(x):
    if x is None:
        return "n/a"
    n = int(round(x))
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suf)


def explain_rows(rec, cfg=None):
    """[(input, value, threshold / rule, what it says)] for one day's record."""
    cfg = cfg or DEFAULTS
    t, v, g = rec["trend"], rec["vol"], rec["gap"]
    rows = [
        ("ADX(%d)" % cfg["adx_period"], t["adx"], ">= %g trend, < %g range" % (cfg["adx_trend"], cfg["adx_range"]),
         "trend strength (direction-blind)"),
        ("+DI / -DI", "%s / %s" % (t["plus_di"], t["minus_di"]), "+DI above -DI: buyers lead", ""),
        ("efficiency ratio(%d)" % cfg["er_period"], t["er"], ">= %g trend, < %g range" % (cfg["er_trend"], cfg["er_range"]),
         "net move / path travelled"),
        ("EMA %d vs EMA %d" % (cfg["ema_fast"], cfg["ema_slow"]), "%s vs %s" % (t["ema_fast"], t["ema_slow"]),
         "fast above slow and close above slow: up", ""),
        ("EMA %d slope, %d sessions" % (cfg["ema_fast"], cfg["slope_sessions"]),
         None if t["ema_fast_slope_pct"] is None else "%+.3f%%" % t["ema_fast_slope_pct"], "> 0 up, < 0 down", ""),
        ("close vs %d-day average" % cfg["sma_long"], None if t["vs_sma_long_pct"] is None else
         "%+.2f%%" % t["vs_sma_long_pct"], "above or below", "the long-term side"),
        ("trend verdict", t["label"], "direction agreed and (ADX or ER at trend level)", "transition counts as range"),
    ]
    if v.get("vix") is not None:
        rows.append(("India VIX", v["vix"], "percentile over %d sessions" % cfg["vol_lookback"], ""))
        rows.append(("VIX percentile", v["vix_pct"], "< %g low, >= %g high" % (cfg["vol_low_pct"], cfg["vol_high_pct"]),
                     "share of the last year's closes below today"))
    rows.append(("realised vol %dd" % cfg["rv_short"], v["rv20"], "annualised %", ""))
    rows.append(("realised vol percentile", v["rv_pct"], "< %g low, >= %g high" % (cfg["vol_low_pct"], cfg["vol_high_pct"]),
                 "the bucket for a stock"))
    rows.append(("realised vol 20d / 1y", v["rv_ratio"], "> 1: busier than usual", ""))
    rows.append(("IV rank", v.get("iv_rank"), "0 = year's low, 100 = high", v.get("iv_source") or "not available"))
    rows.append(("vol bucket", v["bucket"], v["basis"], ""))
    rows.append(("gap", "%s (%s ATR)" % (g["pct"], g["atr_mult"]) if g["pct"] is not None else None,
                 ">= %g ATR gap, >= %g big" % (cfg["gap_atr"], cfg["gap_big_atr"]), g["type"]))
    if rec.get("breadth"):
        b = rec["breadth"]
        rows.append(("breadth", "%d up / %d down" % (b["advances"], b["declines"]),
                     ">= %g advancing: broad" % cfg["breadth_strong"], b["label"]))
    e = (rec.get("calendar") or {}).get("expiry") or {}
    rows.append(("expiry", "today: %s; next session: %s; next %s (%s)" % (e.get("today"), e.get("next_session"),
                                                                        e.get("next_expiry"), e.get("next_kind")),
                 "", e.get("source") or ""))
    return rows


# ---------------------------------------------------------------- data: the archive, offline files, Yahoo

def archive_api():
    """trader-archive's offline read API (the sibling folder), or None when it is not installed."""
    name = "bt_trader_archive"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), "trader-archive", "archive.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def read_bars_file(path):
    """SYMBOL-1d.csv(.gz) (date,time,open,high,low,close,volume) -> bars."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    out = []
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        try:
            out.append({"date": r["date"], "time": r.get("time") or "", "o": float(r["open"]), "h": float(r["high"]),
                        "l": float(r["low"]), "c": float(r["close"]), "v": int(float(r.get("volume") or 0))})
        except (KeyError, ValueError):
            continue
    return out


def file_name(symbol):
    return INDEX_YAHOO.get(symbol.upper(), symbol.upper())


def yahoo_daily(symbol, now, timeout=25):
    """Two years of Yahoo daily bars (today's dropped until 15:30 IST)."""
    q = urllib.parse.quote(file_name(symbol) if symbol.upper() in INDEX_YAHOO else symbol.upper() + ".NS", safe=".-")
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d" % q
    return _bars_from_yahoo(_get(url, {"User-Agent": YAHOO_UA, "Accept": "application/json"}, timeout), now, "1d")


def yahoo_5m(symbol, now, timeout=25):
    q = urllib.parse.quote(file_name(symbol) if symbol.upper() in INDEX_YAHOO else symbol.upper() + ".NS", safe=".-")
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m" % q
    return _bars_from_yahoo(_get(url, {"User-Agent": YAHOO_UA, "Accept": "application/json"}, timeout), now, "5m")


_last_call = [0.0]


def _get(url, headers, timeout=25):
    for attempt in (0, 1):
        gap = time.time() - _last_call[0]
        if gap < PAUSE:
            time.sleep(PAUSE - gap)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers=dict(headers, **{"Accept-Encoding": "gzip"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt == 0:
                time.sleep(BACKOFF)
                continue
            raise RegimeError("HTTP %s from %s" % (e.code, url.split("/")[2]))
        except (urllib.error.URLError, OSError) as e:
            raise RegimeError("could not reach %s: %s" % (url.split("/")[2], getattr(e, "reason", None) or e))
    raise RegimeError("blocked by %s: try again later" % url.split("/")[2])


def _bars_from_yahoo(data, now, tf):
    try:
        res = json.loads(data.decode("utf-8"))["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RegimeError("not a Yahoo chart response")
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            continue
        local = datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)
        if tf == "1d":
            if local.date() == now.date() and now.hour * 60 + now.minute < CLOSE_MIN:
                continue
            key = (local.date().isoformat(), "")
        else:
            if local + datetime.timedelta(minutes=5) > now:
                continue
            key = (local.date().isoformat(), local.strftime("%H:%M"))
        out[key] = {"date": key[0], "time": key[1], "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                    "c": round(c, 2), "v": int(v or 0)}
    return [out[k] for k in sorted(out)]


class Sources(object):
    """Where bars come from: --data DIR (offline files), the archive, then Yahoo (unless offline)."""

    def __init__(self, archive=None, data_dir=None, fetch=True, workdir="trader"):
        self.root = archive if archive and os.path.isdir(archive) else None
        self.data_dirs = [x for x in str(data_dir).split(",") if x] if data_dir else []
        self.data_dir = data_dir
        self.fetch = fetch and not data_dir
        self.workdir = workdir
        self.api = archive_api() if self.root else None
        if self.root and self.api is None:
            self.root = None
        self.notes = []

    def closed(self):
        if not self.root:
            return None
        return self.api.Store(self.root).calendar().get("closed") or None

    def daily(self, symbol, now):
        """(bars up to now's last complete session, source text)."""
        sym = symbol.upper()
        bars, src = None, None
        if self.data_dir:
            for d in self.data_dirs:
                for ext in (".csv.gz", ".csv"):
                    p = os.path.join(d, "%s-1d%s" % (file_name(sym), ext))
                    if bars is None and os.path.isfile(p):
                        bars, src = read_bars_file(p), os.path.basename(p)
            if bars is None:
                raise RegimeError("no %s-1d.csv(.gz) in %s" % (file_name(sym), self.data_dir))
        if bars is None and self.root:
            try:
                if sym == "VIX":
                    got = self.api.load_vix(root=self.root)
                elif sym in INDEX_ARCHIVE:
                    got = self.api.load_index(INDEX_ARCHIVE[sym], root=self.root)
                else:
                    got = self.api.load_daily(sym, root=self.root)
            except (self.api.ArchiveError, OSError, ValueError):
                got = []
            if got:
                bars = [{"date": b["date"], "time": "", "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                         "v": int(b.get("v") or 0)} for b in got]
                src = "archive %s to %s" % (bars[0]["date"], bars[-1]["date"])
        if bars is None:
            if not self.fetch:
                raise RegimeError("no daily bars for %s: no archive (python3 trader-archive/archive.py backfill) and "
                                  "fetching is off" % sym)
            bars, src = yahoo_daily(sym, now), "Yahoo %s (2 years, unofficial)" % file_name(sym)
        cut = now.date().isoformat()
        done = now.hour * 60 + now.minute >= CLOSE_MIN
        today = [b for b in bars if b["date"] == cut]
        bars = [b for b in bars if b["date"] < cut or (b["date"] == cut and done)]
        return bars, src, (today[0] if today and not done else None)

    def intraday(self, symbol, now):
        if self.root:
            try:
                got = self.api.load_intraday(symbol.upper(), "5m", now.date().isoformat(), now.date().isoformat(),
                                             root=self.root)
                if got:
                    return got, "archive 5m"
            except (self.api.ArchiveError, OSError, ValueError):
                pass
        if self.fetch:
            try:
                return yahoo_5m(symbol, now), "Yahoo 5m"
            except RegimeError as e:
                self.notes.append("intraday: %s" % e)
        return [], None

    def breadth(self, day):
        if not self.root:
            return None
        p = self.api.Store(self.root).path("eq", _day(day))
        if not os.path.exists(p):
            return None
        return breadth_from_eq(self.api.read_gz_csv(p))

    def board(self, symbol, now):
        """Board meetings for a stock: the cache trader/regime/board/SYMBOL.json, refreshed from NSE once a day
        when fetching is on. -> [{"date", "purpose"}] or None."""
        path = os.path.join(self.workdir, "regime", "board", "%s.json" % symbol.upper())
        doc = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                doc = None
        if self.data_dir:
            for d in self.data_dirs:
                p = os.path.join(d, "board-%s.json" % symbol.upper())
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        return parse_board(json.load(fh))
            return doc.get("meetings") if doc else None
        if self.fetch and (not doc or doc.get("fetched") != now.date().isoformat()):
            try:
                raw = json.loads(_get(BOARD_URL % urllib.parse.quote(symbol.upper()),
                                      {"User-Agent": NSE_UA, "Accept": "application/json",
                                       "Referer": "https://www.nseindia.com/"}).decode("utf-8"))
                doc = {"source": BOARD_URL % symbol.upper(), "fetched": now.date().isoformat(),
                       "meetings": parse_board(raw)}
                _write_json(path, doc)
            except (RegimeError, ValueError) as e:
                self.notes.append("board meetings for %s: %s%s" % (symbol, e, " (using the cache of %s)" % doc["fetched"]
                                                                  if doc else ""))
        return doc.get("meetings") if doc else None


def parse_board(raw):
    """NSE corporate-board-meetings JSON -> [{"date": YYYY-MM-DD, "purpose"}] (one per date and purpose)."""
    out, seen = [], set()
    for b in raw if isinstance(raw, list) else (raw or {}).get("data") or []:
        if not isinstance(b, dict):
            continue
        try:
            d = datetime.datetime.strptime(str(b.get("bm_date") or ""), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        purpose = " ".join(str(b.get("bm_purpose") or "").split())
        if purpose.lower() == "board meeting intimation":
            desc = str(b.get("bm_desc") or "")
            purpose = "Financial Results (intimation)" if re.search(r"result", desc, re.I) else purpose
        if (d, purpose) not in seen:
            seen.add((d, purpose))
            out.append({"date": d, "purpose": purpose})
    return sorted(out, key=lambda m: m["date"])


def breadth_from_eq(header_rows, cfg=None):
    """Advances and declines among EQ-series stocks of one equity bhavcopy (archive schema)."""
    cfg = cfg or DEFAULTS
    head, rows = header_rows
    try:
        i_s, i_c, i_p = head.index("series"), head.index("close"), head.index("prev_close")
    except ValueError:
        return None
    adv = dec = unch = 0
    for r in rows:
        if len(r) <= max(i_s, i_c, i_p) or r[i_s] != "EQ":
            continue
        try:
            c, p = float(r[i_c]), float(r[i_p])
        except ValueError:
            continue
        if p <= 0:
            continue
        if c > p:
            adv += 1
        elif c < p:
            dec += 1
        else:
            unch += 1
    if adv + dec == 0:
        return None
    share = adv / float(adv + dec)
    s = cfg["breadth_strong"]
    label = "broad advance" if share >= s else ("broad decline" if share <= 1 - s else "mixed")
    return {"advances": adv, "declines": dec, "unchanged": unch, "advancing_share": round(share, 3), "label": label}


def atm_iv(chain, day):
    """An approximate ATM implied vol (%) from one archived end-of-day F&O board: the nearest expiry at least
    5 calendar days out, the strike nearest spot, IV ~ straddle / (0.8 x spot x sqrt(T)) (Brenner and
    Subrahmanyam 1988). Settlement prices; None when the board is thin."""
    if not chain or not chain.get("spot"):
        return None
    spot = float(chain["spot"])
    d0 = _day(day)
    exps = sorted({o["expiry"] for o in chain["options"] if o.get("expiry") and (_day(o["expiry"]) - d0).days >= 5})
    for e in exps[:1]:
        rows = [o for o in chain["options"] if o["expiry"] == e]
        strikes = sorted({o["strike"] for o in rows if o.get("strike")}, key=lambda k: abs(k - spot))
        for k in strikes[:1]:
            ce = [o for o in rows if o["strike"] == k and o["option_type"] == "CE"]
            pe = [o for o in rows if o["strike"] == k and o["option_type"] == "PE"]
            if not ce or not pe:
                return None
            px = lambda o: o.get("settle") or o.get("close")
            if not px(ce[0]) or not px(pe[0]):
                return None
            T = ((_day(e) - d0).days + 0.0) / 365.0
            return 100.0 * (px(ce[0]) + px(pe[0])) / (0.8 * spot * math.sqrt(T))
    return None


def iv_history(symbol, src, start, end):
    """{date: approximate ATM IV} for a stock from the archive, cached in archive/.cache/regime-iv/."""
    if not src.root:
        return {}
    cpath = os.path.join(src.root, ".cache", "regime-iv", "%s.json" % symbol.upper())
    try:
        with open(cpath, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    st = src.api.Store(src.root)
    changed = False
    for k in st.dates("fo"):
        if k < start or k > end or k in cache:
            continue
        cache[k] = atm_iv(src.api.load_fo_chain_eod(symbol, k, root=src.root), k)
        changed = True
    if changed:
        try:
            _write_json(cpath, cache)
        except OSError:
            pass
    return {k: v for k, v in cache.items() if v and start <= k <= end}


def _write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the reads behind the commands

def build(symbol, src, now, cfg, start=None, end=None, iv=False):
    """{date: record} for one symbol up to now's last complete session, plus (source, today's partial bar)."""
    sym = symbol.upper()
    kind = "index" if sym in INDEX_YAHOO else "stock"
    bars, source, partial = src.daily(sym, now)
    if not bars:
        raise RegimeError("no daily bars for %s up to %s" % (sym, now.date()))
    vix = None
    try:
        vix, _, _ = src.daily("VIX", now)
    except RegimeError as e:
        src.notes.append("India VIX: %s" % e)
    meetings = src.board(sym, now) if kind == "stock" else None
    first = start or bars[-1]["date"]
    last = end or bars[-1]["date"]
    breadth = {}
    if src.root:
        for b in bars:
            if first <= b["date"] <= last:
                got = src.breadth(b["date"])
                if got:
                    breadth[b["date"]] = got
    ivd = iv_history(sym, src, "0000", last) if (iv and kind == "stock") else None
    regs = daily_regimes(bars, vix, sym, kind, cfg, src.root, src.closed(), None, meetings, breadth, ivd, src.api,
                         first, last)
    return regs, source, partial, bars


def read_now(symbol, src, now, cfg, iv=False):
    """The regime 'now': the last complete session's record, plus today's calendar, gap and time bucket when
    `now` is on a session day before the close."""
    regs, source, partial, bars = build(symbol, src, now, cfg, iv=iv)
    last = regs[max(regs)]
    sym = symbol.upper()
    kind = last["kind"]
    out = {"schema": SCHEMA, "symbol": sym, "as_of": now.strftime("%Y-%m-%d %H:%M"), "source": source,
           "daily": last, "words": last["words"], "today": None, "config_hash": config_hash(cfg)}
    today = now.date()
    if is_session(today) and today.isoformat() > last["date"]:
        events = builtin_events() + list(cfg.get("events") or [])
        meetings = src.board(sym, now) if kind == "stock" else None
        cal = calendar_flags(sym, today, kind, src.root, src.closed(), events, meetings, src.api)
        seg = "DERIV" if kind == "index" else "EQ_FNO"
        tb = time_bucket(now, seg, cfg)
        gap = {"type": "unknown", "pct": None, "atr_mult": None}
        intr = None
        if tb["bucket"] not in ("pre-market", "pre-open"):
            ib, isrc = src.intraday(sym, now)
            open_ = partial["o"] if partial else (ib[0]["o"] if [b for b in ib if b["date"] == today.isoformat()] else None)
            if open_ is None and ib:
                todays = [b for b in ib if b["date"] == today.isoformat()]
                open_ = todays[0]["o"] if todays else None
            gap = gap_type(open_, last["close"], last["atr"], cfg)
            intr = intraday_read(ib, now, last["close"], last["atr"])
            if intr:
                intr["source"] = isrc
        rec = {"date": today.isoformat(), "symbol": sym, "kind": kind, "trend": last["trend"], "vol": last["vol"],
               "gap": gap, "calendar": cal, "time": tb, "intraday": intr, "breadth": None, "close": last["close"],
               "atr": last["atr"]}
        rec["words"] = words(rec) + "; now: " + tb["bucket"]
        if intr:
            rec["words"] += ", %+.2f%% so far, range %s ATR" % (intr["move_pct"] or 0.0, intr["range_atr"])
        out["today"] = rec
        out["words"] = rec["words"] + " (trend and vol as of the %s close)" % last["date"]
    return out


# ---------------------------------------------------------------- storage: trader/regime/*.json and the archive table

TABLE_COLS = ["symbol", "trend", "direction", "adx", "er", "above_sma_long", "vol_bucket", "vix_pct", "rv20",
              "rv_ratio", "iv_rank", "gap", "gap_atr", "expiry_today", "expiry_next_session", "events", "words"]


def compact(rec):
    return {"trend": rec["trend"]["label"], "class": rec["trend"]["class"], "direction": rec["trend"]["direction"],
            "adx": rec["trend"]["adx"], "er": rec["trend"]["er"], "above_sma_long": rec["trend"]["above_sma_long"],
            "vol": rec["vol"]["bucket"], "vix_pct": rec["vol"]["vix_pct"], "rv20": rec["vol"]["rv20"],
            "rv_ratio": rec["vol"]["rv_ratio"], "iv_rank": rec["vol"].get("iv_rank"), "gap": rec["gap"]["type"],
            "gap_atr": rec["gap"]["atr_mult"], "expiry": expiry_flag(rec),
            "events": [e.get("kind") for e in rec["calendar"]["events"]] +
                      (["results"] if rec["calendar"].get("results_today") else []),
            "breadth": (rec.get("breadth") or {}).get("label"), "words": rec["words"]}


def store_history(workdir, symbol, regs, cfg, source):
    """trader/regime/SYMBOL.json: {date: compact record}, merged with what is there."""
    path = os.path.join(workdir, "regime", "%s.json" % symbol.upper())
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    if doc.get("config_hash") != config_hash(cfg):
        doc = {}
    days = doc.get("days") or {}
    days.update({k: compact(v) for k, v in regs.items()})
    doc = {"schema": SCHEMA, "symbol": symbol.upper(), "config_hash": config_hash(cfg), "source": source,
           "from": min(days), "to": max(days), "days": {k: days[k] for k in sorted(days)}}
    _write_json(path, doc)
    return path


def store_table(root, api, regs, now):
    """archive/regime/YYYY/YYYY-MM-DD.csv.gz: one row per symbol, merged with the rows other symbols left."""
    st = api.Store(root)
    n = 0
    for k, rec in sorted(regs.items()):
        c = compact(rec)
        row = [rec["symbol"], c["trend"], c["direction"] or "", c["adx"], c["er"], c["above_sma_long"], c["vol"],
               c["vix_pct"], c["rv20"], c["rv_ratio"], c["iv_rank"], c["gap"], c["gap_atr"],
               rec["calendar"]["expiry"].get("today") or "", rec["calendar"]["expiry"].get("next_session") or "",
               ";".join(x for x in c["events"] if x), rec["words"]]
        row = ["" if x is None else x for x in row]
        p = st.path("regime", k)
        rows = []
        if os.path.exists(p):
            rows = [r for r in api.read_gz_csv(p)[1] if r and r[0] != rec["symbol"]]
        rows.append([str(x) for x in row])
        rows.sort()
        st.put_rows("regime", k, TABLE_COLS, rows, {"source": "trader-regime history", "fetched_at":
                                                   now.strftime("%Y-%m-%dT%H:%M:%S+05:30")})
        n += 1
    return n


def load_table(symbol, start=None, end=None, root=None):
    """The archive table back: [{"date", column: value}] for one symbol."""
    api = archive_api()
    if api is None or not root:
        return []
    st = api.Store(root)
    out = []
    for k in st.dates("regime"):
        if (start and k < start) or (end and k > end):
            continue
        head, rows = api.read_gz_csv(st.path("regime", k))
        for r in rows:
            if r and r[0] == symbol.upper():
                out.append(dict(zip(["date"] + head, [k] + r)))
    return out


# ---------------------------------------------------------------- command line

def parse_now(text):
    if not text:
        return datetime.datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(text[:16] if "T" in text or " " in text else text[:10], fmt)
            return d.replace(hour=16, minute=0) if fmt == "%Y-%m-%d" else d
        except ValueError:
            continue
    raise RegimeError("--now is YYYY-MM-DD HH:MM (IST), not %r" % text)


def _sources(a):
    archive = None if a.no_archive else (a.archive or os.environ.get("BT_ARCHIVE") or os.path.join(a.workdir, "archive"))
    src = Sources(archive, a.data, not a.offline, a.workdir)
    return src


def _symbols(a):
    out = []
    for s in a.symbols or list(DEFAULT_SYMBOLS):
        for x in s.split(","):
            x = x.strip().upper()
            if x:
                if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", x):
                    raise RegimeError("not an NSE symbol: %r" % x)
                out.append(x)
    return out


def render_now(r):
    L = [r["words"]]
    d = r["daily"]
    t, v = d["trend"], d["vol"]
    L.append("  close %s on %s (%s); ADX %s, ER %s, %s the %d-day average (%s%%)" % (
        num(d["close"]), d["date"], r["source"], t["adx"], t["er"],
        "above" if t["above_sma_long"] else ("below" if t["above_sma_long"] is not None else "n/a vs"),
        DEFAULTS["sma_long"], t["vs_sma_long_pct"]))
    L.append("  vol: %s by %s; realised %s%% (20d) vs %s%% (1y)%s" % (
        v["bucket"], v["basis"], v["rv20"], v["rv_long"],
        "; IV rank %s (%s)" % (v["iv_rank"], v["iv_source"]) if v.get("iv_rank") is not None else ""))
    rec = r["today"] or d
    cal = rec["calendar"]
    e = cal["expiry"]
    how = e["source"]
    if how.startswith("rule: "):
        how = "the dated weekday rule: no archived F&O bhavcopy for the day"
    L.append("  expiry: %s (%s)" % ("next %s %s, %s sessions away" % (e["next_kind"], e["next_expiry"],
                                                                       e["sessions_to_expiry"])
                                   if e.get("next_expiry") else "not known", how))
    if cal.get("session") and cal["session"].get("closing_auction"):
        L.append("  closing auction (F&O stocks): %s; %s continuous trading ends %s" % (
            cal["session"]["closing_auction"], cal["session"]["segment"], cal["session"]["continuous_end"]))
    if r["today"] and r["today"].get("time"):
        tb = r["today"]["time"]
        L.append("  time: %s%s%s" % (tb["bucket"], " (%s-%s)" % (tb["from"], tb["to"]) if tb["from"] and tb["to"] else "",
                                     "; " + tb["note"] if tb["note"] else ""))
    if d.get("breadth"):
        b = d["breadth"]
        L.append("  breadth %s: %d up, %d down (%s)" % (d["date"], b["advances"], b["declines"], b["label"]))
    return "\n".join(L)


def cmd_now(a):
    cfg = load_config(a.config or os.path.join(a.workdir, "regime-config.json"))
    now = parse_now(a.now)
    src = _sources(a)
    out, errors = [], []
    for s in _symbols(a):
        try:
            out.append(read_now(s, src, now, cfg, a.iv))
        except RegimeError as e:
            errors.append("%s: %s" % (s, e))
    if a.json:
        print(json.dumps({"schema": SCHEMA, "as_of": now.strftime("%Y-%m-%d %H:%M"), "regimes": out,
                          "errors": errors, "notes": src.notes,
                          "disclaimer": "Education and tooling, not financial advice."}, indent=1, ensure_ascii=False))
    else:
        for r in out:
            print(render_now(r))
            print("")
        for e in errors + src.notes:
            print("Note: %s" % e)
        print("A regime describes the market so far; it does not predict the next move.")
    return 0 if out else 1


def cmd_history(a):
    cfg = load_config(a.config or os.path.join(a.workdir, "regime-config.json"))
    now = parse_now(a.now)
    src = _sources(a)
    start = _day(a.start).isoformat() if a.start else "0000-00-00"
    end = _day(a.end).isoformat() if a.end else None
    summary = []
    for s in _symbols(a):
        regs, source, _, _ = build(s, src, now, cfg, start, end, a.iv)
        if not regs:
            raise RegimeError("%s: no sessions between %s and %s" % (s, start, end))
        item = {"symbol": s, "from": min(regs), "to": max(regs), "days": len(regs), "source": source,
                "counts": {}}
        for rec in regs.values():
            key = regime_key(rec)
            item["counts"][key] = item["counts"].get(key, 0) + 1
        if not a.no_store:
            item["file"] = store_history(a.workdir, s, regs, cfg, source)
            if src.root:
                item["archive_rows"] = store_table(src.root, src.api, regs, now)
        if a.json:
            item["regimes"] = {k: compact(v) for k, v in regs.items()}
        summary.append(item)
    if a.json:
        print(json.dumps({"schema": SCHEMA, "history": summary, "notes": src.notes}, indent=1, ensure_ascii=False))
    else:
        for it in summary:
            print("%s: %d sessions %s to %s (%s)" % (it["symbol"], it["days"], it["from"], it["to"], it["source"]))
            for k, n in sorted(it["counts"].items(), key=lambda kv: -kv[1]):
                print("  %4d  %s" % (n, k.replace("|", " · ")))
            if it.get("file"):
                print("  wrote %s%s" % (it["file"], "; archive table: %d days" % it["archive_rows"]
                                        if it.get("archive_rows") else ""))
        for n in src.notes:
            print("Note: %s" % n)
    return 0


def cmd_explain(a):
    cfg = load_config(a.config or os.path.join(a.workdir, "regime-config.json"))
    now = parse_now(a.now)
    src = _sources(a)
    syms = _symbols(a)[:1] if a.symbols else ["NIFTY"]
    r = read_now(syms[0], src, now, cfg, a.iv)
    rows = explain_rows(r["daily"], cfg)
    if a.json:
        print(json.dumps({"schema": SCHEMA, "symbol": syms[0], "date": r["daily"]["date"], "words": r["words"],
                          "inputs": [{"input": x[0], "value": x[1], "rule": x[2], "meaning": x[3]} for x in rows],
                          "thresholds": {k: v for k, v in cfg.items() if k != "events"},
                          "config_hash": config_hash(cfg)}, indent=1, ensure_ascii=False))
        return 0
    print(r["words"])
    print("")
    print("Inputs for %s on %s (each uses data up to that day's close only):" % (syms[0], r["daily"]["date"]))
    for name, val, rule, meaning in rows:
        print("  %-30s %-28s %s%s" % (name, val, rule, ("  (%s)" % meaning) if meaning else ""))
    print("")
    print("Thresholds (trader/regime-config.json overrides any): %s" %
          ", ".join("%s=%s" % (k, v) for k, v in sorted(cfg.items()) if k != "events"))
    return 0


def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    utf8_console()
    p = argparse.ArgumentParser(prog="regime.py", description="Market regime: trend, volatility, gap, expiry and "
                                "event days, time of day, for NSE indices and stocks. Analytics only.")
    sub = p.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("symbols", nargs="*", help="NIFTY, BANKNIFTY or NSE stocks (default NIFTY BANKNIFTY)")
    common.add_argument("--workdir", default="trader", help="the trader/ folder (default trader)")
    common.add_argument("--archive", help="a trader-archive folder (default trader/archive or $BT_ARCHIVE)")
    common.add_argument("--no-archive", action="store_true", help="ignore the archive")
    common.add_argument("--data", help="offline: folders (comma separated) of SYMBOL-1d.csv(.gz) files (^NSEI, ^NSEBANK, "
                        "^INDIAVIX) and board-SYMBOL.json")
    common.add_argument("--offline", action="store_true", help="no network at all (archive or --data only)")
    common.add_argument("--config", help="thresholds file (default trader/regime-config.json)")
    common.add_argument("--now", help="pin the clock, IST: YYYY-MM-DD HH:MM")
    common.add_argument("--iv", action="store_true", help="a stock's IV rank from the archived F&O bhavcopy (slower)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("now", parents=[common], help="today's regime in plain words")
    h = sub.add_parser("history", parents=[common], help="every day's regime, stored in trader/regime/ and the archive")
    h.add_argument("--from", dest="start", help="first day YYYY-MM-DD")
    h.add_argument("--to", dest="end", help="last day YYYY-MM-DD")
    h.add_argument("--no-store", action="store_true", help="print only, write nothing")
    sub.add_parser("explain", parents=[common], help="each input, its value and its threshold")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        return {"now": cmd_now, "history": cmd_history, "explain": cmd_explain}[a.cmd](a)
    except (RegimeError, RulesError) as e:
        sys.stderr.write("regime.py: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
