#!/usr/bin/env python3
"""Screenshots of the local UI with a headless browser you already have. Standard library only.

    python3 ui/screenshots.py                          # every page at 1280, 1440 and 390 px -> ./ui-shots/
    python3 ui/screenshots.py --out docs/images --pages today,lab --widths 1440 --dark
    python3 ui/screenshots.py --fresh                  # an empty working folder: the first-run setup

It serves the offline demo (tests/fixtures/ui, saved real data, clock pinned) on a free loopback
port and points a headless Chromium-family browser (Chrome, Chromium, Edge) at each page. Set
BT_BROWSER to the browser's path if it is not found. No browser: it says so and exits 2, and the
screenshot smoke test skips. Nothing here touches the network or your own trader/ folder.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import serve  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "ui")
PAGES = ["today", "chart", "scanner", "options", "strategy", "cotrader", "risk", "portfolio", "autopilot",
         "lab", "journal", "agents", "settings", "about"]
HEIGHTS = {390: 844}
CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"]


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


def shoot(browser, url, path, width, height, dark=False, budget=4000):
    """One headless screenshot of url at width x height; True when the PNG was written."""
    profile = tempfile.mkdtemp(prefix="bt-shot-")
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions", "--user-data-dir=" + profile,
           "--window-size=%d,%d" % (width, height), "--timeout=%d" % budget,
           "--screenshot=" + os.path.abspath(path)]
    if dark:
        cmd.append("--force-dark-mode")
    cmd.append(url)
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return os.path.isfile(path) and os.path.getsize(path) > 0


class Demo(object):
    """The offline demo on a free port; fresh=True serves an empty working folder instead."""

    def __init__(self, fresh=False):
        self.dir = tempfile.mkdtemp(prefix="bt-shots-")
        if not fresh:
            shutil.copytree(os.path.join(FIXTURES, "trader"), os.path.join(self.dir, "trader"))
        self.srv = serve.make_server("127.0.0.1", 0, self.dir, FIXTURES)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d/" % self.srv.server_address[1]

    def close(self):
        self.srv.stop()
        self.thread.join(5)
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


def run(out, pages, widths, dark=False, fresh=False, browser=None, prefix=""):
    browser = browser or find_browser()
    if not browser:
        return None
    os.makedirs(out, exist_ok=True)
    demo = Demo(fresh=fresh)
    written = []
    try:
        for name in pages:
            for w in widths:
                fname = "%s%s-%d%s.png" % (prefix, name or "today", w, "-dark" if dark else "")
                path = os.path.join(out, fname)
                if os.path.exists(path):
                    os.remove(path)
                if shoot(browser, demo.base + page_hash(name), path, w, HEIGHTS.get(w, 900), dark):
                    written.append(path)
    finally:
        demo.close()
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Screenshots of the local UI (offline demo) with a headless browser.")
    ap.add_argument("--out", default="ui-shots", help="folder for the PNGs (default ./ui-shots)")
    ap.add_argument("--pages", default=",".join(PAGES), help="comma list (default: every page)")
    ap.add_argument("--widths", default="1280,1440,390", help="comma list of widths in px")
    ap.add_argument("--dark", action="store_true", help="dark theme")
    ap.add_argument("--fresh", action="store_true", help="an empty working folder (first-run setup)")
    ap.add_argument("--prefix", default="", help="file name prefix")
    a = ap.parse_args(argv)
    pages = [p.strip() for p in a.pages.split(",") if p.strip()]
    widths = [int(w) for w in a.widths.split(",") if w.strip()]
    got = run(a.out, pages, widths, a.dark, a.fresh, prefix=a.prefix)
    if got is None:
        sys.stderr.write("screenshots.py: no headless browser found (Chrome, Chromium or Edge); set BT_BROWSER\n")
        return 2
    for p in got:
        print(p)
    missing = len(pages) * len(widths) - len(got)
    if missing:
        sys.stderr.write("screenshots.py: %d screenshot(s) failed\n" % missing)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
