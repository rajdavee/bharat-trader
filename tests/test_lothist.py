"""The dated F&O lot-size history (trader-archive's lothist dataset) and its use by trader-lab's option engines,
offline, on real NSE files trimmed to a few rows.

Fixtures (tests/fixtures/trader-archive/lothist/, fetched 2026-11-02):
  fo_mktlots_wayback_20080723.csv   the Wayback Machine's copy of NSE's fo_mktlots.csv of 23 Jul 2008 (the
                                    old one-column layout), cut to NIFTY, BANKNIFTY, MINIFTY and 3 stocks
  fo_mktlots_wayback_20141111.csv   its copy of 11 Nov 2014 (one lot per expiry month), cut to 2 indices and
                                    4 stocks: NIFTY's Nov-14 to Jan-15 at 25, its long-dated Mar-15 on at 50
  wayback_cdx_fo_mktlots.json       the Wayback Machine's CDX answer for that page, cut to those two copies
  fo01042015.zip                    NSE's F&O market activity report of 1 Apr 2015 (archives/fo/mkt/), its
                                    futures and options files cut to NIFTY, HDFCBANK and TECHM
  FAOP70616.lines.txt, FAOP30449.lines.txt, FAOP47854.lines.txt
                                    the text (history.pdftext lines) of NSE's index lot circulars of
                                    3 Oct 2025, 7 Aug 2015 and 31 Mar 2021, cut to the tables and rules
  tests/fixtures/trader-lab/legacy/ (reused) legacy F&O bhavcopies of 31 Mar to 7 Apr 2015, NIFTY and HDFCBANK
"""

import contextlib
import datetime
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

arc = load("trader-archive", "archive")
L = arc.history().lots
DIR = os.path.join(FIX, "trader-archive", "lothist")
LEGACY = os.path.join(FIX, "trader-lab", "legacy")
NOW = datetime.datetime(2026, 11, 2, 20, 0)


def text(name):
    with open(os.path.join(DIR, name), encoding="latin-1") as fh:
        return fh.read()


def mkt_files():
    z = zipfile.ZipFile(os.path.join(DIR, "fo01042015.zip"))
    return dict((n, z.read(n).decode("latin-1")) for n in z.namelist())


class Resp(object):
    def __init__(self, data):
        self.data, self.status, self.headers = data, 200, {}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet(object):
    """The Wayback Machine (its CDX index and two copies) and NSE's market activity report of 1 Apr 2015;
    anything else is a 404."""

    def __init__(self):
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        name = None
        if "/cdx/search/cdx" in url:
            name = "wayback_cdx_fo_mktlots.json" if "url=nseindia.com/content" in url else None
        elif "20080723130702id_" in url:
            name = "fo_mktlots_wayback_20080723.csv"
        elif "20141111015346id_" in url:
            name = "fo_mktlots_wayback_20141111.csv"
        elif url.endswith("/archives/fo/mkt/fo01042015.zip"):
            name = "fo01042015.zip"
        if name is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        with open(os.path.join(DIR, name), "rb") as fh:
            return Resp(fh.read())


def http(opener):
    return arc.Http(pause=1.0, sleep=lambda s: None, opener=opener, clock=lambda: 0.0)


def legacy_copy(t):
    tmp = tempfile.mkdtemp(prefix="bt-lots-")
    t.addCleanup(shutil.rmtree, tmp, True)
    root = os.path.join(tmp, "archive")
    shutil.copytree(LEGACY, root)
    return root


def backfill_lots(root, start="2015-03-25", end="2015-04-10"):
    op = FakeNet()
    with contextlib.redirect_stdout(io.StringIO()):
        s = arc.backfill(arc.Store(root), http(op), start, end, ["lothist"], NOW, log=lambda m: None)
    return s, op


