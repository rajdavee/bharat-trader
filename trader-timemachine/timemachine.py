#!/usr/bin/env python3
"""Time Machine for trader-timemachine. NSE cash market, IST. Standard library only.

Replays a planned intraday trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data. Average R is shown gross and net of round-trip charges.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:00            # end each replay earlier than the plan's flat-by
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol HDFCBANK --date 2026-10-26 --entry 692.70 \\
        --stop 689.90 --target 698.30 --entry-time 10:10
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work:
  1. Fetch about 60 sessions of 5-minute bars (Yahoo v8 chart, SYMBOL.NS,
     range=60d interval=5m; one request). The NSE session is 09:15 to 15:30
     IST: 75 five-minute bars, the last one starting 15:25. Keep only past
     sessions (before the plan's date) that have every bar from 09:15 up to
     the flat-by time. Sessions with gaps, special sessions (Muhurat) and
     today are left out. Yahoo often leaves the 15:15 to 15:25 bars of an NSE
     session empty, which is one more reason to be flat by 15:10.
  2. The pool: for every bar slot 1..last of the day, each past session's bar
     as ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..last, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default: the plan's flat_by,
     else the config's, else 15:10 IST). You are flat by then, win or lose.
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
     Net R = R minus the round-trip charges for that exit / (risk x qty).
     qty is --qty, else the plan's charges.qty, else the most the config's max
     loss per trade allows (capped by max_order_value).
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     sessions, first close beyond the range = entry, same stop-first rule.
     Stops round to the tick away from the entry, targets to the nearest tick.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), the average R when its sign goes against the lean, and
a line when charges turn a positive gross average R negative.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 usable sessions, the entry bar is not in
the data, or the plan's day is not a regular NSE session. Same plan, same data
and same --seed give the same numbers on any machine. A simulation built from
the stock's own recent bars is not a prediction: results, news and circuit
limits can do what the last 60 sessions never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are IST (UTC+05:30, no daylight saving); --now "2026-10-26 10:15" pins the clock.
"""

import argparse
import copy
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; bharat-trader/0.1; +https://github.com/rajdavee/bharat-trader)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 75    # 09:15 to 15:30 IST in 5-minute bars (last bar starts 15:25)
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PAISE = Decimal("0.01")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

DEFAULTS = {
    "capital": 100000, "risk_per_trade_pct": 1.0, "max_loss_per_trade": 1000, "max_order_value": 50000,
    "no_entries_after": "14:45", "flat_by": "15:10", "product": "MIS", "tick_size": "auto", "include_charges": True,
    "charges": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
                "exchange_txn_pct": 0.00297, "sebi_per_crore": 10, "stamp_buy_pct": 0.003,
                "stamp_delivery_pct": 0.015, "gst_pct": 18},
    "extra_holidays": [], "special_sessions": [],
}

# NSE Capital Market trading holidays, from NSE's holiday master. 2027 is not published yet.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading timing announced separately by NSE)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)


class TMError(Exception):
    pass


# ---------------------------------------------------------------- money

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


def _dec(x):
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(str(x))


def _paise(x):
    return x.quantize(PAISE, rounding=ROUND_HALF_UP)


