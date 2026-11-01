"""trader-lab v2 engines and the edge-hunt executor, offline.

Fixtures:
- tests/fixtures/trader-lab/archive/: real NSE UDiFF F&O bhavcopies, NIFTY only, 20 sessions of Oct 2026 with
  the 27-Oct monthly expiry (see test_lab_options.py). The continuous future is built from them.
- tests/fixtures/trader-lab/SYMBOL-1d.csv.gz and ^NSEI-1d.csv.gz: real daily bars of ten NIFTY 50 stocks and
  NIFTY, 30 Oct 2024 to 30 Oct 2026 (the lab's own fixtures).
- tests/fixtures/trader-lab/hunt/: members.json (those ten stocks, all NIFTY 50 members over the span, as
  "NIFTY 50" and "F&O"), sectors.json (their NSE sectors), results.json (the real results broadcast times of
  RELIANCE, TCS and HDFCBANK from trader-analyst's NSE fixtures), continuous/NIFTY.csv.gz (NIFTY's index
  closes standing in for its future: expiry and lot columns from the NSE calendar of the time, no basis) and
  edge-hunt/: three TEST-ONLY registrations with their own manifest, on those short windows.
"""

import copy
import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

lab = load("trader-lab", "lab")
E = lab.engines_api()
C, F, P, EV, PR, X, J, M = (E.core, E.futures, E.portfolio, E.events, E.pairs, E.executor, E.jobs, E.market)
LABFIX = os.path.join(FIX, "trader-lab")
HUNT = os.path.join(LABFIX, "hunt")
REAL = os.path.join(ROOT, "trader-lab", "library", "edge-hunt")
NOW = __import__("datetime").datetime(2026, 11, 1, 12, 0)


def market():
    return M.FolderMarket(HUNT, lab.archive_api(), bars_dir=LABFIX)


def tmp_library(t):
    d = tempfile.mkdtemp(prefix="bt-hunt-")
    t.addCleanup(shutil.rmtree, d, True)
    lib = os.path.join(d, "edge-hunt")
    shutil.copytree(os.path.join(HUNT, "edge-hunt"), lib)
    return lib, os.path.join(d, "out")


def series(rows):
    return F.Continuous("NIFTY", rows, "test")


def all_pass(real):
    return lambda *a, **k: [dict(r, **{"pass": True}) for r in real(*a, **k)]


class Charges(unittest.TestCase):
    def test_futures_charges_are_the_dated_lab_table(self):
        before = C.fut_side_charges(0, 1000000, None, "2026-03-31")
        after = C.fut_side_charges(0, 1000000, None, "2026-04-01")
        # STT on a ₹10 lakh futures sale: 0.02% to 31 Mar 2026, 0.05% from 1 Apr 2026
        self.assertAlmostEqual(after - before, 300.0, places=2)
        rt = lab.fno_round_trip_charges(1000000, 1000000, 1, None, "FUT", "2026-11-02")["total"]
        self.assertAlmostEqual(C.fut_side_charges(1000000, 0, None, "2026-11-02") +
                               C.fut_side_charges(0, 1000000, None, "2026-11-02"), rt, places=1)

    def test_delivery_charges_split_into_buy_and_sell_orders(self):
        rt = lab.round_trip_charges(100000, 100000, 1, {"product": "CNC"})
        buy = C.cnc_side_charges(100000, 0)
        sell = C.cnc_side_charges(0, 100000)
        self.assertAlmostEqual(buy + sell, rt["total"], places=1)
        # STT 0.1% on both sides: ₹100 on each ₹1 lakh order
        self.assertAlmostEqual(lab.round_trip_charges(100000, 100000, 1, {"product": "CNC"})["stt"], 200.0)
        self.assertGreater(buy, sell)          # stamp duty is on the buy

    def test_dp_charge_once_per_scrip_per_sell_day(self):
        days = ["2026-01-01", "2026-01-02", "2026-01-05"]
        # decided at the 2 Jan close (the only loser), bought at the 5 Jan open, sold at the window's last close
        bars = {"AAA": [{"date": "2026-01-01", "o": 100.0, "c": 100.0}, {"date": "2026-01-02", "o": 100.0, "c": 90.0},
                        {"date": "2026-01-05", "o": 100.0, "c": 100.0}]}
        panel = P.Panel(days, bars)
        r = P.run(panel, lambda d: ["AAA"], {"signal": "reversal", "lookback": 1, "top_n": 1, "rebalance": 1,
                                             "stale_sessions": 5}, 1, 2, 10000.0, {"dp_charge": 15.93}, bps=0.0)
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        q = t["qty"]
        expect = C.cnc_side_charges(q * 100.0, 0, None, "2026-01-05") + C.cnc_side_charges(0, q * 100.0, None,
                                                                                            "2026-01-05") + 15.93
        self.assertAlmostEqual(t["charges"], expect, places=2)


