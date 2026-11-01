"""trader-discover tests, offline.

Two kinds of data:
  - synthetic daily bars made here, seeded: a pure random walk (no edge at all: every rule is noise) and
    the same walk with a planted edge (after a close above the prior 20 sessions' high, the next five
    sessions drift up 0.6% a day). The overfitting controls must reject the first and find the second.
  - the real NSE bars trader-lab's tests use (tests/fixtures/trader-lab: ten NIFTY 50 stocks, Yahoo
    daily bars 2024-10-30 to 2026-10-30 and 5-minute bars 2026-08-05 to 2026-10-30, NIFTY 50 and India
    VIX daily), for runs on real history.
"""

import datetime
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

disc = load("trader-discover", "discover")
lab = disc.lab_api()
LABFIX = os.path.join(FIX, "trader-lab")
NOW = lab.ist_now("2026-11-01 12:00")


def sessions(n, start="2021-01-04"):
    d, out = datetime.date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def market(seed, plant=False, symbols=6, n=700, vol=0.015, edge=0.006):
    """({symbol: Series}, {"nifty": equal-weight index}) of synthetic daily bars. Without plant: a driftless
    random walk. With plant: after a close above the prior 20 highs, five sessions of +edge drift."""
    rng = random.Random(seed)
    days, data = sessions(n), {}
    for k in range(symbols):
        sym = "SYN%s" % chr(65 + k)
        c, bars, highs, boost = 1000.0, [], [], 0
        for d in days:
            drift = edge if plant and boost > 0 else 0.0
            boost = max(0, boost - 1)
            o = c * (1 + rng.gauss(0, 0.002))
            cl = o * (1 + drift + rng.gauss(0, vol))
            h = max(o, cl) * (1 + abs(rng.gauss(0, 0.004)))
            lo = min(o, cl) * (1 - abs(rng.gauss(0, 0.004)))
            if plant and len(highs) >= 20 and cl > max(highs[-20:]):
                boost = 5
            highs.append(h)
            bars.append({"date": d, "time": "", "o": round(o, 2), "h": round(h, 2), "l": round(lo, 2),
                         "c": round(cl, 2), "v": 100000})
            c = cl
        data[sym] = lab.Series(sym, "1d", bars)
    idx = []
    for i, d in enumerate(days):
        v = sum(s.c[i] / s.c[0] for s in data.values()) / len(data) * 10000
        idx.append({"date": d, "time": "", "o": v, "h": v * 1.003, "l": v * 0.997, "c": v, "v": 0})
    return data, {"nifty": lab.Series("^NSEI", "1d", idx), "vix": None}


def run(tmp, data, ctx, budget, seed, costs="normal", name="t", lab_out=None, cancel=None, bootstrap=200,
        tf="1d", fno=None):
    return disc.start(lab, name, tf, ",".join(sorted(data)), budget, seed, out=os.path.join(tmp, "discover"),
                      lab_out=lab_out, data=data, ctx=ctx, fno=fno, now=NOW, cancel=cancel,
                      settings={"bootstrap": bootstrap, "costs": costs})


def fixture_market(tf, symbols=disc.LAB10):
    return disc.load_market(lab, list(symbols), tf, LABFIX, None, False, NOW, None)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt-discover-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- the statistics

