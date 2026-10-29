import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-29T16:30:00+05:30"   # the fixtures were fetched at 16:29 IST on Thu 29 Oct 2026
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def item(title, summary="", kind="filing", category=""):
    return {"id": title, "source": "NSE", "kind": kind, "category": category, "ticker": "RELIANCE",
            "title": title, "summary": summary, "link": "", "time": None}


class Feeds(unittest.TestCase):
    def test_yahoo_rss_india(self):
        items = news.parse_rss(raw("yahoo-TCS.xml"), "TCS")
        self.assertEqual(len(items), 9)
        first = items[0]
        self.assertEqual(first["title"], "TCS and Workato Join Forces to Speed Enterprise Integration "
                                         "Modernization with AI")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 26, 13, 0, tzinfo=UTC))
        self.assertEqual(news.fmt_ist(first["time"]), "2026-10-26 18:30 IST")
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_an_empty_yahoo_feed_is_not_an_error(self):
        # Yahoo returned an empty channel for RELIANCE.NS on 2026-10-29
        self.assertEqual(news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE"), [])
        with self.assertRaises(news.NewsError):
            news.parse_rss(b"<html>blocked", "RELIANCE")

    def test_nse_announcements(self):
        items = news.parse_nse_announcements(raw("nse-ann-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(items), 15)
        a = items[0]
        self.assertEqual(a["id"], "nse:106833561")
        self.assertEqual(a["category"], "Other Restructuring")
        # the LODR boilerplate is dropped from the title, kept in the summary
        self.assertEqual(a["title"], "Other Restructuring: Voluntary strike off of Hamleys (Franchising) Limited")
        self.assertIn("Regulation 30", a["summary"])
        self.assertEqual(a["company"], "Reliance Industries Limited")
        self.assertTrue(a["link"].startswith("https://nsearchives.nseindia.com/corporate/"))
        # an_dt "29-Oct-2026 08:55:40" is IST = 03:25:40 UTC
        self.assertEqual(a["time"], datetime.datetime(2026, 10, 29, 3, 25, 40, tzinfo=UTC))
        # nothing left after the boilerplate: NSE's own words are kept
        orders = [i for i in items if i["category"].startswith("Action(s)")][0]
        self.assertIn("Disclosure under Regulation 30", orders["title"])

    def test_nse_rejects_html(self):
        with self.assertRaises(news.NewsError):
            news.parse_nse_announcements(b"<html>Access Denied</html>", "RELIANCE")
        self.assertEqual(news.parse_nse_announcements(b'{"data": []}', "RELIANCE"), [])

    def test_board_meetings_and_upcoming(self):
        items = news.parse_nse_board_meetings(raw("nse-bm-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["meeting_date"], "2026-10-30")
        self.assertEqual(items[0]["time"], datetime.datetime(2026, 10, 27, 13, 50, 14, tzinfo=UTC))
        self.assertTrue(items[0]["title"].startswith("Board meeting on 2026-10-30: Consider and approve the "
                                                     "Half Yearly Unaudited Financial results"))
        tcs = news.parse_nse_board_meetings(raw("nse-bm-TCS.json"), "TCS")
        now = news.parse_time(NOW)
        up = news.upcoming_meetings(items + tcs, now)
        self.assertEqual([(u["ticker"], u["meeting_date"], u["category"]) for u in up],
                         [("RELIANCE", "2026-10-30", "Financial Results/Fund Raising")])
        later = news.parse_time("2026-10-31T09:00")
        self.assertEqual(news.upcoming_meetings(items + tcs, later), [])

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(news.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(news.yahoo_symbol("^NSEI"), "%5ENSEI")


class Tickers(unittest.TestCase):
    def test_space_and_comma_separated(self):
        self.assertEqual(news.split_tickers(["RELIANCE,TCS", "infy"]), ["RELIANCE", "TCS", "INFY"])
        self.assertEqual(news.split_tickers(["RELIANCE, TCS ,HDFCBANK"]), ["RELIANCE", "TCS", "HDFCBANK"])
        self.assertEqual(news.split_tickers(["M&M,BAJAJ-AUTO", "3MINDIA", "sbin.ns", "SBIN", "^nsei"]),
                         ["M&M", "BAJAJ-AUTO", "3MINDIA", "SBIN", "^NSEI"])
        self.assertEqual(news.split_tickers(None), [])
        for bad in ("RELIANCE!", "-TCS", "A" * 21, "^"):
            with self.assertRaises(news.NewsError):
                news.split_tickers([bad])

    def test_comma_list_on_the_command_line_is_two_tickers(self):
        # upstream bug: "--tickers A,B" was looked up as one ticker called "A,B"
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run(["--tickers", "RELIANCE,TCS", "--offline", DIR, "--now", NOW, "--hours", "400",
                                  "--json", "--dry-run", "--no-headlines",
                                  "--portfolio", os.path.join(d, "x"), "--watchlist", os.path.join(d, "x"),
                                  "--plan", os.path.join(d, "x")])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual({i["ticker"] for i in data["items"]}, {"RELIANCE", "TCS"})
        self.assertEqual(sorted(m["ticker"] for m in data["moves"]), ["RELIANCE", "TCS"])
        self.assertEqual(data["notes"], [])

    def test_bad_ticker_exit_code(self):
        code, _, err = run(["--tickers", "RELIANCE,$$$", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("not an NSE symbol", err)


class Tagging(unittest.TestCase):
    CASES = [
        ("results", "Outcome of Board Meeting: financial results for the quarter ended September 30, 2026"),
        ("results", "Infosys Q2FY27 net profit rises 8%"),
        ("board meeting", "Board Meeting Intimation for considering fund raising"),
        ("dividend", "Board recommends final dividend of Rs 10 per share"),
        ("bonus", "Board approves bonus issue in the ratio 1:1"),
        ("split", "Sub-division of equity shares from face value Rs 10 to Rs 2"),
        ("split", "Stock split record date fixed"),
        ("buyback", "Board approves buy-back of equity shares at Rs 4,500"),
        ("rights", "Rights issue opens on November 5; rights entitlement ratio 1:15"),
        ("fund raising", "Board approves raising of funds via QIP up to Rs 5,000 crore"),
        ("fund raising", "Allotment of Non-Convertible Debentures on private placement basis"),
        ("deal", "Completion of acquisition of BBY Services India LLP"),
        ("deal", "Scheme of Arrangement for demerger of the financial services business"),
        ("deal", "Board approves amalgamation of wholly owned subsidiary"),
        ("order win", "Bagging/Receiving of orders/contracts: order worth Rs 2,100 crore from NHAI"),
        ("order win", "L&T wins a contract from Saudi Aramco"),
        ("credit rating", "CRISIL reaffirms AAA credit rating on long-term debt"),
        ("regulatory", "SEBI order in the matter of front running; show-cause notice received"),
        ("regulatory", "Action(s) taken or orders passed: GST demand order received"),
        ("pledge", "Disclosure of creation of pledge / encumbrance by promoter group"),
        ("insider", "Disclosure under SEBI Takeover Regulations: Regulation 29(2)"),
        ("insider", "Closure of trading window under SEBI (Prohibition of Insider Trading) Regulations"),
        ("block deal", "Promoter sells 2% stake via block deal"),
        ("management", "Resignation of Chief Financial Officer; appointment of new CFO"),
        ("management", "Change in Management: cessation of Managing Director"),
        ("auditor", "Resignation of statutory auditor M/s XYZ & Co."),
        ("auditor", "Auditors resign citing lack of information"),
        ("macro", "RBI keeps repo rate unchanged; MPC sees inflation easing"),
        ("macro", "Union Budget 2027: fiscal deficit target cut to 4.4%"),
        ("macro", "FPIs sell Rs 8,000 crore as rupee hits record low"),
    ]

    def test_each_india_tag(self):
        for want, text in self.CASES:
            got = news.tag([item(text)], {"RELIANCE": None})[0]["tags"]
            self.assertIn(want, got, "%r -> %s" % (text, got))

    def test_every_tag_has_a_case(self):
        self.assertEqual({label for label, _ in news.KEYWORDS}, {w for w, _ in self.CASES})

    def test_lodr_boilerplate_is_not_regulatory(self):
        items = news.tag(news.parse_nse_announcements(raw("nse-ann-RELIANCE.json"), "RELIANCE"),
                         {"RELIANCE": None})
        self.assertNotIn("regulatory", items[0]["tags"])         # "Regulation 30 of SEBI (LODR)" alone
        self.assertEqual(items[0]["tags"], ["deal"])              # Other Restructuring
        by_cat = {}
        for i in items:
            by_cat.setdefault(i["category"], i)
        self.assertEqual(by_cat["Credit Rating"]["tags"], ["credit rating"])
        self.assertTrue(by_cat["Credit Rating"]["important"])
        self.assertEqual(by_cat["Trading Window"]["tags"], ["insider"])
        self.assertIn("regulatory", by_cat["Action(s) taken or orders passed"]["tags"])
        self.assertIn("fund raising", by_cat["Allotment of Securities"]["tags"])   # NCD allotment
        self.assertEqual(by_cat["Updates"]["tags"], [])
        self.assertFalse(by_cat["Updates"]["important"])

    def test_tcs_results_dividend_acquisition(self):
        items = news.tag(news.parse_nse_announcements(raw("nse-ann-TCS.json"), "TCS"), {"TCS": None})
        by_cat = {}
        for i in items:
            by_cat.setdefault(i["category"], i)
        self.assertEqual(by_cat["Outcome of Board Meeting"]["tags"], ["results", "board meeting"])
        self.assertIn("dividend", by_cat["Dividend"]["tags"])
        self.assertIn("completion of acquisition", by_cat["General Updates"]["title"])
        self.assertEqual(by_cat["General Updates"]["tags"], ["deal"])

    def test_headlines_never_get_the_bang(self):
        it = news.tag([item("Reliance Q2 results beat estimates", kind="headline")], {"RELIANCE": None})[0]
        self.assertIn("results", it["tags"])
        self.assertFalse(it["important"])

    def test_touches_by_name_and_symbol(self):
        items = [item("Reliance Industries and TCS sign a cloud pact"), item("Buy (NSE: SBIN) on dips")]
        watch = {"RELIANCE": None, "TCS": "Tata Consultancy Services", "SBIN": None, "INFY": "Infosys"}
        news.tag(items, watch)
        self.assertEqual(items[0]["touches"], ["RELIANCE"])
        items[0]["title"] = "Reliance and Tata Consultancy Services sign a cloud pact"
        news.tag(items, watch)
        self.assertEqual(items[0]["touches"], ["RELIANCE", "TCS"])
        self.assertEqual(items[1]["touches"], ["RELIANCE", "SBIN"])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-TCS.xml"), "TCS")
        b = news.parse_rss(raw("yahoo-TCS.xml"), "INFY")
        items = news.dedupe(news.tag(a + b, {"TCS": None, "INFY": None}))
        self.assertEqual(len(items), 9)
        self.assertEqual(items[0]["touches"], ["INFY", "TCS"])


class Time(unittest.TestCase):
    def test_ist_is_utc_plus_0530_all_year(self):
        for month in (1, 4, 7, 10, 12):
            utc = datetime.datetime(2026, month, 15, 3, 45, tzinfo=UTC)
            self.assertEqual(news.to_ist(utc).strftime("%H:%M"), "09:15")
        self.assertEqual(news.fmt_ist(datetime.datetime(2026, 10, 29, 20, 0, tzinfo=UTC)), "2026-10-30 01:30 IST")

    def test_parse_times(self):
        self.assertEqual(news.parse_time("Thu, 29 Oct 2026 09:54:00 +0000").hour, 9)
        self.assertEqual(news.parse_time("2026-10-29T10:15").isoformat(), "2026-10-29T04:45:00+00:00")  # IST
        self.assertEqual(news.parse_time("2026-10-29T04:45:00Z").hour, 4)
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.parse_nse_time("29-Oct-2026 08:55:40").isoformat(), "2026-10-29T03:25:40+00:00")
        self.assertEqual(news.parse_nse_time("2026-10-29 08:55:40").hour, 3)
        self.assertIsNone(news.parse_nse_time("yesterday"))


class Money(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(news.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(news.inr(1167.4), "₹1,167.40")
        self.assertEqual(news.inr(999), "₹999.00")
        self.assertEqual(news.inr(0), "₹0.00")
        self.assertEqual(news.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(news.inr(-1500), "-₹1,500.00")
        self.assertEqual(news.fmt_num(22936.75), "22,936.75")


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "INFY", "--hours", "72", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertTrue(all(t.endswith("+05:30") for t in times))
        # 72 h back from 16:30 IST: 3 RELIANCE filings, 2 RELIANCE board meetings, 1 TCS headline
        self.assertEqual(len(data["items"]), 6)
        self.assertEqual(sorted(i["kind"] for i in data["items"]),
                         ["board_meeting", "board_meeting", "filing", "filing", "filing", "headline"])
        self.assertEqual(data["items"][0]["id"], "nse:106833561")
        self.assertEqual(data["upcoming_board_meetings"][0]["date"], "2026-10-30")
        self.assertEqual(data["notes"], ["NSE announcements for INFY: offline: no file %s"
                                         % os.path.join(DIR, "nse-ann-INFY.json")])
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "INFY", "--hours", "72"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "INFY", "--hours", "72", "--all"] + self.base)
        self.assertNotIn("Nothing new.", out)

    def test_text_output_in_ist(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "24"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("UPCOMING RELIANCE board meeting 2026-10-30: Financial Results/Fund Raising", out)
        self.assertIn("! 2026-10-29 08:55 IST  RELIANCE    NSE           Other Restructuring: Voluntary strike off",
                      out)
        self.assertIn("RELIANCE   ₹1,167.40 +0.81% vs previous close ₹1,158.00 at 15:29 IST", out)
        self.assertIn("Checked 2026-10-29 16:30 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn("MOVING", out)
        self.assertNotIn(chr(0x2014), out)

    def test_hours_window(self):
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "6", "--json", "--dry-run"] + self.base)
        self.assertEqual([i["id"] for i in json.loads(out)["items"]], [])
        self.assertEqual(code, 1)
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "8", "--json", "--dry-run"] + self.base)
        self.assertEqual([i["id"] for i in json.loads(out)["items"]], ["nse:106833561"])  # 08:55 is 7.6 h back

    def test_a_pinned_clock_hides_later_items(self):
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "48", "--json", "--dry-run",
                            "--now", "2026-10-27T20:00"] + self.base[:4])
        ids = [i["id"] for i in json.loads(out)["items"]]
        self.assertNotIn("nse:106833561", ids)       # filed 29 Oct
        self.assertIn("RELIANCE", {i["ticker"] for i in json.loads(out)["items"]})

    def test_moves_and_moving_flag(self):
        m = news.parse_chart(raw("chart-INFY.json"), "INFY")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1146.5, 1123.15, 2.08))
        self.assertEqual(news.fmt_ist(m["time"], False), "15:29 IST")
        self.assertNotIn("MOVING", news.render_moves([m])[0])
        self.assertIn("MOVING", news.render_moves([m], move_pct=2.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "NOSUCH")
        code, out, _ = run(["--tickers", "SBIN", "--hours", "200", "--dry-run", "--no-nse",
                            "--move-pct", "1.5"] + self.base)
        self.assertIn("SBIN       ₹971.90 +1.58% vs previous close ₹956.80 at 15:29 IST  MOVING", out)
        self.assertNotIn("note:", out)

    def test_market_flag_adds_nifty(self):
        code, out, err = run(["--market", "--hours", "2000", "--json", "--dry-run", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        rbi = [i for i in data["items"] if "RBI hikes rates" in i["title"]][0]
        self.assertEqual(rbi["ticker"], "^NSEI")
        self.assertIn("macro", rbi["tags"])
        self.assertEqual(rbi["time"], "2026-10-07T11:45:08+05:30")

    def test_tickers_from_watchlist_plan_and_portfolio(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "TCS", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "RELIANCE"}, fh)
        pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 50000, "positions": {"SBIN": {"qty": 10, "avg_price": 950.0}}}, fh)
        self.assertEqual(sorted(news.watch_from_files(pf, wl, plan)), ["RELIANCE", "SBIN", "TCS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "400", "--no-headlines"] + args)
        self.assertEqual(code, 0, err)
        self.assertEqual({t for i in json.loads(out)["items"] for t in i["touches"]}, {"TCS"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


class FakeResp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (news._urlopen, news._sleep)
        self.calls, self.sleeps = [], []

    def tearDown(self):
        news._urlopen, news._sleep = self.saved

    def fake(self, codes, body=b"[]"):
        def urlopen(req, timeout=None):
            self.calls.append(req)
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
            return FakeResp(body)
        news._urlopen = urlopen
        news._sleep = self.sleeps.append

    def test_nse_headers_and_backoff_on_429(self):
        self.fake([429, 200])
        f = news.Fetcher(pause=0)
        self.assertEqual(f.get(news.NSE_BM % "RELIANCE", "x", nse=True), b"[]")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [news.BACKOFF[0]])
        req = self.calls[0]
        self.assertEqual(req.get_header("User-agent"), news.NSE_UA)
        self.assertEqual(req.get_header("Referer"), "https://www.nseindia.com/")
        self.assertEqual(req.get_header("Accept"), "application/json")

    def test_gives_up_after_the_backoffs(self):
        self.fake([429, 429, 429])
        with self.assertRaises(news.NewsError) as cm:
            news.Fetcher(pause=0).get(news.YAHOO % "TCS.NS", "x")
        self.assertIn("HTTP 429", str(cm.exception))
        self.assertEqual(self.calls[0].get_header("User-agent"), news.YAHOO_UA)
        self.assertIn("region=IN", self.calls[0].full_url)
        self.assertEqual(len(self.calls), 3)

    def test_announcement_url_has_a_date_window(self):
        self.fake([200, 200, 200])
        news.collect(news.Fetcher(pause=0), {"M&M": None}, news.parse_time(NOW), hours=48, headlines=False)
        self.assertEqual(self.calls[0].full_url,
                         "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=M%26M"
                         "&from_date=27-10-2026&to_date=29-10-2026")
        self.assertIn("corporate-board-meetings?index=equities&symbol=M%26M", self.calls[1].full_url)


if __name__ == "__main__":
    unittest.main()
