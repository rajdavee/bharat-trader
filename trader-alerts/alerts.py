#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is NSE open? next open / close (IST)
    python3 alerts.py clock --verify                 # also ask nseindia.com/api/marketStatus
    python3 alerts.py quote RELIANCE TCS --write     # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by 15:10 from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1163.35
    python3 alerts.py add --symbol RELIANCE --target 1183.50
    python3 alerts.py add --symbol ^NSEI --below 22800      # Nifty 50 works as a symbol too
    python3 alerts.py add --at 15:10 --label "flat by" --symbol RELIANCE
    python3 alerts.py add --symbol SBIN --pct 2
    python3 alerts.py add --opening-bell              # 09:15 IST
    python3 alerts.py add --pre-open                  # 09:00 IST, pre-open order entry
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-29 10:15" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE normal session opened today (09:15 IST; rings once per trading day)
  pre_open        the pre-open session started (09:00 IST); rings once per trading
                  day, only if a check runs before the open
  time            the IST clock passed "at" (HH:MM) on a trading day (rings once per trading day)

A rule with "date" is only watched on that IST date (rules from a plan are for
the plan's day only). An above / below alert rings ONCE and then stays quiet
until you reset it, unless the rule has "rearm": true (it re-arms when the
price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  Yahoo Finance chart JSON, https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS
  (query1 on a 429 or a 5xx; one retry with a pause on a 429; one request per
  symbol, half a second apart). NSE symbols get ".NS"; an index such as ^NSEI,
  ^NSEBANK or ^INDIAVIX is used as it is. --offline DIR reads
  DIR/yahoo-<SYMBOL>.json (or .json.gz) instead.

NSE calendar: Capital Market segment, normal session 09:15-15:30 IST Mon-Fri,
pre-open 09:00-09:08, IST = UTC+05:30 all year (no daylight saving). The 2026
trading holidays are built in (from NSE's holiday master). trader/config.json
can add "extra_holidays": ["YYYY-MM-DD"] and "special_sessions":
[{"date": "2026-11-08", "open": "18:00", "close": "19:00", "name": "Muhurat"}]
(NSE announces Muhurat timings separately). For a year with no table the
clock says the calendar is unknown and treats weekdays as open.
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

UA = "Mozilla/5.0 (compatible; bharat-trader/0.1; +https://github.com/rajdavee/bharat-trader)"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130 Safari/537.36")
NSE_STATUS_URL = "https://www.nseindia.com/api/marketStatus"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
INDEX = re.compile(r"^\^[A-Z0-9]{1,20}$")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

# NSE Capital Market trading holidays 2026 (NSE holiday master, key "CM").
# Weekend dates are listed by NSE too; they change nothing but are kept for the record.
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
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading: timing announced separately by NSE)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
PRE_OPEN = datetime.time(9, 0)
PRE_OPEN_END = datetime.time(9, 8)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
DEFAULT_FLAT_BY = "15:10"


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- money

def fmt_num(x):
    """Indian digit grouping, two decimals: 1234567.5 -> "12,34,567.50"."""
    neg = float(x) < 0
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
    """123456.5 -> "₹1,23,456.50"; -1500 -> "-₹1,500.00"."""
    s = fmt_num(x)
    return ("-₹" + s[1:]) if s.startswith("-") else ("₹" + s)


def money(sym, x):
    """Rupees for a stock, plain points for an index (^NSEI)."""
    return fmt_num(x) if str(sym).startswith("^") else inr(x)


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive (or aware) UTC -> naive IST wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return utc + IST_OFFSET


def ist_iso(utc):
    """Naive UTC -> "2026-10-29T10:15:00+05:30"."""
    return to_ist(utc).replace(tzinfo=IST).isoformat()


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-10-29 10:15\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt - IST_OFFSET
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _minus(t, minutes):
    return (datetime.datetime.combine(datetime.date(2000, 1, 1), t)
            - datetime.timedelta(minutes=minutes)).time()


class Calendar(object):
    """The NSE calendar plus the user's extra_holidays and special_sessions."""

    def __init__(self, cfg=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.holidays = dict(NSE_HOLIDAYS)
        for d in cfg.get("extra_holidays") or []:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d)):
                raise AlertError("extra_holidays wants dates like \"2027-01-26\", got %r" % d)
            self.holidays.setdefault(str(d), "holiday (from your config)")
        self.special = {}
        for s in cfg.get("special_sessions") or []:
            if not isinstance(s, dict):
                raise AlertError("special_sessions wants {\"date\", \"open\", \"close\"} entries")
            o, c = _hhmm(s.get("open")), _hhmm(s.get("close"))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(s.get("date"))) or not o or not c or o >= c:
                raise AlertError("special session %r needs a date and open < close as HH:MM IST" % s)
            self.special[str(s["date"])] = (o, c, s.get("name") or "special session")

    def known(self, d):
        return d.year in CALENDAR_YEARS

    def trading_day(self, d):
        """(is_trading_day, reason)."""
        key = d.isoformat()
        if key in self.special:
            return True, self.special[key][2]
        if d.weekday() >= 5:
            return False, "weekend"
        name = self.holidays.get(key)
        if name:
            return False, "NSE holiday: " + name
        return True, ""

    def session(self, d):
        """(pre_open, open, close, special_name or None) in IST, or None if closed all day."""
        key = d.isoformat()
        if key in self.special:
            o, c, name = self.special[key]
            return _minus(o, 15), o, c, name
        ok, _ = self.trading_day(d)
        if not ok:
            return None
        return PRE_OPEN, OPEN, CLOSE, None


