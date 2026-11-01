"""The auto-pilot's intraday option-chain capture job (trader-autopilot CaptureJob), offline.

The capture process is a stub: spawn() records the command line and writes the heartbeat and lock files the real
trader-archive capture-chains writes (archive/chain/capture.json, archive/chain/.capture.lock); alive() and
terminate() are stubs too, so nothing is started and NSE is never asked. The snapshot files carry a REAL NIFTY
option chain (tests/fixtures/trader-options/chain-NIFTY-03-Nov-2026.json.gz) in trader-archive's snapshot shape.
"""

import datetime
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

ap = load("trader-autopilot", "autopilot")
SESS = (datetime.time(9, 0), datetime.time(9, 15), datetime.time(15, 30))
DAY = "2026-11-02"


def at(hhmm, day=DAY):
    return datetime.datetime.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M")


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


class Proc(object):
    """A pretend capture-chains: pids, which of them are alive, and the files it would write."""

    def __init__(self, root):
        self.root = root
        self.alive_pids = set()
        self.started = []
        self.killed = []
        self.next_pid = 4100

    def spawn(self, argv, log_path):
        self.next_pid += 1
        self.started.append(argv)
        self.alive_pids.add(self.next_pid)
        os.makedirs(os.path.join(self.root, "chain"), exist_ok=True)
        with open(os.path.join(self.root, "chain", ".capture.lock"), "w") as fh:
            json.dump({"pid": self.next_pid, "started": "x"}, fh)
        return self.next_pid

    def beat(self, state, last=None, nxt=None, error=None):
        doc = {"pid": max(self.alive_pids) if self.alive_pids else None, "state": state, "rounds": 3, "files": 12,
               "last": stamp(last) if last else None, "next": stamp(nxt) if nxt else None, "last_error": error}
        with open(os.path.join(self.root, "chain", "capture.json"), "w") as fh:
            json.dump(doc, fh)

    def alive(self, pid):
        try:
            return int(pid) in self.alive_pids
        except (TypeError, ValueError):
            return False

    def terminate(self, pid):
        self.killed.append(pid)
        self.alive_pids.discard(int(pid))
        return True

    def die(self):
        self.alive_pids.clear()


