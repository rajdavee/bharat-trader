"""The edge-hunt executor: a pre-registered hypothesis file in, lab runs and a sealed-holdout verdict out.

    1. The file must match its manifest hash (its registered content, without status and outcome); an
       edited file is refused: a change is a new hypothesis with a new id.
    2. Every grid point runs on the train window and on the validation window, each closed out at its last
       session (purged); the data is read only up to the validation's last day. Each point is one trial,
       logged in trials.jsonl and never run twice. The trial count the deflated Sharpe ratio is charged for
       is the grid, plus the file's prior_trials, plus the trials of the same family already run by this
       executor, plus any extra trials you declare.
    3. The finalist is the grid point with the best primary metric on train. It is screened on validation
       with the lab's gates (and the file's overrides and extras): out-of-sample trades, expectancy after
       charges, profit factor, drawdown on the registered basis, the deflated Sharpe ratio, the probability
       of backtest overfitting over the whole grid, 1.5x costs, the grid neighbourhood, and the primary
       metric itself. Walk-forward folds, 2x costs and a regime split are reported alongside.
    4. Only a finalist that passes every gate opens the holdout: holdout.json is written first (the
       opening is logged before any holdout number exists), then the holdout is run once and recorded.
       A second opening is refused, whatever happened to the first.
    5. The verdict (REJECT, or WATCH at best) is written to the file's status and outcome, the only fields
       the hash ignores, with the scorecard.

Files: <out>/edge-hunt/<id>/state.json, trials.jsonl, holdout.json, results.json, scorecard.md.
"""

import datetime
import hashlib
import itertools
import json
import math
import os
import re

from . import core, jobs

STATUS_OF = {"REJECT": "rejected", "WATCH": "watch"}
METRIC_WORDS = {"oos_active_sharpe": "active Sharpe over the benchmark", "oos_net_sharpe": "net Sharpe",
                "oos_expectancy_r": "expectancy a trade after charges", "oos_mean_event_return":
                "mean abnormal return an event after charges", "oos_return_on_margin": "return on margin"}


class HuntError(core.EngineError):
    pass


# ---------------------------------------------------------------- the registration

def registered_hash(doc):
    """SHA-256 of the registered content: canonical JSON (keys sorted, no spaces) without status and outcome."""
    body = {k: v for k, v in doc.items() if k not in ("status", "outcome")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc, indent=1):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=indent, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def library(folder):
    """[(id, path)] of the hypothesis files in a folder, by rank."""
    out = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.endswith(".json") and f != "manifest.json":
                doc = read_json(os.path.join(folder, f))
                out.append((doc.get("rank") or 99, doc.get("id") or f[:-5], os.path.join(folder, f)))
    return [(i, p) for _, i, p in sorted(out)]


def resolve(arg, folder):
    p = arg if arg.endswith(".json") else os.path.join(folder, arg + ".json")
    if os.path.isfile(p):
        return p
    ids = [i for i, _ in library(folder)]
    hit = [i for i in ids if i == arg or i.startswith(arg.lower() + "-") or i.upper().startswith(arg.upper() + "-")]
    if len(hit) == 1:
        return os.path.join(folder, hit[0] + ".json")
    raise HuntError("no edge-hunt hypothesis %r in %s (%s)" % (arg, folder, ", ".join(ids) or "none"))


def load(path):
    """(doc, registration hash) after checking the file against its folder's manifest.json."""
    doc = read_json(path)
    if not isinstance(doc, dict) or "id" not in doc:
        raise HuntError("%s is not an edge-hunt hypothesis" % path)
    man = read_json(os.path.join(os.path.dirname(os.path.abspath(path)), "manifest.json"), {}) or {}
    row = next((r for r in man.get("hypotheses") or [] if r.get("id") == doc["id"]), None)
    h = registered_hash(doc)
    if row is None:
        raise HuntError("%s is not in the manifest: only a registered hypothesis runs" % doc["id"])
    if row.get("sha256") != h:
        raise HuntError("%s changed after registration (hash %s, registered %s): a change is a new hypothesis "
                        "with a new id; this one is refused" % (doc["id"], h[:12], str(row.get("sha256"))[:12]))
    if os.path.basename(path)[:-5] != doc["id"]:
        raise HuntError("%s: the file name must be its id" % path)
    return doc, h


def grid(doc):
    """Every grid point, in the registration's own order: [{name: value}]."""
    names = list(doc["parameters"])
    pts = [dict(zip(names, combo)) for combo in itertools.product(*[doc["parameters"][n] for n in names])]
    if len(pts) != doc["trial_budget"]:
        raise HuntError("%s: the grid has %d points but the trial budget is %d" % (doc["id"], len(pts),
                                                                                   doc["trial_budget"]))
    return pts


def neighbours(doc, pts, j):
    """The points within one step of point j on every axis (itself included)."""
    names = list(doc["parameters"])
    ix = [[doc["parameters"][n].index(p[n]) for n in names] for p in pts]
    me = ix[j]
    return [k for k, v in enumerate(ix) if all(abs(v[a] - me[a]) <= 1 for a in range(len(names)))]


def gates_for(doc):
    g = dict(core.L().GATE_DEFAULTS)
    g.update(doc["gates"].get("overrides") or {})
    return g, dict(doc["gates"].get("extra") or {})


# ---------------------------------------------------------------- state

def run_dir(out, hid):
    return os.path.join(out, "edge-hunt", hid)


