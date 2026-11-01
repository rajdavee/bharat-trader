"""Event engine: entries and exits relative to dated events, on index futures (calendar windows) or on stocks
(results dates).

Calendar windows (intents for futures.simulate; every date comes from the session calendar, which NSE
publishes in advance, so no price after the entry is read):

    turn_of_month    enter at the close of the month's session `entry_session` (-1 the last, -2 the one before),
                     exit at the close of the next month's session `exit_session` (1 the first)
    pre_holiday      exit at the close of the last session before an exchange holiday (a weekday with no
                     session), enter `entry_sessions_before` sessions earlier
    expiry_week      exit at the close of the monthly expiry session, enter `entry_sessions_before` earlier
    overnight        enter at the close, exit at the next open: every session, after a down session, or
                     before a weekend or holiday

Results-date events (post-earnings drift, results_drift): the surprise is the stock's 3-session abnormal
return around the results (the close before the reaction session to the close after it, minus the
equal-weighted universe), known at that last close. An event is taken when its surprise is in the top
`top_fraction` of the surprises of the previous 252 sessions (at least 20 of them, min_past_events), entered at the close
`entry_lag_sessions` later, held `hold_sessions`, one of `slots` equal slots of the equity, delivery
(CNC) charges and DP charges. The reaction session is the first session on or after the results date
(after it, when the results came out at or after 15:30).
"""

import bisect

from . import core


def _purge(intents, a, b):
    return [it for it in intents if it["entry"] >= a and it["exit"] <= b and it["exit"] > it["entry"] - 1]


def turn_of_month(days, a, b, entry_session, exit_session):
    by = {}
    for k, d in enumerate(days):
        by.setdefault(core.month_key(d), []).append(k)
    months = sorted(by)
    out = []
    for m, nxt in zip(months, months[1:]):
        cur, new = by[m], by[nxt]
        if len(cur) < abs(entry_session) or len(new) < exit_session:
            continue
        k, j = cur[entry_session], new[exit_session - 1]
        out.append({"entry": k, "entry_at": "close", "exit": j, "exit_at": "close", "side": 1, "why": "turn of month"})
    return _purge(out, a, b)


def pre_holiday(days, a, b, n):
    hol = core.holidays(days)
    out, seen = [], set()
    for h in hol:
        k = bisect.bisect_left(days, h) - 1
        if k < 0 or k in seen:
            continue
        seen.add(k)
        if k - n >= 0:
            out.append({"entry": k - n, "entry_at": "close", "exit": k, "exit_at": "close", "side": 1,
                        "why": "pre-holiday"})
    return _purge(out, a, b)


def expiry_week(days, expiries, a, b, n):
    out = []
    for e in sorted(set(x for x in expiries if x)):
        if e not in days:
            continue
        k = days.index(e)
        if k - n >= 0:
            out.append({"entry": k - n, "entry_at": "close", "exit": k, "exit_at": "close", "side": 1,
                        "why": "monthly expiry"})
    return _purge(out, a, b)


def overnight(s, a, b, condition):
    out = []
    for k in range(a, b):
        if condition == "after_down_session":
            if k == 0 or s.ret_cc[k] >= 0:
                continue
        elif condition == "before_weekend_or_holiday":
            if (core.day(s.days[k + 1]) - core.day(s.days[k])).days <= 1:
                continue
        elif condition != "every_session":
            raise core.EngineError("condition is every_session, after_down_session or before_weekend_or_holiday")
        out.append({"entry": k, "entry_at": "close", "exit": k + 1, "exit_at": "open", "side": 1, "why": "overnight"})
    return out


