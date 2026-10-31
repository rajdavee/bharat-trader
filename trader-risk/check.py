#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (NSE cash market and F&O). Standard library only; the network only
for NSE's F&O ban list and lot sizes (cached, once a day; --offline never asks).

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
  kill        the kill switch is on for today (check.py kill)
  breaker     max_consecutive_losses losing trades in a row today, or a realised loss of
              max_daily_drawdown_pct of capital; cooling-off: cooloff_after_stop_min minutes
              after a stop filled; revenge: re-entering a symbol within revenge_window_min of a
              loss (blocks, or a warning with "revenge_trade": "warn")
  rate        max_orders_per_second orders already went in the last second (default 3,
              never above 9: SEBI registers algos above 10 a second)
  api         a live broker ("paper": false): MARKET / SL-M need market_protection, else LIMIT

F&O ("allow_fno": true). The legs of one basket (orders.json "basket": true, or a shared
basket_id) are checked as ONE position and pass or block together:

  contract    an NFO contract: symbol + expiry + strike + option_type (trader-strategy's
              fields) or a trading symbol like NIFTY26NOV23000CE; not expired
  lot         qty a multiple of the lot for that expiry month (NSE fo_mktlots.csv)
  ban         a stock underlying in NSE's F&O ban list for today (fo_secban) takes no new
              positions; with no list known the entry blocks (require_ban_list)
  stop        defined risk (every short covered by a long) counts as the stop
              (defined_risk_counts_as_stop); otherwise a risk_rule (max_loss or
              stop_underlying) or SL / SL-M stops on every short leg
  naked       uncovered short options need "allow_naked_short": true AND a stop rule
  loss        the basket's max loss (at expiry for defined risk, at the stop otherwise),
              charges included, against max_loss_per_trade and max_daily_loss
  premium     net premium paid against max_premium_per_trade / max_premium_per_day
  lots        max_lots_per_underlying per side (open positions + this batch)
  margin      the SPAN-style margin (trader-strategy's estimate when trader/strategy.json is
              this basket), its upper bound, against the cash left after cash_buffer_pct and
              margin already blocked. On expiry day: 2% more ELM on index shorts expiring
              today and no calendar-spread benefit
  greeks      the book's net delta (lots), gamma, vega and theta after this basket over
              max_net_delta_lots, max_net_gamma_lots_1pct, max_net_vega, max_theta_paid_per_day,
              unless the basket reduces it (a hedge always passes)
  scenario    the book's worst P&L after charges on the grid spot +/-1/2/3 sigma x IV
              +/-5/10 vol over max_scenario_loss; the reason names the scenario

Also:  check.py kill [--reason ...] [--flatten]   check.py unkill   check.py scenarios
       check.py refresh (NSE ban list + lot sizes)  check.py rate-gate (for broker adapters)

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
    # behaviour (trader-risk only): locks on new entries, never on exits
    "max_consecutive_losses": 3,
    "max_daily_drawdown_pct": 3,
    "cooloff_after_stop_min": 15,
    "revenge_window_min": 15,
    "revenge_trade": "block",
    "max_orders_per_second": 3,
    # F&O (trader-risk only; with "allow_fno": true). null = the default in fno_limits(), 0 = off
    "allow_naked_short": False,
    "defined_risk_counts_as_stop": True,
    "require_ban_list": True,
    "margin_use_upper_bound": True,
    "max_premium_per_trade": None,
    "max_premium_per_day": None,
    "max_lots_per_underlying": None,
    "max_net_delta_lots": None,
    "max_net_gamma_lots_1pct": None,
    "max_net_vega": None,
    "max_theta_paid_per_day": None,
    "max_scenario_loss": None,
    "scenario_sigmas": [1, 2, 3],
    "scenario_iv_shifts": [5, 10],
    "scenario_horizon_days": 1,
    "span": {},
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
        if str(p.get("exchange") or "").upper() in FNO_EXCHANGES or p.get("expiry") or FNO.match(str(sym).upper()):
            continue          # an F&O position: fno_positions() reads it
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


# ---------------------------------------------------------------- F&O contracts, lots and the ban list (NSE files, cached)

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
INDEX_QUOTES = {"NIFTY": ("NIFTY", "^NSEI", "NIFTY 50"), "BANKNIFTY": ("BANKNIFTY", "^NSEBANK", "NIFTY BANK"),
                "FINNIFTY": ("FINNIFTY",), "MIDCPNIFTY": ("MIDCPNIFTY",), "NIFTYNXT50": ("NIFTYNXT50",)}
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
LOTS_URL = NSE_ARCHIVES + "/content/fo/fo_mktlots.csv"
BAN_URL = NSE_ARCHIVES + "/content/fo/fo_secban.csv"
BAN_DATED_URL = NSE_ARCHIVES + "/archives/fo/sec_ban/fo_secban_%s.csv"      # DDMMYYYY, the trade date
NSE_GET_HEADERS = {"User-Agent": FNO_LIST_HEADERS["User-Agent"], "Accept": "application/json, text/csv, */*",
                   "Referer": "https://www.nseindia.com/"}
NSE_PAUSE = 1.5                     # seconds between two NSE requests
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"), 1)}
WEEK_MONTH = dict({str(i): i for i in range(1, 10)}, O=10, N=11, D=12)
FNO_PARTS = re.compile(r"^(?P<u>[A-Z][A-Z&\-]*?)(?P<yy>\d{2})(?:(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|"
                       r"DEC)|(?P<wm>[1-9OND])(?P<dd>\d{2}))(?:(?P<fut>FUT)|(?P<k>\d+(?:\.\d+)?)(?P<t>CE|PE))$")
OPTION_TICK = 0.05
EXPIRY_TIME = datetime.time(15, 30)          # equity derivatives settle at 15:30 IST on expiry day
YEAR_SECONDS = 365.0 * 86400
DEFAULT_RATE = 0.0557                       # trader-options' default (91-day T-bill, 08-Oct-2026 auction)
_LAST_GET = [0.0]


def nse_get(url, timeout=30, retries=2):
    """One polite GET from NSE (browser headers, a pause between calls, back-off on 403/429). Text or RulesError."""
    import time
    import urllib.error
    import urllib.request
    delay = 2.0
    for attempt in range(retries + 1):
        gap = time.time() - _LAST_GET[0]
        if gap < NSE_PAUSE:
            time.sleep(NSE_PAUSE - gap)
        _LAST_GET[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=NSE_GET_HEADERS), timeout=timeout) as r:
                data, ctype = r.read(), str(r.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise RulesError("HTTP %s for %s" % (e.code, url))
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise RulesError("could not reach %s (%s)" % (url, getattr(e, "reason", None) or e))
        text = data.decode("utf-8-sig", "replace")
        if "html" in ctype.lower() or text.lstrip().startswith("<"):
            raise RulesError("NSE sent a web page, not the file, for %s (missing or retired)" % url)
        return text
    raise RulesError("gave up on %s" % url)


def parse_lots(text):
    """fo_mktlots.csv text -> {"NIFTY": {"2026-11": 65, ...}, ...} (same reader as trader-options).

    The file is fixed-width padded, has an index block then a second header row for stocks, and its
    month columns read like NOV-26: lots are per expiry month."""
    import csv
    import io
    out, months = {}, None
    for row in csv.reader(io.StringIO(text)):
        cells = [x.strip() for x in row]
        if len(cells) < 3:
            continue
        if cells[1].upper() == "SYMBOL":
            months = []
            for x in cells[2:]:
                try:
                    months.append(datetime.datetime.strptime(x.title(), "%b-%y").strftime("%Y-%m"))
                except ValueError:
                    months.append(None)
            continue
        if months is None:
            continue
        lots = {}
        for m, x in zip(months, cells[2:]):
            v = _f(x)
            if m and v:
                lots[m] = int(v)
        if cells[1] and lots:
            out[cells[1].upper()] = lots
    return out


def parse_ban(text):
    """NSE fo_secban CSV -> {"trade_date": "2026-11-02", "symbols": ["KAYNES", "SAIL"]}."""
    m = re.search(r"Trade Date\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", text or "")
    if not m:
        raise RulesError("not NSE's F&O ban list (no 'Securities in Ban For Trade Date' line)")
    day = datetime.datetime.strptime(m.group(1).title(), "%d-%b-%Y").date().isoformat()
    syms = set()
    for line in text.splitlines():
        cells = [x.strip().upper() for x in line.split(",")]
        if len(cells) >= 2 and cells[0].isdigit() and TICKER.match(cells[1]):
            syms.add(cells[1])
    return {"trade_date": day, "symbols": sorted(syms)}


def _read_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_cache(path, doc):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def load_ban(cache_path, today, fetch=False):
    """(set of banned underlyings for trade date `today` or None if unknown, note).

    The cache (trader/fo_secban.json) keeps each trade date's list. fetch=True asks NSE for the dated
    file of that trade date first, then the current one (published the evening before)."""
    doc = (_read_cache(cache_path) if cache_path else None) or {}
    days = doc.get("days") if isinstance(doc.get("days"), dict) else {}
    errors = []
    if fetch and today not in days:
        d = datetime.date.fromisoformat(today)
        for url in (BAN_DATED_URL % d.strftime("%d%m%Y"), BAN_URL):
            try:
                b = parse_ban(nse_get(url))
            except RulesError as e:
                errors.append(str(e))
                continue
            days[b["trade_date"]] = {"symbols": b["symbols"], "source": url,
                                     "fetched": datetime.datetime.now(IST).isoformat(timespec="seconds")}
            if b["trade_date"] == today:
                break
        if cache_path:
            keep = sorted(days)[-15:]
            _write_cache(cache_path, {"source": BAN_URL, "days": {k: days[k] for k in keep}})
    if today in days:
        syms = set(days[today].get("symbols") or [])
        return syms, "NSE F&O ban list for %s: %s" % (today, ", ".join(sorted(syms)) or "none")
    return None, "no NSE F&O ban list for %s in %s%s" % (today, cache_path or "the cache",
                                                          (" (%s)" % "; ".join(errors)) if errors else
                                                          " (check.py refresh fetches it)")


def load_lots(path, fetch=False, today=None):
    """(lots per underlying and expiry month or None, note). path is trader/fo_mktlots.csv; fetch=True
    refreshes it from NSE at most once a day."""
    note = ""
    stamp = None
    if path and os.path.exists(path):
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path), IST).date().isoformat()
    if fetch and path and stamp != (today or datetime.datetime.now(IST).date().isoformat()):
        try:
            text = nse_get(LOTS_URL)
            if len(parse_lots(text)) < 20:
                raise RulesError("fo_mktlots.csv has too few rows: not trusting it")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            stamp = today
        except (RulesError, OSError) as e:
            note = " (refresh failed: %s)" % e
    if not path or not os.path.exists(path):
        return None, "no NSE lot-size file (%s)%s: lot sizes come from the orders" % (path, note)
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lots = parse_lots(fh.read())
    except OSError as e:
        return None, "cannot read %s (%s)" % (path, e)
    return (lots or None), "NSE lot sizes from %s (%s)%s" % (os.path.basename(path), stamp, note)


def lot_for(lots, underlying, expiry):
    """Lot size of underlying for the expiry's month, or None."""
    return ((lots or {}).get(underlying) or {}).get(str(expiry)[:7])


def _closed_day(d, c):
    iso = d.isoformat()
    return d.weekday() >= 5 or iso in NSE_HOLIDAYS or iso in {str(x) for x in (c.get("extra_holidays") or [])}


def monthly_expiry(year, month, c=None):
    """NSE's monthly expiry: the last Tuesday of the month, a day earlier for each holiday."""
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    while d.weekday() != 1:
        d -= datetime.timedelta(days=1)
    while _closed_day(d, c or {}):
        d -= datetime.timedelta(days=1)
    return d


