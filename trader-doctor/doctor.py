#!/usr/bin/env python3
"""trader-doctor: a self-test of every free data source the pack reads (NSE and Yahoo).

    python3 doctor.py                      # probe every source once, politely, and print a table
    python3 doctor.py --only nse           # only NSE (or --only yahoo)
    python3 doctor.py --json               # the same as one JSON document
    python3 doctor.py --offline DIR        # replay recorded answers, no network (repeatable: later wins)
    python3 doctor.py --record DIR         # save each live answer to DIR (for --offline and tests)
    python3 doctor.py --now "2026-11-03 11:00"   # judge freshness at another IST time

Each source gets one request (NSE: a browser User-Agent, Accept: application/json, the
nseindia.com Referer, 1.5 s between calls, one retry after 5 s on HTTP 403 or 429, and no
more NSE calls once NSE blocks twice). A source is:

ok       it answered, in the shape the tools read, fresh for the NSE calendar, units sane
slow     ok, but it took longer than 4 s
changed  it answered, but a field the tools need is gone, a unit looks wrong, the data is
         stale (older than the last session the calendar says it should have), or the
         address is gone (HTTP 404/410)
down     no answer, a timeout, or HTTP 5xx
blocked  HTTP 401, 403 or 429, or a web page instead of data (a bot filter)
skipped  not tried (no recorded answer offline, or NSE had already blocked this run)

Freshness follows the IST calendar: on a weekend or an NSE holiday the last session's
answer is the fresh one. "since" comes from a small history, trader/doctor.json: when each
source last changed status and when it was last ok. Exit code 0 when nothing is changed,
down or blocked, else 1 (2 for a usage error).

Standard library only. Nothing here places an order or writes anything but trader/doctor.json.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCHEMA = "bharat-trader/doctor/1"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130 Safari/537.36")
YAHOO_UA = "Mozilla/5.0 (compatible; bharat-trader/0.3; +https://github.com/rajdavee/bharat-trader)"
NSE = "https://www.nseindia.com/api/"
ARCHIVES = "https://nsearchives.nseindia.com/content/"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
TIMEOUT = 10.0
SLOW_S = 4.0
PAUSE = {"nse": 1.5, "yahoo": 0.5}
BACKOFF = 5.0
KEEP_RUNS = 30
STATUSES = ("ok", "slow", "changed", "down", "blocked", "skipped")
FAILED = ("changed", "down", "blocked")
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
SLEEP = time.sleep

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


class DoctorError(Exception):
    pass


class Changed(Exception):
    """The source answered, but not the way the tools read it."""


# ---------------------------------------------------------------- calendar (IST)

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise DoctorError('--now wants a time like "2026-11-03 11:00" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return dt - IST_OFFSET
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def to_ist(utc):
    return utc + IST_OFFSET


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS


def prev_trading_day(d):
    d -= datetime.timedelta(days=1)
    while not trading_day(d):
        d -= datetime.timedelta(days=1)
    return d


def last_session(now_ist):
    """The latest session that has started by now (IST): today after 09:15 on a trading day."""
    d = now_ist.date()
    if trading_day(d) and now_ist.time() >= OPEN:
        return d
    return prev_trading_day(d)


def last_close(now_ist):
    """The latest session that has closed by now (IST)."""
    d = now_ist.date()
    if trading_day(d) and now_ist.time() >= CLOSE:
        return d
    return prev_trading_day(d)


def early_in_session(now_ist):
    """The first half hour of a session: yesterday's answer may still be what a source has."""
    d = now_ist.date()
    t = now_ist.time()
    return trading_day(d) and OPEN <= t < datetime.time(9, 45)


def market_words(now_ist):
    d = now_ist.date()
    if not trading_day(d):
        why = "weekend" if d.weekday() >= 5 else "NSE holiday: %s" % NSE_HOLIDAYS.get(d.isoformat(), "")
        return "NSE closed (%s)" % why
    t = now_ist.time()
    if t < datetime.time(9, 0):
        return "before the pre-open"
    if t < OPEN:
        return "pre-open"
    if t < CLOSE:
        return "NSE open"
    return "after the close"


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def nse_day(text):
    """'30-Oct-2026 15:30', '30-OCT-2026', '30-10-2026' or '2026-10-30' -> date, else None."""
    s = str(text or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s.title() if "%b" in fmt else s, fmt).date()
        except ValueError:
            continue
    return None


def ddmmyyyy(d):
    return d.strftime("%d-%m-%Y")


# ---------------------------------------------------------------- shape and unit helpers

def need(row, keys, what):
    if not isinstance(row, dict):
        raise Changed("%s is not an object any more" % what)
    gone = [k for k in keys if k not in row]
    if gone:
        raise Changed("%s has no %s (the tools read %s)" % (what, ", ".join(gone), "it" if len(gone) == 1 else "them"))


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        raise Changed("not JSON any more")


