"""trader-analyst tests, offline.

Fixtures (tests/fixtures/trader-analyst/), real NSE responses fetched 2026-10-29:
  results-<SYM>.json         integrated filing results list, cut to the quarters below
  results-legacy-<SYM>.json  the older results list (RELIANCE: the Dec-2024 quarter)
  board-<SYM>.json           board meetings (RELIANCE meets 30-Oct-2026 for results)
  *.xml                      each quarter's results XBRL, trimmed to its dimension-free
                             periods: TCS consolidated Q2 FY26..Q2 FY27 and standalone
                             Q2 FY27; RELIANCE consolidated Q1 FY26..Q1 FY27 and Q3 FY25
                             (old template); HDFCBANK consolidated Q2 FY27 (bank template)
"""

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")
TODAY = dt.date(2026, 10, 29)


def read(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


def analysed(symbol, **kw):
    return facts.company(symbol, facts.Fetcher(offline=DIR), today=TODAY, **kw)


class Formatting(unittest.TestCase):
    def test_inr_uses_indian_grouping(self):
        self.assertEqual(facts.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(facts.inr(1234567.891), "₹12,34,567.89")
        self.assertEqual(facts.inr(-54321.1), "-₹54,321.10")
        self.assertEqual(facts.inr(5), "₹5.00")

    def test_crore(self):
        self.assertEqual(facts.crore(731880000000), "₹73,188 cr")
        self.assertEqual(facts.crore(3118500000000), "₹3,11,850 cr")
        self.assertEqual(facts.crore(-8370600000), "-₹837 cr")
        self.assertEqual(facts.crore(123456789), "₹12.35 cr")
        self.assertEqual(facts.crore(None), "-")

    def test_indian_financial_year_quarters(self):
        self.assertEqual(facts.fiscal("2026-09-30"), "Q2 FY27")
        self.assertEqual(facts.fiscal("2026-06-30"), "Q1 FY27")
        self.assertEqual(facts.fiscal("2026-03-31"), "Q4 FY26")
        self.assertEqual(facts.fiscal("2025-12-31"), "Q3 FY26")
        self.assertEqual(facts.fiscal("2099-12-31"), "Q3 FY00")
        self.assertEqual(facts.months_label("2026-03-31"), "Jan-Mar 2026")

    def test_nse_dates(self):
        self.assertEqual(facts.nse_date("30-SEP-2026").date(), dt.date(2026, 9, 30))
        self.assertEqual(facts.nse_date("16-Jan-2025 20:20").hour, 20)
        self.assertIsNone(facts.nse_date("soon"))


class Xbrl(unittest.TestCase):
    def test_tcs_quarter_not_year_to_date(self):
        p = facts.parse_xbrl(read("INTEGRATED_FILING_INDAS_1734607_08102026100810_WEB.xml"))
        self.assertEqual((p["start"], p["end"]), ("2026-07-01", "2026-09-30"))
        self.assertTrue(p["quarterly"])
        f = p["facts"]
        self.assertEqual(f["RevenueFromOperations"], 731880000000)     # not the H1 1,45,463 cr
        self.assertEqual(f["ProfitLossForPeriod"], 139340000000)
        self.assertEqual(f["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"], 38.37)
        self.assertEqual(p["meta"]["NatureOfReportStandaloneConsolidated"], "Consolidated")

    def test_old_template_with_mislabelled_year_to_date_context(self):
        # The Dec-2024 filing labels its 9-month context with quarter dates.
        p = facts.parse_xbrl(read("INDAS_117297_1348248_16012025081520.xml"))
        self.assertEqual(p["facts"]["RevenueFromOperations"], 2438650000000)   # not 7,15,563 cr
        row = facts.quarter_row(p)
        self.assertEqual(row["fiscal"], "Q3 FY25")
        self.assertEqual(row["net_profit"], 185400000000)

    def test_ebitda_approximation(self):
        row = facts.quarter_row(facts.parse_xbrl(
            read("INTEGRATED_FILING_INDAS_1734607_08102026100810_WEB.xml")))
        # PBT 18,638 + finance 252 + D&A 1,262 - other income 1,337
        self.assertEqual(row["ebitda"], 188150000000)
        self.assertEqual(row["ebitda_margin"], 25.7)
        self.assertEqual(row["net_margin"], 19.0)
        self.assertEqual(row["template"], "ind-as")

    def test_bank_template(self):
        row = facts.quarter_row(facts.parse_xbrl(
            read("INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml")))
        self.assertEqual(row["template"], "bank")
        self.assertEqual(row["revenue"], 1293586900000)            # total income
        self.assertEqual(row["nii"], 951316300000 - 506674300000)
        self.assertEqual(row["ppop"], 328715400000)
        self.assertEqual(row["net_profit"], 207000300000)
        self.assertEqual(row["eps_diluted"], 13.41)
        self.assertIsNone(row["ebitda"])

    def test_garbage_is_a_clear_error(self):
        with self.assertRaises(facts.FactsError):
            facts.parse_xbrl(b"<html>blocked</html")


class FilingsList(unittest.TestCase):
    def test_merges_integrated_and_old_lists(self):
        integ = json.loads(read("results-RELIANCE.json"))
        legacy = json.loads(read("results-legacy-RELIANCE.json"))
        items = facts.filings(integ, legacy)
        cons = [i["end"] for i in items if i["nature"] == "consolidated"]
        self.assertEqual(cons, ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
                                "2025-06-30", "2024-12-31"])
        self.assertEqual(items[0]["source"], "integrated")
        self.assertEqual(items[-1]["source"], "legacy")
        self.assertTrue(all(not i["bank"] for i in items))

    def test_bank_flag(self):
        items = facts.filings(json.loads(read("results-HDFCBANK.json")), [])
        self.assertTrue(items and all(i["bank"] for i in items))

    def test_later_revision_wins(self):
        base = {"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "audited": "Audited",
                "type_Sub": "Original", "broadcast_Date": "08-Oct-2026 22:07:33",
                "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/a.xml"}
        rev = dict(base, type_Sub="Revision", broadcast_Date="10-Oct-2026 10:00:00",
                   xbrl="https://nsearchives.nseindia.com/corporate/xbrl/b.xml")
        items = facts.filings({"data": [base, rev]}, [])
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["xbrl"].endswith("b.xml"))


