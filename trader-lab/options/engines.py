"""The options engines behind the edge-hunt executor's option families. Standard library only.

trader-lab/engines/jobs.py's OptionsJob is the only caller: the executor's Job interface is the one contract
(blockers, load(until), run(params, a, b, m) -> the v2 run shape of engines/core.py). This module supplies,
per family:

    prepare(hyp, data, start, until) -> sessions
        read what the family needs from the archive, up to `until` and never later, into `data` (the
        job's cache); EngineError when the data is not there (the job reports it as a blocker)
    engine(family)(hyp, params, a, b, data, cost_mult=1.0) -> run
        one registered grid point on the sessions a..b

    families   "vrp-selling" (eh-05), "event-vol" (eh-08 short iron fly, eh-09 long straddle, by the
               registration's side), "option-buying" (eh-14)
    hyp        the registered hypothesis, read as is (never edited here)
    params     one grid point: every name in hyp["parameters"], each value one of the registered values
               (anything else is refused: a new value is a new hypothesis)
    a, b       the window (ISO dates): signals only inside it, every position closed by its last session
               ("window end", purged). Models (HAR-RV) are fitted on hyp["span"]["train"] only. A window
               starting on or after the registered holdout's first day is the holdout: eh-14 then prices
               from captured chains only.
    data       {"root": the trader-archive folder, "archive": trader-archive's module, "regime":
               trader-regime's module, "capital": rupees, "rate": 0.0557, "until": the last day loaded,
               "symbols": [stocks] (event-vol; default the stocks with options in the archive),
               "min_history": earlier results moves needed (event-vol, default 4)} plus prepare()'s caches
    run        {"days", "ret", "bench": None, "equity", "exposure", "trades", "traded", "charges", "notes",
                "extra", "capital"}; each trade carries the lab's options trade fields (net, r, margin,
                return_on_margin_pct, legs...) and "ret" (net over the margin for sellers, over the premium
                paid for buyers; the executor's expectancy) and, for buyers, "abnormal" (the return on the
                premium: eh-09's event return). extra: "return_on_margin_pct" (eh-05, eh-08's primary),
                "options" (options_metrics + expectancy_r, win rate, mean return on premium, the family's own:
                the untimed baseline, the event study, the IV/move split), "skipped", "status",
                "model_priced", "verdict_cap", "engine"

Nothing here places an order, reads the network or writes a file.
Education and tooling, not financial advice.
"""

import bisect
import csv
import datetime
import itertools
import math
import os

O = None          # the options package, set by its loader (options.engines_api())

FAMILIES = ("vrp-selling", "event-vol", "option-buying")
DEFAULT_CAPITAL = 1000000.0
MIN_DTE_MONTHLY = 21           # eh-05: the monthly expiry at least 21 calendar days away
MARGIN_CAP_PCT = 50.0          # sizing: at most half the capital blocked as margin
TAIL_LOSS_PCT = 1.0            # sizing: the defined-risk worst case of a position at most 1% of capital
MIN_HISTORY = 4                # event-vol: earlier results moves of the stock needed for the ratio
IV_HISTORY = 252               # event-vol: sessions of ATM IV behind the IV percentile
IV_HISTORY_MIN = 126
EVENT_BAND = 0.12              # event-vol: strikes within 12% of the spot (the 8% wings fit)
EVENT_DTE = 70                 # event-vol: expiries within 70 days (stock options are monthly)
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


def engine(family):
    """The engine function for an option family (EngineError for one this package does not run)."""
    fn = ENGINES.get(family)
    if fn is None:
        raise EngineError("no options engine for family %r (options engines: %s)" % (family, ", ".join(FAMILIES)))
    return fn


def run(hyp, params, a, b, data, cost_mult=1.0):
    """engine(hyp["family"])(hyp, params, a, b, data, cost_mult)."""
    return engine(hyp.get("family"))(hyp, params, a, b, data, cost_mult)


def prepare(hyp, data, start, until):
    """Read the family's data up to `until` into `data`; returns the sessions it covers (EngineError when
    there is none)."""
    fam = hyp.get("family")
    if fam not in PREPARE:
        raise EngineError("no options engine for family %r" % fam)
    data["until"] = until
    return PREPARE[fam](hyp, data, start, until)


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


def _window(hyp, a, b, data):
    until = (data or {}).get("until")
    if until and b > until:
        b = until                     # never past what was loaded (the executor loads to validation's end)
    if a > b:
        raise EngineError("the window %s to %s is empty" % (a, b))
    ho = ((hyp.get("span") or {}).get("holdout") or {}).get("from")
    return {"from": a, "to": b, "holdout": bool(ho) and a >= ho}


def _underlying(hyp):
    text = str((hyp.get("instrument") or {}).get("underlying") or "NIFTY").split()[0].upper()
    return text if text in STEPS else "NIFTY"


