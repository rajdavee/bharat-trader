"""The strategy portfolio (trader-lab `lab.py portfolio`), offline.

Synthetic return streams of known correlation check the statistics and the sizing (risk parity against the
analytic two-asset answer, the walk-forward's lack of look-ahead, near-duplicate clustering). The smoke tests use
REAL lab outputs: tests/fixtures/ui/lab/ holds three real trader-lab runs (breakout20-swing, nifty-expiry-iron-fly,
orb15-vwap; all REJECT on these bars), and the edge-hunt fixture hypothesis eh-92 is run by the executor on the
real fixture bars in tests/fixtures/trader-lab/hunt/ to get a decided run with its recorded daily returns.
"""

import contextlib
import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

lab = load("trader-lab", "lab")
E = lab.engines_api()
PM, C, X, M = E.portfolio_mix, E.core, E.executor, E.market
UILAB = os.path.join(FIX, "ui", "lab")
LABFIX = os.path.join(FIX, "trader-lab")
HUNT = os.path.join(LABFIX, "hunt")
NOW = __import__("datetime").datetime(2026, 11, 1, 12, 0)


def days_from(start, n):
    d, out = C.day(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(C.iso(d))
        d += __import__("datetime").timedelta(days=1)
    return out


def correlated(n, rho, s1=0.01, s2=0.01, m1=0.0, m2=0.0, seed=3):
    rng = random.Random(seed)
    a, b = [], []
    for _ in range(n):
        z1, z2 = rng.gauss(0, 1), rng.gauss(0, 1)
        a.append(m1 + s1 * z1)
        b.append(m2 + s2 * (rho * z1 + math.sqrt(1 - rho * rho) * z2))
    return a, b


def write_lab_run(out, name, days, rets, verdict="WATCH", capital=100000.0, oos_from=0, forward=None):
    """A lab run folder in trader-lab's own file shapes (equity.csv, results.json, status.json)."""
    d = os.path.join(out, name)
    os.makedirs(d, exist_ok=True)
    eq = capital
    with open(os.path.join(d, "equity.csv"), "w", encoding="utf-8") as fh:
        fh.write("date,pnl,equity,drawdown_pct,trades,out_of_sample\n")
        for i, (x, r) in enumerate(zip(days, rets)):
            p = r * capital
            eq += p
            fh.write("%s,%.2f,%.2f,0.000,%d,%d\n" % (x, p, eq, 1 if p else 0, 1 if i >= oos_from else 0))
    lab.write_json(os.path.join(d, "results.json"), {"name": name, "title": name, "spec_hash": "h-" + name,
                                                     "config": {"capital": capital}, "timeframe": "1d",
                                                     "data": {"from": days[0], "to": days[-1]}})
    lab.write_json(os.path.join(d, "status.json"), {"name": name, "verdict": verdict, "spec_hash": "h-" + name})
    if forward:
        lab.write_json(os.path.join(d, "forward.json"), {"name": name, "updated": forward[-1]["exit_date"] + " 16:00",
                                                         "trades": forward})


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_json(path):
    return json.loads(read_text(path))


def tmpdir(t):
    d = tempfile.mkdtemp(prefix="bt-mix-")
    t.addCleanup(shutil.rmtree, d, True)
    return d


class Statistics(unittest.TestCase):
    def test_pearson_spearman_downside_on_known_correlation(self):
        a, b = correlated(4000, 0.6)
        self.assertAlmostEqual(PM.pearson(a, b), 0.6, delta=0.03)
        # bivariate normal: Spearman = 6/pi asin(rho/2)
        self.assertAlmostEqual(PM.spearman(a, b), 6 / math.pi * math.asin(0.3), delta=0.03)
        self.assertGreater(PM.downside(a, b), 0.3)
        c, d = correlated(4000, 0.0, seed=9)
        self.assertAlmostEqual(PM.pearson(c, d), 0.0, delta=0.04)
        # ranks resist one huge day; Pearson does not
        a2, b2 = list(a), list(b)
        a2[0], b2[0] = 5.0, 5.0
        self.assertGreater(PM.pearson(a2, b2), 0.9)
        self.assertAlmostEqual(PM.spearman(a2, b2), PM.spearman(a, b), delta=0.01)

    def test_downside_ignores_the_winning_days(self):
        a = [0.01, -0.01, 0.02, -0.02, 0.0]
        b = [0.05, -0.02, 0.07, -0.04, 0.03]
        self.assertAlmostEqual(PM.downside(a, b), 1.0, places=9)   # the losing days line up exactly
        self.assertEqual(PM.downside([0.01, 0.02], [-0.01, 0.0]), 0.0)

    def test_weekly_sums_by_iso_week(self):
        days = ["2026-10-26", "2026-10-27", "2026-11-02", "2026-11-03"]
        rows = [[0.01, 0.0], [0.02, 0.01], [0.0, -0.01], [0.03, 0.0]]
        w = PM.weekly(days, rows)
        self.assertEqual(len(w), 2)
        self.assertAlmostEqual(w[0][0], 0.03)
        self.assertAlmostEqual(w[1][1], -0.01)


class Sizing(unittest.TestCase):
    def cov2(self, s1, s2, rho):
        return [[s1 * s1, rho * s1 * s2], [rho * s1 * s2, s2 * s2]]

    def test_risk_parity_matches_the_two_asset_solution(self):
        # two assets: equal risk contribution is w1 = s2 / (s1 + s2), whatever the correlation
        for s1, s2, rho in ((0.01, 0.02, 0.3), (0.015, 0.005, -0.4), (0.02, 0.02, 0.9), (0.01, 0.03, 0.0)):
            w = PM.risk_parity(self.cov2(s1, s2, rho))
            self.assertAlmostEqual(w[0], s2 / (s1 + s2), places=8)
            self.assertAlmostEqual(sum(w), 1.0, places=12)

    def test_risk_parity_equalises_contributions(self):
        s = [0.01, 0.012, 0.02]
        r = [[1, 0.8, 0.0], [0.8, 1, 0.1], [0.0, 0.1, 1]]
        cov = [[r[i][j] * s[i] * s[j] for j in range(3)] for i in range(3)]
        w = PM.risk_parity(cov)
        shares = PM.risk_shares(cov, w)
        for x in shares:
            self.assertAlmostEqual(x, 1 / 3.0, places=3)
        # the correlated pair gets less than inverse volatility would give it
        iv, _ = PM.w_inverse_vol(cov, None, 0, PM.DEFAULTS)
        self.assertLess(w[0] + w[1], iv[0] + iv[1])
        # uncorrelated: risk parity is inverse volatility
        d = [[s[i] * s[i] if i == j else 0.0 for j in range(3)] for i in range(3)]
        for a, b in zip(PM.risk_parity(d), PM.w_inverse_vol(d, None, 0, PM.DEFAULTS)[0]):
            self.assertAlmostEqual(a, b, places=8)

    def test_caps_water_fill_and_the_vol_target_only_scales_down(self):
        w = PM.cap_weights([0.7, 0.2, 0.1], 0.5)
        self.assertAlmostEqual(w[0], 0.5)
        self.assertAlmostEqual(sum(w), 1.0)
        self.assertAlmostEqual(w[1] / w[2], 2.0)
        self.assertEqual(PM.cap_weights([0.5, 0.5], 0.4), [0.4, 0.4])   # every one at the cap: the rest is cash
        a, b = correlated(300, 0.0, 0.02, 0.02)
        rows = [list(x) for x in zip(a, b)]
        cfg = PM.settings({"vol_target_pct": 10.0})
        w, info = PM.fit_weights("equal", rows, cfg)
        vol = math.sqrt(PM.port_var(PM.covariance(rows), w) * 252)
        self.assertAlmostEqual(vol, 0.10, places=6)
        self.assertGreater(info["cash"], 0.3)
        quiet = [[x / 10 for x in r] for r in rows]
        w2, info2 = PM.fit_weights("equal", quiet, cfg)
        self.assertEqual(info2["scale"], 1.0)                 # under the target: no leverage, never scaled up
        self.assertAlmostEqual(sum(w2), 1.0)

    def test_kelly_is_shrunk_capped_and_long_only(self):
        a, b = correlated(500, 0.2, 0.01, 0.01, 0.002, -0.001)
        rows = [list(x) for x in zip(a, b)]
        cfg = PM.settings({})
        w, info = PM.fit_weights("kelly", rows, cfg)
        self.assertLessEqual(w[0], cfg["kelly_cap"] + 1e-12)
        self.assertGreater(w[0], 0)
        self.assertEqual(w[1], 0.0)                           # a losing stream gets nothing, never a short
        self.assertTrue(any("estimation error" in n for n in info["notes"]))
        self.assertTrue(any("hard cap" in n for n in info["notes"]))
        # a mean inside its noise is shrunk to zero: t = 0.5 -> max(0, 1 - 4) = 0
        cov = [[0.0001]]
        self.assertEqual(PM.shrunk_means([0.0005 / math.sqrt(100)], cov, 100), [0.0])
        self.assertAlmostEqual(PM.shrunk_means([0.003], cov, 100)[0], 0.003 * (1 - 1 / 9.0))

    def test_settings_refuse_leverage_and_unknown_keys(self):
        for bad in ({"max_gross": 1.5}, {"max_weight": 0}, {"kelly_fraction": 0.8}, {"scheme": "magic"},
                    {"fit": 5}, {"leverage": 2}):
            with self.assertRaises(PM.MixError):
                PM.settings(bad)


class WalkForward(unittest.TestCase):
    def setUp(self):
        a, b = correlated(500, 0.3, 0.01, 0.02, 0.0005, 0.0004)
        self.days = days_from("2024-01-01", 500)
        self.rows = [list(x) for x in zip(a, b)]
        self.cfg = PM.settings({"fit": 60, "refit": 20, "lookback": 120})

    def test_no_look_ahead(self):
        for sc in PM.SCHEMES:
            base = PM.walk_forward(self.days, self.rows, sc, self.cfg)
            cut = 60 + 20 * 7                                  # a rebalance day
            changed = [list(r) for r in self.rows]
            for t in range(cut, len(changed)):
                changed[t] = [x * -3 + 0.01 for x in changed[t]]
            alt = PM.walk_forward(self.days, changed, sc, self.cfg)
            for r1, r2 in zip(base["rebalances"], alt["rebalances"]):
                if r1["date"] <= self.days[cut]:
                    self.assertEqual(r1["weights"], r2["weights"], sc)
                    self.assertLess(r1["fitted_on"][1], r1["date"])
            k = base["days"].index(self.days[cut])
            self.assertEqual(base["ret"][:k], alt["ret"][:k])
            at_cut = next(r for r in base["rebalances"] if r["date"] == self.days[cut])
            if any(at_cut["weights"]):
                self.assertNotEqual(base["ret"][k], alt["ret"][k])

    def test_scored_days_start_after_the_first_fit_and_buckets_compound(self):
        wf = PM.walk_forward(self.days, self.rows, "equal", self.cfg)
        self.assertEqual(wf["days"][0], self.days[60])
        self.assertEqual(len(wf["rebalances"]), len(range(60, 500, 20)))
        w = wf["rebalances"][0]["weights"]
        r0 = sum(wi * x for wi, x in zip(w, self.rows[60]))
        self.assertAlmostEqual(wf["ret"][0], r0, places=5)       # the logged weights are rounded
        # day two: the buckets have drifted, so the mix is not the plain weighted sum
        v = [wi * (1 + x) for wi, x in zip(w, self.rows[60])]
        cash = 1 - sum(w)
        r1 = sum(vi * x for vi, x in zip(v, self.rows[61])) / (sum(v) + cash)
        self.assertAlmostEqual(wf["ret"][1], r1, places=5)

    def test_bootstrap_is_seeded_and_ordered(self):
        rets = [r[0] for r in self.rows]
        b1 = PM.bootstrap(rets, 200, 10, 16)
        self.assertEqual(b1, PM.bootstrap(rets, 200, 10, 16))
        self.assertLessEqual(b1["final_pct"]["p5"], b1["final_pct"]["p50"])
        self.assertLessEqual(b1["final_pct"]["p50"], b1["final_pct"]["p95"])
        for x in b1["bands"]:
            self.assertLessEqual(x["p5"], x["p95"])


class Build(unittest.TestCase):
    def make(self, specs):
        out = os.path.join(tmpdir(self), "trader", "lab")
        for s in specs:
            write_lab_run(out, *s[:3], **(s[3] if len(s) > 3 else {}))
        return out

    def test_clusters_warn_about_the_same_bet(self):
        days = days_from("2023-01-02", 400)
        a, c = correlated(400, 0.0, 0.01, 0.01, 0.0004, 0.0003, seed=5)
        rng = random.Random(8)
        b = [x + rng.gauss(0, 0.002) for x in a]               # a near copy of a
        out = self.make([("alpha", days, a), ("alpha-copy", days, b), ("gamma", days, c)])
        doc = PM.build("dup", out, cfg={"boot_runs": 100})
        self.assertEqual(sorted(sorted(g) for g in doc["clusters"]["duplicates"]), [["alpha", "alpha-copy"]])
        self.assertIn(["gamma"], doc["clusters"]["groups"])
        self.assertTrue(any(n.startswith("Same bet: alpha and alpha-copy") for n in doc["notes"]))
        self.assertGreater(doc["correlation"]["pearson"][0][1], 0.9)
        # risk parity gives the copied pair about two thirds together, not the half each it gets alone
        w = doc["schemes"]["risk-parity"]["weights"]
        self.assertLess(w[2], 0.6)
        self.assertGreater(w[2], w[0])

    def test_overlap_only_and_reported(self):
        d1 = days_from("2023-01-02", 400)
        d2 = d1[150:] + days_from("2024-07-01", 100)
        a, _ = correlated(400, 0.0, seed=1)
        b, _ = correlated(len(d2), 0.0, seed=2)
        out = self.make([("early", d1, a), ("late", d2, b, {"oos_from": 20})])
        doc = PM.build("ov", out, cfg={"boot_runs": 50, "fit": 60})
        ov = doc["overlap"]
        self.assertEqual(ov["from"], d2[20])                   # late's first out-of-sample day
        self.assertEqual(ov["to"], d1[-1])                     # early's last day
        self.assertEqual(ov["sessions"], len([x for x in d1 if x >= d2[20]]))
        self.assertTrue(all(ov["from"] <= x <= ov["to"] for x in doc["scored_days"]))
        self.assertEqual(ov["coverage"], {"early": 1.0, "late": 1.0})
        # in-sample days are never used
        self.assertEqual(doc["strategies"][1]["segments"][0]["from"], d2[20])

    def test_no_overlap_is_refused_with_the_spans(self):
        a, _ = correlated(100, 0.0)
        out = self.make([("one", days_from("2022-01-03", 100), a), ("two", days_from("2024-01-01", 100), a)])
        with self.assertRaises(PM.MixError) as e:
            PM.build("none", out)
        self.assertIn("share only 0 sessions", str(e.exception))
        self.assertIn("one 2022-01-03", str(e.exception))

    def test_only_watch_and_promote_unless_research(self):
        days = days_from("2023-01-02", 300)
        a, b = correlated(300, 0.1, seed=4)
        out = self.make([("good", days, a), ("bad", days, b, {"verdict": "REJECT"}), ("fine", days, b,
                                                                                    {"verdict": "PROMOTE"})])
        doc = PM.build("auto", out, cfg={"boot_runs": 50})
        self.assertEqual([s["name"] for s in doc["strategies"]], ["fine", "good"])
        with self.assertRaises(PM.MixError) as e:
            PM.build("x", out, ["good", "bad"])
        self.assertIn("bad is REJECT", str(e.exception))
        doc = PM.build("x", out, ["good", "bad"], research=True, cfg={"boot_runs": 50}, capital=200000)
        ap = doc["proposals"]["autopilot"]
        self.assertEqual(list(ap["autopilot"]["strategies"]), ["good"])
        self.assertEqual(ap["left_out"][0]["name"], "bad")
        self.assertTrue(any(n.startswith("Research only: bad") for n in doc["notes"]))
        with self.assertRaises(PM.MixError):
            PM.build("y", out, ["good"])                      # one strategy is not a portfolio

    def test_forward_record_extends_the_stream(self):
        days = days_from("2023-01-02", 300)
        a, b = correlated(300, 0.1, seed=6)
        fwd = [{"symbol": "SBIN", "exit_date": "2024-03-12", "exit_time": "15:10", "net": 500.0, "spec_hash": "h-good"},
               {"symbol": "TCS", "exit_date": "2024-03-12", "exit_time": "15:10", "net": -200.0, "spec_hash": "h-good"},
               {"symbol": "ITC", "exit_date": "2024-03-13", "exit_time": "15:10", "net": 900.0, "spec_hash": "old"}]
        out = self.make([("good", days, a, {"forward": fwd}), ("other", days, b)])
        s = PM.lab_stream(out, "good")
        self.assertEqual([g["source"] for g in s["segments"]], ["backtest out of sample", "paper forward record"])
        self.assertEqual(s["segments"][1]["trades"], 2)            # the old spec's trade is not counted
        got = dict(s["series"])
        self.assertAlmostEqual(got["2024-03-12"], 300.0 / 100000)
        self.assertEqual(got["2024-03-13"], 0.0)
        self.assertEqual(len(PM.lab_stream(out, "good", use_forward=False)["series"]), 300)

    def test_proposals_are_files_only(self):
        days = days_from("2023-01-02", 400)
        a, b = correlated(400, 0.2, 0.01, 0.015, 0.0006, 0.0005, seed=11)
        out = self.make([("aa", days, a), ("bb", days, b)])
        wd = os.path.dirname(os.path.dirname(out))
        cfg_path = os.path.join(wd, "trader", "config.json")
        lab.write_json(cfg_path, {"capital": 500000, "max_daily_drawdown_pct": 2,
                                  "autopilot": {"strategies": {"aa": {"capital": 10000}}}})
        before = read_text(cfg_path)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = lab.main(["--out", out, "portfolio", "build", "mix", "--boot-runs", "100"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing in trader/config.json was changed", buf.getvalue())
        self.assertEqual(read_text(cfg_path), before)
        d = os.path.join(out, "portfolio", "mix")
        ap = read_json(os.path.join(d, "proposal-autopilot.json"))
        rk = read_json(os.path.join(d, "proposal-risk.json"))
        self.assertFalse(ap["applied"])
        self.assertFalse(rk["applied"])
        self.assertEqual(ap["capital_total"], 500000)
        self.assertEqual(ap["capital_basis"], "trader/config.json capital")
        caps = [b["capital"] for b in ap["autopilot"]["strategies"].values()]
        self.assertTrue(all(c % 1000 == 0 for c in caps))
        self.assertLessEqual(sum(caps), 500000 + 1000)
        self.assertLessEqual(rk["max_daily_drawdown_pct"], 2)      # never loosens the user's own limit
        self.assertIn("NOT a trader-risk key", rk["how"])
        # list and report read what was written
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            lab.main(["--out", out, "portfolio", "list"])
        self.assertIn("mix", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            lab.main(["--out", out, "portfolio", "report", "mix", "--json"])
        v = json.loads(buf.getvalue())
        self.assertNotIn("daily", v["schemes"]["equal"])
        self.assertEqual(v["chosen"], "risk-parity")
        lines = read_text(os.path.join(d, "equity.csv")).strip().split("\n")
        self.assertEqual(lines[0], "date,equal,inverse-vol,risk-parity,kelly")
        self.assertEqual(len(lines) - 1, v["scored"]["sessions"])


class RealFixtures(unittest.TestCase):
    """Smoke on real lab outputs (all three runs are REJECT: --research)."""

    def setUp(self):
        self.out = os.path.join(tmpdir(self), "trader", "lab")
        shutil.copytree(UILAB, self.out)

    def oos(self, name):
        rows = PM._equity_rows(os.path.join(UILAB, name, "equity.csv"))
        cap = read_json(os.path.join(UILAB, name, "results.json"))["config"]["capital"]
        return {x: p / cap for x, p, o in rows if o}

    def test_real_mix_of_a_swing_and_an_options_run(self):
        names = ["breakout20-swing", "nifty-expiry-iron-fly"]
        self.assertEqual([c["verdict"] for c in PM.candidates(self.out) if c["name"] in names], ["REJECT", "REJECT"])
        with self.assertRaises(lab.LabError):
            lab.portfolio_build(self.out, "real", names, now=lab.ist_now("2026-11-01 12:00"))
        doc = lab.portfolio_build(self.out, "real", names, research=True, now=lab.ist_now("2026-11-01 12:00"),
                                  settings={"boot_runs": 200})
        a, b = self.oos(names[0]), self.oos(names[1])
        lo, hi = max(min(a), min(b)), min(max(a), max(b))
        self.assertEqual((doc["overlap"]["from"], doc["overlap"]["to"]), (lo, hi))
        days = sorted(x for x in set(a) | set(b) if lo <= x <= hi)
        rho = C.correlation([a.get(x, 0.0) for x in days], [b.get(x, 0.0) for x in days])
        self.assertAlmostEqual(doc["correlation"]["pearson"][0][1], round(rho, 4), places=4)
        rp = doc["schemes"]["risk-parity"]
        self.assertAlmostEqual(sum(rp["weights"]) + rp["cash"], 1.0, places=3)
        self.assertTrue(rp["metrics"]["sessions"] > 100)
        self.assertEqual(doc["proposals"]["autopilot"]["autopilot"]["strategies"], {})
        self.assertTrue(any(n.startswith("Only 2 strategies") for n in doc["notes"]))
        card = read_text(os.path.join(self.out, "portfolio", "real", "scorecard.md"))
        for words in ("What this does NOT show", "Research only", "never applied", "walk-forward"):
            self.assertIn(words, card)
        for bad in ("guarantee", "sure-shot", "risk-free"):
            self.assertNotIn(bad, card.lower())

    def test_short_real_overlap_is_refused_plainly(self):
        with self.assertRaises(lab.LabError) as e:
            lab.portfolio_build(self.out, "three", ["breakout20-swing", "nifty-expiry-iron-fly", "orb15-vwap"], True)
        self.assertIn("share only", str(e.exception))

    def test_a_decided_edge_hunt_run_joins_with_its_recorded_returns(self):
        base = tmpdir(self)
        lib = os.path.join(base, "edge-hunt")
        shutil.copytree(os.path.join(HUNT, "edge-hunt"), lib)
        real = X.gate_rows
        X.gate_rows = lambda *a, **k: [dict(r, **{"pass": True}) for r in real(*a, **k)]
        self.addCleanup(setattr, X, "gate_rows", real)
        mk = M.FolderMarket(HUNT, lab.archive_api(), bars_dir=LABFIX)
        r = X.execute(os.path.join(lib, "eh-92-fixture-turn-of-month.json"), mk, self.out, NOW)
        rr = read_json(os.path.join(r["dir"], "returns.json"))
        self.assertEqual(rr["net_of"], "charges and slippage")
        self.assertTrue(rr["segments"]["validation"]["days"])
        ho = read_json(os.path.join(r["dir"], "holdout.json"))
        if ho.get("metrics") is not None:
            self.assertEqual(rr["segments"]["holdout"]["days"], ho["series"]["days"])
        s = PM.hunt_stream(self.out, "eh-92-fixture-turn-of-month")
        self.assertFalse(s["tradable"])
        self.assertEqual(s["segments"][0]["source"], "edge hunt validation")
        self.assertEqual(s["segments"][0]["from"], rr["segments"]["validation"]["days"][0])
        self.assertNotIn(rr["segments"]["train"]["days"][0], dict(s["series"]))     # train is in sample


if __name__ == "__main__":
    unittest.main()
