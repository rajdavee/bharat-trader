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
import ipaddress
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VERSION = "0.2.0"
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
    "options": ("trader-options", "options"),
    "strategy": ("trader-strategy", "strategy"),
    "doctor": ("trader-doctor", "doctor"),
    "regime": ("trader-regime", "regime"),
}
# how long a live answer is reused, in seconds (free endpoints: be polite)
TTL = {"quotes": 15, "nse": 120, "chart-1m": 30, "chart-5m": 60, "chart-1d": 900, "history": 900}
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
                      ("broker", "TicketError"), ("budget", "SizeError"), ("scanner", "ScanError"),
                      ("options", "OptionsError"), ("strategy", "StrategyError")):
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
    fno = fno_view(ctx, pf)
    return {
        "paper": True, "date": today, "cash": pf["cash"], "positions": positions, "working_legs": legs,
        "margin_blocked": pf.get("margin_blocked") or 0.0, "free_cash": broker.free_cash(pf), "fno": fno,
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


def fno_view(ctx, pf=None):
    """The paper F&O book marked to trader/options.json (trader-broker mtm_view): positions grouped by strategy,
    P&L, Black-76 Greeks, margin blocked and the payoff at expiry of each open structure."""
    broker = skill("broker")
    if pf is None:
        try:
            pf = broker.load_portfolio(ctx.path("portfolio.json"), ctx.config())
        except broker.TicketError as e:
            raise ApiError(422, str(e))
    markets = broker.load_markets([ctx.path("options.json")])
    v = broker.mtm_view(pf, markets, ctx.now_ist(), ctx.config())
    v["why"] = {
        "unrealised": why("sum over open F&O positions of (mark - average price) x signed units",
                          {"positions": len(v["positions"])}, "trader-broker mtm_view; marks from trader/options.json",
                          v["as_of"], "Marks are the chain's mid (futures: its parity forward), not a trade."),
        "margin": why("per strategy: trader-strategy's SPAN-style estimate less the premium paid, or trader-broker's "
                      "own estimate after an adjustment", {"blocked": v["totals"]["margin_blocked"]},
                      "trader/portfolio.json (the paper ledger)", v["as_of"],
                      "An estimate of what a broker blocks: check your broker's basket margin."),
        "greeks": why("Black-76 on the forward at each leg's IV, x signed units", {"from": v["greeks_from"]},
                      "trader-options black76_greeks", v["as_of"])}
    return v


def r_fno(ctx, q, body):
    return fno_view(ctx)


def r_fno_adjust(ctx, q, body):
    """Write an adjustment PROPOSAL (ticket.py adjust) to trader/orders.json. It places nothing: the ticket
    still needs trader-risk and your typed yes."""
    skill("broker").paper_only(ctx.config(), "the local UI's adjust")
    body = body or {}
    sid = body.get("strategy")
    if not isinstance(sid, str) or not re.match(r"^S-\d{8}-\d{1,4}$", sid):
        raise ApiError(400, "strategy must be an id like S-20261102-1")
    action = body.get("action")
    argv = ["adjust", "--strategy", sid, "--orders", ctx.path("orders.json"), "--portfolio", ctx.path("portfolio.json"),
            "--config", ctx.path("config.json"), "--options", ctx.path("options.json")]
    if action == "close-all":
        argv.append("--close-all")
    elif action in ("close-leg", "roll-leg"):
        argv += ["--" + action, str(_int(body, "leg"))]
        if action == "roll-leg":
            k = body.get("strike")
            if isinstance(k, bool) or not isinstance(k, (int, float)) or k <= 0:
                raise ApiError(400, "a roll needs the new strike (a positive number)")
            argv += ["--strike", str(k)]
            exp = body.get("expiry")
            if exp:
                if not isinstance(exp, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", exp):
                    raise ApiError(400, "expiry is YYYY-MM-DD")
                argv += ["--expiry", exp]
    elif action == "add-hedge":
        hedge = body.get("hedge")
        if not isinstance(hedge, str) or len(hedge) > 60:
            raise ApiError(400, "hedge is short text like \"BUY 1 PE 22500\"")
        argv += ["--add-hedge", hedge]
    else:
        raise ApiError(400, "action is close-leg, roll-leg, add-hedge or close-all")
    rc, out, err = run_skill(ctx, "broker", argv + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["placed"] = False
    return res


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
    if orders and broker.is_basket_doc(doc, orders):
        try:
            pf = broker.load_portfolio(ctx.path("portfolio.json"), cfg)
            view = broker.basket_view(doc, orders, cfg, risk_doc, state, pf, ctx.read("strategy.json"),
                                      broker.load_markets([ctx.path("options.json")]), ctx.now_ist(), today)
        except broker.TicketError as e:
            raise ApiError(422, str(e))
        view["text"] = broker.render_basket(view)
        return {"orders_file": True, "source": doc.get("source") if isinstance(doc, dict) else None,
                "created_at": doc.get("created_at") if isinstance(doc, dict) else None, "tickets": [],
                "basket": view, "risk_checked_at": (risk_doc or {}).get("checked_at"),
                "rule": "A multi-leg basket is ONE ticket: your typed yes covers every leg, all or nothing. Paper "
                        "account only."}
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


def _fno_paths(ctx):
    return ["--portfolio", ctx.path("portfolio.json"), "--options", ctx.path("options.json"),
            "--strategy", ctx.path("strategy.json")]


def r_confirm(ctx, q, body):
    skill("broker").paper_only(ctx.config(), "the local UI's confirm")
    basket = (body or {}).get("basket") is True
    idx = None if basket else _int(body, "index")
    text = (body or {}).get("yes_text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "type your reply (yes_text): the paper broker records it word for word")
    if len(text) > 500:
        raise ApiError(400, "yes_text is too long")
    if basket:
        bid = (body or {}).get("basket_id")
        if not isinstance(bid, str) or not re.match(r"^[0-9a-f]{16}$", bid):
            raise ApiError(400, "basket_id (16 hex characters, from the basket ticket) is needed with basket")
        which = ["--basket", "--basket-id", bid] + _fno_paths(ctx)
    else:
        which = ["--index", str(idx)]
    rc, out, err = run_skill(ctx, "broker", ["confirm"] + _broker_paths(ctx) + which + ["--yes-text", text]
                             + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["tickets"] = tickets(ctx)["tickets"]
    return res


def r_decline(ctx, q, body):
    which = ["--basket"] if (body or {}).get("basket") is True else ["--index", str(_int(body, "index"))]
    reply = (body or {}).get("reply") or "no"
    if not isinstance(reply, str) or len(reply) > 500:
        raise ApiError(400, "reply must be short text")
    rc, out, err = run_skill(ctx, "broker", ["decline"] + _broker_paths(ctx) + which + ["--reply", reply]
                             + ctx.now_args())
    res = _command_result(ctx, rc, out, err)
    res["tickets"] = tickets(ctx)["tickets"]
    return res


def r_fill(ctx, q, body):
    if (body or {}).get("basket") is True:
        argv = ["fill", "--basket", "--orders", ctx.path("orders.json"), "--prices", ctx.path("prices.json"),
                "--portfolio", ctx.path("portfolio.json"), "--trades", ctx.path("state", "trades.json"),
                "--config", ctx.path("config.json"), "--state", ctx.path("state", "broker.json"),
                "--options", ctx.path("options.json"), "--strategy", ctx.path("strategy.json"),
                "--bands", ctx.path("bands.csv")]
        rc, out, err = run_skill(ctx, "broker", argv + ctx.now_args())
        return _command_result(ctx, rc, out, err)
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


# ---------------------------------------------------------------- routes: live broker (read only)

def broker_status(ctx):
    """Which broker the config names, and for each live broker: which BT_* variables are set (never their
    values), the capabilities and plans, and the last preflight. No network."""
    b = skill("broker")
    cfg = ctx.config()
    try:
        mode, live = b.mode_of(cfg)
        err = None
    except b.TicketError as e:
        mode, live, err = "invalid", None, str(e)
    named = b.broker_name(cfg.get("broker"))
    pre = ctx.read("state", "preflight.json")
    pre = pre if isinstance(pre, dict) else {}
    le = cfg.get("live_enabled") if isinstance(cfg.get("live_enabled"), dict) else {}
    rows = []
    for name in b.LIVE_BROKERS:
        ad = b.get_adapter(name, cfg)
        p = pre.get(name) if isinstance(pre.get(name), dict) else None
        rows.append({"broker": name, "title": ad.title, "configured": name == named,
                     "live_enabled": le.get(name) is True, "credentials": ad.credentials_status(),
                     "token": bool(ad.token()), "token_cutoff": ad.token_cutoff, "capabilities": ad.caps(),
                     "plans": ad.plans,
                     "preflight": {k: p.get(k) for k in ("at", "date", "passed", "public_ip", "static_ip",
                                                         "registered_ips", "checks")} if p else None,
                     "preflight_today": bool(p and p.get("date") == ctx.today() and p.get("passed"))})
    return {"mode": mode, "broker": live or named, "error": err, "brokers": rows,
            "rule": "Reading your account works from any IP. A live order needs the config broker, \"paper\": "
                    "false, live_enabled, trader-risk PASS, your typed yes and today's preflight, and is sent "
                    "only by ticket.py place in your own terminal. This page never sends an order.",
            "commands": {"login": "python3 trader-broker/ticket.py login <broker>",
                         "preflight": "python3 trader-broker/ticket.py preflight <broker>"},
            "disclaimer": DISCLAIMER}


def _broker_pick(ctx, q):
    b = skill("broker")
    want = b.broker_name(q.get("broker") or ctx.config().get("broker"))
    if want == "paper":
        return None, "The config broker is paper: there is no live account to read. Pick one with ?broker=upstox, " \
                     "kite or dhan (read only)."
    if want not in b.LIVE_BROKERS:
        raise ApiError(400, "broker is one of %s" % ", ".join(b.LIVE_BROKERS))
    return want, None


def broker_rows(ctx, q, what):
    """Holdings or positions straight from the live broker (read only, cached 30 s)."""
    b = skill("broker")
    name, why_not = _broker_pick(ctx, q)
    if name is None:
        return {"connected": False, "broker": None, "rows": [], "note": why_not, "read_only": True}
    ad = b.get_adapter(name, ctx.config())
    if ctx.offline:
        return {"connected": False, "broker": name, "title": ad.title, "rows": [], "read_only": True,
                "note": "Offline mode: the live broker is not called."}
    if not ad.token():
        return {"connected": False, "broker": name, "title": ad.title, "rows": [], "read_only": True,
                "note": "No %s token in this server's environment. In the shell that starts the UI: eval \"$(python3 "
                        "trader-broker/ticket.py login %s)\", then start it again." % (ad.env_names["access_token"],
                                                                                    name)}
    base = b.adapters().base
    try:
        rows = ctx.cached("broker-%s-%s" % (what, name), 30, getattr(ad, what))
    except base.BrokerError as e:
        raise ApiError(502, "%s: %s" % (ad.title, e.message))
    value = sum((r.get("last_price") or r.get("avg_price") or 0) * abs(r.get("qty") or 0) for r in rows)
    pnl = [r.get("pnl") for r in rows if r.get("pnl") is not None]
    return {"connected": True, "broker": name, "title": ad.title, "read_only": True, "as_of": ist_text(ctx.now_ist()),
            "rows": rows, "value": round(value, 2), "pnl": round(sum(pnl), 2) if pnl else None,
            "note": "Read only, from %s's API. Nothing here places or changes an order." % ad.title}


def r_broker(ctx, q, body):
    return broker_status(ctx)


def r_broker_holdings(ctx, q, body):
    return broker_rows(ctx, q, "holdings")


def r_broker_positions(ctx, q, body):
    return broker_rows(ctx, q, "positions")


# ---------------------------------------------------------------- routes: risk

def _risk_inputs(ctx, orders, doc, cfg):
    """(portfolio, extra) for trader-risk: the F&O, behaviour and kill-switch files of trader/ (no network)."""
    risk = skill("risk")
    try:
        return risk.gather_inputs(os.path.join(ctx.workdir, "trader"), orders, doc, cfg, ctx.today(), False)
    except risk.RiskError as e:
        raise ApiError(422, str(e))


def r_risk(ctx, q, body):
    risk = skill("risk")
    cfg = ctx.config()
    c = risk.merged_config(cfg)
    last = ctx.read("state", "risk.json")
    preview, err = None, None
    doc = ctx.read("orders.json")
    orders = (doc.get("orders", doc) if isinstance(doc, dict) else doc) if doc is not None else []
    now = risk.parse_now(ctx.now_ist().strftime("%Y-%m-%d %H:%M:%S"))
    portfolio, extra = _risk_inputs(ctx, orders if isinstance(orders, list) else [], doc, cfg)
    if doc is not None:
        try:
            pdoc = ctx.read("prices.json") or {}
            prices, times = risk.load_prices(pdoc)
            preview = risk.check(orders, cfg, portfolio, prices, ctx.read("state", "broker.json"),
                                 ctx.today(), now, times, extra)
            if pdoc.get("as_of"):
                preview["prices_as_of"] = pdoc["as_of"]
        except risk.RiskError as e:
            err = str(e)
    view = risk.scenarios(cfg, portfolio, risk.load_prices(ctx.read("prices.json") or {})[0], now, extra,
                          orders if isinstance(orders, list) else [])
    limits = [{"key": k, "value": c.get(k)} for k in (
        "capital", "max_loss_per_trade", "max_daily_loss", "max_trades_per_day", "max_orders_per_day",
        "max_position_pct", "max_order_value", "min_order_value", "cash_buffer_pct", "min_reward_risk",
        "require_stop", "no_entries_before", "no_entries_after", "flat_by", "include_charges", "allow_short",
        "allow_fno", "paper", "max_consecutive_losses", "max_daily_drawdown_pct", "cooloff_after_stop_min",
        "revenge_window_min", "revenge_trade", "order_rate_per_sec", "allow_naked_short",
        "defined_risk_counts_as_stop")]
    fno = risk.fno_limits(c)
    now_s = ist_text(ctx.now_ist())
    kill = view["kill"]
    return {"limits": limits, "fno_limits": [{"key": k, "value": v} for k, v in sorted(fno.items())],
            "last_check": last, "preview": preview, "preview_error": err,
            "stale": bool(last) and last.get("date") != ctx.today(),
            "kill": {"active": bool(kill), "since": (kill or {}).get("at"), "reason": (kill or {}).get("reason"),
                     "word": risk.KILL_WORD},
            "breakers": view["breakers"], "rate": view["rate"], "scenarios": view,
            "why": {"verdict": why("each order against every rule in order, as if each PASS had filled: format, "
                                   "tick, exchange, F&O, short; for entries also size, position, cash, stop, "
                                   "target, loss at stop + charges, reward:risk, daily loss, trades, window, "
                                   "duplicates, daily order limit, kill switch, circuit-breakers, order rate; "
                                   "an F&O basket is one position (lots, ban list, premium, margin, Greeks, "
                                   "scenarios)",
                                   {"orders": "trader/orders.json", "config": "trader/config.json",
                                    "portfolio": "trader/portfolio.json", "prices": "trader/prices.json",
                                    "placed today": "trader/state/broker.json", "fills": "trader/state/trades.json",
                                    "kill switch": "trader/state/kill.json"},
                                   "trader-risk check (pure function, no network)", now_s,
                                   "The preview is not recorded. The paper broker only accepts a PASS that was "
                                   "recorded today: use Run risk check."),
                    "scenarios": why("Black-76 on each leg's forward and IV; every underlying moves k of its own "
                                     "sigmas (at-the-money IV x sqrt(days / 252)) at once, IV shifts in vol "
                                     "points; P&L after charges",
                                     {"open positions": "trader/portfolio.json", "chain": "trader/options.json",
                                      "strategy": "trader/strategy.json", "proposals": "trader/orders.json"},
                                     "trader-risk scenarios (read only)", now_s,
                                     "A model at today's IV, not a forecast."),
                    "greeks": why("net per underlying: delta in lots (and rupees per 1% move), gamma as lots of "
                                  "delta per 1% move, vega rupees per vol point, theta rupees per calendar day",
                                  {k: v for k, v in fno.items() if k.startswith("max_net") or k.startswith("max_theta")},
                                  "trader-risk book_greeks (Black-76)", now_s)},
            "disclaimer": DISCLAIMER}


def _risk_paths(ctx):
    return ["--orders", ctx.path("orders.json"), "--config", ctx.path("config.json"),
            "--portfolio", ctx.path("portfolio.json"), "--prices", ctx.path("prices.json"),
            "--state", ctx.path("state", "broker.json"), "--trades", ctx.path("state", "trades.json"),
            "--kill", ctx.path("state", "kill.json")] + (["--offline"] if ctx.offline else [])


def r_risk_check(ctx, q, body):
    if ctx.read("orders.json") is None:
        raise ApiError(409, "no trader/orders.json to check (trader-budget writes it)")
    argv = _risk_paths(ctx) + ["--out", ctx.path("state", "risk.json"), "--json"]
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


def r_risk_kill(ctx, q, body):
    """The kill switch: no new entries today (recorded entry PASSes are revoked); --flatten writes exit tickets."""
    reason = (body or {}).get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 300):
        raise ApiError(400, "reason is a short text")
    argv = ["kill"] + _risk_paths(ctx) + ["--risk", ctx.path("state", "risk.json"),
                                         "--plan", ctx.path("plan.json")]
    if reason and reason.strip():
        argv += ["--reason", reason.strip()]
    if (body or {}).get("flatten") is True:
        argv.append("--flatten")
    rc, out, err = run_skill(ctx, "risk", argv + ctx.now_args())
    return _command_result(ctx, rc, out, err)


def r_risk_unkill(ctx, q, body):
    """Lift today's kill switch: the typed word unkill, word for word."""
    typed = (body or {}).get("typed")
    if not isinstance(typed, str) or not typed.strip() or len(typed) > 50:
        raise ApiError(400, "type the word %s to lift the kill switch" % skill("risk").KILL_WORD)
    rc, out, err = run_skill(ctx, "risk", ["unkill", "--kill", ctx.path("state", "kill.json"), "--yes-text", typed]
                             + ctx.now_args())
    return _command_result(ctx, rc, out, err)


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
    for name in ("risk", "budget", "scanner", "entry", "broker"):
        for k, v in skill(name).DEFAULTS.items():
            keys.setdefault(k, v)
    keys.setdefault("insider_watch_clients", [])
    keys.setdefault("risk_free_rate", skill("options").DEFAULT_RATE)
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
        if k in LIVE_KEYS:
            errors.extend(_live_key_errors(k, v, broker))
            continue
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
    errors.extend(broker.config_mode_errors(doc))
    try:
        mode, live = broker.mode_of(doc)
    except broker.TicketError:
        mode, live = "paper", None
    if mode == "live":
        warnings.append("LIVE with %s (\"paper\": false): orders would use real money. They go out only with "
                        "ticket.py place in your own terminal, after every gate (live_enabled, trader-risk PASS, "
                        "your yes, today's preflight); this page never sends one" % live)
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


LIVE_KEYS = ("live_enabled", "static_ip", "order_rate_per_sec")
GUARDED = ("broker", "paper", "live_enabled")     # going live is done in the file, not from this page


def _live_key_errors(k, v, broker):
    if k == "live_enabled":
        if not isinstance(v, dict) or any(not isinstance(x, bool) for x in v.values()):
            return ["live_enabled is an object like {\"upstox\": true}"]
        bad = [n for n in v if broker.broker_name(n) not in broker.LIVE_BROKERS]
        return ["live_enabled: %s is not a broker (%s)" % (n, ", ".join(broker.LIVE_BROKERS)) for n in bad]
    if k == "static_ip":
        vals = list(v.values()) if isinstance(v, dict) else [v]
        flat = []
        for x in vals:
            flat.extend(x if isinstance(x, list) else [x])
        for x in flat:
            if not isinstance(x, str):
                return ["static_ip is an IP address, a list of them, or {\"kite\": \"...\"} per broker"]
            if x.strip():
                try:
                    ipaddress.ip_address(x.strip())
                except ValueError:
                    return ["static_ip: %r is not an IP address" % x]
        return []
    if k == "order_rate_per_sec":
        if not _is_num(v) or v <= 0:
            return ["order_rate_per_sec must be a number above 0"]
        if v > 9:
            return ["order_rate_per_sec is at most 9 (SEBI: more than 10 orders a second needs exchange algo "
                    "registration)"]
    return []


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
            "help": "Every key is optional: a missing key uses its default. Paper trading is the default; going "
                    "live (broker, paper, live_enabled) is your own edit of trader/config.json, never this page "
                    "(see docs/brokers.md). Create one with: python3 trader-budget/size.py init"}


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
    cur = ctx.read("config.json")
    cur = cur if isinstance(cur, dict) else {}
    changed = [k for k in GUARDED if doc.get(k) != cur.get(k)]
    if changed:
        return {"ok": False, "saved": False, "errors": [
            "%s cannot be changed from this page: going live (or back to paper) is your own edit of "
            "trader/config.json (see docs/brokers.md)" % ", ".join(changed)], "warnings": warnings, "summary": None}
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


# ---------------------------------------------------------------- routes: option chain

OPTION_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "RELIANCE", "HDFCBANK", "ICICIBANK",
                  "INFY", "TCS", "SBIN"]
MAX_PATHS = 20000


def _naive_now(ctx):
    return ctx.now_ist().replace(tzinfo=None, microsecond=0)


def _option_symbol(text):
    op = skill("options")
    try:
        return op._symbol(text or "NIFTY")
    except op.OptionsError as e:
        raise ApiError(400, str(e))


def _offline_has(ctx, name):
    return any(os.path.isfile(os.path.join(ctx.offline, n)) for n in (name, name + ".gz"))


def option_chain(ctx, sym, want=None):
    """trader-options' full analysis of one expiry (cached), with the expiry list. Offline: the saved files."""
    op = skill("options")
    now = _naive_now(ctx)

    def load():
        f = op.Fetcher(offline=ctx.offline)
        info = op.fetch_contract_info(f, sym)
        if not info["expiries"]:
            raise ApiError(404, "NSE lists no option expiries for %s (not in F&O?)" % sym)
        try:
            lots = op.fetch_lots(f)
        except op.OptionsError:
            lots = {}
        e = op.pick_expiry(info["expiries"], want, now)
        chain = op.fetch_chain(f, sym, e)
        ts = op.parse_nse_time(chain.get("timestamp"))
        r, src = op.risk_free_rate(ctx.path("config.json"))
        # an offline chain can be newer than the pinned clock: judge its age from its own time then
        a = op.analyse(chain, op.lot_size(lots, sym, e), r, now=max(now, ts) if ts else now, rate_source=src)
        a["smile"] = op.smile(a["rows"], a["forward"])
        a["max_pain_curve"] = op.max_pain(a["rows"], a["lot_size"] or 1)["curve"]
        a["buildup"] = op.oi_buildup(a["rows"])
        table = op.expiry_table(info["expiries"], lots, sym, now)
        for row in table:
            row["available"] = (not ctx.offline) or _offline_has(ctx, "chain-%s-%s.json" % (sym, row["expiry_nse"]))
        a["expiry_list"] = table
        return a
    return ctx.cached(("options", sym, str(want or "")), TTL["nse"], load)


def _options_why(a):
    T, r = a["t_years"], a["rate"]
    em = a.get("expected_move") or {}
    st_ = a.get("straddle") or {}
    pc = (a.get("pcr") or {}).get("all") or {}
    mp = a.get("max_pain_detail") or {}
    w = a.get("walls") or {}
    src = "trader-options analyse(), NSE option-chain-v3 at %s" % a["asof"]
    as_of = a["asof"]
    return {
        "forward": why("F = K + e^(rT) x (call mid - put mid), the median over the strikes nearest the money with "
                       "a tight two-sided quote (put-call parity)",
                       {"strikes": a.get("forward_strikes"), "method": a.get("forward_method"), "spot": a["spot"],
                        "forward": a["forward"], "forward - spot": a.get("basis"), "r": r, "T (years)": T},
                       src, as_of, "Spot stops at 15:30 while options trade to 15:40: the forward is what the options "
                                   "themselves imply, so every IV and Greek here uses it."),
        "atm_iv": why("Black-76 implied volatility of the ATM call and put on the forward (a bracketed Newton solve), "
                      "their average", {"ATM": a["atm"], "call IV": a.get("atm_iv_ce"), "put IV": a.get("atm_iv_pe")},
                      src, as_of, "NSE's own IV column is not used: it differs between calls and puts."),
        "pcr": why("PCR = total put OI / total call OI for this expiry (contracts on both sides)",
                   {"put OI": pc.get("pe_oi"), "call OI": pc.get("ce_oi"), "PCR OI": a.get("pcr_oi"),
                    "PCR volume": a.get("pcr_volume"), "near ATM": ((a.get("pcr") or {}).get("near_atm") or {}).get("oi")},
                   src, as_of, "A crowd gauge, not a signal. Above about 1.2 more puts are open; below 0.8 more calls."),
        "max_pain": why("the listed strike K* with the least sum over strikes of call OI x max(K* - K, 0) + put OI x "
                        "max(K - K*, 0), x lot = rupees option buyers would be paid at expiry",
                        {"max pain": mp.get("strike"), "pain (₹)": mp.get("pain"), "distance from forward": mp.get("distance"),
                         "lot": a.get("lot_size")}, src, as_of, "A weak pull, strongest on expiry day."),
        "walls": why("the out-of-the-money call strike (at or above the forward) and put strike (at or below) with the "
                     "most open interest", {"call wall": w.get("call"), "put wall": w.get("put"),
                                             "top calls": [x["strike"] for x in w.get("calls") or []],
                                             "top puts": [x["strike"] for x in w.get("puts") or []]},
                     src, as_of, "Sellers there lose if the price goes through: read as resistance and support, "
                                 "not as a promise."),
        "expected_move": why("one standard deviation to expiry: straddle x e^(rT) x 1.2533 (the straddle prices the "
                             "average move, 0.80 of one SD); check: forward x IV x sqrt(T)",
                             {"straddle": em.get("straddle"), "from straddle": em.get("from_straddle"),
                              "from IV": em.get("from_iv"), "low": em.get("low"), "high": em.get("high"), "T (years)": T},
                             src, as_of, "About 68% of the time the underlying ends inside it, if IV is right."),
        "straddle": why("ATM call price + ATM put price (mid, or LTP when the quote is wide)",
                        {"strike": st_.get("strike"), "call": st_.get("ce"), "put": st_.get("pe"),
                         "₹ per lot": st_.get("rupees_per_lot")}, src, as_of),
        "iv": why("Black-76 IV of this option's price on the forward, % a year; none when the price is at or below "
                  "intrinsic, or the quote is too wide and it did not trade", {"forward": a["forward"], "T": T, "r": r},
                  src, as_of),
        "greeks": why("Black-76 on the forward, per unit: delta = dPrice/dF, gamma per point, theta ₹ per calendar day, "
                      "vega ₹ per vol point", {"forward": a["forward"], "T": T, "r": r}, src, as_of,
                      "Multiply by lot size and lots for a position. Unreliable in the last hours before expiry."),
        "oi": why("open interest in CONTRACTS (lots); x lot size = units. Bars scale to the largest OI shown",
                  {"lot": a.get("lot_size")}, src, as_of),
        "oi_chg": why("change in open interest today, contracts. With the price change it classifies the build-up: "
                      "price up + OI up = long build-up, down + up = short build-up (writing), down + down = long "
                      "unwinding, up + down = short covering", {}, src, as_of),
        "smile": why("IV of the out-of-the-money side at each strike: puts below the forward, calls above",
                     {"points": len(a.get("smile") or []), "25-delta risk reversal": (a.get("skew") or {}).get(
                         "risk_reversal")}, src, as_of, "Puts usually cost more IV than calls on Indian indices (skew)."),
    }


def r_options(ctx, q, body):
    sym = _option_symbol(q.get("symbol"))
    try:
        n = int(q.get("strikes") or 15)
    except ValueError:
        raise ApiError(400, "strikes is a whole number (0 = every strike)")
    a = option_chain(ctx, sym, q.get("expiry") or None)
    op = skill("options")
    out = {k: v for k, v in a.items() if k != "rows"}
    out["rows"] = op.window_rows(a["rows"], a["atm"], max(0, min(n, 200)))
    out["strikes_total"] = len(a["rows"])
    out["available"] = True
    out["symbols"] = OPTION_SYMBOLS
    out["why"] = _options_why(a)
    out["disclaimer"] = DISCLAIMER
    return out


# ---------------------------------------------------------------- routes: strategy builder

def _num_param(body, key, lo=None, hi=None, integer=False):
    v = (body or {}).get(key)
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ApiError(400, "%s must be a number" % key)
    try:
        x = int(v) if integer else float(v)
    except ValueError:
        raise ApiError(400, "%s must be a number" % key)
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ApiError(400, "%s must be between %s and %s" % (key, lo, hi))
    return x


def strategy_inputs(ctx, body):
    """Validate a strategy request and load what it needs: (doc args, legs, markets, near, name, params)."""
    stg = skill("strategy")
    if not isinstance(body, dict):
        raise ApiError(400, "send a JSON object")
    sym = _option_symbol(body.get("symbol"))
    expiry = body.get("expiry") or None
    far = body.get("far_expiry") or None
    tmpl = body.get("template") or None
    legs_text = body.get("legs") or None
    if bool(tmpl) == bool(legs_text):
        raise ApiError(400, "send either a template or legs")
    if legs_text is not None and (not isinstance(legs_text, str) or len(legs_text) > 600):
        raise ApiError(400, "legs is short text, like '-1 CE ATM+200, +1 CE ATM+300'")
    params = {}
    raw = body.get("params") or {}
    if not isinstance(raw, dict):
        raise ApiError(400, "params is an object")
    for key, lo, hi, integer in (("strike", 1, 10 ** 7, False), ("wings", 0, 10 ** 6, False),
                                 ("width", 0.01, 10 ** 6, False), ("delta", 0.01, 0.99, False),
                                 ("skip", 0, 10 ** 6, False), ("ratio", 1, 10, True), ("lots", 1, 100, True)):
        v = _num_param(raw, key, lo, hi, integer)
        if v is not None:
            params[key] = v
    if raw.get("type") not in (None, ""):
        if str(raw["type"]).upper() not in ("CE", "PE"):
            raise ApiError(400, "type is CE or PE")
        params["type"] = str(raw["type"]).upper()
    first = option_chain(ctx, sym, expiry)
    near = first["expiry"]
    markets = {near: first}
    if tmpl:
        name = stg.template_name(tmpl)
        if name == "calendar":
            if not far:
                later = [e for e in first["expiry_list"] if e["expiry"] > near and e["days"] >= 7 and e["available"]]
                if not later:
                    raise ApiError(400, "a calendar needs a second expiry (far_expiry)")
                far = later[0]["expiry_nse"]
            a2 = option_chain(ctx, sym, far)
            if a2["expiry"] <= near:
                raise ApiError(400, "far_expiry must be after the near expiry")
            markets[a2["expiry"]] = a2
        specs = stg.template_specs(name, params, first)
    else:
        name = "custom"
        specs = stg.parse_legs(legs_text)
        params = {"legs": legs_text}
        for sp in specs:
            if sp["expiry"] not in ("near", "far") and sp["expiry"] not in markets:
                a2 = option_chain(ctx, sym, sp["expiry"])
                markets[a2["expiry"]] = a2
    fill = body.get("fill") or "mid"
    if fill not in ("mid", "touch"):
        raise ApiError(400, "fill is mid or touch")
    legs = stg.resolve_legs(specs, markets, near, fill)
    return legs, markets, near, name, params


def _history(ctx, sym, asof):
    stg = skill("strategy")
    key = ("history", sym, asof)

    def load():
        try:
            return stg.load_history(sym, [ctx.offline] if ctx.offline else None, skill("options").parse_nse_time(asof))
        except stg.StrategyError:
            return None          # the page shows the textbook figure and says the simulation had no data
    return ctx.cached(key, TTL["history"], load)


def strategy_doc(ctx, body):
    stg = skill("strategy")
    legs, markets, near, name, params = strategy_inputs(ctx, body)
    iv_shift = _num_param(body, "iv_shift", -50, 50) or 0.0
    seed = _num_param(body, "seed", 0, 2 ** 31, True)
    paths = _num_param(body, "paths", 500, MAX_PATHS, True) or 5000
    at = body.get("at") or None
    if at is not None and (not isinstance(at, str) or len(at) > 20):
        raise ApiError(400, "at is today, expiry or a date")
    simulate = body.get("simulate", True) is not False
    hist = _history(ctx, markets[near]["underlying"], markets[near]["asof"]) if simulate else None
    doc = stg.evaluate(legs, markets, ctx.config(), name, params, at, iv_shift, hist,
                       stg.DEFAULT_SEED if seed is None else seed, paths, None, _naive_now(ctx), simulate)
    doc["generated_at"] = ist_text(ctx.now_ist())
    doc["why"] = _strategy_why(doc)
    doc["disclaimer"] = DISCLAIMER
    return doc


def _strategy_why(d):
    e, m, p = d["expiry"], d["margin"], d["probability"]
    src = "trader-strategy evaluate() on the trader-options chain of %s" % d["asof"]
    t = p["textbook"]
    s = p.get("simulated") or {}
    legs = ["%s %d %s %s @ %s" % (l["side"], l["lots"], l["strike"] if l["strike"] else "FUT", l["option_type"] or "",
                                  l["price"]) for l in d["legs"]]
    return {
        "net_premium": why("sum over option legs of premium x qty: received for a sell, paid for a buy",
                           {"legs": legs, "received": d["net_premium"]["received"], "paid": d["net_premium"]["paid"],
                            "crossing the spread": d["spread_cost"]}, src, d["asof"]),
        "max_profit": why("the payoff's highest point at the near expiry, underlying from 0 up; unlimited when it "
                          "keeps rising (a naked call bought)", {"at": e["max_profit_at"], "after charges":
                                                                  e["max_profit_after_charges"], "exact": e["exact"]},
                          src, d["asof"]),
        "max_loss": why("the payoff's lowest point at the near expiry, underlying from 0 up; unlimited when it keeps "
                        "falling (a naked call sold)", {"at": e["max_loss_at"], "after charges":
                                                         e["max_loss_after_charges"], "exact": e["exact"]},
                        src, d["asof"], None if e["exact"] else "Two expiries: the later leg is a Black-76 value."),
        "breakevens": why("where the expiry payoff crosses 0, refined to 0.01 point; after charges every leg's round "
                          "trip is taken off first (STT on exercise for in-the-money longs)",
                          {"gross": e["breakevens"], "after charges": e["breakevens_after_charges"],
                           "shift": [x["shift"] for x in e["breakeven_shift"]]}, src, d["asof"]),
        "margin": why(m["explain"], {"method": m["method"], "required": m["required"], "error +/- %": m["error_pct"],
                                     "max loss (not the margin)": m["max_loss"],
                                     "return on margin %": m["return_on_margin"],
                                     "scan risk": m["span_style"]["scan_risk"], "exposure": m["span_style"]["exposure"],
                                     "premium paid": m["span_style"]["premium_paid"],
                                     "price scan": m["span_style"]["assumptions"]["price_scan"],
                                     "broker basket": m["broker_basket"]},
                      m["span_style"]["assumptions"]["source"], d["asof"], m["broker_note"]),
        "pop_textbook": why(t["method"] + ": PoP = share of outcomes with P&L after charges above 0",
                            {"PoP gross": t["pop_gross"], "expected value": t["ev"], "5% tail": t["tail_5pct"],
                             "worst 5% average": t["tail_5pct_avg"], "IV": t["sigma"]}, src, d["asof"],
                            "A model: it assumes IV is right and moves are normal in log terms."),
        "pop_simulated": why(s.get("method") or "not run", {"PoP gross": s.get("pop_gross"), "expected value": s.get("ev"),
                                                           "5% tail": s.get("tail_5pct"), "realised vol": s.get(
                                                               "realised_vol"), "sessions": s.get("sessions")},
                             (s.get("history") or {}).get("source") or "no history", d["asof"],
                             "History is not the future: a quiet year makes selling look safer than it is."),
        "greeks": why("sum over legs of the leg's Black-76 Greek x qty, signed by side",
                      {"delta (units)": d["greeks"]["delta"], "delta (lots)": d["greeks"]["delta_lots"],
                       "gamma": d["greeks"]["gamma"], "theta ₹/day": d["greeks"]["theta"],
                       "vega ₹/vol pt": d["greeks"]["vega"]}, src, d["asof"]),
        "charges": why("round trip per leg at the dated 2026 F&O rates, held to expiry with the underlying at the "
                       "forward", {"total": d["charges"]["at_forward_expiry"],
                                   "per leg": [x["total"] for x in d["charges"]["at_forward_expiry_detail"]]},
                       "trader-strategy round_trip_charges (the shared F&O table)", d["asof"], d["charges"]["note"]),
    }


def r_strategy(ctx, q, body):
    stg = skill("strategy")
    saved = ctx.read("strategy.json")
    summary = None
    if isinstance(saved, dict) and saved.get("schema") == stg.SCHEMA:
        summary = {k: saved.get(k) for k in ("name", "underlying", "near_expiry", "generated_at", "net_premium")}
        summary["legs"] = len(saved.get("legs") or [])
    return {"available": True, "templates": stg.list_templates(), "symbols": OPTION_SYMBOLS, "default": "NIFTY",
            "saved": summary, "max_paths": MAX_PATHS,
            "legs_help": "[+|-]LOTS CE|PE|FUT STRIKE [@DD-Mon-YYYY] [=PRICE]; STRIKE is 23000, ATM, ATM+200, D25 "
                         "(delta) or P50 (premium)", "disclaimer": DISCLAIMER}


def r_strategy_price(ctx, q, body):
    return strategy_doc(ctx, body)


def r_strategy_draft(ctx, q, body):
    """The legs as a PROPOSAL in trader/orders.json: nothing is placed. trader-risk checks each leg, then
    the paper broker needs a typed yes for each one, as for any other order."""
    stg = skill("strategy")
    c = skill("broker").merged_config(ctx.config())
    if not c.get("paper", True) or str(c.get("broker", "paper")).lower() != "paper":
        raise ApiError(409, "the UI drafts orders for the paper account only (\"paper\": true, broker paper)")
    product = str((body or {}).get("product") or "NRML").upper()
    doc = strategy_doc(ctx, dict(body or {}, simulate=False))
    doc.pop("why", None)
    try:
        written = stg.write_orders(ctx.path("orders.json"), doc, product, bool((body or {}).get("replace")),
                                   (body or {}).get("persona"))
    except stg.StrategyError as e:
        raise ApiError(409, str(e).replace(ctx.path("orders.json"), "trader/orders.json")
                       .replace("--replace", "replace (tick 'Replace other proposals')"))
    stg.write_json(ctx.path("strategy.json"), doc)
    return {"ok": True, "paper": True, "orders": written["orders"], "path": ctx.path("orders.json"),
            "strategy_path": ctx.path("strategy.json"), "tickets": tickets(ctx)["tickets"],
            "basket": tickets(ctx).get("basket"),
            "next": "Proposals written, nothing placed. Next: Risk panel -> Run risk check, then type yes for each "
                    "leg on the Portfolio page. Until trader-risk has F&O rules it blocks option legs unless your "
                    "config allows F&O (\"allow_fno\": true)."}


# ---------------------------------------------------------------- routes: data self-test

def r_doctor(ctx, q, body):
    """The last self-test from trader/doctor.json, for the header badge. Nothing is fetched."""
    hist = ctx.read("doctor.json")
    last = hist.get("last") if isinstance(hist, dict) else None
    if not isinstance(last, dict):
        return {"checked": False, "note": "not checked yet: run python3 trader-doctor/doctor.py, or click the badge"}
    failing = [r for r in last.get("sources") or [] if r.get("status") in ("changed", "down", "blocked")]
    return dict(last, checked=True, failing=failing)


def r_doctor_run(ctx, q, body):
    """Run the self-test now (about 30 s live: one polite request per source) and update trader/doctor.json."""
    argv = ["--json", "--history", ctx.path("doctor.json")]
    only = (body or {}).get("only")
    if only is not None:
        if only not in ("nse", "yahoo"):
            raise ApiError(400, "only is nse or yahoo")
        argv += ["--only", only]
    if ctx.offline:
        rec = os.path.join(ctx.offline, "doctor")
        if not os.path.isdir(rec):
            raise ApiError(409, "offline demo: no recorded answers for the self-test (a doctor/ folder in %s)"
                           % ctx.offline)
        argv += ["--offline", rec]
    rc, out, err = run_skill(ctx, "doctor", argv + ctx.now_args())
    try:
        report = json.loads(out)
    except ValueError:
        raise ApiError(502, (err or out).strip() or "the self-test stopped with exit code %s" % rc)
    return r_doctor(ctx, {}, None) if isinstance(report, dict) else report


# ---------------------------------------------------------------- routes: market regime

def r_regime(ctx, q, body):
    """trader-regime's read for now: ?symbols=NIFTY,BANKNIFTY (default) or stocks, up to 6. From the archive
    (trader/archive or $BT_ARCHIVE) when present, else Yahoo daily bars; offline: the archive only."""
    rg = skill("regime")
    syms = [s.strip().upper() for s in str(q.get("symbols") or "NIFTY,BANKNIFTY").split(",") if s.strip()]
    if len(syms) > 6 or not all(re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", s) for s in syms):
        raise ApiError(400, "symbols: up to 6 NSE symbols, e.g. ?symbols=NIFTY,BANKNIFTY,RELIANCE")
    now = ctx.now_ist().replace(tzinfo=None, second=0, microsecond=0)

    def read():
        try:
            cfg = rg.load_config(ctx.path("regime-config.json"))
        except rg.RegimeError as e:
            raise ApiError(400, str(e))
        src = rg.Sources(os.environ.get("BT_ARCHIVE") or ctx.path("archive"), None, not ctx.offline, ctx.path())
        out, errors = [], []
        for s in syms:
            try:
                out.append(rg.read_now(s, src, now, cfg))
            except (rg.RegimeError, rg.RulesError) as e:
                errors.append("%s: %s" % (s, e))
        return {"regimes": out, "errors": errors, "notes": src.notes, "as_of": ist_text(ctx.now_ist()),
                "source": "archive" if src.root else ("none (offline, no archive)" if ctx.offline else "yahoo"),
                "why": why("trend: ADX(14), efficiency ratio(10), EMA 20/50 and slope, 200-day average; vol: India "
                           "VIX percentile over 252 sessions (indices) or 20-day realised vol percentile (stocks); "
                           "each day from data up to its own close", {"thresholds": {k: v for k, v in cfg.items()
                                                                                    if k != "events"}},
                           "trader-regime (docs/regime.md)", ist_text(ctx.now_ist()),
                           "A regime describes the market so far; it does not predict the next move."),
                "disclaimer": DISCLAIMER}
    return ctx.cached(("regime", ",".join(syms), now.strftime("%Y-%m-%d %H:%M")[:15]), TTL["history"], read)


# ---------------------------------------------------------------- routes: agent personas and their scorecards

AGENTS = os.path.join(ROOT, "agents")
REVENGE_MINUTES = 30
MIN_SAMPLE = 20


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return v


def parse_frontmatter(text):
    """The YAML subset the agents/ files use: `key: value`, flow lists `[a, b]`, and one level of
    nested string map (metadata). -> (fields, body). ValueError when it is not that subset."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("no --- frontmatter --- block at the top")
    fields, parent = {}, None
    for n, line in enumerate(m.group(1).splitlines(), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        nested = line.startswith("  ")
        km = re.match(r"^\s*([A-Za-z][A-Za-z0-9_-]*):(?:\s+(.*))?$", line)
        if not km:
            raise ValueError("line %d is not `key: value`" % n)
        key, value = km.group(1), (km.group(2) or "").strip()
        if nested:
            if parent is None:
                raise ValueError("line %d is indented under nothing" % n)
            fields[parent][key] = _scalar(value)
            continue
        if key in fields:
            raise ValueError("%s is set twice" % key)
        if value:
            fields[key], parent = _scalar(value), None
        else:
            fields[key], parent = {}, key
    return fields, m.group(2)


def personas():
    """The agent personas in agents/*.md: frontmatter plus the instructions (the body)."""
    out = []
    if not os.path.isdir(AGENTS):
        return out
    for f in sorted(os.listdir(AGENTS)):
        if not f.endswith(".md") or f.upper() == "README.MD":
            continue
        with open(os.path.join(AGENTS, f), encoding="utf-8") as fh:
            fields, body = parse_frontmatter(fh.read())
        meta = fields.get("metadata") if isinstance(fields.get("metadata"), dict) else {}
        out.append({"name": fields.get("name"), "file": "agents/" + f, "description": fields.get("description", ""),
                    "title": meta.get("title") or fields.get("name"), "role": meta.get("role", ""),
                    "skills": fields.get("skills") or [], "mcp_tools": fields.get("mcp-tools") or [],
                    "tools": fields.get("tools", ""), "metadata": meta, "body": body.strip() + "\n"})
    return out


def _persona_index(ctx):
    """Order fingerprint -> what the drafting persona tagged, from trader/state/personas.json."""
    broker = skill("broker")
    doc = ctx.read("state", "personas.json") or {}
    drafts = doc.get("drafts") if isinstance(doc, dict) else None
    index = {}
    for d in drafts if isinstance(drafts, list) else []:
        orders = [o for o in (d.get("orders") or []) if isinstance(o, dict)] if isinstance(d, dict) else []
        if not orders:
            continue
        fps = [broker.fingerprint(o) for o in orders]
        basket = len(orders) > 1 and all(o.get("leg") for o in orders)       # a trader-strategy multi-leg
        for o, fp in zip(orders, fps):
            plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
            setup = str(plan.get("setup") or "")
            index[fp] = {"persona": str(d.get("persona") or plan.get("persona") or ""),
                         "idea": fps[0] if basket else fp, "setup": setup,
                         "strategy": setup[4:] if setup.startswith("lab:") else None,
                         "drafted_at": d.get("at"), "source": d.get("source")}
    return index


def _instrument(t):
    return tuple(str(t.get(k) or "") for k in ("symbol", "expiry", "strike", "option_type"))


def _minutes(at):
    m = re.match(r"^(\d{4}-\d\d-\d\d)[T ](\d\d):(\d\d)", str(at or ""))
    return (m.group(1), int(m.group(2)) * 60 + int(m.group(3))) if m else (None, None)


def persona_ideas(trades, index):
    """Every paper trade idea, rebuilt from the fills in time order. A fill whose order fingerprint a
    persona tagged opens (or adds to) that persona's idea; a fill that reduces an open position closes
    the oldest lots first (the same idea first), so a stop, a target or a flatten ticket is credited to
    the idea it closes. P&L is sells minus buys minus every charge of both sides."""
    lots, ideas, order = {}, {}, []
    for n, t in enumerate(sorted((t for t in trades if isinstance(t, dict)), key=lambda t: str(t.get("at") or ""))):
        try:
            qty, price = int(t.get("qty") or 0), float(t.get("price") or 0)
            charges = float(t.get("charges") or 0)
        except (TypeError, ValueError):
            continue
        side = str(t.get("side") or "").upper()
        if qty <= 0 or price <= 0 or side not in ("BUY", "SELL"):
            continue
        sign = 1 if side == "BUY" else -1
        tag = index.get(t.get("fingerprint")) if t.get("fingerprint") else None
        key = _instrument(t)
        book = lots.setdefault(key, [])
        cpu = charges / qty
        left = qty
        mine = tag["idea"] if tag else None
        for lot in sorted([l for l in book if l["qty"] * sign < 0], key=lambda l: (l["idea"] != mine, l["n"])):
            if not left:
                break
            take = min(left, abs(lot["qty"]))
            idea = ideas[lot["idea"]]
            gross = (price - lot["price"]) * take * (1 if lot["qty"] > 0 else -1)
            idea["pnl"] += gross - lot["cpu"] * take - cpu * take
            idea["charges"] += lot["cpu"] * take + cpu * take
            idea["open_qty"] -= take
            idea["fills"] += 1
            idea["exits"].append(str(t.get("leg") or ("order" if t.get("fingerprint") else "manual")))
            idea["closed_at"] = t.get("at")
            if t.get("product"):
                idea["exit_products"].add(str(t["product"]).upper())
            lot["qty"] += take * sign
            left -= take
        book[:] = [l for l in book if l["qty"]]
        if left:
            iid = mine or "manual:%s:%s:%d" % (key[0], t.get("at"), n)
            idea = ideas.get(iid)
            if idea is None:
                idea = ideas[iid] = {
                    "id": iid, "persona": (tag or {}).get("persona") or "", "strategy": (tag or {}).get("strategy"),
                    "setup": (tag or {}).get("setup") or "", "symbol": key[0], "opened_at": t.get("at"),
                    "date": t.get("date") or str(t.get("at") or "")[:10], "closed_at": None, "side": side,
                    "pnl": 0.0, "charges": 0.0, "open_qty": 0, "fills": 0, "exits": [],
                    "exit_products": set(), "ticketed": bool(tag), "product": str(t.get("product") or "").upper()}
                order.append(iid)
            idea["open_qty"] += left
            idea["fills"] += 1
            book.append({"qty": left * sign, "price": price, "cpu": cpu, "idea": iid, "n": n})
    out = []
    for iid in order:
        i = ideas[iid]
        i["closed"] = i["open_qty"] == 0
        i["pnl"] = round(i["pnl"], 2) if i["closed"] else None
        i["charges"] = round(i["charges"], 2)
        i["exit_products"] = sorted(i["exit_products"])
        out.append(i)
    return out


def _scorecard(name, ideas, records, index, cfg):
    mine = [i for i in ideas if i["persona"] == name]
    closed = [i for i in mine if i["closed"]]
    closed.sort(key=lambda i: str(i["closed_at"] or ""))
    pnls = [i["pnl"] for i in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    peak = run = dd = 0.0
    streak = longest = 0
    for p in pnls:
        run += p
        peak = max(peak, run)
        dd = max(dd, peak - run)
        streak = streak + 1 if p < 0 else 0
        longest = max(longest, streak)
    per_day = {}
    for i in mine:
        per_day[i["date"]] = per_day.get(i["date"], 0) + 1
    flags = []
    cap = int(cfg.get("max_trades_per_day") or 0)
    for day, n in sorted(per_day.items()):
        if cap and n > cap:
            flags.append({"kind": "overtrading", "date": day,
                          "text": "%d entries on %s, over your max_trades_per_day of %d" % (n, day, cap)})
    flat = cfg.get("flat_by") or "15:10"
    fm = int(flat[:2]) * 60 + int(flat[3:5]) if re.match(r"^\d\d:\d\d$", str(flat)) else None
    for i in closed:
        d, m = _minutes(i["closed_at"])
        if fm is not None and m is not None and i["product"] == "MIS" and m > fm:
            flags.append({"kind": "late-exit", "date": d, "text": "%s closed at %s IST, after flat_by %s (MIS)"
                          % (i["symbol"], str(i["closed_at"])[11:16], flat)})
        if i["pnl"] < 0:
            for j in mine:
                jd, jm = _minutes(j["opened_at"])
                if j is not i and jd == d and m is not None and jm is not None and 0 <= jm - m <= REVENGE_MINUTES:
                    flags.append({"kind": "revenge", "date": jd, "text": "a new %s entry %d minutes after a loss on "
                                  "%s (%s IST): the classic revenge trade" % (j["symbol"], jm - m, i["symbol"],
                                                                            str(i["closed_at"])[11:16])})
    for i in mine:
        if "manual" in i["exits"]:
            flags.append({"kind": "off-ticket", "date": i["date"], "text": "%s was closed by a fill with no ticket "
                          "(a manual fill, outside the trader-risk check)" % i["symbol"]})
    fps = {fp for fp, v in index.items() if v["persona"] == name}
    placed = [r for r in records if r.get("fingerprint") in fps and r.get("status") == "placed"]
    declined = [r for r in records if r.get("fingerprint") in fps and r.get("status") == "declined"]
    gross_w, gross_l = sum(wins), -sum(losses)
    n = len(closed)
    if n == 0:
        verdict = "No closed paper trades yet: nothing to judge."
    elif n < MIN_SAMPLE:
        verdict = ("%d closed paper trade%s: far too few to tell skill from luck (want %d or more, and even then "
                   "a record is not a forecast)." % (n, "" if n == 1 else "s", MIN_SAMPLE))
    else:
        verdict = ("%d closed paper trades. Still a record of the past, not a forecast: compare it with the "
                   "strategy's lab scorecard and its out-of-sample expectancy." % n)
    return {
        "persona": name, "ideas": len(mine), "closed": n, "open": len(mine) - n, "wins": len(wins),
        "losses": len(losses), "scratches": n - len(wins) - len(losses),
        "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
        "net_pnl": round(sum(pnls), 2), "charges": round(sum(i["charges"] for i in mine), 2),
        "avg_win": round(gross_w / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_l / len(losses), 2) if losses else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "expectancy": round(sum(pnls) / n, 2) if n else None,
        "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
        "max_drawdown": round(dd, 2), "losing_streak_now": streak, "longest_losing_streak": longest,
        "days_traded": len(per_day), "max_entries_in_a_day": max(per_day.values()) if per_day else 0,
        "drafted": len({fp for fp in fps}), "placed": len(placed), "declined": len(declined),
        "strategies": sorted({i["strategy"] for i in mine if i["strategy"]}),
        "first_at": mine[0]["opened_at"] if mine else None, "last_at": (closed[-1]["closed_at"] if closed else
                                                                         (mine[-1]["opened_at"] if mine else None)),
        "flags": flags, "verdict": verdict,
        "recent": [{k: i[k] for k in ("symbol", "setup", "strategy", "opened_at", "closed_at", "pnl", "charges",
                                      "exits", "closed")} for i in mine[-10:]],
    }


def persona_scorecards(ctx, only=None):
    """Each persona's paper track record from trader/state/trades.json: closed ideas, P&L after every
    charge, win rate, profit factor, drawdown, streaks, and behaviour flags (overtrading, revenge
    trades, exits after flat_by, fills with no ticket). Read only."""
    broker = skill("broker")
    cfg = broker.merged_config(ctx.config())
    try:
        trades = (broker.read_json(ctx.path("state", "trades.json"), "trades log", required=False) or {}
                  ).get("trades", [])
        state = broker.load_state(ctx.path("state", "broker.json"))
    except broker.TicketError as e:
        raise ApiError(422, str(e))
    index = _persona_index(ctx)
    ideas = persona_ideas(trades if isinstance(trades, list) else [], index)
    known = personas()
    names = [p["name"] for p in known]
    for i in ideas:
        if i["persona"] and i["persona"] not in names:
            names.append(i["persona"])
    if only:
        if only not in names:
            raise ApiError(404, "no persona %s (agents/: %s)" % (only, ", ".join(names) or "none"))
        names = [only]
    about = {p["name"]: p for p in known}
    cards = []
    for n in names:
        c = _scorecard(n, ideas, state.get("orders", []), index, cfg)
        p = about.get(n) or {}
        c.update({"title": p.get("title") or n, "role": p.get("role", ""), "description": p.get("description", ""),
                  "file": p.get("file"), "known": n in about})
        cards.append(c)
    loose = [i for i in ideas if not i["persona"]]
    now_s = ist_text(ctx.now_ist())
    return {
        "personas": cards,
        "unattributed": {"ideas": len(loose), "closed": len([i for i in loose if i["closed"]]),
                         "net_pnl": round(sum(i["pnl"] for i in loose if i["closed"]), 2)},
        "as_of": now_s, "trades": len(trades) if isinstance(trades, list) else 0,
        "why": why("per idea: sells minus buys minus every charge of both sides, once its position is flat; a fill "
                   "is credited to the persona whose tagged proposal (trader/state/personas.json) has its order "
                   "fingerprint, and a closing fill to the idea whose position it closes",
                   {"trades": "trader/state/trades.json", "tags": "trader/state/personas.json",
                    "yes and no": "trader/state/broker.json"}, "the paper broker's own fill log", now_s,
                   "Paper fills are simulated: a real fill can be worse. Fewer than %d closed trades says almost "
                   "nothing about skill." % MIN_SAMPLE),
        "disclaimer": DISCLAIMER,
    }


def lab_verdicts(ctx):
    """The Strategy Lab's current verdict per run (trader/lab/<name>/status.json), read from disk."""
    out = []
    base = ctx.path("lab")
    if not os.path.isdir(base):
        return out
    for n in sorted(os.listdir(base)):
        p = os.path.join(base, n, "status.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                st = json.load(fh)
        except ValueError:
            continue
        if isinstance(st, dict):
            out.append({"name": n, "verdict": st.get("verdict"), "why": st.get("why"), "updated": st.get("updated")})
    return out


def r_agents(ctx, q, body):
    cards = persona_scorecards(ctx)
    by = {c["persona"]: c for c in cards["personas"]}
    team = [{k: p[k] for k in ("name", "title", "role", "description", "file", "skills", "mcp_tools")}
            for p in personas()]
    for t in team:
        t["scorecard"] = by.get(t["name"])
    return {"team": team, "others": [c for c in cards["personas"] if not c["known"]],
            "unattributed": cards["unattributed"], "lab": lab_verdicts(ctx), "as_of": cards["as_of"],
            "why": cards["why"], "disclaimer": DISCLAIMER,
            "how": "Tag a proposal with --persona NAME (trader-budget size.py, trader-strategy --orders) or the "
                   "persona argument of the MCP tools paper_order_draft and lab_forward. Untagged fills show "
                   "as unattributed."}


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
    ("GET", "/api/orders", r_orders, "order tickets with their trader-risk status (a basket as one ticket)", False),
    ("GET", "/api/fno", r_fno, "paper F&O by strategy: MTM, Greeks, margin, payoff at expiry", False),
    ("GET", "/api/broker", r_broker, "live broker connection: credentials set, capabilities, last preflight", False),
    ("GET", "/api/broker/holdings", r_broker_holdings, "holdings at the live broker, read only: ?broker=", False),
    ("GET", "/api/broker/positions", r_broker_positions, "positions at the live broker, read only: ?broker=", False),
    ("GET", "/api/risk", r_risk, "your limits, the verdicts, a fresh preview, Greeks, scenarios, kill switch",
     False),
    ("GET", "/api/config", r_config, "trader/config.json, the effective settings and their meaning", False),
    ("GET", "/api/options", r_options, "option chain with IV, Greeks, PCR, max pain, walls: ?symbol=&expiry=", False),
    ("GET", "/api/strategy", r_strategy, "strategy templates, symbols and the saved trader/strategy.json", False),
    ("GET", "/api/doctor", r_doctor, "the last data self-test (trader/doctor.json), for the header badge", False),
    ("GET", "/api/agents", r_agents, "the agent personas (agents/) and each one's paper scorecard", False),
    ("GET", "/api/regime", r_regime, "market regime now: trend, vol bucket, gap, expiry and events: ?symbols=", False),
    ("POST", "/api/risk/check", r_risk_check, "run trader-risk on trader/orders.json and record the verdicts", True),
    ("POST", "/api/risk/kill", r_risk_kill, "kill switch on: no new entries today (exits still pass)", True),
    ("POST", "/api/risk/unkill", r_risk_unkill, "lift the kill switch: the typed word unkill", True),
    ("POST", "/api/prices/refresh", r_refresh_prices, "trader-alerts quote --holdings --write", True),
    ("POST", "/api/orders/confirm", r_confirm, "record your typed yes for ONE order (paper broker)", True),
    ("POST", "/api/orders/decline", r_decline, "record a no for one order", True),
    ("POST", "/api/orders/fill", r_fill, "simulate a paper fill (entry, stop or target; or a whole basket)", True),
    ("POST", "/api/fno/adjust", r_fno_adjust, "write an F&O adjustment PROPOSAL (roll, close a leg, hedge); places nothing",
     True),
    ("POST", "/api/config/validate", r_config_validate, "check a config without saving it", True),
    ("POST", "/api/config", r_config_save, "validate and save trader/config.json (keeps a .bak)", True),
    ("POST", "/api/strategy/price", r_strategy_price, "price a template or custom legs (trader-strategy evaluate)",
     True),
    ("POST", "/api/strategy/draft", r_strategy_draft, "write the legs as a paper PROPOSAL to trader/orders.json", True),
    ("POST", "/api/doctor/run", r_doctor_run, "run the data self-test now and update trader/doctor.json", True),
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
