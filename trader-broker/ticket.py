#!/usr/bin/env python3
"""Order tickets, your yes and the PAPER broker for trader-broker. Standard library only.

v0.1 has one broker: paper. Nothing here talks to a real broker or places a
real order. It does its work around the one moment that matters, your yes:

  show      each proposed order as a plain-language ticket plus the exact
            broker-neutral order JSON. An order is READY only if trader-risk
            passed THIS exact order today (matched by fingerprint) and nobody
            has decided on it yet.
  confirm   records your explicit yes for ONE order, your words verbatim,
            and places it on the paper account. It refuses "yes to all",
            "sure", "ok", a yes that names another stock or amount, and any
            order that is not READY.
  decline   records a no.
  fill      simulates a paper fill: at --price, or at the fresh price in
            trader/prices.json. Applies the estimated charges, updates
            trader/portfolio.json (cash, positions) and the trades log
            trader/state/trades.json. An entry's SL-M stop and LIMIT target
            then work as a pair: when one fills, the other is cancelled.
  flatten   builds SELL MARKET tickets for every open MIS position, to be
            out by flat_by (15:10 IST), before the broker's own auto
            square-off. They still go through trader-risk and your yes.
  status    cash, positions, open legs, today's orders, trades and limits.

    python3 ticket.py show
    python3 ticket.py confirm --index 1 --yes-text "yes"
    python3 ticket.py fill --index 1 --price 1174.30          # the entry
    python3 ticket.py fill --index 1 --leg target              # at the target price
    python3 ticket.py flatten
    python3 ticket.py status

Decisions go to trader/state/broker.json (append only). Times are IST;
--now "2026-10-29 09:41" pins the clock.
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


def round_trip_charges(entry, exit, qty, cfg=None):
    """Estimated charges for buying qty at entry and selling qty at exit (one round trip).

    brokerage  min(turnover x brokerage_pct, brokerage_cap) on EACH executed order (buy and sell)
    stt        MIS: stt_sell_pct on the sell side only. CNC: stt_delivery_pct on both sides
    exchange   exchange_txn_pct on buy + sell turnover
    sebi       sebi_per_crore rupees per crore (1,00,00,000) of buy + sell turnover
    stamp      MIS: stamp_buy_pct on the buy side. CNC: stamp_delivery_pct on the buy side
    gst        gst_pct on brokerage + exchange + sebi
    Each part is rounded half up to the paisa from its unrounded value; total is the sum of the parts."""
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
PRODUCT_WORDS = {"MIS": "intraday", "CNC": "delivery"}
FRESH_MINUTES = 15


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
        ch = round_trip_charges(buy, sell, q, dict(c, product=order.get("product") or c["product"]))["total"]
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
    """The decision recorded today for this fingerprint (placed or declined), or None."""
    found = records(state, fp, today, ("placed", "declined"))
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
    if rec:
        return False, "DONE: %s at %s" % (rec.get("status"), rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).upper() == "BUY" else "exit"


def paper_only(cfg):
    c = merged_config(cfg)
    if str(c.get("broker") or "paper").lower() != "paper" or not c.get("paper", True):
        raise TicketError("this version ships only the paper broker (\"broker\": \"paper\", \"paper\": true). "
                          "Live broker adapters are not built yet; nothing was sent")


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    lines = ["ORDER %d of %d, PAPER account (no real money)" % (idx, total)]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.append("  The broker-neutral order:")
    for l in json.dumps(neutral(order), indent=2, ensure_ascii=False).splitlines():
        lines.append("    " + l)
    if ready:
        lines.append("  Reply \"yes\" to place this one order on the paper account, or \"no\" to skip it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    gross, ch, total = loss_at_stop(order, cfg)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
            "fingerprint": fingerprint(order), "paper": True, "broker": "paper", "order": neutral(order),
            "why": order.get("why", ""), "loss_at_stop": gross, "charges": ch, "max_loss": total,
            "stop": stop_of(order), "target": target_of(order)}


# ---------------------------------------------------------------- paper fills

def side_charges(side, price, qty, cfg, product):
    """Charges on one executed order: the buy-side or sell-side half of round_trip_charges."""
    c = dict(merged_config(cfg), product=product)
    if side == "BUY":
        return round_trip_charges(price, 0, qty, c)
    return round_trip_charges(0, price, qty, c)


def load_portfolio(path, cfg):
    pf = read_json(path, "portfolio", required=False)
    if pf is None:
        cap = _f(merged_config(cfg).get("capital"))
        if not cap:
            raise TicketError("no %s yet and no capital in the config to start it from" % path)
        pf = {"cash": cap, "positions": {}}
    if isinstance(pf.get("positions"), list):          # an older list shape
        pf["positions"] = {str(p.get("symbol") or p.get("ticker")).upper():
                           {"qty": p.get("qty", p.get("shares", 0)), "avg_price": p.get("avg_price", 0)}
                           for p in pf["positions"]}
    pf.setdefault("positions", {})
    return pf


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


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_confirm(a):
    orders, cfg, risk, state, today, now = _load(a)
    paper_only(cfg)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.yes_text, order, [o.get("symbol", "") for o in orders], cfg)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
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
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d, word for word: \"%s\"." % (a.index, a.yes_text))
    print("Placed on the PAPER account as %s: %s." % (rec["paper_order_id"], plain(order, cfg)))
    print("When it fills: ticket.py fill --index %d --price P (or the fresh price in trader/prices.json)." % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": str(order.get("side", "")).upper(),
           "plain": plain(order, cfg), "status": "declined", "yes_text": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    paper_only(cfg)
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
    write_json(a.portfolio, pf)
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


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    c = merged_config(cfg)
    now, today = _clock(a)
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
        flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) or c["flat_by"]
        orders.append({"symbol": sym, "exchange": "NSE", "product": prod, "side": "SELL", "qty": q,
                       "order_type": "MARKET", "price": None, "validity": "DAY",
                       "why": "flat by %s IST: closes the %s %s position before the broker's MIS auto square-off "
                              "(qty from %s)" % (flat_by, sym, prod, a.portfolio)})
    if not orders:
        raise TicketError("nothing to flatten: no open MIS positions%s in %s%s"
                          % ((" for " + want) if want else "", a.portfolio,
                             ("; kept " + ", ".join(skipped)) if skipped else ""))
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker",
           "created_at": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "status": "proposed", "paper": True,
           "note": "Flatten tickets. Nothing is placed until trader-risk passes each one and you say yes to it.",
           "source": "ticket.py flatten on %s" % today, "orders": orders}
    write_json(a.orders, doc)
    fb = hhmm_time(c["flat_by"])
    late = fb is not None and now.time() >= fb
    for o in orders:
        print("Flatten: %s." % plain(o, cfg))
    for s in skipped:
        print("Not flattened: %s." % s)
    print("Wrote %d ticket%s to %s%s." % (len(orders), "" if len(orders) == 1 else "s", a.orders,
                                          (" (it replaced %d earlier proposal%s)" % (replaced, "" if replaced == 1 else "s"))
                                          if replaced else ""))
    if late:
        print("It is %s IST, past your flat_by %s: do this now. Brokers auto square-off MIS positions around "
              "15:15-15:25 IST, usually with an extra fee." % (now.strftime("%H:%M"), c["flat_by"]))
    print("Next: trader-risk (check.py; a sell that only closes what you hold is never blocked by the day limits), "
          "then ticket.py show and the user's yes for each ticket. When a flatten sell fills, the paper stop and "
          "target of that position are cancelled.")
    return 0


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
    fb = hhmm_time(c["flat_by"])
    if mis and fb and now.time() >= fb:
        lines.append("Past flat_by %s with MIS open (%s): run ticket.py flatten now." % (c["flat_by"], ", ".join(mis)))
    lines.append("Paper trading. Education and tooling, not financial advice.")
    print("\n".join(lines))
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
    ap = argparse.ArgumentParser(description="Order tickets, your yes and the paper broker (trader-broker). "
                                             "No real orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:41\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("confirm", help="record the user's explicit yes for one order and paper-place it")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--yes-text", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("fill", help="simulate a paper fill")
    p.add_argument("--index", type=int, help="the order in trader/orders.json (after confirm)")
    p.add_argument("--leg", choices=["order", "stop", "target"], help="which part filled (default: the order)")
    p.add_argument("--symbol")
    p.add_argument("--side", type=str.upper, choices=["BUY", "SELL"])
    p.add_argument("--qty", type=int)
    p.add_argument("--product", type=str.upper, choices=["MIS", "CNC"])
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
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "confirm": cmd_confirm, "decline": cmd_decline, "fill": cmd_fill,
                "flatten": cmd_flatten, "status": cmd_status}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
