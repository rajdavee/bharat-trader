"""trader-options tests, offline.

Fixtures (tests/fixtures/trader-options/), real NSE responses fetched 2026-11-01
(the chains are the close of Fri 30-Oct-2026, timestamp 15:40):
  contract-info-NIFTY.json, contract-info-RELIANCE.json   the exchange's expiry lists
  chain-NIFTY-03-Nov-2026.json.gz      option-chain-v3, NIFTY weekly, all 104 strikes
  chain-NIFTY-01-Dec-2026.json.gz      option-chain-v3, NIFTY 01-Dec, all 78 strikes
  chain-RELIANCE-23-Nov-2026.json.gz   option-chain-v3, RELIANCE monthly, all 41 strikes
  chain-RELIANCE-25-Nov-2026.json      a wrong expiry: HTTP 200, no rows, spot 0
  fo_mktlots.csv                       lot sizes, index block + 8 stocks
  vixhistory-*.json                    India VIX closes, Nov-2025 to Oct-2026 (5 chunks)
  allIndices.json                      NIFTY 50, NIFTY BANK and INDIA VIX rows only
"""

import gzip
import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

op = load("trader-options", "options")
DIR = os.path.join(FIX, "trader-options")
NOW = "2026-11-01 10:00"           # a Sunday: the chain is from the last session


def fixture(name):
    path = os.path.join(DIR, name)
    if name.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    with open(path, encoding="utf-8") as fh:
        return fh.read() if name.endswith(".csv") else json.load(fh)


def lots():
    return op.parse_lots(fixture("fo_mktlots.csv"))


def analysis(sym="NIFTY", expiry="03-Nov-2026", r=0.0557):
    chain = op.parse_chain(fixture("chain-%s-%s.json.gz" % (sym, expiry)), sym)
    return op.analyse(chain, op.lot_size(lots(), sym, chain["expiry"]), r, now=op.parse_nse_time(NOW))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = op.main(args + ["--offline", DIR, "--now", NOW])
    return code, out.getvalue(), err.getvalue()


def row(a, strike):
    return next(x for x in a["rows"] if x["strike"] == strike)


class BlackSeventySix(unittest.TestCase):
    def test_textbook_value(self):
        # Haug, The Complete Guide to Option Pricing Formulas: F=19, K=19, T=0.75, r=10%, vol 28% -> 1.7011
        self.assertAlmostEqual(op.black76_price(19, 19, 0.75, 0.10, 0.28, "CE"), 1.7011, places=4)
        self.assertAlmostEqual(op.black76_price(19, 19, 0.75, 0.10, 0.28, "PE"), 1.7011, places=4)

    def test_put_call_parity(self):
        for K in (21000, 23000, 25000):
            c = op.black76_price(23020, K, 0.011, 0.0557, 0.13, "CE")
            p = op.black76_price(23020, K, 0.011, 0.0557, 0.13, "PE")
            self.assertAlmostEqual(c - p, math.exp(-0.0557 * 0.011) * (23020 - K), places=6)

    def test_iv_round_trip_across_strikes_and_tenors(self):
        for T in (1.0 / 365, 4.0 / 365, 30.0 / 365, 1.0):
            for K in (18000, 21000, 23000, 25000, 30000):
                for sigma in (0.05, 0.12, 0.4, 1.5):
                    for kind in ("CE", "PE"):
                        px = op.black76_price(23000, K, T, 0.0557, sigma, kind)
                        df = math.exp(-0.0557 * T)
                        intrinsic = df * max(0.0, (23000 - K) if kind == "CE" else (K - 23000))
                        if px - intrinsic < 1e-6:
                            continue           # no time value left to invert
                        iv = op.black76_iv(px, 23000, K, T, 0.0557, kind)
                        self.assertIsNotNone(iv, (T, K, sigma, kind))
                        self.assertAlmostEqual(iv, sigma, places=4, msg=(T, K, sigma, kind))

    def test_iv_is_none_for_untradeable_prices(self):
        F, K, T, r = 23000, 22000, 4.0 / 365, 0.0557
        intrinsic = math.exp(-r * T) * 1000
        self.assertIsNone(op.black76_iv(intrinsic - 1, F, K, T, r, "CE"))   # below intrinsic
        self.assertIsNone(op.black76_iv(intrinsic, F, K, T, r, "CE"))
        self.assertIsNone(op.black76_iv(0, F, K, T, r, "CE"))
        self.assertIsNone(op.black76_iv(None, F, K, T, r, "CE"))
        self.assertIsNone(op.black76_iv(F, F, K, T, r, "CE"))               # at the ceiling
        self.assertIsNone(op.black76_iv(50, F, 23000, 0.5 / (365 * 24), r, "CE"))  # 30 min to expiry
        self.assertIsNone(op.black76_iv(9000, F, 23000, 4.0 / 365, r, "CE"))   # beyond 500% vol

    def test_greeks_match_finite_differences(self):
        F, K, T, r, s = 23020.0, 23100.0, 4.0 / 365, 0.0557, 0.12
        for kind in ("CE", "PE"):
            g = op.black76_greeks(F, K, T, r, s, kind)
            px = lambda f=F, t=T, v=s: op.black76_price(f, K, t, r, v, kind)  # noqa: E731
            h = 0.5
            self.assertAlmostEqual(g["delta"], (px(F + h) - px(F - h)) / (2 * h), places=4)
            self.assertAlmostEqual(g["gamma"], (px(F + h) - 2 * px() + px(F - h)) / (h * h), places=5)
            self.assertAlmostEqual(g["vega"], px(v=s + 0.005) - px(v=s - 0.005), delta=0.001)
            day = 1.0 / 365
            self.assertAlmostEqual(g["theta"], px(t=T - day) - px(), delta=abs(g["theta"]) * 0.15)
            self.assertLess(g["theta"], 0)
        ce, pe = op.black76_greeks(F, K, T, r, s, "CE"), op.black76_greeks(F, K, T, r, s, "PE")
        self.assertAlmostEqual(ce["delta"] - pe["delta"], math.exp(-r * T), places=8)
        self.assertAlmostEqual(ce["gamma"], pe["gamma"], places=12)

    def test_years_to_expiry_counts_minutes_to_1530(self):
        asof = op.parse_nse_time("30-Oct-2026 15:40:00")
        self.assertAlmostEqual(op.years_to_expiry("03-Nov-2026", asof), 5750 / 525600.0, places=12)
        self.assertEqual(op.years_to_expiry("30-Oct-2026", asof), 0.0)


