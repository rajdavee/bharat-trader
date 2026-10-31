"""trader-broker adapters (Upstox, Zerodha Kite, Dhan): one contract suite run against every adapter, offline.

A local stub HTTP server on 127.0.0.1 replays recorded, secret-free fixtures, so real urllib behaviour
(gzip, HTTP errors, retries, timeouts) is exercised without the network. Fixtures in
tests/fixtures/trader-broker/<broker>/ (see brokers-sources.json):

  <case>.json                  DOC-DERIVED: the example response printed in the broker's official API
                               docs (source URL in each file, read 2026-11-01), scrubbed of names, emails and
                               tokens. Where a docs example was not valid JSON the file says how it was repaired.
                               error_*.json use the documented error format and codes.
  kite/instruments.csv         REAL public instrument masters downloaded 2026-11-01, cut to the same exchange
  upstox/NSE.json.gz           tokens: RELIANCE, TCS, SBIN, INFY, HDFCBANK, M&M; NIFTY, BANKNIFTY, RELIANCE
  dhan/api-scrip-master.csv    November futures; NIFTY 25000/25500 options (weekly 3, 9 Nov; monthly 23 Nov).

The opt-in sandbox smoke tests at the end run only with BT_SANDBOX=1 and the broker's sandbox token in the
environment; they never run by default.
"""

import datetime
import glob
import gzip
import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
pkg = ticket.adapters()
base, auth = pkg.base, pkg.auth
BFIX = os.path.join(FIX, "trader-broker")
BROKERS = ("kite", "upstox", "dhan")
IST = base.IST
CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")

# fake credentials: short, so nothing here looks like a real key
ENV = {"BT_KITE_API_KEY": "kitekey11", "BT_KITE_API_SECRET": "kitesec22", "BT_KITE_ACCESS_TOKEN": "kitetok33",
       "BT_UPSTOX_API_KEY": "upkey44", "BT_UPSTOX_API_SECRET": "upsec55", "BT_UPSTOX_ACCESS_TOKEN": "uptok66",
       "BT_DHAN_CLIENT_ID": "1000000009", "BT_DHAN_ACCESS_TOKEN": "dhtok77", "BT_DHAN_API_KEY": "dhkey88",
       "BT_DHAN_API_SECRET": "dhsec99"}
TOKENS = ("kitetok33", "uptok66", "dhtok77", "kitesec22", "upsec55", "dhsec99")


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- the stub broker

class Stub(object):
    """A 127.0.0.1 HTTP server that answers like the brokers, from the fixtures. .hits records every request."""

    def __init__(self):
        self.routes, self.hits, self.lock = {}, [], threading.Lock()
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                u = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(u.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with stub.lock:
                    stub.hits.append({"method": self.command, "path": path, "query": dict(urllib.parse.parse_qsl(
                        u.query)), "headers": dict(self.headers), "body": body})
                    queue = stub.routes.get((self.command, path))
                    r = None
                    if queue:
                        r = queue[0]
                        if len(queue) > 1:
                            queue.pop(0)
                if r is None:
                    r = {"status": 404, "body": {"status": "error", "message": "no stub for %s %s"
                                                % (self.command, path)}}
                data = r.get("raw")
                if data is None:
                    data = json.dumps(r.get("body")).encode("utf-8")
                self.send_response(r.get("status", 200))
                for k, v in (r.get("headers") or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = handle_one

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def add(self, method, path, status=200, body=None, raw=None, headers=None, then=None):
        """One answer; with `then`, a queue: the first answers once, then the next ones."""
        q = [{"status": status, "body": body, "raw": raw, "headers": headers}]
        for t in then or []:
            q.append(t)
        self.routes[(method, path)] = q

    def load(self, broker):
        for f in glob.glob(os.path.join(BFIX, broker, "*.json")):
            name = os.path.basename(f)[:-5]
            if name.startswith("error_"):
                continue
            d = jload(f)
            self.add(d["request"]["method"], "/" + broker + d["request"]["path"], d["response"]["status"],
                     d["response"]["body"])
        files = {"kite": ("/kite/instruments", "instruments.csv"),
                 "upstox": ("/upstox/market-quote/instruments/exchange/NSE.json.gz", "NSE.json.gz"),
                 "dhan": ("/dhan/api-data/api-scrip-master.csv", "api-scrip-master.csv")}
        path, fname = files[broker]
        with open(os.path.join(BFIX, broker, fname), "rb") as fh:
            self.add("GET", path, raw=fh.read())

    def error(self, broker, case, path=None):
        d = jload(os.path.join(BFIX, broker, "error_%s.json" % case))
        self.add(d["request"]["method"], path or ("/" + broker + d["request"]["path"]), d["response"]["status"],
                 d["response"]["body"])

    def hosts(self, broker):
        b = self.base
        return {"kite": {"api": b + "/kite", "login": b + "/kite-login"},
                "upstox": {"api": b + "/upstox", "hft": b + "/upstox", "assets": b + "/upstox"},
                "dhan": {"api": b + "/dhan/v2", "auth": b + "/dhan", "images": b + "/dhan"}}[broker]

    def calls(self, method=None, prefix=""):
        return [h for h in self.hits if (method is None or h["method"] == method) and h["path"].startswith(prefix)]


class FakeClock(object):
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 4))
        self.t += s


def adapter(stub, broker, env=None, cfg=None, sleep=None):
    fc = FakeClock()
    return pkg.make(broker, env=dict(ENV, **(env or {})), hosts=stub.hosts(broker), cfg=cfg or {},
                    sleep=sleep or fc.sleep, clock=fc.clock)


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class StubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = Stub()
        for b in BROKERS:
            cls.stub.load(b)

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def setUp(self):
        self.stub.hits[:] = []
        for b in BROKERS:              # every test starts from the plain fixtures
            self.stub.load(b)


# ---------------------------------------------------------------- the contract, for every adapter

PROFILE_KEYS = {"broker", "user_id", "name", "exchanges", "products", "token_valid_until", "data_plan"}
FUNDS_KEYS = {"available", "used", "collateral", "opening", "total"}
HOLDING_KEYS = {"symbol", "exchange", "isin", "qty", "t1_qty", "avg_price", "last_price", "pnl", "product"}
POSITION_KEYS = {"symbol", "exchange", "product", "qty", "avg_price", "last_price", "pnl", "realised",
                 "unrealised", "buy_qty", "sell_qty"}
ORDER_KEYS = {"broker_order_id", "symbol", "exchange", "side", "qty", "filled_qty", "pending_qty", "order_type",
              "product", "price", "trigger_price", "avg_price", "status", "status_message", "tag", "time"}
TRADE_KEYS = {"trade_id", "broker_order_id", "symbol", "exchange", "side", "qty", "price", "product", "time"}
INSTRUMENT_KEYS = {"symbol", "exchange", "kind", "underlying", "expiry", "strike", "right", "lot_size",
                   "tick_size", "freeze_qty", "exchange_token", "broker_symbol", "broker_id"}

# what each doc example says, mapped to the neutral shapes
EXPECT = {
    "kite": {"available": 99725.05, "holding": ("ASHOKLEY", 1, 131.95), "position_product": "NRML",
             "order_status": "cancelled", "basket": 34786.73, "ltp_symbol": "INFY", "ltp": 1074.35,
             "opt": "NIFTY26NOV25000CE", "weekly": "NIFTY26N0925000CE", "fut": "NIFTY26NOVFUT"},
    "upstox": {"available": 15507.46, "holding": ("YESBANK", 36, 18.75), "position_product": "NRML",
               "order_status": "filled", "basket": 33.6, "ltp_symbol": "NIFTY 2025-04-30 21600 PE", "ltp": 303.9,
               "opt": "NSE_FO|64439", "weekly": "NSE_FO|40037", "fut": "NSE_FO|61471"},
    "dhan": {"available": 98440.0, "holding": ("HDFC", 1000, 2655.0), "position_product": "CNC",
             "order_status": "open", "basket": 150000.0, "ltp_symbol": "TCS", "ltp": 4520.0,
             "opt": "NIFTY-Nov2026-25000-CE", "weekly": "NIFTY-Nov2026-25000-CE", "fut": "NIFTY-Nov2026-FUT"},
}


