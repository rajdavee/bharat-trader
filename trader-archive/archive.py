#!/usr/bin/env python3
"""trader-archive: a local, append-only NSE market data archive, read offline by the other skills.

    python3 archive.py backfill --years 3                         # EQ + F&O bhavcopies, indices, VIX, ...
    python3 archive.py backfill --from 2026-10-01 --datasets eq,fo,vix
    python3 archive.py update                                     # from the last archived day to today
    python3 archive.py status                                     # what is stored, how big, how fresh
    python3 archive.py verify                                     # checksums, gaps, duplicates, outliers
    python3 archive.py export --symbol RELIANCE --from 2024-01-01 --adjusted
    python3 archive.py intraday --symbols RELIANCE,TCS --interval 1m --from 2026-09-01   # Upstox candles
    python3 archive.py snapshot-chain --symbols NIFTY --every 15  # option chains during market hours

Everything lands under <workdir>/archive/ (default trader/archive, or $BT_ARCHIVE): one gzip CSV per
dataset per trading day (archive/eq/2026/2026-10-30.csv.gz), and a manifest.json per dataset per
year with the source URL, fetched_at (IST), row count, size and SHA-256 of every file. A run that is
stopped (Ctrl-C, a dropped line, NSE blocking) loses nothing: the next run skips what the manifest
already holds and carries on.

Datasets (all public, all from NSE except intraday):
  eq      the cash-market bhavcopy: UDiFF from 2024-01-01, the legacy cm...bhav.csv before
          (the legacy file stops on 2024-07-05); both are normalized to one schema
  fo      the F&O bhavcopy: options and futures with settle price, OI in shares, volume in contracts
  index   every NSE index's daily OHLC (ind_close_all), India VIX included
  vix     India VIX history from NSE's API (70 rows a call, so it is fetched a quarter at a time)
  lots    today's F&O lot sizes per expiry month (fo_mktlots; NSE keeps no history, so the archive
          keeps one snapshot a day; the F&O bhavcopy carries each contract's lot from 2024)
  ban     the F&O ban list for each trading day
  settle  the daily MTM settlement price of every future
  ca      corporate actions; splits and bonuses become the price adjustment factors

Politeness: a browser User-Agent and NSE's Referer, --pause seconds between NSE calls (default 1.5,
never below 1.0), a growing wait after HTTP 401/403/429, and a stop after 3 blocks in a row with
everything fetched so far kept. Weekends and NSE holidays are skipped without a call; a weekday with
no bhavcopy (an older holiday) is remembered in archive/calendar.json and never asked again.

The read API is pure and offline (no network, ever): load_daily, load_daily_many, load_intraday,
load_fo_chain_eod, load_vix, load_index, load_ban, trading_days, adjustment_factors. trader-lab and
trader-timemachine use it when an archive is present and fall back to Yahoo when not.

Standard library only. This is data infrastructure: it never places an order.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SCHEMA = "bharat-trader/archive/1"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130 Safari/537.36")
ARCHIVES = "https://nsearchives.nseindia.com"
WWW = "https://www.nseindia.com"
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"
PAUSE = 1.5              # seconds between NSE calls (the default; --pause can only make it longer than MIN_PAUSE)
MIN_PAUSE = 1.0
BLOCK_WAIT = 20.0        # seconds after the first 401/403/429, then 2x, then stop
MAX_BLOCKS = 3           # blocks in a row before the run stops (progress is kept)
RETRIES = 2              # for timeouts and 5xx
TIMEOUT = 40
BROKER_PAUSE = 0.5       # seconds between broker history calls

UDIFF_FROM = datetime.date(2024, 1, 1)     # first UDiFF bhavcopy: 2024-01-02 (verified; 2023-12-29 is 404)
LEGACY_EQ_FROM = datetime.date(1994, 11, 3)   # the oldest legacy cm...bhav.csv NSE serves (verified 2026-11-01)
LEGACY_FO_FROM = datetime.date(2000, 6, 12)   # the first F&O day (NIFTY futures only), verified
INDEX_FILES_FROM = datetime.date(2012, 2, 21)  # ind_close_all starts here; before it: indicesHistory (indexhist)
VIX_FROM = datetime.date(2010, 7, 19)      # NSE's VIX history API returns nothing earlier (verified)
LEGACY_UNTIL = datetime.date(2024, 7, 5)   # last legacy cm/fo...bhav.csv (2024-07-08 is 404)
UPSTOX_MINUTES_FROM = datetime.date(2022, 1, 1)   # Upstox minute candles start in January 2022
NOT_PUBLISHED_DAYS = 3   # a 404 this recent may just be "not published yet": ask again next time

DATASETS = ("eq", "fo", "index", "vix", "lots", "ban", "settle", "ca", "indexhist", "results", "members")
DAILY = ("eq", "fo", "index", "ban", "settle")
DEFAULT_DATASETS = ("eq", "fo", "index", "vix", "lots", "ban", "settle", "ca")
LONG_DATASETS = ("indexhist", "results", "members")     # opt in: --datasets all, or name them
SERIES_PREF = ("EQ", "BE", "BZ", "SM", "ST")  # a stock that moves to trade-for-trade keeps its history
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
INTERVALS = {"1m": 1, "5m": 5}
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
INDEX_NAMES = {
    "^NSEI": "Nifty 50", "NIFTY": "Nifty 50", "NIFTY50": "Nifty 50",
    "^NSEBANK": "Nifty Bank", "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Financial Services", "^CNXFIN": "Nifty Financial Services",
    "MIDCPNIFTY": "Nifty Midcap Select", "NIFTYNXT50": "Nifty Next 50", "^NSMIDCP": "Nifty Next 50",
    "^INDIAVIX": "India VIX", "INDIAVIX": "India VIX", "VIX": "India VIX",
}
UPSTOX_INDEX_KEYS = {"Nifty 50": "NSE_INDEX|Nifty 50", "Nifty Bank": "NSE_INDEX|Nifty Bank",
                     "Nifty Financial Services": "NSE_INDEX|Nifty Fin Service",
                     "Nifty Midcap Select": "NSE_INDEX|NIFTY MID SELECT", "Nifty Next 50": "NSE_INDEX|Nifty Next 50",
                     "India VIX": "NSE_INDEX|India VIX"}

# NSE Capital Market trading holidays, from NSE's holiday master. 2027 is not published yet. Older years
# need no table: a weekday with no bhavcopy is learned as closed (archive/calendar.json).
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
# Sessions outside Monday to Friday, or on a holiday, that have a bhavcopy (each one verified on NSE's
# archive). Short ones (Muhurat, disaster-recovery drills) are flagged in the quality report.
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

EQ_COLS = ["symbol", "series", "isin", "open", "high", "low", "close", "last", "prev_close", "volume",
           "turnover", "trades"]
FO_COLS = ["symbol", "kind", "expiry", "strike", "option_type", "open", "high", "low", "close", "settle",
           "underlying_price", "oi", "oi_change", "volume", "turnover", "lot"]
INDEX_COLS = ["index", "open", "high", "low", "close", "change_pct", "volume", "turnover_cr", "pe", "pb",
              "div_yield"]
BAN_COLS = ["symbol"]
SETTLE_COLS = ["instrument", "underlying", "expiry", "settle"]
VIX_COLS = ["date", "open", "high", "low", "close", "prev_close"]
LOTS_COLS = ["underlying", "symbol", "month", "lot"]
BAR_COLS = ["date", "time", "open", "high", "low", "close", "volume", "oi", "source"]
COLS = {"eq": EQ_COLS, "fo": FO_COLS, "index": INDEX_COLS, "ban": BAN_COLS, "settle": SETTLE_COLS,
        "lots": LOTS_COLS}
KEYS = {"eq": (0, 1), "fo": (0, 1, 2, 3, 4), "index": (0,), "ban": (0,), "settle": (0, 1, 2), "lots": (1, 2)}
LEGACY_FO_KIND = {"OPTIDX": "IDO", "FUTIDX": "IDF", "OPTSTK": "STO", "FUTSTK": "STF"}
OUTLIER_PCT = 20.0       # a day-on-day close move bigger than this, with no corporate action, is flagged
LAB_HEAD = "date,time,open,high,low,close,volume"


class ArchiveError(Exception):
    pass


class Blocked(ArchiveError):
    """NSE refused us MAX_BLOCKS times in a row: stop, keep what we have."""


# ---------------------------------------------------------------- time and the calendar

def ist_now():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def parse_day(text, what="date"):
    if isinstance(text, datetime.date):
        return text
    try:
        return datetime.date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        raise ArchiveError("%s wants YYYY-MM-DD, not %r" % (what, text))


def iso(d):
    return d.isoformat() if isinstance(d, datetime.date) else str(d)[:10]


def ddmmyyyy(d):
    return d.strftime("%d%m%Y")


def dmy_dash(d):
    return d.strftime("%d-%m-%Y")


def nse_date(text):
    """'25-Jan-2023', '03-JAN-2023', '03-01-2023', '2026-10-27', '23-OCT-2026' -> ISO, or ''."""
    s = (text or "").strip()
    if not s or s == "-":
        return ""
    if re.match(r"^\d{4}-\d\d-\d\d", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3,9})-(\d{4})$", s)      # 06-Jun-2012, 06-June-2012, 2-JAN-1995
    if m and m.group(2)[:3].upper() in MONTHS:
        return "%s-%02d-%02d" % (m.group(3), MONTHS.index(m.group(2)[:3].upper()) + 1, int(m.group(1)))
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1)))
    raise ArchiveError("not an NSE date: %r" % text)


def session_kind(d, closed=None, holidays=None):
    """'session', 'weekend', 'holiday' or 'closed' (learned) for one day."""
    k = iso(d)
    if k in EXTRA_SESSIONS:
        return "session"
    if k in (holidays if holidays is not None else NSE_HOLIDAYS):
        return "holiday"
    if closed and k in closed:
        return "closed"
    if parse_day(k).weekday() >= 5:
        return "weekend"
    return "session"


def candidate_days(start, end, closed=None, holidays=None):
    """Days that may have a session, oldest first: weekdays that are not a known or learned holiday,
    plus the special weekend sessions."""
    d, out = parse_day(start), []
    end = parse_day(end)
    while d <= end:
        if session_kind(d, closed, holidays) == "session":
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- HTTP, politely

class Http(object):
    """GET with a pause between calls, retries for timeouts and 5xx, and a stop after repeated blocks.
    opener(req, timeout) and sleep are injectable for tests. get() -> (status, bytes)."""

    def __init__(self, pause=PAUSE, sleep=None, opener=None, block_wait=BLOCK_WAIT, max_blocks=MAX_BLOCKS,
                 clock=None):
        self.pause = max(float(pause), MIN_PAUSE)
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.opener = opener or urllib.request.urlopen
        self.block_wait = block_wait
        self.max_blocks = max_blocks
        self.blocks = 0
        self.calls = 0
        self.bytes = 0
        self.last = None

    def _wait(self, pause):
        if self.last is not None:
            gap = pause - (self.clock() - self.last)
            if gap > 0:
                self.sleep(gap)
        self.last = self.clock()

    def get(self, url, accept=None, pause=None, headers=None):
        nse = "nseindia.com" in url
        h = {"User-Agent": UA, "Accept-Encoding": "gzip",
             "Accept": accept or ("application/json" if "/api/" in url else "*/*")}
        if nse:
            h["Referer"] = WWW + "/"
        h.update(headers or {})
        tries = 0
        while True:
            self._wait(self.pause if pause is None else pause)
            self.calls += 1
            try:
                with self.opener(urllib.request.Request(url, headers=h), timeout=TIMEOUT) as r:
                    data = r.read()
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or \
                            (data[:2] == b"\x1f\x8b" and not url.endswith((".gz", ".zip"))):
                        data = gzip.decompress(data)
                    self.blocks = 0
                    self.bytes += len(data)
                    return getattr(r, "status", 200) or 200, data
            except urllib.error.HTTPError as e:
                if e.code == 404 or e.code == 410:
                    self.blocks = 0
                    return e.code, b""
                if e.code in (401, 403, 429):
                    self.blocks += 1
                    if self.blocks >= self.max_blocks:
                        raise Blocked("%s answered HTTP %d %d times in a row; stopping politely. Everything "
                                      "fetched so far is kept: run the same command later to carry on."
                                      % (urllib.parse.urlparse(url).netloc, e.code, self.blocks))
                    self.sleep(self.block_wait * self.blocks)
                    continue
                if e.code >= 500 and tries < RETRIES:
                    tries += 1
                    self.sleep(5.0 * tries)
                    continue
                return e.code, b""
            except (urllib.error.URLError, OSError) as e:
                if tries < RETRIES:
                    tries += 1
                    self.sleep(5.0 * tries)
                    continue
                return 0, str(getattr(e, "reason", None) or e).encode("utf-8", "replace")


# ---------------------------------------------------------------- source URLs

def eq_url(d, fmt=None):
    fmt = fmt or ("udiff" if d >= UDIFF_FROM else "legacy")
    if fmt == "udiff":
        return "%s/content/cm/BhavCopy_NSE_CM_0_0_0_%s_F_0000.csv.zip" % (ARCHIVES, d.strftime("%Y%m%d"))
    mon = MONTHS[d.month - 1]
    return "%s/content/historical/EQUITIES/%d/%s/cm%02d%s%dbhav.csv.zip" % (ARCHIVES, d.year, mon, d.day, mon, d.year)


def fo_url(d, fmt=None):
    fmt = fmt or ("udiff" if d >= UDIFF_FROM else "legacy")
    if fmt == "udiff":
        return "%s/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % (ARCHIVES, d.strftime("%Y%m%d"))
    mon = MONTHS[d.month - 1]
    return "%s/content/historical/DERIVATIVES/%d/%s/fo%02d%s%dbhav.csv.zip" % (ARCHIVES, d.year, mon, d.day, mon,
                                                                              d.year)


def daily_urls(ds, d):
    """[(url, format)] to try in order: the format for the date first, the other while both exist."""
    if ds in ("eq", "fo"):
        fn = eq_url if ds == "eq" else fo_url
        first = "udiff" if d >= UDIFF_FROM else "legacy"
        out = [(fn(d, first), first)]
        if UDIFF_FROM <= d <= LEGACY_UNTIL:
            out.append((fn(d, "legacy"), "legacy"))
        return out
    if ds == "index":
        return [("%s/content/indices/ind_close_all_%s.csv" % (ARCHIVES, ddmmyyyy(d)), "csv")]
    if ds == "ban":
        return [("%s/archives/fo/sec_ban/fo_secban_%s.csv" % (ARCHIVES, ddmmyyyy(d)), "csv")]
    if ds == "settle":
        return [("%s/archives/nsccl/sett/FOSett_prce_%s.csv" % (ARCHIVES, ddmmyyyy(d)), "csv")]
    raise ArchiveError("not a daily dataset: %s" % ds)


def vix_url(a, b):
    return "%s/api/historicalOR/vixhistory?from=%s&to=%s" % (WWW, dmy_dash(a), dmy_dash(b))


def ca_url(a, b):
    return "%s/api/corporates-corporateActions?index=equities&from_date=%s&to_date=%s" % (WWW, dmy_dash(a),
                                                                                        dmy_dash(b))


LOTS_URL = ARCHIVES + "/content/fo/fo_mktlots.csv"


# ---------------------------------------------------------------- parsing (each source -> one schema)

def is_html(data):
    return data[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))


def unzip_csv(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ArchiveError("not a zip file (NSE may have sent a web page)")
    names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise ArchiveError("the zip holds no CSV")
    return z.read(names[0]).decode("utf-8-sig", "replace")


def _f(x):
    """A price or count as text, '' when missing; trailing zeros trimmed."""
    s = (x or "").strip()
    if s in ("", "-", "NA", "null"):
        return ""
    try:
        v = float(s)
    except ValueError:
        return ""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return ("%.4f" % v).rstrip("0").rstrip(".")


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def _dicts(text):
    rows = _rows(text)
    if not rows:
        return [], []
    head = [h.strip() for h in rows[0]]
    return head, [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if any(c.strip() for c in r)]


def _check_day(found, d, what):
    if found and found != iso(d):
        raise ArchiveError("the %s file is for %s, not %s" % (what, found, iso(d)))


def parse_eq(text, d):
    """Either bhavcopy format -> (rows in EQ_COLS order, sorted, 'udiff'|'legacy')."""
    head, recs = _dicts(text)
    out = []
    if "TradDt" in head:
        fmt = "udiff"
        for r in recs:
            _check_day(nse_date(r.get("TradDt")), d, "equity bhavcopy")
            out.append([r["TckrSymb"], r["SctySrs"], r.get("ISIN", ""), _f(r["OpnPric"]), _f(r["HghPric"]),
                        _f(r["LwPric"]), _f(r["ClsPric"]), _f(r.get("LastPric")), _f(r.get("PrvsClsgPric")),
                        _f(r.get("TtlTradgVol")), _f(r.get("TtlTrfVal")), _f(r.get("TtlNbOfTxsExctd"))])
    elif "SYMBOL" in head and "SERIES" in head:
        fmt = "legacy"
        for r in recs:
            _check_day(nse_date(r.get("TIMESTAMP")), d, "equity bhavcopy")
            out.append([r["SYMBOL"], r["SERIES"], r.get("ISIN", ""), _f(r["OPEN"]), _f(r["HIGH"]), _f(r["LOW"]),
                        _f(r["CLOSE"]), _f(r.get("LAST")), _f(r.get("PREVCLOSE")), _f(r.get("TOTTRDQTY")),
                        _f(r.get("TOTTRDVAL")), _f(r.get("TOTALTRADES"))])
    else:
        raise ArchiveError("the equity bhavcopy header is not one we know: %s" % ",".join(head[:6]))
    out.sort(key=lambda r: (r[0], r[1]))
    return out, fmt


def parse_fo(text, d):
    """Either F&O bhavcopy format -> (rows in FO_COLS order, sorted, format). OI in shares, volume in
    contracts, turnover in rupees. Legacy files carry no underlying price and no lot size."""
    head, recs = _dicts(text)
    out = []
    if "TradDt" in head:
        fmt = "udiff"
        for r in recs:
            _check_day(nse_date(r.get("TradDt")), d, "F&O bhavcopy")
            kind = r["FinInstrmTp"]
            out.append([r["TckrSymb"], kind, nse_date(r.get("XpryDt")),
                        _f(r.get("StrkPric")) if kind in ("IDO", "STO") else "",
                        r.get("OptnTp", "") if kind in ("IDO", "STO") else "",
                        _f(r["OpnPric"]), _f(r["HghPric"]), _f(r["LwPric"]), _f(r["ClsPric"]), _f(r.get("SttlmPric")),
                        _f(r.get("UndrlygPric")), _f(r.get("OpnIntrst")), _f(r.get("ChngInOpnIntrst")),
                        _f(r.get("TtlTradgVol")), _f(r.get("TtlTrfVal")), _f(r.get("NewBrdLotQty"))])
    elif "INSTRUMENT" in head and "EXPIRY_DT" in head:
        fmt = "legacy"
        for r in recs:
            _check_day(nse_date(r.get("TIMESTAMP")), d, "F&O bhavcopy")
            kind = LEGACY_FO_KIND.get(r["INSTRUMENT"], r["INSTRUMENT"])
            opt = kind in ("IDO", "STO")
            lakh = r.get("VAL_INLAKH", "")
            turnover = _f(str(float(lakh) * 100000)) if _f(lakh) else ""
            out.append([r["SYMBOL"], kind, nse_date(r.get("EXPIRY_DT")), _f(r.get("STRIKE_PR")) if opt else "",
                        r.get("OPTION_TYP", r.get("OPTIONTYPE", "")) if opt else "", _f(r["OPEN"]), _f(r["HIGH"]), _f(r["LOW"]),
                        _f(r["CLOSE"]), _f(r.get("SETTLE_PR")), "", _f(r.get("OPEN_INT")), _f(r.get("CHG_IN_OI")),
                        _f(r.get("CONTRACTS")), turnover, ""])
    else:
        raise ArchiveError("the F&O bhavcopy header is not one we know: %s" % ",".join(head[:6]))
    out.sort(key=lambda r: (r[0], r[1], r[2], r[4], float(r[3] or 0)))
    return out, fmt


def parse_index(text, d):
    head, recs = _dicts(text)
    if "Index Name" not in head:
        raise ArchiveError("the index file header is not one we know: %s" % ",".join(head[:4]))
    out = []
    for r in recs:
        _check_day(nse_date(r.get("Index Date")), d, "index")
        out.append([r["Index Name"], _f(r.get("Open Index Value")), _f(r.get("High Index Value")),
                    _f(r.get("Low Index Value")), _f(r.get("Closing Index Value")), _f(r.get("Change(%)")),
                    _f(r.get("Volume")), _f(r.get("Turnover (Rs. Cr.)")), _f(r.get("P/E")), _f(r.get("P/B")),
                    _f(r.get("Div Yield"))])
    out.sort(key=lambda r: r[0].lower())
    return out, "csv"


def parse_ban(text, d=None):
    """'Securities in Ban For Trade Date 23-OCT-2026:' then '1,SAIL' lines (or ': NIL')."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or "ban" not in lines[0].lower():
        raise ArchiveError("the ban list does not start with its title line")
    m = re.search(r"(\d{1,2}-[A-Za-z]{3,9}-\d{4})", lines[0])
    if m and d is not None:
        _check_day(nse_date(m.group(1)), d, "ban list")
    out = []
    for l in lines[1:]:
        p = [c.strip() for c in l.split(",")]
        if len(p) >= 2 and p[1]:
            out.append([p[1]])
    return sorted(out), "csv"


