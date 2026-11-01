#!/usr/bin/env python3
"""Order tickets, your yes, the PAPER broker and live broker accounts for trader-broker. Stdlib only.

Paper is the default and the only broker that fills here. Live brokers (Upstox, Zerodha Kite,
Dhan; adapters/ next to this file) are read-only unless every gate is open. Around the one
moment that matters, your yes:

  show      each proposed order as a plain-language ticket plus the exact
            broker-neutral order JSON. An order is READY only if trader-risk
            passed THIS exact order today (matched by fingerprint) and nobody
            has decided on it yet.
  confirm   records your explicit yes for ONE order, your words verbatim. On
            paper it places the order on the paper account; with a live broker
            it only records the yes (place sends it). It refuses "yes to all",
            "sure", "ok", a yes that names another stock or amount, and any
            order that is not READY.
  decline   records a no.
  fill      simulates a paper fill: at --price, or at the fresh price in
            trader/prices.json. Applies the estimated charges, updates
            trader/portfolio.json (cash, positions) and the trades log
            trader/state/trades.json. An entry's SL-M stop and LIMIT target
            then work as a pair: when one fills, the other is cancelled.
  flatten   builds SELL MARKET tickets for every open MIS position, to be
            out by flat_by (15:10 IST, pulled in for an F&O stock's closing
            auction and your broker_profile's square-off), before the
            broker's own auto square-off. They still go through trader-risk
            and your yes.
  status    cash, positions, open legs, today's orders, trades and limits.

  F&O on paper: a multi-leg proposal (trader-strategy --orders, "basket": true)
  is ONE ticket; confirm --basket records one yes for every leg; fill --basket
  fills every leg at the latest chain (trader/options.json), buys first, or
  none; margin is blocked per strategy in trader/portfolio.json (schema 2,
  with a cash/margin ledger).
  mtm       F&O positions marked to the chain: P&L, Black-76 Greeks, margin.
  adjust    roll a leg, close a leg, add a hedge, close all: a NEW ticket.
  settle    expiring positions at NSE's final settlement price (cash-settled
            index options, STT on exercise, futures).
  shadow-report  shadow mode ("shadow": true): each live order next to its
            paper twin.

  accounts, funds, holdings, positions, orders, instruments
            read your real broker account (any IP; nothing is sent).
  login     a one-shot 127.0.0.1 browser login; prints export lines for your
            shell (the token is never written to disk).
  preflight public IP vs your static IP, token freshness, one harmless call.
  place     sends ONE order to the live broker, only when all gates are open:
            config broker is that broker, "paper": false, live_enabled for
            it, trader-risk PASS for this exact order today, your recorded
            yes for it, and today's preflight with the token in use.
  cancel    cancels one live order this tool sent today.

    python3 ticket.py show
    python3 ticket.py confirm --index 1 --yes-text "yes"
    python3 ticket.py fill --index 1 --price 1174.30          # the entry
    python3 ticket.py fill --index 1 --leg target              # at the target price
    python3 ticket.py flatten
    python3 ticket.py status
    python3 ticket.py holdings --broker upstox                 # read only
    python3 ticket.py confirm --basket --yes-text "yes"        # an F&O basket, one yes
    python3 ticket.py fill --basket
    python3 ticket.py mtm

Decisions go to trader/state/broker.json (append only). Times are IST;
--now "2026-10-29 09:41" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import random
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
    "live_enabled": {},
    "static_ip": "",
    "order_rate_per_sec": 3,
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


def tick_round(x, tick=0.05, how="nearest"):
    """Round a price to the tick: "nearest" (half up) or "down"."""
    p, t = to_paise(x), max(to_paise(tick), 1)
    n = p // t if how == "down" else (p + t // 2) // t
    return round(n * t / 100.0, 2)

# ---------------------------------------------------------------- the rest of trader-broker

NOT_SENT = ("why", "plan", "note")
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each", "sab"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "nahi"}
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel"}
PRODUCT_WORDS = {"MIS": "intraday", "CNC": "delivery", "NRML": "carry forward"}
FRESH_MINUTES = 15


# ---------------------------------------------------------------- dated market rules (same in entry, exit, alerts, risk, timemachine, scanner, broker, lab)
# Every row holds from its effective_from date (IST) until the next row starts.
# Times are IST. Segments: "EQ" a cash-market stock with no derivatives, "EQ_FNO"
# a cash-market stock in NSE's F&O list (the Closing Auction Session applies to
# it from 2026-08-03), "DERIV" futures and options. The sources are in
# docs/data-sources.md; when NSE changes a timing, add a row, never edit one.

SESSION_RULES = [
    {"effective_from": "2000-01-01",
     "pre_open": {"start": "09:00", "market_orders_until": "09:08", "random_close": ["09:07", "09:08"],
                  "matching": ["09:08", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:30", "DERIV": "15:30"},
     "cas": None,
     "post_close": ["15:40", "16:00"],
     "source": "NSE/CMTR/74969 (1 Jul 2026, 'current' pre-open times); NSE/FAOP/74467 (29 May 2026, "
               "derivatives close moved from 15:30)"},
    {"effective_from": "2026-08-03",
     "pre_open": {"start": "09:00", "market_orders_until": "09:08", "random_close": ["09:07", "09:08"],
                  "matching": ["09:08", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:15", "DERIV": "15:40"},
     "cas": {"start": "15:15", "orders_from": "15:20", "limit_only_from": "15:25", "random_close": ["15:28", "15:30"],
             "end": "15:35"},
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular 16 Jan 2026 para 4.2 and 6.1 (NSE/CMTR/72394); NSE/FAOP/74467; go-live NSE/CMTR/75479 "
               "and NSE/FAOP/75472 (30 Jul 2026)"},
    {"effective_from": "2026-09-07",
     "pre_open": {"start": "09:00", "market_orders_until": "09:05", "random_close": ["09:08", "09:10"],
                  "matching": ["09:10", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:15", "DERIV": "15:40"},
     "cas": {"start": "15:15", "orders_from": "15:20", "limit_only_from": "15:25", "random_close": ["15:28", "15:30"],
             "end": "15:35"},
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular 16 Jan 2026 para 5 and 6.2 (NSE/CMTR/72394); NSE/CMTR/74969 (1 Jul 2026); "
               "NSE/CMTR/76187 (4 Sep 2026)"},
    {"effective_from": "2026-12-07",
     "pre_open": {"start": "09:00", "market_orders_until": "09:05", "random_close": ["09:08", "09:10"],
                  "matching": ["09:10", "09:12"]},
     "open": "09:15",
     "continuous_end": {"EQ": "15:30", "EQ_FNO": "15:30", "DERIV": "15:45"},
     "cas": {"start": "15:30", "orders_from": "15:31", "limit_only_from": "15:36", "random_close": ["15:38", "15:40"],
             "end": "15:40"},
     "expiring_index_derivatives_end": "15:30",
     "post_close": ["15:50", "16:00"],
     "source": "SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/23843/2026 (15 Oct 2026) para 4.2 and 6.1; "
               "Zerodha bulletin 16 Oct 2026"},
]

# Brokers' MIS auto square-off, IST, by segment. A date before a broker's first
# row uses that first row. Brokers move these without much notice: check yours.
BROKER_SQUARE_OFF = {
    "zerodha": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:25", "DERIV": "15:26",
         "source": "Zerodha product update 10 Aug 2026 (CAS stocks were 15:10)"},
        {"effective_from": "2026-08-10", "EQ_FNO": "15:12", "EQ": "15:25", "DERIV": "15:26",
         "source": "support.zerodha.com intraday-auto-square-off-timings (read 1 Nov 2026)"}],
    "upstox": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:05", "EQ": "15:20", "DERIV": "15:20",
         "source": "upstox.com announcement 4 Sep 2026 (old times)"},
        {"effective_from": "2026-09-11", "EQ_FNO": "15:10", "EQ": "15:25", "DERIV": "15:25",
         "source": "upstox.com announcement 4 Sep 2026, effective 11 Sep 2026"}],
    "dhan": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:10", "DERIV": "15:25",
         "source": "dhan.co support: what-are-the-dhan-intraday-auto-square-off-timings (read 1 Nov 2026)"}],
    "angel": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:10", "EQ": "15:10", "DERIV": "15:20",
         "source": "smartapi.angelone.in forum post 19209 (CAS changes from 3 Aug 2026)"}],
    "groww": [
        {"effective_from": "2026-08-03", "EQ_FNO": "15:05", "EQ": "15:20", "DERIV": "15:20",
         "source": "groww.in/p/risk-policy (read 1 Nov 2026)"}],
}
BROKER_PROFILES = tuple(sorted(BROKER_SQUARE_OFF)) + ("custom",)
SEGMENTS = ("EQ", "EQ_FNO", "DERIV")
RULES_DEFAULT_FLAT_BY = "15:10"   # the config's default flat_by
SQUARE_OFF_MARGIN_MIN = 5    # flat by at least this long before the broker's square-off
ENTRY_LEAD_MIN = 15          # and stop new entries this long before the flat-by
FNO_LIST_URL = "https://www.nseindia.com/api/master-quote"
FNO_LIST_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
                                  "Gecko) Chrome/130 Safari/537.36",
                    "Accept": "application/json", "Referer": "https://www.nseindia.com/"}


class RulesError(ValueError):
    pass


def _rule_time(text, what="time"):
    try:
        h, m = str(text).strip().split(":")
        return datetime.time(int(h), int(m))
    except (ValueError, TypeError):
        raise RulesError("%s wants an IST time like 15:10, not %r" % (what, text))


def _rule_day(d):
    return d if isinstance(d, str) else d.isoformat()


def _rule_minus(t, minutes):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 3), t) - datetime.timedelta(minutes=minutes)
    return dt.time()


def _dated_row(rows, d):
    day = _rule_day(d)
    pick = rows[0]
    for row in rows:
        if row["effective_from"] <= day:
            pick = row
    return pick


def session_for(d, segment="EQ"):
    """The NSE session rules that hold on date d for one segment, as datetime.time values:
    pre_open (start, order entry end), open, continuous_end (the last continuous-trading
    minute), close (when the segment's day is over: the auction's end for EQ_FNO under CAS),
    cas (dict or None), post_close (start, end), rule_from and source."""
    seg = str(segment or "EQ").upper()
    if seg not in SEGMENTS:
        raise RulesError("segment is one of %s, not %r" % (", ".join(SEGMENTS), segment))
    row = _dated_row(SESSION_RULES, d)
    cts_end = _rule_time(row["continuous_end"][seg])
    cas = None
    if seg == "EQ_FNO" and row.get("cas"):
        cas = {k: ([_rule_time(x) for x in v] if isinstance(v, list) else _rule_time(v))
               for k, v in row["cas"].items()}
    po = row["pre_open"]
    return {"date": _rule_day(d), "segment": seg, "rule_from": row["effective_from"], "source": row["source"],
            "pre_open": (_rule_time(po["start"]), _rule_time(po["random_close"][1])),
            "pre_open_market_orders_until": _rule_time(po["market_orders_until"]),
            "open": _rule_time(row["open"]), "continuous_end": cts_end,
            "close": cas["end"] if cas else cts_end, "cas": cas,
            "post_close": tuple(_rule_time(x) for x in row["post_close"])}


def segment_of(symbol, fno_stocks):
    """EQ_FNO when the stock is in the F&O list. With no list at all, EQ_FNO too:
    its earlier close is the safe side to be wrong on."""
    if fno_stocks is None:
        return "EQ_FNO"
    return "EQ_FNO" if str(symbol or "").strip().upper() in fno_stocks else "EQ"


def square_off_for(d, segment, cfg):
    """(time, "zerodha: source") of the broker's MIS auto square-off, or (None, "") with no profile."""
    cfg = cfg or {}
    name = str(cfg.get("broker_profile") or "").strip().lower()
    if not name:
        return None, ""
    seg = str(segment or "EQ").upper()
    if name == "custom":
        custom = cfg.get("square_off") or {}
        if not isinstance(custom, dict) or not custom.get(seg):
            raise RulesError('broker_profile "custom" needs "square_off": {"EQ": "15:20", "EQ_FNO": "15:10", '
                             '"DERIV": "15:25"} (your broker\'s MIS square-off times, IST)')
        return _rule_time(custom[seg], "square_off %s" % seg), "custom: square_off in your config"
    if name not in BROKER_SQUARE_OFF:
        raise RulesError("broker_profile is one of %s, not %r" % (", ".join(BROKER_PROFILES), name))
    row = _dated_row(BROKER_SQUARE_OFF[name], d)
    return _rule_time(row[seg]), "%s: %s" % (name, row["source"])


def safe_flat_by(d, segment, cfg):
    """(flat_by time, note). The latest safe time is the earlier of continuous trading's
    end and the broker's square-off, each less the margin (5 minutes). flat_by "auto"
    takes it; flat_by (15:10 when missing) later than it is pulled in, with a note."""
    cfg = cfg or {}
    margin = int(cfg.get("square_off_margin_min", SQUARE_OFF_MARGIN_MIN) or 0)
    s = session_for(d, segment)
    safe, why = _rule_minus(s["continuous_end"], margin), "continuous trading ends %s" % s["continuous_end"].strftime("%H:%M")
    sq, src = square_off_for(d, segment, cfg)
    if sq is not None and _rule_minus(sq, margin) < safe:
        safe, why = _rule_minus(sq, margin), "%s squares off MIS at %s" % (src.split(":")[0], sq.strftime("%H:%M"))
    want = cfg.get("flat_by") or RULES_DEFAULT_FLAT_BY
    if want == "auto":
        return safe, "%s (%d min before: %s)" % (safe.strftime("%H:%M"), margin, why)
    t = _rule_time(want, "flat_by")
    if t > safe:
        return safe, "flat_by %s is too late on %s for this stock: %s, so flat by %s" % (
            t.strftime("%H:%M"), _rule_day(d), why, safe.strftime("%H:%M"))
    return t, ""


def entry_cutoff(d, segment, cfg):
    """The last time for a new entry: no_entries_after, but at least ENTRY_LEAD_MIN before the safe flat-by."""
    cfg = cfg or {}
    flat, _ = safe_flat_by(d, segment, cfg)
    late = _rule_minus(flat, ENTRY_LEAD_MIN)
    want = cfg.get("no_entries_after")
    t = _rule_time(want, "no_entries_after") if want else late
    return min(t, late)


def parse_fno_list(text):
    """NSE master-quote JSON (a list of F&O underlying symbols) -> set of symbols."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise RulesError("the NSE F&O list is not JSON")
    if isinstance(doc, dict):
        doc = doc.get("symbols") or doc.get("data") or []
    syms = {str(x).strip().upper() for x in doc if isinstance(x, str) and x.strip()}
    if len(syms) < 50:
        raise RulesError("the NSE F&O list has only %d symbols: not trusting it" % len(syms))
    return syms


def fetch_fno_stocks(timeout=20):
    """NSE's F&O underlying list, live. One polite request; raises RulesError on failure."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(FNO_LIST_URL, headers=FNO_LIST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_fno_list(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as e:
        raise RulesError("could not fetch the NSE F&O list (%s)" % (getattr(e, "reason", None) or e))


def load_fno_stocks(cfg=None, cache_path=None, fetch=False, today=None):
    """(set of F&O stock symbols or None, note). The config's "fno_stocks" list wins.
    Else the cache (trader/fno_stocks.json), refreshed from NSE at most once a day when
    fetch is True. None means unknown: callers then treat every stock as EQ_FNO."""
    cfg = cfg or {}
    own = cfg.get("fno_stocks")
    if isinstance(own, list) and own:
        return {str(x).strip().upper() for x in own}, "F&O list from fno_stocks in your config"
    today = _rule_day(today or datetime.date.today())
    cached, fetched = None, None
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            cached, fetched = {str(x).upper() for x in doc.get("symbols") or []} or None, doc.get("fetched")
        except (OSError, ValueError, AttributeError):
            cached = None
    if fetch and (cached is None or fetched != today):
        try:
            syms = fetch_fno_stocks()
        except RulesError as e:
            if cached is not None:
                return cached, "F&O list cached %s (%s)" % (fetched, e)
            return None, "%s: treating every stock as an F&O stock (earlier close)" % e
        if cache_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump({"source": FNO_LIST_URL, "fetched": today, "symbols": sorted(syms)}, fh, indent=0)
                    fh.write("\n")
            except OSError:
                pass
        return syms, "F&O list from NSE, %s (%d stocks)" % (today, len(syms))
    if cached is not None:
        return cached, "F&O list cached %s (%d stocks)" % (fetched, len(cached))
    return None, "no F&O list (trader/fno_stocks.json): treating every stock as an F&O stock (earlier close)"

# ---------------------------------------------------------------- end of dated market rules


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """ISO time -> aware IST datetime. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TicketError("--now wants a time like \"2026-10-29 09:41\" (IST) or ...Z (UTC)")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def parse_time(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def ist_iso(now):
    return now.astimezone(IST).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(v):
    return "%.2f" % round_paise(v)


def _q(v):
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: v for k, v in order.items() if k not in NOT_SENT and v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def neutral(order):
    """The broker-neutral order: every field except our notes ("why", "plan").
    A broker adapter maps exactly this to its own API."""
    return {k: v for k, v in order.items() if k not in NOT_SENT}


def entry_ref(order):
    plan = order.get("plan") or {}
    if str(order.get("order_type", "")).upper() in ("LIMIT", "SL") and _f(order.get("price")):
        return _f(order["price"])
    if str(order.get("order_type", "")).upper() == "SL-M" and _f(order.get("trigger_price")):
        return _f(order["trigger_price"])
    return _f(plan.get("entry"))


def stop_of(order):
    return _f((order.get("stop_loss") or {}).get("trigger_price")) or _f((order.get("plan") or {}).get("stop"))


def target_of(order):
    return _f((order.get("target") or {}).get("price")) or _f((order.get("plan") or {}).get("target"))


def loss_at_stop(order, cfg):
    """(gross, charges, total) lost if the stop fills exactly at the stop, or (None, None, None)."""
    stop, q, entry = stop_of(order), _f(order.get("qty")), entry_ref(order)
    if not stop or not q or not entry:
        return None, None, None
    gross = round_paise(q * abs(to_paise(entry) - to_paise(stop)) / 100.0)
    c = merged_config(cfg)
    ch = 0.0
    if c.get("include_charges", True):
        buy, sell = (entry, stop) if str(order.get("side", "")).upper() == "BUY" else (stop, entry)
        ch = round_trip_charges(buy, sell, q, charges_config(cfg, order.get("product") or c["product"]))["total"]
    return gross, ch, round_paise(gross + ch)


def plain_parts(order, cfg=None):
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    prod = str(order.get("product") or "MIS").upper()
    otype = str(order.get("order_type") or "MARKET").upper()
    validity = VALIDITY_WORDS.get(str(order.get("validity") or "DAY").upper(), order.get("validity"))
    if otype == "LIMIT":
        how = "LIMIT %s" % _p(order.get("price"))
    elif otype == "SL":
        how = "SL trigger %s, limit %s" % (_p(order.get("trigger_price")), _p(order.get("price")))
    elif otype == "SL-M":
        how = "SL-M trigger %s" % _p(order.get("trigger_price"))
    else:
        how = "MARKET"
    parts = ["%s %s %s on %s, %s (%s), %s, %s" % (side, _q(order.get("qty")), sym, order.get("exchange") or "NSE",
                                                  prod, PRODUCT_WORDS.get(prod, prod), how, validity)]
    sl, tg = order.get("stop_loss"), order.get("target")
    if isinstance(sl, dict):
        parts.append("then a stop-loss: %s trigger %s" % (sl.get("order_type", "SL-M"), _p(sl.get("trigger_price"))))
    if isinstance(tg, dict):
        parts.append("then a target: LIMIT %s (when the stop or the target fills, the other is cancelled)"
                     % _p(tg.get("price")))
    ref = entry_ref(order)
    if ref and _f(order.get("qty")):
        parts.append("order value about %s" % inr(ref * float(order["qty"])))
    gross, ch, total = loss_at_stop(order, cfg)
    if total is not None and isinstance(sl, dict):
        parts.append("loss if the stop fills at %s: about %s%s (a stop can fill worse in a fast market)"
                     % (_p(stop_of(order)), inr(gross), (" + %s charges = %s" % (inr(ch), inr(total))) if ch else ""))
    return parts


def plain(order, cfg=None):
    return " / ".join(plain_parts(order, cfg))


def amount_numbers(order, cfg=None):
    nums = []
    for v in (order.get("qty"), order.get("price"), order.get("trigger_price"),
              (order.get("stop_loss") or {}).get("trigger_price"), (order.get("target") or {}).get("price")):
        if _f(v) is not None:
            nums.append(_f(v))
    ref = entry_ref(order)
    if ref and _f(order.get("qty")):
        nums.append(round_paise(ref * float(order["qty"])))
    nums.extend(x for x in loss_at_stop(order, cfg) if x is not None)
    return nums


def parse_yes(reply, order, batch_symbols=(), cfg=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{0,19}", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order, cfg)
        if not all(any(abs(n - w) < 0.005 for w in want) for n in nums):
            return False, "the reply says %s, but this order is %s" % (", ".join(_q(n) for n in nums),
                                                                     plain(order, cfg))
    return True, "explicit yes for %s" % plain(order, cfg)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def records(state, fp=None, today=None, status=None):
    out = []
    for r in state.get("orders", []):
        if fp is not None and r.get("fingerprint") != fp:
            continue
        if today is not None and rec_day(r) != today:
            continue
        if status is not None and r.get("status") not in ((status,) if isinstance(status, str) else status):
            continue
        out.append(r)
    return out


def decision(state, fp, today):
    """The decision recorded today for this fingerprint (placed, approved for a live broker, or declined)."""
    found = records(state, fp, today, ("placed", "declined", "approved"))
    return found[-1] if found else None


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = decision(state, fp, today)
    if rec and rec.get("status") == "approved":
        return False, "YES RECORDED for %s at %s: send it with ticket.py place --index %s" % (
            rec.get("broker"), rec.get("at"), rec.get("index")), rv
    if rec:
        return False, "DONE: %s at %s" % (rec.get("status"), rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).upper() == "BUY" else "exit"


LIVE_BROKERS = ("upstox", "kite", "dhan", "angel")
BROKER_ALIASES = {"zerodha": "kite", "angelone": "angel"}


def broker_name(name):
    n = str(name or "paper").strip().lower()
    return BROKER_ALIASES.get(n, n)


def mode_of(cfg):
    """("paper", None) or ("live", broker). A config that is neither is an error, in plain words."""
    c = merged_config(cfg)
    b = broker_name(c.get("broker"))
    paper = c.get("paper", True)
    if b == "paper":
        if paper is not True:
            raise TicketError("\"paper\" is false but \"broker\" is \"paper\": name the live broker (%s) or set "
                              "\"paper\": true; nothing was sent" % ", ".join(LIVE_BROKERS))
        return "paper", None
    if b not in LIVE_BROKERS:
        raise TicketError("\"broker\" is %r: the brokers are paper, %s; nothing was sent"
                          % (c.get("broker"), ", ".join(LIVE_BROKERS)))
    if paper is not False:
        raise TicketError("\"broker\" is \"%s\" but \"paper\" is true: the paper broker only works with "
                          "\"broker\": \"paper\". Set \"broker\" back to \"paper\", or go live yourself (paper "
                          "false and live_enabled, see docs/brokers.md); nothing was sent" % b)
    return "live", b


def config_mode_errors(cfg):
    """The errors mode_of would raise, as a list (for the settings page)."""
    try:
        mode_of(cfg)
    except TicketError as e:
        return [str(e)]
    return []


def paper_only(cfg, what="a paper fill"):
    """Paper-only commands refuse a live config (a live fill comes from the broker; a live order is sent
    only by ticket.py place, in your own terminal)."""
    mode, b = mode_of(cfg)
    if mode != "paper":
        raise TicketError("the config is live with %s (\"paper\": false): %s works on the paper account only. "
                          "Live orders go out only with ticket.py confirm and then ticket.py place, in your own "
                          "terminal; live fills show in ticket.py orders. Nothing was sent" % (b, what))


# ---------------------------------------------------------------- rendering

def account_words(cfg):
    """"PAPER account (no real money)" or "LIVE at Upstox (real money)"; a bad config says so."""
    try:
        mode, b = mode_of(cfg)
    except TicketError:
        return "PAPER account (no real money)", None
    if mode == "paper":
        return "PAPER account (no real money)", None
    return "LIVE at %s (real money)" % BROKER_TITLES.get(b, b), b


BROKER_TITLES = {"upstox": "Upstox", "kite": "Zerodha Kite", "dhan": "Dhan", "angel": "Angel One"}


# ---------------------------------------------------------------- messages: English and Hinglish (--lang hi-en)
# The yes stays the English word "yes" in every language: parse_yes() reads only that, on purpose.

MSG = {
    "en": {
        "order_head": "ORDER {i} of {n}, {account}",
        "acct_paper": "PAPER account (no real money)",
        "acct_live": "LIVE at {broker} (real money)",
        "why": "  why: {text}",
        "note": "  note: {text}",
        "neutral": "  The broker-neutral order:",
        "reply_live": "  Reply \"yes\" to approve this one order for your {broker} account (real money), or \"no\" to "
                      "skip it. Your yes is recorded; the order goes out only with ticket.py place.",
        "reply_paper": "  Reply \"yes\" to place this one order on the paper account, or \"no\" to skip it.",
        "footer": "\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, not "
                  "financial advice.",
        "st_NOT READY": "NOT READY: ",
        "st_BLOCKED": "BLOCKED by trader-risk: ",
        "st_READY": "READY: trader-risk PASS, waiting for your yes",
        "st_DONE": "DONE: ",
        "pp_stop": "then a stop-loss: ",
        "pp_target": "then a target: ",
        "pp_oco": " (when the stop or the target fills, the other is cancelled)",
        "pp_value": "order value about ",
        "pp_loss": "loss if the stop fills at ",
        "pp_about": ": about ",
        "pp_worse": " (a stop can fill worse in a fast market)",
        "not_recorded": "Not recorded: {why}. Ask the user again with the ticket for order {i}.",
        "recorded": "Recorded your yes for order {i}, word for word: \"{text}\".",
        "live_wait": "Nothing is sent yet. This is a LIVE {broker} order (real money): {plain}.",
        "live_place": "To send it, run in your own terminal: python3 ticket.py place --index {i} (it checks every gate "
                      "first: live_enabled, today's trader-risk PASS, this yes, and today's preflight).",
        "paper_placed": "Placed on the PAPER account as {id}: {plain}.",
        "when_fills": "When it fills: ticket.py fill --index {i} --price P (or the fresh price in trader/prices.json).",
        "declined": "Recorded: you said no to order {i} ({plain}). Nothing was sent.",
    },
    "hi-en": {
        "order_head": "ORDER {i} / {n}, {account}",
        "acct_paper": "PAPER account (asli paisa nahi)",
        "acct_live": "LIVE, {broker} pe (asli paisa)",
        "why": "  kyun: {text}",
        "note": "  dhyan dein: {text}",
        "neutral": "  Broker-neutral order:",
        "reply_live": "  Sirf is EK order ko apne {broker} account (asli paisa) ke liye approve karne ke liye \"yes\" "
                      "likhiye, ya chhodne ke liye \"no\". Aapka yes record hoga; order sirf ticket.py place se hi "
                      "jaata hai.",
        "reply_paper": "  Sirf is EK order ko paper account pe place karne ke liye \"yes\" likhiye, ya chhodne ke liye "
                       "\"no\".",
        "footer": "\nEk baar mein ek order: ek yes sirf ek ticket ke liye hai, poore batch ke liye kabhi nahi. Yeh "
                  "education aur tooling hai, financial advice nahi.",
        "st_NOT READY": "TAIYAAR NAHI: ",
        "st_BLOCKED": "trader-risk ne BLOCK kiya: ",
        "st_READY": "READY: trader-risk PASS, aapke yes ka intezaar",
        "st_DONE": "HO GAYA: ",
        "pp_stop": "phir stop-loss: ",
        "pp_target": "phir target: ",
        "pp_oco": " (stop ya target mein se ek fill hote hi doosra cancel)",
        "pp_value": "order value lagbhag ",
        "pp_loss": "agar stop ",
        "pp_about": " pe fill ho to nuksaan lagbhag ",
        "pp_worse": " (tez market mein stop isse bure price pe bhi fill ho sakta hai)",
        "not_recorded": "Record nahi hua: {why}. User ko order {i} ka ticket dikhakar dobara poochiye.",
        "recorded": "Order {i} ke liye aapka yes, word for word, record hua: \"{text}\".",
        "live_wait": "Abhi kuch bhi bheja nahi gaya. Yeh LIVE {broker} order hai (asli paisa): {plain}.",
        "live_place": "Bhejne ke liye apne terminal mein khud chalaiye: python3 ticket.py place --index {i} (yeh pehle "
                      "har gate check karta hai: live_enabled, aaj ka trader-risk PASS, yeh yes, aur aaj ka preflight).",
        "paper_placed": "PAPER account pe {id} ke roop mein place hua: {plain}.",
        "when_fills": "Fill hone par: ticket.py fill --index {i} --price P (ya trader/prices.json ka fresh price).",
        "declined": "Record hua: aapne order {i} ko no kaha ({plain}). Kuch bhi bheja nahi gaya.",
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


def plain_words(order, cfg=None, lang="en"):
    """plain_parts() in lang: the fixed phrases are swapped; the order's own words and numbers stay as they are."""
    parts = plain_parts(order, cfg)
    if lang == "en":
        return parts
    out = []
    for part in parts:
        for k in ("pp_stop", "pp_target", "pp_oco", "pp_value", "pp_worse"):
            part = part.replace(t(k), t(k, lang))
        if part.startswith(t("pp_loss")):
            part = t("pp_loss", lang) + part[len(t("pp_loss")):].replace(t("pp_about"), t("pp_about", lang), 1)
        out.append(part)
    return out


