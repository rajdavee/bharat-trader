#!/usr/bin/env python3
"""The bharat-trader local UI: a small web app on 127.0.0.1. Standard library only.

    python3 ui/serve.py --open                         # http://127.0.0.1:8611/, reads ./trader/
    python3 ui/serve.py --workdir ~/trading --port 9000
    python3 ui/serve.py --offline tests/fixtures/ui --open   # demo on saved real data, no network

It reads the trader/ folder of --workdir (default: the current folder) through the skills'
own functions (ui/api.py has the one route table). It never places a live order: the only
order writes are the paper broker's own commands, which need trader-risk PASS for that exact
order plus your typed yes.

Security, because a web page from anywhere can try to talk to localhost:
  - binds 127.0.0.1 only; another --host prints a loud warning (anyone who can reach it
    can see your account);
  - the Host header must be this server (stops DNS rebinding) and a browser Origin must be
    this server's own; no CORS headers at all;
  - every POST needs the session cookie AND its CSRF token in X-CSRF-Token, and a JSON body;
  - a strict Content-Security-Policy: scripts, styles and data from this server only,
    nothing inline, nothing external, no framing.

--offline DIR serves saved answers (DIR/yahoo-<SYMBOL>.json, DIR/chart-<SYMBOL>-<interval>.json,
DIR/nse-marketStatus.json) and pins the clock to DIR/offline.json's "now". If DIR has a trader/
folder and you give no --workdir, a temporary copy of it is used (plus DIR/lab/ as its trader/lab/,
saved Strategy Lab runs), so the demo never edits the fixtures. Ctrl-C stops the server cleanly.
"""

import argparse
import hmac
import http.cookies
import json
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_PORT = 8611
MAX_BODY = 256 * 1024
COOKIE = "bt_session"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# The chart library injects one <style> for its attribution logo; its hash is allowed, nothing else inline.
LWC_LOGO_STYLE = "'sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY='"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' %s; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'" % LWC_LOGO_STYLE)
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}
PLAIN_NAMES = {"LICENSE", "NOTICE"}


