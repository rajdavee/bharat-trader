#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. NSE cash market, prices in rupees, times
in IST. Standard library only. Long side only. It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write
    python3 exit.py plan --symbol HDFCBANK --entry 692.70 --or-high 692.00 --or-low 687.85
    python3 exit.py plan --stop atr:1.5 --offline HDFCBANK.json   # ATR needs the session's bars
    python3 exit.py plan --symbol RELIANCE --entry 1452.30 --setup level --stop pct:1 --tick 0.10
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round to the NEAREST tick, half up):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

The tick is "tick_size" in trader/config.json ("auto": by NSE price band) or --tick.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under the larger of 0.05% of the price and two ticks (too tight to
survive the spread), an entry.json that has not triggered (unless you give
--entry), a short (longs only), a day NSE is shut.

flat_by (default 15:10 IST, or "flat_by" in the config): out by then, win or
lose. Brokers square off intraday (MIS) positions on their own around
15:15 to 15:25; the plan gets out first. On a special session (Muhurat
trading, from "special_sessions" in the config) it moves to 5 minutes before
that session's close.

Charges: with "include_charges" (default true) it estimates the round-trip
charges (brokerage, STT, exchange fee, SEBI fee, stamp duty, GST) for the
quantity you give with --qty, or the most your max loss per trade allows
(capped by max_order_value), and shows reward:risk gross and net of them.
The rates live in config "charges": check them against your broker.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import copy
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; bharat-trader/0.1; +https://github.com/rajdavee/bharat-trader)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
MIN_RISK_TICKS = 2                 # and never less than two ticks
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

