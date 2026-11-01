"""Options backtests for trader-lab, on the local archive's end-of-day F&O bhavcopies. Standard library only.

lab.py loads this package by path and binds it to two modules it passes in (nothing here imports another
skill): the lab itself (charges, statistics, walk-forward, DSR, PBO, gates, the verdict) and
trader-strategy's script (the leg templates, strike picking, Black-76 and the SPAN-style margin).

    spec      a JSON spec with "kind": "options": underlying, structure (a trader-strategy template), the
              strike rule (delta, % out of the money or points from ATM, on the entry day's EOD chain with
              IV solved from settle prices), entry timing (days to expiry, weekdays, IV rank, India VIX),
              exits (profit target % of the premium, stop as a multiple of it, days before expiry, else
              cash settlement), lots, capital, costs, grid, walk-forward and gates
    data      every session's EOD chain of the underlying from the archive (settle, close, open, volume,
              OI and lot per contract), India VIX and NIFTY for the regimes
    backtest  day by day: enter at the entry day's close (or open), mark every leg at each later close
              (the contract's settle price when it traded that day; Black-76 at that day's smile IV
              when it did not, flagged), exit on a rule at the close with slippage, or settle in cash at
              expiry at the final settlement price (STT on exercise for in-the-money longs)
    run       the same robustness machinery as equity specs, plus return on margin, worst week, tail
              losses, losing streaks, time in drawdown and the share of positions whose short strike
              was breached

Options edge v2 (#73), loaded by path from here: vol.py (realised vol, HAR-RV, IV - RV), events.py (implied vs
realised moves around results, the event calendar) and engines.py (the edge-hunt executor's option
families; trader-lab/engines/jobs.py's OptionsJob calls them).

EOD only: no intraday stops, no adjustments, strikes that never traded are priced by a model.
Education and tooling, not financial advice. Nothing here places an order.
"""

import bisect
import datetime
import math
import os

ENV = {"lab": None, "strategy": None, "archive": None, "regime": None}
TICK = 0.05
MAX_DTE_LOADED = 100          # expiries further out than this many calendar days are not kept
STRIKE_BAND = 0.25            # strikes within +/-25% of spot are kept
STRUCTURES = ("short-strangle", "short-straddle", "iron-condor", "iron-fly", "bull-put-spread",
              "bear-call-spread", "calendar", "long-straddle", "long-strangle")
CREDIT = ("short-strangle", "short-straddle", "iron-condor", "iron-fly", "bull-put-spread", "bear-call-spread")
STRIKE_RULES = ("delta", "pct_otm", "points", "atm")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
OPTION_KEYS = {"schema", "name", "title", "description", "kind", "timeframe", "product", "underlying", "structure",
               "expiry", "params", "strikes", "wings", "entry", "exit", "lots", "capital", "costs", "grid",
               "walk_forward", "gates", "trials_tried", "seed", "monte_carlo", "regimes", "references", "notes",
               "iv_rank", "max_open", "type", "rate", "charges"}
OPTION_COSTS = {"slippage_pct": 2.0, "slippage_ticks": 1}
OPTION_WF = {"in_sample": 120, "out_of_sample": 60}
SPEC_SCHEMA = "bharat-trader/lab-spec/1"


class OptionsError(ValueError):
    pass


def bind(lab, strategy=None, archive=None, regime=None):
    """Point this package at the lab module and the sibling skills' scripts it is handed: trader-strategy
    (legs, Black-76, margin), trader-archive (the chains) and trader-regime (the market regime split and
    its IV rank formula). Any but the lab may be None until needed."""
    ENV["lab"] = lab
    for k, v in (("strategy", strategy), ("archive", archive), ("regime", regime)):
        if v is not None:
            ENV[k] = v


def _L():
    return ENV["lab"]


def _S():
    s = ENV["strategy"]
    if s is None:
        raise _L().SpecError("options backtests use trader-strategy's legs and margin: put the trader-strategy "
                             "folder next to trader-lab")
    return s


def _err(path, msg):
    raise _L().SpecError("%s: %s" % (path or "spec", msg))


# ---------------------------------------------------------------- the spec

def _num(params, x, path, lo=None, hi=None, integer=False, allow_none=False):
    if x is None and allow_none:
        return None
    if isinstance(x, str) and x.startswith("$"):
        if x[1:] not in params:
            _err(path, "unknown param %r (declare it in \"params\")" % x)
        x = params[x[1:]]
    L = _L()
    if not L._is_num(x):
        _err(path, "want a number, not %r" % (x,))
    if integer and float(x) != int(x):
        _err(path, "want a whole number, not %r" % (x,))
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        _err(path, "%r is outside %s to %s" % (x, lo, hi))
    return int(x) if integer else float(x)


