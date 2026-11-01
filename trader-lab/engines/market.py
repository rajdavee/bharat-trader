"""The data the engines read, behind one small adapter.

ArchiveMarket reads trader-archive (the local NSE bhavcopy archive). The long-history archive adds four read
functions this adapter calls by name when they are there, and otherwise falls back as written:

    load_continuous(underlying, method, start, end)   a continuous near-month futures series; fallback: built
                                                      here from the archive's daily F&O boards (roll at the
                                                      close of the session before expiry)
    members(index, day)                               an index's members on a day (point in time); fallback:
                                                      none, and a point-in-time run is refused
    results_dates(symbol, start, end)                 the dates a company published results; fallback: none,
                                                      and a results-date run is refused
    load_daily / load_index (and legacy files to 2010) the adjusted daily bars, already in the archive

FolderMarket reads a plain folder (the tests' fixtures, or data you bring): SYMBOL-1d.csv(.gz) bars in the
lab's format, ^NSEI-1d.csv(.gz) for the index, members.json ({"NIFTY 200": {"YYYY-MM-DD": [symbols]}}, each
list holding from its date until the next), results.json ({symbol: [{"date", "time"}]}), sectors.json
({symbol: sector}), continuous/UNDERLYING.csv (date,expiry,lot,open,high,low,close,roll,next_close) or an
archive/ subfolder of daily F&O boards.
"""

import bisect
import csv
import gzip
import io
import json
import os

from . import core
from . import futures

INDEX_ALIASES = {"NIFTY": "Nifty 50", "NIFTY 50": "Nifty 50", "^NSEI": "Nifty 50", "BANKNIFTY": "Nifty Bank",
                 "NIFTY BANK": "Nifty Bank", "^NSEBANK": "Nifty Bank", "INDIA VIX": "India VIX",
                 "^INDIAVIX": "India VIX"}
INDEX_FILES = {"Nifty 50": "^NSEI", "Nifty Bank": "^NSEBANK", "India VIX": "^INDIAVIX"}


