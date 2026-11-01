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
lab/                     three REAL trader-lab runs, copied to the demo's trader/lab/ (lab.py run orb15-vwap and breakout20-swing --data
                         tests/fixtures/trader-lab --now "2026-11-01 12:00"; nifty-expiry-iron-fly --archive on a local
                         backfill of NSE F&O bhavcopies 2024-01-01 to 2026-10-30); results.json without the trade
                         lists (and the options run without its equity rows, which equity.csv holds).
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
    serve.copy_demo_lab(UIFIX, tmp)
    serve.copy_demo_journal(UIFIX, tmp)
    return tmp


class Running(object):
    """A real server on a free loopback port, serving the offline fixtures."""

    def __init__(self, fresh=False):
        self.dir = tempfile.mkdtemp(prefix="bt-ui-test-") if fresh else workdir()
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
        self.assertEqual(d["tickets"], [])                  # a basket is one ticket
        self.assertFalse(d["basket"]["ready"])
        self.assertEqual(len(d["basket"]["legs"]), 4)
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


class FnoBasket(unittest.TestCase):
    """An F&O basket from the UI: one ticket, one typed yes, a paper fill, MTM, and adjust buttons that only
    write a proposal."""

    def setUp(self):
        self.s = Running()
        self.t = os.path.join(self.s.dir, "trader")

    def tearDown(self):
        self.s.close()

    def jl(self, *parts):
        with open(os.path.join(self.t, *parts), encoding="utf-8") as fh:
            return json.load(fh)

    def js(self, doc, *parts):
        with open(os.path.join(self.t, *parts), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def test_basket_yes_fill_mtm_adjust(self):
        import gzip
        op = load("trader-options", "options")
        broker = load("trader-broker", "ticket")
        with gzip.open(os.path.join(UIFIX, "chain-NIFTY-03-Nov-2026.json.gz"), "rb") as fh:
            chain = op.parse_chain(json.loads(fh.read().decode("utf-8")), "NIFTY")
        self.js(op.analyse(chain, 65, 0.0557), "options.json")
        status, d = self.s.post("/api/strategy/draft", {"symbol": "NIFTY", "template": "iron-condor", "replace": True,
                                                       "params": {"wings": 200, "width": 100}})
        self.assertEqual(status, 200, d)
        cfg = self.jl("config.json")
        cfg.update(paper_spread_share=0, capital=300000)
        self.js(cfg, "config.json")
        pf = self.jl("portfolio.json")
        pf["cash"] = 300000
        self.js(pf, "portfolio.json")
        orders = self.jl("orders.json")["orders"]
        _, o = self.s.get("/api/orders")
        bid = broker.basket_id(orders)
        self.assertEqual(o["basket"]["basket_id"], bid)
        self.js({"date": "2026-10-29", "results": [{"fingerprint": broker.fingerprint(x), "verdict": "PASS",
                                                    "reasons": [], "warnings": [], "basket_id": bid} for x in orders]},
                "state", "risk.json")
        _, o = self.s.get("/api/orders")
        self.assertTrue(o["basket"]["ready"], o["basket"]["status"])
        self.assertEqual(self.s.post("/api/orders/confirm", {"basket": True, "yes_text": "yes"})[0], 400)
        status, res = self.s.post("/api/orders/confirm", {"basket": True, "basket_id": bid, "yes_text": "sure"})
        self.assertFalse(res["ok"])
        status, res = self.s.post("/api/orders/confirm", {"basket": True, "basket_id": bid, "yes_text": "yes"})
        self.assertTrue(res["ok"], res)
        status, res = self.s.post("/api/orders/fill", {"basket": True})
        self.assertTrue(res["ok"], res)
        _, a = self.s.get("/api/portfolio?live=0")
        self.assertGreater(a["margin_blocked"], 0)
        s = a["fno"]["strategies"][0]
        self.assertEqual(len(s["positions"]), 4)
        self.assertEqual(len(s["payoff"]["x"]), len(s["payoff"]["y"]))
        self.assertIn("delta", s["greeks"])
        _, f = self.s.get("/api/fno")
        self.assertEqual(f["strategies"][0]["id"], s["id"])
        placed = len([r for r in self.jl("state", "broker.json")["orders"] if r.get("status") == "placed"])
        self.assertEqual(self.s.post("/api/fno/adjust", {"strategy": "x", "action": "close-all"})[0], 400)
        self.assertEqual(self.s.post("/api/fno/adjust", {"strategy": s["id"], "action": "sell-all"})[0], 400)
        status, res = self.s.post("/api/fno/adjust", {"strategy": s["id"], "action": "close-leg", "leg": 3})
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["placed"])
        doc = self.jl("orders.json")
        self.assertEqual((doc["adjust"]["action"], len(doc["orders"]), doc["orders"][0]["side"]), ("close", 1, "BUY"))
        self.assertEqual(len([r for r in self.jl("state", "broker.json")["orders"] if r.get("status") == "placed"]),
                         placed)                                      # a proposal only: nothing placed
        _, o = self.s.get("/api/orders")
        self.assertFalse(o["basket"]["ready"])                       # needs trader-risk again


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
            self.assertTrue(os.path.isfile(os.path.join(tmp, "trader", "lab", "orb15-vwap", "results.json")))
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