class Futures(unittest.TestCase):
    def test_continuous_from_real_bhavcopies_rolls_the_session_before_expiry(self):
        s = M.ArchiveMarket(os.path.join(LABFIX, "archive"), lab.archive_api()).futures("NIFTY")
        k = s.days.index("2026-10-26")
        self.assertTrue(s.roll[k])
        self.assertEqual(s.expiry[k], "2026-10-27")
        self.assertEqual(s.expiry[k + 1], "2026-11-23")
        self.assertEqual(s.lot[k + 1], 65)
        # the expiry-day return is the NEXT contract's own: close 27-Oct over its close on 26-Oct
        self.assertAlmostEqual(s.ret_cc[k + 1], s.c[k + 1] / s.next_c[k] - 1, places=12)
        self.assertNotAlmostEqual(s.ret_cc[k + 1], s.c[k + 1] / s.c[k] - 1, places=4)
        self.assertEqual(sum(s.roll), 1)

    def test_the_long_history_archive_shapes(self):
        """The adapter reads load_continuous / members / results_dates as the long-history archive returns
        them: a continuous series with its roll log, members that raise when not built, results with the
        first session that could react."""
        arc = lab.archive_api()
        root = os.path.join(LABFIX, "archive")
        direct = M.ArchiveMarket(root, arc).futures("NIFTY")

        class Api(object):
            def __getattr__(self, k):
                return getattr(arc, k)

            def load_continuous(self, u, method="back", start=None, end=None, roll="days:0", root=None):
                assert (method, roll) == ("none", "days:1")
                bars = [{"date": d, "o": direct.o[k], "h": direct.h[k], "l": direct.l[k], "c": direct.c[k],
                         "settle": direct.c[k], "v": 0, "oi": 0, "expiry": direct.expiry[k]} for k, d in enumerate(direct.days)]
                rolls = [{"date": direct.days[k + 1], "from": direct.expiry[k], "to": direct.expiry[k + 1],
                          "old": direct.c[k], "new": direct.next_c[k], "why": "1 session before expiry"}
                         for k in range(direct.n - 1) if direct.roll[k]]
                return {"underlying": u, "method": method, "bars": bars, "rolls": rolls, "issues": []}

            def members(self, index, day, root=None):
                raise arc.ArchiveError("no current %s list yet" % index)

            def results_dates(self, symbol, start=None, end=None, root=None):
                return [{"symbol": symbol, "date": "2026-10-17", "announced_at": "2026-10-17 17:47",
                         "after_close": True, "event_session": "2026-10-19"}]
        mk = M.ArchiveMarket(root, Api())
        s = mk.futures("NIFTY")
        self.assertIn("load_continuous", s.source)
        self.assertEqual(s.roll, direct.roll)
        self.assertEqual(s.ret_cc, direct.ret_cc)
        self.assertEqual(s.lot_at(s.days.index("2026-10-27")), 65)        # read from that day's board
        self.assertIsNone(mk.members("NIFTY 200", "2026-10-01"))
        self.assertEqual(mk.results("HDFCBANK"), [{"date": "2026-10-19", "announced": "2026-10-17 17:47"}])

    def test_hand_computed_long_with_a_roll(self):
        rows = [{"date": "2026-04-0%d" % (i + 1), "o": o, "h": c, "l": c, "c": c, "expiry": e, "lot": lot}
                for i, (o, c, e, lot) in enumerate([(100, 100, "2026-04-03", 10), (101, 102, "2026-04-03", 10),
                                                    (104, 105, "2026-04-30", 20), (106, 107, "2026-04-30", 20),
                                                    (108, 110, "2026-04-30", 20)])]
        rows[1]["roll"], rows[1]["next_c"] = True, 103.0
        s = series(rows)
        intents = [{"entry": 0, "entry_at": "close", "exit": 4, "exit_at": "close", "side": 1}]
        r = F.simulate(s, intents, 0, 4, 2500.0, None, 1.0, bps=0.0)
        # 2500 / (100 x 10) = 2 lots = 20 units; at the roll 2 lots of the new lot 20 = 40 units
        gross = 20 * (102 - 100) + 40 * (110 - 103)
        orders = [(100 * 20, True, "2026-04-01"), (102 * 20, False, "2026-04-02"), (103 * 40, True, "2026-04-02"),
                  (110 * 40, False, "2026-04-05")]
        ch = sum(C.fut_side_charges(v if b else 0, 0 if b else v, None, d) for v, b, d in orders)
        t = r["trades"][0]
        self.assertEqual((t["lots"], t["rolls"]), (2, 1))
        self.assertAlmostEqual(t["gross"], gross, places=6)
        self.assertAlmostEqual(t["charges"], ch, places=6)
        self.assertAlmostEqual(r["equity"][-1], 2500 + gross - ch, places=6)
        self.assertEqual(r["extra"]["rolls"], 1)
        # daily MTM: the day after the roll is marked from the new contract's roll price
        self.assertAlmostEqual(r["equity"][2] - r["equity"][1], 40 * (105 - 103), places=6)

    def test_overnight_and_intraday_holding(self):
        rows = [{"date": "2026-05-0%d" % (i + 4), "o": o, "c": c, "lot": 1} for i, (o, c) in
                enumerate([(100, 100), (103, 101), (99, 104)])]
        s = series(rows)
        on = F.simulate(s, EV.overnight(s, 0, 2, "every_session"), 0, 2, 1000.0, None, 1.0, bps=0.0)
        self.assertEqual([(t["entry"], t["exit"]) for t in on["trades"]], [(100.0, 103.0), (101.0, 99.0)])
        day = F.simulate(s, [{"entry": 1, "entry_at": "open", "exit": 1, "exit_at": "close", "side": -1}], 0, 2,
                         1000.0, None, 1.0, bps=0.0)
        self.assertAlmostEqual(day["trades"][0]["gross"], -9 * (101 - 103), places=6)

    def test_margin_and_below_one_lot(self):
        self.assertAlmostEqual(F.margin_pct("NIFTY"), 11.3)
        self.assertAlmostEqual(F.margin_pct("RELIANCE"), 23.5)
        rows = [{"date": "2026-05-0%d" % (i + 4), "o": 20000.0, "c": 20000.0, "lot": 65} for i in range(3)]
        r = F.simulate(series(rows), [{"entry": 0, "entry_at": "close", "exit": 2, "exit_at": "close", "side": 1}],
                       0, 2, 100000.0)
        self.assertEqual((r["trades"], r["extra"]["skipped_below_one_lot"]), ([], 1))

    def test_trend_signal_reads_no_later_bar(self):
        rng = random.Random(3)
        rows, x = [], 100.0
        for i in range(400):
            x *= 1 + rng.gauss(0.0003, 0.01)
            d = __import__("datetime").date(2024, 1, 1) + __import__("datetime").timedelta(days=i)
            rows.append({"date": d.isoformat(), "o": x, "c": x, "lot": 1})
        s = series(rows)
        want = [F.trend_signal(s, k, "tsmom_blend_3_6_12m") for k in range(300)]
        for r in rows[300:]:
            r["c"] *= 3.0
            r["o"] *= 0.2
        s2 = series(rows)
        self.assertEqual([F.trend_signal(s2, k, "tsmom_blend_3_6_12m") for k in range(300)], want)
        self.assertEqual(F.trend_signal(s, 100, "tsmom_12m"), 0)       # too little history: no position

    def test_intraday_momentum_hand_case(self):
        def bar(t, o, c):
            return {"date": "2026-06-01", "time": t, "o": o, "c": c}
        bars = {"2026-06-01": [bar("09:15", 100, 100.2), bar("09:44", 100.4, 100.6), bar("15:00", 101, 101.1),
                               bar("15:29", 101.4, 101.5)]}
        # without the gap the first half-hour is +0.6% (100 to 100.6): under a 0.7% threshold, no trade
        r = F.intraday_momentum(bars, {"2026-06-01": 99.0}, lambda d: 10, "NIFTY",
                                {"first_window": "0915_0945_without_gap", "min_abs_first_return_pct": 0.7},
                                5000.0, None, 1.0, bps=0.0)
        self.assertEqual(r["trades"], [])
        r = F.intraday_momentum(bars, {"2026-06-01": 99.0}, lambda d: 10, "NIFTY",
                                {"first_window": "0915_0945_with_gap", "min_abs_first_return_pct": 0.7},
                                5000.0, None, 1.0, bps=0.0)
        t = r["trades"][0]
        # 100.6 / 99 - 1 = 1.6% up: long 15:00 open to 15:29 close, 4 lots of 10 (5000 / 1010)
        self.assertEqual((t["side"], t["lots"]), ("long", 4))
        self.assertAlmostEqual(t["gross"], 40 * (101.5 - 101.0), places=6)


