"""The tools, resources and prompts of the bharat-trader MCP server. Standard library only.

Every number comes from a skill script, loaded from its folder through ui/api.py's own loader
(the same modules the local UI uses): either one of the skill's pure functions, or the skill's
own command line run in-process with --json, so the checks a command makes are the checks the
assistant gets. Nothing here reimplements a calculation.

There is no live-order tool. The only order-path writes are the paper broker's own commands,
and paper_order_confirm still needs trader-risk PASS for that exact order (its fingerprint)
plus the user's own typed yes.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DISCLAIMER = "Education and tooling, not financial advice."
PAPER_NOTE = "Paper trading only: this server has no way to place a live order."
URI_PREFIX = "bharat-trader://trader/"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}\.json$")
REGIME_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}\.json$")     # trader/regime/NIFTY.json
JOURNAL_NAME_RE = re.compile(r"^(notes\.json|weekly-\d{4}-W\d{2}\.md)$")  # trader/journal/weekly-2026-W44.md
SUB_RES = {"state": NAME_RE, "regime": REGIME_NAME_RE, "journal": JOURNAL_NAME_RE}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&\-^.]{1,24}$")
FP_RE = re.compile(r"^[0-9a-f]{16}$")


def _load_api():
    path = os.path.join(ROOT, "ui", "api.py")
    spec = importlib.util.spec_from_file_location("bt_mcp_ui_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # one loader and one module cache for every skill: add the skills the UI does not use yet
    mod.SKILLS.update({
        "news": ("trader-news", "news"),
        "exit": ("trader-exit", "exit"),
        "timemachine": ("trader-timemachine", "timemachine"),
        "insider": ("trader-insider", "insider"),
        "analyst": ("trader-analyst", "facts"),
        "lab": ("trader-lab", "lab"),
        "regime": ("trader-regime", "regime"),
        "discover": ("trader-discover", "discover"),
        "cotrader": ("trader-cotrader", "cotrader"),
        "autopilot": ("trader-autopilot", "autopilot"),
        "journal": ("trader-journal", "journal"),
        "tax": ("trader-tax", "tax"),
        "archive": ("trader-archive", "archive"),
    })
    return mod


api = _load_api()
VERSION = api.VERSION


class ToolError(Exception):
    """A tool execution error: the message goes back to the assistant with isError: true."""


class NeedInput(Exception):
    """The tool needs the user's input (elicitation) before it can finish."""

    def __init__(self, key, request, bind):
        Exception.__init__(self, key)
        self.key, self.request, self.bind = key, request, bind


# ---------------------------------------------------------------- schema helpers

def S(type_, desc=None, **kw):
    out = {"type": type_}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def obj(props=None, required=None, extra=False):
    out = {"type": "object", "properties": props or {}}
    if required:
        out["required"] = list(required)
    if not extra:
        out["additionalProperties"] = False
    return out


SYMBOL = S("string", "NSE symbol, e.g. RELIANCE, HDFCBANK, M&M (no .NS)", pattern=SYMBOL_RE.pattern, maxLength=24)
SYMBOLS = S("string", "NSE symbols, comma separated, e.g. RELIANCE,TCS", maxLength=400,
            pattern=r"^[A-Za-z0-9&\-^.,\s]{1,400}$")
