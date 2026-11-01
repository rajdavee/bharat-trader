"""trader-copilot, offline: who may trade live (eligibility), sizing inside the live bucket, every gate that stops a
live order, the typed phrase tied to the proposal's fingerprint, expiry and price-move invalidation, the daily
caps and the automatic live kill, the shadow paper twin, and that nothing (the MCP server, the auto-pilot, a
stale or reused approval) can send a live order without the user's fresh typed approval.

The broker is the stub from test_broker_adapters.py (a 127.0.0.1 server replaying each broker's documented
examples), so nothing here touches the network. The strategies' forward records and the co-trader's prediction
log are tests/fixtures/trader-copilot/{dip,cross}/: simulated trades on the REAL NSE daily bars in
tests/fixtures/trader-lab (10 stocks, July to October 2026; each file's note says the rule). The RELIANCE plan is
the real 2026-10-29 opening-range plan in tests/fixtures/trader-risk/orders-reliance.json.
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402
from test_broker_adapters import CFG_PATH, ENV, ORDERS_PATH, Stub, jload  # noqa: E402

cp = load("trader-copilot", "copilot")
broker = cp.broker_api()                      # the copilot's own copy of trader-broker (ADAPTER_KW lives here)
base = broker.adapters().base
CFIX = os.path.join(FIX, "trader-copilot")
NOW = "2026-10-29 09:41"
DAY = "2026-10-29"
TOKENS = {"kite": "kitetok33", "upstox": "uptok66", "dhan": "dhtok77", "angel": "angtok34"}
DIP_HASH = "c0f1e7d2a9b4f001"


def write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def lab(wd, name, verdict, fixture, history=None):
    d = os.path.join(wd, "trader", "lab", name)
    write(os.path.join(d, "status.json"), {"name": name, "verdict": verdict, "why": "test", "spec_hash": DIP_HASH,
                                           "history": history or [{"at": "2026-08-01 16:00", "verdict": "WATCH"},
                                                                  {"at": "2026-10-01 16:00", "verdict": verdict}]})
    shutil.copyfile(os.path.join(CFIX, fixture, "forward.json"), os.path.join(d, "forward.json"))


def plan(sym="RELIANCE", entry=1174.3, stop=1163.3, target=1196.3, name="t-dip-live", tier="PROMOTED"):
    return {"symbol": sym, "side": "buy", "entry": entry, "stop": stop, "target": target, "date": DAY,
            "entry_time": "09:40", "flat_by": "15:10", "setup": "cotrader-%s:%s" % (tier.lower(), name)}


def candidate(sym="RELIANCE", name="t-dip-live", tier="PROMOTED", **kw):
    return {"id": "%s-%s-%s" % (DAY, name, sym), "strategy": name, "symbol": sym, "tier": tier, "date": DAY,
            "kind": "equity", "plan": plan(sym, name=name, tier=tier, **kw),
            "evidence": {"p": 0.61, "p_lo": 0.4, "p_hi": 0.79, "trades_cell": 18, "trades_all": 18, "ev_r": 0.23,
                         "cell_words": "NIFTY trending, India VIX mid"}}


def make_wd(broker_name="kite", copilot=None, cands=None, price=1174.3, cfg_over=None):
    wd = tempfile.mkdtemp()
    t = os.path.join(wd, "trader")
    cfg = jload(CFG_PATH)
    cfg.update(broker=broker_name, paper=False, live_enabled={broker_name: True}, static_ip="203.0.113.7")
    block = {"capital": 100000, "min_forward_trades": 15, "min_forward_weeks": 4, "max_forward_drawdown_r": 6,
             "max_brier": 0.25, "min_calibration": 15, "notify": {"bell": False, "desktop": False}}
    block.update(copilot or {})
    cfg["copilot"] = block
    cfg.update(cfg_over or {})
    write(os.path.join(t, "config.json"), cfg)
    lab(wd, "t-dip-live", "PROMOTE", "dip")
    lab(wd, "t-sma20-cross", "PROMOTE", "cross")
    lab(wd, "t-dip-watch", "WATCH", "dip")
    os.makedirs(os.path.join(t, "cotrader"))
    with open(os.path.join(t, "cotrader", "log.jsonl"), "w", encoding="utf-8") as fh:
        for f in ("dip", "cross"):
            with open(os.path.join(CFIX, f, "log.jsonl"), encoding="utf-8") as src:
                fh.write(src.read())
    write(os.path.join(t, "cotrader.json"), {"as_of": "2026-10-29 09:35", "candidates": cands if cands is not None else [
        candidate(), candidate("SBIN", "t-dip-watch", "WATCH", entry=842.0, stop=830.0, target=866.0),
        candidate("TCS", "t-research", "RESEARCH", entry=3010.0, stop=2980.0, target=3070.0),
        candidate("INFY", "t-sma20-cross", entry=1500.0, stop=1480.0, target=1540.0)]})
    write(os.path.join(t, "prices.json"), {"as_of": "2026-10-29T09:40:00+05:30", "prices": {"RELIANCE": price}})
    write(os.path.join(t, "state", "preflight.json"), {broker_name: {
        "date": DAY, "at": "2026-10-29T09:30:00+05:30", "passed": True, "public_ip": "203.0.113.7",
        "token_hash": base.token_hash(TOKENS[broker_name])}})
    return wd


class Typed(object):
    """What the user types at the prompt (and what the prompt said)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = Stub()

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def setUp(self):
        self.stub.hits[:] = []
        self.stub.routes.clear()
        for b in TOKENS:
            self.stub.load(b)
        broker.ADAPTER_KW.clear()
        for b in TOKENS:
            broker.ADAPTER_KW[b] = {"hosts": self.stub.hosts(b), "env": dict(ENV), "sleep": lambda s: None}
        self.dirs = []
        self.logs = []

    def tearDown(self):
        broker.ADAPTER_KW.clear()
        cp.ASK_PHRASE = None
        cp.NOTIFY_RUN = None
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def wd(self, *a, **kw):
        d = make_wd(*a, **kw)
        self.dirs.append(d)
        return d

    def log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))

    def sends(self):
        """Every request that could change something at a broker (any write)."""
        return [h for h in self.stub.hits if h["method"] in ("POST", "PUT", "DELETE") and "margin" not in h["path"]]

    def propose(self, wd, now=NOW, **kw):
        return cp.propose(wd, now, log=self.log, **kw)

    def approve(self, wd, pid, typed, now="2026-10-29 09:42", **kw):
        cp.ASK_PHRASE = Typed(typed)
        return cp.approve(wd, pid, now=now, log=self.log, **kw)

    def state(self, wd):
        return cp.load_state(cp.Paths(wd))


