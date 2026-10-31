"""trader-strategy tests, offline.

Fixtures:
  tests/fixtures/trader-options/      REAL NSE option chains of Fri 30-Oct-2026 15:40 (NIFTY 03-Nov and
                                      01-Dec, RELIANCE 23-Nov), contract info and fo_mktlots.csv, reused
  tests/fixtures/trader-strategy/     REAL Yahoo Finance chart answers fetched 2026-11-01, trimmed to the
                                      timestamps and closes: history-^NSEI-1d.json (1 year of NIFTY 50
                                      daily closes to 30-Oct-2026), history-^NSEI-5m.json (five-minute
                                      closes 26 to 30-Oct-2026), history-RELIANCE.NS-1d.json (1 year daily)

Reference margins: scanning risk computed from the risk arrays of NSE's SPAN file
nsccl.20261030.s.zip for 1 lot (65) of NIFTY: short 23000 CE 03-Nov 1,30,859; short 23000
straddle 03-Nov 1,24,079; short 23000 straddle 01-Dec 97,186; iron condor 22700/22800 PE -
23300/23400 CE 03-Nov 4,367 (exposure at 2% of spot notional per short lot: 29,969 a leg).
"""

import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

st = load("trader-strategy", "strategy")
op = load("trader-options", "options")
OPT = os.path.join(FIX, "trader-options")
HIST = os.path.join(FIX, "trader-strategy")
NOW = "2026-11-01 10:00"           # a Sunday: the chains are the last session's
CFG = {}


def analysis(sym="NIFTY", expiry="03-Nov-2026"):
    import gzip
    with gzip.open(os.path.join(OPT, "chain-%s-%s.json.gz" % (sym, expiry)), "rb") as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    with open(os.path.join(OPT, "fo_mktlots.csv"), encoding="utf-8") as fh:
        lots = op.parse_lots(fh.read())
    chain = op.parse_chain(raw, sym)
    return op.analyse(chain, op.lot_size(lots, sym, chain["expiry"]), 0.0557, now=op.parse_nse_time(NOW))


NIFTY = analysis()
NIFTY_DEC = analysis("NIFTY", "01-Dec-2026")
REL = analysis("RELIANCE", "23-Nov-2026")
MKT = {NIFTY["expiry"]: NIFTY}
MKT2 = {NIFTY["expiry"]: NIFTY, NIFTY_DEC["expiry"]: NIFTY_DEC}


def history(name="^NSEI"):
    h = {}
    for interval, key in (("1d", "daily"), ("5m", "intraday")):
        p = os.path.join(HIST, "history-%s-%s.json" % (name, interval))
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                h[key] = st.parse_history(fh.read())["closes"]
    h["source"] = "fixture"
    return h


def build(name, params=None, markets=None, **kw):
    markets = markets or MKT
    near = min(markets)
    legs = st.resolve_legs(st.template_specs(name, params or {}, markets[near]), markets, near)
    kw.setdefault("simulate", False)
    return st.evaluate(legs, markets, CFG, name, params, **kw)


def legs_of(text, markets=None, **kw):
    markets = markets or MKT
    return st.resolve_legs(st.parse_legs(text), markets, min(markets), **kw)


def price(a, k, kind):
    return next(x for x in a["rows"] if x["strike"] == k)[kind.lower()]["price"]