def status_words(label, lang="en"):
    """status_of()'s label in lang (its English prefix swapped; what follows stays as it is)."""
    if lang == "en":
        return label
    for k in ("st_READY", "st_NOT READY", "st_BLOCKED", "st_DONE"):
        if label.startswith(t(k)):
            return t(k, lang) + label[len(t(k)):]
    return label


def render(order, idx, total, cfg, risk, state, today, lang="en"):
    ready, label, rv = status_of(order, risk, state, today)
    acct, live = account_words(cfg)
    if lang != "en":
        acct = t("acct_live", lang, broker=BROKER_TITLES.get(live, live)) if live else t("acct_paper", lang)
    lines = [t("order_head", lang, i=idx, n=total, account=acct)]
    for part in plain_words(order, cfg, lang):
        lines.append("  " + part)
    if order.get("why"):
        lines.append(t("why", lang, text=order["why"]))
    lines.append("  " + status_words(label, lang))
    for w in (rv or {}).get("warnings", []):
        lines.append(t("note", lang, text=w))
    lines.append(t("neutral", lang))
    for l in json.dumps(neutral(order), indent=2, ensure_ascii=False).splitlines():
        lines.append("    " + l)
    if ready and live:
        lines.append(t("reply_live", lang, broker=BROKER_TITLES[live]))
    elif ready:
        lines.append(t("reply_paper", lang))
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    gross, ch, total = loss_at_stop(order, cfg)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
            "fingerprint": fingerprint(order), "paper": not account_words(cfg)[1],
            "broker": account_words(cfg)[1] or "paper", "order": neutral(order),
            "why": order.get("why", ""), "loss_at_stop": gross, "charges": ch, "max_loss": total,
            "stop": stop_of(order), "target": target_of(order)}


# ---------------------------------------------------------------- paper fills

def side_charges(side, price, qty, cfg, product, segment="EQ", on=None):
    """Charges on one executed order: the buy-side or sell-side half of round_trip_charges, with your
    broker_profile's brokerage (CHARGE_PROFILES). segment "OPT" or "FUT" for an F&O leg (dated rates on `on`)."""
    c = charges_config(cfg, product)
    seg = str(segment or "EQ").upper()
    if side == "BUY":
        return round_trip_charges(price, 0, qty, c, segment=seg, on=on)
    return round_trip_charges(0, price, qty, c, segment=seg, on=on)


# ---------------------------------------------------------------- broker charge profiles (paper charges)
# Brokerage only: STT, exchange, SEBI, stamp and GST are the same at every broker (the tables above).
# broker_profile in the config picks one (it also sets the MIS square-off times); "custom" or none
# keeps your own "charges" / "fno_charges" exactly as written. A flat Rs 20 an order is written as
# brokerage_pct 100 with brokerage_cap 20 (the lower of the value and Rs 20). Minimum-brokerage
# rules (Angel One, Groww: Rs 5) and joining offers are not modelled. Read 1 Nov 2026; check yours.
FLAT_20 = {"brokerage_pct": 100, "brokerage_cap": 20}
CHARGE_PROFILES = {
    "zerodha": {"EQ_MIS": {"brokerage_pct": 0.03, "brokerage_cap": 20}, "EQ_CNC": {"brokerage_pct": 0, "brokerage_cap": 0},
                "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20}, "OPT": {"brokerage_flat": 20},
                "source": "zerodha.com/charges (read 1 Nov 2026): delivery zero; intraday and futures 0.03% or Rs 20 "
                          "an executed order, whichever is lower; options flat Rs 20 an executed order"},
    "upstox": {"EQ_MIS": {"brokerage_pct": 0.1, "brokerage_cap": 20}, "EQ_CNC": dict(FLAT_20),
               "FUT": {"brokerage_pct": 0.05, "brokerage_cap": 20}, "OPT": {"brokerage_flat": 20},
               "source": "upstox.com/brokerage-charges (read 1 Nov 2026): delivery Rs 20 an executed order; intraday "
                         "Rs 20 or 0.1%, futures Rs 20 or 0.05%, whichever is lower; options flat Rs 20"},
    "dhan": {"EQ_MIS": {"brokerage_pct": 0.03, "brokerage_cap": 20}, "EQ_CNC": {"brokerage_pct": 0, "brokerage_cap": 0},
             "FUT": dict(FLAT_20), "OPT": {"brokerage_flat": 20},
             "source": "dhan.co/pricing (read 1 Nov 2026): delivery zero; intraday Rs 20 or 0.03%, whichever is "
                       "lower; futures and options Rs 20 an executed order"},
    "angel": {"EQ_MIS": {"brokerage_pct": 0.1, "brokerage_cap": 20}, "EQ_CNC": {"brokerage_pct": 0.1, "brokerage_cap": 20},
              "FUT": dict(FLAT_20), "OPT": {"brokerage_flat": 20},
              "source": "angelone.in/exchange-transaction-charges (read 1 Nov 2026): delivery and intraday the lower "
                        "of Rs 20 or 0.1% (minimum Rs 5, not modelled); futures and options Rs 20 an executed order; "
                        "the first-30-days offer is ignored"},
    "groww": {"EQ_MIS": {"brokerage_pct": 0.1, "brokerage_cap": 20}, "EQ_CNC": {"brokerage_pct": 0.1, "brokerage_cap": 20},
              "FUT": dict(FLAT_20), "OPT": {"brokerage_flat": 20},
              "source": "groww.in/pricing and groww.in/pricing/futures-and-options (read 1 Nov 2026): equity Rs 20 or "
                        "0.1% an executed order, whichever is lower (minimum Rs 5, not modelled); F&O Rs 20 an "
                        "executed order"},
}
BROKERAGE_KEYS = ("brokerage_pct", "brokerage_cap", "brokerage_flat")


def charge_profile(cfg):
    """(name, profile or None). "custom", "" or an unknown name: None (your own charges as written)."""
    name = str((cfg or {}).get("broker_profile") or "").strip().lower()
    return name, CHARGE_PROFILES.get(name)


def charges_config(cfg, product=None):
    """The merged config whose "charges" (cash market) and "fno_charges" (F&O) carry your broker_profile's
    brokerage on top of your own statutory rates. product picks the delivery or intraday brokerage."""
    raw = cfg if isinstance(cfg, dict) else {}
    c = merged_config(raw)
    if product:
        c["product"] = str(product).upper()
    name, prof = charge_profile(raw)
    if not prof:
        return c
    eq = dict(c["charges"])
    eq.update(prof["EQ_CNC" if str(c.get("product") or "MIS").upper() == "CNC" else "EQ_MIS"])
    c["charges"] = eq
    own = raw.get("fno_charges") if isinstance(raw.get("fno_charges"), dict) else {}
    fno = {}
    for seg in ("OPT", "FUT"):
        d = {k: v for k, v in (own.get(seg) or {}).items() if k not in BROKERAGE_KEYS}
        d.update(prof[seg])
        fno[seg] = d
    c["fno_charges"] = fno
    c["charges_profile"] = name
    return c


PORTFOLIO_SCHEMA = "bharat-trader/portfolio/2"


def migrate_portfolio(pf):
    """An older portfolio.json (no schema: cash and share positions only) to schema 2, in place. Cash and
    the share positions are kept as they are; F&O positions, strategies, the margin blocked and the
    cash/margin ledger start empty. Returns what it came from, or None when it is schema 2 already."""
    if pf.get("schema") == PORTFOLIO_SCHEMA:
        for k, v in (("margin_blocked", 0.0), ("fno_positions", []), ("strategies", {}), ("ledger", [])):
            pf.setdefault(k, v)
        return None
    old = pf.get("schema") or "1"
    pf["schema"] = PORTFOLIO_SCHEMA
    pf["margin_blocked"] = round_paise(_f(pf.get("margin_blocked")) or 0.0)
    pf.setdefault("fno_positions", [])
    pf.setdefault("strategies", {})
    pf.setdefault("ledger", [])
    pf["migrated_from"] = old
    return old


def load_portfolio(path, cfg):
    """trader/portfolio.json, schema 2 (an older file is migrated in memory; save_portfolio keeps a copy
    of the old file next to it as portfolio.json.v1.bak the first time it writes)."""
    pf = read_json(path, "portfolio", required=False)
    if pf is None:
        cap = _f(merged_config(cfg).get("capital"))
        if not cap:
            raise TicketError("no %s yet and no capital in the config to start it from" % path)
        pf = {"schema": PORTFOLIO_SCHEMA, "cash": cap, "positions": {}, "margin_blocked": 0.0, "fno_positions": [],
              "strategies": {}, "ledger": []}
    if isinstance(pf.get("positions"), list):          # an older list shape
        pf["positions"] = {str(p.get("symbol") or p.get("ticker")).upper():
                           {"qty": p.get("qty", p.get("shares", 0)), "avg_price": p.get("avg_price", 0)}
                           for p in pf["positions"]}
    pf.setdefault("positions", {})
    migrate_portfolio(pf)
    return pf


def save_portfolio(path, pf):
    """Write the portfolio. The first write over an older (schema 1) file keeps it as <path>.v1.bak."""
    if os.path.exists(path) and not os.path.exists(path + ".v1.bak"):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = None
        if isinstance(old, dict) and old.get("schema") != PORTFOLIO_SCHEMA:
            import shutil
            shutil.copyfile(path, path + ".v1.bak")
    write_json(path, pf)


def apply_fill(pf, symbol, side, qty, price, charges, product):
    """Cash and positions after one fill. Returns realised P&L for a sell, after the charges of both
    sides (a position remembers the buy-side charges it paid in "buy_charges")."""
    symbol, qty, price = symbol.upper(), int(qty), float(price)
    pos = pf["positions"].get(symbol)
    value = round_paise(qty * price)
    realised = None
    if side == "BUY":
        old_q = int((pos or {}).get("qty", 0))
        old_avg = float((pos or {}).get("avg_price", 0))
        new_q = old_q + qty
        avg = round_paise((old_q * old_avg + qty * price) / new_q)
        paid = round_paise(float((pos or {}).get("buy_charges", 0)) + charges)
        pf["positions"][symbol] = {"qty": new_q, "avg_price": avg, "product": product, "buy_charges": paid}
        pf["cash"] = round_paise(float(pf["cash"]) - value - charges)
    else:
        held = int((pos or {}).get("qty", 0))
        if held < qty:
            raise TicketError("cannot fill a SELL of %d %s: the paper portfolio holds %d" % (qty, symbol, held))
        avg = float(pos.get("avg_price", 0))
        paid = float(pos.get("buy_charges", 0))
        share = round_paise(paid * qty / held)
        realised = round_paise((price - avg) * qty - charges - share)
        if held == qty:
            del pf["positions"][symbol]
        else:
            pos["qty"] = held - qty
            pos["buy_charges"] = round_paise(paid - share)
        pf["cash"] = round_paise(float(pf["cash"]) + value - charges)
    return realised


def fresh_price(path, sym, now):
    doc = read_json(path, "prices", required=False)
    if not isinstance(doc, dict):
        return None, "no trader/prices.json"
    if isinstance(doc.get("prices"), dict):
        p = _f(doc["prices"].get(sym))
        det = (doc.get("detail") or {}).get(sym) or {}
        t = parse_time(det.get("time")) or parse_time(doc.get("as_of"))
    else:
        row = doc.get(sym) if isinstance(doc.get(sym), dict) else {}
        p, t = _f(row.get("price")), parse_time(row.get("time"))
    if not p:
        return None, "no %s price in %s" % (sym, path)
    if t is None or not -60 <= (now - t).total_seconds() <= FRESH_MINUTES * 60:
        return None, "the %s price in %s is not fresh (older than %d minutes)" % (sym, path, FRESH_MINUTES)
    return p, None


def open_legs(state, today, symbol=None):
    """Entries filled today whose stop and target are still working: [placed record]."""
    out = []
    for r in records(state, today=today, status="placed"):
        if r.get("kind") != "entry" or not r.get("has_legs"):
            continue
        if symbol and r.get("symbol") != symbol:
            continue
        fp = r.get("fingerprint")
        fills = {x.get("leg") for x in records(state, fp, today, "filled")}
        closed = records(state, fp, today, "cancelled")
        if "order" in fills and not ({"stop", "target"} & fills) and not closed:
            out.append(r)
    return out


def log_trade(trades_path, trade):
    doc = read_json(trades_path, "trades log", required=False) or {"trades": []}
    doc.setdefault("trades", []).append(trade)
    write_json(trades_path, doc)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-29")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _is_basket_file(a):
    doc = read_json(a.orders, "orders file", required=False)
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    return isinstance(orders, list) and bool(orders) and is_basket_doc(doc, orders)


BASKET_WORDS = ("%s holds a multi-leg F&O basket: it is ONE ticket and one yes covers every leg. Use ticket.py show, "
                "then confirm --basket --yes-text \"yes\" and fill --basket")


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    if _is_basket_file(a):
        doc, orders = _basket_load(a)
        return cmd_show_basket(a, doc, orders, cfg, risk, state, today, now)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    lang = pick_lang(getattr(a, "lang", None), cfg)
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, lang)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print(t("footer", lang))
    return 0 if any_ready else 1


def cmd_confirm(a):
    if a.basket:
        return cmd_confirm_basket(a)
    if a.index is None:
        raise TicketError("say which order: --index N (or --basket for a basket ticket)")
    orders, cfg, risk, state, today, now = _load(a)
    if _is_basket_file(a):
        raise TicketError((BASKET_WORDS % a.orders) + "; nothing was recorded")
    mode, live = mode_of(cfg)
    actor = actor_of(a, mode)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.yes_text, order, [o.get("symbol", "") for o in orders], cfg)
    lang = pick_lang(getattr(a, "lang", None), cfg)
    words = " / ".join(plain_words(order, cfg, lang))
    if not ok:
        print(t("not_recorded", lang, why=why, i=a.index))
        return 1
    gross, ch, total = loss_at_stop(order, cfg)
    n = len(records(state, today=today, status="placed")) + 1
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": str(order.get("side", "")).upper(),
           "qty": order.get("qty"), "product": str(order.get("product") or "MIS").upper(),
           "kind": kind_of(order, rv), "max_loss": total, "stop": stop_of(order), "target": target_of(order),
           "has_legs": bool(order.get("stop_loss") or order.get("target")),
           "plain": plain(order, cfg), "status": "placed", "yes_text": a.yes_text, "broker": "paper",
           "paper": True, "paper_order_id": "PAPER-%s-%d" % (today.replace("-", ""), n), "order": neutral(order)}
    if actor != "user":
        rec["actor"] = actor
    if mode == "live":
        rec.update(status="approved", broker=live, paper=False)
        rec.pop("paper_order_id")
        state.setdefault("orders", []).append(rec)
        write_json(a.state, state)
        print(t("recorded", lang, i=a.index, text=a.yes_text))
        print(t("live_wait", lang, broker=BROKER_TITLES[live], plain=words))
        print(t("live_place", lang, i=a.index))
        return 0
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print(t("recorded", lang, i=a.index, text=a.yes_text))
    print(t("paper_placed", lang, id=rec["paper_order_id"], plain=words))
    print(t("when_fills", lang, i=a.index))
    return 0


ACTORS = ("user", "autopilot")


def actor_of(a, mode):
    """Who typed the yes: "user" (the default) or "autopilot", trader-autopilot's machine yes, which is for the
    PAPER account only: on a live config it is refused before anything is recorded."""
    actor = str(getattr(a, "actor", None) or "user").strip().lower()
    if actor not in ACTORS:
        raise TicketError("--actor is user or autopilot, not %r" % actor)
    if actor == "autopilot" and mode != "paper":
        raise TicketError("an auto-pilot yes is for the PAPER account only and the config is live: the auto-pilot "
                          "never approves a live order. Nothing was recorded or sent")
    return actor


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    if a.basket or (a.index is None and _is_basket_file(a)):
        doc, orders = _basket_load(a)
        bid = basket_id(orders)
        state.setdefault("orders", []).append({"at": ist_iso(now), "date": today, "basket_id": bid,
                                               "symbol": underlying_of(orders[0]), "status": "declined",
                                               "plain": "basket of %d legs on %s" % (len(orders), underlying_of(orders[0])),
                                               "yes_text": a.reply or "no"})
        write_json(a.state, state)
        print("Recorded: you said no to the basket %s (%d legs on %s). Nothing was sent." % (
            bid, len(orders), underlying_of(orders[0])))
        return 0
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": str(order.get("side", "")).upper(),
           "plain": plain(order, cfg), "status": "declined", "yes_text": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    lang = pick_lang(getattr(a, "lang", None), cfg)
    print(t("declined", lang, i=a.index, plain=" / ".join(plain_words(order, cfg, lang))))
    return 0