class Contract(StubCase):
    """The same checks, run against Upstox, Kite and Dhan."""

    def each(self):
        for b in BROKERS:
            with self.subTest(broker=b):
                yield b, adapter(self.stub, b)

    def test_capability_flags_are_declared(self):
        for b, ad in self.each():
            caps = ad.caps()
            for c in base.CAPABILITIES:
                self.assertIn(c, caps)
                self.assertIsInstance(caps[c], bool)
            self.assertTrue(caps["place_orders"])
        self.assertTrue(pkg.make("upstox").caps()["sandbox"])
        self.assertTrue(pkg.make("dhan").caps()["sandbox"])
        self.assertFalse(pkg.make("kite").caps()["sandbox"])
        self.assertIs(pkg.make("zerodha").__class__, pkg.KiteAdapter)
        with self.assertRaises(base.Unsupported):
            pkg.make("angel")

    def test_profile(self):
        for b, ad in self.each():
            p = ad.profile()
            self.assertEqual(set(p), PROFILE_KEYS)
            self.assertEqual(p["broker"], b)
            self.assertIn("*", p["user_id"])            # masked, never the whole id
            self.assertIsInstance(p["exchanges"], list)
        d = adapter(self.stub, "dhan").profile()
        self.assertEqual(d["token_valid_until"], "2025-03-30T15:37:00+05:30")
        self.assertTrue(d["data_plan"])

    def test_funds(self):
        for b, ad in self.each():
            f = ad.funds()
            self.assertEqual(set(f), FUNDS_KEYS)
            self.assertEqual(f["available"], EXPECT[b]["available"])
            self.assertIsInstance(f["used"], float)

    def test_holdings(self):
        for b, ad in self.each():
            rows = ad.holdings()
            self.assertTrue(rows)
            for h in rows:
                self.assertEqual(set(h), HOLDING_KEYS)
                self.assertIsInstance(h["qty"], int)
                self.assertEqual(h["product"], "CNC")
            sym, qty, avg = EXPECT[b]["holding"]
            first = rows[0]
            self.assertEqual((first["symbol"], first["qty"], first["avg_price"]), (sym, qty, avg))

    def test_positions(self):
        for b, ad in self.each():
            rows = ad.positions()
            self.assertTrue(rows)
            for p in rows:
                self.assertEqual(set(p), POSITION_KEYS)
                self.assertIn(p["product"], ("MIS", "CNC", "NRML"))
            self.assertEqual(rows[0]["product"], EXPECT[b]["position_product"])
        tcs = adapter(self.stub, "dhan").positions()[0]
        self.assertEqual((tcs["symbol"], tcs["exchange"], tcs["qty"], tcs["avg_price"], tcs["pnl"]),
                         ("TCS", "NSE", 40, 3345.8, 6122.0))

    def test_orders_and_trades(self):
        for b, ad in self.each():
            rows = ad.orders()
            self.assertTrue(rows)
            for o in rows:
                self.assertEqual(set(o), ORDER_KEYS)
                self.assertIn(o["status"], ("open", "filled", "cancelled", "rejected"))
                self.assertIn(o["side"], ("BUY", "SELL"))
                self.assertTrue(o["time"] is None or o["time"].endswith("+05:30"))
            self.assertEqual(rows[0]["status"], EXPECT[b]["order_status"])
            for t in ad.trades():
                self.assertEqual(set(t), TRADE_KEYS)
                self.assertGreater(t["qty"], 0)
        self.assertEqual(adapter(self.stub, "upstox").trades()[0]["symbol"], "GMRINFRA")   # "-EQ" dropped

    def test_ltp(self):
        for b, ad in self.each():
            if b == "upstox":
                inst = {"symbol": EXPECT[b]["ltp_symbol"], "broker_id": "NSE_FO|51834", "exchange": "NFO"}
            else:
                inst = ad.resolve(EXPECT[b]["ltp_symbol"], "NSE")
            self.assertEqual(ad.ltp([inst]), {EXPECT[b]["ltp_symbol"]: EXPECT[b]["ltp"]})
        hit = self.stub.calls("GET", "/kite/quote/ltp")[-1]
        self.assertEqual(hit["query"], {"i": "NSE:INFY"})
        body = json.loads(self.stub.calls("POST", "/dhan/v2/marketfeed/ltp")[-1]["body"])
        self.assertEqual(body, {"NSE_EQ": [11536]})

    def test_history_candles(self):
        for b, ad in self.each():
            if b == "kite":
                inst = {"broker_id": "5633"}
                rows = ad.history(inst, "1m", "2017-12-15 09:15:00", "2017-12-15 09:20:00")
            elif b == "upstox":
                inst = {"broker_id": "NSE_EQ|INE848E01016"}
                rows = ad.history(inst, "1d", "2025-01-01", "2025-01-02")
            else:
                inst = ad.resolve("HDFCBANK")
                rows = ad.history(inst, "1d", "2022-01-08", "2022-02-08")
            self.assertTrue(rows)
            for c in rows:
                self.assertEqual(len(c), 6)
                self.assertTrue(c[0].endswith("+05:30"), c[0])
                self.assertTrue(all(isinstance(x, float) for x in c[1:5]))
        self.assertEqual(adapter(self.stub, "kite").history({"broker_id": "5633"}, "1m", "a", "b")[0],
                         ["2017-12-15T09:15:00+05:30", 1704.5, 1705.0, 1699.25, 1702.8, 2499.0])

    def test_instrument_master_and_symbol_mapping(self):
        masters = {}
        for b, ad in self.each():
            m = ad.master()
            masters[b] = m
            self.assertEqual(len(m), 20)
            for r in m.rows:
                self.assertTrue(INSTRUMENT_KEYS <= set(r))
            opt = ad.resolve("NIFTY 2026-11-23 25000 CE", "NFO")
            self.assertEqual((opt["kind"], opt["lot_size"], opt["tick_size"], opt["strike"], opt["right"]),
                             ("OPT", 65, 0.05, 25000.0, "CE"))
            key = "broker_symbol" if b in ("kite", "dhan") else "broker_id"
            self.assertEqual(opt[key], EXPECT[b]["opt"])
            weekly = ad.resolve("nifty 2026-11-09 25000.0 ce", "NFO")        # typed loosely, still found
            self.assertEqual(weekly[key], EXPECT[b]["weekly"])
            self.assertEqual(ad.resolve("NIFTY 2026-11-23 FUT", "NFO")[key], EXPECT[b]["fut"])
            self.assertEqual(ad.resolve("RELIANCE 2026-11-23 FUT", "NFO")["lot_size"], 500)
            self.assertEqual(ad.resolve("M&M")["tick_size"], 0.1)
            self.assertEqual(ad.resolve("SBIN")["tick_size"], 0.05)          # paise in Upstox and Dhan masters
            with self.assertRaises(base.InvalidInstrument):
                ad.resolve("NIFTY 2026-11-23 25100 CE", "NFO")
            with self.assertRaises(base.InvalidInstrument):
                ad.resolve("RELIANCE", "NFO")
        # the same contract everywhere: one join key, the exchange token
        for sym in ("NIFTY 2026-11-23 25000 CE", "BANKNIFTY 2026-11-23 FUT", "TCS"):
            exch = "NSE" if sym == "TCS" else "NFO"
            toks = {b: masters[b].resolve(sym, exch)["exchange_token"] for b in BROKERS}
            self.assertEqual(len(set(toks.values())), 1, toks)
        self.assertEqual(masters["upstox"].resolve("NIFTY 2026-11-23 25000 CE", "NFO")["freeze_qty"], 1755)
        self.assertEqual(masters["kite"].resolve("TCS")["broker_id"], "2953217")

    def test_basket_margin(self):
        for b, ad in self.each():
            m = ad.basket_margin([{"symbol": "NIFTY 2026-11-23 25000 CE", "exchange": "NFO", "product": "NRML",
                                   "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 120.5}])
            self.assertEqual(m["total"], EXPECT[b]["basket"])
        sent = json.loads(self.stub.calls("POST", "/upstox/v2/charges/margin")[-1]["body"])
        self.assertEqual(sent, {"instruments": [{"instrument_key": "NSE_FO|64439", "quantity": 65,
                                                 "transaction_type": "SELL", "product": "D", "price": 120.5}]})
        sent = json.loads(self.stub.calls("POST", "/kite/margins/basket")[-1]["body"])
        self.assertEqual(sent[0]["tradingsymbol"], "NIFTY26NOV25000CE")

    def test_auth_headers(self):
        for b, ad in self.each():
            ad.profile()
        h = {b: self.stub.calls("GET", "/" + b)[-1]["headers"] for b in BROKERS}
        self.assertEqual(h["kite"]["Authorization"], "token kitekey11:kitetok33")
        self.assertEqual(h["kite"]["X-Kite-Version"], "3")
        self.assertEqual(h["upstox"]["Authorization"], "Bearer uptok66")
        self.assertEqual((h["dhan"]["Access-Token"], h["dhan"]["Client-Id"]), ("dhtok77", "1000000009"))

    def test_missing_credentials_send_nothing(self):
        for b in BROKERS:
            with self.subTest(broker=b):
                ad = pkg.make(b, env={}, hosts=self.stub.hosts(b))
                n = len(self.stub.hits)
                with self.assertRaises(base.MissingCredentials) as cm:
                    ad.profile()
                self.assertIn("BT_%s_" % b.upper(), str(cm.exception))
                self.assertEqual(len(self.stub.hits), n)


class Errors(StubCase):
    def test_documented_errors_are_normalised(self):
        cases = [("kite", "token", "GET", "profile", base.AuthExpired),
                 ("kite", "margin", "POST", None, base.InsufficientFunds),
                 ("upstox", "token", "GET", "profile", base.AuthExpired),
                 ("dhan", "token", "GET", "holdings", base.AuthExpired),
                 ("dhan", "data_plan", "POST", None, base.NotSubscribed)]
        for b, case, method, fn, exc in cases:
            with self.subTest(broker=b, case=case):
                self.stub.error(b, case)
                ad = adapter(self.stub, b)
                with self.assertRaises(exc) as cm:
                    if fn:
                        getattr(ad, fn)()
                    elif b == "kite":
                        ad.call("POST", "api", "/orders/regular", kind="order", form={"x": "1"})
                    else:
                        ad.ltp([ad.resolve("TCS")])
                self.assertTrue(cm.exception.message)
        self.stub.error("upstox", "ip")
        ad = adapter(self.stub, "upstox")
        with self.assertRaises(base.IPNotWhitelisted) as cm:
            ad.call("POST", "hft", "/v3/order/place", kind="order", json_body={})
        self.assertIn("static IP", cm.exception.message)

    def test_ip_words_from_any_broker(self):
        self.stub.add("POST", "/kite/orders/regular", 403, {"status": "error", "error_type": "PermissionException",
                                                            "message": "Order placement is not allowed from IP "
                                                                       "203.0.113.9 as it is not whitelisted"})
        with self.assertRaises(base.IPNotWhitelisted):
            adapter(self.stub, "kite").call("POST", "api", "/orders/regular", kind="order", form={})
        self.stub.add("POST", "/dhan/v2/orders", 400, {"errorType": "Order_Error", "errorCode": "DH-906",
                                                      "errorMessage": "Static IP not registered for order APIs"})
        with self.assertRaises(base.IPNotWhitelisted):
            adapter(self.stub, "dhan").call("POST", "api", "/orders", kind="order", json_body={})

    def test_reads_retry_with_backoff_and_orders_never_do(self):
        down = {"status": 503, "body": {"status": "error", "message": "down"}}
        good = jload(os.path.join(BFIX, "kite", "profile.json"))["response"]
        self.stub.add("GET", "/kite/user/profile", 503, {"status": "error", "message": "down"},
                      then=[down, {"status": 200, "body": good["body"]}])
        fc = FakeClock()
        ad = adapter(self.stub, "kite", sleep=fc.sleep)
        self.assertEqual(ad.profile()["broker"], "kite")
        self.assertEqual(len(self.stub.calls("GET", "/kite/user/profile")), 3)
        self.assertIn(0.5, fc.slept)
        self.assertIn(1.0, fc.slept)
        self.stub.add("POST", "/kite/orders/regular", 503, {"status": "error", "message": "down"})
        with self.assertRaises(base.BrokerDown):
            ad.call("POST", "api", "/orders/regular", kind="order", form={"a": "b"})
        self.assertEqual(len(self.stub.calls("POST", "/kite/orders/regular")), 1)

    def test_429_is_rate_limited_after_retries(self):
        self.stub.add("GET", "/upstox/v2/user/profile", 429, {"status": "error", "errors": [
            {"errorCode": "UDAPI10005", "message": "Too Many Request Sent"}]}, headers={"Retry-After": "1"})
        fc = FakeClock()
        with self.assertRaises(base.RateLimited):
            adapter(self.stub, "upstox", sleep=fc.sleep).profile()
        self.assertEqual(len(self.stub.calls("GET", "/upstox/v2/user/profile")), base.READ_RETRIES)
        self.assertIn(1.0, fc.slept)

    def test_unreachable_is_broker_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ad = pkg.make("kite", env=ENV, hosts={"api": "http://127.0.0.1:%d" % port}, sleep=lambda x: None)
        with self.assertRaises(base.BrokerDown) as cm:
            ad.profile()
        self.assertIn("could not reach", cm.exception.message)

    def test_timeouts_apply(self):
        class Slow(object):
            def __call__(self, req, timeout=None):
                self.timeout = timeout
                raise socket.timeout("timed out")
        slow = Slow()
        t = base.Transport(opener=slow, sleep=lambda x: None, timeout=4)
        with self.assertRaises(base.BrokerDown):
            t.request("GET", "http://127.0.0.1:9/x")
        self.assertEqual(slow.timeout, 4)
        self.assertEqual(t.attempts, base.READ_RETRIES)

    def test_secrets_are_scrubbed_from_errors_and_logs(self):
        self.stub.add("GET", "/kite/user/profile", 403, {"status": "error", "error_type": "TokenException",
                                                         "message": "token kitekey11:kitetok33 is invalid"})
        ad = adapter(self.stub, "kite")
        with self.assertRaises(base.AuthExpired) as cm:
            ad.profile()
        for s in TOKENS + ("kitekey11",):
            self.assertNotIn(s, str(cm.exception))
            self.assertNotIn(s, " ".join(ad.transport.log))
        self.assertIn("[redacted]", str(cm.exception))
        self.assertEqual(base.scrub("Authorization: Bearer abcdefgh12345678"), "Authorization: Bearer [redacted]")
        self.assertEqual(base.scrub("https://x/cb?code=mk404x&state=1"), "https://x/cb?code=[redacted]&state=1")
        self.assertNotIn("eyJhbGciOi", base.scrub("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"))
        self.assertEqual(base.scrub("DH-901 errorCode stays"), "DH-901 errorCode stays")


class RateLimits(unittest.TestCase):
    def test_token_bucket_paces_calls(self):
        fc = FakeClock()
        rl = base.RateLimiter(3, burst=1, clock=fc.clock, sleep=fc.sleep)
        for _ in range(4):
            rl.take()
        self.assertEqual(len(fc.slept), 3)
        self.assertAlmostEqual(sum(fc.slept), 1.0, places=3)        # 4 calls at 3 a second take 1 second

    def test_order_rate_defaults_to_3_and_never_reaches_sebis_10(self):
        self.assertEqual(base.order_rate({}), 3.0)
        self.assertEqual(base.order_rate({"order_rate_per_sec": 5}), 5.0)
        self.assertEqual(base.order_rate({"order_rate_per_sec": 50}), 9.0)
        self.assertEqual(base.order_rate({"order_rate_per_sec": -1}), 3.0)
        self.assertLess(base.MAX_ORDER_RATE, base.SEBI_MAX_OPS)
        for b in BROKERS:
            ad = pkg.make(b, env=ENV, cfg={"order_rate_per_sec": 99})
            self.assertEqual(ad.transport.buckets["order"].rate, 9.0)
            self.assertEqual(pkg.make(b, env=ENV).transport.buckets["order"].rate, 3.0)
            self.assertEqual(ad.transport.buckets["order"].cap, 1.0)     # no bursts of orders

    def test_orders_are_paced_through_the_transport(self):
        fc = FakeClock()
        seen = []

        class Ok(object):
            status = 200
            headers = {}

            def read(self):
                return b'{"status": "success", "data": {"order_id": "1"}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        t = base.Transport(opener=lambda req, timeout=None: seen.append(req) or Ok(), clock=fc.clock, sleep=fc.sleep)
        for _ in range(7):
            t.request("POST", "http://127.0.0.1:9/orders", kind="order", form={"a": "1"})
        self.assertEqual(len(seen), 7)
        self.assertAlmostEqual(sum(fc.slept), 2.0, places=3)        # 7 orders at 3 a second: 2 seconds of waiting


# ---------------------------------------------------------------- the neutral order -> each broker's request

RELIANCE_LIMIT = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
                  "order_type": "LIMIT", "price": 1174.30, "validity": "DAY", "tag": "bt0123456789abcde"}
RELIANCE_STOP = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 42,
                 "order_type": "SL-M", "trigger_price": 1163.30, "validity": "DAY"}
NIFTY_OPT = {"symbol": "NIFTY 2026-11-23 25000 CE", "exchange": "NFO", "product": "NRML", "side": "BUY", "qty": 130,
             "order_type": "SL", "price": 121.0, "trigger_price": 120.5, "validity": "IOC"}


class OrderMapping(StubCase):
    def req(self, b, order):
        ad = adapter(self.stub, b)
        return ad.order_request(order, ad.resolve(order["symbol"], order["exchange"]))

    def test_kite_requests(self):
        m, host, path, p = self.req("kite", RELIANCE_LIMIT)
        self.assertEqual((m, host, path), ("POST", "api", "/orders/regular"))
        self.assertEqual(p["form"], {"tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "BUY",
                                     "order_type": "LIMIT", "quantity": 42, "product": "MIS", "validity": "DAY",
                                     "price": "1174.30", "tag": "bt0123456789abcde"})
        f = self.req("kite", RELIANCE_STOP)[3]["form"]
        self.assertEqual((f["order_type"], f["trigger_price"], f["market_protection"]), ("SL-M", "1163.30", -1))
        self.assertNotIn("price", f)
        f = self.req("kite", NIFTY_OPT)[3]["form"]
        self.assertEqual((f["tradingsymbol"], f["exchange"], f["product"], f["price"], f["trigger_price"],
                          f["validity"], f["quantity"]), ("NIFTY26NOV25000CE", "NFO", "NRML", "121.00", "120.50",
                                                          "IOC", 130))

    def test_upstox_requests(self):
        m, host, path, p = self.req("upstox", RELIANCE_LIMIT)
        self.assertEqual((m, host, path), ("POST", "hft", "/v3/order/place"))
        self.assertEqual(p["json_body"], {"quantity": 42, "product": "I", "validity": "DAY", "price": 1174.3,
                                          "tag": "bt0123456789abcde", "instrument_token": "NSE_EQ|INE002A01018",
                                          "order_type": "LIMIT", "transaction_type": "BUY", "disclosed_quantity": 0,
                                          "trigger_price": 0, "is_amo": False, "slice": False})
        b = self.req("upstox", RELIANCE_STOP)[3]["json_body"]
        self.assertEqual((b["order_type"], b["price"], b["trigger_price"], b["market_protection"]),
                         ("SL-M", 0, 1163.3, -1))
        b = self.req("upstox", NIFTY_OPT)[3]["json_body"]
        self.assertEqual((b["instrument_token"], b["product"], b["validity"]), ("NSE_FO|64439", "D", "IOC"))

    def test_dhan_requests(self):
        m, host, path, p = self.req("dhan", RELIANCE_LIMIT)
        self.assertEqual((m, host, path), ("POST", "api", "/orders"))
        self.assertEqual(p["json_body"], {"dhanClientId": "1000000009", "correlationId": "bt0123456789abcde",
                                          "transactionType": "BUY", "exchangeSegment": "NSE_EQ",
                                          "productType": "INTRADAY", "orderType": "LIMIT", "validity": "DAY",
                                          "securityId": "2885", "quantity": 42, "afterMarketOrder": False,
                                          "price": 1174.3})
        b = self.req("dhan", NIFTY_OPT)[3]["json_body"]
        self.assertEqual((b["exchangeSegment"], b["productType"], b["orderType"], b["securityId"], b["triggerPrice"]),
                         ("NSE_FNO", "MARGIN", "STOP_LOSS", "64439", 120.5))

    def test_cancel_requests(self):
        self.assertEqual(adapter(self.stub, "kite").cancel_request("151220000000000"),
                         ("DELETE", "api", "/orders/regular/151220000000000", {}))
        self.assertEqual(adapter(self.stub, "upstox").cancel_request("1644490272000"),
                         ("DELETE", "hft", "/v3/order/cancel", {"query": {"order_id": "1644490272000"}}))
        self.assertEqual(adapter(self.stub, "dhan").cancel_request("112111182045"),
                         ("DELETE", "api", "/orders/112111182045", {}))

    def test_invalid_orders_are_refused_before_any_io(self):
        ad = adapter(self.stub, "kite")
        opt = ad.resolve("NIFTY 2026-11-23 25000 CE", "NFO")
        rel = ad.resolve("RELIANCE")
        cases = [(dict(NIFTY_OPT, qty=64), opt, "lot size 65"),
                 (dict(NIFTY_OPT, product="CNC"), opt, "CNC is for NSE stocks"),
                 (dict(RELIANCE_LIMIT, price=1174.33), rel, "not on the 0.10 tick"),
                 (dict(RELIANCE_LIMIT, price=None), rel, "needs a price"),
                 (dict(RELIANCE_STOP, price=1160.0), rel, "no price"),
                 (dict(RELIANCE_LIMIT, qty=0), rel, "above 0"),
                 (dict(RELIANCE_LIMIT, qty=4.5), rel, "whole number"),
                 (dict(RELIANCE_LIMIT, side="HOLD"), rel, "BUY or SELL"),
                 (dict(RELIANCE_LIMIT, product="NRML"), rel, "NRML is for F&O"),
                 (dict(NIFTY_OPT, trigger_price=125.0), opt, "trigger_price at or below")]
        for order, inst, words in cases:
            with self.subTest(order=order):
                bad = base.validate_order(order, inst)
                self.assertTrue(bad)
                self.assertIn(words, " ".join(bad))
        upx = adapter(self.stub, "upstox")
        self.assertIn("freeze quantity 1755", " ".join(base.validate_order(
            dict(NIFTY_OPT, qty=1820), upx.resolve("NIFTY 2026-11-23 25000 CE", "NFO"))))
        self.assertEqual(base.validate_order(RELIANCE_LIMIT, rel), [])
        self.assertEqual(base.validate_order(NIFTY_OPT, opt), [])

    def test_live_market_orders_need_market_protection(self):
        mkt = dict(RELIANCE_LIMIT, order_type="MARKET", price=None)
        self.assertEqual(adapter(self.stub, "kite").live_policy(mkt), [])
        self.assertEqual(adapter(self.stub, "upstox").live_policy(mkt), [])
        self.assertIn("Use a LIMIT", adapter(self.stub, "dhan").live_policy(mkt)[0])
        self.assertIn("Use a LIMIT", adapter(self.stub, "dhan").live_policy(RELIANCE_STOP)[0])


# ---------------------------------------------------------------- the gate: every condition, one at a time

TODAY = "2026-10-29"
NOW = datetime.datetime(2026, 10, 29, 9, 45, tzinfo=IST)


def ticket_order():
    with open(ORDERS_PATH) as fh:
        return json.load(fh)["orders"][0]


def open_docs(broker="kite", token="kitetok33"):
    t = ticket_order()
    fp = base.order_fingerprint(t)
    cfg = {"broker": broker, "paper": False, "live_enabled": {broker: True}}
    risk = {"date": TODAY, "results": [{"fingerprint": fp, "verdict": "PASS"}]}
    state = {"orders": [{"date": TODAY, "fingerprint": fp, "status": "approved", "broker": broker,
                         "yes_text": "yes 42 RELIANCE"}]}
    pre = {broker: {"date": TODAY, "at": "2026-10-29T09:30:00+05:30", "passed": True, "public_ip": "203.0.113.7",
                    "token_hash": base.token_hash(token)}}
    return t, cfg, risk, state, pre


class Gate(StubCase):
    def gate(self, **over):
        t, cfg, risk, state, pre = open_docs()
        args = dict(broker="kite", cfg=cfg, ticket=t, risk_doc=risk, state=state, preflight_doc=pre,
                    current_token="kitetok33", today=TODAY, now=NOW)
        args.update(over)
        return base.open_gate(**args)

    def closed(self, gate, **over):
        with self.assertRaises(base.GateClosed) as cm:
            self.gate(**over)
        self.assertIn(gate, [g for g, _ in cm.exception.failed], cm.exception.failed)
        return cm.exception

    def test_all_gates_open_gives_one_pass(self):
        p = self.gate()
        self.assertEqual(p.broker, "kite")
        self.assertEqual(p.fingerprint, base.order_fingerprint(ticket_order()))
        self.assertEqual(p.order_fp, base.order_fingerprint(base.leg_order(ticket_order(), "entry")))
        self.assertTrue(p.tag.startswith("bt") and len(p.tag) <= 20 and p.tag.isalnum())

    def test_each_gate_alone_closes_it(self):
        t, cfg, risk, state, pre = open_docs()
        self.closed("broker", cfg=dict(cfg, broker="upstox"))
        self.closed("broker", cfg=dict(cfg, broker="paper"))
        self.closed("paper", cfg=dict(cfg, paper=True))
        self.closed("paper", cfg={k: v for k, v in cfg.items() if k != "paper"})     # missing means paper
        self.closed("live_enabled", cfg=dict(cfg, live_enabled={}))
        self.closed("live_enabled", cfg=dict(cfg, live_enabled={"kite": "yes"}))
        self.closed("live_enabled", cfg=dict(cfg, live_enabled={"upstox": True}))
        self.closed("risk", risk_doc=None)
        self.closed("risk", risk_doc=dict(risk, date="2026-10-28"))
        self.closed("risk", risk_doc={"date": TODAY, "results": [dict(risk["results"][0], verdict="BLOCK")]})
        self.closed("risk", ticket=dict(t, qty=41))                       # an edited order is another order
        self.closed("yes", state={"orders": []})
        self.closed("yes", state={"orders": [dict(state["orders"][0], broker="upstox")]})
        self.closed("yes", state={"orders": [dict(state["orders"][0], yes_text="  ")]})
        self.closed("yes", state={"orders": [dict(state["orders"][0], date="2026-10-28")]})
        self.closed("preflight", preflight_doc={})
        self.closed("preflight", preflight_doc={"kite": dict(pre["kite"], passed=False)})
        self.closed("preflight", preflight_doc={"kite": dict(pre["kite"], date="2026-10-28")})
        self.closed("preflight", current_token="anothertok")             # a new login needs a new preflight
        self.closed("preflight", now=NOW + datetime.timedelta(hours=13))
        self.closed("leg", leg="all")

    def test_one_yes_one_order(self):
        t, cfg, risk, state, pre = open_docs()
        sent = dict(state["orders"][0], status="placed", live=True, leg="entry")
        e = self.closed("yes", state={"orders": state["orders"] + [sent]})
        self.assertIn("already sent", str(e))
        p = self.gate(state={"orders": state["orders"] + [sent]}, leg="stop")      # the same ticket's stop leg
        self.assertEqual(p.leg, "stop")

    def test_place_order_needs_a_real_pass(self):
        ad = adapter(self.stub, "kite")
        single = base.leg_order(ticket_order(), "entry")
        with self.assertRaises(base.GateClosed):
            ad.place_order(single, None)
        with self.assertRaises(base.GateClosed):
            ad.place_order(single, object())
        forged = base.GatePass("kite", base.order_fingerprint(ticket_order()), base.order_fingerprint(single), "entry")
        forged.fingerprint = "0" * 16                      # tampered after sealing
        with self.assertRaises(base.GateClosed):
            ad.place_order(single, forged)
        p = self.gate()
        with self.assertRaises(base.GateClosed):
            adapter(self.stub, "upstox").place_order(single, p)               # a kite pass is not an upstox pass
        with self.assertRaises(base.GateClosed):
            ad.place_order(dict(single, qty=43), self.gate())                # a pass for another order
        self.assertEqual(self.stub.calls("POST", "/kite/orders"), [])
        p = self.gate()
        self.assertEqual(ad.place_order(single, p), "151220000000000")
        with self.assertRaises(base.GateClosed):
            ad.place_order(single, p)                                        # single use
        with self.assertRaises(base.GateClosed):
            adapter(self.stub, "kite").place_order(single, p)               # single use across adapters too
        sent = self.stub.calls("POST", "/kite/orders/regular")
        self.assertEqual(len(sent), 1)
        form = dict(urllib.parse.parse_qsl(sent[0]["body"].decode()))
        self.assertEqual(form, {"tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "BUY",
                                "order_type": "LIMIT", "quantity": "42", "product": "MIS", "validity": "DAY",
                                "price": "1174.30", "tag": p.tag})

    def test_legs_are_their_own_orders(self):
        t = ticket_order()
        self.assertEqual(base.leg_order(t, "stop"), {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS",
                                                     "side": "SELL", "qty": 42, "validity": "DAY",
                                                     "order_type": "SL-M", "trigger_price": 1163.30})
        self.assertEqual(base.leg_order(t, "target")["price"], 1196.30)
        self.assertNotIn("stop_loss", base.leg_order(t, "entry"))
        self.assertNotIn("why", base.leg_order(t, "entry"))

    def test_the_fingerprint_is_the_same_everywhere(self):
        o = ticket_order()
        self.assertEqual(base.order_fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(base.order_fingerprint(o), check.fingerprint(o))


# ---------------------------------------------------------------- ticket.py end to end against the stub

class LiveFlow(StubCase):
    """budget's RELIANCE order -> risk -> confirm (yes recorded, nothing sent) -> preflight -> place."""

    def setUp(self):
        StubCase.setUp(self)
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)
        os.makedirs("trader")
        cfg = jload(CFG_PATH)
        cfg.update(broker="kite", paper=False, live_enabled={"kite": True}, static_ip="203.0.113.7")
        self.write("trader/config.json", cfg)
        shutil.copy(ORDERS_PATH, "trader/orders.json")
        self.write("trader/portfolio.json", {"cash": 100000, "positions": {}})
        ticket.ADAPTER_KW.clear()
        for b in BROKERS:
            ticket.ADAPTER_KW[b] = {"hosts": self.stub.hosts(b), "env": dict(ENV), "sleep": lambda s: None}
        self.ips = ["203.0.113.7", "203.0.113.7", "203.0.113.7"]
        ticket.IP_OPENER = self.ip_opener

    def tearDown(self):
        ticket.ADAPTER_KW.clear()
        ticket.IP_OPENER = None
        os.chdir(self.old)
        shutil.rmtree(self.dir)

    def ip_opener(self, req, timeout=None):
        ip = self.ips.pop(0) if self.ips else "198.51.100.1"
        return io.BytesIO(ip.encode() + b"\n")

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def tk(self, *argv):
        return run(ticket, list(argv))

    def cfg(self, **over):
        c = jload("trader/config.json")
        c.update(over)
        self.write("trader/config.json", c)

    def orders_sent(self):
        return self.stub.calls("POST", "/kite/orders")

    def ready(self):
        self.assertEqual(run(check, ["--now", "2026-10-29 09:40"])[0], 0)
        code, out, err = self.tk("confirm", "--index", "1", "--yes-text", "yes 42 RELIANCE", "--now",
                                 "2026-10-29 09:41")
        self.assertEqual(code, 0, err)
        return out

    def test_confirm_only_records_the_yes(self):
        code, text, _ = self.tk("show", "--now", "2026-10-29 09:41")
        self.assertIn("LIVE at Zerodha Kite (real money)", text)
        out = self.ready()
        self.assertIn("Nothing is sent yet", out)
        self.assertIn("ticket.py place --index 1", out)
        rec = jload("trader/state/broker.json")["orders"][-1]
        self.assertEqual((rec["status"], rec["broker"], rec["yes_text"], rec["paper"]),
                         ("approved", "kite", "yes 42 RELIANCE", False))
        self.assertIn("YES RECORDED for kite", self.tk("show", "--now", "2026-10-29 09:42")[1])
        self.assertEqual(self.orders_sent(), [])
        # paper-only commands refuse a live config
        code, _, err = self.tk("fill", "--index", "1", "--price", "1174.30", "--now", "2026-10-29 09:42")
        self.assertEqual(code, 1)
        self.assertIn("paper account only", err)

    def test_place_needs_preflight_then_sends_once(self):
        self.ready()
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:42")
        self.assertEqual(code, 1)
        self.assertIn("preflight", err)
        self.assertEqual(self.orders_sent(), [])
        code, out, err = self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS static_ip: this machine's IP 203.0.113.7 is the static IP your config static_ip", out)
        self.assertIn("SEBI", out)
        pre = jload("trader/state/preflight.json")["kite"]
        self.assertTrue(pre["passed"])
        self.assertEqual(pre["token_hash"], base.token_hash("kitetok33"))
        code, out, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:44")
        self.assertEqual(code, 0, err)
        self.assertIn("SENT to Zerodha Kite (real money)", out)
        self.assertIn("151220000000000", out)
        self.assertIn("NOT at the broker yet", out)
        self.assertEqual(len(self.orders_sent()), 1)
        form = dict(urllib.parse.parse_qsl(self.orders_sent()[0]["body"].decode()))
        self.assertEqual((form["tradingsymbol"], form["quantity"], form["price"], form["order_type"]),
                         ("RELIANCE", "42", "1174.30", "LIMIT"))
        rec = jload("trader/state/broker.json")["orders"][-1]
        self.assertEqual((rec["status"], rec["broker_order_id"], rec["live"], rec["max_loss"]),
                         ("placed", "151220000000000", True, 514.0))
        # one yes, one order
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:45")
        self.assertEqual(code, 1)
        self.assertIn("already sent", err)
        self.assertEqual(len(self.orders_sent()), 1)
        # trader-risk now counts the live entry like a paper one
        code, text, _ = run(check, ["--now", "2026-10-29 09:46"])
        self.assertIn("already placed today", text)
        # nothing secret was written anywhere under trader/
        for base_, _, files in os.walk("trader"):
            for f in files:
                with open(os.path.join(base_, f), encoding="utf-8") as fh:
                    text = fh.read()
                for s in TOKENS:
                    self.assertNotIn(s, text, f)

    def test_every_gate_from_the_command_line(self):
        self.ready()
        self.assertEqual(self.tk("preflight", "kite", "--now", "2026-10-29 09:43")[0], 0)

        def refused(words, **cfg):
            if cfg:
                self.cfg(**cfg)
            code, out, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:44")
            self.assertEqual(code, 1, out)
            self.assertIn(words, err)
            self.assertEqual(self.orders_sent(), [])

        refused("live_enabled", live_enabled={})
        refused("live_enabled", live_enabled={"kite": False})
        self.cfg(live_enabled={"kite": True})
        refused("the paper broker only works", paper=True)
        refused("paper", paper=None)
        refused("config is paper", broker="paper", paper=True)
        self.cfg(broker="kite", paper=False)
        ticket.ADAPTER_KW["kite"]["env"]["BT_KITE_ACCESS_TOKEN"] = "kitetok34"     # logged in again
        refused("run ticket.py preflight kite again")
        ticket.ADAPTER_KW["kite"]["env"]["BT_KITE_ACCESS_TOKEN"] = "kitetok33"
        os.remove("trader/state/risk.json")
        refused("trader-risk has no verdict")
        run(check, ["--now", "2026-10-29 09:40"])
        doc = jload("trader/orders.json")
        doc["orders"][0]["qty"] = 41                       # edited after the yes and the risk check
        self.write("trader/orders.json", doc)
        refused("trader-risk has no verdict")

    def test_no_yes_no_order(self):
        run(check, ["--now", "2026-10-29 09:40"])
        self.assertEqual(self.tk("preflight", "kite", "--now", "2026-10-29 09:43")[0], 0)
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:44")
        self.assertEqual(code, 1)
        self.assertIn("no typed yes recorded", err)
        code, out, _ = self.tk("confirm", "--index", "1", "--yes-text", "sure", "--now", "2026-10-29 09:44")
        self.assertIn("Not recorded", out)
        self.assertEqual(self.tk("place", "--index", "1", "--now", "2026-10-29 09:45")[0], 1)
        self.assertEqual(self.orders_sent(), [])

    def test_paper_stays_the_default(self):
        self.write("trader/config.json", jload(CFG_PATH))
        run(check, ["--now", "2026-10-29 09:40"])
        code, out, _ = self.tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:41")
        self.assertIn("PAPER account", out)
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:42")
        self.assertEqual(code, 1)
        self.assertIn("config is paper", err)
        self.assertEqual(self.stub.hits, [])

    def test_legs_go_after_the_fill_and_one_at_a_time(self):
        self.ready()
        self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        code, _, err = self.tk("place", "--index", "1", "--leg", "stop", "--now", "2026-10-29 09:44")
        self.assertIn("send the entry first", err)
        self.tk("place", "--index", "1", "--now", "2026-10-29 09:44")
        code, _, err = self.tk("place", "--index", "1", "--leg", "stop", "--now", "2026-10-29 09:45")
        self.assertEqual(code, 1)
        self.assertIn("not in the order book", err)
        book = jload(os.path.join(BFIX, "kite", "orders.json"))["response"]["body"]
        book["data"][0].update(order_id="151220000000000", status="COMPLETE", filled_quantity=42, average_price=1174.3)
        self.stub.add("GET", "/kite/orders", body=book)
        self.stub.add("POST", "/kite/orders/regular", body={"status": "success", "data": {"order_id": "151220000000001"}})
        self.stub.add("DELETE", "/kite/orders/regular/151220000000001", body={"status": "success", "data": {
            "order_id": "151220000000001"}})
        code, out, err = self.tk("place", "--index", "1", "--leg", "stop", "--now", "2026-10-29 09:46")
        self.assertEqual(code, 0, err)
        form = dict(urllib.parse.parse_qsl(self.orders_sent()[-1]["body"].decode()))
        self.assertEqual((form["transaction_type"], form["order_type"], form["trigger_price"],
                          form["market_protection"]), ("SELL", "SL-M", "1163.30", "-1"))
        book["data"].append(dict(book["data"][0], order_id="151220000000001", status="OPEN"))
        code, _, err = self.tk("place", "--index", "1", "--leg", "target", "--now", "2026-10-29 09:47")
        self.assertEqual(code, 1)
        self.assertIn("only one exit leg works at the broker at a time", err)
        code, out, err = self.tk("cancel", "--index", "1", "--leg", "stop", "--now", "2026-10-29 09:48")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.stub.calls("DELETE", "/kite/orders/regular/151220000000001")), 1)
        self.assertEqual(jload("trader/state/broker.json")["orders"][-1]["status"], "cancel_sent")

    def test_a_refused_order_is_recorded_without_secrets(self):
        self.ready()
        self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        self.stub.error("kite", "margin")
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:44")
        self.assertEqual(code, 1)
        self.assertIn("Insufficient funds", err)
        rec = jload("trader/state/broker.json")["orders"][-1]
        self.assertEqual(rec["status"], "send_failed")
        code, _, err = self.tk("place", "--index", "1", "--now", "2026-10-29 09:45")      # the yes is spent
        self.assertEqual(code, 1)
        self.assertIn("already sent", err)
        self.assertEqual(len(self.orders_sent()), 1)

    def test_preflight_fails_in_plain_words(self):
        self.ips = ["203.0.113.8", "198.51.100.2", "192.0.2.3"]          # three services disagree
        code, out, _ = self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        self.assertEqual(code, 1)
        self.assertIn("FAIL public_ip", out)
        self.ips = ["198.51.100.2"] * 3
        code, out, _ = self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        self.assertIn("is not the static IP your config static_ip (203.0.113.7): orders would be refused", out)
        self.assertIn("Reading holdings and positions works anyway", out)
        self.assertFalse(jload("trader/state/preflight.json")["kite"]["passed"])
        # a token from before today's 06:00 cutoff is stale
        ticket.ADAPTER_KW["kite"]["env"]["BT_KITE_TOKEN_DATE"] = "2026-10-28T21:00:00+05:30"
        self.ips = ["203.0.113.7"] * 3
        code, out, _ = self.tk("preflight", "kite", "--now", "2026-10-29 09:43")
        self.assertIn("FAIL token: the token is from 2026-10-28 21:00, before today's 06:00 IST cutoff", out)
        # Upstox and Dhan say which IPs they have on record
        self.cfg(static_ip="")
        self.ips = ["122.181.101.247"] * 3
        code, out, _ = self.tk("preflight", "upstox", "--now", "2026-10-29 09:43")
        self.assertEqual(code, 0, out)
        self.assertIn("is the static IP Upstox has on record", out)
        self.cfg(static_ip="203.0.113.7")
        code, out, _ = self.tk("preflight", "upstox", "--now", "2026-10-29 09:43", "--ip", "203.0.113.7")
        self.assertIn("your config static_ip 203.0.113.7 is not what Upstox has on record", out)
        self.stub.error("dhan", "token", "/dhan/v2/profile")
        code, out, _ = self.tk("preflight", "dhan", "--now", "2026-10-29 09:43", "--ip", "203.0.113.7")
        self.assertIn("FAIL login: the profile call failed: Dhan DH-901", out)
        self.assertNotIn("dhtok77", out)

    def test_read_commands(self):
        code, out, err = self.tk("holdings", "--now", "2026-10-29 10:00")
        self.assertEqual(code, 0, err)
        self.assertIn("Zerodha Kite holdings (delivery), 3 stocks", out)
        self.assertIn("SBIN 3 shares at 573.40 average, last 593.75, P&L ₹61.05", out)
        self.assertIn("Read only", out)
        code, out, _ = self.tk("positions", "--broker", "upstox", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["broker"], doc["read_only"]), ("upstox", True))
        self.assertEqual(doc["data"][0]["product"], "NRML")
        code, out, _ = self.tk("funds", "--broker", "dhan")
        self.assertIn("available ₹98,440.00, used ₹15,202.00", out)
        code, out, _ = self.tk("accounts", "--broker", "dhan", "--now", "2024-12-01 10:00")
        self.assertIn("Dhan account 11******26", out)
        self.assertIn("Token: the token is good until 2025-03-30 15:37 IST", out)
        code, out, _ = self.tk("orders", "--broker", "upstox")
        self.assertIn("Upstox orders today: 3; trades: 1.", out)
        self.assertIn("231019025000398 SELL 1500 SBIN23OCTFUT SL-M, rejected (MIS)", out)
        code, out, _ = self.tk("instruments", "--broker", "kite", "--symbol", "NIFTY 2026-11-09 25000 CE",
                               "--exchange", "NFO")
        self.assertIn("NIFTY26N0925000CE", out)
        self.assertIn("lot 65", out)
        self.write("trader/config.json", jload(CFG_PATH))
        code, _, err = self.tk("holdings")
        self.assertEqual(code, 1)
        self.assertIn("which broker?", err)
        # never an order call from a read command
        self.assertEqual([h for h in self.stub.hits if h["method"] != "GET" and "marketfeed" not in h["path"]], [])