def parse_settle(text, d=None):
    head, recs = _dicts(text)
    if "MTM SETTLEMENT PRICE" not in head:
        raise ArchiveError("the settlement price file header is not one we know")
    out = []
    for r in recs:
        if d is not None:
            _check_day(nse_date(r.get("DATE")), d, "settlement price")
        out.append([r["INSTRUMENT"], r["UNDERLYING"], nse_date(r["EXPIRY DATE"]), _f(r["MTM SETTLEMENT PRICE"])])
    out.sort()
    return out, "csv"


def parse_vix(text):
    """vixhistory JSON -> [[date, open, high, low, close, prev_close]] oldest first."""
    try:
        doc = json.loads(text)
        data = doc["data"] if isinstance(doc, dict) else doc
    except (ValueError, KeyError, TypeError):
        raise ArchiveError("the VIX history is not the JSON we know")
    out = {}
    for r in data or []:
        day = nse_date(r.get("EOD_TIMESTAMP"))
        out[day] = [day] + [_f(str(r.get(k, ""))) for k in ("EOD_OPEN_INDEX_VAL", "EOD_HIGH_INDEX_VAL",
                                                           "EOD_LOW_INDEX_VAL", "EOD_CLOSE_INDEX_VAL",
                                                           "EOD_PREV_CLOSE")]
    return [out[k] for k in sorted(out)]


def parse_lots(text):
    """fo_mktlots.csv (padded cells, two header rows) -> [[underlying, symbol, YYYY-MM, lot]]."""
    out, months = [], []
    for row in _rows(text):
        cells = [c.strip() for c in row]
        if len(cells) < 3:
            continue
        if cells[1].upper() == "SYMBOL":
            months = []
            for c in cells[2:]:
                m = re.match(r"^([A-Za-z]{3})-(\d\d)$", c)
                months.append("20%s-%02d" % (m.group(2), MONTHS.index(m.group(1).upper()) + 1) if m else "")
            continue
        for month, lot in zip(months, cells[2:]):
            if month and _f(lot):
                out.append([cells[0], cells[1], month, _f(lot)])
    if not out:
        raise ArchiveError("no lot sizes in fo_mktlots.csv")
    return sorted(out, key=lambda r: (r[1], r[2])), "csv"


_BONUS = re.compile(r"\bbonus\b[^0-9:/]{0,30}?(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)   # "Bonus 1:1",
# "Bonus - 3:1", "Bonus Issue 1 : 1", "Bonus Shares In The Ratio Of 1:1"
_FV_TO = re.compile(r"(?:rs|re|inr)\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*(?:each\s*)?to\s+(?:rs|re|inr)\.?\s*"
                    r"(\d+(?:\.\d+)?)", re.I)      # older subjects: "Fv Split Rs.10 To Rs.2"
_FV = re.compile(r"from\s+(?:rs|re|inr)?\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*(?:per(?:\s+share)?)?\s*to\s+(?:rs|re|inr)?\.?\s*"
                 r"(\d+(?:\.\d+)?)", re.I)


def ca_factor(subject):
    """A corporate-action subject -> (kind, price factor) or None. The factor multiplies prices BEFORE the
    ex-date: bonus a:b (a new for b held) -> b / (a + b); split from F1 to F2 -> F2 / F1; consolidation
    likewise (> 1). Bonus preference shares and debentures (NCRPS) do not change the share count."""
    s = " ".join((subject or "").split())
    low = s.lower()
    if any(w in low for w in ("ncrps", "preference", "debenture", "warrant", " dvr", " deb")):
        return None
    got = []
    m = _BONUS.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            got.append(("bonus", b / (a + b)))
    if "split" in low or "sub-division" in low or "subdivision" in low or "consolidation" in low:
        m = _FV.search(s) or _FV_TO.search(s)
        if m:
            f1, f2 = float(m.group(1)), float(m.group(2))
            if f1 > 0 and f2 > 0 and f1 != f2:
                got.append(("split" if f2 < f1 else "consolidation", f2 / f1))
    if not got:
        return None
    if len(got) == 1:
        return got[0]
    return "+".join(k for k, _ in got), got[0][1] * got[1][1]     # one subject, both on one ex-date


def ca_events(raw):
    """NSE corporate actions JSON (a list) -> split/bonus events, oldest first, duplicates dropped."""
    seen, out = set(), []
    for r in raw or []:
        got = ca_factor(r.get("subject"))
        if not got:
            continue
        try:
            ex = nse_date(r.get("exDate"))
        except ArchiveError:
            continue
        key = (r.get("symbol"), ex, got[0], round(got[1], 8))
        if not ex or key in seen:
            continue
        seen.add(key)
        out.append({"symbol": r.get("symbol"), "ex_date": ex, "kind": got[0], "factor": got[1],
                    "subject": " ".join(str(r.get("subject") or "").split()), "series": r.get("series")})
    out.sort(key=lambda e: (e["symbol"] or "", e["ex_date"]))
    return out


# ---------------------------------------------------------------- the store

def default_root(root=None, workdir=None):
    if root:
        return root
    if workdir:
        return os.path.join(workdir, "archive")
    return os.environ.get("BT_ARCHIVE") or os.path.join("trader", "archive")


def gz_bytes(text):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:     # mtime 0: the same bytes every time
        g.write(text.encode("utf-8"))
    return buf.getvalue()


def csv_text(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    atomic_write(path, (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def read_gz_csv(path):
    """(header, rows) of a .csv.gz file."""
    with open(path, "rb") as fh:
        text = gzip.decompress(fh.read()).decode("utf-8")
    rows = _rows(text)
    return (rows[0] if rows else []), rows[1:]


class Store(object):
    """archive/<dataset>/<YYYY>/<YYYY-MM-DD>.csv.gz plus archive/<dataset>/<YYYY>/manifest.json."""

    def __init__(self, root):
        self.root = root
        self._man = {}

    # paths
    def path(self, ds, d, ext=".csv.gz"):
        k = iso(d)
        return os.path.join(self.root, ds, k[:4], k + ext)

    def man_path(self, ds, year):
        return os.path.join(self.root, ds, str(year), "manifest.json")

    def manifest(self, ds, year):
        key = (ds, str(year))
        if key not in self._man:
            doc = read_json(self.man_path(ds, year), None) or {}
            doc.setdefault("schema", SCHEMA)
            doc.setdefault("dataset", ds)
            doc.setdefault("files", {})
            doc.setdefault("missing", {})
            self._man[key] = doc
        return self._man[key]

    def save_manifest(self, ds, year):
        write_json(self.man_path(ds, year), self.manifest(ds, year))

    def entry(self, ds, d):
        k = iso(d)
        return self.manifest(ds, k[:4])["files"].get(k)

    def has(self, ds, d, ext=".csv.gz"):
        e = self.entry(ds, d)
        p = self.path(ds, d, ext)
        return bool(e) and os.path.exists(p) and os.path.getsize(p) == e.get("bytes")

    def put(self, ds, d, data, meta, ext=".csv.gz"):
        """Write the file, then its manifest entry (a crash between the two only means a refetch)."""
        k = iso(d)
        atomic_write(self.path(ds, d, ext), data)
        man = self.manifest(ds, k[:4])
        man["files"][k] = dict(meta, file=os.path.basename(self.path(ds, d, ext)), bytes=len(data),
                               sha256=sha256(data))
        man["missing"].pop(k, None)
        self.save_manifest(ds, k[:4])

    def put_rows(self, ds, d, header, rows, meta):
        self.put(ds, d, gz_bytes(csv_text(header, rows)), dict(meta, rows=len(rows)))

    def mark_missing(self, ds, d, status, url, at):
        k = iso(d)
        man = self.manifest(ds, k[:4])
        man["missing"][k] = {"status": status, "url": url, "checked_at": at}
        self.save_manifest(ds, k[:4])

    def missing(self, ds, d):
        k = iso(d)
        return self.manifest(ds, k[:4])["missing"].get(k)

    # the calendar NSE taught us
    def calendar(self):
        doc = read_json(os.path.join(self.root, "calendar.json"), None) or {}
        doc.setdefault("schema", SCHEMA)
        doc.setdefault("closed", {})
        return doc

    def mark_closed(self, d, why):
        doc = self.calendar()
        doc["closed"][iso(d)] = why
        write_json(os.path.join(self.root, "calendar.json"), doc)

    # what is on disk
    def years(self, ds):
        base = os.path.join(self.root, ds)
        try:
            return sorted(y for y in os.listdir(base) if re.match(r"^\d{4}$", y))
        except OSError:
            return []

    def dates(self, ds):
        out = []
        for y in self.years(ds):
            for f in os.listdir(os.path.join(self.root, ds, y)):
                m = re.match(r"^(\d{4}-\d\d-\d\d)\.(csv|json)\.gz$", f)
                if m:
                    out.append(m.group(1))
        return sorted(out)


# ---------------------------------------------------------------- backfill and update

FIRST_DAY = {"eq": LEGACY_EQ_FROM, "fo": LEGACY_FO_FROM, "index": INDEX_FILES_FROM}
PARSERS = {"eq": parse_eq, "fo": parse_fo, "index": parse_index, "ban": parse_ban, "settle": parse_settle}


def _decode(ds, data):
    if ds in ("eq", "fo"):
        return unzip_csv(data)
    return data.decode("utf-8-sig", "replace")


def fetch_daily(store, http, ds, d, now):
    """One dataset for one day -> 'ok', 'skip', 'before' (NSE has no such file that early), 'none' (404),
    'closed' or 'error'."""
    if store.has(ds, d):
        return "skip", None
    if d < FIRST_DAY.get(ds, d):
        return "before", None
    old = (now.date() - d).days > NOT_PUBLISHED_DAYS
    miss = store.missing(ds, d)
    if miss and miss.get("status") in (404, 410) and \
            (parse_day(miss.get("checked_at", "0001-01-01")[:10]) - d).days > NOT_PUBLISHED_DAYS:
        return "none", None          # NSE said "no such file" well after the day: do not ask again
    last_status, last_url = None, None
    for url, fmt in daily_urls(ds, d):
        status, data = http.get(url)
        last_status, last_url = status, url
        if status == 200 and data and not is_html(data):
            try:
                rows, fmt = PARSERS[ds](_decode(ds, data), d)
            except (ArchiveError, KeyError, ValueError, UnicodeDecodeError) as e:
                return "error", "%s %s: %s" % (ds, iso(d), e)
            store.put_rows(ds, d, COLS[ds], rows, {"source_url": url, "format": fmt, "source_bytes": len(data),
                                                    "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")})
            return "ok", len(rows)
        if status == 200:
            last_status = "html"
        if status not in (404, 410):
            break
    if last_status in (404, 410):
        if old:
            store.mark_missing(ds, d, last_status, last_url, now.strftime("%Y-%m-%dT%H:%M:%S+05:30"))
            if ds == "eq" or (ds == "fo" and not store.has("eq", d)):
                store.mark_closed(d, "NSE has no %s bhavcopy for this day (a holiday)" % ("equity" if ds == "eq"
                                                                                        else "F&O"))
                return "closed", None
        return "none", None
    return "error", "%s %s: HTTP %s from %s" % (ds, iso(d), last_status, last_url)


def quarters(start, end):
    """[(a, b)] calendar quarters clipped to start..end (the VIX API returns at most 70 rows a call)."""
    out, d = [], start
    while d <= end:
        q_end_month = ((d.month - 1) // 3) * 3 + 3
        nxt = datetime.date(d.year + (q_end_month == 12), (q_end_month % 12) + 1, 1)
        b = min(end, nxt - datetime.timedelta(days=1))
        out.append((d, b))
        d = nxt
    return out


def fetch_vix(store, http, start, end, now, log):
    got = 0
    if end < VIX_FROM:
        return 0
    for a, b in quarters(max(start, VIX_FROM), end):
        year = str(a.year)
        man = store.manifest("vix", year)
        key = "%s..%s" % (iso(a), iso(b))
        done = man.setdefault("chunks", {}).get(key)
        if done and done.get("complete") and os.path.exists(os.path.join(store.root, "vix", year + ".csv.gz")):
            continue
        url = vix_url(a, b)
        status, data = http.get(url)
        if status != 200 or is_html(data):
            log("VIX %s: HTTP %s, left for the next run" % (key, status))
            continue
        try:
            rows = [r for r in parse_vix(data.decode("utf-8", "replace")) if iso(a) <= r[0] <= iso(b)]
        except ArchiveError as e:
            log("VIX %s: %s" % (key, e))
            continue
        path = os.path.join(store.root, "vix", year + ".csv.gz")
        have = {}
        if os.path.exists(path):
            have = {r[0]: r for r in read_gz_csv(path)[1]}
        for r in rows:
            have[r[0]] = r
        merged = [have[k] for k in sorted(have)]
        blob = gz_bytes(csv_text(VIX_COLS, merged))
        atomic_write(path, blob)
        complete = (now.date() - b).days > NOT_PUBLISHED_DAYS
        man["chunks"][key] = {"source_url": url, "rows": len(rows), "complete": complete,
                              "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")}
        man["files"][year] = {"file": year + ".csv.gz", "rows": len(merged), "bytes": len(blob),
                              "sha256": sha256(blob), "source_url": url,
                              "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")}
        store.save_manifest("vix", year)
        got += len(rows)
    return got


def fetch_ca(store, http, start, end, now, log):
    """Corporate actions a year at a time (the current year is fetched again on every update)."""
    got = 0
    for year in range(start.year, end.year + 1):
        a, b = max(start, datetime.date(year, 1, 1)), min(end, datetime.date(year, 12, 31))
        man = store.manifest("ca", year)
        path = os.path.join(store.root, "ca", "%d.json.gz" % year)
        e = man["files"].get(str(year))
        if e and e.get("complete") and os.path.exists(path):
            continue
        a = datetime.date(year, 1, 1)
        url = ca_url(a, b)
        status, data = http.get(url)
        if status != 200 or is_html(data):
            log("corporate actions %d: HTTP %s, left for the next run" % (year, status))
            continue
        try:
            raw = json.loads(data.decode("utf-8", "replace"))
            if not isinstance(raw, list):
                raise ValueError
        except ValueError:
            log("corporate actions %d: not the JSON list we know" % year)
            continue
        blob = gz_bytes(json.dumps(raw, ensure_ascii=False, sort_keys=True))
        atomic_write(path, blob)
        man["files"][str(year)] = {"file": "%d.json.gz" % year, "rows": len(raw), "bytes": len(blob),
                                   "sha256": sha256(blob), "source_url": url,
                                   "splits_bonuses": len(ca_events(raw)),
                                   "complete": b == datetime.date(year, 12, 31) and now.date() > b,
                                   "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")}
        store.save_manifest("ca", year)
        got += len(raw)
    return got


def fetch_lots(store, http, now, log):
    d = now.date()
    if store.has("lots", d):
        return 0
    status, data = http.get(LOTS_URL)
    if status != 200 or is_html(data):
        log("lot sizes: HTTP %s, left for the next run" % status)
        return 0
    try:
        rows, _ = parse_lots(data.decode("utf-8", "replace"))
    except ArchiveError as e:
        log("lot sizes: %s" % e)
        return 0
    store.put_rows("lots", d, LOTS_COLS, rows, {"source_url": LOTS_URL, "format": "csv", "source_bytes": len(data),
                                                "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")})
    return len(rows)