def cmd_fill(a):
    if a.basket:
        return cmd_fill_basket(a)
    cfg = read_json(a.config, "config", required=False) or {}
    paper_only(cfg)
    if a.index is not None and _is_basket_file(a):
        raise TicketError((BASKET_WORDS % a.orders) + "; nothing was filled")
    now, today = _clock(a)
    state = load_state(a.state)
    fp, placed, leg = None, None, a.leg or "order"
    if a.index is not None:
        doc = read_json(a.orders, "orders file")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        order = _pick(orders, a.index)
        fp = fingerprint(order)
        got = records(state, fp, today, "placed")
        if not got:
            raise TicketError("order %d has no yes recorded today (ticket.py confirm first); nothing to fill" % a.index)
        placed = got[-1]
        sym, qty = placed["symbol"], int(placed["qty"])
        product = placed.get("product", "MIS")
        fills = {r.get("leg") for r in records(state, fp, today, "filled")}
        if leg == "order":
            side = placed["side"]
            if "order" in fills:
                raise TicketError("order %d is already filled" % a.index)
        else:
            if not placed.get("has_legs"):
                raise TicketError("order %d has no stop or target leg" % a.index)
            if "order" not in fills:
                raise TicketError("the entry of order %d has not filled yet: fill it first" % a.index)
            if {"stop", "target"} & fills or records(state, fp, today, "cancelled"):
                raise TicketError("the stop and target of order %d are closed already (one filled or the position "
                                  "was flattened)" % a.index)
            side = "SELL" if placed["side"] == "BUY" else "BUY"
    else:
        if not (a.symbol and a.side and a.qty):
            raise TicketError("say what filled: --index N, or --symbol, --side, --qty (and --price)")
        sym, side, qty = a.symbol.strip().upper(), a.side.upper(), int(a.qty)
        product = (a.product or merged_config(cfg)["product"]).upper()
        order = None
    # the price
    price, src = _f(a.price), "your --price"
    if price is None and leg == "stop":
        price, src = stop_of(order), "the stop trigger (a real stop can fill worse)"
    elif price is None and leg == "target":
        price, src = target_of(order), "the target limit"
    if price is None:
        price, why = fresh_price(a.prices, sym, now)
        if price is None:
            raise TicketError("no fill price: pass --price, or refresh %s (%s)" % (a.prices, why))
        src = "the fresh price in %s" % a.prices
    if price <= 0 or qty <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if order is not None and leg == "order" and str(order.get("order_type", "")).upper() == "LIMIT":
        lim = _f(order.get("price"))
        if side == "BUY" and price > lim + 0.005:
            raise TicketError("a BUY LIMIT at %s cannot fill at %s; it waits" % (_p(lim), _p(price)))
        if side == "SELL" and price < lim - 0.005:
            raise TicketError("a SELL LIMIT at %s cannot fill at %s; it waits" % (_p(lim), _p(price)))
    ch = side_charges(side, price, qty, cfg, product)
    pf = load_portfolio(a.portfolio, cfg)
    realised = apply_fill(pf, sym, side, qty, price, ch["total"], product)
    save_portfolio(a.portfolio, pf)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": sym, "side": side, "qty": qty,
           "price": round_paise(price), "charges": ch["total"], "leg": leg, "product": product, "paper": True,
           "plain": "%s %d %s at %s (%s)" % (side, qty, sym, _p(price), leg)}
    if fp:
        rec["fingerprint"] = fp
        rec["index"] = a.index
    state.setdefault("orders", []).append(rec)
    cancelled = []
    if leg in ("stop", "target"):
        cancelled.append(("target" if leg == "stop" else "stop", fp))
    elif side == "SELL" and sym not in pf["positions"]:
        for r in open_legs(state, today, sym):
            cancelled.append(("stop and target", r["fingerprint"]))
    for what, f in cancelled:
        state["orders"].append({"at": ist_iso(now), "date": today, "status": "cancelled", "symbol": sym,
                                "fingerprint": f, "leg": what, "paper": True,
                                "plain": "cancelled the %s of %s" % (what, sym)})
    write_json(a.state, state)
    trade = {"at": ist_iso(now), "date": today, "symbol": sym, "exchange": "NSE", "product": product, "side": side,
             "qty": qty, "price": round_paise(price), "value": round_paise(qty * price), "charges": ch["total"],
             "charges_detail": ch, "leg": leg, "paper": True}
    if realised is not None:
        trade["realised_pnl"] = realised
    if fp:
        trade["fingerprint"] = fp
    log_trade(a.trades, trade)
    print("PAPER fill: %s %d %s at %s (%s). Charges about %s. Cash now %s."
          % (side, qty, sym, _p(price), src, inr(ch["total"]), inr(pf["cash"])))
    if realised is not None:
        print("Realised on this sell, after buy and sell charges: %s." % inr(realised))
    for what, _ in cancelled:
        print("Cancelled the %s of %s (paper)." % (what, sym))
    if order is not None and leg == "order" and placed.get("has_legs"):
        print("The SL-M stop at %s and the LIMIT target at %s are now working: when one fills, the other is "
              "cancelled. Record it with: ticket.py fill --index %d --leg stop|target"
              % (_p(stop_of(order)), _p(target_of(order)), a.index))
    return 0


def fno_list(a, cfg):
    """NSE's F&O stocks: the config's fno_stocks, else trader/fno_stocks.json (written by the
    scanner and the entry timer), else None (every stock is then treated as an F&O stock)."""
    cache = os.path.join(os.path.dirname(a.config) or ".", "fno_stocks.json")
    return load_fno_stocks(cfg, cache, fetch=False)[0]


def flat_by_for(sym, day, c, fno, plan=None):
    """(time, note): the plan's flat_by for its symbol, else the config's, pulled in to the
    safe time for this stock on this day (SESSION_RULES and broker_profile)."""
    want = (plan.get("flat_by") if plan and str(plan.get("symbol", "")).upper() == sym else None) or c["flat_by"]
    return safe_flat_by(day, segment_of(sym, fno), dict(c, flat_by=want))


def square_off_words(day, c, fno, syms):
    """What the broker does at the end of the day, in words."""
    if not c.get("broker_profile"):
        return ("Brokers auto square-off MIS positions from about 15:05 (F&O stocks at some brokers) to 15:26 IST, "
                "usually with an extra fee; set broker_profile in the config for your broker's times")
    times = sorted({square_off_for(day, segment_of(s, fno), c)[0] for s in syms} - {None})
    return "%s squares off MIS at %s IST, usually with an extra fee" % (
        c["broker_profile"], " / ".join(t.strftime("%H:%M") for t in times))


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    c = merged_config(cfg)
    now, today = _clock(a)
    fno = fno_list(a, cfg)
    pf = load_portfolio(a.portfolio, cfg)
    plan = read_json(a.plan, "plan", required=False) or {}
    want = a.symbol.strip().upper() if a.symbol else None
    orders, skipped = [], []
    for sym, p in sorted(pf["positions"].items()):
        if want and sym != want:
            continue
        q = int(_f(p.get("qty")) or 0)
        if q <= 0:
            continue
        prod = str(p.get("product") or "MIS").upper()
        if prod != "MIS" and not a.include_cnc:
            skipped.append("%s %d (%s, delivery: kept)" % (sym, q, prod))
            continue
        flat_by = flat_by_for(sym, today, c, fno, plan)[0].strftime("%H:%M")
        orders.append({"symbol": sym, "exchange": "NSE", "product": prod, "side": "SELL", "qty": q,
                       "order_type": "MARKET", "price": None, "validity": "DAY",
                       "why": "flat by %s IST: closes the %s %s position before the broker's MIS auto square-off "
                              "(qty from %s)" % (flat_by, sym, prod, a.portfolio)})
    fno_hint = fno_flatten_words(pf, today)
    if not orders:
        raise TicketError("nothing to flatten: no open MIS positions%s in %s%s%s"
                          % ((" for " + want) if want else "", a.portfolio,
                             ("; kept " + ", ".join(skipped)) if skipped else "", ("; " + fno_hint) if fno_hint else ""))
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker",
           "created_at": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "status": "proposed", "paper": True,
           "note": "Flatten tickets. Nothing is placed until trader-risk passes each one and you say yes to it.",
           "source": "ticket.py flatten on %s" % today, "orders": orders}
    write_json(a.orders, doc)
    fb = min(flat_by_for(o["symbol"], today, c, fno, plan)[0] for o in orders)
    late = now.time() >= fb
    for o in orders:
        print("Flatten: %s." % plain(o, cfg))
    for s in skipped:
        print("Not flattened: %s." % s)
    if fno_hint:
        print(fno_hint[0].upper() + fno_hint[1:] + ".")
    print("Wrote %d ticket%s to %s%s." % (len(orders), "" if len(orders) == 1 else "s", a.orders,
                                          (" (it replaced %d earlier proposal%s)" % (replaced, "" if replaced == 1 else "s"))
                                          if replaced else ""))
    if late:
        print("It is %s IST, past your flat_by %s: do this now. %s." % (
            now.strftime("%H:%M"), fb.strftime("%H:%M"), square_off_words(today, c, fno, [o["symbol"] for o in orders])))
    print("Next: trader-risk (check.py; a sell that only closes what you hold is never blocked by the day limits), "
          "then ticket.py show and the user's yes for each ticket. When a flatten sell fills, the paper stop and "
          "target of that position are cancelled.")
    return 0


def fno_flatten_words(pf, today):
    """F&O strategies are closed as one basket each (ticket.py adjust --close-all), not by flatten's tickets."""
    open_ = {}
    for p in fno_positions(pf):
        open_.setdefault(p.get("strategy_id"), set()).add(str(p.get("product") or "NRML").upper())
    if not open_:
        return ""
    parts = ["%s (%s)" % (sid, "/".join(sorted(prods))) for sid, prods in sorted(open_.items(), key=lambda x: str(x[0]))]
    mis = [sid for sid, prods in open_.items() if "MIS" in prods]
    return ("F&O strategies are closed as one basket each, hedge first: ticket.py adjust --strategy ID --close-all "
            "for %s%s" % (", ".join(parts), (" (MIS: before the broker's square-off)" if mis else "")))


def hhmm_time(text):
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    return datetime.time(int(m.group(1)), int(m.group(2))) if m else None


def cmd_status(a):
    cfg = read_json(a.config, "config", required=False) or {}
    c = merged_config(cfg)
    now, today = _clock(a)
    pf = load_portfolio(a.portfolio, cfg)
    state = load_state(a.state)
    trades = (read_json(a.trades, "trades log", required=False) or {}).get("trades", [])
    lines = ["PAPER account at %s IST" % now.strftime("%Y-%m-%d %H:%M"), "Cash %s" % inr(pf["cash"])]
    if pf["positions"]:
        lines.append("Positions:")
        for sym, p in sorted(pf["positions"].items()):
            lines.append("  %s %s shares at %s average (%s)" % (sym, _q(p.get("qty", 0)), _p(p.get("avg_price", 0)),
                                                              p.get("product", "MIS")))
    else:
        lines.append("No open positions.")
    for r in open_legs(state, today):
        lines.append("Working: %s stop %s / target %s (order %s)" % (r["symbol"], _p(r["stop"] or 0),
                                                                    _p(r["target"] or 0), r.get("index")))
    fno = fno_positions(pf)
    if fno or _f(pf.get("margin_blocked")):
        lines.append("Margin blocked %s, free cash %s." % (inr(pf.get("margin_blocked") or 0), inr(free_cash(pf))))
        for sid, st in sorted((pf.get("strategies") or {}).items()):
            mine = fno_positions(pf, sid)
            if not mine:
                continue
            lines.append("F&O %s %s on %s, margin %s, realised %s:" % (sid, st.get("name"), st.get("underlying"),
                                                                   inr(st.get("margin") or 0), inr(st.get("realised") or 0)))
            for p in mine:
                lines.append("  %s %+d at %s (%s)" % (contract_words(p), int(p["qty"]), _p(p["avg_price"]),
                                                     p.get("product")))
        if fno:
            lines.append("Mark them to the latest chain with ticket.py mtm.")
    placed = records(state, today=today, status="placed")
    entries = [r for r in placed if r.get("kind") == "entry"]
    at_risk = sum(_f(r.get("max_loss")) or 0 for r in entries)
    lines.append("Today: %d of %s orders, %d of %s trades, %s of %s daily loss at risk."
                 % (len(placed), c["max_orders_per_day"], len(entries), c["max_trades_per_day"], inr(at_risk),
                    inr(c["max_daily_loss"])))
    today_trades = [t for t in trades if t.get("date") == today]
    if today_trades:
        lines.append("Fills today:")
        for t in today_trades:
            lines.append("  %s %s %d %s at %s, charges %s%s" % (str(t.get("at", ""))[11:16], t["side"], t["qty"],
                                                              t["symbol"], _p(t["price"]), inr(t["charges"]),
                                                              (", realised %s" % inr(t["realised_pnl"]))
                                                              if t.get("realised_pnl") is not None else ""))
        realised = sum(t.get("realised_pnl") or 0 for t in today_trades)
        charges = sum(t.get("charges") or 0 for t in today_trades)
        lines.append("Realised today %s (closed trades, after all their charges); charges paid today %s."
                     % (inr(realised), inr(charges)))
    mis = [s for s, p in pf["positions"].items() if str(p.get("product", "MIS")).upper() == "MIS"]
    if mis:
        fno = fno_list(a, cfg)
        fb = min(flat_by_for(s, today, c, fno)[0] for s in mis)
        if now.time() >= fb:
            lines.append("Past flat_by %s with MIS open (%s): run ticket.py flatten now." % (
                fb.strftime("%H:%M"), ", ".join(sorted(mis))))
    lines.append("Paper trading. Education and tooling, not financial advice.")
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------- paper F&O: contracts, quotes, fills, margin

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
OPTION_TICK = 0.05
EXPIRY_TIME = datetime.time(15, 30)       # options and futures settle on the close of expiry day
MIN_YEARS = 1.0 / (365 * 24)
YEAR_MINUTES = 525600.0
PAPER_DEFAULTS = {
    "paper_spread_share": 0.5,    # fill at mid +/- this share of the bid/ask spread (0.5: at the far side)
    "paper_slippage_bps": 10,     # with no two-sided quote: the last price +/- this many basis points
    "market_protection_pct": 2,   # MARKET -> LIMIT this far from the last price (brokers' MPP for API orders)
    "paper_partial_fills": False,  # a seeded share of single-leg fills comes in part
    "paper_partial_chance": 0.5,
    "paper_seed": 7,
    "shadow": False,              # record a paper twin of every live order sent by ticket.py place
}
STOCK_EXPIRY_WARN_DAYS = 3        # warn this many days before a stock option's (physical) expiry
# SPAN-style scan for a paper estimate when trader/strategy.json does not describe the basket: same
# numbers and method as trader-strategy (NSE SPAN file 30-Oct-2026 for NIFTY; a typical stock scan).
SPAN_DEFAULTS = {
    "index": {"scan_pct": 9.3, "vol_scan": 4.0, "elm_pct": 2.0,
              "source": "NSE SPAN file 30-Oct-2026: NIFTY priceScan 2143.94 / spot 23053.10, volScan 0.04"},
    "stock": {"scan_pct": 20.0, "vol_scan": 10.0, "elm_pct": 3.5,
              "source": "typical F&O stock scan (12% to 25%), not read from a SPAN file; ELM from NSE's ael file"},
}
EXPIRY_DAY_ELM_PCT = 2.0
EXTREME_MOVE, EXTREME_WEIGHT = 3.0, 0.30


def paper_setting(cfg, key):
    v = (cfg or {}).get(key)
    return PAPER_DEFAULTS[key] if v is None else v


def is_fno(o):
    """An F&O order or position: exchange NFO, or segment OPT / FUT."""
    o = o or {}
    return str(o.get("exchange") or "").upper() == "NFO" or str(o.get("segment") or "").upper() in ("OPT", "FUT")


def fno_segment(o):
    seg = str(o.get("segment") or "").upper()
    if seg in ("OPT", "FUT"):
        return seg
    return "FUT" if str(o.get("instrument") or "").upper().startswith("FUT") or not o.get("option_type") else "OPT"


def underlying_of(o):
    return str(o.get("underlying") or o.get("symbol") or "").strip().upper()


def kind_of_underlying(u):
    return "index" if u in INDICES else "stock"


def contract_key(o):
    """NIFTY-2026-11-03-22700-PE or NIFTY-2026-11-24-FUT: one contract, broker-neutral."""
    u, exp = underlying_of(o), str(o.get("expiry") or "")
    if fno_segment(o) == "FUT":
        return "%s-%s-FUT" % (u, exp)
    return "%s-%s-%s-%s" % (u, exp, _q(o.get("strike")), str(o.get("option_type") or "").upper())


def nse_day(iso):
    try:
        return datetime.date.fromisoformat(str(iso)[:10]).strftime("%d-%b-%Y")
    except ValueError:
        return str(iso)


def contract_words(o):
    """"NIFTY 22700 PE 03-Nov-2026" or "NIFTY FUT 24-Nov-2026"."""
    if fno_segment(o) == "FUT":
        return "%s FUT %s" % (underlying_of(o), nse_day(o.get("expiry")))
    return "%s %s %s %s" % (underlying_of(o), _q(o.get("strike")), str(o.get("option_type") or "").upper(),
                            nse_day(o.get("expiry")))


def lot_of(o):
    lot = int(_f(o.get("lot_size")) or 0)
    return lot if lot > 0 else 1


def fno_plain(o):
    """One F&O order in plain words."""
    side, q = str(o.get("side", "")).upper(), int(_f(o.get("qty")) or 0)
    lot = lot_of(o)
    lots = q // lot if lot and q % lot == 0 else None
    otype = str(o.get("order_type") or "MARKET").upper()
    how = "LIMIT %s" % _p(o.get("price")) if otype == "LIMIT" else (
        "SL trigger %s, limit %s" % (_p(o.get("trigger_price")), _p(o.get("price"))) if otype == "SL" else "MARKET")
    prod = str(o.get("product") or "NRML").upper()
    size = ("%d lot%s (%d)" % (lots, "" if lots == 1 else "s", q)) if lots else "%d units" % q
    return "%s %s %s on NFO, %s (%s), %s, %s" % (side, size, contract_words(o), prod, PRODUCT_WORDS.get(prod, prod), how,
                                                 VALIDITY_WORDS.get(str(o.get("validity") or "DAY").upper(), "DAY"))


def tick_adverse(x, side, tick=OPTION_TICK):
    """A price on the tick, rounded against you: up for a buy, down for a sell (never below one tick)."""
    t = max(to_paise(tick), 1)
    p = Decimal(repr(float(x))) * 100 / t
    n = int(p.to_integral_value(rounding="ROUND_CEILING" if side == "BUY" else "ROUND_FLOOR"))
    return max(round(n * t / 100.0, 2), round(t / 100.0, 2))


def years_left(expiry, now):
    """Calendar years (minutes / 525,600) from now to 15:30 IST on the expiry date; 0 once past."""
    try:
        d = datetime.date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return 0.0
    end = datetime.datetime.combine(d, EXPIRY_TIME, IST)
    return max(0.0, (end - now).total_seconds() / 60.0 / YEAR_MINUTES)


# ---------------------------------------------------------------- Black-76 (trader-options' functions when it sits next to this skill)

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _b76_price(F, K, T, r, sigma, kind):
    df = math.exp(-r * max(T, 0.0))
    if T <= 0 or not sigma or sigma <= 0:
        return df * max(0.0, (F - K) if kind == "CE" else (K - F))
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    d2 = d1 - vt
    if kind == "CE":
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def _b76_greeks(F, K, T, r, sigma, kind):
    if sigma is None or T < MIN_YEARS or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    df = math.exp(-r * T)
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    price = _b76_price(F, K, T, r, sigma, kind)
    delta = df * _ncdf(d1) if kind == "CE" else -df * _ncdf(-d1)
    theta_year = -df * F * pdf * sigma / (2.0 * math.sqrt(T)) + r * price
    return {"delta": delta, "gamma": df * pdf / (F * vt), "theta": theta_year / 365.0,
            "vega": df * F * pdf * math.sqrt(T) / 100.0}


def options_module():
    """trader-options/options.py when that skill sits next to this one (loaded by path), else None."""
    import importlib.util
    name = "bt_trader_options_for_broker"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-options", "options.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:          # a broken neighbour must not stop the paper broker
        return None
    sys.modules[name] = mod
    return mod


def b76_price(F, K, T, r, sigma, kind):
    m = options_module()
    if m is not None:
        return m.black76_price(F, K, T, r, sigma or 0.0, kind)
    return _b76_price(F, K, T, r, sigma, kind)


def b76_greeks(F, K, T, r, sigma, kind):
    """Per unit: delta dPrice/dF, gamma per point, theta per calendar day, vega per vol point."""
    m = options_module()
    if m is not None:
        return m.black76_greeks(F, K, T, r, sigma, kind)
    return _b76_greeks(F, K, T, r, sigma, kind)


def greeks_source():
    return "trader-options black76_greeks" if options_module() is not None else "trader-broker's own Black-76 copy"


# ---------------------------------------------------------------- the latest chain (trader-options' options.json) and price bands

def load_markets(paths):
    """{(UNDERLYING, expiry ISO): options doc} from trader-options' JSON (schema bharat-trader/options/1;
    one doc or a list; .gz is fine). Files that are missing are skipped."""
    import gzip
    out = {}
    for p in paths or []:
        if not p or not os.path.exists(p):
            continue
        try:
            if p.endswith(".gz"):
                with gzip.open(p, "rb") as fh:
                    doc = json.loads(fh.read().decode("utf-8"))
            else:
                with open(p, encoding="utf-8") as fh:
                    doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise TicketError("%s is not a readable options file: %s" % (p, e))
        for d in (doc if isinstance(doc, list) else [doc]):
            if isinstance(d, dict) and d.get("underlying") and d.get("expiry") and isinstance(d.get("rows"), list):
                out[(str(d["underlying"]).upper(), str(d["expiry"]))] = d
    return out


def quote_for(markets, o):
    """The latest quote for one contract from the chain: {"bid", "ask", "ltp", "mid", "mark", "iv", "bid_qty",
    "ask_qty", "forward", "rate", "asof", "source"}, or None. A future is marked at the chain's parity forward
    for its expiry (no futures quote in the option chain)."""
    u, exp = underlying_of(o), str(o.get("expiry") or "")
    doc = (markets or {}).get((u, exp))
    if doc is None:
        return None
    base = {"forward": _f(doc.get("forward")), "rate": _f(doc.get("rate")) or 0.0, "asof": doc.get("asof"),
            "kind": doc.get("kind") or kind_of_underlying(u)}
    if fno_segment(o) == "FUT":
        fwd = base["forward"]
        if not fwd:
            return None
        return dict(base, bid=None, ask=None, ltp=fwd, mid=fwd, mark=fwd, iv=None, bid_qty=0, ask_qty=0,
                    source="the parity forward of the %s %s chain (%s)" % (u, nse_day(exp), doc.get("asof")))
    k, t = _f(o.get("strike")), str(o.get("option_type") or "").lower()
    row = next((r for r in doc["rows"] if _f(r.get("strike")) == k), None)
    side = (row or {}).get(t)
    if not side:
        return None
    bid, ask = _f(side.get("bid")) or 0.0, _f(side.get("ask")) or 0.0
    mid = _f(side.get("mid")) or ((bid + ask) / 2.0 if bid > 0 and ask >= bid else None)
    mark = _f(side.get("price")) or mid or _f(side.get("ltp"))
    return dict(base, bid=bid or None, ask=ask or None, ltp=_f(side.get("ltp")), mid=mid, mark=mark,
                iv=_f(side.get("iv")), bid_qty=int(_f(side.get("bid_qty")) or 0), ask_qty=int(_f(side.get("ask_qty")) or 0),
                source="the %s %s chain (%s)" % (u, nse_day(exp), doc.get("asof")))


NSE_EPOCH = datetime.datetime(1980, 1, 1)


