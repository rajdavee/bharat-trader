"""trader-budget tests, offline.

Fixtures, cut from real Yahoo data fetched 2026-10-29:
  trader-budget/yahoo-RELIANCE-2026-10-29-5m.json  RELIANCE.NS five-minute bars of Thursday 2026-10-29
  trader-budget/plan-reliance.json   the opening-range breakout in those bars: range 1156.20 to 1170.50
                                     (09:15/09:20/09:25), the 09:35 bar closes above it at 1174.30, entry
                                     09:40; stop the range midpoint 1163.30, target 2R 1196.30
  trader-budget/yahoo-spark-nse-2026-10-29.json   daily closes of seven NSE stocks (spark endpoint)
  trader-risk/prices-reliance.json   RELIANCE 1177.30 at 10:20 IST (the 10:15 bar's close)
"""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, ROOT, FIX  # noqa: E402

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-reliance.json")
BARS = os.path.join(FIX, "trader-budget", "yahoo-RELIANCE-2026-10-29-5m.json")
SPARK = os.path.join(FIX, "trader-budget", "yahoo-spark-nse-2026-10-29.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-reliance.json")
PRICES = os.path.join(FIX, "trader-risk", "prices-reliance.json")   # RELIANCE 1177.30 at 10:20 IST
NOW = "2026-10-29 09:40"
TODAY = "2026-10-29"

# the spec's config, key for key
SPEC_CFG = {
    "capital": 100000, "risk_per_trade_pct": 1.0, "max_loss_per_trade": 1000, "max_daily_loss": 3000,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "max_position_pct": 50, "max_order_value": 50000,
    "min_order_value": 500, "cash_buffer_pct": 10, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10", "broker_profile": "",
    "square_off_margin_min": 5, "exchange": "NSE",
    "product": "MIS", "tick_size": "auto", "broker": "paper", "paper": True, "blocked": [], "allow_short": False,
    "allow_fno": False, "include_charges": True,
    "charges": {"brokerage_pct": 0.03, "brokerage_cap": 20, "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
                "exchange_txn_pct": 0.00307, "sebi_per_crore": 10, "stamp_buy_pct": 0.003,
                "stamp_delivery_pct": 0.015, "gst_pct": 18},
    "extra_holidays": [], "special_sessions": [], "fno_stocks": [],
}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def spark_closes():
    with open(SPARK) as fh:
        d = json.load(fh)
    return {k.replace(".NS", ""): v["close"][-1] for k, v in d.items()}