class Statistics(unittest.TestCase):
    def test_pbo_matches_trader_labs_cscv(self):
        rng = random.Random(3)
        cols = [[rng.gauss(0.001 * (k % 3), 0.01) for _ in range(64)] for k in range(7)]
        matrix = [list(r) for r in zip(*cols)]
        mine, theirs = disc.pbo_fast(cols, 8), lab.pbo_cscv(matrix, 8)
        self.assertEqual(mine["pbo"], theirs["pbo"])
        self.assertEqual(mine["splits"], 70)
        self.assertAlmostEqual(mine["median_logit"], theirs["median_logit"], places=3)
        self.assertIsNone(disc.pbo_fast(cols[:1], 8))

    def test_benjamini_hochberg(self):
        self.assertEqual(disc.bh_threshold([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205], 8, 0.05), 0.008)
        self.assertEqual(disc.bh_threshold([0.2, 0.5], 2, 0.1), 0.0)
        # trials without a p-value count as p = 1: a bigger family makes the cut harder
        self.assertEqual(disc.bh_threshold([0.01], 1, 0.1), 0.01)
        self.assertEqual(disc.bh_threshold([0.01], 20, 0.1), 0.0)

    def test_stationary_bootstrap_blocks_cover_the_sample(self):
        rng = random.Random(1)
        for _ in range(50):
            st, en = disc.stationary_blocks(rng, 100, 5)
            self.assertEqual(sum(e - s for s, e in zip(st, en)), 100)
            self.assertTrue(all(0 <= s < 100 and s < e <= 200 for s, e in zip(st, en)))
        st, en = disc.stationary_blocks(rng, 50, 1)
        self.assertEqual(len(st), 50)                  # block length 1: the plain bootstrap

    def test_reality_check_is_calibrated_on_noise(self):
        """No column has an edge: the SPA p-value of the best of ten falls under 5% about 5% of the time."""
        low = 0
        for seed in range(40):
            rng = random.Random(seed)
            cols = [[rng.gauss(0, 0.01) for _ in range(120)] for _ in range(10)]
            low += disc.bootstrap_tests(cols, 200, None, seed)["spa_p"] < 0.05
        self.assertLessEqual(low, 5)

    def test_reality_check_sees_a_real_edge(self):
        rng = random.Random(5)
        cols = [[rng.gauss(0, 0.01) for _ in range(250)] for _ in range(20)]
        cols[7] = [rng.gauss(0.004, 0.01) for _ in range(250)]
        bs = disc.bootstrap_tests(cols, 300, None, 5)
        self.assertLess(bs["spa_p"], 0.01)
        self.assertEqual(min(range(20), key=lambda k: bs["p"][k]), 7)
        self.assertLess(bs["p"][7], 0.01)
        self.assertGreater(sorted(bs["p"])[10], 0.1)

    def test_deflated_sharpe_charges_for_trials(self):
        rng = random.Random(2)
        x = [rng.gauss(0.0015, 0.01) for _ in range(300)]
        one = disc.deflated_sharpe(lab, x, [], 1)
        many = disc.deflated_sharpe(lab, x, [rng.gauss(0, 0.06) for _ in range(500)], 500)
        self.assertEqual(one["sharpe_hurdle"], 0.0)
        self.assertGreater(many["sharpe_hurdle"], 0.1)
        self.assertLess(many["dsr"], one["dsr"])
        # the luck variance never drops below one sample's: 1 / (T - 1)
        self.assertAlmostEqual(disc.trial_sharpe_variance([0.1] * 10, 301), 1 / 300.0)


# ---------------------------------------------------------------- the search space and the split

class Space(unittest.TestCase):
    def test_every_point_is_a_valid_lab_spec(self):
        for tf in ("1d", "5m"):
            pts = disc.space(tf)
            self.assertGreater(len(pts), 300)
            self.assertEqual(len(set(disc.trial_id(t) for t in pts)), len(pts))
            for t in pts:
                sp = lab.validate_spec(disc.build_doc(tf, t, ["RELIANCE"]))
                self.assertEqual(sp["timeframe"], tf)
                self.assertEqual(sp["product"], "MIS" if tf == "5m" else "CNC")

    def test_budget_is_fixed_and_seeded(self):
        a = disc.sample_trials("1d", 50, 7)
        self.assertEqual(a, disc.sample_trials("1d", 50, 7))
        self.assertNotEqual(a, disc.sample_trials("1d", 50, 8))
        self.assertEqual(len(set(disc.trial_id(t) for t in a)), 50)
        self.assertEqual(disc.sample_trials("1d", 5, 1, "grid"), disc.space("1d")[:5])
        with self.assertRaises(disc.DiscoverError):
            disc.sample_trials("1d", 10 ** 5, 1)

    def test_neighbours_move_one_axis_one_step(self):
        t = {"family": "breakout", "params": {"look": 20, "stop_atr": 1.5, "target_r": 3, "hold": 40},
             "choices": {"trend": "none", "exit": "hold"}}
        nb = disc.neighbours("1d", t)
        self.assertEqual(len(nb), 6)                  # look 10/50, stop 2, target 2/4, hold 20
        for n in nb:
            diff = [k for k in t["params"] if n["params"][k] != t["params"][k]]
            self.assertEqual(len(diff), 1)
            self.assertEqual(n["choices"], t["choices"])
        g = disc.lab_grid("1d", t)
        self.assertEqual(g, {"look": [10, 20, 50], "stop_atr": [1.5, 2], "target_r": [2, 3, 4]})

    def test_split_by_time_with_embargo_and_purge(self):
        days = sessions(400)
        seg = disc.split_days(days, "1d", disc.SETTINGS)
        self.assertEqual(len(seg["train"]), 200)
        self.assertEqual(len(seg["validation"]), 100)
        self.assertEqual(days.index(seg["validation"][0]) - days.index(seg["train"][-1]), 6)
        self.assertEqual(days.index(seg["holdout"][0]) - days.index(seg["validation"][-1]), 6)
        self.assertLess(seg["validation"][-1], seg["holdout"][0])
        trades = [{"signal_date": seg["train"][-1], "exit_date": seg["validation"][0], "reason": "x"},
                  {"signal_date": seg["train"][3], "exit_date": seg["train"][5], "reason": "x"}]
        kept, purged = disc.segment_trades(trades, seg["train"])
        self.assertEqual((len(kept), purged), (1, 1))
        with self.assertRaises(disc.DiscoverError):
            disc.split_days(days[:20], "1d", disc.SETTINGS)


