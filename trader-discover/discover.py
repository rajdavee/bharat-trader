#!/usr/bin/env python3
"""Strategy discovery for trader-discover: search for an edge, with the overfitting controls on. Standard library only.

Most strategies a computer "finds" are noise: try enough rules on the same history and some will look
brilliant by luck alone. This tool searches anyway, but counts every try and makes each survivor beat
the luck of the whole search before it is shown to you.

    python3 discover.py run --budget 500 --universe nifty50 --timeframe 1d     # search, then decide
    python3 discover.py run --budget 200 --universe nifty50 --timeframe 5m --seed 3
    python3 discover.py resume nifty50-1d-s7-b500                               # carry on after a stop
    python3 discover.py report nifty50-1d-s7-b500                               # the plain-words report
    python3 discover.py list                                                    # your discovery runs

What a run does (docs/discovery.md has the method and the why):

  1. Declares the search up front, in plan.json: the rule families (trader-lab's safe vocabulary:
     RSI, moving averages, breakouts, opening ranges, VWAP, gaps, time windows, stops and targets in
     ATR or R, a maximum holding), a coarse grid of values, the FIXED trial budget, the seed, the
     data split and every gate. Nothing is chosen after seeing results.
  2. Splits the sessions by time: train, then validation, then an untouched holdout, with an embargo
     between them; a trade that crosses a boundary is dropped (purged). Trials never see the holdout.
  3. Runs each trial with trader-lab's own backtest (next-bar fills, Indian charges, slippage, sizing,
     day limits) and logs it to trials.jsonl: spec hash, params, train and validation numbers.
  4. Screens: enough trades and expectancy above 0R after charges, in train and then in validation.
  5. Robustness on the survivors: costs at 1.5x and 2x, the parameter neighbourhood (each one-step
     neighbour is run and counted as a trial), consistency across market regimes (trader-regime).
  6. Multiple testing with the TRUE trial count N: a bootstrap Reality Check / SPA p-value for the best
     of the whole search against doing nothing (White 2000, Hansen 2005), a Benjamini-Hochberg FDR cut
     over all N, the deflated Sharpe ratio with N (Bailey and Lopez de Prado 2014) and the probability
     of backtest overfitting over the trial matrix (CSCV, Bailey et al. 2015).
  7. Opens the holdout once per finalist (logged in holdout.jsonl; never re-opened) and hands the ones
     that hold up to trader-lab as specs, where the lab's own gates and a paper forward test decide.

Output: trader/discover/<run>/plan.json, trials.jsonl, holdout.jsonl, results.json, report.md, and
trader/lab/<finalist>/ for each finalist. Same data and seed give the same answer on any machine.

Needs trader-lab next to this folder (its backtest is the only one in the pack) and uses trader-regime
and trader-archive through it when they are there. Education and tooling, not financial advice.
"""

import argparse
import datetime
import hashlib
import importlib.util
import itertools
import json
import math
import os
import random
import sys
import time
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PLAN = "bharat-trader/discover-plan/1"
SCHEMA_TRIAL = "bharat-trader/discover-trial/1"
SCHEMA_RESULTS = "bharat-trader/discover-results/1"
OUT_DIR = os.path.join("trader", "discover")
LAB_OUT = os.path.join("trader", "lab")
ARCHIVE_DIR = os.path.join("trader", "archive")
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# NIFTY 50 as NSE Indices published it on 2026-11-01 (niftyindices.com ind_nifty50list.csv). Today's
# members, applied to the whole history: stocks that left the index are missing (survivorship bias).
NIFTY50 = ("ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE", "BAJAJ-AUTO", "BAJFINANCE",
           "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM",
           "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO",
           "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC",
           "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM", "TMPV",
           "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO")
LAB10 = ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK")
UNIVERSES = {"nifty50": NIFTY50, "lab10": LAB10}
UNIVERSE_NOTES = {
    "nifty50": "NIFTY 50 as of 2026-11-01, applied to the whole history: the archive has no dated index "
               "membership, so stocks that left the index are missing and ones that joined late are tested "
               "before they were members (survivorship bias: it flatters results).",
    "lab10": "ten liquid NIFTY 50 names picked today (survivorship bias: it flatters results).",
}

# Every threshold, fixed before the run and written into plan.json.
GATES = {
    "min_train_trades": 30,          # trades in the training window, at least
    "min_validation_trades": 15,     # ... in the validation window
    "min_holdout_trades": 15,        # ... in the untouched holdout
    "min_expectancy_r": 0.0,         # mean R a trade after charges, above (train, validation, holdout)
    "cost_stress": 1.5,              # train+validation expectancy still above the minimum at this cost multiple
    "min_neighbour_positive": 0.5,   # share of one-step parameter neighbours with expectancy above 0R
    "min_neighbours": 2,             # and at least this many neighbours to judge
    "min_regime_trades": 10,         # a regime cell needs this many trades to count
    "min_regime_positive": 0.5,      # share of measured regime cells with expectancy above 0R
    "max_spa_p": 0.05,               # Reality Check / SPA p-value of the best trial vs no edge, below
    "fdr_q": 0.10,                   # Benjamini-Hochberg false discovery rate over all N trials
    "min_dsr": 0.95,                 # deflated Sharpe ratio with the true trial count, at least
    "max_pbo": 0.5,                  # probability of backtest overfitting (CSCV over the trial matrix), below
}
SETTINGS = {
    "train": 0.50, "validation": 0.25,   # shares of the tested sessions; the holdout is the rest (about 25%)
    "embargo": {"1d": 5, "5m": 1},       # sessions skipped after train and after validation
    "bootstrap": 1000,                   # stationary bootstrap resamples (Politis and Romano 1994)
    "block": 0,                          # mean block length in sessions (0: about T^(1/3))
    "max_survivors": 40,                 # validation survivors taken to robustness, best validation Sharpe first
    "pbo_blocks": 8,                     # CSCV blocks (70 splits)
    "costs": "normal",                   # "none": no charges, no slippage (a statistics check, never for decisions)
}
SIZING = {"capital": 100000, "risk_per_trade_pct": 1, "max_trades_per_day": 3}
DISCLAIMER = "Education and tooling, not financial advice. Nothing here placed an order."


class DiscoverError(Exception):
    pass


class Stopped(Exception):
    """The time budget ran out or the caller cancelled: the run is saved and can be resumed."""


def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian digit grouping), -1234 -> '-₹1,234.00'."""
    d = Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = format(abs(d), "f").split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%s₹%s.%s" % (sign, whole, frac)


def lab_api():
    """trader-lab's lab.py from the sibling skill folder (its backtest, statistics and files)."""
    name = "bt_trader_lab"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), "trader-lab", "lab.py")
    if not os.path.isfile(path):
        raise DiscoverError("trader-discover needs trader-lab installed next to it (%s is missing)" % path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def ist_now(text=None):
    if text:
        return lab_api().ist_now(text)
    return datetime.datetime.now(IST).replace(tzinfo=None)


# ---------------------------------------------------------------- the search space
#
# A rule family builds a trader-lab spec from numeric params (ordered axes: these get neighbours and
# become the lab grid) and choices (structural options). Every value is on a coarse, pre-declared grid.

def _trend(choice):
    return [] if choice == "none" else [["close", ">", {"sma": int(choice[3:])}]]


def _b_rsi_dip(p, c):
    entry = [[{"rsi": "$rsi_n"}, "<", "$rsi_below"]] + _trend(c["trend"])
    ex = {"max_bars": "$hold"}
    if c["exit"] == "sma5":
        ex["when"] = ["close", ">", {"sma": 5}]
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"}, "exit": ex}


def _b_breakout(p, c):
    entry = [["close", "crosses_above", {"highest": "$look", "shift": 1}]] + _trend(c["trend"])
    ex = {"max_bars": "$hold"}
    if c["exit"] == "low10":
        ex["when"] = ["close", "<", {"lowest": 10, "shift": 1}]
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"}, "target": {"r": "$target_r"},
            "exit": ex}


def _b_ma_cross(p, c):
    out = {"entry": {"all": [[{"ema": "$fast"}, "crosses_above", {"sma": "$slow"}]]},
           "stop": {"atr": 14, "mult": "$stop_atr"},
           "exit": {"when": [{"ema": "$fast"}, "crosses_below", {"sma": "$slow"}], "max_bars": "$hold"}}
    if c["target"] != "none":
        out["target"] = {"r": float(c["target"][1:])}
    return out


def _b_gap_down(p, c):
    entry = [["gap_pct", "<", {"mul": ["$gap", -1]}], ["close", ">", "open"]] + _trend(c["trend"])
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"}, "exit": {"max_bars": "$hold"}}


def _b_pullback(p, c):
    entry = [["close", ">", {"sma": "$trend_n"}], ["close", "<", {"lowest": "$dip_n", "of": "close", "shift": 1}]]
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"},
            "exit": {"when": ["close", ">", {"sma": 5}], "max_bars": "$hold"}}


