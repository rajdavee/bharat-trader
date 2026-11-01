"""Shared pieces of the lab's v2 engines: the bound modules, costs, the session calendar, portfolio metrics
and the statistics the executor needs beyond the lab's own (bootstrap p-values, Benjamini-Hochberg).

Every engine returns the same shape, a "run" dict over one window of sessions:

    days       the window's sessions, oldest first
    ret        the strategy's daily return after charges and slippage (P&L over the equity at the
               previous close)
    bench      the benchmark's daily return on the same days (no charges), or None
    equity     the equity at each close, in rupees
    exposure   gross exposure over equity at each close (0 when flat)
    trades     [{symbol, side, entry_date, exit_date, entry, exit, qty, gross, charges, net, ret, sessions, ...}]
    traded     the rupee value bought plus sold (for turnover)
    charges    the charges paid (rupees), slippage excluded (it is in the fills)
    notes      plain-words caveats of this run
    extra      engine-specific numbers (margin, roll costs, stop share, capacity...)

Education and tooling, not financial advice. Nothing here places an order.
"""

import bisect
import datetime
import math
import random
import statistics

ENV = {"lab": None, "archive": None, "regime": None, "strategy": None}
YEAR = 252


class EngineError(ValueError):
    pass


def L():
    lab = ENV["lab"]
    if lab is None:
        raise EngineError("the engines are not bound to the lab (lab.py loads them with engines_api())")
    return lab


def day(text):
    return datetime.date(*[int(x) for x in str(text)[:10].split("-")])


def iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def mean(x):
    return sum(x) / float(len(x)) if x else 0.0


def sd(x):
    return statistics.stdev(x) if len(x) > 1 else 0.0


def r4(x, n=4):
    return None if x is None else round(float(x), n)


# ---------------------------------------------------------------- costs

COSTS = {
    "slippage_bps": 3.0,          # each side, on stocks, stock futures and their legs (the lab's default)
    "index_slippage_bps": 1.0,    # each side, on index futures
    "dp_charge": 15.93,           # rupees per scrip per sell day of delivery holdings (a typical discount broker's
                                  # depository charge with GST; set yours in the config's "dp_charge")
}


def slip(price, bps, side, mult=1.0):
    """A fill bps basis points against you: side +1 buys higher, -1 sells lower."""
    return float(price) * (1.0 + side * float(bps) * mult / 10000.0)


def cnc_side_charges(value_buy, value_sell, cfg=None, on=None):
    """Delivery (CNC) charges of one buy order and/or one sell order with the lab's round_trip_charges (STT
    0.1% on both sides, stamp on the buy, exchange, SEBI, GST), in rupees. A value of 0 leaves that side out."""
    c = dict(cfg or {})
    c["product"] = "CNC"
    return L().round_trip_charges(float(value_buy), float(value_sell), 1, c, "EQ", on)["total"]


def fut_side_charges(value_buy, value_sell, cfg=None, on=None):
    """Futures charges with the lab's dated table (STT on the sell side: 0.02% to 31 Mar 2026, 0.05% from 1
    Apr 2026), one buy and/or one sell order, in rupees."""
    return L().fno_round_trip_charges(float(value_buy), float(value_sell), 1, cfg, "FUT", on)["total"]


# ---------------------------------------------------------------- the session calendar

def month_key(d):
    return str(d)[:7]


def last_of_period(days, period):
    """The sessions that close a period: "weekly" (the last session of each ISO week), "monthly",
    "quarterly" (Mar, Jun, Sep, Dec), "semiannual" (Jun, Dec), or an int N (every N sessions from the first)."""
    out = []
    if isinstance(period, int) and not isinstance(period, bool):
        return [days[i] for i in range(period - 1, len(days), period)]
    for i, d in enumerate(days):
        nxt = days[i + 1] if i + 1 < len(days) else None
        if period == "weekly":
            end = nxt is None or day(nxt).isocalendar()[:2] != day(d).isocalendar()[:2]
        else:
            end = nxt is None or month_key(nxt) != month_key(d)
            m = int(str(d)[5:7])
            if period == "quarterly":
                end = end and m in (3, 6, 9, 12)
            elif period == "semiannual":
                end = end and m in (6, 12)
            elif period != "monthly":
                raise EngineError("rebalance is weekly, monthly, quarterly, semiannual or a number of sessions")
        if end:
            out.append(d)
    return out


