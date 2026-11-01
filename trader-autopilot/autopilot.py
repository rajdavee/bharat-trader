#!/usr/bin/env python3
"""Auto-pilot on PAPER for trader-autopilot: the co-trader loop, run by itself on the paper account. Stdlib only.

Every strategy that trader-lab has validated (PROMOTED, or WATCH: gates passed, forward test running) can build a
real forward record without anyone at the keyboard: the auto-pilot asks the co-trader for candidates, sizes each one
with trader-budget inside a per-strategy capital bucket, runs trader-risk on that exact order, records a machine
"yes" that is labelled autopilot, and lets the PAPER broker fill it on the bars. It never touches a live broker.

    python3 autopilot.py start                  # the loop, driven by the NSE clock (IST, holidays, CAS); Ctrl-C stops
    python3 autopilot.py start --once           # one step now (for cron), then exit
    python3 autopilot.py start --dry-run        # decide, record "would place", place nothing
    python3 autopilot.py stop                   # ask a running loop to finish its step and exit
    python3 autopilot.py status [--json]
    python3 autopilot.py report [--date 2026-09-21]
    python3 autopilot.py replay --date 2026-09-21 [--to 2026-09-23] [--data DIR]   # a past day, simulated clock

One day, in order (each step resumable: state is saved after every action):

  pre-open    from the pre-open (09:00): trader-doctor's data check (live only, if configured), the market regime
              (trader-regime), a trader-archive update (if configured, live only) and the co-trader brief.
  entries     from no_entries_before to the entry cut-off (no_entries_after, pulled in before the safe flat-by):
              the co-trader every `cadence_min` minutes. PROMOTED and WATCH candidates only, never RESEARCH. Each is
              sized by trader-budget with its strategy's capital, checked by trader-risk (every rule, the circuit
              breakers, the kill switch, the order rate), confirmed with the recorded machine yes "yes (autopilot
              machine yes, paper account)" (actor autopilot in trader/state/broker.json) and placed on paper as a
              LIMIT at the plan's entry with its SL-M stop and LIMIT target.
  manage      every completed 5-minute bar: a working LIMIT fills when a bar trades through it (skipped if it opens
              through the stop or the target, like trader-lab); then the stop (a gap fills at the open), the target,
              the trailing stop (if configured), the strategy's own exit rule and maximum holding (at the next bar's
              open), MIS out at the flat-by, and F&O baskets marked at the close with the spec's profit target, stop
              and expiry settlement. MARKET and stop fills pay the spec's slippage (trader-lab's model).
  end of day  after the close: unfilled entries cancelled, the trade journal synced (trader-journal, when it is
              installed), the co-trader's calibration scored (overconfident strategies demoted), each strategy's
              paper trades written into its trader-lab forward record and the lab's forward test re-run (WATCH ->
              PROMOTE, PROMOTE -> DEMOTE), and the day's report: trader/autopilot/days/<date>/report.md and .json.

The kill switch (trader-risk check.py kill) stops new entries and flattens every auto-pilot position on paper. A config
that is not paper ("paper": false, or a live broker) stops the loop before anything else: the auto-pilot refuses to
start on it, and it never calls trader-broker's live place command. Education and tooling, not financial advice.
"""

import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import sys
import time
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_STATE = "bharat-trader/autopilot-state/1"
SCHEMA_REPORT = "bharat-trader/autopilot-report/1"
SCHEMA_EVENT = "bharat-trader/autopilot-event/1"
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TIERS = ("PROMOTED", "WATCH")             # the only tiers the auto-pilot trades; RESEARCH never
YES_TEXT = "yes (autopilot machine yes, paper account)"
START_WORDS = "start paper"               # typed by the user to start the loop from the UI or an assistant
STOP_WORDS = "stop paper"
DISCLAIMER = "Paper trading only. Education and tooling, not financial advice. Nothing here can place a live order."
# the only trader-broker commands this tool may run: paper confirm (with --actor autopilot), paper fills, expiry
# settlement and the F&O close-all PROPOSAL (adjust writes a ticket, places nothing). Never place, cancel or any
# live-account command.
BROKER_COMMANDS = ("confirm", "fill", "settle", "adjust")
DEFAULTS = {
    "strategies": {},          # {lab strategy name: {"capital", "max_positions", "max_daily_loss", ...}}
    "max_positions": 4,        # open + working auto-pilot positions, all strategies
    "max_daily_loss": None,    # rupees, all strategies: realised loss today + the new trade's loss at the stop
    "cadence_min": 15,         # how often the co-trader is asked during the entry window
    "tiers": list(TIERS),      # PROMOTED and/or WATCH
    "doctor": True,            # trader-doctor's data check at the pre-open (live only)
    "regime_symbols": ["NIFTY"],
    "archive_update": False,   # trader-archive update at the pre-open (live only)
    "calibration_demote": True,
    "forward_update": True,    # feed the lab's forward record and re-run its forward test at the end of the day
}
STRATEGY_KEYS = {"capital", "max_positions", "max_daily_loss", "max_loss_per_trade", "product", "trail_after_r",
                 "trail_lock_r", "enabled"}


class AutopilotError(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by the test hook to cut a run mid-step (the next run resumes)."""


def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian digit grouping), -1234 -> '-₹1,234.00'."""
    d = Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = format(abs(d), "f").split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%s₹%s.%s" % (sign, whole, frac)


def rp(x):
    return float(Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- sibling skills, loaded by path

def _sibling(folder, script, required=True):
    name = "bt_autopilot_" + script
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), folder, script + ".py")
    if not os.path.isfile(path):
        if required:
            raise AutopilotError("trader-autopilot needs %s installed next to it (%s is missing)" % (folder, path))
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def broker_api():
    return _sibling("trader-broker", "ticket")


def risk_api():
    return _sibling("trader-risk", "check")


def budget_api():
    return _sibling("trader-budget", "size")


def cotrader_api():
    return _sibling("trader-cotrader", "cotrader")


def lab_api():
    return _sibling("trader-lab", "lab")


def alerts_api():
    return _sibling("trader-alerts", "alerts")


def doctor_api():
    return _sibling("trader-doctor", "doctor", required=False)


def regime_api():
    return _sibling("trader-regime", "regime", required=False)


def archive_api():
    return _sibling("trader-archive", "archive", required=False)


def journal_api():
    """trader-journal (#49) when it is installed next to this folder, else None (the hook is then skipped)."""
    return _sibling("trader-journal", "journal", required=False)


def errors():
    out = [AutopilotError]
    for mod, name in ((broker_api(), "TicketError"), (risk_api(), "RiskError"), (budget_api(), "SizeError"),
                      (cotrader_api(), "CotraderError"), (lab_api(), "LabError"), (alerts_api(), "AlertError")):
        if mod is not None and hasattr(mod, name):
            out.append(getattr(mod, name))
    out.append(broker_api().RulesError)
    return tuple(out)


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


class Paths(object):
    """The files of one workdir. The paper account is trader-broker's own (portfolio.json, state/); the
    auto-pilot's working files live in trader/autopilot/ so your own orders.json and risk.json are never touched."""

    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        t = self.trader = os.path.join(self.workdir, "trader")
        self.config = os.path.join(t, "config.json")
        self.portfolio = os.path.join(t, "portfolio.json")
        self.broker_state = os.path.join(t, "state", "broker.json")
        self.trades = os.path.join(t, "state", "trades.json")
        self.kill = os.path.join(t, "state", "kill.json")
        self.lab = os.path.join(t, "lab")
        a = self.ap = os.path.join(t, "autopilot")
        self.state = os.path.join(a, "state.json")
        self.lock = os.path.join(a, "lock.json")
        self.stop = os.path.join(a, "stop.flag")
        self.work = os.path.join(a, "work")          # config copy, prices, risk verdicts, F&O inputs
        self.orders = os.path.join(a, "orders")      # one orders file per auto-pilot order
        self.records = os.path.join(a, "records")    # each strategy's paper forward record

    def day(self, day, name=None):
        d = os.path.join(self.ap, "days", day)
        return os.path.join(d, name) if name else d

    def w(self, name):
        return os.path.join(self.work, name)


# ---------------------------------------------------------------- safety: paper only, in code

def paper_guard(cfg):
    """Refuse anything but the PAPER broker. Returns the merged config. Checked when the loop starts and again
    before every step, so a config switched to live mid-run stops the loop before it does anything."""
    broker = broker_api()
    try:
        mode, live = broker.mode_of(cfg or {})
    except broker.TicketError as e:
        raise AutopilotError("the auto-pilot runs on the PAPER broker only, and the config is not a clean paper "
                             "config (%s). Nothing was started or sent" % e)
    c = broker.merged_config(cfg or {})
    if mode != "paper" or c.get("paper") is not True or broker.broker_name(c.get("broker")) != "paper":
        raise AutopilotError("the config is live (%s, \"paper\": false): the auto-pilot runs on the PAPER broker "
                             "only and never sends a live order. Live trading is the co-pilot, with your own yes "
                             "for every order. Nothing was started or sent" % (live or c.get("broker")))
    if c.get("shadow"):
        raise AutopilotError("shadow mode is a live-broker feature: the auto-pilot runs on the plain PAPER broker "
                             "only. Nothing was started or sent")
    return c


def settings_of(cfg):
    """The "autopilot" block of trader/config.json with its defaults, validated."""
    raw = (cfg or {}).get("autopilot")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AutopilotError("\"autopilot\" in trader/config.json must be an object")
    s = json.loads(json.dumps(DEFAULTS))
    s.update(raw)
    bad = sorted(set(raw) - set(DEFAULTS))
    if bad:
        raise AutopilotError("unknown \"autopilot\" keys: %s (known: %s)" % (", ".join(bad), ", ".join(sorted(DEFAULTS))))
    tiers = [str(t).upper() for t in (s.get("tiers") or [])]
    if "RESEARCH" in tiers:
        raise AutopilotError("\"tiers\" cannot include RESEARCH: unproven ideas are never traded by the auto-pilot "
                             "(PROMOTED and WATCH only)")
    if not tiers or set(tiers) - set(TIERS):
        raise AutopilotError("\"tiers\" is a list of PROMOTED and/or WATCH")
    s["tiers"] = tiers
    cad = s.get("cadence_min")
    if not isinstance(cad, int) or isinstance(cad, bool) or not 5 <= cad <= 120 or cad % 5:
        raise AutopilotError("\"cadence_min\" is a whole number of minutes, a multiple of 5 from 5 to 120")
    mp = s.get("max_positions")
    if not isinstance(mp, int) or isinstance(mp, bool) or not 1 <= mp <= 50:
        raise AutopilotError("\"max_positions\" is a whole number from 1 to 50")
    if s.get("max_daily_loss") is not None and not _pos_num(s["max_daily_loss"]):
        raise AutopilotError("\"max_daily_loss\" is rupees above 0 (or null for none beyond your config's)")
    strat = s.get("strategies") or {}
    if not isinstance(strat, dict):
        raise AutopilotError("\"strategies\" is an object: {\"lab-strategy-name\": {\"capital\": 50000, ...}}")
    out = {}
    for name, b in strat.items():
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,47}$", str(name)):
            raise AutopilotError("strategy %r is not a trader-lab strategy name" % name)
        b = dict(b or {})
        extra = sorted(set(b) - STRATEGY_KEYS)
        if extra:
            raise AutopilotError("strategy %s: unknown keys %s (known: %s)" % (name, ", ".join(extra),
                                                                                ", ".join(sorted(STRATEGY_KEYS))))
        if not _pos_num(b.get("capital")):
            raise AutopilotError("strategy %s needs \"capital\": the rupees it may use (its bucket)" % name)
        b.setdefault("max_positions", 2)
        if not isinstance(b["max_positions"], int) or isinstance(b["max_positions"], bool) or b["max_positions"] < 1:
            raise AutopilotError("strategy %s: \"max_positions\" is a whole number from 1" % name)
        for k in ("max_daily_loss", "max_loss_per_trade", "trail_after_r", "trail_lock_r"):
            if b.get(k) is not None and not _pos_num(b[k]) and not (k == "trail_lock_r" and b[k] == 0):
                raise AutopilotError("strategy %s: \"%s\" is a number above 0" % (name, k))
        if b.get("product") is not None and str(b["product"]).upper() not in ("MIS", "CNC"):
            raise AutopilotError("strategy %s: \"product\" is MIS or CNC (default: the strategy's own)" % name)
        if b.get("trail_lock_r") is not None and b.get("trail_after_r") is None:
            raise AutopilotError("strategy %s: \"trail_lock_r\" needs \"trail_after_r\"" % name)
        out[name] = b
    s["strategies"] = out
    return s


def _pos_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


# ---------------------------------------------------------------- the NSE clock

def naive(dt):
    return dt.astimezone(IST).replace(tzinfo=None) if dt.tzinfo else dt


def aware(dt):
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def hhmm(t):
    return t.strftime("%H:%M")


def minutes(t):
    return t.hour * 60 + t.minute


