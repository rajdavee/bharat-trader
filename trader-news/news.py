#!/usr/bin/env python3
"""News monitor for trader-news (NSE, India). Standard library only.

Watches headlines and NSE corporate filings for the stocks you watch or hold
and prints only what is new since the last check, newest first, in IST, with
how far each stock has moved today and a flag on anything that touches your
symbols.

    python3 news.py                                    # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS INFY
    python3 news.py --tickers RELIANCE,TCS,HDFCBANK    # commas work too
    python3 news.py --tickers SBIN --all --hours 72    # everything in the last 3 days, seen or not
    python3 news.py --market                           # add Nifty 50 / macro headlines
    python3 news.py --no-nse                           # headlines only
    python3 news.py --no-moves                         # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. Symbols are NSE symbols (RELIANCE, M&M,
BAJAJ-AUTO); an index can be given in Yahoo form (^NSEI, ^NSEBANK).

Sources, one request each per symbol, a pause between calls:
  - NSE corporate announcements (what the company filed with the exchange
    under SEBI LODR Regulation 30: results, board outcomes, dividends,
    acquisitions, credit ratings, SAST/insider disclosures, orders...):
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>
  - NSE board meetings (announced dates; upcoming ones are listed on top):
    https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=<SYM>
  - Yahoo Finance headline RSS (thin and patchy for many NSE names; some
    return an empty feed, which is not an error):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYM>.NS&region=IN&lang=en-IN
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against the previous close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYM>.NS?range=1d&interval=5m

Times print in IST (UTC+05:30). State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
yahoo-<SYM>.xml, nse-ann-<SYM>.json, nse-bm-<SYM>.json and chart-<SYM>.json
from DIR. The tests use it.

Headlines are leads, not facts: open the filing or story before acting, and
treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
NSE_BM = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=%s"
YAHOO_UA = "Mozilla/5.0 (compatible; bharat-trader/0.3; +https://github.com/rajdavee/bharat-trader)"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130 Safari/537.36")
MARKET = "^NSEI"           # --market: Nifty 50 headlines and move
KEEP_DAYS = 30
MOVE_PCT = 3.0             # a stock this far from the previous close is flagged as moving
PAUSE = 0.35               # seconds between requests
BACKOFF = (2.0, 5.0)       # waits before the retries after a 429
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
INDEX = re.compile(r"^\^[A-Z0-9]{1,20}$")

# label, regex. Matched against title + summary (case-insensitive) after the
# SEBI LODR boilerplate is stripped, so "Regulation 30 of SEBI (LODR)" alone
# does not make an item "regulatory".
KEYWORDS = [
    ("results", r"\bresults?\b|\bearnings\b|\bquarter(?:ly)? (?:numbers|update)\b|\bQ[1-4]\s?FY\s?\d{2}"
                r"|\bnet (?:profit|loss)\b|\bprofit (?:rises|falls|jumps|drops|up|down)\b|\bEBITDA\b"
                r"|\brevenue\b|\bguidance\b|\bintegrated filing\b"),
    ("board meeting", r"\bboard meeting\b|\bmeeting of (?:the )?board\b|\boutcome of (?:the )?board\b"),
    ("dividend", r"\bdividends?\b"),
    ("bonus", r"\bbonus (?:issue|shares?|equity)\b|\bissue of bonus\b"),
    ("split", r"\bstock split\b|\bshare split\b|\bsplit of (?:equity )?shares\b|\bsub-?division\b"),
    ("buyback", r"\bbuy-?\s?back\b"),
    ("rights", r"\brights (?:issue|entitlement|offer)\b"),
    ("fund raising", r"\bQIP\b|\bqualified institution(?:s|al)? placement\b|\bfund[- ]?rais|\braising of funds\b"
                     r"|\bpreferential (?:issue|allotment)\b|\bnon-convertible debentures\b|\bNCDs?\b"
                     r"|\bwarrants\b|\binitial public offer\b|\bIPO\b|\bOFS\b|\boffer for sale\b"),
    ("deal", r"\bacqui(?:re|res|red|ring|sition|sitions)\b|\bmerger\b|\bamalgamation\b|\bde-?merger\b"
             r"|\bscheme of arrangement\b|\btakeover\b|\bjoint venture\b|\bstake (?:sale|purchase)\b"
             r"|\bsale or disposal\b|\bdivest|\bdisinvest|\brestructuring\b"),
    ("order win", r"\border (?:win|wins|worth|inflow|book)\b|\bbag(?:s|ged|ging)?\b.{0,40}\borders?\b"
                  r"|\breceiving of orders\b|\bletter of (?:award|intent)\b|\bwins? (?:a |an )?(?:contract|deal|order)"
                  r"|\bcontract (?:win|worth|award)"),
    ("credit rating", r"\bcredit rating\b|\brating (?:upgrade|downgrade|reaffirm)|\b(?:upgrade|downgrade)s? (?:the )?rating"
                      r"|\bCRISIL\b|\bICRA\b|\bCARE Ratings\b|\bIndia Ratings\b|\bMoody'?s\b|\bFitch\b"),
    ("regulatory", r"\bSEBI (?:order|penalty|notice|probe|ban|bars|action|investigation)|\bshow[- ]cause\b"
                   r"|\bpenalt(?:y|ies)\b|\borders? passed\b|\baction\(s\) (?:taken|initiated)\b|\badjudicat"
                   r"|\bNCLT\b|\bNCLAT\b|\btribunal\b|\blitigation|\bcourt\b|\btax demand\b|\bGST (?:demand|notice)\b"
                   r"|\bregulator\b|\bprobe\b|\braid"),
    ("pledge", r"\bpledg|\bencumbrance\b|\bencumbered\b"),
    ("insider", r"\binsider\b|\bSAST\b|\btakeover regulations\b|\bregulation 29\b|\bregulation 31\b"
                r"|\btrading window\b|\btrading plan\b|\bpromoters? (?:group )?(?:buy|sell|bought|sold|acquir|stake|hike|cut)"),
    ("block deal", r"\bblock deals?\b|\bbulk deals?\b|\bblock trade\b"),
    ("management", r"\bCEO\b|\bCFO\b|\bCOO\b|\bmanaging director\b|\bwhole[- ]time director\b|\bresign|\bsteps? down\b"
                   r"|\bappoint|\bcessation\b|\bchange in (?:management|directors?)\b|\bkey managerial\b|\bKMP\b"),
    ("auditor", r"\bauditors?\b.{0,30}\b(?:resign|quit|change|cessation)|\bresignation of (?:the )?(?:statutory )?auditor"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bmonetary policy\b|\bMPC\b|\binflation\b|\bCPI\b|\bWPI\b|\bGDP\b"
              r"|\bunion budget\b|\bbudget\b|\bfiscal deficit\b|\bGST collection|\bFIIs?\b|\bFPIs?\b"
              r"|\brupee\b|\bcrude\b|\bFed\b|\bFOMC\b|\btariffs?\b|\bIndia VIX\b"),
]
IMPORTANT_TAGS = {"results", "board meeting", "fund raising", "deal", "credit rating", "regulatory",
                  "pledge", "auditor", "buyback", "bonus", "split", "rights", "order win"}
BOILERPLATE = [
    r"disclosure under regulation \d+\s*(?:\([^)]*\))?\s*of the securities and exchange board of india\s*"
    r"\(listing obligations and disclosure requirements\)\s*regulations,?\s*\d{4}",
    r"(?:pursuant to |under )?regulation \d+\s*(?:\([^)]*\))?\s*of (?:the )?sebi\s*\(listing obligations and "
    r"disclosure requirements\)\s*regulations,?\s*\d{4}",
    r"\bsebi \(lodr\)(?: regulations,? \d{4})?",
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware UTC (or naive UTC) -> aware IST datetime."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_dt.timezone.utc)
    return utc.astimezone(IST)


def fmt_ist(utc, with_date=True):
    return to_ist(utc).strftime("%Y-%m-%d %H:%M IST" if with_date else "%H:%M IST")


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None. An ISO time
    without an offset is IST."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_nse_time(value):
    """NSE's "29-Oct-2026 08:55:40" (IST) -> aware UTC, or None."""
    value = (value or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            d = _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        return d.replace(tzinfo=IST).astimezone(_dt.timezone.utc)
    return None


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def normalise(sym):
    """'reliance.ns' -> 'RELIANCE'; '^nsei' -> '^NSEI'. Raises NewsError on junk."""
    s = str(sym or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    if TICKER.match(s) or INDEX.match(s):
        return s
    raise NewsError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO) or an index like ^NSEI" % sym)


def split_tickers(args):
    """--tickers RELIANCE TCS, --tickers RELIANCE,TCS or a mix -> ordered unique symbols."""
    out = []
    for a in args or []:
        for part in re.split(r"[\s,;]+", str(a)):
            if part:
                s = normalise(part)
                if s not in out:
                    out.append(s)
    return out


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []) if isinstance(wl, dict) else []:
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[str(sym).upper()] = h.get("name")
    pl = load_json(plan) or {}
    if isinstance(pl, dict) and pl.get("symbol"):
        out.setdefault(str(pl["symbol"]).upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    pos = pf.get("positions", {}) if isinstance(pf, dict) else {}
    if isinstance(pos, dict):           # {"RELIANCE": {"qty", "avg_price"}}
        for sym, p in pos.items():
            name = p.get("name") if isinstance(p, dict) else None
            out[str(sym).upper()] = name or out.get(str(sym).upper())
    else:                               # older list form
        for p in pos:
            sym = p.get("symbol") or p.get("ticker")
            if sym:
                out[str(sym).upper()] = p.get("name") or out.get(str(sym).upper())
    clean = {}
    for sym, name in out.items():
        try:
            clean[normalise(sym)] = name
        except NewsError:
            continue
    return clean


# ---------------------------------------------------------------- fetching

_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _decode(data, encoding):
    encoding = (encoding or "").lower()
    if encoding == "gzip" or data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    return data


class Fetcher(object):
    def __init__(self, offline=None, pause=PAUSE):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if nse:
            headers = {"User-Agent": NSE_UA, "Accept": "application/json",
                       "Referer": "https://www.nseindia.com/", "Accept-Encoding": "gzip, deflate"}
        else:
            headers = {"User-Agent": YAHOO_UA, "Accept-Encoding": "gzip, deflate"}
        waits = list(BACKOFF)
        while True:
            gap = time.time() - self._last
            if gap < self.pause:
                _sleep(self.pause - gap)
            self._last = time.time()
            req = urllib.request.Request(url, headers=headers)
            try:
                with _urlopen(req, timeout=30) as resp:
                    return _decode(resp.read(), resp.headers.get("Content-Encoding"))
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 429 and waits:
                    _sleep(waits.pop(0))
                    continue
                raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
            except urllib.error.URLError as exc:
                raise NewsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))


