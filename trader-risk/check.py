#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (NSE cash market). Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so orders that are fine one by one but
break a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-29 09:40"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, F&O and short selling
only; every other rule (day limits, window, size) becomes a warning for it.

What blocks any order:

  format      no symbol, side not BUY/SELL, qty not a whole number above 0,
              unknown order_type / validity, a LIMIT with no price, legs
              that are not SL-M/SL stop and LIMIT target
  tick        a price, trigger or leg not on the tick (tick_size, auto by price band)
  exchange    anything but NSE (this pack trades the NSE cash market)
  fno         futures and options (NIFTY26NOVFUT, NIFTY26NOV25000CE, NFO,
              product NRML) unless "allow_fno": true
  ticker      not an NSE symbol: ^[A-Z0-9][A-Z0-9&-]{0,19}$
  short       selling more than you hold unless "allow_short": true (and
              never in CNC: delivery shorts are not possible)

What blocks an entry (on top):

  blocked     a symbol on your "blocked" list
  size        under min_order_value, over max_order_value
  position    the stock would be over max_position_pct of your capital
  cash        dips into your cash buffer, or needs broker leverage
  stop        no stop while "require_stop": true; stop on the wrong side
  target      target on the wrong side of the entry
  loss        qty x (entry - stop) + estimated round-trip charges over
              max_loss_per_trade. The entry is the LIMIT price for a limit
              order, else the HIGHER of the plan's entry and a fresh price
              (under 15 minutes old) from trader/prices.json
  reward:risk (target - entry) / (entry - stop) under min_reward_risk
  daily loss  today's entries' max losses plus this one over max_daily_loss
  trades      max_trades_per_day entries already placed today
  window      before no_entries_before / after no_entries_after (IST), or the
              market is closed (weekend, NSE holiday, extra_holidays, outside
              09:15-15:30; a special_sessions date is open in its own hours).
              For an F&O stock (fno_stocks in the config, else trader/fno_stocks.json,
              else every stock) continuous trading ends early under the closing
              auction (15:15 from 2026-08-03, 15:30 from 2026-12-07), and the last
              entry is 15 minutes before the safe flat-by (SESSION_RULES and
              broker_profile; see entry_cutoff)
  duplicate   the same symbol and side twice in this batch or placed today
  daily limit max_orders_per_day orders placed today

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
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
    "exchange_txn_pct": 0.00297,
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


# ---------------------------------------------------------------- F&O charges, dated (same in budget, risk, broker, exit, timemachine)
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

# ---------------------------------------------------------------- the rest of trader-risk

NSE_HOLIDAYS = {  # Capital Market segment, NSE holiday master
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
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
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)

FNO = re.compile(r"^[A-Z][A-Z&\-]*\d{2}[A-Z0-9]{3}(FUT|\d+(\.\d+)?(CE|PE))$")
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALIDITY = {"DAY", "IOC"}
FNO_EXCHANGES = {"NFO", "BFO", "MCX", "CDS", "BCD", "NCO"}
NOT_SENT = ("why", "plan", "note")
PAISA = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


# ---------------------------------------------------------------- dated market rules (same in entry, exit, alerts, risk, timemachine, scanner, broker)
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


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time and the NSE calendar (IST)

