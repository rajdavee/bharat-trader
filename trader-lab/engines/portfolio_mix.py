"""A portfolio of strategies: combine the WATCH and PROMOTE survivors, size them, and test the mix honestly.

Pure functions on the lab's own files (trader/lab/<name>/equity.csv, results.json, status.json, forward.json and
trader/lab/edge-hunt/<id>/state.json, returns.json). lab.py's `portfolio` command reads and writes; nothing here
sizes an order, changes a config or places anything.

  return streams   one daily return series a strategy, after charges and slippage, from data its parameters
                   never saw: a lab run's out-of-sample (walk-forward) days from equity.csv (P&L / the run's
                   capital) followed by its paper forward record (forward.json, on the exit day); an edge-hunt
                   hypothesis's validation and sealed-holdout days (returns.json; the train window is in sample
                   and left out). The in-sample days are never used.
  alignment        only the period every stream covers: from the latest first day to the earliest last day. The
                   calendar is every session any stream has in that window; a stream with no row on a session
                   was flat (0). The overlap, each stream's coverage of it and every pair's own overlap are
                   reported. Nothing is filled outside a stream's span.
  correlation      Pearson; Spearman (ranks, robust to a few huge days); downside (the lower semi-correlation,
                   sum min(x,0)min(y,0) over the root of the sums of squares: how the losing days line up);
                   stress (Pearson on the worst 10% of the equal-weight mix's days); weekly (Pearson on weekly
                   sums, for swing trades booked on their exit day).
  near-duplicates  single-linkage clusters of strategies whose Pearson, Spearman or weekly correlation is at
                   or above `duplicate_corr` (0.7): the same bet twice. Sized as one cluster in spirit, warned.
  sizing           equal capital; equal risk (inverse volatility); risk parity (equal risk contribution,
                   cyclical coordinate descent, Griveau-Billion, Richard and Roncalli 2013); capped fractional
                   Kelly (f = fraction x Sigma^-1 mu on a shrunk mean, mu x max(0, 1 - 1/t^2), long only, a
                   shrunk covariance, the hard cap, and a warning). Then a per-strategy cap (water-filled), a
                   gross cap (no leverage) and the volatility target, which only scales DOWN (cash).
  walk-forward     weights fitted on the `lookback` sessions BEFORE each rebalance only (first after `fit`
                   sessions, then every `refit`); each bucket compounds on its own between rebalances. The
                   scored days start after the first fit: no weight ever saw the day it is scored on.
  metrics          CAGR, volatility, Sharpe, Sortino, max drawdown, worst month, time in drawdown, the
                   diversification ratio (sum w_i s_i / s_p; its square is roughly the number of independent
                   bets), each strategy's share of the risk (w_i (Sigma w)_i / w' Sigma w) and of the return.
  bands            a stationary block bootstrap (Politis and Romano 1994, mean block 10) of the walk-forward
                   mix's daily returns: the 5th, 50th and 95th percentile equity paths, final returns and max
                   drawdowns. Same-day returns move together, so the cross-correlation is kept.
  proposals        an "autopilot" block of capital buckets for trader/config.json and a drawdown limit for
                   trader-risk, written as proposal files. Never applied: the user copies them by hand.

Education and research tooling, not financial advice. Nothing here places an order.
"""

import datetime
import json
import math
import os
import random

from . import core

SCHEMA = "bharat-trader/lab-portfolio/1"
SCHEMA_PROPOSAL = "bharat-trader/portfolio-proposal/1"
SCHEMES = ("equal", "inverse-vol", "risk-parity", "kelly")
SCHEME_WORDS = {"equal": "equal capital", "inverse-vol": "equal risk (inverse volatility)",
                "risk-parity": "risk parity (equal risk contribution)", "kelly": "capped fractional Kelly"}
ELIGIBLE = ("WATCH", "PROMOTE")
DEFAULTS = {
    "scheme": "risk-parity",     # the scheme the proposal uses: chosen BEFORE the run, never by its backtest
    "vol_target_pct": 10.0,      # the mix's annual volatility target (it only scales down: cash)
    "max_weight": 0.6,           # the most one strategy may get, a share of the capital (0.5 or less forces
                                 # two strategies to equal weights)
    "max_gross": 1.0,            # the sum of the weights: 1.0 = no leverage
    "fit": 126,                  # sessions before the first weights are fitted
    "refit": 21,                 # sessions between rebalances
    "lookback": 252,             # the fitting window (rolling, past sessions only)
    "kelly_fraction": 0.25,      # quarter Kelly
    "kelly_cap": 0.25,           # the hard cap on any one Kelly weight
    "shrink_cov": 0.2,           # covariance shrunk this much toward its diagonal (Kelly only)
    "duplicate_corr": 0.7,       # the same bet at or above this correlation
    "boot_runs": 1000,
    "boot_block": 10,
    "seed": 16,
    "min_overlap": 60,           # sessions every stream must share to build anything
}
LIMITS = [
    "It does not predict the future: every weight and every number is fitted on the same past the strategies "
    "were tested on, and correlations between strategies change, most of all in a crash.",
    "With few strategies there is little to diversify: two or three streams give at most two or three bets, "
    "and fewer when they are correlated.",
    "Correlation is measured on days when strategies are often flat; strategies that rarely trade on the same "
    "day look uncorrelated until the day they both lose.",
    "Lab runs book a trade's P&L on its exit day, not marked to market daily, so swing strategies' daily "
    "correlation is understated (the weekly matrix is the better read).",
    "The bootstrap bands reshuffle the mix's own past days: they show luck in the order of those days, not new "
    "kinds of days.",
    "Moving capital between paper buckets costs nothing here; the strategies' own charges are in their returns.",
    "The stress correlation is measured on days picked because the mix lost: such a sample is biased (one big "
    "loser with the other flat reads as negative), so only a clear rise against the other days means much.",
]


class MixError(ValueError):
    pass


# ---------------------------------------------------------------- reading the lab's files

