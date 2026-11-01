"""Dhan (DhanHQ v2) over urllib. Docs: https://dhanhq.co/docs/v2/ (read 2026-11-01).

Trading APIs are free; quotes and candles need the Data API plan (a DH-902 error otherwise,
reported as NotSubscribed). The sandbox is sandbox.dhan.co/v2: a closed environment for the API
shape and order workflow, not paper trading with live prices. Tick sizes in the scrip master are
in paise. Dhan's own docs spell the fund field "availabelBalance".
"""

import csv
import datetime
import io
import urllib.parse

from .base import (IST, Adapter, AuthExpired, BrokerDown, BrokerError, InsufficientFunds, InvalidInstrument,
                   InvalidOrder, IPNotWhitelisted, NotSubscribed, RateLimited, Rejected, canonical_symbol, ip_words,
                   mask_id, num,
                   parse_ist, rupees, whole)

STATUS = {"TRADED": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected", "EXPIRED": "cancelled"}
PRODUCT_TO = {"MIS": "INTRADAY", "CNC": "CNC", "NRML": "MARGIN"}
PRODUCT_FROM = {"INTRADAY": "MIS", "CNC": "CNC", "MARGIN": "NRML", "MTF": "CNC", "CO": "MIS", "BO": "MIS"}
TYPE_TO = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}
TYPE_FROM = {v: k for k, v in TYPE_TO.items()}
SEGMENT_TO = {"NSE": "NSE_EQ", "NFO": "NSE_FNO"}
SEGMENT_FROM = {"NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO", "MCX_COMM": "MCX"}
INTRADAY = {"1m": "1", "5m": "5", "15m": "15", "25m": "25", "60m": "60"}


