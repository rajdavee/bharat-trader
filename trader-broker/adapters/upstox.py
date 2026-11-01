"""Upstox API v2 and v3 over urllib. Docs: https://upstox.com/developer/api-documentation/ (read 2026-11-01).

Orders go to the HFT host (api-hft.upstox.com, v3 place and cancel). The sandbox (one sandbox app
per user, a 30-day token, orders only) is api-sandbox.upstox.com. Funds are one combined figure in
"equity" since 19 Jul 2025. Instrument tick sizes in the master file are in paise.
"""

import datetime
import gzip
import io
import json
import urllib.parse

from .base import (IST, Adapter, AuthExpired, BrokerDown, BrokerError, InsufficientFunds, InvalidInstrument, InvalidOrder,
                   IPNotWhitelisted, RateLimited, Rejected, canonical_symbol, expiry_from_epoch_ms, ip_words,
                   mask_id, num, parse_ist, rupees, whole)

STATUS = {"complete": "filled", "cancelled": "cancelled", "rejected": "rejected"}
PRODUCT_TO = {"MIS": "I", "CNC": "D", "NRML": "D"}
INTERVALS = {"1m": ("minutes", 1), "5m": ("minutes", 5), "15m": ("minutes", 15), "30m": ("minutes", 30),
             "60m": ("hours", 1), "1d": ("days", 1)}
AUTH_CODES = {"UDAPI100050", "UDAPI100067", "UDAPI100016"}
RATE_CODES = {"UDAPI10005", "UDAPI100099"}
DOWN_CODES = {"UDAPI100072", "UDAPI100074", "UDAPI100500"}


def product_from(p, exchange):
    p = str(p or "").upper()
    if p in ("I", "CO", "OCO"):         # intraday, cover order: intraday products
        return "MIS"
    if p == "MTF":                      # margin-funded delivery
        return "CNC"
    if p == "D":
        return "NRML" if str(exchange).upper() in ("NFO", "NSE_FO") else "CNC"
    return p


def _us_time(v):
    """Upstox GTT times are microseconds since the epoch -> IST ISO text (or None)."""
    x = num(v)
    if not x:
        return None
    return datetime.datetime.fromtimestamp(x / 1e6, IST).replace(microsecond=0).isoformat()


def plain_symbol(s):
    s = str(s or "")
    return s[:-3] if s.endswith("-EQ") else s


