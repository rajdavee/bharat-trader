"""The JSON API of the local UI: one table of routes, each mapped to the skills' own functions.

Every number comes from a skill script, loaded straight from its folder (the folder names
have hyphens, so they are loaded by path, like tests/_load.py does). Nothing here places an
order: the only order-path writes are the paper broker's own commands (confirm, decline,
fill), and they still need trader-risk PASS for that exact order plus the user's typed yes.

    ctx = Context(workdir=".", offline=None)
    status, body = dispatch(ctx, "GET", "/api/status", {}, None)

ROUTES is the single source of truth. ui/serve.py serves it over HTTP; another front end
(an MCP server, say) can import this module and call dispatch() the same way.

Two chart overlays are computed here because no skill has them yet: the EMA and the
session VWAP. They are drawn on the chart and explained in its "why" panel; no skill
decision uses them.
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VERSION = "0.2.0-dev"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDICES = [("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"), ("^INDIAVIX", "INDIA VIX")]
INTERVALS = {"1m": 60, "5m": 300, "1d": 86400}
SKILLS = {
    "alerts": ("trader-alerts", "alerts"),
    "entry": ("trader-entry", "entry"),
    "risk": ("trader-risk", "check"),
    "broker": ("trader-broker", "ticket"),
    "budget": ("trader-budget", "size"),
    "scanner": ("trader-scanner", "scan"),
}
# how long a live answer is reused, in seconds (free endpoints: be polite)
TTL = {"quotes": 15, "nse": 120, "chart-1m": 30, "chart-5m": 60, "chart-1d": 900}
MAX_SYMBOLS = 12
HHMM_KEYS = ("no_entries_before", "no_entries_after", "flat_by")
PCT_KEYS = ("risk_per_trade_pct", "max_position_pct", "cash_buffer_pct")
DISCLAIMER = "Paper trading. Education and tooling, not financial advice."

_mods = {}
_mods_lock = threading.Lock()


def skill(name):
    """The skill script as a module, loaded once from the repo."""
    with _mods_lock:
        if name not in _mods:
            folder, script = SKILLS[name]
            path = os.path.join(ROOT, folder, script + ".py")
            spec = importlib.util.spec_from_file_location("bt_ui_" + script, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _mods[name] = mod
        return _mods[name]


def skill_errors():
    """Every skill's own error class: a message for the user, not a crash."""
    out = []
    for name, cls in (("alerts", "AlertError"), ("entry", "EntryError"), ("risk", "RiskError"),
                      ("broker", "TicketError"), ("budget", "SizeError"), ("scanner", "ScanError")):
        out.append(getattr(skill(name), cls))
    return tuple(out)


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def why(formula, inputs=None, source="", as_of=None, note=None):
    """The "why" panel of one number: how it was worked out, from what, and when."""
    out = {"formula": formula, "inputs": inputs or {}, "source": source, "as_of": as_of}
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- context