def number(x, what, lo, hi):
    try:
        v = float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        raise Changed("%s is not a number (%r)" % (what, x))
    if not lo <= v <= hi:
        raise Changed("%s is %s, outside %s to %s: a unit change?" % (what, _num(v), _num(lo), _num(hi)))
    return v


def _num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def fresh(day, allowed, what):
    """Raise Changed when `day` is older than every allowed date."""
    if day is None:
        raise Changed("%s has no readable date" % what)
    if day < min(allowed):
        raise Changed("stale: %s is from %s, expected %s" % (what, day_label(day), day_label(max(allowed))))
    return day


def csv_rows(text):
    text = text.lstrip("﻿")
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    if not rows:
        raise Changed("an empty file")
    return [c.strip('"').strip() for c in rows[0]], rows[1:]


def header_has(header, want, what):
    low = [h.lower() for h in header]
    gone = [w for w in want if w.lower() not in low]
    if gone:
        raise Changed("%s has no column %s" % (what, ", ".join(gone)))
    return [low.index(w.lower()) for w in want]


# ---------------------------------------------------------------- the checks: one per source

def ck_equity_list(text, ctx):
    header, rows = csv_rows(text)
    header_has(header, ["SYMBOL", "SERIES", "ISIN NUMBER"], "EQUITY_L.csv")
    if len(rows) < 1000:
        raise Changed("only %d rows (NSE lists over 2,000 equities)" % len(rows))
    syms = {r[0] for r in rows}
    if "RELIANCE" not in syms:
        raise Changed("RELIANCE is not in the list")
    return "%d listed equities" % len(rows), None


def ck_market_status(text, ctx):
    doc = as_json(text)
    need(doc, ["marketState"], "the answer")
    cm = [m for m in doc["marketState"] if isinstance(m, dict) and m.get("market") == "Capital Market"]
    if not cm:
        raise Changed("no Capital Market row in marketState")
    need(cm[0], ["marketStatus", "tradeDate"], "the Capital Market row")
    day = nse_day(str(cm[0]["tradeDate"])[:11])
    fresh(day, ctx["session_allowed"], "tradeDate")
    if cm[0].get("last") not in (None, ""):
        number(cm[0]["last"], "NIFTY 50 last", 5000, 100000)
    return "Capital Market %s, trade date %s" % (cm[0]["marketStatus"], day.isoformat()), day


def ck_holidays(text, ctx):
    doc = as_json(text)
    need(doc, ["CM"], "the answer")
    rows = doc["CM"]
    if not isinstance(rows, list) or not rows:
        raise Changed("the CM holiday list is empty")
    need(rows[0], ["tradingDate", "description"], "a CM holiday")
    year = ctx["now_ist"].year
    theirs = {nse_day(r.get("tradingDate")) for r in rows if isinstance(r, dict)}
    theirs = {d.isoformat() for d in theirs if d and d.year == year}
    if year not in CALENDAR_YEARS:
        raise Changed("the tools have no %d holiday table; NSE lists %d holidays: update NSE_HOLIDAYS"
                      % (year, len(theirs)))
    ours = {d for d in NSE_HOLIDAYS if d.startswith(str(year))}
    if theirs != ours:
        extra, gone = sorted(theirs - ours), sorted(ours - theirs)
        raise Changed("NSE's %d list differs from the tools' table:%s%s" % (
            year, (" NSE adds " + ", ".join(extra)) if extra else "",
            (" NSE drops " + ", ".join(gone)) if gone else ""))
    return "%d CM holidays in %d, same as the tools' table" % (len(theirs), year), None


def _rows_list(doc, what):
    rows = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise Changed("%s is not a list of rows any more" % what)
    return rows


def ck_announcements(text, ctx):
    rows = _rows_list(as_json(text), "the answer")
    if not rows:
        return "no announcements in the window (an empty answer is normal for a quiet week)", None
    need(rows[0], ["symbol", "desc", "an_dt", "attchmntFile"], "an announcement")
    day = max(filter(None, (nse_day(str(r.get("an_dt"))[:11]) for r in rows)), default=None)
    return "%d announcements, latest %s" % (len(rows), day.isoformat() if day else "?"), day


def ck_board_meetings(text, ctx):
    rows = _rows_list(as_json(text), "the answer")
    if not rows:
        raise Changed("no board meetings at all for RELIANCE (NSE always lists past ones)")
    need(rows[0], ["bm_symbol", "bm_date", "bm_purpose"], "a board meeting")
    return "%d board meetings listed" % len(rows), None


def ck_results(text, ctx):
    doc = as_json(text)
    rows = _rows_list(doc, "the answer")
    if not rows:
        raise Changed("no integrated filings for RELIANCE")
    need(rows[0], ["symbol", "qe_Date", "xbrl", "broadcast_Date", "consolidated"], "a filing")
    qe = max(filter(None, (nse_day(r.get("qe_Date")) for r in rows)), default=None)
    if qe is not None and (ctx["today"] - qe).days > 200:
        raise Changed("stale: the latest quarter is %s" % qe.isoformat())
    return "%d filings, latest quarter %s" % (len(rows), qe.isoformat() if qe else "?"), qe