def validate(doc, params=None):
    """An options spec (dict) -> a checked, compiled copy (lab.SpecError with the path of the problem)."""
    L = _L()
    if not isinstance(doc, dict):
        raise L.SpecError("a spec is a JSON object")
    extra = set(doc) - OPTION_KEYS
    if extra:
        _err("spec", "unknown keys %s for an options spec (known: %s)" % (sorted(extra)[:5],
                                                                          ", ".join(sorted(OPTION_KEYS))))
    name = doc.get("name")
    if not isinstance(name, str) or not L.SPEC_NAME.match(name):
        _err("name", "lowercase letters, digits and dashes, 2 to 48 long")
    if doc.get("timeframe", "1d") != "1d":
        _err("timeframe", "options backtests run on end-of-day chains: 1d")
    under = doc.get("underlying")
    if not isinstance(under, str) or not L.SYMBOL.match(under):
        _err("underlying", "an NSE F&O underlying such as NIFTY or BANKNIFTY")
    structure = str(doc.get("structure") or "").strip().lower()
    if structure not in STRUCTURES:
        _err("structure", "one of %s (trader-strategy's templates)" % ", ".join(STRUCTURES))
    expiry = doc.get("expiry", "weekly")
    if expiry not in ("weekly", "monthly"):
        _err("expiry", "weekly (the nearest listed expiry) or monthly (an expiry with a future)")
    base = doc.get("params") or {}
    if not isinstance(base, dict):
        _err("params", "an object of name: number")
    for k, v in base.items():
        if not L.PARAM_NAME.match(str(k)) or not L._is_num(v):
            _err("params.%s" % k, "a param is a lowercase name with a number")
    p = dict(base)
    if params:
        unknown = set(params) - set(base)
        if unknown:
            _err("params", "unknown params %s" % sorted(unknown))
        p.update(params)

    st = doc.get("strikes") or {"by": "atm"}
    if not isinstance(st, dict) or st.get("by") not in STRIKE_RULES or set(st) - {"by", "value"}:
        _err("strikes", "{\"by\": \"delta\"|\"pct_otm\"|\"points\"|\"atm\", \"value\": x}")
    by = st["by"]
    sval = None
    if by == "delta":
        sval = _num(p, st.get("value"), "strikes.value", 0.01, 0.6)
    elif by == "pct_otm":
        sval = _num(p, st.get("value"), "strikes.value", 0, 30)
    elif by == "points":
        sval = _num(p, st.get("value"), "strikes.value", 0, 20000)
    if structure in ("short-straddle", "iron-fly", "long-straddle") and by not in ("atm",):
        _err("strikes", "%s sells or buys the ATM strike: use {\"by\": \"atm\"}" % structure)
    wings = doc.get("wings")
    wing = None
    if structure in ("iron-condor", "iron-fly", "bull-put-spread", "bear-call-spread"):
        if not isinstance(wings, dict) or set(wings) - {"points"}:
            _err("wings", "{\"points\": n}: the distance from each short strike to its bought wing")
        wing = _num(p, wings.get("points"), "wings.points", 1, 20000)
    elif wings is not None:
        _err("wings", "%s has no bought wings" % structure)
    opt_type = str(doc.get("type") or "CE").upper()
    if opt_type not in ("CE", "PE"):
        _err("type", "CE or PE (the calendar's option type)")

    en = doc.get("entry") or {}
    known = {"days_to_expiry", "weekdays", "fill", "iv_rank_min", "iv_rank_max", "vix_min", "vix_max"}
    if not isinstance(en, dict) or set(en) - known:
        _err("entry", "an object with %s" % ", ".join(sorted(known)))
    dte = _num(p, en.get("days_to_expiry"), "entry.days_to_expiry", 0, 90, True)
    days = en.get("weekdays")
    if days is not None and (not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days)):
        _err("entry.weekdays", "a list from %s" % ", ".join(WEEKDAYS))
    fill = en.get("fill", "close")
    if fill not in ("close", "open"):
        _err("entry.fill", "close (the entry day's closing settle prices) or open (the day's opening prices)")
    if fill == "open" and by == "delta":
        _err("entry.fill", "an open fill picks strikes by points or % from the index's open, not by delta")
    ivr = doc.get("iv_rank") or {}
    if not isinstance(ivr, dict) or set(ivr) - {"lookback", "days"}:
        _err("iv_rank", "{\"lookback\": sessions, \"days\": the constant maturity in calendar days}")
    entry = {"dte": dte, "weekdays": list(days) if days else None, "fill": fill,
             "iv_rank_min": _num(p, en.get("iv_rank_min"), "entry.iv_rank_min", 0, 100, allow_none=True),
             "iv_rank_max": _num(p, en.get("iv_rank_max"), "entry.iv_rank_max", 0, 100, allow_none=True),
             "vix_min": _num(p, en.get("vix_min"), "entry.vix_min", 0, 200, allow_none=True),
             "vix_max": _num(p, en.get("vix_max"), "entry.vix_max", 0, 200, allow_none=True)}
    ex = doc.get("exit") or {}
    if not isinstance(ex, dict) or set(ex) - {"profit_target_pct", "stop_loss_mult", "days_before_expiry"}:
        _err("exit", "an object with profit_target_pct, stop_loss_mult, days_before_expiry")
    exits = {"target_pct": _num(p, ex.get("profit_target_pct"), "exit.profit_target_pct", 1, 1000, allow_none=True),
             "stop_mult": _num(p, ex.get("stop_loss_mult"), "exit.stop_loss_mult", 0.1, 50, allow_none=True),
             "exit_dte": _num(p, ex.get("days_before_expiry", 0), "exit.days_before_expiry", 0, 60, True)}
    if exits["exit_dte"] and exits["exit_dte"] >= max(dte, 1) and structure != "calendar":
        _err("exit.days_before_expiry", "must be under entry.days_to_expiry, or the trade never runs")
    if structure == "calendar" and entry["fill"] != "close":
        _err("entry.fill", "a calendar enters at the close")
    lots = _num(p, doc.get("lots", 1), "lots", 1, 50, True)
    capital = _num(p, doc.get("capital", 500000), "capital", 10000, 1e10)
    costs = dict(OPTION_COSTS)
    for k, v in (doc.get("costs") or {}).items():
        if k not in OPTION_COSTS:
            _err("costs", "known: slippage_pct (of the premium), slippage_ticks (per leg)")
        costs[k] = _num(p, v, "costs." + k, 0, 100)
    gates = dict(L.GATE_DEFAULTS)
    for k, v in (doc.get("gates") or {}).items():
        if k not in L.GATE_DEFAULTS:
            _err("gates", "unknown gate %r (known: %s)" % (k, ", ".join(L.GATE_DEFAULTS)))
        gates[k] = _num(p, v, "gates." + k)
    grid = doc.get("grid") or {}
    if not isinstance(grid, dict):
        _err("grid", "an object of param: [values]")
    combos = 1
    for k, vals in grid.items():
        if k not in base:
            _err("grid.%s" % k, "a grid varies a declared param")
        if not isinstance(vals, list) or not 1 <= len(vals) <= 12 or not all(L._is_num(x) for x in vals):
            _err("grid.%s" % k, "a list of 1 to 12 numbers")
        combos *= len(set(vals) | {base[k]})
    if combos > L.MAX_GRID:
        _err("grid", "%d points: keep the grid at %d or fewer" % (combos, L.MAX_GRID))
    wf = dict(OPTION_WF)
    for k, v in (doc.get("walk_forward") or {}).items():
        if k not in ("in_sample", "out_of_sample"):
            _err("walk_forward", "in_sample and out_of_sample, in sessions")
        wf[k] = _num(p, v, "walk_forward." + k, 2, 2000, True)
    rg = doc.get("regimes") or {}
    vix_edges = rg.get("vix", [13, 16, 20])
    if not isinstance(vix_edges, list) or not all(L._is_num(x) for x in vix_edges) or vix_edges != sorted(vix_edges):
        _err("regimes.vix", "rising India VIX bucket edges, e.g. [13, 16, 20]")
    mc = doc.get("monte_carlo") or {}
    charges = doc.get("charges") or {}
    if not isinstance(charges, dict) or set(charges) - {"OPT", "FUT"}:
        _err("charges", "{\"OPT\": {rate: value}} overrides of the dated F&O charges")
    return {
        "kind": "options", "name": name, "title": str(doc.get("title") or name)[:120],
        "description": str(doc.get("description") or ""), "timeframe": "1d", "product": "NRML",
        "underlying": under, "universe": [under], "index": under in _index_names(),
        "structure": structure, "expiry": expiry, "strike_by": by, "strike_value": sval, "wing": wing,
        "type": opt_type, "entry": entry, "exit": exits, "lots": lots, "capital": capital, "costs": costs,
        "params": p, "base_params": dict(base), "grid": grid, "walk_forward": wf, "gates": gates,
        "iv_lookback": _num(p, ivr.get("lookback", 252), "iv_rank.lookback", 20, 1000, True),
        "iv_days": _num(p, ivr.get("days", 30), "iv_rank.days", 7, 90, True),
        "max_open": _num(p, doc.get("max_open", 1), "max_open", 1, 10, True),
        "rate": _num(p, doc.get("rate", 0.0557), "rate", 0, 0.2),
        "cfg": {"capital": capital, "fno_charges": charges, "fno_stocks": [under]},
        "vix_edges": [float(x) for x in vix_edges],
        "mc_runs": _num(p, mc.get("runs", 1000), "monte_carlo.runs", 100, 20000, True),
        "trials_tried": _num(p, doc.get("trials_tried", 0), "trials_tried", 0, 100000, True),
        "seed": _num(p, doc.get("seed", 16), "seed", 0, 2 ** 31, True), "lookback": 0, "raw": doc,
    }


