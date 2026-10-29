"""trader-exit tests, offline.

Fixtures (tests/fixtures/trader-exit/), cut from real Yahoo data fetched 2026-10-29:
  HDFCBANK-2026-10-26-5m.json   HDFCBANK.NS five-minute bars of Monday 2026-10-26 (75 bars,
                                09:15 to 15:25 IST; Yahoo leaves the 15:20 bar empty)
  entry-HDFCBANK-2026-10-26.json  the opening-range breakout in that data: the 09:15/09:20/09:25
                                bars make the range 687.85 to 692.00; the 10:05 bar (bar 10)
                                is the first to close above it, at 692.70, so the entry is 10:10.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, FIX, ROOT  # noqa: E402

exitp = load("trader-exit", "exit")

DIR = os.path.join(FIX, "trader-exit")
BARS = os.path.join(DIR, "HDFCBANK-2026-10-26-5m.json")
with open(os.path.join(DIR, "entry-HDFCBANK-2026-10-26.json"), encoding="utf-8") as _fh:
    ENTRY = json.load(_fh)
NOW = "2026-10-26 10:11"

# trader/plan.json for the fixture's trigger, defaults (or-mid stop, 2R target), as the contract writes it
PLAN = {"created_by": "trader-exit", "symbol": "HDFCBANK", "exchange": "NSE", "side": "buy", "date": "2026-10-26",
        "setup": "orb", "entry": 692.7, "entry_time": "10:10", "entry_bar": 10, "interval": "5m",
        "opening_range": {"high": 692.0, "low": 687.85, "minutes": 15},
        "stop": 689.9, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 698.3, "target_r": 2.0, "target_label": "2R (two times the risk)",
        "risk_per_share": 2.8, "reward_risk": 2.0, "flat_by": "15:10", "tick_size": 0.05,
        "reward_risk_net": 1.38}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = exitp.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=692.0, ol=687.85, atr=None, n=None, tick="0.05"):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, Decimal(tick))


class Money(unittest.TestCase):
    def test_inr_indian_grouping(self):
        cases = [(123456.5, "₹1,23,456.50"), (0, "₹0.00"), (999, "₹999.00"), (1000, "₹1,000.00"),
                 (99999.99, "₹99,999.99"), (100000, "₹1,00,000.00"), (10000000, "₹1,00,00,000.00"),
                 (123456789.123, "₹12,34,56,789.12"), (12.345, "₹12.35"), (-1234.5, "-₹1,234.50"),
                 (Decimal("53.15"), "₹53.15")]
        for x, want in cases:
            self.assertEqual(exitp.inr(x), want, x)
        for x, want in (("689.925", "₹689.925"), ("1163.35", "₹1,163.35"), ("123456.789", "₹1,23,456.789")):
            self.assertEqual(exitp.exact(x), want)

    def test_round_trip_charges_by_hand(self):
        # HDFCBANK, 72 shares, buy 692.70, sell 698.30 (the 2R target), intraday MIS, default rates:
        #   buy value  692.70 x 72 = 49,874.40      sell value 698.30 x 72 = 50,277.60   turnover 1,00,152.00
        #   brokerage  0.03% each leg, cap 20: 14.96232 + 15.08328 = 30.0456          -> 30.05
        #   STT        0.025% of the sell 50,277.60 = 12.5694                         -> 12.57
        #   exchange   0.00297% of 1,00,152.00 = 2.9745144                            -> 2.97
        #   SEBI       10 per crore: 1,00,152.00 x 10 / 1,00,00,000 = 0.100152        -> 0.10
        #   stamp      0.003% of the buy 49,874.40 = 1.496232                         -> 1.50
        #   GST        18% of (30.0456 + 2.9745144 + 0.100152) = 18% of 33.1202664 = 5.96164795 -> 5.96
        #   total      30.05 + 12.57 + 2.97 + 0.10 + 1.50 + 5.96 = 53.15
        cfg = exitp.read_config(None)
        self.assertEqual(exitp.round_trip_charges(692.70, 698.30, 72, cfg),
                         {"brokerage": 30.05, "stt": 12.57, "exchange": 2.97, "sebi": 0.1, "stamp": 1.5,
                          "gst": 5.96, "total": 53.15})
        # out at the stop 689.90: sell 49,672.80, brokerage 29.86416, STT 12.4182, GST 18% of 32.920258
        self.assertEqual(exitp.round_trip_charges(692.70, 689.90, 72, cfg)["total"], 52.77)

    def test_brokerage_cap_delivery_and_custom_rates(self):
        cfg = exitp.read_config(None)
        # 100 x 2,000 = 2,00,000 a leg: 0.03% would be 60, the cap makes it 20 a leg
        self.assertEqual(exitp.round_trip_charges(2000, 2000, 100, cfg)["brokerage"], 40.0)
        # CNC: STT 0.1% of both legs (400.00), stamp 0.015% of the buy (30.00)
        cnc = dict(cfg, product="CNC")
        ch = exitp.round_trip_charges(2000, 2000, 100, cnc)
        self.assertEqual((ch["stt"], ch["stamp"]), (400.0, 30.0))
        zero = dict(cfg, charges=dict(cfg["charges"], brokerage_pct=0))
        ch = exitp.round_trip_charges(692.70, 698.30, 72, zero)
        self.assertEqual((ch["brokerage"], ch["gst"]), (0.0, 0.55))     # 18% of 3.0746664
        parts = sum(v for k, v in ch.items() if k != "total")
        self.assertAlmostEqual(ch["total"], parts, places=9)


class Math(unittest.TestCase):
    def test_the_fixture_or_mid_2r(self):
        p = c(692.70, "or-mid")
        self.assertEqual(p["stop"], Decimal("689.90"))               # (692.00 + 687.85) / 2 = 689.925, down to 0.05
        self.assertEqual(p["risk"], Decimal("2.80"))
        self.assertEqual(p["target"], Decimal("698.30"))             # 692.70 + 2 x 2.80
        self.assertEqual(p["reward_risk"], Decimal("2.00"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertEqual(p["stop_detail"], "(₹692.00 + ₹687.85) / 2 = ₹689.925, rounded down to the ₹0.05 tick")

    def test_1r(self):
        p = c(692.70, "or-mid", "1R")
        self.assertEqual((p["target"], p["target_label"]), (Decimal("695.50"), "1R (one times the risk)"))

    def test_raw_yahoo_floats_give_the_same_ticks(self):
        # the fixture's own floats for the 10:05 close and the opening range
        p = c(692.7000122070312, "or-mid", "2R", 692.0, 687.8499755859375)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("692.70"), Decimal("689.90"), Decimal("698.30")))

    def test_other_stops(self):
        p = c(692.70, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("687.85"), Decimal("4.85"), Decimal("702.40")))
        p = c(692.70, "price:689.94")
        self.assertEqual(p["stop"], Decimal("689.90"))               # rounds down, away from the entry
        p = c(692.70, "pct:1")                                       # 692.70 x 0.99 = 685.773
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("685.75"), Decimal("6.95"), Decimal("706.60")))
        p = c(692.70, "atr:1.5", atr=2.11363636, n=11)               # 692.70 - 3.17045 = 689.52955
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("689.50"), Decimal("3.20"), Decimal("699.10")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(11) under the entry")
        self.assertIn("fewer than 14", p["stop_detail"])

    def test_target_rounds_to_the_nearest_tick(self):
        p = c(100, "price:99", "1.33R", None, None)                  # 101.33 -> 101.35
        self.assertEqual(p["target"], Decimal("101.35"))
        p = c(100, "price:99", "1.32R", None, None)                  # 101.32 -> 101.30
        self.assertEqual(p["target"], Decimal("101.30"))
        p = c(100, "price:99", "1.325R", None, None)                 # 101.325 is half way: up to 101.35
        self.assertEqual(p["target"], Decimal("101.35"))
        p = c(692.70, "or-mid", "price:700.02")
        self.assertEqual((p["target"], p["reward_risk"]), (Decimal("700.00"), Decimal("2.61")))
        self.assertEqual(p["target_label"], "your price (2.61R)")

    def test_another_tick_size(self):
        # RELIANCE trades above 1,000 where every price in the real data sits on a 0.10 tick
        p = c(1452.30, "pct:1", "2R", None, None, tick="0.10")       # 1437.777 -> 1437.70
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1437.70"), Decimal("14.60"), Decimal("1481.50")))
        p = c(1452.34, "or-mid", "1.5R", 1450.05, 1440.03, tick="0.10")   # entry 1452.30, range 1450.10/1440.00
        self.assertEqual((p["entry"], p["stop"]), (Decimal("1452.30"), Decimal("1445.00")))
        self.assertEqual(p["stop_detail"], "(₹1,450.10 + ₹1,440.00) / 2 = ₹1,445.05, rounded down to the ₹0.10 tick")
        self.assertEqual(p["target"], Decimal("1463.30"))            # 1452.30 + 1.5 x 7.30 = 1463.25 -> 1463.30

    def test_refusals_say_why(self):
        cases = [("price:692.70", "2R", "at or above the entry"),
                 ("price:693", "2R", "at or above the entry"),
                 ("or-mid", "price:692.70", "at or below the entry"),
                 ("or-mid", "price:692.72", "at or below the entry"),   # rounds to 692.70
                 ("price:692.45", "2R", "too tight to survive the spread"),
                 ("pct:0.03", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(692.70, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # the minimum is the larger of 0.05% of 692.70 (0.34635) and two ticks (0.10): 0.35 passes, 0.30 does not
        self.assertEqual(c(692.70, "price:692.35")["risk"], Decimal("0.35"))
        with self.assertRaises(exitp.PlanError) as cm:
            c(692.70, "price:692.40")
        self.assertIn("₹0.35", str(cm.exception))
        # on a ₹100 stock two ticks (0.10) is the larger: one tick under is refused
        with self.assertRaises(exitp.PlanError):
            c(100, "price:99.95", "2R", None, None)
        self.assertEqual(c(100, "price:99.90", "2R", None, None)["risk"], Decimal("0.10"))
        with self.assertRaises(exitp.PlanError):
            c(687.00, "or-mid")                                       # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(692.70, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(692.70, "or-mid", oh=687.85, ol=692.0)
        with self.assertRaises(exitp.PlanError):
            c(692.70, "or-mid", tick="0")

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("HDFCBANK", "5m", None, BARS)
        day = exitp.datetime.date(2026, 10, 26)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=10)
        self.assertEqual(len(bars), 11)                               # 09:15 ... the 10:05 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 692.0 - 688.15, places=3)      # the 09:15 bar, no overnight gap
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 11)
        self.assertAlmostEqual(atr, sum(trs) / 11, places=9)
        self.assertAlmostEqual(atr, 2.1136, places=4)
        # the whole day: 73 bars with a price (Yahoo left 15:15 and 15:20 empty), the last 14 count
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 73)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        self.assertEqual(exitp.session_bars(ch, exitp.datetime.date(2026, 10, 25), "5m"), [])   # a Sunday


class Calendar(unittest.TestCase):
    def test_market_days(self):
        md = exitp.market_day
        d = exitp.datetime.date
        self.assertEqual(md(d(2026, 10, 26))["kind"], "regular")
        self.assertEqual((md(d(2026, 10, 26))["start"], md(d(2026, 10, 26))["end"]),
                         (exitp.datetime.time(9, 15), exitp.datetime.time(15, 30)))
        self.assertIn("Dussehra", md(d(2026, 10, 20))["reason"])
        self.assertFalse(md(d(2026, 11, 10))["open"])
        self.assertFalse(md(d(2026, 10, 24))["open"])                 # Saturday
        self.assertFalse(md(d(2026, 10, 27), {"extra_holidays": ["2026-10-27"]})["open"])
        sp = md(d(2026, 11, 8), {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]})
        self.assertEqual((sp["open"], sp["kind"], exitp.hm(sp["start"]), exitp.hm(sp["end"])),
                         (True, "special", "18:00", "19:00"))
        y27 = md(d(2027, 1, 4))
        self.assertTrue(y27["open"])
        self.assertIn("calendar for 2027 is unknown", y27["warning"])

    def test_times_are_ist(self):
        self.assertEqual(exitp.parse_now("2026-10-26 10:11"), exitp.datetime.datetime(2026, 10, 26, 10, 11))
        self.assertEqual(exitp.parse_now("2026-10-26T04:41:00Z"), exitp.datetime.datetime(2026, 10, 26, 10, 11))
        self.assertEqual(exitp.ist_of(1792986300), exitp.datetime.datetime(2026, 10, 26, 9, 15))   # 2026-10-26 03:45Z, the fixture's first bar
        self.assertEqual(exitp.parse_clock("10:10", "t"), exitp.datetime.time(10, 10))
        self.assertEqual(exitp.parse_clock("2:35 PM", "t"), exitp.datetime.time(14, 35))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self.write(os.path.join("trader", "entry.json"), ENTRY)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args, now=NOW):
        return run(["plan", "--now", now] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, text, err = self.plan("--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["created_at"], "2026-10-26T04:41:00Z")
        ch = p["charges"]
        self.assertEqual((ch["qty"], ch["qty_source"], ch["product"]), (72, "estimate", "MIS"))
        self.assertEqual((ch["at_target"]["total"], ch["at_stop"]["total"]), (53.15, 52.77))
        # 403.20 - 53.15 = 350.05 over 201.60 + 52.77 = 254.37 -> 1.376 -> 1.38
        self.assertEqual((ch["reward_gross"], ch["reward_net"], ch["risk_gross"], ch["risk_net"]),
                         (403.2, 350.05, 201.6, 254.37))
        self.assertIn("Entry         ₹692.70 at 10:10 IST (bar 10)", text)
        self.assertIn("Stop          ₹689.90  opening-range midpoint", text)
        self.assertIn("Target        ₹698.30  2R (two times the risk)", text)
        self.assertIn("Risk a share  ₹2.80 (0.40% of the entry)", text)
        self.assertIn("Reward:risk   2.0 gross, 1.38 net of charges", text)
        self.assertIn("₹53.15 at the target, ₹52.77 at the stop, round trip on 72 shares", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())
        self.assertNotIn("Note:", text)
        code, text, _ = self.plan("--target", "1R", "--write")
        self.assertEqual((self.read_plan()["target"], self.read_plan()["target_r"]), (695.5, 1.0))
        self.assertIn("under your min_reward_risk 1.5", text)       # the default 1.5 from the spec
        code, text, _ = run(["show", "--now", "2026-10-26 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("₹695.50", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(["show", "--now", "2026-10-27 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, err = self.plan("--symbol", "hdfcbank", "--entry", "692.70", "--or-high", "692.00",
                                    "--or-low", "687.85", "--time", "10:10", "--entry-bar", "10",
                                    "--date", "2026-10-26", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        code, text, _ = self.plan("--symbol", "M&M", "--entry", "3000", "--stop", "pct:1", "--json")
        self.assertEqual(json.loads(text)["symbol"], "M&M")
        code, _, err = self.plan("--symbol", "TCS.NS", "--entry", "3000", "--stop", "pct:1")
        self.assertIn("not an NSE symbol", err)

    def test_qty_and_no_charges(self):
        code, text, _ = self.plan("--qty", "10", "--json")
        p = json.loads(text)
        self.assertEqual((p["charges"]["qty"], p["charges"]["qty_source"]), (10, "--qty"))
        self.assertEqual(p["charges"]["at_target"]["total"],
                         exitp.round_trip_charges(692.70, 698.30, 10, exitp.read_config(None))["total"])
        # percentage charges scale with the size, so net reward:risk is the same 1.38 at 10 shares
        self.assertEqual(p["reward_risk_net"], 1.38)
        # a flat ₹20 an order (brokerage_pct so high the cap always binds) hurts a small size far more
        self.write(os.path.join("trader", "config.json"), {"charges": {"brokerage_pct": 100}})
        self.assertEqual(json.loads(self.plan("--qty", "10", "--json")[1])["reward_risk_net"], 0.08)
        self.assertEqual(json.loads(self.plan("--json")[1])["reward_risk_net"], 1.27)
        os.remove(os.path.join("trader", "config.json"))
        code, text, _ = self.plan("--no-charges", "--json")
        p = json.loads(text)
        self.assertNotIn("charges", p)
        self.assertNotIn("reward_risk_net", p)
        self.write(os.path.join("trader", "config.json"), {"include_charges": False})
        code, text, _ = self.plan()
        self.assertIn("Reward:risk   2.0 (charges not included)", text)
        code, _, err = self.plan("--qty", "0")
        self.assertIn("--qty must be 1 or more", err)

    def test_config_defaults_with_missing_keys(self):
        self.write(os.path.join("trader", "config.json"), {"capital": 50000, "risk_per_trade_pct": 0.5,
                                                          "max_order_value": 1000000,
                                                          "charges": {"brokerage_cap": 10}})
        cfg = exitp.read_config(os.path.join("trader", "config.json"))
        self.assertEqual((cfg["tick_size"], cfg["flat_by"], cfg["product"], cfg["include_charges"]),
                         ("auto", "15:10", "MIS", True))
        self.assertEqual((cfg["charges"]["brokerage_cap"], cfg["charges"]["stt_sell_pct"]), (10, 0.025))
        self.assertEqual(exitp.max_loss_of(cfg), Decimal("250"))     # 50,000 x 0.5% (no explicit max loss)
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual((p["stop"], p["target"], p["flat_by"], p["tick_size"]), (689.9, 698.3, "15:10", 0.05))
        self.assertEqual(p["charges"]["qty"], 89)                      # floor(250 / 2.80)
        self.write(os.path.join("trader", "config.json"), {"capital": 50000, "max_loss_per_trade": 140})
        self.assertEqual(json.loads(self.plan("--json")[1])["charges"]["qty"], 50)   # explicit: floor(140 / 2.80)
        self.write(os.path.join("trader", "config.json"), {"tick_size": 0.10})
        p = json.loads(self.plan("--json")[1])
        self.assertEqual((p["stop"], p["target"]), (689.9, 698.3))   # 689.925 -> 689.90 on 0.10 too
        self.write(os.path.join("trader", "config.json"), {})
        self.assertEqual(json.loads(self.plan("--json")[1])["charges"]["qty"], 72)

    def test_atr_stop_reads_bars(self):
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", BARS, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (689.5, 3.2, 699.1))
        self.assertEqual((p["stop_rule"], p["atr_bars"], p["atr"]), ("atr", 11, 2.1136))
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", BARS, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("HDFCBANK is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "692.05", "--json")     # plan ahead just over the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (692.05, 689.9, None))

        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, setup="level", level=692.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "HDFCBANK")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "HDFCBANK", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "HDFCBANK", "--entry", "692.70", "--or-high", "692.00")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_holidays_and_special_sessions(self):
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 2.5})
        p = json.loads(self.plan("--json")[1])
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 2.5", p["warnings"][0])
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:20"})
        p = json.loads(self.plan("--json")[1])
        self.assertIn("late for MIS", p["warnings"][0])
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:30"})
        code, _, err = self.plan()
        self.assertIn("outside the 09:15 to 15:30 IST session", err)
        os.remove(os.path.join("trader", "config.json"))
        code, _, err = self.plan("--flat-by", "15:45")
        self.assertIn("outside the 09:15 to 15:30 IST session", err)
        code, _, err = self.plan("--flat-by", "10:10")
        self.assertIn("no time left to hold it", err)

        # a day NSE is shut
        for day, words in (("2026-10-20", "Dussehra"), ("2026-11-10", "Diwali-Balipratipada"),
                           ("2026-10-24", "a Saturday")):
            self.write(os.path.join("trader", "entry.json"), dict(ENTRY, date=day))
            code, _, err = self.plan()
            self.assertEqual(code, 1, day)
            self.assertIn("NSE is shut", err)
            self.assertIn(words, err)

        # Muhurat trading: a special session from the config, 18:00 to 19:00 on the Sunday holiday
        self.write(os.path.join("trader", "config.json"),
                   {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]})
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, date="2026-11-08", time="18:20", bar_index=3))
        p = json.loads(self.plan("--json", now="2026-11-08 18:21")[1])
        self.assertEqual(p["flat_by"], "18:55")
        self.assertIn("5 minutes before the close of the special session", p["warnings"][0])
        code, _, err = self.plan("--flat-by", "15:10", now="2026-11-08 18:21")
        self.assertIn("outside the 18:00 to 19:00 IST session", err)

        # a year without a holiday table: open on weekdays, with a warning
        os.remove(os.path.join("trader", "config.json"))
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, date="2027-01-04"))
        p = json.loads(self.plan("--json", now="2027-01-04 10:11")[1])
        self.assertIn("calendar for 2027 is unknown", p["warnings"][0])

    def test_show_without_a_plan(self):
        code, text, _ = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        desc = text.split("description:", 1)[1].split("argument-hint:", 1)[0]
        self.assertGreater(len(" ".join(desc.split())), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", "₹689.90", "₹698.30", "₹2.80",
                     "₹53.15", "1.38 net of charges", "15:10 ist", "mis", "ist", "nse",
                     "too tight to survive the spread", "not financial advice", "typed an explicit yes"):
            self.assertIn(must, low)
        for never in ("—", "pro" + "fit", "ny" + "se", "\x24", " et,", "skill_dir"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