def backfill(store, http, start, end, datasets=DEFAULT_DATASETS, now=None, log=None, max_days=None, indices=None):
    """Fetch every missing file for start..end, newest day first. Returns a summary dict. Raises Blocked
    (after saving everything fetched) when NSE keeps refusing."""
    now = now or ist_now()
    log = log or (lambda msg: None)
    end = min(parse_day(end), now.date())
    start = parse_day(start)
    if start > end:
        raise ArchiveError("--from %s is after --to %s" % (iso(start), iso(end)))
    bad = [d for d in datasets if d not in DATASETS]
    if bad:
        raise ArchiveError("unknown dataset %s: the datasets are %s" % (", ".join(bad), ",".join(DATASETS)))
    t0 = time.time()
    calls0, bytes0 = http.calls, http.bytes
    summary = {"from": iso(start), "to": iso(end), "files": {}, "rows": {}, "closed": [], "errors": [],
               "not_published": []}
    daily = [ds for ds in DAILY if ds in datasets]
    days = list(reversed(candidate_days(start, end, store.calendar()["closed"])))
    if max_days:
        days = days[:max_days]
    try:
        for n, d in enumerate(days, 1):
            parts = []
            for ds in daily:
                res, info = fetch_daily(store, http, ds, d, now)
                if res == "ok":
                    summary["files"][ds] = summary["files"].get(ds, 0) + 1
                    summary["rows"][ds] = summary["rows"].get(ds, 0) + info
                    parts.append("%s %s rows" % (ds, "{:,}".format(info)))
                elif res == "closed":
                    summary["closed"].append(iso(d))
                    parts.append("no session (holiday)")
                    break
                elif res == "none" and ds in ("eq", "fo") and (now.date() - d).days <= NOT_PUBLISHED_DAYS:
                    summary["not_published"].append(iso(d))
                    parts.append("%s not published yet" % ds)
                    break
                elif res == "error":
                    summary["errors"].append(info)
                    parts.append("%s failed" % ds)
            if parts:
                log("%s  %s  (%d/%d)" % (iso(d), ", ".join(parts), n, len(days)))
        if "vix" in datasets:
            summary["rows"]["vix"] = fetch_vix(store, http, start, end, now, log)
        if "ca" in datasets:
            summary["rows"]["ca"] = fetch_ca(store, http, start, end, now, log)
        if "lots" in datasets:
            summary["rows"]["lots"] = fetch_lots(store, http, now, log)
        if "indexhist" in datasets:
            summary["rows"]["indexhist"] = fetch_indexhist(store, http, start, end, now, log, indices)
        if "results" in datasets:
            summary["rows"]["results"] = fetch_results(store, http, start, end, now, log)
        if "members" in datasets:
            summary["rows"]["members"] = fetch_members(store, http, start, end, now, log)
    finally:
        summary["calls"] = http.calls - calls0
        summary["downloaded_bytes"] = http.bytes - bytes0
        summary["seconds"] = round(time.time() - t0, 1)
    return summary


def last_archived(store, datasets):
    last = None
    for ds in datasets:
        if ds in DAILY:
            ds_dates = store.dates(ds)
            if ds_dates and (last is None or ds_dates[-1] < last):
                last = ds_dates[-1]
    return last


# ---------------------------------------------------------------- the read API (offline, pure)

_CACHE = {}


def _files(root, ds, start=None, end=None):
    """[(date, path)] of a daily dataset within start..end."""
    st = Store(root)
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    return [(k, st.path(ds, k)) for k in st.dates(ds) if a <= k <= b]


def _gz_lines(path):
    with open(path, "rb") as fh:
        return gzip.decompress(fh.read()).decode("utf-8").splitlines()


def trading_days(start=None, end=None, root=None, expected=False):
    """The sessions the archive holds (any of eq, fo, index), oldest first. expected=True: the sessions
    there should be, from the calendar (weekdays, NSE holidays, the closed days learned from NSE, the
    special sessions) for start..end; start and end are then required."""
    root = default_root(root)
    if expected:
        if not (start and end):
            raise ArchiveError("trading_days(expected=True) needs start and end")
        return [iso(d) for d in candidate_days(start, end, Store(root).calendar()["closed"])]
    have = set()
    for ds in ("eq", "fo", "index"):
        have.update(k for k, _ in _files(root, ds, start, end))
    return sorted(have)


def adjustment_factors(symbol, root=None):
    """[{"ex_date", "factor", "kind", "subject"}] for one symbol, oldest first, from the archived
    corporate actions. A price before ex_date is multiplied by factor (volumes divided)."""
    root = default_root(root)
    base = os.path.join(root, "ca")
    key = ("ca", os.path.abspath(root))
    try:
        stamp = tuple(sorted((f, os.path.getmtime(os.path.join(base, f))) for f in os.listdir(base)
                             if f.endswith(".json.gz")))
    except OSError:
        stamp = ()
    if _CACHE.get(key, (None,))[0] != stamp:
        raw = []
        for f, _ in stamp:
            with open(os.path.join(base, f), "rb") as fh:
                raw.extend(json.loads(gzip.decompress(fh.read()).decode("utf-8")))
        by = {}
        for e in ca_events(raw):
            by.setdefault(e["symbol"], []).append(e)
        _CACHE[key] = (stamp, by)
    return [dict(e) for e in _CACHE[key][1].get(symbol.upper(), [])]


def cumulative_factor(events, day):
    f = 1.0
    for e in events:
        if e["ex_date"] > day:
            f *= e["factor"]
    return f


def _num(x, default=None):
    try:
        return float(x) if x not in ("", None) else default
    except ValueError:
        return default


def _eq_rows(root, symbols, start, end):
    """{symbol: {date: [rows]}} for a few symbols, scanning each day's file once. A per-symbol cache
    under archive/.cache/eq/ remembers which days were scanned (delete it any time)."""
    want = sorted(set(s.upper() for s in symbols))
    files = _files(root, "eq")
    out = {s: {} for s in want}
    todo = {}
    for s in want:
        cpath = os.path.join(root, ".cache", "eq", "%s.json.gz" % s)
        cache = None
        if os.path.exists(cpath):
            try:
                with open(cpath, "rb") as fh:
                    cache = json.loads(gzip.decompress(fh.read()).decode("utf-8"))
            except (OSError, ValueError):
                cache = None
        cache = cache or {"scanned": {}, "rows": {}}
        out[s] = cache
        for k, p in files:
            if cache["scanned"].get(k) != os.path.getsize(p):
                todo.setdefault(k, []).append(s)
    paths = dict(files)
    for k in sorted(todo):
        lines = _gz_lines(paths[k])
        for s in todo[k]:
            prefix = s + ","
            rows = [next(csv.reader([l])) for l in lines[1:] if l.startswith(prefix)]
            if rows:
                out[s]["rows"][k] = rows
            else:
                out[s]["rows"].pop(k, None)
            out[s]["scanned"][k] = os.path.getsize(paths[k])
    if todo:
        for s in set(x for v in todo.values() for x in v):
            try:
                atomic_write(os.path.join(root, ".cache", "eq", "%s.json.gz" % s),
                             gz_bytes(json.dumps(out[s], sort_keys=True)))
            except OSError:
                pass      # a read-only archive still reads
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    return {s: {k: v for k, v in out[s]["rows"].items() if a <= k <= b} for s in want}


def _daily_bars(symbol, by_day, adjusted, root, series):
    events = adjustment_factors(symbol, root) if adjusted else []
    bars = []
    for k in sorted(by_day):
        rows = {r[1]: r for r in by_day[k]}
        pick = next((rows[s] for s in series if s in rows), None)
        if pick is None:
            continue
        o, h, l, c = (_num(pick[i]) for i in (3, 4, 5, 6))
        if None in (o, h, l, c) or c <= 0:
            continue
        if o <= 0 or h <= 0 or l <= 0:      # no trade that day: NSE prints the previous close as the close
            o = h = l = c
        f = cumulative_factor(events, k) if events else 1.0
        v = _num(pick[9], 0.0)
        bars.append({"date": k, "time": "", "o": round(o * f, 2), "h": round(h * f, 2), "l": round(l * f, 2),
                     "c": round(c * f, 2), "v": int(round(v / f)), "series": pick[1], "isin": pick[2],
                     "turnover": _num(pick[10], 0.0), "trades": int(_num(pick[11], 0) or 0), "factor": f})
    return bars


def load_daily(symbol, start=None, end=None, adjusted=True, root=None, series=SERIES_PREF):
    """Daily bars of one NSE stock from the equity bhavcopies, oldest first:
    {"date", "time": "", "o", "h", "l", "c", "v", "series", "isin", "turnover", "trades", "factor"}.
    adjusted=True multiplies prices before each split/bonus ex-date by its factor (volumes divided), so
    the series has no artificial jumps. One row per day: the first series of `series` that traded."""
    root = default_root(root)
    by = _eq_rows(root, [symbol], start, end)[symbol.upper()]
    return _daily_bars(symbol.upper(), by, adjusted, root, series)


def load_daily_many(symbols, start=None, end=None, adjusted=True, root=None, series=SERIES_PREF):
    """{symbol: bars} for several stocks, reading each day's file once."""
    root = default_root(root)
    by = _eq_rows(root, symbols, start, end)
    return {s: _daily_bars(s, by[s], adjusted, root, series) for s in by}


def index_name(name):
    n = (name or "").strip()
    return INDEX_NAMES.get(n.upper(), INDEX_NAMES.get(n.upper().replace(" ", ""), n))


# names an index carried before NSE's renames (S&P branding until 2013, CNX until November 2015), lower case
OLD_INDEX_NAMES = {
    "s&p cnx nifty": "nifty 50", "cnx nifty": "nifty 50", "nifty": "nifty 50", "cnx nifty junior": "nifty next 50",
    "nifty junior": "nifty next 50", "bank nifty": "nifty bank", "cnx bank": "nifty bank", "s&p cnx 500": "nifty 500",
    "cnx 500": "nifty 500", "cnx 100": "nifty 100", "cnx 200": "nifty 200", "cnx midcap": "nifty midcap 100",
    "cnx smallcap": "nifty smallcap 100", "cnx finance": "nifty financial services",
    "cnx infrastructure": "nifty infrastructure", "cnx infra": "nifty infrastructure",
    "s&p cnx nifty dividend": "nifty50 dividend points",
}


def index_canon(name):
    """Any spelling an NSE index has had -> one lower-case key ('S&P CNX Nifty', 'CNX Nifty', 'NIFTY 50',
    'Nifty 50' -> 'nifty 50'), so one series reads across the renames."""
    s = " ".join(str(name or "").replace("&amp;", "&").lower().split())
    if s in OLD_INDEX_NAMES:
        return OLD_INDEX_NAMES[s]
    s = re.sub(r"^s&p\s+", "", s)
    if s in OLD_INDEX_NAMES:
        return OLD_INDEX_NAMES[s]
    s = re.sub(r"^cnx\s+", "nifty ", s).replace("nifty nifty", "nifty")
    return OLD_INDEX_NAMES.get(s, s)


def load_index(name, start=None, end=None, root=None):
    """Daily OHLC of one NSE index (any name in ind_close_all, or NIFTY, BANKNIFTY, ^NSEI, ^INDIAVIX...):
    [{"date", "time": "", "o", "h", "l", "c", "v", "pe", "pb", "div_yield"}]. An index with only a
    close that day gets o = h = l = c. Old names (S&P CNX Nifty, CNX Bank, ...) read as today's; days before
    the daily index files (2012-02-21) come from the indexhist dataset when it was backfilled."""
    root = default_root(root)
    want = index_canon(index_name(name))
    bars = []
    for k, p in _files(root, "index", start, end):
        for row in csv.reader(_gz_lines(p)[1:]):
            if row and index_canon(row[0]) == want:
                c = _num(row[4])
                if c is None:
                    break
                o, h, l = (_num(row[i], c) for i in (1, 2, 3))
                bars.append({"date": k, "time": "", "o": o, "h": h, "l": l, "c": c, "v": int(_num(row[6], 0) or 0),
                             "pe": _num(row[8]), "pb": _num(row[9]), "div_yield": _num(row[10])})
                break
    have = set(b["date"] for b in bars)
    old = [b for b in _indexhist_bars(root, want, start, end) if b["date"] not in have]
    return sorted(bars + old, key=lambda b: b["date"]) if old else bars


def load_vix(start=None, end=None, root=None):
    """India VIX daily: [{"date", "time": "", "o", "h", "l", "c", "prev_close"}] from the VIX history,
    with days only the index files have filled in from there."""
    root = default_root(root)
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    have = {}
    base = os.path.join(root, "vix")
    for f in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if re.match(r"^\d{4}\.csv\.gz$", f):
            for r in read_gz_csv(os.path.join(base, f))[1]:
                if a <= r[0] <= b and _num(r[4]) is not None:
                    have[r[0]] = {"date": r[0], "time": "", "o": _num(r[1]), "h": _num(r[2]), "l": _num(r[3]),
                                  "c": _num(r[4]), "prev_close": _num(r[5]), "v": 0}
    for bar in load_index("India VIX", start, end, root):
        if bar["date"] not in have:
            have[bar["date"]] = {"date": bar["date"], "time": "", "o": bar["o"], "h": bar["h"], "l": bar["l"],
                                 "c": bar["c"], "prev_close": None, "v": 0}
    return [have[k] for k in sorted(have)]


def load_ban(day, root=None):
    """The F&O ban list for one trading day (symbols), or None when the archive does not have that day."""
    p = Store(default_root(root)).path("ban", parse_day(day))
    if not os.path.exists(p):
        return None
    return [r[0] for r in read_gz_csv(p)[1] if r]


def load_settle(day, root=None):
    p = Store(default_root(root)).path("settle", parse_day(day))
    if not os.path.exists(p):
        return None
    return [{"instrument": r[0], "underlying": r[1], "expiry": r[2], "settle": _num(r[3])}
            for r in read_gz_csv(p)[1] if r]


def load_lots(day=None, root=None):
    """{symbol: {YYYY-MM: lot}} from the lot-size snapshot on `day` (or the latest one on or before it)."""
    st = Store(default_root(root))
    dates = [k for k in st.dates("lots") if day is None or k <= iso(day)]
    if not dates:
        return {}
    out = {}
    for r in read_gz_csv(st.path("lots", dates[-1]))[1]:
        out.setdefault(r[1], {})[r[2]] = int(float(r[3]))
    return out


def load_fo_chain_eod(underlying, day, root=None):
    """The end-of-day F&O board of one underlying from the bhavcopy:
    {"date", "underlying", "spot", "format", "futures": [...], "options": [...]}; each option row has
    expiry, strike, option_type (CE/PE), open, high, low, close, settle, oi (shares), oi_change,
    volume (contracts), turnover, lot. None when the archive does not have that day. spot is NSE's
    underlying price (UDiFF); for older legacy files it comes from the index or equity archive."""
    root = default_root(root)
    k = iso(parse_day(day))
    p = Store(root).path("fo", k)
    if not os.path.exists(p):
        return None
    sym = underlying.upper()
    prefix = sym + ","
    lines = _gz_lines(p)
    fmt = (Store(root).entry("fo", k) or {}).get("format")
    futs, opts, spot = [], [], None
    for row in csv.reader([l for l in lines[1:] if l.startswith(prefix)]):
        rec = dict(zip(FO_COLS, row))
        for c in ("strike", "open", "high", "low", "close", "settle", "underlying_price", "turnover"):
            rec[c] = _num(rec[c])
        for c in ("oi", "oi_change", "volume", "lot"):
            rec[c] = int(_num(rec[c], 0) or 0) if rec[c] != "" else None
        if spot is None and rec["underlying_price"]:
            spot = rec["underlying_price"]
        del rec["symbol"]
        (opts if rec["kind"] in ("IDO", "STO") else futs).append(rec)
    if not futs and not opts:
        return {"date": k, "underlying": sym, "spot": None, "format": fmt, "futures": [], "options": []}
    if spot is None:
        idx = load_index(sym, k, k, root) if sym in INDEX_SYMBOLS else []
        if idx:
            spot = idx[0]["c"]
        else:
            eq = load_daily(sym, k, k, adjusted=False, root=root)
            spot = eq[0]["c"] if eq else None
    lots = None
    for rec in futs + opts:
        if rec["lot"] is None:
            if lots is None:
                lots = load_lots(k, root).get(sym, {})
            rec["lot"] = lots.get(rec["expiry"][:7])
    return {"date": k, "underlying": sym, "spot": spot, "format": fmt, "futures": futs, "options": opts}


def _intraday_dir(root, interval, symbol):
    return os.path.join(root, "intraday", interval, symbol.upper())


def _read_bars(path):
    out = []
    for r in read_gz_csv(path)[1]:
        if len(r) >= 7:
            out.append({"date": r[0], "time": r[1], "o": float(r[2]), "h": float(r[3]), "l": float(r[4]),
                        "c": float(r[5]), "v": int(float(r[6] or 0)), "oi": int(float(r[7])) if len(r) > 7 and r[7]
                        else None, "source": r[8] if len(r) > 8 else ""})
    return out