def _deals(text, ctx, what, required_rows):
    if text.lstrip().startswith(("<", "{")):
        raise Changed("%s is a page, not the CSV" % what)
    header, rows = csv_rows(text)
    i_date, i_qty, i_price = header_has(header, ["Date", "Quantity Traded", "Trade Price / Wght. Avg. Price"], what)
    header_has(header, ["Symbol", "Client Name"], what)
    if not {"buy/sell", "buy / sell"} & {h.lower() for h in header}:
        raise Changed("%s has no column Buy/Sell" % what)
    if not rows:
        if required_rows:
            raise Changed("%s has a header and no rows" % what)
        return "no deals in the file (some days have none)", None
    for r in rows[:5]:
        number(r[i_qty], "quantity", 1, 1e11)
        number(r[i_price], "price", 0.01, 1e6)
    day = max(filter(None, (nse_day(r[i_date]) for r in rows)), default=None)
    return "%d deals, latest %s" % (len(rows), day.isoformat() if day else "?"), day


def ck_bulk(text, ctx):
    summary, day = _deals(text, ctx, "bulk.csv", True)
    fresh(day, ctx["eod_allowed"], "the latest bulk deal")
    return summary, day


def ck_block(text, ctx):
    summary, day = _deals(text, ctx, "block.csv", False)
    if day is not None:
        fresh(day, ctx["eod_allowed"], "the latest block deal")
    return summary, day


def ck_deals_history(text, ctx):
    return _deals(text, ctx, "the bulk deals history", True)


def ck_pit(text, ctx):
    rows = _rows_list(as_json(text), "the answer")
    if not rows:
        raise Changed("no PIT disclosures in a week (there are dozens on any trading day)")
    need(rows[0], ["symbol", "xmlFileName", "broadcastDateTime", "regulation"], "a disclosure")
    day = max(filter(None, (nse_day(str(r.get("broadcastDateTime"))[:11]) for r in rows)), default=None)
    fresh(day, ctx["eod_allowed"], "the latest disclosure")
    return "%d disclosures, latest %s" % (len(rows), day.isoformat()), day


