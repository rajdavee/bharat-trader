"""trader-broker phase 2, offline: native linked exits (Kite GTT two-leg OCO, Upstox multi-leg GTT, Dhan Super
Order) through the same gates as every live order, reconciliation, the Angel One adapter and its TOTP login, and
the broker basket-margin cross-check in trader-strategy and trader-risk.

The request shapes are checked against the examples in each broker's docs (fixtures under
tests/fixtures/trader-broker/<broker>/, all marked doc_derived). The stub broker is the one in
test_broker_adapters.py: a 127.0.0.1 HTTP server, so nothing here touches the network.
"""

import base64
import copy
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402
from test_broker_adapters import (BFIX, CFG_PATH, ENV, NOW, ORDERS_PATH, TODAY, TOKENS, Stub, adapter,  # noqa: E402
                                  jload, open_docs, run, ticket_order)

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
strategy = load("trader-strategy", "strategy")
pkg = ticket.adapters()
base, auth = pkg.base, pkg.auth
BROKERS = ("kite", "upstox", "dhan", "angel")


def kite_ticket(**over):
    """budget's RELIANCE ticket, with an SL stop that has a limit price (Kite's GTT legs are LIMIT orders)."""
    t = ticket_order()
    t["stop_loss"] = {"order_type": "SL", "trigger_price": 1163.3, "price": 1162.0}
    t.update(over)
    return t


class StubBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = Stub()

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def setUp(self):
        self.stub.hits[:] = []
        self.stub.routes.clear()
        for b in BROKERS:
            self.stub.load(b)


# ---------------------------------------------------------------- request shapes, from the documented examples

