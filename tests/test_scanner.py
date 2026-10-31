"""trader-scanner tests, offline. Fixtures in tests/fixtures/trader-scanner/, all REAL data
fetched 2026-10-29 at 11:01 UTC (16:31 IST, after the close):

EQUITY_L-sample-2026-10-29.csv  67 rows cut from NSE's EQUITY_L.csv (header as NSE sends it,
                                with leading spaces): 62 EQ, 3 BE, 2 BZ. Includes M&M, BAJAJ-AUTO, 3MINDIA.
daily-v8-2026-10-29.json.gz     {SYMBOL: Yahoo v8 chart, range=3mo interval=1d} for 61 NSE symbols
                                plus ZZZQX (made up: Yahoo's "No data found").
daily-spark-2026-10-29.json.gz  the same 62 symbols' spark answers (3 real requests of 20/20/22,
                                merged into one spark document). Spark drops ZZZQX silently.
spark-3mo-batch1-2026-10-29.json  the first spark request, byte for byte as Yahoo sent it.
index-spark-2026-10-29.json     spark range=5d interval=1d for ^NSEI, ^NSEBANK, ^INDIAVIX.
wall5m-2026-10-29.json.gz       {SYMBOL: v8 chart, range=5d interval=5m} cut to Oct 28 and Oct 29.
spark-5m-2026-10-29.json.gz     spark range=1d interval=5m for the same symbols (closes only).
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, FIX, ROOT  # noqa: E402

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner")
EQL = os.path.join(SDIR, "EQUITY_L-sample-2026-10-29.csv")
DAILY = os.path.join(SDIR, "daily-v8-2026-10-29.json.gz")
DSPARK = os.path.join(SDIR, "daily-spark-2026-10-29.json.gz")
BATCH1 = os.path.join(SDIR, "spark-3mo-batch1-2026-10-29.json")
INDEX = os.path.join(SDIR, "index-spark-2026-10-29.json")
WALL = os.path.join(SDIR, "wall5m-2026-10-29.json.gz")
SPARK5 = os.path.join(SDIR, "spark-5m-2026-10-29.json.gz")
DT = datetime.datetime
D = datetime.date
CR = 10000000


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    utc = scan.ist_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, start="09:15"):
    """A v8-like series of 5-min bars from `start` IST on `day`."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": [c - 0.1 if c is not None else None for c in closes], "o": None,
            "v": [1000] * len(closes), "meta": {}}


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(scan.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(scan.inr(0), "₹0.00")
        self.assertEqual(scan.inr(999.999), "₹1,000.00")
        self.assertEqual(scan.inr(-1234567.891), "-₹12,34,567.89")
        self.assertEqual(scan.inr(100000000), "₹10,00,00,000.00")
        self.assertEqual(scan.group(12345678.9), "1,23,45,678.90")
        self.assertEqual(scan.group(22936.75), "22,936.75")
        self.assertEqual(scan.crore(1e8), "₹10.00 cr")


class Universe(unittest.TestCase):
    def setUp(self):
        with open(EQL, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_eq_only_from_the_real_file(self):
        self.assertTrue(self.text.startswith("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING"))
        kept, counts = scan.build_universe(self.text)
        self.assertEqual(counts, {"rows": 67, "keep": 62, "series BE": 3, "series BZ": 2})
        d = {s: (n, i) for s, n, i in kept}
        self.assertEqual(d["M&M"], ("Mahindra & Mahindra Limited", "INE101A01026"))
        self.assertEqual(d["BAJAJ-AUTO"], ("Bajaj Auto Limited", "INE917I01010"))
        self.assertIn("3MINDIA", d)
        self.assertEqual(d["RELIANCE"], ("Reliance Industries Limited", "INE002A01018"))
        for out in ("3IINFOLTD", "ABAN", "AARNAV", "AGSTRA", "ANKITMETAL"):   # BE and BZ: trade-for-trade
            self.assertNotIn(out, d)
        kept2, _ = scan.build_universe(self.text, ("EQ", "BE"))
        self.assertEqual(len(kept2), 65)

    def test_symbol_rule(self):
        row = {"SYMBOL": "BAD SYM", "SERIES": "EQ"}
        self.assertEqual(scan.classify(row), "not a plain symbol")
        self.assertEqual(scan.classify({"SYMBOL": "M&M", "SERIES": "EQ"}), "keep")
        self.assertEqual(scan.classify({"SYMBOL": "3MINDIA", "SERIES": "EQ"}), "keep")
        self.assertEqual(scan.classify({"SYMBOL": "ABAN", "SERIES": "BE"}), "series BE")
        self.assertEqual(scan.classify({"SYMBOL": "A" * 21, "SERIES": "EQ"}), "not a plain symbol")
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_list("<html>maintenance</html>")


class Time(unittest.TestCase):
    def test_ist_has_no_daylight_saving(self):
        self.assertEqual(scan.parse_now("2026-10-29 10:15"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(scan.parse_now("2026-01-05 10:15"), DT(2026, 1, 5, 4, 45))
        self.assertEqual(scan.parse_now("2026-07-06 09:15 IST"), DT(2026, 7, 6, 3, 45))
        self.assertEqual(scan.parse_now("2026-10-29T04:45:00Z"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-29T10:15:00+05:30"), DT(2026, 10, 29, 4, 45))
        self.assertEqual(scan.ts_to_ist(1793245500), DT(2026, 10, 29, 9, 15))   # the real 09:15 bar
        self.assertEqual(scan.hm(DT(2026, 10, 29, 9, 5)), "09:05")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_calendar(self):
        self.assertEqual(scan.trading_day(D(2026, 10, 29)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 10, 31)), (False, "weekend"))
        self.assertEqual(scan.trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))
        self.assertEqual(scan.trading_day(D(2026, 11, 10)), (False, "NSE holiday: Diwali-Balipratipada"))
        self.assertFalse(scan.trading_day(D(2026, 1, 15))[0])
        self.assertEqual(len(scan.NSE_HOLIDAYS), 20)
        cfg = {"extra_holidays": ["2026-10-30"],
               "special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]}
        self.assertEqual(scan.trading_day(D(2026, 10, 30), cfg), (False, "holiday (extra_holidays in config)"))
        self.assertEqual(scan.session(D(2026, 11, 8)), None)                            # a Sunday
        self.assertEqual(scan.session(D(2026, 11, 8), cfg), (datetime.time(18, 0), datetime.time(19, 0)))
        self.assertEqual(scan.session(D(2026, 10, 29)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertFalse(scan.calendar_known(D(2027, 1, 4)))
        self.assertIn("no NSE holiday list for 2027", scan.calendar_warning(D(2027, 1, 4)))
        self.assertEqual(scan.trading_day(D(2027, 1, 26)), (True, ""))   # unknown year: weekdays open
        self.assertIsNone(scan.calendar_warning(D(2026, 12, 1)))


class Symbols(unittest.TestCase):
    def test_yahoo_symbols_and_urls(self):
        self.assertEqual(scan.to_yahoo("m&m"), "M&M.NS")
        self.assertEqual(scan.to_yahoo("^NSEI"), "^NSEI")
        self.assertEqual(scan.from_yahoo("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")
        self.assertEqual(scan.spark_path(["M&M", "BAJAJ-AUTO", "reliance"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,BAJAJ-AUTO.NS,RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("M&M", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")
        self.assertEqual(scan.spark_path(["^NSEI", "^INDIAVIX"], "5d", "1d"),
                         "/v7/finance/spark?symbols=%5ENSEI,%5EINDIAVIX&range=5d&interval=1d")
        self.assertEqual(scan.norm_symbol("m&m.ns"), "M&M")
        with self.assertRaises(scan.ScanError):
            scan.norm_symbol("RELIANCE;DROP")

    def test_symbol_lists(self):
        d = tempfile.mkdtemp()
        try:
            txt = os.path.join(d, "list.txt")
            with open(txt, "w") as fh:
                fh.write("# my list\nM&M|Mahindra & Mahindra Limited|INE101A01026\nreliance\n\nM&M\n")
            self.assertEqual(scan.symbols_from_file(txt), ["M&M", "RELIANCE", "M&M"])
            js = os.path.join(d, "wl.json")
            with open(js, "w") as fh:
                json.dump({"hits": [{"symbol": "hdfcbank"}, {"symbol": "TCS"}]}, fh)
            self.assertEqual(scan.symbols_from_file(js), ["HDFCBANK", "TCS"])
        finally:
            shutil.rmtree(d)


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        with open(BATCH1, encoding="utf-8") as fh:
            data = scan.parse_spark(fh.read())
        self.assertEqual(len(data), 20)
        self.assertIn("M&M", data)                       # M&M.NS comes back as ours
        r = data["RELIANCE"]
        self.assertEqual(r["meta"]["currency"], "INR")
        self.assertEqual(r["meta"]["exchangeTimezoneName"], "Asia/Kolkata")
        self.assertIsNone(r["h"])                        # spark is closes only
        self.assertIsNone(r["v"])
        self.assertEqual(r["meta"]["regularMarketVolume"], 20750193)
        closes = scan.daily_closes(r, scan.parse_now("2026-10-29 09:00"))
        self.assertEqual(closes[-1], (D(2026, 10, 28), 1158.0))
        closes = scan.daily_closes(r, scan.parse_now("2026-10-29 16:00"))
        self.assertEqual(closes[-1][0], D(2026, 10, 29))

    def test_the_merged_answer_drops_the_unknown_symbol(self):
        data, errors = scan.load_offline(DSPARK)
        self.assertEqual(len(data), 61)
        self.assertNotIn("ZZZQX", data)
        self.assertEqual(errors, {})

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_index_context(self):
        data, _ = scan.load_offline(INDEX)
        ctx = scan.index_context(data, scan.parse_now("2026-10-29 16:00"))
        self.assertEqual([(x["name"], x["last"], x["prev"], x["change_pct"]) for x in ctx],
                         [("NIFTY 50", 22936.75, 22698.85, 1.05), ("BANK NIFTY", 54909.65, 54267.4, 1.18),
                          ("INDIA VIX", 15.12, 16.25, -6.97)])
        self.assertEqual(scan.index_line(ctx),
                         "Index context, 2026-10-29 close vs the close before: NIFTY 50 22,936.75 (+1.05%), "
                         "BANK NIFTY 54,909.65 (+1.18%), INDIA VIX 15.12 (-6.97%).")
        # before the open the same answer gives the day before: today's bar is not done
        pre = scan.index_context(data, scan.parse_now("2026-10-29 09:00"))
        self.assertEqual((pre[0]["date"], pre[0]["last"]), ("2026-10-28", 22698.85))
        self.assertIsNone(scan.index_line([]))


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(BATCH1, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, nse=False):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        batch = [s for s in scan.parse_spark(self.body)]
        syms = batch + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 20)
        self.assertEqual(len(failed), 20)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Closes through Wed Oct 28 2026 (the scan at 09:00 IST on Thu Oct 29), no floors.
    EXPECT = {
        "INFY": ["bull_flag"], "TCS": ["bull_flag"], "HCLTECH": ["bull_flag"], "TECHM": ["bull_flag"],
        "WIPRO": ["bull_flag"],
        "LT": ["breakout_20d", "reclaim_ma20"],   # 3801.30 > 3770.00; 3665.10 under its average, then over 3684.28
        "DRREDDY": ["breakout_20d"],              # 1335.40 > 1310.90
        "21STCENMGM": ["breakout_20d"],           # 52.45 > 50.87, but ₹0.07 cr a day: illiquid
        "M&M": ["reclaim_ma20"],                  # 2858.40 under, 2869.90 over 2858.71
        "APOLLOHOSP": ["reclaim_ma20"], "ADANIPORTS": ["reclaim_ma20"],
        "NESTLEIND": ["oversold"],                # RSI 26.9
        "YESBANK": ["oversold"],                  # RSI 17.9, but ₹15.61: under the ₹50 floor
        "20MICRONS": ["oversold"],                # RSI 28.8, ₹1.61 cr a day: illiquid
        "VEDL": ["oversold_bounce"],              # RSI 29.1 on Tuesday, closed up Wednesday (225.60 -> 233.05)
        "TMPV": ["oversold_bounce"],
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(set(self.data) | set(self.errors))

    def test_every_setup_before_the_open(self):
        self.assertEqual(self.errors, {"ZZZQX": "no data for ZZZQX: No data found, symbol may be delisted"})
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 10, 28))
        self.assertEqual((counts["requested"], counts["fetched"], counts["scanned"], counts["hits"]), (62, 61, 61, 16))
        lt = [h for h in hits if h["symbol"] == "LT"][0]
        self.assertEqual((lt["price"], lt["rsi"], lt["close_date"], lt["volume"]), (3801.3, 56.4, "2026-10-28", 6174490))
        self.assertEqual(round(lt["turnover"] / CR, 2), 744.63)
        self.assertEqual(lt["turnover_basis"], "median 20d")
        closes = [c for _, c in scan.daily_closes(self.data["LT"], scan.parse_now("2026-10-29 09:00"))]
        self.assertEqual(max(closes[-21:-1]), 3770.0)
        self.assertEqual(hits[0]["symbol"], "INFY")                     # most rupees traded first

    def test_liquidity_filter_in_rupees(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        got = {h["symbol"] for h in hits}
        self.assertEqual(got, set(self.EXPECT) - {"YESBANK", "20MICRONS", "21STCENMGM"})
        self.assertEqual(counts["illiquid"], 5)   # YESBANK, IDEA, SUZLON under ₹50; 20MICRONS, 21STCENMGM under ₹10 cr
        self.assertEqual(counts["scanned"], 56)
        # 20MICRONS: median of the last 20 sessions' close x volume
        bars = scan.daily_bars(self.data["20MICRONS"], scan.parse_now("2026-10-29 09:00"))
        self.assertEqual(round(scan.median_turnover(bars) / CR, 2), 1.61)
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 1e7)
        self.assertIn("20MICRONS", {h["symbol"] for h in hits})       # a ₹1 cr floor lets it in

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 15:29"), 50, 1e8)
        self.assertEqual(before, during)
        self.assertEqual(scan.daily_bars(self.data["RELIANCE"], scan.parse_now("2026-10-29 15:29"))[-1],
                         (D(2026, 10, 28), 1158.0, 21866598))
        after = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 15:30"), 50, 1e8)
        self.assertEqual(after[2], D(2026, 10, 29))
        self.assertEqual(len(after[0]), 21)
        self.assertIn("breakout_20d", [h for h in after[0] if h["symbol"] == "KOTAKBANK"][0]["setups"])

    def test_spark_stage_two_gets_real_volumes(self):
        spark, _ = scan.load_offline(DSPARK)
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        now = scan.parse_now("2026-10-29 16:00")
        want, _, _ = scan.scan_daily(self.data, self.syms, now, 50, 1e8)
        hits, counts, _ = scan.scan_daily(spark, self.syms, now, 50, 1e8, confirm)
        # spark's 3mo daily closes skip a session for a few names (TMPV: 63 closes, v8 has 64), so
        # the v8 chart decides: TMPV looks like an oversold bounce on spark, not on the full chart
        self.assertEqual([h["symbol"] for h in hits], [h["symbol"] for h in want])
        self.assertEqual(hits, want)
        self.assertEqual(counts["setup_gone"], 1)
        self.assertIn("TMPV", asked)
        self.assertEqual(len(scan.daily_closes(spark["TMPV"], now)), 63)
        self.assertEqual(len(scan.daily_closes(self.data["TMPV"], now)), 64)
        self.assertEqual(counts["confirmed"], len(asked))
        self.assertEqual(len(asked), 23)                   # only charts that matched a setup on spark
        hits, counts, _ = scan.scan_daily(spark, self.syms, now, 50, 1e8, confirm, max_confirm=5)
        self.assertEqual((counts["confirmed"], len(hits)), (5, 5))
        self.assertEqual(counts["unconfirmed"], 18)
        for h in hits:                                     # the cap picks by last session's turnover
            self.assertIn(h, want)
        # no confirm: the last session's turnover from the spark meta stands in
        hits, _, _ = scan.scan_daily(spark, self.syms, now, 50, 1e8)
        self.assertEqual({h["turnover_basis"] for h in hits}, {"last session"})

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [100.0] * 10, "v": [10 ** 7] * 10,
             "meta": {}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=50, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(14, 45))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_1015_scan_on_oct_29(self):
        hits, counts, failed = self.scan_at("2026-10-29 10:15")
        self.assertEqual(len(self.syms), 62)
        self.assertEqual(counts["scanned"], 58)
        self.assertEqual(counts["illiquid"], 3)                 # YESBANK, IDEA, SUZLON under ₹50
        self.assertEqual(len(hits), 32)
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        # range 09:15-09:25 bars: high 1170.50, low 1156.20; the 09:35 bar closed 1174.30, at 09:40
        self.assertEqual(rel, {"symbol": "RELIANCE", "setups": ["orb"], "price": 1174.3, "at": "09:40",
                               "bar_start": "09:35", "bar_index": 4, "or_high": 1170.5, "or_low": 1156.2,
                               "pct_over": 0.32})
        hdfc = [h for h in hits if h["symbol"] == "HDFCBANK"][0]
        self.assertEqual((hdfc["at"], hdfc["price"], hdfc["or_high"], hdfc["pct_over"]), ("09:35", 682.5, 682.45, 0.01))
        self.assertEqual(hits[0]["symbol"], "ADANIENT")          # sorted by time, then symbol
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))
        self.assertEqual(hits[-1]["symbol"], "GRASIM")
        self.assertEqual(hits[-1]["at"], "10:15")

    def test_the_trigger_bar_counts_only_once_closed(self):
        hits, _, _ = self.scan_at("2026-10-29 09:39", syms=["RELIANCE"])
        self.assertEqual(hits, [])
        hits, _, _ = self.scan_at("2026-10-29 09:40", syms=["RELIANCE"])
        self.assertEqual(hits[0]["price"], 1174.3)
        hits, _, _ = self.scan_at("2026-10-29 14:00", syms=["RELIANCE"])
        self.assertEqual(hits[0]["at"], "09:40")                  # the first close above, not a later one

    def test_another_day_in_the_same_file(self):
        hits, counts, _ = self.scan_at("2026-10-28 10:15")
        self.assertEqual((counts["scanned"], len(hits)), (58, 22))
        self.assertEqual((hits[0]["symbol"], hits[0]["at"]), ("APOLLOHOSP", "09:35"))

    def test_stage_one_is_a_necessary_condition(self):
        day, cut = D(2026, 10, 29), DT(2026, 10, 29, 10, 15)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, day, cut)
            if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_real_spark_closes_then_v8_confirm(self):
        spark, _ = scan.load_offline(SPARK5)
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        day, cut = D(2026, 10, 29), DT(2026, 10, 29, 10, 15)
        hits, counts, failed = scan.scan_orb(spark, self.syms, day, cut, 15, 50, 0, confirm)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), counts["scanned"])
        want = {h["symbol"]: h for h in self.scan_at("2026-10-29 10:15")[0]}
        for h in hits:
            self.assertEqual(h, want[h["symbol"]])
        self.assertIn("RELIANCE", [h["symbol"] for h in hits])
        hits, counts, _ = scan.scan_orb(spark, self.syms, day, cut, 15, 50, 0, confirm, max_confirm=5)
        self.assertEqual(counts["confirmed"], 5)
        self.assertEqual(counts["unconfirmed"], counts["candidates"] - 5)

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark, ["RELIANCE"], day, cut, 15, 50, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["RELIANCE"])

    def test_live_tick_missing_bar_and_turnover_floor(self):
        s = bars_series("2026-10-29", [100, 101, 100.5, 101.5, 101.8, 102])
        s["t"].append(ts("2026-10-29 09:45") + 27)               # the live tick at 09:45:27
        for k in ("c", "h", "l"):
            s[k].append(102.5)
        s["v"].append(5)
        bars = scan.session_bars(s, D(2026, 10, 29), DT(2026, 10, 29, 9, 46))
        self.assertEqual([scan.hm(b["start"]) for b in bars], ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40"])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 10, 29), DT(2026, 10, 29, 9, 46), 15, 50, 0)
        self.assertEqual((hits[0]["at"], hits[0]["price"], hits[0]["or_high"]), ("09:35", 101.5, 101.1))
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 10, 29), DT(2026, 10, 29, 9, 46), 15, 50, 1e7)
        self.assertEqual(counts["illiquid"], 1)                  # ₹6 lakh traded so far, under ₹1 cr
        gap = bars_series("2026-10-29", [100, 101, 100.5, None, 101.8, 102])
        bars = scan.session_bars(gap, D(2026, 10, 29), DT(2026, 10, 29, 9, 46))
        self.assertFalse(scan.usable(bars, D(2026, 10, 29), DT(2026, 10, 29, 9, 46), 3))

    def test_special_session(self):
        cfg = {"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00"}]}
        closes = [100, 100.4, 100.2, 100.9, 101, 100.7]
        s = bars_series("2026-11-08", closes, start="18:00")
        day, cut = scan.orb_window(scan.parse_now("2026-11-08 18:30"), None, 15, datetime.time(14, 45), cfg=cfg)
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], day, cut, 15, 50, 0, cfg=cfg)
        self.assertEqual((hits[0]["at"], hits[0]["bar_start"]), ("18:20", "18:15"))
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(scan.parse_now("2026-11-08 18:30"), None, 15, datetime.time(14, 45))
        self.assertIn("weekend", str(cm.exception))

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-31 10:15"))
        self.assertIn("NSE holiday: Dussehra", msg("2026-10-20 10:15"))
        m = msg("2026-10-29 09:05")
        self.assertIn("Run orb again after 09:35", m)
        self.assertIn("pre-open call auction runs 09:00 to 09:10", m)   # revised from 2026-09-07
        self.assertIn("Run orb again after 09:35", msg("2026-10-29 09:34"))
        self.assertIn("expired", msg("2026-10-29 14:50"))
        self.assertIn("expired", msg("2026-10-29 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 15:30", msg("2026-10-29 15:45"))
        self.assertIn("later than now", msg("2026-10-29 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 15:30 close", msg("2026-10-29 17:00", at=datetime.time(15, 40)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 16:30"), datetime.time(10, 15), 15, until),
                         (D(2026, 10, 29), DT(2026, 10, 29, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 09:35"), None, 15, until)[1],
                         DT(2026, 10, 29, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-29 10:15"), None, 7, until)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.cfg = os.path.join(self.t, "config.json")   # not there: defaults

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", EQL, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 62 NSE equities in series EQ", text)
        self.assertIn("Dropped: 3 series BE, 2 series BZ.", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 62)
        self.assertIn("M&M|Mahindra & Mahindra Limited|INE101A01026", lines)

        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-10-29 09:00",
                             "--out", self.wl, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 61 of 62 charts (1 failed or had no data). 56 passed the liquidity filter "
                      "(₹50.00+ and a median daily turnover of ₹10.00 cr+); 56 scanned.", text)
        self.assertIn("  LT             ₹3,801.30  RSI  56.4  ₹744.63 cr  20-day closing-high breakout, "
                      "reclaimed the 20-day average", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["as_of"]),
                         ("daily", 62, 56, 1, "2026-10-28 close"))   # 3MINDIA is in the universe, not the file
        self.assertEqual(doc["filters"], {"min_price": 50.0, "min_turnover": 100000000.0})
        self.assertEqual(len(doc["hits"]), 13)
        self.assertNotIn(".NS", json.dumps(doc["hits"]))

        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 10:15", "--out", self.wl,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("32 of 58 charts closed above their opening-range high by 10:15 IST", text)
        self.assertIn("  09:40  RELIANCE       ₹1,174.30  range 1,156.20 to 1,170.50  +0.32% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source", "exchange")},
                         {"created_by": "trader-scanner", "created_at": "2026-10-29T04:45:00Z", "mode": "orb",
                          "as_of": "2026-10-29 10:15 IST", "universe": 62, "scanned": 58, "source": "yahoo",
                          "exchange": "NSE"})
        self.assertEqual(len(doc["hits"]), 32)

        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:15", "--watchlist", self.wl, "--json",
                             "--no-write", "--config", self.cfg])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (32, 32, 32))

    def test_daily_with_the_index_line(self):
        folder = os.path.join(self.d, "mix")
        os.makedirs(folder)
        shutil.copy(DAILY, os.path.join(folder, "daily.json.gz"))
        shutil.copy(INDEX, os.path.join(folder, "index.json"))
        code, text, _ = run(["daily", "--offline", folder, "--now", "2026-10-29 16:00", "--no-write",
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Index context, 2026-10-29 close vs the close before: NIFTY 50 22,936.75 (+1.05%)", text)
        self.assertIn("Fetched 61 of 62 charts", text)            # the indices are not scanned
        code, text, _ = run(["daily", "--offline", folder, "--now", "2026-10-29 16:00", "--no-write", "--json",
                             "--config", self.cfg])
        self.assertEqual([x["symbol"] for x in json.loads(text)["index"]], ["^NSEI", "^NSEBANK", "^INDIAVIX"])

    def test_networked_daily_with_a_fake_yahoo(self):
        spark = json.loads(scan._read_maybe_gz(DSPARK))["spark"]["result"]
        bundle = json.loads(scan._read_maybe_gz(DAILY))
        with open(INDEX, encoding="utf-8") as fh:
            index = fh.read()
        by = {x["symbol"]: x for x in spark}
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                ys = [scan.urllib.parse.unquote(y) for y in path.split("symbols=")[1].split("&")[0].split(",")]
                if ys[0].startswith("^"):
                    return index
                return json.dumps({"spark": {"result": [by[y] for y in ys if y in by], "error": None}})
            sym = scan.from_yahoo(scan.urllib.parse.unquote(path.split("/chart/")[1].split("?")[0]))
            return json.dumps(bundle[sym])
        old_get, old_sleep = scan.yahoo_get, scan.SLEEP
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        try:
            code, text, _ = run(["daily", "--symbols", ",".join(sorted(bundle)), "--now", "2026-10-29 16:00",
                                 "--json", "--no-write", "--config", self.cfg, "--max-confirm", "10"])
        finally:
            scan.yahoo_get, scan.SLEEP = old_get, old_sleep
        out = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 5)   # 62 symbols: 4 requests + the indices
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 10)
        self.assertTrue(all(c.endswith(".NS?range=3mo&interval=1d") for c in calls if c.startswith("/v8/")))
        self.assertTrue(any("M%26M.NS" in c for c in calls if c.startswith("/v7/")))
        self.assertEqual(len(out["hits"]), 10)
        self.assertEqual(out["unconfirmed"], 13)
        self.assertEqual(out["index"][2]["name"], "INDIA VIX")

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-31 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:20", "--out", self.wl])
        self.assertIn("after 09:35", err)
        os.makedirs(self.t)
        with open(self.cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 10:15", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:38", "--symbols", "RELIANCE",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 09:38 IST", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)

    def test_config_defaults_and_overrides(self):
        self.assertEqual(scan.read_config(os.path.join(self.d, "missing.json")), scan.DEFAULTS)
        os.makedirs(self.t)
        with open(self.cfg, "w") as fh:
            json.dump({"capital": 100000, "min_turnover": 10000000}, fh)   # only one of this tool's keys
        cfg = scan.read_config(self.cfg)
        self.assertEqual((cfg["min_price"], cfg["min_turnover"], cfg["no_entries_after"]), (50.0, 10000000, "14:45"))
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-29 09:00", "--json", "--no-write",
                             "--config", self.cfg])
        self.assertIn("20MICRONS", [h["symbol"] for h in json.loads(text)["hits"]])
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-29 09:00", "--json", "--no-write",
                             "--config", self.cfg, "--min-price", "10", "--min-turnover", "0"])
        self.assertEqual(len(json.loads(text)["hits"]), 16)
        with open(self.cfg, "w") as fh:
            fh.write("{nope")
        code, _, err = run(["daily", "--offline", DAILY, "--config", self.cfg])
        self.assertIn("not valid JSON", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("RELIANCE", "HDFCBANK", "M&M", "ZZZQX"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-10-29 10:15", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["HDFCBANK", "RELIANCE"])
        self.assertEqual(code, 0)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-scanner", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-scanner\n"))
        desc = text.split("description:", 1)[1].split("argument-hint:", 1)[0]
        self.assertGreater(len(desc), 200)
        low = text.lower()
        for must in ("nse", "ist", "₹", "nifty", "mis", "equity_l.csv", "09:15", "not financial advice"):
            self.assertIn(must, low)
        for never in ("ny" + "se", "nas" + "daq", "$" + "5", "new " + "york"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
