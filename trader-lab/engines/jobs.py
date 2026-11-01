"""From a pre-registered edge-hunt hypothesis to engine runs: one Job per family. A Job loads its data only up
to the date it is given (the executor passes the validation's last day until the holdout is opened), so
nothing it computes before then can depend on a holdout price.

    job.blockers()           what is missing before it can run (data, an engine), in plain words
    job.load(until)          read the data up to and including `until`
    job.run(params, a, b, m) one engine run over the registered window a..b (ISO dates), costs x m
    job.coverage(a, b)       (first session, last session, sessions) of the loaded data in a..b
"""

import datetime
import re

from . import core, events, futures, market, pairs, portfolio

FAMILIES_V2 = ("momentum-xs", "low-volatility", "reversal", "trend-ts", "calendar", "overnight", "pead", "pairs",
               "intraday-momentum")
FAMILIES_OPTIONS = ("vrp-selling", "event-vol", "option-buying")
UNIVERSES = {"nifty50": "NIFTY 50", "nifty100": "NIFTY 100", "nifty200": "NIFTY 200", "nifty500": "NIFTY 500",
             "fno": "F&O"}
WARMUP_DAYS = 560          # calendar days of history read before the first window (252 + 21 sessions and more)
FUTURES_CAPITAL = 5000000.0
STOCK_CAPITAL = 1000000.0


class Blocked(core.EngineError):
    pass


def _shift(iso_day, days):
    return (core.day(iso_day) + datetime.timedelta(days=days)).isoformat()


def universe_index(doc, params=None):
    """The index whose point-in-time members are the universe ("NIFTY 200"), from the registration."""
    if params and params.get("universe") in UNIVERSES:
        return UNIVERSES[params["universe"]]
    name = doc["universe"]["name"]
    for k in sorted(UNIVERSES, key=len, reverse=True):
        if name.startswith(k):
            return UNIVERSES[k]
    return None


class Job(object):
    engine = "?"

    def __init__(self, doc, mkt, cfg=None):
        self.doc, self.mkt, self.cfg = doc, mkt, dict(cfg or {})
        self.span = doc["span"]
        self.start = _shift(self.span["train"]["from"], -WARMUP_DAYS)
        self.until = None
        self.notes = []
        self.capital = float(self.cfg.get("capital") or self.default_capital())

    def default_capital(self):
        return STOCK_CAPITAL

    def blockers(self):
        return []

    def coverage(self, a, b):
        days = core.window(self.days(), a, b)
        return (days[0], days[-1], len(days)) if days else (None, None, 0)

    def days(self):
        return []

    def idx(self, a, b):
        days = self.days()
        import bisect
        i = bisect.bisect_left(days, a)
        j = bisect.bisect_right(days, b) - 1
        if i > j or i >= len(days):
            raise Blocked("no sessions in %s to %s" % (a, b))
        return i, j


# ---------------------------------------------------------------- futures families

class FuturesJob(Job):
    engine = "futures"

    def default_capital(self):
        return FUTURES_CAPITAL

    def underlyings(self):
        u = self.doc["parameters"].get("underlying") or [self.doc["instrument"]["underlying"]]
        return [str(x).upper() for x in u]

    def blockers(self):
        return []

    def load(self, until):
        self.until = until
        self.series = {}
        for u in self.underlyings():
            s = self.mkt.futures(u, self.start, until)
            if s is None or not s.n:
                raise Blocked("no %s futures data from %s to %s: the run waits for the history (trader-archive's "
                              "load_continuous: backfill the F&O bhavcopies)" % (u, self.start, until))
            self.series[u] = s
            self.notes.append("%s futures: %s" % (u, s.source))

    def days(self):
        s = self.series.get(self.underlyings()[0]) if getattr(self, "series", None) else None
        return s.days if s else []

    def run(self, p, a, b, m=1.0):
        u = str(p.get("underlying") or self.underlyings()[0]).upper()
        s = self.series[u]
        i, j = self._idx(s, a, b)
        intents = self.intents(s, p, i, j)
        r = futures.simulate(s, intents, i, j, self.capital, self.cfg, m)
        r["extra"]["intents"] = len(intents)
        self.after(s, r, intents, i, j)
        return r

    def _idx(self, s, a, b):
        import bisect
        i = bisect.bisect_left(s.days, a)
        j = bisect.bisect_right(s.days, b) - 1
        if i > j:
            raise Blocked("no %s sessions in %s to %s" % (s.underlying, a, b))
        return i, j

    def after(self, s, r, intents, i, j):
        pass


