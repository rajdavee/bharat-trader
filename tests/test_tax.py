"""trader-tax tests, offline: a fixture financial year through every income head, the ICAI turnover method
(options premium included), the s.112A exemption and the 31 Jan 2018 grandfathering, the loss set-off and
carry-forward rules, the FY boundary and the dated switch to the Income-tax Act, 2025 on 1 Apr 2026.

Fixtures (tests/fixtures/trader-tax/, see its README.md):
  zerodha-tradebook-fy2025-26.csv   Zerodha Console tradebook layout; real NSE prices on each date (equity
                                    from trader-lab's daily bars, NIFTY futures and options from NSE's F&O
                                    bhavcopy of 18 and 20 Nov 2025, 27 Mar and 6 Apr 2026); ids made up
  trader/tax/holdings.csv           100 SBIN bought 15 Jun 2017 at 284.00 (NSE close) with the 31 Jan 2018
                                    highest price 316.60 (NSE bhavcopy)
  trader/tax/carry-forward.csv, expenses.csv, profile.json   example inputs
"""

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

tax = load("trader-tax", "tax")
journal = load("trader-journal", "journal")
TF = os.path.join(FIX, "trader-tax")
BOOK = os.path.join(TF, "zerodha-tradebook-fy2025-26.csv")
NOW = tax.parse_now("2026-11-01 10:00")