def parse_now(text):
    """ISO time -> aware IST datetime. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-29 09:40\" (IST) or ...Z (UTC)")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def parse_time(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def session(d, c, segment="EQ"):
    """The NSE session on date d: {"open", "close", "closed": reason or None, "special": name or None,
    "warn": [...]}. special_sessions win over weekends and holidays. On a regular day "close"
    is the end of continuous trading for the segment (SESSION_RULES)."""
    iso = d.isoformat()
    warn = []
    for s in c.get("special_sessions") or []:
        if isinstance(s, dict) and str(s.get("date")) == iso:
            return {"open": hhmm(s.get("open"), "special_sessions.open"),
                    "close": hhmm(s.get("close"), "special_sessions.close"), "closed": None,
                    "special": s.get("name") or "special session", "warn": warn}
    if d.year not in CALENDAR_YEARS:
        warn.append("the NSE holiday calendar for %d is not in this tool (it has %s): weekdays are treated as "
                    "open. Check nseindia.com and add the holidays to \"extra_holidays\""
                    % (d.year, ", ".join(map(str, CALENDAR_YEARS))))
    rules = session_for(d, segment)
    base = {"open": rules["open"], "close": rules["continuous_end"], "closed": None, "special": None, "warn": warn,
            "cas": rules["cas"]}
    if d.weekday() >= 5:
        base["closed"] = "weekend"
    elif iso in NSE_HOLIDAYS:
        base["closed"] = "NSE holiday: " + NSE_HOLIDAYS[iso]
    elif iso in {str(x) for x in (c.get("extra_holidays") or [])}:
        base["closed"] = "holiday (your extra_holidays)"
    return base


def window_check(now, c, segment="EQ"):
    """(reasons, warnings) for a new entry at now (IST) in a stock of this segment."""
    s = session(now.date(), c, segment)
    warns = list(s["warn"])
    t = now.time().replace(second=0, microsecond=0)
    stamp = now.strftime("%a %Y-%m-%d %H:%M")
    if s["closed"]:
        return ["window: the market is closed today (%s, %s IST)" % (s["closed"], stamp)], warns
    if s.get("cas") and s["close"] <= now.time() < s["cas"]["end"]:
        return ["window: continuous trading in F&O stocks ended at %s IST; the closing auction runs to %s "
                "(%s IST): no new intraday entries" % (s["close"].strftime("%H:%M"), s["cas"]["end"].strftime("%H:%M"),
                                                       stamp)], warns
    if s["open"] is None or s["close"] is None or not (s["open"] <= now.time() < s["close"]):
        return ["window: the market is closed (%s IST; the %s is %s to %s)"
                % (stamp, s["special"] or "normal session",
                   s["open"].strftime("%H:%M") if s["open"] else "?",
                   s["close"].strftime("%H:%M") if s["close"] else "?")], warns
    if s["special"]:
        warns.append("%s today (%s to %s IST): your no_entries_before/after and flat_by do not apply; be flat "
                     "before %s" % (s["special"], s["open"].strftime("%H:%M"), s["close"].strftime("%H:%M"),
                                    s["close"].strftime("%H:%M")))
        return [], warns
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    why = ""
    cut = entry_cutoff(now.date(), segment, dict(c, no_entries_after=None))
    if after is None or cut < after:
        flat, note = safe_flat_by(now.date(), segment, c)
        after, why = cut, " (15 minutes before the safe flat-by %s: %s)" % (
            flat.strftime("%H:%M"), note.split(": ", 1)[-1] if note else "")
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)%s" % (after.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"), why)], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked
    (every field except the notes "why", "plan" and "note")."""
    core = {k: v for k, v in order.items() if k not in NOT_SENT and v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(x):
    return "%.2f" % round_paise(x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def valid_tick(price, setting="auto"):
    """The finest tick NSE can use at this price ("auto"), or the configured one.

    Checks are permissive on purpose: producers round to the coarsest tick of the
    band (see tick_for in trader-budget), and a valid price must never be blocked."""
    if setting not in (None, "", "auto"):
        return float(setting)
    return 0.01 if float(price) < 250 else 0.05


def on_tick(x, tick):
    t = max(to_paise(tick), 1)
    return to_paise(x) % t == 0 and abs(float(x) * 100 - to_paise(x)) < 1e-6


def positions_of(portfolio):
    """{SYM: (qty, avg_price)} from portfolio.json: {"positions": {SYM: {"qty", "avg_price"}}}
    (a list of {"symbol", "qty"} rows is read too)."""
    out = {}
    pos = (portfolio or {}).get("positions") or {}
    rows = pos.items() if isinstance(pos, dict) else [((p or {}).get("symbol") or (p or {}).get("ticker"), p)
                                                       for p in pos]
    for sym, p in rows:
        if not sym or not isinstance(p, dict):
            continue
        q = _f(p.get("qty", p.get("shares"))) or 0.0
        prev = out.get(str(sym).upper(), (0.0, None))
        out[str(sym).upper()] = (prev[0] + q, _f(p.get("avg_price")) or prev[1])
    return out


def account(c, portfolio, prices):
    """Cash, shares and position values. No portfolio = a fresh account funded with the capital."""
    warnings = []
    if portfolio is None:
        cap = _f(c.get("capital"))
        if not cap:
            raise RiskError("no trader/portfolio.json and no capital in the config: nothing to check against")
        return {"cash": cap, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for sym, (q, avg) in positions_of(portfolio).items():
        shares[sym] = q
        if prices.get(sym):
            values[sym] = q * prices[sym]
        else:
            values[sym] = q * (avg or 0)
            warnings.append("no price for %s: valued at its average price %s" % (sym, inr(avg or 0)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).upper() == "BUY"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker (a paper order counts once it has your yes)."""
    return [r for r in (state or {}).get("orders", [])
            if r.get("status") == "placed" and rec_day(r) == today]


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...], ...}.

    now          aware datetime (default: the real clock); its IST date is "today"
    price_times  {SYM: aware time of that price}; None = every price counts as fresh"""
    c = merged_config(cfg)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now = (now or parse_now(None)).astimezone(IST)
    today = today or now.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    capital = _f(c.get("capital"))
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    exchange_ok = str(c.get("exchange") or "NSE").upper()
    with_charges = bool(c.get("include_charges", True))
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in the daily "
                            "loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).upper()) for r in done_today}
    win_reasons, win_warns = window_check(now, c)
    fno_list = c["_fno"] if "_fno" in c else load_fno_stocks(c)[0]
    windows = {}

    def window_for(sym):
        seg = segment_of(sym, fno_list) if sym else "EQ"
        if seg not in windows:
            windows[seg] = window_check(now, c, seg)[0]
        return windows[seg]
    seen, results = set(), []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders would use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = [], [], [], []      # later: entry-only, a warning for an exit
        o = o if isinstance(o, dict) else {}
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().upper()
        otype = str(o.get("order_type") or "MARKET").strip().upper()
        validity = str(o.get("validity") or "DAY").strip().upper()
        product = str(o.get("product") or c.get("product") or "MIS").strip().upper()
        exch = str(o.get("exchange") or exchange_ok).strip().upper()
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        sl = o.get("stop_loss") if isinstance(o.get("stop_loss"), dict) else None
        tg = o.get("target") if isinstance(o.get("target"), dict) else None
        q_raw = o.get("qty")
        q = _f(q_raw)

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("BUY", "SELL"):
            reasons.append("format: side must be BUY or SELL, got %r" % o.get("side"))
        if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("format: qty must be a whole number of shares above 0, got %r" % q_raw)
            q = None
        if otype not in ORDER_TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT, SL, SL-M)" % otype)
        if validity not in VALIDITY:
            reasons.append("format: unknown validity %r (DAY or IOC)" % validity)
        if otype in ("LIMIT", "SL") and (_f(o.get("price")) or 0) <= 0:
            reasons.append("format: a %s order needs a positive price" % otype)
        if otype in ("SL", "SL-M") and (_f(o.get("trigger_price")) or 0) <= 0:
            reasons.append("format: a %s order needs a positive trigger_price" % otype)
        if otype == "MARKET" and o.get("price") is not None:
            warns.append("a MARKET order ignores its price %s" % o.get("price"))
        if o.get("stop_loss") is not None:
            if sl is None or str(sl.get("order_type", "")).upper() not in ("SL-M", "SL") or \
                    (_f(sl.get("trigger_price")) or 0) <= 0:
                reasons.append("format: stop_loss must be {\"order_type\": \"SL-M\" (or \"SL\"), \"trigger_price\": "
                               "a positive number}")
            elif str(sl.get("order_type")).upper() == "SL" and (_f(sl.get("price")) or 0) <= 0:
                reasons.append("format: an SL stop_loss needs a limit price as well as its trigger_price")
        if o.get("target") is not None:
            if tg is None or str(tg.get("order_type", "")).upper() != "LIMIT" or (_f(tg.get("price")) or 0) <= 0:
                reasons.append("format: target must be {\"order_type\": \"LIMIT\", \"price\": a positive number}")
        # tick
        ticks = [("price", o.get("price") if otype in ("LIMIT", "SL") else None), ("trigger_price", o.get("trigger_price")),
                 ("stop_loss trigger", (sl or {}).get("trigger_price")), ("stop_loss price", (sl or {}).get("price")),
                 ("target price", (tg or {}).get("price"))]
        for name, v in ticks:
            fv = _f(v)
            tick = valid_tick(fv, c.get("tick_size")) if fv else None
            if fv and fv > 0 and not on_tick(fv, tick):
                reasons.append("tick: %s %s is not a multiple of the %s tick" % (name, v, _p(tick)))

        # exchange, F&O, ticker
        fno = bool(sym and FNO.match(sym)) or exch in FNO_EXCHANGES or product == "NRML"
        if fno and not c.get("allow_fno"):
            reasons.append("fno: %s looks like futures or options (%s); F&O is off (\"allow_fno\": false). "
                           "This pack trades NSE cash equities" % (sym or "?", exch if exch in FNO_EXCHANGES
                                                                  else ("product NRML" if product == "NRML"
                                                                        else "derivative symbol")))
        elif fno:
            warns.append("F&O order: derivatives can lose more than you put in, and this pack's sizing assumes "
                         "cash shares")
        else:
            if exch != "NSE":
                reasons.append("exchange: %s is not NSE; this pack trades the NSE cash market" % exch)
            if product not in ("MIS", "CNC"):
                reasons.append("product: %s is not MIS (intraday) or CNC (delivery)" % product)
            if sym and not TICKER.match(sym):
                reasons.append("ticker: %r is not an NSE symbol (capitals, digits, & and -, like M&M or "
                               "BAJAJ-AUTO)" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if otype in ("LIMIT", "SL") and _f(o.get("price")):
            ref, ref_from = _f(o["price"]), "limit"
        elif otype == "SL-M" and _f(o.get("trigger_price")):
            ref, ref_from = _f(o["trigger_price"]), "trigger"
        else:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(live), _p(plan_entry), _p(live)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if otype == "MARKET" and live is None and sym in prices and price_times is not None:
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        value = q * ref if (q and ref) else None

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "SELL" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "BUY" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to value a %d-share order; add it to trader/prices.json "
                         "(trader-alerts: alerts.py quote %s --write)" % (sym, int(q), sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_value") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order" % (inr(value), inr(c["min_order_value"])))
            if c.get("max_order_value") and value > float(c["max_order_value"]) + PAISA:
                later.append("size: %s is over your %s max order value" % (inr(value), inr(c["max_order_value"])))

        # account effects, simulated in order
        cash = acct["cash"]
        base = capital or (cash + sum(acct["values"].values()))
        buffer_rs = base * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "BUY" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA:
                later.append("cash: needs %s but only %s cash; that would be broker leverage (MIS margin), which "
                             "this pack does not use" % (inr(value), inr(cash)))
            elif after_cash < buffer_rs - PAISA:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(max(after_cash, 0)), _num(c["cash_buffer_pct"]), inr(buffer_rs)))
            pos_after = held_value + value
            limit = base * float(c.get("max_position_pct") or 100) / 100.0
            if base > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of %s, over your %s%% cap"
                             % (sym, inr(pos_after), 100.0 * pos_after / base, inr(base),
                                _num(c["max_position_pct"])))
        if side == "SELL" and sym and kind == "entry" and q is not None and q > held + 1e-9:
            if product == "CNC":
                reasons.append("short: selling %d %s in CNC but you hold %s; delivery short selling is not "
                               "possible (the exchange auctions undelivered shares)" % (int(q), sym, _num(held)))
            elif not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off "
                               "(\"allow_short\": false). If you bought it, record the fill first (ticket.py fill)"
                               % (sym, _num(held)))

        # trade checks: stop, target, loss with charges, reward:risk, daily loss, trades, window
        gross = charges = max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("BUY", "SELL"):
            long = side == "BUY"
            leg_stop, plan_stop = _f((sl or {}).get("trigger_price")), _f(plan.get("stop"))
            stop = leg_stop or plan_stop
            target = _f((tg or {}).get("price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warns.append("the stop_loss trigger %s differs from the plan's stop %s: the leg's is the one that "
                             "is placed" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not an order leg: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): an SL-M "
                                 "stop_loss leg, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_p = abs(to_paise(ref) - to_paise(stop))
                    risk_ps = risk_p / 100.0
                    shares = q or 0
                    gross = round_paise(shares * risk_ps)
                    charges = 0.0
                    if with_charges and shares:
                        buy_px, sell_px = (ref, stop) if long else (stop, ref)
                        charges = round_trip_charges(buy_px, sell_px, shares, c)["total"]
                    max_loss = round_paise(gross + charges)
                    cap = _f(c.get("max_loss_per_trade"))
                    if cap and max_loss > cap + PAISA:
                        later.append("loss: %s shares x %s risk a share = %s%s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps), inr(gross),
                                                    (" + %s charges = %s" % (inr(charges), inr(max_loss)))
                                                    if charges else "", inr(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(abs(to_paise(target) - to_paise(ref)) / float(risk_p), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch; your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(window_for(sym))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("loss at the stop %s%s%s, reward:risk %s%s"
                            % (inr(gross), (" + %s charges = %s" % (inr(charges), inr(max_loss))) if charges else "",
                               (" (limit %s)" % inr(c["max_loss_per_trade"])) if _f(c.get("max_loss_per_trade")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch; your limit is %d a day"
                         % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and q is not None:
            if kind == "entry":
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "BUY":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                acct["shares"][sym] = held + q
            elif side == "SELL":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind, "product": product,
            "qty": int(q) if q is not None else q_raw,
            "value": round_paise(value) if value is not None else None,
            "entry_ref": round_paise(ref) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": gross, "charges": charges,
            "max_loss_with_charges": max_loss, "reward_risk": rr, "fingerprint": fingerprint(o),
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round_paise(acct["cash"]),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round_paise(loss_today)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s (%s), %s" % (r["verdict"], r["index"], r["side"], r["qty"], r["symbol"],
                                                         r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json (either shape) -> ({SYM: price}, {SYM: aware IST time or None})."""
    if not isinstance(pdoc, dict):
        return {}, {}
    out, times = {}, {}
    if isinstance(pdoc.get("prices"), dict):
        as_of = parse_time(pdoc.get("as_of"))
        detail = pdoc.get("detail") or {}
        for s, p in pdoc["prices"].items():
            if _f(p) is None:
                continue
            s = str(s).upper()
            out[s] = float(p)
            d = detail.get(s) or {}
            times[s] = parse_time(d.get("time")) or parse_time(d.get("fetched")) or as_of
        return out, times
    for s, row in pdoc.items():
        if isinstance(row, dict) and _f(row.get("price")) is not None:
            s = str(s).upper()
            out[s] = float(row["price"])
            times[s] = parse_time(row.get("time"))
    return out, times


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the capital")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:40\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-29")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        cache = os.path.join(os.path.dirname(a.config) or ".", "fno_stocks.json")
        cfg["_fno"] = load_fno_stocks(cfg, cache, fetch=False)[0]
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except (RiskError, RulesError) as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