class Parsers(unittest.TestCase):
    def test_fo_mktlots_by_month(self):
        rows = L.parse_mktlots_any(text("fo_mktlots_wayback_20141111.csv"))
        got = dict(((r[1], r[2]), r[3]) for r in rows)
        self.assertEqual(got[("NIFTY", "2014-11")], 25)
        self.assertEqual(got[("NIFTY", "2015-01")], 25)
        self.assertEqual(got[("NIFTY", "2015-03")], 50)      # the long-dated options kept the old lot in 2014
        self.assertEqual(got[("HDFCBANK", "2014-12")], 250)
        self.assertEqual(got[("SBIN", "2015-01")], 125)
        self.assertNotIn(("BANKNIFTY", "2015-03"), got)      # blank cells are no lot

    def test_fo_mktlots_old_one_column(self):
        rows = L.parse_mktlots_any(text("fo_mktlots_wayback_20080723.csv"))
        self.assertIn(["S&P CNX Nifty", "NIFTY", "", 50], rows)   # "" = every live contract
        self.assertIn(["BANK Nifty", "BANKNIFTY", "", 25], rows)
        self.assertIn(["State Bank Of India", "SBIN", "", 132], rows)

    def test_the_archive_parser_still_reads_todays_file(self):
        with open(os.path.join(FIX, "trader-archive", "fo_mktlots.csv"), encoding="utf-8") as fh:
            today = fh.read()
        rows, _ = arc.parse_lots(today)
        new = L.parse_mktlots_any(today)
        self.assertEqual(sorted((r[1], r[2], int(r[3])) for r in rows), sorted((r[1], r[2], r[3]) for r in new))

    def test_market_activity_report(self):
        got = dict(((u, e), lot) for u, e, lot, n in L.parse_mkt_activity(mkt_files()))
        self.assertEqual(got[("NIFTY", "2015-04-30")], 25)      # 83075 traded / 3323 contracts on the 7000 CE
        self.assertEqual(got[("NIFTY", "2015-12-31")], 50)      # a long-dated option: still 50 on 1 Apr 2015
        self.assertEqual(got[("HDFCBANK", "2015-04-30")], 250)
        self.assertEqual(got[("TECHM", "2015-05-28")], 500)
        self.assertNotIn(("fohelp.txt", ""), got)

    def test_circular_rows(self):
        rows = L.parse_circular_rows(text("FAOP70616.lines.txt").splitlines())
        self.assertIn(("NIFTY", 75, 65), rows)
        self.assertIn(("BANKNIFTY", 35, 30), rows)
        self.assertIn(("NIFTYNXT50", 25, None), rows)           # the "unchanged" table
        self.assertIn(("NIFTY", 25, 75), L.parse_circular_rows(text("FAOP30449.lines.txt").splitlines()))
        self.assertIn(("NIFTY", 75, 50), L.parse_circular_rows(text("FAOP47854.lines.txt").splitlines()))

    def test_the_circular_table_matches_the_circulars(self):
        table = dict(((c[0], c[1]), (c[3], c[4])) for c in L.INDEX_CIRCULARS)
        for name, cno in (("FAOP70616.lines.txt", "NSE/FAOP/70616"), ("FAOP30449.lines.txt", "NSE/FAOP/30449"),
                          ("FAOP47854.lines.txt", "NSE/FAOP/47854")):
            for sym, present, new in L.parse_circular_rows(text(name).splitlines()):
                if (sym, cno) in table:
                    self.assertEqual(table[(sym, cno)], (present, new if new is not None else present), (sym, cno))
        self.assertIn(("NIFTY", "NSE/FAOP/47854"), table)
        self.assertIn(("BANKNIFTY", "NSE/FAOP/30449"), table)