# ---------------------------------------------------------------- the login helper

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Login(StubCase):
    def setUp(self):
        StubCase.setUp(self)
        self.port = free_port()
        self.env = dict(ENV, BT_KITE_REDIRECT="http://127.0.0.1:%d/kite" % self.port,
                        BT_UPSTOX_REDIRECT="http://127.0.0.1:%d/upstox" % self.port,
                        BT_DHAN_REDIRECT="http://127.0.0.1:%d/dhan" % self.port)
        ticket.ADAPTER_KW.clear()
        for b in BROKERS:
            ticket.ADAPTER_KW[b] = {"hosts": self.stub.hosts(b), "env": self.env}
        self.browser = None
        import webbrowser
        self.wb = webbrowser
        self.old_open = webbrowser.open
        webbrowser.open = self.fake_open

    def tearDown(self):
        self.wb.open = self.old_open
        ticket.ADAPTER_KW.clear()

    def fake_open(self, url):
        """The user's browser: log in, then the broker redirects to our local callback."""
        self.opened = url
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        cb = self.browser(q)

        def go():
            try:
                urllib.request.urlopen(cb, timeout=5).read()
            except urllib.error.HTTPError as e:       # a 400 page is fine too
                e.close()
            except OSError:
                pass
        threading.Thread(target=go, daemon=True).start()
        return True

    def login(self, broker):
        return run(ticket, ["login", broker, "--timeout", "10", "--now", "2026-10-29 08:30"])

    def test_kite_login_checks_state_and_prints_exports(self):
        def browser(q):
            state = dict(urllib.parse.parse_qsl(q["redirect_params"]))["state"]
            return "http://127.0.0.1:%d/kite?action=login&status=success&request_token=rt12345&state=%s" % (
                self.port, state)
        self.browser = browser
        code, out, err = self.login("kite")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["export BT_KITE_ACCESS_TOKEN=REDACTED",
                                            "export BT_KITE_TOKEN_DATE=2026-10-29T08:30:00+05:30"])
        self.assertIn("not saved anywhere", err)
        self.assertIn("06:00 IST", err)
        sent = dict(urllib.parse.parse_qsl(self.stub.calls("POST", "/kite/session/token")[-1]["body"].decode()))
        self.assertEqual(sent["checksum"], hashlib.sha256(b"kitekey11rt12345kitesec22").hexdigest())
        self.assertEqual(sent["request_token"], "rt12345")
        for s in ("rt12345", "kitesec22"):
            self.assertNotIn(s, err)
        self.assertTrue(self.opened.startswith(self.stub.base + "/kite-login/connect/login?v=3&api_key=kitekey11"))

    def test_a_wrong_state_is_refused(self):
        self.browser = lambda q: "http://127.0.0.1:%d/upstox?code=abc123&state=forged" % self.port
        code, out, err = self.login("upstox")
        self.assertEqual(code, 1)
        self.assertIn("different state value", err)
        self.assertEqual(out, "")
        self.assertEqual(self.stub.calls("POST", "/upstox/v2/login/authorization/token"), [])

    def test_upstox_login(self):
        def browser(q):
            self.assertEqual((q["client_id"], q["response_type"], q["redirect_uri"]),
                             ("upkey44", "code", "http://127.0.0.1:%d/upstox" % self.port))
            return "http://127.0.0.1:%d/upstox?code=c0de99&state=%s" % (self.port, q["state"])
        self.browser = browser
        code, out, err = self.login("upstox")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("export BT_UPSTOX_ACCESS_TOKEN="))
        sent = dict(urllib.parse.parse_qsl(self.stub.calls("POST", "/upstox/v2/login/authorization/token")[-1][
            "body"].decode()))
        self.assertEqual((sent["code"], sent["grant_type"], sent["client_secret"]), ("c0de99", "authorization_code",
                                                                                  "upsec55"))

    def test_dhan_consent_login(self):
        def browser(q):
            self.assertEqual(q["consentAppId"], "940b0ca1-3ff4-4476-b46e-03a3ce7dc55d")
            return "http://127.0.0.1:%d/dhan?tokenId=tok456" % self.port
        self.browser = browser
        code, out, err = self.login("dhan")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("export BT_DHAN_ACCESS_TOKEN="))
        first = self.stub.calls("POST", "/dhan/app/generate-consent")[-1]
        hdr = {k.lower(): v for k, v in first["headers"].items()}
        self.assertEqual((first["query"], hdr["app_id"], hdr["app_secret"]), ({"client_id": "1000000009"}, "dhkey88",
                                                                             "dhsec99"))
        self.assertEqual(self.stub.calls("GET", "/dhan/app/consumeApp-consent")[-1]["query"], {"tokenId": "tok456"})

    def test_only_a_local_redirect(self):
        for url in ("https://example.com/cb", "http://192.168.1.5:8765/cb", "http://127.0.0.1/cb"):
            with self.assertRaises(auth.LoginError):
                auth.parse_redirect(url)
        self.assertEqual(auth.parse_redirect("http://127.0.0.1:8765/kite"), (8765, "/kite"))

    def test_one_shot_and_timeout(self):
        with self.assertRaises(auth.LoginError) as cm:
            auth.wait_for_callback(free_port(), "/cb", "code", "s1", timeout=1)
        self.assertIn("no login came back", str(cm.exception))
        port = free_port()

        def hit(url):
            def go():
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/other" % port, timeout=5)
                except urllib.error.HTTPError as e:     # 404: not the callback path, the server keeps waiting
                    e.close()
                urllib.request.urlopen("http://127.0.0.1:%d/cb?code=x1y2z3&state=s1" % port, timeout=5).read()
            threading.Thread(target=go, daemon=True).start()
        got = auth.wait_for_callback(port, "/cb", "code", "s1", timeout=10, on_ready=hit)
        self.assertEqual(got["code"], "x1y2z3")
        self.assertEqual(base.scrub("code=x1y2z3"), "code=[redacted]")


