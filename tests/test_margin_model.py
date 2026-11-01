"""The SPAN + ELM margin model (the shared block in trader-strategy, trader-risk and trader-broker), offline.

Fixtures:
  tests/fixtures/trader-strategy/span-nifty-20261030.json   REAL NSE Clearing SPAN risk arrays
        (nsccl.20261030.s.zip, end of day 30-Oct-2026), trimmed to NIFTY options of 03-Nov and 01-Dec-2026,
        strikes 21800 to 24300: the 16 scenario losses a unit, the settle price and the delta of each
  tests/fixtures/trader-strategy/broker-margin-examples.json  published broker margins for hedged NIFTY
        spreads (HDFC Securities 2026, lot 65; Zerodha 2020) and the NSE / SEBI rules they rest on
  tests/fixtures/trader-options/   the REAL option chains of 30-Oct-2026 the legs are priced on

Reference total for a structure from the SPAN file: max(0, scan risk - net option value) + 2% ELM on the
spot notional of each short leg + the net premium paid (NSE: "Total SPAN Margin requirement is equal to
SPAN Risk Requirement less the net option value"; ELM "levied" on short options only, no offset).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402
import test_strategy as TS  # noqa: E402

st = TS.st
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
HERE = os.path.join(FIX, "trader-strategy")
with open(os.path.join(HERE, "span-nifty-20261030.json"), encoding="utf-8") as fh:
    SPAN = json.load(fh)
with open(os.path.join(HERE, "broker-margin-examples.json"), encoding="utf-8") as fh:
    BROKER = json.load(fh)
LOT = SPAN["lot_size"]
EXP = {"N": "20261103", "D": "20261201"}

# (name, legs for trader-strategy, which chain, defined risk)
CASES = [
    ("short 23000 CE", "-1 CE 23000", "N", False),
    ("short 23000 straddle", "-1 CE 23000, -1 PE 23000", "N", False),
    ("short 22800/23300 strangle", "-1 PE 22800, -1 CE 23300", "N", False),
    ("iron condor 22700/22800-23300/23400", "+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400", "N", True),
    ("iron condor 22500/22800-23300/23600", "+1 PE 22500, -1 PE 22800, -1 CE 23300, +1 CE 23600", "N", True),
    ("iron fly 22800/23050/23300", "+1 PE 22800, -1 PE 23050, -1 CE 23050, +1 CE 23300", "N", True),
    ("bull put 23000/22900", "-1 PE 23000, +1 PE 22900", "N", True),
    ("bear call 23100/23200", "-1 CE 23100, +1 CE 23200", "N", True),
    ("bear put debit 23100/22900", "+1 PE 23100, -1 PE 22900", "N", True),
    ("short 23000 straddle, Dec", "-1 CE 23000, -1 PE 23000", "D", False),
    ("iron condor 21900/22250-23850/24200, Dec", "+1 PE 21900, -1 PE 22250, -1 CE 23850, +1 CE 24200", "D", True),
    ("iron condor 22200/22300-23800/23900, Dec", "+1 PE 22200, -1 PE 22300, -1 CE 23800, +1 CE 23900", "D", True),
    ("bull put 22800/22500, Dec", "-1 PE 22800, +1 PE 22500", "D", True),
]


def markets(which):
    return TS.MKT if which == "N" else {TS.NIFTY_DEC["expiry"]: TS.NIFTY_DEC}


def file_reference(text, which):
    """(scan risk, net option value, reference total) from the SPAN file's own risk arrays."""
    series = next(s for s in SPAN["series"] if s["pe"] == EXP[which])
    book = {(o["o"], o["k"]): o for o in series["opts"]}
    tot, nov, elm = [0.0] * 16, 0.0, 0.0
    for spec in st.parse_legs(text):
        q = spec["lots"] * LOT * (1 if spec["side"] == "BUY" else -1)
        o = book[(spec["kind"][0], float(spec["strike"]["value"]))]
        for i in range(16):
            tot[i] += q * o["ra"][i]
        nov += q * o["p"]
        if q < 0:
            elm += -q * SPAN["spot"] * 0.02
    scan = max(0.0, max(tot))
    return scan, nov, max(0.0, scan - nov) + elm + max(0.0, nov)


def estimate(text, which, when="2026-11-01 10:00"):
    mk = markets(which)
    pos = st.Position(TS.legs_of(text, mk))
    return pos, st.span_style(pos, "index", asof=st.parse_time(when))


