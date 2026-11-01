"""trader-lab options backtests, offline.

Fixture (tests/fixtures/trader-lab/archive/): a trader-archive folder cut from NSE's real UDiFF F&O
bhavcopies (BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip) and index closes (ind_close_all_DDMMYYYY.csv),
fetched 2026-11-01 by archive.py backfill and trimmed: NIFTY rows only, expiries within 40 calendar days,
option strikes within 5% of that day's spot, 20 sessions 2026-10-01 to 2026-10-30; the index files keep
only "Nifty 50" and "India VIX". It holds the weekly expiries 06-Oct, 13-Oct, 19-Oct (a Monday: Tuesday
20-Oct was the Dussehra holiday), the monthly 27-Oct (with its future) and the 03-Nov weekly, under the STT
rates in force from 1 April 2026, and lot 65.
"""

import copy
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

lab = load("trader-lab", "lab")
O = lab.options_api()
ARC = os.path.join(FIX, "trader-lab", "archive")
LENIENT = {"min_oos_trades": 0, "min_oos_expectancy_r": -100, "min_oos_profit_factor": 0, "max_drawdown_pct": 100,
           "min_dsr": 0, "max_pbo": 1.01, "min_neighbourhood_positive": 0, "min_forward_trades": 100,
           "demote_drawdown_mult": 1000, "survive_cost_multiplier": 1.5}
CHAINS = O.load_chains("NIFTY", ARC, lab.archive_api())


def spec_doc(name="nifty-weekly-strangle-16d", **over):
    doc = copy.deepcopy(lab.library()[name])
    doc["walk_forward"] = {"in_sample": 8, "out_of_sample": 4}
    doc.update(over)
    return doc


def raw_row(day, expiry, strike, kind):
    import gzip
    with gzip.open(os.path.join(ARC, "fo", day[:4], day + ".csv.gz"), "rt") as fh:
        for line in fh:
            r = line.strip().split(",")
            if r[:5] == ["NIFTY", "IDO", expiry, "%g" % strike, kind]:
                return r
    return None


class Library(unittest.TestCase):
    def test_five_option_specs_validate(self):
        lib = lab.library()
        names = ("nifty-weekly-strangle-16d", "nifty-weekly-condor-16d", "nifty-expiry-iron-fly",
                 "banknifty-monthly-put-spread", "nifty-ivr-strangle")
        for n in names:
            spec = lab.validate_spec(lib[n])
            self.assertEqual((spec["kind"], spec["name"]), ("options", n))
            self.assertLessEqual(len(lab.grid_points(spec)[2]), lab.MAX_GRID)
        self.assertEqual(lab.validate_spec(lib["nifty-weekly-condor-16d"])["wing"], 100)
        self.assertEqual(lab.validate_spec(lib["nifty-weekly-strangle-16d"])["strike_value"], 0.16)

    def bad(self, change, msg):
        doc = spec_doc()
        change(doc)
        with self.assertRaises(lab.SpecError) as cm:
            lab.validate_spec(doc)
        self.assertIn(msg, str(cm.exception))

    def test_rejects_unsound_specs(self):
        self.bad(lambda d: d.update(structure="naked-call"), "structure")
        self.bad(lambda d: d["entry"].update(fill="open"), "not by delta")
        self.bad(lambda d: d.update(structure="iron-fly"), "ATM")
        self.bad(lambda d: d.update(structure="iron-condor"), "wings")
        self.bad(lambda d: d["exit"].update(days_before_expiry=6), "never runs")
        self.bad(lambda d: d.update(universe=["NIFTY"]), "unknown keys")
        self.bad(lambda d: d.update(timeframe="5m"), "end-of-day")
        self.bad(lambda d: d["params"].update(delta="x"), "lowercase name with a number")


