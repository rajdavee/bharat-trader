"""ui/chat.py, the optional Ask panel: a relay to the user's own LLM endpoint, tested offline against a stub LLM
HTTP server that speaks both supported formats (chat-completions and messages) and makes multi-step tool calls.
The tools run on the MCP server's offline bundle (tests/fixtures/mcp), as in tests/test_mcp.py."""

import json
import os
import shutil
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX  # noqa: E402
import test_ui  # noqa: E402

api = test_ui.api
chat = api.chat_module()
BUNDLE = os.path.join(FIX, "mcp")
KEY = "-".join(["test", "key", "never", "logged", "4f9a"])      # built at run time: no secret-looking literal
VERSION_HEADER = ("X-Api-Version", "2099-01-01")
FP = "9ba8b90f83f96546"        # tests/fixtures/ui/trader/orders.json ticket 1, PASS in state/risk.json


class StubLLM(object):
    """A local LLM endpoint. script(fmt, body, n) -> the JSON answer; every request is recorded."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                fmt = "messages" if self.path.endswith("/messages") else "chat-completions"
                stub.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body})
                status, doc = stub.script(fmt, body, len(stub.requests))
                raw = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---- what a real model would send back, in each format

def cc_calls(*calls):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_%d" % i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(calls)]}}]}


def cc_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def msg_calls(*calls, **kw):
    blocks = [{"type": "text", "text": kw["text"]}] if kw.get("text") else []
    return {"role": "assistant", "stop_reason": "tool_use", "content": blocks + [
        {"type": "tool_use", "id": "tu_%d" % i, "name": n, "input": a} for i, (n, a) in enumerate(calls)]}


def msg_text(text):
    return {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


def tool_results(fmt, body):
    """The tool results the relay sent back, in order: [(id, content dict)]."""
    out = []
    for m in body["messages"]:
        if fmt == "chat-completions" and m["role"] == "tool":
            out.append((m["tool_call_id"], json.loads(m["content"])))
        if fmt == "messages" and m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    out.append((b["tool_use_id"], json.loads(b["content"])))
    return out


def research_script(fmt, body, n):
    """regime_now, then best_for_regime and strategy_design together, then an answer quoting the tools."""
    calls, text = (cc_calls, cc_text) if fmt == "chat-completions" else (msg_calls, msg_text)
    done = tool_results(fmt, body)
    if not done:
        return 200, calls(("regime_now", {}))
    if len(done) == 1:
        return 200, calls(("best_for_regime", {}), ("strategy_design", {"template": "orb15-vwap",
                                                                         "window_from": "09:15", "window_to": "10:00"}))
    regime = done[0][1]["data"]["regimes"][0]["words"]
    best = done[1][1]["data"]["headline"]
    design = done[2][1]["data"]
    return 200, text("Regime: %s. %s Draft %s is valid: %s, %d grid points, untested. No promises." % (
        regime, best, design["spec"]["name"], design["valid"], design["grid_points"]))


def env_for(stub, fmt, **extra):
    env = {"BT_LLM_BASE_URL": stub.url, "BT_LLM_MODEL": "stub-model", "BT_LLM_API_KEY": KEY, "BT_LLM_FORMAT": fmt}
    env.update(extra)
    return env


class Base(unittest.TestCase):
    SCRIPT = staticmethod(research_script)

    def setUp(self):
        self.stub = StubLLM(self.SCRIPT)
        self.dir = test_ui.workdir()

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def relay(self, fmt="chat-completions", **extra):
        return chat.Relay(self.dir, offline=BUNDLE, env=env_for(self.stub, fmt, **extra))


class Config(unittest.TestCase):
    def test_off_by_default_and_checked(self):
        self.assertFalse(chat.config({})["enabled"])
        self.assertIn("BT_LLM_BASE_URL", chat.config({})["why_off"])
        base = {"BT_LLM_BASE_URL": "https://llm.example.com/v1", "BT_LLM_MODEL": "m"}
        self.assertTrue(chat.config(base)["enabled"])
        self.assertEqual(chat.config(base)["format"], "chat-completions")
        for bad, why in ((dict(base, BT_LLM_FORMAT="telepathy"), "BT_LLM_FORMAT"),
                         (dict(base, BT_LLM_BASE_URL="http://llm.example.com/v1"), "https"),
                         (dict(base, BT_LLM_BASE_URL="ftp://x/v1"), "http(s)"),
                         (dict(base, BT_LLM_BASE_URL="https://user:pw@llm.example.com/v1"), "not in the URL"),
                         (dict(base, BT_LLM_EXTRA_HEADERS="[1]"), "JSON object"),
                         (dict(base, BT_LLM_EXTRA_HEADERS='{"X-A": "a\\r\\nX-B: b"}'), "not allowed"),
                         (dict(base, BT_LLM_AUTH_HEADER="Bad Header"), "header name"),
                         (dict(base, BT_LLM_TIMEOUT="soon"), "numbers")):
            c = chat.config(bad)
            self.assertFalse(c["enabled"], bad)
            self.assertIn(why, c["why_off"], bad)
        self.assertTrue(chat.config(dict(base, BT_LLM_BASE_URL="http://127.0.0.1:9999/v1"))["enabled"])

    def test_public_status_never_has_the_key(self):
        c = chat.config({"BT_LLM_BASE_URL": "https://llm.example.com/v1", "BT_LLM_MODEL": "m", "BT_LLM_API_KEY": KEY,
                         "BT_LLM_EXTRA_HEADERS": json.dumps({"X-Secret-Ish": KEY + "x"})})
        p = chat.public(c)
        self.assertTrue(p["key_set"])
        self.assertEqual(p["endpoint_host"], "llm.example.com")
        self.assertNotIn(KEY, json.dumps(p))
        self.assertIn("llm.example.com", p["notice"])
        self.assertIn("Your data goes to the LLM provider you configured", p["notice"])

    def test_scrub(self):
        c = {"key": KEY, "headers": {"X-Other": "another-value-123"}}
        text = chat.scrub("401: bad key %s; header another-value-123; Authorization: Bearer abcdefghijkl123" % KEY, c)
        self.assertNotIn(KEY, text)
        self.assertNotIn("another-value-123", text)
        self.assertNotIn("abcdefghijkl123", text)
        self.assertIn("[redacted]", text)


class ToolTable(unittest.TestCase):
    def test_read_only_tools_and_the_paper_draft_only(self):
        d = test_ui.workdir()
        try:
            r = chat.Relay(d, offline=BUNDLE, env={})
            names = [t["name"] for t in chat.allowed_tools(r.kit())]
            kit = r.kit()
            by = {t["name"]: t for t in kit.tools}
            self.assertIn("paper_order_draft", names)
            for n in ("paper_order_confirm", "broker_holdings", "broker_positions", "broker_status", "lab_run",
                      "lab_run_spec", "lab_forward", "discover_strategies", "cotrader_candidates", "entry_check"):
                self.assertNotIn(n, names, n)
            for n in names:
                self.assertTrue(by[n]["annotations"]["readOnlyHint"] or n == "paper_order_draft", n)
            for n in ("trade_brief", "best_for_regime", "strategy_design", "compare_strategies", "regime_now",
                      "calibration_report", "archive_status"):
                self.assertIn(n, names)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ChatCompletions(Base):
    FMT = "chat-completions"

    def check_round_trips(self, fmt):
        r = self.relay(fmt, BT_LLM_EXTRA_HEADERS=json.dumps(dict([VERSION_HEADER])))
        lab_before = sorted(os.listdir(os.path.join(self.dir, "trader", "lab")))
        with self.assertRaises(chat.ChatError) as cm:
            r.ask("s1", "what now?")
        self.assertEqual(cm.exception.status, 403)               # consent first
        self.assertEqual(self.stub.requests, [])                 # nothing was sent
        r.set_consent("s1", True)
        out = r.ask("s1", "Which strategies fit today's regime? Draft an open-drive ORB.")
        self.assertEqual([s["tool"] for s in out["steps"]], ["regime_now", "best_for_regime", "strategy_design"])
        self.assertTrue(all(s["ok"] for s in out["steps"]), out["steps"])
        self.assertIn("Regime: NIFTY", out["reply"])
        self.assertIn("Nothing validated shows an edge", out["reply"])     # the demo's lab runs are REJECTs
        self.assertIn("is valid: True, 6 grid points", out["reply"])
        self.assertFalse(out["drafted"])
        self.assertEqual(len(self.stub.requests), 3)
        for q in self.stub.requests:
            self.assertEqual(q["headers"].get(VERSION_HEADER[0].lower()), VERSION_HEADER[1])
            names = [t["function"]["name"] if fmt == "chat-completions" else t["name"] for t in q["body"]["tools"]]
            self.assertNotIn("paper_order_confirm", names)
            self.assertIn("trade_brief", names)
            self.assertEqual(q["body"]["model"], "stub-model")
        # the strategy draft ran nothing
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "trader", "lab"))), lab_before)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "trader", "state", "lab-trials.jsonl")))
        # the conversation goes on: the history is sent again with the next question
        r.ask("s1", "and again?")
        last = self.stub.requests[-1]["body"]["messages"]
        self.assertGreaterEqual(len(last), 6)
        # another session has its own consent and history
        with self.assertRaises(chat.ChatError):
            r.ask("s2", "hello")
        self.assertEqual(r.status("s1")["consent"], True)
        self.assertEqual(r.status("s2")["consent"], False)
        r.set_consent("s1", False)
        self.assertEqual(r.status("s1")["turns"], 0)
        return self.stub.requests

    def test_multi_step_tool_calls(self):
        reqs = self.check_round_trips("chat-completions")
        q = reqs[0]
        self.assertEqual(q["path"], "/v1/chat/completions")
        self.assertEqual(q["headers"]["authorization"], "Bearer " + KEY)
        self.assertEqual(q["body"]["messages"][0]["role"], "system")
        self.assertIn("Evidence first", q["body"]["messages"][0]["content"])
        self.assertIn("cannot place or confirm any order", q["body"]["messages"][0]["content"])
        t = q["body"]["tools"][0]
        self.assertEqual(t["type"], "function")
        self.assertEqual(t["function"]["parameters"]["type"], "object")
        second = reqs[1]["body"]["messages"]
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "regime_now")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call_0")


class Messages(Base):
    def test_multi_step_tool_calls(self):
        reqs = ChatCompletions.check_round_trips(self, "messages")
        q = reqs[0]
        self.assertEqual(q["path"], "/v1/messages")
        self.assertEqual(q["headers"]["x-api-key"], KEY)
        self.assertNotIn("authorization", q["headers"])
        self.assertIn("Evidence first", q["body"]["system"])
        self.assertIn("input_schema", q["body"]["tools"][0])
        self.assertTrue(q["body"]["max_tokens"] >= 64)
        third = reqs[2]["body"]["messages"]
        # two tool calls in one turn: both results come back in ONE user message, in order
        self.assertEqual([b["type"] for b in third[-2]["content"]], ["tool_use", "tool_use"])
        self.assertEqual([b["tool_use_id"] for b in third[-1]["content"]], ["tu_0", "tu_1"])

    def test_custom_auth_header(self):
        r = self.relay("messages", BT_LLM_AUTH_HEADER="api-key")
        r.set_consent("s", True)
        r.ask("s", "brief me")
        self.assertEqual(self.stub.requests[0]["headers"]["api-key"], KEY)
        self.assertNotIn("x-api-key", self.stub.requests[0]["headers"])


def order_script(fmt, body, n):
    calls, text = (cc_calls, cc_text) if fmt == "chat-completions" else (msg_calls, msg_text)
    if not tool_results(fmt, body):
        return 200, calls(("paper_order_confirm", {"index": 1, "risk_fingerprint": FP, "yes_text": "yes 42 RELIANCE"}),
                          ("place_live_order", {"symbol": "RELIANCE", "qty": 42}),
                          ("paper_order_draft", {}))
    return 200, text("I drafted a paper ticket; you confirm it yourself.")


class NoOrders(Base):
    SCRIPT = staticmethod(order_script)

    def broker_bytes(self):
        p = os.path.join(self.dir, "trader", "state", "broker.json")
        with open(p, "rb") as fh:
            return fh.read()

    def test_no_order_can_be_placed_from_chat(self):
        for fmt in ("chat-completions", "messages"):
            before = self.broker_bytes()
            r = self.relay(fmt)
            r.set_consent("s", True)
            out = r.ask("s", "just buy it for me")
            steps = {s["tool"]: s for s in out["steps"]}
            self.assertTrue(steps["paper_order_confirm"]["refused"])
            self.assertIn("typed yes", steps["paper_order_confirm"]["summary"])
            self.assertTrue(steps["place_live_order"]["refused"])
            self.assertTrue(steps["paper_order_draft"]["ok"], steps["paper_order_draft"])
            self.assertTrue(out["drafted"])
            self.assertIn("type your own yes", out["next_step"])
            self.assertEqual(self.broker_bytes(), before, fmt)          # no yes recorded, nothing placed
            sent = tool_results(fmt, self.stub.requests[-1]["body"])
            self.assertIn("not available here", sent[0][1]["error"])
            self.assertTrue(os.path.exists(os.path.join(self.dir, "trader", "orders.json.bak")))


def leaky_script(fmt, body, n):
    auth = KEY
    return 401, {"error": {"message": "invalid key %s for this model" % auth}}


class KeyScrubbing(Base):
    SCRIPT = staticmethod(leaky_script)

    def test_errors_never_carry_the_key(self):
        r = self.relay("chat-completions")
        r.set_consent("s", True)
        with self.assertRaises(chat.ChatError) as cm:
            r.ask("s", "hi")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("401", cm.exception.message)
        self.assertNotIn(KEY, cm.exception.message)
        self.assertIn("[redacted]", cm.exception.message)
        self.assertNotIn(KEY, json.dumps(r.status("s")))

    def test_unreachable_endpoint(self):
        env = env_for(self.stub, "messages")
        env["BT_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"           # discard port: nothing listens
        env["BT_LLM_TIMEOUT"] = "5"
        r = chat.Relay(self.dir, offline=BUNDLE, env=env)
        r.set_consent("s", True)
        with self.assertRaises(chat.ChatError) as cm:
            r.ask("s", "hi")
        self.assertIn("cannot reach", cm.exception.message)
        self.assertNotIn(KEY, cm.exception.message)


def loop_script(fmt, body, n):
    calls = cc_calls if fmt == "chat-completions" else msg_calls
    return 200, calls(("market_status", {}))


class StepLimit(Base):
    SCRIPT = staticmethod(loop_script)

    def test_a_model_that_never_stops_is_stopped(self):
        r = self.relay("chat-completions")
        r.set_consent("s", True)
        out = r.ask("s", "loop")
        self.assertEqual(len(self.stub.requests), chat.MAX_STEPS)
        self.assertIn("stopped after", out["reply"])


class OverHttp(Base):
    """Through the real UI server: env-only config, CSRF, the session's consent, loopback only, no key leaves."""

    def test_routes(self):
        s = test_ui.Running()
        try:
            s.srv.ctx.ask_relay = chat.Relay(s.dir, offline=BUNDLE, env=env_for(self.stub, "chat-completions"))
            r, raw = s.request("GET", "/api/ask", headers={"Cookie": s.cookie})
            st = json.loads(raw.decode("utf-8"))
            self.assertTrue(st["enabled"])
            self.assertFalse(st["consent"])
            self.assertNotIn(KEY, raw.decode("utf-8"))
            self.assertIn("trade_brief", st["tools"])
            self.assertEqual(s.post("/api/ask", {"message": "hi"})[0], 403)          # no consent yet
            self.assertEqual(s.post("/api/ask/consent", {"consent": True}, token=False)[0], 403)   # CSRF
            self.assertEqual(s.post("/api/ask/consent", {"consent": "yes"})[0], 400)
            status, st = s.post("/api/ask/consent", {"consent": True})
            self.assertEqual((status, st["consent"]), (200, True))
            status, out = s.post("/api/ask", {"message": "what fits today?"})
            self.assertEqual(status, 200, out)
            self.assertEqual([x["tool"] for x in out["steps"]], ["regime_now", "best_for_regime", "strategy_design"])
            self.assertNotIn(KEY, json.dumps(out))
            self.assertEqual(s.post("/api/ask", {"message": ""})[0], 400)
            self.assertEqual(s.post("/api/ask/reset", {})[1]["turns"], 0)
            # only 127.0.0.1 may use it, whatever the bind address
            ctx = s.srv.ctx
            self.assertEqual(api.dispatch(ctx, "GET", "/api/ask", {}, None, {"peer": "192.168.1.9", "session": "x"})[0],
                             403)
            self.assertEqual(api.dispatch(ctx, "POST", "/api/ask", {}, {"message": "hi"},
                                          {"peer": "10.0.0.2", "session": "x"})[0], 403)
            self.assertEqual(api.dispatch(ctx, "POST", "/api/ask/consent", {}, {"consent": True},
                                          {"peer": "127.0.0.1", "session": None})[0], 403)
        finally:
            s.close()

    def test_off_when_not_configured(self):
        s = test_ui.Running()
        try:
            s.srv.ctx.ask_relay = chat.Relay(s.dir, offline=BUNDLE, env={})
            status, st = s.get("/api/ask")
            self.assertEqual(status, 200)
            self.assertFalse(st["enabled"])
            s.post("/api/ask/consent", {"consent": True})
            status, out = s.post("/api/ask", {"message": "hi"})
            self.assertEqual(status, 409)
            self.assertIn("off", out["error"])
            self.assertEqual(self.stub.requests, [])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
