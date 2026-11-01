"""The optional Ask panel: a relay from the local UI to YOUR OWN LLM endpoint. Standard library only.

Off by default. It turns on only when the server's environment names an endpoint and a model:

    BT_LLM_BASE_URL   the API's base URL, e.g. https://llm.example.com/v1 or http://127.0.0.1:8080/v1
    BT_LLM_MODEL      the model name your endpoint expects
    BT_LLM_API_KEY    your key (optional for a local server); read from the environment only
    BT_LLM_FORMAT     chat-completions (default) or messages: the two common HTTP chat-API shapes
    BT_LLM_EXTRA_HEADERS  optional JSON object of extra request headers (e.g. a version header)
    BT_LLM_AUTH_HEADER    optional: the header the key goes in (default: Authorization: Bearer for
                          chat-completions, x-api-key for messages)
    BT_LLM_MAX_TOKENS, BT_LLM_TIMEOUT  optional: reply length (default 1500) and seconds (default 90)

The two formats, described by shape:
  chat-completions  POST {base}/chat/completions with {"model", "messages": [{"role", "content"}],
                    "tools": [{"type": "function", "function": {...}}]}; the reply is choices[0].message
                    with optional tool_calls [{"id", "function": {"name", "arguments": "<json>"}}] and the
                    results go back as {"role": "tool", "tool_call_id", "content"} messages.
  messages          POST {base}/messages with {"model", "max_tokens", "system", "messages",
                    "tools": [{"name", "description", "input_schema"}]}; the reply's content is a list of
                    blocks, {"type": "text"} and {"type": "tool_use", "id", "name", "input"}; results go back
                    in a user message of {"type": "tool_result", "tool_use_id", "content"} blocks.

Tool calls run the SAME functions as the MCP server (mcp/toolkit.py), limited to the read-only tools plus
paper_order_draft. paper_order_confirm is never offered and refused if asked for: a paper order still needs
the user's own typed yes on the Portfolio and orders page. The key never leaves this process except in the
request to the endpoint you configured: it is not logged, not returned to the browser and scrubbed from
every error message. Each browser session must switch consent on before anything is sent.
"""

import importlib.util
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FORMATS = ("chat-completions", "messages")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DENIED = frozenset(["paper_order_confirm", "broker_status", "broker_holdings", "broker_positions"])
ALSO_ALLOWED = ("paper_order_draft",)
MAX_STEPS = 8                 # LLM round trips per question
MAX_TEXT = 4000               # characters in one question
MAX_RESULT = 12000            # characters of one tool result sent to the LLM
MAX_TURNS = 40                # messages kept per session
MAX_SESSIONS = 32
NOTICE = ("Your data goes to the LLM provider you configured: your questions, and the tool results the assistant "
          "asks for (prices, plans, your paper account, lab results), are sent to %s. Nothing is sent until you "
          "switch consent on for this browser session. The key stays on this computer.")
SYSTEM = (
    "You are the user's co-trader inside bharat-trader, a paper-trading toolkit for the Indian stock market (NSE). "
    "Use the tools for every number; never guess prices or levels. Evidence first: before any opinion, show what "
    "the tools returned. With every probability or rate give n (the past trades it rests on) and its 95% interval; "
    "quote results after charges; name the market regime it was measured in; and say what the evidence does NOT "
    "show. Say plainly when n is small, and treat no trade as a valid answer. No promises: never say guaranteed, "
    "sure-shot or risk-free. Proposing is not placing: you cannot place or confirm any order. paper_order_draft "
    "only drafts a paper ticket and runs trader-risk on it; the user confirms it themselves by typing their yes on "
    "the Portfolio and orders page. Keep answers short and in plain words, amounts in rupees. Education and "
    "tooling, not financial advice.")


class ChatError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status, self.message = status, message


# ---------------------------------------------------------------- configuration (environment only)