class Chain(unittest.TestCase):
    def test_leg_pricing_from_settle(self):
        r = raw_row("2026-10-14", "2026-10-19", 22800, "CE")
        self.assertEqual(r[9], "62.5")                                  # SttlmPric in the real bhavcopy
        px, src = CHAINS.price("2026-10-14", "2026-10-19", 22800.0, "CE")
        self.assertEqual((px, src), (62.5, "settle"))
        a = CHAINS.analysis("2026-10-14", "2026-10-19")
        self.assertEqual((a["lot_size"], a["atm"]), (65, 22600.0))
        self.assertTrue(22550 < a["forward"] < 22700, a["forward"])
        row = next(x for x in a["rows"] if x["strike"] == 22800.0)
        S = O._S()
        back = S.black76_price(a["forward"], 22800.0, a["t_years"], a["rate"], row["ce"]["iv"] / 100.0, "CE")
        self.assertAlmostEqual(back, 62.5, places=2)                    # the IV re-prices the settle
        self.assertTrue(0.2 < row["ce"]["delta"] < 0.4)

    def test_missing_strike_falls_back_to_black76_flagged(self):
        day, exp, k = "2026-10-14", "2026-10-19", 22800.0
        board = CHAINS.boards[day]
        saved = board["opts"].pop((exp, k, "CE"))
        CHAINS._an.pop((day, exp), None)
        try:
            px, src = CHAINS.price(day, exp, k, "CE")
        finally:
            board["opts"][(exp, k, "CE")] = saved
            CHAINS._an.pop((day, exp), None)
        self.assertEqual(src, "model")
        self.assertLess(abs(px - 62.5) / 62.5, 0.15, px)               # the smile's neighbours price it closely

    def test_solve_iv_round_trip_and_refusals(self):
        S = O._S()
        p = S.black76_price(22600, 23000, 5 / 365.0, 0.0557, 0.13, "CE")
        self.assertAlmostEqual(O.solve_iv(p, 22600, 23000, 5 / 365.0, 0.0557, "CE"), 13.0, places=2)
        self.assertIsNone(O.solve_iv(10.0, 22600, 22000, 5 / 365.0, 0.0557, "CE"))   # under intrinsic
        self.assertIsNone(O.solve_iv(10.0, 22600, 23000, 0.0, 0.0557, "CE"))

    def test_monday_expiry_shift_from_the_archive(self):
        exps = O.expiries_for(O.validate(spec_doc()), CHAINS)
        self.assertIn("2026-10-19", exps)                               # Monday: 20-Oct was Dussehra
        self.assertNotIn("2026-10-20", exps)
        self.assertEqual(lab.datetime.date(2026, 10, 19).weekday(), 0)
        fly = O.validate(spec_doc("nifty-expiry-iron-fly"))
        self.assertEqual(O.entry_day_for(fly, CHAINS, "2026-10-19"), "2026-10-19")
        bt = O.backtest(fly, CHAINS)
        t = next(x for x in bt["trades"] if x["expiry"] == "2026-10-19")
        self.assertEqual((t["entry_date"], t["exit_date"], t["reason"]), ("2026-10-19", "2026-10-19", "expiry settlement"))
        fsp, how = CHAINS.final_settlement("2026-10-19")
        self.assertEqual(fsp, 22888.95)                                 # no weekly future: the index close
        self.assertIn("index close", how)
        self.assertEqual(CHAINS.final_settlement("2026-10-27")[0], 22749.75)   # the monthly future's settle
        for l in t["legs"]:
            want = max(0.0, fsp - l["strike"]) if l["option_type"] == "CE" else max(0.0, l["strike"] - fsp)
            self.assertAlmostEqual(l["exit"], round(want, 2), places=2)
            self.assertEqual(l["exit_source"], "settlement")
        self.assertEqual(t["margin_detail"]["expiry_day"], t["margin"])  # opened on expiry day: its ELM applies


