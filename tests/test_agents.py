"""The agent team (agents/*.md): frontmatter, every reference, the MCP persona prompts, the persona
scorecards, and an offline end-to-end flow scan -> plan -> lab check -> risk -> paper draft."""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "ui"))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import api        # noqa: E402  (ui/api.py)
import toolkit    # noqa: E402  (mcp/toolkit.py)
from test_mcp import Client, WATCH_SPEC, broker_records   # noqa: E402

AGENTS = os.path.join(ROOT, "agents")
FIX = os.path.join(HERE, "fixtures", "agents")
TEAM = ["intraday-trader", "journal-coach", "options-seller", "researcher", "risk-officer", "swing-trader"]
ROLES = {"trader", "research", "risk", "review"}
SECTIONS = ["## Mandate", "## Hard limits", "## Hand-offs", "## Never", "## How you report"]
ORDER_TOOLS = {"paper_order_draft", "paper_order_confirm", "size_trade"}
SKILLS = sorted(d for d in os.listdir(ROOT) if d.startswith("trader-") and os.path.isdir(os.path.join(ROOT, d)))
TOOLS = {t["name"] for t in toolkit.build_tools()}


def files():
    return sorted(f for f in glob.glob(os.path.join(AGENTS, "*.md")) if os.path.basename(f) != "README.md")