def size_lots(capital, margin_per_lot, max_loss_per_lot, margin_cap_pct=MARGIN_CAP_PCT, tail_loss_pct=TAIL_LOSS_PCT):
    """Lots by margin and tail loss: the fewer of (capital x margin_cap_pct% / margin a lot) and (capital x
    tail_loss_pct% / the worst case a lot). 0 when not even one lot fits."""
    by_m = int(capital * margin_cap_pct / 100.0 // margin_per_lot) if margin_per_lot > 0 else 0
    by_t = int(capital * tail_loss_pct / 100.0 // max_loss_per_lot) if max_loss_per_lot > 0 else by_m
    return max(0, min(by_m, by_t))


def _compiled(under, structure, strikes, wing, lots, capital, rate, cache, expiry="monthly"):
    k = (under, structure, str(strikes), wing, lots, expiry)
    if k not in cache:
        doc = {"name": "edge-%s" % structure, "kind": "options", "underlying": under, "structure": structure,
               "expiry": expiry, "strikes": strikes, "entry": {"days_to_expiry": 0}, "exit": {},
               "lots": lots, "capital": max(10000, capital), "rate": rate}
        if wing:
            doc["wings"] = {"points": wing}
        cache[k] = O.validate(doc)
    return cache[k]


def _wing_points(spot, pct, step):
    return max(step, round(spot * pct / 100.0 / step) * step)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _skip(skipped, why):
    skipped[why] = skipped.get(why, 0) + 1


def _metrics(trades, days, capital, mtm):
    m = O.options_metrics(trades, mtm, days, capital) if days else {"positions": 0}
    prem = [100.0 * t["net"] / abs(t["premium"]) for t in trades if t.get("premium")]
    m["expectancy_r"] = round(_mean([t.get("r") for t in trades]), 5) if trades else None
    m["win_rate_pct"] = round(100.0 * sum(1 for t in trades if t["net"] > 0) / len(trades), 2) if trades else None
    m["mean_return_on_premium_pct"] = round(_mean(prem), 3) if prem else None
    m["net"] = round(sum(t["net"] for t in trades), 2)
    return m


def _realised_mtm(trades, days):
    by, out, run_ = {}, {}, 0.0
    for t in trades:
        by[t["exit_date"]] = by.get(t["exit_date"], 0.0) + t["net"]
    for d in days:
        run_ += by.get(d, 0.0)
        out[d] = run_
    return out


def _run_shape(hyp, eng, days, capital, trades, metrics, mtm, skipped, notes, status="ok", model_priced=False,
               extra=None):
    """The v2 run shape (engines/core.py) from the options results: daily P&L from the MTM curve (or
    realised P&L on exit days), exposure = the margin blocked over the equity."""
    eq, prev, rets, equity, expo = capital, 0.0, [], [], []
    for d in days:
        cur = mtm.get(d, prev)
        p = cur - prev
        rets.append(p / eq if eq > 0 else 0.0)
        eq += p
        equity.append(eq)
        prev = cur
        held = sum(t.get("margin") or 0.0 for t in trades if t["entry_date"] <= d < t["exit_date"])
        expo.append(held / eq if eq > 0 else 0.0)
    traded = 0.0
    for t in trades:
        legs = t.get("legs") or [{"qty": t.get("lot_size", 1) * t.get("lots", 1), "entry": t.get("entry", 0.0),
                                  "exit": t.get("exit", 0.0)}]
        traded += sum(abs(l["qty"]) * (abs(l.get("entry") or 0.0) + abs(l.get("exit") or 0.0)) for l in legs)
    vc = ((hyp.get("gates") or {}).get("extra") or {}).get("verdict_cap")
    ex = {"engine": eng, "status": status, "model_priced": model_priced, "verdict_cap": vc,
          "return_on_margin_pct": metrics.get("return_on_margin_pct"), "options": metrics, "skipped": skipped,
          "margin_model": getattr(_S(), "MARGIN_MODEL", None)}
    ex.update(extra or {})
    return {"days": list(days), "ret": rets, "bench": None, "equity": equity, "exposure": expo, "trades": trades,
            "traded": traded, "charges": round(sum(t.get("charges") or 0.0 for t in trades), 2), "notes": notes,
            "extra": ex, "capital": float(capital)}


def _seller_ret(t):
    t["ret"] = t["net"] / t["margin"] if t.get("margin") else 0.0
    return t


def _buyer_ret(t):
    paid = abs(t.get("premium") or 0.0)
    t["ret"] = t["net"] / paid if paid else 0.0
    t["abnormal"] = t["ret"]
    return t


# ---------------------------------------------------------------- vrp-selling (eh-05)

def prepare_vrp(hyp, data, start, until):
    A = _arch(data)
    root = data.get("root")
    under = _underlying(hyp)
    notes = data.setdefault("notes", [])
    try:
        chains = O.load_chains(under, root, A, start, until, band=0.12, keep_dte=63, monthly_only=True)
    except Exception:  # noqa: BLE001 - the lab's LabError: no chains in the archive
        raise EngineError("no %s option chains in the archive from %s to %s: the run waits for the F&O history "
                          "(archive.py backfill --from %s --datasets fo,index,vix,lots)" % (under, start, until, start))
    idx = A.load_index(under, start, until, root)
    vix = A.load_vix(start, until, root)
    if not idx or not vix:
        raise EngineError("no %s index closes or India VIX in the archive up to %s (backfill index,vix)" % (under, until))
    data.setdefault("chains", {})[under] = chains
    data.setdefault("index", {})[under] = idx
    data["vix"] = vix
    tr = (hyp.get("span") or {}).get("train") or {}
    V = _V()
    data["har"] = None
    try:
        data["har"] = V.har_fit([b for b in idx if b["date"] <= tr["to"]], tr["from"], tr["to"], h=20)
        notes.append("HAR-RV fitted on the train window %s to %s (%d rows, R2 %.2f): reported beside each "
                     "condor, not the registered signal" % (tr["from"], tr["to"], data["har"]["n"],
                                                             data["har"]["r2"] or 0))
    except (V.VolError, KeyError) as e:
        notes.append("no HAR-RV forecast: %s" % e)
    notes.append("%s EOD chains: %s" % (under, chains.source))
    return list(chains.days)


def _monthly_expiries(chains):
    seen = set()
    for d in chains.days:
        seen.update(chains.boards[d]["monthly"])
    return sorted(seen)


def vrp_backtest(chains, days, signal, threshold, p, cost_mult, capital, rate, facts_of=None, cache=None):
    """One condor at a time, one per monthly cycle, on `days`. threshold None: untimed (every cycle).
    signal: {day: IV - RV}. Entry at the close; exit at expiry settlement, at 50% of the credit
    (take_50pct_profit), or at the last of `days` ("window end"). -> (trades, skipped, mtm)"""
    cache = {} if cache is None else cache
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
                trades.append(_seller_ret(t))
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
            i = bisect.bisect_left(monthly, exp) if exp else 0
            prev_exp = monthly[i - 1] if exp and i > 0 else None
            if exp is None or exp in used or (prev_exp and day <= prev_exp):
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
            wing = _wing_points(b["spot"], p["wing_width_pct_of_spot"], _S().strike_step(a))
            strikes = {"by": "delta", "value": p["short_delta"]}
            spec = _compiled(under, "iron-condor", strikes, wing, 1, capital, rate, cache)
            pos, why = O.open_position(spec, chains, day, exp, cost_mult)
            if pos is None:
                _skip(skipped, why)
                mtm[day] = realised
                continue
            lot = pos["lot_size"] or 1
            m = O.margin_for(pos["legs"], "index" if spec["index"] else "stock", day, exp, True)["required"]
            worst = max(0.0, wing - pos["premium"] / float(lot)) * lot
            lots = size_lots(capital, m, worst)
            if lots > 1:
                spec = _compiled(under, "iron-condor", strikes, wing, min(lots, 50), capital, rate, cache)
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


def _event_split(trades, a, b, data):
    """The positions held through an RBI policy or Union Budget day against the rest (a diagnostic: eh-05
    registered no event filter, so none is applied). Each trade gets facts["events_in_hold"]."""
    R = _reg(data)
    evs = list(R.builtin_events() if R is not None and hasattr(R, "builtin_events") else [])
    evs += list((data or {}).get("events") or [])
    cal = _E().calendar(a, b, evs)
    out = {"held_through_event": {"n": 0, "net": 0.0}, "clear": {"n": 0, "net": 0.0}}
    for t in trades:
        hit = ["%s %s" % (k, d) for d, ks in sorted(cal.items()) for k in ks if t["entry_date"] < d <= t["exit_date"]]
        t.setdefault("facts", {})["events_in_hold"] = hit
        x = out["held_through_event" if hit else "clear"]
        x["n"] += 1
        x["net"] = round(x["net"] + t["net"], 2)
    return out


def run_vrp(hyp, params, a, b, data, cost_mult=1.0):
    """eh-05: a monthly NIFTY iron condor sold only when India VIX - the 20-session close-to-close realised
    vol is above the registered threshold. The untimed condor (same strikes, every cycle) rides along as the
    baseline its holdout rule compares with."""
    p = check_params(hyp, params)
    w = _window(hyp, a, b, data)
    V = _V()
    under = _underlying(hyp)
    if under not in (data.get("chains") or {}):
        prepare_vrp(hyp, data, _minus(w["from"], 60), w["to"])
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    rate = float(data.get("rate", 0.0557))
    chains, idx, vix, model = data["chains"][under], data["index"][under], data["vix"], data.get("har")
    notes = list(data.get("notes") or [])
    days = [d for d in chains.days if w["from"] <= d <= w["to"]]
    if not days:
        return _run_shape(hyp, "vrp-condor", [], capital, [], {"positions": 0}, {}, {},
                          notes + ["no EOD chains in the window"], status="no data")
    key = ("vrp-signal", idx[-1]["date"] if idx else None)
    if data.get("_sigkey") != key:
        iv = V.vix_iv(vix)
        rv = V.realised_vol(idx, 20, "close")
        data["_sig"] = (iv, rv, V.spread(iv, rv), V.har_forecast(model, idx) if model else {}, chains.iv_series(30, rate))
        data["_sigkey"] = key
    iv, rv, sig, har, atm = data["_sig"]

    def facts(day):
        return {"vix": iv.get(day), "rv20": None if rv.get(day) is None else round(rv[day], 3),
                "har_rv": None if har.get(day) is None else round(har[day], 3),
                "atm_iv30": None if atm.get(day) is None else round(atm[day], 3)}
    cache = data.setdefault("_specs", {})
    th = float(p["vrp_threshold_vol_points"])
    trades, skipped, mtm = vrp_backtest(chains, days, sig, th, p, cost_mult, capital, rate, facts, cache)
    base_t, base_s, base_m = vrp_backtest(chains, days, sig, None, p, cost_mult, capital, rate, facts, cache)
    met = _metrics(trades, days, capital, mtm)
    avg_c = _mean([t["premium"] for t in trades if t.get("credit")])
    met["worst_loss_avg_credits"] = round(-min(t["net"] for t in trades) / avg_c, 3) if trades and avg_c else None
    bm = _metrics(base_t, days, capital, base_m)
    met["beats_untimed"] = (met["return_on_margin_pct"] > bm["return_on_margin_pct"]
                            if met.get("return_on_margin_pct") is not None
                            and bm.get("return_on_margin_pct") is not None else None)
    met["by_events"] = _event_split(trades, w["from"], w["to"], data)
    notes.append("signal: India VIX - 20-session close-to-close realised vol (the registered rule), entry at the "
                 "close on the first session of a cycle with the spread above %g vol points" % th)
    notes.append("strikes: %g-delta shorts on Black-76 IV from settle prices; wings %g%% of spot; margin is "
                 "trader-strategy's SPAN-style estimate (with the expiry-day ELM when held to expiry)"
                 % (p["short_delta"], p["wing_width_pct_of_spot"]))
    return _run_shape(hyp, "vrp-condor", days, capital, trades, met, mtm, skipped, notes,
                      model_priced=met.get("model_priced_marks", 0) > 0,
                      extra={"baseline": {"label": "the same condor sold every cycle (untimed)", "n": len(base_t),
                                          "return_on_margin_pct": bm.get("return_on_margin_pct"),
                                          "net": bm.get("net"), "skipped": base_s},
                             "har": None if model is None else {k: model[k] for k in ("coef", "n", "r2", "h", "train")}})


# ---------------------------------------------------------------- event-vol (eh-08, eh-09)

def _fo_lines(A, root, day):
    p = A.Store(A.default_root(root)).path("fo", day)
    try:
        return A._gz_lines(p)
    except OSError:
        return []


def fno_stocks(A, root, start, end):
    """The stocks with options in the archive's F&O bhavcopies between start and end."""
    out = set()
    for d in A.Store(A.default_root(root)).dates("fo"):
        if start <= d <= end:
            for l in _fo_lines(A, root, d)[1:]:
                f = l.split(",", 2)
                if len(f) > 1 and f[1] == "STO":
                    out.add(f[0])
    return sorted(out)


def _chain_dicts(A, day, lines, syms):
    """One read of a day's F&O bhavcopy -> {symbol: load_fo_chain_eod()-shaped chain} for `syms` (the spot
    is the bhavcopy's underlying price; a missing lot stays None)."""
    rows = {}
    for l in lines[1:]:
        k = l.find(",")
        s = l[:k]
        if s in syms:
            rows.setdefault(s, []).append(l)
    out = {}
    for s, ls in rows.items():
        futs, opts, spot = [], [], None
        for row in csv.reader(ls):
            rec = dict(zip(A.FO_COLS, row))
            for c in ("strike", "open", "high", "low", "close", "settle", "underlying_price", "turnover"):
                rec[c] = A._num(rec.get(c))
            for c in ("oi", "oi_change", "volume", "lot"):
                rec[c] = int(A._num(rec.get(c), 0) or 0) if rec.get(c) not in ("", None) else None
            if spot is None and rec["underlying_price"]:
                spot = rec["underlying_price"]
            (opts if rec["kind"] in ("IDO", "STO") else futs).append(rec)
        out[s] = {"date": day, "underlying": s, "spot": spot, "futures": futs, "options": opts}
    return out


def _boards(data, need):
    """{(symbol, day): trimmed board} for need = {day: {symbols}}, each day's file read once, cached."""
    A = _arch(data)
    cache = data.setdefault("_boards", {})
    for day in sorted(need):
        miss = set(s for s in need[day] if (s, day) not in cache)
        if not miss:
            continue
        got = _chain_dicts(A, day, _fo_lines(A, data.get("root"), day), miss)
        for s in miss:
            ch = got.get(s)
            cache[(s, day)] = O.trim_board(ch, keep_dte=EVENT_DTE, band=EVENT_BAND) if ch and ch["options"] else None
    return cache


def _iv_panel(data, syms, days, rate):
    """{symbol: {day: near-monthly ATM IV (%)}} (the expiry at least 7 days out), each day read once, cached."""
    A = _arch(data)
    panel = data.setdefault("_ivp", {})
    done = data.setdefault("_ivp_days", set())
    for day in days:
        if day in done:
            continue
        got = _chain_dicts(A, day, _fo_lines(A, data.get("root"), day), set(syms))
        for s, ch in got.items():
            if not ch["options"]:
                continue
            c = O.Chains(s, {day: O.trim_board(ch, keep_dte=EVENT_DTE, band=0.05)})
            b = c.boards[day]
            exps = [e for e in sorted(b["monthly"] or b["expiries"]) if (_d(e) - _d(day)).days >= 7]
            v = c.atm_iv(day, exps[0], rate) if exps else None
            if v:
                panel.setdefault(s, {})[day] = v
        done.add(day)
    return panel


def _closes(A, root, sym, end):
    try:
        bars = A.load_daily(sym, None, end, adjusted=False, root=root)
    except Exception:  # noqa: BLE001 - an archive without equity files
        bars = []
    return {b["date"]: b["c"] for b in bars if b.get("c")}


def prepare_events(hyp, data, start, until):
    A = _arch(data)
    root = data.get("root")
    days = [d for d in A.Store(A.default_root(root)).dates("fo") if start <= d <= until]
    if not days:
        raise EngineError("no F&O bhavcopies in the archive from %s to %s" % (start, until))
    data["sessions"] = [d for d in A.trading_days(None, until, root=root)]
    if data.get("symbols") is None:
        data["symbols"] = fno_stocks(A, root, start, until)
    data.setdefault("notes", []).append("%d F&O stocks with options in the archive, %s to %s"
                                        % (len(data["symbols"]), days[0], days[-1]))
    return days


def _percentile(hist, cur):
    R = O.ENV.get("regime")
    if R is not None and hasattr(R, "percentile_rank"):
        return R.percentile_rank(hist, cur)
    return 100.0 * sum(1 for x in hist if x < cur) / len(hist) if hist else None


def run_event_vol(hyp, params, a, b, data, cost_mult=1.0):
    """eh-08 (short iron fly over results) or eh-09 (long straddle into results), by the registration's side."""
    side = str((hyp.get("instrument") or {}).get("side") or "")
    return _event_run(hyp, params, a, b, data, cost_mult, short=side.startswith("short"))


def _event_plan(hyp, p, w, data, short):
    """[(symbol, event, windows, entry, exit, span)] for every results event of the universe in the window."""
    A = _arch(data)
    root = data.get("root")
    E = _E()
    sessions = data.get("sessions") or A.trading_days(None, w["to"], root=root)
    res = data.setdefault("_results", {})
    out = []
    for sym in data.get("symbols") or []:
        if sym not in res:
            got = (data.get("results") or {}).get(sym)
            res[sym] = got if got is not None else A.results_dates(sym, None, data.get("until") or w["to"], root=root)
        for ev in res[sym] or []:
            win = E.event_windows(ev, sessions)
            if win is None:
                continue
            i = win["i_event"]
            if short:
                j = i + int(p["exit_session"]) - 1
                if j >= len(sessions):
                    continue
                entry, exit_ = win["entry"], sessions[j]
            else:
                k = i - int(p["entry_sessions_before"])
                if k < 0:
                    continue
                entry, exit_ = sessions[k], win["entry"]
            if not (w["from"] <= entry and exit_ <= w["to"] and win["move_end"] <= w["to"]):
                continue
            lo, hi = sessions.index(entry), sessions.index(max(exit_, win["move_end"]))
            out.append((sym, ev, win, entry, exit_, sessions[lo:hi + 1]))
    return out, sessions


def _event_run(hyp, params, a, b, data, cost_mult, short):
    p = check_params(hyp, params)
    w = _window(hyp, a, b, data)
    A = _arch(data)
    E = _E()
    root = data.get("root")
    rate = float(data.get("rate", 0.0557))
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    min_hist = int(data.get("min_history", MIN_HISTORY))
    if data.get("symbols") is None:
        data["symbols"] = fno_stocks(A, root, w["from"], w["to"])
    plan, sessions = _event_plan(hyp, p, w, data, short)
    need = {}
    for sym, ev, win, entry, exit_, span in plan:
        for d in span:
            need.setdefault(d, set()).add(sym)
    boards = _boards(data, need)
    closes_all = data.setdefault("_closes", {})
    trades, skipped, study = [], {}, []
    notes = list(data.get("notes") or [])
    cache = data.setdefault("_specs", {})
    eng = "event-crush-iron-fly" if short else "event-runup-straddle"
    for n, (sym, ev, win, entry, exit_, span) in enumerate(plan):
        if n % 200 == 199:
            _say(data, "%s: %d of %d events" % (hyp.get("id"), n + 1, len(plan)))
        bd = {d: boards.get((sym, d)) for d in span if boards.get((sym, d))}
        if entry not in bd:
            _skip(skipped, "no options on the entry session (not in the F&O list then)")
            continue
        if exit_ not in bd:
            _skip(skipped, "no chain on the exit session")
            continue
        ch = O.Chains(sym, bd)
        if sym not in closes_all:
            closes_all[sym] = _closes(A, root, sym, data.get("until") or w["to"])
        closes = closes_all[sym]
        for d in span:                         # equity closes, else the bhavcopy's underlying price
            if d not in closes and d in bd:
                closes[d] = bd[d]["spot"]
        evs = data["_results"][sym]
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
            an = ch.analysis(entry, exp, rate)
            wing = _wing_points(an["spot"], p["wing_pct_of_spot"], _S().strike_step(an))
            spec = _compiled(sym, "iron-fly", {"by": "atm"}, wing, 1, capital, rate, cache)
        else:
            cap = float(p["iv_percentile_cap"])
            if cap < 100:
                k0 = bisect.bisect_left(sessions, entry)
                look = sessions[max(0, k0 - IV_HISTORY):k0 + 1]
                ivp = _iv_panel(data, [sym], look, rate).get(sym, {})
                hist_iv = [ivp[d] for d in look[:-1] if d in ivp]
                cur = ivp.get(entry)
                if cur is None or len(hist_iv) < IV_HISTORY_MIN:
                    _skip(skipped, "under %d sessions of ATM IV history" % IV_HISTORY_MIN)
                    continue
                pct = _percentile(hist_iv, cur)
                row["iv_percentile"] = round(pct, 1)
                if pct > cap:
                    _skip(skipped, "ATM IV percentile over the cap")
                    continue
            spec = _compiled(sym, "long-straddle", {"by": "atm"}, None, 1, capital, rate, cache)
        pos, why = O.open_position(spec, ch, entry, exp, cost_mult)
        if pos is None:
            _skip(skipped, why)
            continue
        if any(l["price_source"] != "settle" for l in pos["legs"]):
            _skip(skipped, "a strike did not trade on the entry session")
            continue
        gross, prices, models = O._value(pos, ch, exit_)
        pos["breached"] = O._breach(pos, bd[exit_]["spot"]) if short else False
        pos["models"] += models
        t = O.close_position(spec, pos, ch, exit_, "exit session" if short else "close before results",
                             prices, cost_mult, False)
        t["facts"] = dict(row)
        if short:
            _seller_ret(t)
        else:
            t["attribution"] = _attribution(pos, ch, exit_, rate, t)
            _buyer_ret(t)
        if not win["time_known"]:
            t["flags"] = (t.get("flags") or []) + ["results time unknown: the reaction may be on %s or the "
                                                   "session after" % win["event_session"]]
        trades.append(t)
    trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    days = [d for d in A.Store(A.default_root(root)).dates("fo") if w["from"] <= d <= w["to"]]
    mtm = _realised_mtm(trades, days)
    met = _metrics(trades, days, capital, mtm)
    met["event_study"] = E.summary(study)
    met["events_traded"] = len(trades)
    if not short and trades:
        met["pnl_from_iv_change"] = round(sum(t["attribution"]["iv_change"] for t in trades), 2)
        met["pnl_from_move_and_time"] = round(sum(t["attribution"]["move_and_time"] for t in trades), 2)
    notes.append("results events from trader-archive's results dataset; the reacting session re-derived from "
                 "the archive's sessions; implied move = ATM straddle / spot at the entry close on the nearest "
                 "monthly expiry alive after the exit; P&L booked on the exit day")
    notes.append("EOD settle prices: a real fill at 15:29 on a thin stock-option strike can be far worse than "
                 "the slippage model")
    return _run_shape(hyp, eng, days, capital, trades, met, mtm, skipped, notes,
                      extra={"event_rows": study[:200]})


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


def captured_days(A, root, under, a, b):
    """The days in a..b with captured chains of `under`."""
    base = os.path.join(A.default_root(root), "chain", under.upper())
    try:
        return sorted(d for d in os.listdir(base) if a <= d <= b and A.chain_snapshots(under, d, root))
    except OSError:
        return []


def prepare_buying(hyp, data, start, until):
    A = _arch(data)
    root = data.get("root")
    under = _underlying(hyp)
    notes = data.setdefault("notes", [])
    bars = A.load_intraday(under, "1m", start, until, root) or A.load_intraday(under, "5m", start, until, root)
    if not bars:
        raise EngineError("no %s minute bars in the archive (archive.py intraday --symbols %s with a broker token: "
                          "Upstox index candles go back to Jan 2022)" % (under, under))
    data.setdefault("intraday", {})[under] = bars
    data["vix"] = A.load_vix(start, until, root)
    try:
        data.setdefault("chains", {})[under] = O.load_chains(under, root, A, start, until, band=0.03, keep_dte=10)
    except Exception as e:  # noqa: BLE001 - no EOD chains: IV from India VIX, expiries from the rules
        notes.append("no %s EOD chains (%s): IV from India VIX, expiries from trader-regime's rules" % (under, e))
    return sorted(set(x["date"] for x in bars))


def run_option_buying(hyp, params, a, b, data, cost_mult=1.0):
    """eh-14: on a session whose previous India VIX close is under the cap, buy the nearest weekly ATM call
    (put) when a bar closes above (below) the opening range by min_breakout_pct; out by 15:15 or on a 30%
    trail from the peak. Train and validation are MODEL-PRICED (Black-76 on index bars, the previous
    session's EOD ATM IV of that expiry, else India VIX); the holdout prices from captured chains only, so
    it waits for #76's capture and its verdict is capped at WATCH."""
    p = check_params(hyp, params)
    w = _window(hyp, a, b, data)
    A = _arch(data)
    root = data.get("root")
    under = _underlying(hyp)
    rate = float(data.get("rate", 0.0557))
    capital = float(data.get("capital") or DEFAULT_CAPITAL)
    L = _L()
    live = w["holdout"]
    notes, skipped, trades = list(data.get("notes") or []), {}, []
    if live and not captured_days(A, root, under, w["from"], w["to"]):
        return _run_shape(hyp, "trend-day-buying", [], capital, [], {"positions": 0}, {}, {},
                          notes + ["the holdout is priced from captured option chains only (archive.py "
                                   "capture-chains, #76) and none exist in the window yet; the verdict is "
                                   "capped at WATCH whatever it shows"], status="waiting for captured chains")
    if under not in (data.get("intraday") or {}):
        try:
            prepare_buying(hyp, data, _minus(w["from"], 10), w["to"])
        except EngineError as e:
            return _run_shape(hyp, "trend-day-buying", [], capital, [], {"positions": 0}, {}, {}, notes + [str(e)],
                              status="no data", model_priced=not live)
    bars = [x for x in data["intraday"][under] if w["from"] <= x["date"] <= w["to"]]
    vixc = {x["date"]: x["c"] for x in data.get("vix") or [] if x.get("c")}
    chains = (data.get("chains") or {}).get(under)
    by = {}
    for x in bars:
        by.setdefault(x["date"], []).append(x)
    days = sorted(by)
    if live:
        cap = set(captured_days(A, root, under, w["from"], w["to"]))
        days = [d for d in days if d in cap]
    known = sorted(set(vixc) | set(chains.days if chains else []))
    R = _reg(data)
    cfg = {"fno_charges": {}}
    orm, pct, vcap = int(p["opening_range_minutes"]), float(p["min_breakout_pct"]), float(p["vix_cap"])
    start, flat = _hm("09:15"), _hm(FLAT_BY)
    for day in days:
        db = sorted(by[day], key=lambda x: x["time"])
        k = bisect.bisect_left(known, day)
        prev = known[k - 1] if k > 0 else None
        v = vixc.get(prev) if prev else None
        if v is None:
            _skip(skipped, "no India VIX close the session before")
            continue
        if not v < vcap:
            _skip(skipped, "India VIX at or over the cap")
            continue
        step_m = max(1, _hm(db[1]["time"]) - _hm(db[0]["time"])) if len(db) > 1 else 1
        orb = [x for x in db if _hm(x["time"]) < start + orm]
        if len(orb) < orm // step_m:
            _skip(skipped, "opening range incomplete")
            continue
        hi, lo = max(x["h"] for x in orb), min(x["l"] for x in orb)
        sig = None
        for j, x in enumerate(db):
            end = _hm(x["time"]) + step_m
            if _hm(x["time"]) < start + orm or end > _hm(LAST_ENTRY):
                continue
            if x["c"] > hi * (1 + pct / 100.0):
                sig = (j, "CE", x, end)
                break
            if x["c"] < lo * (1 - pct / 100.0):
                sig = (j, "PE", x, end)
                break
        if sig is None:
            _skip(skipped, "no opening-range break by %s" % LAST_ENTRY)
            continue
        j, kind, x, end = sig
        expiry, lot, sigma, src = None, None, None, None
        if chains is not None and prev in chains.boards:
            exps = [e for e in chains.boards[prev]["expiries"] if e >= day]
            expiry = exps[0] if exps else None
            if expiry:
                sigma = chains.atm_iv(prev, expiry, rate)
                an = chains.analysis(prev, expiry, rate)
                lot = an and an.get("lot_size")
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
        K = round(x["c"] / step) * step
        if live:
            px_in, why = snapshot_quote(A, root, under, day, end, expiry, K, kind, "buy")
            if px_in is None:
                _skip(skipped, why)
                continue
            fill_in = px_in
        else:
            px_in = model_price(x["c"], K, day, end, expiry, sigma, rate, kind)
            fill_in = px_in + O.slip(px_in, O.OPTION_COSTS, cost_mult)
        peak, exit_px, exit_t, reason, spot = fill_in, None, None, None, None
        for y in db[j + 1:]:
            t0 = _hm(y["time"])
            if t0 >= flat:
                exit_t, reason, spot = flat, "15:15", y["o"]
                break
            te = t0 + step_m
            if live:
                mark, _ = snapshot_quote(A, root, under, day, te, expiry, K, kind, "sell")
            else:
                mark = model_price(y["c"], K, day, te, expiry, sigma, rate, kind)
            if mark is None:
                continue
            peak = max(peak, mark)
            if p["exit"] == "trail_30pct_from_peak" and mark <= (1 - TRAIL) * peak:
                exit_px, exit_t, reason = mark, te, "trail 30% from the peak"
                break
        else:
            exit_t, reason, spot = _hm(db[-1]["time"]) + step_m, "last bar", db[-1]["c"]
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
        t = {"symbol": under, "signal_date": day, "entry_date": day, "entry_time": _hhmm(end),
             "exit_date": day, "exit_time": _hhmm(exit_t), "expiry": expiry, "reason": reason,
             "structure": "long-%s" % ("call" if kind == "CE" else "put"), "side": "BUY", "strike": K,
             "option_type": kind, "lots": 1, "lot_size": lot, "qty": qty, "premium": round(-risk, 2),
             "credit": False, "entry": round(fill_in, 2), "exit": round(fill_out, 2), "gross": L.round_paise(gross),
             "charges": L.round_paise(ch), "net": L.round_paise(net), "margin": round(risk, 2),
             "r": round(net / risk, 5) if risk else 0.0, "sessions": 0,
             "return_on_margin_pct": round(100.0 * net / risk, 3) if risk else None,
             "price_source": "captured chain" if live else "model (Black-76)",
             "iv": round(sigma, 2), "iv_source": src, "vix_prev": v, "breached": False,
             "model_marks": 0 if live else 1, "flags": [] if live else ["model-priced"]}
        trades.append(_buyer_ret(t))
    mtm = _realised_mtm(trades, days)
    met = _metrics(trades, days, capital, mtm)
    notes.append("signal on %s bars: opening range %d min, break %g%%, India VIX (previous close) under %g, "
                 "entries until %s, out by %s" % (under, orm, pct, vcap, LAST_ENTRY, FLAT_BY))
    if not live:
        notes.append("model-priced: Black-76 with the IV held for the session; intraday IV moves, the bid-ask "
                     "spread and the expiry-day smile are not in these prices. A model-priced pass can only send "
                     "the idea to the live-captured holdout, and that can only reach WATCH")
    return _run_shape(hyp, "trend-day-buying", days, capital, trades, met, mtm, skipped, notes, model_priced=not live)


def holdout_wait(hyp, data, today):
    """Why the sealed holdout cannot be opened yet (None when it can): eh-14's holdout is the live captured
    chain, so it waits until the window has ended and the capture covers it. The executor asks before it
    opens the holdout, so it is never burnt on data that does not exist yet."""
    if hyp.get("family") != "option-buying":
        return None
    w = (hyp.get("span") or {}).get("holdout") or {}
    if today and today <= w.get("to", ""):
        return "the holdout %s to %s is live captured data and has not ended yet (#76 captures it)" % (
            w.get("from"), w.get("to"))
    A = _arch(data)
    have = captured_days(A, data.get("root"), _underlying(hyp), w.get("from", ""), w.get("to", ""))
    if not have:
        return "no captured %s chains in the holdout %s to %s yet (archive.py capture-chains, #76)" % (
            _underlying(hyp), w.get("from"), w.get("to"))
    return None


PREPARE = {"vrp-selling": prepare_vrp, "event-vol": prepare_events, "option-buying": prepare_buying}
ENGINES = {"vrp-selling": run_vrp, "event-vol": run_event_vol, "option-buying": run_option_buying}
