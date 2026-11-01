"""Angel One SmartAPI over urllib. Docs: https://smartapi.angelone.in/docs (read 2026-11-01). Written against
the published docs only: no code from any SDK.

Login is direct, with no browser redirect: your client code, your PIN and a 6-digit TOTP code go to
loginByPassword and a JWT comes back that works until midnight IST. The TOTP code is computed from your
TOTP secret when BT_ANGEL_TOTP_SECRET is set, or typed by you from your authenticator app. Trading and
data APIs are free. Orders, GTT and their cancels are accepted only from the static IP registered against
a static-IP API key (smartapi.angelone.in, "New Login"); SmartAPI has no API to read that IP back, so the
preflight compares this machine's IP with "static_ip" in your config. The rate limit is per client code
across every process. Errors often come back as HTTP 200 with "status": false; they are raised as errors.
Tick sizes in the scrip master are in paise and option strikes in paise too.
"""

import datetime
import json
import re

from .base import (Adapter, AuthExpired, BrokerDown, BrokerError, InsufficientFunds, InvalidInstrument,
                   IPNotWhitelisted, MissingCredentials, RateLimited, Rejected, canonical_symbol, ip_words, mask_id,
                   num, parse_ist, rupees, whole)

PREFIX = "/rest/secure/angelbroking"
STATUS = {"complete": "filled", "cancelled": "cancelled", "rejected": "rejected"}
PRODUCT_TO = {"MIS": "INTRADAY", "CNC": "DELIVERY", "NRML": "CARRYFORWARD"}
PRODUCT_FROM = {"INTRADAY": "MIS", "DELIVERY": "CNC", "CARRYFORWARD": "NRML", "MARGIN": "CNC", "BO": "MIS"}
TYPE_TO = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
TYPE_FROM = {v: k for k, v in TYPE_TO.items()}
INTERVALS = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE", "10m": "TEN_MINUTE",
             "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE", "60m": "ONE_HOUR", "1d": "ONE_DAY"}
AUTH_CODES = {"AG8001", "AG8002", "AG8003", "AB8050", "AB8051", "AB1010", "AB1011"}
INSTRUMENT_CODES = {"AB1009", "AB1018"}
DOWN_CODES = {"AB1004", "AB2000", "AB2001"}
TIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
LOCAL_IP = "127.0.0.1"
NO_MAC = "00:00:00:00:00:00"     # SmartAPI asks for a MAC address header; this tool never sends your real one


def angel_time(text):
    s = str(text or "").strip()
    if not s:
        return None
    for f in TIME_FORMATS:
        try:
            return parse_ist(datetime.datetime.strptime(s, f).isoformat())
        except ValueError:
            continue
    return parse_ist(s)


def _when(x, default_time):
    """SmartAPI's candle dates are "yyyy-MM-dd HH:mm"; a bare date gets the session's open or close."""
    s = str(x).replace("T", " ")
    return s[:16] if len(s) > 10 else s[:10] + " " + default_time


def plain_symbol(s):
    s = str(s or "")
    return s[:-3] if s.endswith("-EQ") else s