def yahoo_symbol(sym):
    """RELIANCE -> RELIANCE.NS, M&M -> M%26M.NS, ^NSEI -> %5ENSEI (URL-encoded)."""
    s = sym.upper()
    if not s.startswith("^"):
        s += ".NS"
    return urllib.parse.quote(s, safe=".-")


def is_index(sym):
    return sym.startswith("^")


# ---------------------------------------------------------------- parsing

def _text(el, name):
    for c in el.iter():
        if c.tag.rsplit("}", 1)[-1] == name:
            return (c.text or "").strip()
    return ""


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items. An empty channel is a valid, empty answer."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def _load_list(data, what):
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError):
        raise NewsError("%s is not JSON (NSE may be blocking the request; try again later)" % what)
    if isinstance(doc, dict):
        doc = doc.get("data", [])
    if not isinstance(doc, list):
        raise NewsError("%s has an unexpected shape" % what)
    return doc


def _squash(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_nse_announcements(data, ticker):
    """NSE corporate-announcements JSON -> items (one per filing)."""
    out = []
    for a in _load_list(data, "NSE announcements for %s" % ticker):
        if not isinstance(a, dict):
            continue
        cat = _squash(a.get("desc")) or "Announcement"
        text = _squash(a.get("attchmntText"))
        short = _squash(strip_boilerplate(text)).lstrip(" .,:;-")
        if len(short) < 20:   # nothing left but "is attached": keep NSE's own words
            short = text
        sym = str(a.get("symbol") or ticker).upper()
        title = "%s: %s" % (cat, short) if short else cat
        if len(title) > 220:
            title = title[:217].rstrip() + "..."
        key = a.get("seq_id") or "%s|%s|%s" % (sym, a.get("an_dt"), cat)
        out.append({"id": "nse:%s" % key, "source": "NSE", "kind": "filing", "category": cat,
                    "ticker": ticker, "company": a.get("sm_name"), "title": title, "summary": text,
                    "link": a.get("attchmntFile") or "",
                    "time": parse_nse_time(a.get("an_dt") or a.get("exchdisstime") or a.get("sort_date"))})
    return out


def parse_nse_board_meetings(data, ticker):
    """NSE corporate-board-meetings JSON -> items. "meeting_date" is the day the board meets."""
    out = []
    for b in _load_list(data, "NSE board meetings for %s" % ticker):
        if not isinstance(b, dict):
            continue
        purpose = _squash(b.get("bm_purpose")) or "Board Meeting"
        mdate = None
        try:
            mdate = _dt.datetime.strptime(str(b.get("bm_date") or ""), "%d-%b-%Y").date()
        except ValueError:
            pass
        stamp = b.get("bm_timestamp") or b.get("sysTime")
        key = hashlib.sha1(("%s|%s|%s" % (b.get("bm_symbol") or ticker, stamp, purpose))
                           .encode("utf-8")).hexdigest()[:12]
        what = purpose
        if purpose.lower() == "board meeting intimation" and b.get("bm_desc"):
            what = _squash(b.get("bm_desc"))
            what = re.sub(r"^.*?\bto be held on \S+ (?:to |inter[- ]alia,? )*", "", what, flags=re.I) or what
            what = what[:1].upper() + what[1:]
            if len(what) > 160:
                what = what[:157].rstrip() + "..."
        title = "Board meeting on %s: %s" % (mdate.isoformat() if mdate else b.get("bm_date"), what)
        out.append({"id": "nsebm:%s" % key, "source": "NSE", "kind": "board_meeting", "category": purpose,
                    "ticker": ticker, "company": b.get("sm_name"), "title": title,
                    "summary": _squash(b.get("bm_desc")),
                    "link": b.get("attachment") or b.get("ixbrl") or "",
                    "meeting_date": mdate.isoformat() if mdate else None,
                    "time": parse_nse_time(stamp)})
    return out


def upcoming_meetings(items, now):
    """Board meetings on or after today (IST), soonest first, one per symbol and date."""
    today = to_ist(now).date().isoformat()
    seen, out = set(), []
    generic = lambda i: i["category"].lower() == "board meeting intimation"  # noqa: E731
    for it in sorted((i for i in items if i["kind"] == "board_meeting" and i.get("meeting_date")),
                     key=lambda i: (i["meeting_date"], i["ticker"], generic(i))):
        k = (it["ticker"], it["meeting_date"])
        if it["meeting_date"] >= today and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        if is_index(t):
            continue
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t),
                r"\b%s\.NS\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def strip_boilerplate(text):
    for rx in BOILERPLATE:
        text = re.sub(rx, " ", text, flags=re.I)
    return text