class UpstoxAdapter(Adapter):
    name = "upstox"
    title = "Upstox"
    capabilities = {"profile": True, "funds": True, "holdings": True, "positions": True, "orders": True,
                    "quotes": True, "history": True, "instruments": True, "basket_margin": True, "gtt_oco": True,
                    "native_oco": True, "sandbox": True, "place_orders": True, "market_protection": True}
    env_names = {"api_key": "BT_UPSTOX_API_KEY", "api_secret": "BT_UPSTOX_API_SECRET",
                 "redirect": "BT_UPSTOX_REDIRECT", "access_token": "BT_UPSTOX_ACCESS_TOKEN",
                 "token_date": "BT_UPSTOX_TOKEN_DATE", "sandbox_token": "BT_UPSTOX_SANDBOX_TOKEN"}
    hosts = {"api": "https://api.upstox.com", "hft": "https://api-hft.upstox.com",
             "assets": "https://assets.upstox.com"}
    sandbox_host = "https://api-sandbox.upstox.com"
    token_cutoff = "03:30"
    plans = "Trading and data APIs are free; brokerage per executed order applies (see docs/brokers.md)."
    default_redirect = "http://127.0.0.1:8765/upstox"

    def __init__(self, *a, **kw):
        Adapter.__init__(self, *a, **kw)
        if self.sandbox and not (kw.get("hosts") or {}).get("api"):
            self.hosts["api"] = self.hosts["hft"] = self.sandbox_host

    def transport_rates(self):
        return {"read_rate": 10, "quote_rate": 5}

    def auth_headers(self):
        tok = self.token()
        if not tok:
            self.need("sandbox_token" if self.sandbox else "access_token")
        return {"Authorization": "Bearer %s" % tok}

    def error_from(self, status, body, text):
        errs = (body or {}).get("errors") if isinstance(body, dict) else None
        e = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else {}
        code = e.get("errorCode") or e.get("error_code") or ""
        msg = e.get("message") or (text or "")[:200] or ("HTTP %s" % status)
        words = "Upstox %s: %s" % (code, msg) if code else "Upstox: %s" % msg
        if code == "UDAPI1154" or ip_words(msg):
            return IPNotWhitelisted("%s. Orders must come from the static IP registered in Upstox My Apps"
                                    % words, code, status)
        if code in RATE_CODES or status == 429:
            return RateLimited(words, code, status)
        if code in AUTH_CODES or status == 401:
            return AuthExpired("%s. Log in again: python3 ticket.py login upstox" % words, code, status)
        if code == "UDAPI100011":
            return InvalidInstrument(words, code, status)
        if code in DOWN_CODES or status >= 500:
            return BrokerDown(words, code, status)
        low = msg.lower()
        if "insufficient" in low or "margin" in low and "shortfall" in low:
            return InsufficientFunds(words, code, status)
        if status in (400, 403, 422):
            return Rejected(words, code, status)
        return BrokerError(words, code, status)

    @staticmethod
    def _data(res):
        return (res or {}).get("data") if isinstance(res, dict) else None

    # ---------------------------------------------------------------- read
    def profile(self):
        d = self._data(self.call("GET", "api", "/v2/user/profile")) or {}
        return {"broker": self.name, "user_id": mask_id(d.get("user_id")), "name": d.get("user_name") or "",
                "exchanges": sorted(d.get("exchanges") or []),
                "products": [product_from(p, "NSE") for p in d.get("products") or []],
                "token_valid_until": None, "data_plan": True}

    def funds(self):
        d = (self._data(self.call("GET", "api", "/v2/user/get-funds-and-margin")) or {}).get("equity") or {}
        av, used = num(d.get("available_margin"), 0), num(d.get("used_margin"), 0)
        return {"available": rupees(av), "used": rupees(used), "collateral": None,
                "opening": None, "total": rupees(av + used)}

    def holdings(self):
        out = []
        for h in self._data(self.call("GET", "api", "/v2/portfolio/long-term-holdings")) or []:
            out.append({"symbol": plain_symbol(h.get("trading_symbol") or h.get("tradingsymbol")),
                        "exchange": h.get("exchange"), "isin": h.get("isin"), "qty": whole(h.get("quantity")),
                        "t1_qty": whole(h.get("t1_quantity")), "avg_price": rupees(h.get("average_price")),
                        "last_price": rupees(h.get("last_price")), "pnl": rupees(h.get("pnl")), "product": "CNC"})
        return out

    def positions(self):
        out = []
        for p in self._data(self.call("GET", "api", "/v2/portfolio/short-term-positions")) or []:
            ex = p.get("exchange")
            out.append({"symbol": plain_symbol(p.get("trading_symbol") or p.get("tradingsymbol")), "exchange": ex,
                        "product": product_from(p.get("product"), ex), "qty": whole(p.get("quantity")),
                        "avg_price": rupees(p.get("average_price")), "last_price": rupees(p.get("last_price")),
                        "pnl": rupees(p.get("pnl")), "realised": rupees(p.get("realised")),
                        "unrealised": rupees(p.get("unrealised")),
                        "buy_qty": whole(p.get("day_buy_quantity")) + whole(p.get("overnight_buy_quantity")),
                        "sell_qty": whole(p.get("day_sell_quantity")) + whole(p.get("overnight_sell_quantity"))})
        return out

    def orders(self):
        out = []
        for o in self._data(self.call("GET", "api", "/v2/order/retrieve-all")) or []:
            ex = o.get("exchange")
            out.append({"broker_order_id": str(o.get("order_id")),
                        "symbol": plain_symbol(o.get("trading_symbol") or o.get("tradingsymbol")), "exchange": ex,
                        "side": o.get("transaction_type"), "qty": whole(o.get("quantity")),
                        "filled_qty": whole(o.get("filled_quantity")), "pending_qty": whole(o.get("pending_quantity")),
                        "order_type": o.get("order_type"), "product": product_from(o.get("product"), ex),
                        "price": rupees(o.get("price")), "trigger_price": rupees(o.get("trigger_price")),
                        "avg_price": rupees(o.get("average_price")),
                        "status": STATUS.get(str(o.get("status") or "").lower(), "open"),
                        "status_message": o.get("status_message") or "", "tag": o.get("tag"),
                        "time": parse_ist(o.get("order_timestamp"))})
        return out

    def trades(self):
        out = []
        for t in self._data(self.call("GET", "api", "/v2/order/trades/get-trades-for-day")) or []:
            ex = t.get("exchange")
            out.append({"trade_id": str(t.get("trade_id")), "broker_order_id": str(t.get("order_id")),
                        "symbol": plain_symbol(t.get("trading_symbol") or t.get("tradingsymbol")), "exchange": ex,
                        "side": t.get("transaction_type"), "qty": whole(t.get("quantity")),
                        "price": rupees(t.get("average_price")), "product": product_from(t.get("product"), ex),
                        "time": parse_ist(t.get("exchange_timestamp") or t.get("order_timestamp"))})
        return out

    def ltp(self, instruments):
        keys = {i["broker_id"]: i for i in instruments}
        d = self._data(self.call("GET", "api", "/v3/market-quote/ltp", kind="quote",
                                 query={"instrument_key": ",".join(keys)})) or {}
        out = {}
        for v in d.values():
            inst = keys.get(v.get("instrument_token"))
            if inst:
                out[inst["symbol"]] = rupees(v.get("last_price"))
        return out

    def history(self, inst, interval, start, end):
        if interval not in INTERVALS:
            raise BrokerError("interval is one of %s" % ", ".join(sorted(INTERVALS)))
        unit, n = INTERVALS[interval]
        path = "/v3/historical-candle/%s/%s/%d/%s/%s" % (urllib.parse.quote(inst["broker_id"], safe=""), unit, n,
                                                         str(end)[:10], str(start)[:10])
        d = self._data(self.call("GET", "api", path)) or {}
        return [[parse_ist(c[0])] + [num(x) for x in c[1:6]] for c in d.get("candles") or []]

    def instruments(self):
        res = self.transport.request("GET", self.hosts["assets"].rstrip("/") +
                                     "/market-quote/instruments/exchange/NSE.json.gz", raw=True,
                                     error=self.error_from)
        data = res.data
        if data[:2] == b"\x1f\x8b":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        return parse_master(json.loads(data.decode("utf-8")))

    def registered_ips(self):
        d = self._data(self.call("GET", "api", "/v2/user/ip")) or {}
        return [x for x in (d.get("primary_ip"), d.get("secondary_ip")) if x]

    def basket_margin(self, orders):
        body = []
        for o in orders:
            inst = self.resolve(o["symbol"], o.get("exchange") or "NSE")
            row = {"instrument_key": inst["broker_id"], "quantity": int(o["qty"]),
                   "transaction_type": str(o["side"]).upper(), "product": PRODUCT_TO[str(o["product"]).upper()]}
            if num(o.get("price")):
                row["price"] = num(o.get("price"))
            body.append(row)
        d = self._data(self.call("POST", "api", "/v2/charges/margin", json_body={"instruments": body})) or {}
        req, fin = num(d.get("required_margin"), 0), num(d.get("final_margin"), 0)
        return {"total": rupees(fin), "initial": rupees(req), "hedge_benefit": rupees(req - fin),
                "orders": [{"symbol": o["symbol"], "total": rupees(m.get("total_margin"))}
                           for o, m in zip(orders, d.get("margins") or [])],
                "note": "Upstox margin for the basket (up to 20 instruments)"}

    # ---------------------------------------------------------------- write (only through a GatePass)
    def order_request(self, order, inst):
        otype = str(order["order_type"]).upper()
        body = {"quantity": int(order["qty"]), "product": PRODUCT_TO[str(order["product"]).upper()],
                "validity": str(order.get("validity") or "DAY").upper(),
                "price": float(order["price"]) if otype in ("LIMIT", "SL") else 0,
                "tag": str(order.get("tag") or ""), "instrument_token": inst["broker_id"], "order_type": otype,
                "transaction_type": str(order["side"]).upper(), "disclosed_quantity": 0,
                "trigger_price": float(order["trigger_price"]) if otype in ("SL", "SL-M") else 0,
                "is_amo": False, "slice": False}
        if otype in ("MARKET", "SL-M"):
            body["market_protection"] = -1        # the broker's automatic protection
        return "POST", "hft", "/v3/order/place", {"json_body": body}

    def placed_id(self, res):
        ids = (self._data(res) or {}).get("order_ids") or []
        return str(ids[0]) if ids else None

    def cancel_request(self, broker_order_id):
        return "DELETE", "hft", "/v3/order/cancel", {"query": {"order_id": str(broker_order_id)}}

    # ---------------------------------------------------------------- native linked order: multi-leg GTT
    linked_style = "bracket"
    linked_words = ("a multi-leg GTT: the entry (IMMEDIATE, a limit order sent at once) with its TARGET and STOPLOSS "
                    "in one order; the broker cancels the other exit when one fills")

    @staticmethod
    def _rules(entry_price, linked):
        return [{"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": round(float(entry_price), 2)},
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE",
                 "trigger_price": round(float(linked["target"]["price"]), 2)},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE",
                 "trigger_price": round(float(linked["stop"]["trigger_price"]), 2)}]

    def linked_request(self, linked, inst, last_price=None):
        if linked.get("linked") != "bracket":
            raise InvalidOrder("Upstox's native linked order is the bracket (the entry with its stop and target)")
        body = {"type": "MULTIPLE", "quantity": int(linked["qty"]), "product": PRODUCT_TO[str(linked["product"]).upper()],
                "rules": self._rules(linked["price"], linked), "instrument_token": inst["broker_id"],
                "transaction_type": str(linked["side"]).upper()}
        return "POST", "api", "/v3/order/gtt/place", {"json_body": body}

    def linked_placed_id(self, res):
        ids = (self._data(res) or {}).get("gtt_order_ids") or []
        return str(ids[0]) if ids else None

    def linked_cancel_request(self, linked_id, record=None):
        return "DELETE", "api", "/v3/order/gtt/cancel", {"json_body": {"gtt_order_id": str(linked_id)}}

    def linked_modify_requests(self, linked_id, linked, inst, record=None, last_price=None):
        entry = num(((record or {}).get("order") or {}).get("price"))
        if not entry:
            raise InvalidOrder("Upstox's GTT change needs the entry price of the original bracket (from this tool's "
                               "record of it)")
        return [("PUT", "api", "/v3/order/gtt/modify", {"json_body": {
            "type": "MULTIPLE", "quantity": int(linked["qty"]), "gtt_order_id": str(linked_id),
            "rules": self._rules(entry, linked)}})]

    def linked_book(self):
        """Upstox's GTT list. It leaves out completed GTTs (its docs say so)."""
        out = []
        for g in self._data(self.call("GET", "api", "/v3/order/gtt")) or []:
            rules = {str(r.get("strategy")).upper(): r for r in g.get("rules") or []}
            st = {k: str(r.get("status") or "").upper() for k, r in rules.items()}
            if "COMPLETED" in (st.get("TARGET"), st.get("STOPLOSS")):
                status = "completed"
            elif st.get("ENTRY") == "FAILED":
                status = "rejected"
            elif st and all(v == "CANCELLED" for v in st.values()):
                status = "cancelled"
            elif "EXPIRED" in st.values():
                status = "expired"
            else:
                status = "active"
            entry = rules.get("ENTRY") or {}
            out.append({"linked_id": str(g.get("gtt_order_id")),
                        "style": "bracket" if g.get("type") == "MULTIPLE" else "single",
                        "symbol": plain_symbol(g.get("trading_symbol")), "broker_symbol": g.get("trading_symbol"),
                        "broker_id": g.get("instrument_token"),
                        "exchange": "NFO" if g.get("exchange") == "NSE_FO" else "NSE" if g.get("exchange") == "NSE_EQ"
                        else g.get("exchange"), "product": product_from(g.get("product"), g.get("exchange")),
                        "side": str(entry.get("transaction_type") or "").upper(), "qty": whole(g.get("quantity")),
                        "entry_price": num(entry.get("trigger_price")),
                        "stop_trigger": num((rules.get("STOPLOSS") or {}).get("trigger_price")), "stop_price": None,
                        "target_price": num((rules.get("TARGET") or {}).get("trigger_price")), "status": status,
                        "raw_status": ",".join("%s %s" % kv for kv in sorted(st.items())),
                        "legs": [{"leg": {"ENTRY": "entry", "TARGET": "target", "STOPLOSS": "stop"}.get(k, k.lower()),
                                  "status": r.get("status"), "order_id": r.get("order_id")} for k, r in rules.items()],
                        "tag": None, "time": parse_ist(_us_time(g.get("created_at")))})
        return out

    # ---------------------------------------------------------------- login
    def redirect(self):
        return self.env_get("redirect") or self.default_redirect

    def login_start(self, state):
        self.need("api_key", "api_secret")
        q = urllib.parse.urlencode({"response_type": "code", "client_id": self.env_get("api_key"),
                                    "redirect_uri": self.redirect(), "state": state})
        return self.hosts["api"].rstrip("/") + "/v2/login/authorization/dialog?" + q, "code", "state"

    def login_finish(self, params):
        res = self.transport.request("POST", self.hosts["api"].rstrip("/") + "/v2/login/authorization/token",
                                     kind="auth", error=self.error_from,
                                     form={"code": params["code"], "client_id": self.env_get("api_key"),
                                           "client_secret": self.env_get("api_secret"),
                                           "redirect_uri": self.redirect(), "grant_type": "authorization_code"})
        tok = (res or {}).get("access_token") if isinstance(res, dict) else None
        if not tok:
            raise AuthExpired("Upstox did not return an access token")
        return tok


