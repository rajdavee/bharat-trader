#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan NSE charts for intraday setups. Standard library only.

    python3 scan.py universe                              # NSE EQUITY_L.csv -> trader/universe.txt
    python3 scan.py daily                                 # before 09:15 IST: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,HDFCBANK,M&M # or --watchlist FILE, or --limit 200
    python3 scan.py orb                                   # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15        # cut the clock at 10:15 IST
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-29 10:15"

universe  Downloads NSE's list of listed equities (EQUITY_L.csv from
          nsearchives.nseindia.com) and keeps the rows whose SERIES is EQ
          (--series EQ; BE and BZ are trade-for-trade, no intraday) and whose
          symbol is a plain NSE symbol (letters, digits, & and -, like M&M,
          BAJAJ-AUTO, 3MINDIA). Writes one SYMBOL|Name|ISIN a line.

daily     Pre-market scan of daily CLOSES (Yahoo spark, SYMBOL.NS, 20 symbols a
          request, 3 months of 1-day bars). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 15:30 close.
          Liquidity, in rupees: last close >= min_price (50) and the MEDIAN daily
          turnover (close x volume) of the last 20 sessions >= min_turnover
          (10 crore = 100000000). Spark has no daily volumes, so the turnover
          check runs in two stages: a cheap cut on the last session's turnover
          from the spark meta (at least half the floor), then each chart that
          matched a setup gets its v8 daily chart (real volumes, and it decides
          the setups) for the median, at most --max-confirm (300), the most
          traded first.
          Also prints one index line: NIFTY 50, BANK NIFTY and INDIA VIX, last
          close vs the close before it (one extra request; --no-index skips it).

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: the 09:15, 09:20 and 09:25 bars.
          Stage 1: spark closes for every symbol; a chart goes on only if a
          completed bar after the range closed at or above the highest close of
          the range (a necessary condition). Stage 2: each candidate's v8 chart
          (real highs and lows): range high = the highest high of the range
          bars, a hit = the first completed bar to CLOSE above it, at that bar's
          close time. At most --max-confirm (200) charts, the most traded first.
          A chart is usable only when every 5-minute bar from 09:15 to the cutoff
          is there (a missing bar is a halt, a circuit or no trades). Spark
          leaves out a close on the straight line between its neighbours;
          stage 1 puts it back on that line, stage 2 never fills a bar.
          Floors: --min-price 50, --min-turnover 0 (rupees traded so far today).

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are India Standard Time (IST,
UTC+05:30, no daylight saving). --now pins the clock ("2026-10-29 10:15",
IST; ...Z for UTC).

Calendar: NSE trading holidays for 2026 (Capital Market segment). Extra
holidays and special sessions (Muhurat trading) come from trader/config.json:
"extra_holidays": ["2027-01-26"], "special_sessions": [{"date": "2026-11-08",
"open": "18:00", "close": "19:00"}]. For a year with no table the calendar is
unknown: weekdays are treated as open, with a warning.

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants an
EQUITY_L.csv file (or a folder holding one); daily and orb take a folder of
<SYMBOL>.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"RELIANCE": <v8 json>, ...}. .gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
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

UA = "Mozilla/5.0 (compatible; bharat-trader/0.2; +https://github.com/rajdavee/bharat-trader)"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130 Safari/537.36")
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
EQUITY_FILE = "EQUITY_L.csv"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MAX_CONFIRM_DAILY = 300
MIN_DAILY_BARS = 30
TURNOVER_DAYS = 20
PREFILTER = 0.5     # daily stage 1: last session's turnover >= this share of min_turnover
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
CRORE = 10000000.0
DEFAULTS = {"min_price": 50.0, "min_turnover": 100000000.0, "no_entries_after": "14:45",
            "extra_holidays": [], "special_sessions": []}
INDEXES = [("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"), ("^INDIAVIX", "INDIA VIX")]

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)

NSE_HOLIDAYS = {  # Capital Market segment, NSE holiday master, 2026
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
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading: timing announced separately by NSE)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


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


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- money