class AgainstNseSpanFile(unittest.TestCase):
    def test_every_structure_within_the_stated_band(self):
        worst = 0.0
        for name, text, which, _ in CASES:
            scan, nov, ref = file_reference(text, which)
            _, s = estimate(text, which)
            err = s["total"] / ref - 1.0
            worst = max(worst, abs(err))
            self.assertLess(abs(err), s["error_pct"] / 100.0, (name, s["total"], ref))
            self.assertGreaterEqual(err, -0.02, (name, "should not undershoot the exchange"))
            if scan > 1000:
                self.assertLess(abs(s["scan_risk"] / scan - 1.0), 0.20, (name, s["scan_risk"], scan))
        self.assertLess(worst, 0.08)        # measured: +0.1% to +7.4% on 30-Oct-2026

    def test_hedged_structures_within_three_percent(self):
        for name, text, which, defined in CASES:
            if defined:
                _, _, ref = file_reference(text, which)
                _, s = estimate(text, which)
                self.assertLess(abs(s["total"] / ref - 1.0), 0.03, name)

    def test_a_credit_spread_needs_about_its_width_of_span(self):
        _, s = estimate("-1 PE 23000, +1 PE 22900", "N")
        self.assertAlmostEqual(s["span"], 100 * LOT, delta=0.02 * 100 * LOT)
        self.assertAlmostEqual(s["span"], s["scan_risk"] - s["net_option_value"], 1)
        _, d = estimate("+1 PE 23100, -1 PE 22900", "N")          # a debit spread: no SPAN, the debit paid
        self.assertEqual(d["span"], 0)
        self.assertAlmostEqual(d["premium_paid"], d["net_option_value"], delta=0.02 * d["premium_paid"])


class HedgedAgainstNaked(unittest.TestCase):
    def test_condor_fly_and_spread_need_less_than_the_naked_strangle(self):
        _, naked = estimate("-1 PE 22800, -1 CE 23300", "N")
        for text in ("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400",
                     "+1 PE 22800, -1 PE 23050, -1 CE 23050, +1 CE 23300", "-1 PE 22800, +1 PE 22700"):
            _, h = estimate(text, "N")
            self.assertLess(h["total"], naked["total"], text)
            self.assertLess(h["span"], naked["span"] / 5, text)
        _, ic = estimate("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400", "N")
        self.assertEqual(ic["exposure"], naked["exposure"])          # ELM on both shorts, hedged or not

    def test_naked_stays_conservative(self):
        for name, text, which, defined in CASES:
            if not defined:
                _, _, ref = file_reference(text, which)
                _, s = estimate(text, which)
                self.assertGreaterEqual(s["total"], ref, name)
                self.assertGreaterEqual(s["high"], ref * 1.10, name)


class MarginAtLeastMaxLoss(unittest.TestCase):
    def test_defined_risk_margin_covers_the_max_loss(self):
        for name, text, which, defined in CASES:
            if not defined:
                continue
            pos, s = estimate(text, which)
            ext = st.extremes(pos, lambda x: pos.gross(x, pos.t_near))
            self.assertGreaterEqual(s["total"], -ext["max_loss"], name)
            self.assertGreaterEqual(s["span"] + s["premium_paid"], -ext["max_loss"] * 0.98, name)


class ExpiryDay(unittest.TestCase):
    def test_two_percent_more_on_every_short_index_option_hedged_or_not(self):
        for text in ("-1 CE 23000", "+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400"):
            _, mon = estimate(text, "N", "2026-11-02 10:00")
            _, tue = estimate(text, "N", "2026-11-03 09:15")
            shorts = text.count("-1")
            self.assertEqual(mon["expiry_day_elm"], 0)
            self.assertAlmostEqual(tue["expiry_day_elm"], shorts * LOT * SPAN["spot"] * 0.02, 1)
            self.assertAlmostEqual(tue["total"] - mon["total"], tue["expiry_day_elm"], 1)
        # an iron condor's margin on its expiry day: about 1.9 x the normal day, near 30x its max loss here
        _, mon = estimate("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400", "N", "2026-11-02 10:00")
        _, tue = estimate("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400", "N", "2026-11-03 09:15")
        self.assertGreater(tue["total"] / mon["total"], 1.8)

    def test_no_calendar_benefit_for_the_expiring_contract(self):
        mk = TS.MKT2
        legs = TS.legs_of("-1 CE 23000, +1 CE 23000 @01-Dec-2026", mk)
        pos = st.Position(legs)
        normal = st.span_style(pos, "index", asof=st.parse_time("2026-11-02 10:00"))
        expiry = st.span_style(pos, "index", asof=st.parse_time("2026-11-03 09:15"))
        self.assertTrue(normal["calendar_benefit"])
        self.assertGreater(normal["calendar_charge"], 0)            # 1.77% of spot a unit of matched delta
        self.assertFalse(expiry["calendar_benefit"])
        self.assertGreater(expiry["scan_risk"], normal["scan_risk"] * 3)


