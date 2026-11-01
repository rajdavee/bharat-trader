#!/usr/bin/env python3
"""Position sizing for trader-budget (NSE cash market). Standard library only, no network.

Three jobs:

  init      builds trader/config.json for you: your capital in rupees, the
            risk per trade in percent, trades per day and the product (MIS
            intraday or CNC delivery). Every other limit is derived from
            those and printed in plain words. It never overwrites an existing
            config unless you pass --force.

            python3 size.py init                                  # asks, in a terminal
            python3 size.py init --capital 100000 --risk-pct 1 --max-trades 3 --product MIS --yes

  (default) sizes ONE trade from trader/plan.json (written by trader-exit)
            and writes it to trader/orders.json as linked legs: an entry, an
            SL-M stop-loss and a LIMIT target on NSE. It never places
            anything: trader-risk checks it and trader-broker asks you yes.

            python3 size.py                                  # trader/plan.json + trader/config.json
            python3 size.py --write                          # write trader/orders.json
            python3 size.py --market                         # a MARKET entry instead of LIMIT
            python3 size.py --limit 1175.00 --write          # a LIMIT entry at 1175.00
            python3 size.py --now "2026-10-29 09:40" --json  # pin the clock (IST)
            python3 size.py --write --persona intraday-trader  # tag it for the persona scorecard

  weights   splits your capital across a list of NSE stocks by weight
            (long-term, CNC, whole shares, needs prices).

            python3 size.py --weights RELIANCE=30,TCS=25,HDFCBANK=20,INFY=15,SBIN=10
            python3 size.py --targets targets.example.json --prices trader/prices.json

How many shares. Whole shares only. The size is the smallest of:

    max loss       the most shares where qty x (entry - stop) + the round-trip
                   charges of a stop-out stay inside max_loss_per_trade
    position cap   capital x max_position_pct / entry
    max order      max_order_value / entry
    cash           (cash minus the cash buffer) / entry; cash is the cash in
                   trader/portfolio.json if you have one, else your capital

If even ONE share breaks the max loss (the stop is too far away), it refuses:
no order is written. It never shrinks the stop to fit.

The entry it sizes at: a LIMIT order's limit price (the plan's entry by
default); for a MARKET order the plan's entry, or the price in
trader/prices.json if it is fresh (under 15 minutes old) and higher, because
a price that ran up since the plan means a bigger loss at the same stop.

Charges (round_trip_charges) model a typical discount broker. VERIFY them
with your broker's charge sheet: rates change.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------- shared helpers (same in budget, risk, broker)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

CHARGES_DEFAULTS = {
    "brokerage_pct": 0.03,
    "brokerage_cap": 20,
    "stt_sell_pct": 0.025,
    "stt_delivery_pct": 0.1,
    "exchange_txn_pct": 0.00307,
    "sebi_per_crore": 10,
    "stamp_buy_pct": 0.003,
    "stamp_delivery_pct": 0.015,
    "gst_pct": 18,
}

DEFAULTS = {
    "capital": 100000,
    "risk_per_trade_pct": 1.0,
    "max_loss_per_trade": 1000,
    "max_daily_loss": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "max_position_pct": 50,
    "max_order_value": 50000,
    "min_order_value": 500,
    "cash_buffer_pct": 10,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:10",
    "broker_profile": "",
    "square_off_margin_min": 5,
    "exchange": "NSE",
    "product": "MIS",
    "tick_size": "auto",
    "broker": "paper",
    "paper": True,
    "blocked": [],
    "allow_short": False,
    "allow_fno": False,
    "include_charges": True,
    "charges": dict(CHARGES_DEFAULTS),
    "extra_holidays": [],
    "special_sessions": [],
    "fno_stocks": [],
}


def round_paise(x):
    """Round rupees to the paisa, half up (1.005 -> 1.01)."""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def inr(x):
    """123456.5 -> "₹1,23,456.50" (Indian digit grouping: lakh, crore)."""
    x = round_paise(x)
    neg = x < 0
    whole, frac = ("%.2f" % abs(x)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + whole + "." + frac


def merged_config(cfg):
    """The spec defaults with your config on top (missing keys use the defaults)."""
    c = dict(DEFAULTS)
    c["charges"] = dict(CHARGES_DEFAULTS)
    for k, v in (cfg or {}).items():
        if k == "charges" and isinstance(v, dict):
            c["charges"].update(v)
        else:
            c[k] = v
    if cfg and "max_loss_per_trade" not in cfg and cfg.get("capital") is not None:
        c["max_loss_per_trade"] = round_paise(float(c["capital"]) * float(c["risk_per_trade_pct"]) / 100.0)
    return c


def round_trip_charges(entry, exit, qty, cfg=None, segment=None, on=None, exercised=False):
    """Estimated charges for buying qty at entry and selling qty at exit (one round trip).

    brokerage  min(turnover x brokerage_pct, brokerage_cap) on EACH executed order (buy and sell)
    stt        MIS: stt_sell_pct on the sell side only. CNC: stt_delivery_pct on both sides
    exchange   exchange_txn_pct on buy + sell turnover
    sebi       sebi_per_crore rupees per crore (1,00,00,000) of buy + sell turnover
    stamp      MIS: stamp_buy_pct on the buy side. CNC: stamp_delivery_pct on the buy side
    gst        gst_pct on brokerage + exchange + sebi
    Each part is rounded half up to the paisa from its unrounded value; total is the sum of the parts."""
    seg = str(segment or (cfg.get("segment") if isinstance(cfg, dict) else None) or "EQ").upper()
    if seg != "EQ":
        return fno_round_trip_charges(entry, exit, qty, cfg, seg, on, exercised)
    c = merged_config({"charges": cfg} if (cfg and "brokerage_pct" in cfg) else cfg)
    ch = c["charges"]
    product = str(c.get("product") or "MIS").upper()

    def D(x):
        return Decimal(repr(float(x or 0)))

    pct = Decimal("0.01")
    q = D(qty)
    buy, sell = D(entry) * q, D(exit) * q
    turnover = buy + sell
    bp = D(ch.get("brokerage_pct")) * pct
    cap = ch.get("brokerage_cap")

    def brok(value):
        b = value * bp
        return min(b, D(cap)) if cap is not None else b

    brokerage = brok(buy) + brok(sell)
    if product == "CNC":
        stt = turnover * D(ch.get("stt_delivery_pct")) * pct
        stamp = buy * D(ch.get("stamp_delivery_pct")) * pct
    else:
        stt = sell * D(ch.get("stt_sell_pct")) * pct
        stamp = buy * D(ch.get("stamp_buy_pct")) * pct
    exchange = turnover * D(ch.get("exchange_txn_pct")) * pct
    sebi = turnover * D(ch.get("sebi_per_crore")) / Decimal(10000000)
    gst = (brokerage + exchange + sebi) * D(ch.get("gst_pct")) * pct

    def paisa(x):
        return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    out = {"brokerage": paisa(brokerage), "stt": paisa(stt), "exchange": paisa(exchange), "sebi": paisa(sebi),
           "stamp": paisa(stamp), "gst": paisa(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}


# ---------------------------------------------------------------- F&O charges, dated (same in budget, risk, broker, exit, timemachine, lab)
# segment "OPT" (rates on the premium) or "FUT" (on the traded value). Each row holds
# from its effective_from date until the next. Exchange = NSE transaction charge plus
# the IPFT contribution (one number on a contract note). Sources: docs/data-sources.md.
# "fno_charges": {"OPT": {...}, "FUT": {...}} in the config overrides any rate.
FNO_CHARGES = [
    {"effective_from": "2024-10-01",
     "OPT": {"brokerage_flat": 20, "stt_sell_pct": 0.1, "stt_exercise_pct": 0.125, "exchange_txn_pct": 0.03553,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
     "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.02, "exchange_txn_pct": 0.00183,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.002, "gst_pct": 18},
     "source": "NSE/FA/64323 (27 Sep 2024) and NSE/FA/73061 (27 Feb 2026, total outflow); STT up to 31 Mar 2026 "
               "per NSE/FATAX/73524"},
    {"effective_from": "2026-04-01",
     "OPT": {"brokerage_flat": 20, "stt_sell_pct": 0.15, "stt_exercise_pct": 0.15, "exchange_txn_pct": 0.03553,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
     "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.05, "exchange_txn_pct": 0.00183,
             "sebi_per_crore": 10, "stamp_buy_pct": 0.002, "gst_pct": 18},
     "source": "Finance Act 2026, NSE/FATAX/73524 (31 Mar 2026); NSE/FA/73061"},
]


def fno_rates(segment, on=None, cfg=None):
    """The dated F&O rates for "OPT" or "FUT" on date `on` (default today), with the config's overrides."""
    seg = str(segment).upper()
    if seg not in ("OPT", "FUT"):
        raise ValueError("segment is EQ, OPT or FUT, not %r" % segment)
    day = on if isinstance(on, str) else (on or datetime.date.today()).isoformat()
    row = FNO_CHARGES[0]
    for r in FNO_CHARGES:
        if r["effective_from"] <= day:
            row = r
    rates = dict(row[seg])
    over = ((cfg or {}).get("fno_charges") or {}).get(seg) if isinstance(cfg, dict) else None
    if isinstance(over, dict):
        rates.update({k: v for k, v in over.items() if v is not None})
    return rates