def parse_day(text):
    """'2026-11-03' or '03-Nov-2026' -> '2026-11-03', else None."""
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y"):
        try:
            return datetime.datetime.strptime(s.title() if "-" in s and not s[:4].isdigit() else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def contract_of(o, c=None):
    """The F&O contract an order is for: ({"underlying", "kind", "instrument" OPT|FUT, "option_type",
    "strike", "expiry"}, None) or (None, reason). Reads trader-strategy's fields (symbol = underlying,
    expiry, strike, option_type, segment) or an NSE trading symbol (NIFTY26NOV23000CE, NIFTY26N0323000CE)."""
    sym = str(o.get("symbol") or "").strip().upper()
    seg = str(o.get("segment") or "").strip().upper()
    inst = str(o.get("instrument") or "").strip().upper()
    if o.get("expiry") and sym and TICKER.match(sym) and not FNO.match(sym):
        exp = parse_day(o.get("expiry"))
        if exp is None:
            return None, "contract: expiry %r is not a date like 2026-11-03" % o.get("expiry")
        if seg == "FUT" or inst.startswith("FUT") or (not o.get("option_type") and o.get("strike") is None):
            return {"underlying": sym, "kind": "index" if sym in INDICES else "stock", "instrument": "FUT",
                    "option_type": None, "strike": None, "expiry": exp}, None
        t = str(o.get("option_type") or "").strip().upper()
        k = _f(o.get("strike"))
        if t not in ("CE", "PE") or not k or k <= 0:
            return None, "contract: an option needs option_type CE or PE and a positive strike"
        return {"underlying": sym, "kind": "index" if sym in INDICES else "stock", "instrument": "OPT",
                "option_type": t, "strike": k, "expiry": exp}, None
    text = str(o.get("tradingsymbol") or "").strip().upper() or sym
    m = FNO_PARTS.match(text)
    if not m:
        return None, ("contract: %r is not an NSE F&O contract (give symbol, expiry, strike and option_type, or a "
                      "trading symbol like NIFTY26NOV23000CE)" % (text or "?"))
    year = 2000 + int(m.group("yy"))
    try:
        if m.group("mon"):
            exp = monthly_expiry(year, MONTHS[m.group("mon")], c).isoformat()
        else:
            exp = datetime.date(year, WEEK_MONTH[m.group("wm")], int(m.group("dd"))).isoformat()
    except ValueError:
        return None, "contract: %r has no valid expiry date" % text
    u = m.group("u")
    base = {"underlying": u, "kind": "index" if u in INDICES else "stock", "expiry": exp}
    if m.group("fut"):
        return dict(base, instrument="FUT", option_type=None, strike=None), None
    return dict(base, instrument="OPT", option_type=m.group("t"), strike=float(m.group("k"))), None


def contract_label(k):
    if k["instrument"] == "FUT":
        return "%s FUT %s" % (k["underlying"], k["expiry"])
    return "%s %s %s %s" % (k["underlying"], _num(k["strike"]), k["option_type"], k["expiry"])


def contract_key(k):
    return (k["underlying"], k["instrument"], k.get("option_type"), _f(k.get("strike")), k["expiry"])


# ---------------------------------------------------------------- Black-76 (same maths as trader-options and trader-strategy)

def _ncdf(x):
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def b76_price(F, K, T, r, sigma, kind):
    """Black-76 price of a European option on forward F; T years, r and sigma decimals."""
    import math
    df = math.exp(-r * max(T, 0.0))
    if T <= 0 or sigma <= 0 or F <= 0:
        return df * max(0.0, (F - K) if kind == "CE" else (K - F))
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    d2 = d1 - vt
    if kind == "CE":
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def b76_greeks(F, K, T, r, sigma, kind):
    """Per unit: delta dPrice/dF, gamma per point, theta per calendar day, vega per 1 vol point.
    Under an hour to expiry the option is treated as its intrinsic value (delta 0 or 1, no gamma)."""
    import math
    if T < 1.0 / (365 * 24) or sigma <= 0 or F <= 0:
        itm = (F > K) if kind == "CE" else (F < K)
        return {"delta": (1.0 if kind == "CE" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    df = math.exp(-r * T)
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    price = b76_price(F, K, T, r, sigma, kind)
    delta = df * _ncdf(d1) if kind == "CE" else -df * _ncdf(-d1)
    theta_year = -df * F * pdf * sigma / (2.0 * math.sqrt(T)) + r * price
    return {"delta": delta, "gamma": df * pdf / (F * vt), "theta": theta_year / 365.0,
            "vega": df * F * pdf * math.sqrt(T) / 100.0}


def b76_iv(price, F, K, T, r, kind):
    """Implied volatility in percent from a premium, by bisection on [0.1%, 500%]; None if untradeable."""
    if not price or not F or not K or T is None or T < 1.0 / (365 * 24):
        return None
    lo, hi = 0.001, 5.0
    if not (b76_price(F, K, T, r, lo, kind) < price < b76_price(F, K, T, r, hi, kind)):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if b76_price(F, K, T, r, mid, kind) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 100.0 * 0.5 * (lo + hi)


def years_to(expiry, now):
    """Years (365-day) from now to 15:30 IST on the expiry date; 0 once it has passed."""
    end = datetime.datetime.combine(datetime.date.fromisoformat(expiry), EXPIRY_TIME).replace(tzinfo=IST)
    return max(0.0, (end - now).total_seconds() / YEAR_SECONDS)


# ---------------------------------------------------------------- the F&O book: open positions + proposals, valued

def fno_positions(portfolio, c=None):
    """Open F&O positions from portfolio.json: [{"contract", "qty" (signed: + long, - short), "avg_price",
    "lot_size", "iv", "ltp"}]. A position is F&O when it says exchange NFO, carries an expiry, or its key is
    a contract symbol. qty may be signed, or positive with "side": "SELL" (or "short": true)."""
    out = []
    pos = (portfolio or {}).get("positions") or {}
    rows = list(pos.items()) if isinstance(pos, dict) else [((p or {}).get("tradingsymbol") or (p or {}).get("symbol"), p)
                                                             for p in pos]
    rows += [((p or {}).get("tradingsymbol") or (p or {}).get("symbol"), p)
             for p in ((portfolio or {}).get("fno_positions") or []) if isinstance(p, dict)]
    for key, p in rows:
        if not isinstance(p, dict):
            continue
        is_fno = str(p.get("exchange") or "").upper() in FNO_EXCHANGES or p.get("expiry") or \
            bool(key and FNO.match(str(key).upper()))
        if not is_fno:
            continue
        o = dict(p)
        o.setdefault("symbol", p.get("underlying") or key)
        if p.get("underlying"):
            o["symbol"] = p["underlying"]
        if not p.get("expiry") and key:
            o["tradingsymbol"] = key
        k, why = contract_of(o, c)
        q = _f(p.get("qty", p.get("quantity"))) or 0.0
        if q > 0 and (str(p.get("side") or "").upper() == "SELL" or p.get("short") is True):
            q = -q
        if not q:
            continue
        out.append({"contract": k, "error": why, "name": str(key), "qty": q, "avg_price": _f(p.get("avg_price")),
                    "lot_size": int(_f(p.get("lot_size")) or 0) or None, "iv": _f(p.get("iv")),
                    "ltp": _f(p.get("ltp") or p.get("last_price") or p.get("mark")),
                    "forward": _f(p.get("forward"))})
    return out


def market_inputs(strategy=None, options=None):
    """{(underlying, expiry): {"forward", "spot", "atm_iv", "asof", "ivs": {(type, strike): iv}, "source"}}
    from trader/strategy.json (its priced legs) and trader/options.json (a whole chain)."""
    out = {}
    docs = options if isinstance(options, list) else ([options] if isinstance(options, dict) else [])
    for a in docs:
        if not isinstance(a, dict) or not a.get("underlying") or not a.get("expiry"):
            continue
        m = out.setdefault((str(a["underlying"]).upper(), str(a["expiry"])), {"ivs": {}})
        m.update({"forward": _f(a.get("forward")), "spot": _f(a.get("spot")), "atm_iv": _f(a.get("atm_iv")),
                  "asof": a.get("asof"), "source": "trader/options.json (%s)" % a.get("asof")})
        for row in a.get("rows") or []:
            for side in ("ce", "pe"):
                iv = _f((row.get(side) or {}).get("iv"))
                if iv:
                    m["ivs"][(side.upper(), _f(row.get("strike")))] = iv
    if isinstance(strategy, dict) and strategy.get("underlying"):
        u = str(strategy["underlying"]).upper()
        for l in strategy.get("legs") or []:
            if not l.get("expiry"):
                continue
            m = out.setdefault((u, str(l["expiry"])), {"ivs": {}})
            if _f(l.get("forward")):
                m["forward"] = _f(l["forward"])
                m["spot"] = _f(strategy.get("spot")) or m.get("spot")
                m["asof"] = strategy.get("asof")
                m["source"] = "trader/strategy.json (%s)" % strategy.get("asof")
            if m.get("atm_iv") is None:
                m["atm_iv"] = _f(strategy.get("atm_iv"))
            if l.get("instrument") == "OPT" and _f(l.get("iv")):
                m["ivs"][(l.get("option_type"), _f(l.get("strike")))] = _f(l["iv"])
    return out


def underlying_price(u, prices):
    for name in INDEX_QUOTES.get(u, (u,)):
        if (prices or {}).get(name):
            return prices[name]
    return None


def book_leg(k, qty, entry, now, mk, prices, rate, lot=None, iv=None, forward=None, is_open=False, label=None,
             mark=None):
    """One valued leg of the book: the contract, signed qty, the entry premium (a proposal) or the current
    mark (an open position), and the model inputs (forward, IV, years left). (leg, None) or (None, why)."""
    m = mk.get((k["underlying"], k["expiry"])) or {}
    live = underlying_price(k["underlying"], prices)
    F, src = forward or m.get("forward"), m.get("source")
    if F and live and m.get("spot") and not forward:
        F = F * live / m["spot"]
    if not F and live:
        F, src = live, "trader/prices.json (spot as the forward)"
    if not F and k["instrument"] == "FUT" and mark:
        F, src = mark, "the future's own last price"
    if not F:
        other = [x for (uu, _), x in sorted(mk.items()) if uu == k["underlying"] and x.get("spot")]
        if other:
            F, src = other[0]["spot"], "%s spot as the forward" % (other[0].get("source") or "the chain")
    if not F:
        return None, ("no price for %s: write a fresh chain (trader-options: options.py %s --write) or a strategy "
                      "(trader-strategy --write)" % (k["underlying"], k["underlying"]))
    T = years_to(k["expiry"], now)
    leg = {"label": label or contract_label(k), "contract": k, "underlying": k["underlying"], "kind": k["kind"],
           "instrument": k["instrument"], "option_type": k.get("option_type"), "strike": k.get("strike"),
           "expiry": k["expiry"], "qty": float(qty), "lot_size": lot, "F": F, "T": T, "rate": rate,
           "open": is_open, "source": src, "asof": m.get("asof") if src == m.get("source") else None, "notes": []}
    if k["instrument"] == "OPT":
        sig = iv or m["ivs"].get((k["option_type"], _f(k["strike"]))) if m else iv
        if not sig and T > 0:
            ref = entry if not is_open else mark
            sig = b76_iv(ref, F, k["strike"], T, rate, k["option_type"]) if ref else None
            if sig:
                leg["notes"].append("IV %.2f%% implied from the premium %s" % (sig, _p(ref)))
        if not sig and T > 0 and m.get("atm_iv"):
            sig = m["atm_iv"]
            leg["notes"].append("IV not in the chain for this strike: the at-the-money IV %.2f%% is used" % sig)
        if not sig and T > 0:
            return None, "no IV for %s: write a fresh chain (trader-options --write)" % leg["label"]
        leg["iv"] = sig or 0.0
    else:
        leg["iv"] = 0.0
    leg["mark"] = leg_value(leg, 0.0, 0.0, 0.0)
    leg["entry"] = entry if entry is not None else (mark if mark is not None else leg["mark"])
    return leg, None


def leg_value(leg, rel, dv, h):
    """Per-unit value of one leg after the underlying moves by rel (0.01 = +1%), IV by dv vol points and
    h years pass. Every expiry's forward moves in proportion."""
    F = leg["F"] * (1.0 + rel)
    if leg["instrument"] == "FUT":
        return F
    T = leg["T"] - h
    if T <= 1e-9:
        return max(0.0, F - leg["strike"]) if leg["option_type"] == "CE" else max(0.0, leg["strike"] - F)
    return b76_price(F, leg["strike"], T, leg["rate"], max(0.005, (leg["iv"] + dv) / 100.0), leg["option_type"])


def leg_charges(leg, value, settles, cfg, on):
    """Charges of one leg from here to `value`: a proposal pays its round trip (entry and exit), an open
    position only its exit. settles=True: the leg reaches expiry (a long in the money is exercised and
    pays STT on its intrinsic value; anything else expires with no closing order)."""
    seg = "FUT" if leg["instrument"] == "FUT" else "OPT"
    q, long = abs(leg["qty"]), leg["qty"] > 0
    entry = 0.0 if leg["open"] else leg["entry"]
    if seg == "FUT":
        return round_trip_charges(entry, value, q, cfg, seg, on)["total"] if long else \
            round_trip_charges(value, entry, q, cfg, seg, on)["total"]
    if long:
        if settles:
            return round_trip_charges(entry, value, q, cfg, seg, on, value > 0)["total"]
        return round_trip_charges(entry, value, q, cfg, seg, on)["total"]
    if settles:
        return round_trip_charges(0, entry, q, cfg, seg, on)["total"]
    return round_trip_charges(value, entry, q, cfg, seg, on)["total"]


def book_pnl(book, rel_of, dv, h, cfg, on):
    """P&L after charges of the whole book in one scenario. rel_of: {underlying: move}."""
    total = 0.0
    for l in book:
        v = leg_value(l, rel_of.get(l["underlying"], 0.0), dv, h)
        base = l["mark"] if l["open"] else l["entry"]
        total += (v - base) * l["qty"] - leg_charges(l, v, l["T"] - h <= 1e-9, cfg, on)
    return total


def book_greeks(book):
    """Net Greeks per underlying and in total: delta in units and lots (and ₹ per 1% move), gamma per point,
    vega ₹ per vol point, theta ₹ per calendar day."""
    per = {}
    for l in book:
        g = per.setdefault(l["underlying"], {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                                             "lot_size": l.get("lot_size"), "forward": l["F"]})
        if l["instrument"] == "FUT":
            g["delta"] += l["qty"]
            continue
        x = b76_greeks(l["F"], l["strike"], l["T"], l["rate"], l["iv"] / 100.0, l["option_type"])
        for k in ("delta", "gamma", "vega", "theta"):
            g[k] += x[k] * l["qty"]
        g["lot_size"] = g["lot_size"] or l.get("lot_size")
    out, tot = {}, {"vega": 0.0, "theta": 0.0, "delta_rupees_1pct": 0.0}
    for u, g in sorted(per.items()):
        lot = g["lot_size"] or 1
        out[u] = {"delta": round(g["delta"], 2), "delta_lots": round(g["delta"] / lot, 3),
                  "delta_rupees_1pct": round_paise(g["delta"] * g["forward"] / 100.0),
                  "gamma": round(g["gamma"], 5), "gamma_lots_1pct": round(g["gamma"] * g["forward"] / 100.0 / lot, 3),
                  "vega": round_paise(g["vega"]), "theta": round_paise(g["theta"]), "lot_size": g["lot_size"],
                  "forward": round_paise(g["forward"])}
        tot["vega"] += g["vega"]
        tot["theta"] += g["theta"]
        tot["delta_rupees_1pct"] += g["delta"] * g["forward"] / 100.0
    return {"by_underlying": out, "total": {k: round_paise(v) for k, v in tot.items()}}


def underlying_sigma(book, mk, days):
    """{underlying: one-sigma move over `days` sessions, as a fraction}: at-the-money IV (else the legs'
    average IV) x sqrt(days / 252)."""
    import math
    out = {}
    for u in sorted({l["underlying"] for l in book}):
        ivs = [m.get("atm_iv") for (uu, _), m in sorted(mk.items()) if uu == u and m.get("atm_iv")]
        legs = [l["iv"] for l in book if l["underlying"] == u and l["instrument"] == "OPT" and l["iv"]]
        iv = ivs[0] if ivs else (sum(legs) / len(legs) if legs else 15.0)
        out[u] = iv / 100.0 * math.sqrt(days / 252.0)
    return out


def scenario_grid(book, now, cfg, mk=None):
    """The book's P&L after charges on a grid: every underlying moves the same number of its own sigmas
    (one session of at-the-money IV unless scenario_horizon_days says otherwise) at once, times IV shifts in
    vol points; one session of time passes. Returns rows, the worst cell and the sigma of each underlying."""
    c = cfg
    days = float(c.get("scenario_horizon_days") or 1)
    sig = sorted({abs(float(x)) for x in (c.get("scenario_sigmas") or [1, 2, 3])})
    moves = [-s for s in reversed(sig)] + [0.0] + sig
    shifts = sorted({float(x) for x in (c.get("scenario_iv_shifts") or [5, 10])} |
                    {-float(x) for x in (c.get("scenario_iv_shifts") or [5, 10])} | {0.0})
    sigma = underlying_sigma(book, mk or {}, days)
    h = days / 365.0
    on = now.date().isoformat()
    rows, worst, best = [], None, None
    for k in moves:
        rel = {u: k * s for u, s in sigma.items()}
        pnl = []
        for dv in shifts:
            v = round_paise(book_pnl(book, rel, dv, h, c, on))
            pnl.append(v)
            cell = {"pnl": v, "sigma": k, "iv_shift": dv, "move_pct": {u: round(100 * x, 2) for u, x in rel.items()}}
            if worst is None or v < worst["pnl"]:
                worst = cell
            if best is None or v > best["pnl"]:
                best = cell
        rows.append({"sigma": k, "move_pct": {u: round(100 * x, 2) for u, x in rel.items()}, "pnl": pnl})
    return {"horizon_days": days, "sigmas": moves, "iv_shifts": shifts,
            "sigma_pct": {u: round(100 * s, 3) for u, s in sigma.items()},
            "forward": {u: round_paise(next(l["F"] for l in book if l["underlying"] == u)) for u in sigma},
            "rows": rows, "worst": worst, "best": best,
            "method": "Black-76 on each leg's forward and IV; every underlying moves k of its own sigmas "
                      "(at-the-money IV x sqrt(%s/252)) at once, IV shifts in vol points, %s day(s) pass; P&L "
                      "after charges (a proposal pays its entry and exit, an open position its exit)" % (
                          _num(days), _num(days))}


def scenario_words(cell, limit=None):
    if not cell:
        return ""
    moves = ", ".join("%s %+.2f%%" % (u, x) for u, x in sorted(cell["move_pct"].items()))
    return "spot %+g sigma (%s) with IV %+g vol: %s after charges%s" % (
        cell["sigma"], moves or "no move", cell["iv_shift"], inr(cell["pnl"]),
        (", over your %s max scenario loss" % inr(limit)) if limit else "")


# ---------------------------------------------------------------- one basket as one position

SPAN_STYLE = {   # the same assumptions as trader-strategy's SPAN-style estimate (NSE SPAN file of 30-Oct-2026)
    "index": {"scan_pct": 9.3, "vol_scan": 4.0, "elm_pct": 2.0, "error_pct": 15},
    "stock": {"scan_pct": 20.0, "vol_scan": 10.0, "elm_pct": 3.5, "error_pct": 35},
}
EXPIRY_DAY_ELM_PCT = 2.0     # SEBI 01-Oct-2024: 2% more ELM on short index options on their expiry day


def defined_risk(legs):
    """True when every short option is covered by a long of the same type, same or later expiry, in
    quantity, and there is no future (trader-strategy's rule)."""
    if any(l["instrument"] == "FUT" for l in legs):
        return False
    for kind in ("CE", "PE"):
        for s in [l for l in legs if l["option_type"] == kind and l["qty"] < 0]:
            cover = sum(l["qty"] for l in legs if l["option_type"] == kind and l["qty"] > 0 and l["expiry"] >= s["expiry"])
            short = -sum(l["qty"] for l in legs if l["option_type"] == kind and l["qty"] < 0
                         and l["expiry"] <= s["expiry"])
            if cover < short:
                return False
    return True


def expiry_extremes(legs, cfg, on):
    """Max loss of the basket held to its (near) expiry, after charges: (loss >= 0 or None if unlimited,
    at, exact). One expiry: exact on the piecewise-linear payoff. Two expiries: the later legs are valued
    by Black-76 at the near expiry, on a wide price grid, IV as now and 5 points lower (an estimate)."""
    near = min(l["expiry"] for l in legs)
    F0 = next(l["F"] for l in legs if l["expiry"] == near)
    if len({l["expiry"] for l in legs}) == 1:
        def pnl(x):
            tot = 0.0
            for l in legs:
                v = x if l["instrument"] == "FUT" else (max(0.0, x - l["strike"]) if l["option_type"] == "CE"
                                                        else max(0.0, l["strike"] - x))
                tot += (v - l["entry"]) * l["qty"] - leg_charges(l, v, True, cfg, on)
            return tot
        slope = sum(l["qty"] for l in legs if l["instrument"] == "FUT" or l["option_type"] == "CE")
        if slope < -1e-9:
            return None, None, True
        # the gross payoff is straight between strikes, so its extremes sit at a strike or at the edge of
        # the range; searched over the strikes +/- 3.5 standard deviations (trader-strategy's chart range):
        # far beyond it the STT on a deep in-the-money exercise keeps growing, which is not a real risk
        import math
        ivs = [l["iv"] for l in legs if l["instrument"] == "OPT" and l["iv"]]
        T = max(next(l["T"] for l in legs if l["expiry"] == near), 1.0 / 365)
        sd = (sum(ivs) / len(ivs) if ivs else 15.0) / 100.0 * math.sqrt(T)
        strikes = [l["strike"] for l in legs if l["strike"]]
        lo_x = max(0.0, min(strikes + [F0]) * (1.0 - 3.5 * sd))
        hi_x = max(strikes + [F0]) * (1.0 + 3.5 * sd)
        pts = sorted({lo_x, F0, hi_x} | set(strikes))
        lo = min((pnl(x), x) for x in pts)
        return max(0.0, -lo[0]), lo[1], True
    if not defined_risk(legs):
        return None, None, False
    h = next(l["T"] for l in legs if l["expiry"] == near)
    worst, at = 0.0, None
    for i in range(241):
        rel = 0.5 * (3.0 ** (i / 240.0)) - 1.0           # 0.5 x to 1.5 x the forward
        for dv in (0.0, -5.0):
            v = book_pnl(legs, {legs[0]["underlying"]: rel}, dv, h, cfg, on)
            if v < worst:
                worst, at = v, F0 * (1 + rel)
    return -worst, at, False


def span_margin(legs, today, cfg=None):
    """A SPAN-style margin estimate for one underlying's legs, the same method as trader-strategy: the worst
    of 16 price and IV scenarios on the whole position + exposure (ELM) on every short option and future +
    2% more on index shorts expiring today + premium paid. Only bought options: the premium."""
    p = dict(SPAN_STYLE[legs[0]["kind"]])
    p.update({k: v for k, v in ((cfg or {}).get("span") or {}).items() if v is not None})
    paid = sum(l["entry"] * l["qty"] for l in legs if l["instrument"] == "OPT" and l["qty"] > 0)
    if not any(l["instrument"] == "FUT" or l["qty"] < 0 for l in legs):
        return {"method": "premium", "total": round_paise(paid), "high": round_paise(paid), "expiry_day_elm": 0.0,
                "scan_risk": 0.0, "exposure": 0.0, "premium_paid": round_paise(paid), "error_pct": 0}
    ps, vs = p["scan_pct"] / 100.0, p["vol_scan"]

    def gross(rel, dv):
        return sum((leg_value(l, rel, dv, 0.0) - l["entry"]) * l["qty"] for l in legs)
    base = gross(0.0, 0.0)
    worst = 0.0
    for m in (0.0, 1 / 3.0, -1 / 3.0, 2 / 3.0, -2 / 3.0, 1.0, -1.0):
        for dv in (vs, -vs):
            worst = max(worst, base - gross(m * ps, dv))
    for m in (3.0, -3.0):
        worst = max(worst, (base - gross(m * ps, 0.0)) * 0.30)
    exposure = sum(l["F"] * -l["qty"] * p["elm_pct"] / 100.0 for l in legs
                   if l["qty"] < 0 or l["instrument"] == "FUT") + \
        sum(l["F"] * l["qty"] * p["elm_pct"] / 100.0 for l in legs if l["instrument"] == "FUT" and l["qty"] > 0)
    extra = sum(l["F"] * -l["qty"] * EXPIRY_DAY_ELM_PCT / 100.0 for l in legs
                if l["kind"] == "index" and l["instrument"] == "OPT" and l["qty"] < 0 and l["expiry"] == today)
    total = worst + exposure + extra + paid
    return {"method": "span-style", "scan_risk": round_paise(worst), "exposure": round_paise(exposure),
            "expiry_day_elm": round_paise(extra), "premium_paid": round_paise(paid), "total": round_paise(total),
            "high": round_paise(total * (1 + p["error_pct"] / 100.0)), "error_pct": p["error_pct"]}


def strategy_matches(strategy, legs):
    """True when trader/strategy.json describes exactly these legs (side, type, strike, expiry, qty)."""
    if not isinstance(strategy, dict) or not strategy.get("legs") or not legs:
        return False
    if str(strategy.get("underlying") or "").upper() != legs[0]["underlying"]:
        return False

    def sig(side, inst, t, k, e, q):
        return (side, inst, t or None, round(_f(k) or 0, 2), str(e), int(round(abs(_f(q) or 0))))
    mine = sorted(sig("BUY" if l["qty"] > 0 else "SELL", l["instrument"], l["option_type"], l["strike"], l["expiry"],
                      l["qty"]) for l in legs)
    theirs = sorted(sig(str(l.get("side")).upper(), l.get("instrument"), l.get("option_type"), l.get("strike"),
                        l.get("expiry"), l.get("qty")) for l in strategy["legs"])
    return mine == theirs


def basket_margin(legs, today, cfg, strategy=None, upper=True):
    """(margin to hold against your cash, detail). trader-strategy's estimate when trader/strategy.json is
    this basket, else the same method here. Expiry day: 2% more ELM on index shorts expiring today (if the
    estimate was made on another day) and no calendar-spread benefit: each expiry margined on its own."""
    own = span_margin(legs, today, cfg)
    detail = {"method": own["method"], "source": "trader-risk (same SPAN-style method as trader-strategy)",
              "estimate": own["total"], "high": own["high"], "expiry_day_elm": own["expiry_day_elm"],
              "calendar_benefit": True, "error_pct": own["error_pct"]}
    if strategy_matches(strategy, legs) and isinstance(strategy.get("margin"), dict):
        m = strategy["margin"]
        span = m.get("span_style") or {}
        est, high = _f(m.get("required")) or own["total"], _f(m.get("high")) or _f(m.get("required")) or own["high"]
        add = own["expiry_day_elm"] - (_f(span.get("expiry_day_elm")) or 0.0)
        if add > PAISA:
            est, high = est + add, high + add
            detail["expiry_day_elm_added"] = round_paise(add)
        detail.update({"source": "trader-strategy (trader/strategy.json, %s)" % strategy.get("asof"),
                       "method": m.get("method") or own["method"], "estimate": round_paise(est),
                       "high": round_paise(high), "error_pct": m.get("error_pct", own["error_pct"])})
    exps = sorted({l["expiry"] for l in legs})
    if today in exps and len(exps) > 1:
        parts = [span_margin([l for l in legs if l["expiry"] == e], today, cfg) for e in exps]
        split = sum(x["total"] for x in parts)
        split_high = sum(x["high"] for x in parts)
        detail["calendar_benefit"] = False
        detail["without_calendar_benefit"] = round_paise(split)
        if split > detail["estimate"]:
            detail["estimate"], detail["high"] = round_paise(split), round_paise(max(split_high, detail["high"]))
    use = detail["high"] if upper else detail["estimate"]
    detail["used"] = round_paise(use)
    detail["used_is"] = "the upper bound (+%s%%)" % _num(detail["error_pct"]) if upper else "the estimate"
    return use, detail


def _risk_rule(orders):
    for o in orders:
        r = o.get("risk_rule")
        if isinstance(r, dict) and r:
            return r
    return None


def stop_rule_loss(legs, orders, rule, cfg, on):
    """(loss at the stop or adjustment rule after charges, words) or (None, why it does not cover the
    position). Rules: "risk_rule": {"max_loss": 4000} (exit or adjust when the basket is down that much),
    {"stop_underlying": {"above": 23400, "below": 22700}}, or an SL / SL-M stop_loss on every short leg
    and future."""
    entry_rt = sum(leg_charges(dict(l, open=False), l["entry"], False, cfg, on) for l in legs)
    if rule and _f(rule.get("max_loss")):
        m = _f(rule["max_loss"])
        return round_paise(m + entry_rt), "your risk_rule exits at a %s loss (+ %s charges)" % (inr(m), inr(entry_rt))
    if rule and isinstance(rule.get("stop_underlying"), dict):
        su = rule["stop_underlying"]
        F0 = legs[0]["F"]
        need_up = any(l["qty"] < 0 and (l["instrument"] == "FUT" or l["option_type"] == "CE") for l in legs)
        need_down = any((l["qty"] < 0 and l["option_type"] == "PE") or (l["instrument"] == "FUT" and l["qty"] > 0)
                        for l in legs)
        up, down = _f(su.get("above")), _f(su.get("below"))
        if (need_up and not up) or (need_down and not down):
            return None, ("stop_underlying needs %s: the basket loses without limit on that side" %
                          " and ".join(x for x, n in (("\"above\"", need_up and not up), ("\"below\"", need_down and not down))
                                       if n))
        worst = 0.0
        for lvl in (up, down):
            if lvl:
                rel = lvl / F0 - 1.0
                worst = min(worst, book_pnl(legs, {legs[0]["underlying"]: rel}, 5.0, 0.0, cfg, on))
        return round_paise(-worst), "your underlying stop (%s), valued now with IV 5 points higher" % ", ".join(
            "%s %s" % (k, _p(v)) for k, v in (("above", up), ("below", down)) if v)
    risky = [(l, o) for l, o in zip(legs, orders) if l["qty"] < 0 or l["instrument"] == "FUT"]
    if risky and all(isinstance(o.get("stop_loss"), dict) and _f(o["stop_loss"].get("trigger_price")) for _, o in risky):
        loss = 0.0
        for l, o in risky:
            t = _f(o["stop_loss"]["trigger_price"])
            if (l["qty"] < 0 and t <= l["entry"]) or (l["qty"] > 0 and t >= l["entry"]):
                return None, "the stop %s of %s is on the wrong side of its entry %s" % (_p(t), l["label"], _p(l["entry"]))
            loss += abs(t - l["entry"]) * abs(l["qty"])
        loss += sum(l["entry"] * l["qty"] for l in legs if l["qty"] > 0 and l["instrument"] == "OPT")
        return round_paise(loss + entry_rt), "the stop_loss legs on every short leg (a stop can fill worse)"
    return None, "no stop or adjustment rule"


# ---------------------------------------------------------------- behaviour: circuit-breakers, kill switch, order rate

SEBI_ORDER_RATE = 10        # orders a second per exchange per client, above which an algo must be registered
ORDER_RATE_HARD_CAP = 9     # this pack never goes above it, whatever the config says


def order_rate(c):
    """(orders a second this pack allows, warning or None): the config's max_orders_per_second (3), never
    above ORDER_RATE_HARD_CAP (below SEBI's 10 a second)."""
    want = _f(c.get("max_orders_per_second"))
    if want is None or want <= 0:
        return 3, None if want is None else "max_orders_per_second must be above 0: using 3"
    if want > ORDER_RATE_HARD_CAP:
        return ORDER_RATE_HARD_CAP, ("max_orders_per_second %s is over the hard cap of %d (SEBI registers algos "
                                     "above %d a second): using %d" % (_num(want), ORDER_RATE_HARD_CAP,
                                                                        SEBI_ORDER_RATE, ORDER_RATE_HARD_CAP))
    return int(want), None


def rate_gate(sent, now, per_second):
    """(ok, wait seconds): may one more order go now? sent: aware times of orders already sent. A sliding
    one-second window: at most per_second orders in any second."""
    window = [t for t in sent if t is not None and 0 <= (now - t).total_seconds() < 1.0]
    if len(window) < per_second:
        return True, 0.0
    oldest = min(window)
    return False, round(1.0 - (now - oldest).total_seconds(), 3)


def trades_today(trades, today):
    """Today's closed trades (fills with a realised P&L), oldest first: [{"at", "symbol", "pnl", "leg"}]."""
    out = []
    for t in ((trades or {}).get("trades") or []) if isinstance(trades, dict) else (trades or []):
        if not isinstance(t, dict) or rec_day(t) != today:
            continue
        out.append({"at": parse_time(t.get("at")), "symbol": str(t.get("underlying") or t.get("symbol") or "").upper(),
                    "pnl": _f(t.get("realised_pnl")), "leg": t.get("leg"), "side": t.get("side")})
    out.sort(key=lambda x: x["at"] or datetime.datetime.min.replace(tzinfo=IST))
    return out


def circuit_breakers(c, trades, today, now, capital):
    """The behavioural locks on new entries (exits are never blocked). SEBI's study of F&O traders found
    that the more often people trade, the more they lose; these slow you down after losses.

    Returns {"locks": [reasons for every entry], "revenge": {SYM: reason}, "state": {...}}."""
    rows = trades_today(trades, today)
    closed = [r for r in rows if r["pnl"] is not None]
    locks, revenge, state = [], {}, {}
    streak = 0
    for r in reversed(closed):
        if r["pnl"] < -PAISA:
            streak += 1
        else:
            break
    realised = round_paise(sum(r["pnl"] for r in closed))
    state.update({"closed_trades": len(closed), "losing_streak": streak, "realised_today": realised})
    n = int(_f(c.get("max_consecutive_losses")) or 0)
    if n and streak >= n:
        locks.append("breaker: %d losing trades in a row today (your limit is %d): no new entries until tomorrow"
                     % (streak, n))
    dd = _f(c.get("max_daily_drawdown_pct"))
    if dd and capital and realised < 0 and -realised >= capital * dd / 100.0 - PAISA:
        locks.append("breaker: down %s today after charges, %.2f%% of %s (your limit is %s%%): no new entries "
                     "until tomorrow" % (inr(-realised), -100.0 * realised / capital, inr(capital), _num(dd)))
    state["drawdown_limit"] = round_paise(capital * dd / 100.0) if dd and capital else None
    cool = _f(c.get("cooloff_after_stop_min"))
    stops = [r for r in rows if r["leg"] == "stop" and r["at"]]
    if cool and stops:
        last = stops[-1]
        until = last["at"] + datetime.timedelta(minutes=cool)
        state["cooloff_until"] = until.strftime("%H:%M")
        if now < until:
            locks.append("cooling-off: a stop filled at %s IST (%s); no new entries for %s minutes, until %s IST"
                         % (last["at"].strftime("%H:%M"), last["symbol"], _num(cool), until.strftime("%H:%M")))
    win = _f(c.get("revenge_window_min"))
    if win:
        for r in closed:
            if r["pnl"] < -PAISA and r["at"] and now < r["at"] + datetime.timedelta(minutes=win):
                revenge[r["symbol"]] = ("revenge: %s lost %s at %s IST; re-entering it within %s minutes of a loss "
                                        "is a revenge trade (until %s IST)" % (
                                            r["symbol"], inr(-r["pnl"]), r["at"].strftime("%H:%M"), _num(win),
                                            (r["at"] + datetime.timedelta(minutes=win)).strftime("%H:%M")))
    state["locked"] = bool(locks)
    return {"locks": locks, "revenge": revenge, "state": state}


KILL_WORD = "unkill"


def kill_active(kill, today):
    """The kill-switch record if it is on for today, else None."""
    if isinstance(kill, dict) and kill.get("active") and kill.get("date") == today:
        return kill
    return None


def kill_on(kill, now, reason=None):
    """The kill.json body after `check.py kill` at now."""
    doc = dict(kill) if isinstance(kill, dict) else {}
    log = list(doc.get("log") or [])
    rec = {"at": now.isoformat(timespec="seconds"), "action": "kill", "reason": reason or "no reason given"}
    log.append(rec)
    doc.update({"active": True, "date": now.date().isoformat(), "at": rec["at"], "reason": rec["reason"],
                "log": log[-50:]})
    return doc


def kill_off(kill, now, typed):
    """(kill.json body, None) after `check.py unkill`, or (None, why not). The typed confirmation must be
    the word unkill: a kill switch is not lifted by accident."""
    if not kill_active(kill, now.date().isoformat()):
        return None, "the kill switch is not on today: nothing to lift"
    if " ".join(str(typed or "").lower().split()) != KILL_WORD:
        return None, "not lifted: type the word %s to lift the kill switch (you typed %r)" % (KILL_WORD, typed or "")
    doc = dict(kill)
    log = list(doc.get("log") or [])
    log.append({"at": now.isoformat(timespec="seconds"), "action": "unkill", "typed": typed})
    doc.update({"active": False, "lifted_at": now.isoformat(timespec="seconds"), "log": log[-50:]})
    return doc, None

def revoke_entries(risk_doc, kill):
    """trader/state/risk.json with every recorded entry PASS turned into a BLOCK by the kill switch, so a
    verdict recorded before the kill cannot be confirmed after it. Exits keep their verdict. -> (doc, n)."""
    if not isinstance(risk_doc, dict) or not isinstance(risk_doc.get("results"), list):
        return risk_doc, 0
    doc, n = dict(risk_doc), 0
    why = "kill: the kill switch went on at %s IST (%s): no new entries today; exits still pass" % (
        str(kill.get("at", ""))[11:16], kill.get("reason") or "no reason given")
    rows = []
    for r in doc["results"]:
        if isinstance(r, dict) and r.get("verdict") == "PASS" and r.get("kind", "entry") == "entry":
            r = dict(r, verdict="BLOCK", reasons=list(r.get("reasons") or []) + [why])
            n += 1
        rows.append(r)
    doc["results"] = rows
    doc["pass"] = sum(1 for r in rows if isinstance(r, dict) and r.get("verdict") == "PASS")
    doc["block"] = sum(1 for r in rows if isinstance(r, dict) and r.get("verdict") == "BLOCK")
    doc["kill"] = {"active": True, "since": kill.get("at"), "reason": kill.get("reason")}
    return doc, n


def fno_exit_orders(portfolio, cfg=None):
    """Closing tickets for every open F&O position (the kill switch's --flatten): short legs first, so a
    hedge is never removed before the leg it covers. MARKET exits; through a live broker's API a market order
    needs market protection, which the check reports as a warning (an exit is never blocked)."""
    rows = []
    for p in fno_positions(portfolio, cfg):
        k = p["contract"]
        if not k:
            continue
        o = {"symbol": k["underlying"], "exchange": "NFO", "segment": k["instrument"],
             "instrument": ("OPTIDX" if k["kind"] == "index" else "OPTSTK") if k["instrument"] == "OPT"
             else ("FUTIDX" if k["kind"] == "index" else "FUTSTK"),
             "expiry": k["expiry"], "strike": k["strike"], "option_type": k["option_type"],
             "side": "BUY" if p["qty"] < 0 else "SELL", "qty": int(abs(p["qty"])), "product": "NRML",
             "order_type": "MARKET", "price": None, "validity": "DAY",
             "why": "kill switch: closes the open %s position (%+d)" % (contract_label(k), int(p["qty"]))}
        if p.get("lot_size"):
            o["lot_size"] = p["lot_size"]
        rows.append((0 if p["qty"] < 0 else 1, o))
    return [o for _, o in sorted(rows, key=lambda x: x[0])]

# ---------------------------------------------------------------- end of the F&O and behaviour rules


# ---------------------------------------------------------------- the check

def is_fno(o):
    sym = str(o.get("symbol", "")).strip().upper()
    exch = str(o.get("exchange") or "").strip().upper()
    product = str(o.get("product") or "").strip().upper()
    return bool(sym and FNO.match(sym)) or exch in FNO_EXCHANGES or product == "NRML" or bool(o.get("expiry")) \
        or bool(o.get("tradingsymbol") and FNO.match(str(o["tradingsymbol"]).upper()))


def fno_limits(c):
    """The F&O and portfolio limits in force: a missing (null) key takes its default, often derived from
    your loss limits; 0 switches a limit off."""
    ml, dl = _f(c.get("max_loss_per_trade")), _f(c.get("max_daily_loss"))

    def pick(key, derived):
        v = c.get(key)
        if v is None:
            return derived
        v = _f(v)
        return v if v else None
    return {"max_premium_per_trade": pick("max_premium_per_trade", ml),
            "max_premium_per_day": pick("max_premium_per_day", dl),
            "max_lots_per_underlying": pick("max_lots_per_underlying", 2),
            "max_net_delta_lots": pick("max_net_delta_lots", 2),
            "max_net_vega": pick("max_net_vega", round_paise(dl / 10.0) if dl else None),
            "max_net_gamma_lots_1pct": pick("max_net_gamma_lots_1pct", None),
            "max_theta_paid_per_day": pick("max_theta_paid_per_day", None),
            "max_scenario_loss": pick("max_scenario_loss", dl)}


def risk_rate(c):
    r = _f(c.get("risk_free_rate"))
    if r is None:
        return DEFAULT_RATE
    return r / 100.0 if r > 1 else r


def open_book(portfolio, now, mk, prices, c, lots=None):
    """(valued open F&O positions, [errors])."""
    book, errors = [], []
    rate = risk_rate(c)
    for p in fno_positions(portfolio, c):
        k = p["contract"]
        if k is None:
            errors.append("cannot read the open position %s (%s)" % (p["name"], p["error"]))
            continue
        if k["expiry"] < now.date().isoformat():
            continue
        lot = p["lot_size"] or lot_for(lots, k["underlying"], k["expiry"])
        leg, why = book_leg(k, p["qty"], None, now, mk, prices, rate, lot, p["iv"], p["forward"], True,
                            mark=p["ltp"] or p["avg_price"])
        if leg is None:
            errors.append("cannot value the open position %s: %s" % (contract_label(k), why))
            continue
        leg["avg_price"] = p["avg_price"]
        book.append(leg)
    return book, errors


def side_lots(legs_by_key):
    """{underlying: {"CE long", "CE short", "PE long", "PE short", "FUT"}: lots} from net qty per contract."""
    out = {}
    for key, (q, lot) in legs_by_key.items():
        u, inst, t = key[0], key[1], key[2]
        if not q:
            continue
        b = "FUT" if inst == "FUT" else "%s %s" % (t, "long" if q > 0 else "short")
        d = out.setdefault(u, {})
        d[b] = d.get(b, 0.0) + (abs(q) / float(lot or 1) if inst == "OPT" else q / float(lot or 1))
    return {u: {b: abs(v) for b, v in d.items()} for u, d in out.items()}


def portfolio_risk(book, now, c, mk=None, errors=None):
    """Greeks and the scenario grid of a book, with the limits."""
    lim = fno_limits(c)
    if not book:
        return {"legs": 0, "greeks": None, "scenarios": None, "limits": lim, "errors": list(errors or [])}
    return {"legs": len(book), "greeks": book_greeks(book), "scenarios": scenario_grid(book, now, c, mk),
            "limits": lim, "errors": list(errors or []),
            "positions": [{"label": l["label"], "qty": l["qty"], "lots": round(abs(l["qty"]) / float(l["lot_size"]), 3)
                           if l.get("lot_size") else None, "open": l["open"], "iv": round(l["iv"], 2),
                           "forward": round_paise(l["F"]), "mark": round_paise(l["mark"]),
                           "days_left": round(l["T"] * 365.0, 2), "source": l.get("source")} for l in book]}


def greek_breaches(before, after, lim):
    """Reasons for Greek limits that `after` breaks AND makes worse than `before` (a hedge always passes)."""
    out = []
    b_by = (before or {}).get("by_underlying") or {}
    for u, g in sorted(((after or {}).get("by_underlying") or {}).items()):
        b = b_by.get(u) or {}
        x = lim.get("max_net_delta_lots")
        if x and abs(g["delta_lots"]) > x + 1e-9 and abs(g["delta_lots"]) > abs(b.get("delta_lots") or 0) + 1e-9:
            out.append("greeks: %s net delta would be %+.2f lots (%s per 1%% move), over your %s-lot limit"
                       % (u, g["delta_lots"], inr(g["delta_rupees_1pct"]), _num(x)))
        x = lim.get("max_net_gamma_lots_1pct")
        if x and abs(g["gamma_lots_1pct"]) > x + 1e-9 and \
                abs(g["gamma_lots_1pct"]) > abs(b.get("gamma_lots_1pct") or 0) + 1e-9:
            out.append("greeks: %s gamma would move the delta %+.2f lots per 1%% move, over your %s"
                       % (u, g["gamma_lots_1pct"], _num(x)))
    ta, tb = (after or {}).get("total") or {}, (before or {}).get("total") or {}
    x = lim.get("max_net_vega")
    if x and abs(ta.get("vega", 0)) > x + PAISA and abs(ta.get("vega", 0)) > abs(tb.get("vega", 0)) + PAISA:
        out.append("greeks: net vega would be %s per vol point (a 10-point IV move = %s), over your %s"
                   % (inr(ta["vega"]), inr(abs(ta["vega"]) * 10), inr(x)))
    x = lim.get("max_theta_paid_per_day")
    if x and ta.get("theta", 0) < -x - PAISA and ta.get("theta", 0) < tb.get("theta", 0) - PAISA:
        out.append("greeks: the book would pay %s a day in time decay (theta), over your %s"
                   % (inr(-ta["theta"]), inr(x)))
    return out


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None, extra=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...], ...}.

    now          aware datetime (default: the real clock); its IST date is "today"
    price_times  {SYM: aware time of that price}; None = every price counts as fresh
    extra        the F&O and behaviour inputs, all optional: {"basket": orders.json's basket flag (every F&O
                 order in the file is one position), "strategy": trader/strategy.json, "options":
                 trader/options.json (or a list), "trades": trader/state/trades.json, "kill":
                 trader/state/kill.json, "ban": set of banned underlyings today or None (unknown), "ban_note",
                 "lots": parse_lots() output or None, "lots_note"}"""
    extra = extra or {}
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
    groups_today = {(r.get("basket_id") or (r.get("order") or {}).get("basket_id") or r.get("fingerprint") or id(r))
                    for r in entries_today}
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in the daily "
                            "loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = set()
    premium_today = 0.0
    for r in done_today:
        o = r.get("order") if isinstance(r.get("order"), dict) else {}
        if o and is_fno(o):
            k, _ = contract_of(o, c)
            if k:
                placed_keys.add((contract_key(k), str(r.get("side", "")).upper()))
                if k["instrument"] == "OPT" and str(r.get("side", "")).upper() == "BUY":
                    premium_today += (_f(o.get("price")) or 0.0) * (_f(r.get("qty")) or 0.0)
        else:
            placed_keys.add((str(r.get("symbol", "")).upper(), str(r.get("side", "")).upper()))
    win_reasons, win_warns = window_check(now, c)
    fno_list = c["_fno"] if "_fno" in c else load_fno_stocks(c)[0]
    windows = {}

    def window_for(sym, seg=None):
        seg = seg or (segment_of(sym, fno_list) if sym else "EQ")
        if seg not in windows:
            windows[seg] = window_check(now, c, seg)[0]
        return windows[seg]
    seen, results = set(), []
    run = {"entries": 0, "loss": 0.0, "premium": 0.0, "book": [], "groups": 0}
    live_broker = not c.get("paper", True) or str(c.get("broker") or "paper").lower() != "paper"
    if live_broker:
        top_warn.append("LIVE account (\"paper\": false): these orders would use real money")
    top_warn.extend(win_warns)

    # behaviour, the kill switch and the order rate: locks on new entries, never on exits
    breakers = circuit_breakers(c, extra.get("trades"), today, now, capital or acct["cash"])
    kill = kill_active(extra.get("kill"), today)
    locks = []
    if kill:
        locks.append("kill: the kill switch is on since %s IST (%s): no new entries today; exits still pass"
                     % (str(kill.get("at", ""))[11:16], kill.get("reason") or "no reason given"))
    locks.extend(breakers["locks"])
    per_second, rate_warn = order_rate(c)
    if rate_warn:
        top_warn.append(rate_warn)
    sent = [parse_time(r.get("at")) for r in done_today]
    rate_ok, rate_wait = rate_gate(sent, now, per_second)
    rate_reason = None if rate_ok else ("rate: %d orders went to the broker in the last second (limit %d a second, "
                                        "below SEBI's %d): wait %.1f s" % (per_second, per_second, SEBI_ORDER_RATE,
                                                                            rate_wait))

    def api_rule(o, label):
        """A live broker takes LIMIT orders, or MARKET / SL-M with market protection (SEBI's API rules)."""
        if not live_broker:
            return []
        out = []
        otype = str(o.get("order_type") or "MARKET").upper()
        prot = _f(o.get("market_protection"))
        if otype in ("MARKET", "SL-M") and not (prot and prot > 0):
            out.append("api: a %s order through a live broker API needs market protection (\"market_protection\": "
                       "a percent above 0) or a LIMIT price (%s)" % (otype, label))
        sl = o.get("stop_loss") if isinstance(o.get("stop_loss"), dict) else None
        if sl and str(sl.get("order_type", "")).upper() == "SL-M" and not (_f(sl.get("market_protection")) or prot):
            out.append("api: the SL-M stop_loss needs market protection through a live broker API, or use an SL stop "
                       "with a limit price (%s)" % label)
        return out

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

    # ------------------------------------------------ F&O baskets: every leg of one basket is one position
    allow_fno = bool(c.get("allow_fno"))
    mk = market_inputs(extra.get("strategy"), extra.get("options"))
    lots = extra.get("lots")
    ban = extra.get("ban")
    lim = fno_limits(c)
    rate = risk_rate(c)
    on = today
    holdings_book, book_errors = ([], [])
    open_keys = {}
    if allow_fno:
        holdings_book, book_errors = open_book(portfolio, now, mk, prices, c, lots)
        for p in fno_positions(portfolio, c):
            if p["contract"]:
                key = contract_key(p["contract"])
                q0, l0 = open_keys.get(key, (0.0, None))
                open_keys[key] = (q0 + p["qty"], p["lot_size"] or l0 or lot_for(lots, p["contract"]["underlying"],
                                                                               p["contract"]["expiry"]))
        if fno_positions(portfolio, c) and not (portfolio or {}).get("margin_blocked") and \
                not (portfolio or {}).get("margin_used"):
            top_warn.append("portfolio.json has open F&O positions but no margin_blocked: their margin is not "
                            "counted against your cash")
    open_margin = _f((portfolio or {}).get("margin_blocked")) or _f((portfolio or {}).get("margin_used")) or 0.0
    groups, group_of, done = {}, {}, {}
    for j, o in enumerate(orders):
        if allow_fno and isinstance(o, dict) and is_fno(o):
            gid = "file" if extra.get("basket") else (o.get("basket_id") or ("one", j))
            groups.setdefault(gid, []).append(j)
            group_of[j] = gid

    def basket(idxs):
        members = [(j, orders[j]) for j in idxs]
        legs, per = [], {}
        for j, o in members:
            R = {"reasons": [], "warns": [], "info": []}
            per[j] = R
            side = str(o.get("side", "")).strip().upper()
            otype = str(o.get("order_type") or "MARKET").strip().upper()
            validity = str(o.get("validity") or "DAY").strip().upper()
            product = str(o.get("product") or "NRML").strip().upper()
            exch = str(o.get("exchange") or "NFO").strip().upper()
            q = _f(o.get("qty"))
            if side not in ("BUY", "SELL"):
                R["reasons"].append("format: side must be BUY or SELL, got %r" % o.get("side"))
            if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
                R["reasons"].append("format: qty must be a whole number of units above 0, got %r" % o.get("qty"))
                q = None
            if otype not in ORDER_TYPES:
                R["reasons"].append("format: unknown order_type %r (MARKET, LIMIT, SL, SL-M)" % otype)
            if validity not in VALIDITY:
                R["reasons"].append("format: unknown validity %r (DAY or IOC)" % validity)
            if otype in ("LIMIT", "SL") and (_f(o.get("price")) or 0) <= 0:
                R["reasons"].append("format: a %s order needs a positive price" % otype)
            if otype in ("SL", "SL-M") and (_f(o.get("trigger_price")) or 0) <= 0:
                R["reasons"].append("format: a %s order needs a positive trigger_price" % otype)
            if exch != "NFO":
                R["reasons"].append("exchange: %s; this pack trades NSE futures and options on NFO only" % exch)
            if product not in ("NRML", "MIS"):
                R["reasons"].append("product: %s is not NRML (carry forward) or MIS (intraday) for F&O" % product)
            k, why = contract_of(o, c)
            if why:
                R["reasons"].append(why)
            tick = float(c["tick_size"]) if c.get("tick_size") not in (None, "", "auto") else OPTION_TICK
            for name, v in (("price", o.get("price") if otype in ("LIMIT", "SL") else None),
                            ("trigger_price", o.get("trigger_price")),
                            ("stop_loss trigger", (o.get("stop_loss") or {}).get("trigger_price")
                             if isinstance(o.get("stop_loss"), dict) else None)):
                fv = _f(v)
                if fv and fv > 0 and not on_tick(fv, tick):
                    R["reasons"].append("tick: %s %s is not a multiple of the %s F&O tick" % (name, v, _p(tick)))
            lot = None
            if k:
                if not TICKER.match(k["underlying"]):
                    R["reasons"].append("ticker: %r is not an NSE underlying" % k["underlying"])
                if k["expiry"] < today or (k["expiry"] == today and now.time() >= EXPIRY_TIME):
                    R["reasons"].append("expiry: %s expired on %s" % (contract_label(k), k["expiry"]))
                file_lot = lot_for(lots, k["underlying"], k["expiry"])
                own = int(_f(o.get("lot_size")) or 0) or None
                lot = file_lot or own
                if file_lot and own and file_lot != own:
                    R["reasons"].append("lot: the order says a lot of %d; NSE's fo_mktlots.csv says %d for %s %s"
                                        % (own, file_lot, k["underlying"], k["expiry"][:7]))
                if lot is None:
                    R["reasons"].append("lot: no lot size for %s %s (NSE's fo_mktlots.csv: check.py refresh)"
                                        % (k["underlying"], k["expiry"][:7]))
                elif q is not None and int(round(q)) % lot:
                    R["reasons"].append("lot: qty %d is not a multiple of the %s lot of %d (%s lots)"
                                        % (int(q), k["underlying"], lot, _num(q / float(lot))))
                elif q is not None and _f(o.get("lots")) and int(_f(o["lots"])) * lot != int(round(q)):
                    R["reasons"].append("lot: %s lots x %d is not the qty %d" % (_num(o["lots"]), lot, int(q)))
                if file_lot is None and own:
                    R["warns"].append("lot size %d is the order's own; NSE's fo_mktlots.csv was not available (%s)"
                                      % (own, extra.get("lots_note") or "check.py refresh"))
            signed = (q if side == "BUY" else -q) if (q and side in ("BUY", "SELL")) else None
            kind = "entry"
            if k and signed:
                held = open_keys.get(contract_key(k), (0.0, None))[0]
                if held and held * signed < 0 and abs(signed) <= abs(held) + 1e-9:
                    kind = "exit"
            entry = _f(o.get("price")) if otype in ("LIMIT", "SL") else None
            leg = None
            if k and signed:
                leg, why = book_leg(k, signed, entry, now, mk, prices, rate, lot, _f(o.get("iv")), _f(o.get("forward")))
                if leg is None:
                    R["reasons"].append("price: " + why)
                else:
                    if entry is None:
                        R["warns"].append("a %s order: valued at the model price %s (a market order can fill worse)"
                                          % (otype, _p(leg["entry"])))
                    R["warns"].extend(leg["notes"])
            key = (contract_key(k), side) if k else None
            if key and key in seen:
                R.setdefault("later", []).append("duplicate: a second %s %s in this batch" % (side, contract_label(k)))
            if key and key in placed_keys:
                R.setdefault("later", []).append("duplicate: a %s %s was already placed today"
                                                 % (side, contract_label(k)))
            R.setdefault("later", []).extend(api_rule(o, "leg %s" % (o.get("leg") or j + 1)))
            legs.append({"j": j, "order": o, "contract": k, "leg": leg, "kind": kind, "qty": q, "side": side,
                         "product": product, "lot": lot, "entry": entry})
        entry_legs = [x for x in legs if x["kind"] == "entry"]
        kind = "entry" if entry_legs else "exit"
        bl = [x["leg"] for x in entry_legs if x["leg"]]
        valued = len(bl) == len(entry_legs)
        later, info = [], []
        warns = ["F&O order: derivatives can lose more than you put in; the margin and scenario numbers are "
                 "estimates, so check your broker's basket margin too"]
        old_chain = sorted({str(l["asof"])[:16].replace("T", " ") for l in bl
                            if l.get("asof") and str(l["asof"])[:10] != today})
        if old_chain:
            warns.append("priced from a chain of %s, not today: refresh it (trader-options --write, trader-strategy "
                         "--write) before trading" % ", ".join(old_chain))
        summary = {"id": fingerprint({"basket": [fingerprint(orders[j]) for j in idxs]}), "legs": [j + 1 for j in idxs],
                   "kind": kind}
        unders = sorted({x["contract"]["underlying"] for x in legs if x["contract"]})
        u = unders[0] if unders else "?"
        if len(unders) > 1:
            later.append("basket: %s are different underlyings; one basket is one underlying" % ", ".join(unders))
        later.extend(locks)
        if u in breakers["revenge"]:
            rv = breakers["revenge"][u]
            (later if str(c.get("revenge_trade") or "block").lower() == "block" else warns).append(rv)
        if rate_reason:
            later.append(rate_reason)
        later.extend(window_for(u, "DERIV"))
        if unders and unders[0] not in INDICES:
            if ban is None:
                if c.get("require_ban_list", True):
                    later.append("ban: NSE's F&O ban list for %s is not known (%s); a stock in the ban takes no new "
                                 "positions. check.py refresh fetches it" % (today, extra.get("ban_note") or "no file"))
                else:
                    warns.append("NSE's F&O ban list for %s is not known: check %s is not in it" % (today, u))
            elif u in ban:
                later.append("ban: %s is in NSE's F&O ban for %s (open interest over 95%% of its market-wide limit): "
                             "no new positions, only closing ones" % (u, today))
        cap_t = int(c.get("max_trades_per_day") or 0)
        if cap_t and len(groups_today) + run["entries"] >= cap_t:
            later.append("trades: %d entries placed today plus %d passed earlier in this batch; your limit is %d a day "
                         "(a basket counts as one)" % (len(groups_today), run["entries"], cap_t))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far + len(idxs) > cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch plus %d legs here; your "
                         "limit is %d orders a day" % (count_today, passed_so_far, len(idxs), cap_n))
        loss = None
        paid = sum(l["entry"] * l["qty"] for l in bl if l["instrument"] == "OPT" and l["qty"] > 0)
        received = sum(-l["entry"] * l["qty"] for l in bl if l["instrument"] == "OPT" and l["qty"] < 0)
        debit = max(0.0, paid - received)       # the premium budget counts the net premium paid
        margin_use, margin = None, None
        if bl and valued and len(unders) == 1:
            defined = defined_risk(bl)
            maxloss, at, exact = expiry_extremes(bl, c, on)
            shorts = any(l["qty"] < 0 for l in bl)
            rule = _risk_rule([x["order"] for x in entry_legs])
            summary.update({"defined_risk": defined, "max_loss_at_expiry": round_paise(maxloss)
                            if maxloss is not None else None, "unlimited_loss": maxloss is None,
                            "max_loss_at": round_paise(at) if at else None, "exact": exact})
            how = None
            if defined and maxloss is not None:
                if c.get("defined_risk_counts_as_stop", True):
                    loss, how = round_paise(maxloss), "the most it can lose at expiry, charges included"
                else:
                    loss, how = stop_rule_loss(bl, [x["order"] for x in entry_legs], rule, c, on)
                    if loss is None:
                        later.append("stop: %s (\"defined_risk_counts_as_stop\": false, so a defined-risk basket "
                                     "needs a risk_rule too)" % how)
            else:
                naked = shorts
                if naked and not c.get("allow_naked_short"):
                    later.append("naked: the %s basket has short legs not covered by bought ones (a loss %s); naked "
                                 "shorts are off (\"allow_naked_short\": false)" % (
                                     u, "without limit" if maxloss is None else "far past the premium"))
                loss, how = stop_rule_loss(bl, [x["order"] for x in entry_legs], rule, c, on)
                if loss is None:
                    if naked or c.get("require_stop", True):
                        later.append("stop: the %s basket is not defined-risk and has %s. Add \"risk_rule\": "
                                     "{\"max_loss\": RUPEES} or {\"stop_underlying\": {\"above\": X, \"below\": Y}} "
                                     "to every leg, or an SL-M stop_loss on each short leg" % (u, how))
                    else:
                        warns.append("no stop: the loss is not capped")
                        loss = round_paise(maxloss) if maxloss is not None else None
                elif maxloss is not None:
                    loss = min(loss, round_paise(maxloss))
            summary.update({"loss_used": loss, "loss_from": how})
            cap = _f(c.get("max_loss_per_trade"))
            if cap and loss is not None and loss > cap + PAISA:
                later.append("loss: the %s basket can lose %s (%s), over your %s max loss per trade"
                             % (u, inr(loss), how, inr(cap)))
            daily = _f(c.get("max_daily_loss"))
            if daily and loss is not None:
                used = loss_today + run["loss"]
                if used + loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this basket = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(loss), inr(used + loss), inr(daily)))
            x = lim["max_premium_per_trade"]
            if x and debit > x + PAISA:
                later.append("premium: %s net premium paid for this basket (%s paid, %s received), over your %s per "
                             "trade (max_premium_per_trade)" % (inr(debit), inr(paid), inr(received), inr(x)))
            x = lim["max_premium_per_day"]
            if x and debit and premium_today + run["premium"] + debit > x + PAISA:
                later.append("premium: %s paid today plus %s here = %s, over your %s a day (max_premium_per_day)"
                             % (inr(premium_today + run["premium"]), inr(debit),
                                inr(premium_today + run["premium"] + debit), inr(x)))
            # lots per underlying: open + passed in this batch + this basket
            net = {}
            for key, (q0, l0) in open_keys.items():
                net[key] = (q0, l0)
            for l in run["book"] + [x["leg"] for x in legs if x["leg"]]:
                key = contract_key(l["contract"])
                q0, l0 = net.get(key, (0.0, None))
                net[key] = (q0 + l["qty"], l0 or l["lot_size"])
            sl = side_lots(net).get(u) or {}
            x = lim["max_lots_per_underlying"]
            worst_side = max(sl.items(), key=lambda kv: kv[1]) if sl else None
            summary["lots_after"] = {b: round(v, 3) for b, v in sl.items()}
            if x and worst_side and worst_side[1] > x + 1e-9:
                later.append("lots: %s would hold %s lots of %s (open + this batch), over your %s lots per underlying"
                             % (u, _num(round(worst_side[1], 3)), worst_side[0], _num(x)))
            # margin against the cash left after the buffer
            margin_use, margin = basket_margin(bl, today, c, extra.get("strategy"),
                                               bool(c.get("margin_use_upper_bound", True)))
            base = capital or (acct["cash"] + sum(acct["values"].values()))
            buffer_rs = base * float(c.get("cash_buffer_pct") or 0) / 100.0
            avail = acct["cash"] - open_margin - buffer_rs
            margin.update({"available": round_paise(avail), "cash": round_paise(acct["cash"]),
                           "open_margin": round_paise(open_margin), "buffer": round_paise(buffer_rs)})
            summary["margin"] = margin
            if margin_use > avail + PAISA:
                later.append("margin: the basket needs about %s (%s, %s) but only %s is free after your %s%% cash "
                             "buffer%s" % (inr(margin_use), margin["used_is"], margin["source"], inr(max(avail, 0)),
                                           _num(c.get("cash_buffer_pct") or 0),
                                           (" and %s already blocked" % inr(open_margin)) if open_margin else ""))
            if today in [l["expiry"] for l in bl]:
                warns.append("expiry day for %s: %s%s" % (
                    ", ".join(sorted({contract_label(l["contract"]) for l in bl if l["expiry"] == today})),
                    "2%% more margin (ELM) on index shorts expiring today (%s)" % inr(margin["expiry_day_elm"])
                    if margin.get("expiry_day_elm") else "gamma is extreme and Greeks are unreliable in the last hours",
                    "; no calendar-spread benefit: each expiry is margined on its own (%s)"
                    % inr(margin["without_calendar_benefit"]) if margin.get("calendar_benefit") is False else ""))
        elif entry_legs and not valued:
            later.append("price: some legs could not be valued, so the basket's loss, margin and scenarios are unknown")
        # the whole book: Greeks and the scenario grid, before and after this basket
        if book_errors:
            later.append("scenario: %s; the portfolio's risk is unknown, so no new F&O entries" % "; ".join(book_errors))
        all_new = [x["leg"] for x in legs if x["leg"]]
        if all_new and valued and len(unders) == 1:
            before = holdings_book + run["book"]
            after = before + all_new
            gb = book_greeks(before) if before else None
            ga = book_greeks(after)
            later.extend(greek_breaches(gb, ga, lim))
            grid_b = scenario_grid(before, now, c, mk) if before else None
            grid_a = scenario_grid(after, now, c, mk)
            wa, wb = grid_a["worst"], (grid_b or {}).get("worst")
            x = lim["max_scenario_loss"]
            if x and wa and wa["pnl"] < -x - PAISA and (wb is None or wa["pnl"] < wb["pnl"] - PAISA):
                later.append("scenario: %s" % scenario_words(wa, x))
            summary.update({"greeks_after": ga, "worst_scenario_before": wb, "worst_scenario_after": wa})
            info.append("portfolio after this basket: %s; worst scenario %s" % (
                ", ".join("%s delta %+.2f lots, vega %s, theta %s/day" % (uu, g["delta_lots"], inr(g["vega"]),
                                                                           inr(g["theta"]))
                          for uu, g in sorted(ga["by_underlying"].items())),
                scenario_words(wa)))
        if loss is not None:
            info.insert(0, "basket of %d leg%s on %s, %s: can lose %s (%s)%s" % (
                len(idxs), "" if len(idxs) == 1 else "s", u, "defined risk" if summary.get("defined_risk") else
                "NOT defined risk", inr(loss), summary.get("loss_from"),
                (" (limit %s)" % inr(c["max_loss_per_trade"])) if _f(c.get("max_loss_per_trade")) else ""))
        if margin:
            info.insert(1 if loss is not None else 0, "margin about %s (%s, %s), %s free; premium paid %s, received %s"
                        % (inr(margin_use), margin["used_is"], margin["source"], inr(max(margin["available"], 0)),
                           inr(paid), inr(received)))
        summary.update({"premium_paid": round_paise(paid), "premium_received": round_paise(received)})
        # verdicts: all or nothing
        own_block = [x["j"] for x in legs if per[x["j"]]["reasons"]]
        out = {}
        share = round_paise((loss or 0.0) / len(idxs)) if loss is not None else None
        for n, x in enumerate(legs):
            j, o, R = x["j"], x["order"], per[x["j"]]
            reasons, wl = list(R["reasons"]), list(R["warns"]) + list(warns)
            group = later + R.get("later", [])
            if kind == "entry":
                reasons.extend(group)
            else:
                wl.extend("exit, not blocked: " + w for w in group)
            others = [m + 1 for m in own_block if m != j]
            if others:
                reasons.append("basket: leg%s %s %s blocked, and a basket is placed whole or not at all" % (
                    "s" if len(others) > 1 else "", ", ".join("#%d" % m for m in others),
                    "are" if len(others) > 1 else "is"))
            lines = list(info) if n == 0 else ["leg %d of %d of the basket in #%d" % (n + 1, len(idxs), idxs[0] + 1)]
            l = x["leg"]
            ml = share
            if share is not None and n == len(legs) - 1:
                ml = round_paise(loss - share * (len(idxs) - 1))
            out[j] = {
                "index": j + 1, "symbol": (x["contract"] or {}).get("underlying") or str(o.get("symbol", "")).upper(),
                "side": x["side"], "kind": kind, "product": x["product"],
                "qty": int(x["qty"]) if x["qty"] is not None else o.get("qty"),
                "lots": round(x["qty"] / float(x["lot"]), 3) if x["qty"] and x["lot"] else None,
                "segment": (x["contract"] or {}).get("instrument"),
                "contract": contract_label(x["contract"]) if x["contract"] else None,
                "value": round_paise(l["entry"] * abs(l["qty"])) if l else None,
                "entry_ref": round_paise(l["entry"]) if l else None,
                "entry_ref_from": ("limit" if x["entry"] is not None else "model") if l else None,
                "stop": None, "target": None, "risk_per_share": None, "max_loss": ml, "charges": None,
                "max_loss_with_charges": ml, "reward_risk": None, "fingerprint": fingerprint(o),
                "basket_id": summary["id"], "basket": summary,
                "verdict": "BLOCK" if reasons else "PASS", "reasons": reasons, "warnings": wl, "info": lines}
        if all(r["verdict"] == "PASS" for r in out.values()):
            if kind == "entry":
                run["entries"] += 1
                run["loss"] += loss or 0.0
                run["premium"] += debit
                if margin_use:
                    acct["cash"] -= margin_use
            run["book"].extend(x["leg"] for x in legs if x["leg"])
            for x in legs:
                if x["contract"]:
                    seen.add((contract_key(x["contract"]), x["side"]))
        return out

    for i, o in enumerate(orders, 1):
        if (i - 1) in group_of:
            if (i - 1) not in done:
                done.update(basket(groups[group_of[i - 1]]))
            results.append(done[i - 1])
            continue
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
                used = loss_today + run["loss"]
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(groups_today) + run["entries"] >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch; your limit "
                             "is %d a day" % (len(groups_today), run["entries"], cap_t))
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
                            % (len(groups_today) + run["entries"], cap_t or "unlimited",
                               inr(loss_today + run["loss"]), inr(daily) if daily else "no limit on"))

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
        # the kill switch, the circuit-breakers, the order rate and a live broker's API rules
        later.extend(locks)
        if sym in breakers["revenge"]:
            (later if str(c.get("revenge_trade") or "block").lower() == "block" else warns).append(
                breakers["revenge"][sym])
        if rate_reason:
            later.append(rate_reason)
        later.extend(api_rule(o, sym or "?"))

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and q is not None:
            if kind == "entry":
                run["entries"] += 1
                run["loss"] += max_loss or 0.0
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
        "today": {"orders_placed": count_today, "entries_placed": len(groups_today),
                  "loss_at_risk": round_paise(loss_today), "premium_paid": round_paise(premium_today)},
        "kill": {"active": bool(kill), "since": (kill or {}).get("at"), "reason": (kill or {}).get("reason")},
        "breakers": dict(breakers["state"], locks=breakers["locks"], revenge=breakers["revenge"],
                         limits={k: c.get(k) for k in ("max_consecutive_losses", "max_daily_drawdown_pct",
                                                        "cooloff_after_stop_min", "revenge_window_min",
                                                        "revenge_trade", "max_trades_per_day")}),
        "rate": {"per_second": per_second, "hard_cap": ORDER_RATE_HARD_CAP, "sebi_threshold": SEBI_ORDER_RATE,
                 "ok_now": rate_ok},
        "fno": {"allowed": allow_fno, "ban_note": extra.get("ban_note"), "lots_note": extra.get("lots_note"),
                "banned_today": sorted(ban) if isinstance(ban, (set, list, tuple)) else None},
        "portfolio": portfolio_risk(holdings_book + run["book"], now, c, mk, book_errors)
        if (allow_fno and (holdings_book or run["book"] or book_errors)) else None,
    }


