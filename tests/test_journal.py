"""trader-journal tests, offline: entries from a real paper history, broker trade-book imports, the
statistics (Wilson), the behaviour flags, the counterfactual replays and the weekly review.

Fixtures:
  trader-journal/paper/trader/     a paper week driven through the real trader-risk check.py and trader-broker
                                   ticket.py (confirm, fill, stop and target legs, flatten) at the real 5-minute
                                   bars of tests/fixtures/trader-lab (26-30 Oct 2026), then the NIFTY iron condor
                                   of tests/test_broker_fno.py on the real 30-Oct-2026 chain (opened, short call
                                   rolled, closed) on 2 Nov. config.json, portfolio.json, state/trades.json,
                                   state/broker.json, state/personas.json and journal/risk-log.jsonl (each
                                   check.py verdict captured by journal.capture_risk, three of them BLOCKs:
                                   duplicate, trades a day, entry window)
  trader-journal/imports/          broker trade books, DOC-DERIVED: the column layouts of each broker's own
                                   documentation (see LAYOUTS in journal.py for the pages), filled with prices
                                   from the same real 5-minute bars. zerodha-tradebook.csv (Console Tradebook),
                                   upstox-trades.csv (API v2 historical trades: a date, no time),
                                   dhan-trades.csv (DhanHQ trade history, with charges), angel-tradebook.csv
                                   (SmartAPI trade book: time only), groww-trades.json (Trade API trade list, UTC,
                                   one REJECTED row), kite-live-trades.json (trader-broker's broker-neutral
                                   `ticket.py orders --broker kite --json` trades)
  trader-cotrader/log.jsonl        trader-cotrader's own fixture prediction log (schema bharat-trader/cotrader-log/1):
                                   38 daily candidates the co-trader produced on the lab fixture bars, read through
                                   trader-cotrader's read_log
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

journal = load("trader-journal", "journal")
JF = os.path.join(FIX, "trader-journal")
BARS = os.path.join(FIX, "trader-lab")
NOW = "2026-11-02 16:00"


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def text_of(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = journal.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Workdir(unittest.TestCase):
    IMPORT = ("zerodha-tradebook.csv",)
    COTRADER = True

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-journal-")
        shutil.copytree(os.path.join(JF, "paper", "trader"), os.path.join(self.dir, "trader"))
        if self.COTRADER:
            os.makedirs(os.path.join(self.dir, "trader", "cotrader"))
            shutil.copy(os.path.join(FIX, "trader-cotrader", "log.jsonl"), os.path.join(self.dir, "trader", "cotrader"))
        self.paths = journal.Paths(self.dir)
        for f in self.IMPORT:
            self.ok("import", os.path.join(JF, "imports", f))
        journal._BARS.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def ok(self, *argv):
        code, out, err = run(*(list(argv) + ["--workdir", self.dir, "--bars", BARS, "--now", NOW]))
        self.assertEqual(code, 0, out + err)
        return out

    def doc(self):
        return journal.gather(self.paths, BARS, True, True, journal.parse_now(NOW))

    def entry(self, doc, symbol, day, source=None):
        got = [e for e in doc["entries"] if e["symbol"] == symbol and e["opened_at"][:10] == day
               and (source is None or e["source"] == source)]
        self.assertEqual(len(got), 1, [(e["symbol"], e["opened_at"], e["source"]) for e in doc["entries"]])
        return got[0]


class Entries(Workdir):
    IMPORT = ()

    def test_one_entry_per_closed_paper_trade(self):
        doc = self.doc()
        self.assertEqual(len(doc["entries"]), 11)
        self.assertEqual(doc["open"], [])
        trades = jload(os.path.join(self.dir, "trader", "state", "trades.json"))["trades"]
        for e in doc["entries"]:
            if e["kind"] != "equity":
                continue
            # the paper broker's realised P&L on the sells already carries the buy-side charges
            sells = [t for t in trades if t["symbol"] == e["symbol"] and t["side"] == "SELL"
                     and t["at"][:19].replace("+05:30", "") in [f["at"] for f in e["fills"]]]
            self.assertAlmostEqual(e["net"], sum(t["realised_pnl"] for t in sells), places=2)
            self.assertAlmostEqual(e["gross"], round((e["exit_price"] - e["entry_price"]) * e["qty"], 2), places=2)

    def test_equity_entry_fields(self):
        e = self.entry(self.doc(), "RELIANCE", "2026-10-26")
        self.assertEqual((e["side"], e["qty"], e["entry_price"], e["exit_price"]), ("long", 75, 1193.9, 1203.5))
        self.assertEqual((e["opened_at"], e["closed_at"], e["holding_minutes"]),
                         ("2026-10-26T10:01:00", "2026-10-26T13:29:00", 208))
        self.assertEqual((e["gross"], e["net"]), (720.0, 640.81))
        self.assertEqual(e["charges"], round(e["gross"] - e["net"], 2))
        self.assertEqual((e["persona"], e["strategy"], e["setup"]), ("intraday-trader", "orb15-vwap", "lab:orb15-vwap"))
        self.assertEqual(e["risk"], 438.88)           # broker.json: the max loss recorded with the yes
        self.assertEqual(e["r_multiple"], round(640.81 / 438.88, 3))
        self.assertEqual((e["tod"], e["weekday"], e["instrument"]), ("morning", "Monday", "equity intraday (MIS)"))
        self.assertTrue(e["ticketed"])
        self.assertEqual(e["regime"], "trend|high")   # NIFTY on Fri 23 Oct, the close before (trader-regime)
        # MAE/MFE from the real 5-minute bars between the fills
        self.assertEqual(e["excursion"]["timeframe"], "5m")
        self.assertLessEqual(e["mae"], 0)
        self.assertGreaterEqual(e["mfe"], round((1203.5 - 1193.9) * 75, 2) - 0.01)

    def test_iron_condor_basket_is_one_entry(self):
        doc = self.doc()
        e = self.entry(doc, "NIFTY", "2026-11-02")
        pf = jload(os.path.join(self.dir, "trader", "portfolio.json"))
        st = pf["strategies"]["S-20261102-1"]
        self.assertEqual((e["kind"], e["strategy"], e["persona"]), ("fno", "iron-condor", "options-seller"))
        self.assertEqual(len(e["fills"]), 4 + 2 + 4)          # open, roll the short call, close all
        self.assertEqual(e["legs"], 5)                        # 23200 CE rolled to 23250 CE
        self.assertEqual(e["net"], st["realised"])            # -272.71: every rupee of the strategy
        self.assertEqual(e["charges"], st["charges"])         # 262.96
        self.assertEqual(e["opened_for"], "credit")
        self.assertEqual(e["entry_price"], round((44.30 + 44.85 - 28.70 - 24.05) * 65, 2))   # 2,366 received
        self.assertEqual(e["instrument"], "index options")
        self.assertEqual(e["opened_at"], "2026-11-02T10:01:00")
        self.assertEqual(e["closed_at"], "2026-11-02T12:01:00")
        self.assertIsNone(e["mae"])                            # no option bars: nothing invented

    def test_ids_are_stable_and_notes_stick(self):
        first = [e["id"] for e in self.doc()["entries"]]
        eid = first[0]
        out = self.ok("note", eid, "held the plan through the dip", "--tag", "followed-plan")
        self.assertIn("1 note, tags followed-plan", out)
        self.ok("sync")
        doc = jload(self.paths.journal)
        self.assertEqual([e["id"] for e in doc["entries"]], first)
        e = doc["entries"][0]
        self.assertEqual(e["notes"][0]["text"], "held the plan through the dip")
        self.assertEqual(e["tags"], ["followed-plan"])
        self.assertIn("held the plan", self.ok("show", eid))
        code, _, err = run("note", "nope", "x", "--workdir", self.dir)
        self.assertEqual(code, 1)
        self.assertIn("not a journal entry ID", err)


class Imports(unittest.TestCase):
    def parse(self, name, broker=None, day=None):
        with open(os.path.join(JF, "imports", name), encoding="utf-8-sig") as fh:
            return journal.parse_tradebook(fh.read(), broker, day)

    def test_layouts_are_detected(self):
        for name, want in (("zerodha-tradebook.csv", "zerodha"), ("upstox-trades.csv", "upstox"),
                           ("dhan-trades.csv", "dhan"), ("angel-tradebook.csv", "angel"),
                           ("groww-trades.json", "groww"), ("kite-live-trades.json", "live")):
            fills, broker, _ = self.parse(name, day="2026-10-28" if "angel" in name else None)
            self.assertEqual(broker, want, name)
            self.assertEqual(len(fills), 6 if "zerodha" in name else 2, name)
            self.assertEqual([f["side"] for f in fills[:2]], ["BUY", "SELL"], name)

    def test_zerodha(self):
        fills, _, _ = self.parse("zerodha-tradebook.csv")
        self.assertEqual(len(fills), 6)
        f = fills[0]
        self.assertEqual((f["symbol"], f["qty"], f["price"], f["at"], f["segment"], f["trade_id"]),
                         ("RELIANCE", 75, 1192.7, "2026-10-27T10:50:12", "EQ", "40121871"))
        self.assertTrue(f["charges"] is None and f["charges_estimated"])

    def test_dhan_carries_its_charges(self):
        fills, _, _ = self.parse("dhan-trades.csv")
        self.assertEqual([f["charges"] for f in fills], [29.9, 50.87])    # sebi+stt+brokerage+gst+exchange+stamp
        self.assertEqual([f["charges_estimated"] for f in fills], [False, False])
        self.assertEqual((fills[0]["product"], fills[0]["exchange"], fills[0]["at"]), ("MIS", "NSE", "2026-10-27T11:00:03"))

    def test_groww_utc_and_rejected_rows(self):
        fills, _, notes = self.parse("groww-trades.json")
        self.assertEqual([f["at"] for f in fills], ["2026-10-29T11:00:04", "2026-10-29T14:30:05"])   # Z -> IST
        self.assertIn("1 row skipped", notes[0])

    def test_angel_needs_the_date(self):
        fills, _, _ = self.parse("angel-tradebook.csv", day="2026-10-28")
        self.assertEqual((fills[0]["symbol"], fills[0]["at"]), ("ITC", "2026-10-28T10:00:07"))
        self.assertEqual(self.parse("angel-tradebook.csv")[0], [])

    def test_upstox_has_a_date_only(self):
        fills, _, _ = self.parse("upstox-trades.csv")
        self.assertEqual((fills[0]["symbol"], fills[0]["at"], fills[0]["time_known"]),
                         ("INFY", "2026-10-26T00:00:00", False))

    def test_unknown_columns_are_refused(self):
        with self.assertRaises(journal.JournalError):
            journal.parse_tradebook("a,b,c\n1,2,3\n")

    def test_option_symbols(self):
        self.assertEqual(journal.instrument_of("NIFTY-2026-11-03-22700-PE"), ("NIFTY-2026-11-03-22700-PE", "NIFTY", "OPT"))
        self.assertEqual(journal.instrument_of("NIFTY26N0322700PE")[1:], ("NIFTY", "OPT"))
        self.assertEqual(journal.instrument_of("BANKNIFTY26NOV52000CE")[1:], ("BANKNIFTY", "OPT"))
        self.assertEqual(journal.instrument_of("RELIANCE26NOVFUT")[1:], ("RELIANCE", "FUT"))
        self.assertEqual(journal.instrument_of("ITC-EQ"), ("ITC", "ITC", "EQ"))
        self.assertEqual(journal.instrument_of("NIFTY", None, "NIFTY", "CE", "23200.0", "2026-11-03"),
                         ("NIFTY-2026-11-03-23200-CE", "NIFTY", "OPT"))


class ImportFlow(Workdir):
    IMPORT = ("zerodha-tradebook.csv", "upstox-trades.csv", "dhan-trades.csv", "groww-trades.json",
              "kite-live-trades.json")

    def test_every_broker_becomes_entries(self):
        self.ok("import", os.path.join(JF, "imports", "angel-tradebook.csv"), "--broker", "angel", "--date", "2026-10-28")
        out = self.ok("import", os.path.join(JF, "imports", "zerodha-tradebook.csv"))     # again: no doubles
        self.assertIn("(0 new)", out)
        doc = self.doc()
        self.assertEqual(len(doc["entries"]), 11 + 3 + 5)
        e = self.entry(doc, "SBIN", "2026-10-27", "dhan")
        self.assertEqual(e["net"], round((955.0 - 961.2) * 100 - 29.9 - 50.87, 2))
        self.assertFalse(e["charges_estimated"])
        e = self.entry(doc, "INFY", "2026-10-26", "upstox")
        self.assertEqual(e["tod"], "unknown")
        self.assertNotIn("outside-window", [n["kind"] for n in e["rule_notes"]])
        e = self.entry(doc, "ICICIBANK", "2026-10-30", "live")
        self.assertEqual(e["gross"], round((1401.5 - 1392.2) * 70, 2))
        self.assertTrue(e["charges_estimated"])
        self.assertGreater(e["charges"], 0)
        self.assertIn("off-ticket", [n["kind"] for n in e["rule_notes"]])
        self.assertEqual(self.entry(doc, "ITC", "2026-10-28", "angel")["gross"], round((256.4 - 254.75) * 400, 2))


class Stats(Workdir):
    def test_wilson(self):
        lo, hi = journal.wilson(8, 10)                    # the textbook case
        self.assertEqual((round(lo, 3), round(hi, 3)), (0.490, 0.943))
        self.assertEqual(journal.wilson(0, 10)[0], 0.0)
        self.assertEqual(journal.wilson(0, 0), (None, None))

    def test_headline_numbers(self):
        s = self.doc()["stats"]
        self.assertEqual((s["trades"], s["wins"], s["losses"]), (14, 2, 12))
        lo, hi = journal.wilson(2, 14)
        self.assertEqual(s["win_rate_ci"], [lo, hi])
        self.assertEqual((round(lo, 3), round(hi, 3)), (0.040, 0.399))
        doc = self.doc()
        nets = [e["net"] for e in doc["entries"]]
        self.assertEqual(s["net"], round(sum(nets), 2))
        self.assertEqual(s["expectancy"], round(sum(nets) / 14, 2))
        wins = [x for x in nets if x > 0]
        self.assertEqual(s["profit_factor"], round(sum(wins) / -sum(x for x in nets if x < 0), 2))
        self.assertEqual(s["avg_win"], round(sum(wins) / 2, 2))
        self.assertTrue(s["small_sample"])
        self.assertIn("far too few", s["verdict"])
        eq = s["equity"]
        self.assertEqual(eq[-1]["equity"], s["net"])
        peak, dd, run_ = 0.0, 0.0, 0.0
        for p in eq:
            run_ += p["net"]
            peak = max(peak, run_)
            dd = max(dd, peak - run_)
        self.assertEqual(s["max_drawdown"], round(dd, 2))
        self.assertEqual(s["longest_loss_streak"], 11)       # 27 Oct to 2 Nov: every trade lost
        self.assertEqual(s["current_streak"], {"kind": "loss", "length": 11})

    def test_breakdowns(self):
        doc = self.doc()
        for by in journal.GROUPS:
            rows = journal.breakdown(doc["entries"], by)
            self.assertEqual(sum(r["trades"] for r in rows), 14, by)
        rows = {r["group"]: r for r in journal.breakdown(doc["entries"], "persona")}
        self.assertEqual(set(rows), {"intraday-trader", "swing-trader", "options-seller", "unattributed"})
        self.assertEqual(rows["unattributed"]["trades"], 4)          # three broker trades and one untagged paper
        rows = {r["group"]: r for r in journal.breakdown(doc["entries"], "weekday")}
        self.assertEqual(rows["Wednesday"]["trades"], 4)
        out = self.ok("stats", "--by", "strategy")
        self.assertIn("orb15-vwap", out)
        self.assertIn("95% Wilson range", out)


class Behaviour(Workdir):
    def flags(self, doc, symbol, day, source=None):
        return sorted({n["kind"] for n in self.entry(doc, symbol, day, source)["rule_notes"]})

    def test_rule_breaks(self):
        doc = self.doc()
        # the trades taken at the broker after trader-risk had blocked them
        self.assertEqual(self.flags(doc, "RELIANCE", "2026-10-27", "zerodha"), ["off-ticket", "revenge"])
        self.assertEqual(self.flags(doc, "RELIANCE", "2026-10-28", "zerodha"),
                         ["after-breaker", "off-ticket", "oversized", "overtrading"])
        self.assertEqual(self.flags(doc, "TCS", "2026-10-29", "zerodha"), ["off-ticket", "outside-window"])
        # the paper trades through the tools are clean, but the condor's max loss is over the per-trade limit
        for e in doc["entries"]:
            if e["source"] == "paper" and e["kind"] == "equity":
                self.assertEqual(e["rule_notes"], [], e["id"])
        self.assertEqual(self.flags(doc, "NIFTY", "2026-11-02"), ["oversized"])
        b = doc["behaviour"]
        self.assertEqual(b["counts"], {"off-ticket": 3, "revenge": 1, "after-breaker": 1, "oversized": 2,
                                       "overtrading": 1, "outside-window": 1})
        o = b["overtrading"]
        self.assertEqual((o["entries"], o["days"], o["plan_per_day"], o["days_over_plan"]), (14, 6, 3, {"2026-10-28": 4}))
        self.assertEqual(o["index"], round(14 / 6.0 / 3, 2))
        c = b["cost_drag"]
        self.assertIsNone(c["pct_of_gross"])                  # a gross loss: no share to speak of
        self.assertIn("made it worse", c["words"])
        self.assertEqual(c["charges"], doc["stats"]["charges"])

    def test_override_of_a_warning(self):
        e = {"id": "J20261027-aaaaaa", "opened_at": "2026-10-27T10:50:00", "closed_at": "2026-10-27T11:30:00",
             "underlying": "RELIANCE", "symbol": "RELIANCE", "net": -10.0, "gross": 0.0, "charges": 10.0,
             "kind": "equity", "product": "MIS", "risk": 400.0, "entry_price": 1192.7, "qty": 75, "ticketed": True,
             "fills": [{"at": "2026-10-27T10:50:00", "fingerprint": "abc"}]}
        v = {"abc": [{"verdict": "PASS", "warnings": ["revenge: RELIANCE lost ₹438.88 at 10:24 IST"]}]}
        b = journal.behaviour([e], dict(journal.DEFAULTS), v)
        self.assertEqual([r["kind"] for r in b["log"]], ["override"])
        self.assertIn("trader-risk warned: revenge", b["log"][0]["text"])

    def test_late_exit(self):
        e = {"id": "J20261027-bbbbbb", "opened_at": "2026-10-27T10:00:00", "closed_at": "2026-10-27T15:20:00",
             "underlying": "SBIN", "symbol": "SBIN", "net": 5.0, "gross": 25.0, "charges": 20.0, "kind": "equity",
             "product": "MIS", "risk": 300.0, "entry_price": 960.0, "qty": 50, "ticketed": True, "fills": []}
        b = journal.behaviour([e], dict(journal.DEFAULTS), {})
        self.assertEqual([r["kind"] for r in b["log"]], ["late-exit"])


class Counterfactual(Workdir):
    def test_cotrader_candidate_hand_computed(self):
        cf = journal.counterfactual(self.paths, self.doc()["entries"], journal.config_of(self.paths), BARS, True)
        row = next(r for r in cf["rows"] if r["id"] == "2026-02-26-t-sma20-research-ICICIBANK")
        # 26-Feb-2026 opens at 1405.10; slippage max(3 bps = 0.4215, one 0.10 tick) -> fill 1405.52.
        # The stop 1312.00 is first touched on 6-Mar (low 1310.40, open 1339 above it): 1312 - 0.3936 = 1311.61.
        self.assertEqual((row["result"], row["entry"], row["exit"], row["reason"]), ("replayed", 1405.52, 1311.61, "stop"))
        self.assertEqual(row["exit_bar"], "2026-03-06")
        self.assertEqual(row["gross"], round((1311.61 - 1405.52) * 11, 2))      # -1,033.01
        # delivery (CNC) charges on 11 shares, the example config's rates: brokerage 0.03% a side 8.97, STT 0.1%
        # of 29,888.43 = 29.89, stamp 0.015% of the buy 2.32, exchange 0.92, SEBI 0.03, GST 18% 1.78
        self.assertEqual(row["charges"], 43.91)
        self.assertEqual(row["net"], -1076.92)
        self.assertEqual(row["group"], "co-trader skipped")

    def test_risk_block_hand_computed(self):
        p = [x for x in journal.blocked_orders(journal.risk_records(self.paths)) if x["symbol"] == "RELIANCE"][0]
        self.assertEqual((p["date"], p["time"], p["entry"], p["stop"], p["target"], p["qty"]),
                         ("2026-10-27", "10:50", 1192.7, 1187.95, 1202.2, 75))
        self.assertIn("duplicate", p["reasons"][0])
        lab = journal.lab_api()
        r = journal.replay(p, journal.load_series(lab, "RELIANCE", "5m", self.paths, BARS), journal.config_of(self.paths), lab)
        # the 10:50 bar opens at 1192.70 (+0.36 slippage = 1193.06); the stop 1187.95 is hit at 14:20 (-0.36)
        self.assertEqual((r["entry"], r["exit"], r["reason"], r["exit_bar"]), (1193.06, 1187.59, "stop", "2026-10-27 14:20"))
        self.assertEqual(r["gross"], -410.25)
        # MIS: brokerage capped at 20 a side, STT 0.025% of the sell 22.27, exchange 5.48, SEBI 0.18, stamp 2.68,
        # GST 8.22
        self.assertEqual(r["charges"], 78.83)

    def test_ledger_totals(self):
        cf = journal.counterfactual(self.paths, self.doc()["entries"], journal.config_of(self.paths), BARS, True)
        g = cf["groups"]["co-trader skipped"]
        done = [r for r in cf["rows"] if r["group"] == "co-trader skipped" and r["result"] == "replayed"]
        self.assertEqual(g["plans"], 38)
        self.assertEqual(g["replayed"], len(done))
        self.assertEqual(g["saved"], round(-sum(r["net"] for r in done if r["net"] < 0), 2))
        self.assertEqual(g["cost"], round(sum(r["net"] for r in done if r["net"] > 0), 2))
        self.assertEqual(g["net_effect"], round(g["saved"] - g["cost"], 2))
        # the user went around all three blocks at the broker (Zerodha) or later on paper: taken, not replayed
        mine = cf["groups"]["your orders blocked by risk"]
        self.assertEqual((mine["plans"], mine["counts"]), (3, {"taken": 3}))
        self.assertTrue(cf["caveats"])
        out = self.ok("counterfactual")
        self.assertIn("co-trader skipped: 38 plans", out)

    def test_blocked_candidates_and_no_lab(self):
        p = os.path.join(self.dir, "trader", "cotrader", "log.jsonl")
        rows = [json.loads(l) for l in text_of(p).splitlines() if l.strip()]
        rows[0]["status"] = "blocked"
        rows[1]["entry"] = rows[1]["stop"] = None          # an options candidate: legs, no share entry or stop
        rows.append({"schema": "bharat-trader/other/1", "id": "x"})
        with open(p, "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
        plans = journal.read_cotrader_log(p)
        self.assertEqual(len(plans), 38)
        cf = journal.counterfactual(self.paths, [], journal.config_of(self.paths), BARS, True)
        self.assertEqual(cf["groups"]["co-trader blocked by risk"]["plans"], 1)
        self.assertEqual(cf["groups"]["co-trader skipped"]["counts"].get("not replayable"), 1)
        self.assertEqual(journal.cotrader_rows(self.paths)[1], "trader-cotrader read_log")


class NoImports(Workdir):
    IMPORT = ()
    COTRADER = False

    def test_blocks_replay_when_not_taken(self):
        cf = journal.counterfactual(self.paths, self.doc()["entries"], journal.config_of(self.paths), BARS, True)
        mine = cf["groups"]["your orders blocked by risk"]
        # RELIANCE twice (duplicate, trades a day) are replayed; TCS at 09:20 was taken later on paper at 09:46
        self.assertEqual(mine["counts"], {"replayed": 2, "taken": 1})
        self.assertEqual(mine["would_have_lost"], 2)
        self.assertEqual(mine["saved"], round(489.08 + 490.89, 2))
        self.assertIn("saved you ₹979.97", mine["words"])


class Review(Workdir):
    def test_weekly_report(self):
        out = self.ok("review", "--week", "2026-W44")
        path = os.path.join(self.dir, "trader", "journal", "weekly-2026-W44.md")
        self.assertTrue(os.path.isfile(path))
        text = text_of(path)
        self.assertIn("# Weekly review 2026-W44 (Mon 26 Oct 2026 to Sun 01 Nov 2026)", text)
        self.assertIn("| closed trades | 13 (2 won, 11 lost, 0 flat) |", text)      # the condor closed in W45
        self.assertIn("Wilson", text)
        self.assertIn("far too few to tell skill from luck", text)
        self.assertIn("2026-10-28 13:30 IST, RELIANCE (after-breaker)", text)
        self.assertIn("Days over plan: 2026-10-28 (4).", text)
        self.assertIn("your orders blocked by risk: 3 plans", text)
        self.assertIn("Under 20 closed trades: review process only", text)
        self.assertIn("ONE process change", text)
        for bad in ("guaranteed", "sure-shot", "risk-free"):
            self.assertNotIn(bad, text.lower())
        self.assertIn("Wrote trader/journal/weekly-2026-W44.md", out)
        res = journal.review(self.paths, "2026-W45", BARS, True, journal.parse_now(NOW))
        self.assertEqual(res["stats"]["trades"], 1)
        with self.assertRaises(journal.JournalError):
            journal.week_bounds("week 44", journal.parse_now(NOW))

    def test_summary_and_cli(self):
        s = journal.summary(self.paths, BARS, True, journal.parse_now(NOW))
        self.assertEqual(s["this_week"]["week"], "2026-W45")
        self.assertEqual(s["this_week"]["trades"], 1)
        self.assertEqual(set(s["by"]), set(journal.GROUPS))
        self.assertFalse(os.path.exists(self.paths.journal))       # read only
        out = self.ok("sync")
        self.assertIn("14 closed trades", out)
        self.assertTrue(os.path.exists(self.paths.journal))
        self.assertIn("J2026", self.ok("list"))
        self.assertIn("win rate 14% (95% range 4% to 40%, Wilson)", self.ok("stats"))


if __name__ == "__main__":
    unittest.main()