class Portfolio(unittest.TestCase):
    def panel(self, prices, names=("AAA", "BBB", "CCC")):
        days = ["2026-01-%02d" % (i + 1) for i in range(len(prices))]
        bars = {n: [{"date": d, "o": row[j], "c": row[j]} for d, row in zip(days, prices)] for j, n in enumerate(names)}
        return P.Panel(days, bars)

    def test_hand_computed_momentum_rebalance(self):
        prices = [(100, 100, 100), (110, 100, 90), (120, 100, 80), (120, 110, 80), (121, 110, 80)]
        pn = self.panel(prices)
        r = P.run(pn, lambda d: ["AAA", "BBB", "CCC"], {"signal": "momentum", "lookback": 1, "skip": 0, "top_n": 1,
                                                       "rebalance": 10, "stale_sessions": 5}, 2, 4, 10000.0,
                  {"dp_charge": 0.0}, bps=0.0)
        # decided at session 1's close (110/100 best: AAA), bought at session 2's open 120: floor(10000/120) = 83
        t = r["trades"][0]
        self.assertEqual((t["symbol"], t["qty"], t["entry"]), ("AAA", 83, 120.0))
        ch = C.cnc_side_charges(83 * 120.0, 0, None, "2026-01-03") + C.cnc_side_charges(0, 83 * 121.0, None,
                                                                                        "2026-01-05")
        self.assertAlmostEqual(t["gross"], 83 * (121 - 120), places=6)
        self.assertAlmostEqual(t["charges"], ch, places=6)
        self.assertAlmostEqual(r["equity"][-1], 10000 + 83 - ch, places=6)
        # the benchmark: the equal-weighted universe, close to close
        self.assertAlmostEqual(r["bench"][1], ((110 / 110.0 - 1) + (110 / 100.0 - 1) + (80 / 80.0 - 1)) / 3, places=12)

    def test_point_in_time_universe(self):
        prices = [(100, 100, 100), (110, 100, 90), (120, 100, 80), (120, 110, 80), (121, 110, 80)]
        pn = self.panel(prices)
        # AAA only joins the index on 2026-01-04: before that the best name is BBB
        mem = lambda d: ["BBB", "CCC"] + (["AAA"] if d >= "2026-01-04" else [])
        r = P.run(pn, mem, {"signal": "momentum", "lookback": 1, "skip": 0, "top_n": 1, "rebalance": 10,
                            "stale_sessions": 5}, 2, 4, 10000.0, bps=0.0)
        self.assertEqual(r["trades"][0]["symbol"], "BBB")

    def test_low_volatility_and_reversal_scores(self):
        prices = [(100, 100, 100), (101, 120, 100), (102, 90, 99), (103, 130, 98), (104, 95, 97)]
        pn = self.panel(prices)
        p = dict(P.DEFAULTS, signal="low_volatility", vol_window=3)
        w = P.select(pn, ["AAA", "BBB", "CCC"], 4, dict(p, top_n=1))
        self.assertIn(list(w), [["AAA"], ["CCC"]])
        self.assertNotIn("BBB", w)
        p = dict(P.DEFAULTS, signal="reversal", lookback=1, top_n=1, side="long_short")
        w = P.select(pn, ["AAA", "BBB", "CCC"], 4, p)
        self.assertEqual(w, {"BBB": 0.5, "AAA": -0.5})      # the biggest loser long, the biggest winner short

    def test_no_look_ahead_on_real_stocks(self):
        mk = market()
        syms = mk.members("NIFTY 50", "2025-06-01")
        bars = mk.daily_many(syms)
        days = [b["date"] for b in mk.index("NIFTY 50")]
        par = {"signal": "momentum", "lookback": 63, "skip": 21, "top_n": 3, "rebalance": "monthly"}
        a, b = 150, 330
        r1 = P.run(P.Panel(days, bars), lambda d: syms, par, a, b, 1000000.0)
        cut = days[b]
        mutated = copy.deepcopy(bars)
        rng = random.Random(9)
        for s_ in mutated:
            for x in mutated[s_]:
                if x["date"] > cut:
                    x["c"] *= rng.uniform(0.3, 3.0)
                    x["o"] *= rng.uniform(0.3, 3.0)
        r2 = P.run(P.Panel(days, mutated), lambda d: syms, par, a, b, 1000000.0)
        self.assertEqual(r1["ret"], r2["ret"])
        self.assertEqual([t["symbol"] for t in r1["trades"]], [t["symbol"] for t in r2["trades"]])
        self.assertTrue(r1["trades"])
        m = C.metrics(r1)
        for k in ("cagr_pct", "vol_pct", "sharpe", "sortino", "max_drawdown_pct", "turnover_pct_per_year", "hit_rate",
                  "exposure_pct", "active_sharpe", "bench_cagr_pct"):
            self.assertIn(k, m)
        self.assertIn("capital", r1["extra"]["capacity"]["note"])