def aggregate(bars, minutes):
    """1-minute bars -> N-minute bars on the 09:15 grid. A bucket is kept only when complete."""
    groups = {}
    for b in bars:
        hh, mm = int(b["time"][:2]), int(b["time"][3:5])
        m = hh * 60 + mm
        start = m - (m - OPEN_MIN) % minutes
        groups.setdefault((b["date"], start), []).append(b)
    out = []
    for (d, start), g in sorted(groups.items()):
        g.sort(key=lambda b: b["time"])
        if len(g) < minutes:
            continue
        out.append({"date": d, "time": "%02d:%02d" % (start // 60, start % 60), "o": g[0]["o"],
                    "h": max(b["h"] for b in g), "l": min(b["l"] for b in g), "c": g[-1]["c"],
                    "v": sum(b["v"] for b in g), "oi": g[-1].get("oi"), "source": g[0].get("source", "")})
    return out


def load_intraday(symbol, interval="1m", start=None, end=None, root=None):
    """Intraday bars of one symbol from the archive, oldest first: {"date", "time" (bar start, IST), "o",
    "h", "l", "c", "v", "oi", "source" (upstox, kite, dhan or yahoo)}. Regular session only. "5m" falls
    back to complete 5-minute buckets built from 1-minute bars when no 5-minute file exists."""
    root = default_root(root)
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    if interval not in INTERVALS:
        raise ArchiveError("interval is 1m or 5m")
    d = _intraday_dir(root, interval, symbol)
    files = sorted(f for f in os.listdir(d) if re.match(r"^\d{4}-\d\d\.csv\.gz$", f)) if os.path.isdir(d) else []
    files = [f for f in files if a[:7] <= f[:7] <= b[:7]]
    if not files and interval != "1m":
        one = load_intraday(symbol, "1m", start, end, root)
        return aggregate(one, INTERVALS[interval])
    out = []
    for f in files:
        out.extend(x for x in _read_bars(os.path.join(d, f)) if a <= x["date"] <= b)
    return out


def chain_snapshots(underlying, day, root=None):
    """[{"time", "expiry", "path"}] of the option-chain snapshots taken on one day (snapshot-chain)."""
    d = os.path.join(default_root(root), "chain", underlying.upper(), iso(parse_day(day)))
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        m = re.match(r"^(\d\d)(\d\d)_(\d{4}-\d\d-\d\d)\.json\.gz$", f)
        if m:
            out.append({"time": "%s:%s" % (m.group(1), m.group(2)), "expiry": m.group(3), "path": os.path.join(d, f)})
    return out


def load_chain_snapshot(path):
    with open(path, "rb") as fh:
        return json.loads(gzip.decompress(fh.read()).decode("utf-8"))


# ---------------------------------------------------------------- intraday ingest (broker history, Yahoo)

def broker_adapters(path=None):
    """trader-broker's adapters/ folder (next to this skill), loaded by path; None when it is not there."""
    import importlib.util
    name = "bt_trader_broker_adapters"
    if name in sys.modules:
        return sys.modules[name]
    d = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-broker",
                             "adapters")
    if not os.path.isfile(os.path.join(d, "__init__.py")):
        return None
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "__init__.py"), submodule_search_locations=[d])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def latest_isin(symbol, root):
    """The stock's latest ISIN from the equity archive (an ISIN changes on a split), or None."""
    st = Store(root)
    for k in reversed(st.dates("eq")[-15:]):
        for l in _gz_lines(st.path("eq", k))[1:]:
            if l.startswith(symbol.upper() + ","):
                r = next(csv.reader([l]))
                if r[1] in SERIES_PREF and r[2]:
                    return r[2]
    return None


def broker_instrument(adapter, symbol, root):
    """The instrument dict adapter.history() wants."""
    sym = symbol.upper()
    name = index_name(sym)
    if adapter.name == "upstox":
        if name in UPSTOX_INDEX_KEYS:
            return {"symbol": sym, "exchange": "NSE", "segment": "INDEX", "broker_id": UPSTOX_INDEX_KEYS[name]}
        isin = latest_isin(sym, root)
        if isin:
            return {"symbol": sym, "exchange": "NSE", "segment": "EQ", "broker_id": "NSE_EQ|" + isin}
    for inst in adapter.instruments():
        if str(inst.get("symbol", "")).upper() == sym and inst.get("exchange") in ("NSE", None) \
                and str(inst.get("segment", "EQ")).upper() in ("EQ", "INDEX", "INDICES", ""):
            return inst
    raise ArchiveError("%s: no %s instrument for %s (backfill the equity archive first, or check the symbol)"
                       % (adapter.name, adapter.name, sym))


def month_windows(start, end):
    out, d = [], start
    while d <= end:
        nxt = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out.append((d, min(end, nxt - datetime.timedelta(days=1))))
        d = nxt
    return out


def candle_bars(candles, source):
    """adapter.history() rows [[ist_iso, o, h, l, c, v], ...] -> regular-session bars, oldest first."""
    out = {}
    for c in candles or []:
        t = str(c[0])
        if len(t) < 16 or None in c[1:5]:
            continue
        hh, mm = int(t[11:13]), int(t[14:16])
        m = hh * 60 + mm
        if m < OPEN_MIN or m >= CLOSE_MIN:
            continue
        key = (t[:10], t[11:16])
        out[key] = {"date": key[0], "time": key[1], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
                    "c": float(c[4]), "v": int(float(c[5] or 0)) if len(c) > 5 else 0,
                    "oi": int(float(c[6])) if len(c) > 6 and c[6] else None, "source": source}
    return [out[k] for k in sorted(out)]


def yahoo_bars(text, interval, now):
    """A Yahoo v8 chart JSON -> complete regular-session bars on the grid, source 'yahoo'."""
    try:
        doc = json.loads(text) if isinstance(text, str) else text
        res = doc["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ArchiveError("not a Yahoo chart response")
    step = INTERVALS[interval]
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c) or int(ts) % (60 * step):
            continue
        t = datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)
        m = t.hour * 60 + t.minute
        if m < OPEN_MIN or m >= CLOSE_MIN or t + datetime.timedelta(minutes=step) > now:
            continue
        key = (t.date().isoformat(), t.strftime("%H:%M"))
        out[key] = {"date": key[0], "time": key[1], "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                    "c": round(c, 2), "v": int(v or 0), "oi": None, "source": "yahoo"}
    return [out[k] for k in sorted(out)]


def fetch_yahoo(symbol, interval, http):
    rng = "60d" if interval == "5m" else "7d"
    sym = symbol if symbol.startswith("^") else symbol.upper() + ".NS"
    status, data = http.get(YAHOO % (urllib.parse.quote(sym), rng, interval), pause=0.6)
    if status != 200:
        raise ArchiveError("Yahoo answered HTTP %s for %s" % (status, sym))
    return data.decode("utf-8", "replace")


def merge_bars(old, new):
    """Merge two bar lists on (date, time). A broker bar beats a Yahoo bar; a Yahoo bar only fills a slot
    nobody else has. Returns (bars, added, conflicts): conflicts are slots where a broker bar and a Yahoo
    bar disagree on the close by more than 0.5%."""
    by = {(b["date"], b["time"]): b for b in old}
    added = conflicts = 0
    for b in new:
        k = (b["date"], b["time"])
        cur = by.get(k)
        if cur is None:
            by[k] = b
            added += 1
            continue
        if (cur.get("source") == "yahoo") != (b.get("source") == "yahoo") and cur["c"] and \
                abs(b["c"] / cur["c"] - 1) > 0.005:
            conflicts += 1
        if b.get("source") != "yahoo" or cur.get("source") == "yahoo":
            by[k] = b
    return [by[k] for k in sorted(by)], added, conflicts


def write_intraday(root, interval, symbol, bars, meta_extra, now):
    """Merge bars into the per-month files and the symbol's manifest. Returns (added, conflicts)."""
    d = _intraday_dir(root, interval, symbol)
    man_path = os.path.join(d, "manifest.json")
    man = read_json(man_path, None) or {"schema": SCHEMA, "symbol": symbol.upper(), "interval": interval,
                                        "months": {}}
    months = {}
    for b in bars:
        months.setdefault(b["date"][:7], []).append(b)
    added = conflicts = 0
    for month, new in sorted(months.items()):
        path = os.path.join(d, month + ".csv.gz")
        old = _read_bars(path) if os.path.exists(path) else []
        merged, a, c = merge_bars(old, new)
        added += a
        conflicts += c
        rows = [[b["date"], b["time"], _f(str(b["o"])), _f(str(b["h"])), _f(str(b["l"])), _f(str(b["c"])),
                 str(int(b["v"])), "" if b.get("oi") is None else str(b["oi"]), b.get("source", "")] for b in merged]
        blob = gz_bytes(csv_text(BAR_COLS, rows))
        atomic_write(path, blob)
        src = {}
        for b in merged:
            src[b.get("source", "")] = src.get(b.get("source", ""), 0) + 1
        e = man["months"].get(month, {})
        e.update({"file": month + ".csv.gz", "rows": len(rows), "bytes": len(blob), "sha256": sha256(blob),
                  "sources": src, "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30")})
        e.update(meta_extra.get(month, {}))
        man["months"][month] = e
    write_json(man_path, man)
    return added, conflicts


def intraday_ingest(root, symbols, interval, start, end, broker="upstox", adapter=None, now=None, log=None,
                    sleep=None, yahoo_http=None, use_broker=True, use_yahoo=False):
    """Pull broker history candles (a month per call) and/or Yahoo's recent bars into the archive.
    A month already fetched complete is skipped (resume). Returns {symbol: {...}} and a list of notes."""
    now = now or ist_now()
    log = log or (lambda m: None)
    sleep = sleep or time.sleep
    out, notes = {}, []
    start, end = parse_day(start), min(parse_day(end), now.date())
    if use_broker and adapter is None:
        mod = broker_adapters()
        if mod is None:
            raise ArchiveError("intraday from a broker needs the trader-broker skill next to trader-archive "
                               "(its adapters/ folder); or use --yahoo for the last 60 sessions only")
        adapter = mod.make(broker)
    if use_broker and not adapter.token():
        msg = ("%s: no access token in %s, so no broker candles. Log in once: eval \"$(python3 "
               "trader-broker/ticket.py login %s)\"" % (adapter.title, adapter.env_names.get("access_token"),
                                                       adapter.name))
        if adapter.name == "upstox" and not use_yahoo:
            raise ArchiveError(msg)
        notes.append(msg + " (skipped)")
        use_broker = False
    if use_broker and adapter.name == "upstox" and start < UPSTOX_MINUTES_FROM:
        notes.append("Upstox keeps minute candles from January 2022; earlier months are skipped")
        start = max(start, UPSTOX_MINUTES_FROM)
    for sym in symbols:
        sym = sym.upper()
        rec = {"broker_bars": 0, "yahoo_bars": 0, "added": 0, "conflicts": 0, "months_skipped": 0}
        if use_broker:
            man = read_json(os.path.join(_intraday_dir(root, interval, sym), "manifest.json"), None) or {}
            inst = broker_instrument(adapter, sym, root)
            for a, b in month_windows(start, end):
                month = iso(a)[:7]
                e = (man.get("months") or {}).get(month) or {}
                if e.get("complete") and e.get("broker") == adapter.name:
                    rec["months_skipped"] += 1
                    continue
                candles = adapter.history(inst, interval, iso(a), iso(b))
                bars = candle_bars(candles, adapter.name)
                last_day = datetime.date(a.year + (a.month == 12), a.month % 12 + 1, 1) - datetime.timedelta(days=1)
                complete = b == last_day and (now.date() - b).days >= 1 and a.day == 1
                meta = {month: {"complete": complete, "covers": "%s..%s" % (iso(a), iso(b)),
                                "instrument": inst.get("broker_id"), "broker": adapter.name}}
                ad, cf = write_intraday(root, interval, sym, bars, meta, now)
                rec["broker_bars"] += len(bars)
                rec["added"] += ad
                rec["conflicts"] += cf
                log("%s %s %s: %d %s candles" % (sym, interval, month, len(bars), adapter.name))
                sleep(BROKER_PAUSE)
        if use_yahoo:
            try:
                bars = yahoo_bars(fetch_yahoo(sym, interval, yahoo_http or Http(pause=MIN_PAUSE)), interval, now)
            except ArchiveError as e:
                notes.append("%s: %s" % (sym, e))
                bars = []
            bars = [b for b in bars if iso(start) <= b["date"] <= iso(end)] if use_broker else bars
            if bars:
                ad, cf = write_intraday(root, interval, sym, bars, {}, now)
                rec["yahoo_bars"] += len(bars)
                rec["added"] += ad
                rec["conflicts"] += cf
                log("%s %s: %d Yahoo bars (%d new)" % (sym, interval, len(bars), ad))
        out[sym] = rec
    return out, notes


def intraday_gaps(root, symbol, interval, start=None, end=None):
    """Per trading day: bars expected vs held. Sessions come from the archive's own calendar (eq/fo/index
    files) when it has them, else from the days the bars cover. Short sessions are left out."""
    bars = load_intraday(symbol, interval, start, end, root)
    if not bars:
        return {"symbol": symbol.upper(), "interval": interval, "days": 0, "missing_days": [], "short_days": [],
                "bars": 0}
    per = INTERVALS[interval]
    want = (CLOSE_MIN - OPEN_MIN) // per
    have = {}
    for b in bars:
        have[b["date"]] = have.get(b["date"], 0) + 1
    first, last = min(have), max(have)
    sessions = [d for d in trading_days(first, last, root) if d not in SHORT_SESSIONS] or sorted(have)
    missing = [d for d in sessions if d not in have]
    short = [{"date": d, "bars": have[d], "expected": want} for d in sorted(have)
             if have[d] < want and d not in SHORT_SESSIONS]
    return {"symbol": symbol.upper(), "interval": interval, "from": first, "to": last, "days": len(have),
            "bars": len(bars), "missing_days": missing, "short_days": short}


# ---------------------------------------------------------------- option-chain snapshots

def market_open(now, closed=None):
    m = now.hour * 60 + now.minute
    return session_kind(now.date(), closed) == "session" and OPEN_MIN <= m <= CLOSE_MIN


def snapshot_chain(root, http, symbols, expiries=1, now=None, log=None):
    """One snapshot of each symbol's nearest option-chain expiries -> archive/chain/SYM/DATE/HHMM_EXPIRY.json.gz.
    Returns the number of files written."""
    now = now or ist_now()
    log = log or (lambda m: None)
    wrote = 0
    for sym in symbols:
        sym = sym.upper()
        status, data = http.get("%s/api/option-chain-contract-info?symbol=%s" % (WWW, urllib.parse.quote(sym)))
        if status != 200 or is_html(data):
            log("%s: contract info HTTP %s" % (sym, status))
            continue
        try:
            exps = json.loads(data.decode("utf-8"))["expiryDates"][:max(1, expiries)]
        except (ValueError, KeyError, TypeError):
            log("%s: the contract info is not the JSON we know" % sym)
            continue
        kind = "Indices" if sym in INDEX_SYMBOLS else "Equity"
        for exp in exps:
            url = "%s/api/option-chain-v3?type=%s&symbol=%s&expiry=%s" % (WWW, kind, urllib.parse.quote(sym),
                                                                         urllib.parse.quote(exp))
            status, data = http.get(url)
            if status != 200 or is_html(data):
                log("%s %s: chain HTTP %s" % (sym, exp, status))
                continue
            try:
                doc = json.loads(data.decode("utf-8"))
            except ValueError:
                log("%s %s: the chain is not JSON" % (sym, exp))
                continue
            doc = {"schema": SCHEMA + "/chain", "symbol": sym, "expiry": nse_date(exp), "source_url": url,
                   "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"), "data": doc}
            path = os.path.join(root, "chain", sym, now.date().isoformat(),
                                "%s_%s.json.gz" % (now.strftime("%H%M"), nse_date(exp)))
            atomic_write(path, gz_bytes(json.dumps(doc, ensure_ascii=False, sort_keys=True)))
            wrote += 1
            log("%s %s: snapshot %s" % (sym, exp, os.path.relpath(path, root)))
    return wrote


# ---------------------------------------------------------------- the long history (helpers in history/)

def history():
    """This skill's history/ helper package (announcement PDFs, membership, continuous futures, results,
    expired options), loaded by path so the folder works copied alone."""
    import importlib.util
    name = "bt_archive_history"
    if name in sys.modules:
        return sys.modules[name]
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "__init__.py"), submodule_search_locations=[d])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    for sub in ("pdftext", "members", "continuous", "events", "expired"):
        __import__(name + "." + sub)
    return mod