HHMM = S("string", "a time in IST, 24-hour HH:MM", pattern=r"^\d{1,2}:\d{2}$")
DATE = S("string", "a date, YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")
PRICE = S("number", "rupees", exclusiveMinimum=0)
PERSONA = S("string", "the agent persona drafting it, e.g. intraday-trader (agents/): tags the proposal for its "
                      "scorecard; changes no size and no check", pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", maxLength=40)
ANY_OBJ = {"type": "object"}
ANY_LIST = {"type": "array"}
LANG = S("string", "the language of the text summaries: en, or hi-en for Hinglish (Roman script). Structured data "
                   "keeps its English keys and values. Default: the config's lang, else en", enum=["en", "hi-en"])

# the few sentences this server writes itself, in each language the skills speak (their own text comes from them)
TEXT = {
    "en": {
        "brief_ready": "{n} candidate(s) passed your risk check; each still needs your typed yes.",
        "brief_none": "No ready candidate: sitting out is the decision today.",
    },
    "hi-en": {
        "brief_ready": "{n} candidate(s) aapke risk check mein pass hue; har ek ko ab bhi aapka typed yes chahiye.",
        "brief_none": "Koi candidate ready nahi: aaj baith kar intezaar karna hi faisla hai.",
    },
}


def say(key, lang="en", **kw):
    text = (TEXT.get(lang) or TEXT["en"]).get(key) or TEXT["en"][key]
    return text.format(**kw) if kw else text


def lang_of(kit, a):
    """The tool's lang argument, else the config's "lang", else English."""
    return api.skill("risk").pick_lang((a or {}).get("lang"), kit.ctx.config())


def envelope_schema(data_schema):
    """Every tool returns the same envelope; `data` is the skill's own answer."""
    return {
        "type": "object",
        "properties": {
            "tool": S("string"),
            "as_of": S("string", "IST time the answer was worked out"),
            "offline": S("boolean", "true when the answer came from saved fixtures, not the network"),
            "paper_only": {"const": True},
            "data": data_schema,
            "notes": {"type": "array", "items": {"type": "string"}},
            "disclaimer": {"const": DISCLAIMER},
        },
        "required": ["tool", "as_of", "offline", "paper_only", "data", "notes", "disclaimer"],
    }


# ---------------------------------------------------------------- the offline bundle

class Offline(object):
    """--offline DIR: saved answers instead of the network.

    DIR/offline.json may map each tool to its own saved file or folder and pin its clock:
        {"now": "...", "ui": "../ui", "trader": "../ui/trader",
         "tools": {"scan_orb": {"offline": "../trader-scanner/wall5m.json.gz", "now": "..."}}}
    "{symbol}" in a path is the tool's symbol. Without an entry a skill gets DIR/<skill folder>
    when that folder exists, else DIR itself.
    """

    def __init__(self, folder):
        self.dir = os.path.abspath(folder) if folder else None
        self.manifest = {}
        if self.dir:
            if not os.path.isdir(self.dir):
                raise ValueError("--offline %s is not a folder" % folder)
            p = os.path.join(self.dir, "offline.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    self.manifest = json.load(fh)

    def __bool__(self):
        return bool(self.dir)

    __nonzero__ = __bool__

    def _rel(self, p):
        return os.path.normpath(os.path.join(self.dir, p))

    def entry(self, tool):
        return (self.manifest.get("tools") or {}).get(tool) or {}

    def path(self, tool, folder, symbol=None):
        if not self.dir:
            return None
        e = self.entry(tool)
        if e.get("offline"):
            p = e["offline"]
            if symbol:
                p = p.replace("{symbol}", symbol.replace("&", ""))
            return self._rel(p)
        sub = os.path.join(self.dir, folder)
        return sub if os.path.isdir(sub) else self.dir

    def now(self, tool):
        return self.entry(tool).get("now") or self.manifest.get("now")

    def ui_dir(self):
        if not self.dir:
            return None
        if self.manifest.get("ui"):
            return self._rel(self.manifest["ui"])
        sub = os.path.join(self.dir, "ui")
        return sub if os.path.isdir(sub) else self.dir

    def trader_dir(self):
        if not self.dir:
            return None
        if self.manifest.get("trader"):
            return self._rel(self.manifest["trader"])
        sub = os.path.join(self.dir, "trader")
        return sub if os.path.isdir(sub) else None


class RateLimit(object):
    """A token bucket: `per_minute` tool calls a minute, bursts up to the same number."""

    def __init__(self, per_minute=30):
        self.rate = max(1, int(per_minute)) / 60.0
        self.cap = float(max(1, int(per_minute)))
        self.tokens = self.cap
        self.at = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        """0 if a call may go now, else the seconds to wait."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.cap, self.tokens + (now - self.at) * self.rate)
            self.at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0
            return (1 - self.tokens) / self.rate


# ---------------------------------------------------------------- the kit

class Call(object):
    """What a tool sees of the request: elicitation, cancellation, logging."""

    def __init__(self, can_elicit=False, elicit_now=None, responses=None, state=None, cancel=None, log=None,
                 progress=None):
        self.can_elicit = can_elicit
        self.progress = progress or (lambda fraction, message: None)   # the share of the work done, 0 to 1
        self._elicit_now = elicit_now          # legacy era: ask the client and wait
        self.responses = responses or {}       # modern era: inputResponses of the retry
        self.state = state                     # modern era: the verified requestState
        self.cancel = cancel or threading.Event()
        self.log = log or (lambda level, data: None)

    def elicit(self, key, message, schema, bind):
        """The user's answer {"action", "content"}, or None when this client cannot ask."""
        if not self.can_elicit:
            return None
        if self._elicit_now is not None:
            return self._elicit_now({"mode": "form", "message": message, "requestedSchema": schema})
        if self.state is not None and self.state.get("bind") == bind and key in self.responses:
            return self.responses[key]
        raise NeedInput(key, {"method": "elicitation/create",
                              "params": {"mode": "form", "message": message, "requestedSchema": schema}}, bind)


class Kit(object):
    def __init__(self, workdir, offline=None, now=None, rate_per_minute=30):
        self.offline = Offline(offline)
        self.now_override = now
        self.workdir = os.path.abspath(workdir)
        ui_now = now or (self.offline.manifest.get("now") if self.offline else None)
        self.ctx = api.Context(self.workdir, offline=self.offline.ui_dir(), now=ui_now)
        self.limit = RateLimit(rate_per_minute)
        self.tools = build_tools()
        self.by_name = {t["name"]: t for t in self.tools}
        self.prompts = build_prompts()
        self.prompt_by_name = {p["name"]: p for p in self.prompts}
        if self.offline:
            for name in api.SKILLS:
                mod = api.skill(name)
                if hasattr(mod, "fetch_fno_stocks"):
                    # offline means no network: the F&O list comes from the config or the cache only
                    def no_network(timeout=20, _m=mod):
                        raise _m.RulesError("offline: the F&O list is not fetched")
                    mod.fetch_fno_stocks = no_network

    # paths: everything a tool writes is under <workdir>/trader/
    def p(self, *parts):
        return self.ctx.path(*parts)

    def now_text(self, tool):
        n = self.now_override or (self.offline.now(tool) if self.offline else None)
        return n

    def now_args(self, tool, flag="--now"):
        n = self.now_text(tool)
        return [flag, n] if n else []

    def today(self, tool):
        n = self.now_text(tool)
        if n:
            return str(n)[:10]
        return self.ctx.today()

    def off_args(self, tool, folder, symbol=None):
        p = self.offline.path(tool, folder, symbol)
        return ["--offline", p] if p else []

    def backup(self, *parts):
        path = self.p(*parts)
        if os.path.exists(path):
            shutil.copyfile(path, path + ".bak")

    def run(self, skill, argv):
        """The skill's own command, in-process, its printing captured. -> (rc, stdout, stderr)"""
        mod = api.skill(skill)
        out, err = io.StringIO(), io.StringIO()
        with self.ctx.cmd_lock:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = mod.main(argv)
                except SystemExit as e:
                    rc = e.code if isinstance(e.code, int) else 2
        self.ctx.forget("quotes")
        return rc, out.getvalue(), err.getvalue()

    def run_json(self, skill, argv):
        rc, out, err = self.run(skill, argv)
        text = out.strip()
        if text[:1] in ("{", "["):
            try:
                return json.loads(text), err.strip()
            except ValueError:
                pass
        msg = err.strip() or text or ("%s stopped with exit code %s" % (skill, rc))
        raise ToolError(_clean(msg))

    # the envelope
    def wrap(self, tool, data, notes=()):
        return {"tool": tool, "as_of": api.ist_text(self.ctx.now_ist()) if not self.now_text(tool)
                else str(self.now_text(tool)), "offline": bool(self.offline), "paper_only": True,
                "data": data, "notes": [n for n in notes if n], "disclaimer": DISCLAIMER}

    def call(self, name, args, call=None):
        t = self.by_name[name]
        call = call or Call()
        try:
            data, notes = t["fn"](self, args, call)
        except (ToolError, NeedInput):
            raise
        except api.ApiError as e:
            raise ToolError(e.message)
        except api.skill_errors() as e:
            raise ToolError(str(e))
        return self.wrap(name, data, notes)

    # resources
    def resources(self):
        base = os.path.join(self.workdir, "trader")
        out = []
        for sub in ("", "state", "regime", "journal"):
            folder = os.path.join(base, sub) if sub else base
            if not os.path.isdir(folder):
                continue
            for f in sorted(os.listdir(folder)):
                p = os.path.join(folder, f)
                if not (SUB_RES.get(sub, NAME_RE).match(f) and os.path.isfile(p)):
                    continue
                rel = (sub + "/" + f) if sub else f
                about = RESOURCE_ABOUT.get(rel) or (("the daily market regime of %s, one record a day "
                                                     "(trader-regime history)" % f[:-5]) if sub == "regime"
                                                    else ("the weekly trade review %s (trader-journal review), the "
                                                          "journal coach's input" % f[7:-3]) if sub == "journal"
                                                    else "trader/%s" % rel)
                out.append({"uri": URI_PREFIX + rel, "name": rel, "title": "trader/" + rel,
                            "description": about, "mimeType": mime_of(f),
                            "size": os.path.getsize(p)})
        return out

    def resource_path(self, uri):
        """The file behind a bharat-trader://trader/ URI, or None (bad URI or not there)."""
        if not isinstance(uri, str) or not uri.startswith(URI_PREFIX):
            return None
        rel = uri[len(URI_PREFIX):]
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] in SUB_RES:
            name = parts[1]
        elif len(parts) == 1:
            name = parts[0]
        else:
            return None
        pattern = SUB_RES[parts[0]] if len(parts) == 2 else NAME_RE
        if not pattern.match(name) or ".." in name:
            return None
        base = os.path.realpath(os.path.join(self.workdir, "trader"))
        p = os.path.realpath(os.path.join(base, *parts))
        if not p.startswith(base + os.sep) or not os.path.isfile(p):
            return None
        return p


def mime_of(name):
    return "text/markdown" if name.endswith(".md") else "application/json"


def _clean(msg):
    """A skill's stderr line without its script prefix."""
    lines = [l for l in msg.strip().splitlines() if l.strip()]
    text = " ".join(lines[-3:])
    return re.sub(r"^[a-z]+(\.py)?: ", "", text)


RESOURCE_ABOUT = {
    "config.json": "your rules: capital, limits, charges, the paper-only switch (trader-budget init)",
    "watchlist.json": "the last scan's hits (trader-scanner)",
    "entry.json": "the last entry trigger (trader-entry)",
    "plan.json": "the trade plan: entry, stop, target, flat-by (trader-exit)",
    "orders.json": "order tickets sized from the plan (trader-budget)",
    "prices.json": "fresh prices for the risk check and paper fills (trader-alerts quote --write)",
    "portfolio.json": "the paper account: cash and positions (trader-broker)",
    "options.json": "the last option chain analysis (trader-options --write)",
    "strategy.json": "the strategy being built (trader-strategy)",
    "journal.json": "your trade journal: every closed trade after charges, the stats and the rule breaks "
                    "(trader-journal sync)",
    "journal/notes.json": "your own notes and tags on journal entries (trader-journal note)",
    "timemachine.json": "the last time-machine replay (trader-timemachine --write)",
    "alerts.json": "alert rules (trader-alerts)",
    "state/risk.json": "trader-risk's recorded verdicts, with each order's fingerprint",
    "state/broker.json": "the paper broker's decisions log: every yes and no, word for word",
    "state/trades.json": "paper fills with charges and realised P&L",
}

RESOURCE_TEMPLATES = [
    {"uriTemplate": URI_PREFIX + "{file}", "name": "trader-file", "title": "A trader/ file",
     "description": "Any JSON file in your trader/ folder, e.g. plan.json, watchlist.json, portfolio.json, "
                    "options.json, strategy.json, journal.json", "mimeType": "application/json"},
    {"uriTemplate": URI_PREFIX + "state/{file}", "name": "trader-state-file", "title": "A trader/state/ file",
     "description": "The tools' own records: risk.json (verdicts), broker.json (decisions), trades.json (fills)",
     "mimeType": "application/json"},
    {"uriTemplate": URI_PREFIX + "regime/{file}", "name": "trader-regime-file", "title": "A trader/regime/ file",
     "description": "The daily market regime of one symbol from trader-regime history, e.g. NIFTY.json: trend, "
                    "volatility bucket, gap, expiry and event flags per day", "mimeType": "application/json"},
    {"uriTemplate": URI_PREFIX + "journal/{file}", "name": "trader-journal-file", "title": "A trader/journal/ file",
     "description": "trader-journal's weekly reviews (weekly-2026-W44.md, markdown) and your notes (notes.json)",
     "mimeType": "text/markdown"},
]


# ---------------------------------------------------------------- argument helpers

def _sym(args, key="symbol", required=True):
    v = args.get(key)
    if v is None:
        if required:
            raise ToolError("%s is required" % key)
        return None
    v = v.strip().upper()
    if v.endswith(".NS"):
        v = v[:-3]
    if not SYMBOL_RE.match(v):
        raise ToolError("%r is not an NSE symbol" % args.get(key))
    return v


def _add(argv, flag, value):
    if value is not None and value is not False:
        argv += [flag] if value is True else [flag, str(value)]
    return argv


def _cfg(kit):
    return ["--config", kit.p("config.json")]


# ---------------------------------------------------------------- tools: market and research

def t_market_status(kit, a, call):
    st = api.market(kit.ctx, verify=bool(a.get("verify")))
    return st, ["NSE equity session per the 2026 holiday calendar and your extra_holidays / special_sessions."]


def _scan_common(kit, a, tool, argv):
    _add(argv, "--symbols", a.get("symbols") and ",".join(s.strip().upper() for s in a["symbols"].split(",")
                                                          if s.strip()))
    _add(argv, "--limit", a.get("limit"))
    _add(argv, "--min-price", a.get("min_price"))
    _add(argv, "--min-turnover", a.get("min_turnover"))
    argv += _cfg(kit) + ["--no-write", "--json", "--quiet"] + kit.off_args(tool, "trader-scanner") \
        + kit.now_args(tool)
    data, err = kit.run_json("scanner", argv)
    return data, [err if err else None, "A scan is a list of charts that match a rule, not a list of stocks to buy."]


def t_scan_daily(kit, a, call):
    return _scan_common(kit, a, "scan_daily", ["daily"])


def t_scan_orb(kit, a, call):
    argv = ["orb"]
    _add(argv, "--at", a.get("at"))
    _add(argv, "--or-minutes", a.get("or_minutes"))
    _add(argv, "--until", a.get("until"))
    return _scan_common(kit, a, "scan_orb", argv)


def t_news(kit, a, call):
    argv = ["--json", "--dry-run", "--state", kit.p("state", "news.json"), "--portfolio", kit.p("portfolio.json"),
            "--watchlist", kit.p("watchlist.json"), "--plan", kit.p("plan.json")]
    if a.get("tickers"):
        argv += ["--tickers", ",".join(s.strip().upper() for s in a["tickers"].split(",") if s.strip())]
    _add(argv, "--hours", a.get("hours"))
    _add(argv, "--market", bool(a.get("market")))
    _add(argv, "--all", bool(a.get("include_seen")))
    argv += kit.off_args("news", "trader-news") + kit.now_args("news")
    data, err = kit.run_json("news", argv)
    return data, [err or None, "Headlines are context, not signals. Check the source before acting."]


def t_entry_check(kit, a, call):
    sym = _sym(a)
    argv = ["check", sym, "--json"] + _cfg(kit)
    _add(argv, "--setup", a.get("setup"))
    _add(argv, "--level", a.get("level"))
    _add(argv, "--or-minutes", a.get("or_minutes"))
    _add(argv, "--interval", a.get("interval"))
    if a.get("save"):
        kit.backup("entry.json")
        argv += ["--write", kit.p("entry.json")]
    argv += kit.off_args("entry_check", "trader-entry", sym) + kit.now_args("entry_check")
    data, err = kit.run_json("entry", argv)
    return data, [err or None, "saved to trader/entry.json" if a.get("save") else None]


def t_exit_plan(kit, a, call):
    argv = ["plan", "--json"] + _cfg(kit)
    sym = _sym(a, required=False)
    if sym:
        if a.get("entry") is None:
            raise ToolError("symbol needs entry (the entry price in rupees)")
        argv += ["--symbol", sym]
    else:
        argv += ["--from", kit.p("entry.json")]
    for key, flag in (("entry", "--entry"), ("or_high", "--or-high"), ("or_low", "--or-low"),
                      ("or_minutes", "--or-minutes"), ("setup", "--setup"), ("date", "--date"), ("time", "--time"),
                      ("interval", "--interval"), ("stop", "--stop"), ("target", "--target"),
                      ("flat_by", "--flat-by"), ("qty", "--qty")):
        _add(argv, flag, a.get(key))
    if a.get("save"):
        kit.backup("plan.json")
        argv += ["--write", kit.p("plan.json")]
    argv += kit.off_args("exit_plan", "trader-exit", sym) + kit.now_args("exit_plan")
    data, err = kit.run_json("exit", argv)
    return data, [err or None, "saved to trader/plan.json (the old one kept as plan.json.bak)" if a.get("save")
                  else "Not saved. Call again with save: true to make it the plan."]


def t_timemachine_run(kit, a, call):
    argv = ["run", "--json"] + _cfg(kit) + ["--replays", str(a.get("replays") or 2000),
                                            "--seed", str(a.get("seed") if a.get("seed") is not None else 19)]
    sym = _sym(a, required=False)
    if sym:
        for k in ("entry", "stop", "target"):
            if a.get(k) is None:
                raise ToolError("a plan given as arguments needs symbol, entry, stop and target")
        argv += ["--symbol", sym]
        for key, flag in (("entry", "--entry"), ("stop", "--stop"), ("target", "--target"), ("date", "--date"),
                          ("entry_time", "--entry-time")):
            _add(argv, flag, a.get(key))
    else:
        argv += ["--plan", kit.p("plan.json")]
    if not sym:
        plan = kit.ctx.read("plan.json") or {}
        sym = str(plan.get("symbol") or "") or None
    argv += kit.off_args("timemachine_run", "trader-timemachine", sym) + kit.now_args("timemachine_run")
    call.log("info", "replaying the plan %s times" % (a.get("replays") or 2000))
    data, err = kit.run_json("timemachine", argv)
    return data, [err or None, "A replay of past sessions is not a forecast."]


# ---------------------------------------------------------------- tools: sizing, risk, paper orders

def _size_argv(kit, a, tool):
    argv = ["--json", "--plan", kit.p("plan.json"), "--portfolio", kit.p("portfolio.json")] + _cfg(kit)
    if os.path.exists(kit.p("prices.json")):
        argv += ["--prices", kit.p("prices.json")]
    _add(argv, "--capital", a.get("capital"))
    _add(argv, "--limit", a.get("limit"))
    _add(argv, "--market", bool(a.get("market")))
    _add(argv, "--persona", a.get("persona"))
    return argv + kit.ctx.now_args()


def t_size_trade(kit, a, call):
    if not os.path.exists(kit.p("plan.json")):
        raise ToolError("no trader/plan.json yet: make one with exit_plan (save: true)")
    data, err = kit.run_json("budget", _size_argv(kit, a, "size_trade"))
    return data, [err or None, "Not written. paper_order_draft writes the order tickets."]


def _risk_record(kit):
    if kit.ctx.read("orders.json") is None:
        raise ToolError("no trader/orders.json to check: make tickets with paper_order_draft")
    argv = ["--orders", kit.p("orders.json"), "--config", kit.p("config.json"), "--portfolio", kit.p("portfolio.json"),
            "--prices", kit.p("prices.json"), "--state", kit.p("state", "broker.json"),
            "--trades", kit.p("state", "trades.json"), "--kill", kit.p("state", "kill.json"),
            "--out", kit.p("state", "risk.json"), "--json"] + (["--offline"] if kit.ctx.offline else []) + \
        kit.ctx.now_args()
    data, err = kit.run_json("risk", argv)
    api.capture_risk(kit.ctx)
    return data, err


def t_risk_check(kit, a, call):
    risk = api.skill("risk")
    if a.get("record"):
        data, err = _risk_record(kit)
        if a.get("lang"):
            data["summary"] = risk.report(data, lang_of(kit, a))
        return data, [err or None, "Recorded in trader/state/risk.json: the paper broker accepts these verdicts today."]
    view = api.r_risk(kit.ctx, {}, None)
    if a.get("lang") and view.get("preview"):
        view["summary"] = risk.report(view["preview"], lang_of(kit, a))
    view.pop("disclaimer", None)
    view.pop("scenarios", None)           # risk_scenarios has the F&O book
    notes = ["A preview, not recorded. Call with record: true to record the verdicts."]
    if (view.get("kill") or {}).get("active"):
        notes.append("The kill switch is ON today: every new entry is blocked; exits still pass. Only the user lifts "
                     "it (check.py unkill, or the local UI, with the typed word unkill).")
    if ((view.get("breakers") or {}).get("locks")):
        notes.append("A circuit-breaker locks new entries: %s" % "; ".join(view["breakers"]["locks"]))
    return view, notes


def t_risk_scenarios(kit, a, call):
    view = api.r_risk(kit.ctx, {}, None)
    sc = view["scenarios"]
    which = a.get("book") or "with_proposals"
    out = {"now_ist": sc["now_ist"], "book": which, "risk": sc.get(which), "limits": view["fno_limits"],
           "kill": view["kill"], "breakers": sc["breakers"], "rate": sc["rate"]}
    return out, [sc["disclaimer"], "Read only: nothing is written. The Greeks and the grid are a Black-76 model at "
                                   "today's IV; P&L is after charges."]


def t_paper_orders_show(kit, a, call):
    return api.tickets(kit.ctx), ["One order at a time: a yes covers one ticket, never the batch."]


def t_paper_order_draft(kit, a, call):
    if a.get("candidate"):
        cid = a["candidate"]
        ct = api.skill("cotrader")
        try:
            path = ct.plan_path(kit.workdir, cid)
        except ct.CotraderError as e:
            raise ToolError(str(e))
        if not os.path.exists(path):
            raise ToolError("no co-trader plan %s: call cotrader_candidates first" % cid)
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
        if plan.get("kind") == "options":
            if plan.get("date") != kit.today("paper_order_draft"):
                raise ToolError("this basket is for %s and today is %s: draft it on its own day"
                                % (plan.get("date"), kit.today("paper_order_draft")))
            kit.backup("orders.json")
            kit.backup("strategy.json")
            ct.write_json(kit.p("strategy.json"), plan["strategy"])
            ct.write_json(kit.p("orders.json"), plan["orders"])
            risk, rerr = _risk_record(kit)
            view = api.tickets(kit.ctx)
            return {"sizing": None, "risk": risk, "tickets": view["tickets"], "basket": view.get("basket"),
                    "next_step": "Show the user the basket (every leg, the margin, the max loss after charges). "
                                 "Only a basket trader-risk passed can be confirmed, with one typed yes for every leg."}, \
                [rerr or None, "Wrote trader/strategy.json and trader/orders.json (old ones kept as .bak) and recorded "
                               "the risk verdicts."]
        kit.backup("plan.json")
        ct.write_json(kit.p("plan.json"), plan)
    if not os.path.exists(kit.p("plan.json")):
        raise ToolError("no trader/plan.json yet: make one with exit_plan (save: true)")
    kit.backup("orders.json")
    data, err = kit.run_json("budget", _size_argv(kit, a, "paper_order_draft") + ["--write", kit.p("orders.json")])
    risk, rerr = _risk_record(kit)
    view = api.tickets(kit.ctx)
    ready = [t for t in view["tickets"] if t.get("ready")]
    step = ("Show the user each READY ticket in plain words (side, qty, symbol, price, stop, target, the loss at the "
            "stop with charges). Only if the user types a yes for that one order, call paper_order_confirm with its "
            "index and fingerprint." if ready else "No ticket passed trader-risk; nothing can be confirmed.")
    return {"sizing": data, "risk": risk, "tickets": view["tickets"], "next_step": step}, \
        [err or None, rerr or None, "Wrote trader/orders.json (the old one kept as orders.json.bak) and recorded "
                                    "the risk verdicts."]


YES_SCHEMA = {
    "type": "object",
    "properties": {
        "yes_text": {"type": "string", "title": "Your reply",
                     "description": "Type yes to place this ONE order on the paper account, e.g. \"yes 42 RELIANCE\". "
                                    "Anything else places nothing.", "minLength": 1, "maxLength": 500},
    },
    "required": ["yes_text"],
}


def t_paper_order_confirm(kit, a, call):
    broker = api.skill("broker")
    broker.paper_only(kit.ctx.config(), "paper_order_confirm")   # a live config stops here, before anything
    idx, fp = a.get("index"), a.get("risk_fingerprint")
    view = api.tickets(kit.ctx)
    tks = view["tickets"]
    if not tks:
        raise ToolError("no order tickets: make them with paper_order_draft")
    if not 1 <= idx <= len(tks):
        raise ToolError("there is no ticket %d (there are %d)" % (idx, len(tks)))
    t = tks[idx - 1]
    if t["fingerprint"] != fp:
        raise ToolError("ticket %d's fingerprint is %s, not %s: the order changed or this is a different ticket. "
                        "Show the user the current ticket (paper_orders_show) first." % (idx, t["fingerprint"], fp))
    risk = kit.ctx.read("state", "risk.json")
    rv, why = broker.risk_verdict(risk, fp, kit.ctx.today())
    if rv is None:
        raise ToolError("not confirmed: %s" % why)
    if rv.get("verdict") != "PASS":
        raise ToolError("not confirmed: trader-risk BLOCKED this order: %s" % "; ".join(rv.get("reasons") or []))
    if not t.get("ready"):
        raise ToolError("not confirmed: %s" % t.get("status"))
    bind = {"index": idx, "fingerprint": fp}
    answer = call.elicit("confirm", "PAPER order %d: %s. Type yes to place this one order on the paper account "
                                    "(no real money), or anything else to skip it. %s"
                         % (idx, t["plain"], DISCLAIMER), YES_SCHEMA, bind)
    via = "argument"
    if answer is not None:
        via = "elicitation"
        if not isinstance(answer, dict) or answer.get("action") != "accept":
            act = answer.get("action") if isinstance(answer, dict) else None
            return {"confirmed": False, "index": idx, "fingerprint": fp, "via": via, "action": act or "cancel",
                    "message": "The user did not accept. Nothing was placed or recorded."}, []
        text = (answer.get("content") or {}).get("yes_text")
    else:
        text = a.get("yes_text")
        if not text:
            raise ToolError("not confirmed: this client cannot ask the user directly, so yes_text must be the "
                            "user's own reply, word for word, after you showed them ticket %d. Never write it "
                            "yourself." % idx)
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ToolError("not confirmed: the reply must be short text")
    argv = ["confirm", "--orders", kit.p("orders.json"), "--config", kit.p("config.json"),
            "--risk", kit.p("state", "risk.json"), "--state", kit.p("state", "broker.json"),
            "--index", str(idx), "--yes-text", text, "--lang", lang_of(kit, a)] + kit.ctx.now_args()
    rc, out, err = kit.run("broker", argv)
    if rc != 0:
        msg = _clean(err or out) if (err or out) else "the paper broker refused"
        if via == "elicitation" and out.strip().startswith(("Not recorded", "Record nahi hua")):
            return {"confirmed": False, "index": idx, "fingerprint": fp, "via": via, "action": "accept",
                    "message": out.strip()}, []
        raise ToolError(msg)
    state = broker.load_state(kit.p("state", "broker.json"))
    recs = broker.records(state, fp=fp, today=kit.ctx.today(), status="placed")
    rec = recs[-1] if recs else {}
    return {"confirmed": True, "index": idx, "fingerprint": fp, "via": via,
            "paper_order_id": rec.get("paper_order_id"), "yes_text": rec.get("yes_text"),
            "plain": rec.get("plain"), "message": out.strip()}, \
        ["Placed on the PAPER account only. A fill is simulated later with the paper broker's fill command."]


def t_portfolio_status(kit, a, call):
    view = api.account_view(kit.ctx, live_quotes=a.get("live_quotes", True))
    view.pop("disclaimer", None)
    return view, []


def t_paper_mtm(kit, a, call):
    view = api.fno_view(kit.ctx)
    if a.get("strategy"):
        view["strategies"] = [s for s in view["strategies"] if s["id"] == a["strategy"]]
        view["positions"] = [p for p in view["positions"] if p["strategy_id"] == a["strategy"]]
    return view, ["Read only: marks from trader/options.json (refresh it with options_chain). Nothing was written."]


# ---------------------------------------------------------------- tools: options, results, insider, config

def _opt_argv(kit, a, cmd, tool, strikes):
    sym = _sym(a)
    argv = [cmd, sym, "--json", "--strikes", str(strikes)] + _cfg(kit)
    _add(argv, "--expiry", a.get("expiry"))
    return argv + kit.off_args(tool, "trader-options", sym) + kit.now_args(tool)


def t_options_chain(kit, a, call):
    strikes = a.get("strikes") if a.get("strikes") is not None else 10
    data, err = kit.run_json("options", _opt_argv(kit, a, "chain", "options_chain", strikes))
    return data, [err or None, "IV and Greeks are Black-76 on the put-call-parity forward, from delayed NSE data."]


SUMMARY_KEYS = ("underlying", "kind", "expiry", "expiry_nse", "days_to_expiry", "asof", "spot", "forward", "basis",
                "lot_size", "rate", "atm", "atm_iv", "atm_iv_ce", "atm_iv_pe", "straddle", "expected_move",
                "pcr_oi", "pcr_volume", "pcr", "max_pain", "max_pain_detail", "walls", "skew", "iv_rank",
                "warnings", "notes", "sources", "generated_at", "units")


def t_options_summary(kit, a, call):
    data, err = kit.run_json("options", _opt_argv(kit, a, "iv", "options_summary", 0))
    out = {k: data.get(k) for k in SUMMARY_KEYS if k in data}
    return out, [err or None, "PCR, max pain and walls describe positioning, not direction."]


def t_results_analyse(kit, a, call):
    syms = [s.strip().upper() for s in str(a.get("symbols") or "").split(",") if s.strip()]
    if not syms:
        raise ToolError("symbols is required, e.g. TCS or TCS,INFY")
    if len(syms) > 5:
        raise ToolError("at most 5 symbols per call (NSE is asked once for each)")
    for s in syms:
        if not SYMBOL_RE.match(s):
            raise ToolError("%r is not an NSE symbol" % s)
    argv = syms + ["--json", "--quarters", str(a.get("quarters") or 5), "--today", kit.today("results_analyse")]
    _add(argv, "--standalone", bool(a.get("standalone")))
    argv += kit.off_args("results_analyse", "trader-analyst")
    data, err = kit.run_json("analyst", argv)
    return data, [err or None, "Facts from the company's own XBRL filings with NSE; no ratings, no targets."]


def _insider_common(kit, tool):
    return ["--json", "--portfolio", kit.p("portfolio.json"), "--watchlist", kit.p("watchlist.json"),
            "--config", kit.p("config.json"), "--today", kit.today(tool)] + kit.off_args(tool, "trader-insider")


def t_insider_deals(kit, a, call):
    argv = ["deals"] + _insider_common(kit, "insider_deals")
    _add(argv, "--symbol", _sym(a, required=False))
    for key, flag in (("client", "--client"), ("min_value_cr", "--min-value-cr"), ("side", "--side"),
                      ("kind", "--kind"), ("days", "--days"), ("limit", "--limit")):
        _add(argv, flag, a.get(key))
    _add(argv, "--mine", bool(a.get("mine")))
    data, err = kit.run_json("insider", argv)
    return data, [err or None, "A bulk or block deal is one party's trade, with reasons we cannot see."]


def t_insider_disclosures(kit, a, call):
    argv = ["insider"] + _insider_common(kit, "insider_disclosures")
    _add(argv, "--symbol", _sym(a, required=False))
    _add(argv, "--days", a.get("days"))
    _add(argv, "--max-filings", a.get("max_filings"))
    _add(argv, "--promoters-only", bool(a.get("promoters_only")))
    _add(argv, "--market-only", bool(a.get("market_only")))
    _add(argv, "--mine", bool(a.get("mine")))
    data, err = kit.run_json("insider", argv)
    return data, [err or None, "SEBI PIT disclosures are filed after the trade, sometimes days later."]


def t_config_get(kit, a, call):
    view = api.r_config(kit.ctx, {}, None)
    if not a.get("with_schema"):
        view.pop("schema", None)
    return view, ["Read only. Edit trader/config.json yourself, or use the local UI's Settings page."]


def _broker_q(a):
    b = a.get("broker")
    return {"broker": b} if b else {}


def t_broker_status(kit, a, call):
    view = api.broker_status(kit.ctx)
    view.pop("disclaimer", None)
    return view, ["Read only, no network: which BT_* variables are set (never their values), what each broker's "
                  "API can do, the last preflight. There is no order tool here."]


def t_broker_holdings(kit, a, call):
    return api.broker_rows(kit.ctx, _broker_q(a), "holdings"), [
        "Read only: delivery holdings from the live broker's own API. Nothing is placed or changed."]


def t_broker_positions(kit, a, call):
    return api.broker_rows(kit.ctx, _broker_q(a), "positions"), [
        "Read only: today's positions from the live broker's own API. Nothing is placed or changed."]


# ---------------------------------------------------------------- tools: the strategy lab

LAB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
LAB_SLIM = ("trades", "oos_trades", "equity")
LAB_LIMITS = ("A backtest does not predict the future: out of sample means later bars of the same history, fills "
              "and charges are models, and the universe has survivorship bias. A WATCH or PROMOTE is evidence, "
              "not a promise.")


def _lab_strategy(a):
    n = str(a.get("strategy") or "").strip().lower()
    if not LAB_NAME_RE.match(n):
        raise ToolError("strategy is a library or lab name such as orb15-vwap (lab_list shows them)")
    return n


def _lab_now(kit, tool, lab):
    n = kit.now_text(tool)
    return lab.ist_now(str(n)) if n else lab.ist_now()


def _lab_archive(kit, tool):
    """The trader-archive folder an options spec reads: the offline bundle's trader-lab/archive, else
    $BT_ARCHIVE, else trader/archive."""
    if kit.offline:
        p = os.path.join(kit.offline.path(tool, "trader-lab"), "archive")
        if os.path.isdir(p):
            return p
    return os.environ.get("BT_ARCHIVE") or kit.p("archive")


def _lab_data(kit, tool, lab, spec, now):
    """The spec's bars (offline: the bundle's saved bars, as seen at the tool's pinned clock), the regime
    context, the F&O list and notes. An options spec gets its EOD chains from the local archive."""
    if spec.get("kind") == "options":
        data, ctx, notes = lab.load_data(spec, None, None, False, now, _lab_archive(kit, tool))
        return data, ctx, None, notes
    data_dir = kit.offline.path(tool, "trader-lab") if kit.offline else None
    data, ctx, notes = lab.load_data(spec, data_dir, os.path.join(kit.p("lab"), "data"), not kit.offline, now)
    fno, fnote = lab.load_fno_stocks(spec["cfg"], kit.p("fno_stocks.json"), fetch=not kit.offline)
    return data, ctx, fno, notes + [fnote]


def _lab_view(lab, res, status):
    st = dict(status or {})
    st["history"] = (st.get("history") or [])[-5:]
    return {"name": res["name"], "verdict": st.get("verdict") or res.get("verdict"),
            "verdict_why": st.get("why") or res.get("verdict_why") or "", "status": st,
            "results": {k: v for k, v in res.items() if k not in LAB_SLIM},
            "scorecard": lab.scorecard(res, status), "does_not_prove": lab.does_not_prove(res),
            "folder": "trader/lab/%s/" % res["name"]}


def t_lab_list(kit, a, call):
    lab = api.skill("lab")
    out = kit.p("lab")
    lib = [{"name": n, "title": d.get("title"), "timeframe": d.get("timeframe"),
            "product": d.get("product") or ("MIS" if d.get("timeframe") == "5m" else "CNC"),
            "description": d.get("description"), "symbols": len(d.get("universe") or []),
            "kind": d.get("kind") or "equity", "underlying": d.get("underlying"), "structure": d.get("structure"),
            "params": d.get("params") or {}, "grid": d.get("grid") or {}} for n, d in sorted(lab.library().items())]
    names = []
    if os.path.isdir(out):
        names = [n for n in sorted(os.listdir(out)) if os.path.isfile(os.path.join(out, n, "results.json"))]
    try:
        runs = lab.compare_rows(out, names)
    except lab.LabError as e:
        raise ToolError(str(e))
    return {"library": lib, "runs": runs}, ["Library specs are strategies to test, not promises. lab_run tests one."]


def t_lab_report(kit, a, call):
    lab = api.skill("lab")
    name = _lab_strategy(a)
    d = os.path.join(kit.p("lab"), name)
    try:
        res = lab.read_json(os.path.join(d, "results.json"))
        st = lab.read_json(os.path.join(d, "status.json"), {}) or {}
    except lab.LabError as e:
        raise ToolError(str(e))
    if not res:
        raise ToolError("no results for %s yet: run it with lab_run" % name)
    view = _lab_view(lab, res, st)
    if a.get("include_trades"):
        view["oos_trades"] = (res.get("oos_trades") or [])[:500]
    return view, [LAB_LIMITS]


def t_lab_forward(kit, a, call):
    lab = api.skill("lab")
    name = _lab_strategy(a)
    want = _sym(a, "draft", required=False)
    try:
        doc, src = lab.resolve_spec(name, kit.p("lab"))
        spec = lab.validate_spec(doc)
        now = _lab_now(kit, "lab_forward", lab)
        data, ctx, fno, notes = _lab_data(kit, "lab_forward", lab, spec, now)
        r = lab.forward(doc, data, kit.p("lab"), None, fno, now)
    except (lab.LabError, lab.RulesError) as e:
        raise ToolError(str(e))
    drafted = None
    if want and r.get("kind") == "options":
        drafted, dnotes = _lab_draft_basket(kit, lab, r, want, a.get("persona"))
        notes += dnotes
    elif want:
        plan = [p for p in r["plans"] if p["symbol"] == want]
        if not plan:
            raise ToolError("no fresh %s signal to draft (today's lab plans: %s)"
                            % (want, ", ".join(p["symbol"] for p in r["plans"]) or "none"))
        kit.backup("plan.json")
        lab.write_json(kit.p("plan.json"), plan[0])
        drafted, dnotes = t_paper_order_draft(kit, {"persona": a.get("persona")} if a.get("persona") else {}, call)
        notes += list(dnotes) + ["The lab's plan for %s is now trader/plan.json (the old one kept as plan.json.bak)."
                                 % want]
    if drafted:
        step = drafted["next_step"]
    elif r.get("baskets"):
        step = ("A fresh options basket for %s. To paper trade it, call lab_forward again with draft: %s. It "
                "becomes trader/strategy.json and trader/orders.json and trader-risk checks the legs as ONE "
                "basket; the user then confirms the one basket ticket with their own typed yes (local UI or "
                "ticket.py)." % (", ".join(b["expiry"] for b in r["baskets"]),
                                         r["baskets"][0]["strategy"]["underlying"]))
    elif r["plans"]:
        step = ("Fresh signals: %s. To paper trade one, call lab_forward again with draft: SYMBOL. It becomes "
                "trader/plan.json and goes through paper_order_draft (trader-budget sizes it, trader-risk checks "
                "it); then show the user the ticket and wait for their own typed yes before paper_order_confirm."
                % ", ".join(p["symbol"] for p in r["plans"]))
    else:
        step = "No paper plans to draft."
    return {"forward": r, "drafted": drafted, "next_step": step}, \
        notes + ["The forward record is the lab's simulated fills on bars the backtest never saw. Nothing was placed."]


def _lab_draft_basket(kit, lab, r, want, persona=None):
    """An options forward basket -> trader/strategy.json and trader/orders.json (the old ones kept as .bak),
    then trader-risk's check of the basket. Places nothing."""
    pick = [b for b in r["baskets"] if b["strategy"]["underlying"] == want]
    if not pick:
        raise ToolError("no fresh %s basket to draft (today's lab baskets: %s)" % (
            want, ", ".join(b["strategy"]["underlying"] for b in r["baskets"]) or "none"))
    b = pick[0]
    body = dict(b["orders"])
    if persona:
        strat = lab.strategy_api()
        try:
            tag = strat.persona_name(persona)
        except ValueError as e:
            raise ToolError(str(e))
        body["orders"] = [dict(o) for o in body["orders"]]
        strat.tag_orders(body["orders"], tag)
    kit.backup("strategy.json")
    kit.backup("orders.json")
    lab.write_json(kit.p("strategy.json"), b["strategy"])
    lab.write_json(kit.p("orders.json"), body)
    risk, rerr = _risk_record(kit)
    view = api.tickets(kit.ctx)
    bv = view.get("basket") or {}
    step = ("Show the user the basket leg by leg in plain words (side, qty, strike, type, expiry, the limit price, "
            "the margin estimate and the max loss). It is ONE basket ticket: the user confirms it themselves "
            "with their own typed yes in the local UI (Orders) or trader-broker's ticket.py; every leg or none, "
            "paper only."
            if bv.get("ready") else "trader-risk did not pass the basket (%s); nothing can be confirmed."
            % (bv.get("status") or "no verdict"))
    notes = [rerr or None, "The lab's %s basket is now trader/strategy.json and trader/orders.json (the old ones "
                           "kept as .bak); trader-risk recorded its verdicts." % want]
    return {"risk": risk, "basket_ticket": bv, "next_step": step, "basket": b}, notes


# ---------------------------------------------------------------- tools: strategy discovery

RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
DISCOVER_LIMITS = ("Most runs find nothing, and that is the honest answer. A finalist beat the luck of every trial "
                   "counted here on data the search never saw; it is still the past, the universe has survivorship "
                   "bias, and it needs the lab's paper forward test before any PROMOTE.")


def _discover_report(disc, out, name):
    row = disc.run_summary(out, name)
    if not row:
        raise ToolError("no discovery run %s (discover_report without a name lists them)" % name)
    res = disc.read_json(os.path.join(out, name, "results.json"))
    if res:
        row["report"] = res["report"]
        row["run_level"] = res["run_level"]
        row["candidates"] = [{k: c.get(k) for k in ("id", "family", "params", "choices", "train", "validation",
                                                    "holdout", "gates", "dsr", "haircut", "lab")}
                             for c in res["candidates"]]
        row["does_not_show"] = disc.limits(res)
    return row


def t_discover_strategies(kit, a, call):
    disc = api.skill("discover")
    lab = disc.lab_api()
    out, lab_out = kit.p("discover"), kit.p("lab")
    name = a.get("name")
    if name is not None and not RUN_NAME_RE.match(str(name)):
        raise ToolError("name is lowercase letters, digits and dashes")
    n = kit.now_text("discover_strategies")
    now = lab.ist_now(str(n)) if n else lab.ist_now()
    tf = a.get("timeframe") or "1d"

    def progress(f, msg):
        if call.cancel.is_set():
            return
        call.progress(min(0.99, max(0.0, f)), msg)
        call.log("info", msg)

    try:
        syms, ulabel, _ = disc.resolve_universe(a.get("universe") or "nifty50")
        budget, seed = int(a.get("budget") or 100), int(a.get("seed") if a.get("seed") is not None else 7)
        name = name or disc.default_name(ulabel, tf, seed, budget)
        existing = disc.read_json(os.path.join(out, name, "plan.json"))
        if existing:
            syms, tf = existing["universe"], existing["timeframe"]
        data_dir = kit.offline.path("discover_strategies", "trader-lab") if kit.offline else None
        archive = kit.p("archive") if os.path.isdir(kit.p("archive")) else None
        call.progress(0.01, "loading bars for %d symbols" % len(syms))
        data, ctx, notes = disc.load_market(lab, syms, tf, data_dir, os.path.join(lab_out, "data"), not kit.offline,
                                            now, archive)
        missing = [x for x in syms if x not in data]
        notes = [x for x in notes if not any(x.startswith(m + ":") for m in missing)]
        if missing:
            notes.append("no bars for %d of %d symbols (%s%s)" % (len(missing), len(syms), ", ".join(missing[:8]),
                                                                  "..." if len(missing) > 8 else ""))
        fno, fnote = lab.load_fno_stocks({}, kit.p("fno_stocks.json"), fetch=not kit.offline)
        cancel = call.cancel.is_set
        secs = float(a.get("max_seconds") or 300)
        if existing:
            plan, res, msg = disc.resume(lab, name, out, lab_out, data, ctx, fno, now, progress, secs, cancel)
        else:
            plan, res, msg = disc.start(lab, name, tf, ",".join(syms) if ulabel == "custom" else ulabel, budget, seed,
                                        a.get("search") or "random", out, lab_out, data, ctx, fno, now, progress, secs,
                                        cancel, sources=notes)
    except disc.DiscoverError as e:
        raise ToolError(str(e))
    except (lab.LabError, lab.RulesError) as e:
        raise ToolError(str(e))
    if call.cancel.is_set():
        raise ToolError("cancelled: the run is saved; call discover_strategies again with name %s to resume" % name)
    view = _discover_report(disc, out, name)
    view["message"] = msg
    if res is None:
        view["next_step"] = "Paused (%s). Call discover_strategies with name %s to carry on." % (msg, name)
    elif res["finalists"]:
        view["next_step"] = ("Finalists are trader-lab specs now; read their lab verdicts. A WATCH needs lab_forward "
                             "each session (a paper forward test) before any PROMOTE; any paper order still needs "
                             "risk_check and the user's own yes.")
    else:
        view["next_step"] = "No finalists: tell the user plainly that nothing beat the luck of the search."
    return view, notes + [fnote, "Wrote trader/discover/%s/." % name, DISCOVER_LIMITS]


def t_discover_report(kit, a, call):
    disc = api.skill("discover")
    out = kit.p("discover")
    name = a.get("name")
    if not name:
        return {"runs": disc.list_runs(out)}, ["discover_strategies starts a run; most runs find nothing."]
    if not RUN_NAME_RE.match(str(name)):
        raise ToolError("name is lowercase letters, digits and dashes")
    try:
        return _discover_report(disc, out, str(name)), [DISCOVER_LIMITS]
    except disc.DiscoverError as e:
        raise ToolError(str(e))


# ---------------------------------------------------------------- tools: trader-strategy

_STRATEGY_CTX = {}
STRATEGY_COMMON = ("expiry", "fill", "at", "iv_shift", "seed", "paths", "simulate")


def _strategy_ctx(kit, tool):
    """The UI's context (one loader, one cache), pointed at this tool's offline folder and clock."""
    if not kit.offline:
        return kit.ctx
    key = (id(kit), tool)
    if key not in _STRATEGY_CTX:
        folder = kit.offline.path(tool, "ui")
        _STRATEGY_CTX[key] = api.Context(kit.workdir, offline=folder, now=kit.now_text(tool))
    return _STRATEGY_CTX[key]


def _strategy(kit, tool, body, a):
    body["symbol"] = _sym(a)
    for k in STRATEGY_COMMON:
        if a.get(k) is not None:
            body[k] = a[k]
    doc = api.strategy_doc(_strategy_ctx(kit, tool), body)
    doc.pop("why", None)
    doc.pop("orders", None)               # proposals are written only by the CLI or the UI, then the paper flow
    if not a.get("include_curve"):
        c = doc.get("curve") or {}
        doc["curve"] = {k: c.get(k) for k in ("horizon", "iv_shift", "breakevens_at", "spot", "forward",
                                               "expected_move")}
    notes = ["Max profit and loss, breakevens and charges at expiry are exact for one expiry; the today curve, a "
             "calendar's far leg, the SPAN-style margin and both probabilities are models.",
             "To paper-trade it, the user writes a proposal with the CLI (strategy.py ... --orders) or the local UI "
             "(Send to paper); then risk_check and paper_order_confirm, one leg at a time with their typed yes. "
             "trader-risk checks the legs as ONE basket (lots, ban list, premium, margin, Greeks, scenarios) and "
             "blocks them all unless the config allows F&O (allow_fno)."]
    return doc, notes


def t_strategy_build(kit, a, call):
    params = {k: a[k] for k in ("strike", "wings", "width", "delta", "skip", "ratio", "lots", "type")
              if a.get(k) is not None}
    body = {"template": a.get("template"), "params": params}
    if a.get("far_expiry"):
        body["far_expiry"] = a["far_expiry"]
    return _strategy(kit, "strategy_build", body, a)


def t_strategy_price(kit, a, call):
    return _strategy(kit, "strategy_price", {"legs": a.get("legs")}, a)


def t_doctor(kit, a, call):
    """trader-doctor's self-test, read only: the history in trader/doctor.json is read, not updated."""
    argv = ["--json", "--no-write", "--history", kit.p("doctor.json")]
    if a.get("only"):
        argv += ["--only", a["only"]]
    if kit.offline:
        argv += kit.off_args("doctor", "trader-doctor")
    report, _ = kit.run_json("doctor", argv + kit.now_args("doctor"))
    bad = [r for r in report.get("sources") or [] if r.get("status") in ("changed", "down", "blocked")]
    notes = ["%s: %s (%s). Fallback: %s." % (r["name"], r["status"], r["detail"], r.get("fallback_used")
                                              or r["fallback"]) for r in bad]
    return report, notes or ["Every data source answered as the tools expect (%s)." % report.get("market")]


# ---------------------------------------------------------------- tools: agent personas

def t_persona_scorecards(kit, a, call):
    data = api.persona_scorecards(kit.ctx, a.get("persona"))
    data.pop("disclaimer", None)
    return data, ["A paper record is simulated fills, and a short one says almost nothing about skill: compare it "
                  "with the rule's lab scorecard. Read only."]


# ---------------------------------------------------------------- tools: market regime

def t_regime_now(kit, a, call):
    """trader-regime's own `now --json`. Offline: the bundle's saved daily bars, no network."""
    argv = ["now", "--json", "--workdir", kit.p()]
    syms = [s.strip().upper() for s in str(a.get("symbols") or "").split(",") if s.strip()]
    for s in syms[:12]:
        if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", s):
            raise ToolError("not an NSE symbol: %r" % s)
    argv += syms[:12]
    if a.get("iv"):
        argv.append("--iv")
    if kit.offline:
        dirs = [kit.offline.path("regime_now", "trader-lab")]
        also = kit.offline.entry("regime_now").get("also")
        if also:
            dirs.append(os.path.normpath(os.path.join(kit.offline.dir, also)))
        argv += ["--data", ",".join(d for d in dirs if d), "--offline"]
    data, _ = kit.run_json("regime", argv + kit.now_args("regime_now"))
    notes = list(data.get("errors") or []) + list(data.get("notes") or [])
    notes.append("A regime describes the market so far; it does not predict the next move. Show the inputs "
                 "(ADX, VIX percentile) with the label.")
    data.pop("disclaimer", None)
    return data, notes


# ---------------------------------------------------------------- tools: co-trader

COTRADER_ID_RE = r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{1,47}-[A-Z0-9][A-Z0-9&-]{0,19}$"
COTRADER_LIMITS = ("A candidate's probability is the share of similar past trades (same rule, same kind of market) that "
                   "made money after charges, with its sample size and 95% range; never a promise about this trade. "
                   "Say the tier: RESEARCH ideas are unproven, for study or paper only. No candidate is a valid, "
                   "often the best, answer: say so plainly.")


def _cotrader_args(kit, tool):
    lab = api.skill("lab")
    n = kit.now_text(tool)
    now = lab.ist_now(str(n)) if n else lab.ist_now()
    data = kit.offline.path(tool, "trader-lab") if kit.offline else None
    return now, data


def _slim_candidate(c):
    out = dict(c)
    out.pop("order", None)
    return out


def t_cotrader_candidates(kit, a, call):
    ct = api.skill("cotrader")
    now, data = _cotrader_args(kit, "cotrader_candidates")
    tier = "research" if a.get("include_research") else "validated"
    if a.get("refresh", True):
        call.progress(0.1, "reading the lab's strategies and the latest bars")
        try:
            doc = ct.run_today(kit.workdir, tier, a.get("strategy"), now, data, None, not kit.offline, True)
        except ct.errors() as e:
            raise ToolError(str(e))
        notes = ["Wrote trader/cotrader.json, a plan per candidate (trader/cotrader/plans/) and %d new prediction(s) "
                 "in trader/cotrader/log.jsonl for calibration. No order was drafted or placed." % doc.get("logged", 0)]
    else:
        doc = kit.ctx.read("cotrader.json")
        if not doc:
            raise ToolError("no trader/cotrader.json yet: call cotrader_candidates with refresh: true")
        notes = ["The saved run of %s IST (not refreshed)." % doc.get("as_of")]
    doc = dict(doc)
    if a.get("lang"):
        doc["summary"] = ct.render_today(doc, lang_of(kit, a))
    doc["candidates"] = [_slim_candidate(c) for c in doc.get("candidates") or []]
    doc.pop("settings", None)
    doc.pop("disclaimer", None)
    if doc.get("candidates"):
        step = ("Show each candidate with its tier, the probability with its 95% range and n, the expected R after "
                "charges, the regime, the risk verdict and the 'does not show' list. To paper trade one, call "
                "paper_order_draft with candidate: ID, show the ticket, and wait for the user's own typed yes.")
    else:
        step = "No candidate: tell the user plainly that sitting out is the decision today, with the reasons."
    doc["next_step"] = step
    return doc, notes + [COTRADER_LIMITS]


def t_cotrader_explain(kit, a, call):
    ct = api.skill("cotrader")
    if not a.get("id"):
        doc = kit.ctx.read("cotrader.json") or {}
        rows = [{"id": c["id"], "tier": c["tier"], "status": c["status"], "symbol": c["symbol"],
                 "strategy": c["strategy"]} for c in doc.get("candidates") or []]
        return {"as_of": doc.get("as_of"), "candidates": rows, "no_edge": doc.get("no_edge")}, \
            ["The candidates of the last run (%s): call again with id for one in full." % (doc.get("as_of") or "none yet")]
    c, doc = ct.find_candidate(kit.workdir, a["id"])
    if c is None:
        raise ToolError("no candidate %s in trader/cotrader.json: call cotrader_candidates first" % a["id"])
    out = _slim_candidate(c)
    out["text"] = ct.render_explain(c, lang_of(kit, a))
    return out, [COTRADER_LIMITS, "Read only."]


def t_calibration_report(kit, a, call):
    ct = api.skill("cotrader")
    now, data = _cotrader_args(kit, "calibration_report")
    try:
        rep = ct.calibration_report(kit.workdir, now, data, None, not kit.offline, False)
    except ct.errors() as e:
        raise ToolError(str(e))
    rep.pop("disclaimer", None)
    rep["records"] = rep.get("records", [])[-100:]
    return rep, ["Read only: demotion happens in a terminal (cotrader.py calibration), never from here. A Brier "
                 "score lower than the base-rate reference is better than a constant guess; a few dozen outcomes "
                 "prove little either way."]


# ---------------------------------------------------------------- the auto-pilot (paper only)

AUTOPILOT_LIMITS = ("The auto-pilot is PAPER only: it refuses a live config and has no way to place a live order. "
                    "A few days of paper results prove little either way; quote the after-charges numbers, the number "
                    "of trades and the lab's verdict, never a promise.")
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


def _slim_report(rep, keep=60):
    out = dict(rep or {})
    out["timeline"] = (out.get("timeline") or [])[-keep:]
    out.pop("disclaimer", None)
    return out


def t_autopilot_status(kit, a, call):
    view = api.autopilot_view(kit.ctx)
    view["timeline"] = view.get("timeline", [])[-60:]
    if view.get("report"):
        view["report"] = _slim_report(view["report"])
    view.pop("disclaimer", None)
    return view, [AUTOPILOT_LIMITS, "Read only."]


def t_autopilot_replay(kit, a, call):
    ap_ = api.skill("autopilot")
    day, to = a.get("date"), a.get("to")
    data = kit.offline.path("autopilot_replay", "trader-lab") if kit.offline else None
    call.progress(0.1, "re-running the lab as of the close before %s, then the day on a simulated clock" % day)
    try:
        reps = ap_.replay(kit.workdir, day, to or None, data, None, True)
    except ap_.errors() as e:
        raise ToolError(str(e))
    name = day + (("_" + to) if to and to != day else "")
    return {"sandbox": "trader/autopilot/replays/%s" % name, "reports": [_slim_report(r) for r in reps if r]}, \
        [AUTOPILOT_LIMITS, "A sandbox: your own paper account, orders and lab runs were not touched."]


START_SCHEMA = {"type": "object", "properties": {"typed": {
    "type": "string", "title": "Your confirmation", "minLength": 1, "maxLength": 50,
    "description": "Type the words: start paper (anything else starts nothing)"}}, "required": ["typed"]}
STOP_SCHEMA = {"type": "object", "properties": {"typed": {
    "type": "string", "title": "Your confirmation", "minLength": 1, "maxLength": 50,
    "description": "Type the words: stop paper"}}, "required": ["typed"]}


def _autopilot_typed(kit, a, call, key, message, schema):
    answer = call.elicit(key, message, schema, {"tool": key})
    if answer is not None:
        if not isinstance(answer, dict) or answer.get("action") != "accept":
            return None, "elicitation"
        return (answer.get("content") or {}).get("typed"), "elicitation"
    text = a.get("typed")
    if not text:
        raise ToolError("not done: this client cannot ask the user directly, so typed must be the user's own words, "
                        "typed by them. Never write it yourself.")
    return text, "argument"


def t_autopilot_start(kit, a, call):
    ap_ = api.skill("autopilot")
    cfg = kit.ctx.config()
    try:
        ap_.paper_guard(cfg)            # a live config stops here, before the user is even asked
        ap_.settings_of(cfg)
    except ap_.AutopilotError as e:
        raise ToolError(str(e))
    text, via = _autopilot_typed(kit, a, call, "autopilot_start",
                                 "Start the auto-pilot on the PAPER account (no real money)? It trades the strategies "
                                 "in your autopilot config inside your limits, each order checked by trader-risk. Type "
                                 "%s to start. %s" % (ap_.START_WORDS, DISCLAIMER), START_SCHEMA)
    if text is None:
        return {"started": False, "via": via, "message": "The user did not accept. Nothing was started."}, []
    try:
        view = api.r_autopilot_start(kit.ctx, {}, {"typed": text})
    except api.ApiError as e:
        raise ToolError(e.message)
    view["timeline"] = view.get("timeline", [])[-30:]
    view.pop("report", None)
    view.pop("disclaimer", None)
    return dict(view, via=via), [AUTOPILOT_LIMITS]


def t_autopilot_stop(kit, a, call):
    ap_ = api.skill("autopilot")
    text, via = _autopilot_typed(kit, a, call, "autopilot_stop", "Stop the auto-pilot after its current step? Type "
                                 "%s. Open paper positions stay open (stops and targets are managed when it starts "
                                 "again)." % ap_.STOP_WORDS, STOP_SCHEMA)
    if text is None:
        return {"stopped": "The user did not accept. Nothing was changed.", "via": via}, []
    try:
        view = api.r_autopilot_stop(kit.ctx, {}, {"typed": text})
    except api.ApiError as e:
        raise ToolError(e.message)
    return {"stopped": view["stopped"], "running": view["running"], "via": via}, [AUTOPILOT_LIMITS]
# ---------------------------------------------------------------- tools: the trade journal

JOURNAL_NOTE = ("A journal describes the user's own past trades: quote each rate with its range and the number of trades, "
                "after charges, and say plainly when there are under 20. Patterns in a few trades can be chance. Read "
                "only.")


def _journal(kit):
    jr = api.skill("journal")
    return jr, jr.Paths(kit.workdir), kit.ctx.now_ist().replace(tzinfo=None, microsecond=0)


def _journal_bars(kit):
    """Offline: the bundle's saved lab bars. Otherwise the archive and the lab cache only (never fetched)."""
    if kit.offline:
        p = kit.offline.path("journal_summary", "trader-lab")
        return p if p and os.path.isdir(p) else None
    return None


def t_journal_summary(kit, a, call):
    """trader-journal's overview, read only: stats with intervals, this week, rule breaks, the ledger totals."""
    jr, paths, now = _journal(kit)
    n = int(a.get("recent") or 10)
    try:
        d = jr.summary(paths, _journal_bars(kit), True, now, recent=n)
    except jr.JournalError as e:
        raise ToolError(str(e))
    d["stats"].pop("equity", None)
    d.pop("by", None)
    d["counterfactual"].pop("rows", None)
    d.pop("disclaimer", None)
    for e in d["entries"]:
        e.pop("fills", None)
    return d, [JOURNAL_NOTE]


def t_journal_stats(kit, a, call):
    jr, paths, now = _journal(kit)
    by, week = a.get("by"), a.get("week")
    try:
        doc = jr.gather(paths, _journal_bars(kit), True, False, now)
        entries = doc["entries"]
        label = None
        if week:
            label, mon, sun = jr.week_bounds(week, now)
            entries = [e for e in entries if mon.isoformat() <= e["closed_at"][:10] <= sun.isoformat()]
        cfg = jr.config_of(paths)
        s = jr.stats(entries, cfg.get("capital"))
        s.pop("equity", None)
        beh = jr.behaviour(entries, cfg, jr.verdicts_by_fp(jr.risk_records(paths))) if week else doc["behaviour"]
        out = {"week": label, "stats": s, "behaviour": beh}
        if by:
            out["by"] = by
            out["groups"] = jr.breakdown(entries, by)
    except jr.JournalError as e:
        raise ToolError(str(e))
    return out, [JOURNAL_NOTE]


def t_counterfactual_report(kit, a, call):
    jr, paths, now = _journal(kit)
    try:
        doc = jr.gather(paths, _journal_bars(kit), True, False, now, with_bars=False)
        cf = jr.counterfactual(paths, doc["entries"], jr.config_of(paths), _journal_bars(kit), True,
                               a.get("since"), a.get("until"))
    except jr.JournalError as e:
        raise ToolError(str(e))
    rows = cf["rows"]
    if a.get("group"):
        rows = [r for r in rows if r["group"] == a["group"]]
    cf["rows"] = rows[-int(a.get("limit") or 50):]
    return cf, ["Hypothetical fills on bars with trader-lab's rules, not an order book. Give the counts and the "
                "caveats with any rupee total; a skipped trade that would have won is not a mistake by itself. "
                "Read only."]


# ---------------------------------------------------------------- tools: the tax estimate

TAX_NOTE = ("An ESTIMATE for discussion with the user's chartered accountant, not tax advice: say so whenever you "
            "quote it. Give each figure with its head and section, and the rule's effective date when it matters. "
            "Slab tax, surcharge, cess, rebates and deductions are not computed; do not compute them. Read only.")


def t_tax_summary(kit, a, call):
    """trader-tax's financial-year report, read only: heads, turnover and audit check, set-off, special rates."""
    tx = api.skill("tax")
    now = kit.ctx.now_ist().replace(tzinfo=None, microsecond=0)
    try:
        d = tx.report(tx.Paths(kit.workdir), tx.parse_fy(a["fy"]) if a.get("fy") else None, a.get("book") or "live",
                      None, None, now)
    except tx.TaxError as e:
        raise ToolError(str(e))
    out = tx.summary_view(d)
    out.pop("trades_preview", None)
    if not a.get("rules"):
        out["rules"] = [{"what": r["what"], "section": r["section"], "effective": r["effective"]} for r in d["rules"]]
    return out, [TAX_NOTE]


# ---------------------------------------------------------------- tools v2: the co-trader workflow layer

TRIALS_LEDGER = ("state", "lab-trials.jsonl")
TRIAL_NOTE = ("Every spec run through the lab is a trial: the deflated Sharpe of each later spec in the same family "
              "is charged for it, so trying many variants and keeping the best one cannot pass the gates by luck.")
PROPOSE_NOTE = ("Proposing is not placing: nothing here places an order. Any paper order still needs trader-risk "
                "PASS and the user's own typed yes for that one ticket.")


def _family(doc):
    """A strategy family: the spec's structure with every number left out (names, fields and indicators used,
    the stop and target kinds). Variants that only change a number, a symbol or the name share a family."""
    if not isinstance(doc, dict):
        return "unknown", "not a spec"
    if doc.get("kind") == "options":
        en = doc.get("entry") if isinstance(doc.get("entry"), dict) else {}
        sig = ["options", str(doc.get("underlying")), str(doc.get("structure")), str(doc.get("expiry", "weekly")),
               str((doc.get("strikes") or {}).get("by") if isinstance(doc.get("strikes"), dict) else None),
               sorted(k for k in en if k != "days_to_expiry")]
        words = "%s %s (%s expiry)" % (doc.get("underlying"), doc.get("structure"), doc.get("expiry", "weekly"))
    else:
        names = set()

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k not in ("shift",):
                        names.add(str(k))
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, str) and not x.startswith("$") and not re.match(r"^\d{1,2}:\d{2}$", x):
                names.add(x)
        walk(doc.get("entry"))
        stop = sorted(doc["stop"]) if isinstance(doc.get("stop"), dict) else []
        target = sorted(doc["target"]) if isinstance(doc.get("target"), dict) else []
        ex = doc.get("exit") if isinstance(doc.get("exit"), dict) else {}
        exn = set()
        if ex.get("when") is not None:
            walk_before = set(names)
            walk(ex["when"])
            exn = names - walk_before
            names = walk_before
        sig = ["equity", str(doc.get("timeframe")), sorted(names), stop, target, sorted(exn)]
        shown = sorted(n for n in names if n not in ("all", "any", "not") and not re.match(r"^[<>=]+$", n))
        words = "%s entry on %s; stop by %s" % (doc.get("timeframe"), ", ".join(shown) or "nothing",
                                                ", ".join(stop) or "none")
    h = hashlib.sha1(json.dumps(sig, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    return "%s-%s" % (sig[0], h), words


def _ledger(kit):
    p = kit.p(*TRIALS_LEDGER)
    rows = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("family") and r.get("spec_hash"):
                    rows.append(r)
    return rows


def _family_trials(kit, family, spec_h=None):
    """{spec_hash: grid points} of the specs of this family already run through the lab from this server."""
    out = {}
    for r in _ledger(kit):
        if r["family"] == family and r["spec_hash"] != spec_h:
            out[r["spec_hash"]] = max(out.get(r["spec_hash"], 1), int(r.get("points") or 1))
    return out


def _record_trial(kit, tool, name, family, spec_h, points, now):
    p = kit.p(*TRIALS_LEDGER)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = {"at": now.strftime("%Y-%m-%d %H:%M"), "tool": tool, "name": name, "family": family, "spec_hash": spec_h,
           "points": int(points)}
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _lab_execute(kit, a, call, tool, doc):
    """Run one spec doc through the lab (time budget, progress, cancel), charge it for the other specs of its
    family run from here, save it under trader/lab/<name>/ and record it in the trials ledger."""
    lab = api.skill("lab")
    out = kit.p("lab")
    budget = float(a.get("max_seconds") or 180)
    started = time.monotonic()
    done = [0, 1]

    def check(message, fraction):
        if call.cancel.is_set():
            raise ToolError("cancelled")
        if time.monotonic() - started > budget:
            raise ToolError("stopped after %d seconds (max_seconds): try fewer symbols or a smaller grid"
                            % int(budget))
        call.progress(fraction, message)
        call.log("info", message)

    def progress(message):
        done[0] += 1
        check(message, min(0.95, 0.1 + 0.85 * done[0] / float(done[1])))

    try:
        spec = lab.validate_spec(doc)
        done[1] = len(lab.grid_points(spec)[2])
        family, words = _family(doc)
        h = lab.spec_hash(doc)
        status = lab.read_json(os.path.join(out, spec["name"], "status.json"), {}) or {}
        by_name = set(x for x in status.get("spec_hashes") or [] if x != h)
        fam = _family_trials(kit, family, h)
        # the lab already charges 1 for each earlier version of the same name: add the rest of each grid
        extra = sum(n - (1 if k in by_name else 0) for k, n in fam.items())
        now = _lab_now(kit, tool, lab)
        check("loading bars for %d symbols" % len(spec["universe"]), 0.02)
        data, ctx, fno, notes = _lab_data(kit, tool, lab, spec, now)
        check("backtesting %d grid points" % done[1], 0.1)
        res = lab.run_and_decide(doc, data, ctx, out, fno, now, progress, extra_trials=extra)
        res["notes"] = notes
        st = lab.save_run(out, doc, res, now)
        _record_trial(kit, tool, spec["name"], family, h, done[1], now)
    except (lab.LabError, lab.RulesError) as e:
        raise ToolError(str(e))
    view = _lab_view(lab, res, st)
    view["trials"] = {"family": family, "family_words": words, "grid_points": done[1],
                      "other_specs_in_family": len(fam), "family_trials_charged": extra,
                      "earlier_versions_of_this_name": len(by_name),
                      "trials_counted": (res.get("dsr") or {}).get("trials")}
    return view, notes + ["Wrote trader/lab/%s/ (spec, results, scorecard, equity, status) and one line in "
                          "trader/state/lab-trials.jsonl." % spec["name"], TRIAL_NOTE, LAB_LIMITS]


def t_lab_run(kit, a, call):
    lab = api.skill("lab")
    name = _lab_strategy(a)
    try:
        doc, src = lab.resolve_spec(name, kit.p("lab"))
    except lab.LabError as e:
        raise ToolError(str(e))
    if a.get("symbols"):
        syms = [x.strip().upper() for x in a["symbols"].split(",") if x.strip()]
        if doc.get("kind") == "options":
            doc["underlying"] = syms[0]
        else:
            doc["universe"] = syms
    if a.get("seed") is not None:
        doc["seed"] = int(a["seed"])
    return _lab_execute(kit, a, call, "lab_run", doc)


MAX_SPEC_TEXT = 20000


def _json_arg(a, key, kind, what):
    raw = a.get(key)
    if raw is None or raw == "":
        return None
    try:
        val = json.loads(raw)
    except ValueError as e:
        raise ToolError("%s is not valid JSON: %s" % (key, e))
    if not isinstance(val, kind):
        raise ToolError("%s must be %s" % (key, what))
    return val


def t_lab_run_spec(kit, a, call):
    lab = api.skill("lab")
    doc = _json_arg(a, "spec", dict, "one JSON object: a trader-lab spec (strategy_design drafts one)")
    if doc is None:
        raise ToolError("spec is required: a trader-lab spec as JSON text (strategy_design drafts one)")
    doc.setdefault("schema", lab.SCHEMA_SPEC)
    name = doc.get("name")
    lib = lab.library()
    if isinstance(name, str) and name in lib and lab.spec_hash(lib[name]) != lab.spec_hash(doc):
        raise ToolError("%s is a library spec's name: give your variant its own name (it still counts as a trial "
                        "of the same family)" % name)
    try:
        lab.validate_spec(doc)
    except lab.LabError as e:
        raise ToolError("the spec does not validate: %s (strategy_design shows the vocabulary)" % e)
    if a.get("seed") is not None:
        doc["seed"] = int(a["seed"])
    return _lab_execute(kit, a, call, "lab_run_spec", doc)


# ---- strategy_design: a structured intent -> a lab spec draft (never run)

DESIGN_KEYS = ("symbols", "window_from", "window_to", "days_to_expiry", "weekdays", "vix_min", "vix_max",
               "iv_rank_min", "iv_rank_max", "trend", "regime_gate", "capital", "risk_per_trade_pct",
               "max_trades_per_day", "lots", "target_r", "stop_atr_mult", "profit_target_pct", "stop_loss_mult",
               "params", "extra_entry", "note")
WEEKDAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri)(\s*,\s*(Mon|Tue|Wed|Thu|Fri)){0,4}$")


def vocabulary(lab):
    return {"fields": list(lab.FIELDS), "session_values_5m": list(lab.SESSION_VALUES),
            "indicators": list(lab.INDICATORS), "compare": list(lab.OPS), "arithmetic": list(lab.ARITH),
            "conditions": ["[a, op, b]", "{\"all\": [...]}", "{\"any\": [...]}", "{\"not\": c}",
                           "{\"time_between\": [\"09:15\", \"10:00\"]} (5m only)"],
            "intraday_only": list(lab.INTRADAY_ONLY), "max_grid_points": lab.MAX_GRID,
            "examples": [["close", "crosses_above", {"or_high": 15}], [{"rsi": 2}, "<", 10],
                         ["close", ">", {"sma": 200}], [{"ema": 20}, ">", {"ema": 50}]]}


def _uses_param(x, p):
    if isinstance(x, dict):
        return any(_uses_param(v, p) for v in x.values())
    if isinstance(x, list):
        return any(_uses_param(v, p) for v in x)
    return x == "$" + p


def _set_time_window(entry, a, b):
    """Replace the entry's time_between (or add one) with [a, b]."""
    tb = {"time_between": [a, b]}
    if isinstance(entry, dict) and "all" in entry and isinstance(entry["all"], list):
        rest = [c for c in entry["all"] if not (isinstance(c, dict) and "time_between" in c)]
        return {"all": rest + [tb]}
    if isinstance(entry, dict) and "time_between" in entry:
        return tb
    return {"all": [entry, tb]}


def _add_condition(entry, cond):
    if isinstance(entry, dict) and "all" in entry and isinstance(entry["all"], list):
        return {"all": list(entry["all"]) + [cond]}
    return {"all": [entry, cond]}


def design_spec(lab, template, a):
    """(spec doc, errors, warnings, changes) from a library template and a structured intent. Pure: reads the
    library, runs nothing."""
    lib = lab.library()
    if template not in lib:
        raise ToolError("template is one of %s" % ", ".join(sorted(lib)))
    doc = json.loads(json.dumps(lib[template]))
    errors, warnings, changes = [], [], []
    is_opt = doc.get("kind") == "options"
    tf = doc.get("timeframe")
    syms = [s.strip().upper() for s in str(a.get("symbols") or "").split(",") if s.strip()]
    if syms:
        if is_opt:
            doc["underlying"] = syms[0]
            changes.append("underlying %s" % syms[0])
            if len(syms) > 1:
                warnings.append("an options spec has one underlying: kept %s, dropped %s" % (syms[0],
                                                                                           ", ".join(syms[1:])))
        else:
            doc["universe"] = syms[:100]
            changes.append("universe %s" % ", ".join(syms[:100]))
    if a.get("window_from") or a.get("window_to"):
        if is_opt or tf != "5m":
            errors.append("window: a time window needs a 5-minute equity template (%s); %s trades on daily bars"
                          % (", ".join(n for n, d in sorted(lib.items()) if d.get("timeframe") == "5m"), template))
        else:
            w_from = a.get("window_from") or "09:15"
            w_to = a.get("window_to") or "15:00"
            doc["window"] = {"from": w_from, "to": w_to}
            doc["entry"] = _set_time_window(doc.get("entry"), w_from, w_to)
            changes.append("entries only between %s and %s IST" % (w_from, w_to))
    opt_only = [k for k in ("days_to_expiry", "weekdays", "vix_min", "vix_max", "iv_rank_min", "iv_rank_max",
                            "profit_target_pct", "stop_loss_mult", "lots") if a.get(k) is not None]
    if opt_only and not is_opt:
        errors.append("%s: only options templates take these (%s). The equity vocabulary has no expiry, VIX or IV "
                      "condition; regime_gate: true makes the lab check the trades in today's regime cell instead"
                      % (", ".join(opt_only), ", ".join(n for n, d in sorted(lib.items())
                                                         if d.get("kind") == "options")))
    if is_opt:
        en = dict(doc.get("entry") or {})
        if a.get("days_to_expiry") is not None:
            en["days_to_expiry"] = int(a["days_to_expiry"])
            changes.append("enter %d day(s) before expiry%s" % (en["days_to_expiry"],
                                                                " (expiry day)" if not en["days_to_expiry"] else ""))
        if a.get("weekdays"):
            en["weekdays"] = [d.strip() for d in a["weekdays"].split(",")]
            changes.append("entry weekdays %s" % ", ".join(en["weekdays"]))
        for k in ("vix_min", "vix_max", "iv_rank_min", "iv_rank_max"):
            if a.get(k) is not None:
                en[k] = a[k]
                changes.append("%s %s" % (k, a[k]))
        doc["entry"] = en
        ex = dict(doc.get("exit") or {})
        if a.get("profit_target_pct") is not None:
            ex["profit_target_pct"] = a["profit_target_pct"]
            changes.append("take profit at %s%% of the credit" % a["profit_target_pct"])
        if a.get("stop_loss_mult") is not None:
            ex["stop_loss_mult"] = a["stop_loss_mult"]
            changes.append("stop at %sx the credit" % a["stop_loss_mult"])
        doc["exit"] = ex
        if a.get("lots") is not None:
            doc["lots"] = int(a["lots"])
        if a.get("capital") is not None:
            doc["capital"] = a["capital"]
        for k in ("risk_per_trade_pct", "max_trades_per_day", "target_r", "stop_atr_mult", "trend"):
            if a.get(k) is not None and not (k == "trend" and a[k] == "any"):
                errors.append("%s: an options spec sizes by lots and capital and exits by profit_target_pct or "
                              "stop_loss_mult" % k)
        if a.get("extra_entry"):
            errors.append("extra_entry: options entries take days_to_expiry, weekdays, vix and iv_rank bounds only")
    else:
        sizing = dict(doc.get("sizing") or {})
        for k in ("capital", "risk_per_trade_pct", "max_trades_per_day"):
            if a.get(k) is not None:
                sizing[k] = a[k]
                changes.append("%s %s" % (k, a[k]))
        doc["sizing"] = sizing
        if a.get("trend") == "up":
            doc["entry"] = _add_condition(doc.get("entry"), [{"ema": 20}, ">", {"ema": 50}])
            changes.append("only when the 20 EMA is above the 50 EMA (an uptrend filter on this timeframe's bars)")
        if a.get("target_r") is not None:
            if "target_r" in (doc.get("params") or {}) and _uses_param(doc.get("target"), "target_r"):
                doc["params"]["target_r"] = a["target_r"]
            else:
                doc["target"] = {"r": a["target_r"]}
            changes.append("target %sR" % a["target_r"])
        if a.get("stop_atr_mult") is not None:
            doc["stop"] = {"atr": 14, "mult": a["stop_atr_mult"]}
            changes.append("stop %s x ATR(14)" % a["stop_atr_mult"])
        extra = _json_arg(a, "extra_entry", list, "one condition [a, op, b] in the lab vocabulary")
        if extra is not None:
            doc["entry"] = _add_condition(doc.get("entry"), extra)
            changes.append("extra entry condition %s" % json.dumps(extra)[:120])
    params = _json_arg(a, "params", dict, "a JSON object of declared param: number")
    if params:
        base = doc.get("params") or {}
        for k, v in params.items():
            if k not in base:
                errors.append("params.%s: %s declares %s" % (k, template, ", ".join(sorted(base)) or "no params"))
            elif not isinstance(v, (int, float)) or isinstance(v, bool):
                errors.append("params.%s: a number" % k)
            else:
                base[k] = v
                changes.append("%s = %s" % (k, v))
        doc["params"] = base
    if a.get("regime_gate"):
        g = dict(doc.get("gates") or {})
        g["require_regime_edge"] = 1
        doc["gates"] = g
        changes.append("gate: the out-of-sample trades in today's regime cell must show an edge too")
    seed = json.dumps({k: a.get(k) for k in DESIGN_KEYS}, sort_keys=True, default=str)
    name = a.get("name") or ("a-%s-%s" % (template[:36], hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]))
    doc["name"] = name
    doc["title"] = ("%s (designed)" % (doc.get("title") or template))[:120]
    if a.get("note"):
        doc["description"] = ("%s Designed for: %s" % (doc.get("description") or "", a["note"].strip()))[:1500]
    doc["notes"] = ["Drafted by strategy_design from the %s template: %s." % (template, "; ".join(changes) or
                                                                              "no changes")]
    if not errors:
        try:
            lab.validate_spec(doc)
        except lab.LabError as e:
            errors.append(str(e))
    if not syms and not is_opt:
        warnings.append("universe: the template's own %d symbols" % len(doc.get("universe") or []))
    return doc, errors, warnings, changes