def in_window_vs_other(s, intents, a, b):
    """The mean close-to-close return of the sessions the intents hold against the window's other sessions,
    with a Welch t-statistic (positive: the windows did better)."""
    held = set()
    for it in intents:
        for k in range(it["entry"] + 1, it["exit"] + 1):
            held.add(k)
    inn = [s.ret_cc[k] for k in range(a + 1, b + 1) if k in held]
    out = [s.ret_cc[k] for k in range(a + 1, b + 1) if k not in held]
    mi, mo = core.mean(inn), core.mean(out)
    vi = core.sd(inn) ** 2 / len(inn) if len(inn) > 1 else 0.0
    vo = core.sd(out) ** 2 / len(out) if len(out) > 1 else 0.0
    t = (mi - mo) / (vi + vo) ** 0.5 if vi + vo > 0 else 0.0
    return {"in_sessions": len(inn), "other_sessions": len(out), "in_mean_pct": round(mi * 100, 5),
            "other_mean_pct": round(mo * 100, 5), "welch_t": round(t, 3), "in_above_other": mi > mo}


# ---------------------------------------------------------------- results-date events on stocks

def reaction_session(days, event):
    """The index of the first session that can react to a results announcement {date, time?}."""
    d = event["date"] if isinstance(event, dict) else str(event)[:10]
    t = event.get("time") if isinstance(event, dict) else None
    k = bisect.bisect_left(days, d)
    if t and str(t)[:5] >= "15:30" and k < len(days) and days[k] == d:
        k += 1
    return k if k < len(days) else None


