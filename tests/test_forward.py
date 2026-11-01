"""The forward-test dashboard (trader-lab forward-report), offline, on REAL trades.

tests/fixtures/trader-lab/forward/orb15-vwap/
    results.json   cut from a real trader-lab run of orb15-vwap on the real 5-minute bars in tests/fixtures/trader-lab
                   (5 Aug to 30 Oct 2026): its 65 out-of-sample trades after charges are the backtest's expectation
    forward.json   20 real trades of the same run that are NOT in the expectation (in-sample ones), used as the
                   paper forward record
    status.json    set to WATCH for the test (the real run REJECTs the strategy: these bars show no edge)

A decayed record is made from the same real trades: the losers only, in their real order.
"""

import contextlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

lab = load("trader-lab", "lab")
ft = lab.engines_api().fwdtest
SRC = os.path.join(FIX, "trader-lab", "forward", "orb15-vwap")
NAME = "orb15-vwap"


def read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return json.load(fh)


class Stats(unittest.TestCase):
    def setUp(self):
        self.res, self.fwd, self.status = read("results.json"), read("forward.json"), read("status.json")

    def test_backtest_expectation_is_the_out_of_sample_record(self):
        bt = ft.backtest_expectation(self.res)
        st = self.res["walk_forward"]["stats"]
        self.assertEqual(bt["trades"], 65)
        self.assertEqual(bt["trades"], st["trades"])
        self.assertAlmostEqual(bt["mean_r"], st["expectancy_r"], places=3)
        self.assertAlmostEqual(bt["win_rate"], st["win_rate"], places=3)
        self.assertAlmostEqual(bt["trades_per_session"], 65 / 30.0, places=3)

    def test_predicted_range_is_a_prediction_interval(self):
        bt = ft.backtest_expectation(self.res)
        pr = ft.predicted(bt, 20)
        half = ft.Z * bt["sd_r"] * math.sqrt(1 / 20.0 + 1 / 65.0)
        self.assertAlmostEqual(pr["mean_r"][0], bt["mean_r"] - half, places=3)
        self.assertAlmostEqual(pr["mean_r"][1], bt["mean_r"] + half, places=3)
        # wider for a shorter record, never narrower than the backtest's own noise allows
        self.assertGreater(ft.predicted(bt, 5)["mean_r"][1] - ft.predicted(bt, 5)["mean_r"][0], 2 * half)
        lo, hi = pr["win_rate"]
        self.assertTrue(0 <= lo < bt["win_rate"] < hi <= 1)
        self.assertIsNone(ft.predicted(bt, 0))

    def test_real_forward_record_inside_the_range_and_no_alert(self):
        row = ft.strategy_row(NAME, self.res, self.status, self.fwd)
        self.assertEqual(row["forward"]["trades"], 20)
        self.assertEqual(row["where"], "inside")
        self.assertEqual(row["win_rate_where"], "inside")
        self.assertFalse(row["decay"])
        self.assertIsNone(row["recommendation"])
        lo, hi = row["forward"]["mean_r_ci"]
        self.assertLess(lo, row["forward"]["mean_r"])
        self.assertGreater(hi, row["forward"]["mean_r"])
        # PROMOTE needs 20 forward trades with a mean above 0R: this record has the trades, not the mean
        el = row["eligibility"]
        self.assertEqual((el["need"], el["have"], el["trades_left"], el["eligible_now"]), (20, 20, 0, False))
        self.assertIn("not met", el["words"])

    def test_losers_only_record_decays_and_recommends_demote(self):
        losers = dict(self.fwd, trades=[t for t in self.fwd["trades"] if t["r"] < 0])
        self.assertGreaterEqual(len(losers["trades"]), 10)
        row = ft.strategy_row(NAME, self.res, self.status, losers)
        self.assertEqual(row["where"], "below")
        self.assertTrue(row["decay"])
        self.assertGreaterEqual(row["below_run"], ft.SETTINGS["decay_run"])
        self.assertIsNotNone(row["control_chart"]["alarm_at"])
        self.assertEqual(row["recommendation"]["action"], "DEMOTE")
        self.assertIn("--demote orb15-vwap", row["recommendation"]["how"])
        self.assertIn("Decay alert", row["words"])
        # a REJECT strategy is not in a forward test: an alert, but nothing to demote
        row = ft.strategy_row(NAME, self.res, dict(self.status, verdict="REJECT"), losers)
        self.assertTrue(row["decay"])
        self.assertIsNone(row["recommendation"])

    def test_no_alert_before_the_minimum_trades(self):
        few = dict(self.fwd, trades=[t for t in self.fwd["trades"] if t["r"] < 0][:ft.SETTINGS["decay_min_trades"] - 1])
        row = ft.strategy_row(NAME, self.res, self.status, few)
        self.assertEqual(row["where"], "below")      # judged from 5 trades
        self.assertFalse(row["decay"])               # but no alert under 10

    def test_cusum_thresholds(self):
        c = ft.cusum_lower([0.0] * 50, 0.0, 1.0)
        self.assertIsNone(c["alarm_at"])               # on target: never alarms
        self.assertEqual(c["last"], 0.0)
        c = ft.cusum_lower([-1.0] * 20, 0.0, 1.0, k=0.5, h=4.0)
        self.assertEqual(c["alarm_at"], 9)             # 0.5 a trade over the allowance: past 4 at trade 9
        self.assertEqual(ft.cusum_lower([1.0], None, None)["alarm_at"], None)

    def test_other_spec_hash_and_end_of_data_ignored(self):
        doc = dict(self.fwd, trades=[dict(t, spec_hash="other") for t in self.fwd["trades"][:5]] +
                   [dict(self.fwd["trades"][5], reason="end of data")] + self.fwd["trades"][6:])
        row = ft.strategy_row(NAME, self.res, self.status, doc)
        self.assertEqual(row["forward"]["trades"], 14)

    def test_timeline_merges_the_co_trader_demotion(self):
        row = ft.strategy_row(NAME, self.res, self.status, self.fwd, demoted={"at": "2026-10-20 16:00", "why": "overconfident"})
        self.assertEqual([x["verdict"] for x in row["timeline"]], ["WATCH", "DEMOTE"])
        self.assertEqual(row["timeline"][-1]["source"], "trader-cotrader")


