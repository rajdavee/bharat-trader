"""The options engines the edge-hunt executor calls by family name. Standard library only.

Each engine runs ONE grid point of ONE registered hypothesis (trader-lab/library/edge-hunt/<id>.json) on ONE
window of its span, and returns trades and metrics; the executor owns the grid loop, the train ->
validation -> holdout order, the deflated Sharpe ratio, PBO and the gates.

    engine(family) -> fn          families: "vrp-selling", "event-vol", "option-buying"
    fn(hyp, params, window, data, cost_mult=1.0) -> result

    hyp        the registered hypothesis (the JSON file, read as is; this module never edits it)
    params     one grid point: {name: value}, every name in hyp["parameters"], each value one of the
               registered values (anything else is refused: a new value is a new hypothesis)
    window     "train" | "validation" | "holdout", or {"name", "from", "to"}: signals are taken only inside
               it and every position is closed by its last session ("window end", purged). Models (the
               HAR-RV forecast) are fitted on hyp["span"]["train"] only, whatever the window.
    data       {"root": the trader-archive folder, "archive": trader-archive's module (default: the bound
               one), "regime": trader-regime's module, "chains": {UNDERLYING: Chains} (optional, prebuilt),
               "index": {UNDERLYING: daily bars}, "vix": daily bars, "intraday": {UNDERLYING: bars},
               "symbols": [F&O stocks] (event-vol; default: every stock with options in the window's
               bhavcopies), "capital": rupees (sizing; default 10 lakh, one lot), "rate": 0.0557,
               "progress": fn(text)}
    cost_mult  slippage and charges multiplier (1.5 for the cost-stress gate)

    result     {"family", "hypothesis", "engine", "params", "window", "status" ("ok", "no data",
               "waiting for captured chains"), "trades" (the lab's trade shape: net, r, margin,
               return_on_margin_pct, entry_date, exit_date, ...), "n", "metrics" (options_metrics plus
               expectancy_r, win_rate_pct, mean_return_on_premium_pct and the family's own),
               "primary_metric", "primary" (the hypothesis's primary metric on this window), "baseline"
               (vrp-selling: the same condor untimed), "skipped" {why: n}, "notes", "flags"
               {"model_priced", "verdict_cap"}, "registration": the hypothesis id and status}

Nothing here places an order, reads the network or writes a file.
Education and tooling, not financial advice.
"""

import bisect
import datetime
import itertools
import math
import os

O = None          # the options package, set by its loader (options.engines_api())

FAMILIES = ("vrp-selling", "event-vol", "option-buying")
PRIMARY = {"oos_return_on_margin": "return_on_margin_pct", "oos_expectancy_r": "expectancy_r",
           "oos_mean_event_return": "mean_return_on_premium_pct"}
DEFAULT_CAPITAL = 1000000.0
MIN_DTE_MONTHLY = 21           # eh-05: the monthly expiry at least 21 calendar days away
MARGIN_CAP_PCT = 50.0          # sizing: at most half the capital blocked as margin
TAIL_LOSS_PCT = 1.0            # sizing: the defined-risk worst case of a position at most 1% of capital
MIN_HISTORY = 4                # event-vol: earlier results moves of the stock needed for the ratio
IV_HISTORY = 252               # event-vol: sessions of ATM IV behind the IV percentile
IV_HISTORY_MIN = 126
LAST_ENTRY = "15:00"           # option-buying: no new entry after this bar end
FLAT_BY = "15:15"              # option-buying: out by 15:15 ("1515")
TRAIL = 0.30                   # option-buying: "trail_30pct_from_peak"
SNAPSHOT_STALE_MIN = 10        # holdout: a captured chain older than this is not a price
STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "NIFTYNXT50": 100}


class EngineError(ValueError):
    pass


# ---------------------------------------------------------------- plumbing

def _L():
    return O.ENV["lab"]


def _S():
    return O._S()


def _V():
    return O.vol_api()


def _E():
    return O.events_api()


def _arch(data):
    a = (data or {}).get("archive") or O.ENV.get("archive")
    if a is None:
        raise EngineError("the options engines read the local archive: put the trader-archive folder next to "
                          "trader-lab")
    return a


def _reg(data):
    return (data or {}).get("regime") or O.ENV.get("regime")


def _d(s):
    return datetime.date.fromisoformat(s)


def _minus(day, n):
    return (_d(day) - datetime.timedelta(days=n)).isoformat()


def _say(data, text):
    fn = (data or {}).get("progress")
    if fn:
        fn(text)


def families():
    return FAMILIES


def engine(family):
    """The engine function for a hypothesis family (EngineError for one this package does not run)."""
    fn = ENGINES.get(family)
    if fn is None:
        raise EngineError("no options engine for family %r (options engines: %s)" % (family, ", ".join(FAMILIES)))
    return fn


def run(hyp, params, window, data, cost_mult=1.0):
    """engine(hyp["family"])(hyp, params, window, data, cost_mult)."""
    return engine(hyp.get("family"))(hyp, params, window, data, cost_mult)


