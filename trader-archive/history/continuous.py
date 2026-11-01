"""Continuous futures from the daily F&O bhavcopies: one price series out of a chain of expiring contracts.

Roll rules (each chooses the contract held on a day; nothing looks ahead):
  days:N    hold the nearest expiry until N sessions before it, then the next one. The last day on a
            contract is the session N sessions before its expiry day ("days:0" holds through expiry day).
  oi        switch the day after the next contract's open interest first exceeds the near contract's
  volume    the same with traded contracts
  The OI and volume rules still roll by expiry day at the latest.

Methods:
  none      the held contract's own prices; each roll is logged with the price gap
  back      back-adjusted: everything before a roll is shifted by the gap between the two contracts on the
            last day of the old one, so the series has no roll jumps (absolute price levels in the past
            are not real; differences are)
  ratio     ratio-adjusted: everything before a roll is multiplied by new / old on that day (returns are
            real; levels are not)

Pure functions: archive.py reads the bhavcopies and passes the rows in.
"""

import bisect

RULES = ("days", "oi", "volume")
METHODS = ("none", "back", "ratio")


def parse_rule(rule):
    """'days:2' / 'oi' / 'volume' -> (kind, n)."""
    r = str(rule or "days:0").strip().lower()
    if r.startswith("days"):
        n = r.split(":", 1)[1] if ":" in r else "0"
        try:
            n = int(n)
        except ValueError:
            raise ValueError("roll days:N wants a whole number of sessions, not %r" % rule)
        if n < 0 or n > 15:
            raise ValueError("roll days:N wants N from 0 to 15")
        return "days", n
    if r in ("oi", "volume"):
        return r, 0
    raise ValueError("roll is days:N, oi or volume, not %r" % rule)


def _price(c):
    """A contract's close for the day; an untraded contract (no open) falls back to its settle price."""
    if c.get("close") and (c.get("open") or not c.get("settle")):
        return c["close"]
    return c.get("settle") or c.get("close")


def build(days, sessions, rule="days:0", method="none"):
    """days: {date: [{"expiry", "open", "high", "low", "close", "settle", "oi", "volume"}]} for one
    underlying's futures; sessions: every trading day (sorted ISO strings, may run past the data so the
    sessions to a later expiry can be counted). Returns {"bars": [...], "rolls": [...], "issues": [...]}.

    Each bar: date, o, h, l, c, settle, v, oi, expiry (the contract held), adj (what was added, back) or
    factor (what multiplied, ratio). Each roll: date (first day on the new contract), from, to, old and new
    prices on the last day of the old contract, gap, ratio, why."""
    kind, n = parse_rule(rule)
    if method not in METHODS:
        raise ValueError("method is none, back or ratio")
    sess = sorted(set(sessions) | set(days))
    pos = {d: i for i, d in enumerate(sess)}

    def sessions_between(a, b):
        """Sessions after a up to and including b."""
        if b in pos:
            return pos[b] - pos[a]
        return bisect.bisect_right(sess, b) - 1 - pos[a]

    held, bars, rolls, issues = None, [], [], []
    switch_next = False
    days_prev_rows = {}
    for d in sorted(days):
        rows = {}
        for c in days[d]:
            if c.get("expiry") and c["expiry"] >= d:
                rows[c["expiry"]] = c
        if not rows:
            issues.append({"date": d, "kind": "no live futures contract"})
            continue
        exps = sorted(rows)
        if kind == "days":
            want = next((e for e in exps if sessions_between(d, e) >= n), exps[-1])
        else:
            if held is None or held not in rows:
                want = exps[0]
            elif switch_next:
                later = [e for e in exps if e > held]
                want = later[0] if later else held
            else:
                want = held
        prev = held
        missing = (pos[d] - pos[bars[-1]["date"]] - 1) if bars else 0
        if missing > 0:
            issues.append({"date": d, "kind": "sessions missing in the archive", "after": bars[-1]["date"],
                           "count": missing})
        if prev is not None and want != prev:
            last = bars[-1] if bars else None
            old_c = days_prev_rows.get(prev) if last else None
            new_c = days_prev_rows.get(want) if last else None
            old_p = _price(old_c) if old_c else None
            new_p = _price(new_c) if new_c else None
            if missing > 0:
                why = "after %d sessions missing in the archive (no adjustment across the gap)" % missing
                old_p = new_p = None
            elif kind == "days":
                why = "expiry" if n == 0 else "%d session%s before expiry" % (n, "" if n == 1 else "s")
            else:
                why = "expiry" if prev not in rows else "the next contract's %s passed the near one" % kind
            roll = {"date": d, "from": prev, "to": want, "old": old_p, "new": new_p, "why": why,
                    "gap": round(new_p - old_p, 4) if old_p and new_p else None,
                    "ratio": (new_p / old_p) if old_p and new_p else None}
            if roll["gap"] is None and missing <= 0:
                issues.append({"date": d, "kind": "roll without a price for both contracts on the last day",
                               "from": prev, "to": want})
            rolls.append(roll)
        held = want
        c = rows[want]
        p = _price(c)
        if not p:
            issues.append({"date": d, "kind": "held contract has no price", "expiry": want})
            days_prev_rows = rows
            continue
        o, h, l = c.get("open"), c.get("high"), c.get("low")
        if not (o and h and l):
            o = h = l = p
            issues.append({"date": d, "kind": "held contract did not trade (settle price used)", "expiry": want})
        bars.append({"date": d, "o": o, "h": h, "l": l, "c": p, "settle": c.get("settle"), "v": c.get("volume") or 0,
                     "oi": c.get("oi"), "expiry": want})
        if kind in ("oi", "volume"):
            later = [e for e in exps if e > want]
            nxt = rows[later[0]] if later else None
            key = "oi" if kind == "oi" else "volume"
            switch_next = bool(nxt and (nxt.get(key) or 0) > (c.get(key) or 0))
            if later and sessions_between(d, want) == 0:
                switch_next = True                     # expiry day: the next session is on the next contract
        days_prev_rows = rows
    _adjust(bars, rolls, method)
    return {"bars": bars, "rolls": rolls, "issues": issues, "rule": "%s:%d" % (kind, n) if kind == "days" else kind,
            "method": method}


def _adjust(bars, rolls, method):
    if method == "none" or not bars:
        for b in bars:
            b["adj"], b["factor"] = 0.0, 1.0
        return
    starts = {r["date"]: r for r in rolls}
    add, mul = 0.0, 1.0
    for b in reversed(bars):
        b["adj"], b["factor"] = round(add, 4), mul
        if method == "back" and add:
            for k in ("o", "h", "l", "c", "settle"):
                if b.get(k) is not None:
                    b[k] = round(b[k] + add, 2)
        elif method == "ratio" and mul != 1.0:
            for k in ("o", "h", "l", "c", "settle"):
                if b.get(k) is not None:
                    b[k] = round(b[k] * mul, 2)
        r = starts.get(b["date"])
        if r and r.get("gap") is not None:
            add += r["gap"]
            mul *= r["ratio"]