def group(x, decimals=2):
    """Indian digit grouping: 12345678.9 -> '1,23,45,678.90'."""
    neg = x < 0
    s = "%.*f" % (decimals, abs(float(x)))
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian grouping)."""
    s = group(x)
    return "-₹" + s[1:] if s.startswith("-") else "₹" + s


def crore(x):
    """Rupees -> '₹12.34 cr'."""
    return "₹%s cr" % group(x / CRORE)


# ---------------------------------------------------------------- time

def to_ist(utc):
    """naive UTC -> naive IST."""
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST_OFFSET


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-29 10:15" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:15, not %r" % (flag, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """datetime or time -> '09:35' (24-hour IST)."""
    return "%02d:%02d" % (t.hour, t.minute)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def _special(d, cfg):
    for s in (cfg or {}).get("special_sessions") or []:
        if isinstance(s, dict) and str(s.get("date")) == d.isoformat():
            return s
    return None


def trading_day(d, cfg=None):
    """(is_trading_day, reason). A special session (Muhurat) opens a day; then
    weekends, the NSE holiday list and the config's extra_holidays close it."""
    if _special(d, cfg):
        return True, "special session"
    if d.weekday() >= 5:
        return False, "weekend"
    if d.isoformat() in [str(x) for x in (cfg or {}).get("extra_holidays") or []]:
        return False, "holiday (extra_holidays in config)"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def calendar_known(d):
    return d.year in CALENDAR_YEARS


def session(d, cfg=None, segment="EQ"):
    """(open_time, close_time) in IST, or None if closed all day. On a regular day the
    close is the end of continuous trading for the segment: an F&O stock (EQ_FNO) stops
    early for the closing auction from 2026-08-03 (SESSION_RULES)."""
    ok, _ = trading_day(d, cfg)
    if not ok:
        return None
    sp = _special(d, cfg)
    if sp:
        return parse_hhmm(sp.get("open"), "special_sessions open"), parse_hhmm(sp.get("close"), "special_sessions close")
    s = session_for(d, segment)
    return s["open"], s["continuous_end"]


def fno_set(cfg):
    """The F&O stock list the caller resolved (cfg["_fno"]), else the config's fno_stocks, else None."""
    cfg = cfg or {}
    return cfg["_fno"] if "_fno" in cfg else load_fno_stocks(cfg)[0]


# ---------------------------------------------------------------- network

def http_get(url, timeout=20, nse=False):
    h = {"User-Agent": NSE_UA if nse else UA, "Accept": "*/*"}
    if nse:
        h.update({"Accept": "text/csv,application/json,*/*", "Referer": "https://www.nseindia.com/"})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    """NSE symbol -> Yahoo symbol: RELIANCE -> RELIANCE.NS. An index (^NSEI) stays."""
    s = sym.strip().upper()
    return s if s.startswith("^") or s.endswith(".NS") else s + ".NS"