def grid(hyp):
    """Every registered grid point, in the file's parameter order: [{name: value}] (len == trial_budget)."""
    names = list(hyp.get("parameters") or {})
    return [dict(zip(names, vals)) for vals in itertools.product(*[hyp["parameters"][n] for n in names])]


def check_params(hyp, params):
    """The grid point, checked against the registration."""
    reg = hyp.get("parameters") or {}
    params = dict(params or {})
    missing = [k for k in reg if k not in params]
    extra = [k for k in params if k not in reg]
    if missing or extra:
        raise EngineError("%s: params must be exactly %s (missing %s, unknown %s)"
                          % (hyp.get("id"), sorted(reg), missing, extra))
    for k, v in params.items():
        if v not in reg[k]:
            raise EngineError("%s: %s=%r is not a registered value %s (a new value is a new hypothesis)"
                              % (hyp.get("id"), k, v, reg[k]))
    return params


def window_of(hyp, window):
    if isinstance(window, str):
        sp = (hyp.get("span") or {}).get(window)
        if not sp:
            raise EngineError("%s has no %r window" % (hyp.get("id"), window))
        return {"name": window, "from": sp["from"], "to": sp["to"]}
    if not isinstance(window, dict) or not window.get("from") or not window.get("to"):
        raise EngineError("window is train, validation, holdout or {\"name\", \"from\", \"to\"}")
    w = dict(window)
    w.setdefault("name", "custom")
    return w


def _underlying(hyp):
    text = str((hyp.get("instrument") or {}).get("underlying") or "NIFTY").split()[0].upper()
    return text if text in STEPS else "NIFTY"