class DhanAdapter(Adapter):
    name = "dhan"
    title = "Dhan"
    capabilities = {"profile": True, "funds": True, "holdings": True, "positions": True, "orders": True,
                    "quotes": True, "history": True, "instruments": True, "basket_margin": True, "gtt_oco": True,
                    "native_oco": True, "sandbox": True, "place_orders": True, "market_protection": False}
    env_names = {"client_id": "BT_DHAN_CLIENT_ID", "access_token": "BT_DHAN_ACCESS_TOKEN",
                 "api_key": "BT_DHAN_API_KEY", "api_secret": "BT_DHAN_API_SECRET", "redirect": "BT_DHAN_REDIRECT",
                 "token_date": "BT_DHAN_TOKEN_DATE", "sandbox_token": "BT_DHAN_SANDBOX_TOKEN"}
    hosts = {"api": "https://api.dhan.co/v2", "auth": "https://auth.dhan.co", "images": "https://images.dhan.co"}
    sandbox_host = "https://sandbox.dhan.co/v2"
    token_cutoff = None          # 24 hours from login; /profile says exactly when
    plans = ("Trading APIs free; Data APIs (quotes, candles, option chain, live feed) Rs 499/month "
             "(Rs 399/month billed yearly).")
    default_redirect = "http://127.0.0.1:8765/dhan"

    def __init__(self, *a, **kw):
        Adapter.__init__(self, *a, **kw)
        if self.sandbox and not (kw.get("hosts") or {}).get("api"):
            self.hosts["api"] = self.sandbox_host

    def transport_rates(self):
        return {"read_rate": 10, "quote_rate": 1}

    def client_id(self):
        self.need("client_id")
        return self.env_get("client_id")

    def auth_headers(self):
        tok = self.token()
        if not tok:
            self.need("sandbox_token" if self.sandbox else "access_token")
        return {"access-token": tok, "client-id": self.client_id()}

    def error_from(self, status, body, text):
        body = body if isinstance(body, dict) else {}
        code = body.get("errorCode") or ""
        msg = body.get("errorMessage") or body.get("message") or (text or "")[:200] or ("HTTP %s" % status)
        words = "Dhan %s: %s" % (code, msg) if code else "Dhan: %s" % msg
        if ip_words(msg):
            return IPNotWhitelisted("%s. Orders must come from the static IP set in Dhan (web.dhan.co or "
                                    "/v2/ip/setIP)" % words, code, status)
        if code == "DH-901" or (status == 401 and not code):
            return AuthExpired("%s. Make a new token: python3 ticket.py login dhan, or on web.dhan.co" % words,
                               code, status)
        if code == "DH-902":
            return NotSubscribed("%s (quotes and candles need Dhan's Data API plan)" % words, code, status)
        if code == "DH-904" or status == 429:
            return RateLimited(words, code, status)
        if code in ("DH-908", "DH-909") or status >= 500:
            return BrokerDown(words, code, status)
        low = msg.lower()
        if "insufficient" in low or "fund" in low and "not" in low:
            return InsufficientFunds(words, code, status)
        if code == "DH-907" and "security" in low:
            return InvalidInstrument(words, code, status)
        if code in ("DH-903", "DH-905", "DH-906"):
            return Rejected(words, code, status)
        return BrokerError(words, code, status)

    # ---------------------------------------------------------------- read
    def profile(self):
        d = self.call("GET", "api", "/profile") or {}
        valid = None
        try:
            valid = datetime.datetime.strptime(str(d.get("tokenValidity")), "%d/%m/%Y %H:%M").replace(
                tzinfo=IST).isoformat()
        except ValueError:
            pass
        return {"broker": self.name, "user_id": mask_id(d.get("dhanClientId")), "name": "",
                "exchanges": [s.strip() for s in str(d.get("activeSegment") or "").split(",") if s.strip()],
                "products": [], "token_valid_until": valid,
                "data_plan": str(d.get("dataPlan") or "").lower() == "active"}

    def funds(self):
        d = self.call("GET", "api", "/fundlimit") or {}
        av = d.get("availabelBalance", d.get("availableBalance"))
        used = num(d.get("utilizedAmount"), 0)
        return {"available": rupees(av), "used": rupees(used), "collateral": rupees(d.get("collateralAmount")),
                "opening": rupees(d.get("sodLimit")), "total": rupees(num(av, 0) + used)}

    def holdings(self):
        out = []
        for h in self.call("GET", "api", "/holdings") or []:
            ex = h.get("exchange")
            out.append({"symbol": h.get("tradingSymbol"), "exchange": "NSE" if ex in ("ALL", "NSE") else ex,
                        "isin": h.get("isin"), "qty": whole(h.get("totalQty")), "t1_qty": whole(h.get("t1Qty")),
                        "avg_price": rupees(h.get("avgCostPrice")), "last_price": None, "pnl": None,
                        "product": "CNC"})
        return out

    def positions(self):
        out = []
        for p in self.call("GET", "api", "/positions") or []:
            q = whole(p.get("netQty"))
            real, unreal = num(p.get("realizedProfit"), 0), num(p.get("unrealizedProfit"), 0)
            out.append({"symbol": p.get("tradingSymbol"),
                        "exchange": SEGMENT_FROM.get(p.get("exchangeSegment"), p.get("exchangeSegment")),
                        "product": PRODUCT_FROM.get(p.get("productType"), p.get("productType")), "qty": q,
                        "avg_price": rupees(p.get("buyAvg") if q >= 0 else p.get("sellAvg")), "last_price": None,
                        "pnl": rupees(real + unreal), "realised": rupees(real), "unrealised": rupees(unreal),
                        "buy_qty": whole(p.get("buyQty")), "sell_qty": whole(p.get("sellQty"))})
        return out

    def orders(self):
        out = []
        for o in self.call("GET", "api", "/orders") or []:
            out.append({"broker_order_id": str(o.get("orderId")), "symbol": o.get("tradingSymbol") or "",
                        "exchange": SEGMENT_FROM.get(o.get("exchangeSegment"), o.get("exchangeSegment")),
                        "side": o.get("transactionType"), "qty": whole(o.get("quantity")),
                        "filled_qty": whole(o.get("filledQty")), "pending_qty": whole(o.get("remainingQuantity")),
                        "order_type": TYPE_FROM.get(o.get("orderType"), o.get("orderType")),
                        "product": PRODUCT_FROM.get(o.get("productType"), o.get("productType")),
                        "price": rupees(o.get("price")), "trigger_price": rupees(o.get("triggerPrice")),
                        "avg_price": rupees(o.get("averageTradedPrice")),
                        "status": STATUS.get(str(o.get("orderStatus") or "").upper(), "open"),
                        "status_message": o.get("omsErrorDescription") or "", "tag": o.get("correlationId"),
                        "time": parse_ist(o.get("createTime"))})
        return out

    def trades(self):
        out = []
        for t in self.call("GET", "api", "/trades") or []:
            out.append({"trade_id": str(t.get("exchangeTradeId")), "broker_order_id": str(t.get("orderId")),
                        "symbol": t.get("tradingSymbol"),
                        "exchange": SEGMENT_FROM.get(t.get("exchangeSegment"), t.get("exchangeSegment")),
                        "side": t.get("transactionType"), "qty": whole(t.get("tradedQuantity")),
                        "price": rupees(t.get("tradedPrice")),
                        "product": PRODUCT_FROM.get(t.get("productType"), t.get("productType")),
                        "time": parse_ist(t.get("exchangeTime") or t.get("createTime"))})
        return out

    def ltp(self, instruments):
        body, back = {}, {}
        for i in instruments:
            seg = SEGMENT_TO[i["exchange"]]
            body.setdefault(seg, []).append(int(i["broker_id"]))
            back[(seg, str(i["broker_id"]))] = i
        d = (self.call("POST", "api", "/marketfeed/ltp", kind="quote", json_body=body) or {}).get("data") or {}
        out = {}
        for seg, rows in d.items():
            for sid, v in (rows or {}).items():
                inst = back.get((seg, str(sid)))
                if inst:
                    out[inst["symbol"]] = rupees(v.get("last_price"))
        return out

    def history(self, inst, interval, start, end):
        base = {"securityId": str(inst["broker_id"]), "exchangeSegment": SEGMENT_TO[inst["exchange"]],
                "instrument": inst.get("broker_kind") or "EQUITY", "oi": False}
        if interval == "1d":
            d = self.call("POST", "api", "/charts/historical", json_body=dict(
                base, expiryCode=0, fromDate=str(start)[:10], toDate=str(end)[:10])) or {}
        elif interval in INTRADAY:
            d = self.call("POST", "api", "/charts/intraday", json_body=dict(
                base, interval=INTRADAY[interval], fromDate=str(start)[:19], toDate=str(end)[:19])) or {}
        else:
            raise BrokerError("interval is 1d or one of %s" % ", ".join(sorted(INTRADAY)))
        ts = d.get("timestamp") or []
        out = []
        for n, t in enumerate(ts):
            when = datetime.datetime.fromtimestamp(float(t), IST).replace(microsecond=0).isoformat()
            out.append([when] + [num((d.get(k) or [None] * len(ts))[n]) for k in ("open", "high", "low", "close",
                                                                                  "volume")])
        return out

    def instruments(self):
        res = self.transport.request("GET", self.hosts["images"].rstrip("/") + "/api-data/api-scrip-master.csv",
                                     raw=True, error=self.error_from)
        return parse_master(res.text())

    def registered_ips(self):
        d = self.call("GET", "api", "/ip/getIP") or {}
        return [x for x in (d.get("primaryIP"), d.get("secondaryIP")) if x]

    def basket_margin(self, orders):
        scripts = []
        for o in orders:
            inst = self.resolve(o["symbol"], o.get("exchange") or "NSE")
            row = {"exchangeSegment": SEGMENT_TO[inst["exchange"]], "transactionType": str(o["side"]).upper(),
                   "quantity": int(o["qty"]), "productType": PRODUCT_TO[str(o["product"]).upper()],
                   "securityId": str(inst["broker_id"]), "price": num(o.get("price"), 0)}
            if num(o.get("trigger_price")):
                row["triggerPrice"] = num(o.get("trigger_price"))
            scripts.append(row)
        d = self.call("POST", "api", "/margincalculator/multi", json_body={
            "includePosition": True, "includeOrders": True, "dhanClientId": self.client_id(),
            "scripts": scripts}) or {}
        hb = num(d.get("hedge_benefit"))
        return {"total": rupees(d.get("total_margin")), "initial": None, "hedge_benefit": rupees(hb) if hb else None,
                "orders": [], "note": "Dhan multi-order margin (the docs show two spellings of the request fields; "
                                      "this sends scripts/includeOrders)"}

    # ---------------------------------------------------------------- write (only through a GatePass)
    def order_request(self, order, inst):
        otype = str(order["order_type"]).upper()
        body = {"dhanClientId": self.client_id(), "correlationId": str(order.get("tag") or "")[:25],
                "transactionType": str(order["side"]).upper(), "exchangeSegment": SEGMENT_TO[inst["exchange"]],
                "productType": PRODUCT_TO[str(order["product"]).upper()], "orderType": TYPE_TO[otype],
                "validity": str(order.get("validity") or "DAY").upper(), "securityId": str(inst["broker_id"]),
                "quantity": int(order["qty"]), "afterMarketOrder": False}
        if otype in ("LIMIT", "SL"):
            body["price"] = float(order["price"])
        if otype in ("SL", "SL-M"):
            body["triggerPrice"] = float(order["trigger_price"])
        return "POST", "api", "/orders", {"json_body": body}

    def placed_id(self, res):
        return str((res or {}).get("orderId")) if isinstance(res, dict) else None

    def cancel_request(self, broker_order_id):
        return "DELETE", "api", "/orders/%s" % urllib.parse.quote(str(broker_order_id)), {}

    # ---------------------------------------------------------------- native linked order: the Super Order
    linked_style = "bracket"
    linked_words = ("a Super Order: the entry, the target and the stop loss in one order; Dhan cancels the other exit "
                    "when one fills (the stop leg is Dhan's own stop at stopLossPrice)")

    def linked_request(self, linked, inst, last_price=None):
        if linked.get("linked") != "bracket":
            raise InvalidOrder("Dhan's native linked order is the Super Order (the entry with its stop and target)")
        body = {"dhanClientId": self.client_id(), "correlationId": str(linked.get("tag") or "")[:25],
                "transactionType": str(linked["side"]).upper(), "exchangeSegment": SEGMENT_TO[inst["exchange"]],
                "productType": PRODUCT_TO[str(linked["product"]).upper()], "orderType": "LIMIT",
                "securityId": str(inst["broker_id"]), "quantity": int(linked["qty"]),
                "price": round(float(linked["price"]), 2), "targetPrice": round(float(linked["target"]["price"]), 2),
                "stopLossPrice": round(float(linked["stop"]["trigger_price"]), 2), "trailingJump": 0}
        return "POST", "api", "/super/orders", {"json_body": body}

    def linked_placed_id(self, res):
        return str((res or {}).get("orderId")) if isinstance(res, dict) and res.get("orderId") else None

    def linked_cancel_request(self, linked_id, record=None):
        """Cancelling the ENTRY_LEG cancels every leg of the super order (Dhan's docs)."""
        return "DELETE", "api", "/super/orders/%s/ENTRY_LEG" % urllib.parse.quote(str(linked_id)), {}

    def linked_modify_requests(self, linked_id, linked, inst, record=None, last_price=None):
        oid = str(linked_id)
        path = "/super/orders/%s" % urllib.parse.quote(oid)
        return [("PUT", "api", path, {"json_body": {"dhanClientId": self.client_id(), "orderId": oid,
                                                     "legName": "TARGET_LEG",
                                                     "targetPrice": round(float(linked["target"]["price"]), 2)}}),
                ("PUT", "api", path, {"json_body": {"dhanClientId": self.client_id(), "orderId": oid,
                                                     "legName": "STOP_LOSS_LEG",
                                                     "stopLossPrice": round(float(linked["stop"]["trigger_price"]), 2),
                                                     "trailingJump": 0}})]

    def linked_book(self):
        out = []
        for o in self.call("GET", "api", "/super/orders") or []:
            legs = {str(x.get("legName")): x for x in o.get("legDetails") or []}
            st = str(o.get("orderStatus") or "").upper()
            status = {"PENDING": "active", "TRANSIT": "active", "PART_TRADED": "active", "TRADED": "active",
                      "CLOSED": "completed", "REJECTED": "rejected", "CANCELLED": "cancelled",
                      "EXPIRED": "expired"}.get(st, st.lower() or "unknown")
            out.append({"linked_id": str(o.get("orderId")), "style": "bracket", "symbol": o.get("tradingSymbol"),
                        "broker_symbol": o.get("tradingSymbol"), "broker_id": str(o.get("securityId") or ""),
                        "exchange": SEGMENT_FROM.get(o.get("exchangeSegment"), o.get("exchangeSegment")),
                        "product": PRODUCT_FROM.get(o.get("productType"), o.get("productType")),
                        "side": str(o.get("transactionType") or "").upper(), "qty": whole(o.get("quantity")),
                        "entry_price": num(o.get("price")),
                        "stop_trigger": num((legs.get("STOP_LOSS_LEG") or {}).get("price")), "stop_price": None,
                        "target_price": num((legs.get("TARGET_LEG") or {}).get("price")), "status": status,
                        "raw_status": st, "legs": [{"leg": "entry", "status": st, "order_id": str(o.get("orderId"))}] + [
                            {"leg": "stop" if k == "STOP_LOSS_LEG" else "target", "status": v.get("orderStatus"),
                             "order_id": v.get("orderId")} for k, v in sorted(legs.items())],
                        "tag": o.get("correlationId"), "time": parse_ist(o.get("createTime"))})
        return out

    # ---------------------------------------------------------------- login (API key and secret consent flow)
    def redirect(self):
        return self.env_get("redirect") or self.default_redirect

    def _app_headers(self):
        return {"app_id": self.env_get("api_key"), "app_secret": self.env_get("api_secret")}

    def login_start(self, state):
        """Dhan has no state parameter: the consent id is single use and only one callback is accepted."""
        self.need("client_id", "api_key", "api_secret")
        res = self.transport.request("POST", self.hosts["auth"].rstrip("/") + "/app/generate-consent",
                                     headers=self._app_headers(), kind="auth", error=self.error_from,
                                     query={"client_id": self.client_id()})
        cid = (res or {}).get("consentAppId") if isinstance(res, dict) else None
        if not cid:
            raise AuthExpired("Dhan did not start a login (no consentAppId)")
        return (self.hosts["auth"].rstrip("/") + "/login/consentApp-login?" +
                urllib.parse.urlencode({"consentAppId": cid}), "tokenId", None)

    def login_finish(self, params):
        res = self.transport.request("GET", self.hosts["auth"].rstrip("/") + "/app/consumeApp-consent",
                                     headers=self._app_headers(), kind="auth", error=self.error_from,
                                     query={"tokenId": params["tokenId"]})
        tok = (res or {}).get("accessToken") if isinstance(res, dict) else None
        if not tok:
            raise AuthExpired("Dhan did not return an access token")
        return tok


