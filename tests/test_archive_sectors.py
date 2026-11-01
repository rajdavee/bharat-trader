"""trader-archive's sector classification (backfill --datasets sectors, sectors()) and its use by trader-lab's
pairs engine, offline.

Fixtures (tests/fixtures/trader-archive/sectors/, fetched 2026-11-01 with a browser User-Agent):
  master-quote.json        NSE's F&O stock list (api/master-quote), trimmed to 7 of its 218 symbols
  quote_<SYMBOL>.json      NSE's quote API (api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData),
                           trimmed to metaData and secInfo: macro, sector, industryInfo, basicIndustry. RELIANCE,
                           HDFCBANK, ICICIBANK, INFY, TCS, M&M, MARUTI; SESAGOA (an old symbol, now VEDL) answers
                           200 with empty fields
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

arc = load("trader-archive", "archive")
lab = load("trader-lab", "lab")
E = lab.engines_api()
DIR = os.path.join(FIX, "trader-archive", "sectors")
NOW = datetime.datetime(2026, 11, 1, 20, 0)
REAL = os.path.join(ROOT, "trader-lab", "library", "edge-hunt")


class Resp(object):
    def __init__(self, data):
        self.data, self.status, self.headers = data, 200, {}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNse(object):
    def __init__(self, fail_after=None):
        self.urls, self.fail_after = [], fail_after

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.fail_after is not None and len(self.urls) > self.fail_after:
            raise KeyboardInterrupt("the line dropped")
        if url.endswith("/api/master-quote"):
            name = "master-quote.json"
        elif "GetQuoteApi" in url:
            name = "quote_%s.json" % urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["symbol"][0]
        else:
            name = "-"
        path = os.path.join(DIR, name)
        if not os.path.isfile(path):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"<!DOCTYPE html>"))
        with open(path, "rb") as fh:
            return Resp(fh.read())


def http(op):
    t = [0.0]

    def sleep(s):
        t[0] += s
    return arc.Http(pause=1.5, sleep=sleep, opener=op, clock=lambda: t[0])


class Sectors(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bt-sectors-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def fetch(self, op=None, now=NOW, symbols=None):
        op = op or FakeNse()
        n = arc.fetch_sectors(arc.Store(self.root), http(op), now.date(), now.date(), now, lambda m: None, symbols)
        return n, op

    def test_parse_nse_quote(self):
        with open(os.path.join(DIR, "quote_HDFCBANK.json"), "rb") as fh:
            r = arc.parse_sector_quote(fh.read(), "HDFCBANK")
        self.assertEqual((r["macro"], r["sector"], r["industry"], r["basic_industry"]),
                         ("Financial Services", "Financial Services", "Banks", "Private Sector Bank"))
        self.assertEqual(r["isin"], "INE040A01034")
        self.assertEqual(r["listed"], "1995-11-08")
        with open(os.path.join(DIR, "quote_SESAGOA.json"), "rb") as fh:
            self.assertIsNone(arc.parse_sector_quote(fh.read(), "SESAGOA"))
        self.assertIsNone(arc.parse_sector_quote(b"<html>", "X"))

    def test_fetch_politely_into_a_dated_snapshot(self):
        n, op = self.fetch(symbols=None)
        self.assertEqual(n, 7)
        self.assertEqual(len(op.urls), 8)                          # the list, then one quote a stock
        self.assertTrue(any("symbol=M%26M" in u for u in op.urls))
        man = arc.read_json(os.path.join(self.root, "sectors", "manifest.json"))
        e = man["snapshots"]["2026-11-01"]
        self.assertTrue(e["complete"])
        self.assertEqual((e["symbols"], e["unclassified"]), (7, 0))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "sectors", "2026", "2026-11-01.json.gz")))
        # fresh: a second run the next day asks nothing
        n2, op2 = self.fetch(now=NOW + datetime.timedelta(days=1))
        self.assertEqual((n2, op2.urls), (0, []))

    def test_old_symbols_are_recorded_unclassified(self):
        n, _ = self.fetch(symbols=["SESAGOA", "TCS", "NOSUCH"])
        self.assertEqual(n, 1)
        snap = arc._get_json(self.root, os.path.join("sectors", "2026", "2026-11-01.json.gz"))
        self.assertEqual(sorted(snap["unclassified"]), ["NOSUCH", "SESAGOA"])
        self.assertIsNone(arc.sectors("SESAGOA", root=self.root))

    def test_a_stopped_run_resumes(self):
        with self.assertRaises(KeyboardInterrupt):
            self.fetch(FakeNse(fail_after=4))
        man = arc.read_json(os.path.join(self.root, "sectors", "manifest.json"))
        self.assertFalse(man["snapshots"]["2026-11-01"]["complete"])
        n, op = self.fetch()
        self.assertEqual(len(op.urls), 1 + 7 - 3)                  # the list + the four not yet asked
        self.assertTrue(arc.read_json(os.path.join(self.root, "sectors", "manifest.json"))["snapshots"]
                        ["2026-11-01"]["complete"])

    def test_read_api(self):
        with self.assertRaises(arc.ArchiveError):
            arc.sectors(root=self.root)
        self.fetch()
        m = arc.sectors(root=self.root)
        self.assertEqual(m["HDFCBANK"], m["ICICIBANK"])
        self.assertEqual(m["INFY"], "Information Technology")
        self.assertEqual(m["M&M"], m["MARUTI"])
        self.assertEqual(len(m), 7)
        self.assertEqual(arc.sectors(root=self.root, level="industry")["TCS"], "IT - Software")
        r = arc.sectors("reliance", "2027-01-01", self.root)
        self.assertEqual((r["sector"], r["asof"], r["before_first_snapshot"]),
                         ("Oil Gas & Consumable Fuels", "2026-11-01", False))
        self.assertTrue(arc.sectors("RELIANCE", "2015-06-01", self.root)["before_first_snapshot"])
        with self.assertRaises(arc.ArchiveError):
            arc.sectors(root=self.root, level="nope")

    def test_backfill_dataset_and_cli(self):
        s = arc.backfill(arc.Store(self.root), http(FakeNse()), "2026-11-01", "2026-11-01", ["sectors"], NOW)
        self.assertEqual(s["rows"]["sectors"], 7)
        self.assertIn("sectors", arc.LONG_DATASETS)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = arc.main(["--root", self.root, "sectors", "--symbol", "TCS"])
        self.assertEqual(code, 0)
        self.assertIn("Information Technology > Information Technology > IT - Software", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            arc.main(["--root", self.root, "sectors", "--json"])
        self.assertEqual(json.loads(buf.getvalue())["HDFCBANK"], "Financial Services")
        self.assertEqual(arc.long_status(self.root)["sectors"]["symbols"], 7)


class PairsUseTheArchive(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bt-sectors-lab-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_eh11_is_no_longer_blocked_on_sectors(self):
        with open(os.path.join(REAL, "eh-11-pairs.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        empty = E.market.ArchiveMarket(self.root, arc)
        self.assertIsNone(empty.sectors())
        blocked = E.jobs.job_for(doc, empty).blockers()
        self.assertTrue(any("--datasets sectors" in b for b in blocked))
        t = [0.0]
        arc.fetch_sectors(arc.Store(self.root), arc.Http(sleep=lambda s: None, opener=FakeNse(), clock=lambda: t[0]),
                          NOW.date(), NOW.date(), NOW, lambda m: None)
        mk = E.market.ArchiveMarket(self.root, arc)
        got = mk.sectors()
        self.assertEqual(got["ICICIBANK"], "Financial Services")
        self.assertFalse([b for b in E.jobs.job_for(doc, mk).blockers() if "sector" in b])

    def test_a_sectors_json_still_wins(self):
        with open(os.path.join(self.root, "sectors.json"), "w") as fh:
            json.dump({"AAA": "X"}, fh)
        self.assertEqual(E.market.ArchiveMarket(self.root, arc).sectors(), {"AAA": "X"})


if __name__ == "__main__":
    unittest.main()
