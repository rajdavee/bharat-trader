"""The forward-test dashboard: each strategy's paper forward record against what its backtest predicted.

Pure functions on plain records (trader/lab/<name>/results.json, forward.json, status.json). lab.py's
forward_report() reads the files and hands them in; nothing here writes, sizes or places anything.

The backtest's expectation is its OUT-OF-SAMPLE (walk-forward) trades after charges: N trades with mean R mu,
standard deviation s and win rate p. For a forward record of n trades:

  predicted range of the forward mean R   mu +- z * s * sqrt(1/n + 1/N)       (a prediction interval: the
                                                                               forward sample's own noise
                                                                               plus the backtest's)
  predicted range of the forward win rate p +- z * sqrt(p(1-p) * (1/n + 1/N)), clipped to 0..1
  the forward record's own 95% ranges      mean R +- z * sd/sqrt(n); win rate: Wilson

  inside / below / above                  where the forward mean R falls against the predicted range
                                          ("too few" under min_judge trades: noise decides)

  control chart (one-sided lower CUSUM)   z_i = (r_i - mu) / s, S_i = max(0, S_(i-1) - z_i - k), alarm when
                                          S_i > h. With k = 0.5 and h = 4 a record that really matches the
                                          backtest raises a false alarm about once in 170 trades, and a fall
                                          of one standard deviation per trade is caught in about 8 trades.

  decay alert                             at least decay_min_trades forward trades, and either the running
                                          forward mean R sat below the predicted range's lower bound for the
                                          last decay_run trades in a row, or the CUSUM alarmed. A PROMOTE or
                                          WATCH strategy with a decay alert gets the recommendation DEMOTE,
                                          applied only when you ask (lab.py forward-report --demote NAME,
                                          the lab's own status mechanism, like the co-trader's calibration).

Eligibility follows trader-lab's own decide(): min_forward_trades forward trades (gates_set), with a mean above
0R and a profit factor of at least 1. The days to go are an estimate from the backtest's out-of-sample trade
rate (trades per session), not a promise.

Education and research tooling, not financial advice.
"""

import math

Z = 1.959964
SETTINGS = {
    "z": Z,                    # two-sided 95%
    "min_judge": 5,            # forward trades before inside / below / above is said at all
    "decay_min_trades": 10,    # forward trades before a decay alert can fire
    "decay_run": 5,            # trades in a row with the running mean below the predicted range
    "cusum_k": 0.5,            # the CUSUM's allowance, in backtest standard deviations
    "cusum_h": 4.0,            # the CUSUM's decision line, in backtest standard deviations
}
ACTIVE = ("PROMOTE", "WATCH")


def _mean(x):
    return sum(x) / float(len(x)) if x else None


def _sd(x):
    if len(x) < 2:
        return None
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def _r4(x):
    return None if x is None else round(x, 4)


def wilson(k, n, z=Z):
    if n <= 0:
        return None
    p = float(k) / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)) / d
    return [round(max(0.0, c - half), 4), round(min(1.0, c + half), 4)]


def rs_of(trades):
    return [float(t["r"]) for t in trades if t.get("r") is not None]


def describe(trades, z=Z):
    """n, mean R with its 95% range, sd, win rate (after charges: net > 0) with its Wilson range, net rupees."""
    rs = rs_of(trades)
    n = len(rs)
    nets = [float(t.get("net") or 0) for t in trades if t.get("r") is not None]
    wins = sum(1 for x in nets if x > 0)
    m, sd = _mean(rs), _sd(rs)
    half = z * sd / math.sqrt(n) if sd is not None and n else None
    gl = -sum(x for x in nets if x <= 0)
    return {"trades": n, "mean_r": _r4(m), "sd_r": _r4(sd),
            "mean_r_ci": [_r4(m - half), _r4(m + half)] if half is not None else None,
            "wins": wins, "win_rate": _r4(wins / float(n)) if n else None, "win_rate_ci": wilson(wins, n, z),
            "net": round(sum(nets), 2), "profit_factor": round(sum(x for x in nets if x > 0) / gl, 3) if gl > 0 else None}


def predicted(bt, n, z=Z):
    """The backtest's predicted ranges for a forward record of n trades (None when it cannot be said)."""
    N, mu, s, p = bt["trades"], bt["mean_r"], bt["sd_r"], bt["win_rate"]
    if not n or not N or mu is None or s is None:
        return None
    f = math.sqrt(1.0 / n + 1.0 / N)
    wr = None
    if p is not None:
        hw = z * math.sqrt(max(p * (1 - p), 0.0)) * f
        wr = [_r4(max(0.0, p - hw)), _r4(min(1.0, p + hw))]
    return {"mean_r": [_r4(mu - z * s * f), _r4(mu + z * s * f)], "win_rate": wr}