# ---------------------------------------------------------------- the controls on synthetic data

class NoEdge(Tmp):
    def test_random_walk_gives_no_finalists_without_costs(self):
        """No edge and no charges either: only the statistics stand between noise and a finalist. Zero
        finalists in at least 95% of seeds (here: every one of 20)."""
        found = []
        for seed in range(20):
            data, ctx = market(seed)
            plan, res, msg = run(os.path.join(self.tmp, str(seed)), data, ctx, 40, seed, costs="none")
            self.assertEqual(msg, "decided")
            self.assertEqual(res["counts"]["search_trials"], 40)
            found.append(res["counts"]["finalists"])
        self.assertGreaterEqual(sum(1 for f in found if f == 0), 19, found)

    def test_random_walk_gives_no_finalists_with_costs(self):
        found = []
        for seed in range(100, 110):
            data, ctx = market(seed)
            plan, res, msg = run(os.path.join(self.tmp, str(seed)), data, ctx, 40, seed)
            found.append(res["counts"]["finalists"])
            for t in res["trials"]:
                self.assertEqual(t["status"], "REJECTED")
                self.assertTrue(t["reason"])
        self.assertEqual(found, [0] * 10)


class PlantedEdge(Tmp):
    def test_planted_breakout_is_found(self):
        for seed in (0, 1, 2):
            data, ctx = market(seed, plant=True)
            plan, res, msg = run(os.path.join(self.tmp, str(seed)), data, ctx, 60, seed)
            fins = [c for c in res["candidates"] if c["id"] in res["finalists"]]
            self.assertTrue(fins, "seed %d: %s" % (seed, res["report"][:600]))
            self.assertEqual(set(c["family"] for c in fins), {"breakout"})
            for c in fins:
                self.assertTrue(all(g["pass"] for g in c["gates"]))
                self.assertGreater(c["holdout"]["stats"]["expectancy_r"], 0)
            self.assertIn("survived the untouched holdout", res["report"])

    def test_holdout_is_opened_once_and_logged(self):
        data, ctx = market(1, plant=True)
        plan, res, msg = run(self.tmp, data, ctx, 60, 1)
        d = os.path.join(self.tmp, "discover", "t")
        before = disc.read_jsonl(os.path.join(d, "holdout.jsonl"))
        self.assertEqual(len(before), res["counts"]["passed_robustness_and_multiple_testing"])
        self.assertEqual(len(set(h["id"] for h in before)), len(before))
        later = lab.ist_now("2026-11-02 12:00")
        res2 = disc.finalize(lab, plan, d, data, ctx, None, None, later)
        after = disc.read_jsonl(os.path.join(d, "holdout.jsonl"))
        self.assertEqual(after, before)              # not re-opened: the first look is the only look
        self.assertEqual(res2["finalists"], res["finalists"])
        # the trials never touched the holdout: no trial record has a holdout number
        for r in disc.read_jsonl(os.path.join(d, "trials.jsonl")):
            self.assertNotIn("holdout", r)

    def test_finalists_go_to_the_lab_charged_for_every_trial(self):
        data, ctx = market(2, plant=True)
        lab_out = os.path.join(self.tmp, "lab")
        plan, res, msg = run(self.tmp, data, ctx, 60, 2, lab_out=lab_out)
        fins = [c for c in res["candidates"] if c["id"] in res["finalists"]]
        self.assertTrue(fins)
        for c in fins:
            lb = c["lab"]
            self.assertTrue(lb["handed"], lb)
            self.assertIn(lb["verdict"], ("WATCH", "REJECT"))
            spec = lab.read_json(os.path.join(lab_out, lb["name"], "spec.json"))
            self.assertEqual(spec["trials_tried"], res["counts"]["counted"] - 1)
            self.assertIn(plan["name"], spec["description"])
            st = lab.read_json(os.path.join(lab_out, lb["name"], "status.json"))
            self.assertEqual(st["verdict"], lb["verdict"])
            self.assertNotEqual(st["verdict"], "PROMOTE")     # never before a paper forward test

    def test_statistics_only_runs_never_reach_the_lab(self):
        data, ctx = market(0, plant=True)
        lab_out = os.path.join(self.tmp, "lab")
        plan, res, msg = run(self.tmp, data, ctx, 60, 0, costs="none", lab_out=lab_out)
        self.assertTrue(res["finalists"])
        self.assertFalse(os.path.exists(lab_out))
        self.assertIn("Statistics-only run", res["report"])