def read_trials(d):
    rows = []
    p = os.path.join(d, "trials.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def append_trial(d, row):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "trials.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def family_trials(out, folder, doc):
    """Trials of the same family already run by this executor for other ids (the family's count grows)."""
    n, ids = 0, []
    for hid, path in library(folder):
        if hid == doc["id"]:
            continue
        other = read_json(path, {}) or {}
        if other.get("family") != doc["family"]:
            continue
        k = len(read_trials(run_dir(out, hid)))
        if k:
            n += k
            ids.append(hid)
    return n, ids


def set_status(path, doc, status, outcome):
    """Write status and outcome into the hypothesis file (the hash ignores exactly these two fields)."""
    doc = dict(doc)
    doc["status"], doc["outcome"] = status, outcome
    before = registered_hash(read_json(path))
    if registered_hash(doc) != before:
        raise HuntError("refusing to write %s: only status and outcome may change" % path)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(path + ".tmp", path)
    return doc


# ---------------------------------------------------------------- numbers of one run

def primary_series(doc, run):
    """The daily series the primary metric is about: active returns or net returns."""
    return core.active(run) if doc["primary_metric"] == "oos_active_sharpe" else list(run["ret"])


def primary_value(doc, m, run):
    pm = doc["primary_metric"]
    if pm == "oos_active_sharpe":
        return m.get("active_sharpe") or 0.0
    if pm == "oos_net_sharpe":
        return m["sharpe"]
    if pm == "oos_expectancy_r":
        return m["expectancy_pct"]
    if pm == "oos_mean_event_return":
        ab = [t["abnormal"] for t in run["trades"] if "abnormal" in t]
        return core.mean(ab) * 100 if ab else m["expectancy_pct"]
    if pm == "oos_return_on_margin":
        return run["extra"].get("return_on_margin_pct") or 0.0
    raise HuntError("no primary metric %s" % pm)


def net_value(doc, m, run):
    """The after-charges quantity that must be above zero: active total return, mean event return or
    expectancy, else the total return."""
    pm = doc["primary_metric"]
    if pm == "oos_active_sharpe":
        return m.get("active_total_pct") or 0.0
    if pm in ("oos_expectancy_r",):
        return m["expectancy_pct"]
    if pm in ("oos_mean_event_return", "oos_return_on_margin"):
        return primary_value(doc, m, run)
    return m["total_return_pct"]


def summary(doc, run):
    m = core.metrics(run)
    m["primary"] = round(primary_value(doc, m, run), 5)
    m["net_value"] = round(net_value(doc, m, run), 5)
    ex = {k: v for k, v in run["extra"].items() if k not in ("pairs",)}
    if "pairs" in run["extra"]:
        ex["pairs"] = [{k: p[k] for k in ("y", "x", "sector", "corr", "adf_t", "b")} for p in run["extra"]["pairs"]]
    m["extra"] = ex
    return m


def _round(xs, n=7):
    return [round(x, n) for x in xs]


# ---------------------------------------------------------------- robustness on the grid

def walk_forward(doc, trials, dates):
    """Expanding walk-forward over train + validation, by validation year: choose the point with the best
    Sharpe of its primary series on everything before the year, score it in the year."""
    years = sorted(set(d[:4] for d in dates if d >= doc["span"]["validation"]["from"]))
    folds, oos = [], []
    for y in years:
        before = [k for k, d in enumerate(dates) if d < y]
        inside = [k for k, d in enumerate(dates) if d[:4] == y and d >= doc["span"]["validation"]["from"]]
        if len(before) < 40 or not inside:
            continue

        def sr(t, ks):
            x = [t["series"][k] for k in ks]
            s = core.sd(x)
            return core.mean(x) / s if s > 0 else 0.0
        best = max(range(len(trials)), key=lambda j: (round(sr(trials[j], before), 8), -j))
        x = [trials[best]["series"][k] for k in inside]
        oos.extend(x)
        folds.append({"year": y, "chose": trials[best]["params"], "sessions": len(x),
                      "return_pct": round((math.exp(sum(math.log(1 + v) for v in x if v > -1)) - 1) * 100, 3)})
    s = core.sd(oos)
    return {"folds": folds, "sharpe": round(core.mean(oos) / s * math.sqrt(core.YEAR), 3) if s > 0 else 0.0,
            "positive_folds": sum(1 for f in folds if f["return_pct"] > 0)}


def regime_split(context, days, series):
    """Mean daily return of the series by the previous session's NIFTY trend and India VIX bucket."""
    lab = core.L()
    if not context.get("nifty"):
        return {}
    ctx = {k: lab.Series(k, "1d", v) for k, v in context.items() if v}
    vmap, tmap = lab.regime_labels(ctx, [13.0, 16.0, 20.0])
    out = {}
    for kind, mp in (("trend", tmap), ("vix", vmap)):
        keys = sorted(mp)
        groups = {}
        for d, x in zip(days, series):
            p = lab._before(keys, d)
            groups.setdefault(mp.get(p, "unknown") if p else "unknown", []).append(x)
        out[kind] = [{"regime": g, "sessions": len(v), "mean_daily_pct": round(core.mean(v) * 100, 4),
                      "annualised_pct": round(core.mean(v) * core.YEAR * 100, 2)} for g, v in sorted(groups.items())]
    return out


def gate_rows(doc, g, extra, fin, val, whole, dsr, pbo, cost, neigh_share):
    """[{gate, needs, got, pass}] on validation (drawdown on train + validation), as registered."""
    rows = []

    def add(name, needs, got, ok):
        rows.append({"gate": name, "needs": needs, "got": got, "pass": bool(ok)})
    pf = val["profit_factor"]
    add("enough out-of-sample trades", ">= %d (validation)" % g["min_oos_trades"], val["trades"],
        val["trades"] >= g["min_oos_trades"])
    add("out-of-sample expectancy after charges", "> %g%% a trade" % g["min_oos_expectancy_r"],
        "%.3f%%" % val["expectancy_pct"], val["trades"] > 0 and val["expectancy_pct"] > g["min_oos_expectancy_r"])
    add("out-of-sample profit factor", ">= %g" % g["min_oos_profit_factor"], "none (no losses)" if pf is None else pf,
        (pf is None and val["trades"] > 0 and val["net"] > 0) or (pf is not None and pf >= g["min_oos_profit_factor"]))
    basis = extra.get("drawdown_basis", "capital")
    dd = whole.get("active_max_drawdown_pct") if basis == "active" else whole["max_drawdown_pct"]
    add("max drawdown (train + validation, %s)" % basis, "<= %g%%" % g["max_drawdown_pct"],
        "%.2f%%" % (dd or 0.0), (dd or 0.0) <= g["max_drawdown_pct"])
    if "max_drawdown_vs_benchmark" in extra:
        k = float(extra["max_drawdown_vs_benchmark"])
        bdd = val.get("bench_max_drawdown_pct")
        add("drawdown against the benchmark (validation)", "<= %gx the benchmark's" % k,
            "%.2f%% vs %s" % (val["max_drawdown_pct"], "%.2f%%" % bdd if bdd is not None else "none"),
            bdd is not None and val["max_drawdown_pct"] <= k * bdd)
    add("deflated Sharpe ratio", ">= %g" % g["min_dsr"], dsr["dsr"] if dsr else "not measured",
        bool(dsr) and dsr["dsr"] >= g["min_dsr"])
    add("probability of backtest overfitting", "< %g" % g["max_pbo"], pbo["pbo"] if pbo else "not measured",
        bool(pbo) and pbo["pbo"] < g["max_pbo"])
    m = g["survive_cost_multiplier"]
    co = cost.get("%gx" % m)
    add("survives %gx costs" % m, "validation %s > 0" % ("active return" if doc["primary_metric"] == "oos_active_sharpe"
                                                         else "net result"),
        "%.3f" % co["net_value"] if co else "not run", bool(co) and co["net_value"] > 0)
    add("parameter neighbourhood", ">= %d%% of nearby grid points positive on validation" %
        round(g["min_neighbourhood_positive"] * 100), "%d%%" % round(neigh_share * 100),
        neigh_share >= g["min_neighbourhood_positive"])
    add("primary metric on validation (%s)" % METRIC_WORDS.get(doc["primary_metric"], doc["primary_metric"]),
        "> 0", round(val["primary"], 4), val["primary"] > 0)
    if "min_oos_months" in extra:
        add("validation months", ">= %d" % int(extra["min_oos_months"]), val["months"],
            val["months"] >= int(extra["min_oos_months"]))
    yrs = (core.day(doc["span"]["holdout"]["to"]) - core.day(doc["span"]["train"]["from"])).days / 365.25
    need = float(doc["gates"].get("min_span_years") or 0)
    exc = doc["gates"].get("span_exception")
    add("span", ">= %g years" % need, "%.1f years%s" % (yrs, " (declared exception: verdict capped at WATCH)"
                                                         if exc else ""), yrs >= need or bool(exc))
    return rows


def holdout_checks(doc, extra, hm, run):
    """The registered pass_if, as checks on the holdout numbers. The count comes from "at least N" in the
    text, else the file's min_holdout_trades."""
    pm = doc["primary_metric"]
    text = doc["holdout_rule"]["pass_if"]
    rows = []

    def add(name, got, ok):
        rows.append({"check": name, "got": got, "pass": bool(ok)})
    mt = re.search(r"at least (\d+)", text)
    need = int(mt.group(1)) if mt else int(extra.get("min_holdout_trades") or 1)
    add("holdout trades or events >= %d" % need, hm["trades"], hm["trades"] >= need)
    add("holdout primary metric > 0", round(hm["primary"], 4), hm["primary"] > 0)
    add("holdout result after charges > 0", round(hm["net_value"], 4), hm["net_value"] > 0)
    basis = extra.get("drawdown_basis")
    g, _ = gates_for(doc)
    if basis == "active" and "max_drawdown_vs_benchmark" not in extra:
        add("deepest active drawdown < %g%%" % g["max_drawdown_pct"], hm.get("active_max_drawdown_pct"),
            (hm.get("active_max_drawdown_pct") or 0) < g["max_drawdown_pct"])
    if "max_drawdown_vs_benchmark" in extra:
        k = float(extra["max_drawdown_vs_benchmark"])
        add("Sharpe above the benchmark's", "%s vs %s" % (hm["sharpe"], hm.get("bench_sharpe")),
            hm.get("bench_sharpe") is not None and hm["sharpe"] > hm["bench_sharpe"])
        add("drawdown <= %gx the benchmark's" % k, "%s%% vs %s%%" % (hm["max_drawdown_pct"],
                                                                    hm.get("bench_max_drawdown_pct")),
            hm.get("bench_max_drawdown_pct") is not None and hm["max_drawdown_pct"] <= k * hm["bench_max_drawdown_pct"])
    iw = (hm.get("extra") or {}).get("in_window")
    if iw and "other sessions" in text:
        add("in-window mean above the other sessions'", "%s%% vs %s%%" % (iw["in_mean_pct"], iw["other_mean_pct"]),
            iw["in_above_other"])
    sl = (hm.get("extra") or {}).get("predictive_slope")
    if sl is not None and "slope" in text:
        add("positive predictive slope", sl, sl > 0)
    if pm == "oos_active_sharpe":
        pass
    return rows


# ---------------------------------------------------------------- the run

def plan(doc, h, job, out, folder, extra_trials=0):
    pts = grid(doc)
    fam_n, fam_ids = family_trials(out, folder, doc)
    prior = int(doc["prior_trials"]["count"])
    return {"id": doc["id"], "family": doc["family"], "engine": getattr(job, "label", None) or job.engine,
            "registration_hash": h,
            "grid_points": len(pts), "prior_trials": prior, "family_trials": fam_n, "family_ids": fam_ids,
            "extra_trials": int(extra_trials), "trials_counted": len(pts) + prior + fam_n + int(extra_trials),
            "windows": {k: doc["span"][k] for k in ("train", "validation", "holdout")},
            "embargo_days": doc["span"]["embargo_days"], "primary_metric": doc["primary_metric"],
            "blockers": job.blockers(), "status": doc["status"]}


def execute(path, mkt, out, now=None, progress=None, deadline=None, cancel=None, extra_trials=0, dry_run=False,
            cfg=None, context=None):
    """Run one hypothesis file as registered. Returns {"state", "plan", ...}; state is one of dry-run,
    blocked, paused, decided. Raises HuntError when the file or its run history do not match."""
    now = now or datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    folder = os.path.dirname(os.path.abspath(path))
    doc, h = load(path)
    d = run_dir(out, doc["id"])
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    if st and st.get("registration_hash") != h:
        raise HuntError("%s: the run history in %s belongs to registration %s, not %s" % (
            doc["id"], d, str(st.get("registration_hash"))[:12], h[:12]))
    if doc["status"] == "withdrawn":
        raise HuntError("%s is withdrawn" % doc["id"])
    if st.get("phase") == "decided" or doc["status"] in ("rejected", "watch", "promoted"):
        # decided once: the file's own outcome stands even if the run folder was deleted
        return {"state": "decided", "already": True, "outcome": doc.get("outcome") or st.get("outcome"),
                "plan": st.get("plan"), "dir": d}
    try:
        job = jobs.job_for(doc, mkt, cfg)
    except jobs.Blocked as e:
        return {"state": "blocked", "plan": {"id": doc["id"], "family": doc["family"], "blockers": [str(e)]},
                "dir": d}
    pl = plan(doc, h, job, out, folder, extra_trials)
    if st.get("plan"):
        # the trial count is fixed by the first run: later family runs and declarations only add to it
        pl["family_trials"] = max(pl["family_trials"], st["plan"].get("family_trials", 0))
        pl["extra_trials"] = max(pl["extra_trials"], st["plan"].get("extra_trials", 0))
        pl["trials_counted"] = pl["grid_points"] + pl["prior_trials"] + pl["family_trials"] + pl["extra_trials"]
    if pl["blockers"]:
        return {"state": "blocked", "plan": pl, "dir": d}
    val_to = doc["span"]["validation"]["to"]
    try:
        job.load(val_to)
    except jobs.Blocked as e:
        pl["blockers"] = [str(e)]
        return {"state": "blocked", "plan": pl, "dir": d}
    cov = {}
    for k in ("train", "validation"):
        w = doc["span"][k]
        first, last, n = job.coverage(w["from"], w["to"])
        cov[k] = {"first": first, "last": last, "sessions": n}
        late = first is None or (core.day(first) - core.day(w["from"])).days > 10
        early = last is None or (core.day(w["to"]) - core.day(last)).days > 10
        if late or early:
            pl["blockers"].append("the data covers %s to %s of the %s window %s to %s: the run waits for the "
                                  "history (the long-history archive)" % (first, last, k, w["from"], w["to"]))
    pl["coverage"] = cov
    if pl["blockers"]:
        return {"state": "blocked", "plan": pl, "dir": d}
    if dry_run:
        pl["notes"] = job.notes
        return {"state": "dry-run", "plan": pl, "dir": d}
    os.makedirs(d, exist_ok=True)
    if not st:
        st = {"id": doc["id"], "registration_hash": h, "started": stamp, "phase": "trials", "plan": pl}
    st["plan"] = pl
    st["updated"] = stamp
    write_json(os.path.join(d, "state.json"), st)
    if doc["status"] == "registered":
        doc = set_status(path, doc, "running", None)
    pts = grid(doc)
    done = {r["trial"]: r for r in read_trials(d)}
    for j, p in enumerate(pts):
        if j in done:
            continue
        if cancel is not None and cancel():
            return {"state": "paused", "plan": pl, "done": len(done), "of": len(pts), "dir": d, "why": "cancelled"}
        if deadline is not None and deadline():
            return {"state": "paused", "plan": pl, "done": len(done), "of": len(pts), "dir": d, "why": "time budget"}
        tr = job.run(p, doc["span"]["train"]["from"], doc["span"]["train"]["to"])
        va = job.run(p, doc["span"]["validation"]["from"], val_to)
        row = {"trial": j, "params": p, "at": now.strftime("%Y-%m-%d %H:%M:%S"),
               "train": summary(doc, tr), "validation": summary(doc, va),
               "dates": tr["days"] + va["days"],
               "series": _round(primary_series(doc, tr) + primary_series(doc, va)),
               "val_series": _round(primary_series(doc, va))}
        append_trial(d, row)
        done[j] = row
        if progress:
            progress("trial %d of %d: %s" % (len(done), len(pts), ", ".join("%s=%s" % kv for kv in p.items())),
                     len(done) / float(len(pts)))
    trials = [done[j] for j in range(len(pts))]
    res = decide(doc, h, job, trials, pl, d, now, context, path)
    if res["verdict"] == "WAITING":
        st.update({"phase": "waiting for the holdout", "updated": stamp, "why": res["why"]})
        write_json(os.path.join(d, "state.json"), st)
        write_json(os.path.join(d, "results.json"), res)
        with open(os.path.join(d, "scorecard.md"), "w", encoding="utf-8") as fh:
            fh.write(scorecard(doc, res))
        return {"state": "waiting", "plan": pl, "dir": d, "why": res["why"], "results": res}
    status = STATUS_OF.get(res["verdict"], "rejected")
    outcome = res["outcome"]
    doc = set_status(path, doc, status, outcome)
    st.update({"phase": "decided", "verdict": res["verdict"], "updated": stamp, "outcome": outcome})
    write_json(os.path.join(d, "state.json"), st)
    write_json(os.path.join(d, "results.json"), res)
    with open(os.path.join(d, "scorecard.md"), "w", encoding="utf-8") as fh:
        fh.write(scorecard(doc, res))
    return {"state": "decided", "already": False, "outcome": outcome, "plan": pl, "dir": d, "results": res}


def decide(doc, h, job, trials, pl, d, now, context=None, path=None):
    lab = core.L()
    g, extra = gates_for(doc)
    pts = [t["params"] for t in trials]
    fin = max(range(len(trials)), key=lambda j: (round(trials[j]["train"]["primary"], 8), -j))
    ft = trials[fin]
    val = ft["validation"]
    # whole sample (train + validation) of the finalist, for the drawdown gate
    tr_run = job.run(ft["params"], doc["span"]["train"]["from"], doc["span"]["train"]["to"])
    va_run = job.run(ft["params"], doc["span"]["validation"]["from"], doc["span"]["validation"]["to"])
    whole_run = {"days": tr_run["days"] + va_run["days"], "ret": tr_run["ret"] + va_run["ret"],
                 "bench": (tr_run["bench"] + va_run["bench"]) if tr_run.get("bench") and va_run.get("bench") else None,
                 "equity": tr_run["equity"] + va_run["equity"], "exposure": tr_run["exposure"] + va_run["exposure"],
                 "trades": tr_run["trades"] + va_run["trades"], "traded": tr_run["traded"] + va_run["traded"],
                 "charges": tr_run["charges"] + va_run["charges"], "capital": tr_run["capital"], "extra": {}}
    whole = core.metrics(whole_run)
    # the deflated Sharpe ratio on the validation series, charged for every counted trial
    vs = [t["val_series"] for t in trials]
    trial_sr = [core.mean(x) / core.sd(x) if core.sd(x) > 0 else 0.0 for x in vs]
    rets = vs[fin]
    dsr = lab.deflated_sharpe(rets, trial_sr if len(trials) > 1 else (), pl["trials_counted"] - len(trials)) \
        if len(rets) > 2 and core.sd(rets) > 0 else None
    # PBO over the grid's train + validation series (aligned by date)
    dates = sorted(set(x for t in trials for x in t["dates"]))
    pos = {x: k for k, x in enumerate(dates)}
    cols = []
    for t in trials:
        c = [0.0] * len(dates)
        for x, v in zip(t["dates"], t["series"]):
            c[pos[x]] = v
        cols.append(c)
    matrix = [list(r) for r in zip(*cols)]
    nb = 8 if len(dates) >= 32 else (4 if len(dates) >= 8 else 2)
    pbo = lab.pbo_cscv(matrix, nb) if len(trials) > 1 and matrix else None
    cost = {}
    for m in sorted(set([1.5, 2.0, float(g["survive_cost_multiplier"])])):
        r = job.run(ft["params"], doc["span"]["validation"]["from"], doc["span"]["validation"]["to"], m)
        s = summary(doc, r)
        cost["%gx" % m] = {"net_value": s["net_value"], "primary": s["primary"], "cagr_pct": s["cagr_pct"],
                           "charges": s["charges"]}
    near = neighbours(doc, pts, fin)
    share = sum(1 for k in near if trials[k]["validation"]["net_value"] > 0) / float(len(near))
    rows = gate_rows(doc, g, extra, ft, val, whole, dsr, pbo, cost, share)
    wf = walk_forward(doc, [{"params": t["params"], "series": _align(t, dates)} for t in trials], dates)
    if context is None:
        # the regime labels come from NIFTY and India VIX up to the validation's last day only
        until = doc["span"]["validation"]["to"]
        context = {"nifty": job.mkt.index("NIFTY 50", job.start, until) or None,
                   "vix": job.mkt.index("India VIX", job.start, until) or None}
    regimes = regime_split(context, va_run["days"], primary_series(doc, va_run))
    failed = [r["gate"] for r in rows if not r["pass"]]
    hold_path = os.path.join(d, "holdout.json")
    holdout = None
    verdict, why = "REJECT", ""
    if failed:
        why = "%d of %d validation gates failed: %s. The holdout stays sealed." % (len(failed), len(rows),
                                                                                  "; ".join(failed))
    elif getattr(job, "holdout_wait", None) and job.holdout_wait(now.strftime("%Y-%m-%d")):
        # a holdout of future, live-captured data (eh-14): not opened, so not burnt, until it exists
        verdict = "WAITING"
        why = ("every validation gate passed, but the holdout cannot be opened yet: %s. It stays sealed; run "
               "this again when it has ended" % job.holdout_wait(now.strftime("%Y-%m-%d")))
    else:
        holdout = open_holdout(doc, h, job, ft["params"], hold_path, now, extra, path)
        if holdout.get("error"):
            why = holdout["error"]
        elif all(c["pass"] for c in holdout["checks"]):
            verdict = "WATCH"
            why = ("every validation gate passed and the holdout, opened once on %s, passed as registered; a "
                   "WATCH goes to the paper forward test, and the programme's Benjamini-Hochberg cut still "
                   "applies" % holdout["opened_at"])
        else:
            bad = [c["check"] for c in holdout["checks"] if not c["pass"]]
            why = "the holdout (opened once on %s) failed: %s" % (holdout["opened_at"], "; ".join(bad))
    outcome = {
        "verdict": verdict, "why": why, "registration_hash": h, "decided_at": now.strftime("%Y-%m-%d %H:%M"),
        "engine": job.engine, "trials": {k: pl[k] for k in ("grid_points", "prior_trials", "family_trials",
                                                            "extra_trials", "trials_counted")},
        "finalist": ft["params"], "chosen_on": "train (best primary metric), screened on validation",
        "validation": {k: val.get(k) for k in ("primary", "net_value", "sharpe", "cagr_pct", "max_drawdown_pct",
                                               "active_sharpe", "active_return_pct", "trades", "turnover_pct_per_year",
                                               "charges_pct_per_year")},
        "gates_passed": "%d of %d" % (len(rows) - len(failed), len(rows)),
        "holdout": None if holdout is None else {k: holdout.get(k) for k in ("opened_at", "window", "p_value", "passed",
                                                                            "error")},
        "scorecard": "trader/lab/edge-hunt/%s/scorecard.md" % doc["id"],
    }
    write_returns(d, doc, ft["params"], tr_run, va_run, holdout)
    if holdout and not holdout.get("error"):
        hm = holdout["metrics"]
        outcome["holdout"].update({k: hm.get(k) for k in ("primary", "net_value", "sharpe", "cagr_pct",
                                                          "max_drawdown_pct", "trades")})
    return {"id": doc["id"], "title": doc["title"], "verdict": verdict, "why": why, "outcome": outcome,
            "plan": pl, "finalist": fin, "gates": rows, "dsr": dsr, "pbo": pbo, "costs": cost,
            "neighbourhood": {"points": [pts[k] for k in near], "positive_share": round(share, 4)},
            "walk_forward": wf, "regimes": regimes, "whole": whole, "validation": val, "train": ft["train"],
            "grid": [{"params": t["params"], "train_primary": t["train"]["primary"],
                      "validation_primary": t["validation"]["primary"],
                      "validation_net": t["validation"]["net_value"]} for t in trials],
            "holdout": holdout, "notes": list(job.notes), "limits": limits(doc)}


def write_returns(d, doc, params, tr_run, va_run, holdout):
    """The finalist's daily NET returns (after charges and slippage) per window, for trader-lab's strategy
    portfolio (lab.py portfolio): train is in sample; validation and the holdout are what a mix may use."""
    segs = {"train": {"days": list(tr_run["days"]), "ret": _round(tr_run["ret"])},
            "validation": {"days": list(va_run["days"]), "ret": _round(va_run["ret"])}, "holdout": None}
    if holdout and not holdout.get("error") and holdout.get("series"):
        segs["holdout"] = holdout["series"]
    write_json(os.path.join(d, "returns.json"), {"schema": "bharat-trader/edge-hunt-returns/1", "id": doc["id"],
                                                 "params": params, "net_of": "charges and slippage",
                                                 "segments": segs})


def _align(t, dates):
    pos = {x: k for k, x in enumerate(t["dates"])}
    return [t["series"][pos[x]] if x in pos else 0.0 for x in dates]


def open_holdout(doc, h, job, params, path, now, extra, file_path=None):
    """The sealed holdout, once. The opening is logged (holdout.json, and the hypothesis file's outcome)
    before a single holdout number is computed; an existing record of an opening is never opened again."""
    prev = read_json(path)
    was = (doc.get("outcome") or {}).get("holdout_opened_at") if isinstance(doc.get("outcome"), dict) else None
    if prev is None and was:
        return {"error": "the hypothesis file records a holdout opened on %s whose record is gone; it is never "
                         "opened again (register a new id)" % was, "opened_at": was, "checks": []}
    if prev is not None:
        if prev.get("metrics") is not None and prev.get("registration_hash") == h and prev.get("params") == params:
            return prev
        return {"error": "the holdout was opened on %s and %s; it is never opened again (register a new id)" % (
            prev.get("opened_at"), "its result was not recorded" if prev.get("metrics") is None else
            "for other parameters"), "opened_at": prev.get("opened_at"), "checks": []}
    w = doc["span"]["holdout"]
    rec = {"id": doc["id"], "registration_hash": h, "params": params, "opened_at": now.strftime("%Y-%m-%d %H:%M"),
           "window": [w["from"], w["to"]], "metrics": None, "opened_once": True}
    write_json(path, rec)
    if file_path:
        set_status(file_path, doc, "running", {"verdict": None, "holdout_opened_at": rec["opened_at"],
                                               "finalist": params, "registration_hash": h})
    try:
        job.load(w["to"])
        first, last, n = job.coverage(w["from"], w["to"])
        if first is None or (core.day(first) - core.day(w["from"])).days > 10 or \
                (core.day(w["to"]) - core.day(last)).days > 10:
            raise jobs.Blocked("the data covers %s to %s of the holdout %s to %s" % (first, last, w["from"], w["to"]))
        r = job.run(params, w["from"], w["to"])
        hm = summary(doc, r)
        r15 = summary(doc, job.run(params, w["from"], w["to"], 1.5))
    except core.EngineError as e:
        rec["error"] = "the holdout was opened but could not be run: %s; it is never opened again" % e
        write_json(path, rec)
        return dict(rec, checks=[])
    series = primary_series(doc, r)
    if doc["primary_metric"] == "oos_mean_event_return":
        series = [t["abnormal"] for t in r["trades"] if "abnormal" in t]
        p = core.stationary_bootstrap_p(series, block=1)
    else:
        p = core.stationary_bootstrap_p(series)
    checks = holdout_checks(doc, extra, hm, r)
    rec.update({"metrics": hm, "at_1_5x": {"net_value": r15["net_value"], "primary": r15["primary"]},
                "p_value": round(p, 4), "checks": checks, "passed": all(c["pass"] for c in checks),
                "trades": r["trades"][:500], "series": {"days": list(r["days"]), "ret": _round(r["ret"])}})
    write_json(path, rec)
    return rec


def limits(doc):
    out = ["It does not predict the future: the train, validation and holdout windows are all the same past.",
           "Charges are a typical discount broker's (the lab's dated tables), income tax is left out and slippage "
           "is a model; a bigger account moves prices more.",
           "A WATCH is a candidate for the paper forward test with its own capital bucket, not a strategy to trade. "
           "Every paper or live order still needs trader-risk PASS and your typed yes.",
           "Fourteen hypotheses are fourteen chances to be fooled: the programme's Benjamini-Hochberg cut on the "
           "holdout p-values decides which WATCH verdicts survive together."]
    if doc["gates"].get("span_exception"):
        out.append("The span is a declared exception (%s)." % doc["gates"]["span_exception"])
    out.append("What was already seen: %s" % doc["contamination"])
    return out


# ---------------------------------------------------------------- the scorecard and the programme status

def _f(x, n=2, pct=False):
    if x is None:
        return "–"
    return ("%." + str(n) + "f%s") % (x, "%" if pct else "")


def scorecard(doc, res):
    o = res["outcome"]
    L = ["# %s: %s" % (doc["id"], doc["title"]), "",
         "**Verdict: %s.** %s" % (res["verdict"], res["why"]), "",
         "Hypothesis: %s" % doc["hypothesis"], "",
         "Registered %s, hash %s. Engine: %s. Primary metric: %s." % (doc["registered_on"], o["registration_hash"][:16],
                                                                     o["engine"], METRIC_WORDS.get(doc["primary_metric"])),
         ""]
    t = o["trials"]
    L.append("Trials counted: %d (grid %d + earlier trials of this kind %d + this family's other edge-hunt runs %d "
             "+ declared %d). The deflated Sharpe ratio is charged for all of them." % (
                 t["trials_counted"], t["grid_points"], t["prior_trials"], t["family_trials"], t["extra_trials"]))
    L.append("")
    L.append("Windows: train %s to %s, validation %s to %s, holdout %s to %s (embargo %d days, each window closed "
             "at its last session)." % (doc["span"]["train"]["from"], doc["span"]["train"]["to"],
                                         doc["span"]["validation"]["from"], doc["span"]["validation"]["to"],
                                         doc["span"]["holdout"]["from"], doc["span"]["holdout"]["to"],
                                         doc["span"]["embargo_days"]))
    L.append("")
    L.append("Finalist (best primary metric on train): %s." % ", ".join("%s=%s" % kv for kv in o["finalist"].items()))
    L.append("")
    mm = ((res.get("validation") or {}).get("extra") or {}).get("margin_model")
    if mm:
        L.append("Margin model: %s (return on margin is over this estimate; docs/options.md, Margin)." % mm)
        L.append("")
    L.append("## The numbers, after charges and slippage")
    L.append("")
    L.append("| | train | validation | holdout |")
    L.append("| --- | --- | --- | --- |")
    hm = (res.get("holdout") or {}).get("metrics") or {}
    for label, k, n, pct in (("CAGR", "cagr_pct", 2, True), ("volatility", "vol_pct", 2, True),
                             ("Sharpe", "sharpe", 2, False), ("Sortino", "sortino", 2, False),
                             ("max drawdown", "max_drawdown_pct", 2, True),
                             ("benchmark CAGR", "bench_cagr_pct", 2, True),
                             ("active return a year", "active_return_pct", 2, True),
                             ("active Sharpe", "active_sharpe", 2, False),
                             ("trades", "trades", 0, False), ("hit rate", "hit_rate", 2, False),
                             ("expectancy a trade", "expectancy_pct", 3, True),
                             ("turnover a year (one way)", "turnover_pct_per_year", 0, True),
                             ("charges a year", "charges_pct_per_year", 2, True),
                             ("exposure", "exposure_pct", 0, True)):
        L.append("| %s | %s | %s | %s |" % (label, _f(res["train"].get(k), n, pct), _f(res["validation"].get(k), n, pct),
                                            _f(hm.get(k), n, pct) if hm else "sealed"))
    L.append("")
    L.append("## Validation gates (written in the file before the run)")
    L.append("")
    L.append("| gate | needs | got | |")
    L.append("| --- | --- | --- | --- |")
    for g in res["gates"]:
        L.append("| %s | %s | %s | %s |" % (g["gate"], g["needs"], g["got"], "pass" if g["pass"] else "**FAIL**"))
    L.append("")
    L.append("## Robustness")
    L.append("")
    ds, pb = res["dsr"], res["pbo"]
    L.append("- Deflated Sharpe ratio: %s (%s trials counted)." % (ds["dsr"] if ds else "not measured",
                                                                 ds["trials"] if ds else t["trials_counted"]))
    L.append("- Probability of backtest overfitting over the grid: %s." % (pb["pbo"] if pb else "not measured"))
    L.append("- Costs on validation: %s." % ", ".join("%s: %.3f" % (k, v["net_value"]) for k, v in sorted(
        res["costs"].items(), key=lambda kv: float(kv[0][:-1]))))
    L.append("- Grid neighbourhood: %d%% of %d points positive on validation." % (
        round(res["neighbourhood"]["positive_share"] * 100), len(res["neighbourhood"]["points"])))
    wf = res["walk_forward"]
    L.append("- Walk-forward by validation year: %d folds, %d positive, Sharpe %.2f." % (
        len(wf["folds"]), wf["positive_folds"], wf["sharpe"]))
    for kind in ("trend", "vix"):
        rows = (res.get("regimes") or {}).get(kind) or []
        if rows:
            L.append("- Validation by %s the session before: %s." % (
                "NIFTY trend" if kind == "trend" else "India VIX", "; ".join(
                    "%s %d sessions, %.2f%% a year" % (r["regime"], r["sessions"], r["annualised_pct"]) for r in rows)))
    cap = (res["validation"].get("extra") or {}).get("capacity")
    if cap and cap.get("note"):
        L.append("- Capacity: %s." % cap["note"])
    ho = res.get("holdout")
    L.append("")
    L.append("## The holdout")
    L.append("")
    if not ho:
        L.append("Sealed: no finalist passed every validation gate, so it was not opened.")
    elif ho.get("error"):
        L.append(ho["error"] + ".")
    else:
        L.append("Opened once on %s for the finalist; one-sided bootstrap p-value %.4f (judged with the programme's "
                 "Benjamini-Hochberg cut)." % (ho["opened_at"], ho["p_value"]))
        L.append("")
        for c in ho["checks"]:
            L.append("- %s: %s (%s)" % (c["check"], c["got"], "pass" if c["pass"] else "FAIL"))
    L.append("")
    L.append("## What this does NOT show")
    L.append("")
    for x in res["limits"] + res["notes"]:
        L.append("- " + x)
    L.append("")
    L.append("Education and research tooling, not financial advice. Nothing here placed an order.")
    return "\n".join(L) + "\n"


def status(folder, out):
    """Every registered hypothesis with its file status, run state and outcome, plus the programme-level
    Benjamini-Hochberg cut over the holdout p-values recorded so far."""
    rows, pv, q = [], {}, 0.10
    for hid, path in library(folder):
        doc = read_json(path)
        try:
            _, h = load(path)
            ok = True
        except HuntError as e:
            ok, h = str(e), registered_hash(doc)
        d = run_dir(out, hid)
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        ho = read_json(os.path.join(d, "holdout.json"))
        trials = len(read_trials(d))
        o = doc.get("outcome") or {}
        # a hypothesis whose holdout stayed sealed counts with p = 1: every registered idea is one chance
        pv[hid] = ho["p_value"] if ho and ho.get("p_value") is not None else 1.0
        q = float((doc["gates"].get("extra") or {}).get("fdr_q", q))
        rows.append({"id": hid, "rank": doc.get("rank"), "family": doc["family"], "title": doc["title"],
                     "status": doc["status"], "verdict": o.get("verdict"), "why": o.get("why"),
                     "trials_run": trials, "trial_budget": doc["trial_budget"],
                     "phase": st.get("phase") or "not started", "holdout_opened": (ho or {}).get("opened_at"),
                     "p_value": (ho or {}).get("p_value"), "registered_ok": ok is True,
                     "problem": None if ok is True else ok,
                     "engine": ("options" if doc["family"] in jobs.FAMILIES_OPTIONS else
                                jobs.JOBS[doc["family"]].engine if doc["family"] in jobs.JOBS else "none"),
                     "prior": doc["prior"]["probability"], "outcome": o or None})
    keep = core.benjamini_hochberg(pv, q) if pv else {}
    opened = sum(1 for r in rows if r["p_value"] is not None)
    for r in rows:
        r["bh_kept"] = keep.get(r["id"]) if r["p_value"] is not None else None
    return {"hypotheses": rows, "fdr_q": q, "holdouts_opened": opened,
            "bh_note": "Benjamini-Hochberg at q = %g across all %d registered hypotheses (a sealed holdout counts as "
                       "p = 1); %d holdout p-values so far" % (q, len(rows), opened)}