# ---------------------------------------------------------------- who may trade live

class Eligibility(Base):
    def test_only_promote_with_a_forward_record_that_holds_up_is_eligible(self):
        wd = self.wd()
        rows = {r["strategy"]: r for r in cp.eligibility(cp.Paths(wd), cp.settings_of(jload(
            os.path.join(wd, "trader", "config.json"))), cp.parse_now(NOW))}
        dip = rows["t-dip-live"]
        self.assertTrue(dip["eligible"], dip["reasons"])
        self.assertEqual(dip["mode"], "live proposals")
        self.assertEqual((dip["forward"]["trades"], dip["forward"]["drawdown_r"]), (18, 2.69))
        self.assertEqual(dip["calibration"]["n"], 18)
        self.assertLessEqual(dip["calibration"]["brier"], 0.25)
        cross = rows["t-sma20-cross"]                     # PROMOTE, but its forward record fell 14.7R
        self.assertFalse(cross["eligible"])
        self.assertIn("its deepest fall was 14.7R (at most 6R)", cross["reasons"])
        watch = rows["t-dip-watch"]
        self.assertFalse(watch["eligible"])
        self.assertEqual(watch["mode"], "paper only")
        self.assertIn("trader-lab verdict WATCH: only PROMOTE can trade live (its paper forward test is running)",
                      watch["reasons"])

    def test_the_default_thresholds_are_conservative(self):
        wd = self.wd(copilot={"min_forward_trades": 30, "min_calibration": 20})
        s = cp.settings_of(jload(os.path.join(wd, "trader", "config.json")))
        dip = [r for r in cp.eligibility(cp.Paths(wd), s, cp.parse_now(NOW)) if r["strategy"] == "t-dip-live"][0]
        self.assertFalse(dip["eligible"])
        self.assertIn("18 paper forward trades (needs 30)", dip["reasons"])
        self.assertIn("18 resolved predictions (needs 20 before the Brier score counts)", dip["reasons"])
        d = cp.DEFAULTS
        self.assertEqual((d["min_forward_trades"], d["min_forward_weeks"], d["max_live_orders_per_day"]), (30, 4, 2))

    def test_brier_weeks_allow_list_and_a_recent_demote_rule_it_out(self):
        wd = self.wd(copilot={"max_brier": 0.2, "min_forward_weeks": 20, "strategies": ["t-other"]})
        s = cp.settings_of(jload(os.path.join(wd, "trader", "config.json")))
        lab(wd, "t-dip-live", "PROMOTE", "dip", history=[{"at": "2026-10-20 16:00", "verdict": "DEMOTE", "why": "decay"},
                                                          {"at": "2026-10-27 16:00", "verdict": "PROMOTE"}])
        dip = [r for r in cp.eligibility(cp.Paths(wd), s, cp.parse_now(NOW)) if r["strategy"] == "t-dip-live"][0]
        text = " | ".join(dip["reasons"])
        self.assertIn("Brier 0.239 on 18 predictions (at most 0.2", text)
        self.assertIn("spans 16.1 weeks (needs 20)", text)
        self.assertIn("not in your copilot.strategies list", text)
        self.assertIn("demoted at 2026-10-20 16:00 (decay)", text)

    def test_there_is_no_auto_approve_setting(self):
        for key in ("auto_approve", "autoApprove", "unattended", "approve", "auto_place"):
            with self.assertRaises(cp.CopilotError) as e:
                cp.settings_of({"copilot": {"capital": 50000, key: True}})
            self.assertIn("no auto-approve", str(e.exception))
        with self.assertRaises(cp.CopilotError):
            cp.settings_of({"copilot": {"capital": 50000, "max_orders": 9}})          # unknown keys are refused

    def test_a_paper_config_makes_no_live_proposal(self):
        wd = self.wd(cfg_over={"paper": True, "broker": "paper", "live_enabled": {}})
        with self.assertRaises(cp.CopilotError) as e:
            self.propose(wd)
        self.assertIn("PAPER broker", str(e.exception))
        self.assertEqual(self.sends(), [])


