"""The broker adapter contract: capabilities, broker-neutral shapes, errors, the HTTP transport
and the live-order gate. Standard library only.

Every adapter (upstox.py, kite.py, dhan.py) maps the broker's own JSON to the shapes below and
back, and nothing more. The read side works from any IP. The write side (place_order,
cancel_order) only runs with a GatePass, and the only way to get one is open_gate(), which
checks every gate for that exact order:

  1. "broker" in the config names this broker
  2. "paper" is false in the config
  3. "live_enabled" has this broker set to true in the config
  4. trader-risk passed this exact order today (its fingerprint)
  5. the user's typed yes for this exact order is recorded today by ticket.py, and that yes has
     not been used for another order
  6. ticket.py preflight passed today for this broker with the access token in use now

Shapes (plain dicts; money in rupees as floats rounded to the paisa, times in IST ISO text):

  profile   {"broker", "user_id", "name", "exchanges", "products", "token_valid_until", "data_plan"}
  funds     {"available", "used", "collateral", "opening", "total"}
  holding   {"symbol", "exchange", "isin", "qty", "t1_qty", "avg_price", "last_price", "pnl", "product"}
  position  {"symbol", "exchange", "product", "qty", "avg_price", "last_price", "pnl", "realised",
             "unrealised", "buy_qty", "sell_qty"}
  order     {"broker_order_id", "symbol", "exchange", "side", "qty", "filled_qty", "pending_qty",
             "order_type", "product", "price", "trigger_price", "avg_price", "status", "status_message",
             "tag", "time"}            status: open, filled, cancelled, rejected
  trade     {"trade_id", "broker_order_id", "symbol", "exchange", "side", "qty", "price", "product", "time"}
  candle    [time, open, high, low, close, volume]
  instrument {"symbol", "exchange", "kind", "underlying", "expiry", "strike", "right", "lot_size",
              "tick_size", "freeze_qty", "exchange_token", "broker_symbol", "broker_id"}
  margin    {"total", "orders": [{"symbol", "total"}], "hedge_benefit", "note"}

The neutral order is trader-broker's own (see SKILL.md): symbol, exchange (NSE or NFO), product
(MIS, CNC, NRML), side, qty (units, never lots), order_type (MARKET, LIMIT, SL, SL-M), price,
trigger_price, validity (DAY, IOC). An NFO symbol is written "NIFTY 2026-11-23 25000 CE" or
"NIFTY 2026-11-23 FUT" and is always resolved through the broker's own instrument master,
never built from a formula.
"""

import datetime
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
CAPABILITIES = ("profile", "funds", "holdings", "positions", "orders", "quotes", "history", "instruments",
                "basket_margin", "gtt_oco", "sandbox", "place_orders")
SEBI_MAX_OPS = 10            # orders a second before exchange algo registration is needed
MAX_ORDER_RATE = 9           # never configurable above this
DEFAULT_ORDER_RATE = 3       # orders a second, the default
READ_TIMEOUT = 15
READ_RETRIES = 3
USER_AGENT = "bharat-trader/0.2 (+https://github.com/rajdavee/bharat-trader)"


# ---------------------------------------------------------------- errors

class BrokerError(Exception):
    """A broker problem in plain words. Messages are always scrubbed of secrets."""
    kind = "error"

    def __init__(self, message, code=None, status=None):
        Exception.__init__(self, scrub(message))
        self.message = scrub(message)
        self.code = code
        self.status = status


class AuthExpired(BrokerError):
    kind = "auth_expired"


class IPNotWhitelisted(BrokerError):
    kind = "ip_not_whitelisted"


class RateLimited(BrokerError):
    kind = "rate_limited"

    def __init__(self, message, code=None, status=None, retry_after=None):
        BrokerError.__init__(self, message, code, status)
        self.retry_after = retry_after


class InsufficientFunds(BrokerError):
    kind = "insufficient_funds"


class Rejected(BrokerError):
    kind = "rejected"


class InvalidInstrument(BrokerError):
    kind = "invalid_instrument"