class PublicIP(unittest.TestCase):
    def test_two_services_must_agree(self):
        answers = iter(["203.0.113.7\n", "not an ip", "203.0.113.7"])
        ip, notes = auth.public_ip(opener=lambda req, timeout=None: io.BytesIO(next(answers).encode()))
        self.assertEqual(ip, "203.0.113.7")
        self.assertEqual(len(notes), 3)
        answers = iter(["203.0.113.7", "203.0.113.8", "203.0.113.9"])
        self.assertIsNone(auth.public_ip(opener=lambda req, timeout=None: io.BytesIO(next(answers).encode()))[0])

    def test_static_ip_config_shapes(self):
        self.assertEqual(auth.static_ips({"static_ip": "203.0.113.7"}, "kite"), ["203.0.113.7"])
        self.assertEqual(auth.static_ips({"static_ip": ["203.0.113.7", "2001:db8::1"]}, "kite"),
                         ["203.0.113.7", "2001:db8::1"])
        self.assertEqual(auth.static_ips({"static_ip": {"dhan": "198.51.100.4"}}, "dhan"), ["198.51.100.4"])
        self.assertEqual(auth.static_ips({"static_ip": {"dhan": "198.51.100.4"}}, "kite"), [])
        self.assertEqual(auth.static_ips({}, "kite"), [])