def cusum_lower(rs, mu, s, k=0.5, h=4.0):
    """The one-sided lower CUSUM path on standardised R and the first trade (1-based) where it passed h."""
    if mu is None or not s:
        return {"path": [], "alarm_at": None, "last": None}
    S, path, alarm = 0.0, [], None
    for i, r in enumerate(rs):
        S = max(0.0, S - (r - mu) / s - k)
        path.append(round(S, 3))
        if alarm is None and S > h:
            alarm = i + 1
    return {"path": path, "alarm_at": alarm, "last": path[-1] if path else None, "k": k, "h": h}


def running_below(rs, bt, z=Z):
    """For each forward trade i: the running mean of the first i trades and the predicted lower bound for i
    trades. Returns (rows, trailing run of trades with the running mean below the bound)."""
    rows, run, total = [], 0, 0.0
    for i, r in enumerate(rs):
        total += r
        m = total / (i + 1)
        pr = predicted(bt, i + 1, z)
        lo = pr["mean_r"][0] if pr else None
        below = lo is not None and m < lo
        run = run + 1 if below else 0
        rows.append({"n": i + 1, "running_mean_r": round(m, 4), "lower": lo, "below": below})
    return rows, run


def judge(fwd, pr, min_judge):
    if fwd["trades"] < min_judge or pr is None:
        return "too few"
    lo, hi = pr["mean_r"]
    m = fwd["mean_r"]
    return "below" if m < lo else ("above" if m > hi else "inside")


def eligibility(fwd_trades, gates, bt, verdict):
    """Trades and (estimated) sessions until trader-lab's PROMOTE rule can be met, and whether it is met now."""
    need = int((gates or {}).get("min_forward_trades") or 20)
    rs = rs_of(fwd_trades)
    n = len(rs)
    left = max(0, need - n)
    rate = bt.get("trades_per_session")
    sessions = int(math.ceil(left / rate)) if left and rate else (0 if not left else None)
    d = describe(fwd_trades)
    ok = n >= need and (d["mean_r"] or 0) > 0 and (d["profit_factor"] is None or d["profit_factor"] >= 1.0)
    first = min((t.get("exit_date") or "" for t in fwd_trades), default="") or None
    if verdict == "PROMOTE":
        words = "PROMOTE already (the forward record held up)"
    elif verdict not in ACTIVE:
        words = "not on the way to PROMOTE: verdict %s" % (verdict or "none")
    elif left:
        words = "%d more forward trade%s needed (%d of %d)%s" % (
            left, "" if left == 1 else "s", n, need,
            ", about %d session%s at the backtest's pace" % (sessions, "" if sessions == 1 else "s")
            if sessions else "")
    else:
        words = "enough trades (%d of %d); PROMOTE needs a mean above 0R and a profit factor of at least 1: %s" % (
            n, need, "met" if ok else "not met")
    return {"need": need, "have": n, "trades_left": left, "sessions_left": sessions, "eligible_now": ok,
            "first_forward_exit": first, "words": words}


def backtest_expectation(res):
    """The backtest's out-of-sample record as the expectation (trades, mean R, sd, win rate, pace)."""
    oos = res.get("oos_trades") or []
    d = describe(oos)
    st = (res.get("walk_forward") or {}).get("stats") or {}
    sessions = st.get("days") or 0
    d["sessions"] = sessions
    d["trades_per_session"] = round(d["trades"] / float(sessions), 4) if sessions and d["trades"] else None
    d["source"] = "out-of-sample (walk-forward) trades after charges"
    if not oos and st.get("trades"):
        # an old results.json without the trades: the summary only (no sd, so no range)
        d.update({"trades": st.get("trades"), "mean_r": st.get("expectancy_r"), "win_rate": st.get("win_rate"),
                  "source": "out-of-sample summary only (no trade list: run the lab again for ranges)"})
    return d