def run(args, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with redirect_stdout(out), redirect_stderr(err):
            code = st.main(args if args[:1] == ["list"] else args + ["--offline", OPT, "--offline", HIST, "--now", NOW])
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class Shared(unittest.TestCase):
    def test_black76_is_trader_options_maths(self):
        for F, K, T, s, kind in ((23022.14, 23000, 0.0109, 0.121, "CE"), (23022.14, 22500, 0.0109, 0.15, "PE"),
                                 (1174.5, 1250, 0.0657, 0.28, "CE"), (100, 120, 1.0, 0.5, "PE")):
            self.assertAlmostEqual(st.black76_price(F, K, T, 0.0557, s, kind), op.black76_price(F, K, T, 0.0557, s, kind), 10)
            g1, g2 = st.black76_greeks(F, K, T, 0.0557, s, kind), op.black76_greeks(F, K, T, 0.0557, s, kind)
            for k in g1:
                self.assertAlmostEqual(g1[k], g2[k], 10)

    def test_inr_and_option_tick(self):
        self.assertEqual(st.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(st.tick_price(44.33, "BUY"), 44.35)
        self.assertEqual(st.tick_price(44.33, "SELL"), 44.30)
        self.assertEqual(st.tick_price(24.025, "SELL"), 24.0)
        self.assertEqual(st.tick_price(24.05, "BUY"), 24.05)

    def test_trading_sessions(self):
        t = st.parse_time("2026-10-30 15:40")
        self.assertEqual(st.sessions_between(t, st.expiry_moment("2026-11-03")), 2)       # Mon 2, Tue 3
        self.assertEqual(st.sessions_between(t, st.expiry_moment("2026-11-17")), 11)      # 10 Nov is Diwali-Balipratipada
        self.assertEqual(st.minutes_left_today(st.parse_time("2026-11-02 14:30")), 60)
        self.assertEqual(st.minutes_left_today(st.parse_time("2026-11-01 10:00")), 0)     # Sunday


class Legs(unittest.TestCase):
    def test_parse_legs_grammar(self):
        s = st.parse_legs("-1 CE ATM+200, +2 pe 22800 @01-Dec-2026; -1 CALL D25 =41.5\n+1 FUT, +1 PE P50")
        self.assertEqual([x["side"] for x in s], ["SELL", "BUY", "SELL", "BUY", "BUY"])
        self.assertEqual(s[0]["strike"], {"by": "offset", "value": 200.0})
        self.assertEqual(s[1], {"side": "BUY", "lots": 2, "kind": "PE", "strike": {"by": "strike", "value": 22800.0},
                                "expiry": "2026-12-01", "price": None})
        self.assertEqual(s[2]["strike"], {"by": "delta", "value": 0.25})
        self.assertEqual(s[2]["price"], 41.5)
        self.assertEqual(s[3]["kind"], "FUT")
        self.assertIsNone(s[3]["strike"])
        self.assertEqual(s[4]["strike"], {"by": "price", "value": 50.0})
        for bad in ("", "1 XX 23000", "-1 CE abc", "-1 FUT 23000", "-0 CE 23000", "-1 CE 23000 @someday"):
            with self.assertRaises(st.StrategyError, msg=bad):
                st.parse_legs(bad)

    def test_strikes_by_offset_delta_and_price(self):
        legs = legs_of("-1 CE ATM+200, -1 PE D16, +1 CE P50, +1 PE 22000")
        self.assertEqual(legs[0]["strike"], 23200)          # ATM 23000 + 200
        self.assertEqual(legs[1]["strike"], 22700)          # put delta -0.16
        self.assertAlmostEqual(legs[1]["greeks"]["delta"], -0.16, 2)
        self.assertEqual(legs[2]["strike"], 23200)          # premium nearest ₹50: 44.85 (23150 is 60.35)
        self.assertEqual(legs[3]["strike"], 22000)
        with self.assertRaises(st.StrategyError):
            legs_of("-1 CE 23010")                          # not listed: says the nearest
        for l in legs:
            self.assertEqual(l["lot_size"], 65)
            self.assertEqual(l["qty"], 65)

    def test_premium_iv_and_fill(self):
        mid = legs_of("+1 CE 23000, -1 PE 23000")
        self.assertAlmostEqual(mid[0]["price"], price(NIFTY, 23000, "CE"), 6)
        self.assertEqual(mid[0]["iv"], 12.1)
        touch = legs_of("+1 CE 23000, -1 PE 23000", fill="touch")
        row = next(x for x in NIFTY["rows"] if x["strike"] == 23000)
        self.assertEqual(touch[0]["price"], row["ce"]["ask"])
        self.assertEqual(touch[1]["price"], row["pe"]["bid"])
        own = legs_of("-1 CE 23000 =130")
        self.assertEqual((own[0]["price"], own[0]["price_source"]), (130.0, "given"))

    def test_every_template_builds(self):
        for t in st.TEMPLATE_NAMES:
            markets = MKT2 if t == "calendar" else MKT
            d = build(t, {}, markets)
            self.assertEqual(d["schema"], "bharat-trader/strategy/1", t)
            self.assertTrue(d["legs"], t)
            self.assertEqual(len(d["orders"]), len(d["legs"]), t)
        self.assertEqual(len(st.list_templates()), 17)
        with self.assertRaises(st.StrategyError):
            st.template_name("moon-spread")
        self.assertEqual(st.template_name("Iron Butterfly"), "iron-fly")


class Payoff(unittest.TestCase):
    def expiry_value(self, d, x):
        """Hand payoff at expiry: intrinsic minus premium, per leg."""
        total = 0.0
        for l in d["legs"]:
            if l["instrument"] == "FUT":
                v = x
            else:
                v = max(0.0, x - l["strike"]) if l["option_type"] == "CE" else max(0.0, l["strike"] - x)
            total += (1 if l["side"] == "BUY" else -1) * (v - l["price"]) * l["qty"]
        return total

    def test_curve_is_the_hand_payoff_for_every_single_expiry_template(self):
        for t in st.TEMPLATE_NAMES:
            if t == "calendar":
                continue
            d = build(t)
            c = d["curve"]
            for x, y in list(zip(c["x"], c["expiry"]))[::20]:
                self.assertAlmostEqual(y, self.expiry_value(d, x), delta=0.02, msg=(t, x))

    def test_iron_condor_numbers(self):
        d = build("iron-condor", {"wings": 200, "width": 100})
        ks = [(l["side"], l["option_type"], l["strike"]) for l in d["legs"]]
        self.assertEqual(ks, [("BUY", "PE", 22700), ("SELL", "PE", 22800), ("SELL", "CE", 23200), ("BUY", "CE", 23300)])
        credit = (price(NIFTY, 22800, "PE") + price(NIFTY, 23200, "CE") - price(NIFTY, 22700, "PE")
                  - price(NIFTY, 23300, "CE"))
        e = d["expiry"]
        self.assertAlmostEqual(d["net_premium"]["amount"], credit * 65, 1)
        self.assertEqual(d["net_premium"]["kind"], "credit")
        self.assertAlmostEqual(e["max_profit"], credit * 65, 1)
        self.assertAlmostEqual(e["max_loss"], -(100 - credit) * 65, 1)
        self.assertAlmostEqual(e["breakevens"][0], 22800 - credit, delta=0.02)
        self.assertAlmostEqual(e["breakevens"][1], 23200 + credit, delta=0.02)
        self.assertFalse(e["unlimited_profit"] or e["unlimited_loss"])
        self.assertTrue(e["exact"])
        # charges narrow the profit zone on both sides
        lo, hi = e["breakevens_after_charges"]
        self.assertGreater(lo, e["breakevens"][0])
        self.assertLess(hi, e["breakevens"][1])
        self.assertEqual([s["shift"] > 0 for s in e["breakeven_shift"]], [True, False])
        self.assertAlmostEqual(e["reward_risk"], credit / (100 - credit), 2)

    def test_spreads_breakevens_and_caps(self):
        bc = build("bull-call-spread", {"width": 100})
        debit = price(NIFTY, 23000, "CE") - price(NIFTY, 23100, "CE")
        self.assertAlmostEqual(bc["expiry"]["breakevens"][0], 23000 + debit, delta=0.02)
        self.assertAlmostEqual(bc["expiry"]["max_profit"], (100 - debit) * 65, 1)
        self.assertAlmostEqual(bc["expiry"]["max_loss"], -debit * 65, 1)
        bp = build("bull-put-spread", {"width": 100})
        credit = price(NIFTY, 23000, "PE") - price(NIFTY, 22900, "PE")
        self.assertAlmostEqual(bp["expiry"]["breakevens"][0], 23000 - credit, delta=0.02)
        self.assertAlmostEqual(bp["expiry"]["max_loss"], -(100 - credit) * 65, 1)
        br = build("bear-put-spread", {"width": 100})
        self.assertAlmostEqual(br["expiry"]["max_profit"], (100 - (price(NIFTY, 23000, "PE") - price(NIFTY, 22900, "PE"))) * 65, 1)
        bk = build("bear-call-spread", {"width": 100})
        self.assertEqual(bk["net_premium"]["kind"], "credit")

    def test_unlimited_sides(self):
        self.assertTrue(build("short-straddle")["expiry"]["unlimited_loss"])
        self.assertTrue(build("short-strangle")["expiry"]["unlimited_loss"])
        self.assertTrue(build("call-ratio-spread")["expiry"]["unlimited_loss"])
        ls = build("long-straddle")
        self.assertTrue(ls["expiry"]["unlimited_profit"])
        self.assertFalse(ls["expiry"]["unlimited_loss"])
        straddle = (price(NIFTY, 23000, "CE") + price(NIFTY, 23000, "PE"))
        self.assertAlmostEqual(ls["expiry"]["max_loss"], -straddle * 65, 1)
        self.assertEqual(ls["expiry"]["breakevens"], [round(23000 - straddle, 2), round(23000 + straddle, 2)])
        # a short put side is not unlimited: the most it can lose is with the underlying at 0
        pr = build("put-ratio-spread", {"width": 100})
        self.assertFalse(pr["expiry"]["unlimited_loss"])
        self.assertEqual(pr["expiry"]["max_loss_at"], 0.0)
        credit = 2 * price(NIFTY, 22900, "PE") - price(NIFTY, 23000, "PE")
        self.assertAlmostEqual(pr["expiry"]["max_loss"], (23000 - 2 * 22900 + credit) * 65, 0)

    def test_butterflies_and_broken_wing(self):
        cb = build("call-butterfly", {"width": 100})
        self.assertEqual([(l["strike"], l["lots"], l["side"]) for l in cb["legs"]],
                         [(22900, 1, "BUY"), (23000, 2, "SELL"), (23100, 1, "BUY")])
        debit = price(NIFTY, 22900, "CE") - 2 * price(NIFTY, 23000, "CE") + price(NIFTY, 23100, "CE")
        self.assertAlmostEqual(cb["expiry"]["max_profit"], (100 - debit) * 65, 1)
        self.assertEqual(cb["expiry"]["max_profit_at"], 23000)
        bw = build("call-butterfly", {"width": 100, "skip": 100})
        self.assertEqual([l["strike"] for l in bw["legs"]], [22900, 23000, 23200])
        self.assertFalse(bw["expiry"]["unlimited_loss"])
        # broken wing: above the top strike it loses the extra 100 points less the net premium
        high = bw["curve"]["expiry"][-1]
        self.assertAlmostEqual(high, self.expiry_value(bw, bw["curve"]["x"][-1]), delta=0.02)
        pb = build("put-butterfly", {"width": 100, "skip": 50})
        self.assertEqual([l["strike"] for l in pb["legs"]], [23100, 23000, 22850])

    def test_covered_call_and_protective_put(self):
        cc = build("covered-call", {"wings": 300})
        self.assertEqual([l["instrument"] for l in cc["legs"]], ["FUT", "OPT"])
        self.assertEqual(cc["legs"][0]["price"], round(NIFTY["forward"], 2))
        self.assertEqual(cc["legs"][1]["strike"], 23300)
        self.assertAlmostEqual(cc["expiry"]["max_profit"], (23300 - cc["legs"][0]["price"] + price(NIFTY, 23300, "CE")) * 65, 0)
        pp = build("protective-put", {"wings": 300})
        self.assertTrue(pp["expiry"]["unlimited_profit"])
        self.assertAlmostEqual(pp["expiry"]["max_loss"], -(pp["legs"][0]["price"] - 22700 + price(NIFTY, 22700, "PE")) * 65, 0)
        self.assertEqual(pp["orders"][0]["instrument"], "FUTIDX")

    def test_calendar_two_expiries(self):
        d = build("calendar", {}, MKT2)
        self.assertEqual(d["expiries"], ["2026-11-03", "2026-12-01"])
        near, far = d["legs"]
        self.assertEqual((near["side"], near["expiry"], far["side"], far["expiry"]),
                         ("SELL", "2026-11-03", "BUY", "2026-12-01"))
        e = d["expiry"]
        self.assertFalse(e["exact"])
        self.assertFalse(e["unlimited_profit"] or e["unlimited_loss"])
        self.assertAlmostEqual(e["max_loss"], d["net_premium"]["amount"], 0)        # the debit, far below or above
        self.assertEqual(len(e["breakevens"]), 2)
        # at the near expiry, at the strike: the far call by Black-76 less the debit
        T = far["t_years"] - near["t_years"]
        Ff = 23000 * far["forward"] / near["forward"]
        want = (st.black76_price(Ff, 23000, T, 0.0557, far["iv"] / 100, "CE") - far["price"] + near["price"]) * 65
        i = d["curve"]["x"].index(23000)
        self.assertAlmostEqual(d["curve"]["expiry"][i], want, 1)
        self.assertTrue(d["margin"]["defined_risk"])
        self.assertTrue(any("two expiries" in n for n in d["notes"]))

    def test_today_curve_and_iv_shift(self):
        d = build("long-straddle", at="today")
        c = d["curve"]
        self.assertEqual(c["horizon"], "now")
        i = min(range(len(c["x"])), key=lambda j: abs(c["x"][j] - NIFTY["forward"]))
        self.assertLess(abs(c["at"][i]), 400)            # worth about what was paid, right now
        up = build("long-straddle", at="today", iv_shift=2.0)
        self.assertGreater(up["curve"]["at"][i], c["at"][i] + 0.9 * 2 * 65 * sum(l["greeks"]["vega"] for l in d["legs"]))
        with self.assertRaises(st.StrategyError):
            build("long-straddle", at="2026-11-09")       # after the near expiry
        mid = build("iron-fly", at="2026-11-02 15:30")
        self.assertEqual(mid["curve"]["horizon"], "2026-11-02T15:30")

    def test_net_greeks_add_up(self):
        d = build("short-strangle", {"delta": 0.16})
        g = d["greeks"]
        want = sum((1 if l["side"] == "BUY" else -1) * l["greeks"]["theta"] * l["qty"] for l in d["legs"])
        self.assertAlmostEqual(g["theta"], want, 1)
        self.assertGreater(g["theta"], 0)
        self.assertLess(g["vega"], 0)
        self.assertLess(g["gamma"], 0)
        self.assertLess(abs(g["delta_lots"]), 0.1)


class Charges(unittest.TestCase):
    def test_after_charges_curve_is_round_trip_charges_per_leg(self):
        d = build("bull-call-spread", {"width": 100})
        lc, sc = d["legs"]
        c = d["curve"]
        on = "2026-10-30"
        for x in (22800.0, 23050.0, 23300.0):
            i = c["x"].index(x) if x in c["x"] else None
            if i is None:
                continue
            ch = 0.0
            ch += (st.round_trip_charges(lc["price"], x - 23000, 65, CFG, "OPT", on, True)["total"] if x > 23000
                   else st.round_trip_charges(lc["price"], 0, 65, CFG, "OPT", on)["total"])
            ch += st.round_trip_charges(0, sc["price"], 65, CFG, "OPT", on)["total"]
            self.assertAlmostEqual(c["expiry"][i] - c["expiry_net"][i], ch, 1, x)

    def test_stt_on_exercise_and_warning(self):
        # 2026 rates: 0.15% of the intrinsic value on exercise, from 1 April 2026
        r = st.round_trip_charges(127.48, 300, 65, CFG, "OPT", "2026-10-30", True)
        self.assertAlmostEqual(r["stt"], 300 * 65 * 0.0015, 2)
        self.assertEqual(st.fno_rates("OPT", "2026-03-31")["stt_exercise_pct"], 0.125)
        d = build("long-straddle")
        w = [x for x in d["warnings"] if x.startswith("STT on exercise")]
        self.assertEqual(len(w), 2)
        self.assertIn("0.15%", w[0])
        self.assertFalse(any(x.startswith("STT on exercise") for x in build("short-strangle")["warnings"]))

    def test_stock_options_warn_of_delivery(self):
        d = build("bull-call-spread", {}, {REL["expiry"]: REL})
        self.assertTrue(any("DELIVERY" in x for x in d["warnings"]))
        self.assertEqual(d["legs"][0]["lot_size"], 500)
        self.assertEqual(d["orders"][0]["instrument"], "OPTSTK")

    def test_config_overrides_the_rates(self):
        legs = legs_of("+1 CE 23000")
        cheap = st.evaluate(legs, MKT, {"fno_charges": {"OPT": {"brokerage_flat": 0}}}, simulate=False)
        dear = st.evaluate(legs, MKT, {}, simulate=False)
        self.assertLess(cheap["charges"]["at_forward_expiry"], dear["charges"]["at_forward_expiry"])


class Margin(unittest.TestCase):
    def test_margin_required_is_what_a_broker_blocks_not_the_max_loss(self):
        d = build("iron-condor", {"wings": 200, "width": 100})
        m = d["margin"]
        self.assertTrue(m["defined_risk"])
        self.assertEqual(m["method"], "span-style")
        self.assertEqual(m["label"], "Margin required (estimate, +/-15%)")
        self.assertEqual(m["max_loss"], round(-d["expiry"]["max_loss"], 2))          # exact, its own field
        self.assertEqual(m["max_loss"], 4129.12)
        self.assertGreater(m["required"], m["max_loss"])                                # never read max loss as capital
        s = m["span_style"]
        self.assertAlmostEqual(m["required"], s["scan_risk"] + s["exposure"] + s["premium_paid"], 1)
        # exposure is charged on both short legs' notional, hedged or not
        self.assertAlmostEqual(s["exposure"], 2 * NIFTY["forward"] * 65 * 0.02, 0)
        self.assertAlmostEqual(m["return_on_margin"], 100 * d["expiry"]["max_profit"] / m["required"], 2)
        self.assertEqual(m["low"], round(m["required"] * 0.85, 2))
        self.assertIsNone(m["broker_basket"])
        self.assertNotIn("amount", m)
        for t in ("iron-fly", "bull-call-spread", "bear-call-spread", "call-butterfly", "bull-put-spread"):
            mm = build(t)["margin"]
            self.assertEqual(mm["method"], "span-style", t)
            self.assertGreater(mm["required"], mm["max_loss"], t)

    def test_hedged_needs_less_than_naked(self):
        ic = build("iron-condor", {"wings": 200, "width": 100})["margin"]
        naked_doc = build("short-strangle", {"wings": 200})
        naked = naked_doc["margin"]
        self.assertEqual([l["strike"] for l in naked_doc["legs"]], [23200, 22800])
        self.assertLess(ic["required"], naked["required"])
        self.assertLess(ic["span_style"]["scan_risk"], naked["span_style"]["scan_risk"] / 10)
        self.assertEqual(ic["span_style"]["exposure"], naked["span_style"]["exposure"])   # same shorts, same ELM
        self.assertTrue(naked["max_loss_unlimited"])
        spread = st.evaluate(legs_of("-1 PE 23000, +1 PE 22900"), MKT, CFG, simulate=False)["margin"]
        put = st.evaluate(legs_of("-1 PE 23000"), MKT, CFG, simulate=False)["margin"]
        self.assertLess(spread["required"], put["required"])

    def test_bought_options_only_need_the_premium(self):
        d = build("long-strangle")
        m = d["margin"]
        self.assertEqual(m["method"], "premium")
        self.assertEqual(m["error_pct"], 0)
        self.assertEqual(m["required"], -d["net_premium"]["amount"])
        self.assertEqual(m["max_loss"], m["required"])
        self.assertIsNone(m["return_on_margin"])                                        # unlimited profit

    def test_span_style_against_nse_span_file(self):
        cases = [("-1 CE 23000", MKT, 130859), ("-1 CE 23000, -1 PE 23000", MKT, 124079),
                 ("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400", MKT, 4367)]
        for text, mk, scan in cases:
            d = st.evaluate(legs_of(text, mk), mk, CFG, simulate=False)
            s = d["margin"]["span_style"]
            self.assertLess(abs(s["scan_risk"] - scan) / scan, 0.10, (text, s["scan_risk"]))
        # the whole estimate (scan + exposure) for the 01-Dec straddle, within its stated bound
        d = st.evaluate(legs_of("-1 CE 23000, -1 PE 23000", {NIFTY_DEC["expiry"]: NIFTY_DEC}),
                        {NIFTY_DEC["expiry"]: NIFTY_DEC}, CFG, simulate=False)
        m = d["margin"]
        self.assertEqual(m["method"], "span-style")
        self.assertEqual(m["error_pct"], 15)
        ref = 97186 + 2 * 29969
        self.assertLess(abs(m["required"] - ref) / ref, m["error_pct"] / 100.0)
        self.assertLessEqual(m["low"], ref)
        self.assertGreaterEqual(m["high"], ref)
        self.assertIn("SPAN-style", m["explain"])

    def test_expiry_day_adds_exposure_on_index_shorts(self):
        legs = legs_of("-1 CE 23000")
        pos = st.Position(legs)
        normal = st.span_style(pos, "index", asof=st.parse_time("2026-11-02 10:00"))
        expiry = st.span_style(pos, "index", asof=st.parse_time("2026-11-03 10:00"))
        self.assertEqual(normal["expiry_day_elm"], 0)
        self.assertAlmostEqual(expiry["expiry_day_elm"], NIFTY["forward"] * 65 * 0.02, 1)

    def test_stock_scan_has_a_wider_bound(self):
        mk = {REL["expiry"]: REL}
        d = st.evaluate(legs_of("-1 CE 1200", mk), mk, CFG, simulate=False)
        self.assertEqual(d["margin"]["error_pct"], 35)
        self.assertAlmostEqual(d["margin"]["span_style"]["assumptions"]["price_scan"], REL["forward"] * 0.20, 1)
        over = st.span_style(st.Position(legs_of("-1 CE 1200", mk)), "stock", {"scan_pct": 15})
        self.assertLess(over["scan_risk"], d["margin"]["span_style"]["scan_risk"])


class Probability(unittest.TestCase):
    def test_textbook_matches_the_closed_form(self):
        # one long call: profit after charges above a breakeven b, P = N(d2) with the forward measure
        d = build("bull-call-spread", {"width": 500})
        legs = legs_of("+1 CE 23000")
        doc = st.evaluate(legs, MKT, CFG, simulate=False)
        b = doc["expiry"]["breakevens_after_charges"][0]
        s, T, F = NIFTY["atm_iv"] / 100.0, NIFTY["t_years"], NIFTY["forward"]
        d2 = (math.log(F / b) - 0.5 * s * s * T) / (s * math.sqrt(T))
        want = 100 * statistics.NormalDist().cdf(d2)
        self.assertAlmostEqual(doc["probability"]["textbook"]["pop"], want, delta=0.1)
        self.assertTrue(d["probability"]["textbook"]["pop"] > 0)

    def test_textbook_expected_value_is_model_value_less_price(self):
        # gross EV under the lognormal = undiscounted Black-76 value at the ATM IV minus the premium
        legs = legs_of("+1 CE 23000")
        pos = st.Position(legs)
        s, T, F = NIFTY["atm_iv"] / 100.0, NIFTY["t_years"], NIFTY["forward"]
        n = 4000
        xs = [F * math.exp(-0.5 * s * s * T + s * math.sqrt(T) * z) for z in st._quantiles(n)]
        ev = sum(pos.gross(x, T) for x in xs) / n
        want = (st.black76_price(F, 23000, T, 0, s, "CE") - legs[0]["price"]) * 65
        self.assertAlmostEqual(ev, want, delta=5)

    def test_simulation_is_seeded_and_reproducible(self):
        h = history()
        a = build("iron-condor", {"wings": 200, "width": 100}, history=h, simulate=True, paths=3000)
        b = build("iron-condor", {"wings": 200, "width": 100}, history=h, simulate=True, paths=3000)
        c = build("iron-condor", {"wings": 200, "width": 100}, history=h, simulate=True, paths=3000, seed=8)
        sa, sc = a["probability"]["simulated"], c["probability"]["simulated"]
        self.assertEqual(sa, b["probability"]["simulated"])
        self.assertNotEqual(sa["ev"], sc["ev"])
        self.assertLess(abs(sa["pop"] - sc["pop"]), 4)
        self.assertEqual(sa["sessions"], 2)                   # Mon 2 and Tue 3 Nov
        self.assertEqual(sa["history"]["daily_returns"], 246)      # 248 sessions, one with no close
        self.assertGreater(sa["realised_vol"], 5)
        self.assertLess(sa["realised_vol"], 40)
        self.assertLessEqual(sa["tail_5pct_avg"], sa["tail_5pct"])
        self.assertGreaterEqual(sa["tail_5pct"], a["expiry"]["max_loss_after_charges"] - 1)
        sc3 = a["probability"]["scenarios"]
        self.assertEqual([x["iv_shift"] for x in sc3], [-5.0, 0.0, 5.0])
        self.assertGreater(sc3[0]["pop"], sc3[2]["pop"])     # a short-vol position likes a quieter market

    def test_bootstrap_scales_to_a_target_vol(self):
        pos = st.Position(legs_of("+1 CE 23000"))
        rets = st.returns_from_closes(history()["daily"])
        a = st.simulated_probability(pos, rets, 20, paths=500, scale_to=0.2)
        self.assertEqual(a["scaled_to_vol"], 20.0)
        self.assertIsNone(st.simulated_probability(pos, [], 2))

    def test_no_history_says_so(self):
        d = build("iron-fly", simulate=True, history=None)
        self.assertIsNone(d["probability"]["simulated"])
        self.assertIn("no price history", d["probability"]["simulated_skipped"])


class Orders(unittest.TestCase):
    def test_broker_neutral_drafts(self):
        d = build("iron-condor", {"wings": 200, "width": 100})
        o = d["orders"]
        self.assertEqual(len(o), 4)
        for x, l in zip(o, d["legs"]):
            self.assertEqual((x["exchange"], x["product"], x["order_type"], x["segment"]), ("NFO", "NRML", "LIMIT", "OPT"))
            self.assertIsNone(x["tradingsymbol"])
            self.assertEqual(x["qty"], 65)
            self.assertEqual(x["instrument"], "OPTIDX")
            self.assertAlmostEqual(round(x["price"] / 0.05) * 0.05, x["price"], 6)
            if x["side"] == "BUY":
                self.assertGreaterEqual(x["price"], l["price"] - 1e-9)
            else:
                self.assertLessEqual(x["price"], l["price"] + 1e-9)
        mis = st.draft_orders(d, "MIS")
        self.assertEqual({x["product"] for x in mis}, {"MIS"})
        with self.assertRaises(st.StrategyError):
            st.draft_orders(d, "CNC")
        doc = st.orders_document(d)
        self.assertTrue(doc["paper"])
        self.assertEqual(doc["status"], "proposed")
        self.assertEqual(doc["created_by"], "trader-strategy")

    def test_trader_risk_still_decides(self):
        rk = load("trader-risk", "check")
        d = build("iron-condor")
        res = rk.check(st.orders_document(d)["orders"], {"capital": 100000, "paper": True}, {"cash": 100000},
                       {}, None, "2026-11-02", rk.parse_now("2026-11-02 10:00"), {})
        self.assertEqual(res["pass"], 0)
        self.assertTrue(all(any(r.startswith("fno:") for r in x["reasons"]) for x in res["results"]))


class Commands(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-strategy-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_list(self):
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("iron-condor", out)
        self.assertIn("calendar", out)

    def test_build_iron_condor_text(self):
        code, out, err = run(["build", "iron-condor", "NIFTY", "--expiry", "03-Nov-2026", "--wings", "200",
                              "--width", "100", "--lots", "1", "--paths", "2000"])
        self.assertEqual(code, 0, err)
        for bit in ("iron-condor on NIFTY, expiry 03-Nov-2026", "SELL 1 lot 22,800 PE", "BUY  1 lot 23,300 CE",
                    "Net premium: ₹", "received (credit)", "Breakevens at expiry: 22,", "after charges",
                    "Margin required (estimate, +/-15%): about ₹67,408.76",
                    "Max loss at expiry: ₹4,129.12 (not the margin); return on margin", "textbook (lognormal at IV 12.10%)", "simulated (bootstrap",
                    "STT on exercise", "Nothing was placed"):
            self.assertIn(bit, out)

    def test_json_write_and_orders(self):
        code, out, err = run(["build", "short-strangle", "NIFTY", "--delta", "0.16", "--json", "--write",
                              "--orders", "--no-sim"], cwd=self.dir)
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertTrue(d["expiry"]["unlimited_loss"])
        self.assertEqual(d["margin"]["method"], "span-style")
        with open(os.path.join(self.dir, "trader", "strategy.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["schema"], "bharat-trader/strategy/1")
        self.assertEqual(saved["generated_at"], "2026-11-01T10:00:00")
        with open(os.path.join(self.dir, "trader", "orders.json"), encoding="utf-8") as fh:
            orders = json.load(fh)
        self.assertEqual(len(orders["orders"]), 2)
        self.assertTrue(orders["paper"])
        # a file with someone else's proposals is kept unless --replace
        with open(os.path.join(self.dir, "trader", "orders.json"), "w", encoding="utf-8") as fh:
            json.dump({"created_by": "trader-budget", "orders": [{"symbol": "TCS"}]}, fh)
        code, _, err = run(["build", "iron-fly", "NIFTY", "--orders", "--no-sim"], cwd=self.dir)
        self.assertEqual(code, 2)
        self.assertIn("--replace", err)
        code, _, err = run(["build", "iron-fly", "NIFTY", "--orders", "--replace", "--no-sim"], cwd=self.dir)
        self.assertEqual(code, 0, err)

    def test_price_custom_legs_over_two_expiries(self):
        code, out, err = run(["price", "NIFTY", "--legs", "-1 CE 23000, +1 CE 23000 @01-Dec-2026", "--json", "--no-sim"])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual(d["expiries"], ["2026-11-03", "2026-12-01"])
        self.assertEqual(d["name"], "custom")

    def test_calendar_needs_two_expiries(self):
        code, _, err = run(["build", "calendar", "NIFTY"])
        self.assertEqual(code, 2)
        self.assertIn("--far-expiry", err)
        code, out, err = run(["build", "calendar", "NIFTY", "--expiry", "03-Nov-2026", "--far-expiry", "01-Dec-2026",
                              "--no-sim"])
        self.assertEqual(code, 0, err)
        self.assertIn("approximate: two expiries", out)

    def test_errors_are_plain_words(self):
        for args, bit in ((["build", "moon", "NIFTY"], "no template"),
                          (["build", "iron-condor", "NIFTY", "--expiry", "04-Nov-2026"], "not an expiry"),
                          (["price", "NIFTY", "--legs", "-1 CE 23010"], "not a listed strike"),
                          (["build", "iron-condor", "nifty 50!"], "not an NSE F&O symbol")):
            code, _, err = run(args + ["--no-sim"])
            self.assertEqual(code, 2, args)
            self.assertIn(bit, err, args)

    def test_works_copied_alone_from_an_options_file(self):
        alone = os.path.join(self.dir, "skills", "trader-strategy")
        os.makedirs(alone)
        shutil.copy(os.path.join(ROOT, "trader-strategy", "strategy.py"), alone)
        opts = os.path.join(self.dir, "options.json")
        with open(opts, "w", encoding="utf-8") as fh:
            json.dump(NIFTY, fh)
        p = subprocess.run([sys.executable, os.path.join(alone, "strategy.py"), "build", "iron-fly", "NIFTY",
                            "--options", opts, "--no-sim", "--json", "--now", NOW],
                           cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        d = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(d["margin"]["method"], "span-style")
        self.assertTrue(d["margin"]["defined_risk"])
        # without trader-options next to it and no file, it says what to do
        p = subprocess.run([sys.executable, os.path.join(alone, "strategy.py"), "build", "iron-fly", "NIFTY",
                            "--no-sim", "--now", NOW], cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 2)
        self.assertIn("trader-options", p.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