WINDOWS = {"open": ["09:30", "11:00"], "morning": ["09:30", "12:30"], "day": ["09:30", "14:00"],
           "late": ["11:00", "14:30"], "mid": ["10:00", "14:00"]}


def _intra_filter(choice):
    return {"none": [], "vwap": [["close", ">", "vwap"]], "prev_close": [["close", ">", "prev_close"]],
            "day_open": [["close", ">", "day_open"]]}[choice]


def _b_orb(p, c):
    stop = {"or_mid": {"price": {"or_mid": "$or_m"}}, "or_low": {"price": {"or_low": "$or_m"}},
            "atr": {"atr": 14, "mult": 1.5}}[c["stop"]]
    entry = [["close", "crosses_above", {"or_high": "$or_m"}], {"time_between": WINDOWS[c["window"]]}] + \
        _intra_filter(c["filter"])
    return {"entry": {"all": entry}, "stop": stop, "target": {"r": "$target_r"}, "exit": {"max_bars": "$hold"}}


def _b_vwap_reclaim(p, c):
    entry = [["close", "crosses_above", "vwap"], {"time_between": WINDOWS[c["window"]]}] + _intra_filter(c["filter"])
    return {"entry": {"all": entry}, "stop": {"price": {"lowest": "$low_n"}}, "target": {"r": "$target_r"},
            "exit": {"max_bars": "$hold"}}


def _b_pdh_break(p, c):
    entry = [["close", "crosses_above", "prev_high"], {"time_between": WINDOWS[c["window"]]}] + \
        _intra_filter(c["filter"])
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"}, "target": {"r": "$target_r"},
            "exit": {"max_bars": "$hold"}}


def _b_gap_fade(p, c):
    entry = [["gap_pct", "<", {"mul": ["$gap", -1]}], ["close", "crosses_above", "day_open"],
             {"time_between": WINDOWS[c["window"]]}]
    stop = {"price": "day_low"} if c["stop"] == "day_low" else {"atr": 14, "mult": 1.5}
    target = {"price": "prev_close"} if c["target"] == "prev_close" else {"r": float(c["target"][1:])}
    return {"entry": {"all": entry}, "stop": stop, "target": target, "exit": {}}


def _b_rsi_bounce(p, c):
    entry = [[{"rsi": "$rsi_n"}, "crosses_above", "$level"], {"time_between": WINDOWS["mid"]}] + \
        _intra_filter(c["filter"])
    return {"entry": {"all": entry}, "stop": {"atr": 14, "mult": "$stop_atr"}, "target": {"r": "$target_r"},
            "exit": {"max_bars": "$hold"}}


FAMILIES = {
    "1d": {
        "rsi-dip": {"axes": [("rsi_n", [2, 3, 5]), ("rsi_below", [5, 10, 15, 25]), ("stop_atr", [2, 3]),
                             ("hold", [5, 10, 20])],
                    "choices": [("trend", ["none", "sma50", "sma200"]), ("exit", ["sma5", "hold"])],
                    "build": _b_rsi_dip, "words": "buy a short-term dip (RSI under a level)"},
        "breakout": {"axes": [("look", [10, 20, 50]), ("stop_atr", [1.5, 2, 3]), ("target_r", [2, 3, 4]),
                              ("hold", [10, 20, 40])],
                     "choices": [("trend", ["none", "sma200"]), ("exit", ["low10", "hold"])],
                     "build": _b_breakout, "words": "buy a close above the prior N-day high"},
        "ma-cross": {"axes": [("fast", [5, 10, 20]), ("slow", [50, 100, 200]), ("stop_atr", [2, 3]),
                              ("hold", [20, 40])],
                     "choices": [("target", ["none", "r3"])],
                     "build": _b_ma_cross, "words": "buy when a fast EMA crosses above a slow average"},
        "gap-down": {"axes": [("gap", [1, 2, 3]), ("stop_atr", [1.5, 2, 3]), ("hold", [3, 5, 10])],
                     "choices": [("trend", ["none", "sma200"])],
                     "build": _b_gap_down, "words": "buy a gap down that closes above its open"},
        "pullback": {"axes": [("trend_n", [50, 100, 200]), ("dip_n", [3, 5, 10]), ("stop_atr", [2, 3]),
                              ("hold", [5, 10])],
                     "choices": [],
                     "build": _b_pullback, "words": "buy a new short-term low in an uptrend"},
    },
    "5m": {
        "orb": {"axes": [("or_m", [15, 30, 45, 60]), ("target_r", [1.5, 2, 3]), ("hold", [12, 24, 75])],
                "choices": [("window", ["open", "morning", "day"]), ("stop", ["or_mid", "or_low", "atr"]),
                            ("filter", ["none", "vwap"])],
                "build": _b_orb, "words": "buy a close above the opening range"},
        "vwap-reclaim": {"axes": [("low_n", [6, 12]), ("target_r", [1.5, 2, 3]), ("hold", [12, 24])],
                         "choices": [("window", ["open", "mid", "late"]), ("filter", ["none", "prev_close", "day_open"])],
                         "build": _b_vwap_reclaim, "words": "buy a close back above VWAP"},
        "pdh-break": {"axes": [("stop_atr", [1, 1.5, 2]), ("target_r", [1.5, 2, 3]), ("hold", [12, 24, 75])],
                      "choices": [("window", ["open", "morning", "day"]), ("filter", ["none", "vwap"])],
                      "build": _b_pdh_break, "words": "buy a close above yesterday's high"},
        "gap-fade": {"axes": [("gap", [0.5, 0.75, 1, 1.5])],
                     "choices": [("window", ["open", "morning"]), ("stop", ["day_low", "atr"]),
                                 ("target", ["prev_close", "r2", "r3"])],
                     "build": _b_gap_fade, "words": "buy a gap down that reclaims the day's open"},
        "rsi-bounce": {"axes": [("rsi_n", [7, 14]), ("level", [20, 25, 30, 35]), ("stop_atr", [1, 1.5, 2]),
                                ("target_r", [1.5, 2, 3]), ("hold", [12, 24])],
                       "choices": [("filter", ["none", "vwap", "day_open"])],
                       "build": _b_rsi_bounce, "words": "buy RSI crossing back up from oversold"},
    },
}
# Option structures (iron condors, spreads) join the space when trader-lab backtests options (#35).
OPTION_FAMILIES = {}


def family_points(tf, fam):
    f = FAMILIES[tf][fam]
    names = [a for a, _ in f["axes"]] + [c for c, _ in f["choices"]]
    vals = [v for _, v in f["axes"]] + [v for _, v in f["choices"]]
    na = len(f["axes"])
    for combo in itertools.product(*vals):
        yield {"family": fam, "params": dict(zip(names[:na], combo[:na])), "choices": dict(zip(names[na:], combo[na:]))}


def space(tf):
    """Every point of the declared search space for a timeframe, in a fixed order."""
    out = []
    for fam in sorted(FAMILIES[tf]):
        out.extend(family_points(tf, fam))
    return out


def trial_key(t):
    return json.dumps({"f": t["family"], "p": t["params"], "c": t["choices"]}, sort_keys=True, separators=(",", ":"))


def trial_id(t):
    return hashlib.sha256(trial_key(t).encode("utf-8")).hexdigest()[:12]


def build_doc(tf, t, universe, costs="normal", name=None, extra=None):
    """A trader-lab spec (dict) for one trial."""
    f = FAMILIES[tf][t["family"]]
    body = f["build"](t["params"], t["choices"])
    doc = {"schema": "bharat-trader/lab-spec/1", "name": name or ("d-%s-%s" % (t["family"], trial_id(t)[:8])),
           "title": "%s (%s)" % (f["words"], ", ".join("%s %s" % kv for kv in sorted(t["choices"].items())) or "base"),
           "kind": "equity", "timeframe": tf, "product": "MIS" if tf == "5m" else "CNC", "side": "long",
           "universe": list(universe), "params": dict(t["params"]), "sizing": dict(SIZING)}
    doc.update(body)
    if tf == "5m":
        doc.setdefault("exit", {})["flat_by"] = "auto"
    if costs == "none":
        doc["sizing"]["include_charges"] = False
        doc["costs"] = {"slippage_bps": 0, "slippage_ticks": 0}
    if extra:
        doc.update(extra)
    return doc


def neighbours(tf, t):
    """The one-step neighbours of a trial: each numeric axis moved one grid step, the rest unchanged."""
    out = []
    for name, vals in FAMILIES[tf][t["family"]]["axes"]:
        k = vals.index(t["params"][name])
        for j in (k - 1, k + 1):
            if 0 <= j < len(vals):
                p = dict(t["params"])
                p[name] = vals[j]
                out.append({"family": t["family"], "params": p, "choices": dict(t["choices"])})
    return out


