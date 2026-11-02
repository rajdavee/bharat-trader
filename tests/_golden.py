"""Digests of engine results on the fixtures, for the memory work (#88): the memory-bounded loaders must give
the very same numbers as the loaders they replaced. tests/fixtures/trader-lab/memory-golden.json holds these
digests as computed by the code before the change; tests/test_lab_memory.py recomputes them with the code now.
The baseline is main before the change with one edit applied to both sides: the options package sums floats
with math.fsum (correctly rounded on every Python), because the built-in sum() of floats became compensated in
Python 3.12 and a premium on a rounding boundary came out 3313.76 on 3.8-3.11 and 3313.77 on 3.12+.

Nothing is loaded at import: golden_digests(lab) takes the lab module it should exercise, so the same cases
can be run against an older checkout to write the golden file.
"""

import copy
import hashlib
import json
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
LABFIX = os.path.join(FIX, "trader-lab")
EDGE = os.path.join(LABFIX, "edge")
OPTARC = os.path.join(LABFIX, "archive")
HUNT = os.path.join(LABFIX, "hunt")
A0, B0 = "2026-09-30", "2026-10-30"


def canon(x):
    """A stable text form: floats rounded to 9 places, keys sorted, sets sorted."""
    def fix(v):
        if isinstance(v, float):
            return round(v, 9)
        if isinstance(v, dict):
            return {str(k): fix(w) for k, w in v.items()}
        if isinstance(v, (list, tuple)):
            return [fix(w) for w in v]
        if isinstance(v, (set, frozenset)):
            return sorted(fix(w) for w in v)
        return v
    return json.dumps(fix(x), sort_keys=True, ensure_ascii=False, default=str)


def digest(x):
    return hashlib.sha256(canon(x).encode("utf-8")).hexdigest()[:20]


def run_digest(r):
    keep = {k: r.get(k) for k in ("days", "ret", "bench", "equity", "exposure", "trades", "traded", "charges",
                                  "capital")}
    ex = dict(r.get("extra") or {})
    keep["extra"] = ex
    return digest(keep)


def _hyp(root, prefix):
    lib = os.path.join(root, "trader-lab", "library", "edge-hunt")
    name = [f for f in os.listdir(lib) if f.startswith(prefix)][0]
    with open(os.path.join(lib, name), encoding="utf-8") as fh:
        return json.load(fh)


def option_cases(lab, root):
    """The option engines on the real-data fixture archive (tests/fixtures/trader-lab/edge)."""
    O = lab.options_api()
    EN = O.engines_api()
    A = lab.archive_api()
    R = lab.regime_api()
    out = {}

    def data(**kw):
        d = {"root": EDGE, "archive": A, "regime": R}
        d.update(kw)
        return d
    h5 = _hyp(root, "eh-05")
    d5 = data()
    for k, p in enumerate(EN.grid(h5)):
        out["eh05-%02d" % k] = run_digest(EN.run(h5, p, A0, B0, d5))
    out["eh05-cost1.5"] = run_digest(EN.run(h5, EN.grid(h5)[0], A0, B0, d5, cost_mult=1.5))
    h8 = _hyp(root, "eh-08")
    d8 = data(symbols=["HDFCBANK", "ICICIBANK"], min_history=2)
    for k, p in enumerate(EN.grid(h8)):
        out["eh08-%02d" % k] = run_digest(EN.run(h8, p, A0, B0, d8))
    h9 = _hyp(root, "eh-09")
    d9 = data(symbols=["HDFCBANK", "ICICIBANK"], min_history=2)
    for k, p in enumerate(EN.grid(h9)):
        out["eh09-%02d" % k] = run_digest(EN.run(h9, p, A0, B0, d9))
    h14 = _hyp(root, "eh-14")
    for k, p in enumerate(EN.grid(h14)):
        out["eh14-%02d" % k] = run_digest(EN.run(h14, p, "2026-10-29", "2026-10-29", data()))
    # the lab's own options backtests on the NIFTY board fixture
    chains = O.load_chains("NIFTY", OPTARC, A)
    out["chains-iv30"] = digest(chains.iv_series(30))
    out["chains-boards"] = digest({d: {"spot": chains.boards[d]["spot"], "futures": chains.boards[d]["futures"],
                                       "expiries": chains.boards[d]["expiries"],
                                       "monthly": sorted(chains.boards[d]["monthly"]),
                                       "opts": sorted([list(k) + list(v) for k, v in chains.boards[d]["opts"].items()],
                                                      key=str)} for d in chains.days})
    lib = os.path.join(root, "trader-lab", "library")
    for name in ("nifty-weekly-strangle-16d", "nifty-expiry-iron-fly", "nifty-weekly-condor-16d"):
        with open(os.path.join(lib, name + ".json"), encoding="utf-8") as fh:
            spec = O.validate(json.load(fh))
        bt = O.backtest(spec, O.load_chains("NIFTY", OPTARC, A))
        out["lib-" + name] = digest({k: bt.get(k) for k in ("trades", "skipped", "pending", "mtm", "open")})
    return out