class Fixtures(unittest.TestCase):
    def test_every_fixture_is_marked_and_secret_free(self):
        files = glob.glob(os.path.join(BFIX, "*", "*.json"))
        self.assertGreaterEqual(len(files), 40)
        for f in files:
            d = jload(f)
            self.assertTrue(d["doc_derived"], f)
            self.assertRegex(d["source"], r"^https://(kite\.trade|upstox\.com|dhanhq\.co)/", f)
            text = json.dumps(d)
            self.assertNotRegex(text, r"eyJ[A-Za-z0-9_-]{10,}", f)
            self.assertNotRegex(text, r"[A-Z]{5}\d{4}[A-Z]", f)               # no PAN
            self.assertNotRegex(text.lower(), r"@(?!example\.invalid)[a-z0-9-]+\.[a-z]", f)
        src = jload(os.path.join(BFIX, "brokers-sources.json"))
        for path in src["files"]:
            self.assertTrue(os.path.isfile(os.path.join(BFIX, path)), path)
        with gzip.open(os.path.join(BFIX, "upstox", "NSE.json.gz")) as fh:
            self.assertEqual(len(json.load(fh)), 20)


# ---------------------------------------------------------------- opt-in sandbox smoke tests (never by default)

SANDBOX = os.environ.get("BT_SANDBOX") == "1"


