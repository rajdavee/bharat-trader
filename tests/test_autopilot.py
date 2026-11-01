"""trader-autopilot tests, offline, on REAL bars.

tests/fixtures/trader-lab/   REAL daily bars of ten NIFTY 50 stocks, NIFTY 50 and India VIX (30 Oct 2024 to 30 Oct
                             2026) and their 5-minute bars (5 Aug to 30 Oct 2026), shared with the lab and co-trader
                             tests. On the 18 Sep 2026 close SMA20 fired on BHARTIARTL and AXISBANK.
tests/fixtures/trader-lab/archive/
                             the REAL trimmed trader-archive (NIFTY F&O bhavcopies, October 2026).

SMA20_WATCH has lenient gates on purpose (like the co-trader's tests) so trader-lab says WATCH and the auto-pilot
has something to trade: the real verdict of every built-in strategy on these bars is REJECT (RESEARCH), which the
auto-pilot never trades, and that is tested too. Every replay re-runs the lab as of the close before the day.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

ap = load("trader-autopilot", "autopilot")
lab = ap.lab_api()
risk = ap.risk_api()
broker = ap.broker_api()
LABFIX = os.path.join(FIX, "trader-lab")
ARCHIVE = os.path.join(LABFIX, "archive")
TEN = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"]
LENIENT = {"min_oos_trades": 0, "min_oos_expectancy_r": -100, "min_oos_profit_factor": 0, "max_drawdown_pct": 100,
           "min_dsr": 0, "max_pbo": 1.01, "min_neighbourhood_positive": 0, "min_forward_trades": 100,
           "demote_drawdown_mult": 1000}
DAY = "2026-09-21"          # the session after the 18 Sep signal


def sma20(name, stop, gates=None):
    d = {"name": name, "title": "Close crosses above the 20-day average", "timeframe": "1d", "product": "CNC",
         "universe": TEN, "params": {"stop_atr": stop},
         "entry": {"all": [["close", "crosses_above", {"sma": 20}]]}, "stop": {"atr": 14, "mult": "$stop_atr"},
         "target": {"r": 2}, "exit": {"max_bars": 15}, "grid": {"stop_atr": [stop, stop + 1]},
         "walk_forward": {"in_sample": 120, "out_of_sample": 40}}
    if gates:
        d["gates"] = gates
    return d


SMA20_WATCH = sma20("t-sma20-watch", 2, LENIENT)
SMA20_REJECT = sma20("t-sma20-research", 3)


def base_config(**over):
    with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(over)
    return cfg


def make_wd(specs=(SMA20_WATCH,), strategies=None, cfg=None, **ap_over):
    """A user folder: trader/config.json with an autopilot block and the strategies' specs in trader/lab/."""
    wd = tempfile.mkdtemp(prefix="bt-autopilot-")
    for doc in specs:
        d = os.path.join(wd, "trader", "lab", doc["name"])
        os.makedirs(d)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
    c = cfg if cfg is not None else base_config()
    block = {"strategies": strategies if strategies is not None else
             {"t-sma20-watch": {"capital": 60000, "max_positions": 2}}}
    block.update(ap_over)
    c["autopilot"] = block
    write(os.path.join(wd, "trader", "config.json"), c)
    return wd


def write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)