def market_status(now_utc, cal=None):
    cal = cal or Calendar()
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = cal.trading_day(d)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": cal.known(d),
          "open": False, "phase": "closed", "reason": reason, "special_session": None}
    if not cal.known(d):
        st["warning"] = ("NSE's %d holiday list is not built in: weekdays are treated as open. Check "
                         "nseindia.com and add extra_holidays to trader/config.json." % d.year)
    s = cal.session(d)
    if s:
        pre, o, c, special = s
        st.update({"pre_open": pre.strftime("%H:%M"), "opens": o.strftime("%H:%M"), "closes": c.strftime("%H:%M"),
                   "special_session": special})
        now = local.time()
        if o <= now < c:
            st["open"], st["phase"] = True, "open"
            st["reason"] = special or "normal session"
        elif pre <= now < o:
            st["phase"] = "pre-open"
            st["reason"] = ("pre-open session (order entry until %s, normal session from %s)"
                            % (_minus(o, 7).strftime("%H:%M"), o.strftime("%H:%M")))
        elif now < pre:
            st["reason"] = "before the pre-open"
        else:
            st["reason"] = "after the close"
    nd = d if (s and local.time() < s[1]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        ns = cal.session(nd)
        if ns:
            st["next_open"] = "%s %s" % (nd.isoformat(), ns[1].strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def normalise(sym):
    """'reliance.ns' -> 'RELIANCE', '^nsei' -> '^NSEI'."""
    s = str(sym or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    if TICKER.match(s) or INDEX.match(s):
        return s
    raise AlertError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO) or an index like ^NSEI" % sym)


def yahoo_symbol(sym):
    s = sym.upper()
    if not s.startswith("^"):
        s += ".NS"
    return s


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=5m" % (
        host, urllib.parse.quote(yahoo_symbol(sym), safe=".-"))


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = ist_iso(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).replace(tzinfo=None)) if t else ""
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "price": float(price), "open": round(opens[0], 2) if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("chartPreviousClose") or m.get("previousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20, headers=None):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return 200, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, name):
    base = os.path.join(folder, name)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline file %s" % base)


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        try:
            sym = normalise(sym)
            if offline:
                text = _offline_text(offline, "yahoo-%s.json" % sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc):
    """Merge quotes into trader/prices.json:
    {"as_of", "source": "yahoo", "note", "prices": {SYM: price},
     "detail": {SYM: {"price", "prev_close", "time" (IST ISO), "source", "fetched" (IST ISO)}}}"""
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {})) if isinstance(old, dict) else {}
    detail = dict(old.get("detail", {})) if isinstance(old, dict) else {}
    stamp = ist_iso(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"price": q["price"], "prev_close": q.get("prev_close"), "time": q.get("time"),
                     "source": q.get("source", "yahoo"), "fetched": stamp}
    doc = {"as_of": stamp, "source": "yahoo",
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.time is the quote's own time (IST), detail.fetched when it was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def parse_nse_status(text):
    """nseindia.com/api/marketStatus -> the Capital Market row, simplified."""
    try:
        doc = json.loads(text)
        rows = doc["marketState"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("NSE market status is not JSON (NSE may be blocking the request)")
    for r in rows:
        if isinstance(r, dict) and r.get("market") == "Capital Market":
            pct = r.get("percentChange")
            return {"status": r.get("marketStatus"), "message": r.get("marketStatusMessage"),
                    "trade_date": r.get("tradeDate"), "index": r.get("index"),
                    "last": r.get("last"), "pct": pct if isinstance(pct, (int, float)) else None,
                    "open": str(r.get("marketStatus", "")).lower() == "open"}
    raise AlertError("NSE market status has no Capital Market row")


def nse_status(offline=None):
    if offline:
        return parse_nse_status(_offline_text(offline, "nse-marketStatus.json"))
    code, text = _get(NSE_STATUS_URL, headers={"User-Agent": NSE_UA, "Accept": "application/json",
                                               "Referer": "https://www.nseindia.com/"})
    if code == 429:
        _sleep(BACKOFF)
        code, text = _get(NSE_STATUS_URL, headers={"User-Agent": NSE_UA, "Accept": "application/json",
                                                   "Referer": "https://www.nseindia.com/"})
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (NSE_STATUS_URL, code))
    return parse_nse_status(text)


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


def load_calendar(path):
    return Calendar(_read(path, {}) if path else {})


# ---------------------------------------------------------------- rules

RULE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "pre_open", "time"}
NO_QUOTE = {"opening_bell", "pre_open", "time"}