class LinkedShapes(StubBase):
    def linked(self, b, ticket_, leg):
        ad = adapter(self.stub, b)
        lk = base.leg_order(ticket_, leg)
        return ad, lk, ad.resolve(lk["symbol"], lk["exchange"])

    def test_the_two_linked_shapes(self):
        t = kite_ticket()
        oco = base.leg_order(t, "oco")
        self.assertEqual(oco, {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 42,
                               "stop": {"order_type": "SL", "trigger_price": 1163.3, "price": 1162.0},
                               "target": {"price": 1196.3}, "linked": "oco"})
        br = base.leg_order(ticket_order(), "bracket")
        self.assertEqual((br["side"], br["order_type"], br["price"], br["stop"], br["target"], br["linked"]),
                         ("BUY", "LIMIT", 1174.3, {"order_type": "SL-M", "trigger_price": 1163.3},
                          {"price": 1196.3}, "bracket"))
        self.assertNotIn("why", br)
        no_target = dict(ticket_order())
        no_target.pop("target")
        with self.assertRaises(base.InvalidOrder):
            base.leg_order(no_target, "oco")

    def test_kite_gtt_two_leg_oco(self):
        ad, lk, inst = self.linked("kite", kite_ticket(), "oco")
        m, host, path, p = ad.linked_request(lk, inst, last_price=1174.3)
        self.assertEqual((m, host, path), ("POST", "api", "/gtt/triggers"))
        f = p["form"]
        self.assertEqual(f["type"], "two-leg")
        self.assertEqual(json.loads(f["condition"]), {"exchange": "NSE", "tradingsymbol": "RELIANCE",
                                                      "trigger_values": [1163.3, 1196.3], "last_price": 1174.3})
        self.assertEqual(json.loads(f["orders"]), [
            {"exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL", "quantity": 42,
             "order_type": "LIMIT", "product": "MIS", "price": 1162.0},
            {"exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL", "quantity": 42,
             "order_type": "LIMIT", "product": "MIS", "price": 1196.3}])
        # a short position: the target is the lower trigger, the stop the upper
        short = kite_ticket(side="SELL", price=1174.3, stop_loss={"order_type": "SL", "trigger_price": 1185.0,
                                                                  "price": 1186.0}, target={"price": 1150.0})
        ad, lk, inst = self.linked("kite", short, "oco")
        f = ad.linked_request(lk, inst, last_price=1170.0)[3]["form"]
        self.assertEqual(json.loads(f["condition"])["trigger_values"], [1150.0, 1185.0])
        self.assertEqual([o["price"] for o in json.loads(f["orders"])], [1150.0, 1186.0])
        self.assertEqual({o["transaction_type"] for o in json.loads(f["orders"])}, {"BUY"})
        # an SL-M stop has no limit price for a GTT leg; a crossed trigger would fire at once
        ad, lk, inst = self.linked("kite", ticket_order(), "oco")
        with self.assertRaises(base.InvalidOrder) as cm:
            ad.linked_request(lk, inst, 1174.3)
        self.assertIn("GTT legs are LIMIT orders", cm.exception.message)
        ad, lk, inst = self.linked("kite", kite_ticket(), "oco")
        with self.assertRaises(base.InvalidOrder) as cm:
            ad.linked_request(lk, inst, 1160.0)
        self.assertIn("not between the two triggers", cm.exception.message)
        self.assertEqual(ad.linked_cancel_request("123"), ("DELETE", "api", "/gtt/triggers/123", {}))
        mod = ad.linked_modify_requests("123", lk, inst, None, 1174.3)
        self.assertEqual([(x[0], x[2]) for x in mod], [("PUT", "/gtt/triggers/123")])

    def test_upstox_multi_leg_gtt(self):
        ad, lk, inst = self.linked("upstox", ticket_order(), "bracket")
        m, host, path, p = ad.linked_request(lk, inst)
        self.assertEqual((m, host, path), ("POST", "api", "/v3/order/gtt/place"))
        # the documented multi-leg example's shape: ENTRY, TARGET, STOPLOSS; IMMEDIATE is a limit order sent now
        self.assertEqual(p["json_body"], {
            "type": "MULTIPLE", "quantity": 42, "product": "I", "instrument_token": "NSE_EQ|INE002A01018",
            "transaction_type": "BUY", "rules": [
                {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 1174.3},
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 1196.3},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 1163.3}]})
        doc = jload(os.path.join(BFIX, "upstox", "gtt_place.json"))["response"]["body"]
        self.assertEqual(ad.linked_placed_id(doc), "GTT-CU25280200021013")
        self.assertEqual(ad.linked_cancel_request("GTT-CU25280200021013"),
                         ("DELETE", "api", "/v3/order/gtt/cancel", {"json_body": {"gtt_order_id":
                                                                                  "GTT-CU25280200021013"}}))
        rec = {"order": lk}
        new = base.leg_order(kite_ticket(target={"price": 1200.0}), "oco")
        (m, host, path, p), = ad.linked_modify_requests("GTT-CU25280200021013", new, inst, rec)
        self.assertEqual((m, path, p["json_body"]["gtt_order_id"], p["json_body"]["type"]),
                         ("PUT", "/v3/order/gtt/modify", "GTT-CU25280200021013", "MULTIPLE"))
        self.assertEqual([r["trigger_price"] for r in p["json_body"]["rules"]], [1174.3, 1200.0, 1163.3])
        with self.assertRaises(base.InvalidOrder):
            ad.linked_modify_requests("GTT-1", new, inst, {})       # no record of the entry price: refused

    def test_dhan_super_order(self):
        ad, lk, inst = self.linked("dhan", dict(ticket_order(), tag="bt0123456789abcdeb"), "bracket")
        m, host, path, p = ad.linked_request(dict(lk, tag="bt0123456789abcdeb"), inst)
        self.assertEqual((m, host, path), ("POST", "api", "/super/orders"))
        # the documented request structure, field for field
        self.assertEqual(p["json_body"], {
            "dhanClientId": "1000000009", "correlationId": "bt0123456789abcdeb", "transactionType": "BUY",
            "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT", "securityId": "2885",
            "quantity": 42, "price": 1174.3, "targetPrice": 1196.3, "stopLossPrice": 1163.3, "trailingJump": 0})
        self.assertEqual(ad.linked_cancel_request("112111182198"),
                         ("DELETE", "api", "/super/orders/112111182198/ENTRY_LEG", {}))
        new = base.leg_order(kite_ticket(target={"price": 1200.0}), "oco")
        mods = ad.linked_modify_requests("112111182198", new, inst)
        self.assertEqual([x[3]["json_body"] for x in mods], [
            {"dhanClientId": "1000000009", "orderId": "112111182198", "legName": "TARGET_LEG", "targetPrice": 1200.0},
            {"dhanClientId": "1000000009", "orderId": "112111182198", "legName": "STOP_LOSS_LEG",
             "stopLossPrice": 1163.3, "trailingJump": 0}])

    def test_angel_has_no_native_linked_order(self):
        ad, lk, inst = self.linked("angel", kite_ticket(), "oco")
        self.assertFalse(ad.caps()["native_oco"])
        with self.assertRaises(base.Unsupported) as cm:
            ad.linked_request(lk, inst)
        self.assertIn("single-leg", cm.exception.message)

    def test_linked_shapes_are_validated_before_any_io(self):
        ad = adapter(self.stub, "dhan")
        inst = ad.resolve("RELIANCE")
        good = base.leg_order(ticket_order(), "bracket")
        self.assertEqual(base.validate_linked(good, inst), [])
        cases = [(dict(good, stop={"order_type": "SL-M", "trigger_price": 1200.0}), "must be below the target"),
                 (dict(good, price=1199.0), "must sit between"),
                 (dict(good, order_type="MARKET", price=None), "a bracket's entry is a LIMIT"),
                 (dict(good, target={"price": 1196.33}), "not on the 0.10 tick"),
                 (dict(good, qty=0), "above 0")]
        for lk, words in cases:
            with self.subTest(words=words):
                self.assertIn(words, " ".join(base.validate_linked(lk, inst)))

    def test_linked_books_map_to_one_shape(self):
        kite = adapter(self.stub, "kite").linked_book()
        self.assertEqual([(g["linked_id"], g["style"], g["status"]) for g in kite],
                         [("112127", "single", "active"), ("105099", "exit_oco", "triggered")])
        oco = kite[1]
        self.assertEqual((oco["side"], oco["qty"], oco["stop_trigger"], oco["target_price"]), ("BUY", 1, 102.0, 103.7))
        self.assertEqual(oco["legs"][1], {"leg": "target", "status": "failed", "order_id": None})
        up = adapter(self.stub, "upstox").linked_book()
        self.assertEqual((up[0]["linked_id"], up[0]["style"], up[0]["status"], up[0]["entry_price"],
                          up[0]["stop_trigger"], up[0]["target_price"], up[0]["time"]),
                         ("GTT-CU25280200021010", "bracket", "rejected", 7.7, 7.6, 7.64, "2025-02-28T11:41:40+05:30"))
        dh = adapter(self.stub, "dhan").linked_book()
        self.assertEqual((dh[0]["linked_id"], dh[0]["status"], dh[0]["entry_price"], dh[0]["stop_trigger"],
                          dh[0]["target_price"], dh[0]["exchange"], dh[0]["product"]),
                         ("5925022734212", "active", 1500.0, 1400.0, 1550.0, "NSE", "CNC"))
        keys = {"linked_id", "style", "symbol", "exchange", "product", "side", "qty", "entry_price", "stop_trigger",
                "stop_price", "target_price", "status", "legs", "tag", "time", "raw_status"}
        for row in kite + up + dh:
            self.assertTrue(keys <= set(row), set(row) ^ keys)


# ---------------------------------------------------------------- the gates, for linked orders