def v2_cases(lab, root):
    """The v2 engines (futures, portfolio, events, pairs) through their Jobs on the folder fixtures."""
    E = lab.engines_api()
    M, J = E.market, E.jobs
    mk = M.FolderMarket(HUNT, lab.archive_api(), bars_dir=LABFIX)
    with open(os.path.join(HUNT, "edge-hunt", "eh-91-fixture-momentum.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    cases = [
        ("momentum-xs", {"lookback_months": 3, "skip_months": 1, "top_n": 3, "rebalance": "monthly",
                         "volatility_adjusted": True}),
        ("low-volatility", {"volatility_window_days": 63, "top_n": 3, "rebalance": "quarterly"}),
        ("reversal", {"lookback_sessions": 5, "bottom_n": 2, "hold_sessions": 5, "instrument": "futures_long_short"}),
        ("trend-ts", {"signal": "close_above_200d_average", "mode": "long_short", "underlying": "NIFTY"}),
        ("calendar", {"event": "pre_holiday", "entry_sessions_before": 1, "underlying": "NIFTY"}),
        ("overnight", {"condition": "after_down_session", "underlying": "NIFTY"}),
        ("pead", {"top_fraction": 0.5, "hold_sessions": 20, "entry_lag_sessions": 1, "min_past_events": 2}),
        ("pairs", {"formation_sessions": 126, "entry_z": 2.0, "exit_z": 0.0, "stop_z": 4.0, "max_hold_sessions": 20}),
    ]
    out = {}
    for fam, prm in cases:
        doc = copy.deepcopy(base)
        doc["family"] = fam
        doc["parameters"] = {k: [v] for k, v in prm.items()}
        doc["instrument"]["underlying"] = "NIFTY"
        doc["universe"]["name"] = "fno-stocks" if fam == "pairs" else "nifty50-pit"
        doc["span"]["train"] = {"from": "2025-01-01", "to": "2025-09-30"}
        job = J.job_for(doc, mk)
        job.load("2026-05-29")
        out["v2-" + fam] = run_digest(job.run(prm, "2025-10-15", "2026-05-29"))
        out["v2-" + fam + "-train"] = run_digest(job.run(prm, "2025-01-01", "2025-09-30", 1.5))
    # the continuous futures series read straight from real bhavcopies
    s = M.ArchiveMarket(OPTARC, lab.archive_api()).futures("NIFTY")
    out["continuous-NIFTY"] = digest({"days": s.days, "c": s.c, "ret_cc": s.ret_cc, "expiry": s.expiry,
                                      "lots": [s.lot_at(k) for k in range(s.n)]})
    return out


def executor_cases(lab, root):
    """Whole executor runs on the fixture libraries: every trial row (without its time stamp)."""
    E = lab.engines_api()
    X, M = E.executor, E.market
    out = {}
    tmp = tempfile.mkdtemp(prefix="bt-golden-")
    try:
        lib = os.path.join(tmp, "edge-hunt")
        shutil.copytree(os.path.join(HUNT, "edge-hunt"), lib)
        mk = M.FolderMarket(HUNT, lab.archive_api(), bars_dir=LABFIX)
        for hid, path in X.library(lib):
            r = X.execute(path, mk, os.path.join(tmp, "out"), __import__("datetime").datetime(2026, 11, 1, 12, 0))
            rows = X.read_trials(r["dir"]) if r.get("dir") and os.path.isdir(r["dir"]) else []
            for row in rows:
                row.pop("at", None)
                row.pop("engine_rev", None)
            out["hunt-" + hid] = digest({"state": r["state"], "trials": rows,
                                         "verdict": (r.get("outcome") or {}).get("verdict")})
        # an option family through the executor on the real-data fixture archive (eh-05 re-dated)
        lib2 = os.path.join(tmp, "opt", "edge-hunt")
        os.makedirs(lib2)
        doc = _hyp(root, "eh-05")
        doc["id"], doc["rank"] = "eh-95-fixture-vrp-selling", 95
        doc["span"] = {"train": {"from": "2026-09-30", "to": "2026-10-14"},
                       "validation": {"from": "2026-10-15", "to": "2026-10-30"},
                       "holdout": {"from": "2026-11-16", "to": "2027-05-31"}, "embargo_days": 14}
        with open(os.path.join(lib2, doc["id"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        with open(os.path.join(lib2, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"hypotheses": [{"id": doc["id"], "sha256": X.registered_hash(doc)}]}, fh)
        r = X.execute(os.path.join(lib2, doc["id"] + ".json"), M.ArchiveMarket(EDGE, lab.archive_api()),
                      os.path.join(tmp, "opt", "out"), __import__("datetime").datetime(2026, 11, 1, 12, 0))
        rows = X.read_trials(r["dir"])
        for row in rows:
            row.pop("at", None)
            row.pop("engine_rev", None)
        out["hunt-eh-95-vrp"] = digest({"state": r["state"], "trials": rows,
                                        "verdict": (r.get("outcome") or {}).get("verdict")})
    finally:
        shutil.rmtree(tmp, True)
    return out


def golden_digests(lab, root):
    out = {}
    out.update(option_cases(lab, root))
    out.update(v2_cases(lab, root))
    out.update(executor_cases(lab, root))
    return out