# ---------------------------------------------------------------- proposals

class Proposals(Base):
    def test_promoted_and_eligible_only_sized_in_the_live_bucket(self):
        wd = self.wd()
        r = self.propose(wd)
        self.assertEqual([p["symbol"] for p in r["made"]], ["RELIANCE"])
        p = r["made"][0]
        self.assertEqual(p["status"], "pending", p["risk"])
        self.assertEqual(p["risk"]["verdict"], "PASS")
        self.assertEqual(p["phrase"], "yes live BUY 42 RELIANCE")
        self.assertEqual(p["order"]["qty"], 42)
        self.assertEqual(p["max_loss"], 514.11)
        self.assertEqual(p["order"]["stop_loss"], {"order_type": "SL", "trigger_price": 1163.3, "price": 1151.6})
        why = {x["candidate"]: x["why"] for x in r["skipped"]}
        self.assertIn("WATCH is paper only", why["2026-10-29-t-dip-watch-SBIN"])
        self.assertIn("RESEARCH is paper only (unproven)", why["2026-10-29-t-research-TCS"])
        self.assertIn("t-sma20-cross is not eligible for live", why["2026-10-29-t-sma20-cross-INFY"])
        text = "\n".join(p["ticket"])
        for words in ("LIVE ORDER at Zerodha Kite (real money)", "BUY 42 RELIANCE on NSE, MIS",
                      "max loss at the stop: ₹514.11 including charges", "GTT OCO",
                      "61% of 18 similar past trades made money after charges (95% 40% to 79%)",
                      "calibration: Brier 0.239 on 18 resolved predictions", "broker margin: Zerodha Kite says",
                      "to send it, type exactly: yes live BUY 42 RELIANCE"):
            self.assertIn(words, text)
        self.assertEqual(self.sends(), [])                                       # proposing sends nothing
        doc = jload(p["order_file"])
        self.assertEqual(len(doc["orders"]), 1)
        self.assertFalse(doc["paper"])

    def test_the_live_bucket_caps_the_size(self):
        wd = self.wd(copilot={"capital": 20000})
        p = self.propose(wd)["made"][0]
        self.assertEqual(p["order"]["qty"], 8)              # 50% of ₹20,000 / 1174.30, not the account's ₹1,00,000
        self.assertEqual(p["phrase"], "yes live BUY 8 RELIANCE")

    def test_bracket_brokers_send_the_exits_with_the_entry(self):
        wd = self.wd("upstox")
        p = self.propose(wd)["made"][0]
        self.assertEqual((p["entry_leg"], p["exits_leg"]), ("bracket", None))
        self.assertIn("WITH the entry as one Upstox order", "\n".join(p["ticket"]))

    def test_desktop_notification_and_bell_are_local_and_optional(self):
        calls = []
        cp.NOTIFY_RUN = calls.append
        wd = self.wd(copilot={"notify": {"bell": False, "desktop": True}})
        self.propose(wd)
        self.assertEqual(len(calls), 1)
        self.assertIn("yes live BUY 42 RELIANCE", calls[0][-1])
        self.assertIn(calls[0][0], ("osascript", "notify-send"))


# ---------------------------------------------------------------- approval and sending

