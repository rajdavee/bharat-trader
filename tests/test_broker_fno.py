"""trader-broker paper F&O tests, offline: baskets, margin, fills, MTM, adjustments, expiry settlement.

Fixtures (REAL NSE data, trimmed):
  trader-options/chain-NIFTY-03-Nov-2026.json.gz   the NIFTY option chain of Fri 30-Oct-2026 15:40 (reused): its
                                                   bid, ask, depth and our Black-76 IVs are the "latest chain"
  trader-options/fo_mktlots.csv                    lot sizes (NIFTY 65)
  trader-broker/fno/NSE_FO_contract_30102026-NIFTY.csv   NSE's F&O contract master of 30-Oct-2026: the NIFTY
                                                   03-Nov rows around the money and the three NIFTY futures
                                                   (price bands MinPric / MaxPric in paise, expiry in seconds
                                                   since 1980)
  trader-broker/fno/FOSett_prce_27102026.csv       NSE's futures settlement prices of Tue 27-Oct-2026, an expiry
                                                   day: the NIFTY Oct future's row, 22,749.75, is the final
                                                   settlement price
  trader-broker/fno/BhavCopy_NSE_FO_20261027-NIFTY-expiry.csv   the F&O bhavcopy of 27-Oct-2026, the NIFTY
                                                   contracts expiring that day (UndrlygPric 22,749.75)
  trader-broker/fno/risk-pass-iron-condor.json     trader-risk's PASS for the four condor legs (the file check.py
                                                   writes), so these tests do not depend on the risk rules

Charges are worked by hand below (opt_side) with the 2026-04-01 rates: STT 0.15% on the option sold and on
the intrinsic value of an exercised long, NSE 0.03553%, SEBI Rs 10 a crore, stamp 0.003% on the buy, GST 18%
on brokerage + exchange + SEBI, and Zerodha's flat Rs 20 an option order.
"""

import io
import json
import gzip
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

ticket = load("trader-broker", "ticket")
st = load("trader-strategy", "strategy")
op = load("trader-options", "options")
OPT = os.path.join(FIX, "trader-options")
FNO = os.path.join(FIX, "trader-broker", "fno")
NOW = "2026-11-02 10:00"          # Monday: the chain is Friday's close
TODAY = "2026-11-02"
SID = "S-20261102-1"


def analysis(expiry="03-Nov-2026"):
    with gzip.open(os.path.join(OPT, "chain-NIFTY-%s.json.gz" % expiry), "rb") as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    with open(os.path.join(OPT, "fo_mktlots.csv"), encoding="utf-8") as fh:
        lots = op.parse_lots(fh.read())
    chain = op.parse_chain(raw, "NIFTY")
    return op.analyse(chain, op.lot_size(lots, "NIFTY", chain["expiry"]), 0.0557, now=op.parse_nse_time(NOW))


NIFTY = analysis()
MKT = {("NIFTY", NIFTY["expiry"]): NIFTY}


def side_of(k, kind):
    return next(r for r in NIFTY["rows"] if r["strike"] == k)[kind.lower()]


def condor():
    legs = st.resolve_legs(st.template_specs("iron-condor", {"wings": 200, "width": 100}, NIFTY),
                           {NIFTY["expiry"]: NIFTY}, NIFTY["expiry"])
    return st.evaluate(legs, {NIFTY["expiry"]: NIFTY}, {}, "iron-condor", {"wings": 200, "width": 100}, simulate=False,
                       at=op.parse_nse_time(NOW))


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jsave(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)