class Events(unittest.TestCase):
    DAYS = ["2026-03-26", "2026-03-27", "2026-03-30", "2026-04-01", "2026-04-02", "2026-04-06", "2026-04-07"]

    def test_turn_of_month_windows(self):
        it = EV.turn_of_month(self.DAYS, 0, 6, -2, 2)
        self.assertEqual([(self.DAYS[x["entry"]], self.DAYS[x["exit"]]) for x in it], [("2026-03-27", "2026-04-02")])

    def test_pre_holiday_and_overnight_before_a_holiday(self):
        # 31 Mar and 3 Apr 2026 were NSE holidays (Mahavir Jayanti, Good Friday)
        self.assertEqual(C.holidays(self.DAYS), ["2026-03-31", "2026-04-03"])
        it = EV.pre_holiday(self.DAYS, 0, 6, 1)
        self.assertEqual([(self.DAYS[x["entry"]], self.DAYS[x["exit"]]) for x in it],
                         [("2026-03-27", "2026-03-30"), ("2026-04-01", "2026-04-02")])
        s = series([{"date": d, "o": 100.0, "c": 100.0} for d in self.DAYS])
        on = EV.overnight(s, 0, 6, "before_weekend_or_holiday")
        self.assertEqual([self.DAYS[x["entry"]] for x in on], ["2026-03-27", "2026-03-30", "2026-04-02"])

    def test_expiry_week(self):
        it = EV.expiry_week(self.DAYS, ["2026-03-30", "2026-04-28"], 0, 6, 2)
        self.assertEqual([(self.DAYS[x["entry"]], self.DAYS[x["exit"]]) for x in it], [("2026-03-26", "2026-03-30")])

    def test_results_after_the_close_react_the_next_session(self):
        days = ["2026-07-16", "2026-07-17", "2026-07-20"]
        self.assertEqual(EV.reaction_session(days, {"date": "2026-07-17", "time": "19:50"}), 2)
        self.assertEqual(EV.reaction_session(days, {"date": "2026-07-17", "time": "11:00"}), 1)
        self.assertEqual(EV.reaction_session(days, {"date": "2026-07-18"}), 2)

    def test_results_drift_on_real_results_dates(self):
        mk = market()
        syms = mk.members("NIFTY 50", "2025-06-01")
        days = [b["date"] for b in mk.index("NIFTY 50")]
        bars = mk.daily_many(syms)
        res = {s_: mk.results(s_) for s_ in syms if mk.results(s_)}
        p = {"top_fraction": 0.5, "hold_sessions": 10, "entry_lag_sessions": 1, "min_past_events": 2}
        r = EV.results_drift(P.Panel(days, bars), lambda d: syms, res, p, 100, 490, 1000000.0)
        self.assertTrue(r["trades"])
        idx = {d: k for k, d in enumerate(days)}
        for t in r["trades"]:
            held = idx[t["exit_date"]] - idx[t["entry_date"]]
            self.assertTrue(held == 10 or t["exit_date"] == days[490], t)
            self.assertIn("abnormal", t)
        # the surprise is known at the close after the reaction session: mutate everything after an entry's
        # decision close and the decision itself does not change
        first = r["trades"][0]
        cut = days[idx[first["entry_date"]] - 1]
        mut = copy.deepcopy(bars)
        for s_ in mut:
            for x in mut[s_]:
                if x["date"] > cut:
                    x["c"] *= 1.7
        r2 = EV.results_drift(P.Panel(days, mut), lambda d: syms, res, p, 100, 490, 1000000.0)
        self.assertEqual((r2["trades"][0]["symbol"], r2["trades"][0]["entry_date"]),
                         (first["symbol"], first["entry_date"]))