class Approve(Base):
    def proposal(self, wd):
        return self.propose(wd)["made"][0]

    def test_the_exact_phrase_sends_the_order_and_records_a_shadow_twin(self):
        wd = self.wd()
        p = self.proposal(wd)
        r = self.approve(wd, p["id"], "yes live BUY 42 RELIANCE")
        self.assertEqual(len(self.stub.calls("POST", "/kite/orders/regular")), 1)
        body = self.stub.calls("POST", "/kite/orders/regular")[0]["body"].decode()
        self.assertIn("tradingsymbol=RELIANCE", body)
        self.assertIn("quantity=42", body)
        self.assertEqual(r["broker_order_id"], "151220000000000")
        st = self.state(wd)
        q = cp.find(st, p["id"])
        self.assertEqual(q["status"], "sent")
        bstate = jload(os.path.join(wd, "trader", "state", "broker.json"))["orders"]
        self.assertEqual([x["status"] for x in bstate], ["approved", "placed"])
        self.assertEqual(bstate[0]["yes_text"], "yes live BUY 42 RELIANCE")
        self.assertNotIn("actor", bstate[0])                                     # the user's own yes
        shadow = jload(os.path.join(wd, "trader", "state", "shadow.json"))["orders"]
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["broker_order_id"], "151220000000000")
        self.assertIn("BUY LIMIT 1174.30 waits", shadow[0]["paper"]["why"])     # the paper twin, same rules
        self.assertTrue(r["shadow"]["recorded"])
        with open(os.path.join(wd, "trader", "copilot", "approvals.jsonl"), encoding="utf-8") as fh:
            a = [json.loads(x) for x in fh]
        self.assertEqual((a[0]["phrase"], a[0]["fingerprint"], a[0]["channel"]),
                         ("yes live BUY 42 RELIANCE", p["fingerprint"], "terminal"))
        # one approval, one order
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], "yes live BUY 42 RELIANCE")
        self.assertIn("is sent, not pending", str(e.exception))
        self.assertEqual(len(self.stub.calls("POST", "/kite/orders/regular")), 1)

    def test_any_other_phrase_sends_nothing(self):
        for typed in ("yes", "y", "yes live buy 42 reliance", "yes live BUY 43 RELIANCE", "yes BUY 42 RELIANCE",
                      "yes live BUY 42 RELIANCE now", "", "ok", "yes live SELL 42 RELIANCE"):
            wd = self.wd()
            p = self.proposal(wd)
            with self.assertRaises(cp.CopilotError) as e:
                self.approve(wd, p["id"], typed)
            self.assertIn("did not match", str(e.exception))
            self.assertEqual(cp.find(self.state(wd), p["id"])["status"], "pending")
        self.assertEqual(self.sends(), [])

    def test_the_prompt_shows_the_ticket_and_asks_for_the_phrase(self):
        wd = self.wd()
        p = self.proposal(wd)
        ask = Typed("nope")
        cp.ASK_PHRASE = ask
        with self.assertRaises(cp.CopilotError):
            cp.approve(wd, p["id"], now="2026-10-29 09:42", log=self.log)
        self.assertIn("Type exactly  yes live BUY 42 RELIANCE  to send this LIVE order", ask.prompts[0])
        self.assertIn("LIVE ORDER at Zerodha Kite (real money): %s" % p["id"], self.logs[0])

    def test_no_terminal_means_no_approval(self):
        wd = self.wd()
        p = self.proposal(wd)
        cp.ASK_PHRASE = None
        old = sys.stdin
        sys.stdin = open(os.devnull)
        try:
            with self.assertRaises(cp.CopilotError) as e:
                cp.approve(wd, p["id"], now="2026-10-29 09:42", log=self.log)
        finally:
            sys.stdin.close()
            sys.stdin = old
        self.assertIn("nobody is at the keyboard", str(e.exception))
        self.assertEqual(self.sends(), [])

    def test_expiry_and_a_price_move_invalidate_the_proposal(self):
        wd = self.wd()
        p = self.proposal(wd)
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"], now="2026-10-29 09:47")        # 6 minutes: expired
        self.assertIn("expired", str(e.exception))
        self.assertEqual(cp.find(self.state(wd), p["id"])["status"], "expired")
        wd = self.wd(price=1174.3)
        p = self.proposal(wd)
        write(os.path.join(wd, "trader", "prices.json"), {"as_of": "2026-10-29T09:41:30+05:30",
                                                          "prices": {"RELIANCE": 1181.5}})     # +0.61%
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("the price moved 0.61% from the plan's 1174.30 to 1181.50 (more than 0.5%): re-plan",
                      str(e.exception))
        self.assertEqual(cp.find(self.state(wd), p["id"])["status"], "invalid")
        wd = self.wd()
        p = self.proposal(wd)
        os.remove(os.path.join(wd, "trader", "prices.json"))
        broker.ADAPTER_KW["kite"]["env"] = {k: v for k, v in ENV.items() if k != "BT_KITE_ACCESS_TOKEN"}
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("no fresh price for RELIANCE", str(e.exception))
        self.assertEqual(self.sends(), [])

    def test_every_broker_gate_stops_it(self):
        """With the phrase typed right, each of trader-broker's gates still stops the send on its own."""
        cases = {
            "live_enabled": lambda wd, cfg: cfg.update(live_enabled={"kite": False}),
            "preflight": lambda wd, cfg: os.remove(os.path.join(wd, "trader", "state", "preflight.json")),
            "token": lambda wd, cfg: write(os.path.join(wd, "trader", "state", "preflight.json"), {"kite": dict(
                jload(os.path.join(wd, "trader", "state", "preflight.json"))["kite"], token_hash="0" * 16)}),
        }
        for gate, breakit in cases.items():
            wd = self.wd()
            p = self.proposal(wd)
            path = os.path.join(wd, "trader", "config.json")
            cfg = jload(path)
            breakit(wd, cfg)
            write(path, cfg)
            with self.assertRaises(cp.CopilotError) as e:
                self.approve(wd, p["id"], p["phrase"])
            self.assertIn("NOT sent", str(e.exception), gate)
            self.assertEqual(self.stub.calls("POST", "/kite/orders"), [], gate)
        # the config no longer live, or another broker: refused before the phrase is asked
        for over in ({"paper": True, "broker": "paper"}, {"broker": "upstox", "live_enabled": {"upstox": True}}):
            wd = self.wd()
            p = self.proposal(wd)
            path = os.path.join(wd, "trader", "config.json")
            write(path, dict(jload(path), **over))
            ask = Typed(p["phrase"])
            cp.ASK_PHRASE = ask
            with self.assertRaises(cp.CopilotError):
                cp.approve(wd, p["id"], now="2026-10-29 09:42", log=self.log)
            self.assertEqual(ask.prompts, [])
        self.assertEqual(self.sends(), [])

    def test_a_risk_block_at_approval_stops_it(self):
        wd = self.wd()
        p = self.proposal(wd)
        path = os.path.join(wd, "trader", "config.json")
        write(path, dict(jload(path), max_loss_per_trade=400))       # the loss at the stop is ₹514.11 now
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("trader-risk BLOCK on your live config now", str(e.exception))
        write(os.path.join(wd, "trader", "state", "kill.json"), {"active": True, "date": DAY, "reason": "test"})
        self.assertEqual(self.sends(), [])

    def test_an_edited_order_file_or_proposal_is_refused(self):
        wd = self.wd()
        p = self.proposal(wd)
        doc = jload(p["order_file"])
        doc["orders"][0]["qty"] = 420
        write(p["order_file"], doc)
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("the order file changed", str(e.exception))
        wd = self.wd()
        p = self.proposal(wd)
        st = self.state(wd)
        cp.find(st, p["id"])["plan_entry"] = 1100.0
        cp.save_state(cp.Paths(wd), st)
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("fingerprint", str(e.exception))
        self.assertEqual(self.sends(), [])

    def test_send_needs_a_fresh_unused_approval_for_this_fingerprint(self):
        """_send is the only path to the broker's place: call it directly with every kind of bad approval."""
        wd = self.wd()
        p0 = self.proposal(wd)
        paths = cp.Paths(wd)
        cfg = jload(paths.config)
        st = self.state(wd)
        p = cp.find(st, p0["id"])
        now = cp.parse_now("2026-10-29 09:42")
        good = {"proposal": p["id"], "fingerprint": p["fingerprint"], "order_fp": p["order_fp"], "phrase": p["phrase"],
                "channel": "terminal", "actor": "user", "action": "place", "nonce": "a1b2c3d4e5f60718",
                "at": cp.stamp(now)}
        bad = [None, {}, dict(good, actor="autopilot"), dict(good, channel="mcp"), dict(good, fingerprint="0" * 16),
               dict(good, proposal="CP-20261029-9"), dict(good, phrase="yes"), dict(good, action="exits"),
               dict(good, at="2026-10-29T09:30:00+05:30"), dict(good, nonce=None), dict(good, order_fp="1" * 16)]
        for a in bad:
            with self.assertRaises(cp.CopilotError, msg=a) as e:
                cp._send(paths, cfg, st, p, a, now, "place", self.log)
            self.assertIn("NOT sent", str(e.exception))
        self.assertEqual(self.sends(), [])
        st["used_nonces"].append(good["nonce"])
        with self.assertRaises(cp.CopilotError) as e:
            cp._send(paths, cfg, st, p, good, now, "place", self.log)
        self.assertIn("used already", str(e.exception))
        self.assertEqual(self.sends(), [])

    def test_an_autopilot_yes_never_opens_the_live_gate(self):
        wd = self.wd()
        p = self.proposal(wd)
        # the auto-pilot's machine yes on this live config: trader-broker refuses to even record it
        code = broker.main(["confirm", "--orders", p["order_file"], "--risk", p["risk_file"], "--config",
                            os.path.join(wd, "trader", "config.json"), "--state",
                            os.path.join(wd, "trader", "state", "broker.json"), "--index", "1", "--yes-text",
                            "yes (autopilot machine yes, paper account)", "--actor", "autopilot", "--now", NOW])
        self.assertEqual(code, 1)
        # and a yes recorded as autopilot (written by hand) does not satisfy place's gate either
        write(os.path.join(wd, "trader", "state", "broker.json"), {"orders": [{
            "date": DAY, "at": "2026-10-29T09:41:00+05:30", "fingerprint": p["order_fp"], "status": "approved",
            "broker": "kite", "yes_text": "yes", "actor": "autopilot", "index": 1, "symbol": "RELIANCE"}]})
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["phrase"])
        self.assertIn("NOT sent", str(e.exception))
        self.assertEqual(self.sends(), [])
        # the auto-pilot never loads the co-pilot, and refuses a live config itself
        with open(os.path.join(ROOT, "trader-autopilot", "autopilot.py"), encoding="utf-8") as fh:
            ap = fh.read()
        self.assertNotIn("trader-copilot", ap)
        self.assertNotIn('"place"', ap)

    def test_kite_exits_go_as_one_gtt_oco_after_the_fill_with_their_own_phrase(self):
        wd = self.wd()
        p = self.proposal(wd)
        self.approve(wd, p["id"], p["phrase"])
        self.assertEqual(p["exit_phrase"], "yes live exits SELL 42 RELIANCE")
        book = jload(os.path.join(FIX, "trader-broker", "kite", "orders.json"))["response"]["body"]
        book["data"][0].update(order_id="151220000000000", status="COMPLETE", filled_quantity=42, average_price=1174.3)
        self.stub.add("GET", "/kite/orders", body=book)
        with self.assertRaises(cp.CopilotError):
            self.approve(wd, p["id"], p["phrase"], action="exits")                # the entry's phrase is not it
        self.assertEqual(self.stub.calls("POST", "/kite/gtt"), [])
        self.approve(wd, p["id"], p["exit_phrase"], now="2026-10-29 09:45", action="exits")
        gtt = self.stub.calls("POST", "/kite/gtt/triggers")
        self.assertEqual(len(gtt), 1)
        self.assertIn("1163.3", gtt[0]["body"].decode())
        with self.assertRaises(cp.CopilotError) as e:
            self.approve(wd, p["id"], p["exit_phrase"], now="2026-10-29 09:46", action="exits")
        self.assertIn("already sent", str(e.exception))
        self.assertEqual(len(self.stub.calls("POST", "/kite/gtt/triggers")), 1)

    def test_upstox_sends_one_bracket(self):
        wd = self.wd("upstox")
        p = self.proposal(wd)
        self.approve(wd, p["id"], p["phrase"])
        self.assertEqual(len(self.stub.calls("POST", "/upstox/v3/order/gtt/place")), 1)
        self.assertEqual([h["path"] for h in self.sends()], ["/upstox/v3/order/gtt/place"])
        rec = jload(os.path.join(wd, "trader", "state", "broker.json"))["orders"][-1]
        self.assertEqual((rec["leg"], rec["linked"]), ("bracket", "bracket"))

    def test_the_ui_dialog_countdown_then_phrase_sends_it(self):
        api = load("ui", "api")
        wd = self.wd()
        ctx = api.Context(wd, now="2026-10-29 09:41")
        status, d = api.dispatch(ctx, "POST", "/api/copilot/propose", {}, {})
        self.assertEqual(status, 200, d)
        pid = d["proposed"]["made"][0]
        self.assertEqual(d["pending"][0]["phrase"], "yes live BUY 42 RELIANCE")
        self.assertEqual(d["countdown_s"], 10)
        client = {"session": "s1", "peer": "127.0.0.1"}
        t = [1000.0]
        api.CLOCK = lambda: t[0]
        try:
            self.assertEqual(api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": pid},
                                          dict(client, peer="192.0.2.9"))[0], 403)
            status, arm = api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": pid}, client)
            self.assertEqual((status, arm["wait_s"]), (200, 10))
            body = {"id": pid, "nonce": arm["nonce"], "typed": "yes live BUY 42 RELIANCE"}
            t[0] += 9
            status, d = api.dispatch(ctx, "POST", "/api/copilot/approve", {}, body, client)
            self.assertEqual(status, 409)
            self.assertIn("countdown", d["error"])
            self.assertEqual(self.sends(), [])
            status, arm = api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": pid}, client)
            t[0] += 11
            status, d = api.dispatch(ctx, "POST", "/api/copilot/approve", {}, dict(body, nonce=arm["nonce"],
                                                                                 typed="yes"), client)
            self.assertEqual(status, 409)
            self.assertIn("did not match", d["error"])
            self.assertEqual(self.sends(), [])
            status, arm = api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": pid}, client)
            t[0] += 11
            self.assertEqual(api.dispatch(ctx, "POST", "/api/copilot/approve", {}, dict(body, nonce=arm["nonce"]),
                                          dict(client, session="s2"))[0], 409)
            status, arm = api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": pid}, client)
            t[0] += 11
            status, d = api.dispatch(ctx, "POST", "/api/copilot/approve", {}, dict(body, nonce=arm["nonce"]), client)
            self.assertEqual(status, 200, d)
            self.assertEqual(d["sent"]["broker_order_id"], "151220000000000")
            self.assertEqual(len(self.stub.calls("POST", "/kite/orders/regular")), 1)
            with open(os.path.join(wd, "trader", "copilot", "approvals.jsonl"), encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.readline())["channel"], "ui")
        finally:
            api.CLOCK = __import__("time").monotonic

    def test_reject(self):
        wd = self.wd()
        p = self.proposal(wd)
        self.assertEqual(cp.reject(wd, p["id"], NOW)["status"], "rejected")
        with self.assertRaises(cp.CopilotError):
            self.approve(wd, p["id"], p["phrase"])
        self.assertEqual(self.sends(), [])