class Context(object):
    """Where the trader/ files live, where offline fixtures live, the clock and a small cache."""

    def __init__(self, workdir=".", offline=None, now=None):
        self.workdir = os.path.abspath(workdir)
        self.offline = os.path.abspath(offline) if offline else None
        manifest = {}
        if self.offline and os.path.isfile(os.path.join(self.offline, "offline.json")):
            with open(os.path.join(self.offline, "offline.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
        self.offline_note = manifest.get("note")
        self.pinned = now or manifest.get("now")
        if self.pinned:
            skill("alerts").parse_now(self.pinned)        # fail early on a bad --now
        self.cmd_lock = threading.RLock()                  # skill commands print: one at a time
        self._cache = {}
        self._cache_lock = threading.Lock()

    # files
    def path(self, *parts):
        return os.path.join(self.workdir, "trader", *parts)

    def read(self, *parts):
        """A trader/ JSON file, or None if it is not there. Bad JSON is an error the user can fix."""
        p = self.path(*parts)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError as e:
            raise ApiError(422, "trader/%s is not valid JSON: %s" % ("/".join(parts), e))

    def config(self):
        doc = self.read("config.json")
        return doc if isinstance(doc, dict) else {}

    # clock
    def now_utc(self):
        """Naive UTC, the way trader-alerts and trader-entry keep time."""
        return skill("alerts").parse_now(self.pinned)

    def now_ist(self):
        return self.now_utc().replace(tzinfo=datetime.timezone.utc).astimezone(IST)

    def today(self):
        return self.now_ist().date().isoformat()

    def now_args(self):
        """--now for a skill command when the clock is pinned (offline demo, tests)."""
        return ["--now", self.now_ist().strftime("%Y-%m-%d %H:%M:%S")] if self.pinned else []

    # cache
    def cached(self, key, ttl, fn):
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._cache_lock:
            self._cache[key] = (time.time(), value)
            if len(self._cache) > 256:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:64]
                for k, _ in oldest:
                    self._cache.pop(k, None)
        return value

    def forget(self, prefix):
        with self._cache_lock:
            for k in [k for k in self._cache if str(k[0]).startswith(prefix)]:
                self._cache.pop(k, None)


def ist_text(now_ist):
    return now_ist.replace(microsecond=0).isoformat()


def run_skill(ctx, name, argv):
    """Run a skill's own command line (the existing flow, with its own checks) and capture what it says."""
    mod = skill(name)
    out, err = io.StringIO(), io.StringIO()
    with ctx.cmd_lock:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = mod.main(argv)
            except SystemExit as e:                    # argparse said no
                rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- shared readers

def _symbols(text):
    syms = []
    for part in re.split(r"[\s,;]+", str(text or "")):
        if part:
            try:
                s = skill("alerts").normalise(part)
            except skill_errors() as e:
                raise ApiError(400, str(e))
            if s not in syms:
                syms.append(s)
    return syms


def quotes(ctx, symbols):
    """{SYM: quote} and errors, from trader-alerts' get_quotes (fixtures when offline), cached briefly."""
    syms = list(symbols)[:MAX_SYMBOLS]
    if not syms:
        return {}, []
    key = ("quotes", tuple(syms))
    return ctx.cached(key, TTL["quotes"], lambda: skill("alerts").get_quotes(syms, ctx.offline))


def quote_rows(qs, now_ist):
    out = []
    for sym, q in qs.items():
        row = dict(q)
        prev = q.get("prev_close")
        if prev:
            row["change"] = round(q["price"] - prev, 2)
            row["change_pct"] = round(100.0 * (q["price"] / prev - 1), 2)
        row["why"] = why("change % = (price / previous close - 1) x 100",
                         {"price": q["price"], "previous close": prev},
                         "Yahoo Finance chart, via trader-alerts get_quotes (free, delayed)",
                         q.get("time") or ist_text(now_ist),
                         "Delayed and unofficial: for alerts and context only. Your broker's price is the one "
                         "that fills.")
        out.append(row)
    return out


def market(ctx, verify=False):
    alerts = skill("alerts")
    try:
        cal = alerts.load_calendar(ctx.path("config.json"))
    except alerts.AlertError as e:
        raise ApiError(422, str(e))
    st = alerts.market_status(ctx.now_utc(), cal)
    st["why"] = why("the NSE calendar: Mon-Fri, pre-open 09:00-09:08, normal session 09:15-15:30 IST, minus the "
                    "2026 trading holidays, plus your extra_holidays and special_sessions",
                    {"now (IST)": st["now_ist"], "date": st["date"], "weekday": st["weekday"]},
                    "trader-alerts market_status (no network)", st["now_ist"])
    if verify:
        try:
            nse = ctx.cached(("nse",), TTL["nse"], lambda: alerts.nse_status(ctx.offline))
            st["nse"] = nse
            st["nse_agrees"] = nse["open"] == st["open"]
        except alerts.AlertError as e:
            st["nse_error"] = str(e)
    return st


def watched_symbols(ctx):
    syms = []
    pf = ctx.read("portfolio.json") or {}
    pos = pf.get("positions") if isinstance(pf, dict) else None
    if isinstance(pos, dict):
        syms += [str(s).upper() for s in pos]
    plan = ctx.read("plan.json")
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    out = []
    for s in syms:
        if s and s not in out:
            out.append(s)
    return out


def rings(ctx, qs=None):
    """What the alert rules would ring right now (a dry run: nothing is written)."""
    alerts = skill("alerts")
    doc = ctx.read("alerts.json") or {}
    rules = doc.get("rules") or [] if isinstance(doc, dict) else []
    state = ctx.read("state", "alerts.json") or {}
    if not rules:
        return {"rules": [], "rings": [], "errors": []}
    now = ctx.now_utc()
    status = alerts.market_status(now, alerts.load_calendar(ctx.path("config.json")))
    syms = []
    for r in rules:
        if alerts.active(r, status["date"]) and r.get("type") not in alerts.NO_QUOTE and r.get("symbol"):
            s = alerts.normalise(r["symbol"])
            if s not in syms:
                syms.append(s)
    errors = []
    if qs is None:
        qs, errors = quotes(ctx, syms)
    try:
        hit, _ = alerts.evaluate(rules, qs, status, state, now)
    except alerts.AlertError as e:
        raise ApiError(422, str(e))
    rung = state.get("rung", {}) if isinstance(state, dict) else {}
    rows = []
    for r in rules:
        rid = r.get("id")
        rows.append(dict(r, active=alerts.active(r, status["date"]), rang_at=rung.get(rid)))
    return {"rules": rows, "rings": hit, "errors": errors, "checked_at": status["now_ist"]}


# ---------------------------------------------------------------- routes: market

def r_meta(ctx, q, body):
    cfg = ctx.config()
    c = skill("broker").merged_config(cfg)
    return {"app": "bharat-trader", "version": VERSION, "workdir": ctx.workdir, "offline": bool(ctx.offline),
            "offline_note": ctx.offline_note, "pinned_now": ctx.pinned, "now_ist": ist_text(ctx.now_ist()),
            "paper": bool(c.get("paper", True)) and str(c.get("broker", "paper")).lower() == "paper",
            "config_exists": os.path.exists(ctx.path("config.json")),
            "routes": [{"method": r[0], "path": r[1], "about": r[3]} for r in ROUTES],
            "disclaimer": DISCLAIMER}


def r_status(ctx, q, body):
    return market(ctx, verify=q.get("verify") in ("1", "true", "yes"))


def r_quotes(ctx, q, body):
    syms = _symbols(q.get("symbols"))
    if not syms:
        raise ApiError(400, "which symbols? /api/quotes?symbols=RELIANCE,TCS or ^NSEI")
    if len(syms) > MAX_SYMBOLS:
        raise ApiError(400, "at most %d symbols per request (free endpoints, one request each)" % MAX_SYMBOLS)
    qs, errors = quotes(ctx, syms)
    return {"quotes": quote_rows(qs, ctx.now_ist()), "errors": errors, "source": "yahoo"}


def r_indices(ctx, q, body):
    qs, errors = quotes(ctx, [s for s, _ in INDICES])
    names = dict(INDICES)
    rows = quote_rows(qs, ctx.now_ist())
    for r in rows:
        r["name"] = names.get(r["symbol"], r["symbol"])
    return {"indices": rows, "errors": errors}


# ---------------------------------------------------------------- routes: account

def _positions(ctx, cfg, pf, prices_doc, live):
    """Each open position marked at a price, and what closing it there would realise after charges,
    worked out by the paper broker's own apply_fill on a copy of the portfolio."""
    broker = skill("broker")
    risk = skill("risk")
    file_prices, times = risk.load_prices(prices_doc or {})
    rows = []
    for sym, p in sorted(pf["positions"].items()):
        qty, avg = int(p.get("qty", 0)), float(p.get("avg_price", 0))
        product = str(p.get("product") or "MIS").upper()
        price, src, when = None, None, None
        lq = live.get(sym)
        if lq:
            price, src, when = lq["price"], "Yahoo quote via trader-alerts (delayed)", lq.get("time")
        elif file_prices.get(sym):
            price, src = file_prices[sym], "trader/prices.json"
            t = times.get(sym)
            when = t.isoformat() if t else None
        row = {"symbol": sym, "qty": qty, "avg_price": avg, "product": product,
               "buy_charges": p.get("buy_charges", 0), "price": price, "price_source": src, "price_time": when}
        if price and qty > 0:
            ch = broker.side_charges("SELL", price, qty, cfg, product)
            sim = json.loads(json.dumps(pf))
            realised = broker.apply_fill(sim, sym, "SELL", qty, price, ch["total"], product)
            row.update(value=broker.round_paise(qty * price), gross_pnl=broker.round_paise((price - avg) * qty),
                       exit_charges=ch["total"], pnl_if_closed=realised)
            row["why"] = {
                "pnl_if_closed": why("(price - average) x qty - sell-side charges - this position's buy charges",
                                     {"price": price, "average": avg, "qty": qty, "sell charges": ch["total"],
                                      "buy charges": p.get("buy_charges", 0), "product": product},
                                     "trader-broker apply_fill and side_charges on a copy of the paper portfolio; "
                                     "price from " + src, when,
                                     "An estimate: the charges model is a typical discount broker's."),
                "exit_charges": why("brokerage + STT + exchange + SEBI + stamp + GST on the sell side",
                                    dict(ch), "trader-broker side_charges (round_trip_charges)", when)}
        rows.append(row)
    return rows


def account_view(ctx, live_quotes=True):
    broker = skill("broker")
    cfg = ctx.config()
    c = broker.merged_config(cfg)
    try:
        pf = broker.load_portfolio(ctx.path("portfolio.json"), cfg)
        state = broker.load_state(ctx.path("state", "broker.json"))
        trades = (broker.read_json(ctx.path("state", "trades.json"), "trades log", required=False) or {}
                  ).get("trades", [])
    except broker.TicketError as e:
        raise ApiError(422, str(e))
    today = ctx.today()
    live, errors = ({}, [])
    if live_quotes and pf["positions"]:
        live, errors = quotes(ctx, sorted(pf["positions"]))
    positions = _positions(ctx, cfg, pf, ctx.read("prices.json"), live)
    placed = broker.records(state, today=today, status="placed")
    entries = [r for r in placed if r.get("kind") == "entry"]
    at_risk = broker.round_paise(sum(broker._f(r.get("max_loss")) or 0 for r in entries))
    today_trades = [t for t in trades if t.get("date") == today]
    realised = broker.round_paise(sum(t.get("realised_pnl") or 0 for t in today_trades))
    charges = broker.round_paise(sum(t.get("charges") or 0 for t in today_trades))
    open_pnl = broker.round_paise(sum(p.get("pnl_if_closed") or 0 for p in positions))
    legs = [{"symbol": r["symbol"], "index": r.get("index"), "stop": r.get("stop"), "target": r.get("target"),
             "paper_order_id": r.get("paper_order_id")} for r in broker.open_legs(state, today)]
    now_s = ist_text(ctx.now_ist())
    return {
        "paper": True, "date": today, "cash": pf["cash"], "positions": positions, "working_legs": legs,
        "today": {"orders": len(placed), "max_orders": c["max_orders_per_day"], "trades": len(entries),
                  "max_trades": c["max_trades_per_day"], "at_risk": at_risk, "max_daily_loss": c["max_daily_loss"],
                  "realised": realised, "charges": charges, "open_pnl": open_pnl,
                  "pnl": broker.round_paise(realised + open_pnl)},
        "fills_today": today_trades, "orders_today": broker.records(state, today=today),
        "history": state.get("orders", [])[-50:], "quote_errors": errors,
        "why": {
            "cash": why("capital at the start, minus every paper buy and its charges, plus every sell after charges",
                        {"portfolio": "trader/portfolio.json"}, "trader-broker load_portfolio", now_s),
            "realised": why("sum of realised_pnl of today's paper fills (each after buy and sell charges)",
                            {"fills today": len(today_trades)}, "trader/state/trades.json (trader-broker fill)",
                            now_s),
            "open_pnl": why("sum over open positions of what closing at the marked price would realise, after "
                            "charges", {"positions": len(positions)},
                            "trader-broker apply_fill on a copy of the portfolio", now_s),
            "pnl": why("realised today + open P&L if closed now",
                       {"realised": realised, "open": open_pnl}, "trader-broker", now_s,
                       "Open P&L counts delivery (CNC) holdings from their buy price, not from yesterday's close."),
            "at_risk": why("sum of max_loss (loss at the stop + charges) of today's placed entries",
                           {"entries": len(entries), "limit": c["max_daily_loss"]},
                           "trader/state/broker.json (what trader-broker placed)", now_s),
        },
        "disclaimer": DISCLAIMER,
    }


def r_portfolio(ctx, q, body):
    return account_view(ctx, live_quotes=q.get("live", "1") not in ("0", "false"))


def tickets(ctx):
    broker = skill("broker")
    doc = ctx.read("orders.json")
    if doc is None:
        return {"orders_file": False, "tickets": [], "note": "no trader/orders.json yet (trader-budget writes it)"}
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list):
        raise ApiError(422, "trader/orders.json has no list of orders")
    cfg = ctx.config()
    risk_doc = ctx.read("state", "risk.json")
    state = broker.load_state(ctx.path("state", "broker.json"))
    today = ctx.today()
    out = []
    for i, o in enumerate(orders, 1):
        t = broker.ticket_json(o, i, cfg, risk_doc, state, today)
        t["explain"] = {"max_loss": why("qty x (entry - stop) + estimated round-trip charges at the stop",
                                    {"qty": o.get("qty"), "entry": broker.entry_ref(o), "stop": t["stop"],
                                     "loss at stop": t["loss_at_stop"], "charges": t["charges"]},
                                    "trader-broker loss_at_stop", ist_text(ctx.now_ist()))}
        out.append(t)
    return {"orders_file": True, "source": doc.get("source") if isinstance(doc, dict) else None,
            "created_at": doc.get("created_at") if isinstance(doc, dict) else None, "tickets": out,
            "risk_checked_at": (risk_doc or {}).get("checked_at"), "rule": "One order at a time: a yes covers "
            "one ticket, never the batch. Paper account only."}