def t_strategy_design(kit, a, call):
    lab = api.skill("lab")
    template = str(a.get("template") or "")
    doc, errors, warnings, changes = design_spec(lab, template, a)
    family, words = _family(doc)
    points = None
    if not errors:
        try:
            points = len(lab.grid_points(lab.validate_spec(doc))[2])
        except lab.LabError as e:
            errors.append(str(e))
    tried = _family_trials(kit, family, lab.spec_hash(doc))
    out = {"spec": doc, "valid": not errors, "errors": errors, "warnings": warnings, "changes": changes,
           "family": family, "family_words": words, "grid_points": points,
           "family_specs_tried": len(tried), "family_trials_so_far": sum(tried.values()),
           "vocabulary": vocabulary(lab)}
    if errors:
        out["next_step"] = "Fix the errors (or pick another template) and call strategy_design again. Nothing ran."
    else:
        out["next_step"] = ("A draft, not a result. To test it, pass this spec as JSON text to lab_run_spec: it is "
                            "backtested after charges with walk-forward, and it counts as %d more trial(s) in family "
                            "%s (%d already tried there)." % (points, family, sum(tried.values())))
    return out, ["Nothing was run or written: a spec is data in the lab's fixed vocabulary, never code.", TRIAL_NOTE]


# ---- archive_status

