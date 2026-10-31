"""ui/serve.py and ui/api.py tests, offline. Fixtures in tests/fixtures/ui/:

yahoo-<SYMBOL>.json      REAL Yahoo v8 chart answers (range=1d, 5m) used as quotes: RELIANCE, TCS, INFY,
                         HDFCBANK, SBIN and ^NSEI of Thu 29 Oct 2026 (copied from the trader-alerts fixtures);
                         ^NSEBANK and ^INDIAVIX of Fri 30 Oct 2026, fetched 2026-11-01.
chart-RELIANCE-5m.json   REAL: the trader-entry fixture of 29 Oct 2026 (five-minute bars).
chart-RELIANCE-1m.json   REAL: the trader-entry fixture of 29 Oct 2026 (one-minute bars).
chart-<SYM>-1d.json      REAL: daily bars (range=3mo) of RELIANCE and TCS, cut from the trader-scanner bundle.
nse-marketStatus.json    REAL: nseindia.com/api/marketStatus after the close of 29 Oct 2026.
offline.json             the pinned clock (2026-10-29 14:30 IST) and a note for the demo banner.
trader/                  a working folder built by the skills themselves on those fixtures: size.py init,
                         a paper BUY of 5 TCS, the trader-alerts plan fixture as plan.json, scan.py daily,
                         alerts.py quote --write, size.py --write, alerts.py add --from-plan, check.py.
"""

import contextlib
import hashlib
import io
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

UI = os.path.join(ROOT, "ui")
UIFIX = os.path.join(FIX, "ui")
STATIC = os.path.join(UI, "static")