def config(env=None):
    """The relay's settings from the environment, or a dict with enabled False and why."""
    env = os.environ if env is None else env
    base = (env.get("BT_LLM_BASE_URL") or "").strip().rstrip("/")
    model = (env.get("BT_LLM_MODEL") or "").strip()
    fmt = (env.get("BT_LLM_FORMAT") or "chat-completions").strip().lower()
    out = {"enabled": False, "base": base, "model": model, "format": fmt, "key": env.get("BT_LLM_API_KEY") or "",
           "why_off": None, "headers": {}, "auth_header": (env.get("BT_LLM_AUTH_HEADER") or "").strip()}
    if not base or not model:
        out["why_off"] = "off: set BT_LLM_BASE_URL and BT_LLM_MODEL (and BT_LLM_API_KEY) and restart the UI"
        return out
    if fmt not in FORMATS:
        out["why_off"] = "BT_LLM_FORMAT must be %s, not %r" % (" or ".join(FORMATS), fmt[:40])
        return out
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ("http", "https") or not u.hostname:
        out["why_off"] = "BT_LLM_BASE_URL must be an http(s) URL"
        return out
    if u.scheme == "http" and u.hostname not in LOOPBACK_HOSTS:
        out["why_off"] = "BT_LLM_BASE_URL must use https unless it is on this computer (127.0.0.1)"
        return out
    if u.username or u.password:
        out["why_off"] = "put the key in BT_LLM_API_KEY, not in the URL"
        return out
    raw = env.get("BT_LLM_EXTRA_HEADERS")
    if raw:
        try:
            h = json.loads(raw)
        except ValueError:
            h = None
        if not isinstance(h, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in h.items()):
            out["why_off"] = "BT_LLM_EXTRA_HEADERS must be a JSON object of header: text"
            return out
        if any(not re.match(r"^[A-Za-z0-9-]{1,64}$", k) or "\n" in v or "\r" in v for k, v in h.items()):
            out["why_off"] = "BT_LLM_EXTRA_HEADERS has a header name or value that is not allowed"
            return out
        out["headers"] = h
    if out["auth_header"] and not re.match(r"^[A-Za-z0-9-]{1,64}$", out["auth_header"]):
        out["why_off"] = "BT_LLM_AUTH_HEADER is a header name such as api-key"
        return out
    try:
        out["max_tokens"] = max(64, min(8000, int(env.get("BT_LLM_MAX_TOKENS") or 1500)))
        out["timeout"] = max(5, min(600, float(env.get("BT_LLM_TIMEOUT") or 90)))
    except ValueError:
        out["why_off"] = "BT_LLM_MAX_TOKENS and BT_LLM_TIMEOUT are numbers"
        return out
    out["enabled"] = True
    return out


def public(cfg):
    """What the browser may see: never the key, never the extra header values."""
    host = urllib.parse.urlsplit(cfg["base"]).hostname if cfg.get("base") else None
    return {"enabled": cfg["enabled"], "format": cfg["format"] if cfg["enabled"] else None,
            "model": cfg["model"] if cfg["enabled"] else None, "endpoint_host": host if cfg["enabled"] else None,
            "key_set": bool(cfg.get("key")), "why_off": cfg.get("why_off"),
            "notice": NOTICE % (host or "the endpoint you configure")}


def scrub(text, cfg):
    """Remove the key (and the extra header values) from any text that may reach a log or the browser."""
    text = str(text)
    secrets = [cfg.get("key") or ""] + list((cfg.get("headers") or {}).values())
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[redacted]")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1[redacted]", text)


# ---------------------------------------------------------------- the two wire formats

def _headers(cfg):
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "bharat-trader-ui"}
    h.update(cfg.get("headers") or {})
    if cfg.get("key"):
        name = cfg.get("auth_header") or ("Authorization" if cfg["format"] == "chat-completions" else "x-api-key")
        h[name] = ("Bearer " + cfg["key"]) if name.lower() == "authorization" else cfg["key"]
    return h


