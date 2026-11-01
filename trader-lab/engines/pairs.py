"""Pairs engine: pair selection on the train window only, then z-score bands traded with two futures legs.

    select      every same-sector pair with closes on at least 90% of the last `formation` train sessions:
                the correlation of daily log returns (at least min_corr, 0.6) and the Engle-Granger test:
                OLS of log y on log x, then an augmented Dickey-Fuller regression on the residuals
                (one lag, no constant); the pair is cointegrated when the t-statistic is below the
                two-variable critical value (MacKinnon 2010: -3.90 at 1%, -3.34 at 5%, -3.04 at 10%;
                5% by default). The `max_pairs` (5) most negative t-statistics are kept. Train data only.
    trade       each session the hedge ratio, mean and standard deviation of the spread (log y - b log x)
                come from the previous `formation` sessions (none of today's); z = today's spread against them.
                |z| >= entry_z opens (short the rich leg, long the cheap one) at the next open; |z| <= exit_z
                (or a sign flip) exits, |z| >= stop_z stops, max_hold sessions times out; all exits at the next
                open. Each leg is half the pair's slot of the equity (dollar neutral), futures charges per
                order, slippage each fill, closed at the window's last close.
"""

import math

from . import core

EG_CRITICAL = {0.01: -3.90, 0.05: -3.34, 0.10: -3.04}


def adf_t(e, lags=1):
    """t-statistic of g in  de_t = g e_{t-1} + sum_i c_i de_{t-i} (no constant), by least squares."""
    de = [e[i] - e[i - 1] for i in range(1, len(e))]
    rows, ys = [], []
    for t in range(lags, len(de)):
        rows.append([e[t]] + [de[t - i] for i in range(1, lags + 1)])
        ys.append(de[t])
    n, m = len(rows), lags + 1
    if n <= m + 2:
        return 0.0
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(m)] for i in range(m)]
    xty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(m)]
    inv = _inverse(xtx)
    if inv is None:
        return 0.0
    beta = [sum(inv[i][j] * xty[j] for j in range(m)) for i in range(m)]
    res = [y - sum(b * x for b, x in zip(beta, r)) for r, y in zip(rows, ys)]
    s2 = sum(x * x for x in res) / float(n - m)
    se = math.sqrt(s2 * inv[0][0]) if s2 > 0 and inv[0][0] > 0 else 0.0
    return beta[0] / se if se > 0 else 0.0