def size_lots(capital, margin_per_lot, max_loss_per_lot, margin_cap_pct=MARGIN_CAP_PCT, tail_loss_pct=TAIL_LOSS_PCT):
    """Lots by margin and tail loss: the fewer of (capital x margin_cap_pct% / margin a lot) and (capital x
    tail_loss_pct% / the worst case a lot). 0 when not even one lot fits."""
    by_m = int(capital * margin_cap_pct / 100.0 // margin_per_lot) if margin_per_lot > 0 else 0
    by_t = int(capital * tail_loss_pct / 100.0 // max_loss_per_lot) if max_loss_per_lot > 0 else by_m
    return max(0, min(by_m, by_t))


def _compiled(under, structure, strikes, wing, lots, capital, rate, expiry="monthly", cache=None):
    k = (under, structure, str(strikes), wing, lots, expiry)
    if cache is not None and k in cache:
        return cache[k]
    doc = {"name": "edge-%s" % structure, "kind": "options", "underlying": under, "structure": structure,
           "expiry": expiry, "strikes": strikes, "entry": {"days_to_expiry": 0}, "exit": {},
           "lots": lots, "capital": max(10000, capital), "rate": rate}
    if wing:
        doc["wings"] = {"points": wing}
    spec = O.validate(doc)
    if cache is not None:
        cache[k] = spec
    return spec


def _wing_points(spot, pct, step):
    return max(step, round(spot * pct / 100.0 / step) * step)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _metrics(trades, days, capital, mtm=None):
    if mtm is None:
        mtm, run_, by = {}, 0.0, {}
        for t in trades:
            by[t["exit_date"]] = by.get(t["exit_date"], 0.0) + t["net"]
        for d in days:
            run_ += by.get(d, 0.0)
            mtm[d] = run_
    m = O.options_metrics(trades, mtm, days, capital) if days else {"positions": 0}
    rs = [t.get("r") for t in trades]
    prem = [100.0 * t["net"] / abs(t["premium"]) for t in trades if t.get("premium")]
    m["expectancy_r"] = round(_mean(rs), 5) if trades else None
    m["win_rate_pct"] = round(100.0 * sum(1 for t in trades if t["net"] > 0) / len(trades), 2) if trades else None
    m["mean_return_on_premium_pct"] = round(_mean(prem), 3) if prem else None
    m["net"] = round(sum(t["net"] for t in trades), 2)
    return m


def _result(hyp, eng, params, w, trades, metrics, skipped, notes, status="ok", model_priced=False, extra=None):
    pm = hyp.get("primary_metric")
    key = PRIMARY.get(pm)
    out = {"family": hyp.get("family"), "hypothesis": hyp.get("id"), "engine": eng, "params": params, "window": w,
           "status": status, "trades": trades, "n": len(trades), "metrics": metrics, "primary_metric": pm,
           "primary": metrics.get(key) if key else None, "skipped": skipped, "notes": notes,
           "flags": {"model_priced": model_priced,
                     "verdict_cap": ((hyp.get("gates") or {}).get("extra") or {}).get("verdict_cap")},
           "registration": {"id": hyp.get("id"), "status": hyp.get("status"), "trial_budget": hyp.get("trial_budget")}}
    out.update(extra or {})
    return out


def _skip(skipped, why):
    skipped[why] = skipped.get(why, 0) + 1


# ---------------------------------------------------------------- vrp-selling (eh-05)

def vrp_inputs(hyp, w, data):
    """(chains, index bars, vix bars, HAR model or None, notes) for a VRP run."""
    A = _arch(data)
    root = data.get("root")
    under = _underlying(hyp)
    notes = []
    chains = (data.get("chains") or {}).get(under)
    if chains is None:
        chains = O.load_chains(under, root, A, _minus(w["from"], 40), w["to"])
    idx = (data.get("index") or {}).get(under)
    if idx is None:
        idx = A.load_index(under, None, w["to"], root)
    vix = data.get("vix")
    if vix is None:
        vix = A.load_vix(None, w["to"], root)
    tr = (hyp.get("span") or {}).get("train") or {}
    model = None
    try:
        model = _V().har_fit([b for b in idx if b["date"] <= tr.get("to", "")], tr["from"], tr["to"], h=20)
        notes.append("HAR-RV fitted on the train window %s to %s (%d rows, R2 %.2f): a diagnostic, not the "
                     "registered signal" % (tr["from"], tr["to"], model["n"], model["r2"] or 0))
    except (_V().VolError, KeyError) as e:
        notes.append("no HAR-RV forecast: %s" % e)
    return chains, idx, vix, model, notes


def _monthly_expiries(chains):
    seen = set()
    for d in chains.days:
        seen.update(chains.boards[d]["monthly"])
    return sorted(seen)


def vrp_backtest(chains, days, signal, threshold, p, cost_mult, capital, rate, facts_of=None, cache=None):
    """One condor at a time, one per monthly cycle, on `days`. threshold None: untimed (every cycle).
    signal: {day: IV - RV}. Entry at the close; exit at expiry settlement, at 50% of the credit
    (take_50pct_profit), or at the last of `days` ("window end")."""
    under = chains.underlying
    monthly = _monthly_expiries(chains)
    trades, skipped, mtm = [], {}, {}
    pos, spec, used, realised = None, None, set(), 0.0
    last = days[-1] if days else None
    for day in days:
        unreal = 0.0
        if pos is not None:
            exp = pos["expiry"]
            settle = None
            if day >= exp:
                settle = chains.final_settlement(exp)[0] if day == exp else None
                if settle is None:
                    settle = chains.boards[day]["spot"]
                    pos["flags"].append("expiry day %s missing: settled at %s's price" % (exp, day))
            gross, prices, models = O._value(pos, chains, day, settle)
            spot = settle if settle is not None else chains.boards[day]["spot"]
            if O._breach(pos, spot):
                pos["breached"] = True
            reason = "expiry settlement" if settle is not None else None
            if reason is None and p["exit"] == "take_50pct_profit" and gross >= 0.5 * abs(pos["premium"]):
                reason = "profit target"
            if reason is None and day == last:
                reason = "window end"
            if reason:
                pos["models"] += models
                t = O.close_position(spec, pos, chains, day, reason, prices, cost_mult, settle is not None)
                t["facts"] = pos["facts"]
                t["sized_lots"] = pos.get("sized_lots")
                trades.append(t)
                realised += t["net"]
                pos = None
            else:
                for l, (px, src) in zip(pos["legs"], prices):
                    l["mark"] = px
                unreal = gross
        if pos is None and day != last:
            b = chains.boards[day]
            cands = [e for e in sorted(b["monthly"]) if (_d(e) - _d(day)).days >= MIN_DTE_MONTHLY]
            exp = cands[0] if cands else None
            if exp is None:
                mtm[day] = realised
                continue
            i = bisect.bisect_left(monthly, exp)
            prev_exp = monthly[i - 1] if i > 0 else None
            if exp in used or (prev_exp and day <= prev_exp):
                mtm[day] = realised            # one position a monthly cycle: wait for the previous expiry
                continue
            s = signal.get(day)
            if threshold is not None:
                if s is None:
                    _skip(skipped, "no India VIX or realised vol that day")
                    mtm[day] = realised
                    continue
                if not s > threshold:
                    _skip(skipped, "spread at or under the threshold")
                    mtm[day] = realised
                    continue
            a = chains.analysis(day, exp, rate)
            if a is None:
                _skip(skipped, "no chain for the expiry")
                mtm[day] = realised
                continue
            step = _S().strike_step(a)
            wing = _wing_points(b["spot"], p["wing_width_pct_of_spot"], step)
            strikes = {"by": "delta", "value": p["short_delta"]}
            spec = _compiled(under, "iron-condor", strikes, wing, 1, capital, rate, cache=cache)
            pos, why = O.open_position(spec, chains, day, exp, cost_mult)
            if pos is None:
                _skip(skipped, why)
                mtm[day] = realised
                continue
            lot = pos["lot_size"] or 1
            credit_unit = pos["premium"] / float(lot)
            m = O.margin_for(pos["legs"], "index" if spec["index"] else "stock", day, exp, True)["required"]
            worst = max(0.0, wing - credit_unit) * lot
            lots = size_lots(capital, m, worst)
            pos["sized_lots"] = lots
            if lots > 1:
                spec = _compiled(under, "iron-condor", strikes, wing, min(lots, 50), capital, rate, cache=cache)
                pos, why = O.open_position(spec, chains, day, exp, cost_mult)
                pos["sized_lots"] = lots
            pos["facts"] = dict(facts_of(day) if facts_of else {}, spread=None if s is None else round(s, 3),
                                wing_points=wing, margin_per_lot=round(m, 2), worst_case_per_lot=round(worst, 2),
                                sized_lots=lots)
            if lots == 0:
                pos["flags"].append("capital under the 1%% tail-loss rule for one lot (worst case %.0f a lot): "
                                    "booked at one lot" % worst)
            used.add(exp)
        mtm[day] = realised + unreal
    return trades, skipped, mtm


def _event_split(trades, w, data):
    """The positions held through an RBI policy or Union Budget day against the rest (a diagnostic: eh-05
    registered no event filter, so none is applied). Each trade gets facts["events_in_hold"]."""
    R = _reg(data)
    evs = R.builtin_events() if R is not None and hasattr(R, "builtin_events") else []
    evs = list(evs) + list((data or {}).get("events") or [])
    cal = _E().calendar(w["from"], w["to"], evs)
    out = {"held_through_event": {"n": 0, "net": 0.0}, "clear": {"n": 0, "net": 0.0}}
    for t in trades:
        hit = ["%s %s" % (k, d) for d, ks in sorted(cal.items()) for k in ks if t["entry_date"] < d <= t["exit_date"]]
        t.setdefault("facts", {})["events_in_hold"] = hit
        b = out["held_through_event" if hit else "clear"]
        b["n"] += 1
        b["net"] = round(b["net"] + t["net"], 2)
    return out


def run_vrp(hyp, params, window, data, cost_mult=1.0):
    """eh-05: a monthly NIFTY iron condor sold only when India VIX - the 20-session close-to-close realised
    vol is above the registered threshold. The untimed condor (same strikes, every cycle) rides along as the
    baseline its holdout rule compares with."""
    p = check_params(hyp, params)
    w = window_of(hyp, window)
    data = data or {}
    V = _V()
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    rate = float(data.get("rate", 0.0557))
    chains, idx, vix, model, notes = vrp_inputs(hyp, w, data)
    days = [d for d in chains.days if w["from"] <= d <= w["to"]]
    if not days:
        return _result(hyp, "vrp-condor", p, w, [], {"positions": 0}, {}, notes + ["no EOD chains in the window"],
                       status="no data")
    iv = V.vix_iv(vix)
    rv = V.realised_vol(idx, 20, "close")
    sig = V.spread(iv, rv)
    har = V.har_forecast(model, idx) if model else {}
    atm = chains.iv_series(30, rate)

    def facts(day):
        return {"vix": iv.get(day), "rv20": None if rv.get(day) is None else round(rv[day], 3),
                "har_rv": None if har.get(day) is None else round(har[day], 3),
                "atm_iv30": None if atm.get(day) is None else round(atm[day], 3)}
    cache = {}
    th = float(p["vrp_threshold_vol_points"])
    trades, skipped, mtm = vrp_backtest(chains, days, sig, th, p, cost_mult, capital, rate, facts, cache)
    base_t, base_s, base_m = vrp_backtest(chains, days, sig, None, p, cost_mult, capital, rate, facts, cache)
    met = _metrics(trades, days, capital, mtm)
    credits = [t["premium"] for t in trades if t.get("credit")]
    avg_c = _mean(credits)
    met["worst_loss_avg_credits"] = round(-min(t["net"] for t in trades) / avg_c, 3) if trades and avg_c else None
    bm = _metrics(base_t, days, capital, base_m)
    notes.append("signal: India VIX - 20-session close-to-close realised vol (the registered rule), entry at the "
                 "close on the first session of a cycle with the spread above %g vol points" % th)
    notes.append("strikes: %g-delta shorts on Black-76 IV from settle prices; wings %g%% of spot" %
                 (p["short_delta"], p["wing_width_pct_of_spot"]))
    beat = None
    if met.get("return_on_margin_pct") is not None and bm.get("return_on_margin_pct") is not None:
        beat = met["return_on_margin_pct"] > bm["return_on_margin_pct"]
    met["beats_untimed"] = beat
    met["by_events"] = _event_split(trades, w, data)
    return _result(hyp, "vrp-condor", p, w, trades, met, skipped, notes,
                   model_priced=met.get("model_priced_marks", 0) > 0,
                   extra={"baseline": {"label": "the same condor sold every cycle (untimed)", "n": len(base_t),
                                       "metrics": bm, "skipped": base_s},
                          "har": None if model is None else {k: model[k] for k in ("coef", "n", "r2", "h", "train")}})


# ---------------------------------------------------------------- event-vol (eh-08, eh-09)

def fno_stocks(A, root, start, end):
    """The stocks with options in the archive's F&O bhavcopies between start and end (point in time: a stock
    counts only on the sessions it had options)."""
    out = set()
    for d in A.trading_days(start, end, root=root):
        p = A.Store(A.default_root(root)).path("fo", d)
        try:
            lines = A._gz_lines(p)
        except OSError:
            continue
        for l in lines[1:]:
            f = l.split(",", 2)
            if len(f) > 1 and f[1] == "STO":
                out.add(f[0])
    return sorted(out)


def _chains_for(A, root, sym, days, rate):
    boards = {}
    for d in days:
        ch = A.load_fo_chain_eod(sym, d, root=root)
        if ch and ch.get("options"):
            boards[d] = O.trim_board(ch, band=0.3)
    return O.Chains(sym, boards, None, None, "archive (%d EOD chains)" % len(boards)) if boards else None


def _closes(A, root, sym, start, end):
    try:
        bars = A.load_daily(sym, start, end, adjusted=False, root=root)
    except Exception:  # noqa: BLE001 - an archive without equity files
        bars = []
    return {b["date"]: b["c"] for b in bars if b.get("c")}


def atm_iv_panel(A, root, sym, days, rate):
    """{day: the near-monthly ATM IV (%) of one stock} (the expiry at least 7 days out), from the EOD chains."""
    out = {}
    for d in days:
        ch = A.load_fo_chain_eod(sym, d, root=root)
        if not ch or not ch.get("options"):
            continue
        c = O.Chains(sym, {d: O.trim_board(ch, band=0.1)})
        b = c.boards[d]
        exps = [e for e in sorted(b["monthly"] or b["expiries"]) if (_d(e) - _d(d)).days >= 7]
        if exps:
            v = c.atm_iv(d, exps[0], rate)
            if v:
                out[d] = v
    return out


def _percentile(hist, cur):
    R = O.ENV.get("regime")
    if R is not None and hasattr(R, "percentile_rank"):
        return R.percentile_rank(hist, cur)
    return 100.0 * sum(1 for x in hist if x < cur) / len(hist) if hist else None


def run_event_vol(hyp, params, window, data, cost_mult=1.0):
    """eh-08 (short iron fly over results) or eh-09 (long straddle into results), by the registration's side."""
    side = str((hyp.get("instrument") or {}).get("side") or "")
    return _event_run(hyp, params, window, data, cost_mult, short=side.startswith("short"))


def _event_run(hyp, params, window, data, cost_mult, short):
    p = check_params(hyp, params)
    w = window_of(hyp, window)
    data = data or {}
    A = _arch(data)
    E = _E()
    root = data.get("root")
    rate = float(data.get("rate", 0.0557))
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    min_hist = int(data.get("min_history", MIN_HISTORY))
    sessions = A.trading_days(root=root)
    days = [d for d in sessions if w["from"] <= d <= w["to"]]
    symbols = data.get("symbols") or fno_stocks(A, root, w["from"], w["to"])
    trades, skipped, notes, study = [], {}, [], []
    cache = {}
    eng = "event-crush-iron-fly" if short else "event-runup-straddle"
    for si, sym in enumerate(symbols):
        if si % 20 == 19:
            _say(data, "%s: %d of %d stocks" % (hyp.get("id"), si + 1, len(symbols)))
        evs = (data.get("results") or {}).get(sym)
        if evs is None:
            evs = A.results_dates(sym, None, w["to"], root=root)
        if not evs:
            continue
        closes = _closes(A, root, sym, None, w["to"])
        ivp = None
        for ev in evs:
            win = E.event_windows(ev, sessions)
            if win is None:
                continue
            i = win["i_event"]
            if short:
                entry = win["entry"]
                j = i + int(p["exit_session"]) - 1
                if j >= len(sessions):
                    continue
                exit_ = sessions[j]
            else:
                k = i - int(p["entry_sessions_before"])
                if k < 0:
                    continue
                entry, exit_ = sessions[k], win["entry"]
            if not (w["from"] <= entry and exit_ <= w["to"]):
                continue
            span = sessions[sessions.index(entry):sessions.index(max(exit_, win["move_end"])) + 1]
            ch = _chains_for(A, root, sym, span, rate)
            if ch is None or ch.board(entry) is None:
                _skip(skipped, "no options on the entry session (not in the F&O list then)")
                continue
            if ch.board(exit_) is None:
                _skip(skipped, "no chain on the exit session")
                continue
            for d in span:                     # equity closes, else the bhavcopy's underlying price
                if d not in closes and ch.board(d):
                    closes[d] = ch.board(d)["spot"]
            im = E.implied_move(ch, entry, exit_, rate)
            rm = E.realised_move(closes, win["entry"], win["move_end"])
            hist = E.history_moves(evs, closes, sessions, entry)
            row = {"symbol": sym, "date": ev["date"], "event_session": win["event_session"],
                   "time_known": win["time_known"], "implied_move_pct": im and round(im["move_pct"], 4),
                   "realised_move_pct": None if rm is None else round(rm, 4),
                   "history_mean_pct": round(_mean(hist), 4) if hist else None, "history_n": len(hist)}
            row["realised_above_implied"] = (rm > im["move_pct"]) if im and rm is not None else None
            study.append(row)
            if im is None:
                _skip(skipped, "no ATM straddle on the entry session")
                continue
            exp = im["expiry"]
            if short:
                if len(hist) < min_hist:
                    _skip(skipped, "fewer than %d earlier results moves" % min_hist)
                    continue
                ratio = im["move_pct"] / _mean(hist) if _mean(hist) else None
                row["ratio_to_history"] = ratio and round(ratio, 4)
                if ratio is None or ratio < float(p["min_event_iv_ratio"]):
                    _skip(skipped, "implied move under %g x the stock's past results moves" % p["min_event_iv_ratio"])
                    continue
                a = ch.analysis(entry, exp, rate)
                wing = _wing_points(a["spot"], p["wing_pct_of_spot"], _S().strike_step(a))
                spec = _compiled(sym, "iron-fly", {"by": "atm"}, wing, 1, capital, rate, cache=cache)
            else:
                cap = float(p["iv_percentile_cap"])
                if cap < 100:
                    if ivp is None:
                        lo = bisect.bisect_left(sessions, w["from"])
                        ivp = atm_iv_panel(A, root, sym, sessions[max(0, lo - IV_HISTORY - 5):
                                                                  bisect.bisect_right(sessions, w["to"])], rate)
                    k0 = bisect.bisect_left(sessions, entry)
                    hist_iv = [ivp[d] for d in sessions[max(0, k0 - IV_HISTORY):k0] if d in ivp]
                    cur = ivp.get(entry)
                    if cur is None or len(hist_iv) < IV_HISTORY_MIN:
                        _skip(skipped, "under %d sessions of ATM IV history" % IV_HISTORY_MIN)
                        continue
                    pct = _percentile(hist_iv, cur)
                    row["iv_percentile"] = round(pct, 1)
                    if pct > cap:
                        _skip(skipped, "ATM IV percentile over the cap")
                        continue
                spec = _compiled(sym, "long-straddle", {"by": "atm"}, None, 1, capital, rate, cache=cache)
            pos, why = O.open_position(spec, ch, entry, exp, cost_mult)
            if pos is None:
                _skip(skipped, why)
                continue
            if any(l["price_source"] != "settle" for l in pos["legs"]):
                _skip(skipped, "a strike did not trade on the entry session")
                continue
            gross, prices, models = O._value(pos, ch, exit_)
            spot_x = ch.board(exit_)["spot"]
            pos["breached"] = O._breach(pos, spot_x) if short else False
            pos["models"] += models
            t = O.close_position(spec, pos, ch, exit_, "exit session" if short else "close before results",
                                 prices, cost_mult, False)
            t["facts"] = dict(row)
            if not short:
                t["attribution"] = _attribution(pos, ch, exit_, rate, t)
            if not win["time_known"]:
                t["flags"] = (t.get("flags") or []) + ["results time unknown: the reaction may be on %s or the "
                                                       "session after" % win["event_session"]]
            trades.append(t)
    trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    met = _metrics(trades, days, capital)
    met["event_study"] = E.summary(study)
    met["events_traded"] = len(trades)
    if not short and trades:
        met["pnl_from_iv_change"] = round(sum(t["attribution"]["iv_change"] for t in trades), 2)
        met["pnl_from_move_and_time"] = round(sum(t["attribution"]["move_and_time"] for t in trades), 2)
    notes.append("results events from trader-archive's results dataset; the reacting session re-derived from "
                 "the archive's sessions; implied move = ATM straddle / spot at the entry close on the nearest "
                 "monthly expiry alive after the exit")
    notes.append("EOD settle prices: a real fill at 15:29 on a thin stock-option strike can be far worse than "
                 "the slippage model")
    return _result(hyp, eng, p, w, trades, met, skipped, notes, extra={"event_study": study})


def _attribution(pos, ch, day, rate, t):
    """A long straddle's gross split: the move and the time (each leg repriced at the exit with its ENTRY IV)
    and the rest, the change in implied volatility (the thing eh-09 is about)."""
    S = _S()
    move_time = 0.0
    for l in pos["legs"]:
        a = ch.analysis(day, l["expiry"], rate)
        iv = l.get("iv")
        if a is None or not iv:
            return {"iv_change": 0.0, "move_and_time": t["gross"], "note": "no IV to split by"}
        px = S.black76_price(a["forward"], l["strike"], a["t_years"], rate, iv / 100.0, l["option_type"])
        move_time += O._sign(l) * (px - l["fill"]) * l["qty"]
    return {"move_and_time": round(move_time, 2), "iv_change": round(t["gross"] - move_time, 2)}


# ---------------------------------------------------------------- option-buying (eh-14)

def _hm(t):
    return int(t[:2]) * 60 + int(t[3:5])


def _hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def _years_to(day, minute, expiry):
    """Calendar years from `day` at `minute` (IST) to 15:30 on the expiry day."""
    mins = (_d(expiry) - _d(day)).days * 1440 + (15 * 60 + 30 - minute)
    return max(mins, 1) / 525600.0


def model_price(spot, strike, day, minute, expiry, sigma_pct, rate, kind):
    """Black-76 on the index (F = S e^(rT)) at a bar's end, the IV fixed for the session."""
    T = _years_to(day, minute, expiry)
    F = spot * math.exp(rate * T)
    return _S().black76_price(F, strike, T, rate, sigma_pct / 100.0, kind)


def snapshot_quote(A, root, under, day, minute, expiry, strike, kind, side):
    """(price, time) from the captured chain (archive snapshot-chain / capture-chains) at or before `minute`
    on `day`, no older than SNAPSHOT_STALE_MIN: the ask to buy, the bid to sell (last price when a side is
    empty). (None, why) when nothing usable was captured."""
    snaps = [s for s in A.chain_snapshots(under, day, root) if s["expiry"] == expiry and _hm(s["time"]) <= minute]
    if not snaps:
        return None, "no captured chain"
    s = snaps[-1]
    if minute - _hm(s["time"]) > SNAPSHOT_STALE_MIN:
        return None, "captured chain stale"
    doc = A.load_chain_snapshot(s["path"])
    recs = ((doc.get("data") or doc).get("records") or {}).get("data") or []
    for r in recs:
        if float(r.get("strikePrice") or -1) == float(strike) and r.get(kind):
            q = r[kind]
            px = q.get("sellPrice1") if side == "buy" else q.get("buyPrice1")
            px = px or q.get("lastPrice")
            return (float(px), s["time"]) if px else (None, "no quote at the strike")
    return None, "strike not in the captured chain"


def _captured(A, root, under, w):
    """True when the archive holds any captured chain of `under` inside the window."""
    base = os.path.join(A.default_root(root), "chain", under.upper())
    try:
        return any(w["from"] <= d <= w["to"] and A.chain_snapshots(under, d, root) for d in os.listdir(base))
    except OSError:
        return False


def _sessions_bars(bars):
    by = {}
    for b in bars:
        by.setdefault(b["date"], []).append(b)
    for v in by.values():
        v.sort(key=lambda b: b["time"])
    return by


def _interval(day_bars):
    if len(day_bars) < 2:
        return 1
    return max(1, _hm(day_bars[1]["time"]) - _hm(day_bars[0]["time"]))


def run_option_buying(hyp, params, window, data, cost_mult=1.0):
    """eh-14: on a session whose previous India VIX close is under the cap, buy the nearest weekly ATM call
    (put) when a bar closes above (below) the opening range by min_breakout_pct; out by 15:15 or on a 30%
    trail from the peak. Train and validation are MODEL-PRICED (Black-76 on index bars, the previous
    session's EOD ATM IV of that expiry, else India VIX); the holdout prices from captured chains only, so
    it waits for #76's capture and its verdict is capped at WATCH."""
    p = check_params(hyp, params)
    w = window_of(hyp, window)
    data = data or {}
    A = _arch(data)
    root = data.get("root")
    under = _underlying(hyp)
    rate = float(data.get("rate", 0.0557))
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    L = _L()
    live = w["name"] == "holdout"
    notes, skipped, trades = [], {}, []
    bars = (data.get("intraday") or {}).get(under)
    if bars is None:
        bars = A.load_intraday(under, "1m", w["from"], w["to"], root) or A.load_intraday(under, "5m", w["from"],
                                                                                          w["to"], root)
    bars = [b for b in bars if w["from"] <= b["date"] <= w["to"]]
    vix = data.get("vix")
    if vix is None:
        vix = A.load_vix(_minus(w["from"], 10), w["to"], root)
    vixc = {b["date"]: b["c"] for b in vix if b.get("c")}
    chains = (data.get("chains") or {}).get(under)
    if chains is None:
        try:
            chains = O.load_chains(under, root, A, _minus(w["from"], 10), w["to"])
        except Exception as e:  # noqa: BLE001 - no EOD chains: IV from India VIX, expiries from the rules
            notes.append("no EOD chains (%s): IV from India VIX, expiries from trader-regime's rules" % e)
            chains = None
    by = _sessions_bars(bars)
    days = sorted(by)
    if live and not _captured(A, root, under, w):
        return _result(hyp, "trend-day-buying", p, w, [], {"positions": 0}, {},
                       notes + ["the holdout is priced from captured option chains only (archive.py "
                                "capture-chains, #76) and none exist in the window yet; the verdict is capped at "
                                "WATCH whatever it shows"], status="waiting for captured chains")
    if not days:
        return _result(hyp, "trend-day-buying", p, w, [], {"positions": 0}, {},
                       notes + ["no %s intraday bars in the window" % under], status="no data", model_priced=not live)
    known = sorted(set(vixc) | set(chains.days if chains else []))
    R = _reg(data)
    cfg = {"fno_charges": {}}
    S = _S()
    orm, pct, cap = int(p["opening_range_minutes"]), float(p["min_breakout_pct"]), float(p["vix_cap"])
    for day in days:
        db = by[day]
        k = bisect.bisect_left(known, day)
        prev = known[k - 1] if k > 0 else None
        v = vixc.get(prev) if prev else None
        if v is None:
            _skip(skipped, "no India VIX close the session before")
            continue
        if not v < cap:
            _skip(skipped, "India VIX at or over the cap")
            continue
        iv_ = _interval(db)
        start = _hm("09:15")
        orb = [b for b in db if _hm(b["time"]) < start + orm]
        if len(orb) < orm // iv_:
            _skip(skipped, "opening range incomplete")
            continue
        hi, lo = max(b["h"] for b in orb), min(b["l"] for b in orb)
        sig = None
        for j, b in enumerate(db):
            end = _hm(b["time"]) + iv_
            if _hm(b["time"]) < start + orm or end > _hm(LAST_ENTRY):
                continue
            if b["c"] > hi * (1 + pct / 100.0):
                sig = (j, "CE", b, end)
                break
            if b["c"] < lo * (1 - pct / 100.0):
                sig = (j, "PE", b, end)
                break
        if sig is None:
            _skip(skipped, "no opening-range break by %s" % LAST_ENTRY)
            continue
        j, kind, b, end = sig
        expiry, lot, sigma, src = None, None, None, None
        if chains is not None and prev in chains.boards:
            exps = [e for e in chains.boards[prev]["expiries"] if e >= day]
            expiry = exps[0] if exps else None
            if expiry:
                sigma = chains.atm_iv(prev, expiry, rate)
                a = chains.analysis(prev, expiry, rate)
                lot = a and a.get("lot_size")
                src = "the previous session's EOD ATM IV" if sigma else None
        if expiry is None and R is not None:
            expiry = R.expiry_info(under, day).get("next_expiry")
        if expiry is None:
            _skip(skipped, "no expiry known")
            continue
        if not sigma:
            sigma, src = v, "India VIX (previous close)"
        lot = lot or int(data.get("lot") or 65)
        step = STEPS.get(under, 50)
        K = round(b["c"] / step) * step
        flat = _hm(FLAT_BY)
        if live:
            px_in, why = snapshot_quote(A, root, under, day, end, expiry, K, kind, "buy")
            if px_in is None:
                _skip(skipped, why)
                continue
            fill_in = px_in
        else:
            px_in = model_price(b["c"], K, day, end, expiry, sigma, rate, kind)
            fill_in = px_in + O.slip(px_in, O.OPTION_COSTS, cost_mult)
        peak = fill_in
        exit_px, exit_t, reason = None, None, None
        for x in db[j + 1:]:
            t0 = _hm(x["time"])
            if t0 >= flat:
                exit_t, reason = flat, "15:15"
                spot = x["o"]
                break
            te = t0 + iv_
            if live:
                mark, _ = snapshot_quote(A, root, under, day, te, expiry, K, kind, "sell")
            else:
                mark = model_price(x["c"], K, day, te, expiry, sigma, rate, kind)
            if mark is None:
                continue
            peak = max(peak, mark)
            if p["exit"] == "trail_30pct_from_peak" and mark <= (1 - TRAIL) * peak:
                exit_px, exit_t, reason = mark, te, "trail 30% from the peak"
                break
        else:
            last = db[-1]
            exit_t, reason, spot = _hm(last["time"]) + iv_, "last bar", last["c"]
        if exit_px is None:
            if live:
                q, why = snapshot_quote(A, root, under, day, exit_t, expiry, K, kind, "sell")
                if q is None:
                    _skip(skipped, "no captured quote to exit: %s" % why)
                    continue
                exit_px = q
            else:
                exit_px = model_price(spot, K, day, exit_t, expiry, sigma, rate, kind)
        fill_out = exit_px if live else max(O.TICK, exit_px - O.slip(exit_px, O.OPTION_COSTS, cost_mult))
        qty = lot
        ch = L.fno_round_trip_charges(fill_in, fill_out, qty, cfg, "OPT", day)["total"] * cost_mult
        gross = (fill_out - fill_in) * qty
        net = gross - ch
        risk = fill_in * qty
        trades.append({"symbol": under, "signal_date": day, "entry_date": day, "entry_time": _hhmm(end),
                       "exit_date": day, "exit_time": _hhmm(exit_t), "expiry": expiry, "reason": reason,
                       "structure": "long-%s" % ("call" if kind == "CE" else "put"), "strike": K,
                       "option_type": kind, "lots": 1, "lot_size": lot, "premium": round(-risk, 2), "credit": False,
                       "entry": round(fill_in, 2), "exit": round(fill_out, 2), "gross": L.round_paise(gross),
                       "charges": L.round_paise(ch), "net": L.round_paise(net), "margin": round(risk, 2),
                       "r": round(net / risk, 5) if risk else 0.0,
                       "return_on_margin_pct": round(100.0 * net / risk, 3) if risk else None,
                       "price_source": "captured chain" if live else "model (Black-76)",
                       "iv": round(sigma, 2), "iv_source": src, "vix_prev": v, "breached": False,
                       "model_marks": 0 if live else 1, "flags": [] if live else ["model-priced"]})
    met = _metrics(trades, days, capital)
    notes.append("signal on %s bars: opening range %d min, break %g%%, India VIX (previous close) under %g, "
                 "entries until %s, out by %s" % (under, orm, pct, cap, LAST_ENTRY, FLAT_BY))
    if not live:
        notes.append("model-priced: Black-76 with the IV held for the session; intraday IV moves, the bid-ask "
                     "spread and the expiry-day smile are not in these prices. A model-priced pass can only send "
                     "the idea to the live-captured holdout, and that can only reach WATCH")
    return _result(hyp, "trend-day-buying", p, w, trades, met, skipped, notes, model_priced=not live)


ENGINES = {"vrp-selling": run_vrp, "event-vol": run_event_vol, "option-buying": run_option_buying}