def word_list(v):
    """SmartAPI's docs show exchanges and products both as a list and as list-looking text."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return re.findall(r"[A-Z][A-Z0-9_]*", str(v or ""))


class AngelAdapter(Adapter):
    name = "angel"
    title = "Angel One"
    capabilities = {"profile": True, "funds": True, "holdings": True, "positions": True, "orders": True,
                    "quotes": True, "history": True, "instruments": True, "basket_margin": True, "gtt_oco": False,
                    "native_oco": False, "sandbox": False, "place_orders": True, "market_protection": False}
    env_names = {"api_key": "BT_ANGEL_API_KEY", "client_id": "BT_ANGEL_CLIENT_CODE", "pin": "BT_ANGEL_PIN",
                 "totp_secret": "BT_ANGEL_TOTP_SECRET", "access_token": "BT_ANGEL_ACCESS_TOKEN",
                 "token_date": "BT_ANGEL_TOKEN_DATE"}
    hosts = {"api": "https://apiconnect.angelone.in", "master": "https://margincalculator.angelbroking.com"}
    token_cutoff = "00:00"         # SmartAPI sessions end at midnight IST
    plans = "Trading and data APIs are free (quotes, candles and the live feed included)."
    linked_words = ("no native linked stop and target: SmartAPI's GTT rules are single-leg, so the exits go leg by "
                    "leg")
    login_kind = "totp"

    def transport_rates(self):
        return {"read_rate": 3, "quote_rate": 1}

    # credentials
    def client_code(self):
        self.need("client_id")
        return self.env_get("client_id")

    def public_ip(self):
        from .auth import static_ips
        ips = static_ips(self.cfg, self.name)
        return ips[0] if ips else LOCAL_IP

    def base_headers(self):
        self.need("api_key")
        return {"X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": LOCAL_IP,
                "X-ClientPublicIP": self.public_ip(), "X-MACAddress": NO_MAC, "X-PrivateKey": self.env_get("api_key")}

    def auth_headers(self):
        self.need("api_key", "access_token")
        return dict(self.base_headers(), Authorization="Bearer %s" % self.token())

    def error_from(self, status, body, text):
        body = body if isinstance(body, dict) else {}
        code = str(body.get("errorcode") or body.get("errorCode") or "")
        msg = body.get("message") or (text or "")[:200] or ("HTTP %s" % status)
        words = "Angel One %s: %s" % (code, msg) if code else "Angel One: %s" % msg
        low = str(msg).lower()
        if "exceeding" in low and "rate" in low or status == 429:
            return RateLimited(words, code, status)
        if ip_words(msg):
            return IPNotWhitelisted("%s. Orders must come from the static IP registered against your SmartAPI key"
                                    % words, code, status)
        if code in AUTH_CODES or status == 401 or "invalid token" in low:
            return AuthExpired("%s. Log in again: python3 ticket.py login angel" % words, code, status)
        if code in INSTRUMENT_CODES:
            return InvalidInstrument(words, code, status)
        if "insufficient" in low:
            return InsufficientFunds(words, code, status)
        if code in DOWN_CODES or status >= 500:
            return BrokerDown(words, code, status)
        if status in (200, 400, 403, 422):
            return Rejected(words, code, status)
        return BrokerError(words, code, status)

    def call(self, method, host, path, kind="read", **kw):
        res = Adapter.call(self, method, host, path, kind=kind, **kw)
        if isinstance(res, dict) and (res.get("status") is False or str(res.get("status")).lower() == "false"):
            raise self.error_from(200, res, json.dumps(res)[:200])
        return res

    @staticmethod
    def _data(res):
        return (res or {}).get("data") if isinstance(res, dict) else None

    def get(self, path, kind="read"):
        return self._data(self.call("GET", "api", PREFIX + path, kind=kind))

    def post(self, path, body, kind="read"):
        return self._data(self.call("POST", "api", PREFIX + path, kind=kind, json_body=body))

    # ---------------------------------------------------------------- read
    def profile(self):
        d = self.get("/user/v1/getProfile") or {}
        return {"broker": self.name, "user_id": mask_id(d.get("clientcode")), "name": d.get("name") or "",
                "exchanges": sorted(word_list(d.get("exchanges"))),
                "products": [PRODUCT_FROM.get(p, p) for p in word_list(d.get("products"))],
                "token_valid_until": None, "data_plan": True}

    def funds(self):
        d = self.get("/user/v1/getRMS") or {}
        av, used = num(d.get("net"), 0), num(d.get("utiliseddebits"), 0)
        return {"available": rupees(av), "used": rupees(used), "collateral": rupees(d.get("collateral")),
                "opening": rupees(d.get("availablecash")), "total": rupees(av + used)}

    def holdings(self):
        d = self.get("/portfolio/v1/getHolding") or []
        rows = d.get("holdings") if isinstance(d, dict) else d
        out = []
        for h in rows or []:
            out.append({"symbol": plain_symbol(h.get("tradingsymbol")), "exchange": h.get("exchange"),
                        "isin": h.get("isin"), "qty": whole(h.get("quantity")), "t1_qty": whole(h.get("t1quantity")),
                        "avg_price": rupees(h.get("averageprice")), "last_price": rupees(h.get("ltp"), None),
                        "pnl": rupees(h.get("profitandloss"), None), "product": "CNC"})
        return out

    def positions(self):
        out = []
        for p in self.get("/order/v1/getPosition") or []:
            q = whole(p.get("netqty"))
            avg = p.get("buyavgprice") if q > 0 else p.get("sellavgprice") if q < 0 else p.get("avgnetprice")
            out.append({"symbol": plain_symbol(p.get("tradingsymbol")), "exchange": p.get("exchange"),
                        "product": PRODUCT_FROM.get(p.get("producttype"), p.get("producttype")), "qty": q,
                        "avg_price": rupees(avg), "last_price": rupees(p.get("ltp"), None),
                        "pnl": rupees(p.get("pnl"), None), "realised": rupees(p.get("realised"), None),
                        "unrealised": rupees(p.get("unrealised"), None),
                        "buy_qty": whole(p.get("buyqty")) + whole(p.get("cfbuyqty")),
                        "sell_qty": whole(p.get("sellqty")) + whole(p.get("cfsellqty"))})
        return out

    def orders(self):
        out = []
        for o in self.get("/order/v1/getOrderBook") or []:
            st = str(o.get("orderstatus") or o.get("status") or "").lower()
            out.append({"broker_order_id": str(o.get("orderid")), "symbol": plain_symbol(o.get("tradingsymbol")),
                        "exchange": o.get("exchange"), "side": str(o.get("transactiontype") or "").upper(),
                        "qty": whole(o.get("quantity")), "filled_qty": whole(o.get("filledshares")),
                        "pending_qty": whole(o.get("unfilledshares")),
                        "order_type": TYPE_FROM.get(o.get("ordertype"), o.get("ordertype")),
                        "product": PRODUCT_FROM.get(o.get("producttype"), o.get("producttype")),
                        "price": rupees(o.get("price")), "trigger_price": rupees(o.get("triggerprice")),
                        "avg_price": rupees(o.get("averageprice")), "status": STATUS.get(st, "open"),
                        "status_message": o.get("text") or "", "tag": o.get("ordertag") or None,
                        "time": angel_time(o.get("updatetime") or o.get("exchtime"))})
        return out

    def trades(self):
        out = []
        for t in self.get("/order/v1/getTradeBook") or []:
            out.append({"trade_id": str(t.get("fillid")), "broker_order_id": str(t.get("orderid")),
                        "symbol": plain_symbol(t.get("tradingsymbol")), "exchange": t.get("exchange"),
                        "side": str(t.get("transactiontype") or "").upper(), "qty": whole(t.get("fillsize")),
                        "price": rupees(t.get("fillprice")),
                        "product": PRODUCT_FROM.get(t.get("producttype"), t.get("producttype")),
                        "time": angel_time(t.get("filltime")) if len(str(t.get("filltime") or "")) > 8 else None})
        return out

    def ltp(self, instruments):
        body, back = {}, {}
        for i in instruments:
            body.setdefault(i["exchange"], []).append(str(i["broker_id"]))
            back[(i["exchange"], str(i["broker_id"]))] = i
        d = self.post("/market/v1/quote/", {"mode": "LTP", "exchangeTokens": body}, kind="quote") or {}
        out = {}
        for v in d.get("fetched") or []:
            inst = back.get((v.get("exchange"), str(v.get("symbolToken"))))
            if inst:
                out[inst["symbol"]] = rupees(v.get("ltp"))
        return out

    def history(self, inst, interval, start, end):
        if interval not in INTERVALS:
            raise BrokerError("interval is one of %s" % ", ".join(sorted(INTERVALS)))
        body = {"exchange": inst["exchange"], "symboltoken": str(inst["broker_id"]), "interval": INTERVALS[interval],
                "fromdate": _when(start, "09:15"), "todate": _when(end, "15:30")}
        d = self.post("/historical/v1/getCandleData", body) or []
        return [[parse_ist(c[0])] + [num(x) for x in c[1:6]] for c in d]

    def instruments(self):
        url = self.hosts["master"].rstrip("/") + "/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = self.transport.request("GET", url, raw=True, error=self.error_from)
        return parse_master(json.loads(res.text()))

    def registered_ips(self):
        return None          # SmartAPI cannot tell: the preflight compares with your config's static_ip

    def basket_margin(self, orders):
        rows = []
        for o in orders:
            inst = self.resolve(o["symbol"], o.get("exchange") or "NSE")
            rows.append({"exchange": inst["exchange"], "qty": int(o["qty"]), "price": num(o.get("price"), 0),
                         "productType": PRODUCT_TO[str(o["product"]).upper()], "token": str(inst["broker_id"]),
                         "tradeType": str(o["side"]).upper(),
                         "orderType": TYPE_TO.get(str(o.get("order_type") or "LIMIT").upper(), "LIMIT")})
        d = self.post("/margin/v1/batch", {"positions": rows}) or {}
        comp = d.get("marginComponents") or {}
        hb = num(comp.get("marginBenefit"))
        return {"total": rupees(d.get("totalMarginRequired")), "initial": None,
                "hedge_benefit": rupees(hb) if hb else None, "orders": [],
                "note": "Angel One margin calculator for the basket (up to 50 positions)"}

    # ---------------------------------------------------------------- write (only through a GatePass)
    def order_request(self, order, inst):
        otype = str(order["order_type"]).upper()
        body = {"variety": "STOPLOSS" if otype in ("SL", "SL-M") else "NORMAL", "tradingsymbol": inst["broker_symbol"],
                "symboltoken": str(inst["broker_id"]), "transactiontype": str(order["side"]).upper(),
                "exchange": inst["exchange"], "ordertype": TYPE_TO[otype],
                "producttype": PRODUCT_TO[str(order["product"]).upper()],
                "duration": str(order.get("validity") or "DAY").upper(),
                "price": "%.2f" % float(order["price"]) if otype in ("LIMIT", "SL") else "0",
                "triggerprice": "%.2f" % float(order["trigger_price"]) if otype in ("SL", "SL-M") else "0",
                "squareoff": "0", "stoploss": "0", "quantity": str(int(order["qty"]))}
        if order.get("tag"):
            body["ordertag"] = str(order["tag"])[:19]        # AB4008: under 20 characters
        return "POST", "api", PREFIX + "/order/v1/placeOrder", {"json_body": body}

    def placed_id(self, res):
        return str((self._data(res) or {}).get("orderid"))

    def cancel_request(self, broker_order_id, variety="NORMAL"):
        return "POST", "api", PREFIX + "/order/v1/cancelOrder", {"json_body": {"variety": variety,
                                                                             "orderid": str(broker_order_id)}}

    def cancel_order(self, broker_order_id, gate_pass):
        """SmartAPI's cancel names the order's variety (NORMAL or STOPLOSS): read it from the order book first."""
        self._check_pass(None, gate_pass, cancel=broker_order_id)
        self.require("place_orders")
        variety = "NORMAL"
        for o in self.get("/order/v1/getOrderBook") or []:
            if str(o.get("orderid")) == str(broker_order_id) and o.get("variety"):
                variety = str(o["variety"]).upper()
        method, host, path, payload = self.cancel_request(broker_order_id, variety)
        self.call(method, host, path, kind="order", **payload)
        return True

    # ---------------------------------------------------------------- login: client code + PIN + TOTP
    def login_direct(self, code):
        """POST loginByPassword with the client code and PIN from the environment and a 6-digit TOTP code.
        -> the JWT (the refresh and feed tokens that come back are dropped, never printed or kept)."""
        if not self.env_get("client_id") or not self.env_get("pin") or not self.env_get("api_key"):
            missing = [self.env_names[r] for r in ("api_key", "client_id", "pin") if not self.env_get(r)]
            raise MissingCredentials("set %s in your shell (see docs/brokers.md); nothing was sent" % ", ".join(missing))
        from .auth import clean_code, remember_secret
        code = clean_code(code)
        remember_secret(code)
        res = self.transport.request("POST", self.hosts["api"].rstrip("/") +
                                     "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self.base_headers(),
                                     kind="auth", error=self.error_from,
                                     json_body={"clientcode": self.env_get("client_id"), "password": self.env_get("pin"),
                                                "totp": code})
        if isinstance(res, dict) and (res.get("status") is False or str(res.get("status")).lower() == "false"):
            raise self.error_from(200, res, "")
        data = (res or {}).get("data") if isinstance(res, dict) else None
        tok = (data or {}).get("jwtToken") if isinstance(data, dict) else None
        if not tok:
            raise AuthExpired("Angel One did not return a session token")
        for k in ("jwtToken", "refreshToken", "feedToken"):
            remember_secret((data or {}).get(k))
        return tok

    def totp_code(self, at=None):
        """The current code from BT_ANGEL_TOTP_SECRET, or None when that is not set (then the user types it)."""
        sec = self.env_get("totp_secret")
        if not sec:
            return None
        from .auth import totp
        return totp(sec, at)


def _expiry(text):
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s.title(), "%d%b%Y").date().isoformat()
    except ValueError:
        return None


def parse_master(rows):
    """SmartAPI's OpenAPIScripMaster.json -> instrument rows for NSE stocks and NFO futures and options.
    tick_size and strike are in paise; freeze_qty is the first quantity refused (NSE's limit + 1)."""
    out = []
    for r in rows:
        exch, itype, sym = r.get("exch_seg"), str(r.get("instrumenttype") or ""), str(r.get("symbol") or "")
        if exch == "NSE" and sym.endswith("-EQ") and not itype:
            kind, und, exp, strike, right = "EQ", sym[:-3], None, None, None
        elif exch == "NFO" and itype in ("FUTIDX", "FUTSTK"):
            kind, und, exp, strike, right = "FUT", r.get("name"), _expiry(r.get("expiry")), None, None
        elif exch == "NFO" and itype in ("OPTIDX", "OPTSTK") and sym[-2:] in ("CE", "PE"):
            kind, und, exp, right = "OPT", r.get("name"), _expiry(r.get("expiry")), sym[-2:]
            strike = round(num(r.get("strike"), 0) / 100.0, 2)
        else:
            continue
        tick = num(r.get("tick_size"))
        frz = whole(r.get("freeze_qty"), 0)
        out.append({"symbol": canonical_symbol(kind, und, exp, strike, right), "exchange": exch, "kind": kind,
                    "underlying": und, "expiry": exp, "strike": strike, "right": right,
                    "lot_size": whole(r.get("lotsize"), 1), "tick_size": round(tick / 100.0, 4) if tick else None,
                    "freeze_qty": frz - 1 if frz > 1 else None, "exchange_token": str(r.get("token")),
                    "broker_symbol": sym, "broker_id": str(r.get("token"))})
    return out

