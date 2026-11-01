#!/usr/bin/env python3
"""Strategy Lab for trader-lab: honest backtests on NSE bars. Standard library only.

A strategy earns a place in your playbook only with evidence that survives
realistic costs and out-of-sample testing. This tool gathers that evidence and
says plainly what it does NOT prove. It never promises a profit and it never
places an order.

    python3 lab.py list                                  # the built-in library and your lab runs
    python3 lab.py show orb15-vwap                       # one spec, as JSON
    python3 lab.py validate my-spec.json                 # check a spec against the rule vocabulary
    python3 lab.py run orb15-vwap                        # fetch bars, run everything, write trader/lab/orb15-vwap/
    python3 lab.py run orb15-vwap --data DIR             # offline: DIR holds SYMBOL-5m.csv(.gz) / SYMBOL-1d.csv(.gz)
    python3 lab.py report orb15-vwap                     # the plain-words scorecard
    python3 lab.py compare orb15-vwap rsi2-nifty50       # key numbers side by side
    python3 lab.py forward orb15-vwap                    # today's signals -> draft plans for trader-budget

What a run does (docs/lab.md has the method and the why):

  1. Spec. Plain JSON: universe, timeframe (5m intraday MIS or 1d swing CNC),
     entry and exit rules in a small safe vocabulary (indicators, comparisons,
     crosses, time windows; nothing is ever eval'd), stop and target, max
     holding, flat-by, sizing, and the gates, written down BEFORE the run.
  2. Backtest, bar by bar. A signal on a bar's close fills at the NEXT bar's
     open plus slippage. Stops fill at the stop less slippage (at the open if
     the bar gaps through it); both stop and target inside one bar count as the
     stop. Indian charges on every trade (round_trip_charges, the same helper as
     trader-budget). Shares sized like trader-budget: the loss at the stop,
     charges included, fits the max loss per trade. One position per stock,
     MIS flat by the safe flat-by (the closing auction and your broker's
     square-off, from the dated SESSION_RULES), no entries after the cutoff,
     trader-risk's day limits (trades a day, daily loss) for the portfolio.
  3. Robustness. Walk-forward (choose parameters on an in-sample window, score
     them on the next, unseen window, roll on), the parameter grid around the
     spec's point (its neighbourhood, not the best point), Monte Carlo trade
     reshuffles and bootstrap equity bands, the deflated Sharpe ratio (Bailey
     and Lopez de Prado 2014), the probability of backtest overfitting (CSCV,
     Bailey, Borwein, Lopez de Prado and Zhu 2015), a regime split (India VIX
     buckets; NIFTY trending or ranging) and costs at 1.5x and 2x.
  4. Gates and a verdict: REJECT, WATCH (paper forward test first) or PROMOTE
     (only after a forward record). A promoted strategy whose rolling forward
     record decays is DEMOTED automatically.

Output: trader/lab/<name>/spec.json, results.json, scorecard.md, equity.csv,
status.json and forward.json. Same spec, same data, same --seed give the same
numbers on any machine.

Data: the local trader-archive when it is there (trader/archive: years of
split- and bonus-adjusted NSE daily bars, broker intraday candles, NIFTY and
India VIX; --archive DIR, --no-archive), else Yahoo Finance v8 chart
(unofficial, free, can lag): SYMBOL.NS 5-minute bars for about 60 sessions,
or daily bars for 2 years; ^NSEI and ^INDIAVIX daily for the regimes. Fetched
bars are cached in trader/lab/data/ as CSV. Times are IST (UTC+05:30).

Education and tooling, not financial advice.
"""

import argparse
import copy
import datetime
import gzip
import hashlib
import io
import itertools
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
from decimal import Decimal, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; bharat-trader/0.2; +https://github.com/rajdavee/bharat-trader)"
PAUSE = 0.6          # seconds between Yahoo requests
BACKOFF = 5.0        # seconds to wait once after a 429
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PAISE = Decimal("0.01")
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
SCHEMA_SPEC = "bharat-trader/lab-spec/1"
SCHEMA_RESULTS = "bharat-trader/lab-results/1"
HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY_DIR = os.path.join(HERE, "library")
VERDICTS = ("REJECT", "WATCH", "PROMOTE", "DEMOTE")

DEFAULTS = {
    "capital": 100000, "risk_per_trade_pct": 1.0, "max_loss_per_trade": 1000, "max_daily_loss": 3000,
    "max_trades_per_day": 3, "max_position_pct": 50, "max_order_value": 50000, "min_order_value": 500,
    "cash_buffer_pct": 10, "min_reward_risk": 1.5, "no_entries_before": "09:30", "no_entries_after": "14:45",
    "flat_by": "15:10", "broker_profile": "", "square_off_margin_min": 5, "product": "MIS", "tick_size": "auto",
    "include_charges": True,
    "charges": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
                "exchange_txn_pct": 0.00307, "sebi_per_crore": 10, "stamp_buy_pct": 0.003,
                "stamp_delivery_pct": 0.015, "gst_pct": 18},
    "fno_stocks": [],
}

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


# ---------------------------------------------------------------- money

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


def _dec(x):
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(str(x))


def _paise(x):
    return x.quantize(PAISE, rounding=ROUND_HALF_UP)


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
    q = _dec(qty)
    buy, sell = _dec(entry) * q, _dec(exit) * q
    turnover = buy + sell
    cap = _dec(ch["brokerage_cap"])
    bp = _dec(ch["brokerage_pct"]) * pct
    brokerage = min(buy * bp, cap) + min(sell * bp, cap)
    if str((cfg or {}).get("product") or "MIS").upper() == "CNC":
        stt = turnover * _dec(ch["stt_delivery_pct"]) * pct
        stamp = buy * _dec(ch["stamp_delivery_pct"]) * pct
    else:
        stt = sell * _dec(ch["stt_sell_pct"]) * pct
        stamp = buy * _dec(ch["stamp_buy_pct"]) * pct
    exchange = turnover * _dec(ch["exchange_txn_pct"]) * pct
    sebi = turnover * _dec(ch["sebi_per_crore"]) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * _dec(ch["gst_pct"]) * pct
    out = {"brokerage": _paise(brokerage), "stt": _paise(stt), "exchange": _paise(exchange),
           "sebi": _paise(sebi), "stamp": _paise(stamp), "gst": _paise(gst)}
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


class LabError(Exception):
    pass


class SpecError(LabError):
    pass


# ---------------------------------------------------------------- ticks and sizing (as in trader-budget)

TICK_BANDS = ((1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))


