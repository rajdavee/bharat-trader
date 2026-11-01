#!/usr/bin/env python3
"""Live co-pilot for trader-copilot: real-money proposals for PROMOTED strategies, one typed approval per order.
Standard library only.

This is the bridge from paper to real money, and it is built to be slow on purpose. It never runs by itself:
there is no auto-approve setting, no background placing and no batch approval. Every live order needs, at that
moment, every one of these:

  1. the strategy is ELIGIBLE: trader-lab's verdict is PROMOTE, its paper forward record meets your thresholds
     (trades, weeks, drawdown in R), the co-trader's calibration of its odds is good enough (Brier score), and it
     was not demoted recently. WATCH, RESEARCH, REJECT and DEMOTE are paper only, and the table says why;
  2. a co-trader candidate of that strategy for today, sized by trader-budget inside the co-pilot's own live
     capital bucket (never the whole account);
  3. trader-risk PASS for that exact order on your LIVE config, checked again at approval;
  4. the day's caps: live orders a day, live capital at risk, live loss a day (past it the live kill switch
     turns on by itself), and a cool-off after any live loss;
  5. a fresh proposal: not older than expire_min minutes, and the price has not moved more than
     max_price_move_pct from the plan (else: re-plan);
  6. YOUR typed approval: the exact phrase on the ticket, e.g. "yes live BUY 42 RELIANCE", typed in your own
     terminal (copilot.py approve ID asks for it) or in the local UI (typed, after a 10-second countdown). The
     approval is tied to that proposal's fingerprint and used once;
  7. trader-broker's six live gates and today's preflight, checked by trader-broker itself at the moment of
     sending (ticket.py confirm records your phrase as your yes, ticket.py place sends).

Then the stop and the target go as the broker's native linked order where it has one (a bracket with the entry at
Upstox and Dhan; a GTT OCO after the fill at Zerodha Kite: copilot.py exits ID, with its own typed phrase) and a
paper twin of the order is recorded (trader/state/shadow.json) so the journal can compare live with paper.

    python3 copilot.py status                 # eligibility, pending proposals, caps, live kill
    python3 copilot.py eligibility            # every lab strategy: live proposals or paper only, and why
    python3 copilot.py propose [--run]        # today's live proposals from the co-trader (places nothing)
    python3 copilot.py proposals              # the pending live tickets, in full
    python3 copilot.py approve CP-20261029-1  # shows the ticket, asks for the exact phrase, then sends
    python3 copilot.py reject CP-20261029-1
    python3 copilot.py exits CP-20261029-1    # Kite / Angel One: the stop (and target) after the entry fills
    python3 copilot.py caps                   # today's live orders, capital at risk, live loss, cool-off
    python3 copilot.py kill [--reason ...]    # live kill switch on: no new live proposals; flatten tickets offered
    python3 copilot.py kill --lift            # off again, with the typed words "lift live kill"

Files: trader/copilot/state.json (proposals), approvals.jsonl (every typed approval, append only), kill.json,
orders/<ID>.json and <ID>-risk.json (one order per proposal), work/ (the live bucket as trader-risk sees it).
Times are IST; --now "2026-10-29 09:41" pins the clock. Education and tooling, not financial advice.
"""

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_STATE = "bharat-trader/copilot-state/1"
SCHEMA_APPROVAL = "bharat-trader/copilot-approval/1"
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
LIVE_TIERS = ("PROMOTED",)              # the only co-trader tier that can become a live proposal
LIVE_VERDICT = "PROMOTE"                # the only trader-lab verdict that can
APPROVAL_MAX_AGE_S = 120                # a typed approval is used at once, or not at all
UI_COUNTDOWN_S = 10                     # the UI's approval dialog waits this long before it accepts the phrase
UI_ARM_MAX_S = 300                      # and an opened dialog is good for this long
LIFT_WORDS = "lift live kill"
# the only trader-broker commands this tool runs. "place" and "cancel" only from _send(), after a fresh approval.
BROKER_COMMANDS = ("confirm", "place", "cancel")
DEFAULTS = {
    "capital": None,                # rupees: the live capital bucket (required for proposals; no default)
    "strategies": None,             # optional allow-list of trader-lab strategy names
    "min_forward_trades": 30,       # paper forward trades of the promoted spec
    "min_forward_weeks": 4,         # first entry to last exit of that record
    "max_forward_drawdown_r": 6.0,  # deepest fall of the forward record, in R (risk units)
    "max_brier": 0.25,              # the co-trader's Brier score for the strategy (a 50% guess scores 0.25)
    "min_calibration": 20,          # resolved predictions before the Brier score counts
    "demote_lookback_days": 30,     # a DEMOTE (lab or calibration) this recent rules it out
    "max_live_orders_per_day": 2,
    "max_capital_at_risk_pct": 2.0, # % of the live capital: the loss at the stop of every live position today
    "max_live_loss_per_day_pct": 1.0,   # % of the live capital: past it, the live kill switch turns on
    "cooloff_after_loss_min": 60,   # no new live proposal this long after a live loss
    "expire_min": 5,                # a proposal not approved in this many minutes expires
    "max_price_move_pct": 0.5,      # the price moved more than this from the plan: re-plan
    "stop_limit_pct": 1.0,          # an SL stop's limit price this far past its trigger (brokers that need SL)
    "notify": {"bell": True, "desktop": False},
}
NUM_KEYS = {"capital": (1000, 1e10), "min_forward_trades": (1, 10000), "min_forward_weeks": (0, 520),
            "max_forward_drawdown_r": (0.5, 1000), "max_brier": (0.01, 1.0), "min_calibration": (1, 10000),
            "demote_lookback_days": (0, 3650), "max_live_orders_per_day": (1, 50),
            "max_capital_at_risk_pct": (0.05, 50), "max_live_loss_per_day_pct": (0.05, 50),
            "cooloff_after_loss_min": (0, 1440), "expire_min": (1, 60), "max_price_move_pct": (0.05, 10),
            "stop_limit_pct": (0.1, 10)}
DISCLAIMER = ("Education and tooling, not financial advice. A live order is real money: it can lose more than the "
              "ticket's estimate (a stop can fill worse in a fast market or a gap).")

# Every sentence the user reads, in one place (so they can be translated and catalogued).
WORDS = {
    "no_auto": "there is no auto-approve: every live order needs your own typed approval for that one order. "
               "Remove %r from the copilot block",
    "paper_config": "the config is the PAPER broker: the co-pilot makes live proposals only with a live config "
                    "(\"paper\": false, a live \"broker\" and live_enabled, set by you, see docs/brokers.md). On "
                    "paper, use the auto-pilot or trader-broker's own tickets",
    "no_capital": "set \"copilot\": {\"capital\": N} in trader/config.json: the rupees the co-pilot may use live "
                  "(its own bucket; the rest of your account is never used)",
    "no_tty": "nobody is at the keyboard (no terminal): a live approval is typed by you, in your own terminal or "
              "the local UI. Nothing was sent",
    "phrase_prompt": "Type exactly  %s  to send this LIVE order (anything else cancels): ",
    "phrase_wrong": "Not approved: the phrase did not match (you typed %r, the ticket needs %r). Nothing was sent",
    "approved": "Approved %s with your typed phrase \"%s\" (%s).",
    "kill_on": "LIVE KILL SWITCH ON for %s: %s. No new live proposals today; open live positions get flatten "
               "tickets, each still needs your typed approval.",
    "kill_lifted": "Live kill switch lifted. Proposals resume (the caps still apply).",
    "kill_lift_typed": "not lifted: type the words %s to lift the live kill switch" % LIFT_WORDS,
    "expired": "expired: %d minutes old (proposals live %d minutes). Re-plan: copilot.py propose",
    "moved": "the price moved %.2f%% from the plan's %s to %s (more than %s%%): re-plan with copilot.py propose",
    "no_price": "no fresh price for %s (%s): the move from the plan cannot be checked, so it is not sent",
    "waiting": "LIVE proposal %s is waiting for your approval: %s",
    "eligible": "live proposals",
    "paper_only": "paper only",
}

COUNTDOWN_WORDS = "Read the ticket. The approve button unlocks after %d seconds." % UI_COUNTDOWN_S


class CopilotError(Exception):
    pass


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
    name = "bt_copilot_" + script
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), folder, script + ".py")
    if not os.path.isfile(path):
        if required:
            raise CopilotError("trader-copilot needs %s installed next to it (%s is missing)" % (folder, path))
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
    return _sibling("trader-cotrader", "cotrader", required=False)


def lab_api():
    return _sibling("trader-lab", "lab", required=False)