def validate(rule):
    t = rule.get("type")
    if t not in RULE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if rule.get("symbol"):
        normalise(rule["symbol"])
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this stamp (IST ISO, or older UTC "...Z") ring on this IST date?"""
    if not stamp:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return str(stamp)[:10] == today
    if dt.tzinfo is None:
        return dt.date().isoformat() == today
    return dt.astimezone(IST).date().isoformat() == today


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = ist_iso(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (str(r.get("symbol", "market")).lower(), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                special = status.get("special_session")
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST, %s %s-%s)."
                              % (status["now_ist"][11:], special or "normal session",
                                 status["opens"], status["closes"])})
                rung[rid] = stamp
            continue
        if typ == "pre_open":
            if status.get("phase") == "pre-open" and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Pre-open: NSE pre-open order entry has started "
                              "(%s IST); the normal session opens at %s." % (status["now_ist"][11:], status["opens"])})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                           local.strftime("%H:%M"), at.strftime("%H:%M"))
                if status["open"]:
                    msg += ", the market closes at %s." % status["closes"]
                elif status.get("phase") == "pre-open" or local.time() < _hhmm(status["opens"]):
                    msg += ", the market opens at %s." % status["opens"]
                else:
                    msg += ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it before the broker's intraday square-off: "
                            "trader-broker flatten --symbol %s builds the exit order, then trader-risk and "
                            "your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = normalise(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", money(sym, p), word, label,
                                 money(sym, r["price"]), (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, money(sym, p), money(sym, float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day.
    A short plan (side sell/short) gets its stop above and its target below."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = normalise(plan["symbol"])
    short = str(plan.get("side", "buy")).lower() in ("sell", "short")
    out = []
    for key, word in (("stop", "plan stop"), ("target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        typ = key if not short else {"stop": "above", "target": "below"}[key]
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or DEFAULT_FLAT_BY
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:10" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by %s" % flat})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    pos = pf.get("positions", {}) if isinstance(pf, dict) else {}
    if isinstance(pos, dict):
        syms += [str(s).upper() for s in pos]
    else:
        for p in pos:
            syms.append(str(p.get("symbol") or p.get("ticker") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def split_symbols(args):
    out = []
    for a in args or []:
        for part in re.split(r"[\s,;]+", str(a)):
            if part:
                out.append(normalise(part))
    return _dedupe(out)


def cmd_clock(a):
    st = market_status(parse_now(a.now), load_calendar(a.config))
    nse = None
    if a.verify:
        try:
            nse = nse_status(a.offline)
            st["nse"] = nse
            st["nse_agrees"] = nse["open"] == st["open"]
        except AlertError as e:
            st["nse_error"] = str(e)
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
    else:
        print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                           "OPEN" if st["open"] else ("PRE-OPEN" if st["phase"] == "pre-open"
                                                                      else "closed"), st["reason"]))
        if st.get("opens"):
            print("Today's session: pre-open %s, normal %s to %s IST%s." % (
                st["pre_open"], st["opens"], st["closes"],
                (" (%s)" % st["special_session"]) if st.get("special_session") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if st.get("warning"):
            print("Warning: " + st["warning"])
        if nse:
            print("nseindia.com says: Capital Market %s (%s)%s." % (
                nse["status"], nse["message"],
                ", Nifty 50 %s %+.2f%%" % (fmt_num(nse["last"]), nse["pct"])
                if isinstance(nse.get("last"), (int, float)) and nse.get("pct") is not None else ""))
            if not st["nse_agrees"]:
                print("The local calendar and NSE disagree: trust NSE (a special session, an unscheduled "
                      "closure, or a pinned --now).")
        elif st.get("nse_error"):
            print("Could not cross-check with NSE: " + st["nse_error"])
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe(split_symbols(a.symbols) + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                           money(s, q["prev_close"]))
                print("%-10s %s%s  %s  [%s]" % (s, money(s, q["price"]), chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now, load_calendar(a.config))
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([normalise(r["symbol"]) for r in watched if r.get("type") not in NO_QUOTE and r.get("symbol")])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2, ensure_ascii=False))
    else:
        word = "OPEN" if status["open"] else ("PRE-OPEN" if status["phase"] == "pre-open" else "closed")
        print("NSE %s (%s IST, %s)." % (word, status["now_ist"], status["reason"]))
        if status.get("warning"):
            print("Warning: " + status["warning"])
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, money(s, quotes[s]["price"])) for s in syms
                                                if s in quotes)) if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Once your entry fills, the SL-M stop and the LIMIT "
                  "target sit at the broker: check the position and record any fill with trader-broker. To "
                  "close a position yourself: trader-broker flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r, ensure_ascii=False)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or a loop). "
              "Free quotes are delayed; the stop-loss order at the broker is what actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.pre_open:
        rule = {"id": a.id or "pre-open", "type": "pre_open"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = normalise(a.symbol)
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell, --pre-open)")
        sym = normalise(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower().lstrip("^"), rule["type"], _n(val)))},
                    symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule, ensure_ascii=False)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}, ensure_ascii=False),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
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
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is NSE open?")
    net(p)
    p.add_argument("--config", default="trader/config.json", help="extra_holidays and special_sessions")
    p.add_argument("--verify", action="store_true", help="cross-check with nseindia.com/api/marketStatus")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*", help="NSE symbols (space or comma separated) or an index like ^NSEI")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--config", default="trader/config.json", help="extra_holidays and special_sessions")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true", help="ring when NSE opens (09:15 IST)")
    g.add_argument("--pre-open", action="store_true", help="ring when the pre-open starts (09:00 IST)")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by 15:10\"")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