def load_bands(path):
    """{contract_key: (lower, upper, source)} from NSE's F&O contract master (NSE_FO_contract_DDMMYYYY.csv:
    MinPric / MaxPric in paise, expiry in seconds since 1980) or a JSON {key: {"lower", "upper"}}."""
    if not path or not os.path.exists(path):
        return {}
    import csv
    out = {}
    if path.endswith(".json"):
        doc = read_json(path, "price bands") or {}
        for k, v in doc.items():
            if isinstance(v, dict) and _f(v.get("lower")) is not None and _f(v.get("upper")) is not None:
                out[k] = (_f(v["lower"]), _f(v["upper"]), v.get("source") or path)
        return out
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            r = {str(k).strip(): str(v or "").strip() for k, v in r.items()}
            try:
                exp = (NSE_EPOCH + datetime.timedelta(seconds=int(r["XpryDt"]))).date().isoformat()
                lo, hi = int(r["MinPric"]) / 100.0, int(r["MaxPric"]) / 100.0
            except (KeyError, ValueError):
                continue
            inst = r.get("FinInstrmNm", "")
            o = {"symbol": r.get("TckrSymb"), "expiry": exp, "segment": "FUT" if inst.startswith("FUT") else "OPT"}
            if o["segment"] == "OPT":
                o.update(strike=int(r.get("StrkPric") or 0) / 100.0, option_type=r.get("OptnTp"))
            out[contract_key(o)] = (lo, hi, "NSE contract master %s" % os.path.basename(path))
    return out


# ---------------------------------------------------------------- fill realism

def paper_fill_price(o, q, cfg, band=None, forced=None):
    """Where one F&O order would fill on paper now. Returns {"ok", "price", "why", "notes", "source",
    "limit"}: a buy pays mid + paper_spread_share x spread (the ask at the default 0.5), a sell gets
    mid - that; with no two-sided quote, the last price +/- paper_slippage_bps. A MARKET order is turned
    into a LIMIT market_protection_pct away from the last price (SEBI's rule for API orders: brokers
    send market orders with market protection) and is cancelled if the book is beyond it. A LIMIT
    fills only at its price or better. A price outside the contract's band (when known) is refused."""
    side = str(o.get("side", "")).upper()
    otype = str(o.get("order_type") or "MARKET").upper()
    notes, limit = [], _f(o.get("price")) if otype in ("LIMIT", "SL") else None
    if forced is not None:
        px, src = round_paise(forced), "your --leg-price"
        ref = px
    else:
        if not q:
            return {"ok": False, "why": "no quote for %s in the chain files (refresh trader/options.json)"
                                       % contract_words(o), "notes": notes}
        bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
        if bid > 0 and ask >= bid:
            share = float(paper_setting(cfg, "paper_spread_share"))
            mid = (bid + ask) / 2.0
            raw = mid + share * (ask - bid) if side == "BUY" else mid - share * (ask - bid)
            src = "bid %s / ask %s, filled %s%% of the spread from the mid" % (_p(bid), _p(ask), _q(share * 100))
            ref = q.get("ltp") or mid
        else:
            ref = q.get("ltp") or q.get("mark")
            if not ref:
                return {"ok": False, "why": "no price for %s in the chain" % contract_words(o), "notes": notes}
            bps = float(paper_setting(cfg, "paper_slippage_bps"))
            raw = ref * (1 + bps / 10000.0) if side == "BUY" else ref * (1 - bps / 10000.0)
            src = "last %s %s %s bps slippage (no two-sided quote)" % (_p(ref), "+" if side == "BUY" else "-", _q(bps))
        px = tick_adverse(raw, side, OPTION_TICK if is_fno(o) else (0.01 if ref < 250 else 0.05))
    if band and limit is not None and not band[0] - 0.001 <= limit <= band[1] + 0.001:
        return {"ok": False, "why": "rejected: the limit price %s is outside %s's price band %s to %s (%s)"
                                   % (_p(limit), contract_words(o), _p(band[0]), _p(band[1]), band[2]),
                "notes": notes, "limit": limit}
    if otype == "MARKET":
        mpp = float(paper_setting(cfg, "market_protection_pct"))
        lim = ref * (1 + mpp / 100.0) if side == "BUY" else ref * (1 - mpp / 100.0)
        limit = tick_adverse(lim, "SELL" if side == "BUY" else "BUY")      # never past the protection
        notes.append("MARKET sent as LIMIT %s: market protection %s%% from %s (brokers convert API market orders "
                     "since SEBI's retail-algo rules; paper does the same)" % (_p(limit), _q(mpp), _p(ref)))
        if (side == "BUY" and px > limit + 0.001) or (side == "SELL" and px < limit - 0.001):
            return {"ok": False, "why": "cancelled by market protection: the book is at %s, past the %s limit"
                                       % (_p(px), _p(limit)), "notes": notes, "limit": limit}
    elif limit is not None:
        if side == "BUY" and px > limit + 0.001:
            return {"ok": False, "why": "the BUY LIMIT %s waits: the book is at %s" % (_p(limit), _p(px)),
                    "notes": notes, "limit": limit}
        if side == "SELL" and px < limit - 0.001:
            return {"ok": False, "why": "the SELL LIMIT %s waits: the book is at %s" % (_p(limit), _p(px)),
                    "notes": notes, "limit": limit}
    if band:
        lo, hi, bsrc = band
        for what, v in (("limit", limit), ("fill", px)):
            if v is not None and not lo - 0.001 <= v <= hi + 0.001:
                return {"ok": False, "why": "rejected: the %s price %s is outside %s's price band %s to %s (%s)"
                                           % (what, _p(v), contract_words(o), _p(lo), _p(hi), bsrc),
                        "notes": notes, "limit": limit}
    return {"ok": True, "price": px, "source": src, "notes": notes, "limit": limit}


def partial_lots(o, q, cfg, rng, want_qty):
    """Units that fill now: all of want_qty, or (paper_partial_fills on) the top-of-book depth or a seeded
    random number of whole lots."""
    if not paper_setting(cfg, "paper_partial_fills"):
        return want_qty
    lot = lot_of(o)
    lots = want_qty // lot
    if lots <= 1:
        return want_qty
    side = str(o.get("side", "")).upper()
    depth = (q or {}).get("ask_qty" if side == "BUY" else "bid_qty") or 0
    n = lots
    if depth and depth // lot < lots:
        n = max(depth // lot, 0)
    if rng.random() < float(paper_setting(cfg, "paper_partial_chance")):
        n = min(n, rng.randint(1, lots - 1))
    return n * lot


# ---------------------------------------------------------------- margin: what a broker would block (SPAN-style estimate)

def span_estimate(legs, now, kind):
    """A SPAN-style estimate for one structure, excluding premium (paid from cash): the worst loss of 14
    price/vol scenarios (0, +/-1/3, +/-2/3, +/-1 x the price scan, vol up and down) and +/-3 x the scan
    at 30% weight, on the WHOLE structure (hedges count), plus exposure (ELM) on the notional of every
    short option and future, plus 2% more on index shorts expiring today. legs: [{"segment", "option_type",
    "strike", "expiry", "qty" (signed units), "forward", "iv" (percent), "rate", "avg_price"}]."""
    p = SPAN_DEFAULTS[kind if kind in SPAN_DEFAULTS else "stock"]
    today = now.date().isoformat()
    shorts = [l for l in legs if l["segment"] == "FUT" or l["qty"] < 0]
    if not shorts:
        return {"total": 0.0, "scan_risk": 0.0, "exposure": 0.0, "expiry_day_elm": 0.0, "method": "premium",
                "source": "only bought options: the premium is paid up front, nothing is blocked"}

    def value(l, m, dv):
        F = (l.get("forward") or l.get("avg_price") or 0.0) * (1 + m * p["scan_pct"] / 100.0)
        if l["segment"] == "FUT":
            return F
        T = years_left(l["expiry"], now)
        sig = ((l.get("iv") or 0.0) + dv) / 100.0
        return b76_price(F, float(l["strike"]), T, l.get("rate") or 0.0, max(sig, 0.0) if l.get("iv") else 0.0,
                         l["option_type"])

    base = sum(l["qty"] * value(l, 0.0, 0.0) for l in legs)
    worst = 0.0
    for m in (0.0, 1 / 3.0, -1 / 3.0, 2 / 3.0, -2 / 3.0, 1.0, -1.0):
        for dv in (p["vol_scan"], -p["vol_scan"]):
            worst = max(worst, base - sum(l["qty"] * value(l, m, dv) for l in legs))
    for m in (EXTREME_MOVE, -EXTREME_MOVE):
        worst = max(worst, (base - sum(l["qty"] * value(l, m, 0.0) for l in legs)) * EXTREME_WEIGHT)
    exposure = extra = 0.0
    for l in shorts:
        notional = abs(l["qty"]) * (l.get("forward") or l.get("avg_price") or 0.0)
        exposure += notional * p["elm_pct"] / 100.0
        if kind == "index" and l["segment"] == "OPT" and l["expiry"] == today:
            extra += notional * EXPIRY_DAY_ELM_PCT / 100.0
    return {"total": round_paise(worst + exposure + extra), "scan_risk": round_paise(worst),
            "exposure": round_paise(exposure), "expiry_day_elm": round_paise(extra), "method": "span-style",
            "source": "trader-broker estimate: %s; price scan %s%%, vol scan %s points, ELM %s%%" % (
                p["source"], _q(p["scan_pct"]), _q(p["vol_scan"]), _q(p["elm_pct"]))}


def leg_view(o, q=None, qty=None, price=None):
    """A leg for span_estimate / payoff: from an order or a position, signed units."""
    side = str(o.get("side") or ("BUY" if (_f(o.get("qty")) or 0) > 0 else "SELL")).upper()
    n = abs(int(_f(qty if qty is not None else o.get("qty")) or 0))
    return {"segment": fno_segment(o), "option_type": str(o.get("option_type") or "").upper() or None,
            "strike": _f(o.get("strike")), "expiry": str(o.get("expiry") or ""), "qty": n if side == "BUY" else -n,
            "forward": (q or {}).get("forward") or _f(o.get("forward")), "iv": (q or {}).get("iv") or _f(o.get("iv")),
            "rate": (q or {}).get("rate") or 0.0,
            "avg_price": _f(price if price is not None else (o.get("avg_price") or o.get("price"))) or 0.0}


def payoff_at(legs, x):
    """Gross P&L at expiry with the underlying at x, each leg against its own price."""
    tot = 0.0
    for l in legs:
        if l["segment"] == "FUT":
            v = x
        else:
            v = max(0.0, x - l["strike"]) if l["option_type"] == "CE" else max(0.0, l["strike"] - x)
        tot += l["qty"] * (v - l["avg_price"])
    return tot


def payoff_extremes(legs):
    """(max loss as a positive number or None when unlimited, max profit or None) at expiry. Exact for one
    expiry: the payoff is piecewise linear with kinks at the strikes."""
    ks = sorted({l["strike"] for l in legs if l["segment"] == "OPT"})
    ref = next((l["forward"] or l["avg_price"] for l in legs if (l["forward"] or l["segment"] == "FUT")), None)
    pts = [0.0] + ks + ([ref] if ref else [])
    hi = max(pts) * 2 + 1
    pts.append(hi)
    vals = [payoff_at(legs, x) for x in pts]
    up_slope = payoff_at(legs, hi + 1) - payoff_at(legs, hi)
    lo, top = min(vals), max(vals)
    loss = None if up_slope < -1e-9 else round_paise(max(0.0, -lo))
    prof = None if up_slope > 1e-9 else round_paise(top)
    return loss, prof


# ---------------------------------------------------------------- baskets: one ticket, one yes, every leg or none

def is_basket_doc(doc, orders):
    """trader-strategy's multi-leg proposal (basket: true) or a file of F&O orders only (an adjustment)."""
    if isinstance(doc, dict) and doc.get("basket") is True:
        return True
    return bool(orders) and all(isinstance(o, dict) and is_fno(o) for o in orders)


def basket_id(orders):
    """The same id trader-risk gives a basket: the fingerprint of the legs' fingerprints, in file order."""
    return fingerprint({"basket": [fingerprint(o) for o in orders]})


def hedge_first(orders):
    """Leg numbers (1-based) in the order paper fills them: every BUY first (wings, and buy-backs of shorts),
    then the SELLs, as you would place them for the margin benefit of the hedge."""
    idx = list(range(1, len(orders) + 1))
    return [i for i in idx if str(orders[i - 1].get("side", "")).upper() == "BUY"] + \
        [i for i in idx if str(orders[i - 1].get("side", "")).upper() != "BUY"]


def strategy_matches(sdoc, orders):
    """True when trader/strategy.json is this exact basket (same legs: side, type, strike, expiry, qty)."""
    if not isinstance(sdoc, dict) or not isinstance(sdoc.get("legs"), list):
        return False
    if str(sdoc.get("underlying") or "").upper() != underlying_of(orders[0]) or len(sdoc["legs"]) != len(orders):
        return False

    def k(l):
        return (str(l.get("side")).upper(), fno_segment(l), str(l.get("option_type") or "").upper(),
                _f(l.get("strike")), str(l.get("expiry")), int(_f(l.get("qty")) or 0))
    return sorted(k(l) for l in sdoc["legs"]) == sorted(k(o) for o in orders)


def leg_order_key(p):
    return (str(p.get("expiry")), p.get("segment") == "OPT", _f(p.get("strike")) or 0.0, str(p.get("option_type") or ""))


def fno_positions(pf, sid=None):
    """Open F&O positions (of one strategy), in leg order: by expiry, futures first, then by strike."""
    return sorted([p for p in pf.get("fno_positions") or [] if sid is None or p.get("strategy_id") == sid],
                  key=lambda p: (str(p.get("strategy_id")),) + leg_order_key(p))


def basket_summary(doc, orders, cfg, pf, sdoc, markets, now):
    """Totals for the basket ticket: net premium, charges, margin and max loss (of the structure after it)."""
    day = now.date().isoformat()
    adj = (doc or {}).get("adjust") if isinstance(doc, dict) else None
    sid = (adj or {}).get("strategy_id")
    prem = 0.0
    charges = 0.0
    legs = []
    for o in orders:
        px = _f(o.get("price")) or (quote_for(markets, o) or {}).get("mark") or 0.0
        q = int(_f(o.get("qty")) or 0)
        seg = fno_segment(o)
        if seg == "OPT":
            prem += (q * px) if str(o.get("side")).upper() == "SELL" else -(q * px)
        charges += side_charges(str(o.get("side")).upper(), px, q, cfg, o.get("product") or "NRML", seg, day)["total"]
        legs.append(leg_view(o, quote_for(markets, o), price=px))
    after = [leg_view(p, quote_for(markets, p)) for p in fno_positions(pf, sid)] if sid else []
    after = net_legs(after + legs)
    exit_charges = sum(side_charges("SELL" if l["qty"] > 0 else "BUY", l["avg_price"], abs(l["qty"]), cfg, "NRML",
                                    l["segment"], day)["total"] for l in after)
    under = underlying_of(orders[0])
    kind = kind_of_underlying(under)
    matched = (not sid) and strategy_matches(sdoc, orders)
    if matched and isinstance(sdoc.get("margin"), dict):
        m = sdoc["margin"]
        paid = _f((m.get("span_style") or {}).get("premium_paid")) or 0.0
        margin = round_paise(max(0.0, (_f(m.get("required")) or 0.0) - paid))
        msrc = "trader-strategy's estimate in trader/strategy.json (%s, less the premium paid from cash)" % (
            m.get("label") or m.get("method"))
    else:
        est = span_estimate(after, now, kind) if after else {"total": 0.0, "source": "nothing left open"}
        margin, msrc = est["total"], est["source"]
    single_expiry = len({l["expiry"] for l in after}) <= 1
    loss, profit = payoff_extremes(after) if after else (0.0, 0.0)
    if matched and isinstance(sdoc.get("expiry"), dict) and sdoc["expiry"].get("max_loss") is not None:
        loss = round_paise(abs(_f(sdoc["expiry"]["max_loss"])))
    held_margin = _f(((pf.get("strategies") or {}).get(sid) or {}).get("margin")) if sid else 0.0
    return {"basket_id": basket_id(orders), "legs": len(orders), "underlying": under, "kind": kind,
            "strategy_id": sid, "action": (adj or {}).get("action") or "open",
            "name": (sdoc.get("name") if matched else None) or (adj or {}).get("name") or "custom basket",
            "net_premium": round_paise(prem), "premium_kind": "credit" if prem > 0 else "debit",
            "charges": round_paise(charges), "exit_charges": round_paise(exit_charges),
            "round_trip_charges": round_paise(charges + exit_charges),
            "margin": margin, "margin_change": round_paise(margin - (held_margin or 0.0)), "margin_source": msrc,
            "max_loss": loss, "max_loss_unlimited": bool(after) and loss is None, "max_profit": profit,
            "max_loss_with_charges": round_paise(loss + charges + exit_charges) if loss is not None else None,
            "exact": single_expiry, "hedge_first": hedge_first(orders),
            "strategy_file": bool(matched)}


def net_legs(legs):
    """Combine legs of the same contract (signed units, average price of the net side)."""
    out = {}
    for l in legs:
        k = (l["segment"], l["option_type"], l["strike"], l["expiry"])
        if k not in out:
            out[k] = dict(l)
            continue
        cur = out[k]
        q = cur["qty"] + l["qty"]
        if q and (cur["qty"] > 0) == (l["qty"] > 0):
            cur["avg_price"] = (cur["avg_price"] * abs(cur["qty"]) + l["avg_price"] * abs(l["qty"])) / abs(q)
        elif q and (q > 0) != (cur["qty"] > 0):
            cur["avg_price"] = l["avg_price"]
        cur["qty"] = q
    return [l for l in out.values() if l["qty"]]


def basket_numbers(orders, s):
    nums = []
    for o in orders:
        for v in (o.get("qty"), o.get("lots"), o.get("strike"), o.get("price")):
            if _f(v) is not None:
                nums.append(_f(v))
    for k in ("net_premium", "charges", "round_trip_charges", "margin", "max_loss", "max_loss_with_charges", "legs"):
        if _f(s.get(k)) is not None:
            nums.append(abs(_f(s[k])))
    return nums


def parse_basket_yes(reply, orders, s):
    """An explicit yes for THIS basket ticket: starts with yes, no doubt, no other symbol, any number in it
    is one of this basket's (a strike, qty, price or total). Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if {"rest", "remaining", "everything", "batch", "every"} & set(words):
        return False, "a yes covers this one basket ticket, not other tickets"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    mine = underlying_of(orders[0])
    allowed = {mine, "CE", "PE", "FUT", "NRML", "MIS", "NFO", "YES", "BASKET", "LEGS", "LEG", "ALL", "BOTH", "LOT",
               "LOTS", "IST", "INR"}
    other = (set(re.findall(r"\b[A-Z][A-Z0-9&\-]{2,19}\b", raw)) | (set(INDICES) & set(re.findall(r"[A-Z]+",
                                                                                                    raw.upper())))) - allowed
    if other:
        return False, "the reply names %s, but this basket is on %s" % (", ".join(sorted(other)), underlying_of(orders[0]))
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = basket_numbers(orders, s)
        bad = [n for n in nums if not any(abs(n - w) < 0.005 for w in want)]
        if bad:
            return False, "the reply says %s, which is not in this basket ticket" % ", ".join(_q(n) for n in bad)
    return True, "explicit yes for the basket of %d legs on %s" % (len(orders), underlying_of(orders[0]))


def basket_status(orders, risk, state, today):
    """(ready, label, verdicts). READY only when trader-risk passed EVERY leg today (each fingerprint) and
    nothing was decided for this basket yet."""
    bid = basket_id(orders)
    verdicts = []
    for i, o in enumerate(orders, 1):
        rv, why = risk_verdict(risk, fingerprint(o), today)
        if rv is None:
            return False, "NOT READY: leg %d: %s" % (i, why), verdicts
        if rv.get("basket_id") and rv["basket_id"] != bid:
            return False, "NOT READY: trader-risk checked leg %d as part of another basket; run check.py again" % i, \
                verdicts
        if rv.get("verdict") != "PASS":
            return False, "BLOCKED by trader-risk: leg %d: %s" % (i, "; ".join(rv.get("reasons", []))), verdicts
        verdicts.append(rv)
    done = [r for r in records(state, today=today, status=("placed", "declined", "approved")) if r.get("basket_id") == bid]
    if done:
        st = done[-1]
        fills = [r for r in records(state, today=today, status="filled") if r.get("basket_id") == bid]
        word = st.get("status")
        if word == "placed":
            word = "placed, filled" if basket_filled(orders, state, today) else (
                "placed, part filled" if fills else "placed, waiting for the fill")
        return False, "DONE: %s at %s" % (word, st.get("at")), verdicts
    return True, "READY: trader-risk PASS on all %d legs, waiting for your yes to the whole basket" % len(orders), verdicts


def basket_filled_qty(orders, state, today):
    bid = basket_id(orders)
    got = {}
    for r in records(state, today=today, status="filled"):
        if r.get("basket_id") == bid:
            got[r.get("index")] = got.get(r.get("index"), 0) + int(r.get("qty") or 0)
    return got


def basket_filled(orders, state, today):
    got = basket_filled_qty(orders, state, today)
    return all(got.get(i, 0) >= int(_f(o.get("qty")) or 0) for i, o in enumerate(orders, 1))


def basket_view(doc, orders, cfg, risk, state, pf, sdoc, markets, now, today):
    ready, label, verdicts = basket_status(orders, risk, state, today)
    s = basket_summary(doc, orders, cfg, pf, sdoc, markets, now)
    legs = [{"index": i, "plain": fno_plain(o), "fingerprint": fingerprint(o), "order": neutral(o),
             "contract": contract_key(o), "why": o.get("why", "")} for i, o in enumerate(orders, 1)]
    return {"basket": True, "basket_id": s["basket_id"], "ready": ready, "status": label, "summary": s, "legs": legs,
            "paper": True, "broker": "paper",
            "warnings": sorted({w for v in verdicts for w in (v.get("warnings") or [])})}


def render_basket(v):
    s = v["summary"]
    lines = ["BASKET of %d leg%s on %s, PAPER account (no real money): %s%s" % (
        s["legs"], "" if s["legs"] == 1 else "s", s["underlying"], s["name"],
        (", %s %s" % (s["action"].replace("-", " "), s["strategy_id"])) if s["strategy_id"] else "")]
    for l in v["legs"]:
        lines.append("  leg %d: %s" % (l["index"], l["plain"]))
    lines.append("  paper fills hedge first: leg%s %s" % ("" if len(s["hedge_first"]) == 1 else "s",
                                                         ", ".join(str(i) for i in s["hedge_first"])))
    if s["net_premium"]:
        lines.append("  net premium: %s %s" % (inr(abs(s["net_premium"])), "received" if s["net_premium"] > 0 else "paid"))
    lines.append("  margin blocked: about %s%s (%s)" % (
        inr(s["margin"]), (", %s%s from now" % ("+" if s["margin_change"] >= 0 else "-", inr(abs(s["margin_change"]))))
        if s["strategy_id"] else "", s["margin_source"]))
    if s["max_loss_unlimited"]:
        lines.append("  max loss at expiry: UNLIMITED (a short leg is not covered)")
    elif s["max_loss"] is not None:
        lines.append("  max loss at expiry: %s%s; with charges about %s" % (
            inr(s["max_loss"]), "" if s["exact"] else " (two expiries: an estimate)", inr(s["max_loss_with_charges"])))
    lines.append("  charges: %s now, about %s for the round trip" % (inr(s["charges"]), inr(s["round_trip_charges"])))
    lines.append("  basket id %s; %s" % (v["basket_id"], v["status"]))
    for w in v.get("warnings") or []:
        lines.append("  note: " + w)
    if v["ready"]:
        lines.append("  Reply \"yes\" to place EVERY leg of this one basket on the paper account (all or nothing), "
                     "or \"no\" to skip it.")
    return "\n".join(lines)


# ---------------------------------------------------------------- the F&O ledger: positions, cash, margin

def ledger_add(pf, now, kind, amount, what, **kw):
    """One cash or margin movement. amount is the change in cash (margin rows: the change in margin blocked)."""
    row = {"at": ist_iso(now), "kind": kind, "amount": round_paise(amount), "what": what}
    row.update({k: v for k, v in kw.items() if v is not None})
    pf.setdefault("ledger", []).append(row)
    return row


def set_margin(pf, sid, amount, now, source):
    """Block (or release) margin for one strategy so that it holds `amount`; pf["margin_blocked"] is the total."""
    st = pf.setdefault("strategies", {}).setdefault(sid, {"id": sid})
    old = _f(st.get("margin")) or 0.0
    amount = round_paise(max(0.0, amount))
    if abs(amount - old) >= 0.005:
        ledger_add(pf, now, "margin_block" if amount > old else "margin_release", amount - old,
                   "%s margin %s -> %s (%s)" % (sid, inr(old), inr(amount), source), strategy_id=sid)
    st["margin"], st["margin_source"] = amount, source
    pf["margin_blocked"] = round_paise(sum(_f(x.get("margin")) or 0.0 for x in pf["strategies"].values()))
    return amount - old


def free_cash(pf):
    return round_paise((_f(pf.get("cash")) or 0.0) - (_f(pf.get("margin_blocked")) or 0.0))


def apply_fno_fill(pf, sid, o, side, qty, price, charges, now, q=None):
    """Cash, positions and the ledger after one F&O fill. An option's premium moves cash (paid on a buy,
    received on a sell); a future moves only its charges until it is closed (then its P&L). Returns the
    realised P&L of the part that closed a position (after its share of the opening charges), or None."""
    key, seg = contract_key(o), fno_segment(o)
    sign = 1 if side == "BUY" else -1
    plist = pf.setdefault("fno_positions", [])
    pos = next((p for p in plist if p.get("key") == key and p.get("strategy_id") == sid), None)
    value = round_paise(qty * price)
    if seg == "OPT":
        pf["cash"] = round_paise(float(pf["cash"]) - sign * value)
        ledger_add(pf, now, "premium_paid" if sign > 0 else "premium_received", -sign * value,
                   "%s %d %s at %s" % (side, qty, contract_words(o), _p(price)), strategy_id=sid, contract=key)
    pf["cash"] = round_paise(float(pf["cash"]) - charges)
    ledger_add(pf, now, "charges", -charges, "charges on %s %d %s" % (side, qty, contract_words(o)), strategy_id=sid,
               contract=key)
    realised = None
    if pos is None or (pos["qty"] > 0) == (sign > 0):
        if pos is None:
            pos = {"key": key, "strategy_id": sid, "underlying": underlying_of(o), "kind": kind_of_underlying(
                underlying_of(o)), "exchange": "NFO", "segment": seg,
                "instrument": o.get("instrument") or ("%s%s" % ("OPT" if seg == "OPT" else "FUT",
                                                                 "IDX" if underlying_of(o) in INDICES else "STK")),
                "expiry": str(o.get("expiry")), "strike": _f(o.get("strike")) if seg == "OPT" else None,
                "option_type": str(o.get("option_type")).upper() if seg == "OPT" else None, "lot_size": lot_of(o),
                "qty": 0, "avg_price": 0.0, "product": str(o.get("product") or "NRML").upper(), "open_charges": 0.0,
                "opened_at": ist_iso(now), "tradingsymbol": o.get("tradingsymbol")}
            plist.append(pos)
        old = abs(pos["qty"])
        pos["avg_price"] = round(((pos["avg_price"] * old) + price * qty) / (old + qty), 4)
        pos["qty"] = pos["qty"] + sign * qty
        pos["open_charges"] = round_paise(pos["open_charges"] + charges)
    else:
        held = abs(pos["qty"])
        if qty > held:
            raise TicketError("%s %d %s would close more than the %d held in %s: close it first, then open the "
                              "other side" % (side, qty, contract_words(o), held, sid))
        long_ = pos["qty"] > 0
        gross = (price - pos["avg_price"]) * qty * (1 if long_ else -1)
        share = round_paise(pos["open_charges"] * qty / held)
        realised = round_paise(gross - charges - share)
        if seg == "FUT":
            pf["cash"] = round_paise(float(pf["cash"]) + gross)
            ledger_add(pf, now, "futures_pnl", gross, "futures P&L on closing %d %s" % (qty, contract_words(o)),
                       strategy_id=sid, contract=key)
        pos["qty"] = pos["qty"] + sign * qty
        pos["open_charges"] = round_paise(pos["open_charges"] - share)
        if pos["qty"] == 0:
            plist.remove(pos)
    if q:
        mark_position(pos, q, now)
    st = pf.setdefault("strategies", {}).setdefault(sid, {"id": sid})
    st["charges"] = round_paise((_f(st.get("charges")) or 0.0) + charges)
    if realised is not None:
        st["realised"] = round_paise((_f(st.get("realised")) or 0.0) + realised)
    return realised


def mark_position(pos, q, now):
    """Keep the latest mark on the position (trader-risk reads ltp, iv and forward from it)."""
    if not pos or not q:
        return
    for k, v in (("ltp", q.get("mark")), ("iv", q.get("iv")), ("forward", q.get("forward"))):
        if v is not None:
            pos[k] = round(v, 4)
    pos["marked_at"] = q.get("asof") or ist_iso(now)


def strategy_margin(pf, sid, markets, now):
    """The paper estimate of what the strategy's open legs need (span_estimate), and its words."""
    legs = [leg_view(p, quote_for(markets, p)) for p in fno_positions(pf, sid)]
    if not legs:
        return 0.0, "nothing open: released"
    kind = kind_of_underlying(fno_positions(pf, sid)[0]["underlying"])
    est = span_estimate(legs, now, kind)
    return est["total"], est["source"]