def grid_lines(grid, indent="  "):
    """The scenario grid as a small text table (rupees after charges)."""
    if not grid:
        return []
    head = indent + "%-22s" % "spot \\ IV" + "".join("%13s" % ("%+g vol" % v) for v in grid["iv_shifts"])
    out = [head]
    for row in grid["rows"]:
        moves = ", ".join("%+.2f%%" % x for _, x in sorted(row["move_pct"].items()))
        out.append(indent + "%-22s" % ("%+g sigma (%s)" % (row["sigma"], moves))[:22] +
                   "".join("%13s" % inr(v).replace("₹", "") for v in row["pnl"]))
    return out


def portfolio_lines(port):
    if not port:
        return []
    lines = []
    for e in port.get("errors") or []:
        lines.append("  ! " + e)
    if not port.get("greeks"):
        return lines
    for u, g in sorted(port["greeks"]["by_underlying"].items()):
        lines.append("  %s: delta %+.2f lots (%s per 1%% move), gamma %+.3f lots per 1%%, vega %s per vol point, "
                     "theta %s a day" % (u, g["delta_lots"], inr(g["delta_rupees_1pct"]), g["gamma_lots_1pct"],
                                         inr(g["vega"]), inr(g["theta"])))
    s = port["scenarios"]
    lim = port["limits"]
    lines.append("  scenarios over %s session(s), P&L after charges (sigma: %s):" % (
        _num(s["horizon_days"]), ", ".join("%s %.2f%%" % (u, v) for u, v in sorted(s["sigma_pct"].items()))))
    lines.extend(grid_lines(s, "    "))
    lines.append("  worst: %s (limit %s)" % (scenario_words(s["worst"]),
                                             inr(lim["max_scenario_loss"]) if lim.get("max_scenario_loss") else "off"))
    return lines


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if (res.get("kill") or {}).get("active"):
        lines.append("KILL SWITCH ON since %s (%s): every new entry is blocked today; exits still pass."
                     % (str(res["kill"].get("since") or "")[11:16], res["kill"].get("reason")))
    for x in (res.get("breakers") or {}).get("locks") or []:
        lines.append("LOCKED: " + x)
    if res["warnings"] or (res.get("kill") or {}).get("active") or (res.get("breakers") or {}).get("locks"):
        lines.append("")
    for r in res["results"]:
        what = r.get("contract") or r["symbol"]
        lines.append("%-5s #%d %s %s %s (%s), %s" % (r["verdict"], r["index"], r["side"], r["qty"], what,
                                                         r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    if res.get("portfolio"):
        lines.append("")
        lines.append("F&O book (open positions + the orders that passed):")
        lines.extend(portfolio_lines(res["portfolio"]))
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


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def tdir_of(config):
    return os.path.dirname(config) or "."


def gather_inputs(tdir, orders, doc, cfg, today, fetch=False, portfolio_path=None, trades_path=None,
                  kill_path=None):
    """(portfolio, extra): the F&O and behaviour inputs check() takes, read from a trader/ folder (and NSE's
    ban list and lot sizes when fetch is True and an F&O order or position needs them). The UI and the MCP
    server call this too, with fetch=False."""
    c = merged_config(cfg)
    portfolio = read_json(portfolio_path or os.path.join(tdir, "portfolio.json"), "portfolio", required=False)
    orders = [o for o in (orders or []) if isinstance(o, dict)]
    stock_fno = c.get("allow_fno") and any((contract_of(o, c)[0] or {}).get("kind") == "stock"
                                           for o in orders if is_fno(o))
    any_fno = c.get("allow_fno") and (any(is_fno(o) for o in orders) or bool(fno_positions(portfolio, c)))
    ban, ban_note = load_ban(os.path.join(tdir, "fo_secban.json"), today, bool(fetch and stock_fno))
    lots, lots_note = load_lots(os.path.join(tdir, "fo_mktlots.csv"), bool(fetch and any_fno), today)
    return portfolio, {
        "basket": bool(isinstance(doc, dict) and doc.get("basket")),
        "strategy": read_json(os.path.join(tdir, "strategy.json"), "strategy", required=False),
        "options": read_json(os.path.join(tdir, "options.json"), "option chain", required=False),
        "trades": read_json(trades_path or os.path.join(tdir, "state", "trades.json"), "trades log", required=False),
        "kill": read_json(kill_path or os.path.join(tdir, "state", "kill.json"), "kill switch", required=False),
        "ban": ban, "ban_note": ban_note, "lots": lots, "lots_note": lots_note}


def gather(a, orders, doc, cfg, today, fetch):
    """gather_inputs() for the command line's paths."""
    return gather_inputs(tdir_of(a.config), orders, doc, cfg, today, fetch, a.portfolio, a.trades, a.kill)


def _paths(ap):
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the capital")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--trades", default="trader/state/trades.json", help="fills with realised P&L (circuit-breakers)")
    ap.add_argument("--kill", default="trader/state/kill.json", help="the kill switch")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:40\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--offline", action="store_true", help="never ask NSE for the ban list or lot sizes")


def cmd_kill(argv):
    ap = argparse.ArgumentParser(prog="check.py kill", description="Kill switch: block every new entry today.")
    _paths(ap)
    ap.add_argument("--reason", help="why, in your words")
    ap.add_argument("--flatten", action="store_true", help="also build exit tickets with trader-broker's flatten")
    ap.add_argument("--include-cnc", action="store_true", help="with --flatten: delivery holdings too")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--risk", default="trader/state/risk.json", help="the recorded verdicts (entry PASSes revoked)")
    a = ap.parse_args(argv)
    now = parse_now(a.now)
    kill = read_json(a.kill, "kill switch", required=False)
    doc = kill_on(kill, now, a.reason)
    write_json(a.kill, doc)
    print("KILL SWITCH ON for %s from %s IST (%s). Every new entry is blocked today; exits still pass. "
          "Lift it with: check.py unkill" % (doc["date"], now.strftime("%H:%M"), doc["reason"]))
    rec = read_json(a.risk, "risk verdicts", required=False)
    if rec:
        rec, n = revoke_entries(rec, doc)
        if n:
            write_json(a.risk, rec)
            print("Revoked %d recorded entry PASS%s in %s: they cannot be confirmed now." % (
                n, "" if n == 1 else "es", a.risk))
    if not a.flatten:
        print("Open positions are untouched. check.py kill --flatten builds exit tickets for them.")
        return 0
    ticket = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-broker", "ticket.py")
    cfg = read_json(a.config, "config", required=False) or {}
    portfolio = read_json(a.portfolio, "portfolio", required=False)
    fno = fno_exit_orders(portfolio, cfg)
    rc = 0
    if os.path.isfile(ticket):
        import subprocess
        cmd = [sys.executable, ticket, "flatten", "--orders", a.orders, "--portfolio", a.portfolio, "--config",
               a.config, "--plan", a.plan] + (["--now", a.now] if a.now else []) + \
            (["--include-cnc"] if a.include_cnc else [])
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        keys = {str(x["name"]).upper() for x in fno_positions(portfolio, cfg) if FNO.match(str(x["name"]).upper())}
        if p.returncode and fno and "nothing to flatten" in out:
            out = "No cash positions to flatten.\n"
        elif p.returncode:
            rc = 1
        elif keys:
            # trader-broker's flatten knows cash shares only: an F&O position keyed by its trading symbol
            # must not become a cash SELL ticket; it gets its own exit ticket below
            body = read_json(a.orders, "orders", required=False)
            if isinstance(body, dict) and isinstance(body.get("orders"), list):
                keep = [o for o in body["orders"] if str(o.get("symbol", "")).upper() not in keys]
                if len(keep) != len(body["orders"]):
                    body["orders"] = keep
                    write_json(a.orders, body)
            out = "".join(l for l in out.splitlines(True) if not any(k in l for k in keys)) or \
                "No cash positions to flatten.\n"
        sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    else:
        print("trader-broker is not next to this skill: run its flatten yourself (ticket.py flatten) for cash shares.")
        rc = 1
    if fno:
        body = read_json(a.orders, "orders", required=False) if rc == 0 and os.path.isfile(a.orders) else None
        if not (isinstance(body, dict) and body.get("created_by") == "trader-broker" and
                str(body.get("source", "")).startswith("ticket.py flatten")):
            body = {"created_by": "trader-risk",
                    "created_at": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "proposed", "paper": True, "orders": [],
                    "note": "Kill-switch exit tickets. Nothing is placed until trader-risk passes each one and you "
                            "say yes to it.", "source": "check.py kill --flatten on %s" % doc["date"]}
        body["orders"] = list(body.get("orders") or []) + fno
        write_json(a.orders, body)
        for o in fno:
            print("Close: %s %d %s %s (%s)." % (o["side"], o["qty"], o["symbol"],
                                                "FUT %s" % o["expiry"] if o["segment"] == "FUT" else
                                                "%s %s %s" % (_num(o["strike"]), o["option_type"], o["expiry"]),
                                                "short legs first" if o["side"] == "BUY" else "after the shorts"))
        print("Wrote %d F&O exit ticket%s to %s. The paper broker takes F&O fills from paper v2; until then "
              "close them at your broker." % (len(fno), "" if len(fno) == 1 else "s", a.orders))
    print("The exit tickets still go through check.py and your typed yes, one by one.")
    return rc


def cmd_unkill(argv, ask=None):
    ap = argparse.ArgumentParser(prog="check.py unkill", description="Lift today's kill switch (typed confirmation).")
    ap.add_argument("--kill", default="trader/state/kill.json")
    ap.add_argument("--yes-text", help="the confirmation, typed by you: the word unkill")
    ap.add_argument("--now")
    a = ap.parse_args(argv)
    now = parse_now(a.now)
    kill = read_json(a.kill, "kill switch", required=False)
    typed = a.yes_text
    if typed is None:
        if not kill_active(kill, now.date().isoformat()):
            print("The kill switch is not on today: nothing to lift.")
            return 0
        if not sys.stdin.isatty() and ask is None:
            raise RiskError("no terminal to ask in: pass --yes-text with the word unkill, typed by you")
        typed = (ask or input)("The kill switch has been on since %s IST (%s). Type unkill to lift it: " % (
            str(kill.get("at", ""))[11:16], kill.get("reason")))
    doc, why = kill_off(kill, now, typed)
    if doc is None:
        print(why)
        return 1
    write_json(a.kill, doc)
    print("Kill switch lifted at %s IST. New entries are checked by every rule again (the circuit-breakers too)."
          % now.strftime("%H:%M"))
    return 0


def cmd_refresh(argv):
    ap = argparse.ArgumentParser(prog="check.py refresh", description="Fetch NSE's F&O ban list and lot sizes.")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--now")
    a = ap.parse_args(argv)
    today = parse_now(a.now).date().isoformat()
    tdir = tdir_of(a.config)
    ban, note = load_ban(os.path.join(tdir, "fo_secban.json"), today, True)
    print(note)
    lots, lnote = load_lots(os.path.join(tdir, "fo_mktlots.csv"), True, today)
    print(lnote + ("; %d underlyings" % len(lots) if lots else ""))
    return 0 if ban is not None and lots else 1


def cmd_scenarios(argv):
    ap = argparse.ArgumentParser(prog="check.py scenarios",
                                 description="Greeks and the scenario grid of your F&O book (read only).")
    _paths(ap)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = parse_now(a.now)
    today = a.today or now.date().isoformat()
    cfg = read_json(a.config, "config", required=False) or {}
    doc = read_json(a.orders, "orders", required=False)
    orders = (doc.get("orders", []) if isinstance(doc, dict) else doc) or []
    portfolio, extra = gather(a, orders, doc, cfg, today, False)
    res = scenarios(cfg, portfolio, load_prices(read_json(a.prices, "prices", required=False) or {})[0], now,
                    extra, orders)
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    for name, key in (("Open F&O positions", "open"), ("Open positions + every F&O proposal in orders.json",
                                                       "with_proposals")):
        print(name + ":")
        port = res[key]
        lines = portfolio_lines(port)
        print("\n".join(lines) if lines else "  none")
    print("Education and tooling, not financial advice.")
    return 0


def scenarios(cfg, portfolio, prices, now, extra=None, orders=None):
    """Read only: the F&O book's Greeks and scenario grid, open positions alone and with every F&O proposal
    (as if it filled), plus the kill switch and circuit-breaker state. For the UI and the MCP server."""
    extra = extra or {}
    c = merged_config(cfg)
    today = now.date().isoformat()
    mk = market_inputs(extra.get("strategy"), extra.get("options"))
    book, errors = open_book(portfolio, now, mk, prices or {}, c, extra.get("lots"))
    prop, perr = [], []
    for o in orders or []:
        if not (isinstance(o, dict) and is_fno(o)):
            continue
        k, why = contract_of(o, c)
        q = _f(o.get("qty"))
        if not k or not q:
            perr.append("order %s: %s" % (o.get("leg") or o.get("symbol"), why or "no qty"))
            continue
        signed = q if str(o.get("side", "")).upper() == "BUY" else -q
        lot = lot_for(extra.get("lots"), k["underlying"], k["expiry"]) or int(_f(o.get("lot_size")) or 0) or None
        leg, why = book_leg(k, signed, _f(o.get("price")), now, mk, prices or {}, risk_rate(c), lot,
                            _f(o.get("iv")), _f(o.get("forward")))
        if leg is None:
            perr.append(why)
        else:
            prop.append(leg)
    capital = _f(c.get("capital")) or 0.0
    return {"now_ist": now.strftime("%Y-%m-%d %H:%M"), "open": portfolio_risk(book, now, c, mk, errors),
            "with_proposals": portfolio_risk(book + prop, now, c, mk, errors + perr),
            "kill": kill_active(extra.get("kill"), today),
            "breakers": circuit_breakers(c, extra.get("trades"), today, now, capital),
            "rate": {"per_second": order_rate(c)[0], "hard_cap": ORDER_RATE_HARD_CAP, "sebi_threshold": SEBI_ORDER_RATE},
            "disclaimer": "Estimates from a model (Black-76 at today's IV), not a forecast. Education and tooling, "
                          "not financial advice."}


def cmd_rate_gate(argv):
    ap = argparse.ArgumentParser(prog="check.py rate-gate",
                                 description="May one more order go to the broker now? (for broker adapters)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--log", default="trader/state/rate.json", help="when orders were sent")
    ap.add_argument("--record", action="store_true", help="when allowed, record this send")
    a = ap.parse_args(argv)
    c = merged_config(read_json(a.config, "config", required=False) or {})
    per, warn = order_rate(c)
    now = datetime.datetime.now(IST)
    log = read_json(a.log, "order-rate log", required=False) or {}
    sent = [parse_time(t) for t in log.get("sent") or []]
    ok, wait = rate_gate(sent, now, per)
    if warn:
        print("WARNING: " + warn)
    if not ok:
        print("WAIT %.3f s: %d orders a second is your limit (SEBI registers algos above %d)" % (wait, per,
                                                                                               SEBI_ORDER_RATE))
        return 1
    if a.record:
        keep = [t.isoformat() for t in sent if t and (now - t).total_seconds() < 5] + [now.isoformat()]
        write_json(a.log, {"sent": keep, "per_second": per})
    print("OK: send now (%d a second)" % per)
    return 0


COMMANDS = {"kill": cmd_kill, "unkill": cmd_unkill, "refresh": cmd_refresh, "scenarios": cmd_scenarios,
            "rate-gate": cmd_rate_gate}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        try:
            return COMMANDS[argv[0]](argv[1:])
        except (RiskError, RulesError) as e:
            print("check.py: %s" % e, file=sys.stderr)
            return 1
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk). Also: "
                                             "check.py kill | unkill | scenarios | refresh | rate-gate.")
    _paths(ap)
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-29")
        today = a.today or now.date().isoformat()
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        cache = os.path.join(tdir_of(a.config), "fno_stocks.json")
        cfg["_fno"] = load_fno_stocks(cfg, cache, fetch=False)[0]
        portfolio, extra = gather(a, orders, doc, cfg, today, not a.offline)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, extra)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            write_json(a.out, rec)
    except (RiskError, RulesError) as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