# ---------------------------------------------------------------- caps and the live kill

class Caps(Base):
    def positions(self, pnl, qty=42, realised=0.0):
        body = jload(os.path.join(FIX, "trader-broker", "kite", "positions.json"))["response"]["body"]
        row = dict(body["data"]["net"][2], tradingsymbol="RELIANCE", product="MIS", quantity=qty, pnl=pnl,
                   realised=realised, unrealised=pnl - realised, buy_quantity=42, sell_quantity=42 - qty,
                   last_price=1150.0)
        body["data"]["net"] = [row]
        self.stub.add("GET", "/kite/portfolio/positions", body=body)

    def sent(self, wd):
        p = self.propose(wd)["made"][0]
        self.approve(wd, p["id"], p["phrase"])
        return p

    def test_orders_a_day_cap(self):
        wd = self.wd(copilot={"max_live_orders_per_day": 1})
        self.sent(wd)
        write(os.path.join(wd, "trader", "cotrader.json"), {"as_of": "2026-10-29 10:00", "candidates": [
            candidate("HDFCBANK", entry=990.0, stop=982.0, target=1006.0)]})
        r = self.propose(wd, "2026-10-29 10:00")
        self.assertEqual(r["made"], [])
        self.assertIn("1 of 1 live orders today already: the daily cap", r["blocked"])

    def test_capital_at_risk_cap(self):
        wd = self.wd(copilot={"max_capital_at_risk_pct": 0.5})          # ₹500 of ₹1,00,000; this one risks ₹514.11
        r = self.propose(wd)
        self.assertEqual(r["made"], [])
        self.assertIn("live capital at risk would be ₹514.11, over the cap ₹500.00",
                      [x for x in r["skipped"] if x["strategy"] == "t-dip-live"][0]["why"])

    def test_daily_live_loss_turns_the_live_kill_on_and_offers_flatten_tickets(self):
        wd = self.wd()
        p = self.sent(wd)
        self.positions(-1050.0)                                           # over 1% of ₹1,00,000
        write(os.path.join(wd, "trader", "prices.json"), {"as_of": "2026-10-29T10:30:00+05:30",
                                                          "prices": {"RELIANCE": 1150.0}})
        d = cp.status(wd, "2026-10-29 10:30")
        self.assertTrue(d["kill"]["active"])
        self.assertTrue(d["kill"]["auto"])
        self.assertIn("today's live loss ₹1,050.00 reached the cap ₹1,000.00", d["kill"]["reason"])
        flat = [x for x in d["pending"] if x["kind"] == "flatten"]
        self.assertEqual(len(flat), 1)
        f = flat[0]
        self.assertEqual((f["order"]["side"], f["order"]["qty"], f["order"]["order_type"], f["order"]["price"]),
                         ("SELL", 42, "LIMIT", 1138.5))
        self.assertEqual(f["phrase"], "yes live SELL 42 RELIANCE")
        self.assertEqual(f["risk"]["verdict"], "PASS")                    # an exit passes under the kill switch
        self.assertEqual(self.sends(), [self.stub.calls("POST", "/kite/orders/regular")[0]])   # only the entry
        # no new live proposal while it is on
        r = self.propose(wd, "2026-10-29 10:31")
        self.assertIn("the live kill switch is on", r["blocked"][0])
        # the flatten ticket still needs the typed phrase
        with self.assertRaises(cp.CopilotError):
            self.approve(wd, f["id"], "yes", now="2026-10-29 10:31")
        self.assertEqual(len(self.stub.calls("POST", "/kite/orders/regular")), 1)
        self.approve(wd, f["id"], f["phrase"], now="2026-10-29 10:31")
        orders = self.stub.calls("POST", "/kite/orders/regular")
        self.assertEqual(len(orders), 2)
        self.assertIn("transaction_type=SELL", orders[1]["body"].decode())
        st = self.state(wd)
        self.assertEqual(cp.find(st, p["id"])["status"], "closed")
        # lifting needs the typed words
        with self.assertRaises(cp.CopilotError):
            cp.kill_lift(cp.Paths(wd), cp.parse_now("2026-10-29 10:40"), "unkill")
        cp.kill_lift(cp.Paths(wd), cp.parse_now("2026-10-29 10:40"), "Lift  live kill")
        self.assertIsNone(cp.kill_active(cp.Paths(wd), DAY))

    def test_cool_off_after_a_live_loss(self):
        wd = self.wd(copilot={"max_live_orders_per_day": 5})
        self.sent(wd)
        self.positions(-300.0, qty=0, realised=-300.0)                    # closed at a loss
        d = cp.status(wd, "2026-10-29 10:30")
        self.assertEqual(d["caps"]["cooloff_until"][11:16], "11:30")
        self.assertIn("cooling off after a live loss until 11:30 IST", d["blocked_now"])
        self.assertFalse(d["kill"])

    def test_manual_kill_rejects_waiting_entries(self):
        wd = self.wd()
        p = self.propose(wd)["made"][0]
        k, made = cp.kill(wd, "2026-10-29 09:42", "stepping away")
        self.assertEqual(k["reason"], "stepping away")
        self.assertEqual(cp.find(self.state(wd), p["id"])["status"], "rejected")
        self.assertEqual(made, [])


