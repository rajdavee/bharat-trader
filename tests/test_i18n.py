"""Hinglish output (--lang hi-en): every skill's message catalogue is complete in both languages, the
placeholders match, the safety lines keep their meaning, and three golden outputs (a ticket, a risk BLOCK and the
co-trader's sit-out answer) stay exactly as reviewed.

Golden files: tests/fixtures/i18n/*.txt, made from the same real-data fixtures the skills' own tests use
(trader-risk/orders-reliance.json, the RELIANCE order of 2026-10-29; the lab's NSE daily bars). Regenerate them
after a reviewed wording change with BT_GOLDEN=write python3 -m unittest tests.test_i18n.
"""

import io
import json
import os
import re
import shutil
import string
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load, ROOT, FIX  # noqa: E402

# every script with a message catalogue, and the safety keys whose meaning must survive translation
CATALOGUES = {
    ("trader-broker", "ticket"): ("reply_paper", "reply_live", "footer", "live_wait", "live_place"),
    ("trader-risk", "check"): ("blocked_next", "all_passed", "disclaimer", "rule_kill", "rule_loss"),
    ("trader-alerts", "alerts"): ("after_ring",),
    ("trader-cotrader", "cotrader"): ("footer", "disclaimer", "ne_headline", "d_past", "d_research"),
    ("trader-autopilot", "autopilot"): ("disclaimer", "dns"),
    ("trader-exit", "exit"): ("gap", "never_move", "next"),
    ("trader-budget", "size"): ("stop_promise", "proposed", "next", "disclaimer"),
    ("trader-journal", "journal"): ("disclaimer", "dns_future", "coach_one"),
}
GOLDEN = os.path.join(FIX, "i18n")
CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
PORTFOLIO_PATH = os.path.join(FIX, "trader-risk", "portfolio.json")
PRICES_PATH = os.path.join(FIX, "trader-risk", "prices-reliance.json")
# words an honest tool never uses about odds or risk, in either language
OVERSELL = re.compile(r"(?<!not a )(?<!no )guarantee[ds]?\b(?! nahi)|sure[- ]shot|risk[- ]free|pakka|\b100% (?:safe|sure)", re.I)


