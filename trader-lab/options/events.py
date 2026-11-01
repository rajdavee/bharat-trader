"""Event studies on stock options, and the event-day calendar. Standard library only.

    implied move   the ATM straddle (call + put at the strike nearest the forward) over the spot, at the
                   close of the session before the first reacting session, on the nearest expiry that is
                   still alive after it (EOD settle prices; an untraded leg is priced by Black-76 and flagged)
    realised move  |close of the first reacting session / close before it - 1|; when the results came from
                   a board-meeting row with no time, the reaction may be that day or the next, so the move
                   is taken to the close of the session after the meeting date and the event is flagged
    history        the stock's own earlier events only (dated before the entry session): their mean
                   realised move is what the implied move is compared with

trader-archive's results_dates() gives "event_session" from the sessions the archive holds; when the archive
starts after an event, that is the archive's first session, not the event's. This module re-derives it from
the archive's sessions and drops an event whose reacting session is more than a week after its date.

The calendar: results (stocks), RBI policy and Union Budget days (trader-regime's events.json) and expiry
days (the archive's EOD chains). EOD caveats: settle prices are end-of-day, not the price you could trade at
15:29; wide stock-option spreads are modelled by the slippage, not observed.
Education and tooling, not financial advice.
"""

import bisect
import datetime
import math

MAX_REACTION_DAYS = 7
CALENDAR_KINDS = ("results", "rbi_policy", "budget", "expiry")


def _d(s):
    return datetime.date.fromisoformat(s)


def session_index(sessions, day):
    """Index of the first session on or after `day` (len(sessions) when none)."""
    return bisect.bisect_left(sessions, day)


def reacting_session(ev, sessions):
    """(first reacting session, time_known) for one results_dates() event, from `sessions`, or (None, why).
    After 15:30 IST: the next session; during or before the session: that day (or the next session when the
    date is not one, e.g. a Saturday meeting); no time: the session on or after the date, time_known False
    (True when the date is not a session: then only the next session can react)."""
    day = ev.get("date")
    if not day:
        return None, "no date"
    i = session_index(sessions, day)
    if ev.get("after_close") is True and i < len(sessions) and sessions[i] == day:
        i += 1
    if i >= len(sessions):
        return None, "no session after the event in the archive"
    s = sessions[i]
    if (_d(s) - _d(day)).days > MAX_REACTION_DAYS:
        return None, "the archive has no session within a week of %s" % day
    # a meeting on a non-session day (a Saturday) can only be reacted to on the next session
    return s, ev.get("after_close") is not None or s != day


def event_windows(ev, sessions):
    """The sessions an event study needs, or None:
    {"date", "event_session", "time_known", "entry" (the session before the reacting one), "move_end"
     (the session the realised move is measured to), "i_event"}."""
    s, known = reacting_session(ev, sessions)
    if s is None:
        return None
    i = sessions.index(s)
    if i == 0:
        return None
    end = s
    if not known and s == ev["date"] and i + 1 < len(sessions):
        end = sessions[i + 1]          # time unknown: the move to the close after the meeting covers both
    return {"date": ev["date"], "event_session": s, "time_known": known, "entry": sessions[i - 1],
            "move_end": end, "i_event": i}


def straddle(chains, day, expiry, rate):
    """The ATM straddle at `day`'s close on `expiry`: {"strike", "ce", "pe", "premium", "forward", "spot",
    "sources", "model_legs"} or None (no board, no expiry, no usable leg)."""
    a = chains.analysis(day, expiry, rate)
    if a is None:
        return None
    k = a["atm"]
    ce, cs = chains.price(day, expiry, k, "CE")
    pe, ps = chains.price(day, expiry, k, "PE")
    if not ce or not pe:
        return None
    return {"strike": k, "ce": ce, "pe": pe, "premium": ce + pe, "forward": a["forward"], "spot": a["spot"],
            "sources": [cs, ps], "model_legs": sum(1 for s in (cs, ps) if s != "settle"), "expiry": expiry,
            "lot": a.get("lot_size"), "atm_iv": a.get("atm_iv")}


def alive_expiry(chains, day, after, monthly=True):
    """The nearest expiry listed on `day` that is on or after the session `after` (so the position is
    still alive when it is closed): monthly (with a future) when stock options are monthly."""
    b = chains.board(day)
    if b is None:
        return None
    pool = sorted(b["monthly"]) if monthly and b["monthly"] else b["expiries"]
    later = [e for e in pool if e > after]
    return later[0] if later else None


def implied_move(chains, day, after, rate, monthly=True):
    """The implied move (straddle / spot) at `day`'s close on the nearest expiry alive after `after`:
    {"move_pct", "straddle", ...} or None."""
    e = alive_expiry(chains, day, after, monthly)
    if e is None:
        return None
    st = straddle(chains, day, e, rate)
    if st is None or not st["spot"]:
        return None
    st["move_pct"] = 100.0 * st["premium"] / st["spot"]
    return st


