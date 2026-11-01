"""Futures engine: a continuous near-month series, lot sizes by date, a SPAN-style margin estimate, the dated
2026 futures charges, roll costs, overnight or intraday holding and daily mark-to-market.

    series      continuous(rows): one row a session for the contract held that day, with its own open and
                close, its expiry and lot, and on a roll day the price of the next contract at that close.
                Returns are always within one contract, so a roll never shows up as a fake gain or loss.
    simulate    turns intents ([{entry, entry_at, exit, exit_at, side}], session indexes, "open" or
                "close") into fills: whole lots sized on the equity at entry (notional = equity x leverage,
                no leverage by default), slippage on every fill, each order's charges on its own date,
                the position marked to market at every close, rolled at the roll-day close (both orders
                charged: the roll cost), and closed at the window's last close.
    trend       time-series momentum and the 200-day average, decided at each month's last close and filled
                at the next open.
    intraday    the last half-hour after a large first half-hour (minute bars).

Margin: what a broker would block, estimated as the price scan plus exposure margin on the notional
(trader-strategy's SPAN-style figures: index 9.3% + 2%, stock 20% + 3.5%), not the exchange's SPAN file.
"""

import math

from . import core

MARGIN = {"index": {"scan_pct": 9.3, "elm_pct": 2.0}, "stock": {"scan_pct": 20.0, "elm_pct": 3.5}}
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def kind_of(underlying):
    return "index" if str(underlying).upper() in INDEX else "stock"


def margin_pct(underlying):
    """The SPAN-style margin as % of notional, from trader-strategy's SPAN_DEFAULTS when it is installed."""
    k = kind_of(underlying)
    s = core.ENV.get("strategy")
    p = dict(MARGIN[k])
    sd = getattr(s, "SPAN_DEFAULTS", None) if s is not None else None
    if isinstance(sd, dict) and isinstance(sd.get(k), dict):
        p["scan_pct"] = float(sd[k].get("scan_pct", p["scan_pct"]))
        p["elm_pct"] = float(sd[k].get("elm_pct", p["elm_pct"]))
    return p["scan_pct"] + p["elm_pct"]


# ---------------------------------------------------------------- the continuous series

class Continuous(object):
    """Columns of a continuous futures series. ret_cc[k]: close k-1 to close k of the contract held on k
    (from the next contract's price at k-1's close on the day after a roll); ret_co: to the open; ret_oc:
    open to close. roll[k]: at close k the position moves to the next contract, priced next_c[k]."""

    def __init__(self, underlying, rows, source="", lot_fn=None):
        self.underlying = str(underlying).upper()
        self.source = source
        self.lot_fn = lot_fn          # (day, expiry) -> lot, for sessions whose rows carry none
        rows = sorted(rows, key=lambda r: r["date"])
        self.days = [r["date"] for r in rows]
        self.n = len(rows)
        self.o = [float(r.get("o") or r["c"]) for r in rows]
        self.h = [float(r.get("h") or r["c"]) for r in rows]
        self.l = [float(r.get("l") or r["c"]) for r in rows]
        self.c = [float(r["c"]) for r in rows]
        self.expiry = [r.get("expiry") for r in rows]
        self.lot = [int(r["lot"]) if r.get("lot") else None for r in rows]
        self.roll = [bool(r.get("roll")) for r in rows]
        self.brk = [bool(r.get("break")) for r in rows]
        self.next_c = [float(r["next_c"]) if r.get("next_c") else None for r in rows]
        self.ret_cc, self.ret_co, self.ret_oc = [0.0] * self.n, [0.0] * self.n, [0.0] * self.n
        for k in range(self.n):
            self.ret_oc[k] = self.c[k] / self.o[k] - 1 if self.o[k] > 0 else 0.0
            if k == 0:
                continue
            ref = self.next_c[k - 1] if self.roll[k - 1] and self.next_c[k - 1] else self.c[k - 1]
            if rows[k].get("break"):
                ref = self.o[k]
            self.ret_cc[k] = self.c[k] / ref - 1 if ref > 0 else 0.0
            self.ret_co[k] = self.o[k] / ref - 1 if ref > 0 else 0.0
        self.index = {d: k for k, d in enumerate(self.days)}
        # a price index that compounds the within-contract returns: indicators read this, never the raw jumps
        self.adj = []
        x = 100.0
        for k in range(self.n):
            x *= (1 + self.ret_cc[k])
            self.adj.append(x)

    def lot_at(self, k):
        """The lot of the contract held on session k (None when the data does not say)."""
        if self.lot[k] is None and self.lot_fn is not None and self.expiry[k]:
            try:
                self.lot[k] = self.lot_fn(self.days[k], self.expiry[k]) or None
            except Exception:       # a missing file or a missing row: the lot stays unknown
                self.lot[k] = None
            if self.lot[k] is None:
                self.lot_fn_misses = getattr(self, "lot_fn_misses", 0) + 1
        return self.lot[k]

    def next_lot(self, k):
        """The lot of the contract rolled into at close k."""
        if k + 1 < self.n:
            return self.lot_at(k + 1)
        return None

    def ref_close(self, k):
        """The close a position held overnight from k is marked from on k+1."""
        return self.next_c[k] if self.roll[k] and self.next_c[k] else self.c[k]