class BrokerRoutes(unittest.TestCase):
    """The live broker on the Portfolio and Settings pages: status and holdings/positions, read only, against the
    local stub broker of tests/test_broker_adapters.py (doc-derived fixtures)."""

    @classmethod
    def setUpClass(cls):
        import test_broker_adapters as tba
        cls.tba = tba
        cls.stub = tba.Stub()
        for b in tba.BROKERS:
            cls.stub.load(b)

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def setUp(self):
        self.dir = workdir()
        self.ctx = api.Context(self.dir, now="2026-10-29 10:00")
        self.kw = api.skill("broker").ADAPTER_KW
        self.kw.clear()
        for b in self.tba.BROKERS:
            self.kw[b] = {"hosts": self.stub.hosts(b), "env": dict(self.tba.ENV)}
        self.stub.hits[:] = []

    def tearDown(self):
        self.kw.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def get(self, path, q=None):
        return api.dispatch(self.ctx, "GET", path, q or {})

    def set_cfg(self, **over):
        p = os.path.join(self.dir, "trader", "config.json")
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg.update(over)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return cfg

    def test_status_names_variables_never_values(self):
        status, st = self.get("/api/broker")
        self.assertEqual(status, 200)
        self.assertEqual((st["mode"], st["broker"]), ("paper", "paper"))
        rows = {r["broker"]: r for r in st["brokers"]}
        self.assertEqual(sorted(rows), ["dhan", "kite", "upstox"])
        self.assertTrue(rows["kite"]["credentials"]["BT_KITE_ACCESS_TOKEN"])
        self.assertFalse(rows["upstox"]["credentials"]["BT_UPSTOX_REDIRECT"])
        self.assertEqual(rows["kite"]["token_cutoff"], "06:00")
        self.assertTrue(rows["dhan"]["capabilities"]["sandbox"])
        self.assertIn("never sends an order", st["rule"])
        text = json.dumps(st)
        for secret in self.tba.TOKENS:
            self.assertNotIn(secret, text)
        self.assertEqual(self.stub.hits, [])                 # no network for the status

    def test_holdings_and_positions_read_only(self):
        status, h = self.get("/api/broker/holdings")
        self.assertEqual(status, 200)
        self.assertFalse(h["connected"])                     # the config broker is paper
        self.assertIn("?broker=", h["note"])
        status, h = self.get("/api/broker/holdings", {"broker": "kite"})
        self.assertEqual(status, 200, h)
        self.assertTrue(h["connected"] and h["read_only"])
        self.assertEqual([r["symbol"] for r in h["rows"]], ["ASHOKLEY", "BHEL", "SBIN"])
        self.assertEqual(h["pnl"], 97.35)
        self.set_cfg(broker="upstox", paper=False, live_enabled={"upstox": True})
        status, p = self.get("/api/broker/positions")
        self.assertEqual((status, p["broker"], p["rows"][0]["product"]), (200, "upstox", "NRML"))
        self.assertEqual(self.get("/api/broker/positions", {"broker": "angel"})[0], 400)
        self.assertEqual([x for x in self.stub.hits if x["method"] != "GET"], [])

    def test_no_token_and_broker_errors_in_plain_words(self):
        self.kw["dhan"]["env"] = {}
        status, h = self.get("/api/broker/holdings", {"broker": "dhan"})
        self.assertEqual(status, 200)
        self.assertFalse(h["connected"])
        self.assertIn("ticket.py login dhan", h["note"])
        self.stub.add("GET", "/upstox/v2/portfolio/long-term-holdings", 401, jload_fix("upstox", "error_token"))
        self.ctx.forget("broker")
        status, body = self.get("/api/broker/holdings", {"broker": "upstox"})
        self.stub.load("upstox")
        self.assertEqual(status, 502)
        self.assertIn("UDAPI100050", body["error"])
        self.assertNotIn("uptok66", body["error"])

    def test_offline_never_calls_a_broker(self):
        ctx = api.Context(self.dir, offline=UIFIX)
        status, h = api.dispatch(ctx, "GET", "/api/broker/holdings", {"broker": "kite"})
        self.assertEqual((status, h["connected"]), (200, False))
        self.assertEqual(self.stub.hits, [])

    def test_going_live_is_never_done_from_the_ui(self):
        cfg = self.set_cfg()
        live = dict(cfg, broker="kite", paper=False, live_enabled={"kite": True}, static_ip="203.0.113.7")
        status, res = api.dispatch(self.ctx, "POST", "/api/config/validate", {}, {"config": live})
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("LIVE with kite" in w for w in res["warnings"]))
        status, res = api.dispatch(self.ctx, "POST", "/api/config", {}, {"config": live})
        self.assertFalse(res["saved"])
        self.assertIn("cannot be changed from this page", res["errors"][0])
        for bad, word in (({"live_enabled": {"kite": "yes"}}, "live_enabled"),
                          ({"live_enabled": {"angel": True}}, "not a broker"),
                          ({"static_ip": "my home"}, "not an IP address"),
                          ({"order_rate_per_sec": 12}, "at most 9"),
                          ({"broker": "kite"}, "the paper broker only works"),
                          ({"broker": "angel", "paper": False}, "the brokers are")):
            _, res = api.dispatch(self.ctx, "POST", "/api/config/validate", {}, {"config": dict(cfg, **bad)})
            self.assertIn(word, " ".join(res["errors"]), bad)
        # a live config on disk: the UI's confirm refuses, the tickets say LIVE
        self.set_cfg(broker="kite", paper=False, live_enabled={"kite": True})
        status, body = api.dispatch(self.ctx, "POST", "/api/orders/confirm", {}, {"index": 1, "yes_text": "yes"})
        self.assertEqual(status, 422)
        self.assertIn("paper account only", body["error"])
        _, o = self.get("/api/orders")
        self.assertEqual((o["tickets"][0]["paper"], o["tickets"][0]["broker"]), (False, "kite"))
        self.assertEqual(self.stub.hits, [])


