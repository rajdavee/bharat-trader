"""Dated market rules, offline: NSE session timings by segment (closing auction, pre-open,
derivatives close), broker square-off profiles, the safe flat-by and entry cutoff, NSE's
F&O stock list, and the dated F&O charges. Every copy of the shared helpers is tested,
on both sides of every effective date.

Fixture, from the live NSE answer of 2026-11-01:
  rules/nse-master-quote-2026-11-01.json  https://www.nseindia.com/api/master-quote, the 218 F&O stocks
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, FIX, ROOT  # noqa: E402

RULE_TOOLS = [("trader-entry", "entry"), ("trader-exit", "exit"), ("trader-alerts", "alerts"),
              ("trader-risk", "check"), ("trader-timemachine", "timemachine"), ("trader-scanner", "scan"),
              ("trader-broker", "ticket")]
CHARGE_TOOLS = [("trader-budget", "size"), ("trader-broker", "ticket"), ("trader-risk", "check"),
                ("trader-exit", "exit"), ("trader-timemachine", "timemachine")]
MODS = {name: load(folder, name) for folder, name in RULE_TOOLS + CHARGE_TOOLS}
MQ = os.path.join(FIX, "rules", "nse-master-quote-2026-11-01.json")
T = datetime.time
D = datetime.date


def hm(t):
    return t.strftime("%H:%M")


def each(test):
    """Run test(mod) for every tool that carries the dated rules."""
    def run(self):
        for _, name in RULE_TOOLS:
            with self.subTest(tool=name):
                test(self, MODS[name])
    return run


class Sessions(unittest.TestCase):
    @each
    def test_cash_stock_without_derivatives_keeps_15_30(self, m):
        for day in ("2026-07-31", "2026-08-03", "2026-12-04", "2026-12-07"):
            s = m.session_for(day, "EQ")
            self.assertEqual((hm(s["open"]), hm(s["continuous_end"]), hm(s["close"])), ("09:15", "15:30", "15:30"))
            self.assertIsNone(s["cas"])

    @each
    def test_closing_auction_starts_on_3_august_2026(self, m):
        before, on = m.session_for("2026-07-31", "EQ_FNO"), m.session_for(D(2026, 8, 3), "EQ_FNO")
        self.assertEqual(hm(before["continuous_end"]), "15:30")
        self.assertIsNone(before["cas"])
        self.assertEqual(hm(on["continuous_end"]), "15:15")
        self.assertEqual({k: (hm(v) if not isinstance(v, list) else [hm(x) for x in v]) for k, v in on["cas"].items()},
                         {"start": "15:15", "orders_from": "15:20", "limit_only_from": "15:25",
                          "random_close": ["15:28", "15:30"], "end": "15:35"})
        self.assertEqual(hm(on["close"]), "15:35")
        self.assertEqual(on["rule_from"], "2026-08-03")
        self.assertIn("NSE/CMTR/72394", on["source"])

    @each
    def test_closing_auction_revised_on_7_december_2026(self, m):
        old, new = m.session_for("2026-12-04", "EQ_FNO"), m.session_for("2026-12-07", "EQ_FNO")
        self.assertEqual((hm(old["continuous_end"]), hm(old["close"])), ("15:15", "15:35"))
        self.assertEqual((hm(new["continuous_end"]), hm(new["cas"]["orders_from"]), hm(new["cas"]["limit_only_from"]),
                          [hm(x) for x in new["cas"]["random_close"]], hm(new["close"])),
                         ("15:30", "15:31", "15:36", ["15:38", "15:40"], "15:40"))
        self.assertIn("23843/2026", new["source"])

    @each
    def test_derivatives_close(self, m):
        self.assertEqual(hm(m.session_for("2026-07-31", "DERIV")["close"]), "15:30")
        self.assertEqual(hm(m.session_for("2026-08-03", "DERIV")["close"]), "15:40")
        self.assertEqual(hm(m.session_for("2026-12-04", "DERIV")["close"]), "15:40")
        self.assertEqual(hm(m.session_for("2026-12-07", "DERIV")["close"]), "15:45")

    @each
    def test_pre_open_revised_on_7_september_2026(self, m):
        old, new = m.session_for("2026-09-04", "EQ"), m.session_for("2026-09-07", "EQ")
        self.assertEqual([hm(x) for x in old["pre_open"]], ["09:00", "09:08"])
        self.assertEqual(hm(old["pre_open_market_orders_until"]), "09:08")
        self.assertEqual([hm(x) for x in new["pre_open"]], ["09:00", "09:10"])
        self.assertEqual(hm(new["pre_open_market_orders_until"]), "09:05")
        self.assertEqual(hm(new["open"]), "09:15")

    @each
    def test_bad_segment(self, m):
        with self.assertRaises(m.RulesError):
            m.session_for("2026-10-29", "OPT")


class BrokerSquareOff(unittest.TestCase):
    @each
    def test_profiles_and_their_dates(self, m):
        def sq(name, day, seg):
            return hm(m.square_off_for(day, seg, {"broker_profile": name})[0])
        self.assertEqual(sq("zerodha", "2026-08-07", "EQ_FNO"), "15:10")
        self.assertEqual(sq("zerodha", "2026-08-10", "EQ_FNO"), "15:12")
        self.assertEqual((sq("zerodha", "2026-10-29", "EQ"), sq("zerodha", "2026-10-29", "DERIV")), ("15:25", "15:26"))
        self.assertEqual(sq("upstox", "2026-09-10", "EQ_FNO"), "15:05")
        self.assertEqual(sq("upstox", "2026-09-11", "EQ_FNO"), "15:10")
        self.assertEqual((sq("dhan", "2026-10-29", "EQ"), sq("dhan", "2026-10-29", "DERIV")), ("15:10", "15:25"))
        self.assertEqual((sq("angel", "2026-10-29", "EQ_FNO"), sq("angel", "2026-10-29", "DERIV")), ("15:10", "15:20"))
        self.assertEqual((sq("groww", "2026-10-29", "EQ_FNO"), sq("groww", "2026-10-29", "EQ")), ("15:05", "15:20"))
        self.assertEqual(sq("ZERODHA", "2026-07-01", "EQ_FNO"), "15:10")       # before a broker's first row: its first
        self.assertEqual(m.square_off_for("2026-10-29", "EQ", {}), (None, ""))
        self.assertIn("support.zerodha.com", m.square_off_for("2026-10-29", "EQ", {"broker_profile": "zerodha"})[1])

    @each
    def test_custom_and_unknown(self, m):
        c = {"broker_profile": "custom", "square_off": {"EQ": "15:18", "EQ_FNO": "15:02", "DERIV": "15:24"}}
        self.assertEqual(hm(m.square_off_for("2026-10-29", "EQ_FNO", c)[0]), "15:02")
        with self.assertRaises(m.RulesError):
            m.square_off_for("2026-10-29", "EQ", {"broker_profile": "custom"})
        with self.assertRaises(m.RulesError) as cm:
            m.square_off_for("2026-10-29", "EQ", {"broker_profile": "kite"})
        self.assertIn("zerodha", str(cm.exception))


class FlatBy(unittest.TestCase):
    @each
    def test_the_safe_flat_by(self, m):
        f = m.safe_flat_by
        # no profile: 5 minutes before continuous trading ends
        self.assertEqual(hm(f("2026-10-29", "EQ_FNO", {"flat_by": "15:10"})[0]), "15:10")
        self.assertEqual(f("2026-10-29", "EQ_FNO", {"flat_by": "15:10"})[1], "")
        self.assertEqual(hm(f("2026-10-29", "EQ_FNO", {})[0]), "15:10")               # the default flat_by
        self.assertEqual(hm(f("2026-12-08", "EQ", {})[0]), "15:10")
        self.assertEqual(hm(f("2026-10-29", "EQ_FNO", {"flat_by": "auto"})[0]), "15:10")
        self.assertEqual(hm(f("2026-12-08", "EQ_FNO", {"flat_by": "auto"})[0]), "15:25")
        self.assertEqual(hm(f("2026-07-31", "EQ_FNO", {"flat_by": "auto"})[0]), "15:25")
        self.assertEqual(hm(f("2026-10-29", "EQ", {"flat_by": "auto"})[0]), "15:25")
        # a late flat_by is pulled in, with the reason
        t, note = f("2026-10-29", "EQ_FNO", {"flat_by": "15:14"})
        self.assertEqual(hm(t), "15:10")
        self.assertIn("continuous trading ends 15:15", note)
        # the broker's square-off, less the margin
        t, note = f("2026-10-29", "EQ_FNO", {"flat_by": "15:10", "broker_profile": "groww"})
        self.assertEqual(hm(t), "15:00")
        self.assertIn("groww squares off MIS at 15:05", note)
        self.assertEqual(hm(f("2026-10-29", "EQ_FNO", {"flat_by": "15:10", "broker_profile": "zerodha"})[0]), "15:07")
        self.assertEqual(hm(f("2026-10-29", "EQ", {"flat_by": "15:10", "broker_profile": "zerodha"})[0]), "15:10")
        self.assertEqual(hm(f("2026-10-29", "EQ_FNO", {"broker_profile": "zerodha", "square_off_margin_min": 10})[0]),
                         "15:02")

    @each
    def test_entry_cutoff(self, m):
        self.assertEqual(hm(m.entry_cutoff("2026-10-29", "EQ_FNO", {"no_entries_after": "14:45"})), "14:45")
        self.assertEqual(hm(m.entry_cutoff("2026-10-29", "EQ_FNO", {"no_entries_after": "15:05"})), "14:55")
        self.assertEqual(hm(m.entry_cutoff("2026-12-08", "EQ_FNO", {"no_entries_after": "15:05"})), "14:55")
        self.assertEqual(hm(m.entry_cutoff("2026-12-08", "EQ_FNO", {"no_entries_after": "15:05", "flat_by": "15:25"})),
                         "15:05")
        self.assertEqual(hm(m.entry_cutoff("2026-10-29", "EQ_FNO", {"no_entries_after": "15:05", "flat_by": "15:25"})),
                         "14:55")                                   # the auction: flat by 15:10 at the latest
        self.assertEqual(hm(m.entry_cutoff("2026-10-29", "EQ_FNO", {"broker_profile": "groww"})), "14:45")


class FnoList(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(MQ, encoding="utf-8") as fh:
            self.text = fh.read()

    def tearDown(self):
        shutil.rmtree(self.d)

    @each
    def test_the_real_nse_list(self, m):
        syms = m.parse_fno_list(self.text)
        self.assertEqual(len(syms), 218)
        self.assertTrue({"RELIANCE", "HDFCBANK", "TCS", "INFY", "SBIN", "M&M"} <= syms)
        self.assertEqual(m.segment_of("reliance", syms), "EQ_FNO")
        self.assertEqual(m.segment_of("IRCTC", syms), "EQ")
        self.assertEqual(m.segment_of("IRCTC", None), "EQ_FNO")      # no list: the earlier close, to be safe
        with self.assertRaises(m.RulesError):
            m.parse_fno_list('["RELIANCE", "TCS"]')
        with self.assertRaises(m.RulesError):
            m.parse_fno_list("<html>")

    @each
    def test_config_cache_and_refresh(self, m):
        cache = os.path.join(self.d, m.__name__, "fno_stocks.json")
        self.assertEqual(m.load_fno_stocks({"fno_stocks": ["reliance"]})[0], {"RELIANCE"})
        syms, note = m.load_fno_stocks({}, cache, fetch=False)
        self.assertIsNone(syms)
        self.assertIn("treating every stock as an F&O stock", note)
        real = m.parse_fno_list(self.text)
        old = m.fetch_fno_stocks
        calls = []

        def fake(timeout=20):
            calls.append(1)
            return real
        m.fetch_fno_stocks = fake
        try:
            syms, note = m.load_fno_stocks({}, cache, fetch=True, today="2026-11-01")
            self.assertEqual(len(syms), 218)
            with open(cache, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["fetched"], doc["source"]), ("2026-11-01", "https://www.nseindia.com/api/master-quote"))
            m.load_fno_stocks({}, cache, fetch=True, today="2026-11-01")
            self.assertEqual(len(calls), 1)                         # once a day
        finally:
            m.fetch_fno_stocks = old                                # back to "no network in the tests"
        syms, note = m.load_fno_stocks({}, cache, fetch=True, today="2026-11-02")
        self.assertEqual(len(syms), 218)                            # the fetch failed: the cached list
        self.assertIn("cached 2026-11-01", note)
        self.assertEqual(len(m.load_fno_stocks({}, cache)[0]), 218)


class FnoCharges(unittest.TestCase):
    """Hand-checked: options on the premium, futures on the traded value, STT from the Finance Act 2026."""

    def each(self, fn):
        for _, name in CHARGE_TOOLS:
            with self.subTest(tool=name):
                fn(MODS[name])

    def test_options_both_sides_of_1_april_2026(self):
        def t(m):
            new = m.round_trip_charges(120.50, 135.25, 65, None, "OPT", "2026-10-30")
            self.assertEqual(new, {"brokerage": 40.0, "stt": 13.19, "exchange": 5.91, "sebi": 0.02, "stamp": 0.23,
                                   "gst": 8.27, "total": 67.62})
            old = m.round_trip_charges(120.50, 135.25, 65, {}, "OPT", "2026-03-31")
            self.assertEqual((old["stt"], old["total"]), (8.79, 63.22))
            self.assertEqual(m.round_trip_charges(120.50, 135.25, 65, {}, "OPT", "2026-04-01")["stt"], 13.19)
        self.each(t)

    def test_futures_both_sides_of_1_april_2026(self):
        def t(m):
            new = m.round_trip_charges(1174.30, 1180.00, 500, {}, "FUT", "2026-10-30")
            self.assertEqual(new, {"brokerage": 40.0, "stt": 295.0, "exchange": 21.54, "sebi": 1.18, "stamp": 11.74,
                                   "gst": 11.29, "total": 380.75})
            self.assertEqual(m.round_trip_charges(1174.30, 1180.00, 500, {}, "FUT", "2026-03-31")["total"], 203.75)
        self.each(t)

    def test_exercised_option_pays_stt_on_the_intrinsic_value(self):
        def t(m):
            new = m.round_trip_charges(10, 50, 65, {}, "OPT", "2026-10-30", True)
            self.assertEqual(new, {"brokerage": 20.0, "stt": 4.88, "exchange": 0.23, "sebi": 0.0, "stamp": 0.02,
                                   "gst": 3.64, "total": 28.77})
            self.assertEqual(m.round_trip_charges(10, 50, 65, {}, "OPT", "2026-03-31", True)["stt"], 4.06)  # 0.125%
        self.each(t)

    def test_segment_from_the_config_and_overrides(self):
        def t(m):
            self.assertEqual(m.round_trip_charges(120.50, 135.25, 65, {"segment": "opt"}, on="2026-10-30")["total"],
                             67.62)
            cheap = {"fno_charges": {"OPT": {"brokerage_flat": 0}}}
            self.assertEqual(m.round_trip_charges(120.50, 135.25, 65, cheap, "OPT", "2026-10-30")["brokerage"], 0.0)
            with self.assertRaises(ValueError):
                m.round_trip_charges(1, 2, 1, {}, "CUR")
        self.each(t)

    def test_equity_is_unchanged(self):
        def t(m):
            base = {"product": "MIS"}
            eq = m.round_trip_charges(1174.30, 1163.30, 83, base)
            self.assertEqual(eq["total"], 81.28)
            self.assertEqual(m.round_trip_charges(1174.30, 1163.30, 83, base, "EQ", "2026-03-31"), eq)
        self.each(t)


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class InTheTools(unittest.TestCase):
    """Each tool on both sides of a rule date."""

    def test_entry_session_closes_for_the_auction(self):
        e = MODS["entry"]
        self.assertEqual(e.session(D(2026, 7, 31), {}, "EQ_FNO"), (T(9, 15), T(15, 30)))
        self.assertEqual(e.session(D(2026, 8, 3), {}, "EQ_FNO"), (T(9, 15), T(15, 15)))
        self.assertEqual(e.session(D(2026, 8, 3), {}, "EQ"), (T(9, 15), T(15, 30)))
        self.assertEqual(e.session(D(2026, 12, 7), {}, "EQ_FNO"), (T(9, 15), T(15, 30)))
        win = e.entry_window(D(2026, 10, 29), (T(9, 15), T(15, 15)),
                             {"no_entries_before": "09:30", "no_entries_after": "15:05", "broker_profile": "groww"},
                             "EQ_FNO")
        self.assertEqual([hm(x) for x in win], ["09:30", "14:45"])

    def test_entry_check_reports_the_auction(self):
        path = os.path.join(FIX, "trader-entry", "RELIANCE-2026-10-29-5m.json")
        d = tempfile.mkdtemp()
        try:
            cfgp = os.path.join(d, "config.json")
            with open(cfgp, "w") as fh:
                json.dump({"broker_profile": "zerodha"}, fh)
            with open(MQ, encoding="utf-8") as fh:
                syms = sorted(MODS["entry"].parse_fno_list(fh.read()))
            with open(os.path.join(d, "fno_stocks.json"), "w") as fh:
                json.dump({"fetched": "2026-11-01", "symbols": syms}, fh)      # the cache the tools write
            code, out, _ = run(MODS["entry"], ["check", "RELIANCE", "--now", "2026-10-29 10:15", "--offline", path,
                                               "--config", cfgp, "--json"])
            res = json.loads(out)
            self.assertEqual((res["segment"], res["closes"], res["closing_auction"], res["flat_by"]),
                             ("EQ_FNO", "15:15", ["15:15", "15:35"], "15:07"))
            self.assertTrue(res["fno_list"])
            code, out, _ = run(MODS["entry"], ["check", "RELIANCE", "--now", "2026-10-29 10:15", "--offline", path,
                                               "--config", cfgp])
            self.assertIn("F&O stock: continuous trading ends 15:15, then the closing auction (15:15-15:35 IST); "
                          "entries until 14:45, flat by 15:07.", out)
            self.assertIn("plan to be flat by 15:07", out)
        finally:
            shutil.rmtree(d)

    def test_risk_window_for_an_fno_stock(self):
        check = MODS["check"]
        with open(os.path.join(FIX, "trader-risk", "orders-reliance.json")) as fh:
            order = json.load(fh)["orders"][0]
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            base = json.load(fh)
        c = dict(base, no_entries_after="15:00", broker_profile="zerodha")
        now = check.parse_now("2026-10-29 14:55")
        r = check.check([order], c, None, None, None, None, now)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("no new entries after 14:52 IST (it is 14:55) (15 minutes before the safe flat-by 15:07",
                      " ".join(r["reasons"]))
        # the same stock read as a plain cash stock: its window runs to 15:00
        r = check.check([order], dict(c, fno_stocks=["TCS"]), None, None, None, None, now)["results"][0]
        self.assertNotIn("window", " ".join(r["reasons"]))
        # before the closing auction existed, 15:20 was continuous trading; on 2026-10-29 it is the auction
        r = check.check([order], base, None, None, None, None, check.parse_now("2026-10-29 15:20"))["results"][0]
        self.assertIn("the closing auction runs to 15:35", " ".join(r["reasons"]))
        r = check.check([order], base, None, None, None, None, check.parse_now("2026-07-31 15:20"))["results"][0]
        self.assertIn("no new entries after 14:45", " ".join(r["reasons"]))

    def test_alerts_closing_auction_bell_and_flat_by(self):
        a = MODS["alerts"]
        rule = [{"id": "closing-auction", "type": "closing_auction"}]
        for when, rings in (("2026-10-29 15:14", 0), ("2026-10-29 15:16", 1), ("2026-07-31 15:16", 0),
                            ("2026-12-08 15:16", 0), ("2026-12-08 15:31", 1)):
            now = a.parse_now(when)
            got, _ = a.evaluate(rule, {}, a.market_status(now), {}, now)
            self.assertEqual(len(got), rings, when)
        now = a.parse_now("2026-10-29 15:16")
        msg = a.evaluate(rule, {}, a.market_status(now), {}, now)[0][0]["message"]
        self.assertIn("ended at 15:15 IST; the auction runs to 15:35", msg)
        plan = {"symbol": "RELIANCE", "stop": 1163.3, "target": 1196.3, "flat_by": "15:10", "date": "2026-10-29"}
        flat = a.rules_from_plan(plan, None, {"broker_profile": "groww"}, None)[2]
        self.assertEqual((flat["at"], flat["label"]), ("15:00", "flat by 15:00"))
        self.assertIn("groww squares off MIS at 15:05", flat["note"])
        self.assertEqual(a.rules_from_plan(plan)[2]["at"], "15:10")

    def test_budget_init_takes_a_broker_profile(self):
        size = MODS["size"]
        self.assertEqual(size.build_config(100000, broker_profile="Groww")["broker_profile"], "groww")
        self.assertEqual(size.build_config(100000)["broker_profile"], "")
        for bad in ("kite", "custom"):
            with self.assertRaises(size.SizeError):
                size.build_config(100000, broker_profile=bad)
        self.assertIn("groww squares off MIS positions",
                      size.describe_config(size.build_config(100000, broker_profile="groww")))
        self.assertEqual(sorted(size.BROKER_PROFILE_NAMES), sorted(MODS["entry"].BROKER_PROFILES))

    def test_broker_flatten_uses_the_profile(self):
        t = MODS["ticket"]
        d = tempfile.mkdtemp()
        try:
            pf = os.path.join(FIX, "trader-broker", "portfolio-open.json")
            cfgp = os.path.join(d, "config.json")
            with open(cfgp, "w") as fh:
                json.dump({"broker_profile": "groww"}, fh)
            code, out, err = run(t, ["flatten", "--portfolio", pf, "--config", cfgp, "--orders",
                                     os.path.join(d, "orders.json"), "--plan", os.path.join(d, "none.json"),
                                     "--now", "2026-10-29 15:02"])
            self.assertEqual(code, 0, err)
            self.assertIn("past your flat_by 15:00", out)
            self.assertIn("groww squares off MIS at 15:05 IST", out)
            with open(os.path.join(d, "orders.json")) as fh:
                self.assertIn("flat by 15:00 IST", json.load(fh)["orders"][0]["why"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
