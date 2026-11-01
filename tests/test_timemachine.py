"""trader-timemachine tests, offline.

Fixtures (tests/fixtures/trader-timemachine/):
  HDFCBANK-60d5m.json.gz      Yahoo v8 chart for HDFCBANK.NS, range=60d interval=5m, fetched
                              2026-10-29 16:29 IST (after the close). 60 sessions, 2026-08-04 to
                              2026-10-29, 75 bars each (09:15 to 15:25 IST). Yahoo left 117 bars
                              empty: the 15:20 bar of every session, the 15:15 or 15:25 bar of many,
                              and 11:45 / 11:50 on 2026-10-15. No data on the NSE holidays
                              2026-09-14, 2026-10-02 and 2026-10-20.
  plan-HDFCBANK-2026-10-26.json  trader/plan.json as trader-exit writes it for the real opening-range
                              breakout in that data (see tests/fixtures/trader-exit/): entry 692.70 at
                              10:10 (bar 10), stop 689.90 (or-mid), 2R target 698.30, flat by 15:10,
                              charges estimated on 72 shares.
"""

import gzip
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, FIX, ROOT  # noqa: E402

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
CHART = os.path.join(DIR, "HDFCBANK-60d5m.json.gz")
with gzip.open(CHART, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
PLAN_FILE = os.path.join(DIR, "plan-HDFCBANK-2026-10-26.json")
with open(PLAN_FILE, encoding="utf-8") as _fh:
    PLAN = json.load(_fh)
NOW = "2026-10-26 10:15"
NOW_IST = tm.parse_now(NOW)


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_IST, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.ist_of(ts)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheFixturePlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:10"))
        self.assertEqual(m["pool_sessions"], 55)
        self.assertEqual(m["bars_left"], 60)                           # bars 11 .. 70 (10:10 to 15:10)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-04", "2026-10-23"))
        self.assertEqual(m["p_target"], 0.1973)
        self.assertEqual(m["p_stop"], 0.6402)
        self.assertEqual(m["p_neither"], 0.1625)
        self.assertEqual(m["p_green"], 0.3157)
        self.assertEqual(m["median_close"], 691.96)
        self.assertEqual(m["p5_close"], 683.87)
        self.assertEqual(m["p95_close"], 700.59)
        self.assertEqual(m["avg_R"], -0.17)
        self.assertEqual(m["avg_R_net"], -0.433)
        self.assertEqual(m["r_at_target"], 2.0)

    def test_the_bands_and_histogram_are_pinned(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [692.7] * 5)
        self.assertEqual(m["bands"][-1], [683.75, 688.34, 691.7, 695.13, 700.46])
        self.assertEqual(len(m["bands"]), 61)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:10", "15:10", 61))
        self.assertEqual(m["hist"], {"lo": 680.55, "hi": 704.37,
                                     "bins": [111, 117, 138, 215, 283, 368, 519, 589, 690, 790, 752, 758, 752, 743,
                                              618, 554, 493, 385, 290, 231, 185, 124, 74, 58]})
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertTrue(all(p[0] == 692.7 and len(p) == 61 for p in m["sample_paths"]))

    def test_charges(self):
        ch = self.doc["charges"]
        self.assertEqual(ch, {"qty": 72, "qty_source": "plan", "product": "MIS", "at_target": 53.27, "at_stop": 52.88})
        # at the target R is +2, net 2 - 53.27 / (2.80 x 72) = 2 - 0.2642 = 1.7358; at the stop -1 - 0.2623
        self.assertAlmostEqual(2 - 53.27 / (2.80 * 72), 1.7358, places=4)

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (55, 33))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (5, 18, 10, 11))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (-0.155, -0.443))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0})
        self.assertEqual(h["trades"][0], {"date": "2026-08-05", "time": "10:50", "entry": 740.7, "stop": 737.1,
                                          "target": 747.9, "outcome": "stop", "R": -1.0, "charges": 56.52,
                                          "R_net": -1.22})
        self.assertEqual(h["trades"][-1]["date"], "2026-10-23")
        for t in h["trades"]:
            self.assertLessEqual(t["time"], "14:45")                 # no_entries_after
            for k in ("entry", "stop", "target"):                    # every price on the 0.05 tick
                self.assertAlmostEqual(round(t[k] / 0.05) * 0.05, t[k], places=6)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of "
                                              "the replays that hit one side; it did in 24 % (target first 19.7 % vs "
                                              "stop first 64.0 %).")
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("HDFCBANK (NSE) buy, Monday 2026-10-26: entry 692.70 at 10:10 IST (bar 10), stop 689.90, "
                     "target 698.30 (2R, risk 2.80 a share).", "10,000 replays", "10:10 to 15:10 IST",
                     "60 five-minute bars", "seed 19", "55 sessions", "target first   19.7 %",
                     "stop first     64.0 %", "neither        16.2 %   still open at 15:10",
                     "green          31.6 %", "median 691.96, 90 % of replays between 683.87 and 700.59",
                     "Average R: -0.170 gross", "Average R: -0.433 net of charges (₹53.27 at the target, ₹52.88 at "
                     "the stop, round trip on 72 shares, MIS)",
                     "fired 33 times: 5 target, 18 stop, 10 neither. Average R -0.155 gross, -0.443 net of charges.",
                     "Left out 1 session(s)", "2026-10-15", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "$", " ET"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)
        self.assertEqual((a["p_target"], a["p_stop"], a["avg_R"], a["avg_R_net"]), (0.199, 0.64, -0.17, -0.432))

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["avg_R"]), (0.2034, 0.6336, -0.149))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; this pins the first
        # draws so a failure points at the RNG rather than the code
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(55)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"], a["p_target"], a["p_stop"]), (500, 0, 0.198, 0.616))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        for kw in ({"replays": 0}, {"window": 11}, {"qty": 0}):
            with self.assertRaises(tm.TMError):
                machine(**kw)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "HDFCBANK")

    def test_ist_session_grid(self):
        meta, bars, dropped = tm.parse_bars(self.result, tm.parse_now("2026-10-29 16:30"))
        self.assertEqual(dropped, {"no_price": 117, "off_grid": 0, "not_complete": 1, "outside_hours": 0})
        first = bars[0]
        self.assertEqual((first["date"], first["start"], first["slot"]), ("2026-08-04", "09:15", 0))
        # the 15:25 bar of the last day is the last slot (74); it closes 15:30, after Yahoo's 15:29 stamp
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-29", "15:10", 71))
        days = tm.split_sessions(bars)
        self.assertEqual(len(days), 60)
        self.assertTrue(all(max(s) <= tm.SESSION_BARS - 1 for s in days.values()))
        for holiday in ("2026-09-14", "2026-10-02", "2026-10-20"):
            self.assertNotIn(holiday, days)
        self.assertNotIn(20, [b["slot"] for b in bars if b["start"] == "15:20"])   # 15:20 always empty
        self.assertEqual([b for b in bars if b["start"] == "15:20"], [])

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-26 10:08"))[1]
        today = [b for b in bars if b["date"] == "2026-10-26"]
        self.assertEqual(len(today), 10)                               # 09:15 .. 10:00 closed by 10:08
        self.assertEqual(len([b for b in tm.parse_bars(self.result, tm.parse_now("2026-10-26 10:10"))[1]
                              if b["date"] == "2026-10-26"]), 11)      # the 10:05 entry bar closes at 10:10
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-26 10:08"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("10:00 bar", str(cm.exception))
        self.assertEqual(tm.parse_now("2026-10-26T04:45:00Z"), NOW_IST)

    def test_a_partial_session_is_left_out(self):
        def cut(res):  # 2026-09-04 ends at 13:00, like a session cut short: 45 bars
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 54)
        self.assertIn({"date": "2026-09-04", "bars": 45, "why": "no 13:00 bar"}, doc["data"]["short_sessions_left_out"])
        self.assertEqual(doc["history"]["sessions"], 54)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        # an earlier flat-by keeps it: every bar up to 12:55 is there
        self.assertEqual(machine(text=edited(cut), replays=50, until="13:00")["mc"]["pool_sessions"], 55)

    def test_special_sessions_and_extra_holidays_leave_the_pool(self):
        cfg = tm.read_config(None)
        cfg["special_sessions"] = [{"date": "2026-09-04", "open": "09:15", "close": "12:00"}]
        cfg["extra_holidays"] = ["2026-09-03"]
        doc = machine(replays=200, cfg=cfg)
        self.assertEqual(doc["mc"]["pool_sessions"], 53)
        why = {x["date"]: x["why"] for x in doc["data"]["short_sessions_left_out"]}
        self.assertIn("special session", why["2026-09-04"])
        self.assertIn("NSE holiday", why["2026-09-03"])

    def test_the_plans_day_must_be_a_regular_session(self):
        for day, words in (("2026-10-20", "Dussehra"), ("2026-10-25", "Sunday"), ("2026-11-10", "Balipratipada")):
            with self.assertRaises(tm.TMError) as cm:
                machine(plan(date=day))
            self.assertIn("NSE is shut", str(cm.exception))
            self.assertIn(words, str(cm.exception))
        cfg = tm.read_config(None)
        cfg["special_sessions"] = [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-08"), cfg=cfg)
        self.assertIn("regular 09:15 to 15:30 sessions only", str(cm.exception))
        self.assertEqual(tm.market_day("2027-02-01")[0], "regular")
        self.assertIn("calendar for 2027 is unknown", tm.market_day("2027-02-01")[2])

    def test_yahoos_empty_closing_bars(self):
        # HDFCBANK is an F&O stock: since 2026-08-03 its continuous trading ends 15:15 and the
        # closing auction follows (Yahoo shows those bars empty or as one print)
        for until in ("15:20", "15:25"):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=until, replays=10)
            self.assertIn("after HDFCBANK's continuous trading ends on 2026-10-26 (15:15 IST", str(cm.exception))
        self.assertEqual(machine(until="15:15", replays=10)["mc"]["pool_sessions"], 55)   # needs up to 15:10
        # treated as a plain cash stock (an F&O list without it), the late bars are just missing
        cfg = tm.read_config(None)
        cfg["fno_stocks"] = ["RELIANCE"]
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:25", replays=10, cfg=cfg)
        self.assertIn("only 0 usable session(s)", str(cm.exception))
        self.assertIn("flat-by at or before 15:15", str(cm.exception))
        self.assertEqual(machine(until="15:20", replays=10, cfg=cfg)["mc"]["pool_sessions"], 49)   # 15:15 empty on 6

    def test_flat_by_pulled_in_for_the_closing_auction(self):
        cfg = tm.read_config(None)
        cfg.update(flat_by="15:15", broker_profile="groww")      # Groww squares off F&O stocks at 15:05
        doc = machine(replays=10, cfg=cfg)
        self.assertEqual(doc["mc"]["until"], "15:00")
        self.assertTrue(any("the replays end at 15:00" in w for w in doc["warnings"]), doc["warnings"])

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200, now=tm.parse_now("2026-10-29 16:30"))
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-23")
        self.assertNotIn("2026-10-26", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 118)
        self.assertEqual(doc["mc"]["pool_sessions"], 54)   # 2026-08-04 now has a gap at 09:40

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "HDFCBANK.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-24")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 usable session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-23")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="RELIANCE"))
        self.assertIn("data is for HDFCBANK.NS", str(cm.exception))
        self.assertEqual(plan(symbol="hdfcbank.ns")["symbol"], "HDFCBANK")


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-30"))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # the 10:15 bar has not closed by 10:15
            machine(plan(entry_time="10:20", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:12", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25 IST"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=693.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="HDFC BANK"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="26 Oct"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:10")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 10)
        self.assertEqual(plan(entry_time="10:10 AM", entry_bar=None)["entry_bar"], 10)
        self.assertEqual(plan(entry_time="2:30", entry_bar=None)["entry_time"], "14:30")
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")
        self.assertEqual(tm.chart_url("m&m"), "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS"
                                              "?range=60d&interval=5m")

    def test_until_and_flat_by(self):
        doc = machine(until="15:00", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:00", 58, "15:00"))
        self.assertIn("still open at 15:00", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:00 PM", replays=500)["mc"], m)
        self.assertEqual(machine(plan(flat_by="15:00"), replays=500)["mc"], m)          # the plan's flat_by
        cfg = tm.read_config(None)
        cfg["flat_by"] = "15:00"
        self.assertEqual(machine(plan(flat_by=None), replays=500, cfg=cfg)["mc"], m)     # else the config's
        for bad, words in (("10:10", "nothing to replay"), ("15:35", "between 09:20 and 15:30"),
                           ("15:07", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=696.0, stop=693.0, target=702.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_ticks(self):
        self.assertEqual(tm.tick_away(689.925, True), 689.9)
        self.assertEqual(tm.tick_away(689.925, False), 689.95)
        self.assertEqual(tm.tick_away(687.8499755859375, True), 687.8)       # a raw float just under the tick
        self.assertEqual(tm.tick_away(687.85, True), 687.85)
        self.assertEqual(tm.tick_near(687.8499755859375), 687.85)
        self.assertEqual(tm.tick_near(101.325), 101.35)
        self.assertEqual(tm.tick_away(1445.05, True, 0.10), 1445.0)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=687.85, target=702.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 2.0))
        rule = machine(plan(stop_rule="atr", stop=689.5, target=699.1), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.462, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="level"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=695.5, target=687.1, stop_rule="or_high", charges=None), replays=2000)
        m = doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["r_at_target"]), (0.322, 0.4895, 2.0))
        self.assertEqual((doc["history"]["rule"]["stop_rule"], doc["history"]["triggered"]), ("or_high", 35))
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_qty_and_charges_switch(self):
        doc = machine(qty=1, replays=10000)
        self.assertEqual(doc["charges"], {"qty": 1, "qty_source": "--qty", "product": "MIS",
                                          "at_target": 0.73, "at_stop": 0.72})
        self.assertEqual(doc["mc"]["avg_R_net"], -0.429)
        est = machine(plan(charges=None), replays=200)["charges"]
        self.assertEqual((est["qty"], est["qty_source"]), (72, "estimate"))   # min(1000 / 2.80, 50000 / 692.70)
        cfg = tm.read_config(None)
        cfg["include_charges"] = False
        doc = machine(replays=200, cfg=cfg)
        self.assertIsNone(doc["charges"])
        self.assertIsNone(doc["mc"]["avg_R_net"])
        self.assertIsNone(doc["history"]["avg_R_net"])
        self.assertIn("Charges not included", "\n".join(tm.report(doc)))

    def test_config_defaults_with_missing_keys(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "config.json")
            with open(path, "w") as fh:
                json.dump({"capital": 50000, "charges": {"gst_pct": 0}}, fh)
            cfg = tm.read_config(path)
            self.assertEqual((cfg["flat_by"], cfg["tick_size"], cfg["product"], cfg["include_charges"]),
                             ("15:10", "auto", "MIS", True))
            self.assertEqual((cfg["charges"]["gst_pct"], cfg["charges"]["brokerage_cap"]), (0, 20))
            self.assertEqual(tm.estimate_qty(692.7, 2.8, cfg), 72)     # 50,000 x 1% = 500 -> 178, cap 72
            cfg["max_order_value"] = None
            self.assertEqual(tm.estimate_qty(692.7, 2.8, cfg), 178)
            self.assertEqual(tm.read_config(os.path.join(d, "missing.json"))["flat_by"], "15:10")
        finally:
            shutil.rmtree(d)

    def test_the_verdict_thresholds(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 15:10: the price at 15:10 decides every one.")
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095,
              "avg_R_net": -0.02}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), ["Net of charges the average R is -0.020: at this size the charges "
                                                "take the whole edge."])
        lean = dict(mc, p_target=0.15, p_stop=0.55, avg_R_net=0.01)
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])

    def test_1r_on_the_fixture(self):
        doc = machine(plan(target=695.5))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["avg_R"], m["avg_R_net"]),
                         (1.0, 0.4185, 0.5562, -0.138, -0.401))
        self.assertEqual(doc["verdict"], "Leans toward the stop: target first 41.9 % vs stop first 55.6 %.")
        h = doc["history"]
        self.assertEqual((h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]), (14, 16, 3, -0.036, -0.323))


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("hdfcbank"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/HDFCBANK.NS?range=60d&interval=5m")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("BAJAJ-AUTO", "query1"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; bharat-trader/0.3; +https://github.com/rajdavee/bharat-trader)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("HDFCBANK"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("HDFCBANK"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("HDFCBANK")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            code, text, err = run(["run", "--plan", PLAN_FILE, "--config", os.path.join(d, "none.json"),
                                   "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        shutil.copy(PLAN_FILE, self.plan)
        self.out = os.path.join(self.d, "trader", "timemachine.json")
        self.cfg = os.path.join(self.d, "trader", "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def base(self, *extra):
        return ["run", "--plan", self.plan, "--config", self.cfg, "--offline", CHART, "--now", NOW] + list(extra)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(self.base("--write", self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("target first   19.7 %", text)
        self.assertIn("Average R: -0.433 net of charges", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history", "charges") if k in doc),
                         ["charges", "history", "mc", "plan"])
        self.assertEqual((doc["created_by"], doc["source"], doc["created_at"]),
                         ("trader-timemachine", "offline", "2026-10-26T04:45:00Z"))
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["avg_R_net"]), (0.1973, -0.17, -0.433))
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (33, -0.155))
        self.assertEqual(doc["plan"]["from"], self.plan)
        self.assertEqual(doc["data"]["last_bar"], "2026-10-26 10:10 IST")

    def test_json_output_and_flags(self):
        code, text, _ = run(self.base("--json"))
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.6402)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertFalse(os.path.exists(self.out))
        code, text, _ = run(self.base("--json", "--no-charges"))
        self.assertIsNone(json.loads(text)["mc"]["avg_R_net"])
        with open(self.cfg, "w") as fh:
            json.dump({"flat_by": "15:00"}, fh)
        os.remove(self.plan)
        code, text, err = run(["run", "--symbol", "hdfcbank", "--date", "2026-10-26", "--entry", "692.70",
                               "--stop", "689.90", "--target", "698.30", "--entry-time", "10:10",
                               "--config", self.cfg, "--offline", CHART, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["until"], doc["plan"]["stop_rule"], doc["charges"]["qty"]), ("15:00", "or_mid", 72))
        code, _, err = run(["run", "--symbol", "HDFCBANK", "--entry", "692.70", "--offline", CHART])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_archive_adds_older_sessions_to_the_pool(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_lab import archive_with
        root = archive_with(os.path.join(self.d, "archive"), hdfc_july=True)
        code, text, _ = run(self.base("--json", "--replays", "500"))
        plain = json.loads(text)
        code, text, err = run(self.base("--json", "--replays", "500", "--archive", root))
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["source"], "offline+archive")
        self.assertGreaterEqual(doc["mc"]["pool_sessions"], plain["mc"]["pool_sessions"] + 20)
        self.assertTrue(doc["mc"]["pool_from"].startswith("2026-07"))
        self.assertEqual(plain["source"], "offline")
        code, text, _ = run(self.base("--json", "--replays", "500", "--archive", root, "--no-archive"))
        self.assertEqual(json.loads(text)["mc"]["pool_sessions"], plain["mc"]["pool_sessions"])

    def test_offline_folder_and_default_paths(self):
        shutil.copy(CHART, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     self.base("--until", "9:00"),
                     self.base("--qty", "0"),
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(self.base("--write", self.out))
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:10 IST entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("   698.30 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("   689.90 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("   692.70 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("10:10", text)
        self.assertIn("15:10", text)
        self.assertEqual(lines[-1], "Leans toward the stop: at 2R the target has to come first in 33 % of the "
                                    "replays that hit one side; it did in 24 % (target first 19.7 % vs stop first "
                                    "64.0 %).")
        code, again, _ = run(["chart", "--plan", self.plan, "--config", self.cfg, "--offline", CHART, "--now", NOW])
        self.assertEqual(again, text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-timemachine", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-timemachine\n"))
        desc = text.split("description:", 1)[1].split("argument-hint:", 1)[0]
        self.assertGreater(len(" ".join(desc.split())), 200)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("10,000", "seed 19", "09:15 to 15:30 ist", "75 five-minute bars", "15:10", "net of charges",
                     "not a prediction", "/trader-budget", "19.7 %", "64.0 %", "-0.433", "mis",
                     "not financial advice", "typed an explicit yes"):
            self.assertIn(must, low)
        for never in ("—", "pro" + "fit", "ny" + "se", "\x24", "new " + "york", "skill_dir"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