def _remaining(days, i, expiry):
    """Sessions after days[i] up to and including expiry (weekdays past the end of the data)."""
    import bisect
    if expiry <= days[-1]:
        return bisect.bisect_right(days, expiry) - (i + 1)
    n = len(days) - (i + 1)
    d, end = core.day(days[-1]), core.day(expiry)
    import datetime
    while d < end:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def build_continuous(boards, underlying, roll_sessions=1):
    """Continuous near-month rows from end-of-day F&O boards ([{date, futures: [{expiry, open, high, low,
    close, settle, lot, volume}]}], one a session). The near contract is held until the close
    `roll_sessions` sessions before its expiry day, then the next one (1: roll at the close of the session
    before expiry). A roll needs the next contract's price that day; without it the roll waits a day. A
    session where the held contract is missing restarts on the nearest one, flagged break (no return)."""
    boards = sorted([b for b in boards if b and b.get("futures")], key=lambda b: b["date"])
    days = [b["date"] for b in boards]
    rows = []
    active = None
    for i, b in enumerate(boards):
        futs = {f["expiry"]: f for f in b["futures"] if (f.get("close") or f.get("settle"))}
        exps = sorted(e for e in futs if e >= b["date"])
        if not exps:
            continue
        brk = False
        if active is None or active not in futs:
            brk = active is not None
            active = next((e for e in exps if _remaining(days, i, e) >= roll_sessions), exps[-1])
        f = futs[active]
        px = float(f.get("close") or f.get("settle"))
        row = {"date": b["date"], "o": float(f.get("open") or px), "h": float(f.get("high") or px),
               "l": float(f.get("low") or px), "c": px, "expiry": active, "lot": f.get("lot"),
               "volume": f.get("volume"), "break": brk}
        if _remaining(days, i, active) <= roll_sessions:
            nxt = next((e for e in exps if e > active), None)
            if nxt is not None:
                nf = futs[nxt]
                row.update({"roll": True, "next_c": float(nf.get("close") or nf.get("settle")),
                            "next_expiry": nxt, "next_lot": nf.get("lot")})
                active = nxt
        rows.append(row)
    return rows


# ---------------------------------------------------------------- the simulator

