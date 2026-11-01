"""trader-cotrader tests, offline, on REAL bars.

tests/fixtures/trader-lab/   REAL daily bars of ten NIFTY 50 stocks, NIFTY 50 and India VIX (30 Oct 2024 to 30 Oct
                             2026) and 60 days of 5-minute bars, shared with the trader-lab tests.
tests/fixtures/trader-lab/archive/
                             the options lab's REAL trimmed trader-archive (NIFTY F&O bhavcopies, October 2026).
tests/fixtures/trader-cotrader/log.jsonl
                             a REAL prediction log: the co-trader run after every close from 18 Dec 2025 to
                             8 Oct 2026 on those bars (`cotrader.py today --tier research`), with trader-lab re-run
                             each day so no verdict saw a later bar. 38 predictions from SMA20_WATCH and
                             SMA20_REJECT below, rsi2-nifty50 and breakout20-swing.

SMA20_WATCH has lenient gates on purpose (like test_mcp's WATCH_SPEC) so the lab says WATCH and the WATCH tier can
be tested end to end: the real verdict of every built-in strategy on these bars is REJECT, and that no-edge answer is
tested too.
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
from _load import FIX, load  # noqa: E402

ct = load("trader-cotrader", "cotrader")
lab = ct.lab_api()
risk = ct.risk_api()
LABFIX = os.path.join(FIX, "trader-lab")
LOGFIX = os.path.join(FIX, "trader-cotrader", "log.jsonl")
TEN = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"]
LENIENT = {"min_oos_trades": 0, "min_oos_expectancy_r": -100, "min_oos_profit_factor": 0, "max_drawdown_pct": 100,
           "min_dsr": 0, "max_pbo": 1.01, "min_neighbourhood_positive": 0, "min_forward_trades": 100,
           "demote_drawdown_mult": 1000}


def sma20(name, stop, gates=None):
    d = {"name": name, "title": "Close crosses above the 20-day average", "timeframe": "1d", "product": "CNC",
         "universe": TEN, "params": {"stop_atr": stop},
         "entry": {"all": [["close", "crosses_above", {"sma": 20}]]}, "stop": {"atr": 14, "mult": "$stop_atr"},
         "target": {"r": 2}, "exit": {"max_bars": 15}, "grid": {"stop_atr": [stop, stop + 1]},
         "walk_forward": {"in_sample": 120, "out_of_sample": 40}}
    if gates:
        d["gates"] = gates
    return d


SMA20_WATCH = sma20("t-sma20-watch", 2, LENIENT)
SMA20_REJECT = sma20("t-sma20-research", 3)
SIGNAL = "2026-09-18 16:00"           # SMA20 fired on BHARTIARTL and AXISBANK at this close


def run_lab(wd, docs, when):
    now = lab.ist_now(when)
    src = ct.Sources(wd, LABFIX, None, False)
    out = os.path.join(wd, "trader", "lab")
    for doc in docs:
        spec = lab.validate_spec(doc)
        res = lab.run_and_decide(doc, src.market(spec, now), src.context(now), out, None, now)
        lab.save_run(out, doc, res, now)
    return now


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue(), err.getvalue()


class Maths(unittest.TestCase):
    def test_wilson_matches_the_textbook(self):
        # Wilson score interval, z = 1.96: 7/10 -> 0.3968 to 0.8922, 0/10 -> 0 to 0.2775, 5/10 -> 0.2366 to 0.7634
        for (k, n), (lo, hi) in (((7, 10), (0.3968, 0.8922)), ((0, 10), (0.0, 0.2775)), ((5, 10), (0.2366, 0.7634)),
                                 ((10, 10), (0.7225, 1.0))):
            got = ct.wilson(k, n)
            self.assertAlmostEqual(got[0], lo, places=4)
            self.assertAlmostEqual(got[1], hi, places=4)
        self.assertIsNone(ct.wilson(0, 0))
        # by hand: p = 0.3, n = 40
        p, n, z = 0.3, 40, ct.Z95
        centre = (p + z * z / 80) / (1 + z * z / 40)
        half = z * math.sqrt(p * 0.7 / 40 + z * z / 6400) / (1 + z * z / 40)
        self.assertAlmostEqual(ct.wilson(12, 40)[0], centre - half, places=12)

    def test_beta_functions(self):
        self.assertAlmostEqual(ct.beta_cdf(0.4, 2, 3), 0.5248, places=10)      # 6x.16x.36 + 4x.064x.6 + .0256
        self.assertAlmostEqual(ct.beta_cdf(0.5, 2, 2), 0.5, places=10)
        self.assertAlmostEqual(ct.beta_ppf(0.025, 3, 1), 0.025 ** (1 / 3.0), places=8)   # Beta(a, 1): q^(1/a)
        self.assertAlmostEqual(ct.beta_ppf(0.975, 1, 1), 0.975, places=8)
        self.assertAlmostEqual(ct.beta_ppf(0.5, 7, 7), 0.5, places=8)

    def test_shrinkage_by_hand(self):
        # 3 wins of 4 in the cell, the strategy's overall rate 40%, a prior worth 20 trades:
        # Beta(3 + 8, 1 + 12) -> mean 11/24
        s = ct.shrink(3, 4, 0.4, 20)
        self.assertAlmostEqual(s["mean"], 11 / 24.0, places=12)
        self.assertEqual((s["alpha"], s["beta"], s["weight_on_cell"]), (11.0, 13.0, round(4 / 24.0, 4)))
        self.assertAlmostEqual(ct.beta_cdf(s["lo"], 11, 13), 0.025, places=8)
        self.assertAlmostEqual(ct.beta_cdf(s["hi"], 11, 13), 0.975, places=8)
        # an empty cell is the prior itself; a huge cell is (nearly) its own rate
        self.assertAlmostEqual(ct.shrink(0, 0, 0.4, 20)["mean"], 0.4, places=12)
        self.assertAlmostEqual(ct.shrink(700, 1000, 0.4, 20)["mean"], 708 / 1020.0, places=12)
        # expected R: (sum of the cell + 20 x overall) / (n + 20), +/- 1.96 sd / sqrt(n + 20)
        e = ct.shrink_mean([2, 2, -1, 2], -0.03, 1.1, 20)
        self.assertAlmostEqual(e["mean"], (5 - 0.6) / 24.0, places=12)
        self.assertAlmostEqual(e["hi"] - e["mean"], ct.Z95 * 1.5 / math.sqrt(24), places=12)
        self.assertAlmostEqual(ct.shrink_mean([0.5], 0.1, 1.1, 20)["hi"] - (0.5 + 2) / 21.0,
                               ct.Z95 * 1.1 / math.sqrt(21), places=12)    # one trade: the overall spread

    def test_brier_log_loss_and_reliability_by_hand(self):
        ps, ys = [0.8, 0.3, 0.6], [1, 0, 0]
        self.assertAlmostEqual(ct.brier(ps, ys), (0.04 + 0.09 + 0.36) / 3, places=12)
        self.assertAlmostEqual(ct.log_loss(ps, ys), -(math.log(0.8) + math.log(0.7) + math.log(0.4)) / 3, places=12)
        bins = ct.reliability([0.05, 0.15, 0.17, 0.95, 1.0], [0, 1, 0, 1, 1], 10)
        self.assertEqual([(b["lo"], b["n"], b["observed"]) for b in bins], [(0.0, 1, 0.0), (0.1, 2, 0.5), (0.9, 2, 1.0)])
        self.assertEqual(bins[1]["mean_p"], 0.16)
        g = ct.score_group([0.5] * 4, [1, 0, 1, 0])
        self.assertEqual((g["brier"], g["brier_reference"], g["skill"]), (0.25, 0.25, 0.0))
        self.assertEqual(ct.score_group([], []), {"n": 0})

    def test_correlation(self):
        a = [0.01, -0.02, 0.03, 0.0, 0.01, -0.01, 0.02, 0.0, -0.03, 0.01]
        self.assertAlmostEqual(ct.correlation(a, a), 1.0, places=12)
        self.assertAlmostEqual(ct.correlation(a, [-x for x in a]), -1.0, places=12)
        self.assertIsNone(ct.correlation(a[:5], a[:5]))          # too short to say


class Tiers(unittest.TestCase):
    def row(self, verdict, source="lab", name="s"):
        return {"name": name, "verdict": verdict, "verdict_why": "why", "source": source}

    def test_tier_of_every_verdict(self):
        self.assertEqual(ct.tier_of(self.row("PROMOTE"), {})[0], "PROMOTED")
        self.assertEqual(ct.tier_of(self.row("WATCH"), {})[0], "WATCH")
        for v in ("REJECT", "DEMOTE", None):
            self.assertEqual(ct.tier_of(self.row(v), {})[0], "RESEARCH", v)
        t, why = ct.tier_of(self.row(None, "library"), {})
        self.assertEqual(t, "RESEARCH")
        self.assertIn("never run", why)
        t, why = ct.tier_of(self.row("PROMOTE"), {"s": {"why": "overconfident"}})
        self.assertEqual(t, "RESEARCH")                         # demoted for calibration wins over the lab verdict
        self.assertIn("calibration", why)

    def test_views(self):
        self.assertEqual(ct.VIEWS["validated"], ("PROMOTED", "WATCH"))
        self.assertIn("RESEARCH", ct.VIEWS["research"])
        with self.assertRaises(ct.CotraderError):
            ct.run_today(tempfile.mkdtemp(prefix="bt-ct-"), "everything")


class Ranking(unittest.TestCase):
    def cand(self, cid, sym, ev, lo, hi, tier="WATCH", liq=1e8):
        return {"id": cid, "symbol": sym, "tier": tier, "liquidity": liq,
                "evidence": {"ev_r": ev, "ev_lo": lo, "ev_hi": hi}}

    def test_score_width_correlation_tier_and_liquidity(self):
        days = ["2026-10-%02d" % d for d in range(1, 21)]
        base = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.012, -0.018, 0.007] * 2
        rets = {"A": dict(zip(days, base)), "B": dict(zip(days, base)),         # B moves exactly with A
                "C": dict(zip(days, [x * (-1 if i % 2 else 1) for i, x in enumerate(base)]))}
        a = self.cand("a", "A", 0.30, 0.10, 0.50)     # score 0.30 - 0.25 x 0.40 = 0.20
        b = self.cand("b", "B", 0.32, 0.12, 0.52)     # 0.22 alone, but 1.0 correlated with A once A is in
        c = self.cand("c", "C", 0.20, 0.10, 0.30)     # 0.15
        out = ct.rank([a, b, c], rets, [], {})
        self.assertEqual([x["id"] for x in out], ["b", "c", "a"])          # A drops below C once B is in
        self.assertAlmostEqual(out[0]["score"], 0.22, places=4)
        self.assertAlmostEqual(out[2]["rho"], 1.0, places=4)                  # A after B: fully correlated
        self.assertAlmostEqual(out[2]["score"], 0.20 - 0.5 * 1.0 * 0.30, places=4)
        self.assertIn("candidate b", out[2]["rho_with"])
        self.assertLess(out[1]["rho"], 0.5)
        # an open position in B: B itself is penalised as a duplicate of the holding
        out = ct.rank([self.cand("a", "A", 0.30, 0.10, 0.50), self.cand("b", "B", 0.32, 0.12, 0.52)], rets, ["B"], {})
        self.assertEqual([x["rho_with"] for x in out], ["B (open position)"] * 2)    # A moves with B too
        self.assertEqual([x["rho"] for x in out], [1.0, 1.0])
        # the tier comes first; a tie goes to the more liquid stock
        hi = self.cand("r", "C", 0.9, 0.8, 1.0, tier="RESEARCH")
        w = self.cand("w", "A", 0.1, 0.0, 0.2, tier="WATCH")
        self.assertEqual([x["id"] for x in ct.rank([hi, w], {}, [], {})], ["w", "r"])
        t1, t2 = self.cand("x", "X", 0.2, 0.1, 0.3, liq=5e7), self.cand("y", "Y", 0.2, 0.1, 0.3, liq=9e8)
        self.assertEqual([x["id"] for x in ct.rank([t1, t2], {}, [], {})], ["y", "x"])
        self.assertEqual([x["rank"] for x in ct.rank([t1, t2], {}, [], {})], [1, 2])


class EndToEnd(unittest.TestCase):
    """The lab runs as of the 18 Sep 2026 close; the co-trader then reads them with the same clock."""

    @classmethod
    def setUpClass(cls):
        cls.wd = tempfile.mkdtemp(prefix="bt-cotrader-")
        cls.now = run_lab(cls.wd, [SMA20_WATCH, SMA20_REJECT], SIGNAL)
        cls.doc = ct.run_today(cls.wd, "validated", None, cls.now, LABFIX, None, False, True)
        cls.research = ct.run_today(cls.wd, "research", None, cls.now, LABFIX, None, False, False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wd, ignore_errors=True)

    def test_candidates_end_to_end_from_real_bars(self):
        d = self.doc
        self.assertEqual(d["schema"], "bharat-trader/cotrader/1")
        ids = [c["id"] for c in d["candidates"]]
        self.assertEqual(sorted(ids), ["2026-09-21-t-sma20-watch-AXISBANK", "2026-09-21-t-sma20-watch-BHARTIARTL"])
        self.assertIsNone(d["no_edge"])
        for c in d["candidates"]:
            self.assertEqual((c["tier"], c["status"], c["date"], c["signal_date"]), ("WATCH", "ready", "2026-09-21",
                                                                                      "2026-09-18"))
            self.assertTrue(ct.ID_RE.match(c["id"]))
            p = c["plan"]
            self.assertLess(p["stop"], p["entry"])
            self.assertLess(p["entry"], p["target"])
            self.assertEqual(p["setup"], "cotrader-watch:t-sma20-watch")
            self.assertFalse(c["live_eligible"])                   # WATCH is paper only
            s = c["sizing"]
            self.assertGreater(s["qty"], 0)
            self.assertGreater(s["charges"], 0)
            self.assertLessEqual(s["max_loss_with_charges"], 1000.005)     # trader-budget's default max loss
            self.assertGreater(c["charges_at_target"], 0)
            self.assertEqual(c["risk"]["verdict"], "PASS")
            self.assertEqual(c["risk"]["as_of"], "2026-09-21 09:30")       # the next session's first entry time
            ev = c["evidence"]
            self.assertEqual(ev["cell"], "trend|normal")
            self.assertGreater(ev["trades_all"], ev["trades_cell"])
            self.assertTrue(ev["p_lo"] <= ev["p"] <= ev["p_hi"])
            self.assertGreater(ev["ev_r"], 0)
            # the shrinkage, recomputed by hand from the counts it reports
            k = ev["prior_trades"]
            self.assertEqual(k, min(20, ev["trades_all"]))
            prior = (ev["wins_all"] + 1) / float(ev["trades_all"] + 2)       # Laplace-smoothed overall rate
            self.assertEqual(ev["p_prior"], round(prior, 4))
            want = (ev["wins_cell"] + k * prior) / (ev["trades_cell"] + k)
            self.assertAlmostEqual(ev["p"], want, places=4)
            self.assertLess(ev["to"], "2026-09-18")                # only trades before the signal day
            so = c["second_opinion"]
            self.assertTrue(so["available"], so)
            self.assertEqual((so["replays"], so["seed"]), (2000, 19))
            self.assertIn("stop and target only", so["method"])
            x = c["explain"]
            for key in ("why", "invalidation", "evidence", "does_not_show"):
                self.assertTrue(x[key], key)
            self.assertIn("Only %d past trades" % ev["trades_cell"], " ".join(x["does_not_show"]))
            self.assertIn("WATCH", " ".join(x["does_not_show"]))
            self.assertTrue(any("stop" in s for s in x["invalidation"]))
            self.assertEqual(len(c["similar"]), 5)
            self.assertEqual(c["similar"][0]["symbol"], c["symbol"])     # the same stock first
            self.assertIsNotNone(c["liquidity"])
        self.assertEqual([c["rank"] for c in d["candidates"]], [1, 2])
        text = json.dumps(d).lower()
        for word in ("guaranteed", "sure-shot", "sure shot", "risk-free"):
            self.assertNotIn(word, text)

    def test_files_and_the_log(self):
        base = os.path.join(self.wd, "trader")
        with open(os.path.join(base, "cotrader.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["candidates"][0]["id"], self.doc["candidates"][0]["id"])
        for c in self.doc["candidates"]:
            with open(ct.plan_path(self.wd, c["id"]), encoding="utf-8") as fh:
                plan = json.load(fh)
            self.assertEqual((plan["symbol"], plan["cotrader"]["tier"]), (c["symbol"], "WATCH"))
        log = ct.read_log(self.wd)
        self.assertEqual(sorted(r["id"] for r in log), sorted(c["id"] for c in self.doc["candidates"]))
        self.assertEqual(log[0]["schema"], "bharat-trader/cotrader-log/1")
        # the same candidates again: logged once, never twice
        again = ct.run_today(self.wd, "validated", None, self.now, LABFIX, None, False, True)
        self.assertEqual(again["logged"], 0)
        self.assertEqual(len(ct.read_log(self.wd)), len(log))
        with self.assertRaises(ct.CotraderError):
            ct.plan_path(self.wd, "../../etc/passwd")

    def test_research_only_behind_the_flag(self):
        self.assertTrue(all(c["tier"] != "RESEARCH" for c in self.doc["candidates"]))
        names = {r["name"]: r for r in self.doc["strategies"]}
        self.assertFalse(names["t-sma20-research"]["in_view"])
        self.assertIn("hidden", names["t-sma20-research"]["status"])
        res = [c for c in self.research["candidates"] if c["tier"] == "RESEARCH"]
        self.assertEqual(sorted(c["symbol"] for c in res), ["AXISBANK", "BHARTIARTL"])
        for c in res:
            self.assertEqual(c["label"], "unproven, for study/paper only")
            self.assertFalse(c["live_eligible"])
            self.assertTrue(c["plan"]["setup"].startswith("cotrader-research:"))
            self.assertIn("UNPROVEN", " ".join(c["explain"]["does_not_show"]))
        # the research view still puts the validated tier first
        self.assertEqual(self.research["candidates"][0]["tier"], "WATCH")
        lib = [r for r in self.research["strategies"] if r["source"] == "library"]
        self.assertEqual(len(lib), len(lab.library()))           # never-run library specs are RESEARCH too
        self.assertTrue(all(r["tier"] == "RESEARCH" for r in lib))

    def test_research_idea_is_blocked_on_a_live_config(self):
        c = [x for x in self.research["candidates"] if x["tier"] == "RESEARCH"][0]
        order = c["order"]
        cfg = {"paper": False, "capital": 100000, "max_loss_per_trade": 1000, "max_daily_loss": 3000}
        now = lab.ist_now("2026-09-21 09:40").replace(tzinfo=ct.IST)
        live = risk.check([order], cfg, None, {}, None, "2026-09-21", now)["results"][0]
        self.assertEqual(live["verdict"], "BLOCK")
        self.assertTrue(any(r.startswith("research:") for r in live["reasons"]), live["reasons"])
        paper = risk.check([order], dict(cfg, paper=True), None, {}, None, "2026-09-21", now)["results"][0]
        self.assertFalse(any(r.startswith("research:") for r in paper["reasons"]), paper["reasons"])
        w = [x for x in self.doc["candidates"]][0]["order"]
        watch = risk.check([w], cfg, None, {}, None, "2026-09-21", now)["results"][0]
        self.assertFalse(any(r.startswith("research:") for r in watch["reasons"]))

    def test_explain_and_log_commands(self):
        cid = self.doc["candidates"][0]["id"]
        rc, out, _ = quiet(ct.main, ["explain", cid, "--workdir", self.wd])
        self.assertEqual(rc, 0)
        for part in ("Why this trade", "What would invalidate it", "does NOT show", "Nearest similar past trades",
                     "trader-risk preview"):
            self.assertIn(part, out)
        rc, out, _ = quiet(ct.main, ["explain", cid, "--workdir", self.wd, "--json"])
        self.assertEqual(json.loads(out)["id"], cid)
        rc, _, err = quiet(ct.main, ["explain", "2026-01-01-nothing-SBIN", "--workdir", self.wd])
        self.assertEqual(rc, 1)
        self.assertIn("no candidate", err)
        rc, out, _ = quiet(ct.main, ["log", "--workdir", self.wd])
        self.assertEqual(rc, 0)
        self.assertIn(cid, out)

    def test_kill_switch_blocks_and_says_why(self):
        wd = tempfile.mkdtemp(prefix="bt-cotrader-kill-")
        try:
            shutil.copytree(os.path.join(self.wd, "trader", "lab"), os.path.join(wd, "trader", "lab"))
            os.makedirs(os.path.join(wd, "trader", "state"))
            with open(os.path.join(wd, "trader", "state", "kill.json"), "w", encoding="utf-8") as fh:
                # on for the 21st, the session the daily signal would fill in
                json.dump({"active": True, "date": "2026-09-21", "at": "2026-09-21 09:00", "reason": "test"}, fh)
            d = ct.run_today(wd, "validated", None, self.now, LABFIX, None, False, False)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertIsNotNone(d["no_edge"])                      # nothing ready: sitting out
        self.assertTrue(all(c["status"] == "blocked" for c in d["candidates"]))
        self.assertTrue(any("kill" in r.lower() for r in d["no_edge"]["reasons"]), d["no_edge"]["reasons"])
        self.assertTrue(d["no_edge"]["would_change"])


class NoEdge(unittest.TestCase):
    def test_all_rejected_says_so_plainly(self):
        wd = tempfile.mkdtemp(prefix="bt-cotrader-noedge-")
        try:
            now = run_lab(wd, [lab.library()["rsi2-nifty50"]], "2026-10-30 16:00")
            rc, out, _ = quiet(ct.main, ["today", "--workdir", wd, "--data", LABFIX, "--now", "2026-10-30 16:00",
                                         "--offline"])
            with open(os.path.join(wd, "trader", "cotrader.json"), encoding="utf-8") as fh:
                d = json.load(fh)
            empty = ct.run_today(tempfile.mkdtemp(prefix="bt-ct-empty-"), "validated", None, now, LABFIX, None, False,
                                 False)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertEqual(rc, 0)
        self.assertEqual(d["candidates"], [])
        ne = d["no_edge"]
        self.assertEqual(ne["decision"], "sit out")
        self.assertIn("sitting out is the decision", ne["headline"])
        self.assertTrue(ne["reasons"][0].startswith("No validated strategy: none of the 1 lab runs"), ne["reasons"])
        self.assertIn("REJECT", ne["reasons"][0])
        self.assertTrue(any("trader-lab's gates" in w for w in ne["would_change"]))
        self.assertTrue(any("--tier research" in w for w in ne["would_change"]))
        self.assertIn("No trade today", out)
        self.assertIn("What would change that", out)
        self.assertIn("lab is empty", " ".join(empty["no_edge"]["reasons"]).replace("trader/lab/ is empty",
                                                                                   "lab is empty"))

    def test_regime_mismatch_is_a_reason_with_numbers(self):
        trades = [{"symbol": "SBIN", "signal_date": "2026-0%d-1%d" % (m, d), "exit_date": "2026-0%d-1%d" % (m, d + 1),
                   "r": -0.5, "net": -100.0, "entry": 100.0, "stop": 98.0, "exit": 99.0, "reason": "stop"}
                  for m in (3, 4) for d in range(1, 7)]
        ev = ct.evidence(trades, {}, "unknown|unknown", {}, "test")
        self.assertEqual((ev["trades_cell"], ev["fits"]), (12, False))      # 10+ losing trades in the cell
        self.assertEqual(ev["p"], round((0 + 12 * (1 / 14.0)) / 24.0, 4))  # 0 of 12, never a flat 0%
        self.assertGreater(ev["p_hi"], 0)
        allwin = [dict(t, r=1.0, net=50.0) for t in trades[:4]]
        ev = ct.evidence(allwin, {}, "unknown|unknown")
        self.assertAlmostEqual(ev["p"], (4 + 4 * 5 / 6.0) / 8.0, places=4)     # 4 of 4 is not 100%
        self.assertLess(ev["p_lo"], 0.8)
        ev = ct.evidence(trades[:5], {}, "unknown|unknown")
        self.assertIsNone(ev["fits"])                                       # too few to rule it out
        ev = ct.evidence([], {}, "trend|low")
        self.assertIsNone(ev["p"])


class Options(unittest.TestCase):
    """An options spec through trader-lab's options forward test: the basket becomes a candidate, risk-checked as
    one position (lenient gates so the lab says WATCH; the built-in option specs are REJECT on these bars)."""

    def test_option_basket_candidate(self):
        import copy
        arc = os.path.join(LABFIX, "archive")
        doc = copy.deepcopy(lab.library()["nifty-weekly-strangle-16d"])
        doc.update({"name": "t-strangle-4d", "walk_forward": {"in_sample": 8, "out_of_sample": 4},
                    "gates": dict(LENIENT, survive_cost_multiplier=1.5)})
        doc["params"]["dte"] = 4
        doc["entry"]["days_to_expiry"] = "$dte"
        wd = tempfile.mkdtemp(prefix="bt-cotrader-opt-")
        try:
            now = lab.ist_now("2026-11-01 12:00")
            spec = lab.validate_spec(doc)
            chains, cx, _ = lab.load_data(spec, None, None, False, now, archive=arc)
            out = os.path.join(wd, "trader", "lab")
            lab.save_run(out, doc, lab.run_and_decide(doc, chains, cx, out, None, now), now)
            d = ct.run_today(wd, "validated", None, now, None, arc, False, True)
            none = ct.run_today(wd, "validated", None, now, None, os.path.join(wd, "no-archive"), False, False)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertEqual(len(d["candidates"]), 1)
        c = d["candidates"][0]
        self.assertEqual((c["id"], c["kind"], c["tier"]), ("2026-11-02-t-strangle-4d-NIFTY", "options", "WATCH"))
        b = c["basket"]
        self.assertEqual((b["structure"], b["expiry"], b["legs"]), ("short-strangle", "2026-11-03", 2))
        self.assertTrue(b["unlimited_loss"])
        self.assertGreater(b["margin_required"], 0)
        self.assertTrue(all(o["exchange"] == "NFO" and o["plan"]["setup"] == "cotrader-watch:t-strangle-4d"
                            for o in c["plan"]["orders"]["orders"]))
        # the default config has F&O off: trader-risk blocks the basket, and the output says so
        self.assertEqual((c["status"], c["risk"]["verdict"]), ("blocked", "BLOCK"))
        self.assertTrue(any(r.startswith("fno:") for r in c["why_not"]))
        self.assertTrue(d["no_edge"])
        ev = c["evidence"]
        self.assertLess(ev["p"], 1.0)                 # every past trade won, yet the odds are not 100%
        self.assertIn("margin", c["explain"]["evidence"])
        self.assertIn("unlimited", " ".join(c["explain"]["invalidation"]))
        self.assertFalse(c["second_opinion"]["available"])
        self.assertIn("no local trader-archive", none["strategies"][0]["status"])


class Calibration(unittest.TestCase):
    """The fixture log, resolved on the real bars after each signal."""

    @classmethod
    def setUpClass(cls):
        cls.wd = tempfile.mkdtemp(prefix="bt-cotrader-cal-")
        os.makedirs(os.path.join(cls.wd, "trader", "cotrader"))
        shutil.copyfile(LOGFIX, ct.log_path(cls.wd))
        cls.now = lab.ist_now("2026-10-30 16:00")
        cls.rep = ct.calibration_report(cls.wd, cls.now, LABFIX, None, False, False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wd, ignore_errors=True)

    def test_fixture_log_scores_by_hand(self):
        rep = self.rep
        recs = ct.resolve(ct.read_log(self.wd), self.wd, ct.Sources(self.wd, LABFIX, None, False), self.now)
        done = [r for r in recs if r["outcome"] and r["outcome"]["hit"] is not None]
        self.assertEqual(rep["resolved"], len(done))
        self.assertEqual(rep["resolved"] + rep["pending"], 38)
        self.assertGreaterEqual(rep["resolved"], 30)
        ps = [r["p"] for r in done]
        ys = [r["outcome"]["hit"] for r in done]
        self.assertAlmostEqual(rep["overall"]["brier"], sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps), places=4)
        self.assertAlmostEqual(rep["overall"]["log_loss"],
                               -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(ps, ys)) / len(ps),
                               places=4)
        self.assertEqual(sum(b["n"] for b in rep["overall"]["bins"]), len(done))
        self.assertEqual(set(rep["by_tier"]), {"WATCH", "RESEARCH"})
        self.assertEqual(sum(g["n"] for g in rep["by_strategy"].values()), len(done))
        self.assertIn("bars replay", rep["sources"])
        self.assertIn("Brier", rep["words"])
        # under the 20 outcomes a strategy needs: nothing demoted on this log
        self.assertEqual(rep["demote"], [])

    def test_replay_outcome_by_hand(self):
        rec = [r for r in ct.read_log(self.wd) if r["id"] == "2026-09-21-t-sma20-watch-AXISBANK"][0]
        s = ct.Sources(self.wd, LABFIX, None, False).series("AXISBANK", "1d", self.now)
        o = ct.replay_outcome(rec, s, lab.lab_config({}))
        i = s.date.index("2026-09-21")
        fill = s.o[i]
        exit_px, j = None, i
        for j in range(i, s.n):                 # stop first, then the target, else out after 15 sessions
            if s.l[j] <= rec["stop"]:
                exit_px = rec["stop"] if j == i else min(rec["stop"], s.o[j])
                break
            if s.h[j] >= rec["target"]:
                exit_px = rec["target"]
                break
            if j - i + 1 >= 15:
                exit_px = s.c[j]
                break
        net = (exit_px - fill) * rec["qty"] - lab.round_trip_charges(fill, exit_px, rec["qty"], lab.lab_config({}))["total"]
        self.assertAlmostEqual(o["net"], round(net, 2), places=2)
        self.assertEqual((o["hit"], o["exit_date"]), (1 if net > 0 else 0, s.date[j]))

    def test_paper_fills_and_the_forward_record_come_first(self):
        rec = {"symbol": "AXISBANK", "date": "2026-09-21", "signal_date": "2026-09-18", "strategy": "t-x"}
        fills = {"trades": [
            {"at": "2026-09-21T09:31:00+05:30", "date": "2026-09-21", "symbol": "AXISBANK", "side": "BUY", "leg": "order"},
            {"at": "2026-09-23T11:00:00+05:30", "date": "2026-09-23", "symbol": "AXISBANK", "side": "SELL",
             "leg": "target", "realised_pnl": 412.5}]}
        o = ct.paper_outcome(rec, fills)
        self.assertEqual((o["source"], o["hit"], o["net"]), ("paper fills", 1, 412.5))
        self.assertIsNone(ct.paper_outcome(dict(rec, date="2026-09-22"), fills))
        out = os.path.join(self.wd, "trader", "lab")
        os.makedirs(os.path.join(out, "t-x"), exist_ok=True)
        with open(os.path.join(out, "t-x", "forward.json"), "w", encoding="utf-8") as fh:
            json.dump({"trades": [{"symbol": "AXISBANK", "signal_date": "2026-09-18", "net": -55.0, "r": -0.06,
                                   "exit_date": "2026-09-25", "reason": "stop"}]}, fh)
        o = ct.forward_outcome(rec, out)
        self.assertEqual((o["source"], o["hit"]), ("lab forward record", 0))

    def test_overconfident_strategy_is_demoted_through_the_lab(self):
        wd = tempfile.mkdtemp(prefix="bt-cotrader-demote-")
        try:
            run_lab(wd, [SMA20_WATCH], SIGNAL)
            recs = [{"id": "x%d" % i, "strategy": "t-sma20-watch", "tier": "WATCH", "p": 0.6,
                     "outcome": {"source": "bars replay", "hit": 1 if i < 5 else 0}} for i in range(25)]
            rep = ct.calibration(recs)
            self.assertEqual([d["strategy"] for d in rep["demote"]], ["t-sma20-watch"])     # 60% said, 20% came
            self.assertIn("overconfident", rep["demote"][0]["why"])
            done = ct.apply_demotions(rep, wd, lab.ist_now("2026-10-30 16:00"))
            self.assertEqual(done, ["t-sma20-watch"])
            with open(os.path.join(wd, "trader", "lab", "t-sma20-watch", "status.json"), encoding="utf-8") as fh:
                st = json.load(fh)
            self.assertEqual(st["verdict"], "DEMOTE")
            self.assertIn("calibration", st["why"])
            row = [r for r in ct.strategies(os.path.join(wd, "trader", "lab")) if r["name"] == "t-sma20-watch"][0]
            demoted = ct.read_json(ct.demoted_path(wd))["strategies"]
            self.assertEqual(ct.tier_of(row, demoted)[0], "RESEARCH")
            # a calibrated one is left alone, and so is a RESEARCH-only record
            ok = [dict(r, outcome={"source": "x", "hit": 1 if i < 15 else 0}) for i, r in enumerate(recs)]
            self.assertEqual(ct.calibration(ok)["demote"], [])
            res = [dict(r, tier="RESEARCH") for r in recs]
            self.assertEqual(ct.calibration(res)["demote"], [])
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_calibration_command(self):
        rc, out, _ = quiet(ct.main, ["calibration", "--workdir", self.wd, "--data", LABFIX, "--now", "2026-10-30 16:00",
                                     "--offline", "--no-demote"])
        self.assertEqual(rc, 0)
        for part in ("Brier", "Reliability", "By strategy", "By tier"):
            self.assertIn(part, out)
        rc, out, _ = quiet(ct.main, ["calibration", "--workdir", self.wd, "--data", LABFIX, "--now",
                                     "2026-10-30 16:00", "--offline", "--no-demote", "--json"])
        self.assertEqual(json.loads(out)["schema"], "bharat-trader/cotrader-calibration/1")


if __name__ == "__main__":
    unittest.main()