def ck_contract_info(text, ctx):
    doc = as_json(text)
    need(doc, ["expiryDates", "strikePrice"], "the answer")
    exp = [nse_day(x) for x in doc["expiryDates"] or []]
    if not exp or None in exp:
        raise Changed("expiryDates is empty or unreadable")
    if not doc["strikePrice"]:
        raise Changed("strikePrice is empty")
    number(doc["strikePrice"][len(doc["strikePrice"]) // 2], "a middle strike", 1000, 200000)
    future = [d for d in exp if d >= ctx["today"]]
    if not future:
        raise Changed("stale: every expiry is in the past (latest %s)" % max(exp).isoformat())
    ctx["nifty_expiry"] = min(future)
    return "%d expiries, next %s" % (len(exp), min(future).isoformat()), None


def ck_option_chain(text, ctx):
    doc = as_json(text)
    need(doc, ["records"], "the answer")
    rec = doc["records"]
    need(rec, ["data", "underlyingValue", "timestamp"], "records")
    if not rec["data"]:
        raise Changed("an empty chain (NSE answers that for a wrong expiry, or when it changes the format)")
    spot = number(rec["underlyingValue"], "NIFTY spot (underlyingValue)", 5000, 100000)
    row = next((r for r in rec["data"] if isinstance(r, dict) and r.get("CE")), None)
    if row is None:
        raise Changed("no CE leg in any row")
    need(row, ["strikePrice", "CE"], "a chain row")
    need(row["CE"], ["openInterest", "lastPrice", "totalTradedVolume", "impliedVolatility"], "a CE leg")
    number(row["CE"]["openInterest"], "openInterest (contracts)", 0, 1e9)
    day = nse_day(str(rec["timestamp"])[:11])
    fresh(day, ctx["session_allowed"], "the chain timestamp")
    return "%d strikes, spot %s at %s" % (len(rec["data"]), _num(spot), rec["timestamp"]), day


def ck_fo_mktlots(text, ctx):
    header, rows = csv_rows(text)
    header_has(header, ["UNDERLYING", "SYMBOL"], "fo_mktlots.csv")
    months = [m for m in header[2:] if m]
    if not months:
        raise Changed("no expiry month columns")
    try:
        first = datetime.datetime.strptime(months[0].title(), "%b-%y").date()
    except ValueError:
        raise Changed("the first month column is %r, not like NOV-26" % months[0])
    nifty = [r for r in rows if len(r) > 2 and r[1] == "NIFTY"]
    if not nifty:
        raise Changed("no NIFTY row")
    lot = number(nifty[0][2], "NIFTY lot size", 1, 2000)
    t = ctx["today"]
    this_month = datetime.date(t.year, t.month, 1)
    if first < this_month:
        raise Changed("stale: the first month is %s" % months[0])
    return "NIFTY lot %d from %s, %d rows" % (lot, months[0], len(rows)), None


def ck_fno_list(text, ctx):
    doc = as_json(text)
    if not isinstance(doc, list) or not all(isinstance(x, str) for x in doc):
        raise Changed("not a list of symbols any more")
    if not 100 <= len(doc) <= 500:
        raise Changed("%d F&O stocks (about 200 is normal)" % len(doc))
    if "RELIANCE" not in doc:
        raise Changed("RELIANCE is not in the F&O list")
    return "%d F&O stocks" % len(doc), None


def ck_vix(text, ctx):
    rows = _rows_list(as_json(text), "the answer")
    if not rows:
        raise Changed("no India VIX rows for the last two weeks")
    need(rows[-1], ["EOD_TIMESTAMP", "EOD_CLOSE_INDEX_VAL"], "a VIX row")
    for r in rows:
        number(r.get("EOD_CLOSE_INDEX_VAL"), "India VIX close", 5, 100)
    day = max(filter(None, (nse_day(r.get("EOD_TIMESTAMP")) for r in rows)), default=None)
    fresh(day, ctx["eod_allowed"], "the latest VIX close")
    return "%d closes, latest %s" % (len(rows), day.isoformat()), day


def _chart(text, ctx, symbol, lo, hi, currency):
    doc = as_json(text)
    need(doc, ["chart"], "the answer")
    res = (doc["chart"] or {}).get("result")
    if not res:
        err = (doc["chart"] or {}).get("error") or {}
        raise Changed("no result: %s" % (err.get("description") or "no data"))
    r = res[0]
    need(r, ["meta", "timestamp", "indicators"], "the chart")
    meta = r["meta"]
    need(meta, ["regularMarketPrice", "regularMarketTime", "gmtoffset"], "meta")
    if meta.get("gmtoffset") != 19800:
        raise Changed("gmtoffset is %s, not 19800 (IST)" % meta.get("gmtoffset"))
    if currency and meta.get("currency") != currency:
        raise Changed("currency is %s, not %s" % (meta.get("currency"), currency))
    q = (r["indicators"].get("quote") or [{}])[0]
    need(q, ["open", "high", "low", "close", "volume"], "the quote")
    price = number(meta["regularMarketPrice"], "%s price" % symbol, lo, hi)
    day = to_ist(EPOCH + datetime.timedelta(seconds=int(meta["regularMarketTime"]))).date()
    fresh(day, ctx["session_allowed"], "the last trade")
    return "%s %s, last trade %s" % (symbol, _num(price), day.isoformat()), day


def ck_chart_stock(text, ctx):
    return _chart(text, ctx, "RELIANCE.NS", 10, 1e6, "INR")


def ck_chart_index(text, ctx):
    return _chart(text, ctx, "^NSEI", 5000, 100000, "INR")


def ck_spark(text, ctx):
    doc = as_json(text)
    need(doc, ["spark"], "the answer")
    sp = doc["spark"] or {}
    got = {}
    for item in sp.get("result") or []:
        resp = (item or {}).get("response") or []
        if resp:
            got[item.get("symbol")] = resp[0]
    if not got:
        err = sp.get("error") or {}
        raise Changed("no result: %s" % (err.get("description") or "no symbols"))
    for sym in ("RELIANCE.NS", "^NSEI"):
        if sym not in got:
            raise Changed("%s is missing from the answer" % sym)
        need(got[sym], ["timestamp", "indicators", "meta"], "%s" % sym)
        closes = ((got[sym]["indicators"].get("quote") or [{}])[0] or {}).get("close")
        if not closes:
            raise Changed("%s has no closes" % sym)
    number(got["^NSEI"]["meta"].get("regularMarketPrice"), "^NSEI price", 5000, 100000)
    t = got["RELIANCE.NS"]["timestamp"]
    day = to_ist(EPOCH + datetime.timedelta(seconds=int(t[-1]))).date()
    fresh(day, ctx["session_allowed"], "the last RELIANCE.NS bar")
    return "%d symbols, last bar %s" % (len(got), day.isoformat()), day


def ck_rss(text, ctx):
    if not text.lstrip().startswith("<"):
        raise Changed("not XML any more")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        raise Changed("not well-formed XML")
    ch = root.find("channel")
    if root.tag != "rss" or ch is None:
        raise Changed("not an RSS feed (no rss/channel)")
    items = ch.findall("item")
    if not items:
        return "an empty feed (Yahoo's coverage of Indian stocks is patchy; the tools still read NSE)", None
    for tag in ("title", "link", "pubDate"):
        if items[0].find(tag) is None:
            raise Changed("an item has no %s" % tag)
    return "%d headlines" % len(items), None


# ---------------------------------------------------------------- the sources

def _ann_url(ctx):
    t = ctx["today"]
    return NSE + "corporate-announcements?index=equities&symbol=RELIANCE&from_date=%s&to_date=%s" % (
        ddmmyyyy(t - datetime.timedelta(days=7)), ddmmyyyy(t))


def _pit_url(ctx):
    t = ctx["today"]
    return NSE + "corporates-pit-gg?index=equities&from_date=%s&to_date=%s" % (
        ddmmyyyy(t - datetime.timedelta(days=7)), ddmmyyyy(t))


def _deals_url(ctx):
    t = ctx["today"]
    return NSE + "historicalOR/bulk-block-short-deals?optionType=bulk_deals&from=%s&to=%s&csv=true" % (
        ddmmyyyy(t - datetime.timedelta(days=7)), ddmmyyyy(t))


def _vix_url(ctx):
    t = ctx["today"]
    return NSE + "historicalOR/vixhistory?from=%s&to=%s" % (ddmmyyyy(t - datetime.timedelta(days=14)), ddmmyyyy(t))


def _chain_url(ctx):
    exp = ctx.get("nifty_expiry")
    if exp is None:
        return None
    return NSE + "option-chain-v3?type=Indices&symbol=NIFTY&expiry=%s" % exp.strftime("%d-%b-%Y")


def _yahoo(path):
    return lambda ctx: YAHOO_HOSTS[0] + path


def _fno_cache(ctx, status=None):
    p = os.path.join(ctx["trader_dir"], "fno_stocks.json")
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        return "using the cached list of %s (%d stocks)" % (doc.get("fetched"), len(doc.get("symbols") or []))
    except (OSError, ValueError, AttributeError, TypeError):
        return "no cache: every stock is treated as an F&O stock (the earlier close)"


def _query1(ctx, status):
    if status in ("down", "blocked"):
        return "query1 when query2 fails (the tools retry there)"
    return "none: query1 sends the same answer"


def _universe(ctx, status=None):
    p = os.path.join(ctx["trader_dir"], "universe.txt")
    if os.path.exists(p):
        day = datetime.datetime.fromtimestamp(os.path.getmtime(p)).date()
        return "using trader/universe.txt from %s" % day.isoformat()
    return "no trader/universe.txt yet: pass --symbols"


# id, group, name, url(ctx), check, affects, fallback (plain words; a function says what is in use)
SOURCES = [
    ("nse-equity-list", "nse", "NSE equity list (EQUITY_L.csv)",
     lambda ctx: ARCHIVES + "equities/EQUITY_L.csv", ck_equity_list,
     "scanner universe", _universe),
    ("nse-market-status", "nse", "NSE market status",
     lambda ctx: NSE + "marketStatus", ck_market_status,
     "alerts clock --verify, UI", "the built-in NSE calendar (the tools' own clock)"),
    ("nse-holidays", "nse", "NSE holiday master",
     lambda ctx: NSE + "holiday-master?type=trading", ck_holidays,
     "every tool's calendar", "the built-in 2026 holiday table"),
    ("nse-announcements", "nse", "NSE corporate announcements", _ann_url, ck_announcements,
     "news", "Yahoo headlines only"),
    ("nse-board-meetings", "nse", "NSE board meetings",
     lambda ctx: NSE + "corporate-board-meetings?index=equities&symbol=RELIANCE", ck_board_meetings,
     "news, analyst", "none: results dates are not flagged"),
    ("nse-results", "nse", "NSE results (integrated filings)",
     lambda ctx: NSE + "integrated-filing-results?index=equities&symbol=RELIANCE&period_ended=all"
                       "&type=Integrated%20Filing-%20Financials", ck_results,
     "analyst", "none: the older results list stops at Dec-2024"),
    ("nse-bulk", "nse", "NSE bulk deals (bulk.csv)",
     lambda ctx: ARCHIVES + "equities/bulk.csv", ck_bulk,
     "insider deals", "insider deals --days N (the history API)"),
    ("nse-block", "nse", "NSE block deals (block.csv)",
     lambda ctx: ARCHIVES + "equities/block.csv", ck_block,
     "insider deals", "insider deals --days N (the history API)"),
    ("nse-deals-history", "nse", "NSE bulk and block deals history", _deals_url, ck_deals_history,
     "insider deals --days", "none"),
    ("nse-pit", "nse", "NSE insider trading (SEBI PIT)", _pit_url, ck_pit,
     "insider", "none for filings after the 2025 switch"),
    ("nse-contract-info", "nse", "NSE option contract info (NIFTY)",
     lambda ctx: NSE + "option-chain-contract-info?symbol=NIFTY", ck_contract_info,
     "options (expiries)", "none: options stops"),
    ("nse-option-chain", "nse", "NSE option chain v3 (NIFTY)", _chain_url, ck_option_chain,
     "options, UI option chain", "none: options stops"),
    ("nse-fo-mktlots", "nse", "NSE F&O lot sizes (fo_mktlots.csv)",
     lambda ctx: ARCHIVES + "fo/fo_mktlots.csv", ck_fo_mktlots,
     "options (lot sizes)", "none: options shows no lot sizes"),
    ("nse-fno-list", "nse", "NSE F&O stock list (master-quote)",
     lambda ctx: NSE + "master-quote", ck_fno_list,
     "scanner, entry, exit, timemachine, risk", _fno_cache),
    ("nse-vix-history", "nse", "NSE India VIX history", _vix_url, ck_vix,
     "options (IV rank)", "none: IV rank and percentile are left out"),
    ("yahoo-chart", "yahoo", "Yahoo chart (RELIANCE.NS)",
     _yahoo("/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"), ck_chart_stock,
     "scanner, entry, exit, timemachine, alerts, UI", _query1),
    ("yahoo-chart-index", "yahoo", "Yahoo chart (^NSEI)",
     _yahoo("/v8/finance/chart/%5ENSEI?range=1d&interval=5m"), ck_chart_index,
     "scanner index line, news --market, UI", _query1),
    ("yahoo-spark", "yahoo", "Yahoo spark (RELIANCE.NS, ^NSEI)",
     _yahoo("/v7/finance/spark?symbols=RELIANCE.NS,%5ENSEI&range=1d&interval=5m"), ck_spark,
     "scanner daily and orb", _query1),
    ("yahoo-rss", "yahoo", "Yahoo headlines RSS (RELIANCE.NS)",
     lambda ctx: "https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN", ck_rss,
     "news", "NSE announcements only"),
]
BY_ID = {s[0]: s for s in SOURCES}


# ---------------------------------------------------------------- fetching: live and recorded

class Answer(object):
    def __init__(self, url, status=None, body=b"", elapsed=0.0, error=None, content_type=""):
        self.url, self.status, self.body, self.elapsed = url, status, body, elapsed
        self.error, self.content_type = error, content_type

    def text(self):
        return self.body.decode("utf-8", "replace") if isinstance(self.body, bytes) else str(self.body)

    def record(self, sid, at):
        return {"id": sid, "url": self.url, "fetched_at": at, "status": self.status,
                "content_type": self.content_type, "elapsed": round(self.elapsed, 3), "error": self.error,
                "body": self.text()}


def http_get(url, nse, timeout=TIMEOUT):
    if nse:
        h = {"User-Agent": NSE_UA, "Referer": "https://www.nseindia.com/", "Accept-Encoding": "gzip",
             "Accept": "application/json" if "/api/" in url else "text/csv,*/*"}
    else:
        h = {"User-Agent": YAHOO_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return Answer(url, r.status, body, time.time() - t0, None, r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
            if e.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
        except Exception:
            body = b""
        finally:
            e.close()
        return Answer(url, e.code, body, time.time() - t0, None, e.headers.get("Content-Type") or "")
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        msg = "timed out after %d s" % timeout if isinstance(reason, socket.timeout) or "timed out" in str(reason) \
            else str(reason)
        return Answer(url, None, b"", time.time() - t0, msg, "")


class Recorded(object):
    """--offline DIR [DIR ...]: DIR/<id>.json(.gz) holds one recorded answer; a later DIR wins."""

    def __init__(self, dirs):
        self.dirs = [os.path.abspath(d) for d in dirs]
        for d in self.dirs:
            if not os.path.isdir(d):
                raise DoctorError("--offline %s is not a folder" % d)

    def get(self, name):
        for d in reversed(self.dirs):
            for ext in (".json", ".json.gz"):
                p = os.path.join(d, name + ext)
                if os.path.isfile(p):
                    opener = gzip.open if ext.endswith(".gz") else open
                    with opener(p, "rt", encoding="utf-8") as fh:
                        try:
                            doc = json.load(fh)
                        except ValueError as e:
                            raise DoctorError("%s is not valid JSON: %s" % (p, e))
                    return Answer(doc.get("url", ""), doc.get("status"), (doc.get("body") or "").encode("utf-8"),
                                  float(doc.get("elapsed") or 0), doc.get("error"), doc.get("content_type") or "")
        return None


def classify(ans, kind):
    """(status, detail) for the transport part, or None when the body should be checked."""
    if ans.error:
        return "down", "no answer: %s" % ans.error
    code = ans.status
    if code in (401, 403, 429):
        why = "too many requests" if code == 429 else "refused (a bot filter or a block)"
        return "blocked", "HTTP %d: %s" % (code, why)
    if code in (404, 410):
        return "changed", "HTTP %d: the address is gone" % code
    if code is None or code >= 500 or code != 200:
        return "down", "HTTP %s" % code
    head = ans.text().lstrip()[:400].lower()
    if kind != "xml" and (head.startswith("<!doctype") or head.startswith("<html") or "access denied" in head):
        return "blocked", "a web page instead of data (a bot filter or maintenance)"
    return None


# ---------------------------------------------------------------- the run

def run(only=None, offline=None, record=None, now_utc=None, trader_dir="trader", log=None):
    now_utc = now_utc or parse_now(None)
    now_ist = to_ist(now_utc)
    today = now_ist.date()
    sess, eod = last_session(now_ist), last_close(now_ist)
    ctx = {"now_ist": now_ist, "today": today, "trader_dir": trader_dir,
           "session_allowed": [sess] + ([prev_trading_day(sess)] if early_in_session(now_ist) else []),
           "eod_allowed": [eod, prev_trading_day(eod)]}
    rec = Recorded(offline) if offline else None
    if record:
        os.makedirs(record, exist_ok=True)
    at = now_ist.replace(microsecond=0).isoformat() + "+05:30"
    rows = []
    last_group, blocked_by = None, None
    for sid, group, name, url_fn, check, affects, fallback in SOURCES:
        if only and group != only:
            continue
        row = {"id": sid, "group": group, "name": name, "affects": affects, "url": None, "status": "skipped",
               "http": None, "seconds": None, "detail": "", "data_date": None}
        rows.append(row)
        url = url_fn(ctx)
        row["url"] = url
        if url is None:
            row["detail"] = "not tried: needs the expiry from the contract info"
            continue
        if group == "nse" and blocked_by:
            row["detail"] = "not tried: NSE blocked %s twice in this run (be polite; try later)" % blocked_by
            continue
        kind = "xml" if "rss" in url else ("csv" if url.endswith(".csv") or "csv=true" in url else "json")
        if rec:
            ans = rec.get(sid)
            if ans is None:
                row["detail"] = "no recorded answer in the offline folder"
                continue
        else:
            if last_group is not None:
                SLEEP(PAUSE[group])
            last_group = group
            if log:
                log("  %s ..." % name)
            ans = http_get(url, group == "nse")
            if ans.status in (403, 429) and group == "nse":
                SLEEP(BACKOFF)
                ans = http_get(url, True)
                if ans.status in (403, 429):
                    blocked_by = name
        if record:
            _save(record, sid, ans.record(sid, at))
        status, detail, day = _judge(ans, kind, check, ctx)
        if status in ("down", "blocked") and group == "yahoo":
            alt = _alt(ans, sid, rec, record, at)
            if alt is not None:
                a_status, a_detail, _ = _judge(alt, kind, check, ctx)
                row["fallback_used"] = "query1: %s" % (a_status if a_status not in ("ok", "slow") else "answering")
        row.update({"status": status, "detail": detail, "http": ans.status, "seconds": round(ans.elapsed, 2),
                    "data_date": day.isoformat() if day else None})
    for row in rows:
        fb = BY_ID[row["id"]][6]
        row["fallback"] = fb(ctx, row["status"]) if callable(fb) else fb
    return {"schema": SCHEMA, "checked_at": at, "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
            "market": market_words(now_ist), "last_session": sess.isoformat(), "last_close": eod.isoformat(),
            "offline": bool(rec), "only": only, "sources": rows, "summary": summarise(rows)}


def _judge(ans, kind, check, ctx):
    t = classify(ans, kind)
    if t:
        return t[0], t[1], None
    try:
        summary, day = check(ans.text(), ctx)
    except Changed as e:
        return "changed", str(e), None
    if ans.elapsed > SLOW_S:
        return "slow", "%s (took %.1f s)" % (summary, ans.elapsed), day
    return "ok", summary, day


def _alt(ans, sid, rec, record, at):
    if rec:
        return rec.get(sid + "~query1")
    if not ans.url.startswith(YAHOO_HOSTS[0]):
        return None
    SLEEP(PAUSE["yahoo"])
    alt = http_get(YAHOO_HOSTS[1] + ans.url[len(YAHOO_HOSTS[0]):], False)
    if record:
        _save(record, sid + "~query1", alt.record(sid + "~query1", at))
    return alt


def _save(folder, sid, doc):
    p = os.path.join(folder, sid + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def summarise(rows):
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] += 1
    out["failed"] = sum(out[s] for s in FAILED)
    return out


# ---------------------------------------------------------------- history: trader/doctor.json

def load_history(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("sources"), dict):
            return doc
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "sources": {}, "runs": []}


def apply_history(report, hist):
    """Fill each row's since / last_ok from the history and return the updated history."""
    at = report["checked_at"]
    srcs = hist.setdefault("sources", {})
    for row in report["sources"]:
        prev = srcs.get(row["id"]) or {}
        if row["status"] == "skipped":
            row["since"], row["last_ok"] = prev.get("since"), prev.get("last_ok")
            continue
        same = prev.get("status") == row["status"]
        since = prev.get("since") if same and prev.get("since") else at
        good = row["status"] in ("ok", "slow")
        last_ok = at if good else prev.get("last_ok")
        row["since"], row["last_ok"] = since, last_ok
        srcs[row["id"]] = {"status": row["status"], "since": since, "last_ok": last_ok, "last_checked": at,
                           "detail": row["detail"]}
    runs = hist.setdefault("runs", [])
    runs.append({"at": at, "offline": report["offline"], "only": report["only"], "summary": report["summary"]})
    del runs[:-KEEP_RUNS]
    hist["schema"] = SCHEMA
    hist["updated"] = at
    keep = ("id", "group", "name", "status", "detail", "since", "last_ok", "affects", "fallback", "fallback_used")
    hist["last"] = {"checked_at": at, "now_ist": report["now_ist"], "market": report["market"],
                    "offline": report["offline"], "only": report["only"], "summary": report["summary"],
                    "sources": [{k: r.get(k) for k in keep if k in r} for r in report["sources"]]}
    return hist


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- printing

def short_time(iso):
    """'2026-11-01T10:41:00+05:30' -> '2026-11-01 10:41'."""
    return str(iso or "")[:16].replace("T", " ") if iso else "-"


def render(report):
    now = report["now_ist"]
    d = datetime.date.fromisoformat(report["last_session"])
    lines = ["Data self-test, %s %s IST. %s; fresh data is from %s or later.%s" % (
        datetime.date.fromisoformat(now[:10]).strftime("%a"), now, report["market"], day_label(d),
        " Recorded answers (offline)." if report["offline"] else "")]
    cols = [("source", 34), ("status", 12), ("since", 16), ("affects", 44), ("fallback", 0)]
    lines.append("  ".join(n.ljust(w) if w else n for n, w in cols).rstrip())
    for r in report["sources"]:
        st = r["status"] + ((" %.1fs" % r["seconds"]) if r["seconds"] is not None and r["status"] in ("ok", "slow")
                            else "")
        fb = "-" if r["status"] in ("ok", "slow", "skipped") else r.get("fallback_used") or r["fallback"]
        cells = [r["name"], st, short_time(r.get("since")), r["affects"], fb]
        lines.append("  ".join(c.ljust(w) if w else c for c, (_, w) in zip(cells, cols)).rstrip())
        if r["status"] not in ("ok", "slow"):
            extra = r["detail"]
            if r["status"] in FAILED:
                extra += "; last ok %s" % short_time(r.get("last_ok")) if r.get("last_ok") else "; never ok here"
            lines.append("    " + extra)
    s = report["summary"]
    lines.append("%d ok, %d slow, %d changed, %d down, %d blocked%s." % (
        s["ok"], s["slow"], s["changed"], s["down"], s["blocked"],
        (", %d skipped" % s["skipped"]) if s["skipped"] else ""))
    if s["failed"]:
        lines.append("If it lasts, open a data source issue with this table "
                     "(https://github.com/rajdavee/bharat-trader/issues/new?template=data_source.yml).")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- command line

def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="doctor.py", description="Self-test every data source the tools read.")
    ap.add_argument("--only", choices=("nse", "yahoo"), help="probe only one group")
    ap.add_argument("--json", action="store_true", help="print one JSON document")
    ap.add_argument("--offline", metavar="DIR", action="append",
                    help="replay recorded answers from DIR (repeat it: a later folder wins)")
    ap.add_argument("--record", metavar="DIR", help="save each live answer to DIR")
    ap.add_argument("--now", help='judge freshness at this IST time, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--history", default=os.path.join("trader", "doctor.json"),
                    help="the history file (default trader/doctor.json)")
    ap.add_argument("--no-write", action="store_true", help="read the history but do not update it")
    a = ap.parse_args(argv)
    try:
        if a.offline and a.record:
            raise DoctorError("--record saves live answers; it does not go with --offline")
        now = parse_now(a.now)
        log = None if a.json else (lambda m: print(m, file=sys.stderr))
        if log and not a.offline:
            log("Probing %s, one request each..." % ("NSE" if a.only == "nse" else "Yahoo" if a.only == "yahoo"
                                                    else "NSE and Yahoo"))
        report = run(a.only, a.offline, a.record, now, os.path.dirname(os.path.abspath(a.history)), log)
    except DoctorError as e:
        print("doctor: %s" % e, file=sys.stderr)
        return 2
    hist = apply_history(report, load_history(a.history))
    if not a.no_write:
        write_json(a.history, hist)
    report["history"] = None if a.no_write else a.history
    code = 1 if report["summary"]["failed"] else 0
    report["exit_code"] = code
    if a.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(report))
        if not a.no_write:
            print("History: %s." % a.history)
    return code


if __name__ == "__main__":
    sys.exit(main())
