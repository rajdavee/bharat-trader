"""trader-archive tests, offline, on real NSE and Upstox answers trimmed to a few rows.

Fixtures (tests/fixtures/trader-archive/, fetched 2026-11-01 with a browser User-Agent):
  BhavCopy_NSE_CM_0_0_0_2026102{1,2,3}_F_0000.csv.zip   UDiFF equity bhavcopies, 9 stocks each; JSW Dulux
                                                         splits Rs 10 -> Re 1 (ex 22 Oct 2026, new ISIN)
  BhavCopy_NSE_FO_0_0_0_2026102{2,3}_F_0000.csv.zip     UDiFF F&O: NIFTY futures, NIFTY 27-Oct options
                                                         22400..23000, RELIANCE futures and two strikes
  cm0{2,3}JAN2023bhav.csv.zip, fo03JAN2023bhav.csv.zip  the legacy (pre-UDiFF) formats
  ind_close_all_DDMMYYYY.csv                            NSE index closes (NIFTY, Bank, Fin, India VIX)
  fo_secban_*.csv, FOSett_prce_23102026.csv, fo_mktlots.csv
  vixhistory_2026Q4.json                                NSE India VIX history (October 2026 rows)
  corporate_actions.json                                9 real rows: splits, bonuses, a split + bonus on
                                                         one day (CGCL), bonus NCRPS (SIYSIL), a dividend
  upstox_RELIANCE_1m_20261022-23.json.gz                Upstox v3 historical candles, 1-minute
  upstox_HDFCBANK_5m_202607.json.gz                     Upstox v3 historical candles, 5-minute, July 2026
  oc-contract-info-NIFTY.json, oc-v3-NIFTY.trimmed.json the option chain (for snapshot-chain)
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

arc = load("trader-archive", "archive")
DIR = os.path.join(FIX, "trader-archive")
NOW = datetime.datetime(2026, 10, 26, 8, 0)
VIX_ROWS = 4       # the fixture's VIX rows within the backfill's 19..25 Oct 2026


class Resp(object):
    def __init__(self, data):
        self.data, self.status, self.headers = data, 200, {}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fixture_for(url):
    if "vixhistory" in url:
        return "vixhistory_2026Q4.json"
    if "corporates-corporateActions" in url:
        return "corporate_actions.json"
    if "option-chain-contract-info" in url:
        return "oc-contract-info-NIFTY.json"
    if "option-chain-v3" in url:
        return "oc-v3-NIFTY.trimmed.json"
    if "indicesHistory" in url:
        return "indexhist_NIFTY50_2010Q1.json" if "from=01-01-2010" in url and "NIFTY%2050" in url else "-"
    if "corporate-board-meetings" in url:
        return "board_meetings_201201.json" if "from_date=01-01-2012" in url else "-"
    if "corporates-financial-results" in url:
        return "financial_results_201201.json" if "from_date=01-01-2012" in url else "-"
    if url.endswith("/press-release"):
        return "press-release.trimmed.html"
    return url.rsplit("/", 1)[-1]


class FakeNse(object):
    """An opener that serves the fixtures by file name; anything else is NSE's 404."""

    def __init__(self, fail_after=None, status=None):
        self.urls, self.fail_after, self.status = [], fail_after, status

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.fail_after is not None and len(self.urls) > self.fail_after:
            raise KeyboardInterrupt("the line dropped")
        if self.status:
            raise urllib.error.HTTPError(url, self.status, "blocked", {}, io.BytesIO(b"<html>"))
        path = os.path.join(DIR, fixture_for(url))
        if not os.path.isfile(path):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"<!DOCTYPE html>"))
        with open(path, "rb") as fh:
            return Resp(fh.read())