class TrendJob(FuturesJob):
    def intents(self, s, p, i, j):
        return futures.trend_intents(s, i, j, p["signal"], p["mode"])


class CalendarJob(FuturesJob):
    def intents(self, s, p, i, j):
        if "entry_session" in p:
            return events.turn_of_month(s.days, i, j, int(p["entry_session"]), int(p["exit_session"]))
        n = int(p["entry_sessions_before"])
        if p["event"] == "pre_holiday":
            return events.pre_holiday(s.days, i, j, n)
        if p["event"] == "monthly_expiry_week":
            return events.expiry_week(s.days, s.expiry, i, j, n)
        raise core.EngineError("event is pre_holiday or monthly_expiry_week")

    def after(self, s, r, intents, i, j):
        r["extra"]["in_window"] = events.in_window_vs_other(s, intents, i, j)


class OvernightJob(FuturesJob):
    def intents(self, s, p, i, j):
        return events.overnight(s, i, j, p["condition"])


class IntradayJob(FuturesJob):
    def blockers(self):
        return []

    def load(self, until):
        self.until = until
        self.bars, self.prev, self.series = {}, {}, {}
        for u in self.underlyings():
            got = self.mkt.intraday(u, self.start, until)
            if not got:
                raise Blocked("no 1-minute bars for %s (index minute bars from Jan 2022 with a broker token, or "
                              "futures minute bars when the long-history archive has them)" % u)
            self.bars[u] = got
            daily = self.mkt.index(u, self.start, until)
            closes = [(x["date"], x["c"]) for x in daily]
            self.prev[u] = {closes[k][0]: closes[k - 1][1] for k in range(1, len(closes))}
            fs = self.mkt.futures(u, self.start, until)
            self.series[u] = fs

    def days(self):
        u = self.underlyings()[0]
        return sorted(self.bars.get(u, {})) if getattr(self, "bars", None) else []

    def run(self, p, a, b, m=1.0):
        u = str(p.get("underlying") or self.underlyings()[0]).upper()
        bars = {d: v for d, v in self.bars[u].items() if a <= d <= b}
        fs = self.series.get(u)
        default_lot = {"NIFTY": 75, "BANKNIFTY": 30}.get(u, 1)

        def lot_of(d):
            k = fs.index.get(d) if fs else None
            return (fs.lot_at(k) if k is not None else None) or default_lot
        r = futures.intraday_momentum(bars, self.prev[u], lot_of, u, p, self.capital, self.cfg, m)
        if not fs:
            r["notes"].append("lot sizes: %d for every day (no futures series to read them from)" % default_lot)
        return r


# ---------------------------------------------------------------- stock families

class StockJob(Job):
    """A panel of stocks with point-in-time membership."""
    engine = "portfolio"

    def index_name(self, p=None):
        return universe_index(self.doc, p)

    def indexes(self):
        names = set()
        for v in self.doc["parameters"].get("universe") or [None]:
            names.add(universe_index(self.doc, {"universe": v} if v else None))
        return sorted(n for n in names if n)

    def blockers(self):
        out = []
        if self.doc["universe"].get("point_in_time") and not self.mkt.has_members():
            out.append("point-in-time index membership (trader-archive's members(index, date), backfill --datasets "
                       "members): until then a cross-sectional run would carry survivorship bias, so it is refused")
        return out

    def load(self, until):
        self.until = until
        syms = set()
        months = []
        d = core.day(self.start)
        end = core.day(until)
        while d <= end:
            months.append(d.isoformat())
            d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        months.append(until)
        for ix in self.indexes():
            for m in months:
                got = self.mkt.members(ix, m)
                if got is None:
                    raise Blocked("no %s membership on %s" % (ix, m))
                syms.update(got)
        self.extra_symbols(syms)
        bars = self.mkt.daily_many(sorted(syms), self.start, until)
        idx = self.mkt.index("NIFTY 50", self.start, until)
        days = sorted(x["date"] for x in idx) if idx else sorted(set(b["date"] for v in bars.values() for b in v))
        self.index_bars = idx
        self.panel = portfolio.Panel(days, bars)
        for ix in self.indexes():
            note = self.mkt.membership_note(ix)
            if note and note not in self.notes:
                self.notes.append(note)
        missing = sorted(syms - set(bars))
        if missing:
            self.notes.append("%d member symbols have no bars in the data (%s%s)" % (
                len(missing), ", ".join(missing[:8]), "..." if len(missing) > 8 else ""))

    def extra_symbols(self, syms):
        pass

    def days(self):
        return self.panel.days if getattr(self, "panel", None) else []

    def members_fn(self, p):
        ix = self.index_name(p)
        return lambda day: self.mkt.members(ix, day) or []


