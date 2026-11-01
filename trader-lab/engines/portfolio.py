"""Cross-sectional portfolio engine: a point-in-time universe, ranking signals, rebalance schedules, top-N or
quantile portfolios, equal or volatility-scaled weights, delivery (CNC) or stock-futures charges, turnover,
the benchmarks and a capacity note.

    signals     momentum (the return over `lookback` sessions, skipping the latest `skip`; 12-1 is
                252 and 21), low volatility (the lowest standard deviation of daily returns over
                `vol_window`), short-term reversal (the biggest losers over `lookback`). volatility_adjusted
                divides a momentum score by its volatility (like NSE's momentum indices). Quality needs dated
                fundamentals and is not here.
    universe    members(day): the index's members on that day (point in time, so a stock that later left
                the index or was delisted is still there before it left). A stock also needs enough real
                closes for its signal, and a close in the last 5 sessions.
    rebalance   decided at a period's last close (weekly, monthly, quarterly, semiannual, or every N
                sessions) from closes up to that close, filled at the next session's open with slippage.
                A holding that stays in the portfolio is resized only when it is more than `band` (25%) off
                its target weight; names that leave are sold, names that enter are bought.
    charges     CNC: the lab's delivery charges (STT 0.1% on both sides, stamp 0.015% on the buy, exchange,
                SEBI, GST) on every order, plus a DP charge per scrip per sell day. FUT: the dated futures
                charges per order; the short leg of a long-short book is a stock future (priced at the
                stock's close when no futures series is given: no basis, lot rounding ignored).
    benchmark   universe_ew: the equal-weighted point-in-time universe, rebalanced monthly, no charges;
                nifty_tr: NIFTY 50 plus its dividend yield accrued daily (a total-return proxy).

Every position is closed at the window's last close (purged), with charges.
"""

import math

from . import core

DEFAULTS = {"signal": "momentum", "lookback": 252, "skip": 21, "vol_window": 252, "top_n": 20, "top_fraction": None,
            "side": "long", "rebalance": "monthly", "weights": "equal", "volatility_adjusted": False,
            "product": "CNC", "band": 0.25, "benchmark": "universe_ew", "stale_sessions": 5}


