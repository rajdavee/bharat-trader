#!/usr/bin/env python3
"""bharat-trader MCP server: connect your own MCP-compatible AI assistant. Standard library only.

    python3 mcp/server.py --workdir ~/trading                  # stdio (what MCP clients launch)
    python3 mcp/server.py --workdir ~/trading --http 8765      # streamable HTTP on 127.0.0.1
    python3 mcp/server.py --offline tests/fixtures/mcp         # saved real data, no network

It speaks MCP revision 2026-07-28 (stateless: every request carries its protocol version and
client capabilities in _meta; server/discover; multi round-trip requests for elicitation) and,
for clients that still open with an initialize handshake, revisions 2025-11-25, 2025-06-18 and
2025-03-26. JSON-RPC 2.0, one message per line on stdin/stdout; logs go to stderr only.

Safety: there is no live-order tool. The only order write is the paper broker's own confirm,
which needs trader-risk PASS for that exact order (its fingerprint) and the user's own typed yes,
asked through elicitation when the client supports it. Tool calls are rate limited. Every
write lands in <workdir>/trader/. Every result says: education and tooling, not financial advice.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import toolkit  # noqa: E402

NAME = "bharat-trader"
MODERN = ["2026-07-28"]
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26"]
SUPPORTED = MODERN + LEGACY
K_VERSION = "io.modelcontextprotocol/protocolVersion"
K_CAPS = "io.modelcontextprotocol/clientCapabilities"
K_CLIENT = "io.modelcontextprotocol/clientInfo"
K_LOG = "io.modelcontextprotocol/logLevel"
K_SERVER = "io.modelcontextprotocol/serverInfo"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, MISSING_CAPABILITY, UNSUPPORTED_VERSION = -32020, -32021, -32022
LEGACY_RESOURCE_NOT_FOUND = -32002
LEVELS = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]
MAX_LINE = 4 * 1024 * 1024
STATE_TTL = 900                # seconds a confirmation question stays answerable
LIST_TTL_MS = 60000
INSTRUCTIONS = (
    "bharat-trader: tools for the Indian stock market (NSE) built on its skills: market status, scans, news, "
    "entry and exit plans, a time-machine replay, sizing, risk checks, a paper broker, option chains, results "
    "and insider deals. Use the tools for every number and show their source and time. Paper trading only: "
    "there is no live-order tool. To place a paper order: paper_order_draft, show the ticket in plain words, "
    "then paper_order_confirm with the ticket's index and fingerprint; the user must type the yes themselves. "
    "Present evidence and risks, never promises. " + toolkit.DISCLAIMER)


def log(msg):
    sys.stderr.write("bharat-trader mcp: %s\n" % msg)
    sys.stderr.flush()


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code, self.message, self.data = code, message, data


class Session(object):
    """Legacy era only: what an initialize handshake set up for this stdio process or HTTP session."""

    def __init__(self, sid=None):
        self.sid = sid
        self.version = None
        self.caps = {}
        self.client = {}
        self.initialized = False
        self.log_level = None


class Req(object):
    def __init__(self, rid, method, params, era, version, caps, log_level, session, out):
        self.id, self.method, self.params = rid, method, params
        self.era, self.version, self.caps = era, version, caps
        self.log_level, self.session, self.out = log_level, session, out
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        self.progress = meta.get("progressToken")
        self.cancel = threading.Event()

    def notify(self, method, params):
        if not self.cancel.is_set() and self.out is not None:
            self.out.notify({"jsonrpc": "2.0", "method": method, "params": params})

    def log(self, level, data):
        if self.log_level and LEVELS.index(level) >= LEVELS.index(self.log_level):
            self.notify("notifications/message", {"level": level, "logger": NAME, "data": data})

    def progress_to(self, n, total, message):
        if self.progress is not None and isinstance(self.progress, (str, int)) and not isinstance(self.progress, bool):
            self.notify("notifications/progress", {"progressToken": self.progress, "progress": n, "total": total,
                                                   "message": message})


# ---------------------------------------------------------------- the protocol

class Server(object):
    def __init__(self, kit, page_size=100, elicit_timeout=600):
        self.kit = kit
        self.page_size = max(1, int(page_size))
        self.elicit_timeout = elicit_timeout
        self.key = secrets.token_bytes(32)            # signs requestState; a restart forgets open questions
        self.used = {}
        self.used_lock = threading.Lock()
        self.inflight = {}
        self.early_cancel = {}                         # cancelled before its worker started
        self.inflight_lock = threading.Lock()
        self.info = {"name": NAME, "title": "bharat-trader (NSE tools, paper trading)", "version": toolkit.VERSION}

    # -- capabilities
    def capabilities(self, era):
        caps = {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False}}
        if era == "legacy":
            caps["logging"] = {}       # deprecated in 2026-07-28: the per-request logLevel is used there
        return caps

    # -- entry point: one parsed message -> a response dict, or None
    def handle(self, msg, session, out=None, transport="stdio"):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            rid = msg.get("id") if isinstance(msg, dict) and _good_id(msg.get("id")) else None
            return _error(rid, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in msg:
            if "id" in msg and ("result" in msg or "error" in msg):
                if out is not None and hasattr(out, "resolve"):
                    out.resolve(msg)
                return None
            return _error(msg.get("id") if _good_id(msg.get("id")) else None, INVALID_REQUEST, "no method")
        method = msg.get("method")
        if not isinstance(method, str):
            return _error(msg.get("id") if _good_id(msg.get("id")) else None, INVALID_REQUEST, "method must be text")
        if "id" not in msg:
            self.on_notification(method, msg.get("params"), session)
            return None
        rid = msg["id"]
        if not _good_id(rid):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer, not null")
        try:
            req = self.make_req(rid, method, msg.get("params"), session, out)
            key = (id(session), rid)
            with self.inflight_lock:
                self.inflight[key] = req
                if self.early_cancel.pop(key, None) and method != "initialize":
                    req.cancel.set()
            try:
                result = None if req.cancel.is_set() else self.dispatch(req, transport)
            finally:
                with self.inflight_lock:
                    self.inflight.pop(key, None)
            if req.cancel.is_set():
                log("dropped the answer to cancelled request %r" % (rid,))
                return None                            # cancelled: no further messages for it
            return {"jsonrpc": "2.0", "id": rid, "result": self.finish(req, result)}
        except RpcError as e:
            return _error(rid, e.code, e.message, e.data)
        except Exception as e:                          # a bug: say so, keep serving
            log("internal error in %s: %r" % (method, e))
            return _error(rid, INTERNAL_ERROR, "internal error: %s" % e.__class__.__name__)

    def on_notification(self, method, params, session):
        params = params if isinstance(params, dict) else {}
        if method == "notifications/initialized":
            session.initialized = True
        elif method == "notifications/cancelled":
            rid = params.get("requestId")
            if not _good_id(rid):
                return
            with self.inflight_lock:
                req = self.inflight.get((id(session), rid))
                if req is None:
                    if len(self.early_cancel) > 256:
                        self.early_cancel.clear()
                    self.early_cancel[(id(session), rid)] = True
            if req is not None and req.method != "initialize":
                req.cancel.set()
            log("cancel request %r: %s" % (rid, params.get("reason") or "no reason given"))
        # anything else (progress, roots/list_changed, unknown) is ignored, as notifications must be

    def make_req(self, rid, method, params, session, out):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        meta = params.get("_meta")
        if meta is not None and not isinstance(meta, dict):
            raise RpcError(INVALID_PARAMS, "_meta must be an object")
        meta = meta or {}
        if method == "initialize":
            return Req(rid, method, params, "legacy", None, {}, None, session, out)
        if K_VERSION in meta:
            version = meta[K_VERSION]
            if not isinstance(version, str) or version not in MODERN:
                if isinstance(version, str) and version in LEGACY and session.version == version:
                    return Req(rid, method, params, "legacy", version, session.caps, session.log_level, session, out)
                raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version",
                               {"supported": SUPPORTED, "requested": version})
            caps = meta.get(K_CAPS)
            if not isinstance(caps, dict):
                raise RpcError(INVALID_PARAMS, "_meta needs %s (an object)" % K_CAPS)
            level = meta.get(K_LOG)
            if level is not None and level not in LEVELS:
                raise RpcError(INVALID_PARAMS, "unknown log level %r (one of %s)" % (level, ", ".join(LEVELS)))
            return Req(rid, method, params, "modern", version, caps, level, session, out)
        if session.version:
            return Req(rid, method, params, "legacy", session.version, session.caps, session.log_level, session, out)
        if method == "ping":
            return Req(rid, method, params, "legacy", None, {}, None, session, out)
        raise RpcError(INVALID_PARAMS, "missing _meta[\"%s\"]: send it on every request (revision %s), or open "
                                       "with initialize (revisions %s)" % (K_VERSION, MODERN[0], ", ".join(LEGACY)))

    def dispatch(self, req, transport):
        m = req.method
        common = {"tools/list": self.tools_list, "tools/call": self.tools_call,
                  "resources/list": self.resources_list, "resources/templates/list": self.templates_list,
                  "resources/read": self.resources_read, "prompts/list": self.prompts_list,
                  "prompts/get": self.prompts_get}
        if req.era == "modern":
            if m == "server/discover":
                return self.discover(req)
            if m in common:
                return common[m](req)
            raise RpcError(METHOD_NOT_FOUND, "Method not found: %s" % m)
        # legacy era
        if m == "initialize":
            return self.initialize(req, transport)
        if m == "ping":
            return {}
        if req.version is None:
            raise RpcError(INVALID_REQUEST, "send initialize first")
        if m == "logging/setLevel":
            level = req.params.get("level")
            if level not in LEVELS:
                raise RpcError(INVALID_PARAMS, "level is one of %s" % ", ".join(LEVELS))
            req.session.log_level = level
            return {}
        if m in common:
            return common[m](req)
        raise RpcError(METHOD_NOT_FOUND, "Method not found: %s" % m)

    def finish(self, req, result):
        if req.era == "modern":
            result.setdefault("resultType", "complete")
            meta = result.setdefault("_meta", {})
            meta[K_SERVER] = {"name": self.info["name"], "version": self.info["version"]}
        return result

    def cacheable(self, req, result, ttl=LIST_TTL_MS):
        if req.era == "modern":
            result["ttlMs"] = ttl
            result["cacheScope"] = "private"       # your own trader/ folder: never for a shared cache
        return result

    # -- lifecycle
    def discover(self, req):
        return {"supportedVersions": SUPPORTED, "capabilities": self.capabilities("modern"),
                "instructions": INSTRUCTIONS, "ttlMs": 3600000, "cacheScope": "public"}

    def initialize(self, req, transport):
        p = req.params
        asked = p.get("protocolVersion")
        if not isinstance(asked, str):
            raise RpcError(INVALID_PARAMS, "initialize needs protocolVersion")
        if asked in MODERN:
            raise RpcError(INVALID_PARAMS, "revision %s has no initialize: send _meta on each request instead"
                           % asked, {"supported": SUPPORTED})
        version = asked if asked in LEGACY else LEGACY[0]
        s = req.session
        s.version = version
        s.caps = p.get("capabilities") if isinstance(p.get("capabilities"), dict) else {}
        s.client = p.get("clientInfo") if isinstance(p.get("clientInfo"), dict) else {}
        s.initialized = False
        log("initialize from %s %s, protocol %s (asked %s)" % (s.client.get("name", "?"), s.client.get("version", ""),
                                                               version, asked))
        return {"protocolVersion": version, "capabilities": self.capabilities("legacy"),
                "serverInfo": dict(self.info), "instructions": INSTRUCTIONS}

    # -- pagination
    def page(self, req, kind, items):
        cur = req.params.get("cursor")
        start = 0
        if cur is not None:
            try:
                doc = json.loads(base64.urlsafe_b64decode(str(cur).encode("ascii") + b"==").decode("utf-8"))
                start = int(doc["o"])
                if doc.get("k") != kind or not 0 <= start <= len(items):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise RpcError(INVALID_PARAMS, "invalid cursor")
        chunk = items[start:start + self.page_size]
        nxt = None
        if start + self.page_size < len(items):
            raw = json.dumps({"k": kind, "o": start + self.page_size}, separators=(",", ":")).encode("utf-8")
            nxt = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return chunk, nxt

    # -- tools
    def tool_def(self, t):
        return {k: t[k] for k in ("name", "title", "description", "inputSchema", "outputSchema", "annotations")}

    def tools_list(self, req):
        chunk, nxt = self.page(req, "tools", [self.tool_def(t) for t in self.kit.tools])
        out = {"tools": chunk}
        if nxt:
            out["nextCursor"] = nxt
        return self.cacheable(req, out)

    def tools_call(self, req):
        p = req.params
        name = p.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
        t = self.kit.by_name.get(name)
        if t is None:
            raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % name)
        args = p.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        problems = validate(args, t["inputSchema"])
        if problems:
            return tool_error("invalid arguments for %s: %s" % (name, "; ".join(problems)))
        state = None
        if "requestState" in p:
            if req.era != "modern":
                raise RpcError(INVALID_PARAMS, "requestState belongs to revision %s" % MODERN[0])
            state = self.open_state(p.get("requestState"), name, args)
        wait = self.kit.limit.take()
        if wait:
            return tool_error("rate limit: too many tool calls; try again in %d seconds" % (int(wait) + 1))
        responses = p.get("inputResponses") if isinstance(p.get("inputResponses"), dict) else {}
        call = toolkit.Call(can_elicit=self.can_elicit(req), elicit_now=self.elicit_now(req),
                            responses=responses, state=state, cancel=req.cancel, log=req.log,
                            progress=lambda f, msg: req.progress_to(1 + round(min(max(float(f), 0.0), 0.99), 3), 2,
                                                                    msg))
        req.progress_to(1, 2, "running %s" % name)
        req.log("info", "tool %s started" % name)
        started = time.time()
        try:
            env = self.kit.call(name, args, call)
        except toolkit.NeedInput as need:
            return {"resultType": "input_required", "inputRequests": {need.key: need.request},
                    "requestState": self.seal_state(name, args, need.bind)}
        except toolkit.ToolError as e:
            req.log("warning", "tool %s: %s" % (name, e))
            return tool_error(str(e))
        if state is not None:
            self.spend_state(state)
        req.log("info", "tool %s finished in %d ms" % (name, int((time.time() - started) * 1000)))
        req.progress_to(2, 2, "done")
        text = json.dumps(env, ensure_ascii=False, separators=(",", ":"), default=str)
        return {"content": [{"type": "text", "text": text}], "structuredContent": env, "isError": False}

    def can_elicit(self, req):
        caps = req.caps if isinstance(req.caps, dict) else {}
        el = caps.get("elicitation")
        if not isinstance(el, dict):
            return False
        if req.era == "modern":
            return "form" in el or not el
        out = req.out
        return out is not None and hasattr(out, "request")   # legacy: only where the server can ask (stdio)

    def elicit_now(self, req):
        if req.era == "modern" or req.out is None or not hasattr(req.out, "request"):
            return None

        def ask(params):
            if req.version == "2025-06-18":
                params = {k: v for k, v in params.items() if k != "mode"}
            return req.out.request("elicitation/create", params, self.elicit_timeout, req.cancel)
        return ask

    # requestState: signed, short-lived, tied to the tool and its arguments, used once
    def _digest(self, name, args):
        blob = json.dumps({"t": name, "a": {k: v for k, v in args.items() if k != "yes_text"}}, sort_keys=True,
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def seal_state(self, name, args, bind):
        body = {"m": "tools/call", "t": name, "d": self._digest(name, args), "bind": bind,
                "exp": int(time.time()) + STATE_TTL, "n": secrets.token_hex(8)}
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8")).decode("ascii")
        sig = hmac.new(self.key, raw.encode("ascii"), hashlib.sha256).hexdigest()
        return raw + "." + sig

    def open_state(self, token, name, args):
        try:
            raw, sig = str(token).rsplit(".", 1)
            good = hmac.new(self.key, raw.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, sig):
                raise ValueError("signature")
            body = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            raise RpcError(INVALID_PARAMS, "requestState failed verification: ask again from the start")
        if body.get("t") != name or body.get("d") != self._digest(name, args) or body.get("m") != "tools/call":
            raise RpcError(INVALID_PARAMS, "requestState belongs to a different request")
        if body.get("exp", 0) < time.time():
            raise RpcError(INVALID_PARAMS, "requestState expired: ask again from the start")
        with self.used_lock:
            if body.get("n") in self.used:
                raise RpcError(INVALID_PARAMS, "requestState was already used")
        return body

    def spend_state(self, body):
        now = time.time()
        with self.used_lock:
            self.used[body.get("n")] = body.get("exp", now)
            for n in [n for n, exp in self.used.items() if exp < now]:
                self.used.pop(n, None)

    # -- resources
    def resources_list(self, req):
        chunk, nxt = self.page(req, "resources", self.kit.resources())
        out = {"resources": chunk}
        if nxt:
            out["nextCursor"] = nxt
        return self.cacheable(req, out, 5000)

    def templates_list(self, req):
        chunk, nxt = self.page(req, "templates", list(toolkit.RESOURCE_TEMPLATES))
        out = {"resourceTemplates": chunk}
        if nxt:
            out["nextCursor"] = nxt
        return self.cacheable(req, out)

    def resources_read(self, req):
        uri = req.params.get("uri")
        if not isinstance(uri, str):
            raise RpcError(INVALID_PARAMS, "resources/read needs a uri")
        path = self.kit.resource_path(uri)
        if path is None:
            code = INVALID_PARAMS if req.era == "modern" else LEGACY_RESOURCE_NOT_FOUND
            raise RpcError(code, "Resource not found", {"uri": uri})
        with open(path, "rb") as fh:
            data = fh.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise RpcError(INVALID_PARAMS, "%s is over 4 MB" % uri)
        text = data.decode("utf-8", errors="replace")
        return self.cacheable(req, {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}, 5000)

    # -- prompts
    def prompts_list(self, req):
        defs = [{k: p[k] for k in ("name", "title", "description", "arguments")} for p in self.kit.prompts]
        chunk, nxt = self.page(req, "prompts", defs)
        out = {"prompts": chunk}
        if nxt:
            out["nextCursor"] = nxt
        return self.cacheable(req, out)

    def prompts_get(self, req):
        name = req.params.get("name")
        p = self.kit.prompt_by_name.get(name) if isinstance(name, str) else None
        if p is None:
            raise RpcError(INVALID_PARAMS, "Unknown prompt: %s" % name)
        args = req.params.get("arguments")
        if args is not None and not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        try:
            text = toolkit.render_prompt(p, args)
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e))
        return {"description": p["description"], "messages": [{"role": "user", "content": {"type": "text",
                                                                                            "text": text}}]}


def tool_error(message):
    text = "%s %s" % (message.rstrip(". ") + ".", toolkit.DISCLAIMER)
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _good_id(v):
    return isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    out = {"jsonrpc": "2.0", "error": err}
    if rid is not None:
        out["id"] = rid
    return out


# ---------------------------------------------------------------- a small JSON Schema check for tool arguments

_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _is(value, typ):
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "null":
        return value is None
    return isinstance(value, _TYPES[typ])


def validate(value, schema, path="arguments"):
    """The problems with `value` against the subset of JSON Schema 2020-12 the tool schemas use."""
    out = []
    typ = schema.get("type")
    if typ and not _is(value, typ):
        return ["%s must be %s" % (path, {"integer": "a whole number", "number": "a number", "string": "text",
                                          "boolean": "true or false", "object": "an object",
                                          "array": "a list"}.get(typ, typ))]
    if "const" in schema and value != schema["const"]:
        out.append("%s must be %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        out.append("%s must be one of %s" % (path, ", ".join(str(x) for x in schema["enum"])))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append("%s is too short" % path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append("%s is too long (at most %d characters)" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append("%s %r is not in the expected form" % (path, value[:40]))
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            out.append("%s must be at least %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append("%s must be at most %s" % (path, schema["maximum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append("%s must be above %s" % (path, schema["exclusiveMinimum"]))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append("%s.%s is required" % (path, k))
        for k, v in value.items():
            if k in props:
                out += validate(v, props[k], k if path == "arguments" else "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                out.append("unknown argument %s" % k if path == "arguments" else "%s.%s is not allowed" % (path, k))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out += validate(v, schema["items"], "%s[%d]" % (path, i))
    return out


# ---------------------------------------------------------------- stdio transport

class StdioOut(object):
    """Writes one JSON-RPC message per line to the protocol stream; asks the client (legacy elicitation)."""

    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()
        self.pending = {}
        self.plock = threading.Lock()
        self.n = 0

    def send(self, msg):
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)
        data = (line + "\n").encode("utf-8")
        with self.lock:
            try:
                self.stream.write(data)
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    notify = send

    def request(self, method, params, timeout, cancel):
        with self.plock:
            self.n += 1
            rid = "bt-%d" % self.n
            slot = {"event": threading.Event(), "msg": None}
            self.pending[rid] = slot
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while not slot["event"].wait(0.25):
            if cancel.is_set() or time.time() > deadline:
                with self.plock:
                    self.pending.pop(rid, None)
                self.notify({"jsonrpc": "2.0", "method": "notifications/cancelled",
                             "params": {"requestId": rid, "reason": "no answer" if not cancel.is_set()
                                        else "the request was cancelled"}})
                return {"action": "cancel"}
        msg = slot["msg"]
        if "error" in msg:
            return {"action": "cancel"}
        return msg.get("result") or {"action": "cancel"}

    def resolve(self, msg):
        with self.plock:
            slot = self.pending.pop(msg.get("id"), None)
        if slot is not None:
            slot["msg"] = msg
            slot["event"].set()


def serve_stdio(server, inp, proto):
    out = StdioOut(proto)
    session = Session()
    workers = threading.BoundedSemaphore(8)
    threads = []

    def work(msg):
        try:
            resp = server.handle(msg, session, out, "stdio")
            if resp is not None:
                out.send(resp)
        finally:
            workers.release()

    while True:
        line = inp.readline(MAX_LINE + 1)
        if not line:
            break                                  # stdin closed: the client is shutting us down
        if len(line) > MAX_LINE:
            out.send(_error(None, INVALID_REQUEST, "message too large"))
            while line and not line.endswith(b"\n"):
                line = inp.readline(MAX_LINE + 1)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            out.send(_error(None, PARSE_ERROR, "Parse error"))
            continue
        if isinstance(msg, list):
            out.send(_error(None, INVALID_REQUEST, "JSON-RPC batches are not part of MCP"))
            continue
        if isinstance(msg, dict) and "method" not in msg:
            server.handle(msg, session, out, "stdio")   # a reply to our question: resolve it inline
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize" or not isinstance(msg, dict) \
                or "id" not in msg:
            resp = server.handle(msg, session, out, "stdio")   # handshake and notifications stay in order
            if resp is not None:
                out.send(resp)
            continue
        workers.acquire()
        th = threading.Thread(target=work, args=(msg,), daemon=True)
        th.start()
        threads.append(th)
        threads = [t for t in threads if t.is_alive()]
    for th in threads:
        th.join(5)
    return 0


# ---------------------------------------------------------------- streamable HTTP transport (127.0.0.1)

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")


def _host_ok(value, port):
    if not value:
        return False
    try:
        u = urllib.parse.urlsplit("//" + value)
        return u.hostname in LOOPBACK_NAMES and (u.port is None or u.port == port)
    except ValueError:
        return False


def _origin_ok(value):
    if value is None:
        return True                                 # not a browser
    try:
        u = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and u.hostname in LOOPBACK_NAMES


def _header_value(v):
    if v is not None and v.startswith("=?base64?") and v.endswith("?="):
        try:
            return base64.b64decode(v[len("=?base64?"):-2]).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
    return v


class HttpOut(object):
    """HTTP answers are single JSON objects: request-scoped notifications are not streamed."""

    def notify(self, msg):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "bharat-trader-mcp"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            log("http " + fmt % args)

    def _send(self, status, body=None, headers=None):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _guard(self):
        if self.path.split("?", 1)[0] != "/mcp":
            self._send(404, _error(None, INVALID_REQUEST, "the MCP endpoint is /mcp"))
            return False
        if not _host_ok(self.headers.get("Host"), self.server.server_address[1]):
            self._send(403, _error(None, INVALID_REQUEST, "Host must be this server on 127.0.0.1"))
            return False
        if not _origin_ok(self.headers.get("Origin")):
            self._send(403, _error(None, INVALID_REQUEST, "Origin not allowed"))
            return False
        return True

    def do_GET(self):
        if self._guard():
            self._send(405, _error(None, INVALID_REQUEST, "POST JSON-RPC messages to /mcp"), {"Allow": "POST"})

    def do_DELETE(self):
        if not self._guard():
            return
        sid = self.headers.get("Mcp-Session-Id")
        with self.server.slock:
            gone = self.server.sessions.pop(sid, None) if sid else None
        self._send(200 if gone else 404 if sid else 405)

    def do_POST(self):
        if not self._guard():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, _error(None, INVALID_REQUEST, "Content-Type must be application/json"))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 1024 * 1024:
            return self._send(413 if n > 0 else 400, _error(None, INVALID_REQUEST, "a JSON body up to 1 MB"))
        try:
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeError):
            return self._send(400, _error(None, PARSE_ERROR, "Parse error"))
        if not isinstance(msg, dict):
            return self._send(400, _error(None, INVALID_REQUEST, "one JSON-RPC message per POST"))
        srv = self.server
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        hv = self.headers.get("MCP-Protocol-Version")
        sid = self.headers.get("Mcp-Session-Id")
        rid = msg.get("id") if _good_id(msg.get("id")) else None
        headers = {}
        if K_VERSION in meta:                       # modern: headers mirror the body
            m = msg.get("method")
            want_name = params.get("uri") if m == "resources/read" else params.get("name")
            if hv != meta[K_VERSION]:
                return self._send(400, _error(rid, HEADER_MISMATCH, "MCP-Protocol-Version header does not match "
                                                                     "_meta"))
            if self.headers.get("Mcp-Method") != m:
                return self._send(400, _error(rid, HEADER_MISMATCH, "Mcp-Method header does not match the method"))
            if m in ("tools/call", "resources/read", "prompts/get") and \
                    _header_value(self.headers.get("Mcp-Name")) != want_name:
                return self._send(400, _error(rid, HEADER_MISMATCH, "Mcp-Name header does not match the body"))
            session = Session()
        elif msg.get("method") == "initialize":
            session = Session(secrets.token_urlsafe(24))
            with srv.slock:
                if len(srv.sessions) >= 64:
                    srv.sessions.pop(next(iter(srv.sessions)))
                srv.sessions[session.sid] = session
            headers["Mcp-Session-Id"] = session.sid
        elif sid:
            with srv.slock:
                session = srv.sessions.get(sid)
            if session is None:
                return self._send(404, _error(rid, INVALID_REQUEST, "unknown or expired session: initialize again"))
            if hv and session.version and hv != session.version:
                return self._send(400, _error(rid, INVALID_REQUEST, "MCP-Protocol-Version header does not match "
                                                                     "the session"))
        else:
            session = Session()
        resp = srv.mcp.handle(msg, session, HttpOut(), "http")
        if resp is None:
            return self._send(202, None, headers)
        status = 200
        if "error" in resp:
            code = resp["error"]["code"]
            if code == METHOD_NOT_FOUND:
                status = 404
            elif code in (PARSE_ERROR, INVALID_REQUEST, INVALID_PARAMS, HEADER_MISMATCH, MISSING_CAPABILITY,
                          UNSUPPORTED_VERSION, LEGACY_RESOURCE_NOT_FOUND):
                status = 400
            else:
                status = 500
        self._send(status, resp, headers)


class McpHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, mcp, verbose=False):
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", port), Handler)
        self.mcp, self.verbose = mcp, verbose
        self.sessions, self.slock = {}, threading.Lock()


# ---------------------------------------------------------------- start

def block_network():
    """--offline: no tool may reach the network (the HTTP transport only listens)."""
    def refuse(*a, **k):
        raise OSError("offline: no network")
    socket.create_connection = refuse
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse


def parser():
    ap = argparse.ArgumentParser(description="bharat-trader MCP server (stdio by default; paper trading only).")
    ap.add_argument("--workdir", help="the folder whose trader/ files the tools read and write (default: here)")
    ap.add_argument("--offline", metavar="DIR", help="saved answers from DIR instead of the network")
    ap.add_argument("--now", help="pin the clock, IST, e.g. \"2026-10-29 14:30\"")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on 127.0.0.1:PORT instead of stdio")
    ap.add_argument("--rate-limit", type=int, default=30, metavar="N", help="tool calls a minute (default 30)")
    ap.add_argument("--page-size", type=int, default=100, metavar="N", help="items per list page (default 100)")
    ap.add_argument("--tool-reference", action="store_true", help="print the tool reference (Markdown) and exit")
    ap.add_argument("--verbose", action="store_true", help="log HTTP requests to stderr")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    if a.tool_reference:
        sys.stdout.write(toolkit.tool_reference_md())
        return 0
    # stdout is the protocol channel: keep a private copy of it and point fd 1 at stderr, so a stray
    # print anywhere can never corrupt a JSON-RPC message
    proto = None
    if a.http is None:
        proto = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        sys.stdout.flush()
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    temp = None
    try:
        workdir = a.workdir
        if not workdir and a.offline:
            src = toolkit.Offline(a.offline).trader_dir()
            if src and os.path.isdir(src):
                temp = tempfile.mkdtemp(prefix="bharat-trader-mcp-")
                shutil.copytree(src, os.path.join(temp, "trader"))
                workdir = temp
        workdir = os.path.abspath(workdir or os.getcwd())
        if not os.path.isdir(workdir):
            log("--workdir %s is not a folder" % workdir)
            return 2
        os.chdir(workdir)                          # a skill's relative default can only land in here
        try:
            kit = toolkit.Kit(workdir, offline=a.offline, now=a.now, rate_per_minute=a.rate_limit)
        except (ValueError, OSError) as e:
            log(str(e))
            return 2
        if a.offline:
            block_network()
        server = Server(kit, page_size=a.page_size)
        log("serving %s/trader%s, protocol %s (and %s), %d tools, paper trading only"
            % (workdir, " offline from %s" % a.offline if a.offline else "", MODERN[0], ", ".join(LEGACY),
               len(kit.tools)))
        if temp:
            log("offline demo copy of the fixtures' trader/ folder: %s (deleted on exit)" % temp)
        if a.http is not None:
            try:
                httpd = McpHttpServer(a.http, server, a.verbose)
            except OSError as e:
                log("cannot listen on 127.0.0.1:%d: %s" % (a.http, e.strerror or e))
                return 1
            log("streamable HTTP on http://127.0.0.1:%d/mcp" % httpd.server_address[1])

            def stop(signum, frame):
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            signal.signal(signal.SIGINT, stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, stop)
            try:
                httpd.serve_forever(poll_interval=0.5)
            finally:
                httpd.server_close()
            return 0
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))
        return serve_stdio(server, sys.stdin.buffer, proto)
    finally:
        if temp:
            os.chdir(tempfile.gettempdir())
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