class NotSubscribed(BrokerError):
    """The broker plan does not include this (Kite quotes need Connect; Dhan data needs the Data API)."""
    kind = "not_subscribed"


class Unsupported(BrokerError):
    kind = "unsupported"


class BrokerDown(BrokerError):
    kind = "broker_down"


class MissingCredentials(BrokerError):
    kind = "missing_credentials"


class InvalidOrder(BrokerError):
    """Refused before any network call: the order cannot be mapped one to one."""
    kind = "invalid_order"


class GateClosed(BrokerError):
    """A live order was refused because a gate is closed. .failed lists the closed gates."""
    kind = "gate_closed"

    def __init__(self, failed):
        self.failed = list(failed)
        BrokerError.__init__(self, "no live order: " + "; ".join("%s: %s" % (g, why) for g, why in self.failed))


# ---------------------------------------------------------------- secrets never leave in text

_SECRETS = set()
_SECRETS_LOCK = threading.Lock()
SECRET_ENV = re.compile(r"^BT_[A-Z0-9_]*(KEY|SECRET|TOKEN|PIN|TOTP|PASSWORD)[A-Z0-9_]*$")
SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[redacted]"),
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?token\s+)[^\s'\"]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(token\s+)[A-Za-z0-9]{6,}:[A-Za-z0-9]{6,}"), r"\1[redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted-jwt]"),
    (re.compile(r"(?i)\b((?:access[_-]?token|api[_-]?secret|client[_-]?secret|app[_-]?secret|checksum|request_token|"
                r"tokenId|code|password|pin|totp)['\"]?\s*[=:]\s*['\"]?)[^&\s'\",}]+"), r"\1[redacted]"),
]


def remember_secret(value):
    """Scrub this exact text from every message from now on (tokens, keys, codes)."""
    if value and isinstance(value, str) and len(value) >= 6:
        with _SECRETS_LOCK:
            _SECRETS.add(value)


def remember_env_secrets(env=None):
    for k, v in (env if env is not None else os.environ).items():
        if SECRET_ENV.match(k):
            remember_secret(v)


def scrub(text):
    s = "" if text is None else str(text)
    with _SECRETS_LOCK:
        known = sorted(_SECRETS, key=len, reverse=True)
    for v in known:
        if v in s:
            s = s.replace(v, "[redacted]")
    for rx, rep in SECRET_PATTERNS:
        s = rx.sub(rep, s)
    return s


def token_hash(token):
    """A short one-way tag of a token, to tell "the same token" without keeping the token."""
    return hashlib.sha256(("bt-token:" + (token or "")).encode("utf-8")).hexdigest()[:16] if token else None


def mask_id(text):
    """AB1234 -> AB**34: enough to recognise your account, not to copy it."""
    s = str(text or "")
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ---------------------------------------------------------------- numbers and times

def num(x, default=None):
    if x is None or isinstance(x, bool) or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def rupees(x, default=0.0):
    v = num(x)
    return round(v, 2) if v is not None else default


def whole(x, default=0):
    v = num(x)
    return int(round(v)) if v is not None else default


def ist_text(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def parse_ist(text, fmts=("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S.%f",
                          "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d")):
    """Broker time text (IST unless it says otherwise) -> IST ISO text, or None."""
    if not text:
        return None
    s = str(text).strip()
    m = re.match(r"^(.*[T ]\d\d:\d\d:\d\d)([+-]\d\d)(\d\d)$", s)       # +0530 -> +05:30
    if m:
        s = "%s%s:%s" % m.groups()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return ist_text(dt)
    except ValueError:
        pass
    for f in fmts:
        try:
            return ist_text(datetime.datetime.strptime(s, f))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- the rate limiter

class RateLimiter(object):
    """A token bucket: `rate` calls a second, bursts up to `burst`. clock() and sleep() are injectable."""

    def __init__(self, rate, burst=None, clock=None, sleep=None):
        self.rate = float(rate)
        self.cap = float(burst if burst is not None else max(1.0, rate))
        self.tokens = self.cap
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.at = self.clock()
        self.lock = threading.Lock()
        self.waited = 0.0

    def take(self):
        """Wait (via sleep) until a call may go, then take one token. Returns the seconds waited."""
        waited = 0.0
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.cap, self.tokens + (now - self.at) * self.rate)
                self.at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    self.waited += waited
                    return waited
                need = (1 - self.tokens) / self.rate
            self.sleep(need)
            waited += need