def jload_fix(broker, case):
    with open(os.path.join(FIX, "trader-broker", broker, case + ".json"), encoding="utf-8") as fh:
        return json.load(fh)["response"]["body"]


class LabAndSetup(unittest.TestCase):
    """The Strategy Lab page's route and the first-run setup guide (onboarding) on an empty folder."""

    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_lab_runs_are_trader_labs_own_numbers(self):
        def run_of(d, n):
            return [r for r in d["runs"] if r["name"] == n][0]

        status, d = self.s.get("/api/lab")
        self.assertEqual(status, 200)
        names = [r["name"] for r in d["runs"]]
        self.assertEqual(sorted(names), ["breakout20-swing", "nifty-expiry-iron-fly", "orb15-vwap"])
        fly = [r for r in d["runs"] if r["name"] == "nifty-expiry-iron-fly"][0]
        fres = json.loads(read_text(os.path.join(UIFIX, "lab", "nifty-expiry-iron-fly", "results.json")))
        self.assertEqual(fly["kind"], "options")
        self.assertEqual(fly["options"]["whole_sample"]["return_on_margin_annual_pct"],
                         fres["options"]["whole_sample"]["return_on_margin_annual_pct"])
        self.assertEqual(fly["options"]["whole_sample"]["tail_loss_5pct"], fres["options"]["whole_sample"]["tail_loss_5pct"])
        self.assertIn("formula", fly["options"]["why"]["return_on_margin"])
        self.assertIn("end-of-day only", fly["limits"])
        self.assertIsNone(run_of(d, "orb15-vwap")["options"])
        res = json.loads(read_text(os.path.join(UIFIX, "lab", "orb15-vwap", "results.json")))
        run = [r for r in d["runs"] if r["name"] == "orb15-vwap"][0]
        self.assertEqual(run["verdict"], "REJECT")
        self.assertEqual(run["oos"]["trades"], res["walk_forward"]["stats"]["trades"])
        self.assertEqual(run["oos"]["expectancy_r"], res["walk_forward"]["stats"]["expectancy_r"])
        self.assertEqual(run["gates"], res["gates"])
        self.assertEqual(run["monte_carlo"]["final_p5"], res["monte_carlo"]["final_p5"])
        self.assertTrue(run["verdict_why"].startswith("6 of 8 gates failed"))
        for k in ("oos_expectancy", "mc_range", "dsr", "pbo"):
            self.assertIn("formula", run["why"][k])
        # the curve comes from equity.csv, thinned to at most MAX_CURVE points, the last day kept
        long_run = [r for r in d["runs"] if r["name"] == "breakout20-swing"][0]
        self.assertLessEqual(len(long_run["equity"]), api.MAX_CURVE + 1)
        last = read_text(os.path.join(UIFIX, "lab", "breakout20-swing", "equity.csv")).strip().split("\n")[-1]
        self.assertEqual(long_run["equity"][-1]["date"], last.split(",")[0])
        # honest: what a backtest does not show, and no promise words
        self.assertIn("does not show", d["limits"])
        text = json.dumps(d).lower()
        for word in ("guaranteed", "sure-shot", "risk-free", "sure shot"):
            self.assertNotIn(word, text)

    def test_lab_without_runs(self):
        empty = tempfile.mkdtemp(prefix="bt-ui-empty-")
        try:
            status, d = api.dispatch(api.Context(empty), "GET", "/api/lab", {}, None)
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual((status, d["runs"]), (200, []))
        self.assertEqual(d["discovery"], [])
        self.assertIn("lab.py run", d["how"])

    def test_lab_lists_discovery_runs(self):
        wd = tempfile.mkdtemp(prefix="bt-ui-disc-")
        try:
            disc = api.skill("discover")
            lab = disc.lab_api()
            fix = os.path.join(ROOT, "tests", "fixtures", "trader-lab")
            data, cx, _ = disc.load_market(lab, list(disc.LAB10), "1d", fix, None, False, None, None)
            out = os.path.join(wd, "trader", "discover")
            disc.start(lab, "ui-run", "1d", "lab10", 20, 1, out=out, lab_out=None, data=data, ctx=cx,
                       now=lab.ist_now("2026-11-01 12:00"), settings={"bootstrap": 100})
            status, d = api.dispatch(api.Context(wd), "GET", "/api/lab", {}, None)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in d["discovery"]], ["ui-run"])
        run = d["discovery"][0]
        self.assertEqual((run["state"], run["counts"]["search_trials"], run["finalists"]), ("decided", 20, []))
        self.assertIn("holdout", d["discovery_limits"])

    def test_onboarding_preview_is_size_py_init(self):
        _, d = self.s.get("/api/onboarding")
        self.assertIsNone(d["preview"])
        self.assertTrue(d["config_exists"])
        self.assertIn("server.py", d["mcp"]["snippet"])
        self.assertIn("--workdir", d["mcp"]["snippet"])
        self.assertNotIn("custom", d["profiles"])
        self.assertTrue(d["brokers"])
        for b in d["brokers"]:
            self.assertTrue(all(isinstance(v, bool) for v in b["credentials"].values()))   # names, never values
        _, d = self.s.get("/api/onboarding?capital=250000&risk_pct=0.5&max_trades=4&product=CNC&broker_profile=zerodha")
        cfg = d["preview"]["config"]
        want = load("trader-budget", "size").build_config(250000, 0.5, 4, "CNC", None, "zerodha")
        self.assertEqual(d["preview"]["errors"], [])
        for k in ("capital", "risk_per_trade_pct", "max_loss_per_trade", "max_daily_loss", "max_trades_per_day",
                  "product", "broker_profile"):
            self.assertEqual(cfg[k], want[k], k)
        for k in api.GUARDED:
            self.assertNotIn(k, cfg)            # going live is never part of the setup guide
        self.assertIn("₹2,50,000.00", d["preview"]["summary"])
        _, d = self.s.get("/api/onboarding?capital=100")
        self.assertIsNone(d["preview"]["config"])
        self.assertIn("5,000", d["preview"]["errors"][0])
        _, d = self.s.get("/api/onboarding?capital=100000&risk_pct=9")
        self.assertTrue(d["preview"]["errors"])

    def test_first_run_on_an_empty_folder(self):
        fresh = Running(fresh=True)
        try:
            _, m = fresh.get("/api/meta")
            self.assertFalse(m["config_exists"])
            self.assertTrue(m["paper"])
            _, d = fresh.get("/api/onboarding?capital=150000&risk_pct=1&max_trades=3&product=MIS")
            self.assertFalse(d["config_exists"])
            status, res = fresh.post("/api/config", {"config": d["preview"]["config"]})
            self.assertEqual(status, 200)
            self.assertTrue(res["saved"], res)
            saved = json.loads(read_text(os.path.join(fresh.dir, "trader", "config.json")))
            self.assertEqual(saved["capital"], 150000)
            self.assertEqual(saved["max_loss_per_trade"], 1500)
            _, m = fresh.get("/api/meta")
            self.assertTrue(m["config_exists"])
            self.assertTrue(m["paper"])          # still paper: the guide cannot turn it off
            _, risk = fresh.get("/api/risk")
            limits = {x["key"]: x["value"] for x in risk["limits"]}
            self.assertEqual(limits["max_loss_per_trade"], 1500)
            # every GET route still answers on the brand-new folder
            for r in api.ROUTES:
                if r[0] == "GET" and r[1] not in ("/api/broker/holdings", "/api/broker/positions"):
                    status, body = fresh.get(r[1] + Routes.QUERY.get(r[1], ""))
                    self.assertEqual(status, 200, (r[1], body))
        finally:
            fresh.close()