class LinkedGates(StubBase):
    def gate(self, t=None, leg="oco", state=None, broker="kite", token="kitetok33", **over):
        t = t or kite_ticket()
        _, cfg, _, st, pre = open_docs(broker, token)
        fp = base.order_fingerprint(t)
        risk = {"date": TODAY, "results": [{"fingerprint": fp, "verdict": "PASS"}]}
        st = {"orders": [dict(st["orders"][0], fingerprint=fp)] + list((state or {}).get("orders", []))}
        args = dict(broker=broker, cfg=cfg, ticket=t, risk_doc=risk, state=st, preflight_doc=pre,
                    current_token=token, today=TODAY, now=NOW, leg=leg)
        args.update(over)
        return base.open_gate(**args)

    def closed(self, gate, **kw):
        with self.assertRaises(base.GateClosed) as cm:
            self.gate(**kw)
        self.assertIn(gate, [g for g, _ in cm.exception.failed], cm.exception.failed)
        return " ".join(w for _, w in cm.exception.failed)

    def sent(self, leg, status, oid="1", t=None):
        fp = base.order_fingerprint(t or kite_ticket())
        return {"date": TODAY, "fingerprint": fp, "broker": "kite", "live": True, "leg": leg, "status": status,
                "broker_order_id": oid}

    def test_every_gate_applies_to_a_linked_order(self):
        t = kite_ticket()
        p = self.gate()
        self.assertEqual((p.leg, p.order_fp), ("oco", base.order_fingerprint(base.leg_order(t, "oco"))))
        self.assertTrue(p.tag.endswith("o") and len(p.tag) <= 20 and p.tag.isalnum())
        _, cfg, _, _, pre = open_docs()
        self.closed("paper", cfg=dict(cfg, paper=True))
        self.closed("live_enabled", cfg=dict(cfg, live_enabled={}))
        self.closed("risk", risk_doc={"date": TODAY, "results": []})
        self.closed("preflight", preflight_doc={})
        self.closed("preflight", current_token="anothertok")
        self.closed("yes", state=None, ticket=dict(t, qty=41))       # another ticket: no risk PASS, no yes

    def test_the_autopilot_yes_never_opens_a_live_gate(self):
        t = kite_ticket()
        fp = base.order_fingerprint(t)
        _, cfg, _, _, pre = open_docs()
        risk = {"date": TODAY, "results": [{"fingerprint": fp, "verdict": "PASS"}]}
        st = {"orders": [{"date": TODAY, "fingerprint": fp, "status": "approved", "broker": "kite",
                          "yes_text": "yes 42 RELIANCE", "actor": "autopilot"}]}
        for leg in ("entry", "oco", "bracket"):
            with self.subTest(leg=leg):
                with self.assertRaises(base.GateClosed) as cm:
                    base.open_gate("kite", cfg, t, risk, st, pre, "kitetok33", TODAY, NOW, leg=leg)
                why = dict(cm.exception.failed)["yes"]
                self.assertIn("recorded by autopilot", why)

    def test_the_yes_covers_its_own_linked_exit_only(self):
        ad = adapter(self.stub, "kite")
        t = kite_ticket()
        mine = base.leg_order(t, "oco")
        p = self.gate()
        other = base.leg_order(kite_ticket(qty=43), "oco")                # another ticket's exits
        with self.assertRaises(base.GateClosed):
            ad.place_linked(other, p, 1174.3)
        with self.assertRaises(base.GateClosed):
            ad.place_order(base.leg_order(t, "stop"), p)                   # a linked pass sends no plain order
        with self.assertRaises(base.GateClosed):
            ad.place_linked(mine, self.gate(leg="entry"), 1174.3)          # an entry pass sends no linked order
        with self.assertRaises(base.GateClosed):
            ad.place_linked(dict(mine, target={"price": 1210.0}), p, 1174.3)   # edited after the PASS
        self.assertEqual(self.stub.calls("POST", "/kite/gtt"), [])
        p = self.gate()
        self.assertEqual(ad.place_linked(mine, p, 1174.3), "123")
        with self.assertRaises(base.GateClosed):
            ad.place_linked(mine, p, 1174.3)                               # single use
        self.assertEqual(len(self.stub.calls("POST", "/kite/gtt/triggers")), 1)
        # one ticket, one style of exits: never both, never twice
        self.assertIn("already sent", self.closed("yes", state={"orders": [self.sent("oco", "linked_sent")]}))
        self.assertIn("already at kite as one linked order",
                      self.closed("leg", leg="stop", state={"orders": [self.sent("oco", "linked_sent")]}))
        self.assertIn("leg-by-leg exit of this ticket is working",
                      self.closed("leg", state={"orders": [self.sent("stop", "leg_sent", "9")]}))
        cancelled = {"orders": [self.sent("stop", "leg_sent", "9"), self.sent("stop", "cancel_sent", "9")]}
        self.assertEqual(self.gate(state=cancelled).leg, "oco")             # after the cancel, the OCO may go
        self.assertIn("a bracket would be a second entry",
                      self.closed("leg", leg="bracket", state={"orders": [self.sent("entry", "placed")]}))
        self.assertIn("nothing more to send",
                      self.closed("leg", leg="entry", state={"orders": [self.sent("bracket", "placed")]}))
        self.assertIn("already sent", self.closed("yes", state={"orders": [self.sent("oco", "unknown")]}))

    def test_a_change_needs_a_new_ticket_with_its_own_pass_and_yes(self):
        t, new = kite_ticket(), kite_ticket(target={"price": 1200.0})
        _, cfg, _, _, pre = open_docs()
        rec = dict(self.sent("oco", "linked_sent", "123"), ticket=ticket.neutral(t), order=base.leg_order(t, "oco"))
        fp_new = base.order_fingerprint(new)
        risk = {"date": TODAY, "results": [{"fingerprint": fp_new, "verdict": "PASS"}]}
        yes = {"date": TODAY, "fingerprint": fp_new, "status": "approved", "broker": "kite", "yes_text": "yes"}

        def gate(new_=new, rec_=rec, risk_=risk, extra=()):
            return base.open_modify_gate("kite", cfg, new_, rec_, risk_, {"orders": [yes] + list(extra)}, pre,
                                         "kitetok33", TODAY, NOW)
        p = gate()
        self.assertEqual((p.modify, p.leg, p.order_fp), ("123", "oco", base.order_fingerprint(
            base.leg_order(new, "oco"))))
        for kw, word in ((dict(risk_={"date": TODAY, "results": []}), "risk"),
                         (dict(new_=kite_ticket(qty=43, target={"price": 1200.0})), "order"),
                         (dict(new_=t), "yes"),
                         (dict(rec_=None), "order"),
                         (dict(extra=[dict(yes, status="modify_sent", live=True)]), "yes")):
            with self.subTest(kw=list(kw)):
                with self.assertRaises(base.GateClosed) as cm:
                    gate(**kw)
                self.assertIn(word, [g for g, _ in cm.exception.failed])
        ad = adapter(self.stub, "kite")
        with self.assertRaises(base.GateClosed):
            ad.place_linked(base.leg_order(new, "oco"), gate(), 1174.3)     # a change pass places nothing new
        with self.assertRaises(base.GateClosed):
            ad.modify_linked("124", base.leg_order(new, "oco"), gate(), rec, 1174.3)   # another linked order
        self.assertTrue(ad.modify_linked("123", base.leg_order(new, "oco"), gate(), rec, 1174.3))
        self.assertEqual(len(self.stub.calls("PUT", "/kite/gtt/triggers/123")), 1)