def fields(text):
    return sorted({f for _, f, _, _ in string.Formatter().parse(text) if f is not None})


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def golden(test, name, text):
    path = os.path.join(GOLDEN, name)
    if os.environ.get("BT_GOLDEN") == "write":
        os.makedirs(GOLDEN, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(path, encoding="utf-8") as fh:
        test.assertEqual(text, fh.read(), "%s changed: review it, then BT_GOLDEN=write" % name)


class Catalogues(unittest.TestCase):
    def test_every_key_in_both_languages_with_the_same_placeholders(self):
        for (folder, name), safety in CATALOGUES.items():
            mod = load(folder, name)
            self.assertEqual(sorted(mod.MSG), ["en", "hi-en"], folder)
            en, hi = mod.MSG["en"], mod.MSG["hi-en"]
            self.assertEqual(sorted(en), sorted(hi), "%s: keys missing in one language: %s" % (
                folder, sorted(set(en) ^ set(hi))))
            for k in en:
                self.assertEqual(fields(en[k]), fields(hi[k]), "%s %s: placeholders differ" % (folder, k))
                self.assertNotIn("lang", fields(en[k]), "%s %s: 'lang' is t()'s own argument" % (folder, k))
                for lang in ("en", "hi-en"):
                    filled = mod.t(k, lang, **{f: "X" for f in fields(en[k])})     # formats without an error
                    self.assertNotRegex(filled, OVERSELL, "%s %s %s" % (folder, lang, k))
            for k in safety:
                self.assertIn(k, en, "%s: safety key %s" % (folder, k))
                self.assertNotEqual(en[k], hi[k], "%s %s: not translated" % (folder, k))

    def test_safety_lines_keep_their_meaning(self):
        risk = load("trader-risk", "check").MSG["hi-en"]
        broker = load("trader-broker", "ticket").MSG["hi-en"]
        cot = load("trader-cotrader", "cotrader").MSG["hi-en"]
        for mod in CATALOGUES:
            for k, v in load(*mod).MSG["hi-en"].items():
                if k == "disclaimer" or "advice" in load(*mod).MSG["en"][k]:
                    self.assertIn("financial advice nahi", v, (mod, k))
        # one order at a time, the English word yes, nothing sent without it
        self.assertIn("\"yes\"", broker["reply_paper"])
        self.assertIn("EK order", broker["reply_paper"])
        self.assertIn("asli paisa", broker["reply_live"])
        self.assertIn("sirf ticket.py place se", broker["reply_live"])
        self.assertIn("poore batch ke liye kabhi nahi", broker["footer"])
        self.assertIn("kuch bhi bheja nahi gaya", broker["live_wait"].lower())
        # a block never reaches the broker; a pass still waits for the yes
        self.assertIn("broker tak nahi", risk["blocked_next"])
        self.assertIn("yes ya no", risk["all_passed"])
        self.assertIn("typed yes", cot["footer"])
        self.assertIn("live order ke liye nahi", cot["footer"])
        self.assertIn("yeh nahi ki yeh trade bhi", cot["d_past"])

    def test_pick_lang(self):
        size = load("trader-budget", "size")
        self.assertEqual(size.pick_lang(None, None), "en")
        self.assertEqual(size.pick_lang(None, {"lang": "hi-en"}), "hi-en")
        self.assertEqual(size.pick_lang("en", {"lang": "hi-en"}), "en")           # the flag wins
        self.assertEqual(size.pick_lang(None, {"lang": "Hinglish"}), "hi-en")
        self.assertEqual(size.pick_lang(None, {"lang": "hi_EN"}), "hi-en")
        self.assertEqual(size.pick_lang("fr", {"lang": "de"}), "en")              # unknown: English
        self.assertEqual(size.t("disclaimer", "xx"), size.MSG["en"]["disclaimer"])

    def test_english_output_is_unchanged(self):
        size = load("trader-budget", "size")
        self.assertEqual(size.t("refuse_min", "en", shares="1 share", sym="SBIN", value="₹400.00", min="₹500.00"),
                         "1 share of SBIN is ₹400.00, under your ₹500.00 minimum order. No order.")


class Golden(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp(prefix="bt-i18n-")
        os.chdir(self.dir)
        os.makedirs("trader")
        with open(CFG_PATH, encoding="utf-8") as fh:
            self.cfg = json.load(fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_cfg(self, **over):
        c = dict(self.cfg, **over)
        with open("trader/config.json", "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def risk_argv(self, now):
        return ["--orders", "trader/orders.json", "--config", "trader/config.json", "--portfolio", PORTFOLIO_PATH,
                "--prices", PRICES_PATH, "--state", "trader/state/broker.json", "--now", now, "--offline"]

    def test_ticket_in_hinglish(self):
        check, ticket = load("trader-risk", "check"), load("trader-broker", "ticket")
        self.write_cfg(lang="hi-en")
        shutil.copy(ORDERS_PATH, "trader/orders.json")
        self.assertEqual(run(check, self.risk_argv("2026-10-29 09:40"))[0], 0)
        code, text, err = run(ticket, ["show", "--now", "2026-10-29 09:41"])
        self.assertEqual(code, 0, err)
        golden(self, "ticket-hi-en.txt", text)
        self.assertIn("PAPER account (asli paisa nahi)", text)
        self.assertIn("₹514.11", text)                                      # numbers and ₹ unchanged
        # the English flag still wins over the config, word for word as before
        code, en, _ = run(ticket, ["show", "--now", "2026-10-29 09:41", "--lang", "en"])
        self.assertIn("Reply \"yes\" to place this one order on the paper account", en)
        # the yes is still the English word, checked exactly as before
        code, out, _ = run(ticket, ["confirm", "--index", "1", "--yes-text", "haan", "--now", "2026-10-29 09:41"])
        self.assertEqual(code, 1)
        self.assertIn("Record nahi hua", out)
        code, out, _ = run(ticket, ["confirm", "--index", "1", "--yes-text", "yes", "--now", "2026-10-29 09:41"])
        self.assertEqual(code, 0)
        self.assertIn("PAPER account pe PAPER-20261029-1 ke roop mein place hua", out)

    def test_risk_block_in_hinglish(self):
        check = load("trader-risk", "check")
        self.write_cfg(max_loss_per_trade=300)
        shutil.copy(ORDERS_PATH, "trader/orders.json")
        code, text, _ = run(check, self.risk_argv("2026-10-29 09:40") + ["--no-write", "--lang", "hi-en"])
        self.assertEqual(code, 1)
        golden(self, "risk-block-hi-en.txt", text)
        self.assertIn("- Is trade mein aapke max loss per trade se zyada nuksaan ho sakta hai.", text)
        self.assertIn("(loss: 42 shares x 11.00 risk a share", text)         # the exact reason stays under it
        code, en, _ = run(check, self.risk_argv("2026-10-29 09:40") + ["--no-write"])
        self.assertIn("        - loss: 42 shares", en)                       # English: unchanged


class GoldenCotrader(unittest.TestCase):
    def test_no_edge_in_hinglish(self):
        lab, ct = load("trader-lab", "lab"), load("trader-cotrader", "cotrader")
        data = os.path.join(FIX, "trader-lab")
        wd = tempfile.mkdtemp(prefix="bt-i18n-ct-")
        try:
            # the same run as test_cotrader's NoEdge: the library's RSI(2) rule, rejected by the lab's gates
            now = lab.ist_now("2026-10-30 16:00")
            doc = lab.library()["rsi2-nifty50"]
            src = ct.Sources(wd, data, None, False)
            spec = lab.validate_spec(doc)
            out = os.path.join(wd, "trader", "lab")
            lab.save_run(out, doc, lab.run_and_decide(doc, src.market(spec, now), src.context(now), out, None, now), now)
            code, text, err = run(ct, ["today", "--workdir", wd, "--data", data, "--now", "2026-10-30 16:00",
                                       "--offline", "--no-write", "--lang", "hi-en"])
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        self.assertEqual(code, 0, err)
        text = text.replace(wd, "WORKDIR")
        golden(self, "cotrader-no-edge-hi-en.txt", text)
        self.assertIn("Aaj koi trade nahi: baith kar intezaar karna bhi ek faisla hai, galti nahi.", text)
        self.assertIn("Kya badle to yeh badlega:", text)


if __name__ == "__main__":
    unittest.main()
