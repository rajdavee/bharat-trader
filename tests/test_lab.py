"""trader-lab tests, offline.

Fixtures (tests/fixtures/trader-lab/), real Yahoo Finance v8 chart data fetched 2026-11-01 (a
Sunday) and stored by lab.py's own converter (bars_from_yahoo + write_bars_csv) as
date,time,open,high,low,close,volume CSV, gzipped:
  <SYMBOL>-5m.csv.gz      RELIANCE HDFCBANK ICICIBANK INFY TCS SBIN BHARTIARTL ITC LT AXISBANK:
                          range=60d interval=5m, 60 sessions 2026-08-05 to 2026-10-30, 09:15 to
                          15:25 IST starts. Yahoo leaves about 115 bars a stock empty (mostly
                          15:15 to 15:25, the closing auction); those are dropped. The 09:15
                          bar's volume is 0 on Yahoo.
  <SYMBOL>-1d.csv.gz      the same ten, range=2y interval=1d, 496 sessions 2024-10-30 to 2026-10-30.
  ^NSEI-1d.csv.gz         NIFTY 50, daily, 2 years (volume set to 0: Yahoo has none for the index).
  ^INDIAVIX-1d.csv.gz     India VIX, daily, 2 years (4 empty days dropped).
The Yahoo-parser test reuses tests/fixtures/trader-budget/yahoo-RELIANCE-2026-10-29-5m.json.
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
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

lab = load("trader-lab", "lab")
DIR = os.path.join(FIX, "trader-lab")
NOW = lab.ist_now("2026-11-01 12:00")
THREE = ["RELIANCE", "HDFCBANK", "SBIN"]


def archive_with(root, hdfc_july=False, reliance_1m=False):
    """A trader-archive folder built from tests/fixtures/trader-archive (real NSE and Upstox answers)."""
    import datetime
    import gzip as _gz
    arc = load("trader-archive", "archive")
    adir = os.path.join(FIX, "trader-archive")

    class Op(object):
        def __call__(self, req, timeout=None):
            import urllib.error
            name = req.full_url.rsplit("/", 1)[-1]
            path = os.path.join(adir, "vixhistory_2026Q4.json" if "vixhistory" in name else
                                "corporate_actions.json" if "corporateActions" in name else name)
            if not os.path.isfile(path):
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
            data = open(path, "rb").read()

            class R(object):
                status, headers = 200, {}

                def read(self):
                    return data

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    now = datetime.datetime(2026, 10, 26, 8, 0)
    h = arc.Http(sleep=lambda s: None, opener=Op(), clock=lambda: 0.0)
    arc.backfill(arc.Store(root), h, "2026-10-21", "2026-10-23", ["eq", "index", "vix", "ca"], now)

    class T(object):
        def __init__(self, fixture):
            self.fixture = fixture

        def request(self, method, url, **kw):
            with _gz.open(os.path.join(adir, self.fixture), "rt", encoding="utf-8") as fh:
                return json.load(fh)
    for flag, sym, iv, fx, a, b in ((hdfc_july, "HDFCBANK", "5m", "upstox_HDFCBANK_5m_202607.json.gz",
                                     "2026-07-01", "2026-07-31"),
                                    (reliance_1m, "RELIANCE", "1m", "upstox_RELIANCE_1m_20261022-23.json.gz",
                                     "2026-10-22", "2026-10-23")):
        if flag:
            ad = arc.broker_adapters().make("upstox", env={"BT_UPSTOX_ACCESS_TOKEN": "test-token"}, transport=T(fx))
            arc.intraday_ingest(root, [sym], iv, a, b, adapter=ad, now=now, sleep=lambda s: None)
    return root


def series(sym, tf="5m"):
    return lab.Series(sym, tf, lab.load_bars(sym, tf, DIR, now=NOW)[0])


def data_for(syms, tf="5m"):
    return {s: series(s, tf) for s in syms}


def context():
    return {"nifty": series("^NSEI", "1d"), "vix": series("^INDIAVIX", "1d")}


def spec_doc(name, **over):
    doc = copy.deepcopy(lab.library()[name])
    doc.update(over)
    return doc


LENIENT = {"min_oos_trades": 0, "min_oos_expectancy_r": -100, "min_oos_profit_factor": 0, "max_drawdown_pct": 100,
           "min_dsr": 0, "max_pbo": 1.01, "min_neighbourhood_positive": 0, "min_forward_trades": 100,
           "demote_drawdown_mult": 1000}


class Data(unittest.TestCase):
    def test_yahoo_bars_regular_session_only(self):
        with open(os.path.join(FIX, "trader-budget", "yahoo-RELIANCE-2026-10-29-5m.json"), encoding="utf-8") as fh:
            text = fh.read()
        bars = lab.bars_from_yahoo(text, "5m", lab.ist_now("2026-10-29 16:00"))
        self.assertTrue(bars)
        self.assertEqual(bars[0]["time"], "09:15")
        for b in bars:
            m = lab.minutes_of(b["time"])
            self.assertTrue(lab.OPEN_MIN <= m < lab.CLOSE_MIN and m % 5 == 0, b)
            self.assertTrue(b["l"] <= min(b["o"], b["c"]) <= max(b["o"], b["c"]) <= b["h"], b)
        early = lab.bars_from_yahoo(text, "5m", lab.ist_now("2026-10-29 09:31"))
        self.assertEqual([b["time"] for b in early], ["09:15", "09:20", "09:25"])   # complete bars only

    def test_fixtures_cover_ten_names_and_csv_round_trips(self):
        for sym in ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"]:
            five, day = series(sym, "5m"), series(sym, "1d")
            self.assertEqual(len(set(five.date)), 60, sym)
            self.assertEqual((five.date[0], five.date[-1]), ("2026-08-05", "2026-10-30"))
            self.assertGreater(day.n, 490, sym)
        bars = lab.load_bars("RELIANCE", "5m", DIR, now=NOW)[0][:100]
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "X-5m.csv.gz")
            lab.write_bars_csv(p, bars)
            with open(p, "rb") as fh:
                first = fh.read()
            lab.write_bars_csv(p, bars)
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), first)          # gzip without a timestamp: same bytes
            self.assertEqual(lab.read_bars_csv(p), bars)
        finally:
            shutil.rmtree(tmp)

    def test_a_pinned_clock_sees_only_complete_bars(self):
        five = lab.load_bars("RELIANCE", "5m", DIR, now=NOW)[0]
        seen = lab.seen_by(five, "5m", lab.ist_now("2026-10-29 09:50"))
        self.assertEqual((seen[-1]["date"], seen[-1]["time"]), ("2026-10-29", "09:45"))
        day = lab.load_bars("RELIANCE", "1d", DIR, now=NOW)[0]
        self.assertEqual(lab.seen_by(day, "1d", lab.ist_now("2026-10-29 15:00"))[-1]["date"], "2026-10-28")
        self.assertEqual(lab.seen_by(day, "1d", lab.ist_now("2026-10-29 15:45"))[-1]["date"], "2026-10-29")
        spec = lab.validate_spec(spec_doc("rsi2-nifty50", universe=["TCS"]))
        data, ctx, notes = lab.load_data(spec, DIR, now=lab.ist_now("2026-10-23 16:00"))
        self.assertEqual(data["TCS"].date[-1], "2026-10-23")
        self.assertEqual(ctx["vix"].date[-1], "2026-10-23")
        self.assertEqual(notes, [])

    def test_known_opening_range(self):
        # trader-budget's real example: RELIANCE 2026-10-29, opening range 09:15-09:30 = 1156.20 to 1170.50
        s = series("RELIANCE")
        hi = lab.operand_values(s, {"ind": "or_high", "n": 15, "of": "close", "shift": 0})
        lo = lab.operand_values(s, {"ind": "or_low", "n": 15, "of": "close", "shift": 0})
        day = [i for i in range(s.n) if s.date[i] == "2026-10-29"]
        self.assertIsNone(hi[day[1]])                          # 09:20: the range is not complete yet
        self.assertEqual((hi[day[2]], lo[day[2]]), (1170.5, 1156.2))    # known at the 09:25 bar's close
        self.assertEqual((hi[day[-1]], lo[day[-1]]), (1170.5, 1156.2))


class Indicators(unittest.TestCase):
    def test_sma_ema_rsi_by_hand(self):
        x = [10.0, 11.0, 12.0, 11.0, 13.0, 14.0]
        self.assertEqual(lab._sma(x, 3), [None, None, 11.0, 34 / 3.0, 12.0, 38 / 3.0])
        e = lab._ema(x, 3)                                     # seed 11.0, then a = 0.5
        self.assertEqual(e[:3], [None, None, 11.0])
        self.assertAlmostEqual(e[3], 11.0)
        self.assertAlmostEqual(e[4], 12.0)
        self.assertAlmostEqual(e[5], 13.0)
        r = lab._rsi(x, 2)                                     # changes +1 +1 -1 +2 +1
        self.assertEqual(r[:2], [None, None])
        self.assertEqual(r[2], 100.0)                          # no losses yet
        self.assertAlmostEqual(r[3], 100 - 100 / (1 + 0.5 / 0.5))     # ag (1+0)/2, al (0+1)/2
        self.assertAlmostEqual(r[4], 100 - 100 / (1 + 1.25 / 0.25))   # ag (0.5+2)/2, al (0.5+0)/2

    def test_atr_wilder(self):
        bars = [{"date": "2026-01-0%d" % (i + 1), "time": "", "o": 10, "h": h, "l": l, "c": c, "v": 0}
                for i, (h, l, c) in enumerate([(11, 9, 10), (12, 10, 11), (13, 10, 12), (12, 11, 11)])]
        s = lab.Series("X", "1d", bars)
        a = lab._atr(s, 2)                                     # TR 2, 2, 3, 1
        self.assertEqual(a[0], None)
        self.assertAlmostEqual(a[1], 2.0)
        self.assertAlmostEqual(a[2], 2.5)
        self.assertAlmostEqual(a[3], 1.75)

    def test_session_values(self):
        s = series("SBIN")
        prev_high = lab.operand_values(s, {"sess": "prev_high", "shift": 0})
        vwap = lab.operand_values(s, {"sess": "vwap", "shift": 0})
        spans = s.day_spans()
        a0, b0 = spans[0]
        a1, b1 = spans[1]
        self.assertTrue(all(v is None for v in prev_high[a0:b0]))
        self.assertEqual(prev_high[a1], max(s.h[a0:b0]))
        self.assertIsNone(vwap[a1])                            # Yahoo's 09:15 bar has no volume
        self.assertTrue(min(s.l[a1:a1 + 2]) <= vwap[a1 + 1] <= max(s.h[a1:a1 + 2]))


class Vocabulary(unittest.TestCase):
    def test_library_has_six_valid_specs(self):
        lib = lab.library()
        for name in ("orb15-vwap", "vwap-reclaim", "gap-fade", "pdh-breakout", "rsi2-nifty50", "breakout20-swing"):
            self.assertIn(name, lib)
            spec = lab.validate_spec(lib[name])
            self.assertEqual(spec["name"], name)
            self.assertLessEqual(len(lab.grid_points(spec)[2]), lab.MAX_GRID)
        self.assertGreaterEqual(len(lib), 6)

    def bad(self, change, msg=None):
        doc = spec_doc("orb15-vwap")
        change(doc)
        with self.assertRaises(lab.SpecError) as cm:
            lab.validate_spec(doc)
        if msg:
            self.assertIn(msg, str(cm.exception))

    def test_rejects_code_and_unknown_names(self):
        for evil in ("__import__('os').system('rm -rf /')", "close; import os", "eval", "exec(open('x').read())",
                     "lambda: 1", "close + 1", "os.environ"):
            self.bad(lambda d, e=evil: d.update(entry=["close", ">", e]), "unknown name")
        self.bad(lambda d: d.update(entry=["close", "and", "open"]), "op one of")
        self.bad(lambda d: d.update(entry={"exec": ["close"]}), "unknown condition")
        self.bad(lambda d: d.update(entry=["close", ">", {"__class__": 1}]), "unknown indicator")
        self.bad(lambda d: d.update(entry=["close", ">", {"sma": 20, "of": "__dict__"}]), "bar field")
        self.bad(lambda d: d.update(entry=["close", ">", "$nope"]), "unknown param")
        self.bad(lambda d: d.update(entry=["close", ">", True]), "want a number")
        self.bad(lambda d: d.update(entry=["close", ">", float("nan")]), "want a number")
        self.bad(lambda d: d.update(entry=["close", ">", {"sma": 100000}]), "outside")
        self.bad(lambda d: d.update(params={"or_minutes": "15", "target_r": 2}), "a param")
        self.bad(lambda d: d.update(hook="os.system"), "unknown keys")

    def test_rejects_deep_or_huge_rules(self):
        deep = ["close", ">", "open"]
        for _ in range(12):
            deep = {"not": deep}
        self.bad(lambda d: d.update(entry=deep), "deep")
        wide = {"all": [{"any": [["close", ">", "open"]] * 20}] * 20}
        self.bad(lambda d: d.update(entry=wide), "too many")

    def test_rejects_unsound_specs(self):
        self.bad(lambda d: d.update(side="short"), "long only")
        self.bad(lambda d: d.update(timeframe="1d"), "MIS")
        self.bad(lambda d: d.pop("stop"), "stop")
        self.bad(lambda d: d.update(kind="options"), "for an options spec")
        self.bad(lambda d: d.update(universe=["reliance"]), "NSE symbol")
        self.bad(lambda d: d.update(grid={"or_minutes": [5, 10, 15, 20, 25, 30, 45], "target_r": [1, 1.5, 2, 2.5, 3, 4]}),
                 "36 or fewer")
        self.bad(lambda d: d["sizing"].update(risk_per_trade_pct=8), "at most 5")
        swing = spec_doc("rsi2-nifty50", entry=["close", ">", "vwap"])
        with self.assertRaises(lab.SpecError):
            lab.validate_spec(swing)                          # no VWAP on daily bars

    def test_params_substitute_and_lookback(self):
        spec = lab.validate_spec(spec_doc("breakout20-swing"), params={"lookback": 40})
        self.assertEqual(spec["entry"]["b"]["n"], 40)
        self.assertEqual(spec["lookback"], 41)                # 40 bars, shifted one


class NoLookAhead(unittest.TestCase):
    def check_spec(self, doc, sym, tf, cut_frac=0.6):
        spec = lab.validate_spec(doc)
        s = series(sym, tf)
        before = lab.signals(spec, s)
        k = int(s.n * cut_frac)
        bars = copy.deepcopy(s.bars)
        rng = random.Random(7)
        for b in bars[k + 1:]:                                 # scramble every bar after k
            f = rng.uniform(0.7, 1.3)
            b.update(o=b["o"] * f, h=b["h"] * f * 1.01, l=b["l"] * f * 0.99, c=b["c"] * f, v=b["v"] * 3)
        after = lab.signals(spec, lab.Series(sym, tf, bars))
        for key in ("entry", "stop", "target", "exit"):
            if before[key] is None:
                continue
            self.assertEqual(before[key][:k + 1], after[key][:k + 1], "%s %s" % (doc["name"], key))
        return spec, s, bars, k

    def test_future_bars_do_not_change_past_signals(self):
        for name in ("orb15-vwap", "vwap-reclaim", "gap-fade", "pdh-breakout"):
            self.check_spec(spec_doc(name), "RELIANCE", "5m")
        for name in ("rsi2-nifty50", "breakout20-swing"):
            self.check_spec(spec_doc(name), "ICICIBANK", "1d")

    def test_future_bars_do_not_change_finished_trades(self):
        doc = spec_doc("orb15-vwap", universe=THREE)
        spec = lab.validate_spec(doc)
        data = data_for(THREE)
        cut = "2026-09-30"
        full = lab.backtest(spec, data)["trades"]
        messy = {}
        for sym, s in data.items():
            bars = copy.deepcopy(s.bars)
            for b in bars:
                if b["date"] > cut:
                    b.update(o=b["o"] * 1.2, h=b["h"] * 1.25, l=b["l"] * 1.15, c=b["c"] * 1.2)
            messy[sym] = lab.Series(sym, "5m", bars)
        other = lab.backtest(spec, messy)["trades"]
        done = [t for t in full if t["exit_date"] <= cut]
        self.assertTrue(done)
        self.assertEqual(done, [t for t in other if t["exit_date"] <= cut])

    def test_signal_on_close_fills_at_next_open(self):
        doc = spec_doc("orb15-vwap", universe=["RELIANCE"])
        spec = lab.validate_spec(doc)
        s = series("RELIANCE")
        sig = lab.signals(spec, s)
        res = lab.backtest(spec, {"RELIANCE": s})
        self.assertTrue(res["trades"])
        for t in res["trades"]:
            i = [j for j in range(s.n) if s.date[j] == t["entry_date"] and s.bars[j]["time"] == t["entry_time"]][0]
            self.assertTrue(sig["entry"][i - 1], t)           # the signal was the bar BEFORE the fill
            self.assertFalse(s.new_day[i])
            slip = lab.slippage(s.o[i], spec["costs"])
            self.assertAlmostEqual(t["entry"], lab.round_paise(s.o[i] + slip), places=2)


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = spec_doc("orb15-vwap", universe=THREE)
        cls.spec = lab.validate_spec(cls.doc)
        cls.data = data_for(THREE)
        cls.res = lab.backtest(cls.spec, cls.data)

    def test_charges_on_every_trade(self):
        cfg = self.spec["cfg"]
        self.assertTrue(self.res["trades"])
        for t in self.res["trades"]:
            ch = lab.round_trip_charges(t["entry"], t["exit"], t["qty"], cfg)["total"]
            self.assertAlmostEqual(t["charges"], ch, places=2)
            self.assertAlmostEqual(t["net"], t["gross"] - t["charges"], places=2)
            self.assertAlmostEqual(t["gross"], (t["exit"] - t["entry"]) * t["qty"], places=1)
        double = lab.backtest(self.spec, self.data, cost_mult=2.0)["trades"]
        t = double[0]
        ch = lab.round_trip_charges(t["entry"], t["exit"], t["qty"], cfg)["total"]
        self.assertAlmostEqual(t["charges"], lab.round_paise(ch * 2), places=2)
        self.assertLess(sum(x["net"] for x in double), sum(x["net"] for x in self.res["trades"]))

    def test_cnc_pays_delivery_charges(self):
        spec = lab.validate_spec(spec_doc("rsi2-nifty50", universe=["HDFCBANK"]))
        trades = lab.backtest(spec, {"HDFCBANK": series("HDFCBANK", "1d")})["trades"]
        self.assertTrue(trades)
        t = trades[0]
        self.assertEqual(spec["cfg"]["product"], "CNC")
        self.assertEqual(t["charges"], lab.round_trip_charges(t["entry"], t["exit"], t["qty"],
                                                              dict(spec["cfg"], product="CNC"))["total"])
        self.assertGreater(t["charges"], lab.round_trip_charges(t["entry"], t["exit"], t["qty"],
                                                                dict(spec["cfg"], product="MIS"))["total"])

    def test_mis_flat_by_and_entry_window_follow_the_closing_auction(self):
        for t in self.res["trades"]:
            self.assertEqual(t["entry_date"], t["exit_date"])          # MIS: never overnight
            self.assertLessEqual(t["exit_time"], "15:10", t)          # CAS: continuous trading ends 15:15
            self.assertLessEqual(t["entry_time"], "11:35", t)          # window to 11:30 + the next bar
        self.assertIn("flat-by", {t["reason"] for t in self.res["trades"]})
        self.assertEqual(lab._day_rules(self.spec, "2026-10-29", "RELIANCE", None), (570, 885, 910))
        groww = lab.validate_spec(spec_doc("orb15-vwap", sizing={"broker_profile": "groww"}))
        self.assertEqual(lab._day_rules(groww, "2026-10-29", "RELIANCE", None)[2], 15 * 60)   # 15:05 - 5 min
        late = lab.validate_spec(spec_doc("pdh-breakout", window={"to": "15:00"}))
        self.assertEqual(lab._day_rules(late, "2026-10-29", "RELIANCE", None)[1], 14 * 60 + 45)

    def test_one_position_per_symbol_and_day_limits(self):
        by = {}
        for t in self.res["trades"]:
            by.setdefault(t["symbol"], []).append(t)
        for sym, ts in by.items():
            ts.sort(key=lambda t: (t["entry_date"], t["entry_time"]))
            for a, b in zip(ts, ts[1:]):
                self.assertLessEqual((a["exit_date"], a["exit_time"]), (b["entry_date"], b["entry_time"]), sym)
        one = lab.validate_spec(spec_doc("orb15-vwap", universe=THREE,
                                         sizing={"capital": 100000, "risk_per_trade_pct": 1, "max_trades_per_day": 1}))
        res = lab.backtest(one, self.data)
        days = [t["entry_date"] for t in res["trades"]]
        self.assertEqual(len(days), len(set(days)))
        self.assertIn("max trades a day", res["skipped"])

    def test_losses_stay_inside_the_max_loss_unless_it_gaps(self):
        ml = self.spec["cfg"]["max_loss_per_trade"]
        for t in self.res["trades"]:
            if t["reason"] == "stop":
                self.assertLess(-t["net"], ml * 1.2, t)       # slippage and a worse fill can push it a little

    def test_deterministic(self):
        again = lab.backtest(self.spec, data_for(THREE))
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.res, sort_keys=True))


class Robustness(unittest.TestCase):
    def test_walk_forward_splits_by_hand(self):
        days = ["d%d" % i for i in range(10)]
        got = lab.walk_forward_splits(days, 4, 2)
        self.assertEqual(got, [(days[0:4], days[4:6]), (days[2:6], days[6:8]), (days[4:8], days[8:10])])
        self.assertEqual(lab.walk_forward_splits(days, 4, 2, skip=3), [(days[3:7], days[7:9])])
        self.assertEqual(lab.walk_forward_splits(days, 8, 4), [])
        for ins, outs in got:
            self.assertFalse(set(ins) & set(outs))
            self.assertLess(max(ins), min(outs))

    def test_probabilistic_and_deflated_sharpe_by_hand(self):
        self.assertAlmostEqual(lab.probabilistic_sharpe(0.1, 0.0, 100, 0.0, 3.0), 0.8395254, places=6)
        self.assertAlmostEqual(lab.probabilistic_sharpe(0.1, 0.0, 100, -0.5, 6.0), 0.8327976, places=6)
        self.assertAlmostEqual(lab.expected_max_sharpe(10, 0.01), 0.1574598, places=6)
        self.assertAlmostEqual(lab.expected_max_sharpe(1000, 1.0), 3.2551215, places=6)   # the paper's ~3.26
        self.assertEqual(lab.expected_max_sharpe(1, 1.0), 0.0)
        r = [0.01, -0.02, 0.015, 0.0, 0.005, -0.01, 0.02, 0.01]
        d = lab.deflated_sharpe(r, [0.1, 0.3, 0.2])
        self.assertAlmostEqual(d["sharpe"], 0.28204, places=5)
        self.assertAlmostEqual(d["skew"], -0.6396, places=4)
        self.assertAlmostEqual(d["kurtosis"], 2.3137, places=4)
        self.assertEqual(d["trials"], 3)
        star = lab.expected_max_sharpe(3, 0.01)
        self.assertAlmostEqual(d["sharpe_hurdle"], star, places=5)
        self.assertAlmostEqual(d["dsr"], lab.probabilistic_sharpe(0.2820380, star, 8, -0.6395695, 2.3137435), places=4)
        self.assertLess(d["dsr"], d["psr"])                   # more trials, a higher bar
        more = lab.deflated_sharpe(r, [0.1, 0.3, 0.2], extra_trials=50)
        self.assertLess(more["dsr"], d["dsr"])

    def test_pbo_by_hand(self):
        # 4 periods x 3 trials, 4 blocks of one period, mean as the metric. Of the 6 in/out splits,
        # in {0,3} the in-sample winner (trial 0) is the worst out of sample, and in {1,2} trial 2 wins
        # in sample and is the worst out of sample: 2 of 6 overfit.
        m = [[3, 1, 0], [0, 1, 2], [0, 1, 2], [3, 1, 0]]
        got = lab.pbo_cscv(m, blocks=4, metric="mean")
        self.assertEqual((got["splits"], got["trials"]), (6, 3))
        self.assertAlmostEqual(got["pbo"], 2 / 6.0, places=4)
        steady = [[3, 1, 0]] * 8                               # always the same winner: no overfitting
        self.assertEqual(lab.pbo_cscv(steady, 4, "mean")["pbo"], 0.0)
        flip = [[1, 0]] * 4 + [[-1, 0]] * 4                    # the in-sample winner always loses after
        self.assertEqual(lab.pbo_cscv(flip, 2, "mean")["pbo"], 1.0)
        self.assertIsNone(lab.pbo_cscv([[1], [2]], 2))         # one trial: nothing to compare

    def test_monte_carlo_is_seeded(self):
        nets = [100, -50, 30, -80, 120, -40, 10, -20, 60, -10]
        a, b = lab.monte_carlo(nets, 500, 16), lab.monte_carlo(nets, 500, 16)
        self.assertEqual(a, b)
        self.assertNotEqual(a, lab.monte_carlo(nets, 500, 17))
        self.assertEqual(a["drawdown_actual"], lab.max_drawdown(nets))
        self.assertLessEqual(a["drawdown_p50"], a["drawdown_p95"])
        self.assertLessEqual(a["final_p5"], a["final_p50"])
        self.assertLessEqual(a["final_p50"], a["final_p95"])
        self.assertEqual(a["bands"][-1]["trade"], len(nets))
        self.assertIsNone(lab.monte_carlo([], 100, 1))

    def test_regimes_use_the_day_before(self):
        trades = [{"signal_date": "2026-10-29", "net": 100.0, "r": 0.5}]
        out = lab.regime_split(trades, context(), [13, 16, 20])
        vix = series("^INDIAVIX", "1d")
        prev = vix.c[vix.date.index("2026-10-28")]
        want = [lab.regime_labels({"vix": vix}, [13, 16, 20])[0]["2026-10-28"]]
        self.assertEqual([r["regime"] for r in out["vix"]], want, prev)
        self.assertEqual(out["trend"][0]["trades"], 1)


class Gates(unittest.TestCase):
    G = dict(lab.GATE_DEFAULTS)

    def stats(self, **kw):
        s = {"trades": 60, "expectancy_r": 0.2, "profit_factor": 1.5, "net": 5000.0, "max_drawdown_pct": 5.0,
             "max_drawdown": 5000.0}
        s.update(kw)
        return s

    def gates(self, oos=None, full=None, dsr=0.97, pbo=0.2, cost=0.1, near=0.8):
        return lab.evaluate_gates(self.G, oos or self.stats(), full or self.stats(), {"dsr": dsr} if dsr is not None else None,
                                  {"pbo": pbo} if pbo is not None else None,
                                  {"1.5x": {"trades": 60, "expectancy_r": cost}}, near)

    def test_all_pass_then_each_fails(self):
        self.assertTrue(all(g["pass"] for g in self.gates()))
        cases = [dict(oos=self.stats(trades=10)), dict(oos=self.stats(expectancy_r=-0.01)),
                 dict(oos=self.stats(profit_factor=1.1)), dict(full=self.stats(max_drawdown_pct=12)),
                 dict(dsr=0.9), dict(dsr=None), dict(pbo=0.5), dict(pbo=None), dict(cost=0.0), dict(near=0.4)]
        for kw in cases:
            failed = [g["gate"] for g in self.gates(**kw) if not g["pass"]]
            self.assertEqual(len(failed), 1, (kw, failed))

    def test_verdicts(self):
        ok = self.gates()
        good = [{"r": 0.3, "net": 300.0}] * 25
        bad = [{"r": -0.4, "net": -400.0}] * 30
        self.assertEqual(lab.decide(self.gates(pbo=0.7), self.G)[0], "REJECT")
        self.assertEqual(lab.decide(ok, self.G)[0], "WATCH")
        self.assertEqual(lab.decide(ok, self.G, good[:5])[0], "WATCH")
        self.assertEqual(lab.decide(ok, self.G, good, 5000.0)[0], "PROMOTE")
        self.assertEqual(lab.decide(ok, self.G, good + bad, 5000.0, "PROMOTE")[0], "DEMOTE")
        self.assertEqual(lab.decide(ok, self.G, bad, 5000.0, "WATCH")[0], "REJECT")
        self.assertEqual(lab.decide(ok, self.G, good, 5000.0, "DEMOTE")[0], "PROMOTE")   # a clean record again
        deep = good[:20] + [{"r": -1.0, "net": -9000.0}] + good[:9]
        self.assertEqual(lab.decide(ok, self.G, deep, 5000.0, "PROMOTE")[0], "DEMOTE")   # drawdown > 1.5x


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = os.path.join(cls.tmp, "lab")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_run_lab_shape_and_determinism(self):
        doc = spec_doc("orb15-vwap", universe=THREE)
        a = lab.run_lab(doc, data_for(THREE), context())
        b = lab.run_lab(doc, data_for(THREE), context())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        for k in ("full", "walk_forward", "grid", "monte_carlo", "dsr", "pbo", "regimes", "costs_sensitivity", "gates"):
            self.assertIn(k, a)
        self.assertEqual(len(a["grid"]["points"]), 6)
        self.assertEqual(len(a["walk_forward"]["folds"]), 3)
        self.assertEqual(a["dsr"]["trials"], 6)
        self.assertEqual(set(a["costs_sensitivity"]["out_of_sample"]), {"1x", "1.5x", "2x"})
        oos = a["walk_forward"]["stats"]
        self.assertGreater(oos["trades"], 0)
        self.assertLessEqual(oos["trades"], a["full"]["trades"])
        x15 = a["costs_sensitivity"]["out_of_sample"]["1.5x"]["expectancy_r"]
        self.assertLess(x15, oos["expectancy_r"])
        text = lab.scorecard(dict(a, verdict="REJECT", verdict_why="test"))
        self.assertIn("What this does NOT prove", text)
        self.assertIn("not financial advice", text)
        self.assertNotIn("guarantee", text.replace("not a guarantee", ""))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lab.main(["--out", self.out] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_cli_run_report_compare_list(self):
        for name in ("rsi2-nifty50", "breakout20-swing"):
            code, out, err = self.run_cli("run", name, "--data", DIR, "--now", "2026-11-01 12:00")
            self.assertEqual(code, 0, err)
            self.assertIn("Verdict:", out)
            d = os.path.join(self.out, name)
            for f in ("spec.json", "results.json", "scorecard.md", "equity.csv", "status.json"):
                self.assertTrue(os.path.exists(os.path.join(d, f)), f)
            with open(os.path.join(d, "equity.csv"), encoding="utf-8") as fh:
                self.assertEqual(fh.readline().strip(), "date,pnl,equity,drawdown_pct,trades,out_of_sample")
        code, out, _ = self.run_cli("report", "rsi2-nifty50")
        self.assertEqual(code, 0)
        self.assertIn("What this does NOT prove", out)
        code, out, _ = self.run_cli("compare", "rsi2-nifty50", "breakout20-swing")
        self.assertIn("breakout20-swing", out)
        code, out, _ = self.run_cli("list")
        self.assertIn("orb15-vwap", out)
        self.assertIn("rsi2-nifty50", out)
        code, out, err = self.run_cli("report", "nothing-here")
        self.assertEqual(code, 1)
        self.assertIn("run it first", err)

    def test_changed_spec_counts_as_another_trial(self):
        out = os.path.join(self.tmp, "trials")
        doc = spec_doc("breakout20-swing", universe=["ITC", "LT"])
        data = data_for(["ITC", "LT"], "1d")
        r1 = lab.run_and_decide(doc, data, {}, out, now=NOW)
        lab.save_run(out, doc, r1, NOW)
        doc2 = dict(doc, exit={"when": doc["exit"]["when"], "max_bars": 25})      # a variant: same grid
        r2 = lab.run_and_decide(doc2, data, {}, out, now=NOW)
        self.assertEqual(r2["earlier_versions"], 1)
        self.assertEqual(r2["dsr"]["trials"], r1["dsr"]["trials"] + 1)

    def test_forward_drafts_plans_only_for_a_watched_strategy(self):
        out = os.path.join(self.tmp, "fwd")
        doc = spec_doc("orb15-vwap", universe=THREE, gates=LENIENT)
        data = data_for(THREE)
        cut = "2026-10-16"

        def upto(d, last_date, last_minute=None):
            res = {}
            for sym, s in d.items():
                bars = [b for b in s.bars if b["date"] < last_date or (
                    b["date"] == last_date and (last_minute is None or lab.minutes_of(b["time"]) <= last_minute))]
                res[sym] = lab.Series(sym, "5m", bars)
            return res

        res = lab.run_and_decide(doc, upto(data, cut), context(), out, now=NOW)
        self.assertEqual(res["verdict"], "WATCH", res["verdict_why"])
        lab.save_run(out, doc, res, NOW)
        # a real signal after the backtest: cut the data right after its signal bar
        later = [t for t in lab.backtest(lab.validate_spec(doc), data)["trades"] if t["signal_date"] > "2026-10-20"]
        self.assertTrue(later)
        t = later[-1]
        fill_m = lab.minutes_of(t["entry_time"])
        now = lab.ist_now("%s %s" % (t["entry_date"], t["entry_time"]))
        r = lab.forward(doc, upto(data, t["entry_date"], fill_m - 5), out, now=now)
        self.assertEqual(r["verdict"], "WATCH", r["why"])
        self.assertGreater(r["settled"], 0)
        with open(os.path.join(out, "orb15-vwap", "forward.json"), encoding="utf-8") as fh:
            fwd = json.load(fh)
        self.assertTrue(all(x["signal_date"] > cut for x in fwd["trades"]))
        if True:
            plans = [p for p in r["plans"] if p["symbol"] == t["symbol"]]
            self.assertEqual(len(plans), 1)
            plan = plans[0]
            self.assertEqual(plan["setup"], "lab:orb15-vwap")
            self.assertEqual(plan["date"], t["entry_date"])
            self.assertTrue(os.path.exists(r["written"][r["plans"].index(plan)]))
            budget = load("trader-budget", "size")                 # the plan feeds trader-budget unchanged
            sized = budget.size_trade(plan, lab.validate_spec(doc)["cfg"], today=plan["date"])
            self.assertEqual(sized["symbol"], t["symbol"])
            self.assertGreater(sized["qty"], 0)
        # running forward twice settles nothing new
        again = lab.forward(doc, upto(data, t["entry_date"], fill_m - 5), out, now=now)
        self.assertEqual(again["settled"], 0)

    def test_forward_refuses_a_rejected_or_unrun_strategy(self):
        out = os.path.join(self.tmp, "rej")
        doc = spec_doc("orb15-vwap", universe=THREE)
        with self.assertRaises(lab.LabError):
            lab.forward(doc, data_for(THREE), out, now=NOW)
        res = lab.run_and_decide(doc, data_for(THREE), context(), out, now=NOW)
        lab.save_run(out, doc, res, NOW)
        self.assertEqual(res["verdict"], "REJECT")
        r = lab.forward(doc, data_for(THREE), out, now=NOW)
        self.assertEqual(r["plans"], [])
        self.assertTrue(any("no paper plans" in n for n in r["notes"]))

    def test_options_hook(self):
        doc = {"name": "short-strangle", "kind": "options"}
        with self.assertRaises(lab.SpecError) as cm:
            lab.run_lab(doc, {})
        self.assertIn("underlying", str(cm.exception))
        try:
            lab.OPTIONS_HOOK = lambda d, **kw: {"name": d["name"], "hooked": True}
            self.assertTrue(lab.run_lab(doc, {})["hooked"])
        finally:
            lab.OPTIONS_HOOK = None


class FromTheArchive(unittest.TestCase):
    """The lab reads the local trader-archive first when there is one, and falls back when not."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bt-lab-archive-")
        self.addCleanup(shutil.rmtree, self.root, True)
        archive_with(self.root, reliance_1m=True)

    def test_daily_bars_are_adjusted_bhavcopy_bars(self):
        bars, src = lab.load_bars("JSWDULUX", "1d", fetch=False, archive=self.root)
        self.assertEqual(src, "archive 2026-10-21 to 2026-10-23")
        self.assertEqual([b["c"] for b in bars], [299.48, 302.2, 286.6])      # the 10:1 split, adjusted
        self.assertEqual(bars[0]["time"], "")

    def test_regime_series_and_intraday(self):
        nifty, src = lab.load_bars("^NSEI", "1d", fetch=False, archive=self.root)
        self.assertTrue(src.startswith("archive") and len(nifty) == 3 and nifty[0]["c"] > 20000)
        vix, _ = lab.load_bars("^INDIAVIX", "1d", fetch=False, archive=self.root)
        self.assertTrue(10 < vix[-1]["c"] < 40)
        five, src = lab.load_bars("RELIANCE", "5m", fetch=False, archive=self.root)
        self.assertEqual((len(five), five[0]["time"], five[-1]["time"]), (150, "09:15", "15:25"))
        s = lab.Series("RELIANCE", "5m", five)
        self.assertEqual(len(s.c), 150)

    def test_falls_back_without_an_archive(self):
        with self.assertRaises(lab.LabError) as cm:
            lab.load_bars("TCS", "5m", fetch=False, archive=self.root)        # no TCS intraday archived
        self.assertIn("no cached bars", str(cm.exception))
        missing = os.path.join(self.root, "nowhere")
        self.assertIsNone(lab.archive_bars("RELIANCE", "1d", missing))
        bars, src = lab.load_bars("RELIANCE", "1d", DIR, archive=self.root)    # --data still wins
        self.assertEqual(src, "RELIANCE-1d.csv.gz")

    def test_load_data_notes_the_archive(self):
        spec = lab.validate_spec(lab.library()["orb15-vwap"])
        spec = dict(spec, universe=["RELIANCE"])
        data, ctx, notes = lab.load_data(spec, None, None, False, None, self.root)
        self.assertEqual(len(data["RELIANCE"].c), 150)
        self.assertIsNotNone(ctx["nifty"])
        self.assertTrue(any("local archive" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