# ---------------------------------------------------------------- nothing else can approve

class NoOtherPath(unittest.TestCase):
    def test_place_and_cancel_only_inside_send(self):
        """In copilot.py, the broker's write commands appear in one function only, and that function checks a
        fresh typed approval for this proposal's fingerprint before anything else."""
        path = os.path.join(ROOT, "trader-copilot", "copilot.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        where = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.List) and any(isinstance(e, ast.Constant) and e.value in ("place", "cancel")
                                                          for e in node.elts):
                        where.add(fn.name)
        self.assertEqual(where, {"_send"})
        send = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_send"][0]
        first = send.body[1]          # after the docstring
        self.assertIn("_check_approval", ast.dump(first))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_send"]
        callers = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and
                   any(c in list(ast.walk(f)) for c in calls)]
        self.assertEqual(callers, ["approve"])
        self.assertNotIn("auto", " ".join(cp.DEFAULTS))
        self.assertEqual(cp.BROKER_COMMANDS, ("confirm", "place", "cancel"))

    def test_the_mcp_server_and_the_ask_panel_have_no_approval_path(self):
        for f in ("server.py", "toolkit.py"):
            with open(os.path.join(ROOT, "mcp", f), encoding="utf-8") as fh:
                text = fh.read()
            for bad in ("copilot/approve", "copilot/arm", "r_copilot_approve", "r_copilot_arm", "copilot_approve",
                        ".approve(", "kill_lift", "copilot/exits"):
                self.assertNotIn(bad, text, f)
        sys.path.insert(0, os.path.join(ROOT, "mcp"))
        tk = load("mcp", "toolkit")
        names = [t["name"] for t in tk.build_tools()]
        self.assertIn("copilot_status", names)
        self.assertIn("copilot_proposals", names)
        self.assertEqual([n for n in names if "approve" in n or ("copilot" in n and n not in (
            "copilot_status", "copilot_proposals"))], [])
        for t in tk.build_tools():
            if t["name"].startswith("copilot"):
                self.assertTrue(t["annotations"]["readOnlyHint"], t["name"])
        chat = load("ui", "chat")
        with open(os.path.join(ROOT, "ui", "chat.py"), encoding="utf-8") as fh:
            self.assertNotIn("copilot/approve", fh.read())
        self.assertTrue(hasattr(chat, "allowed_tools"))

    def test_the_ui_approval_needs_a_session_a_countdown_and_the_phrase(self):
        """The UI route refuses a call without a browser session (the MCP server and the Ask panel have none),
        before the 10-second countdown, and with any phrase but the ticket's."""
        api = load("ui", "api")
        wd = make_wd()
        try:
            ctx = api.Context(wd, now="2026-10-29 09:41")
            status, d = api.dispatch(ctx, "POST", "/api/copilot/approve", {}, {"id": "CP-20261029-1",
                                                                              "typed": "yes live BUY 42 RELIANCE"})
            self.assertEqual(status, 403, d)
            status, d = api.dispatch(ctx, "POST", "/api/copilot/arm", {}, {"id": "CP-20261029-1"})
            self.assertEqual(status, 403, d)
        finally:
            shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