def strategy_row(name, res, status, fwd_doc, calibration=None, demoted=None, settings=None):
    """One strategy's dashboard row."""
    s = dict(SETTINGS, **(settings or {}))
    z = float(s["z"])
    h = (status or {}).get("spec_hash") or (res or {}).get("spec_hash")
    trades = [t for t in (fwd_doc or {}).get("trades") or [] if (not h or t.get("spec_hash") == h)
              and t.get("reason") != "end of data"]
    trades.sort(key=lambda t: (t.get("exit_date") or "", t.get("exit_time") or "", t.get("symbol") or ""))
    verdict = (status or {}).get("verdict")
    bt = backtest_expectation(res or {})
    fwd = describe(trades, z)
    pr = predicted(bt, fwd["trades"], z)
    rs = rs_of(trades)
    chart = cusum_lower(rs, bt["mean_r"], bt["sd_r"], float(s["cusum_k"]), float(s["cusum_h"]))
    rows, run = running_below(rs, bt, z)
    where = judge(fwd, pr, int(s["min_judge"]))
    wr_where = None
    if pr and pr.get("win_rate") and fwd["trades"] >= int(s["min_judge"]):
        lo, hi = pr["win_rate"]
        wr_where = "below" if fwd["win_rate"] < lo else ("above" if fwd["win_rate"] > hi else "inside")
    reasons = []
    if fwd["trades"] >= int(s["decay_min_trades"]):
        if run >= int(s["decay_run"]):
            reasons.append("the running forward mean has been below the backtest's predicted range for the last %d "
                           "trades (now %+.3fR, the range starts at %+.3fR)" % (run, fwd["mean_r"], pr["mean_r"][0]))
        if chart["alarm_at"]:
            reasons.append("the control chart (lower CUSUM, k %g, h %g) alarmed at forward trade %d" % (
                chart["k"], chart["h"], chart["alarm_at"]))
    decay = bool(reasons)
    rec = None
    if decay and verdict in ACTIVE:
        rec = {"action": "DEMOTE", "why": "; ".join(reasons),
               "how": "python3 trader-lab/lab.py forward-report --demote %s" % name}
    hist = [{"at": x.get("at"), "verdict": x.get("verdict"), "why": x.get("why"), "source": "trader-lab"}
            for x in (status or {}).get("history") or []]
    if demoted:
        hist.append({"at": demoted.get("at"), "verdict": "DEMOTE", "why": "co-trader calibration: %s"
                     % demoted.get("why", ""), "source": "trader-cotrader"})
    hist.sort(key=lambda x: x.get("at") or "")
    cal = calibration or {"n": 0}
    return {
        "strategy": name, "title": (res or {}).get("title"), "verdict": verdict, "why": (status or {}).get("why"),
        "kind": "options" if (res or {}).get("kind") == "options" or "options" in str((res or {}).get("schema") or "")
        else "equity", "spec_hash": h,
        "backtest": bt, "forward": fwd, "predicted": pr, "where": where, "win_rate_where": wr_where,
        "control_chart": {"k": chart.get("k"), "h": chart.get("h"), "alarm_at": chart["alarm_at"],
                          "last": chart["last"], "path": chart["path"][-120:]},
        "running": rows[-120:], "below_run": run,
        "decay": decay, "decay_reasons": reasons, "recommendation": rec,
        "calibration": {k: cal.get(k) for k in ("n", "brier", "brier_reference", "skill", "mean_p", "observed",
                                                "observed_ci")},
        "timeline": hist[-30:], "eligibility": eligibility(trades, (res or {}).get("gates_set"), bt, verdict),
        "words": row_words(name, verdict, bt, fwd, pr, where, decay, reasons),
    }


def row_words(name, verdict, bt, fwd, pr, where, decay, reasons):
    if not fwd["trades"]:
        return "%s (%s): no forward trades yet. The backtest expects %s a trade over %d out-of-sample trades." % (
            name, verdict or "no verdict", "%+.3fR" % bt["mean_r"] if bt.get("mean_r") is not None else "–",
            bt.get("trades") or 0)
    s = "%s (%s): %d forward trade%s average %+.3fR" % (name, verdict or "no verdict", fwd["trades"],
                                                       "" if fwd["trades"] == 1 else "s", fwd["mean_r"])
    if fwd.get("mean_r_ci"):
        s += " (95%% %+.3f to %+.3f)" % tuple(fwd["mean_r_ci"])
    if pr:
        s += "; the backtest predicted %+.3f to %+.3f for %d trades" % (pr["mean_r"][0], pr["mean_r"][1], fwd["trades"])
    s += {"inside": ": inside the range", "below": ": BELOW the range", "above": ": above the range",
          "too few": ": too few trades to judge"}[where]
    if decay:
        s += ". Decay alert: " + "; ".join(reasons)
    return s + "."


def summary_words(rows):
    active = [r for r in rows if r["verdict"] in ACTIVE]
    dec = [r["strategy"] for r in rows if r["decay"]]
    if not rows:
        return "No trader-lab strategy has a forward test yet: run one (lab.py run NAME) and, once it is WATCH, " \
               "let the auto-pilot build its paper record."
    s = "%d strateg%s in a forward test (PROMOTE or WATCH), %d in all." % (
        len(active), "y" if len(active) == 1 else "ies", len(rows))
    if dec:
        s += " Decay alert: %s." % ", ".join(dec)
    return s


DOES_NOT_SHOW = [
    "A paper fill is a model: real fills, queue position and slippage can be worse.",
    "A forward record of a few weeks proves little either way; the ranges say how little.",
    "The predicted range assumes the backtest's out-of-sample trades are a fair sample of the future; a regime "
    "change breaks that, and so does any overfitting the gates did not catch.",
    "Inside the range is not proof of an edge, and below it is not proof of none: they are odds on noise.",
]