class Parsing(unittest.TestCase):
    def test_contract_info(self):
        info = op.parse_contract_info(fixture("contract-info-NIFTY.json"))
        self.assertEqual(info["expiries"][:4], ["2026-11-03", "2026-11-09", "2026-11-17", "2026-11-23"])
        self.assertEqual(len(info["expiries"]), 18)
        self.assertIn(23000.0, info["strikes"])
        rel = op.parse_contract_info(fixture("contract-info-RELIANCE.json"))
        self.assertEqual(rel["expiries"], ["2026-11-23", "2026-12-29", "2027-01-25"])

    def test_lot_sizes_per_expiry_month(self):
        lt = lots()
        self.assertEqual(op.lot_size(lt, "NIFTY", "03-Nov-2026"), 65)
        self.assertEqual(op.lot_size(lt, "BANKNIFTY", "2026-12-29"), 30)
        self.assertEqual(op.lot_size(lt, "FINNIFTY", "2026-11-23"), 60)
        self.assertEqual(op.lot_size(lt, "MIDCPNIFTY", "2026-11-23"), 120)
        self.assertEqual(op.lot_size(lt, "NIFTYNXT50", "2026-11-23"), 25)
        self.assertEqual(op.lot_size(lt, "RELIANCE", "23-Nov-2026"), 500)
        self.assertEqual(op.lot_size(lt, "NIFTY", "2031-06-24"), 65)
        self.assertIsNone(op.lot_size(lt, "BANKNIFTY", "2028-06-27"))   # blank cell, no contract
        self.assertIsNone(op.lot_size(lt, "NOSUCH", "2026-11-23"))
        self.assertIn("M&M", lt)

    def test_chain_rows_and_units(self):
        c = op.parse_chain(fixture("chain-NIFTY-03-Nov-2026.json.gz"), "NIFTY")
        self.assertEqual(c["expiry"], "2026-11-03")
        self.assertEqual(c["timestamp"], "2026-10-30T15:40:00")
        self.assertEqual(c["spot"], 23053.1)
        self.assertEqual(len(c["rows"]), 104)
        ks = [x["strike"] for x in c["rows"]]
        self.assertEqual(ks, sorted(ks))
        r = next(x for x in c["rows"] if x["strike"] == 23000)
        # OI is in contracts: 1,13,312 x lot 65 = 73,65,280 units, the bhavcopy's OpnIntrst
        self.assertEqual(r["ce"]["oi"], 113312)
        self.assertEqual(r["ce"]["oi"] * 65, 7365280)
        self.assertEqual(r["ce"]["bid_qty"] % 65, 0)                 # quote sizes are in units
        # NSE prints IV 0 for strikes it did not compute; we keep that as None, not 0
        self.assertIsNone(next(x for x in c["rows"] if x["strike"] == 22800)["ce"]["nse_iv"])

    def test_wrong_expiry_is_an_error_not_zeros(self):
        with self.assertRaises(op.OptionsError):
            op.parse_chain(fixture("chain-RELIANCE-25-Nov-2026.json"), "RELIANCE")
        with self.assertRaises(op.OptionsError):
            op.parse_chain({"error": "blocked"})

    def test_vix_history(self):
        chunks = [fixture(f) for f in sorted(os.listdir(DIR)) if f.startswith("vixhistory-")]
        h = op.parse_vix_history(chunks)
        self.assertEqual(len(h), 246)
        self.assertEqual(h[0], {"date": "2025-11-03", "close": h[0]["close"]})
        self.assertEqual(h[-1]["date"], "2026-10-30")
        self.assertEqual(h[-1]["close"], 15.3)
        self.assertEqual(op.vix_now(fixture("allIndices.json"))["last"], 15.45)

    def test_vix_chunks_stay_under_the_70_row_cap(self):
        import datetime as dt
        ch = op.vix_chunks(dt.date(2026, 11, 1))
        self.assertEqual(ch[0][0], dt.date(2025, 11, 1))
        self.assertEqual(ch[-1][1], dt.date(2026, 11, 1))
        for a, b in ch:
            self.assertLessEqual((b - a).days, 89)
        for (_, b), (a, _) in zip(ch, ch[1:]):
            self.assertEqual((a - b).days, 1)