class Tcs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = analysed("TCS", quarters=5)

    def test_latest_quarter_and_yoy(self):
        q = self.r["rows"][0]
        self.assertEqual(q["fiscal"], "Q2 FY27")
        self.assertEqual(q["revenue"], 731880000000)
        self.assertEqual(q["net_profit"], 138840000000)       # owners of the parent
        self.assertEqual(q["pat"], 139340000000)
        self.assertEqual(q["eps_diluted"], 38.37)
        self.assertEqual(q["revenue_yoy"], 11.2)              # vs 65,799 cr in Q2 FY26
        self.assertEqual(q["revenue_qoq"], 1.3)               # vs 72,275 cr in Q1 FY27
        self.assertEqual(q["net_profit_yoy"], 15.0)           # vs 12,075 cr
        self.assertEqual(q["eps_diluted_yoy"], 15.0)          # vs 33.37
        self.assertEqual(q["yoy_end"], "2025-09-30")

    def test_quarter_labels(self):
        self.assertEqual([r["fiscal"] for r in self.r["rows"]],
                         ["Q2 FY27", "Q1 FY27", "Q4 FY26", "Q3 FY26", "Q2 FY26"])

    def test_q4_is_the_jan_to_mar_quarter_not_the_year(self):
        q4 = next(r for r in self.r["rows"] if r["fiscal"] == "Q4 FY26")
        self.assertEqual(q4["start"], "2026-01-01")
        self.assertEqual(q4["revenue"], 706980000000)

    def test_trailing_twelve_months(self):
        t = self.r["ttm"]
        self.assertEqual((t["from"], t["to"]), ("2025-10-01", "2026-09-30"))
        self.assertEqual(round(t["revenue"] / facts.CRORE), 283248)
        self.assertEqual(t["eps_diluted"], 142.64)

    def test_notes_and_calendar(self):
        self.assertTrue(any("year-ago quarter (Q2 FY26) had exceptional items" in n
                            for n in self.r["notes"]))
        self.assertIsNone(self.r["calendar"]["next"])
        self.assertEqual(self.r["calendar"]["last"], "2026-10-08")

    def test_standalone_flag(self):
        r = analysed("TCS", quarters=1, standalone=True)
        self.assertEqual(r["nature"], "standalone")
        self.assertEqual(r["rows"][0]["revenue"], 604410000000)
        self.assertEqual(r["rows"][0]["net_profit"], r["rows"][0]["pat"])
        self.assertTrue(any("Could not read" in n for n in r["notes"]))   # rest not in fixtures