def fill(n, at, symbol, side, qty, price, segment="EQ", charges=0.0):
    return {"id": "t:%d" % n, "at": at, "symbol": symbol, "underlying": symbol, "segment": segment,
            "exchange": "NSE" if segment == "EQ" else "NFO", "product": "", "side": side, "qty": qty,
            "price": price, "value": qty * price, "charges": charges, "charges_estimated": False,
            "source": "test", "paper": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = tax.Paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture(self):
        shutil.copytree(os.path.join(TF, "trader"), os.path.join(self.tmp, "trader"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tax.main(["import", BOOK, "--broker", "zerodha", "--workdir", self.tmp]), 0)

    def own(self, fills, holdings=None, carry=None, profile=None):
        """A workdir with these fills as an import (read directly, as without trader-journal)."""
        os.makedirs(self.paths.t("journal", "imports"))
        with open(self.paths.t("journal", "imports", "test.json"), "w") as fh:
            json.dump({"fills": fills}, fh)
        os.makedirs(self.paths.tax())
        if holdings:
            with open(self.paths.tax("holdings.csv"), "w") as fh:
                fh.write("symbol,qty,buy_date,buy_price,fmv_2018\n" + "\n".join(holdings) + "\n")
        if carry:
            with open(self.paths.tax("carry-forward.csv"), "w") as fh:
                fh.write("fy,kind,amount\n" + "\n".join(carry) + "\n")
        if profile is not None:
            with open(self.paths.tax("profile.json"), "w") as fh:
                json.dump(profile, fh)

    def report(self, fy, **kw):
        return tax.report(self.paths, tax.parse_fy(fy), now=NOW, **kw)


class FixtureYear(Base):
    def setUp(self):
        Base.setUp(self)
        self.fixture()
        self.d = self.report("2025-26")

    def test_intraday_equity_is_speculative_business(self):
        sp = self.d["heads"]["speculative"]
        rows = [r for r in self.d["trades"] if r["kind"] == "intraday"]
        self.assertEqual([(r["symbol"], r["date"], r["qty"]) for r in rows],
                         [("RELIANCE", "2025-06-10", 50), ("SBIN", "2025-08-14", 100), ("INFY", "2025-12-03", 40),
                          ("TCS", "2026-03-30", 20)])
        self.assertEqual(sp["gross"], -410.0)              # -575 - 455 + 944 - 324
        self.assertTrue(all(r["head"] == "speculative business" for r in rows))
        self.assertEqual(sp["income"], tax.r2(sp["net"] - sp["expenses"]))
        self.assertLess(sp["income"], 0)

    def test_fno_is_non_speculative_business(self):
        ns = self.d["heads"]["non_speculative"]
        self.assertEqual(ns["futures"]["gross"], 15060.0)   # 75 x (26220.80 - 26020.00)
        self.assertEqual(ns["options"]["gross"], -4912.5)   # +1575 expired, -3585, -2902.50
        self.assertEqual(ns["gross"], 10147.5)
        # every charge is a business expense, STT included; expenses.csv: 2,400 F&O + its turnover share of 5,988
        self.assertAlmostEqual(ns["expenses"], 2400 + 5988 * 23122.5 / 25420.5, places=1)
        self.assertEqual(ns["income"], tax.r2(ns["gross"] - ns["charges"] - ns["expenses"]))

    def test_an_option_never_closed_in_the_file_expires_worthless_and_asks(self):
        pe = [r for r in self.d["trades"] if r["symbol"] == "NIFTY25N1825900PE"]
        self.assertEqual(len(pe), 1)
        self.assertTrue(pe[0]["expired"])
        self.assertEqual((pe[0]["date"], pe[0]["gross"], pe[0]["premium_sold"]), ("2025-11-18", 1575.0, 1575.0))
        self.assertTrue(any("NIFTY25N1825900PE expired" in x for x in self.d["needs_input"]))

    def test_delivery_split_from_intraday_on_the_same_day(self):
        # INFY on 3 Dec 2025: bought 40, sold 70: 40 intraday, 30 out of the lot bought 5 Nov 2024 (long-term)
        infy = [r for r in self.d["trades"] if r["symbol"] == "INFY"]
        self.assertEqual(sorted((r["kind"], r["qty"]) for r in infy), [("intraday", 40), ("ltcg", 30)])
        lt = [r for r in infy if r["kind"] == "ltcg"][0]
        self.assertEqual((lt["bought"], lt["gross"]), ("2024-11-05", -5265.0))

    def test_capital_gains_heads_and_stt_not_deductible(self):
        st = [r for r in self.d["trades"] if r["kind"] == "stcg"]
        self.assertEqual(sorted(r["symbol"] for r in st), ["ITC", "RELIANCE", "TCS"])
        rel = [r for r in st if r["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel["gross"], 14508.0)             # 40 x (1528.40 - 1165.70)
        # STT is in the charges but not deducted: net = gross - the other charges only
        self.assertAlmostEqual(rel["net"], rel["gross"] - rel["expenses"], places=2)
        self.assertAlmostEqual(rel["stt_not_deductible"], 0.001 * (46628 + 61136), places=1)
        self.assertEqual(self.d["heads"]["stcg"]["gains_by_rate"], [[20.0, rel["net"]]])

    def test_grandfathered_sbin(self):
        sb = [r for r in self.d["trades"] if r["symbol"] == "SBIN" and r["kind"] == "ltcg"][0]
        self.assertEqual(sb["grandfathered"], {"fmv_2018": 316.6, "actual_cost": 28400.0, "cost_used": 31660.0})
        self.assertEqual(sb["cost"], 31660.0)
        self.assertAlmostEqual(sb["net"], 82475 - 31660 - sb["expenses"], places=2)
        self.assertEqual(self.d["heads"]["ltcg"]["grandfathered"], 1)

    def test_turnover_the_icai_way_with_the_premium_question(self):
        t = self.d["turnover"]
        self.assertEqual((t["intraday"], t["futures"], t["options"]), (2298.0, 15060.0, 8062.5))
        self.assertEqual(t["total"], 25420.5)
        self.assertEqual(t["options_premium_sold"], 7950.0)  # 75 x 21 + 75 x 85: the shorts' premium
        self.assertEqual(t["total_with_premium"], 33370.5)
        self.assertIn("33,370.50", t["premium_words"])
        self.assertEqual(t["delivery_sale_value"], 328157.0)
        self.assertIn("5.11", t["source"])

    def test_audit_check_words(self):
        a = self.d["audit"]
        self.assertEqual(a["status"], "no audit by turnover")
        self.assertEqual(a["limit"], 100000000.0)
        words = " ".join(a["words"])
        self.assertIn("presumptive", words)
        self.assertIn("s.44AB", words)
        self.assertIn("stock-in-trade", words)

    def test_set_off_and_carry_forward(self):
        so = self.d["setoff"]
        steps = " ".join(so["steps"])
        self.assertIn("Speculative loss", steps)
        self.assertIn("NOT set off against F&O", steps)
        lapsed = [(x["fy"], x["kind"]) for x in so["lapsed"]]
        self.assertEqual(lapsed, [("2020-21", "speculative")])
        cf = {(c["fy"], c["kind"]): c for c in so["carry_forward"]}
        self.assertEqual(cf[("2022-23", "speculative")]["usable_until"], "2026-27")
        self.assertEqual(cf[("2025-26", "speculative")]["usable_until"], "2029-30")
        self.assertEqual(cf[("2025-26", "speculative")]["amount"], -self.d["heads"]["speculative"]["income"])
        # the FY 2023-24 F&O loss absorbed this year's F&O income; the rest carries to FY 2031-32
        used = {(u["fy"], u["kind"]): u["used"] for u in so["brought_forward_used"]}
        self.assertEqual(used[("2023-24", "non_speculative")], self.d["heads"]["non_speculative"]["income"])
        self.assertAlmostEqual(cf[("2023-24", "non_speculative")]["amount"],
                               6000 - self.d["heads"]["non_speculative"]["income"], places=2)
        self.assertEqual(used[("2024-25", "stcl")], 3000.0)
        self.assertEqual(self.d["taxable"]["business_total"], 0.0)

    def test_ltcg_exemption_covers_the_rest(self):
        t = self.d["taxable"]
        self.assertGreater(t["ltcg_total"], 0)
        self.assertEqual(t["ltcg_exempt"], t["ltcg_total"])       # under Rs 1.25 lakh
        self.assertEqual(t["ltcg_taxable"], 0.0)
        self.assertEqual(self.d["special_rate"][-1]["rate_pct"], 12.5)

    def test_itr_mapping_advance_tax_and_disclaimer(self):
        where = {x["what"]: x["where"] for x in self.d["itr"]}
        self.assertIn("Schedule BP part B", where["intraday"])
        self.assertIn("Schedule 112A", where["ltcg"])
        self.assertIn("CFL", where["setoff"])
        self.assertEqual([x["due"] for x in self.d["advance_tax"]["dates"]],
                         ["2025-06-15", "2025-09-15", "2025-12-15", "2026-03-15"])
        self.assertEqual([x["cumulative_pct"] for x in self.d["advance_tax"]["dates"]], [15, 45, 75, 100])
        self.assertIn("not tax advice", self.d["disclaimer"])
        self.assertEqual(self.d["act"], "Income-tax Act, 1961")
        self.assertEqual(self.d["open"]["delivery_lots"][0]["symbol"], "SBIN")     # 50 bought 3 Nov 2025
        self.assertEqual([o["symbol"] for o in self.d["open"]["fno"]], ["NIFTY26MAYFUT"])

    def test_export(self):
        res = tax.export(self.paths, self.d)
        self.assertEqual(res["folder"], self.paths.tax("FY2025-26"))
        self.assertEqual(res["files"], ["carry-forward.csv", "report.md", "summary.csv", "tax.json", "trades.csv"])
        for f in res["files"]:
            with open(os.path.join(res["folder"], f), encoding="utf-8") as fh:
                self.assertIn("not tax advice", fh.read(), f)
        with open(os.path.join(res["folder"], "report.md"), encoding="utf-8") as fh:
            md = fh.read()
        for part in ("## Income per head", "## Turnover and the audit check", "## Set-off and carry-forward",
                     "## Taxable amounts", "## Advance tax", "## Where each number goes", "## Rules used"):
            self.assertIn(part, md)
        with open(os.path.join(res["folder"], "trades.csv"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 2 + len(self.d["trades"]))


class Boundary(Base):
    def test_fy_boundary_and_the_2025_act(self):
        self.fixture()
        d = self.report("2026-27")
        self.assertEqual(d["act"], "Income-tax Act, 2025")
        self.assertEqual(d["year_word"], "tax year")
        syms = [(r["symbol"], r["kind"]) for r in d["trades"]]
        self.assertEqual(syms, [("RELIANCE", "intraday"), ("NIFTY26MAYFUT", "futures")])
        fut = d["trades"][1]
        self.assertEqual((fut["opened"], fut["date"], fut["gross"]), ("2026-03-27", "2026-04-06", -17134.0))
        self.assertEqual(d["turnover"]["futures"], 17134.0)
        self.assertIn("s.63", " ".join(d["audit"]["words"]))
        self.assertTrue(any("Income-tax Act, 2025" in n for n in d["notes"]))
        # FY 2025-26 (no return on record yet) is chained: its speculative loss arrives here
        cf = {(c["fy"], c["kind"]) for c in d["setoff"]["carry_forward"]}
        self.assertIn(("2025-26", "speculative"), cf)
        self.assertIn(("2026-27", "non_speculative"), cf)
        self.assertTrue(any("worked out here" in n for n in d["notes"]))
        self.assertFalse(d["needs_input"])                  # last year's expired option belongs to last year
        prev = self.report("2025-26")
        self.assertEqual([r for r in prev["trades"] if r["date"] > "2026-03-31"], [])
        self.assertTrue(any("closed after 31 Mar" in n for n in prev["notes"]))

    def test_fy_helpers(self):
        self.assertEqual(tax.parse_fy("2025-26"), 2025)
        self.assertEqual(tax.parse_fy("FY2026-27"), 2026)
        self.assertEqual(tax.fy_of(tax.parse_date("2026-03-31")), 2025)
        self.assertEqual(tax.fy_of(tax.parse_date("2026-04-01")), 2026)
        for bad in ("2025-27", "2017-18", "twenty"):
            with self.assertRaises(tax.TaxError):
                tax.parse_fy(bad)

    def test_dated_rule_switch(self):
        self.assertEqual(tax.act_for(2025)["act"], "Income-tax Act, 1961")
        a = tax.act_for(2026)
        self.assertEqual((a["s"]["stcg"], a["s"]["ltcg"], a["s"]["audit"][:4]), ("s.196", "s.198", "s.63"))
        self.assertEqual(tax.rule_at(tax.RATES, tax.parse_date("2024-07-22"))["stcg_pct"], 15.0)
        self.assertEqual(tax.rule_at(tax.RATES, tax.parse_date("2024-07-23"))["stcg_pct"], 20.0)
        self.assertEqual(tax.rule_at(tax.RATES, tax.parse_date("2024-07-23"))["ltcg_pct"], 12.5)
        self.assertEqual(tax.by_fy(tax.LTCG_EXEMPTION, 2023)["amount"], 100000.0)
        self.assertEqual(tax.by_fy(tax.LTCG_EXEMPTION, 2024)["amount"], 125000.0)
        r24 = [r["rule"] for r in tax.rules_for(2024) if r["section"] == "s.111A"]
        self.assertEqual(len(r24), 2)                       # FY 2024-25 has both rates
        self.assertEqual(len([r for r in tax.rules_for(2025) if r["section"] == "s.111A"]), 1)
        self.assertTrue(any(r["section"] == "s.536" for r in tax.rules_for(2026)))
        for r in tax.rules_for(2026) + tax.rules_for(2025):
            self.assertTrue(r["source"] and r["effective"] and r["section"], r)

    def test_fy_2024_25_two_rates_and_the_exemption_on_the_higher_one(self):
        # 100 shares bought 1 Jun 2023; half sold before 23 Jul 2024, half after (illustrative prices)
        self.own([fill(1, "2023-06-01T10:00:00", "ABC", "BUY", 100, 1000.0),
                  fill(2, "2024-07-10T10:00:00", "ABC", "SELL", 50, 3000.0),
                  fill(3, "2024-08-10T10:00:00", "ABC", "SELL", 50, 4000.0)])
        d = self.report("2024-25")
        self.assertEqual(d["heads"]["ltcg"]["gains_by_rate"], [[10.0, 100000.0], [12.5, 150000.0]])
        sr = {x["rate_pct"]: x for x in d["special_rate"]}
        self.assertEqual((sr[12.5]["exempt"], sr[12.5]["amount"]), (125000.0, 25000.0))
        self.assertEqual((sr[10.0]["exempt"], sr[10.0]["amount"]), (0.0, 100000.0))
        self.assertTrue(any("two rates" in n for n in d["notes"]))


class Rules(Base):
    def test_holding_period_is_more_than_twelve_months(self):
        self.own([fill(1, "2024-11-05T10:00:00", "ABC", "BUY", 10, 100.0),
                  fill(2, "2025-11-05T10:00:00", "ABC", "SELL", 5, 120.0),
                  fill(3, "2025-11-06T10:00:00", "ABC", "SELL", 5, 120.0)])
        d = self.report("2025-26")
        self.assertEqual([(r["date"], r["kind"]) for r in d["trades"]],
                         [("2025-11-05", "stcg"), ("2025-11-06", "ltcg")])
        self.assertTrue(tax.long_term(tax.parse_date("2024-02-29"), tax.parse_date("2025-03-01")))
        self.assertFalse(tax.long_term(tax.parse_date("2024-02-29"), tax.parse_date("2025-02-28")))

    def test_grandfathering_three_ways(self):
        # cost 284, FMV 316.60 (SBIN's real numbers); three sale prices: above the FMV, between, below the cost
        self.own([fill(1, "2025-06-02T10:00:00", "SBIN", "SELL", 10, 400.0),
                  fill(2, "2025-06-03T10:00:00", "SBIN", "SELL", 10, 300.0),
                  fill(3, "2025-06-04T10:00:00", "SBIN", "SELL", 10, 250.0)],
                 holdings=["SBIN,30,2017-06-15,284.00,316.60"])
        rows = self.report("2025-26")["trades"]
        self.assertEqual([r["cost"] for r in rows], [3166.0, 3000.0, 2840.0])   # FMV; the sale value; actual cost
        self.assertEqual([r["net"] for r in rows], [834.0, 0.0, -340.0])

    def test_grandfathering_without_the_fmv_asks(self):
        self.own([fill(1, "2025-06-02T10:00:00", "SBIN", "SELL", 10, 400.0)],
                 holdings=["SBIN,10,2017-06-15,284.00,"])
        d = self.report("2025-26")
        self.assertEqual(d["trades"][0]["cost"], 2840.0)
        self.assertTrue(any("31 Jan 2018 value" in x for x in d["needs_input"]))

    def test_sale_with_no_lot_is_left_out_and_asked(self):
        self.own([fill(1, "2025-06-02T10:00:00", "XYZ", "SELL", 10, 400.0)])
        d = self.report("2025-26")
        self.assertEqual(d["trades"], [])
        self.assertTrue(any("no lot to match" in x for x in d["needs_input"]))

    def test_option_turnover_with_premiums(self):
        # a short call bought back at a loss and a long put sold at a loss: |difference| each; premium shown apart
        self.own([fill(1, "2025-11-20T09:20:00", "NIFTY25N2526200CE", "SELL", 75, 85.0, "OPT"),
                  fill(2, "2025-11-20T15:20:00", "NIFTY25N2526200CE", "BUY", 75, 132.8, "OPT"),
                  fill(3, "2025-11-20T09:20:00", "NIFTY25N2526100PE", "BUY", 75, 110.0, "OPT"),
                  fill(4, "2025-11-20T15:20:00", "NIFTY25N2526100PE", "SELL", 75, 71.3, "OPT")])
        t = self.report("2025-26")["turnover"]
        self.assertEqual(t["options"], 6487.5)              # 3,585 + 2,902.50: losses count like profits
        self.assertEqual(t["options_premium_sold"], 6375.0)  # only the short's opening premium, 75 x 85
        self.assertEqual(t["total_with_premium"], 12862.5)

    def test_partial_closes_are_separate_squared_off_trades(self):
        self.own([fill(1, "2025-11-18T09:15:00", "NIFTY25NOVFUT", "BUY", 150, 26020.0, "FUT"),
                  fill(2, "2025-11-18T15:00:00", "NIFTY25NOVFUT", "SELL", 75, 25948.8, "FUT"),
                  fill(3, "2025-11-20T15:25:00", "NIFTY25NOVFUT", "SELL", 75, 26220.8, "FUT")])
        d = self.report("2025-26")
        self.assertEqual([r["gross"] for r in d["trades"]], [-5340.0, 15060.0])
        self.assertEqual(d["turnover"]["futures"], 20400.0)
        self.assertEqual(d["heads"]["non_speculative"]["gross"], 9720.0)

    def test_audit_thresholds(self):
        big = 1500000.0    # each NIFTY future round trip +/- 15 lakh: 70 of them make Rs 10.5 crore turnover
        fills = []
        for i in range(70):
            day = "2025-%02d-%02dT10:00:00" % (5 + i // 25, 1 + i % 25)
            fills += [fill(2 * i, day, "NIFTY25DECFUT", "BUY", 75, 26000.0, "FUT"),
                      fill(2 * i + 1, day.replace("10:00", "15:00"), "NIFTY25DECFUT", "SELL", 75,
                           26000.0 + (big if i % 2 else -big) / 75, "FUT")]
        self.own(fills)
        d = self.report("2025-26")
        self.assertEqual(d["turnover"]["futures"], 105000000.0)
        self.assertEqual(d["audit"]["status"], "audit required")

    def test_cash_and_presumptive_history(self):
        self.own([fill(1, "2025-11-18T09:15:00", "NIFTY25NOVFUT", "BUY", 75, 26020.0, "FUT"),
                  fill(2, "2025-11-20T15:25:00", "NIFTY25NOVFUT", "SELL", 75, 25948.8, "FUT")],
                 profile={"cash_over_5pct": True, "presumptive_years": ["2022-23"]})
        a = self.report("2025-26")["audit"]
        self.assertEqual(a["limit"], 10000000.0)            # Rs 1 crore when cash is over 5%
        self.assertEqual(a["status"], "audit likely")       # a loss after using s.44AD in the last five years
        self.assertEqual(a["presumptive_recent"], ["2022-23"])


class SetOff(unittest.TestCase):
    def run_(self, spec=0.0, nons=0.0, stg=None, ltg=None, stl=0.0, ltl=0.0, bf=None, y=2025):
        return tax.setoff({"speculative": spec, "non_speculative": nons, "stcg_by_rate": stg or [],
                           "ltcg_by_rate": ltg or [], "stcl": stl, "ltcl": ltl}, bf or [], y)

    def test_speculative_loss_only_against_speculative(self):
        so = self.run_(spec=-5000, nons=8000, stg=[[20.0, 3000]])
        self.assertEqual((so["speculative"], so["non_speculative"]), (0.0, 8000.0))
        self.assertEqual(so["stcg_by_rate"], [[20.0, 3000.0]])
        self.assertEqual(so["carry_forward"], [{"fy": "2025-26", "kind": "speculative", "amount": 5000.0,
                                                "usable_until": "2029-30"}])

    def test_fno_loss_against_intraday_then_capital_gains(self):
        so = self.run_(spec=2000, nons=-10000, stg=[[20.0, 3000]], ltg=[[12.5, 4000]])
        self.assertEqual((so["speculative"], so["stcg_by_rate"], so["ltcg_by_rate"]), (0.0, [], []))
        self.assertEqual(so["carry_forward"][0]["kind"], "non_speculative")
        self.assertEqual(so["carry_forward"][0]["amount"], 1000.0)
        self.assertEqual(so["carry_forward"][0]["usable_until"], "2033-34")

    def test_capital_losses(self):
        so = self.run_(stg=[[20.0, 1000]], ltg=[[12.5, 5000]], stl=3000, ltl=0)
        self.assertEqual((so["stcg_by_rate"], so["ltcg_by_rate"]), ([], [[12.5, 3000.0]]))
        so = self.run_(stg=[[20.0, 1000]], ltl=4000)        # a long-term loss never touches short-term gains
        self.assertEqual(so["stcg_by_rate"], [[20.0, 1000.0]])
        self.assertEqual(so["carry_forward"][0]["kind"], "ltcl")

    def test_brought_forward(self):
        bf = [{"fy": 2021, "kind": "speculative", "amount": 900, "filed_on_time": True},     # lapsed after 2025-26
              {"fy": 2022, "kind": "speculative", "amount": 700, "filed_on_time": True},
              {"fy": 2024, "kind": "non_speculative", "amount": 5000, "filed_on_time": True},
              {"fy": 2024, "kind": "ltcl", "amount": 2000, "filed_on_time": False}]
        so = self.run_(spec=1000, nons=3000, ltg=[[12.5, 2000]], bf=bf, y=2026)
        self.assertEqual([(x["fy"], x["kind"]) for x in so["lapsed"]], [("2021-22", "speculative"),
                                                                       ("2024-25", "ltcl")])
        used = {(u["fy"], u["kind"]): u["used"] for u in so["brought_forward_used"]}
        self.assertEqual(used, {("2022-23", "speculative"): 700.0, ("2024-25", "non_speculative"): 3300.0})
        # the F&O loss of 2024-25 covers F&O profit first, then the speculative profit left
        self.assertEqual((so["speculative"], so["non_speculative"]), (0.0, 0.0))
        self.assertEqual(so["carry_forward"], [{"fy": "2024-25", "kind": "non_speculative", "amount": 1700.0,
                                                "usable_until": "2032-33"}])
        self.assertEqual(so["ltcg_by_rate"], [[12.5, 2000.0]])


class Cli(Base):
    def test_report_rules_years_and_errors(self):
        self.fixture()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(tax.main(["report", "--fy", "2025-26", "--workdir", self.tmp, "--now",
                                       "2026-11-01 10:00"]), 0)
            self.assertEqual(tax.main(["rules", "--fy", "2026-27", "--json"]), 0)
            self.assertEqual(tax.main(["years", "--workdir", self.tmp, "--json"]), 0)
            self.assertEqual(tax.main(["report", "--fy", "2016-17", "--workdir", self.tmp]), 1)
            self.assertEqual(tax.main(["export", "--fy", "2025-26", "--workdir", self.tmp, "--json",
                                       "--now", "2026-11-01 10:00"]), 0)
        text = out.getvalue()
        self.assertIn("Tax ESTIMATE, FY 2025-26", text)
        self.assertIn("not tax advice", text)
        self.assertIn('"years": ["2026-27", "2025-26", "2024-25"]', text)
        self.assertIn("before FY 2018-19", err.getvalue())
        self.assertTrue(os.path.isfile(self.paths.tax("FY2025-26", "report.md")))

    def test_tradebook_without_storing_and_the_paper_book(self):
        os.makedirs(self.paths.trader)
        d = tax.report(self.paths, 2025, "live", [(BOOK, "zerodha")], None, NOW)
        self.assertEqual(d["turnover"]["total"], 25420.5)
        # without the fixture's holdings.csv, SBIN's sale of the 2017 lot has nothing to match
        self.assertTrue(any("SBIN: sold 100" in x for x in d["needs_input"]))
        self.assertFalse(os.path.isdir(self.paths.t("journal", "imports")))
        shutil.rmtree(self.paths.trader)
        shutil.copytree(os.path.join(FIX, "trader-journal", "paper", "trader"), self.paths.trader)
        p = tax.report(self.paths, 2026, "paper", None, None, NOW)
        self.assertTrue(p["notes"][0].startswith("PRACTICE"))
        self.assertGreater(p["heads"]["speculative"]["trades"], 0)
        empty = tax.report(self.paths, 2026, "live", None, None, NOW)
        self.assertTrue(empty["needs_input"][0].startswith("no live fills"))

    def test_summary_view_is_small(self):
        self.fixture()
        d = self.report("2025-26")
        s = tax.summary_view(d)
        self.assertNotIn("trades", s)
        self.assertEqual(s["trade_count"], len(d["trades"]))


if __name__ == "__main__":
    unittest.main()