def t_archive_status(kit, a, call):
    arch = api.skill("archive")
    root = _lab_archive(kit, "archive_status")
    s = arch.status(root)
    s["root"] = "trader/archive" if not kit.offline and root == kit.p("archive") else os.path.basename(root)
    have = [ds for ds, i in s["datasets"].items() if i.get("files") or i.get("first")]
    if not have:
        words = ("No local archive yet: the lab's options backtests and IV ranks need one (python3 "
                 "trader-archive/archive.py backfill --years 1). Equity backtests fall back to Yahoo bars.")
    else:
        words = "Archive: %s." % "; ".join("%s %s to %s%s" % (ds, s["datasets"][ds]["first"], s["datasets"][ds]["last"],
                                                             (" (%d files)" % s["datasets"][ds]["files"])
                                                             if s["datasets"][ds]["files"] else "") for ds in have)
    s["words"] = words
    return s, ["Read only. A gap in the archive means fewer tested days, not a missing loss: say which days a "
               "backtest covered."]


# ---- compare_strategies and best_for_regime

def _ci_mean(xs):
    """(mean, lo, hi): a normal-approximation 95% range of a mean; None below 2 values."""
    if len(xs) < 2:
        return (xs[0] if xs else None), None, None
    m = sum(xs) / float(len(xs))
    var = sum((x - m) ** 2 for x in xs) / float(len(xs) - 1)
    half = 1.96 * (var ** 0.5) / (len(xs) ** 0.5)
    return round(m, 4), round(m - half, 4), round(m + half, 4)