def lab_grid(tf, t, limit=3):
    """The finalist's neighbourhood as a trader-lab grid (at most `limit` axes: 3^3 = 27 points)."""
    g = {}
    for name, vals in FAMILIES[tf][t["family"]]["axes"]:
        if len(vals) < 2 or len(g) >= limit:
            continue
        k = vals.index(t["params"][name])
        g[name] = vals[max(0, k - 1):k + 2]
    return g


def sample_trials(tf, budget, seed, search="random"):
    """The FIXED list of trials, decided before any is run: a seeded random sample of the space without
    replacement (or the first `budget` points in grid order)."""
    pts = space(tf)
    if budget > len(pts):
        raise DiscoverError("a budget of %d is more than the %d points in the %s search space" % (budget, len(pts), tf))
    if search == "grid":
        return pts[:budget]
    rng = random.Random(seed)
    return rng.sample(pts, budget)


# ---------------------------------------------------------------- the time split

def split_days(days, tf, settings):
    """{"train", "validation", "holdout", "embargo"}: consecutive session lists with embargo gaps."""
    n = len(days)
    emb = int(settings["embargo"][tf] if isinstance(settings["embargo"], dict) else settings["embargo"])
    a = int(n * settings["train"])
    b = int(n * settings["validation"])
    train = days[:a]
    val = days[a + emb:a + emb + b]
    hold = days[a + emb + b + emb:]
    if len(train) < 10 or len(val) < 5 or len(hold) < 5:
        raise DiscoverError("too few sessions to split (%d after the warm-up): train %d, validation %d, holdout %d"
                            % (n, len(train), len(val), len(hold)))
    return {"train": train, "validation": val, "holdout": hold, "embargo": emb}


# ---------------------------------------------------------------- statistics

def _mean(x):
    return sum(x) / float(len(x)) if x else 0.0


def _sd(x):
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def ci95(rs):
    if len(rs) < 2:
        return None
    m, h = _mean(rs), 1.96 * _sd(rs) / math.sqrt(len(rs))
    return [round(m - h, 4), round(m + h, 4)]


def seg_stats(lab, trades, days, capital):
    st = lab.trade_stats(trades, days, capital)
    out = {k: st[k] for k in ("trades", "days", "win_rate", "net", "charges", "expectancy_r", "profit_factor",
                              "max_drawdown_pct", "sharpe_daily")}
    out["ci95_r"] = ci95([t["r"] for t in trades])
    return out


def segment_trades(trades, days):
    """(kept, purged): trades signalled inside the window; purged = those still open after its last day."""
    if not days:
        return [], 0
    a, b = days[0], days[-1]
    inside = [t for t in trades if a <= t["signal_date"] <= b and t["reason"] != "end of data"]
    kept = [t for t in inside if t["exit_date"] <= b]
    return kept, len(inside) - len(kept)


def sparse(pnl):
    return [[i, v] for i, v in enumerate(pnl) if v]


def dense(sp, n):
    out = [0.0] * n
    for i, v in sp:
        out[i] = v
    return out


def stationary_blocks(rng, T, L):
    """One stationary-bootstrap resample as (starts, ends) into a doubled series (Politis and Romano 1994):
    blocks start anywhere, lengths are geometric with mean L, and the series wraps around."""
    starts, ends, n = [], [], 0
    q = math.log(1 - 1.0 / L) if L > 1 else None
    while n < T:
        s = rng.randrange(T)
        ln = 1 if q is None else 1 + int(math.log(1.0 - rng.random()) / q)
        ln = min(ln, T - n)
        starts.append(s)
        ends.append(s + ln)
        n += ln
    return starts, ends


def bootstrap_tests(cols, B=1000, L=None, seed=0):
    """White's Reality Check in Hansen's SPA form, plus one bootstrap p-value per column.

    cols: K series of daily returns (same T days), each a trial. H0 for the whole family: no trial beats
    zero (doing nothing). The statistic is the largest studentised mean, sqrt(T) * mean / sd; the null is
    simulated by a stationary bootstrap, re-centring each trial on its own mean unless it is clearly bad
    (Hansen's consistent SPA: t below -sqrt(2 log log T) stays un-centred). Per column, p_k is the bootstrap
    share of mean* - mean >= mean: the one-sided p-value of mean > 0, for the FDR cut.
    -> {"spa_p", "statistic", "p": [K], "T", "B", "block"}"""
    K = len(cols)
    if not K:
        return {"spa_p": 1.0, "statistic": 0.0, "p": [], "T": 0, "B": 0, "block": 0}
    T = len(cols[0])
    L = L or max(2, int(round(T ** (1.0 / 3))))
    rt = math.sqrt(T)
    means = [sum(c) / float(T) for c in cols]
    sds = [_sd(c) or 1e-12 for c in cols]
    ts = [rt * m / s for m, s in zip(means, sds)]
    thr = -math.sqrt(2 * math.log(math.log(T))) if T > 15 else -1.0
    centre = [m if t >= thr else 0.0 for m, t in zip(means, ts)]
    stat = max(0.0, max(ts))
    pref = []
    for c in cols:
        p, acc = [0.0], 0.0
        for v in c + c:
            acc += v
            p.append(acc)
        pref.append(p)
    rng = random.Random(seed)
    hit, cnt = 0, [0] * K
    for _ in range(B):
        st, en = stationary_blocks(rng, T, L)
        vmax = 0.0
        for k in range(K):
            P = pref[k]
            mb = (sum(map(P.__getitem__, en)) - sum(map(P.__getitem__, st))) / T
            z = rt * (mb - centre[k]) / sds[k]
            if z > vmax:
                vmax = z
            if mb - means[k] >= means[k]:
                cnt[k] += 1
        if vmax >= stat:
            hit += 1
    return {"spa_p": round((1 + hit) / float(B + 1), 4), "statistic": round(stat, 4),
            "p": [round((1 + c) / float(B + 1), 5) for c in cnt], "T": T, "B": B, "block": L}


def bh_threshold(pvals, m, q):
    """Benjamini-Hochberg (1995): the largest p_(k) with p_(k) <= k q / m, over m hypotheses (those not in
    pvals count as p = 1). 0.0 when nothing passes."""
    ps = sorted(pvals)
    cut = 0.0
    for k, p in enumerate(ps, 1):
        if p <= k * q / float(m):
            cut = p
    return cut