class Pairs(unittest.TestCase):
    def test_adf_and_engle_granger_hand_series(self):
        rng = random.Random(1)
        e, rw = [0.0], [0.0]
        for _ in range(400):
            e.append(0.5 * e[-1] + rng.gauss(0, 1))
            rw.append(rw[-1] + rng.gauss(0, 1))
        self.assertLess(PR.adf_t(e), -8)
        self.assertGreater(PR.adf_t(rw), -3.0)
        x = [math.log(100) + v * 0.01 for v in rw]
        y = [0.3 + 2.0 * a + 0.002 * b for a, b in zip(x, e)]
        b_, a_, t = PR.engle_granger(y, x)
        self.assertAlmostEqual(b_, 2.0, places=2)
        self.assertLess(t, PR.EG_CRITICAL[0.01])

    def test_selection_reads_train_only_and_trades_bands(self):
        mk = market()
        syms = mk.members("F&O", "2025-06-01")
        days = [b["date"] for b in mk.index("NIFTY 50")]
        bars = mk.daily_many(syms)
        pick = PR.select(P.Panel(days, bars), mk.sectors(), 0, 300, 126, min_corr=0.3, level=0.10)
        mut = copy.deepcopy(bars)
        for s_ in mut:
            for x in mut[s_]:
                if x["date"] > days[300]:
                    x["c"] *= random.Random(len(x["date"])).uniform(0.5, 2.0)
        self.assertEqual(PR.select(P.Panel(days, mut), mk.sectors(), 0, 300, 126, min_corr=0.3, level=0.10), pick)
        for p in pick:
            self.assertEqual(mk.sectors()[p["y"]], mk.sectors()[p["x"]])
        # a hand-made pair that leaves its band once and comes back
        d = ["2026-02-%02d" % i for i in range(1, 29)]
        base = [100 + (i % 3) for i in range(len(d))]
        y = [v * (1 + 0.002 * math.sin(i)) for i, v in enumerate(base)]
        y[22] = base[22] * 1.08
        bars2 = {"Y": [{"date": a, "o": v, "c": v} for a, v in zip(d, y)],
                 "X": [{"date": a, "o": v, "c": v} for a, v in zip(d, base)]}
        pn = P.Panel(d, bars2)
        r = PR.run(pn, [{"y": "Y", "x": "X"}], {"formation_sessions": 15, "entry_z": 2.0, "exit_z": 0.0,
                                                 "stop_z": 1000.0, "max_hold_sessions": 20}, 16, 27, 100000.0)
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["side"], "short spread")
        self.assertEqual(t["entry_date"], d[23])          # z seen at session 22's close, filled at the next open