class PortfolioJob(StockJob):
    def translate(self, p):
        fam = self.doc["family"]
        q = {}
        if fam == "momentum-xs":
            q = {"signal": "momentum", "lookback": int(p["lookback_months"]) * 21, "skip": int(p["skip_months"]) * 21,
                 "top_n": int(p["top_n"]), "rebalance": p["rebalance"],
                 "volatility_adjusted": bool(p.get("volatility_adjusted")), "benchmark": "universe_ew"}
        elif fam == "low-volatility":
            q = {"signal": "low_volatility", "vol_window": int(p["volatility_window_days"]), "top_n": int(p["top_n"]),
                 "rebalance": p["rebalance"], "benchmark": "universe_ew"}
        elif fam == "reversal":
            ls = p.get("instrument") == "futures_long_short"
            q = {"signal": "reversal", "lookback": int(p["lookback_sessions"]), "top_n": int(p["bottom_n"]),
                 "rebalance": int(p["hold_sessions"]), "side": "long_short" if ls else "long",
                 "product": "FUT" if ls else "CNC", "benchmark": "universe_ew"}
        for k in ("weights", "band", "top_fraction"):
            if k in p:
                q[k] = p[k]
        return q

    def run(self, p, a, b, m=1.0):
        i, j = self.idx(a, b)
        return portfolio.run(self.panel, self.members_fn(p), self.translate(p), i, j, self.capital, self.cfg, m,
                             self.index_bars)


class EventJob(StockJob):
    engine = "events"

    def blockers(self):
        out = StockJob.blockers(self)
        if not self.mkt.has_results():
            out.append("results dates history (trader-archive's results_dates(symbol, from, to), backfill the "
                       "results dataset)")
        return out

    def load(self, until):
        StockJob.load(self, until)
        self.results = {}
        for s in sorted(self.panel.c):
            got = self.mkt.results(s, self.start, until)
            if got:
                self.results[s] = got
        rule = self.doc["universe"].get("rule", "")
        m = re.search(r"₹\s?(\d+(?:\.\d+)?)\s*crore", rule)
        self.floor = float(m.group(1)) * 1e7 if m else None
        w = re.search(r"(\d+)-session median", rule)
        self.floor_window = int(w.group(1)) if w else 20

    def run(self, p, a, b, m=1.0):
        i, j = self.idx(a, b)
        return events.results_drift(self.panel, self.members_fn(p), self.results, p, i, j, self.capital, self.cfg,
                                    m, liquidity_floor=self.floor, liquidity_window=self.floor_window)


class PairsJob(StockJob):
    engine = "pairs"

    def indexes(self):
        return ["F&O"]

    def index_name(self, p=None):
        return "F&O"

    def blockers(self):
        out = []                # the F&O list on a day comes from that day's bhavcopy (or members.json)
        if not self.mkt.sectors():
            out.append("a sector classification (sectors.json: {symbol: sector}, or the archive's sectors())")
        return out

    def load(self, until):
        StockJob.load(self, until)
        self.sectors = self.mkt.sectors() or {}
        self._pairs = {}

    def chosen(self, formation):
        """The pairs, chosen once per formation length on the train window only."""
        if formation not in self._pairs:
            ta, tb = self.idx(self.span["train"]["from"], self.span["train"]["to"])
            mem = set(self.mkt.members("F&O", self.span["train"]["to"]) or [])
            self._pairs[formation] = pairs.select(self.panel, self.sectors, ta, tb, formation, members=mem)
        return self._pairs[formation]

    def run(self, p, a, b, m=1.0):
        i, j = self.idx(a, b)
        return pairs.run(self.panel, self.chosen(int(p["formation_sessions"])), p, i, j, self.capital, self.cfg, m)


JOBS = {"trend-ts": TrendJob, "calendar": CalendarJob, "overnight": OvernightJob, "intraday-momentum": IntradayJob,
        "momentum-xs": PortfolioJob, "low-volatility": PortfolioJob, "reversal": PortfolioJob, "pead": EventJob,
        "pairs": PairsJob}


def job_for(doc, mkt, cfg=None):
    fam = doc["family"]
    if fam in FAMILIES_OPTIONS:
        raise Blocked("family %s needs the options edge engine (issue #73: VRP timing, event straddles, intraday "
                      "option plays); this executor does not run it yet" % fam)
    if fam not in JOBS:
        raise Blocked("no engine for family %s" % fam)
    return JOBS[fam](doc, mkt or market.Market(), cfg)