# ---------------------------------------------------------------- ticket.py end to end against the stub

class LinkedFlow(StubBase):
    """confirm (the yes), then place / cancel / modify-exits / reconcile for each broker's native linked order."""

    def setUp(self):
        StubBase.setUp(self)
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)
        os.makedirs("trader/state")
        ticket.ADAPTER_KW.clear()
        for b in BROKERS:
            ticket.ADAPTER_KW[b] = {"hosts": self.stub.hosts(b), "env": dict(ENV), "sleep": lambda s: None}

    def tearDown(self):
        ticket.ADAPTER_KW.clear()
        os.chdir(self.old)
        shutil.rmtree(self.dir)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def tk(self, *argv):
        return run(ticket, list(argv) + (["--now", "2026-10-29 09:50"] if "--now" not in argv else []))

    def ready(self, broker, tickets, token):
        cfg = jload(CFG_PATH)
        cfg.update(broker=broker, paper=False, live_enabled={broker: True}, static_ip="203.0.113.7")
        self.write("trader/config.json", cfg)
        self.write("trader/orders.json", {"orders": tickets})
        self.write("trader/state/risk.json", {"date": TODAY, "results": [
            {"fingerprint": base.order_fingerprint(t), "verdict": "PASS"} for t in tickets]})
        self.write("trader/state/preflight.json", {broker: {
            "date": TODAY, "at": "2026-10-29T09:30:00+05:30", "passed": True, "public_ip": "203.0.113.7",
            "token_hash": base.token_hash(token)}})
        code, out, err = self.tk("confirm", "--index", "1", "--yes-text", "yes 42 RELIANCE", "--now",
                                 "2026-10-29 09:41")
        self.assertEqual(code, 0, out + err)

    def state(self):
        return jload("trader/state/broker.json")["orders"]

    def test_kite_entry_then_one_gtt_oco_after_the_fill(self):
        self.ready("kite", [kite_ticket()], "kitetok33")
        code, out, err = self.tk("place", "--index", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("send both as one linked order: ticket.py place --index 1 --leg oco", out)
        code, _, err = self.tk("place", "--index", "1", "--leg", "oco")
        self.assertIn("not in the order book", err)                         # not before the fill
        self.assertEqual(self.stub.calls("POST", "/kite/gtt"), [])
        book = jload(os.path.join(BFIX, "kite", "orders.json"))["response"]["body"]
        book["data"][0].update(order_id="151220000000000", status="COMPLETE", filled_quantity=42, average_price=1174.3)
        self.stub.add("GET", "/kite/orders", body=book)
        code, out, err = self.tk("place", "--index", "1", "--leg", "oco")
        self.assertEqual(code, 0, err)
        self.assertIn("SENT to Zerodha Kite (real money): the stop and the target as ONE linked order", out)
        self.assertIn("Zerodha Kite id 123", out)
        form = dict(urllib.parse.parse_qsl(self.stub.calls("POST", "/kite/gtt/triggers")[0]["body"].decode()))
        self.assertEqual(json.loads(form["condition"])["trigger_values"], [1163.3, 1196.3])
        self.assertEqual(json.loads(form["condition"])["last_price"], 1174.3)       # the fill price
        rec = self.state()[-1]
        self.assertEqual((rec["leg"], rec["status"], rec["broker_order_id"], rec["linked"]),
                         ("oco", "linked_sent", "123", "exit_oco"))
        for leg, words in (("oco", "already sent"), ("stop", "already at kite as one linked order")):
            code, _, err = self.tk("place", "--index", "1", "--leg", leg)
            self.assertEqual(code, 1)
            self.assertIn(words, err)
        self.assertEqual(len(self.stub.calls("POST", "/kite/gtt/triggers")), 1)
        # after a restart: the broker's GTT book against this tool's records (read only at the broker)
        n = len(self.stub.hits)
        code, out, err = self.tk("reconcile", "--now", "2026-10-29 10:30")
        self.assertEqual(code, 0, err)
        self.assertIn("123 oco RELIANCE: here linked_sent", out)
        self.assertIn("not in the broker's book", out)
        self.assertIn("105099 RAIN at Zerodha Kite triggered: not made by this tool: left alone", out)
        self.assertEqual([h["method"] for h in self.stub.hits[n:]], ["GET"])
        # a new stop and target: a new ticket, its own risk PASS and yes, then modify-exits
        new = kite_ticket(stop_loss={"order_type": "SL", "trigger_price": 1170.0, "price": 1169.0},
                          target={"price": 1200.0})
        doc = {"orders": [kite_ticket(), new]}
        self.write("trader/orders.json", doc)
        code, _, err = self.tk("modify-exits", "--index", "2", "--linked-id", "123", "--last-price", "1180")
        self.assertEqual(code, 1)
        self.assertIn("trader-risk has no verdict", err)
        risk = jload("trader/state/risk.json")
        risk["results"].append({"fingerprint": base.order_fingerprint(new), "verdict": "PASS"})
        self.write("trader/state/risk.json", risk)
        code, _, err = self.tk("modify-exits", "--index", "2", "--linked-id", "123", "--last-price", "1180")
        self.assertIn("no typed yes recorded", err)                          # the first yes does not cover it
        self.assertEqual(self.stub.calls("PUT", "/kite"), [])
        self.assertEqual(self.tk("confirm", "--index", "2", "--yes-text", "yes 42 RELIANCE")[0], 0)
        code, out, err = self.tk("modify-exits", "--index", "2", "--linked-id", "123", "--last-price", "1180")
        self.assertEqual(code, 0, err)
        self.assertIn("CHANGE SENT to Zerodha Kite (real money): linked order 123 now has the stop at 1170.00", out)
        put = dict(urllib.parse.parse_qsl(self.stub.calls("PUT", "/kite/gtt/triggers/123")[0]["body"].decode()))
        self.assertEqual(json.loads(put["condition"])["trigger_values"], [1170.0, 1200.0])
        code, _, err = self.tk("modify-exits", "--index", "2", "--linked-id", "123", "--last-price", "1180")
        self.assertIn("one yes, one change", err)
        # cancel the linked order: the original yes covers its own cancel
        code, out, err = self.tk("cancel", "--index", "1", "--leg", "oco")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.stub.calls("DELETE", "/kite/gtt/triggers/123")), 1)
        self.assertEqual(self.state()[-1]["status"], "cancel_sent")
        for path, _, files in os.walk("trader"):
            for f in files:
                with open(os.path.join(path, f), encoding="utf-8") as fh:
                    text = fh.read()
                for s in TOKENS:
                    self.assertNotIn(s, text, f)

    def test_upstox_bracket_is_one_multi_leg_gtt(self):
        self.ready("upstox", [ticket_order()], "uptok66")
        code, _, err = self.tk("place", "--index", "1", "--leg", "oco")
        self.assertIn("Upstox has no oco leg here", err)
        self.assertIn("use --leg bracket", err)
        code, out, err = self.tk("place", "--index", "1", "--leg", "bracket")
        self.assertEqual(code, 0, err)
        self.assertIn("the entry with its stop and target as ONE order (with the entry)", out)
        body = json.loads(self.stub.calls("POST", "/upstox/v3/order/gtt/place")[0]["body"])
        self.assertEqual([r["strategy"] for r in body["rules"]], ["ENTRY", "TARGET", "STOPLOSS"])
        self.assertEqual(self.stub.calls("POST", "/upstox/v3/order/place"), [])      # no separate entry
        rec = self.state()[-1]
        self.assertEqual((rec["leg"], rec["status"], rec["broker_order_id"]), ("bracket", "placed",
                                                                               "GTT-CU25280200021013"))
        code, _, err = self.tk("place", "--index", "1")
        self.assertIn("nothing more to send", err)
        # trader-risk counts the bracket as today's entry
        self.assertEqual(len(ticket.records({"orders": self.state()}, base.order_fingerprint(ticket_order()), TODAY,
                                            "placed")), 1)
        code, out, err = self.tk("cancel", "--index", "1", "--leg", "bracket")
        self.assertEqual(code, 0, err)
        sent = self.stub.calls("DELETE", "/upstox/v3/order/gtt/cancel")[0]
        self.assertEqual(json.loads(sent["body"]), {"gtt_order_id": "GTT-CU25280200021013"})

    def test_dhan_super_order_unknown_send_is_found_by_reconcile(self):
        self.ready("dhan", [ticket_order()], "dhtok77")
        self.stub.add("POST", "/dhan/v2/super/orders", 503, {"errorCode": "DH-908", "errorMessage": "down"})
        code, _, err = self.tk("place", "--index", "1", "--leg", "bracket")
        self.assertEqual(code, 1)
        self.assertIn("run ticket.py reconcile", err)
        rec = self.state()[-1]
        self.assertEqual(rec["status"], "unknown")
        self.assertEqual(len(self.stub.calls("POST", "/dhan/v2/super/orders")), 1)      # never retried
        code, _, err = self.tk("place", "--index", "1", "--leg", "bracket")
        self.assertIn("already sent", err)                                 # unknown is not a free second try
        # the broker did get it: its super-order book carries our correlation id
        listed = jload(os.path.join(BFIX, "dhan", "super_list.json"))["response"]["body"]
        ours = copy.deepcopy(listed[0])
        ours.update(orderId="112111182198", correlationId=rec["tag"], tradingSymbol="RELIANCE", securityId="2885",
                    quantity=42, price=1174.3, productType="INTRADAY")
        ours["legDetails"][0]["price"], ours["legDetails"][1]["price"] = 1163.3, 1196.3
        self.stub.add("GET", "/dhan/v2/super/orders", body=listed + [ours])
        code, out, err = self.tk("reconcile", "--json", "--now", "2026-10-29 10:00")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)["data"]
        self.assertEqual(doc["records_added"], 1)
        self.assertEqual(doc["rows"][0]["linked_id"], "112111182198")
        self.assertIn("found at the broker", doc["rows"][0]["verdict"])
        self.assertEqual([x["linked_id"] for x in doc["not_ours"]], ["5925022734212"])
        self.assertEqual((self.state()[-1]["status"], self.state()[-1]["broker_order_id"]), ("placed", "112111182198"))
        # now it can be changed (a new ticket, its own yes) and cancelled
        new = dict(ticket_order(), target={"order_type": "LIMIT", "price": 1200.0})
        self.write("trader/orders.json", {"orders": [ticket_order(), new]})
        risk = jload("trader/state/risk.json")
        risk["results"].append({"fingerprint": base.order_fingerprint(new), "verdict": "PASS"})
        self.write("trader/state/risk.json", risk)
        self.assertEqual(self.tk("confirm", "--index", "2", "--yes-text", "yes 42 RELIANCE")[0], 0)
        code, out, err = self.tk("modify-exits", "--index", "2", "--linked-id", "112111182198")
        self.assertEqual(code, 0, err)
        legs = [json.loads(h["body"])["legName"] for h in self.stub.calls("PUT", "/dhan/v2/super/orders/112111182198")]
        self.assertEqual(legs, ["TARGET_LEG", "STOP_LOSS_LEG"])
        code, out, err = self.tk("cancel", "--linked-id", "112111182198")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.stub.calls("DELETE", "/dhan/v2/super/orders/112111182198/ENTRY_LEG")), 1)

    def test_angel_exits_go_leg_by_leg(self):
        self.ready("angel", [ticket_order()], "angtok34")
        code, _, err = self.tk("place", "--index", "1", "--leg", "oco")
        self.assertEqual(code, 1)
        self.assertIn("Angel One has no oco leg here", err)
        self.assertIn("send the entry, then the stop", err)
        code, out, err = self.tk("place", "--index", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("SENT to Angel One (real money)", out)
        body = json.loads(self.stub.calls("POST", "/angel/rest/secure/angelbroking/order/v1/placeOrder")[0]["body"])
        self.assertEqual((body["variety"], body["tradingsymbol"], body["symboltoken"], body["ordertype"],
                          body["producttype"], body["price"], body["quantity"]),
                         ("NORMAL", "RELIANCE-EQ", "2885", "LIMIT", "INTRADAY", "1174.30", "42"))
        self.assertLess(len(body["ordertag"]), 20)
        code, out, err = self.tk("reconcile")
        self.assertIn("Nothing to reconcile there", out)


# ---------------------------------------------------------------- Angel One: the adapter and its TOTP login

class Angel(StubBase):
    def test_rfc6238_codes(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(auth.totp(secret, at=59, digits=8), "94287082")       # RFC 6238 appendix B
        self.assertEqual(auth.totp(secret, at=1111111109, digits=8), "07081804")
        self.assertEqual(auth.totp(secret.lower(), at=59), "287082")
        with self.assertRaises(auth.LoginError):
            auth.totp("not base32!")
        self.assertEqual(auth.clean_code(" 123 456 "), "123456")
        with self.assertRaises(auth.LoginError):
            auth.clean_code("12345")

    def test_orders_cancels_and_errors(self):
        ad = adapter(self.stub, "angel")
        opt = ad.resolve("NIFTY 2026-11-23 25000 CE", "NFO")
        self.assertEqual((opt["broker_symbol"], opt["broker_id"], opt["strike"], opt["freeze_qty"]),
                         ("NIFTY23NOV2625000CE", "64439", 25000.0, 1800))
        sl = {"symbol": "NIFTY 2026-11-23 25000 CE", "exchange": "NFO", "product": "NRML", "side": "BUY",
              "qty": 130, "order_type": "SL", "price": 121.0, "trigger_price": 120.5, "validity": "IOC",
              "tag": "bt0123456789abcdee"}
        m, host, path, p = ad.order_request(sl, opt)
        self.assertEqual(path, "/rest/secure/angelbroking/order/v1/placeOrder")
        self.assertEqual(p["json_body"], {"variety": "STOPLOSS", "tradingsymbol": "NIFTY23NOV2625000CE",
                                          "symboltoken": "64439", "transactiontype": "BUY", "exchange": "NFO",
                                          "ordertype": "STOPLOSS_LIMIT", "producttype": "CARRYFORWARD",
                                          "duration": "IOC", "price": "121.00", "triggerprice": "120.50",
                                          "squareoff": "0", "stoploss": "0", "quantity": "130",
                                          "ordertag": "bt0123456789abcdee"})
        self.assertIn("Use a LIMIT", ad.live_policy(dict(sl, order_type="MARKET"))[0])
        # SmartAPI answers errors with HTTP 200 and "status": false
        self.stub.error("angel", "token")
        with self.assertRaises(base.AuthExpired) as cm:
            adapter(self.stub, "angel").profile()
        self.assertIn("AG8001", cm.exception.message)
        self.stub.add("POST", "/angel/rest/secure/angelbroking/order/v1/placeOrder", 403,
                      {"message": "Access denied because of exceeding access rate", "errorcode": "", "status": False})
        with self.assertRaises(base.RateLimited):
            ad.call("POST", "api", "/rest/secure/angelbroking/order/v1/placeOrder", kind="order", json_body={})
        # a cancel names the order's variety, read from the order book first
        t, cfg, risk, state, pre = open_docs("angel", "angtok34")
        sent = {"broker_order_id": "201020000000080", "fingerprint": "f", "yes_text": "yes"}
        gp = base.open_cancel_gate("angel", cfg, "201020000000080", sent, state, pre, "angtok34", TODAY, NOW)
        self.assertTrue(ad.cancel_order("201020000000080", gp))
        body = json.loads(self.stub.calls("POST", "/angel/rest/secure/angelbroking/order/v1/cancelOrder")[0]["body"])
        self.assertEqual(body, {"variety": "NORMAL", "orderid": "201020000000080"})

    def test_static_ip_preflight_uses_the_config(self):
        ad = pkg.make("angel", env=ENV, hosts=self.stub.hosts("angel"), cfg={"static_ip": {"angel": "203.0.113.7"}})
        self.assertIsNone(ad.registered_ips())
        self.assertEqual(ad.base_headers()["X-ClientPublicIP"], "203.0.113.7")


class AngelLogin(StubBase):
    def setUp(self):
        StubBase.setUp(self)
        self.secret = base64.b32encode(b"12345678901234567890").decode()
        ticket.ADAPTER_KW.clear()
        ticket.ADAPTER_KW["angel"] = {"hosts": self.stub.hosts("angel"), "env": dict(ENV)}
        self.asked = []
        ticket.ASK_CODE = lambda prompt: self.asked.append(prompt) or "123456"

    def tearDown(self):
        ticket.ADAPTER_KW.clear()
        ticket.ASK_CODE = None

    def login(self):
        return run(ticket, ["login", "angel", "--now", "2026-10-29 08:30"])

    def sent(self):
        return json.loads(self.stub.calls("POST", "/angel/rest/auth/angelbroking/user/v1/loginByPassword")[-1]["body"])

    def test_typed_code(self):
        code, out, err = self.login()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["export BT_ANGEL_ACCESS_TOKEN=TEST-JWT",
                                            "export BT_ANGEL_TOKEN_DATE=2026-10-29T08:30:00+05:30"])
        self.assertEqual(self.sent(), {"clientcode": "A1234567", "password": "4321", "totp": "123456"})
        self.assertIn("not shown", self.asked[0])
        self.assertIn("midnight IST", err)
        self.assertNotIn("TEST-REFRESH", out + err)
        self.assertNotIn("TEST-FEED", out + err)
        hdr = {k.lower(): v for k, v in self.stub.calls("POST", "/angel/rest/auth")[-1]["headers"].items()}
        self.assertEqual((hdr["x-privatekey"], hdr["x-usertype"], hdr["x-sourceid"]), ("angkey12", "USER", "WEB"))
        self.assertNotIn("authorization", hdr)

    def test_code_from_the_totp_secret(self):
        ticket.ADAPTER_KW["angel"]["env"]["BT_ANGEL_TOTP_SECRET"] = self.secret
        before = auth.totp(self.secret)
        code, out, err = self.login()
        after = auth.totp(self.secret)
        self.assertEqual(code, 0, err)
        self.assertIn(self.sent()["totp"], (before, after))
        self.assertEqual(self.asked, [])
        self.assertIn("Anyone who can read your shell environment can log in as you", err)
        self.assertNotIn(self.secret, out + err)

    def test_bad_code_or_missing_pin_sends_nothing(self):
        ticket.ASK_CODE = lambda prompt: "12a45"
        code, _, err = self.login()
        self.assertEqual(code, 1)
        self.assertIn("6 digits", err)
        env = dict(ENV)
        env.pop("BT_ANGEL_PIN")
        ticket.ADAPTER_KW["angel"]["env"] = env
        ticket.ASK_CODE = lambda prompt: "123456"
        code, _, err = self.login()
        self.assertIn("BT_ANGEL_PIN", err)
        self.assertEqual(self.stub.calls("POST", "/angel/rest/auth"), [])

    def test_refused_login_is_plain_words(self):
        self.stub.add("POST", "/angel/rest/auth/angelbroking/user/v1/loginByPassword",
                      body={"status": False, "message": "Invalid totp", "errorcode": "AB1050", "data": None})
        code, out, err = self.login()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Invalid totp", err)


# ---------------------------------------------------------------- the broker's basket margin next to ours

STRAT_LEGS = [  # trader-strategy's basket legs (an iron condor on NIFTY, 23-Nov-2026)
    {"symbol": "NIFTY", "exchange": "NFO", "segment": "OPT", "expiry": "2026-11-23", "strike": 25500,
     "option_type": "CE", "side": "SELL", "qty": 65, "product": "NRML", "order_type": "LIMIT", "price": 120.5},
    {"symbol": "NIFTY", "exchange": "NFO", "segment": "OPT", "expiry": "2026-11-23", "strike": 25000,
     "option_type": "PE", "side": "BUY", "qty": 65, "product": "NRML", "order_type": "LIMIT", "price": 80.0}]


class MarginCrossCheck(StubBase):
    def setUp(self):
        StubBase.setUp(self)
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)
        os.makedirs("trader/state")
        with open("trader/orders.json", "w") as fh:
            json.dump({"orders": STRAT_LEGS}, fh)
        ticket.ADAPTER_KW.clear()
        for b in BROKERS:
            ticket.ADAPTER_KW[b] = {"hosts": self.stub.hosts(b), "env": dict(ENV), "sleep": lambda s: None}

    def tearDown(self):
        ticket.ADAPTER_KW.clear()
        os.chdir(self.old)
        shutil.rmtree(self.dir)

    def test_the_three_margin_keys_agree(self):
        rows = ticket.margin_rows(STRAT_LEGS)
        self.assertEqual(ticket.margin_key(rows), strategy.margin_key(rows))
        self.assertEqual(ticket.margin_key(rows), check.margin_key(rows))
        self.assertEqual(ticket.margin_key(rows), ticket.margin_key(list(reversed(rows))))    # order-free
        self.assertNotEqual(ticket.margin_key(rows), ticket.margin_key(rows[:1]))

    def test_the_shared_block_is_identical(self):
        def block(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            m = re.search(r"# -+ the broker's basket margin \(shared block\)\n(.*?)# -+ end of the shared block",
                          text, re.S)
            self.assertIsNotNone(m, path)
            return m.group(1)
        self.assertEqual(block(os.path.join(ROOT, "trader-strategy", "strategy.py")),
                         block(os.path.join(ROOT, "trader-risk", "check.py")))

    def test_margin_check_reads_each_broker_and_saves_the_difference(self):
        expect = {"kite": 34786.73, "upstox": 33.6, "dhan": 150000.0, "angel": 29612.35}
        for b, total in expect.items():
            with self.subTest(broker=b):
                n = len(self.stub.hits)
                code, out, err = run(ticket, ["margin-check", "--broker", b, "--estimate", "30000", "--json",
                                              "--now", "2026-10-29 10:02"])
                self.assertEqual(code, 0, err)
                d = json.loads(out)["data"]
                self.assertEqual((d["total"], d["our_estimate"], d["read_only"]), (total, 30000.0, True))
                self.assertEqual(d["difference"], round(total - 30000, 2))
                self.assertEqual([h["method"] for h in self.stub.hits[n:] if "instrument" not in h["path"]
                                  and "scrip" not in h["path"].lower() and "OpenAPI" not in h["path"]], ["POST"])
        body = json.loads(self.stub.calls("POST", "/angel/rest/secure/angelbroking/margin/v1/batch")[-1]["body"])
        self.assertEqual(body["positions"][0], {"exchange": "NFO", "qty": 65, "price": 120.5,
                                                "productType": "CARRYFORWARD", "token": "64473", "tradeType": "SELL",
                                                "orderType": "LIMIT"})
        code, out, _ = run(ticket, ["margin-check", "--broker", "kite", "--estimate", "30000", "--now",
                                    "2026-10-29 10:02"])
        self.assertIn("Zerodha Kite basket margin for 2 legs at 10:02 IST: ₹34,786.73", out)
        self.assertIn("Difference: +₹4,786.73 (+16.0%)", out)
        self.assertIn("Read only: nothing was sent", out)
        cache = jload("trader/state/broker_margin.json")
        self.assertEqual(len(cache["checks"]), 1)                     # newest per basket
        # no token: refused before any call
        ticket.ADAPTER_KW["kite"]["env"] = {k: v for k, v in ENV.items() if k != "BT_KITE_ACCESS_TOKEN"}
        n = len(self.stub.hits)
        code, _, err = run(ticket, ["margin-check", "--broker", "kite"])
        self.assertEqual(code, 1)
        self.assertIn("no BT_KITE_ACCESS_TOKEN token", err)
        self.assertEqual(len(self.stub.hits), n)

    def test_strategy_and_risk_show_it_next_to_ours(self):
        run(ticket, ["margin-check", "--broker", "kite", "--estimate", "30000", "--now", "2026-10-29 10:02"])
        rows = ticket.margin_rows(STRAT_LEGS)
        key = strategy.margin_key(rows)
        bm = strategy.broker_margin_lookup(key, 31000.0, None, TODAY)
        self.assertEqual((bm["title"], bm["total"], bm["our_estimate"], bm["difference"], bm["difference_pct"]),
                         ("Zerodha Kite", 34786.73, 31000.0, 3786.73, 12.2))
        words = strategy.broker_margin_words(bm, strategy.inr)
        self.assertEqual(words, "Broker basket margin (Zerodha Kite, 10:02 IST): ₹34,786.73 vs our SPAN-style "
                                "estimate ₹31,000.00: +₹3,786.73 (+12.2%). The broker's figure is what it blocks; "
                                "read only, nothing was sent.")
        self.assertIsNone(strategy.broker_margin_lookup(key, 31000.0, None, "2026-10-30"))    # today's only
        self.assertIsNone(check.broker_margin_lookup(check.margin_key(rows[:1]), 1.0, None, TODAY))
        # trader-strategy's own document: broker_basket filled, our estimate untouched
        doc = {"underlying": "NIFTY", "legs": [
            {"side": "SELL", "instrument": "OPT", "expiry": "2026-11-23", "strike": 25500.0, "option_type": "CE",
             "qty": 65}, {"side": "BUY", "instrument": "OPT", "expiry": "2026-11-23", "strike": 25000.0,
                          "option_type": "PE", "qty": 65}], "margin": {"required": 31000.0, "broker_basket": None}}

        class A(object):
            no_broker_margin = False
            broker_margin = None
            broker_margin_file = None
        got = strategy.attach_broker_margin(doc, {"broker": "paper"}, A(), NOW)
        self.assertEqual(got["total"], 34786.73)
        self.assertEqual((doc["margin"]["required"], doc["margin"]["broker_basket"]["difference"]), (31000.0, 3786.73))
        self.assertIn("Broker basket margin (Zerodha Kite", doc["margin"]["broker_note"])
        # trader-risk: shown with the basket, never a verdict by itself
        legs = [{"qty": -65.0, "underlying": "NIFTY", "expiry": "2026-11-23", "strike": 25500.0, "instrument": "OPT",
                 "option_type": "CE"}, {"qty": 65.0, "underlying": "NIFTY", "expiry": "2026-11-23", "strike": 25000.0,
                                        "instrument": "OPT", "option_type": "PE"}]
        bm = check.basket_broker_margin(legs, STRAT_LEGS, 31000.0, {"broker": "paper"}, TODAY)
        self.assertEqual((bm["total"], bm["difference"]), (34786.73, 3786.73))

    def test_no_token_means_no_fetch(self):
        self.assertIsNone(strategy.margin_broker({"broker": "kite"}, env={}))
        self.assertEqual(strategy.margin_broker({"broker": "zerodha"}, env={"BT_KITE_ACCESS_TOKEN": "x"}), "kite")
        self.assertEqual(check.margin_broker({"broker": "paper", "margin_broker": "angel"},
                                             env={"BT_ANGEL_ACCESS_TOKEN": "x"}), "angel")
        self.assertIsNone(check.margin_broker({"broker": "paper"}, env={"BT_KITE_ACCESS_TOKEN": "x"}))


if __name__ == "__main__":
    unittest.main()