def _wilson(ct, k, n):
    w = ct.wilson(k, n)
    return [round(w[0], 4), round(w[1], 4)] if w else None


def _regime_now_cell(kit, tool, ct, lab):
    now, data = _cotrader_args(kit, tool)
    src = ct.Sources(kit.workdir, data, None, not kit.offline, kit.p("lab"))
    regs = lab.lab_regimes(src.context(now))
    cell, words, close = ct.current_cell(regs, now.date().isoformat())
    return now, regs, cell, words, close


def t_compare_strategies(kit, a, call):
    lab = api.skill("lab")
    ct = api.skill("cotrader")
    names = [n.strip().lower() for n in str(a.get("strategies") or "").split(",") if n.strip()]
    if not 2 <= len(names) <= 8 or not all(LAB_NAME_RE.match(n) for n in names):
        raise ToolError("strategies: 2 to 8 lab run names, comma separated (lab_list shows the runs)")
    out = kit.p("lab")
    try:
        rows = lab.compare_rows(out, names)
    except lab.LabError as e:
        raise ToolError(str(e))
    now, regs, cell, words, close = _regime_now_cell(kit, "compare_strategies", ct, lab)
    for r in rows:
        res = lab.read_json(os.path.join(out, r["name"], "results.json")) or {}
        o = res["walk_forward"]["stats"]
        oos = res.get("oos_trades") or []
        m, lo, hi = _ci_mean([float(t["r"]) for t in oos])
        r["oos_win_rate"] = o.get("win_rate")
        r["oos_win_rate_ci"] = _wilson(ct, o.get("wins", 0), o.get("trades", 0))
        r["oos_expectancy_r_ci"] = [lo, hi] if lo is not None else None
        r["oos_trade_list"] = bool(oos)
        r["trials_counted"] = (res.get("dsr") or {}).get("trials")
        r["family"] = _family(lab.read_json(os.path.join(out, r["name"], "spec.json")) or {})[0]
        r["tested"] = [res["data"].get("tested_from"), res["data"].get("to")]
        if oos and regs:
            ev = ct.evidence(oos, regs, cell, None, "out of sample")
            r["regime_now"] = {"cell": cell, "trades": ev["trades_cell"], "wins": ev["wins_cell"],
                               "win_rate_ci": ev.get("p_cell_ci"), "expectancy_r": ev.get("ev_r_cell_raw")}
        else:
            r["regime_now"] = None
        r["does_not_prove"] = lab.does_not_prove(res)
    order = {"PROMOTE": 0, "WATCH": 1, "DEMOTE": 2, "REJECT": 3}

    def key(r):
        ci = r["oos_expectancy_r_ci"]
        return (order.get(r["verdict"], 4), -(ci[0] if ci else -99))
    rows.sort(key=key)
    fams = sorted(set(r["family"] for r in rows))
    return {"regime_now": {"cell": cell, "words": words, "as_of_close": close}, "rows": rows,
            "ranked_by": "verdict, then the lower end of the out-of-sample expectancy's 95% range",
            "families": fams}, \
        ["Choosing the best of %d compared strategies is one more selection: the top row's numbers flatter it. "
         "Overlapping ranges mean the data cannot tell them apart." % len(rows), LAB_LIMITS, "Read only."]


def t_best_for_regime(kit, a, call):
    lab = api.skill("lab")
    ct = api.skill("cotrader")
    st = dict(ct.SETTINGS)
    now, regs, cell, words, close = _regime_now_cell(kit, "best_for_regime", ct, lab)
    demoted = (ct.read_json(ct.demoted_path(kit.workdir), {}) or {}).get("strategies") or {}
    today = now.date().isoformat()
    out = []
    for row in ct.strategies(kit.p("lab")):
        tier, tier_why = ct.tier_of(row, demoted)
        trades, basis = ct.history(row, {}, None, st)
        trades = [t for t in trades if t.get("exit_date", "") < today]
        ev = ct.evidence(trades, regs, cell, st, basis)
        out.append({"name": row["name"], "title": row["doc"].get("title"), "kind": row["doc"].get("kind") or "equity",
                    "timeframe": row["doc"].get("timeframe"), "verdict": row.get("verdict"), "tier": tier,
                    "tier_why": tier_why, "basis": basis, "trades_in_cell": ev["trades_cell"],
                    "wins_in_cell": ev["wins_cell"], "win_rate_in_cell": ev.get("p_cell_raw"),
                    "win_rate_in_cell_ci": ev.get("p_cell_ci"), "p": ev.get("p"), "p_ci": [ev.get("p_lo"),
                                                                                          ev.get("p_hi")],
                    "expectancy_r_in_cell": ev.get("ev_r_cell_raw"), "ev_r": ev.get("ev_r"),
                    "ev_r_ci": [ev.get("ev_lo"), ev.get("ev_hi")], "trades_all": ev["trades_all"],
                    "fits": ev.get("fits"), "enough": ev["trades_cell"] >= int(st["min_cell_trades"]),
                    "edge_cells": [ct.cell_words(c) for c in ct.edge_cells(trades, regs, int(st["min_cell_trades"]))],
                    "why": ev.get("why")})
    tier_rank = {"PROMOTED": 0, "WATCH": 1, "RESEARCH": 2}

    def key(r):
        lo = r["ev_r_ci"][0]
        return (0 if r["fits"] else (1 if r["fits"] is None else 2), tier_rank.get(r["tier"], 3),
                -(lo if lo is not None else -99))
    out.sort(key=key)
    fit = [r for r in out if r["fits"] and r["tier"] in ("PROMOTED", "WATCH")]
    if not out:
        headline = "No lab runs yet: there is no evidence for any regime. Run a spec through the lab first."
    elif not fit:
        headline = ("Nothing validated shows an edge in %s with at least %d trades: the honest answer is no "
                    "strategy for this regime." % (ct.cell_words(cell), int(st["min_cell_trades"])))
    else:
        headline = "%d validated strateg%s with an edge so far in %s (see n and the ranges)." % (
            len(fit), "y" if len(fit) == 1 else "ies", ct.cell_words(cell))
    return {"regime_now": {"cell": cell, "words": words, "as_of_close": close}, "headline": headline,
            "strategies": out, "min_cell_trades": int(st["min_cell_trades"]),
            "ranked_by": "fits today's cell, then tier, then the lower end of the expected R's 95% range"}, \
        ["The regime cell is NIFTY's trend (ADX) and India VIX bucket as of the close before; a cell with few trades "
         "says little, so the shrunk p leans on the rule's overall rate. Past trades in a regime are not a forecast "
         "of this one.", COTRADER_LIMITS, "Read only."]


# ---- trade_brief

def t_trade_brief(kit, a, call):
    ct = api.skill("cotrader")
    out, gaps = {}, []
    now, data = _cotrader_args(kit, "trade_brief")

    def part(key, fn):
        try:
            out[key] = fn()
        except (ToolError, api.ApiError) + api.skill_errors() as e:
            out[key] = None
            gaps.append("%s: unavailable (%s)" % (key, getattr(e, "message", None) or e))

    call.progress(0.05, "reading the market regime")

    def regime():
        d, _ = t_regime_now(kit, {}, call)
        return [{"symbol": r.get("symbol"), "words": r.get("words"), "as_of": r.get("as_of"),
                 "source": r.get("source"), "today": r.get("today")} for r in d.get("regimes") or []]
    part("regime", regime)
    call.progress(0.3, "checking today's candidates (nothing is written)")

    def cands():
        doc = ct.run_today(kit.workdir, "validated", None, now, data, None, not kit.offline, False)
        rows = [{"id": c["id"], "tier": c["tier"], "symbol": c["symbol"], "strategy": c["strategy"],
                 "status": c["status"], "p": c["evidence"].get("p"), "p_ci": [c["evidence"].get("p_lo"),
                                                                          c["evidence"].get("p_hi")],
                 "n_in_regime": c["evidence"].get("trades_cell"), "n_all": c["evidence"].get("trades_all"),
                 "ev_r": c["evidence"].get("ev_r"), "ev_r_ci": [c["evidence"].get("ev_lo"), c["evidence"].get("ev_hi")],
                 "risk": (c.get("risk") or {}).get("verdict"), "why_not": c.get("why_not")}
                for c in doc.get("candidates") or []]
        out["no_edge"] = doc.get("no_edge")
        out["risk_state"] = doc.get("risk_state")
        out["regime_cell"] = doc.get("regime_now")
        return rows
    part("candidates", cands)
    call.progress(0.7, "reading the paper account")

    def account():
        v = api.account_view(kit.ctx, live_quotes=False)
        return {"cash": v.get("cash"), "free_cash": v.get("free_cash"), "positions": v.get("positions"),
                "working_legs": v.get("working_legs"), "margin_blocked": v.get("margin_blocked"),
                "today": v.get("today")}
    part("open_risk", account)
    call.progress(0.85, "reading the calibration of past odds")

    def calib():
        rep = ct.calibration_report(kit.workdir, now, data, None, not kit.offline, False)
        return {"overall": rep.get("overall"), "words": rep.get("words"), "resolved": rep.get("resolved"),
                "pending": rep.get("pending")}
    part("calibration", calib)
    out["unavailable"] = gaps
    ready = [c for c in out.get("candidates") or [] if c["status"] == "ready"]
    lang = lang_of(kit, a)
    out["lang"] = lang
    out["headline"] = say("brief_ready", lang, n=len(ready)) if ready else say("brief_none", lang)
    return out, ["One read-only look: regime, candidates (not logged, not written), the paper account and the "
                 "calibration. Quote n and the 95% ranges with every probability.", PROPOSE_NOTE, COTRADER_LIMITS]


# ---------------------------------------------------------------- the tool table

RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


def tool(name, title, description, fn, props=None, required=None, data=None, annotations=None, network=True):
    props = dict(props or {})
    props.setdefault("lang", LANG)          # every tool takes it; the ones with text summaries use it
    ann = dict(annotations or RO)
    ann["title"] = title
    ann.setdefault("openWorldHint", network)
    return {"name": name, "title": title, "description": description.strip() + " " + DISCLAIMER,
            "inputSchema": obj(props, required), "outputSchema": envelope_schema(data or ANY_OBJ),
            "annotations": ann, "fn": fn, "network": network}