class NiftyWeekly(unittest.TestCase):
    """NIFTY 03-Nov-2026 at the 30-Oct close."""

    @classmethod
    def setUpClass(cls):
        cls.a = analysis()

    def test_forward_from_parity_below_the_stale_spot(self):
        a = self.a
        self.assertEqual(a["forward_method"], "parity-mid")
        self.assertAlmostEqual(a["forward"], 23022.14, places=2)
        self.assertLess(a["forward"], a["spot"])        # spot froze at 15:30, options traded to 15:40
        self.assertEqual(len(a["forward_strikes"]), 5)
        self.assertTrue(all(abs(k - a["forward"]) <= 150 for k in a["forward_strikes"]))

    def test_put_and_call_iv_agree_at_the_money(self):
        a = self.a
        self.assertEqual(a["atm"], 23000)
        self.assertAlmostEqual(a["atm_iv_ce"], a["atm_iv_pe"], delta=0.1)
        self.assertAlmostEqual(a["atm_iv"], 12.07, delta=0.1)
        for k in (22900, 22950, 23050, 23100):
            r = row(a, k)
            self.assertAlmostEqual(r["ce"]["iv"], r["pe"]["iv"], delta=0.3, msg=k)
        # NSE's own field disagrees by about 5 points at the same strike; we never use it
        r = row(a, 23000)
        self.assertGreater(abs(r["pe"]["nse_iv"] - r["ce"]["nse_iv"]), 3)

    def test_smile_has_a_put_skew(self):
        a = self.a
        self.assertGreater(row(a, 22600)["pe"]["iv"], row(a, 23000)["pe"]["iv"])
        self.assertGreater(row(a, 22600)["pe"]["iv"], row(a, 23400)["ce"]["iv"])
        sk = a["skew"]
        self.assertGreater(sk["risk_reversal"], 0)
        sm = op.smile(a["rows"], a["forward"])
        self.assertTrue(all(s["type"] == ("PE" if s["strike"] < a["forward"] else "CE") for s in sm))

    def test_greeks_are_sane_and_in_the_stated_units(self):
        a = self.a
        ce, pe = row(a, 23000)["ce"], row(a, 23000)["pe"]
        self.assertAlmostEqual(ce["delta"], 0.53, delta=0.02)
        self.assertAlmostEqual(ce["delta"] - pe["delta"], 1.0, delta=0.001)
        self.assertAlmostEqual(ce["gamma"], pe["gamma"], places=6)
        self.assertAlmostEqual(ce["vega"], pe["vega"], places=3)
        # theta per calendar day: an ATM weekly with 4 days left loses roughly a quarter of its value a day
        self.assertLess(ce["theta"], -10)
        self.assertGreater(ce["theta"], -30)
        # vega per vol point: 1 point of IV is worth about straddle / IV / 2 per leg
        self.assertAlmostEqual(ce["vega"], a["straddle"]["price"] / a["atm_iv"] / 2, delta=1.0)

    def test_untradeable_strikes_get_no_iv(self):
        for r in self.a["rows"]:
            for s in ("ce", "pe"):
                side = r[s]
                if side and side["price"] is None:
                    self.assertIsNone(side["iv"])
                    self.assertIsNone(side["delta"])
                if side and side["iv"] is not None:
                    self.assertTrue(0.1 <= side["iv"] <= 500)

    def test_pcr_matches_nse_totals(self):
        a = self.a
        self.assertEqual(a["pcr"]["all"]["ce_oi"], 2149563)
        self.assertEqual(a["pcr"]["all"]["pe_oi"], 3490526)
        self.assertAlmostEqual(a["pcr_oi"], 3490526 / 2149563.0, places=4)
        self.assertAlmostEqual(a["pcr_volume"], 59605086 / 60163610.0, places=4)
        self.assertEqual(a["notes"], [])                  # row sums agree with NSE's totals
        near = a["pcr"]["near_atm"]
        self.assertEqual(near["strikes"], [22500, 23500])

    def test_max_pain_brute_force(self):
        rows = self.a["rows"]
        best, best_k = None, None
        for k_star in [x["strike"] for x in rows]:
            pay = sum((x["ce"]["oi"] if x["ce"] else 0) * max(0, k_star - x["strike"]) +
                      (x["pe"]["oi"] if x["pe"] else 0) * max(0, x["strike"] - k_star) for x in rows)
            if best is None or pay < best:
                best, best_k = pay, k_star
        self.assertEqual(self.a["max_pain"], best_k)
        self.assertEqual(self.a["max_pain"], 23000)
        self.assertAlmostEqual(self.a["max_pain_detail"]["pain"], best * 65, places=2)

    def test_walls(self):
        w = self.a["walls"]
        self.assertEqual(w["call"], 23500)
        self.assertEqual(w["put"], 22000)
        F = self.a["forward"]
        self.assertEqual(w["calls"][0]["oi"], max(r["ce"]["oi"] for r in self.a["rows"] if r["strike"] >= F))
        self.assertEqual(w["puts"][0]["oi"], max(r["pe"]["oi"] for r in self.a["rows"] if r["strike"] <= F))
        # without a forward every strike counts
        self.assertEqual(op.walls(self.a["rows"])["calls"][0]["oi"],
                         max(r["ce"]["oi"] for r in self.a["rows"] if r["ce"]))

    def test_straddle_and_expected_move_agree(self):
        a = self.a
        st, em = a["straddle"], a["expected_move"]
        self.assertAlmostEqual(st["price"], 232.82, places=2)
        self.assertAlmostEqual(st["rupees_per_lot"], 232.82 * 65, delta=0.5)
        self.assertAlmostEqual(em["from_straddle"], em["from_iv"], delta=0.03 * em["from_iv"])
        self.assertAlmostEqual(em["move"], 292, delta=3)
        self.assertAlmostEqual(em["high"] - em["low"], 2 * em["move"], places=1)

    def test_header_fields_for_consumers(self):
        a = self.a
        self.assertEqual(a["schema"], "bharat-trader/options/1")
        self.assertEqual(a["days_to_expiry"], 4)
        self.assertEqual(a["minutes_to_expiry"], 5750)
        self.assertEqual(a["lot_size"], 65)
        self.assertEqual(a["kind"], "index")
        self.assertEqual(a["units"]["oi"], "contracts")
        json.dumps(a, allow_nan=False)                   # plain JSON, no NaN or objects

    def test_stale_spot_warnings(self):
        w = " ".join(self.a["warnings"])
        self.assertIn("15:40", w)
        self.assertIn("last session", w)