def results_drift(panel, members_fn, results, params, a, b, capital, cfg=None, cost_mult=1.0, bps=None,
                  liquidity_floor=None, liquidity_window=20):
    """Post-earnings drift on stocks. results: {symbol: [{date, time?}]}. Returns the engine run dict; each
    trade carries its abnormal net return ("abnormal") against the equal-weighted universe."""
    cfg = cfg or {}
    bps = core.COSTS["slippage_bps"] if bps is None else bps
    dp = float(cfg.get("dp_charge", core.COSTS["dp_charge"]))
    top = float(params["top_fraction"])
    hold = int(params["hold_sessions"])
    lag = int(params["entry_lag_sessions"])
    slots = int(params.get("slots", 20))
    need = int(params.get("min_past_events", 20))
    days = panel.days
    # the equal-weighted universe's daily return (yesterday's members), for abnormal returns
    ew = [0.0] * panel.n
    mem_cache = {}

    def members(k):
        m = core.month_key(days[k])
        if m not in mem_cache:
            mem_cache[m] = set(members_fn(days[k]) or [])
        return mem_cache[m]
    for k in range(1, panel.n):
        rs = [panel.c[s_][k] / panel.cf[s_][k - 1] - 1 for s_ in members(k - 1)
              if s_ in panel.c and panel.c[s_][k] is not None and panel.cf[s_][k - 1]]
        ew[k] = core.mean(rs)

    def ew_between(i, j):
        x = 1.0
        for k in range(i + 1, j + 1):
            x *= 1 + ew[k]
        return x - 1

    # every event's surprise, known at the close `known`
    evs = []
    for sym, lst in sorted(results.items()):
        if sym not in panel.c:
            continue
        for e in lst or []:
            k = reaction_session(days, e)
            if k is None or k < 1 or k + 1 >= panel.n:
                continue
            c0, c1 = panel.cf[sym][k - 1], panel.c[sym][k + 1]
            if not c0 or c1 is None or sym not in (members_fn(days[k]) or []):
                continue                    # a member on the reaction day itself (point in time)
            if liquidity_floor:
                mv = panel.median_value(sym, k - 1, liquidity_window)
                if not mv or mv < liquidity_floor:
                    continue
            ar = (c1 / c0 - 1) - ew_between(k - 1, k + 1)
            evs.append({"symbol": sym, "reaction": k, "known": k + 1, "surprise": ar})
    evs.sort(key=lambda x: (x["known"], x["symbol"]))
    known_k = [x["known"] for x in evs]
    picks = []
    warm = 0
    for x in evs:
        lo = bisect.bisect_left(known_k, x["known"] - 252)
        hi = bisect.bisect_left(known_k, x["known"])
        past = sorted(y["surprise"] for y in evs[lo:hi])
        if len(past) < need:
            warm += 1
            continue
        cut = past[min(len(past) - 1, int((1 - top) * len(past)))]
        if x["surprise"] >= cut and x["surprise"] > 0:
            entry = x["known"] + lag
            if a <= entry < b:
                picks.append(dict(x, entry=entry, exit=min(entry + hold, b)))
    run = core.new_run(days[a:b + 1], capital)
    pnl = [0.0] * (b - a + 1)
    eq = float(capital)
    open_, skipped = [], 0
    by_entry = {}
    for x in picks:
        by_entry.setdefault(x["entry"], []).append(x)

    def charge(value, buy, on):
        c = core.cnc_side_charges(value if buy else 0.0, 0.0 if buy else value, cfg, on) * cost_mult
        run["charges"] += c
        run["traded"] += value
        return c
    for k in range(a, b + 1):
        i = k - a
        dpnl = 0.0
        for p in open_:
            c = panel.c[p["symbol"]][k]
            if c is not None:
                g = p["qty"] * (c - p["ref"])
                dpnl += g
                p["gross"] += g
                p["ref"] = c
        still = []
        sold = set()
        for p in open_:
            if k >= p["exit"]:
                px = panel.cf[p["symbol"]][k]
                fill = core.slip(px, bps, -1, cost_mult)
                g = p["qty"] * (fill - px)
                c = charge(p["qty"] * fill, False, days[k])
                if p["symbol"] not in sold:
                    c += dp * cost_mult
                    run["charges"] += dp * cost_mult
                    sold.add(p["symbol"])
                p["gross"] += g
                p["charges"] += c
                dpnl += g - c
                net = p["gross"] - p["charges"]
                ret = net / p["value"] if p["value"] > 0 else 0.0
                run["trades"].append({"symbol": p["symbol"], "side": "long", "entry_date": days[p["entry"]],
                                      "exit_date": days[k], "entry": round(p["fill"], 2), "exit": round(fill, 2),
                                      "qty": p["qty"], "gross": round(p["gross"], 2), "charges": round(p["charges"], 2),
                                      "net": round(net, 2), "ret": ret, "sessions": k - p["entry"],
                                      "surprise": round(p["surprise"], 5),
                                      "abnormal": ret - ew_between(p["entry"], k)})
            else:
                still.append(p)
        open_ = still
        for x in by_entry.get(k, []):
            if len(open_) >= slots or any(p["symbol"] == x["symbol"] for p in open_):
                skipped += 1
                continue
            px = panel.c[x["symbol"]][k]
            if px is None:
                skipped += 1
                continue
            fill = core.slip(px, bps, 1, cost_mult)
            q = int(eq / slots / fill)
            if q <= 0:
                skipped += 1
                continue
            c = charge(q * fill, True, days[k])
            dpnl -= c + q * (fill - px)
            open_.append({"symbol": x["symbol"], "qty": q, "ref": px, "fill": fill, "entry": k, "exit": x["exit"],
                          "gross": -q * (fill - px), "charges": c, "value": q * fill, "surprise": x["surprise"]})
        eq += dpnl
        pnl[i] = dpnl
        run["exposure"][i] = sum(p["qty"] * (panel.cf[p["symbol"]][k] or 0) for p in open_) / eq if eq > 0 else 0.0
    core.finish(run, pnl)
    run["bench"] = ew[a:b + 1]
    run["bench"][0] = 0.0 if a == 0 else run["bench"][0]
    ab = [t["abnormal"] for t in run["trades"]]
    run["extra"] = {"events_seen": len(evs), "events_selected": len(picks), "skipped_no_slot": skipped,
                    "warming_up": warm, "mean_abnormal_pct": round(core.mean(ab) * 100, 4) if ab else 0.0,
                    "slots": slots}
    return run