def errors():
    out = [CopilotError]
    for mod, name in ((broker_api(), "TicketError"), (risk_api(), "RiskError"), (budget_api(), "SizeError"),
                      (cotrader_api(), "CotraderError"), (lab_api(), "LabError")):
        if mod is not None and hasattr(mod, name):
            out.append(getattr(mod, name))
    out.append(broker_api().RulesError)
    return tuple(out)


# ---------------------------------------------------------------- files and the clock

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise CopilotError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def parse_now(text=None):
    """IST now, or a pinned time ("2026-10-29 09:41"; no offset means IST)."""
    if not text:
        return datetime.datetime.now(IST).replace(microsecond=0)
    if isinstance(text, datetime.datetime):
        return text.replace(tzinfo=IST) if text.tzinfo is None else text.astimezone(IST)
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise CopilotError("--now wants a time like \"2026-10-29 09:41\" (IST)")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def stamp(now):
    return now.astimezone(IST).replace(microsecond=0).isoformat()


def from_stamp(text):
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


class Paths(object):
    """The files of one workdir. Your trader/orders.json and trader/state/risk.json are never touched: each live
    proposal is its own orders file with its own risk verdicts in trader/copilot/orders/."""

    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        t = self.trader = os.path.join(self.workdir, "trader")
        self.config = os.path.join(t, "config.json")
        self.lab = os.path.join(t, "lab")
        self.cotrader = os.path.join(t, "cotrader.json")
        self.broker_state = os.path.join(t, "state", "broker.json")
        self.preflight = os.path.join(t, "state", "preflight.json")
        self.shadow = os.path.join(t, "state", "shadow.json")
        self.trades = os.path.join(t, "state", "trades.json")
        self.risk_kill = os.path.join(t, "state", "kill.json")
        self.prices = os.path.join(t, "prices.json")
        c = self.cp = os.path.join(t, "copilot")
        self.state = os.path.join(c, "state.json")
        self.approvals = os.path.join(c, "approvals.jsonl")
        self.kill = os.path.join(c, "kill.json")
        self.orders = os.path.join(c, "orders")
        self.work = os.path.join(c, "work")

    def order_file(self, pid):
        return os.path.join(self.orders, "%s.json" % pid)

    def risk_file(self, pid):
        return os.path.join(self.orders, "%s-risk.json" % pid)


# ---------------------------------------------------------------- the config

def settings_of(cfg):
    """The "copilot" block of trader/config.json with its defaults, validated. There is no setting that approves,
    places or runs anything by itself: a key that looks like one is refused, not ignored."""
    raw = (cfg or {}).get("copilot")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CopilotError("\"copilot\" in trader/config.json is an object, e.g. {\"capital\": 50000}")
    for k in raw:
        low = str(k).lower()
        if "auto" in low or "unattended" in low or low in ("approve", "approved", "yes", "skip_approval",
                                                           "no_confirm", "confirm"):
            raise CopilotError(WORDS["no_auto"] % k)
        if k not in DEFAULTS:
            raise CopilotError("\"copilot\" has no setting %r. The settings are: %s" % (k, ", ".join(sorted(DEFAULTS))))
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k in NUM_KEYS:
            if v is None and k == "capital":
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise CopilotError("copilot.%s is a number" % k)
            lo, hi = NUM_KEYS[k]
            if not lo <= float(v) <= hi:
                raise CopilotError("copilot.%s is between %g and %g (it is %g)" % (k, lo, hi, v))
            s[k] = v
        elif k == "strategies":
            if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
                raise CopilotError("copilot.strategies is a list of trader-lab strategy names")
            s[k] = v
        elif k == "notify":
            if not isinstance(v, dict) or set(v) - {"bell", "desktop"} or \
                    not all(isinstance(x, bool) for x in v.values()):
                raise CopilotError("copilot.notify is {\"bell\": true|false, \"desktop\": true|false}")
            s[k] = dict(DEFAULTS["notify"], **v)
    return s


def load_config(paths):
    cfg = read_json(paths.config)
    if not isinstance(cfg, dict):
        raise CopilotError("no trader/config.json: run trader-budget's size.py init first")
    return cfg


def live_broker(cfg):
    """The live broker of a live config, or CopilotError in plain words."""
    broker = broker_api()
    try:
        mode, b = broker.mode_of(cfg or {})
    except broker.TicketError as e:
        raise CopilotError("the config is not a clean live config (%s). Nothing was proposed or sent" % e)
    if mode != "live":
        raise CopilotError(WORDS["paper_config"])
    return b


# ---------------------------------------------------------------- eligibility