class Charges(unittest.TestCase):
    def test_stt_on_exercise_at_expiry(self):
        leg = {"side": "BUY", "qty": 65, "fill": 40.0}
        cfg = {}
        intrinsic = 120.0
        got = O.leg_charges(cfg, leg, "2026-10-21", "2026-10-27", intrinsic, True)
        buy = lab.fno_round_trip_charges(40.0, 0, 65, cfg, "OPT", "2026-10-21")["total"]
        ex = lab.fno_round_trip_charges(0, intrinsic, 65, cfg, "OPT", "2026-10-27", True)
        self.assertAlmostEqual(got, buy + ex["total"], places=2)
        self.assertAlmostEqual(ex["stt"], round(0.0015 * intrinsic * 65, 2), places=2)   # 0.15% from 1 Apr 2026
        self.assertEqual((ex["brokerage"], ex["exchange"]), (0.0, 0.0))                # no closing order
        old = lab.fno_round_trip_charges(0, intrinsic, 65, cfg, "OPT", "2026-03-26", True)
        self.assertAlmostEqual(old["stt"], round(0.00125 * intrinsic * 65, 2), places=2)
        # an out-of-the-money long and an expiring short: no closing order, no exercise STT
        self.assertAlmostEqual(O.leg_charges(cfg, leg, "2026-10-21", "2026-10-27", 0.0, True), buy, places=2)
        short = {"side": "SELL", "qty": 65, "fill": 40.0}
        sell = lab.fno_round_trip_charges(0, 40.0, 65, cfg, "OPT", "2026-10-21")
        self.assertAlmostEqual(O.leg_charges(cfg, short, "2026-10-21", "2026-10-27", 55.0, True), sell["total"], places=2)
        self.assertAlmostEqual(sell["stt"], round(0.0015 * 40 * 65, 2), places=2)
        # closed before expiry: a buy-back on the exit day
        back = lab.fno_round_trip_charges(12.0, 0, 65, cfg, "OPT", "2026-10-24")["total"]
        self.assertAlmostEqual(O.leg_charges(cfg, short, "2026-10-21", "2026-10-24", 12.0, False),
                               sell["total"] + back, places=2)