class Metrics(unittest.TestCase):
    def test_portfolio_metrics_by_hand(self):
        run = C.new_run(["2026-01-01", "2026-01-02", "2026-01-05", "2026-02-02"], 100.0)
        C.finish(run, [10.0, -11.0, 5.0, 0.0])
        run["bench"] = [0.0, 0.0, 0.0, 0.0]
        m = C.metrics(run)
        rets = [0.1, -0.1, 5.0 / 99.0, 0.0]
        self.assertAlmostEqual(m["max_drawdown_pct"], 10.0, places=3)
        self.assertAlmostEqual(m["sharpe"], round(C.mean(rets) / C.sd(rets) * math.sqrt(252), 3), places=3)
        self.assertAlmostEqual(m["total_return_pct"], 4.0, places=3)
        self.assertEqual(m["months"], 2)

    def test_bh_and_bootstrap(self):
        keep = C.benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.6}, 0.10)
        self.assertEqual(keep, {"a": True, "b": True, "c": True, "d": False})
        self.assertLess(C.stationary_bootstrap_p([0.01 + 0.001 * (i % 3) for i in range(200)], B=300), 0.01)
        self.assertGreater(C.stationary_bootstrap_p([(-1) ** i * 0.01 for i in range(200)], B=300), 0.2)


