"""trader-lab memory work (#88): memory-bounded loaders give the very same numbers, the memory guard stops a
hunt cleanly, an interrupted hunt resumes without counting a trial twice or opening the holdout early, and
a board with no spot (eh-05's crash) is handled honestly.

Fixtures:
  tests/fixtures/trader-lab/memory-golden.json   digests of every engine's results on the existing fixtures,
      computed by tests/_golden.py with the code before the memory work; the same cases are recomputed here.
  tests/fixtures/trader-lab/legacy/               a trader-archive folder cut from NSE's real legacy files
      (2015-03-31 to 2015-04-07): F&O bhavcopies trimmed to NIFTY futures and its 2015-04-30 / 2015-05-28
      options on 100-point strikes from 7800 to 9600, and HDFCBANK futures and April options from 900 to
      1150; the HDFCBANK equity rows; the index files of 31 Mar and 1 Apr cut to CNX Nifty and India VIX.
      NSE's index files for 6 and 7 Apr 2015 are not in the archive (two of the NIFTY sessions eh-05
      crashed on): legacy F&O files carry no underlying price, so those days have no spot from NSE.
"""

import copy
import gzip
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402
import _golden  # noqa: E402

lab = load("trader-lab", "lab")
O = lab.options_api()
EN = O.engines_api()
A = lab.archive_api()
ENG = lab.engines_api()
C, X, J, M = ENG.core, ENG.executor, ENG.jobs, ENG.market
LABFIX = os.path.join(FIX, "trader-lab")
HUNT = os.path.join(LABFIX, "hunt")
LEGACY = os.path.join(LABFIX, "legacy")
NOW = __import__("datetime").datetime(2026, 11, 1, 12, 0)


def golden():
    with open(os.path.join(LABFIX, "memory-golden.json"), encoding="utf-8") as fh:
        return json.load(fh)


def tmp_library(t):
    d = tempfile.mkdtemp(prefix="bt-mem-")
    t.addCleanup(shutil.rmtree, d, True)
    lib = os.path.join(d, "edge-hunt")
    shutil.copytree(os.path.join(HUNT, "edge-hunt"), lib)
    return lib, os.path.join(d, "out")


def market():
    return M.FolderMarket(HUNT, lab.archive_api(), bars_dir=LABFIX)


class SameNumbers(unittest.TestCase):
    def test_every_engine_matches_the_digests_from_before(self):
        want = golden()
        got = _golden.golden_digests(lab, ROOT)
        self.assertEqual(sorted(got), sorted(want))
        self.assertEqual([k for k in want if got[k] != want[k]], [])

    def test_tiny_caches_change_nothing(self):
        """Evicting unpacked boards and analyses all the time recomputes the same numbers."""
        old = (O.DECODED_BOARDS, O.ANALYSIS_CACHE)
        O.DECODED_BOARDS, O.ANALYSIS_CACHE = 1, 2
        try:
            got = _golden.option_cases(lab, ROOT)
        finally:
            O.DECODED_BOARDS, O.ANALYSIS_CACHE = old
            O.release_boards()
        want = golden()
        self.assertEqual([k for k in got if got[k] != want[k]], [])


class PackedBoards(unittest.TestCase):
    def test_reads_like_the_dict_it_replaces_and_is_smaller(self):
        ch = A.load_fo_chain_eod("NIFTY", "2015-04-01", root=LEGACY)
        b = O.trim_board(ch, keep_dte=63, band=0.12, monthly_only=True)
        p = b["opts"]
        self.assertIsInstance(p, O.PackedOpts)
        plain = {}
        for o in ch["options"]:
            if o["expiry"] in b["monthly"] and abs(o["strike"] - b["spot"]) <= 0.12 * b["spot"]:
                plain[(o["expiry"], float(o["strike"]), o["option_type"])] = (
                    o.get("settle") or 0.0, o.get("close") or 0.0, o.get("open") or 0.0, int(o.get("volume") or 0),
                    int(o.get("oi") or 0), o.get("lot"))
        self.assertEqual(list(p.items()), list(plain.items()))     # same contracts, same order, same values
        self.assertEqual(len(p), len(plain))
        k = next(iter(plain))
        self.assertIn(k, p)
        self.assertEqual(p[k], plain[k])
        self.assertIsNone(p.get(("2099-01-01", 1.0, "CE")))
        self.assertLessEqual(p.nbytes(), 72 * len(plain))          # 9 doubles a contract
        saved = p.pop(k)
        self.assertNotIn(k, p)
        p[k] = saved
        self.assertEqual(p[k], plain[k])

    def test_unpacked_boards_are_bounded(self):
        old = O.DECODED_BOARDS
        O.DECODED_BOARDS = 2
        try:
            O.release_boards()
            chains = O.load_chains("NIFTY", LEGACY, A)
            boards = [chains.boards[d]["opts"] for d in chains.days]
            for p in boards:
                len(list(p.items()))
            self.assertEqual(sum(1 for p in boards if p._d is not None), 2)
            self.assertEqual(len(O._HOT), 2)
        finally:
            O.DECODED_BOARDS = old
            O.release_boards()

    def test_lru(self):
        c = O.LRU(2)
        c["a"], c["b"] = 1, 2
        _ = c["a"]
        c["c"] = 3
        self.assertEqual(sorted(c), ["a", "c"])