def _num(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def from_load_continuous(res):
    """The long-history archive's load_continuous(..., method "none") answer -> rows: its bars (the contract
    held each day, unadjusted) and its roll log (each roll dated on the first day on the new contract, with
    both contracts' prices on the old one's last day) as this engine's roll flag and roll price."""
    if isinstance(res, list):
        return normalize_continuous(res)
    bars = list((res or {}).get("bars") or [])
    rows = normalize_continuous(bars)
    at = {r["date"]: k for k, r in enumerate(rows)}
    for ro in (res or {}).get("rolls") or []:
        k = at.get(ro.get("date"))
        if k is None or k == 0:
            continue
        prev = rows[k - 1]
        if ro.get("new"):
            prev["roll"], prev["next_c"] = True, float(ro["new"])
            rows[k]["break"] = False
        else:
            prev["roll"], prev["next_c"] = False, None
            rows[k]["break"] = True
    return rows


def normalize_continuous(rows):
    """Rows from load_continuous in this engine's shape. Accepts o/open, h/high, l/low, c/close, expiry, lot,
    roll and next_c/next_close. With raw_c/close_raw as well, c is read as the adjusted close and a roll's
    next-contract price is recovered from it; with one price only and no roll price, the day after a contract
    change is flagged as a break (no return is booked across it)."""
    out = []
    for r in rows or []:
        g = (lambda *ks: next((r[k] for k in ks if k in r and r[k] not in (None, "")), None))
        c = _num(g("c", "close"))
        if c is None:
            continue
        raw = _num(g("raw_c", "close_raw", "unadjusted_close"))
        row = {"date": str(g("date"))[:10], "o": _num(g("raw_o", "o", "open")) or c, "h": _num(g("h", "high")) or c,
               "l": _num(g("l", "low")) or c, "c": raw or c, "adj": c if raw else None, "expiry": g("expiry"),
               "lot": int(_num(g("lot", "lot_size")) or 0) or None, "roll": bool(g("roll")),
               "next_c": _num(g("next_c", "next_close"))}
        out.append(row)
    out.sort(key=lambda x: x["date"])
    for k in range(1, len(out)):
        prev, cur = out[k - 1], out[k]
        if prev["expiry"] and cur["expiry"] and prev["expiry"] != cur["expiry"] and not prev["roll"]:
            prev["roll"] = True
            if prev["next_c"] is None and cur["adj"] and prev["adj"]:
                prev["next_c"] = cur["c"] * prev["adj"] / cur["adj"]
            if prev["next_c"] is None:
                prev["roll"] = False
                cur["break"] = True
    return out


class Market(object):
    """What the engines need. Every method returns None when the data is not there (never a guess)."""
    source = "none"

    def futures(self, underlying, start=None, end=None):
        return None

    def daily_many(self, symbols, start=None, end=None):
        return {}

    def index(self, name, start=None, end=None):
        return []

    def members(self, index, day):
        return None

    def results(self, symbol, start=None, end=None):
        return None

    def sectors(self):
        return None

    def intraday(self, symbol, start=None, end=None):
        return None

    def has_members(self):
        return False

    def has_results(self):
        return False


class ArchiveMarket(Market):
    def __init__(self, root, api, method="none", sectors_path=None, roll_sessions=1):
        self.root, self.api, self.method = root, api, method
        self.roll_sessions = roll_sessions
        self.source = "trader-archive at %s" % root
        self.sectors_path = sectors_path
        self._members = {}

    def lot_fn(self, underlying):
        """(day, expiry) -> lot from that day's F&O board, else the lot-size snapshot on or before it."""
        cache = {}

        def get(day, expiry):
            if day not in cache:
                b = self.api.load_fo_chain_eod(underlying, day, root=self.root) or {}
                cache[day] = {f["expiry"]: f.get("lot") for f in b.get("futures") or []}
            lot = cache[day].get(expiry)
            if not lot:
                lot = (self.api.load_lots(day, root=self.root).get(str(underlying).upper()) or {}).get(expiry[:7])
            return lot
        return get

    def futures(self, underlying, start=None, end=None):
        fn = getattr(self.api, "load_continuous", None)
        if fn is not None:
            try:
                res = fn(underlying, "none", start, end, roll="days:%d" % self.roll_sessions, root=self.root)
            except TypeError:
                res = fn(underlying, self.method, start, end)
            except Exception:       # the archive has no futures of it in that span
                res = None
            rows = from_load_continuous(res)
            if rows:
                return futures.Continuous(underlying, rows, "load_continuous(%s, none, days:%d) with its roll log"
                                          % (underlying, self.roll_sessions), self.lot_fn(underlying))
        days = self.api.trading_days(start, end, root=self.root)
        boards = []
        for d in days:
            b = self.api.load_fo_chain_eod(underlying, d, root=self.root)
            if b and b.get("futures"):
                boards.append(b)
        rows = futures.build_continuous(boards, underlying)
        if not rows:
            return None
        return futures.Continuous(underlying, rows, "built from the archive's F&O boards (roll the session before "
                                                    "expiry)")

    def daily_many(self, symbols, start=None, end=None):
        return self.api.load_daily_many(list(symbols), start, end, root=self.root)

    def index(self, name, start=None, end=None):
        return self.api.load_index(INDEX_ALIASES.get(str(name).upper(), name), start, end, root=self.root)

    def has_members(self):
        return getattr(self.api, "members", None) is not None

    def members(self, index, day):
        key = (index, day)
        if index == "F&O":
            if key not in self._members:
                self._members[key] = self.fno_on(day)
            return self._members[key]
        fn = getattr(self.api, "members", None)
        if fn is None:
            return None
        if key not in self._members:
            try:
                got = fn(index, day, root=self.root)
            except TypeError:
                got = fn(index, day)
            except Exception:       # the membership is not built for this index or this date
                got = None
            self._members[key] = sorted(got) if got is not None else None
        return self._members[key]

    def fno_on(self, day):
        """The F&O stocks on a day: the symbols with a stock future in the latest F&O bhavcopy on or before it."""
        st = self.api.Store(self.root)
        dates = [k for k in st.dates("fo") if k <= day]
        if not dates:
            return None
        with gzip.open(st.path("fo", dates[-1]), "rt", encoding="utf-8") as fh:
            rows = csv.reader(fh.read().splitlines()[1:])
            return sorted(set(r[0] for r in rows if len(r) > 1 and r[1] == "STF"))

    def has_results(self):
        return getattr(self.api, "results_dates", None) is not None

    def results(self, symbol, start=None, end=None):
        fn = getattr(self.api, "results_dates", None)
        if fn is None:
            return None
        try:
            got = fn(symbol, start, end, root=self.root)
        except TypeError:
            got = fn(symbol, start, end)
        except Exception:           # no results dataset yet
            return None
        out = []
        for x in got or []:
            if not isinstance(x, dict):
                out.append({"date": str(x)[:10]})
            elif x.get("event_session"):
                # the archive already knows the first session that could react: use it as the date
                out.append({"date": x["event_session"], "announced": x.get("announced_at")})
            else:
                out.append({"date": x["date"], "time": "15:30" if x.get("after_close") else None})
        return out

    def sectors(self):
        fn = getattr(self.api, "sectors", None)
        if fn is not None:
            return fn()
        for p in (self.sectors_path, os.path.join(self.root, "sectors.json")):
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    return json.load(fh)
        return None

    def intraday(self, symbol, start=None, end=None):
        try:
            bars = self.api.load_intraday(symbol, "1m", start, end, root=self.root)
        except Exception:
            return None
        out = {}
        for b in bars:
            out.setdefault(b["date"], []).append(b)
        return out or None


class FolderMarket(Market):
    def __init__(self, folder, api=None, bars_dir=None):
        self.folder, self.api = folder, api
        self.bars_dir = bars_dir or folder
        self.source = "folder %s" % folder
        self._json = {}

    def _read(self, name):
        if name not in self._json:
            p = os.path.join(self.folder, name)
            self._json[name] = None
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    self._json[name] = json.load(fh)
        return self._json[name]

    def _bars(self, sym, start, end):
        for ext in (".csv.gz", ".csv"):
            p = os.path.join(self.bars_dir, "%s-1d%s" % (sym, ext))
            if os.path.isfile(p):
                bars = core.L().read_bars_csv(p)
                return [b for b in bars if (not start or b["date"] >= start) and (not end or b["date"] <= end)]
        return None

    def futures(self, underlying, start=None, end=None):
        u = str(underlying).upper()
        for ext in (".csv.gz", ".csv"):
            p = os.path.join(self.folder, "continuous", u + ext)
            if os.path.isfile(p):
                opener = gzip.open if p.endswith(".gz") else open
                with opener(p, "rt", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(io.StringIO(fh.read())))
                rows = [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
                return futures.Continuous(u, normalize_continuous(rows), "continuous/%s%s" % (u, ext))
        arch = os.path.join(self.folder, "archive")
        if self.api is not None and os.path.isdir(arch):
            return ArchiveMarket(arch, self.api).futures(u, start, end)
        return None

    def daily_many(self, symbols, start=None, end=None):
        out = {}
        for s in symbols:
            b = self._bars(s, start, end)
            if b:
                out[s] = b
        return out

    def index(self, name, start=None, end=None):
        n = INDEX_ALIASES.get(str(name).upper(), name)
        return self._bars(INDEX_FILES.get(n, n), start, end) or []

    def has_members(self):
        return self._read("members.json") is not None

    def members(self, index, day):
        m = self._read("members.json")
        if not m or index not in m:
            return None
        snaps = sorted(m[index])
        k = bisect.bisect_right(snaps, day) - 1
        return sorted(m[index][snaps[k]]) if k >= 0 else []

    def has_results(self):
        return self._read("results.json") is not None

    def results(self, symbol, start=None, end=None):
        r = self._read("results.json")
        if r is None:
            return None
        return [x for x in r.get(symbol, []) if (not start or x["date"] >= start) and (not end or x["date"] <= end)]

    def sectors(self):
        return self._read("sectors.json")
