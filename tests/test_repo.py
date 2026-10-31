"""Repo-wide rules: layout, skill metadata, safety text, and copies that must stay in sync."""

import json
import os
import re
import subprocess
import unittest

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT, load  # noqa: E402

SELF = os.path.abspath(__file__)

SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-insider", "trader-options"]
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-insider", "trader-risk", "trader-broker", "trader-alerts"]
ROOT_FILES = {".github", ".gitignore", "CHANGELOG.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md",
              "LICENSE", "NOTICE", "README.md", "SECURITY.md", "docs", "mcp", "tests", "ui"}

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
    "2026-11-24", "2026-12-25",
}


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def ignored(names):
    """The entries git ignores (local scratch, tool workspaces), so checks see only the project."""
    try:
        out = subprocess.run(["git", "check-ignore", "--"] + list(names), cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip().rstrip("/") for line in out.stdout.splitlines() if line.strip()}


def text_files():
    skip = ignored(os.listdir(ROOT))
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")
                   and not (base == ROOT and d in skip)]
        for f in files:
            path = os.path.join(base, f)
            if path == SELF:
                continue  # this file spells out the words it bans
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml", ".yml", ".yaml", ".js", ".css", ".html")) \
                    or f in ("LICENSE", "NOTICE", ".gitignore"):
                yield path


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def scripts():
    for s in SKILLS:
        for f in sorted(os.listdir(os.path.join(ROOT, s))):
            if f.endswith(".py"):
                yield s, f[:-3]


class Layout(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_has_only_the_expected_entries(self):
        names = os.listdir(ROOT)
        root = {d for d in names if d != ".git" and not d.startswith("trader-")} - ignored(names)
        root.discard(".DS_Store")
        self.assertEqual(root, ROOT_FILES)

    def test_each_skill_has_one_script_and_a_test(self):
        for s in SKILLS:
            py = [f for f in os.listdir(os.path.join(ROOT, s)) if f.endswith(".py")]
            self.assertEqual(len(py), 1, s)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "tests", "test_%s.py" % s[7:])), s)


class SkillDocs(unittest.TestCase):
    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_every_script_named_in_its_skill_doc(self):
        for s, name in scripts():
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn(name + ".py", text, s)


class Hygiene(unittest.TestCase):
    def test_no_vendor_or_assistant_product_names(self):
        bad = re.compile("edualc|ciporhtna"[::-1], re.I)  # spelled backwards so this file passes too
        hits = [os.path.relpath(p, ROOT) for p in text_files() if bad.search(read(p))]
        self.assertEqual(hits, [])

    def test_license_keeps_both_copyright_lines(self):
        text = read(os.path.join(ROOT, "LICENSE"))
        self.assertIn("MIT License", text)
        lines = re.findall(r"^Copyright \(c\) .+$", text, re.M)
        # ours first; the inherited MIT notice must stay (the license's one condition)
        self.assertGreaterEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("Copyright (c) 2026 Raj Dave"), lines)

    def test_no_us_market_leftovers_in_skills(self):
        bad = re.compile(r"\bnyse\b|nasdaq|sec\.gov|edgar|alpaca|stooq|finra|pelosi|"
                         r"america/new_york|\bUSD\b|max_\w*_usd", re.I)
        hits = []
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                p = os.path.join(ROOT, s, f)
                if os.path.isfile(p) and bad.search(read(p)):
                    hits.append("%s/%s: %s" % (s, f, sorted(set(m.lower() for m in bad.findall(read(p))))))
        self.assertEqual(hits, [])

    def test_no_secrets_in_tree(self):
        bad = re.compile(r"(api[_-]?key|secret|access[_-]?token)\s*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9]{16,}", re.I)
        hits = [os.path.relpath(p, ROOT) for p in text_files() if bad.search(read(p))]
        self.assertEqual(hits, [])