def simulate(s, intents, a, b, capital, cfg=None, cost_mult=1.0, leverage=1.0, bps=None):
    """Fill the intents on series s within sessions a..b (indexes, inclusive) and mark to market daily.
    Returns the engine run dict (core.new_run) with the futures extras: rolls, roll cost, margin."""
    cfg = cfg or {}
    days = s.days[a:b + 1]
    run = core.new_run(days, capital)
    run["bench"] = [s.ret_cc[k] if k > a else 0.0 for k in range(a, b + 1)]
    bps = core.COSTS["index_slippage_bps" if kind_of(s.underlying) == "index" else "slippage_bps"] \
        if bps is None else bps
    mpct = margin_pct(s.underlying)
    pnl = [0.0] * len(days)
    eq = float(capital)
    pos = None
    by_entry = {}
    for it in intents:
        if a <= it["entry"] <= b:
            by_entry.setdefault((it["entry"], it["entry_at"]), []).append(it)
    rolls = {"count": 0, "cost": 0.0}
    margins = []
    skipped = 0
    unknown = 0

    def order(value, buy, on):
        c = core.fut_side_charges(value if buy else 0.0, 0.0 if buy else value, cfg, on) * cost_mult
        run["charges"] += c
        run["traded"] += value
        return c

    def open_pos(it, k, px_mid, when):
        nonlocal skipped, unknown
        lot = s.lot_at(k)
        rolled = when == "close" and s.roll[k] and s.next_c[k]
        if rolled:
            # entering at a roll-day close: straight into the next contract
            px_mid = s.next_c[k]
            lot = s.next_lot(k) or lot
        if not lot:
            lot, unknown = 1, unknown + 1
        fill = core.slip(px_mid, bps, it["side"], cost_mult)
        lots = int(math.floor(eq * leverage / (fill * lot))) if fill > 0 else 0
        if lots <= 0:
            skipped += 1
            return None
        units = lots * lot
        c = order(fill * units, it["side"] > 0, s.days[k])
        slip_cost = abs(fill - px_mid) * units
        return {"side": it["side"], "lots": lots, "lot": lot, "units": units, "ref": fill, "entry": fill,
                "entry_date": s.days[k], "entry_k": k, "entry_at": when, "charges": c, "gross": 0.0, "exit_at": None,
                "intent": it, "rolls": 0, "notional": fill * units, "slip": slip_cost,
                "no_roll": k if rolled else None, "mid": px_mid}

    def close_pos(p, k, px_mid, when, why):
        fill = core.slip(px_mid, bps, -p["side"], cost_mult)
        g = p["side"] * p["units"] * (fill - p["ref"])
        c = order(fill * p["units"], p["side"] < 0, s.days[k])
        p["gross"] += g
        p["charges"] += c
        p["slip"] += abs(fill - px_mid) * p["units"]
        net = p["gross"] - p["charges"]
        run["trades"].append({
            "symbol": s.underlying, "side": "long" if p["side"] > 0 else "short", "entry_date": p["entry_date"],
            "entry_at": p["entry_at"], "exit_date": s.days[k], "exit_at": when, "entry": round(p["entry"], 2),
            "exit": round(fill, 2), "lots": p["lots"], "qty": p["units"], "gross": round(p["gross"], 2),
            "charges": round(p["charges"], 2), "net": round(net, 2),
            "ret": net / p["notional"] if p["notional"] > 0 else 0.0, "sessions": k - p["entry_k"],
            "rolls": p["rolls"], "reason": why, "slippage": round(p["slip"], 2)})
        return g - c

    for k in range(a, b + 1):
        i = k - a
        day_pnl = 0.0
        # the open: exits due at the open, then entries at the open
        if pos is not None and s.brk[k]:
            pos["ref"] = s.o[k]          # the held contract is missing from the data: no gap is booked
        if pos is not None:
            day_pnl += pos["side"] * pos["units"] * (s.o[k] - pos["ref"])
            pos["gross"] += pos["side"] * pos["units"] * (s.o[k] - pos["ref"])
            pos["ref"] = s.o[k]
            it = pos["intent"]
            if it["exit"] == k and it["exit_at"] == "open":
                pos["ref"] = s.o[k]
                day_pnl += close_pos(pos, k, s.o[k], "open", it.get("why", "rule"))
                pos = None
        for it in by_entry.get((k, "open"), []):
            if pos is None and k < b:
                pos = open_pos(it, k, s.o[k], "open")
                if pos is not None:
                    day_pnl -= pos["charges"]
                    day_pnl += pos["side"] * pos["units"] * (s.o[k] - pos["entry"])
                    pos["gross"] += pos["side"] * pos["units"] * (s.o[k] - pos["entry"])
                    pos["ref"] = s.o[k]
        # the close: mark, exits, entries, then the roll
        if pos is not None:
            g = pos["side"] * pos["units"] * (s.c[k] - pos["ref"])
            day_pnl += g
            pos["gross"] += g
            pos["ref"] = s.c[k]
            it = pos["intent"]
            if (it["exit"] == k and it["exit_at"] == "close") or k == b or it["exit"] < k:
                why = "window end" if (k == b and it["exit"] != k) else it.get("why", "rule")
                day_pnl += close_pos(pos, k, s.c[k], "close", why)
                pos = None
        for it in by_entry.get((k, "close"), []):
            if pos is None and k < b:
                pos = open_pos(it, k, s.c[k], "close")
                if pos is not None:
                    day_pnl -= pos["charges"]
                    g = pos["side"] * pos["units"] * (pos["mid"] - pos["entry"])
                    day_pnl += g
                    pos["gross"] += g
                    pos["ref"] = pos["mid"]
        if pos is not None and s.roll[k] and s.next_c[k] and pos["no_roll"] != k:
            # roll: close this contract at its close and open the next at its close, both orders charged
            old_units = pos["units"]
            out_fill = core.slip(s.c[k], bps, -pos["side"], cost_mult)
            g = pos["side"] * old_units * (out_fill - s.c[k])
            c1 = order(out_fill * old_units, pos["side"] < 0, s.days[k])
            new_lot = s.next_lot(k) or pos["lot"]
            units = pos["lots"] * new_lot
            in_fill = core.slip(s.next_c[k], bps, pos["side"], cost_mult)
            c2 = order(in_fill * units, pos["side"] > 0, s.days[k])
            g2 = pos["side"] * units * (s.next_c[k] - in_fill)
            day_pnl += g + g2 - c1 - c2
            pos["gross"] += g + g2
            pos["charges"] += c1 + c2
            cost = c1 + c2 - g - g2
            rolls["count"] += 1
            rolls["cost"] += cost
            pos["rolls"] += 1
            pos["units"], pos["lot"] = units, new_lot
            pos["ref"] = s.next_c[k]
        pnl[i] = day_pnl
        eq += day_pnl
        if pos is not None:
            px = pos["ref"]
            run["exposure"][i] = pos["units"] * px / eq if eq > 0 else 0.0
            margins.append(pos["units"] * px * mpct / 100.0)
    core.finish(run, pnl)
    run["extra"] = {"rolls": rolls["count"], "roll_cost": round(rolls["cost"], 2), "margin_pct": mpct,
                    "max_margin": round(max(margins), 2) if margins else 0.0,
                    "avg_margin": round(core.mean(margins), 2) if margins else 0.0,
                    "skipped_below_one_lot": skipped, "slippage_bps": bps, "leverage": leverage}
    net = sum(t["net"] for t in run["trades"])
    if margins:
        run["extra"]["return_on_avg_margin_pct"] = round(net / core.mean(margins) * 100, 3)
    if skipped:
        run["notes"].append("%d entries skipped: the equity was below one lot's notional" % skipped)
    if unknown:
        run["notes"].append("%d entries on days with no lot size in the data: sized in single units" % unknown)
    run["extra"]["unknown_lot_entries"] = unknown
    return run