class Report(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-fwd-")
        self.lab = os.path.join(self.dir, "trader", "lab")
        shutil.copytree(SRC, os.path.join(self.lab, NAME))
        self.now = lab.ist_now("2026-11-01 16:00")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def decay(self):
        p = os.path.join(self.lab, NAME, "forward.json")
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["trades"] = [t for t in doc["trades"] if t["r"] < 0]
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def test_report_reads_the_lab_folder(self):
        rep = lab.forward_report(self.lab, self.dir, self.now)
        self.assertEqual([r["strategy"] for r in rep["strategies"]], [NAME])
        self.assertEqual(rep["decay_alerts"], [])
        self.assertIn("no co-trader predictions", rep["calibration_note"])
        self.assertIn("1 strategy in a forward test", rep["words"])
        md = lab.forward_report_md(rep)
        self.assertIn("inside the range", md)
        self.assertIn("not financial advice", md)

    def test_calibration_from_the_co_trader_log(self):
        fwd = read("forward.json")["trades"]
        os.makedirs(os.path.join(self.dir, "trader", "cotrader"))
        with open(os.path.join(self.dir, "trader", "cotrader", "log.jsonl"), "w", encoding="utf-8") as fh:
            for t in fwd:            # a test log: one logged probability (0.6) per real forward trade
                fh.write(json.dumps({"id": "%s-%s" % (t["signal_date"], t["symbol"]), "strategy": NAME, "tier": "WATCH",
                                     "symbol": t["symbol"], "date": t["signal_date"], "signal_date": t["signal_date"],
                                     "timeframe": "5m", "p": 0.6}) + "\n")
        rep = lab.forward_report(self.lab, self.dir, self.now)
        cal = rep["strategies"][0]["calibration"]
        wins = sum(1 for t in fwd if t["net"] > 0)
        want = (wins * 0.4 ** 2 + (len(fwd) - wins) * 0.6 ** 2) / len(fwd)
        self.assertEqual(cal["n"], 20)
        self.assertAlmostEqual(cal["brier"], want, places=3)

    def test_demote_only_when_recommended(self):
        with self.assertRaises(lab.LabError):
            lab.apply_forward_demote(self.lab, NAME, self.now, self.dir)       # inside the range: nothing to demote
        self.decay()
        rep = lab.forward_report(self.lab, self.dir, self.now)
        self.assertEqual(rep["decay_alerts"], [NAME])
        with open(os.path.join(self.lab, NAME, "status.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["verdict"], "WATCH")                   # read only
        st = lab.apply_forward_demote(self.lab, NAME, self.now, self.dir)
        self.assertEqual(st["verdict"], "DEMOTE")
        self.assertTrue(st["why"].startswith("forward test:"))
        self.assertEqual(st["history"][-1]["verdict"], "DEMOTE")

    def test_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lab.main(["--out", self.lab, "forward-report", "--workdir", self.dir, "--now", "2026-11-01 16:00",
                           "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["strategies"][0]["where"], "inside")
        self.decay()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lab.main(["--out", self.lab, "forward-report", "--workdir", self.dir, "--demote", NAME]), 0)
        self.assertIn("now DEMOTE", out.getvalue())

    def test_ui_route_and_notify_test(self):
        sys.path.insert(0, os.path.join(ROOT, "ui"))
        import api
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        ctx = api.Context(self.dir, now="2026-11-02 10:00")
        status, d = api.dispatch(ctx, "GET", "/api/forward", {}, None)
        self.assertEqual(status, 200, d)
        self.assertEqual(d["strategies"][0]["strategy"], NAME)
        self.assertIn("holdout", d["capture"])
        self.assertFalse(d["notify"]["settings"]["enabled"])
        status, d = api.dispatch(ctx, "POST", "/api/notify/test", {}, {})
        self.assertEqual(status, 200, d)
        self.assertFalse(d["sent"])                       # off by default: nothing sent
        self.assertEqual(api.dispatch(ctx, "POST", "/api/notify/test", {}, {"channel": "sms"})[0], 400)

    def test_thresholds_from_the_config(self):
        rep = lab.forward_report(self.lab, self.dir, self.now, cfg={"forward_tests": {"decay_run": 3}})
        self.assertEqual(rep["settings"]["decay_run"], 3)
        with self.assertRaises(lab.LabError):
            lab.forward_report(self.lab, self.dir, self.now, cfg={"forward_tests": {"nope": 1}})

    def test_reject_without_forward_trades_is_skipped(self):
        st = os.path.join(self.lab, NAME, "status.json")
        with open(st, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.update(verdict="REJECT", history=doc["history"][:1])
        with open(st, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.remove(os.path.join(self.lab, NAME, "forward.json"))
        rep = lab.forward_report(self.lab, self.dir, self.now)
        self.assertEqual(rep["strategies"], [])
        self.assertEqual(rep["skipped"][0]["strategy"], NAME)


class AutopilotEndOfDay(unittest.TestCase):
    """The auto-pilot runs the dashboard at the end of the day: a decay alert is notified and, only with
    "decay_demote": true, applied."""

    def setUp(self):
        self.ap = load("trader-autopilot", "autopilot")
        self.dir = tempfile.mkdtemp(prefix="bt-fwd-ap-")
        lab_dir = os.path.join(self.dir, "trader", "lab", NAME)
        shutil.copytree(SRC, lab_dir)
        p = os.path.join(lab_dir, "forward.json")
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["trades"] = [t for t in doc["trades"] if t["r"] < 0]
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def pilot(self, **block):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["autopilot"] = dict({"strategies": {NAME: {"capital": 50000}}}, **block)
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        p = self.ap.Pilot(self.dir, self.ap.Market(self.dir, None, None, fetch=False), fetch=False)
        p.load_config()
        told = []
        p.notify = lambda event, title, text, key=None: told.append((event, text))
        return p, told

    def status(self):
        with open(os.path.join(self.dir, "trader", "lab", NAME, "status.json"), encoding="utf-8") as fh:
            return json.load(fh)["verdict"]

    def test_decay_is_recommended_and_notified(self):
        p, told = self.pilot()
        out = p.forward_tests("2026-11-02", lab.ist_now("2026-11-02 15:45"))
        self.assertEqual(out["decay_alerts"], [NAME])
        self.assertEqual(out["rows"][0]["recommendation"]["action"], "DEMOTE")
        self.assertEqual(out["demoted"], [])
        self.assertEqual(self.status(), "WATCH")
        self.assertEqual(told[0][0], "decay_alert")
        md = self.ap.report_md({"date": "2026-11-02", "mode": "paper", "phases": {}, "totals": {
            "fills": 0, "closed": 0, "charges": 0, "realised": 0}, "by_strategy": {}, "closed_trades": [],
            "open_positions": [], "forward_tests": out, "timeline": []})
        self.assertIn("Recommended: DEMOTE", md)

    def test_decay_demote_on(self):
        p, told = self.pilot(decay_demote=True)
        out = p.forward_tests("2026-11-02", lab.ist_now("2026-11-02 15:45"))
        self.assertEqual(out["demoted"], [NAME])
        self.assertEqual(self.status(), "DEMOTE")


if __name__ == "__main__":
    unittest.main()