def tags_for(text):
    hay = strip_boilerplate(text)
    return [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]


def tag(items, watch):
    """Which of your symbols each item touches, why it matters, and the "!" flag on filings."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s %s" % (it.get("category") or "", it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = tags_for(hay)
        if it["kind"] == "board_meeting" and "board meeting" not in it["tags"]:
            it["tags"].insert(0, "board meeting")
        it["important"] = it["kind"] != "headline" and bool(set(it["tags"]) & IMPORTANT_TAGS)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, now, hours=48, nse=True, headlines=True, market=False, errors=None):
    errors = [] if errors is None else errors
    items = []
    syms = sorted(watch)
    if market and MARKET not in syms:
        syms.append(MARKET)
    if headlines:
        for t in syms:
            try:
                items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
            except NewsError as exc:
                errors.append("headlines for %s: %s" % (t, exc))
    if nse:
        start = to_ist(now - _dt.timedelta(hours=max(hours, 24))).strftime("%d-%m-%Y")
        end = to_ist(now).strftime("%d-%m-%Y")
        for t in syms:
            if is_index(t):
                continue
            q = urllib.parse.quote(t, safe="-")
            try:
                items += parse_nse_announcements(
                    fetcher.get(NSE_ANN % (q, start, end), "nse-ann-%s.json" % t, nse=True), t)
            except NewsError as exc:
                errors.append("NSE announcements for %s: %s" % (t, exc))
            try:
                items += parse_nse_board_meetings(
                    fetcher.get(NSE_BM % q, "nse-bm-%s.json" % t, nse=True), t)
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("NSE board meetings for %s: %s" % (t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def fmt_num(x):
    """Indian digit grouping without the rupee sign: 22936.75 -> "22,936.75"."""
    neg = x < 0
    whole, frac = ("%.2f" % abs(float(x))).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def inr(x):
    """123456.5 -> "₹1,23,456.50" (Indian grouping); -1500 -> "-₹1,500.00"."""
    s = fmt_num(x)
    return ("-₹" + s[1:]) if s.startswith("-") else ("₹" + s)


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        money = fmt_num if is_index(m["ticker"]) else inr
        when = (" at %s" % fmt_ist(m["time"], False)) if m.get("time") else ""
        out.append(("%-10s %s %+.2f%% vs previous close %s%s  %s" % (
            m["ticker"], money(m["price"]), m["pct"], money(m["prev_close"]), when, flag)).rstrip())
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT, upcoming=None):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    for b in upcoming or []:
        out.append("UPCOMING %s board meeting %s: %s" % (b["ticker"], b["meeting_date"], b["category"]))
    if upcoming:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = fmt_ist(it["time"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the filing or story before acting. "
               "Not financial advice." % fmt_ist(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = to_ist(it["time"]).isoformat() if it["time"] else None
        out.append(d)
    return out


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols, space or comma separated "
                                                 "(default: watchlist + plan + portfolio)")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--market", action="store_true", help="add Nifty 50 (^NSEI) headlines and move")
    ap.add_argument("--no-nse", action="store_true", help="skip NSE announcements and board meetings")
    ap.add_argument("--no-headlines", action="store_true", help="skip Yahoo Finance headlines")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs (no offset = IST)")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        given = split_tickers(args.tickers)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if given:
        watch = {t: known.get(t) for t in given}
    else:
        watch = known
    if not watch and not args.market:
        print("news: no symbols. Pass --tickers RELIANCE,TCS, or run trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    if now is None:
        print("news: --now wants an ISO time like 2026-10-29T10:15 (IST)", file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, now, hours=args.hours, nse=not args.no_nse,
                    headlines=not args.no_headlines, market=args.market, errors=errors)
    names = dict(watch)
    for it in items:   # NSE tells us the company name: use it to spot mentions in headlines
        if it.get("company") and not names.get(it["ticker"]):
            names[it["ticker"]] = re.sub(r"\s+(?:Limited|Ltd\.?)$", "", it["company"], flags=re.I)
    if args.market:
        names.setdefault(MARKET, None)
    items = dedupe(tag(items, names))
    upcoming = upcoming_meetings(items, now)
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    move_syms = dict(watch)
    if args.market:
        move_syms.setdefault(MARKET, None)
    moves = [] if args.no_moves else collect_moves(fetcher, move_syms, errors)
    if args.json:
        mv = [dict(m, time=to_ist(m["time"]).isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": to_ist(now).isoformat(), "moves": mv,
                          "upcoming_board_meetings": [{"symbol": u["ticker"], "date": u["meeting_date"],
                                                       "purpose": u["category"], "link": u["link"]}
                                                      for u in upcoming],
                          "items": _json_ready(shown), "notes": errors}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct, upcoming))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