def _stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def _put_json(root, rel, doc):
    blob = gz_bytes(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    atomic_write(os.path.join(root, rel), blob)
    return blob


def _get_json(root, rel, default=None):
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            return json.loads(gzip.decompress(fh.read()).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return default


# -- index history before the daily index files

INDEXHIST_URL = WWW + "/api/historicalOR/indicesHistory?indexType=%s&from=%s&to=%s"
INDEXHIST_DEFAULT = ("NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "NIFTY BANK",
                     "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100", "NIFTY IT", "NIFTY AUTO", "NIFTY FMCG", "NIFTY PHARMA",
                     "NIFTY METAL", "NIFTY ENERGY", "NIFTY REALTY", "NIFTY PSU BANK", "NIFTY FINANCIAL SERVICES",
                     "NIFTY MEDIA", "NIFTY INFRASTRUCTURE", "NIFTY MNC", "NIFTY COMMODITIES", "NIFTY CONSUMPTION")
INDEXHIST_COLS = ["date", "open", "high", "low", "close", "volume", "turnover_cr", "published_as"]


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", index_canon(name)).strip("-")


def parse_indexhist(text):
    """indicesHistory JSON -> [[date, o, h, l, c, volume, turnover_cr, name]] oldest first."""
    try:
        doc = json.loads(text)
        data = doc["data"] if isinstance(doc, dict) else doc
    except (ValueError, KeyError, TypeError):
        raise ArchiveError("the index history is not the JSON we know")
    out = {}
    for r in data or []:
        day = nse_date(r.get("EOD_TIMESTAMP"))
        if not day:
            continue
        out[day] = [day] + [_f(str(r.get(k, ""))) for k in ("EOD_OPEN_INDEX_VAL", "EOD_HIGH_INDEX_VAL",
                                                           "EOD_LOW_INDEX_VAL", "EOD_CLOSE_INDEX_VAL",
                                                           "HIT_TRADED_QTY", "HIT_TURN_OVER")] + \
            [str(r.get("EOD_INDEX_NAME") or "")]
    return [out[k] for k in sorted(out)]


def _indexhist_names(root, indices):
    if indices in (None, "", "default"):
        return list(INDEXHIST_DEFAULT)
    if indices == "all":
        dates = Store(root).dates("index")
        if not dates:
            raise ArchiveError("--indices all takes the names from the newest daily index file: backfill "
                               "--datasets index for one recent day first")
        names = [r[0] for r in read_gz_csv(Store(root).path("index", dates[-1]))[1] if r]
        return sorted(set(n.upper() for n in names if index_canon(n) != "india vix"))
    return [n.strip().upper() for n in str(indices).split(",") if n.strip()]


def fetch_indexhist(store, http, start, end, now, log, indices=None):
    """Daily OHLC of the chosen indices before 2012-02-21 (when NSE's daily index files start), a quarter
    a call per index (about 60 rows; NSE caps a call at 70). Stored as indexhist/<index>/<YYYY>.csv.gz."""
    last = INDEX_FILES_FROM - datetime.timedelta(days=1)
    if start > last:
        return 0
    got = 0
    for name in _indexhist_names(store.root, indices):
        slug = _slug(name)
        man_path = os.path.join(store.root, "indexhist", slug, "manifest.json")
        man = read_json(man_path, None) or {"schema": SCHEMA, "index": name, "chunks": {}, "files": {}}
        for a, b in quarters(start, min(end, last)):
            key = "%s..%s" % (iso(a), iso(b))
            if (man["chunks"].get(key) or {}).get("complete"):
                continue
            url = INDEXHIST_URL % (urllib.parse.quote(name), dmy_dash(a), dmy_dash(b))
            status, data = http.get(url)
            if status != 200 or is_html(data):
                log("index history %s %s: HTTP %s, left for the next run" % (name, key, status))
                continue
            try:
                rows = [r for r in parse_indexhist(data.decode("utf-8", "replace")) if iso(a) <= r[0] <= iso(b)]
            except ArchiveError as e:
                log("index history %s %s: %s" % (name, key, e))
                continue
            path = os.path.join(store.root, "indexhist", slug, "%d.csv.gz" % a.year)
            have = {r[0]: r for r in read_gz_csv(path)[1]} if os.path.exists(path) else {}
            for r in rows:
                have[r[0]] = r
            blob = gz_bytes(csv_text(INDEXHIST_COLS, [have[k] for k in sorted(have)]))
            atomic_write(path, blob)
            man["chunks"][key] = {"source_url": url, "rows": len(rows), "complete": True, "fetched_at": _stamp(now)}
            man["files"][str(a.year)] = {"file": "%d.csv.gz" % a.year, "rows": len(have), "bytes": len(blob),
                                         "sha256": sha256(blob)}
            write_json(man_path, man)
            got += len(rows)
        if got:
            log("index history %s: done to %s" % (name, iso(min(end, last))))
    return got


def _indexhist_bars(root, canon, start=None, end=None):
    d = os.path.join(root, "indexhist", re.sub(r"[^a-z0-9]+", "-", canon).strip("-"))
    if not os.path.isdir(d):
        return []
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    out = []
    for f in sorted(os.listdir(d)):
        if re.match(r"^\d{4}\.csv\.gz$", f) and a[:4] <= f[:4] <= b[:4]:
            for r in read_gz_csv(os.path.join(d, f))[1]:
                c = _num(r[4])
                if c is None or not (a <= r[0] <= b):
                    continue
                o, h, l = (_num(r[i], c) or c for i in (1, 2, 3))
                out.append({"date": r[0], "time": "", "o": o, "h": h, "l": l, "c": c, "v": int(_num(r[5], 0) or 0),
                            "pe": None, "pb": None, "div_yield": None})
    return out


# -- results dates

def fetch_results(store, http, start, end, now, log):
    """Board meetings and quarterly results filings, a calendar month a call each, into results/<YYYY>/.
    A month is asked again until it ended more than a few days ago."""
    ev = history().events
    man_path = os.path.join(store.root, "results", "manifest.json")
    man = read_json(man_path, None) or {"schema": SCHEMA, "months": {}}
    got = 0
    for a, b in month_windows(datetime.date(start.year, start.month, 1), end):
        month = iso(a)[:7]
        if (man["months"].get(month) or {}).get("complete"):
            continue
        rec = {}
        for kind, url_t, parse in (("board", ev.BOARD_URL, ev.board_rows), ("filings", ev.FILINGS_URL, ev.filing_rows)):
            url = url_t % (dmy_dash(a), dmy_dash(b))
            status, data = http.get(url)
            if status != 200 or is_html(data):
                log("results %s %s: HTTP %s, left for the next run" % (month, kind, status))
                rec = None
                break
            try:
                raw = json.loads(data.decode("utf-8", "replace"))
                if isinstance(raw, dict):
                    raw = raw.get("data") or []
                rows = parse(raw)
            except (ValueError, TypeError, AttributeError):
                log("results %s %s: not the JSON we know" % (month, kind))
                rec = None
                break
            blob = _put_json(store.root, os.path.join("results", month[:4], "%s_%s.json.gz" % (month, kind)), rows)
            rec[kind] = {"rows": len(rows), "raw_rows": len(raw), "bytes": len(blob), "sha256": sha256(blob),
                         "source_url": url}
            got += len(rows)
        if rec is None:
            continue
        rec.update({"complete": (now.date() - b).days > NOT_PUBLISHED_DAYS and b.day >= 28, "fetched_at": _stamp(now)})
        man["months"][month] = rec
        write_json(man_path, man)
        log("results %s: %d results meetings, %d filings" % (month, rec["board"]["rows"], rec["filings"]["rows"]))
    return got


def _results_rows(root, kind, start=None, end=None):
    base = os.path.join(root, "results")
    out = []
    a, b = (iso(start)[:7] if start else "0000"), (iso(end)[:7] if end else "9999")
    for y in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not re.match(r"^\d{4}$", y):
            continue
        for f in sorted(os.listdir(os.path.join(base, y))):
            m = re.match(r"^(\d{4}-\d\d)_%s\.json\.gz$" % kind, f)
            if m and a <= m.group(1) <= b:
                out.extend(_get_json(root, os.path.join("results", y, f), []) or [])
    return out


def results_dates(symbol, start=None, end=None, root=None):
    """Results (earnings) events of one stock, oldest first, from the results dataset:
    [{"symbol", "date", "announced_at", "after_close", "event_session", "period_end", "quarter", "meeting_date",
    "audited", "consolidated", "source"}]. event_session is the first session the price could react on
    (the next one when the results came out after 15:30 IST), from the archive's trading days."""
    root = default_root(root)
    pad_a = iso(parse_day(start) - datetime.timedelta(days=5)) if start else None
    board = _results_rows(root, "board", pad_a, end)
    filings = _results_rows(root, "filings", pad_a, end)
    sessions = trading_days(None, None, root) or None
    evs = history().events.results_events(symbol, board, filings, sessions)
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    return [e for e in evs if a <= e["date"] <= b]


# -- point-in-time index membership

def fetch_members(store, http, start, end, now, log):
    """The press-release listing, every index-change announcement dated from 90 days before `start` (its
    text kept as lines: re-parsing needs no download), today's NIFTY 50/Next 50/100/200/500 lists, NSE's
    symbol changes and listed companies. Returns the number of announcements fetched."""
    mem = history().members
    root = store.root
    base = os.path.join(root, "members")
    man_path = os.path.join(base, "manifest.json")
    man = read_json(man_path, None) or {"schema": SCHEMA, "announcements": {}, "lists": {}, "ref": {}}
    today = iso(now.date())
    got = 0
    if man.get("listing_fetched") != today:
        status, data = http.get(mem.LISTING_URL, accept="text/html")
        if status == 200 and data and is_html(data):
            items = mem.parse_listing(data.decode("utf-8", "replace"))
            if items:
                _put_json(root, os.path.join("members", "listing.json.gz"), items)
                man["listing_fetched"] = today
                man["listing_items"] = len(items)
                write_json(man_path, man)
        else:
            log("index announcements: the press-release page answered HTTP %s, left for the next run" % status)
    items = _get_json(root, os.path.join("members", "listing.json.gz"), []) or []
    lo = iso(start - datetime.timedelta(days=90))
    for it in items:
        if not (it.get("date") and lo <= it["date"] <= iso(end) and mem.wanted(it)):
            continue
        name = it["url"].rsplit("/", 1)[-1]
        if name in man["announcements"]:
            continue
        status, data = http.get(it["url"], accept="application/pdf")
        if status != 200 or not data.startswith(b"%PDF"):
            log("announcement %s: HTTP %s, left for the next run" % (name, status))
            continue
        try:
            lines = history().pdftext.pdf_lines(data)
        except Exception as e:        # a PDF this reader cannot take apart: recorded, never fatal
            lines = []
            log("announcement %s: could not read the PDF (%s)" % (name, e.__class__.__name__))
        rel = os.path.join("members", "announcements", it["date"][:4], name[:-4] + ".txt.gz")
        atomic_write(os.path.join(root, rel), gz_bytes("\n".join(lines)))
        changes = mem.parse_announcement(lines, it.get("title", ""), it["date"])
        man["announcements"][name] = {"date": it["date"], "title": it.get("title", ""), "source_url": it["url"],
                                      "pdf_bytes": len(data), "pdf_sha256": sha256(data), "text": rel,
                                      "lines": len(lines), "changes": len(changes), "fetched_at": _stamp(now)}
        write_json(man_path, man)
        got += 1
        log("announcement %s (%s): %d changes for the tracked indices" % (it["date"], it.get("title", "")[:60],
                                                                          len(changes)))
    if (man["lists"].get("fetched") or "") != today:
        ok = 0
        for key, url in sorted(mem.LIST_URLS.items()):
            status, data = http.get(url)
            if status == 200 and data and not is_html(data):
                try:
                    rows = mem.parse_constituents(data.decode("utf-8-sig", "replace"))
                except ValueError:
                    log("%s list: not the CSV we know" % key)
                    continue
                atomic_write(os.path.join(base, "lists", today, _slug(key) + ".csv"), data)
                man["lists"].setdefault("snapshots", {}).setdefault(today, {})[key] = {
                    "rows": len(rows), "sha256": sha256(data), "source_url": url}
                ok += 1
        if ok:
            man["lists"]["fetched"] = today
        for key, url in (("symbolchange", mem.SYMBOL_CHANGES_URL), ("equity_list", mem.EQUITY_LIST_URL)):
            status, data = http.get(url)
            if status == 200 and data and not is_html(data):
                atomic_write(os.path.join(base, "ref", key + ".csv.gz"), gz_bytes(data.decode("utf-8-sig", "replace")))
                man["ref"][key] = {"fetched": today, "bytes": len(data), "sha256": sha256(data), "source_url": url}
        write_json(man_path, man)
    return got


def _members_inputs(root):
    """(changes, lists {key: (date, [symbols])}, Symbols, names map, unresolved, announcements count)."""
    mem = history().members
    base = os.path.join(root, "members")
    man = read_json(os.path.join(base, "manifest.json"), None)
    if not man:
        raise ArchiveError("no index membership data in %s: python3 archive.py backfill --from 2010-01-01 "
                           "--datasets members" % root)
    changes, aliases = [], {}
    for name, e in sorted(man.get("announcements", {}).items(), key=lambda kv: kv[1].get("date", "")):
        try:
            with open(os.path.join(root, e["text"]), "rb") as fh:
                lines = gzip.decompress(fh.read()).decode("utf-8").split("\n")
        except (OSError, KeyError, EOFError):
            continue
        for c in mem.parse_announcement(lines, e.get("title", ""), e.get("date")):
            c["source"] = name
            changes.append(c)
        aliases.update(mem.parse_index_list(lines, e.get("title", ""), e.get("date"))[1])
    lists = {}
    snaps = sorted((man.get("lists") or {}).get("snapshots", {}).keys())
    for day in reversed(snaps):
        for key in mem.LIST_URLS:
            if key in lists:
                continue
            p = os.path.join(base, "lists", day, _slug(key) + ".csv")
            if os.path.exists(p):
                with open(p, encoding="utf-8-sig", errors="replace") as fh:
                    rows = mem.parse_constituents(fh.read())
                lists[key] = (day, rows)
    sym_changes = []
    p = os.path.join(base, "ref", "symbolchange.csv.gz")
    if os.path.exists(p):
        with open(p, "rb") as fh:
            sym_changes = mem.parse_symbol_changes(gzip.decompress(fh.read()).decode("utf-8", "replace"))
    symbols = mem.Symbols(sym_changes, aliases)
    names = {}
    p = os.path.join(base, "ref", "equity_list.csv.gz")
    if os.path.exists(p):
        with open(p, "rb") as fh:
            names.update(mem.parse_equity_list(gzip.decompress(fh.read()).decode("utf-8", "replace")))
    for day, rows in lists.values():
        for r in rows:
            names.setdefault(mem.norm_name(r["name"]), r["symbol"])
    for c in changes:                    # later announcements carry symbols: they name earlier ones too
        if c["symbol"]:
            names[mem.norm_name(c["name"])] = c["symbol"]
    unresolved = mem.resolve_names(changes, names)
    return changes, lists, symbols, unresolved, len(man.get("announcements", {}))


def membership(index, root=None):
    """The rebuilt history of one index: {"index", "snapshot", "intervals" [{"from", "to", "members"}],
    "issues", "unresolved", "first_announcement"}. Cached under .cache/ against the inputs' mtimes."""
    root = default_root(root)
    mem = history().members
    key = mem.index_key(index)
    if not key:
        raise ArchiveError("membership is rebuilt for NIFTY 50, NIFTY NEXT 50, NIFTY 100, NIFTY 200 and NIFTY 500, "
                           "not %r" % index)
    base = os.path.join(root, "members")
    stamp = os.path.getmtime(os.path.join(base, "manifest.json")) if os.path.exists(os.path.join(base, "manifest.json")) \
        else None
    ck = ("members", os.path.abspath(root), key)
    if _CACHE.get(ck, (None,))[0] == stamp and stamp is not None:
        return _CACHE[ck][1]
    changes, lists, symbols, unresolved, n_ann = _members_inputs(root)
    if key not in lists:
        raise ArchiveError("no current %s list in %s yet: run backfill --datasets members" % (key, root))
    day, rows = lists[key]
    got = mem.rebuild(key, [r["symbol"] for r in rows], day, changes, symbols)
    mine = [c for c in changes if c["index"] == key]
    out = {"index": key, "snapshot": day, "intervals": got["intervals"], "issues": got["issues"],
           "unresolved": [u for u in unresolved if u["index"] == key], "changes": len(mine),
           "first_change": min((c["effective"] for c in mine if c["effective"]), default=None),
           "announcements": n_ann, "_symbols": symbols}
    _CACHE[ck] = (stamp, out)
    return out


def members(index, day, root=None):
    """The constituents of NIFTY 50 / NIFTY NEXT 50 / NIFTY 100 / NIFTY 200 / NIFTY 500 on `day`, as the
    symbols they traded under that day (survivorship-free cross-sections). Before the oldest announcement
    the archive holds, the answer is only as good as the announcements: see membership(index)["issues"]."""
    m = membership(index, root)
    return history().members.members_on(m["intervals"], iso(parse_day(day)), m["_symbols"])


# -- continuous futures

FUT_KINDS = ("IDF", "STF")


def _fut_rows(root, underlying, start=None, end=None):
    """{date: [{"expiry", "open", "high", "low", "close", "settle", "oi", "volume"}]} of one underlying's
    futures from the F&O bhavcopies; scanned once, then remembered under .cache/fut/."""
    sym = underlying.upper()
    files = _files(root, "fo")
    cpath = os.path.join(root, ".cache", "fut", "%s.json.gz" % re.sub(r"[^A-Z0-9&-]", "_", sym))
    cache = _get_json(root, os.path.relpath(cpath, root), None) or {"scanned": {}, "rows": {}}
    todo = [(k, p) for k, p in files if cache["scanned"].get(k) != os.path.getsize(p)]
    prefix = sym + ","
    for k, p in todo:
        rows = []
        for l in _gz_lines(p)[1:]:
            if l.startswith(prefix):
                r = next(csv.reader([l]))
                if len(r) > 13 and r[1] in FUT_KINDS:
                    rows.append([r[2], _num(r[5]), _num(r[6]), _num(r[7]), _num(r[8]), _num(r[9]), _num(r[11]),
                                 _num(r[13])])
        if rows:
            cache["rows"][k] = rows
        else:
            cache["rows"].pop(k, None)
        cache["scanned"][k] = os.path.getsize(p)
    if todo:
        try:
            _put_json(root, os.path.relpath(cpath, root), cache)
        except OSError:
            pass
    a, b = (iso(start) if start else "0000"), (iso(end) if end else "9999")
    out = {}
    for k, rows in cache["rows"].items():
        if a <= k <= b:
            out[k] = [{"expiry": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "settle": r[5],
                       "oi": r[6], "volume": r[7]} for r in rows]
    return out


def load_continuous(underlying, method="back", start=None, end=None, roll="days:0", root=None):
    """A continuous futures series of NIFTY, BANKNIFTY or any F&O stock from the archived bhavcopies.

    method: "none" (unadjusted, with the roll log), "back" (back-adjusted by the roll gaps) or "ratio"
    (ratio-adjusted). roll: "days:N" (leave the near contract N sessions before its expiry), "oi" or
    "volume" (switch the day after the next contract's OI or volume passes the near one's). Returns
    {"underlying", "method", "rule", "bars": [{"date", "o", "h", "l", "c", "settle", "v", "oi", "expiry",
    "adj", "factor"}], "rolls": [{"date", "from", "to", "old", "new", "gap", "ratio", "why"}], "issues"}.
    Adjustments are computed over start..end, so the newest bar is always the real price."""
    root = default_root(root)
    days = _fut_rows(root, underlying, start, end)
    if not days:
        raise ArchiveError("no futures of %s in the F&O archive at %s (backfill --datasets fo)" % (underlying.upper(),
                                                                                                 root))
    last = max(days)
    tail = [iso(d) for d in candidate_days(parse_day(last) + datetime.timedelta(days=1),
                                           parse_day(last) + datetime.timedelta(days=120),
                                           Store(root).calendar()["closed"])]       # to count to a later expiry
    closed = Store(root).calendar()["closed"]
    sessions = sorted(set(trading_days(min(days), last, root)) |
                      set(iso(d) for d in candidate_days(min(days), last, closed))) + tail
    try:
        res = history().continuous.build(days, sessions, roll, method)
    except ValueError as e:
        raise ArchiveError(str(e))
    res["underlying"] = underlying.upper()
    return res


# -- option-chain snapshots as a table

def chain_series(underlying, day, expiry=None, root=None):
    """Every option-chain snapshot of one day as rows: [{"time", "expiry", "strike", "type", "ltp", "oi",
    "oi_change", "volume", "iv", "bid", "ask", "spot"}] sorted by time, expiry, strike, type."""
    out = []
    for snap in chain_snapshots(underlying, day, root):
        if expiry and snap["expiry"] != iso(parse_day(expiry)):
            continue
        doc = load_chain_snapshot(snap["path"]).get("data") or {}
        rec = doc.get("records") or {}
        spot = rec.get("underlyingValue")
        for row in rec.get("data") or []:
            for side in ("CE", "PE"):
                o = row.get(side)
                if not o:
                    continue
                out.append({"time": snap["time"], "expiry": snap["expiry"], "strike": o.get("strikePrice"),
                            "type": side, "ltp": o.get("lastPrice"), "oi": o.get("openInterest"),
                            "oi_change": o.get("changeinOpenInterest"), "volume": o.get("totalTradedVolume"),
                            "iv": o.get("impliedVolatility"), "bid": o.get("buyPrice1"), "ask": o.get("sellPrice1"),
                            "spot": o.get("underlyingValue", spot)})
    out.sort(key=lambda r: (r["time"], r["expiry"], r["strike"] or 0, r["type"]))
    return out


# -- expired options from a broker

OPTX_COLS = ["date", "time", "open", "high", "low", "close", "volume", "oi", "strike", "spot", "iv"]


def _write_optx(root, rel_dir, name, bars, meta, now):
    d = os.path.join(root, rel_dir)
    path = os.path.join(d, name + ".csv.gz")
    have = {}
    if os.path.exists(path):
        for r in read_gz_csv(path)[1]:
            have[(r[0], r[1])] = r
    for b in bars:
        have[(b["date"], b["time"])] = [b["date"], b["time"], _f(str(b["o"])), _f(str(b["h"])), _f(str(b["l"])),
                                        _f(str(b["c"])), str(int(b.get("v") or 0)),
                                        "" if b.get("oi") is None else str(b["oi"]),
                                        "" if b.get("strike") is None else _f(str(b["strike"])),
                                        "" if b.get("spot") is None else _f(str(b["spot"])),
                                        "" if b.get("iv") is None else _f(str(b["iv"]))]
    rows = [have[k] for k in sorted(have)]
    blob = gz_bytes(csv_text(OPTX_COLS, rows))
    atomic_write(path, blob)
    man_path = os.path.join(d, "manifest.json")
    man = read_json(man_path, None) or {"schema": SCHEMA, "files": {}}
    man["files"][name] = dict(meta, rows=len(rows), bytes=len(blob), sha256=sha256(blob), fetched_at=_stamp(now))
    write_json(man_path, man)
    return len(bars)


def expired_options_ingest(root, underlying, start, end, broker="upstox", adapter=None, interval="1m",
                           strikes=5, now=None, log=None, sleep=None):
    """Intraday candles of expired options, with your own broker token (Upstox Plus, or Dhan's Data API).
    Upstox: every expiry of the underlying in start..end, the `strikes` strikes either side of that expiry's
    settlement-day spot, calls and puts, into optx/upstox/<UNDERLYING>/<EXPIRY>/<STRIKE><CE|PE>_<interval>.
    Dhan: rolling ATM-relative strikes (ATM-strikes .. ATM+strikes) of the nearest weekly expiry, 30 days a
    call, into optx/dhan/<UNDERLYING>/WEEK1/<ATM+k>_<CE|PE>_<interval>. Returns {"files", "bars", "notes"}."""
    ex = history().expired
    now = now or ist_now()
    log = log or (lambda m: None)
    sleep = sleep or time.sleep
    start, end = parse_day(start), min(parse_day(end), now.date())
    sym = underlying.upper()
    if adapter is None:
        mod = broker_adapters()
        if mod is None:
            raise ArchiveError("expired options need the trader-broker skill next to trader-archive (its adapters/)")
        adapter = mod.make(broker)
    if not adapter.token():
        raise ArchiveError("%s: no access token in %s. Log in once: eval \"$(python3 trader-broker/ticket.py login "
                           "%s)\". Expired-option history needs %s." % (
                               adapter.title, adapter.env_names.get("access_token"), adapter.name,
                               "the Upstox Plus plan" if adapter.name == "upstox" else "Dhan's Data API plan"))
    out = {"files": 0, "bars": 0, "notes": []}
    if adapter.name == "upstox":
        key = UPSTOX_INDEX_KEYS.get(index_name(sym))
        if not key:
            isin = latest_isin(sym, root)
            if not isin:
                raise ArchiveError("no Upstox key for %s: backfill the equity archive first" % sym)
            key = "NSE_EQ|" + isin
        expiries = [e for e in ex.upstox_list(adapter.call("GET", "api", ex.upstox_expiries_path(key)))
                    if iso(start) <= str(e)[:10] <= iso(end)]
        for exp in sorted(expiries):
            exp = str(exp)[:10]
            rel = os.path.join("optx", "upstox", sym, exp)
            man = read_json(os.path.join(root, rel, "manifest.json"), None) or {}
            contracts = [ex.upstox_contract(c) for c in
                         ex.upstox_list(adapter.call("GET", "api", ex.upstox_contracts_path(key, exp)))]
            spot = None
            idx = load_index(sym, exp, exp, root) if sym in INDEX_SYMBOLS else load_daily(sym, exp, exp, False, root)
            if idx:
                spot = idx[0]["c"]
            for c in ex.near_strikes(contracts, spot, strikes):
                name = "%s%s_%s" % (_f(str(c["strike"])), c["type"], interval)
                if name in (man.get("files") or {}):
                    continue
                first = iso(max(start, parse_day(exp) - datetime.timedelta(days=95)))
                doc = adapter.call("GET", "api", ex.upstox_candles_path(c["key"], interval, first, exp))
                bars = ex.upstox_bars(doc, c["strike"])
                out["bars"] += _write_optx(root, rel, name, bars, {"instrument": c["key"], "broker": "upstox",
                                                                   "expiry": exp, "lot": c.get("lot")}, now)
                out["files"] += 1
                sleep(BROKER_PAUSE)
            log("%s %s: %d contracts near %s" % (sym, exp, len(contracts), spot))
    elif adapter.name == "dhan":
        for k in range(-strikes, strikes + 1):
            strike = "ATM" if k == 0 else "ATM%+d" % k
            for side, opt in (("CE", "CALL"), ("PE", "PUT")):
                rel = os.path.join("optx", "dhan", sym, "WEEK1")
                name = "%s_%s_%s" % (strike, side, interval)
                for a, b in ex.dhan_windows(iso(start), iso(end)):
                    doc = adapter.call("POST", "api", "/charts/rollingoption",
                                       json_body=ex.dhan_payload(sym, strike, opt, a, b, interval))
                    bars = ex.dhan_bars(doc, side.lower())
                    out["bars"] += _write_optx(root, rel, name, bars, {"broker": "dhan", "strike": strike,
                                                                       "expiry_flag": "WEEK", "expiry_code": 1}, now)
                    sleep(BROKER_PAUSE)
                out["files"] += 1
                log("%s %s %s: done" % (sym, strike, side))
    else:
        raise ArchiveError("expired-option candles are wired for upstox and dhan; Kite's history API does not "
                           "serve expired option contracts")
    return out


def load_option_intraday(underlying, expiry, strike, option_type, interval="1m", broker="upstox", root=None):
    """Bars of one expired option contract from the optx dataset (expired-options): [{"date", "time", "o",
    "h", "l", "c", "v", "oi"}]. For Dhan's rolling data pass expiry "WEEK1" and strike "ATM", "ATM+1" ..."""
    root = default_root(root)
    if broker == "dhan":
        p = os.path.join(root, "optx", "dhan", underlying.upper(), str(expiry), "%s_%s_%s.csv.gz" % (
            strike, option_type.upper(), interval))
    else:
        p = os.path.join(root, "optx", "upstox", underlying.upper(), iso(parse_day(expiry)), "%s%s_%s.csv.gz" % (
            _f(str(strike)), option_type.upper(), interval))
    if not os.path.exists(p):
        return []
    out = []
    for r in read_gz_csv(p)[1]:
        out.append({"date": r[0], "time": r[1], "o": float(r[2]), "h": float(r[3]), "l": float(r[4]), "c": float(r[5]),
                    "v": int(float(r[6] or 0)), "oi": int(float(r[7])) if r[7] else None,
                    "strike": _num(r[8]), "spot": _num(r[9]), "iv": _num(r[10])})
    return out


# -- capture-chains: a background loop that builds forward intraday option data

def next_open(now, closed=None):
    """The next 09:15 IST session start at or after `now` (a datetime)."""
    d = now.date()
    for _ in range(15):
        if session_kind(d, closed) == "session":
            t = datetime.datetime.combine(d, datetime.time(9, 15))
            if t >= now or (d == now.date() and now.hour * 60 + now.minute <= CLOSE_MIN):
                return max(t, now)
        d += datetime.timedelta(days=1)
    return None


def capture_chains(root, http, symbols, every=5, expiries=2, sessions=1, now_fn=None, sleep=None, log=None,
                   until="15:30"):
    """Take option-chain snapshots every `every` minutes through each session's hours, for `sessions`
    sessions (0: until stopped), sleeping between sessions. Writes a heartbeat to chain/capture.json after
    each round (pid, rounds, files, last and next times, last error) so a scheduler or the auto-pilot can
    see it is alive; a second copy on the same archive refuses to start. NSE blocking pauses the loop
    for 15 minutes instead of ending it. Returns the heartbeat."""
    now_fn = now_fn or ist_now
    sleep = sleep or time.sleep
    log = log or (lambda m: None)
    every = max(3, int(every))
    base = os.path.join(root, "chain")
    lock = os.path.join(base, ".capture.lock")
    beat_path = os.path.join(base, "capture.json")
    os.makedirs(base, exist_ok=True)
    other = read_json(lock, None)
    if other and other.get("pid") != os.getpid() and _alive(other.get("pid")):
        raise ArchiveError("another capture-chains (pid %s) is already writing to %s" % (other.get("pid"), base))
    write_json(lock, {"pid": os.getpid(), "started": _stamp(now_fn())})
    closed = Store(root).calendar()["closed"]
    beat = {"schema": SCHEMA + "/capture", "pid": os.getpid(), "symbols": [s.upper() for s in symbols],
            "every_minutes": every, "expiries": expiries, "rounds": 0, "files": 0, "sessions_done": 0,
            "started": _stamp(now_fn()), "last": None, "next": None, "last_error": None, "state": "starting"}
    in_day = None
    try:
        while True:
            now = now_fn()
            if market_open(now, closed) and now.strftime("%H:%M") <= until:
                in_day = now.date()
                try:
                    beat["files"] += snapshot_chain(root, http, symbols, expiries, now, log)
                    beat["last_error"] = None
                    wait = every * 60.0
                except Blocked as e:
                    beat["last_error"] = str(e)
                    http.blocks = 0
                    wait = 15 * 60.0
                beat["rounds"] += 1
                beat["last"] = _stamp(now)
                beat["state"] = "capturing"
                beat["next"] = _stamp(now + datetime.timedelta(seconds=wait))
                write_json(beat_path, beat)
                sleep(wait)
                continue
            if in_day is not None:
                beat["sessions_done"] += 1
                in_day = None
            if sessions and beat["sessions_done"] >= sessions:
                beat["state"] = "done"
                break
            nxt = next_open(now + datetime.timedelta(minutes=1), closed)
            if nxt is None:
                beat["state"] = "no session in the next two weeks"
                break
            beat["state"] = "waiting for the session"
            beat["next"] = _stamp(nxt)
            write_json(beat_path, beat)
            sleep(min(600.0, max(30.0, (nxt - now).total_seconds())))
    finally:
        beat["stopped"] = _stamp(now_fn())
        if beat["state"] not in ("done",):
            beat["state"] = beat["state"] if beat["state"].startswith("no session") else "stopped"
        write_json(beat_path, beat)
        try:
            os.remove(lock)
        except OSError:
            pass
    return beat


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


# ---------------------------------------------------------------- status, verify, quality

def long_status(root):
    """What the long-history datasets hold (indexhist, results, members)."""
    out = {}
    base = os.path.join(root, "indexhist")
    for slug in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        man = read_json(os.path.join(base, slug, "manifest.json"), {}) or {}
        keys = sorted(man.get("chunks", {}))
        if keys:
            out.setdefault("indexhist", {})[man.get("index", slug)] = {"first": keys[0][:10], "last": keys[-1][-10:]}
    man = read_json(os.path.join(root, "results", "manifest.json"), None)
    if man and man.get("months"):
        ms = sorted(man["months"])
        out["results"] = {"months": len(ms), "first": ms[0], "last": ms[-1],
                          "filings": sum((v.get("filings") or {}).get("rows", 0) for v in man["months"].values())}
    man = read_json(os.path.join(root, "members", "manifest.json"), None)
    if man:
        ds = sorted(e.get("date", "") for e in man.get("announcements", {}).values())
        snaps = sorted((man.get("lists") or {}).get("snapshots", {}))
        out["members"] = {"announcements": len(ds), "first": ds[0] if ds else None, "last": ds[-1] if ds else None,
                          "lists": snaps[-1] if snaps else None}
    return out


def dir_bytes(path):
    total = 0
    for base, dirs, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(base, f))
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def status(root):
    st = Store(root)
    out = {"schema": SCHEMA, "root": root, "datasets": {}, "bytes": dir_bytes(root) if os.path.isdir(root) else 0,
           "closed_days": len(st.calendar()["closed"])}
    for ds in DATASETS:
        info = {"files": 0, "rows": 0, "bytes": 0, "first": None, "last": None, "fetched_at": None, "missing": 0}
        for y in st.years(ds):
            man = st.manifest(ds, y)
            for k, e in man["files"].items():
                info["files"] += 1
                info["rows"] += e.get("rows", 0)
                info["bytes"] += e.get("bytes", 0)
                if info["fetched_at"] is None or e.get("fetched_at", "") > info["fetched_at"]:
                    info["fetched_at"] = e.get("fetched_at")
            info["missing"] += len(man["missing"])
            for k in man.get("chunks", {}):
                a, b = k.split("..")
                info["first"] = min(info["first"] or a, a)
                info["last"] = max(info["last"] or b, b)
        if ds in DAILY or ds == "lots":
            dates = st.dates(ds)
            if dates:
                info["first"], info["last"] = dates[0], dates[-1]
        elif ds == "ca" and info["files"]:
            ys = st.years(ds)
            info["first"], info["last"] = ys[0], ys[-1]
        out["datasets"][ds] = info
    intr = {}
    base = os.path.join(root, "intraday")
    for iv in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        for sym in sorted(os.listdir(os.path.join(base, iv))):
            man = read_json(os.path.join(base, iv, sym, "manifest.json"), {}) or {}
            months = sorted((man.get("months") or {}).keys())
            intr.setdefault(iv, {})[sym] = {"months": len(months), "first": months[0] if months else None,
                                            "last": months[-1] if months else None,
                                            "rows": sum(m.get("rows", 0) for m in (man.get("months") or {}).values())}
    out["intraday"] = intr
    chain = os.path.join(root, "chain")
    out["chain_snapshots"] = sum(len(f) for _, _, f in os.walk(chain)) if os.path.isdir(chain) else 0
    out["long"] = long_status(root)
    return out


def verify(root, now=None, deep=True, intraday=True):
    """Checks the archive against its manifests and the trading calendar. Returns a report dict whose
    "problems" count is 0 when everything holds (warnings do not count)."""
    now = now or ist_now()
    st = Store(root)
    rep = {"schema": SCHEMA, "root": root, "checked_files": 0, "checksum_mismatch": [], "missing_files": [],
           "unlisted_files": [], "row_count_mismatch": [], "duplicates": [], "gaps": {}, "ohlc_errors": [],
           "outliers": [], "ca_checks": [], "short_sessions": [], "survivorship": {}, "intraday": [], "notes": []}
    closed = st.calendar()["closed"]
    ca = {}
    for e in _all_ca(root):
        ca.setdefault((e["symbol"], e["ex_date"]), []).append(e)
    for ds in DAILY + ("lots",):
        listed = set()
        for y in st.years(ds):
            man = st.manifest(ds, y)
            for k, e in sorted(man["files"].items()):
                p = st.path(ds, k)
                listed.add(k)
                if not os.path.exists(p):
                    rep["missing_files"].append("%s/%s" % (ds, k))
                    continue
                with open(p, "rb") as fh:
                    blob = fh.read()
                rep["checked_files"] += 1
                if sha256(blob) != e.get("sha256"):
                    rep["checksum_mismatch"].append("%s/%s" % (ds, k))
                    continue
                if not deep:
                    continue
                rows = _rows(gzip.decompress(blob).decode("utf-8"))[1:]
                if len(rows) != e.get("rows"):
                    rep["row_count_mismatch"].append("%s/%s: %d rows, manifest says %s" % (ds, k, len(rows),
                                                                                          e.get("rows")))
                seen, dup = set(), 0
                for r in rows:
                    key = tuple(r[i] for i in KEYS[ds] if i < len(r))
                    if key in seen:
                        dup += 1
                    seen.add(key)
                if dup:
                    rep["duplicates"].append("%s/%s: %d duplicate row%s" % (ds, k, dup, "" if dup == 1 else "s"))
                if ds == "eq":
                    _check_eq_day(rep, k, rows, ca)
        for k in st.dates(ds):
            if k not in listed:
                rep["unlisted_files"].append("%s/%s" % (ds, k))
    # gaps against the calendar, within what the archive covers
    eq_dates = st.dates("eq")
    span = eq_dates or st.dates("fo") or st.dates("index")
    if span:
        expected = set(iso(d) for d in candidate_days(span[0], span[-1], closed))
        expected = {d for d in expected if (now.date() - parse_day(d)).days > NOT_PUBLISHED_DAYS or d in span}
        for ds in DAILY:
            have = set(st.dates(ds))
            if not have:
                continue
            first = min(have)
            missing = sorted(d for d in expected if d >= first and d not in have
                             and not (st.missing(ds, d) and ds in ("ban", "settle")))
            if missing:
                rep["gaps"][ds] = missing
        rep["short_sessions"] = [{"date": d, "why": SHORT_SESSIONS[d]} for d in span if d in SHORT_SESSIONS]
    if deep and len(eq_dates) >= 2:
        first = set(_eq_symbols(st.path("eq", eq_dates[0])))
        last = set(_eq_symbols(st.path("eq", eq_dates[-1])))
        gone = sorted(first - last)
        rep["survivorship"] = {
            "from": eq_dates[0], "to": eq_dates[-1], "listed_then": len(first), "listed_now": len(last),
            "gone": len(gone), "gone_sample": gone[:15], "new": len(last - first),
            "note": ("%d EQ-series stocks traded on %s but not on %s (delisted, merged, renamed or moved to "
                     "another series). The archive keeps their history; a backtest that picks today's "
                     "stocks only is biased towards survivors." % (len(gone), eq_dates[0], eq_dates[-1]))}
    if deep:
        rep["adjustment_span"] = adjustment_span(root)
        rep["membership"] = membership_report(root)
        rep["rolls"] = roll_report(root)
        rep["results"] = results_report(root)
        if rep["survivorship"] and rep["membership"]:
            rep["survivorship"]["note"] += (" Point-in-time index membership is on file: pick a backtest's universe "
                                            "with members(index, date) and the survivors problem goes away for "
                                            "index-based universes (see the membership lines for its gaps).")
    if intraday:
        base = os.path.join(root, "intraday")
        for iv in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            for sym in sorted(os.listdir(os.path.join(base, iv))):
                g = intraday_gaps(root, sym, iv)
                if g["days"]:
                    rep["intraday"].append(g)
    rep["problems"] = (len(rep["checksum_mismatch"]) + len(rep["missing_files"]) + len(rep["row_count_mismatch"])
                       + len(rep["duplicates"]) + sum(len(v) for v in rep["gaps"].values())
                       + len(rep["ohlc_errors"]))
    rep["warnings"] = (len(rep["unlisted_files"]) + len(rep["outliers"]) +
                       len([c for c in rep["ca_checks"] if not c["ok"]]) +
                       len((rep.get("adjustment_span") or {}).get("bad", [])) +
                       sum(v.get("wrong_size", 0) + v.get("unresolved", 0) for v in (rep.get("membership") or {}).values()) +
                       sum(v.get("big_gap_count", 0) for v in (rep.get("rolls") or {}).values()) +
                       len((rep.get("results") or {}).get("missing_months", [])) +
                       sum(len(g["missing_days"]) + len(g["short_days"]) for g in rep["intraday"]))
    return rep


def membership_report(root, check_trading=True):
    """Per tracked index: changes on file, the size of every rebuilt composition against the index's size,
    names no symbol was found for, and members that did not trade on the first day of their composition."""
    root = default_root(root)
    if not os.path.exists(os.path.join(root, "members", "manifest.json")):
        return {}
    out = {}
    eq_days = set(Store(root).dates("eq"))
    for key in sorted(history().members.SIZES):
        try:
            m = membership(key, root)
        except ArchiveError as e:
            out[key] = {"error": str(e)}
            continue
        size = history().members.SIZES[key]
        off = [i for i in m["issues"] if i["kind"] == "size"]
        rec = {"snapshot": m["snapshot"], "changes": m["changes"], "first_change": m["first_change"],
               "compositions": len(m["intervals"]), "wrong_size": len(off),
               "wrong_size_sample": off[:6], "unresolved": len(m["unresolved"]),
               "unresolved_sample": m["unresolved"][:6],
               "consistency": [i for i in m["issues"] if i["kind"] != "size"][:8],
               "consistency_count": len([i for i in m["issues"] if i["kind"] != "size"]), "expected_size": size}
        if check_trading and eq_days:
            missing = []
            for iv in m["intervals"]:
                d = iv["from"]
                if not d or d not in eq_days:
                    continue
                traded = set(_eq_symbols(Store(root).path("eq", d)) + _eq_symbols(Store(root).path("eq", d), "BE"))
                gone = [s for s in members(key, d, root) if s not in traded]
                if gone:
                    missing.append({"date": d, "symbols": gone[:10], "count": len(gone)})
            rec["not_trading"] = missing[:8]
            rec["not_trading_count"] = sum(x["count"] for x in missing)
        out[key] = rec
    return out


def print_membership(rep):
    if not rep:
        print("No index membership data yet: backfill --datasets members.")
        return
    for key, r in rep.items():
        if r.get("error"):
            print("%s: %s" % (key, r["error"]))
            continue
        print("%s: %d changes back to %s, %d compositions, %d not %d members, %d names without a symbol, "
              "%d inconsistencies%s." % (key, r["changes"], r["first_change"] or "-", r["compositions"],
                                         r["wrong_size"], r["expected_size"], r["unresolved"], r["consistency_count"],
                                         (", %d member-days not trading" % r["not_trading_count"])
                                         if r.get("not_trading_count") else ""))
        for w in r["wrong_size_sample"][:3]:
            print("  %s to %s: %d members" % (w["date"], w["until"] or "now", w["count"]))
        for u in r["unresolved_sample"][:3]:
            print("  no symbol for %r (%s %s)" % (u["name"], u["action"], u["effective"]))


def adjustment_span(root):
    """Every split and bonus inside the equity archive's span, checked against the closes of the session
    before the ex-date and the ex-date itself (the archive's own closes, not NSE's previous-close field)."""
    st = Store(root)
    dates = st.dates("eq")
    if len(dates) < 2:
        return {"events": 0, "checked": 0, "ok": 0, "bad": [], "no_prices": 0}
    pos = {d: i for i, d in enumerate(dates)}
    closed = st.calendar()["closed"]
    by_day = {}
    for e in _all_ca(root):
        if dates[0] < e["ex_date"] <= dates[-1]:
            by_day.setdefault(e["ex_date"], []).append(e)
    rep = {"events": sum(len(v) for v in by_day.values()), "checked": 0, "ok": 0, "bad": [], "no_prices": 0}
    for day in sorted(by_day):
        ex = day if day in pos else next((d for d in dates if d > day), None)
        if ex is None or pos[ex] == 0:
            continue
        prev = dates[pos[ex] - 1]
        if len(candidate_days(prev, ex, closed)) > 2 or ex != day:
            rep["no_prices"] += len(by_day[day])       # the archive has a gap there: nothing to compare
            continue
        want = set(e["symbol"] for e in by_day[day])
        closes = {}
        for k in (prev, ex):
            try:
                lines = _gz_lines(st.path("eq", k))[1:]
            except (OSError, EOFError, ValueError):
                lines = []          # a damaged file: its checksum already says so
            for l in lines:
                sym = l.split(",", 1)[0]
                if sym in want:
                    r = next(csv.reader([l]))
                    if r[1] in SERIES_PREF:
                        closes.setdefault((sym, k), _num(r[6]))
        per = {}
        for e in by_day[day]:
            per.setdefault(e["symbol"], []).append(e)
        for sym, evs in per.items():
            a, b = closes.get((sym, prev)), closes.get((sym, ex))
            if not a or not b:
                rep["no_prices"] += 1
                continue
            f = 1.0
            for e in evs:
                f *= e["factor"]
            move = 100.0 * (b / (a * f) - 1)
            rep["checked"] += 1
            if abs(move) <= OUTLIER_PCT:
                rep["ok"] += 1
            else:
                rep["bad"].append({"symbol": sym, "ex_date": day, "factor": round(f, 6), "prev_close": a, "close": b,
                                   "adjusted_move_pct": round(move, 2), "subject": "; ".join(e["subject"] for e in evs)})
    return rep


def roll_report(root, underlyings=("NIFTY", "BANKNIFTY"), rule="days:0", big_pct=2.0):
    """Rolls of the main index futures: how many, the biggest gaps, and days the chain was broken."""
    out = {}
    if not Store(root).dates("fo"):
        return out
    for u in underlyings:
        try:
            res = load_continuous(u, "none", None, None, rule, root)
        except ArchiveError:
            continue
        big = [r for r in res["rolls"] if r["ratio"] and abs(r["ratio"] - 1) * 100 > big_pct]
        out[u] = {"sessions": len(res["bars"]), "rolls": len(res["rolls"]),
                  "first": res["bars"][0]["date"] if res["bars"] else None,
                  "big_gaps": [{"date": r["date"], "from": r["from"], "to": r["to"], "gap_pct": round(100 * (r["ratio"] - 1), 2)}
                               for r in big][:8], "big_gap_count": len(big),
                  "issues": len(res["issues"]), "issue_sample": res["issues"][:5]}
    return out


def results_report(root):
    man = read_json(os.path.join(root, "results", "manifest.json"), None)
    if not man:
        return {}
    months = sorted(man.get("months", {}))
    have = set(months)
    gaps = []
    if months:
        for a, _ in month_windows(parse_day(months[0] + "-01"), parse_day(months[-1] + "-01")):
            if iso(a)[:7] not in have:
                gaps.append(iso(a)[:7])
    rows = sum((m.get("filings") or {}).get("rows", 0) for m in man["months"].values())
    return {"months": len(months), "first": months[0] if months else None, "last": months[-1] if months else None,
            "missing_months": gaps, "filings": rows,
            "incomplete": [k for k, v in man["months"].items() if not v.get("complete")]}


def _eq_symbols(path, series="EQ"):
    try:
        return [r[0] for r in read_gz_csv(path)[1] if len(r) > 1 and r[1] == series]
    except (OSError, EOFError, ValueError):
        return []         # a damaged file: already reported by its checksum


def _all_ca(root):
    adjustment_factors("", root)      # loads the cache
    return [e for evs in _CACHE.get(("ca", os.path.abspath(root)), (None, {}))[1].values() for e in evs]


def _check_eq_day(rep, day, rows, ca):
    """OHLC sanity, day moves beyond OUTLIER_PCT (explained by a split or bonus, or not), per stock."""
    for r in rows:
        if r[1] not in SERIES_PREF:
            continue
        o, h, l, c, pc, v = (_num(r[i]) for i in (3, 4, 5, 6, 8, 9))
        if not v or c is None:
            continue
        if None in (o, h, l) or h < l - 1e-9 or not (l - 1e-9 <= c <= h + 1e-9) or not (l - 1e-9 <= o <= h + 1e-9) \
                or min(o, h, l, c) <= 0:
            rep["ohlc_errors"].append("eq/%s %s: open %s high %s low %s close %s" % (day, r[0], r[3], r[4], r[5], r[6]))
            continue
        if not pc:
            continue
        move = 100.0 * (c / pc - 1)
        ev = ca.get((r[0], day))
        if ev:
            f = 1.0
            for e in ev:
                f *= e["factor"]
            adj_move = 100.0 * (c / (pc * f) - 1)
            rep["ca_checks"].append({"date": day, "symbol": r[0], "factor": round(f, 6),
                                     "raw_move_pct": round(move, 2), "adjusted_move_pct": round(adj_move, 2),
                                     "ok": abs(adj_move) <= OUTLIER_PCT,
                                     "subject": "; ".join(e["subject"] for e in ev)})
        elif abs(move) > OUTLIER_PCT:
            rep["outliers"].append({"date": day, "symbol": r[0], "series": r[1], "prev_close": pc, "close": c,
                                    "move_pct": round(move, 2),
                                    "why": "no split or bonus on file: check for a demerger, rights issue, "
                                           "relisting or a missing corporate action"})


# ---------------------------------------------------------------- the commands

def _log(a):
    if getattr(a, "json", False):
        return lambda m: print(m, file=sys.stderr)
    return lambda m: print(m)


def _datasets(text):
    if not text:
        return list(DEFAULT_DATASETS)
    if text.strip().lower() == "all":
        return list(DATASETS)
    if text.strip().lower() == "long":
        return list(LONG_DATASETS)
    out = [d.strip().lower() for d in text.split(",") if d.strip()]
    bad = [d for d in out if d not in DATASETS]
    if bad:
        raise ArchiveError("unknown dataset %s: choose from %s" % (", ".join(bad), ",".join(DATASETS)))
    return out


def _now(a):
    if getattr(a, "now", None):
        try:
            return datetime.datetime.fromisoformat(a.now)
        except ValueError:
            raise ArchiveError("--now wants \"YYYY-MM-DD HH:MM\" (IST)")
    return ist_now()


def print_summary(s, root):
    files = ", ".join("%s %d" % (k, v) for k, v in sorted(s["files"].items()))
    rows = ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in sorted(s["rows"].items()) if v)
    print("Archived %s to %s: %s%s." % (s["from"], s["to"], (files + " files") if files else "no new daily files",
                                       (" (" + rows + " rows)") if rows else ""))
    print("%d NSE call%s, %s downloaded, %.0f s." % (s["calls"], "" if s["calls"] == 1 else "s",
                                                    human(s["downloaded_bytes"]), s["seconds"]))
    if s["closed"]:
        print("No session on %d weekday%s (holidays, now remembered): %s" % (
            len(s["closed"]), "" if len(s["closed"]) == 1 else "s", ", ".join(sorted(s["closed"])[:12])))
    if s["not_published"]:
        print("Not published yet: %s (the next update asks again)." % ", ".join(sorted(set(s["not_published"]))))
    for e in s["errors"][:10]:
        print("Problem: %s" % e)
    print("Store: %s (%s on disk). Check it: python3 archive.py verify" % (root, human(dir_bytes(root))))