def from_yahoo(ysym):
    s = ysym.strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def q(ysym):
    """URL-encode one Yahoo symbol: M&M.NS -> M%26M.NS, ^NSEI -> %5ENSEI."""
    return urllib.parse.quote(ysym, safe=".-")


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(q(to_yahoo(s)) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (q(to_yahoo(sym)), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    qt = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = qt.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc):
    """Spark JSON -> {SYMBOL: series}, NSE symbols without .NS. Symbols Yahoo
    does not know are simply missing from the answer."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        out[from_yahoo(ysym)] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = from_yahoo(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    elif isinstance(doc, list):
        for v in doc:
            _absorb(v, "", data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYMBOL: series}, {SYMBOL: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def parse_equity_list(text):
    """NSE EQUITY_L.csv -> rows with clean keys (the header has leading spaces)."""
    text = text.lstrip("﻿")
    if "SYMBOL" not in text.split("\n", 1)[0].upper():
        raise ScanError("not NSE's EQUITY_L.csv (no SYMBOL header)")
    rd = csv.reader(io.StringIO(text))
    hdr = [h.strip().upper() for h in next(rd)]
    for need in ("SYMBOL", "NAME OF COMPANY", "SERIES"):
        if need not in hdr:
            raise ScanError("not NSE's EQUITY_L.csv (no %s column)" % need)
    rows = []
    for rec in rd:
        if not rec or not "".join(rec).strip():
            continue
        rows.append({k: (rec[i].strip() if i < len(rec) else "") for i, k in enumerate(hdr)})
    return rows


def classify(row, series=("EQ",)):
    """One EQUITY_L row -> 'keep' or why it is out."""
    sym = (row.get("SYMBOL") or "").strip().upper()
    ser = (row.get("SERIES") or "").strip().upper()
    if ser not in series:
        return "series %s" % (ser or "blank")
    if not TICKER.match(sym):
        return "not a plain symbol"
    return "keep"


def build_universe(text, series=("EQ",)):
    """EQUITY_L.csv text -> (kept [(SYMBOL, name, isin)], counts)."""
    rows = parse_equity_list(text)
    kept, counts = {}, {"rows": len(rows)}
    for r in rows:
        why = classify(r, series)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            sym = r["SYMBOL"].strip().upper()
            kept[sym] = (" ".join((r.get("NAME OF COMPANY") or "").split()).replace("|", "/"),
                         (r.get("ISIN NUMBER") or "").strip())
    return [(s, n, i) for s, (n, i) in sorted(kept.items())], counts


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_bars(series, now_utc, cfg=None):
    """[(date, close, volume or None)] of COMPLETE daily bars: null closes dropped,
    today's bar dropped until the session closes, anything after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today, cfg)
    today_done = s is None or now.time() >= s[1]
    vs = series.get("v")
    out = {}
    for i, (ts, c) in enumerate(zip(series["t"], series["c"])):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        v = vs[i] if vs and i < len(vs) else None
        out[d] = (float(c), v)  # one bar a day: a late duplicate (the live tick) wins
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def daily_closes(series, now_utc, cfg=None):
    return [(d, c) for d, c, _ in daily_bars(series, now_utc, cfg)]


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def median_turnover(bars, days=TURNOVER_DAYS):
    """Median close x volume of the last `days` complete sessions that have a volume."""
    tv = [c * v for _, c, v in bars[-days:] if v]
    return median(tv) if tv else None


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_turnover, confirm=None, max_confirm=None, cfg=None):
    """data = {SYMBOL: series} (spark or v8). Turnover: the median of the last 20
    sessions when the series has volumes (v8). A spark series has only the last
    session's volume (meta): it must reach PREFILTER x min_turnover, then
    confirm(sym) -> v8 daily series gives the median. Without confirm, the last
    session's turnover stands in (turnover_basis "last session").
    Returns (hits, counts, last_date)."""
    hits, dates, pending = [], {}, []
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0,
              "confirmed": 0, "unconfirmed": 0, "failed_confirm": 0, "setup_gone": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now_utc, cfg)
        meta = s.get("meta") or {}
        price = bars[-1][1] if bars else meta.get("regularMarketPrice")
        if price is None or price < min_price:
            counts["illiquid"] += 1
            continue
        has_vol = bool(s.get("v")) and any(v for _, _, v in bars)
        if has_vol:
            tv, basis = median_turnover(bars), "median 20d"
            last_vol = bars[-1][2] if bars else None
        else:
            last_vol = meta.get("regularMarketVolume")
            tv = (price * last_vol) if last_vol else None
            basis = "last session"
        floor = min_turnover if (has_vol or confirm is None) else min_turnover * PREFILTER
        if min_turnover > 0 and (tv is None or tv < floor):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c, _ in bars]
        tags, nums = find_setups(closes)
        if not tags:
            continue
        h = {"symbol": sym, "setups": tags, "price": round(closes[-1], 2), "close_date": d_last.isoformat(),
             "rsi": round(nums["rsi"], 1), "volume": int(last_vol) if last_vol else None,
             "turnover": round(tv, 2) if tv else None, "turnover_basis": basis}
        if has_vol or confirm is None:
            hits.append(h)
        else:
            pending.append(h)
    # stage 2: real daily volumes for the spark-only hits, most traded first
    pending.sort(key=lambda h: (-(h["turnover"] or 0), h["symbol"]))
    if max_confirm and len(pending) > max_confirm:
        counts["unconfirmed"] = len(pending) - max_confirm
        pending = pending[:max_confirm]
    for h in pending:
        try:
            s2 = confirm(h["symbol"])
        except ScanError:
            counts["failed_confirm"] += 1
            continue
        counts["confirmed"] += 1
        bars2 = daily_bars(s2, now_utc, cfg)
        tv = median_turnover(bars2)
        if min_turnover > 0 and (tv is None or tv < min_turnover):
            counts["illiquid"] += 1
            counts["scanned"] -= 1
            continue
        # spark's daily closes can skip or merge a session: the v8 chart decides the setups
        closes2 = [c for _, c, _ in bars2]
        tags2, nums2 = find_setups(closes2) if len(closes2) >= MIN_DAILY_BARS else ([], {})
        if not tags2:
            counts["setup_gone"] += 1
            continue
        h.update(setups=tags2, price=round(closes2[-1], 2), close_date=bars2[-1][0].isoformat(),
                 rsi=round(nums2["rsi"], 1), volume=int(bars2[-1][2]) if bars2[-1][2] else h["volume"],
                 turnover=round(tv, 2) if tv else None, turnover_basis="median 20d")
        hits.append(h)
    hits.sort(key=lambda h: (-(h["turnover"] or 0), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


def index_context(data, now_utc=None, cfg=None):
    """{^NSEI: series, ...} -> [{symbol, name, last, prev, change_pct, date}] from the last two
    COMPLETE daily closes at now_utc (today's bar counts once the session has closed)."""
    out = []
    for ysym, name in INDEXES:
        s = data.get(ysym)
        if not s:
            continue
        bars = daily_closes(s, now_utc or parse_now(None), cfg)
        c = [x for _, x in bars]
        if len(c) < 2:
            continue
        out.append({"symbol": ysym, "name": name, "last": round(c[-1], 2), "prev": round(c[-2], 2),
                    "change_pct": round((c[-1] / c[-2] - 1) * 100, 2), "date": bars[-1][0].isoformat()})
    return out


def index_line(ctx):
    if not ctx:
        return None
    return "Index context, %s close vs the close before: " % ctx[0]["date"] + ", ".join(
        "%s %s (%+.2f%%)" % (x["name"], group(x["last"]), x["change_pct"]) for x in ctx) + "."


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k, cfg=None):
    s = session(day, cfg)
    return datetime.datetime.combine(day, s[0] if s else OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, cfg=None, segment="EQ"):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST),
    on the grid, with a close, inside continuous trading. Sorted, one per slot."""
    s = session(day, cfg, segment)
    open_t, close_t = s if s else (OPEN, CLOSE)
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        it = ts_to_ist(ts)
        if it.date() != day or it.second or it.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if it.time() < open_t or it.time() >= close_t:
            continue  # pre-open, the closing auction, the 15:30 closing print
        if it + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[it] = {"start": it, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    if not hs:
        _fill_spark_gaps(out)
    return [out[k] for k in sorted(out)]


def _fill_spark_gaps(out):
    """Spark (closes only) thins its line: a 5-minute close that lies exactly on the
    straight line between its neighbours is left out (seen on every range, 1d and 5d;
    each one checked against the v8 chart). Put those slots back, on that line, between
    two bars of the same session. Only stage 1 sees them; stage 2 rechecks the v8 chart."""
    keys = sorted(out)
    step = datetime.timedelta(minutes=BAR_MIN)
    for a, b in zip(keys, keys[1:]):
        n = int((b - a).total_seconds() // (BAR_MIN * 60))
        for j in range(1, n):
            ca, cb = out[a]["c"], out[b]["c"]
            out[a + step * j] = {"start": a + step * j, "c": ca + (cb - ca) * j / n, "h": None, "l": None,
                                 "v": 0, "filled": True}


def usable(bars, day, cutoff, n_range, cfg=None, segment="EQ"):
    """Every slot from the open to the cutoff (or continuous trading's end) is there, and at least the range."""
    first = slot(day, 0, cfg)
    want = int((cutoff - first).total_seconds() // (BAR_MIN * 60))
    s = session(day, cfg, segment)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - first).total_seconds() // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k, cfg) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": hm(closes_at),
                    "bar_start": hm(b["start"]), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, cfg=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day, cfg)
    if not ok:
        raise ScanError("No session on %s (%s): the NSE is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day, cfg)
    open_dt = datetime.datetime.combine(day, s[0])
    range_end = slot(day, or_minutes // BAR_MIN, cfg)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (hm(at), hm(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (hm(at), hm(close_dt), day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The NSE closed at %s IST on %s. Replay the session with --at 10:15."
                            % (hm(close_dt), day_label(day)))
    if cutoff < first_close:
        po = session_for(day)["pre_open"]
        pre = " The pre-open call auction runs %s to %s; the normal session opens at %s." % (
            hm(po[0]), hm(po[1]), hm(s[0])) if cutoff < open_dt and not _special(day, cfg) else ""
        raise ScanError("It is %s IST: the %d-minute opening range runs %s to %s and the first bar after it "
                        "closes at %s. Run orb again after %s.%s"
                        % (hm(cutoff), or_minutes, hm(open_dt), hm(range_end), hm(first_close), hm(first_close), pre))
    if until is not None and cutoff.time() > until and s[0] == OPEN:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (hm(cutoff), hm(until), hm(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_turnover=0, confirm=None,
             max_confirm=None, cfg=None):
    """stage1 = {SYMBOL: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (rupees traded so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    fno = fno_set(cfg)
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        seg = segment_of(sym, fno)
        bars = session_bars(s, day, cutoff, cfg, seg)
        if not usable(bars, day, cutoff, n_range, cfg, seg):
            counts["unusable"] += 1
            continue
        if s.get("v"):
            traded = sum(b["c"] * b["v"] for b in bars)
        else:
            traded = bars[-1]["c"] * ((s.get("meta") or {}).get("regularMarketVolume") or 0)
        if bars[-1]["c"] < min_price or traded < min_turnover:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, traded))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            seg = segment_of(sym, fno)
            full = session_bars(s2, day, cutoff, cfg, seg)
            if not usable(full, day, cutoff, n_range, cfg, seg):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYMBOL: series}, {SYMBOL: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)))
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def norm_symbol(s):
    s = from_yahoo(str(s).strip().upper())
    if not (s.startswith("^") or TICKER.match(s)):
        raise ScanError("%r is not an NSE symbol (letters, digits, & and -, like M&M or BAJAJ-AUTO)" % s)
    return s


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        s = norm_symbol(s)
        if s.startswith("^"):
            continue  # indices are context, not scan targets
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    """trader/config.json -> dict with this tool's keys, defaults for missing ones."""
    cfg = dict(DEFAULTS)
    if not path or not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    if isinstance(doc, dict):
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
        for k in ("flat_by", "broker_profile", "square_off", "square_off_margin_min", "fno_stocks"):
            if doc.get(k) not in (None, ""):
                cfg[k] = doc[k]
    return cfg


def resolve_fno(a, cfg, today=None):
    """trader/fno_stocks.json: NSE's F&O list, refreshed once a day when this run is online."""
    cache = os.path.join(os.path.dirname(a.config or "trader/config.json") or ".", "fno_stocks.json")
    cfg["_fno"], note = load_fno_stocks(cfg, cache, fetch=not a.offline, today=today)
    return note


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _n(x):
    return "{:,}".format(x)


def _floors(a, cfg):
    mp = a.min_price if a.min_price is not None else float(cfg["min_price"])
    mt = a.min_turnover if a.min_turnover is not None else None
    return mp, mt


def calendar_warning(day):
    if calendar_known(day):
        return None
    return ("Warning: this tool has no NSE holiday list for %d; weekdays are treated as open. Add the year's "
            "holidays to extra_holidays in trader/config.json." % day.year)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    if a.offline:
        p = a.offline
        if os.path.isdir(p):
            p = os.path.join(p, EQUITY_FILE)
        if not os.path.exists(p):
            raise ScanError("no %s at %s" % (EQUITY_FILE, a.offline))
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        try:
            text = http_get(EQUITY_URL, timeout=60, nse=True)
        except urllib.error.HTTPError as e:
            raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s" % (e.code, EQUITY_FILE))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach nsearchives.nseindia.com: %s" % getattr(e, "reason", e))
    series = tuple(s.strip().upper() for s in a.series.split(",") if s.strip())
    kept, counts = build_universe(text, series)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name, isin in kept:
                fh.write("%s|%s|%s\n" % (sym, name, isin))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "series": list(series),
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE EQUITY_L.csv: %s rows." % _n(counts.get("rows", 0)))
        print("Kept %s NSE equities in series %s." % (_n(len(kept)), ", ".join(series)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % (_n(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name|ISIN a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    cfg = read_config(a.config)
    if not a.offline:
        progress(a, resolve_fno(a, cfg, to_ist(parse_now(None)).date()))
    min_price, min_turnover = _floors(a, cfg)
    if min_turnover is None:
        min_turnover = float(cfg["min_turnover"])
    offline_syms = None
    idx = {}
    confirm = None
    if a.offline:
        data, failed = load_offline(a.offline)
        idx = {k: v for k, v in data.items() if k.startswith("^")}
        offline_syms = sorted(k for k in set(data) | set(failed) if not k.startswith("^"))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s NSE symbols (%d spark requests)..."
                 % (_n(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
        if not a.no_index:
            try:
                SLEEP(a.pause)
                idx = parse_spark(yahoo_get(spark_path([s for s, _ in INDEXES], "5d", "1d")))
            except (ScanError, ValueError):
                idx = {}

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            return parse_chart(yahoo_get(chart_path(sym, "3mo", "1d")), sym)
    hits, counts, last_date = scan_daily(data, symbols, now, min_price, min_turnover, confirm,
                                         a.max_confirm, cfg)
    nfailed = len(symbols) - counts["fetched"]
    ctx = [] if a.no_index else index_context(idx, now, cfg)
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "exchange": "NSE", "universe": len(symbols), "scanned": counts["scanned"],
           "failed": nfailed, "unconfirmed": counts["unconfirmed"], "source": "yahoo",
           "filters": {"min_price": min_price, "min_turnover": min_turnover}, "index": ctx,
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo, NSE daily closes)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    line = index_line(ctx)
    if line:
        print(line)
    warn = calendar_warning(local.date())
    if warn:
        print(warn)
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s+ and a median daily turnover of %s+)%s; "
          "%s scanned." % (_n(counts["fetched"]), _n(len(symbols)),
                           (" (%s failed or had no data)" % _n(nfailed)) if nfailed else "",
                           _n(counts["fetched"] - counts["illiquid"]), inr(min_price), crore(min_turnover),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           _n(counts["scanned"])))
    if counts["confirmed"] or counts["unconfirmed"]:
        print("Spark has no daily volumes: the first cut used the last session's turnover (%s+); the median was "
              "then checked on the full daily chart of %s matching chart%s." % (
                  crore(min_turnover * PREFILTER), _n(counts["confirmed"]), "" if counts["confirmed"] == 1 else "s"))
    if counts["unconfirmed"] or counts["failed_confirm"]:
        print("PARTIAL: %s matching charts were not checked for turnover (only the %s most traded are; "
              "--max-confirm 0 checks all)%s." % (
                  _n(counts["unconfirmed"]), _n(a.max_confirm or 0),
                  (", %d failed to load" % counts["failed_confirm"]) if counts["failed_confirm"] else ""))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (median daily turnover):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-11s %12s  RSI %5.1f  %10s  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                                    crore(h["turnover"]) if h["turnover"] else "-",
                                                    ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    min_price, min_turnover = _floors(a, cfg)
    min_turnover = min_turnover or 0.0
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(k for k in set(data) | set(failed) if not k.startswith("^"))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cfg)
    resolve_fno(a, cfg, to_ist(parse_now(None)).date())
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (_n(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, min_price, min_turnover, confirm,
                                     a.max_confirm, cfg)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), hm(cutoff))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "exchange": "NSE", "universe": len(symbols), "scanned": counts["scanned"],
           "failed": nfailed, "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": min_price, "min_turnover": min_turnover}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), hm(cutoff), a.or_minutes, hm(slot(day, 0, cfg)),
             hm(slot(day, a.or_minutes // BAR_MIN, cfg))))
    warn = calendar_warning(day)
    if warn:
        print(warn)
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % (_n(counts["fetched"]), _n(len(symbols)),
             (" (%s failed or had no data)" % _n(nfailed)) if nfailed else "",
             _n(counts["scanned"]),
             (" (%s under the price / turnover floor)" % _n(counts["illiquid"])) if counts["illiquid"] else "",
             _n(counts["candidates"]), _n(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (_n(counts["unconfirmed"]), _n(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s IST:"
              % (len(hits), ("the %s checked" % _n(counts["confirmed"])) if counts["unconfirmed"]
                 else _n(counts["scanned"]), hm(cutoff)))
        for h in hits[:a.show]:
            print("  %5s  %-11s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["price"]), group(h["or_low"]), group(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s IST." % hm(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): NSE daily setups and "
                                             "opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's EQUITY_L.csv -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="PATH", help="a saved EQUITY_L.csv (or a folder holding one)")
    p.add_argument("--series", default="EQ", help="series to keep, comma-separated (default EQ)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p, max_confirm):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name|ISIN a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, help="rupees (default: config min_price, 50)")
        p.add_argument("--min-turnover", type=float, help="rupees (daily default: config min_turnover, "
                                                          "10 crore; orb default 0)")
        p.add_argument("--max-confirm", type=int, default=max_confirm,
                       help="stage-2 v8 charts, most traded first (%d; 0 = all)" % max_confirm)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-29 10:15" IST, or ...Z)')
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, MAX_CONFIRM_DAILY)
    p.add_argument("--no-index", action="store_true", help="skip the NIFTY / BANK NIFTY / VIX line")
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, MAX_CONFIRM)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM IST (default: config no_entries_after, 14:45)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, RulesError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