def _serve_module():
    spec = importlib.util.spec_from_file_location("bt_ui_serve", os.path.join(UI, "serve.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


serve = _serve_module()
api = serve.api


def read_text(path, size=-1):
    with open(path, encoding="utf-8") as fh:
        return fh.read(size)


def blob(path):
    with open(path, "rb") as fh:
        return fh.read()


def workdir():
    tmp = tempfile.mkdtemp(prefix="bt-ui-test-")
    shutil.copytree(os.path.join(UIFIX, "trader"), os.path.join(tmp, "trader"))
    return tmp


class Running(object):
    """A real server on a free loopback port, serving the offline fixtures."""

    def __init__(self):
        self.dir = workdir()
        self.srv = serve.make_server("127.0.0.1", 0, self.dir, UIFIX)
        self.port = self.srv.server_address[1]
        self.host = "127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.cookie, self.token = self.session()

    def close(self):
        self.srv.stop()
        self.thread.join(5)
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": self.host if host is None else host}
        h.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, raw

    def session(self):
        r, raw = self.request("GET", "/")
        cookie = r.getheader("Set-Cookie").split(";")[0]
        token = re.search(r'name="csrf-token" content="([^"]+)"', raw.decode("utf-8")).group(1)
        return cookie, token

    def get(self, path):
        r, raw = self.request("GET", path)
        return r.status, json.loads(raw.decode("utf-8"))

    def post(self, path, body, token=True, cookie=True):
        h = {"Content-Type": "application/json", "Origin": "http://" + self.host}
        if token:
            h["X-CSRF-Token"] = self.token
        if cookie:
            h["Cookie"] = self.cookie
        r, raw = self.request("POST", path, body, h)
        return r.status, json.loads(raw.decode("utf-8"))


class Security(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_binds_loopback_by_default(self):
        self.assertEqual(self.s.srv.server_address[0], "127.0.0.1")
        self.assertEqual(serve.make_server.__defaults__[0], "127.0.0.1")
        a = serve.parser().parse_args([])
        self.assertEqual(a.host, "127.0.0.1")
        self.assertIsNone(a.workdir)
        self.assertFalse(a.open)

    def test_non_loopback_host_header_is_rejected(self):
        for host in ("evil.example:%d" % self.s.port, "192.168.1.5:%d" % self.s.port, "127.0.0.1:1", ""):
            r, _ = self.s.request("GET", "/api/meta", host=host)
            self.assertEqual(r.status, 421, host)
        r, _ = self.s.request("GET", "/api/meta", host="localhost:%d" % self.s.port)
        self.assertEqual(r.status, 200)

    def test_bad_origin_is_rejected(self):
        r, _ = self.s.request("GET", "/api/meta", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.s.request("GET", "/api/meta", headers={"Origin": "null"})
        self.assertEqual(r.status, 403)
        status, _ = self.s.post("/api/config/validate", {"config": {}})
        self.assertEqual(status, 200)
        r, _ = self.s.request("POST", "/api/config/validate", {"config": {}}, {
            "Content-Type": "application/json", "X-CSRF-Token": self.s.token, "Cookie": self.s.cookie,
            "Origin": "http://localhost.evil.example:%d" % self.s.port})
        self.assertEqual(r.status, 403)
        r, _ = self.s.request("GET", "/api/meta", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)

    def test_post_without_csrf_is_rejected(self):
        status, body = self.s.post("/api/risk/check", {}, token=False)
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])
        status, _ = self.s.post("/api/risk/check", {}, cookie=False)
        self.assertEqual(status, 403)
        r, _ = self.s.request("POST", "/api/risk/check", {}, {"Content-Type": "application/json",
                                                              "X-CSRF-Token": "x" * 43, "Cookie": self.s.cookie})
        self.assertEqual(r.status, 403)
        # another session's token does not work with this cookie
        _, other = self.s.session()
        r, _ = self.s.request("POST", "/api/risk/check", {}, {"Content-Type": "application/json",
                                                              "X-CSRF-Token": other, "Cookie": self.s.cookie})
        self.assertEqual(r.status, 403)

    def test_post_needs_json(self):
        r, _ = self.s.request("POST", "/api/config/validate", b"config=1", {
            "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": self.s.token,
            "Cookie": self.s.cookie})
        self.assertEqual(r.status, 415)
        r, _ = self.s.request("POST", "/api/config/validate", b"{nope", {
            "Content-Type": "application/json", "X-CSRF-Token": self.s.token, "Cookie": self.s.cookie})
        self.assertEqual(r.status, 400)
        r, _ = self.s.request("POST", "/api/config/validate", b"x" * (serve.MAX_BODY + 1), {
            "Content-Type": "application/json", "X-CSRF-Token": self.s.token, "Cookie": self.s.cookie})
        self.assertEqual(r.status, 413)

    def test_session_cookie_is_httponly_and_strict(self):
        r, raw = self.s.request("GET", "/")
        c = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Strict", c)
        self.assertNotIn("__CSRF_TOKEN__", raw.decode("utf-8"))
        # a page load with the cookie keeps the same token
        r, raw = self.s.request("GET", "/", headers={"Cookie": self.s.cookie})
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertIn(self.s.token, raw.decode("utf-8"))

    def test_csp_and_security_headers_everywhere(self):
        for path in ("/", "/api/meta", "/static/js/app.js", "/nope"):
            r, _ = self.s.request("GET", path)
            csp = r.getheader("Content-Security-Policy")
            self.assertIsNotNone(csp, path)
            self.assertIn("default-src 'none'", csp)
            self.assertIn("script-src 'self';", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("connect-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertNotIn("http", csp)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
            self.assertIsNone(r.getheader("Access-Control-Allow-Origin"), path)

    def test_no_cors_preflight(self):
        r, _ = self.s.request("OPTIONS", "/api/config", headers={
            "Origin": "http://" + self.s.host, "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status, 405)
        for k, _ in r.getheaders():
            self.assertFalse(k.lower().startswith("access-control"), k)

    def test_static_cannot_escape_its_folder(self):
        for path in ("/static/../serve.py", "/static/%2e%2e/serve.py", "/static/..%2fapi.py",
                     "/static/index.html", "/static/js/", "/serve.py", "/static/vendor/../../api.py"):
            r, _ = self.s.request("GET", path)
            self.assertEqual(r.status, 404, path)

    def test_csp_hash_matches_the_chart_library_style(self):
        import base64
        src = read_text(os.path.join(STATIC, "vendor", "lightweight-charts.standalone.production.js"))
        css = re.search(r'createElement\("style"\),this\.\w+\.innerText="([^"]*)"', src).group(1)
        digest = base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode()
        self.assertEqual(serve.LWC_LOGO_STYLE, "'sha256-%s'" % digest)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    QUERY = {"/api/quotes": "?symbols=RELIANCE,TCS,^NSEI", "/api/chart": "?symbol=RELIANCE&interval=5m",
             "/api/status": "?verify=1", "/api/dashboard": "?verify=1"}

    def test_every_get_route_answers_json(self):
        gets = [r for r in api.ROUTES if r[0] == "GET"]
        self.assertGreaterEqual(len(gets), 15)
        for _, path, _, _, _ in gets:
            r, raw = self.s.request("GET", path + self.QUERY.get(path, ""))
            self.assertEqual(r.status, 200, (path, raw[:300]))
            self.assertTrue(r.getheader("Content-Type").startswith("application/json"), path)
            json.loads(raw.decode("utf-8"))

    def test_meta_lists_every_route(self):
        _, m = self.s.get("/api/meta")
        self.assertTrue(m["offline"])
        self.assertTrue(m["paper"])
        self.assertEqual(m["pinned_now"], "2026-10-29 14:30")
        self.assertEqual(len(m["routes"]), len(api.ROUTES))

    def test_status_is_trader_alerts_market_status(self):
        _, st = self.s.get("/api/status?verify=1")
        alerts = load("trader-alerts", "alerts")
        want = alerts.market_status(alerts.parse_now("2026-10-29 14:30"))
        for k in ("open", "phase", "reason", "next_open", "opens", "closes"):
            self.assertEqual(st[k], want[k], k)
        self.assertTrue(st["open"])
        self.assertEqual(st["nse"]["status"], "Closed")       # the fixture was taken after the close
        self.assertFalse(st["nse_agrees"])
        self.assertIn("formula", st["why"])

    def test_indices_and_quotes(self):
        _, d = self.s.get("/api/indices")
        got = {q["symbol"]: q for q in d["indices"]}
        self.assertEqual(sorted(got), ["^INDIAVIX", "^NSEBANK", "^NSEI"])
        n = got["^NSEI"]
        self.assertEqual(n["price"], 22936.75)
        self.assertEqual(n["change_pct"], round(100.0 * (22936.75 / n["prev_close"] - 1), 2))
        self.assertEqual(n["why"]["inputs"]["price"], 22936.75)
        status, body = self.s.get("/api/quotes?symbols=NOT%20A%20SYMBOL!")
        self.assertEqual(status, 400)
        status, body = self.s.get("/api/quotes")
        self.assertEqual(status, 400)

    def test_chart_comes_from_trader_entry(self):
        _, c = self.s.get("/api/chart?symbol=RELIANCE&interval=5m")
        entry = load("trader-entry", "entry")
        ch = entry.get_chart("RELIANCE", "5m", offline=os.path.join(UIFIX, "chart-RELIANCE-5m.json"))
        res = entry.evaluate(ch, entry.parse_now("2026-10-29 14:30"), interval="5m")
        self.assertEqual(c["opening_range"]["high"], res["opening_range"]["high"])
        self.assertEqual(c["opening_range"]["low"], res["opening_range"]["low"])
        self.assertEqual(c["entry"]["status"], res["status"])
        self.assertEqual(c["entry"]["trigger"]["price"], res["trigger"]["price"])
        # no bar from after the pinned clock
        self.assertTrue(all(b["time"] < entry.ts_of(entry.parse_now("2026-10-29 14:30")) for b in c["bars"]))
        self.assertEqual(c["plan"]["stop"], 1163.3)
        self.assertEqual(set(c["ema"]), {"9", "21"})
        self.assertTrue(c["vwap"])
        for k in ("vwap", "ema9", "ema21", "opening_range", "plan"):
            self.assertIn("formula", c["why"][k], k)

    def test_chart_intervals_and_errors(self):
        status, d = self.s.get("/api/chart?symbol=RELIANCE&interval=1d")
        self.assertEqual(status, 200)
        self.assertEqual(len(d["bars"]), 64)
        self.assertEqual(d["vwap"], [])
        self.assertIsNone(d["opening_range"])
        status, d = self.s.get("/api/chart?symbol=RELIANCE&interval=1m")
        self.assertEqual(status, 200)
        self.assertEqual(d["opening_range"]["bars"], 15)
        self.assertEqual(self.s.get("/api/chart?symbol=RELIANCE&interval=15m")[0], 400)
        self.assertEqual(self.s.get("/api/chart?symbol=RELIANCE&ema=1")[0], 400)
        self.assertEqual(self.s.get("/api/chart?symbol=WIPRO&interval=5m")[0], 404)   # no fixture
        self.assertEqual(self.s.get("/api/chart")[0], 400)

    def test_portfolio_numbers_are_the_paper_brokers(self):
        _, a = self.s.get("/api/portfolio")
        broker = load("trader-broker", "ticket")
        cfg = json.loads(read_text(os.path.join(UIFIX, "trader", "config.json")))
        pf = broker.load_portfolio(os.path.join(UIFIX, "trader", "portfolio.json"), cfg)
        self.assertEqual(a["cash"], pf["cash"])
        tcs = a["positions"][0]
        self.assertEqual(tcs["symbol"], "TCS")
        ch = broker.side_charges("SELL", 2478.0, 5, cfg, "CNC")
        self.assertEqual(tcs["exit_charges"], ch["total"])
        sim = json.loads(json.dumps(pf))
        self.assertEqual(tcs["pnl_if_closed"], broker.apply_fill(sim, "TCS", "SELL", 5, 2478.0, ch["total"], "CNC"))
        self.assertEqual(a["today"]["pnl"], tcs["pnl_if_closed"])
        self.assertTrue(a["paper"])

    def test_orders_and_risk_preview(self):
        _, o = self.s.get("/api/orders")
        t = o["tickets"][0]
        self.assertTrue(t["ready"])
        self.assertEqual(t["max_loss"], 514.0)
        self.assertTrue(t["paper"])
        self.assertIn("formula", t["explain"]["max_loss"])
        _, r = self.s.get("/api/risk")
        self.assertEqual(r["preview"]["results"][0]["verdict"], "PASS")
        self.assertEqual(r["last_check"]["pass"], 1)
        self.assertFalse(r["stale"])

    def test_scanner_alerts_config_and_placeholders(self):
        _, w = self.s.get("/api/scanner")
        self.assertEqual(len(w["hits"]), 21)
        self.assertTrue(all(h["setup_labels"] for h in w["hits"]))
        _, al = self.s.get("/api/alerts")
        self.assertEqual(len(al["rules"]), 3)
        self.assertEqual(al["rings"], [])     # RELIANCE 1167.40 sits between the stop and the target
        _, c = self.s.get("/api/config")
        self.assertTrue(c["exists"])
        self.assertEqual(c["errors"], [])
        self.assertIn("Capital ₹1,00,000.00", c["summary"])
        meanings = {s["key"]: s["doc"] for s in c["schema"]}
        self.assertIn("Trading capital", meanings["capital"])
        self.assertNotIn("`", meanings["product"])
        self.assertIn("option pricing", meanings["risk_free_rate"])     # trader-options' key
        _, d = self.s.get("/api/strategy")
        self.assertTrue(d["available"])
        self.assertEqual(len(d["templates"]), 17)

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.s.get("/api/nope")[0], 404)
        status, _ = self.s.post("/api/meta", {})
        self.assertEqual(status, 405)

    def test_static_types(self):
        want = {"/": "text/html", "/static/js/app.js": "text/javascript", "/static/css/app.css": "text/css",
                "/static/js/pages/chart.js": "text/javascript", "/static/js/pages/options.js": "text/javascript",
                "/static/js/pages/strategy.js": "text/javascript", "/static/js/plot.js": "text/javascript",
                "/static/vendor/lightweight-charts.standalone.production.js": "text/javascript",
                "/static/vendor/NOTICE": "text/plain", "/static/vendor/LICENSE": "text/plain"}
        for path, ctype in want.items():
            r, raw = self.s.request("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), (path, r.getheader("Content-Type")))
            self.assertTrue(raw)

    def test_stream_sends_events(self):
        sock = socket.create_connection(("127.0.0.1", self.s.port), timeout=20)
        try:
            sock.sendall(("GET /api/stream HTTP/1.1\r\nHost: %s\r\nAccept: text/event-stream\r\n\r\n"
                          % self.s.host).encode())
            buf = b""
            while b"event: alerts" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        text = buf.decode("utf-8", "replace")
        self.assertIn("text/event-stream", text)
        for ev in ("event: status", "event: prices", "event: alerts"):
            self.assertIn(ev, text)
        data = re.search(r"event: status\ndata: (.*)\n", text).group(1)
        self.assertTrue(json.loads(data)["open"])


class OptionsAndStrategy(unittest.TestCase):
    """The option chain and strategy builder pages' routes, on the real NSE chains of Fri 30-Oct-2026."""

    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_option_chain_is_trader_options_analysis(self):
        status, d = self.s.get("/api/options?symbol=NIFTY&strikes=5")
        self.assertEqual(status, 200, d)
        self.assertEqual((d["underlying"], d["expiry"], d["lot_size"], d["atm"]), ("NIFTY", "2026-11-03", 65, 23000))
        self.assertEqual(len(d["rows"]), 11)
        self.assertEqual(d["strikes_total"], 104)
        self.assertEqual((d["max_pain"], d["walls"]["call"], d["walls"]["put"]), (23000, 23500, 22000))
        self.assertAlmostEqual(d["pcr_oi"], 1.6238, 4)
        self.assertEqual(d["atm_iv"], 12.1)
        self.assertTrue(d["smile"])
        self.assertTrue(d["max_pain_curve"])
        for k in ("forward", "atm_iv", "pcr", "max_pain", "walls", "expected_move", "iv", "greeks", "oi", "oi_chg"):
            self.assertIn("formula", d["why"][k], k)
        avail = {e["expiry_nse"]: e["available"] for e in d["expiry_list"]}
        self.assertTrue(avail["01-Dec-2026"])
        self.assertFalse(avail["09-Nov-2026"])          # not saved offline: the page greys it out
        _, dec = self.s.get("/api/options?symbol=NIFTY&expiry=01-Dec-2026&strikes=0")
        self.assertEqual(dec["expiry"], "2026-12-01")
        self.assertEqual(len(dec["rows"]), dec["strikes_total"])
        _, rel = self.s.get("/api/options?symbol=RELIANCE")
        self.assertEqual((rel["expiry"], rel["lot_size"]), ("2026-11-23", 500))
        self.assertEqual(self.s.get("/api/options?symbol=NIFTY&expiry=04-Nov-2026")[0], 422)
        self.assertEqual(self.s.get("/api/options?symbol=not!a!symbol")[0], 400)

    def test_price_a_template(self):
        status, d = self.s.post("/api/strategy/price", {"symbol": "NIFTY", "template": "iron-condor",
                                                        "params": {"wings": 200, "width": 100}, "paths": 1000})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["schema"], "bharat-trader/strategy/1")
        self.assertEqual(d["margin"]["required"], 67408.76)
        self.assertEqual(d["margin"]["max_loss"], 4129.12)
        self.assertGreater(d["margin"]["required"], d["margin"]["max_loss"])
        self.assertEqual(d["expiry"]["breakevens"], [22763.53, 23236.47])
        self.assertEqual(d["probability"]["simulated"]["paths"], 1000)
        self.assertIn("formula", d["why"]["margin"])
        self.assertEqual(len(d["curve"]["x"]), len(d["curve"]["expiry_net"]))
        # the same request through the CLI gives the same numbers: one implementation
        st = load("trader-strategy", "strategy")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st.main(["build", "iron-condor", "NIFTY", "--wings", "200", "--width", "100", "--json", "--no-sim",
                     "--offline", UIFIX, "--now", "2026-11-01 10:00"])
        cli = json.loads(out.getvalue())
        self.assertEqual(cli["expiry"], d["expiry"])
        self.assertEqual(cli["margin"], d["margin"])

    def test_calendar_custom_legs_and_horizon(self):
        _, d = self.s.post("/api/strategy/price", {"symbol": "NIFTY", "template": "calendar", "simulate": False})
        self.assertEqual(d["expiries"], ["2026-11-03", "2026-12-01"])
        _, d = self.s.post("/api/strategy/price", {"symbol": "NIFTY", "legs": "+1 CE 23000, +1 PE 23000",
                                                   "at": "today", "iv_shift": 2, "simulate": False})
        self.assertEqual(d["name"], "custom")
        self.assertEqual(d["curve"]["horizon"], "now")
        self.assertTrue(d["expiry"]["unlimited_profit"])

    def test_bad_strategy_requests(self):
        for body, code in (({"symbol": "NIFTY"}, 400), ({"symbol": "NIFTY", "template": "iron-condor", "legs": "+1 CE"}, 400),
                           ({"symbol": "NIFTY", "template": "moon"}, 422),
                           ({"symbol": "NIFTY", "legs": "-1 CE 23010"}, 422),
                           ({"symbol": "NIFTY", "template": "iron-condor", "params": {"lots": 0}}, 400),
                           ({"symbol": "NIFTY", "template": "iron-condor", "paths": 10 ** 6}, 400),
                           ({"symbol": "NIFTY", "template": "iron-condor", "fill": "best"}, 400)):
            status, d = self.s.post("/api/strategy/price", body)
            self.assertEqual(status, code, (body, d))
            self.assertTrue(d["error"])
        self.assertEqual(self.s.post("/api/strategy/price", {"symbol": "NIFTY", "template": "iron-fly"}, token=False)[0],
                         403)


class StrategyDraft(unittest.TestCase):
    """Send to paper: a proposal in trader/orders.json, then trader-risk and a typed yes. Never live."""

    def setUp(self):
        self.s = Running()

    def tearDown(self):
        self.s.close()

    def test_draft_is_a_proposal_that_risk_still_checks(self):
        body = {"symbol": "NIFTY", "template": "iron-fly", "params": {"width": 100}}
        status, d = self.s.post("/api/strategy/draft", body)
        self.assertEqual(status, 409)                       # trader-budget's RELIANCE proposal is there
        self.assertIn("replace", d["error"])
        status, d = self.s.post("/api/strategy/draft", dict(body, replace=True))
        self.assertEqual(status, 200, d)
        self.assertTrue(d["paper"])
        self.assertEqual([o["exchange"] for o in d["orders"]], ["NFO"] * 4)
        self.assertTrue(all(o["tradingsymbol"] is None and o["product"] == "NRML" for o in d["orders"]))
        with open(os.path.join(self.s.dir, "trader", "orders.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("trader-strategy", "proposed", True))
        with open(os.path.join(self.s.dir, "trader", "strategy.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["name"], "iron-fly")
        self.assertTrue(all(not t["ready"] for t in d["tickets"]))
        # trader-risk decides: F&O is off in this config, so every leg is blocked and none can be confirmed
        status, res = self.s.post("/api/risk/check", {})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["pass"], 0)
        status, res = self.s.post("/api/orders/confirm", {"index": 1, "yes_text": "yes"})
        self.assertFalse(res["ok"])
        with open(os.path.join(self.s.dir, "trader", "state", "broker.json"), encoding="utf-8") as fh:
            broker = json.load(fh)
        self.assertFalse([r for r in broker.get("orders", []) if r.get("status") == "placed"
                          and (r.get("order") or {}).get("exchange") == "NFO"])

    def test_draft_refuses_a_non_paper_config(self):
        path = os.path.join(self.s.dir, "trader", "config.json")
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["paper"] = False
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        status, d = self.s.post("/api/strategy/draft", {"symbol": "NIFTY", "template": "iron-fly", "replace": True})
        self.assertEqual(status, 409)
        self.assertIn("paper", d["error"])


class PaperFlow(unittest.TestCase):
    """The writes: the same commands and checks as the terminal, never a live order."""

    def setUp(self):
        self.s = Running()

    def tearDown(self):
        self.s.close()

    def state(self, *parts):
        with open(os.path.join(self.s.dir, "trader", *parts), encoding="utf-8") as fh:
            return json.load(fh)

    def test_risk_check_records_verdicts(self):
        os.remove(os.path.join(self.s.dir, "trader", "state", "risk.json"))
        _, o = self.s.get("/api/orders")
        self.assertFalse(o["tickets"][0]["ready"])
        status, res = self.s.post("/api/risk/check", {})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["pass"], 1)
        self.assertEqual(self.state("state", "risk.json")["results"][0]["verdict"], "PASS")
        _, o = self.s.get("/api/orders")
        self.assertTrue(o["tickets"][0]["ready"])

    def test_kill_switch_from_the_risk_panel(self):
        _, r = self.s.get("/api/risk")
        self.assertFalse(r["kill"]["active"])
        self.assertEqual(r["kill"]["word"], "unkill")
        self.assertIn("locks", r["breakers"])
        self.assertEqual(r["rate"]["hard_cap"], 9)
        self.assertEqual(r["scenarios"]["open"]["legs"], 0)        # a cash-only account: no F&O book
        status, res = self.s.post("/api/risk/kill", {"reason": "testing the switch"})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"], res)
        self.assertIn("KILL SWITCH ON", res["output"])
        self.assertIn("Revoked 1 recorded entry PASS", res["output"])
        self.assertEqual(self.state("state", "risk.json")["results"][0]["verdict"], "BLOCK")
        _, o = self.s.get("/api/orders")
        self.assertFalse(o["tickets"][0]["ready"])
        _, r = self.s.get("/api/risk")
        self.assertTrue(r["kill"]["active"])
        self.assertEqual(r["kill"]["reason"], "testing the switch")
        self.assertIn("kill", [x.split(":")[0] for x in r["preview"]["results"][0]["reasons"]])
        status, res = self.s.post("/api/risk/unkill", {"typed": "yes"})
        self.assertFalse(res["ok"])
        self.assertIn("type the word unkill", res["output"])
        status, _ = self.s.post("/api/risk/unkill", {})
        self.assertEqual(status, 400)
        status, res = self.s.post("/api/risk/unkill", {"typed": "unkill"})
        self.assertTrue(res["ok"], res)
        _, r = self.s.get("/api/risk")
        self.assertFalse(r["kill"]["active"])
        self.assertEqual(r["preview"]["results"][0]["verdict"], "PASS")

    def test_risk_panel_shows_the_fno_book(self):
        pf = self.state("portfolio.json")
        pf.setdefault("fno_positions", []).append(
            {"tradingsymbol": "NIFTY26N0322800PE", "exchange": "NFO", "qty": -65, "avg_price": 44.3, "lot_size": 65,
             "ltp": 44.3, "iv": 14.0, "forward": 23022.14})
        pf["margin_blocked"] = 60000
        with open(os.path.join(self.s.dir, "trader", "portfolio.json"), "w", encoding="utf-8") as fh:
            json.dump(pf, fh)
        cfg = self.state("config.json")
        cfg["allow_fno"] = True
        with open(os.path.join(self.s.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        _, r = self.s.get("/api/risk")
        book = r["scenarios"]["open"]
        self.assertEqual(book["legs"], 1)
        g = book["greeks"]["by_underlying"]["NIFTY"]
        self.assertGreater(g["delta_lots"], 0)               # a short put is long delta
        self.assertLess(g["vega"], 0)
        self.assertEqual(len(book["scenarios"]["rows"]), 7)
        self.assertIn("formula", r["why"]["scenarios"])
        self.assertIn("max_scenario_loss", [x["key"] for x in r["fno_limits"]])

    def test_confirm_needs_a_real_yes_then_fill(self):
        status, res = self.s.post("/api/orders/confirm", {"index": 1, "yes_text": "sure"})
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        self.assertIn("Not recorded", res["output"])
        status, res = self.s.post("/api/orders/confirm", {"index": 1, "yes_text": "yes to all of them"})
        self.assertFalse(res["ok"])
        self.assertEqual(self.s.post("/api/orders/confirm", {"index": 1})[0], 400)
        self.assertEqual(self.s.post("/api/orders/confirm", {"index": "1", "yes_text": "yes"})[0], 400)
        status, res = self.s.post("/api/orders/confirm", {"index": 1, "yes_text": "yes"})
        self.assertTrue(res["ok"], res)
        self.assertIn("PAPER-20261029-1", res["output"])
        rec = [r for r in self.state("state", "broker.json")["orders"] if r["status"] == "placed"][-1]
        self.assertEqual(rec["yes_text"], "yes")
        self.assertTrue(rec["paper"])
        self.assertFalse(res["tickets"][0]["ready"])
        # the same order cannot be confirmed twice
        status, res = self.s.post("/api/orders/confirm", {"index": 1, "yes_text": "yes"})
        self.assertFalse(res["ok"])
        status, res = self.s.post("/api/orders/fill", {"index": 1, "price": 1174.3})
        self.assertTrue(res["ok"], res)
        pf = self.state("portfolio.json")
        self.assertEqual(pf["positions"]["RELIANCE"]["qty"], 42)
        status, res = self.s.post("/api/orders/fill", {"index": 1, "leg": "target"})
        self.assertTrue(res["ok"], res)
        _, a = self.s.get("/api/portfolio")
        self.assertNotIn("RELIANCE", [p["symbol"] for p in a["positions"]])
        self.assertGreater(a["today"]["realised"], 0)
        self.assertEqual(self.s.post("/api/orders/fill", {"index": 1, "leg": "sideways"})[0], 400)
        self.assertEqual(self.s.post("/api/orders/fill", {"index": 1, "price": -3})[0], 400)

    def test_decline(self):
        status, res = self.s.post("/api/orders/decline", {"index": 1, "reply": "no, not today"})
        self.assertTrue(res["ok"])
        self.assertTrue(res["tickets"][0]["status"].startswith("DONE: declined"))

    def test_refresh_prices_writes_prices_json(self):
        os.remove(os.path.join(self.s.dir, "trader", "prices.json"))
        status, res = self.s.post("/api/prices/refresh", {})
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(self.state("prices.json")["prices"]), ["RELIANCE", "TCS"])

    def test_config_validate_and_save(self):
        path = os.path.join(self.s.dir, "trader", "config.json")
        before = read_text(path)
        bad = {"capital": -1, "flat_by": "25:00", "product": "BTST", "blocked": ["not a symbol!"],
               "charges": {"gst_pct": "18"}, "special_sessions": [{"date": "x"}]}
        _, res = self.s.post("/api/config/validate", {"config": bad})
        self.assertFalse(res["ok"])
        text = " ".join(res["errors"])
        for word in ("capital", "flat_by", "product", "blocked", "charges.gst_pct"):
            self.assertIn(word, text)
        # paper trading cannot be switched off from the UI
        cfg = json.loads(before)
        status, res = self.s.post("/api/config", {"config": dict(cfg, paper=False)})
        self.assertFalse(res["saved"])
        self.assertIn("paper", " ".join(res["errors"]))
        status, res = self.s.post("/api/config", {"config": dict(cfg, broker="zerodha")})
        self.assertFalse(res["saved"])
        self.assertEqual(read_text(path), before)
        _, res = self.s.post("/api/config/validate", {"config": dict(cfg, no_entries_before="15:00")})
        self.assertFalse(res["ok"])
        _, res = self.s.post("/api/config/validate", {"config": dict(cfg, risk_free_rate=0.06)})
        self.assertEqual((res["errors"], res["warnings"]), ([], []))
        _, res = self.s.post("/api/config/validate", {"config": dict(cfg, mystery=1)})
        self.assertTrue(res["ok"])
        self.assertTrue(any("mystery" in w for w in res["warnings"]))
        good = dict(cfg, capital=200000, max_loss_per_trade=2000)
        status, res = self.s.post("/api/config", {"config": good})
        self.assertTrue(res["saved"], res)
        self.assertIn("₹2,00,000.00", res["summary"])
        self.assertEqual(json.loads(read_text(path))["capital"], 200000)
        self.assertEqual(read_text(path + ".bak"), before)
        self.assertEqual(self.s.post("/api/config", {"nope": 1})[0], 400)


class Pieces(unittest.TestCase):
    def test_ema(self):
        self.assertEqual(api.ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(api.ema([10, 10, 10, 20], 2), [None, 10.0, 10.0, 16.67])

    def test_vwap_resets_each_ist_day_and_skips_zero_volume(self):
        day1 = 1793245500          # 2026-10-29 09:15 IST
        bars = [{"t": day1, "h": 11, "l": 9, "c": 10, "v": 0},
                {"t": day1 + 300, "h": 12, "l": 10, "c": 11, "v": 100},
                {"t": day1 + 600, "h": 14, "l": 12, "c": 13, "v": 300},
                {"t": day1 + 86400, "h": 21, "l": 19, "c": 20, "v": 50}]
        self.assertEqual(api.vwap(bars), [None, 11.0, 12.5, 20.0])

    def test_offline_demo_uses_a_copy(self):
        tmp = serve.offline_workdir(UIFIX)
        try:
            self.assertTrue(os.path.isfile(os.path.join(tmp, "trader", "config.json")))
            self.assertNotEqual(os.path.realpath(tmp), os.path.realpath(UIFIX))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(serve.offline_workdir(os.path.join(FIX, "trader-risk")))

    def test_dispatch_reports_skill_errors_as_messages(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "trader"))
            with open(os.path.join(tmp, "trader", "config.json"), "w") as fh:
                fh.write("{broken")
            ctx = api.Context(tmp, offline=UIFIX)
            status, body = api.dispatch(ctx, "GET", "/api/config")
            self.assertEqual(status, 422)
            self.assertIn("not valid JSON", body["error"])
            status, body = api.dispatch(ctx, "POST", "/api/risk/check", {}, {})
            self.assertEqual(status, 409)
        finally:
            shutil.rmtree(tmp)

    def test_routes_are_one_table(self):
        paths = [(m, p) for m, p, _, _, _ in api.ROUTES]
        self.assertEqual(len(paths), len(set(paths)))
        for m, p, fn, about, writes in api.ROUTES:
            self.assertTrue(p.startswith("/api/"))
            self.assertEqual(writes, m == "POST", p)
            self.assertTrue(about)


class StaticFiles(unittest.TestCase):
    def files(self):
        for base, _, names in os.walk(STATIC):
            for n in names:
                if not n.startswith("."):       # vendor/.gitattributes is for git, never served
                    yield os.path.join(base, n)

    def test_vendored_files_are_byte_exact_in_git(self):
        rules = read_text(os.path.join(STATIC, "vendor", ".gitattributes"))
        self.assertIn("* -text", rules)    # no CRLF conversion on Windows checkouts: the SHA-256 must hold

    def test_no_external_urls_outside_vendor(self):
        vendor = os.path.join(STATIC, "vendor") + os.sep
        hits = []
        for p in self.files():
            if p.startswith(vendor):
                continue
            text = read_text(p)
            for m in re.finditer(r"(?:https?:)?//[a-z0-9-]+\.[a-z0-9.-]+", text, re.I):
                hits.append("%s: %s" % (os.path.relpath(p, ROOT), m.group(0)))
            if re.search(r"\b(?:src|href)\s*=\s*[\"'](?!/static/|#|data:|/)[^\"']", text):
                hits.append("%s: a non-local src/href" % os.path.relpath(p, ROOT))
        self.assertEqual(hits, [])

    def test_no_inline_script_or_style_in_html(self):
        html = read_text(os.path.join(STATIC, "index.html"))
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"\sstyle=")
        self.assertIn('content="__CSRF_TOKEN__"', html)

    def test_only_known_file_types(self):
        for p in self.files():
            name = os.path.basename(p)
            self.assertTrue(name in serve.PLAIN_NAMES or os.path.splitext(name)[1] in serve.TYPES, p)

    def test_vendored_library_matches_its_manifest(self):
        readme = read_text(os.path.join(STATIC, "vendor", "README.md"))
        for name in ("lightweight-charts.standalone.production.js", "LICENSE", "NOTICE"):
            data = blob(os.path.join(STATIC, "vendor", name))
            digest = hashlib.sha256(data).hexdigest()
            self.assertIn(digest, readme, name)
        js = read_text(os.path.join(STATIC, "vendor", "lightweight-charts.standalone.production.js"), 400)
        self.assertIn("Lightweight Charts™ v5.2.1", js)
        self.assertIn("Apache License 2.0", js)
        self.assertIn("Apache License", read_text(os.path.join(STATIC, "vendor", "LICENSE")))
        notice = read_text(os.path.join(ROOT, "NOTICE"))
        self.assertIn("Lightweight Charts", notice)
        self.assertIn("Apache", notice)

    def test_attribution_logo_is_not_turned_off(self):
        for p in self.files():
            if p.endswith(".js") and os.sep + "vendor" + os.sep not in p:
                self.assertNotRegex(read_text(p), r"attributionLogo\s*:\s*false", p)


if __name__ == "__main__":
    unittest.main()
