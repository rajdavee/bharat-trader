"""trader-lab options edge v2 (#73): realised vol, HAR-RV, IV - RV, event studies and the engines, offline.

Fixtures:
  tests/fixtures/trader-lab/^NSEI-1d.csv.gz   NIFTY 50 daily OHLC, 2024-10-30 to 2026-10-30 (real, Yahoo).
  tests/fixtures/trader-lab/edge/             a trader-archive folder cut from NSE's real files, fetched
      2026-11-01 by archive.py backfill and trimmed: UDiFF F&O bhavcopies 2026-09-30 to 2026-10-30 with only
      NIFTY (monthly expiries within 60 days, strikes within 10% on 100s; weeklies within 14 days, strikes
      within 1.5%) and HDFCBANK and ICICIBANK (monthly, strikes within 10%); index closes (Nifty 50, Nifty
      Bank, India VIX) from 2026-09-01; India VIX history; equity rows of the two stocks around their April,
      July and October 2026 results; the results dataset (board meetings and filings, April to October 2026)
      cut to those two stocks; and NIFTY 5-minute bars of 2026-10-29 (the real Yahoo chart response kept
      under tests/fixtures/trader-alerts). HDFCBANK and ICICIBANK both met on Saturday 17 Oct 2026, so the
      first reacting session is Monday 19 Oct.
"""

import copy
import csv
import gzip
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

lab = load("trader-lab", "lab")
O = lab.options_api()
V = O.vol_api()
E = O.events_api()
EN = O.engines_api()
A = lab.archive_api()
R = lab.regime_api()
ARC = os.path.join(FIX, "trader-lab", "edge")
LIB = os.path.join(ROOT, "trader-lab", "library", "edge-hunt")
FIXWIN = {"name": "validation", "from": "2026-09-30", "to": "2026-10-30"}


