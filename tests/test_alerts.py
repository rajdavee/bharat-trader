import datetime
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; bharat-trader/0.3; +https://github.com/rajdavee/bharat-trader)"

# NSE Capital Market trading holidays 2026, from NSE's holiday master (key "CM")
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
            "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
            "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
            "2026-11-24", "2026-12-25"]
MUHURAT = {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00", "name": "Muhurat trading"}]}


def ist(text):
    return alerts.parse_now(text)


def st(text, cfg=None):
    return alerts.market_status(ist(text), alerts.Calendar(cfg))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Money(unittest.TestCase):
    def test_inr_indian_grouping(self):
        self.assertEqual(alerts.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(alerts.inr(1167.4), "₹1,167.40")
        self.assertEqual(alerts.inr(100), "₹100.00")
        self.assertEqual(alerts.inr(10000000), "₹1,00,00,000.00")
        self.assertEqual(alerts.inr(-98765.4321), "-₹98,765.43")
        self.assertEqual(alerts.money("^NSEI", 22936.75), "22,936.75")
        self.assertEqual(alerts.money("SBIN", 971.9), "₹971.90")


class Calendar(unittest.TestCase):
    def test_holidays_match_nse(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        with open(os.path.join(ROOT, "tests", "fixtures", "trader-alerts", "nse-marketStatus.json")) as fh:
            self.assertIn("marketState", json.load(fh))

    def test_ist_has_no_daylight_saving(self):
        self.assertEqual(ist("2026-01-15T09:15"), DT(2026, 1, 15, 3, 45))
        self.assertEqual(ist("2026-07-15T09:15"), DT(2026, 7, 15, 3, 45))
        self.assertEqual(ist("2026-10-29T03:45:00Z"), DT(2026, 10, 29, 3, 45))
        self.assertEqual(ist("2026-10-29T09:15:00+05:30"), DT(2026, 10, 29, 3, 45))
        self.assertEqual(alerts.to_ist(DT(2026, 10, 29, 10, 0)), DT(2026, 10, 29, 15, 30))
        self.assertEqual(alerts.ist_iso(DT(2026, 10, 29, 4, 45)), "2026-10-29T10:15:00+05:30")
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_normal_session_edges(self):
        self.assertEqual(st("2026-10-29 08:59")["phase"], "closed")
        self.assertEqual(st("2026-10-29 08:59")["reason"], "before the pre-open")
        pre = st("2026-10-29 09:00")
        self.assertEqual((pre["open"], pre["phase"]), (False, "pre-open"))
        self.assertIn("order entry until 09:10", pre["reason"])      # revised pre-open from 2026-09-07
        self.assertIn("order entry until 09:08", st("2026-08-20 09:00")["reason"])
        self.assertEqual(pre["next_open"], "2026-10-29 09:15")
        self.assertEqual(st("2026-10-29 09:14")["phase"], "pre-open")
        self.assertTrue(st("2026-10-29 09:15")["open"])
        self.assertEqual(st("2026-10-29 09:15")["reason"], "normal session")
        self.assertTrue(st("2026-10-29 15:29")["open"])
        after = st("2026-10-29 15:30")
        self.assertFalse(after["open"])
        self.assertEqual(after["reason"], "after the close")
        self.assertEqual(after["next_open"], "2026-10-30 09:15")
        self.assertEqual((after["opens"], after["closes"], after["pre_open"]), ("09:15", "15:30", "09:00"))

    def test_weekends_and_holidays(self):
        sat = st("2026-10-31 10:00")
        self.assertEqual((sat["open"], sat["reason"]), (False, "weekend"))
        self.assertEqual(sat["next_open"], "2026-11-02 09:15")
        dus = st("2026-10-20 10:00")                         # Tuesday, Dussehra
        self.assertFalse(dus["open"])
        self.assertEqual(dus["reason"], "NSE holiday: Dussehra")
        self.assertEqual(dus["next_open"], "2026-10-21 09:15")
        self.assertEqual(st("2026-10-19 15:45")["next_open"], "2026-10-21 09:15")   # skips the holiday
        self.assertFalse(st("2026-01-26 11:00")["open"])     # Republic Day
        self.assertEqual(st("2026-11-09 15:31")["next_open"], "2026-11-11 09:15")   # Balipratipada on the 10th
        self.assertEqual(st("2026-12-24 16:00")["next_open"], "2026-12-28 09:15")   # Christmas, then the weekend

    def test_extra_holidays_and_special_sessions(self):
        cfg = {"extra_holidays": ["2026-10-30"]}
        self.assertIn("your config", st("2026-10-30 10:00", cfg)["reason"])
        self.assertEqual(st("2026-10-29 16:00", cfg)["next_open"], "2026-11-02 09:15")
        # Muhurat trading on Sunday 8 Nov (a listed holiday): only with the session in the config
        self.assertFalse(st("2026-11-08 18:30")["open"])
        m = st("2026-11-08 18:30", MUHURAT)
        self.assertTrue(m["open"])
        self.assertEqual((m["reason"], m["opens"], m["closes"], m["pre_open"]),
                         ("Muhurat trading", "18:00", "19:00", "17:45"))
        self.assertEqual(st("2026-11-08 17:50", MUHURAT)["phase"], "pre-open")
        self.assertFalse(st("2026-11-08 19:00", MUHURAT)["open"])
        self.assertEqual(st("2026-11-07 10:00", MUHURAT)["next_open"], "2026-11-08 18:00")
        with self.assertRaises(alerts.AlertError):
            alerts.Calendar({"special_sessions": [{"date": "2026-11-08", "open": "19:00", "close": "18:00"}]})
        with self.assertRaises(alerts.AlertError):
            alerts.Calendar({"extra_holidays": ["next friday"]})

    def test_unknown_year_warns_and_treats_weekdays_as_open(self):
        s = st("2027-01-26 10:00")                            # Republic Day 2027, list not published yet
        self.assertFalse(s["calendar_known"])
        self.assertTrue(s["open"])
        self.assertIn("2027 holiday list is not built in", s["warning"])
        self.assertTrue(st("2026-10-29 10:00")["calendar_known"])
        self.assertNotIn("warning", st("2026-10-29 10:00"))
        self.assertFalse(st("2027-01-26 10:00", {"extra_holidays": ["2027-01-26"]})["open"])

    def test_nse_market_status(self):
        with open(os.path.join(QDIR, "nse-marketStatus.json")) as fh:
            s = alerts.parse_nse_status(fh.read())
        self.assertEqual(s, {"status": "Closed", "message": "Normal Market has Closed",
                             "trade_date": "29-Oct-2026 15:30", "index": "NIFTY 50", "last": 22936.75,
                             "pct": 1.05, "open": False})
        with self.assertRaises(alerts.AlertError):
            alerts.parse_nse_status("<html>Access Denied</html>")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        q, errors = alerts.get_quotes(["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "M&M", "^NSEI"], QDIR)
        self.assertEqual(errors, [])
        r = q["RELIANCE"]
        self.assertEqual((r["price"], r["prev_close"], r["open"], r["currency"]), (1167.4, 1158.0, 1159.2, "INR"))
        self.assertEqual(r["time"], "2026-10-29T15:29:03+05:30")
        self.assertEqual(q["HDFCBANK"]["price"], 683.3)
        self.assertEqual(q["M&M"]["price"], 2890.0)
        self.assertEqual(q["^NSEI"]["price"], 22936.75)

    def test_symbols_and_urls(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("M&M"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", alerts.yahoo_url("BAJAJ-AUTO"))
        self.assertIn("/chart/%5ENSEI?", alerts.yahoo_url("^NSEI"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("TCS", alerts.YAHOO_HOSTS[1]))
        self.assertEqual(alerts.normalise("sbin.ns"), "SBIN")
        self.assertEqual(alerts.split_symbols(["RELIANCE,TCS", "infy"]), ["RELIANCE", "TCS", "INFY"])
        with self.assertRaises(alerts.AlertError):
            alerts.normalise("RELI ANCE")

    def test_offline_errors(self):
        q, errors = alerts.get_quotes(["TCS", "NOSUCHSTOCK", "NOPE", "BAD!"], QDIR)
        self.assertEqual(sorted(q), ["TCS"])
        self.assertEqual(len(errors), 3)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline file", errors[1])
        self.assertIn("not an NSE symbol", errors[2])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-SBIN.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-SBIN.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["SBIN"], d)
            self.assertEqual((q["SBIN"]["price"], errors), (971.9, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent"), req.get_header("Referer")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes, body=None):
        f = FakeNet(codes, body)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [c[0].split(".")[0] for c in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1167.4, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA, None)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1167.4)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["TCS"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "TCS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])

    def test_nse_status_uses_browser_headers(self):
        with open(os.path.join(QDIR, "nse-marketStatus.json")) as fh:
            f = self.net([429, 200], fh.read())
        s = alerts.nse_status()
        self.assertEqual(s["status"], "Closed")
        self.assertEqual(f.calls[0], ("www.nseindia.com", alerts.NSE_UA, "https://www.nseindia.com/"))
        self.assertEqual(f.sleeps, [alerts.BACKOFF])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "TCS", "HDFCBANK", "SBIN", "^NSEI"], QDIR)
        self.now = ist("2026-10-29 10:00")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None, cfg=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes,
                               alerts.market_status(now, alerts.Calendar(cfg)), state or {}, now)

    def test_above_rings_once(self):
        rules = [{"id": "rel-1165", "symbol": "RELIANCE", "type": "above", "price": 1165}]
        rings, s = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "RELIANCE is ₹1,167.40: at or above your alert level ₹1,165.00.")
        self.assertEqual(s["rung"]["rel-1165"], "2026-10-29T10:00:00+05:30")
        for later in ("2026-10-29 10:05", "2026-10-29 14:00", "2026-10-30 10:00"):
            self.assertEqual(self.at(later, rules, s)[0], [])     # once, until reset

    def test_below_with_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK", "type": "stop", "price": 684, "rearm": True}]
        rings, s = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK is ₹683.30: at or below your stop ₹684.00", rings[0]["message"])
        self.q["HDFCBANK"]["price"] = 690.0
        rings, s = alerts.evaluate(rules, self.q, self.open, s, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", s["rung"])
        self.q["HDFCBANK"]["price"] = 680.0
        self.assertEqual(len(alerts.evaluate(rules, self.q, self.open, s, self.now)[0]), 1)

    def test_nifty_index_as_a_symbol(self):
        rules = [{"id": "nifty", "symbol": "^NSEI", "type": "above", "price": 22900}]
        rings, _ = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "^NSEI is 22,936.75: at or above your alert level 22,900.00.")

    def test_pct_move_from_previous_close(self):
        big = [{"id": "sbin-2", "symbol": "SBIN", "type": "pct_move", "pct": 2}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])   # +1.58%
        small = [{"id": "sbin-1.5", "symbol": "SBIN", "type": "pct_move", "pct": 1.5, "direction": "up"}]
        rings, s = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "SBIN moved +1.58% to ₹971.90 (from ₹956.80).")
        self.assertEqual(self.at("2026-10-29 11:00", small, s)[0], [])                   # once a day
        self.assertEqual(len(self.at("2026-10-30 10:00", small, s)[0]), 1)              # next day again
        down = [{"id": "sbin-dn", "symbol": "SBIN", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        hdfc = [{"id": "h", "symbol": "HDFCBANK", "type": "pct_move", "pct": 0.25, "direction": "down"}]
        self.assertIn("-0.29%", alerts.evaluate(hdfc, self.q, self.open, {}, self.now)[0][0]["message"])

    def test_opening_bell_at_0915_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-10-29 09:14", rules)[0], [])
        rings, s = self.at("2026-10-29 09:15", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: NSE is open (09:15 IST, normal session 09:15-15:30).")
        self.assertEqual(self.at("2026-10-29 11:00", rules, s)[0], [])
        self.assertEqual(self.at("2026-10-31 10:00", rules, s)[0], [])     # Saturday
        self.assertEqual(self.at("2026-11-10 10:00", rules, s)[0], [])     # Diwali-Balipratipada
        self.assertEqual(len(self.at("2026-10-30 09:16", rules, s)[0]), 1)

    def test_pre_open_at_0900(self):
        rules = [{"id": "pre", "type": "pre_open"}]
        self.assertEqual(self.at("2026-10-29 08:59", rules)[0], [])
        rings, s = self.at("2026-10-29 09:00", rules)
        self.assertIn("pre-open order entry has started (09:00 IST); the normal session opens at 09:15", rings[0]["message"])
        self.assertEqual(self.at("2026-10-29 09:05", rules, s)[0], [])
        self.assertEqual(self.at("2026-10-29 09:20", rules)[0], [])        # missed it: no late pre-open alert
        self.assertEqual(self.at("2026-10-20 09:05", rules)[0], [])        # Dussehra

    def test_muhurat_session_bell(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-11-08 18:05", rules)[0], [])
        rings, _ = self.at("2026-11-08 18:05", rules, cfg=MUHURAT)
        self.assertIn("Muhurat trading 18:00-19:00", rings[0]["message"])

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1163.30, 1196.30))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:10", "flat by 15:10"))
        self.assertTrue(all(r["date"] == "2026-10-29" for r in rules))
        rings, s = self.at("2026-10-29 15:29", rules[:2])         # the real 15:29 quote, 1167.40
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1163.0
        rings, s = self.at("2026-10-29 11:30", rules[:2], s, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertEqual(rings[0]["message"], "RELIANCE is ₹1,163.00: at or below your stop ₹1,163.30 "
                                              "(plan stop: opening-range midpoint).")
        q["RELIANCE"]["price"] = 1196.5
        rings, s = self.at("2026-10-29 12:00", rules[:2], s, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-target"])
        self.assertEqual(self.at("2026-10-29 12:05", rules[:2], s, q)[0], [])  # both rang once

    def test_plan_defaults_and_short_side(self):
        rules = alerts.rules_from_plan({"symbol": "tcs", "stop": 2440, "target": 2510})
        self.assertEqual(rules[2]["at"], "15:10")                    # default flat_by
        self.assertNotIn("date", rules[0])
        self.assertEqual(alerts.rules_from_plan({"symbol": "TCS", "stop": 2440, "target": 2510}, "15:00")[2]["at"],
                         "15:00")
        short = alerts.rules_from_plan({"symbol": "SBIN", "side": "sell", "stop": 980, "target": 960})
        self.assertEqual([(r["type"], r["price"]) for r in short[:2]], [("above", 980.0), ("below", 960.0)])
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "TCS", "stop": 2440})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "TCS", "stop": 2440, "target": 2510, "flat_by": "3pm"})

    def test_flat_by_rings_once_on_the_plan_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-29 15:09", rules)[0], [])
        rings, s = self.at("2026-10-29 15:10", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by 15:10: it is 15:10 IST (set for 15:10), the market closes at 15:30.",
                      rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertEqual(self.at("2026-10-29 15:20", rules, s)[0], [])
        rings, _ = self.at("2026-10-29 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 15:15", rules, s)[0], [])     # plan rules: their day only

    def test_time_alert_needs_a_session(self):
        rule = [{"id": "t", "type": "time", "at": "15:10", "label": "check"}]
        self.assertEqual(self.at("2026-10-31 15:15", rule)[0], [])         # Saturday
        self.assertEqual(self.at("2026-10-02 15:15", rule)[0], [])         # Gandhi Jayanti
        rings, s = self.at("2026-12-01 15:10", rule)
        self.assertEqual(s["rung"]["t"], "2026-12-01T15:10:00+05:30")

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())

    def test_bad_rules(self):
        for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "TCS"},
                    {"id": "x", "type": "time", "at": "3pm"}, {"id": "x", "type": "above", "symbol": "T C S",
                                                               "price": 1}):
            with self.assertRaises(alerts.AlertError):
                alerts.evaluate([bad], {}, self.open, {}, self.now)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.none = os.path.join(self.d, "none.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now, "--config", self.none])

    def test_a_day_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.none])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)
        self.assertEqual(run(["add", "--rules", self.rules, "--pre-open"])[0], 0)

        code, text, _ = self.check("2026-10-29 09:02")
        self.assertEqual(code, 0)
        self.assertIn("NSE PRE-OPEN (2026-10-29 09:02 IST", text)
        self.assertIn("ALERT Pre-open", text)
        code, text, _ = self.check("2026-10-29 09:15")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-29 10:30")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 5 rule(s) watched today, RELIANCE ₹1,167.40.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1167.4})
        self.assertEqual(doc["source"], "yahoo")
        self.assertEqual(doc["as_of"], "2026-10-29T10:30:00+05:30")
        self.assertEqual(doc["detail"]["RELIANCE"], {"price": 1167.4, "prev_close": 1158.0,
                                                     "time": "2026-10-29T15:29:03+05:30", "source": "yahoo",
                                                     "fetched": "2026-10-29T10:30:00+05:30"})

        code, text, _ = self.check("2026-10-29 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by 15:10", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-29 15:12")[0], 1)
        code, text, _ = self.check("2026-10-30 09:16")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30 09:20")
        self.assertIn("Nothing hit. 2 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "pre-open", "reliance-flat-by", "reliance-plan-stop",
                                       "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_evening_and_next_morning(self):
        for args in (["--opening-bell"], ["--symbol", "TCS", "--above", "2500"], ["--symbol", "SBIN", "--stop", "950"],
                     ["--symbol", "^NSEI", "--below", "22800"]):
            self.assertEqual(run(["add", "--rules", self.rules] + args)[0], 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TCS", "--above", "2500"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        with open(self.rules) as fh:
            self.assertEqual([r["id"] for r in json.load(fh)["rules"]],
                             ["opening-bell", "tcs-above-2500", "sbin-stop-950", "nsei-below-22800"])

        code, text, _ = self.check("2026-10-29T16:30")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-10-29 16:30 IST, after the close)", text)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            self.assertEqual(json.load(fh)["prices"], {"TCS": 2478.0, "SBIN": 971.9, "^NSEI": 22936.75})
        code, text, _ = self.check("2026-10-31T10:00")                    # Saturday: no bell
        self.assertEqual(code, 1)
        self.assertIn("weekend", text)
        code, text, _ = self.check("2026-11-02T09:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        self.assertEqual(self.check("2026-11-02T09:45")[0], 1)
        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang 2026-11-02T09:15:00+05:30", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, _, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by 15:10",
                          "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, _, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1183.5",
                          "--date", "2026-10-29"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by 15:10",
                                    "symbol": "RELIANCE"})
        self.assertEqual(rules[1], {"id": "reliance-target-1183.50", "symbol": "RELIANCE", "type": "target",
                                    "price": 1183.5, "date": "2026-10-29"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "BAD!", "--above", "1"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", self.none])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_from_plan_uses_config_flat_by(self):
        plan = os.path.join(self.d, "plan.json")
        with open(PLAN) as fh:
            p = json.load(fh)
        p.pop("flat_by")
        with open(plan, "w") as fh:
            json.dump(p, fh)
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"flat_by": "15:05"}, fh)
        run(["add", "--from-plan", "--plan", plan, "--rules", self.rules, "--state", self.state, "--config", cfg])
        with open(self.rules) as fh:
            self.assertEqual(json.load(fh)["rules"][2]["at"], "15:05")

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE,TCS", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-29T16:30"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   ₹1,167.40 (+0.81% vs prev close ₹1,158.00)", text)
        self.assertIn("delayed", text)
        run(["quote", "M&M", "--offline", QDIR, "--write", self.prices, "--now", "2026-10-29T16:31"])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["M&M", "RELIANCE", "TCS"])
        self.assertEqual(doc["detail"]["TCS"]["prev_close"], 2460.0)
        self.assertEqual(doc["detail"]["TCS"]["fetched"], "2026-10-29T16:30:00+05:30")
        self.assertEqual(doc["detail"]["M&M"]["fetched"], "2026-10-29T16:31:00+05:30")
        self.assertEqual(doc["as_of"], "2026-10-29T16:31:00+05:30")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 50000, "positions": {"SBIN": {"qty": 10, "avg_price": 950.0}}}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["RELIANCE", "SBIN"])

    def test_clock(self):
        code, text, _ = run(["clock", "--now", "2026-10-29 10:15", "--config", self.none])
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN, normal session", text)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00", "--config", self.none])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-03-01 10:00", "--config", self.none])
        self.assertIn("Warning: NSE's 2027 holiday list is not built in", text)
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump(MUHURAT, fh)
        code, text, _ = run(["clock", "--now", "2026-11-08 18:10", "--config", cfg])
        self.assertEqual(code, 0)
        self.assertIn("normal 18:00 to 19:00 IST (Muhurat trading)", text)

    def test_clock_verify_against_nse(self):
        code, text, _ = run(["clock", "--now", "2026-10-29 16:29", "--verify", "--offline", QDIR,
                             "--config", self.none])
        self.assertEqual(code, 1)
        self.assertIn("nseindia.com says: Capital Market Closed (Normal Market has Closed), "
                      "Nifty 50 22,936.75 +1.05%.", text)
        self.assertNotIn("disagree", text)
        code, text, _ = run(["clock", "--now", "2026-10-29 11:00", "--verify", "--offline", QDIR, "--json",
                             "--config", self.none])
        self.assertFalse(json.loads(text)["nse_agrees"])
        code, text, _ = run(["clock", "--now", "2026-10-29 11:00", "--verify", "--offline", self.d,
                             "--config", self.none])
        self.assertIn("Could not cross-check with NSE", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
