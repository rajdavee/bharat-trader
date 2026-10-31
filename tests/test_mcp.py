"""mcp/server.py: the MCP server, spawned as a subprocess and driven over pipes (and HTTP), offline."""

import base64
import http.client
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT  # noqa: E402

SERVER = os.path.join(ROOT, "mcp", "server.py")
BUNDLE = os.path.join(FIX, "mcp")
MODERN = "2026-07-28"
DISCLAIMER = "Education and tooling, not financial advice."
FP = "9ba8b90f83f96546"        # tests/fixtures/ui/trader/orders.json ticket 1, PASS in state/risk.json
TOOLS = ["market_status", "scan_daily", "scan_orb", "news", "entry_check", "exit_plan", "timemachine_run",
         "size_trade", "risk_check", "paper_orders_show", "paper_order_draft", "paper_order_confirm",
         "portfolio_status", "options_chain", "options_summary", "results_analyse", "insider_deals",
         "insider_disclosures", "config_get", "lab_list", "lab_run", "lab_report", "lab_forward", "strategy_build",
         "strategy_price", "broker_status", "broker_holdings", "broker_positions"]
# one offline call per tool, on the real saved answers in tests/fixtures (see tests/fixtures/mcp/offline.json)
CALLS = [
    ("market_status", {}),
    ("scan_daily", {}),
    ("scan_orb", {}),
    ("news", {"tickers": "RELIANCE,TCS", "hours": 400}),
    ("entry_check", {"symbol": "RELIANCE"}),
    ("exit_plan", {"symbol": "HDFCBANK", "entry": 692.7, "or_high": 692.0, "or_low": 687.85, "time": "10:10"}),
    ("timemachine_run", {"symbol": "HDFCBANK", "entry": 692.7, "stop": 689.9, "target": 698.3, "date": "2026-10-26",
                         "entry_time": "10:10", "replays": 300}),
    ("size_trade", {}),
    ("risk_check", {}),
    ("paper_orders_show", {}),
    ("portfolio_status", {}),
    ("options_chain", {"symbol": "NIFTY", "strikes": 3}),
    ("options_summary", {"symbol": "NIFTY"}),
    ("results_analyse", {"symbols": "TCS"}),
    ("insider_deals", {}),
    ("insider_disclosures", {"days": 4}),
    ("config_get", {}),
    ("lab_list", {}),
    ("lab_run", {"strategy": "rsi2-nifty50"}),
    ("lab_report", {"strategy": "rsi2-nifty50"}),
    ("lab_forward", {"strategy": "rsi2-nifty50"}),
    ("strategy_build", {"template": "iron-condor", "symbol": "NIFTY", "wings": 200, "width": 100, "paths": 1000}),
    ("strategy_price", {"symbol": "NIFTY", "legs": "-1 CE 23000, +1 CE 23000 @01-Dec-2026", "simulate": False}),
    ("broker_status", {}),
    ("broker_holdings", {"broker": "upstox"}),
    ("broker_positions", {}),
    ("paper_order_draft", {}),
    ("paper_order_confirm", {"index": 1, "risk_fingerprint": FP, "yes_text": "yes 42 RELIANCE"}),
]


def meta(caps=None, **extra):
    m = {"io.modelcontextprotocol/protocolVersion": MODERN,
         "io.modelcontextprotocol/clientCapabilities": caps if caps is not None else {},
         "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"}}
    m.update(extra)
    return m


def check_schema(value, schema, path="$"):
    """An independent check of the JSON Schema subset the server publishes (type, const, enum, required,
    properties, additionalProperties, items, pattern, bounds)."""
    out = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t:
        ok = isinstance(value, types[t])
    else:
        ok = True
    if not ok:
        return ["%s is not %s" % (path, t)]
    if "const" in schema and value != schema["const"]:
        out.append("%s != const" % path)
    if "enum" in schema and value not in schema["enum"]:
        out.append("%s not in enum" % path)
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                out.append("%s.%s missing" % (path, k))
        for k, v in value.items():
            if k in schema.get("properties", {}):
                out += check_schema(v, schema["properties"][k], path + "." + k)
            elif schema.get("additionalProperties") is False:
                out.append("%s.%s not allowed" % (path, k))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out += check_schema(v, schema["items"], "%s[%d]" % (path, i))
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        out.append("%s does not match" % path)
    return out


def workdir_copy():
    d = tempfile.mkdtemp(prefix="bt-mcp-test-")
    shutil.copytree(os.path.join(FIX, "ui", "trader"), os.path.join(d, "trader"))
    return d


class Client(object):
    """A minimal MCP client over the server's stdin/stdout. Every stdout line must be JSON-RPC."""

    def __init__(self, *args, **kw):
        self.workdir = kw.pop("workdir", None) or workdir_copy()
        self.own_dir = "workdir" not in kw
        argv = [sys.executable, SERVER, "--offline", BUNDLE, "--workdir", self.workdir] + list(args)
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q = queue.Queue()
        self.bad = []
        self.err = []
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in iter(self.p.stdout.readline, b""):
            try:
                msg = json.loads(line.decode("utf-8"))
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    raise ValueError
            except ValueError:
                self.bad.append(line)
                continue
            self.q.put(msg)
        self.q.put(None)

    def _read_err(self):
        for line in iter(self.p.stderr.readline, b""):
            self.err.append(line.decode("utf-8", "replace"))

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def raw(self, text):
        self.p.stdin.write(text.encode("utf-8") + b"\n")
        self.p.stdin.flush()

    def next(self, timeout=30):
        msg = self.q.get(timeout=timeout)
        if msg is None:
            raise AssertionError("the server closed stdout; stderr: %s" % "".join(self.err))
        return msg

    def wait_for(self, rid, timeout=60, others=None):
        end = time.time() + timeout
        while True:
            msg = self.next(max(0.1, end - time.time()))
            if msg.get("id") == rid and "method" not in msg:
                return msg
            if others is not None:
                others.append(msg)

    def request(self, method, params=None, modern=True, caps=None, others=None, rid=None):
        self.n += 1
        rid = rid if rid is not None else self.n
        params = dict(params or {})
        if modern:
            params.setdefault("_meta", meta(caps))
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.wait_for(rid, others=others)

    def call(self, name, args=None, caps=None, **params):
        p = dict(params, name=name, arguments=args or {})
        return self.request("tools/call", p, caps=caps)

    def initialize(self, version="2025-11-25", caps=None):
        r = self.request("initialize", {"protocolVersion": version, "capabilities": caps or {},
                                        "clientInfo": {"name": "legacy-test", "version": "1"}}, modern=False)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(15)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()
        if self.own_dir:
            shutil.rmtree(self.workdir, ignore_errors=True)
        return self.p.returncode