class Reliance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = analysed("RELIANCE", quarters=6)

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["fiscal"], "Q1 FY27")
        self.assertEqual(q["revenue"], 3118500000000)
        self.assertEqual(q["net_profit"], 209460000000)
        self.assertEqual(q["eps_diluted"], 15.48)
        self.assertEqual(q["revenue_yoy"], 25.4)
        self.assertEqual(q["net_profit_yoy"], -22.4)

    def test_yoy_across_the_switch_to_integrated_filing(self):
        q3 = next(r for r in self.r["rows"] if r["fiscal"] == "Q3 FY26")
        self.assertEqual(q3["revenue_yoy"], 10.5)    # 2,69,496 vs 2,43,865 (old list)
        self.assertEqual(q3["net_profit_yoy"], 0.6)

    def test_high_base_is_flagged(self):
        self.assertTrue(any("Other income was 41% of profit before tax in the year-ago" in n
                            for n in self.r["notes"]))

    def test_reports_this_week(self):
        n = self.r["calendar"]["next"]
        self.assertEqual(n["date"], "2026-10-30")
        self.assertEqual(n["days"], 1)
        self.assertTrue(n["this_week"])

    def test_render(self):
        text = facts.render("RELIANCE", self.r)
        self.assertIn("₹3,11,850 cr (+25.4% YoY, +4.4% QoQ)", text)
        self.assertIn("3,11,850", text)
        self.assertIn("Next results: board meeting 2026-10-30 (tomorrow, this week)", text)
        self.assertIn("EBITDA is an approximation", text)
        self.assertNotIn("$", text)


class Bank(unittest.TestCase):
    def test_hdfc_bank_renders_without_ebitda(self):
        r = analysed("HDFCBANK", quarters=1)
        text = facts.render("HDFCBANK", r)
        self.assertIn("total income ₹1,29,359 cr", text)
        self.assertIn("Net interest income ₹44,464 cr", text)
        self.assertNotIn("EBITDA is an approximation", text)
        self.assertTrue(any("Bank results template" in n for n in r["notes"]))
        self.assertTrue(any("calendar unavailable" in n for n in r["notes"]))


class Calendar(unittest.TestCase):
    def test_next_results_this_week_or_later(self):
        meetings = [
            {"bm_date": "03-Nov-2026", "bm_purpose": "Financial Results", "bm_desc": "Q2"},
            {"bm_date": "05-Sep-2026", "bm_purpose": "Bonus", "bm_desc": "bonus issue"},
            {"bm_date": "14-Oct-2024", "bm_purpose": "RESULTS text in the purpose field",
             "bm_desc": "Board Meeting Intimation"},
        ]
        n = facts.next_results(meetings, TODAY)
        self.assertEqual(n["next"]["date"], "2026-11-03")
        self.assertFalse(n["next"]["this_week"])          # next Tuesday
        self.assertEqual(n["last"], "2024-10-14")
        self.assertIsNone(facts.next_results([meetings[1]], TODAY))


class Cli(unittest.TestCase):
    def test_offline_run_with_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["TCS", "RELIANCE", "--offline", DIR, "--today", "2026-10-29",
                                  "--metrics", path])
            self.assertEqual(code, 0, err)
            self.assertIn("Q2 FY27", out)
            with open(path, encoding="utf-8") as fh:
                m = json.load(fh)
        rows = {r["symbol"]: r for r in m["companies"]}
        self.assertEqual(rows["TCS"]["revenue_cr"], 73188.0)
        self.assertEqual(rows["TCS"]["revenue_yoy"], 11.2)
        self.assertEqual(rows["RELIANCE"]["next_results"], "2026-10-30")
        self.assertEqual(m["unit"], "INR crore")

    def test_json(self):
        code, out, _ = run(["TCS", "--offline", DIR, "--today", "2026-10-29", "--json",
                            "--quarters", "2"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data[0]["rows"]), 2)
        self.assertNotIn("all_rows", data[0])

    def test_bad_and_unknown_symbols(self):
        code, _, err = run(["not a symbol", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("not an NSE symbol", err)
        code, _, err = run(["ZZZZ", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("ZZZZ", err)

    def test_yahoo_style_suffix_is_accepted(self):
        code, out, _ = run(["tcs.ns", "--offline", DIR, "--today", "2026-10-29", "--quarters", "1"])
        self.assertEqual(code, 0)
        self.assertIn("TCS - Tata Consultancy Services Limited", out)

    def test_no_arguments_prints_help(self):
        code, out, _ = run([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