class CotraderRoutes(unittest.TestCase):
    """The Co-trader page and the Today card: trader-cotrader's own run, the calibration, and the draft that goes
    through trader-budget and trader-risk like any plan (tests/test_cotrader.py has the method)."""

    @classmethod
    def setUpClass(cls):
        import test_cotrader as tc
        cls.tc = tc
        cls.dir = workdir()
        shutil.rmtree(os.path.join(cls.dir, "trader", "lab"), ignore_errors=True)
        tc.run_lab(cls.dir, [tc.SMA20_WATCH, tc.SMA20_REJECT], tc.SIGNAL)
        ct = api.skill("cotrader")
        ct.run_today(cls.dir, "validated", None, ct.ist_now(tc.SIGNAL), tc.LABFIX, None, False, True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def ctx(self, now):
        return api.Context(self.dir, now=now)

    def test_get_shows_the_run_and_its_calibration(self):
        status, d = api.dispatch(self.ctx("2026-09-18 16:30"), "GET", "/api/cotrader", {}, None)
        self.assertEqual(status, 200, d)
        ids = [c["id"] for c in d["run"]["candidates"]]
        self.assertEqual(sorted(ids), ["2026-09-21-t-sma20-watch-AXISBANK", "2026-09-21-t-sma20-watch-BHARTIARTL"])
        c = d["run"]["candidates"][0]
        for k in ("tier", "evidence", "risk", "sizing", "explain", "second_opinion", "similar", "why_not"):
            self.assertIn(k, c)
        self.assertFalse(d["stale"])
        self.assertEqual(d["calibration"]["pending"], 2)            # logged, not resolved yet
        self.assertIn("not a forecast", d["limits"])
        _, later = api.dispatch(self.ctx("2026-09-21 10:00"), "GET", "/api/cotrader", {}, None)
        self.assertTrue(later["stale"])

    def test_draft_goes_through_budget_and_risk(self):
        ctx = self.ctx("2026-09-21 10:00")
        cid = "2026-09-21-t-sma20-watch-BHARTIARTL"
        for bad in ("../x", "BHARTIARTL", ""):
            self.assertEqual(api.dispatch(ctx, "POST", "/api/cotrader/draft", {}, {"id": bad})[0], 400, bad)
        self.assertEqual(api.dispatch(ctx, "POST", "/api/cotrader/draft", {}, {"id": "2026-09-21-nope-SBIN"})[0], 404)
        early = api.dispatch(self.ctx("2026-09-18 16:30"), "POST", "/api/cotrader/draft", {}, {"id": cid})
        self.assertEqual(early[0], 409)                              # a daily plan fills on its own day
        self.assertIn("2026-09-21", early[1]["error"])
        before = len((json.loads(read_text(os.path.join(self.dir, "trader", "state", "broker.json"))) or {})
                     .get("orders", []))
        status, d = api.dispatch(ctx, "POST", "/api/cotrader/draft", {}, {"id": cid})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["paper"])
        orders = json.loads(read_text(os.path.join(self.dir, "trader", "orders.json")))
        self.assertEqual(orders["created_by"], "trader-budget")
        self.assertEqual(orders["orders"][0]["symbol"], "BHARTIARTL")
        self.assertEqual(orders["orders"][0]["plan"]["setup"], "cotrader-watch:t-sma20-watch")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "trader", "plan.json.bak")))
        self.assertEqual(len(d["risk"]["results"]), 1)              # trader-risk recorded its verdict
        after = json.loads(read_text(os.path.join(self.dir, "trader", "state", "broker.json"))).get("orders", [])
        self.assertEqual(len(after), before)                        # nothing placed: the typed yes is still to come

    def test_draft_refuses_a_live_config(self):
        path = os.path.join(self.dir, "trader", "config.json")
        old = read_text(path)
        try:
            cfg = json.loads(old)
            cfg["paper"] = False
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            status, d = api.dispatch(self.ctx("2026-09-21 10:00"), "POST", "/api/cotrader/draft", {},
                                     {"id": "2026-09-21-t-sma20-watch-AXISBANK"})
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(old)
        self.assertEqual(status, 409)
        self.assertIn("paper account only", d["error"])

    def test_run_on_the_offline_demo_is_a_plain_no_edge(self):
        wd = workdir()
        try:
            ctx = api.Context(wd, offline=UIFIX)
            self.assertIsNone(api.dispatch(ctx, "GET", "/api/cotrader", {}, None)[1]["run"])
            self.assertEqual(api.dispatch(ctx, "POST", "/api/cotrader/run", {}, {"tier": "everything"})[0], 400)
            status, d = api.dispatch(ctx, "POST", "/api/cotrader/run", {}, {"tier": "validated"})
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertEqual(status, 200, d)
        self.assertEqual(d["run"]["candidates"], [])
        self.assertEqual(d["run"]["no_edge"]["decision"], "sit out")
        self.assertIn("No validated strategy", d["run"]["no_edge"]["reasons"][0])