def parse_master(text):
    """Dhan's api-scrip-master.csv -> instrument rows for NSE stocks and NSE F&O. SEM_TICK_SIZE is in paise."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("SEM_EXM_EXCH_ID") != "NSE":
            continue
        seg, name = r.get("SEM_SEGMENT"), r.get("SEM_INSTRUMENT_NAME")
        ts = r.get("SEM_TRADING_SYMBOL") or ""
        exp = (r.get("SEM_EXPIRY_DATE") or "")[:10] or None
        if seg == "E" and name == "EQUITY" and r.get("SEM_SERIES") == "EQ":
            kind, und, exp, strike, right, exch = "EQ", ts, None, None, None, "NSE"
        elif seg == "D" and name in ("FUTIDX", "FUTSTK"):
            kind, und, strike, right, exch = "FUT", ts.split("-")[0], None, None, "NFO"
        elif seg == "D" and name in ("OPTIDX", "OPTSTK") and r.get("SEM_OPTION_TYPE") in ("CE", "PE"):
            kind, und, strike, right, exch = ("OPT", ts.split("-")[0], num(r.get("SEM_STRIKE_PRICE")),
                                              r.get("SEM_OPTION_TYPE"), "NFO")
        else:
            continue
        tick = num(r.get("SEM_TICK_SIZE"))
        out.append({"symbol": canonical_symbol(kind, und, exp, strike, right), "exchange": exch, "kind": kind,
                    "underlying": und, "expiry": exp, "strike": strike, "right": right,
                    "lot_size": whole(r.get("SEM_LOT_UNITS"), 1), "tick_size": round(tick / 100.0, 4) if tick else None,
                    "freeze_qty": None, "exchange_token": r.get("SEM_SMST_SECURITY_ID"), "broker_symbol": ts,
                    "broker_id": r.get("SEM_SMST_SECURITY_ID"), "broker_kind": name})
    return out