def load(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    fields, body = api.parse_frontmatter(text)
    return text, fields, body


def commands(body):
    """Every `python3 trader-x/y.py ...` command line in a persona's body, split like a shell would."""
    return [shlex.split(c) for c in re.findall(r"`(python3 [^`]+)`", body)]


def bracket_tools(body):
    """The MCP tool names in the [`tool` ...] brackets after each routine step."""
    out = set()
    for inside in re.findall(r"\[(`[a-z_]+`[^\]]*)\]", body):
        out.update(re.findall(r"`([a-z][a-z0-9_]*)`", inside))
    return out


_HELP = {}


def help_text(script, sub=None):
    key = (script, sub)
    if key not in _HELP:
        argv = [sys.executable, os.path.join(ROOT, script)] + ([sub] if sub else []) + ["--help"]
        r = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        _HELP[key] = (r.returncode, r.stdout.decode("utf-8", "replace"))
    return _HELP[key]


# ---------------------------------------------------------------- the files themselves

class Frontmatter(unittest.TestCase):
    def test_the_team_is_complete(self):
        self.assertEqual([os.path.basename(f)[:-3] for f in files()], TEAM)
        self.assertTrue(os.path.isfile(os.path.join(AGENTS, "README.md")))
        self.assertEqual([p["name"] for p in api.personas()], TEAM)

    def test_portable_frontmatter(self):
        for f in files():
            text, fields, body = load(f)
            name = os.path.basename(f)[:-3]
            self.assertEqual(fields.get("name"), name, f)
            self.assertGreater(len(fields.get("description", "")), 200, f)
            if fields.get("metadata", {}).get("may-draft") == "paper":
                self.assertIn("Paper only, never a live order.", fields["description"], f)
            self.assertIsInstance(fields.get("tools"), str, f)
            self.assertIsInstance(fields.get("skills"), list, f)
            self.assertIsInstance(fields.get("mcp-tools"), list, f)
            meta = fields.get("metadata")
            self.assertIsInstance(meta, dict, f)
            self.assertIn(meta.get("role"), ROLES, f)
            self.assertTrue(meta.get("title"), f)
            self.assertIn(meta.get("may-draft"), ("paper", "never"), f)
            self.assertTrue(body.lstrip().startswith("# "), f)

    def test_frontmatter_is_plain_yaml(self):
        """Other frameworks parse it with a real YAML parser: no `: ` or ` #` inside a plain value, no value
        starting with a YAML indicator, no tabs."""
        for f in files():
            text = open(f, encoding="utf-8").read()
            head = text.split("---", 2)[1]
            self.assertNotIn("\t", head, f)
            for line in head.strip().splitlines():
                key, _, value = line.strip().partition(":")
                value = value.strip()
                if not value or value[0] in "[\"'":
                    continue
                self.assertNotRegex(value, r": | #", "%s: %s needs quotes in YAML" % (f, key))
                self.assertNotIn(value[0], "&*!|>%@`{-?,", "%s: %s starts with a YAML indicator" % (f, key))

    def test_the_body_has_every_section(self):
        for f in files():
            _, fields, body = load(f)
            for s in SECTIONS:
                self.assertIn("\n" + s + "\n", body, "%s: %s" % (f, s))
            self.assertRegex(body, r"\n## (Daily routine|Weekly routine|Routine|On every ticket)", f)
            self.assertIn("not financial advice", body, f)
            self.assertRegex(body.lower(), r"never (promise|call)", f)


# ---------------------------------------------------------------- every reference exists

class References(unittest.TestCase):
    def test_skills_exist_and_cover_the_commands(self):
        for f in files():
            _, fields, body = load(f)
            for s in fields["skills"]:
                self.assertIn(s, SKILLS, "%s: no skill folder %s" % (f, s))
            used = {c[1].split("/")[0] for c in commands(body)}
            self.assertLessEqual(used, set(fields["skills"]), "%s: commands from skills not in its list" % f)

    def test_mcp_tools_exist_and_cover_the_brackets(self):
        for f in files():
            _, fields, body = load(f)
            listed = set(fields["mcp-tools"])
            self.assertLessEqual(listed, TOOLS, "%s: unknown MCP tools %s" % (f, sorted(listed - TOOLS)))
            used = bracket_tools(body)
            self.assertTrue(used, f)
            self.assertLessEqual(used - {"record", "save", "draft", "persona", "iv_shift", "at"}, listed,
                                 "%s: tools used but not listed" % f)

    def test_every_command_line_parses(self):
        """Each command is run with --help: the script exists, the subcommand exists, every --flag is real."""
        for f in files():
            _, _, body = load(f)
            for argv in commands(body):
                self.assertEqual(argv[0], "python3")
                script = argv[1]
                self.assertTrue(os.path.isfile(os.path.join(ROOT, script)), "%s: %s" % (f, script))
                rest = argv[2:]
                rc, top = help_text(script)
                self.assertEqual(rc, 0, "%s: %s --help" % (f, script))
                sub, text = None, top
                choices = re.search(r"\{([a-z0-9,_-]+)\}", top)
                if choices and rest and not rest[0].startswith("-"):
                    sub = rest[0]
                    self.assertIn(sub, choices.group(1).split(","), "%s: %s has no %s" % (f, script, sub))
                elif rest and re.match(r"^[a-z][a-z-]+$", rest[0]):
                    sub = rest[0]              # a subcommand dispatched by hand (check.py kill, size.py init)
                if sub:
                    rc, text = help_text(script, sub)
                    self.assertEqual(rc, 0, "%s: %s %s --help" % (f, script, sub))
                for flag in [a for a in rest if a.startswith("--")]:
                    self.assertRegex(text, re.escape(flag) + r"\b", "%s: %s %s has no %s"
                                     % (f, script, sub or "", flag))

    def test_lab_rules_named_are_in_the_library(self):
        lib = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(ROOT, "trader-lab", "library", "*.json"))}
        for f in files():
            _, _, body = load(f)
            for name in re.findall(r"trader-lab/lab\.py (?:report|forward|show|run) ([a-z0-9-]+)", body):
                self.assertIn(name, lib, "%s: no lab spec %s" % (f, name))

    def test_strategy_templates_named_exist(self):
        names = {t[0] for t in api.skill("strategy").TEMPLATES}
        for f in files():
            _, _, body = load(f)
            for name in re.findall(r"strategy\.py build ([a-z-]+)", body) + \
                    re.findall(r"`((?:iron|bull|bear|short|long)-[a-z-]+)`", body):
                self.assertIn(name, names, "%s: no template %s" % (f, name))

    def test_config_keys_named_exist(self):
        keys = set(api.skill("broker").DEFAULTS) | set(api.skill("risk").DEFAULTS)
        for f in files():
            _, _, body = load(f)
            for key in re.findall(r"`((?:max|min|no|flat|allow|cooloff)_[a-z0-9_]+)`", body):
                self.assertIn(key, keys, "%s: no config key %s" % (f, key))

    def test_no_persona_can_place_a_live_order(self):
        for f in files():
            text, fields, body = load(f)
            self.assertNotRegex(text, r"ticket\.py place|--live\b", f)
            self.assertNotIn("broker_place", fields["mcp-tools"])
            self.assertIn("live order", body, f)
            if fields["metadata"]["may-draft"] == "never":
                self.assertFalse(ORDER_TOOLS & set(fields["mcp-tools"]), f)
                self.assertNotRegex(body, r"ticket\.py confirm|--orders|size\.py[^`]*--write", f)

    def test_traders_trade_only_lab_evidence(self):
        for name in ("intraday-trader", "swing-trader"):
            _, fields, body = load(os.path.join(AGENTS, name + ".md"))
            self.assertEqual(fields["metadata"].get("lab-verdicts"), "WATCH PROMOTE")
            self.assertIn("--persona %s" % name, body)
        _, fields, body = load(os.path.join(AGENTS, "options-seller.md"))
        self.assertIn("--persona options-seller", body)
        self.assertIn("never `short-strangle`", body)
        _, fields, _ = load(os.path.join(AGENTS, "risk-officer.md"))
        self.assertEqual(fields["metadata"].get("veto"), "final")