class AutopilotRoutes(unittest.TestCase):
    """The Auto-pilot page: status, start/stop with a typed confirmation (paper only), and a replay in a sandbox
    (tests/test_autopilot.py has the loop itself)."""

    def setUp(self):
        import test_autopilot as ta
        self.ta = ta
        self.dir = ta.make_wd(strategies={"t-sma20-watch": {"capital": 60000, "product": "MIS"}})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def ctx(self, now="2026-09-22 10:00"):
        return api.Context(self.dir, offline=FIX, now=now)

    def test_status_start_stop_and_the_typed_words(self):
        ctx = self.ctx()
        status, d = api.dispatch(ctx, "GET", "/api/autopilot", {}, None)
        self.assertEqual(status, 200)
        self.assertFalse(d["running"])
        self.assertEqual((d["start_words"], d["stop_words"]), ("start paper", "stop paper"))
        self.assertIn("t-sma20-watch", d["settings"]["strategies"])
        self.assertIn("Paper only", d["note"])
        for typed in (None, "", "yes", "start", "start live"):
            self.assertEqual(api.dispatch(ctx, "POST", "/api/autopilot/start", {}, {"typed": typed})[0], 400, typed)
        status, d = api.dispatch(ctx, "POST", "/api/autopilot/stop", {}, {"typed": "Stop  Paper"})
        self.assertEqual(status, 200)
        self.assertIn("no auto-pilot loop is running", d["stopped"])
        # the offline demo runs one step on the pinned clock (a Tuesday, 10:00: the pre-open brief is done)
        status, d = api.dispatch(ctx, "POST", "/api/autopilot/start", {}, {"typed": "start paper"})
        self.assertEqual(status, 200, d)
        self.assertIn("one step", d["started"])
        self.assertEqual(d["day"], "2026-09-22")
        self.assertFalse(d["running"])
        self.assertEqual(d["timeline"][0]["kind"], "pre-open")

    def test_a_live_config_is_refused(self):
        cfg = json.loads(read_text(os.path.join(self.dir, "trader", "config.json")))
        cfg.update(paper=False, broker="upstox", live_enabled={"upstox": True})
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        status, d = api.dispatch(self.ctx(), "POST", "/api/autopilot/start", {}, {"typed": "start paper"})
        self.assertEqual(status, 409)
        self.assertIn("PAPER broker only", d["error"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "trader", "autopilot", "lock.json")))
        status, d = api.dispatch(self.ctx(), "GET", "/api/autopilot", {}, None)
        self.assertTrue(d["problems"])
        self.assertEqual(api.dispatch(self.ctx(), "POST", "/api/autopilot/replay", {}, {"date": "2026-09-21"})[0], 409)

    def test_replay_in_a_sandbox(self):
        ctx = self.ctx()
        for bad in ({}, {"date": "21-09-2026"}, {"date": "2026-09-21", "to": "2026-09-01"},
                    {"date": "2026-09-01", "to": "2026-12-01"}):
            self.assertEqual(api.dispatch(ctx, "POST", "/api/autopilot/replay", {}, bad)[0], 400, bad)
        self.assertEqual(api.dispatch(ctx, "POST", "/api/autopilot/replay", {}, {"date": "2026-09-19"})[0], 409)  # Saturday
        status, d = api.dispatch(ctx, "POST", "/api/autopilot/replay", {}, {"date": "2026-09-21"})
        self.assertEqual(status, 200, d)
        rep = d["reports"][0]
        self.assertEqual((rep["totals"]["fills"], rep["totals"]["realised"]), (4, -255.90))
        self.assertEqual(d["sandbox"], "trader/autopilot/replays/2026-09-21")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "trader", "portfolio.json")))
        _, view = api.dispatch(ctx, "GET", "/api/autopilot", {}, None)
        self.assertEqual(view["replays"], [{"name": "2026-09-21", "days": ["2026-09-21"]}])