def parse_now(text):
    if not text:
        return datetime.datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.datetime.strptime(text.strip()[:19], fmt)
        except ValueError:
            continue
    raise AutopilotError("--now is \"YYYY-MM-DD HH:MM\" (IST), not %r" % text)


class Clock(object):
    """NSE sessions from trader-alerts' calendar (holidays, your extra_holidays and special_sessions) and the
    dated market rules (closing auction, broker square-off) shared by every order-path skill."""

    def __init__(self, cfg, fno):
        self.cfg = cfg
        self.fno = fno
        self.cal = alerts_api().Calendar(cfg)

    def session(self, day):
        """(pre_open, open, close) as times, or None on a closed day."""
        d = datetime.date.fromisoformat(day)
        s = self.cal.session(d)
        if not s:
            return None
        return s[0], s[1], s[2]

    def closed_reason(self, day):
        return self.cal.trading_day(datetime.date.fromisoformat(day))[1]

    def segment(self, sym):
        return broker_api().segment_of(sym, self.fno)

    def flat_by(self, sym, day):
        b = broker_api()
        return b.safe_flat_by(day, self.segment(sym), b.merged_config(self.cfg))[0]

    def entry_cutoff(self, day):
        """The last entry time for the most restricted stock (F&O stocks under the closing auction)."""
        b = broker_api()
        return b.entry_cutoff(day, "EQ_FNO", b.merged_config(self.cfg))

    def entries_from(self):
        return datetime.datetime.strptime(str(broker_api().merged_config(self.cfg).get("no_entries_before")
                                              or "09:30"), "%H:%M").time()

    def next_session(self, day):
        d = datetime.date.fromisoformat(day)
        for _ in range(15):
            d += datetime.timedelta(days=1)
            if self.cal.session(d):
                return d.isoformat()
        return None

    def prev_session(self, day):
        d = datetime.date.fromisoformat(day)
        for _ in range(15):
            d -= datetime.timedelta(days=1)
            if self.cal.session(d):
                return d.isoformat()
        return None


# ---------------------------------------------------------------- bars

class Market(object):
    """5-minute and daily bars as seen at `now`: --data DIR (offline files), else the local trader-archive, else
    trader-lab's Yahoo cache (fetched only when allowed). A replay sees no bar that had not closed by its clock."""

    def __init__(self, workdir, data=None, archive=None, fetch=True, live=False):
        self.data = data
        if archive is None:
            archive = os.environ.get("BT_ARCHIVE") or os.path.join(os.path.abspath(workdir), "trader", "archive")
        self.archive = archive if archive and os.path.isdir(archive) else None
        self.fetch = bool(fetch) and not data
        self.live = live
        self.cache = os.path.join(os.path.abspath(workdir), "trader", "lab", "data")
        self.raw = {}
        self.notes = []

    def new_tick(self):
        if self.live:
            self.raw = {k: v for k, v in self.raw.items() if k[1] != "5m"}

    def bars(self, sym, tf, now):
        lab = lab_api()
        key = (sym, tf)
        if key not in self.raw:
            try:
                got, _ = lab.load_bars(sym, tf, self.data, self.cache, self.fetch, now, refresh=self.live,
                                       archive=self.archive)
            except lab.LabError as e:
                got = []
                self.notes.append("%s %s bars: %s" % (sym, tf, e))
            self.raw[key] = got or []
        return lab.seen_by(self.raw[key], tf, now)

    def day_bars(self, sym, day, now):
        """Today's completed 5-minute bars: [{"date", "time", "o", "h", "l", "c", "v", "minute"}]."""
        out = []
        for b in self.bars(sym, "5m", now):
            if b["date"] == day:
                b = dict(b)
                h, m = [int(x) for x in b["time"].split(":")]
                b["minute"] = h * 60 + m
                out.append(b)
        return out

    def series(self, sym, tf, now):
        bars = self.bars(sym, tf, now)
        return lab_api().Series(sym, tf, bars) if bars else None

    def last_price(self, sym, day, now):
        """(price, time) of the latest completed 5-minute bar today, else the last daily close."""
        bars = self.day_bars(sym, day, now)
        if bars:
            b = bars[-1]
            m = b["minute"] + 5
            return float(b["c"]), datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=m // 60, minute=m % 60)
        d = self.bars(sym, "1d", now)
        if d:
            return float(d[-1]["c"]), None
        return None, None


# ---------------------------------------------------------------- the auto-pilot

def risk_file(orders_path):
    return orders_path[:-5] + "-risk.json"


def tick_price(x, how):
    b = budget_api()
    return b.tick_round(x, b.tick_for(x, "auto"), how)