def new_strategy_id(pf, today):
    n = sum(1 for k in (pf.get("strategies") or {}) if str(k).startswith("S-%s-" % today.replace("-", ""))) + 1
    return "S-%s-%d" % (today.replace("-", ""), n)


def fill_basket(pf, doc, orders, cfg, markets, bands, sdoc, now, state, today, leg_prices=None, rng=None):
    """All-or-nothing paper fill of a placed basket, hedge first. Returns (fills, sid, notes) or raises
    TicketError with nothing changed. fills: [{"index", "order", "side", "qty", "price", "charges",
    "charges_detail", "realised", "source", "notes"}]."""
    import copy
    rng = rng or random.Random("%s|%s" % (paper_setting(cfg, "paper_seed"), basket_id(orders)))
    leg_prices = leg_prices or {}
    done = basket_filled_qty(orders, state, today)
    plan = []
    for i in hedge_first(orders):
        o = orders[i - 1]
        left = int(_f(o.get("qty")) or 0) - done.get(i, 0)
        if left <= 0:
            continue
        q = quote_for(markets, o)
        res = paper_fill_price(o, q, cfg, bands.get(contract_key(o)), leg_prices.get(i))
        if not res["ok"]:
            raise TicketError("nothing filled (a basket fills whole or not at all): leg %d, %s: %s" % (
                i, fno_plain(o), res["why"]))
        n = partial_lots(o, q, cfg, rng, left)
        plan.append((i, o, q, res, n, left))
    if not plan:
        raise TicketError("every leg of this basket is filled already")
    if len(orders) > 1 and any(n < left for _, _, _, _, n, left in plan):
        i = next(i for i, _, _, _, n, left in plan if n < left)
        raise TicketError("nothing filled: leg %d would only fill in part (seeded paper_partial_fills), and a basket "
                          "fills whole or not at all. Try ticket.py fill --basket again" % i)
    if all(n == 0 for _, _, _, _, n, _ in plan):
        raise TicketError("nothing filled: no depth at the top of the book")
    adj = (doc or {}).get("adjust") if isinstance(doc, dict) else None
    sid = (adj or {}).get("strategy_id") or next((r.get("strategy_id") for r in records(state, today=today)
                                                  if r.get("basket_id") == basket_id(orders) and r.get("strategy_id")),
                                                 None) or new_strategy_id(pf, today)
    sim = copy.deepcopy(pf)
    st = sim.setdefault("strategies", {}).setdefault(sid, {"id": sid})
    if not st.get("opened_at"):
        st.update(opened_at=ist_iso(now), underlying=underlying_of(orders[0]), status="open",
                  name=(sdoc.get("name") if strategy_matches(sdoc, orders) else None) or "custom basket",
                  realised=0.0, charges=0.0, margin=0.0, basket_ids=[])
    st["status"] = "open"
    if basket_id(orders) not in st.setdefault("basket_ids", []):
        st["basket_ids"].append(basket_id(orders))
    fills, notes = [], []
    for step, (i, o, q, res, n, left) in enumerate(plan, 1):
        if n <= 0:
            continue
        side = str(o.get("side")).upper()
        ch = side_charges(side, res["price"], n, cfg, o.get("product") or "NRML", fno_segment(o), today)
        realised = apply_fno_fill(sim, sid, o, side, n, res["price"], ch["total"], now, q)
        need, src = strategy_margin(sim, sid, markets, now)
        set_margin(sim, sid, need, now, src)
        if free_cash(sim) < -0.005:
            raise TicketError("nothing filled: after leg %d (%s, filled hedge first) the paper account would need "
                              "%s of margin and has %s free. Add funds or a smaller basket" % (
                                  i, fno_plain(o), inr(sim["margin_blocked"]), inr(free_cash(sim) + sim["margin_blocked"])))
        fills.append({"index": i, "order": o, "side": side, "qty": n, "price": res["price"], "charges": ch["total"],
                      "charges_detail": ch, "realised": realised, "source": res["source"], "notes": res["notes"],
                      "step": step})
    if not fno_positions(sim, sid):
        set_margin(sim, sid, 0.0, now, "nothing open: released")
        st = sim["strategies"][sid]
        st.update(status="closed", closed_at=ist_iso(now))
    elif not adj and strategy_matches(sdoc, orders) and isinstance(sdoc.get("margin"), dict) and \
            not any(done.values()):
        m = sdoc["margin"]
        paid = _f((m.get("span_style") or {}).get("premium_paid")) or 0.0
        set_margin(sim, sid, max(0.0, (_f(m.get("required")) or 0.0) - paid), now,
                   "trader-strategy's estimate in trader/strategy.json, less the premium paid")
        if free_cash(sim) < -0.005:
            raise TicketError("nothing filled: the basket needs %s of margin (trader/strategy.json) and the paper "
                              "account has %s free" % (inr(sim["margin_blocked"]),
                                                       inr(free_cash(sim) + sim["margin_blocked"])))
    pf.clear()
    pf.update(sim)
    return fills, sid, notes


# ---------------------------------------------------------------- mark to market