class Job(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-capture-")
        self.paths = ap.Paths(self.dir)
        os.makedirs(self.paths.ap)
        self.root = os.path.join(self.dir, "trader", "archive")
        self.proc = Proc(self.root)
        self.told = []
        script = os.path.join(ROOT, "trader-archive", "archive.py")
        self.job = ap.CaptureJob(self.paths, ap.capture_settings({}), self.root, spawn=self.proc.spawn,
                                 alive=self.proc.alive, terminate=self.proc.terminate, script=script,
                                 notify=lambda e, ti, tx, key=None: self.told.append((e, tx, key)))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def snap(self, sym, hhmm, expiry="2026-11-03"):
        with gzip.open(os.path.join(FIX, "trader-options", "chain-NIFTY-03-Nov-2026.json.gz"), "rt",
                       encoding="utf-8") as fh:
            data = json.load(fh)
        d = os.path.join(self.root, "chain", sym, DAY)
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, "%s_%s.json.gz" % (hhmm, expiry)), "wt", encoding="utf-8") as fh:
            json.dump({"symbol": sym, "expiry": expiry, "data": data}, fh)
        return len(data["records"]["data"])

    def test_settings(self):
        c = ap.capture_settings(None)
        self.assertEqual((c["symbols"], c["every_min"], c["expiries"], c["enabled"]), (["NIFTY", "BANKNIFTY"], 5, 2, True))
        for bad in ({"symbols": []}, {"symbols": ["nifty"]}, {"every_min": 2}, {"stale_min": 5}, {"nope": 1},
                    {"enabled": "yes"}, {"max_restarts": -1}):
            with self.assertRaises(ap.AutopilotError, msg=bad):
                ap.capture_settings(bad)
        with self.assertRaises(ap.AutopilotError):
            ap.settings_of({"autopilot": {"chain_capture": {"every_min": 1}}})
        self.assertFalse(ap.settings_of({"autopilot": {"chain_capture": {"enabled": False}}})["chain_capture"]["enabled"])

    def test_starts_at_the_pre_open_with_a_polite_command(self):
        self.assertIsNone(self.job.tick(at("08:55"), SESS))           # before the pre-open: nothing
        words = self.job.tick(at("09:00"), SESS)
        self.assertIn("chain capture started", words)
        argv = self.proc.started[0]
        self.assertIn("capture-chains", argv)
        self.assertEqual(argv[argv.index("--symbols") + 1], "NIFTY,BANKNIFTY")
        self.assertEqual(argv[argv.index("--every") + 1], "5")
        self.assertEqual(argv[argv.index("--sessions") + 1], "1")
        self.assertEqual(argv[argv.index("--root") + 1], self.root)
        self.proc.beat("waiting for the session", nxt=at("09:15"))
        self.assertIsNone(self.job.tick(at("09:05"), SESS))           # running, not stale: left alone
        self.assertEqual(len(self.proc.started), 1)
        self.assertIsNone(self.job.tick(at("15:30"), SESS))           # after the close: never started
        self.assertIsNone(self.job.tick(at("09:30"), None))           # a closed day

    def test_heartbeat_staleness(self):
        self.job.tick(at("09:00"), SESS)
        self.proc.beat("capturing", last=at("10:00"), nxt=at("10:05"))
        self.assertFalse(self.job.health(at("10:20"), SESS)["stale"])
        h = self.job.health(at("10:21"), SESS)                         # 21 minutes since the last round
        self.assertTrue(h["stale"])
        self.assertTrue(any("stale" in p for p in h["problems"]))
        self.proc.beat("capturing", last=at("10:00"), nxt=at("10:15"), error="NSE answered 403")   # blocked: 15 min
        self.assertFalse(self.job.health(at("10:25"), SESS)["stale"])
        self.assertTrue(any("403" in p for p in self.job.health(at("10:25"), SESS)["problems"]))
        self.proc.beat("waiting for the session")                       # never captured after the open
        self.assertFalse(self.job.health(at("09:30"), SESS)["stale"])
        self.assertTrue(self.job.health(at("09:36"), SESS)["stale"])

    def test_restart_policy(self):
        self.job.tick(at("09:00"), SESS)
        self.proc.beat("capturing", last=at("10:00"), nxt=at("10:05"))
        words = self.job.tick(at("10:30"), SESS)                        # stale and ours: stopped and restarted
        self.assertIn("restarted", words)
        self.assertEqual(self.proc.killed, [4101])
        self.assertEqual(len(self.proc.started), 2)
        self.assertEqual(self.told[-1][0], "capture_failure")
        self.proc.die()                                                 # it crashes
        self.assertIsNone(self.job.tick(at("10:35"), SESS))           # within restart_gap_min (10): waits
        self.assertIn("restarted", self.job.tick(at("10:40"), SESS))
        self.proc.die()
        self.assertIn("restarted", self.job.tick(at("10:50"), SESS))
        self.proc.die()
        words = self.job.tick(at("11:00"), SESS)                        # 3 restarts used: gives up, says so once
        self.assertIn("given up", words)
        self.assertIsNone(self.job.tick(at("11:30"), SESS))
        self.assertEqual(len(self.proc.started), 4)
        self.assertIn("gave up", " ".join(self.job.health(at("11:30"), SESS)["problems"]))
        self.assertEqual(sum(1 for e in self.told if "given up" in e[1]), 1)
        # the next session starts afresh
        self.assertIn("started", self.job.tick(at("09:00", "2026-11-03"), SESS))

    def test_a_foreign_stale_capture_is_left_alone(self):
        self.proc.spawn([], "")                                         # started by someone else (cron, a terminal)
        self.proc.beat("capturing", last=at("10:00"), nxt=at("10:05"))
        words = self.job.tick(at("10:40"), SESS)
        self.assertIn("left alone", words)
        self.assertEqual(self.proc.killed, [])
        self.assertIsNone(self.job.tick(at("10:45"), SESS))           # said once
        self.assertEqual(self.told[0][0], "capture_failure")

    def test_a_finished_session_is_not_restarted(self):
        self.job.tick(at("09:00"), SESS)
        self.proc.die()
        with open(os.path.join(self.root, "chain", "capture.json"), "w") as fh:
            json.dump({"state": "done", "stopped": stamp(at("15:29"))}, fh)
        self.assertIsNone(self.job.tick(at("15:29"), SESS))

    def test_rows_gaps_and_the_holdout(self):
        rows = self.snap("NIFTY", "0915")
        for hhmm in ("0920", "0925", "0950", "0955"):
            self.snap("NIFTY", hhmm)
        self.snap("NIFTY", "0955", "2026-11-10")
        h = self.job.health(at("10:00"), SESS)
        n = h["today"]["NIFTY"]
        self.assertEqual((n["rounds"], n["snapshots"], n["first"], n["last"], n["latest_rows"]),
                         (5, 6, "0915", "0955", rows))
        self.assertGreater(rows, 10)
        self.assertEqual([(g["symbol"], g["from"], g["to"], g["minutes"]) for g in h["gaps"]],
                         [("NIFTY", "09:25", "09:50", 25)])
        self.assertEqual(h["today"]["BANKNIFTY"]["snapshots"], 0)
        self.assertIn("starts 2026-11-16", h["holdout"]["words"])
        self.assertTrue(h["holdout"]["ok"])
        late = self.job.health(at("10:00", "2026-11-20"), SESS)
        self.assertEqual(late["first_snapshot_day"], DAY)
        self.assertIn("in time for the holdout", late["holdout"]["words"])
        trailing = self.job.health(at("10:20"), SESS)["gaps"]
        self.assertEqual(trailing[-1]["to"], "now")

    def test_no_archive_skill(self):
        job = ap.CaptureJob(self.paths, ap.capture_settings({}), self.root, spawn=self.proc.spawn,
                            alive=self.proc.alive, script=os.path.join(self.dir, "missing.py"))
        self.assertIn("not installed", job.tick(at("09:00"), SESS))
        self.assertEqual(self.proc.started, [])