def _index_names():
    s = ENV["strategy"]
    return tuple(getattr(s, "INDICES", None) or ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"))


# ---------------------------------------------------------------- data: EOD chains from the archive

def _d(s):
    return datetime.date.fromisoformat(s)


def _dte(day, expiry):
    return (_d(expiry) - _d(day)).days


class Chains(object):
    """Every session's end-of-day board of one underlying, trimmed to the contracts a backtest can use.

    day -> {"spot", "futures": {expiry: settle}, "opts": {(expiry, strike, "CE"|"PE"): (settle, close, open,
    volume, oi, lot)}, "expiries": [...], "monthly": set of expiries with a future}. Index open and close
    (for open fills and the final settlement fallback) and India VIX ride along."""

    def __init__(self, underlying, boards, index_bars=None, vix=None, source=""):
        self.underlying = underlying
        self.days = sorted(boards)
        self.boards = boards
        self.index = {b["date"]: b for b in index_bars or []}
        self.vix = {b["date"]: b["c"] for b in vix or [] if b.get("c") is not None}
        self.source = source
        self._an = {}
        self._iv30 = None

    def board(self, day):
        return self.boards.get(day)

    def price(self, day, expiry, strike, kind):
        """(premium per unit, source) for one contract at the day's close: "settle" when it traded that day,
        else Black-76 at that day's smile IV ("model"), else None."""
        b = self.boards.get(day)
        if b is None:
            return None, "no data"
        row = b["opts"].get((expiry, strike, kind))
        if row and row[3] > 0 and (row[0] or row[1]):
            return (row[0] or row[1]), "settle"
        a = self.analysis(day, expiry)
        if a is None:
            return None, "no chain"
        S = _S()
        iv, _ = S.smile_iv(a, kind, strike)
        if iv is None:
            return None, "no iv"
        return S.black76_price(a["forward"], strike, a["t_years"], a["rate"], iv / 100.0, kind), "model"

    def open_price(self, day, expiry, strike, kind, prev_day, index_open):
        """The contract's opening price that day when it traded, else Black-76 at the previous session's smile
        IV on the index's open with the day's whole session left ("model")."""
        b = self.boards.get(day)
        row = b["opts"].get((expiry, strike, kind)) if b else None
        if row and row[3] > 0 and row[2]:
            return row[2], "open"
        a = self.analysis(prev_day, expiry) if prev_day else None
        if a is None:
            return None, "no chain"
        S = _S()
        iv, _ = S.smile_iv(a, kind, strike)
        if iv is None:
            return None, "no iv"
        T = (_dte(day, expiry) * 1440 + 375) / 525600.0
        return S.black76_price(index_open, strike, T, a["rate"], iv / 100.0, kind), "model"

    def forward(self, day, expiry, rate):
        """The parity forward F = K + e^(rT) (C - P), the median over the 5 traded strikes nearest the
        spot; else the expiry's future settle; else the spot."""
        key = ("fwd", day, expiry, rate)
        if key not in self._an:
            self._an[key] = self._forward(day, expiry, rate)
        return self._an[key]

    def _forward(self, day, expiry, rate):
        b = self.boards[day]
        T = max(0, _dte(day, expiry)) / 365.0
        spot = b["spot"]
        if T <= 0:
            return spot
        ks = sorted({k for (e, k, t) in b["opts"] if e == expiry})
        both = []
        for k in ks:
            c, p = b["opts"].get((expiry, k, "CE")), b["opts"].get((expiry, k, "PE"))
            if c and p and c[3] > 0 and p[3] > 0 and (c[0] or c[1]) and (p[0] or p[1]):
                both.append(k)
        if spot:
            both.sort(key=lambda k: abs(k - spot))
        near = both[:5]
        if near:
            fs = sorted(k + math.exp(rate * T) * ((b["opts"][(expiry, k, "CE")][0] or b["opts"][(expiry, k, "CE")][1])
                                                  - (b["opts"][(expiry, k, "PE")][0] or b["opts"][(expiry, k, "PE")][1]))
                        for k in near)
            return fs[len(fs) // 2]
        return b["futures"].get(expiry) or spot

    def analysis(self, day, expiry, rate=None):
        """A trader-options-shaped analysis of one expiry at the day's close, IV solved from settle prices,
        so trader-strategy's templates can pick strikes and price legs on it. None without a board."""
        key = (day, expiry)
        if key in self._an:
            return self._an[key]
        b = self.boards.get(day)
        if b is None or expiry not in b["expiries"]:
            self._an[key] = None
            return None
        S = _S()
        r = S.DEFAULT_RATE if rate is None else rate
        dte = _dte(day, expiry)
        T = max(0.0, dte / 365.0)
        F = self.forward(day, expiry, r)
        ks = sorted({k for (e, k, t) in b["opts"] if e == expiry})
        rows = []
        for k in ks:
            row = {"strike": k}
            for kind in ("CE", "PE"):
                rec = b["opts"].get((expiry, k, kind))
                if not rec:
                    row[kind.lower()] = None
                    continue
                px = rec[0] or rec[1]
                traded = rec[3] > 0 and px
                iv = solve_iv(px, F, k, T, r, kind) if traded and T > 0 else None
                g = S.black76_greeks(F, k, T, r, iv / 100.0, kind) if iv else {"delta": None}
                row[kind.lower()] = {"price": px if traded else None, "price_source": "settle" if traded else None,
                                     "iv": iv, "delta": g.get("delta"), "oi": rec[4], "volume": rec[3],
                                     "bid": None, "ask": None, "settle": rec[0]}
            rows.append(row)
        if not rows:
            self._an[key] = None
            return None
        atm = min(ks, key=lambda k: (abs(k - F), k))
        arow = next(x for x in rows if x["strike"] == atm)
        ivs = [x["iv"] for x in (arow.get("ce"), arow.get("pe")) if x and x.get("iv")]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
        if atm_iv is None:
            sm = [x for x in rows if (x.get("ce") or {}).get("iv") or (x.get("pe") or {}).get("iv")]
            if sm:
                near = min(sm, key=lambda x: abs(x["strike"] - F))
                atm_iv = ((near.get("ce") or {}).get("iv") or (near.get("pe") or {}).get("iv"))
        lot = None
        for (e, k, t), rec in b["opts"].items():
            if e == expiry and rec[5]:
                lot = rec[5]
                break
        move = F * (atm_iv or 0) / 100.0 * math.sqrt(T) if atm_iv else None
        a = {"underlying": self.underlying, "expiry": expiry, "forward": F, "spot": b["spot"], "rows": rows,
             "t_years": T, "rate": r, "atm": atm, "atm_iv": atm_iv, "lot_size": lot,
             "kind": "index" if self.underlying in _index_names() else "stock",
             "expected_move": {"move": move} if move else {}, "asof": "%sT15:30:00" % day,
             "sources": {"chain": "NSE F&O bhavcopy %s (settle prices), IV solved by Black-76" % day}}
        self._an[key] = a
        return a

    def atm_iv(self, day, expiry, rate):
        """The ATM IV (%) of one expiry at the day's close: the mean of the call's and the put's IV at the
        traded strike nearest the forward (only those two are solved)."""
        key = ("atm", day, expiry)
        if key in self._an:
            return self._an[key]
        b = self.boards.get(day)
        out = None
        if b is not None and expiry in b["expiries"]:
            T = max(0, _dte(day, expiry)) / 365.0
            F = self.forward(day, expiry, rate)
            ks = sorted({k for (e, k, t) in b["opts"] if e == expiry}, key=lambda k: (abs(k - F), k))
            for k in ks[:3]:
                ivs = []
                for kind in ("CE", "PE"):
                    rec = b["opts"].get((expiry, k, kind))
                    if rec and rec[3] > 0 and (rec[0] or rec[1]):
                        v = solve_iv(rec[0] or rec[1], F, k, T, rate, kind)
                        if v:
                            ivs.append(v)
                if ivs:
                    out = sum(ivs) / len(ivs)
                    break
        self._an[key] = out
        return out

    def iv_series(self, days=30, rate=0.0557):
        """{day: constant-maturity ATM IV (%)}: total variance interpolated between the two listed expiries
        that bracket `days` calendar days (the nearer one alone when nothing is further out)."""
        if self._iv30 is not None and self._iv30[0] == days:
            return self._iv30[1]
        out = {}
        for d in self.days:
            b = self.boards[d]
            exps = [e for e in b["expiries"] if _dte(d, e) >= 2]
            if not exps:
                continue
            lo = [e for e in exps if _dte(d, e) <= days]
            hi = [e for e in exps if _dte(d, e) > days]
            e1 = lo[-1] if lo else None
            e2 = hi[0] if hi else None
            v1 = self.atm_iv(d, e1, rate) if e1 else None
            v2 = self.atm_iv(d, e2, rate) if e2 else None
            if v1 and v2:
                t1, t2 = _dte(d, e1), _dte(d, e2)
                w1 = v1 * v1 * t1
                w2 = v2 * v2 * t2
                var = w1 + (w2 - w1) * (days - t1) / float(t2 - t1)
                out[d] = math.sqrt(max(var, 0.0) / days)
            elif v1 or v2:
                out[d] = v1 or v2
        self._iv30 = (days, out)
        return out

    def iv_rank(self, day, lookback, days=30, rate=0.0557):
        """(IV rank 0-100 over the last `lookback` sessions up to `day`, IV now) or (None, IV) when there is
        less than half the lookback of history."""
        s = self.iv_series(days, rate)
        now = s.get(day)
        if now is None:
            return None, None
        i = bisect.bisect_right(self.days, day)
        hist = [s[d] for d in self.days[max(0, i - lookback):i] if d in s]
        if len(hist) < max(10, lookback // 2):
            return None, now
        reg = ENV["regime"]
        if reg is not None and hasattr(reg, "range_rank"):
            rank = reg.range_rank(hist, now)            # trader-regime's (and trader-options') IV rank
            return (50.0 if rank is None else rank), now
        lo, hi = min(hist), max(hist)
        return (100.0 * (now - lo) / (hi - lo) if hi > lo else 50.0), now

    def final_settlement(self, expiry):
        """The final settlement price on an expiry day: the expiring future's settle (NSE's FSP), else the
        underlying's close that day. (price, source) or (None, why)."""
        b = self.boards.get(expiry)
        if b is None:
            return None, "no data for the expiry day"
        if b["futures"].get(expiry):
            return b["futures"][expiry], "the expiring future's final settlement price"
        bar = self.index.get(expiry)
        if bar and bar.get("c"):
            return bar["c"], "the index close on expiry day"
        return b["spot"], "the underlying price in the bhavcopy"


def trim_board(chain, keep_dte=MAX_DTE_LOADED, band=STRIKE_BAND, monthly_only=False):
    """trader-archive's load_fo_chain_eod() -> the compact board Chains keeps (monthly_only: only the expiries
    that have a future, NSE's monthly contracts)."""
    day, spot = chain["date"], chain.get("spot")
    futures, monthly = {}, set()
    for f in chain.get("futures") or []:
        if f.get("kind") in ("IDF", "STF") and f.get("expiry"):
            futures[f["expiry"]] = f.get("settle") or f.get("close")
            monthly.add(f["expiry"])
    opts, exps = {}, set()
    for o in chain.get("options") or []:
        e, k, t = o.get("expiry"), o.get("strike"), o.get("option_type")
        if not e or k is None or t not in ("CE", "PE"):
            continue
        if monthly_only and e not in monthly:
            continue
        dte = _dte(day, e)
        if dte < 0 or dte > keep_dte:
            continue
        if spot and abs(k - spot) > band * spot:
            continue
        opts[(e, float(k), t)] = (o.get("settle") or 0.0, o.get("close") or 0.0, o.get("open") or 0.0,
                                  int(o.get("volume") or 0), int(o.get("oi") or 0), o.get("lot"))
        exps.add(e)
    return {"spot": spot, "futures": futures, "opts": opts, "expiries": sorted(exps), "monthly": monthly}


def load_chains(underlying, root, archive, start=None, end=None, progress=None, band=STRIKE_BAND,
                keep_dte=MAX_DTE_LOADED, monthly_only=False):
    """Chains for one underlying from a trader-archive folder (offline). archive: trader-archive's module.
    band, keep_dte, monthly_only: what trim_board keeps (a long history needs less of each board)."""
    days = archive.trading_days(start, end, root=root)
    boards = {}
    for i, d in enumerate(days):
        ch = archive.load_fo_chain_eod(underlying, d, root=root)
        if ch and ch.get("options"):
            boards[d] = trim_board(ch, keep_dte, band, monthly_only)
        if progress and i % 100 == 99:
            progress("loaded %d of %d EOD chains" % (i + 1, len(days)))
    if not boards:
        raise _L().LabError("no %s option chains in the archive at %s: backfill it first "
                            "(archive.py backfill --years 2 --datasets fo,index,vix,lots)" % (underlying, root))
    try:
        idx = archive.load_index(underlying, start, end, root=root) if underlying in _index_names() else []
    except Exception:  # an index the archive has no name for
        idx = []
    try:
        vix = archive.load_vix(start, end, root=root)
    except Exception:
        vix = []
    ds = sorted(boards)
    return Chains(underlying, boards, idx, vix, "archive %s to %s (%d EOD chains)" % (ds[0], ds[-1], len(ds)))


def solve_iv(price, F, K, T, r, kind, lo=0.005, hi=5.0):
    """Black-76 implied volatility (%) by bisection, or None when the price is at or under the discounted
    intrinsic value, above the forward, or T is zero."""
    if price is None or price <= 0 or T <= 0 or F <= 0:
        return None
    S = _S()
    df = math.exp(-r * T)
    intrinsic = df * max(0.0, (F - K) if kind == "CE" else (K - F))
    if price <= intrinsic + 1e-6 or price >= (df * F if kind == "CE" else df * K):
        return None
    a, b = lo, hi
    fa = S.black76_price(F, K, T, r, a, kind) - price
    fb = S.black76_price(F, K, T, r, b, kind) - price
    if fa > 0 or fb < 0:
        return None
    for _ in range(60):
        m = 0.5 * (a + b)
        fm = S.black76_price(F, K, T, r, m, kind) - price
        if fm > 0:
            b = m
        else:
            a = m
        if b - a < 1e-6:
            break
    return round(50.0 * (a + b), 4)


# ---------------------------------------------------------------- picking the position

def _step(a):
    return _S().strike_step(a)


def _round_to(x, step):
    return round(x / step) * step


def template_params(spec, a, centre=None):
    """The trader-strategy template params for this spec's strike rule on analysis a."""
    S = _S()
    st = spec["structure"]
    step = _step(a)
    F = a["forward"]
    by, v = spec["strike_by"], spec["strike_value"]
    p = {"lots": spec["lots"]}
    if spec["wing"]:
        p["width"] = spec["wing"]
    if centre is not None:
        F = centre
    if st in ("short-strangle", "iron-condor", "long-strangle"):
        if by == "delta":
            p["delta"] = v
        elif by == "points":
            p["wings"] = max(step, _round_to(v, step))
        elif by == "pct_otm":
            p["wings"] = max(step, _round_to(F * v / 100.0, step))
        else:
            p["wings"] = step
        if centre is not None and "wings" in p:
            p["strike"] = S.nearest_strike(a, centre)
    elif st in ("short-straddle", "iron-fly", "long-straddle"):
        p["strike"] = S.nearest_strike(a, F)
    elif st in ("bull-put-spread", "bear-call-spread"):
        kind = "PE" if st == "bull-put-spread" else "CE"
        sgn = -1 if kind == "PE" else 1
        if by == "delta":
            best = None
            for x in a["rows"]:
                side = x.get(kind.lower())
                if side and side.get("price") is not None and side.get("delta") is not None:
                    gap = abs(abs(side["delta"]) - v)
                    if best is None or gap < best[0]:
                        best = (gap, x["strike"])
            if best is None:
                raise OptionsError("no traded %s strike with a delta" % kind)
            p["strike"] = best[1]
        elif by == "points":
            p["strike"] = S.nearest_strike(a, F + sgn * v, kind)
        elif by == "pct_otm":
            p["strike"] = S.nearest_strike(a, F * (1 + sgn * v / 100.0), kind)
        else:
            p["strike"] = S.nearest_strike(a, F, kind)
    elif st == "calendar":
        p["type"] = spec["type"]
        sgn = 1 if spec["type"] == "CE" else -1
        if by == "points":
            p["strike"] = S.nearest_strike(a, F + sgn * v)
        elif by == "pct_otm":
            p["strike"] = S.nearest_strike(a, F * (1 + sgn * v / 100.0))
        else:
            p["strike"] = S.nearest_strike(a, F)
    return p


def pick_legs(spec, chains, day, expiry, far=None):
    """The position's legs on `day` for `expiry`, through trader-strategy's template and strike picking
    on the EOD analysis (prices are that day's settle prices; a leg with no trade that day is priced by
    Black-76 and flagged)."""
    S = _S()
    a = chains.analysis(day, expiry, spec["rate"])
    if a is None:
        raise OptionsError("no chain for %s on %s" % (expiry, day))
    if not a.get("lot_size"):
        raise OptionsError("no lot size for %s %s" % (spec["underlying"], expiry))
    markets = {expiry: a}
    if spec["structure"] == "calendar":
        if not far:
            raise OptionsError("no later expiry for the calendar's far leg")
        fa = chains.analysis(day, far, spec["rate"])
        if fa is None:
            raise OptionsError("no chain for the far expiry %s" % far)
        markets[far] = fa
    params = template_params(spec, a)
    specs = S.template_specs(spec["structure"], params, a)
    legs = S.resolve_legs(specs, markets, near=expiry, fill="mid", lot_size=a["lot_size"])
    return legs, a


def _sign(leg):
    return 1 if leg["side"] == "BUY" else -1


def slip(price, costs, mult=1.0):
    return (price * costs["slippage_pct"] / 100.0 + costs["slippage_ticks"] * TICK) * mult


def _fill(price, side, costs, mult):
    """A fill worse than the mark by the slippage: pay more to buy, get less to sell (never under a tick)."""
    s = slip(price, costs, mult)
    return price + s if side == "BUY" else max(TICK, price - s)


def leg_charges(cfg, leg, entry_day, exit_day, exit_px, settled, mult=1.0):
    """Charges of one option leg, each side dated on its own day (STT changed on 1 April 2026).
    settled: the leg reached expiry: an in-the-money long is exercised (STT on the intrinsic value at
    the exercise rate, no closing order), anything else expires with no closing order."""
    L = _L()
    q = leg["qty"]
    px = leg["fill"]
    if leg["side"] == "BUY":
        opening = L.fno_round_trip_charges(px, 0, q, cfg, "OPT", entry_day)["total"]
        if settled:
            closing = L.fno_round_trip_charges(0, exit_px, q, cfg, "OPT", exit_day, True)["total"] if exit_px > 0 else 0.0
        else:
            closing = L.fno_round_trip_charges(0, exit_px, q, cfg, "OPT", exit_day)["total"]
    else:
        opening = L.fno_round_trip_charges(0, px, q, cfg, "OPT", entry_day)["total"]
        closing = 0.0 if settled else L.fno_round_trip_charges(exit_px, 0, q, cfg, "OPT", exit_day)["total"]
    return (opening + closing) * mult


def margin_for(legs, kind, entry_day, expiry, held_to_expiry_day):
    """trader-strategy's SPAN-style estimate at entry, and on the expiry day itself (2% more ELM on short
    index options) when the position is still open then: the larger is the margin it blocks."""
    S = _S()
    pos = S.Position(legs, {}, entry_day)
    at = datetime.datetime.combine(_d(entry_day), datetime.time(15, 30))
    m = S.span_style(pos, kind, None, at)
    out = {"entry": m["total"], "expiry_day": None, "scan_risk": m["scan_risk"], "exposure": m["exposure"],
           "premium_paid": m["premium_paid"], "error_pct": m["error_pct"]}
    if held_to_expiry_day and entry_day != expiry:
        e = S.span_style(pos, kind, None, datetime.datetime.combine(_d(expiry), datetime.time(9, 15)))
        out["expiry_day"] = e["total"]
    elif entry_day == expiry:
        out["expiry_day"] = m["total"]
    out["required"] = max(out["entry"], out["expiry_day"] or 0.0)
    return out


# ---------------------------------------------------------------- the backtest

def expiries_for(spec, chains):
    """The expiries this spec trades, oldest first: weekly = every listed expiry, monthly = those with a
    listed future (NSE's monthly contracts)."""
    seen = set()
    for d in chains.days:
        b = chains.boards[d]
        seen.update(b["monthly"] if spec["expiry"] == "monthly" else b["expiries"])
    return sorted(seen)


def entry_day_for(spec, chains, expiry):
    """The session a cycle is decided on: the first session at or under days_to_expiry before this expiry
    (on an allowed weekday), when the session before it was still further out. None when the cycle's
    window starts before the data does."""
    n = spec["entry"]["dte"]
    days = chains.days
    first = days[0] if days else None
    if first is None or _dte(first, expiry) <= n:
        return None
    k = bisect.bisect_left(days, (_d(expiry) - datetime.timedelta(days=n)).isoformat())
    wd = spec["entry"]["weekdays"]
    while k < len(days) and days[k] <= expiry:
        d = days[k]
        wday = _d(d).weekday()
        if not wd or (wday < 5 and WEEKDAYS[wday] in wd):
            return d
        k += 1
    return None


def _far_expiry(spec, chains, day, expiry):
    b = chains.boards[day]
    pool = sorted(b["monthly"]) if spec["expiry"] == "monthly" else b["expiries"]
    later = [e for e in pool if e > expiry]
    return later[0] if later else None


def filters_ok(spec, chains, day):
    """(ok, why not, facts) for the regime filters on the entry day's close."""
    en = spec["entry"]
    facts = {}
    if en["iv_rank_min"] is not None or en["iv_rank_max"] is not None:
        rank, iv = chains.iv_rank(day, spec["iv_lookback"], spec["iv_days"], spec["rate"])
        facts["iv_rank"] = None if rank is None else round(rank, 1)
        facts["iv"] = None if iv is None else round(iv, 2)
        if rank is None:
            return False, "no IV rank yet (too little history)", facts
        if en["iv_rank_min"] is not None and rank < en["iv_rank_min"]:
            return False, "IV rank under the minimum", facts
        if en["iv_rank_max"] is not None and rank > en["iv_rank_max"]:
            return False, "IV rank over the maximum", facts
    if en["vix_min"] is not None or en["vix_max"] is not None:
        v = chains.vix.get(day)
        facts["vix"] = v
        if v is None:
            return False, "no India VIX that day", facts
        if en["vix_min"] is not None and v < en["vix_min"]:
            return False, "India VIX under the minimum", facts
        if en["vix_max"] is not None and v > en["vix_max"]:
            return False, "India VIX over the maximum", facts
    return True, "", facts


def open_position(spec, chains, day, expiry, mult):
    """A new position at the entry day's close (or open), or (None, why)."""
    S = _S()
    far = _far_expiry(spec, chains, day, expiry) if spec["structure"] == "calendar" else None
    flags = []
    if spec["entry"]["fill"] == "open":
        i = bisect.bisect_left(chains.days, day)
        prev = chains.days[i - 1] if i > 0 else None
        bar = chains.index.get(day) or {}
        io = bar.get("o")
        if not prev or not io:
            return None, "no previous chain or index open"
        try:
            legs, a = pick_legs(spec, chains, prev, expiry, far)
        except (OptionsError, S.StrategyError) as e:
            return None, "no legs: %s" % e
        # re-centre on today's open: the same distances from the ATM strike at the index's open
        shift = _round_to(io - a["atm"], _step(a))
        for l in legs:
            k = l["strike"] + shift
            px, src = chains.open_price(day, l["expiry"], k, l["option_type"], prev, io)
            if px is None:
                return None, "no opening price for %s %s" % (k, l["option_type"])
            l.update({"strike": k, "price": px, "price_source": src, "forward": io,
                      "t_years": (_dte(day, l["expiry"]) * 1440 + 375) / 525600.0})
            if src == "model":
                flags.append("%s %s opened untraded: Black-76 at the previous smile" % (k, l["option_type"]))
    else:
        try:
            legs, a = pick_legs(spec, chains, day, expiry, far)
        except (OptionsError, S.StrategyError) as e:
            return None, "no legs: %s" % e
        for l in legs:
            if l["price_source"] == "model":
                flags.append("%s %s did not trade on entry: Black-76" % (l["strike"], l["option_type"]))
    for l in legs:
        if l["instrument"] != "OPT":
            return None, "a futures leg is not modelled"
        if l["price"] is None or l["price"] <= 0:
            return None, "a leg has no price"
        l["mark"] = l["price"]
        l["fill"] = round(_fill(l["price"], l["side"], spec["costs"], mult), 4)
    premium = sum(-_sign(l) * l["fill"] * l["qty"] for l in legs)
    return {"entry_date": day, "expiry": expiry, "legs": legs, "premium": premium, "flags": flags,
            "credit": premium > 0, "lot_size": legs[0]["lot_size"], "lots": spec["lots"], "far": far,
            "spot_entry": chains.boards[day]["spot"], "forward_entry": a["forward"], "atm_iv": a.get("atm_iv"),
            "marks": {}, "models": sum(1 for l in legs if l["price_source"] == "model"), "breached": False}, ""


def _value(pos, chains, day, settle_near=None):
    """Mark the position at the day's close: (gross P&L per the fills, per-leg prices, models used).
    settle_near: the final settlement price when the near legs expire that day."""
    gross, prices, models = 0.0, [], 0
    for l in pos["legs"]:
        if settle_near is not None and l["expiry"] == pos["expiry"]:
            px = max(0.0, settle_near - l["strike"]) if l["option_type"] == "CE" else max(0.0, l["strike"] - settle_near)
            src = "settlement"
        else:
            px, src = chains.price(day, l["expiry"], l["strike"], l["option_type"])
            if px is None:
                px, src = l.get("mark", l["price"]), "stale"
                models += 1
            elif src == "model":
                models += 1
        prices.append((px, src))
        gross += _sign(l) * (px - l["fill"]) * l["qty"]
    return gross, prices, models


def close_position(spec, pos, chains, day, reason, prices, mult, settled):
    """Book the exit: fills with slippage (none at settlement), charges per leg, the margin and R."""
    gross, charges, legs_out = 0.0, 0.0, []
    for l, (px, src) in zip(pos["legs"], prices):
        at_settle = settled and l["expiry"] == pos["expiry"]
        if at_settle:
            fill = px
        else:
            fill = _fill(px, "SELL" if l["side"] == "BUY" else "BUY", spec["costs"], mult) if px > 0 else 0.0
        g = _sign(l) * (fill - l["fill"]) * l["qty"]
        c = leg_charges(spec["cfg"], l, pos["entry_date"], day, fill, at_settle, mult)
        gross += g
        charges += c
        legs_out.append({"side": l["side"], "option_type": l["option_type"], "strike": l["strike"],
                         "expiry": l["expiry"], "qty": l["qty"], "entry": round(l["fill"], 2),
                         "entry_source": l["price_source"], "exit": round(fill, 2), "exit_source": src,
                         "iv": l.get("iv"), "gross": round(g, 2), "charges": round(c, 2)})
    L = _L()
    kind = "index" if spec["index"] else "stock"
    held = day >= pos["expiry"]
    m = margin_for(pos["legs"], kind, pos["entry_date"], pos["expiry"], held)
    net = gross - charges
    sessions = sum(1 for d in chains.days if pos["entry_date"] < d <= day)
    return {"symbol": spec["underlying"], "signal_date": pos["entry_date"], "entry_date": pos["entry_date"],
            "entry_time": "09:15" if spec["entry"]["fill"] == "open" else "15:30",
            "exit_date": day, "exit_time": "15:30", "expiry": pos["expiry"], "reason": reason,
            "structure": spec["structure"], "lots": pos["lots"], "lot_size": pos["lot_size"],
            "premium": round(pos["premium"], 2), "credit": pos["credit"],
            "gross": L.round_paise(gross), "charges": L.round_paise(charges), "net": L.round_paise(net),
            "margin": round(m["required"], 2), "margin_detail": {k: (round(v, 2) if isinstance(v, float) else v)
                                                                  for k, v in m.items()},
            "r": round(net / m["required"], 5) if m["required"] > 0 else 0.0,
            "return_on_margin_pct": round(100.0 * net / m["required"], 3) if m["required"] > 0 else None,
            "bars": sessions, "days_held": (_d(day) - _d(pos["entry_date"])).days,
            "spot_entry": pos["spot_entry"], "spot_exit": chains.boards[day]["spot"] if day in chains.boards else None,
            "atm_iv_entry": None if pos["atm_iv"] is None else round(pos["atm_iv"], 2),
            "breached": pos["breached"], "model_marks": pos["models"], "flags": pos["flags"][:6],
            "legs": legs_out, "facts": pos.get("facts") or {}}


def _breach(pos, spot):
    """The underlying closed beyond a short strike of the near expiry. When the short call and put share a
    strike (straddle, fly), one of them is always in the money: there the line is the strike plus or minus
    the premium a unit (the breakeven at expiry)."""
    if spot is None:
        return False
    shorts = [l for l in pos["legs"] if l["side"] == "SELL" and l["expiry"] == pos["expiry"]]
    same = len({l["strike"] for l in shorts}) == 1 and len({l["option_type"] for l in shorts}) == 2
    pad = max(0.0, pos["premium"]) / float(shorts[0]["qty"]) if same and shorts else 0.0
    for l in shorts:
        if (l["option_type"] == "CE" and spot > l["strike"] + pad) or (l["option_type"] == "PE" and spot < l["strike"] - pad):
            return True
    return False


def backtest(spec, chains, cost_mult=1.0):
    """Run a compiled options spec over the chains. Pure: no files, no network.

    {"trades", "skipped": {why: n}, "pending": [decisions on the last session], "open": [still open],
     "mtm": {day: the open positions' gross P&L marked at the close, plus realised net so far}}"""
    trades, skipped, pending = [], {}, []
    by_day = {}
    for e in expiries_for(spec, chains):
        d = entry_day_for(spec, chains, e)
        if d is not None:
            by_day.setdefault(d, []).append(e)
    open_pos = []
    last = chains.days[-1] if chains.days else None
    realised, mtm = 0.0, {}
    tgt, stop, xdte = spec["exit"]["target_pct"], spec["exit"]["stop_mult"], spec["exit"]["exit_dte"]
    for day in chains.days:
        still = []
        for pos in open_pos:
            near_exp = pos["expiry"]
            settle = None
            if day >= near_exp:
                settle, how = chains.final_settlement(near_exp) if day == near_exp else (None, "")
                if settle is None and day > near_exp:
                    # the expiry day is missing from the archive: settle at this session's spot, flagged
                    settle = chains.boards[day]["spot"]
                    pos["flags"].append("expiry day %s missing: settled at %s's price" % (near_exp, day))
            gross, prices, models = _value(pos, chains, day, settle)
            pos["models"] += models
            spot = chains.boards[day]["spot"] if settle is None else settle
            if _breach(pos, spot):
                pos["breached"] = True
            reason = None
            if settle is not None:
                reason = "expiry settlement"
            else:
                base = abs(pos["premium"]) or 1.0
                if tgt is not None and gross >= base * tgt / 100.0:
                    reason = "profit target"
                elif stop is not None and gross <= -base * stop:
                    reason = "stop"
                elif xdte and _dte(day, near_exp) <= xdte:
                    reason = "days before expiry"
                if spec["structure"] == "calendar" and reason is None and day >= near_exp:
                    reason = "expiry settlement"
            if reason:
                t = close_position(spec, pos, chains, day, reason, prices, cost_mult, settle is not None)
                trades.append(t)
                realised += t["net"]
            else:
                for l, (px, src) in zip(pos["legs"], prices):
                    l["mark"] = px
                pos["unreal"] = gross
                still.append(pos)
        open_pos = still
        for e in by_day.get(day, []):
            if len(open_pos) >= spec["max_open"]:
                skipped["a position already open"] = skipped.get("a position already open", 0) + 1
                continue
            ok, why, facts = filters_ok(spec, chains, day)
            if not ok:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            if day == last and spec["entry"]["fill"] == "close":
                pending.append({"date": day, "expiry": e, "facts": facts})
            pos, why = open_position(spec, chains, day, e, cost_mult)
            if pos is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            pos["facts"] = facts
            if day == e:
                # entered on expiry day (an open fill): settles at the day's final settlement price
                settle, _ = chains.final_settlement(e)
                gross, prices, models = _value(pos, chains, day, settle)
                pos["breached"] = _breach(pos, settle)
                t = close_position(spec, pos, chains, day, "expiry settlement", prices, cost_mult, True)
                trades.append(t)
                realised += t["net"]
                continue
            pos["unreal"] = 0.0
            open_pos.append(pos)
        mtm[day] = realised + sum(p.get("unreal", 0.0) for p in open_pos)
    trades.sort(key=lambda t: (t["exit_date"], t["signal_date"], t["expiry"]))
    return {"trades": trades, "skipped": skipped, "pending": pending, "mtm": mtm,
            "open": [{"entry_date": p["entry_date"], "expiry": p["expiry"], "premium": round(p["premium"], 2),
                      "unrealised": round(p.get("unreal", 0.0), 2),
                      "legs": [{"side": l["side"], "option_type": l["option_type"], "strike": l["strike"],
                                "expiry": l["expiry"], "qty": l["qty"], "entry": round(l["fill"], 2)}
                               for l in p["legs"]]} for p in open_pos]}


# ---------------------------------------------------------------- options metrics

def options_metrics(trades, mtm, days, capital):
    """Return on margin, worst week, tail losses, streaks, time in drawdown, breaches; MTM where it matters."""
    L = _L()
    nets = [t["net"] for t in trades]
    margins = [t["margin"] for t in trades if t.get("margin")]
    roms = [t["return_on_margin_pct"] for t in trades if t.get("return_on_margin_pct") is not None]
    eq = [mtm.get(d, None) for d in days]
    last, curve = 0.0, []
    for v in eq:
        if v is not None:
            last = v
        curve.append(last)
    daily = [b - a for a, b in zip([0.0] + curve[:-1], curve)]
    weeks = {}
    for d, p in zip(days, daily):
        y, w, _ = _d(d).isocalendar()
        weeks[(y, w)] = weeks.get((y, w), 0.0) + p
    peak, under, dd = 0.0, 0, 0.0
    for v in curve:
        peak = max(peak, v)
        if v < peak - 1e-9:
            under += 1
        dd = max(dd, peak - v)
    streak = run = 0
    for x in nets:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    worst5 = sorted(nets)[:max(1, int(math.ceil(len(nets) * 0.05)))] if nets else []
    years = max(1e-9, len(days) / 252.0)
    avg_m = sum(margins) / len(margins) if margins else 0.0
    exp = {}
    for t in trades:
        exp.setdefault(t["expiry"], False)
        exp[t["expiry"]] = exp[t["expiry"]] or bool(t.get("breached"))
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "positions": len(trades), "avg_margin": L.round_paise(avg_m),
        "max_margin": L.round_paise(max(margins) if margins else 0.0),
        "return_on_margin_pct": round(100.0 * sum(nets) / avg_m, 2) if avg_m else None,
        "return_on_margin_annual_pct": round(100.0 * sum(nets) / avg_m / years, 2) if avg_m else None,
        "avg_return_on_margin_pct": round(sum(roms) / len(roms), 3) if roms else None,
        "worst_week": L.round_paise(min(weeks.values())) if weeks else 0.0,
        "best_week": L.round_paise(max(weeks.values())) if weeks else 0.0,
        "tail_loss_5pct": L.round_paise(L.percentile(nets, 0.05)) if nets else 0.0,
        "tail_loss_1pct": L.round_paise(L.percentile(nets, 0.01)) if nets else 0.0,
        "tail_avg_worst_5pct": L.round_paise(sum(worst5) / len(worst5)) if worst5 else 0.0,
        "worst_trade": L.round_paise(min(nets)) if nets else 0.0,
        "max_consecutive_losses": streak,
        "time_in_drawdown_pct": round(100.0 * under / len(curve), 2) if curve else 0.0,
        "max_drawdown_mtm": L.round_paise(dd), "max_drawdown_mtm_pct": round(100.0 * dd / capital, 3),
        "breach_pct": round(100.0 * sum(1 for v in exp.values() if v) / len(exp), 2) if exp else 0.0,
        "expiries": len(exp), "exits": reasons,
        "model_priced_marks": sum(t.get("model_marks", 0) for t in trades),
        "credit_positions": sum(1 for t in trades if t.get("credit")),
        "avg_premium": L.round_paise(sum(abs(t["premium"]) for t in trades) / len(trades)) if trades else 0.0,
    }


# ---------------------------------------------------------------- the whole run

def run(doc, chains, context=None, extra_trials=0, progress=None):
    """The full lab run of an options spec on loaded chains. Pure: returns the results dict (the lab's
    shape, plus "kind": "options" and an "options" block); nothing is written."""
    L = _L()
    spec = validate(doc)
    context = context or {}
    names, axes, pts, home = L.grid_points(spec)
    days = list(chains.days)
    if len(days) < 10:
        raise L.LabError("%d EOD chains is too little history for an options backtest" % len(days))
    cap = float(spec["capital"])
    runs = []
    for j, p in enumerate(pts):
        sp = validate(doc, params=p)
        runs.append({"params": p, "spec": sp, "res": backtest(sp, chains, 1.0)})
        if progress:
            progress("grid point %d of %d" % (j + 1, len(pts)))
    me = runs[home]
    full_trades = list(me["res"]["trades"])
    full = L.trade_stats(full_trades, days, cap)
    wf = spec["walk_forward"]
    splits = L.walk_forward_splits(days, wf["in_sample"], wf["out_of_sample"], 0)
    folds, oos_trades, oos_days, chosen = [], [], [], []
    for ins, outs in splits:
        iset, oset = set(ins), set(outs)
        is_tr = [[t for t in r["res"]["trades"] if t["signal_date"] in iset] for r in runs]
        is_net = [sum(t["net"] for t in ts) for ts in is_tr]
        able = [j for j in range(len(runs)) if len(is_tr[j]) >= L.MIN_IS_TRADES] or list(range(len(runs)))
        best = max(able, key=lambda j: (round(is_net[j], 2), j == home, -j))
        picked = [t for t in runs[best]["res"]["trades"] if t["signal_date"] in oset]
        oos_trades.extend(picked)
        oos_days.extend(outs)
        chosen.append(best)
        folds.append({"in_sample_days": [ins[0], ins[-1]], "out_of_sample_days": [outs[0], outs[-1]],
                      "params": {k: runs[best]["params"][k] for k in names}, "in_sample_net": L.round_paise(is_net[best]),
                      "stats": L.trade_stats(picked, outs, cap)})
    oos_trades.sort(key=lambda t: (t["exit_date"], t["signal_date"], t["expiry"]))
    oos = L.trade_stats(oos_trades, oos_days, cap)
    keys = ("trades", "net", "expectancy_r", "profit_factor")
    mults = sorted(set([1.5, 2.0, float(spec["gates"]["survive_cost_multiplier"])]))
    cost_full, cost_oos = {"1x": {k: full[k] for k in keys}}, {"1x": {k: oos[k] for k in keys}}
    for m in mults:
        cache = {j: backtest(runs[j]["spec"], chains, m)["trades"] for j in sorted(set(chosen) | {home})}
        st = L.trade_stats(cache[home], days, cap)
        cost_full[L._mkey(m)] = {k: st[k] for k in keys}
        ot = []
        for (ins, outs), j in zip(splits, chosen):
            oset = set(outs)
            ot.extend(t for t in cache[j] if t["signal_date"] in oset)
        st = L.trade_stats(ot, oos_days, cap)
        cost_oos[L._mkey(m)] = {k: st[k] for k in keys}
    grid_rows, trial_sr, cols = [], [], []
    for j, r in enumerate(runs):
        st = L.trade_stats(r["res"]["trades"], days, cap)
        om = options_metrics(r["res"]["trades"], r["res"]["mtm"], days, cap)
        grid_rows.append({"params": {k: r["params"][k] for k in names}, "trades": st["trades"], "net": st["net"],
                          "expectancy_r": st["expectancy_r"], "profit_factor": st["profit_factor"],
                          "max_drawdown_pct": st["max_drawdown_pct"],
                          "return_on_margin_pct": om["return_on_margin_pct"], "home": j == home})
        trial_sr.append(st["sharpe_daily"])
        cols.append(L.daily_pnl(r["res"]["trades"], days))
    near = L.neighbourhood(names, axes, pts, home, None)
    pos_share = sum(1 for j in near if grid_rows[j]["net"] > 0) / float(len(near))
    all_share = sum(1 for r in grid_rows if r["net"] > 0) / float(len(grid_rows))
    near_exp = L.percentile([grid_rows[j]["expectancy_r"] for j in near], 0.5)
    rets = [p / cap for p in L.daily_pnl(full_trades, days)]
    dsr = L.deflated_sharpe(rets, trial_sr if len(runs) > 1 else (), spec["trials_tried"] + extra_trials) \
        if len(rets) > 2 and L._std(rets) > 0 else None
    matrix = [list(row) for row in zip(*cols)] if cols else []
    nb = 8 if len(days) >= 32 else (4 if len(days) >= 8 else 2)
    pbo = L.pbo_cscv(matrix, nb) if len(runs) > 1 and matrix else None
    mc = L.monte_carlo([t["net"] for t in oos_trades], spec["mc_runs"], spec["seed"])
    regimes = L.regime_split(full_trades, context, spec["vix_edges"])
    regimes["iv_rank"] = _iv_rank_split(full_trades, chains, spec)
    regs = L.lab_regimes(context)
    if regs:
        regimes["market"] = L.market_regimes(full_trades, oos_trades, regs, int(spec["gates"]["min_regime_trades"]))
    gates = L.evaluate_gates(spec["gates"], oos, full, dsr, pbo, cost_oos, pos_share)
    if spec["gates"]["require_regime_edge"]:
        gates.append(L.regime_gate(spec["gates"], oos_trades, regs))
    om_full = options_metrics(full_trades, me["res"]["mtm"], days, cap)
    om_oos = options_metrics(oos_trades, _realised_curve(oos_trades, oos_days), oos_days, cap)
    return {
        "schema": L.SCHEMA_RESULTS, "kind": "options", "name": spec["name"], "title": spec["title"],
        "spec_hash": L.spec_hash(doc), "timeframe": "1d", "product": "NRML",
        "underlying": spec["underlying"], "structure": spec["structure"], "expiry_kind": spec["expiry"],
        "data": {"symbols": [spec["underlying"]], "missing": [], "from": days[0], "to": days[-1],
                 "sessions": len(days), "warmup_sessions": 0, "tested_from": days[0], "source": chains.source,
                 "context": sorted(k for k, v in context.items() if v is not None)},
        "params": {k: spec["params"][k] for k in sorted(spec["params"])},
        "config": {"capital": cap, "lots": spec["lots"], "max_loss_per_trade": None, "rate": spec["rate"]},
        "costs": spec["costs"], "rules": _rules_text(spec),
        "full": full, "skipped": me["res"]["skipped"],
        "walk_forward": {"in_sample": wf["in_sample"], "out_of_sample": wf["out_of_sample"], "folds": folds,
                         "stats": oos, "latest_params": folds[-1]["params"] if folds else {k: spec["params"][k] for k in names}},
        "grid": {"params": names, "points": grid_rows, "neighbourhood": [grid_rows[j]["params"] for j in near],
                 "neighbourhood_positive": round(pos_share, 4), "grid_positive": round(all_share, 4),
                 "neighbourhood_median_expectancy_r": round(near_exp, 4)},
        "costs_sensitivity": {"whole_sample": cost_full, "out_of_sample": cost_oos},
        "monte_carlo": mc, "dsr": dsr, "pbo": pbo, "regimes": regimes, "gates": gates, "gates_set": spec["gates"],
        "options": {"whole_sample": om_full, "out_of_sample": om_oos, "open": me["res"]["open"]},
        "trades": full_trades, "oos_trades": oos_trades,
        "equity": _equity_mtm(me["res"]["mtm"], days, cap, set(oos_days), full_trades),
    }


def _realised_curve(trades, days):
    out, run_ = {}, 0.0
    by = {}
    for t in trades:
        by[t["exit_date"]] = by.get(t["exit_date"], 0.0) + t["net"]
    for d in days:
        run_ += by.get(d, 0.0)
        out[d] = run_
    return out


def _equity_mtm(mtm, days, capital, oos_days, trades=()):
    """The equity curve marked to market each close (open positions at their EOD value), as lab rows."""
    L = _L()
    rows, peak, prev = [], float(capital), 0.0
    n_by = {}
    for t in trades:
        n_by[t["exit_date"]] = n_by.get(t["exit_date"], 0) + 1
    for d in days:
        v = mtm.get(d, prev)
        eq = L.round_paise(capital + v)
        peak = max(peak, eq)
        rows.append({"date": d, "pnl": L.round_paise(v - prev), "equity": eq,
                     "drawdown_pct": round((peak - eq) / capital * 100, 3), "trades": n_by.get(d, 0),
                     "out_of_sample": d in oos_days})
        prev = v
    return rows


def _iv_rank_split(trades, chains, spec):
    rows = {}
    for t in trades:
        rank, _ = chains.iv_rank(t["signal_date"], spec["iv_lookback"], spec["iv_days"], spec["rate"])
        lab = "IV rank unknown" if rank is None else ("IV rank under 33" if rank < 33 else
                                                      ("IV rank 33 to 66" if rank < 66 else "IV rank 66 and up"))
        rows.setdefault(lab, []).append(t)
    out = []
    for lab in sorted(rows):
        ts = rows[lab]
        nets = [x["net"] for x in ts]
        gl = -sum(x for x in nets if x <= 0)
        out.append({"regime": lab, "trades": len(ts), "net": _L().round_paise(sum(nets)),
                    "expectancy_r": round(sum(x["r"] for x in ts) / len(ts), 4),
                    "win_rate": round(sum(1 for x in nets if x > 0) / float(len(ts)), 4),
                    "profit_factor": round(sum(x for x in nets if x > 0) / gl, 3) if gl > 0 else None})
    return out


def _rules_text(spec):
    en, ex = spec["entry"], spec["exit"]
    by = {"delta": "%.2f delta" % (spec["strike_value"] or 0), "pct_otm": "%g%% out of the money" % (spec["strike_value"] or 0),
          "points": "%g points from ATM" % (spec["strike_value"] or 0), "atm": "the ATM strike"}[spec["strike_by"]]
    parts = ["%s %s on %s %s expiries, %d lot%s" % (spec["structure"], by, spec["underlying"], spec["expiry"],
                                                     spec["lots"], "" if spec["lots"] == 1 else "s")]
    if spec["wing"]:
        parts.append("wings %g points beyond the shorts" % spec["wing"])
    if en["dte"]:
        parts.append("enter at the %s %d calendar day%s before expiry" % (en["fill"], en["dte"], "" if en["dte"] == 1 else "s"))
    else:
        parts.append("enter at the %s on expiry day" % en["fill"])
    if en["weekdays"]:
        parts.append("on %s" % "/".join(en["weekdays"]))
    if en["iv_rank_min"] is not None:
        parts.append("IV rank at least %g" % en["iv_rank_min"])
    if en["iv_rank_max"] is not None:
        parts.append("IV rank at most %g" % en["iv_rank_max"])
    if en["vix_min"] is not None or en["vix_max"] is not None:
        parts.append("India VIX %s to %s" % (en["vix_min"] if en["vix_min"] is not None else "any",
                                             en["vix_max"] if en["vix_max"] is not None else "any"))
    if ex["target_pct"] is not None:
        parts.append("take profit at %g%% of the premium" % ex["target_pct"])
    if ex["stop_mult"] is not None:
        parts.append("stop at a loss of %gx the premium" % ex["stop_mult"])
    if ex["exit_dte"]:
        parts.append("out %d day%s before expiry" % (ex["exit_dte"], "" if ex["exit_dte"] == 1 else "s"))
    else:
        parts.append("else held to cash settlement")
    return "; ".join(parts)


# ---------------------------------------------------------------- the scorecard, options part

def scorecard_lines(res):
    """Extra markdown lines for an options scorecard."""
    L = _L()
    o, w = res["options"]["out_of_sample"], res["options"]["whole_sample"]

    def pct(x):
        return "n/a" if x is None else "%.2f%%" % x

    out = ["## Options numbers (after charges and slippage)", "",
           "| | whole sample | out of sample |", "| --- | --- | --- |"]
    for label, k, fmt in (("positions", "positions", str), ("average margin blocked", "avg_margin", L.inr),
                          ("return on margin, total", "return_on_margin_pct", pct),
                          ("return on margin, a year", "return_on_margin_annual_pct", pct),
                          ("return on margin, a trade", "avg_return_on_margin_pct", pct),
                          ("worst week (marked to market)", "worst_week", L.inr),
                          ("tail loss: 5th percentile trade", "tail_loss_5pct", L.inr),
                          ("tail loss: 1st percentile trade", "tail_loss_1pct", L.inr),
                          ("average of the worst 5% of trades", "tail_avg_worst_5pct", L.inr),
                          ("max consecutive losses", "max_consecutive_losses", str),
                          ("time in drawdown", "time_in_drawdown_pct", pct),
                          ("max drawdown, marked to market", "max_drawdown_mtm_pct", lambda x: "%.2f%% of capital" % x),
                          ("expiries with a short strike breached", "breach_pct", pct)):
        out.append("| %s | %s | %s |" % (label, fmt(w[k]), fmt(o[k])))
    out.append("")
    out.append("R for an options trade is its net result over the margin it blocked (the SPAN-style estimate, "
               "the larger of the entry day's and the expiry day's): the expectancy is the average return on "
               "margin a trade. Exits: %s." % (", ".join("%s %d" % kv for kv in sorted(w["exits"].items())) or "none"))
    if w["model_priced_marks"]:
        out.append("")
        out.append("%d leg marks used Black-76 at the day's smile IV because the strike did not trade that day "
                   "(flagged per trade)." % w["model_priced_marks"])
    out.append("")
    return out


def does_not_prove(res):
    d = res["data"]
    lines = [
        "It does not predict the future. It shows how these exact rules would have traded %d sessions (%s to %s) "
        "of %s end-of-day option chains, with the costs modelled below." % (d["sessions"], d["from"], d["to"],
                                                                            res["underlying"]),
        "End-of-day only: entries and exits happen at a session's settle (or open) prices. A stop is checked "
        "once a day at the close, so the intraday loss before it could be much larger; there are no "
        "adjustments, rolls or hedges during the day.",
        "Strikes that did not trade on a day are priced by Black-76 at that day's smile IV (flagged), and "
        "strike selection only sees strikes that traded: deep strikes that never traded are invisible "
        "(survivorship of strikes).",
        "Fills are a model: the settle price less %g%% of the premium and %d tick a leg. Real spreads on far "
        "strikes and on expiry day are often wider, and a large order moves them." % (
            res["costs"]["slippage_pct"], res["costs"]["slippage_ticks"]),
        "Margin is a SPAN-style estimate (NSE's 2026 scan on today's parameters), not the margin a broker "
        "blocked on those days; return on margin moves with it.",
        "Short options carry tail risk that a couple of years may not show: one gap like June 2024's election "
        "day can exceed many months of premium.",
        "Charges are a typical discount broker's with the STT rates in force on each trade date. Income tax is "
        "not included.",
        "A pass on every gate is evidence that survived some tests, not a guarantee. Most strategies that pass "
        "a backtest still fail live.",
    ]
    if d["sessions"] < 250:
        lines.insert(1, "%d sessions is a short sample for options: a few expiries either way change the numbers "
                        "a lot." % d["sessions"])
    return lines


# ---------------------------------------------------------------- the paper forward test

def forward(doc, chains, out, res, status, today=None, now=None, write=True):
    """Settle the spec's positions since the backtest's last day into forward.json and, for a WATCH or PROMOTE
    strategy whose entry rule fires on the latest session, draft the basket through trader-strategy: a
    strategy.json body and an orders.json proposal (paper, every leg a LIMIT at the close). Never places
    anything: trader-risk checks the basket as one position and every leg needs the user's typed yes."""
    L = _L()
    S = _S()
    now = now or L.ist_now()
    name = doc["name"]
    d = L.lab_dir(out, name)
    h = L.spec_hash(doc)
    params = dict(validate(doc)["base_params"])
    params.update(res["walk_forward"].get("latest_params") or {})
    spec = validate(doc, params=params)
    bt = backtest(spec, chains, 1.0)
    cutoff = res["data"]["to"]
    today = today or (chains.days[-1] if chains.days else now.date().isoformat())
    fwd_doc = L.read_json(os.path.join(d, "forward.json"), {}) or {}
    have = {(t.get("signal_date"), t.get("expiry")) for t in fwd_doc.get("trades") or []}
    new = [dict(t, spec_hash=h, settled=now.strftime("%Y-%m-%d %H:%M"), params=params)
           for t in bt["trades"] if t["signal_date"] > cutoff and (t["signal_date"], t["expiry"]) not in have]
    trades = (fwd_doc.get("trades") or []) + new
    trades.sort(key=lambda t: (t["exit_date"], t["signal_date"], t.get("expiry") or ""))
    mine = [t for t in trades if t.get("spec_hash") == h]
    prev = status.get("verdict") if status.get("spec_hash") == h else None
    v, why = L.decide(res["gates"], res["gates_set"], mine, res["walk_forward"]["stats"]["max_drawdown"], prev)
    fs = L.forward_summary(mine)
    baskets, notes = [], []
    if v in ("WATCH", "PROMOTE"):
        for p in bt["pending"]:
            if p["date"] != today:
                continue
            try:
                far = _far_expiry(spec, chains, today, p["expiry"]) if spec["structure"] == "calendar" else None
                legs, a = pick_legs(spec, chains, today, p["expiry"], far)
                markets = {p["expiry"]: a}
                if far:
                    markets[far] = chains.analysis(today, far, spec["rate"])
                body = S.evaluate(legs, markets, {"fno_charges": spec["cfg"]["fno_charges"]}, spec["structure"],
                                  template_params(spec, a), simulate=False)
            except (OptionsError, S.StrategyError) as e:
                notes.append("today's signal on %s could not be priced: %s" % (p["expiry"], e))
                continue
            body["lab"] = {"strategy": name, "spec_hash": h, "verdict": v, "decided_on": today,
                           "note": "a paper forward-test basket priced at %s's closing settle prices; the next "
                                   "session's prices will differ. Not a recommendation." % today}
            orders = S.orders_document(body, "NRML", now.strftime("%Y-%m-%dT%H:%M:%S+05:30"))
            orders["created_by"] = "trader-lab"
            orders["note"] = ("Paper proposals from trader-lab's forward test (%s, %s). Nothing is placed until "
                              "trader-risk passes the basket and you say yes to each leg. Multi-leg: place every "
                              "leg or none." % (name, v))
            baskets.append({"date": L.next_session(today), "decided_on": today, "expiry": p["expiry"],
                            "strategy": body, "orders": orders})
        for o in bt["open"]:
            if o["entry_date"] == today and spec["entry"]["fill"] == "open":
                notes.append("the lab's paper position for %s opened today at the open: too late to draft" % o["expiry"])
    else:
        notes.append("verdict %s: no paper baskets. %s" % (v, why))
    written = []
    if write:
        L.write_json(os.path.join(d, "forward.json"), {"schema": "bharat-trader/lab-forward/1", "name": name,
                                                       "updated": now.strftime("%Y-%m-%d %H:%M"), "trades": trades})
        L.update_status(out, name, v, why, h, now, fs)
        for b in baskets:
            base = os.path.join(d, "forward", b["decided_on"])
            L.write_json(os.path.join(base, "strategy-%s.json" % b["expiry"]), b["strategy"])
            L.write_json(os.path.join(base, "orders-%s.json" % b["expiry"]), b["orders"])
            written.append(os.path.join(base, "orders-%s.json" % b["expiry"]))
    return {"name": name, "kind": "options", "today": today, "verdict": v, "why": why, "settled": len(new),
            "forward": fs, "plans": [], "baskets": baskets, "written": written, "notes": notes, "params": params,
            "open": bt["open"]}


# ---------------------------------------------------------------- options edge v2: vol, events, engines

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_FAMILIES = ("vrp-selling", "event-vol", "option-buying")


def _submodule(name):
    """vol.py, events.py or engines.py from this folder, loaded by path (this package is itself loaded by
    path, so a relative import would not work), with O pointing back here."""
    import importlib.util
    import sys
    me = sys.modules.get(__name__)
    if me is None or getattr(me, "__dict__", None) is not globals():
        import types
        me = types.SimpleNamespace(**globals())
    key = "%s_%s" % (__name__, name)
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    mod.O = me
    return mod


def vol_api():
    """Realised-vol estimators, the HAR-RV forecast and the IV - RV spread (vol.py)."""
    return _submodule("vol")


def events_api():
    """Implied vs realised moves around results, and the event-day calendar (events.py)."""
    return _submodule("events")


def engines_api():
    """The engines the edge-hunt executor calls by family name (engines.py; its docstring is the contract)."""
    return _submodule("engines")


def engine(family):
    """engines_api().engine(family): fn(hyp, params, window, data, cost_mult=1.0) -> result."""
    return engines_api().engine(family)