class MissingSpot(unittest.TestCase):
    """eh-05 stopped on `spot * pct` with spot None: a legacy NIFTY board on a day without an index file."""

    def test_archive_gives_no_spot_on_those_days(self):
        self.assertIsNotNone(A.load_fo_chain_eod("NIFTY", "2015-04-01", root=LEGACY)["spot"])
        self.assertIsNone(A.load_fo_chain_eod("NIFTY", "2015-04-06", root=LEGACY)["spot"])

    def test_futures_implied_spot_and_a_logged_source(self):
        chains = O.load_chains("NIFTY", LEGACY, A, band=0.12, keep_dte=63, monthly_only=True)
        self.assertEqual(chains.days, ["2015-03-31", "2015-04-01", "2015-04-06", "2015-04-07"])
        self.assertEqual(chains.boards["2015-04-01"]["spot"], 8586.25)       # CNX Nifty close, 1 Apr 2015
        b = chains.boards["2015-04-06"]
        # the April future settled at 8689.8 on 6 Apr; 24 days to the 30 Apr expiry
        self.assertAlmostEqual(b["spot"], 8689.8 * math.exp(-0.0557 * 24 / 365.0), places=6)
        self.assertTrue(b["spot_source"].startswith("futures-implied"))
        self.assertEqual(chains.implied_spot_days, ["2015-04-06", "2015-04-07"])
        self.assertIn("futures-implied spot", chains.source)
        self.assertTrue(all(abs(k - b["spot"]) <= 0.12 * b["spot"] for (_, k, _) in b["opts"]))   # trimmed

    def test_no_spot_and_no_future_is_left_out_with_a_reason(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        arc = os.path.join(tmp, "archive")
        shutil.copytree(LEGACY, arc)
        p = os.path.join(arc, "fo", "2015", "2015-04-07.csv.gz")
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if not l.startswith("NIFTY,IDF")]
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        chains = O.load_chains("NIFTY", arc, A)
        self.assertNotIn("2015-04-07", chains.days)
        self.assertIn("no NIFTY spot", chains.skipped_days["2015-04-07"])
        self.assertIn("left out with no spot", chains.source)

    def test_eh05_runs_through_those_days(self):
        h = _golden._hyp(ROOT, "eh-05")
        p = {"short_delta": 0.1, "wing_width_pct_of_spot": 1.5, "vrp_threshold_vol_points": 0, "exit": "expiry"}
        d = {"root": LEGACY, "archive": A, "regime": lab.regime_api()}
        r = EN.run(h, p, "2015-04-06", "2015-04-07", d)       # crashed with a TypeError before
        self.assertEqual(r["extra"]["engine"], "vrp-condor")
        self.assertTrue(any("futures-implied" in n for n in r["notes"]))
        # the timed one has no 20-session realised vol yet: skipped with that reason, not a zero
        self.assertEqual(r["extra"]["skipped"], {"no India VIX or realised vol that day": 1})
        # legacy bhavcopies carry no lot size and the archive's lot snapshot is from 2026: skipped, said so
        self.assertEqual(r["extra"]["baseline"]["skipped"], {"no legs: no lot size for NIFTY 2015-04-30": 1})
        with self.assertRaises(EN.EngineError):
            EN._wing_points(None, 1.5, 50)
        # with a lot size on file (a test snapshot, lot 25, not checked against NSE's 2015 circulars) the untimed
        # condor is sold on 6 Apr with wings sized off the futures-implied spot
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        arc = os.path.join(tmp, "archive")
        shutil.copytree(LEGACY, arc)
        os.makedirs(os.path.join(arc, "lots", "2015"))
        with gzip.open(os.path.join(arc, "lots", "2015", "2015-03-02.csv.gz"), "wt", encoding="utf-8") as fh:
            fh.write("underlying,symbol,month,lot\nNIFTY 50,NIFTY,2015-04,25\nNIFTY 50,NIFTY,2015-05,25\n")
        r = EN.run(h, p, "2015-04-06", "2015-04-07", {"root": arc, "archive": A, "regime": lab.regime_api()})
        t = r["extra"]["baseline"]
        self.assertEqual(t["n"], 1)
        spot = 8689.8 * math.exp(-0.0557 * 24 / 365.0)
        self.assertEqual(EN._wing_points(spot, 1.5, 100), 100.0)   # 1.5% of about 8,680 on 100-point strikes

    def test_a_hunt_without_lot_sizes_waits_instead_of_rejecting(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lib = os.path.join(tmp, "edge-hunt")
        os.makedirs(lib)
        rows = []
        for pre, hid in (("eh-05", "eh-95-legacy-vrp"), ("eh-08", "eh-96-legacy-crush")):
            doc = _golden._hyp(ROOT, pre)
            doc["id"] = hid
            doc["span"] = {"train": {"from": "2015-03-31", "to": "2015-04-01"},
                           "validation": {"from": "2015-04-06", "to": "2015-04-07"},
                           "holdout": {"from": "2015-04-21", "to": "2015-04-30"}, "embargo_days": 14}
            with open(os.path.join(lib, hid + ".json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            rows.append({"id": hid, "sha256": X.registered_hash(doc)})
        with open(os.path.join(lib, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"hypotheses": rows}, fh)
        for hid in ("eh-95-legacy-vrp", "eh-96-legacy-crush"):
            r = X.execute(os.path.join(lib, hid + ".json"), M.ArchiveMarket(LEGACY, A), os.path.join(tmp, "out"), NOW)
            self.assertEqual(r["state"], "blocked", hid)
            self.assertIn("no lot sizes", r["plan"]["blockers"][0])
            self.assertIn("2015-03-31", r["plan"]["blockers"][0])
            self.assertFalse(os.path.exists(os.path.join(tmp, "out", "edge-hunt", hid, "trials.jsonl")))

    def test_stock_spot_from_the_equity_close_on_legacy_files(self):
        lines = EN._fo_lines(A, LEGACY, "2015-04-06")
        bare = EN._chain_dicts(A, "2015-04-06", lines, {"HDFCBANK"})["HDFCBANK"]
        self.assertIsNone(bare["spot"])                        # no underlying price in a legacy bhavcopy
        data = {"root": LEGACY, "archive": A, "until": "2015-04-07"}
        got = EN._chain_dicts(A, "2015-04-06", lines, {"HDFCBANK"}, EN._spot_of(data, A, LEGACY))["HDFCBANK"]
        eq = A.load_daily("HDFCBANK", "2015-04-06", "2015-04-06", adjusted=False, root=LEGACY)[0]["c"]
        self.assertEqual(got["spot"], eq)
        self.assertIn("equity", got["spot_source"])
        b = O.trim_board(got, keep_dte=EN.EVENT_DTE, band=EN.EVENT_BAND)
        self.assertTrue(b["opts"] and all(abs(k - eq) <= EN.EVENT_BAND * eq for (_, k, _) in b["opts"]))

    def test_board_window_reads_each_day_once_and_lets_go(self):
        need = {"2015-04-06": {"HDFCBANK"}, "2015-04-07": {"HDFCBANK"}}
        uses = {("HDFCBANK", "2015-04-06"): 1, ("HDFCBANK", "2015-04-07"): 2}
        data = {"root": LEGACY, "archive": A, "until": "2015-04-07"}
        w = EN.BoardWindow(data, need, uses, EN._spot_of(data, A, LEGACY))
        self.assertIsNotNone(w.get("HDFCBANK", "2015-04-06"))
        self.assertIsNotNone(w.get("HDFCBANK", "2015-04-07"))
        self.assertIsNotNone(w.get("HDFCBANK", "2015-04-06"))
        self.assertEqual(w.reads, 2)
        w.done("HDFCBANK", ["2015-04-06", "2015-04-07"])
        self.assertEqual(sorted(w.live), [("HDFCBANK", "2015-04-07")])   # a second event still needs 7 Apr
        w.done("HDFCBANK", ["2015-04-07"])
        self.assertEqual(w.live, {})
        self.assertIsNone(w.get("HDFCBANK", "2015-04-07"))                # nothing needs it: never re-read
        self.assertEqual(w.reads, 2)


class Guard(unittest.TestCase):
    def test_peak_rss_units(self):
        self.assertEqual(C.peak_rss_mb(3 * 1024 * 1024, "darwin"), 3.0)     # macOS: bytes
        self.assertEqual(C.peak_rss_mb(3 * 1024, "linux"), 3.0)             # Linux: kilobytes
        mb = C.peak_rss_mb()
        self.assertTrue(mb is None or mb > 1)

    def test_guard_raises_past_its_limit_only(self):
        g = C.MemoryGuard(100, reader=lambda: 99.0)
        g.check("x")
        g2 = C.MemoryGuard(100, reader=lambda: 101.0)
        with self.assertRaises(C.MemoryLimit) as e:
            g2.check("loading bars")
        self.assertIn("101 MB, over the 100 MB limit (loading bars)", str(e.exception))
        self.assertIn("holdout is untouched", str(e.exception))
        C.MemoryGuard(0, reader=lambda: 1e9).check()                        # 0 turns it off

    def test_a_hunt_stops_cleanly_and_resumes(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-91-fixture-momentum.json")
        state = {"checks": 0}

        def reader():
            state["checks"] += 1
            return 5000.0 if state["checks"] > 7 else 100.0     # the trials' checks: 3 a trial after loading
        r = X.execute(p, market(), out, NOW, mem_guard=C.MemoryGuard(2048, reader=reader))
        self.assertEqual(r["state"], "paused")
        self.assertTrue(r["memory"])
        self.assertIn("5000 MB, over the 2048 MB limit", r["why"])
        d = X.run_dir(out, "eh-91-fixture-momentum")
        rows = X.read_trials(d)
        self.assertEqual(len(rows), 1)                          # trial 1 finished; trial 2 stopped mid-way
        self.assertEqual(r["done"], 1)
        self.assertFalse(os.path.exists(os.path.join(d, "holdout.json")))
        self.assertEqual(X.read_json(p)["status"], "running")
        st = X.read_json(os.path.join(d, "state.json"))
        self.assertEqual(st["phase"], "trials")
        self.assertIn("memory guard", st["last_stop"]["why"])
        self.assertIsNone(C.ENV.get("mem_guard"))               # the guard is unbound after the run
        again = X.execute(p, market(), out, NOW)
        self.assertEqual(again["state"], "decided")
        rows = X.read_trials(d)
        self.assertEqual(sorted(x["trial"] for x in rows), [0, 1, 2, 3])
        self.assertEqual(again["results"]["dsr"]["trials"], 4)  # interrupted or not, the grid is charged once
        self.assertEqual(again["outcome"]["trials"]["trials_counted"], 4)

    def test_the_cli_returns_4_on_a_memory_stop(self):
        if C.peak_rss_mb() is None:
            self.skipTest("no resource module on this platform: no guard")
        lib, out = tmp_library(self)
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = lab.main(["--out", out, "hunt", "--library", lib, "run", "eh-92", "--data", HUNT,
                             "--max-mem-mb", "1"])
        self.assertIn("memory guard: stops at 1 MB", err.getvalue())
        self.assertEqual(code, 4)
        self.assertIn("paused by the memory guard", buf.getvalue())
        self.assertIn("over the 1 MB limit", buf.getvalue())

    def test_config_default(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "trader"))

        class Args(object):
            max_mem_mb = None
        with open(os.path.join(tmp, "trader", "config.json"), "w") as fh:
            json.dump({"lab_max_mem_mb": 1500}, fh)
        g, limit = lab.hunt_memory_guard(Args(), tmp)
        if C.peak_rss_mb() is not None:
            self.assertEqual((limit, g.limit_mb), (1500, 1500.0))
        with open(os.path.join(tmp, "trader", "config.json"), "w") as fh:
            json.dump({}, fh)
        g, limit = lab.hunt_memory_guard(Args(), tmp)
        if C.peak_rss_mb() is not None:
            self.assertEqual(limit, 2048)
        Args.max_mem_mb = 0
        self.assertEqual(lab.hunt_memory_guard(Args(), tmp), (None, 0))


class Resume(unittest.TestCase):
    def _two_trials_then_stop(self, lib, out):
        p = os.path.join(lib, "eh-91-fixture-momentum.json")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2
        r = X.execute(p, market(), out, NOW, cancel=cancel)
        self.assertEqual((r["state"], r["done"]), ("paused", 2))
        return p, X.run_dir(out, "eh-91-fixture-momentum")

    def test_torn_and_repeated_rows_count_once(self):
        lib, out = tmp_library(self)
        p, d = self._two_trials_then_stop(lib, out)
        path = os.path.join(d, "trials.jsonl")
        with open(path, encoding="utf-8") as fh:
            first = fh.readline()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(first)                                     # trial 0 logged twice
            fh.write('{"trial": 2, "params": {"lookba')           # a crash while writing trial 2
        torn = []
        self.assertEqual([x["trial"] for x in X.read_trials(d, torn=torn)], [0, 1])
        self.assertEqual(torn, [4])
        r = X.execute(p, market(), out, NOW)
        self.assertEqual(r["state"], "decided")
        self.assertEqual(r["plan"]["torn_lines"], [4])
        self.assertEqual(sorted(x["trial"] for x in X.read_trials(d)), [0, 1, 2, 3])
        self.assertEqual(r["results"]["dsr"]["trials"], 4)
        self.assertEqual(r["outcome"]["trials"]["trials_counted"], 4)

    def test_rows_of_an_older_engine_are_never_mixed(self):
        lib, out = tmp_library(self)
        p, d = self._two_trials_then_stop(lib, out)
        self.assertTrue(all(x["engine_rev"] == 1 for x in X.read_trials(d)))
        old = dict(J.ENGINE_REV)
        J.ENGINE_REV["momentum-xs"] = 2
        self.addCleanup(lambda: (J.ENGINE_REV.clear(), J.ENGINE_REV.update(old)))
        r = X.execute(p, market(), out, NOW)
        self.assertEqual(r["state"], "blocked")
        self.assertIn("revision 1 of the momentum-xs engine", r["plan"]["blockers"][0])
        self.assertIn("--rerun-superseded", r["plan"]["blockers"][0])
        self.assertEqual(len(X.read_trials(d)), 2)              # nothing moved without the flag
        dry = X.execute(p, market(), out, NOW, dry_run=True, rerun_superseded=True)
        self.assertEqual((dry["state"], dry["plan"]["superseded_trials"], dry["plan"]["trials_counted"]),
                         ("dry-run", 2, 6))
        self.assertEqual(len(X.read_trials(d)), 2)              # a dry run moves nothing either
        r = X.execute(p, market(), out, NOW, rerun_superseded=True)
        self.assertEqual(r["state"], "decided")
        self.assertEqual(len(X.read_trials(d, X.SUPERSEDED)), 2)
        self.assertTrue(all(x["engine_rev"] == 2 for x in X.read_trials(d)))
        self.assertEqual(r["outcome"]["trials"]["trials_counted"], 6)     # 4 + the 2 set aside
        self.assertEqual(r["outcome"]["trials"]["superseded_trials"], 2)
        self.assertEqual(r["results"]["dsr"]["trials"], 6)
        with open(os.path.join(d, "scorecard.md"), encoding="utf-8") as fh:
            self.assertIn("older engine revision and set aside 2", fh.read())

    def test_the_holdout_data_is_read_before_the_opening_is_logged(self):
        lib, out = tmp_library(self)
        p = os.path.join(lib, "eh-92-fixture-turn-of-month.json")
        real = X.gate_rows
        X.gate_rows = lambda *a, **k: [dict(r, **{"pass": True}) for r in real(*a, **k)]
        self.addCleanup(setattr, X, "gate_rows", real)
        orig = J.FuturesJob.load

        def heavy(self_, until):
            orig(self_, until)
            if until > "2026-05-29":
                raise C.MemoryLimit("memory guard: test")
        J.FuturesJob.load = heavy
        self.addCleanup(setattr, J.FuturesJob, "load", orig)
        r = X.execute(p, market(), out, NOW)
        self.assertEqual((r["state"], r.get("memory")), ("paused", True))
        d = r["dir"]
        self.assertFalse(os.path.exists(os.path.join(d, "holdout.json")))       # still sealed
        self.assertNotIn("holdout_opened_at", json.dumps(X.read_json(p).get("outcome")))
        J.FuturesJob.load = orig
        again = X.execute(p, market(), out, NOW)
        self.assertEqual(again["state"], "decided")
        self.assertTrue(os.path.exists(os.path.join(d, "holdout.json")))         # opened once, now


if __name__ == "__main__":
    unittest.main()