def drawdown_r(trades):
    eq = peak = dd = 0.0
    for t in trades:
        eq += float(t.get("r") or 0)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def weeks_of(trades):
    days = sorted([str(t.get("entry_date") or t.get("exit_date") or "")[:10] for t in trades] +
                  [str(t.get("exit_date") or "")[:10] for t in trades])
    days = [d for d in days if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    if not days:
        return 0.0
    a, b = datetime.date.fromisoformat(days[0]), datetime.date.fromisoformat(days[-1])
    return round((b - a).days / 7.0, 1)


def calibration_by_strategy(paths):
    """The co-trader's scores of its logged odds, per strategy, from outcomes already known (no network)."""
    ct = cotrader_api()
    if ct is None:
        return {}, "trader-cotrader is not installed: no calibration"
    try:
        recs = ct.resolve(ct.read_log(paths.workdir), paths.workdir)
        rep = ct.calibration(recs)
    except Exception as e:  # a broken log must not make anything eligible; it says so instead
        return {}, "the co-trader's log could not be scored (%s)" % e
    return rep.get("by_strategy") or {}, None


def recent_demote(status, demoted, name, now, days):
    """(when, why) of a DEMOTE within the lookback, from trader-lab's history or the co-trader's list."""
    cut = now - datetime.timedelta(days=days)
    for h in reversed((status or {}).get("history") or []):
        if h.get("verdict") == "DEMOTE":
            at = from_stamp(str(h.get("at") or "").replace(" ", "T"))
            if at is None or at >= cut:
                return h.get("at"), h.get("why") or ""
    d = (demoted or {}).get(name)
    if d:
        at = from_stamp(str(d.get("at") or "").replace(" ", "T"))
        if at is None or at >= cut:
            return d.get("at"), "co-trader calibration: %s" % (d.get("why") or "")
    return None, None


def eligibility(paths, s, now, cal=None):
    """One row per trader-lab strategy: can it make LIVE proposals, and why or why not (every check listed)."""
    rows = []
    if cal is None:
        cal, cal_note = calibration_by_strategy(paths)
    else:
        cal_note = None
    demoted = (read_json(os.path.join(paths.trader, "cotrader", "demoted.json"), {}) or {}).get("strategies") or {}
    names = sorted(n for n in (os.listdir(paths.lab) if os.path.isdir(paths.lab) else [])
                   if os.path.isfile(os.path.join(paths.lab, n, "status.json")))
    for name in names:
        st = read_json(os.path.join(paths.lab, name, "status.json"), {}) or {}
        fwd = read_json(os.path.join(paths.lab, name, "forward.json"), {}) or {}
        h = st.get("spec_hash")
        trades = [t for t in fwd.get("trades") or [] if not h or t.get("spec_hash") == h]
        trades = [t for t in trades if t.get("reason") != "end of data"]
        v = st.get("verdict") or "no verdict"
        checks = []

        def add(check, ok, words):
            checks.append({"check": check, "ok": bool(ok), "words": words})
        add("verdict", v == LIVE_VERDICT, "trader-lab verdict %s%s" % (v, "" if v == LIVE_VERDICT else
                                                                      ": only PROMOTE can trade live (%s)" % {
                                                                          "WATCH": "its paper forward test is running",
                                                                          "REJECT": "it failed a gate",
                                                                          "DEMOTE": "its forward record decayed"}.get(
                                                                          v, "unproven: RESEARCH")))
        if s.get("strategies") is not None:
            add("allowed", name in s["strategies"], "in your copilot.strategies list" if name in s["strategies"]
                else "not in your copilot.strategies list")
        n, need = len(trades), int(s["min_forward_trades"])
        add("forward_trades", n >= need, "%d paper forward trades (needs %d)" % (n, need))
        wk, needw = weeks_of(trades), float(s["min_forward_weeks"])
        add("forward_weeks", wk >= needw, "the forward record spans %g weeks (needs %g)" % (wk, needw))
        dd, maxdd = drawdown_r(trades), float(s["max_forward_drawdown_r"])
        mean_r = round(sum(float(t.get("r") or 0) for t in trades) / n, 3) if n else None
        add("forward_drawdown", n > 0 and dd <= maxdd, "its deepest fall was %gR (at most %gR)" % (dd, maxdd))
        g = cal.get(name) or {}
        need_c = int(s["min_calibration"])
        if not g.get("n"):
            add("calibration", False, "no resolved co-trader predictions yet (needs %d)%s" % (
                need_c, ("; " + cal_note) if cal_note else ""))
        elif g["n"] < need_c:
            add("calibration", False, "%d resolved predictions (needs %d before the Brier score counts)" % (
                g["n"], need_c))
        else:
            ok = g.get("brier") is not None and g["brier"] <= float(s["max_brier"])
            add("calibration", ok, "Brier %.3f on %d predictions (at most %g; a 50%% guess scores 0.25); predicted "
                                   "%.0f%% on average, %.0f%% made money" % (g["brier"], g["n"], s["max_brier"],
                                                                           100 * g.get("mean_p", 0),
                                                                           100 * g.get("observed", 0)))
        when, why = recent_demote(st, demoted, name, now, int(s["demote_lookback_days"]))
        add("no_recent_demote", when is None, "no DEMOTE in the last %d days" % s["demote_lookback_days"]
            if when is None else "demoted at %s (%s)" % (when, why))
        ok = all(c["ok"] for c in checks)
        rows.append({"strategy": name, "verdict": v, "eligible": ok, "mode": WORDS["eligible"] if ok else
                     WORDS["paper_only"], "reasons": [c["words"] for c in checks if not c["ok"]], "checks": checks,
                     "forward": {"trades": n, "weeks": wk, "drawdown_r": dd, "mean_r": mean_r,
                                 "net": rp(sum(float(t.get("net") or 0) for t in trades))},
                     "calibration": {k: g.get(k) for k in ("n", "brier", "mean_p", "observed", "observed_ci",
                                                          "brier_reference")} if g else {"n": 0}})
    return rows


# ---------------------------------------------------------------- state

def load_state(paths):
    st = read_json(paths.state) or {}
    st.setdefault("schema", SCHEMA_STATE)
    st.setdefault("seq", 0)
    st.setdefault("proposals", [])
    st.setdefault("used_nonces", [])
    st.setdefault("losses", {})
    return st


def save_state(paths, st):
    st["used_nonces"] = st.get("used_nonces", [])[-500:]
    write_json(paths.state, st)


def find(st, pid):
    for p in st["proposals"]:
        if p["id"] == pid:
            return p
    raise CopilotError("there is no co-pilot proposal %s (copilot.py proposals lists them)" % pid)


def new_id(st, day):
    st["seq"] = int(st.get("seq", 0)) + 1
    return "CP-%s-%d" % (day.replace("-", ""), st["seq"])


def proposal_fingerprint(p):
    """What an approval is tied to: this proposal, this exact order, this broker, this moment and plan."""
    core = {"id": p["id"], "kind": p["kind"], "broker": p["broker"], "order_fp": p["order_fp"],
            "created_at": p["created_at"], "plan_entry": p.get("plan_entry"), "action": p.get("action")}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def phrase_for(order, what=None):
    """The words the user types for one live order: yes live BUY 42 RELIANCE (the exits: yes live exits ...)."""
    side = str(order.get("side") or "").upper()
    head = "yes live exits" if what == "exits" else "yes live"
    return "%s %s %d %s" % (head, side if what != "exits" else ("SELL" if side == "BUY" else "BUY"),
                            int(float(order.get("qty") or 0)), str(order.get("symbol") or "").upper())


def day_of(p):
    return str(p.get("created_at") or "")[:10]


def entries_sent(st, day):
    return [p for p in st["proposals"] if p["kind"] == "entry" and day_of(p) == day and p["status"] in
            ("sent", "closed", "unknown")]


def open_live(st):
    """Live entries this tool sent that are not closed (any day: CNC carries over)."""
    return [p for p in st["proposals"] if p["kind"] == "entry" and p["status"] in ("sent", "unknown")]


# ---------------------------------------------------------------- the live kill switch

def kill_doc(paths):
    return read_json(paths.kill) or {}


def kill_active(paths, day):
    k = kill_doc(paths)
    return k if k.get("active") and k.get("date") == day else None


def kill_on(paths, now, reason, auto=False):
    k = kill_doc(paths)
    log = list(k.get("log") or [])
    log.append({"at": stamp(now), "action": "kill", "reason": reason, "auto": bool(auto)})
    k.update(active=True, date=now.date().isoformat(), at=stamp(now), reason=reason, auto=bool(auto), log=log[-50:])
    write_json(paths.kill, k)
    return k


def kill_lift(paths, now, typed):
    k = kill_doc(paths)
    if not (k.get("active") and k.get("date") == now.date().isoformat()):
        raise CopilotError("the live kill switch is not on today: nothing to lift")
    if " ".join(str(typed or "").lower().split()) != LIFT_WORDS:
        raise CopilotError(WORDS["kill_lift_typed"])
    log = list(k.get("log") or [])
    log.append({"at": stamp(now), "action": "lift", "typed": typed})
    k.update(active=False, lifted_at=stamp(now), log=log[-50:])
    write_json(paths.kill, k)
    return k


# ---------------------------------------------------------------- the broker, read only

def adapter(cfg, b):
    try:
        return broker_api().get_adapter(b, cfg)
    except Exception:
        return None


def live_positions(cfg, b):
    """(rows, None) from the broker's positions (read only), or (None, why not)."""
    ad = adapter(cfg, b)
    if ad is None or not ad.token():
        return None, "no %s token in this shell (ticket.py login %s): live P&L unknown" % (b, b)
    try:
        return ad.positions(), None
    except Exception as e:
        return None, "could not read %s positions: %s" % (b, getattr(e, "message", e))


def fresh_price(paths, cfg, b, symbol, now):
    """(price, source) from the broker's own quote (read only), else trader/prices.json under 15 minutes old."""
    ad = adapter(cfg, b)
    if ad is not None and ad.token() and ad.capabilities.get("quotes"):
        try:
            inst = ad.resolve(symbol, "NSE")
            got = ad.ltp([inst]) or {}
            px = got.get(inst["symbol"]) or got.get(symbol)
            if px:
                return float(px), "%s quote" % ad.title
        except Exception:
            pass
    px, why = broker_api().fresh_price(paths.prices, symbol, now)
    return (float(px), "trader/prices.json") if px else (None, why)


def margin_check(cfg, b, order):
    """The broker's own margin for the entry next to the funds it shows (read only), in words."""
    ad = adapter(cfg, b)
    if ad is None or not ad.token():
        return {"checked": False, "words": "not checked: no %s token in this shell" % b}
    entry = broker_api().adapters().base.leg_order(order, "entry")
    try:
        bm = ad.basket_margin([entry]) if ad.capabilities.get("basket_margin") else None
    except Exception as e:
        bm = None
        why = getattr(e, "message", str(e))
    else:
        why = None if bm else "%s has no margin calculator here" % ad.title
    try:
        funds = ad.funds()
    except Exception as e:
        funds = None
        why = (why + "; " if why else "") + "funds: %s" % getattr(e, "message", e)
    need = (bm or {}).get("total")
    have = (funds or {}).get("available")
    out = {"checked": need is not None and have is not None, "required": need, "available": have}
    if need is not None and have is not None:
        out["ok"] = need <= have
        out["words"] = "%s says this order needs %s and %s is available%s" % (
            ad.title, inr(need), inr(have), "" if need <= have else ": NOT ENOUGH")
    else:
        out["words"] = "not checked: %s" % (why or "no figure from %s" % ad.title)
    return out


def refresh(paths, cfg, b, st, now, positions=None):
    """Mark sent entries closed when the broker shows their position flat (read only), and note live losses."""
    if positions is None:
        positions, why = live_positions(cfg, b)
    else:
        why = None
    if positions is None:
        return None, why
    by = {}
    for r in positions:
        by.setdefault(str(r.get("symbol") or "").upper(), []).append(r)
    pnl = 0.0
    seen = set()
    for p in st["proposals"]:
        if p["kind"] != "entry" or p["status"] not in ("sent", "unknown", "closed") or day_of(p) != now.date().isoformat():
            continue
        sym = p["symbol"]
        rows = by.get(sym) or []
        if sym not in seen:
            pnl += sum(float(r.get("pnl") or 0) for r in rows)
            seen.add(sym)
        flat = rows and all(int(float(r.get("qty") or 0)) == 0 for r in rows) and \
            any(float(r.get("buy_qty") or 0) > 0 for r in rows)
        if flat and p["status"] != "closed":
            realised = rp(sum(float(r.get("realised") or r.get("pnl") or 0) for r in rows))
            p["status"] = "closed"
            p["closed_at"] = stamp(now)
            p["realised"] = realised
            p.setdefault("history", []).append({"at": stamp(now), "what": "flat at the broker, realised %s" % inr(
                realised)})
            if realised < 0:
                st["losses"][p["id"]] = {"at": stamp(now), "realised": realised}
    return rp(pnl), None


# ---------------------------------------------------------------- the caps

def caps_view(paths, cfg, s, st, now, positions=None, pnl=None, pnl_why=None):
    day = now.date().isoformat()
    cap = float(s["capital"] or 0)
    sent = entries_sent(st, day)
    at_risk = rp(sum(float(p.get("max_loss") or 0) for p in open_live(st)))
    max_risk = rp(cap * float(s["max_capital_at_risk_pct"]) / 100.0) if cap else None
    max_loss = rp(cap * float(s["max_live_loss_per_day_pct"]) / 100.0) if cap else None
    last_loss = None
    for pid, x in (st.get("losses") or {}).items():
        at = from_stamp(x.get("at"))
        if at and at.date().isoformat() == day and (last_loss is None or at > last_loss):
            last_loss = at
    cool = last_loss + datetime.timedelta(minutes=int(s["cooloff_after_loss_min"])) if last_loss else None
    k = kill_active(paths, day)
    rk = risk_api().kill_active(read_json(paths.risk_kill), day)
    return {"date": day, "capital": cap or None,
            "orders": {"used": len(sent), "max": int(s["max_live_orders_per_day"])},
            "at_risk": {"used": at_risk, "max": max_risk},
            "loss": {"today": pnl, "max": max_loss, "known": pnl is not None, "why": pnl_why},
            "cooloff_until": stamp(cool) if cool and cool > now else None,
            "kill": k, "risk_kill": rk}


def cap_reasons(view, extra_risk=0.0, now=None):
    """Why a NEW live entry is not allowed now (empty: allowed)."""
    out = []
    if view["kill"]:
        out.append("the live kill switch is on (%s)" % view["kill"].get("reason"))
    if view["risk_kill"]:
        out.append("trader-risk's kill switch is on today (%s)" % view["risk_kill"].get("reason"))
    o = view["orders"]
    if o["used"] >= o["max"]:
        out.append("%d of %d live orders today already: the daily cap" % (o["used"], o["max"]))
    a = view["at_risk"]
    if a["max"] is not None and a["used"] + extra_risk > a["max"] + 0.005:
        out.append("live capital at risk would be %s, over the cap %s (%s at risk already)" % (
            inr(a["used"] + extra_risk), inr(a["max"]), inr(a["used"])))
    if view["cooloff_until"]:
        out.append("cooling off after a live loss until %s IST" % view["cooloff_until"][11:16])
    lo = view["loss"]
    if lo["max"] is not None and lo["today"] is not None and -lo["today"] >= lo["max"] - 0.005:
        out.append("today's live loss %s reached the cap %s" % (inr(-lo["today"]), inr(lo["max"])))
    return out


def auto_kill(paths, cfg, s, st, view, now, log=None):
    """Past the day's live loss cap: the live kill switch turns on by itself and every open live position gets a
    flatten ticket (each still needs the user's typed approval). Returns the new flatten proposals."""
    lo = view["loss"]
    if lo["max"] is None or lo["today"] is None or -lo["today"] < lo["max"] - 0.005 or view["kill"]:
        return []
    reason = "today's live loss %s reached the cap %s (%s%% of %s)" % (
        inr(-lo["today"]), inr(lo["max"]), s["max_live_loss_per_day_pct"], inr(view["capital"]))
    view["kill"] = kill_on(paths, now, reason, auto=True)
    made = flatten_proposals(paths, cfg, s, st, now, "the live kill switch turned on: " + reason)
    if log:
        log(WORDS["kill_on"] % (now.date().isoformat(), reason))
    return made


# ---------------------------------------------------------------- trader-risk on one live order

def live_portfolio(paths, s, st, cfg):
    """The co-pilot's live bucket as trader-risk sees it: its capital less what its open live positions use, and
    those positions (so a flatten ticket is an exit)."""
    used, pos = 0.0, {}
    for p in open_live(st):
        o = p["order"]
        q = int(o.get("qty") or 0)
        px = float(o.get("price") or p.get("plan_entry") or 0)
        used += q * px
        cur = pos.setdefault(p["symbol"], {"qty": 0, "avg_price": px, "product": o.get("product") or "MIS"})
        cur["qty"] += q
    cash = rp(max(float(s["capital"] or 0) - used, 0.0))
    doc = {"schema": "bharat-trader/portfolio/2", "cash": cash, "positions": pos, "fno_positions": [],
           "note": "trader-copilot: the live capital bucket as trader-risk sees it (not your broker account)"}
    path = os.path.join(paths.work, "portfolio.json")
    write_json(path, doc)
    return path, cash


def run_risk(paths, pid, now, portfolio):
    risk = risk_api()
    argv = ["--orders", paths.order_file(pid), "--config", paths.config, "--portfolio", portfolio,
            "--prices", paths.prices, "--state", paths.broker_state, "--trades", paths.trades,
            "--kill", paths.risk_kill, "--now", now.strftime("%Y-%m-%d %H:%M:%S"), "--offline",
            "--out", paths.risk_file(pid), "--json"]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            risk.main(argv)
        except SystemExit:
            pass
    try:
        res = json.loads(out.getvalue())
    except ValueError:
        lines = (err.getvalue() or out.getvalue()).strip().splitlines()
        return {"verdict": "BLOCK", "reasons": [lines[-1] if lines else "trader-risk did not answer"]}
    r0 = (res.get("results") or [{}])[0]
    return {"verdict": r0.get("verdict") or "BLOCK", "reasons": r0.get("reasons") or [],
            "fingerprint": r0.get("fingerprint"), "kind": r0.get("kind"), "warnings": r0.get("warnings") or []}


# ---------------------------------------------------------------- making proposals

def linked_style(cfg, b):
    """(how the exits go, the leg name for ticket.py place of the entry, the exits leg after the fill)."""
    ad = adapter(cfg, b)
    leg = ad.linked_leg() if ad is not None else None
    title = getattr(ad, "title", b)
    if leg == "bracket":
        return ("the stop and the target go WITH the entry as one %s order (the broker cancels the other when one "
                "fills)" % title), "bracket", None
    if leg == "oco":
        return ("after the entry fills, the stop and the target go as ONE %s GTT OCO: copilot.py exits ID (your "
                "typed phrase again)" % title), "entry", "oco"
    return ("%s has no linked stop and target here: after the entry fills, the stop goes leg by leg: copilot.py "
            "exits ID (your typed phrase again); watch the target yourself" % title), "entry", "stop"


def live_stop(order, cfg, b, s):
    """The stop as this broker can take it live: SL-M where the broker adds market protection and the exits are
    not a GTT; otherwise SL with a limit price stop_limit_pct past the trigger (a stop-limit can miss in a gap)."""
    ad = adapter(cfg, b)
    protected = bool(ad is not None and ad.capabilities.get("market_protection"))
    gtt = ad is not None and ad.linked_leg() == "oco"
    sl = dict(order.get("stop_loss") or {})
    if not sl or (protected and not gtt):
        return order
    budget = budget_api()
    trig = float(sl["trigger_price"])
    tick = budget.tick_for(trig, budget.merged_config(cfg).get("tick_size", "auto"))
    lim = budget.tick_round(trig * (1 - float(s["stop_limit_pct"]) / 100.0), tick, "down")
    order = dict(order)
    order["stop_loss"] = {"order_type": "SL", "trigger_price": trig, "price": lim}
    return order


def ticket_words(p, cfg):
    """The plain-language live ticket: what, size, max loss with charges, exits, evidence, margin."""
    broker = broker_api()
    o = p["order"]
    L = ["LIVE ORDER at %s (real money): %s" % (broker.BROKER_TITLES.get(p["broker"], p["broker"]), p["id"])]
    L += ["  " + x for x in broker.plain_parts(o, cfg)]
    if p["kind"] == "entry":
        sl = o.get("stop_loss") or {}
        if sl.get("order_type") == "SL":
            L.append("  the stop is SL: trigger %.2f, limit %.2f (a stop-limit can miss in a gap or a fast market)" % (
                float(sl["trigger_price"]), float(sl["price"])))
        L.append("  exits: %s" % p["exits_words"])
        L.append("  size: %d shares, about %s of the co-pilot's %s live bucket" % (
            int(o["qty"]), inr(p["value"]), inr(p["capital"])))
        L.append("  max loss at the stop: %s including charges" % inr(p["max_loss"]))
        ev = p.get("evidence") or {}
        if ev.get("p") is not None:
            L.append("  evidence: %s, %s; %.0f%% of %s similar past trades made money after charges (95%% %.0f%% to "
                     "%.0f%%), %s, expected %+.2fR" % (
                         p["tier"], p["strategy"], 100 * ev["p"], ev.get("trades_cell"), 100 * (ev.get("p_lo") or 0),
                         100 * (ev.get("p_hi") or 0), ev.get("cell_words") or ev.get("cell") or "this regime",
                         float(ev.get("ev_r") or 0)))
        cal = p.get("calibration") or {}
        if cal.get("n"):
            L.append("  calibration: Brier %.3f on %d resolved predictions; forward record %d trades over %g weeks, "
                     "deepest fall %gR" % (cal["brier"], cal["n"], p["forward"]["trades"], p["forward"]["weeks"],
                                           p["forward"]["drawdown_r"]))
    else:
        L.append("  why: %s" % p.get("why"))
    L.append("  broker margin: %s" % (p.get("margin") or {}).get("words", "not checked"))
    L.append("  trader-risk: %s on your live config at %s" % (p["risk"]["verdict"], p["created_at"][11:16]))
    L.append("  expires %s IST, or if the price moves more than %s%% from %.2f" % (
        p["expires_at"][11:16], p["max_move_pct"], float(p.get("plan_entry") or 0)))
    L.append("  to send it, type exactly: %s" % p["phrase"])
    return L


def write_order(paths, pid, order, source):
    write_json(paths.order_file(pid), {"created_by": "trader-copilot", "status": "proposed", "paper": False,
                                       "live": True, "source": source, "orders": [order],
                                       "note": "ONE live proposal. Nothing is sent until you type its phrase "
                                               "(copilot.py approve %s)." % pid})


def _new_proposal(paths, st, s, cfg, b, now, kind, order, extra):
    broker = broker_api()
    day = now.date().isoformat()
    pid = new_id(st, day)
    write_order(paths, pid, order, extra.pop("source", "trader-copilot"))
    port, cash = live_portfolio(paths, s, st, cfg)
    risk = run_risk(paths, pid, now, port)
    gross, ch, total = broker.loss_at_stop(order, cfg)
    p = {"id": pid, "kind": kind, "status": "pending", "created_at": stamp(now),
         "expires_at": stamp(now + datetime.timedelta(minutes=int(s["expire_min"]))), "broker": b,
         "symbol": str(order["symbol"]).upper(), "order": broker.neutral(order), "order_fp": broker.fingerprint(order),
         "order_file": paths.order_file(pid), "risk_file": paths.risk_file(pid), "risk": risk,
         "max_loss": total, "value": rp(float(order.get("price") or extra.get("plan_entry") or 0) *
                                        int(order.get("qty") or 0)),
         "capital": s["capital"], "max_move_pct": s["max_price_move_pct"], "history": []}
    p.update(extra)
    p["phrase"] = phrase_for(order)
    if kind == "entry" and p.get("exits_leg"):
        p["exit_phrase"] = phrase_for(order, "exits")
    p["margin"] = margin_check(cfg, b, order)
    p["fingerprint"] = proposal_fingerprint(p)
    p["ticket"] = ticket_words(p, cfg)
    if risk["verdict"] != "PASS":
        p["status"] = "blocked"
        p["history"].append({"at": stamp(now), "what": "trader-risk BLOCK: " + "; ".join(risk["reasons"])})
    st["proposals"].append(p)
    return p


def candidates(paths, now, run=False, data=None):
    """Today's co-trader candidates: its last run (trader/cotrader.json) or a fresh one (--run)."""
    ct = cotrader_api()
    if run:
        if ct is None:
            raise CopilotError("trader-cotrader is not installed next to trader-copilot")
        return ct.run_today(paths.workdir, "validated", None, now, data, None, data is None, True)
    doc = read_json(paths.cotrader)
    if not doc:
        raise CopilotError("no co-trader run yet: run cotrader.py today (or copilot.py propose --run)")
    if str(doc.get("as_of") or "")[:10] != now.date().isoformat():
        raise CopilotError("the co-trader's last run is from %s, not today: run cotrader.py today (or copilot.py "
                           "propose --run)" % str(doc.get("as_of"))[:10])
    return doc


def propose(workdir, now=None, run=False, data=None, log=None, positions=None):
    """Live proposals from today's co-trader candidates of ELIGIBLE strategies. Places nothing."""
    paths = Paths(workdir)
    now = parse_now(now)
    day = now.date().isoformat()
    cfg = load_config(paths)
    s = settings_of(cfg)
    b = live_broker(cfg)
    if not s["capital"]:
        raise CopilotError(WORDS["no_capital"])
    st = load_state(paths)
    expire_old(st, now)
    pnl, why = refresh(paths, cfg, b, st, now, positions)
    view = caps_view(paths, cfg, s, st, now, pnl=pnl, pnl_why=why)
    out = {"made": [], "skipped": [], "flatten": auto_kill(paths, cfg, s, st, view, now, log), "caps": view}
    blockers = cap_reasons(view)
    if blockers:
        save_state(paths, st)
        out["blocked"] = blockers
        return out
    elig = {r["strategy"]: r for r in eligibility(paths, s, now)}
    out["eligibility"] = list(elig.values())
    doc = candidates(paths, now, run, data)
    budget = budget_api()
    have = set(p.get("candidate") for p in st["proposals"] if day_of(p) == day)
    held = set(p["symbol"] for p in open_live(st)) | set(p["symbol"] for p in st["proposals"]
                                                          if p["status"] == "pending")
    words, entry_leg, exits_leg = linked_style(cfg, b)
    waiting = [p for p in st["proposals"] if p["status"] == "pending" and p["kind"] == "entry"]
    pending_risk = sum(float(p.get("max_loss") or 0) for p in waiting)
    pending_n = len(waiting)
    for c in doc.get("candidates") or []:
        cid, name, tier = c.get("id"), c.get("strategy"), c.get("tier")

        def skip(why):
            out["skipped"].append({"candidate": cid, "strategy": name, "tier": tier, "why": why})
        if tier not in LIVE_TIERS:
            skip("%s is paper only (%s): never a live order" % (tier, "unproven" if tier == "RESEARCH" else
                                                                 "its paper forward test is running"))
            continue
        row = elig.get(name)
        if not row or not row["eligible"]:
            skip("%s is not eligible for live: %s" % (name, "; ".join((row or {}).get("reasons") or
                                                                       ["no trader-lab run"])))
            continue
        if c.get("kind") == "options":
            skip("F&O baskets are paper only in this version")
            continue
        if cid in have:
            skip("already proposed today")
            continue
        if c.get("symbol") in held:
            skip("a live co-pilot position or proposal in %s is already open" % c.get("symbol"))
            continue
        if c.get("date") != day:
            skip("the plan is for %s, not today" % c.get("date"))
            continue
        _, cash = live_portfolio(paths, s, st, cfg)
        bcfg = dict(cfg, capital=float(s["capital"]))
        if "max_loss_per_trade" not in cfg:
            bcfg["max_loss_per_trade"] = rp(float(s["capital"]) * float(budget.merged_config(cfg).get(
                "risk_per_trade_pct") or 1) / 100.0)
        try:
            sz = budget.size_trade(dict(c["plan"]), bcfg, cash=cash, today=day)
        except budget.SizeError as e:
            skip("trader-budget: %s" % e)
            continue
        if sz["refusal"]:
            skip("trader-budget: %s" % sz["refusal"])
            continue
        loss = float(sz["max_loss_with_charges"])
        more = cap_reasons(view, loss + pending_risk)
        if view["orders"]["used"] + pending_n >= view["orders"]["max"]:
            more.append("%d live order(s) today and %d waiting: the daily cap is %d" % (
                view["orders"]["used"], pending_n, view["orders"]["max"]))
        if more:
            skip("; ".join(more))
            continue
        order = live_stop(budget.leg_order(sz), cfg, b, s)
        order["why"] = "LIVE CO-PILOT %s (%s): %s" % (name, tier, order["why"])
        order["plan"]["copilot"] = {"strategy": name, "candidate": cid, "tier": tier}
        p = _new_proposal(paths, st, s, cfg, b, now, "entry", order, {
            "strategy": name, "candidate": cid, "tier": tier, "plan_entry": float(sz["entry"]),
            "evidence": c.get("evidence") or {}, "calibration": row["calibration"], "forward": row["forward"],
            "exits_words": words, "entry_leg": entry_leg, "exits_leg": exits_leg,
            "source": "trader-copilot %s (co-trader %s)" % (name, cid)})
        held.add(p["symbol"])
        out["made"].append(p)
        if p["status"] == "pending":
            pending_risk += loss              # waiting proposals count against the caps until they expire
            pending_n += 1
            notify(s, WORDS["waiting"] % (p["id"], p["phrase"]))
    save_state(paths, st)
    return out


def flatten_proposals(paths, cfg, s, st, now, why):
    """A SELL ticket for each open live co-pilot position: a marketable LIMIT 1% under the last price (or MARKET
    when no price is known). Each one still needs trader-risk PASS and the user's typed phrase."""
    b = live_broker(cfg)
    budget = budget_api()
    made = []
    pending = set(p.get("closes") for p in st["proposals"] if p["kind"] == "flatten" and p["status"] == "pending")
    for p0 in list(open_live(st)):
        if p0["id"] in pending:
            continue
        o0 = p0["order"]
        px, src = fresh_price(paths, cfg, b, p0["symbol"], now)
        order = {"symbol": p0["symbol"], "exchange": o0.get("exchange") or "NSE", "product": o0.get("product"),
                 "side": "SELL", "qty": int(o0["qty"]), "validity": "DAY"}
        if px:
            tick = budget.tick_for(px, budget.merged_config(cfg).get("tick_size", "auto"))
            order.update(order_type="LIMIT", price=budget.tick_round(px * 0.99, tick, "down"))
        else:
            order.update(order_type="MARKET", price=None)
        order["why"] = "LIVE CO-PILOT flatten %s: %s" % (p0["id"], why)
        made.append(_new_proposal(paths, st, s, cfg, b, now, "flatten", order, {
            "strategy": p0.get("strategy"), "closes": p0["id"], "why": why, "plan_entry": px,
            "source": "trader-copilot flatten of %s" % p0["id"]}))
    return made


def expire_old(st, now):
    for p in st["proposals"]:
        if p["status"] == "pending" and from_stamp(p["expires_at"]) and from_stamp(p["expires_at"]) < now:
            p["status"] = "expired"
            p["history"].append({"at": stamp(now), "what": "expired unapproved"})


# ---------------------------------------------------------------- approval

ASK_PHRASE = None        # tests replace how the phrase is asked for (default: input() on a real terminal)


def ask_terminal(prompt):
    if ASK_PHRASE is not None:
        return ASK_PHRASE(prompt)
    if not (sys.stdin and sys.stdin.isatty()):
        raise CopilotError(WORDS["no_tty"])
    try:
        return input(prompt)
    except EOFError:
        raise CopilotError(WORDS["no_tty"])


def still_valid(paths, cfg, s, st, p, now, positions=None, action="place"):
    """Every reason this proposal may not be sent NOW (empty: it may, if the phrase matches)."""
    out = []
    try:
        b = live_broker(cfg)
    except CopilotError as e:
        return [str(e)]
    if b != p["broker"]:
        out.append("the config broker is now %s, the proposal is for %s" % (b, p["broker"]))
    if proposal_fingerprint(p) != p["fingerprint"]:
        out.append("the proposal changed since it was made (fingerprint): re-plan")
    doc = read_json(p["order_file"]) or {}
    orders = doc.get("orders") or []
    if len(orders) != 1 or broker_api().fingerprint(orders[0]) != p["order_fp"]:
        out.append("the order file changed since the proposal was made: re-plan")
    if action == "exits":
        if p["status"] != "sent" or not p.get("exits_leg"):
            out.append("there are no exits to send for %s (status %s)" % (p["id"], p["status"]))
        if p.get("exits_sent"):
            out.append("the exits of %s were already sent" % p["id"])
        return out
    if p["status"] != "pending":
        return out + ["%s is %s, not pending" % (p["id"], p["status"])]
    age = (now - from_stamp(p["created_at"])).total_seconds() / 60.0
    if now > from_stamp(p["expires_at"]):
        out.append(WORDS["expired"] % (int(age), int(s["expire_min"])))
    if p["kind"] == "entry":
        pnl, why = refresh(paths, cfg, b, st, now, positions)
        view = caps_view(paths, cfg, s, st, now, pnl=pnl, pnl_why=why)
        out += cap_reasons(view, float(p.get("max_loss") or 0))
        elig = {r["strategy"]: r for r in eligibility(paths, s, now)}
        row = elig.get(p["strategy"])
        if not row or not row["eligible"]:
            out.append("%s is no longer eligible: %s" % (p["strategy"], "; ".join((row or {}).get("reasons") or
                                                                                  ["no trader-lab run"])))
    if p.get("plan_entry"):
        px, src = fresh_price(paths, cfg, b, p["symbol"], now)
        if px is None:
            out.append(WORDS["no_price"] % (p["symbol"], src))
        else:
            move = abs(px - float(p["plan_entry"])) / float(p["plan_entry"]) * 100.0
            p["last_price"] = {"price": px, "source": src, "move_pct": round(move, 3), "at": stamp(now)}
            if move > float(s["max_price_move_pct"]) + 1e-9:
                out.append(WORDS["moved"] % (move, "%.2f" % float(p["plan_entry"]), "%.2f" % px,
                                             s["max_price_move_pct"]))
    port, _ = live_portfolio(paths, s, st, cfg)
    risk = run_risk(paths, p["id"], now, port)
    p["risk_at_approval"] = risk
    if risk["verdict"] != "PASS":
        out.append("trader-risk BLOCK on your live config now: %s" % "; ".join(risk["reasons"]))
    elif risk.get("fingerprint") != p["order_fp"]:
        out.append("trader-risk checked a different order (fingerprint): re-plan")
    return out


def record_approval(paths, st, p, phrase, channel, now, action):
    a = {"schema": SCHEMA_APPROVAL, "at": stamp(now), "proposal": p["id"], "fingerprint": p["fingerprint"],
         "order_fp": p["order_fp"], "phrase": phrase, "channel": channel, "actor": "user", "action": action,
         "nonce": secrets.token_hex(8), "broker": p["broker"], "symbol": p["symbol"]}
    append_jsonl(paths.approvals, a)
    p.setdefault("approvals", []).append({k: a[k] for k in ("at", "channel", "action", "nonce")})
    return a


def approve(workdir, pid, typed=None, channel="terminal", now=None, positions=None, log=print, action="place"):
    """The one way a live order goes out: re-check everything, then the user's exact phrase, then trader-broker.
    channel "terminal" asks for the phrase; channel "ui" passes what the user typed in the local UI's dialog."""
    if channel not in ("terminal", "ui"):
        raise CopilotError("approvals come from your terminal or the local UI only")
    paths = Paths(workdir)
    now = parse_now(now)
    cfg = load_config(paths)
    s = settings_of(cfg)
    st = load_state(paths)
    p = find(st, pid)
    expire_old(st, now)
    reasons = still_valid(paths, cfg, s, st, p, now, positions, action)
    if reasons:
        if p["status"] == "pending":
            p["status"] = "expired" if any("expired" in r for r in reasons) else \
                ("invalid" if any("moved" in r or "changed" in r or "eligible" in r for r in reasons) else "pending")
        p.setdefault("history", []).append({"at": stamp(now), "what": "not sent: " + "; ".join(reasons)})
        save_state(paths, st)
        raise CopilotError("%s was NOT sent: %s" % (pid, "; ".join(reasons)))
    want = p["exit_phrase"] if action == "exits" else p["phrase"]
    if channel == "terminal":
        for line in p["ticket"]:
            log(line)
        typed = ask_terminal(WORDS["phrase_prompt"] % want)
    if not isinstance(typed, str) or typed.strip() != want:
        p.setdefault("history", []).append({"at": stamp(now), "what": "phrase did not match: not sent"})
        save_state(paths, st)
        raise CopilotError(WORDS["phrase_wrong"] % (typed, want))
    a = record_approval(paths, st, p, typed.strip(), channel, now, action)
    if action == "place":
        p["status"] = "approved"
    save_state(paths, st)
    log(WORDS["approved"] % (pid, a["phrase"], channel))
    return _send(paths, cfg, st, p, a, now, action, log)


def _check_approval(st, p, a, now, action):
    """The approval must be fresh, typed by the user, for THIS proposal's fingerprint and phrase, and unused."""
    bad = []
    if not isinstance(a, dict):
        return ["no approval"]
    if a.get("actor") != "user" or a.get("channel") not in ("terminal", "ui"):
        bad.append("the approval was not typed by you (%s)" % a.get("actor"))
    if a.get("proposal") != p["id"] or a.get("fingerprint") != p["fingerprint"] or \
            a.get("fingerprint") != proposal_fingerprint(p) or a.get("order_fp") != p["order_fp"]:
        bad.append("the approval is for another proposal or an older version of it (fingerprint)")
    if a.get("action") != action:
        bad.append("the approval was for %s, not %s" % (a.get("action"), action))
    want = p.get("exit_phrase") if action == "exits" else p.get("phrase")
    if a.get("phrase") != want:
        bad.append("the approval's phrase is not this ticket's")
    at = from_stamp(a.get("at"))
    if at is None or not -5 <= (now - at).total_seconds() <= APPROVAL_MAX_AGE_S:
        bad.append("the approval is not fresh (it is used at once, within %d seconds)" % APPROVAL_MAX_AGE_S)
    if not a.get("nonce") or a["nonce"] in st.get("used_nonces", []):
        bad.append("this approval was used already: one approval, one send")
    return bad


def run_broker(argv):
    if not argv or argv[0] not in BROKER_COMMANDS:
        raise CopilotError("the co-pilot runs only these trader-broker commands: %s" % ", ".join(BROKER_COMMANDS))
    broker = broker_api()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = broker.main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _paths_args(paths, order_file, now, risk_file=None):
    return ["--orders", order_file, "--config", paths.config, "--risk", risk_file or order_file[:-5] + "-risk.json",
            "--state", paths.broker_state, "--now", now.strftime("%Y-%m-%d %H:%M:%S")]


def _send(paths, cfg, st, p, a, now, action, log):
    """The ONLY function here that can send anything to a live broker, and only with a fresh typed approval for
    this proposal's fingerprint (checked again here, whoever calls it). trader-broker then checks its own six
    gates and today's preflight, and refuses a yes that was not typed by the user."""
    bad = _check_approval(st, p, a, now, action)
    if bad:
        raise CopilotError("NOT sent: %s" % "; ".join(bad))
    st.setdefault("used_nonces", []).append(a["nonce"])
    save_state(paths, st)
    broker = broker_api()
    live = ["--preflight", paths.preflight, "--prices", paths.prices, "--shadow", paths.shadow]
    result = {"proposal": p["id"], "action": action, "steps": []}

    def step(argv):
        rc, out, err = run_broker(argv)
        result["steps"].append({"cmd": argv[0], "rc": rc, "out": out.strip()[-600:], "err": err.strip()[-600:]})
        return rc, out, err
    if action == "exits":
        rc, out, err = step(["place"] + _paths_args(paths, p["order_file"], now) + live +
                            ["--index", "1", "--leg", p["exits_leg"]])
        if rc != 0:
            p["history"].append({"at": stamp(now), "what": "exits refused: %s" % (err or out).strip()[-300:]})
            save_state(paths, st)
            raise CopilotError("the exits of %s were NOT sent: %s" % (p["id"], (err or out).strip()[-400:]))
        p["exits_sent"] = stamp(now)
        p["history"].append({"at": stamp(now), "what": "exits sent (%s)" % p["exits_leg"]})
        save_state(paths, st)
        log(out.strip())
        return result
    if p["kind"] == "flatten" and p.get("closes"):
        p0 = next((x for x in st["proposals"] if x["id"] == p["closes"]), None)
        leg0 = (p0 or {}).get("entry_leg") if (p0 or {}).get("entry_leg") == "bracket" else \
            ((p0 or {}).get("exits_leg") if (p0 or {}).get("exits_sent") else None)
        if p0 and leg0:
            rc, out, err = step(["cancel"] + _paths_args(paths, p0["order_file"], now) + live +
                                ["--index", "1", "--leg", leg0])
            p["history"].append({"at": stamp(now), "what": "cancel of %s's %s: %s" % (
                p0["id"], leg0, "sent" if rc == 0 else (err or out).strip()[-200:])})
    rc, out, err = step(["confirm"] + _paths_args(paths, p["order_file"], now) + ["--index", "1", "--yes-text",
                                                                                   a["phrase"]])
    if rc != 0:
        p["status"] = "failed"
        p["history"].append({"at": stamp(now), "what": "trader-broker did not record the yes: %s" % (
            err or out).strip()[-300:]})
        save_state(paths, st)
        raise CopilotError("NOT sent: trader-broker refused the yes: %s" % (err or out).strip()[-400:])
    leg = p.get("entry_leg") or "entry"
    rc, out, err = step(["place"] + _paths_args(paths, p["order_file"], now) + live + ["--index", "1", "--leg", leg])
    bstate = broker.load_state(paths.broker_state)
    sent = [r for r in broker.records(bstate, p["order_fp"], now.date().isoformat()) if r.get("live") and
            r.get("broker") == p["broker"]]
    rec = sent[-1] if sent else None
    if rc != 0:
        p["status"] = "unknown" if rec and rec.get("status") == "unknown" else "failed"
        p["history"].append({"at": stamp(now), "what": "trader-broker did not send it: %s" % (
            err or out).strip()[-300:]})
        save_state(paths, st)
        raise CopilotError("NOT sent (or unknown, see ticket.py reconcile): %s" % (err or out).strip()[-500:])
    p["status"] = "sent" if p["kind"] == "entry" else "closed"
    p["sent_at"] = stamp(now)
    p["broker_order_id"] = (rec or {}).get("broker_order_id")
    p["tag"] = (rec or {}).get("tag")
    p["history"].append({"at": stamp(now), "what": "sent to %s as %s (%s)" % (p["broker"], p["broker_order_id"], leg)})
    if p["kind"] == "flatten" and p.get("closes"):
        for x in st["proposals"]:
            if x["id"] == p["closes"] and x["status"] in ("sent", "unknown"):
                x["status"] = "closed"
                x["closed_at"] = stamp(now)
                x["history"].append({"at": stamp(now), "what": "flattened by %s" % p["id"]})
    p["shadow"] = shadow_twin(paths, cfg, p, rec, now)
    save_state(paths, st)
    log(out.strip())
    if p.get("exits_leg") and p["kind"] == "entry":
        log("When the entry fills, send its exits: copilot.py exits %s (type: %s)" % (p["id"], p["exit_phrase"]))
    result["broker_order_id"] = p["broker_order_id"]
    result["shadow"] = p["shadow"]
    return result


def shadow_twin(paths, cfg, p, rec, now):
    """The paper twin of a live order, for the journal's live-vs-paper comparison. trader-broker records it itself
    with "shadow": true; otherwise the co-pilot records it the same way (the live order is not touched)."""
    broker = broker_api()
    if rec is None:
        return {"recorded": False, "why": "no broker record to twin"}
    entry = broker.adapters().base.leg_order(p["order"], "entry")
    if broker.merged_config(cfg).get("shadow") is True:
        doc = read_json(paths.shadow) or {}
        got = [x for x in doc.get("orders") or [] if x.get("fingerprint") == p["order_fp"]]
        if got:
            return dict(got[-1].get("paper") or {}, recorded=True, by="trader-broker")
    ns = argparse.Namespace(prices=paths.prices, options=None, shadow=paths.shadow)
    try:
        twin = broker.record_shadow(ns, cfg, entry, rec, now)
    except Exception as e:  # the live order is out already: the twin never changes that
        return {"recorded": False, "why": str(e)}
    return dict(twin, recorded=True, by="trader-copilot")


def reject(workdir, pid, now=None):
    paths = Paths(workdir)
    now = parse_now(now)
    st = load_state(paths)
    p = find(st, pid)
    if p["status"] not in ("pending", "blocked"):
        raise CopilotError("%s is %s: nothing to reject" % (pid, p["status"]))
    p["status"] = "rejected"
    p["history"].append({"at": stamp(now), "what": "rejected by you"})
    save_state(paths, st)
    return p


# ---------------------------------------------------------------- notifications (optional, local only)

NOTIFY_RUN = None        # tests replace the desktop call
PLATFORM = sys.platform  # and the platform it picks the notifier by


def notify(s, words):
    """A terminal bell and/or a desktop notification on this machine. Nothing leaves it."""
    n = s.get("notify") or {}
    if n.get("bell"):
        sys.stderr.write("\a")
        sys.stderr.flush()
    if not n.get("desktop"):
        return None
    text = re.sub(r"[^\w .,:%₹()/-]", " ", str(words))[:200]
    if PLATFORM == "darwin":
        argv = ["osascript", "-e", "display notification \"%s\" with title \"bharat-trader co-pilot\"" % text]
    elif PLATFORM.startswith("linux"):
        argv = ["notify-send", "bharat-trader co-pilot", text]
    else:
        return None
    run = NOTIFY_RUN or (lambda a: subprocess.run(a, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL, timeout=5, check=False))
    try:
        run(argv)
    except Exception:
        return None
    return argv


# ---------------------------------------------------------------- status

def status(workdir, now=None, positions=None, act=True):
    """Everything the user (or the UI, or a read-only assistant tool) needs to see. Places nothing. With act
    (the command line and the UI's own page), it also expires old proposals and, past the day's live loss cap, turns
    the live kill switch on and writes flatten tickets; act=False (the assistant's read-only tools) writes nothing."""
    paths = Paths(workdir)
    now = parse_now(now)
    cfg = read_json(paths.config) or {}
    s = settings_of(cfg)
    problems = []
    try:
        b = live_broker(cfg)
    except CopilotError as e:
        b = None
        problems.append(str(e))
    if not s["capital"]:
        problems.append(WORDS["no_capital"])
    st = load_state(paths)
    expire_old(st, now)
    pnl, why = (refresh(paths, cfg, b, st, now, positions) if b else (None, "the config is not live"))
    view = caps_view(paths, cfg, s, st, now, pnl=pnl, pnl_why=why)
    if b and act:
        auto_kill(paths, cfg, s, st, view, now)
    if act and os.path.isdir(paths.cp):
        save_state(paths, st)
    day = now.date().isoformat()
    pend = [p for p in st["proposals"] if p["status"] == "pending"]
    return {"as_of": stamp(now), "live": b is not None, "broker": b, "settings": s, "problems": problems,
            "eligibility": eligibility(paths, s, now), "pending": pend,
            "today": [p for p in st["proposals"] if day_of(p) == day and p["status"] != "pending"],
            "open": open_live(st), "caps": view, "kill": view["kill"], "allowed_now": not cap_reasons(view),
            "blocked_now": cap_reasons(view), "countdown_s": UI_COUNTDOWN_S, "lift_words": LIFT_WORDS,
            "disclaimer": DISCLAIMER}


def render_status(d):
    L = ["Co-pilot at %s IST: %s" % (d["as_of"][11:16], ("LIVE with %s (real money, one typed approval per order)" %
                                                        d["broker"]) if d["live"] else "not live")]
    for x in d["problems"]:
        L.append("  ! " + x)
    L.append("")
    L += render_eligibility(d["eligibility"])
    L.append("")
    L += render_caps(d["caps"])
    L.append("")
    if d["pending"]:
        L.append("Waiting for your approval:")
        for p in d["pending"]:
            L.append("  %s  %s  (copilot.py approve %s)" % (p["id"], p["phrase"], p["id"]))
    else:
        L.append("No live proposal is waiting.")
    for p in d["today"]:
        L.append("  today: %s %s %s %s" % (p["id"], p["kind"], p["symbol"], p["status"]))
    L.append("")
    L.append(d["disclaimer"])
    return L


def render_eligibility(rows):
    if not rows:
        return ["No trader-lab strategy yet: nothing can trade live (lab.py run NAME, then a paper forward record)."]
    L = ["Strategies (only PROMOTE with a paper forward record that meets your thresholds can trade live):"]
    for r in rows:
        L.append("  %-24s %-8s %s" % (r["strategy"], r["verdict"], r["mode"].upper()))
        for x in r["reasons"]:
            L.append("      - " + x)
    return L


def render_caps(v):
    L = ["Today's live caps (%s):" % v["date"]]
    L.append("  live orders: %d of %d" % (v["orders"]["used"], v["orders"]["max"]))
    if v["at_risk"]["max"] is not None:
        L.append("  capital at risk (loss at the stops): %s of %s" % (inr(v["at_risk"]["used"]), inr(v["at_risk"]["max"])))
    if v["loss"]["max"] is not None:
        L.append("  live loss today: %s of %s%s" % (inr(-(v["loss"]["today"] or 0)) if v["loss"]["known"] else "unknown",
                                                  inr(v["loss"]["max"]), (" (%s)" % v["loss"]["why"]) if
                                                  v["loss"]["why"] else ""))
    if v["cooloff_until"]:
        L.append("  cooling off after a live loss until %s IST" % v["cooloff_until"][11:16])
    if v["kill"]:
        L.append("  LIVE KILL SWITCH ON: %s" % v["kill"].get("reason"))
    if v["risk_kill"]:
        L.append("  trader-risk kill switch ON: %s" % v["risk_kill"].get("reason"))
    return L


def kill(workdir, now=None, reason=None):
    paths = Paths(workdir)
    now = parse_now(now)
    cfg = load_config(paths)
    s = settings_of(cfg)
    st = load_state(paths)
    k = kill_on(paths, now, reason or "turned on by you")
    made = []
    for p in st["proposals"]:
        if p["status"] == "pending" and p["kind"] == "entry":
            p["status"] = "rejected"
            p["history"].append({"at": stamp(now), "what": "the live kill switch turned on"})
    try:
        made = flatten_proposals(paths, cfg, s, st, now, "the live kill switch is on: %s" % k["reason"])
    except CopilotError:
        made = []
    save_state(paths, st)
    return k, made


# ---------------------------------------------------------------- the command line

def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="copilot.py", description="Live co-pilot: live proposals for PROMOTED "
                                 "strategies, each sent only after your own typed approval. There is no "
                                 "auto-approve.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--workdir", default=".", help="the folder holding trader/")
        p.add_argument("--now", help="pin the IST clock, e.g. \"2026-10-29 09:41\"")
        p.add_argument("--json", action="store_true")
    for name in ("status", "eligibility", "proposals", "caps"):
        common(sub.add_parser(name))
    p = sub.add_parser("propose", help="today's live proposals from the co-trader (places nothing)")
    common(p)
    p.add_argument("--run", action="store_true", help="run the co-trader now instead of reading its last run")
    p.add_argument("--data", help="offline bars for --run")
    for name in ("approve", "reject", "exits"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("id", help="the proposal, e.g. CP-20261029-1")
    p = sub.add_parser("kill", help="live kill switch: on (no new live proposals), or --lift")
    common(p)
    p.add_argument("--reason")
    p.add_argument("--lift", action="store_true", help="lift it (asks for the typed words)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    out = print
    try:
        if a.cmd in ("status", "eligibility", "caps", "proposals"):
            d = status(a.workdir, a.now)
            if a.json:
                key = {"status": None, "eligibility": "eligibility", "caps": "caps", "proposals": "pending"}[a.cmd]
                print(json.dumps(d if key is None else d[key], indent=1, ensure_ascii=False))
                return 0
            if a.cmd == "status":
                lines = render_status(d)
            elif a.cmd == "eligibility":
                lines = render_eligibility(d["eligibility"])
            elif a.cmd == "caps":
                lines = render_caps(d["caps"]) + (["", "New live proposals now: no (%s)" % "; ".join(d["blocked_now"])]
                                                  if d["blocked_now"] else ["", "New live proposals now: allowed"])
            else:
                lines = []
                for p in d["pending"]:
                    lines += p["ticket"] + [""]
                lines = lines or ["No live proposal is waiting. copilot.py propose makes today's (it places nothing)."]
            print("\n".join(lines))
            return 0
        if a.cmd == "propose":
            r = propose(a.workdir, a.now, a.run, a.data, log=out)
            if a.json:
                print(json.dumps(r, indent=1, ensure_ascii=False, default=str))
                return 0
            for x in r.get("blocked") or []:
                print("No new live proposals: " + x)
            for p in r["flatten"]:
                print("\n".join(p["ticket"]) + "\n")
            for p in r["made"]:
                print("\n".join(p["ticket"]) + "\n")
                if p["status"] == "blocked":
                    print("  BLOCKED by trader-risk: %s\n" % "; ".join(p["risk"]["reasons"]))
            for x in r["skipped"]:
                print("skipped %s: %s" % (x["candidate"], x["why"]))
            if not r["made"] and not r.get("blocked"):
                print("No live proposal today. Most days that is the honest answer.")
            print(DISCLAIMER)
            return 0
        if a.cmd == "approve":
            r = approve(a.workdir, a.id, channel="terminal", now=a.now, log=out)
            print(json.dumps(r, indent=1, ensure_ascii=False) if a.json else
                  "Shadow: %s" % (r.get("shadow") or {}).get("why", "paper twin recorded"))
            return 0
        if a.cmd == "exits":
            approve(a.workdir, a.id, channel="terminal", now=a.now, log=out, action="exits")
            return 0
        if a.cmd == "reject":
            p = reject(a.workdir, a.id, a.now)
            print("Rejected %s: nothing was sent." % p["id"])
            return 0
        if a.cmd == "kill":
            if a.lift:
                kill_lift(Paths(a.workdir), parse_now(a.now), ask_terminal("Type %s to lift the live kill switch: "
                                                                           % LIFT_WORDS))
                print(WORDS["kill_lifted"])
                return 0
            k, made = kill(a.workdir, a.now, a.reason)
            print(WORDS["kill_on"] % (k["date"], k["reason"]))
            for p in made:
                print("\n".join(p["ticket"]) + "\n")
            return 0
    except errors() as e:
        print("copilot.py: %s" % e, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