def request_body(cfg, history, tools):
    """(url, body) for one round trip, from the provider-neutral history:
    {"role": "user", "text"} {"role": "assistant", "text", "calls": [{"id", "name", "args"}]}
    {"role": "tool", "id", "name", "content"}"""
    if cfg["format"] == "chat-completions":
        msgs = [{"role": "system", "content": SYSTEM}]
        for m in history:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                one = {"role": "assistant", "content": m.get("text") or None}
                if m.get("calls"):
                    one["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["calls"]]
                msgs.append(one)
            else:
                msgs.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
        body = {"model": cfg["model"], "messages": msgs, "max_tokens": cfg["max_tokens"],
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["inputSchema"]}} for t in tools]}
        return cfg["base"] + "/chat/completions", body
    msgs = []
    for m in history:
        if m["role"] == "user":
            msgs.append({"role": "user", "content": [{"type": "text", "text": m["text"]}]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["text"]}] if m.get("text") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                       for c in m.get("calls") or []]
            msgs.append({"role": "assistant", "content": blocks})
        else:
            block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]}
            if msgs and msgs[-1]["role"] == "user" and msgs[-1]["content"] and \
                    msgs[-1]["content"][0].get("type") == "tool_result":
                msgs[-1]["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
    body = {"model": cfg["model"], "max_tokens": cfg["max_tokens"], "system": SYSTEM, "messages": msgs,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
                      for t in tools]}
    return cfg["base"] + "/messages", body


def parse_reply(cfg, doc):
    """(text, calls) from the endpoint's answer."""
    try:
        if cfg["format"] == "chat-completions":
            msg = doc["choices"][0]["message"]
            text = msg.get("content") or ""
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    args = {"_unparsed": str(raw)[:200]}
                calls.append({"id": str(tc.get("id") or "call-%d" % len(calls)), "name": str(fn.get("name")),
                              "args": args if isinstance(args, dict) else {}})
            return str(text), calls
        text, calls = [], []
        for b in doc["content"]:
            if b.get("type") == "text":
                text.append(b.get("text") or "")
            elif b.get("type") == "tool_use":
                args = b.get("input")
                calls.append({"id": str(b.get("id") or "call-%d" % len(calls)), "name": str(b.get("name")),
                              "args": args if isinstance(args, dict) else {}})
        return "\n".join(t for t in text if t), calls
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ChatError(502, "the LLM endpoint answered in a shape that is not %s" % cfg["format"])