def nifty_bars():
    with gzip.open(os.path.join(FIX, "trader-lab", "^NSEI-1d.csv.gz"), "rt") as fh:
        return [{"date": r["date"], "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                 "c": float(r["close"])} for r in csv.DictReader(fh)]


def hyp(prefix):
    name = [f for f in os.listdir(LIB) if f.startswith(prefix)][0]
    with open(os.path.join(LIB, name), encoding="utf-8") as fh:
        return json.load(fh)


def data(**kw):
    d = {"root": ARC, "archive": A, "regime": R}
    d.update(kw)
    return d


BARS = nifty_bars()


class RealisedVol(unittest.TestCase):
    def test_close_to_close_by_hand(self):
        b = BARS[:6]
        r = [math.log(b[i]["c"] / b[i - 1]["c"]) for i in range(1, 6)]
        m = sum(r) / 5
        want = math.sqrt(sum((x - m) ** 2 for x in r) / 4) * math.sqrt(252) * 100
        got = V.realised_vol(b, 5, "close")
        self.assertEqual(list(got), [b[5]["date"]])
        self.assertAlmostEqual(got[b[5]["date"]], want, places=9)
        # the same number as trader-regime's realised_vol
        self.assertAlmostEqual(R.realised_vol([x["c"] for x in b], 5)[5], want, places=9)

    def test_parkinson_and_garman_klass_by_hand(self):
        b = BARS[:5]
        pk = sum(math.log(x["h"] / x["l"]) ** 2 for x in b) / (4 * math.log(2)) / 5
        gk = sum(0.5 * math.log(x["h"] / x["l"]) ** 2 - (2 * math.log(2) - 1) * math.log(x["c"] / x["o"]) ** 2
                 for x in b) / 5
        self.assertAlmostEqual(V.realised_vol(b, 5, "parkinson")[b[4]["date"]], math.sqrt(pk * 252) * 100, places=9)
        self.assertAlmostEqual(V.realised_vol(b, 5, "garman-klass")[b[4]["date"]], math.sqrt(gk * 252) * 100,
                               places=9)
        # 2024-10-30: high 24498.20, low 24307.30 -> ln(H/L)^2 / (4 ln 2)
        d = V.daily_variance(b[:1], "parkinson")[0]
        self.assertAlmostEqual(d[1], math.log(24498.2 / 24307.3) ** 2 / (4 * math.log(2)), places=12)

    def test_flat_bar_falls_back_to_close(self):
        b = [dict(BARS[0]), dict(BARS[1], o=BARS[1]["c"], h=BARS[1]["c"], l=BARS[1]["c"])]
        d = V.daily_variance(b, "garman-klass")
        self.assertEqual(d[1][2], "no range: close-to-close")
        self.assertAlmostEqual(d[1][1], math.log(b[1]["c"] / b[0]["c"]) ** 2, places=12)
        with self.assertRaises(V.VolError):
            V.daily_variance(b, "yang-zhang")


class Har(unittest.TestCase):
    def test_fit_is_least_squares_on_the_train_window_only(self):
        tiny = BARS[:130]
        m = V.har_fit(tiny, tiny[0]["date"], tiny[-1]["date"], h=5)
        rows = [(x, y) for i, x, y in V.har_rows([v for _, v, _ in V.daily_variance(tiny)], 5) if y is not None]
        self.assertEqual(m["n"], len(rows))
        resid = [y - sum(c * v for c, v in zip(m["coef"], x)) for x, y in rows]
        for j in range(4):      # the normal equations: X'e = 0
            self.assertAlmostEqual(sum(x[j] * e for (x, _), e in zip(rows, resid)) / len(rows), 0.0, places=12)
        self.assertTrue(0 <= m["r2"] <= 1)
        # bars after the train window change nothing, even absurd ones
        spoiled = tiny + [dict(b, c=b["c"] * 3, h=b["h"] * 3) for b in BARS[130:160]]
        m2 = V.har_fit(spoiled, tiny[0]["date"], tiny[-1]["date"], h=5)
        self.assertEqual(m["coef"], m2["coef"])
        # the last target ends inside the window
        self.assertLessEqual(m["fitted_through"], tiny[-1]["date"])

    def test_too_short_is_refused(self):
        with self.assertRaises(V.VolError):
            V.har_fit(BARS[:30], BARS[0]["date"], BARS[29]["date"], h=5)

    def test_forecast_and_iv_minus_rv_have_no_look_ahead(self):
        model = V.har_fit(BARS, "2024-11-01", "2025-12-31", h=20)
        vix = A.load_vix(None, None, ARC)
        iv = V.vix_iv(vix)
        full = V.vrp_table(BARS, iv, model)
        day = "2026-10-15"
        cut = [b for b in BARS if b["date"] <= day]
        later = [dict(b, c=b["c"] * 0.7, h=b["h"] * 0.7, l=b["l"] * 0.6, o=b["o"] * 0.7) for b in BARS if b["date"] > day]
        for bars in (cut, cut + later):
            part = V.vrp_table(bars, iv, model)
            for d in [k for k in full if k <= day]:
                self.assertEqual(full[d], part[d], d)
        row = full[day]
        self.assertAlmostEqual(row["spread_rv"], row["iv"] - row["rv"], places=9)
        self.assertAlmostEqual(row["spread_har"], row["iv"] - row["rv_forecast"], places=9)
        self.assertEqual(row["iv"], iv[day])
        self.assertIsNone(V.forward_realised(BARS, BARS[-1]["date"], 5))


class EventStudy(unittest.TestCase):
    def setUp(self):
        self.sessions = A.trading_days(root=ARC)
        self.evs = A.results_dates("HDFCBANK", None, None, root=ARC)

    def test_reacting_session_and_a_bad_archive_session(self):
        ev = [e for e in self.evs if e["date"] == "2026-10-17"][0]
        w = E.event_windows(ev, self.sessions)
        self.assertEqual((w["event_session"], w["entry"], w["move_end"], w["time_known"]),
                         ("2026-10-19", "2026-10-16", "2026-10-19", True))
        # an event before the archive's first session is dropped, not moved to that session
        self.assertEqual(E.reacting_session({"date": "2026-03-10"}, self.sessions)[0], None)
        # an after-close filing reacts the next session
        s, known = E.reacting_session({"date": "2026-10-15", "after_close": True}, self.sessions)
        self.assertEqual((s, known), ("2026-10-16", True))
        # a board meeting on a session with no time: measured to the next session's close, flagged
        w2 = E.event_windows({"date": "2026-10-15", "after_close": None}, self.sessions)
        self.assertEqual((w2["entry"], w2["move_end"], w2["time_known"]), ("2026-10-14", "2026-10-16", False))

    def test_implied_vs_realised_on_hdfcbank_results(self):
        days = [d for d in self.sessions if "2026-10-14" <= d <= "2026-10-21"]
        boards = {d: O.trim_board(A.load_fo_chain_eod("HDFCBANK", d, root=ARC), band=0.3) for d in days}
        ch = O.Chains("HDFCBANK", boards)
        im = E.implied_move(ch, "2026-10-16", "2026-10-19", 0.0557)
        # raw bhavcopy, 16 Oct 2026: 730 CE settle 11.05, 730 PE settle 11.95, underlying 728.84; expiry 27 Oct
        self.assertEqual((im["strike"], im["expiry"], im["sources"]), (730.0, "2026-10-27", ["settle", "settle"]))
        self.assertAlmostEqual(im["move_pct"], 100 * (11.05 + 11.95) / 728.84, places=9)
        closes = {b["date"]: b["c"] for b in A.load_daily("HDFCBANK", None, None, adjusted=False, root=ARC)}
        # equity closes: 728.85 on 16 Oct, 712.00 on 19 Oct
        self.assertAlmostEqual(E.realised_move(closes, "2026-10-16", "2026-10-19"), 100 * abs(712.0 / 728.85 - 1),
                               places=9)
        rows = E.study("HDFCBANK", self.evs, ch, closes, self.sessions, 0.0557, start="2026-10-01")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertFalse(r["realised_above_implied"])         # 2.31% realised against 3.16% implied
        self.assertEqual(r["history_n"], 2)                     # April and July 2026, before the entry
        hist = E.history_moves(self.evs, closes, self.sessions, "2026-10-16")
        self.assertAlmostEqual(r["history_mean_pct"], round(sum(hist) / 2, 4))
        s = E.summary(rows)
        self.assertEqual((s["with_both"], s["realised_above_implied"]), (1, 0))

    def test_calendar(self):
        cal = E.calendar("2026-10-01", "2026-10-31", R.builtin_events(), ["2026-10-27"],
                         [{"date": "2026-10-17", "event_session": "2026-10-19"}])
        self.assertEqual(cal["2026-10-07"], ["rbi_policy"])
        self.assertEqual(cal["2026-10-27"], ["expiry"])
        self.assertEqual(cal["2026-10-19"], ["results"])
        self.assertEqual(E.blocked("2026-10-06", cal), [])
        self.assertEqual(E.blocked("2026-10-06", cal, sessions=A.trading_days(root=ARC), ahead=1), ["rbi_policy"])
        self.assertEqual(E.blocked("2026-10-27", cal, kinds=("rbi_policy",)), [])


class Charges(unittest.TestCase):
    def test_stt_on_exercise_of_a_stock_put(self):
        # HDFCBANK 700 PE (27 Oct 2026, lot 650) bought at 2.80 on 16 Oct; the October future settled at
        # 680.20 on expiry day, so the put is exercised for 19.80 a share
        ch = O.Chains("HDFCBANK", {d: O.trim_board(A.load_fo_chain_eod("HDFCBANK", d, root=ARC))
                                   for d in ("2026-10-16", "2026-10-27")})
        fsp, how = ch.final_settlement("2026-10-27")
        self.assertEqual((fsp, how), (680.2, "the expiring future's final settlement price"))
        intrinsic = round(700 - fsp, 2)
        c = lab.fno_round_trip_charges(0, intrinsic, 650, None, "OPT", "2026-10-27", exercised=True)
        self.assertEqual(c["stt"], 19.31)              # 19.80 x 650 x 0.15% = 19.305, half up
        self.assertEqual((c["exchange"], c["brokerage"], c["stamp"]), (0.0, 0.0, 0.0))
        leg = {"side": "BUY", "qty": 650, "fill": 2.8}
        opening = lab.fno_round_trip_charges(2.8, 0, 650, None, "OPT", "2026-10-16")["total"]
        self.assertAlmostEqual(O.leg_charges({}, leg, "2026-10-16", "2026-10-27", intrinsic, True),
                               opening + c["total"], places=6)
        # closing it by a sale instead pays STT on the sale at the sell rate
        sold = lab.fno_round_trip_charges(0, intrinsic, 650, None, "OPT", "2026-10-27")
        self.assertEqual(sold["stt"], 19.31)
        self.assertGreater(sold["exchange"], 0)

    def test_size_lots(self):
        self.assertEqual(EN.size_lots(1000000, 100000, 9000), 1)          # 1% tail: 10,000 / 9,000
        self.assertEqual(EN.size_lots(1000000, 100000, 20000), 0)         # one lot breaks the 1% rule
        self.assertEqual(EN.size_lots(10000000, 1000000, 10000), 5)       # margin: 50% of 1 crore / 10 lakh
        self.assertEqual(EN.size_lots(1000000, 0, 1000), 0)


class Contract(unittest.TestCase):
    def test_families_grid_and_params(self):
        for fam in EN.FAMILIES:
            self.assertTrue(callable(O.engine(fam)))
        with self.assertRaises(EN.EngineError):
            EN.engine("momentum-xs")
        for pre, fam in (("eh-05", "vrp-selling"), ("eh-08", "event-vol"), ("eh-09", "event-vol"),
                         ("eh-14", "option-buying")):
            h = hyp(pre)
            self.assertEqual(h["family"], fam)
            g = EN.grid(h)
            self.assertEqual(len(g), h["trial_budget"])
            self.assertEqual(len({json.dumps(x, sort_keys=True) for x in g}), len(g))
        h = hyp("eh-05")
        with self.assertRaises(EN.EngineError):
            EN.check_params(h, dict(EN.grid(h)[0], vrp_threshold_vol_points=3))
        with self.assertRaises(EN.EngineError):
            EN.check_params(h, {"short_delta": 0.1})
        self.assertEqual(EN.window_of(h, "holdout"), {"name": "holdout", "from": "2024-12-02", "to": "2026-10-30"})

    def test_result_shape_and_the_registration_is_untouched(self):
        h = hyp("eh-05")
        before = copy.deepcopy(h)
        r = EN.run(h, EN.grid(h)[0], FIXWIN, data())
        self.assertEqual(h, before)
        for k in ("family", "hypothesis", "engine", "params", "window", "status", "trades", "n", "metrics",
                  "primary_metric", "primary", "skipped", "notes", "flags", "registration", "baseline"):
            self.assertIn(k, r)
        self.assertEqual((r["family"], r["hypothesis"], r["primary_metric"]),
                         ("vrp-selling", "eh-05-vrp-condor", "oos_return_on_margin"))
        self.assertEqual(r["primary"], r["metrics"]["return_on_margin_pct"])
        for t in r["trades"]:
            for k in ("net", "r", "margin", "return_on_margin_pct", "entry_date", "exit_date", "charges"):
                self.assertIn(k, t)


class Engines(unittest.TestCase):
    def test_vrp_condor_on_the_fixture(self):
        h = hyp("eh-05")
        p = {"short_delta": 0.1, "wing_width_pct_of_spot": 1.5, "vrp_threshold_vol_points": 0, "exit": "expiry"}
        r = EN.run(h, p, FIXWIN, data())
        self.assertEqual(r["status"], "ok")
        t = r["trades"]
        self.assertEqual([(x["entry_date"], x["exit_date"], x["reason"]) for x in t],
                         [("2026-09-30", "2026-10-27", "expiry settlement"), ("2026-10-28", "2026-10-30", "window end")])
        f = t[0]["facts"]
        # India VIX 13.49 at the 30 Sep close against NIFTY's 20-session realised vol to that close
        self.assertEqual(f["vix"], 13.49)
        rv = V.realised_vol(A.load_index("NIFTY", None, None, ARC), 20)["2026-09-30"]
        self.assertAlmostEqual(f["spread"], round(13.49 - rv, 3), places=3)
        self.assertEqual(f["wing_points"], 300.0)              # 1.5% of about 23,000 on 100-point strikes
        self.assertEqual(f["events_in_hold"], ["rbi_policy 2026-10-07"])
        self.assertEqual(f["sized_lots"], 0)                   # 10 lakh cannot carry one lot within 1%
        self.assertEqual(r["baseline"]["n"], 2)
        self.assertIn("beats_untimed", r["metrics"])
        self.assertIn("worst_loss_avg_credits", r["metrics"])
        # a 4-point threshold keeps only the cycles with a wide spread
        hi = EN.run(h, dict(p, vrp_threshold_vol_points=4), FIXWIN, data())
        self.assertLess(hi["n"], r["n"])
        self.assertGreater(sum(hi["skipped"].values()), 0)
        # cost stress costs money
        st = EN.run(h, p, FIXWIN, data(), cost_mult=1.5)
        self.assertLess(st["metrics"]["net"], r["metrics"]["net"])

    def test_event_crush_and_runup(self):
        d = data(symbols=["HDFCBANK", "ICICIBANK"], min_history=2)
        h8 = hyp("eh-08")
        r = EN.run(h8, {"wing_pct_of_spot": 5, "exit_session": 1, "min_event_iv_ratio": 1.0}, FIXWIN, d)
        self.assertEqual(r["engine"], "event-crush-iron-fly")
        self.assertEqual(sorted(t["symbol"] for t in r["trades"]), ["HDFCBANK", "ICICIBANK"])
        for t in r["trades"]:
            self.assertEqual((t["entry_date"], t["exit_date"], t["structure"]), ("2026-10-16", "2026-10-19", "iron-fly"))
            self.assertTrue(t["credit"])
            self.assertEqual(len(t["legs"]), 4)
        self.assertEqual(r["metrics"]["event_study"]["with_both"], 2)
        # the registered MIN_HISTORY (4 earlier events) is more than this fixture holds
        r4 = EN.run(h8, {"wing_pct_of_spot": 5, "exit_session": 1, "min_event_iv_ratio": 1.0}, FIXWIN,
                    data(symbols=["HDFCBANK"]))
        self.assertEqual(r4["n"], 0)
        self.assertEqual(r4["skipped"], {"fewer than 4 earlier results moves": 1})
        h9 = hyp("eh-09")
        r9 = EN.run(h9, {"entry_sessions_before": 3, "iv_percentile_cap": 100}, FIXWIN, d)
        self.assertEqual(r9["primary_metric"], "oos_mean_event_return")
        self.assertEqual(sorted((t["symbol"], t["entry_date"], t["exit_date"]) for t in r9["trades"]),
                         [("HDFCBANK", "2026-10-14", "2026-10-16"), ("ICICIBANK", "2026-10-14", "2026-10-16")])
        for t in r9["trades"]:
            self.assertFalse(t["credit"])
            a = t["attribution"]
            self.assertAlmostEqual(a["iv_change"] + a["move_and_time"], t["gross"], places=1)
        self.assertAlmostEqual(r9["primary"], sum(100 * t["net"] / abs(t["premium"]) for t in r9["trades"]) / 2,
                               places=2)
        capped = EN.run(h9, {"entry_sessions_before": 3, "iv_percentile_cap": 50}, FIXWIN, d)
        self.assertEqual(capped["skipped"], {"under 126 sessions of ATM IV history": 2})

    def test_trend_day_buying_model_priced_and_holdout_waits(self):
        h = hyp("eh-14")
        win = {"name": "validation", "from": "2026-10-29", "to": "2026-10-29"}
        p = {"opening_range_minutes": 30, "min_breakout_pct": 0.2, "vix_cap": 18, "exit": "1515"}
        r = EN.run(h, p, win, data())
        self.assertTrue(r["flags"]["model_priced"])
        self.assertEqual(r["flags"]["verdict_cap"], "WATCH")
        self.assertEqual(r["n"], 1)
        t = r["trades"][0]
        self.assertEqual((t["option_type"], t["exit_time"], t["reason"], t["expiry"]), ("CE", "15:15", "15:15", "2026-11-03"))
        self.assertEqual(t["iv_source"], "the previous session's EOD ATM IV")
        # the entry is Black-76 at the signal bar's close plus slippage
        bars = A.load_intraday("NIFTY", "5m", "2026-10-29", "2026-10-29", ARC)
        bar = [b for b in bars if b["time"] == "10:20"][0]
        px = EN.model_price(bar["c"], t["strike"], "2026-10-29", 10 * 60 + 25, "2026-11-03", t["iv"], 0.0557, "CE")
        self.assertAlmostEqual(t["entry"], px + O.slip(px, O.OPTION_COSTS), delta=0.25)    # IV kept to 2 places
        self.assertAlmostEqual(t["r"], t["net"] / (t["entry"] * t["lot_size"]), places=3)
        # a VIX cap under the previous close (16.25 on 28 Oct) blocks the day
        low = EN.run(h, dict(p, vix_cap=14), win, data())
        self.assertEqual((low["n"], low["skipped"]), (0, {"India VIX at or over the cap": 1}))
        held = EN.run(h, p, "holdout", data())
        self.assertEqual(held["status"], "waiting for captured chains")
        self.assertEqual(held["primary"], None)

    def test_snapshot_quote_reads_a_captured_chain(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            d = os.path.join(tmp, "chain", "NIFTY", "2026-10-30")
            os.makedirs(d)
            with open(os.path.join(FIX, "trader-archive", "oc-v3-NIFTY.trimmed.json"), encoding="utf-8") as fh:
                doc = {"symbol": "NIFTY", "expiry": "2026-11-03", "data": json.load(fh)}
            with gzip.open(os.path.join(d, "1500_2026-11-03.json.gz"), "wt", encoding="utf-8") as fh:
                json.dump(doc, fh)
            # 22800 CE: bid 266.35, ask 266.95 in NSE's chain
            self.assertEqual(EN.snapshot_quote(A, tmp, "NIFTY", "2026-10-30", 15 * 60 + 5, "2026-11-03", 22800,
                                               "CE", "buy"), (266.95, "15:00"))
            self.assertEqual(EN.snapshot_quote(A, tmp, "NIFTY", "2026-10-30", 15 * 60 + 5, "2026-11-03", 22800,
                                               "CE", "sell"), (266.35, "15:00"))
            self.assertEqual(EN.snapshot_quote(A, tmp, "NIFTY", "2026-10-30", 15 * 60 + 20, "2026-11-03", 22800,
                                               "CE", "sell")[0], None)          # stale: 20 minutes old
            self.assertEqual(EN.snapshot_quote(A, tmp, "NIFTY", "2026-10-30", 14 * 60, "2026-11-03", 22800,
                                               "CE", "sell"), (None, "no captured chain"))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
