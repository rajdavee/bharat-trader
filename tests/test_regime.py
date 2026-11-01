"""trader-regime tests, offline.

Fixtures:
  tests/fixtures/trader-lab/^NSEI-1d.csv.gz, ^INDIAVIX-1d.csv.gz, RELIANCE-1d.csv.gz, RELIANCE-5m.csv.gz
      real Yahoo daily (2024-10-30 to 2026-10-30) and 5-minute bars (see tests/test_lab.py)
  tests/fixtures/trader-regime/^NSEBANK-1d.csv.gz
      Nifty Bank daily from Yahoo v8 chart (range=2y, fetched 2026-11-01), the same converter and dates
  tests/fixtures/trader-regime/board-RELIANCE.json
      NSE corporate-board-meetings for RELIANCE (fetched 2026-11-01), trimmed to six fields
  tests/fixtures/trader-archive/
      the real NSE bhavcopies of 21-23 Oct 2026, built into an archive with tests/test_archive.py's fake NSE
"""

import contextlib
import copy
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

rg = load("trader-regime", "regime")
LAB = os.path.join(FIX, "trader-lab")
OWN = os.path.join(FIX, "trader-regime")


def bars(name, folder=LAB):
    return rg.read_bars_file(os.path.join(folder, name))


def data_dir():
    d = tempfile.mkdtemp(prefix="bt-regime-")
    for f in ("^NSEI-1d.csv.gz", "^INDIAVIX-1d.csv.gz", "RELIANCE-1d.csv.gz"):
        shutil.copy(os.path.join(LAB, f), d)
    shutil.copy(os.path.join(OWN, "^NSEBANK-1d.csv.gz"), d)
    shutil.copy(os.path.join(OWN, "board-RELIANCE.json"), d)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = rg.main(argv)
    return rc, out.getvalue(), err.getvalue()


def bar(h, l, c, o=None, d="2026-01-01"):
    return {"date": d, "o": c if o is None else o, "h": h, "l": l, "c": c}