class Pilot(object):
    """One workdir's auto-pilot. step(now) does everything due at `now` and saves its state after every action,
    so a crash or a restart resumes where it stopped."""

    def __init__(self, workdir, market, dry_run=False, replay=False, log=None, crash=None, fetch=True):
        self.paths = Paths(workdir)
        self.market = market
        self.dry_run = dry_run
        self.replay = replay
        self.fetch = fetch and not replay
        self.log = log or (lambda msg: None)
        self.crash = crash               # test hook: {"point": "after-fill", "n": 1}
        self.crash_seen = {}
        self.state = read_json(self.paths.state) or {}
        self.cfg = None
        self.ap = None
        self.clock = None
        self.seq = 0

    # ------------------------------------------------------------ plumbing

    def hook(self, point):
        if not self.crash or self.crash.get("point") != point:
            return
        self.crash_seen[point] = self.crash_seen.get(point, 0) + 1
        if self.crash_seen[point] == int(self.crash.get("n", 1)):
            raise SimulatedCrash("simulated crash at %s" % point)

    def save(self):
        write_json(self.paths.state, self.state)

    def event(self, now, kind, words, **data):
        rec = {"schema": SCHEMA_EVENT, "at": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "words": words}
        rec.update(data)
        path = self.paths.day(now.date().isoformat(), "timeline.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self.state["events"] = self.state.get("events", 0) + 1
        self.log("%s %s" % (now.strftime("%H:%M"), words))
        return rec

    def load_config(self):
        cfg = read_json(self.paths.config)
        if not isinstance(cfg, dict):
            raise AutopilotError("no trader/config.json: run trader-budget's size.py init first (your capital and "
                                 "limits), then add an \"autopilot\" block (docs/autopilot.md)")
        merged = paper_guard(cfg)
        self.ap = settings_of(cfg)
        self.cfg = cfg
        self.merged = merged
        os.makedirs(self.paths.work, exist_ok=True)
        write_json(self.paths.w("config.json"), cfg)
        for name in ("fno_stocks.json", "fo_mktlots.csv", "fo_secban.json"):
            src = os.path.join(self.paths.trader, name)
            if os.path.exists(src):
                shutil.copyfile(src, self.paths.w(name))
        fno = broker_api().load_fno_stocks(cfg, self.paths.w("fno_stocks.json"), fetch=False)[0]
        self.clock = Clock(cfg, fno)
        return cfg

    def run_broker(self, argv):
        if not argv or argv[0] not in BROKER_COMMANDS:
            raise AutopilotError("the auto-pilot runs only these trader-broker commands: %s" % ", ".join(BROKER_COMMANDS))
        broker = broker_api()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = broker.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 2
        return rc, out.getvalue(), err.getvalue()

    def run_risk(self, orders_path, now):
        """trader-risk's own command line on one auto-pilot orders file; its verdicts are recorded next to it
        (<orders file>-risk.json), which is what the paper broker's confirm reads."""
        risk = risk_api()
        argv = ["--orders", orders_path, "--config", self.paths.w("config.json"), "--portfolio", self.paths.portfolio,
                "--prices", self.paths.w("prices.json"), "--state", self.paths.broker_state, "--trades",
                self.paths.trades, "--kill", self.paths.kill, "--now", now.strftime("%Y-%m-%d %H:%M:%S"),
                "--offline", "--out", risk_file(orders_path), "--json"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                risk.main(argv)
            except SystemExit:
                pass
        try:
            return json.loads(out.getvalue())
        except ValueError:
            lines = (err.getvalue() or out.getvalue()).strip().splitlines()
            return {"results": [], "error": lines[-1] if lines else "trader-risk did not answer"}

    def write_prices(self, day, now, syms):
        doc = {"as_of": aware(now).isoformat(), "prices": {}, "detail": {}, "source": "trader-autopilot bars"}
        for s in sorted(set(syms)):
            p, t = self.market.last_price(s, day, now)
            if p:
                doc["prices"][s] = p
                doc["detail"][s] = {"time": aware(t or now).isoformat()}
        write_json(self.paths.w("prices.json"), doc)

    def positions(self, *status):
        return [p for p in self.state.get("positions", []) if not status or p["status"] in status]

    def strategy_positions(self, name, *status):
        return [p for p in self.positions(*status) if p["strategy"] == name]

    def enabled(self):
        return [n for n, b in self.ap["strategies"].items() if b.get("enabled") is not False]

    def bucket(self, name):
        return self.ap["strategies"].get(name)

    def new_id(self, day):
        n = self.state.get("seq", 0) + 1
        self.state["seq"] = n
        return "AP-%s-%d" % (day.replace("-", ""), n)

    # ------------------------------------------------------------ the day

    def roll_day(self, day, now):
        if self.state.get("day") == day:
            return
        carried = []
        for p in self.positions("working", "placing"):
            p["status"] = "cancelled"
            p["closed_why"] = "a DAY order: not filled by the end of %s" % p.get("plan_date")
        for p in self.positions("open"):
            carried.append(p["id"])
        keep = [p for p in self.state.get("positions", []) if p["status"] in ("open",)]
        self.state.update({"schema": SCHEMA_STATE, "day": day, "phases": {}, "last_poll": None, "acted": {},
                           "seen": {}, "positions": keep, "kill_handled": False, "entries_stopped": None,
                           "entries_closed": False, "closed_today": [], "pre_open": None, "forward": None,
                           "mode": "replay" if self.replay else ("dry-run" if self.dry_run else "paper"),
                           "events": 0})
        self.save()
        if carried:
            self.event(now, "carry", "carried over from the last session: %s" % ", ".join(
                "%s %s %d" % (p["id"], p["symbol"], p["qty"]) for p in self.positions("open")), ids=carried)

    def step(self, now):
        """Everything due at `now` (naive IST). Returns the phase it is in."""
        self.load_config()                                  # paper_guard every step
        self.market.new_tick()
        day = now.date().isoformat()
        sess = self.clock.session(day)
        if not sess:
            if self.state.get("closed_day") != day:
                self.state["closed_day"] = day
                self.save()
            return "closed: %s" % (self.clock.closed_reason(day) or "no session")
        self.roll_day(day, now)
        pre, opens, close = sess
        t = now.time()
        ph = self.state["phases"]
        if t < pre:
            return "before the pre-open"
        if "pre_open" not in ph:
            self.pre_open(day, now)
        if t < opens:
            return "pre-open"
        if t <= close or "eod" not in ph:
            self.manage(day, now)
            self.kill_check(day, now)
            self.flat_by(day, now)
            if t < close:
                self.entries(day, now)
        if t >= close and "eod" not in ph:
            self.end_of_day(day, now)
            return "end of day done"
        return "closed for the day" if t >= close else "open"

    # ------------------------------------------------------------ pre-open

    def pre_open(self, day, now):
        notes = {}
        doc = doctor_api()
        if self.replay:
            notes["doctor"] = "replay: the archive is the data (no live check)"
        elif not self.ap.get("doctor"):
            notes["doctor"] = "off in the config"
        elif doc is None:
            notes["doctor"] = "trader-doctor is not installed"
        elif not self.fetch:
            notes["doctor"] = "offline: skipped"
        else:
            try:
                rep = doc.run(trader_dir=self.paths.trader)
                notes["doctor"] = "%d sources checked, %d failing" % (len(rep["sources"]), rep["summary"].get("failed", 0))
            except Exception as e:  # a data check must never stop the day
                notes["doctor"] = "could not run: %s" % e
        notes["regime"] = self.regime_words(now)
        if self.ap.get("archive_update") and not self.replay and self.fetch:
            arc = archive_api()
            if arc is None:
                notes["archive"] = "trader-archive is not installed"
            else:
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    try:
                        rc = arc.main(["update"] + (["--root", self.market.archive] if self.market.archive else []))
                    except SystemExit as e:
                        rc = e.code
                notes["archive"] = "updated" if not rc else "update failed: %s" % out.getvalue().strip()[-200:]
        else:
            notes["archive"] = "not configured" if not self.ap.get("archive_update") else "replay/offline: skipped"
        if self.enabled():
            brief = self.ask_cotrader(day, now, brief=True)
        else:
            brief = "no strategy enabled in the autopilot config: nothing to trade"
        notes["cotrader"] = brief
        self.state["phases"]["pre_open"] = now.strftime("%H:%M")
        self.state["pre_open"] = notes
        self.save()
        self.event(now, "pre-open", "pre-open: regime %s; co-trader: %s; data check: %s" % (
            notes["regime"], brief, notes["doctor"]), notes=notes)

    def regime_words(self, now):
        reg = regime_api()
        if reg is None:
            return "trader-regime is not installed"
        out = []
        for sym in self.ap.get("regime_symbols") or ["NIFTY"]:
            try:
                src = reg.Sources(self.market.archive, self.market.data, self.fetch, self.paths.trader)
                r = reg.read_now(sym, src, now, reg.load_config(None))
                out.append(r["daily"]["words"])
            except Exception as e:  # no bars is a note, not a stop
                out.append("%s: not available (%s)" % (sym, str(e)[:120]))
        return "; ".join(out)

    # ------------------------------------------------------------ entries

    def ask_cotrader(self, day, now, brief=False):
        ct = cotrader_api()
        try:
            doc = ct.run_today(self.paths.workdir, "validated", None, now, self.market.data, self.market.archive,
                               self.fetch, True)
        except errors() as e:
            if brief:
                return "could not run (%s)" % e
            raise
        if brief:
            enabled = self.ap["strategies"]
            mine = [c for c in doc["candidates"] if c["strategy"] in enabled]
            if doc.get("no_edge") and not mine:
                return "no candidate today (%s)" % "; ".join((doc["no_edge"].get("reasons") or [])[:2]) \
                    if isinstance(doc["no_edge"], dict) else "no candidate today"
            return "%d candidate(s) for enabled strategies: %s" % (len(mine), ", ".join(c["id"] for c in mine) or "none")
        return doc

    def entries(self, day, now):
        st = self.state
        if self.kill_on(day):
            return
        start, cut = self.clock.entries_from(), self.clock.entry_cutoff(day)
        t = now.time()
        if t < start or t > cut:
            if t > cut and not st.get("entries_closed"):
                st["entries_closed"] = True
                self.cancel_working(day, now, "the entry window closed at %s IST" % hhmm(cut))
                self.save()
            return
        last = st.get("last_poll")
        if last and (now - datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() < \
                self.ap["cadence_min"] * 60 - 1:
            return
        if not self.enabled():
            st["last_poll"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save()
            return
        doc = self.ask_cotrader(day, now)
        for i, c in enumerate(doc["candidates"]):
            self.seq = i + 1                            # orders a second apart (the order-rate rule), same on resume
            self.consider(c, day, now)
        st["last_poll"] = now.strftime("%Y-%m-%d %H:%M:%S")
        self.save()

    def note_once(self, cid, why, now, kind="skip", **data):
        seen = self.state.setdefault("seen", {})
        if seen.get(cid) == why:
            return
        seen[cid] = why
        self.save()
        self.event(now, kind, "%s: %s" % (cid, why), candidate=cid, **data)

    def consider(self, c, day, now):
        cid, name, tier = c["id"], c["strategy"], c["tier"]
        if cid in self.state["acted"]:
            return
        if tier not in TIERS:                           # hard rule: RESEARCH is never traded
            return self.note_once(cid, "RESEARCH (unproven): never traded by the auto-pilot", now)
        if tier not in self.ap["tiers"]:
            return self.note_once(cid, "tier %s is not in your autopilot tiers" % tier, now)
        b = self.bucket(name)
        if not b or b.get("enabled") is False:
            return self.note_once(cid, "strategy %s is not enabled in the autopilot config" % name, now)
        if c.get("date") != day:
            return self.note_once(cid, "the plan is for %s, not today" % c.get("date"), now)
        if c.get("kind") == "options":
            return self.consider_basket(c, b, day, now)
        live = self.positions("open", "working", "placing")
        if any(p["symbol"] == c["symbol"] for p in live):
            return self.note_once(cid, "an auto-pilot position in %s is already open or working" % c["symbol"], now)
        if len(self.strategy_positions(name, "open", "working", "placing")) >= int(b["max_positions"]):
            return self.note_once(cid, "%s has its %d position(s) open: its max_positions" % (name, b["max_positions"]), now)
        if len(live) >= int(self.ap["max_positions"]):
            return self.note_once(cid, "the auto-pilot has %d position(s) open: its max_positions" % len(live), now)
        product = str(b.get("product") or c.get("product") or "MIS").upper()
        used = sum(p["entry_limit"] * p["qty"] for p in self.strategy_positions(name, "open", "working", "placing"))
        free = rp(float(b["capital"]) - used)
        if free <= 0:
            return self.note_once(cid, "%s's capital %s is in use" % (name, inr(b["capital"])), now)
        bcfg = dict(self.cfg, capital=float(b["capital"]), product=product)
        if b.get("max_loss_per_trade"):
            bcfg["max_loss_per_trade"] = float(b["max_loss_per_trade"])
        elif "max_loss_per_trade" not in self.cfg:
            bcfg["max_loss_per_trade"] = rp(float(b["capital"]) * float(self.merged.get("risk_per_trade_pct") or 1) / 100)
        pf = read_json(self.paths.portfolio) or {}
        cash = pf.get("cash") if isinstance(pf.get("cash"), (int, float)) else float(self.merged.get("capital") or 0)
        budget = budget_api()
        plan = dict(c["plan"])
        try:
            s = budget.size_trade(plan, bcfg, cash=min(float(cash), free), today=day)
        except budget.SizeError as e:
            return self.note_once(cid, "trader-budget: %s" % e, now)
        if s["refusal"]:
            return self.note_once(cid, "trader-budget: %s" % s["refusal"], now)
        loss = float(s["max_loss_with_charges"])
        sd, td = self.realised_today(name, day), self.realised_today(None, day)
        if b.get("max_daily_loss") and -min(sd, 0) + loss > float(b["max_daily_loss"]) + 0.005:
            return self.note_once(cid, "%s's daily loss budget %s: realised %s today, this one risks %s" % (
                name, inr(b["max_daily_loss"]), inr(sd), inr(loss)), now)
        if self.ap.get("max_daily_loss") and -min(td, 0) + loss > float(self.ap["max_daily_loss"]) + 0.005:
            return self.note_once(cid, "the auto-pilot's daily loss budget %s: realised %s today, this one risks %s"
                                  % (inr(self.ap["max_daily_loss"]), inr(td), inr(loss)), now)
        order = budget.leg_order(s)
        order["why"] = "AUTO-PILOT (paper) %s, %s tier: %s" % (name, tier, order["why"])
        order["plan"]["autopilot"] = {"strategy": name, "candidate": cid, "tier": tier}
        when = now + datetime.timedelta(seconds=self.seq)
        self.place(c, s, order, product, day, when)

    def realised_today(self, name, day):
        tot = 0.0
        for p in self.state.get("positions", []):
            if name and p["strategy"] != name:
                continue
            for x in p.get("exits") or []:
                if x["at"][:10] == day and x.get("realised") is not None:
                    tot += float(x["realised"])
        return rp(tot)

    def place(self, c, s, order, product, day, now):
        cid = c["id"]
        pid = self.new_id(day)
        path = os.path.join(self.paths.orders, "%s-entry.json" % pid)
        write_json(path, {"created_by": "trader-autopilot", "status": "proposed", "paper": True,
                          "source": "trader-autopilot %s" % cid, "orders": [order]})
        self.write_prices(day, now, [c["symbol"]])
        res = self.run_risk(path, now)
        r0 = (res.get("results") or [{}])[0]
        if r0.get("verdict") != "PASS":
            reasons = r0.get("reasons") or [res.get("error") or "no verdict"]
            self.state["seq"] -= 1
            self.save()
            return self.note_once(cid, "trader-risk BLOCK: %s" % "; ".join(reasons), now, kind="blocked",
                                  reasons=reasons)
        fp = r0["fingerprint"]
        lab = c.get("plan", {}).get("lab") or {}
        pos = {"id": pid, "strategy": c["strategy"], "candidate": cid, "tier": c["tier"], "symbol": c["symbol"],
               "product": product, "qty": int(s["qty"]), "entry_limit": float(s["entry"]), "stop": float(s["stop"]),
               "stop0": float(s["stop"]), "target": float(s["target"]), "risk_ps": float(s["risk_per_share"]),
               "max_loss": float(s["max_loss_with_charges"]), "timeframe": c.get("timeframe") or "1d",
               "signal_date": c.get("signal_date"), "plan_date": day, "spec_hash": c.get("spec_hash") or
               lab.get("spec_hash"), "max_bars": c.get("max_bars"), "order_file": path, "fingerprint": fp,
               "status": "placing", "placed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "exits": [], "bars": 0,
               "last_bar": None, "entry_time": (c.get("plan") or {}).get("entry_time"),
               "p": (c.get("evidence") or {}).get("p")}
        if self.dry_run:
            self.state["acted"][cid] = "dry-run"
            self.save()
            return self.event(now, "dry-run", "would place %s: BUY %d %s %s LIMIT %s, stop %s, target %s (trader-risk "
                                              "PASS); dry run, nothing placed" % (
                                                  cid, pos["qty"], pos["symbol"], product, pos["entry_limit"],
                                                  pos["stop"], pos["target"]), candidate=cid)
        self.state.setdefault("positions", []).append(pos)
        self.state["acted"][cid] = pid
        self.save()
        self.confirm(pos, now)

    def confirm(self, pos, now):
        """The machine yes for this one order, recorded with actor autopilot. Resumable: a yes already in the
        broker's state (a crash after confirm) is found, not repeated."""
        broker = broker_api()
        day = now.date().isoformat()
        got = broker.records(broker.load_state(self.paths.broker_state), pos["fingerprint"], day, "placed")
        if not got:
            rc, out, err = self.run_broker(["confirm", "--orders", pos["order_file"], "--config",
                                            self.paths.w("config.json"), "--risk", risk_file(pos["order_file"]),
                                            "--state", self.paths.broker_state, "--index", "1", "--yes-text",
                                            YES_TEXT, "--actor", "autopilot", "--now",
                                            now.strftime("%Y-%m-%d %H:%M:%S")])
            if rc != 0:
                pos["status"] = "refused"
                pos["closed_why"] = (err or out).strip()[-300:]
                self.save()
                return self.event(now, "refused", "%s: the paper broker refused: %s" % (pos["candidate"],
                                                                                         pos["closed_why"]))
            got = broker.records(broker.load_state(self.paths.broker_state), pos["fingerprint"], day, "placed")
        self.hook("after-confirm")
        pos["status"] = "working"
        pos["paper_order_id"] = got[-1].get("paper_order_id")
        self.save()
        self.event(now, "placed", "placed %s on paper (%s): BUY %d %s %s LIMIT %s, SL-M %s, target %s, loss at the "
                                  "stop %s with charges; trader-risk PASS, machine yes recorded (actor autopilot)"
                   % (pos["id"], pos["paper_order_id"], pos["qty"], pos["symbol"], pos["product"],
                      "%.2f" % pos["entry_limit"], "%.2f" % pos["stop"], "%.2f" % pos["target"], inr(pos["max_loss"])),
                   position=pos["id"], candidate=pos["candidate"], fingerprint=pos["fingerprint"])

    def cancel_working(self, day, now, why, baskets=False):
        """Cancel unfilled entries. An F&O basket waits for the close (it fills on the settle prices) unless
        baskets is True (the kill switch)."""
        for p in self.positions("working"):
            if p.get("kind") == "basket" and not baskets:
                continue
            p["status"] = "cancelled"
            p["closed_why"] = why
            self.event(now, "cancelled", "%s %s: entry not filled, cancelled (%s)" % (p["id"], p["symbol"], why),
                       position=p["id"])
        self.save()

    # ------------------------------------------------------------ fills (the paper broker's own fill command)

    def costs(self, pos):
        doc = read_json(os.path.join(self.paths.lab, pos["strategy"], "spec.json")) or {}
        c = dict(lab_api().COST_DEFAULTS)
        c.update(doc.get("costs") or {})
        return c

    def fill(self, pos, leg, price, at, why, order_file=None, fp=None, side="SELL"):
        """One paper fill through ticket.py fill. Resumable: the intent is saved first, and a fill already in
        trades.json (a crash after the fill) is recognised, not repeated."""
        price = rp(price)
        fp = fp or pos["fingerprint"]
        stamp = at.strftime("%Y-%m-%d %H:%M:%S")
        pos["filling"] = {"leg": leg, "price": price, "at": stamp, "why": why, "fp": fp, "side": side,
                          "order_file": order_file or pos["order_file"]}
        self.save()
        trade = self.find_trade(fp, leg, stamp)
        if trade is None:
            argv = ["fill", "--orders", order_file or pos["order_file"], "--index", "1", "--price", "%.2f" % price,
                    "--portfolio", self.paths.portfolio, "--trades", self.paths.trades, "--state",
                    self.paths.broker_state, "--config", self.paths.w("config.json"), "--prices",
                    self.paths.w("prices.json"), "--now", stamp]
            if leg in ("stop", "target"):
                argv += ["--leg", leg]
            rc, out, err = self.run_broker(argv)
            if rc != 0:
                pos.pop("filling", None)
                self.save()
                raise AutopilotError("the paper broker refused the %s fill of %s: %s" % (leg, pos["id"],
                                                                                        (err or out).strip()))
            trade = self.find_trade(fp, leg, stamp)
        self.hook("after-fill")
        return self.book(pos, trade, why)

    def find_trade(self, fp, leg, stamp):
        doc = read_json(self.paths.trades) or {}
        want = aware(datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).isoformat()
        for t in reversed(doc.get("trades") or []):
            if t.get("fingerprint") == fp and t.get("leg") == ("order" if leg in ("entry", "exit") else leg) \
                    and t.get("at") == want:
                return t
        return None

    def book(self, pos, trade, why):
        f = pos.pop("filling")
        at = f["at"]
        if f["leg"] == "entry":
            pos.update({"status": "open", "entry_price": trade["price"], "entry_charges": trade["charges"],
                        "filled_at": at, "entry_day": at[:10]})
            words = "%s filled on paper: BUY %d %s at %.2f (%s); charges %s" % (
                pos["id"], pos["qty"], pos["symbol"], trade["price"], why, inr(trade["charges"]))
            kind = "fill"
        else:
            x = {"at": at, "leg": f["leg"], "price": trade["price"], "qty": trade["qty"], "charges": trade["charges"],
                 "realised": trade.get("realised_pnl"), "why": why}
            pos.setdefault("exits", []).append(x)
            pos.update({"status": "closed", "closed_at": at, "closed_why": why})
            net = trade.get("realised_pnl")
            r = net / (pos["qty"] * (pos["entry_price"] - pos["stop0"])) if net is not None and \
                pos["entry_price"] > pos["stop0"] else None
            pos["result"] = {"net": net, "r": round(r, 4) if r is not None else None,
                             "charges": rp((pos.get("entry_charges") or 0) + trade["charges"])}
            words = "%s closed on paper: SELL %d %s at %.2f (%s); charges %s; realised %s after charges" % (
                pos["id"], trade["qty"], pos["symbol"], trade["price"], why, inr(trade["charges"]),
                inr(net or 0))
            kind = "exit"
            self.state.setdefault("closed_today", []).append(pos["id"])
        self.save()
        self.event(datetime.datetime.strptime(at, "%Y-%m-%d %H:%M:%S"), kind, words, position=pos["id"],
                   strategy=pos["strategy"], leg=f["leg"], price=trade["price"], charges=trade["charges"], realised=trade.get("realised_pnl"))
        return trade

    def exit_order(self, pos, at, price, why):
        """A SELL MARKET order for a time exit, the flat-by, the kill switch or a carried position: trader-risk
        (an exit always passes), the machine yes, then the paper fill."""
        if pos.get("filling"):
            f = pos["filling"]
            return self.fill(pos, f["leg"], f["price"], datetime.datetime.strptime(f["at"], "%Y-%m-%d %H:%M:%S"),
                             f["why"], f["order_file"], f["fp"])
        n = len(pos.get("exit_orders") or []) + 1
        path = os.path.join(self.paths.orders, "%s-exit-%d.json" % (pos["id"], n))
        order = {"symbol": pos["symbol"], "exchange": "NSE", "product": pos["product"], "side": "SELL",
                 "qty": pos["qty"], "order_type": "MARKET", "price": None, "validity": "DAY",
                 "why": "AUTO-PILOT (paper) exit of %s: %s" % (pos["id"], why),
                 "plan": {"setup": "autopilot-exit", "autopilot": {"position": pos["id"]}}}
        write_json(path, {"created_by": "trader-autopilot", "status": "proposed", "paper": True,
                          "source": "trader-autopilot exit %s" % pos["id"], "orders": [order]})
        self.write_prices(at.date().isoformat(), at, [pos["symbol"]])
        res = self.run_risk(path, at)
        r0 = (res.get("results") or [{}])[0]
        if r0.get("verdict") != "PASS":
            raise AutopilotError("trader-risk blocked the exit of %s: %s" % (pos["id"], "; ".join(
                r0.get("reasons") or [res.get("error") or "no verdict"])))
        fp = r0["fingerprint"]
        pos.setdefault("exit_orders", []).append({"file": path, "fingerprint": fp})
        self.save()
        broker = broker_api()
        day = at.date().isoformat()
        if not broker.records(broker.load_state(self.paths.broker_state), fp, day, "placed"):
            rc, out, err = self.run_broker(["confirm", "--orders", path, "--config", self.paths.w("config.json"),
                                            "--risk", risk_file(path), "--state", self.paths.broker_state,
                                            "--index", "1", "--yes-text", YES_TEXT, "--actor", "autopilot",
                                            "--now", at.strftime("%Y-%m-%d %H:%M:%S")])
            if rc != 0:
                raise AutopilotError("the paper broker refused the exit of %s: %s" % (pos["id"], (err or out).strip()))
        return self.fill(pos, "exit", price, at, why, path, fp)

    # ------------------------------------------------------------ manage

    def compiled(self, name):
        """The strategy's compiled spec with the lab's latest parameters (for its own exit rule)."""
        cache = self.__dict__.setdefault("_specs", {})
        if name not in cache:
            lab = lab_api()
            d = os.path.join(self.paths.lab, name)
            doc, res = read_json(os.path.join(d, "spec.json")), read_json(os.path.join(d, "results.json")) or {}
            spec = None
            if doc:
                try:
                    params = dict(lab.validate_spec(doc)["base_params"])
                    params.update((res.get("walk_forward") or {}).get("latest_params") or {})
                    spec = lab.validate_spec(doc, params=params)
                except lab.LabError:
                    spec = None
            cache[name] = spec
        return cache[name]

    def manage(self, day, now):
        for pos in self.positions("open", "working", "placing"):
            if pos.get("kind") == "basket":
                continue
            if pos["status"] == "placing":
                self.confirm(pos, datetime.datetime.strptime(pos["placed_at"], "%Y-%m-%d %H:%M:%S"))
                if pos["status"] != "working":
                    continue
            if pos.get("filling"):
                f = pos["filling"]
                at = datetime.datetime.strptime(f["at"], "%Y-%m-%d %H:%M:%S")
                if f["leg"] == "exit":
                    self.exit_order(pos, at, f["price"], f["why"])
                else:
                    self.fill(pos, f["leg"], f["price"], at, f["why"], f["order_file"], f["fp"])
                if pos["status"] == "closed":
                    continue
            self.walk_bars(pos, day, now)

    def walk_bars(self, pos, day, now):
        bars = self.market.day_bars(pos["symbol"], day, now)
        costs = self.costs(pos)
        lab = lab_api()
        flat = minutes(self.clock.flat_by(pos["symbol"], day))
        intraday = pos["product"] == "MIS"
        spec = self.compiled(pos["strategy"]) if pos["timeframe"] == "5m" else None
        sig = None
        for i, b in enumerate(bars):
            stamp = "%s %s" % (day, b["time"])
            if pos["last_bar"] and stamp <= pos["last_bar"]:
                continue
            if pos["status"] == "working" and stamp < pos["placed_at"][:16]:
                continue
            bar_at = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            inside = bar_at + datetime.timedelta(minutes=4)
            if intraday and b["minute"] >= flat:
                break                                       # the flat-by step takes it from here
            o, h, l = float(b["o"]), float(b["h"]), float(b["l"])
            fresh = False
            if pos["status"] == "working":
                lim = pos["entry_limit"]
                if l > lim + 1e-9:
                    pos["last_bar"] = stamp
                    continue
                price = min(o, lim)
                if price <= pos["stop"] or price >= pos["target"]:
                    pos.update({"status": "cancelled", "closed_why": "opened through the stop or the target at %.2f "
                                "(trader-lab skips these too)" % price, "last_bar": stamp})
                    self.save()
                    self.event(bar_at, "cancelled", "%s %s: %s" % (pos["id"], pos["symbol"], pos["closed_why"]),
                               position=pos["id"])
                    return
                placed = datetime.datetime.strptime(pos["placed_at"], "%Y-%m-%d %H:%M:%S")
                self.fill(pos, "entry", price, max(bar_at if price == o else inside, placed),
                          "the LIMIT %.2f, bar of %s" % (lim, b["time"]), side="BUY")
                fresh = True
            elif pos.get("exit_pending") and stamp > pos.get("exit_pending_bar", ""):
                why = pos.pop("exit_pending")
                pos.pop("exit_pending_bar", None)
                px = tick_price(o - lab.slippage(o, costs), "down")
                pos["last_bar"] = stamp
                self.exit_order(pos, bar_at, px, "%s, at the %s open" % (why, b["time"]))
                return
            stop, target = pos["stop"], pos["target"]
            same_day = pos.get("entry_day") == day
            if not fresh and o <= stop:
                px = tick_price(o - lab.slippage(o, costs), "down")
                pos["last_bar"] = stamp
                return self.exit_stop(pos, same_day, px, bar_at, "stop %.2f (gapped: the %s open)" % (stop, b["time"]))
            if l <= stop:
                px = tick_price(stop - lab.slippage(stop, costs), "down")
                pos["last_bar"] = stamp
                return self.exit_stop(pos, same_day, px, inside, "stop %.2f hit in the %s bar" % (stop, b["time"]))
            if not fresh and o >= target:
                pos["last_bar"] = stamp
                return self.exit_target(pos, same_day, o, bar_at, "target %.2f (gapped: the %s open)" % (target, b["time"]))
            if h >= target:
                pos["last_bar"] = stamp
                return self.exit_target(pos, same_day, target, inside, "target %.2f hit in the %s bar" % (target, b["time"]))
            self.trail(pos, h, bar_at)
            pos["last_bar"] = stamp
            if pos["timeframe"] == "5m":
                pos["bars"] = pos.get("bars", 0) + 1
                if spec is not None:
                    if sig is None:
                        s = self.market.series(pos["symbol"], "5m", now)
                        sig = (s, lab.signals(spec, s)) if s is not None else (None, None)
                    s, g = sig
                    if s is not None and g and g.get("exit") is not None:
                        k = [j for j in range(s.n) if s.date[j] == day and s.minute[j] == b["minute"]]
                        if k and g["exit"][k[0]]:
                            pos["exit_pending"], pos["exit_pending_bar"] = "the strategy's exit rule", stamp
                if not pos.get("exit_pending") and pos.get("max_bars") and pos["bars"] >= int(pos["max_bars"]):
                    pos["exit_pending"], pos["exit_pending_bar"] = "the maximum holding of %d bars" % pos["max_bars"], stamp
        self.save()

    def exit_stop(self, pos, same_day, price, at, why):
        if same_day:
            return self.fill(pos, "stop", price, at, why)
        return self.exit_order(pos, at, price, why)

    def exit_target(self, pos, same_day, price, at, why):
        if same_day:
            return self.fill(pos, "target", price, at, why)
        return self.exit_order(pos, at, price, why)

    def trail(self, pos, high, at):
        b = self.bucket(pos["strategy"]) or {}
        after = b.get("trail_after_r")
        if not after or pos["status"] != "open":
            return
        r = pos["entry_price"] - pos["stop0"]
        if r <= 0 or high - pos["entry_price"] < float(after) * r:
            return
        new = tick_price(pos["entry_price"] + float(b.get("trail_lock_r") or 0) * r, "down")
        if new > pos["stop"] + 1e-9 and new < high:
            old = pos["stop"]
            pos["stop"] = new
            self.save()
            self.event(at, "trail", "%s %s: the price reached %s (%.2fR): stop raised from %.2f to %.2f" % (
                pos["id"], pos["symbol"], "%.2f" % high, (high - pos["entry_price"]) / r, old, new), position=pos["id"])

    def flat_by(self, day, now):
        lab = lab_api()
        for pos in self.positions("open"):
            if pos["product"] != "MIS" or pos.get("kind") == "basket":
                continue
            fb = self.clock.flat_by(pos["symbol"], day)
            if now.time() < fb:
                continue
            p, _ = self.market.last_price(pos["symbol"], day, now)
            if p is None:
                p = pos["entry_price"]
            px = tick_price(p - lab.slippage(p, self.costs(pos)), "down")
            at = datetime.datetime.combine(now.date(), fb)
            self.exit_order(pos, max(at, now.replace(second=0)), px, "flat by %s IST (MIS)" % hhmm(fb))

    # ------------------------------------------------------------ kill switch

    def kill_on(self, day):
        return bool(risk_api().kill_active(read_json(self.paths.kill), day))

    def kill_check(self, day, now):
        if not self.kill_on(day) or self.state.get("kill_handled"):
            return
        kill = read_json(self.paths.kill) or {}
        self.state["kill_handled"] = True
        self.state["entries_stopped"] = "kill switch on at %s IST (%s)" % (str(kill.get("at", ""))[11:16],
                                                                        kill.get("reason") or "no reason given")
        self.save()
        self.event(now, "kill", "KILL SWITCH: %s. No new entries today; every auto-pilot position is flattened on "
                                "paper" % self.state["entries_stopped"])
        self.cancel_working(day, now, "the kill switch", baskets=True)
        lab = lab_api()
        for pos in self.positions("open"):
            if pos.get("kind") == "basket":
                pos["close_at_eod"] = "the kill switch"
                self.save()
                self.event(now, "note", "%s: the F&O basket closes at today's close (end-of-day prices)" % pos["id"],
                           position=pos["id"])
                continue
            p, _ = self.market.last_price(pos["symbol"], day, now)
            px = tick_price((p or pos["entry_price"]) - lab.slippage(p or pos["entry_price"], self.costs(pos)), "down")
            self.exit_order(pos, now, px, "the kill switch")

    # ------------------------------------------------------------ F&O baskets (one position, run at the close)
    #
    # trader-lab tests option strategies on NSE's end-of-day F&O bhavcopy, so the auto-pilot runs them the same way:
    # the decision (trader-risk on the basket as ONE position, the machine yes for every leg) is taken in the entry
    # window; the basket fills at the close on that session's settle prices from trader-archive (the paper broker's
    # own basket fill, hedge first, with its slippage), and from the next session it is marked at each close with
    # the spec's profit target, stop and days-before-expiry exit, else cash-settled at expiry.

    def consider_basket(self, c, b, day, now):
        cid = c["id"]
        if len(self.strategy_positions(c["strategy"], "open", "working", "placing")) >= int(b["max_positions"]):
            return self.note_once(cid, "%s has its %d position(s) open: its max_positions" % (c["strategy"],
                                                                                            b["max_positions"]), now)
        margin = float((c.get("basket") or {}).get("margin_required") or 0)
        if margin > float(b["capital"]):
            return self.note_once(cid, "the basket needs about %s of margin, more than %s's capital %s" % (
                inr(margin), c["strategy"], inr(b["capital"])), now)
        plan = c["plan"]
        und = plan["strategy"].get("underlying") or c["symbol"]
        pid = self.new_id(day)
        path = os.path.join(self.paths.orders, "%s-basket.json" % pid)
        orders = json.loads(json.dumps(plan["orders"]))
        orders["basket"] = True
        orders["note"] = "AUTO-PILOT (paper) basket %s: %s" % (pid, cid)
        ex = (self.compiled(c["strategy"]) or {}).get("exit") or {}
        credit = float((((c.get("basket") or {}).get("net_premium")) or {}).get("received") or 0)
        legs0 = orders.get("orders") or []
        if ex.get("stop_mult") and credit > 0 and not any(o.get("risk_rule") for o in legs0):
            # the spec's own stop (a loss of N x the credit, checked at the close) is the basket's risk rule
            for o in legs0:
                o["risk_rule"] = {"max_loss": rp(float(ex["stop_mult"]) * credit)}
        write_json(path, orders)
        write_json(self.paths.w("strategy.json"), plan["strategy"])
        self.write_chain(und, self.clock.prev_session(day))
        res = self.run_risk(path, now)
        rows = res.get("results") or []
        reasons = []
        for r in rows:
            reasons += [x for x in r.get("reasons") or [] if x not in reasons]
        if not rows or reasons:
            self.state["seq"] -= 1
            self.save()
            return self.note_once(cid, "trader-risk BLOCK (basket): %s" % "; ".join(
                reasons or [res.get("error") or "no verdict"]), now, kind="blocked", reasons=reasons)
        if self.dry_run:
            self.state["acted"][cid] = "dry-run"
            self.save()
            return self.event(now, "dry-run", "would place the basket %s (trader-risk PASS); dry run, nothing placed"
                              % cid, candidate=cid)
        legs = orders.get("orders") or []
        pos = {"id": pid, "kind": "basket", "strategy": c["strategy"], "candidate": cid, "tier": c["tier"],
               "symbol": und, "product": "NRML", "qty": len(legs), "entry_limit": margin, "order_file": path,
               "status": "placing", "placed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "expiry": plan.get("expiry"),
               "signal_date": c.get("signal_date"), "plan_date": day, "exits": [], "timeframe": "1d",
               "spec_hash": c.get("spec_hash"), "p": (c.get("evidence") or {}).get("p"), "max_loss": None,
               "structure": (c.get("basket") or {}).get("structure")}
        self.state.setdefault("positions", []).append(pos)
        self.state["acted"][cid] = pid
        self.save()
        self.confirm_basket(pos, path, now)

    def confirm_basket(self, pos, path, now):
        broker = broker_api()
        day = now.date().isoformat()
        bid = broker.basket_id((read_json(path) or {}).get("orders") or [])
        placed = [r for r in broker.records(broker.load_state(self.paths.broker_state), today=day, status="placed")
                  if r.get("basket_id") == bid]
        if not placed:
            rc, out, err = self.run_broker(["confirm", "--basket", "--orders", path, "--config",
                                            self.paths.w("config.json"), "--risk", risk_file(path), "--state",
                                            self.paths.broker_state, "--portfolio", self.paths.portfolio,
                                            "--strategy", self.paths.w("strategy.json"), "--options",
                                            self.paths.w("options.json"), "--yes-text", YES_TEXT, "--actor",
                                            "autopilot", "--now", now.strftime("%Y-%m-%d %H:%M:%S")])
            if rc != 0:
                pos.update({"status": "refused", "closed_why": (err or out).strip()[-300:]})
                self.save()
                return self.event(now, "refused", "%s: the paper broker refused the basket: %s" % (
                    pos["candidate"], pos["closed_why"]), position=pos["id"])
        pos.update({"status": "working", "basket_id": bid})
        self.save()
        return self.event(now, "placed", "placed %s on paper: the %s basket %s (%d legs, one position); trader-risk "
                                         "PASS, machine yes recorded (actor autopilot); it fills at the close on the "
                                         "session's settle prices" % (pos["id"], pos.get("structure") or "F&O",
                                                                      pos["candidate"], pos["qty"]),
                          position=pos["id"], candidate=pos["candidate"])

    def write_chain(self, underlying, day):
        """trader/autopilot/work/options.json: the archive's end-of-day chain of `day` in trader-options' shape
        (one document per expiry; settle prices, no bid/ask), which the paper broker fills and marks at."""
        docs = eod_chain(underlying, day, self.market.archive)
        if docs:
            write_json(self.paths.w("options.json"), docs)
        return docs

    def baskets_eod(self, day, now):
        baskets = [p for p in self.positions("working", "open", "placing") if p.get("kind") == "basket"]
        if not baskets:
            return
        lab = lab_api()
        broker = broker_api()
        for pos in baskets:
            if pos["status"] == "placing":
                self.confirm_basket(pos, pos["order_file"], datetime.datetime.strptime(pos["placed_at"],
                                                                                        "%Y-%m-%d %H:%M:%S"))
            docs = self.write_chain(pos["symbol"], day)
            if not docs:
                self.event(now, "note", "%s: no end-of-day %s chain for %s in the archive (archive.py update after the "
                                        "close): not filled or marked today" % (pos["id"], pos["symbol"], day),
                           position=pos["id"])
                continue
            if pos["status"] == "working":
                if not self.fill_basket(pos, pos["order_file"], now, "entry"):
                    pos.update({"status": "cancelled", "closed_why": "a DAY basket: not filled at the close"})
                    self.save()
                continue
            if pos["status"] != "open" or pos.get("entry_day") == day:
                continue
            if day >= (pos.get("expiry") or "9999"):
                self.settle_basket(pos, day, now, docs)
                continue
            if pos.get("close_at_eod"):
                self.close_basket(pos, day, now, pos.pop("close_at_eod"))
                continue
            spec_doc = read_json(os.path.join(self.paths.lab, pos["strategy"], "spec.json")) or {}
            try:
                ex = lab.validate_spec(spec_doc)["exit"] if spec_doc else {}
            except lab.LabError:
                ex = {}
            pf = read_json(self.paths.portfolio) or {}
            legs = [p for p in broker.fno_positions(pf, pos.get("strategy_id"))]
            marks = chain_marks(docs)
            gross, base = 0.0, 0.0
            for l in legs:
                m = marks.get((float(l["strike"]), str(l["option_type"]).lower(), str(l["expiry"])))
                if m is None:
                    gross = None
                    break
                gross += (m - float(l["avg_price"])) * int(l["qty"])
                base += float(l["avg_price"]) * abs(int(l["qty"]))
            if gross is None:
                self.event(now, "note", "%s: a leg has no settle price in the %s chain: not marked" % (pos["id"], day))
                continue
            pos["mtm"] = {"day": day, "gross": rp(gross)}
            prem = abs(float(pos.get("premium") or base)) or 1.0
            reason = None
            if ex.get("target_pct") is not None and gross >= prem * float(ex["target_pct"]) / 100.0:
                reason = "profit target %s%% of the premium" % ex["target_pct"]
            elif ex.get("stop_mult") is not None and gross <= -prem * float(ex["stop_mult"]):
                reason = "stop at %sx the premium" % ex["stop_mult"]
            elif ex.get("exit_dte") and (datetime.date.fromisoformat(pos["expiry"]) -
                                         datetime.date.fromisoformat(day)).days <= int(ex["exit_dte"]):
                reason = "%d days before expiry" % int(ex["exit_dte"])
            self.save()
            self.event(now, "mtm", "%s %s basket marked at the %s settle prices: %s before exit charges%s" % (
                pos["id"], pos["symbol"], day, inr(gross), ("; exit: " + reason) if reason else ""), position=pos["id"])
            if reason:
                self.close_basket(pos, day, now, reason)

    def fill_basket(self, pos, path, now, what):
        broker = broker_api()
        day = now.date().isoformat()
        rc, out, err = self.run_broker(["fill", "--basket", "--orders", path, "--config", self.paths.w("config.json"),
                                        "--state", self.paths.broker_state, "--portfolio", self.paths.portfolio,
                                        "--trades", self.paths.trades, "--strategy", self.paths.w("strategy.json"),
                                        "--options", self.paths.w("options.json"), "--bands", self.paths.w("bands.csv"),
                                        "--now", now.strftime("%Y-%m-%d %H:%M:%S")])
        if rc != 0:
            self.event(now, "note", "%s: the paper basket %s fill did not happen: %s" % (pos["id"], what,
                                                                                         (err or out).strip()[-300:]),
                       position=pos["id"])
            return False
        bid = broker.basket_id((read_json(path) or {}).get("orders") or [])
        rows = [t for t in (read_json(self.paths.trades) or {}).get("trades") or [] if t.get("basket_id") == bid]
        charges = rp(sum(float(t.get("charges") or 0) for t in rows))
        realised = [float(t["realised_pnl"]) for t in rows if t.get("realised_pnl") is not None]
        if what == "entry":
            sid = rows[0].get("strategy_id") if rows else None
            prem = rp(sum((1 if t["side"] == "SELL" else -1) * float(t["value"]) for t in rows))
            pos.update({"status": "open", "strategy_id": sid, "filled_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_day": day, "entry_charges": charges, "premium": prem})
            self.save()
            self.event(now, "fill", "%s filled on paper: %s basket %s (%d legs, hedge first) at the %s settle prices, "
                                    "net premium %s %s; charges %s" % (
                                        pos["id"], pos["symbol"], sid, len(rows), day, inr(abs(prem)),
                                        "received" if prem > 0 else "paid", inr(charges)),
                       position=pos["id"], strategy=pos["strategy"], charges=charges, realised=None)
            return True
        net = rp(sum(realised))
        self.book_basket_close(pos, now, what, charges, net)
        return True

    def book_basket_close(self, pos, now, why, charges, net):
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        pf = read_json(self.paths.portfolio) or {}
        st = (pf.get("strategies") or {}).get(pos.get("strategy_id")) or {}
        total = st.get("realised")
        pos.setdefault("exits", []).append({"at": stamp, "leg": "basket", "price": None, "qty": pos["qty"],
                                            "charges": charges, "realised": net, "why": why})
        pos.update({"status": "closed", "closed_at": stamp, "closed_why": why,
                    "result": {"net": total if total is not None else net, "r": None,
                               "charges": rp((pos.get("entry_charges") or 0) + charges)}})
        margin = float(pos.get("entry_limit") or 0)
        if margin > 0 and pos["result"]["net"] is not None:
            pos["result"]["r"] = round(float(pos["result"]["net"]) / margin, 5)
        self.state.setdefault("closed_today", []).append(pos["id"])
        self.save()
        self.event(now, "exit", "%s closed on paper: the %s basket %s (%s); charges %s; realised %s after all its "
                                "charges" % (pos["id"], pos["symbol"], pos.get("strategy_id"), why, inr(charges),
                                             inr(pos["result"]["net"] or 0)),
                   position=pos["id"], strategy=pos["strategy"], charges=charges, realised=net)

    def close_basket(self, pos, day, now, why):
        """Close every leg as ONE basket ticket (ticket.py adjust --close-all), trader-risk, the machine yes,
        then the paper basket fill at the close's settle prices."""
        if pos.get("kind") != "basket" or pos["status"] != "open":
            return
        docs = self.write_chain(pos["symbol"], day if now.time() >= datetime.time(15, 30) else
                                self.clock.prev_session(day))
        if not docs:
            return self.event(now, "note", "%s: no chain to close the basket at (%s); close it with ticket.py adjust "
                                           "--strategy %s --close-all" % (pos["id"], why, pos.get("strategy_id")))
        n = len(pos.get("exit_orders") or []) + 1
        path = os.path.join(self.paths.orders, "%s-close-%d.json" % (pos["id"], n))
        rc, out, err = self.run_broker(["adjust", "--strategy", str(pos.get("strategy_id")), "--close-all",
                                        "--options", self.paths.w("options.json"), "--orders", path, "--portfolio",
                                        self.paths.portfolio, "--config", self.paths.w("config.json"), "--now",
                                        now.strftime("%Y-%m-%d %H:%M:%S")])
        if rc != 0:
            return self.event(now, "note", "%s: the close-all ticket failed: %s" % (pos["id"], (err or out).strip()[-300:]))
        pos.setdefault("exit_orders", []).append({"file": path})
        self.save()
        res = self.run_risk(path, now)
        bad = [x for r in res.get("results") or [] for x in r.get("reasons") or []]
        if not res.get("results") or bad:
            return self.event(now, "note", "%s: trader-risk did not pass the close-all ticket: %s" % (
                pos["id"], "; ".join(bad or [res.get("error") or "no verdict"])))
        rc, out, err = self.run_broker(["confirm", "--basket", "--orders", path, "--config", self.paths.w("config.json"),
                                        "--risk", risk_file(path), "--state", self.paths.broker_state, "--portfolio",
                                        self.paths.portfolio, "--options", self.paths.w("options.json"),
                                        "--yes-text", YES_TEXT, "--actor", "autopilot", "--now",
                                        now.strftime("%Y-%m-%d %H:%M:%S")])
        if rc != 0:
            return self.event(now, "note", "%s: the paper broker refused the close-all: %s" % (pos["id"],
                                                                                              (err or out).strip()[-300:]))
        self.fill_basket(pos, path, now, why)

    def settle_basket(self, pos, day, now, docs):
        """Cash settlement at expiry (ticket.py settle): NSE's final settlement price from the archive's settle
        file when it has one, else the underlying's closing value in that day's F&O bhavcopy."""
        price, src = None, None
        arc = archive_api()
        if arc is not None and self.market.archive:
            try:
                got = arc.load_settle(day, root=self.market.archive) or {}
            except Exception:
                got = {}
            for k, v in (got.items() if isinstance(got, dict) else []):
                if str(k).upper().startswith(pos["symbol"]):
                    price, src = float(v), "NSE's final settlement price (archive)"
                    break
        if price is None:
            price = next((d.get("spot") for d in docs if d.get("spot")), None)
            src = "the underlying's close in the %s F&O bhavcopy" % day
        if price is None:
            return self.event(now, "note", "%s: no settlement price for %s: run ticket.py settle yourself" % (
                pos["id"], day))
        before = read_json(self.paths.trades) or {}
        n0 = len(before.get("trades") or [])
        rc, out, err = self.run_broker(["settle", "--date", day, "--price", "%s=%.2f" % (pos["symbol"], price),
                                        "--portfolio", self.paths.portfolio, "--trades", self.paths.trades, "--config",
                                        self.paths.w("config.json"), "--options", self.paths.w("options.json"),
                                        "--now", now.strftime("%Y-%m-%d %H:%M:%S")])
        if rc != 0:
            return self.event(now, "note", "%s: settlement failed: %s" % (pos["id"], (err or out).strip()[-300:]))
        rows = [t for t in ((read_json(self.paths.trades) or {}).get("trades") or [])[n0:]
                if t.get("strategy_id") == pos.get("strategy_id")]
        charges = rp(sum(float(t.get("charges") or 0) for t in rows))
        net = rp(sum(float(t.get("realised_pnl") or 0) for t in rows))
        self.book_basket_close(pos, now, "expiry: cash-settled at %.2f (%s)" % (price, src), charges, net)

    # ------------------------------------------------------------ end of day

    def end_of_day(self, day, now):
        self.cancel_working(day, now, "a DAY order, not filled by the close")
        for p in self.positions("open"):
            if p["product"] == "MIS" and p.get("kind") != "basket":
                self.event(now, "note", "%s %s is still open after the close (no bar at the flat-by)" % (p["id"],
                                                                                                       p["symbol"]))
        self.baskets_eod(day, now)
        self.daily_exit_rules(day, now)
        recs = self.write_records(day)
        journal = self.journal_hook(day, now)
        cal = self.calibration(now)
        fwd = self.forward_update(day, now) if self.ap.get("forward_update") else {"skipped": "off in the config"}
        self.state["phases"]["eod"] = now.strftime("%H:%M")
        self.save()
        rep = build_report(self.paths.workdir, day, extra={"journal": journal, "calibration": cal, "forward": fwd,
                                                             "records": recs})
        self.event(now, "eod", "end of day: %d trade(s) closed, realised %s after %s charges; report in "
                               "trader/autopilot/days/%s/" % (rep["totals"]["closed"], inr(rep["totals"]["realised"]),
                                                              inr(rep["totals"]["charges"]), day))
        save_report(self.paths, rep)
        return rep

    def daily_exit_rules(self, day, now):
        """Delivery positions held overnight: the strategy's own exit rule and its maximum holding, judged on the
        day's daily bar (like trader-lab: out at the next session's open)."""
        lab = lab_api()
        for pos in self.positions("open"):
            if pos.get("kind") == "basket" or pos["timeframe"] != "1d" or pos["product"] != "CNC":
                continue
            if pos.get("eod_counted") == day:
                continue
            pos["eod_counted"] = day
            pos["bars"] = pos.get("bars", 0) + 1
            spec = self.compiled(pos["strategy"])
            why = None
            if spec is not None:
                s = self.market.series(pos["symbol"], "1d", now)
                if s is not None and s.n and s.date[-1] == day:
                    g = lab.signals(spec, s)
                    if g.get("exit") is not None and g["exit"][-1]:
                        why = "the strategy's exit rule on the %s close" % day
            if why is None and pos.get("max_bars") and pos["bars"] >= int(pos["max_bars"]):
                why = "the maximum holding of %d sessions" % int(pos["max_bars"])
            if why:
                pos["exit_pending"] = why
                pos["exit_pending_bar"] = "%s 99:99" % day
                self.event(now, "note", "%s %s: %s; out at the next session's open" % (pos["id"], pos["symbol"], why),
                           position=pos["id"])
        self.save()

    def write_records(self, day):
        """Each strategy's closed auto-pilot trades: trader/autopilot/records/<strategy>.json (paper fills, exact
        charges), and the same trades fed into trader-lab's forward record for that strategy."""
        out = {}
        done = [p for p in self.state.get("positions", []) if p["status"] == "closed" and not p.get("recorded")]
        for p in done:
            out.setdefault(p["strategy"], []).append(p)
        for name, ps in out.items():
            path = os.path.join(self.paths.records, "%s.json" % name)
            doc = read_json(path) or {"schema": "bharat-trader/autopilot-record/1", "strategy": name, "trades": []}
            for p in ps:
                doc["trades"].append(trade_row(p))
                p["recorded"] = True
            write_json(path, doc)
            self.feed_lab(name, ps)
        self.state["positions"] = [p for p in self.state.get("positions", []) if p["status"] in ("open",)]
        self.save()
        return {k: len(v) for k, v in out.items()}

    def feed_lab(self, name, ps):
        """Paper fills replace trader-lab's simulated trade for the same signal in its forward record, so
        promotion is judged on what the paper account actually got."""
        path = os.path.join(self.paths.lab, name, "forward.json")
        if not os.path.isdir(os.path.dirname(path)):
            return
        doc = read_json(path) or {"schema": "bharat-trader/lab-forward/1", "name": name, "trades": []}
        trades = doc.get("trades") or []
        for p in ps:
            if p.get("kind") == "basket" or not p.get("result") or p["result"].get("r") is None:
                continue
            row = lab_row(p)
            trades = [t for t in trades if not (t.get("symbol") == row["symbol"] and
                                                t.get("signal_date") == row["signal_date"] and
                                                t.get("spec_hash") == row["spec_hash"])]
            trades.append(row)
        trades.sort(key=lambda t: (t["exit_date"], t["exit_time"], t["symbol"]))
        doc["trades"] = trades
        write_json(path, doc)

    def journal_hook(self, day, now):
        """trader-journal's own sync (journal.sync(Paths(workdir), bars, offline, now)): it rebuilds
        trader/journal.json from the paper fills, the yes records (the auto-pilot's included, labelled) and the
        risk verdicts. Skipped, and said so, when trader-journal is not installed next to this folder."""
        j = journal_api()
        if j is None or not hasattr(j, "sync") or not hasattr(j, "Paths"):
            return {"status": "skipped", "why": "trader-journal is not installed next to trader-autopilot"}
        try:
            doc = j.sync(j.Paths(self.paths.workdir), self.market.data, not self.fetch, now)
        except Exception as e:  # the journal must not stop the end of day
            return {"status": "failed", "why": str(e)[:300]}
        s = doc.get("stats") or {}
        today = [e for e in doc.get("entries") or [] if str(e.get("closed_at") or "")[:10] == day]
        return {"status": "synced", "file": "trader/journal.json", "entries": len(doc.get("entries") or []),
                "closed_today": len(today), "trades": s.get("trades"), "expectancy": s.get("expectancy"),
                "words": "journal synced: %d closed trade(s), %d today" % (len(doc.get("entries") or []), len(today))}

    def calibration(self, now):
        ct = cotrader_api()
        try:
            rep = ct.calibration_report(self.paths.workdir, now, self.market.data, self.market.archive, self.fetch,
                                        bool(self.ap.get("calibration_demote")))
        except errors() as e:
            return {"error": str(e)}
        g = rep.get("overall") or {}
        return {"resolved": rep.get("resolved"), "pending": rep.get("pending"), "brier": g.get("brier"),
                "words": rep.get("words"), "demoted": rep.get("demoted_now") or []}

    def forward_update(self, day, now):
        """trader-lab's forward test on today's bars for every enabled strategy: it settles the record, re-checks the
        verdict (WATCH -> PROMOTE once the forward record holds up; PROMOTE -> DEMOTE when it decays) and drafts
        the next plans. Returns {strategy: {"before", "after", "why", "forward"}}."""
        lab = lab_api()
        out = {}
        for name in sorted(self.ap["strategies"]):
            d = os.path.join(self.paths.lab, name)
            doc = read_json(os.path.join(d, "spec.json"))
            st = read_json(os.path.join(d, "status.json")) or {}
            if not doc:
                out[name] = {"error": "no trader-lab run in trader/lab/%s" % name}
                continue
            before = st.get("verdict")
            try:
                spec = lab.validate_spec(doc)
                if spec.get("kind") == "options":
                    data, _, _ = lab.load_data(spec, None, None, False, now, archive=self.market.archive)
                else:
                    data, _, _ = lab.load_data(spec, self.market.data, os.path.join(self.paths.lab, "data"),
                                               self.fetch, now, archive=self.market.archive)
                r = lab.forward(doc, data, self.paths.lab, None, None, now, True)
            except lab.LabError as e:
                out[name] = {"before": before, "error": str(e)}
                continue
            out[name] = {"before": before, "after": r["verdict"], "why": r["why"], "forward": r["forward"],
                         "changed": before != r["verdict"]}
        self.state["forward"] = out
        self.save()
        return out


# ---------------------------------------------------------------- end-of-day option chains

def eod_chain(underlying, day, root):
    """The archive's F&O bhavcopy board of `day` as trader-options' chain documents, one per expiry:
    {"underlying", "expiry", "asof", "spot", "forward", "rows": [{"strike", "ce": {...}, "pe": {...}}]}, priced at
    NSE's settle prices (the close when there is no settle). [] when the archive does not have the day."""
    arc = archive_api()
    if arc is None or not root or not day:
        return []
    try:
        ch = arc.load_fo_chain_eod(underlying, day, root=root)
    except Exception:  # an unreadable archive day is "no chain"
        return []
    if not ch or not ch.get("options"):
        return []
    futs = {str(f.get("expiry")): f for f in ch.get("futures") or []}
    by = {}
    for o in ch["options"]:
        px = o.get("settle") or o.get("close")
        if not px or not o.get("expiry") or o.get("strike") is None:
            continue
        rows = by.setdefault(str(o["expiry"]), {})
        row = rows.setdefault(float(o["strike"]), {"strike": float(o["strike"])})
        row[str(o["option_type"]).lower()] = {"price": float(px), "ltp": float(o.get("close") or px),
                                              "settle": float(px), "oi": o.get("oi"), "lot": o.get("lot")}
    out = []
    for e in sorted(by):
        f = futs.get(e) or {}
        out.append({"schema": "bharat-trader/options/1", "underlying": underlying.upper(), "expiry": e,
                    "asof": "%sT15:30:00+05:30" % day, "spot": ch.get("spot"),
                    "forward": f.get("settle") or f.get("close") or ch.get("spot"), "rate": 0.0,
                    "kind": "index" if underlying.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else "stock",
                    "rows": [by[e][k] for k in sorted(by[e])],
                    "source": "trader-archive F&O bhavcopy %s (settle prices, end of day)" % day})
    return out


def chain_marks(docs):
    """{(strike, "ce"|"pe", expiry): settle price} from eod_chain()'s documents."""
    out = {}
    for d in docs or []:
        for r in d.get("rows") or []:
            for side in ("ce", "pe"):
                if r.get(side):
                    out[(float(r["strike"]), side, str(d["expiry"]))] = float(r[side]["price"])
    return out


# ---------------------------------------------------------------- records and reports

def trade_row(p):
    x = (p.get("exits") or [{}])[-1]
    return {"position": p["id"], "candidate": p["candidate"], "strategy": p["strategy"], "tier": p["tier"],
            "symbol": p["symbol"], "product": p["product"], "qty": p["qty"], "signal_date": p.get("signal_date"),
            "entry_at": p.get("filled_at"), "entry": p.get("entry_price"), "stop": p.get("stop0"),
            "target": p.get("target"), "exit_at": x.get("at") or p.get("closed_at"), "exit": x.get("price"),
            "exit_why": p.get("closed_why"), "charges": (p.get("result") or {}).get("charges"),
            "net": (p.get("result") or {}).get("net"), "r": (p.get("result") or {}).get("r"), "p": p.get("p"),
            "source": "trader-autopilot paper fills"}


def lab_row(p):
    """A closed paper position in trader-lab's forward-trade shape."""
    x = p["exits"][-1]
    entry, qty = float(p["entry_price"]), int(p["qty"])
    gross = rp((float(x["price"]) - entry) * qty)
    net = float(p["result"]["net"])
    return {"symbol": p["symbol"], "entry_date": p["filled_at"][:10],
            "entry_time": (p.get("entry_time") or p["filled_at"][11:16]) if p["timeframe"] == "5m" else "",
            "exit_date": x["at"][:10],
            "exit_time": x["at"][11:16] if p["timeframe"] == "5m" else "", "entry": entry, "exit": float(x["price"]),
            "stop": p["stop0"], "target": p["target"], "qty": qty, "gross": gross, "charges": rp(gross - net),
            "net": net, "r": p["result"]["r"], "reason": "paper: %s" % p.get("closed_why"), "bars": p.get("bars", 0),
            "signal_date": p.get("signal_date"), "spec_hash": p.get("spec_hash"),
            "settled": x["at"][:16], "source": "trader-autopilot paper fills"}


def read_timeline(paths, day):
    p = paths.day(day, "timeline.jsonl")
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    return out


def build_report(workdir, day, extra=None):
    """The day's report from the timeline, the paper account's trades log and the auto-pilot's records."""
    paths = Paths(workdir)
    tl = read_timeline(paths, day)
    st = read_json(paths.state) or {}
    fills, by = [], {}
    for e in tl:
        if e["kind"] in ("fill", "exit") and e.get("position"):
            fills.append(e)
    rows = []
    for name in sorted(os.listdir(paths.records)) if os.path.isdir(paths.records) else []:
        doc = read_json(os.path.join(paths.records, name)) or {}
        for t in doc.get("trades") or []:
            if str(t.get("exit_at") or "")[:10] == day:
                rows.append(t)
    trades = read_json(paths.trades) or {}
    pos_ids = {e["position"] for e in fills}
    charges_today = 0.0
    realised_today = 0.0
    for e in fills:
        charges_today += float(e.get("charges") or 0)
        if e.get("realised") is not None:
            realised_today += float(e["realised"])
        s = by.setdefault(e.get("strategy") or _strategy_of(st, rows, e["position"]), {
            "fills": 0, "closed": 0, "charges": 0.0, "realised": 0.0, "wins": 0})
        s["fills"] += 1
        s["charges"] = rp(s["charges"] + float(e.get("charges") or 0))
        if e["kind"] == "exit":
            s["closed"] += 1
            s["realised"] = rp(s["realised"] + float(e.get("realised") or 0))
            s["wins"] += 1 if float(e.get("realised") or 0) > 0 else 0
    open_ = [{"id": p["id"], "strategy": p["strategy"], "symbol": p["symbol"], "product": p["product"],
              "qty": p["qty"], "entry": p.get("entry_price"), "stop": p.get("stop"), "target": p.get("target"),
              "since": p.get("filled_at"), "kind": p.get("kind") or "equity"}
             for p in st.get("positions", []) if p["status"] == "open"] if st.get("day") == day else []
    pf = read_json(paths.portfolio) or {}
    rep = {"schema": SCHEMA_REPORT, "date": day, "mode": st.get("mode") or "paper", "paper": True,
           "phases": st.get("phases") if st.get("day") == day else {},
           "pre_open": st.get("pre_open") if st.get("day") == day else None,
           "totals": {"fills": len(fills), "closed": sum(1 for e in fills if e["kind"] == "exit"),
                      "charges": rp(charges_today), "realised": rp(realised_today),
                      "positions_touched": len(pos_ids)},
           "by_strategy": by, "closed_trades": rows, "open_positions": open_,
           "account": {"cash": pf.get("cash"), "positions": pf.get("positions") or {}},
           "entries_stopped": st.get("entries_stopped") if st.get("day") == day else None,
           "events": len(tl), "timeline": tl[-300:], "disclaimer": DISCLAIMER}
    rep.update(extra or {})
    del trades
    return rep


def _strategy_of(st, rows, pid):
    for p in st.get("positions", []):
        if p["id"] == pid:
            return p["strategy"]
    for r in rows:
        if r.get("position") == pid:
            return r["strategy"]
    return "?"


def report_md(rep):
    L = ["# Auto-pilot report, %s (%s, paper only)" % (rep["date"], rep["mode"]), ""]
    ph = rep.get("phases") or {}
    L.append("Pre-open %s, end of day %s IST." % (ph.get("pre_open") or "not run", ph.get("eod") or "not run"))
    pre = rep.get("pre_open") or {}
    if pre:
        L.append("")
        L.append("- Market regime: %s" % pre.get("regime"))
        L.append("- Co-trader brief: %s" % pre.get("cotrader"))
        L.append("- Data check: %s" % pre.get("doctor"))
    t = rep["totals"]
    L += ["", "## Today", "",
          "%d fill(s), %d trade(s) closed. Realised %s after charges; charges paid today %s." % (
              t["fills"], t["closed"], inr(t["realised"]), inr(t["charges"]))]
    if rep.get("entries_stopped"):
        L.append("New entries stopped: %s." % rep["entries_stopped"])
    if rep["by_strategy"]:
        L += ["", "| strategy | fills | closed | won | charges | realised after charges |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, s in sorted(rep["by_strategy"].items()):
            L.append("| %s | %d | %d | %d | %s | %s |" % (name, s["fills"], s["closed"], s["wins"], inr(s["charges"]),
                                                          inr(s["realised"])))
    if rep["closed_trades"]:
        L += ["", "## Closed trades", "", "| position | stock | qty | entry | exit | why | charges | net | R |",
              "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |"]
        for r in rep["closed_trades"]:
            L.append("| %s | %s %s | %d | %.2f | %.2f | %s | %s | %s | %s |" % (
                r["position"], r["symbol"], r["product"], r["qty"], r["entry"], r["exit"], r["exit_why"],
                inr(r["charges"] or 0), inr(r["net"] or 0), "%+.2f" % r["r"] if r.get("r") is not None else "–"))
    if rep["open_positions"]:
        L += ["", "## Open at the close (carried)", ""]
        for p in rep["open_positions"]:
            L.append("- %s %s %s %d from %s, stop %s, target %s" % (p["id"], p["symbol"], p["product"], p["qty"],
                                                                   p["since"], p["stop"], p["target"]))
    fwd = rep.get("forward") or {}
    if fwd:
        L += ["", "## Forward records (trader-lab)", ""]
        if fwd.get("skipped"):
            L.append("Skipped: %s." % fwd["skipped"])
        for name, f in sorted((k, v) for k, v in fwd.items() if isinstance(v, dict)):
            if f.get("error"):
                L.append("- %s: %s" % (name, f["error"]))
                continue
            fr = f.get("forward") or {}
            L.append("- %s: %s -> %s%s. %d forward trade(s), %+.3fR a trade after charges. %s" % (
                name, f.get("before"), f.get("after"), " (CHANGED)" if f.get("changed") else "", fr.get("trades", 0),
                fr.get("expectancy_r") or 0.0, f.get("why") or ""))
    cal = rep.get("calibration") or {}
    if cal:
        L += ["", "## Calibration (co-trader)", "", cal.get("words") or cal.get("error") or ""]
        if cal.get("demoted"):
            L.append("Demoted for poor calibration: %s." % ", ".join(cal["demoted"]))
    j = rep.get("journal") or {}
    if j:
        L += ["", "Journal: %s%s." % (j.get("status"), (" (%s)" % j["why"]) if j.get("why") else "")]
    L += ["", "## Timeline", ""]
    for e in rep.get("timeline") or []:
        L.append("- %s %s" % (e["at"][11:16], e["words"]))
    L += ["", "What this does NOT show: a paper fill is a model (fills at the bar's prices, the spec's slippage on "
              "market and stop orders); a real order can fill worse or not at all. A few days of results prove "
              "little either way.", "", DISCLAIMER, ""]
    return "\n".join(L)


def save_report(paths, rep):
    write_json(paths.day(rep["date"], "report.json"), rep)
    with open(paths.day(rep["date"], "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report_md(rep))


# ---------------------------------------------------------------- the lock and the loop

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # on Windows os.kill(pid, 0) would TERMINATE the process: ask the kernel instead
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        finally:
            k32.CloseHandle(h)
        return bool(ok) and code.value == 259               # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock(paths, mode, now):
    """trader/autopilot/lock.json, created exclusively: one loop per workdir. A lock left by a process that is no
    longer running is taken over (and said so)."""
    os.makedirs(paths.ap, exist_ok=True)
    body = json.dumps({"pid": os.getpid(), "started": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode})
    for _ in range(2):
        try:
            fd = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            old = read_json(paths.lock) or {}
            if pid_alive(old.get("pid")):
                raise AutopilotError("another auto-pilot loop is running in this workdir (pid %s, since %s): stop it "
                                     "with autopilot.py stop" % (old.get("pid"), old.get("started")))
            try:
                os.remove(paths.lock)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return True
    raise AutopilotError("could not take the lock %s" % paths.lock)


def release_lock(paths):
    old = read_json(paths.lock) or {}
    if old.get("pid") == os.getpid():
        try:
            os.remove(paths.lock)
        except OSError:
            pass


def run_loop(workdir, once=False, dry_run=False, data=None, archive=None, fetch=True, now_text=None, log=print,
             sleep=time.sleep, max_steps=None):
    """The live loop on the real clock (or one step with once). Graceful: Ctrl-C, SIGTERM or autopilot.py stop
    finish the current step, save the state and release the lock."""
    paths = Paths(workdir)
    pilot = Pilot(workdir, Market(workdir, data, archive, fetch, live=not data), dry_run=dry_run, log=log,
                  fetch=fetch)
    now = parse_now(now_text)
    pilot.load_config()                 # refuses a live config before the lock, the state or anything else
    acquire_lock(paths, "dry-run" if dry_run else "paper", now)
    flag = {"stop": False}

    def on_signal(signum, frame):
        flag["stop"] = True
    old = {}
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            old[s] = signal.signal(s, on_signal)
        except (ValueError, OSError):
            pass
    if os.path.exists(paths.stop):
        os.remove(paths.stop)
    steps = 0
    try:
        while True:
            now = parse_now(now_text) if (once or not now_text) else now
            phase = pilot.step(now)
            steps += 1
            st = pilot.state
            st["heartbeat"] = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "phase": phase, "pid": os.getpid()}
            pilot.save()
            if once or (max_steps and steps >= max_steps):
                return phase
            wait = seconds_to_next(now)
            while wait > 0 and not flag["stop"] and not os.path.exists(paths.stop):
                sleep(min(1.0, wait))
                wait -= 1.0
            if flag["stop"] or os.path.exists(paths.stop):
                log("auto-pilot: stopping (state saved; start again to resume)")
                return "stopped"
            if now_text:
                now = now + datetime.timedelta(minutes=5)
    finally:
        if os.path.exists(paths.stop):
            try:
                os.remove(paths.stop)
            except OSError:
                pass
        release_lock(paths)
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass


def seconds_to_next(now):
    """Wake 20 seconds after the next 5-minute bar closes (its bar is then complete)."""
    m = now.hour * 60 + now.minute
    nxt = (m // 5 + 1) * 5
    target = now.replace(hour=0, minute=0, second=20, microsecond=0) + datetime.timedelta(minutes=nxt)
    return max(1.0, (target - now).total_seconds())


def request_stop(workdir):
    paths = Paths(workdir)
    lock = read_json(paths.lock) or {}
    if not lock:
        return False, "no auto-pilot loop is running in this workdir"
    if not pid_alive(lock.get("pid")):
        try:
            os.remove(paths.lock)
        except OSError:
            pass
        return False, "the lock was left by pid %s, which is not running: removed it" % lock.get("pid")
    os.makedirs(paths.ap, exist_ok=True)
    with open(paths.stop, "w", encoding="utf-8") as fh:
        fh.write(datetime.datetime.now(IST).isoformat())
    return True, "asked the loop (pid %s) to stop after its current step" % lock.get("pid")


# ---------------------------------------------------------------- replay: a past day on a simulated clock

def replay(workdir, date, to=None, data=None, archive=None, relab=True, resume=False, crash=None, log=None,
           step_min=5, on_step=None):
    """Replay one or more past sessions from saved bars in a sandbox (trader/autopilot/replays/<date>/): your
    config and the enabled strategies' trader-lab runs are copied in, the lab is re-run as of the close before
    the first day (no look-ahead), a fresh paper account starts from your capital, and the clock steps every
    5 minutes from the pre-open to after the close. Your own account is never touched. Returns the reports.
    on_step(t, paths), when given, runs before each step (the tests use it to flip the kill switch)."""
    src = Paths(workdir)
    cfg = read_json(src.config)
    if not isinstance(cfg, dict):
        raise AutopilotError("no trader/config.json in %s: run size.py init first" % src.workdir)
    paper_guard(cfg)
    ap = settings_of(cfg)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or "") or (to and not re.match(r"^\d{4}-\d{2}-\d{2}$", to)):
        raise AutopilotError("--date (and --to) are YYYY-MM-DD")
    box = os.path.join(src.ap, "replays", date + (("_" + to) if to and to != date else ""))
    paths = Paths(box)
    if archive is None:
        archive = os.environ.get("BT_ARCHIVE") or os.path.join(src.trader, "archive")
    clock = Clock(cfg, broker_api().load_fno_stocks(cfg, os.path.join(src.trader, "fno_stocks.json"), False)[0])
    days = []
    d = date
    while d and d <= (to or date):
        if clock.session(d):
            days.append(d)
        d = clock.next_session(d) if clock.session(d) else (datetime.date.fromisoformat(d) +
                                                            datetime.timedelta(days=1)).isoformat()
    if not days:
        raise AutopilotError("%s is not an NSE session (%s)" % (date, clock.closed_reason(date)))
    if not resume or not os.path.exists(paths.state):
        if os.path.isdir(box):
            shutil.rmtree(box)
        os.makedirs(paths.trader)
        write_json(paths.config, cfg)
        for name in ("fno_stocks.json", "fo_mktlots.csv", "fo_secban.json"):
            if os.path.exists(os.path.join(src.trader, name)):
                shutil.copyfile(os.path.join(src.trader, name), os.path.join(paths.trader, name))
        prepare_lab(src, paths, ap, clock.prev_session(days[0]), data, archive, relab, log)
    market = Market(box, data, archive, fetch=False)
    acquire_lock(paths, "replay", parse_now(days[0] + " 09:00"))
    reports = []
    try:
        pilot = Pilot(box, market, replay=True, log=log, crash=crash, fetch=False)
        for day in days:
            pilot.load_config()
            t = datetime.datetime.strptime(day + " 09:00", "%Y-%m-%d %H:%M")
            end = datetime.datetime.strptime(day + " 15:45", "%Y-%m-%d %H:%M")
            st = pilot.state
            if resume and st.get("day") == day and st.get("clock"):
                t = max(t, datetime.datetime.strptime(st["clock"], "%Y-%m-%d %H:%M") +
                        datetime.timedelta(minutes=step_min))
            elif resume and st.get("day", "") > day:
                continue
            while t <= end:
                if on_step is not None:
                    on_step(t, paths)            # tests: flip the kill switch, change the config, ...
                pilot.step(t)
                pilot.state["clock"] = t.strftime("%Y-%m-%d %H:%M")
                pilot.save()
                t += datetime.timedelta(minutes=step_min)
            reports.append(read_json(paths.day(day, "report.json")))
    finally:
        release_lock(paths)
    return reports


def prepare_lab(src, paths, ap, as_of_day, data, archive, relab, log):
    """Copy each enabled strategy's trader-lab run into the sandbox and (relab) re-run it as of the close before
    the replay, with its forward record cut to trades closed by then: the replay never sees a later verdict."""
    lab = lab_api()
    ct = cotrader_api()
    out = paths.lab
    os.makedirs(out, exist_ok=True)
    now = lab.ist_now(as_of_day + " 16:00")
    for name in sorted(ap["strategies"]):
        d = os.path.join(src.lab, name)
        doc = read_json(os.path.join(d, "spec.json"))
        if not doc:
            continue
        dst = os.path.join(out, name)
        os.makedirs(dst, exist_ok=True)
        fwd = read_json(os.path.join(d, "forward.json")) or {}
        if fwd:
            fwd["trades"] = [t for t in fwd.get("trades") or [] if str(t.get("exit_date") or "") <= as_of_day]
            write_json(os.path.join(dst, "forward.json"), fwd)
        if not relab:
            for f in ("spec.json", "results.json", "status.json"):
                if os.path.exists(os.path.join(d, f)):
                    shutil.copyfile(os.path.join(d, f), os.path.join(dst, f))
            continue
        spec = lab.validate_spec(doc)
        if spec.get("kind") == "options":
            mkt, cx, _ = lab.load_data(spec, None, None, False, now, archive=archive if os.path.isdir(archive or "") else None)
        else:
            s = ct.Sources(paths.workdir, data, archive if os.path.isdir(archive or "") else None, False, out)
            mkt, cx = s.market(spec, now), s.context(now)
        res = lab.run_and_decide(doc, mkt, cx, out, None, now)
        st = lab.save_run(out, doc, res, now)
        if log:
            log("replay: trader-lab re-ran %s as of the %s close: %s" % (name, as_of_day, st["verdict"]))


# ---------------------------------------------------------------- status

def status(workdir, now=None):
    paths = Paths(workdir)
    st = read_json(paths.state) or {}
    lock = read_json(paths.lock) or {}
    running = bool(lock) and pid_alive(lock.get("pid"))
    cfg = read_json(paths.config) or {}
    problems = []
    try:
        paper_guard(cfg)
        ap = settings_of(cfg)
    except AutopilotError as e:
        problems.append(str(e))
        ap = None
    day = st.get("day")
    tl = read_timeline(paths, day) if day else []
    positions = st.get("positions") or []
    records = {}
    if os.path.isdir(paths.records):
        for f in sorted(os.listdir(paths.records)):
            doc = read_json(os.path.join(paths.records, f)) or {}
            ts = doc.get("trades") or []
            nets = [float(t["net"]) for t in ts if t.get("net") is not None]
            rs = [float(t["r"]) for t in ts if t.get("r") is not None]
            records[doc.get("strategy") or f[:-5]] = {
                "trades": len(ts), "net": rp(sum(nets)), "wins": sum(1 for x in nets if x > 0),
                "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None, "last": ts[-5:]}
    lab_rows = {}
    for name in sorted((ap or {}).get("strategies") or {}):
        s = read_json(os.path.join(paths.lab, name, "status.json")) or {}
        lab_rows[name] = {"verdict": s.get("verdict"), "why": s.get("why"), "forward": s.get("forward"),
                          "bucket": (ap or {}).get("strategies", {}).get(name)}
    return {"running": running, "lock": lock or None, "stopping": os.path.exists(paths.stop), "day": day,
            "mode": st.get("mode"), "phases": st.get("phases") or {}, "heartbeat": st.get("heartbeat"),
            "entries_stopped": st.get("entries_stopped"), "problems": problems, "settings": ap,
            "positions": [p for p in positions if p["status"] in ("open", "working", "placing")],
            "timeline": tl[-100:], "records": records, "strategies": lab_rows,
            "reports": sorted(os.listdir(os.path.join(paths.ap, "days")))[-30:]
            if os.path.isdir(os.path.join(paths.ap, "days")) else [],
            "paper_only": True, "disclaimer": DISCLAIMER}


def render_status(s):
    L = ["Auto-pilot (PAPER only): %s" % ("RUNNING, pid %s since %s" % (s["lock"]["pid"], s["lock"]["started"])
                                          if s["running"] else "not running")]
    for p in s["problems"]:
        L.append("  problem: " + p)
    if s["day"]:
        L.append("Day %s (%s): pre-open %s, end of day %s" % (s["day"], s["mode"], s["phases"].get("pre_open") or "–",
                                                            s["phases"].get("eod") or "–"))
    if s["entries_stopped"]:
        L.append("New entries stopped: %s" % s["entries_stopped"])
    if s["positions"]:
        L.append("Positions:")
        for p in s["positions"]:
            L.append("  %s %s %s %s %d, %s" % (p["id"], p["status"], p["symbol"], p["product"], p["qty"], p["strategy"]))
    for name, r in sorted(s["strategies"].items()):
        rec = s["records"].get(name) or {}
        L.append("  %s: lab %s; auto-pilot paper record %d trade(s), %s net" % (
            name, r["verdict"], rec.get("trades", 0), inr(rec.get("net") or 0)))
    for e in s["timeline"][-10:]:
        L.append("  %s %s" % (e["at"][11:16], e["words"]))
    L.append(DISCLAIMER)
    return "\n".join(L)


# ---------------------------------------------------------------- command line

def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="autopilot.py", description="Auto-pilot on PAPER: the co-trader loop, run by "
                                 "itself on the paper account (never a live broker).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--workdir", default=".", help="the folder holding trader/ (default .)")
        p.add_argument("--data", help="offline bar files (SYM-5m.csv.gz, SYM-1d.csv.gz)")
        p.add_argument("--archive", help="a trader-archive folder (default trader/archive or $BT_ARCHIVE)")
    p = sub.add_parser("start", help="run the loop on the NSE clock (paper only)")
    common(p)
    p.add_argument("--once", action="store_true", help="one step now, then exit")
    p.add_argument("--dry-run", action="store_true", help="decide and record, place nothing")
    p.add_argument("--offline", action="store_true", help="no network: saved bars only")
    p.add_argument("--now", help="pin the clock (IST), with --once")
    p = sub.add_parser("stop", help="ask the running loop to stop after its current step")
    p.add_argument("--workdir", default=".")
    p = sub.add_parser("status", help="running or not, today's phase, positions, records")
    p.add_argument("--workdir", default=".")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("report", help="a day's report (markdown, or --json)")
    p.add_argument("--workdir", default=".")
    p.add_argument("--date", help="YYYY-MM-DD (default: the last day run)")
    p.add_argument("--replay", help="read the report of this replay (its sandbox name, e.g. 2026-09-21)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("replay", help="replay past sessions from saved bars on a simulated clock (a sandbox)")
    common(p)
    p.add_argument("--date", required=True, help="the first day, YYYY-MM-DD")
    p.add_argument("--to", help="the last day (default: --date)")
    p.add_argument("--no-relab", action="store_true", help="use the lab runs as they are (they may know later bars)")
    p.add_argument("--resume", action="store_true", help="continue an interrupted replay")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        if a.cmd == "start":
            phase = run_loop(a.workdir, once=a.once, dry_run=a.dry_run, data=a.data, archive=a.archive,
                             fetch=not a.offline, now_text=a.now)
            print("auto-pilot: %s" % phase)
            return 0
        if a.cmd == "stop":
            ok, words = request_stop(a.workdir)
            print(words)
            return 0 if ok else 1
        if a.cmd == "status":
            s = status(a.workdir)
            print(json.dumps(s, indent=1, ensure_ascii=False) if a.json else render_status(s))
            return 0
        if a.cmd == "report":
            base = a.workdir if not a.replay else os.path.join(Paths(a.workdir).ap, "replays", a.replay)
            paths = Paths(base)
            day = a.date
            if not day:
                days = sorted(os.listdir(os.path.join(paths.ap, "days"))) if os.path.isdir(
                    os.path.join(paths.ap, "days")) else []
                if not days:
                    raise AutopilotError("no auto-pilot day yet in %s" % paths.ap)
                day = days[-1]
            rep = read_json(paths.day(day, "report.json")) or build_report(base, day)
            print(json.dumps(rep, indent=1, ensure_ascii=False) if a.json else report_md(rep))
            return 0
        if a.cmd == "replay":
            reps = replay(a.workdir, a.date, a.to, a.data, a.archive, not a.no_relab, a.resume,
                          log=None if a.json else print)
            if a.json:
                print(json.dumps(reps, indent=1, ensure_ascii=False))
            else:
                for r in reps:
                    if r:
                        sys.stdout.write(report_md(r))
            return 0
    except errors() as e:
        print("autopilot: %s" % e, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