def round_trip_charges(entry, exit, qty, cfg):
    """Estimated charges for buying qty at entry and selling at exit, in rupees, to the paisa.

    Intraday (product MIS): brokerage on each order (brokerage_pct of its value, capped at
    brokerage_cap), STT on the sell side (stt_sell_pct), exchange transaction charge
    (exchange_txn_pct of the turnover), SEBI fee (sebi_per_crore per Rs 1 crore of turnover),
    stamp duty on the buy side (stamp_buy_pct), GST (gst_pct) on brokerage + exchange + SEBI.
    Delivery (CNC): STT stt_delivery_pct on both sides, stamp stamp_delivery_pct on the buy.
    Each part is rounded half up to the paisa from its unrounded value; total = sum of the parts."""
    ch = dict(DEFAULTS["charges"])
    ch.update((cfg or {}).get("charges") or {})
    pct = Decimal("0.01")
    q = _dec(qty)
    buy, sell = _dec(entry) * q, _dec(exit) * q
    turnover = buy + sell
    cap = _dec(ch["brokerage_cap"])
    bp = _dec(ch["brokerage_pct"]) * pct
    brokerage = min(buy * bp, cap) + min(sell * bp, cap)
    if str((cfg or {}).get("product") or "MIS").upper() == "CNC":
        stt = turnover * _dec(ch["stt_delivery_pct"]) * pct
        stamp = buy * _dec(ch["stamp_delivery_pct"]) * pct
    else:
        stt = sell * _dec(ch["stt_sell_pct"]) * pct
        stamp = buy * _dec(ch["stamp_buy_pct"]) * pct
    exchange = turnover * _dec(ch["exchange_txn_pct"]) * pct
    sebi = turnover * _dec(ch["sebi_per_crore"]) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * _dec(ch["gst_pct"]) * pct
    out = {"brokerage": _paise(brokerage), "stt": _paise(stt), "exchange": _paise(exchange),
           "sebi": _paise(sebi), "stamp": _paise(stamp), "gst": _paise(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}


def read_config(path):
    """trader/config.json over the defaults. Missing keys (or a missing file) take the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_explicit_max_loss"] = False
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (path, e))
        if not isinstance(doc, dict):
            raise TMError("%s is not a JSON object" % path)
        for k, v in doc.items():
            if v is None:
                continue
            if k == "charges" and isinstance(v, dict):
                cfg["charges"].update({ck: cv for ck, cv in v.items() if cv is not None})
            else:
                cfg[k] = v
        cfg["_explicit_max_loss"] = doc.get("max_loss_per_trade") is not None
    return cfg


def estimate_qty(entry, risk, cfg):
    """The most shares the max loss per trade allows at this risk, capped by max_order_value. At least 1.
    max loss = max_loss_per_trade if the config sets it, else capital x risk_per_trade_pct / 100."""
    if cfg.get("_explicit_max_loss"):
        ml = float(cfg["max_loss_per_trade"])
    else:
        ml = float(cfg["capital"]) * float(cfg["risk_per_trade_pct"]) / 100.0
    q = int(ml / risk + 1e-9)
    if cfg.get("max_order_value"):
        q = min(q, int(float(cfg["max_order_value"]) / entry + 1e-9))
    return max(1, q)


# ---------------------------------------------------------------- time (IST, fixed +05:30)

def ist_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)


def parse_now(text):
    """ISO time -> naive IST. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-10-26 10:15\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def parse_clock(text, what):
    """'10:10' or '14:35' (24-hour IST), '2:35 PM', or a bare '3:10' (1 to 8 o'clock means p.m.) -> minutes."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants a 24-hour IST time like 10:10 or 15:10, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def market_day(iso, cfg=None):
    """(kind, reason, warning) for a date: kind is "regular", "special" or "closed"."""
    cfg = cfg or {}
    d = datetime.date.fromisoformat(iso)
    for s in cfg.get("special_sessions") or []:
        if isinstance(s, dict) and str(s.get("date")) == iso:
            return "special", "a special session (%s to %s IST)" % (s.get("open"), s.get("close")), None
    if d.weekday() >= 5:
        return "closed", "a %s" % WEEKDAYS[d.weekday()], None
    if iso in NSE_HOLIDAYS or iso in set(str(x) for x in (cfg.get("extra_holidays") or [])):
        return "closed", "an NSE holiday (%s)" % NSE_HOLIDAYS.get(iso, "extra_holidays"), None
    if d.year not in CALENDAR_YEARS:
        return "regular", "", ("the NSE holiday calendar for %d is unknown here: treating %s as a normal trading "
                               "day. Add holidays to extra_holidays in the config" % (d.year, iso))
    return "regular", "", None


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(sym.upper() + ".NS"))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s.NS (HTTP 404): check the NSE symbol" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown symbol"))
    return res[0]


def parse_bars(result, now):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price (Yahoo leaves some NSE bars
    empty, often 15:15 to 15:25), bars off the 5-minute grid (a live tick), bars
    not complete yet (now < start + 5 min, where now is the earlier of --now and the
    data's own regularMarketTime), and bars outside 09:15 to 15:30 IST."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and ist_of(rmt) < as_of:
        as_of = ist_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        local = ist_of(ts)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if local + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def usable_sessions(days, before, last, cfg=None):
    """Past sessions (strictly before the plan's date, oldest first) with every bar
    from 09:15 through slot `last`, and the ones left out: gaps, short sessions,
    special sessions and days the calendar says NSE was shut."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        kind, reason, _ = market_day(d, cfg)
        if kind != "regular":
            short.append({"date": d, "bars": len(s), "why": reason})
            continue
        missing = [i for i in range(last + 1) if i not in s]
        if missing:
            short.append({"date": d, "bars": len(s), "why": "no %s bar" % clock24(OPEN_MIN + 5 * missing[0])})
        else:
            full.append((d, [s[i] for i in range(last + 1)]))
    return full, short


# ---------------------------------------------------------------- the simulation

def build_pool(sessions, last):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(last + 1)]
    for _, D in sessions:
        for i in range(1, last + 1):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy",
             qty=None, cfg=None):
    """Monte Carlo replays of bars k+1..last. With qty, also the average R net of charges."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(last, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    avg_net = None
    if qty:
        memo = {}

        def cost(px):
            key = round(px, 2)
            if key not in memo:
                b, s = (entry, key) if long else (key, entry)
                memo[key] = round_trip_charges(round(b, 2), round(s, 2), qty, cfg)["total"]
            return memo[key]
        nets = [R - cost(target if o == "target" else stop if o == "stop" else p) / (risk * qty)
                for R, o, p in zip(Rs, outcomes, closes)]
        avg_net = round(statistics.mean(nets), 3)
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3), "avg_R_net": avg_net,
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

# NSE sets each stock's tick from its price band and reviews it periodically.
# "auto" rounds to the coarsest tick the band can have: a multiple of it is a
# valid price on any finer grid too. A number in tick_size overrides it.
TICK_BANDS = ((1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))


def tick_for(price, setting="auto"):
    """The tick to round a price of this size to."""
    if setting not in (None, "", "auto"):
        return float(setting)
    for top, tick in TICK_BANDS:
        if float(price) < top:
            return tick
    return 5.00


def tick_away(x, long, tick=0.05):
    """Round a stop to the tick, away from the entry: down for a buy, up for a sell."""
    n = round(x / tick, 6)
    return round((math.floor(n) if long else math.ceil(n)) * tick, 2)


def tick_near(x, tick=0.05):
    """Round to the nearest tick (half up): targets and entries."""
    return round(math.floor(round(x / tick, 6) + 0.5) * tick, 2)


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, tick):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((tick_near(oh, tick) + tick_near(ol, tick)) / 2, long, tick)
    if r in ("or_low", "or_high"):
        return tick_away(tick_near(ol if long else oh, tick), long, tick)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, tick)


def history_replay(sessions, rule, last, side="buy", tick=0.05, qty=None, cfg=None):
    """The plan's rule on every past session: entry = the first bar after the
    opening range to close beyond it (its bar closing by no_entries_after, 14:45, and
    before --until), then stop first, then target, bar by bar, else the close at --until."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    cutoff = parse_clock((cfg or {}).get("no_entries_after") or DEFAULTS["no_entries_after"], "no_entries_after")
    stop_at = min(last, (cutoff - OPEN_MIN) // 5)       # the entry bar must close by no_entries_after
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, stop_at) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = tick_near(D[kk]["c"], tick)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = round((e - s) if long else (s - e), 2)
        if risk <= 0:
            continue
        t = tick_near(e + rule["target_r"] * risk, tick) if long else tick_near(e - rule["target_r"] * risk, tick)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        tr = {"date": d, "time": clock24(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
              "outcome": oc or "close", "R": round(R, 2)}
        if qty:
            px = t if oc == "target" else s if oc == "stop" else round(cl, 2)
            b_, s_ = (e, px) if long else (px, e)
            tr["charges"] = round_trip_charges(b_, s_, qty, cfg)["total"]
            tr["R_net"] = round(R - tr["charges"] / (risk * qty), 2)
        trades.append(tr)
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if (trades and qty) else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now.date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    if not TICKER.match(sym):
        raise TMError("the plan needs an NSE symbol (like HDFCBANK or M&M), got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-26, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:10)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:10) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 09:20 and 15:25 IST to leave bars to replay "
                      "(got %s)" % clock24(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    ch = raw.get("charges") if isinstance(raw.get("charges"), dict) else {}
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock24(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "tick_size": raw.get("tick_size"),
        "qty": ch.get("qty") or raw.get("qty"), "from": src,
    }


def resolve_rule(plan, entry_day, tick):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(rule_stop({"stop_rule": "or_mid"}, oh, ol, plan["entry"], long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(rule_stop({"stop_rule": far}, oh, ol, plan["entry"], long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    until = mc.get("until") or "15:10"
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (until, until)
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    until = mc.get("until") or "15:10"
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, until, until))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    net = mc.get("avg_R_net")
    if avg is not None and net is not None and avg > 0 >= net:
        notes.append("Net of charges the average R is %+.3f: at this size the charges take the whole edge." % net)
    return notes


def time_machine(text, plan, now, replays=10000, seed=19, window=2, until=None, source="yahoo", cfg=None,
                 qty=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    if qty is not None and qty < 1:
        raise TMError("--qty must be 1 or more")
    cfg = cfg if cfg is not None else read_config(None)
    sym = plan["symbol"]
    kind, reason, cal_warning = market_day(plan["date"], cfg)
    if kind == "closed":
        raise TMError("NSE is shut on %s, %s: there is no session to replay" % (plan["date"], reason))
    if kind == "special":
        raise TMError("%s is %s: the time machine replays regular 09:15 to 15:30 sessions only" % (plan["date"], reason))
    tick = float(plan.get("tick_size") or tick_for(plan["entry"], cfg.get("tick_size", DEFAULTS["tick_size"])))
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != sym + ".NS":
        raise TMError("the data is for %s but the plan is for %s.NS" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    until_text = until or plan.get("flat_by") or cfg.get("flat_by") or DEFAULTS["flat_by"]
    until_min = parse_clock(until_text, "--until" if until else "flat_by")
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN or until_min <= OPEN_MIN:
        raise TMError("--until / flat_by must be on the 5-minute grid between 09:20 and 15:30 IST (got %s)"
                      % until_text)
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("%s %s leaves nothing to replay after the %s entry"
                      % ("--until" if until else "flat_by", clock24(until_min), plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if cal_warning:
        warn.append(cal_warning)
    if not eb["l"] - tick <= plan["entry"] <= eb["h"] + tick:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = usable_sessions(days, plan["date"], last, cfg)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d usable session(s) of %s before %s with every bar to %s in the data; the time "
                      "machine needs at least %d%s"
                      % (len(sessions), sym, plan["date"], clock24(until_min), MIN_SESSIONS,
                         " (Yahoo leaves the last NSE bars of most sessions empty: use a flat-by at or before 15:15)"
                         if until_min > 15 * 60 + 15 else ""))
    include = cfg.get("include_charges", True)
    if include:
        q = qty or plan.get("qty") or estimate_qty(plan["entry"], plan["risk_per_share"], cfg)
        q = int(q)
        qsrc = "--qty" if qty else "plan" if plan.get("qty") else "estimate"
    else:
        q, qsrc = None, None
    pool = build_pool(sessions, last)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  q, cfg)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    charges = None
    if q:
        long = plan["side"] == "buy"
        b_t, s_t = (plan["entry"], plan["target"]) if long else (plan["target"], plan["entry"])
        b_s, s_s = (plan["entry"], plan["stop"]) if long else (plan["stop"], plan["entry"])
        charges = {"qty": q, "qty_source": qsrc, "product": str(cfg.get("product") or "MIS").upper(),
                   "at_target": round_trip_charges(b_t, s_t, q, cfg)["total"],
                   "at_stop": round_trip_charges(b_s, s_s, q, cfg)["total"]}
    rule = resolve_rule(plan, entry_day, tick)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], tick, q, cfg)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": (now - datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym + ".NS", "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "charges": charges,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d sessions. It is not a prediction: results, news and "
                 "circuit limits can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    ch = doc.get("charges")
    lines = [
        "%s (NSE) %s, %s %s: entry %.2f at %s IST (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s, out at the flat-by" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f gross (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"])),
    ]
    if m.get("avg_R_net") is not None and ch:
        lines.append("  Average R: %+.3f net of charges (%s at the target, %s at the stop, round trip on %d shares, %s)."
                     % (m["avg_R_net"], inr(ch["at_target"]), inr(ch["at_stop"]), ch["qty"], ch["product"]))
    else:
        lines.append("  Charges not included (include_charges is off).")
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            net = (", %+.3f net of charges" % h["avg_R_net"]) if h.get("avg_R_net") is not None else ""
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f "
                         "gross%s."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], net))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    left = doc["data"]["short_sessions_left_out"]
    if left:
        lines.append("Left out %d session(s) missing bars before %s or not regular: %s."
                     % (len(left), until, ", ".join(x["date"] for x in left)))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s IST entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    cfg = read_config(a.config)
    if a.no_charges:
        cfg["include_charges"] = False
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, cfg, a.qty)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your capital and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned intraday trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--config", default="trader/config.json", help="flat_by, tick_size, charges, holidays")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol (no .NS)")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, 24-hour IST, e.g. "10:10"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this IST time (default: the plan's flat_by, 15:10)")
        p.add_argument("--qty", type=int, help="shares for the charges (default: the plan's, or what max loss allows)")
        p.add_argument("--no-charges", action="store_true", help="gross R only")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-26 10:15" IST, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