class Money(unittest.TestCase):
    def test_inr_uses_indian_grouping(self):
        self.assertEqual(size.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(size.inr(0.5), "₹0.50")
        self.assertEqual(size.inr(999), "₹999.00")
        self.assertEqual(size.inr(1000), "₹1,000.00")
        self.assertEqual(size.inr(100000), "₹1,00,000.00")
        self.assertEqual(size.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(size.inr(-49320.6), "-₹49,320.60")
        self.assertEqual(size.inr(1.005), "₹1.01")          # half up, not banker's

    def test_tick_for_uses_the_coarsest_tick_of_the_nse_price_band(self):
        for price, tick in ((999.95, 0.05), (1000, 0.10), (1174.30, 0.10), (4999.90, 0.10), (5000, 0.50),
                            (15000, 1.00), (25000, 5.00), (120.35, 0.05)):
            self.assertEqual(size.tick_for(price), tick, price)
            self.assertEqual(size.tick_for(price, "auto"), tick, price)
        self.assertEqual(size.tick_for(1174.30, 0.01), 0.01)        # an explicit number overrides
        self.assertEqual(size.tick_for(25000, 0.05), 0.05)

    def test_tick_rounding(self):
        self.assertEqual(size.tick_round(1163.37, 0.05, "down"), 1163.35)
        self.assertEqual(size.tick_round(1163.39, 0.05, "down"), 1163.35)
        self.assertEqual(size.tick_round(1163.39, 0.10, "down"), 1163.30)
        self.assertEqual(size.tick_round(1196.23, 0.05), 1196.25)
        self.assertEqual(size.tick_round(1196.22, 0.05), 1196.20)
        self.assertEqual(size.tick_round(1174.30, 0.05), 1174.30)
        self.assertEqual(size.tick_round(1174.33, 0.10, "down"), 1174.30)


class Charges(unittest.TestCase):
    """Worked by hand: buy 83 RELIANCE at 1174.30, the stop fills at 1163.30 (the plan's stop:
    the range midpoint 1163.35 rounded down to RELIANCE's ₹0.10 tick), MIS.

    buy turnover   83 x 1174.30 = 97,466.90       sell turnover 83 x 1163.30 = 96,553.90
    turnover       1,94,020.80
    brokerage      buy  0.03% = 29.24 -> capped at 20; sell 0.03% = 28.97 -> capped at 20     = 40.00
    STT            0.025% of the sell side 96,553.90 = 24.1385                                = 24.14
    exchange       0.00307% of 1,94,020.80 = 5.9564                                          =  5.96
    SEBI           10 per crore: 1,94,020.80 x 10 / 1,00,00,000 = 0.1940                      =  0.19
    stamp          0.003% of the buy side 97,466.90 = 2.9240                                  =  2.92
    GST            18% of (40 + 5.9564 + 0.1940) = 8.3071                                     =  8.31
    total                                                                                     = 81.52
    """

    def test_the_hand_worked_example(self):
        self.assertEqual(size.round_trip_charges(1174.30, 1163.30, 83, cfg()),
                         {"brokerage": 40.0, "stt": 24.14, "exchange": 5.96, "sebi": 0.19, "stamp": 2.92,
                          "gst": 8.31, "total": 81.52})

    def test_delivery_pays_stt_both_sides_and_more_stamp(self):
        # CNC: STT 0.1% of 1,94,020.80 = 194.02; stamp 0.015% of 97,466.90 = 14.62
        ch = size.round_trip_charges(1174.30, 1163.30, 83, cfg(product="CNC"))
        self.assertEqual((ch["stt"], ch["stamp"], ch["brokerage"], ch["gst"]), (194.02, 14.62, 40.0, 8.31))
        self.assertEqual(ch["total"], 263.10)

    def test_brokerage_under_the_cap(self):
        # 1 share: 0.03% of 1174.30 = 0.3523 and of 1163.30 = 0.3490: 0.70 in all
        self.assertEqual(size.round_trip_charges(1174.30, 1163.30, 1, cfg())["brokerage"], 0.70)

    def test_a_bare_charges_dict_works_too(self):
        self.assertEqual(size.round_trip_charges(1174.30, 1163.30, 83, SPEC_CFG["charges"])["total"], 81.52)
        self.assertEqual(size.round_trip_charges(1174.30, 1163.30, 83, {})["total"], 81.52)   # the defaults


class TheFixtures(unittest.TestCase):
    def test_config_example_is_the_spec(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), SPEC_CFG)

    def test_the_plan_comes_from_the_real_bars(self):
        """RELIANCE on 2026-10-29: opening range = the 09:15, 09:20, 09:25 bars; the 09:35 bar closed above it."""
        with open(BARS) as fh:
            r = json.load(fh)["chart"]["result"][0]
        self.assertEqual(r["meta"]["currency"], "INR")
        q = r["indicators"]["quote"][0]
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        bars = {datetime.datetime.fromtimestamp(t, ist).strftime("%H:%M"): i for i, t in enumerate(r["timestamp"])}
        orb = [bars["09:15"], bars["09:20"], bars["09:25"]]
        hi = round(max(q["high"][i] for i in orb), 2)
        lo = round(min(q["low"][i] for i in orb), 2)
        p = plan()
        self.assertEqual((p["opening_range"]["high"], p["opening_range"]["low"]), (hi, lo))
        self.assertEqual(p["entry"], round(q["close"][bars["09:35"]], 2))
        self.assertLessEqual(round(q["close"][bars["09:30"]], 2), hi)
        # RELIANCE above ₹1,000 trades on ₹0.10 steps: the midpoint 1163.35 rounds down to 1163.30
        self.assertEqual(size.tick_for(p["entry"]), 0.10)
        self.assertEqual(round((hi + lo) / 2, 2), 1163.35)
        self.assertEqual(p["stop"], size.tick_round((hi + lo) / 2, size.tick_for(p["entry"]), "down"))
        self.assertEqual(p["target"], round(p["entry"] + 2 * (p["entry"] - p["stop"]), 2))


class PlanSizing(unittest.TestCase):
    def test_reliance_is_42_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 42)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 83), ("position cap", 42), ("max order", 42), ("cash", 76)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertEqual((s["risk_per_share"], s["value"], s["max_loss"]), (11.00, 49320.60, 462.00))
        self.assertEqual((s["charges"], s["max_loss_with_charges"]), (52.11, 514.11))
        self.assertEqual(s["reward_risk"], 2.0)
        self.assertIsNone(s["refusal"])

    def test_max_loss_counts_the_charges(self):
        # 83 shares: 83 x 11.00 = 913.00 + 81.52 = 994.52 fits 1,000; 84: 924.00 + 81.94 = 1,005.94 does not
        self.assertEqual(round(83 * 11.00 + size.round_trip_charges(1174.30, 1163.30, 83, cfg())["total"], 2), 994.52)
        self.assertEqual(round(84 * 11.00 + size.round_trip_charges(1174.30, 1163.30, 84, cfg())["total"], 2), 1005.94)
        s = size.size_trade(plan(), cfg(max_position_pct=100, max_order_value=100000, cash_buffer_pct=0), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_with_charges"]), (83, "max loss", 994.52))
        # without charges the same limit allows floor(1000 / 11.00) = 90
        s = size.size_trade(plan(), cfg(max_position_pct=100, max_order_value=200000, cash_buffer_pct=0,
                                        include_charges=False), today=TODAY)
        self.assertEqual((s["qty"], s["charges"]), (85, 0.0))   # 100% of 1,00,000 / 1174.30 = 85 (cap and cash tie)
        s = size.size_trade(plan(), cfg(max_position_pct=100, max_order_value=200000, cash_buffer_pct=0,
                                        include_charges=False, capital=200000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (90, "max loss"))

    def test_each_limit_can_bind(self):
        cases = [
            (dict(max_loss_per_trade=300), None, "max loss"),
            (dict(max_position_pct=30), None, "position cap"),         # 30,000 / 1174.30 = 25
            (dict(max_order_value=20000), None, "max order"),           # 20,000 / 1174.30 = 17
            ({}, 25000, "cash"),                                        # (25,000 - 10,000) / 1174.30 = 12
        ]
        for over, cash, name in cases:
            s = size.size_trade(plan(), cfg(**over), cash=cash, today=TODAY)
            self.assertEqual(s["bound_by"], name, over)
        self.assertEqual(size.size_trade(plan(), cfg(max_position_pct=30), today=TODAY)["qty"], 25)
        self.assertEqual(size.size_trade(plan(), cfg(max_order_value=20000), today=TODAY)["qty"], 17)
        self.assertEqual(size.size_trade(plan(), cfg(), cash=25000, today=TODAY)["qty"], 12)
        s = size.size_trade(plan(), cfg(max_loss_per_trade=300), today=TODAY)
        ch = lambda q: size.round_trip_charges(1174.30, 1163.30, q, cfg())["total"]
        self.assertLessEqual(s["qty"] * 11.00 + ch(s["qty"]), 300)
        self.assertGreater((s["qty"] + 1) * 11.00 + ch(s["qty"] + 1), 300)
        self.assertEqual(s["qty"], 24)

    def test_one_share_over_the_max_loss_is_a_refusal_not_a_smaller_order(self):
        s = size.size_trade(plan(stop=1100.00), cfg(max_loss_per_trade=50), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (0, "max loss"))
        self.assertIn("one share risks", s["refusal"])
        self.assertIn("not shrunk", s["refusal"])
        self.assertIsNone(size.leg_order(s))
        # the charges alone tip one share over: 11.00 + 0.73 charges > 11
        s = size.size_trade(plan(), cfg(max_loss_per_trade=11), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("charges", s["refusal"])

    def test_too_expensive_for_one_share(self):
        s = size.size_trade(plan(), cfg(max_order_value=1000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (0, "max order"))
        self.assertIn("costs ₹1,174.30", s["refusal"])

    def test_reward_risk_and_minimum_order_refusals(self):
        s = size.size_trade(plan(), cfg(), price=1190.00, today=TODAY, market=True)
        self.assertEqual(s["entry"], 1190.00)
        self.assertIn("reward:risk 0.24", s["refusal"])
        s = size.size_trade(plan(), cfg(min_order_value=60000), today=TODAY)
        self.assertIn("minimum order", s["refusal"])

    def test_market_order_is_sized_at_a_higher_fresh_price(self):
        # the real 10:20 price 1177.30: risk 14.00 a share, reward:risk (1196.30 - 1177.30) / 14.00 = 1.36
        s = size.size_trade(plan(), cfg(), price=1177.30, today=TODAY, market=True)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"], s["reward_risk"]),
                         (1177.30, "price", 14.00, 1.36))
        self.assertIn("reward:risk 1.36", s["refusal"])
        s = size.size_trade(plan(), cfg(min_reward_risk=1.0), price=1177.30, today=TODAY, market=True)
        o = size.leg_order(s)
        self.assertEqual((o["order_type"], o["price"]), ("MARKET", None))
        # a lower price never shrinks the risk
        s = size.size_trade(plan(), cfg(), price=1170.00, today=TODAY, market=True)
        self.assertEqual((s["entry"], s["entry_from"]), (1174.30, "plan"))

    def test_a_limit_price(self):
        s = size.size_trade(plan(), cfg(), limit=1175.00, today=TODAY)
        self.assertEqual((s["entry"], s["risk_per_share"], s["order_type"]), (1175.00, 11.70, "LIMIT"))
        self.assertEqual(size.leg_order(s)["price"], 1175.00)

    def test_prices_are_rounded_to_the_tick(self):
        # a ₹1,174 stock is on the ₹0.10 grid: entry and target to the nearest 0.10, the stop DOWN
        s = size.size_trade(plan(entry=1174.32, stop=1163.39, target=1196.26), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1174.30, 1163.30, 1196.30))
        s = size.size_trade(plan(stop=1163.35), cfg(), today=TODAY)       # the raw midpoint
        self.assertEqual(s["stop"], 1163.30)
        s = size.size_trade(plan(stop=1163.35), cfg(tick_size=0.05), today=TODAY)   # a number overrides "auto"
        self.assertEqual(s["stop"], 1163.35)

    def test_bad_plans(self):
        bad = [
            (plan(date="2026-10-28"), "today is 2026-10-29"),
            (plan(side="sell"), "long (buy) trades only"),
            (plan(stop=1180.00), "not below the entry"),
            (plan(target=1170.00), "not above the entry"),
            (plan(symbol=""), "no symbol"),
            (plan(stop=None), "positive stop"),
        ]
        for p, msg in bad:
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(msg, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["RELIANCE"]), today=TODAY)
        self.assertIn("blocked", str(e.exception))

    def test_the_order_is_the_spec_shape(self):
        o = size.leg_order(size.size_trade(plan(), cfg(), today=TODAY))
        self.assertEqual(list(o), ["symbol", "exchange", "product", "side", "qty", "order_type", "price",
                                   "stop_loss", "target", "validity", "why", "plan"])
        self.assertEqual((o["symbol"], o["exchange"], o["product"], o["side"], o["qty"], o["order_type"],
                          o["price"], o["validity"]), ("RELIANCE", "NSE", "MIS", "BUY", 42, "LIMIT", 1174.30, "DAY"))
        self.assertEqual(o["stop_loss"], {"order_type": "SL-M", "trigger_price": 1163.30})
        self.assertEqual(o["target"], {"order_type": "LIMIT", "price": 1196.30})
        self.assertEqual(o["plan"], {"entry": 1174.30, "stop": 1163.30, "target": 1196.30, "risk_per_share": 11.00,
                                     "max_loss": 462.00, "charges": 52.11, "max_loss_with_charges": 514.11,
                                     "setup": "orb", "entry_time": "09:40", "flat_by": "15:10"})
        self.assertIn("bound by your position cap", o["why"])
        self.assertIn("₹514.11", o["why"])

    def test_cli_writes_the_fixture_orders_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--prices", PRICES,
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 42 shares of RELIANCE = ₹49,320.60 at 1174.30", text)
            self.assertIn("<- binds", text)
            self.assertIn("SL-M stop 1163.30", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh, open(ORDERS) as fx:
                self.assertEqual(json.load(fh), json.load(fx))

    def test_cli_market_uses_the_fresh_price(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--prices", PRICES,
                             "--now", "2026-10-29 10:25", "--market", "--json"])
        self.assertEqual(code, 1)                    # the move ran: reward:risk 1.36 is under 1.5
        out = json.loads(text)
        self.assertEqual((out["sizing"]["entry"], out["sizing"]["entry_from"]), (1177.30, "price"))
        self.assertEqual(out["orders"], [])
        self.assertIn("reward:risk 1.36", out["sizing"]["refusal"])
        self.assertIn("above the plan's entry", out["notes"][0])
        # an hour later the 10:20 price is stale: not used
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--prices", PRICES,
                             "--now", "2026-10-29 11:30", "--market", "--json"])
        out = json.loads(text)
        self.assertEqual(out["sizing"]["entry_from"], "plan")
        self.assertIn("not used", out["notes"][0])

    def test_cli_refusal_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.json")
            with open(c, "w") as fh:
                json.dump(cfg(max_loss_per_trade=10), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", c, "--portfolio", "/nonexistent", "--now", NOW,
                                 "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 25000, "positions": {}}, fh)
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])
            s = json.loads(text)["sizing"]
            self.assertEqual((s["qty"], s["bound_by"]), (12, "cash"))