def to_paise(x):
    return int(Decimal(repr(float(x))).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def round_paise(x):
    return float(Decimal(repr(float(x))).quantize(PAISE, rounding=ROUND_HALF_UP))


def tick_for(price, setting="auto"):
    """The tick to round a price of this size to (the coarsest tick its NSE price band can have)."""
    if setting not in (None, "", "auto"):
        return float(setting)
    for top, tick in TICK_BANDS:
        if float(price) < top:
            return tick
    return 5.00


def tick_round(x, tick=0.05, how="nearest"):
    """Round a price to the tick: "nearest" (half up) or "down"."""
    p, t = to_paise(x), max(to_paise(tick), 1)
    n = p // t if how == "down" else (p + t // 2) // t
    return round(n * t / 100.0, 2)


def shares_for(rupees, per_share):
    """Whole shares that fit in rupees at per_share, in exact paise."""
    r, p = to_paise(rupees), to_paise(per_share)
    if p <= 0 or r <= 0:
        return 0
    return r // p


def size_qty(entry, stop, cfg, cash=None):
    """trader-budget's sizing, as a pure function: (qty, loss at the stop with charges, the limit that binds).

    The smallest of: the most shares whose loss at the stop, charges included, stays inside
    max_loss_per_trade; the position cap (capital x max_position_pct); max_order_value; and the cash
    after the buffer (cash, else the capital). qty 0 means no trade."""
    capital = float(cfg.get("capital") or 0)
    risk = (to_paise(entry) - to_paise(stop)) / 100.0
    if capital <= 0 or risk <= 0:
        return 0, 0.0, "stop"
    with_charges = bool(cfg.get("include_charges", True))

    def loss(q):
        ch = round_trip_charges(entry, stop, q, cfg)["total"] if (with_charges and q > 0) else 0.0
        return round_paise(round_paise(q * risk) + ch)

    limits = []
    ml = cfg.get("max_loss_per_trade")
    if ml:
        q = shares_for(ml, risk)
        while q > 0 and loss(q) > float(ml) + 0.005:
            q -= 1
        limits.append((q, "max loss"))
    if cfg.get("max_position_pct"):
        limits.append((shares_for(round_paise(capital * float(cfg["max_position_pct"]) / 100.0), entry), "position cap"))
    if cfg.get("max_order_value"):
        limits.append((shares_for(cfg["max_order_value"], entry), "max order"))
    buf = round_paise(capital * float(cfg.get("cash_buffer_pct") or 0) / 100.0)
    avail = capital - buf if cash is None else min(capital - buf, cash - buf)
    limits.append((shares_for(max(round_paise(avail), 0.0), entry), "cash"))
    qty, bound = min(limits, key=lambda x: x[0])
    qty = max(0, int(qty))
    if qty and round_paise(qty * entry) < float(cfg.get("min_order_value") or 0) - 0.005:
        return 0, 0.0, "min order"
    return qty, (loss(qty) if qty else 0.0), bound


def lab_config(sizing=None):
    """The rules a backtest trades under: trader-budget's defaults, then the spec's "sizing".
    capital, risk_per_trade_pct and max_trades_per_day derive the limits the way size.py init does,
    unless the spec sets them itself."""
    s = dict(sizing or {})
    cfg = copy.deepcopy(DEFAULTS)
    capital = float(s.get("capital", cfg["capital"]))
    pct = float(s.get("risk_per_trade_pct", cfg["risk_per_trade_pct"]))
    trades = int(s.get("max_trades_per_day", cfg["max_trades_per_day"]))
    if capital <= 0:
        raise SpecError("sizing.capital must be above 0")
    if not 0 < pct <= 5:
        raise SpecError("sizing.risk_per_trade_pct must be above 0 and at most 5 (trader-budget refuses more)")
    if not 1 <= trades <= 50:
        raise SpecError("sizing.max_trades_per_day must be 1 to 50")
    cfg["capital"], cfg["risk_per_trade_pct"], cfg["max_trades_per_day"] = capital, pct, trades
    cfg["max_loss_per_trade"] = round_paise(capital * pct / 100.0)
    cfg["max_daily_loss"] = round_paise(trades * cfg["max_loss_per_trade"])
    cfg["max_order_value"] = round_paise(capital * float(s.get("max_position_pct", cfg["max_position_pct"])) / 100.0)
    for k, v in s.items():
        if k == "charges" and isinstance(v, dict):
            cfg["charges"].update({ck: cv for ck, cv in v.items() if cv is not None})
        elif v is not None:
            cfg[k] = v
    return cfg


# ---------------------------------------------------------------- time

def clock(minutes):
    return "%02d:%02d" % divmod(int(minutes), 60)


def minutes_of(text, what="time"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SpecError("%s wants a 24-hour IST time like 09:30, not %r" % (what, text))
    return int(m.group(1)) * 60 + int(m.group(2))


def ist_now(text=None):
    """--now text (naive = IST) or the clock, as a naive IST datetime."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)
    t = str(text).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise LabError("--now wants a time like \"2026-10-30 15:40\" (IST)")
    return dt if dt.tzinfo is None else dt.astimezone(IST).replace(tzinfo=None)


def ist_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)


# ---------------------------------------------------------------- bars: Yahoo, CSV, the cache

TIMEFRAMES = {"5m": 5, "1d": None}
YAHOO_RANGE = {"5m": ("60d", "5m"), "1d": ("2y", "1d")}
CSV_HEAD = "date,time,open,high,low,close,volume"


def bars_from_yahoo(text, timeframe, now=None):
    """Yahoo v8 chart JSON -> bars, oldest first: {"date", "time", "o", "h", "l", "c", "v"}.

    5m: regular-session bars only (09:15 to 15:25 starts, on the 5-minute grid), complete ones only.
    1d: one bar a day, time ""; today's bar is dropped until 15:30 IST (it is not final yet).
    Bars with a missing price are dropped (Yahoo leaves some NSE bars empty, often around the closing
    auction). Prices are rounded to the paisa."""
    try:
        doc = json.loads(text) if isinstance(text, str) else text
        res = doc["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LabError("not a Yahoo chart response")
    now = now or ist_now()
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            continue
        local = ist_of(ts)
        if timeframe == "5m":
            if int(ts) % 300:
                continue
            m = local.hour * 60 + local.minute
            if m < OPEN_MIN or m >= CLOSE_MIN or local + datetime.timedelta(minutes=5) > now:
                continue
            key = (local.date().isoformat(), clock(m))
        else:
            d = local.date()
            if d == now.date() and now.hour * 60 + now.minute < CLOSE_MIN:
                continue
            key = (d.isoformat(), "")
        out[key] = {"date": key[0], "time": key[1], "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                    "c": round(c, 2), "v": int(v or 0)}
    return [out[k] for k in sorted(out)]


def write_bars_csv(path, bars):
    lines = [CSV_HEAD] + ["%s,%s,%s,%s,%s,%s,%d" % (b["date"], b["time"], _n(b["o"]), _n(b["h"]), _n(b["l"]),
                                                   _n(b["c"]), int(b["v"])) for b in bars]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if path.endswith(".gz"):
        with open(path, "wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:   # mtime 0: the same bytes every time
                gz.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)


def _n(x):
    s = "%.2f" % float(x)
    return s.rstrip("0").rstrip(".") if "." in s else s


def read_bars_csv(path):
    """date,time,open,high,low,close,volume (time empty for daily bars). .csv or .csv.gz."""
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            rows = fh.read().splitlines()
    except OSError as e:
        raise LabError("could not read %s: %s" % (path, e))
    if not rows or rows[0].strip().lower() != CSV_HEAD:
        raise LabError("%s does not start with the header %s" % (path, CSV_HEAD))
    bars = []
    for n, row in enumerate(rows[1:], 2):
        if not row.strip():
            continue
        p = row.split(",")
        if len(p) != 7:
            raise LabError("%s line %d: want 7 columns" % (path, n))
        try:
            datetime.date.fromisoformat(p[0])
            bars.append({"date": p[0], "time": p[1], "o": float(p[2]), "h": float(p[3]), "l": float(p[4]),
                         "c": float(p[5]), "v": int(float(p[6] or 0))})
        except ValueError:
            raise LabError("%s line %d: not a bar: %r" % (path, n, row))
    return bars


def yahoo_symbol(sym):
    return sym if sym.startswith("^") else sym + ".NS"


def fetch_yahoo(sym, timeframe, timeout=25):
    rng, iv = YAHOO_RANGE[timeframe]
    last = None
    for host in ("query2", "query1"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
            host, urllib.parse.quote(yahoo_symbol(sym)), rng, iv)
        for attempt in (0, 1):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if e.code == 404:
                    raise LabError("Yahoo has no chart for %s (HTTP 404): check the NSE symbol" % yahoo_symbol(sym))
                break
            except (urllib.error.URLError, OSError) as e:
                raise LabError("could not reach Yahoo: %s" % (getattr(e, "reason", None) or e))
        time.sleep(PAUSE)
    raise LabError("Yahoo answered HTTP %s for %s. Try again later." % (last, yahoo_symbol(sym)))


def find_file(folder, sym, timeframe):
    for ext in (".csv.gz", ".csv", ".json.gz", ".json"):
        p = os.path.join(folder, "%s-%s%s" % (sym, timeframe, ext))
        if os.path.exists(p):
            return p
    return None


def read_bars_file(path, timeframe, now=None):
    if path.endswith((".csv", ".csv.gz")):
        return read_bars_csv(path)
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return bars_from_yahoo(fh.read(), timeframe, now or datetime.datetime(2100, 1, 1))


ARCHIVE_DIR = os.path.join("trader", "archive")


def archive_api():
    """trader-archive's offline read API (archive.py in the sibling skill folder), or None when that skill
    is not installed next to this one."""
    import importlib.util
    name = "bt_trader_archive"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-archive", "archive.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sibling_script(folder, script, name):
    """A sibling skill's entry script loaded by path (cached), or None when that skill is not installed next
    to this one."""
    import importlib.util
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


def regime_api():
    """trader-regime's pure functions (regime.py in the sibling skill folder), or None when that skill is
    not installed next to this one: then the scorecard keeps only the simple VIX and trend splits."""
    import importlib.util
    name = "bt_trader_regime"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-regime", "regime.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def strategy_api():
    """trader-strategy's script (the option leg templates, Black-76 and the SPAN-style margin), or None."""
    return sibling_script("trader-strategy", "strategy.py", "bt_lab_strategy")


def options_api():
    """This skill's options package (the "options" helper folder next to lab.py), bound to the lab,
    trader-strategy and trader-archive."""
    import importlib.util
    name = "bt_lab_options"
    mod = sys.modules.get(name)
    if mod is None:
        path = os.path.join(HERE, "options", "__init__.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    mod.bind(_this_module(), strategy_api(), archive_api(), regime_api())
    return mod


def _this_module():
    """This script as a module object (it may be loaded by path under any name, or not registered at all)."""
    me = sys.modules.get(__name__)
    if me is not None and getattr(me, "__dict__", None) is globals():
        return me
    for m in list(sys.modules.values()):
        if getattr(m, "__dict__", None) is globals():
            return m
    import types
    return types.SimpleNamespace(**globals())


def lab_regimes(context):
    """{date: trader-regime record} for NIFTY 50 from the context's daily bars (India VIX for the vol bucket,
    NIFTY's expiry calendar), or {} without trader-regime or NIFTY bars. Each day uses bars up to its close."""
    api = regime_api()
    nifty = (context or {}).get("nifty")
    if api is None or nifty is None or not nifty.n:
        return {}
    vix = (context or {}).get("vix")

    def cols(s):
        return [{"date": s.date[i], "o": s.o[i], "h": s.h[i], "l": s.l[i], "c": s.c[i]} for i in range(s.n)]
    return api.daily_regimes(cols(nifty), cols(vix) if vix is not None and vix.n else None, "NIFTY", "index")


def market_regimes(full_trades, oos_trades, regs, need):
    """trader-regime's split of the trades (whole sample and out of sample) and today's regime."""
    api = regime_api()
    last = regs[max(regs)]
    return {"whole_sample": api.split_by_regime(full_trades, regs, need),
            "out_of_sample": api.split_by_regime(oos_trades, regs, need),
            "current": {"date": last["date"], "key": api.regime_key(last, expiry=False), "words": last["words"]},
            "basis": "NIFTY 50 trend or range x India VIX bucket x expiry flag (trader-regime), "
                     "as of the close before each signal day"}


def regime_gate(g, oos_trades, regs):
    """The opt-in gate "edge in the current regime" (gates.require_regime_edge = 1)."""
    api = regime_api()
    need = int(g["min_regime_trades"])
    needs = ">= %d out-of-sample trades there, mean > %gR" % (need, g["min_oos_expectancy_r"])
    if not regs or api is None:
        return {"gate": "edge in the current regime", "needs": needs,
                "got": "not measured (needs trader-regime and NIFTY bars)", "pass": False}
    cur = api.regime_key(regs[max(regs)], expiry=False)
    e = api.edge_in_regime(oos_trades, regs, cur, need, g["min_oos_expectancy_r"])
    got = "%d trades, %s" % (e["trades"], "none" if e["expectancy_r"] is None else "%.3fR" % e["expectancy_r"])
    if e["ci95_r"]:
        got += " (95%% interval %.2fR to %.2fR)" % tuple(e["ci95_r"])
    return {"gate": "edge in the current regime (%s vol)" % cur.replace("|", " · "), "needs": needs, "got": got,
            "pass": bool(e["pass"])}


def archive_bars(sym, timeframe, root):
    """Bars for one symbol from the local archive in the lab's shape (daily: split- and bonus-adjusted
    NSE bhavcopy prices; ^NSEI and ^INDIAVIX from NSE's index files), or None when there is no archive,
    no such data, or the archive skill is missing."""
    if not root or not os.path.isdir(root):
        return None
    api = archive_api()
    if api is None:
        return None
    try:
        if timeframe == "1d":
            name = api.index_name(sym)
            if name == "India VIX":
                got = api.load_vix(root=root)
            elif sym.startswith("^") or name != sym:
                got = api.load_index(sym, root=root)
            else:
                got = api.load_daily(sym, root=root)
        else:
            got = api.load_intraday(sym, timeframe, root=root)
    except (api.ArchiveError, OSError, ValueError):
        return None
    bars = [{"date": b["date"], "time": b.get("time") or "", "o": round(b["o"], 2), "h": round(b["h"], 2),
             "l": round(b["l"], 2), "c": round(b["c"], 2), "v": int(b.get("v") or 0)} for b in got]
    return bars or None


def load_bars(sym, timeframe, data_dir=None, cache_dir=None, fetch=True, now=None, refresh=False, archive=None):
    """(bars, source). Offline: data_dir/SYM-<tf>.csv(.gz) or Yahoo JSON. Then the local archive (when
    `archive` is a trader-archive folder holding the symbol). Online: Yahoo, cached as CSV in cache_dir
    (re-used the same day unless refresh)."""
    if data_dir:
        p = find_file(data_dir, sym, timeframe)
        if not p:
            raise LabError("no %s-%s.csv(.gz) in %s" % (sym, timeframe, data_dir))
        return read_bars_file(p, timeframe, now), os.path.basename(p)
    bars = archive_bars(sym, timeframe, archive)
    if bars:
        return bars, "archive %s to %s" % (bars[0]["date"], bars[-1]["date"])
    cache = os.path.join(cache_dir, "%s-%s.csv" % (sym, timeframe)) if cache_dir else None
    now = now or ist_now()
    if cache and os.path.exists(cache) and not refresh:
        made = datetime.datetime.fromtimestamp(os.path.getmtime(cache), IST).replace(tzinfo=None)
        if made.date() == now.date() or not fetch:
            return read_bars_csv(cache), "cache %s" % os.path.basename(cache)
    if not fetch:
        raise LabError("no cached bars for %s %s and fetching is off" % (sym, timeframe))
    bars = bars_from_yahoo(fetch_yahoo(sym, timeframe), timeframe, now)
    time.sleep(PAUSE)
    if cache:
        write_bars_csv(cache, bars)
    return bars, "Yahoo %s" % yahoo_symbol(sym)


class Series:
    """One symbol's bars as columns, with an indicator cache. minute = the bar's start (IST) for 5m."""

    def __init__(self, symbol, timeframe, bars):
        self.symbol, self.tf = symbol, timeframe
        self.interval = TIMEFRAMES[timeframe]
        self.bars = bars
        self.n = len(bars)
        self.date = [b["date"] for b in bars]
        self.minute = [minutes_of(b["time"]) if b["time"] else 0 for b in bars]
        self.o = [float(b["o"]) for b in bars]
        self.h = [float(b["h"]) for b in bars]
        self.l = [float(b["l"]) for b in bars]
        self.c = [float(b["c"]) for b in bars]
        self.v = [float(b["v"]) for b in bars]
        self.new_day = [i == 0 or self.date[i] != self.date[i - 1] for i in range(self.n)]
        self.cache = {}

    def field(self, name):
        return {"open": self.o, "high": self.h, "low": self.l, "close": self.c, "volume": self.v}[name]

    def day_spans(self):
        """[(start, end_exclusive)] per session."""
        if "_spans" not in self.cache:
            spans, s = [], 0
            for i in range(1, self.n + 1):
                if i == self.n or self.new_day[i]:
                    spans.append((s, i))
                    s = i
            self.cache["_spans"] = spans
        return self.cache["_spans"]


# ---------------------------------------------------------------- the rule vocabulary (no eval, ever)
#
# An operand is one of:
#   a number                          2, 0.5, -1.5
#   "$name"                           a number from "params" (the grid varies these)
#   a bar field                       "open" "high" "low" "close" "volume"
#   a session value                   "vwap" "prev_high" "prev_low" "prev_close" "day_open" "day_high"
#                                     "day_low" "gap_pct"
#   an indicator                      {"sma": 20} {"ema": 9} {"rsi": 2} {"atr": 14} {"highest": 20}
#                                     {"lowest": 10} {"or_high": 15} {"or_low": 15} {"or_mid": 15}
#                                     with "of": a field (sma, ema, rsi, highest, lowest; default close,
#                                     high for highest, low for lowest)
#   arithmetic                        {"add": [a, b]} {"sub": [a, b]} {"mul": [a, b]} {"div": [a, b]}
#   any operand but a number may carry "shift": k (its value k bars back); a session value as an
#   object takes true: {"prev_high": true, "shift": 1}
# A condition is one of:
#   [a, op, b]                        op: ">" ">=" "<" "<=" "crosses_above" "crosses_below"
#   {"all": [...]} {"any": [...]} {"not": cond}
#   {"time_between": ["09:30", "11:30"]}   the bar's CLOSE time, IST (5-minute specs only)

FIELDS = ("open", "high", "low", "close", "volume")
SESSION_VALUES = ("vwap", "prev_high", "prev_low", "prev_close", "day_open", "day_high", "day_low", "gap_pct")
INDICATORS = ("sma", "ema", "rsi", "atr", "highest", "lowest", "or_high", "or_low", "or_mid")
ARITH = ("add", "sub", "mul", "div")
OPS = (">", ">=", "<", "<=", "crosses_above", "crosses_below")
INTRADAY_ONLY = ("vwap", "day_high", "day_low", "or_high", "or_low", "or_mid")
PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SPEC_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{1,47}$")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
MAX_DEPTH = 10
MAX_NODES = 300
MAX_N = 400
MAX_SHIFT = 100
MAX_GRID = 36

TOP_KEYS = {"schema", "name", "title", "description", "kind", "timeframe", "product", "side", "universe",
            "params", "entry", "window", "stop", "target", "exit", "entries_per_symbol_per_day", "sizing",
            "costs", "grid", "walk_forward", "gates", "trials_tried", "seed", "monte_carlo", "regimes",
            "references", "notes", "options"}
GATE_DEFAULTS = {
    "min_oos_trades": 30,            # out-of-sample trades, at least
    "min_oos_expectancy_r": 0.0,     # mean R per out-of-sample trade after charges, above
    "min_oos_profit_factor": 1.2,    # out-of-sample net wins / net losses, at least
    "max_drawdown_pct": 10.0,        # deepest fall of the whole-sample equity, % of capital, at most
    "min_dsr": 0.95,                 # deflated Sharpe ratio (probability the Sharpe is real), at least
    "max_pbo": 0.5,                  # probability of backtest overfitting (CSCV), below
    "survive_cost_multiplier": 1.5,  # out-of-sample expectancy still above the minimum with charges and slippage x this
    "min_neighbourhood_positive": 0.5,   # share of the grid points around the spec's point that make money
    "min_forward_trades": 20,        # paper forward trades before a PROMOTE
    "demote_window": 30,             # the rolling forward record: the last N trades
    "demote_below_r": 0.0,           # demote when their mean R falls to this or below
    "demote_drawdown_mult": 1.5,     # or the forward drawdown passes the out-of-sample one x this
    "require_regime_edge": 0,        # opt-in (1): the out-of-sample trades in today's market regime must show an edge
    "min_regime_trades": 10,         # ... with at least this many trades (and below it a regime cell is "too few")
}
COST_DEFAULTS = {"slippage_bps": 3.0, "slippage_ticks": 1}
WF_DEFAULTS = {"5m": {"in_sample": 20, "out_of_sample": 10}, "1d": {"in_sample": 120, "out_of_sample": 40}}


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _err(path, msg):
    raise SpecError("%s: %s" % (path or "spec", msg))


class _Walker:
    """Checks operands and conditions against the vocabulary; counts nodes and depth."""

    def __init__(self, params, timeframe):
        self.params, self.tf, self.nodes, self.lookback = params, timeframe, 0, 0

    def _tick(self, path, depth):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            _err(path, "too many rule parts (more than %d)" % MAX_NODES)
        if depth > MAX_DEPTH:
            _err(path, "nested more than %d deep" % MAX_DEPTH)

    def num(self, x, path, lo=None, hi=None, integer=False):
        if isinstance(x, str) and x.startswith("$"):
            name = x[1:]
            if name not in self.params:
                _err(path, "unknown param %r (declare it in \"params\")" % x)
            x = self.params[name]
        if not _is_num(x):
            _err(path, "want a number, not %r" % (x,))
        if integer and float(x) != int(x):
            _err(path, "want a whole number, not %r" % (x,))
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            _err(path, "%r is outside %s to %s" % (x, lo, hi))
        return int(x) if integer else float(x)

    def operand(self, x, path, depth=0):
        self._tick(path, depth)
        if _is_num(x):
            return {"num": float(x)}
        if isinstance(x, bool) or x is None or isinstance(x, float):
            _err(path, "want a number (finite), a field or an indicator, not %r" % (x,))
        if isinstance(x, str):
            if x.startswith("$"):
                return {"num": self.num(x, path)}
            if x in FIELDS:
                return {"field": x, "shift": 0}
            if x in SESSION_VALUES:
                self._intraday_check(x, path)
                return {"sess": x, "shift": 0}
            _err(path, "unknown name %r (fields: %s; session values: %s)" % (
                x[:40], ", ".join(FIELDS), ", ".join(SESSION_VALUES)))
        if isinstance(x, list):
            _err(path, "a list is a condition [a, op, b], not a value")
        if not isinstance(x, dict):
            _err(path, "not a rule value: %r" % (x,))
        keys = set(x)
        arith = keys & set(ARITH)
        if arith:
            if len(keys) != 1:
                _err(path, "arithmetic takes one key, e.g. {\"sub\": [a, b]}")
            op = arith.pop()
            args = x[op]
            if not isinstance(args, list) or len(args) != 2:
                _err(path, "%s wants a list of two values" % op)
            return {"arith": op, "args": [self.operand(a, "%s.%s[%d]" % (path, op, i), depth + 1)
                                          for i, a in enumerate(args)]}
        shift = self.num(x.get("shift", 0), path + ".shift", 0, MAX_SHIFT, True)
        names = keys - {"shift", "of"}
        if len(names) != 1:
            _err(path, "an indicator object has one name (and optional \"of\", \"shift\"), got %s" % sorted(keys)[:6])
        name = names.pop()
        if name in SESSION_VALUES:
            if x[name] is not True or "of" in x:
                _err(path, "%s takes true: {\"%s\": true}" % (name, name))
            self._intraday_check(name, path)
            self.lookback = max(self.lookback, shift)
            return {"sess": name, "shift": shift}
        if name not in INDICATORS:
            _err(path, "unknown indicator %r (known: %s)" % (str(name)[:40], ", ".join(INDICATORS)))
        self._intraday_check(name, path)
        if name.startswith("or_"):
            n = self.num(x[name], "%s.%s" % (path, name), 5, 180, True)
            if n % 5:
                _err(path, "an opening range is a multiple of 5 minutes, not %d" % n)
        else:
            n = self.num(x[name], "%s.%s" % (path, name), 1, MAX_N, True)
        default_of = {"highest": "high", "lowest": "low"}.get(name, "close")
        of = x.get("of", default_of)
        if "of" in x and name in ("atr",) or "of" in x and name.startswith("or_"):
            _err(path, "%s does not take \"of\"" % name)
        if of not in FIELDS:
            _err(path, "\"of\" is a bar field (%s), not %r" % (", ".join(FIELDS), of))
        self.lookback = max(self.lookback, (0 if name.startswith("or_") else n) + shift)
        return {"ind": name, "n": n, "of": of, "shift": shift}

    def _intraday_check(self, name, path):
        if self.tf != "5m" and name in INTRADAY_ONLY:
            _err(path, "%s needs 5-minute bars (timeframe 5m)" % name)

    def condition(self, c, path, depth=0):
        self._tick(path, depth)
        if isinstance(c, list):
            if len(c) != 3 or not isinstance(c[1], str) or c[1] not in OPS:
                _err(path, "a comparison is [a, op, b] with op one of %s" % " ".join(OPS))
            a = self.operand(c[0], path + "[0]", depth + 1)
            b = self.operand(c[2], path + "[2]", depth + 1)
            if c[1].startswith("crosses"):
                self.lookback = max(self.lookback, 1)
            return {"cmp": c[1], "a": a, "b": b}
        if not isinstance(c, dict) or len(c) != 1:
            _err(path, "a condition is [a, op, b], {\"all\": [...]}, {\"any\": [...]}, {\"not\": ...} "
                       "or {\"time_between\": [from, to]}")
        k, v = next(iter(c.items()))
        if k in ("all", "any"):
            if not isinstance(v, list) or not 1 <= len(v) <= 20:
                _err(path, "%s wants a list of 1 to 20 conditions" % k)
            return {k: [self.condition(x, "%s.%s[%d]" % (path, k, i), depth + 1) for i, x in enumerate(v)]}
        if k == "not":
            return {"not": self.condition(v, path + ".not", depth + 1)}
        if k == "time_between":
            if self.tf != "5m":
                _err(path, "time_between needs 5-minute bars")
            if not isinstance(v, list) or len(v) != 2:
                _err(path, "time_between wants [\"09:30\", \"11:30\"]")
            a, b = minutes_of(v[0], path + ".time_between"), minutes_of(v[1], path + ".time_between")
            if a > b:
                _err(path, "time_between runs forward: %s is after %s" % (v[0], v[1]))
            return {"time": [a, b]}
        _err(path, "unknown condition %r" % str(k)[:40])


def _level(w, x, path, kind):
    """A stop or target rule: {"atr": n, "mult": k} {"pct": x} {"price": operand, "buffer_ticks": t} {"r": k}."""
    if not isinstance(x, dict):
        _err(path, "a %s is an object like {\"atr\": 14, \"mult\": 2}" % kind)
    keys = set(x)
    if "atr" in keys:
        if keys - {"atr", "mult"}:
            _err(path, "an ATR %s takes atr and mult" % kind)
        n = w.num(x["atr"], path + ".atr", 1, MAX_N, True)
        w.lookback = max(w.lookback, n)
        return {"atr": {"ind": "atr", "n": n, "of": "close", "shift": 0},
                "mult": w.num(x.get("mult", 1), path + ".mult", 0.1, 20)}
    if "pct" in keys:
        if keys != {"pct"}:
            _err(path, "a percent %s takes only pct" % kind)
        return {"pct": w.num(x["pct"], path + ".pct", 0.05, 50)}
    if "price" in keys:
        if keys - {"price", "buffer_ticks"}:
            _err(path, "a price %s takes price and buffer_ticks" % kind)
        return {"price": w.operand(x["price"], path + ".price"),
                "buffer_ticks": w.num(x.get("buffer_ticks", 0), path + ".buffer_ticks", 0, 100, True)}
    if "r" in keys and kind == "target":
        if keys != {"r"}:
            _err(path, "an R target takes only r")
        return {"r": w.num(x["r"], path + ".r", 0.25, 20)}
    _err(path, "a %s is %s" % (kind, "atr, pct, price or r" if kind == "target" else "atr, pct or price"))


def validate_spec(doc, params=None):
    """A spec (dict) -> a checked, compiled copy. Raises SpecError with the path of the first problem.
    params overrides the spec's "params" (the grid and walk-forward pass their own)."""
    if not isinstance(doc, dict):
        raise SpecError("a spec is a JSON object")
    if doc.get("kind") == "options":
        return options_api().validate(doc, params)
    extra = set(doc) - TOP_KEYS
    if extra:
        _err("spec", "unknown keys %s (known: %s)" % (sorted(extra)[:5], ", ".join(sorted(TOP_KEYS))))
    name = doc.get("name")
    if not isinstance(name, str) or not SPEC_NAME.match(name):
        _err("name", "lowercase letters, digits and dashes, 2 to 48 long")
    kind = doc.get("kind", "equity")
    if kind == "options":
        return options_api().validate(doc, params)
    if kind != "equity":
        _err("kind", "equity or options, not %r" % kind)
    tf = doc.get("timeframe")
    if tf not in TIMEFRAMES:
        _err("timeframe", "5m (intraday) or 1d (swing), not %r" % (tf,))
    product = str(doc.get("product") or ("MIS" if tf == "5m" else "CNC")).upper()
    if product not in ("MIS", "CNC") or (tf == "5m") != (product == "MIS"):
        _err("product", "5-minute specs are MIS (squared off the same day); daily specs are CNC")
    if doc.get("side", "long") != "long":
        _err("side", "long only: the pack sizes and checks buys (trader-budget refuses shorts)")
    uni = doc.get("universe")
    if not isinstance(uni, list) or not 1 <= len(uni) <= 100:
        _err("universe", "a list of 1 to 100 NSE symbols")
    for s in uni:
        if not isinstance(s, str) or not SYMBOL.match(s):
            _err("universe", "%r is not an NSE symbol" % (s,))
    base = doc.get("params") or {}
    if not isinstance(base, dict):
        _err("params", "an object of name: number")
    for k, v in base.items():
        if not PARAM_NAME.match(str(k)) or not _is_num(v):
            _err("params.%s" % k, "a param is a lowercase name with a number")
    p = dict(base)
    if params:
        unknown = set(params) - set(base)
        if unknown:
            _err("params", "unknown params %s" % sorted(unknown))
        p.update(params)
    w = _Walker(p, tf)
    if "entry" not in doc:
        _err("entry", "the entry condition is required")
    entry = w.condition(doc["entry"], "entry")
    if "stop" not in doc:
        _err("stop", "every trade here has a stop (trader-risk requires one)")
    stop = _level(w, doc["stop"], "stop", "stop")
    target = _level(w, doc["target"], "target", "target") if doc.get("target") is not None else None
    ex = doc.get("exit") or {}
    if not isinstance(ex, dict) or set(ex) - {"when", "max_bars", "flat_by"}:
        _err("exit", "an object with when, max_bars, flat_by")
    exit_when = w.condition(ex["when"], "exit.when") if ex.get("when") is not None else None
    max_bars = w.num(ex["max_bars"], "exit.max_bars", 1, 2000, True) if ex.get("max_bars") is not None else None
    if not (target or exit_when or max_bars or tf == "5m"):
        _err("exit", "a daily spec needs a way out besides the stop: a target, exit.when or exit.max_bars")
    flat = ex.get("flat_by", "auto")
    if flat != "auto":
        minutes_of(flat, "exit.flat_by")
    win = doc.get("window") or {}
    if not isinstance(win, dict) or set(win) - {"from", "to"}:
        _err("window", "{\"from\": \"09:30\", \"to\": \"14:45\"}")
    if win and tf != "5m":
        _err("window", "an entry window needs 5-minute bars")
    window = [minutes_of(win[k], "window.%s" % k) if win.get(k) else None for k in ("from", "to")]
    per_day = w.num(doc.get("entries_per_symbol_per_day", 1), "entries_per_symbol_per_day", 1, 20, True)
    costs = dict(COST_DEFAULTS)
    for k, v in (doc.get("costs") or {}).items():
        if k not in COST_DEFAULTS:
            _err("costs", "known: slippage_bps, slippage_ticks")
        costs[k] = w.num(v, "costs." + k, 0, 200)
    gates = dict(GATE_DEFAULTS)
    for k, v in (doc.get("gates") or {}).items():
        if k not in GATE_DEFAULTS:
            _err("gates", "unknown gate %r (known: %s)" % (k, ", ".join(GATE_DEFAULTS)))
        gates[k] = w.num(v, "gates." + k)
    grid = doc.get("grid") or {}
    if not isinstance(grid, dict):
        _err("grid", "an object of param: [values]")
    combos = 1
    for k, vals in grid.items():
        if k not in base:
            _err("grid.%s" % k, "a grid varies a declared param")
        if not isinstance(vals, list) or not 1 <= len(vals) <= 12 or not all(_is_num(x) for x in vals):
            _err("grid.%s" % k, "a list of 1 to 12 numbers")
        combos *= len(set(vals) | {base[k]})
    if combos > MAX_GRID:
        _err("grid", "%d points: keep the grid at %d or fewer (every point is a trial the deflated Sharpe "
                     "charges you for)" % (combos, MAX_GRID))
    wf = dict(WF_DEFAULTS[tf])
    for k, v in (doc.get("walk_forward") or {}).items():
        if k not in ("in_sample", "out_of_sample"):
            _err("walk_forward", "in_sample and out_of_sample, in sessions")
        wf[k] = w.num(v, "walk_forward." + k, 2, 2000, True)
    sizing = doc.get("sizing") or {}
    if not isinstance(sizing, dict):
        _err("sizing", "an object of trader-budget config keys")
    cfg = lab_config(sizing)
    cfg["product"] = product
    rg = doc.get("regimes") or {}
    vix_edges = rg.get("vix", [13, 16, 20])
    if not isinstance(vix_edges, list) or not all(_is_num(x) for x in vix_edges) or vix_edges != sorted(vix_edges):
        _err("regimes.vix", "rising India VIX bucket edges, e.g. [13, 16, 20]")
    mc = doc.get("monte_carlo") or {}
    runs = w.num(mc.get("runs", 1000), "monte_carlo.runs", 100, 20000, True)
    trials = w.num(doc.get("trials_tried", 0), "trials_tried", 0, 100000, True)
    seed = w.num(doc.get("seed", 16), "seed", 0, 2 ** 31, True)
    return {"name": name, "title": str(doc.get("title") or name)[:120], "description": str(doc.get("description") or ""),
            "timeframe": tf, "product": product, "universe": list(uni), "params": p, "base_params": dict(base),
            "entry": entry, "stop": stop, "target": target, "exit_when": exit_when, "max_bars": max_bars,
            "flat_by": flat, "window": window, "per_day": per_day, "costs": costs, "gates": gates, "grid": grid,
            "walk_forward": wf, "cfg": cfg, "vix_edges": [float(x) for x in vix_edges], "mc_runs": runs,
            "trials_tried": trials, "seed": seed, "lookback": w.lookback, "raw": doc}


def spec_hash(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- indicators (each value uses bars up to i only)

def _sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(x, n):
    out, a, e = [None] * len(x), 2.0 / (n + 1), None
    for i, v in enumerate(x):
        if i == n - 1:
            e = sum(x[:n]) / n
        elif i >= n:
            e = a * v + (1 - a) * e
        out[i] = e
    return out


def _rsi(x, n):
    """Wilder's RSI: the first average over n changes, then (prev x (n-1) + now) / n."""
    out = [None] * len(x)
    if len(x) <= n:
        return out
    gains = [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    losses = [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(x)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _atr(s, n):
    tr = [s.h[0] - s.l[0]] + [max(s.h[i] - s.l[i], abs(s.h[i] - s.c[i - 1]), abs(s.l[i] - s.c[i - 1]))
                              for i in range(1, s.n)]
    out, a = [None] * s.n, None
    for i in range(s.n):
        if i == n - 1:
            a = sum(tr[:n]) / n
        elif i >= n:
            a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def _rolling(x, n, fn):
    return [fn(x[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(x))]


def _session(s, name):
    """Session values for 5-minute bars (per day), or the daily bar's own for 1d."""
    out = [None] * s.n
    if s.tf == "1d":
        for i in range(s.n):
            if name == "day_open":
                out[i] = s.o[i]
            elif name in ("prev_high", "prev_low", "prev_close") and i > 0:
                out[i] = {"prev_high": s.h, "prev_low": s.l, "prev_close": s.c}[name][i - 1]
            elif name == "gap_pct" and i > 0:
                out[i] = (s.o[i] - s.c[i - 1]) / s.c[i - 1] * 100.0
        return out
    prev = None
    for a, b in s.day_spans():
        full_open = s.minute[a] == OPEN_MIN
        cum_pv = cum_v = 0.0
        hi, lo = -1e18, 1e18
        for i in range(a, b):
            hi, lo = max(hi, s.h[i]), min(lo, s.l[i])
            if name == "vwap":
                tp = (s.h[i] + s.l[i] + s.c[i]) / 3.0
                cum_pv += tp * s.v[i]
                cum_v += s.v[i]
                out[i] = cum_pv / cum_v if cum_v > 0 else None
            elif name == "day_high":
                out[i] = hi
            elif name == "day_low":
                out[i] = lo
            elif name == "day_open":
                out[i] = s.o[a] if full_open else None
            elif prev is not None:
                if name == "prev_high":
                    out[i] = prev[0]
                elif name == "prev_low":
                    out[i] = prev[1]
                elif name == "prev_close":
                    out[i] = prev[2]
                elif name == "gap_pct" and full_open:
                    out[i] = (s.o[a] - prev[2]) / prev[2] * 100.0
        prev = (max(s.h[a:b]), min(s.l[a:b]), s.c[b - 1])
    return out


def _opening_range(s, minutes, which):
    """The day's high/low/mid over its first `minutes`, known once the range's last bar has closed.
    A day whose 09:15 bar or any range bar is missing has no range."""
    out = [None] * s.n
    end = OPEN_MIN + minutes
    for a, b in s.day_spans():
        k = minutes // s.interval
        if b - a < k or s.minute[a] != OPEN_MIN or s.minute[a + k - 1] != end - s.interval:
            continue
        hi, lo = max(s.h[a:a + k]), min(s.l[a:a + k])
        val = {"or_high": hi, "or_low": lo, "or_mid": (hi + lo) / 2.0}[which]
        for i in range(a + k - 1, b):
            out[i] = val
    return out


def _shift(x, k):
    return x if not k else [None] * min(k, len(x)) + x[:-k] if k < len(x) else [None] * len(x)


def operand_values(s, node):
    """A compiled operand -> its value at every bar of series s (None where it is not known yet)."""
    key = json.dumps(node, sort_keys=True)
    if key in s.cache:
        return s.cache[key]
    if "num" in node:
        out = [node["num"]] * s.n
    elif "field" in node:
        out = _shift(s.field(node["field"]), node["shift"])
    elif "sess" in node:
        out = _shift(_session(s, node["sess"]), node["shift"])
    elif "arith" in node:
        a, b = [operand_values(s, x) for x in node["args"]]
        op = node["arith"]
        out = [None] * s.n
        for i in range(s.n):
            x, y = a[i], b[i]
            if x is None or y is None:
                continue
            if op == "add":
                out[i] = x + y
            elif op == "sub":
                out[i] = x - y
            elif op == "mul":
                out[i] = x * y
            elif y != 0:
                out[i] = x / y
    else:
        name, n, of = node["ind"], node["n"], node["of"]
        if name == "sma":
            raw = _sma(s.field(of), n)
        elif name == "ema":
            raw = _ema(s.field(of), n)
        elif name == "rsi":
            raw = _rsi(s.field(of), n)
        elif name == "atr":
            raw = _atr(s, n)
        elif name == "highest":
            raw = _rolling(s.field(of), n, max)
        elif name == "lowest":
            raw = _rolling(s.field(of), n, min)
        else:
            raw = _opening_range(s, n, name)
        out = _shift(raw, node["shift"])
    s.cache[key] = out
    return out


def condition_values(s, c):
    """A compiled condition -> True/False at every bar (False where a value is not known)."""
    if "cmp" in c:
        a, b, op = operand_values(s, c["a"]), operand_values(s, c["b"]), c["cmp"]
        out = [False] * s.n
        for i in range(s.n):
            x, y = a[i], b[i]
            if x is None or y is None:
                continue
            if op == ">":
                out[i] = x > y
            elif op == ">=":
                out[i] = x >= y
            elif op == "<":
                out[i] = x < y
            elif op == "<=":
                out[i] = x <= y
            elif i > 0 and a[i - 1] is not None and b[i - 1] is not None and not (s.tf == "5m" and s.new_day[i]):
                if op == "crosses_above":
                    out[i] = x > y and a[i - 1] <= b[i - 1]
                else:
                    out[i] = x < y and a[i - 1] >= b[i - 1]
        return out
    if "all" in c:
        parts = [condition_values(s, x) for x in c["all"]]
        return [all(p[i] for p in parts) for i in range(s.n)]
    if "any" in c:
        parts = [condition_values(s, x) for x in c["any"]]
        return [any(p[i] for p in parts) for i in range(s.n)]
    if "not" in c:
        inner = condition_values(s, c["not"])
        return [not v for v in inner]
    a, b = c["time"]
    return [a <= s.minute[i] + s.interval <= b for i in range(s.n)]


def level_values(s, rule, kind, stops=None):
    """Stop or target levels for a signal on each bar's close (None where unknown)."""
    if rule is None:
        return [None] * s.n
    out = [None] * s.n
    if "atr" in rule:
        atr = operand_values(s, rule["atr"])
        sign = -1 if kind == "stop" else 1
        for i in range(s.n):
            if atr[i] is not None:
                out[i] = s.c[i] + sign * rule["mult"] * atr[i]
    elif "pct" in rule:
        f = (1 - rule["pct"] / 100.0) if kind == "stop" else (1 + rule["pct"] / 100.0)
        out = [c * f for c in s.c]
    elif "price" in rule:
        vals = operand_values(s, rule["price"])
        for i in range(s.n):
            if vals[i] is not None:
                buf = rule["buffer_ticks"] * tick_for(s.c[i])
                out[i] = vals[i] - buf if kind == "stop" else vals[i] + buf
    else:   # r
        for i in range(s.n):
            if stops[i] is not None and stops[i] < s.c[i]:
                out[i] = s.c[i] + rule["r"] * (s.c[i] - stops[i])
    return out


def signals(spec, s):
    """{entry, exit, stop, target}: arrays over series s for a compiled spec."""
    stop = level_values(s, spec["stop"], "stop")
    return {"entry": condition_values(s, spec["entry"]),
            "exit": condition_values(s, spec["exit_when"]) if spec["exit_when"] else None,
            "stop": stop, "target": level_values(s, spec["target"], "target", stop)}


# ---------------------------------------------------------------- the backtest, bar by bar

def slippage(price, costs, mult=1.0):
    """Rupees a share lost to slippage on a market or stop fill: the larger of slippage_bps of the price
    and slippage_ticks ticks, times the cost multiplier."""
    return max(costs["slippage_bps"] * price / 10000.0, costs["slippage_ticks"] * tick_for(price)) * mult


def _day_rules(spec, date, symbol, fno):
    """(entry from, entry cutoff, flat-by) in minutes for one symbol on one date, from the dated rules."""
    cfg = spec["cfg"]
    seg = segment_of(symbol, fno)
    c = dict(cfg, flat_by=(cfg.get("flat_by") if spec["flat_by"] == "auto" else spec["flat_by"]))
    flat, _ = safe_flat_by(date, seg, c)
    cut = entry_cutoff(date, seg, c)
    start = minutes_of(cfg.get("no_entries_before") or "09:15")
    frm, to = spec["window"]
    start = max(start, frm) if frm is not None else start
    cut_m = cut.hour * 60 + cut.minute
    cut_m = min(cut_m, to) if to is not None else cut_m
    return start, cut_m, flat.hour * 60 + flat.minute


def backtest(spec, data, cost_mult=1.0, fno=None, start=None, end=None):
    """Run a compiled spec over {symbol: Series}. Pure: no files, no network.

    Signals are read on a bar's close and fill at the symbol's next bar's open (plus slippage); stops and
    targets fill inside later bars. start/end (YYYY-MM-DD) bound the days a signal may fire on. Returns
    {"trades": [...], "skipped": {why: n}, "pending": [...signals waiting for the next bar...],
     "open": [...positions still open at the end of the data...]}."""
    cfg, costs = spec["cfg"], spec["costs"]
    intraday = spec["timeframe"] == "5m"
    with_charges = bool(cfg.get("include_charges", True))
    max_dl, max_trades = float(cfg["max_daily_loss"]), int(cfg["max_trades_per_day"])
    min_rr = float(cfg.get("min_reward_risk") or 0)
    capital = float(cfg["capital"])
    syms = [x for x in spec["universe"] if x in data]
    sig = {x: signals(spec, data[x]) for x in syms}
    events = []
    for k, x in enumerate(syms):
        s = data[x]
        events.extend((s.date[i], s.minute[i], k, i) for i in range(s.n))
    events.sort()
    rules_cache = {}
    state = {x: {"pos": None, "pend": None, "exit": None, "today": None, "count": 0, "last": None} for x in syms}
    trades, skipped = [], {}
    day, day_trades, day_risk, day_pnl = None, 0, 0.0, 0.0
    cash_used = 0.0

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    def close(x, st, price, date, minute, reason):
        nonlocal cash_used, day_pnl
        p = st["pos"]
        price = round_paise(max(price, 0.05))
        gross = round_paise((price - p["entry"]) * p["qty"])
        ch = round_trip_charges(p["entry"], price, p["qty"], cfg)["total"] if with_charges else 0.0
        ch = round_paise(ch * cost_mult)
        net = round_paise(gross - ch)
        risk = p["qty"] * (p["entry"] - p["stop"])
        trades.append({"symbol": x, "entry_date": p["date"], "entry_time": p["time"], "exit_date": date,
                       "exit_time": clock(minute) if intraday else "", "entry": p["entry"], "exit": price,
                       "stop": p["stop"], "target": p["target"], "qty": p["qty"], "gross": gross, "charges": ch,
                       "net": net, "r": round(net / risk, 4) if risk > 0 else 0.0, "reason": reason,
                       "bars": p["bars"], "signal_date": p["signal_date"]})
        cash_used -= p["qty"] * p["entry"]
        if date == day:
            day_pnl += net
        st["pos"], st["exit"] = None, None

    for date, minute, k, i in events:
        x = syms[k]
        s, st, g = data[x], state[x], sig[x]
        if date != day:
            day, day_trades, day_risk, day_pnl = date, 0, 0.0, 0.0
        if st["today"] != date:
            st["today"], st["count"] = date, 0
        if intraday:
            rk = (date, x)
            if rk not in rules_cache:
                rules_cache[rk] = _day_rules(spec, date, x, fno)
            frm, cut, flat = rules_cache[rk]
            if st["pos"] and st["pos"]["date"] != date:       # no flat-by bar that day: out at its last close
                j = st["last"]
                close(x, st, s.c[j] - slippage(s.c[j], costs, cost_mult), s.date[j], s.minute[j] + 5,
                      "flat (no bar at the flat-by)")
            if st["pend"] and st["pend"]["date"] != date:
                st["pend"] = None
                skip("signal on the day's last bar")
        o, h, l, c = s.o[i], s.h[i], s.l[i], s.c[i]
        fresh = False
        if st["pos"] and st["exit"]:
            close(x, st, o - slippage(o, costs, cost_mult), date, minute, st["exit"])
        if st["pend"]:
            pe, st["pend"] = st["pend"], None
            fill = round_paise(o + slippage(o, costs, cost_mult))
            if intraday and minute >= flat:
                skip("next bar at or after the flat-by")
            elif fill <= pe["stop"] or (pe["target"] is not None and fill >= pe["target"]):
                skip("opened through the stop or target")
            else:
                st["pos"] = {"entry": fill, "stop": pe["stop"], "target": pe["target"], "qty": pe["qty"],
                             "date": date, "time": clock(minute) if intraday else "", "bars": 0,
                             "signal_date": pe["date"]}
                cash_used += pe["qty"] * fill
                fresh = True
        if st["pos"]:
            p = st["pos"]
            if intraday and minute >= flat:
                close(x, st, o - slippage(o, costs, cost_mult), date, minute, "flat-by")
            elif not fresh and o <= p["stop"]:
                close(x, st, o - slippage(o, costs, cost_mult), date, minute, "stop (gap)")
            elif l <= p["stop"]:
                close(x, st, p["stop"] - slippage(p["stop"], costs, cost_mult), date, minute, "stop")
            elif p["target"] is not None and not fresh and o >= p["target"]:
                close(x, st, o, date, minute, "target (gap)")
            elif p["target"] is not None and h >= p["target"]:
                close(x, st, p["target"], date, minute, "target")
            else:
                p["bars"] += 1
                if g["exit"] is not None and g["exit"][i]:
                    st["exit"] = "exit rule"
                elif spec["max_bars"] and p["bars"] >= spec["max_bars"]:
                    st["exit"] = "max holding"
        st["last"] = i
        if st["pos"] or st["pend"] or not g["entry"][i]:
            continue
        if (start and date < start) or (end and date > end):
            continue
        if intraday:
            close_m = minute + 5
            if close_m < frm or close_m > cut:
                skip("outside the entry window")
                continue
        if st["count"] >= spec["per_day"]:
            skip("already traded this stock today")
            continue
        if day_trades >= max_trades:
            skip("max trades a day")
            continue
        if day_pnl <= -max_dl:
            skip("daily loss limit hit")
            continue
        est = c + slippage(c, costs, cost_mult)
        tick = tick_for(est, cfg.get("tick_size"))
        stop = g["stop"][i]
        if stop is None or stop <= 0:
            skip("no stop level yet")
            continue
        stop = tick_round(stop, tick, "down")
        if stop >= est:
            skip("stop not below the entry")
            continue
        if spec["target"] is not None and "r" in spec["target"]:
            target = est + spec["target"]["r"] * (est - stop)      # R from the expected fill and the rounded stop
        else:
            target = g["target"][i]
        target = tick_round(target, tick) if target is not None else None
        if target is not None and min_rr and (target - est) < min_rr * (est - stop) - tick / 2.0:
            skip("reward:risk under the minimum")
            continue
        qty, loss, bound = size_qty(est, stop, cfg, capital - cash_used)
        if qty <= 0:
            skip("too small to size (%s)" % bound)
            continue
        if day_risk + loss > max_dl + 0.005:
            skip("daily loss budget used up")
            continue
        st["pend"] = {"date": date, "time": clock(minute + 5) if intraday else "", "stop": stop, "target": target,
                      "qty": qty, "est": round_paise(est), "close": c, "loss": loss}
        st["count"] += 1
        day_trades += 1
        day_risk += loss
    pending, still_open = [], []
    for x in syms:
        st, s = state[x], data[x]
        if st["pend"]:
            pending.append(dict(st["pend"], symbol=x))
        if st["pos"]:
            still_open.append(dict(st["pos"], symbol=x))
            j = s.n - 1
            close(x, st, s.c[j] - slippage(s.c[j], costs, cost_mult), s.date[j], s.minute[j] + 5, "end of data")
    trades.sort(key=lambda t: (t["exit_date"], t["exit_time"], t["symbol"]))
    return {"trades": trades, "skipped": skipped, "pending": pending, "open": still_open}


# ---------------------------------------------------------------- statistics

def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _std(x):
    return statistics.stdev(x) if len(x) > 1 else 0.0


def daily_pnl(trades, days):
    """Net P&L by exit date over the given days (zero on days with no exit)."""
    by = {}
    for t in trades:
        by[t["exit_date"]] = by.get(t["exit_date"], 0.0) + t["net"]
    return [round_paise(by.get(d, 0.0)) for d in days]


def max_drawdown(pnls, start=0.0):
    """(deepest fall from a peak, in rupees, as a positive number) of the running sum of pnls."""
    eq, peak, dd = start, start, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def sharpe(returns):
    """Per-period Sharpe ratio: mean / sample standard deviation (0 when it cannot vary)."""
    sd = _std(returns)
    return _mean(returns) / sd if sd > 0 else 0.0


def trade_stats(trades, days, capital):
    nets = [t["net"] for t in trades]
    rs = [t["r"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gl = -sum(losses)
    pnl = daily_pnl(trades, days)
    rets = [p / capital for p in pnl]
    dd = max_drawdown(pnl)
    streak = run = 0
    for x in nets:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    return {
        "trades": len(trades), "days": len(days), "wins": len(wins),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "gross": round_paise(sum(t["gross"] for t in trades)), "charges": round_paise(sum(t["charges"] for t in trades)),
        "net": round_paise(sum(nets)), "avg_net": round_paise(_mean(nets)),
        "expectancy_r": round(_mean(rs), 4), "avg_win_r": round(_mean([r for r in rs if r > 0]), 4),
        "avg_loss_r": round(_mean([r for r in rs if r <= 0]), 4),
        "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
        "max_drawdown": round_paise(dd), "max_drawdown_pct": round(dd / capital * 100, 3),
        "sharpe_daily": round(sharpe(rets), 4), "sharpe_annual": round(sharpe(rets) * math.sqrt(252), 3),
        "longest_losing_streak": streak, "avg_bars": round(_mean([t["bars"] for t in trades]), 2),
    }


def session_days(data):
    return sorted(set(d for s in data.values() for d in s.date))


# ---------------------------------------------------------------- robustness

def walk_forward_splits(days, in_sample, out_of_sample, skip=0):
    """Rolling windows over sessions: [(in-sample days, out-of-sample days)], each out-of-sample window
    right after its in-sample one, rolled forward by the out-of-sample length. Only whole windows."""
    days = list(days)[skip:]
    out, k = [], 0
    while k + in_sample + out_of_sample <= len(days):
        out.append((days[k:k + in_sample], days[k + in_sample:k + in_sample + out_of_sample]))
        k += out_of_sample
    return out


def grid_points(spec):
    """(names, axes, points, index of the spec's own point). The spec's own value is always on its axis."""
    base, grid = spec["base_params"], spec["grid"]
    names = sorted(grid)
    axes = [sorted(set(float(v) for v in grid[k]) | {float(base[k])}) for k in names]
    pts = [dict(base, **dict(zip(names, combo))) for combo in itertools.product(*axes)] if names else [dict(base)]
    own = tuple(float(base[k]) for k in names)
    home = [tuple(p[k] for k in names) for p in pts].index(own) if names else 0
    return names, axes, pts, home


def neighbourhood(names, axes, pts, home, scores):
    """The points within one grid step of the spec's point on every axis (itself included)."""
    if not names:
        return [home]
    idx = [[axes[a].index(p[n]) for a, n in enumerate(names)] for p in pts]
    me = idx[home]
    return [j for j, ix in enumerate(idx) if all(abs(ix[a] - me[a]) <= 1 for a in range(len(names)))]


def percentile(xs, q):
    """Linear-interpolated percentile, q in 0..1."""
    if not xs:
        return 0.0
    x = sorted(xs)
    pos = q * (len(x) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(x) - 1)
    return x[lo] + (x[hi] - x[lo]) * (pos - lo)


def monte_carlo(nets, runs=1000, seed=16, points=40):
    """Trade-order reshuffles (how deep could the drawdown have been with the same trades in another
    order) and a bootstrap (trades drawn with replacement: the spread of outcomes). Seeded."""
    if not nets:
        return None
    rng = random.Random(seed)
    dds = []
    for _ in range(runs):
        x = list(nets)
        rng.shuffle(x)
        dds.append(max_drawdown(x))
    n = len(nets)
    marks = sorted(set([max(1, int(round(n * k / float(points)))) for k in range(1, points + 1)]))
    at = {m: [] for m in marks}
    finals = []
    for _ in range(runs):
        eq = 0.0
        for j in range(1, n + 1):
            eq += nets[rng.randrange(n)]
            if j in at:
                at[j].append(eq)
        finals.append(eq)
    actual = max_drawdown(nets)
    return {"runs": runs, "seed": seed, "trades": n,
            "drawdown_actual": round_paise(actual), "drawdown_p50": round_paise(percentile(dds, 0.5)),
            "drawdown_p95": round_paise(percentile(dds, 0.95)), "drawdown_worst": round_paise(max(dds)),
            "actual_drawdown_rank": round(sum(1 for d in dds if d <= actual) / float(runs), 3),
            "final_p5": round_paise(percentile(finals, 0.05)), "final_p50": round_paise(percentile(finals, 0.5)),
            "final_p95": round_paise(percentile(finals, 0.95)),
            "prob_loss": round(sum(1 for f in finals if f < 0) / float(runs), 4),
            "bands": [{"trade": m, "p5": round_paise(percentile(at[m], 0.05)), "p50": round_paise(percentile(at[m], 0.5)),
                       "p95": round_paise(percentile(at[m], 0.95))} for m in marks]}


EULER_GAMMA = 0.5772156649015329
_ND = statistics.NormalDist()


def moments(x):
    """(skewness, kurtosis) from population moments; kurtosis is 3 for a normal distribution."""
    n = len(x)
    if n < 2:
        return 0.0, 3.0
    m = _mean(x)
    m2 = sum((v - m) ** 2 for v in x) / n
    if m2 <= 0:
        return 0.0, 3.0
    m3 = sum((v - m) ** 3 for v in x) / n
    m4 = sum((v - m) ** 4 for v in x) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def probabilistic_sharpe(sr, sr_star, n, skew, kurt):
    """PSR (Bailey and Lopez de Prado 2012): the probability the true per-period Sharpe exceeds sr_star,
    given n observations with this skewness and (non-excess) kurtosis."""
    if n < 2:
        return 0.0
    den = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if den <= 0:
        return 0.0
    return _ND.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(den))


def expected_max_sharpe(n_trials, var_sr):
    """E[max Sharpe] of n_trials strategies with zero true Sharpe and this variance across trials
    (Bailey and Lopez de Prado 2014, eq. 2 with the Euler-Mascheroni constant)."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    g = EULER_GAMMA
    return math.sqrt(var_sr) * ((1 - g) * _ND.inv_cdf(1 - 1.0 / n_trials) +
                                g * _ND.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(returns, trial_sharpes=(), extra_trials=0):
    """The deflated Sharpe ratio: PSR against the Sharpe the best of N luck-only trials would show.
    N = the trials in trial_sharpes (the grid) plus extra_trials (variants tried before, declared in the
    spec as trials_tried, plus earlier spec versions). Their variance comes from trial_sharpes; with
    fewer than two, the null variance 1 / (T - 1) stands in."""
    t = len(returns)
    sr = sharpe(returns)
    skew, kurt = moments(returns)
    n = max(1, len(trial_sharpes)) + int(extra_trials)
    var = statistics.variance(trial_sharpes) if len(trial_sharpes) >= 2 else (1.0 / (t - 1) if t > 1 else 0.0)
    star = expected_max_sharpe(n, var)
    return {"sharpe": round(sr, 5), "observations": t, "skew": round(skew, 4), "kurtosis": round(kurt, 4),
            "trials": n, "trial_sharpe_variance": round(var, 8), "sharpe_hurdle": round(star, 5),
            "psr": round(probabilistic_sharpe(sr, 0.0, t, skew, kurt), 4),
            "dsr": round(probabilistic_sharpe(sr, star, t, skew, kurt), 4)}


def _avg_ranks(vals):
    """1-based ranks, ties share the average rank."""
    order = sorted(range(len(vals)), key=lambda j: vals[j])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return ranks


def pbo_cscv(matrix, blocks=8, metric="sharpe"):
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey, Borwein,
    Lopez de Prado and Zhu 2015). matrix: T rows (periods) x N columns (trials, e.g. grid points).

    The rows are cut into `blocks` contiguous blocks. For every way to pick half the blocks as in-sample
    (the rest out-of-sample) the trial that is best in-sample is ranked out-of-sample; w = rank / (N + 1),
    logit = ln(w / (1 - w)). PBO is the share of splits where the logit is at or below 0: the in-sample
    winner did no better than the median out of sample. None when it cannot be measured."""
    if not matrix or len(matrix[0]) < 2:
        return None
    T, N = len(matrix), len(matrix[0])
    blocks = int(blocks)
    if blocks % 2 or blocks < 2 or T < blocks:
        return None
    size, extra = divmod(T, blocks)
    bounds, s = [], 0
    for b in range(blocks):
        e = s + size + (1 if b < extra else 0)
        bounds.append((s, e))
        s = e

    def perf(rows, n):
        col = [matrix[r][n] for r in rows]
        if metric == "mean":
            return _mean(col)
        return sharpe(col)

    logits = []
    for J in itertools.combinations(range(blocks), blocks // 2):
        ins = [r for b in J for r in range(*bounds[b])]
        outs = [r for b in range(blocks) if b not in J for r in range(*bounds[b])]
        pin = [perf(ins, n) for n in range(N)]
        best = max(range(N), key=lambda n: (pin[n], -n))
        rank = _avg_ranks([perf(outs, n) for n in range(N)])[best]
        w = rank / (N + 1.0)
        logits.append(math.log(w / (1 - w)))
    return {"pbo": round(sum(1 for x in logits if x <= 0) / float(len(logits)), 4), "splits": len(logits),
            "blocks": blocks, "trials": N, "median_logit": round(percentile(logits, 0.5), 4)}


def regime_labels(context, edges):
    """{date: (vix bucket, trend)} from the PREVIOUS session's India VIX close and NIFTY 50 trend.
    Trend: up when NIFTY closed above its 50-day average and that average rose over 10 sessions, down
    for the mirror image, range otherwise."""
    vix, nifty = context.get("vix"), context.get("nifty")
    vmap, tmap = {}, {}
    if vix is not None:
        for i in range(vix.n):
            v = vix.c[i]
            lab = None
            for k, e in enumerate(edges):
                if v < e:
                    lab = ("VIX under %g" % e) if k == 0 else ("VIX %g to %g" % (edges[k - 1], e))
                    break
            vmap[vix.date[i]] = lab or "VIX %g and up" % edges[-1]
    if nifty is not None:
        sma = _sma(nifty.c, 50)
        for i in range(nifty.n):
            if sma[i] is None or i < 59 or sma[i - 10] is None:
                continue
            up, dn = nifty.c[i] > sma[i] and sma[i] > sma[i - 10], nifty.c[i] < sma[i] and sma[i] < sma[i - 10]
            tmap[nifty.date[i]] = "NIFTY trending up" if up else ("NIFTY trending down" if dn else "NIFTY ranging")
    return vmap, tmap


def _before(sorted_dates, d):
    import bisect
    k = bisect.bisect_left(sorted_dates, d)
    return sorted_dates[k - 1] if k > 0 else None


def regime_split(trades, context, edges):
    vmap, tmap = regime_labels(context, edges)
    vd, td = sorted(vmap), sorted(tmap)
    groups = {"vix": {}, "trend": {}}
    for t in trades:
        d = t["signal_date"]
        pv, pt = _before(vd, d), _before(td, d)
        groups["vix"].setdefault(vmap.get(pv, "VIX unknown") if pv else "VIX unknown", []).append(t)
        groups["trend"].setdefault(tmap.get(pt, "trend unknown") if pt else "trend unknown", []).append(t)
    out = {}
    for kind, g in groups.items():
        rows = []
        for lab in sorted(g):
            ts = g[lab]
            nets = [x["net"] for x in ts]
            gl = -sum(x for x in nets if x <= 0)
            rows.append({"regime": lab, "trades": len(ts), "net": round_paise(sum(nets)),
                         "expectancy_r": round(_mean([x["r"] for x in ts]), 4),
                         "win_rate": round(sum(1 for x in nets if x > 0) / float(len(ts)), 4),
                         "profit_factor": round(sum(x for x in nets if x > 0) / gl, 3) if gl > 0 else None})
        out[kind] = rows
    return out


# ---------------------------------------------------------------- gates and the verdict

def _pf_ok(stats, need):
    pf = stats.get("profit_factor")
    if pf is None:
        return stats.get("trades", 0) > 0 and stats.get("net", 0) > 0
    return pf >= need


def evaluate_gates(g, oos, full, dsr, pbo, cost_oos, neigh_share):
    """Every gate the spec set before the run: [{gate, needs, got, pass}]."""
    m = g["survive_cost_multiplier"]
    co = cost_oos.get(_mkey(m))
    rows = [
        ("enough out-of-sample trades", ">= %d" % g["min_oos_trades"], oos["trades"], oos["trades"] >= g["min_oos_trades"]),
        ("out-of-sample expectancy after charges", "> %gR" % g["min_oos_expectancy_r"], "%.3fR" % oos["expectancy_r"],
         oos["trades"] > 0 and oos["expectancy_r"] > g["min_oos_expectancy_r"]),
        ("out-of-sample profit factor", ">= %g" % g["min_oos_profit_factor"],
         "none (no losses)" if oos["profit_factor"] is None else oos["profit_factor"], _pf_ok(oos, g["min_oos_profit_factor"])),
        ("max drawdown (whole sample)", "<= %g%% of capital" % g["max_drawdown_pct"], "%.2f%%" % full["max_drawdown_pct"],
         full["max_drawdown_pct"] <= g["max_drawdown_pct"]),
        ("deflated Sharpe ratio", ">= %g" % g["min_dsr"], dsr["dsr"] if dsr else "not measured",
         bool(dsr) and dsr["dsr"] >= g["min_dsr"]),
        ("probability of backtest overfitting", "< %g" % g["max_pbo"],
         pbo["pbo"] if pbo else "not measured (needs a grid of 2+ points)", bool(pbo) and pbo["pbo"] < g["max_pbo"]),
        ("survives %gx costs" % m, "out-of-sample expectancy > %gR" % g["min_oos_expectancy_r"],
         "%.3fR" % co["expectancy_r"] if co else "not run",
         bool(co) and co["trades"] > 0 and co["expectancy_r"] > g["min_oos_expectancy_r"]),
        ("parameter neighbourhood", ">= %d%% of nearby grid points make money" % round(g["min_neighbourhood_positive"] * 100),
         "%d%%" % round(neigh_share * 100), neigh_share >= g["min_neighbourhood_positive"]),
    ]
    return [{"gate": a, "needs": b, "got": c, "pass": bool(d)} for a, b, c, d in rows]


def _mkey(m):
    return "%gx" % m


def forward_decay(trades, g, oos_drawdown):
    """(decayed?, why) for the rolling forward record (oldest first)."""
    w = int(g["demote_window"])
    last = trades[-w:]
    if len(last) >= min(w, int(g["min_forward_trades"])):
        mr = _mean([t["r"] for t in last])
        if mr <= g["demote_below_r"]:
            return True, "the last %d forward trades average %.3fR, at or below %gR" % (len(last), mr, g["demote_below_r"])
    dd = max_drawdown([t["net"] for t in trades])
    if oos_drawdown > 0 and dd > g["demote_drawdown_mult"] * oos_drawdown:
        return True, "the forward drawdown %s is past %gx the out-of-sample drawdown %s" % (
            inr(dd), g["demote_drawdown_mult"], inr(oos_drawdown))
    return False, ""


def decide(gates, g, forward_trades=(), oos_drawdown=0.0, previous=None):
    """(verdict, why). REJECT on any failed gate. WATCH until the paper forward record has
    min_forward_trades. Then PROMOTE if the forward record made money, else REJECT; a PROMOTE whose
    rolling record decays becomes DEMOTE."""
    failed = [x["gate"] for x in gates if not x["pass"]]
    if failed:
        return "REJECT", "%d of %d gates failed: %s" % (len(failed), len(gates), "; ".join(failed))
    ft = list(forward_trades)
    decayed, why = forward_decay(ft, g, oos_drawdown) if ft else (False, "")
    if decayed:
        return ("DEMOTE" if previous in ("PROMOTE", "DEMOTE") else "REJECT"), why
    need = int(g["min_forward_trades"])
    if len(ft) < need:
        if previous == "DEMOTE":
            return "DEMOTE", "demoted earlier; change the spec and run it again to start over"
        return "WATCH", ("every backtest gate passed; paper forward test it: %d of %d forward trades so far"
                         % (len(ft), need))
    fr = _mean([t["r"] for t in ft])
    gl = -sum(t["net"] for t in ft if t["net"] <= 0)
    pf = sum(t["net"] for t in ft if t["net"] > 0) / gl if gl > 0 else None
    if fr > 0 and (pf is None or pf >= 1.0):
        return "PROMOTE", "backtest gates passed and %d paper forward trades average %.3fR" % (len(ft), fr)
    return "REJECT", "the paper forward record did not hold up: %d trades average %.3fR" % (len(ft), fr)


# ---------------------------------------------------------------- the whole run

MIN_IS_TRADES = 5


def _warmup_days(spec, lookback):
    return lookback if spec["timeframe"] == "1d" else int(math.ceil(lookback / 75.0))


def options_hook(spec_doc, **kw):
    """An options spec's run: OPTIONS_HOOK when something registered one (fn(spec_doc, **kw) -> a results
    dict in this same shape), else this skill's options package (options/) on the archive's EOD chains."""
    if OPTIONS_HOOK is not None:
        return OPTIONS_HOOK(spec_doc, **kw)
    if strategy_api() is None:
        raise SpecError("options backtests use trader-strategy's legs and margin: put the trader-strategy folder "
                        "next to trader-lab")
    return options_api().run(spec_doc, kw.get("data"), kw.get("context"), kw.get("extra_trials", 0),
                             kw.get("progress"))


OPTIONS_HOOK = None


def run_lab(doc, data, context=None, fno=None, extra_trials=0, progress=None):
    """The full lab run on loaded data. Pure: returns the results dict; nothing is written.

    doc       the spec (dict)
    data      {symbol: Series} of the spec's timeframe
    context   {"nifty": Series, "vix": Series} daily, for the regime split (optional)
    fno       the F&O stock set for the session rules (None: every stock treated as an F&O stock)
    extra_trials  variants tried before (earlier spec versions), on top of the spec's trials_tried"""
    if isinstance(doc, dict) and doc.get("kind") == "options":
        return options_hook(doc, data=data, context=context, extra_trials=extra_trials, progress=progress)
    spec = validate_spec(doc)
    context = context or {}
    names, axes, pts, home = grid_points(spec)
    missing = [x for x in spec["universe"] if x not in data]
    if len(missing) == len(spec["universe"]):
        raise LabError("no bars for any symbol in the universe")
    days = session_days({k: v for k, v in data.items() if k in spec["universe"]})
    cap = float(spec["cfg"]["capital"])
    runs, lookback = [], 0
    for j, p in enumerate(pts):
        sp = validate_spec(doc, params=p)
        lookback = max(lookback, sp["lookback"])
        r = backtest(sp, data, 1.0, fno)
        runs.append({"params": p, "spec": sp, "res": r})
        if progress:
            progress("grid point %d of %d" % (j + 1, len(pts)))
    warm = min(_warmup_days(spec, lookback), len(days) // 2)
    days_used = days[warm:]
    me = runs[home]
    full = trade_stats([t for t in me["res"]["trades"] if t["signal_date"] >= days_used[0]], days_used, cap) \
        if days_used else trade_stats([], [], cap)
    full_trades = [t for t in me["res"]["trades"] if days_used and t["signal_date"] >= days_used[0]]

    wf = spec["walk_forward"]
    splits = walk_forward_splits(days, wf["in_sample"], wf["out_of_sample"], warm)
    folds, oos_trades, oos_days, chosen = [], [], [], []
    for ins, outs in splits:
        iset, oset = set(ins), set(outs)
        is_tr = [[t for t in r["res"]["trades"] if t["signal_date"] in iset] for r in runs]
        is_net = [sum(t["net"] for t in ts) for ts in is_tr]
        # a parameter set that hardly traded in-sample proves nothing: choose among those with 5+ trades
        able = [j for j in range(len(runs)) if len(is_tr[j]) >= MIN_IS_TRADES] or list(range(len(runs)))
        best = max(able, key=lambda j: (round(is_net[j], 2), j == home, -j))
        picked = [t for t in runs[best]["res"]["trades"] if t["signal_date"] in oset]
        oos_trades.extend(picked)
        oos_days.extend(outs)
        chosen.append(best)
        folds.append({"in_sample_days": [ins[0], ins[-1]], "out_of_sample_days": [outs[0], outs[-1]],
                      "params": {k: runs[best]["params"][k] for k in names}, "in_sample_net": round_paise(is_net[best]),
                      "stats": trade_stats(picked, outs, cap)})
    oos_trades.sort(key=lambda t: (t["exit_date"], t["exit_time"], t["symbol"]))
    oos = trade_stats(oos_trades, oos_days, cap)

    mults = sorted(set([1.5, 2.0, float(spec["gates"]["survive_cost_multiplier"])]))
    cost_full, cost_oos = {"1x": {k: full[k] for k in ("trades", "net", "expectancy_r", "profit_factor")}}, \
        {"1x": {k: oos[k] for k in ("trades", "net", "expectancy_r", "profit_factor")}}
    for m in mults:
        cache = {}
        for j in sorted(set(chosen) | {home}):
            cache[j] = backtest(runs[j]["spec"], data, m, fno)["trades"]
        ft = [t for t in cache[home] if days_used and t["signal_date"] >= days_used[0]]
        st = trade_stats(ft, days_used, cap)
        cost_full[_mkey(m)] = {k: st[k] for k in ("trades", "net", "expectancy_r", "profit_factor")}
        ot = []
        for (ins, outs), j in zip(splits, chosen):
            oset = set(outs)
            ot.extend(t for t in cache[j] if t["signal_date"] in oset)
        st = trade_stats(ot, oos_days, cap)
        cost_oos[_mkey(m)] = {k: st[k] for k in ("trades", "net", "expectancy_r", "profit_factor")}

    grid_rows, trial_sr, matrix_cols = [], [], []
    for j, r in enumerate(runs):
        ts = [t for t in r["res"]["trades"] if days_used and t["signal_date"] >= days_used[0]]
        st = trade_stats(ts, days_used, cap)
        grid_rows.append({"params": {k: r["params"][k] for k in names}, "trades": st["trades"], "net": st["net"],
                          "expectancy_r": st["expectancy_r"], "profit_factor": st["profit_factor"],
                          "max_drawdown_pct": st["max_drawdown_pct"], "home": j == home})
        trial_sr.append(st["sharpe_daily"])
        matrix_cols.append(daily_pnl(ts, days_used))
    near = neighbourhood(names, axes, pts, home, None)
    pos_share = sum(1 for j in near if grid_rows[j]["net"] > 0) / float(len(near))
    all_share = sum(1 for r in grid_rows if r["net"] > 0) / float(len(grid_rows))
    near_exp = percentile([grid_rows[j]["expectancy_r"] for j in near], 0.5)

    rets = [p / cap for p in daily_pnl(full_trades, days_used)]
    dsr = deflated_sharpe(rets, trial_sr if len(runs) > 1 else (), spec["trials_tried"] + extra_trials) \
        if len(rets) > 2 and _std(rets) > 0 else None
    matrix = [list(row) for row in zip(*matrix_cols)] if matrix_cols and days_used else []
    nb = 8 if len(days_used) >= 32 else (4 if len(days_used) >= 8 else 2)
    pbo = pbo_cscv(matrix, nb) if len(runs) > 1 and matrix else None
    mc = monte_carlo([t["net"] for t in oos_trades], spec["mc_runs"], spec["seed"])
    regimes = regime_split(full_trades, context, spec["vix_edges"])
    regs = lab_regimes(context)
    if regs:
        regimes["market"] = market_regimes(full_trades, oos_trades, regs, int(spec["gates"]["min_regime_trades"]))
    gates = evaluate_gates(spec["gates"], oos, full, dsr, pbo, cost_oos, pos_share)
    if spec["gates"]["require_regime_edge"]:
        gates.append(regime_gate(spec["gates"], oos_trades, regs))
    reasons = {}
    for k, v in me["res"]["skipped"].items():
        reasons[k] = v
    data_from = days[0] if days else None
    return {
        "schema": SCHEMA_RESULTS, "name": spec["name"], "title": spec["title"], "spec_hash": spec_hash(doc),
        "timeframe": spec["timeframe"], "product": spec["product"],
        "data": {"symbols": [x for x in spec["universe"] if x in data], "missing": missing, "from": data_from,
                 "to": days[-1] if days else None, "sessions": len(days), "warmup_sessions": warm,
                 "tested_from": days_used[0] if days_used else None,
                 "context": sorted(k for k, v in context.items() if v is not None)},
        "params": {k: spec["params"][k] for k in sorted(spec["params"])},
        "config": {k: spec["cfg"][k] for k in ("capital", "risk_per_trade_pct", "max_loss_per_trade", "max_daily_loss",
                                               "max_trades_per_day", "max_position_pct", "max_order_value",
                                               "cash_buffer_pct", "min_reward_risk", "flat_by", "broker_profile")},
        "costs": spec["costs"],
        "full": full, "skipped": reasons,
        "walk_forward": {"in_sample": wf["in_sample"], "out_of_sample": wf["out_of_sample"], "folds": folds,
                         "stats": oos, "latest_params": folds[-1]["params"] if folds else {k: spec["params"][k] for k in names}},
        "grid": {"params": names, "points": grid_rows, "neighbourhood": [grid_rows[j]["params"] for j in near],
                 "neighbourhood_positive": round(pos_share, 4), "grid_positive": round(all_share, 4),
                 "neighbourhood_median_expectancy_r": round(near_exp, 4)},
        "costs_sensitivity": {"whole_sample": cost_full, "out_of_sample": cost_oos},
        "monte_carlo": mc, "dsr": dsr, "pbo": pbo, "regimes": regimes, "gates": gates,
        "gates_set": spec["gates"],
        "trades": full_trades, "oos_trades": oos_trades,
        "equity": equity_rows(full_trades, days_used, cap, set(oos_days)),
    }


def equity_rows(trades, days, capital, oos_days=()):
    rows, eq, peak = [], float(capital), float(capital)
    pnl = daily_pnl(trades, days)
    n_by = {}
    for t in trades:
        n_by[t["exit_date"]] = n_by.get(t["exit_date"], 0) + 1
    for d, p in zip(days, pnl):
        eq = round_paise(eq + p)
        peak = max(peak, eq)
        rows.append({"date": d, "pnl": p, "equity": eq, "drawdown_pct": round((peak - eq) / capital * 100, 3),
                     "trades": n_by.get(d, 0), "out_of_sample": d in oos_days})
    return rows


# ---------------------------------------------------------------- the scorecard, in plain words

VERDICT_WORDS = {
    "REJECT": "REJECT: not good enough for the playbook on this evidence.",
    "WATCH": "WATCH: the backtest gates passed. Next is a paper forward test, not real money.",
    "PROMOTE": "PROMOTE: backtest gates passed and the paper forward record held up. Still not a promise.",
    "DEMOTE": "DEMOTE: it was promoted, and its rolling forward record decayed. Out of the playbook.",
}


def _r(x):
    return "%+.3fR" % x


def _pf(x):
    return "n/a" if x is None else "%.2f" % x


def does_not_prove(res):
    if res.get("kind") == "options":
        return options_api().does_not_prove(res)
    d = res["data"]
    lines = [
        "It does not predict the future. It shows how these exact rules would have traded %d sessions "
        "(%s to %s) of %s, with the costs modelled below." % (d["sessions"], d["from"], d["to"],
                                                             "5-minute bars" if res["timeframe"] == "5m" else "daily bars"),
        "Out of sample here means later bars of the SAME history the parameters were not chosen on. It is "
        "not the future; a paper forward test is the next real check.",
        "The universe (%s) is today's liquid names, picked after the fact: stocks that fell out of favour "
        "are missing (survivorship bias)." % ", ".join(d["symbols"][:12]),
        "Fills are a model: the next bar's open plus %g bps (at least %d tick), stops at the stop less the "
        "same. Real fills meet spreads, queues, circuit limits and your broker's own square-off."
        % (res["costs"]["slippage_bps"], res["costs"]["slippage_ticks"]),
        "Charges are a typical discount broker's (brokerage, STT, exchange, SEBI, stamp duty, GST). Check "
        "yours. Income tax on trading profits is not included.",
        "Yahoo Finance bars are free and unofficial: they can lag, skip bars and miss the closing auction.",
        "Sizes stay inside a %s capital with %s max loss a trade; much bigger orders would move the price."
        % (inr(res["config"]["capital"]), inr(res["config"]["max_loss_per_trade"])),
        "A pass on every gate is evidence that survived some tests, not a guarantee. Most strategies that "
        "pass a backtest still fail live.",
    ]
    if d["sessions"] < 120:
        lines.insert(1, "%d sessions is a short sample: one or two market moods. A few trades either way "
                        "change the numbers a lot." % d["sessions"])
    return lines


def scorecard(res, status=None):
    """The results as a plain-words markdown scorecard."""
    v = (status or {}).get("verdict") or res.get("verdict") or "REJECT"
    why = (status or {}).get("why") or res.get("verdict_why") or ""
    d, full, oos = res["data"], res["full"], res["walk_forward"]["stats"]
    opt = res.get("kind") == "options"
    L = ["# %s: %s" % (res["name"], res["title"]), ""]
    L.append("**Verdict: %s**" % VERDICT_WORDS[v])
    if why:
        L.append("")
        L.append("Why: %s." % why.rstrip("."))
    L.append("")
    if opt:
        L.append("Data: %s end-of-day option chains (NSE F&O bhavcopy settle prices), %s to %s, %d sessions, %s. "
                 "Rules: %s. Capital %s. Spec %s." % (res["underlying"], d["from"], d["to"], d["sessions"],
                                                     d.get("source") or "the archive", res.get("rules") or "",
                                                     inr(res["config"]["capital"]), res["spec_hash"]))
    else:
        L.append("Data: %d NSE stocks (%s), %s bars, %s to %s, %d sessions (the first %d warm up the indicators). "
                 "%s, %s. Spec %s." % (len(d["symbols"]), ", ".join(d["symbols"]), res["timeframe"], d["from"],
                                       d["to"], d["sessions"], d["warmup_sessions"], res["product"],
                                       "intraday, flat by the safe flat-by" if res["product"] == "MIS" else "delivery",
                                       res["spec_hash"]))
    if d["missing"]:
        L.append("No bars for: %s." % ", ".join(d["missing"]))
    L.append("")
    L.append("## The numbers, after charges and slippage")
    L.append("")
    L.append("| | whole sample | out of sample (walk-forward) |")
    L.append("| --- | --- | --- |")
    for label, k, fmt in (("trades", "trades", str), ("win rate", "win_rate", lambda x: "%.0f%%" % (x * 100)),
                          ("expectancy a trade", "expectancy_r", _r), ("profit factor", "profit_factor", _pf),
                          ("net P&L", "net", inr), ("charges paid", "charges", inr),
                          ("max drawdown", "max_drawdown_pct", lambda x: "%.2f%% of capital" % x),
                          ("Sharpe (annualised)", "sharpe_annual", lambda x: "%.2f" % x)):
        L.append("| %s | %s | %s |" % (label, fmt(full[k]), fmt(oos[k])))
    L.append("")
    if opt:
        L.extend(options_api().scorecard_lines(res))
    else:
        L.append("R is a trade's net result over the rupees it risked at the stop. Expectancy is the average R "
                 "per trade: +0.10R means about %s on a %s risk, before luck."
                 % (inr(0.1 * res["config"]["max_loss_per_trade"]), inr(res["config"]["max_loss_per_trade"])))
        L.append("")
    L.append("## Gates (written in the spec before the run)")
    L.append("")
    L.append("| gate | needs | got | |")
    L.append("| --- | --- | --- | --- |")
    for g in res["gates"]:
        L.append("| %s | %s | %s | %s |" % (g["gate"], g["needs"], g["got"], "pass" if g["pass"] else "**FAIL**"))
    L.append("")
    L.append("## Robustness")
    L.append("")
    wf = res["walk_forward"]
    L.append("- Walk-forward: %d windows of %d sessions to choose the parameters, each scored on the next %d. "
             "Out of sample: %d trades, %s a trade, %s net." % (len(wf["folds"]), wf["in_sample"], wf["out_of_sample"],
                                                               oos["trades"], _r(oos["expectancy_r"]), inr(oos["net"])))
    for f in wf["folds"]:
        L.append("  - %s to %s, chose %s: %d trades, %s" % (
            f["out_of_sample_days"][0], f["out_of_sample_days"][1],
            ", ".join("%s=%g" % kv for kv in sorted(f["params"].items())) or "the spec's params",
            f["stats"]["trades"], _r(f["stats"]["expectancy_r"])))
    g = res["grid"]
    L.append("- Parameter grid: %d points; %d%% of them make money after charges, %d%% of the %d around the "
             "spec's point (median %s there). A strategy that works only at one point is fitted to noise."
             % (len(g["points"]), round(g["grid_positive"] * 100), round(g["neighbourhood_positive"] * 100),
                len(g["neighbourhood"]), _r(g["neighbourhood_median_expectancy_r"])))
    mc = res["monte_carlo"]
    if mc:
        L.append("- Monte Carlo (%d runs, seed %d, out-of-sample trades): the same trades in another order "
                 "draw down %s at the median and %s in the worst 5%% (seen: %s). Drawn with replacement, "
                 "the total lands between %s and %s nine times in ten; %.0f%% of draws lose money."
                 % (mc["runs"], mc["seed"], inr(mc["drawdown_p50"]), inr(mc["drawdown_p95"]),
                    inr(mc["drawdown_actual"]), inr(mc["final_p5"]), inr(mc["final_p95"]), mc["prob_loss"] * 100))
    else:
        L.append("- Monte Carlo: no out-of-sample trades to reshuffle.")
    ds = res["dsr"]
    if not ds:
        L.append("- Deflated Sharpe ratio: not measured (no trades, or returns that never vary).")
    else:
        L.append("- Deflated Sharpe ratio %.2f (Sharpe a session %.3f over %d sessions, %d trials counted; "
                 "luck alone would show about %.3f). It is the probability the Sharpe is above zero after "
                 "allowing for the trials, skew and fat tails." % (ds["dsr"], ds["sharpe"], ds["observations"],
                                                                   ds["trials"], ds["sharpe_hurdle"]))
    pb = res["pbo"]
    if pb:
        L.append("- Probability of backtest overfitting %.0f%% (%d splits of %d blocks): how often the grid's "
                 "best in-sample point was no better than the median out of sample." % (pb["pbo"] * 100,
                                                                                        pb["splits"], pb["blocks"]))
    else:
        L.append("- Probability of backtest overfitting: not measured (it needs a grid of 2 or more points).")
    co = res["costs_sensitivity"]["out_of_sample"]
    L.append("- Costs: out-of-sample expectancy %s." % ", ".join("%s at %s" % (_r(co[k]["expectancy_r"]), k)
                                                              for k in sorted(co, key=lambda s: float(s[:-1]))))
    for kind, title in (("vix", "India VIX the day before"), ("trend", "NIFTY 50 trend the day before"),
                        ("iv_rank", "IV rank on the entry day")):
        rows = res["regimes"].get(kind) or []
        if rows:
            L.append("- By %s: %s." % (title, "; ".join("%s: %d trades, %s" % (r["regime"], r["trades"],
                                                                              _r(r["expectancy_r"])) for r in rows)))
    mk = res["regimes"].get("market")
    if mk:
        need = int((res.get("gates_set") or {}).get("min_regime_trades", 10))
        L.append("- By market regime (%s; R a trade with a 95%% interval; under %d trades is too few to judge):"
                 % (mk["basis"], need))
        for r in mk["whole_sample"]:
            ci = " (%.2fR to %.2fR)" % tuple(r["ci95_r"]) if r["ci95_r"] else ""
            L.append("  - %s: %d trades, %s%s%s" % (r["regime"], r["trades"], _r(r["expectancy_r"]), ci,
                                                   "" if r["enough"] else ", too few to judge"))
        L.append("  - The market now (%s): %s." % (mk["current"]["date"], mk["current"]["words"]))
    if res.get("skipped"):
        L.append("- Signals not taken: %s." % ", ".join("%s %d" % (k, v) for k, v in sorted(res["skipped"].items())))
    fw = (status or {}).get("forward") or res.get("forward")
    if fw:
        L.append("- Paper forward record: %d trades, %s a trade, %s net." % (fw["trades"], _r(fw["expectancy_r"]),
                                                                          inr(fw["net"])))
    L.append("")
    L.append("## What this does NOT prove")
    L.append("")
    for x in does_not_prove(res):
        L.append("- " + x)
    L.append("")
    L.append("Education and tooling, not financial advice. Nothing here placed an order.")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- files: trader/lab/<name>/

def lab_dir(out, name):
    return os.path.join(out, name)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise LabError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def write_equity_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("date,pnl,equity,drawdown_pct,trades,out_of_sample\n")
        for r in rows:
            fh.write("%s,%.2f,%.2f,%.3f,%d,%d\n" % (r["date"], r["pnl"], r["equity"], r["drawdown_pct"], r["trades"],
                                                    1 if r["out_of_sample"] else 0))


def library():
    """{name: spec} of the built-in library (trader-lab/library/*.json)."""
    out = {}
    if os.path.isdir(LIBRARY_DIR):
        for f in sorted(os.listdir(LIBRARY_DIR)):
            if f.endswith(".json"):
                with open(os.path.join(LIBRARY_DIR, f), encoding="utf-8") as fh:
                    doc = json.load(fh)
                out[doc.get("name") or f[:-5]] = doc
    return out


def resolve_spec(arg, out="trader/lab"):
    """A spec from a file path, your lab folder (trader/lab/<name>/spec.json) or the library."""
    if arg.endswith(".json") or os.path.sep in arg or "/" in arg:
        doc = read_json(arg)
        if doc is None:
            raise LabError("no spec file %s" % arg)
        return doc, arg
    p = os.path.join(lab_dir(out, arg), "spec.json")
    if os.path.exists(p):
        return read_json(p), p
    lib = library()
    if arg in lib:
        return copy.deepcopy(lib[arg]), "library/%s.json" % arg
    raise LabError("no strategy %r: not a file, not in %s, not in the library (%s)" % (arg, out, ", ".join(sorted(lib))))


def seen_by(bars, timeframe, now):
    """The bars complete at `now` (IST): a saved file replayed with --now sees no later bar."""
    if now is None:
        return bars
    day, m = now.date().isoformat(), now.hour * 60 + now.minute
    if timeframe == "5m":
        return [b for b in bars if b["date"] < day or (b["date"] == day and minutes_of(b["time"]) + 5 <= m)]
    return [b for b in bars if b["date"] < day or (b["date"] == day and m >= CLOSE_MIN)]


def load_options_data(spec, archive=None, now=None, progress=None):
    """(Chains, {"nifty", "vix"}, notes) for a compiled options spec, from the local archive only (the NSE
    F&O bhavcopies), as seen at `now`: a session's EOD chain counts once its day has closed."""
    root = archive or ARCHIVE_DIR
    api = archive_api()
    if api is None:
        raise LabError("options backtests read the local F&O archive: put the trader-archive folder next to "
                       "trader-lab")
    if not os.path.isdir(root):
        raise LabError("no archive at %s: options backtests need the EOD F&O chains (archive.py backfill "
                       "--years 2 --datasets fo,index,vix,lots)" % root)
    end = None
    if now is not None:
        end = now.date() if now.hour * 60 + now.minute >= CLOSE_MIN else now.date() - datetime.timedelta(days=1)
    chains = options_api().load_chains(spec["underlying"], root, api, None, end, progress)
    ctx = {}
    for key, sym in (("nifty", "^NSEI"), ("vix", "^INDIAVIX")):
        bars = archive_bars(sym, "1d", root)
        bars = seen_by(bars or [], "1d", now)
        ctx[key] = Series(sym, "1d", bars) if bars else None
    return chains, ctx, ["%s %s from the local archive (%s)" % (spec["underlying"], chains.source, root)]


def load_data(spec, data_dir=None, cache_dir=None, fetch=True, now=None, archive=None):
    """({symbol: Series}, {"nifty", "vix"}, notes) for a compiled spec, as seen at `now`. archive: a
    trader-archive folder to prefer over Yahoo (None: Yahoo and the cache only). An options spec reads
    the archive only and returns its Chains in place of the {symbol: Series}."""
    if spec.get("kind") == "options":
        return load_options_data(spec, archive, now)
    data, notes = {}, []
    from_archive = 0
    for sym in spec["universe"]:
        try:
            bars, src = load_bars(sym, spec["timeframe"], data_dir, cache_dir, fetch, now, archive=archive)
            from_archive += src.startswith("archive")
        except LabError as e:
            notes.append("%s: %s" % (sym, e))
            continue
        bars = seen_by(bars, spec["timeframe"], now)
        if bars:
            data[sym] = Series(sym, spec["timeframe"], bars)
    ctx = {}
    for key, sym in (("nifty", "^NSEI"), ("vix", "^INDIAVIX")):
        try:
            bars, _ = load_bars(sym, "1d", data_dir, cache_dir, fetch, now, archive=archive)
            bars = seen_by(bars, "1d", now)
            ctx[key] = Series(sym, "1d", bars) if bars else None
        except LabError as e:
            ctx[key] = None
            notes.append("regimes: %s" % e)
    if from_archive:
        notes.append("%d of %d symbols from the local archive (%s), the rest from Yahoo or the cache"
                     % (from_archive, len(spec["universe"]), archive))
    return data, ctx, notes


def forward_trades(out, name, spec_h):
    doc = read_json(os.path.join(lab_dir(out, name), "forward.json"), {}) or {}
    return [t for t in doc.get("trades") or [] if t.get("spec_hash") == spec_h]


def forward_summary(trades):
    if not trades:
        return {"trades": 0, "net": 0.0, "expectancy_r": 0.0, "profit_factor": None}
    st = trade_stats(trades, sorted(set(t["exit_date"] for t in trades)), 1.0)
    return {"trades": st["trades"], "net": st["net"], "expectancy_r": st["expectancy_r"],
            "profit_factor": st["profit_factor"]}


def update_status(out, name, verdict, why, spec_h, now, forward=None):
    path = os.path.join(lab_dir(out, name), "status.json")
    st = read_json(path, {}) or {}
    hist = st.get("history") or []
    if not hist or hist[-1].get("verdict") != verdict or hist[-1].get("spec_hash") != spec_h:
        hist.append({"at": now.strftime("%Y-%m-%d %H:%M"), "verdict": verdict, "why": why, "spec_hash": spec_h})
    hashes = st.get("spec_hashes") or []
    if spec_h not in hashes:
        hashes.append(spec_h)
    st.update({"name": name, "verdict": verdict, "why": why, "spec_hash": spec_h, "updated": now.strftime("%Y-%m-%d %H:%M"),
               "history": hist[-50:], "spec_hashes": hashes, "forward": forward or st.get("forward")})
    write_json(path, st)
    return st


def save_run(out, doc, res, now):
    """Write spec.json, results.json, scorecard.md, equity.csv and status.json; returns the status."""
    d = lab_dir(out, res["name"])
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, "spec.json"), doc)
    write_json(os.path.join(d, "results.json"), res)
    write_equity_csv(os.path.join(d, "equity.csv"), res["equity"])
    st = update_status(out, res["name"], res["verdict"], res["verdict_why"], res["spec_hash"], now, res.get("forward"))
    with open(os.path.join(d, "scorecard.md"), "w", encoding="utf-8") as fh:
        fh.write(scorecard(res, st))
    return st


def run_and_decide(doc, data, ctx, out, fno=None, now=None, progress=None):
    """run_lab + the verdict against the status and forward record kept in out/<name>/."""
    now = now or ist_now()
    name = doc.get("name")
    status = read_json(os.path.join(lab_dir(out, name), "status.json"), {}) or {} if name else {}
    h = spec_hash(doc)
    earlier = [x for x in status.get("spec_hashes") or [] if x != h]
    res = run_lab(doc, data, ctx, fno, extra_trials=len(earlier), progress=progress)
    fwd = forward_trades(out, res["name"], h)
    prev = status.get("verdict") if status.get("spec_hash") == h else None
    v, why = decide(res["gates"], res["gates_set"], fwd, res["walk_forward"]["stats"]["max_drawdown"], prev)
    res["verdict"], res["verdict_why"], res["forward"] = v, why, forward_summary(fwd)
    res["earlier_versions"] = len(earlier)
    res["run_at"] = now.strftime("%Y-%m-%d %H:%M")
    return res


# ---------------------------------------------------------------- the paper forward test

def next_session(day):
    d = datetime.date.fromisoformat(day) + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d.isoformat()


def forward(doc, data, out, today=None, fno=None, now=None, write=True):
    """Evaluate the latest bars with the strategy and draft paper plans for trader-budget.

    Settles the strategy's signals since the backtest's last day into the rolling forward record
    (forward.json), re-checks the verdict (a decayed PROMOTE becomes DEMOTE) and, for a WATCH or PROMOTE
    strategy, writes one trader-exit-shaped plan per fresh signal to out/<name>/forward/<date>/.
    It never sizes, checks or places anything itself: trader-budget sizes, trader-risk checks, and
    trader-broker asks the user yes."""
    now = now or ist_now()
    name = doc["name"]
    d = lab_dir(out, name)
    res = read_json(os.path.join(d, "results.json"))
    status = read_json(os.path.join(d, "status.json"), {}) or {}
    h = spec_hash(doc)
    if not res or res.get("spec_hash") != h:
        raise LabError("run it first (lab.py run %s): the forward test follows the spec that was backtested" % name)
    if doc.get("kind") == "options":
        return options_api().forward(doc, data, out, res, status, today, now, write)
    params = dict(validate_spec(doc)["base_params"])
    params.update(res["walk_forward"].get("latest_params") or {})
    spec = validate_spec(doc, params=params)
    bt = backtest(spec, data, 1.0, fno)
    cutoff = res["data"]["to"]
    days = session_days(data)
    today = today or (days[-1] if days else now.date().isoformat())
    fwd_doc = read_json(os.path.join(d, "forward.json"), {}) or {}
    have = {(t.get("symbol"), t.get("signal_date"), t.get("entry_time")) for t in fwd_doc.get("trades") or []}
    new = []
    for t in bt["trades"]:
        if t["signal_date"] <= cutoff or t["reason"] == "end of data":
            continue
        key = (t["symbol"], t["signal_date"], t["entry_time"])
        if key in have:
            continue
        new.append(dict(t, spec_hash=h, settled=now.strftime("%Y-%m-%d %H:%M"), params=params))
    trades = (fwd_doc.get("trades") or []) + new
    trades.sort(key=lambda t: (t["exit_date"], t["exit_time"], t["symbol"]))
    mine = [t for t in trades if t.get("spec_hash") == h]
    prev = status.get("verdict") if status.get("spec_hash") == h else None
    v, why = decide(res["gates"], res["gates_set"], mine, res["walk_forward"]["stats"]["max_drawdown"], prev)
    fs = forward_summary(mine)
    plans, notes = [], []
    if v in ("WATCH", "PROMOTE"):
        for p in bt["pending"]:
            if p["date"] != today:
                continue
            plans.append(plan_from_signal(spec, p, today, fno, h, v))
        for p in bt["open"]:
            if p["signal_date"] == today:
                notes.append("%s already triggered today (the lab's paper fill %s at %s): too late to draft a plan."
                             % (p["symbol"], "%.2f" % p["entry"], p["time"] or "the open"))
    else:
        notes.append("verdict %s: no paper plans. %s" % (v, why))
    written = []
    if write:
        write_json(os.path.join(d, "forward.json"), {"schema": "bharat-trader/lab-forward/1", "name": name,
                                                     "updated": now.strftime("%Y-%m-%d %H:%M"), "trades": trades})
        update_status(out, name, v, why, h, now, fs)
        for pl in plans:
            path = os.path.join(d, "forward", pl["date"], "plan-%s.json" % pl["symbol"])
            write_json(path, pl)
            written.append(path)
    return {"name": name, "today": today, "verdict": v, "why": why, "settled": len(new), "forward": fs,
            "plans": plans, "written": written, "notes": notes, "params": params}


def plan_from_signal(spec, p, today, fno, h, verdict):
    """A trader-exit-shaped plan from a pending lab signal: a LIMIT at the signal bar's close."""
    sym, close_px = p["symbol"], p["close"]
    tick = tick_for(close_px, spec["cfg"].get("tick_size"))
    entry = tick_round(close_px, tick)
    stop = p["stop"]
    risk = round(entry - stop, 2)
    if p["target"] is not None:
        target, label = p["target"], "the spec's target"
    else:
        target, label = tick_round(entry + 3 * risk, tick), ("3R, a cap for the order legs only: the strategy "
                                                             "exits on its own rule; watch it with trader-alerts")
    intraday = spec["timeframe"] == "5m"
    date = today if intraday else next_session(today)
    flat = None
    if intraday:
        cfg = dict(spec["cfg"], flat_by=(spec["cfg"].get("flat_by") if spec["flat_by"] == "auto" else spec["flat_by"]))
        flat = safe_flat_by(date, segment_of(sym, fno), cfg)[0].strftime("%H:%M")
    return {"created_by": "trader-lab", "symbol": sym, "exchange": "NSE", "side": "buy", "date": date,
            "setup": "lab:%s" % spec["name"], "entry": entry, "entry_time": p["time"] or "09:15",
            "interval": spec["timeframe"], "stop": stop, "stop_label": "the spec's stop rule",
            "target": target, "target_label": label, "risk_per_share": risk,
            "reward_risk": round((target - entry) / risk, 2) if risk > 0 else None,
            "flat_by": flat, "tick_size": tick,
            "lab": {"strategy": spec["name"], "spec_hash": h, "verdict": verdict, "product": spec["product"],
                    "note": "a paper forward-test signal, not a recommendation"}}


# ---------------------------------------------------------------- commands

def _progress(msg):
    sys.stderr.write("  %s\n" % msg)
    sys.stderr.flush()


def _fno(a, cfg):
    fetch = not a.data and not getattr(a, "offline", False)
    syms, note = load_fno_stocks(cfg, os.path.join("trader", "fno_stocks.json"), fetch=fetch)
    return syms, note


def cmd_list(a):
    lib = library()
    print("Built-in library (specs to test, not promises):")
    for n in sorted(lib):
        s = lib[n]
        print("  %-30s %-7s %s" % (n, "options" if s.get("kind") == "options" else s.get("timeframe"), s.get("title")))
    rows = []
    if os.path.isdir(a.out):
        for n in sorted(os.listdir(a.out)):
            st = read_json(os.path.join(a.out, n, "status.json"))
            res = read_json(os.path.join(a.out, n, "results.json"))
            if st and res:
                rows.append((n, st.get("verdict"), st.get("updated"), res["walk_forward"]["stats"]))
    print("")
    if not rows:
        print("No lab runs in %s yet. Try: lab.py run orb15-vwap" % a.out)
    else:
        print("Your lab runs (%s):" % a.out)
        for n, v, up, o in rows:
            print("  %-22s %-8s %s  out of sample: %d trades, %s" % (n, v, up, o["trades"], _r(o["expectancy_r"])))
    print("")
    print("Education and tooling, not financial advice.")
    return 0


def cmd_show(a):
    doc, src = resolve_spec(a.strategy, a.out)
    print(json.dumps(doc, indent=1, ensure_ascii=False))
    return 0


def cmd_validate(a):
    doc, src = resolve_spec(a.strategy, a.out)
    spec = validate_spec(doc)
    names, axes, pts, home = grid_points(spec)
    print("%s is a valid spec (%s, %s, %d symbols, %d grid point%s, hash %s)." % (
        src, spec["timeframe"], spec["product"], len(spec["universe"]), len(pts), "" if len(pts) == 1 else "s",
        spec_hash(doc)))
    return 0


def cmd_run(a):
    doc, src = resolve_spec(a.strategy, a.out)
    if a.seed is not None:
        doc["seed"] = a.seed
    spec = validate_spec(doc)
    now = ist_now(a.now)
    data, ctx, notes = load_data(spec, a.data, os.path.join(a.out, "data"), not a.offline, now,
                                 None if a.no_archive else a.archive)
    fno, fnote = _fno(a, spec["cfg"]) if spec.get("kind") != "options" else (None, "options: EOD chains, no F&O list needed")
    res = run_and_decide(doc, data, ctx, a.out, fno, now, None if a.json else _progress)
    res["notes"] = notes + [fnote]
    st = save_run(a.out, doc, res, now)
    if a.json:
        slim = {k: v for k, v in res.items() if k not in ("trades", "oos_trades", "equity")}
        print(json.dumps(slim, indent=1, ensure_ascii=False))
    else:
        print(scorecard(res, st))
        print("Wrote %s/{spec.json, results.json, scorecard.md, equity.csv, status.json}" % lab_dir(a.out, res["name"]))
        for n in res["notes"]:
            print("Note: %s" % n)
    return 0


def cmd_report(a):
    d = lab_dir(a.out, a.strategy)
    res = read_json(os.path.join(d, "results.json"))
    if not res:
        raise LabError("no results for %s in %s: run it first (lab.py run %s)" % (a.strategy, a.out, a.strategy))
    st = read_json(os.path.join(d, "status.json"), {})
    if a.json:
        print(json.dumps({"status": st, "results": {k: v for k, v in res.items()
                                                     if k not in ("trades", "oos_trades", "equity")}}, indent=1))
    else:
        print(scorecard(res, st))
    return 0


def compare_rows(out, names):
    rows = []
    for n in names:
        res = read_json(os.path.join(lab_dir(out, n), "results.json"))
        if not res:
            raise LabError("no results for %s: run it first" % n)
        st = read_json(os.path.join(lab_dir(out, n), "status.json"), {}) or {}
        o, f = res["walk_forward"]["stats"], res["full"]
        c15 = res["costs_sensitivity"]["out_of_sample"].get("1.5x") or {}
        rows.append({"name": n, "verdict": st.get("verdict") or res.get("verdict"), "timeframe": res["timeframe"],
                     "trades": f["trades"], "oos_trades": o["trades"], "oos_expectancy_r": o["expectancy_r"],
                     "oos_profit_factor": o["profit_factor"], "net": f["net"], "max_drawdown_pct": f["max_drawdown_pct"],
                     "dsr": (res.get("dsr") or {}).get("dsr"), "pbo": (res.get("pbo") or {}).get("pbo"),
                     "oos_expectancy_r_1_5x": c15.get("expectancy_r"),
                     "gates_passed": sum(1 for g in res["gates"] if g["pass"]), "gates": len(res["gates"])})
    return rows


def cmd_compare(a):
    rows = compare_rows(a.out, a.strategies)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    head = ("strategy", "verdict", "tf", "trades", "OOS tr", "OOS exp", "OOS PF", "net", "max DD", "DSR", "PBO",
            "OOS 1.5x", "gates")
    print("  ".join("%-10s" % h for h in head))
    for r in rows:
        cells = (r["name"][:18], r["verdict"], r["timeframe"], str(r["trades"]), str(r["oos_trades"]),
                 _r(r["oos_expectancy_r"]), _pf(r["oos_profit_factor"]), inr(r["net"]), "%.2f%%" % r["max_drawdown_pct"],
                 "-" if r["dsr"] is None else "%.2f" % r["dsr"], "-" if r["pbo"] is None else "%.2f" % r["pbo"],
                 "-" if r["oos_expectancy_r_1_5x"] is None else _r(r["oos_expectancy_r_1_5x"]),
                 "%d/%d" % (r["gates_passed"], r["gates"]))
        print("  ".join("%-10s" % c for c in cells))
    print("")
    print("OOS = out of sample (walk-forward). Expectancy in R after charges. Education and tooling, not financial advice.")
    return 0


def cmd_forward(a):
    doc, src = resolve_spec(a.strategy, a.out)
    spec = validate_spec(doc)
    now = ist_now(a.now)
    data, ctx, notes = load_data(spec, a.data, os.path.join(a.out, "data"), not a.offline, now,
                                 None if a.no_archive else a.archive)
    fno = _fno(a, spec["cfg"])[0] if spec.get("kind") != "options" else None
    r = forward(doc, data, a.out, a.date, fno, now)
    if a.json:
        print(json.dumps(r, indent=1))
        return 0
    print("%s, %s: verdict %s. %s." % (r["name"], r["today"], r["verdict"], r["why"].rstrip(".")))
    if r.get("kind") == "options":
        return _print_option_forward(r)
    print("Paper forward record: %d trades (%d settled now), %s a trade, %s net." % (
        r["forward"]["trades"], r["settled"], _r(r["forward"]["expectancy_r"]), inr(r["forward"]["net"])))
    for n in r["notes"]:
        print(n)
    if r["plans"]:
        print("")
        print("Draft paper plans (nothing is sized, checked or placed yet):")
        for pl, path in zip(r["plans"], r["written"] or [None] * len(r["plans"])):
            print("  %s buy, entry %.2f at %s, stop %.2f, target %.2f (%s)" % (
                pl["symbol"], pl["entry"], pl["entry_time"], pl["stop"], pl["target"], pl["target_label"]))
            if path:
                print("    python3 <trader-budget dir>/size.py --plan %s --write" % path)
        print("  then: python3 <trader-risk dir>/check.py, and python3 <trader-broker dir>/ticket.py show for "
              "your yes on each paper ticket.")
    elif r["verdict"] in ("WATCH", "PROMOTE"):
        print("No fresh signal on the latest bar.")
    print("Education and tooling, not financial advice.")
    return 0


def _print_option_forward(r):
    print("Paper forward record: %d positions (%d settled now), %s a position on margin, %s net." % (
        r["forward"]["trades"], r["settled"], _r(r["forward"]["expectancy_r"]), inr(r["forward"]["net"])))
    for n in r["notes"]:
        print(n)
    if r["baskets"]:
        print("")
        print("Draft paper baskets (nothing is checked or placed yet):")
        for b, path in zip(r["baskets"], r["written"] or [None] * len(r["baskets"])):
            st = b["strategy"]
            print("  %s %s, expiry %s, %s: margin about %s, max loss %s" % (
                st["underlying"], st["name"], b["expiry"], st["net_premium"]["kind"] + " " +
                inr(abs(st["net_premium"]["amount"])), inr(st["margin"]["required"]),
                "unlimited" if st["margin"]["max_loss_unlimited"] else inr(st["margin"]["max_loss"] or 0)))
            for o in b["orders"]["orders"]:
                print("    %s %d x %s %s %s at %.2f" % (o["side"], o["qty"], o["symbol"], o["strike"],
                                                      o["option_type"], o["price"]))
            if path:
                print("    copy %s to trader/orders.json and its strategy-*.json to trader/strategy.json," % path)
        print("  then: python3 <trader-risk dir>/check.py (it checks the legs as ONE basket), and python3 "
              "<trader-broker dir>/ticket.py show for your yes on each paper leg.")
    elif r["verdict"] in ("WATCH", "PROMOTE"):
        print("No entry on the latest session.")
    print("Education and tooling, not financial advice.")
    return 0


def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="lab.py", description="Strategy Lab: honest backtests on NSE bars.")
    ap.add_argument("--out", default=os.path.join("trader", "lab"), help="the lab folder (default trader/lab)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="the built-in library and your lab runs")
    for name, hlp in (("show", "print a spec"), ("validate", "check a spec")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("strategy")
    for name, hlp in (("run", "backtest, robustness, gates, verdict"),
                      ("forward", "today's signals -> draft paper plans; settles the forward record")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("strategy", help="a library name, a lab name or a spec file")
        p.add_argument("--data", help="offline: a folder of SYMBOL-5m.csv(.gz) / SYMBOL-1d.csv(.gz) bars")
        p.add_argument("--offline", action="store_true", help="use the cached bars in trader/lab/data only")
        p.add_argument("--archive", default=ARCHIVE_DIR,
                       help="a trader-archive folder to read bars from first (default trader/archive, if present)")
        p.add_argument("--no-archive", action="store_true", help="ignore the local archive: Yahoo and the cache only")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-30 15:40\"")
        p.add_argument("--json", action="store_true")
        if name == "run":
            p.add_argument("--seed", type=int, help="the Monte Carlo seed (default: the spec's, else 16)")
        else:
            p.add_argument("--date", help="the session to read signals on (default: the latest in the data)")
    p = sub.add_parser("report", help="the plain-words scorecard of a run")
    p.add_argument("strategy")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compare", help="key numbers of several runs side by side")
    p.add_argument("strategies", nargs="+")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"list": cmd_list, "show": cmd_show, "validate": cmd_validate, "run": cmd_run, "report": cmd_report,
                "compare": cmd_compare, "forward": cmd_forward}[a.cmd](a)
    except (LabError, RulesError) as e:
        print("lab: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