# ---------------------------------------------------------------- the persona scorecard

def ctx_for(workdir, now="2026-10-30 16:00"):
    """A ui/api context on a workdir with the clock pinned and no network."""
    return api.Context(workdir, now=now)


class Scorecards(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bt-agents-")
        shutil.copytree(os.path.join(FIX, "trader"), os.path.join(self.d, "trader"))
        self.ctx = ctx_for(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_from_the_fixture_trades_log(self):
        out = api.persona_scorecards(self.ctx)
        by = {c["persona"]: c for c in out["personas"]}
        self.assertEqual(sorted(by), TEAM)
        c = by["intraday-trader"]
        self.assertEqual((c["ideas"], c["closed"], c["wins"], c["losses"]), (4, 4, 2, 2))
        # HDFCBANK 26 Oct: (689.90 - 692.70) x 72 - 20.96 - 31.81 = -254.37
        # ICICIBANK 26 Oct: (1400.10 - 1386.10) x 36 - 20.97 - 32.27 = 450.76
        # HDFCBANK 28 Oct: (684.35 - 678.00) x 73 - 20.80 - 31.98 = 410.77
        # RELIANCE 29 Oct: (1165.00 - 1174.30) x 42 - 20.73 - 31.32 = -442.65
        self.assertEqual(c["net_pnl"], round(-254.37 + 450.76 + 410.77 - 442.65, 2))
        self.assertEqual(c["worst"], -442.65)
        self.assertEqual(c["losing_streak_now"], 1)
        self.assertEqual(c["max_drawdown"], 442.65)
        self.assertEqual(c["profit_factor"], round((450.76 + 410.77) / (254.37 + 442.65), 2))
        kinds = [fl["kind"] for fl in c["flags"]]
        self.assertIn("revenge", kinds)       # ICICIBANK bought 5 minutes after the HDFCBANK stop
        self.assertIn("far too few", c["verdict"])
        self.assertEqual(by["researcher"]["ideas"], 0)
        self.assertEqual(out["unattributed"]["ideas"], 1)     # the TCS delivery buy, no tagged ticket
        self.assertIn("not financial advice", out["disclaimer"])

    def test_one_persona_and_an_unknown_one(self):
        out = api.persona_scorecards(self.ctx, "intraday-trader")
        self.assertEqual([c["persona"] for c in out["personas"]], ["intraday-trader"])
        with self.assertRaises(api.ApiError):
            api.persona_scorecards(self.ctx, "get-rich-trader")

    def test_the_agents_route(self):
        out = api.r_agents(self.ctx, {}, None)
        self.assertEqual([t["name"] for t in out["team"]], TEAM)
        self.assertEqual(out["team"][0]["scorecard"]["closed"], 4)
        self.assertIn("--persona", out["how"])

    def test_an_empty_workdir(self):
        d = tempfile.mkdtemp(prefix="bt-agents-empty-")
        try:
            os.makedirs(os.path.join(d, "trader"))
            out = api.persona_scorecards(ctx_for(d))
            self.assertTrue(all(c["ideas"] == 0 for c in out["personas"]))
            self.assertIn("nothing to judge", out["personas"][0]["verdict"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- the persona tag on proposals

class Tag(unittest.TestCase):
    def test_the_tag_is_a_note_not_part_of_the_order(self):
        broker = api.skill("broker")
        order = {"symbol": "RELIANCE", "side": "BUY", "qty": 42, "order_type": "LIMIT", "price": 1174.3,
                 "plan": {"setup": "orb"}}
        tagged = json.loads(json.dumps(order))
        api.skill("budget").tag_orders([tagged], "intraday-trader")
        self.assertEqual(tagged["plan"]["persona"], "intraday-trader")
        self.assertEqual(broker.fingerprint(tagged), broker.fingerprint(order))
        self.assertEqual(api.skill("risk").fingerprint(tagged), api.skill("risk").fingerprint(order))

    def test_bad_names_are_refused(self):
        for mod in ("budget", "strategy"):
            m = api.skill(mod)
            self.assertEqual(m.persona_name(" Intraday-Trader "), "intraday-trader")
            self.assertIsNone(m.persona_name(""))
            for bad in ("../x", "a b", "x" * 41, "-x", "x--y"):
                with self.assertRaises(ValueError):
                    m.persona_name(bad)

    def test_the_persona_block_is_the_same_in_budget_and_strategy(self):
        def block(path):
            src = open(os.path.join(ROOT, path), encoding="utf-8").read()
            a = src.index("# ---------------------------------------------------------------- persona tag")
            b = src.index("# ---------------------------------------------------------------- end of persona tag")
            return src[a:b]
        self.assertEqual(block("trader-budget/size.py"), block("trader-strategy/strategy.py"))


# ---------------------------------------------------------------- MCP: prompts, the tool, the whole flow

class Mcp(unittest.TestCase):
    def setUp(self):
        self.c = Client()

    def tearDown(self):
        self.c.close()
        self.assertEqual(self.c.bad, [])

    def ok(self, resp):
        self.assertNotIn("error", resp, resp)
        return resp["result"]

    def data(self, name, args=None):
        r = self.ok(self.c.call(name, args or {}))
        self.assertFalse(r["isError"], r["content"][0]["text"][:500])
        return r["structuredContent"]["data"]

    def test_persona_prompts_list_and_get(self):
        names = [p["name"] for p in self.ok(self.c.request("prompts/list"))["prompts"]]
        want = ["persona_" + n.replace("-", "_") for n in TEAM]
        self.assertEqual([n for n in names if n.startswith("persona_")], want)
        for n, name in zip(TEAM, want):
            r = self.ok(self.c.request("prompts/get", {"name": name, "arguments": {"focus": "RELIANCE"}}))
            text = r["messages"][0]["content"]["text"]
            _, _, body = load(os.path.join(AGENTS, n + ".md"))
            self.assertIn(body.strip()[:200], text)
            self.assertIn("## Current context", text)
            self.assertIn("your limits (trader/config.json): capital", text)
            self.assertIn("Strategy Lab verdicts", text)
            self.assertIn("your paper scorecard", text)
            self.assertIn("Today's focus: RELIANCE", text)
            self.assertIn("not financial advice", text)
        self.assertEqual(self.c.request("prompts/get", {"name": "persona_options_seller",
                                                        "arguments": {"symbol": "NIFTY"}})["error"]["code"], -32602)

    def test_persona_scorecards_tool(self):
        d = self.data("persona_scorecards")
        self.assertEqual(sorted(c["persona"] for c in d["personas"]), TEAM)
        d = self.data("persona_scorecards", {"persona": "risk-officer"})
        self.assertEqual(len(d["personas"]), 1)
        self.assertTrue(self.ok(self.c.call("persona_scorecards", {"persona": "nobody"}))["isError"])

    def test_offline_flow_scan_plan_lab_risk_paper(self):
        """What the intraday trader's routine does, end to end on saved data: nothing is placed."""
        before = len(broker_records(self.c.workdir))
        scan = self.data("scan_orb")
        self.assertIn("hits", json.dumps(scan))
        # the lab check: a rule with a WATCH verdict on the saved bars
        folder = os.path.join(self.c.workdir, "trader", "lab", "t-watch")
        os.makedirs(folder)
        with open(os.path.join(folder, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(WATCH_SPEC, fh)
        self.assertEqual(self.data("lab_run", {"strategy": "t-watch"})["verdict"], "WATCH")
        self.assertEqual(self.data("lab_report", {"strategy": "t-watch"})["verdict"], "WATCH")
        ls = self.data("lab_list")
        self.assertIn("t-watch", [r["name"] for r in ls["runs"]])
        # the plan and the tagged paper draft (trader-budget sizes it, trader-risk checks it)
        dr = self.data("lab_forward", {"strategy": "t-watch", "draft": "RELIANCE", "persona": "intraday-trader"})
        self.assertTrue(dr["drafted"]["tickets"])
        with open(os.path.join(self.c.workdir, "trader", "orders.json"), encoding="utf-8") as fh:
            order = json.load(fh)["orders"][0]
        self.assertEqual(order["plan"]["persona"], "intraday-trader")
        self.assertEqual(order["plan"]["setup"], "lab:t-watch")
        with open(os.path.join(self.c.workdir, "trader", "state", "personas.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["drafts"][-1]["persona"], "intraday-trader")
        # the risk officer's check, recorded
        risk = self.data("risk_check", {"record": True})
        self.assertIn(risk["results"][0]["verdict"], ("PASS", "BLOCK"))
        shown = self.data("paper_orders_show")
        self.assertTrue(shown["tickets"])
        # drafted, never placed: no new yes, no fill
        self.assertEqual(len(broker_records(self.c.workdir)), before)
        card = self.data("persona_scorecards", {"persona": "intraday-trader"})["personas"][0]
        self.assertEqual(card["drafted"], 1)
        self.assertEqual(card["placed"], 0)
        self.assertEqual(card["strategies"], [])           # nothing filled yet


if __name__ == "__main__":
    unittest.main()
