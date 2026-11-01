"""Login and preflight helpers: a one-shot 127.0.0.1 OAuth callback, the public IP, static-IP words.

The callback server listens on 127.0.0.1 only, accepts ONE request on the registered path,
checks the state value in constant time (where the broker sends one back), answers with a short
"you can close this tab" page and shuts down. It never logs the code it receives.
"""

import hmac
import http.server
import ipaddress
import secrets
import threading
import time
import urllib.parse
import urllib.request

from .base import BrokerError, remember_secret, scrub

PAGE = (b"<!doctype html><meta charset=utf-8><title>bharat-trader login</title>"
        b"<body style=\"font-family:system-ui;margin:3em\"><h1>Logged in</h1>"
        b"<p>You can close this tab and go back to your terminal.</p></body>")
BAD_PAGE = (b"<!doctype html><meta charset=utf-8><title>bharat-trader login</title>"
            b"<body style=\"font-family:system-ui;margin:3em\"><h1>Login not accepted</h1>"
            b"<p>%s</p></body>")
IP_ECHO = ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://icanhazip.com")


class LoginError(BrokerError):
    kind = "login"


def new_state():
    return secrets.token_urlsafe(24)


def parse_redirect(url):
    """(port, path) of a local redirect URL like http://127.0.0.1:8765/callback. Only 127.0.0.1 or localhost."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost"):
        raise LoginError("the redirect URL must be http://127.0.0.1:<port>/<path> (registered the same way in your "
                         "broker's developer console), not %r" % (url,))
    if not u.port:
        raise LoginError("the redirect URL needs a port, e.g. http://127.0.0.1:8765/callback")
    return u.port, u.path or "/"


def wait_for_callback(port, path, want_param, state=None, state_param="state", timeout=180, on_ready=None,
                      host="127.0.0.1"):
    """Serve one callback on host:port/path. -> {query params}. Raises LoginError on a bad state, an
    error from the broker or a timeout. on_ready(url) is called once the server is listening."""
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):          # never print the code
            pass

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path.rstrip("/") != path.rstrip("/") or done.is_set():
                self._send(404, BAD_PAGE % b"Not this page.")
                return
            q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            for k in ("code", "request_token", "tokenId", "auth_code"):
                if q.get(k):
                    remember_secret(q[k])
            if state is not None:
                got = q.get(state_param) or ""
                if not hmac.compare_digest(got.encode("utf-8"), state.encode("utf-8")):
                    result["error"] = "the login came back with a different state value (not the login this " \
                                      "command started); nothing was exchanged"
                    self._send(400, BAD_PAGE % b"The state value did not match. Start the login again.")
                    done.set()
                    return
            if not q.get(want_param):
                why = q.get("error_description") or q.get("error") or q.get("status") or "no %s" % want_param
                result["error"] = "the broker did not send a login code back (%s)" % scrub(why)
                self._send(400, BAD_PAGE % b"No login code came back.")
                done.set()
                return
            result["params"] = q
            self._send(200, PAGE)
            done.set()

    try:
        srv = http.server.HTTPServer((host, port), Handler)
    except OSError as e:
        raise LoginError("cannot listen on %s:%d for the login callback (%s); is another login running?"
                         % (host, port, e))
    srv.timeout = 0.5
    try:
        if on_ready:
            on_ready("http://%s:%d%s" % (host, srv.server_address[1], path))
        end = time.monotonic() + timeout
        while not done.is_set() and time.monotonic() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if "error" in result:
        raise LoginError(result["error"])
    if "params" not in result:
        raise LoginError("no login came back within %d seconds; run the login again" % timeout)
    return result["params"]


def public_ip(services=IP_ECHO, opener=None, timeout=8, need=2):
    """(ip or None, [(service, answer or error)]). The IP counts only when `need` services agree."""
    opener = opener or urllib.request.urlopen
    seen, notes = {}, []
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bharat-trader preflight"})
            with opener(req, timeout=timeout) as r:
                txt = r.read(200).decode("ascii", "replace").strip()
            ip = str(ipaddress.ip_address(txt))
            seen[ip] = seen.get(ip, 0) + 1
            notes.append((url, ip))
        except (ValueError, OSError) as e:
            notes.append((url, "no answer (%s)" % (getattr(e, "reason", None) or e.__class__.__name__)))
        best = max(seen.items(), key=lambda kv: kv[1]) if seen else None
        if best and best[1] >= need:
            return best[0], notes
    return None, notes


def static_ips(cfg, broker):
    """The static IPs the user registered, from the config: "static_ip": "203.0.113.7", a list, or
    {"kite": "...", "upstox": [...]} per broker."""
    v = (cfg or {}).get("static_ip")
    if isinstance(v, dict):
        v = v.get(broker)
    if isinstance(v, str):
        v = [v] if v.strip() else []
    out = []
    for x in v or []:
        try:
            out.append(str(ipaddress.ip_address(str(x).strip())))
        except ValueError:
            out.append(str(x).strip())
    return out


def totp(secret, at=None, digits=6, step=30):
    """RFC 6238 time-based one-time code (HMAC-SHA1, 30-second steps): the code your authenticator app shows
    for a base32 secret. at: Unix seconds (default now). Raises LoginError on a secret that is not base32."""
    import base64
    import struct
    s = "".join(str(secret or "").split()).upper().rstrip("=")
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except (ValueError, TypeError):
        raise LoginError("the TOTP secret is not base32 text (the key your broker showed when you enabled TOTP)")
    if not key:
        raise LoginError("the TOTP secret is empty")
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def clean_code(text, digits=6):
    """A typed one-time code: digits only, exactly `digits` of them, or LoginError."""
    s = "".join(str(text or "").split())
    if not (s.isdigit() and len(s) == digits):
        raise LoginError("the one-time code is %d digits from your authenticator app" % digits)
    return s


SEBI_WORDS = ("Why this matters: under SEBI's retail algo framework (circular of 4 Feb 2025, in force since "
              "1 Apr 2026) brokers accept API orders (place, modify, cancel, GTT) only from the static IP you "
              "registered with them, most let you change it about once a week, and you log in with 2FA once "
              "every trading day (no long-lived sessions). Reading holdings, positions and quotes works from "
              "any IP. More than 10 orders a second needs exchange algo registration; this tool sends at most "
              "%s a second.")