class Engine(unittest.TestCase):
    def setUp(self):
        self.spec = O.validate(spec_doc())
        self.bt = O.backtest(self.spec, CHAINS)

    def test_strangle_trades_and_exits(self):
        tr = self.bt["trades"]
        self.assertEqual([t["expiry"] for t in tr], ["2026-10-13", "2026-10-19", "2026-10-27"])
        self.assertEqual(tr[2]["reason"], "profit target")
        for t in tr:
            self.assertEqual(t["lot_size"], 65)
            self.assertAlmostEqual(t["net"], t["gross"] - t["charges"], places=1)
            self.assertGreater(t["charges"], 0)
            shorts = [l for l in t["legs"] if l["side"] == "SELL"]
            self.assertEqual(len(shorts), 2)
            self.assertTrue(all(l["entry_source"] == "settle" for l in t["legs"]))
        self.assertEqual(self.bt["open"][0]["expiry"], "2026-11-03")   # entered 28-Oct, still open at the end
        self.assertEqual(json.dumps(O.backtest(self.spec, CHAINS)["trades"]), json.dumps(tr))   # deterministic

    def test_entry_fill_has_slippage_off_the_settle(self):
        t = self.bt["trades"][1]
        day = t["entry_date"]
        for l in t["legs"]:
            settle = CHAINS.price(day, l["expiry"], l["strike"], l["option_type"])[0]
            want = max(O.TICK, settle - (settle * 0.02 + 0.05))
            self.assertAlmostEqual(l["entry"], round(want, 2), places=2)
        worse = O.backtest(self.spec, CHAINS, 2.0)["trades"]
        self.assertLess(sum(x["net"] for x in worse), sum(x["net"] for x in self.bt["trades"]))

    def test_margin_and_return_on_margin(self):
        S = O._S()
        for t in self.bt["trades"]:
            m = t["margin_detail"]
            self.assertEqual(t["margin"], round(max(m["entry"], m["expiry_day"] or 0), 2))
            self.assertAlmostEqual(t["r"], t["net"] / t["margin"], places=4)
            self.assertAlmostEqual(t["return_on_margin_pct"], 100 * t["net"] / t["margin"], places=2)
            if t["exit_date"] == t["expiry"]:
                self.assertGreater(m["expiry_day"], m["entry"])        # 2% more ELM on the expiry day
            else:
                self.assertIsNone(m["expiry_day"])
        legs, a = O.pick_legs(self.spec, CHAINS, "2026-10-21", "2026-10-27")
        pos = S.Position(legs, {}, "2026-10-21")
        span = S.span_style(pos, "index", None, lab.datetime.datetime(2026, 10, 21, 15, 30))
        t = self.bt["trades"][2]
        self.assertAlmostEqual(t["margin_detail"]["entry"], round(span["total"], 2), places=1)
        self.assertTrue(100000 < t["margin"] < 400000, t["margin"])

    def test_delta_picks_through_the_strategy_template(self):
        legs, a = O.pick_legs(self.spec, CHAINS, "2026-10-21", "2026-10-27")
        for l in legs:
            self.assertLess(abs(abs(l["greeks"]["delta"]) - 0.16), 0.06, l)
        condor = O.validate(spec_doc("nifty-weekly-condor-16d"))
        legs, _ = O.pick_legs(condor, CHAINS, "2026-10-21", "2026-10-27")
        ks = sorted((l["strike"], l["option_type"], l["side"]) for l in legs)
        self.assertEqual(len(ks), 4)
        pe = sorted(l["strike"] for l in legs if l["option_type"] == "PE")
        ce = sorted(l["strike"] for l in legs if l["option_type"] == "CE")
        self.assertEqual((pe[1] - pe[0], ce[1] - ce[0]), (100.0, 100.0))

    def test_metrics(self):
        m = O.options_metrics(self.bt["trades"], self.bt["mtm"], CHAINS.days, 500000)
        nets = [t["net"] for t in self.bt["trades"]]
        self.assertEqual(m["positions"], 3)
        self.assertEqual(m["worst_trade"], min(nets))
        self.assertLessEqual(m["worst_week"], 0.0 if min(nets) < 0 else m["best_week"])
        self.assertLessEqual(m["tail_loss_1pct"], m["tail_loss_5pct"])
        self.assertTrue(0 <= m["time_in_drawdown_pct"] <= 100)
        self.assertTrue(0 <= m["breach_pct"] <= 100)
        self.assertEqual(m["expiries"], 3)

    def test_iv_rank_filter_waits_for_history(self):
        spec = O.validate(spec_doc("nifty-ivr-strangle", walk_forward={"in_sample": 8, "out_of_sample": 4}))
        bt = O.backtest(spec, CHAINS)
        self.assertEqual(bt["trades"], [])
        self.assertTrue(any("IV rank" in k for k in bt["skipped"]), bt["skipped"])
        short = O.validate(spec_doc("nifty-ivr-strangle", iv_rank={"lookback": 20, "days": 30}))
        rank, iv = CHAINS.iv_rank("2026-10-28", 20)
        self.assertTrue(rank is not None and 0 <= rank <= 100 and 5 < iv < 40, (rank, iv))
        self.assertIsNotNone(short)