def fno_round_trip_charges(entry, exit, qty, cfg=None, segment="OPT", on=None, exercised=False):
    """Buy qty at entry, sell at exit, for options (prices are premiums) or futures.

    brokerage  OPT: brokerage_flat per executed order. FUT: lower of brokerage_pct and brokerage_cap per order
    stt        stt_sell_pct on the sell side. OPT with exercised=True: no sell order; exit is the
               intrinsic value per unit and STT is stt_exercise_pct of it (paid by the buyer)
    exchange   exchange_txn_pct on buy + sell turnover (the exercise leg has none)
    sebi       sebi_per_crore rupees per crore of that turnover
    stamp      stamp_buy_pct on the buy side
    gst        gst_pct on brokerage + exchange + sebi
    Each part is rounded half up to the paisa; total is the sum of the parts."""
    seg = str(segment).upper()
    ch = fno_rates(seg, on, cfg)

    def dec(x):
        return Decimal(repr(float(x or 0)))

    pct = Decimal("0.01")
    q = dec(qty)
    buy, sell = dec(entry) * q, dec(exit) * q
    exercise = bool(exercised) and seg == "OPT"
    traded = buy + (Decimal(0) if exercise else sell)

    def brok(value):
        if seg == "OPT":
            return dec(ch.get("brokerage_flat")) if value > 0 else Decimal(0)
        b = value * dec(ch.get("brokerage_pct")) * pct
        cap = ch.get("brokerage_cap")
        return min(b, dec(cap)) if cap is not None else b

    brokerage = brok(buy) + (Decimal(0) if exercise else brok(sell))
    stt = sell * dec(ch.get("stt_exercise_pct" if exercise else "stt_sell_pct")) * pct
    exchange = traded * dec(ch.get("exchange_txn_pct")) * pct
    sebi = traded * dec(ch.get("sebi_per_crore")) / Decimal(10000000)
    stamp = buy * dec(ch.get("stamp_buy_pct")) * pct
    gst = (brokerage + exchange + sebi) * dec(ch.get("gst_pct")) * pct

    def paisa(x):
        return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    out = {"brokerage": paisa(brokerage), "stt": paisa(stt), "exchange": paisa(exchange), "sebi": paisa(sebi),
           "stamp": paisa(stamp), "gst": paisa(gst)}
    out["total"] = sum(out.values(), Decimal("0"))
    return {k: float(v) for k, v in out.items()}