class Clock(object):
    """Time moves only when the code sleeps."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def http(opener, clock=None, pause=1.5):
    clock = clock or Clock()
    return arc.Http(pause=pause, sleep=clock.sleep, opener=opener, clock=clock.clock)


def build(root, start="2026-10-19", end="2026-10-25", datasets=arc.DATASETS, now=NOW, opener=None):
    opener = opener or FakeNse()
    return arc.backfill(arc.Store(root), http(opener), start, end, datasets, now), opener


def zipped(name):
    import zipfile
    z = zipfile.ZipFile(os.path.join(DIR, name))
    return z.read(z.namelist()[0]).decode("utf-8")


def text(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bt-archive-")
        self.addCleanup(shutil.rmtree, self.root, True)
        arc._CACHE.clear()


# ---------------------------------------------------------------- parsing both formats

class Parsing(unittest.TestCase):
    def test_udiff_equity(self):
        rows, fmt = arc.parse_eq(zipped("BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip"), datetime.date(2026, 10, 23))
        self.assertEqual(fmt, "udiff")
        rel = dict(zip(arc.EQ_COLS, [r for r in rows if r[0] == "RELIANCE"][0]))
        self.assertEqual((rel["series"], rel["isin"], rel["open"], rel["high"], rel["low"], rel["close"]),
                         ("EQ", "INE002A01018", "1184", "1201.9", "1163.1", "1190"))
        self.assertEqual((rel["prev_close"], rel["volume"], rel["trades"]), ("1184.5", "14905641", "241397"))
        self.assertEqual(rows, sorted(rows, key=lambda r: (r[0], r[1])))

    def test_legacy_equity_has_the_same_schema(self):
        rows, fmt = arc.parse_eq(zipped("cm03JAN2023bhav.csv.zip"), datetime.date(2023, 1, 3))
        self.assertEqual(fmt, "legacy")
        self.assertTrue(all(len(r) == len(arc.EQ_COLS) for r in rows))
        rel = dict(zip(arc.EQ_COLS, [r for r in rows if r[0] == "RELIANCE" and r[1] == "EQ"][0]))
        self.assertEqual(rel["isin"], "INE002A01018")
        self.assertTrue(float(rel["close"]) > 2000)          # before the 2024 bonus: raw prices
        with self.assertRaises(arc.ArchiveError):
            arc.parse_eq(zipped("cm03JAN2023bhav.csv.zip"), datetime.date(2023, 1, 2))   # the wrong day's file

    def test_udiff_fo_units(self):
        rows, fmt = arc.parse_fo(zipped("BhavCopy_NSE_FO_0_0_0_20261023_F_0000.csv.zip"), datetime.date(2026, 10, 23))
        self.assertEqual(fmt, "udiff")
        recs = [dict(zip(arc.FO_COLS, r)) for r in rows]
        fut = [r for r in recs if r["symbol"] == "NIFTY" and r["kind"] == "IDF"]
        self.assertTrue(fut and all(r["strike"] == "" and r["option_type"] == "" for r in fut))
        opt = [r for r in recs if r["kind"] == "IDO"][0]
        self.assertEqual((opt["expiry"], opt["lot"]), ("2026-10-27", "65"))
        self.assertTrue(opt["underlying_price"])
        self.assertEqual(int(opt["oi"]) % 65, 0)      # OI is in shares: whole lots of 65

    def test_legacy_fo_is_mapped(self):
        rows, fmt = arc.parse_fo(zipped("fo03JAN2023bhav.csv.zip"), datetime.date(2023, 1, 3))
        self.assertEqual(fmt, "legacy")
        recs = [dict(zip(arc.FO_COLS, r)) for r in rows]
        self.assertEqual({r["kind"] for r in recs}, {"IDF", "IDO", "STF", "STO"})
        fut = [r for r in recs if r["symbol"] == "NIFTY" and r["kind"] == "IDF"][0]
        self.assertRegex(fut["expiry"], r"^2023-\d\d-\d\d$")
        self.assertEqual((fut["strike"], fut["option_type"], fut["lot"], fut["underlying_price"]), ("", "", "", ""))
        raw = [l.split(",") for l in zipped("fo03JAN2023bhav.csv.zip").splitlines()[1:]
               if l.startswith("FUTIDX,NIFTY,") and nse(l.split(",")[2]) == fut["expiry"]][0]
        self.assertAlmostEqual(float(fut["turnover"]), float(raw[11]) * 100000, places=0)   # lakh -> rupees

    def test_index_ban_settle_lots_vix(self):
        rows, _ = arc.parse_index(text("ind_close_all_23102026.csv"), datetime.date(2026, 10, 23))
        vix = [r for r in rows if r[0] == "India VIX"][0]
        self.assertEqual(vix[6:], ["", "", "", "", ""])      # "-" -> empty
        self.assertEqual(arc.parse_ban(text("fo_secban_23102026.csv"), datetime.date(2026, 10, 23))[0],
                         [["AMBUJACEM"], ["KAYNES"], ["SAIL"]])
        self.assertEqual(arc.parse_ban(text("fo_secban_03012023.csv"))[0], [])        # ": NIL"
        st, _ = arc.parse_settle(text("FOSett_prce_23102026.csv"), datetime.date(2026, 10, 23))
        self.assertTrue(all(r[2].startswith("20") for r in st if r[0].startswith("FUT")))
        self.assertIn(["OPTIDX", "NIFTY", "", "22600"], st)        # the option rows: the underlying's settle
        lots, _ = arc.parse_lots(text("fo_mktlots.csv"))
        self.assertIn(["NIFTY 50", "NIFTY", "2026-11", "65"], lots)
        self.assertTrue(any(r[1] == "RELIANCE" for r in lots))
        v = arc.parse_vix(text("vixhistory_2026Q4.json"))
        self.assertEqual(v[0][0], "2026-10-01")
        self.assertEqual(v[0][4], "14.46")

    def test_urls_and_the_udiff_cut_over(self):
        self.assertEqual(arc.eq_url(datetime.date(2026, 10, 30)),
                         "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv.zip")
        self.assertEqual(arc.eq_url(datetime.date(2023, 1, 3)),
                         "https://nsearchives.nseindia.com/content/historical/EQUITIES/2023/JAN/cm03JAN2023bhav.csv.zip")
        self.assertEqual(arc.fo_url(datetime.date(2023, 1, 3)), "https://nsearchives.nseindia.com/content/"
                         "historical/DERIVATIVES/2023/JAN/fo03JAN2023bhav.csv.zip")
        both = arc.daily_urls("eq", datetime.date(2024, 3, 1))
        self.assertEqual([f for _, f in both], ["udiff", "legacy"])     # both formats exist Jan to Jul 2024
        self.assertEqual(len(arc.daily_urls("fo", datetime.date(2025, 3, 3))), 1)


def nse(text):
    return arc.nse_date(text)


# ---------------------------------------------------------------- corporate actions

class Adjustment(unittest.TestCase):
    def test_factors_from_subjects(self):
        f = arc.ca_factor
        self.assertAlmostEqual(f("Bonus 1:2")[1], 2.0 / 3)
        self.assertAlmostEqual(f("Bonus 1:1")[1], 0.5)
        self.assertEqual(f("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"),
                         ("split", 0.1))
        self.assertEqual(f("Face Value Split (Sub-Division) - From Rs10/- Per Share To Re 1/- Per Share"),
                         ("split", 0.1))
        self.assertEqual(f("Consolidation - From Rs 1/- Per Share To Rs 10/- Per Share"), ("consolidation", 10.0))
        self.assertIsNone(f("Scheme Of Arrangement - Bonus Ncrps 4:1"))
        self.assertIsNone(f("Dividend - Rs 5.5 Per Share"))

    def test_events_from_the_real_rows(self):
        ev = arc.ca_events(json.loads(text("corporate_actions.json")))
        by = {}
        for e in ev:
            by.setdefault(e["symbol"], []).append(e)
        self.assertEqual([(e["ex_date"], e["factor"]) for e in by["JSWDULUX"]], [("2026-10-22", 0.1)])
        self.assertAlmostEqual(by["TRENT"][0]["factor"], 2.0 / 3)
        self.assertEqual(sorted(e["kind"] for e in by["CGCL"]), ["bonus", "split"])    # same day, both apply
        self.assertNotIn("SIYSIL", by)
        self.assertNotIn("RELIANCE", by)                     # a dividend is not a price factor here
        self.assertAlmostEqual(arc.cumulative_factor(by["CGCL"], "2024-03-04"), 0.25)
        self.assertEqual(arc.cumulative_factor(by["CGCL"], "2024-03-05"), 1.0)


# ---------------------------------------------------------------- backfill, politeness, resume

class Backfill(Tmp):
    def test_backfill_fetches_and_records_each_file(self):
        s, op = build(self.root)
        self.assertEqual(s["files"], {"eq": 3, "fo": 2, "index": 3, "ban": 1, "settle": 1})
        self.assertEqual(s["rows"]["vix"], VIX_ROWS)
        self.assertGreater(s["rows"]["ca"], 0)
        self.assertGreater(s["rows"]["lots"], 0)
        self.assertFalse(any("20102026" in u or "20261020" in u for u in op.urls), "a holiday is never asked")
        self.assertFalse(any("24102026" in u or "20261025" in u for u in op.urls), "nor a weekend")
        with open(os.path.join(self.root, "eq", "2026", "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        e = man["files"]["2026-10-23"]
        self.assertEqual(e["rows"], 9)
        self.assertTrue(e["source_url"].endswith("BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip"))
        self.assertEqual((e["format"], len(e["sha256"])), ("udiff", 64))
        self.assertTrue(e["fetched_at"].startswith("2026-10-26T08:00"))
        with open(os.path.join(self.root, "eq", "2026", "2026-10-23.csv.gz"), "rb") as fh:
            self.assertEqual(e["bytes"], len(fh.read()))
        self.assertEqual(arc.Store(self.root).calendar()["closed"], {"2026-10-19": arc.Store(
            self.root).calendar()["closed"]["2026-10-19"]})   # no fixture for 19 Oct: learned as closed

    def test_polite_pause_between_calls(self):
        c = Clock()
        arc.backfill(arc.Store(self.root), http(FakeNse(), c), "2026-10-21", "2026-10-23", ["eq", "index"], NOW)
        self.assertEqual(len(c.sleeps), 5)
        self.assertTrue(all(abs(x - 1.5) < 1e-9 for x in c.sleeps), c.sleeps)
        self.assertEqual(arc.Http(pause=0.2).pause, arc.MIN_PAUSE)          # never faster than 1 s

    def test_stops_after_repeated_blocks(self):
        c = Clock()
        h = http(FakeNse(status=403), c)
        with self.assertRaises(arc.Blocked):
            arc.backfill(arc.Store(self.root), h, "2026-10-21", "2026-10-23", ["eq"], NOW)
        self.assertEqual(h.calls, 3)
        self.assertIn(20.0, c.sleeps)
        self.assertIn(40.0, c.sleeps)

    def test_resume_after_an_interruption(self):
        op = FakeNse(fail_after=4)
        with self.assertRaises(KeyboardInterrupt):
            build(self.root, opener=op)
        first = arc.Store(self.root).dates("eq") + arc.Store(self.root).dates("fo")
        self.assertTrue(first)
        # a write cut off between the file and its manifest leaves an unlisted file: it is fetched again
        ghost = os.path.join(self.root, "index", "2026", "2026-10-21.csv.gz")
        os.makedirs(os.path.dirname(ghost), exist_ok=True)
        with open(ghost, "wb") as fh:
            fh.write(b"half a file")
        self.assertIn("index/2026-10-21", arc.verify(self.root, NOW)["unlisted_files"])
        s2, op2 = build(self.root)
        again = [u for u in op2.urls if "BhavCopy" in u and any(d.replace("-", "") in u for d in first)]
        self.assertEqual(again, [], "files the manifest holds are not fetched again")
        fresh = tempfile.mkdtemp(prefix="bt-archive-fresh-")
        self.addCleanup(shutil.rmtree, fresh, True)
        build(fresh)
        for ds in ("eq", "fo", "index"):
            for d in arc.Store(fresh).dates(ds):
                with open(arc.Store(self.root).path(ds, d), "rb") as fa, open(arc.Store(fresh).path(ds, d), "rb") as fb:
                    a, b = fa.read(), fb.read()
                self.assertEqual(a, b, "%s %s: resumed archive differs from a clean one" % (ds, d))
        rep = arc.verify(self.root, NOW)
        self.assertEqual(rep["unlisted_files"], [])
        s3, op3 = build(self.root)
        self.assertEqual(s3["files"], {})
        self.assertFalse([u for u in op3.urls if "BhavCopy" in u], "a third run asks NSE for nothing old again")

    def test_legacy_days_and_not_published_yet(self):
        now = datetime.datetime(2023, 1, 9, 19, 0)
        s, _ = build(self.root, "2023-01-02", "2023-01-04", ["eq", "fo", "index"], now)
        self.assertEqual(s["files"], {"eq": 2, "fo": 1, "index": 2})
        self.assertEqual(s["closed"], ["2023-01-04"])                     # no fixture: taught as a holiday
        man = arc.Store(self.root).entry("eq", "2023-01-03")
        self.assertEqual(man["format"], "legacy")
        today = datetime.datetime(2026, 10, 30, 19, 0)
        s, _ = build(self.root, "2026-10-30", "2026-10-30", ["eq"], today)
        self.assertEqual((s["closed"], s["not_published"]), ([], ["2026-10-30"]))
        self.assertNotIn("2026-10-30", arc.Store(self.root).calendar()["closed"])

    def test_cli_backfill_status_update(self):
        real = arc.Http
        op = FakeNse()
        arc.Http = lambda pause=1.5: real(pause, sleep=lambda s: None, opener=op)
        self.addCleanup(setattr, arc, "Http", real)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arc.main(["--root", self.root, "backfill", "--from", "2026-10-21", "--to", "2026-10-23",
                             "--datasets", "eq,fo,index", "--now", "2026-10-26 08:00"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Archived 2026-10-21 to 2026-10-23", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            arc.main(["--root", self.root, "status"])
        self.assertRegex(out.getvalue(), r"eq\s+3\s+27\b")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arc.main(["--root", self.root, "update", "--datasets", "eq", "--now", "2026-10-26 08:00"])
        self.assertEqual(code, 0)
        self.assertIn("Archived 2026-10-24 to 2026-10-26", out.getvalue())


# ---------------------------------------------------------------- verify and the quality report

class Verify(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        build(self.root, "2026-10-21", "2026-10-23", ["eq", "index", "ca"])

    def test_clean_archive(self):
        rep = arc.verify(self.root, NOW)
        self.assertEqual(rep["problems"], 0, rep)
        self.assertEqual(rep["checked_files"], 6)
        ca = [c for c in rep["ca_checks"] if c["symbol"] == "JSWDULUX"][0]
        self.assertEqual((ca["date"], ca["factor"], ca["ok"]), ("2026-10-22", 0.1, True))
        self.assertLess(ca["raw_move_pct"], -85)                 # the split, raw
        self.assertLess(abs(ca["adjusted_move_pct"]), 2)         # an ordinary day, adjusted
        self.assertEqual(rep["outliers"], [])
        self.assertEqual(rep["survivorship"]["from"], "2026-10-21")

    def test_catches_a_duplicate_row(self):
        st = arc.Store(self.root)
        p = st.path("eq", "2026-10-22")
        head, rows = arc.read_gz_csv(p)
        rows.append(list(rows[0]))
        blob = arc.gz_bytes(arc.csv_text(head, rows))
        arc.atomic_write(p, blob)
        man = st.manifest("eq", "2026")
        man["files"]["2026-10-22"].update(sha256=arc.sha256(blob), bytes=len(blob), rows=len(rows))
        st.save_manifest("eq", "2026")
        rep = arc.verify(self.root, NOW)
        self.assertEqual(rep["duplicates"], ["eq/2026-10-22: 1 duplicate row"])
        self.assertEqual(rep["problems"], 1)

    def test_catches_a_gap_and_a_changed_file(self):
        st = arc.Store(self.root)
        os.remove(st.path("index", "2026-10-22"))
        with open(st.path("eq", "2026-10-21"), "ab") as fh:
            fh.write(b"x")
        rep = arc.verify(self.root, NOW)
        self.assertEqual(rep["missing_files"], ["index/2026-10-22"])
        self.assertEqual(rep["gaps"], {"index": ["2026-10-22"]})
        self.assertEqual(rep["checksum_mismatch"], ["eq/2026-10-21"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arc.main(["--root", self.root, "verify", "--now", "2026-10-26 08:00"])
        self.assertEqual(code, 1)
        self.assertIn("Gap in index: 1 trading day missing: 2026-10-22", out.getvalue())
        self.assertIn("NOT OK", out.getvalue())

    def test_unexplained_jump_is_an_outlier(self):
        st = arc.Store(self.root)
        p = st.path("eq", "2026-10-23")
        head, rows = arc.read_gz_csv(p)
        for r in rows:
            if r[0] == "TCS":
                r[6] = r[4] = str(round(float(r[8]) * 1.5, 2))       # +50% close with no corporate action
        blob = arc.gz_bytes(arc.csv_text(head, rows))
        arc.atomic_write(p, blob)
        st.manifest("eq", "2026")["files"]["2026-10-23"].update(sha256=arc.sha256(blob), bytes=len(blob))
        st.save_manifest("eq", "2026")
        rep = arc.verify(self.root, NOW)
        self.assertEqual([(o["symbol"], o["date"]) for o in rep["outliers"]], [("TCS", "2026-10-23")])
        self.assertEqual(rep["problems"], 0)        # a warning: real stocks do move 50% sometimes


# ---------------------------------------------------------------- the read API

class ReadApi(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        build(self.root)
        build(self.root, "2023-01-02", "2023-01-03", ["eq", "fo", "index"], datetime.datetime(2023, 1, 9, 19))

    def test_daily_adjusted_and_raw(self):
        raw = arc.load_daily("JSWDULUX", "2026-10-21", "2026-10-23", adjusted=False, root=self.root)
        adj = arc.load_daily("jswdulux", "2026-10-21", "2026-10-23", root=self.root)
        self.assertEqual([b["c"] for b in raw], [2994.8, 302.2, 286.6])
        self.assertEqual([b["c"] for b in adj], [299.48, 302.2, 286.6])
        self.assertEqual(adj[0]["v"], 593150)                 # 59,315 old shares are 5,93,150 new ones
        self.assertEqual([b["isin"] for b in adj], ["INE133A01011", "INE133A01029", "INE133A01029"])
        rel = arc.load_daily("RELIANCE", root=self.root)
        self.assertEqual([b["date"] for b in rel], ["2023-01-02", "2023-01-03", "2026-10-21", "2026-10-22",
                                                    "2026-10-23"])
        self.assertEqual(rel[-1]["c"], 1190.0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".cache", "eq")))
        self.assertEqual(arc.load_daily("RELIANCE", root=self.root), rel)        # from the cache, the same
        many = arc.load_daily_many(["RELIANCE", "JSWDULUX"], "2026-10-21", "2026-10-23", root=self.root)
        self.assertEqual(many["JSWDULUX"], adj)
        self.assertEqual(arc.load_daily("NOSUCH", root=self.root), [])

    def test_trading_days(self):
        self.assertEqual(arc.trading_days("2026-10-19", "2026-10-25", self.root),
                         ["2026-10-21", "2026-10-22", "2026-10-23"])
        self.assertEqual(arc.trading_days("2026-10-19", "2026-10-25", self.root, expected=True),
                         ["2026-10-21", "2026-10-22", "2026-10-23"])   # 19 learned closed, 20 Dussehra
        self.assertEqual(arc.trading_days("2026-01-30", "2026-02-02", self.root, expected=True),
                         ["2026-01-30", "2026-02-01", "2026-02-02"])   # the Budget Sunday had a session

    def test_index_and_vix(self):
        n = arc.load_index("NIFTY", "2026-10-21", "2026-10-23", self.root)
        self.assertEqual(n, arc.load_index("^NSEI", "2026-10-21", "2026-10-23", self.root))
        self.assertEqual(len(n), 3)
        self.assertTrue(20000 < n[-1]["c"] < 30000)
        old = arc.load_index("Nifty 50", "2023-01-03", "2023-01-03", self.root)
        self.assertEqual(old[0]["c"], 18232.55)
        vix = arc.load_vix("2026-10-01", "2026-10-31", self.root)
        self.assertEqual(vix[0]["date"], "2026-10-19")
        self.assertEqual(vix[0]["c"], 14.77)
        self.assertEqual(len(vix), VIX_ROWS)
        early = arc.load_vix("2023-01-01", "2023-01-31", self.root)       # only the index files have 2023
        self.assertEqual([b["date"] for b in early], ["2023-01-02", "2023-01-03"])
        self.assertEqual(early[1]["c"], 14.39)

    def test_fo_chain_eod(self):
        ch = arc.load_fo_chain_eod("NIFTY", "2026-10-23", self.root)
        self.assertEqual(ch["format"], "udiff")
        self.assertEqual(len(ch["futures"]), 3)
        self.assertEqual({o["option_type"] for o in ch["options"]}, {"CE", "PE"})
        self.assertEqual({o["lot"] for o in ch["options"]}, {65})
        self.assertTrue(20000 < ch["spot"] < 30000)
        o = ch["options"][0]
        self.assertIsInstance(o["strike"], float)
        self.assertIsInstance(o["oi"], int)
        rel = arc.load_fo_chain_eod("RELIANCE", "2026-10-23", self.root)
        self.assertTrue(rel["futures"] and rel["options"])
        self.assertNotIn("NIFTY", {f.get("symbol") for f in rel["futures"]})
        old = arc.load_fo_chain_eod("NIFTY", "2023-01-03", self.root)
        self.assertEqual(old["format"], "legacy")
        self.assertEqual(old["spot"], 18232.55)                # legacy files: spot from the index archive
        self.assertIsNone(arc.load_fo_chain_eod("NIFTY", "2026-10-21", self.root))   # not archived

    def test_ban_settle_lots(self):
        self.assertEqual(arc.load_ban("2026-10-23", self.root), ["AMBUJACEM", "KAYNES", "SAIL"])
        self.assertIsNone(arc.load_ban("2026-10-22", self.root))
        self.assertTrue(any(r["underlying"] == "NIFTY" for r in arc.load_settle("2026-10-23", self.root)))
        self.assertEqual(arc.load_lots("2026-10-26", self.root)["NIFTY"]["2026-11"], 65)

    def test_export_reads_back_in_the_lab(self):
        out = os.path.join(self.root, "JSWDULUX-1d.csv")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(arc.main(["--root", self.root, "export", "--symbol", "JSWDULUX", "--from", "2026-10-21",
                                       "--out", out]), 0)
        lab = load("trader-lab", "lab")
        bars = lab.read_bars_csv(out)
        self.assertEqual([b["c"] for b in bars], [299.48, 302.2, 286.6])
        self.assertIn("adjusted", err.getvalue())

    def test_status(self):
        s = arc.status(self.root)
        self.assertEqual(s["datasets"]["eq"]["files"], 5)
        self.assertEqual((s["datasets"]["eq"]["first"], s["datasets"]["eq"]["last"]), ("2023-01-02", "2026-10-23"))
        self.assertEqual(s["datasets"]["vix"]["rows"], VIX_ROWS)
        self.assertGreater(s["bytes"], 0)


# ---------------------------------------------------------------- intraday: broker history, Yahoo, gaps

class FakeTransport(object):
    def __init__(self, fixture):
        self.fixture, self.calls = fixture, []

    def request(self, method, url, **kw):
        self.calls.append(url)
        with gzip.open(os.path.join(DIR, self.fixture), "rt", encoding="utf-8") as fh:
            return json.load(fh)


def upstox(fixture, token="test-token"):
    mod = arc.broker_adapters()
    env = {"BT_UPSTOX_ACCESS_TOKEN": token} if token else {}
    return mod.make("upstox", env=env, transport=FakeTransport(fixture))


class Intraday(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        build(self.root, "2026-10-21", "2026-10-23", ["eq", "index"])

    def test_upstox_candles_through_the_broker_adapter(self):
        ad = upstox("upstox_RELIANCE_1m_20261022-23.json.gz")
        res, notes = arc.intraday_ingest(self.root, ["RELIANCE"], "1m", "2026-10-22", "2026-10-23", adapter=ad,
                                         now=NOW, sleep=lambda s: None)
        self.assertEqual(ad.transport.calls, ["https://api.upstox.com/v3/historical-candle/NSE_EQ%7CINE002A01018/"
                                              "minutes/1/2026-10-23/2026-10-22"])   # the ISIN from the archive
        self.assertEqual(res["RELIANCE"]["broker_bars"], 750)
        bars = arc.load_intraday("RELIANCE", "1m", root=self.root)
        self.assertEqual(len(bars), 750)
        self.assertEqual((bars[0]["date"], bars[0]["time"], bars[-1]["time"]), ("2026-10-22", "09:15", "15:29"))
        self.assertEqual({b["source"] for b in bars}, {"upstox"})
        five = arc.load_intraday("RELIANCE", "5m", root=self.root)           # built from the 1-minute bars
        self.assertEqual(len(five), 150)
        self.assertEqual(five[0]["o"], bars[0]["o"])
        self.assertEqual(five[0]["h"], max(b["h"] for b in bars[:5]))
        self.assertEqual(five[0]["v"], sum(b["v"] for b in bars[:5]))
        g = arc.intraday_gaps(self.root, "RELIANCE", "1m")
        self.assertEqual((g["days"], g["missing_days"], g["short_days"]), (2, [], []))

    def test_no_token_is_a_plain_message(self):
        ad = upstox("upstox_RELIANCE_1m_20261022-23.json.gz", token=None)
        with self.assertRaises(arc.ArchiveError) as cm:
            arc.intraday_ingest(self.root, ["RELIANCE"], "1m", "2026-10-22", "2026-10-23", adapter=ad, now=NOW)
        self.assertIn("BT_UPSTOX_ACCESS_TOKEN", str(cm.exception))
        self.assertEqual(ad.transport.calls, [])

    def test_a_complete_month_is_not_fetched_again(self):
        ad = upstox("upstox_HDFCBANK_5m_202607.json.gz")
        arc.intraday_ingest(self.root, ["HDFCBANK"], "5m", "2026-07-01", "2026-07-31", adapter=ad, now=NOW,
                            sleep=lambda s: None)
        self.assertEqual(len(ad.transport.calls), 1)
        res, _ = arc.intraday_ingest(self.root, ["HDFCBANK"], "5m", "2026-07-01", "2026-07-31", adapter=ad, now=NOW,
                                     sleep=lambda s: None)
        self.assertEqual(len(ad.transport.calls), 1)
        self.assertEqual(res["HDFCBANK"]["months_skipped"], 1)
        bars = arc.load_intraday("HDFCBANK", "5m", "2026-07-01", "2026-07-31", self.root)
        self.assertEqual(len(bars) % 75, 0)
        self.assertGreaterEqual(len(bars) // 75, 20)

    def test_yahoo_fills_gaps_and_never_overrides_the_broker(self):
        ad = upstox("upstox_HDFCBANK_5m_202607.json.gz")
        arc.intraday_ingest(self.root, ["HDFCBANK"], "5m", "2026-07-01", "2026-07-31", adapter=ad, now=NOW,
                            sleep=lambda s: None)
        with gzip.open(os.path.join(FIX, "trader-timemachine", "HDFCBANK-60d5m.json.gz"), "rt") as fh:
            ybars = arc.yahoo_bars(fh.read(), "5m", datetime.datetime(2026, 10, 30, 8, 0))
        self.assertTrue(ybars and ybars[0]["date"] >= "2026-08-04")
        added, conflicts = arc.write_intraday(self.root, "5m", "HDFCBANK", ybars, {}, NOW)
        self.assertEqual((added, conflicts), (len(ybars), 0))
        allb = arc.load_intraday("HDFCBANK", "5m", root=self.root)
        self.assertEqual({b["source"] for b in allb}, {"upstox", "yahoo"})
        b1 = {"date": "2026-07-01", "time": "09:15", "o": 1, "h": 2, "l": 1, "c": 2.0, "v": 5, "source": "upstox"}
        y1 = dict(b1, c=2.5, source="yahoo")
        y2 = dict(b1, time="09:20", source="yahoo")
        merged, added, conflicts = arc.merge_bars([b1], [y1, y2])
        self.assertEqual((added, conflicts), (1, 1))
        self.assertEqual(merged[0]["source"], "upstox")       # the broker's bar stays
        merged, _, _ = arc.merge_bars([y1], [b1])
        self.assertEqual(merged[0]["source"], "upstox")       # and replaces Yahoo's

    def test_short_day_in_the_gap_report(self):
        ad = upstox("upstox_RELIANCE_1m_20261022-23.json.gz")
        arc.intraday_ingest(self.root, ["RELIANCE"], "1m", "2026-10-22", "2026-10-23", adapter=ad, now=NOW,
                            sleep=lambda s: None)
        p = os.path.join(self.root, "intraday", "1m", "RELIANCE", "2026-10.csv.gz")
        head, rows = arc.read_gz_csv(p)
        rows = [r for r in rows if not (r[0] == "2026-10-23" and r[1] >= "15:00")]
        arc.atomic_write(p, arc.gz_bytes(arc.csv_text(head, rows)))
        g = arc.intraday_gaps(self.root, "RELIANCE", "1m")
        self.assertEqual(g["short_days"], [{"date": "2026-10-23", "bars": 345, "expected": 375}])


# ---------------------------------------------------------------- option-chain snapshots

class Snapshots(Tmp):
    def test_one_snapshot(self):
        op = FakeNse()
        n = arc.snapshot_chain(self.root, http(op), ["NIFTY"], 1, datetime.datetime(2026, 10, 30, 10, 0))
        self.assertEqual(n, 1)
        snaps = arc.chain_snapshots("NIFTY", "2026-10-30", self.root)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["time"], "10:00")
        doc = arc.load_chain_snapshot(snaps[0]["path"])
        self.assertEqual(doc["symbol"], "NIFTY")
        self.assertIn("records", doc["data"])
        self.assertIn("type=Indices", op.urls[-1])

    def test_outside_market_hours(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            arc.main(["--root", self.root, "snapshot-chain", "--now", "2026-11-01 10:00"])
        self.assertIn("not in session", out.getvalue())
        self.assertFalse(os.path.isdir(os.path.join(self.root, "chain")))


# ---------------------------------------------------------------- the long history (v0.4)

hist = arc.history()


class LegacySpan(Tmp):
    def test_oldest_formats_parse(self):
        rows, fmt = arc.parse_eq(zipped("cm02JAN1995bhav.csv.zip"), datetime.date(1995, 1, 2))
        self.assertEqual(fmt, "legacy")
        rec = dict(zip(arc.EQ_COLS, [r for r in rows if r[0] == "ABB"][0]))
        self.assertEqual((rec["isin"], rec["trades"], rec["close"]), ("", "", "600"))    # no ISIN or trades then
        rows, _ = arc.parse_fo(zipped("fo03JAN2005bhav.csv.zip"), datetime.date(2005, 1, 3))
        opts = [dict(zip(arc.FO_COLS, r)) for r in rows if r[1] == "IDO"]
        self.assertTrue(opts and {o["option_type"] for o in opts} <= {"CE", "PE"})       # the OPTIONTYPE header
        self.assertEqual(arc.nse_date("06-June-2012"), "2012-06-06")
        self.assertEqual(arc.parse_ban(text("fo_secban_06062012.csv"), datetime.date(2012, 6, 6))[0],
                         [["ABGSHIP"], ["COREEDUTEC"]])

    def test_days_before_nse_has_files_cost_no_call(self):
        op = FakeNse()
        s = arc.backfill(arc.Store(self.root), http(op), "2000-06-05", "2000-06-09", ["fo", "index"], NOW)
        self.assertEqual(op.urls, [])                       # F&O starts 2000-06-12, index files 2012-02-21
        self.assertEqual(s["files"], {})
        self.assertEqual(arc.FIRST_DAY["eq"], datetime.date(1994, 11, 3))

    def test_backfill_a_legacy_day_and_old_index_names(self):
        s, _ = build(self.root, "2012-06-01", "2012-06-01", ["index"])
        self.assertEqual(s["files"], {"index": 1})
        nifty = arc.load_index("NIFTY", root=self.root)        # stored as "S&P CNX Nifty"
        self.assertEqual((nifty[0]["date"], nifty[0]["c"]), ("2012-06-01", 4841.6))
        self.assertTrue(arc.load_index("Nifty Bank", root=self.root))      # "CNX Bank" then
        self.assertEqual(arc.index_canon("CNX Nifty Junior"), "nifty next 50")
        self.assertEqual(arc.index_canon("S&P CNX 500"), "nifty 500")

    def test_older_split_subjects(self):
        self.assertEqual(arc.ca_factor("Fv Split Rs.10 To Re.1"), ("split", 0.1))     # 2010 wording
        self.assertEqual(arc.ca_factor("Fv Split Rs.10/- To Rs.2/-"), ("split", 0.2))
        self.assertIsNone(arc.ca_factor("Bonus 1 Dvr : 10 Eq Share"))                 # DVR shares: no factor
        self.assertIsNone(arc.ca_factor("Sch Of Agmt- Bonus Deb1:1"))
        # real subjects from the 2010-2026 corporate actions that the first parser missed
        self.assertEqual(arc.ca_factor("Bonus Shares In The Ratio Of 1:1"), ("bonus", 0.5))
        self.assertEqual(arc.ca_factor("Bonus - 3:1"), ("bonus", 0.25))
        self.assertEqual(arc.ca_factor("Bonus Issue 1 : 1"), ("bonus", 0.5))
        self.assertAlmostEqual(arc.ca_factor("Annual General Meeting / Dividend - Rs 3/- Per Share / Bonus - 1:2")[1],
                               2.0 / 3)
        self.assertEqual(arc.ca_factor("Face Valus Split (Sub-Division) - From Rs 10/- Per To Rs 2/- Per Share"),
                         ("split", 0.2))
        self.assertIsNone(arc.ca_factor("Scheme Of Arrangement - Bonus Debentures 6:1"))
        self.assertIsNone(arc.ca_factor("Bonus Preference Shares 21:1"))
        both = arc.ca_factor("Bonus 1:2/Face Value Split (Sub-Division) From Rs 2/- Per Share To Re 1/- Per Share")
        self.assertEqual(both[0], "bonus+split")                  # AVANTIFEED, 26 Jun 2018: one row, both
        self.assertAlmostEqual(both[1], 1.0 / 3)

    def test_vix_history_starts_july_2010(self):
        op = FakeNse()
        arc.backfill(arc.Store(self.root), http(op), "2009-01-01", "2010-12-31", ["vix"], NOW)
        self.assertTrue(op.urls[0].endswith("from=19-07-2010&to=30-09-2010"), op.urls[0])
        self.assertEqual(len(op.urls), 2)

    def test_index_history_before_the_daily_files(self):
        op = FakeNse()
        n = arc.fetch_indexhist(arc.Store(self.root), http(op), datetime.date(2010, 1, 1), datetime.date(2010, 3, 31),
                                NOW, lambda m: None, "NIFTY 50")
        self.assertEqual(n, 60)
        self.assertIn("indexType=NIFTY%2050&from=01-01-2010&to=31-03-2010", op.urls[0])
        bars = arc.load_index("^NSEI", "2010-01-01", "2010-03-31", root=self.root)
        self.assertEqual(len(bars), 60)
        self.assertEqual((bars[-1]["date"], bars[-1]["c"]), ("2010-03-31", 5249.1))
        again = FakeNse()
        arc.fetch_indexhist(arc.Store(self.root), http(again), datetime.date(2010, 1, 1), datetime.date(2010, 3, 31),
                            NOW, lambda m: None, "NIFTY 50")
        self.assertEqual(again.urls, [])                    # a complete quarter is not asked again


class AdjustmentSpan(Tmp):
    def test_split_checked_against_the_closes_either_side(self):
        build(self.root)
        rep = arc.adjustment_span(self.root)
        self.assertEqual((rep["checked"], rep["ok"], rep["bad"]), (1, 1, []))   # JSW Dulux 10 -> 1, 22 Oct 2026


ROLL_DAYS = ("2018-06-26", "2018-06-27", "2018-06-28", "2018-06-29", "2018-07-02")


class Continuous(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        s, _ = build(self.root, "2018-06-26", "2018-07-02", ["fo"], now=datetime.datetime(2018, 7, 10, 20, 0))
        self.assertEqual(s["files"], {"fo": 5})

    def test_unadjusted_with_the_roll_log(self):
        res = arc.load_continuous("NIFTY", "none", root=self.root)
        self.assertEqual([b["date"] for b in res["bars"]], list(ROLL_DAYS))
        self.assertEqual([b["expiry"] for b in res["bars"]], ["2018-06-28"] * 3 + ["2018-07-26"] * 2)
        r = res["rolls"][0]
        self.assertEqual((r["date"], r["from"], r["to"]), ("2018-06-29", "2018-06-28", "2018-07-26"))
        jun = [x for x in arc.load_fo_chain_eod("NIFTY", "2018-06-28", self.root)["futures"]
               if x["expiry"] == "2018-06-28"][0]
        self.assertEqual(r["old"], jun["close"])
        self.assertEqual(res["bars"][2]["c"], jun["close"])          # raw prices, untouched

    def test_back_and_ratio_adjusted(self):
        raw = arc.load_continuous("NIFTY", "none", root=self.root)
        back = arc.load_continuous("NIFTY", "back", root=self.root)
        ratio = arc.load_continuous("NIFTY", "ratio", root=self.root)
        gap, k = raw["rolls"][0]["gap"], raw["rolls"][0]["ratio"]
        for i in range(3):                                           # before the roll: shifted or scaled
            self.assertAlmostEqual(back["bars"][i]["c"], round(raw["bars"][i]["c"] + gap, 2), places=2)
            self.assertAlmostEqual(ratio["bars"][i]["c"], round(raw["bars"][i]["c"] * k, 2), places=2)
        for i in (3, 4):                                             # the newest contract: real prices
            self.assertEqual(back["bars"][i]["c"], raw["bars"][i]["c"])
            self.assertEqual(ratio["bars"][i]["c"], raw["bars"][i]["c"])
        # no jump left at the roll: the adjusted old contract closes where the new one stood that day
        self.assertAlmostEqual(back["bars"][2]["c"], raw["rolls"][0]["new"], places=1)

    def test_roll_rules(self):
        one = arc.load_continuous("NIFTY", "none", roll="days:1", root=self.root)
        self.assertEqual(one["rolls"][0]["date"], "2018-06-28")      # off the June contract on expiry day
        oi = arc.load_continuous("NIFTY", "none", roll="oi", root=self.root)
        self.assertTrue(oi["rolls"])
        self.assertLessEqual(oi["rolls"][0]["date"], "2018-06-29")
        stock = arc.load_continuous("RELIANCE", "back", root=self.root)
        self.assertEqual(stock["rolls"][0]["date"], "2018-06-29")
        with self.assertRaises(arc.ArchiveError):
            arc.load_continuous("NIFTY", "none", roll="days:x", root=self.root)

    def test_cli_and_quality_report(self):
        out = io.StringIO()
        dest = os.path.join(self.root, "nifty-cont.csv")
        with contextlib.redirect_stdout(out):
            arc.main(["--root", self.root, "continuous", "--underlying", "NIFTY", "--method", "ratio", "--out", dest])
        self.assertIn("1 rolls", out.getvalue())
        with open(dest) as fh:
            self.assertEqual(len(fh.read().splitlines()), 6)
        rep = arc.roll_report(self.root)
        self.assertEqual((rep["NIFTY"]["rolls"], rep["BANKNIFTY"]["rolls"]), (1, 1))


class Results(Tmp):
    def test_results_dates_with_the_minute_they_came_out(self):
        op = FakeNse()
        n = arc.fetch_results(arc.Store(self.root), http(op), datetime.date(2012, 1, 1), datetime.date(2012, 1, 31),
                              NOW, lambda m: None)
        self.assertGreater(n, 0)
        self.assertEqual(len(op.urls), 2)
        evs = arc.results_dates("63MOONS", root=self.root)
        q3 = [e for e in evs if e["period_end"] == "2011-12-31"][0]
        self.assertEqual((q3["date"], q3["announced_at"], q3["after_close"], q3["meeting_date"]),
                         ("2012-01-31", "2012-01-31 19:44:44", True, "2012-01-31"))
        sessions = ["2012-01-30", "2012-01-31", "2012-02-01"]
        ev = hist.events.results_events("63MOONS", [], [["63MOONS", "2012-01-31 19:44:44", "2011-10-01", "2011-12-31",
                                                          "Third Quarter", "Un-Audited", "Consolidated", ""]], sessions)
        self.assertEqual(ev[0]["event_session"], "2012-02-01")      # out after 15:30: the next session reacts
        again = FakeNse()
        arc.fetch_results(arc.Store(self.root), http(again), datetime.date(2012, 1, 1), datetime.date(2012, 1, 31),
                          NOW, lambda m: None)
        self.assertEqual(again.urls, [])
        self.assertEqual(arc.results_report(self.root)["months"], 1)


class Membership(Tmp):
    def lines(self, name):
        with open(os.path.join(DIR, name), encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_announcement_with_symbols(self):
        with open(os.path.join(DIR, "ind_prs21022014.pdf"), "rb") as fh:
            ch = hist.members.announcement_changes(fh.read())
        got = sorted((c["index"], c["action"], c["symbol"], c["effective"]) for c in ch)
        self.assertEqual(got, [("NIFTY 200", "exclude", "CASTROL", "2014-02-28"),
                               ("NIFTY 200", "include", "SUPREMEIND", "2014-02-28"),
                               ("NIFTY 500", "exclude", "CASTROL", "2014-02-28"),
                               ("NIFTY 500", "include", "JYOTHYLAB", "2014-02-28")])   # CNX Midcap etc. ignored

    def test_announcement_with_names_only_and_tables_drawn_apart(self):
        with open(os.path.join(DIR, "ind_prs18082010.pdf"), "rb") as fh:
            ch = hist.members.announcement_changes(fh.read(), "Index Changes w.e.f. October 01, 2010")
        n50 = sorted((c["action"], c["name"]) for c in ch if c["index"] == "NIFTY 50")
        self.assertEqual(n50, [("exclude", "ABB Ltd."), ("exclude", "Idea Cellular Ltd."), ("exclude", "Unitech Ltd."),
                               ("include", "Bajaj Auto Ltd."), ("include", "Dr. Reddy's Laboratories Ltd."),
                               ("include", "Sesa Goa Ltd.")])
        self.assertEqual({c["effective"] for c in ch}, {"2010-10-01"})
        self.assertEqual(len([c for c in ch if c["index"] == "NIFTY 100"]), 8)        # "C NX 100" heading
        with open(os.path.join(DIR, "ind_prs20052010.pdf"), "rb") as fh:
            single = hist.members.announcement_changes(fh.read())
        self.assertEqual(sorted((c["index"], c["action"], c["symbol"]) for c in single),
                         [("NIFTY 500", "exclude", "GRASIM"), ("NIFTY 500", "include", "JSWENERGY")])

    def test_modern_announcement_lines(self):
        ch = hist.members.parse_announcement(self.lines("ind_prs10082026.lines.txt"),
                                             "Replacements in indices w.e.f. September 30, 2026")
        n50 = [(c["action"], c["symbol"]) for c in ch if c["index"] == "NIFTY 50"]
        self.assertEqual(n50, [("exclude", "WIPRO"), ("include", "BSE")])     # the Note's "1. BSE Ltd." is no row
        n500 = [c for c in ch if c["index"] == "NIFTY 500"]
        self.assertEqual((len([c for c in n500 if c["action"] == "exclude"]),
                          len([c for c in n500 if c["action"] == "include"])), (27, 27))   # across a page break
        self.assertEqual(len([c for c in ch if c["index"] == "NIFTY 100"]), 10)

    def test_symbol_identity_over_renames(self):
        sym = hist.members.Symbols(hist.members.parse_symbol_changes(text("symbolchange.csv")))
        self.assertEqual(sym.canon("SESAGOA", "2010-10-01"), "VEDL")
        self.assertEqual(sym.as_of("VEDL", "2012-01-02"), "SESAGOA")
        self.assertEqual(sym.as_of("VEDL", "2014-06-02"), "SSLT")
        self.assertEqual(sym.as_of("ETERNAL", "2024-01-01"), "ZOMATO")

    def test_fetch_rebuild_and_point_in_time(self):
        now = datetime.datetime(2026, 11, 1, 20, 0)
        op = FakeNse()
        n = arc.fetch_members(arc.Store(self.root), http(op), datetime.date(2010, 1, 1), now.date(), now, lambda m: None)
        self.assertEqual(n, 3)                     # the three PDFs on file; SME/IPO/fixed-income items skipped
        self.assertFalse([u for u in op.urls if "ind_prs29082025" in u or "ind_prs25082025" in u])
        # the August 2026 replacement, as parsed text (the PDF itself is 1 MB)
        man_path = os.path.join(self.root, "members", "manifest.json")
        man = arc.read_json(man_path)
        rel = os.path.join("members", "announcements", "2026", "ind_prs10082026.txt.gz")
        arc.atomic_write(os.path.join(self.root, rel), arc.gz_bytes("\n".join(self.lines("ind_prs10082026.lines.txt"))))
        man["announcements"]["ind_prs10082026.pdf"] = {"date": "2026-08-10", "text": rel,
                                                       "title": "Replacements in indices w.e.f. September 30, 2026"}
        arc.write_json(man_path, man)
        arc._CACHE.clear()
        before = arc.members("NIFTY", "2026-09-29", self.root)
        after = arc.members("NIFTY 50", "2026-09-30", self.root)
        self.assertEqual(len(after), 50)
        self.assertIn("WIPRO", before)
        self.assertNotIn("BSE", before)
        self.assertIn("BSE", after)
        self.assertEqual(len(before), 50)
        # 2010: names only, resolved through NSE's list of companies, today's symbol mapped back to then
        m2010 = arc.members("NIFTY 50", "2010-09-30", self.root)
        self.assertIn("UNITECH", m2010)
        self.assertNotIn("BAJAJ-AUTO", m2010)
        self.assertIn("BAJAJ-AUTO", arc.members("NIFTY 50", "2010-10-01", self.root))
        rep = arc.membership_report(self.root)["NIFTY 50"]
        names = sorted(u["name"] for u in arc.membership("NIFTY 50", self.root)["unresolved"])
        self.assertIn("Sesa Goa Ltd.", names)       # renamed company: an honest gap, not a guess
        self.assertEqual(rep["unresolved"], len(names))
        self.assertEqual(rep["wrong_size"], 0)

    def test_cli_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arc.main(["--root", self.root, "members", "--report"])
        self.assertEqual(code, 0)
        self.assertIn("No index membership data", out.getvalue())


class ExpiredOptions(Tmp):
    """Response shapes as documented by Upstox and Dhan (a paid-plan token was not available to record
    real answers): the parsers, the paths and the storage."""

    class Transport(object):
        def __init__(self, answers):
            self.answers, self.calls = answers, []

        def request(self, method, url, **kw):
            self.calls.append((method, url, kw.get("json_body")))
            for key, doc in self.answers:
                if key in url:
                    return doc
            return {"status": "success", "data": []}

    def test_upstox_expired_contracts(self):
        build(self.root, "2026-10-21", "2026-10-23", ["index"])
        mod = arc.broker_adapters()
        candles = {"status": "success", "data": {"candles": [
            ["2026-10-23T09:15:00+05:30", 120.5, 125, 118, 121, 15000, 320000],
            ["2026-10-23T09:16:00+05:30", 121, 122.4, 119.9, 120.1, 9100, 321500]]}}
        contracts = {"status": "success", "data": [
            {"instrument_key": "NSE_FO|1%d|23-10-2026" % k, "strike_price": s, "instrument_type": t,
             "expiry": "2026-10-23", "trading_symbol": "NIFTY %d %s" % (s, t), "lot_size": 65}
            for k, (s, t) in enumerate([(s, t) for s in range(22500, 24001, 100) for t in ("CE", "PE")])]}
        tr = self.Transport([("historical-candle", candles), ("option/contract", contracts),
                             ("expiries", {"status": "success", "data": ["2026-10-14", "2026-10-23"]})])
        ad = mod.make("upstox", env={"BT_UPSTOX_ACCESS_TOKEN": "test-token"}, transport=tr)
        res = arc.expired_options_ingest(self.root, "NIFTY", "2026-10-20", "2026-10-23", adapter=ad, strikes=1,
                                         now=NOW.replace(day=30), sleep=lambda s: None)
        self.assertEqual(res["files"], 6)                    # 3 strikes around the day's close, calls and puts
        self.assertIn("/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050", tr.calls[0][1])
        spot = arc.load_index("NIFTY", "2026-10-23", "2026-10-23", self.root)[0]["c"]
        atm = min(range(22500, 24001, 100), key=lambda k: abs(k - spot))
        bars = arc.load_option_intraday("NIFTY", "2026-10-23", atm, "CE", root=self.root)
        self.assertEqual([(b["time"], b["c"], b["oi"]) for b in bars], [("09:15", 121.0, 320000), ("09:16", 120.1, 321500)])

    def test_dhan_rolling_payload_and_parse(self):
        p = hist.expired.dhan_payload("NIFTY", "ATM+1", "CALL", "2026-09-01", "2026-09-30")
        self.assertEqual((p["securityId"], p["instrument"], p["expiryFlag"], p["interval"]), (13, "OPTIDX", "WEEK", "1"))
        self.assertEqual(hist.expired.dhan_windows("2026-08-01", "2026-09-15"),
                         [("2026-08-01", "2026-08-30"), ("2026-08-31", "2026-09-15")])
        doc = {"data": {"ce": {"timestamp": [1790480100, 1790480160], "open": [100, 101], "high": [102, 101.5],
                               "low": [99, 100], "close": [101, 100.5], "volume": [500, 300], "oi": [1000, 1200],
                               "iv": [14.2, 14.1], "spot": [25100, 25102], "strike": [25150, 25150]}, "pe": None}}
        bars = hist.expired.dhan_bars(doc, "ce")
        self.assertEqual((len(bars), bars[0]["iv"], bars[0]["strike"]), (2, 14.2, 25150))
        self.assertEqual(hist.expired.dhan_bars(doc, "pe"), [])

    def test_no_token_says_which_plan(self):
        mod = arc.broker_adapters()
        ad = mod.make("upstox", env={}, transport=self.Transport([]))
        with self.assertRaises(arc.ArchiveError) as e:
            arc.expired_options_ingest(self.root, "NIFTY", "2026-10-01", "2026-10-23", adapter=ad, now=NOW)
        self.assertIn("Upstox Plus", str(e.exception))


class CaptureChains(Tmp):
    def test_one_session_then_stops_with_a_heartbeat(self):
        clock = {"t": datetime.datetime(2026, 10, 30, 15, 18)}
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock["t"] += datetime.timedelta(seconds=s)

        beat = arc.capture_chains(self.root, http(FakeNse()), ["NIFTY"], every=5, expiries=1, sessions=1,
                                  now_fn=lambda: clock["t"], sleep=sleep)
        self.assertEqual((beat["state"], beat["rounds"], beat["files"], beat["sessions_done"]), ("done", 3, 3, 1))
        self.assertEqual(sleeps[:2], [300.0, 300.0])
        doc = arc.read_json(os.path.join(self.root, "chain", "capture.json"))
        self.assertEqual(doc["state"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.root, "chain", ".capture.lock")))
        rows = arc.chain_series("NIFTY", "2026-10-30", root=self.root)
        self.assertEqual(len(set(r["time"] for r in rows)), 3)
        self.assertTrue(all(r["type"] in ("CE", "PE") for r in rows))

    def test_waits_for_the_next_session(self):
        clock = {"t": datetime.datetime(2026, 10, 31, 11, 0)}       # a Saturday
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock["t"] += datetime.timedelta(seconds=s)

        beat = arc.capture_chains(self.root, http(FakeNse()), ["NIFTY"], every=10, expiries=1, sessions=1,
                                  now_fn=lambda: clock["t"], sleep=sleep, until="09:40")
        self.assertEqual(beat["state"], "done")
        self.assertLessEqual(max(sleeps), 600.0)                     # wakes at least every 10 minutes
        self.assertEqual(beat["rounds"], 3)                          # Monday 09:15, 09:25, 09:35

    def test_refuses_a_second_copy(self):
        os.makedirs(os.path.join(self.root, "chain"))
        arc.write_json(os.path.join(self.root, "chain", ".capture.lock"), {"pid": os.getppid()})
        arc.write_json(os.path.join(self.root, "chain", "capture.json"), {"state": "capturing"})   # fresh heartbeat
        with self.assertRaises(arc.ArchiveError):
            arc.capture_chains(self.root, http(FakeNse()), ["NIFTY"], now_fn=lambda: NOW, sleep=lambda s: None)


class Docs(unittest.TestCase):
    def test_skill_doc_names_the_commands_and_the_read_api(self):
        with open(os.path.join(ROOT, "trader-archive", "SKILL.md"), encoding="utf-8") as fh:
            doc = fh.read()
        for word in ("backfill", "update", "status", "verify", "export", "intraday", "snapshot-chain",
                     "load_daily", "load_intraday", "load_fo_chain_eod", "load_vix", "load_index", "trading_days"):
            self.assertIn(word, doc)


if __name__ == "__main__":
    unittest.main()