def r_orders(ctx, q, body):
    return tickets(ctx)


def _int(body, key, required=True):
    v = (body or {}).get(key)
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(400, "%s must be a whole number" % key)
    return v


def _broker_paths(ctx):
    return ["--orders", ctx.path("orders.json"), "--config", ctx.path("config.json"),
            "--risk", ctx.path("state", "risk.json"), "--state", ctx.path("state", "broker.json")]


def _command_result(ctx, rc, out, err):
    ctx.forget("quotes")
    return {"ok": rc == 0, "exit_code": rc, "output": out.strip(), "error": err.strip(), "paper": True}


def r_confirm(ctx, q, body):
    idx = _int(body, "index")
    text = (body or {}).get("yes_text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "type your reply (yes_text): the paper broker records it word for word")
    if len(text) > 500:
        raise ApiError(400, "yes_text is too long")
    rc, out, err = run_skill(ctx, "broker", ["confirm"] + _broker_paths(ctx) + ["--index", str(idx),
                                                                                "--yes-text", text]
                             + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["tickets"] = tickets(ctx)["tickets"]
    return res


def r_decline(ctx, q, body):
    idx = _int(body, "index")
    reply = (body or {}).get("reply") or "no"
    if not isinstance(reply, str) or len(reply) > 500:
        raise ApiError(400, "reply must be short text")
    rc, out, err = run_skill(ctx, "broker", ["decline"] + _broker_paths(ctx) + ["--index", str(idx),
                                                                                "--reply", reply]
                             + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["tickets"] = tickets(ctx)["tickets"]
    return res


def r_fill(ctx, q, body):
    idx = _int(body, "index")
    leg = (body or {}).get("leg") or "order"
    if leg not in ("order", "stop", "target"):
        raise ApiError(400, "leg is order, stop or target")
    argv = ["fill", "--index", str(idx), "--leg", leg, "--orders", ctx.path("orders.json"),
            "--prices", ctx.path("prices.json"), "--portfolio", ctx.path("portfolio.json"),
            "--trades", ctx.path("state", "trades.json"), "--config", ctx.path("config.json"),
            "--state", ctx.path("state", "broker.json")]
    price = (body or {}).get("price")
    if price not in (None, ""):
        if isinstance(price, bool) or not isinstance(price, (int, float, str)):
            raise ApiError(400, "price must be a number")
        try:
            if float(price) <= 0:
                raise ValueError
        except ValueError:
            raise ApiError(400, "price must be a positive number")
        argv += ["--price", str(price)]
    rc, out, err = run_skill(ctx, "broker", argv + ctx.now_args())
    return _command_result(ctx, rc, out, err)


def r_refresh_prices(ctx, q, body):
    """trader-alerts quote --holdings --write: fresh prices for the risk check and paper fills."""
    argv = ["quote", "--holdings", "--json", "--portfolio", ctx.path("portfolio.json"),
            "--plan", ctx.path("plan.json"), "--write", ctx.path("prices.json")]
    if ctx.offline:
        argv += ["--offline", ctx.offline]
    rc, out, err = run_skill(ctx, "alerts", argv + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    try:
        res["result"] = json.loads(out) if out.strip().startswith("{") else None
    except ValueError:
        res["result"] = None
    res["output"] = "" if res["result"] else res["output"]
    return res


# ---------------------------------------------------------------- routes: risk

def r_risk(ctx, q, body):
    risk = skill("risk")
    cfg = ctx.config()
    c = risk.merged_config(cfg)
    last = ctx.read("state", "risk.json")
    preview, err = None, None
    doc = ctx.read("orders.json")
    if doc is not None:
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        try:
            pdoc = ctx.read("prices.json") or {}
            prices, times = risk.load_prices(pdoc)
            preview = risk.check(orders, cfg, ctx.read("portfolio.json"), prices, ctx.read("state", "broker.json"),
                                 ctx.today(), risk.parse_now(ctx.now_ist().strftime("%Y-%m-%d %H:%M:%S")), times)
            if pdoc.get("as_of"):
                preview["prices_as_of"] = pdoc["as_of"]
        except risk.RiskError as e:
            err = str(e)
    limits = [{"key": k, "value": c.get(k)} for k in (
        "capital", "max_loss_per_trade", "max_daily_loss", "max_trades_per_day", "max_orders_per_day",
        "max_position_pct", "max_order_value", "min_order_value", "cash_buffer_pct", "min_reward_risk",
        "require_stop", "no_entries_before", "no_entries_after", "flat_by", "include_charges", "allow_short",
        "allow_fno", "paper")]
    now_s = ist_text(ctx.now_ist())
    return {"limits": limits, "last_check": last, "preview": preview, "preview_error": err,
            "stale": bool(last) and last.get("date") != ctx.today(),
            "why": {"verdict": why("each order against every rule in order, as if each PASS had filled: format, "
                                   "tick, exchange, F&O, short; for entries also size, position, cash, stop, "
                                   "target, loss at stop + charges, reward:risk, daily loss, trades, window, "
                                   "duplicates, daily order limit",
                                   {"orders": "trader/orders.json", "config": "trader/config.json",
                                    "portfolio": "trader/portfolio.json", "prices": "trader/prices.json",
                                    "placed today": "trader/state/broker.json"},
                                   "trader-risk check (pure function, no network)", now_s,
                                   "The preview is not recorded. The paper broker only accepts a PASS that was "
                                   "recorded today: use Run risk check.")},
            "disclaimer": DISCLAIMER}


def r_risk_check(ctx, q, body):
    if ctx.read("orders.json") is None:
        raise ApiError(409, "no trader/orders.json to check (trader-budget writes it)")
    argv = ["--orders", ctx.path("orders.json"), "--config", ctx.path("config.json"),
            "--portfolio", ctx.path("portfolio.json"), "--prices", ctx.path("prices.json"),
            "--state", ctx.path("state", "broker.json"), "--out", ctx.path("state", "risk.json"), "--json"]
    rc, out, err = run_skill(ctx, "risk", argv + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["ok"] = rc in (0, 1) and out.strip().startswith("{")
    try:
        res["result"] = json.loads(out) if res["ok"] else None
    except ValueError:
        res["ok"], res["result"] = False, None
    if res["result"]:
        res["output"] = ""
    return res


# ---------------------------------------------------------------- routes: chart

def ema(values, n):
    """Exponential moving average: the first value is the simple average of the first n closes,
    then each one is close x k + previous x (1 - k), k = 2 / (n + 1). None until n closes exist."""
    out, prev = [], None
    k = 2.0 / (n + 1)
    for i, v in enumerate(values):
        if i + 1 < n:
            out.append(None)
            continue
        if prev is None:
            prev = sum(values[i + 1 - n:i + 1]) / float(n)
        else:
            prev = v * k + prev * (1 - k)
        out.append(round(prev, 2))
    return out


def vwap(bars):
    """Session VWAP: sum(typical x volume) / sum(volume) from each IST day's first bar, typical =
    (high + low + close) / 3. Yahoo's NSE 09:15 bar has no volume, so the line starts at the first
    bar with one."""
    out, day, pv, vol = [], None, 0.0, 0
    for b in bars:
        d = datetime.datetime.fromtimestamp(b["t"], IST).date()
        if d != day:
            day, pv, vol = d, 0.0, 0
        if b["v"] > 0:
            pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            vol += b["v"]
        out.append(round(pv / vol, 2) if vol else None)
    return out


def _chart_text(ctx, sym, interval):
    if ctx.offline:
        for name in ("chart-%s-%s.json" % (sym, interval), "chart-%s-%s.json.gz" % (sym, interval)):
            p = os.path.join(ctx.offline, name)
            if os.path.exists(p):
                return skill("entry").read_text(p)
        raise ApiError(404, "no offline chart for %s %s in %s" % (sym, interval, ctx.offline))
    scanner = skill("scanner")
    rng = "6mo" if interval == "1d" else "1d"
    text = scanner.yahoo_get(scanner.chart_path(sym, rng, interval))
    if interval != "1d":
        ch = skill("entry").parse_chart(text, sym)
        if not ch["bars"]:          # before the open Yahoo's 1d range is empty: show the last session
            text = scanner.yahoo_get(scanner.chart_path(sym, "5d", interval))
    return text


def chart_view(ctx, sym, interval, ema_lengths=(9, 21)):
    entry = skill("entry")
    alerts = skill("alerts")
    try:
        sym = alerts.normalise(sym)
    except alerts.AlertError as e:
        raise ApiError(400, str(e))
    if interval not in INTERVALS:
        raise ApiError(400, "interval is 1m, 5m or 1d")
    key = ("chart", sym, interval)
    text = ctx.cached(key, TTL["chart-" + interval], lambda: _chart_text(ctx, sym, interval))
    ch = entry.parse_chart(text, sym)
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise ApiError(502, "the chart for %s has %s bars, not %s" % (sym, gran, interval))
    now_utc = ctx.now_utc()
    now_ts = entry.ts_of(now_utc)
    step = INTERVALS[interval]
    bars = [b for b in ch["bars"] if (b["t"] % step == 0 or interval == "1d") and b["t"] < now_ts]
    if interval != "1d" and bars:
        last_day = datetime.datetime.fromtimestamp(bars[-1]["t"], IST).date()
        bars = [b for b in bars if datetime.datetime.fromtimestamp(b["t"], IST).date() == last_day]
    closes = [b["c"] for b in bars]
    out = {"symbol": sym, "interval": interval, "source": "Yahoo Finance chart (free, delayed, unofficial)",
           "data_time": None, "bars": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"],
                                        "close": b["c"], "volume": b["v"]} for b in bars],
           "ema": {}, "vwap": [], "opening_range": None, "entry": None, "plan": None, "why": {}}
    if ch.get("data_time"):
        out["data_time"] = alerts.ist_iso(entry.utc_of(ch["data_time"]))
    as_of = out["data_time"]
    for n in ema_lengths:
        vals = ema(closes, n)
        out["ema"][str(n)] = [{"time": b["t"], "value": v} for b, v in zip(bars, vals) if v is not None]
        out["why"]["ema%d" % n] = why("EMA(%d): first = average of the first %d closes; then close x k + "
                                      "previous EMA x (1 - k), k = 2 / (%d + 1)" % (n, n, n),
                                      {"closes": len(closes), "last close": closes[-1] if closes else None,
                                       "last EMA": vals[-1] if vals else None},
                                      "computed by the UI from the chart's closes (an overlay; no skill decision "
                                      "uses it)", as_of)
    if interval != "1d":
        vw = vwap(bars)
        out["vwap"] = [{"time": b["t"], "value": v} for b, v in zip(bars, vw) if v is not None]
        out["why"]["vwap"] = why("session VWAP = sum((high + low + close) / 3 x volume) / sum(volume) since the "
                                 "session's first bar with volume",
                                 {"bars": len(bars), "volume": sum(b["v"] for b in bars),
                                  "last VWAP": vw[-1] if vw else None},
                                 "computed by the UI from the chart's bars (Yahoo's 09:15 bar has no volume)",
                                 as_of)
        if bars:
            # the opening range and the entry status come from trader-entry itself, at the end of the session
            # shown (or now, while it is live)
            last_end = bars[-1]["t"] + step
            at = min(now_ts, max(last_end, ch.get("data_time") or 0))
            res = entry.evaluate(dict(ch, bars=[b for b in ch["bars"] if b["t"] < now_ts]),
                                 entry.utc_of(at), interval=interval, cfg=entry.read_config(ctx.path("config.json")))
            pub = entry.public(res)
            out["entry"] = {k: pub.get(k) for k in ("status", "phase", "reason", "level", "trigger", "date",
                                                       "no_entries_before", "no_entries_after", "now")}
            orr = pub.get("opening_range")
            if orr:
                out["opening_range"] = orr
                out["why"]["opening_range"] = why(
                    "high and low of the first %d minutes (%d bars) of the session" % (orr["minutes"], orr["of"]),
                    {"high": orr["high"], "low": orr["low"], "bars": orr["bars"], "date": pub.get("date")},
                    "trader-entry evaluate", pub.get("now"))
    plan = ctx.read("plan.json")
    if isinstance(plan, dict) and str(plan.get("symbol", "")).upper() == sym:
        out["plan"] = {k: plan.get(k) for k in ("date", "side", "setup", "entry", "entry_time", "stop", "stop_label",
                                                 "target", "target_label", "reward_risk", "flat_by")}
        out["why"]["plan"] = why("entry, stop and target from trader-exit's plan for this symbol",
                                 {"entry": plan.get("entry"), "stop": plan.get("stop"), "target": plan.get("target"),
                                  "stop rule": plan.get("stop_label"), "target rule": plan.get("target_label")},
                                 "trader/plan.json (trader-exit)", plan.get("created_at"))
    return out


def r_chart(ctx, q, body):
    sym = q.get("symbol") or ""
    interval = q.get("interval") or "5m"
    lens = []
    for part in str(q.get("ema") or "9,21").split(","):
        if part.strip():
            try:
                n = int(part)
            except ValueError:
                raise ApiError(400, "ema wants lengths like 9,21")
            if not 2 <= n <= 200:
                raise ApiError(400, "an EMA length is 2 to 200")
            lens.append(n)
    if not sym:
        raise ApiError(400, "which symbol? /api/chart?symbol=RELIANCE&interval=5m")
    return chart_view(ctx, sym, interval, tuple(lens[:3]))


# ---------------------------------------------------------------- routes: scanner, plan, alerts

def r_scanner(ctx, q, body):
    doc = ctx.read("watchlist.json")
    if not isinstance(doc, dict):
        return {"exists": False, "hits": [], "note": "no trader/watchlist.json yet: run trader-scanner "
                                                     "(scan.py daily or scan.py orb)"}
    labels = dict(skill("scanner").SETUPS)
    hits = []
    for h in doc.get("hits") or []:
        row = dict(h)
        row["setup_labels"] = [labels.get(t, t) for t in h.get("setups") or []]
        if h.get("turnover") is not None:
            row["why"] = why("median of the last 20 sessions' close x volume" if h.get("turnover_basis") ==
                             "median 20d" else "the last session's close x volume",
                             {"basis": h.get("turnover_basis"), "volume": h.get("volume"), "price": h.get("price")},
                             "trader-scanner scan_daily", doc.get("created_at"))
        hits.append(row)
    return dict(doc, hits=hits, exists=True)


def r_plan(ctx, q, body):
    plan = ctx.read("plan.json")
    return {"exists": plan is not None, "plan": plan}


def r_alerts(ctx, q, body):
    try:
        return rings(ctx)
    except skill("alerts").AlertError as e:
        raise ApiError(422, str(e))


def r_dashboard(ctx, q, body):
    st = market(ctx, verify=q.get("verify") in ("1", "true", "yes"))
    idx = r_indices(ctx, {}, None)
    acct = account_view(ctx)
    plan = ctx.read("plan.json")
    risk_doc = ctx.read("state", "risk.json")
    wl = ctx.read("watchlist.json")
    try:
        al = rings(ctx)
    except skill("alerts").AlertError as e:
        al = {"rules": [], "rings": [], "errors": [str(e)]}
    return {"market": st, "indices": idx["indices"], "index_errors": idx["errors"],
            "account": {"cash": acct["cash"], "today": acct["today"], "positions": len(acct["positions"]),
                        "working_legs": acct["working_legs"], "why": acct["why"]},
            "plan": plan, "alerts": al,
            "risk": {"date": (risk_doc or {}).get("date"), "pass": (risk_doc or {}).get("pass"),
                     "block": (risk_doc or {}).get("block"), "checked_at": (risk_doc or {}).get("checked_at"),
                     "stale": bool(risk_doc) and risk_doc.get("date") != ctx.today()} if risk_doc else None,
            "watchlist": {"hits": len((wl or {}).get("hits") or []), "as_of": (wl or {}).get("as_of"),
                          "mode": (wl or {}).get("mode")} if isinstance(wl, dict) else None,
            "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------- routes: settings

def known_keys():
    keys = {}
    for name in ("risk", "budget", "scanner", "entry"):
        for k, v in skill(name).DEFAULTS.items():
            keys.setdefault(k, v)
    keys.setdefault("insider_watch_clients", [])
    return keys


def _plain(text):
    """Markdown table cell -> plain words: drop backticks and keep a link's text."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text.replace("`", "").strip()


def config_docs():
    """{key: (default text, meaning)} from docs/configuration.md, so the settings page shows the same words."""
    out = {}
    p = os.path.join(ROOT, "docs", "configuration.md")
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\| (`[^|]+`) \| ([^|]*) \| (.*) \|\s*$", line)
            if not m:
                continue
            for k in re.findall(r"`(\w+)`", m.group(1)):
                out[k] = {"default": _plain(m.group(2)), "meaning": _plain(m.group(3))}
    return out


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_config(doc):
    """(errors, warnings) for a trader/config.json, using the skills' own parsers where they have one."""
    errors, warnings = [], []
    if not isinstance(doc, dict):
        return ["the config must be a JSON object ({...})"], []
    keys = known_keys()
    risk, alerts, broker = skill("risk"), skill("alerts"), skill("broker")
    for k, v in doc.items():
        if k not in keys:
            warnings.append("%s is not read by any tool in this version (kept as it is)" % k)
            continue
        d = keys[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                errors.append("%s must be true or false" % k)
        elif _is_num(d):
            if not _is_num(v):
                errors.append("%s must be a number" % k)
            elif v < 0:
                errors.append("%s cannot be negative" % k)
            elif k in PCT_KEYS and v > 100:
                errors.append("%s is a percentage (0 to 100)" % k)
        elif k == "tick_size":
            if not (v == "auto" or (_is_num(v) and v > 0)):
                errors.append("tick_size is \"auto\" or a positive number like 0.05")
        elif k in HHMM_KEYS:
            try:
                if risk.hhmm(v, k) is None:
                    errors.append("%s needs an IST time like \"09:30\"" % k)
            except risk.RiskError as e:
                errors.append(str(e))
        elif isinstance(d, str):
            if not isinstance(v, str):
                errors.append("%s must be text" % k)
        elif isinstance(d, list):
            if not isinstance(v, list):
                errors.append("%s must be a list [...]" % k)
        elif isinstance(d, dict):
            if not isinstance(v, dict):
                errors.append("%s must be an object {...}" % k)
    if _is_num(doc.get("capital")) and doc["capital"] <= 0:
        errors.append("capital must be above 0")
    if "product" in doc and str(doc["product"]).upper() not in ("MIS", "CNC"):
        errors.append("product is MIS (intraday) or CNC (delivery)")
    if "exchange" in doc and str(doc["exchange"]).upper() != "NSE":
        errors.append("exchange is NSE (this version trades the NSE cash market)")
    try:
        broker.paper_only(doc)
    except broker.TicketError as e:
        errors.append(str(e))
    if isinstance(doc.get("blocked"), list):
        for s in doc["blocked"]:
            if not isinstance(s, str) or not risk.TICKER.match(s.strip().upper()):
                errors.append("blocked: %r is not an NSE symbol" % (s,))
    if isinstance(doc.get("charges"), dict):
        known = risk.CHARGES_DEFAULTS
        for k, v in doc["charges"].items():
            if k not in known:
                warnings.append("charges.%s is not part of the charges model (kept as it is)" % k)
            elif not _is_num(v) or v < 0:
                errors.append("charges.%s must be a number, 0 or more" % k)
    if not errors:
        try:
            alerts.Calendar(doc)
        except alerts.AlertError as e:
            errors.append(str(e))
    if not errors:
        c = risk.merged_config(doc)
        b, a, f = (risk.hhmm(c.get(k), k) for k in HHMM_KEYS)
        if b and a and b >= a:
            errors.append("no_entries_before (%s) must be earlier than no_entries_after (%s)"
                          % (c["no_entries_before"], c["no_entries_after"]))
        if a and f and f < a:
            warnings.append("flat_by (%s) is before no_entries_after (%s): late entries would have to close "
                            "almost at once" % (c["flat_by"], c["no_entries_after"]))
        if _is_num(c.get("max_loss_per_trade")) and _is_num(c.get("max_daily_loss")) and \
                c["max_loss_per_trade"] > c["max_daily_loss"]:
            warnings.append("max_loss_per_trade is above max_daily_loss: one stop-out would end the day")
        if _is_num(c.get("min_order_value")) and _is_num(c.get("max_order_value")) and \
                c["min_order_value"] > c["max_order_value"]:
            errors.append("min_order_value is above max_order_value: no order could pass")
    return errors, warnings


def _summary(doc):
    budget = skill("budget")
    try:
        return budget.describe_config(budget.merged_config(doc))
    except (KeyError, TypeError, ValueError):
        return None


def r_config(ctx, q, body):
    raw = ctx.read("config.json")
    exists = raw is not None
    doc = raw if isinstance(raw, dict) else {}
    errors, warnings = validate_config(doc) if exists else ([], [])
    docs = config_docs()
    keys = known_keys()
    schema = []
    for k in sorted(keys):
        d = keys[k]
        kind = ("bool" if isinstance(d, bool) else "number" if _is_num(d) else "list" if isinstance(d, list)
                else "object" if isinstance(d, dict) else "text")
        schema.append({"key": k, "type": kind, "default": d, "doc": docs.get(k, {}).get("meaning")})
    return {"path": ctx.path("config.json"), "exists": exists, "config": doc,
            "effective": skill("risk").merged_config(doc), "errors": errors, "warnings": warnings,
            "summary": _summary(doc) if exists and not errors else None, "schema": schema,
            "help": "Every key is optional: a missing key uses its default. Paper trading stays on in this "
                    "version. Create one with: python3 trader-budget/size.py init"}


def _body_config(body):
    if not isinstance(body, dict) or "config" not in body:
        raise ApiError(400, "send {\"config\": {...}}")
    return body["config"]


def r_config_validate(ctx, q, body):
    doc = _body_config(body)
    errors, warnings = validate_config(doc)
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "summary": _summary(doc) if not errors else None}


def r_config_save(ctx, q, body):
    doc = _body_config(body)
    errors, warnings = validate_config(doc)
    if errors:
        return {"ok": False, "saved": False, "errors": errors, "warnings": warnings, "summary": None}
    p = ctx.path("config.json")
    with ctx.cmd_lock:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as src, open(p + ".bak", "w", encoding="utf-8") as dst:
                dst.write(src.read())
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, p)
    return {"ok": True, "saved": True, "path": p, "backup": p + ".bak" if os.path.exists(p + ".bak") else None,
            "errors": [], "warnings": warnings, "summary": _summary(doc)}


# ---------------------------------------------------------------- routes: coming next

def r_options(ctx, q, body):
    return {"available": False, "coming": "v0.2.0", "skill": "trader-options",
            "title": "Option chain",
            "will_show": ["NSE option chain by expiry, with lot sizes from NSE's daily file",
                          "open interest and change in OI as bars per strike; call and put walls",
                          "PCR by OI and by volume, max pain, expected move",
                          "the IV smile: Black-76 implied volatility off the put-call-parity forward",
                          "Greeks per strike, and IV rank from India VIX history"],
            "reads": "trader/options.json (written by trader-options)"}


def r_strategy(ctx, q, body):
    return {"available": False, "coming": "v0.2.0", "skill": "trader-strategy",
            "title": "Strategy builder",
            "will_show": ["a legs editor: spreads, straddles, strangles, butterflies, condors, iron flies, ratios",
                          "the payoff at expiry and today, gross and after charges",
                          "breakevens, max profit and max loss, the STT-on-exercise warning",
                          "position Greeks and a margin estimate with honest error bounds",
                          "probability of profit: textbook and simulated"],
            "reads": "the strategy file written by trader-strategy"}


# ---------------------------------------------------------------- the table

# (method, path, handler, what it does, writes?)
ROUTES = [
    ("GET", "/api/meta", r_meta, "app version, workdir, offline mode, the clock, this route list", False),
    ("GET", "/api/status", r_status, "NSE open or closed, the session, the next open (?verify=1 asks NSE)", False),
    ("GET", "/api/dashboard", r_dashboard, "market, indices, today's paper P&L, plan, alerts, risk summary", False),
    ("GET", "/api/indices", r_indices, "NIFTY 50, BANK NIFTY, INDIA VIX quotes", False),
    ("GET", "/api/quotes", r_quotes, "delayed quotes: ?symbols=RELIANCE,TCS", False),
    ("GET", "/api/chart", r_chart, "bars, volume, EMA, VWAP, opening range, plan levels: ?symbol=&interval=", False),
    ("GET", "/api/scanner", r_scanner, "trader/watchlist.json from trader-scanner", False),
    ("GET", "/api/plan", r_plan, "trader/plan.json from trader-exit", False),
    ("GET", "/api/alerts", r_alerts, "alert rules and what would ring now (dry run)", False),
    ("GET", "/api/portfolio", r_portfolio, "paper cash, positions, working legs, fills, today's limits", False),
    ("GET", "/api/orders", r_orders, "order tickets with their trader-risk status", False),
    ("GET", "/api/risk", r_risk, "your limits, the recorded verdicts and a fresh preview", False),
    ("GET", "/api/config", r_config, "trader/config.json, the effective settings and their meaning", False),
    ("GET", "/api/options", r_options, "option chain (coming in v0.2)", False),
    ("GET", "/api/strategy", r_strategy, "strategy builder (coming in v0.2)", False),
    ("POST", "/api/risk/check", r_risk_check, "run trader-risk on trader/orders.json and record the verdicts", True),
    ("POST", "/api/prices/refresh", r_refresh_prices, "trader-alerts quote --holdings --write", True),
    ("POST", "/api/orders/confirm", r_confirm, "record your typed yes for ONE order (paper broker)", True),
    ("POST", "/api/orders/decline", r_decline, "record a no for one order", True),
    ("POST", "/api/orders/fill", r_fill, "simulate a paper fill (entry, stop or target)", True),
    ("POST", "/api/config/validate", r_config_validate, "check a config without saving it", True),
    ("POST", "/api/config", r_config_save, "validate and save trader/config.json (keeps a .bak)", True),
]
_TABLE = {(m, p): (fn, w) for m, p, fn, _, w in ROUTES}
STREAM_PATH = "/api/stream"


def find(method, path):
    return _TABLE.get((method, path))


def dispatch(ctx, method, path, query=None, body=None):
    """(HTTP status, JSON-ready body). Unknown route = 404; a skill's own error = its message."""
    hit = _TABLE.get((method, path))
    if hit is None:
        if any(p == path for _, p, _, _, _ in ROUTES):
            return 405, {"error": "method not allowed"}
        return 404, {"error": "no such route: %s" % path}
    fn, _ = hit
    try:
        return 200, fn(ctx, query or {}, body)
    except ApiError as e:
        return e.status, {"error": e.message}
    except skill_errors() as e:
        return 502 if "Yahoo" in str(e) or "reach" in str(e) else 422, {"error": str(e)}


# ---------------------------------------------------------------- live stream

class Poller(object):
    """One background thread for every browser tab: market status, prices and alerts, polled politely
    and only while someone is listening. Each new snapshot wakes the listeners."""

    def __init__(self, ctx, every_open=15, every_closed=120):
        self.ctx = ctx
        self.every_open, self.every_closed = every_open, every_closed
        self.cond = threading.Condition()
        self.version = 0
        self.snapshot = {}
        self.listeners = 0
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ui-poller", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        with self.cond:
            self.cond.notify_all()

    def subscribe(self):
        with self.cond:
            self.listeners += 1
            self.cond.notify_all()

    def unsubscribe(self):
        with self.cond:
            self.listeners = max(0, self.listeners - 1)

    def poll_once(self):
        ctx = self.ctx
        snap = {}
        try:
            st = market(ctx)
            snap["status"] = {k: st.get(k) for k in ("now_ist", "date", "weekday", "open", "phase", "reason",
                                                      "opens", "closes", "next_open", "special_session",
                                                      "warning")}
        except Exception as e:  # keep the stream alive whatever one source does
            snap["status"] = {"error": str(e)}
            st = {"open": False}
        syms = [s for s, _ in INDICES] + watched_symbols(ctx)
        try:
            qs, errors = quotes(ctx, syms)
            snap["prices"] = {"quotes": quote_rows(qs, ctx.now_ist()), "errors": errors}
        except Exception as e:
            qs, snap["prices"] = {}, {"quotes": [], "errors": [str(e)]}
        try:
            al = rings(ctx, qs)
            snap["alerts"] = {"rings": al["rings"], "rules": len(al["rules"]), "checked_at": al.get("checked_at")}
        except Exception as e:
            snap["alerts"] = {"rings": [], "error": str(e)}
        return snap, bool(st.get("open"))

    def _run(self):
        while not self.stop.is_set():
            with self.cond:
                while self.listeners == 0 and not self.stop.is_set():
                    self.cond.wait(5)
            if self.stop.is_set():
                break
            snap, is_open = self.poll_once()
            with self.cond:
                self.snapshot = snap
                self.version += 1
                self.cond.notify_all()
            wait = 30 if self.ctx.offline else (self.every_open if is_open else self.every_closed)
            self.stop.wait(wait)

    def wait(self, seen, timeout):
        """(version, snapshot) once there is something newer than `seen`, or after timeout."""
        with self.cond:
            if self.version <= seen and not self.stop.is_set():
                self.cond.wait(timeout)
            return self.version, self.snapshot
