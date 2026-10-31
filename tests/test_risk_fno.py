"""trader-risk v2 tests, offline: F&O baskets, the ban list, expiry day, portfolio Greeks and scenarios,
the behavioural circuit-breakers, the kill switch and the order rate.

Fixtures, all REAL NSE files:
  trader-options/chain-NIFTY-03-Nov-2026.json.gz, chain-NIFTY-01-Dec-2026.json.gz
                                  the option chains of Fri 30-Oct-2026 15:40 (reused), priced by
                                  trader-options and turned into baskets by trader-strategy
  trader-options/fo_mktlots.csv   NSE lot sizes (NIFTY 65, RELIANCE 500, KAYNES 150), trimmed (reused)
  trader-risk/fo_secban.csv       NSE's F&O ban list for trade date 02-Nov-2026 (KAYNES, SAIL), as
                                  published on 30-Oct-2026 evening
  trader-risk/fo_secban_30102026.csv   the dated ban list of trade date 30-Oct-2026
  trader-risk/FOSett_prce_30102026.csv NSE's F&O MTM settlement prices of 30-Oct-2026 (NIFTY, KAYNES,
                                  RELIANCE, SAIL futures, NSE expiries only)
The trade logs for the circuit-breakers are simulated (a list of fills with a realised P&L).
"""

import copy
import csv
import gzip
import io
import json
import math
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

check = load("trader-risk", "check")
st = load("trader-strategy", "strategy")
op = load("trader-options", "options")

OPT = os.path.join(FIX, "trader-options")
RISK = os.path.join(FIX, "trader-risk")
MON = "2026-11-02 10:00"          # Monday: the 03-Nov weekly expires tomorrow
EXP = "2026-11-03 10:00"          # Tuesday: expiry day of the 03-Nov weekly
TODAY = "2026-11-02"


def no_network(url, timeout=30, retries=2):
    raise check.RulesError("no network in the tests")


check.nse_get = no_network


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


LOTS = check.parse_lots(text(os.path.join(OPT, "fo_mktlots.csv")))
BAN = check.parse_ban(text(os.path.join(RISK, "fo_secban.csv")))


