"""trader-risk on trader-broker's adjustment tickets (issue #33), offline.

An adjustment (ticket.py adjust: roll a leg, close a leg, add a hedge, close all) is checked together with
the open legs of the same paper strategy in portfolio.json fno_positions, so rolling the covered short of
an iron condor is not mistaken for a naked short.

Fixtures, all REAL NSE files (reused):
  trader-options/chain-NIFTY-03-Nov-2026.json.gz, chain-NIFTY-01-Dec-2026.json.gz
                                  the NIFTY option chains of Fri 30-Oct-2026 15:40
  trader-options/fo_mktlots.csv   NSE lot sizes (NIFTY 65)
  trader-broker/fno/...           the contract master and trader-risk's PASS for the condor's four legs
The condor is opened on the paper account by trader-broker itself (confirm, fill), then adjusted with
ticket.py adjust, exactly as a user would.
"""

import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402
import test_broker_fno as bf  # noqa: E402

check = load("trader-risk", "check")

NOW = "2026-11-02 11:00"
LOTS = check.parse_lots(open(os.path.join(FIX, "trader-options", "fo_mktlots.csv"), encoding="utf-8").read())
DEC = bf.analysis("01-Dec-2026")
RISK_CFG = {"capital": 200000, "allow_fno": True, "max_loss_per_trade": 6000, "max_daily_loss": 20000}


def no_network(url, timeout=30, retries=2):
    raise check.RulesError("no network in the tests")


check.nse_get = no_network


