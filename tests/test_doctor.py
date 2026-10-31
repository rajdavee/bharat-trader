"""trader-doctor tests, offline.

Fixtures (tests/fixtures/trader-doctor/), each file one recorded answer {url, status, error, elapsed, body}:
  ok/       all 19 sources, recorded live by `doctor.py --record` on Sun 1 Nov 2026 (02:45 IST).
            Trimmed: option chain (8 strikes round the spot), holidays (CM and FO), board meetings,
            PIT (8 rows), deals history (40 rows), fo_mktlots (29 rows), EQUITY_L (every other row
            plus RELIANCE). The uploader's login in NSE attachment names is replaced with "filer".
  changed/  real NSE answers the tools cannot read, replayed for one source each:
            option-chain-v3 for an expiry NSE does not list (HTTP 200, no rows, spot 0) as the chain;
            the 404 page of the retired /api/option-chain-indices as master-quote's answer;
            the {} of the retired /api/option-chain-equities as the results answer.
  down/     NSE's bot filter holding a non-browser request past the 10 s timeout (real), as the
            market status and as Yahoo query2's chart; query1's answer is the ok chart of the same run.
  blocked/  Yahoo query2 spark with a curl User-Agent: HTTP 429 "Edge: Too Many Requests" (real).
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

doc = load("trader-doctor", "doctor")
DIR = os.path.join(FIX, "trader-doctor")
OK, CHANGED, DOWN, BLOCKED = (os.path.join(DIR, x) for x in ("ok", "changed", "down", "blocked"))
SUNDAY = "2026-11-01 10:41"


def report(*dirs, now=SUNDAY, only=None, trader_dir=None):
    return doc.run(only, list(dirs), None, doc.parse_now(now), trader_dir or tempfile.gettempdir())


def rows(rep):
    return {r["id"]: r for r in rep["sources"]}


class Workdir(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="bt-doctor-")
        self.hist = os.path.join(self.d, "trader", "doctor.json")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.d)

    def run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = doc.main(list(args) + ["--history", self.hist])
        return code, out.getvalue(), err.getvalue()

    def history(self):
        with open(self.hist, encoding="utf-8") as fh:
            return json.load(fh)


class Calendar(unittest.TestCase):
    def test_sessions_follow_the_nse_calendar(self):
        D = doc.datetime.date
        T = doc.datetime.datetime
        self.assertEqual(doc.last_session(T(2026, 11, 1, 10, 41)), D(2026, 10, 30))      # Sunday: Friday's
        self.assertEqual(doc.last_session(T(2026, 11, 2, 9, 14)), D(2026, 10, 30))       # before the open
        self.assertEqual(doc.last_session(T(2026, 11, 2, 9, 15)), D(2026, 11, 2))
        self.assertEqual(doc.last_session(T(2026, 11, 10, 12, 0)), D(2026, 11, 9))       # Diwali-Balipratipada
        self.assertEqual(doc.last_session(T(2026, 11, 9, 11, 0)), D(2026, 11, 9))
        self.assertEqual(doc.last_close(T(2026, 11, 2, 15, 29)), D(2026, 10, 30))
        self.assertEqual(doc.last_close(T(2026, 11, 2, 15, 30)), D(2026, 11, 2))
        self.assertEqual(doc.prev_trading_day(D(2026, 10, 21)), D(2026, 10, 19))          # Dussehra on the 20th
        self.assertEqual(doc.market_words(T(2026, 11, 8, 18, 0)), "NSE closed (weekend)")
        self.assertIn("Diwali-Balipratipada", doc.market_words(T(2026, 11, 10, 10, 0)))
        self.assertEqual(doc.to_ist(doc.parse_now("2026-11-01T05:11:00Z")), T(2026, 11, 1, 10, 41))

    def test_nse_dates(self):
        D = doc.datetime.date
        for text in ("30-Oct-2026 15:30", "30-OCT-2026", "30-10-2026", "2026-10-30", "30-Oct-2026 15:40:00"):
            self.assertEqual(doc.nse_day(text), D(2026, 10, 30), text)
        self.assertIsNone(doc.nse_day("Friday"))


class Recorded(unittest.TestCase):
    def test_every_source_ok_on_a_sunday(self):
        rep = report(OK)
        self.assertEqual(len(rep["sources"]), 19)
        self.assertEqual(rep["summary"]["ok"], 19, [(r["id"], r["detail"]) for r in rep["sources"] if r["status"] != "ok"])
        self.assertEqual((rep["market"], rep["last_session"]), ("NSE closed (weekend)", "2026-10-30"))
        r = rows(rep)
        self.assertEqual(r["nse-market-status"]["detail"], "Capital Market Close, trade date 2026-10-30")
        self.assertEqual(r["nse-holidays"]["detail"], "20 CM holidays in 2026, same as the tools' table")
        self.assertEqual(r["nse-contract-info"]["detail"], "18 expiries, next 2026-11-03")
        self.assertIn("expiry=03-Nov-2026", r["nse-option-chain"]["url"])               # from the contract info
        self.assertIn("spot 23053.1", r["nse-option-chain"]["detail"])
        self.assertEqual(r["nse-fno-list"]["detail"], "218 F&O stocks")
        self.assertIn("NIFTY lot 65 from NOV-26", r["nse-fo-mktlots"]["detail"])
        self.assertEqual(r["nse-vix-history"]["data_date"], "2026-10-30")
        self.assertEqual(r["yahoo-chart"]["data_date"], "2026-10-30")
        self.assertIn("from_date=25-10-2026&to_date=01-11-2026", r["nse-announcements"]["url"])
        for row in rep["sources"]:
            self.assertTrue(row["affects"] and row["fallback"], row["id"])

    def test_changed_answers(self):
        with tempfile.TemporaryDirectory() as td:
            rep = report(OK, CHANGED, trader_dir=td)
        r = rows(rep)
        self.assertEqual(rep["summary"]["changed"], 3)
        self.assertEqual(r["nse-option-chain"]["status"], "changed")
        self.assertIn("an empty chain", r["nse-option-chain"]["detail"])
        self.assertEqual(r["nse-fno-list"]["detail"], "HTTP 404: the address is gone")
        self.assertEqual(r["nse-fno-list"]["fallback"],
                         "no cache: every stock is treated as an F&O stock (the earlier close)")
        self.assertIn("is not a list of rows", r["nse-results"]["detail"])

    def test_the_fallback_in_use_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "fno_stocks.json"), "w", encoding="utf-8") as fh:
                json.dump({"fetched": "2026-10-30", "symbols": ["RELIANCE", "TCS"]}, fh)
            rep = report(OK, CHANGED, trader_dir=td)
        self.assertEqual(rows(rep)["nse-fno-list"]["fallback"], "using the cached list of 2026-10-30 (2 stocks)")

    def test_down_and_the_query1_fallback(self):
        r = rows(report(OK, DOWN))
        self.assertEqual(r["nse-market-status"]["status"], "down")
        self.assertEqual(r["nse-market-status"]["detail"], "no answer: timed out after 10 s")
        self.assertEqual(r["nse-market-status"]["fallback"], "the built-in NSE calendar (the tools' own clock)")
        self.assertEqual(r["yahoo-chart"]["status"], "down")
        self.assertEqual(r["yahoo-chart"]["fallback_used"], "query1: answering")
        self.assertIn("query1 when query2 fails", r["yahoo-chart"]["fallback"])

    def test_blocked(self):
        rep = report(OK, BLOCKED)
        r = rows(rep)["yahoo-spark"]
        self.assertEqual((r["status"], r["http"]), ("blocked", 429))
        self.assertEqual(r["detail"], "HTTP 429: too many requests")
        self.assertEqual(rep["summary"]["failed"], 1)

    def test_stale_on_a_trading_day_ok_early_and_on_holidays(self):
        rep = report(OK, now="2026-11-03 11:00")                     # Tuesday: Friday's answers are old
        stale = sorted(k for k, v in rows(rep).items() if v["status"] == "changed")
        self.assertEqual(stale, ["nse-market-status", "nse-option-chain", "yahoo-chart", "yahoo-chart-index",
                                 "yahoo-spark"])
        self.assertEqual(rows(rep)["yahoo-chart"]["detail"],
                         "stale: the last trade is from Fri 2026-10-30, expected Tue 2026-11-03")
        self.assertEqual(rows(rep)["yahoo-chart"]["fallback"], "none: query1 sends the same answer")
        # end-of-day files may lag one session: Friday's bulk deals and VIX are fine on Tuesday morning
        self.assertEqual(rows(rep)["nse-bulk"]["status"], "ok")
        self.assertEqual(rows(rep)["nse-vix-history"]["status"], "ok")
        self.assertEqual(report(OK, now="2026-11-02 09:20")["summary"]["ok"], 19)   # first half hour
        self.assertEqual(report(OK, now="2026-10-31 20:00")["summary"]["ok"], 19)   # Saturday
        late = rows(report(OK, now="2026-11-04 10:00"))
        self.assertIn("stale", late["nse-vix-history"]["detail"])                   # two sessions behind

    def test_only_and_missing_answers(self):
        self.assertEqual({r["group"] for r in report(OK, only="yahoo")["sources"]}, {"yahoo"})
        self.assertEqual(len(report(OK, only="nse")["sources"]), 15)
        rep = report(BLOCKED)                                      # one file: the rest were not recorded
        self.assertEqual(rep["summary"]["skipped"], 18)
        self.assertEqual(rows(rep)["nse-option-chain"]["detail"], "not tried: needs the expiry from the contract info")
        self.assertEqual(rows(rep)["nse-bulk"]["detail"], "no recorded answer in the offline folder")


class Checks(unittest.TestCase):
    def setUp(self):
        self.ctx = {"now_ist": doc.datetime.datetime(2026, 11, 1, 10, 41), "today": doc.datetime.date(2026, 11, 1),
                    "session_allowed": [doc.datetime.date(2026, 10, 30)],
                    "eod_allowed": [doc.datetime.date(2026, 10, 30), doc.datetime.date(2026, 10, 29)]}

    def body(self, sid):
        return doc.Recorded([OK]).get(sid).text()

    def test_units_and_fields(self):
        ms = json.loads(self.body("nse-market-status"))
        ms["marketState"][0]["last"] = 230.53                       # NIFTY in hundreds: a unit change
        with self.assertRaises(doc.Changed) as cm:
            doc.ck_market_status(json.dumps(ms), self.ctx)
        self.assertIn("NIFTY 50 last is 230.53, outside 5000 to 100000", str(cm.exception))
        vix = json.loads(self.body("nse-vix-history"))
        del vix["data"][-1]["EOD_CLOSE_INDEX_VAL"]
        with self.assertRaises(doc.Changed) as cm:
            doc.ck_vix(json.dumps(vix), self.ctx)
        self.assertIn("has no EOD_CLOSE_INDEX_VAL", str(cm.exception))
        lots = self.body("nse-fo-mktlots").replace("UNDERLYING", "UNDERLYING_NAME", 1)
        with self.assertRaises(doc.Changed):
            doc.ck_fo_mktlots(lots, self.ctx)

    def test_holiday_table_drift(self):
        text = self.body("nse-holidays")
        saved = dict(doc.NSE_HOLIDAYS)
        try:
            del doc.NSE_HOLIDAYS["2026-12-25"]
            doc.NSE_HOLIDAYS["2026-12-24"] = "made up"
            with self.assertRaises(doc.Changed) as cm:
                doc.ck_holidays(text, self.ctx)
            self.assertIn("NSE adds 2026-12-25 NSE drops 2026-12-24", str(cm.exception))
        finally:
            doc.NSE_HOLIDAYS.clear()
            doc.NSE_HOLIDAYS.update(saved)
        ctx = dict(self.ctx, now_ist=doc.datetime.datetime(2027, 1, 4, 10, 0))
        with self.assertRaises(doc.Changed) as cm:
            doc.ck_holidays(text, ctx)
        self.assertIn("no 2027 holiday table", str(cm.exception))

    def test_transport(self):
        A = doc.Answer
        self.assertEqual(doc.classify(A("u", 200, b"<!DOCTYPE html><html>Access Denied</html>"), "json")[0], "blocked")
        self.assertIsNone(doc.classify(A("u", 200, b'<?xml version="1.0"?><rss/>'), "xml"))
        self.assertEqual(doc.classify(A("u", 503, b""), "json"), ("down", "HTTP 503"))
        self.assertEqual(doc.classify(A("u", 403, b""), "json")[0], "blocked")
        self.assertEqual(doc.classify(A("u", None, b"", error="timed out after 10 s"), "json")[0], "down")


class Live(unittest.TestCase):
    """The network path with a fake transport: politeness, headers, the back-off."""

    def setUp(self):
        self.sleeps, self.calls = [], []
        self.saved = (doc.SLEEP, doc.http_get)
        doc.SLEEP = self.sleeps.append
        ok = doc.Recorded([OK])

        def fake(url, nse, timeout=doc.TIMEOUT):
            self.calls.append((url, nse))
            if nse and "marketStatus" in url:
                return doc.Answer(url, 403, b"", 0.2)
            sid = next(s[0] for s in doc.SOURCES if s[3]({"today": doc.datetime.date(2026, 11, 1),
                                                           "nifty_expiry": doc.datetime.date(2026, 11, 3)}) == url)
            return ok.get(sid)
        doc.http_get = fake

    def tearDown(self):
        doc.SLEEP, doc.http_get = self.saved

    def test_nse_block_backs_off_then_stops_asking_nse(self):
        rep = doc.run(None, None, None, doc.parse_now(SUNDAY), tempfile.gettempdir())
        r = rows(rep)
        self.assertEqual(r["nse-market-status"]["status"], "blocked")
        self.assertEqual([u for u, _ in self.calls].count(doc.NSE + "marketStatus"), 2)    # one retry
        self.assertIn(doc.BACKOFF, self.sleeps)
        self.assertEqual(r["nse-equity-list"]["status"], "ok")
        nse_after = [k for k in r if k.startswith("nse-") and k not in ("nse-equity-list", "nse-market-status")]
        for k in nse_after:
            self.assertEqual(r[k]["status"], "skipped", k)
            if k != "nse-option-chain":                     # that one also waits for the contract info
                self.assertIn("NSE blocked NSE market status twice", r[k]["detail"])
        self.assertEqual({r[k]["status"] for k in r if k.startswith("yahoo-")}, {"ok"})
        self.assertTrue(all(s in (doc.PAUSE["nse"], doc.PAUSE["yahoo"], doc.BACKOFF) for s in self.sleeps))
        self.assertTrue(all(nse == ("nseindia" in u) for u, nse in self.calls))

    def test_record_then_replay(self):
        d = tempfile.mkdtemp(prefix="bt-doctor-rec-")
        try:
            doc.run("yahoo", None, d, doc.parse_now(SUNDAY), d)
            self.assertEqual(sorted(os.listdir(d)), ["yahoo-chart-index.json", "yahoo-chart.json", "yahoo-rss.json",
                                                     "yahoo-spark.json"])
            self.assertEqual(report(d, only="yahoo")["summary"]["ok"], 4)
        finally:
            shutil.rmtree(d)


class Headers(unittest.TestCase):
    def test_nse_gets_a_browser_user_agent_and_the_referer(self):
        seen = []

        class Resp(object):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"[]"
        saved = doc.urllib.request.urlopen
        doc.urllib.request.urlopen = lambda req, timeout=None: seen.append((req, timeout)) or Resp()
        try:
            doc.http_get(doc.NSE + "master-quote", True)
            doc.http_get("https://query2.finance.yahoo.com/v8/finance/chart/%5ENSEI", False)
        finally:
            doc.urllib.request.urlopen = saved
        nse, yahoo = seen[0][0], seen[1][0]
        self.assertIn("Chrome/130", nse.get_header("User-agent"))
        self.assertEqual(nse.get_header("Referer"), "https://www.nseindia.com/")
        self.assertEqual(nse.get_header("Accept"), "application/json")
        self.assertIn("bharat-trader", yahoo.get_header("User-agent"))
        self.assertIsNone(yahoo.get_header("Referer"))
        self.assertEqual(seen[0][1], doc.TIMEOUT)


class Cli(unittest.TestCase):
    def test_table_json_exit_codes_and_history(self):
        with Workdir() as w:
            code, out, _ = w.run("--offline", OK, "--now", SUNDAY)
            self.assertEqual(code, 0)
            self.assertIn("Data self-test, Sun 2026-11-01 10:41 IST. NSE closed (weekend); fresh data is from "
                          "Fri 2026-10-30 or later.", out)
            self.assertIn("19 ok, 0 slow, 0 changed, 0 down, 0 blocked.", out)
            self.assertRegex(out, r"NSE option chain v3 \(NIFTY\) +ok 0\.\ds +2026-11-01 10:41")
            first = w.history()
            self.assertEqual(first["schema"], "bharat-trader/doctor/1")
            # an hour later the market status is down: since = now, last ok = the first run
            code, out, _ = w.run("--offline", OK, "--offline", DOWN, "--now", "2026-11-01 11:41")
            self.assertEqual(code, 1)
            self.assertIn("no answer: timed out after 10 s; last ok 2026-11-01 10:41", out)
            # still down later: since stays; the ok sources keep their first since
            code, out, _ = w.run("--offline", OK, "--offline", DOWN, "--now", "2026-11-01 12:41", "--json")
            self.assertEqual(code, 1)
            j = json.loads(out)
            r = rows(j)
            self.assertEqual(r["nse-market-status"]["since"], "2026-11-01T11:41:00+05:30")
            self.assertEqual(r["nse-market-status"]["last_ok"], "2026-11-01T10:41:00+05:30")
            self.assertEqual(r["nse-holidays"]["since"], "2026-11-01T10:41:00+05:30")
            self.assertEqual(j["exit_code"], 1)
            h = w.history()
            self.assertEqual(len(h["runs"]), 3)
            self.assertEqual(h["sources"]["nse-market-status"]["status"], "down")
            # --no-write reads the history and leaves it alone
            code, out, _ = w.run("--offline", OK, "--now", "2026-11-01 13:41", "--no-write", "--json")
            self.assertEqual(rows(json.loads(out))["nse-market-status"]["since"], "2026-11-01T13:41:00+05:30")
            self.assertEqual(len(w.history()["runs"]), 3)
            for i in range(doc.KEEP_RUNS + 2):
                w.run("--offline", OK, "--now", SUNDAY, "--only", "yahoo")
            self.assertEqual(len(w.history()["runs"]), doc.KEEP_RUNS)

    def test_usage_errors(self):
        with Workdir() as w:
            self.assertEqual(w.run("--offline", OK, "--record", w.d)[0], 2)
            self.assertEqual(w.run("--offline", os.path.join(DIR, "nope"))[0], 2)
            self.assertEqual(w.run("--offline", OK, "--now", "tomorrow")[0], 2)


class Ui(unittest.TestCase):
    """ui/api.py: GET /api/doctor reads the history; POST /api/doctor/run runs the self-test."""

    def test_routes(self):
        api = load("ui", "api")
        d = tempfile.mkdtemp(prefix="bt-doctor-ui-")
        try:
            off = os.path.join(d, "offline")
            shutil.copytree(OK, os.path.join(off, "doctor"))
            shutil.copy(os.path.join(DOWN, "nse-market-status.json"), os.path.join(off, "doctor"))
            ctx = api.Context(os.path.join(d, "work"), offline=off, now=SUNDAY)
            status, body = api.dispatch(ctx, "GET", "/api/doctor")
            self.assertEqual((status, body["checked"]), (200, False))
            status, body = api.dispatch(ctx, "POST", "/api/doctor/run", {}, {})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["checked"])
            self.assertEqual([r["id"] for r in body["failing"]], ["nse-market-status"])
            self.assertEqual(body["summary"]["down"], 1)
            self.assertTrue(os.path.isfile(os.path.join(d, "work", "trader", "doctor.json")))
            status, again = api.dispatch(ctx, "GET", "/api/doctor")
            self.assertEqual(again["checked_at"], body["checked_at"])
            self.assertEqual(api.dispatch(ctx, "POST", "/api/doctor/run", {}, {"only": "bse"})[0], 400)
            bare = api.Context(os.path.join(d, "work"), offline=os.path.join(d, "work"), now=SUNDAY)
            self.assertEqual(api.dispatch(bare, "POST", "/api/doctor/run", {}, {})[0], 409)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