class ServerCase(unittest.TestCase):
    ARGS = ()

    def setUp(self):
        self.c = Client(*self.ARGS)

    def tearDown(self):
        self.c.close()
        self.assertEqual(self.c.bad, [], "stdout carried something that is not JSON-RPC")

    def result(self, resp):
        self.assertNotIn("error", resp, resp)
        return resp["result"]

    def code(self, resp):
        self.assertIn("error", resp, resp)
        return resp["error"]["code"]


# ---------------------------------------------------------------- lifecycle and version negotiation

class Lifecycle(ServerCase):
    def test_modern_discover(self):
        r = self.result(self.c.request("server/discover"))
        self.assertEqual(r["resultType"], "complete")
        self.assertIn(MODERN, r["supportedVersions"])
        self.assertIn("2025-11-25", r["supportedVersions"])
        for cap in ("tools", "resources", "prompts"):
            self.assertIn(cap, r["capabilities"])
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "bharat-trader")
        self.assertIn("not financial advice", r["instructions"])
        self.assertIn("ttlMs", r)
        self.assertIn(r["cacheScope"], ("public", "private"))

    def test_unsupported_version_lists_the_supported_ones(self):
        r = self.c.request("tools/list", {"_meta": dict(meta(), **{"io.modelcontextprotocol/protocolVersion":
                                                                       "1900-01-01"})})
        self.assertEqual(self.code(r), -32022)
        self.assertEqual(r["error"]["data"]["requested"], "1900-01-01")
        self.assertIn(MODERN, r["error"]["data"]["supported"])

    def test_required_meta_is_required(self):
        self.assertEqual(self.code(self.c.request("tools/list", modern=False)), -32602)
        m = meta()
        del m["io.modelcontextprotocol/clientCapabilities"]
        self.assertEqual(self.code(self.c.request("tools/list", {"_meta": m})), -32602)
        bad_level = meta(**{"io.modelcontextprotocol/logLevel": "loud"})
        self.assertEqual(self.code(self.c.request("tools/list", {"_meta": bad_level})), -32602)

    def test_removed_methods_are_unknown_in_the_modern_era(self):
        self.assertEqual(self.code(self.c.request("ping")), -32601)
        self.assertEqual(self.code(self.c.request("logging/setLevel", {"level": "info"})), -32601)

    def test_legacy_handshake_negotiates_the_version(self):
        r = self.result(self.c.initialize("2025-11-25"))
        self.assertEqual(r["protocolVersion"], "2025-11-25")
        self.assertEqual(r["serverInfo"]["name"], "bharat-trader")
        self.assertIn("tools", r["capabilities"])
        self.assertIn("logging", r["capabilities"])
        self.assertNotIn("resultType", r)
        self.assertEqual(self.result(self.c.request("ping", modern=False)), {})
        tools = self.result(self.c.request("tools/list", modern=False))["tools"]
        self.assertEqual([t["name"] for t in tools], TOOLS)

    def test_legacy_older_and_unknown_versions(self):
        c2 = Client()
        try:
            self.assertEqual(c2.initialize("2025-06-18")["result"]["protocolVersion"], "2025-06-18")
        finally:
            c2.close()
        # a version the server does not know: it answers with the latest one it speaks in that era
        self.assertEqual(self.c.initialize("2024-01-01")["result"]["protocolVersion"], "2025-11-25")

    def test_initialize_is_not_part_of_the_modern_revision(self):
        r = self.c.request("initialize", {"protocolVersion": MODERN, "capabilities": {}}, modern=False)
        self.assertEqual(self.code(r), -32602)
        self.assertIn(MODERN, r["error"]["data"]["supported"])

    def test_requests_before_initialize_need_meta(self):
        self.assertEqual(self.result(self.c.request("ping", modern=False)), {})   # ping is always allowed

    def test_legacy_logging(self):
        self.c.initialize()
        self.result(self.c.request("logging/setLevel", {"level": "info"}, modern=False))
        others = []
        r = self.c.request("tools/call", {"name": "market_status", "arguments": {}}, modern=False, others=others)
        self.assertFalse(self.result(r)["isError"])
        logs = [m for m in others if m.get("method") == "notifications/message"]
        self.assertTrue(logs)
        self.assertEqual(logs[0]["params"]["logger"], "bharat-trader")

    def test_modern_logging_only_when_asked(self):
        others = []
        self.c.request("tools/call", {"name": "market_status", "arguments": {}}, others=others)
        self.assertEqual([m for m in others if m.get("method") == "notifications/message"], [])
        others = []
        self.c.request("tools/call", {"name": "market_status", "arguments": {},
                                      "_meta": meta(**{"io.modelcontextprotocol/logLevel": "debug"})}, others=others)
        self.assertTrue([m for m in others if m.get("method") == "notifications/message"])

    def test_progress_notifications(self):
        others = []
        r = self.c.request("tools/call", {"name": "market_status", "arguments": {},
                                          "_meta": meta(progressToken="p-1")}, others=others)
        self.assertFalse(self.result(r)["isError"])
        prog = [m["params"] for m in others if m.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(prog), 2)
        self.assertTrue(all(p["progressToken"] == "p-1" for p in prog))
        self.assertEqual([p["progress"] for p in prog], sorted(p["progress"] for p in prog))

    def test_cancellation_sends_nothing_for_the_request(self):
        self.c.send({"jsonrpc": "2.0", "id": "slow", "method": "tools/call", "params": {
            "name": "timemachine_run", "_meta": meta(),
            "arguments": {"symbol": "HDFCBANK", "entry": 692.7, "stop": 689.9, "target": 698.3, "date": "2026-10-26",
                          "entry_time": "10:10", "replays": 3000}}})
        self.c.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": "slow", "reason": "test"}})
        seen = []
        r = self.c.request("server/discover", others=seen)
        self.result(r)
        end = time.time() + 90
        while time.time() < end and not any("dropped the answer to cancelled request 'slow'" in e
                                            for e in self.c.err):
            time.sleep(0.1)
        self.assertTrue(any("dropped the answer" in e for e in self.c.err), "".join(self.c.err))
        time.sleep(0.5)
        while not self.c.q.empty():
            seen.append(self.c.q.get())
        self.assertEqual([m for m in seen if m and m.get("id") == "slow"], [])

    def test_shutdown_on_stdin_close(self):
        c2 = Client()
        c2.request("server/discover")
        t = time.time()
        self.assertEqual(c2.close(), 0)
        self.assertLess(time.time() - t, 10)


