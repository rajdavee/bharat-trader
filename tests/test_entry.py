"""trader-entry tests, offline. Fixtures in tests/fixtures/trader-entry/:

RELIANCE-2026-10-29-5m.json  REAL: RELIANCE.NS five-minute bars of Thu Oct 29 2026, cut from a
                             Yahoo v8 range=5d answer fetched 16:31 IST (after the close).
                             74 bars (Yahoo left one out), meta.regularMarketTime 15:29:03 IST.
RELIANCE-2026-10-29-1m.json  REAL: the same day, range=1d interval=1m, as Yahoo sent it (13 null bars).
MM-2026-10-29-5m.json        REAL: M&M.NS, range=1d interval=5m, as Yahoo sent it.
HDFCBANK-2026-10-28-5m.json  REAL: HDFCBANK.NS five-minute bars of Wed Oct 28 2026 (cut from range=5d;
                             regularMarketTime set to that day's 15:30 close).
DEMO-1m-synthetic.json       SYNTHETIC one-minute bars (not market data) from 09:15 IST: a wick above
                             the range high, a close AT the high, a null bar, a live tick.
"""

import copy
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
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, FIX, ROOT  # noqa: E402

entry = load("trader-entry", "entry")

EDIR = os.path.join(FIX, "trader-entry")
REL = os.path.join(EDIR, "RELIANCE-2026-10-29-5m.json")
REL1 = os.path.join(EDIR, "RELIANCE-2026-10-29-1m.json")
MM = os.path.join(EDIR, "MM-2026-10-29-5m.json")
HDFC = os.path.join(EDIR, "HDFCBANK-2026-10-28-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
OPEN_TS = 1793245500       # 2026-10-29 09:15 IST = 03:45 UTC
DT = datetime.datetime
D = datetime.date


def ist(text):
    return entry.parse_now(text)


def doc(path=REL):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, when):
    """The same session moved to start at `when` (naive IST datetime)."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    delta = int((entry.ist_to_utc(when) - DT(1970, 1, 1)).total_seconds()) - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1,170.50 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        for k, cap in (("close", 1170.0), ("open", 1170.0), ("high", 1170.4)):
            if q[k][i] is not None:
                q[k][i] = min(q[k][i], cap)
    return d


def live_cut(d, last_slot, data_time):
    """Bars up to `last_slot` (the bar in progress), Yahoo's last trade at `data_time`
    (IST text), plus Yahoo's live tick at that second, the way a live range=1d answer looks."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    keep = [i for i, t in enumerate(r["timestamp"]) if t <= OPEN_TS + last_slot * 300]
    r["timestamp"] = [r["timestamp"][i] for i in keep]
    q = r["indicators"]["quote"][0]
    for k in q:
        q[k] = [q[k][i] for i in keep]
    tick = int((ist(data_time) - DT(1970, 1, 1)).total_seconds())
    r["meta"]["regularMarketTime"] = tick
    if tick % 300:
        r["timestamp"].append(tick)
        last = q["close"][-1]
        for k in ("open", "high", "low", "close"):
            q[k].append(last)
        q["volume"].append(0)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class Money(unittest.TestCase):
    def test_inr(self):
        self.assertEqual(entry.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(entry.inr(1174.3), "₹1,174.30")
        self.assertEqual(entry.inr(-1234567.891), "-₹12,34,567.89")
        self.assertEqual(entry.inr(0.5), "₹0.50")
        self.assertEqual(entry.group(100000000, 0), "10,00,00,000")


class Reliance(unittest.TestCase):
    """Thu 2026-10-29, real 5-minute bars."""

    def test_triggers_at_0940_on_the_0935_bar(self):
        r = at("2026-10-29 09:40")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1170.5)     # the 09:25 bar's high
        self.assertEqual(r["opening_range"]["low"], 1156.2)      # the 09:15 bar's low
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "09:40")
        self.assertEqual(t["bar_start"], "09:35")
        self.assertEqual(t["bar_index"], 4)
        self.assertEqual(t["price"], 1174.3)
        self.assertEqual(t["close"], 1174.300048828125)
        self.assertEqual(t["pct_over"], 0.32)
        self.assertEqual(r["level"], 1170.5)
        self.assertEqual(r["prev_close"], 1158.0)
        self.assertEqual(r["exchange"], "NSE")

    def test_the_0930_bar_wicked_above_but_closed_under(self):
        r = at("2026-10-29 09:38")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:30")         # high 1171.40, close 1169.50
        self.assertEqual(r["last"]["close"], 1169.5)
        self.assertEqual(r["forming"]["bar_start"], "09:35")
        self.assertIn("₹1.00 under the range high ₹1,170.50", r["reason"])
        self.assertEqual(at("2026-10-29 09:39:59")["status"], "waiting")

    def test_pre_open_and_while_the_range_forms(self):
        r = at("2026-10-29 08:45")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("pre-open order entry 09:00 to 09:10", r["reason"])
        r = at("2026-10-29 09:05")
        self.assertEqual(r["phase"], "pre-open")
        self.assertIn("NOT OPEN YET", "\n".join(entry.describe(r)))
        r = at("2026-10-29 09:27")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertEqual(r["opening_range"]["high"], 1164.8)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-29 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        d = live_cut(doc(), 6, "2026-10-29 09:49:45")             # live: the 09:45 bar in progress
        c = chart(d)
        self.assertEqual(c["bars"][-1]["t"] % 300, 285)            # the 09:49:45 tick is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))
        self.assertEqual(r["now"], "2026-10-29 09:49:45")
        self.assertEqual(r["_bars"][-1]["slot"], 5)                # the 09:40 bar
        self.assertEqual(r["forming"]["bar_start"], "09:45")
        r = entry.evaluate(c, ist("2026-10-29 09:50"))             # closed by the clock; Yahoo is behind
        self.assertEqual(r["_bars"][-1]["slot"], 5)
        self.assertIn("Yahoo's last trade is 09:49:45 IST", r["lag_note"])
        r = entry.evaluate(c, ist("2026-10-29 09:51"))             # a minute on, it counts
        self.assertEqual(r["_bars"][-1]["slot"], 6)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-29 16:05")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "09:40")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)
        self.assertIn("plan to be flat by 15:10", text)

    def test_one_minute_real_bars(self):
        c = entry.parse_chart(json.dumps(doc(REL1)), "RELIANCE")
        self.assertEqual(len(c["bars"]), 362)                      # 375 minutes, 13 null bars dropped
        r = entry.evaluate(c, ist("2026-10-29 10:00"), interval="1m")
        self.assertEqual(r["opening_range"]["bars"], 15)
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (1170.5, 1156.2))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("09:34", 18, 1171.3))

    def test_other_real_charts(self):
        r = entry.evaluate(entry.parse_chart(json.dumps(doc(MM)), "M&M"), ist("2026-10-29 13:30"))
        self.assertEqual(r["symbol"], "M&M")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (2887.5, 2858.8))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_start"]), ("13:00", "12:55"))
        r = entry.evaluate(entry.parse_chart(json.dumps(doc(MM)), "M&M"), ist("2026-10-29 12:00"))
        self.assertIn("₹14.70 under the range high ₹2,887.50", r["reason"])
        r = entry.evaluate(entry.parse_chart(json.dumps(doc(HDFC)), "HDFCBANK"), ist("2026-10-28 10:00"))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"], r["level"]), ("09:45", 678.0, 676.8))


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-29 14:00", min_volume_x=1)
        # Yahoo's 09:15 bar has 0 volume on NSE: the average is over the 09:20 and 09:25 bars
        self.assertEqual(r["opening_range"]["avg_volume"], 368006)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("10:00", 8, 1173.8))
        self.assertEqual(t["volume_x"], 2.26)
        r = at("2026-10-29 09:41", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.74x the range's volume, under your 1.00x", r["reason"])
        self.assertIsNone(at("2026-10-29 14:00", min_volume_x=2.5)["trigger"])

    def test_level_setup(self):
        r = at("2026-10-29 14:00", setup="level", level=1176)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("09:55", 7, 1179.0))
        # a level under the open: the first eligible close is the 09:25 bar's, at 09:30 (no_entries_before)
        r = at("2026-10-29 14:00", setup="level", level=1150)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:30", 2))
        self.assertEqual(at("2026-10-29 14:00", setup="level", level=1185)["status"], "waiting")
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_1445(self):
        d = no_breakout()
        self.assertEqual(at("2026-10-29 10:49", d)["status"], "waiting")
        self.assertEqual(at("2026-10-29 14:44", d)["status"], "waiting")
        r = at("2026-10-29 14:45", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 14:45", r["reason"])
        self.assertIn("₹1,170.50", r["reason"])
        self.assertEqual(at("2026-10-29 15:45", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = live_cut(no_breakout(), 17, "2026-10-29 10:44:40")       # live: the 10:40 bar is the last
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:45"}
        r = at("2026-10-29 10:45:10", d, cfg=cfg)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40 IST", r["lag_note"])
        self.assertEqual(at("2026-10-29 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_window(self):
        r = at("2026-10-29 09:40", cfg={"no_entries_before": "09:30", "no_entries_after": "09:35"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-10-29 10:30", cfg={"no_entries_before": "09:45", "no_entries_after": "14:45"})
        self.assertEqual(r["trigger"]["time"], "09:45")          # the 09:40 bar, first to close at or after 09:45
        self.assertEqual(entry.read_config(None), {"no_entries_before": "09:30", "no_entries_after": "14:45",
                                                   "extra_holidays": [], "special_sessions": []})

    def test_bad_inputs_and_ist_clock(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(ist("2026-10-29 10:15"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(ist("2026-01-05 10:15"), DT(2026, 1, 5, 4, 45))          # no daylight saving
        self.assertEqual(ist("2026-10-29T04:45:00Z"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(ist("2026-10-29 10:15 IST"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(entry.to_ist(entry.utc_of(OPEN_TS)), DT(2026, 10, 29, 9, 15))
        with self.assertRaises(entry.EntryError):
            entry.check_symbol("RELIANCE;X")

    def test_or_minutes_30(self):
        r = at("2026-10-29 14:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1175.7)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("09:50", 1175.9))


class OneMinuteSynthetic(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-29 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:44", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-29 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 09:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:47", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 09:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 09:51 still forming, the 09:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_same_rule_on_other_days(self):
        for day in (D(2026, 1, 5), D(2026, 6, 1), D(2026, 12, 1)):
            d = shift_to(doc(), DT.combine(day, datetime.time(9, 15)))
            r = entry.evaluate(chart(d), ist("%s 09:40" % day))
            self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["bar_index"]), ("triggered", "09:40", 4))
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:38" % day))["status"], "waiting")

    def test_weekend_holiday_and_next_open(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        r = at("2026-10-20 10:30")
        self.assertIn("NSE holiday: Dussehra", r["reason"])
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        self.assertEqual(entry.next_open(D(2026, 11, 7)), "2026-11-09 09:15")     # Sun Nov 8 is closed
        self.assertEqual(entry.next_open(D(2026, 12, 24)), "2026-12-28 09:15")    # Christmas, then the weekend
        self.assertIn("Next open: 2026-11-02 09:15 IST.", "\n".join(entry.describe(at("2026-10-31 10:30"))))

    def test_extra_holidays_and_unknown_year(self):
        cfg = {"extra_holidays": ["2026-10-29"]}
        r = at("2026-10-29 10:30", cfg=cfg)
        self.assertEqual(r["status"], "no-data")
        self.assertIn("extra_holidays", r["reason"])
        self.assertEqual(entry.next_open(D(2026, 10, 28), cfg), "2026-10-30 09:15")
        r = at("2027-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("no NSE holiday list for 2027", "\n".join(entry.describe(r)))
        self.assertTrue(at("2026-12-01 10:30")["calendar_known"])

    def test_special_session_muhurat(self):
        cfg = {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]}
        d = shift_to(doc(), DT(2026, 11, 8, 18, 0))
        r = entry.evaluate(chart(d), ist("2026-11-08 18:30"), cfg=cfg)
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"]),
                         ("18:00", "19:00", "18:15", "18:45"))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("18:25", 4))
        self.assertIn("Special session today 18:00-19:00 IST", "\n".join(entry.describe(r)))
        r = entry.evaluate(chart(d), ist("2026-11-08 17:30"), cfg=cfg)
        self.assertEqual(r["phase"], "pre-open")
        self.assertIn("special session opens at 18:00", r["reason"])
        r = entry.evaluate(chart(d), ist("2026-11-08 18:30"))        # without the config: a closed Sunday
        self.assertEqual(r["status"], "no-data")
        cfg2 = {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00",
                                      "no_entries_after": "18:20"}]}
        self.assertEqual(entry.evaluate(chart(no_breakout(d)), ist("2026-11-08 18:21"), cfg=cfg2)["status"], "expired")

    def test_empty_data_and_yahoo_errors(self):
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-29 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        self.assertEqual(at("2026-10-30 10:15")["status"], "no-data")   # Yahoo sent yesterday's bars only
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZQX")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(REL, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes, body=None):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return body or self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", D(2026, 10, 29), today=D(2026, 10, 29))
        self.assertEqual(len(c["bars"]), 74)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
                                      "?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_m_and_m_is_url_encoded(self):
        with open(MM, encoding="utf-8") as fh:
            self.answers(200, body=fh.read())
        c = entry.get_chart("m&m", "5m", D(2026, 10, 29), today=D(2026, 10, 29))
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=5m"])
        self.assertEqual(c["symbol"], "M&M")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("bajaj-auto", "1d", "5m"))

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", D(2026, 10, 29), today=D(2026, 10, 29))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", D(2026, 10, 29), today=D(2026, 10, 29))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-29 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZQX", "5m", D(2026, 10, 29), today=D(2026, 10, 29))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = D(2026, 10, 29)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(D(2026, 10, 28), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(D(2026, 10, 9), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(D(2026, 9, 7), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(D(2026, 10, 9), today, "1m")


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1,171.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1171.00, max(b["h"], 1171.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1171.0, "h": 1171.0, "l": 1171.0, "c": 1171.0, "v": 0})
        return {"symbol": "RELIANCE", "meta": {}, "bars": bars, "prev_close": 1158.0, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, opts=None, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(opts or self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def times(self, calls):
        return [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-29 09:32", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "09:40")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1174.3)
        # it looked at 09:32 (the 09:30 bar spiking to 1,171 in progress) and at 09:35:02 (closed 1,169.50)
        self.assertEqual(self.times(calls), ["09:32:00", "09:35:02", "09:40:02"])
        text = "\n".join(lines)
        self.assertIn("09:30 bar  closed ₹1,169.50  ₹1.00 under ₹1,170.50", text)
        self.assertIn("09:35 bar  closed ₹1,174.30  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        calls = []
        code, res, lines, fired, clock = self.loop(
            live_provider(doc(), lag_until=ist("2026-10-29 09:40:30"), calls=calls), "2026-10-29 09:37", every=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.times(calls), ["09:37:00", "09:40:02", "09:40:32"])   # 09:40:02: Yahoo at 09:39:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "09:40")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-29 08:55", every=30)
        self.assertEqual(code, 0)
        self.assertIn("not open yet (pre-open order entry 09:00 to 09:10)", lines[0])
        text = "\n".join(lines)
        self.assertIn("09:25 bar  closed ₹1,169.00  opening range set: high ₹1,170.50, low ₹1,156.20", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 5)    # 09:15 ... 09:35, one line each

    def test_a_computer_that_dozes_off_catches_up_and_says_it_is_late(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-29 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.times(calls)[:2], ["09:31:00", "09:49:30"])   # straight after waking
        text = "\n".join(lines)
        for start in ("09:30", "09:35", "09:40"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "09:40")
        self.assertIn("minutes ago", text)

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-29 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-29 10:30", every=300,
                                                   until=DT(2026, 10, 29, 11, 0))
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        self.assertIn("Next open: 2026-11-02 09:15 IST", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-29 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 09:40 IST at ₹1,174.30: the 09:35 bar (bar 4) closed above the range high "
                      "₹1,170.50 (+0.32%).", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "RELIANCE", "exchange": "NSE", "setup": "orb", "side": "buy",
                "date": "2026-10-29", "status": "triggered", "time": "09:40", "bar_start": "09:35", "bar_index": 4,
                "price": 1174.3, "level": 1170.5, "interval": "5m",
                "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-29T04:10:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:38", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:38", "--write", "e.json"])
        with open("e.json", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming (₹1,174.30 so far, closes 09:40)", text)

    def test_offline_file_read_as_of_its_own_time(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE  NSE  Thu 2026-10-29 15:29 IST", text)
        self.assertIn("data to 15:29:03", text)

    def test_offline_gz_folder_m_and_m_and_mismatch(self):
        os.mkdir("charts")
        with open(REL, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        shutil.copy(MM, os.path.join("charts", "M&M.json"))
        code, text, _ = run(["check", "reliance", "--offline", "charts", "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        code, text, _ = run(["check", "M&M", "--offline", "charts", "--now", "2026-10-29 13:30"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 13:00 IST", text)
        code, _, err = run(["check", "HDFCBANK", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE, not HDFCBANK", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("long side only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "1200"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--setup", "level"])
        self.assertIn("needs --level", err)
        code, _, err = run(["check", "BAD SYMBOL", "--offline", REL])
        self.assertIn("not an NSE symbol", err)

    def test_config_is_read_and_missing_keys_default(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"capital": 100000, "no_entries_after": "09:35"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        cfg = entry.read_config(os.path.join("trader", "config.json"))
        self.assertEqual((cfg["no_entries_before"], cfg["no_entries_after"], cfg["extra_holidays"]),
                         ("09:30", "09:35", []))
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"capital": 100000, "extra_holidays": ["2026-10-29"]}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40"])
        self.assertIn("MARKET CLOSED", text)
        self.assertIn("Next open: 2026-10-30 09:15 IST.", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", REL])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:20", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:35 bar  closed ₹1,174.30  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bar_index"], 4)
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:20", "--until", "09:35",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(HDFC, os.path.join("charts", "HDFCBANK.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"]}, {"symbol": "ZZZQX", "setups": ["orb"]},
                {"symbol": "DEMO", "setups": ["orb"]}, {"symbol": "HDFCBANK", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE     triggered  09:40  ₹1,174.30    ₹1,170.50"), rows[1])
        self.assertIn("ZZZQX        error: no offline file", rows[2])
        self.assertIn("DEMO         error: DEMO has 1m bars", rows[3])
        self.assertTrue(rows[4].startswith("HDFCBANK     no-data"), rows[4])     # the file is Oct 28's
        self.assertIn("1 of 4 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-29 09:38", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        desc = text.split("description:", 1)[1].split("argument-hint:", 1)[0]
        self.assertGreater(len(desc), 200)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "/trader-exit", "long side only", "in the background", "/loop",
                     "nothing was placed", "09:40", "₹1,174.30", "bar 4", "ist", "09:15", "pre-open", "mis",
                     "nifty", "not financial advice", "typed an explicit yes", "paper"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit", "new " + "york", "$" + "3", "ny" + "se"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