class OtherChains(unittest.TestCase):
    def test_reliance_monthly(self):
        a = analysis("RELIANCE", "23-Nov-2026")
        self.assertEqual(a["kind"], "stock")
        self.assertEqual(a["lot_size"], 500)
        self.assertGreater(a["forward"], a["spot"])      # carry over 24 days
        self.assertAlmostEqual(a["atm_iv_ce"], a["atm_iv_pe"], delta=0.3)
        self.assertEqual(a["max_pain"], 1200)
        self.assertAlmostEqual(a["pcr_oi"], 146938 / 221564.0, places=4)
        self.assertIn("Equity", a["sources"]["chain"])

    def test_nifty_december(self):
        a = analysis("NIFTY", "01-Dec-2026")
        self.assertEqual(a["days_to_expiry"], 32)
        self.assertAlmostEqual(a["atm_iv_ce"], a["atm_iv_pe"], delta=0.5)
        self.assertGreater(a["forward"], a["spot"])

    def test_rate_barely_moves_black76_iv(self):
        a, b = analysis(r=0.0557), analysis(r=0.07)
        self.assertAlmostEqual(a["atm_iv"], b["atm_iv"], delta=0.05)


class Analytics(unittest.TestCase):
    def test_buildup_classes(self):
        self.assertEqual(op.classify_buildup(5, 100), "long build-up")
        self.assertEqual(op.classify_buildup(-5, 100), "short build-up")
        self.assertEqual(op.classify_buildup(-5, -100), "long unwinding")
        self.assertEqual(op.classify_buildup(5, -100), "short covering")
        self.assertIsNone(op.classify_buildup(0, 100))
        self.assertIsNone(op.classify_buildup(5, 0))

    def test_buildup_on_the_real_chain(self):
        b = op.oi_buildup(analysis()["rows"])
        # 23000 PE fell 29% while 68,220 contracts were added: put writing
        hit = next(d for d in b["contracts"] if d["strike"] == 23000 and d["type"] == "PE")
        self.assertEqual(hit["buildup"], "short build-up")
        self.assertEqual(hit["oi_chg"], 68220)
        self.assertGreater(b["summary"]["PE"]["short build-up"], 0)

    def test_iv_rank_and_percentile(self):
        r = op.iv_rank([10, 20, 30, 40, 50], 20)
        self.assertEqual(r["rank"], 25.0)
        self.assertEqual(r["percentile"], 20.0)
        self.assertIsNone(op.iv_rank([], 20)["rank"])
        self.assertIsNone(op.iv_rank([15, 15], 15)["rank"])
        chunks = [fixture(f) for f in sorted(os.listdir(DIR)) if f.startswith("vixhistory-")]
        h = op.parse_vix_history(chunks)
        v = op.iv_rank(h, 15.45)
        lo, hi = min(x["close"] for x in h), max(x["close"] for x in h)
        self.assertAlmostEqual(v["rank"], round(100 * (15.45 - lo) / (hi - lo), 1))
        self.assertEqual(v["sessions"], 246)

    def test_pcr_window_and_empty(self):
        rows = [{"strike": k, "ce": {"oi": 10, "volume": 5, "oi_chg": 1},
                 "pe": {"oi": 20, "volume": 5, "oi_chg": 2}} for k in range(100, 200, 10)]
        self.assertEqual(op.pcr(rows)["oi"], 2.0)
        self.assertEqual(op.pcr(rows, 150, 2)["strikes"], [130, 170])
        self.assertIsNone(op.pcr([])["oi"])

    def test_expected_move_formula(self):
        em = op.expected_move(100.0, 10000.0, 0.2, 0.01)
        self.assertAlmostEqual(em["from_straddle"], 100 * math.sqrt(math.pi / 2), places=2)
        self.assertAlmostEqual(em["from_iv"], 10000 * 0.2 * 0.1, places=2)

    def test_spot_warnings(self):
        t = op.parse_nse_time
        self.assertEqual(op.spot_warnings(t("2026-11-02 11:00"), t("2026-11-02 11:02")), [])
        self.assertIn("minutes old", op.spot_warnings(t("2026-11-02 11:00"), t("2026-11-02 11:30"))[0])
        self.assertIn("15:40", op.spot_warnings(t("2026-11-02 15:35"), t("2026-11-02 15:36"))[0])

    def test_pick_expiry_uses_only_the_exchange_list(self):
        exp = op.parse_contract_info(fixture("contract-info-NIFTY.json"))["expiries"]
        t = op.parse_nse_time
        self.assertEqual(op.pick_expiry(exp, None, t("2026-11-01 10:00")).isoformat(), "2026-11-03")
        self.assertEqual(op.pick_expiry(exp, None, t("2026-11-03 15:31")).isoformat(), "2026-11-09")
        self.assertEqual(op.pick_expiry(exp, "09-Nov-2026", t(NOW)).isoformat(), "2026-11-09")
        with self.assertRaises(op.OptionsError):
            op.pick_expiry(exp, "10-Nov-2026", t(NOW))        # a holiday, not an expiry

    def test_rate_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config.json")
            self.assertEqual(op.risk_free_rate(p)[0], op.DEFAULT_RATE)
            for v, want in ((0.065, 0.065), (6.5, 0.065), ("x", op.DEFAULT_RATE)):
                with open(p, "w") as fh:
                    json.dump({"risk_free_rate": v}, fh)
                self.assertAlmostEqual(op.risk_free_rate(p)[0], want)

    def test_inr(self):
        self.assertEqual(op.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(op.fnum(23022.14), "23,022.14")
        self.assertEqual(op.signed(-14334), "-14,334")


class Commands(unittest.TestCase):
    def test_expiries(self):
        code, out, _ = run(["expiries", "NIFTY"])
        self.assertEqual(code, 0)
        self.assertIn("09-Nov-2026  Mon", out)
        self.assertIn("23-Nov-2026  Mon    22 days  lot 65  monthly", out)
        code, out, _ = run(["expiries", "NIFTY", "--json"])
        data = json.loads(out)
        self.assertEqual(data["expiries"][0]["lot_size"], 65)
        self.assertFalse(data["expiries"][0]["monthly"])

    def test_chain_text(self):
        code, out, _ = run(["chain", "NIFTY", "--strikes", "3"])
        self.assertEqual(code, 0)
        self.assertIn("forward 23,022.14", out)
        self.assertIn("|*  23,000 |", out)
        self.assertIn("max pain 23,000", out)
        self.assertIn("WARNING: spot is the 15:30 cash close", out)
        self.assertEqual(len(re.findall(r"\|[ *] +[\d,]+ \|", out)), 7)

    def test_chain_json_windowed_and_write_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "options.json")
            code, out, _ = run(["chain", "NIFTY", "--strikes", "5", "--json", "--write", path])
            self.assertEqual(code, 0)
            shown = json.loads(out)
            self.assertEqual(len(shown["rows"]), 11)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        self.assertEqual(len(saved["rows"]), 104)
        for k in ("underlying", "spot", "forward", "expiry", "days_to_expiry", "lot_size", "rate", "rows",
                  "pcr_oi", "pcr_volume", "max_pain", "walls", "atm", "straddle", "expected_move",
                  "sources", "iv_rank", "smile"):
            self.assertIn(k, saved)
        r = next(x for x in saved["rows"] if x["strike"] == 23000)
        for k in ("ltp", "bid", "ask", "oi", "oi_chg", "volume", "iv", "delta", "gamma", "theta", "vega"):
            self.assertIn(k, r["ce"])
            self.assertIn(k, r["pe"])
        self.assertEqual(saved["iv_rank"]["current"], 15.45)

    def test_other_commands(self):
        code, out, _ = run(["pcr", "NIFTY"])
        self.assertIn("PCR (OI)      1.6238", out)
        code, out, _ = run(["maxpain", "NIFTY"])
        self.assertIn("<- max pain", out)
        code, out, _ = run(["oi", "RELIANCE"])
        self.assertIn("short build-up", out)
        self.assertIn("Call OI (resistance): 1,200", out)
        code, out, _ = run(["iv", "NIFTY"])
        self.assertIn("India VIX 15.45: IV rank", out)
        self.assertIn("25-delta skew", out)
        code, out, _ = run(["iv", "RELIANCE"])
        self.assertIn("bhavcopy backfill", out)
        code, out, _ = run(["maxpain", "NIFTY", "--json"])
        self.assertEqual(len(json.loads(out)["max_pain_curve"]), 104)

    def test_bad_input(self):
        code, _, err = run(["chain", "NIFTY", "--expiry", "10-Nov-2026"])
        self.assertEqual(code, 2)
        self.assertIn("not an expiry NSE lists", err)
        code, _, err = run(["chain", "nifty; rm"])
        self.assertEqual(code, 2)
        code, _, err = run(["chain", "SBIN"])
        self.assertEqual(code, 2)
        self.assertIn("offline: no file", err)


if __name__ == "__main__":
    unittest.main()