# ---------------------------------------------------------------- JSON-RPC errors

class Errors(ServerCase):
    def test_unknown_method(self):
        self.assertEqual(self.code(self.c.request("tools/frobnicate")), -32601)

    def test_unknown_tool_and_bad_params(self):
        self.assertEqual(self.code(self.c.call("place_live_order", {})), -32602)
        self.assertEqual(self.code(self.c.request("tools/call", {"arguments": {}})), -32602)
        self.assertEqual(self.code(self.c.request("tools/call", {"name": "market_status", "arguments": [1]})),
                         -32602)

    def test_argument_problems_are_tool_errors(self):
        r = self.result(self.c.call("entry_check", {"symbol": "RELIANCE; rm -rf /"}))
        self.assertTrue(r["isError"])
        self.assertIn(DISCLAIMER, r["content"][0]["text"])
        r = self.result(self.c.call("options_chain", {"symbol": "NIFTY", "strikes": 999}))
        self.assertTrue(r["isError"])
        r = self.result(self.c.call("market_status", {"surprise": True}))
        self.assertTrue(r["isError"])
        self.assertIn("unknown argument surprise", r["content"][0]["text"])
        r = self.result(self.c.call("results_analyse", {}))
        self.assertTrue(r["isError"])

    def test_a_skill_error_is_a_tool_error(self):
        r = self.result(self.c.call("entry_check", {"symbol": "NOSUCHSTOCK"}))
        self.assertTrue(r["isError"])
        self.assertTrue(r["content"][0]["text"])

    def test_parse_and_invalid_request(self):
        self.c.raw("{not json")
        self.assertEqual(self.c.next()["error"]["code"], -32700)
        self.c.send({"id": 7, "method": "tools/list"})
        self.assertEqual(self.c.next()["error"]["code"], -32600)
        self.c.send([{"jsonrpc": "2.0", "id": 8, "method": "tools/list"}])
        self.assertEqual(self.c.next()["error"]["code"], -32600)
        self.c.send({"jsonrpc": "2.0", "id": None, "method": "tools/list"})
        self.assertEqual(self.c.next()["error"]["code"], -32600)

    def test_invalid_cursor(self):
        self.assertEqual(self.code(self.c.request("tools/list", {"cursor": "nonsense"})), -32602)


# ---------------------------------------------------------------- tools