def _read_json(path, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return default


def _equity_rows(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        head = fh.readline().strip().split(",")
        for line in fh:
            v = dict(zip(head, line.strip().split(",")))
            try:
                rows.append((v["date"], float(v["pnl"]), v.get("out_of_sample") == "1"))
            except (KeyError, ValueError):
                continue
    return rows


def _weekdays(after, until):
    """Weekdays after `after` up to `until` (NSE holidays count as flat days: a small dilution, said so)."""
    out = []
    d, end = core.day(after) + datetime.timedelta(days=1), core.day(until)
    while d <= end:
        if d.weekday() < 5:
            out.append(core.iso(d))
        d += datetime.timedelta(days=1)
    return out


def lab_stream(out, name, use_forward=True):
    """A lab run's out-of-sample days and paper forward record as one daily return stream, or None."""
    d = os.path.join(out, name)
    res = _read_json(os.path.join(d, "results.json"))
    st = _read_json(os.path.join(d, "status.json"), {}) or {}
    if not isinstance(res, dict):
        return None
    cap = float((res.get("config") or {}).get("capital") or 0)
    rows = _equity_rows(os.path.join(d, "equity.csv"))
    if cap <= 0 or not rows:
        return None
    oos = [(x, p / cap) for x, p, o in rows if o]
    segs, notes = [], []
    if oos:
        segs.append({"source": "backtest out of sample", "from": oos[0][0], "to": oos[-1][0], "sessions": len(oos)})
    series = list(oos)
    if use_forward:
        fwd = _read_json(os.path.join(d, "forward.json"), {}) or {}
        h = st.get("spec_hash") or res.get("spec_hash")
        cut = (res.get("data") or {}).get("to") or (rows[-1][0] if rows else None)
        trades = [t for t in fwd.get("trades") or [] if t.get("spec_hash") == h and str(t.get("exit_date")) > str(cut)]
        if trades and cut:
            end = max([str(t["exit_date"])[:10] for t in trades] + [str(fwd.get("updated") or "")[:10]])
            days = _weekdays(cut, end)
            pnl = {}
            for t in trades:
                pnl[str(t["exit_date"])[:10]] = pnl.get(str(t["exit_date"])[:10], 0.0) + float(t.get("net") or 0)
            if series and days and days[0] <= series[-1][0]:
                days = [x for x in days if x > series[-1][0]]
            if days:
                series += [(x, pnl.get(x, 0.0) / cap) for x in days]
                segs.append({"source": "paper forward record", "from": days[0], "to": days[-1], "sessions": len(days),
                             "trades": len(trades)})
                notes.append("forward days are weekdays after the backtest (an exchange holiday counts as a flat day)")
    if not series:
        return None
    return {"name": name, "kind": res.get("kind") or "equity", "source": "lab", "verdict": st.get("verdict") or
            res.get("verdict"), "title": res.get("title") or name, "capital": cap,
            "timeframe": res.get("timeframe"), "series": series, "segments": segs, "notes": notes,
            "tradable": True}


def hunt_stream(out, hid):
    """An edge-hunt hypothesis's validation and holdout days (net of charges), or None."""
    d = os.path.join(out, "edge-hunt", hid)
    st = _read_json(os.path.join(d, "state.json"), {}) or {}
    rr = _read_json(os.path.join(d, "returns.json"))
    series, segs, notes = [], [], []
    if isinstance(rr, dict) and rr.get("segments"):
        for k in ("validation", "holdout"):
            s = rr["segments"].get(k)
            if not s or not s.get("days"):
                continue
            pairs = [(x, float(v)) for x, v in zip(s["days"], s["ret"]) if not series or x > series[-1][0]]
            if pairs:
                series += pairs
                segs.append({"source": "edge hunt " + k, "from": pairs[0][0], "to": pairs[-1][0],
                             "sessions": len(pairs)})
    else:
        res = _read_json(os.path.join(d, "results.json"), {}) or {}
        fin = res.get("finalist")
        pm = ((res.get("plan") or {}).get("primary_metric")) or ""
        trials = []
        p = os.path.join(d, "trials.jsonl")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            trials.append(json.loads(line))
                        except ValueError:
                            continue
        row = next((t for t in trials if t.get("trial") == fin), None) if fin is not None else None
        if row is None or pm == "oos_active_sharpe" or not row.get("val_series"):
            return None
        val = row["val_series"]
        days = row["dates"][-len(val):]
        series = list(zip(days, [float(v) for v in val]))
        segs.append({"source": "edge hunt validation", "from": days[0], "to": days[-1], "sessions": len(days)})
        notes.append("validation days only, from trials.jsonl (decided before returns.json was written)")
    if not series:
        return None
    return {"name": hid, "kind": "edge-hunt", "source": "edge-hunt", "verdict": st.get("verdict"),
            "title": hid, "capital": None, "timeframe": "1d", "series": series, "segments": segs, "notes": notes,
            "tradable": False}


def candidates(out):
    """Every lab run and decided edge-hunt run with its verdict: [{name, source, verdict}]."""
    rows = []
    if os.path.isdir(out):
        for n in sorted(os.listdir(out)):
            if n in ("portfolio", "edge-hunt", "data"):
                continue
            st = _read_json(os.path.join(out, n, "status.json"))
            if isinstance(st, dict) and os.path.isfile(os.path.join(out, n, "equity.csv")):
                rows.append({"name": n, "source": "lab", "verdict": st.get("verdict")})
    eh = os.path.join(out, "edge-hunt")
    if os.path.isdir(eh):
        for n in sorted(os.listdir(eh)):
            st = _read_json(os.path.join(eh, n, "state.json"))
            if isinstance(st, dict) and st.get("verdict"):
                rows.append({"name": n, "source": "edge-hunt", "verdict": st.get("verdict")})
    return rows


def collect(out, names=None, research=False, use_forward=True):
    """The return streams to combine: every WATCH or PROMOTE strategy, or the named ones. A named strategy with
    another verdict needs research=True and is then flagged: it is studied, never proposed."""
    cands = {c["name"]: c for c in candidates(out)}
    pick = list(names) if names else [n for n, c in sorted(cands.items()) if c["verdict"] in ELIGIBLE]
    streams, skipped = [], []
    for n in pick:
        c = cands.get(n)
        if c is None:
            raise MixError("no lab run or decided edge-hunt run named %s in %s" % (n, out))
        if c["verdict"] not in ELIGIBLE and not research:
            raise MixError("%s is %s: only WATCH and PROMOTE strategies go into a portfolio (add --research to "
                           "study it anyway; it is never proposed)" % (n, c["verdict"]))
        s = lab_stream(out, n, use_forward) if c["source"] == "lab" else hunt_stream(out, n)
        if s is None:
            skipped.append({"name": n, "why": "no out-of-sample daily returns recorded"})
            continue
        s["eligible"] = c["verdict"] in ELIGIBLE
        streams.append(s)
    return streams, skipped


# ---------------------------------------------------------------- alignment

def align(streams):
    """The common window of every stream: (days, matrix rows [r_1..r_k] per day, info)."""
    if not streams:
        raise MixError("no strategies to combine")
    first = max(s["series"][0][0] for s in streams)
    last = min(s["series"][-1][0] for s in streams)
    spans = [{"name": s["name"], "from": s["series"][0][0], "to": s["series"][-1][0], "sessions": len(s["series"])}
             for s in streams]
    if first > last:
        return [], [], {"from": first, "to": last, "sessions": 0, "spans": spans, "coverage": {}, "pairs": []}
    maps = [dict(s["series"]) for s in streams]
    days = sorted(set(x for m in maps for x in m if first <= x <= last))
    rows = [[m.get(x, 0.0) for m in maps] for x in days]
    cov = {s["name"]: round(sum(1 for x in days if x in m) / float(len(days)), 4) if days else 0.0
           for s, m in zip(streams, maps)}
    pairs = []
    for i in range(len(streams)):
        for j in range(i + 1, len(streams)):
            a = max(spans[i]["from"], spans[j]["from"])
            b = min(spans[i]["to"], spans[j]["to"])
            n = len(set(x for x in list(maps[i]) + list(maps[j]) if a <= x <= b)) if a <= b else 0
            pairs.append({"a": streams[i]["name"], "b": streams[j]["name"], "from": a, "to": b, "sessions": n})
    return days, rows, {"from": first, "to": last, "sessions": len(days), "spans": spans, "coverage": cov,
                        "pairs": pairs}


# ---------------------------------------------------------------- statistics (stdlib)

def columns(rows):
    return [list(c) for c in zip(*rows)] if rows else []


def covariance(rows):
    cols = columns(rows)
    k, n = len(cols), len(rows)
    mu = [core.mean(c) for c in cols]
    out = [[0.0] * k for _ in range(k)]
    if n < 2:
        return out
    for i in range(k):
        for j in range(i, k):
            v = sum((cols[i][t] - mu[i]) * (cols[j][t] - mu[j]) for t in range(n)) / (n - 1.0)
            out[i][j] = out[j][i] = v
    return out


def _ranks(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def pearson(x, y):
    return core.correlation(x, y)


def spearman(x, y):
    return core.correlation(_ranks(x), _ranks(y))


def downside(x, y):
    """The lower semi-correlation: sum min(x,0) min(y,0) / sqrt(sum min(x,0)^2 sum min(y,0)^2)."""
    a = [min(v, 0.0) for v in x]
    b = [min(v, 0.0) for v in y]
    sa, sb = math.sqrt(sum(v * v for v in a)), math.sqrt(sum(v * v for v in b))
    if sa <= 0 or sb <= 0:
        return 0.0
    return sum(u * v for u, v in zip(a, b)) / (sa * sb)


def weekly(days, rows):
    """Weekly sums of each column (ISO weeks)."""
    out, cur, acc = [], None, None
    for d, r in zip(days, rows):
        k = core.day(d).isocalendar()[:2]
        if k != cur:
            if acc is not None:
                out.append(acc)
            cur, acc = k, [0.0] * len(r)
        acc = [a + v for a, v in zip(acc, r)]
    if acc is not None:
        out.append(acc)
    return out


def matrix(cols, fn):
    k = len(cols)
    m = [[1.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i + 1, k):
            m[i][j] = m[j][i] = round(fn(cols[i], cols[j]), 4)
    return m


def correlations(days, rows):
    cols = columns(rows)
    k = len(cols)
    ew = [core.mean(r) for r in rows]
    worst = sorted(range(len(rows)), key=lambda t: ew[t])[:max(5, len(rows) // 10)]
    rest = [t for t in range(len(rows)) if t not in set(worst)]
    stress_cols = [[c[t] for t in worst] for c in cols]
    calm_cols = [[c[t] for t in rest] for c in cols]
    wrows = weekly(days, rows)
    wk = columns(wrows)
    both = [[0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            act = sum(1 for t in range(len(rows)) if cols[i][t] != 0 or cols[j][t] != 0)
            co = sum(1 for t in range(len(rows)) if cols[i][t] != 0 and cols[j][t] != 0)
            both[i][j] = round(co / float(act), 4) if act else 0.0
    half = len(rows) // 2
    return {"pearson": matrix(cols, pearson), "spearman": matrix(cols, spearman), "downside": matrix(cols, downside),
            "stress": matrix(stress_cols, pearson), "calm": matrix(calm_cols, pearson),
            "weekly": matrix(wk, pearson) if len(wrows) >= 8 else None,
            "first_half": matrix([c[:half] for c in cols], pearson),
            "second_half": matrix([c[half:] for c in cols], pearson),
            "active_together": both, "stress_days": len(worst), "weeks": len(wrows)}


def clusters(names, corr, thr):
    """Single-linkage groups at correlation >= thr on the largest of Pearson, Spearman and weekly."""
    k = len(names)
    parent = list(range(k))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    links = []
    for i in range(k):
        for j in range(i + 1, k):
            vals = [corr["pearson"][i][j], corr["spearman"][i][j]]
            if corr.get("weekly"):
                vals.append(corr["weekly"][i][j])
            top = max(vals)
            if top >= thr:
                parent[find(i)] = find(j)
                links.append({"a": names[i], "b": names[j], "corr": round(top, 4)})
    groups = {}
    for i in range(k):
        groups.setdefault(find(i), []).append(names[i])
    return {"groups": sorted(groups.values(), key=lambda g: (-len(g), g)), "links": links, "threshold": thr,
            "duplicates": [g for g in groups.values() if len(g) > 1]}


# ---------------------------------------------------------------- sizing

def _mv(m, w):
    return [sum(m[i][j] * w[j] for j in range(len(w))) for i in range(len(w))]


def port_var(cov, w):
    return sum(wi * v for wi, v in zip(w, _mv(cov, w)))


def solve(a, b):
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [list(a[i]) + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-18:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def w_equal(cov, mu, n_obs, cfg):
    k = len(cov)
    return [1.0 / k] * k, []


def w_inverse_vol(cov, mu, n_obs, cfg):
    sd = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(len(cov))]
    inv = [1.0 / s if s > 0 else 0.0 for s in sd]
    tot = sum(inv)
    notes = ["%d strateg%s had no variance in the window and got nothing" % (
        sum(1 for s in sd if s <= 0), "y" if sum(1 for s in sd if s <= 0) == 1 else "ies")] if 0 in inv else []
    return ([x / tot for x in inv] if tot > 0 else [0.0] * len(inv)), notes


def risk_parity(cov, budget=None, tol=1e-10, max_iter=10000):
    """Equal risk contribution weights (sum 1) by cyclical coordinate descent on
    0.5 y' S y - sum b_i ln y_i, whose minimum has y_i (S y)_i = b_i; w = y / sum y."""
    k = len(cov)
    b = budget or [1.0 / k] * k
    live = [i for i in range(k) if cov[i][i] > 0]
    y = [0.0] * k
    for i in live:
        y[i] = 1.0 / math.sqrt(cov[i][i])
    for _ in range(max_iter):
        delta = 0.0
        for i in live:
            c = sum(cov[i][j] * y[j] for j in live if j != i)
            new = (-c + math.sqrt(c * c + 4.0 * cov[i][i] * b[i])) / (2.0 * cov[i][i])
            delta = max(delta, abs(new - y[i]) / max(abs(new), 1e-300))
            y[i] = new
        if delta < tol:
            break
    tot = sum(y)
    return [v / tot for v in y] if tot > 0 else [0.0] * k


def w_risk_parity(cov, mu, n_obs, cfg):
    return risk_parity(cov), []


def shrunk_means(mu, cov, n_obs):
    """mu x max(0, 1 - 1/t^2), t = mu / (sd / sqrt(n)), floored at 0 (long only): a mean barely above its
    noise is shrunk to nothing."""
    out = []
    for i, m in enumerate(mu):
        sd = math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0
        if m <= 0 or sd <= 0 or n_obs < 2:
            out.append(0.0)
            continue
        t = m / (sd / math.sqrt(n_obs))
        out.append(m * max(0.0, 1.0 - 1.0 / (t * t)))
    return out


def w_kelly(cov, mu, n_obs, cfg):
    k = len(cov)
    lam = float(cfg.get("shrink_cov", 0.2))
    s = [[cov[i][j] * (1.0 if i == j else 1.0 - lam) for j in range(k)] for i in range(k)]
    ms = shrunk_means(mu, cov, n_obs)
    notes = ["Kelly is the most sensitive scheme to estimation error: shrunk means, %g Kelly, a hard cap of %.0f%% "
             "a strategy, and still probably too big" % (cfg["kelly_fraction"], 100 * cfg["kelly_cap"])]
    if not any(m > 0 for m in ms):
        return [0.0] * k, notes + ["no strategy's mean survived the shrinkage: Kelly holds cash"]
    f = solve(s, ms) or [0.0] * k
    f = [max(0.0, cfg["kelly_fraction"] * v) for v in f]
    capped = [min(v, cfg["kelly_cap"]) for v in f]
    if any(a > b + 1e-12 for a, b in zip(f, capped)):
        notes.append("the hard cap of %.0f%% bound" % (100 * cfg["kelly_cap"]))
    return capped, notes


WEIGHTERS = {"equal": w_equal, "inverse-vol": w_inverse_vol, "risk-parity": w_risk_parity, "kelly": w_kelly}


def cap_weights(w, cap):
    """Water-fill: no weight above cap, the excess spread over the others in proportion; the sum is kept unless
    every weight is at the cap."""
    w = list(w)
    tot = sum(w)
    for _ in range(len(w) + 1):
        over = [i for i, v in enumerate(w) if v > cap + 1e-12]
        if not over:
            break
        excess = sum(w[i] - cap for i in over)
        for i in over:
            w[i] = cap
        free = [i for i, v in enumerate(w) if v < cap - 1e-12 and v > 0]
        room = sum(w[i] for i in free)
        if not free or room <= 0:
            break
        for i in free:
            w[i] += excess * w[i] / room
    return [min(v, cap) for v in w] if tot > 0 else w


def fit_weights(scheme, window, cfg):
    """Weights for one scheme on a window of past rows, after the caps and the volatility target."""
    cov = covariance(window)
    mu = [core.mean(c) for c in columns(window)]
    raw, notes = WEIGHTERS[scheme](cov, mu, len(window), cfg)
    if scheme != "kelly":
        tot = sum(raw)
        raw = [v / tot * cfg["max_gross"] for v in raw] if tot > 0 else raw
    w = cap_weights(raw, cfg["max_weight"])
    g = sum(w)
    if g > cfg["max_gross"]:
        w = [v * cfg["max_gross"] / g for v in w]
    vol = math.sqrt(max(port_var(cov, w), 0.0) * core.YEAR)
    scale = 1.0
    tgt = cfg["vol_target_pct"] / 100.0
    if tgt > 0 and vol > tgt:
        scale = tgt / vol
        w = [v * scale for v in w]
    return w, {"vol_before_target_pct": round(vol * 100, 3), "scale": round(scale, 4), "notes": notes,
               "cash": round(1.0 - sum(w), 4)}


def risk_shares(cov, w):
    v = port_var(cov, w)
    sw = _mv(cov, w)
    return [round(wi * x / v, 4) if v > 0 else 0.0 for wi, x in zip(w, sw)]


def diversification_ratio(cov, w):
    v = port_var(cov, w)
    num = sum(wi * math.sqrt(max(cov[i][i], 0.0)) for i, wi in enumerate(w))
    return round(num / math.sqrt(v), 4) if v > 0 else None


# ---------------------------------------------------------------- the walk-forward portfolio backtest

def walk_forward(days, rows, scheme, cfg):
    """Weights fitted on past sessions only, rebalanced every refit sessions; buckets compound in between.
    {"days", "ret", "rebalances": [{date, weights, ...}], "contrib": per-strategy summed return}."""
    fit, refit, look = int(cfg["fit"]), int(cfg["refit"]), int(cfg["lookback"])
    k = len(rows[0]) if rows else 0
    if len(rows) <= fit:
        return None
    V, v, cash = 1.0, [0.0] * k, 1.0
    out_d, out_r, rebal, contrib = [], [], [], [0.0] * k
    w = [0.0] * k
    for t in range(fit, len(rows)):
        if (t - fit) % refit == 0:
            window = rows[max(0, t - look):t]
            w, info = fit_weights(scheme, window, cfg)
            v = [wi * V for wi in w]
            cash = V - sum(v)
            rebal.append({"date": days[t], "fitted_on": [days[max(0, t - look)], days[t - 1]],
                          "weights": [round(x, 4) for x in w], "cash": info["cash"], "scale": info["scale"],
                          "notes": info["notes"]})
        before = V
        gains = [vi * rows[t][i] for i, vi in enumerate(v)]
        v = [vi + g for vi, g in zip(v, gains)]
        V = sum(v) + cash
        for i, g in enumerate(gains):
            contrib[i] += g / before if before > 0 else 0.0
        out_d.append(days[t])
        out_r.append(V / before - 1.0 if before > 0 else 0.0)
    return {"days": out_d, "ret": out_r, "rebalances": rebal, "contrib": contrib}


def curve_stats(days, rets):
    """CAGR, volatility, Sharpe, Sortino, max drawdown, worst month, time in drawdown, all in % where it reads."""
    a = core._ann(rets)
    m = core.monthly(days, rets)
    eq, peak, under = 1.0, 1.0, 0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        under += 1 if eq < peak - 1e-12 else 0
    worst = min(m, key=lambda x: x[1]) if m else (None, 0.0)
    return {"sessions": len(rets), "from": days[0] if days else None, "to": days[-1] if days else None,
            "total_return_pct": core.r4(a["total"] * 100, 3), "cagr_pct": core.r4(a["cagr"] * 100, 3),
            "vol_pct": core.r4(a["vol"] * 100, 3), "sharpe": core.r4(a["sharpe"], 3),
            "sortino": core.r4(a["sortino"], 3), "max_drawdown_pct": core.r4(a["max_dd"] * 100, 3),
            "worst_month": worst[0], "worst_month_pct": core.r4(worst[1] * 100, 3),
            "time_in_drawdown_pct": core.r4(under / float(len(rets)) * 100, 2) if rets else 0.0,
            "positive_months_pct": core.r4(sum(1 for _, x in m if x > 0) / float(len(m)) * 100, 1) if m else None}


def percentile(xs, q):
    s = sorted(xs)
    if not s:
        return None
    k = (len(s) - 1) * q
    lo = int(math.floor(k))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def bootstrap(rets, runs=1000, block=10, seed=16, points=120):
    """Stationary block bootstrap of a daily return series: percentile equity bands (thinned to `points` steps),
    final return, max drawdown and the share of paths that lost."""
    n = len(rets)
    if n < 10:
        return None
    rng = random.Random(seed)
    p = 1.0 / max(1, block)
    steps = sorted(set([int(round(i * (n - 1) / float(max(1, points - 1)))) for i in range(points)]))
    at = {s: [] for s in steps}
    finals, dds = [], []
    for _ in range(runs):
        i = rng.randrange(n)
        eq, peak, dd = 1.0, 1.0, 0.0
        for t in range(n):
            if t > 0:
                i = rng.randrange(n) if rng.random() < p else (i + 1) % n
            eq *= 1 + rets[i]
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
            if t in at:
                at[t].append(eq)
        finals.append(eq - 1)
        dds.append(dd)
    bands = [{"step": s, "p5": round(percentile(at[s], 0.05), 5), "p50": round(percentile(at[s], 0.5), 5),
              "p95": round(percentile(at[s], 0.95), 5)} for s in steps]
    return {"runs": runs, "block": block, "seed": seed, "bands": bands,
            "final_pct": {q: round(percentile(finals, v) * 100, 3) for q, v in (("p5", .05), ("p50", .5), ("p95", .95))},
            "max_drawdown_pct": {q: round(percentile(dds, v) * 100, 3) for q, v in (("p50", .5), ("p95", .95))},
            "prob_loss": round(sum(1 for f in finals if f < 0) / float(runs), 4)}


# ---------------------------------------------------------------- the whole build

def settings(over=None):
    cfg = dict(DEFAULTS)
    for k, v in (over or {}).items():
        if v is None:
            continue
        if k not in DEFAULTS:
            raise MixError("unknown portfolio setting %s (known: %s)" % (k, ", ".join(sorted(DEFAULTS))))
        cfg[k] = v
    if cfg["scheme"] not in SCHEMES:
        raise MixError("scheme is one of %s" % ", ".join(SCHEMES))
    for k in ("vol_target_pct", "max_weight", "max_gross", "kelly_fraction", "kelly_cap", "duplicate_corr"):
        if not isinstance(cfg[k], (int, float)) or isinstance(cfg[k], bool) or cfg[k] < 0:
            raise MixError("%s is a number of 0 or more" % k)
    if not 0 < cfg["max_weight"] <= 1 or not 0 < cfg["max_gross"] <= 1:
        raise MixError("max_weight and max_gross are above 0 and at most 1 (no leverage)")
    if not 0 < cfg["kelly_fraction"] <= 0.5 or not 0 < cfg["kelly_cap"] <= cfg["max_weight"]:
        raise MixError("kelly_fraction is above 0 and at most 0.5; kelly_cap above 0 and at most max_weight")
    for k in ("fit", "refit", "lookback", "boot_runs", "boot_block", "min_overlap"):
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] < 1:
            raise MixError("%s is a whole number from 1" % k)
    if cfg["fit"] < 20 or cfg["lookback"] < 20:
        raise MixError("fit and lookback are at least 20 sessions (a covariance needs data)")
    return cfg


def _honest_notes(streams, info, corr, cl, cfg, wf_days):
    names = [s["name"] for s in streams]
    k = len(streams)
    out = []
    if k < 3:
        out.append("Only %d strateg%s: there is little to diversify, and a portfolio of %d is mostly the sum of "
                   "%s parts." % (k, "y" if k == 1 else "ies", k, "its"))
    if info["sessions"] < 252:
        out.append("The overlap every strategy shares is %d sessions (%s to %s), under a year: correlations and "
                   "volatilities this short are noisy." % (info["sessions"], info["from"], info["to"]))
    for n, c in sorted(info["coverage"].items()):
        if c < 0.8:
            out.append("%s has a row on only %.0f%% of the overlap's sessions (the rest count as flat)." % (n, 100 * c))
    for i in range(k):
        for j in range(i + 1, k):
            calm, stress = corr["calm"][i][j], corr["stress"][i][j]
            if stress - calm >= 0.2:
                out.append("%s and %s: correlation %.2f on the worst %d days of the mix against %.2f on the rest: "
                           "they lose together when it matters." % (names[i], names[j], stress, corr["stress_days"],
                                                                    calm))
            both = corr["active_together"][i][j]
            if both < 0.2:
                out.append("%s and %s traded on the same day only %.0f%% of their active days: a low correlation "
                           "here partly means they are rarely in the market together." % (names[i], names[j],
                                                                                          100 * both))
            a, b = corr["first_half"][i][j], corr["second_half"][i][j]
            if abs(a - b) >= 0.3:
                out.append("%s and %s: correlation moved from %.2f in the first half to %.2f in the second: it is "
                           "not stable." % (names[i], names[j], a, b))
    for g in cl["duplicates"]:
        out.append("Same bet: %s correlate at %.2f or more. Treat them as one strategy (keep the simpler one) or "
                   "give the group one bucket." % (" and ".join(g), cl["threshold"]))
    if not wf_days:
        out.append("The overlap is too short for a walk-forward test of the weights (%d sessions; the first fit "
                   "needs %d)." % (info["sessions"], cfg["fit"]))
    if any(not s["eligible"] for s in streams):
        out.append("Research only: %s %s not WATCH or PROMOTE, so no proposal includes %s." % (
            ", ".join(s["name"] for s in streams if not s["eligible"]),
            "is" if sum(1 for s in streams if not s["eligible"]) == 1 else "are",
            "it" if sum(1 for s in streams if not s["eligible"]) == 1 else "them"))
    if any(not s["tradable"] for s in streams):
        out.append("Edge-hunt hypotheses (%s) have no lab spec the auto-pilot can trade yet: their weight is shown "
                   "but left out of the auto-pilot proposal." % ", ".join(s["name"] for s in streams
                                                                         if not s["tradable"]))
    return out


def build(name, out, names=None, research=False, cfg=None, capital=None, workdir_cfg=None, now=None,
          use_forward=True):
    """Collect, align, correlate, size and walk-forward test a mix. Returns the portfolio document (nothing is
    written here)."""
    cfg = settings(cfg)
    streams, skipped = collect(out, names, research, use_forward)
    if len(streams) < 2:
        have = ", ".join(s["name"] for s in streams) or "none"
        raise MixError("a portfolio needs at least 2 strategies with out-of-sample daily returns (have: %s%s). "
                       "Most ideas are REJECT; that is the lab working. Name runs with --strategies and add "
                       "--research to study a mix of them anyway." % (
                           have, "; skipped: " + ", ".join(x["name"] for x in skipped) if skipped else ""))
    days, rows, info = align(streams)
    if info["sessions"] < cfg["min_overlap"]:
        raise MixError("the strategies share only %d sessions (%s to %s; need %d): their spans are %s" % (
            info["sessions"], info["from"], info["to"], cfg["min_overlap"],
            "; ".join("%s %s to %s" % (s["name"], s["from"], s["to"]) for s in info["spans"])))
    k = len(streams)
    labels = [s["name"] for s in streams]
    corr = correlations(days, rows)
    cl = clusters(labels, corr, cfg["duplicate_corr"])
    cov_all = covariance(rows)
    schemes = {}
    first_wf = None
    for sc in SCHEMES:
        w_all, inf_all = fit_weights(sc, rows, cfg)
        wf = walk_forward(days, rows, sc, cfg)
        entry = {"label": SCHEME_WORDS[sc], "hindsight_weights": [round(x, 4) for x in w_all],
                 "hindsight_note": "fitted on the whole overlap: for reading only, not tested",
                 "hindsight_vol_pct": inf_all["vol_before_target_pct"], "notes": inf_all["notes"]}
        if wf:
            first_wf = first_wf or wf
            last = wf["rebalances"][-1]
            win = rows[max(0, len(rows) - cfg["lookback"]):]
            cov_last = covariance(win)
            entry.update({
                "metrics": curve_stats(wf["days"], wf["ret"]),
                "weights": last["weights"], "weights_date": last["date"], "fitted_on": last["fitted_on"],
                "cash": last["cash"], "vol_scale": last["scale"],
                "rebalances": [{"date": r["date"], "weights": r["weights"], "cash": r["cash"]}
                               for r in wf["rebalances"]],
                "risk_share": risk_shares(cov_last, last["weights"]),
                "return_share": [round(c, 5) for c in wf["contrib"]],
                "diversification_ratio": diversification_ratio(cov_last, last["weights"]),
                "bootstrap": bootstrap(wf["ret"], cfg["boot_runs"], cfg["boot_block"], cfg["seed"]),
                "curve": thin([{"date": d, "equity": e} for d, e in zip(wf["days"], _equity(wf["ret"]))], 400),
                "daily": [round(r, 7) for r in wf["ret"]]})
            if entry["bootstrap"]:
                for x in entry["bootstrap"]["bands"]:
                    x["date"] = wf["days"][x["step"]]
            dr = entry["diversification_ratio"]
            entry["effective_bets"] = round(dr * dr, 2) if dr else None
        schemes[sc] = entry
    scored = first_wf["days"] if first_wf else []
    standalone = []
    for i, s in enumerate(streams):
        lo = len(days) - len(scored)
        col = [r[i] for r in rows[lo:]] if scored else [r[i] for r in rows]
        standalone.append(dict(curve_stats(scored or days, col), name=s["name"]))
    sd = [math.sqrt(cov_all[i][i] * core.YEAR) * 100 if cov_all[i][i] > 0 else 0.0 for i in range(k)]
    doc = {
        "schema": SCHEMA, "name": name, "built": (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "settings": cfg, "strategies": [
            {"name": s["name"], "title": s["title"], "source": s["source"], "kind": s["kind"],
             "verdict": s["verdict"], "eligible": s["eligible"], "tradable": s["tradable"], "capital": s["capital"],
             "segments": s["segments"], "notes": s["notes"], "vol_pct": round(sd[i], 3),
             "mean_daily_pct": round(core.mean([r[i] for r in rows]) * 100, 5)}
            for i, s in enumerate(streams)],
        "skipped": skipped, "overlap": info, "correlation": corr, "clusters": cl,
        "schemes": schemes, "chosen": cfg["scheme"],
        "chosen_why": "%s was set before the run (--scheme, default risk parity); it is not the scheme with the "
                      "best backtest, which would be one more fitted choice" % SCHEME_WORDS[cfg["scheme"]],
        "scored": {"from": scored[0] if scored else None, "to": scored[-1] if scored else None,
                   "sessions": len(scored)},
        "scored_days": scored,
        "standalone": standalone,
        "notes": _honest_notes(streams, info, corr, cl, cfg, scored),
        "limits": LIMITS,
        "disclaimer": "Paper research. Education and tooling, not financial advice.",
    }
    doc["proposals"] = proposals(doc, capital, workdir_cfg)
    return doc


def _equity(rets):
    out, eq = [], 1.0
    for r in rets:
        eq *= 1 + r
        out.append(round(eq, 6))
    return out


def thin(rows, n):
    if len(rows) <= n:
        return rows
    step = len(rows) / float(n)
    keep = sorted(set(int(i * step) for i in range(n)) | {len(rows) - 1})
    return [rows[i] for i in keep]


# ---------------------------------------------------------------- proposals (never applied)

def _round_to(x, step):
    return int(round(float(x) / step)) * step


def proposals(doc, capital=None, workdir_cfg=None):
    """An "autopilot.strategies" capital-bucket block and a drawdown limit for trader-risk, from the chosen
    scheme's latest walk-forward weights. Proposals only: nothing here edits trader/config.json."""
    wc = workdir_cfg or {}
    sc = doc["schemes"][doc["chosen"]]
    if not sc.get("weights"):
        return {"autopilot": None, "risk": None,
                "why": "no walk-forward weights (the overlap is too short): no proposal"}
    ap_old = (wc.get("autopilot") or {}).get("strategies") or {}
    total = capital
    basis = "--capital"
    if not total:
        total, basis = wc.get("capital"), "trader/config.json capital"
    if not total and ap_old:
        total = sum(float((b or {}).get("capital") or 0) for b in ap_old.values()) or None
        basis = "the auto-pilot's current buckets"
    strategies, left_out = {}, []
    daily = sc["daily"]
    for i, s in enumerate(doc["strategies"]):
        w = sc["weights"][i]
        if not s["eligible"]:
            left_out.append({"name": s["name"], "weight": w, "why": "%s, studied only" % s["verdict"]})
            continue
        if not s["tradable"]:
            left_out.append({"name": s["name"], "weight": w, "why": "an edge-hunt hypothesis with no lab spec yet"})
            continue
        if w <= 0:
            left_out.append({"name": s["name"], "weight": w, "why": "weight 0"})
            continue
        b = {"weight": w}
        if total:
            b["capital"] = _round_to(w * float(total), 1000) or 1000
        strategies[s["name"]] = b
    mix_loss = [-r for r in daily if r < 0]
    p99 = percentile(mix_loss, 0.99) if mix_loss else 0.0
    worst_day = max(mix_loss) if mix_loss else 0.0
    boot = sc.get("bootstrap") or {}
    dd95 = (boot.get("max_drawdown_pct") or {}).get("p95") or sc["metrics"]["max_drawdown_pct"]
    cur = wc.get("max_daily_drawdown_pct")
    daily_pct = round(max(0.5, min(p99 * 100 * 1.5, float(cur) if cur else 3.0)), 1) if daily else None
    ap = {"schema": SCHEMA_PROPOSAL, "kind": "autopilot buckets", "portfolio": doc["name"], "applied": False,
          "scheme": doc["chosen"], "weights_date": sc["weights_date"], "fitted_on": sc["fitted_on"],
          "capital_total": total, "capital_basis": basis if total else None,
          "autopilot": {"strategies": {n: {"capital": b["capital"]} if "capital" in b else {"weight": b["weight"]}
                                       for n, b in sorted(strategies.items())}},
          "weights": {n: b["weight"] for n, b in sorted(strategies.items())},
          "left_out": left_out, "cash_weight": sc["cash"],
          "how": ("A proposal, never applied. To adopt it, copy the capital numbers into the \"autopilot\" block "
                  "of trader/config.json yourself, keeping your other keys (max_positions, max_daily_loss...). "
                  "The auto-pilot trades PAPER; every order still needs trader-risk PASS."),
          "why": ("%s weights fitted on %s to %s (past sessions only), capped at %.0f%% a strategy, scaled to a "
                  "%.0f%% volatility target. The cash share stays unallocated." % (
                      SCHEME_WORDS[doc["chosen"]], sc["fitted_on"][0], sc["fitted_on"][1],
                      100 * doc["settings"]["max_weight"], doc["settings"]["vol_target_pct"]))}
    if not total:
        ap["capital_note"] = ("no capital known (no --capital, no capital in trader/config.json, no auto-pilot "
                              "buckets): the proposal gives weights only")
    risk = {"schema": SCHEMA_PROPOSAL, "kind": "risk limits", "portfolio": doc["name"], "applied": False,
            "max_daily_drawdown_pct": daily_pct, "current_max_daily_drawdown_pct": cur,
            "portfolio_drawdown_pause_pct": round(max(1.0, dd95 * 1.25), 1),
            "evidence": {"mix_daily_loss_p99_pct": round(p99 * 100, 3), "mix_worst_day_pct": round(worst_day * 100, 3),
                         "bootstrap_max_drawdown_p95_pct": dd95, "walk_forward_max_drawdown_pct":
                         sc["metrics"]["max_drawdown_pct"], "sessions": len(daily)},
            "how": ("A proposal, never applied. max_daily_drawdown_pct is trader-risk's own key (trader/config.json): "
                    "set it yourself if you agree; it is never loosened here (at most your current value). The "
                    "portfolio drawdown pause is NOT a trader-risk key: when the mix is that far below its peak, "
                    "stop the auto-pilot (autopilot.py stop) or use the kill switch and review the strategies."),
            "why": ("1.5x the mix's 99th-percentile daily loss in the walk-forward, and 1.25x the bootstrap's "
                    "95th-percentile max drawdown. A drawdown beyond these is outside what the past shows, not proof "
                    "of a broken strategy.")}
    return {"autopilot": ap, "risk": risk}


# ---------------------------------------------------------------- files

def portfolio_dir(out, name):
    return os.path.join(out, "portfolio", name)


def write(out, doc, write_json):
    d = portfolio_dir(out, doc["name"])
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, "portfolio.json"), doc)
    if doc["proposals"].get("autopilot"):
        write_json(os.path.join(d, "proposal-autopilot.json"), doc["proposals"]["autopilot"])
    if doc["proposals"].get("risk"):
        write_json(os.path.join(d, "proposal-risk.json"), doc["proposals"]["risk"])
    sc = doc["schemes"]
    with open(os.path.join(d, "equity.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("date," + ",".join(s for s in SCHEMES if sc[s].get("daily")) + "\n")
        live = [s for s in SCHEMES if sc[s].get("daily")]
        if live:
            eqs = {s: _equity(sc[s]["daily"]) for s in live}
            days = doc.get("scored_days") or []
            for t, x in enumerate(days):
                fh.write(x + "," + ",".join("%.6f" % eqs[s][t] for s in live) + "\n")
    with open(os.path.join(d, "scorecard.md"), "w", encoding="utf-8") as fh:
        fh.write(scorecard(doc))
    return d


def listing(out):
    base = os.path.join(out, "portfolio")
    rows = []
    if os.path.isdir(base):
        for n in sorted(os.listdir(base)):
            doc = _read_json(os.path.join(base, n, "portfolio.json"))
            if isinstance(doc, dict) and doc.get("schema") == SCHEMA:
                m = (doc["schemes"].get(doc["chosen"]) or {}).get("metrics") or {}
                rows.append({"name": n, "built": doc.get("built"), "strategies": [s["name"] for s in doc["strategies"]],
                             "scheme": doc["chosen"], "overlap": doc["overlap"]["sessions"],
                             "sharpe": m.get("sharpe"), "max_drawdown_pct": m.get("max_drawdown_pct"),
                             "research": any(not s["eligible"] for s in doc["strategies"])})
    return rows


def load(out, name):
    doc = _read_json(os.path.join(portfolio_dir(out, name), "portfolio.json"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise MixError("no portfolio %s in %s: build it first (lab.py portfolio build %s)" % (
            name, os.path.join(out, "portfolio"), name))
    return doc


def view(doc, daily=False):
    """The document without the long daily series (for a UI or an assistant)."""
    v = json.loads(json.dumps(doc))
    if not daily:
        v.pop("scored_days", None)
        for s in v["schemes"].values():
            s.pop("daily", None)
            s.pop("rebalances", None)
    return v


# ---------------------------------------------------------------- the scorecard, in plain words

def _p(x, n=2):
    return "–" if x is None else ("%." + str(n) + "f%%") % x


def _n(x, n=2):
    return "–" if x is None else ("%." + str(n) + "f") % x


def scorecard(doc):
    names = [s["name"] for s in doc["strategies"]]
    ov = doc["overlap"]
    L = ["# Portfolio %s, built %s IST" % (doc["name"], doc["built"]), ""]
    research = [s["name"] for s in doc["strategies"] if not s["eligible"]]
    L.append("%d strategies%s. The overlap every one covers: %s to %s, %d sessions; the weights are tested on %d "
             "of them (after a first fit of %d)." % (
                 len(names), " (research only: %s)" % ", ".join(research) if research else "",
                 ov["from"], ov["to"], ov["sessions"], doc["scored"]["sessions"], doc["settings"]["fit"]))
    L.append("")
    L.append("## The strategies (out-of-sample and forward days only, after charges)")
    L.append("")
    L.append("| strategy | verdict | days used | volatility a year | coverage of the overlap |")
    L.append("| --- | --- | --- | --- | --- |")
    for s in doc["strategies"]:
        L.append("| %s | %s | %s | %s | %.0f%% |" % (
            s["name"], s["verdict"], "; ".join("%s %s to %s" % (g["source"], g["from"], g["to"]) for g in s["segments"]),
            _p(s["vol_pct"]), 100 * ov["coverage"].get(s["name"], 0)))
    L.append("")
    L.append("## Correlation of daily returns")
    L.append("")
    c = doc["correlation"]
    for key, label in (("pearson", "Pearson"), ("spearman", "Spearman (ranks)"), ("downside", "downside (losing "
                       "days)"), ("stress", "stress (the mix's worst %d days; a sample picked for bad days is biased, so read a "
                       "rise against the other days, not the level)" % c["stress_days"]),
                       ("weekly", "weekly sums (%d weeks)" % c["weeks"])):
        m = c.get(key)
        if not m:
            continue
        L.append("%s:" % label)
        L.append("")
        L.append("| | " + " | ".join(names) + " |")
        L.append("| --- " * (len(names) + 1) + "|")
        for i, n in enumerate(names):
            L.append("| %s | %s |" % (n, " | ".join("%.2f" % v for v in m[i])))
        L.append("")
    cl = doc["clusters"]
    if cl["duplicates"]:
        L.append("Same bet (correlation %.2f or more): %s." % (cl["threshold"], "; ".join(" + ".join(g) for g in
                                                                                         cl["duplicates"])))
    else:
        L.append("No two strategies correlate at %.2f or more on any measure." % cl["threshold"])
    L.append("")
    L.append("## Sizing schemes, tested walk-forward (weights from past sessions only)")
    L.append("")
    L.append("| scheme | weights now | cash | CAGR | volatility | Sharpe | Sortino | max drawdown | worst month | "
             "time in drawdown | diversification ratio |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sc in SCHEMES:
        e = doc["schemes"][sc]
        m = e.get("metrics")
        if not m:
            L.append("| %s | (too short to test) | | | | | | | | | |" % e["label"])
            continue
        L.append("| %s%s | %s | %.0f%% | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            e["label"], " (chosen)" if sc == doc["chosen"] else "",
            ", ".join("%s %.0f%%" % (n, 100 * w) for n, w in zip(names, e["weights"])), 100 * e["cash"],
            _p(m["cagr_pct"]), _p(m["vol_pct"]), _n(m["sharpe"]), _n(m["sortino"]), _p(m["max_drawdown_pct"]),
            "%s %s" % (m["worst_month"], _p(m["worst_month_pct"])), _p(m["time_in_drawdown_pct"], 0),
            _n(e.get("diversification_ratio"))))
    L.append("")
    L.append("Each strategy alone, on the same days:")
    L.append("")
    for s in doc["standalone"]:
        L.append("- %s: CAGR %s, volatility %s, Sharpe %s, max drawdown %s." % (
            s["name"], _p(s["cagr_pct"]), _p(s["vol_pct"]), _n(s["sharpe"]), _p(s["max_drawdown_pct"])))
    L.append("")
    ch = doc["schemes"][doc["chosen"]]
    if ch.get("metrics"):
        L.append("## The chosen scheme: %s" % ch["label"])
        L.append("")
        L.append(doc["chosen_why"] + ".")
        L.append("")
        L.append("Share of the risk: %s. Share of the return: %s. Diversification ratio %s (about %s independent "
                 "bets)." % (", ".join("%s %.0f%%" % (n, 100 * x) for n, x in zip(names, ch["risk_share"])),
                             ", ".join("%s %s" % (n, _p(100 * x)) for n, x in zip(names, ch["return_share"])),
                             _n(ch.get("diversification_ratio")), _n(ch.get("effective_bets"), 1)))
        b = ch.get("bootstrap")
        if b:
            L.append("")
            L.append("Block bootstrap (%d paths, mean block %d sessions, seed %d): final return %s to %s (5th to 95th "
                     "percentile, median %s); max drawdown %s at the median, %s at the 95th percentile; %.0f%% of "
                     "paths lost money." % (b["runs"], b["block"], b["seed"], _p(b["final_pct"]["p5"]),
                                            _p(b["final_pct"]["p95"]), _p(b["final_pct"]["p50"]),
                                            _p(b["max_drawdown_pct"]["p50"]), _p(b["max_drawdown_pct"]["p95"]),
                                            100 * b["prob_loss"]))
        for n in ch.get("notes") or []:
            L.append("")
            L.append("Note: %s." % n)
        L.append("")
    pr = doc["proposals"]
    if pr.get("autopilot"):
        ap = pr["autopilot"]
        L.append("## Proposals (never applied)")
        L.append("")
        if ap["autopilot"]["strategies"]:
            L.append("Auto-pilot buckets (proposal-autopilot.json): %s%s." % (
                ", ".join("%s %s" % (n, core.L().inr(b["capital"]) if "capital" in b else "%.0f%%" % (100 * b["weight"]))
                          for n, b in sorted(ap["autopilot"]["strategies"].items())),
                " of %s (%s)" % (core.L().inr(ap["capital_total"]), ap["capital_basis"]) if ap["capital_total"] else ""))
        else:
            L.append("Auto-pilot buckets: none to propose (no WATCH or PROMOTE lab strategy in this mix).")
        for x in ap["left_out"]:
            L.append("- left out: %s (%s)" % (x["name"], x["why"]))
        r = pr["risk"]
        L.append("")
        L.append("trader-risk (proposal-risk.json): max_daily_drawdown_pct %s (yours: %s); pause and review at a "
                 "portfolio drawdown of %s (not a trader-risk key: act on it yourself)." % (
                     _n(r["max_daily_drawdown_pct"], 1), r["current_max_daily_drawdown_pct"] or "the default 3",
                     _p(r["portfolio_drawdown_pause_pct"], 1)))
        L.append("")
        L.append(ap["how"])
        L.append("")
    if doc["notes"]:
        L.append("## Read this first")
        L.append("")
        for n in doc["notes"]:
            L.append("- " + n)
        L.append("")
    L.append("## What this does NOT show")
    L.append("")
    for n in doc["limits"]:
        L.append("- " + n)
    L.append("")
    L.append(doc["disclaimer"])
    return "\n".join(L) + "\n"
