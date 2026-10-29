"""trader-insider tests, offline.

Fixtures (tests/fixtures/trader-insider/), real NSE files fetched 2026-10-29:
  bulk.csv, block.csv          the 28-Oct-2026 files (bulk trimmed to 6 stocks, block whole)
  hist-bulk_deals-22-10-2026-28-10-2026.csv, hist-block_deals-...csv
                               the historical CSV for 22-28 Oct (bulk trimmed)
  pit-26-10-2026-29-10-2026.json   PIT filings list (corporates-pit-gg), 17 of 30 rows
  pit-legacy-26-10-2026-29-10-2026.json   the older list, empty for these dates
  pit-29-10-2025-29-10-2026-RELIANCE.json, pit-legacy-...-RELIANCE.json   one year of RELIANCE
  IT_*.xml, PIT_*.xml          each filing's XBRL (free-text remarks and ID numbers removed)
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

ins = load("trader-insider", "insider")
DIR = os.path.join(FIX, "trader-insider")


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8-sig") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(args)
    return code, out.getvalue(), err.getvalue()


class Workspace(object):
    """A temporary trader/ folder with a portfolio, a watchlist and a config."""

    def __init__(self, held=("DMART",), watched=("WESTLIFE",), clients=("POONAWALLA",)):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "trader")
        os.makedirs(self.dir)
        self._write("portfolio.json", {"cash": 50000,
                                       "positions": {s: {"qty": 10, "avg_price": 100} for s in held}})
        self._write("watchlist.json", {"mode": "orb", "hits": [{"symbol": s} for s in watched]})
        self._write("config.json", {"capital": 100000, "insider_watch_clients": list(clients)})

    def _write(self, name, data):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def args(self):
        return ["--portfolio", os.path.join(self.dir, "portfolio.json"),
                "--watchlist", os.path.join(self.dir, "watchlist.json"),
                "--config", os.path.join(self.dir, "config.json"), "--offline", DIR]

    def path(self, name):
        return os.path.join(self.dir, name)

    def close(self):
        self.tmp.cleanup()


class Formatting(unittest.TestCase):
    def test_inr_and_crore(self):
        self.assertEqual(ins.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(ins.inr(-54321.1), "-₹54,321.10")
        self.assertEqual(ins.crore(533885300), "₹53.39 cr")
        self.assertEqual(ins.crore(4147751659.0), "₹414.78 cr")
        self.assertEqual(ins.crore(30000000000), "₹3,000 cr")
        self.assertEqual(ins.crore(-88300000), "-₹8.83 cr")
        self.assertEqual(ins.qty(219803877), "21,98,03,877")
        self.assertEqual(ins.to_number("66,44,519"), 6644519)
        self.assertIsNone(ins.to_number("-"))

    def test_weekdays_between(self):
        import datetime as dt
        fri, mon = dt.date(2026, 10, 23), dt.date(2026, 10, 26)
        self.assertEqual(ins.weekdays_between(fri, mon), 1)
        self.assertEqual(ins.weekdays_between(mon, mon), 0)
        self.assertIsNone(ins.weekdays_between(mon, fri))


class Deals(unittest.TestCase):
    def test_parse_todays_block_file(self):
        deals = ins.parse_deals(read("block.csv"), "block")
        self.assertEqual(len(deals), 2)
        buy = next(d for d in deals if d["side"] == "BUY")
        self.assertEqual(buy["symbol"], "STLTECH")
        self.assertEqual(buy["client"], "POONAWALLA AVIATION PRIVATE LIMITED")
        self.assertEqual(buy["date"], "2026-10-28")
        self.assertEqual(buy["qty"], 601900)
        self.assertEqual(buy["price"], 887.0)
        self.assertEqual(buy["value_cr"], 53.39)
        self.assertIsNone(buy["remarks"])

    def test_parse_historical_csv_with_bom_quotes_and_indian_commas(self):
        deals = ins.parse_deals(read("hist-bulk_deals-22-10-2026-28-10-2026.csv"), "bulk")
        pc = next(d for d in deals if d["symbol"] == "PCJEWELLER" and d["side"] == "SELL"
                  and d["client"].startswith("JUMP") and d["date"] == "2026-10-22")
        self.assertEqual(pc["qty"], 219803877)
        self.assertEqual(pc["price"], 18.87)
        self.assertEqual(pc["value_cr"], 414.77)     # 21,98,03,877 x 18.87

    def test_double_spaces_in_names_collapse(self):
        deals = ins.parse_deals(read("bulk.csv"), "bulk")
        self.assertIn("MANSI CHANDAK", [d["client"] for d in deals])

    def test_wrong_file_is_refused(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_deals("a,b\n1,2\n", "bulk")

    def test_block_trade_repeated_in_bulk_list_is_kept_once(self):
        f = ins.Fetcher(offline=DIR)
        import datetime as dt
        deals, notes = ins.fetch_deals(f, dt.date(2026, 10, 22), dt.date(2026, 10, 28))
        self.assertEqual(notes, [])
        gall = [d for d in deals if d["symbol"] == "GALLANTT"]
        self.assertEqual(len(gall), 2)                          # in both files, kept once each
        self.assertEqual({d["kind"] for d in gall}, {"block"})
        cap = [d for d in deals if d["symbol"] == "CAPINVIT"]
        self.assertEqual(len(cap), 2)

    def test_filters(self):
        deals = ins.parse_deals(read("bulk.csv"), "bulk") + ins.parse_deals(read("block.csv"), "block")
        self.assertEqual(len(ins.filter_deals(deals, symbol="CARTRADE")), 2)
        self.assertEqual([d["side"] for d in ins.filter_deals(deals, client="serum institute")],
                         ["SELL"])
        big = ins.filter_deals(deals, min_value_cr=40)
        self.assertEqual(sorted(d["symbol"] for d in big), ["CARTRADE", "CARTRADE", "STLTECH", "STLTECH"])
        self.assertEqual(len(ins.filter_deals(deals, side="SELL", min_value_cr=40)), 2)

    def test_matched_trades(self):
        deals = ins.parse_deals(read("block.csv"), "block") + ins.parse_deals(read("bulk.csv"), "bulk")
        pairs = {p["symbol"]: p for p in ins.counterparties(deals)}
        self.assertEqual(pairs["STLTECH"]["seller"], "SERUM INSTITUTE OF INDIA PRIVATE LIMITED")
        self.assertEqual(pairs["STLTECH"]["buyer"], "POONAWALLA AVIATION PRIVATE LIMITED")
        self.assertNotIn("CARTRADE", pairs)      # one desk on both sides, different sizes
        self.assertNotIn("AAATECH", pairs)       # same client both sides


class DealsCli(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(held=("CARTRADE",), watched=(), clients=("POONAWALLA",))

    def tearDown(self):
        self.ws.close()

    def test_todays_deals_flag_holdings_and_notable_clients(self):
        code, out, err = run(["deals"] + self.ws.args() + ["--today", "2026-10-28"])
        self.assertEqual(code, 0, err)
        self.assertIn("NSE bulk and block deals, 2026-10-28: 14 deals", out)
        lines = [l for l in out.splitlines() if "CARTRADE" in l and "HRTI" in l]
        self.assertTrue(lines and all("[HELD]" in l for l in lines))
        self.assertTrue(any("POONAWALLA AVIATION PRIVATE LIMITED  [NOTABLE]" in l
                            for l in out.splitlines()))
        self.assertIn("SERUM INSTITUTE OF INDIA PRIVATE LIMITED sold 6,01,900 shares to "
                      "POONAWALLA AVIATION PRIVATE LIMITED at ₹887.00 (₹53.39 cr)", out)
        # held stocks first
        first = out.splitlines()[3]
        self.assertIn("CARTRADE", first)

    def test_history_json_with_min_value(self):
        code, out, _ = run(["deals"] + self.ws.args() + ["--today", "2026-10-28", "--days", "7",
                                                         "--min-value-cr", "250", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([(d["symbol"], d["side"]) for d in data["deals"]],
                         [("PCJEWELLER", "SELL"), ("PCJEWELLER", "BUY"), ("SWIGGY", "SELL"),
                          ("SWIGGY", "BUY"), ("TONBO", "SELL")])

    def test_mine_only(self):
        code, out, _ = run(["deals"] + self.ws.args() + ["--today", "2026-10-28", "--mine", "--json"])
        self.assertEqual({d["symbol"] for d in json.loads(out)["deals"]}, {"CARTRADE"})

    def test_bad_symbol(self):
        code, _, err = run(["deals", "--symbol", "RELIANCE INDUSTRIES", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("not an NSE symbol", err)

    def test_missing_files_are_a_note_not_a_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out, _ = run(["deals", "--offline", empty, "--portfolio", "x", "--watchlist", "x",
                                "--config", "x"])
        self.assertEqual(code, 0)
        self.assertIn("No deals match.", out)
        self.assertIn("NOTE: bulk deals unavailable", out)


class Pit(unittest.TestCase):
    def test_parse_xbrl_with_two_disclosures(self):
        with open(os.path.join(DIR, "IT_17571_WebXMLFile_20261029_155944284.xml"), "rb") as fh:
            head, txs = ins.parse_pit_xbrl(fh.read())
        self.assertEqual(head["Symbol"], "WESTLIFE")
        self.assertEqual(len(txs), 2)
        big = txs[1]
        self.assertEqual(big["person"], "Horizon Impex Private Limited")
        self.assertEqual(big["category"], "Promoter")
        self.assertEqual(big["type"], "Buy")
        self.assertEqual(big["qty"], 1065000)
        self.assertEqual(big["value"], 638994184)
        self.assertEqual(big["mode"], "Market Purchase")
        self.assertEqual(big["before_pct"], 0.3049)            # a fraction in the XBRL

    def test_window_rows(self):
        import datetime as dt
        rows, notes = ins.fetch_pit(ins.Fetcher(offline=DIR), dt.date(2026, 10, 26),
                                    dt.date(2026, 10, 29))
        self.assertEqual(notes, [])
        self.assertEqual(len(rows), 41)
        w = next(t for t in rows if t["symbol"] == "WESTLIFE" and t["qty"] == 1065000)
        self.assertEqual(w["value_cr"], 63.9)
        self.assertEqual(w["price"], 599.99)
        self.assertEqual((w["before_pct"], w["after_pct"]), (30.49, 31.18))
        self.assertEqual((w["trade_from"], w["trade_to"]), ("2026-10-27", "2026-10-28"))
        self.assertEqual(w["weekdays_to_disclose"], 1)
        self.assertTrue(w["promoter"] and w["market"])
        self.assertFalse(w["late"])
        late = next(t for t in rows if t["symbol"] == "KARURVYSYA" and t["mode"] == "Market Sale")
        self.assertEqual(late["weekdays_to_disclose"], 5)
        self.assertTrue(late["late"])

    def test_duplicates_are_removed(self):
        import datetime as dt
        rows, _ = ins.fetch_pit(ins.Fetcher(offline=DIR), dt.date(2026, 10, 26), dt.date(2026, 10, 29))
        # AJOONI's filing was posted twice; PARIN repeats a row inside a later filing.
        self.assertEqual(len([t for t in rows if t["symbol"] == "AJOONI"]), 1)
        self.assertEqual(len([t for t in rows if t["symbol"] == "PARIN"]), 2)

    def test_price_typed_as_value_is_flagged_and_left_out(self):
        import datetime as dt
        rows, _ = ins.fetch_pit(ins.Fetcher(offline=DIR), dt.date(2026, 10, 26), dt.date(2026, 10, 29))
        ace = [t for t in rows if t["symbol"] == "ACEVECTOR"]
        self.assertEqual(len(ace), 2)
        self.assertTrue(all(t["suspect_value"] for t in ace))
        net = ins.net_by_symbol(rows)
        self.assertNotIn("ACEVECTOR", net)
        self.assertNotIn("BAJFINANCE", net)            # debentures are not shares
        self.assertNotIn("PAISALO", net)               # a pledge is not a sale
        self.assertEqual(round(net["WESTLIFE"]["promoter"] / ins.CRORE, 2), 64.49)
        self.assertEqual(round(net["DMART"]["other"] / ins.CRORE, 2), -8.83)

    def test_older_list_and_new_list_together(self):
        code, out, err = run(["insider", "--symbol", "RELIANCE", "--from", "29-10-2025",
                              "--today", "2026-10-29", "--offline", DIR, "--json",
                              "--portfolio", "x", "--watchlist", "x", "--config", "x"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        rows = data["rows"]
        self.assertEqual(len(rows), 6)
        self.assertEqual({t["source"] for t in rows}, {"xbrl", "legacy"})
        rm = next(t for t in rows if t["person"] == "Radhika Merchant")
        self.assertEqual(rm["type"], "Pledge Revoke")
        self.assertFalse(rm["market"])
        old = next(t for t in rows if t["person"] == "BALANADU NARAYAN")
        self.assertEqual((old["qty"], old["value"], old["type"]), (2320, 3294168, "Sell"))
        self.assertEqual(old["filed"][:10], "2026-02-18")


class PitCli(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(held=("DMART",), watched=("WESTLIFE",))

    def tearDown(self):
        self.ws.close()

    def test_promoters_only(self):
        code, out, err = run(["insider"] + self.ws.args() + ["--today", "2026-10-29", "--days", "4",
                                                            "--promoters-only", "--json"])
        self.assertEqual(code, 0, err)
        rows = json.loads(out)["rows"]
        self.assertEqual(len(rows), 19)
        self.assertTrue(all("promoter" in t["category"].lower() for t in rows))
        self.assertNotIn("DMART", {t["symbol"] for t in rows})

    def test_text_flags_and_rules(self):
        code, out, _ = run(["insider"] + self.ws.args() + ["--today", "2026-10-29", "--days", "4"])
        self.assertEqual(code, 0)
        self.assertIn("41 transactions", out)
        self.assertIn("WESTLIFE    Horizon Impex Private Limited (Promoter): BUY 10,65,000 Equity, "
                      "₹63.90 cr at ~₹599.99/share, via Market Purchase, traded 2026-10-27 to "
                      "2026-10-28, disclosed 1 weekday later, holding 30.49% -> 31.18%  [WATCH]", out)
        self.assertIn("value ₹20.00 as filed (looks like a per-share price)", out)
        self.assertTrue(all("[HELD]" in l for l in out.splitlines() if l.strip().startswith("DMART ") and "):" in l))
        self.assertIn("SEBI PIT Regulations 2015", out)
        self.assertIn("within 2 trading days", out)

    def test_market_only_and_mine(self):
        code, out, _ = run(["insider"] + self.ws.args() + ["--today", "2026-10-29", "--days", "4",
                                                          "--market-only", "--mine", "--json"])
        rows = json.loads(out)["rows"]
        self.assertEqual({t["symbol"] for t in rows}, {"DMART", "WESTLIFE"})
        self.assertTrue(all(t["mode"] in ("Market Purchase", "Market Sale") for t in rows))


class Watch(unittest.TestCase):
    def test_alerts_once_per_item(self):
        ws = Workspace(held=("DMART",), watched=("STLTECH",), clients=("JUNOMONETA",))
        try:
            base = ["watch"] + ws.args() + ["--today", "2026-10-29", "--days", "4",
                                            "--seen", ws.path("insider-seen.json")]
            code, out, err = run(base)
            self.assertEqual(code, 0, err)
            alerts = [l for l in out.splitlines() if l.startswith("ALERT")]
            self.assertTrue(any("STLTECH" in l and "[WATCH]" in l for l in alerts))
            self.assertTrue(any("AARTIDRUGS" in l and "[NOTABLE]" in l for l in alerts))
            self.assertTrue(any("CARTRADE" in l for l in alerts))           # over ₹25 cr
            self.assertFalse(any("EMPOWER" in l for l in alerts))           # small, not ours
            self.assertTrue(any("insider" in l and "WESTLIFE" in l for l in alerts))   # promoter buy
            self.assertTrue(any("insider" in l and "DMART" in l and "[HELD]" in l for l in alerts))
            self.assertFalse(any("ACEVECTOR" in l and "PLEDGE" in l for l in alerts))
            with open(ws.path("insider-seen.json"), encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)["seen"]), len(alerts))
            code, out, _ = run(base)
            self.assertIn("Nothing new since the last check.", out)
            code, out, _ = run(base + ["--all", "--dry-run"])
            self.assertTrue(any("EMPOWER" in l for l in out.splitlines()))
            code, out, _ = run(base + ["--all", "--dry-run"])
            self.assertTrue(any("EMPOWER" in l for l in out.splitlines()))   # dry run saves nothing
        finally:
            ws.close()


class Stocks(unittest.TestCase):
    def test_portfolio_and_watchlist_shapes(self):
        ws = Workspace(held=("M&M", "TCS"), watched=("TCS", "INFY"))
        try:
            m = ins.my_stocks(ws.path("portfolio.json"), ws.path("watchlist.json"))
            self.assertEqual(m["held"], {"M&M", "TCS"})
            self.assertEqual(m["watched"], {"INFY"})
        finally:
            ws.close()
        self.assertEqual(ins.my_stocks("nope.json", "nope.json"), {"held": set(), "watched": set()})

    def test_default_watch_clients(self):
        self.assertIn("ASHISH KACHOLIA", ins.watch_clients("nope.json"))
        self.assertEqual(ins.notable("Ashish  Kacholia", ["ASHISH KACHOLIA"]), "ASHISH KACHOLIA")


if __name__ == "__main__":
    unittest.main()