def _inverse(a):
    n = len(a)
    m = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-15:
            return None
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [x / piv for x in m[c]]
        for r in range(n):
            if r != c:
                f = m[r][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [row[n:] for row in m]


def engle_granger(ly, lx):
    """(hedge ratio b, intercept a, ADF t of the residuals) for log prices ly, lx."""
    a, b, _, res = core.ols(lx, ly)
    return b, a, adf_t(res)


def _logs(panel, sym, k0, k1):
    return [math.log(panel.cf[sym][k]) if panel.cf[sym][k] else None for k in range(k0, k1 + 1)]


def select(panel, sectors, train_a, train_b, formation, max_pairs=5, min_corr=0.6, level=0.05, members=None):
    """The pairs to trade, chosen on sessions train_a..train_b only: [{y, x, sector, corr, adf_t, b}]."""
    k0 = max(train_a, train_b - formation + 1)
    crit = EG_CRITICAL[level]
    by = {}
    for sym, sec in sorted((sectors or {}).items()):
        if sym in panel.c and (members is None or sym in members):
            by.setdefault(sec, []).append(sym)
    out = []
    for sec, names in sorted(by.items()):
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                y, x = names[i], names[j]
                cy = [panel.c[y][k] for k in range(k0, train_b + 1)]
                cx = [panel.c[x][k] for k in range(k0, train_b + 1)]
                have = sum(1 for u, v in zip(cy, cx) if u is not None and v is not None)
                if have < 0.9 * (train_b - k0 + 1) or have < 30:
                    continue
                ly, lx = _logs(panel, y, k0, train_b), _logs(panel, x, k0, train_b)
                if None in ly or None in lx:
                    continue
                ry = [ly[t] - ly[t - 1] for t in range(1, len(ly))]
                rx = [lx[t] - lx[t - 1] for t in range(1, len(lx))]
                corr = core.correlation(ry, rx)
                if corr < min_corr:
                    continue
                b, a, t = engle_granger(ly, lx)
                if t < crit and b > 0:
                    out.append({"y": y, "x": x, "sector": sec, "corr": round(corr, 4), "adf_t": round(t, 3),
                                "b": round(b, 4), "sessions": len(ly)})
    out.sort(key=lambda r: (r["adf_t"], r["y"], r["x"]))
    return out[:max_pairs]


def zscores(panel, y, x, formation, a, b):
    """{k: (z, b)} for sessions a..b: the hedge ratio and spread moments from the `formation` sessions
    before k, today's spread against them."""
    out = {}
    for k in range(a, b + 1):
        k0 = k - formation
        if k0 < 0:
            continue
        ly, lx = _logs(panel, y, k0, k - 1), _logs(panel, x, k0, k - 1)
        if None in ly or None in lx or not panel.cf[y][k] or not panel.cf[x][k]:
            continue
        aa, bb, _, res = core.ols(lx, ly)
        m, s = core.mean(res), core.sd(res)
        if s <= 0:
            continue
        sp = math.log(panel.cf[y][k]) - aa - bb * math.log(panel.cf[x][k])
        out[k] = ((sp - m) / s, bb)
    return out


def run(panel, pairs, params, a, b, capital, cfg=None, cost_mult=1.0, bps=None):
    """Trade the chosen pairs over sessions a..b."""
    cfg = cfg or {}
    bps = core.COSTS["slippage_bps"] if bps is None else bps
    form = int(params["formation_sessions"])
    ez, xz, sz = float(params["entry_z"]), float(params["exit_z"]), float(params["stop_z"])
    mh = int(params["max_hold_sessions"])
    days = panel.days
    r = core.new_run(days[a:b + 1], capital)
    pnl = [0.0] * (b - a + 1)
    eq = float(capital)
    slots = max(1, len(pairs))
    zs = [zscores(panel, p["y"], p["x"], form, max(0, a - 1), b) for p in pairs]
    pos = [None] * len(pairs)
    pending = [None] * len(pairs)
    stops = 0

    def order(value, buy, on):
        c = core.fut_side_charges(value if buy else 0.0, 0.0 if buy else value, cfg, on) * cost_mult
        r["charges"] += c
        r["traded"] += value
        return c

    def close(j, k, at, why):
        nonlocal stops
        p = pos[j]
        tot = 0.0
        for leg in p["legs"]:
            px = panel.o[leg["sym"]][k] if at == "open" else panel.cf[leg["sym"]][k]
            px = px if px is not None else panel.cf[leg["sym"]][k]
            g0 = leg["q"] * (px - leg["ref"])
            fill = core.slip(px, bps, -1 if leg["q"] > 0 else 1, cost_mult)
            g = leg["q"] * (fill - leg["ref"])
            c = order(abs(leg["q"]) * fill, leg["q"] < 0, days[k])
            p["gross"] += g
            p["charges"] += c
            tot += g - c
            leg["exit"] = fill
            _ = g0
        net = p["gross"] - p["charges"]
        if why == "stop":
            stops += 1
        r["trades"].append({"symbol": "%s/%s" % (pairs[j]["y"], pairs[j]["x"]), "side": p["side"],
                            "entry_date": days[p["entry"]], "exit_date": days[k], "entry": round(p["z"], 3),
                            "exit": None, "qty": [leg["q"] for leg in p["legs"]], "gross": round(p["gross"], 2),
                            "charges": round(p["charges"], 2), "net": round(net, 2),
                            "ret": net / p["value"] if p["value"] > 0 else 0.0, "sessions": k - p["entry"],
                            "reason": why})
        pos[j] = None
        return tot

    for k in range(a, b + 1):
        i = k - a
        dp = 0.0
        # the open: pending exits and entries decided at yesterday's close
        for j in range(len(pairs)):
            act = pending[j]
            pending[j] = None
            if pos[j] is not None:
                for leg in pos[j]["legs"]:
                    o = panel.o[leg["sym"]][k]
                    if o is not None:
                        g = leg["q"] * (o - leg["ref"])
                        dp += g
                        pos[j]["gross"] += g
                        leg["ref"] = o
            if act and act[0] == "exit" and pos[j] is not None:
                dp += close(j, k, "open", act[1])
            elif act and act[0] == "enter" and pos[j] is None and k < b:
                side, z = act[1], act[2]
                y, x = pairs[j]["y"], pairs[j]["x"]
                oy, ox = panel.o[y][k], panel.o[x][k]
                if oy is None or ox is None:
                    continue
                leg_value = eq / slots / 2.0
                # side +1: long the spread (long y, short x); -1: short the spread
                legs, cost = [], 0.0
                for sym, sgn, px in ((y, side, oy), (x, -side, ox)):
                    fill = core.slip(px, bps, sgn, cost_mult)
                    q = sgn * leg_value / fill
                    c = order(abs(q) * fill, sgn > 0, days[k])
                    cost += c + abs(q) * abs(fill - px)
                    legs.append({"sym": sym, "q": q, "ref": px})
                pos[j] = {"legs": legs, "entry": k, "side": "long spread" if side > 0 else "short spread",
                          "gross": -sum(abs(l_["q"]) * abs(core.slip(l_["ref"], bps, 1, cost_mult) - l_["ref"])
                                        for l_ in legs), "charges": 0.0, "value": 2 * leg_value, "z": z, "dir": side}
                pos[j]["charges"] = cost + pos[j]["gross"]
                dp -= cost
        # the close: mark, then decide for tomorrow's open
        for j in range(len(pairs)):
            if pos[j] is not None:
                for leg in pos[j]["legs"]:
                    c = panel.c[leg["sym"]][k]
                    if c is not None:
                        g = leg["q"] * (c - leg["ref"])
                        dp += g
                        pos[j]["gross"] += g
                        leg["ref"] = c
                if k == b:
                    dp += close(j, k, "close", "window end")
                    continue
            zk = zs[j].get(k)
            if zk is None or k == b:
                continue
            z = zk[0]
            if pos[j] is None:
                if abs(z) >= ez and abs(z) < sz:
                    pending[j] = ("enter", -1 if z > 0 else 1, z)
            else:
                d = pos[j]["dir"]
                if abs(z) >= sz:
                    pending[j] = ("exit", "stop")
                elif (d < 0 and z <= xz) or (d > 0 and z >= -xz):
                    pending[j] = ("exit", "mean")
                elif k - pos[j]["entry"] + 1 >= mh:
                    pending[j] = ("exit", "time")
        eq += dp
        pnl[i] = dp
        r["exposure"][i] = sum(abs(l_["q"]) * (panel.cf[l_["sym"]][k] or 0) for p in pos if p for l_ in p["legs"]) / eq \
            if eq > 0 else 0.0
    core.finish(r, pnl)
    n = len(r["trades"])
    r["extra"] = {"pairs": pairs, "stop_share": round(stops / float(n), 4) if n else 0.0, "stops": stops}
    r["notes"].append("legs priced on the series given (a stock's own closes stand in for its future when no "
                      "futures series is given: no basis, no lot rounding, margin not modelled)")
    return r