def build_tools():
    T = []
    T.append(tool("market_status", "NSE market status",
                  "Is NSE open right now? The session phase, today's hours, the next open, special sessions, from "
                  "the NSE calendar (no network). verify: true also asks nseindia.com.",
                  t_market_status, {"verify": S("boolean", "also ask nseindia.com's market status (network)")},
                  data=obj({"open": S("boolean"), "phase": S("string"), "now_ist": S("string")},
                           ["open"], extra=True)))
    scan_props = {"symbols": SYMBOLS, "limit": S("integer", "scan only the first N symbols", minimum=1, maximum=3000),
                  "min_price": S("number", "rupees (default: config min_price)", minimum=0),
                  "min_turnover": S("number", "rupees a day (default: config min_turnover)", minimum=0)}
    scan_data = obj({"mode": S("string"), "hits": ANY_LIST, "as_of": {}}, ["mode", "hits"], extra=True)
    T.append(tool("scan_daily", "Daily setups scan",
                  "Pre-market scan of the NSE universe (trader/universe.txt or symbols) on daily closes: breakouts, "
                  "pullbacks, RSI, liquidity filters. Nothing is written.", t_scan_daily, scan_props, data=scan_data))
    orb_props = dict(scan_props, at=HHMM, until=HHMM,
                     or_minutes=S("integer", "opening range length in minutes (default 15)", minimum=5, maximum=60))
    T.append(tool("scan_orb", "Opening-range breakout scan",
                  "Opening-range breakouts so far today across the universe, on 5-minute bars. Nothing is written.",
                  t_scan_orb, orb_props, data=scan_data))
    T.append(tool("news", "News and announcements",
                  "Headlines, NSE corporate announcements, upcoming board meetings and today's move for your "
                  "holdings, plan and watchlist (or tickers). Read only (the seen-items state is not updated).",
                  t_news, {"tickers": SYMBOLS, "hours": S("number", "ignore items older than this", minimum=1,
                                                         maximum=720),
                           "market": S("boolean", "add NIFTY 50 headlines and move"),
                           "include_seen": S("boolean", "show items already seen too")},
                  data=obj({"items": ANY_LIST}, ["items"], extra=True)))
    T.append(tool("entry_check", "Entry trigger check",
                  "Has this stock's setup triggered? Opening-range breakout (orb) or a close above a level, bar by "
                  "bar on 5m or 1m Yahoo bars, inside the entry window. save: true writes trader/entry.json for "
                  "exit_plan.", t_entry_check,
                  {"symbol": SYMBOL, "setup": S("string", enum=["orb", "level"]), "level": PRICE,
                   "or_minutes": S("integer", minimum=5, maximum=60), "interval": S("string", enum=["5m", "1m"]),
                   "save": S("boolean", "write trader/entry.json (keeps a .bak)")}, ["symbol"],
                  data=obj({"status": S("string"), "symbol": S("string")}, ["status"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}))
    T.append(tool("exit_plan", "Stop and target plan",
                  "Set the stop and target for a long entry, from trader/entry.json or from symbol + entry "
                  "(+ or_high/or_low): or-mid, or-low, pct:X, atr:K or price:X stops; 1R/1.5R/2R or price:X targets; "
                  "the flat-by time and a charges estimate. save: true writes trader/plan.json.", t_exit_plan,
                  {"symbol": SYMBOL, "entry": PRICE, "or_high": PRICE, "or_low": PRICE,
                   "or_minutes": S("integer", minimum=5, maximum=60), "setup": S("string", enum=["orb", "level"]),
                   "date": DATE, "time": HHMM, "interval": S("string", enum=["5m", "1m"]),
                   "stop": S("string", "or-mid, or-low, price:X, pct:X or atr:K", maxLength=32),
                   "target": S("string", "1R, 1.5R, 2R or price:X", maxLength=32), "flat_by": HHMM,
                   "qty": S("integer", "shares for the charges estimate", minimum=1, maximum=1000000),
                   "save": S("boolean", "write trader/plan.json (keeps a .bak)")},
                  data=obj({"symbol": S("string"), "entry": S("number"), "stop": S("number"),
                            "target": S("number")}, ["symbol", "entry", "stop", "target"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
                  network=False))
    T.append(tool("timemachine_run", "Time machine replay",
                  "Replay the plan (trader/plan.json, or symbol/entry/stop/target) over thousands of simulated "
                  "sessions drawn from the stock's own recent 5-minute bars, plus the real history: odds of the "
                  "target before the stop, R after charges, a fan of outcomes. Nothing is written.",
                  t_timemachine_run,
                  {"symbol": SYMBOL, "entry": PRICE, "stop": PRICE, "target": PRICE, "date": DATE,
                   "entry_time": HHMM, "replays": S("integer", "simulated sessions (default 2000)", minimum=100,
                                                    maximum=10000),
                   "seed": S("integer", "random seed (default 19)", minimum=0, maximum=2 ** 31 - 1)},
                  data=obj({"plan": ANY_OBJ}, ["plan"], extra=True)))
    T.append(tool("size_trade", "Position size",
                  "How many shares the plan allows under your rules: risk per trade, max position, cash buffer, "
                  "charges, tick size. Reads trader/plan.json and trader/config.json. Nothing is written.",
                  t_size_trade,
                  {"capital": S("number", "override the capital in rupees", exclusiveMinimum=0),
                   "limit": PRICE, "market": S("boolean", "a MARKET entry instead of a LIMIT")},
                  data=obj({"sizing": ANY_OBJ}, ["sizing"], extra=True), network=False))
    T.append(tool("risk_check", "Risk check",
                  "trader-risk on trader/orders.json: every order against your limits (size, loss at stop + charges, "
                  "reward:risk, daily loss, trades, entry window, duplicates), the kill switch, the circuit-breakers "
                  "(losing streak, daily drawdown, cooling-off, revenge trades) and the order rate; F&O legs as one "
                  "basket (lots, ban list, premium, margin, Greeks, scenarios). A preview by default; record: true "
                  "records the verdicts the paper broker needs.", t_risk_check,
                  {"record": S("boolean", "record the verdicts in trader/state/risk.json")},
                  data=ANY_OBJ, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
                  network=False))
    T.append(tool("risk_scenarios", "Risk scenarios",
                  "The F&O book's net Greeks (delta lots, gamma, vega, theta) and its P&L after charges on a grid of "
                  "spot +/-1/2/3 sigma x IV +/-5/10 vol, against your limits; open positions alone or with every F&O "
                  "proposal in trader/orders.json. Also the kill switch and circuit-breaker state. Read only.",
                  t_risk_scenarios,
                  {"book": S("string", "open: open positions only; with_proposals (default): plus every F&O "
                                       "proposal as if filled", enum=["open", "with_proposals"])},
                  data=obj({"risk": ANY_OBJ, "limits": ANY_LIST, "kill": ANY_OBJ}, ["risk", "limits", "kill"],
                           extra=True), network=False))
    T.append(tool("paper_orders_show", "Paper order tickets",
                  "The order tickets in trader/orders.json, in plain words, each with its trader-risk status and "
                  "fingerprint.", t_paper_orders_show, {},
                  data=obj({"tickets": ANY_LIST}, ["tickets"], extra=True), network=False))
    T.append(tool("paper_order_draft", "Draft paper orders",
                  "Size trader/plan.json (or, with candidate: ID, that co-trader candidate's plan) into order tickets "
                  "(trader/orders.json, the old one kept as .bak) and record trader-risk's verdicts. Returns each "
                  "ticket with its fingerprint. Places nothing.",
                  t_paper_order_draft,
                  {"capital": S("number", "override the capital in rupees", exclusiveMinimum=0),
                   "limit": PRICE, "market": S("boolean", "a MARKET entry instead of a LIMIT"),
                   "persona": PERSONA,
                   "candidate": S("string", "a co-trader candidate ID (cotrader_candidates): its plan becomes "
                                            "trader/plan.json first (the old one kept as .bak)",
                                  pattern=COTRADER_ID_RE, maxLength=90)},
                  data=obj({"tickets": ANY_LIST, "risk": ANY_OBJ, "next_step": S("string")},
                           ["tickets", "risk", "next_step"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
                  network=False))
    T.append(tool("paper_order_confirm", "Confirm one paper order",
                  "Record the USER's yes for ONE ticket and place it on the PAPER account (no real money; no live "
                  "broker exists in this server). Needs the ticket's index and its exact risk-PASS fingerprint. "
                  "When the client supports elicitation the server asks the user directly and yes_text is ignored; "
                  "otherwise yes_text must be the user's own reply, word for word, after you showed them the "
                  "ticket. Never write a yes yourself.", t_paper_order_confirm,
                  {"index": S("integer", "the ticket number, from paper_orders_show", minimum=1, maximum=100),
                   "risk_fingerprint": S("string", "the ticket's fingerprint (16 hex characters)",
                                         pattern=FP_RE.pattern),
                   "yes_text": S("string", "the user's reply, word for word (only when the client cannot ask)",
                                 minLength=1, maxLength=500)},
                  ["index", "risk_fingerprint"],
                  data=obj({"confirmed": S("boolean"), "index": S("integer"), "fingerprint": S("string"),
                            "via": S("string", enum=["elicitation", "argument"])},
                           ["confirmed", "index", "fingerprint", "via"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
                  network=False))
    T.append(tool("portfolio_status", "Paper portfolio",
                  "The paper account: cash, positions marked to a delayed price, P&L if closed now after charges, "
                  "working stop/target legs, today's fills and limits.", t_portfolio_status,
                  {"live_quotes": S("boolean", "mark positions to a delayed Yahoo quote (default true)")},
                  data=obj({"cash": S("number"), "positions": ANY_LIST, "today": ANY_OBJ, "fno": ANY_OBJ,
                            "margin_blocked": S("number")}, ["cash", "positions"], extra=True)))
    T.append(tool("paper_mtm", "Paper F&O mark to market",
                  "The paper F&O book marked to the latest chain (trader/options.json): positions grouped by "
                  "strategy, P&L (open and realised), Black-76 Greeks per leg, per strategy and in all, margin "
                  "blocked, and the payoff at expiry of each open structure. Read only.", t_paper_mtm,
                  {"strategy": S("string", "only this strategy id, like S-20261102-1", pattern=r"^S-\d{8}-\d{1,4}$")},
                  data=obj({"positions": ANY_LIST, "strategies": ANY_LIST, "totals": ANY_OBJ},
                           ["positions", "strategies", "totals"], extra=True), network=False))
    T.append(tool("options_chain", "Option chain",
                  "NSE option chain for an index or F&O stock and expiry: LTP, bid/ask, OI and change, volume, our "
                  "Black-76 IV and Greeks per strike around the money, lot size, forward, ATM straddle.",
                  t_options_chain,
                  {"symbol": S("string", "NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 or an F&O stock",
                               pattern=SYMBOL_RE.pattern, maxLength=24),
                   "expiry": S("string", "DD-Mon-YYYY (default: the nearest)", pattern=r"^\d{2}-[A-Za-z]{3}-\d{4}$"),
                   "strikes": S("integer", "strikes each side of ATM (default 10)", minimum=1, maximum=30)},
                  ["symbol"], data=obj({"underlying": S("string"), "rows": ANY_LIST}, ["underlying", "rows"],
                                       extra=True)))
    T.append(tool("options_summary", "Options summary",
                  "One expiry in numbers: PCR (OI and volume), max pain, call and put walls, ATM IV, 25-delta skew, "
                  "expected move from the straddle and from IV, IV rank and percentile from India VIX (NIFTY).",
                  t_options_summary,
                  {"symbol": S("string", "NIFTY, BANKNIFTY, ... or an F&O stock", pattern=SYMBOL_RE.pattern,
                               maxLength=24),
                   "expiry": S("string", "DD-Mon-YYYY (default: the nearest)", pattern=r"^\d{2}-[A-Za-z]{3}-\d{4}$")},
                  ["symbol"], data=obj({"underlying": S("string"), "max_pain": {}, "expected_move": {}},
                                       ["underlying"], extra=True)))
    T.append(tool("results_analyse", "Quarterly results",
                  "Quarterly results from the company's XBRL filings with NSE: revenue, profit, margins, EPS, "
                  "QoQ and YoY changes, plain-words notes and the next board meeting.", t_results_analyse,
                  {"symbols": SYMBOLS, "quarters": S("integer", "quarters to show (default 5)", minimum=1, maximum=8),
                   "standalone": S("boolean", "standalone instead of consolidated")}, ["symbols"],
                  data=ANY_LIST))
    T.append(tool("insider_deals", "Bulk and block deals",
                  "NSE bulk and block deals: who bought or sold how much, tagged when you hold or watch the stock "
                  "or the client is on your watch list.", t_insider_deals,
                  {"symbol": SYMBOL, "client": S("string", "part of the client name", maxLength=80),
                   "min_value_cr": S("number", "only deals worth at least this many crore", minimum=0),
                   "side": S("string", enum=["BUY", "SELL"]), "kind": S("string", enum=["bulk", "block", "all"]),
                   "days": S("integer", "the last N calendar days", minimum=1, maximum=365),
                   "mine": S("boolean", "only stocks you hold or watch"),
                   "limit": S("integer", minimum=1, maximum=500)},
                  data=obj({"deals": ANY_LIST}, ["deals"], extra=True)))
    T.append(tool("insider_disclosures", "Insider trading disclosures",
                  "SEBI PIT insider trading disclosures filed with NSE: promoters, directors and employees buying or "
                  "selling, net by stock.", t_insider_disclosures,
                  {"symbol": SYMBOL, "days": S("integer", "the last N days (default 7)", minimum=1, maximum=365),
                   "promoters_only": S("boolean"), "market_only": S("boolean", "market purchases and sales only"),
                   "mine": S("boolean", "only stocks you hold or watch"),
                   "max_filings": S("integer", "PIT filings to open (default 60)", minimum=1, maximum=200)},
                  data=obj({"rows": ANY_LIST}, ["rows"], extra=True)))
    T.append(tool("config_get", "Your settings",
                  "trader/config.json, the effective settings with defaults filled in, validation errors and "
                  "warnings, and a plain-words summary. Read only.", t_config_get,
                  {"with_schema": S("boolean", "include every known key with its default and meaning")},
                  data=obj({"exists": S("boolean"), "effective": ANY_OBJ}, ["exists", "effective"], extra=True),
                  network=False))
    lab_name = S("string", "a library name (lab_list) or a strategy in trader/lab/, e.g. orb15-vwap",
                 pattern=LAB_NAME_RE.pattern, maxLength=48)
    lab_data = obj({"name": S("string"), "verdict": S("string", enum=["REJECT", "WATCH", "PROMOTE", "DEMOTE"]),
                    "verdict_why": S("string"), "results": ANY_OBJ, "scorecard": S("string"),
                    "does_not_prove": {"type": "array", "items": S("string")}},
                   ["name", "verdict", "results", "scorecard", "does_not_prove"], extra=True)
    T.append(tool("lab_list", "Strategy lab: library and runs",
                  "The Strategy Lab's built-in specs (strategies to test, not promises) and your lab runs in "
                  "trader/lab/ with their verdicts and out-of-sample numbers. Read only.", t_lab_list, {},
                  data=obj({"library": ANY_LIST, "runs": ANY_LIST}, ["library", "runs"], extra=True), network=False))
    T.append(tool("lab_run", "Strategy lab: run a backtest",
                  "Backtest a strategy spec bar by bar on NSE bars after Indian charges and slippage, with no "
                  "look-ahead (an options spec, kind options: day by day on the local archive's end-of-day F&O "
                  "chains, with margin, return on margin and tail losses); then walk-forward out of sample, the parameter neighbourhood, Monte Carlo, the "
                  "deflated Sharpe ratio, the probability of backtest overfitting, regimes and 1.5x/2x costs. "
                  "Pre-registered gates give REJECT, WATCH (paper forward test next) or PROMOTE. Writes "
                  "trader/lab/<name>/. Sends progress and stops at max_seconds. Each run is a trial: the deflated "
                  "Sharpe is charged for every other spec of the same family run from this server "
                  "(trader/state/lab-trials.jsonl). Expect most strategies to be rejected.", t_lab_run,
                  {"strategy": lab_name,
                   "symbols": S("string", "NSE symbols to test on instead of the spec's universe, comma separated "
                                          "(a new variant: it counts as another trial)", maxLength=400,
                                pattern=r"^[A-Za-z0-9&\-,\s]{1,400}$"),
                   "seed": S("integer", "Monte Carlo seed (default: the spec's)", minimum=0, maximum=2 ** 31 - 1),
                   "max_seconds": S("integer", "time budget in seconds (default 180)", minimum=5, maximum=600)},
                  ["strategy"], data=lab_data,
                  annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}))
    T.append(tool("lab_report", "Strategy lab: scorecard",
                  "The plain-words scorecard of a lab run: the verdict, every gate, the robustness checks and what "
                  "the run does NOT prove. Read only.", t_lab_report,
                  {"strategy": lab_name, "include_trades": S("boolean", "add the out-of-sample trades (up to 500)")},
                  ["strategy"], data=lab_data, network=False))
    run_name = S("string", "a discovery run's name (discover_report lists them), e.g. nifty50-1d-s7-b100",
                 pattern=RUN_NAME_RE.pattern, maxLength=64)
    T.append(tool("discover_strategies", "Strategy discovery: search with overfitting controls",
                  "Search trader-lab's rule vocabulary for an edge with a FIXED trial budget declared up front: "
                  "train / validation / untouched holdout split by time (purged, embargoed), every trial logged and "
                  "counted, then cost stress, parameter neighbourhood, regime consistency, the deflated Sharpe with "
                  "the true trial count, overfitting odds over the trial matrix, a Reality Check / SPA bootstrap and "
                  "a false-discovery cut; the holdout is opened once per finalist. Finalists become trader-lab specs "
                  "(WATCH at best). Sends progress, stops at max_seconds and on cancel, and resumes when called "
                  "again with the same name. Writes trader/discover/<name>/. Expect zero finalists.",
                  t_discover_strategies,
                  {"universe": S("string", "nifty50 (default), lab10 or NSE symbols, comma separated", maxLength=400,
                                 pattern=r"^[A-Za-z0-9&\-,\s]{1,400}$"),
                   "timeframe": S("string", "1d (default, swing CNC) or 5m (intraday MIS)", enum=["1d", "5m"]),
                   "budget": S("integer", "trials, declared up front (default 100)", minimum=10, maximum=2000),
                   "seed": S("integer", "the sampling seed (default 7)", minimum=0, maximum=2 ** 31 - 1),
                   "search": S("string", "random (default) or grid", enum=["random", "grid"]),
                   "name": run_name,
                   "max_seconds": S("integer", "time budget in seconds (default 300); the run pauses, resumable",
                                    minimum=5, maximum=1800)},
                  data=obj({"name": S("string"), "state": S("string"), "next_step": S("string")},
                           ["name", "state", "next_step"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}))
    T.append(tool("discover_report", "Strategy discovery: report",
                  "Without a name: every discovery run in trader/discover/ with its state and funnel. With a name: "
                  "the plain-words report (N trials, M survived validation, K survived holdout and multiple "
                  "testing), the whole-search tests, a scorecard per validation survivor, the finalists' lab "
                  "verdicts and what the run does NOT show. Read only.", t_discover_report, {"name": run_name},
                  data=ANY_OBJ, network=False))
    T.append(tool("lab_forward", "Strategy lab: paper forward test",
                  "Run a WATCH or PROMOTE strategy on the latest bars: settle its signals since the backtest into "
                  "the rolling forward record (a decayed PROMOTE is demoted) and list today's fresh signals as draft "
                  "plans. draft: SYMBOL makes that plan trader/plan.json (keeps a .bak) and runs paper_order_draft "
                  "(trader-budget sizes it, trader-risk checks it). For an options spec, draft: UNDERLYING makes "
                  "today's basket trader/strategy.json and trader/orders.json and runs trader-risk on it as one "
                  "basket. Places nothing: a paper order still needs the "
                  "user's own yes through paper_order_confirm.", t_lab_forward,
                  {"strategy": lab_name, "draft": SYMBOL, "persona": PERSONA}, ["strategy"],
                  data=obj({"forward": ANY_OBJ, "drafted": {}, "next_step": S("string")},
                           ["forward", "next_step"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}))
    common = {
        "symbol": S("string", "NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 or an F&O stock",
                    pattern=SYMBOL_RE.pattern, maxLength=24),
        "expiry": S("string", "the near expiry, DD-Mon-YYYY (default: the nearest)", pattern=r"^\d{2}-[A-Za-z]{3}-\d{4}$"),
        "fill": S("string", "mid: the chain's price (default); touch: buy at the ask, sell at the bid",
                  enum=["mid", "touch"]),
        "at": S("string", "horizon of the 'today' curve and the probabilities: today, expiry (default) or YYYY-MM-DD",
                pattern=r"^(today|expiry|\d{4}-\d{2}-\d{2})$"),
        "iv_shift": S("number", "vol points added to every leg's IV (default 0)", minimum=-50, maximum=50),
        "seed": S("integer", "simulation seed (default 7): the same inputs give the same answer", minimum=0),
        "paths": S("integer", "simulated paths (default 5000)", minimum=500, maximum=20000),
        "simulate": S("boolean", "run the bootstrap simulation (default true)"),
        "include_curve": S("boolean", "include the payoff curve arrays (about 240 points each)"),
    }
    result = obj({"schema": {"const": "bharat-trader/strategy/1"}, "name": S("string"), "underlying": S("string"),
                  "legs": ANY_LIST, "net_premium": ANY_OBJ, "expiry": ANY_OBJ, "greeks": ANY_OBJ,
                  "margin": ANY_OBJ, "probability": ANY_OBJ, "charges": ANY_OBJ, "warnings": ANY_LIST},
                 ["schema", "name", "underlying", "legs", "expiry", "margin", "probability"], extra=True)
    names = [t["name"] for t in api.skill("strategy").list_templates()]
    build_props = dict(common)
    build_props.update({
        "template": S("string", "the strategy template", enum=names),
        "far_expiry": S("string", "calendar only: the far expiry, DD-Mon-YYYY", pattern=r"^\d{2}-[A-Za-z]{3}-\d{4}$"),
        "strike": S("number", "centre or long strike (default ATM)", exclusiveMinimum=0),
        "wings": S("number", "points from ATM to the short strikes (default the 1-SD expected move)", minimum=0),
        "width": S("number", "points from a short strike to its bought wing (default 2 strikes)", exclusiveMinimum=0),
        "delta": S("number", "pick the short strikes by delta instead of wings, e.g. 0.16", exclusiveMinimum=0,
                   exclusiveMaximum=1),
        "skip": S("number", "extra points on one butterfly wing (broken wing)", minimum=0),
        "ratio": S("integer", "short options per long in a ratio spread (default 2)", minimum=1, maximum=10),
        "lots": S("integer", "lots per leg (default 1)", minimum=1, maximum=100),
        "type": S("string", "calendar only: CE or PE", enum=["CE", "PE"]),
    })
    T.append(tool("strategy_build", "Build an option strategy",
                  "An option strategy from a template on the real NSE chain (spreads, straddles, strangles, iron "
                  "condor, iron fly, butterflies, ratios, covered call, protective put, calendar): payoff at expiry "
                  "and today, breakevens, max profit and loss (unlimited flagged), net premium and Greeks, the same "
                  "after 2026 F&O charges with the STT-on-exercise warning, margin required (what a broker would "
                  "block: a SPAN-style estimate with its error; the premium for bought options only) next to the "
                  "exact max loss and the return on margin, probability of profit textbook vs simulated with "
                  "expected value and 5% tail loss. Read only: it proposes nothing.", t_strategy_build,
                  build_props, ["template", "symbol"], data=result))
    price_props = dict(common)
    price_props["legs"] = S("string", "[+|-]LOTS CE|PE|FUT STRIKE [@DD-Mon-YYYY] [=PRICE], comma separated; STRIKE "
                                      "is 23000, ATM, ATM+200, D25 (delta 0.25) or P50 (premium 50)",
                            minLength=3, maxLength=600)
    T.append(tool("strategy_price", "Price custom option legs",
                  "Your own option legs (up to 12, strikes by number, offset from ATM, delta or premium, other "
                  "expiries allowed) with every number strategy_build gives. Read only: it proposes nothing.",
                  t_strategy_price, price_props, ["symbol", "legs"], data=result))
    broker_arg = {"broker": S("string", "the live broker to read (default: the config broker)",
                              enum=["upstox", "kite", "dhan"])}
    rows_data = obj({"connected": S("boolean"), "broker": {}, "rows": ANY_LIST, "read_only": {"const": True}},
                    ["connected", "rows", "read_only"], extra=True)
    T.append(tool("broker_status", "Broker connection",
                  "The live broker connection, read only and without network: the config's broker and mode, for "
                  "Upstox, Zerodha Kite and Dhan which BT_* environment variables are set (true/false, never the "
                  "values), what each API can do, the plan notes and the last static-IP preflight. Places nothing.",
                  t_broker_status, {}, data=obj({"mode": S("string"), "brokers": ANY_LIST}, ["mode", "brokers"],
                                                extra=True), network=False))
    T.append(tool("broker_holdings", "Broker holdings",
                  "Delivery holdings read from the live broker's API (Upstox, Zerodha Kite or Dhan): symbol, qty, "
                  "average, last price, P&L. Read only; needs the user's own token in the server's environment.",
                  t_broker_holdings, broker_arg, data=rows_data))
    T.append(tool("broker_positions", "Broker positions",
                  "Today's positions read from the live broker's API: symbol, exchange, product, qty, average, P&L. "
                  "Read only; needs the user's own token in the server's environment.",
                  t_broker_positions, broker_arg, data=rows_data))
    T.append(tool("doctor", "Data sources self-test",
                  "Probe every free data source the tools read (NSE equity list, market status, holidays, "
                  "announcements, board meetings, results, bulk/block deals, PIT, option chain and contract info, "
                  "lot sizes, F&O list, India VIX history; Yahoo chart, spark and headlines), one polite request "
                  "each (about 30 s): ok, slow, changed, down or blocked, since when, the tools affected and the "
                  "fallback in use. Freshness follows the NSE calendar in IST. Read only.", t_doctor,
                  {"only": S("string", "probe only NSE or only Yahoo", enum=["nse", "yahoo"])},
                  data=obj({"summary": ANY_OBJ, "sources": ANY_LIST, "market": S("string")},
                           ["summary", "sources"], extra=True)))
    T.append(tool("persona_scorecards", "Agent persona scorecards",
                  "Each agent persona's paper track record (agents/), rebuilt from trader/state/trades.json: closed "
                  "trades, wins and losses, net P&L after every charge, profit factor, expectancy, max drawdown, "
                  "losing streaks, entries per day, yes and no counts, and behaviour flags (overtrading, revenge "
                  "trades, exits after flat_by, fills with no ticket). Fills are credited through the persona tag "
                  "on the drafted proposal. Read only.", t_persona_scorecards,
                  {"persona": S("string", "one persona, e.g. intraday-trader (default: the whole team)",
                                enum=[p["name"] for p in api.personas()])},
                  data=obj({"personas": ANY_LIST, "unattributed": ANY_OBJ}, ["personas", "unattributed"], extra=True),
                  network=False))
    T.append(tool("regime_now", "Market regime now",
                  "The market regime in plain words for NIFTY, BANKNIFTY or NSE stocks (trader-regime): trending "
                  "up or down, ranging or no clear trend (ADX 14, efficiency ratio, 20/50 EMA slope, 200-day "
                  "average), the volatility bucket (India VIX percentile over a year, realised vol, IV rank), the "
                  "gap, breadth, weekly or monthly expiry today or next session, results day, RBI policy and Budget "
                  "days, holidays, the closing-auction window and the time-of-day bucket. Each day uses data up to "
                  "its own close only. From the local archive when present, else Yahoo daily bars. A description "
                  "of the market so far, not a forecast. Read only (board meetings are cached a day in "
                  "trader/regime/board/).", t_regime_now,
                  {"symbols": S("string", "NIFTY, BANKNIFTY or NSE stocks, comma separated (default NIFTY,BANKNIFTY)",
                                maxLength=200, pattern=r"^[A-Za-z0-9&\-,\s]{1,200}$"),
                   "iv": S("boolean", "a stock's IV rank from the archived F&O bhavcopy (slower)")},
                  data=obj({"regimes": ANY_LIST, "errors": ANY_LIST}, ["regimes"], extra=True)))
    T.append(tool("cotrader_candidates", "Co-trader: today's candidates",
                  "Today's trade candidates from strategies trader-lab validated (tier PROMOTED: gates plus a paper "
                  "forward record; WATCH: gates passed, paper only), or the plain decision to sit out with the "
                  "reasons and what would change them. Each candidate: the plan (entry, stop, target, size by "
                  "trader-budget, charges), trader-risk's PASS or BLOCK with reasons, the share of past trades in "
                  "today's market regime that made money after charges with its 95% range and n (shrunk toward the "
                  "rule's overall rate), the expected R after charges, a time-machine replay as a second opinion "
                  "and any disagreement, why, what would invalidate it, what the evidence does NOT show and similar "
                  "past trades. include_research adds RESEARCH ideas (unproven, for study/paper only). Writes "
                  "trader/cotrader.json and logs each prediction once for calibration; drafts and places nothing "
                  "(paper_order_draft with candidate: ID drafts one).", t_cotrader_candidates,
                  {"include_research": S("boolean", "also the RESEARCH tier: unproven, for study/paper only"),
                   "strategy": S("string", "only this lab strategy", pattern=LAB_NAME_RE.pattern, maxLength=48),
                   "refresh": S("boolean", "run it now (default true); false returns the saved trader/cotrader.json")},
                  data=obj({"candidates": ANY_LIST, "no_edge": {}, "strategies": ANY_LIST, "regime_now": ANY_OBJ,
                            "next_step": S("string")}, ["candidates", "strategies", "next_step"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}))
    T.append(tool("cotrader_explain", "Co-trader: explain a candidate",
                  "One candidate from the last co-trader run in full: why this trade, what would invalidate it, the "
                  "evidence in numbers (sample sizes, Wilson and posterior 95% ranges, expected R after charges), "
                  "the time-machine second opinion and disagreements, what the evidence does NOT show, the nearest "
                  "similar past trades and the risk verdict, plus the plain-words text. Without an id: the last "
                  "run's candidate IDs. Read only.",
                  t_cotrader_explain,
                  {"id": S("string", "the candidate ID, e.g. 2026-11-02-rsi2-nifty50-SBIN", pattern=COTRADER_ID_RE,
                           maxLength=90)}, data=ANY_OBJ, network=False))
    T.append(tool("calibration_report", "Co-trader: calibration report",
                  "How well the co-trader's past probabilities matched what happened: each logged prediction's "
                  "outcome (paper fills, then the lab's forward record, then the bars after the signal), the Brier "
                  "score with its base-rate reference, log loss and reliability bins with Wilson ranges, overall, "
                  "per strategy and per tier, and which strategies are overconfident enough to demote. Read only: "
                  "it never demotes.", t_calibration_report, {},
                  data=obj({"overall": ANY_OBJ, "by_strategy": ANY_OBJ, "by_tier": ANY_OBJ, "words": S("string")},
                           ["overall", "by_strategy", "by_tier", "words"], extra=True)))
    date = S("string", "a date, YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")
    T.append(tool("journal_summary", "Trade journal summary",
                  "The user's trade journal (trader-journal), rebuilt from paper fills (trader/state/trades.json) and "
                  "imported broker trade books: closed trades after charges, expectancy with its 95% range, win rate "
                  "with its Wilson interval, profit factor, drawdown, streaks, this week's numbers, the rule-break log "
                  "(risk overrides, trades after a circuit-breaker lock, revenge trades, oversizing, entries outside "
                  "the window, late exits, fills with no ticket), the overtrading index, the cost drag, the "
                  "counterfactual ledger's totals and the latest trades. Read only.", t_journal_summary,
                  {"recent": S("integer", "how many recent trades to list (default 10)", minimum=0, maximum=200)},
                  data=obj({"stats": ANY_OBJ, "this_week": ANY_OBJ, "behaviour": ANY_OBJ, "counterfactual": ANY_OBJ,
                            "entries": ANY_LIST}, ["stats", "behaviour"], extra=True), network=False))
    T.append(tool("journal_stats", "Trade journal statistics",
                  "Statistics of the user's closed trades after charges (trader-journal): expectancy with its range, "
                  "win rate with its Wilson 95% interval, profit factor, average win and loss, max drawdown and "
                  "streaks, optionally broken down by strategy, persona, regime (NIFTY's trend and volatility the "
                  "close before), time of day, weekday or instrument, for all trades or one ISO week; with the "
                  "behaviour flags. Read only.", t_journal_stats,
                  {"by": S("string", "break down by", enum=["strategy", "persona", "regime", "tod", "weekday",
                                                            "instrument"]),
                   "week": S("string", "an ISO week like 2026-W44, or this / last",
                             pattern=r"^(\d{4}-?W\d{1,2}|this|last)$")},
                  data=obj({"stats": ANY_OBJ, "behaviour": ANY_OBJ}, ["stats"], extra=True), network=False))
    T.append(tool("counterfactual_report", "Counterfactual ledger",
                  "What the trades the user did not take would have done (trader-journal): every co-trader candidate "
                  "skipped or blocked by risk (trader/cotrader/log.jsonl) and every trader-risk BLOCK of the user's "
                  "own order, replayed on the bars after it with trader-lab's no-look-ahead fill rules (next bar's "
                  "open plus slippage, stop first, flat-by or maximum holding, net of charges): per group the plans, "
                  "how many were replayed, taken anyway or unresolved, what the losers would have lost (saved) and "
                  "the winners made (cost), with the caveats. Read only; bars from the local archive or the lab "
                  "cache, never fetched.", t_counterfactual_report,
                  {"group": S("string", "one group only", enum=["co-trader skipped", "co-trader blocked by risk",
                                                                "your orders blocked by risk"]),
                   "since": date, "until": date,
                   "limit": S("integer", "how many replay rows to return (default 50)", minimum=1, maximum=500)},
                  data=obj({"groups": ANY_OBJ, "rows": ANY_LIST, "caveats": ANY_LIST}, ["groups", "caveats"],
                           extra=True), network=False))
    T.append(tool("tax_summary", "Tax estimate for a financial year",
                  "A financial-year tax ESTIMATE of the user's trading (trader-tax), for discussion with a CA: each "
                  "trade classified as intraday equity (speculative business income), F&O (non-speculative business "
                  "income) or delivery shares (short-term gains at the s.111A rate, long-term above the yearly "
                  "exemption at the s.112A rate, with the 31 Jan 2018 grandfathering); charges per head (STT "
                  "deductible for business, not for capital gains); business turnover the ICAI Guidance Note way "
                  "(the absolute sum of profits and losses) with the tax-audit check in words; current-year set-off, "
                  "brought-forward losses, what carries forward and until when; the special-rate amounts; the "
                  "advance-tax dates; the ITR-3 schedule for each number; what needs the user's input. FY 2025-26 "
                  "under the Income-tax Act, 1961; tax year 2026-27 under the Income-tax Act, 2025. Read only.",
                  t_tax_summary,
                  {"fy": S("string", "financial year like 2025-26 (default: the current one)",
                           pattern=r"^\d{4}-\d{2}$"),
                   "book": S("string", "live (default: imported broker trade books), paper (a practice report) or "
                                       "all", enum=["live", "paper", "all"]),
                   "rules": S("boolean", "include each rule's full wording and source (default: section and date "
                                         "only)")},
                  data=obj({"fy": S("string"), "heads": ANY_OBJ, "turnover": ANY_OBJ, "audit": ANY_OBJ,
                            "setoff": ANY_OBJ, "taxable": ANY_OBJ, "disclaimer": S("string")},
                           ["fy", "heads", "turnover", "audit", "setoff", "taxable", "disclaimer"], extra=True),
                  network=False))
    # ---- v2: the co-trader workflow layer
    T.append(tool("archive_status", "Market data archive status",
                  "What the local NSE archive (trader-archive) holds: each dataset (equity and F&O bhavcopies, "
                  "indices, India VIX, ban list, settlement prices, lot sizes, corporate actions) with its files, "
                  "rows, first and last day and when it was fetched, intraday bars and option-chain snapshots, in "
                  "plain words. The lab's options backtests and the regime's IV ranks read it. Read only, no "
                  "network.", t_archive_status, {},
                  data=obj({"datasets": ANY_OBJ, "bytes": S("integer"), "words": S("string")},
                           ["datasets", "words"], extra=True), network=False))
    lib = api.skill("lab").library()
    design_props = {
        "template": S("string", "the trader-lab library spec to start from (lab_list describes each)",
                      enum=sorted(lib)),
        "name": S("string", "a name for the draft (default: a- plus the template plus a short hash)",
                  pattern=LAB_NAME_RE.pattern, maxLength=48),
        "symbols": S("string", "NSE symbols for the universe (equity) or the underlying (options), comma separated",
                     maxLength=400, pattern=r"^[A-Za-z0-9&\-,\s]{1,400}$"),
        "window_from": dict(HHMM, description="5-minute equity templates: no entries before this IST time, e.g. "
                                              "09:15 for the open drive"),
        "window_to": dict(HHMM, description="5-minute equity templates: no entries after this IST time, e.g. 10:00"),
        "days_to_expiry": S("integer", "options: enter this many sessions before expiry (0 = expiry day)",
                            minimum=0, maximum=30),
        "weekdays": S("string", "options: entry weekdays, e.g. Tue,Thu", pattern=WEEKDAY_RE.pattern, maxLength=40),
        "vix_min": S("number", "options: enter only when India VIX is at least this", minimum=0, maximum=200),
        "vix_max": S("number", "options: enter only when India VIX is at most this", minimum=0, maximum=200),
        "iv_rank_min": S("number", "options: enter only when the IV rank is at least this", minimum=0, maximum=100),
        "iv_rank_max": S("number", "options: enter only when the IV rank is at most this", minimum=0, maximum=100),
        "trend": S("string", "equity: up adds a 20 EMA above 50 EMA filter (long only: there is no down)",
                   enum=["any", "up"]),
        "regime_gate": S("boolean", "add the lab's regime gate: the trades in today's regime cell must show an edge"),
        "capital": S("number", "rupees the backtest sizes from", minimum=10000, maximum=1e9),
        "risk_per_trade_pct": S("number", "equity: % of capital lost at the stop", minimum=0.1, maximum=5),
        "max_trades_per_day": S("integer", "equity: the day's trade cap", minimum=1, maximum=20),
        "lots": S("integer", "options: lots per leg", minimum=1, maximum=50),
        "target_r": S("number", "equity: target in R (multiples of the risk at the stop)", minimum=0.5, maximum=10),
        "stop_atr_mult": S("number", "equity: stop at this many ATR(14) below the entry", minimum=0.5, maximum=10),
        "profit_target_pct": S("number", "options: take profit at this % of the credit", minimum=5, maximum=200),
        "stop_loss_mult": S("number", "options: stop when the loss reaches this many times the credit", minimum=0.5,
                            maximum=10),
        "params": S("string", "JSON object of the template's own params to change, e.g. {\"or_minutes\": 30}",
                    maxLength=600),
        "extra_entry": S("string", "equity: one more entry condition in the lab vocabulary as JSON, e.g. "
                                   "[{\"rsi\": 14}, \"<\", 70]", maxLength=1200),
        "note": S("string", "the idea in the user's words (kept in the spec's description)", maxLength=400),
    }
    T.append(tool("strategy_design", "Design a strategy (a lab spec draft)",
                  "Turn a structured intent (instrument, timeframe or time window such as the 09:15-10:00 open "
                  "drive or expiry day, regime conditions, risk budget, style = a library template) into a trader-lab "
                  "spec draft in the lab's fixed, safe vocabulary (fields, session values, indicators, comparisons; "
                  "no code), with every validation error, the strategy family and how many trials that family "
                  "already has. Never runs anything: lab_run_spec tests the draft.", t_strategy_design, design_props,
                  ["template"], data=obj({"spec": ANY_OBJ, "valid": S("boolean"), "errors": ANY_LIST,
                                          "family": S("string"), "next_step": S("string")},
                                         ["spec", "valid", "errors", "family", "next_step"], extra=True),
                  network=False))
    T.append(tool("lab_run_spec", "Strategy lab: run your own spec",
                  "Run a spec the user or the assistant wrote (JSON text, e.g. from strategy_design) through the "
                  "same lab as lab_run: after-charges backtest, walk-forward, deflated Sharpe, overfitting odds, "
                  "gates and a verdict. Trial-count honesty: every spec run here is logged in "
                  "trader/state/lab-trials.jsonl and increments the multiple-testing count of its strategy family, "
                  "so later variants face a higher bar; the result says how many trials were counted. Writes "
                  "trader/lab/<name>/. Sends progress, stops at max_seconds. Places nothing.", t_lab_run_spec,
                  {"spec": S("string", "the trader-lab spec as JSON text", minLength=2, maxLength=MAX_SPEC_TEXT),
                   "seed": S("integer", "Monte Carlo seed (default: the spec's)", minimum=0, maximum=2 ** 31 - 1),
                   "max_seconds": S("integer", "time budget in seconds (default 180)", minimum=5, maximum=600)},
                  ["spec"], data=lab_data,
                  annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}))
    T.append(tool("compare_strategies", "Strategy lab: compare runs",
                  "Two to eight lab runs side by side: verdict, out-of-sample trades, win rate with its Wilson 95% "
                  "range, expectancy in R after charges with its 95% range, profit factor, drawdown, deflated "
                  "Sharpe and the trials it was charged for, overfitting odds, 1.5x costs, gates passed, the strategy "
                  "family, and the trades in today's regime cell; ranked by verdict then the lower end of the "
                  "expectancy range, with what each does NOT prove. Read only.", t_compare_strategies,
                  {"strategies": S("string", "lab run names, comma separated", minLength=3, maxLength=400,
                                   pattern=r"^[a-z0-9\-,\s]{3,400}$")},
                  ["strategies"], data=obj({"rows": ANY_LIST, "regime_now": ANY_OBJ}, ["rows", "regime_now"],
                                           extra=True)))
    T.append(tool("best_for_regime", "Best strategies for today's regime",
                  "Every lab run's past trades filtered to today's regime cell (NIFTY trending or ranging x the India "
                  "VIX bucket, as of the close before): trades and wins in the cell with the Wilson 95% range, the "
                  "shrunk probability and expected R after charges with their 95% ranges, n overall, whether the cell "
                  "has enough trades to judge, the tier (PROMOTED, WATCH, RESEARCH) and the cells where it had an "
                  "edge. Ranked conservatively; says plainly when nothing fits. Read only.", t_best_for_regime, {},
                  data=obj({"regime_now": ANY_OBJ, "headline": S("string"), "strategies": ANY_LIST},
                           ["regime_now", "headline", "strategies"], extra=True)))
    T.append(tool("trade_brief", "Co-trader: one-shot trade brief",
                  "One read-only call for a co-trader brief: the regime now, today's validated candidates with their "
                  "odds, 95% ranges and n (worked out fresh, not written or logged), the no-edge reasons when there "
                  "are none, the risk locks, the open paper positions and today's limits used, and the calibration "
                  "of past odds. Proposing is not placing: a candidate becomes a paper ticket only through "
                  "paper_order_draft, trader-risk and the user's own typed yes.", t_trade_brief, {},
                  data=obj({"headline": S("string"), "regime": {}, "candidates": {}, "no_edge": {},
                            "open_risk": {}, "calibration": {}, "unavailable": ANY_LIST},
                           ["headline", "unavailable"], extra=True)))
    T.append(tool("autopilot_status", "Auto-pilot: status",
                  "The auto-pilot on PAPER: whether its loop is running (pid, last step), today's phase, the timeline "
                  "of today's actions (pre-open brief, orders placed with trader-risk PASS and the machine yes "
                  "labelled autopilot, fills, exits, blocks with reasons, kill switch, end of day), the open paper "
                  "positions by strategy, each strategy's capital bucket, its paper forward record after charges "
                  "next to trader-lab's verdict, today's report and past replays. Read only.", t_autopilot_status, {},
                  data=obj({"running": S("boolean"), "positions": ANY_LIST, "timeline": ANY_LIST, "records": ANY_OBJ,
                            "strategies": ANY_OBJ}, ["running", "positions", "timeline"], extra=True), network=False))
    T.append(tool("autopilot_replay", "Auto-pilot: replay a past day",
                  "Replay one or more past NSE sessions with the auto-pilot on a simulated clock, in a sandbox "
                  "(trader/autopilot/replays/): the enabled strategies' lab runs are re-run as of the close before the "
                  "first day (no look-ahead), a fresh paper account starts from your capital, and every step runs "
                  "from the pre-open to the end-of-day report, with exact charges. Read only for your account: it "
                  "never touches your paper account, orders or lab runs.", t_autopilot_replay,
                  {"date": S("string", "the first session, YYYY-MM-DD", pattern=DATE_RE),
                   "to": S("string", "the last session (default: date), at most a month later", pattern=DATE_RE)},
                  ["date"], data=obj({"sandbox": S("string"), "reports": ANY_LIST}, ["sandbox", "reports"]),
                  annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
                  network=False))
    T.append(tool("autopilot_start", "Auto-pilot: start on paper",
                  "Start the auto-pilot loop on the PAPER account, with the user's typed confirmation (the words "
                  "start paper). A config that is not paper is refused before anything. When the client supports "
                  "elicitation the server asks the user directly; otherwise typed must be the user's own words. "
                  "Never type them yourself. There is no live auto-pilot.", t_autopilot_start,
                  {"typed": S("string", "the user's own words, start paper (only when the client cannot ask)",
                              minLength=1, maxLength=50)},
                  data=obj({"running": S("boolean"), "via": S("string")}, extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
                  network=False))
    T.append(tool("autopilot_stop", "Auto-pilot: stop",
                  "Ask the running auto-pilot loop to finish its current step and stop, with the user's typed "
                  "confirmation (the words stop paper). Open paper positions stay as they are.", t_autopilot_stop,
                  {"typed": S("string", "the user's own words, stop paper (only when the client cannot ask)",
                              minLength=1, maxLength=50)},
                  data=obj({"stopped": S("string"), "via": S("string")}, ["stopped"], extra=True),
                  annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
                  network=False))
    return T


# ---------------------------------------------------------------- prompts

RULES = ("Use the bharat-trader tools for every number; do not guess prices or levels. Present the evidence "
         "(what each tool returned, its source and time) and the risks (what would make it wrong, the loss at the "
         "stop after charges). Never promise a profit, never call anything a sure thing, and say plainly that "
         "this is education and tooling, not financial advice. Paper trading only: an order needs trader-risk "
         "PASS and the user's own typed yes for that one ticket.")

EVIDENCE = ("Evidence first: before any opinion, show what the tools returned. With every probability or rate give n "
            "(the number of past trades it rests on) and its 95% interval; quote results after charges and slippage; "
            "name the market regime it was measured in; and list what the evidence does NOT show (few trades, one "
            "regime, in-sample, survivorship, model fills). Say so plainly when n is small or the interval spans "
            "zero, and treat 'no trade' as a valid answer. No promises: never say guaranteed, sure-shot or "
            "risk-free. Proposing is not placing: every order goes through risk_check and the user's own typed yes "
            "for that one ticket, on the paper account. Every spec you run with lab_run_spec or lab_run is a trial "
            "that raises the bar for its family: say how many were counted, and do not keep trying variants until "
            "one passes. ")


def build_prompts():
    return [
        {"name": "morning_briefing", "title": "Morning briefing",
         "description": "Before the open: market status, news on your stocks, the daily scan, your paper account.",
         "arguments": [{"name": "focus", "description": "symbols or a theme to focus on (optional)",
                        "required": False}],
         "text": ("Give me a pre-market briefing for NSE today. Call market_status, then news (market: true), "
                  "then scan_daily, then portfolio_status. Summarise: is the market open and when, what changed "
                  "overnight for my holdings and watchlist, which scan hits are liquid and why they matched, and "
                  "what I am already carrying. {focus}" + RULES)},
        {"name": "intraday_plan", "title": "Intraday plan",
         "description": "From an opening-range breakout to a sized, risk-checked paper ticket.",
         "arguments": [{"name": "symbol", "description": "NSE symbol, e.g. RELIANCE", "required": True}],
         "text": ("Help me plan an intraday trade in {symbol}. Call entry_check for {symbol}; if it has "
                  "triggered, call exit_plan (from the entry) and show the stop, target and flat-by; then "
                  "timemachine_run on that plan and size_trade. Only if I ask, save the plan and call "
                  "paper_order_draft, then show me the ticket in plain words and wait for my own typed yes before "
                  "paper_order_confirm. " + RULES)},
        {"name": "options_income_scan", "title": "Options income scan",
         "description": "Read one expiry's positioning and volatility before any option-selling idea.",
         "arguments": [{"name": "symbol", "description": "NIFTY, BANKNIFTY or an F&O stock", "required": True},
                       {"name": "expiry", "description": "DD-Mon-YYYY (optional)", "required": False}],
         "text": ("Look at {symbol} options{expiry_text}. Call options_summary and options_chain. Explain PCR, max "
                  "pain, the call and put walls, ATM IV, IV rank and the expected move, and what an option seller "
                  "would be exposed to (gap risk, unlimited loss on naked calls, margin, STT on exercise, lot size "
                  "in rupees). Do not recommend a trade. " + RULES)},
        {"name": "weekly_review", "title": "Weekly review",
         "description": "Look back at the week's paper trades, decisions and the rules that blocked orders.",
         "arguments": [],
         "text": ("Review my trading week. Call journal_stats (week: this) and journal_summary for the closed "
                  "trades after charges, the win rate with its interval, the rule breaks and the cost drag; "
                  "counterfactual_report for what the skipped and blocked trades would have done; then "
                  "portfolio_status, paper_orders_show, risk_check and config_get. Read "
                  "trader/journal/weekly-*.md if there is one. Summarise realised P&L after charges, what risk "
                  "blocked and why, every rule break with its time, and which rule I came closest to breaking. "
                  "Under 20 trades, review process only. Suggest one process change, not a stock. " + RULES)},
        {"name": "research_strategy", "title": "Research a strategy",
         "description": "Test an idea honestly: evidence for and against, after charges, out of sample.",
         "arguments": [{"name": "idea", "description": "the strategy idea in plain words", "required": True},
                       {"name": "symbol", "description": "a stock to try it on (optional)", "required": False}],
         "text": ("I want to research this strategy idea: {idea}.{symbol_text} Start in the Strategy Lab: call "
                  "lab_list and pick the closest built-in spec (say so if none fits, and what spec it would need), "
                  "then lab_run it{symbols_arg} for the after-charges backtest, walk-forward out of sample, parameter "
                  "neighbourhood, deflated Sharpe, probability of backtest overfitting, regimes and cost stress. "
                  "Read me the verdict, every failed gate and the does-not-prove list from lab_report. Use "
                  "scan_daily or scan_orb, entry_check, exit_plan and timemachine_run only to illustrate one real "
                  "example. Only a WATCH or PROMOTE goes on to the paper forward test (lab_forward each session; "
                  "draft a paper ticket only if I ask, and wait for my own typed yes). Separate what the data shows "
                  "from what it cannot show (few samples, one regime, costs, slippage, survivorship). Say what "
                  "result would make you drop the idea. " + RULES)},
        {"name": "daily_cotrader_brief", "title": "Daily co-trader brief",
         "description": "One evidence-first brief: regime, candidates with odds and n, open risk, calibration.",
         "arguments": [{"name": "focus", "description": "symbols or a question for today (optional)",
                        "required": False}],
         "text": ("Be my co-trader for today on NSE. Call trade_brief first, then best_for_regime. Open with the "
                  "regime in plain words (trend, volatility bucket, expiry or event day) and its inputs. For each "
                  "candidate: tier, the probability with its 95% interval and n in this regime and overall, the "
                  "expected R after charges with its interval, trader-risk's verdict and why; if there is none, say "
                  "that sitting out is the decision and give the no-edge reasons and what would change them. Then my "
                  "open positions and how much of today's loss limit is used, and how well past odds were "
                  "calibrated (Brier vs the base rate, with the number of resolved predictions). {focus}"
                  + EVIDENCE + RULES)},
        {"name": "find_best_trade_now", "title": "Find the best trade for this regime",
         "description": "Rank lab-tested strategies by their evidence in today's regime cell; no trade is an answer.",
         "arguments": [{"name": "universe", "description": "symbols or a theme to restrict to (optional)",
                        "required": False}],
         "text": ("Find the best trade for the current market regime{universe_text}. Call regime_now, then "
                  "best_for_regime, then cotrader_candidates (include_research only if I ask) and cotrader_explain "
                  "for the top one. Rank conservatively by the lower end of the expected R's 95% interval, not the "
                  "point estimate. Only strategies with at least the minimum trades in this regime cell can be "
                  "called a fit; say which ones rest on too few. Explain the best candidate's odds honestly, what "
                  "would invalidate it and what the evidence does NOT show. If nothing fits, say there is no edge "
                  "today. Do not draft an order unless I ask; then paper_order_draft and wait for my typed yes. "
                  + EVIDENCE + RULES)},
        {"name": "design_strategy_for_window", "title": "Design a strategy for a time window",
         "description": "From an idea for a window (expiry day, the 09:15-10:00 open drive) to a lab-tested spec.",
         "arguments": [{"name": "window", "description": "the window, e.g. expiry day, or 09:15-10:00",
                        "required": True},
                       {"name": "idea", "description": "the idea in plain words (optional)", "required": False},
                       {"name": "symbol", "description": "an index or stock (optional)", "required": False}],
         "text": ("Help me design a strategy for this window: {window}.{idea_text}{symbol_text} Call regime_now "
                  "and lab_list. Pick the closest library template and say why; for a clock window use a 5-minute "
                  "equity template with window_from and window_to; for expiry day use an options template with "
                  "days_to_expiry 0 (the equity vocabulary has no expiry condition: say so). Call strategy_design "
                  "with the structured intent (template, window, regime conditions, risk budget) and show me the "
                  "spec and every validation error. Pre-register before testing: the one spec you will run, the gate "
                  "that would make you drop it, and how many trials its family already has. Then lab_run_spec once "
                  "and read me the verdict, the trades counted, every failed gate and the does-not-prove list. A "
                  "WATCH is not a result yet: it needs the paper forward test (lab_forward). " + EVIDENCE + RULES)},
        {"name": "stress_test_my_idea", "title": "Stress-test my idea",
         "description": "Try hard to break an idea: costs, regimes, neighbours, trial count, out of sample.",
         "arguments": [{"name": "idea", "description": "the idea or a lab strategy name", "required": True}],
         "text": ("Stress-test this idea and try to break it: {idea}. If it is a lab run, call lab_report and "
                  "compare_strategies against its closest library cousin; otherwise draft it with strategy_design "
                  "and run it once with lab_run_spec. Check, with numbers: does it survive 1.5x and 2x costs; is "
                  "the parameter neighbourhood positive or is it one lucky point; how many out-of-sample trades and "
                  "how wide the expectancy's interval is; the deflated Sharpe with the trials counted (including "
                  "this family's earlier tries); the overfitting odds; which regime cells carry the result "
                  "(best_for_regime) and which lose. End with the strongest argument against the idea and what "
                  "result would make you drop it. " + EVIDENCE + RULES)},
        {"name": "review_my_week", "title": "Review my week (co-trader)",
         "description": "The week's trades after charges, rule breaks, skipped candidates and calibration.",
         "arguments": [{"name": "week", "description": "an ISO week like 2026-W44, or this / last (default this)",
                        "required": False}],
         "text": ("Review my trading week ({week}) as my co-trader. Call journal_stats (week: {week}) and "
                  "journal_summary for the closed trades after charges, the win rate with its interval and n, the "
                  "expectancy with its interval, and the rule breaks with their times; counterfactual_report for "
                  "what the candidates I skipped and the orders risk blocked would have done (with the counts and "
                  "caveats); calibration_report for how my co-trader's odds held up; and persona_scorecards. Under "
                  "20 trades, review the process only: a week is too short to judge an edge. Suggest one process "
                  "change, not a stock. " + EVIDENCE + RULES)},
    ] + persona_prompts()


def _persona_context(kit, persona):
    """What the persona needs to know right now, from the trader/ files and the NSE calendar (no network)."""
    if kit is None:
        return "(no server context: read trader/config.json and call persona_scorecards yourself)"
    inr = api.skill("broker").inr
    lines = []

    def part(label, fn):
        try:
            lines.append("- %s: %s" % (label, fn()))
        except (api.ApiError,) + api.skill_errors() as e:
            lines.append("- %s: unavailable (%s)" % (label, getattr(e, "message", None) or e))

    def market():
        st = api.market(kit.ctx)
        return "%s IST, %s (%s)" % (st["now_ist"], "NSE open" if st["open"] else "NSE closed", st.get("reason") or
                                    st.get("phase"))

    def limits():
        if not kit.ctx.config():
            return ("no trader/config.json yet: ask the user to set their own capital and limits with "
                    "python3 trader-budget/size.py init (until then the skills fall back to example defaults)")
        c = api.skill("broker").merged_config(kit.ctx.config())
        return ("capital %s, max loss per trade %s, max daily loss %s, max trades per day %s, entries %s to %s, "
                "flat by %s, %s" % (inr(c["capital"]), inr(c["max_loss_per_trade"]), inr(c["max_daily_loss"]),
                                    c["max_trades_per_day"], c["no_entries_before"], c["no_entries_after"],
                                    c["flat_by"], "paper account" if c.get("paper", True) else "NOT paper: "
                                    "every order is refused"))

    def lab():
        rows = api.lab_verdicts(kit.ctx)
        return ", ".join("%s %s" % (r["name"], r["verdict"]) for r in rows) or \
            "no lab runs yet (trader/lab/ is empty): nothing has a WATCH or PROMOTE verdict"

    def card():
        c = api.persona_scorecards(kit.ctx, persona["name"])["personas"][0]
        out = ("%d closed paper trades, net %s after charges, losing streak now %d, %s"
               % (c["closed"], inr(c["net_pnl"]), c["losing_streak_now"], c["verdict"]))
        if c["flags"]:
            out += " Flags: " + "; ".join(f["text"] for f in c["flags"][-5:]) + "."
        return out

    part("now", market)
    part("your limits (trader/config.json)", limits)
    part("Strategy Lab verdicts", lab)
    part("your paper scorecard", card)
    return "\n".join(lines)


def persona_prompts():
    out = []
    for p in api.personas():
        def render(kit, args, _p=p):
            focus = (args.get("focus") or "").strip()
            return ("You are the %s of the bharat-trader agent team. Follow these instructions; they are "
                    "%s.\n\n%s\n## Current context (from the server, no network)\n\n%s\n\n%sOver MCP, use the tool in "
                    "brackets for each step; the command lines are the same checks run by hand. %s"
                    % (_p["title"].lower(), _p["file"], _p["body"], _persona_context(kit, _p),
                       ("Today's focus: %s.\n\n" % focus) if focus else "", RULES))
        out.append({"name": "persona_" + p["name"].replace("-", "_"), "title": "Persona: " + p["title"],
                    "description": p["description"],
                    "arguments": [{"name": "focus", "description": "a symbol, a strategy or a question for today "
                                                                   "(optional)", "required": False}],
                    "render": render})
    return out


def render_prompt(p, args, kit=None):
    args = dict(args or {})
    for a in p.get("arguments", []):
        if a.get("required") and not str(args.get(a["name"]) or "").strip():
            raise ValueError("prompt %s needs the argument %s" % (p["name"], a["name"]))
    known = {a["name"] for a in p.get("arguments", [])}
    for k, v in args.items():
        if k not in known:
            raise ValueError("prompt %s has no argument %s" % (p["name"], k))
        if not isinstance(v, str) or len(v) > 400:
            raise ValueError("prompt argument %s must be short text" % k)
    if p.get("render"):
        return p["render"](kit, args)
    focus = args.get("focus")
    fill = {
        "focus": ("Focus on: %s. " % focus) if focus else "",
        "symbol": (args.get("symbol") or "").strip().upper(),
        "expiry_text": (" for the %s expiry" % args["expiry"]) if args.get("expiry") else " (nearest expiry)",
        "idea": (args.get("idea") or "").strip(),
        "symbol_text": (" Try it on %s." % args["symbol"].strip().upper()) if args.get("symbol") else "",
        "symbols_arg": (" with symbols %s" % args["symbol"].strip().upper()) if args.get("symbol") else "",
        "universe_text": (" among %s" % args["universe"].strip()) if args.get("universe") else "",
        "window": (args.get("window") or "").strip(),
        "idea_text": (" The idea: %s." % args["idea"].strip()) if args.get("idea") else "",
        "week": (args.get("week") or "this").strip(),
    }
    return p["text"].format(**fill)


def tool_reference_md(tools=None):
    """docs/mcp.md's tool reference, generated from the schemas."""
    tools = tools or build_tools()
    lines = ["| tool | what it does | arguments | hints |", "| --- | --- | --- | --- |"]
    for t in tools:
        props = t["inputSchema"].get("properties", {})
        req = set(t["inputSchema"].get("required", []))
        args = ", ".join("`%s`%s" % (k, "" if k in req else "?") for k in props if k != "lang") or "none"
        ann = t["annotations"]
        hints = []
        if ann.get("readOnlyHint"):
            hints.append("read-only")
        else:
            hints.append("writes trader/ (replaces a file, keeps a .bak)" if ann.get("destructiveHint")
                         else "writes trader/")
        if ann.get("openWorldHint"):
            hints.append("network")
        desc = t["description"].replace(" " + DISCLAIMER, "").replace("|", "/")
        lines.append("| `%s` | **%s.** %s | %s | %s |" % (t["name"], t["title"], desc, args, "; ".join(hints)))
    return "\n".join(lines) + "\n"