def cmd_backfill(a, http=None):
    now = _now(a)
    root = default_root(a.root, a.workdir)
    if a.start:
        start = parse_day(a.start, "--from")
    else:
        start = now.date() - datetime.timedelta(days=int(round(365.25 * a.years)))
    end = parse_day(a.end, "--to") if a.end else now.date()
    http = http or Http(pause=a.pause)
    days = len(candidate_days(start, min(end, now.date())))
    ds = _datasets(a.datasets)
    if not a.json:
        per = sum(1 for d in ds if d in DAILY)
        print("Backfilling %s to %s into %s: up to %d sessions x %d daily datasets, %.1f s between NSE calls "
              "(about %d minutes if nothing is there yet). Stop any time; the next run resumes."
              % (iso(start), iso(end), root, days, per, http.pause, max(1, days * per * (http.pause + 0.4) / 60)))
    try:
        s = backfill(Store(root), http, start, end, ds, now, _log(a), a.max_days, getattr(a, "indices", None))
    except Blocked as e:
        print("archive: %s" % e, file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(s, indent=1))
    else:
        print_summary(s, root)
    return 1 if s["errors"] else 0


def cmd_update(a, http=None):
    now = _now(a)
    root = default_root(a.root, a.workdir)
    st = Store(root)
    ds = _datasets(a.datasets)
    last = last_archived(st, ds)
    if not last:
        raise ArchiveError("the archive at %s is empty: start with python3 archive.py backfill --years 1" % root)
    a.start, a.end, a.years = iso(parse_day(last) + datetime.timedelta(days=1)), None, 0
    if parse_day(a.start) > now.date():
        a.start = iso(now.date())
    return cmd_backfill(a, http)