class Rules(unittest.TestCase):
    def test_round_contract_value_over_price(self):
        # the NIFTY April 2015 future on 1 Apr 2015: Rs 8,934.5 crore over 416,855 contracts at 8,512..8,645.55
        v, c = 89345285000.0, 416855
        self.assertEqual(round(v / c / ((8512 + 8645.55) / 2)), 25)
        # the bracket the rebuild uses holds 25 and only 25 among the divisors of the open interest
        lo, hi = v / c / 8645.55, v / c / 8512
        self.assertTrue(lo < 25 < hi)
        self.assertEqual(L.candidates(lo, hi, 18973525 // 25 * 25), [25])

    def test_an_option_bracket_uses_strike_plus_premium(self):
        rows = [["NIFTY", "IDO", "2015-04-30", "8600", "CE", "93.3", "142.5", "85.3", "136.2", "136.2", "", "2411075",
                 "-238475", "488316", "106326962000", ""]]       # a real row of 1 Apr 2015
        info = L.day_contracts(rows)[("NIFTY", "2015-04-30")]
        self.assertEqual(info["cands"], [25])
        self.assertIsNone(info["lot"])

    def test_udiff_rows_are_exact(self):
        rows = [["NIFTY", "IDO", "2026-11-24", "26000", "CE", "1", "1", "1", "1", "1", "26000", "65", "0", "1", "1", "65"]]
        self.assertEqual(L.day_contracts(rows)[("NIFTY", "2026-11-24")]["lot"], 65)

    def test_rebuild_on_the_legacy_fixture(self):
        sc = L.Scanner()
        base = os.path.join(LEGACY, "fo", "2015")
        for f in sorted(os.listdir(base)):
            sc.feed(f[:10], arc._fo_rows(os.path.join(base, f)))
        got = dict(((r[1], r[2]), L.leader(L.parse_votes(r[6]), int(r[8]))[0]) for r in sc.rows())
        self.assertEqual(got[("NIFTY", "2015-04-30")], 25)
        self.assertEqual(got[("NIFTY", "2015-05-28")], 25)
        self.assertEqual(got[("HDFCBANK", "2015-04-30")], 250)

    def test_a_mid_life_lot_change_cuts_the_segment(self):
        # the HDFCBANK April 2015 rows of the fixture, then the same rows as after a 1:1 bonus (lot and OI doubled,
        # prices halved): a new segment starts on the first adjusted day
        base = os.path.join(LEGACY, "fo", "2015")
        days = sorted(f[:10] for f in os.listdir(base))
        real = [[r for r in arc._fo_rows(os.path.join(base, d + ".csv.gz")) if r[0] == "HDFCBANK"] for d in days]

        def bonus(r):
            r = list(r)
            for i in (3, 5, 6, 7, 8, 9):
                if r[i]:
                    r[i] = str(float(r[i]) / 2)
            for i in (11, 12):
                if r[i]:
                    r[i] = str(int(float(r[i])) * 2)
            return r
        sc = L.Scanner()
        for d, rows in zip(days, real):
            sc.feed(d, rows)
        later = ["2015-04-08", "2015-04-09", "2015-04-10", "2015-04-13"]
        for d, rows in zip(later, real):
            sc.feed(d, [bonus(r) for r in rows])
        segs = [r for r in sc.rows() if r[2] == "2015-04-30"]
        self.assertEqual(len(segs), 2)
        self.assertEqual((segs[0][4], segs[0][5]), ("2015-03-31", "2015-04-07"))
        self.assertEqual(segs[1][4], "2015-04-08")
        self.assertEqual(L.leader(L.parse_votes(segs[1][6]), int(segs[1][8]))[0], 500)

    def test_years_scanned_alone_merge_back(self):
        base = os.path.join(LEGACY, "fo", "2015")
        days = sorted(f[:10] for f in os.listdir(base))
        a, b = L.Scanner(), L.Scanner()
        for i, d in enumerate(days):
            (a if i < 2 else b).feed(d, arc._fo_rows(os.path.join(base, d + ".csv.gz")))
        merged = L.merge_scan([[str(c) for c in r] for r in a.rows() + b.rows()])
        nifty = [r for r in merged if r[1] == "NIFTY" and r[2] == "2015-04-30"]
        self.assertEqual(len(nifty), 1)
        self.assertEqual((nifty[0][4], nifty[0][5], int(nifty[0][8])), ("2015-03-31", "2015-04-07", 4))

    def test_circular_rules(self):
        self.assertEqual(L.circular_lots("NIFTY", "2021-06-24", "2021-04-15")[0], {75})   # before July 2021
        self.assertEqual(L.circular_lots("NIFTY", "2021-07-29", "2021-07-01")[0], {50})
        self.assertEqual(L.circular_lots("NIFTY", "2021-07-29", "2021-04-30")[0], {75, 50})   # long-dated or new
        self.assertEqual(L.circular_lots("NIFTY", "2021-07-22", "2021-07-01", monthly=False)[0], {75})
        self.assertEqual(L.circular_lots("BANKNIFTY", "2023-07-27", "2023-07-03")[0], {15})
        self.assertEqual(L.circular_lots("BANKNIFTY", "2023-06-29", "2023-05-02")[0], {25})
        self.assertEqual(L.circular_lots("NIFTY", "2012-06-28", "2012-05-02"), (None, None))   # no circular then

    def test_pick_and_coverage(self):
        rows = [("X", "2015-04-30", "M", "2015-03-31", "2015-04-30", 25, "s", "exact"),
                ("X", "2015-05-28", "M", "2015-03-31", "2015-05-28", 25, "s", "reconstructed"),
                ("X", "2015-12-31", "W", "2015-03-31", "2015-12-31", 50, "s", "exact")]
        self.assertEqual(L.pick(rows, "2015-04-06")[1], "2015-04-30")          # the nearest monthly
        self.assertEqual(L.pick(rows, "2015-05-04")[1], "2015-05-28")
        self.assertEqual(L.pick(rows, "2015-04-06", "2015-12-31")[5], 50)
        self.assertIsNone(L.pick(rows, "2015-04-06", "2015-06-25"))
        c = L.coverage(rows, "2015-04-01", "2015-04-30", monthly_only=True)["all"]
        self.assertEqual((c["exact"], c["reconstructed"], c["total"]), (30, 30, 60))


class Backfill(unittest.TestCase):
    def test_backfill_builds_the_table(self):
        root = legacy_copy(self)
        s, op = backfill_lots(root)
        self.assertGreater(s["rows"]["lothist"], 0)
        self.assertTrue(any("fo01042015.zip" in u for u in op.urls))
        self.assertTrue(any("web.archive.org/cdx" in u for u in op.urls))
        st = arc.Store(root)
        self.assertEqual(st.dates(arc.LOT_MKT), ["2015-04-01"])
        self.assertEqual(st.missing(arc.LOT_MKT, "2015-03-31")["status"], 404)     # remembered, not asked again
        self.assertEqual(st.dates(arc.LOT_WAYBACK), [])      # both copies are outside the window
        r = arc.lot_size("NIFTY", "2015-04-06", "2015-04-30", root)
        self.assertEqual((r["lot"], r["confidence"]), (25, "exact"))
        self.assertIn("market activity report 2015-04-01", r["source"])
        self.assertEqual(arc.lot_size("HDFCBANK", "2015-04-01", "2015-04-30", root)["lot"], 250)
        self.assertEqual(arc.lot_size("NIFTY", "2015-04-01", None, root)["expiry"], "2015-04-30")  # nearest monthly
        with open(os.path.join(root, "lothist", "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(man["inputs"]["mkt_reports"], 1)
        self.assertIn("NIFTY", man["coverage"])
        # a second run asks NSE nothing again (the 404 is remembered) and rebuilds from the cache
        s2, op2 = backfill_lots(root)
        self.assertFalse(any(u.startswith("https://nsearchives") for u in op2.urls))

    def test_wayback_copies_in_the_window(self):
        root = legacy_copy(self)
        backfill_lots(root, "2008-01-01", "2015-04-10")
        st = arc.Store(root)
        self.assertEqual(st.dates(arc.LOT_WAYBACK), ["2008-07-23", "2014-11-11"])
        rows = arc.read_gz_csv(st.path(arc.LOT_WAYBACK, "2014-11-11"))[1]
        self.assertIn(["CNX NIFTY", "NIFTY", "2015-03", "50"], rows)

    def test_reconstructed_without_an_exact_source(self):
        root = legacy_copy(self)
        man = arc.build_lothist(root, now=NOW)
        self.assertEqual(man["segments"]["exact"], 0)
        r = arc.lot_size("NIFTY", "2015-04-06", "2015-04-30", root)
        self.assertEqual((r["lot"], r["confidence"]), (25, "reconstructed"))
        c = arc.lot_coverage("NIFTY", "2015-03-31", "2015-04-07", root)
        self.assertEqual((c["exact_pct"], c["reconstructed_pct"]), (0.0, 100.0))
        self.assertEqual(c["by_year"]["2015"]["reconstructed"], 100.0)
        st = arc.lot_coverage("STOCKS", "2015-03-31", "2015-04-07", root)
        self.assertEqual(st["reconstructed_pct"], 100.0)

    def test_fill_lots_and_the_chain(self):
        root = legacy_copy(self)
        arc.build_lothist(root, now=NOW)
        ch = arc.load_fo_chain_eod("NIFTY", "2015-04-01", root)            # exact only: refused
        self.assertTrue(all(o["lot"] is None for o in ch["options"]))
        self.assertTrue(all(o.get("lot_refused") == "reconstructed" for o in ch["options"]))
        self.assertEqual(ch["lots"]["refused"], len(ch["options"]) + len(ch["futures"]))
        ch = arc.load_fo_chain_eod("NIFTY", "2015-04-01", root, lots="any")
        self.assertEqual(set(o["lot"] for o in ch["options"]), {25})
        self.assertEqual(set(o["lot_confidence"] for o in ch["options"]), {"reconstructed"})
        ch = arc.load_fo_chain_eod("NIFTY", "2015-04-01", root, lots="none")
        self.assertNotIn("lots", ch)
        self.assertIsNone(arc.lot_size("NIFTY", "2015-04-01", None, LEGACY))   # no history: None, not a guess

    def test_cli(self):
        root = legacy_copy(self)
        backfill_lots(root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(arc.main(["--root", root, "lots", "--underlying", "NIFTY", "--from", "2015-03-31",
                                       "--to", "2015-04-07"]), 0)
            self.assertEqual(arc.main(["--root", root, "lots", "--underlying", "HDFCBANK", "--date", "2015-04-06",
                                       "--expiry", "2015-04-30"]), 0)
            arc.main(["--root", root, "status"])
        t = out.getvalue()
        self.assertIn("100.0% of contract-days exact", t)
        self.assertIn("from the 2015-04-30 expiry: 25 (exact", t)
        self.assertIn("HDFCBANK 2015-04-30 on 2015-04-06: lot 250 (exact", t)
        self.assertIn("lot-size history:", t)


lab = load("trader-lab", "lab")
O = lab.options_api()
EN = O.engines_api()
ENG = lab.engines_api()
X, J, M = ENG.executor, ENG.jobs, ENG.market


def _hyp(prefix):
    for f in sorted(os.listdir(os.path.join(ROOT, "trader-lab", "library", "edge-hunt"))):
        if f.startswith(prefix):
            with open(os.path.join(ROOT, "trader-lab", "library", "edge-hunt", f), encoding="utf-8") as fh:
                return json.load(fh)
    raise KeyError(prefix)


class Lab(unittest.TestCase):
    P = {"short_delta": 0.1, "wing_width_pct_of_spot": 1.5, "vrp_threshold_vol_points": 0, "exit": "expiry"}

    def run_vrp(self, root, allow):
        d = {"root": root, "archive": arc, "regime": lab.regime_api(), "allow_reconstructed_lots": allow}
        return EN.run(_hyp("eh-05"), self.P, "2015-04-06", "2015-04-07", d)

    def test_eh05_refuses_a_reconstructed_lot_and_takes_it_when_allowed(self):
        root = legacy_copy(self)
        arc.build_lothist(root, now=NOW)
        r = self.run_vrp(root, False)
        self.assertEqual(r["extra"]["baseline"]["skipped"], {"no legs: " + O.LOT_REFUSED: 1})
        self.assertTrue(any("refused (allow_reconstructed_lots is off)" in n for n in r["notes"]))
        r = self.run_vrp(root, True)
        self.assertEqual(r["extra"]["baseline"]["n"], 1)
        self.assertTrue(any("reconstructed and used" in n for n in r["notes"]))

    def test_eh05_trades_on_an_exact_lot(self):
        root = legacy_copy(self)
        backfill_lots(root)                         # the market activity report of 1 Apr 2015: exact
        r = self.run_vrp(root, False)
        self.assertEqual(r["extra"]["baseline"]["n"], 1)
        self.assertFalse(any("lot sizes from the dated lot history" in n and "refused" in n and "0 reconstructed" not in n
                             for n in r["notes"]))

    def hunt(self, root, cfg):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lib = os.path.join(tmp, "edge-hunt")
        os.makedirs(lib)
        rows = []
        for pre, hid in (("eh-05", "eh-95-legacy-vrp"), ("eh-08", "eh-96-legacy-crush")):
            doc = _hyp(pre)
            doc["id"] = hid
            doc["span"] = {"train": {"from": "2015-03-31", "to": "2015-04-01"},
                           "validation": {"from": "2015-04-06", "to": "2015-04-07"},
                           "holdout": {"from": "2015-04-21", "to": "2015-04-30"}, "embargo_days": 14}
            with open(os.path.join(lib, hid + ".json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            rows.append({"id": hid, "sha256": X.registered_hash(doc)})
        with open(os.path.join(lib, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"hypotheses": rows}, fh)
        now = datetime.datetime(2026, 11, 2, 12, 0)
        return [X.execute(os.path.join(lib, h + ".json"), M.ArchiveMarket(root, arc), os.path.join(tmp, "out"), now,
                          dry_run=True, cfg=cfg) for h in ("eh-95-legacy-vrp", "eh-96-legacy-crush")]

    def test_the_hunt_blocks_on_reconstructed_lots_and_says_how_much(self):
        root = legacy_copy(self)
        arc.build_lothist(root, now=NOW)
        for r in self.hunt(root, {}):
            self.assertEqual(r["state"], "blocked")
            b = r["plan"]["blockers"][0]
            self.assertIn("0.0% of the train window's contract-days", b)
            self.assertIn("100.0% reconstructed", b)
            self.assertIn("allow_reconstructed_lots", b)
            lc = r["plan"]["lot_coverage"]
            self.assertEqual(lc["windows"]["train"]["usable_pct"], 0.0)
            self.assertFalse(lc["allow_reconstructed_lots"])
        for r in self.hunt(root, {"allow_reconstructed_lots": True}):
            self.assertNotEqual(r["state"], "blocked", r["plan"].get("blockers"))
            self.assertEqual(r["plan"]["lot_coverage"]["windows"]["validation"]["usable_pct"], 100.0)

    def test_the_hunt_runs_on_exact_lots(self):
        root = legacy_copy(self)
        backfill_lots(root)
        r = self.hunt(root, {})[0]
        self.assertEqual(r["state"], "dry-run", r["plan"].get("blockers"))
        self.assertEqual(r["plan"]["lot_coverage"]["windows"]["train"]["exact_pct"], 100.0)
        words = lab._hunt_words(r)
        self.assertIn("lot sizes, train 2015-03-31 to 2015-04-01: 100.0% of contract-days exact", words)

    def test_engine_revisions_moved(self):
        for fam in ("vrp-selling", "event-vol", "option-buying"):
            self.assertEqual(J.engine_rev(fam), 3)
            self.assertIn("dated lot-size history", J.ENGINE_CHANGES[fam])

    def test_hunt_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(lab.hunt_config(tmp), {"allow_reconstructed_lots": False})
        os.makedirs(os.path.join(tmp, "trader"))
        with open(os.path.join(tmp, "trader", "config.json"), "w") as fh:
            json.dump({"allow_reconstructed_lots": True, "capital": 100}, fh)
        self.assertEqual(lab.hunt_config(tmp), {"allow_reconstructed_lots": True})
        with open(os.path.join(tmp, "trader", "config.json"), "w") as fh:
            json.dump({"allow_reconstructed_lots": "yes"}, fh)
        with self.assertRaises(lab.LabError):
            lab.hunt_config(tmp)


if __name__ == "__main__":
    unittest.main()