class Maths(unittest.TestCase):
    """Each indicator against numbers worked out by hand."""

    def test_ema_and_sma(self):
        # n=3: seed = mean(1, 2, 3) = 2; alpha 0.5: 0.5*4 + 0.5*2 = 3; 0.5*5 + 0.5*3 = 4
        self.assertEqual(rg.ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(rg.sma([1, 2, 3, 4, 5], 2), [None, 1.5, 2.5, 3.5, 4.5])

    def test_efficiency_ratio(self):
        # |12 - 10| / (1 + 1 + 2) = 0.5
        self.assertEqual(rg.efficiency_ratio([10, 11, 10, 12], 3), [None, None, None, 0.5])
        self.assertEqual(rg.efficiency_ratio([10, 11, 12, 13], 3)[3], 1.0)

    def test_wilder_atr(self):
        h, l, c = [10, 11, 12, 11], [8, 9, 10, 8], [9, 10, 11, 9]
        # TR: 2, 2, 3. ATR(2) at bar 2 = (2 + 2) / 2 = 2; bar 3 = (2 x 1 + 3) / 2 = 2.5
        self.assertEqual(rg.true_ranges(h, l, c), [None, 2, 2, 3])
        self.assertEqual(rg.atr(h, l, c, 2), [None, None, 2.0, 2.5])

    def test_wilder_adx(self):
        h, l, c = [10, 11, 10.5, 9], [8, 9, 7, 7.5], [9, 10, 8, 8.5]
        ax, p, m = rg.adx(h, l, c, 2)
        # bar 2: TR sum 2 + 3.5 = 5.5, +DM 1, -DM 2 -> +DI 18.18, -DI 36.36, DX 33.33
        self.assertAlmostEqual(p[2], 100 / 5.5, 6)
        self.assertAlmostEqual(m[2], 200 / 5.5, 6)
        # bar 3: TR 5.5 - 2.75 + 1.5 = 4.25, +DM 0.5, -DM 1 -> +DI 11.76, -DI 23.53, DX 33.33
        self.assertAlmostEqual(p[3], 50 / 4.25, 6)
        self.assertAlmostEqual(m[3], 100 / 4.25, 6)
        self.assertAlmostEqual(ax[3], 100 / 3.0, 6)       # the mean of the first 2 DX
        self.assertIsNone(ax[2])
        # a straight climb: -DM is 0, so DX = 100 and +DI = 2 / 4 = 50%
        h2, l2, c2 = [i + 2 for i in range(6)], [i for i in range(6)], [i + 1 for i in range(6)]
        ax2, p2, m2 = rg.adx(h2, l2, c2, 2)
        self.assertEqual((p2[2], m2[2], ax2[3], ax2[5]), (50.0, 0.0, 100.0, 100.0))

    def test_realised_vol_and_ranks(self):
        # log returns +0.09531, -0.10536: sample sd = |r1 - r2| / sqrt 2 = 0.14190; x sqrt 252 x 100 = 225.25%
        self.assertAlmostEqual(rg.realised_vol([100, 110, 99], 2)[2], 225.25, 1)
        self.assertEqual(rg.percentile_rank([1, 2, 3, 4], 3), 50.0)
        self.assertEqual(rg.range_rank([10, 20, 15], 15), 50.0)
        self.assertIsNone(rg.range_rank([5, 5], 5))

    def test_gap_in_atrs(self):
        self.assertEqual(rg.gap_type(105, 100, 10), {"type": "gap up", "pct": 5.0, "atr_mult": 0.5})
        self.assertEqual(rg.gap_type(101, 100, 10)["type"], "no gap")
        self.assertEqual(rg.gap_type(90, 100, 10)["type"], "big gap down")
        self.assertEqual(rg.gap_type(None, 100, 10)["type"], "unknown")

    def test_trend_rules(self):
        c = dict(rg.DEFAULTS)
        self.assertEqual(rg.classify_trend(27, 0.1, 110, 105, 100, 0.2, c), ("trend_up", "up"))
        self.assertEqual(rg.classify_trend(18, 0.35, 90, 95, 100, -0.2, c), ("trend_down", "down"))
        self.assertEqual(rg.classify_trend(15, 0.1, 110, 105, 100, 0.2, c), ("range", "up"))
        self.assertEqual(rg.classify_trend(30, 0.1, 99, 105, 100, 0.2, c), ("transition", None))
        self.assertEqual(rg.classify_trend(22, 0.25, 110, 105, 100, 0.2, c), ("transition", "up"))
        self.assertEqual(rg.classify_trend(None, 0.1, 1, 1, 1, 0, c)[0], "unknown")
        self.assertEqual([rg.vol_bucket(x) for x in (10, 50, 80, None)], ["low", "normal", "high", "unknown"])


class RealSeries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nifty, cls.vix = bars("^NSEI-1d.csv.gz"), bars("^INDIAVIX-1d.csv.gz")
        cls.regs = rg.daily_regimes(cls.nifty, cls.vix, "NIFTY")

    def test_every_day_labelled_after_warm_up(self):
        self.assertEqual(len(self.regs), len(self.nifty))
        late = [r for d, r in self.regs.items() if d >= "2025-12-01"]
        self.assertTrue(all(r["trend"]["label"] != "unknown" and r["vol"]["bucket"] != "unknown" for r in late))
        self.assertEqual(self.regs[self.nifty[0]["date"]]["trend"]["label"], "unknown")
        last = self.regs["2026-10-30"]
        self.assertEqual(last["vol"]["vix"], 15.3)
        self.assertEqual(last["vol"]["basis"], "India VIX percentile")
        self.assertTrue(last["words"].startswith("NIFTY: "))
        # the VIX percentile is the share of the last 252 sessions' closes below the day's close
        vals = [b["c"] for b in self.vix if b["date"] <= "2026-10-30"][-252:]
        self.assertAlmostEqual(last["vol"]["vix_pct"], round(100.0 * sum(1 for x in vals if x < 15.3) / len(vals), 1))

    def test_no_look_ahead(self):
        cut = "2026-06-30"
        future = [dict(b) for b in self.nifty]
        fvix = [dict(b) for b in self.vix]
        for b in future:
            if b["date"] > cut:
                b.update(o=b["o"] * 2, h=b["h"] * 3, l=b["l"] * 0.5, c=b["c"] * 1.7)
        for b in fvix:
            if b["date"] > cut:
                b["c"] *= 3
        other = rg.daily_regimes(future, fvix, "NIFTY")
        past = [d for d in self.regs if d <= cut]
        self.assertGreater(len(past), 300)
        for d in past:
            self.assertEqual(json.dumps(other[d], sort_keys=True), json.dumps(self.regs[d], sort_keys=True), d)
        self.assertNotEqual(json.dumps(other["2026-10-30"]), json.dumps(self.regs["2026-10-30"]))
        # one day from the whole history is the same as from history cut at that day
        self.assertEqual(rg.regime_on("NIFTY", cut, self.nifty, self.vix), self.regs[cut])

    def test_stock_uses_its_own_realised_vol(self):
        rel = rg.daily_regimes(bars("RELIANCE-1d.csv.gz"), self.vix, "RELIANCE")["2026-10-30"]
        self.assertEqual(rel["kind"], "stock")
        self.assertEqual(rel["vol"]["basis"], "20-day realised vol percentile")
        self.assertIsNone(rel["vol"]["iv_rank"])          # India VIX is NIFTY's implied vol, not the stock's

    def test_config_thresholds(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "regime-config.json")
            with open(p, "w") as fh:
                json.dump({"adx_trend": 99, "er_trend": 2, "vol_low_pct": 80}, fh)
            cfg = rg.load_config(p)
            regs = rg.daily_regimes(self.nifty, self.vix, "NIFTY", cfg=cfg)
            self.assertFalse([r for r in regs.values() if r["trend"]["class"] == "trend"])
            self.assertNotEqual(rg.config_hash(cfg), rg.config_hash(rg.DEFAULTS))
            with open(p, "w") as fh:
                json.dump({"adx_trand": 20}, fh)
            with self.assertRaises(rg.RegimeError):
                rg.load_config(p)
        finally:
            shutil.rmtree(d)


class Calendar(unittest.TestCase):
    def test_expiry_rule_with_holiday_shifts(self):
        e = rg.expiry_info("NIFTY", "2026-11-09")       # Tue 10 Nov is a holiday: the weekly moves to Monday
        self.assertEqual((e["today"], e["source"][:5]), ("weekly", "rule:"))
        e = rg.expiry_info("NIFTY", "2026-11-06")       # Friday: the next session is that Monday
        self.assertEqual((e["today"], e["next_session"], e["sessions_to_expiry"]), (None, "weekly", 1))
        e = rg.expiry_info("BANKNIFTY", "2026-11-23")   # Tue 24 Nov is a holiday: the monthly moves to Monday
        self.assertEqual(e["today"], "monthly")
        self.assertIsNone(rg.expiry_info("BANKNIFTY", "2026-11-17")["today"])   # no weekly for Bank Nifty
        self.assertEqual(rg.expiry_info("NIFTY", "2026-11-17")["today"], "weekly")
        e = rg.expiry_info("NIFTY", "2025-08-28")       # before 1 Sep 2025: Thursday
        self.assertEqual(e["today"], "monthly")
        self.assertIn("2024/132", e["source"])
        self.assertFalse(rg.expiry_info("NIFTY", "2024-06-27")["known"])

    def test_expiry_from_the_archived_bhavcopy(self):
        ta = load_test_archive()
        root = tempfile.mkdtemp()
        try:
            ta.build(root)
            e = rg.expiry_info("NIFTY", "2026-10-23", root)
            self.assertEqual((e["next_expiry"], e["next_kind"], e["sessions_to_expiry"]), ("2026-10-27", "monthly", 2))
            self.assertEqual(e["source"], "archive F&O bhavcopy 2026-10-23")
            e = rg.expiry_info("NIFTY", "2026-10-26", root)      # Monday: no file that day, Friday's list holds
            self.assertEqual(e["next_session"], "monthly")
            self.assertEqual(rg.expiry_info("RELIANCE", "2026-10-23", root)["next_expiry"], "2026-10-27")
            tcs = rg.expiry_info("TCS", "2026-10-23", root)      # in the cash file, not in this F&O file
            self.assertEqual((tcs["known"], tcs["next_expiry"]), (True, None))
            # breadth from the day's equity bhavcopy, counted by hand from the NSE file
            with zipfile.ZipFile(os.path.join(FIX, "trader-archive", "BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip")) as z:
                text = z.read(z.namelist()[0]).decode("utf-8")
            import csv
            rows = [r for r in csv.DictReader(io.StringIO(text)) if r["SctySrs"] == "EQ"]
            adv = sum(1 for r in rows if float(r["ClsPric"]) > float(r["PrvsClsgPric"]))
            dec = sum(1 for r in rows if float(r["ClsPric"]) < float(r["PrvsClsgPric"]))
            got = rg.Sources(root, None, False, root).breadth("2026-10-23")
            self.assertEqual((got["advances"], got["declines"]), (adv, dec))
        finally:
            shutil.rmtree(root)

    def test_events_and_sessions(self):
        cal = rg.calendar_flags("NIFTY", "2026-10-07", events=rg.builtin_events())
        self.assertEqual([e["kind"] for e in cal["events"]], ["rbi_policy"])
        self.assertIn("prid=62422", cal["events"][0]["source"])
        cal = rg.calendar_flags("NIFTY", "2026-10-06", events=rg.builtin_events())
        self.assertEqual([e["kind"] for e in cal["events_next_session"]], ["rbi_policy"])
        cal = rg.calendar_flags("NIFTY", "2026-02-01", events=rg.builtin_events())
        self.assertEqual(cal["special_session"], "Union Budget (Sunday)")
        self.assertEqual([e["kind"] for e in cal["events"]], ["budget"])
        self.assertEqual(rg.calendar_flags("NIFTY", "2026-11-23")["holiday_before_next_session"],
                         "Prakash Gurpurb Sri Guru Nanak Dev")
        self.assertIsNone(rg.calendar_flags("NIFTY", "2026-10-30")["holiday_before_next_session"])
        s = rg.calendar_flags("RELIANCE", "2026-10-29", "stock")["session"]
        self.assertEqual((s["closing_auction"], s["continuous_end"]), ("15:15-15:35", "15:15"))
        self.assertEqual(rg.calendar_flags("NIFTY", "2026-12-07")["session"]["continuous_end"], "15:45")
        for e in rg.builtin_events():
            self.assertTrue(e["source"] and e["kind"] in ("rbi_policy", "budget"), e)

    def test_results_days_from_board_meetings(self):
        with open(os.path.join(OWN, "board-RELIANCE.json"), encoding="utf-8") as fh:
            meetings = rg.parse_board(json.load(fh))
        self.assertIn("2026-10-30", rg.results_days(meetings))
        flags = lambda d: rg.calendar_flags("RELIANCE", d, "stock", meetings=meetings)
        self.assertTrue(flags("2026-10-30")["results_today"])
        self.assertTrue(flags("2026-10-29")["results_next_session"])
        self.assertTrue(flags("2026-11-02")["after_results"])
        self.assertFalse(flags("2026-10-28")["results_today"])

    def test_time_buckets_follow_the_dated_session_rules(self):
        at = lambda s, seg="EQ_FNO": rg.time_bucket(rg.parse_now(s), seg)["bucket"]
        self.assertEqual(at("2026-10-29 09:05"), "pre-open")
        self.assertEqual(at("2026-10-29 09:20"), "open drive")
        self.assertEqual(at("2026-10-29 10:00"), "morning")
        self.assertEqual(at("2026-10-29 12:00"), "midday")
        self.assertEqual(at("2026-10-29 14:00"), "afternoon")
        self.assertEqual(at("2026-10-29 15:00"), "closing window")      # continuous trading ends 15:15
        self.assertEqual(at("2026-10-29 15:20"), "closing auction")
        self.assertEqual(at("2026-10-29 15:20", "DERIV"), "closing window")   # derivatives trade to 15:40
        self.assertEqual(at("2026-10-29 15:20", "EQ"), "closing window")
        self.assertEqual(at("2026-12-07 15:10"), "closing window")       # from 7 Dec 2026: to 15:30, CAS to 15:40
        self.assertEqual(at("2026-12-07 15:35"), "closing auction")
        self.assertEqual(at("2026-07-01 15:20"), "closing window")       # before the CAS
        self.assertEqual(at("2026-11-01 11:00"), "closed")

    def test_intraday_uses_bars_complete_by_now(self):
        five = bars("RELIANCE-5m.csv.gz")
        r = rg.intraday_read(five, rg.parse_now("2026-10-29 10:20"), 1158.0, 27.3)
        self.assertEqual(r["through"], "10:15")      # the 10:15 bar closes at 10:20; the 10:20 bar is still open
        self.assertEqual(r["bars"], len([b for b in five if b["date"] == "2026-10-29" and b["time"] <= "10:15"]))


def load_test_archive():
    import importlib
    return importlib.import_module("test_archive")


class Lab(unittest.TestCase):
    """trader-lab splits its results by these regimes and can require an edge in today's."""

    @classmethod
    def setUpClass(cls):
        cls.lab = load("trader-lab", "lab")
        cls.ctx = {"nifty": cls.lab.Series("^NSEI", "1d", bars("^NSEI-1d.csv.gz")),
                   "vix": cls.lab.Series("^INDIAVIX", "1d", bars("^INDIAVIX-1d.csv.gz"))}
        cls.regs = cls.lab.lab_regimes(cls.ctx)

    def test_trades_take_the_regime_of_the_close_before(self):
        trades = [{"signal_date": "2026-10-29", "net": 120.0, "r": 0.4}, {"signal_date": "2026-11-03", "net": -50.0,
                                                                         "r": -0.2}]
        lab_trades = rg.label_trades(trades, self.regs)
        prev = self.regs["2026-10-28"]
        self.assertEqual(lab_trades[0][1], "%s|%s|no expiry" % (prev["trend"]["class"], prev["vol"]["bucket"]))
        self.assertTrue(lab_trades[1][1].endswith("|expiry day"))    # Tue 3 Nov, after the last bar: known ahead
        rows = rg.split_by_regime(trades, self.regs, 10)
        self.assertEqual(sum(r["trades"] for r in rows), 2)
        self.assertFalse(any(r["enough"] for r in rows))

    def test_run_lab_reports_regimes_and_the_opt_in_gate(self):
        import test_lab as tl
        doc = tl.spec_doc("rsi2-nifty50")
        data = tl.data_for(doc["universe"], doc["timeframe"])
        res = self.lab.run_lab(doc, data, self.ctx)
        mk = res["regimes"]["market"]
        self.assertEqual(sum(r["trades"] for r in mk["whole_sample"]), res["full"]["trades"])
        self.assertEqual(mk["current"]["date"], "2026-10-30")
        self.assertFalse([g for g in res["gates"] if "regime" in g["gate"]])     # off unless asked for
        text = self.lab.scorecard(dict(res, verdict="REJECT", verdict_why="test"))
        self.assertIn("By market regime", text)
        doc2 = copy.deepcopy(doc)
        doc2.setdefault("gates", {})["require_regime_edge"] = 1
        res2 = self.lab.run_lab(doc2, data, self.ctx)
        g = [x for x in res2["gates"] if x["gate"].startswith("edge in the current regime")]
        self.assertEqual(len(g), 1)
        cur = mk["current"]["key"]
        want = rg.edge_in_regime(res2["oos_trades"], self.regs, cur, 10, 0.0)
        self.assertEqual(g[0]["pass"], want["pass"])
        self.assertIn("%d trades" % want["trades"], g[0]["got"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = data_dir()
        self.work = tempfile.mkdtemp(prefix="bt-regime-work-")

    def tearDown(self):
        shutil.rmtree(self.d)
        shutil.rmtree(self.work)

    def test_now_plain_and_json(self):
        rc, out, _ = run(["now", "--data", self.d, "--now", "2026-10-30 16:00", "--workdir", self.work])
        self.assertEqual(rc, 0)
        self.assertIn("NIFTY: ", out)
        self.assertIn("BANKNIFTY: ", out)
        self.assertIn("next weekly 2026-11-03, 2 sessions away", out)
        rc, out, _ = run(["now", "RELIANCE", "--data", self.d, "--now", "2026-10-29 10:20", "--json",
                          "--workdir", self.work])
        doc = json.loads(out)
        r = doc["regimes"][0]
        self.assertEqual(r["daily"]["date"], "2026-10-28")              # the last complete session
        self.assertEqual(r["today"]["time"]["bucket"], "morning")
        self.assertTrue(r["today"]["calendar"]["results_next_session"])  # the board meets on Fri 30 Oct
        self.assertEqual(doc["errors"], [])

    def test_history_stores_files_and_explain(self):
        rc, out, _ = run(["history", "NIFTY", "--from", "2026-10-01", "--to", "2026-10-30", "--data", self.d,
                          "--workdir", self.work, "--now", "2026-11-01 10:00"])
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.work, "regime", "NIFTY.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["from"], doc["to"]), ("2026-10-01", "2026-10-30"))
        self.assertEqual(doc["days"]["2026-10-07"]["events"], ["rbi_policy"])
        rc, out, _ = run(["explain", "NIFTY", "--data", self.d, "--now", "2026-10-30 16:00", "--json",
                          "--workdir", self.work])
        ex = json.loads(out)
        self.assertEqual(ex["thresholds"]["adx_trend"], 25.0)
        self.assertIn("ADX(14)", [x["input"] for x in ex["inputs"]])

    def test_history_writes_the_archive_table(self):
        ta = load_test_archive()
        root = os.path.join(self.work, "archive")
        ta.build(root)
        rc, out, err = run(["history", "NIFTY", "--archive", root, "--offline", "--workdir", self.work,
                            "--now", "2026-10-26 08:00"])
        self.assertEqual(rc, 0, err)
        rows = rg.load_table("NIFTY", root=root)
        self.assertEqual([r["date"] for r in rows], ["2026-10-21", "2026-10-22", "2026-10-23"])
        self.assertEqual(rows[-1]["expiry_today"], "")
        self.assertTrue(rows[0]["words"].startswith("NIFTY: "))

    def test_no_data_is_a_plain_message(self):
        rc, out, err = run(["now", "TCS", "--offline", "--no-archive", "--workdir", self.work])
        self.assertEqual(rc, 1)
        self.assertIn("fetching is off", out)


if __name__ == "__main__":
    unittest.main()