def tk(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def d(x):
    return Decimal(repr(float(x)))


def paisa(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def opt_side(price, qty, buy):
    """Charges on one executed option order, by hand."""
    v = d(price) * qty
    brok = Decimal(20)
    stt = Decimal(0) if buy else v * Decimal("0.15") / 100
    exch = v * Decimal("0.03553") / 100
    sebi = v * 10 / Decimal(10000000)
    stamp = v * Decimal("0.003") / 100 if buy else Decimal(0)
    gst = (brok + exch + sebi) * Decimal(18) / 100
    return float(sum(paisa(x) for x in (brok, stt, exch, sebi, stamp, gst)))


def risk_pass(orders, date=TODAY):
    """The verdicts file check.py writes, PASS for every leg of this basket (same shape as the fixture)."""
    doc = jload(os.path.join(FNO, "risk-pass-iron-condor.json"))
    row = dict(doc["results"][0])
    bid = ticket.basket_id(orders)
    doc["results"] = [dict(row, index=i, side=o["side"], qty=o["qty"], fingerprint=ticket.fingerprint(o), basket_id=bid)
                      for i, o in enumerate(orders, 1)]
    doc["date"] = date
    return doc


class Base(unittest.TestCase):
    CFG = {"capital": 200000, "broker_profile": "zerodha", "paper_spread_share": 0}

    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp(prefix="bt-fno-")
        os.chdir(self.dir)
        os.makedirs("trader/state")
        jsave("trader/config.json", self.CFG)
        jsave("trader/options.json", NIFTY)
        self.strategy = condor()
        jsave("trader/strategy.json", self.strategy)
        jsave("trader/orders.json", st.orders_document(self.strategy, created_at="2026-11-02T04:30:00Z"))
        shutil.copy(os.path.join(FNO, "risk-pass-iron-condor.json"), "trader/state/risk.json")
        shutil.copy(os.path.join(FNO, "NSE_FO_contract_30102026-NIFTY.csv"), "trader/bands.csv")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir, ignore_errors=True)

    def cfg(self, **over):
        c = jload("trader/config.json")
        c.update(over)
        jsave("trader/config.json", c)

    def orders(self):
        return jload("trader/orders.json")["orders"]

    def pf(self):
        return jload("trader/portfolio.json")

    def ok(self, *argv):
        code, out, err = tk(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def place(self, now=NOW, fill_at="2026-11-02 10:01"):
        self.ok("confirm", "--basket", "--yes-text", "yes", "--now", now)
        return self.ok("fill", "--basket", "--now", fill_at)

    def pass_current(self, date=TODAY):
        jsave("trader/state/risk.json", risk_pass(self.orders(), date))


class Fixtures(Base):
    def test_risk_fixture_is_this_basket(self):
        fps = [ticket.fingerprint(o) for o in self.orders()]
        rk = jload("trader/state/risk.json")
        self.assertEqual([r["fingerprint"] for r in rk["results"]], fps)
        self.assertEqual({r["basket_id"] for r in rk["results"]}, {ticket.basket_id(self.orders())})
        self.assertEqual([(o["side"], o["option_type"], o["strike"], o["qty"]) for o in self.orders()],
                         [("BUY", "PE", 22700.0, 65), ("SELL", "PE", 22800.0, 65), ("SELL", "CE", 23200.0, 65),
                          ("BUY", "CE", 23300.0, 65)])


class Basket(Base):
    def test_one_ticket_for_the_whole_basket(self):
        code, out, _ = tk("show", "--now", NOW)
        self.assertEqual(code, 0)
        self.assertIn("BASKET of 4 legs on NIFTY, PAPER account (no real money): iron-condor", out)
        for k in ("22700 PE", "22800 PE", "23200 CE", "23300 CE"):
            self.assertIn("NIFTY %s 03-Nov-2026" % k, out)
        self.assertIn("paper fills hedge first: legs 1, 4, 2, 3", out)
        m = self.strategy["margin"]
        blocked = round(m["required"] - m["span_style"]["premium_paid"], 2)
        self.assertIn("margin blocked: about %s" % ticket.inr(blocked), out)
        self.assertIn("max loss at expiry: %s" % ticket.inr(-self.strategy["expiry"]["max_loss"]), out)
        entry = sum(opt_side(o["price"], 65, o["side"] == "BUY") for o in self.orders())
        self.assertIn("charges: %s now" % ticket.inr(entry), out)
        self.assertIn("READY: trader-risk PASS on all 4 legs", out)
        self.assertEqual(out.count("Reply \"yes\""), 1)
        v = json.loads(tk("show", "--json", "--now", NOW)[1])
        self.assertTrue(v["basket"] and v["ready"])
        self.assertEqual(v["basket_id"], ticket.basket_id(self.orders()))
        self.assertEqual(v["summary"]["net_premium"], round((44.30 + 44.85 - 28.70 - 24.05) * 65, 2))

    def test_the_yes_must_be_for_the_basket_ticket(self):
        for reply in ("sure", "ok", "yes?", "yes but wait", "yes BANKNIFTY", "yes 23400", "yes to the rest",
                      "yes if it fills at 40"):
            code, out, err = tk("confirm", "--basket", "--yes-text", reply, "--now", NOW)
            self.assertNotEqual(code, 0, reply)
            self.assertNotIn("Placed", out, reply)
        code, _, err = tk("confirm", "--index", "1", "--yes-text", "yes", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("ONE ticket and one yes covers every leg", err)
        code, _, err = tk("confirm", "--basket", "--basket-id", "0000000000000000", "--yes-text", "yes", "--now", NOW)
        self.assertIn("show the current ticket", err)
        self.assertEqual(jload("trader/state/broker.json")["orders"] if os.path.exists("trader/state/broker.json")
                         else [], [])
        out = self.ok("confirm", "--basket", "--yes-text", "yes, all 4 legs, credit 2366", "--now", NOW)
        self.assertIn("hedge first: 1, 4, 2, 3", out)
        recs = jload("trader/state/broker.json")["orders"]
        self.assertEqual([r["index"] for r in recs], [1, 4, 2, 3])
        self.assertEqual({r["basket_id"] for r in recs}, {ticket.basket_id(self.orders())})
        self.assertEqual({r["status"] for r in recs}, {"placed"})
        self.assertEqual(sum(r["max_loss"] for r in recs), recs[0]["basket_max_loss"])   # a basket counts once
        code, out, _ = tk("show", "--now", NOW)
        self.assertIn("DONE: placed, waiting for the fill", out)
        code, _, err = tk("confirm", "--basket", "--yes-text", "yes", "--now", NOW)
        self.assertIn("not ready", err)

    def test_every_leg_needs_its_pass(self):
        rk = jload("trader/state/risk.json")
        rk["results"][2]["verdict"] = "BLOCK"
        rk["results"][2]["reasons"] = ["margin: not enough"]
        jsave("trader/state/risk.json", rk)
        code, out, _ = tk("show", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by trader-risk: leg 3: margin: not enough", out)
        rk["results"][2].update(verdict="PASS", reasons=[])
        rk["results"] = rk["results"][:3]
        jsave("trader/state/risk.json", rk)
        self.assertIn("NOT READY: leg 4", tk("show", "--now", NOW)[1])
        jsave("trader/state/risk.json", risk_pass(self.orders(), "2026-10-30"))
        self.assertIn("not today", tk("show", "--now", NOW)[1])

    def test_a_live_config_never_takes_a_basket(self):
        self.cfg(broker="upstox", paper=False)
        code, _, err = tk("confirm", "--basket", "--yes-text", "yes", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("paper-only", err)
        self.assertFalse(os.path.exists("trader/state/broker.json"))


class EndToEnd(Base):
    def test_iron_condor_propose_fill_mtm_adjust_settle(self):
        # fill: hedge first, every leg at the chain's mid (paper_spread_share 0), exact charges
        out = self.place()
        lines = [l for l in out.splitlines() if l.strip()[:2] in ("1.", "2.", "3.", "4.")]
        self.assertEqual([l.split(":")[0].strip() for l in lines], ["1. leg 1", "2. leg 4", "3. leg 2", "4. leg 3"])
        prices = {(22700, "PE"): 28.70, (22800, "PE"): 44.30, (23200, "CE"): 44.85, (23300, "CE"): 24.05}
        trades = jload("trader/state/trades.json")["trades"]
        self.assertEqual([t["price"] for t in trades], [28.70, 24.05, 44.30, 44.85])
        want = [opt_side(28.70, 65, True), opt_side(24.05, 65, True), opt_side(44.30, 65, False),
                opt_side(44.85, 65, False)]
        self.assertEqual([t["charges"] for t in trades], want)
        self.assertEqual(want, [24.44, 24.31, 29.12, 29.20])
        self.assertEqual(trades[2]["charges_detail"]["stt"], 4.32)          # 0.15% of 2,879.50
        pf = self.pf()
        credit = (44.30 + 44.85 - 28.70 - 24.05) * 65                        # 2,366.00
        self.assertEqual(pf["cash"], round(200000 + credit - sum(want), 2))  # 2,02,258.93
        self.assertEqual(pf["cash"], 202258.93)
        m = self.strategy["margin"]
        self.assertEqual(pf["margin_blocked"], round(m["required"] - m["span_style"]["premium_paid"], 2))
        self.assertEqual(pf["strategies"][SID]["name"], "iron-condor")
        legs = {(p["strike"], p["option_type"]): (p["qty"], p["avg_price"]) for p in pf["fno_positions"]}
        self.assertEqual(legs, {(22700.0, "PE"): (65, 28.70), (22800.0, "PE"): (-65, 44.30),
                                (23200.0, "CE"): (-65, 44.85), (23300.0, "CE"): (65, 24.05)})
        self.assertEqual({p["exchange"] for p in pf["fno_positions"]}, {"NFO"})
        self.assertIn("DONE: placed, filled", tk("show", "--now", NOW)[1])

        # mark to market from the latest chain, with Black-76 Greeks
        v = json.loads(self.ok("mtm", "--json", "--now", "2026-11-02 10:05"))
        s = v["strategies"][0]
        self.assertEqual(s["id"], SID)
        marks = {(r["contract"]): r["mark"] for r in v["positions"]}
        self.assertEqual(marks["NIFTY 22700 PE 03-Nov-2026"], side_of(22700, "pe")["price"])
        un = sum((side_of(k, t)["price"] - px) * (65 if (k, t) in ((22700, "PE"), (23300, "CE")) else -65)
                 for (k, t), px in prices.items())
        self.assertAlmostEqual(s["unrealised"], un, delta=0.02)
        F = NIFTY["forward"]
        T = ticket.years_left("2026-11-03", ticket.parse_now("2026-11-02 10:05"))
        g = op.black76_greeks(F, 23200.0, T, 0.0557, side_of(23200, "ce")["iv"] / 100.0, "CE")
        row = next(r for r in v["positions"] if r["contract"] == "NIFTY 23200 CE 03-Nov-2026")
        self.assertAlmostEqual(row["greeks"]["delta"], -65 * g["delta"], 3)
        self.assertGreater(s["greeks"]["theta"], 0)       # short premium earns time decay
        self.assertLess(s["greeks"]["vega"], 0)
        self.assertEqual(v["greeks_from"], "trader-options black76_greeks")
        self.assertEqual(s["payoff"]["strikes"], [22700.0, 22800.0, 23200.0, 23300.0])
        text = self.ok("mtm", "--now", "2026-11-02 10:05")
        self.assertIn("leg 3: NIFTY 23200 CE 03-Nov-2026 -65 (1 lot) at 44.85", text)

        # adjust: roll the short call (leg 3) up to 23250. A new ticket, through risk and a new yes
        out = self.ok("adjust", "--strategy", SID, "--roll-leg", "3", "--strike", "23250", "--now", "2026-11-02 11:00")
        self.assertIn("Nothing was placed", out)
        roll = self.orders()
        self.assertEqual([(o["side"], o["strike"], o["price"]) for o in roll], [("BUY", 23200.0, 44.85),
                                                                             ("SELL", 23250.0, 33.25)])
        code, text, _ = tk("show", "--now", "2026-11-02 11:00")
        self.assertIn("NOT READY", text)                    # the old PASS does not cover a new ticket
        self.pass_current()
        text = self.ok("show", "--now", "2026-11-02 11:00")
        self.assertIn("BASKET of 2 legs on NIFTY", text)
        self.assertIn("roll %s" % SID, text)
        self.place("2026-11-02 11:00", "2026-11-02 11:01")
        pf = self.pf()
        close_ch, open_ch = opt_side(44.85, 65, True), opt_side(33.25, 65, False)
        roll_realised = round(0.0 - close_ch - 29.20, 2)     # bought back at its sale price: only the charges
        self.assertEqual(pf["strategies"][SID]["realised"], roll_realised)
        cash = round(202258.93 - 44.85 * 65 - close_ch + 33.25 * 65 - open_ch, 2)
        self.assertEqual(pf["cash"], cash)
        self.assertEqual(sorted(p["strike"] for p in pf["fno_positions"]), [22700.0, 22800.0, 23250.0, 23300.0])
        self.assertIn("trader-broker estimate", pf["strategies"][SID]["margin_source"])
        self.assertGreater(pf["margin_blocked"], 0)

        # expiry: NIFTY settles at 23,350 (a price by hand: 03-Nov-2026's is not known yet; the real
        # NSE files are read in Settlement below). 23250 CE short is assigned (100), 23300 CE long is
        # exercised (50, STT on exercise), the puts expire worthless.
        out = self.ok("settle", "--date", "2026-11-03", "--price", "NIFTY=23350", "--now", "2026-11-03 16:00")
        stt = float(paisa(d(50) * 65 * Decimal("0.15") / 100))
        self.assertEqual(stt, 4.88)
        self.assertIn("STT on exercise ₹4.88", out)
        self.assertIn("assigned at 23350.00 (intrinsic 100.00)", out)
        pf = self.pf()
        self.assertEqual(pf["fno_positions"], [])
        self.assertEqual(pf["margin_blocked"], 0.0)
        self.assertEqual(pf["strategies"][SID]["status"], "closed")
        final = round(cash - 100 * 65 + 50 * 65 - stt, 2)
        self.assertEqual(pf["cash"], final)
        self.assertEqual(pf["strategies"][SID]["realised"], round(final - 200000, 2))   # every rupee accounted for
        charges = sum(want) + close_ch + open_ch + stt
        self.assertEqual(pf["strategies"][SID]["charges"], round(charges, 2))
        kinds = [r["kind"] for r in pf["ledger"]]
        self.assertEqual(kinds.count("stt_exercise"), 1)
        self.assertAlmostEqual(sum(r["amount"] for r in pf["ledger"] if not r["kind"].startswith("margin")),
                               final - 200000, 2)


class Margin(Base):
    def test_block_and_release(self):
        self.place()
        pf = self.pf()
        blocks = sum(r["amount"] for r in pf["ledger"] if r["kind"] in ("margin_block", "margin_release"))
        self.assertAlmostEqual(blocks, pf["margin_blocked"], 2)
        self.assertEqual(ticket.free_cash(pf), round(pf["cash"] - pf["margin_blocked"], 2))
        self.ok("adjust", "--strategy", SID, "--close-all", "--now", "2026-11-02 12:00")
        self.assertEqual([o["side"] for o in self.orders()], ["SELL", "BUY", "BUY", "SELL"])
        self.pass_current()
        text = self.ok("show", "--now", "2026-11-02 12:00")
        self.assertIn("paper fills hedge first: legs 2, 3, 1, 4", text)     # buy back the shorts first
        out = self.place("2026-11-02 12:00", "2026-11-02 12:01")
        self.assertIn("is closed", out)
        pf = self.pf()
        self.assertEqual((pf["margin_blocked"], pf["fno_positions"], pf["strategies"][SID]["status"]), (0.0, [], "closed"))
        self.assertIn("margin_release", [r["kind"] for r in pf["ledger"] if r["at"].startswith("2026-11-02T12:01")])
        self.assertAlmostEqual(sum(r["amount"] for r in pf["ledger"] if r["kind"].startswith("margin")), 0.0, 2)

    def test_not_enough_cash_fills_nothing(self):
        self.cfg(capital=40000)
        self.ok("confirm", "--basket", "--yes-text", "yes", "--now", NOW)
        code, _, err = tk("fill", "--basket", "--now", "2026-11-02 10:01")
        self.assertEqual(code, 1)
        self.assertIn("nothing filled", err)
        self.assertFalse(os.path.exists("trader/portfolio.json"))
        self.assertFalse(os.path.exists("trader/state/trades.json"))

    def test_hedge_first_needs_less_margin_on_the_way(self):
        pf = {"cash": 200000.0, "positions": {}}
        ticket.migrate_portfolio(pf)
        now = ticket.parse_now("2026-11-02 10:01")
        o = self.orders()
        short_first = ticket.span_estimate([ticket.leg_view(o[1], ticket.quote_for(MKT, o[1]))], now, "index")
        hedged = ticket.span_estimate([ticket.leg_view(x, ticket.quote_for(MKT, x)) for x in (o[0], o[1])], now, "index")
        self.assertLess(hedged["total"], short_first["total"])
        futs = ticket.span_estimate([{"segment": "FUT", "option_type": None, "strike": None, "expiry": "2026-11-24",
                                      "qty": -65, "forward": 23098.90, "iv": None, "rate": 0.0, "avg_price": 23098.90}],
                                    now, "index")
        self.assertEqual(futs["scan_risk"], round(23098.90 * 65 * 0.093, 2))      # a future: notional x scan
        self.assertEqual(futs["exposure"], round(23098.90 * 65 * 0.02, 2))
        longs = ticket.span_estimate([ticket.leg_view(o[0], ticket.quote_for(MKT, o[0]))], now, "index")
        self.assertEqual((longs["total"], longs["method"]), (0.0, "premium"))


class FillRealism(Base):
    def test_a_limit_at_the_mid_waits_when_the_book_is_wider(self):
        self.cfg(paper_spread_share=0.5)
        self.ok("confirm", "--basket", "--yes-text", "yes", "--now", NOW)
        code, _, err = tk("fill", "--basket", "--now", "2026-11-02 10:01")
        self.assertEqual(code, 1)
        self.assertIn("leg 1", err)
        self.assertIn("the BUY LIMIT 28.70 waits: the book is at 28.85", err)
        self.assertFalse(os.path.exists("trader/portfolio.json"))

    def test_spread_and_bps_slippage(self):
        o = dict(self.orders()[0], order_type="LIMIT", price=30.0)
        q = ticket.quote_for(MKT, o)
        self.assertEqual((q["bid"], q["ask"]), (28.5, 28.85))
        r = ticket.paper_fill_price(o, q, {"paper_spread_share": 0.5})
        self.assertEqual(r["price"], 28.85)                                   # a buy pays the ask
        r = ticket.paper_fill_price(dict(o, side="SELL", price=20.0), q, {"paper_spread_share": 0.5})
        self.assertEqual(r["price"], 28.5)                                    # a sell gets the bid
        r = ticket.paper_fill_price(o, dict(q, bid=None, ask=None, ltp=28.9), {"paper_slippage_bps": 50})
        self.assertEqual(r["price"], 29.05)                                   # 28.90 + 0.5%, up to the tick
        self.assertIn("50 bps", r["source"])

    def test_market_orders_get_market_protection(self):
        o = dict(self.orders()[0], order_type="MARKET", price=None)
        q = ticket.quote_for(MKT, o)                                          # last 28.90, ask 28.85
        r = ticket.paper_fill_price(o, q, {"market_protection_pct": 2})
        self.assertTrue(r["ok"])
        self.assertEqual((r["price"], r["limit"]), (28.85, 29.45))            # 28.90 x 1.02 = 29.478, down to 29.45
        self.assertIn("MARKET sent as LIMIT 29.45: market protection 2%", r["notes"][0])
        r = ticket.paper_fill_price(o, dict(q, bid=29.5, ask=30.0), {"market_protection_pct": 2, "paper_spread_share": 0.5})
        self.assertFalse(r["ok"])
        self.assertIn("cancelled by market protection", r["why"])
        cfg = {"market_protection_pct": 2, "paper_spread_share": 0.5}
        s = ticket.paper_fill_price(dict(o, side="SELL"), dict(q, bid=28.4), cfg)
        self.assertEqual((s["price"], s["limit"]), (28.4, 28.35))            # 28.90 x 0.98 = 28.322, up to 28.35
        self.assertFalse(ticket.paper_fill_price(dict(o, side="SELL"), dict(q, bid=28.0), cfg)["ok"])

    def test_price_bands_from_the_nse_contract_master(self):
        bands = ticket.load_bands("trader/bands.csv")
        self.assertEqual(bands["NIFTY-2026-11-03-22700-PE"][:2], (0.05, 80.05))
        self.assertEqual(bands["NIFTY-2026-11-23-FUT"][:2], (20789.10, 25408.70))
        o = dict(self.orders()[1], price=85.0)                               # SELL 22800 PE: band 0.05 to 140.10
        self.assertEqual(bands["NIFTY-2026-11-03-22800-PE"][:2], (0.05, 140.10))
        o2 = dict(self.orders()[0], side="SELL", price=85.0)                 # 22700 PE: above its 80.05 ceiling
        r = ticket.paper_fill_price(o2, ticket.quote_for(MKT, o2), {}, bands[ticket.contract_key(o2)])
        self.assertFalse(r["ok"])
        self.assertIn("outside NIFTY 22700 PE 03-Nov-2026's price band 0.05 to 80.05", r["why"])
        self.assertTrue(ticket.paper_fill_price(o, ticket.quote_for(MKT, o), {}, bands[ticket.contract_key(o)])["ok"]
                        is False)                                            # 85 > the 44.30 bid: it waits

    def test_partial_fills_are_seeded_and_never_split_a_basket(self):
        self.cfg(paper_partial_fills=True, paper_partial_chance=1.0)
        one = jload("trader/orders.json")
        two = json.loads(json.dumps(one))
        for o in two["orders"]:
            o.update(lots=2, qty=130)
        jsave("trader/orders.json", two)
        self.pass_current()
        self.ok("confirm", "--basket", "--yes-text", "yes", "--now", NOW)
        code, _, err = tk("fill", "--basket", "--now", "2026-11-02 10:01")
        self.assertEqual(code, 1)
        self.assertIn("fills whole or not at all", err)
        self.assertFalse(os.path.exists("trader/portfolio.json"))
        # one leg alone may come in parts: a 6-lot hedge against 4 lots (260) at the ask, seeded
        jsave("trader/orders.json", one)
        shutil.copy(os.path.join(FNO, "risk-pass-iron-condor.json"), "trader/state/risk.json")
        self.cfg(paper_partial_fills=False)
        self.place(NOW, "2026-11-02 10:02")
        self.cfg(paper_partial_fills=True, paper_partial_chance=1.0)
        self.ok("adjust", "--strategy", SID, "--add-hedge", "BUY 6 PE 22700", "--now", "2026-11-02 10:30")
        self.assertEqual(self.orders()[0]["qty"], 390)
        self.pass_current()
        self.ok("confirm", "--basket", "--yes-text", "yes", "--now", "2026-11-02 10:30")
        out = self.ok("fill", "--basket", "--now", "2026-11-02 10:31")
        self.assertIn("Part filled", out)
        rng = random.Random("7|%s" % ticket.basket_id(self.orders()))
        rng.random()
        first = min(260 // 65, rng.randint(1, 5)) * 65
        filled = [t for t in jload("trader/state/trades.json")["trades"] if t.get("leg") == "basket leg 1"
                  and t["basket_id"] == ticket.basket_id(self.orders())]
        self.assertEqual(filled[0]["qty"], first)
        again = ticket.partial_lots(self.orders()[0], ticket.quote_for(MKT, self.orders()[0]),
                                    {"paper_partial_fills": True, "paper_partial_chance": 1.0},
                                    random.Random("7|%s" % ticket.basket_id(self.orders())), 390)
        self.assertEqual(again, first)                                       # same seed, same fill
        self.cfg(paper_partial_fills=False)
        self.ok("fill", "--basket", "--now", "2026-11-02 10:32")
        held = next(p for p in self.pf()["fno_positions"] if p["strike"] == 22700.0)
        self.assertEqual(held["qty"], 65 + 390)
        self.assertIn("DONE: placed, filled", tk("show", "--now", "2026-11-02 10:33")[1])


class Settlement(Base):
    def test_nse_final_settlement_files(self):
        a = ticket.load_settlement(os.path.join(FNO, "FOSett_prce_27102026.csv"))
        b = ticket.load_settlement(os.path.join(FNO, "BhavCopy_NSE_FO_20261027-NIFTY-expiry.csv"))
        self.assertEqual(a["FUT"][("NIFTY", "2026-10-27")], 22749.75)
        self.assertEqual(a["OPT"][("NIFTY", "2026-10-27")], 22749.75)
        self.assertNotIn(("NIFTY", "2026-11-23"), a["FUT"])                  # not expiring that day
        self.assertEqual(b["OPT"][("NIFTY", "2026-10-27")], 22749.75)
        self.assertEqual(b["FUT"][("NIFTY", "2026-10-27")], 22749.75)

    def test_settle_index_options_and_a_future_at_the_real_price(self):
        pf = {"cash": 100000.0, "positions": {}}
        ticket.migrate_portfolio(pf)
        now = ticket.parse_now("2026-10-26 10:00")

        def o(side, t, k, px, lots=1):
            leg = {"symbol": "NIFTY", "exchange": "NFO", "segment": "FUT" if t == "FUT" else "OPT", "expiry": "2026-10-27",
                   "strike": k, "option_type": None if t == "FUT" else t, "lot_size": 65, "qty": 65 * lots,
                   "product": "NRML"}
            ch = ticket.side_charges(side, px, 65 * lots, {"broker_profile": "zerodha"}, "NRML", leg["segment"], "2026-10-26")
            ticket.apply_fno_fill(pf, "S-20261026-1", leg, side, 65 * lots, px, ch["total"], now)
        o("BUY", "CE", 22700.0, 60.0)       # ITM by 49.75 at 22,749.75: exercised, STT on exercise
        o("SELL", "CE", 22800.0, 20.0)      # OTM: keeps the premium
        o("BUY", "PE", 23000.0, 240.0)      # ITM by 250.25
        o("SELL", "PE", 22700.0, 5.0)       # OTM
        o("BUY", "FUT", None, 22800.0)      # settles at 22,749.75: -50.25 x 65
        pf["strategies"]["S-20261026-1"].update(name="test", margin=0.0, status="open")
        jsave("trader/portfolio.json", pf)
        cash0 = pf["cash"]
        shutil.copy(os.path.join(FNO, "BhavCopy_NSE_FO_20261027-NIFTY-expiry.csv"), "trader/bhav.csv")
        out = self.ok("settle", "--date", "2026-10-27", "--settlement", "trader/bhav.csv", "--now", "2026-10-27 16:00")
        self.assertIn("NIFTY 22800 CE 27-Oct-2026 -65: expired worthless", out)
        self.assertIn("NIFTY FUT 27-Oct-2026 +65: settled at 22749.75", out)
        stt = [float(paisa(d(49.75) * 65 * Decimal("0.15") / 100)), float(paisa(d(250.25) * 65 * Decimal("0.15") / 100))]
        self.assertEqual(stt, [4.85, 24.40])
        p = self.pf()
        want = cash0 + 49.75 * 65 - stt[0] + 250.25 * 65 - stt[1] + (22749.75 - 22800.0) * 65
        self.assertEqual(p["cash"], round(want, 2))
        self.assertEqual(p["fno_positions"], [])
        self.assertEqual(p["strategies"]["S-20261026-1"]["status"], "closed")
        trades = jload("trader/state/trades.json")["trades"]
        self.assertEqual(sorted(t["leg"] for t in trades), ["exercised", "exercised", "expired worthless",
                                                            "expired worthless", "settled"])

    def test_no_price_no_settlement(self):
        self.place()
        code, _, err = tk("settle", "--date", "2026-11-03", "--now", "2026-11-03 16:00")
        self.assertEqual(code, 1)
        self.assertIn("no final settlement price for NIFTY 2026-11-03", err)
        self.assertEqual(len(self.pf()["fno_positions"]), 4)

    def test_stock_options_warn_of_physical_settlement(self):
        pf = {"cash": 100000.0, "positions": {}}
        ticket.migrate_portfolio(pf)
        now = ticket.parse_now("2026-11-20 10:00")
        leg = {"symbol": "RELIANCE", "exchange": "NFO", "segment": "OPT", "expiry": "2026-11-23", "strike": 1200.0,
               "option_type": "CE", "lot_size": 500, "qty": 500, "product": "NRML"}
        ticket.apply_fno_fill(pf, "S-20261120-1", leg, "BUY", 500, 30.0, 20.0, now)
        v = ticket.mtm_view(pf, {}, now)
        self.assertIn("physically settled", v["positions"][0]["warning"])
        r = ticket.settle_position(pf, pf["fno_positions"][0], 1250.0, {}, now, "2026-11-23")
        self.assertIn("take delivery of 500 shares of RELIANCE", r["note"])


class Ledger(Base):
    def test_old_portfolio_is_migrated_and_kept(self):
        old = {"cash": 87681.05, "positions": {"TCS": {"qty": 5, "avg_price": 2460.0, "product": "CNC",
                                                      "buy_charges": 18.95}}}
        jsave("trader/portfolio.json", old)
        pf = ticket.load_portfolio("trader/portfolio.json", {})
        self.assertEqual((pf["schema"], pf["cash"], pf["positions"], pf["margin_blocked"], pf["fno_positions"],
                          pf["migrated_from"]), (ticket.PORTFOLIO_SCHEMA, 87681.05, old["positions"], 0.0, [], "1"))
        listed = {"cash": 1000, "positions": [{"symbol": "sbin", "qty": 10, "avg_price": 800}]}
        jsave("trader/p2.json", listed)
        self.assertEqual(ticket.load_portfolio("trader/p2.json", {})["positions"]["SBIN"]["qty"], 10)
        ticket.save_portfolio("trader/portfolio.json", pf)
        self.assertEqual(jload("trader/portfolio.json.v1.bak"), old)
        self.assertEqual(jload("trader/portfolio.json")["schema"], ticket.PORTFOLIO_SCHEMA)
        again = ticket.load_portfolio("trader/portfolio.json", {})
        self.assertIsNone(ticket.migrate_portfolio(again))
        ticket.save_portfolio("trader/portfolio.json", again)
        self.assertEqual(jload("trader/portfolio.json.v1.bak"), old)        # written once, never overwritten
        # trader-risk's reader keeps the share position and the F&O ones apart
        code, text, _ = tk("status", "--now", NOW)
        self.assertIn("TCS 5 shares", text)

    def test_status_and_flatten_know_the_fno_book(self):
        self.place()
        text = self.ok("status", "--now", "2026-11-02 10:05")
        self.assertIn("Margin blocked", text)
        self.assertIn("F&O %s iron-condor on NIFTY" % SID, text)
        code, _, err = tk("flatten", "--now", "2026-11-02 15:00")
        self.assertEqual(code, 1)
        self.assertIn("ticket.py adjust --strategy ID --close-all for %s (NRML)" % SID, err)


class Profiles(unittest.TestCase):
    def test_brokerage_by_profile(self):
        def eq(profile, product, value=100000.0):
            return ticket.side_charges("BUY", value, 1, {"broker_profile": profile}, product)["brokerage"]
        self.assertEqual(eq("zerodha", "MIS"), 20.0)
        self.assertEqual(eq("zerodha", "CNC"), 0.0)
        self.assertEqual(eq("upstox", "CNC"), 20.0)
        self.assertEqual(eq("upstox", "MIS", 10000.0), 10.0)               # 0.1% under the Rs 20 cap
        self.assertEqual(eq("dhan", "CNC"), 0.0)
        self.assertEqual(eq("angel", "CNC", 5000.0), 5.0)
        self.assertEqual(eq("groww", "MIS", 50000.0), 20.0)
        self.assertEqual(eq("", "CNC"), 20.0)                               # no profile: the defaults (0.03%, Rs 20)

        def fut(profile, value=1500000.0):
            return ticket.side_charges("SELL", value, 1, {"broker_profile": profile}, "NRML", "FUT", "2026-11-02")
        self.assertEqual(fut("zerodha")["brokerage"], 20.0)
        self.assertEqual(fut("upstox", 30000.0)["brokerage"], 15.0)        # 0.05%
        self.assertEqual(fut("dhan", 30000.0)["brokerage"], 20.0)          # flat
        self.assertEqual(fut("groww")["stt"], 750.0)                       # STT is the same everywhere: 0.05%
        self.assertEqual(ticket.side_charges("BUY", 50.0, 65, {"broker_profile": "angel"}, "NRML", "OPT",
                                             "2026-11-02")["brokerage"], 20.0)
        own = {"broker_profile": "custom", "fno_charges": {"OPT": {"brokerage_flat": 10}}, "square_off": {}}
        self.assertEqual(ticket.side_charges("BUY", 50.0, 65, own, "NRML", "OPT", "2026-11-02")["brokerage"], 10.0)
        for name, p in ticket.CHARGE_PROFILES.items():
            self.assertIn("read 1 Nov 2026", p["source"], name)
            self.assertIn(name, ticket.BROKER_SQUARE_OFF)                    # the same names as the square-off table

    def test_own_black76_is_trader_options(self):
        for F, K, T, s, k in ((23022.14, 23200.0, 0.004, 0.1162, "CE"), (23022.14, 22700.0, 0.004, 0.1379, "PE")):
            a, b = ticket._b76_greeks(F, K, T, 0.0557, s, k), op.black76_greeks(F, K, T, 0.0557, s, k)
            for g in ("delta", "gamma", "theta", "vega"):
                self.assertAlmostEqual(a[g], b[g], 10)
            self.assertAlmostEqual(ticket._b76_price(F, K, T, 0.0557, s, k), op.black76_price(F, K, T, 0.0557, s, k), 10)


class Shadow(unittest.TestCase):
    def test_twin_and_report(self):
        now = ticket.parse_now("2026-10-29 09:44")
        tmp = tempfile.mkdtemp()
        try:
            prices = os.path.join(tmp, "prices.json")
            jsave(prices, {"RELIANCE": {"price": 1174.0, "time": "2026-10-29T09:43:00+05:30"}})
            single = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
                      "order_type": "LIMIT", "price": 1174.30, "validity": "DAY"}
            tw = ticket.shadow_twin(single, {"paper_slippage_bps": 2}, prices, {}, now)
            self.assertTrue(tw["filled"])
            self.assertEqual(tw["price"], 1174.25)                         # 1174.00 + 2 bps = 1174.2348, up to 0.05
            self.assertEqual(tw["charges"], ticket.side_charges("BUY", 1174.25, 42, {}, "MIS")["total"])
            doc = {"orders": [{"broker_order_id": "151220000000000", "side": "BUY", "qty": 42, "paper": tw},
                              {"broker_order_id": "x", "side": "BUY", "qty": 1, "paper": tw}]}
            rep = ticket.shadow_report(doc, {"151220000000000": {"status": "filled", "avg_price": 1174.30,
                                                                 "filled_qty": 42}})
            self.assertEqual(rep["compared"], 1)
            self.assertEqual(rep["orders"][0]["diff_per_unit"], -0.05)     # live paid 5 paise more than paper
            self.assertEqual(rep["total_diff"], -2.1)
            self.assertEqual(rep["orders"][1]["live_status"], "not in the order book")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