class Bookkeeping(Tmp):
    def test_every_trial_logged_and_reproducible(self):
        data, ctx = market(4)
        p1, r1, _ = run(os.path.join(self.tmp, "a"), data, ctx, 30, 4)
        p2, r2, _ = run(os.path.join(self.tmp, "b"), data, ctx, 30, 4)
        t1 = disc.read_jsonl(os.path.join(self.tmp, "a", "discover", "t", "trials.jsonl"))
        t2 = disc.read_jsonl(os.path.join(self.tmp, "b", "discover", "t", "trials.jsonl"))
        strip = lambda rows: [{k: v for k, v in r.items() if k != "seconds"} for r in rows]  # noqa: E731
        self.assertEqual(strip(t1), strip(t2))
        self.assertEqual(len([r for r in t1 if r["role"] == "search"]), 30)
        for r in t1:
            for k in ("spec_hash", "params", "choices", "family", "train", "validation"):
                self.assertIn(k, r)
        self.assertEqual(r1["trials"], r2["trials"])
        self.assertEqual(r1["spa"], r2["spa"])
        plan = lab.read_json(os.path.join(self.tmp, "a", "discover", "t", "plan.json"))
        self.assertEqual(len(plan["trials"]), 30)     # declared before the first trial ran
        self.assertEqual(plan["gates"], disc.GATES)

    def test_cancel_then_resume_gives_the_same_answer(self):
        data, ctx = market(6, plant=True)
        ticks = [0]

        def cancel():
            ticks[0] += 1
            return ticks[0] > 12
        plan, res, msg = run(os.path.join(self.tmp, "a"), data, ctx, 40, 6, cancel=cancel)
        self.assertIsNone(res)
        self.assertIn("resume", msg)
        self.assertEqual(plan["state"], "paused")
        d = os.path.join(self.tmp, "a", "discover")
        self.assertEqual(len(disc.read_jsonl(os.path.join(d, "t", "trials.jsonl"))), 12)
        plan, res, msg = disc.resume(lab, "t", d, None, data, ctx, None, NOW)
        self.assertEqual(msg, "decided")
        _, whole, _ = run(os.path.join(self.tmp, "b"), data, ctx, 40, 6)
        self.assertEqual(res["counts"], whole["counts"])
        self.assertEqual(res["finalists"], whole["finalists"])
        plan, again, msg = disc.resume(lab, "t", d, None, data, ctx, None, NOW)
        self.assertEqual(msg, "already decided")

    def test_resume_refuses_changed_data(self):
        data, ctx = market(7)
        plan, res, msg = run(self.tmp, data, ctx, 20, 7, cancel=lambda: True)
        other, _ = market(8)
        with self.assertRaises(disc.DiscoverError):
            disc.resume(lab, "t", os.path.join(self.tmp, "discover"), None, other, ctx, None, NOW)

    def test_later_bars_do_not_change_a_resumed_run(self):
        data, ctx = market(9)
        short = disc.cut_data(lab, data, data["SYNA"].date[650])
        plan, res, msg = run(self.tmp, short, ctx, 20, 9, cancel=lambda: True)
        plan, res, msg = disc.resume(lab, "t", os.path.join(self.tmp, "discover"), None, data, ctx, None, NOW)
        self.assertEqual(msg, "decided")
        self.assertEqual(res["data"]["to"], data["SYNA"].date[650])

    def test_earlier_runs_count_as_trials(self):
        data, ctx = market(10)
        _, first, _ = run(self.tmp, data, ctx, 20, 10, name="first")
        plan, res, msg = run(self.tmp, data, ctx, 20, 11, name="second")
        self.assertEqual(plan["earlier_runs"], ["first"])
        self.assertEqual(res["counts"]["earlier_trials"], first["counts"]["counted"])   # its neighbours too
        self.assertEqual(res["counts"]["counted"], 20 + res["counts"]["neighbour_trials"] + first["counts"]["counted"])
        self.assertEqual(res["fdr"]["m"], res["counts"]["counted"])

    def test_a_run_name_is_used_once(self):
        data, ctx = market(12)
        run(self.tmp, data, ctx, 10, 1)
        with self.assertRaises(disc.DiscoverError):
            run(self.tmp, data, ctx, 10, 1)