def holidays(days):
    """Weekdays between the first and last session that were not sessions (NSE publishes its holiday
    list before the year starts, so knowing them in advance is no look-ahead)."""
    have = set(days)
    out = []
    if not days:
        return out
    d, end = day(days[0]), day(days[-1])
    while d <= end:
        if d.weekday() < 5 and iso(d) not in have:
            out.append(iso(d))
        d += datetime.timedelta(days=1)
    return out


def window(days, start, end):
    a = bisect.bisect_left(days, start)
    b = bisect.bisect_right(days, end)
    return days[a:b]


# ---------------------------------------------------------------- the result shape

def new_run(days, capital):
    return {"days": list(days), "ret": [0.0] * len(days), "bench": None, "equity": [float(capital)] * len(days),
            "exposure": [0.0] * len(days), "trades": [], "traded": 0.0, "charges": 0.0, "notes": [], "extra": {},
            "capital": float(capital)}


def finish(run, pnl):
    """Fill ret and equity from the daily rupee P&L."""
    eq = run["capital"]
    for i, p in enumerate(pnl):
        run["ret"][i] = p / eq if eq > 0 else 0.0
        eq += p
        run["equity"][i] = eq
    return run


# ---------------------------------------------------------------- metrics

def _curve_dd(rets):
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        dd = max(dd, 1.0 - eq / peak if peak > 0 else 0.0)
    return dd


def _ann(rets):
    n = len(rets)
    if n == 0:
        return {"cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "sortino": 0.0, "max_dd": 0.0, "total": 0.0}
    tot = 1.0
    for r in rets:
        tot *= (1.0 + r)
    yrs = n / float(YEAR)
    cagr = tot ** (1.0 / yrs) - 1.0 if tot > 0 and yrs > 0 else -1.0
    s = sd(rets)
    down = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / float(n))
    return {"cagr": cagr, "vol": s * math.sqrt(YEAR), "sharpe": mean(rets) / s * math.sqrt(YEAR) if s > 0 else 0.0,
            "sortino": mean(rets) / down * math.sqrt(YEAR) if down > 0 else 0.0, "max_dd": _curve_dd(rets),
            "total": tot - 1.0}


def monthly(days, rets):
    out, cur, acc = [], None, 1.0
    for d, r in zip(days, rets):
        k = month_key(d)
        if k != cur:
            if cur is not None:
                out.append((cur, acc - 1.0))
            cur, acc = k, 1.0
        acc *= (1.0 + r)
    if cur is not None:
        out.append((cur, acc - 1.0))
    return out