class Journal(unittest.TestCase):
    """The Journal page's routes: trader-journal's own numbers, read only, plus your notes."""

    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_journal_is_trader_journals_summary(self):
        status, d = self.s.get("/api/journal")
        self.assertEqual(status, 200, d)
        jr = api.skill("journal")
        want = jr.summary(jr.Paths(self.s.dir), None, True, api.Context(self.s.dir, UIFIX).now_ist().replace(
            tzinfo=None, microsecond=0), recent=0)
        self.assertEqual(d["stats"]["trades"], want["stats"]["trades"])
        self.assertEqual(d["stats"]["net"], want["stats"]["net"])
        self.assertEqual(d["stats"]["trades"], 3)                 # the demo's imported Zerodha week
        self.assertEqual(len(d["stats"]["win_rate_ci"]), 2)
        self.assertIn("Wilson", d["why"]["win_rate"]["formula"])
        self.assertEqual(set(d["by"]), set(jr.GROUPS))
        self.assertEqual(d["behaviour"]["counts"]["off-ticket"], 3)          # traded at the broker, never checked
        self.assertIn("caveats", d["counterfactual"])
        status, lite = self.s.get("/api/journal?lite=1")
        self.assertEqual(status, 200)
        self.assertIn("this_week", lite)
        self.assertNotIn("counterfactual", lite)

    def test_note_round_trip(self):
        _, d = self.s.get("/api/journal")
        eid = d["entries"][0]["id"]
        status, res = self.s.post("/api/journal/note", {"id": eid, "text": "skipped my own rule", "tags": "tilt, revenge"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["tags"], ["tilt", "revenge"])
        _, d = self.s.get("/api/journal")
        e = [x for x in d["entries"] if x["id"] == eid][0]
        self.assertEqual(e["notes"][-1]["text"], "skipped my own rule")
        self.assertEqual(self.s.post("/api/journal/note", {"id": "../x", "text": "x"})[0], 400)
        self.assertEqual(self.s.post("/api/journal/note", {"id": eid, "text": "x"}, token=False)[0], 403)


class Tax(unittest.TestCase):
    """The Tax page's routes: trader-tax's own report, read only, plus the export to trader/tax/."""

    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_tax_is_trader_taxs_report(self):
        status, d = self.s.get("/api/tax?fy=2026-27")
        self.assertEqual(status, 200, d)
        tx = api.skill("tax")
        now = api.Context(self.s.dir, UIFIX).now_ist().replace(tzinfo=None, microsecond=0)
        want = tx.report(tx.Paths(self.s.dir), 2026, "live", None, None, now)
        self.assertEqual(d["heads"]["speculative"]["income"], want["heads"]["speculative"]["income"])
        self.assertEqual(d["turnover"]["total"], want["turnover"]["total"])
        self.assertEqual(d["heads"]["speculative"]["trades"], 3)   # the demo's imported Zerodha week, all intraday
        self.assertEqual(d["act"], "Income-tax Act, 2025")
        self.assertIn("2026-27", d["years"])
        self.assertNotIn("trades", d)
        self.assertIn("not tax advice", d["disclaimer"])
        self.assertIn("5.11", d["why"]["turnover"]["source"])
        status, p = self.s.get("/api/tax?fy=2026-27&book=paper")
        self.assertEqual(status, 200)
        self.assertTrue(p["notes"][0].startswith("PRACTICE"))
        self.assertEqual(self.s.get("/api/tax?fy=26")[0], 400)
        self.assertEqual(self.s.get("/api/tax?book=mine")[0], 400)
        self.assertEqual(self.s.get("/api/tax?fy=2016-17")[0], 422)

    def test_export_needs_the_token_and_writes_trader_tax(self):
        self.assertEqual(self.s.post("/api/tax/export", {"fy": "2026-27"}, token=False)[0], 403)
        status, res = self.s.post("/api/tax/export", {"fy": "2026-27", "book": "live"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["folder"], os.path.join("trader", "tax", "FY2026-27"))
        for f in ("report.md", "summary.csv", "trades.csv", "carry-forward.csv", "tax.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.s.dir, res["folder"], f)), f)


class Frontend(unittest.TestCase):
    """The v2 shell: one nav list, every page module present, the CSP unchanged, no inline code."""

    def js(self, *parts):
        return read_text(os.path.join(STATIC, "js", *parts))

    def test_csp_is_unchanged(self):
        self.assertEqual(serve.CSP, "default-src 'none'; script-src 'self'; style-src 'self' %s; img-src 'self' data:; "
                         "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                         "frame-ancestors 'none'" % serve.LWC_LOGO_STYLE)

    def test_every_nav_page_has_a_module(self):
        nav = self.js("nav.js")
        app = self.js("app.js")
        pages = re.findall(r"^  (\w+): \{ label:", nav, re.M)
        self.assertEqual(len(pages), 17)
        for want in ("today", "chart", "scanner", "options", "strategy", "cotrader", "ask", "portfolio", "risk",
                     "autopilot", "lab", "journal", "tax", "agents", "settings", "welcome", "about"):
            self.assertIn(want, pages)
        modules = re.search(r"const MODULES = \{(.*?)\};", app, re.S).group(1)
        for p in pages:
            self.assertRegex(modules, r"\b%s\b" % p, p)
        groups = re.findall(r"name: '(\w+)'", nav)
        self.assertEqual(groups, ["Discover", "Plan", "Trade", "Review", "Setup"])

    def test_no_page_is_a_placeholder_any_more(self):
        # the Co-trader (#44), Journal (#49) and Auto-pilot (#45) pages are real: no "coming in v0.3" page is left
        self.assertFalse(os.path.exists(os.path.join(STATIC, "js", "pages", "soon.js")))
        self.assertNotIn("soon: true", self.js("nav.js"))
        modules = re.search(r"const MODULES = \{(.*?)\};", self.js("app.js"), re.S).group(1)
        self.assertNotIn("soon", modules)
        for page in ("cotrader", "journal", "autopilot"):
            self.assertIn("import * as %s from './pages/%s.js';" % (page, page), self.js("app.js"))

    def test_every_module_import_resolves(self):
        for base, _, names in os.walk(os.path.join(STATIC, "js")):
            for n in names:
                text = read_text(os.path.join(base, n))
                for imp in re.findall(r"from '(\.{1,2}/[\w/.-]+)'", text):
                    self.assertTrue(os.path.isfile(os.path.normpath(os.path.join(base, imp))), (n, imp))

    def test_no_colour_only_state_and_reduced_motion(self):
        css = read_text(os.path.join(STATIC, "css", "app.css"))
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn('content: "▲"', css)      # gain and loss carry a shape, not only a colour
        self.assertIn('content: "▼"', css)
        self.assertIn('[data-density="expert"] .explain', css)
        for token in ("--up", "--down", "--warn", "--info", "--neutral", "--fs-base", "--sp-4", "--r-md", "--e-2"):
            self.assertIn(token + ":", css)

    def test_keyboard_and_palette_are_wired(self):
        html = read_text(os.path.join(STATIC, "index.html"))
        self.assertIn('id="palette"', html)
        self.assertIn('aria-keyshortcuts="Control+K Meta+K"', html)
        self.assertIn('href="#main"', html)
        self.assertIn("e.key.toLowerCase() === 'k'", self.js("app.js"))


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


class Language(unittest.TestCase):
    """The Settings page's language toggle (POST /api/lang) and the UI's own catalogue (ui/static/js/i18n.js)."""

    def setUp(self):
        self.dir = workdir()
        self.ctx = api.Context(self.dir, now="2026-10-29 10:00", offline=UIFIX)
        self.cfg = os.path.join(self.dir, "trader", "config.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_cfg(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return json.load(fh)

    def test_toggle_saves_only_lang(self):
        before = self.read_cfg()
        self.assertEqual(api.dispatch(self.ctx, "GET", "/api/lang")[1]["lang"], "en")
        self.assertEqual(api.dispatch(self.ctx, "GET", "/api/meta")[1]["lang"], "en")
        status, res = api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "hi-en"})
        self.assertEqual((status, res["saved"], res["lang"]), (200, True, "hi-en"))
        after = self.read_cfg()
        self.assertEqual(after.pop("lang"), "hi-en")
        self.assertEqual(after, before)                               # every other key exactly as it was
        self.assertTrue(os.path.exists(self.cfg + ".bak"))
        self.assertEqual(api.dispatch(self.ctx, "GET", "/api/meta")[1]["lang"], "hi-en")
        self.assertFalse(api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "hi-en"})[1]["saved"])   # no-op
        # the settings page's validator knows the key and its two values
        ok = api.dispatch(self.ctx, "POST", "/api/config/validate", {}, {"config": self.read_cfg()})[1]
        self.assertEqual(ok["errors"], [])
        self.assertFalse(any(w.startswith("lang ") for w in ok["warnings"]), ok["warnings"])
        bad = api.dispatch(self.ctx, "POST", "/api/config/validate", {}, {"config": dict(before, lang="hindi")})[1]
        self.assertIn("lang is \"en\" or \"hi-en\" (Hinglish)", bad["errors"])
        self.assertEqual(api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "en"})[1]["lang"], "en")
        self.assertEqual(self.read_cfg()["lang"], "en")

    def test_toggle_refuses_anything_else_and_never_makes_a_config(self):
        status, res = api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "fr"})
        self.assertEqual(status, 400)
        os.remove(self.cfg)
        status, res = api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "hi-en"})
        self.assertEqual((status, res["saved"]), (200, False))
        self.assertIn("browser", res["note"])
        self.assertFalse(os.path.exists(self.cfg))                    # the setup guide still runs first

    def test_hinglish_text_from_the_server(self):
        api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "hi-en"})
        risk = api.dispatch(self.ctx, "GET", "/api/risk")[1]
        self.assertEqual(risk["rule_words"]["loss"],
                         "Is trade mein aapke max loss per trade se zyada nuksaan ho sakta hai.")
        self.assertEqual(risk["limits"][0]["key"], api.dispatch(self.ctx, "GET", "/api/risk")[1]["limits"][0]["key"])
        api.dispatch(self.ctx, "POST", "/api/lang", {}, {"lang": "en"})
        self.assertIsNone(api.dispatch(self.ctx, "GET", "/api/risk")[1]["rule_words"])

    def test_ui_catalogue_is_complete_in_both_languages(self):
        text = read_text(os.path.join(UI, "static", "js", "i18n.js"))
        m = re.search(r"/\* catalogue:start \*/(.*?)/\* catalogue:end \*/", text, re.S)
        cat = json.loads(m.group(1))
        self.assertEqual(sorted(cat), ["en", "hi-en"])
        self.assertEqual(sorted(cat["en"]), sorted(cat["hi-en"]), sorted(set(cat["en"]) ^ set(cat["hi-en"])))
        for k, v in cat["en"].items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", v)), sorted(re.findall(r"\{(\w+)\}", cat["hi-en"][k])), k)
        # every page in the navigation has a label, and every key a page asks for exists
        for r in re.findall(r"^  (\w+): \{ label:", read_text(os.path.join(UI, "static", "js", "nav.js")), re.M):
            self.assertIn("nav." + r, cat["en"], r)
        used = set()
        for name in ("app.js", "pages/today.js", "pages/cotrader.js", "pages/risk.js", "pages/portfolio.js",
                     "pages/settings.js"):
            used |= set(re.findall(r"\btr?\('([a-z]+\.[\w-]+)'", read_text(os.path.join(UI, "static", "js", name))))
        self.assertTrue(used)
        self.assertEqual(sorted(k for k in used if k not in cat["en"]), [])
        # the safety words survive: one order, the English word yes
        self.assertIn("EK order", cat["hi-en"]["pf.reply_hint"])
        self.assertIn("yes", cat["hi-en"]["lang.yes_note"])


if __name__ == "__main__":
    unittest.main()