# ---------------------------------------------------------------- end of F&O charges


def to_paise(x):
    return int(Decimal(repr(float(x))).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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


def tick_round(x, tick=0.05, how="nearest"):
    """Round a price to the tick: "nearest" (half up) or "down"."""
    p, t = to_paise(x), max(to_paise(tick), 1)
    n = p // t if how == "down" else (p + t // 2) // t
    return round(n * t / 100.0, 2)


# ---------------------------------------------------------------- the rest of trader-budget

EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
LIMIT_WORDS = {"max loss": "max loss per trade", "position cap": "position cap",
               "max order": "max order value", "cash": "cash after the buffer"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, fixed +05:30)

def parse_now(text):
    """ISO time -> aware IST datetime. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-29 09:40\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def parse_time(text):
    """A quote time -> aware IST datetime, or None. No offset = IST."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def utc_iso(now=None):
    now = now or datetime.datetime.now(IST)
    return now.astimezone(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    if path and os.path.exists(path):
        return merged_config(read_json(path, "config"))
    return merged_config({})


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_entry(doc, sym):
    """(price, time) for sym from prices.json. Accepts the upstream shape
    {"as_of", "prices": {SYM: p}, "detail": {SYM: {"time"}}} and the flat shape
    {SYM: {"price", "prev_close", "time", "source"}}."""
    if not isinstance(doc, dict):
        return None, None
    if isinstance(doc.get("prices"), dict):
        p = _f(doc["prices"].get(sym))
        det = (doc.get("detail") or {}).get(sym) or {}
        t = parse_time(det.get("time")) or parse_time(det.get("fetched")) or parse_time(doc.get("as_of"))
        return p, t
    row = doc.get(sym)
    if isinstance(row, dict):
        return _f(row.get("price")), parse_time(row.get("time"))
    return _f(row), parse_time(doc.get("as_of"))


def fresh_price(doc, sym, now, minutes=FRESH_MINUTES):
    """(price, time, fresh) for sym, or (None, None, False)."""
    p, t = price_entry(doc, sym)
    if p is None or p <= 0:
        return None, None, False
    if t is None:
        return p, None, False
    age = (now - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def load_prices(path):
    """{SYM: price} from a prices.json (either shape)."""
    if not path:
        return {}
    doc = read_json(path, "prices")
    if not isinstance(doc, dict):
        return {}
    syms = doc["prices"].keys() if isinstance(doc.get("prices"), dict) else doc.keys()
    out = {}
    for s in syms:
        p, _ = price_entry(doc, s)
        if p and p > 0:
            out[str(s).upper()] = p
    return out


def _num(x):
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def _p(x):
    return "%.2f" % round_paise(x)


def shares_for(rupees, per_share):
    """Whole shares that fit in rupees at per_share, in exact paise."""
    r, p = to_paise(rupees), to_paise(per_share)
    if p <= 0 or r <= 0:
        return 0
    return r // p


# ---------------------------------------------------------------- init: build trader/config.json

# the MIS square-off profiles the session tools know (BROKER_SQUARE_OFF in entry, exit, risk...)
BROKER_PROFILE_NAMES = ("angel", "dhan", "groww", "upstox", "zerodha", "custom")


def build_config(capital, risk_pct=1.0, max_trades=3, product="MIS", max_position_pct=None, broker_profile=None):
    """Your four answers -> a full config (the spec keys), every limit derived."""
    capital, risk_pct = _f(capital), _f(risk_pct)
    if capital is None or capital <= 0:
        raise SizeError("capital must be a positive number of rupees, like 100000")
    if capital < 5000:
        raise SizeError("capital %s is too small to size whole NSE shares with a stop; use at least ₹5,000"
                        % inr(capital))
    if risk_pct is None or not 0 < risk_pct <= 5:
        raise SizeError("risk per trade must be above 0 and at most 5 percent of capital (1 is common)")
    try:
        max_trades = int(max_trades)
    except (TypeError, ValueError):
        raise SizeError("max trades per day must be a whole number")
    if not 1 <= max_trades <= 20:
        raise SizeError("max trades per day must be between 1 and 20")
    product = str(product or "MIS").strip().upper()
    if product not in ("MIS", "CNC"):
        raise SizeError("product must be MIS (intraday) or CNC (delivery), got %r" % product)
    profile = str(broker_profile or "").strip().lower()
    if profile and profile not in BROKER_PROFILE_NAMES:
        raise SizeError("broker profile is one of %s, not %r" % (", ".join(BROKER_PROFILE_NAMES), broker_profile))
    if profile == "custom":
        raise SizeError("a custom broker profile needs your square-off times: set \"broker_profile\": \"custom\" and "
                        "\"square_off\" in the config by hand")
    pos_pct = _f(max_position_pct) if max_position_pct is not None else float(DEFAULTS["max_position_pct"])
    if pos_pct is None or not 0 < pos_pct <= 100:
        raise SizeError("max position must be above 0 and at most 100 percent")
    def clean(x):
        x = round_paise(x)
        return int(x) if x == int(x) else x

    c = merged_config({})
    max_loss = round_paise(capital * risk_pct / 100.0)
    c.update({
        "capital": clean(capital),
        "risk_per_trade_pct": risk_pct,
        "max_loss_per_trade": clean(max_loss),
        "max_daily_loss": clean(3 * max_loss),
        "max_trades_per_day": max_trades,
        "max_orders_per_day": max(10, 3 * max_trades),
        "max_position_pct": clean(pos_pct),
        "max_order_value": clean(capital * pos_pct / 100.0),
        "product": product,
        "broker_profile": profile,
    })
    return c


def describe_config(c):
    """The limits in plain words."""
    mis = str(c.get("product")).upper() == "MIS"
    lines = [
        "Capital %s, product %s (%s)." % (inr(c["capital"]), c["product"],
                                          ("intraday: %s squares off MIS positions; this pack exits 5 minutes "
                                           "before it and before an F&O stock's closing auction" % c["broker_profile"])
                                          if mis and c.get("broker_profile") else
                                          "intraday: brokers square off MIS positions from about 15:05 to 15:26 IST "
                                          "(earlier for F&O stocks); this pack exits first" if mis
                                          else "delivery: you keep the shares overnight"),
        "  - Most one trade may lose at its stop, charges included: %s (%s%% of capital)."
        % (inr(c["max_loss_per_trade"]), _num(c["risk_per_trade_pct"])),
        "  - Most one day may lose: %s. After that, no new entries today." % inr(c["max_daily_loss"]),
        "  - Up to %d trades a day and %d orders a day." % (c["max_trades_per_day"], c["max_orders_per_day"]),
        "  - Biggest single order: %s (%s%% of capital). Smallest: %s."
        % (inr(c["max_order_value"]), _num(c["max_position_pct"]), inr(c["min_order_value"])),
        "  - Always kept in cash: %s (%s%%)." % (inr(float(c["capital"]) * float(c["cash_buffer_pct"]) / 100.0),
                                               _num(c["cash_buffer_pct"])),
        "  - Every entry needs a stop, and the target must pay at least %s times the risk." % _num(c["min_reward_risk"]),
        "  - New entries only between %s and %s IST%s." % (c["no_entries_before"], c["no_entries_after"],
                                                         ("; flat by %s IST" % c["flat_by"]) if mis else ""),
        "  - Charges (brokerage, STT, exchange, SEBI, stamp duty, GST) are %s. The rates are a typical discount "
        "broker's: check them against your broker's charge sheet." % ("counted in every loss" if c["include_charges"]
                                                                       else "NOT counted"),
        "  - Paper trading: %s. Short selling: %s. F&O: %s." % ("on" if c["paper"] else "OFF (real money)",
                                                               "allowed" if c["allow_short"] else "off",
                                                               "allowed" if c["allow_fno"] else "off"),
    ]
    return "\n".join(lines)


def run_init(a, ask=None, interactive=None):
    """size.py init. ask(prompt) -> str is input() by default; interactive defaults to stdin being a TTY."""
    path = a.config
    if os.path.exists(path) and not a.force:
        raise SizeError("%s already exists: not overwritten. Pass --force to replace it, or edit it by hand"
                        % path)
    if interactive is None:
        interactive = sys.stdin.isatty() and not a.yes
    ask = ask or input
    capital, risk, trades, product = a.capital, a.risk_pct, a.max_trades, a.product
    if interactive:
        def q(prompt, default):
            got = ask("%s%s: " % (prompt, (" [%s]" % default) if default is not None else "")).strip()
            return got.replace(",", "").replace("₹", "") if got else default
        print("Setting up your trading rules. Only use money you can afford to lose.")
        capital = q("Capital for trading, in rupees", capital)
        risk = q("Most you will risk on one trade, in percent of capital", risk if risk is not None else 1)
        trades = q("Most trades in one day", trades if trades is not None else 3)
        product = q("Product: MIS (intraday) or CNC (delivery)", product or "MIS")
    elif not a.yes:
        raise SizeError("no terminal to ask in: pass --capital, --risk-pct, --max-trades, --product and --yes")
    if capital is None:
        raise SizeError("capital is required: --capital 100000")
    c = build_config(capital, 1 if risk is None else risk, 3 if trades is None else trades, product or "MIS",
                     a.max_position_pct, getattr(a, "broker_profile", None))
    print(describe_config(c))
    if interactive and not a.yes:
        ok = ask("Write these rules to %s? Type yes to save: " % path).strip().lower()
        if ok not in ("yes", "y"):
            print("Not saved.")
            return 1
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(c, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    print("Saved %s. Change any limit by editing that file; paper trading stays on until you turn it off." % path)
    return 0


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, capital=None, limit=None, price=None, cash=None, today=None, market=False):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit   the LIMIT entry price (None = the plan's entry, unless market)
    market  a MARKET entry: sized at the higher of the plan's entry and price
    price   a FRESH latest price
    cash    the cash in trader/portfolio.json, if there is one
    today   IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = merged_config(cfg)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit): every order here has an entry, "
                            "a stop-loss and a target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)
    capital = float(capital if capital is not None else (c.get("capital") or 0))
    if capital <= 0:
        raise SizeError("no capital: run size.py init, or pass --capital")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    tick = tick_for(entry_plan, c.get("tick_size"))
    entry_plan = tick_round(entry_plan, tick)
    if limit is not None:
        ref, ref_from, order_type = tick_round(limit, tick), "limit", "LIMIT"
    elif market:
        order_type = "MARKET"
        if price is not None and tick_round(price, tick) > entry_plan:
            ref, ref_from = tick_round(price, tick), "price"
        else:
            ref, ref_from = entry_plan, "plan"
    else:
        ref, ref_from, order_type = entry_plan, "plan limit", "LIMIT"
    stop, target = tick_round(stop, tick, "down"), tick_round(target, tick)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_p(stop), _p(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_p(target), _p(ref)))
    risk_p = to_paise(ref) - to_paise(stop)
    reward_p = to_paise(target) - to_paise(ref)
    risk = risk_p / 100.0

    cash_buffer = round_paise(capital * buf_pct / 100.0)
    available = capital - cash_buffer
    cash_note = "capital %s minus the %s cash buffer" % (inr(capital), inr(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(round_paise(available), 0.0)

    with_charges = bool(c.get("include_charges", True))

    def loss_at_stop(q):
        gross = round_paise(q * risk)
        ch = round_trip_charges(ref, stop, q, c)["total"] if (with_charges and q > 0) else 0.0
        return gross, ch, round_paise(gross + ch)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade"))
    if max_loss:
        q = shares_for(max_loss, risk)
        while q > 0 and loss_at_stop(q)[2] > max_loss + 0.005:
            q -= 1
        how = "%s / %s risk a share" % (inr(max_loss), _p(risk))
        if with_charges:
            how += ", charges included"
        limits.append({"name": "max loss", "rupees": max_loss, "shares": q, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round_paise(capital * cap_pct / 100.0)
        limits.append({"name": "position cap", "rupees": cap, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(capital), inr(cap), _p(ref))})
    max_order = _f(c.get("max_order_value"))
    if max_order:
        limits.append({"name": "max order", "rupees": max_order, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (inr(max_order), _p(ref))})
    limits.append({"name": "cash", "rupees": available, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, inr(available), _p(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    gross, charges, total_loss = loss_at_stop(qty)
    value = round_paise(qty * ref)
    why_not = None           # [message key, its values]: the refusal in any language
    if qty == 0:
        if bound[0] == "max loss":
            g1, ch1, one = loss_at_stop(1)
            why_not = ["refuse_max_loss", {"loss": inr(one), "entry": _p(ref), "stop": _p(stop),
                                           "charges": (", plus %s charges" % inr(ch1)) if ch1 else "",
                                           "limit": inr(max_loss)}]
        else:
            lim = limits[[l["name"] for l in limits].index(bound[0])]
            why_not = ["refuse_cost", {"sym": sym, "price": inr(ref), "limit": LIMIT_WORDS[bound[0]],
                                       "how": lim["how"]}]
    elif _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        why_not = ["refuse_rr", {"rr": "%.2f" % (reward_p / float(risk_p)), "target": _p(target), "entry": _p(ref),
                                 "stop": _p(stop), "min": _num(c["min_reward_risk"])}]
    elif value < float(c.get("min_order_value") or 0) - 0.005:
        why_not = ["refuse_min", {"shares": "%d share%s" % (qty, "" if qty == 1 else "s"), "sym": sym,
                                  "value": inr(value), "min": inr(c["min_order_value"])}]
    refusal = t(why_not[0], "en", **why_not[1]) if why_not else None
    return {
        "symbol": sym, "side": "BUY", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan,
        "entry": ref, "entry_from": ref_from, "order_type": order_type, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "capital": capital, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade": max_loss, "include_charges": with_charges, "limits": limits, "qty": qty,
        "bound_by": bound[0], "value": value, "max_loss": gross, "charges": charges,
        "max_loss_with_charges": total_loss, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "product": str(c.get("product") or "MIS").upper(), "exchange": str(c.get("exchange") or "NSE").upper(),
        "refusal": refusal, "refusal_msg": why_not,
    }


def leg_order(s):
    """The one proposed order in the broker-neutral shape (entry + SL-M stop + LIMIT target)."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "trade")
    lim = [l for l in s["limits"] if l["binds"]][0]
    why = ("%s on %s: %d shares, bound by your %s (%s). At the stop: %d x %s = %s"
           % (setup, s["symbol"], s["qty"], LIMIT_WORDS[s["bound_by"]], lim["how"], s["qty"],
              _p(s["risk_per_share"]), inr(s["max_loss"])))
    if s["include_charges"]:
        why += " plus about %s charges = %s" % (inr(s["charges"]), inr(s["max_loss_with_charges"]))
    if s["max_loss_per_trade"]:
        why += ", inside your %s max loss per trade" % inr(s["max_loss_per_trade"])
    why += "."
    return {
        "symbol": s["symbol"], "exchange": s["exchange"], "product": s["product"], "side": "BUY",
        "qty": int(s["qty"]), "order_type": s["order_type"],
        "price": s["entry"] if s["order_type"] == "LIMIT" else None,
        "stop_loss": {"order_type": "SL-M", "trigger_price": s["stop"]},
        "target": {"order_type": "LIMIT", "price": s["target"]},
        "validity": "DAY",
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "charges": s["charges"],
                 "max_loss_with_charges": s["max_loss_with_charges"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    }


MSG = {
    "en": {
        "head": "{sym} buy, {setup}, entry {entry}{at} IST",
        "at": " at {time}",
        "stop_target": "Stop {stop}{sl}. Target {target}{tl}.",
        "src_plan": "the plan's entry",
        "src_plan limit": "a LIMIT at the plan's entry",
        "src_price": "the latest price, above the plan's entry",
        "src_limit": "your limit price",
        "sized": "Sized at {entry} ({src}). Risk {risk} a share (entry {entry} minus stop {stop}). Reward:risk {rr}.",
        "limits": "Limits (whole shares; the smallest one wins):",
        "binds": "  <- binds",
        "no_order": "No order: {why}",
        "size": "Size: {qty} shares of {sym} = {value} at {entry}. At the stop: {qty} x {risk} = {loss}{ch}.",
        "size_ch": " plus about {charges} charges (brokerage, STT, exchange, SEBI, stamp, GST) = {total}",
        "stop_promise": "A stop is an order, not a promise: in a fast market it can fill below {stop}.",
        "proposed": "Proposed order ({exch} {prod}, nothing has been placed):",
        "legs": "  BUY {qty} {sym}, {how}, DAY; then SL-M stop {stop} and LIMIT target {target} (when one fills, "
                "cancel the other)",
        "flat_by": "Flat by {time} IST: anything still open then gets closed (trader-alerts rings, trader-broker "
                   "flatten), before the broker's own MIS square-off.",
        "next": "Next: trader-risk checks it, then trader-broker asks you yes or no.",
        "disclaimer": "Education and tooling, not financial advice.",
        "wrote": "Wrote 1 proposed order to {path}",
        "note_market": "{sym} last {price} (at {time} IST) is above the plan's entry {entry}, so the risk is bigger: "
                       "sized at {price}.",
        "note_chase": "{sym} last {price} (at {time} IST) is above your LIMIT {limit}: the order may not fill. "
                      "Do not chase: a higher entry means a bigger loss at the same stop.",
        "refuse_max_loss": "one share risks {loss} at the stop (entry {entry} minus stop {stop}{charges}), more than "
                           "your {limit} max loss per trade. No order: it is not shrunk to fit. Skip this trade, or "
                           "plan a closer stop with trader-exit.",
        "refuse_cost": "one share of {sym} costs {price}, more than your {limit} ({how}). No order.",
        "refuse_rr": "reward:risk {rr} (target {target}, entry {entry}, stop {stop}) is under your minimum {min}, so "
                     "trader-risk would block it. No order. Skip it, or make a new plan with trader-exit.",
        "refuse_min": "{shares} of {sym} is {value}, under your {min} minimum order. No order.",
    },
    "hi-en": {
        "head": "{sym} buy, {setup}, entry {entry}{at} IST",
        "at": ", {time}",
        "stop_target": "Stop {stop} pe hai{sl}. Target {target}{tl}.",
        "src_plan": "plan ki entry",
        "src_plan limit": "plan ki entry pe LIMIT",
        "src_price": "latest price, jo plan ki entry se upar hai",
        "src_limit": "aapka limit price",
        "sized": "Size {entry} pe nikala ({src}). Risk {risk} per share hai (entry {entry} minus stop {stop}). "
                 "Reward:risk {rr} hai.",
        "limits": "Limits (sirf poore shares; jo sabse chhota hai, wahi lagta hai):",
        "binds": "  <- yahi lagta hai",
        "no_order": "Order nahi banega: {why}",
        "size": "Size: {sym} ke {qty} shares = {value}, {entry} pe. Stop lagne pe nuksaan: {qty} x {risk} = {loss}{ch}.",
        "size_ch": " plus lagbhag {charges} charges (brokerage, STT, exchange, SEBI, stamp, GST) = {total}",
        "stop_promise": "Stop ek order hai, guarantee nahi: tez market mein yeh {stop} se neeche bhi fill ho sakta hai.",
        "proposed": "Proposed order ({exch} {prod}, abhi kuch bhi place nahi hua hai):",
        "legs": "  BUY {qty} {sym}, {how}, DAY; phir SL-M stop {stop} aur LIMIT target {target} (ek fill ho to "
                "doosra cancel)",
        "flat_by": "{time} IST tak flat: tab tak jo bhi open hai woh close hoga (trader-alerts ring karega, "
                   "trader-broker flatten), broker ke apne MIS square-off se pehle.",
        "next": "Aage: trader-risk ise check karega, phir trader-broker aapse yes ya no poochega.",
        "disclaimer": "Yeh education aur tooling hai, financial advice nahi.",
        "wrote": "1 proposed order {path} mein likh diya",
        "note_market": "{sym} ka last price {price} ({time} IST) plan ki entry {entry} se upar hai, isliye risk zyada "
                       "hai: size {price} pe nikala.",
        "note_chase": "{sym} ka last price {price} ({time} IST) aapke LIMIT {limit} se upar hai: order shayad fill na "
                      "ho. Peeche mat bhaagiye: oonchi entry matlab usi stop pe bada nuksaan.",
        "refuse_max_loss": "ek share ka risk hi stop pe {loss} hai (entry {entry} minus stop {stop}{charges}), jo "
                           "aapke {limit} max loss per trade se zyada hai. Order nahi: ise fit karne ke liye chhota "
                           "nahi kiya jaata. Yeh trade chhod dijiye, ya trader-exit se paas ka stop plan kijiye.",
        "refuse_cost": "{sym} ka ek share {price} ka hai, jo aapke {limit} ({how}) se zyada hai. Order nahi.",
        "refuse_rr": "reward:risk {rr} (target {target}, entry {entry}, stop {stop}) aapke minimum {min} se kam hai, "
                     "isliye trader-risk ise block karega. Order nahi. Ise chhod dijiye, ya trader-exit se naya plan "
                     "banaiye.",
        "refuse_min": "{sym} ke {shares} ki value {value} hai, aapke {min} minimum order se kam. Order nahi.",
    },
}


# ---------------------------------------------------------------- message catalogue helper (same in every skill with MSG)
LANGS = ("en", "hi-en")


def pick_lang(flag=None, cfg=None):
    """The output language: --lang beats the config's "lang"; "hi-en" is Hinglish (Roman script), else English."""
    for v in (flag, cfg.get("lang") if isinstance(cfg, dict) else None):
        v = str(v or "").strip().lower().replace("_", "-")
        if v == "hinglish":
            v = "hi-en"
        if v in LANGS:
            return v
    return "en"


def t(key, lang="en", **kw):
    """MSG[lang][key] filled with kw (str.format); the English text when a language or key is missing."""
    text = (MSG.get(lang) or {}).get(key)
    if text is None:
        text = MSG["en"][key]
    return text.format(**kw) if kw else text
# ---------------------------------------------------------------- end of message catalogue helper


def plan_report(s, order, notes=(), lang="en"):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append(t("head", lang, sym=s["symbol"], setup=setup, entry=_p(s["entry_plan"]),
                   at=t("at", lang, time=s["entry_time"]) if s["entry_time"] else ""))
    lines.append(t("stop_target", lang, stop=_p(s["stop"]), sl=(": " + s["stop_label"]) if s["stop_label"] else "",
                   target=_p(s["target"]), tl=(": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    lines.append(t("sized", lang, entry=_p(s["entry"]), src=t("src_" + s["entry_from"], lang),
                   risk=_p(s["risk_per_share"]), stop=_p(s["stop"]), rr="%.2f" % s["reward_risk"]))
    lines.append("")
    lines.append(t("limits", lang))
    for l in s["limits"]:
        lines.append("  %-13s %-66s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      t("binds", lang) if l["binds"] else ""))
    lines.append("")
    if order is None:
        why = s.get("refusal_msg")
        lines.append(t("no_order", lang, why=t(why[0], lang, **why[1]) if why else s["refusal"]))
        lines.append(t("disclaimer", lang))
        return "\n".join(lines)
    lines.append(t("size", lang, qty=s["qty"], sym=s["symbol"], value=inr(s["value"]), entry=_p(s["entry"]),
                   risk=_p(s["risk_per_share"]), loss=inr(s["max_loss"]),
                   ch=t("size_ch", lang, charges=inr(s["charges"]), total=inr(s["max_loss_with_charges"]))
                   if s["include_charges"] else ""))
    lines.append(t("stop_promise", lang, stop=_p(s["stop"])))
    lines.append("")
    lines.append(t("proposed", lang, exch=order["exchange"], prod=order["product"]))
    how = "LIMIT %s" % _p(order["price"]) if order["order_type"] == "LIMIT" else "MARKET"
    lines.append(t("legs", lang, qty=order["qty"], sym=order["symbol"], how=how,
                   stop=_p(order["stop_loss"]["trigger_price"]), target=_p(order["target"]["price"])))
    if s["flat_by"] and s["product"] == "MIS":
        lines.append(t("flat_by", lang, time=s["flat_by"]))
    lines.append("")
    lines.append(t("next", lang))
    lines.append(t("disclaimer", lang))
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (long-term split, CNC)

def parse_weights(data):
    """A weights file, a {symbol: weight} dict or a list of rows -> [{"symbol", "name", "weight"}]."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"symbol": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("symbol") or r.get("ticker")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a symbol and a weight" % i)
            rows.append({"symbol": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["symbol"], r["weight"]))
        out.append({"symbol": str(r["symbol"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=30,TCS=25 -> {symbol: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.rsplit("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["symbol"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["symbol"])
        seen.add(r["symbol"])
        if r["symbol"] in blocked:
            dropped.append({"symbol": r["symbol"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"symbol": r["symbol"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"symbol": r["symbol"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact rupees per symbol with the cap enforced: proportional to weight, any
    stock over the cap is pinned at it and the rest shared out, until nobody is over."""
    alloc, capped = {}, set()
    active = list(rows)
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["symbol"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["symbol"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["symbol"]] = cap
            capped.add(r["symbol"])
            money_left -= cap
        active = [r for r in active if r["symbol"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["symbol"]] = 0.0
            break
    return alloc, capped


def whole_shares(rupees, prices, order, cap, cash):
    """Floor each slice to whole shares, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {t: (int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0) for t in order}
    left = cash - sum(shares[t] * prices[t] for t in order)
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, capital=None, prices=None, top=None):
    """Weights mode. Pure function: weights + rules + prices -> whole shares per stock."""
    c = merged_config(cfg)
    capital = float(capital if capital is not None else (c.get("capital") or 0))
    if capital <= 0:
        raise SizeError("no capital: run size.py init, or pass --capital")
    buf_pct = float(c.get("cash_buffer_pct", 10))
    cap_pct = float(c.get("max_position_pct", 50))
    min_order = float(c.get("min_order_value", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}
    keep, dropped = normalise(rows, c.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")
    missing = [r["symbol"] for r in keep if not prices.get(r["symbol"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, e.g. with "
                        "trader-alerts: alerts.py quote %s --write)" % (", ".join(missing), " ".join(missing)))
    cash_buffer = round_paise(capital * buf_pct / 100.0)
    investable = round_paise(capital - cash_buffer)
    cap = round_paise(capital * cap_pct / 100.0)
    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["symbol"] for r in sorted(live, key=lambda r: -exact[r["symbol"]])]
        shares = whole_shares(exact, prices, order, cap, investable)
        rupees = {t: round_paise(shares[t] * prices[t]) for t in order}
        small = [r for r in live if rupees[r["symbol"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["symbol"]))
        if shares[worst["symbol"]] == 0:
            reason = "one share costs %s, more than its slice; it cannot be bought in whole shares" % (
                inr(prices[worst["symbol"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (inr(rupees[worst["symbol"]]), inr(min_order))
        dropped.append({"symbol": worst["symbol"], "reason": reason})
        keep = [r for r in keep if r["symbol"] != worst["symbol"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the capital or hold fewer stocks")
    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["symbol"]
        positions.append({"symbol": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "rupees": rupees[t], "price": prices[t],
                          "shares": shares[t], "capped": t in capped})
    invested = round_paise(sum(p["rupees"] for p in positions))
    return {"capital": capital, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "investable": investable,
            "max_position_pct": cap_pct, "cap": cap, "min_order_value": min_order, "positions": positions,
            "invested": invested, "idle": round_paise(investable - invested), "dropped": dropped,
            "exchange": str(c.get("exchange") or "NSE").upper()}


def orders_from(result, source=""):
    """Weights mode: one CNC MARKET buy per position, in the broker-neutral shape. No stop."""
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of %s capital" % (_num(result["max_position_pct"]), inr(result["capital"]))
        out.append({"symbol": p["symbol"], "exchange": result["exchange"], "product": "CNC", "side": "BUY",
                    "qty": int(p["shares"]), "order_type": "MARKET", "price": None, "validity": "DAY",
                    "why": why})
    return out


def report(result, orders, source=""):
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Capital %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (inr(r["capital"]), _num(r["cash_buffer_pct"]), inr(r["cash_buffer"]), inr(r["investable"]),
                    _num(r["max_position_pct"]), inr(r["cap"])))
    lines.append("")
    lines.append("%-12s %-22s %8s %14s %11s %7s  note" % ("symbol", "name", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %14s %11s %7d" % (p["symbol"], (p["name"] or "")[:22], p["weight"],
                                                       inr(p["rupees"]), _p(p["price"]), p["shares"])
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %14s" % ("", "invested", "", inr(r["invested"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "cash buffer", "", inr(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "left idle", "", inr(r["idle"])))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["symbol"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (CNC delivery, nothing has been placed):")
    for o in orders:
        lines.append("  BUY %d %s, MARKET, DAY" % (o["qty"], o["symbol"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day limits to every buy. Use a separate config (--config) for a long-term split.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- persona tag (same in budget and strategy)
# An optional tag naming the agent persona that drafted a proposal (agents/<name>.md). It rides in each
# order's "plan" note, which is never sent to a broker and is not part of the trader-risk fingerprint,
# and the tagged proposal is logged in trader/state/personas.json so the persona scorecard can credit
# the paper fills to it after trader/orders.json is replaced. It changes no size, no check, no verdict.

PERSONA_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
PERSONA_LOG_MAX = 2000


def persona_name(text):
    """The persona tag, lower case (e.g. intraday-trader), or None. ValueError for a bad name."""
    if text is None or not str(text).strip():
        return None
    name = str(text).strip().lower()
    if len(name) > 40 or not PERSONA_RE.match(name):
        raise ValueError("--persona is a short lowercase name such as intraday-trader (the agents/ file name)")
    return name


def tag_orders(orders, persona):
    """Write the tag into each order's plan note. The broker-neutral order is untouched."""
    for o in orders:
        plan = dict(o.get("plan") or {})
        plan["persona"] = persona
        o["plan"] = plan
    return orders


def log_persona_orders(orders_path, persona, orders, source, created_at):
    """Append the tagged proposal to state/personas.json next to the orders file. -> its path."""
    base = os.path.dirname(os.path.abspath(orders_path))
    path = os.path.join(base, "state", "personas.json")
    doc = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError:
            doc = None
    rows = doc.get("drafts") if isinstance(doc, dict) and isinstance(doc.get("drafts"), list) else []
    rows.append({"at": created_at, "persona": persona, "source": source, "orders": orders})
    out = {"note": "Proposals tagged with the persona that drafted them, for the persona scorecard. "
                   "Read only by the scorecard; nothing here is ever sent to a broker.",
           "drafts": rows[-PERSONA_LOG_MAX:]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path

# ---------------------------------------------------------------- end of persona tag


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now),
        "status": "proposed",
        "paper": bool(merged_config(cfg).get("paper", True)),
        "note": "Proposals only. Nothing is placed until trader-risk passes each order and you say yes to it.",
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    lang = pick_lang(getattr(a, "lang", None), cfg)
    now = parse_now(a.now)
    today = now.date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1175.00")
    if a.market and limit is not None:
        raise SizeError("--market and --limit are opposites: pick one")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and sym:
        pdoc = read_json(ppath, "prices")
        p, at, fresh = fresh_price(pdoc, sym, now)
        entry = _f(plan.get("entry")) or 0
        if p is not None and fresh:
            price = p
            if round_paise(p) > round_paise(entry):
                if a.market:
                    notes.append(t("note_market", lang, sym=sym, price=_p(p), time=at.strftime("%H:%M"),
                                   entry=_p(entry)))
                else:
                    notes.append(t("note_chase", lang, sym=sym, price=_p(p), time=at.strftime("%H:%M"),
                                   limit=_p(limit or entry)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes)."
                         % (sym, ppath, ("from " + at.strftime("%Y-%m-%d %H:%M") + " IST") if at else "undated",
                            FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.capital, limit, price, cash, today, market=a.market)
    order = leg_order(s)
    if order and a.persona:
        tag_orders([order], a.persona)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
        if a.persona:
            log_persona_orders(a.write, a.persona, [order], src, utc_iso(now))
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes, lang))
        if order and a.write:
            print(t("wrote", lang, path=a.write))
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    prices = load_prices(ppath)
    result = size(rows, cfg, a.capital, prices, a.top)
    orders = orders_from(result, source)
    if a.persona:
        tag_orders(orders, a.persona)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
        if a.persona and orders:
            log_persona_orders(a.write, a.persona, orders, source, utc_iso(parse_now(a.now)))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def init_parser():
    ap = argparse.ArgumentParser(prog="size.py init", description="Build trader/config.json (trader-budget).")
    ap.add_argument("--config", default="trader/config.json", help="where to write (default trader/config.json)")
    ap.add_argument("--capital", type=float, help="your trading capital in rupees, e.g. 100000")
    ap.add_argument("--risk-pct", type=float, help="most to risk on one trade, percent of capital (default 1)")
    ap.add_argument("--max-trades", type=int, help="most trades in one day (default 3)")
    ap.add_argument("--product", type=str.upper, choices=["MIS", "CNC"], help="MIS intraday or CNC delivery")
    ap.add_argument("--max-position-pct", type=float, help="biggest position, percent of capital (default 50)")
    ap.add_argument("--broker-profile", type=str.lower, choices=BROKER_PROFILE_NAMES[:-1],
                    help="your broker's MIS square-off times (zerodha, upstox, dhan, angel, groww)")
    ap.add_argument("--yes", action="store_true", help="write without asking (needs --capital)")
    ap.add_argument("--force", action="store_true", help="replace an existing config")
    return ap


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init":
        a = init_parser().parse_args(argv[1:])
        try:
            return run_init(a)
        except SizeError as e:
            print("size.py: %s" % e, file=sys.stderr)
            return 1
    ap = argparse.ArgumentParser(description="Size a trade to your capital and max loss (trader-budget). "
                                             "First time: size.py init.")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--capital", type=float, help="override the capital in the config (rupees)")
    ap.add_argument("--limit", metavar="PRICE", help="a LIMIT entry at PRICE (default: LIMIT at the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET entry instead of a LIMIT")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:40\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    ap.add_argument("--lang", choices=LANGS, metavar="LANG", help="the report's language: en or hi-en (Hinglish); default: the "
                                                  "config's \"lang\", else en")
    ap.add_argument("--persona", help="tag the proposal with the agent persona drafting it, e.g. intraday-trader "
                                      "(a note for the persona scorecard: it changes no size and is never sent)")
    g = ap.add_argument_group("weights mode (split capital across a list, CNC, whole shares)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,TCS=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"symbol\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    a = ap.parse_args(argv)
    try:
        try:
            a.persona = persona_name(a.persona)
        except ValueError as e:
            raise SizeError(str(e))
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