class Init(unittest.TestCase):
    def test_non_interactive_builds_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trader", "config.json")
            code, text, err = run(["init", "--config", path, "--capital", "200000", "--risk-pct", "0.5",
                                   "--max-trades", "2", "--product", "cnc", "--yes"])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                c = json.load(fh)
            self.assertEqual(set(c), set(SPEC_CFG))
            self.assertEqual((c["capital"], c["risk_per_trade_pct"], c["max_loss_per_trade"], c["max_daily_loss"]),
                             (200000, 0.5, 1000, 3000))
            self.assertEqual((c["max_trades_per_day"], c["product"], c["max_order_value"], c["paper"]),
                             (2, "CNC", 100000, True))
            self.assertIn("Most one trade may lose at its stop, charges included: ₹1,000.00", text)
            self.assertIn("Most one day may lose: ₹3,000.00", text)
            self.assertIn("₹2,00,000.00", text)

    def test_spec_defaults_come_back_from_the_spec_answers(self):
        c = size.build_config(100000, 1, 3, "MIS")
        self.assertEqual(c, SPEC_CFG)

    def test_it_never_overwrites_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as fh:
                fh.write('{"capital": 5}')
            code, _, err = run(["init", "--config", path, "--capital", "100000", "--yes"])
            self.assertEqual(code, 1)
            self.assertIn("already exists", err)
            with open(path) as fh:
                self.assertEqual(fh.read(), '{"capital": 5}')
            code, _, err = run(["init", "--config", path, "--capital", "100000", "--yes", "--force"])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                self.assertEqual(json.load(fh)["max_loss_per_trade"], 1000)

    def test_without_a_terminal_it_needs_yes(self):
        a = size.init_parser().parse_args(["--config", "/nonexistent/x.json", "--capital", "100000"])
        with self.assertRaises(size.SizeError) as e:
            size.run_init(a, interactive=False)
        self.assertIn("--yes", str(e.exception))

    def test_interactive_prompts(self):
        answers = iter(["1,50,000", "2", "4", "MIS", "yes"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            a = size.init_parser().parse_args(["--config", path])
            out = io.StringIO()
            with redirect_stdout(out):
                code = size.run_init(a, ask=lambda prompt: next(answers), interactive=True)
            self.assertEqual(code, 0)
            with open(path) as fh:
                c = json.load(fh)
            self.assertEqual((c["capital"], c["max_loss_per_trade"], c["max_daily_loss"], c["max_trades_per_day"],
                              c["max_orders_per_day"]), (150000, 3000, 9000, 4, 12))
        answers = iter(["100000", "", "", "", "no"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            a = size.init_parser().parse_args(["--config", path])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(size.run_init(a, ask=lambda prompt: next(answers), interactive=True), 1)
            self.assertFalse(os.path.exists(path))

    def test_bad_answers(self):
        for args, msg in (((0,), "positive"), ((100000, 10), "at most 5"), ((100000, 1, 0), "between 1 and 20"),
                          ((100000, 1, 3, "NRML"), "MIS (intraday) or CNC"), ((1000,), "too small")):
            with self.assertRaises(size.SizeError) as e:
                size.build_config(*args)
            self.assertIn(msg, str(e.exception))

    def test_missing_max_loss_is_derived_from_capital(self):
        c = size.merged_config({"capital": 50000, "risk_per_trade_pct": 2})
        self.assertEqual(c["max_loss_per_trade"], 1000)


class Weights(unittest.TestCase):
    def prices(self):
        return spark_closes()

    def test_the_example_split_in_whole_shares(self):
        with open(TARGETS) as fh:
            rows = size.parse_weights(json.load(fh))
        self.assertEqual([r["symbol"] for r in rows], ["RELIANCE", "HDFCBANK", "TCS", "INFY", "SBIN"])
        r = size.size(rows, cfg(), prices=self.prices())
        self.assertEqual((r["investable"], r["cap"]), (90000.0, 50000.0))
        self.assertLessEqual(r["invested"], r["investable"])
        for p in r["positions"]:
            self.assertEqual(p["rupees"], round(p["shares"] * p["price"], 2))
            self.assertLessEqual(p["rupees"], r["cap"])
        # slices: RELIANCE 27,000 / 1167.40 floors to 23; the leftovers top up whole shares, never past the cash
        sh = {p["symbol"]: p["shares"] for p in r["positions"]}
        self.assertGreaterEqual(sh["RELIANCE"], 23)
        self.assertLess(r["idle"], max(self.prices()[s] for s in sh))
        orders = size.orders_from(r)
        self.assertTrue(all(o["product"] == "CNC" and o["order_type"] == "MARKET" and o["exchange"] == "NSE"
                            and isinstance(o["qty"], int) for o in orders))

    def test_cap_blocked_top_and_errors(self):
        rows = size.parse_weights(size.weights_from_arg("RELIANCE=80,TCS=20"))
        r = size.size(rows, cfg(max_position_pct=40), prices=self.prices())
        rel = [p for p in r["positions"] if p["symbol"] == "RELIANCE"][0]
        self.assertTrue(rel["capped"])
        self.assertLessEqual(rel["rupees"], 40000)
        rows = size.parse_weights(size.weights_from_arg("RELIANCE=30,M&M=20,TCS=10"))
        r = size.size(rows, cfg(blocked=["TCS"]), prices=self.prices())
        self.assertEqual(r["dropped"][0]["symbol"], "TCS")
        self.assertIn("M&M", [p["symbol"] for p in r["positions"]])
        r = size.size(rows, cfg(), prices=self.prices(), top=1)
        self.assertEqual([p["symbol"] for p in r["positions"]], ["RELIANCE"])
        with self.assertRaises(size.SizeError):
            size.size(size.parse_weights({"RELIANCE": 50, "ZZZZ": 50}), cfg(), prices=self.prices())
        with self.assertRaises(size.SizeError):
            size.size(size.parse_weights([{"symbol": "TCS", "weight": 1}, {"symbol": "TCS", "weight": 2}]), cfg(),
                      prices=self.prices())

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({s: {"price": p, "time": "2026-10-29T15:30:00+05:30", "source": "yahoo"}
                           for s, p in self.prices().items()}, fh)
            out = os.path.join(d, "orders.json")
            code, text, err = run(["--targets", TARGETS, "--config", CFG, "--prices", pp, "--now", NOW,
                                   "--write", out])
            self.assertEqual(code, 0, err)
            self.assertIn("CNC delivery", text)
            self.assertIn("have no stop", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["created_by"], "trader-budget")
            self.assertEqual(len(doc["orders"]), 5)


if __name__ == "__main__":
    unittest.main()