class Sessions(object):
    """session id (HttpOnly cookie) -> CSRF token (in the page's meta tag)."""

    def __init__(self, limit=64):
        self.limit = limit
        self.tokens = {}
        self.lock = threading.Lock()

    def new(self):
        sid, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            if len(self.tokens) >= self.limit:
                self.tokens.pop(next(iter(self.tokens)))
            self.tokens[sid] = token
        return sid, token

    def token(self, sid):
        with self.lock:
            return self.tokens.get(sid) if sid else None


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ctx, quiet=True):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        ThreadingHTTPServer.__init__(self, addr, Handler)
        self.ctx = ctx
        self.quiet = quiet
        self.sessions = Sessions()
        self.poller = api.Poller(ctx)
        self.stopping = threading.Event()
        port = self.server_address[1]
        self.port = port
        host = addr[0]
        names = set(LOOPBACK) | {host}
        self.allowed_hosts = set()
        for n in names:
            n = "[%s]" % n if ":" in n else n
            self.allowed_hosts.add("%s:%d" % (n, port))
        self.allowed_origins = {"http://" + h for h in self.allowed_hosts}

    def url(self):
        h = self.server_address[0]
        h = "127.0.0.1" if h in ("0.0.0.0", "") else ("[%s]" % h if ":" in h else h)
        return "http://%s:%d/" % (h, self.port)

    def handle_error(self, request, client_address):
        """A browser that closes a connection early is normal, not an error worth a traceback."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)

    def stop(self):
        self.stopping.set()
        self.poller.close()
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "bharat-trader-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):
        if not self.server.quiet:
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _headers(self, status, ctype, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def _cookie_sid(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _guard(self):
        """Host and Origin checks for every request. False = already answered."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            self._error(421, "this server only answers to %s" % sorted(self.server.allowed_hosts)[0])
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in self.server.allowed_origins:
            self._error(403, "cross-origin requests are refused")
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            self._error(403, "cross-site requests are refused")
            return False
        return True

    # ------------------------------------------------------------ verbs
    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path == api.STREAM_PATH:
            return self._stream()
        if path.startswith("/api/"):
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
            return self._api("GET", path, q, None)
        if path in ("/", "/index.html"):
            return self._index()
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        self._error(404, "not found")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._guard():
            return
        u = urllib.parse.urlsplit(self.path)
        if not u.path.startswith("/api/"):
            return self._error(404, "not found")
        token = self.server.sessions.token(self._cookie_sid())
        sent = self.headers.get("X-CSRF-Token") or ""
        if not token or not hmac.compare_digest(token.encode(), sent.encode()):
            return self._error(403, "missing or wrong CSRF token: reload the page")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._error(415, "send JSON (Content-Type: application/json)")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "bad Content-Length")
        if n > MAX_BODY:
            return self._error(413, "request too large")
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "the body is not valid JSON")
        self._api("POST", u.path, {}, body)

    def _refuse(self):
        if self._guard():
            self._error(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    # ------------------------------------------------------------ handlers
    def _api(self, method, path, q, body):
        try:
            status, out = api.dispatch(self.server.ctx, method, path, q, body)
        except Exception as e:  # a bug, not the user's fault: say so without a stack trace
            sys.stderr.write("ui error on %s %s: %r\n" % (method, path, e))
            status, out = 500, {"error": "internal error: %s" % e.__class__.__name__}
        self._send(status, out)

    def _index(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        sid = self._cookie_sid()
        token = self.server.sessions.token(sid)
        extra = {}
        if not token:
            sid, token = self.server.sessions.new()
            extra["Set-Cookie"] = "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE, sid)
        html = html.replace("__CSRF_TOKEN__", token)
        self._send(200, html.encode("utf-8"), TYPES[".html"], extra)

    def _static(self, rel):
        rel = urllib.parse.unquote(rel)
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self._error(404, "not found")
        name = os.path.basename(full)
        ext = os.path.splitext(name)[1].lower()
        ctype = TYPES.get(ext) or (TYPES[".txt"] if name in PLAIN_NAMES else None)
        if not ctype or name == "index.html":
            return self._error(404, "not found")
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    def _stream(self):
        """Server-Sent Events: status, prices and alerts whenever the poller has something new."""
        poller = self.server.poller
        poller.start()
        poller.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            seen, last = 0, {}
            while not self.server.stopping.is_set():
                version, snap = poller.wait(seen, 15)
                if version == seen:
                    self.wfile.write(b": ping\n\n")
                else:
                    seen = version
                    for name in ("status", "prices", "alerts"):
                        if name in snap and snap[name] != last.get(name):
                            data = json.dumps(snap[name], ensure_ascii=False, default=str)
                            self.wfile.write(("id: %d\nevent: %s\ndata: %s\n\n" % (version, name, data))
                                             .encode("utf-8"))
                            last[name] = snap[name]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            poller.unsubscribe()


# ---------------------------------------------------------------- start

def offline_workdir(offline):
    """A throwaway copy of DIR/trader/, so the demo can be clicked through without editing the fixtures."""
    src = os.path.join(offline, "trader")
    if not os.path.isdir(src):
        return None
    tmp = tempfile.mkdtemp(prefix="bharat-trader-ui-")
    shutil.copytree(src, os.path.join(tmp, "trader"))
    copy_demo_lab(offline, tmp)
    copy_demo_journal(offline, tmp)
    return tmp


def copy_demo_lab(offline, workdir):
    """DIR/lab/ (saved Strategy Lab runs, if any) becomes the demo's trader/lab/ so the Lab page has runs."""
    lab = os.path.join(offline, "lab")
    if os.path.isdir(lab) and not os.path.exists(os.path.join(workdir, "trader", "lab")):
        shutil.copytree(lab, os.path.join(workdir, "trader", "lab"))


def copy_demo_journal(offline, workdir):
    """DIR/journal/ (imported broker trades, notes) becomes the demo's trader/journal/ so the Journal page has trades."""
    src = os.path.join(offline, "journal")
    if os.path.isdir(src) and not os.path.exists(os.path.join(workdir, "trader", "journal")):
        shutil.copytree(src, os.path.join(workdir, "trader", "journal"))


def make_server(host="127.0.0.1", port=DEFAULT_PORT, workdir=".", offline=None, now=None, quiet=True):
    ctx = api.Context(workdir, offline=offline, now=now)
    return UIServer((host, port), ctx, quiet=quiet)


def parser():
    ap = argparse.ArgumentParser(description="bharat-trader local UI on 127.0.0.1 (paper trading only).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default %d; 0 picks a free one)"
                    % DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default 127.0.0.1; anything else exposes your account)")
    ap.add_argument("--workdir", help="the folder whose trader/ files the UI reads (default: here)")
    ap.add_argument("--offline", metavar="DIR", help="serve saved answers from DIR, no network")
    ap.add_argument("--now", help="pin the clock, IST, e.g. \"2026-10-29 14:30\"")
    ap.add_argument("--open", action="store_true", help="open the UI in your browser")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)

    if a.host not in LOOPBACK:
        bar = "!" * 72
        sys.stderr.write("%s\nWARNING: binding %s, not 127.0.0.1. Anyone who can reach this address can see\n"
                         "your trader/ files and record paper decisions. There is no login. Use it only on a\n"
                         "network you trust, and prefer an SSH tunnel to 127.0.0.1 instead.\n%s\n"
                         % (bar, a.host, bar))
    workdir = a.workdir
    temp = None
    if not workdir and a.offline:
        temp = offline_workdir(a.offline)
        workdir = temp
    workdir = workdir or os.getcwd()
    try:
        srv = make_server(a.host, a.port, workdir, a.offline, a.now, quiet=not a.verbose)
    except OSError as e:
        sys.stderr.write("serve.py: cannot listen on %s:%d: %s\n" % (a.host, a.port, e.strerror or e))
        return 1
    except Exception as e:
        sys.stderr.write("serve.py: %s\n" % e)
        return 1
    url = srv.url()
    print("bharat-trader UI on %s (paper trading only)" % url)
    print("Reading %s" % os.path.join(srv.ctx.workdir, "trader"))
    if a.offline:
        print("Offline: answers from %s, clock pinned to %s IST." % (srv.ctx.offline, srv.ctx.pinned or "now"))
    if temp:
        print("Demo copy of the fixtures' trader/ folder: %s (deleted when you stop)" % temp)
    print("Ctrl-C to stop.")
    sys.stdout.flush()

    def on_signal(signum, frame):
        srv.stop()
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    if a.open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.stop()
        srv.server_close()
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