class Adjust(unittest.TestCase):
    """An iron condor on NIFTY 03-Nov-2026, filled on paper at 10:01: legs 1 BUY 22700 PE at 28.70,
    2 SELL 22800 PE at 44.30, 3 SELL 23200 CE at 44.85, 4 BUY 23300 CE at 24.05 (65 each)."""

    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp(prefix="bt-adj-")
        os.chdir(self.dir)
        os.makedirs("trader/state")
        bf.jsave("trader/config.json", bf.Base.CFG)
        bf.jsave("trader/options.json", [bf.NIFTY, DEC])
        doc = bf.condor()
        bf.jsave("trader/strategy.json", doc)
        bf.jsave("trader/orders.json", bf.st.orders_document(doc, created_at="2026-11-02T04:30:00Z"))
        shutil.copy(os.path.join(bf.FNO, "risk-pass-iron-condor.json"), "trader/state/risk.json")
        shutil.copy(os.path.join(bf.FNO, "NSE_FO_contract_30102026-NIFTY.csv"), "trader/bands.csv")
        for argv in (("confirm", "--basket", "--yes-text", "yes", "--now", bf.NOW),
                     ("fill", "--basket", "--now", "2026-11-02 10:01")):
            code, out, err = bf.tk(*argv)
            self.assertEqual(code, 0, out + err)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir, ignore_errors=True)

    def adjust(self, *argv):
        code, out, err = bf.tk("adjust", "--strategy", bf.SID, *(list(argv) + ["--now", NOW]))
        self.assertEqual(code, 0, out + err)
        return bf.jload("trader/orders.json")

    def risk(self, doc, portfolio=None, kill=None, **over):
        c = dict(RISK_CFG, **over)
        pf, extra = check.gather_inputs("trader", doc["orders"], doc, c, "2026-11-02")
        extra.update(lots=LOTS, lots_note="fixture", kill=kill)
        res = check.check(doc["orders"], c, portfolio or pf, {}, None, None, check.parse_now(NOW), {}, extra)
        return res, [r["verdict"] for r in res["results"]], [x for r in res["results"] for x in r["reasons"]]

    def test_the_open_legs_carry_their_strategy_id(self):
        legs = check.fno_positions(bf.jload("trader/portfolio.json"))
        self.assertEqual(sorted((p["contract"]["strike"], p["qty"], p["strategy_id"]) for p in legs),
                         [(22700.0, 65, bf.SID), (22800.0, -65, bf.SID), (23200.0, -65, bf.SID),
                          (23300.0, 65, bf.SID)])

    def test_rolling_the_covered_short_call_passes(self):
        doc = self.adjust("--roll-leg", "3", "--strike", "23250")
        self.assertEqual(doc["adjust"]["strategy_id"], bf.SID)
        res, verdicts, reasons = self.risk(doc)
        self.assertEqual(verdicts, ["PASS", "PASS"], reasons)
        b = res["results"][0]["basket"]
        a = b["adjustment"]
        self.assertEqual((b["kind"], b["defined_risk"], a["naked_after"]), ("adjust", True, []))
        self.assertEqual(a["open_legs_found"], "its 4 open legs in portfolio.json")
        self.assertEqual(sorted((x["label"], x["qty"]) for x in a["after"]),
                         [("NIFTY 22700 PE 2026-11-03", 65), ("NIFTY 22800 PE 2026-11-03", -65),
                          ("NIFTY 23250 CE 2026-11-03", -65), ("NIFTY 23300 CE 2026-11-03", 65)])
        # Worked by hand, from the open legs' last prices (22700 PE 28.675, 22800 PE 44.325, 23300 CE
        # 24.025) and the new short 23250 CE at 33.25. Below 22700 at expiry the put spread loses its
        # 100 points: 65 x (-100 + (44.325 - 28.675) + (33.25 - 24.025)) = 65 x -75.125 = -4,883.125.
        # Buying back 23200 CE at 44.85 costs its charges 24.92 (it is bought at its last price), and
        # selling 23250 CE costs 27.75; the rest is the exercise of the long put at expiry.
        gross = 65 * (Decimal("-100") + Decimal("44.325") - Decimal("28.675") + Decimal("33.25") - Decimal("24.025"))
        self.assertEqual(gross, Decimal("-4883.125"))
        self.assertEqual(a["close_cost"], bf.opt_side(44.85, 65, True))
        self.assertEqual(a["close_cost"], 24.92)
        self.assertEqual(bf.opt_side(33.25, 65, False), 27.75)
        floor = 4883.125 + 24.92 + 27.75
        self.assertGreater(a["max_loss_after"], floor)
        self.assertLess(a["max_loss_after"], floor + 100)
        self.assertLess(a["max_loss_after"], RISK_CFG["max_loss_per_trade"])
        # a narrower call spread (23250/23300) for a smaller credit: more can be lost than before
        self.assertGreater(a["max_loss_after"], a["max_loss_before"])
        self.assertEqual(a["max_loss_added"], round(a["max_loss_after"] - a["max_loss_before"], 2))
        # margin: only the change against the four legs held now
        m = b["margin"]
        self.assertEqual(m["change"], round(m["after"] - m["before"], 2))
        self.assertLess(abs(m["change"]), 2000)
        # Greeks and the scenario grid of the position it leaves, the same as the book shown after it
        self.assertEqual(res["portfolio"]["greeks"], b["greeks_after"])
        self.assertEqual(res["portfolio"]["scenarios"]["worst"], b["worst_scenario_after"])
        self.assertEqual(len(res["portfolio"]["positions"]), 4)
        self.assertIn("roll of %s: checked with its 4 open legs in portfolio.json" % bf.SID,
                      "\n".join(res["results"][0]["info"]))

    def test_without_the_open_legs_the_same_roll_was_blocked_as_naked(self):
        doc = self.adjust("--roll-leg", "3", "--strike", "23250")
        bare = dict(doc, adjust=None, orders=[dict(o, tag=None) for o in doc["orders"]])
        _, verdicts, reasons = self.risk(bare)
        self.assertEqual(verdicts, ["BLOCK", "BLOCK"])
        self.assertTrue(any(r.startswith("naked: the NIFTY basket has short legs") for r in reasons), reasons)

    def test_a_ticket_without_a_strategy_id_is_matched_by_underlying_and_expiry(self):
        doc = self.adjust("--roll-leg", "3", "--strike", "23250")
        doc["adjust"] = dict(doc["adjust"], strategy_id=None)
        res, verdicts, reasons = self.risk(doc)
        self.assertEqual(verdicts, ["PASS", "PASS"], reasons)
        want = ("roll: the ticket carries no strategy id, so it was matched by underlying and expiry "
                "(NIFTY 2026-11-03): 4 open legs")
        self.assertIn(want, res["results"][0]["warnings"])
        # open positions without an id (a portfolio not written by trader-broker): matched the same way
        pf = bf.jload("trader/portfolio.json")
        for p in pf["fno_positions"]:
            p.pop("strategy_id")
        res, verdicts, _ = self.risk(self.adjust("--roll-leg", "3", "--strike", "23250"), portfolio=pf)
        self.assertEqual(verdicts, ["PASS", "PASS"])
        self.assertIn("no open position carries strategy id %s" % bf.SID, "\n".join(res["results"][0]["warnings"]))

    def test_rolling_the_short_past_its_hedge_leaves_it_naked(self):
        # 23200 CE out to the December expiry: the bought 23300 CE expires on 3 November, before it
        doc = self.adjust("--roll-leg", "3", "--expiry", "2026-12-01")
        self.assertEqual([(o["side"], o["strike"], o["expiry"]) for o in doc["orders"]],
                         [("BUY", 23200.0, "2026-11-03"), ("SELL", 23200.0, "2026-12-01")])
        res, verdicts, reasons = self.risk(doc)
        self.assertEqual(verdicts, ["BLOCK", "BLOCK"])
        self.assertIn("naked: after this roll of %s, NIFTY 23200 CE 2026-12-01 would be short with no bought leg "
                      "of the strategy to cover it (a loss without limit); naked shorts are off "
                      "(\"allow_naked_short\": false)" % bf.SID, reasons)
        self.assertEqual(res["results"][0]["basket"]["adjustment"]["naked_after"], ["NIFTY 23200 CE 2026-12-01"])
        # allow_naked_short: no naked block; it still needs a stop or risk rule, and passes with one
        _, verdicts, reasons = self.risk(doc, allow_naked_short=True, max_net_vega=0, max_scenario_loss=0)
        self.assertFalse([r for r in reasons if r.startswith("naked:")])
        self.assertTrue([r for r in reasons if r.startswith("stop: the NIFTY position after this roll")], reasons)
        ruled = dict(doc, orders=[dict(o, risk_rule={"max_loss": 4000}) for o in doc["orders"]])
        _, verdicts, reasons = self.risk(ruled, allow_naked_short=True, max_net_vega=0, max_scenario_loss=0,
                                         margin_use_upper_bound=False, cash_buffer_pct=0)
        self.assertFalse([r for r in reasons if r.startswith(("naked:", "stop:"))], reasons)
        self.assertEqual(verdicts, ["PASS", "PASS"], reasons)

    def test_closing_the_long_call_leaves_the_short_naked(self):
        doc = self.adjust("--close-leg", "4")
        self.assertEqual([(o["side"], o["strike"], o["option_type"]) for o in doc["orders"]], [("SELL", 23300.0, "CE")])
        res, verdicts, reasons = self.risk(doc)
        self.assertEqual(verdicts, ["BLOCK"])
        self.assertEqual(res["results"][0]["kind"], "adjust")
        self.assertIn("naked: after this close of %s, NIFTY 23200 CE 2026-11-03 would be short with no bought leg of "
                      "the strategy to cover it (a loss without limit); naked shorts are off "
                      "(\"allow_naked_short\": false)" % bf.SID, reasons)

    def test_exits_are_never_blocked(self):
        kill = {"active": True, "date": "2026-11-02", "at": "2026-11-02T10:30:00+05:30", "reason": "test"}
        # buying back the short call: what is left (bull put spread + a bought call) is defined risk
        res, verdicts, reasons = self.risk(self.adjust("--close-leg", "3"), kill=kill)
        self.assertEqual((verdicts, res["results"][0]["kind"]), (["PASS"], "exit"))
        self.assertTrue(any(w.startswith("exit, not blocked: kill:") for w in res["results"][0]["warnings"]))
        res, verdicts, _ = self.risk(self.adjust("--close-all"), kill=kill)
        self.assertEqual(verdicts, ["PASS"] * 4)
        self.assertEqual({r["kind"] for r in res["results"]}, {"exit"})
        self.assertEqual(res["results"][0]["basket"]["adjustment"]["after"], [])
        self.assertIsNone(res["portfolio"])          # nothing open after it

    def test_an_added_hedge_is_checked_on_the_whole_position(self):
        doc = self.adjust("--add-hedge", "BUY 1 PE 22600")
        res, verdicts, reasons = self.risk(doc)
        self.assertEqual(verdicts, ["PASS"], reasons)
        a = res["results"][0]["basket"]["adjustment"]
        self.assertEqual(len(a["after"]), 5)
        # the condor still loses most between 22600 and 22700, where the new put is worth nothing: the
        # hedge adds its premium (65 x its price) and a few rupees of charges to the max loss
        cost = 65 * doc["orders"][0]["price"]
        self.assertGreater(a["max_loss_added"], cost)
        self.assertLess(a["max_loss_added"], cost + 60)


if __name__ == "__main__":
    unittest.main()