def order_rate(cfg):
    """Orders a second from the config ("order_rate_per_sec", default 3), never above 9."""
    v = num((cfg or {}).get("order_rate_per_sec"), DEFAULT_ORDER_RATE)
    if v is None or v <= 0:
        v = DEFAULT_ORDER_RATE
    return min(float(v), float(MAX_ORDER_RATE))


# ---------------------------------------------------------------- HTTP

class Response(object):
    def __init__(self, status, headers, data):
        self.status = status
        self.headers = headers
        self.data = data

    def text(self):
        return self.data.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.text() or "null")
        except ValueError:
            return None


def _gunzip(data, headers):
    enc = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc or data[:2] == b"\x1f\x8b":
        try:
            return gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        except (OSError, EOFError):
            return data
    return data


class Transport(object):
    """urllib with a rate limiter per kind of call, timeouts, gzip, and retries for reads only.

    kind: "read" (GET, retried with backoff on network errors, 429 and 5xx), "quote" (GET, its own
    slower bucket), "order" (POST/PUT/DELETE that changes an order: never retried, the order bucket),
    "auth" (login calls: never retried). Every request and error is noted in .log with secrets
    scrubbed. opener() is injectable for tests."""

    def __init__(self, read_rate=5, quote_rate=1, order_rate_=DEFAULT_ORDER_RATE, timeout=READ_TIMEOUT,
                 retries=READ_RETRIES, clock=None, sleep=None, opener=None):
        sleep = sleep or time.sleep
        self.buckets = {"read": RateLimiter(read_rate, clock=clock, sleep=sleep),
                        "quote": RateLimiter(quote_rate, burst=1, clock=clock, sleep=sleep),
                        "order": RateLimiter(min(float(order_rate_), MAX_ORDER_RATE), burst=1, clock=clock,
                                             sleep=sleep),
                        "auth": RateLimiter(1, burst=2, clock=clock, sleep=sleep)}
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.sleep = sleep
        self.opener = opener or urllib.request.urlopen
        self.log = []
        self.attempts = 0

    def request(self, method, url, headers=None, query=None, json_body=None, form=None, kind="read", error=None,
                raw=False):
        """-> parsed JSON (or Response when raw). error(status, body_json, text) -> BrokerError."""
        method = method.upper()
        if query:
            pairs = []
            for k, v in (query.items() if isinstance(query, dict) else query):
                for one in (v if isinstance(v, (list, tuple)) else [v]):
                    pairs.append((k, one))
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
        data, h = None, {"Accept": "application/json", "User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        retry = kind in ("read", "quote") and method == "GET"
        attempts = self.retries if retry else 1
        bucket = self.buckets.get(kind) or self.buckets["read"]
        last = None
        for n in range(attempts):
            bucket.take()
            self.attempts += 1
            self.log.append(scrub("%s %s" % (method, url.split("?")[0])))
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                with self.opener(req, timeout=self.timeout) as r:
                    body = _gunzip(r.read(), r.headers)
                    resp = Response(r.status, r.headers, body)
                    return resp if raw else resp.json()
            except urllib.error.HTTPError as e:
                try:
                    body = _gunzip(e.read() or b"", e.headers or {})
                finally:
                    e.close()
                resp = Response(e.code, e.headers, body)
                err = (error or default_error)(e.code, resp.json(), resp.text())
                if isinstance(err, RateLimited):
                    ra = num((e.headers or {}).get("Retry-After"))
                    err.retry_after = ra
                    if retry and n + 1 < attempts:
                        self.sleep(min(ra if ra is not None else 2 ** n, 30))
                        last = err
                        continue
                    raise err
                if e.code >= 500 and retry and n + 1 < attempts:
                    self.sleep(min(0.5 * 2 ** n, 8))
                    last = err
                    continue
                raise err
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                reason = getattr(e, "reason", None) or e
                last = BrokerDown("could not reach %s (%s)" % (urllib.parse.urlsplit(url).netloc, reason))
                if retry and n + 1 < attempts:
                    self.sleep(min(0.5 * 2 ** n, 8))
                    continue
                raise last
        raise last


def default_error(status, body, text):
    msg = text[:300] if text else "HTTP %s" % status
    if status in (401, 403):
        return AuthExpired("the broker refused the login (HTTP %s): %s" % (status, msg), status=status)
    if status == 429:
        return RateLimited("too many requests (HTTP 429)", status=status)
    if status >= 500:
        return BrokerDown("the broker is not answering properly (HTTP %s)" % status, status=status)
    return BrokerError("HTTP %s: %s" % (status, msg), status=status)


def ip_words(message):
    """True when a broker's message is about the static-IP rule."""
    m = (message or "").lower()
    return ("static ip" in m or "whitelist" in m or "white list" in m or "ip address" in m
            or re.search(r"\bip\b.*(not|un)\s*(allowed|registered|whitelisted)", m) is not None)


# ---------------------------------------------------------------- the neutral order

NOT_SENT = ("why", "plan", "note")
PRODUCTS = ("MIS", "CNC", "NRML")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VALIDITIES = ("DAY", "IOC")


def order_fingerprint(order):
    """The same function as trader-risk and ticket.py: identifies the exact order."""
    core = {k: v for k, v in order.items() if k not in NOT_SENT and v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def order_tag(fp):
    """Our tag on the broker's order (alphanumeric, fits Kite's 20 characters): bt + the fingerprint."""
    return "bt" + str(fp)[:16]


def _paise(x):
    return int(round(float(x) * 100))


def validate_order(order, inst=None, live=True):
    """Check one neutral order (a single order: the entry, or one leg as its own order) before any I/O.
    Returns a list of problems; empty means it maps one to one."""
    bad = []
    side = str(order.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        bad.append("side is BUY or SELL")
    q = order.get("qty")
    if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
        bad.append("qty is a whole number of units above 0")
    otype = str(order.get("order_type") or "").upper()
    if otype not in ORDER_TYPES:
        bad.append("order_type is MARKET, LIMIT, SL or SL-M")
    product = str(order.get("product") or "").upper()
    if product not in PRODUCTS:
        bad.append("product is MIS, CNC or NRML")
    exch = str(order.get("exchange") or "").upper()
    if exch not in ("NSE", "NFO"):
        bad.append("exchange is NSE or NFO")
    if str(order.get("validity") or "DAY").upper() not in VALIDITIES:
        bad.append("validity is DAY or IOC")
    price, trig = num(order.get("price")), num(order.get("trigger_price"))
    if otype == "LIMIT" and not price:
        bad.append("a LIMIT order needs a price")
    if otype == "SL" and not (price and trig):
        bad.append("an SL order needs a price and a trigger_price")
    if otype == "SL-M" and (not trig or price):
        bad.append("an SL-M order needs a trigger_price and no price")
    if otype == "MARKET" and (price or trig):
        bad.append("a MARKET order has no price or trigger_price")
    if otype == "SL" and price and trig:
        if side == "BUY" and trig > price:
            bad.append("a BUY SL needs trigger_price at or below price")
        if side == "SELL" and trig < price:
            bad.append("a SELL SL needs trigger_price at or above price")
    if exch == "NFO" and product == "CNC":
        bad.append("CNC is for NSE stocks; F&O is MIS or NRML")
    if exch == "NSE" and product == "NRML":
        bad.append("NRML is for F&O; an NSE stock is MIS or CNC")
    if inst:
        lot = whole(inst.get("lot_size"), 1) or 1
        if isinstance(q, int) and not isinstance(q, bool) and q % lot:
            bad.append("qty %d is not a multiple of the lot size %d" % (q, lot))
        frz = whole(inst.get("freeze_qty"), 0)
        if frz and isinstance(q, int) and q > frz:
            bad.append("qty %d is above the exchange freeze quantity %d: split it into smaller orders" % (q, frz))
        tick = num(inst.get("tick_size"))
        if tick:
            t = max(_paise(tick), 1)
            for name, v in (("price", price), ("trigger_price", trig)):
                if v and _paise(v) % t:
                    bad.append("%s %.2f is not on the %.2f tick" % (name, v, tick))
    return bad


# ---------------------------------------------------------------- symbols

def canonical_symbol(kind, underlying, expiry=None, strike=None, right=None):
    """EQ -> "RELIANCE"; FUT -> "NIFTY 2026-11-23 FUT"; OPT -> "NIFTY 2026-11-23 25000 CE"."""
    if kind == "EQ":
        return underlying
    if kind == "FUT":
        return "%s %s FUT" % (underlying, expiry)
    s = num(strike)
    st = ("%d" % s) if s is not None and s == int(s) else ("%s" % s)
    return "%s %s %s %s" % (underlying, expiry, st, right)


def symbol_key(symbol):
    """Normalise what a user typed: case and spaces; "25000.0" -> "25000"."""
    parts = str(symbol or "").strip().upper().split()
    out = []
    for p in parts:
        if re.match(r"^\d+\.0+$", p):
            p = p.split(".")[0]
        out.append(p)
    return " ".join(out)


class InstrumentMaster(object):
    """One broker's instrument rows, indexed by (exchange, canonical symbol) and by exchange token."""

    def __init__(self, broker, rows, as_of=None):
        self.broker = broker
        self.rows = list(rows)
        self.as_of = as_of
        self.by_symbol = {}
        self.by_token = {}
        for r in self.rows:
            self.by_symbol[(r["exchange"], symbol_key(r["symbol"]))] = r
            if r.get("exchange_token"):
                self.by_token[(r["exchange"], str(r["exchange_token"]))] = r

    def __len__(self):
        return len(self.rows)

    def resolve(self, symbol, exchange="NSE"):
        exch = str(exchange or "NSE").upper()
        hit = self.by_symbol.get((exch, symbol_key(symbol)))
        if hit is None:
            raise InvalidInstrument("%s on %s is not in %s's instrument master%s" % (
                symbol, exch, self.broker, (" of %s" % self.as_of) if self.as_of else ""))
        return hit

    def by_exchange_token(self, exchange, token):
        return self.by_token.get((str(exchange).upper(), str(token)))


def expiry_from_epoch_ms(ms):
    if ms in (None, "", 0):
        return None
    return datetime.datetime.fromtimestamp(float(ms) / 1000.0, IST).date().isoformat()


# ---------------------------------------------------------------- the adapter

class Adapter(object):
    """The contract every broker implements. Unsupported capabilities raise Unsupported."""

    name = "base"
    title = "Base"
    capabilities = {}
    env_names = {}                 # role -> environment variable name
    hosts = {}
    token_cutoff = None            # "06:00": the token stops working at this IST time the next morning
    plans = ""                     # what the plans include, in a few words

    def __init__(self, env=None, hosts=None, transport=None, cfg=None, sandbox=False, clock=None, sleep=None):
        self.env = dict(os.environ if env is None else env)
        remember_env_secrets(self.env)
        self.hosts = dict(self.hosts)
        self.sandbox = bool(sandbox)
        if hosts:
            self.hosts.update(hosts)
        self.cfg = cfg or {}
        self.transport = transport or Transport(order_rate_=order_rate(self.cfg), clock=clock, sleep=sleep,
                                                **self.transport_rates())
        self._master = None
        self._used_passes = set()

    def transport_rates(self):
        return {"read_rate": 5, "quote_rate": 1}

    # credentials
    def env_get(self, role):
        name = self.env_names.get(role)
        v = self.env.get(name) if name else None
        v = v.strip() if isinstance(v, str) else v
        if v and role not in ("redirect", "client_id", "token_date"):
            remember_secret(v)
        return v or None

    def need(self, *roles):
        missing = [self.env_names[r] for r in roles if not self.env_get(r)]
        if missing:
            raise MissingCredentials("set %s in your shell (see docs/brokers.md); nothing was sent"
                                     % ", ".join(missing))

    def token(self):
        return self.env_get("sandbox_token" if self.sandbox else "access_token")

    def credentials_status(self):
        """{env var name: True/False}: which are set (never their values)."""
        return {name: bool(self.env.get(name)) for role, name in sorted(self.env_names.items())}

    def caps(self):
        return dict(self.capabilities)

    def require(self, cap):
        if not self.capabilities.get(cap):
            raise Unsupported("%s's API (as wired here) has no %s" % (self.title, cap.replace("_", " ")))

    # http
    def call(self, method, host, path, kind="read", **kw):
        url = self.hosts[host].rstrip("/") + path
        return self.transport.request(method, url, headers=self.auth_headers(), kind=kind, error=self.error_from,
                                      **kw)

    def auth_headers(self):
        raise NotImplementedError

    def error_from(self, status, body, text):
        return default_error(status, body, text)

    # read side: each adapter overrides what it supports
    def profile(self):
        self.require("profile")

    def funds(self):
        self.require("funds")

    def holdings(self):
        self.require("holdings")

    def positions(self):
        self.require("positions")

    def orders(self):
        self.require("orders")

    def trades(self):
        self.require("orders")

    def ltp(self, instruments):
        self.require("quotes")

    def history(self, inst, interval, start, end):
        self.require("history")

    def instruments(self):
        self.require("instruments")

    def basket_margin(self, orders):
        self.require("basket_margin")

    def registered_ips(self):
        """The static IPs the broker has on record, or None when its API cannot tell."""
        return None

    def master(self, refresh=False):
        if self._master is None or refresh:
            self._master = InstrumentMaster(self.name, self.instruments(),
                                            datetime.datetime.now(IST).date().isoformat())
        return self._master

    def resolve(self, symbol, exchange="NSE"):
        return self.master().resolve(symbol, exchange)

    # write side: only with a GatePass from open_gate()
    def order_request(self, order, inst):
        """(method, host, path, payload) for this neutral order: the exact request, for tests and previews."""
        raise NotImplementedError

    def place_order(self, order, gate_pass):
        self._check_pass(order, gate_pass)
        self.require("place_orders")
        inst = self.resolve(order["symbol"], order.get("exchange") or "NSE")
        bad = validate_order(order, inst) + self.live_policy(order)
        if bad:
            raise InvalidOrder("cannot send this order as it is: " + "; ".join(bad))
        method, host, path, payload = self.order_request(dict(order, tag=gate_pass.tag), inst)
        res = self.call(method, host, path, kind="order", **payload)
        return self.placed_id(res)

    def cancel_order(self, broker_order_id, gate_pass):
        self._check_pass(None, gate_pass, cancel=broker_order_id)
        self.require("place_orders")
        method, host, path, payload = self.cancel_request(broker_order_id)
        self.call(method, host, path, kind="order", **payload)
        return True

    def cancel_request(self, broker_order_id):
        raise NotImplementedError

    def placed_id(self, res):
        raise NotImplementedError

    def live_policy(self, order):
        """Live orders are limit or protected: MARKET and SL-M only where the broker applies market
        protection (then sent with automatic protection); otherwise refused."""
        otype = str(order.get("order_type") or "").upper()
        if otype in ("MARKET", "SL-M") and not self.capabilities.get("market_protection"):
            return ["%s orders are not sent to %s from here: since 2026-04-01 exchanges block unprotected market "
                    "orders from APIs, and this adapter has no market protection setting for %s. Use a LIMIT "
                    "(or SL) order" % (otype, self.title, self.title)]
        return []

    def _check_pass(self, order, gate_pass, cancel=None):
        if not isinstance(gate_pass, GatePass) or not hmac.compare_digest(gate_pass._seal, _seal(gate_pass)):
            raise GateClosed([("gate", "no live order without a pass from open_gate()")])
        if gate_pass.broker != self.name:
            raise GateClosed([("broker", "the pass is for %s, not %s" % (gate_pass.broker, self.name))])
        if cancel is not None:
            if gate_pass.cancel != str(cancel):
                raise GateClosed([("gate", "the pass is not for cancelling order %s" % cancel)])
        elif gate_pass.order_fp is None or gate_pass.order_fp != order_fingerprint(order):
            raise GateClosed([("risk", "the pass is for another order (ticket %s)" % gate_pass.fingerprint)])
        if gate_pass.nonce in self._used_passes or gate_pass.nonce in _USED:
            raise GateClosed([("yes", "this pass was used already: one yes, one order")])
        self._used_passes.add(gate_pass.nonce)
        _USED.add(gate_pass.nonce)


# ---------------------------------------------------------------- the gate

_KEY = secrets.token_bytes(32)
_USED = set()
PREFLIGHT_MAX_HOURS = 12


class GatePass(object):
    """Single use. fingerprint: the ticket's (what trader-risk passed and the user said yes to);
    order_fp: the one order this pass may send (the entry, or one exit leg of that ticket)."""
    __slots__ = ("broker", "fingerprint", "order_fp", "leg", "tag", "nonce", "cancel", "_seal")

    def __init__(self, broker, fingerprint, order_fp=None, leg=None, cancel=None):
        self.broker = broker
        self.fingerprint = fingerprint
        self.order_fp = order_fp
        self.leg = leg
        self.tag = (order_tag(fingerprint)[:16] + {"entry": "e", "stop": "s", "target": "t"}.get(leg, "")
                    if fingerprint else None)
        self.nonce = secrets.token_hex(8)
        self.cancel = str(cancel) if cancel is not None else None
        self._seal = _seal(self)


def _seal(p):
    msg = "%s|%s|%s|%s|%s|%s" % (p.broker, p.fingerprint, p.order_fp, p.leg, p.nonce, p.cancel)
    return hmac.new(_KEY, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_broker(name):
    n = str(name or "").strip().lower()
    return {"zerodha": "kite", "kiteconnect": "kite"}.get(n, n)


def gate_checks(broker, cfg, fp, risk_doc, state, preflight_doc, current_token, today, now, approval=None):
    """[(gate, ok, why)] for one live order (or one cancel when fp is None and approval names the
    order). Pure: no I/O. Every gate must be ok."""
    cfg = cfg or {}
    out = []
    want = canonical_broker(cfg.get("broker") or "paper")
    out.append(("broker", want == broker,
                "config broker is %r" % (cfg.get("broker") or "paper") if want != broker
                else "config broker is %s" % broker))
    paper = cfg.get("paper", True)
    out.append(("paper", paper is False, "config paper is %r (it must be false for a live order)" % paper
                if paper is not False else "config paper is false"))
    le = cfg.get("live_enabled")
    on = isinstance(le, dict) and le.get(broker) is True
    out.append(("live_enabled", on, "config live_enabled.%s is true" % broker if on
                else "config live_enabled needs {\"%s\": true}" % broker))
    if fp is not None:
        rv = None
        if isinstance(risk_doc, dict) and risk_doc.get("date") == today:
            for r in risk_doc.get("results") or []:
                if r.get("fingerprint") == fp:
                    rv = r
        ok = bool(rv) and rv.get("verdict") == "PASS"
        out.append(("risk", ok, "trader-risk PASS today for %s" % fp if ok else
                    ("trader-risk did not pass this exact order today" if rv else
                     "trader-risk has no verdict for this exact order today (run check.py)")))
    recs = [r for r in (state or {}).get("orders", []) if r.get("date") == today and
            (fp is None or r.get("fingerprint") == fp)]
    yes = approval if approval is not None else next(
        (r for r in reversed(recs) if r.get("status") == "approved" and r.get("broker") == broker), None)
    ok = bool(yes) and bool(str(yes.get("yes_text") or "").strip()) and yes.get("status") == "approved"
    out.append(("yes", ok, "your typed yes is recorded: \"%s\"" % yes.get("yes_text") if ok else
                "no typed yes recorded today for this order with %s (ticket.py confirm)" % broker))
    pf = (preflight_doc or {}).get(broker) if isinstance(preflight_doc, dict) else None
    ok, why = False, "ticket.py preflight %s has not passed today" % broker
    if isinstance(pf, dict) and pf.get("passed") and pf.get("date") == today:
        at = None
        try:
            at = datetime.datetime.fromisoformat(pf.get("at"))
        except (TypeError, ValueError):
            pass
        if pf.get("token_hash") != token_hash(current_token):
            why = "the access token changed since the preflight: run ticket.py preflight %s again" % broker
        elif at is None or (now - at).total_seconds() > PREFLIGHT_MAX_HOURS * 3600 or (now - at).total_seconds() < -300:
            why = "the preflight is older than %d hours: run it again" % PREFLIGHT_MAX_HOURS
        else:
            ok, why = True, "preflight passed at %s from %s" % (pf.get("at"), pf.get("public_ip"))
    out.append(("preflight", ok, why))
    return out


LEGS = ("entry", "stop", "target")


def leg_order(ticket, leg="entry"):
    """The one order a leg of a ticket sends: the entry without its legs, or the stop or the target as
    its own exit order (opposite side, same symbol, qty and product)."""
    t = {k: v for k, v in ticket.items() if k not in NOT_SENT}
    if leg == "entry":
        return {k: v for k, v in t.items() if k not in ("stop_loss", "target")}
    side = "SELL" if str(t.get("side")).upper() == "BUY" else "BUY"
    base_ = {"symbol": t.get("symbol"), "exchange": t.get("exchange") or "NSE", "product": t.get("product"),
             "side": side, "qty": t.get("qty"), "validity": "DAY"}
    if leg == "stop":
        sl = t.get("stop_loss")
        if not isinstance(sl, dict):
            raise InvalidOrder("this ticket has no stop_loss leg")
        otype = str(sl.get("order_type") or "SL-M").upper()
        out = dict(base_, order_type=otype, trigger_price=sl.get("trigger_price"))
        if otype == "SL":
            out["price"] = sl.get("price")
        return out
    if leg == "target":
        tg = t.get("target")
        if not isinstance(tg, dict):
            raise InvalidOrder("this ticket has no target leg")
        return dict(base_, order_type="LIMIT", price=tg.get("price"))
    raise InvalidOrder("leg is entry, stop or target")


def open_gate(broker, cfg, ticket, risk_doc, state, preflight_doc, current_token, today, now, leg="entry"):
    """A single-use GatePass for one leg of this exact ticket, or GateClosed listing every closed gate."""
    if leg not in LEGS:
        raise GateClosed([("leg", "leg is entry, stop or target")])
    fp = order_fingerprint(ticket)
    checks = gate_checks(broker, cfg, fp, risk_doc, state, preflight_doc, current_token, today, now)
    failed = [(g, why) for g, ok, why in checks if not ok]
    sent = [r for r in (state or {}).get("orders", []) if r.get("date") == today and r.get("fingerprint") == fp
            and r.get("broker") == broker and r.get("live") and (r.get("leg") or "entry") == leg
            and r.get("status") in ("placed", "leg_sent", "unknown", "send_failed")]
    if sent:
        failed.append(("yes", "the %s of this order was already sent to %s today (one yes, one order; after a "
                              "refusal make a new ticket and say yes again)" % (leg, broker)))
    if failed:
        raise GateClosed(failed)
    return GatePass(broker, fp, order_fingerprint(leg_order(ticket, leg)), leg)


def open_cancel_gate(broker, cfg, broker_order_id, sent_record, state, preflight_doc, current_token, today, now):
    """A pass to cancel one order this tool sent today (the yes for that order covers its cancel)."""
    checks = gate_checks(broker, cfg, None, None, state, preflight_doc, current_token, today, now,
                         approval=dict(sent_record or {}, status="approved") if sent_record else None)
    failed = [(g, why) for g, ok, why in checks if not ok]
    if not sent_record or str(sent_record.get("broker_order_id")) != str(broker_order_id):
        failed.append(("order", "order %s was not sent by this tool today" % broker_order_id))
    if failed:
        raise GateClosed(failed)
    return GatePass(broker, sent_record.get("fingerprint"), cancel=broker_order_id)
