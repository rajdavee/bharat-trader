"""Local notifications (trader-alerts/notify/), offline: routing, toggles, rate limits, quiet hours, the desktop
commands per platform, and a stub webhook server on 127.0.0.1 that records what it receives (so the tests can
check that no secret and no position list ever leaves the machine)."""

import contextlib
import datetime
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

alerts = load("trader-alerts", "alerts")
nt = alerts.notify_api()
FAKE_SECRET = "plainfakevalue-for-tests"       # not a real credential: the redaction is what is tested
T0 = datetime.datetime(2026, 11, 2, 10, 0)


class Hook(http.server.BaseHTTPRequestHandler):
    got = []
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Hook.got.append({"path": self.path, "type": self.headers.get("Content-Type"),
                         "body": json.loads(self.rfile.read(n).decode("utf-8"))})
        self.send_response(Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/push/topic" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-notify-")
        os.makedirs(os.path.join(self.dir, "trader"))
        Hook.got, Hook.status = [], 200
        self.ran = []
        nt.RUN = self.ran.append
        self.env = {"BT_NOTIFY_WEBHOOK": self.url, "BT_TEST_API_SECRET": FAKE_SECRET}

    def tearDown(self):
        nt.RUN = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def config(self, **notify):
        cfg = {"capital": 100000, "notify": notify}
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return cfg

    def send(self, event="autopilot_fill", text="AP-1 filled on paper: BUY SBIN at 812.40", at=T0, key=None, **kw):
        return nt.send(self.dir, event, "Title", text, now=at, key=key, env=self.env, **kw)

    def files(self):
        return sorted(os.listdir(os.path.join(self.dir, "trader")))


class Settings(Base):
    def test_off_by_default_and_writes_nothing(self):
        r = self.send()
        self.assertFalse(r["sent"])
        self.assertIn("off", r["why"])
        self.assertEqual((self.ran, Hook.got), ([], []))
        self.assertEqual(self.files(), [])             # not even a log: off means off

    def test_validation(self):
        for bad in ({"enabled": "yes"}, {"nope": 1}, {"quiet_hours": ["22:00"]}, {"quiet_hours": ["25:00", "07:00"]},
                    {"events": {"made_up": True}}, {"min_gap_min": -1}, {"max_per_hour": 0}):
            with self.assertRaises(nt.NotifyError, msg=bad):
                nt.settings_of({"notify": bad})
        s = nt.settings_of({"notify": {"enabled": True, "events": {"kill": False}}})
        self.assertFalse(s["events"]["kill"])
        self.assertTrue(s["events"]["autopilot_fill"])
        with self.assertRaises(nt.NotifyError):
            nt.send(self.dir, "not_an_event", "t", "x", cfg={})

    def test_quiet_hours_wrap_midnight(self):
        q = ["22:00", "07:00"]
        self.assertTrue(nt.in_quiet_hours(q, datetime.datetime(2026, 11, 2, 23, 30)))
        self.assertTrue(nt.in_quiet_hours(q, datetime.datetime(2026, 11, 2, 6, 59)))
        self.assertFalse(nt.in_quiet_hours(q, datetime.datetime(2026, 11, 2, 7, 0)))
        self.assertTrue(nt.in_quiet_hours(["12:00", "13:00"], datetime.datetime(2026, 11, 2, 12, 30)))
        self.assertFalse(nt.in_quiet_hours(None, T0))


