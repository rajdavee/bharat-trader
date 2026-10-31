#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches an NSE setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check RELIANCE                         # opening-range breakout, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-10-29 10:15" --offline RELIANCE.json
    python3 entry.py check HDFCBANK --setup level --level 690   # first close above a price
    python3 entry.py check M&M --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch RELIANCE --every 30 --until 14:45    # one line per closed bar
    python3 entry.py watchlist                              # every hit in trader/watchlist.json

Setups (long only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars. Yahoo reports 0 volume on the NSE
         09:15 bar, so the average uses the range bars that have a volume.

A trigger's time is the trigger bar's CLOSE time in IST (the 09:35 bar closes
at 09:40) and its price is that close, to the paisa. Only bars on the
interval grid count (Yahoo's live tick, e.g. 10:49:45, is dropped) and a bar
counts only once it has closed: now >= its start + the interval.

Session: NSE cash market, IST (UTC+05:30, no daylight saving). Pre-open order
entry 09:00 to 09:08 (09:10 from 2026-09-07), normal session 09:15 to 15:30,
Monday to Friday, NSE trading holidays for 2026. An F&O stock (NSE's list,
cached in trader/fno_stocks.json, or "fno_stocks" in the config) stops
continuous trading at 15:15 from 2026-08-03 (15:30 from 2026-12-07) for the
Closing Auction Session: its bars end there and its last entry is 15 minutes
before the safe flat-by (SESSION_RULES, "broker_profile"). trader/config.json can add "extra_holidays"
(["YYYY-MM-DD"]) and "special_sessions" ([{"date", "open", "close"}], e.g.
Muhurat trading); on a special session entries run from 15 minutes after its
open to 15 minutes before its close, unless the entry gives its own
"no_entries_before" / "no_entries_after".

Statuses: triggered; waiting (pre-open, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 14:45);
no-data (the NSE is closed today or Yahoo sent no bars). Phases: pre-open,
forming, open, after-cutoff, after-close, closed.

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:45), IST, "flat_by" (15:10), "broker_profile",
"square_off", "square_off_margin_min" (5), "fno_stocks". Missing keys take
these defaults.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
  (M&M is M%26M.NS) falls back to query1 on HTTP 429 or 5xx; a 429 gets one
  retry after a pause. --offline PATH reads a saved chart JSON (.json or
  .json.gz) instead, or PATH/<SYMBOL>.json when PATH is a folder. Without
  --now, an offline file is read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
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

UA = "Mozilla/5.0 (compatible; bharat-trader/0.2; +https://github.com/rajdavee/bharat-trader)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:45"}
EPOCH = datetime.datetime(1970, 1, 1)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

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


class EntryError(Exception):
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


# ---------------------------------------------------------------- time

def to_ist(utc):
    """naive UTC -> naive IST (UTC+05:30 all year)."""
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-29 10:15" (or ...T10:15, or just 10:15 for
    today) is IST; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5) if t.count(":") == 1 else t.zfill(8))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants an IST time like "2026-10-29 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 14:45, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """09:40, 14:45: the contract's 24-hour IST form."""
    return "%02d:%02d" % (t.hour, t.minute)


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


def session(d, cfg=None, segment="EQ"):
    """(open_time, close_time) in IST, or None if closed all day. On a regular day the
    close is the end of continuous trading for the segment (15:15 for an F&O stock
    from 2026-08-03 to 2026-12-06: the closing auction follows; see SESSION_RULES)."""
    ok, _ = trading_day(d, cfg)
    if not ok:
        return None
    sp = _special(d, cfg)
    if sp:
        return (parse_hhmm(sp.get("open"), "special_sessions open"),
                parse_hhmm(sp.get("close"), "special_sessions close"))
    s = session_for(d, segment)
    return s["open"], s["continuous_end"]


def fno_set(cfg):
    """The F&O stock list the caller resolved (cfg["_fno"]), else the config's fno_stocks, else None."""
    cfg = cfg or {}
    return cfg["_fno"] if "_fno" in cfg else load_fno_stocks(cfg)[0]


def next_open(d, cfg=None):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = session(nd, cfg)
        if s:
            return "%s %s" % (nd.isoformat(), hm(s[0]))
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    """RELIANCE -> RELIANCE.NS (M&M -> M&M.NS; URL-encoded later)."""
    s = sym.strip().upper()
    return s if s.endswith(".NS") else s + ".NS"


