"""Zerodha Kite Connect v3 over urllib. Docs: https://kite.trade/docs/connect/v3/ (read 2026-11-01).

Plans: Personal (free) covers orders, GTT, portfolio, margins and the instrument dump; quotes and
historical candles need the Connect plan (Rs 500 a month per app). A Personal-plan call to a
data endpoint comes back as PermissionException and is reported as NotSubscribed.
"""

import csv
import json
import hashlib
import io
import urllib.parse

from .base import (Adapter, AuthExpired, BrokerDown, BrokerError, InsufficientFunds, InvalidInstrument, InvalidOrder,
                   IPNotWhitelisted, NotSubscribed, RateLimited, Rejected, canonical_symbol, ip_words, mask_id,
                   num, parse_ist, rupees, whole)

STATUS = {"COMPLETE": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected"}
GTT_STATUS = {"active": "active", "triggered": "triggered", "disabled": "cancelled", "expired": "expired",
              "cancelled": "cancelled", "rejected": "rejected", "deleted": "cancelled"}
PRODUCT_FROM = {"BO": "MIS", "CO": "MIS", "MTF": "CNC"}       # bracket and cover orders are intraday
INTERVALS = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
             "60m": "60minute", "1d": "day"}


class KiteAdapter(Adapter):
    name = "kite"
    title = "Zerodha Kite"
    capabilities = {"profile": True, "funds": True, "holdings": True, "positions": True, "orders": True,
                    "quotes": True, "history": True, "instruments": True, "basket_margin": True, "gtt_oco": True,
                    "native_oco": True, "sandbox": False, "place_orders": True, "market_protection": True}
    env_names = {"api_key": "BT_KITE_API_KEY", "api_secret": "BT_KITE_API_SECRET", "redirect": "BT_KITE_REDIRECT",
                 "access_token": "BT_KITE_ACCESS_TOKEN", "token_date": "BT_KITE_TOKEN_DATE"}
    hosts = {"api": "https://api.kite.trade", "login": "https://kite.zerodha.com"}
    token_cutoff = "06:00"
    plans = ("Personal (free): orders, GTT, portfolio, margins, instruments. Connect (Rs 500/month per app): adds "
             "quotes, historical candles and the live feed.")
    default_redirect = "http://127.0.0.1:8765/kite"

    def transport_rates(self):
        return {"read_rate": 5, "quote_rate": 1}

    def auth_headers(self):
        self.need("api_key", "access_token")
        return {"X-Kite-Version": "3",
                "Authorization": "token %s:%s" % (self.env_get("api_key"), self.env_get("access_token"))}

    def error_from(self, status, body, text):
        body = body if isinstance(body, dict) else {}
        et = body.get("error_type") or ""
        msg = body.get("message") or (text or "")[:200] or ("HTTP %s" % status)
        words = "Kite: %s" % msg
        if status == 429:
            return RateLimited(words, et, status)
        if et == "TokenException" or (status == 403 and not et):
            return AuthExpired("%s. Log in again: python3 ticket.py login kite" % words, et, status)
        if ip_words(msg):
            return IPNotWhitelisted("%s. Orders must come from the static IP registered in the Kite developer "
                                    "console" % words, et, status)
        if et == "PermissionException":
            return NotSubscribed("%s. Quotes and history need the Kite Connect plan" % words, et, status)
        if et == "MarginException":
            return InsufficientFunds(words, et, status)
        if et in ("InputException", "OrderException", "HoldingException", "UserException"):
            if "instrument" in msg.lower() or "tradingsymbol" in msg.lower():
                return InvalidInstrument(words, et, status)
            return Rejected(words, et, status)
        if et in ("NetworkException", "DataException") or status >= 500:
            return BrokerDown(words, et, status)
        return BrokerError(words, et, status)

    @staticmethod
    def _data(res):
        return (res or {}).get("data") if isinstance(res, dict) else None

    # ---------------------------------------------------------------- read
    def profile(self):
        d = self._data(self.call("GET", "api", "/user/profile")) or {}
        return {"broker": self.name, "user_id": mask_id(d.get("user_id")), "name": d.get("user_shortname") or "",
                "exchanges": sorted(d.get("exchanges") or []), "products": d.get("products") or [],
                "token_valid_until": None, "data_plan": None}

    def funds(self):
        d = (self._data(self.call("GET", "api", "/user/margins")) or {}).get("equity") or {}
        av, ut = d.get("available") or {}, d.get("utilised") or {}
        return {"available": rupees(d.get("net")), "used": rupees(ut.get("debits")),
                "collateral": rupees(av.get("collateral")), "opening": rupees(av.get("opening_balance")),
                "total": rupees(num(d.get("net"), 0) + num(ut.get("debits"), 0))}

    def holdings(self):
        out = []
        for h in self._data(self.call("GET", "api", "/portfolio/holdings")) or []:
            out.append({"symbol": h.get("tradingsymbol"), "exchange": h.get("exchange"), "isin": h.get("isin"),
                        "qty": whole(h.get("quantity")), "t1_qty": whole(h.get("t1_quantity")),
                        "avg_price": rupees(h.get("average_price")), "last_price": rupees(h.get("last_price")),
                        "pnl": rupees(h.get("pnl")), "product": "CNC"})
        return out

    def positions(self):
        out = []
        for p in (self._data(self.call("GET", "api", "/portfolio/positions")) or {}).get("net") or []:
            out.append({"symbol": p.get("tradingsymbol"), "exchange": p.get("exchange"),
                        "product": PRODUCT_FROM.get(p.get("product"), p.get("product")),
                        "qty": whole(p.get("quantity")),
                        "avg_price": rupees(p.get("average_price")), "last_price": rupees(p.get("last_price")),
                        "pnl": rupees(p.get("pnl")), "realised": rupees(p.get("realised")),
                        "unrealised": rupees(p.get("unrealised")), "buy_qty": whole(p.get("buy_quantity")),
                        "sell_qty": whole(p.get("sell_quantity"))})
        return out

    def orders(self):
        out = []
        for o in self._data(self.call("GET", "api", "/orders")) or []:
            out.append({"broker_order_id": str(o.get("order_id")), "symbol": o.get("tradingsymbol"),
                        "exchange": o.get("exchange"), "side": o.get("transaction_type"),
                        "qty": whole(o.get("quantity")), "filled_qty": whole(o.get("filled_quantity")),
                        "pending_qty": whole(o.get("pending_quantity")), "order_type": o.get("order_type"),
                        "product": PRODUCT_FROM.get(o.get("product"), o.get("product")), "price": rupees(o.get("price")),
                        "trigger_price": rupees(o.get("trigger_price")), "avg_price": rupees(o.get("average_price")),
                        "status": STATUS.get(str(o.get("status") or "").upper(), "open"),
                        "status_message": o.get("status_message") or "", "tag": o.get("tag"),
                        "time": parse_ist(o.get("order_timestamp"))})
        return out

    def trades(self):
        out = []
        for t in self._data(self.call("GET", "api", "/trades")) or []:
            out.append({"trade_id": str(t.get("trade_id")), "broker_order_id": str(t.get("order_id")),
                        "symbol": t.get("tradingsymbol"), "exchange": t.get("exchange"),
                        "side": t.get("transaction_type"), "qty": whole(t.get("quantity")),
                        "price": rupees(t.get("average_price")), "product": t.get("product"),
                        "time": parse_ist(t.get("fill_timestamp") or t.get("exchange_timestamp"))})
        return out

    def ltp(self, instruments):
        keys = {"%s:%s" % (i["exchange"], i["broker_symbol"]): i for i in instruments}
        d = self._data(self.call("GET", "api", "/quote/ltp", kind="quote", query={"i": list(keys)})) or {}
        return {keys[k]["symbol"]: rupees(v.get("last_price")) for k, v in d.items() if k in keys}

    def history(self, inst, interval, start, end):
        iv = INTERVALS.get(interval)
        if not iv:
            raise BrokerError("interval is one of %s" % ", ".join(sorted(INTERVALS)))
        path = "/instruments/historical/%s/%s" % (inst["broker_id"], iv)
        d = self._data(self.call("GET", "api", path, query={"from": start, "to": end})) or {}
        return [[parse_ist(c[0])] + [num(x) for x in c[1:6]] for c in d.get("candles") or []]

    def instruments(self):
        res = self.transport.request("GET", self.hosts["api"].rstrip("/") + "/instruments",
                                     headers={"X-Kite-Version": "3"}, raw=True, error=self.error_from)
        return parse_master(res.text())

    def basket_margin(self, orders):
        body = []
        for o in orders:
            inst = self.resolve(o["symbol"], o.get("exchange") or "NSE")
            body.append({"exchange": inst["exchange"], "tradingsymbol": inst["broker_symbol"],
                         "transaction_type": o["side"], "variety": "regular", "product": o["product"],
                         "order_type": o["order_type"], "quantity": o["qty"], "price": num(o.get("price"), 0),
                         "trigger_price": num(o.get("trigger_price"), 0)})
        d = self._data(self.call("POST", "api", "/margins/basket", query={"consider_positions": "true"},
                                 json_body=body)) or {}
        initial, final = (d.get("initial") or {}).get("total"), (d.get("final") or {}).get("total")
        return {"total": rupees(final), "initial": rupees(initial),
                "hedge_benefit": rupees(num(initial, 0) - num(final, 0)),
                "orders": [{"symbol": x.get("tradingsymbol"), "total": rupees(x.get("total"))}
                           for x in d.get("orders") or []],
                "note": "Kite basket margin with your open positions considered"}

    # ---------------------------------------------------------------- write (only through a GatePass)
    def order_request(self, order, inst):
        otype = str(order["order_type"]).upper()
        form = {"tradingsymbol": inst["broker_symbol"], "exchange": inst["exchange"],
                "transaction_type": str(order["side"]).upper(), "order_type": otype, "quantity": int(order["qty"]),
                "product": str(order["product"]).upper(), "validity": str(order.get("validity") or "DAY").upper()}
        if otype in ("LIMIT", "SL"):
            form["price"] = "%.2f" % float(order["price"])
        if otype in ("SL", "SL-M"):
            form["trigger_price"] = "%.2f" % float(order["trigger_price"])
        if otype in ("MARKET", "SL-M"):
            form["market_protection"] = -1        # the broker's automatic protection
        if order.get("tag"):
            form["tag"] = str(order["tag"])[:20]
        return "POST", "api", "/orders/regular", {"form": form}

    def placed_id(self, res):
        return str((self._data(res) or {}).get("order_id"))

    def cancel_request(self, broker_order_id):
        return "DELETE", "api", "/orders/regular/%s" % urllib.parse.quote(str(broker_order_id)), {}

    # ---------------------------------------------------------------- native linked exit: GTT two-leg OCO
    linked_style = "exit_oco"
    linked_words = ("a GTT two-leg OCO after the entry fills: the stop and the target as one trigger, the broker "
                    "cancels the other when one fires (GTT legs are LIMIT orders)")

    def _gtt_form(self, linked, inst, last_price):
        if linked.get("linked") != "oco":
            raise InvalidOrder("Kite's native linked order is the exit pair after the fill (leg oco)")
        stop, target = linked["stop"], linked["target"]
        if str(stop.get("order_type") or "").upper() != "SL" or not num(stop.get("price")):
            raise InvalidOrder("Kite's GTT legs are LIMIT orders: give the stop a limit price (an SL stop_loss with "
                               "a price and a trigger_price) for a native OCO, or send the exits leg by leg")
        lp = num(last_price)
        if not lp or lp <= 0:
            raise InvalidOrder("Kite needs the last price for a GTT (the fill price or the latest quote)")
        side = str(linked["side"]).upper()
        leg = {"exchange": inst["exchange"], "tradingsymbol": inst["broker_symbol"], "transaction_type": side,
               "quantity": int(linked["qty"]), "order_type": "LIMIT", "product": str(linked["product"]).upper()}
        stop_leg = dict(leg, price=round(float(stop["price"]), 2))
        target_leg = dict(leg, price=round(float(target["price"]), 2))
        trig_stop, trig_target = round(float(stop["trigger_price"]), 2), round(float(target["price"]), 2)
        if side == "SELL":           # a long position: the stop below, the target above
            values, orders = [trig_stop, trig_target], [stop_leg, target_leg]
        else:                        # a short position: the target below, the stop above
            values, orders = [trig_target, trig_stop], [target_leg, stop_leg]
        if not values[0] < lp < values[1]:
            raise InvalidOrder("the last price %.2f is not between the two triggers %.2f and %.2f: the market already "
                               "crossed one, so a GTT would fire at once. Exit by hand or with a fresh ticket"
                               % (lp, values[0], values[1]))
        cond = {"exchange": inst["exchange"], "tradingsymbol": inst["broker_symbol"], "trigger_values": values,
                "last_price": round(lp, 2)}
        return {"type": "two-leg", "condition": json.dumps(cond, separators=(",", ":")),
                "orders": json.dumps(orders, separators=(",", ":"))}

    def linked_request(self, linked, inst, last_price=None):
        return "POST", "api", "/gtt/triggers", {"form": self._gtt_form(linked, inst, last_price)}

    def linked_placed_id(self, res):
        tid = (self._data(res) or {}).get("trigger_id")
        return str(tid) if tid is not None else None

    def linked_cancel_request(self, linked_id, record=None):
        return "DELETE", "api", "/gtt/triggers/%s" % urllib.parse.quote(str(linked_id)), {}

    def linked_modify_requests(self, linked_id, linked, inst, record=None, last_price=None):
        return [("PUT", "api", "/gtt/triggers/%s" % urllib.parse.quote(str(linked_id)),
                 {"form": self._gtt_form(linked, inst, last_price)})]

    def linked_book(self):
        out = []
        for g in self._data(self.call("GET", "api", "/gtt/triggers")) or []:
            cond, orders = g.get("condition") or {}, g.get("orders") or []
            vals = [num(v) for v in cond.get("trigger_values") or []]
            exit_side = str((orders[0] if orders else {}).get("transaction_type") or "").upper()
            two = g.get("type") == "two-leg" and len(vals) == 2 and len(orders) == 2
            if two and exit_side == "SELL":
                (stop_t, target_t), (stop_o, target_o) = vals, orders
            elif two:
                (target_t, stop_t), (target_o, stop_o) = vals, orders
            else:
                stop_t = target_t = None
                stop_o = target_o = orders[0] if orders else {}
            legs = []
            for name, o in (("stop", stop_o), ("target", target_o)):
                res = (o or {}).get("result") or {}
                orr = res.get("order_result") or {}
                legs.append({"leg": name, "status": orr.get("status"), "order_id": orr.get("order_id") or None})
            st = str(g.get("status") or "").lower()
            out.append({"linked_id": str(g.get("id")), "style": "exit_oco" if two else "single",
                        "symbol": cond.get("tradingsymbol"), "broker_symbol": cond.get("tradingsymbol"),
                        "exchange": cond.get("exchange"), "product": (stop_o or {}).get("product"),
                        "side": "BUY" if exit_side == "SELL" else "SELL", "qty": whole((stop_o or {}).get("quantity")),
                        "entry_price": None, "stop_trigger": stop_t, "stop_price": num((stop_o or {}).get("price")),
                        "target_price": target_t if two else None,
                        "status": GTT_STATUS.get(st, st or "unknown"), "raw_status": g.get("status"), "legs": legs,
                        "tag": None, "time": parse_ist(g.get("created_at"))})
        return out

    # ---------------------------------------------------------------- login
    def redirect(self):
        return self.env_get("redirect") or self.default_redirect

    def login_start(self, state):
        """(browser URL, the query param that carries the code, the state param or None)."""
        self.need("api_key", "api_secret")
        q = urllib.parse.urlencode({"v": "3", "api_key": self.env_get("api_key"),
                                    "redirect_params": urllib.parse.urlencode({"state": state})})
        return self.hosts["login"].rstrip("/") + "/connect/login?" + q, "request_token", "state"

    def login_finish(self, params):
        rt = params["request_token"]
        key, secret = self.env_get("api_key"), self.env_get("api_secret")
        checksum = hashlib.sha256((key + rt + secret).encode("utf-8")).hexdigest()
        res = self.transport.request("POST", self.hosts["api"].rstrip("/") + "/session/token",
                                     headers={"X-Kite-Version": "3"}, kind="auth", error=self.error_from,
                                     form={"api_key": key, "request_token": rt, "checksum": checksum})
        tok = (self._data(res) or {}).get("access_token")
        if not tok:
            raise AuthExpired("Kite did not return an access token")
        return tok


def parse_master(text):
    """Kite's instrument dump (CSV) -> instrument rows for NSE stocks and NFO futures and options."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        exch, seg, itype = r.get("exchange"), r.get("segment"), r.get("instrument_type")
        if exch == "NSE" and seg == "NSE" and itype == "EQ":
            kind, und, exp, strike, right = "EQ", r["tradingsymbol"], None, None, None
        elif exch == "NFO" and itype == "FUT":
            kind, und, exp, strike, right = "FUT", r["name"], r["expiry"], None, None
        elif exch == "NFO" and itype in ("CE", "PE"):
            kind, und, exp, strike, right = "OPT", r["name"], r["expiry"], num(r["strike"]), itype
        else:
            continue
        out.append({"symbol": canonical_symbol(kind, und, exp, strike, right), "exchange": exch, "kind": kind,
                    "underlying": und, "expiry": exp, "strike": strike, "right": right,
                    "lot_size": whole(r.get("lot_size"), 1), "tick_size": num(r.get("tick_size")),
                    "freeze_qty": None, "exchange_token": r.get("exchange_token"),
                    "broker_symbol": r["tradingsymbol"], "broker_id": r.get("instrument_token")})
    return out