class Routing(Base):
    def test_desktop_and_webhook(self):
        self.config(enabled=True, desktop=True, webhook=True)
        r = self.send()
        self.assertTrue(r["sent"])
        self.assertEqual(r["channels"], {"desktop": "sent", "webhook": "sent"})
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(len(Hook.got), 1)
        got = Hook.got[0]
        self.assertEqual((got["path"], got["type"]), ("/push/topic", "application/json"))
        self.assertEqual(sorted(got["body"]), ["at", "event", "source", "text", "title"])
        self.assertEqual(got["body"]["event"], "autopilot_fill")
        self.assertEqual(got["body"]["at"], "2026-11-02T10:00:00+05:30")
        with open(os.path.join(self.dir, "trader", "notify", "log.jsonl"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(self.url, log)                 # the URL is never written
        self.assertNotIn("127.0.0.1", log)

    def test_event_toggle(self):
        self.config(enabled=True, events={"autopilot_fill": False})
        r = self.send()
        self.assertFalse(r["sent"])
        self.assertIn("switched off", r["why"])
        self.assertTrue(self.send("autopilot_exit")["sent"])

    def test_quiet_hours_suppress(self):
        self.config(enabled=True, webhook=True, quiet_hours=["09:30", "11:00"])
        r = self.send()
        self.assertFalse(r["sent"])
        self.assertIn("quiet hours", r["why"])
        self.assertEqual((self.ran, Hook.got), ([], []))
        self.assertTrue(self.send(at=T0.replace(hour=11, minute=5))["sent"])

    def test_rate_limit_per_event_and_folding(self):
        self.config(enabled=True, min_gap_min=5)
        self.assertTrue(self.send()["sent"])
        r = self.send(at=T0 + datetime.timedelta(minutes=2))
        self.assertFalse(r["sent"])
        self.assertIn("rate limit", r["why"])
        self.assertFalse(self.send(at=T0 + datetime.timedelta(minutes=3))["sent"])
        self.assertTrue(self.send("autopilot_exit", at=T0 + datetime.timedelta(minutes=3))["sent"])   # another event
        r = self.send(at=T0 + datetime.timedelta(minutes=6))
        self.assertTrue(r["sent"])
        self.assertIn("+2 more held back", r["payload"]["text"])

    def test_max_per_hour(self):
        self.config(enabled=True, min_gap_min=0, max_per_hour=3)
        sent = [self.send(at=T0 + datetime.timedelta(minutes=i))["sent"] for i in range(5)]
        self.assertEqual(sent, [True, True, True, False, False])
        self.assertTrue(self.send(at=T0 + datetime.timedelta(minutes=61))["sent"])

    def test_same_key_once_a_day(self):
        self.config(enabled=True, min_gap_min=0)
        self.assertTrue(self.send("risk_lock", key="lock:a")["sent"])
        r = self.send("risk_lock", key="lock:a", at=T0 + datetime.timedelta(minutes=30))
        self.assertFalse(r["sent"])
        self.assertIn("already told today", r["why"])
        self.assertTrue(self.send("risk_lock", key="lock:a", at=T0 + datetime.timedelta(days=1))["sent"])

    def test_webhook_failures_are_reported_not_raised(self):
        self.config(enabled=True, desktop=False, webhook=True)
        Hook.status = 500
        r = self.send()
        self.assertFalse(r["sent"])
        self.assertIn("HTTP 500", r["channels"]["webhook"])
        r = nt.send(self.dir, "kill", "t", "x", now=T0, env={})
        self.assertIn("BT_NOTIFY_WEBHOOK is not set", r["channels"]["webhook"])
        r = nt.send(self.dir, "kill", "t", "x", now=T0 + datetime.timedelta(minutes=9), env={"BT_NOTIFY_WEBHOOK": "file:///etc/passwd"})
        self.assertIn("not an http(s) URL", r["channels"]["webhook"])

    def test_test_command_and_channel_override(self):
        r = nt.test(self.dir, now=T0, env=self.env)
        self.assertFalse(r["sent"])                      # off: the configured test sends nothing
        r = nt.test(self.dir, now=T0, channel="webhook", env=self.env)
        self.assertTrue(r["sent"])                       # one channel you asked for, even while off
        self.assertEqual(Hook.got[-1]["body"]["event"], "test")
        self.assertEqual(self.ran, [])
        st = nt.status(self.dir, env=self.env)
        self.assertTrue(st["webhook_set"])
        self.assertEqual(st["webhook_host"], "127.0.0.1")
        self.assertNotIn(self.url, json.dumps(st))

    def test_command_line(self):
        self.config(enabled=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = alerts.main(["notify", "--workdir", self.dir, "status"])
        self.assertEqual(rc, 0)
        self.assertIn("Notifications: ON", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = alerts.main(["notify", "--workdir", self.dir, "--json", "test", "--channel", "desktop"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["sent"])


class NoSecrets(Base):
    def test_payload_redacts_secrets_and_tokens(self):
        self.config(enabled=True, desktop=True, webhook=True)
        text = ("Kill switch on. debug: key %s, header Bearer abcDEF123456ghiJKL789mno, " % FAKE_SECRET) + "x" * 400
        r = self.send("kill", text)
        body = json.dumps(Hook.got[0]["body"])
        self.assertNotIn(FAKE_SECRET, body)
        self.assertNotIn("abcDEF123456ghiJKL789mno", body)
        self.assertNotIn(self.url, body)
        self.assertIn("[redacted]", body)
        self.assertLessEqual(len(Hook.got[0]["body"]["text"]), nt.MAX_TEXT)
        self.assertNotIn(FAKE_SECRET, " ".join(self.ran[0]))
        self.assertTrue(r["sent"])

    def test_plain_words_survive(self):
        s = nt.redact("orb15-vwap SBIN (WATCH, 58%) at ₹1,23,456.50, candidate 2026-07-06-t-dip-live-AXISBANK", env={})
        self.assertNotIn("[redacted]", s)

    def test_hooked_skills_send_short_summaries(self):
        """The co-trader's candidate notification: counts, symbols, tiers and odds; never the plan's prices, the
        quantity or the account."""
        ct = load("trader-cotrader", "cotrader")
        self.config(enabled=True, webhook=True, desktop=False)
        os.environ["BT_NOTIFY_WEBHOOK"] = self.url
        try:
            doc = {"as_of": "2026-11-02 10:00", "candidates": [{
                "id": "2026-11-02-orb15-vwap-SBIN", "date": "2026-11-02", "signal_date": "2026-11-02",
                "strategy": "orb15-vwap", "spec_hash": "e51ea86b214ec56a", "tier": "WATCH", "symbol": "SBIN",
                "timeframe": "5m", "status": "ready", "max_bars": None,
                "evidence": {"p": 0.55, "p_lo": 0.4, "p_hi": 0.7, "ev_r": 0.05, "trades_cell": 30, "trades_all": 60,
                             "cell": "trend|mid"},
                "second_opinion": {"available": False},
                "plan": {"entry": 812.4, "stop": 806.1, "target": 825.0, "entry_time": "09:35", "flat_by": "15:10"},
                "sizing": {"qty": 37}}]}
            ct.save(self.dir, doc)
        finally:
            del os.environ["BT_NOTIFY_WEBHOOK"]
        self.assertEqual(len(Hook.got), 1)
        b = Hook.got[0]["body"]
        self.assertEqual(b["event"], "cotrader_candidate")
        self.assertIn("SBIN orb15-vwap (WATCH, 55%)", b["text"])
        for leak in ("812.4", "806.1", "37"):
            self.assertNotIn(leak, b["text"])

    def test_risk_kill_notifies_through_the_standard_layout(self):
        risk = load("trader-risk", "check")
        self.config(enabled=True, desktop=True)
        tdir = os.path.join(self.dir, "trader")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = risk.main(["kill", "--config", os.path.join(tdir, "config.json"), "--kill",
                            os.path.join(tdir, "state", "kill.json"), "--risk", os.path.join(tdir, "state", "risk.json"),
                            "--now", "2026-11-02 10:00", "--reason", "testing"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.ran), 1)
        self.assertIn("Kill switch", " ".join(self.ran[0]))


class Desktop(unittest.TestCase):
    def test_each_platform(self):
        mac = nt.desktop_argv("Title", 'a "quoted" $x text', "darwin")
        self.assertEqual(mac[0], "osascript")
        self.assertNotIn('"quoted"', mac[2])
        self.assertNotIn("$x", mac[2])
        lin = nt.desktop_argv("Title", "text", "linux")
        self.assertEqual(lin[0], "notify-send")
        win = nt.desktop_argv("Title", "it's", "win32")
        self.assertEqual(win[0], "powershell")
        self.assertIn("'it''s'", win[-1])                # PowerShell single-quote escaping
        self.assertIsNone(nt.desktop_argv("t", "x", "sunos5"))

    def test_missing_tool_is_not_an_error(self):
        def boom(argv):
            raise FileNotFoundError(argv[0])
        nt.RUN = boom
        try:
            self.assertIn("failed", nt.send_desktop("t", "x"))
        finally:
            nt.RUN = None


if __name__ == "__main__":
    unittest.main()