class InSync(unittest.TestCase):
    """Helpers duplicated per skill (so each folder stands alone) must agree."""

    def test_holiday_tables_match_the_nse_list(self):
        checked = 0
        for s, name in scripts():
            text = read(os.path.join(ROOT, s, name + ".py"))
            if "Diwali" not in text:
                continue
            checked += 1
            dates = set(re.findall(r"2026-\d\d-\d\d", text))
            self.assertTrue(NSE_HOLIDAYS_2026 <= dates, "%s is missing %s" % (s, sorted(NSE_HOLIDAYS_2026 - dates)))
        self.assertGreaterEqual(checked, 3, "expected the calendar in at least the entry, alerts and risk tools")

    def test_inr_formatting_is_identical_everywhere(self):
        cases = [0, 5, 999.5, 1000, 12345.678, 100000, 1234567.891, 123456789, -54321.1]
        expected = None
        found = 0
        for s, name in scripts():
            mod = load(s, name)
            fn = getattr(mod, "inr", None)
            if fn is None:
                continue
            found += 1
            got = [fn(x) for x in cases]
            if expected is None:
                expected = got
                self.assertEqual(fn(123456.5), "₹1,23,456.50", s)
                self.assertEqual(fn(1234567.891), "₹12,34,567.89", s)
            self.assertEqual(got, expected, s)
        self.assertGreaterEqual(found, 5)

    def test_charges_agree_everywhere(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            base = json.load(fh)
        trades = [(1174.30, 1163.30, 83), (692.70, 698.30, 72), (123.45, 120.10, 900), (25210.0, 25500.0, 2)]
        # F&O: premiums for options (one lot of 65), traded value for futures, both sides of the
        # 1 April 2026 STT change, plus an exercised option
        fno = [("OPT", 120.50, 135.25, 65, False), ("OPT", 10.0, 50.0, 65, True), ("FUT", 1174.30, 1180.00, 500, False),
               ("FUT", 25210.0, 25100.0, 65, False)]
        expected, found = None, 0
        for s, name in scripts():
            fn = getattr(load(s, name), "round_trip_charges", None)
            if fn is None:
                continue
            found += 1
            got = []
            for product in ("MIS", "CNC"):
                cfg = dict(base, product=product)
                for e, x, q in trades:
                    r = fn(e, x, q, cfg)
                    got.append({k: round(float(v), 2) for k, v in r.items()})
                    self.assertEqual(fn(e, x, q, cfg, "EQ", "2026-03-31"), r, s)   # EQ has no dated table
            for seg, e, x, q, ex in fno:
                for day in ("2026-03-31", "2026-04-01"):
                    r = fn(e, x, q, base, seg, day, ex)
                    got.append({k: round(float(v), 2) for k, v in r.items()})
            if expected is None:
                expected = got
            self.assertEqual(got, expected, s)
        self.assertGreaterEqual(found, 5)

    def shared_block(self, start, end, at_least):
        blocks = {}
        for s, name in scripts():
            text = read(os.path.join(ROOT, s, name + ".py"))
            i = text.find(start)
            if i < 0:
                continue
            j = text.index(end, i)
            blocks[s] = text[text.index("\n", i):j]       # the header line names the copies; the body must match
        self.assertGreaterEqual(len(blocks), at_least, sorted(blocks))
        self.assertEqual(len(set(blocks.values())), 1, "copies differ: %s" % sorted(blocks))
        return blocks

    def test_dated_market_rules_are_identical_everywhere(self):
        blocks = self.shared_block("# ---------------------------------------------------------------- dated market rules",
                                   "# ---------------------------------------------------------------- end of dated market rules",
                                   7)
        for s in ("trader-entry", "trader-exit", "trader-alerts", "trader-risk", "trader-timemachine",
                  "trader-scanner", "trader-broker"):
            self.assertIn(s, blocks)
        body = next(iter(blocks.values()))
        for date in ("2026-08-03", "2026-09-07", "2026-12-07"):
            self.assertIn('"effective_from": "%s"' % date, body)
        for broker in ("zerodha", "upstox", "dhan", "angel", "groww"):
            self.assertIn('"%s": [' % broker, body)

    def test_fno_charge_tables_are_identical_everywhere(self):
        blocks = self.shared_block("# ---------------------------------------------------------------- F&O charges, dated",
                                   "# ---------------------------------------------------------------- end of F&O charges", 5)
        self.assertEqual(sorted(blocks), sorted(s for s, name in scripts()
                                                 if getattr(load(s, name), "round_trip_charges", None)))

    def test_order_path_safety_blocks_are_identical(self):
        blocks = {}
        for s in ORDER_PATH:
            if s == "trader-insider":
                continue  # read-only disclosures: its own short note
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            m = re.search(r"^## Safety\n(.*?)(?=^## |\Z)", text, re.S | re.M)
            self.assertIsNotNone(m, s)
            blocks[s] = " ".join(m.group(1).split())
        self.assertEqual(len(set(blocks.values())), 1, sorted(blocks))

    def test_example_config_is_valid_json_with_the_india_keys(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k in ("capital", "max_loss_per_trade", "max_daily_loss", "flat_by", "tick_size",
                  "product", "exchange", "charges"):
            self.assertIn(k, cfg)
        self.assertTrue(cfg["paper"])
        self.assertEqual(cfg["exchange"], "NSE")


if __name__ == "__main__":
    unittest.main()
