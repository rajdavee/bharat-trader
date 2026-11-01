"""Results (earnings) dates for event studies, from two NSE corporate filing feeds.

  api/corporate-board-meetings?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
      every board meeting intimation since 2010 (verified live back to January 2010): symbol, meeting
      date, purpose ("Financial Results", "Results/Dividend", ...), description, when NSE was told
  api/corporates-financial-results?index=equities&period=Quarterly&from_date=...&to_date=...
      every quarterly results filing since 2010: symbol, period ended, quarter, audited or not,
      standalone or consolidated, and broadCastDate: the minute NSE disseminated it

The filing's broadcast time is the event: results out after 15:30 IST can only move the price on the next
session. A board meeting with no filing in the feed is kept (the meeting date, time unknown).

Pure functions: archive.py fetches a month at a time and stores the compact rows these return.
"""

import datetime
import re

BOARD_URL = ("https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date=%s&to_date=%s")
FILINGS_URL = ("https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly"
               "&from_date=%s&to_date=%s")
BOARD_COLS = ["symbol", "meeting_date", "purpose", "informed_at", "isin", "desc"]
FILING_COLS = ["symbol", "broadcast_at", "period_from", "period_to", "quarter", "audited", "consolidated", "isin"]
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_RESULTS = re.compile(r"result", re.I)
CLOSE = "15:30"


def _day(text):
    """'31-Jan-2012' -> '2012-01-31' ('' when not a date)."""
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})", text or "")
    if not m or m.group(2).upper() not in MONTHS:
        return ""
    return "%s-%02d-%02d" % (m.group(3), MONTHS.index(m.group(2).upper()) + 1, int(m.group(1)))


def _stamp(text):
    """'31-Jan-2012 19:44:44' / '31-Jan-2012 20:56' -> '2012-01-31 19:44:44' ('' when not a stamp)."""
    d = _day(text)
    if not d:
        return ""
    m = re.search(r"(\d{1,2}):(\d\d)(?::(\d\d))?", text)
    return "%s %02d:%s:%s" % (d, int(m.group(1)), m.group(2), m.group(3) or "00") if m else d + " 00:00:00"


def board_rows(raw):
    """The board-meetings JSON -> compact rows (results meetings only), sorted."""
    out = set()
    for r in raw or []:
        purpose = " ".join(str(r.get("bm_purpose") or "").split())
        desc = " ".join(str(r.get("bm_desc") or "").split())
        if not (_RESULTS.search(purpose) or _RESULTS.search(desc[:200])):
            continue
        d = _day(r.get("bm_date"))
        sym = str(r.get("bm_symbol") or "").strip().upper()
        if not (d and sym):
            continue
        out.add((sym, d, purpose[:60], _stamp(r.get("bm_timestamp") or ""), str(r.get("sm_isin") or ""), desc[:160]))
    return [list(x) for x in sorted(out)]


def filing_rows(raw):
    """The financial-results JSON -> compact rows, sorted. The earliest of broadCastDate and filingDate
    is the dissemination time (older rows carry only one)."""
    out = set()
    for r in raw or []:
        sym = str(r.get("symbol") or "").strip().upper()
        stamps = [s for s in (_stamp(r.get("broadCastDate") or ""), _stamp(r.get("filingDate") or "")) if s]
        if not (sym and stamps):
            continue
        out.add((sym, min(stamps), _day(r.get("fromDate")), _day(r.get("toDate")), str(r.get("relatingTo") or ""),
                 str(r.get("audited") or ""), str(r.get("consolidated") or ""), str(r.get("isin") or "")))
    return [list(x) for x in sorted(out)]


def next_session(day, sessions):
    """The first session strictly after `day` from a sorted list (None when past the list)."""
    import bisect
    i = bisect.bisect_right(sessions, day)
    return sessions[i] if i < len(sessions) else None


def session_on_or_after(day, sessions):
    import bisect
    i = bisect.bisect_left(sessions, day)
    return sessions[i] if i < len(sessions) else None


def results_events(symbol, board, filings, sessions=None):
    """One stock's results events, oldest first: one per period (the earliest filing for it), plus board
    meetings that no filing explains.

    Each: {"symbol", "date" (the day the results came out), "announced_at" ('YYYY-MM-DD HH:MM:SS' or None),
    "after_close" (True/False/None), "event_session" (the first session the price could react on, when
    sessions are given), "period_end", "quarter", "meeting_date", "audited", "consolidated", "source"}."""
    sym = symbol.upper()
    by_period = {}
    for f in filings:
        if f[0] != sym:
            continue
        key = f[3] or f[1][:10]
        cur = by_period.get(key)
        if cur is None or f[1] < cur[1]:
            by_period[key] = f
    meetings = sorted(set(b[1] for b in board if b[0] == sym))
    used = set()
    out = []
    for key, f in sorted(by_period.items(), key=lambda kv: kv[1][1]):
        day = f[1][:10]
        near = [m for m in meetings if m <= day and
                (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(m)).days <= 3]
        mdate = near[-1] if near else None
        if mdate:
            used.add(mdate)
        after = f[1][11:16] >= CLOSE if len(f[1]) >= 16 else None
        ev = {"symbol": sym, "date": day, "announced_at": f[1], "after_close": after, "period_end": f[3] or None,
              "quarter": f[4] or None, "meeting_date": mdate, "audited": f[5] or None, "consolidated": f[6] or None,
              "source": "financial-results"}
        if sessions:
            ev["event_session"] = next_session(day, sessions) if after else session_on_or_after(day, sessions)
        out.append(ev)
    for m in meetings:
        if m in used or any(abs((datetime.date.fromisoformat(e["date"]) - datetime.date.fromisoformat(m)).days) <= 3
                            for e in out):
            continue
        ev = {"symbol": sym, "date": m, "announced_at": None, "after_close": None, "period_end": None,
              "quarter": None, "meeting_date": m, "audited": None, "consolidated": None, "source": "board-meeting"}
        if sessions:
            ev["event_session"] = session_on_or_after(m, sessions)
        out.append(ev)
    out.sort(key=lambda e: (e["date"], e["announced_at"] or ""))
    return out