# ---------------------------------------------------------------- real NSE history (the lab's fixtures)

class RealHistory(Tmp):
    def test_daily_run_on_real_bars(self):
        data, ctx, notes = fixture_market("1d")
        self.assertEqual(len(data), 10)
        self.assertIsNotNone(ctx["nifty"])
        plan, res, msg = run(self.tmp, data, ctx, 80, 7, lab_out=os.path.join(self.tmp, "lab"))
        c = res["counts"]
        self.assertEqual(c["search_trials"], 80)
        self.assertEqual(c["finalists"], 0, res["report"])        # the honest answer on two years of ten stocks
        self.assertEqual(plan["split"]["holdout"][1], "2026-10-30")
        self.assertEqual(plan["data"]["warmup_sessions"], 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "lab")))
        self.assertIn("survivorship", res["report"])
        self.assertIn("What this does NOT show", res["report"])
        self.assertEqual(sum(c["rejected_by_stage"].values()), 80)

    def test_intraday_run_on_real_bars(self):
        data, ctx, notes = fixture_market("5m", disc.LAB10[:4])
        plan, res, msg = run(self.tmp, data, ctx, 25, 3, tf="5m")
        self.assertEqual(res["counts"]["search_trials"], 25)
        self.assertEqual(res["counts"]["finalists"], 0)
        self.assertEqual(plan["split"]["embargo_sessions"], 1)
        rows = [r for r in disc.read_jsonl(os.path.join(self.tmp, "discover", "t", "trials.jsonl"))]
        self.assertTrue(any(r["train"]["trades"] for r in rows))


class Cli(Tmp):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = disc.main(["--out", os.path.join(self.tmp, "d"), "--lab-out", os.path.join(self.tmp, "lab")] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_run_report_list(self):
        rc, out, err = self.cli("list")
        self.assertIn("No discovery runs", out)
        rc, out, err = self.cli("run", "--budget", "20", "--universe", "lab10", "--timeframe", "1d", "--data", LABFIX,
                                "--now", "2026-11-01 12:00", "--bootstrap", "100")
        self.assertEqual(rc, 0, err)
        self.assertIn("20 trials were declared up front", out)
        self.assertIn("trial 20 of 20", err)
        rc, out, err = self.cli("report", "lab10-1d-s7-b20")
        self.assertIn("## How the history was split", out)
        rc, out, err = self.cli("report", "lab10-1d-s7-b20", "--json", "--trials")
        doc = json.loads(out)
        self.assertEqual(len(doc["trials"]), doc["counts"]["counted"])
        rc, out, err = self.cli("list", "--json")
        rows = json.loads(out)
        self.assertEqual(rows[0]["state"], "decided")
        self.assertIn("survived holdout + multiple testing", rows[0]["headline"])
        rc, out, err = self.cli("run", "--budget", "20", "--universe", "lab10", "--data", LABFIX)
        self.assertEqual(rc, 1)
        self.assertIn("resume it", err)
        rc, out, err = self.cli("report", "nope")
        self.assertEqual(rc, 1)

    def test_time_budget_pauses_and_resume_finishes(self):
        rc, out, err = self.cli("run", "--budget", "20", "--universe", "lab10", "--data", LABFIX, "--max-seconds",
                                "0.000001", "--name", "slow")
        self.assertEqual(rc, 0, err)
        self.assertIn("resume with: discover.py resume slow", out)
        rc, out, err = self.cli("report", "slow")
        self.assertIn("is paused", out)
        rc, out, err = self.cli("resume", "slow", "--data", LABFIX, "--json")
        self.assertEqual(json.loads(out)["state"], "decided")


if __name__ == "__main__":
    unittest.main()