class Executor(unittest.TestCase):
    def test_every_real_registration_loads_and_none_was_run(self):
        ids = [i for i, _ in X.library(REAL)]
        self.assertEqual(len(ids), 14)
        for i, path in X.library(REAL):
            doc, h = X.load(path)
            self.assertEqual(doc["status"], "registered")
            self.assertEqual(len(X.grid(doc)), doc["trial_budget"])

    def test_refuses_an_edited_file(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-92-fixture-turn-of-month.json")
        doc = json.load(open(p, encoding="utf-8"))
        doc["parameters"]["exit_session"] = [2, 3, 4]
        doc["trial_budget"] = 6
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        with self.assertRaises(X.HuntError) as e:
            X.execute(p, market(), out, NOW)
        self.assertIn("changed after registration", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(out, "edge-hunt")))
        # a status change alone is allowed
        lib2, out2 = tmp_library(self)
        p2 = os.path.join(lib2, "eh-92-fixture-turn-of-month.json")
        d2 = json.load(open(p2, encoding="utf-8"))
        d2["status"] = "running"
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(d2, indent=2, ensure_ascii=False) + "\n")
        X.load(p2)

    def test_trials_counted_once_and_the_holdout_stays_sealed_on_a_reject(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-91-fixture-momentum.json")
        seen = []

        class Rec(M.FolderMarket):
            def daily_many(self, symbols, start=None, end=None):
                seen.append(end)
                return M.FolderMarket.daily_many(self, symbols, start, end)
        mk = Rec(HUNT, lab.archive_api(), bars_dir=LABFIX)
        r = X.execute(p, mk, out, NOW)
        self.assertEqual(r["state"], "decided")
        d = r["dir"]
        rows = X.read_trials(d)
        self.assertEqual(sorted(x["trial"] for x in rows), [0, 1, 2, 3])
        res = r["results"]
        self.assertEqual(res["dsr"]["trials"], 4 + 0)      # grid 4 + prior 0
        self.assertEqual(r["outcome"]["trials"]["trials_counted"], 4)
        if r["outcome"]["verdict"] == "REJECT" and res["holdout"] is None:
            self.assertFalse(os.path.exists(os.path.join(d, "holdout.json")))
            self.assertIn("sealed", open(os.path.join(d, "scorecard.md"), encoding="utf-8").read().lower())
        # nothing past the validation's last day was read while the holdout was sealed
        self.assertTrue(seen and all(e <= "2026-05-29" for e in seen))
        doc = json.load(open(p, encoding="utf-8"))
        self.assertEqual(doc["status"], "rejected")
        self.assertEqual(doc["outcome"]["registration_hash"], X.registered_hash(doc))
        X.load(p)                                          # still matches its manifest
        again = X.execute(p, mk, out, NOW)
        self.assertTrue(again["already"])
        self.assertEqual(len(X.read_trials(d)), 4)

    def test_holdout_opened_once_and_logged_first(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-92-fixture-turn-of-month.json")
        real = X.gate_rows
        X.gate_rows = all_pass(real)
        self.addCleanup(setattr, X, "gate_rows", real)
        r = X.execute(p, market(), out, NOW)
        ho = json.load(open(os.path.join(r["dir"], "holdout.json"), encoding="utf-8"))
        self.assertEqual(ho["opened_at"], "2026-11-01 12:00")
        self.assertTrue(ho["opened_once"])
        self.assertIsNotNone(ho["metrics"])
        self.assertIn(r["outcome"]["verdict"], ("WATCH", "REJECT"))
        self.assertEqual(json.load(open(p))["status"], STATUS(r["outcome"]["verdict"]))
        # a second run of a decided hypothesis re-runs nothing
        self.assertTrue(X.execute(p, market(), out, NOW)["already"])
        # and a run that finds an opened holdout without a recorded result refuses to open it again
        lib2, out2 = tmp_library(self)
        p2 = os.path.join(lib2, "eh-92-fixture-turn-of-month.json")
        d2 = X.run_dir(out2, "eh-92-fixture-turn-of-month")
        os.makedirs(d2)
        doc, h = X.load(p2)
        X.write_json(os.path.join(d2, "holdout.json"), {"registration_hash": h, "params": None, "metrics": None,
                                                         "opened_at": "2026-10-31 09:00"})
        r2 = X.execute(p2, market(), out2, NOW)
        self.assertEqual(r2["outcome"]["verdict"], "REJECT")
        self.assertIn("never opened again", r2["outcome"]["why"])
        self.assertEqual(json.load(open(os.path.join(d2, "holdout.json")))["opened_at"], "2026-10-31 09:00")

    def test_deleting_the_run_folder_does_not_reopen_the_holdout(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-92-fixture-turn-of-month.json")
        real = X.gate_rows
        X.gate_rows = all_pass(real)
        self.addCleanup(setattr, X, "gate_rows", real)
        X.execute(p, market(), out, NOW)
        shutil.rmtree(out)
        r = X.execute(p, market(), out, NOW)
        self.assertTrue(r["already"])                      # the file's own outcome stands
        self.assertFalse(os.path.exists(os.path.join(out, "edge-hunt", "eh-92-fixture-turn-of-month", "holdout.json")))
        # opened, then the record lost before a verdict: the file still says it was opened
        doc = json.load(open(p, encoding="utf-8"))
        X.set_status(p, doc, "running", {"verdict": None, "holdout_opened_at": "2026-10-31 09:00"})
        r = X.execute(p, market(), out, NOW)
        self.assertEqual(r["outcome"]["verdict"], "REJECT")
        self.assertIn("never opened again", r["outcome"]["why"])

    def test_a_crash_inside_the_holdout_still_counts_as_opened(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-92-fixture-turn-of-month.json")
        real = X.gate_rows
        X.gate_rows = all_pass(real)
        self.addCleanup(setattr, X, "gate_rows", real)
        orig = J.CalendarJob.run
        calls = {"n": 0}

        def boom(self_, prm, a, b, m=1.0):
            if a == "2026-04-15":
                raise J.Blocked("disk gone")
            return orig(self_, prm, a, b, m)
        J.CalendarJob.run = boom
        self.addCleanup(setattr, J.CalendarJob, "run", orig)
        r = X.execute(p, market(), out, NOW)
        self.assertEqual(r["outcome"]["verdict"], "REJECT")
        ho = json.load(open(os.path.join(r["dir"], "holdout.json")))
        self.assertEqual(ho["opened_at"], "2026-11-01 12:00")
        self.assertIn("never opened again", ho["error"])
        _ = calls

    def test_family_trials_and_declared_trials_are_charged(self):
        lib, out = tmp_library(self)
        shutil.copy(os.path.join(REAL, "eh-01-xs-momentum.json"), lib)
        man = json.load(open(os.path.join(lib, "manifest.json")))
        real = json.load(open(os.path.join(REAL, "manifest.json")))
        man["hypotheses"].append([r for r in real["hypotheses"] if r["id"] == "eh-01-xs-momentum"][0])
        X.write_json(os.path.join(lib, "manifest.json"), man)
        d = X.run_dir(out, "eh-01-xs-momentum")
        for j in range(3):
            X.append_trial(d, {"trial": j})
        r = X.execute(os.path.join(lib, "eh-91-fixture-momentum.json"), market(), out, NOW, extra_trials=5,
                      dry_run=True)
        self.assertEqual(r["state"], "dry-run")
        pl = r["plan"]
        self.assertEqual((pl["family_trials"], pl["extra_trials"], pl["trials_counted"]), (3, 5, 4 + 3 + 5))
        self.assertEqual(pl["family_ids"], ["eh-01-xs-momentum"])
        self.assertFalse(os.path.exists(X.run_dir(out, "eh-91-fixture-momentum")))

    def test_blocked_without_the_long_history_data(self):
        before = {f: open(os.path.join(REAL, f), "rb").read() for f in os.listdir(REAL)}
        out = tempfile.mkdtemp(prefix="bt-hunt-out-")
        self.addCleanup(shutil.rmtree, out, True)
        empty = M.Market()
        r = X.execute(os.path.join(REAL, "eh-01-xs-momentum.json"), empty, out, NOW)
        self.assertEqual(r["state"], "blocked")
        self.assertTrue(any("point-in-time" in b for b in r["plan"]["blockers"]))
        r = X.execute(os.path.join(REAL, "eh-03-index-trend.json"), empty, out, NOW)
        self.assertEqual(r["state"], "blocked")
        r = X.execute(os.path.join(REAL, "eh-05-vrp-condor.json"), empty, out, NOW)
        self.assertIn("#73", r["plan"]["blockers"][0])
        r = X.execute(os.path.join(REAL, "eh-06-pead.json"), empty, out, NOW)
        self.assertTrue(any("results dates" in b for b in r["plan"]["blockers"]))
        # a futures hypothesis whose data is two years short waits for the history instead of running
        r = X.execute(os.path.join(REAL, "eh-04-turn-of-month.json"), market(), out, NOW, dry_run=True)
        self.assertEqual(r["state"], "blocked")
        self.assertIn("waits for the history", " ".join(r["plan"]["blockers"]))
        after = {f: open(os.path.join(REAL, f), "rb").read() for f in os.listdir(REAL)}
        self.assertEqual(before, after)
        self.assertEqual(os.listdir(out), [])

    def test_status_and_the_cli(self):
        lib, out = tmp_library(self)
        st = X.status(lib, out)
        self.assertEqual([h["id"] for h in st["hypotheses"]],
                         ["eh-91-fixture-momentum", "eh-92-fixture-turn-of-month", "eh-93-fixture-blocked-pead"])
        self.assertIn("Benjamini-Hochberg", st["bh_note"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = lab.main(["--out", out, "hunt", "--library", lib, "run", "eh-92", "--data", HUNT, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("stays sealed", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            lab.main(["--out", out, "hunt", "--library", lib, "status"])
        self.assertIn("eh-92-fixture-turn-of-month", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            lab.main(["hunt", "list"])
        self.assertIn("eh-14-trend-day-buying", buf.getvalue())

    def test_smoke_run_every_v2_family_on_fixtures(self):
        """Each engine family end to end through a Job on the fixture data (not the real hunt windows)."""
        mk = market()
        base = json.load(open(os.path.join(HUNT, "edge-hunt", "eh-91-fixture-momentum.json")))
        cases = [
            ("momentum-xs", {"lookback_months": 3, "skip_months": 1, "top_n": 3, "rebalance": "monthly",
                             "volatility_adjusted": True}),
            ("low-volatility", {"volatility_window_days": 63, "top_n": 3, "rebalance": "quarterly"}),
            ("reversal", {"lookback_sessions": 5, "bottom_n": 2, "hold_sessions": 5, "instrument": "futures_long_short"}),
            ("trend-ts", {"signal": "close_above_200d_average", "mode": "long_short", "underlying": "NIFTY"}),
            ("calendar", {"event": "pre_holiday", "entry_sessions_before": 1, "underlying": "NIFTY"}),
            ("overnight", {"condition": "after_down_session", "underlying": "NIFTY"}),
            ("pead", {"top_fraction": 0.5, "hold_sessions": 20, "entry_lag_sessions": 1, "min_past_events": 2}),
            ("pairs", {"formation_sessions": 126, "entry_z": 2.0, "exit_z": 0.0, "stop_z": 4.0, "max_hold_sessions": 20}),
        ]
        for fam, prm in cases:
            doc = copy.deepcopy(base)
            doc["family"] = fam
            doc["parameters"] = {k: [v] for k, v in prm.items()}
            doc["instrument"]["underlying"] = "NIFTY"
            doc["universe"]["name"] = "fno-stocks" if fam == "pairs" else "nifty50-pit"
            doc["span"]["train"] = {"from": "2025-01-01", "to": "2025-09-30"}
            job = J.job_for(doc, mk)
            self.assertEqual(job.blockers(), [], fam)
            job.load("2026-05-29")
            r = job.run(prm, "2025-10-15", "2026-05-29")
            m = X.summary(dict(doc, primary_metric="oos_net_sharpe"), r)
            self.assertGreater(m["sessions"], 50, fam)
            self.assertIn("cagr_pct", m)
            self.assertTrue(all(isinstance(x, float) for x in r["ret"]), fam)


def STATUS(v):
    return {"WATCH": "watch", "REJECT": "rejected"}[v]


if __name__ == "__main__":
    unittest.main()