# ---------------------------------------------------------------- strategies on the continuous series

def trend_signal(s, k, signal):
    """+1, -1 or 0 from the adjusted closes up to and including session k (no later bar is read)."""
    x = s.adj
    if signal == "tsmom_12m":
        if k < 252:
            return 0
        r = x[k] / x[k - 252] - 1
        return 1 if r > 0 else -1
    if signal == "close_above_200d_average":
        if k < 199:
            return 0
        m = sum(x[k - 199:k + 1]) / 200.0
        return 1 if x[k] > m else -1
    if signal == "tsmom_blend_3_6_12m":
        if k < 252:
            return 0
        v = sum((1 if x[k] / x[k - n] - 1 > 0 else -1) for n in (63, 126, 252)) / 3.0
        return 1 if v > 0 else -1
    raise core.EngineError("signal is tsmom_12m, close_above_200d_average or tsmom_blend_3_6_12m")


def trend_intents(s, a, b, signal, mode):
    """Positions decided at each month's last close (and the session before the window, for the start)
    and filled at the next session's open; long_flat goes flat instead of short."""
    decide = set(core.last_of_period(s.days, "monthly"))
    targets = []
    for k in range(max(0, a - 1), b):
        if s.days[k] in decide or k == a - 1 or (k == a and a == 0):
            sig = trend_signal(s, k, signal)
            if mode == "long_flat" and sig < 0:
                sig = 0
            targets.append((k + 1, sig))
    intents, cur, start = [], 0, None
    for k, sig in targets:
        if sig == cur:
            continue
        if cur != 0:
            intents[-1]["exit"], intents[-1]["exit_at"] = k, "open"
        if sig != 0:
            intents.append({"entry": k, "entry_at": "open", "exit": b, "exit_at": "close", "side": sig,
                            "why": "signal"})
        cur = sig
    return intents