DEFAULTS = {
    "capital": 100000, "risk_per_trade_pct": 1.0, "max_loss_per_trade": 1000, "max_daily_loss": 3000,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "max_position_pct": 50, "max_order_value": 50000,
    "min_order_value": 500, "cash_buffer_pct": 10, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10", "exchange": "NSE",
    "product": "MIS", "tick_size": "auto", "broker": "paper", "paper": True, "blocked": [], "allow_short": False,
    "allow_fno": False, "include_charges": True,
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
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- money and numbers

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


def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().replace("₹", "").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def paise(x):
    """Round half up to the paisa."""
    return D(x).quantize(PAISE, rounding=ROUND_HALF_UP)


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


def tick_floor(x, tick):
    """Round DOWN to a multiple of the tick (a long's stop: away from the entry)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(PAISE)


def tick_ceil(x, tick):
    return ((D(x) / tick).to_integral_value(rounding=ROUND_CEILING) * tick).quantize(PAISE)


def tick_round(x, tick):
    """Round to the NEAREST multiple of the tick, half up (targets, limits, entries)."""
    return ((D(x) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick).quantize(PAISE)


def clean_price(x, tick):
    """A Yahoo float (692.7000122070312) to the exact tick it stands for."""
    return tick_round(paise(x), tick)


def exact(x):
    """A raw rupee amount, Indian grouping, at least two decimals: 689.925 -> '₹689.925', 1163.35 -> '₹1,163.35'."""
    d = D(x)
    s = format(d.normalize(), "f")
    places = len(s.split(".")[1]) if "." in s else 0
    if places <= 2:
        return inr(d)
    head = inr(d.quantize(Decimal(1), rounding=ROUND_FLOOR) if d >= 0 else d.quantize(Decimal(1)))
    return head[:-3] + "." + s.split(".")[1]


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


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
    q = D(qty, "qty")
    buy, sell = D(entry) * q, D(exit) * q
    turnover = buy + sell
    cap = D(ch["brokerage_cap"])
    bp = D(ch["brokerage_pct"]) * pct
    brokerage = min(buy * bp, cap) + min(sell * bp, cap)
    delivery = str((cfg or {}).get("product") or "MIS").upper() == "CNC"
    if delivery:
        stt = turnover * D(ch["stt_delivery_pct"]) * pct
        stamp = buy * D(ch["stamp_delivery_pct"]) * pct
    else:
        stt = sell * D(ch["stt_sell_pct"]) * pct
        stamp = buy * D(ch["stamp_buy_pct"]) * pct
    exchange = turnover * D(ch["exchange_txn_pct"]) * pct
    sebi = turnover * D(ch["sebi_per_crore"]) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * D(ch["gst_pct"]) * pct
    out = {"brokerage": paise(brokerage), "stt": paise(stt), "exchange": paise(exchange), "sebi": paise(sebi),
           "stamp": paise(stamp), "gst": paise(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}


# ---------------------------------------------------------------- time (IST, fixed +05:30, no DST)

def ist_now():
    return datetime.datetime.now(IST).replace(microsecond=0, tzinfo=None)


def ist_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)


def parse_now(text):
    """--now -> naive IST. A time without an offset is IST."""
    if not text:
        return ist_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (ist_now().date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-10-26 10:10" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants a 24-hour IST time like 15:10, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:10, 14:35 (24-hour IST), or 2:35 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a 24-hour IST time like 10:10, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:10, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:10, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def market_day(d, cfg=None):
    """What NSE does on day d: {"open": bool, "start", "end" (times), "kind", "reason", "warning"}.
    A special session in the config (Muhurat trading) wins over a weekend or holiday."""
    cfg = cfg or {}
    iso = d.isoformat()
    for s in cfg.get("special_sessions") or []:
        if isinstance(s, dict) and str(s.get("date")) == iso:
            start = parse_hhmm(s.get("open"), "special_sessions open")
            end = parse_hhmm(s.get("close"), "special_sessions close")
            if end <= start:
                raise PlanError("special session %s closes before it opens" % iso)
            return {"open": True, "start": start, "end": end, "kind": "special",
                    "reason": "special session %s to %s IST" % (hm(start), hm(end)), "warning": None}
    if d.weekday() >= 5:
        return {"open": False, "kind": "closed", "reason": "a %s" % d.strftime("%A"), "warning": None}
    extra = set(str(x) for x in (cfg.get("extra_holidays") or []))
    if iso in NSE_HOLIDAYS or iso in extra:
        return {"open": False, "kind": "closed", "reason": "an NSE holiday (%s)" % NSE_HOLIDAYS.get(iso, "extra_holidays"),
                "warning": None}
    warning = None
    if d.year not in CALENDAR_YEARS:
        warning = ("the NSE holiday calendar for %d is unknown here: treating %s as a normal trading day. "
                   "Add holidays to extra_holidays in the config" % (d.year, iso))
    return {"open": True, "start": OPEN, "end": CLOSE, "kind": "regular", "reason": "", "warning": warning}


# ---------------------------------------------------------------- stop and target rules

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=Decimal("0.05")):
    """The stop, the target and the risk, exact to the tick. Raises PlanError with the reason."""
    tick = D(tick, "tick_size")
    if tick <= 0:
        raise PlanError("tick_size must be above 0")
    e = clean_price(entry, tick)
    if e <= 0:
        raise PlanError("the entry price must be above ₹0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = clean_price(or_high, tick), clean_price(or_low, tick)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (inr(oh), inr(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (inr(oh), inr(ol), exact(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (inr(e), num(stop_param), exact(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = tick_floor(raw, tick)
    if stop != D(raw):
        what = "rounded down to the %s tick" % inr(tick)
        detail = (detail + ", " + what if detail else "%s %s" % (exact(raw), what))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (inr(stop), label, inr(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % inr(stop))
    risk = e - stop
    floor_risk = max(e * MIN_RISK_PCT / 100, tick * MIN_RISK_TICKS)
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than the minimum %s (0.05%% of the price or %d ticks of "
                        "%s, whichever is larger): too tight to survive the spread, normal wiggle would stop you "
                        "out. Widen it." % (inr(risk), inr(floor_risk.quantize(PAISE, rounding=ROUND_CEILING)),
                                            MIN_RISK_TICKS, inr(tick)))
    if target_rule == "r":
        target = tick_round(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = tick_round(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (inr(target), inr(e)))
    rr = ((target - e) / risk).quantize(PAISE, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick}


def max_loss_of(cfg):
    """max_loss_per_trade if the config sets it, else capital x risk_per_trade_pct / 100."""
    if cfg.get("_explicit_max_loss"):
        return D(cfg["max_loss_per_trade"])
    return D(cfg["capital"]) * D(cfg["risk_per_trade_pct"]) / 100


def estimate_qty(entry, risk, cfg):
    """The most shares the max loss per trade allows at this risk, capped by max_order_value. At least 1."""
    q = int(max_loss_of(cfg) / D(risk))
    mov = cfg.get("max_order_value")
    if mov:
        q = min(q, int(D(mov) / D(entry)))
    return max(1, q)


def charges_block(c, qty, cfg, qty_source):
    """Round-trip charges at the target and at the stop, and reward:risk net of them."""
    e, s, t, risk = c["entry"], c["stop"], c["target"], c["risk"]
    at_t = round_trip_charges(e, t, qty, cfg)
    at_s = round_trip_charges(e, s, qty, cfg)
    gross_reward = (t - e) * qty
    gross_risk = risk * qty
    net_reward = gross_reward - D(at_t["total"])
    net_risk = gross_risk + D(at_s["total"])
    rr_net = (net_reward / net_risk).quantize(PAISE, rounding=ROUND_HALF_UP)
    return {"qty": qty, "qty_source": qty_source, "product": str(cfg.get("product") or "MIS").upper(),
            "at_target": at_t, "at_stop": at_s,
            "reward_gross": float(gross_reward), "reward_net": float(net_reward.quantize(PAISE)),
            "risk_gross": float(gross_risk), "risk_net": float(net_risk.quantize(PAISE)),
            "reward_risk_net": float(rr_net)}


# ---------------------------------------------------------------- bars for ATR

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_symbol(sym):
    return sym.upper() + ".NS"


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(0.5)
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None, now=None):
    ysym = yahoo_symbol(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, sym.upper() + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = ((now or ist_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), ysym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now=None, cfg=None):
    """Closed, on-grid bars of one NSE session (IST), oldest first. The live tick is dropped."""
    step = INTERVALS[interval]
    s = market_day(day, cfg)
    if not s["open"]:
        return []
    open_dt = datetime.datetime.combine(day, s["start"])
    close_dt = datetime.datetime.combine(day, s["end"])
    if now is None and chart.get("data_time"):
        now = ist_of(chart["data_time"])
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        lt = ist_of(b["t"])
        if lt.date() != day or not (open_dt <= lt < close_dt):
            continue
        if now and lt + datetime.timedelta(seconds=step) > now:
            continue
        slot = int((lt - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    """trader/config.json over the defaults. Missing keys (or a missing file) take the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_explicit_max_loss"] = False
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise PlanError("%s is not a JSON object" % path)
        for k, v in doc.items():
            if v is None:
                continue
            if k == "charges" and isinstance(v, dict):
                cfg["charges"].update({ck: cv for ck, cv in v.items() if cv is not None})
            else:
                cfg[k] = v
        cfg["_explicit_max_loss"] = doc.get("max_loss_per_trade") is not None
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). Must fall inside the session; on a special session the
    config's flat-by moves to 5 minutes before that session's close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = market_day(day, cfg)
    if s["open"]:
        if not (s["start"] < t < s["end"]):
            if explicit or s["kind"] == "regular":
                raise PlanError("%s %s is outside the %s to %s IST session on %s" % (
                    "--flat-by" if explicit else "flat_by in the config", hm(t), hm(s["start"]), hm(s["end"]), day))
            t = (datetime.datetime.combine(day, s["end"]) - datetime.timedelta(minutes=5)).time()
            note = "moved to 5 minutes before the close of the %s" % s["reason"]
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (hm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this planner does longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: this planner does longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if not TICKER.match(info["symbol"]):
        raise PlanError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO; no .NS)" % info["symbol"])
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now):
    info = gather(a)
    cfg = read_config(a.config)
    tick = D(a.tick if a.tick is not None else tick_for(info["entry"], cfg.get("tick_size", DEFAULTS["tick_size"])),
             "tick_size")
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = now.date()
    md = market_day(day, cfg)
    if not md["open"]:
        raise PlanError("NSE is shut on %s %s, %s: no session to plan for" % (day.strftime("%a"), day, md["reason"]))
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline, now)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None, cfg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, tick)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(clean_price(info["or_high"], tick)), "low": float(clean_price(info["or_low"], tick)),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "exchange": "NSE", "side": "buy",
            "date": day.isoformat(), "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick_size": float(tick), "stop_detail": c["stop_detail"],
            "created_at": (now - datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    if a.qty is not None and a.qty < 1:
        raise PlanError("--qty must be 1 or more")
    include = cfg.get("include_charges", True) and not a.no_charges
    if include:
        if a.qty is not None:
            qty, qsrc = a.qty, "--qty"
        else:
            qty, qsrc = estimate_qty(c["entry"], c["risk"], cfg), "estimate"
        plan["charges"] = charges_block(c, qty, cfg, qsrc)
        plan["reward_risk_net"] = plan["charges"]["reward_risk_net"]
    warnings = []
    if md.get("warning"):
        warnings.append(md["warning"][0].upper() + md["warning"][1:] + ".")
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    if str(cfg.get("product") or "MIS").upper() == "MIS" and flat_by > "15:15" and md["kind"] == "regular":
        warnings.append("Flat-by %s is late for MIS: brokers start their own square-off around 15:15 to 15:25 "
                        "(and may charge for it)." % flat_by)
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    if include and plan["charges"]["reward_net"] <= 0:
        warnings.append("Charges at the target (%s) eat the whole reward: this trade cannot pay at this size."
                        % inr(plan["charges"]["at_target"]["total"]))
    if day != now.date():
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), now.date().isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISE, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s IST" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s (NSE) long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (inr(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (inr(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (inr(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (inr(plan["risk_per_share"]), pct))
    ch = plan.get("charges")
    if ch:
        out.append("  Reward:risk   %s gross, %s net of charges" % (ratio(plan["reward_risk"]), ratio(ch["reward_risk_net"])))
        out.append("  Charges       %s at the target, %s at the stop, round trip on %d shares (%s, %s)" % (
            inr(ch["at_target"]["total"]), inr(ch["at_stop"]["total"]), ch["qty"], ch["product"],
            "your --qty" if ch["qty_source"] == "--qty" else "the most your max loss per trade allows"))
    else:
        out.append("  Reward:risk   %s (charges not included)" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2, ensure_ascii=False))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
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
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol", help="NSE symbol, e.g. HDFCBANK (no .NS)")
    p.add_argument("--entry", type=float, help="entry price in rupees (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, 24-hour IST, e.g. 10:10")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--tick", type=float, help="tick size in rupees (default: config tick_size, auto by price band)")
    p.add_argument("--qty", type=int, help="shares for the charges estimate (default: what your max loss allows)")
    p.add_argument("--no-charges", action="store_true", help="leave the charges estimate out")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-26 10:10"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
