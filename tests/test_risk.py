"""trader-risk tests, offline.

Fixtures, cut from real Yahoo data fetched 2026-10-29:
  trader-risk/orders-reliance.json  what trader-budget writes for the RELIANCE opening-range breakout of
                                    2026-10-29 (42 shares, LIMIT 1174.30, SL-M 1163.30, target 1196.30)
  trader-risk/prices-reliance.json  RELIANCE 1177.30 at 10:20 IST (the 10:15 five-minute bar's close)
  trader-risk/portfolio.json        a fresh paper account: ₹1,00,000 cash, nothing held
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, ROOT, FIX  # noqa: E402

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
PRICES_PATH = os.path.join(FIX, "trader-risk", "prices-reliance.json")
PORTFOLIO_PATH = os.path.join(FIX, "trader-risk", "portfolio.json")
NOW = "2026-10-29 09:40"
TODAY = "2026-10-29"
LOOSE = dict(max_position_pct=100, max_order_value=200000, cash_buffer_pct=0)   # only the loss limit binds


def prices_doc():
    with open(PRICES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o = copy.deepcopy(o)
    for k, v in over.items():
        if v is None:
            o.pop(k, None)
        else:
            o[k] = v
    return o


def placed(sym="SBIN", side="BUY", day=TODAY, max_loss=500.0, kind="entry", status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:35:00+05:30", "kind": kind}
    if max_loss is not None:
        r["max_loss"] = max_loss
    return r


def run_check(orders, c=None, portfolio=None, prices=None, state=None, now=NOW, times=None):
    return check.check(orders, cfg() if c is None else c, portfolio, prices, state, None, at(now), times)


def one(*a, **k):
    res = run_check(*a, **k)
    return res["results"][0]


def tags(r):
    return sorted({x.split(":")[0] for x in r["reasons"]})


class TheFixtureOrder(unittest.TestCase):
    def test_passes_at_0940(self):
        r = one([rel()])
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["kind"], r["qty"], r["value"], r["entry_ref"], r["entry_ref_from"]),
                         ("entry", 42, 49320.60, 1174.30, "limit"))
        self.assertEqual((r["max_loss"], r["charges"], r["max_loss_with_charges"], r["reward_risk"]),
                         (462.00, 52.00, 514.00, 2.0))

    def test_cli_records_the_verdict_with_the_brokers_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", PORTFOLIO_PATH,
                                   "--prices", PRICES_PATH, "--state", os.path.join(d, "none"),
                                   "--now", NOW, "--out", out])
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn("PASS  #1 BUY 42 RELIANCE (MIS), entry", text)
            self.assertIn("₹462.00 + ₹52.00 charges = ₹514.00 (limit ₹1,000.00)", text)
            self.assertIn("IST", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["fingerprint"], ticket.fingerprint(rel()))

    def test_cli_exit_code_1_on_a_block(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", PORTFOLIO_PATH,
                                   "--state", os.path.join(d, "none"), "--now", "2026-10-31 10:00", "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("weekend", buf.getvalue())
            self.assertFalse(os.path.exists(os.path.join(d, "risk.json")))

    def test_fingerprint_ignores_notes_but_not_the_order(self):
        self.assertEqual(check.fingerprint(rel()), check.fingerprint(rel(why="other words", plan={})))
        self.assertNotEqual(check.fingerprint(rel()), check.fingerprint(rel(qty=43)))
        self.assertNotEqual(check.fingerprint(rel()), check.fingerprint(rel(stop_loss={"order_type": "SL-M",
                                                                                         "trigger_price": 1160.00})))


class StopTargetLoss(unittest.TestCase):
    def test_stop_is_required(self):
        r = one([rel(stop_loss=None, plan={})])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("stop", tags(r))
        r = one([rel(stop_loss=None, plan={})], cfg(require_stop=False, min_reward_risk=0))
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("no stop" in w for w in r["warnings"]))

    def test_a_plan_only_stop_counts_but_warns(self):
        r = one([rel(stop_loss=None)])
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("plan only" in w for w in r["warnings"]))

    def test_stop_and_target_on_the_wrong_side(self):
        r = one([rel(stop_loss={"order_type": "SL-M", "trigger_price": 1175.00}, plan={})])
        self.assertIn("stop", tags(r))
        r = one([rel(target={"order_type": "LIMIT", "price": 1170.00}, plan={})])
        self.assertIn("target", tags(r))

    def test_loss_at_the_stop_includes_the_charges(self):
        # 83 shares: 913.00 + 81.28 charges = 994.28 fits; 84: 924.00 + 81.71 = 1,005.71 does not
        r = one([rel(qty=83)], cfg(**LOOSE))
        self.assertEqual((r["verdict"], r["max_loss_with_charges"]), ("PASS", 994.28))
        r = one([rel(qty=84)], cfg(**LOOSE))
        self.assertEqual(tags(r), ["loss"])
        self.assertIn("84 shares x 11.00 risk a share = ₹924.00 + ₹81.71 charges = ₹1,005.71",
                      r["reasons"][0])
        # without charges 84 x 11.00 = 924.00 fits
        self.assertEqual(one([rel(qty=84)], cfg(include_charges=False, **LOOSE))["verdict"], "PASS")

    def test_market_risk_is_measured_from_the_higher_fresh_price(self):
        mkt = rel(order_type="MARKET", price=None, qty=70)
        # plan entry 1174.30: 70 x 11.00 = 770.00 + charges, fine
        prices, times = check.load_prices(prices_doc())
        stale = one([mkt], cfg(**LOOSE), prices=prices, times=times, now="2026-10-29 11:00")
        self.assertEqual((stale["verdict"], stale["entry_ref_from"]), ("PASS", "plan"))
        self.assertTrue(any("older than 15 minutes" in w for w in stale["warnings"]))
        fresh = one([mkt], cfg(**LOOSE), prices=prices, times=times, now="2026-10-29 10:25")
        self.assertEqual((fresh["entry_ref"], fresh["entry_ref_from"], fresh["risk_per_share"]),
                         (1177.30, "price", 14.00))
        # 70 x 14.00 = 980.00 + charges > 1,000; and reward:risk (1196.30 - 1177.30) / 14.00 = 1.36 < 1.5
        self.assertEqual(tags(fresh), ["loss", "reward"])
        # a LIMIT can never fill above its price: measured from the limit
        lim = one([rel(qty=70)], cfg(**LOOSE), prices=prices, times=times, now="2026-10-29 10:25")
        self.assertEqual((lim["verdict"], lim["entry_ref"]), ("PASS", 1174.30))

    def test_reward_risk(self):
        r = one([rel(target={"order_type": "LIMIT", "price": 1185.00}, plan={"entry": 1174.30})])
        self.assertEqual(tags(r), ["reward"])
        self.assertIn("0.97", r["reasons"][0])     # 10.70 / 11.00
        self.assertEqual(one([rel(target={"order_type": "LIMIT", "price": 1185.00})], cfg(min_reward_risk=0.9))
                         ["verdict"], "PASS")


class DayLimits(unittest.TestCase):
    def test_daily_loss(self):
        r = one([rel()], state={"orders": [placed(max_loss=2600)]})
        self.assertEqual(tags(r), ["daily loss"])
        self.assertIn("₹2,600.00 already at risk today plus ₹514.00", r["reasons"][0])
        self.assertEqual(one([rel()], state={"orders": [placed(max_loss=2400)]})["verdict"], "PASS")
        # yesterday's entries do not count
        self.assertEqual(one([rel()], state={"orders": [placed(max_loss=2900, day="2026-10-28")]})["verdict"], "PASS")

    def test_trades_per_day(self):
        st = {"orders": [placed("SBIN", max_loss=100), placed("TCS", max_loss=100), placed("INFY", max_loss=100)]}
        self.assertEqual(tags(one([rel()], state=st)), ["trades"])
        st["orders"].pop()
        self.assertEqual(one([rel()], state=st)["verdict"], "PASS")

    def test_daily_order_limit(self):
        st = {"orders": [placed("SBIN", side="SELL", kind="exit") for _ in range(10)]}
        self.assertEqual(tags(one([rel()], state=st)), ["daily limit"])

    def test_duplicates(self):
        res = run_check([rel(), rel(qty=5)], cfg(**LOOSE))
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "BLOCK"])
        self.assertIn("duplicate", tags(res["results"][1]))
        r = one([rel()], state={"orders": [placed("RELIANCE", max_loss=100)]})
        self.assertIn("duplicate", tags(r))

    def test_a_batch_is_checked_as_if_each_pass_filled(self):
        sbin = {"symbol": "SBIN", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 45,
                "order_type": "LIMIT", "price": 971.90, "stop_loss": {"order_type": "SL-M", "trigger_price": 965.00},
                "target": {"order_type": "LIMIT", "price": 985.70}, "validity": "DAY"}
        res = run_check([rel(), sbin])           # 49,320.60 + 43,735.50 leaves under the 10,000 buffer
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "BLOCK"])
        self.assertEqual(tags(res["results"][1]), ["cash"])
        self.assertEqual(run_check([sbin])["results"][0]["verdict"], "PASS")

    def test_exits_are_never_blocked_by_day_limits(self):
        pf = {"cash": 50679.40, "positions": {"RELIANCE": {"qty": 42, "avg_price": 1174.30}}}
        st = {"orders": [placed("SBIN", max_loss=1000) for _ in range(3)] +
              [placed("TCS", side="SELL", kind="exit") for _ in range(7)] + [placed("RELIANCE", max_loss=514.00)]}
        flat = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 42,
                "order_type": "MARKET", "price": None, "validity": "DAY"}
        r = one([flat], portfolio=pf, prices={"RELIANCE": 1168.80}, state=st, now="2026-10-29 15:20")
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        self.assertTrue(any(w.startswith("exit, not blocked: daily limit") for w in r["warnings"]))
        # selling one more than held is an entry (a short) and is blocked
        r = one([dict(flat, qty=43)], portfolio=pf, prices={"RELIANCE": 1168.80}, state=st, now="2026-10-29 15:20")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("short", tags(r))


class Window(unittest.TestCase):
    def verdict(self, now, c=None):
        r = one([rel()], c, now=now)
        return r["verdict"], " ".join(r["reasons"]), r

    def test_inside_the_window(self):
        for t in ("2026-10-29 09:30", "2026-10-29 12:00", "2026-10-29 14:45"):
            self.assertEqual(self.verdict(t)[0], "PASS", t)

    def test_before_and_after_the_entry_window(self):
        v, why, _ = self.verdict("2026-10-29 09:20")
        self.assertEqual(v, "BLOCK")
        self.assertIn("no new entries before 09:30 IST", why)
        v, why, _ = self.verdict("2026-10-29 14:50")
        self.assertIn("no new entries after 14:45 IST", why)

    def test_market_closed(self):
        for now, words in (("2026-10-29 09:05", "market is closed"),          # pre-open is not used
                           ("2026-10-29 15:35", "market is closed"),
                           ("2026-10-29 15:30", "the closing auction runs to 15:35"),   # RELIANCE is in F&O
                           ("2026-10-31 10:00", "weekend"),
                           ("2026-11-10 10:00", "Diwali-Balipratipada"),
                           ("2026-10-20 10:00", "Dussehra"),
                           ("2026-01-26 10:00", "Republic Day")):
            v, why, _ = self.verdict(now)
            self.assertEqual(v, "BLOCK", now)
            self.assertIn(words, why, now)

    def test_extra_holidays(self):
        v, why, _ = self.verdict(NOW, cfg(extra_holidays=["2026-10-29"]))
        self.assertIn("extra_holidays", why)

    def test_special_session_muhurat(self):
        c = cfg(special_sessions=[{"date": "2026-11-08", "open": "18:00", "close": "19:00", "name": "Muhurat trading"}])
        v, why, r = self.verdict("2026-11-08 18:15", c)
        self.assertEqual(v, "PASS", why)
        res = run_check([rel()], c, now="2026-11-08 18:15")
        self.assertTrue(any("Muhurat" in w for w in res["warnings"]))
        self.assertIn("market is closed", self.verdict("2026-11-08 17:00", c)[1])
        self.assertIn("weekend", self.verdict("2026-11-08 18:15")[1])     # a Sunday: closed without the session

    def test_a_year_without_a_calendar_warns(self):
        res = run_check([rel()], now="2027-01-04 10:00")
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertTrue(any("2027 is not in this tool" in w for w in res["warnings"]))

    def test_the_2026_calendar(self):
        self.assertEqual(len(check.NSE_HOLIDAYS), 20)
        self.assertEqual(check.NSE_HOLIDAYS["2026-03-03"], "Holi")
        self.assertEqual((check.OPEN.strftime("%H:%M"), check.CLOSE.strftime("%H:%M")), ("09:15", "15:30"))


class SymbolsAndProducts(unittest.TestCase):
    def test_blocked_list(self):
        self.assertEqual(tags(one([rel()], cfg(blocked=["RELIANCE"]))), ["blocked"])

    def test_nse_ticker_rule(self):
        for s in ("RELIANCE", "M&M", "BAJAJ-AUTO", "3MINDIA", "TATAMOTORS"):
            self.assertTrue(check.TICKER.match(s), s)
            self.assertFalse(check.FNO.match(s), s)
        r = one([rel(symbol="RELIANCE.NS")])
        self.assertIn("ticker", tags(r))
        r = one([rel(symbol="reliance industries")])
        self.assertIn("ticker", tags(r))

    def test_fno_is_off(self):
        for sym in ("NIFTY26NOVFUT", "BANKNIFTY26DECFUT", "NIFTY26NOV25000CE", "NIFTY26N0425000PE",
                    "RELIANCE26NOV1200CE"):
            r = one([rel(symbol=sym)])
            self.assertIn("fno", tags(r), sym)
        self.assertIn("fno", tags(one([rel(exchange="NFO")])))
        self.assertIn("fno", tags(one([rel(product="NRML")])))
        r = one([rel(symbol="NIFTY26NOVFUT")], cfg(allow_fno=True))
        self.assertNotIn("fno", tags(r))
        self.assertTrue(any("F&O order" in w for w in r["warnings"]))

    def test_exchange_and_product(self):
        self.assertEqual(tags(one([rel(exchange="BSE")])), ["exchange"])
        self.assertEqual(tags(one([rel(product="MTF")])), ["product"])
        self.assertEqual(one([rel(product="CNC")])["verdict"], "PASS")

    def test_no_shorts_unless_allowed(self):
        short = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "SELL", "qty": 40,
                 "order_type": "LIMIT", "price": 1174.30, "stop_loss": {"order_type": "SL-M", "trigger_price": 1185.25},
                 "target": {"order_type": "LIMIT", "price": 1152.40}, "validity": "DAY"}
        self.assertEqual(tags(one([short])), ["short"])
        r = one([short], cfg(allow_short=True))
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["risk_per_share"], 10.95)   # 1185.25 - 1174.30
        r = one([dict(short, product="CNC")], cfg(allow_short=True))
        self.assertIn("delivery short selling is not possible", " ".join(r["reasons"]))
        r = one([dict(short, stop_loss={"order_type": "SL-M", "trigger_price": 1170.00})], cfg(allow_short=True))
        self.assertIn("stop", tags(r))


class SizeCashPosition(unittest.TestCase):
    def test_order_value(self):
        self.assertEqual(tags(one([rel(qty=1)], cfg(min_order_value=2000, min_reward_risk=0)))[0], "size")
        r = one([rel(qty=50)], cfg(max_position_pct=100, cash_buffer_pct=0))   # 58,715 > 50,000
        self.assertEqual(tags(r), ["size"])

    def test_position_cap(self):
        r = one([rel(qty=50)], cfg(max_order_value=100000, cash_buffer_pct=0))  # 58,715 > 50% of 1,00,000
        self.assertEqual(tags(r), ["position"])

    def test_cash_buffer_and_leverage(self):
        pf = {"cash": 55000, "positions": {}}
        self.assertEqual(tags(one([rel()], portfolio=pf)), ["cash"])
        pf = {"cash": 40000, "positions": {}}
        r = one([rel()], portfolio=pf)
        self.assertEqual(tags(r), ["cash"])
        self.assertIn("leverage", r["reasons"][0])


class Format(unittest.TestCase):
    def test_malformed_orders(self):
        bad = [
            (rel(qty="abc"), "qty must be a whole number"),
            (rel(qty=0), "qty must be a whole number"),
            (rel(qty=2.5), "qty must be a whole number"),
            (rel(side="HOLD"), "side must be BUY or SELL"),
            (rel(order_type="BRACKET"), "unknown order_type"),
            (rel(price=None), "LIMIT order needs a positive price"),
            (rel(validity="GTC"), "unknown validity"),
            (rel(stop_loss={"order_type": "LIMIT", "trigger_price": 1163.30}), "stop_loss must be"),
            (rel(stop_loss={"order_type": "SL", "trigger_price": 1163.30}), "SL stop_loss needs a limit price"),
            (rel(target={"order_type": "SL", "price": 1196.30}), "target must be"),
            (rel(symbol=""), "no symbol"),
        ]
        for o, msg in bad:
            r = one([o])
            self.assertEqual(r["verdict"], "BLOCK", msg)
            self.assertIn(msg, " ".join(r["reasons"]), msg)

    def test_tick_size(self):
        for o, what in ((rel(price=1174.32), "price 1174.32"),
                        (rel(stop_loss={"order_type": "SL-M", "trigger_price": 1163.33}), "stop_loss trigger"),
                        (rel(target={"order_type": "LIMIT", "price": 1196.21}), "target price")):
            r = one([o])
            self.assertIn("tick", tags(r))
            self.assertIn(what, " ".join(r["reasons"]))
        self.assertNotIn("tick", tags(one([rel(price=1174.32)], cfg(tick_size=0.01))))

    def test_valid_tick_is_permissive_by_price_band(self):
        self.assertEqual(check.valid_tick(123.47), 0.01)
        self.assertTrue(check.on_tick(123.47, check.valid_tick(123.47)))          # under ₹250: paise
        self.assertFalse(check.on_tick(312.47, check.valid_tick(312.47)))
        self.assertTrue(check.on_tick(1174.35, check.valid_tick(1174.35)))        # a 0.05 price is never blocked
        self.assertFalse(check.on_tick(1174.33, check.valid_tick(1174.33)))
        self.assertEqual(check.valid_tick(1174.33, 0.01), 0.01)                   # a number overrides "auto"
        # end to end: a ₹123.47 limit on a cheap stock passes the tick check, 1174.33 does not
        cheap = rel(symbol="SBIN", qty=10, price=123.47,
                    stop_loss={"order_type": "SL-M", "trigger_price": 121.13},
                    target={"order_type": "LIMIT", "price": 128.15}, plan={})
        self.assertNotIn("tick", tags(one([cheap])))
        self.assertIn("tick", tags(one([rel(price=1174.33)])))
        self.assertNotIn("tick", tags(one([rel(price=1174.35, plan={})])))

    def test_live_account_warns(self):
        res = run_check([rel()], cfg(paper=False))
        self.assertTrue(any("LIVE" in w for w in res["warnings"]))

    def test_prices_both_shapes(self):
        p, t = check.load_prices({"RELIANCE": {"price": 1168.80, "time": "2026-10-29T15:10:00+05:30"}})
        self.assertEqual(p, {"RELIANCE": 1168.80})
        self.assertEqual(t["RELIANCE"].strftime("%H:%M %z"), "15:10 +0530")
        p, t = check.load_prices(prices_doc())          # the trader-alerts layout, IST +05:30 stamps
        self.assertEqual(p["RELIANCE"], 1177.30)
        self.assertEqual(t["RELIANCE"], at("2026-10-29 10:20"))
        # a UTC "Z" stamp is the same moment (parsed without Python 3.11's fromisoformat)
        p, t = check.load_prices({"as_of": "2026-10-29T04:50:00Z", "prices": {"RELIANCE": 1177.30}})
        self.assertEqual(t["RELIANCE"], at("2026-10-29 10:20"))
        self.assertEqual(at("2026-10-29T04:50:00Z"), at("2026-10-29 10:20"))

    def test_missing_config_keys_use_the_defaults(self):
        self.assertEqual(one([rel()], {})["verdict"], "PASS")

    def test_inr(self):
        self.assertEqual(check.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(check.inr(49320.6), "₹49,320.60")


if __name__ == "__main__":
    unittest.main()