class ElmRules(unittest.TestCase):
    def row(self, cp, k, qty=-65, T=0.05, spot=23053.1, iv=15.0):
        return {"fut": False, "cp": cp, "strike": k, "expiry": "2026-12-01", "qty": qty, "F": spot, "spot": spot,
                "T": T, "iv": iv, "rate": 0.055, "price": None}

    def test_deep_out_of_the_money_and_long_dated_rates(self):
        near = st.span_elm([self.row("CE", 24000)], "index", "2026-11-02")
        deep = st.span_elm([self.row("CE", 25500)], "index", "2026-11-02")           # 10.6% out of the money
        far = st.span_elm([self.row("CE", 24000, T=1.2)], "index", "2026-11-02")      # over 9 months
        self.assertAlmostEqual(near["exposure"], 65 * 23053.1 * 0.02, 2)
        self.assertAlmostEqual(deep["exposure"], 65 * 23053.1 * 0.03, 2)
        self.assertAlmostEqual(far["exposure"], 65 * 23053.1 * 0.05, 2)
        stock = st.span_elm([self.row("PE", 1150, qty=-500, spot=1164.88)], "stock", "2026-11-02")
        deep_s = st.span_elm([self.row("PE", 800, qty=-500, spot=1164.88)], "stock", "2026-11-02")
        self.assertAlmostEqual(stock["exposure"], 500 * 1164.88 * 0.035, 2)
        self.assertAlmostEqual(deep_s["exposure"], 500 * 1164.88 * 0.0525, 2)
        self.assertEqual(stock["error_pct"], 35)

    def test_only_bought_options_need_the_premium(self):
        m = st.span_elm([self.row("CE", 23000, qty=65)], "index", "2026-11-02")
        self.assertEqual(m["method"], "premium")
        self.assertEqual(m["span"] + m["exposure"], 0)
        self.assertAlmostEqual(m["total"], m["premium_paid"], 6)

    def test_the_model_is_named_everywhere(self):
        self.assertEqual(st.MARGIN_MODEL, check.MARGIN_MODEL)
        self.assertEqual(st.MARGIN_MODEL, ticket.MARGIN_MODEL)
        d = TS.build("iron-condor", {"wings": 200, "width": 100})
        self.assertEqual(d["margin"]["margin_model"], st.MARGIN_MODEL)
        self.assertEqual(d["margin"]["span_style"]["model"], st.MARGIN_MODEL)


class PublishedBrokerExamples(unittest.TestCase):
    def implied(self, price, F, K, T, cp):
        lo, hi = 0.01, 3.0
        for _ in range(100):
            mid = 0.5 * (lo + hi)
            if st.black76_price(F, K, T, 0.055, mid, cp) > price:
                hi = mid
            else:
                lo = mid
        return 50.0 * (lo + hi)

    def test_hdfc_securities_debit_spreads_2026(self):
        n = 0
        for ex in BROKER["examples"]:
            if not ex["id"].startswith("hdfcsec"):
                continue
            S, T = ex["spot_close"], ex["days"] / 365.0
            rows = []
            for l in ex["legs"]:
                q = ex["lot_size"] * (1 if l["side"] == "BUY" else -1)
                rows.append({"fut": False, "cp": l["type"], "strike": l["strike"], "expiry": "2099-01-01", "qty": q,
                             "F": S, "spot": S, "T": T, "iv": self.implied(l["premium"], S, l["strike"], T, l["type"]),
                             "rate": 0.055, "price": l["premium"]})
            m = st.span_elm(rows, "index", "2026-01-01")
            pub = ex["published"]
            self.assertAlmostEqual(m["premium_paid"], pub["max_loss"], delta=1.0, msg=ex["id"])
            # the published "approx margin" is SPAN + exposure: within 3%; with the debit paid, inside the band
            self.assertLess(abs((m["span"] + m["exposure"]) / pub["margin"] - 1.0), 0.03, ex["id"])
            self.assertLess(abs(m["total"] / pub["margin"] - 1.0), m["error_pct"] / 100.0, ex["id"])
            n += 1
        self.assertEqual(n, 3)

    def test_zerodha_split_exposure_on_the_short_leg_only(self):
        ex = next(e for e in BROKER["examples"] if e["id"] == "zerodha-2020-06")
        pub = ex["published"]
        spot = pub["exposure"] / 75 / 0.02                          # 9,580: one short leg at 2%
        rows = [{"fut": False, "cp": "CE", "strike": 9600, "expiry": "2020-06-04", "qty": -75, "F": spot, "spot": spot,
                 "T": 5 / 365.0, "iv": 30.0, "rate": 0.04, "price": None},
                {"fut": False, "cp": "CE", "strike": 9700, "expiry": "2020-06-04", "qty": 75, "F": spot, "spot": spot,
                 "T": 5 / 365.0, "iv": 30.0, "rate": 0.04, "price": None}]
        m = st.span_elm(rows, "index", "2020-06-01")
        self.assertAlmostEqual(m["exposure"], pub["exposure"], delta=1.0)
        self.assertLess(abs(m["span"] / pub["span"] - 1.0), 0.05)
        self.assertLess(abs(m["total"] / pub["margin"] - 1.0), 0.05)


class SameModelEverywhere(unittest.TestCase):
    def test_risk_and_broker_agree_with_strategy(self):
        legs = TS.legs_of("+1 PE 22700, -1 PE 22800, -1 CE 23300, +1 CE 23400")
        pos = st.Position(legs)
        s = st.span_style(pos, "index", asof=st.parse_time("2026-11-02 10:00"))
        rows = st.span_rows(legs)
        r = check.span_elm(rows, "index", "2026-11-02")
        b = ticket.span_elm(rows, "index", "2026-11-02")
        self.assertAlmostEqual(r["total"], s["total"], 1)
        self.assertAlmostEqual(b["total"], s["total"], 1)


if __name__ == "__main__":
    unittest.main()