@unittest.skipUnless(SANDBOX and os.environ.get("BT_UPSTOX_SANDBOX_TOKEN"),
                     "opt-in: BT_SANDBOX=1 BT_UPSTOX_SANDBOX_TOKEN=... (an Upstox sandbox app's token)")
class UpstoxSandboxSmoke(unittest.TestCase):
    def test_place_and_cancel_in_the_sandbox(self):
        ad = pkg.make("upstox", sandbox=True)
        order = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 1,
                 "order_type": "LIMIT", "price": 1000.0, "validity": "DAY"}
        t, cfg, risk, state, pre = open_docs("upstox", ad.token())
        today = datetime.datetime.now(IST).date().isoformat()
        fp = base.order_fingerprint(order)
        p = base.open_gate("upstox", cfg, order, {"date": today, "results": [{"fingerprint": fp, "verdict": "PASS"}]},
                           {"orders": [{"date": today, "fingerprint": fp, "status": "approved", "broker": "upstox",
                                        "yes_text": "yes"}]},
                           {"upstox": dict(pre["upstox"], date=today, at=datetime.datetime.now(IST).isoformat())},
                           ad.token(), today, datetime.datetime.now(IST))
        oid = ad.place_order(order, p)
        self.assertTrue(oid)
        sent = {"broker_order_id": oid, "fingerprint": fp, "yes_text": "yes"}
        cp = base.open_cancel_gate("upstox", cfg, oid, sent, {"orders": []},
                                   {"upstox": dict(pre["upstox"], date=today, at=datetime.datetime.now(IST).isoformat())},
                                   ad.token(), today, datetime.datetime.now(IST))
        self.assertTrue(ad.cancel_order(oid, cp))


@unittest.skipUnless(SANDBOX and os.environ.get("BT_DHAN_SANDBOX_TOKEN") and os.environ.get("BT_DHAN_CLIENT_ID"),
                     "opt-in: BT_SANDBOX=1 BT_DHAN_SANDBOX_TOKEN=... BT_DHAN_CLIENT_ID=... (Dhan sandbox)")
class DhanSandboxSmoke(unittest.TestCase):
    def test_profile_and_order_book_in_the_sandbox(self):
        ad = pkg.make("dhan", sandbox=True)
        self.assertEqual(ad.profile()["broker"], "dhan")
        self.assertIsInstance(ad.orders(), list)


if __name__ == "__main__":
    unittest.main()