def post_json(cfg, url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=_headers(cfg), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            raw = r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(600).decode("utf-8", "replace")
        except Exception:
            detail = ""
        finally:
            e.close()
        raise ChatError(502, scrub("the LLM endpoint said %d %s %s" % (e.code, e.reason, detail[:300]), cfg))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ChatError(502, scrub("cannot reach the LLM endpoint: %s" % (getattr(e, "reason", None) or e), cfg))
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ChatError(502, "the LLM endpoint did not answer with JSON")


# ---------------------------------------------------------------- the relay

def _toolkit():
    spec = importlib.util.spec_from_file_location("bt_ui_chat_toolkit", os.path.join(ROOT, "mcp", "toolkit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def allowed_tools(kit):
    """The tool table the chat may use: read-only tools plus the paper draft, never the confirm."""
    out = []
    for t in kit.tools:
        if t["name"] in DENIED:
            continue
        if t["annotations"].get("readOnlyHint") or t["name"] in ALSO_ALLOWED:
            out.append({"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]})
    return out


class Relay(object):
    def __init__(self, workdir, offline=None, now=None, env=None, post=None):
        self.workdir, self.offline, self.now = workdir, offline, now
        self.env = env
        self.post = post or post_json
        self.sessions = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self._kit = None
        self._tk = None

    def cfg(self):
        return config(self.env)

    def kit(self):
        if self._kit is None:
            self._tk = _toolkit()
            self._kit = self._tk.Kit(self.workdir, offline=self.offline, now=self.now)
        return self._kit

    def _session(self, sid, create=True):
        with self.lock:
            s = self.sessions.get(sid)
            if s is None and create:
                if len(self.sessions) >= MAX_SESSIONS:
                    self.sessions.pop(next(iter(self.sessions)))
                s = self.sessions[sid] = {"consent": False, "history": []}
            return s

    def status(self, sid):
        cfg = self.cfg()
        out = public(cfg)
        s = self._session(sid, create=False) if sid else None
        out["consent"] = bool(s and s["consent"])
        out["turns"] = len(s["history"]) if s else 0
        if cfg["enabled"]:
            out["tools"] = [t["name"] for t in allowed_tools(self.kit())]
        return out

    def set_consent(self, sid, on):
        if not sid:
            raise ChatError(403, "no session: reload the page")
        s = self._session(sid)
        s["consent"] = bool(on)
        if not on:
            s["history"] = []
        return self.status(sid)

    def reset(self, sid):
        s = self._session(sid, create=False) if sid else None
        if s:
            s["history"] = []
        return self.status(sid)

    def run_tool(self, name, args):
        """(content text for the LLM, step summary for the browser)."""
        kit = self.kit()
        names = set(t["name"] for t in allowed_tools(kit))
        if name not in names:
            why = ("not available here: placing or confirming an order needs the user's own typed yes on the "
                   "Portfolio and orders page" if name == "paper_order_confirm" or "order" in name else
                   "not available in the Ask panel")
            return json.dumps({"error": "%s is %s" % (name, why)}), {"tool": name, "ok": False, "refused": True,
                                                                      "summary": why}
        try:
            env = kit.call(name, args, self._tk.Call())
        except self._tk.NeedInput:
            return json.dumps({"error": "this tool needs the user's input"}), {"tool": name, "ok": False,
                                                                              "summary": "needs your input"}
        except self._tk.ToolError as e:
            return json.dumps({"error": str(e)}), {"tool": name, "ok": False, "summary": str(e)[:200]}
        except Exception as e:                      # a bug in a tool: say so, keep the chat alive
            return json.dumps({"error": "internal error: %s" % e.__class__.__name__}), \
                {"tool": name, "ok": False, "summary": "internal error"}
        text = json.dumps(env, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + " ...(cut: ask for a narrower question)"
        return text, {"tool": name, "ok": True, "summary": (env.get("notes") or [""])[0][:200]}

    def ask(self, sid, text):
        cfg = self.cfg()
        if not cfg["enabled"]:
            raise ChatError(409, "the Ask panel is %s" % cfg["why_off"])
        if not sid:
            raise ChatError(403, "no session: reload the page")
        s = self._session(sid)
        if not s["consent"]:
            raise ChatError(403, "switch consent on first: your questions and tool results go to the LLM provider "
                                 "you configured")
        text = str(text or "").strip()
        if not text or len(text) > MAX_TEXT:
            raise ChatError(400, "a question is 1 to %d characters" % MAX_TEXT)
        tools = allowed_tools(self.kit())
        with self.run_lock:
            history = list(s["history"])
            history.append({"role": "user", "text": text})
            steps, reply, drafted = [], "", False
            for _ in range(MAX_STEPS):
                url, body = request_body(cfg, history, tools)
                doc = self.post(cfg, url, body)
                reply, calls = parse_reply(cfg, doc)
                history.append({"role": "assistant", "text": reply, "calls": calls})
                if not calls:
                    break
                for c in calls:
                    content, step = self.run_tool(c["name"], c["args"])
                    step["args"] = c["args"]
                    steps.append(step)
                    if c["name"] == "paper_order_draft" and step["ok"]:
                        drafted = True
                    history.append({"role": "tool", "id": c["id"], "name": c["name"], "content": content})
            else:
                reply = (reply + "\n\n" if reply else "") + "(stopped after %d tool rounds)" % MAX_STEPS
            s["history"] = history[-MAX_TURNS:]
            while s["history"] and s["history"][0]["role"] != "user":
                s["history"].pop(0)
        out = {"reply": scrub(reply, cfg), "steps": steps, "drafted": drafted}
        if drafted:
            out["next_step"] = ("A paper ticket was drafted and risk-checked. Nothing was placed: review it on "
                                "Portfolio and orders and type your own yes there if you want it.")
        return out