def metrics(run):
    """Portfolio metrics of one run, after charges: CAGR, volatility, Sharpe, Sortino, max drawdown, turnover,
    hit rate, exposure, the trades' expectancy and profit factor, and the same against the benchmark."""
    rets, days = run["ret"], run["days"]
    a = _ann(rets)
    tr = run["trades"]
    nets = [t["net"] for t in tr]
    wins = [x for x in nets if x > 0]
    gl = -sum(x for x in nets if x <= 0)
    avg_eq = mean(run["equity"]) or run["capital"]
    yrs = len(days) / float(YEAR) if days else 0.0
    m = monthly(days, rets)
    out = {
        "sessions": len(days), "from": days[0] if days else None, "to": days[-1] if days else None,
        "years": r4(yrs, 2), "months": len(m),
        "total_return_pct": r4(a["total"] * 100, 3), "cagr_pct": r4(a["cagr"] * 100, 3),
        "vol_pct": r4(a["vol"] * 100, 3), "sharpe": r4(a["sharpe"], 3), "sortino": r4(a["sortino"], 3),
        "max_drawdown_pct": r4(a["max_dd"] * 100, 3),
        "net": round(sum(p for p in _pnl(run)), 2), "charges": round(run["charges"], 2),
        "charges_pct_per_year": r4(run["charges"] / avg_eq / yrs * 100, 3) if yrs > 0 and avg_eq > 0 else 0.0,
        "turnover_pct_per_year": r4(run["traded"] / 2.0 / avg_eq / yrs * 100, 2) if yrs > 0 and avg_eq > 0 else 0.0,
        "exposure_pct": r4(mean(run["exposure"]) * 100, 2),
        "trades": len(tr), "hit_rate": r4(len(wins) / float(len(tr)), 4) if tr else 0.0,
        "expectancy_pct": r4(mean([t["ret"] for t in tr]) * 100, 4) if tr else 0.0,
        "profit_factor": r4(sum(wins) / gl, 3) if gl > 0 else None,
        "positive_months": r4(sum(1 for _, x in m if x > 0) / float(len(m)), 4) if m else 0.0,
        "sharpe_daily": r4(mean(rets) / sd(rets), 5) if sd(rets) > 0 else 0.0,
    }
    b = run.get("bench")
    if b:
        ba = _ann(b)
        act = [x - y for x, y in zip(rets, b)]
        # the active curve: the strategy's growth over the benchmark's
        rel, peak, add = 1.0, 1.0, 0.0
        sg, bg = 1.0, 1.0
        for x, y in zip(rets, b):
            sg *= 1 + x
            bg *= 1 + y
            rel = sg / bg if bg > 0 else rel
            peak = max(peak, rel)
            add = max(add, 1 - rel / peak)
        am = monthly(days, act)
        pos_total = sum(x for _, x in am)
        best2 = sum(sorted((x for _, x in am), reverse=True)[:2]) if am else 0.0
        out.update({
            "bench_cagr_pct": r4(ba["cagr"] * 100, 3), "bench_sharpe": r4(ba["sharpe"], 3),
            "bench_max_drawdown_pct": r4(ba["max_dd"] * 100, 3), "bench_vol_pct": r4(ba["vol"] * 100, 3),
            "active_return_pct": r4((a["cagr"] - ba["cagr"]) * 100, 3),
            "active_total_pct": r4((sg / bg - 1) * 100 if bg > 0 else 0.0, 3),
            "active_sharpe": r4(mean(act) / sd(act) * math.sqrt(YEAR), 3) if sd(act) > 0 else 0.0,
            "active_sharpe_daily": r4(mean(act) / sd(act), 5) if sd(act) > 0 else 0.0,
            "active_max_drawdown_pct": r4(add * 100, 3),
            "best_two_months_share": r4(best2 / pos_total, 4) if pos_total > 0 else None,
        })
    return out


def _pnl(run):
    out, prev = [], run["capital"]
    for e in run["equity"]:
        out.append(e - prev)
        prev = e
    return out


def active(run):
    b = run.get("bench")
    return [x - y for x, y in zip(run["ret"], b)] if b else list(run["ret"])


# ---------------------------------------------------------------- statistics the lab does not have

def stationary_bootstrap_p(x, B=2000, block=10, seed=7):
    """One-sided p-value that the mean of x is above zero: the share of stationary-bootstrap means
    (Politis and Romano 1994, mean block length `block`) of the de-meaned series at or above the observed
    mean. 1.0 for an empty series."""
    n = len(x)
    if n < 2:
        return 1.0
    m = mean(x)
    z = [v - m for v in x]
    rng = random.Random(seed)
    p = 1.0 / block
    hits = 0
    for _ in range(B):
        i = rng.randrange(n)
        s = 0.0
        for _k in range(n):
            s += z[i]
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
        if s / n >= m:
            hits += 1
    return (hits + 1.0) / (B + 1.0)


def benjamini_hochberg(pvals, q):
    """{key: kept?} for {key: p} at false-discovery rate q."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    cut = 0
    for k, (_, p) in enumerate(items, 1):
        if p <= q * k / float(m):
            cut = k
    keep = set(k for k, _ in items[:cut])
    return {k: (k in keep) for k in pvals}


def ols(x, y):
    """(intercept, slope, slope t-stat, residuals) of y = a + b x."""
    n = len(x)
    mx, my = mean(x), mean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if n < 3 or sxx <= 0:
        return my, 0.0, 0.0, [v - my for v in y]
    b = sum((u - mx) * (v - my) for u, v in zip(x, y)) / sxx
    a = my - b * mx
    res = [v - a - b * u for u, v in zip(x, y)]
    s2 = sum(e * e for e in res) / float(n - 2)
    se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    return a, b, (b / se if se > 0 else 0.0), res


def correlation(x, y):
    n = len(x)
    if n < 3:
        return 0.0
    mx, my = mean(x), mean(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx <= 0 or sy <= 0:
        return 0.0
    return sum((u - mx) * (v - my) for u, v in zip(x, y)) / (sx * sy)