def mtm_view(pf, markets, now, cfg=None):
    """Every open F&O position marked to the latest chain: P&L, Greeks (Black-76), per strategy and in all,
    plus the payoff at expiry of each open structure. Nothing is written."""
    day = now.date().isoformat()
    rows, groups = [], {}
    tot = {"unrealised": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    missing = []
    for p in fno_positions(pf):
        q = quote_for(markets, p)
        mark, src = (q or {}).get("mark"), (q or {}).get("source")
        if mark is None and p.get("ltp") is not None:
            mark, src = _f(p["ltp"]), "the last mark saved on the position (%s)" % p.get("marked_at")
        qty = int(p["qty"])
        r = {"key": p["key"], "strategy_id": p.get("strategy_id"), "contract": contract_words(p), "qty": qty,
             "lots": qty // lot_of(p), "lot_size": lot_of(p), "avg_price": p["avg_price"], "product": p.get("product"),
             "expiry": p["expiry"], "segment": p["segment"], "mark": mark, "mark_source": src}
        if mark is None:
            missing.append(contract_words(p))
            r["unrealised"] = None
        else:
            r["unrealised"] = round_paise((mark - p["avg_price"]) * qty)
            r["value"] = round_paise(mark * qty)
            r["exit_charges"] = side_charges("SELL" if qty > 0 else "BUY", mark, abs(qty), cfg, p.get("product") or "NRML",
                                             p["segment"], day)["total"]
            tot["unrealised"] += r["unrealised"]
        g = {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0} if p["segment"] == "FUT" else None
        if g is None:
            F = (q or {}).get("forward") or _f(p.get("forward"))
            iv = (q or {}).get("iv") or _f(p.get("iv"))
            T = years_left(p["expiry"], now)
            if F and iv:
                g = b76_greeks(F, float(p["strike"]), T, (q or {}).get("rate") or 0.0, iv / 100.0, p["option_type"])
            r["iv"] = iv
        if g and g.get("delta") is not None:
            r["greeks"] = {k: round(g[k] * qty, 4) for k in ("delta", "gamma", "theta", "vega")}
            for k in r["greeks"]:
                tot[k] += r["greeks"][k]
        else:
            r["greeks"] = None
        if p.get("kind") == "stock" and p["segment"] == "OPT":
            days = (datetime.date.fromisoformat(p["expiry"]) - now.date()).days
            if days <= STOCK_EXPIRY_WARN_DAYS:
                r["warning"] = ("stock options are physically settled: an in-the-money %s at expiry means taking or "
                                "giving delivery of %d shares, and brokers raise margins in the last days. Close it "
                                "before expiry (ticket.py adjust --close-all)" % (contract_words(p), abs(qty)))
        r["leg"] = len(groups.get(p.get("strategy_id"), [])) + 1
        rows.append(r)
        groups.setdefault(p.get("strategy_id"), []).append(r)
    strategies = []
    for sid, st in sorted((pf.get("strategies") or {}).items()):
        mine = groups.get(sid, [])
        if not mine and st.get("status") != "open":
            continue
        un = sum(r["unrealised"] or 0 for r in mine)
        g = {k: round(sum((r["greeks"] or {}).get(k, 0.0) for r in mine), 4) for k in ("delta", "gamma", "theta", "vega")}
        legs = [leg_view(p, quote_for(markets, p)) for p in fno_positions(pf, sid)]
        strategies.append({"id": sid, "name": st.get("name"), "underlying": st.get("underlying"),
                           "opened_at": st.get("opened_at"), "status": st.get("status"), "positions": mine,
                           "unrealised": round_paise(un), "realised": round_paise(_f(st.get("realised")) or 0.0),
                           "charges": round_paise(_f(st.get("charges")) or 0.0),
                           "pnl": round_paise(un + (_f(st.get("realised")) or 0.0)), "greeks": g,
                           "margin": round_paise(_f(st.get("margin")) or 0.0), "margin_source": st.get("margin_source"),
                           "payoff": payoff_curve(legs, mine) if legs else None})
    return {"as_of": ist_iso(now), "positions": rows, "strategies": strategies,
            "totals": {"unrealised": round_paise(tot["unrealised"]),
                       "greeks": {k: round(tot[k], 4) for k in ("delta", "gamma", "theta", "vega")},
                       "margin_blocked": round_paise(_f(pf.get("margin_blocked")) or 0.0),
                       "cash": round_paise(_f(pf.get("cash")) or 0.0), "free_cash": free_cash(pf)},
            "missing_quotes": missing, "greeks_from": greeks_source(),
            "model": "marks: the chain's mid (or LTP); futures at the parity forward. Greeks: Black-76 on the forward "
                     "at each leg's IV, per position (x signed units)"}


def payoff_curve(legs, rows, n=61):
    """{"x", "y", "now"}: gross P&L at expiry over +/-8% of the forward, for the UI's plot."""
    ref = next((l["forward"] for l in legs if l["forward"]), None) or next((l["strike"] for l in legs if l["strike"]), None)
    if not ref:
        return None
    lo, hi = ref * 0.92, ref * 1.08
    xs = [lo + (hi - lo) * i / (n - 1.0) for i in range(n)]
    return {"x": [round(x, 2) for x in xs], "y": [round_paise(payoff_at(legs, x)) for x in xs], "now": round(ref, 2),
            "strikes": sorted({l["strike"] for l in legs if l["strike"]})}


# ---------------------------------------------------------------- expiry settlement

def load_settlement(path):
    """{"OPT": {(U, expiry): price}, "FUT": {...}} from NSE's FOSett_prce_DDMMYYYY.csv (futures MTM settle
    prices: on expiry day the expiring contract's row is its final settlement price), the F&O bhavcopy
    (BhavCopy_NSE_FO_..._F_0000.csv: an expiring option's UndrlygPric is the final settlement price of the
    index or stock, an expiring future's SttlmPric its own) or a JSON {"NIFTY": {"2026-11-03": 23012.4}}."""
    import csv
    out = {"OPT": {}, "FUT": {}}
    if not path:
        return out
    if not os.path.exists(path):
        raise TicketError("no settlement file at %s" % path)
    if path.endswith(".json"):
        doc = read_json(path, "settlement prices") or {}
        for u, days in doc.items():
            for d, px in (days or {}).items():
                for seg in ("OPT", "FUT"):
                    out[seg][(u.upper(), d)] = float(px)
        return out
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r = {str(k).strip().upper(): str(v or "").strip() for k, v in r.items()}
        if "MTM SETTLEMENT PRICE" in r:
            try:
                d = datetime.datetime.strptime(r["EXPIRY DATE"].title(), "%d-%b-%Y").date().isoformat()
                day = datetime.datetime.strptime(r["DATE"].title(), "%d-%b-%Y").date().isoformat()
                px = float(r["MTM SETTLEMENT PRICE"])
            except (KeyError, ValueError):
                continue
            if d == day:                 # the expiring contract: its final settlement price
                out["FUT"][(r["UNDERLYING"], d)] = px
                out["OPT"].setdefault((r["UNDERLYING"], d), px)
        elif "UNDRLYGPRIC" in r:
            if r.get("XPRYDT") != r.get("TRADDT"):
                continue
            key = (r.get("TCKRSYMB"), r.get("XPRYDT"))
            try:
                if r.get("FININSTRMTP") in ("IDO", "STO"):
                    out["OPT"][key] = float(r["UNDRLYGPRIC"])
                elif r.get("FININSTRMTP") in ("IDF", "STF"):
                    out["FUT"][key] = float(r["STTLMPRIC"])
            except (KeyError, ValueError):
                continue
    return out


def settle_position(pf, p, price, cfg, now, day):
    """Settle one expiring position at the final settlement price. Index options: cash, intrinsic value.
    An in-the-money long pays STT on exercise (stt_exercise_pct of the intrinsic value); a short pays the
    intrinsic value; out of the money it expires worthless. Futures: the difference to the settlement
    price. Stock options are physically settled at NSE: paper settles them in cash like an index option and
    says so. Returns a fill-like dict."""
    qty = int(p["qty"])
    n = abs(qty)
    sid = p.get("strategy_id")
    note = ""
    if p["segment"] == "FUT":
        gross = (price - p["avg_price"]) * qty
        pf["cash"] = round_paise(float(pf["cash"]) + gross)
        ledger_add(pf, now, "settlement", gross, "%s settled at %s" % (contract_words(p), _p(price)),
                   strategy_id=sid, contract=p["key"])
        ch = {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0, "gst": 0.0, "total": 0.0}
        realised = round_paise(gross - p.get("open_charges", 0.0))
        intrinsic = None
    else:
        k = float(p["strike"])
        intrinsic = max(0.0, price - k) if p["option_type"] == "CE" else max(0.0, k - price)
        intrinsic = round_paise(intrinsic)
        value = round_paise(intrinsic * n)
        ch = {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0, "gst": 0.0, "total": 0.0}
        if intrinsic > 0 and qty > 0:
            ch = fno_round_trip_charges(0, intrinsic, n, charges_config(cfg, "NRML"), "OPT", day, exercised=True)
            pf["cash"] = round_paise(float(pf["cash"]) + value - ch["total"])
            ledger_add(pf, now, "settlement", value, "%s exercised: %s x %d" % (contract_words(p), _p(intrinsic), n),
                       strategy_id=sid, contract=p["key"])
            ledger_add(pf, now, "stt_exercise", -ch["total"], "STT on exercise of %s" % contract_words(p),
                       strategy_id=sid, contract=p["key"])
        elif intrinsic > 0:
            pf["cash"] = round_paise(float(pf["cash"]) - value)
            ledger_add(pf, now, "settlement", -value, "%s assigned: %s x %d" % (contract_words(p), _p(intrinsic), n),
                       strategy_id=sid, contract=p["key"])
        else:
            ledger_add(pf, now, "settlement", 0.0, "%s expired worthless" % contract_words(p), strategy_id=sid,
                       contract=p["key"])
        gross = (intrinsic - p["avg_price"]) * qty
        realised = round_paise(gross - ch["total"] - p.get("open_charges", 0.0))
        if p.get("kind") == "stock":
            note = ("stock options are physically settled at NSE: a real account would %s %d shares of %s at %s "
                    "(and pay delivery STT); paper settled it in cash" % (
                        "take delivery of" if (qty > 0) == (p["option_type"] == "CE") else "deliver", n,
                        p["underlying"], _q(k))) if intrinsic > 0 else ""
    pf["fno_positions"].remove(p)
    st = pf.setdefault("strategies", {}).setdefault(sid, {"id": sid})
    st["realised"] = round_paise((_f(st.get("realised")) or 0.0) + realised)
    st["charges"] = round_paise((_f(st.get("charges")) or 0.0) + ch["total"])
    return {"key": p["key"], "contract": contract_words(p), "qty": qty, "settle": price, "intrinsic": intrinsic,
            "charges": ch["total"], "charges_detail": ch, "realised": realised, "strategy_id": sid, "note": note,
            "outcome": "settled" if p["segment"] == "FUT" else ("exercised" if intrinsic and qty > 0 else
                                                                "assigned" if intrinsic else "expired worthless")}


# ---------------------------------------------------------------- shadow mode (a paper twin of each live order)

def shadow_twin(single, cfg, prices_path, markets, now):
    """What the paper broker would have filled for this live order, now: price, charges and why."""
    if is_fno(single):
        q = quote_for(markets, single)
        res = paper_fill_price(single, q, cfg)
        seg = fno_segment(single)
    else:
        sym = str(single.get("symbol") or "").upper()
        px, why = fresh_price(prices_path, sym, now)
        q = {"ltp": px, "mark": px} if px else None
        res = paper_fill_price(dict(single, segment="EQ"), q, cfg) if px else {"ok": False, "why": why, "notes": []}
        seg = "EQ"
    twin = {"filled": bool(res.get("ok")), "why": res.get("why"), "notes": res.get("notes"), "source": res.get("source")}
    if res.get("ok"):
        q_ = int(_f(single.get("qty")) or 0)
        ch = side_charges(str(single.get("side")).upper(), res["price"], q_, cfg,
                          single.get("product") or "MIS", seg, now.date().isoformat())
        twin.update(price=res["price"], charges=ch["total"], value=round_paise(res["price"] * q_))
    return twin


def shadow_report(doc, book):
    """Each shadowed live order next to its paper twin: [{..., "live_price", "diff_per_unit", "diff_rupees"}]
    and the totals. diff is how much better (+) or worse (-) the live fill was than paper's, in rupees."""
    rows, tot, n = [], 0.0, 0
    for r in (doc or {}).get("orders", []):
        live = book.get(str(r.get("broker_order_id"))) if book else None
        row = dict(r)
        tw = r.get("paper") or {}
        if live and live.get("status") == "filled" and _f(live.get("avg_price")) and tw.get("filled"):
            lp = _f(live["avg_price"])
            per = (tw["price"] - lp) if str(r.get("side")).upper() == "BUY" else (lp - tw["price"])
            qty = int(_f(live.get("filled_qty")) or _f(r.get("qty")) or 0)
            row.update(live_price=lp, live_status="filled", diff_per_unit=round_paise(per),
                       diff_rupees=round_paise(per * qty), diff_bps=round(per / tw["price"] * 10000.0, 1))
            tot += per * qty
            n += 1
        else:
            row.update(live_status=(live or {}).get("status") or "not in the order book")
        rows.append(row)
    return {"orders": rows, "compared": n, "total_diff": round_paise(tot)}


# ---------------------------------------------------------------- commands: baskets, mtm, adjust, settle, shadow-report

def _markets_arg(a):
    return load_markets(list(getattr(a, "options", None) or []) or ["trader/options.json"])


def _strategy_doc(a):
    return read_json(getattr(a, "strategy", None) or "trader/strategy.json", "strategy", required=False)


def _basket_load(a):
    doc = read_json(a.orders, "orders file (run trader-strategy --orders, or ticket.py adjust, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    if not is_basket_doc(doc, orders):
        raise TicketError("%s is not a basket (F&O legs from trader-strategy or ticket.py adjust): use --index N"
                          % a.orders)
    if any(not is_fno(o) for o in orders):
        raise TicketError("a basket holds F&O legs only (exchange NFO); %s mixes in other orders" % a.orders)
    if len({underlying_of(o) for o in orders}) != 1:
        raise TicketError("a basket is one underlying; %s has %s" % (a.orders, ", ".join(sorted({underlying_of(o)
                                                                                                for o in orders}))))
    return doc, orders


def cmd_show_basket(a, doc, orders, cfg, risk, state, today, now):
    pf = load_portfolio(getattr(a, "portfolio", None) or "trader/portfolio.json", cfg)
    v = basket_view(doc, orders, cfg, risk, state, pf, _strategy_doc(a), _markets_arg(a), now, today)
    if a.json:
        print(json.dumps(v, indent=2, ensure_ascii=False))
        return 0 if v["ready"] else 1
    print(render_basket(v))
    print("\nOne ticket for the whole basket: your yes covers these %d legs and nothing else. Education and tooling, "
          "not financial advice." % len(orders))
    return 0 if v["ready"] else 1


def cmd_confirm_basket(a):
    doc, orders = _basket_load(a)
    cfg = read_json(a.config, "config", required=False) or {}
    mode, live = mode_of(cfg)
    if mode != "paper":
        raise TicketError("baskets are paper-only in this version: send live F&O legs one by one (each its own "
                          "ticket and yes). Nothing was recorded or sent")
    actor = actor_of(a, mode)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    pf = load_portfolio(a.portfolio, cfg)
    v = basket_view(doc, orders, cfg, risk, state, pf, _strategy_doc(a), _markets_arg(a), now, today)
    if a.basket_id and a.basket_id != v["basket_id"]:
        raise TicketError("the basket in %s is %s, not %s: show the current ticket to the user again"
                          % (a.orders, v["basket_id"], a.basket_id))
    if not v["ready"]:
        raise TicketError("the basket is not ready: %s" % v["status"])
    ok, why = parse_basket_yes(a.yes_text, orders, v["summary"])
    if not ok:
        print("Not recorded: %s. Ask the user again with the basket ticket." % why)
        return 1
    s = v["summary"]
    n = len({r.get("basket_id") for r in records(state, today=today, status="placed") if r.get("basket_id")}) + 1
    adj = doc.get("adjust") if isinstance(doc, dict) else None
    for j, i in enumerate(hedge_first(orders)):
        o = orders[i - 1]
        closes = bool(adj) and adj.get("action") in ("close", "close-all") or (
            bool(adj) and adj.get("action") == "roll" and i in (adj.get("closing_legs") or []))
        state.setdefault("orders", []).append({
            "at": ist_iso(now), "date": today, "index": i, "fingerprint": fingerprint(o), "symbol": underlying_of(o),
            "side": str(o.get("side")).upper(), "qty": o.get("qty"), "product": str(o.get("product") or "NRML").upper(),
            "kind": "exit" if closes else ("adjust" if adj else "entry"), "segment": fno_segment(o),
            "contract": contract_key(o), "max_loss": (s["max_loss_with_charges"] or 0.0) if j == 0 else 0.0,
            "basket_id": s["basket_id"], "basket_legs": len(orders), "basket_max_loss": s["max_loss_with_charges"],
            "basket_margin": s["margin"], "strategy_id": (adj or {}).get("strategy_id"), "stop": None, "target": None,
            "has_legs": False, "plain": fno_plain(o), "status": "placed", "yes_text": a.yes_text, "broker": "paper",
            "paper": True, "paper_order_id": "PAPER-%s-B%d-L%d" % (today.replace("-", ""), n, i), "order": neutral(o)})
        if actor != "user":
            state["orders"][-1]["actor"] = actor
    write_json(a.state, state)
    print("Recorded your yes to the basket, word for word: \"%s\"." % a.yes_text)
    print("Placed on the PAPER account as basket PAPER-%s-B%d (%d legs, hedge first: %s):" % (
        today.replace("-", ""), n, len(orders), ", ".join(str(i) for i in s["hedge_first"])))
    for i in s["hedge_first"]:
        print("  leg %d: %s" % (i, fno_plain(orders[i - 1])))
    print("When it fills: ticket.py fill --basket (every leg at the latest chain, or none).")
    return 0


def cmd_fill_basket(a):
    cfg = read_json(a.config, "config", required=False) or {}
    paper_only(cfg)
    doc, orders = _basket_load(a)
    now, today = _clock(a)
    state = load_state(a.state)
    bid = basket_id(orders)
    placed = [r for r in records(state, today=today, status="placed") if r.get("basket_id") == bid]
    if not placed:
        raise TicketError("this basket has no yes recorded today (ticket.py confirm --basket first); nothing to fill")
    if basket_filled(orders, state, today):
        raise TicketError("this basket is filled already")
    forced = {}
    for t in a.leg_price or []:
        m = re.match(r"^\s*(\d+)\s*=\s*(\d+(?:\.\d+)?)\s*$", t)
        if not m or not 1 <= int(m.group(1)) <= len(orders):
            raise TicketError("--leg-price wants LEG=PRICE, like 2=31.40 (legs 1 to %d)" % len(orders))
        forced[int(m.group(1))] = float(m.group(2))
    pf = load_portfolio(a.portfolio, cfg)
    markets = _markets_arg(a)
    bands = load_bands(a.bands)
    fills, sid, _ = fill_basket(pf, doc, orders, cfg, markets, bands, _strategy_doc(a), now, state, today, forced)
    save_portfolio(a.portfolio, pf)
    for f in fills:
        o = f["order"]
        state["orders"].append({"at": ist_iso(now), "date": today, "status": "filled", "symbol": underlying_of(o),
                                "contract": contract_key(o), "side": f["side"], "qty": f["qty"], "price": f["price"],
                                "charges": f["charges"], "leg": "order", "index": f["index"],
                                "fingerprint": fingerprint(o), "basket_id": bid, "strategy_id": sid,
                                "product": str(o.get("product") or "NRML").upper(), "paper": True,
                                "plain": "%s %d %s at %s (basket leg %d)" % (f["side"], f["qty"], contract_words(o),
                                                                             _p(f["price"]), f["index"])})
        trade = {"at": ist_iso(now), "date": today, "symbol": contract_key(o), "underlying": underlying_of(o),
                 "exchange": "NFO", "segment": fno_segment(o), "product": str(o.get("product") or "NRML").upper(),
                 "side": f["side"], "qty": f["qty"], "price": f["price"], "value": round_paise(f["qty"] * f["price"]),
                 "charges": f["charges"], "charges_detail": f["charges_detail"], "leg": "basket leg %d" % f["index"],
                 "basket_id": bid, "strategy_id": sid, "fingerprint": fingerprint(o), "paper": True,
                 "fill_source": f["source"]}
        if f["realised"] is not None:
            trade["realised_pnl"] = f["realised"]
        log_trade(a.trades, trade)
    write_json(a.state, state)
    st = pf["strategies"][sid]
    print("PAPER basket fill, %s (%s), hedge first:" % (sid, st.get("name")))
    for f in fills:
        print("  %d. leg %d: %s %d %s at %s (%s). Charges %s%s." % (
            f["step"], f["index"], f["side"], f["qty"], contract_words(f["order"]), _p(f["price"]), f["source"],
            inr(f["charges"]), (", realised %s" % inr(f["realised"])) if f["realised"] is not None else ""))
        for n_ in f["notes"]:
            print("     " + n_)
    print("Charges on this basket %s. Margin blocked for %s: %s (%s). Cash %s, free %s." % (
        inr(sum(f["charges"] for f in fills)), sid, inr(st.get("margin") or 0), st.get("margin_source"),
        inr(pf["cash"]), inr(free_cash(pf))))
    if st.get("status") == "closed":
        print("%s is closed: realised %s after all its charges; its margin is released." % (sid, inr(st.get("realised") or 0)))
    if not basket_filled(orders, state, today):
        print("Part filled (paper_partial_fills): run ticket.py fill --basket again for the rest.")
    return 0


def cmd_mtm(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    pf = load_portfolio(a.portfolio, cfg)
    markets = _markets_arg(a)
    v = mtm_view(pf, markets, now, cfg)
    if a.save:
        for p in fno_positions(pf):
            mark_position(p, quote_for(markets, p), now)
        save_portfolio(a.portfolio, pf)
    if a.json:
        print(json.dumps(v, indent=2, ensure_ascii=False))
        return 0
    if not v["positions"]:
        print("No open F&O positions on the paper account. Cash %s." % inr(pf["cash"]))
        return 0
    print("PAPER F&O marked to market at %s IST (%s):" % (now.strftime("%Y-%m-%d %H:%M"), v["model"]))
    for s in v["strategies"]:
        print("%s %s on %s: P&L %s (open %s, realised %s), margin %s." % (
            s["id"], s["name"], s["underlying"], inr(s["pnl"]), inr(s["unrealised"]), inr(s["realised"]), inr(s["margin"])))
        print("  Greeks: delta %s, gamma %s, theta %s a day, vega %s a vol point." % tuple(
            "%.2f" % s["greeks"][k] for k in ("delta", "gamma", "theta", "vega")))
        for r in s["positions"]:
            print("  leg %d: %s %+d (%d lot%s) at %s, now %s: %s%s" % (
                r["leg"], r["contract"], r["qty"], abs(r["lots"]), "" if abs(r["lots"]) == 1 else "s", _p(r["avg_price"]),
                _p(r["mark"]) if r["mark"] is not None else "no quote",
                inr(r["unrealised"]) if r["unrealised"] is not None else "unknown",
                (" (IV %s%%)" % _q(r["iv"])) if r.get("iv") else ""))
            if r.get("warning"):
                print("    WARNING: " + r["warning"])
    t = v["totals"]
    print("All F&O: open P&L %s; delta %.2f, theta %.2f a day; margin blocked %s; cash %s, free %s." % (
        inr(t["unrealised"]), t["greeks"]["delta"], t["greeks"]["theta"], inr(t["margin_blocked"]), inr(t["cash"]),
        inr(t["free_cash"])))
    if v["missing_quotes"]:
        print("No quote for %s: refresh trader/options.json (trader-options --write)." % ", ".join(v["missing_quotes"]))
    print("Greeks from %s. Paper trading. Education and tooling, not financial advice." % v["greeks_from"])
    return 0


def _adjust_order(base, side, cfg, markets, why, **over):
    o = {"symbol": underlying_of(base), "exchange": "NFO", "segment": fno_segment(base),
         "instrument": base.get("instrument") or None, "expiry": str(base.get("expiry")),
         "strike": _f(base.get("strike")) if fno_segment(base) == "OPT" else None,
         "option_type": str(base.get("option_type")).upper() if fno_segment(base) == "OPT" else None,
         "tradingsymbol": None, "instrument_token": None, "side": side, "lots": None,
         "lot_size": lot_of(base), "qty": None, "product": str(base.get("product") or "NRML").upper(),
         "order_type": "LIMIT", "price": None, "validity": "DAY", "leg": None, "tag": "bt-adjust", "why": why}
    o.update(over)
    if o["lots"] is None:
        o["lots"] = abs(int(_f(base.get("qty")) or 0)) // lot_of(base)
    o["qty"] = int(o["lots"]) * lot_of(o)
    q = quote_for(markets, o)
    if not q or not q.get("mark"):
        raise TicketError("no quote for %s in the chain files: refresh trader/options.json first" % contract_words(o))
    o["price"] = tick_adverse(q["mark"], side)
    return o


def cmd_adjust(a):
    cfg = read_json(a.config, "config", required=False) or {}
    paper_only(cfg, "an F&O adjustment ticket")
    now, today = _clock(a)
    pf = load_portfolio(a.portfolio, cfg)
    sid = a.strategy
    st = (pf.get("strategies") or {}).get(sid)
    held = fno_positions(pf, sid)
    if not st or not held:
        have = sorted(k for k, v in (pf.get("strategies") or {}).items() if fno_positions(pf, k))
        raise TicketError("no open strategy %s on the paper account%s" % (sid, (" (open: %s)" % ", ".join(have))
                                                                          if have else ""))
    markets = _markets_arg(a)
    legs = held

    def pick(n):
        if not 1 <= n <= len(legs):
            raise TicketError("%s has legs 1 to %d (ticket.py mtm lists them)" % (sid, len(legs)))
        return legs[n - 1]

    def closing(p):
        return _adjust_order(p, "SELL" if p["qty"] > 0 else "BUY", cfg, markets,
                             "%s: close %s %+d" % (sid, contract_words(p), p["qty"]))
    orders, adj = [], {"strategy_id": sid, "name": st.get("name")}
    if a.close_all:
        orders = [closing(p) for p in legs]
        adj["action"] = "close-all"
    elif a.close_leg:
        orders = [closing(pick(a.close_leg))]
        adj["action"] = "close"
    elif a.roll_leg:
        p = pick(a.roll_leg)
        if fno_segment(p) == "OPT" and a.strike is None and not a.expiry:
            raise TicketError("roll to what? --strike K and/or --expiry YYYY-MM-DD")
        over = {}
        if a.strike is not None:
            over["strike"] = float(a.strike)
        if a.expiry:
            over["expiry"] = a.expiry
        new = _adjust_order(p, "BUY" if p["qty"] > 0 else "SELL", cfg, markets,
                            "%s: roll %s to %s" % (sid, contract_words(p), contract_words(dict(p, **over))), **over)
        orders = [closing(p), new]
        adj.update(action="roll", closing_legs=[1])
    elif a.add_hedge:
        m = re.match(r"^\s*(BUY|SELL)\s+(\d+)\s+(CE|PE|FUT)\s*(\d+(?:\.\d+)?)?\s*(\d{4}-\d{2}-\d{2})?\s*$",
                     a.add_hedge.upper())
        if not m:
            raise TicketError("--add-hedge wants \"BUY 1 PE 22500\" (side, lots, CE/PE/FUT, strike, optional expiry)")
        side, lots, t, k, exp = m.groups()
        base = dict(legs[0], segment="FUT" if t == "FUT" else "OPT", option_type=None if t == "FUT" else t,
                    strike=float(k) if k else None, expiry=exp.lower() if exp else legs[0]["expiry"],
                    instrument=None)
        if t != "FUT" and not k:
            raise TicketError("an option hedge needs a strike")
        orders = [_adjust_order(base, side, cfg, markets, "%s: add a hedge, %s %s %s" % (sid, side, lots, t),
                                lots=int(lots))]
        adj["action"] = "add-hedge"
    else:
        raise TicketError("say what to do: --close-leg N, --roll-leg N --strike K, --add-hedge \"BUY 1 PE 22500\" "
                          "or --close-all")
    for i, o in enumerate(orders, 1):
        o["leg"] = i
        if not o.get("instrument"):
            o["instrument"] = ("OPT" if o["segment"] == "OPT" else "FUT") + ("IDX" if o["symbol"] in INDICES else "STK")
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "status": "proposed", "paper": True, "basket": True, "adjust": adj,
        "note": "An adjustment of %s: a proposal. Nothing is placed until trader-risk passes every leg and you say "
                "yes to this basket ticket." % sid,
        "source": "ticket.py adjust %s on %s" % (adj["action"], today), "orders": orders}
    write_json(a.orders, doc)
    print("Adjustment ticket for %s (%s), %s:" % (sid, st.get("name"), adj["action"].replace("-", " ")))
    for o in orders:
        print("  %s (%s)" % (fno_plain(o), o["why"]))
    print("Wrote %d leg%s to %s%s. Next: trader-risk (check.py), then ticket.py show and the user's yes to the "
          "basket. Nothing was placed." % (len(orders), "" if len(orders) == 1 else "s", a.orders,
                                          (" (it replaced %d earlier proposal%s)" % (replaced, "" if replaced == 1 else "s"))
                                          if replaced else ""))
    return 0


def cmd_settle(a):
    cfg = read_json(a.config, "config", required=False) or {}
    paper_only(cfg, "expiry settlement")
    now, today = _clock(a)
    day = a.date or today
    pf = load_portfolio(a.portfolio, cfg)
    due = [p for p in fno_positions(pf) if str(p["expiry"]) <= day]
    if not due:
        print("Nothing expires on or before %s on the paper account." % day)
        return 0
    sp = load_settlement(a.settlement)
    for t in a.price or []:
        m = re.match(r"^\s*([A-Za-z0-9&\-]+)\s*=\s*(\d+(?:\.\d+)?)\s*$", t)
        if not m:
            raise TicketError("--price wants UNDERLYING=PRICE, like NIFTY=23012.40")
        for p in due:
            if p["underlying"] == m.group(1).upper():
                for seg in ("OPT", "FUT"):
                    sp[seg][(p["underlying"], p["expiry"])] = float(m.group(2))
    missing = sorted({"%s %s" % (p["underlying"], p["expiry"]) for p in due
                      if (p["underlying"], p["expiry"]) not in sp[p["segment"]]})
    if missing:
        raise TicketError("no final settlement price for %s: pass --settlement FILE (NSE's FOSett_prce or F&O "
                          "bhavcopy of the expiry day) or --price NIFTY=23012.40. Nothing was settled"
                          % ", ".join(missing))
    trades_out, out = [], []
    sids = set()
    for p in list(due):
        px = sp[p["segment"]][(p["underlying"], p["expiry"])]
        r = settle_position(pf, p, px, cfg, now, p["expiry"])
        out.append(r)
        sids.add(r["strategy_id"])
    markets = _markets_arg(a)
    for sid in sorted(sids, key=str):
        need, src = strategy_margin(pf, sid, markets, now)
        set_margin(pf, sid, need, now, src)
        if not fno_positions(pf, sid):
            pf["strategies"][sid].update(status="closed", closed_at=ist_iso(now))
    save_portfolio(a.portfolio, pf)
    for r in out:
        trade = {"at": ist_iso(now), "date": day, "symbol": r["key"], "exchange": "NFO", "side": "SETTLE",
                 "qty": r["qty"], "price": r["settle"], "value": round_paise((r["intrinsic"] if r["intrinsic"] is not None
                                                                              else r["settle"]) * abs(r["qty"])),
                 "charges": r["charges"], "charges_detail": r["charges_detail"], "leg": r["outcome"],
                 "realised_pnl": r["realised"], "strategy_id": r["strategy_id"], "paper": True}
        log_trade(a.trades, trade)
    print("PAPER expiry settlement for %s:" % day)
    for r in out:
        print("  %s %+d: %s at %s%s%s; realised %s." % (
            r["contract"], r["qty"], r["outcome"], _p(r["settle"]),
            (" (intrinsic %s)" % _p(r["intrinsic"])) if r["intrinsic"] else "",
            (", STT on exercise %s" % inr(r["charges"])) if r["charges"] else "", inr(r["realised"])))
        if r["note"]:
            print("    WARNING: " + r["note"])
    for sid in sorted(sids, key=str):
        st = pf["strategies"][sid]
        print("%s %s: realised %s after all charges (%s); %s." % (
            sid, st.get("name"), inr(st.get("realised") or 0), inr(st.get("charges") or 0),
            "closed, margin released" if st.get("status") == "closed" else "still open, margin %s" % inr(st.get("margin"))))
    print("Cash %s, margin blocked %s. Paper trading. Education and tooling, not financial advice." % (
        inr(pf["cash"]), inr(pf["margin_blocked"])))
    return 0


def record_shadow(a, cfg, single, rec, now):
    """Shadow mode: a paper twin of a live order just sent (the live order is not touched)."""
    twin = shadow_twin(single, cfg, getattr(a, "prices", None) or "trader/prices.json",
                       load_markets(list(getattr(a, "options", None) or []) or ["trader/options.json"]), now)
    path = getattr(a, "shadow", None) or "trader/state/shadow.json"
    doc = read_json(path, "shadow ledger", required=False) or {"orders": []}
    doc.setdefault("orders", []).append({
        "at": ist_iso(now), "date": rec.get("date"), "broker": rec.get("broker"), "broker_order_id": rec.get("broker_order_id"),
        "tag": rec.get("tag"), "fingerprint": rec.get("fingerprint"), "leg": rec.get("leg"),
        "symbol": single.get("symbol"), "side": str(single.get("side")).upper(), "qty": single.get("qty"),
        "order_type": single.get("order_type"), "limit": single.get("price"), "plain": rec.get("plain"),
        "paper": twin})
    write_json(path, doc)
    return twin


def cmd_shadow_report(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    doc = read_json(a.shadow, "shadow ledger", required=False)
    if not doc or not doc.get("orders"):
        print("No shadowed orders yet: set \"shadow\": true in the config; every live order ticket.py place sends "
              "then gets a paper twin in %s." % a.shadow)
        return 0
    book = {}
    if a.live_orders:
        rows = read_json(a.live_orders, "live order book")
        rows = rows.get("orders", rows.get("data", rows)) if isinstance(rows, dict) else rows
        book = {str(o.get("broker_order_id")): o for o in rows or []}
    elif a.live:
        b = which_broker(a, cfg)
        book = {str(o["broker_order_id"]): o for o in _broker_call(get_adapter(b, cfg).orders)}
    rep = shadow_report(doc, book)
    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0
    print("Shadow report: %d live order%s with a paper twin, %d compared with the live fill." % (
        len(rep["orders"]), "" if len(rep["orders"]) == 1 else "s", rep["compared"]))
    for r in rep["orders"]:
        tw = r.get("paper") or {}
        line = "  %s %s: paper %s" % (str(r.get("at", ""))[:16], r.get("plain") or r.get("symbol"),
                                     ("filled at %s, charges %s" % (_p(tw["price"]), inr(tw["charges"])))
                                     if tw.get("filled") else "would not fill (%s)" % tw.get("why"))
        if r.get("live_price") is not None:
            line += "; live filled at %s: %s %s a unit (%s in all, %s bps)" % (
                _p(r["live_price"]), "better by" if r["diff_per_unit"] >= 0 else "worse by", _p(abs(r["diff_per_unit"])),
                inr(r["diff_rupees"]), _q(r["diff_bps"]))
        else:
            line += "; live: %s" % r.get("live_status")
        print(line)
    if rep["compared"]:
        print("In all the live fills were %s %s than paper's (positive: live was better). Read only: nothing was sent."
              % ("better" if rep["total_diff"] >= 0 else "worse", inr(abs(rep["total_diff"]))))
    return 0

# ---------------------------------------------------------------- end of paper F&O


# ---------------------------------------------------------------- live brokers (adapters/, read side and the gated order)

ADAPTER_KW = {}          # tests point the adapters at a local stub: {"hosts": {...}, "env": {...}}
IP_OPENER = None         # tests replace the public-IP lookup


def adapters():
    """The adapters/ folder next to this script, loaded by path (it travels with the skill)."""
    import importlib.util
    name = "bt_trader_broker_adapters"
    if name in sys.modules:
        return sys.modules[name]
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "__init__.py"), submodule_search_locations=[d])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_adapter(name, cfg=None, sandbox=False):
    kw = dict(ADAPTER_KW.get(broker_name(name), ADAPTER_KW.get("*", {})))
    return adapters().make(broker_name(name), cfg=cfg or {}, sandbox=sandbox, **kw)


def which_broker(a, cfg):
    b = broker_name(getattr(a, "broker", None) or merged_config(cfg).get("broker"))
    if b == "paper":
        raise TicketError("which broker? Say --broker %s (the config broker is paper; reading a real account is "
                          "fine, it places nothing)" % "|".join(LIVE_BROKERS))
    if b not in LIVE_BROKERS:
        raise TicketError("the brokers are %s, not %r" % (", ".join(LIVE_BROKERS), b))
    return b


def last_cutoff(now, hhmm):
    h, m = [int(x) for x in hhmm.split(":")]
    cut = datetime.datetime.combine(now.date(), datetime.time(h, m), IST)
    return cut if now >= cut else cut - datetime.timedelta(days=1)


def token_freshness(ad, now, profile=None):
    """(ok, words). Uses the broker's daily cutoff and BT_<BROKER>_TOKEN_DATE (set by ticket.py login)."""
    if not ad.token():
        return False, "no access token: run python3 ticket.py login %s and paste the export lines" % ad.name
    valid = (profile or {}).get("token_valid_until")
    if valid:
        until = parse_time(valid)
        if until and until <= now:
            return False, "the token expired at %s" % until.strftime("%Y-%m-%d %H:%M")
        if until:
            return True, "the token is good until %s IST (the broker says so)" % until.strftime("%Y-%m-%d %H:%M")
    made = parse_time(ad.env_get("token_date"))
    if ad.token_cutoff:
        cut = last_cutoff(now, ad.token_cutoff)
        if made is None:
            return True, ("token age unknown (%s is not set); %s tokens stop at %s IST every day"
                          % (ad.env_names["token_date"], ad.title, ad.token_cutoff))
        if made < cut:
            return False, ("the token is from %s, before today's %s IST cutoff: log in again"
                           % (made.strftime("%Y-%m-%d %H:%M"), ad.token_cutoff))
        return True, "the token is from %s; it works until %s IST" % (made.strftime("%Y-%m-%d %H:%M"),
                                                                       ad.token_cutoff)
    if made and (now - made).total_seconds() > 24 * 3600:
        return False, "the token is from %s, more than 24 hours ago: log in again" % made.strftime("%Y-%m-%d %H:%M")
    return True, "the token works for 24 hours from login" + (" (made %s)" % made.strftime("%H:%M") if made else "")


def _broker_call(fn, *args):
    base = adapters().base
    try:
        return fn(*args)
    except base.BrokerError as e:
        raise TicketError(e.message)


def _print_json(broker, now, data, extra=None):
    out = {"broker": broker, "as_of": ist_iso(now), "read_only": True, "data": data}
    out.update(extra or {})
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_accounts(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    prof = _broker_call(ad.profile)
    funds = _broker_call(ad.funds)
    fresh = token_freshness(ad, now, prof)
    if a.json:
        _print_json(b, now, {"profile": prof, "funds": funds, "capabilities": ad.caps(),
                             "token": {"ok": fresh[0], "words": fresh[1]}})
        return 0
    print("%s account %s (read only)" % (ad.title, prof.get("user_id") or "?"))
    print("Available %s, used %s%s." % (inr(funds["available"] or 0), inr(funds["used"] or 0),
                                         (", collateral %s" % inr(funds["collateral"])) if funds.get("collateral")
                                         else ""))
    print("Token: %s." % fresh[1])
    print("Can do here: %s." % ", ".join(k.replace("_", " ") for k, v in sorted(ad.caps().items()) if v))
    print("Plans: %s" % ad.plans)
    return 0


def cmd_funds(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    f = _broker_call(ad.funds)
    if a.json:
        _print_json(b, now, f)
        return 0
    print("%s funds at %s IST: available %s, used %s%s." % (
        ad.title, now.strftime("%H:%M"), inr(f["available"] or 0), inr(f["used"] or 0),
        (", collateral %s" % inr(f["collateral"])) if f.get("collateral") else ""))
    return 0


def cmd_holdings(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    rows = _broker_call(ad.holdings)
    if a.json:
        _print_json(b, now, rows)
        return 0
    print("%s holdings (delivery), %d stock%s:" % (ad.title, len(rows), "" if len(rows) == 1 else "s"))
    for h in rows:
        print("  %s %d shares at %s average%s%s" % (h["symbol"], h["qty"], _p(h["avg_price"] or 0),
                                                   (", last %s" % _p(h["last_price"])) if h.get("last_price") else "",
                                                   (", P&L %s" % inr(h["pnl"])) if h.get("pnl") is not None else ""))
    print("Read only: nothing was sent to %s." % ad.title)
    return 0


def cmd_positions(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    rows = _broker_call(ad.positions)
    if a.json:
        _print_json(b, now, rows)
        return 0
    open_ = [p for p in rows if p["qty"]]
    print("%s positions: %d open, %d closed today." % (ad.title, len(open_), len(rows) - len(open_)))
    for p in rows:
        print("  %s %s %d (%s) at %s, P&L %s" % (p["symbol"], p["exchange"], p["qty"], p["product"],
                                                 _p(p["avg_price"] or 0), inr(p["pnl"] or 0)))
    print("Read only: nothing was sent to %s." % ad.title)
    return 0


def cmd_orders(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    rows = _broker_call(ad.orders)
    trades = _broker_call(ad.trades)
    if a.json:
        _print_json(b, now, {"orders": rows, "trades": trades})
        return 0
    print("%s orders today: %d; trades: %d." % (ad.title, len(rows), len(trades)))
    for o in rows:
        print("  %s %s %d %s %s, %s%s (%s)%s" % (
            o["broker_order_id"], o["side"], o["qty"], o["symbol"], o["order_type"], o["status"],
            (" %d filled at %s" % (o["filled_qty"], _p(o["avg_price"]))) if o["filled_qty"] else "",
            o["product"], (" tag " + o["tag"]) if o.get("tag") else ""))
    print("Read only: nothing was sent to %s." % ad.title)
    return 0


ASK_CODE = None          # tests replace how the user's one-time code is asked for (default: getpass, not echoed)


def ask_code(prompt):
    if ASK_CODE is not None:
        return ASK_CODE(prompt)
    import getpass
    return getpass.getpass(prompt)


def login_totp(a, ad, b):
    """Angel One: client code + PIN from your shell, a 6-digit TOTP code from BT_ANGEL_TOTP_SECRET or typed by you."""
    code = None
    try:
        code = ad.totp_code()
    except adapters().base.BrokerError as e:
        raise TicketError(e.message)
    if code:
        print("Using %s to make the 6-digit code. Anyone who can read your shell environment can log in as you with "
              "it and your PIN; typing the code from your authenticator app instead is safer (unset %s)." % (
                  ad.env_names["totp_secret"], ad.env_names["totp_secret"]), file=sys.stderr)
    else:
        code = ask_code("6-digit code from your authenticator app for %s (not shown): " % ad.title)
    try:
        return ad.login_direct(code)
    except adapters().base.BrokerError as e:
        raise TicketError(e.message)


def cmd_login(a):
    cfg = read_json(a.config, "config", required=False) or {}
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    au = adapters().auth
    if getattr(ad, "login_kind", None) == "totp":
        token = login_totp(a, ad, b)
    else:
        state = au.new_state()
        try:
            url, want, state_param = ad.login_start(state)
            port, path = au.parse_redirect(ad.redirect())
        except adapters().base.BrokerError as e:
            raise TicketError(e.message)

        def ready(local):
            print("Waiting for %s's login to come back to %s (one time, %d seconds)." % (ad.title, local, a.timeout),
                  file=sys.stderr)
            if a.no_browser:
                print("Open this in your browser and log in:\n  %s" % url, file=sys.stderr)
            else:
                import webbrowser
                print("Opening %s's login page in your browser (2FA there)." % ad.title, file=sys.stderr)
                if not webbrowser.open(url):
                    print("Could not open a browser. Open this yourself:\n  %s" % url, file=sys.stderr)

        try:
            params = au.wait_for_callback(port, path, want, state if state_param else None, state_param or "state",
                                          timeout=a.timeout, on_ready=ready)
            token = ad.login_finish(params)
        except adapters().base.BrokerError as e:
            raise TicketError(e.message)
    now, _ = _clock(a)
    var = ad.env_names["access_token"]
    print("export %s=%s" % (var, shell_quote(token)))
    print("export %s=%s" % (ad.env_names["token_date"], ist_iso(now)))
    ends = {"00:00": "at midnight IST tonight"}.get(ad.token_cutoff, ("at %s IST tomorrow" % ad.token_cutoff)
                                                   if ad.token_cutoff else "24 hours from now")
    print("Logged in to %s. The lines above are for your shell only (eval \"$(python3 ticket.py login %s)\"): the "
          "token is not saved anywhere by this tool. It stops working %s." % (ad.title, b, ends), file=sys.stderr)
    return 0


def shell_quote(s):
    import shlex
    return shlex.quote(s)


def cmd_preflight(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    au, base = adapters().auth, adapters().base
    checks = []

    def add(name, ok, words):
        checks.append({"check": name, "ok": bool(ok), "words": words})

    creds = ad.credentials_status()
    tok = ad.token()
    add("credentials", bool(tok), "%s is %s" % (ad.env_names["access_token"], "set" if tok else "not set"))
    prof = None
    try:
        prof = ad.profile() if tok else None
        add("login", bool(prof), "a harmless call (your profile) worked: account %s" % prof["user_id"] if prof
            else "skipped: no token")
    except base.BrokerError as e:
        add("login", False, "the profile call failed: %s" % e.message)
    ok, words = token_freshness(ad, now, prof)
    add("token", ok, words)
    ip, notes = au.public_ip(opener=IP_OPENER) if not a.ip else (a.ip, [("--ip", a.ip)])
    add("public_ip", bool(ip), ("your public IP is %s (%s)" % (ip, ", ".join(n[0].split("//")[-1] for n in notes
                                                                              if n[1] == ip)))
        if ip else "two IP echo services did not agree on your public IP: %s" % "; ".join(
            "%s: %s" % n for n in notes))
    mine = au.static_ips(cfg, b)
    theirs = None
    if prof:
        try:
            theirs = ad.registered_ips()
        except base.BrokerError as e:
            theirs = None
            add("broker_ips", False, "could not read the IPs %s has on record: %s" % (ad.title, e.message))
    allowed = set(theirs or []) or set(mine)
    if theirs and mine and not set(mine) & set(theirs):
        add("static_ip", False, "your config static_ip %s is not what %s has on record (%s)" % (
            ", ".join(mine), ad.title, ", ".join(theirs)))
    elif not allowed:
        add("static_ip", False, "no static IP to compare with: set \"static_ip\" in trader/config.json to the IP you "
                                "registered with %s" % ad.title)
    else:
        src = "%s has on record" % ad.title if theirs else "your config static_ip"
        add("static_ip", bool(ip) and ip in allowed,
            ("this machine's IP %s is the static IP %s" % (ip, src)) if ip in allowed else
            ("this machine's IP %s is not the static IP %s (%s): orders would be refused; reading still works"
             % (ip, src, ", ".join(sorted(allowed)))))
    passed = all(c["ok"] for c in checks)
    rec = {"at": ist_iso(now), "date": today, "broker": b, "passed": passed, "public_ip": ip,
           "static_ip": mine, "registered_ips": theirs, "token_hash": base.token_hash(tok), "checks": checks,
           "credentials": creds}
    doc = read_json(a.preflight, "preflight", required=False) or {}
    doc[b] = rec
    write_json(a.preflight, doc)
    if a.json:
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0 if passed else 1
    print("Preflight for %s at %s IST:" % (ad.title, now.strftime("%Y-%m-%d %H:%M")))
    for c in checks:
        print("  %s %s: %s" % ("PASS" if c["ok"] else "FAIL", c["check"], c["words"]))
    print("%s. %s" % ("Ready for live orders from this machine today (each still needs trader-risk PASS and your yes)"
                      if passed else "Not ready for live orders: fix the FAIL lines and run preflight again. "
                                     "Reading holdings and positions works anyway",
                      au.SEBI_WORDS % _q(adapters().base.order_rate(cfg))))
    return 0 if passed else 1


LINKED_LEGS = ("oco", "bracket")
LINKED_WORDS = {"oco": "the stop and the target as ONE linked order", "bracket": "the entry with its stop and target "
                "as ONE order"}


def _gate_refused(e, what="Not sent"):
    print("%s. Closed gates:" % what, file=sys.stderr)
    for g, why in e.failed:
        print("  %s: %s" % (g, why), file=sys.stderr)
    return 1


def _entry_fill(ad, state, fp, today, b, index, leg):
    """The live entry of this ticket, filled at the broker (its order-book row), or TicketError."""
    entry = [r for r in records(state, fp, today, "placed") if r.get("live") and r.get("broker") == b
             and (r.get("leg") or "entry") == "entry"]
    if not entry:
        raise TicketError("send the entry first: ticket.py place --index %d" % index)
    book = {o["broker_order_id"]: o for o in _broker_call(ad.orders)}
    got = book.get(str(entry[-1].get("broker_order_id")))
    if not got or got["status"] != "filled":
        raise TicketError("the entry %s is %s at %s, not filled: the %s goes out after the fill" % (
            entry[-1].get("broker_order_id"), (got or {}).get("status", "not in the order book"), ad.title, leg))
    return got, book


def cmd_place(a):
    orders, cfg, risk, state, today, now = _load(a)
    mode, b = mode_of(cfg)
    if mode != "live":
        raise TicketError("place sends a LIVE order and the config is paper; on paper, confirm places the order. "
                          "Nothing was sent")
    base = adapters().base
    ticket = _pick(orders, a.index)
    leg = a.leg or "entry"
    fp = fingerprint(ticket)
    ad = get_adapter(b, cfg)
    if leg in LINKED_LEGS and ad.linked_leg() != leg:
        other = ad.linked_leg()
        raise TicketError("%s has no %s leg here (%s)%s. Nothing was sent" % (
            ad.title, leg, ad.linked_words, (": use --leg %s" % other) if other else
            ": send the entry, then the stop or the target leg by leg"))
    pre = read_json(a.preflight, "preflight", required=False) or {}
    try:
        single = base.leg_order(ticket, leg)
        gp = base.open_gate(b, cfg, ticket, risk, state, pre, ad.token(), today, now, leg=leg)
    except base.GateClosed as e:
        return _gate_refused(e)
    except base.BrokerError as e:
        raise TicketError(e.message)
    approved = [r for r in records(state, fp, today, "approved") if r.get("broker") == b][-1]
    if approved.get("actor", "user") != "user":
        raise TicketError("this yes was recorded by %s, not typed by you: a live order needs your own yes. Nothing "
                          "was sent" % approved.get("actor"))
    last_price = getattr(a, "last_price", None)
    if leg in ("stop", "target", "oco"):
        got, book = _entry_fill(ad, state, fp, today, b, a.index, leg)
        if leg == "oco" and last_price is None:
            last_price = got.get("avg_price") or None
        if leg != "oco":
            other = "target" if leg == "stop" else "stop"
            for r in records(state, fp, today, ("leg_sent", "unknown")):
                if r.get("leg") == other and r.get("broker") == b:
                    o = book.get(str(r.get("broker_order_id")))
                    if not o or o["status"] == "open":
                        hint = (" or send both as one linked order next time: --leg %s" % ad.linked_leg()
                                if ad.linked_leg() == "oco" else "")
                        raise TicketError("the %s (%s) is still working at %s. Leg by leg, only one exit leg works at the "
                                          "broker at a time: cancel it first with ticket.py cancel --index %d --leg "
                                          "%s%s" % (other, r.get("broker_order_id"), ad.title, a.index, other, hint))
    inst = None
    try:
        inst = ad.resolve(single["symbol"], single.get("exchange") or "NSE")
    except base.BrokerError:
        pass
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "leg": leg, "broker": b,
           "live": True, "paper": False, "symbol": approved["symbol"], "side": str(single.get("side")).upper(),
           "qty": single.get("qty"), "product": approved.get("product"), "kind": approved.get("kind"),
           "max_loss": approved.get("max_loss"), "stop": approved.get("stop"), "target": approved.get("target"),
           "has_legs": approved.get("has_legs"), "yes_text": approved.get("yes_text"), "order": single,
           "tag": gp.tag, "plain": plain(base.leg_order(ticket, "entry") if leg == "bracket" else
                                         single if leg != "oco" else base.leg_order(ticket, "stop"), cfg)}
    if leg in LINKED_LEGS:
        rec.update(linked=ad.linked_style, ticket=neutral(ticket),
                   broker_symbol=(inst or {}).get("broker_symbol"), last_price=last_price)
    try:
        if leg in LINKED_LEGS:
            oid = ad.place_linked(single, gp, last_price)
        else:
            oid = ad.place_order(single, gp)
    except base.BrokerDown as e:
        rec.update(status="unknown", error=e.message)
        state.setdefault("orders", []).append(rec)
        write_json(a.state, state)
        raise TicketError("%s. It may or may not have reached %s: run ticket.py reconcile (or check ticket.py orders "
                          "for tag %s) before anything else. Not retried." % (e.message, ad.title, gp.tag))
    except base.BrokerError as e:
        rec.update(status="send_failed", error=e.message)
        state.setdefault("orders", []).append(rec)
        write_json(a.state, state)
        raise TicketError("%s refused it: %s" % (ad.title, e.message))
    rec.update(status={"entry": "placed", "bracket": "placed", "oco": "linked_sent"}.get(leg, "leg_sent"),
               broker_order_id=oid)
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    if merged_config(cfg).get("shadow") is True and leg in ("entry", "bracket"):
        try:
            twin = record_shadow(a, cfg, base.leg_order(ticket, "entry"), rec, now)
            print("Shadow: paper twin recorded in %s (%s)." % (a.shadow, ("paper would fill at %s" % _p(twin["price"]))
                                                               if twin.get("filled") else twin.get("why")))
        except Exception as e:          # the live order is out already: the twin never changes that
            print("Shadow: could not record the paper twin (%s); the live order is unaffected." % e, file=sys.stderr)
    if leg in LINKED_LEGS:
        st, tg = single["stop"], single["target"]
        what = (plain(base.leg_order(ticket, "entry"), cfg) if leg == "bracket" else
                "exit %s %s %s" % (single["side"], single["qty"], single["symbol"]))
        print("SENT to %s (real money): %s (%s): %s; stop trigger %s, target %s. %s id %s." % (
            ad.title, LINKED_WORDS[leg], "after the fill" if leg == "oco" else "with the entry", what,
            _p(st.get("trigger_price")), _p(tg.get("price")), ad.title, oid))
        print("When one exit fills, %s cancels the other. Cancel it: ticket.py cancel --index %d --leg %s. A new stop "
              "or target: a new ticket, trader-risk, your yes, then ticket.py modify-exits." % (ad.title, a.index, leg))
        return 0
    print("SENT to %s (real money): %s. %s order id %s, tag %s." % (ad.title, plain(single, cfg), ad.title, oid,
                                                                   gp.tag))
    if leg == "entry" and approved.get("has_legs"):
        nl = ad.linked_leg()
        if nl == "oco":
            print("The stop and the target are NOT at the broker yet. When the entry fills, send both as one linked "
                  "order: ticket.py place --index %d --leg oco (or leg by leg: --leg stop)" % a.index)
        else:
            print("The stop and the target are NOT at the broker yet. When the entry fills, send the stop: "
                  "ticket.py place --index %d --leg stop%s" % (a.index, (" (next time, %s can carry them with the "
                                                                          "entry: --leg bracket)" % ad.title)
                                                                         if nl == "bracket" else ""))
    return 0


def _linked_record(state, b, linked_id):
    """This tool's latest record of a linked order (any day), unless a cancel was sent for it since."""
    hit = None
    for r in state.get("orders", []):
        if r.get("broker") == b and r.get("live") and str(r.get("broker_order_id")) == str(linked_id):
            if r.get("status") == "cancel_sent":
                hit = None
            elif r.get("leg") in LINKED_LEGS and r.get("status") in ("placed", "linked_sent") and not r.get("modify"):
                hit = r
    return hit


def cmd_cancel(a):
    orders, cfg, risk, state, today, now = _load(a)
    mode, b = mode_of(cfg)
    if mode != "live":
        raise TicketError("cancel is for a live order; the config is paper")
    base = adapters().base
    leg = a.leg or "entry"
    ad = get_adapter(b, cfg)
    if getattr(a, "linked_id", None):
        rec = _linked_record(state, b, a.linked_id)
        sent = [rec] if rec else []
        fp, leg = (rec or {}).get("fingerprint"), (rec or {}).get("leg") or "oco"
    else:
        if a.index is None:
            raise TicketError("say which order: --index N --leg LEG, or --linked-id ID")
        ticket = _pick(orders, a.index)
        fp = fingerprint(ticket)
        sent = [r for r in records(state, fp, today, ("placed", "leg_sent", "linked_sent")) if r.get("live")
                and r.get("broker") == b and (r.get("leg") or "entry") == leg]
    pre = read_json(a.preflight, "preflight", required=False) or {}
    try:
        gp = base.open_cancel_gate(b, cfg, (sent[-1] if sent else {}).get("broker_order_id"),
                                   sent[-1] if sent else None, state, pre, ad.token(), today, now)
        if leg in LINKED_LEGS:
            ad.cancel_linked(sent[-1]["broker_order_id"], gp, sent[-1])
        else:
            ad.cancel_order(sent[-1]["broker_order_id"], gp)
    except base.GateClosed as e:
        return _gate_refused(e, "Not cancelled")
    except base.BrokerError as e:
        raise TicketError(e.message)
    state.setdefault("orders", []).append({"at": ist_iso(now), "date": today, "index": getattr(a, "index", None),
                                           "fingerprint": fp, "leg": leg, "broker": b, "live": True,
                                           "status": "cancel_sent", "broker_order_id": sent[-1]["broker_order_id"]})
    write_json(a.state, state)
    print("Cancel sent to %s for %s %s (the %s%s)." % (
        ad.title, "linked order" if leg in LINKED_LEGS else "order", sent[-1]["broker_order_id"],
        {"oco": "linked stop and target", "bracket": "bracket: the entry if open, and its stop and target"}.get(
            leg, leg), (" of order %d" % a.index) if getattr(a, "index", None) else ""))
    return 0


def cmd_modify_exits(a):
    """A new stop and target for a working linked order: the new ticket (--index) needs its own trader-risk PASS
    and your typed yes; the linked order keeps its id."""
    orders, cfg, risk, state, today, now = _load(a)
    mode, b = mode_of(cfg)
    if mode != "live":
        raise TicketError("modify-exits changes a LIVE linked order and the config is paper. Nothing was sent")
    base = adapters().base
    ad = get_adapter(b, cfg)
    if not ad.linked_leg():
        raise TicketError("%s: %s. Nothing was sent" % (ad.title, ad.linked_words))
    new = _pick(orders, a.index)
    fp = fingerprint(new)
    rec = _linked_record(state, b, a.linked_id)
    pre = read_json(a.preflight, "preflight", required=False) or {}
    try:
        gp = base.open_modify_gate(b, cfg, new, rec, risk, state, pre, ad.token(), today, now)
        exits = base.leg_order(new, "oco")
    except base.GateClosed as e:
        return _gate_refused(e)
    except base.BrokerError as e:
        raise TicketError(e.message)
    approved = [r for r in records(state, fp, today, "approved") if r.get("broker") == b][-1]
    last_price = a.last_price
    if last_price is None and ad.linked_style == "exit_oco":
        try:
            inst = ad.resolve(exits["symbol"], exits.get("exchange") or "NSE")
            last_price = ad.ltp([inst]).get(inst["symbol"])
        except base.BrokerError as e:
            raise TicketError("%s needs the last price for the change (%s): pass --last-price P. Nothing was sent"
                              % (ad.title, e.message))
    out = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "leg": rec.get("leg"),
           "broker": b, "live": True, "paper": False, "broker_order_id": a.linked_id, "modify": True,
           "of_fingerprint": rec.get("fingerprint"), "order": exits, "yes_text": approved.get("yes_text"),
           "symbol": approved.get("symbol"), "last_price": last_price}
    try:
        ad.modify_linked(a.linked_id, exits, gp, record=rec, last_price=last_price)
    except base.BrokerDown as e:
        out.update(status="modify_unknown", error=e.message)
        state.setdefault("orders", []).append(out)
        write_json(a.state, state)
        raise TicketError("%s. The change may or may not have reached %s: run ticket.py reconcile. Not retried."
                          % (e.message, ad.title))
    except base.BrokerError as e:
        out.update(status="modify_failed", error=e.message)
        state.setdefault("orders", []).append(out)
        write_json(a.state, state)
        raise TicketError("%s refused the change: %s" % (ad.title, e.message))
    out["status"] = "modify_sent"
    state.setdefault("orders", []).append(out)
    write_json(a.state, state)
    print("CHANGE SENT to %s (real money): linked order %s now has the stop at %s and the target %s (ticket %d, your "
          "yes \"%s\")." % (ad.title, a.linked_id, _p(exits["stop"].get("trigger_price")),
                            _p(exits["target"].get("price")), a.index, approved.get("yes_text")))
    return 0


def _near(x, y):
    return x is not None and y is not None and abs(float(x) - float(y)) < 0.005


def reconcile_rows(ours, book, cancelled, changes):
    """Match this tool's linked records to the broker's linked book. -> (rows, found) where found maps a record
    sent with an unknown outcome to the broker's id. Pure, for tests."""
    by_id = {str(x["linked_id"]): x for x in book}
    used, rows, found = set(), [], {}
    for r in ours:
        lid = str(r.get("broker_order_id") or "")
        o = r.get("order") or {}
        want_stop = (o.get("stop") or {}).get("trigger_price")
        want_target = (o.get("target") or {}).get("price")
        ch = changes.get(lid)
        if ch:
            want_stop = ((ch.get("order") or {}).get("stop") or {}).get("trigger_price")
            want_target = ((ch.get("order") or {}).get("target") or {}).get("price")
        x = by_id.get(lid) if lid else None
        if x is None and r.get("status") == "unknown":
            entry_side = o.get("side") if r.get("leg") == "bracket" else (
                "BUY" if str(o.get("side")).upper() == "SELL" else "SELL")
            cands = [y for y in book if str(y["linked_id"]) not in used and y.get("qty") == o.get("qty")
                     and str(y.get("side")).upper() == str(entry_side).upper()
                     and ((r.get("tag") and y.get("tag") == r.get("tag")) or
                          (_near(y.get("stop_trigger"), want_stop) and _near(y.get("target_price"), want_target)))]
            if len(cands) == 1:
                x = cands[0]
                found[id(r)] = str(x["linked_id"])
        row = {"linked_id": lid or (x or {}).get("linked_id"), "leg": r.get("leg"), "symbol": r.get("symbol"),
               "fingerprint": r.get("fingerprint"), "date": r.get("date"), "ours": r.get("status"),
               "stop": want_stop, "target": want_target}
        if lid in cancelled:
            row.update(broker=(x or {}).get("status"), verdict="cancelled by this tool")
        elif x is None:
            row.update(broker=None, verdict="not in the broker's book: unknown send that never arrived, or completed, "
                                            "expired or deleted outside this tool" if r.get("status") == "unknown"
                       else "not in the broker's book (completed, expired or deleted outside this tool)")
        else:
            used.add(str(x["linked_id"]))
            same = _near(x.get("stop_trigger"), want_stop) and _near(x.get("target_price"), want_target)
            row.update(linked_id=str(x["linked_id"]), broker=x.get("status"), broker_stop=x.get("stop_trigger"),
                       broker_target=x.get("target_price"),
                       verdict=("found at the broker (the send's outcome was unknown)" if id(r) in found else
                                "matches" if same else "prices differ from this tool's record: changed outside it?"))
        rows.append(row)
    extra = [{"linked_id": str(x["linked_id"]), "symbol": x.get("symbol"), "broker": x.get("status"),
              "verdict": "not made by this tool: left alone"} for x in book if str(x["linked_id"]) not in used]
    return rows, extra, found


def cmd_reconcile(a):
    """After a restart: read the broker's linked book (GTT or super orders) and match it to this tool's records.
    Read only at the broker; it may add records here for sends whose outcome was unknown."""
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    state = load_state(a.state)
    recs = state.get("orders", [])
    cancelled = {str(r.get("broker_order_id")) for r in recs if r.get("broker") == b and r.get("status") == "cancel_sent"}
    changes = {}
    for r in recs:
        if r.get("broker") == b and r.get("status") == "modify_sent":
            changes[str(r.get("broker_order_id"))] = r
    since = (now - datetime.timedelta(days=a.days)).date().isoformat()
    ours = [r for r in recs if r.get("broker") == b and r.get("live") and r.get("leg") in LINKED_LEGS and not
            r.get("modify") and r.get("status") in ("placed", "linked_sent", "unknown") and (r.get("date") or "") >= since]
    book = []
    if ad.linked_leg():
        book = _broker_call(ad.linked_book)
    rows, extra, found = reconcile_rows(ours, book, cancelled, changes)
    added = 0
    for r in ours:
        if id(r) in found:
            new = dict(r, status="placed" if r.get("leg") == "bracket" else "linked_sent", broker_order_id=found[id(r)],
                       reconciled=ist_iso(now))
            new.pop("error", None)
            recs.append(new)
            added += 1
    state.setdefault("reconciled", {})[b] = {"at": ist_iso(now), "rows": rows, "not_ours": len(extra)}
    write_json(a.state, state)
    if a.json:
        _print_json(b, now, {"rows": rows, "not_ours": extra, "records_added": added, "native_oco": bool(
            ad.linked_leg())})
        return 0
    if not ad.linked_leg():
        print("%s: %s. Nothing to reconcile there; leg-by-leg exits show in ticket.py orders." % (ad.title,
                                                                                               ad.linked_words))
        return 0
    print("%s linked orders at %s IST: %d of this tool's (last %d days), %d at the broker." % (
        ad.title, now.strftime("%Y-%m-%d %H:%M"), len(rows), a.days, len(book)))
    for r in rows:
        print("  %s %s %s: here %s, at %s %s (stop %s, target %s): %s" % (
            r.get("linked_id") or "(no id)", r.get("leg"), r.get("symbol"), r.get("ours"), ad.title,
            r.get("broker") or "-", _p(r["stop"]) if r.get("stop") is not None else "-",
            _p(r["target"]) if r.get("target") is not None else "-", r["verdict"]))
    for x in extra:
        print("  %s %s at %s %s: %s" % (x["linked_id"], x.get("symbol"), ad.title, x.get("broker"), x["verdict"]))
    if added:
        print("Recorded %d send%s whose outcome was unknown, now found at %s." % (added, "" if added == 1 else "s",
                                                                               ad.title))
    print("Read only at %s: nothing was sent, changed or cancelled." % ad.title)
    return 0


# ---------------------------------------------------------------- the broker's basket margin, read only

MARGIN_CACHE = "trader/state/broker_margin.json"


def margin_key(rows):
    """One basket, broker-neutral: rows of (side, underlying, expiry, strike, CE/PE/FUT, qty). The same function in
    trader-strategy and trader-risk, so each finds the broker's figure for its own legs."""
    parts = []
    for side, und, exp, strike, kind, qty in rows:
        kind = str(kind or "").upper()
        k = "-" if kind == "FUT" or strike in (None, "") else "%g" % float(strike)
        parts.append("%s|%s|%s|%s|%s|%d" % (str(side).upper(), str(und).upper(), str(exp)[:10], k, kind,
                                            int(round(abs(float(qty or 0))))))
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def margin_rows(orders):
    return [(o.get("side"), underlying_of(o), o.get("expiry"), o.get("strike"),
             "FUT" if fno_segment(o) == "FUT" else o.get("option_type"), o.get("qty")) for o in orders]


def adapter_order(o):
    """A basket leg (trader-strategy's shape) -> the adapters' neutral order (a canonical NFO symbol)."""
    base = adapters().base
    if not is_fno(o):
        return dict(o)
    fut = fno_segment(o) == "FUT"
    sym = base.canonical_symbol("FUT" if fut else "OPT", underlying_of(o), str(o.get("expiry"))[:10],
                                None if fut else o.get("strike"), None if fut else str(o.get("option_type")).upper())
    return {"symbol": sym, "exchange": "NFO", "product": str(o.get("product") or "NRML").upper(),
            "side": str(o.get("side")).upper(), "qty": int(_f(o.get("qty")) or 0),
            "order_type": str(o.get("order_type") or "LIMIT").upper(), "price": o.get("price")}


def cmd_margin_check(a):
    """The broker's basket margin for these F&O legs next to our SPAN-style estimate. Read only."""
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    src = a.legs_json or a.orders
    doc = read_json(src, "legs")
    legs = doc.get("orders", doc) if isinstance(doc, dict) else doc
    legs = [o for o in (legs or []) if is_fno(o)]
    if not legs:
        raise TicketError("no F&O legs in %s: the broker's basket margin is for an F&O basket" % src)
    ours, ours_from = a.estimate, "given" if a.estimate is not None else None
    if ours is None:
        sdoc = read_json(a.strategy, "strategy", required=False)
        if strategy_matches(sdoc, legs) and isinstance(sdoc.get("margin"), dict):
            ours, ours_from = _f(sdoc["margin"].get("required")), "trader-strategy (%s)" % a.strategy
    if not ad.token():
        raise TicketError("no %s token in this shell: eval \"$(python3 ticket.py login %s)\" first. Nothing was "
                          "sent" % (ad.env_names["access_token"], b))
    bm = _broker_call(ad.basket_margin, [adapter_order(o) for o in legs])
    key = margin_key(margin_rows(legs))
    total = bm.get("total")
    diff = round_paise(total - ours) if (total is not None and ours) else None
    entry = {"key": key, "date": today, "at": ist_iso(now), "broker": b, "title": ad.title, "legs": len(legs),
             "total": total, "initial": bm.get("initial"), "hedge_benefit": bm.get("hedge_benefit"),
             "note": bm.get("note"), "our_estimate": ours, "our_estimate_from": ours_from, "difference": diff,
             "difference_pct": round(100.0 * diff / ours, 1) if diff is not None else None, "read_only": True}
    cache = read_json(a.cache, "broker margin cache", required=False) or {}
    checks = [c for c in cache.get("checks", []) if c.get("date") == today and c.get("key") != key]
    cache = {"note": "the broker's basket margin, read only, from ticket.py margin-check (newest per basket, today)",
             "checks": checks + [entry]}
    write_json(a.cache, cache)
    if a.json:
        _print_json(b, now, entry)
        return 0
    print("%s basket margin for %d leg%s at %s IST: %s%s." % (
        ad.title, len(legs), "" if len(legs) == 1 else "s", now.strftime("%H:%M"), inr(total or 0),
        (" (hedge benefit %s)" % inr(bm["hedge_benefit"])) if bm.get("hedge_benefit") else ""))
    if ours:
        print("Our SPAN-style estimate: %s, from %s. Difference: %s%s (%+.1f%%). The broker's figure is what it blocks."
              % (inr(ours), ours_from, "+" if diff >= 0 else "-", inr(abs(diff)), entry["difference_pct"]))
    else:
        print("No estimate of ours to compare (run trader-strategy for these legs, or pass --estimate).")
    print("Read only: nothing was sent to %s. Saved for trader-strategy and trader-risk in %s." % (ad.title, a.cache))
    return 0


def cmd_instruments(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    b = which_broker(a, cfg)
    ad = get_adapter(b, cfg)
    inst = _broker_call(ad.resolve, a.symbol, a.exchange)
    if a.json:
        _print_json(b, now, inst)
        return 0
    print("%s on %s at %s: %s (id %s), lot %s, tick %s%s, exchange token %s." % (
        inst["symbol"], inst["exchange"], ad.title, inst["broker_symbol"], inst["broker_id"], inst["lot_size"],
        inst["tick_size"], (", freeze %s" % inst["freeze_qty"]) if inst.get("freeze_qty") else "",
        inst["exchange_token"]))
    return 0

# ---------------------------------------------------------------- end of live brokers


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets, your yes, the paper broker and read-only live "
                                             "broker accounts (trader-broker). Paper by default.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:41\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--lang", choices=LANGS, metavar="LANG", help="messages in en or hi-en (Hinglish); default: the config's lang")
        clock(p)

    def fno_inputs(p):
        p.add_argument("--options", action="append", help="trader-options JSON with the latest chain (repeat it for "
                                                          "more expiries; default trader/options.json)")
        p.add_argument("--strategy", default="trader/strategy.json", help="trader-strategy's file (its margin)")
        p.add_argument("--portfolio", default="trader/portfolio.json")

    p = sub.add_parser("show", help="render the tickets (a multi-leg F&O basket as one ticket)")
    common(p)
    fno_inputs(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("confirm", help="record the user's explicit yes for one order (or one basket) and paper-place it")
    common(p)
    fno_inputs(p)
    p.add_argument("--index", type=int)
    p.add_argument("--basket", action="store_true", help="the yes is for the whole basket ticket")
    p.add_argument("--basket-id", help="the basket id the user was shown (refused if the file changed)")
    p.add_argument("--yes-text", required=True, help="the user's reply, word for word")
    p.add_argument("--actor", choices=list(ACTORS), default="user",
                   help="who typed the yes: user (default) or autopilot (trader-autopilot's machine yes, PAPER only)")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--basket", action="store_true")
    p.add_argument("--reply")
    p = sub.add_parser("fill", help="simulate a paper fill (a basket: every leg at the latest chain, or none)")
    p.add_argument("--index", type=int, help="the order in trader/orders.json (after confirm)")
    p.add_argument("--basket", action="store_true", help="fill the confirmed basket, hedge first, all or nothing")
    p.add_argument("--leg-price", action="append", metavar="LEG=PRICE", help="a basket leg's fill price (repeat)")
    p.add_argument("--bands", default="trader/bands.csv", help="NSE's F&O contract master (price bands) or a JSON")
    p.add_argument("--options", action="append")
    p.add_argument("--strategy", default="trader/strategy.json")
    p.add_argument("--leg", choices=["order", "stop", "target"], help="which part filled (default: the order)")
    p.add_argument("--symbol")
    p.add_argument("--side", type=str.upper, choices=["BUY", "SELL"])
    p.add_argument("--qty", type=int)
    p.add_argument("--product", type=str.upper, choices=["MIS", "CNC", "NRML"])
    p.add_argument("--price", help="the fill price (default: the fresh price in trader/prices.json)")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--trades", default="trader/state/trades.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("flatten", help="SELL tickets for every open MIS position, by flat_by")
    p.add_argument("--symbol", help="only this symbol")
    p.add_argument("--include-cnc", action="store_true", help="also sell delivery (CNC) holdings")
    p.add_argument("--orders", default="trader/orders.json", help="where the tickets are written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("status", help="cash, positions, working legs, today's fills and limits")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--trades", default="trader/state/trades.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("mtm", help="F&O positions marked to the latest chain: P&L, Greeks, margin (read only)")
    fno_inputs(p)
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true", help="also keep the marks on the positions in portfolio.json")
    clock(p)
    p = sub.add_parser("adjust", help="an F&O adjustment ticket (roll, close a leg, add a hedge); places nothing")
    p.add_argument("--strategy", required=True, help="the strategy id, like S-20261102-1 (ticket.py mtm)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--close-leg", type=int, metavar="N")
    g.add_argument("--roll-leg", type=int, metavar="N")
    g.add_argument("--add-hedge", metavar="\"BUY 1 PE 22500\"")
    g.add_argument("--close-all", action="store_true")
    p.add_argument("--strike", type=float, help="with --roll-leg: the new strike")
    p.add_argument("--expiry", help="with --roll-leg: the new expiry, YYYY-MM-DD")
    p.add_argument("--options", action="append")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("settle", help="settle expiring F&O positions at NSE's final settlement price")
    p.add_argument("--date", help="the expiry day, YYYY-MM-DD (default today)")
    p.add_argument("--settlement", help="NSE FOSett_prce CSV, the F&O bhavcopy CSV of that day, or a JSON")
    p.add_argument("--price", action="append", metavar="UNDERLYING=PRICE", help="a final settlement price by hand")
    p.add_argument("--options", action="append")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--trades", default="trader/state/trades.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    brokers = list(LIVE_BROKERS) + ["zerodha", "angelone"]
    p = sub.add_parser("shadow-report", help="each shadowed live order next to its paper twin (read only)")
    p.add_argument("--shadow", default="trader/state/shadow.json")
    p.add_argument("--live", action="store_true", help="read the live order book (nothing is sent)")
    p.add_argument("--live-orders", help="a saved live order book (JSON) instead of --live")
    p.add_argument("--broker", type=str.lower, choices=brokers)
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    clock(p)

    def reader(name, what):
        p = sub.add_parser(name, help=what)
        p.add_argument("--broker", type=str.lower, choices=brokers, help="default: the config broker")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--json", action="store_true")
        clock(p)
        return p

    reader("accounts", "your broker account: profile, funds, token, what the API can do (read only)")
    reader("funds", "available and used funds at your broker (read only)")
    reader("holdings", "delivery holdings at your broker (read only)")
    reader("positions", "today's positions at your broker (read only)")
    reader("orders", "today's orders and trades at your broker (read only)")
    p = reader("instruments", "look one symbol up in the broker's instrument master (read only)")
    p.add_argument("--symbol", required=True, help="RELIANCE, or \"NIFTY 2026-11-23 25000 CE\"")
    p.add_argument("--exchange", type=str.upper, choices=["NSE", "NFO"], default="NSE")
    p = sub.add_parser("login", help="log in to a broker (browser, or Angel One's TOTP); prints export lines for "
                                     "your shell")
    p.add_argument("broker", type=str.lower, choices=brokers)
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--timeout", type=int, default=180, help="seconds to wait for the login (default 180)")
    p.add_argument("--no-browser", action="store_true", help="print the login URL instead of opening it")
    clock(p)
    p = sub.add_parser("preflight", help="static IP, token and a harmless call: ready for live orders?")
    p.add_argument("broker", type=str.lower, choices=brokers)
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--preflight", default="trader/state/preflight.json")
    p.add_argument("--ip", help="skip the IP echo services and use this public IP")
    p.add_argument("--json", action="store_true")
    clock(p)
    legs = ["entry", "stop", "target", "oco", "bracket"]
    for name, what in (("place", "send ONE approved order (or one native linked order) to the live broker, after "
                                 "every gate"),
                       ("cancel", "cancel one live order (or linked order) this tool sent")):
        p = sub.add_parser(name, help=what)
        common(p)
        p.add_argument("--index", type=int, required=(name == "place"))
        p.add_argument("--leg", choices=legs, default="entry",
                       help="entry, stop, target; oco: the stop and target as one linked order after the fill (Kite); "
                            "bracket: the entry with both (Upstox GTT, Dhan Super Order)")
        p.add_argument("--preflight", default="trader/state/preflight.json")
        p.add_argument("--prices", default="trader/prices.json", help="shadow mode: the paper twin's price")
        p.add_argument("--options", action="append", help="shadow mode: the chain for an F&O twin")
        p.add_argument("--shadow", default="trader/state/shadow.json")
        if name == "place":
            p.add_argument("--last-price", type=float, help="oco at Kite: the last price for the GTT (default: the "
                                                            "entry's fill price)")
        else:
            p.add_argument("--linked-id", help="cancel this linked order (from ticket.py reconcile), any day")
    p = sub.add_parser("modify-exits", help="new stop and target for a working linked order; the new ticket needs its "
                                            "own risk PASS and your yes")
    common(p)
    p.add_argument("--index", type=int, required=True, help="the NEW ticket (same position, new stop_loss and target)")
    p.add_argument("--linked-id", required=True, help="the linked order to change (ticket.py reconcile lists them)")
    p.add_argument("--last-price", type=float, help="Kite: the last price for the GTT (default: a quote)")
    p.add_argument("--preflight", default="trader/state/preflight.json")
    p = sub.add_parser("reconcile", help="match the broker's GTT / super-order book to this tool's linked orders "
                                         "(read only at the broker)")
    p.add_argument("--broker", type=str.lower, choices=brokers, help="default: the config broker")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--days", type=int, default=7, help="this tool's linked orders from the last N days (default 7)")
    p.add_argument("--json", action="store_true")
    clock(p)
    p = sub.add_parser("margin-check", help="the broker's basket margin for F&O legs next to our estimate (read "
                                            "only)")
    p.add_argument("--broker", type=str.lower, choices=brokers, help="default: the config broker")
    p.add_argument("--orders", default="trader/orders.json", help="the basket legs (trader-strategy's orders)")
    p.add_argument("--legs-json", help="a JSON list of basket legs instead of --orders")
    p.add_argument("--strategy", default="trader/strategy.json", help="our estimate, when it is this basket")
    p.add_argument("--estimate", type=float, help="our estimate in rupees (trader-strategy and trader-risk pass it)")
    p.add_argument("--cache", default=MARGIN_CACHE, help="where the result is kept for trader-strategy and trader-risk")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    clock(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if getattr(a, "broker", None) is not None:
        a.broker = broker_name(a.broker)
    try:
        return {"show": cmd_show, "confirm": cmd_confirm, "decline": cmd_decline, "fill": cmd_fill,
                "flatten": cmd_flatten, "status": cmd_status, "accounts": cmd_accounts, "funds": cmd_funds,
                "holdings": cmd_holdings, "positions": cmd_positions, "orders": cmd_orders, "login": cmd_login,
                "preflight": cmd_preflight, "place": cmd_place, "cancel": cmd_cancel,
                "instruments": cmd_instruments, "mtm": cmd_mtm, "adjust": cmd_adjust, "settle": cmd_settle,
                "shadow-report": cmd_shadow_report, "modify-exits": cmd_modify_exits, "reconcile": cmd_reconcile,
                "margin-check": cmd_margin_check}[a.cmd](a)
    except (TicketError, RulesError) as e:
        print("ticket.py: %s" % adapters().base.scrub(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