def pbo_fast(cols, blocks=8):
    """Probability of backtest overfitting by CSCV (Bailey, Borwein, Lopez de Prado and Zhu 2015) over a
    trial matrix given as K columns of T daily returns: the same method and answer as trader-lab's
    pbo_cscv, computed from per-block sums so a search of hundreds of trials stays quick."""
    K = len(cols)
    if K < 2:
        return None
    T = len(cols[0])
    if blocks % 2 or blocks < 2 or T < blocks:
        return None
    size, extra = divmod(T, blocks)
    bounds, s = [], 0
    for b in range(blocks):
        e = s + size + (1 if b < extra else 0)
        bounds.append((s, e))
        s = e
    stats = []
    for c in cols:
        stats.append([(e - s, sum(c[s:e]), sum(v * v for v in c[s:e])) for s, e in bounds])

    def sharpe(k, J):
        n = sum(stats[k][b][0] for b in J)
        sm = sum(stats[k][b][1] for b in J)
        sq = sum(stats[k][b][2] for b in J)
        if n < 2:
            return 0.0
        m = sm / n
        var = (sq - n * m * m) / (n - 1)
        return m / math.sqrt(var) if var > 1e-18 else 0.0

    logits = []
    for J in itertools.combinations(range(blocks), blocks // 2):
        out = [b for b in range(blocks) if b not in J]
        pin = [sharpe(k, J) for k in range(K)]
        best = max(range(K), key=lambda k: (pin[k], -k))
        pout = [sharpe(k, out) for k in range(K)]
        x = pout[best]
        rank = sum(1 for v in pout if v < x) + (sum(1 for v in pout if v == x) + 1) / 2.0
        w = rank / (K + 1.0)
        logits.append(math.log(w / (1 - w)))
    logits.sort()
    return {"pbo": round(sum(1 for x in logits if x <= 0) / float(len(logits)), 4), "splits": len(logits),
            "blocks": blocks, "trials": K, "median_logit": round(logits[len(logits) // 2], 4)}


def trial_sharpe_variance(srs, T):
    """The spread of Sharpe ratios that luck alone would give across the trials: their variance measured
    robustly (1.4826 x the median absolute deviation, squared), so a cluster of trials that really differ
    (a genuine edge and its many variants, or rules sunk by charges) does not stretch the luck hurdle; and
    never below the variance of one T-day Sharpe estimate under no edge, 1 / (T - 1)."""
    null = 1.0 / (T - 1) if T > 1 else 0.0
    if len(srs) < 3:
        return null
    med = sorted(srs)[len(srs) // 2]
    mad = sorted(abs(x - med) for x in srs)[len(srs) // 2]
    return max(null, (1.4826 * mad) ** 2)


def deflated_sharpe(lab, returns, trial_srs, n_trials):
    """The deflated Sharpe ratio (Bailey and Lopez de Prado 2014): the probability that the true Sharpe
    beats the best Sharpe n_trials luck-only trials would show (the expected maximum, from the trials'
    Sharpe variance), allowing for skew and fat tails. Uses trader-lab's PSR and expected-maximum helpers."""
    t = len(returns)
    sr = lab.sharpe(returns)
    skew, kurt = lab.moments(returns)
    var = trial_sharpe_variance(trial_srs, t)
    star = lab.expected_max_sharpe(max(1, int(n_trials)), var)
    return {"sharpe": round(sr, 5), "observations": t, "skew": round(skew, 4), "kurtosis": round(kurt, 4),
            "trials": int(n_trials), "trial_sharpe_variance": round(var, 8), "sharpe_hurdle": round(star, 5),
            "psr": round(lab.probabilistic_sharpe(sr, 0.0, t, skew, kurt), 4),
            "dsr": round(lab.probabilistic_sharpe(sr, star, t, skew, kurt), 4)}


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise DiscoverError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, doc):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # a line cut short by a crash: that trial runs again
    return out


# ---------------------------------------------------------------- data

def resolve_universe(text):
    """(symbols, label, survivorship note) from nifty50, lab10 or a comma list."""
    t = (text or "nifty50").strip()
    if t.lower() in UNIVERSES:
        return list(UNIVERSES[t.lower()]), t.lower(), UNIVERSE_NOTES[t.lower()]
    syms = [x.strip().upper() for x in t.split(",") if x.strip()]
    if not syms:
        raise DiscoverError("--universe is nifty50, lab10 or a comma list of NSE symbols")
    return syms, "custom", "a hand-picked list (survivorship bias: names chosen today)."


def load_market(lab, universe, tf, data_dir=None, cache_dir=None, fetch=True, now=None, archive=None):
    """({symbol: Series}, context, notes) through trader-lab's loader (archive first, else Yahoo)."""
    return lab.load_data({"universe": universe, "timeframe": tf}, data_dir, cache_dir, fetch, now, archive)


def cut_data(lab, data, last_day):
    """The same Series with no bar after last_day (a resumed run sees the data its plan was made on)."""
    out = {}
    for k, s in data.items():
        bars = [b for b in s.bars if b["date"] <= last_day]
        if bars:
            out[k] = lab.Series(s.symbol, s.tf, bars)
    return out


def cut_context(lab, ctx, last_day):
    return {k: (None if v is None else (cut_data(lab, {k: v}, last_day).get(k))) for k, v in (ctx or {}).items()}


def fingerprint(data):
    rows = sorted((k, s.n, s.date[0], s.date[-1], round(s.c[-1], 2)) for k, s in data.items() if s.n)
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- plan, trials, decision

def earlier_trials(out, tf, name):
    """Trials of your earlier discovery runs on the same timeframe: they searched the same history, so
    they count against this run's survivors too."""
    n, runs = 0, []
    if os.path.isdir(out):
        for r in sorted(os.listdir(out)):
            if r == name:
                continue
            p = read_json(os.path.join(out, r, "plan.json"))
            if p and p.get("timeframe") == tf:
                k = len([x for x in read_jsonl(os.path.join(out, r, "trials.jsonl"))])
                if k:
                    n += k
                    runs.append(r)
    return n, runs


def make_plan(lab, name, tf, universe, ulabel, unote, budget, seed, search, data, gates=None, settings=None,
              now=None, earlier=(0, []), sources=None):
    gates = dict(GATES, **(gates or {}))
    settings = dict(SETTINGS, **(settings or {}))
    trials = sample_trials(tf, budget, seed, search)
    lookback = 0
    for fam in FAMILIES[tf]:
        f = FAMILIES[tf][fam]
        top = {"family": fam, "params": {a: max(v) for a, v in f["axes"]},
               "choices": {c: v[-1] for c, v in f["choices"]}}
        for cand in [top] + [t for t in trials if t["family"] == fam][:50]:
            lookback = max(lookback, lab.validate_spec(build_doc(tf, cand, universe))["lookback"])
    days = lab.session_days(data)
    warm = lab._warmup_days({"timeframe": tf}, lookback)
    if warm >= len(days) - 30:
        raise DiscoverError("%d sessions of %s bars is too little: the warm-up alone needs %d" % (len(days), tf, warm))
    used = days[warm:]
    seg = split_days(used, tf, settings)
    return {"schema": SCHEMA_PLAN, "name": name, "created": (now or ist_now()).strftime("%Y-%m-%d %H:%M"),
            "timeframe": tf, "universe": list(universe), "universe_label": ulabel, "universe_note": unote,
            "symbols_with_data": sorted(data), "budget": budget, "seed": seed, "search": search,
            "space_size": len(space(tf)), "families": {f: FAMILIES[tf][f]["words"] for f in sorted(FAMILIES[tf])},
            "trials": [{"i": i, "family": t["family"], "params": t["params"], "choices": t["choices"], "id": trial_id(t)}
                       for i, t in enumerate(trials)],
            "gates": gates, "settings": settings, "sizing": dict(SIZING),
            "earlier_trials": earlier[0], "earlier_runs": earlier[1],
            "data": {"from": days[0], "to": days[-1], "sessions": len(days), "warmup_sessions": warm,
                     "fingerprint": fingerprint(data), "sources": sources or []},
            "split": {"train": [seg["train"][0], seg["train"][-1], len(seg["train"])],
                      "validation": [seg["validation"][0], seg["validation"][-1], len(seg["validation"])],
                      "holdout": [seg["holdout"][0], seg["holdout"][-1], len(seg["holdout"])],
                      "embargo_sessions": seg["embargo"]},
            "state": "planned"}


def plan_segments(lab, plan, data):
    days = lab.session_days(data)
    used = days[plan["data"]["warmup_sessions"]:]
    seg = split_days(used, plan["timeframe"], plan["settings"])
    if seg["train"][0] != plan["split"]["train"][0] or seg["holdout"][-1] != plan["split"]["holdout"][1]:
        raise DiscoverError("the data no longer matches the plan's split (%s to %s): start a new run"
                            % (plan["split"]["train"][0], plan["split"]["holdout"][1]))
    return seg


def evaluate(lab, plan, t, data, seg, fno, cost_mult=1.0):
    """Run one trial on train + validation (signals stop at the validation's last day: the holdout is
    never seen). -> (record without bookkeeping, kept trades by window)."""
    tf = plan["timeframe"]
    doc = build_doc(tf, t, plan["universe"], plan["settings"]["costs"])
    sp = lab.validate_spec(doc)
    bt = lab.backtest(sp, data, cost_mult, fno, start=seg["train"][0], end=seg["validation"][-1])
    cap = float(sp["cfg"]["capital"])
    rec = {"spec_hash": lab.spec_hash(doc), "purged": 0}
    kept = {}
    for part in ("train", "validation"):
        ts, purged = segment_trades(bt["trades"], seg[part])
        kept[part] = ts
        rec["purged"] += purged
        rec[part] = seg_stats(lab, ts, seg[part], cap)
        if cost_mult == 1.0:
            rec[part + "_pnl"] = sparse(lab.daily_pnl(ts, seg[part]))
    return rec, kept


def _record(t, i, role, rec, seconds):
    out = {"schema": SCHEMA_TRIAL, "i": i, "role": role, "id": trial_id(t), "family": t["family"],
           "params": t["params"], "choices": t["choices"]}
    out.update(rec)
    out["seconds"] = round(seconds, 3)
    return out


def run_trials(lab, plan, run_dir, data, fno, progress=None, deadline=None, cancel=None):
    """Run the plan's trials not yet in trials.jsonl. Raises Stopped at the deadline or on cancel."""
    seg = plan_segments(lab, plan, data)
    path = os.path.join(run_dir, "trials.jsonl")
    done = {r["i"] for r in read_jsonl(path) if r.get("role") == "search"}
    todo = [x for x in plan["trials"] if x["i"] not in done]
    n, total = len(done), len(plan["trials"])
    t0 = time.time()
    for k, x in enumerate(todo):
        if cancel is not None and cancel():
            raise Stopped("cancelled after %d of %d trials" % (n, total))
        if deadline is not None and time.time() > deadline:
            raise Stopped("the time budget ran out after %d of %d trials" % (n, total))
        t = {"family": x["family"], "params": x["params"], "choices": x["choices"]}
        a = time.time()
        rec, _ = evaluate(lab, plan, t, data, seg, fno)
        append_jsonl(path, _record(t, x["i"], "search", rec, time.time() - a))
        n += 1
        if progress and (n % 10 == 0 or n == total):
            el = time.time() - t0
            eta = el / (k + 1) * (len(todo) - k - 1)
            progress(n / float(total), "trial %d of %d (about %d s left)" % (n, total, int(eta)))
    return n


def _passes(st, need, g):
    return st["trades"] >= need and st["expectancy_r"] > g["min_expectancy_r"]


def _gate(name, needs, got, ok):
    return {"gate": name, "needs": needs, "got": got, "pass": bool(ok)}


def _combined(lab, kept, seg, cap):
    ts = kept["train"] + kept["validation"]
    return seg_stats(lab, ts, seg["train"] + seg["validation"], cap)


def finalize(lab, plan, run_dir, data, ctx, fno, lab_out=None, now=None, progress=None, cancel=None):
    """Robustness, multiple testing, the holdout and the hand-off to trader-lab. Writes results.json and
    report.md. Neighbour checks are logged as trials (role "neighbour") and counted in N."""
    now = now or ist_now()
    tf, g, st = plan["timeframe"], plan["gates"], plan["settings"]
    seg = plan_segments(lab, plan, data)
    path = os.path.join(run_dir, "trials.jsonl")
    recs = read_jsonl(path)
    by_id = {}
    for r in recs:
        by_id.setdefault(r["id"], r)
    search = [by_id[x["id"]] for x in plan["trials"] if x["id"] in by_id]
    if len(search) < len(plan["trials"]):
        raise DiscoverError("%d of %d trials are not run yet: resume the run" % (len(plan["trials"]) - len(search),
                                                                               len(plan["trials"])))
    cap = float(SIZING["capital"])
    status = {}

    def reject(r, stage, why):
        status[r["id"]] = {"status": "REJECTED", "stage": stage, "reason": why}

    def note(msg, f):
        if cancel is not None and cancel():
            raise Stopped("cancelled while deciding")
        if progress:
            progress(f, msg)

    # 1-2. the screens
    survivors = []
    for r in search:
        tr, va = r["train"], r["validation"]
        if tr["trades"] < g["min_train_trades"]:
            reject(r, "train", "%d training trades (needs %d)" % (tr["trades"], g["min_train_trades"]))
        elif tr["expectancy_r"] <= g["min_expectancy_r"]:
            reject(r, "train", "training expectancy %.3fR after charges (needs > %gR)" % (tr["expectancy_r"],
                                                                                          g["min_expectancy_r"]))
        elif va["trades"] < g["min_validation_trades"]:
            reject(r, "validation", "%d validation trades (needs %d)" % (va["trades"], g["min_validation_trades"]))
        elif va["expectancy_r"] <= g["min_expectancy_r"]:
            reject(r, "validation", "validation expectancy %.3fR after charges (needs > %gR)" % (
                va["expectancy_r"], g["min_expectancy_r"]))
        else:
            survivors.append(r)
    passed_train = sum(1 for r in search if not (status.get(r["id"]) and status[r["id"]]["stage"] == "train"))
    survivors.sort(key=lambda r: (-r["validation"]["sharpe_daily"], r["i"]))
    for r in survivors[int(st["max_survivors"]):]:
        reject(r, "validation", "not in the best %d by validation Sharpe (the robustness checks are capped)"
               % st["max_survivors"])
    survivors = survivors[:int(st["max_survivors"])]
    note("%d trials survived validation" % len(survivors), 0.1)

    # 3. robustness: neighbours (run and counted), cost stress, regimes
    regs = lab.lab_regimes(ctx) if ctx else {}
    rapi = lab.regime_api() if regs else None
    robust = {}
    for j, r in enumerate(survivors):
        t = {"family": r["family"], "params": r["params"], "choices": r["choices"]}
        nb = []
        for nt in neighbours(tf, t):
            nid = trial_id(nt)
            if nid not in by_id:
                a = time.time()
                rec, _ = evaluate(lab, plan, nt, data, seg, fno)
                nr = _record(nt, None, "neighbour", rec, time.time() - a)
                append_jsonl(path, nr)
                by_id[nid] = nr
            n = by_id[nid]
            tot = n["train"]["trades"] + n["validation"]["trades"]
            net_r = n["train"]["expectancy_r"] * n["train"]["trades"] + n["validation"]["expectancy_r"] * n["validation"]["trades"]
            nb.append({"id": nid, "params": nt["params"], "trades": tot,
                       "expectancy_r": round(net_r / tot, 4) if tot else 0.0})
        pos = sum(1 for x in nb if x["trades"] and x["expectancy_r"] > g["min_expectancy_r"])
        share = pos / float(len(nb)) if nb else 0.0
        stress = {}
        for m in sorted(set([1.5, 2.0, float(g["cost_stress"])])):
            _, kept_m = evaluate(lab, plan, t, data, seg, fno, cost_mult=m)
            c = _combined(lab, kept_m, seg, cap)
            stress["%gx" % m] = {k: c[k] for k in ("trades", "net", "expectancy_r", "profit_factor")}
        _, kept = evaluate(lab, plan, t, data, seg, fno)
        comb = _combined(lab, kept, seg, cap)
        reg = None
        if rapi is not None:
            rows = rapi.split_by_regime(kept["train"] + kept["validation"], regs, int(g["min_regime_trades"]))
            measured = [x for x in rows if x["enough"]]
            posr = [x for x in measured if x["expectancy_r"] > g["min_expectancy_r"]]
            reg = {"cells": rows, "measured": len(measured), "positive": len(posr),
                   "largest_positive": bool(measured) and measured[0]["expectancy_r"] > g["min_expectancy_r"]}
        robust[r["id"]] = {"neighbours": nb, "neighbour_positive": round(share, 4), "costs": stress,
                           "combined": comb, "regimes": reg}
        note("robustness %d of %d" % (j + 1, len(survivors)), 0.1 + 0.4 * (j + 1) / max(1, len(survivors)))

    # 4. multiple testing over every trial run (search + neighbours) and earlier runs
    recs = list(by_id.values())
    n_search, n_neigh = len(search), len(recs) - len(search)
    N = len(recs) + int(plan.get("earlier_trials") or 0)
    nv, npre = len(seg["validation"]), len(seg["train"]) + len(seg["validation"])
    active = [r for r in recs if r["train"]["trades"] + r["validation"]["trades"] > 0]
    pcols = [[v / cap for v in dense(r["train_pnl"], len(seg["train"])) + dense(r["validation_pnl"], nv)]
             for r in active]
    note("bootstrap Reality Check over %d active trials" % len(active), 0.55)
    bs = bootstrap_tests(pcols, int(st["bootstrap"]), int(st["block"]) or None, int(plan["seed"]) + 1)
    pmap = {r["id"]: p for r, p in zip(active, bs["p"])}
    cut = bh_threshold(bs["p"], N, float(g["fdr_q"]))
    note("CSCV over the trial matrix (%d trials x %d sessions)" % (len(pcols), npre), 0.7)
    blocks = int(st["pbo_blocks"]) if npre >= 4 * int(st["pbo_blocks"]) else 4
    pbo = pbo_fast(pcols, blocks)
    trial_sr = [lab.sharpe(c) for c in pcols]
    run_level = [
        _gate("Reality Check / SPA: the best of the search beats doing nothing", "p < %g" % g["max_spa_p"],
              "p = %.4f over %d active trials" % (bs["spa_p"], len(active)), bs["spa_p"] < g["max_spa_p"]),
        _gate("probability of backtest overfitting (CSCV over the trial matrix)", "< %g" % g["max_pbo"],
              "not measured" if pbo is None else "%.3f" % pbo["pbo"], pbo is not None and pbo["pbo"] < g["max_pbo"]),
    ]
    run_ok = all(x["pass"] for x in run_level)

    # 5. per survivor: the full scorecard; 6. the holdout, once
    hold_path = os.path.join(run_dir, "holdout.jsonl")
    opened = {h["id"]: h for h in read_jsonl(hold_path)}
    cards, finalists = [], []
    for r in survivors:
        rb = robust[r["id"]]
        comb = rb["combined"]
        pre = [v / cap for v in dense(r["train_pnl"], len(seg["train"])) + dense(r["validation_pnl"], nv)]
        dsr = deflated_sharpe(lab, pre, trial_sr, N) if _sd(pre) > 0 else None
        p = pmap.get(r["id"], 1.0)
        cs = rb["costs"].get("%gx" % g["cost_stress"]) or {}
        reg = rb["regimes"]
        rows = [
            _gate("training trades and expectancy after charges", ">= %d trades, > %gR" % (g["min_train_trades"],
                                                                                         g["min_expectancy_r"]),
                  "%d, %.3fR" % (r["train"]["trades"], r["train"]["expectancy_r"]), True),
            _gate("validation trades and expectancy after charges", ">= %d trades, > %gR" % (
                g["min_validation_trades"], g["min_expectancy_r"]),
                "%d, %.3fR" % (r["validation"]["trades"], r["validation"]["expectancy_r"]), True),
            _gate("survives %gx costs (train + validation)" % g["cost_stress"], "expectancy > %gR" % g["min_expectancy_r"],
                  "%.3fR" % cs.get("expectancy_r", 0.0) if cs else "not run",
                  bool(cs) and cs["trades"] > 0 and cs["expectancy_r"] > g["min_expectancy_r"]),
            _gate("parameter neighbourhood", ">= %d%% of %d+ one-step neighbours above %gR" % (
                round(g["min_neighbour_positive"] * 100), g["min_neighbours"], g["min_expectancy_r"]),
                "%d%% of %d" % (round(rb["neighbour_positive"] * 100), len(rb["neighbours"])),
                len(rb["neighbours"]) >= g["min_neighbours"] and rb["neighbour_positive"] >= g["min_neighbour_positive"]),
            _gate("regime consistency (NIFTY trend x volatility, trader-regime)",
                  ">= %d%% of regimes with %d+ trades above %gR, the biggest one too" % (
                      round(g["min_regime_positive"] * 100), g["min_regime_trades"], g["min_expectancy_r"]),
                  "not measured (needs NIFTY bars and trader-regime)" if reg is None else
                  "%d of %d measured regimes" % (reg["positive"], reg["measured"]),
                  reg is not None and reg["measured"] > 0 and reg["largest_positive"] and
                  reg["positive"] >= g["min_regime_positive"] * reg["measured"]),
            _gate("deflated Sharpe ratio with the true trial count", ">= %g (N = %d)" % (g["min_dsr"], N),
                  "not measured" if not dsr else "%.4f" % dsr["dsr"], bool(dsr) and dsr["dsr"] >= g["min_dsr"]),
            _gate("Benjamini-Hochberg FDR cut over all %d trials" % N, "p <= %.5f (q = %g)" % (cut, g["fdr_q"]),
                  "p = %.5f" % p, cut > 0 and p <= cut),
        ] + run_level
        failed = [x for x in rows if not x["pass"]]
        card = {"id": r["id"], "i": r["i"], "family": r["family"], "params": r["params"], "choices": r["choices"],
                "spec_hash": r["spec_hash"], "train": r["train"], "validation": r["validation"], "combined": comb,
                "costs": rb["costs"], "neighbours": rb["neighbours"], "regimes": reg, "dsr": dsr, "fdr_p": p,
                "gates": rows, "holdout": None}
        if failed:
            reject(r, "robustness and multiple testing", "; ".join(x["gate"] for x in failed))
            cards.append(card)
            continue
        t = {"family": r["family"], "params": r["params"], "choices": r["choices"]}
        h = opened.get(r["id"])
        if h is None:
            h = open_holdout(lab, plan, t, data, seg, fno, now)
            append_jsonl(hold_path, h)
            opened[r["id"]] = h
        card["holdout"] = h
        hs = h["stats"]
        hrow = _gate("untouched holdout (opened once, %s)" % h["opened_at"], ">= %d trades, > %gR after charges" % (
            g["min_holdout_trades"], g["min_expectancy_r"]), "%d, %.3fR" % (hs["trades"], hs["expectancy_r"]),
            _passes(hs, g["min_holdout_trades"], g))
        card["gates"].append(hrow)
        if not hrow["pass"]:
            reject(r, "holdout", "holdout: %d trades, %.3fR" % (hs["trades"], hs["expectancy_r"]))
        else:
            status[r["id"]] = {"status": "WATCH", "stage": "finalist",
                               "reason": "passed every gate; paper forward test it in trader-lab before any PROMOTE"}
            finalists.append(card)
        cards.append(card)
    note("%d finalists" % len(finalists), 0.85)

    # 7. the hand-off: each finalist becomes a trader-lab spec; the lab's own gates and forward test decide
    handed = []
    if finalists and lab_out and plan["settings"]["costs"] == "normal":
        for card in finalists:
            handed.append(hand_to_lab(lab, plan, card, data, ctx, fno, lab_out, now, N))
            card["lab"] = handed[-1]
    elif finalists:
        for card in finalists:
            card["lab"] = {"handed": False, "why": "statistics-only run (costs none): nothing goes to the lab"
                           if plan["settings"]["costs"] != "normal" else "no lab folder given"}
    trial_rows = []
    for r in recs:
        s = status.get(r["id"]) or ({"status": "REJECTED", "stage": "neighbour check",
                                     "reason": "a neighbour of a survivor: run for stability, counted in N, "
                                               "not a candidate"} if r["role"] == "neighbour" else
                                    {"status": "REJECTED", "stage": "?", "reason": "?"})
        trial_rows.append({"i": r["i"], "id": r["id"], "role": r["role"], "family": r["family"], "params": r["params"],
                           "choices": r["choices"], "spec_hash": r["spec_hash"],
                           "train_trades": r["train"]["trades"], "train_r": r["train"]["expectancy_r"],
                           "validation_trades": r["validation"]["trades"], "validation_r": r["validation"]["expectancy_r"],
                           "status": s["status"], "stage": s["stage"], "reason": s["reason"]})
    stages = {}
    for x in trial_rows:
        if x["role"] == "search" and x["status"] == "REJECTED":
            stages[x["stage"]] = stages.get(x["stage"], 0) + 1
    passed_mt = sum(1 for c in cards if c["holdout"] is not None)
    res = {"schema": SCHEMA_RESULTS, "name": plan["name"], "timeframe": tf, "decided_at": now.strftime("%Y-%m-%d %H:%M"),
           "universe": plan["universe_label"], "symbols": plan["symbols_with_data"], "universe_note": plan["universe_note"],
           "data": plan["data"], "split": plan["split"], "budget": plan["budget"], "seed": plan["seed"],
           "search": plan["search"], "space_size": plan["space_size"], "costs": plan["settings"]["costs"],
           "counts": {"search_trials": n_search, "neighbour_trials": n_neigh, "earlier_trials": plan.get("earlier_trials", 0),
                      "counted": N, "passed_train": passed_train, "survived_validation": len(survivors),
                      "passed_robustness_and_multiple_testing": passed_mt, "finalists": len(finalists),
                      "rejected_by_stage": stages},
           "run_level": run_level, "spa": {k: bs[k] for k in ("spa_p", "statistic", "T", "B", "block")},
           "pbo": pbo, "fdr": {"q": g["fdr_q"], "m": N, "threshold": cut},
           "gates": g, "candidates": cards, "finalists": [c["id"] for c in finalists], "trials": trial_rows}
    res["report"] = report_text(res)
    write_json(os.path.join(run_dir, "results.json"), res)
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(res["report"])
    plan["state"] = "decided"
    write_json(os.path.join(run_dir, "plan.json"), plan)
    note("done", 1.0)
    return res


def open_holdout(lab, plan, t, data, seg, fno, now):
    """The holdout, for one finalist, once: the record is appended to holdout.jsonl and reused after."""
    doc = build_doc(plan["timeframe"], t, plan["universe"], plan["settings"]["costs"])
    sp = lab.validate_spec(doc)
    out = {}
    for m in (1.0, 1.5):
        bt = lab.backtest(sp, data, m, fno, start=seg["holdout"][0], end=seg["holdout"][-1])
        ts = [x for x in bt["trades"] if x["reason"] != "end of data" and x["signal_date"] >= seg["holdout"][0]]
        out[m] = (ts, seg_stats(lab, ts, seg["holdout"], float(sp["cfg"]["capital"])))
    return {"id": trial_id(t), "spec_hash": lab.spec_hash(doc), "opened_at": now.strftime("%Y-%m-%d %H:%M"),
            "window": [seg["holdout"][0], seg["holdout"][-1]], "stats": out[1.0][1],
            "stats_1_5x": {k: out[1.5][1][k] for k in ("trades", "net", "expectancy_r", "profit_factor")}}


def hand_to_lab(lab, plan, card, data, ctx, fno, lab_out, now, N):
    """Write the finalist as a trader-lab spec and let the lab run it: walk-forward, its own gates, its own
    deflated Sharpe charged for all N trials. The lab's verdict (WATCH at best) is the status."""
    tf = plan["timeframe"]
    t = {"family": card["family"], "params": card["params"], "choices": card["choices"]}
    doc = build_doc(tf, t, plan["universe"], "normal", extra={
        "grid": lab_grid(tf, t), "trials_tried": max(0, N - 1), "seed": int(plan["seed"]),
        "description": "Found by trader-discover run %s (trial %s, spec %s) among %d counted trials; passed train, "
                       "validation, robustness, multiple testing and the untouched holdout %s to %s. A WATCH needs a "
                       "paper forward test before any PROMOTE." % (plan["name"], card["id"], card["spec_hash"], N,
                                                                     plan["split"]["holdout"][0],
                                                                     plan["split"]["holdout"][1]),
        "notes": "discovered: %s" % plan["name"]})
    doc["universe"] = [x for x in doc["universe"] if x in data] or doc["universe"]
    try:
        res = lab.run_and_decide(doc, data, ctx, lab_out, fno, now)
        res["notes"] = ["from trader-discover run %s" % plan["name"]]
        stt = lab.save_run(lab_out, doc, res, now)
    except (lab.LabError, lab.RulesError) as e:
        return {"handed": False, "name": doc["name"], "why": str(e)}
    return {"handed": True, "name": doc["name"], "folder": os.path.join(lab_out, doc["name"]),
            "verdict": stt.get("verdict"), "why": stt.get("why"), "spec_hash": res["spec_hash"]}


# ---------------------------------------------------------------- words

def _r(x):
    return "%+.3fR" % x if x is not None else "-"


def _describe(c):
    bits = ["%s %s" % kv for kv in sorted(c["params"].items())] + ["%s %s" % kv for kv in sorted(c["choices"].items())]
    return "%s (%s)" % (c["family"], ", ".join(bits))


def report_text(res):
    c, sp, d = res["counts"], res["split"], res["data"]
    L = ["# Discovery run %s" % res["name"], ""]
    L.append("%s bars, %d stocks (%s), %s to %s. Decided %s IST." % (
        "Daily" if res["timeframe"] == "1d" else "5-minute", len(res["symbols"]), res["universe"], d["from"], d["to"],
        res["decided_at"]))
    if res["costs"] != "normal":
        L.append("")
        L.append("**Statistics-only run: no charges, no slippage. Nothing here may be traded or handed to the lab.**")
    L += ["", "## The result in plain words", ""]
    L.append("%d trials were declared up front (a seeded %s sample of %d rule combinations), and %d more were run as "
             "neighbour checks%s. All %d count. %d passed the training screen, %d survived validation, %d passed "
             "robustness and the multiple-testing controls, and **%d survived the untouched holdout**." % (
                 c["search_trials"], res["search"], res["space_size"], c["neighbour_trials"],
                 (", plus %d from your earlier runs on this history" % c["earlier_trials"]) if c["earlier_trials"] else "",
                 c["counted"], c["passed_train"], c["survived_validation"], c["passed_robustness_and_multiple_testing"],
                 c["finalists"]))
    L.append("")
    if c["finalists"] == 0:
        L.append("What that means: nothing in this search showed an edge that beats the luck of trying %d rules on "
                 "the same history, after charges. That is the usual honest answer, not a failure: most rules that "
                 "look good on one stretch of history are noise, and this run kept you from trading them." % c["counted"])
    else:
        L.append("What that means: %d rule set%s beat the luck of %d tries on these tests and held up on data the "
                 "search never saw. That is evidence, not proof. Each is now a trader-lab spec; it still needs a paper "
                 "forward test before any PROMOTE, and most such finds fade." % (
                     c["finalists"], "" if c["finalists"] == 1 else "s", c["counted"]))
    L += ["", "## How the history was split", ""]
    L.append("| window | sessions | dates |")
    L.append("| --- | --- | --- |")
    for k in ("train", "validation", "holdout"):
        L.append("| %s | %d | %s to %s |" % (k, sp[k][2], sp[k][0], sp[k][1]))
    L.append("")
    L.append("%d warm-up sessions first; %d sessions of embargo after train and after validation; trades still open "
             "at a window's end are dropped (purged). The trials never saw the holdout: it is opened once per finalist "
             "and logged in holdout.jsonl." % (d["warmup_sessions"], sp["embargo_sessions"]))
    L += ["", "## The whole search", ""]
    for g in res["run_level"]:
        L.append("- %s %s: %s (needs %s)" % ("PASS" if g["pass"] else "FAIL", g["gate"], g["got"], g["needs"]))
    L.append("- Benjamini-Hochberg cut at q = %g over %d trials: p must be at most %.5f%s" % (
        res["fdr"]["q"], res["fdr"]["m"], res["fdr"]["threshold"], "" if res["fdr"]["threshold"] else " (nothing passes)"))
    if c["rejected_by_stage"]:
        L.append("- Rejected by stage: %s" % ", ".join("%s %d" % kv for kv in sorted(c["rejected_by_stage"].items())))
    if res["candidates"]:
        L += ["", "## Validation survivors", ""]
        for card in res["candidates"]:
            st = "WATCH (finalist)" if card["id"] in res["finalists"] else "REJECTED"
            L.append("### %s: %s" % (st, _describe(card)))
            L.append("")
            L.append("Train %d trades %s, validation %d trades %s, holdout %s. Spec %s." % (
                card["train"]["trades"], _r(card["train"]["expectancy_r"]), card["validation"]["trades"],
                _r(card["validation"]["expectancy_r"]),
                "not opened" if not card["holdout"] else "%d trades %s" % (card["holdout"]["stats"]["trades"],
                                                                          _r(card["holdout"]["stats"]["expectancy_r"])),
                card["spec_hash"]))
            L.append("")
            for g in card["gates"]:
                L.append("- %s %s: %s (needs %s)" % ("PASS" if g["pass"] else "FAIL", g["gate"], g["got"], g["needs"]))
            if card.get("lab"):
                lb = card["lab"]
                L.append("- trader-lab: %s" % ("%s, verdict %s: %s" % (lb["name"], lb.get("verdict"), lb.get("why"))
                                              if lb.get("handed") else "not handed over (%s)" % lb.get("why")))
            L.append("")
    L += ["## What this does NOT show", ""]
    for x in limits(res):
        L.append("- " + x)
    L += ["", DISCLAIMER, ""]
    return "\n".join(L)


def limits(res):
    out = [
        "It does not predict the future. Every window, the holdout included, is the same past; a paper forward "
        "test in trader-lab is the next real check.",
        "The universe: %s" % res["universe_note"],
        "The multiple-testing controls count only the trials logged here (and your earlier runs in this folder). "
        "Ideas you tried elsewhere on the same history are not counted, and they make every survivor less likely "
        "to be real.",
        "The bootstrap assumes the past's day-to-day dependence is captured by blocks of about %d sessions; a "
        "regime the history never had is not in it." % (res["spa"]["block"] or 1),
        "Fills and charges are trader-lab's models (next-bar open plus slippage, a typical discount broker's "
        "charges). Income tax is not included.",
        "Market regimes are inferred from NIFTY and India VIX as of the close before each signal; market-wide "
        "regime filters are measured here, not traded (the rule vocabulary filters on the stock's own trend).",
    ]
    if res["data"]["sessions"] < 250:
        out.insert(1, "%d sessions is a short history: one or two market moods, and few trades per window."
                   % res["data"]["sessions"])
    return out


# ---------------------------------------------------------------- the commands, as functions (the CLI, MCP and UI use these)

def run_dir(out, name):
    return os.path.join(out, name)


def default_name(ulabel, tf, seed, budget):
    return "%s-%s-s%d-b%d" % (ulabel, tf, seed, budget)


def start(lab, name, tf, universe_text, budget, seed, search="random", out=OUT_DIR, lab_out=LAB_OUT, data=None,
          ctx=None, fno=None, now=None, progress=None, max_seconds=None, cancel=None, gates=None, settings=None,
          sources=None):
    """Plan and run a discovery on loaded data. -> (plan, results or None when stopped early, message)."""
    syms, ulabel, unote = resolve_universe(universe_text)
    if tf not in FAMILIES:
        raise DiscoverError("--timeframe is 1d or 5m")
    if not 10 <= int(budget) <= 5000:
        raise DiscoverError("--budget is 10 to 5000 trials")
    data = {k: v for k, v in (data or {}).items() if k in syms}
    if not data:
        raise DiscoverError("no bars for any symbol of the universe")
    name = name or default_name(ulabel, tf, seed, budget)
    d = run_dir(out, name)
    if os.path.exists(os.path.join(d, "plan.json")):
        raise DiscoverError("run %s exists: resume it (discover.py resume %s) or pick another --name" % (name, name))
    plan = make_plan(lab, name, tf, [x for x in syms if x in data], ulabel, unote, int(budget), int(seed), search,
                     data, gates, settings, now, earlier_trials(out, tf, name), sources)
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, "plan.json"), plan)
    return _carry_on(lab, plan, d, data, ctx, fno, lab_out, now, progress, max_seconds, cancel)


def resume(lab, name, out=OUT_DIR, lab_out=LAB_OUT, data=None, ctx=None, fno=None, now=None, progress=None,
           max_seconds=None, cancel=None):
    d = run_dir(out, name)
    plan = read_json(os.path.join(d, "plan.json"))
    if not plan:
        raise DiscoverError("no discovery run %s in %s" % (name, out))
    last = plan["data"]["to"]
    data = cut_data(lab, {k: v for k, v in (data or {}).items() if k in plan["universe"]}, last)
    if fingerprint(data) != plan["data"]["fingerprint"]:
        raise DiscoverError("the bars for %s changed since the plan (re-adjusted or re-fetched): start a new run, "
                            "an altered history would mix two experiments" % name)
    ctx = cut_context(lab, ctx, last)
    if plan.get("state") == "decided":
        res = read_json(os.path.join(d, "results.json"))
        return plan, res, "already decided"
    return _carry_on(lab, plan, d, data, ctx, fno, lab_out, now, progress, max_seconds, cancel)


def _carry_on(lab, plan, d, data, ctx, fno, lab_out, now, progress, max_seconds, cancel):
    deadline = time.time() + float(max_seconds) if max_seconds else None
    plan["state"] = "running"
    write_json(os.path.join(d, "plan.json"), plan)
    try:
        def prog(f, msg):
            if progress:
                progress(0.9 * f, msg)
        run_trials(lab, plan, d, data, fno, prog, deadline, cancel)

        def prog2(f, msg):
            if progress:
                progress(0.9 + 0.1 * f, msg)
        res = finalize(lab, plan, d, data, ctx, fno, lab_out, now, prog2, cancel)
    except Stopped as e:
        plan["state"] = "paused"
        plan["paused_why"] = str(e)
        write_json(os.path.join(d, "plan.json"), plan)
        return plan, None, "%s; resume with: discover.py resume %s" % (e, plan["name"])
    return plan, res, "decided"


def run_summary(out, name):
    """One run for a list, the MCP server or the UI: state, the funnel, finalists and their lab verdicts."""
    d = run_dir(out, name)
    plan = read_json(os.path.join(d, "plan.json"))
    if not plan:
        return None
    res = read_json(os.path.join(d, "results.json")) if plan.get("state") == "decided" else None
    done = len([r for r in read_jsonl(os.path.join(d, "trials.jsonl")) if r.get("role") == "search"])
    row = {"name": name, "state": plan.get("state"), "timeframe": plan["timeframe"], "universe": plan["universe_label"],
           "symbols": len(plan["universe"]), "budget": plan["budget"], "seed": plan["seed"], "trials_run": done,
           "created": plan.get("created"), "data": {k: plan["data"][k] for k in ("from", "to", "sessions")},
           "split": plan["split"], "costs": plan["settings"]["costs"]}
    if res:
        row.update({"decided_at": res["decided_at"], "counts": res["counts"], "spa_p": res["spa"]["spa_p"],
                    "pbo": (res.get("pbo") or {}).get("pbo"),
                    "finalists": [{"id": c["id"], "family": c["family"], "params": c["params"], "choices": c["choices"],
                                   "holdout": (c.get("holdout") or {}).get("stats"),
                                   "lab": c.get("lab")} for c in res["candidates"] if c["id"] in res["finalists"]],
                    "headline": "%d trials, %d survived validation, %d survived holdout + multiple testing" % (
                        res["counts"]["counted"], res["counts"]["survived_validation"], res["counts"]["finalists"])})
    elif plan.get("paused_why"):
        row["paused_why"] = plan["paused_why"]
    return row


def list_runs(out):
    rows = []
    if os.path.isdir(out):
        for n in sorted(os.listdir(out)):
            r = run_summary(out, n)
            if r:
                rows.append(r)
    return rows


# ---------------------------------------------------------------- the command line

def _progress(f, msg):
    sys.stderr.write("  %3d%%  %s\n" % (int(round(f * 100)), msg))
    sys.stderr.flush()


def polite():
    """Lower this process's CPU priority where the OS allows it: discovery is a background job."""
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass


def _load(lab, a, universe, tf, now):
    data, ctx, notes = load_market(lab, universe, tf, a.data, os.path.join(a.lab_out, "data"),
                                   not a.offline and not a.data, now, None if a.no_archive else a.archive)
    fno, fnote = lab.load_fno_stocks({}, os.path.join("trader", "fno_stocks.json"),
                                     fetch=not a.offline and not a.data)
    return data, ctx, fno, notes + [fnote]


def cmd_run(a):
    lab = lab_api()
    polite()
    now = ist_now(a.now)
    syms, ulabel, _ = resolve_universe(a.universe)
    data, ctx, fno, notes = _load(lab, a, syms, a.timeframe, now)
    settings = {"costs": a.costs}
    if a.bootstrap:
        settings["bootstrap"] = a.bootstrap
    plan, res, msg = start(lab, a.name, a.timeframe, a.universe, a.budget, a.seed, a.search, a.out, a.lab_out, data,
                           ctx, fno, now, None if a.json else _progress, a.max_seconds, settings=settings,
                           sources=notes)
    return _show(plan, res, msg, a, notes)


def cmd_resume(a):
    lab = lab_api()
    polite()
    plan = read_json(os.path.join(run_dir(a.out, a.run), "plan.json"))
    if not plan:
        raise DiscoverError("no discovery run %s in %s (discover.py list shows them)" % (a.run, a.out))
    now = ist_now(a.now)
    data, ctx, fno, notes = _load(lab, a, plan["universe"], plan["timeframe"], now)
    plan, res, msg = resume(lab, a.run, a.out, a.lab_out, data, ctx, fno, now, None if a.json else _progress,
                            a.max_seconds)
    return _show(plan, res, msg, a, notes)


def _show(plan, res, msg, a, notes):
    if a.json:
        print(json.dumps({"state": plan.get("state"), "message": msg, "summary": run_summary(a.out, plan["name"])},
                         indent=1, ensure_ascii=False))
        return 0
    if res:
        print(res["report"])
        print("Wrote %s/{plan.json, trials.jsonl, holdout.jsonl, results.json, report.md}" % run_dir(a.out, plan["name"]))
    else:
        print("Run %s: %s." % (plan["name"], msg))
    for n in notes:
        if n:
            print("Note: %s" % n)
    return 0


def cmd_report(a):
    res = read_json(os.path.join(run_dir(a.out, a.run), "results.json"))
    if not res:
        row = run_summary(a.out, a.run)
        if not row:
            raise DiscoverError("no discovery run %s in %s" % (a.run, a.out))
        print("Run %s is %s: %d of %d trials run. Resume it: discover.py resume %s" % (
            a.run, row["state"], row["trials_run"], row["budget"], a.run))
        return 0
    if a.json:
        slim = dict(res)
        slim.pop("report", None)
        if not a.trials:
            slim.pop("trials", None)
        print(json.dumps(slim, indent=1, ensure_ascii=False))
    else:
        print(res["report"])
    return 0


def cmd_list(a):
    rows = list_runs(a.out)
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        print("No discovery runs in %s yet. Try: discover.py run --budget 200 --universe nifty50 --timeframe 1d" % a.out)
    for r in rows:
        print("%-28s %-8s %s %s, %s" % (r["name"], r["state"], r["timeframe"], r["universe"],
                                        r.get("headline") or "%d of %d trials run" % (r["trials_run"], r["budget"])))
    print("")
    print(DISCLAIMER)
    return 0


def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="discover.py", description="Strategy discovery with strict overfitting controls.")
    ap.add_argument("--out", default=OUT_DIR, help="the discovery folder (default trader/discover)")
    ap.add_argument("--lab-out", default=LAB_OUT, help="trader-lab's folder for the finalists (default trader/lab)")
    sub = ap.add_subparsers(dest="cmd")

    def data_flags(p):
        p.add_argument("--data", help="offline: a folder of SYMBOL-5m.csv(.gz) / SYMBOL-1d.csv(.gz) bars")
        p.add_argument("--offline", action="store_true", help="no network: the archive and cached bars only")
        p.add_argument("--archive", default=ARCHIVE_DIR, help="a trader-archive folder to read first (default trader/archive)")
        p.add_argument("--no-archive", action="store_true", help="ignore the local archive")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-30 16:00\"")
        p.add_argument("--max-seconds", type=float, default=3600, help="time budget; the run pauses and can be resumed")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="plan and run a discovery")
    p.add_argument("--budget", type=int, default=200, help="the fixed trial budget, declared up front (default 200)")
    p.add_argument("--universe", default="nifty50", help="nifty50, lab10 or a comma list of NSE symbols")
    p.add_argument("--timeframe", choices=sorted(FAMILIES), default="1d")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--search", choices=("random", "grid"), default="random")
    p.add_argument("--name", help="the run's folder name (default UNIVERSE-TF-sSEED-bBUDGET)")
    p.add_argument("--costs", choices=("normal", "none"), default="normal",
                   help="none: no charges or slippage, a statistics check only (finalists never reach the lab)")
    p.add_argument("--bootstrap", type=int, help="bootstrap resamples (default 1000)")
    data_flags(p)
    p = sub.add_parser("resume", help="carry on a paused run on the same data")
    p.add_argument("run")
    data_flags(p)
    p = sub.add_parser("report", help="the plain-words report of a run")
    p.add_argument("run")
    p.add_argument("--json", action="store_true")
    p.add_argument("--trials", action="store_true", help="with --json: every trial's status and reason")
    p = sub.add_parser("list", help="your discovery runs")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"run": cmd_run, "resume": cmd_resume, "report": cmd_report, "list": cmd_list}[a.cmd](a)
    except DiscoverError as e:
        print("discover: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:  # the lab's own errors (bad data, rules) read the same way
        lab = sys.modules.get("bt_trader_lab")
        if lab is not None and isinstance(e, (lab.LabError, lab.RulesError)):
            print("discover: %s" % e, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