class Run(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bt-lab-options-")
        cls.out = os.path.join(cls.tmp, "lab")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lab.main(["--out", self.out] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_run_shape_gates_and_scorecard(self):
        res = lab.run_lab(spec_doc(), CHAINS, {})
        self.assertEqual(res["kind"], "options")
        for k in ("full", "walk_forward", "grid", "monte_carlo", "dsr", "pbo", "regimes", "costs_sensitivity",
                  "gates", "options", "equity"):
            self.assertIn(k, res)
        self.assertEqual(len(res["grid"]["points"]), 9)
        self.assertEqual(len(res["gates"]), 8)
        self.assertEqual(set(res["costs_sensitivity"]["out_of_sample"]), {"1x", "1.5x", "2x"})
        self.assertEqual(len(res["equity"]), 20)
        self.assertIn("return_on_margin_pct", res["options"]["whole_sample"])
        v, why = lab.decide(res["gates"], res["gates_set"])
        self.assertEqual(v, "REJECT")                                   # 20 sessions cannot pass the gates
        self.assertIn("enough out-of-sample trades", why)
        loose = lab.run_lab(spec_doc(gates=LENIENT), CHAINS, {})
        self.assertTrue(all(g["pass"] for g in loose["gates"]), [g for g in loose["gates"] if not g["pass"]])
        self.assertEqual(lab.decide(loose["gates"], loose["gates_set"])[0], "WATCH")
        text = lab.scorecard(dict(res, verdict="REJECT", verdict_why="test"))
        for want in ("Options numbers", "return on margin", "tail loss", "End-of-day only", "survivorship of strikes",
                     "not financial advice"):
            self.assertIn(want, text)
        self.assertNotIn("guarantee", text.replace("not a guarantee", ""))
        self.assertEqual(json.dumps(res, sort_keys=True), json.dumps(lab.run_lab(spec_doc(), CHAINS, {}), sort_keys=True))

    def test_regime_split_comes_from_trader_regime(self):
        ctx = {"nifty": lab.Series("^NSEI", "1d", lab.archive_bars("^NSEI", "1d", ARC)),
               "vix": lab.Series("^INDIAVIX", "1d", lab.archive_bars("^INDIAVIX", "1d", ARC))}
        res = lab.run_lab(spec_doc(gates={"require_regime_edge": 1}), CHAINS, ctx)
        self.assertIn("market", res["regimes"])
        self.assertIn("NIFTY 50 trend or range", res["regimes"]["market"]["basis"])
        self.assertEqual(res["gates"][-1]["gate"][:26], "edge in the current regime")
        self.assertFalse(res["gates"][-1]["pass"])                      # too few trades there to judge

    def test_cli_run_report_compare_and_forward_basket(self):
        code, out, err = self.cli("run", "nifty-weekly-strangle-16d", "--archive", ARC, "--now", "2026-11-01 12:00")
        self.assertEqual(code, 0, err)
        self.assertIn("Options numbers", out)
        d = os.path.join(self.out, "nifty-weekly-strangle-16d")
        with open(os.path.join(d, "results.json"), encoding="utf-8") as fh:
            res = json.load(fh)
        self.assertEqual((res["kind"], res["data"]["sessions"]), ("options", 20))
        code, out, err = self.cli("compare", "nifty-weekly-strangle-16d", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["verdict"], "REJECT")
        # a REJECT drafts nothing
        code, out, err = self.cli("forward", "nifty-weekly-strangle-16d", "--archive", ARC, "--now", "2026-11-01 12:00")
        self.assertEqual(code, 0, err)
        self.assertIn("no paper baskets", out)
        # a spec that enters 4 days out (30-Oct for the 03-Nov weekly) and passed its gates drafts a basket
        doc = spec_doc(gates=LENIENT)
        doc["name"] = "strangle-4d"
        doc["params"]["dte"] = 4
        doc["entry"]["days_to_expiry"] = "$dte"
        path = os.path.join(self.tmp, "strangle-4d.json")
        with open(path, "w") as fh:
            json.dump(doc, fh)
        code, out, err = self.cli("run", path, "--archive", ARC, "--now", "2026-11-01 12:00")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("forward", path, "--archive", ARC, "--now", "2026-11-01 12:00", "--json")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(r["verdict"], "WATCH")
        self.assertEqual(len(r["baskets"]), 1)
        b = r["baskets"][0]
        self.assertEqual((b["decided_on"], b["expiry"], b["date"]), ("2026-10-30", "2026-11-03", "2026-11-02"))
        orders = b["orders"]
        self.assertTrue(orders["paper"] and orders["basket"])
        self.assertEqual(orders["status"], "proposed")
        self.assertEqual(len(orders["orders"]), 2)
        self.assertTrue(all(o["exchange"] == "NFO" and o["order_type"] == "LIMIT" for o in orders["orders"]))
        self.assertEqual(b["strategy"]["schema"], "bharat-trader/strategy/1")
        self.assertGreater(b["strategy"]["margin"]["required"], 0)
        self.assertTrue(os.path.isfile(r["written"][0]))
        self.assertIn("trader-risk", orders["note"])


if __name__ == "__main__":
    unittest.main()
