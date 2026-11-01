#!/usr/bin/env python3
"""Screenshots of the local UI with a headless browser you already have. Standard library only.

    python3 ui/screenshots.py                          # every page at 1280, 1440 and 390 px -> ./ui-shots/
    python3 ui/screenshots.py --out docs/images --pages today,lab --widths 1440 --theme dark
    python3 ui/screenshots.py --fresh --pages welcome  # an empty working folder: the first-run setup

It serves the offline demo (tests/fixtures/ui, saved real data, clock pinned) on a free loopback
port and drives a headless Chromium-family browser (Chrome, Chromium, Edge) over its DevTools
protocol, so the viewport is exactly the width asked for (a phone is 390 px). For each shot it also
measures whether the page scrolls sideways (a layout bug) and prints it. Set BT_BROWSER to the
browser's path if it is not found. No browser: it says so and exits 2, and the screenshot smoke
test skips. Nothing here touches the network or your own trader/ folder.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import serve  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "ui")
PAGES = ["today", "chart", "scanner", "options", "strategy", "cotrader", "portfolio", "risk", "autopilot",
         "lab", "journal", "agents", "settings", "welcome", "about"]
HEIGHTS = {390: 844}
CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"]
# what each shot measures in the page: sideways scroll and the nav links that cannot be seen or reached
PROBE = ("(() => { const W = document.documentElement.clientWidth;"
         " const wide = document.documentElement.scrollWidth - W;"
         " const links = [...document.querySelectorAll('#nav a')];"
         " return JSON.stringify({width: W, overflow: wide, nav: links.length,"
         " title: document.title, errors: [...document.querySelectorAll('.note.error')].map(e => e.textContent)}); })()")


def find_browser():
    env = os.environ.get("BT_BROWSER")
    if env:
        return env if os.path.isfile(env) else None
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    for n in NAMES:
        p = shutil.which(n)
        if p:
            return p
    return None


def page_hash(name):
    return "#/" if name in ("", "today") else "#/" + name


# ---------------------------------------------------------------- a minimal DevTools client

class DevTools(object):
    """One headless browser and one page, driven over the DevTools websocket (RFC 6455, text frames)."""

    def __init__(self, browser):
        self.profile = tempfile.mkdtemp(prefix="bt-shot-")
        self.proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--remote-debugging-port=0",
             "--user-data-dir=" + self.profile, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.next_id = 0
        port = self._port()
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=10) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
        ws = pages[0]["webSocketDebuggerUrl"]
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        path = ws.split(str(port), 1)[1]
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, port, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise IOError("the browser closed the DevTools connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise IOError("the browser refused the DevTools connection")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _port(self):
        f = os.path.join(self.profile, "DevToolsActivePort")
        for _ in range(200):
            if os.path.isfile(f):
                with open(f) as fh:
                    first = fh.readline().strip()
                if first.isdigit():
                    return int(first)
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        raise IOError("the browser did not start its DevTools port")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("the browser closed the DevTools connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        data = b""
        while True:
            b1, b2 = struct.unpack("!BB", self._read(2))
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            data += self._read(n)
            if b1 & 0x80:
                return data

    def call(self, method, **params):
        self.next_id += 1
        msg = json.dumps({"id": self.next_id, "method": method, "params": params}).encode("utf-8")
        mask = os.urandom(4)
        n = len(msg)
        head = struct.pack("!B", 0x81) + (struct.pack("!B", 0x80 | n) if n < 126 else
                                           struct.pack("!BH", 0x80 | 126, n) if n < 65536 else
                                           struct.pack("!BQ", 0x80 | 127, n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
        while True:
            reply = json.loads(self._frame().decode("utf-8"))
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise IOError("%s: %s" % (method, reply["error"].get("message")))
                return reply.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def shoot(dt, url, path, width, height, theme="light", settle=2.5):
    """One screenshot of url at exactly width x height CSS px; returns what the page measured."""
    dt.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1,
            mobile=width < 600)
    dt.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme}])
    dt.call("Page.navigate", url="about:blank")
    dt.call("Page.navigate", url=url)
    time.sleep(settle)
    probe = json.loads(dt.call("Runtime.evaluate", expression=PROBE, returnByValue=True)["result"]["value"])
    png = base64.b64decode(dt.call("Page.captureScreenshot", format="png")["data"])
    with open(path, "wb") as fh:
        fh.write(png)
    return probe


class Demo(object):
    """The offline demo on a free port; fresh=True serves an empty working folder instead."""

    def __init__(self, fresh=False):
        self.dir = tempfile.mkdtemp(prefix="bt-shots-")
        if not fresh:
            shutil.copytree(os.path.join(FIXTURES, "trader"), os.path.join(self.dir, "trader"))
            serve.copy_demo_lab(FIXTURES, self.dir)
        self.srv = serve.make_server("127.0.0.1", 0, self.dir, FIXTURES)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d/" % self.srv.server_address[1]

    def close(self):
        self.srv.stop()
        self.thread.join(5)
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


def run(out, pages, widths, theme="light", fresh=False, browser=None, prefix="", settle=2.5):
    """[(path, probe)] or None without a browser."""
    browser = browser or find_browser()
    if not browser:
        return None
    os.makedirs(out, exist_ok=True)
    demo = Demo(fresh=fresh)
    dt = DevTools(browser)
    written = []
    try:
        for name in pages:
            for w in widths:
                fname = "%s%s-%d%s.png" % (prefix, name or "today", w, "-dark" if theme == "dark" else "")
                path = os.path.join(out, fname)
                written.append((path, shoot(dt, demo.base + page_hash(name), path, w, HEIGHTS.get(w, 900), theme,
                                            settle)))
    finally:
        dt.close()
        demo.close()
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Screenshots of the local UI (offline demo) with a headless browser.")
    ap.add_argument("--out", default="ui-shots", help="folder for the PNGs (default ./ui-shots)")
    ap.add_argument("--pages", default=",".join(PAGES), help="comma list (default: every page)")
    ap.add_argument("--widths", default="1280,1440,390", help="comma list of widths in px")
    ap.add_argument("--theme", choices=("light", "dark"), default="light")
    ap.add_argument("--fresh", action="store_true", help="an empty working folder (first-run setup)")
    ap.add_argument("--prefix", default="", help="file name prefix")
    a = ap.parse_args(argv)
    pages = [p.strip() for p in a.pages.split(",") if p.strip()]
    widths = [int(w) for w in a.widths.split(",") if w.strip()]
    got = run(a.out, pages, widths, a.theme, a.fresh, prefix=a.prefix)
    if got is None:
        sys.stderr.write("screenshots.py: no headless browser found (Chrome, Chromium or Edge); set BT_BROWSER\n")
        return 2
    bad = 0
    for path, probe in got:
        flag = ""
        if probe["overflow"] > 0:
            flag += "  SIDEWAYS SCROLL %d px" % probe["overflow"]
        if probe["errors"]:
            flag += "  ERROR: " + "; ".join(probe["errors"])[:200]
        bad += bool(flag)
        print(path + flag)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