def parse_master(rows):
    """Upstox NSE.json.gz rows -> instrument rows for NSE stocks and NSE F&O. tick_size is in paise there."""
    out = []
    for r in rows:
        seg, itype = r.get("segment"), r.get("instrument_type")
        if seg == "NSE_EQ" and itype == "EQ":
            kind, und, exp, strike, right, exch = "EQ", r.get("trading_symbol"), None, None, None, "NSE"
        elif seg == "NSE_FO" and itype == "FUT":
            kind, und, exp, strike, right, exch = ("FUT", r.get("underlying_symbol"),
                                                   expiry_from_epoch_ms(r.get("expiry")), None, None, "NFO")
        elif seg == "NSE_FO" and itype in ("CE", "PE"):
            kind, und, exp, strike, right, exch = ("OPT", r.get("underlying_symbol"),
                                                   expiry_from_epoch_ms(r.get("expiry")), num(r.get("strike_price")),
                                                   itype, "NFO")
        else:
            continue
        tick = num(r.get("tick_size"))
        out.append({"symbol": canonical_symbol(kind, und, exp, strike, right), "exchange": exch, "kind": kind,
                    "underlying": und, "expiry": exp, "strike": strike, "right": right,
                    "lot_size": whole(r.get("lot_size"), 1), "tick_size": round(tick / 100.0, 4) if tick else None,
                    "freeze_qty": whole(r.get("freeze_quantity"), 0) or None,
                    "exchange_token": str(r.get("exchange_token") or ""), "broker_symbol": r.get("trading_symbol"),
                    "broker_id": r.get("instrument_key")})
    return out