def realised_move(close_of, a, b):
    """|close(b) / close(a) - 1| in %, or None. close_of: {date: close}."""
    x, y = close_of.get(a), close_of.get(b)
    if not x or not y:
        return None
    return 100.0 * abs(y / x - 1.0)


def history_moves(symbol_events, close_of, sessions, before):
    """The realised moves (%) of the stock's events whose entry session is before `before`, oldest first."""
    out = []
    for ev in symbol_events:
        w = event_windows(ev, sessions)
        if w is None or w["entry"] >= before:
            continue
        m = realised_move(close_of, w["entry"], w["move_end"])
        if m is not None:
            out.append(m)
    return out


def study(symbol, events, chains, close_of, sessions, rate, monthly=True, start=None, end=None):
    """The implied-vs-realised event study of one stock: one row per event in [start, end] with a chain:
    {"symbol", "date", "event_session", "time_known", "entry", "implied_move_pct", "realised_move_pct",
     "history_mean_pct", "history_n", "ratio_to_history", "realised_above_implied", "expiry", "strike",
     "model_legs"}."""
    rows = []
    for ev in events:
        w = event_windows(ev, sessions)
        if w is None or (start and w["entry"] < start) or (end and w["move_end"] > end):
            continue
        im = implied_move(chains, w["entry"], w["move_end"], rate, monthly)
        rm = realised_move(close_of, w["entry"], w["move_end"])
        hist = history_moves(events, close_of, sessions, w["entry"])
        hm = sum(hist) / len(hist) if hist else None
        row = {"symbol": symbol, "date": w["date"], "event_session": w["event_session"],
               "time_known": w["time_known"], "entry": w["entry"], "move_end": w["move_end"],
               "implied_move_pct": None if im is None else round(im["move_pct"], 4),
               "realised_move_pct": None if rm is None else round(rm, 4),
               "history_mean_pct": None if hm is None else round(hm, 4), "history_n": len(hist),
               "ratio_to_history": round(im["move_pct"] / hm, 4) if im and hm else None,
               "realised_above_implied": (rm > im["move_pct"]) if im and rm is not None else None,
               "expiry": im and im["expiry"], "strike": im and im["strike"], "model_legs": im and im["model_legs"]}
        rows.append(row)
    return rows


def summary(rows):
    """Counts and means of an event study (rows with both moves)."""
    both = [r for r in rows if r["implied_move_pct"] is not None and r["realised_move_pct"] is not None]
    n = len(both)
    if not n:
        return {"events": len(rows), "with_both": 0}
    im = [r["implied_move_pct"] for r in both]
    rm = [r["realised_move_pct"] for r in both]
    above = sum(1 for r in both if r["realised_above_implied"])
    p = above / float(n)
    half = 1.96 * math.sqrt(p * (1 - p) / n) if n > 1 else None
    return {"events": len(rows), "with_both": n, "mean_implied_pct": round(sum(im) / n, 3),
            "mean_realised_pct": round(sum(rm) / n, 3), "realised_above_implied": above,
            "realised_above_implied_pct": round(100.0 * p, 1),
            "ci95_pct": None if half is None else [round(100 * max(0.0, p - half), 1), round(100 * min(1.0, p + half), 1)],
            "time_unknown": sum(1 for r in both if not r["time_known"])}


# ---------------------------------------------------------------- the event-day calendar

def calendar(start, end, regime_events=(), expiries=(), results=None):
    """{date: [kinds]} of the event days in [start, end]: "rbi_policy" and "budget" from trader-regime's
    events (its builtin_events() plus your own), "expiry" from the given expiry dates, "results" from
    results_dates() rows (their reacting session when given, else the date)."""
    out = {}

    def add(d, k):
        if d and start <= d <= end:
            out.setdefault(d, [])
            if k not in out[d]:
                out[d].append(k)
    for e in regime_events or ():
        if e.get("kind") in ("rbi_policy", "budget"):
            add(e.get("date"), e["kind"])
    for x in expiries or ():
        add(x, "expiry")
    for r in results or ():
        add(r.get("event_session") or r.get("date"), "results")
    return out


def blocked(day, cal, kinds=CALENDAR_KINDS, sessions=None, ahead=0):
    """The event kinds on `day` (and the next `ahead` sessions when sessions are given) that are in `kinds`:
    [] when the day is clear."""
    days = [day]
    if ahead and sessions:
        i = bisect.bisect_right(sessions, day)
        days += sessions[i:i + ahead]
    hit = []
    for d in days:
        for k in cal.get(d, []):
            if k in kinds and k not in hit:
                hit.append(k)
    return hit