def intraday_momentum(days_bars, prev_close, lot_of, underlying, params, capital, cfg=None, cost_mult=1.0,
                      bps=None):
    """The last half-hour after a large first half-hour, on 1-minute bars {date: [bars]} (time HH:MM, o, c).
    first_window 0915_0945_with_gap: the return from the previous close to the 09:44 bar's close;
    _without_gap: from the 09:15 open. Trade its sign from the 15:00 open to the 15:29 close when its size
    is at least min_abs_first_return_pct. Whole lots on the equity, futures charges, slippage both sides."""
    cfg = cfg or {}
    days = sorted(days_bars)
    run = core.new_run(days, capital)
    bps = core.COSTS["index_slippage_bps"] if bps is None else bps
    thr = float(params.get("min_abs_first_return_pct", 0)) / 100.0
    gap = str(params.get("first_window", "")).endswith("_with_gap")
    pnl = [0.0] * len(days)
    eq = float(capital)
    xs, ys = [], []
    for i, d in enumerate(days):
        bars = {b["time"][:5]: b for b in days_bars[d]}
        o915, c944 = bars.get("09:15"), bars.get("09:44")
        e = bars.get("15:00")
        x = bars.get("15:29")
        if not (o915 and c944 and e and x):
            continue
        base = prev_close.get(d) if gap else float(o915["o"])
        if not base:
            continue
        r1 = float(c944["c"]) / float(base) - 1
        r2 = float(x["c"]) / float(e["o"]) - 1
        xs.append(r1)
        ys.append(r2)
        if abs(r1) < thr or r1 == 0:
            continue
        side = 1 if r1 > 0 else -1
        lot = lot_of(d) or 1
        fin = core.slip(float(e["o"]), bps, side, cost_mult)
        fout = core.slip(float(x["c"]), bps, -side, cost_mult)
        lots = int(math.floor(eq / (fin * lot)))
        if lots <= 0:
            continue
        units = lots * lot
        c = (core.fut_side_charges(fin * units if side > 0 else 0, 0 if side > 0 else fin * units, cfg, d) +
             core.fut_side_charges(fout * units if side < 0 else 0, 0 if side < 0 else fout * units, cfg, d)) * cost_mult
        g = side * units * (fout - fin)
        run["charges"] += c
        run["traded"] += (fin + fout) * units
        pnl[i] = g - c
        eq += g - c
        run["trades"].append({"symbol": underlying, "side": "long" if side > 0 else "short", "entry_date": d,
                              "entry_at": "15:00", "exit_date": d, "exit_at": "15:29", "entry": round(fin, 2),
                              "exit": round(fout, 2), "lots": lots, "qty": units, "gross": round(g, 2),
                              "charges": round(c, 2), "net": round(g - c, 2), "ret": (g - c) / (fin * units),
                              "sessions": 0, "first_return": r1})
        run["exposure"][i] = 1.0 / 12.0     # held 30 of the 375 minutes; shown as a share of the session
    core.finish(run, pnl)
    a, slope, t, _ = core.ols(xs, ys)
    run["extra"] = {"predictive_slope": round(slope, 5), "slope_t": round(t, 3), "slope_days": len(xs)}
    run["notes"].append("priced on the minute bars given (an index used for a future is a proxy: no basis)")
    return run
