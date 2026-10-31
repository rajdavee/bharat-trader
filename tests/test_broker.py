"""trader-broker tests, offline: tickets, the typed yes, and the paper broker end to end.

Fixtures, cut from real Yahoo data fetched 2026-10-29:
  trader-risk/orders-reliance.json    trader-budget's RELIANCE order of 2026-10-29 (42 shares, LIMIT 1174.30)
  trader-risk/prices-reliance.json    RELIANCE 1177.30 at 10:20 IST
  trader-broker/prices-1510.json      RELIANCE 1168.80 at 15:10 IST (the 15:05 bar's close), the flat-by time
  trader-broker/portfolio-open.json   42 RELIANCE (MIS) and 5 TCS (CNC) held
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, ROOT, FIX  # noqa: E402

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
PRICES_1020 = os.path.join(FIX, "trader-risk", "prices-reliance.json")
PRICES_1510 = os.path.join(FIX, "trader-broker", "prices-1510.json")
OPEN_PF = os.path.join(FIX, "trader-broker", "portfolio-open.json")
NEUTRAL = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
           "order_type": "LIMIT", "price": 1174.30,
           "stop_loss": {"order_type": "SL-M", "trigger_price": 1163.30},
           "target": {"order_type": "LIMIT", "price": 1196.30}, "validity": "DAY"}


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def tk(*argv):
    return run(ticket, list(argv))


def risk(now):
    return run(check, ["--now", now])


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=("RELIANCE", "TCS", "SBIN")):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 42 RELIANCE",
                  "yes 1174.30", "yes 42 at 1,174.30", "yes target 1196.30", "yes max loss 514.00",
                  "yes ₹49,320.60"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "haan", "yeah", "yep", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes 43", "yes 42 at 1180", "yes but make it 40", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 1170", "yes, stop 1163.30"):
            self.assertFalse(self.yes(r), r)


class TicketShape(unittest.TestCase):
    def test_the_neutral_order_leaves_out_the_notes(self):
        self.assertEqual(ticket.neutral(rel()), NEUTRAL)
        self.assertEqual(ticket.fingerprint(rel()), check.fingerprint(rel()))

    def test_plain_words(self):
        parts = ticket.plain_parts(rel(), jload(CFG_PATH))
        self.assertEqual(parts[0], "BUY 42 RELIANCE on NSE, MIS (intraday), LIMIT 1174.30, good for today")
        self.assertEqual(parts[1], "then a stop-loss: SL-M trigger 1163.30")
        self.assertIn("LIMIT 1196.30", parts[2])
        self.assertEqual(parts[3], "order value about ₹49,320.60")
        self.assertIn("₹462.00 + ₹52.00 charges = ₹514.00", parts[4])

    def test_side_charges_add_up_to_the_round_trip(self):
        c = jload(CFG_PATH)
        # buy 42 at 1174.30 = 49,320.60: brokerage 14.80, exchange 1.46, SEBI 0.05, stamp 1.48, GST 2.94
        buy = ticket.side_charges("BUY", 1174.30, 42, c, "MIS")
        self.assertEqual((buy["brokerage"], buy["stt"], buy["exchange"], buy["sebi"], buy["stamp"], buy["gst"],
                          buy["total"]), (14.80, 0.0, 1.46, 0.05, 1.48, 2.94, 20.73))
        sell = ticket.side_charges("SELL", 1163.30, 42, c, "MIS")
        whole = ticket.round_trip_charges(1174.30, 1163.30, 42, c)["total"]
        self.assertEqual(whole, 52.00)
        self.assertLessEqual(abs(buy["total"] + sell["total"] - whole), 0.02)


class PaperFlow(unittest.TestCase):
    """budget's order -> risk -> show -> confirm -> fill -> flatten -> risk -> confirm -> fill -> status."""

    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)
        os.makedirs("trader")
        shutil.copy(CFG_PATH, "trader/config.json")
        shutil.copy(ORDERS_PATH, "trader/orders.json")
        with open("trader/portfolio.json", "w") as fh:
            json.dump({"cash": 100000, "positions": {}}, fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir)

    def entry(self):
        self.assertEqual(risk("2026-10-29 09:40")[0], 0)
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes 42 RELIANCE", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 0, err)
        code, text, err = tk("fill", "--index", "1", "--price", "1174.30", "--now", "2026-10-29 09:42")
        self.assertEqual(code, 0, err)
        return text

    def test_not_ready_until_risk_passes_this_exact_order(self):
        code, text, _ = tk("show", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked", text)
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        risk("2026-10-29 09:40")
        code, text, _ = tk("show", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS, waiting for your yes", text)
        self.assertIn("PAPER account", text)
        self.assertIn('"trigger_price": 1163.3', text)
        self.assertNotIn('"why"', text.split("The broker-neutral order:")[1])
        # tomorrow the same verdict does not count
        self.assertIn("not today", tk("show", "--now", "2026-10-30 09:41")[1])
        # an edited order is a different order
        doc = jload("trader/orders.json")
        doc["orders"][0]["qty"] = 41
        with open("trader/orders.json", "w") as fh:
            json.dump(doc, fh)
        self.assertIn("was not in the last risk check", tk("show", "--now", "2026-10-29 09:41")[1])

    def test_blocked_is_never_ready(self):
        risk("2026-10-29 09:20")           # before the 09:30 window
        code, text, _ = tk("show", "--now", "2026-10-29 09:21", "--json")
        t = json.loads(text)[0]
        self.assertFalse(t["ready"])
        self.assertIn("BLOCKED by trader-risk: window", t["status"])
        self.assertEqual(t["order"], NEUTRAL)
        self.assertEqual(t["max_loss"], 514.00)

    def test_confirm_records_the_yes_verbatim_once(self):
        risk("2026-10-29 09:40")
        code, text, _ = tk("confirm", "--index", "1", "--yes-text", "sure", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists("trader/state/broker.json"))
        code, text, _ = tk("confirm", "--index", "1", "--yes-text", "Yes, buy 42 RELIANCE", "--now",
                           "2026-10-29 09:41")
        self.assertEqual(code, 0)
        self.assertIn('"Yes, buy 42 RELIANCE"', text)
        rec = jload("trader/state/broker.json")["orders"][0]
        self.assertEqual((rec["status"], rec["yes_text"], rec["kind"], rec["max_loss"], rec["broker"]),
                         ("placed", "Yes, buy 42 RELIANCE", "entry", 514.00, "paper"))
        self.assertEqual(rec["order"], NEUTRAL)
        self.assertEqual(rec["at"], "2026-10-29T09:41:00+05:30")
        self.assertIn("DONE: placed", tk("show", "--now", "2026-10-29 09:42")[1])
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:42")
        self.assertEqual(code, 1)
        # trader-risk now sees it: the same order again is a duplicate, its 514.00 counts in the day
        code, text, _ = risk("2026-10-29 09:45")
        self.assertEqual(code, 1)
        self.assertIn("already placed today", text)
        self.assertIn("₹514.00 of ₹3,000.00 daily loss", text)

    def test_decline(self):
        risk("2026-10-29 09:40")
        code, text, _ = tk("decline", "--index", "1", "--now", "2026-10-29 09:41")
        self.assertIn("Nothing was sent", text)
        self.assertIn("DONE: declined", tk("show", "--now", "2026-10-29 09:42")[1])

    def test_a_live_broker_name_with_paper_true_is_refused(self):
        risk("2026-10-29 09:40")
        c = jload("trader/config.json")
        c["broker"] = "zerodha"
        with open("trader/config.json", "w") as fh:
            json.dump(c, fh)
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:41")
        self.assertEqual(code, 1)
        self.assertIn("the paper broker only works with \"broker\": \"paper\"", err)
        self.assertFalse(os.path.exists("trader/state/broker.json"))

    def test_the_entry_fill_applies_charges(self):
        text = self.entry()
        self.assertIn("Charges about ₹20.73. Cash now ₹50,658.67", text)
        self.assertIn("SL-M stop at 1163.30 and the LIMIT target at 1196.30 are now working", text)
        pf = jload("trader/portfolio.json")
        self.assertEqual(pf["cash"], 50658.67)         # 1,00,000 - 49,320.60 - 20.73
        self.assertEqual(pf["positions"]["RELIANCE"],
                         {"qty": 42, "avg_price": 1174.30, "product": "MIS", "buy_charges": 20.73})
        trades = jload("trader/state/trades.json")["trades"]
        self.assertEqual((trades[0]["side"], trades[0]["qty"], trades[0]["value"], trades[0]["charges"]),
                         ("BUY", 42, 49320.60, 20.73))
        code, _, err = tk("fill", "--index", "1", "--price", "1174.30", "--now", "2026-10-29 09:43")
        self.assertIn("already filled", err)

    def test_a_limit_cannot_fill_above_its_price(self):
        risk("2026-10-29 09:40")
        tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:41")
        code, _, err = tk("fill", "--index", "1", "--price", "1175.00", "--now", "2026-10-29 09:42")
        self.assertIn("cannot fill at 1175.00", err)
        # the fresh 10:20 price 1177.30 is above the limit too
        shutil.copy(PRICES_1020, "trader/prices.json")
        code, _, err = tk("fill", "--index", "1", "--now", "2026-10-29 10:25")
        self.assertIn("cannot fill at 1177.30", err)
        # and with no price at all
        os.remove("trader/prices.json")
        self.assertIn("no fill price", tk("fill", "--index", "1", "--now", "2026-10-29 10:25")[2])

    def test_the_stop_fills_and_cancels_the_target(self):
        self.entry()
        code, text, err = tk("fill", "--index", "1", "--leg", "stop", "--now", "2026-10-29 10:40")
        self.assertEqual(code, 0, err)
        sell = ticket.side_charges("SELL", 1163.30, 42, jload(CFG_PATH), "MIS")["total"]
        realised = round(-462.00 - 20.73 - sell, 2)
        self.assertIn("SELL 42 RELIANCE at 1163.30", text)
        self.assertIn(ticket.inr(realised), text)
        self.assertIn("Cancelled the target", text)
        self.assertLessEqual(abs(-realised - 514.00), 0.02)     # what trader-budget and trader-risk said
        code, _, err = tk("fill", "--index", "1", "--leg", "target", "--now", "2026-10-29 11:00")
        self.assertIn("closed already", err)
        self.assertEqual(jload("trader/portfolio.json")["positions"], {})

    def test_flatten_by_flat_by_then_out(self):
        self.entry()
        code, text, err = tk("flatten", "--now", "2026-10-29 15:10")
        self.assertEqual(code, 0, err)
        self.assertIn("Flatten: SELL 42 RELIANCE on NSE, MIS (intraday), MARKET", text)
        self.assertIn("past your flat_by 15:10", text)
        doc = jload("trader/orders.json")
        self.assertEqual(doc["created_by"], "trader-broker")
        self.assertEqual(ticket.neutral(doc["orders"][0]),
                         {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 42,
                          "order_type": "MARKET", "price": None, "validity": "DAY"})
        shutil.copy(PRICES_1510, "trader/prices.json")
        code, text, _ = risk("2026-10-29 15:10")         # after the 14:45 entry cutoff: an exit still passes
        self.assertEqual(code, 0, text)
        self.assertIn("PASS  #1 SELL 42 RELIANCE (MIS), exit", text)
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 15:10")
        self.assertEqual(code, 0, err)
        code, text, err = tk("fill", "--index", "1", "--now", "2026-10-29 15:11")   # the fresh 15:10 price
        self.assertEqual(code, 0, err)
        # sell 42 at 1168.80 = 49,089.60: charges 31.43; realised -231.00 - 20.73 - 31.43 = -283.16
        self.assertIn("SELL 42 RELIANCE at 1168.80 (the fresh price in trader/prices.json)", text)
        self.assertIn("Charges about ₹31.43", text)
        self.assertIn("-₹283.16", text)
        self.assertIn("Cancelled the stop and target of RELIANCE", text)
        pf = jload("trader/portfolio.json")
        self.assertEqual(pf, {"cash": 99716.84, "positions": {}})   # 1,00,000 - 283.16
        code, text, _ = tk("status", "--now", "2026-10-29 15:12")
        self.assertIn("Cash ₹99,716.84", text)
        self.assertIn("No open positions.", text)
        self.assertIn("Today: 2 of 10 orders, 1 of 3 trades, ₹514.00 of ₹3,000.00 daily loss", text)
        self.assertIn("Realised today -₹283.16", text)
        self.assertIn("charges paid today ₹52.16", text)
        self.assertEqual(len(jload("trader/state/trades.json")["trades"]), 2)
        # nothing left to flatten
        self.assertIn("nothing to flatten", tk("flatten", "--now", "2026-10-29 15:13")[2])

    def test_flatten_keeps_delivery_holdings(self):
        shutil.copy(OPEN_PF, "trader/portfolio.json")
        code, text, _ = tk("flatten", "--now", "2026-10-29 15:05")
        self.assertEqual(code, 0)
        self.assertIn("Not flattened: TCS 5 (CNC, delivery: kept)", text)
        self.assertNotIn("past your flat_by", text)
        self.assertEqual([o["symbol"] for o in jload("trader/orders.json")["orders"]], ["RELIANCE"])
        tk("flatten", "--now", "2026-10-29 15:05", "--include-cnc")
        self.assertEqual([o["symbol"] for o in jload("trader/orders.json")["orders"]], ["RELIANCE", "TCS"])
        code, text, _ = tk("status", "--now", "2026-10-29 15:15")
        self.assertIn("Past flat_by 15:10 with MIS open (RELIANCE)", text)

    def test_a_manual_fill(self):
        code, text, err = tk("fill", "--symbol", "sbin", "--side", "buy", "--qty", "10", "--price", "971.90",
                             "--now", "2026-10-29 10:00")
        self.assertEqual(code, 0, err)
        self.assertEqual(jload("trader/portfolio.json")["positions"]["SBIN"]["qty"], 10)
        code, _, err = tk("fill", "--symbol", "SBIN", "--side", "SELL", "--qty", "11", "--price", "975",
                          "--now", "2026-10-29 10:05")
        self.assertIn("holds 10", err)

    def test_inr(self):
        self.assertEqual(ticket.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(ticket.inr(-283.16), "-₹283.16")


if __name__ == "__main__":
    unittest.main()