def read(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def box(wd, name=DAY):
    return ap.Paths(os.path.join(wd, "trader", "autopilot", "replays", name))


def run(wd, day=DAY, to=None, **kw):
    return ap.replay(wd, day, to, LABFIX, os.path.join(wd, "no-archive"), **kw)


def bars(sym, day):
    import gzip
    with gzip.open(os.path.join(LABFIX, "%s-5m.csv.gz" % sym), "rt", encoding="utf-8") as fh:
        rows = [r.split(",") for r in fh.read().splitlines()[1:]]
    return [{"time": r[1], "o": float(r[2]), "h": float(r[3]), "l": float(r[4]), "c": float(r[5])}
            for r in rows if r[0] == day]


def timeline(paths, day=DAY):
    return ap.read_timeline(paths, day)


class ReplayedDay(unittest.TestCase):
    """The full day, end to end: pre-open, entries, manage, the flat-by and the end-of-day report, with the
    strategy day-traded (product MIS) so the flat-by is part of it."""

    @classmethod
    def setUpClass(cls):
        cls.wd = make_wd(strategies={"t-sma20-watch": {"capital": 60000, "max_positions": 2, "product": "MIS"}})
        cls.reports = run(cls.wd)
        cls.rep = cls.reports[0]
        cls.p = box(cls.wd)
        cls.cfg = read(os.path.join(cls.wd, "trader", "config.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wd, ignore_errors=True)

    def test_every_phase_in_order(self):
        kinds = [e["kind"] for e in timeline(self.p)]
        self.assertEqual(kinds, ["pre-open", "placed", "placed", "fill", "fill", "exit", "exit", "eod"])
        self.assertEqual(self.rep["phases"], {"pre_open": "09:00", "eod": "15:30"})
        pre = self.rep["pre_open"]
        self.assertIn("trending down", pre["regime"])
        self.assertIn("2 candidate(s)", pre["cotrader"])
        self.assertIn("replay", pre["doctor"])
        at = [e["at"] for e in timeline(self.p)]
        self.assertEqual(at[1:3], ["2026-09-21 09:30:01", "2026-09-21 09:30:02"])     # a second apart
        self.assertEqual(at[5:7], ["2026-09-21 15:10:00", "2026-09-21 15:10:00"])     # the flat-by

    def test_fills_follow_the_bars_by_hand(self):
        st = read(self.p.trades)["trades"]
        self.assertEqual(len(st), 4)
        by = {(t["symbol"], t["side"]): t for t in st}
        for sym, limit, qty in (("BHARTIARTL", 1893.30, 10), ("AXISBANK", 1257.00, 14)):
            b = [x for x in bars(sym, DAY) if x["time"] >= "09:30"]
            first = next(x for x in b if x["l"] <= limit)
            buy = by[(sym, "BUY")]
            self.assertEqual((buy["qty"], buy["price"], buy["product"]), (qty, min(first["o"], limit), "MIS"))
            # out by 15:10: the 15:05 bar's close less the spec's slippage (3 bps or a tick), a tick down
            c = next(x for x in b if x["time"] == "15:05")["c"]
            want = ap.tick_price(c - lab.slippage(c, lab.COST_DEFAULTS), "down")
            self.assertEqual(by[(sym, "SELL")]["price"], want)
        self.assertEqual(by[("BHARTIARTL", "BUY")]["price"], 1848.90)        # it opened below the limit
        self.assertEqual(by[("BHARTIARTL", "SELL")]["price"], 1830.20)

    def test_exact_charges(self):
        cfg = self.cfg
        total, realised = 0.0, 0.0
        trades = read(self.p.trades)["trades"]
        for t in trades:
            want = broker.side_charges(t["side"], t["price"], t["qty"], cfg, "MIS")
            self.assertEqual(t["charges"], want["total"], t)
            total += t["charges"]
        for sym in ("BHARTIARTL", "AXISBANK"):
            buy = next(t for t in trades if t["symbol"] == sym and t["side"] == "BUY")
            sell = next(t for t in trades if t["symbol"] == sym and t["side"] == "SELL")
            net = round((sell["price"] - buy["price"]) * buy["qty"] - buy["charges"] - sell["charges"], 2)
            self.assertAlmostEqual(sell["realised_pnl"], net, places=2)
            # the same as the round trip trader-budget, trader-risk and the lab all use
            rt = broker.round_trip_charges(buy["price"], sell["price"], buy["qty"], broker.charges_config(cfg, "MIS"))
            self.assertAlmostEqual(buy["charges"] + sell["charges"], rt["total"], delta=0.02)
            realised += net
        t = self.rep["totals"]
        self.assertEqual((t["fills"], t["closed"]), (4, 2))
        self.assertAlmostEqual(t["charges"], total, places=2)
        self.assertAlmostEqual(t["realised"], realised, places=2)
        self.assertEqual((t["charges"], t["realised"]), (38.10, -255.90))      # BHARTIARTL -206.53, AXISBANK -49.37
        s = self.rep["by_strategy"]["t-sma20-watch"]
        self.assertEqual((s["fills"], s["closed"], s["wins"], s["charges"], s["realised"]), (4, 2, 0, 38.10, -255.90))
        pf = read(self.p.portfolio)
        self.assertEqual(pf["positions"], {})
        self.assertAlmostEqual(pf["cash"], 100000 + realised, places=2)

    def test_sized_inside_the_bucket_and_risk_passed(self):
        for e in [e for e in timeline(self.p) if e["kind"] == "placed"]:
            self.assertIn("trader-risk PASS", e["words"])
        st = read(self.p.state)
        for p in [p for p in read(os.path.join(self.p.records, "t-sma20-watch.json"))["trades"]]:
            self.assertLessEqual(p["qty"] * p["entry"], 60000)
        for f in os.listdir(self.p.orders):
            if f.endswith("-risk.json"):
                self.assertEqual(read(os.path.join(self.p.orders, f))["results"][0]["verdict"], "PASS")
        self.assertEqual(st["mode"], "replay")

    def test_machine_yes_is_recorded_and_labelled(self):
        recs = read(self.p.broker_state)["orders"]
        placed = [r for r in recs if r["status"] == "placed"]
        self.assertEqual(len(placed), 4)                    # two entries, two flat-by exits
        for r in placed:
            self.assertEqual((r["actor"], r["yes_text"], r["broker"], r["paper"]),
                             ("autopilot", ap.YES_TEXT, "paper", True))
        self.assertFalse([r for r in recs if r["status"] in ("approved", "sent") or r.get("live")])
        ok, why = broker.parse_yes(ap.YES_TEXT, {"symbol": "SBIN", "side": "BUY", "qty": 3})
        self.assertTrue(ok, why)

    def test_the_report_files(self):
        rep = read(self.p.day(DAY, "report.json"))
        self.assertEqual((rep["schema"], rep["date"], rep["paper"]), ("bharat-trader/autopilot-report/1", DAY, True))
        with open(self.p.day(DAY, "report.md"), encoding="utf-8") as fh:
            md = fh.read()
        for want in ("# Auto-pilot report, 2026-09-21 (replay, paper only)", "flat by 15:10 IST (MIS)",
                     "-₹255.90", "₹38.10", "What this does NOT show", "not financial advice"):
            self.assertIn(want, md)
        low = md.lower() + json.dumps(rep).lower()
        for word in ("guaranteed", "sure-shot", "sure shot", "risk-free"):
            self.assertNotIn(word, low)

    def test_forward_record_fed_with_the_paper_fills(self):
        fwd = read(os.path.join(self.p.lab, "t-sma20-watch", "forward.json"))
        mine = [t for t in fwd["trades"] if t.get("source") == "trader-autopilot paper fills"]
        self.assertEqual(sorted(t["symbol"] for t in mine), ["AXISBANK", "BHARTIARTL"])
        self.assertEqual(len(fwd["trades"]), 2)             # the paper fill replaced the lab's simulated trade
        for t in mine:
            self.assertEqual(t["signal_date"], "2026-09-18")
            self.assertLess(t["r"], 0)
        st = read(os.path.join(self.p.lab, "t-sma20-watch", "status.json"))
        self.assertEqual((st["verdict"], st["forward"]["trades"]), ("WATCH", 2))
        self.assertEqual(self.rep["forward"]["t-sma20-watch"]["after"], "WATCH")
        cal = self.rep["calibration"]
        self.assertEqual((cal["resolved"], cal["demoted"]), (2, []))     # resolved from the paper fills
        self.assertEqual(self.rep["journal"]["status"], "skipped")

    def test_the_users_own_account_is_untouched(self):
        t = os.path.join(self.wd, "trader")
        for name in ("portfolio.json", os.path.join("state", "broker.json"), os.path.join("state", "trades.json"),
                     "orders.json", "cotrader.json"):
            self.assertFalse(os.path.exists(os.path.join(t, name)), name)

    def test_status_and_report_commands(self):
        s = ap.status(self.p.workdir)
        self.assertFalse(s["running"])
        self.assertEqual((s["day"], s["positions"]), (DAY, []))
        self.assertEqual(s["records"]["t-sma20-watch"]["trades"], 2)
        self.assertEqual(s["strategies"]["t-sma20-watch"]["verdict"], "WATCH")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ap.main(["report", "--workdir", self.wd, "--replay", DAY, "--date", DAY, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["totals"]["realised"], -255.90)
        self.assertIn("RUNNING" if s["running"] else "not running", ap.render_status(s))


class CarriedOver(unittest.TestCase):
    """The strategy's own product (CNC, delivery): held overnight, stopped out on later days with an exit order
    (trader-risk, the machine yes, the paper fill)."""

    @classmethod
    def setUpClass(cls):
        cls.wd = make_wd()
        cls.reports = run(cls.wd, DAY, "2026-09-25")
        cls.p = box(cls.wd, DAY + "_2026-09-25")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wd, ignore_errors=True)

    def test_held_overnight_then_stopped(self):
        self.assertEqual([r["date"] for r in self.reports], ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
                                                             "2026-09-25"])
        first = self.reports[0]
        self.assertEqual((first["totals"]["fills"], first["totals"]["closed"]), (2, 0))
        self.assertEqual(sorted(p["symbol"] for p in first["open_positions"]), ["AXISBANK", "BHARTIARTL"])
        self.assertEqual({p["product"] for p in first["open_positions"]}, {"CNC"})
        carry = [e for e in timeline(self.p, "2026-09-22") if e["kind"] == "carry"]
        self.assertEqual(len(carry), 1)
        rec = read(os.path.join(self.p.records, "t-sma20-watch.json"))["trades"]
        self.assertEqual([(t["symbol"], t["exit_at"][:10]) for t in rec],
                         [("AXISBANK", "2026-09-24"), ("BHARTIARTL", "2026-09-25")])
        for t in rec:
            self.assertIn("stop", t["exit_why"])
            self.assertLess(t["net"], 0)
        trades = read(self.p.trades)["trades"]
        cfg = read(os.path.join(self.wd, "trader", "config.json"))
        for t in trades:
            self.assertEqual(t["charges"], broker.side_charges(t["side"], t["price"], t["qty"], cfg, "CNC")["total"])
        sells = [t for t in trades if t["side"] == "SELL"]
        self.assertEqual([t["leg"] for t in sells], ["order", "order"])          # exit orders on a later day
        exits = [r for r in read(self.p.broker_state)["orders"] if r["status"] == "placed" and r["side"] == "SELL"]
        self.assertEqual([r["actor"] for r in exits], ["autopilot", "autopilot"])
        self.assertEqual(self.reports[-1]["open_positions"], [])


class PaperOnly(unittest.TestCase):
    LIVE = (dict(paper=False, broker="upstox", live_enabled={"upstox": True}), dict(paper=True, broker="kite"),
            dict(paper=False, broker="paper"), dict(paper=True, broker="paper", shadow=True))

    def test_refuses_a_live_config_everywhere(self):
        for live in self.LIVE:
            with self.assertRaises(ap.AutopilotError):
                ap.paper_guard(base_config(**live))
            wd = make_wd(cfg=base_config(**live))
            try:
                with self.assertRaises(ap.AutopilotError) as e:
                    run(wd)
                self.assertIn("PAPER", str(e.exception))
                with self.assertRaises(ap.AutopilotError):
                    ap.run_loop(wd, once=True, data=LABFIX, fetch=False, now_text="2026-09-21 10:00", log=lambda m: None)
                self.assertFalse(os.path.exists(os.path.join(wd, "trader", "autopilot", "lock.json")))
                self.assertFalse(os.path.exists(os.path.join(wd, "trader", "autopilot", "state.json")))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = ap.main(["start", "--once", "--offline", "--workdir", wd, "--data", LABFIX, "--now",
                                  "2026-09-21 10:00"])
                self.assertEqual(rc, 1)
                self.assertIn("PAPER", err.getvalue())
            finally:
                shutil.rmtree(wd, ignore_errors=True)

    def test_never_the_live_place_path(self):
        p = ap.Pilot(tempfile.mkdtemp(prefix="bt-ap-"), ap.Market(".", LABFIX, fetch=False))
        for cmd in ("place", "cancel", "login", "preflight", "orders", "holdings"):
            with self.assertRaises(ap.AutopilotError):
                p.run_broker([cmd, "--index", "1"])
        with open(os.path.join(ROOT, "trader-autopilot", "autopilot.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('"place"', src)
        self.assertNotIn('"cancel"', src)
        self.assertEqual(ap.BROKER_COMMANDS, ("confirm", "fill", "settle", "adjust"))
        # and a whole day never reaches it
        old = broker.cmd_place

        def boom(a):
            raise AssertionError("the auto-pilot reached trader-broker's live place")
        broker.cmd_place = boom
        wd = make_wd(strategies={"t-sma20-watch": {"capital": 60000, "product": "MIS"}})
        try:
            run(wd)
        finally:
            broker.cmd_place = old
            shutil.rmtree(wd, ignore_errors=True)

    def test_the_broker_refuses_an_autopilot_yes_on_a_live_config(self):
        wd = tempfile.mkdtemp(prefix="bt-ap-broker-")
        try:
            t = os.path.join(wd, "trader")
            write(os.path.join(t, "config.json"), base_config(paper=False, broker="upstox", live_enabled={"upstox": True}))
            write(os.path.join(t, "orders.json"), {"orders": [{"symbol": "SBIN", "exchange": "NSE", "side": "BUY",
                                                               "qty": 1, "order_type": "MARKET", "product": "MIS"}]})
            argv = ["confirm", "--orders", os.path.join(t, "orders.json"), "--config", os.path.join(t, "config.json"),
                    "--risk", os.path.join(t, "risk.json"), "--state", os.path.join(t, "state.json"), "--index", "1",
                    "--yes-text", "yes", "--actor", "autopilot"]
            p = ap.Pilot(wd, ap.Market(wd, LABFIX, fetch=False))
            rc, out, err = p.run_broker(argv)
            self.assertEqual(rc, 1)
            self.assertIn("never approves a live order", err)
            self.assertFalse(os.path.exists(os.path.join(t, "state.json")))
            # a live place refuses a yes the user did not type (the gate inside ticket.py place)
            with open(os.path.join(ROOT, "trader-broker", "ticket.py"), encoding="utf-8") as fh:
                self.assertIn('approved.get("actor", "user") != "user"', fh.read())
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_a_config_switched_to_live_mid_run_stops_the_loop(self):
        wd = make_wd(strategies={"t-sma20-watch": {"capital": 60000, "product": "MIS"}})

        def flip(t, paths):
            if t.strftime("%H:%M") == "11:00":
                cfg = read(paths.config)
                cfg.update(paper=False, broker="upstox", live_enabled={"upstox": True})
                write(paths.config, cfg)
        try:
            with self.assertRaises(ap.AutopilotError):
                run(wd, on_step=flip)
            p = box(wd)
            recs = read(p.broker_state)["orders"]
            self.assertTrue(all(r["at"] < "2026-09-21T11:00" for r in recs))
            self.assertEqual(len([r for r in recs if r["status"] == "placed"]), 2)
            self.assertFalse(os.path.exists(p.lock))
        finally:
            shutil.rmtree(wd, ignore_errors=True)


class NeverResearch(unittest.TestCase):
    def test_research_is_refused_in_the_config(self):
        for tiers in (["RESEARCH"], ["WATCH", "RESEARCH"], ["research"]):
            with self.assertRaises(ap.AutopilotError):
                ap.settings_of({"autopilot": {"tiers": tiers}})
        self.assertEqual(ap.settings_of({})["tiers"], ["PROMOTED", "WATCH"])

    def test_a_rejected_strategy_is_never_traded(self):
        wd = make_wd(specs=(SMA20_REJECT,), strategies={"t-sma20-research": {"capital": 60000}})
        try:
            rep = run(wd)[0]
            p = box(wd)
            self.assertEqual(rep["totals"]["fills"], 0)
            self.assertFalse(read(p.broker_state))
            self.assertIn("no candidate", rep["pre_open"]["cotrader"])
            st = read(os.path.join(p.lab, "t-sma20-research", "status.json"))
            self.assertEqual(st["verdict"], "REJECT")
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_a_research_candidate_is_skipped_even_if_offered(self):
        wd = make_wd()
        try:
            pilot = ap.Pilot(wd, ap.Market(wd, LABFIX, fetch=False), replay=True, fetch=False)
            pilot.load_config()
            now = ap.parse_now("2026-09-21 10:00")
            pilot.roll_day(DAY, now)
            c = {"id": "2026-09-21-t-sma20-watch-SBIN", "strategy": "t-sma20-watch", "tier": "RESEARCH",
                 "symbol": "SBIN", "date": DAY, "plan": {}}
            pilot.consider(c, DAY, now)
            self.assertEqual(pilot.state["positions"], [])
            ev = timeline(pilot.paths)
            self.assertEqual(ev[-1]["words"], "2026-09-21-t-sma20-watch-SBIN: RESEARCH (unproven): never traded by "
                                              "the auto-pilot")
        finally:
            shutil.rmtree(wd, ignore_errors=True)


class Breakers(unittest.TestCase):
    MIS = {"t-sma20-watch": {"capital": 60000, "max_positions": 2, "product": "MIS"}}

    def test_kill_switch_flattens_and_stops_entries(self):
        wd = make_wd(strategies=self.MIS)

        def kill(t, paths):
            if t.strftime("%H:%M") == "12:00":
                write(paths.kill, risk.kill_on(None, ap.aware(t), "test: stop everything"))
        try:
            rep = run(wd, on_step=kill)[0]
            p = box(wd)
            ev = timeline(p)
            k = [e for e in ev if e["kind"] == "kill"]
            self.assertEqual(len(k), 1)
            self.assertIn("kill switch on at 12:00", rep["entries_stopped"])
            exits = [e for e in ev if e["kind"] == "exit"]
            self.assertEqual([e["at"] for e in exits], ["2026-09-21 12:00:00", "2026-09-21 12:00:00"])
            for e in exits:
                self.assertIn("the kill switch", e["words"])
            sells = {t["symbol"]: t for t in read(p.trades)["trades"] if t["side"] == "SELL"}
            for sym in ("BHARTIARTL", "AXISBANK"):
                c = next(x for x in bars(sym, DAY) if x["time"] == "11:55")["c"]
                self.assertEqual(sells[sym]["price"], ap.tick_price(c - lab.slippage(c, lab.COST_DEFAULTS), "down"))
            self.assertEqual(read(p.portfolio)["positions"], {})
            self.assertEqual(rep["totals"]["closed"], 2)
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_kill_before_the_window_means_no_entries(self):
        wd = make_wd(strategies=self.MIS)

        def kill(t, paths):
            if t.strftime("%H:%M") == "09:05":
                write(paths.kill, risk.kill_on(None, ap.aware(t), "test"))
        try:
            rep = run(wd, on_step=kill)[0]
            self.assertEqual(rep["totals"]["fills"], 0)
            self.assertTrue(rep["entries_stopped"])
            self.assertFalse(read(box(wd).broker_state))
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_circuit_breaker_blocks_through_trader_risk(self):
        wd = make_wd(strategies=self.MIS, cfg=base_config(max_consecutive_losses=1))

        def lost(t, paths):
            if t.strftime("%H:%M") == "09:05":
                write(paths.trades, {"trades": [{"at": "2026-09-21T09:20:00+05:30", "date": DAY, "symbol": "TCS",
                                                 "side": "SELL", "qty": 5, "price": 3000.0, "leg": "stop",
                                                 "realised_pnl": -420.5, "paper": True}]})
        try:
            rep = run(wd, on_step=lost)[0]
            p = box(wd)
            blocked = [e for e in timeline(p) if e["kind"] == "blocked"]
            # the cooling-off ends at 09:35, the losing-streak breaker holds all day: each new reason is recorded
            self.assertEqual([e["at"][11:16] for e in blocked], ["09:30", "09:30", "09:45", "09:45"])
            self.assertIn("cooling-off", blocked[0]["words"])
            for e in blocked:
                self.assertTrue(any(r.startswith("breaker:") for r in e["reasons"]), e)
            self.assertEqual(rep["totals"]["fills"], 0)
            self.assertFalse(read(p.broker_state))
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_bucket_limits(self):
        wd = make_wd(strategies={"t-sma20-watch": {"capital": 60000, "max_positions": 1, "product": "MIS"}})
        wd2 = make_wd(strategies={"t-sma20-watch": {"capital": 60000, "max_daily_loss": 500, "product": "MIS"}})
        try:
            rep = run(wd)[0]
            self.assertEqual(rep["totals"]["fills"], 2)                     # one position, in and out
            notes = [e["words"] for e in timeline(box(wd)) if e["kind"] == "skip"]
            self.assertTrue(any("its max_positions" in w for w in notes), notes)
            rep2 = run(wd2)[0]
            self.assertEqual(rep2["totals"]["fills"], 0)
            notes = [e["words"] for e in timeline(box(wd2)) if e["kind"] == "skip"]
            self.assertTrue(all("daily loss budget ₹500.00" in w for w in notes), notes)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
            shutil.rmtree(wd2, ignore_errors=True)

    def test_config_validation(self):
        bad = [{"cadence_min": 7}, {"cadence_min": 0}, {"max_positions": 0}, {"strategies": {"x": {}}},
               {"strategies": {"Bad Name": {"capital": 1}}}, {"strategies": {"ok-name": {"capital": 1, "product": "NRML"}}},
               {"strategies": {"ok-name": {"capital": 1, "colour": "red"}}}, {"nonsense": 1},
               {"strategies": {"ok-name": {"capital": 1, "trail_lock_r": 0.5}}}]
        for b in bad:
            with self.assertRaises(ap.AutopilotError, msg=b):
                ap.settings_of({"autopilot": b})


class LockAndResume(unittest.TestCase):
    def test_one_loop_per_folder(self):
        wd = tempfile.mkdtemp(prefix="bt-ap-lock-")
        try:
            p = ap.Paths(wd)
            now = ap.parse_now("2026-09-21 10:00")
            self.assertTrue(ap.acquire_lock(p, "paper", now))
            with self.assertRaises(ap.AutopilotError) as e:
                ap.acquire_lock(p, "paper", now)
            self.assertIn("another auto-pilot loop is running", str(e.exception))
            ap.release_lock(p)
            self.assertFalse(os.path.exists(p.lock))
            # a lock held by another live process is respected; one left by a process that is gone is taken over
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                write(p.lock, {"pid": holder.pid, "started": "2026-09-21 09:00:00", "mode": "paper"})
                with self.assertRaises(ap.AutopilotError):
                    ap.acquire_lock(p, "paper", now)
                ok, words = ap.request_stop(wd)
                self.assertTrue(ok)
                self.assertTrue(os.path.exists(p.stop))
            finally:
                holder.kill()
                holder.wait()
            self.assertTrue(ap.acquire_lock(p, "paper", now))
            self.assertEqual(read(p.lock)["pid"], os.getpid())
            ap.release_lock(p)
            ok, words = ap.request_stop(wd)
            self.assertFalse(ok)
            self.assertIn("no auto-pilot loop", words)
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_start_once_takes_and_releases_the_lock(self):
        wd = make_wd()
        try:
            got = ap.run_loop(wd, once=True, data=LABFIX, fetch=False, now_text="2026-09-19 10:00", log=lambda m: None)
            self.assertEqual(got, "closed: weekend")
            self.assertFalse(os.path.exists(os.path.join(wd, "trader", "autopilot", "lock.json")))
            st = read(os.path.join(wd, "trader", "autopilot", "state.json"))
            self.assertEqual(st["heartbeat"]["phase"], "closed: weekend")
            self.assertEqual(ap.seconds_to_next(ap.parse_now("2026-09-21 10:02")), 200.0)
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_resume_after_a_crash_mid_day(self):
        strategies = {"t-sma20-watch": {"capital": 60000, "max_positions": 2, "product": "MIS"}}
        whole = make_wd(strategies=strategies)
        try:
            want = run(whole)[0]
            want_trades = read(box(whole).trades)["trades"]
        finally:
            shutil.rmtree(whole, ignore_errors=True)
        for point, n in (("after-fill", 1), ("after-confirm", 2), ("after-fill", 3)):
            wd = make_wd(strategies=strategies)
            try:
                with self.assertRaises(ap.SimulatedCrash):
                    run(wd, crash={"point": point, "n": n})
                p = box(wd)
                self.assertFalse(os.path.exists(p.lock))                 # released even on a crash
                got = run(wd, resume=True)[0]
                trades = read(p.trades)["trades"]
                self.assertEqual(len(trades), len(want_trades), point)   # no double fill, no double order
                self.assertEqual([(t["symbol"], t["side"], t["price"], t["charges"]) for t in trades],
                                 [(t["symbol"], t["side"], t["price"], t["charges"]) for t in want_trades])
                self.assertEqual(got["totals"], want["totals"], point)
                placed = [r for r in read(p.broker_state)["orders"] if r["status"] == "placed"]
                self.assertEqual(len(placed), 4)
            finally:
                shutil.rmtree(wd, ignore_errors=True)


class PromoteDemote(unittest.TestCase):
    def test_the_forward_record_moves_the_verdict(self):
        doc = copy.deepcopy(SMA20_WATCH)
        doc["gates"] = dict(LENIENT, min_forward_trades=2)
        wd = make_wd(specs=(doc,), strategies={"t-sma20-watch": {"capital": 60000, "product": "MIS"}})
        try:
            reps = run(wd, DAY, "2026-09-22")
            f = reps[0]["forward"]["t-sma20-watch"]
            self.assertEqual((f["before"], f["after"], f["changed"]), ("WATCH", "REJECT", True))
            self.assertIn("the last 2 forward trades average -0.222R", f["why"])
            p = box(wd, DAY + "_2026-09-22")
            st = read(os.path.join(p.lab, "t-sma20-watch", "status.json"))
            self.assertEqual(st["verdict"], "REJECT")
            self.assertEqual([h["verdict"] for h in st["history"]], ["WATCH", "REJECT"])
            with open(p.day(DAY, "report.md"), encoding="utf-8") as fh:
                self.assertIn("WATCH -> REJECT (CHANGED)", fh.read())
            # the next session it is RESEARCH: never traded again
            self.assertEqual(reps[1]["totals"]["fills"], 0)
        finally:
            shutil.rmtree(wd, ignore_errors=True)


class Baskets(unittest.TestCase):
    """An options spec through the lab's options forward test (lenient gates: WATCH) on the real October archive."""

    @staticmethod
    def strangle():
        doc = copy.deepcopy(lab.library()["nifty-weekly-strangle-16d"])
        doc.update({"name": "t-strangle-4d", "walk_forward": {"in_sample": 6, "out_of_sample": 3},
                    "gates": dict(LENIENT, survive_cost_multiplier=1.5)})
        doc["params"]["dte"] = 4
        doc["entry"]["days_to_expiry"] = "$dte"
        return doc

    def basket_wd(self, **cfg):
        wd = make_wd(specs=(self.strangle(),), strategies={"t-strangle-4d": {"capital": 300000, "max_positions": 1}},
                     cfg=base_config(**cfg))
        shutil.copyfile(os.path.join(FIX, "trader-archive", "fo_mktlots.csv"),
                        os.path.join(wd, "trader", "fo_mktlots.csv"))
        return wd

    def test_blocked_while_fno_is_off(self):
        wd = self.basket_wd()
        try:
            rep = ap.replay(wd, "2026-10-26", None, None, ARCHIVE)[0]
            ev = [e for e in timeline(box(wd, "2026-10-26"), "2026-10-26") if e["kind"] == "blocked"]
            self.assertEqual(len(ev), 1)
            self.assertTrue(any(r.startswith("fno:") for r in ev[0]["reasons"]))
            self.assertEqual(rep["totals"]["fills"], 0)
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_one_yes_for_the_basket_and_an_honest_unfilled_limit(self):
        wd = self.basket_wd(capital=500000, allow_fno=True, allow_naked_short=True, max_loss_per_trade=50000,
                            max_daily_loss=60000, max_order_value=1000000)
        try:
            rep = ap.replay(wd, "2026-10-26", None, None, ARCHIVE)[0]
            p = box(wd, "2026-10-26")
            ev = timeline(p, "2026-10-26")
            placed = [e for e in ev if e["kind"] == "placed"]
            self.assertEqual(len(placed), 1)
            self.assertIn("short-strangle basket 2026-10-26-t-strangle-4d-NIFTY (2 legs, one position)",
                          placed[0]["words"])
            legs = [r for r in read(p.broker_state)["orders"] if r["status"] == "placed"]
            self.assertEqual(len(legs), 2)
            self.assertEqual({(r["actor"], r["basket_id"] is not None, r["kind"]) for r in legs},
                             {("autopilot", True, "entry")})
            orders = read(os.path.join(p.orders, "AP-20261026-1-basket.json"))
            # the spec's stop (the lab's latest parameters: 1.5 x the credit of 5,300.75) is the basket's risk rule
            self.assertEqual({o["risk_rule"]["max_loss"] for o in orders["orders"]}, {7951.13})
            # the legs are LIMITs at the 23 Oct settle; at the 26 Oct close the short put was far cheaper: the paper
            # broker does not fill a SELL LIMIT below its price, so the basket is not filled and is cancelled
            note = [e for e in ev if e["kind"] == "note" and "fill did not happen" in e["words"]]
            self.assertEqual(len(note), 1)
            self.assertIn("the SELL LIMIT 39.60 waits", note[0]["words"])
            self.assertEqual(rep["totals"]["fills"], 0)
            self.assertFalse(read(p.trades))
            st = read(p.state)
            self.assertEqual(st["positions"], [])
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    def test_end_of_day_chain_from_the_archive(self):
        docs = ap.eod_chain("NIFTY", "2026-10-26", ARCHIVE)
        self.assertTrue(docs)
        d = next(x for x in docs if x["expiry"] == "2026-10-27")
        self.assertEqual((d["underlying"], d["asof"]), ("NIFTY", "2026-10-26T15:30:00+05:30"))
        raw = ap.archive_api().load_fo_chain_eod("NIFTY", "2026-10-26", root=ARCHIVE)
        row = next(o for o in raw["options"] if o["expiry"] == "2026-10-27" and o["option_type"] == "PE"
                   and o["strike"] == 22250)
        marks = ap.chain_marks(docs)
        self.assertEqual(marks[(22250.0, "pe", "2026-10-27")], row["settle"] or row["close"])
        self.assertEqual(marks[(22250.0, "pe", "2026-10-27")], 8.45)        # 8.40 to a seller, after slippage
        self.assertEqual(ap.eod_chain("NIFTY", "2026-11-02", ARCHIVE), [])      # not archived: no chain


if __name__ == "__main__":
    unittest.main()