def cmd_status(a):
    root = default_root(a.root, a.workdir)
    s = status(root)
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    if not s["bytes"]:
        print("No archive at %s yet. Start one: python3 archive.py backfill --years 1" % root)
        return 0
    print("Archive %s: %s on disk." % (root, human(s["bytes"])))
    print("%-7s %7s %12s %10s  %-10s  %-10s  %s" % ("dataset", "files", "rows", "size", "first", "last", "fetched"))
    for ds, i in s["datasets"].items():
        if not i["files"]:
            continue
        print("%-7s %7s %12s %10s  %-10s  %-10s  %s" % (ds, "{:,}".format(i["files"]), "{:,}".format(i["rows"]),
                                                       human(i["bytes"]), i["first"] or "-", i["last"] or "-",
                                                       (i["fetched_at"] or "-")[:16].replace("T", " ")))
    for iv, syms in sorted(s["intraday"].items()):
        print("intraday %s: %d symbol%s (%s)" % (iv, len(syms), "" if len(syms) == 1 else "s", ", ".join(
            "%s %s..%s" % (k, v["first"], v["last"]) for k, v in sorted(syms.items())[:8])))
    if s["chain_snapshots"]:
        print("option-chain snapshots: %d" % s["chain_snapshots"])
    lg = s.get("long") or {}
    if lg.get("indexhist"):
        print("index history before 2012-02-21: %d indices (%s)" % (len(lg["indexhist"]), ", ".join(
            "%s %s..%s" % (k, v["first"], v["last"]) for k, v in sorted(lg["indexhist"].items())[:4])))
    if lg.get("results"):
        r = lg["results"]
        print("results dates: %d months %s to %s, %s filings" % (r["months"], r["first"], r["last"],
                                                                 "{:,}".format(r["filings"])))
    if lg.get("members"):
        m = lg["members"]
        print("index announcements: %d (%s to %s); constituent lists of %s" % (m["announcements"], m["first"],
                                                                               m["last"], m["lists"] or "-"))
    print("Weekdays NSE had no session (learned): %d. Next: python3 archive.py update" % s["closed_days"])
    return 0