def nse_symbol(sym):
    s = sym.strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def check_symbol(sym):
    s = nse_symbol(sym)
    if not TICKER.match(s):
        raise EntryError("%r is not an NSE symbol (letters, digits, & and -, like M&M or BAJAJ-AUTO)" % sym)
    return s


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(yahoo_symbol(sym), safe=".-"), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
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
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": nse_symbol(str(meta.get("symbol") or sym)), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz", yahoo_symbol(sym) + ".json", yahoo_symbol(sym) + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one NSE symbol, from Yahoo (range=1d for today) or a saved file."""
    sym = check_symbol(sym)
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or to_ist(utc_now()).date()
        text = yahoo_get(chart_path(sym, chart_range(day or today, today, interval), interval))
    ch = parse_chart(text, sym)
    if ch["symbol"] != sym:
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    """trader/config.json -> this tool's keys; missing keys take the defaults."""
    cfg = dict(DEFAULTS, extra_holidays=[], special_sessions=[])
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        if not isinstance(doc, dict):
            doc = {}
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
        for k in ("extra_holidays", "special_sessions"):
            if isinstance(doc.get(k), list):
                cfg[k] = doc[k]
        for k in RULE_KEYS:
            if doc.get(k) not in (None, ""):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


RULE_KEYS = ("flat_by", "broker_profile", "square_off", "square_off_margin_min", "fno_stocks")


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def entry_window(day, sess, cfg, segment="EQ"):
    """(no_entries_before, no_entries_after) as datetimes. On a regular session they are
    the config's times, the cutoff pulled in to 15 minutes before the safe flat-by
    (entry_cutoff: continuous trading's end and the broker's square-off, see
    SESSION_RULES). On a special session (Muhurat) they are the
    session entry's own "no_entries_before" / "no_entries_after" if given, else from
    the open plus the same lead as on a normal day (15 minutes by default) to 15
    minutes before that session's close."""
    o, c = sess
    before_t = parse_hhmm(cfg["no_entries_before"], "no_entries_before")
    after_t = parse_hhmm(cfg["no_entries_after"], "no_entries_after")
    if not _special(day, cfg):
        after_t = entry_cutoff(day, segment, cfg)
        return datetime.datetime.combine(day, before_t), datetime.datetime.combine(day, after_t)
    open_dt, close_dt = datetime.datetime.combine(day, o), datetime.datetime.combine(day, c)
    sp = _special(day, cfg) or {}
    if sp.get("no_entries_before"):
        before = datetime.datetime.combine(day, parse_hhmm(sp["no_entries_before"], "special_sessions no_entries_before"))
    else:
        lead = datetime.datetime.combine(day, before_t) - datetime.datetime.combine(day, OPEN)
        before = open_dt + max(lead, datetime.timedelta(0))
    if sp.get("no_entries_after"):
        after = datetime.datetime.combine(day, parse_hhmm(sp["no_entries_after"], "special_sessions no_entries_after"))
    else:
        after = close_dt - datetime.timedelta(minutes=15)
    before = min(max(before, open_dt), close_dt)
    after = max(min(after, close_dt), before)
    return before, after


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = to_ist(now_utc)
    day = local.date()
    res = {"symbol": chart["symbol"], "exchange": "NSE", "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"),
           "status": "waiting", "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS, "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_ist(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day, cfg)
    if not ok:
        res.update(status="no-data", phase="closed", reason="the NSE is closed today (%s)" % why,
                   next_open=next_open(day, cfg))
        return res
    fset = fno_set(cfg)
    seg = segment_of(chart["symbol"], fset)
    res["fno_list"] = fset is not None
    o, c = session(day, cfg, seg)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    special = _special(day, cfg) is not None
    before, cutoff = entry_window(day, (o, c), cfg, seg)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    rules = session_for(day, seg)
    res.update(opens=hm(o), closes=hm(c), special_session=special,
               no_entries_before=hm(before), no_entries_after=hm(cutoff), segment=seg,
               pre_open=[hm(rules["pre_open"][0]), hm(rules["pre_open"][1])],
               closing_auction=None if special or not rules["cas"] else
               [hm(rules["cas"]["start"]), hm(rules["cas"]["end"])], rule_from=rules["rule_from"])
    if not special:
        flat, note = safe_flat_by(day, seg, cfg)
        res.update(flat_by=hm(flat), flat_note=note)

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        it = to_ist(utc_of(b["t"]))
        if it.date() != day or not (open_dt <= it < close_dt):
            continue
        slot = int((it - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=it, end=it + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s IST, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_ist(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       hm(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        if special:
            res.update(phase="pre-open", reason="today's special session opens at %s%s" % (
                hm(o), " and the %d-minute range is set at %s" % (or_minutes, hm(or_end)) if setup == "orb" else ""))
        else:
            res.update(phase="pre-open", reason="the NSE opens at %s (pre-open order entry %s to %s)%s" % (
                hm(o), res["pre_open"][0], res["pre_open"][1],
                "; the %d-minute range is set at %s" % (or_minutes, hm(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    # Yahoo reports 0 volume on the NSE 09:15 bar (the pre-open auction's trades are not in it):
    # the range's average volume uses the range bars that have a volume
    vols = [b["v"] for b in or_bars if b["v"] > 0]
    avg_vol = (sum(vols) / float(len(vols))) if vols else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end,
                                "avg_volume": int(round(avg_vol)) if avg_vol is not None else None}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (hm(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": hm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < ist_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s IST, before this bar closed: its close can still move "
                                "a tick or two" % to_ist(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = "close above the range high %s" % inr(lvl) if setup == "orb" else "close above %s" % inr(lvl)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry" % (what, hm(cutoff)))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["et"]), inr(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            hm(last["et"]), inr(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            inr(last["c"]), hm(last["et"]), inr(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % inr(lvl) if setup == "orb" else "the level %s" % inr(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason). Times are IST HH:MM."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "exchange": "NSE", "setup": res["setup"],
            "side": "buy", "date": res["date"], "status": res["status"], "time": t.get("time"),
            "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"), "price": t.get("price"),
            "level": res.get("level"), "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "pre-open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % inr(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  NSE  %s %s IST  %s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            hm(start), hm(end), inr(orr["high"]), inr(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s IST at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], inr(t["price"]), t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", inr(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], inr(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                   "(exit.py plan --from trader/entry.json). Intraday (MIS) positions are squared off by "
                   "the broker; plan to be flat by %s." % (res.get("flat_by") or "15:10"))
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], inr(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("special_session"):
        out.append("Special session today %s-%s IST (from config): entries %s to %s." % (
            res["opens"], res["closes"], res["no_entries_before"], res["no_entries_after"]))
    elif res.get("closing_auction"):
        out.append("%s: continuous trading ends %s, then the closing auction (%s-%s IST); "
                   "entries until %s, flat by %s." % (
                       "F&O stock" if res.get("fno_list") else "No F&O list (trader/fno_stocks.json), so treated "
                       "as an F&O stock", res["closes"], res["closing_auction"][0],
                                                       res["closing_auction"][1], res["no_entries_after"],
                                                       res.get("flat_by")))
    if res.get("flat_note") and "too late" in res["flat_note"]:
        out.append(res["flat_note"][0].upper() + res["flat_note"][1:] + ".")
    if not res.get("calendar_known"):
        out.append("This tool has no NSE holiday list for %s: weekdays are treated as open. Check nseindia.com "
                   "and add holidays to extra_holidays in trader/config.json." % res["date"][:4])
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = hm(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (inr(min(x["l"] for x in seen)), inr(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (inr(orr["high"]), inr(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s IST, %s" % (
            inr(lvl), t["pct_over"], t["time"], inr(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % inr(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (inr(lvl - b["c"]), inr(lvl))
    return "%s bar  closed %s  %s" % (start, inr(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    cfg = dict(DEFAULTS, **(opts.get("cfg") or {}))
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_ist(now)
        day = local.date()
        s = session(day, cfg, segment_of(opts["symbol"], fno_set(cfg)) if opts.get("symbol") else "EQ")
        if not s:
            emit("%s: the NSE is closed today (%s). Nothing to watch. Next open: %s IST." % (
                day.isoformat(), trading_day(day, cfg)[1], next_open(day, cfg)))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                if local < open_dt:
                    state = "not open yet (%s)" % (
                        "pre-open order entry %s to %s" % tuple(hm(x) for x in session_for(day)["pre_open"])
                        if not _special(day, cfg) else "special session")
                else:
                    state = "open, no bar closed yet"
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), state, hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = ist_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **{k: v for k, v in opts.items() if k != "symbol"})
        close_dt = datetime.datetime.combine(day, parse_hhmm(res.get("closes") or hm(s[1]), "close"))
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "14:45", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("long side only: no short entries (intraday shorts are off in this pack). "
                         "Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_ist(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today), now


def resolve_fno(a, cfg):
    """Which stocks are in F&O (their continuous trading ends early under the closing
    auction): the config's fno_stocks, else trader/fno_stocks.json, refreshed from NSE
    once a day unless --offline."""
    cache = os.path.join(os.path.dirname(a.config) or ".", "fno_stocks.json")
    cfg["_fno"], cfg["_fno_note"] = load_fno_stocks(cfg, cache, fetch=not a.offline,
                                                    today=to_ist(utc_now()).date())


def cmd_check(a):
    cfg = read_config(a.config)
    resolve_fno(a, cfg)
    opts = _opts(a, cfg)
    sym = check_symbol(a.symbol)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, ensure_ascii=False))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    resolve_fno(a, cfg)
    opts = _opts(a, cfg)
    sym = check_symbol(a.symbol)
    opts["symbol"] = sym
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_ist(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    resolve_fno(a, cfg)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <SYMBOL>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, ensure_ascii=False))
    else:
        fmt = "%-11s  %-9s  %-5s  %-11s  %-11s  %-11s  %s"
        print(fmt % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-11s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print(fmt % (
                s, status[:9], t.get("time") or "-", inr(t["price"]) if t else "-",
                inr(r["level"]) if r.get("level") is not None else "-",
                inr(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches an NSE setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 10:15"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except (EntryError, RulesError) as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