class Panel(object):
    """Daily bars of many stocks on one session calendar: o, c and traded value per symbol (None where the
    stock did not trade); cf is the close carried forward."""

    def __init__(self, days, bars_by_symbol):
        self.days = list(days)
        self.n = len(self.days)
        self.index = {d: k for k, d in enumerate(self.days)}
        self.o, self.c, self.cf, self.tv, self.first = {}, {}, {}, {}, {}
        for sym, bars in bars_by_symbol.items():
            o, c, tv = [None] * self.n, [None] * self.n, [None] * self.n
            for b in bars:
                k = self.index.get(b["date"])
                if k is None:
                    continue
                cl = float(b["c"])
                o[k] = float(b.get("o") or cl)
                c[k] = cl
                t = b.get("turnover")
                tv[k] = float(t) if t not in (None, "") else cl * float(b.get("v") or 0)
            cf, last = [None] * self.n, None
            for k in range(self.n):
                if c[k] is not None:
                    last = c[k]
                cf[k] = last
            self.o[sym], self.c[sym], self.cf[sym], self.tv[sym] = o, c, cf, tv
            self.first[sym] = next((k for k in range(self.n) if c[k] is not None), None)

    def fresh(self, sym, k, stale):
        c = self.c.get(sym)
        return c is not None and any(c[j] is not None for j in range(max(0, k - stale + 1), k + 1))

    def rets(self, sym, k, n):
        """The n daily returns up to and including session k (closes carried forward), or None."""
        cf = self.cf[sym]
        f = self.first.get(sym)
        if f is None or k - n < f:
            return None
        return [cf[j] / cf[j - 1] - 1 for j in range(k - n + 1, k + 1)]

    def median_value(self, sym, k, n=20):
        v = [x for x in self.tv[sym][max(0, k - n + 1):k + 1] if x]
        if not v:
            return None
        v.sort()
        return v[len(v) // 2]


def score(panel, sym, k, p):
    """The signal of one stock at session k's close, from closes up to k only; None when it has too little
    history. Higher is better for every signal (the long side buys the highest)."""
    cf = panel.cf[sym]
    f = panel.first.get(sym)
    if f is None or not panel.fresh(sym, k, p["stale_sessions"]):
        return None
    sig = p["signal"]
    if sig == "momentum":
        L, S = int(p["lookback"]), int(p["skip"])
        if k - L - S < f:
            return None
        r = cf[k - S] / cf[k - S - L] - 1
        if p.get("volatility_adjusted"):
            rs = panel.rets(sym, k, int(p["vol_window"]))
            v = core.sd(rs) * math.sqrt(core.YEAR) if rs else 0.0
            if v <= 0:
                return None
            r = r / v
        return r
    if sig == "low_volatility":
        rs = panel.rets(sym, k, int(p["vol_window"]))
        return -core.sd(rs) if rs else None
    if sig == "reversal":
        L = int(p["lookback"])
        if k - L < f:
            return None
        return -(cf[k] / cf[k - L] - 1)
    raise core.EngineError("signal is momentum, low_volatility or reversal")


def select(panel, members, k, p):
    """{symbol: weight} decided at session k's close (weights sum to 1 long, or +0.5 / -0.5 long-short)."""
    scored = []
    for sym in sorted(members):
        if sym not in panel.c:
            continue
        s = score(panel, sym, k, p)
        if s is not None:
            scored.append((s, sym))
    if not scored:
        return {}
    scored.sort(key=lambda x: (-x[0], x[1]))
    n = int(p["top_n"]) if p.get("top_n") else max(1, int(round(len(scored) * float(p["top_fraction"]))))
    n = min(n, len(scored) if p["side"] == "long" else len(scored) // 2)
    if n <= 0:
        return {}
    longs = [sym for _, sym in scored[:n]]
    shorts = [sym for _, sym in scored[-n:]] if p["side"] == "long_short" else []

    def weights(names):
        if p["weights"] == "inverse_vol":
            iv = {}
            for sym in names:
                rs = panel.rets(sym, k, int(p["vol_window"])) or panel.rets(sym, k, 20)
                v = core.sd(rs) if rs else 0.0
                iv[sym] = 1.0 / v if v > 0 else 0.0
            tot = sum(iv.values())
            return {s_: (iv[s_] / tot if tot > 0 else 1.0 / len(names)) for s_ in names}
        return {s_: 1.0 / len(names) for s_ in names}
    if p["side"] == "long":
        return weights(longs)
    w = {s_: 0.5 * x for s_, x in weights(longs).items()}
    for s_, x in weights(shorts).items():
        w[s_] = w.get(s_, 0.0) - 0.5 * x
    return w


def _decisions(days, a, b, rebalance):
    if isinstance(rebalance, int) and not isinstance(rebalance, bool):
        ks = list(range(max(0, a - 1), b, rebalance))
        return set(ks)
    ends = set(core.last_of_period(days, rebalance))
    ks = set(k for k in range(a, b) if days[k] in ends)
    if a == 0:
        ks.add(0)
    return ks


def run(panel, members_fn, params, a, b, capital, cfg=None, cost_mult=1.0, index_bars=None, bps=None):
    """The portfolio over sessions a..b of the panel. members_fn(day) -> the universe on that day."""
    p = dict(DEFAULTS)
    p.update(params or {})
    cfg = cfg or {}
    bps = core.COSTS["slippage_bps"] if bps is None else bps
    dp = float(cfg.get("dp_charge", core.COSTS["dp_charge"]))
    fut = str(p["product"]).upper() == "FUT"
    days = panel.days[a:b + 1]
    r = core.new_run(days, capital)
    decide = _decisions(panel.days, a, b, p["rebalance"])
    qty = {}
    ep = {}            # open episodes per symbol
    eq = float(capital)
    pnl = [0.0] * len(days)
    pending = None
    parts = []
    bench_members, bench = None, []
    month_ends = set(core.last_of_period(panel.days, "monthly"))
    ew_members = members_fn(panel.days[max(0, a - 1)]) or []
    tr = []
    idx = {}
    if index_bars:
        for x in index_bars:
            idx[x["date"]] = x

    def charge(sym, value, buy, on):
        if fut:
            c = core.fut_side_charges(value if buy else 0.0, 0.0 if buy else value, cfg, on)
        else:
            c = core.cnc_side_charges(value if buy else 0.0, 0.0 if buy else value, cfg, on)
        r["charges"] += c * cost_mult
        r["traded"] += value
        return c * cost_mult

    def episode_close(sym, k, px):
        e = ep.pop(sym)
        net = e["gross"] - e["charges"]
        r["trades"].append({"symbol": sym, "side": "long" if e["side"] > 0 else "short", "entry_date": e["entry_date"],
                            "exit_date": panel.days[k], "entry": round(e["entry"], 2), "exit": round(px, 2),
                            "qty": e["qty"], "gross": round(e["gross"], 2), "charges": round(e["charges"], 2),
                            "net": round(net, 2), "ret": net / e["value"] if e["value"] > 0 else 0.0,
                            "sessions": k - e["entry_k"]})

    def trade(sym, k, new_q, px_mid, sold_today):
        old = qty.get(sym, 0.0)
        dq = new_q - old
        if abs(dq) < 1e-12:
            return 0.0
        side = 1 if dq > 0 else -1
        fill = core.slip(px_mid, bps, side, cost_mult)
        value = abs(dq) * fill
        c = charge(sym, value, side > 0, panel.days[k])
        if not fut and side < 0 and sym not in sold_today:
            c += dp * cost_mult
            r["charges"] += dp * cost_mult
            sold_today.add(sym)
        slip_loss = abs(dq) * abs(fill - px_mid)
        mv = panel.median_value(sym, k - 1) if k > 0 else None
        if mv:
            parts.append(value / mv)
        if old == 0:
            ep[sym] = {"side": side, "entry": fill, "entry_date": panel.days[k], "entry_k": k, "gross": 0.0,
                       "charges": 0.0, "qty": abs(new_q), "value": abs(new_q) * fill}
        e = ep[sym]
        e["charges"] += c
        e["gross"] -= slip_loss
        e["value"] = max(e["value"], abs(new_q) * fill)
        e["qty"] = max(e["qty"], abs(new_q))
        qty[sym] = new_q
        if abs(new_q) < 1e-12:
            qty.pop(sym, None)
            episode_close(sym, k, fill)
        return -(c + slip_loss)

    if a >= 1:
        pending = select(panel, members_fn(panel.days[a - 1]) or [], a - 1, p)
    for k in range(a, b + 1):
        i = k - a
        day_pnl = 0.0
        sold = set()
        # the benchmark: yesterday's members, equal weight, close to close
        rs = []
        for sym in ew_members:
            c = panel.c.get(sym)
            if c and k > 0 and c[k] is not None and panel.cf[sym][k - 1]:
                rs.append(c[k] / panel.cf[sym][k - 1] - 1)
        bench.append(core.mean(rs) if (rs and k > 0) else 0.0)
        if index_bars:
            prev = panel.days[k - 1] if k > 0 else None
            x, y = idx.get(panel.days[k]), idx.get(prev)
            dy = (x or {}).get("div_yield")
            dy = float(dy) if dy not in (None, "") else float(cfg.get("nifty_div_yield_pct", 1.3))
            tr.append((x["c"] / y["c"] - 1 + dy / 100.0 / core.YEAR) if (x and y and k > 0) else 0.0)
        if pending is not None:
            # overnight move of the old book to the open, then trade at the open
            for sym, q in list(qty.items()):
                o = panel.o[sym][k]
                ref = panel.cf[sym][k - 1]
                if o is None or ref is None:
                    continue
                g = q * (o - ref)
                day_pnl += g
                ep[sym]["gross"] += g
            targets = pending
            pending = None
            for sym in sorted(set(qty) | set(targets)):
                o = panel.o[sym][k]
                if o is None:
                    continue            # no trade today in this stock: it waits for the next rebalance
                w = targets.get(sym, 0.0)
                want_value = w * eq
                if fut:
                    new_q = want_value / o
                else:
                    new_q = float(int(math.floor(abs(want_value) / core.slip(o, bps, 1, cost_mult)))) * (1 if w >= 0 else -1)
                old = qty.get(sym, 0.0)
                if old != 0 and new_q != 0 and (old > 0) == (new_q > 0):
                    if abs(new_q - old) <= p["band"] * abs(new_q):
                        continue
                if old != 0 and new_q != 0 and (old > 0) != (new_q > 0):
                    day_pnl += trade(sym, k, 0.0, o, sold)      # a flip is two orders: out, then in
                day_pnl += trade(sym, k, new_q, o, sold)
            for sym, q in qty.items():
                c = panel.c[sym][k]
                o = panel.o[sym][k]
                ref = o if o is not None else panel.cf[sym][k - 1]
                if c is None or ref is None:
                    continue
                g = q * (c - ref)
                day_pnl += g
                ep[sym]["gross"] += g
        else:
            for sym, q in qty.items():
                c = panel.c[sym][k]
                ref = panel.cf[sym][k - 1] if k > 0 else None
                if c is None or ref is None:
                    continue
                g = q * (c - ref)
                day_pnl += g
                ep[sym]["gross"] += g
        if k == b:
            for sym in sorted(qty):
                px = panel.cf[sym][k]
                day_pnl += trade(sym, k, 0.0, px, sold)
        eq += day_pnl
        pnl[i] = day_pnl
        gross = sum(abs(q) * (panel.cf[s_][k] or 0) for s_, q in qty.items())
        r["exposure"][i] = gross / eq if eq > 0 else 0.0
        if k in decide and k < b:
            mem = members_fn(panel.days[k]) or []
            pending = select(panel, mem, k, p)
        if panel.days[k] in month_ends:
            ew_members = members_fn(panel.days[k]) or ew_members
    core.finish(r, pnl)
    r["bench"] = tr if (p["benchmark"] == "nifty_tr" and index_bars) else bench
    r["extra"] = {"benchmark": p["benchmark"] if (p["benchmark"] != "nifty_tr" or index_bars) else "universe_ew",
                  "universe_ew_cagr_pct": round(core._ann(bench)["cagr"] * 100, 3)}
    if tr:
        r["extra"]["nifty_tr_cagr_pct"] = round(core._ann(tr)["cagr"] * 100, 3)
        if not any(idx[d].get("div_yield") not in (None, "") for d in idx):
            r["notes"].append("NIFTY total-return proxy: the index plus %.1f%% a year of dividends (no yield data)"
                              % float(cfg.get("nifty_div_yield_pct", 1.3)))
    r["extra"]["capacity"] = capacity(parts, r)
    if fut and p["side"] == "long_short":
        r["notes"].append("the short leg is a stock future priced at the stock's own open and close (no basis, "
                          "no lot rounding, margin not modelled)")
    return r


def capacity(parts, r):
    """How large the account could be before its orders matter: the median and 95th percentile order as a
    share of the stock's median daily traded value (20 sessions before), and the capital at which the
    95th-percentile order would be 1% of a day's value."""
    if not parts:
        return {"orders": 0, "note": "no orders with traded-value data"}
    xs = sorted(parts)
    med = xs[len(xs) // 2]
    p95 = xs[min(len(xs) - 1, int(0.95 * (len(xs) - 1)))]
    avg = core.mean(r["equity"]) or r["capital"]
    at = avg * 0.01 / p95 if p95 > 0 else None
    return {"orders": len(xs), "median_share_of_day_pct": round(med * 100, 4), "p95_share_of_day_pct": round(p95 * 100, 4),
            "capital_at_1pct_of_day": round(at, 0) if at else None,
            "note": ("the median order was %.3f%% of its stock's median daily traded value (95th percentile %.3f%%); "
                     "at about %s of capital the 95th-percentile order would be 1%% of a day's value, where "
                     "slippage starts to grow" % (med * 100, p95 * 100, core.L().inr(at) if at else "n/a"))}