def print_verify(rep):
    print("Checked %d files in %s." % (rep["checked_files"], rep["root"]))
    for key, title in (("missing_files", "listed in a manifest but gone"), ("checksum_mismatch", "checksum differs"),
                       ("row_count_mismatch", "row count differs"), ("duplicates", "duplicate rows"),
                       ("unlisted_files", "on disk but in no manifest (an interrupted write; the next run "
                                          "fetches them again)")):
        if rep[key]:
            print("%s: %d. %s" % (title[0].upper() + title[1:], len(rep[key]), "; ".join(rep[key][:8])))
    for ds, days in sorted(rep["gaps"].items()):
        print("Gap in %s: %d trading day%s missing: %s%s" % (ds, len(days), "" if len(days) == 1 else "s",
                                                            ", ".join(days[:10]), " ..." if len(days) > 10 else ""))
    if rep["ohlc_errors"]:
        print("OHLC errors: %d. %s" % (len(rep["ohlc_errors"]), "; ".join(rep["ohlc_errors"][:5])))
    bad_ca = [c for c in rep["ca_checks"] if not c["ok"]]
    if rep["ca_checks"]:
        print("Splits and bonuses checked against the prices: %d, %d look wrong%s" % (
            len(rep["ca_checks"]), len(bad_ca), (": " + "; ".join("%s %s factor %s moves %.1f%%" % (
                c["symbol"], c["date"], c["factor"], c["adjusted_move_pct"]) for c in bad_ca[:5])) if bad_ca else "."))
    if rep["outliers"]:
        print("Unexplained moves over %.0f%%: %d (%s)" % (OUTLIER_PCT, len(rep["outliers"]), "; ".join(
            "%s %s %+.1f%%" % (o["symbol"], o["date"], o["move_pct"]) for o in rep["outliers"][:8])))
    for s in rep["short_sessions"]:
        print("Short session %s: %s (its bars are not a full day)." % (s["date"], s["why"]))
    for g in rep["intraday"]:
        if g["missing_days"] or g["short_days"]:
            print("Intraday %s %s: %d day%s, %d missing, %d with fewer bars than a full session." % (
                g["symbol"], g["interval"], g["days"], "" if g["days"] == 1 else "s", len(g["missing_days"]),
                len(g["short_days"])))
    span = rep.get("adjustment_span") or {}
    if span.get("checked"):
        print("Splits and bonuses across the whole span, checked against the closes either side of the ex-date: "
              "%d of %d agree%s" % (span["ok"], span["checked"], (": " + "; ".join(
                  "%s %s factor %s moves %.1f%%" % (b["symbol"], b["ex_date"], b["factor"], b["adjusted_move_pct"])
                  for b in span["bad"][:5])) if span["bad"] else "."))
    if rep.get("membership"):
        print_membership(rep["membership"])
    for u, r in sorted((rep.get("rolls") or {}).items()):
        print("Continuous %s futures: %d sessions from %s, %d rolls, %d gaps over 2%%, %d chain issues." % (
            u, r["sessions"], r["first"], r["rolls"], r["big_gap_count"], r["issues"]))
    res = rep.get("results") or {}
    if res.get("months"):
        print("Results dates: %d months %s to %s, %s filings%s." % (
            res["months"], res["first"], res["last"], "{:,}".format(res["filings"]),
            (", missing months: " + ", ".join(res["missing_months"][:6])) if res["missing_months"] else ""))
    if rep["survivorship"]:
        print("Survivorship: %s" % rep["survivorship"]["note"])
    print("%s: %d problem%s, %d warning%s." % ("OK" if not rep["problems"] else "NOT OK", rep["problems"],
                                               "" if rep["problems"] == 1 else "s", rep["warnings"],
                                               "" if rep["warnings"] == 1 else "s"))


def cmd_verify(a):
    rep = verify(default_root(a.root, a.workdir), _now(a), deep=not a.quick)
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print_verify(rep)
    return 1 if rep["problems"] else 0


def cmd_export(a):
    root = default_root(a.root, a.workdir)
    sym = a.symbol.upper()
    if a.interval == "1d":
        name = index_name(sym)
        if name == "India VIX":
            bars = load_vix(a.start, a.end, root)
        elif name != sym or sym.startswith("^"):
            bars = load_index(sym, a.start, a.end, root)
        else:
            bars = load_daily(sym, a.start, a.end, adjusted=not a.raw, root=root)
    else:
        bars = load_intraday(sym, a.interval, a.start, a.end, root)
    if not bars:
        raise ArchiveError("no %s bars for %s in %s" % (a.interval, sym, root))
    lines = [LAB_HEAD] + ["%s,%s,%s,%s,%s,%s,%d" % (b["date"], b.get("time", ""), _f(str(b["o"])), _f(str(b["h"])),
                                                   _f(str(b["l"])), _f(str(b["c"])), int(b.get("v") or 0)) for b in bars]
    text = "\n".join(lines) + "\n"
    if a.out:
        if a.out.endswith(".gz"):
            atomic_write(a.out, gz_bytes(text))
        else:
            atomic_write(a.out, text.encode("utf-8"))
        print("Wrote %d %s bars of %s (%s to %s, %s) to %s." % (len(bars), a.interval, sym, bars[0]["date"],
                                                                bars[-1]["date"],
                                                                "raw" if a.raw else "adjusted", a.out),
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_intraday(a):
    now = _now(a)
    root = default_root(a.root, a.workdir)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    start = parse_day(a.start, "--from") if a.start else now.date() - datetime.timedelta(days=30)
    end = parse_day(a.end, "--to") if a.end else now.date()
    res, notes = intraday_ingest(root, syms, a.interval, start, end, a.broker, now=now, log=_log(a),
                                 use_broker=not a.no_broker, use_yahoo=a.yahoo)
    gaps = {s: intraday_gaps(root, s, a.interval, start, end) for s in syms}
    if a.json:
        print(json.dumps({"results": res, "notes": notes, "gaps": gaps}, indent=1))
        return 0
    for n in notes:
        print("Note: %s" % n)
    for s in syms:
        r, g = res[s], gaps[s]
        print("%s %s: %d broker bars, %d Yahoo bars, %d new, %d slots where the broker and Yahoo disagree by over "
              "0.5%%; %d months already complete." % (s, a.interval, r["broker_bars"], r["yahoo_bars"], r["added"],
                                                       r["conflicts"], r["months_skipped"]))
        if g["days"]:
            print("  %d sessions %s to %s; %d missing, %d short%s" % (
                g["days"], g["from"], g["to"], len(g["missing_days"]), len(g["short_days"]),
                (": " + ", ".join(g["missing_days"][:8])) if g["missing_days"] else ""))
    return 0


def cmd_snapshot(a, http=None, sleep=None):
    root = default_root(a.root, a.workdir)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    http = http or Http(pause=a.pause)
    sleep = sleep or time.sleep
    closed = Store(root).calendar()["closed"]
    until = a.until or "15:30"
    total = 0
    while True:
        now = _now(a) if a.now else ist_now()
        if not (a.force or market_open(now, closed)):
            if total == 0:
                print("NSE is not in session now (%s IST); snapshots are taken 09:15 to 15:30 on trading days. "
                      "--force takes one anyway." % now.strftime("%a %Y-%m-%d %H:%M"))
            break
        total += snapshot_chain(root, http, syms, a.expiries, now, _log(a))
        if not a.every or now.strftime("%H:%M") >= until or a.now:
            break
        sleep(max(60, a.every * 60))
    print("Option-chain snapshots written: %d (under %s/chain)." % (total, root))
    return 0


def cmd_continuous(a):
    root = default_root(a.root, a.workdir)
    res = load_continuous(a.underlying, a.method, a.start, a.end, a.roll, root)
    bars = res["bars"]
    if a.out:
        lines = ["date,open,high,low,close,settle,volume,oi,expiry,adj,factor"] + [
            "%s,%s,%s,%s,%s,%s,%d,%s,%s,%s,%s" % (b["date"], _f(str(b["o"])), _f(str(b["h"])), _f(str(b["l"])),
                                                 _f(str(b["c"])), _f(str(b.get("settle") or "")), int(b.get("v") or 0),
                                                 _f(str(b.get("oi") or "")), b["expiry"], _f(str(b["adj"])),
                                                 _f(str(round(b["factor"], 8)))) for b in bars]
        text = "\n".join(lines) + "\n"
        atomic_write(a.out, gz_bytes(text) if a.out.endswith(".gz") else text.encode("utf-8"))
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    if not bars:
        print("No bars for %s in that range." % res["underlying"])
        return 1
    print("%s continuous futures, %s, roll %s: %d sessions %s to %s, %d rolls." % (
        res["underlying"], {"none": "unadjusted", "back": "back-adjusted", "ratio": "ratio-adjusted"}[res["method"]],
        res["rule"], len(bars), bars[0]["date"], bars[-1]["date"], len(res["rolls"])))
    for r in res["rolls"][-a.show:]:
        print("  roll %s  %s -> %s  old %s new %s  gap %s (%s)" % (
            r["date"], r["from"], r["to"], r["old"], r["new"],
            "-" if r["gap"] is None else "%+.2f, %+.2f%%" % (r["gap"], 100 * (r["ratio"] - 1)), r["why"]))
    if res["issues"]:
        kinds = {}
        for i in res["issues"]:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        print("Issues: " + "; ".join("%s: %d" % kv for kv in sorted(kinds.items())))
    if a.out:
        print("Wrote %d bars to %s." % (len(bars), a.out))
    return 0


def cmd_members(a):
    root = default_root(a.root, a.workdir)
    if a.report:
        rep = membership_report(root)
        if a.json:
            print(json.dumps(rep, indent=1))
            return 0
        print_membership(rep)
        return 0
    if not a.index:
        raise ArchiveError("members wants --index (NIFTY50, NIFTYNXT50, NIFTY100, NIFTY200, NIFTY500) or --report")
    day = a.date or iso(ist_now().date())
    syms = members(a.index, day, root)
    if a.json:
        print(json.dumps({"index": a.index, "date": day, "members": syms}, indent=1))
        return 0
    m = membership(a.index, root)
    print("%s on %s: %d members (rebuilt from %d announcements back to %s and the list of %s)." % (
        m["index"], day, len(syms), m["announcements"], m["first_change"] or "-", m["snapshot"]))
    print(", ".join(syms))
    if m["first_change"] and day < m["first_change"]:
        print("Note: %s is before the oldest change on file; the list is today's minus every later change, "
              "so anything announced earlier and not on file is missing." % day)
    return 0


def cmd_results(a):
    root = default_root(a.root, a.workdir)
    evs = results_dates(a.symbol, a.start, a.end, root)
    if a.json:
        print(json.dumps(evs, indent=1))
        return 0
    if not evs:
        print("No results dates for %s in %s (backfill --datasets results first)." % (a.symbol.upper(), root))
        return 1
    print("%s: %d results events." % (a.symbol.upper(), len(evs)))
    for e in evs:
        print("  %s  %-19s %-6s react %s  %s %s" % (
            e["date"], e["announced_at"] or "(time unknown)", "after" if e["after_close"] else
            ("during" if e["after_close"] is False else "-"), e.get("event_session") or "-", e["quarter"] or "",
            ("period " + e["period_end"]) if e["period_end"] else e["source"]))
    return 0


def cmd_expired(a):
    root = default_root(a.root, a.workdir)
    now = _now(a)
    start = parse_day(a.start, "--from") if a.start else now.date() - datetime.timedelta(days=60)
    end = parse_day(a.end, "--to") if a.end else now.date()
    res = expired_options_ingest(root, a.underlying, start, end, a.broker, interval=a.interval, strikes=a.strikes,
                                 now=now, log=_log(a))
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print("Expired options of %s from %s: %d files, %d bars (under %s/optx)." % (
            a.underlying.upper(), a.broker, res["files"], res["bars"], root))
    return 0


def cmd_capture(a, http=None, sleep=None, now_fn=None):
    root = default_root(a.root, a.workdir)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    http = http or Http(pause=a.pause)
    beat = capture_chains(root, http, syms, a.every, a.expiries, a.sessions, now_fn=now_fn, sleep=sleep, log=_log(a),
                          until=a.until or "15:30")
    if a.json:
        print(json.dumps(beat, indent=1))
    else:
        print("capture-chains %s: %d rounds, %d snapshots over %d session%s (heartbeat %s/chain/capture.json)." % (
            beat["state"], beat["rounds"], beat["files"], beat["sessions_done"],
            "" if beat["sessions_done"] == 1 else "s", root))
    return 0


def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="archive.py", description="trader-archive: a local NSE market data archive.")
    ap.add_argument("--workdir", default="trader", help="the trading folder; the archive is WORKDIR/archive")
    ap.add_argument("--root", help="the archive folder itself (overrides --workdir and $BT_ARCHIVE)")
    sub = ap.add_subparsers(dest="cmd")

    def fetching(p):
        p.add_argument("--datasets", help="comma list from %s; 'all' adds the long-history ones (indexhist, "
                       "results, members); default: %s" % (",".join(DATASETS), ",".join(DEFAULT_DATASETS)))
        p.add_argument("--indices", help="indexhist: comma list of index names, 'default' (22 main ones) or 'all'")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between NSE calls (default 1.5, min 1.0)")
        p.add_argument("--max-days", type=int, help="stop after this many sessions (newest first)")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-31 19:00\"")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("backfill", help="fetch history: --years N or --from/--to")
    p.add_argument("--years", type=float, default=1.0, help="how far back (default 1; 3 for the lab)")
    p.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (overrides --years)")
    p.add_argument("--to", dest="end", help="last day (default today)")
    fetching(p)
    p = sub.add_parser("update", help="from the last archived day to today")
    fetching(p)
    p = sub.add_parser("status", help="what is stored, how big, how fresh")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="checksums, gaps vs the calendar, duplicates, outliers, survivorship")
    p.add_argument("--quick", action="store_true", help="checksums and gaps only")
    p.add_argument("--now", help="judge freshness at this IST time")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("export", help="bars as CSV (date,time,open,high,low,close,volume) for the lab or a sheet")
    p.add_argument("--symbol", required=True, help="an NSE stock, or an index: NIFTY, BANKNIFTY, ^NSEI, ^INDIAVIX")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--interval", default="1d", choices=["1d", "1m", "5m"])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--adjusted", action="store_true", help="split- and bonus-adjusted prices (the default)")
    g.add_argument("--raw", action="store_true", help="prices as NSE printed them")
    p.add_argument("--out", help="a file (.csv or .csv.gz; default: print)")
    p = sub.add_parser("intraday", help="1m/5m candles from your broker's history API, and/or Yahoo's last 60 days")
    p.add_argument("--symbols", required=True, help="comma list, e.g. RELIANCE,TCS,NIFTY")
    p.add_argument("--interval", default="1m", choices=["1m", "5m"])
    p.add_argument("--from", dest="start", help="default: 30 days ago")
    p.add_argument("--to", dest="end")
    p.add_argument("--broker", default="upstox", choices=["upstox", "kite", "dhan"],
                   help="whose history API (your token from the environment; Kite and Dhan need their data plan)")
    p.add_argument("--yahoo", action="store_true", help="also merge Yahoo's recent bars (fills gaps only)")
    p.add_argument("--no-broker", action="store_true", help="Yahoo only")
    p.add_argument("--now", help="pretend it is this IST time")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("snapshot-chain", help="save option chains during market hours (for options backtests)")
    p.add_argument("--symbols", default="NIFTY", help="comma list (default NIFTY)")
    p.add_argument("--expiries", type=int, default=1, help="nearest N expiries (default 1)")
    p.add_argument("--every", type=int, help="minutes between snapshots (min 1); omit for one snapshot")
    p.add_argument("--until", help="stop after this IST time (default 15:30)")
    p.add_argument("--force", action="store_true", help="take a snapshot even outside market hours")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--now", help="pretend it is this IST time (one snapshot)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("continuous", help="a continuous futures series (NIFTY, BANKNIFTY or any F&O stock)")
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--method", default="back", choices=["none", "back", "ratio"])
    p.add_argument("--roll", default="days:0", help="days:N (N sessions before expiry), oi or volume")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--out", help="write the bars as CSV (.csv or .csv.gz)")
    p.add_argument("--show", type=int, default=12, help="rolls to print (newest)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("members", help="point-in-time index constituents (NIFTY 50/Next 50/100/200/500)")
    p.add_argument("--index", help="NIFTY50, NIFTYNXT50, NIFTY100, NIFTY200 or NIFTY500")
    p.add_argument("--date", help="the day (default today)")
    p.add_argument("--report", action="store_true", help="coverage and gaps of the rebuilt membership")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("results", help="results (earnings) dates of a stock, for event studies")
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("expired-options", help="intraday candles of expired options from your broker (Upstox "
                                              "Plus or Dhan Data API)")
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--broker", default="upstox", choices=["upstox", "dhan"])
    p.add_argument("--from", dest="start", help="default: 60 days ago")
    p.add_argument("--to", dest="end")
    p.add_argument("--interval", default="1m")
    p.add_argument("--strikes", type=int, default=5, help="strikes either side of the money (default 5)")
    p.add_argument("--now", help="pretend it is this IST time")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("capture-chains", help="background loop: option-chain snapshots through every session")
    p.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    p.add_argument("--every", type=int, default=5, help="minutes between rounds (default 5, min 3)")
    p.add_argument("--expiries", type=int, default=2, help="nearest N expiries (default 2)")
    p.add_argument("--sessions", type=int, default=1, help="sessions to capture, 0 = until stopped (default 1)")
    p.add_argument("--until", help="stop each session after this IST time (default 15:30)")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"backfill": cmd_backfill, "update": cmd_update, "status": cmd_status, "verify": cmd_verify,
                "export": cmd_export, "intraday": cmd_intraday, "snapshot-chain": cmd_snapshot,
                "continuous": cmd_continuous, "members": cmd_members, "results": cmd_results,
                "expired-options": cmd_expired, "capture-chains": cmd_capture}[a.cmd](a)
    except ArchiveError as e:
        print("archive: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\narchive: stopped. Everything written so far is kept; run the same command to carry on.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