def settle():
    """{(underlying, expiry ISO): settlement price} from NSE's FOSett_prce file."""
    out = {}
    with open(os.path.join(RISK, "FOSett_prce_30102026.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["INSTRUMENT"].startswith("FUT"):
                d = check.parse_day(row["EXPIRY DATE"].title())
                out[(row["UNDERLYING"], d)] = float(row["MTM SETTLEMENT PRICE"])
    return out


SETTLE = settle()


def analysis(expiry="03-Nov-2026", sym="NIFTY"):
    with gzip.open(os.path.join(OPT, "chain-%s-%s.json.gz" % (sym, expiry)), "rb") as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    chain = op.parse_chain(raw, sym)
    return op.analyse(chain, op.lot_size(op.parse_lots(text(os.path.join(OPT, "fo_mktlots.csv"))), sym,
                                          chain["expiry"]), 0.0557, now=op.parse_nse_time("2026-11-01 10:00"))


NIFTY = analysis()
NIFTY_DEC = analysis("01-Dec-2026")
MKT = {NIFTY["expiry"]: NIFTY}


def strategy(name, params=None, markets=None):
    """trader-strategy's strategy.json body for a template, as written to disk (JSON round trip)."""
    markets = markets or MKT
    near = min(markets)
    legs = st.resolve_legs(st.template_specs(name, params or {}, markets[near]), markets, near)
    doc = st.evaluate(legs, markets, {}, name, params, simulate=False)
    return json.loads(json.dumps(doc))


def orders_of(doc):
    return json.loads(json.dumps(st.orders_document(doc)))


CONDOR = strategy("iron-condor", {"wings": 200, "width": 100})
STRANGLE = strategy("short-strangle", {"delta": 0.16})
STRADDLE = strategy("long-straddle")

BASE = {"capital": 500000, "allow_fno": True, "max_loss_per_trade": 5000, "max_daily_loss": 10000,
        "max_trades_per_day": 5, "max_orders_per_day": 20}


def cfg(**over):
    c = dict(BASE)
    c.update(over)
    return c


def run(orders, c=None, now=MON, portfolio=None, state=None, prices=None, basket=True, doc=None, ban=BAN,
        trades=None, kill=None, options=None):
    extra = {"basket": basket, "strategy": doc, "options": options, "lots": LOTS, "lots_note": "fixture",
             "ban": None if ban is None else set(ban["symbols"]), "ban_note": "fixture", "trades": trades,
             "kill": kill}
    return check.check(orders, cfg() if c is None else c, portfolio or {"cash": 500000}, prices or {}, state,
                       None, check.parse_now(now), {}, extra)


def tags(r):
    return sorted({x.split(":")[0] for x in r["reasons"]})


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


FUT_CFG = dict(BASE, max_scenario_loss=0)          # a future's grid ignores its stop: the loss rules bind


def fut(sym, expiry, side="BUY", lots=1, **kw):
    lot = check.lot_for(LOTS, sym, expiry)
    o = {"symbol": sym, "exchange": "NFO", "segment": "FUT", "instrument": "FUTSTK", "expiry": expiry,
         "side": side, "qty": lot * lots, "lot_size": lot, "product": "NRML", "order_type": "LIMIT",
         "price": SETTLE[(sym, expiry)], "validity": "DAY",
         "stop_loss": {"order_type": "SL-M", "trigger_price": check.tick_round(
             SETTLE[(sym, expiry)] * (0.995 if side == "BUY" else 1.005))}}
    o.update(kw)
    return o


def stock_prices():
    return {"KAYNES": SETTLE[("KAYNES", "2026-11-23")], "RELIANCE": SETTLE[("RELIANCE", "2026-11-23")],
            "SAIL": SETTLE[("SAIL", "2026-11-23")]}


# ---------------------------------------------------------------- contracts, lots, the ban list

class Contracts(unittest.TestCase):
    def test_trading_symbols_and_strategy_fields(self):
        k, why = check.contract_of({"symbol": "NIFTY26NOV23000CE"})
        self.assertIsNone(why)
        # monthly: last Tuesday of November is 24-Nov (Guru Nanak Jayanti), so NSE's 23-Nov, as in the
        # settlement file
        self.assertEqual((k["expiry"], k["strike"], k["option_type"]), ("2026-11-23", 23000.0, "CE"))
        self.assertIn(("NIFTY", "2026-11-23"), SETTLE)
        self.assertIn(("NIFTY", "2026-12-29"), SETTLE)
        self.assertEqual(check.monthly_expiry(2026, 12).isoformat(), "2026-12-29")
        k, _ = check.contract_of({"symbol": "NIFTY26N0322800PE"})
        self.assertEqual((k["expiry"], k["option_type"], k["kind"]), ("2026-11-03", "PE", "index"))
        k, _ = check.contract_of({"symbol": "RELIANCE26NOVFUT"})
        self.assertEqual((k["instrument"], k["kind"]), ("FUT", "stock"))
        leg = CONDOR["orders"][0]
        k, _ = check.contract_of(leg)
        self.assertEqual((k["underlying"], k["expiry"], k["strike"]), ("NIFTY", "2026-11-03", leg["strike"]))
        self.assertIsNone(check.contract_of({"symbol": "NIFTY", "expiry": "2026-11-03", "option_type": "XX",
                                             "strike": 23000})[0])

    def test_lots_per_expiry_month_from_nse(self):
        self.assertEqual(check.lot_for(LOTS, "NIFTY", "2026-11-03"), 65)
        self.assertEqual(check.lot_for(LOTS, "RELIANCE", "2026-11-23"), 500)
        self.assertEqual(check.lot_for(LOTS, "KAYNES", "2026-12-29"), 150)

    def test_lot_multiple_pass_and_block(self):
        res = run([fut("RELIANCE", "2026-11-23")], c=FUT_CFG, prices=stock_prices(), basket=False)
        self.assertEqual(verdicts(res), ["PASS"], res["results"][0]["reasons"])
        self.assertEqual(res["results"][0]["lots"], 1.0)
        res = run([fut("RELIANCE", "2026-11-23", qty=450)], prices=stock_prices(), basket=False)
        self.assertIn("lot", tags(res["results"][0]))
        self.assertIn("not a multiple of the RELIANCE lot of 500", " ".join(res["results"][0]["reasons"]))
        res = run([fut("RELIANCE", "2026-11-23", lot_size=250, qty=500)], prices=stock_prices(), basket=False)
        self.assertIn("NSE's fo_mktlots.csv says 500", " ".join(res["results"][0]["reasons"]))

    def test_nse_ban_list_files(self):
        self.assertEqual(BAN, {"trade_date": "2026-11-02", "symbols": ["KAYNES", "SAIL"]})
        old = check.parse_ban(text(os.path.join(RISK, "fo_secban_30102026.csv")))
        self.assertEqual(old["trade_date"], "2026-10-30")
        with self.assertRaises(check.RulesError):
            check.parse_ban("<html>not the file</html>")

    def test_ban_blocks_new_positions_in_a_banned_stock(self):
        res = run([fut("KAYNES", "2026-11-23")], prices=stock_prices(), basket=False)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("ban", tags(r))
        self.assertIn("KAYNES is in NSE's F&O ban for 2026-11-02", " ".join(r["reasons"]))
        # not banned: passes on the same list
        self.assertEqual(verdicts(run([fut("RELIANCE", "2026-11-23")], c=FUT_CFG, prices=stock_prices(),
                                      basket=False)), ["PASS"])
        # an index is never in the ban
        self.assertNotIn("ban", tags(run(orders_of(CONDOR)["orders"], doc=CONDOR, ban=None)["results"][0]))

    def test_ban_closing_a_position_is_still_allowed(self):
        pf = {"cash": 500000, "margin_blocked": 90000,
              "positions": [{"symbol": "KAYNES", "exchange": "NFO", "expiry": "2026-11-23", "qty": 150,
                             "avg_price": 3000.0, "lot_size": 150}]}
        o = fut("KAYNES", "2026-11-23", side="SELL")
        o.pop("stop_loss")
        r = run([o], portfolio=pf, prices=stock_prices(), basket=False)["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        self.assertTrue(any("exit, not blocked: ban" in w for w in r["warnings"]))

    def test_unknown_ban_list(self):
        r = run([fut("RELIANCE", "2026-11-23")], prices=stock_prices(), basket=False, ban=None)["results"][0]
        self.assertIn("ban", tags(r))
        self.assertIn("not known", " ".join(r["reasons"]))
        r = run([fut("RELIANCE", "2026-11-23")], c=dict(FUT_CFG, require_ban_list=False), prices=stock_prices(),
                basket=False,
                ban=None)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("ban list" in w for w in r["warnings"]))

    def test_ban_cache_and_lot_file_offline(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "fo_secban.json")
            syms, note = check.load_ban(cache, TODAY, fetch=True)       # no network: unknown, says why
            self.assertIsNone(syms)
            self.assertIn("no network", note)
            with open(cache, "w") as fh:
                json.dump({"days": {TODAY: {"symbols": BAN["symbols"]}}}, fh)
            syms, note = check.load_ban(cache, TODAY)
            self.assertEqual(syms, {"KAYNES", "SAIL"})
            self.assertIn("KAYNES, SAIL", note)
            lots, note = check.load_lots(os.path.join(OPT, "fo_mktlots.csv"))
            self.assertEqual(lots["NIFTY"]["2026-11"], 65)
            self.assertIsNone(check.load_lots(os.path.join(d, "none.csv"))[0])

    def test_contract_format_rules(self):
        legs = orders_of(CONDOR)["orders"]
        bad = copy.deepcopy(legs)
        bad[1]["exchange"] = "BFO"
        res = run(bad, doc=CONDOR)
        self.assertIn("exchange", tags(res["results"][1]))
        bad = copy.deepcopy(legs)
        bad[2]["price"] = bad[2]["price"] + 0.02
        res = run(bad, doc=CONDOR)
        self.assertIn("tick", tags(res["results"][2]))
        old = [fut("RELIANCE", "2026-11-23")]
        res = run(old, now="2026-11-23 15:31", prices=stock_prices(), basket=False)
        self.assertIn("expiry", tags(res["results"][0]))

    def test_fno_stays_off_by_default(self):
        res = run(orders_of(CONDOR)["orders"], c={"capital": 500000}, doc=CONDOR)
        self.assertTrue(all("fno" in tags(r) for r in res["results"]))


# ---------------------------------------------------------------- one basket = one position

class Baskets(unittest.TestCase):
    def test_hedged_condor_passes_as_one_position(self):
        res = run(orders_of(CONDOR)["orders"], doc=CONDOR)
        self.assertEqual(verdicts(res), ["PASS"] * 4, [r["reasons"] for r in res["results"]])
        b = res["results"][0]["basket"]
        self.assertTrue(b["defined_risk"])
        self.assertEqual(len({r["basket_id"] for r in res["results"]}), 1)
        # the most it can lose: (100 width - net credit) x 65 + charges, close to trader-strategy's own
        self.assertAlmostEqual(b["loss_used"], -CONDOR["expiry"]["max_loss_after_charges"], delta=60)
        self.assertGreater(b["loss_used"], -CONDOR["expiry"]["max_loss"])
        self.assertEqual(round(sum(r["max_loss"] for r in res["results"]), 2), b["loss_used"])
        self.assertIn("trader-strategy", b["margin"]["source"])
        self.assertEqual(b["margin"]["used"], CONDOR["margin"]["high"])

    def test_the_same_legs_one_by_one_are_naked_shorts(self):
        res = run(orders_of(CONDOR)["orders"], doc=CONDOR, basket=False)
        by_side = {r["side"]: r for r in res["results"]}
        self.assertEqual([r["verdict"] for r in res["results"] if r["side"] == "BUY"], ["PASS", "PASS"])
        for r in res["results"]:
            if r["side"] == "SELL":
                self.assertEqual(r["verdict"], "BLOCK")
                self.assertIn("naked", tags(r))
                self.assertIn("stop", tags(r))
        self.assertTrue(by_side)

    def test_basket_ids_group_legs_without_the_file_flag(self):
        legs = orders_of(CONDOR)["orders"]
        for o in legs:
            o["basket_id"] = "ic-1"
        self.assertEqual(verdicts(run(legs, doc=CONDOR, basket=False)), ["PASS"] * 4)

    def test_all_or_nothing(self):
        legs = orders_of(CONDOR)["orders"]
        legs[3]["qty"] = 60                      # not a lot multiple
        res = run(legs, doc=CONDOR)
        self.assertEqual(verdicts(res), ["BLOCK"] * 4)
        self.assertIn("lot", tags(res["results"][3]))
        self.assertIn("leg #4 is blocked, and a basket is placed whole or not at all",
                      " ".join(res["results"][0]["reasons"]))

    def test_naked_strangle_needs_the_switch_and_a_stop_rule(self):
        legs = orders_of(STRANGLE)["orders"]
        loose = dict(max_scenario_loss=0, max_net_vega=0)
        r = run(legs, c=cfg(**loose), doc=STRANGLE)["results"][0]
        self.assertEqual(tags(r), ["naked", "stop"])
        self.assertTrue(r["basket"]["unlimited_loss"])
        r = run(legs, c=cfg(allow_naked_short=True, **loose), doc=STRANGLE)["results"][0]
        self.assertEqual(tags(r), ["stop"])
        for o in legs:
            o["risk_rule"] = {"max_loss": 3000}
        res = run(legs, c=cfg(allow_naked_short=True, **loose), doc=STRANGLE)
        self.assertEqual(verdicts(res), ["PASS", "PASS"], res["results"][0]["reasons"])
        self.assertIn("risk_rule exits at a ₹3,000.00 loss", res["results"][0]["basket"]["loss_from"])
        # an underlying stop must cover both sides of a strangle
        for o in legs:
            o["risk_rule"] = {"stop_underlying": {"above": 23400}}
        r = run(legs, c=cfg(allow_naked_short=True, **loose), doc=STRANGLE)["results"][0]
        self.assertIn("\"below\"", " ".join(r["reasons"]))
        for o in legs:
            o["risk_rule"] = {"stop_underlying": {"above": 23400, "below": 22650}}
        r = run(legs, c=cfg(allow_naked_short=True, max_loss_per_trade=8000, **loose), doc=STRANGLE)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_defined_risk_as_stop_is_configurable(self):
        legs = orders_of(CONDOR)["orders"]
        r = run(legs, c=cfg(defined_risk_counts_as_stop=False), doc=CONDOR)["results"][0]
        self.assertIn("stop", tags(r))
        for o in legs:
            o["risk_rule"] = {"max_loss": 2500}
        r = run(legs, c=cfg(defined_risk_counts_as_stop=False), doc=CONDOR)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_loss_limits(self):
        r = run(orders_of(CONDOR)["orders"], c=cfg(max_loss_per_trade=3000), doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["loss"])
        state = {"orders": [{"symbol": "SBIN", "side": "BUY", "status": "placed", "date": TODAY, "kind": "entry",
                             "at": TODAY + "T09:35:00+05:30", "max_loss": 7000}]}
        r = run(orders_of(CONDOR)["orders"], state=state, doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["daily loss"])

    def test_premium_budget(self):
        legs = orders_of(STRADDLE)["orders"]
        paid = sum(o["price"] * o["qty"] for o in legs)
        r = run(legs, c=cfg(max_premium_per_trade=paid - 100, max_scenario_loss=0), doc=STRADDLE)["results"][0]
        self.assertIn("premium", tags(r))
        self.assertIn("max_premium_per_trade", " ".join(r["reasons"]))
        r = run(legs, c=cfg(max_premium_per_trade=paid + 100, max_premium_per_day=paid + 100, max_scenario_loss=0,
                            max_loss_per_trade=paid + 500, max_daily_loss=paid + 500), doc=STRADDLE)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        # a credit basket pays no net premium
        self.assertEqual(run(orders_of(CONDOR)["orders"], c=cfg(max_premium_per_trade=1), doc=CONDOR)["pass"], 4)

    def test_max_lots_per_underlying(self):
        three = strategy("iron-condor", {"wings": 200, "width": 100, "lots": 3})
        r = run(orders_of(three)["orders"], c=cfg(max_loss_per_trade=20000, max_daily_loss=40000,
                                                  max_scenario_loss=0), doc=three)["results"][0]
        self.assertEqual(tags(r), ["lots"])
        self.assertIn("over your 2 lots per underlying", " ".join(r["reasons"]))
        r = run(orders_of(three)["orders"], c=cfg(max_loss_per_trade=20000, max_daily_loss=40000, max_scenario_loss=0,
                                                  max_lots_per_underlying=3), doc=three)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_margin_against_cash_after_the_buffer(self):
        need = CONDOR["margin"]["high"]
        r = run(orders_of(CONDOR)["orders"], c=cfg(capital=100000), portfolio={"cash": need}, doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["margin"])
        self.assertIn("10% cash buffer", " ".join(r["reasons"]))
        res = run(orders_of(CONDOR)["orders"], c=cfg(capital=100000), portfolio={"cash": need + 10000.5}, doc=CONDOR)
        self.assertEqual(res["pass"], 4, res["results"][0]["reasons"])
        self.assertAlmostEqual(res["cash_after"], 10000.5, places=2)       # the margin is set aside
        # margin already blocked by open positions counts
        r = run(orders_of(CONDOR)["orders"], c=cfg(capital=100000),
                portfolio={"cash": need + 10000.5, "margin_blocked": 1000}, doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["margin"])

    def test_own_margin_estimate_without_the_strategy_file(self):
        res = run(orders_of(CONDOR)["orders"], doc=None, options=NIFTY)
        m = res["results"][0]["basket"]["margin"]
        self.assertIn("trader-risk", m["source"])
        # the same SPAN-style method as trader-strategy, on the limit prices
        self.assertAlmostEqual(m["estimate"], CONDOR["margin"]["required"], delta=0.05 * CONDOR["margin"]["required"])


class ExpiryDay(unittest.TestCase):
    def test_extra_elm_on_short_index_options_expiring_today(self):
        legs = orders_of(STRANGLE)["orders"]
        loose = cfg(allow_naked_short=True, max_scenario_loss=0, max_net_vega=0, capital=2000000)
        for o in legs:
            o["risk_rule"] = {"max_loss": 3000}
        mon = run(legs, c=loose, portfolio={"cash": 2000000}, doc=STRANGLE)["results"][0]
        tue = run(legs, c=loose, now=EXP, portfolio={"cash": 2000000}, doc=STRANGLE)["results"][0]
        self.assertEqual(mon["basket"]["margin"]["expiry_day_elm"], 0.0)
        F = tue["basket"]["margin"]
        shorts = [o for o in legs if o["side"] == "SELL"]
        fwd = STRANGLE["forward"]
        self.assertAlmostEqual(F["expiry_day_elm"], sum(fwd * o["qty"] * 0.02 for o in shorts), delta=1.0)
        self.assertAlmostEqual(F["expiry_day_elm_added"], F["expiry_day_elm"], places=2)
        self.assertAlmostEqual(F["used"] - mon["basket"]["margin"]["used"], F["expiry_day_elm"], delta=1.0)
        self.assertTrue(any("expiry day" in w and "2% more margin" in w for w in tue["warnings"]))

    def test_no_calendar_benefit_on_expiry_day(self):
        markets = {NIFTY["expiry"]: NIFTY, NIFTY_DEC["expiry"]: NIFTY_DEC}
        cal = strategy("calendar", {"far_expiry": NIFTY_DEC["expiry"]}, markets)
        legs = orders_of(cal)["orders"]
        c = cfg(max_scenario_loss=0, max_net_vega=0, max_loss_per_trade=50000, max_daily_loss=50000)
        mon = run(legs, c=c, doc=cal)["results"][0]["basket"]["margin"]
        tue = run(legs, c=c, now=EXP, doc=cal)["results"][0]["basket"]["margin"]
        self.assertTrue(mon["calendar_benefit"])
        self.assertFalse(tue["calendar_benefit"])
        self.assertGreaterEqual(tue["estimate"], tue["without_calendar_benefit"] - 0.01)
        self.assertGreater(tue["without_calendar_benefit"], mon["estimate"])


# ---------------------------------------------------------------- portfolio Greeks and the scenario grid

SHORT_FUT = {"cash": 500000, "margin_blocked": 160000,
             "positions": [{"symbol": "NIFTY", "exchange": "NFO", "segment": "FUT", "expiry": "2026-11-23",
                            "qty": -130, "avg_price": 23098.9, "lot_size": 65, "ltp": 23098.9}]}


class Portfolio(unittest.TestCase):
    def test_delta_limit_and_a_hedge_always_passes(self):
        bull = strategy("bull-call-spread")
        legs = orders_of(bull)["orders"]
        pf = {"cash": 500000, "margin_blocked": 160000,
              "positions": [{"symbol": "NIFTY", "exchange": "NFO", "segment": "FUT", "expiry": "2026-11-23",
                             "qty": 130, "avg_price": 23098.9, "lot_size": 65, "ltp": 23098.9}]}
        r = run(legs, c=cfg(max_lots_per_underlying=0, max_scenario_loss=0), portfolio=pf, doc=bull)["results"][0]
        self.assertEqual(tags(r), ["greeks"])
        self.assertIn("NIFTY net delta would be +2.", " ".join(r["reasons"]))
        # the same spread against a short future reduces the delta: it passes
        r = run(legs, c=cfg(max_lots_per_underlying=0, max_scenario_loss=0), portfolio=SHORT_FUT,
                doc=bull)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_vega_gamma_and_theta_limits(self):
        legs = orders_of(STRADDLE)["orders"]
        c = cfg(max_scenario_loss=0, max_loss_per_trade=20000, max_daily_loss=20000)
        r = run(legs, c=dict(c, max_net_vega=100), doc=STRADDLE)["results"][0]
        self.assertEqual(tags(r), ["greeks"])
        self.assertIn("net vega", " ".join(r["reasons"]))
        r = run(legs, c=dict(c, max_net_vega=0, max_theta_paid_per_day=100), doc=STRADDLE)["results"][0]
        self.assertIn("time decay (theta)", " ".join(r["reasons"]))
        r = run(legs, c=dict(c, max_net_vega=0, max_net_gamma_lots_1pct=0.1), doc=STRADDLE)["results"][0]
        self.assertIn("gamma would move the delta", " ".join(r["reasons"]))
        self.assertEqual(run(legs, c=dict(c, max_net_vega=0), doc=STRADDLE)["pass"], 2)

    def test_scenario_grid_numbers_on_the_fixture(self):
        res = run(orders_of(CONDOR)["orders"], doc=CONDOR)
        g = res["portfolio"]["scenarios"]
        self.assertEqual(g["sigmas"], [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0])
        self.assertEqual(g["iv_shifts"], [-10.0, -5.0, 0.0, 5.0, 10.0])
        # one session of the at-the-money IV: 12.10% x sqrt(1/252)
        self.assertAlmostEqual(g["sigma_pct"]["NIFTY"], NIFTY["atm_iv"] * math.sqrt(1 / 252.0), places=3)
        # the +2 sigma, +5 vol cell by hand: Black-76 on every leg, one day later, minus each leg's round trip
        legs = res["results"]
        F = CONDOR["forward"]
        s = CONDOR["atm_iv"] / 100.0 * math.sqrt(1 / 252.0)
        now = check.parse_now(MON)
        T = check.years_to("2026-11-03", now) - 1 / 365.0
        hand = 0.0
        for o, sleg in zip(orders_of(CONDOR)["orders"], CONDOR["legs"]):
            q = o["qty"] if o["side"] == "BUY" else -o["qty"]
            v = check.b76_price(F * (1 + 2 * s), o["strike"], T, check.DEFAULT_RATE, (sleg["iv"] + 5) / 100.0,
                                o["option_type"])
            ch = check.round_trip_charges(o["price"], v, o["qty"], {}, "OPT", TODAY) if q > 0 else \
                check.round_trip_charges(v, o["price"], o["qty"], {}, "OPT", TODAY)
            hand += (v - o["price"]) * q - ch["total"]
        row = g["rows"][g["sigmas"].index(2.0)]
        self.assertAlmostEqual(row["pnl"][g["iv_shifts"].index(5.0)], hand, delta=0.05)
        self.assertTrue(legs)
        # a defined-risk condor never loses more on the grid than its max loss (plus a day's charges)
        self.assertLessEqual(-g["worst"]["pnl"], res["results"][0]["basket"]["loss_used"] + 100)
        self.assertGreater(g["best"]["pnl"], 0)

    def test_scenario_limit_cites_the_scenario(self):
        r = run(orders_of(CONDOR)["orders"], c=cfg(max_scenario_loss=3000), doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["scenario"])
        why = " ".join(r["reasons"])
        self.assertRegex(why, r"scenario: spot [+-]\d sigma \(NIFTY [+-]\d\.\d\d%\) with IV [+-]\d+ vol: -₹[\d,]+\.\d\d "
                              r"after charges, over your ₹3,000.00 max scenario loss")

    def test_open_positions_are_in_the_book(self):
        loose = cfg(max_lots_per_underlying=0, max_net_delta_lots=0, max_scenario_loss=0)
        res = run(orders_of(CONDOR)["orders"], c=loose, portfolio=SHORT_FUT, doc=CONDOR)
        p = res["portfolio"]
        self.assertEqual(p["legs"], 5)
        self.assertLess(p["greeks"]["by_underlying"]["NIFTY"]["delta_lots"], -1.9)
        self.assertEqual([x["open"] for x in p["positions"]], [True, False, False, False, False])
        self.assertEqual(p["positions"][0]["source"], "the future's own last price")
        # the short future already loses more than the limit at +3 sigma; the condor adds to that: BLOCK
        r = run(orders_of(CONDOR)["orders"], c=dict(loose, max_scenario_loss=10000), portfolio=SHORT_FUT,
                doc=CONDOR)["results"][0]
        self.assertEqual(tags(r), ["scenario"])
        before = r["basket"]["worst_scenario_before"]["pnl"]
        self.assertLess(r["basket"]["worst_scenario_after"]["pnl"], before)

    def test_scenarios_read_only_view(self):
        view = check.scenarios(cfg(), SHORT_FUT, {}, check.parse_now(MON),
                               {"strategy": CONDOR, "lots": LOTS}, orders_of(CONDOR)["orders"])
        self.assertEqual(view["open"]["legs"], 1)
        self.assertEqual(view["with_proposals"]["legs"], 5)
        # a short future: +1 sigma loses, -1 sigma gains
        rows = {r["sigma"]: r["pnl"][2] for r in view["open"]["scenarios"]["rows"]}
        self.assertLess(rows[1.0], 0)
        self.assertGreater(rows[-1.0], 0)
        self.assertIsNone(view["kill"])
        self.assertIn("not a forecast", view["disclaimer"])


# ---------------------------------------------------------------- behaviour: circuit-breakers

def trade(at, pnl, sym="SBIN", leg="target"):
    return {"at": "%sT%s:00+05:30" % (TODAY, at), "symbol": sym, "side": "SELL", "qty": 10, "price": 800.0,
            "leg": leg, "realised_pnl": pnl, "paper": True}


def cash_order(sym="RELIANCE"):
    with open(os.path.join(RISK, "orders-reliance.json"), encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o["symbol"] = sym
    return o


CASH_CFG = {"capital": 100000, "max_position_pct": 100, "max_order_value": 200000, "cash_buffer_pct": 0}


def cash(orders, now="2026-11-02 11:00", trades=None, kill=None, c=None, portfolio=None, state=None):
    return check.check(orders, dict(CASH_CFG, **(c or {})), portfolio or {"cash": 100000}, {}, state, None,
                       check.parse_now(now), {}, {"trades": trades, "kill": kill})


HELD = {"cash": 50000, "positions": {"RELIANCE": {"qty": 42, "avg_price": 1174.3, "product": "MIS"}}}
EXIT = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 42, "order_type": "MARKET",
        "validity": "DAY"}


class Breakers(unittest.TestCase):
    def test_pass_with_a_clean_day(self):
        r = cash([cash_order()], trades=[trade("09:45", 300.0)])["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_consecutive_losses_lock_entries_not_exits(self):
        log = {"trades": [trade("09:45", 300.0), trade("10:00", -200.0), trade("10:10", -150.0),
                          trade("10:20", -100.0)]}
        r = cash([cash_order("TCS")], trades=log, c={"max_daily_loss": 0})["results"][0]
        self.assertIn("breaker", tags(r))
        self.assertIn("3 losing trades in a row today (your limit is 3)", " ".join(r["reasons"]))
        r = cash([EXIT], trades=log, portfolio=HELD)["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"))
        self.assertTrue(any("exit, not blocked: breaker" in w for w in r["warnings"]))
        # a win in between resets the streak
        log["trades"].insert(2, trade("10:05", 50.0))
        self.assertNotIn("breaker", tags(cash([cash_order("TCS")], trades=log)["results"][0]))

    def test_daily_drawdown(self):
        log = [trade("09:45", -1600.0, "TCS"), trade("10:00", 100.0), trade("10:15", -1600.0, "INFY")]
        r = cash([cash_order()], trades=log)["results"][0]
        self.assertIn("breaker", tags(r))
        self.assertIn("down ₹3,100.00 today after charges, 3.10% of ₹1,00,000.00 (your limit is 3%)",
                      " ".join(r["reasons"]))
        self.assertEqual(cash([cash_order()], trades=log, c={"max_daily_drawdown_pct": 5})["pass"], 1)

    def test_cooling_off_after_a_stop(self):
        log = [trade("10:50", -400.0, "TCS", leg="stop")]
        r = cash([cash_order()], now="2026-11-02 11:00", trades=log)["results"][0]
        self.assertIn("cooling-off", tags(r))
        self.assertIn("until 11:05 IST", " ".join(r["reasons"]))
        self.assertEqual(cash([cash_order()], now="2026-11-02 11:06", trades=log)["pass"], 1)

    def test_revenge_trade_flag(self):
        log = [trade("10:50", -400.0, "RELIANCE", leg="order")]
        r = cash([cash_order()], trades=log)["results"][0]
        self.assertEqual(tags(r), ["revenge"])
        self.assertEqual(cash([cash_order("TCS")], trades=log)["pass"], 1)
        r = cash([cash_order()], trades=log, c={"revenge_trade": "warn"})["results"][0]
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(any(w.startswith("revenge:") for w in r["warnings"]))
        self.assertEqual(cash([cash_order()], now="2026-11-02 11:06", trades=log)["pass"], 1)

    def test_yesterdays_losses_do_not_count(self):
        log = [dict(trade("10:00", -5000.0), at="2026-10-30T10:00:00+05:30")] * 4
        self.assertEqual(cash([cash_order()], trades=log)["pass"], 1)

    def test_daily_trade_cap_counts_a_basket_once(self):
        state = {"orders": [{"symbol": s, "side": "BUY", "status": "placed", "date": TODAY, "kind": "entry",
                             "at": TODAY + "T09:35:00+05:30", "max_loss": 100, "basket_id": b}
                            for s, b in (("NIFTY", "x"), ("NIFTY", "x"), ("NIFTY", "x"), ("NIFTY", "x"),
                                         ("TCS", None))]}
        r = run(orders_of(CONDOR)["orders"], c=cfg(max_trades_per_day=3), state=state, doc=CONDOR)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        r = run(orders_of(CONDOR)["orders"], c=cfg(max_trades_per_day=2), state=state, doc=CONDOR)["results"][0]
        self.assertIn("trades", tags(r))

    def test_breakers_also_lock_fno_baskets(self):
        log = [trade("09:45", -100.0), trade("09:50", -100.0), trade("09:55", -100.0)]
        r = run(orders_of(CONDOR)["orders"], doc=CONDOR, trades=log)["results"][0]
        self.assertIn("breaker", tags(r))


# ---------------------------------------------------------------- kill switch

class Kill(unittest.TestCase):
    def test_on_and_off_with_a_typed_word(self):
        now = check.parse_now("2026-11-02 11:00")
        k = check.kill_on(None, now, "market feels wrong")
        self.assertTrue(check.kill_active(k, TODAY))
        self.assertIsNone(check.kill_active(k, "2026-11-03"))       # a new day starts clear
        doc, why = check.kill_off(k, now, "yes")
        self.assertIsNone(doc)
        self.assertIn("type the word unkill", why)
        doc, why = check.kill_off(k, now, " UNKILL ")
        self.assertIsNone(why)
        self.assertFalse(check.kill_active(doc, TODAY))
        self.assertEqual([x["action"] for x in doc["log"]], ["kill", "unkill"])
        self.assertIsNone(check.kill_off(doc, now, "unkill")[0])

    def test_blocks_entries_never_exits(self):
        k = check.kill_on(None, check.parse_now("2026-11-02 10:30"), "test")
        r = cash([cash_order()], kill=k)["results"][0]
        self.assertEqual(tags(r), ["kill"])
        r = cash([EXIT], kill=k, portfolio=HELD)["results"][0]
        self.assertEqual(r["verdict"], "PASS")
        res = run(orders_of(CONDOR)["orders"], doc=CONDOR, kill=k)
        self.assertEqual(verdicts(res), ["BLOCK"] * 4)
        self.assertTrue(res["kill"]["active"])

    def test_cli_kill_revokes_recorded_passes_and_unkill(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = os.path.join(d, "trader")
            os.makedirs(os.path.join(tdir, "state"))
            orders = os.path.join(tdir, "orders.json")
            with open(orders, "w") as fh:
                json.dump({"orders": [cash_order()]}, fh)
            with open(os.path.join(tdir, "config.json"), "w") as fh:
                json.dump(CASH_CFG, fh)
            args = ["--orders", orders, "--config", os.path.join(tdir, "config.json"),
                    "--portfolio", os.path.join(tdir, "portfolio.json"), "--prices", os.path.join(tdir, "p.json"),
                    "--state", os.path.join(tdir, "state", "broker.json"),
                    "--trades", os.path.join(tdir, "state", "trades.json"),
                    "--kill", os.path.join(tdir, "state", "kill.json"), "--offline"]
            risk = os.path.join(tdir, "state", "risk.json")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(args + ["--now", "2026-11-02 10:00", "--out", risk]), 0)
                self.assertEqual(check.main(["kill", "--reason", "too many alerts", "--now", "2026-11-02 10:05"] +
                                            args + ["--risk", risk]), 0)
            self.assertIn("KILL SWITCH ON for 2026-11-02 from 10:05 IST (too many alerts)", out.getvalue())
            self.assertIn("Revoked 1 recorded entry PASS", out.getvalue())
            with open(risk) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["results"][0]["verdict"], "BLOCK")
            self.assertTrue(rec["results"][0]["reasons"][-1].startswith("kill:"))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(args + ["--now", "2026-11-02 10:06", "--no-write"]), 1)
            self.assertIn("KILL SWITCH ON since 10:05", out.getvalue())
            kpath = os.path.join(tdir, "state", "kill.json")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(["unkill", "--kill", kpath, "--yes-text", "ok", "--now",
                                             "2026-11-02 10:30"]), 1)
                self.assertEqual(check.cmd_unkill(["--kill", kpath, "--now", "2026-11-02 10:31"],
                                                  ask=lambda prompt: "unkill"), 0)
            self.assertIn("type the word unkill", out.getvalue())
            self.assertIn("Kill switch lifted at 10:31 IST", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(args + ["--now", "2026-11-02 10:32", "--no-write"]), 0)

    def test_kill_flatten_builds_exit_tickets(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = os.path.join(d, "trader")
            os.makedirs(tdir)
            pf = {"cash": 400000, "margin_blocked": 90000,
                  "positions": {"RELIANCE": {"qty": 42, "avg_price": 1174.3, "product": "MIS"},
                                "NIFTY26N0322800PE": {"exchange": "NFO", "qty": 65, "avg_price": 28.7, "lot_size": 65},
                                "NIFTY26N0322900PE": {"exchange": "NFO", "qty": -65, "avg_price": 44.3,
                                                      "lot_size": 65}}}
            with open(os.path.join(tdir, "portfolio.json"), "w") as fh:
                json.dump(pf, fh)
            with open(os.path.join(tdir, "config.json"), "w") as fh:
                json.dump(dict(CASH_CFG, allow_fno=True), fh)
            orders = os.path.join(tdir, "orders.json")
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.main(["kill", "--flatten", "--now", "2026-11-02 14:00", "--orders", orders,
                                   "--config", os.path.join(tdir, "config.json"),
                                   "--portfolio", os.path.join(tdir, "portfolio.json"),
                                   "--plan", os.path.join(tdir, "plan.json"),
                                   "--kill", os.path.join(tdir, "kill.json"), "--risk", os.path.join(tdir, "r.json")])
            self.assertEqual(code, 0, out.getvalue())
            with open(orders) as fh:
                doc = json.load(fh)
            got = [(o["symbol"], o["side"], o["qty"], o.get("strike")) for o in doc["orders"]]
            # cash shares from trader-broker's flatten, then the short F&O leg before the long one
            self.assertEqual(got, [("RELIANCE", "SELL", 42, None), ("NIFTY", "BUY", 65, 22900.0),
                                   ("NIFTY", "SELL", 65, 22800.0)])
            # every ticket is an exit: it passes even with the kill switch on
            with open(os.path.join(tdir, "kill.json")) as fh:
                kill = json.load(fh)
            res = check.check(doc["orders"], dict(CASH_CFG, allow_fno=True), pf, {"RELIANCE": 1170.0}, None, None,
                              check.parse_now("2026-11-02 14:01"), {},
                              {"kill": kill, "lots": LOTS, "strategy": CONDOR})
            self.assertEqual([(r["verdict"], r["kind"]) for r in res["results"]], [("PASS", "exit")] * 3,
                             [r["reasons"] for r in res["results"]])


# ---------------------------------------------------------------- order rate and live-broker API rules

class Rate(unittest.TestCase):
    def test_configured_rate_and_the_hard_cap(self):
        self.assertEqual(check.order_rate({}), (3, None))
        self.assertEqual(check.order_rate({"order_rate_per_sec": 5})[0], 5)
        n, warn = check.order_rate({"order_rate_per_sec": 50})
        self.assertEqual(n, 9)
        self.assertLess(n, check.SEBI_ORDER_RATE)
        self.assertIn("hard cap", warn)
        self.assertEqual(check.order_rate({"order_rate_per_sec": 0})[0], 3)

    def test_sliding_one_second_window(self):
        t0 = check.parse_now("2026-11-02 10:00:00")
        sent = [t0, t0.replace(microsecond=300000), t0.replace(microsecond=600000)]
        ok, wait = check.rate_gate(sent, t0.replace(microsecond=900000), 3)
        self.assertFalse(ok)
        self.assertAlmostEqual(wait, 0.1, places=3)
        self.assertTrue(check.rate_gate(sent, t0.replace(second=1, microsecond=1), 3)[0])

    def test_check_blocks_an_entry_over_the_rate(self):
        state = {"orders": [{"symbol": s, "side": "BUY", "status": "placed", "date": TODAY, "kind": "entry",
                             "at": TODAY + "T10:59:59.%d+05:30" % (100000 + i), "max_loss": 10}
                            for i, s in enumerate(("TCS", "INFY", "SBIN"))]}
        c = {"max_trades_per_day": 10}
        r = cash([cash_order()], now="2026-11-02 10:59:59.900000", state=state, c=c)["results"][0]
        self.assertEqual(tags(r), ["rate"])
        self.assertIn("wait 0.2 s", " ".join(r["reasons"]))
        self.assertEqual(cash([cash_order()], now="2026-11-02 11:00:01", state=state, c=c)["pass"], 1)

    def test_rate_gate_command(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "rate.json")
            cfgp = os.path.join(d, "config.json")
            with open(cfgp, "w") as fh:
                json.dump({"order_rate_per_sec": 2}, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                codes = [check.main(["rate-gate", "--config", cfgp, "--log", log, "--record"]) for _ in range(3)]
            self.assertEqual(codes[:2], [0, 0])
            self.assertIn("2 a second", out.getvalue())
            with open(log) as fh:
                self.assertLessEqual(len(json.load(fh)["sent"]), 3)

    def test_live_broker_needs_limit_or_protected_orders(self):
        o = cash_order()
        o["order_type"] = "MARKET"
        o.pop("price")

        def live(order, broker):
            return check.check([order], dict(CASH_CFG, paper=False, broker=broker), {"cash": 100000},
                               {"RELIANCE": 1174.3}, None, None, check.parse_now("2026-11-02 11:00"), None,
                               {})["results"][0]
        # Kite and Upstox send MARKET and SL-M with the broker's automatic market protection
        for b in ("kite", "zerodha", "upstox"):
            self.assertNotIn("api", tags(live(o, b)), b)
        # Dhan's adapter has no protection setting: LIMIT and SL only
        r = live(o, "dhan")
        self.assertIn("api", tags(r))
        self.assertIn("use a LIMIT order", " ".join(r["reasons"]))
        self.assertIn("SL-M stop_loss", " ".join(r["reasons"]))
        lim = cash_order()
        lim["stop_loss"] = {"order_type": "SL", "trigger_price": 1163.3, "price": 1162.0}
        self.assertNotIn("api", tags(live(lim, "dhan")))
        # paper never asks
        self.assertNotIn("api", tags(cash([o])["results"][0]))


# ---------------------------------------------------------------- commands

class Commands(unittest.TestCase):
    def test_scenarios_command_json_and_text(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = os.path.join(d, "trader")
            os.makedirs(tdir)
            for name, body in (("config.json", cfg()), ("portfolio.json", SHORT_FUT), ("strategy.json", CONDOR),
                               ("orders.json", orders_of(CONDOR))):
                with open(os.path.join(tdir, name), "w", encoding="utf-8") as fh:
                    json.dump(body, fh)
            with open(os.path.join(OPT, "fo_mktlots.csv"), encoding="utf-8") as src, \
                    open(os.path.join(tdir, "fo_mktlots.csv"), "w", encoding="utf-8") as dst:
                dst.write(src.read())
            args = ["scenarios", "--now", MON, "--config", os.path.join(tdir, "config.json"),
                    "--orders", os.path.join(tdir, "orders.json"), "--portfolio", os.path.join(tdir, "portfolio.json"),
                    "--prices", os.path.join(tdir, "prices.json"), "--trades", os.path.join(tdir, "t.json"),
                    "--kill", os.path.join(tdir, "k.json")]
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(args + ["--json"]), 0)
            view = json.loads(out.getvalue())
            self.assertEqual(view["with_proposals"]["legs"], 5)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(check.main(args), 0)
            self.assertIn("spot \\ IV", out.getvalue())
            self.assertIn("worst: spot", out.getvalue())

    def test_refresh_without_network_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(["refresh", "--config", os.path.join(d, "config.json"), "--now", MON])
            self.assertEqual(code, 1)
            self.assertIn("no network", out.getvalue())

    def test_the_cli_reads_the_ban_cache_offline(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = os.path.join(d, "trader")
            os.makedirs(tdir)
            with open(os.path.join(tdir, "fo_secban.json"), "w") as fh:
                json.dump({"days": {TODAY: {"symbols": BAN["symbols"]}}}, fh)
            with open(os.path.join(tdir, "config.json"), "w") as fh:
                json.dump(cfg(), fh)
            with open(os.path.join(tdir, "orders.json"), "w") as fh:
                json.dump({"orders": [fut("KAYNES", "2026-11-23")]}, fh)
            with open(os.path.join(tdir, "prices.json"), "w") as fh:
                json.dump({"KAYNES": {"price": 3055.5}}, fh)
            with open(os.path.join(OPT, "fo_mktlots.csv"), encoding="utf-8") as src, \
                    open(os.path.join(tdir, "fo_mktlots.csv"), "w", encoding="utf-8") as dst:
                dst.write(src.read())
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.main(["--now", MON, "--offline", "--no-write", "--orders", os.path.join(tdir, "orders.json"),
                                   "--config", os.path.join(tdir, "config.json"),
                                   "--portfolio", os.path.join(tdir, "portfolio.json"),
                                   "--prices", os.path.join(tdir, "prices.json"),
                                   "--state", os.path.join(tdir, "b.json"), "--trades", os.path.join(tdir, "t.json"),
                                   "--kill", os.path.join(tdir, "k.json")])
            self.assertEqual(code, 1)
            self.assertIn("KAYNES is in NSE's F&O ban for 2026-11-02", out.getvalue())


if __name__ == "__main__":
    unittest.main()