class InTheLoop(unittest.TestCase):
    """The auto-pilot's step() runs the job on a session day, never in a replay, and reports it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bt-capture-loop-")
        os.makedirs(os.path.join(self.dir, "trader"))
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["autopilot"] = {"strategies": {}}
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def pilot(self, replay=False):
        p = ap.Pilot(self.dir, ap.Market(self.dir, None, None, fetch=False), replay=replay, fetch=False)
        proc = Proc(os.path.join(self.dir, "trader", "archive"))
        p.capture_hooks = {"spawn": proc.spawn, "alive": proc.alive, "terminate": proc.terminate, "force": True}
        return p, proc

    def test_step_starts_the_capture_and_logs_it(self):
        p, proc = self.pilot()
        p.step(at("09:00"))
        self.assertEqual(len(proc.started), 1)
        with open(p.paths.day(DAY, "timeline.jsonl"), encoding="utf-8") as fh:
            words = [json.loads(x)["words"] for x in fh]
        self.assertTrue(any("chain capture started" in w for w in words))
        h = ap.capture_health(self.dir, at("09:05"), hooks={"alive": proc.alive})
        self.assertTrue(h["running"])

    def test_never_in_a_replay(self):
        p, proc = self.pilot(replay=True)
        p.step(at("09:00"))
        self.assertEqual(proc.started, [])

    def test_off_in_the_config(self):
        path = os.path.join(self.dir, "trader", "config.json")
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["autopilot"]["chain_capture"] = {"enabled": False}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        p, proc = self.pilot()
        p.step(at("09:00"))
        self.assertEqual(proc.started, [])


if __name__ == "__main__":
    unittest.main()