class Tools(ServerCase):
    ARGS = ("--rate-limit", "60")       # one call per tool and then some: above the default 30 a minute

    def test_tools_list_schema_validity(self):
        r = self.result(self.c.request("tools/list"))
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("ttlMs", r)
        self.assertEqual(r["cacheScope"], "private")
        tools = r["tools"]
        self.assertEqual([t["name"] for t in tools], TOOLS)       # deterministic order
        for t in tools:
            self.assertRegex(t["name"], r"^[A-Za-z0-9_.-]{1,128}$")
            self.assertTrue(t["title"])
            self.assertIn(DISCLAIMER, t["description"])
            s = t["inputSchema"]
            self.assertEqual(s["type"], "object")
            self.assertFalse(s["additionalProperties"])
            for k in s.get("required", []):
                self.assertIn(k, s["properties"])
            for k, v in s["properties"].items():
                self.assertIn(v["type"], ("string", "integer", "number", "boolean"), (t["name"], k))
            o = t["outputSchema"]
            self.assertEqual(o["type"], "object")
            self.assertIn("data", o["required"])
            self.assertEqual(o["properties"]["disclaimer"]["const"], DISCLAIMER)
            a = t["annotations"]
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                self.assertIsInstance(a[hint], bool, (t["name"], hint))
            self.assertEqual(a["title"], t["title"])
            if a["readOnlyHint"]:
                self.assertFalse(a["destructiveHint"])
        by = {t["name"]: t for t in tools}
        self.assertTrue(by["market_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(by["paper_order_confirm"]["annotations"]["readOnlyHint"])
        self.assertTrue(by["paper_order_draft"]["annotations"]["destructiveHint"])
        self.assertEqual(by["paper_order_confirm"]["inputSchema"]["required"], ["index", "risk_fingerprint"])
        for n in ("broker_status", "broker_holdings", "broker_positions"):
            self.assertTrue(by[n]["annotations"]["readOnlyHint"], n)
            self.assertFalse(by[n]["annotations"]["destructiveHint"], n)
        self.assertFalse(by["broker_status"]["annotations"]["openWorldHint"])

    def test_broker_tools_read_only_and_offline(self):
        st = self.result(self.c.call("broker_status", {}))["structuredContent"]["data"]
        self.assertEqual(st["mode"], "paper")
        self.assertEqual(sorted(b["broker"] for b in st["brokers"]), ["dhan", "kite", "upstox"])
        for b in st["brokers"]:
            self.assertTrue(all(isinstance(v, bool) for v in b["credentials"].values()))
        h = self.result(self.c.call("broker_holdings", {"broker": "kite"}))["structuredContent"]["data"]
        self.assertEqual((h["connected"], h["read_only"], h["rows"]), (False, True, []))
        self.assertIn("Offline", h["note"])
        r = self.result(self.c.call("broker_positions", {"broker": "angel"}))
        self.assertTrue(r["isError"])

    def test_every_tool_offline(self):
        tools = {t["name"]: t for t in self.result(self.c.request("tools/list"))["tools"]}
        self.assertEqual(sorted(n for n, _ in CALLS), sorted(TOOLS))
        for name, args in CALLS:
            r = self.result(self.c.call(name, args))
            self.assertFalse(r.get("isError"), (name, r["content"][0]["text"][:400]))
            sc = r["structuredContent"]
            self.assertEqual(check_schema(sc, tools[name]["outputSchema"]), [], name)
            self.assertEqual(json.loads(r["content"][0]["text"]), sc, name)
            self.assertEqual(sc["disclaimer"], DISCLAIMER)
            self.assertTrue(sc["offline"])
            self.assertTrue(sc["paper_only"])
            self.assertEqual(sc["tool"], name)
        # the data is the skills' own answers
        got = {n: self.result(self.c.call(n, a))["structuredContent"]["data"] for n, a in CALLS[:6]}
        self.assertTrue(got["market_status"]["open"])
        self.assertEqual(got["scan_orb"]["mode"], "orb")
        self.assertTrue(got["scan_daily"]["hits"])
        self.assertEqual(got["entry_check"]["status"], "triggered")
        self.assertEqual(got["exit_plan"]["stop"], 689.9)

    def test_options_summary_has_the_numbers(self):
        d = self.result(self.c.call("options_summary", {"symbol": "NIFTY"}))["structuredContent"]["data"]
        for k in ("pcr_oi", "max_pain", "walls", "expected_move", "iv_rank", "atm_iv"):
            self.assertIn(k, d)
        self.assertNotIn("rows", d)
        self.assertIsNotNone(d["iv_rank"]["rank"])

    def test_strategy_tools_are_trader_strategy_numbers(self):
        d = self.result(self.c.call("strategy_build", {"template": "iron-condor", "symbol": "NIFTY", "wings": 200,
                                                       "width": 100, "paths": 1000}))["structuredContent"]["data"]
        self.assertEqual(d["schema"], "bharat-trader/strategy/1")
        self.assertEqual([(l["side"], l["option_type"], l["strike"]) for l in d["legs"]],
                         [("BUY", "PE", 22700), ("SELL", "PE", 22800), ("SELL", "CE", 23200), ("BUY", "CE", 23300)])
        self.assertEqual(d["margin"]["label"], "Margin required (estimate, +/-15%)")
        self.assertEqual(d["margin"]["required"], 67408.76)
        self.assertEqual(d["margin"]["max_loss"], 4129.12)
        self.assertEqual(d["expiry"]["breakevens"], [22763.53, 23236.47])
        self.assertIsNotNone(d["probability"]["simulated"])
        self.assertNotIn("orders", d)                   # read only: no proposal, no order path
        self.assertNotIn("x", d["curve"])
        r = self.result(self.c.call("strategy_price", {"symbol": "NIFTY", "legs": "-1 CE 23000", "simulate": False,
                                                       "include_curve": True}))
        d = r["structuredContent"]["data"]
        self.assertTrue(d["expiry"]["unlimited_loss"])
        self.assertEqual(d["margin"]["method"], "span-style")
        self.assertEqual(len(d["curve"]["x"]), len(d["curve"]["expiry_net"]))
        bad = self.result(self.c.call("strategy_price", {"symbol": "NIFTY", "legs": "-1 CE 23010"}))
        self.assertTrue(bad.get("isError"))
        self.assertIn("not a listed strike", bad["content"][0]["text"])
        tools = {t["name"]: t for t in self.result(self.c.request("tools/list"))["tools"]}
        for n in ("strategy_build", "strategy_price"):
            self.assertTrue(tools[n]["annotations"]["readOnlyHint"])

    def test_pagination(self):
        c2 = Client("--page-size", "5")
        try:
            names, cursor, pages = [], None, 0
            while True:
                params = {"cursor": cursor} if cursor else {}
                r = c2.request("tools/list", params)["result"]
                names += [t["name"] for t in r["tools"]]
                pages += 1
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            self.assertEqual(names, TOOLS)
            self.assertEqual(pages, -(-len(TOOLS) // 5))
            r = c2.request("prompts/list", {"cursor": json.dumps({"k": "tools", "o": 5})})
            self.assertEqual(r["error"]["code"], -32602)
        finally:
            c2.close()

    def test_rate_limit(self):
        c2 = Client("--rate-limit", "2")
        try:
            out = [c2.call("market_status")["result"] for _ in range(3)]
            self.assertEqual([r["isError"] for r in out], [False, False, True])
            self.assertIn("rate limit", out[2]["content"][0]["text"])
        finally:
            c2.close()

    def test_writes_stay_in_the_workdir_trader_folder(self):
        def tree(d):
            out = {}
            for base, _, files in os.walk(d):
                for f in files:
                    p = os.path.join(base, f)
                    with open(p, "rb") as fh:
                        out[os.path.relpath(p, d)] = fh.read()
            return out
        before = tree(self.c.workdir)
        fix_before = tree(FIX)
        for name, args in CALLS:
            self.result(self.c.call(name, dict(args, save=True) if name in ("exit_plan", "entry_check") else args))
        self.result(self.c.call("risk_check", {"record": True}))
        after = tree(self.c.workdir)
        changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
        self.assertTrue(changed)
        for k in changed:
            self.assertTrue(k.startswith("trader" + os.sep), k)
        self.assertEqual(tree(FIX), fix_before, "the fixtures must never change")


# ---------------------------------------------------------------- the strategy lab

WATCH_SPEC = {
    "name": "t-watch", "title": "a test spec with lenient gates", "timeframe": "5m", "product": "MIS",
    "universe": ["RELIANCE"], "params": {"stop_pct": 0.5},
    "entry": {"all": [["close", ">", 0], {"time_between": ["09:50", "09:50"]}]},
    "stop": {"pct": "$stop_pct"}, "target": {"r": 2}, "grid": {"stop_pct": [0.5, 0.75]},
    "walk_forward": {"in_sample": 10, "out_of_sample": 5},
    "gates": {"min_oos_trades": 0, "min_oos_expectancy_r": -100, "min_oos_profit_factor": 0, "max_drawdown_pct": 100,
              "min_dsr": 0, "max_pbo": 1.01, "min_neighbourhood_positive": 0, "min_forward_trades": 100,
              "demote_drawdown_mult": 1000}}


class Lab(ServerCase):
    def data(self, name, args):
        r = self.result(self.c.call(name, args))
        self.assertFalse(r["isError"], r["content"][0]["text"][:400])
        return r["structuredContent"]["data"]

    def test_run_with_progress_then_report_and_list(self):
        others = []
        r = self.c.request("tools/call", {"name": "lab_run", "arguments": {"strategy": "rsi2-nifty50"},
                                          "_meta": meta(progressToken="lab-1")}, others=others)
        d = self.result(r)["structuredContent"]["data"]
        prog = [m["params"]["progress"] for m in others if m.get("method") == "notifications/progress"]
        self.assertGreater(len(prog), 5)                        # one per grid point, between start and done
        self.assertEqual(prog, sorted(prog))
        self.assertEqual(len(set(prog)), len(prog))
        self.assertEqual(d["verdict"], "REJECT")              # the honest answer on the saved bars
        self.assertEqual(d["results"]["data"]["to"], "2026-10-23")   # the pinned clock: no later bar seen
        self.assertIn("What this does NOT prove", d["scorecard"])
        self.assertNotIn("trades", d["results"])
        folder = os.path.join(self.c.workdir, "trader", "lab", "rsi2-nifty50")
        for f in ("spec.json", "results.json", "scorecard.md", "equity.csv", "status.json"):
            self.assertTrue(os.path.exists(os.path.join(folder, f)), f)
        rep = self.data("lab_report", {"strategy": "rsi2-nifty50", "include_trades": True})
        self.assertEqual(rep["verdict"], "REJECT")
        self.assertEqual(rep["scorecard"], d["scorecard"])
        self.assertTrue(rep["oos_trades"])
        ls = self.data("lab_list", {})
        self.assertGreaterEqual(len(ls["library"]), 6)
        self.assertEqual([x["name"] for x in ls["runs"]], ["rsi2-nifty50"])
        fw = self.data("lab_forward", {"strategy": "rsi2-nifty50"})
        self.assertEqual(fw["forward"]["plans"], [])          # REJECT: no paper plans
        self.assertIsNone(fw["drafted"])

    def test_forward_drafts_only_through_paper_order_draft(self):
        d = os.path.join(self.c.workdir, "trader", "lab", "t-watch")
        os.makedirs(d)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(WATCH_SPEC, fh)
        run = self.data("lab_run", {"strategy": "t-watch"})
        self.assertEqual(run["verdict"], "WATCH", run["verdict_why"])
        before = len(broker_records(self.c.workdir))
        fw = self.data("lab_forward", {"strategy": "t-watch"})
        self.assertGreater(fw["forward"]["settled"], 0)        # 26 to 28 Oct: bars the backtest never saw
        self.assertEqual([p["symbol"] for p in fw["forward"]["plans"]], ["RELIANCE"])
        self.assertIn("draft", fw["next_step"])
        self.assertIsNone(fw["drafted"])
        self.assertFalse(os.path.exists(os.path.join(self.c.workdir, "trader", "plan.json.bak")))
        dr = self.data("lab_forward", {"strategy": "t-watch", "draft": "RELIANCE"})
        self.assertTrue(dr["drafted"]["tickets"])
        self.assertIn("risk", dr["drafted"])
        with open(os.path.join(self.c.workdir, "trader", "plan.json"), encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual((plan["setup"], plan["symbol"], plan["date"]), ("lab:t-watch", "RELIANCE", "2026-10-29"))
        with open(os.path.join(self.c.workdir, "trader", "orders.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["orders"][0]["symbol"], "RELIANCE")
        self.assertEqual(len(broker_records(self.c.workdir)), before)     # drafted, never placed
        r = self.result(self.c.call("lab_forward", {"strategy": "t-watch", "draft": "TCS"}))
        self.assertTrue(r["isError"])

    def test_lab_errors(self):
        r = self.result(self.c.call("lab_run", {"strategy": "../../etc"}))
        self.assertTrue(r["isError"])
        self.assertIn("invalid arguments", r["content"][0]["text"])
        r = self.result(self.c.call("lab_report", {"strategy": "orb15-vwap"}))
        self.assertTrue(r["isError"])
        self.assertIn("lab_run", r["content"][0]["text"])
        r = self.result(self.c.call("lab_forward", {"strategy": "orb15-vwap"}))
        self.assertTrue(r["isError"])
        self.assertIn("run it first", r["content"][0]["text"])
        r = self.result(self.c.call("lab_run", {"strategy": "no-such-thing"}))
        self.assertTrue(r["isError"])


class LabBudget(unittest.TestCase):
    def test_lab_run_stops_at_its_time_budget(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bt_mcp_toolkit_budget", os.path.join(ROOT, "mcp", "toolkit.py"))
        tk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tk)
        wd = workdir_copy()
        real = tk.time

        class Clock(object):
            t = 0.0

            def monotonic(self):
                Clock.t += 4.0                                  # every check is 4 seconds later
                return Clock.t

            def __getattr__(self, name):
                return getattr(real, name)

        try:
            kit = tk.Kit(wd, offline=BUNDLE)
            tk.time = Clock()
            with self.assertRaises(tk.ToolError) as cm:
                kit.call("lab_run", {"strategy": "rsi2-nifty50", "max_seconds": 5})
            self.assertIn("max_seconds", str(cm.exception))
            self.assertFalse(os.path.exists(os.path.join(wd, "trader", "lab", "rsi2-nifty50", "results.json")))
        finally:
            tk.time = real
            shutil.rmtree(wd, ignore_errors=True)


# ---------------------------------------------------------------- resources and prompts

class Resources(ServerCase):
    def test_list_read_and_templates(self):
        r = self.result(self.c.request("resources/list"))
        uris = {x["uri"]: x for x in r["resources"]}
        for name in ("plan.json", "watchlist.json", "portfolio.json", "config.json", "state/risk.json"):
            self.assertIn("bharat-trader://trader/" + name, uris)
        self.assertEqual(uris["bharat-trader://trader/plan.json"]["mimeType"], "application/json")
        self.assertIn("ttlMs", r)
        doc = self.result(self.c.request("resources/read", {"uri": "bharat-trader://trader/plan.json"}))
        text = doc["contents"][0]["text"]
        with open(os.path.join(self.c.workdir, "trader", "plan.json"), encoding="utf-8") as fh:
            self.assertEqual(json.loads(text), json.load(fh))
        self.assertIn("ttlMs", doc)
        t = self.result(self.c.request("resources/templates/list"))["resourceTemplates"]
        self.assertEqual([x["uriTemplate"] for x in t], ["bharat-trader://trader/{file}",
                                                         "bharat-trader://trader/state/{file}"])
        st = self.result(self.c.request("resources/read", {"uri": "bharat-trader://trader/state/risk.json"}))
        self.assertIn(FP, st["contents"][0]["text"])

    def test_missing_and_hostile_uris(self):
        for uri in ("bharat-trader://trader/strategy.json", "bharat-trader://trader/../../etc/passwd.json",
                    "bharat-trader://trader/state/../config.json", "file:///etc/passwd",
                    "bharat-trader://trader/%2e%2e/x.json", "bharat-trader://trader/a/b/c.json"):
            self.assertEqual(self.code(self.c.request("resources/read", {"uri": uri})), -32602, uri)

    def test_legacy_resource_not_found_code(self):
        self.c.initialize()
        r = self.c.request("resources/read", {"uri": "bharat-trader://trader/journal.json"}, modern=False)
        self.assertEqual(self.code(r), -32002)


class Prompts(ServerCase):
    NAMES = ["morning_briefing", "intraday_plan", "options_income_scan", "weekly_review", "research_strategy"]

    def test_list_and_get(self):
        ps = self.result(self.c.request("prompts/list"))["prompts"]
        self.assertEqual([p["name"] for p in ps], self.NAMES)
        args = {"morning_briefing": {}, "intraday_plan": {"symbol": "reliance"},
                "options_income_scan": {"symbol": "NIFTY", "expiry": "03-Nov-2026"}, "weekly_review": {},
                "research_strategy": {"idea": "buy 20-day breakouts", "symbol": "TCS"}}
        for name in self.NAMES:
            r = self.result(self.c.request("prompts/get", {"name": name, "arguments": args[name]}))
            text = r["messages"][0]["content"]["text"]
            self.assertEqual(r["messages"][0]["role"], "user")
            self.assertIn("not financial advice", text)
            self.assertIn("never promise", text.lower())
            self.assertRegex(text, r"\b(market_status|scan_daily|entry_check|options_summary|portfolio_status)\b")
        lab = self.result(self.c.request("prompts/get", {"name": "research_strategy", "arguments":
                                                         {"idea": "opening range breakouts", "symbol": "sbin"}}))
        text = lab["messages"][0]["content"]["text"]
        for tool in ("lab_list", "lab_run", "lab_report", "lab_forward"):
            self.assertIn(tool, text)
        self.assertIn("symbols SBIN", text)
        r = self.result(self.c.request("prompts/get", {"name": "intraday_plan", "arguments": {"symbol": "reliance"}}))
        self.assertIn("RELIANCE", r["messages"][0]["content"]["text"])
        self.assertIn("typed yes", r["messages"][0]["content"]["text"])

    def test_prompt_errors(self):
        self.assertEqual(self.code(self.c.request("prompts/get", {"name": "get_rich"})), -32602)
        self.assertEqual(self.code(self.c.request("prompts/get", {"name": "intraday_plan"})), -32602)
        self.assertEqual(self.code(self.c.request("prompts/get", {"name": "weekly_review",
                                                                  "arguments": {"x": "y"}})), -32602)


# ---------------------------------------------------------------- the paper confirm flow and safety

def broker_records(workdir):
    p = os.path.join(workdir, "trader", "state", "broker.json")
    with open(p, encoding="utf-8") as fh:
        return [r for r in json.load(fh).get("orders", []) if r.get("date") == "2026-10-29"]


class Confirm(ServerCase):
    def confirm(self, caps=None, **args):
        a = {"index": 1, "risk_fingerprint": FP}
        a.update(args)
        return self.result(self.c.call("paper_order_confirm", a, caps=caps))

    def test_without_elicitation_the_typed_reply_is_required(self):
        before = len(broker_records(self.c.workdir))
        r = self.confirm()
        self.assertTrue(r["isError"])
        self.assertIn("word for word", r["content"][0]["text"])
        r = self.confirm(yes_text="sure, go ahead")              # not a yes: the paper broker refuses it
        self.assertTrue(r["isError"])
        r = self.confirm(yes_text="yes 50 RELIANCE")             # the wrong quantity
        self.assertTrue(r["isError"])
        self.assertEqual(len(broker_records(self.c.workdir)), before)
        r = self.confirm(yes_text="yes 42 RELIANCE")
        self.assertFalse(r["isError"], r)
        d = r["structuredContent"]["data"]
        self.assertTrue(d["confirmed"])
        self.assertEqual(d["via"], "argument")
        self.assertTrue(d["paper_order_id"].startswith("PAPER-"))
        recs = broker_records(self.c.workdir)
        self.assertEqual(len(recs), before + 1)
        self.assertEqual(recs[-1]["yes_text"], "yes 42 RELIANCE")
        self.assertTrue(recs[-1]["paper"])
        self.assertTrue(self.confirm(yes_text="yes 42 RELIANCE")["isError"])     # one yes, one order

    def test_the_exact_fingerprint_is_required(self):
        r = self.confirm(risk_fingerprint="0123456789abcdef", yes_text="yes")
        self.assertTrue(r["isError"])
        self.assertIn("fingerprint", r["content"][0]["text"])
        r = self.confirm(risk_fingerprint="not-hex", yes_text="yes")
        self.assertTrue(r["isError"])
        r = self.confirm(index=9, yes_text="yes")
        self.assertTrue(r["isError"])

    def test_modern_elicitation_round_trip(self):
        caps = {"elicitation": {"form": {}}}
        r = self.confirm(caps=caps, yes_text="yes 42 RELIANCE")     # the argument is ignored: the user is asked
        self.assertEqual(r["resultType"], "input_required")
        ask = r["inputRequests"]["confirm"]
        self.assertEqual(ask["method"], "elicitation/create")
        self.assertIn("PAPER", ask["params"]["message"])
        self.assertIn("yes_text", ask["params"]["requestedSchema"]["properties"])
        state = r["requestState"]
        self.assertEqual(len(broker_records(self.c.workdir)), 0)
        # a tampered state is rejected
        raw, sig = state.rsplit(".", 1)
        body = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        body["bind"]["index"] = 2
        forged = base64.urlsafe_b64encode(json.dumps(body).encode("utf-8")).decode("ascii") + "." + sig
        bad = self.c.call("paper_order_confirm", {"index": 1, "risk_fingerprint": FP}, caps=caps,
                          requestState=forged,
                          inputResponses={"confirm": {"action": "accept", "content": {"yes_text": "yes"}}})
        self.assertEqual(self.code(bad), -32602)
        answer = {"confirm": {"action": "accept", "content": {"yes_text": "yes 42 RELIANCE"}}}
        ok = self.result(self.c.call("paper_order_confirm", {"index": 1, "risk_fingerprint": FP}, caps=caps,
                                     requestState=state, inputResponses=answer))
        self.assertEqual(ok["resultType"], "complete")
        self.assertFalse(ok["isError"], ok)
        self.assertEqual(ok["structuredContent"]["data"]["via"], "elicitation")
        self.assertEqual(broker_records(self.c.workdir)[-1]["yes_text"], "yes 42 RELIANCE")
        again = self.c.call("paper_order_confirm", {"index": 1, "risk_fingerprint": FP}, caps=caps,
                            requestState=state, inputResponses=answer)
        self.assertEqual(self.code(again), -32602)                  # a state is used once

    def test_modern_elicitation_declined(self):
        caps = {"elicitation": {"form": {}}}
        first = self.confirm(caps=caps)
        r = self.result(self.c.call("paper_order_confirm", {"index": 1, "risk_fingerprint": FP}, caps=caps,
                                    requestState=first["requestState"],
                                    inputResponses={"confirm": {"action": "decline"}}))
        self.assertFalse(r["isError"])
        self.assertFalse(r["structuredContent"]["data"]["confirmed"])
        self.assertEqual(len(broker_records(self.c.workdir)), 0)

    def test_legacy_elicitation_over_stdio(self):
        self.c.initialize("2025-11-25", caps={"elicitation": {}})
        self.c.send({"jsonrpc": "2.0", "id": 77, "method": "tools/call",
                     "params": {"name": "paper_order_confirm", "arguments": {"index": 1, "risk_fingerprint": FP}}})
        ask = self.c.next()
        self.assertEqual(ask["method"], "elicitation/create")
        self.assertIn("requestedSchema", ask["params"])
        self.c.send({"jsonrpc": "2.0", "id": ask["id"],
                     "result": {"action": "accept", "content": {"yes_text": "yes 42 RELIANCE"}}})
        r = self.result(self.c.wait_for(77))
        self.assertFalse(r["isError"], r)
        self.assertEqual(r["structuredContent"]["data"]["via"], "elicitation")
        self.assertEqual(len(broker_records(self.c.workdir)), 1)

    def test_no_pass_no_order(self):
        risk = os.path.join(self.c.workdir, "trader", "state", "risk.json")
        with open(risk, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["date"] = "2026-10-28"
        with open(risk, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        r = self.confirm(yes_text="yes 42 RELIANCE")
        self.assertTrue(r["isError"])
        self.assertIn("not today", r["content"][0]["text"])

    def test_a_non_paper_config_cannot_confirm(self):
        p = os.path.join(self.c.workdir, "trader", "config.json")
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg.update(broker="zerodha", paper=False)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        r = self.confirm(yes_text="yes 42 RELIANCE")
        self.assertTrue(r["isError"])
        self.assertIn("paper", r["content"][0]["text"])
        self.assertEqual(len(broker_records(self.c.workdir)), 0)


class NoLivePath(unittest.TestCase):
    def test_no_live_order_capability_exists(self):
        sys.path.insert(0, os.path.join(ROOT, "mcp"))
        names = TOOLS
        for n in names:
            self.assertNotRegex(n, r"live|real|kite|upstox|dhan|angel|groww|place_order|buy|sell")
        for f in ("server.py", "toolkit.py"):
            with open(os.path.join(ROOT, "mcp", f), encoding="utf-8") as fh:
                text = fh.read().lower()
            for bad in ("kite.zerodha", "api.upstox", "dhan.co", "smartapi", "place_order", "placeorder"):
                self.assertNotIn(bad, text, f)
        # the only order write is the paper broker's own confirm
        with open(os.path.join(ROOT, "mcp", "toolkit.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(re.findall(r'\["(confirm|fill|flatten|decline)"', text), ["confirm"])

    def test_stdout_is_only_json_rpc_even_on_noise(self):
        c = Client()
        try:
            c.call("scan_daily", {})
            c.call("entry_check", {"symbol": "NOSUCHSTOCK"})
            c.raw("")
            c.request("server/discover")
        finally:
            c.close()
        self.assertEqual(c.bad, [])
        self.assertTrue(any("bharat-trader mcp" in e for e in c.err))


class Docs(unittest.TestCase):
    def test_tool_reference_is_generated_from_the_schemas(self):
        out = subprocess.run([sys.executable, SERVER, "--tool-reference"], stdout=subprocess.PIPE, check=True)
        ref = out.stdout.decode("utf-8").replace("\r\n", "\n")
        with open(os.path.join(ROOT, "docs", "mcp.md"), encoding="utf-8") as fh:
            doc = fh.read()
        m = re.search(r"<!-- tools:start -->\n(.*?)<!-- tools:end -->", doc, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), ref, "run: python3 mcp/server.py --tool-reference, paste into docs/mcp.md")
        for t in TOOLS:
            self.assertIn("`%s`" % t, ref)

    def test_client_config_snippet(self):
        for f in (os.path.join(ROOT, "docs", "mcp.md"), os.path.join(ROOT, "mcp", "README.md")):
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('"command": "python3"', text, f)
            self.assertIn('"/path/to/bharat-trader/mcp/server.py"', text, f)
            self.assertIn('"--workdir"', text, f)


# ---------------------------------------------------------------- streamable HTTP

class Http(unittest.TestCase):
    def setUp(self):
        self.workdir = workdir_copy()
        self.p = subprocess.Popen([sys.executable, SERVER, "--offline", BUNDLE, "--workdir", self.workdir,
                                   "--http", "0"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.port = None
        end = time.time() + 30
        while time.time() < end:
            line = self.p.stderr.readline().decode("utf-8", "replace")
            m = re.search(r"http://127\.0\.0\.1:(\d+)/mcp", line)
            if m:
                self.port = int(m.group(1))
                break
            if not line:
                break
        self.assertIsNotNone(self.port, "the HTTP server did not start")

    def tearDown(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def post(self, msg, headers=None, method="POST", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if isinstance(msg, dict) and "method" in msg:
            h["Mcp-Method"] = msg["method"]
            p = msg.get("params") or {}
            if isinstance(p.get("_meta"), dict):
                h["MCP-Protocol-Version"] = p["_meta"]["io.modelcontextprotocol/protocolVersion"]
            if msg["method"] in ("tools/call", "prompts/get"):
                h["Mcp-Name"] = p.get("name")
            if msg["method"] == "resources/read":
                h["Mcp-Name"] = p.get("uri")
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        body = raw if raw is not None else (json.dumps(msg).encode("utf-8") if msg is not None else None)
        conn.request(method, "/mcp", body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data.decode("utf-8")) if data else None), resp

    def test_modern_requests(self):
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta()}})
        self.assertEqual(st, 200)
        self.assertIn(MODERN, body["result"]["supportedVersions"])
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "market_status", "arguments": {}, "_meta": meta()}})
        self.assertEqual(st, 200)
        self.assertEqual(body["result"]["structuredContent"]["disclaimer"], DISCLAIMER)
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {"_meta": meta()}})
        self.assertEqual((st, body["error"]["code"]), (404, -32601))
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                                 "params": {"_meta": dict(meta(), **{
                                     "io.modelcontextprotocol/protocolVersion": "1900-01-01"})}})
        self.assertEqual((st, body["error"]["code"]), (400, -32022))
        st, body, _ = self.post({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                 "params": {"requestId": 99}})
        self.assertEqual(st, 202)

    def test_header_must_match_the_body(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "market_status", "arguments": {}, "_meta": meta()}}
        for h in ({"MCP-Protocol-Version": "2025-11-25"}, {"Mcp-Method": "tools/list"}, {"Mcp-Name": "news"}):
            st, body, _ = self.post(msg, h)
            self.assertEqual((st, body["error"]["code"]), (400, -32020), h)
        st, body, _ = self.post(msg, {"Mcp-Name": "=?base64?%s?=" % base64.b64encode(b"market_status").decode()})
        self.assertEqual(st, 200)

    def test_origin_and_host_checks(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta()}}
        st, _, _ = self.post(msg, {"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.post(msg, {"Host": "evil.example:%d" % self.port})
        self.assertEqual(st, 403)
        st, _, _ = self.post(msg, {"Origin": "http://localhost:3000"})
        self.assertEqual(st, 200)
        st, _, _ = self.post(None, method="GET")
        self.assertEqual(st, 405)

    def test_legacy_session(self):
        st, body, resp = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(st, 200)
        sid = resp.getheader("Mcp-Session-Id")
        self.assertTrue(sid)
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        h = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18"}
        st, _, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
        self.assertEqual(st, 202)
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h)
        self.assertEqual(st, 200)
        self.assertEqual(len(body["result"]["tools"]), len(TOOLS))
        # no elicitation over plain JSON HTTP: the confirm tool falls back to the typed reply argument
        st, body, _ = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "paper_order_confirm",
                                            "arguments": {"index": 1, "risk_fingerprint": FP}}}, h)
        self.assertTrue(body["result"]["isError"])
        st, _, _ = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                             {"Mcp-Session-Id": "nope", "MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(st, 404)
        st, _, _ = self.post(None, {"Mcp-Session-Id": sid}, method="DELETE")
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
