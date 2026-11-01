#!/usr/bin/env python3
"""Co-trader for trader-cotrader: today's trade candidates from backtested rules, with honest odds. Standard library only.

"What are the best trades right now, and how sure are we?" Most days the honest answer is "none":
no strategy in the lab has passed its gates, or the ones that have do not fit today's market. This tool
says so plainly, and when something does qualify it shows the evidence for and against it.

    python3 cotrader.py today                      # PROMOTED and WATCH candidates, or why there are none
    python3 cotrader.py today --tier research      # also the unproven RESEARCH ideas (study and paper only)
    python3 cotrader.py explain 2026-11-02-rsi2-nifty50-SBIN
    python3 cotrader.py calibration                # how well past probabilities matched what happened
    python3 cotrader.py log                        # every logged prediction and its outcome

How a candidate is made (docs/cotrader.md has the method and the why):

  1. Strategies come from trader-lab (trader/lab/<name>/): PROMOTE is tier PROMOTED (lab gates passed and a
     paper forward record), WATCH is tier WATCH (gates passed, forward test running: paper only). Anything
     else (REJECT, DEMOTE, a library spec never run) is RESEARCH: unproven, for study or paper only, never
     for a live order, and shown only with --tier research.
  2. Each strategy's history of trades (after charges) is split by market regime (trader-regime: NIFTY
     trend or range x India VIX bucket, as of the close before each signal). A strategy whose own regime
     cell has 10+ trades losing on average does not fit today and is passed over.
  3. Live triggers: the lab's own signal engine on the latest bars (lab.forward for WATCH and PROMOTE).
     Each trigger becomes a full plan: entry, stop, target, size (trader-budget), charges, and a
     trader-risk PASS or BLOCK with the reasons. Nothing is written to orders.json.
  4. Probability, honestly: the share of past trades that made money after charges in this regime cell,
     with its sample size and a Wilson 95% interval, shrunk toward the strategy's overall rate with a beta
     prior worth 20 trades (empirical Bayes), plus the expected R after charges. A time-machine replay of
     the stop and target on the stock's own bars is a second opinion; disagreements are flagged.
  5. Ranking: expected R after charges, minus a penalty for a wide interval and for correlation with open
     positions and higher-ranked candidates; ties go to the more liquid stock.
  6. Every candidate's probability is logged (trader/cotrader/log.jsonl, once per candidate) and scored
     when the outcome is known: paper fills, then the lab's forward record, then the next bars. Brier
     score, log loss and reliability bins per strategy and tier; an overconfident strategy is demoted
     through trader-lab's own status file.

Output: trader/cotrader.json, trader/cotrader/plans/<ID>.json (trader-exit-shaped plans for trader-budget),
trader/cotrader/log.jsonl, trader/cotrader/demoted.json. Needs trader-lab next to this folder and uses
trader-regime, trader-budget, trader-risk and trader-timemachine when they are there. Education and
tooling, not financial advice: it never places an order.
"""

import argparse
import bisect
import datetime
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import statistics
import sys
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = "bharat-trader/cotrader/1"
SCHEMA_LOG = "bharat-trader/cotrader-log/1"
SCHEMA_CAL = "bharat-trader/cotrader-calibration/1"
PAISE = Decimal("0.01")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TIERS = ("PROMOTED", "WATCH", "RESEARCH")
TIER_OF_VERDICT = {"PROMOTE": "PROMOTED", "WATCH": "WATCH"}
VIEWS = {"validated": ("PROMOTED", "WATCH"), "research": TIERS}
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{1,47}-[A-Z0-9][A-Z0-9&-]{0,19}$")
OPEN_MIN = 9 * 60 + 15
Z95 = 1.959963984540054

# Every setting, fixed before a run and written into cotrader.json.
SETTINGS = {
    "prior_trades": 20,       # strength of the beta prior (pseudo-trades) at the strategy's overall rate
    "min_cell_trades": 10,    # a regime cell with this many trades can rule a strategy out for today
    "min_oos_trades": 30,     # use the lab's out-of-sample trades when there are this many, else the whole sample
    "width_penalty": 0.25,    # score = EV - width_penalty x (EV interval width) - corr_penalty x max(rho, 0) x EV
    "corr_penalty": 0.5,
    "corr_sessions": 60,      # daily returns used for the correlation
    "liquidity_sessions": 20, # median traded value over these sessions breaks ties
    "replays": 2000,          # time-machine replays per candidate (second opinion)
    "seed": 19,
    "daily_pool": 250,        # sessions of the stock's own daily bars a daily replay draws from
    "daily_hold": 20,         # replay length for a daily rule without a maximum holding
    "similar": 5,             # nearest past trades shown with each candidate
    "min_calibration": 20,    # resolved predictions before a strategy's calibration can demote it
    "bins": 10,               # reliability bins
}
DISCLAIMER = "Education and tooling, not financial advice. Nothing here placed an order."
RESEARCH_LABEL = "unproven, for study/paper only"


class CotraderError(Exception):
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


def pc(x, places=0):
    return "–" if x is None else ("%." + str(places) + "f%%") % (100.0 * x)


def rr(x):
    return "–" if x is None else "%+.2fR" % x


# ---------------------------------------------------------------- sibling skills, loaded by path

def _sibling(folder, script, required=False):
    name = "bt_cotrader_" + script
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(HERE), folder, script + ".py")
    if not os.path.isfile(path):
        if required:
            raise CotraderError("trader-cotrader needs %s installed next to it (%s is missing)" % (folder, path))
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def lab_api():
    """trader-lab (its backtest, forward test, regimes, status files). Required."""
    return _sibling("trader-lab", "lab", required=True)


def budget_api():
    return _sibling("trader-budget", "size")


def risk_api():
    return _sibling("trader-risk", "check")


def tm_api():
    return _sibling("trader-timemachine", "timemachine")


def ist_now(text=None):
    return lab_api().ist_now(text) if text else datetime.datetime.now(IST).replace(tzinfo=None)


# ---------------------------------------------------------------- statistics (pure)

def wilson(k, n, z=Z95):
    """Wilson score interval for k successes in n trials -> (lo, hi), or None for n = 0."""
    if n <= 0:
        return None
    p = float(k) / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def beta_cdf(x, a, b):
    """The regularised incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q, a, b):
    """The q quantile of Beta(a, b), by bisection on beta_cdf (to 1e-10)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def shrink(wins, n, prior_p, prior_n):
    """Empirical Bayes for a hit rate: a Beta(prior_p x prior_n, (1 - prior_p) x prior_n) prior at the
    strategy's overall rate, updated with the cell's wins of n. -> posterior mean, 95% interval, weights."""
    prior_p = min(max(float(prior_p), 1e-6), 1 - 1e-6)
    a0, b0 = prior_p * prior_n, (1 - prior_p) * prior_n
    a, b = wins + a0, (n - wins) + b0
    if a <= 0 or b <= 0:
        return None
    return {"mean": a / (a + b), "lo": beta_ppf(0.025, a, b), "hi": beta_ppf(0.975, a, b), "alpha": round(a, 4),
            "beta": round(b, 4), "weight_on_cell": round(n / float(n + prior_n), 4) if n + prior_n else 0.0}


def mean_sd(xs):
    if not xs:
        return None, None
    m = sum(xs) / float(len(xs))
    sd = statistics.stdev(xs) if len(xs) > 1 else None
    return m, sd


def shrink_mean(cell, overall_mean, overall_sd, prior_n):
    """Expected R shrunk the same way: (sum of the cell's R + prior_n x the overall mean) / (n + prior_n), with a
    normal 95% interval from the cell's spread (the overall spread when the cell has under 2 trades) over
    sqrt(n + prior_n). Approximate: the prior's pseudo-trades are not real trades."""
    n = len(cell)
    if overall_mean is None:
        return None
    m = (sum(cell) + prior_n * overall_mean) / float(n + prior_n)
    sd = statistics.stdev(cell) if n > 1 else overall_sd
    if sd is None:
        return {"mean": m, "lo": None, "hi": None}
    half = Z95 * sd / math.sqrt(n + prior_n)
    return {"mean": m, "lo": m - half, "hi": m + half}


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / float(len(ps)) if ps else None


def log_loss(ps, ys, eps=1e-6):
    if not ps:
        return None
    tot = 0.0
    for p, y in zip(ps, ys):
        p = min(max(p, eps), 1 - eps)
        tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return tot / float(len(ps))


def reliability(ps, ys, bins=10):
    """Equal-width bins of predicted p: [{lo, hi, n, mean_p, observed, ci}] for the bins that hold any."""
    rows = []
    for k in range(bins):
        lo, hi = k / float(bins), (k + 1) / float(bins)
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (k == bins - 1 and p == 1.0)]
        if not idx:
            continue
        hit = sum(ys[i] for i in idx)
        ci = wilson(hit, len(idx))
        rows.append({"lo": lo, "hi": hi, "n": len(idx), "mean_p": round(sum(ps[i] for i in idx) / len(idx), 4),
                     "observed": round(hit / float(len(idx)), 4), "ci": [round(ci[0], 4), round(ci[1], 4)]})
    return rows


def score_group(ps, ys, bins=10):
    """Brier, log loss, the base-rate reference and reliability for one group of resolved predictions."""
    n = len(ps)
    if not n:
        return {"n": 0}
    hit = sum(ys)
    base = hit / float(n)
    ref = base * (1 - base)
    b = brier(ps, ys)
    ci = wilson(hit, n)
    return {"n": n, "brier": round(b, 4), "log_loss": round(log_loss(ps, ys), 4),
            "base_rate": round(base, 4), "brier_reference": round(ref, 4),
            "skill": round(1 - b / ref, 4) if ref > 0 else None,
            "mean_p": round(sum(ps) / n, 4), "observed": round(base, 4),
            "observed_ci": [round(ci[0], 4), round(ci[1], 4)], "bins": reliability(ps, ys, bins)}


def correlation(a, b):
    """Pearson correlation of two equal-length lists, or None."""
    n = len(a)
    if n < 10 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


# ---------------------------------------------------------------- the evidence for one strategy in one regime

def cell_of(key):
    """'trend|low|no expiry' -> 'trend|low' (trend or range x vol bucket: the lab's regime gate cell)."""
    return "|".join(str(key or "unknown|unknown").split("|")[:2])


def cell_words(cell):
    t, v = (cell.split("|") + ["unknown"])[:2]
    return "%s · %s vol" % ({"trend": "NIFTY trending", "range": "NIFTY ranging"}.get(t, "regime unknown"), v)


def label(trades, regs):
    """[(trade, cell)] by trader-regime as of the close before each signal (cell 'unknown|unknown' without)."""
    lab = lab_api()
    api = lab.regime_api()
    if not regs or api is None:
        return [(t, "unknown|unknown") for t in trades]
    return [(t, cell_of(k)) for t, k, _ in api.label_trades(trades, regs)]


MSG = {
    "en": {
        "ev_why": "{wins} of {n} past trades in {cell} made money after charges ({raw}, Wilson 95% {lo} to {hi}); all "
                  "regimes {wins_all} of {n_all} ({p_all}). Shrunk with a prior worth {k} trades: {p} (95% {p_lo} to "
                  "{p_hi}), expected {ev} a trade after charges.",
        "ev_none": "no past trades to learn from",
        "d_past": "It is the past: {n} trades from {start} to {end}. A probability of {p} means about that share of "
                  "similar past trades made money after charges, not that this one will.",
        "d_thin": "Only {n} past trades in today's regime ({cell}): the number leans mostly on the strategy's overall "
                  "rate ({w}% weight on this regime).",
        "d_research": "UNPROVEN: this strategy has not passed trader-lab's gates ({why}). Study it or paper trade it; "
                      "it is never eligible for a live order.",
        "d_watch": "WATCH: the backtest gates passed but the paper forward test is still running. Paper only.",
        "d_fills": "Fills, slippage and charges are models (next-bar fills, {bps} bps slippage a side). A real fill "
                   "can be worse, and a gap can jump the stop.",
        "d_survivor": "The universe is today's liquid stocks applied to the whole history (survivorship bias "
                      "flatters it).",
        "d_news": "It knows nothing about today's news, results or events: check trader-news and trader-regime's "
                  "calendar before acting.",
        "d_replay": "The time-machine replay plays the stop and target only, not the strategy's own exit rule.",
        "e_when_1d": "on the {day} close, for the next session's open",
        "e_when_5m": "on the 5-minute bar closing {time} IST",
        "e_why_desc": "{title} fired on {sym} {when}. {desc}.",
        "e_why": "{title} fired on {sym} {when}.",
        "e_stop_label": "the spec's stop rule",
        "e_stop": "A trade at or below the stop {stop} ({label}) is out: that is the planned loss.",
        "e_gap": "If the next session opens at or below {stop}, skip it: the lab never fills through the stop.",
        "e_flat": "Out by the {time} flat-by whatever happens (intraday).",
        "e_bars": "Out after {n} bars at most, or earlier on the strategy's exit rule.",
        "e_regime": "A change of market regime away from {cell} before the entry removes the regime evidence.",
        "e_tm": " Time machine: {green} green, {target} target first, {stop} stop first, {net} after charges "
                "({method}).",
        "ne_headline": "No trade today: sitting out is the decision, not a failure.",
        "ne_no_gates": "No validated strategy: none of the {n} lab runs has passed the lab's gates ({verdicts}).",
        "ne_no_lab": "No validated strategy: no strategy has been run in the lab yet (trader/lab/ is empty).",
        "ne_ch_lab": "A strategy that passes trader-lab's gates (lab.py run NAME) or a trader-discover finalist, then "
                     "its paper forward test (lab.py forward NAME), becomes a WATCH candidate here.",
        "ne_mismatch": "{name} does not fit today's market: {why}.",
        "ne_ch_cells": "{name} had an edge in: {cells}. A move into one of those would bring it back.",
        "ne_quiet": "No live trigger today for {names}.",
        "ne_ch_quiet": "A fresh signal on the next bars (re-run this after the next close, or during the session for "
                       "intraday rules).",
        "ne_noev": "{n} trigger(s) with no edge after charges in this regime.",
        "ne_blocked": "{n} candidate(s) blocked by your own rules: {items}.",
        "ne_locks": "Risk locks on new entries: {locks}.",
        "ne_ch_locks": "The kill switch and circuit breakers reset with the next session (or lift the kill switch "
                       "yourself with check.py unkill).",
        "ne_hidden": "{n} RESEARCH strategies are hidden: today --tier research shows their ideas, labelled {label}.",
        "ne_research": "RESEARCH ideas: none ready either ({n} strategies: {mis} regime mismatch, {quiet} no live "
                       "trigger, {other} other).",
        "ne_nothing": "Nothing qualified today.",
        "headline": "{n} candidate(s) passed your risk check ({tiers}); each still needs your typed yes on the paper "
                    "account.",
        "head": "Co-trader, {as_of} IST ({view})",
        "view_validated": "PROMOTED + WATCH",
        "view_all": "all tiers, RESEARCH included",
        "market": "Market: {words}",
        "options_line": "  {structure} on {date}, expiry {expiry}: {legs} legs, margin about {margin}, max loss after "
                        "charges {loss}",
        "unlimited": "unlimited",
        "buy_line": "  buy {product} on {date}{when}: entry {entry}, stop {stop}, target {target}",
        "when_after": " after {time} IST",
        "when_open": " at the open",
        "size_line": "  size {qty} shares ({value}), loss at the stop {loss} with charges; charges about {charges} at "
                     "the target",
        "odds": "  odds: {p} made money after charges (95% {lo} to {hi}), n = {n} in {cell}, {n_all} in all; expected "
                "{ev} a trade{rupees}",
        "odds_rupees": " (about {x})",
        "tm_line": "  time machine: {green} green, {net} after charges",
        "disagrees": "  DISAGREES: {text}",
        "risk_line": "  trader-risk: {verdict}{why}",
        "not_checked": "not checked",
        "corr": "  correlation {rho} with {other} (score {score})",
        "why_line": "  why: {text}",
        "explain_cmd": "  explain: cotrader.py explain {id}",
        "why_head": "Why:",
        "change_head": "What would change that:",
        "passed_head": "Passed over:",
        "strategies_head": "Strategies:",
        "note": "Note: {text}",
        "footer": "Every order still needs trader-risk PASS and your typed yes; RESEARCH ideas are never for a live "
                  "order.",
        "disclaimer": "Education and tooling, not financial advice. Nothing here placed an order.",
        "wrote": "Wrote trader/cotrader.json ({n} candidate(s), {logged} newly logged).",
        "x_why": "Why this trade: {text}",
        "x_inval": "What would invalidate it:",
        "x_evidence": "Evidence ({basis}): {text}",
        "x_dns": "What the evidence does NOT show:",
        "x_similar": "Nearest similar past trades:",
        "x_risk": "trader-risk preview ({as_of}): {verdict}",
        "x_paper_opt": "To paper trade it: the basket in trader/cotrader/plans/{id}.json (\"orders\") becomes "
                       "trader/orders.json through the UI's Draft to paper or paper_order_draft, then check.py and "
                       "one typed yes for the whole basket.",
        "x_paper": "To paper trade it: copy trader/cotrader/plans/{id}.json to trader/plan.json, then size.py "
                   "--write, check.py, and ticket.py show for your typed yes (or the UI's Draft to paper).",
    },
    "hi-en": {
        "ev_why": "{cell} mein {n} purane trades mein se {wins} ne charges ke baad profit kiya ({raw}, Wilson 95% {lo} "
                  "se {hi}); saare regimes mein {n_all} mein se {wins_all} ({p_all}). {k} trades ke prior se shrink "
                  "karke: {p} (95% {p_lo} se {p_hi}), charges ke baad har trade se expected {ev}.",
        "ev_none": "seekhne ke liye koi purane trades nahi",
        "d_past": "Yeh sab past hai: {start} se {end} tak ke {n} trades. {p} probability ka matlab hai ki milte-julte "
                  "purane trades mein se lagbhag itne charges ke baad profit mein rahe, yeh nahi ki yeh trade bhi "
                  "rahega.",
        "d_thin": "Aaj ke regime ({cell}) mein sirf {n} purane trades: yeh number zyaadatar strategy ke overall rate "
                  "pe tika hai (is regime ko {w}% weight).",
        "d_research": "UNPROVEN: is strategy ne trader-lab ke gates pass nahi kiye ({why}). Ise study kijiye ya paper "
                      "trade kijiye; yeh kabhi bhi live order ke liye eligible nahi hai.",
        "d_watch": "WATCH: backtest gates pass ho gaye, par paper forward test abhi chal raha hai. Sirf paper.",
        "d_fills": "Fills, slippage aur charges model hain (next-bar fills, har side {bps} bps slippage). Asli fill "
                   "isse bura ho sakta hai, aur gap stop ke paar bhi ja sakta hai.",
        "d_survivor": "Universe aaj ke liquid stocks ka hai, jo poori history pe lagaya gaya (survivorship bias ise "
                      "behtar dikhata hai).",
        "d_news": "Ise aaj ki news, results ya events ka kuch pata nahi: kuch bhi karne se pehle trader-news aur "
                  "trader-regime ka calendar check kijiye.",
        "d_replay": "Time-machine replay sirf stop aur target chalata hai, strategy ka apna exit rule nahi.",
        "e_when_1d": "{day} ke close pe, agle session ke open ke liye",
        "e_when_5m": "{time} IST pe band hue 5-minute bar pe",
        "e_why_desc": "{title} ka signal {sym} pe aaya, {when}. {desc}.",
        "e_why": "{title} ka signal {sym} pe aaya, {when}.",
        "e_stop_label": "spec ka stop rule",
        "e_stop": "Price stop {stop} ({label}) pe ya usse neeche gaya to trade se bahar: yahi planned loss hai.",
        "e_gap": "Agar agla session {stop} pe ya usse neeche khule, to ise chhod dijiye: lab kabhi stop ke paar fill "
                 "nahi karta.",
        "e_flat": "{time} flat-by tak bahar, kuch bhi ho (intraday).",
        "e_bars": "Zyada se zyada {n} bars ke baad bahar, ya strategy ke exit rule pe usse pehle.",
        "e_regime": "Entry se pehle market regime {cell} se badal gaya, to regime wala evidence khatam.",
        "e_tm": " Time machine: {green} green, {target} mein pehle target, {stop} mein pehle stop, charges ke baad "
                "{net} ({method}).",
        "ne_headline": "Aaj koi trade nahi: baith kar intezaar karna bhi ek faisla hai, galti nahi.",
        "ne_no_gates": "Koi validated strategy nahi: {n} lab runs mein se koi bhi lab ke gates pass nahi kar paaya "
                       "({verdicts}).",
        "ne_no_lab": "Koi validated strategy nahi: lab mein abhi tak koi strategy run nahi hui (trader/lab/ khaali "
                     "hai).",
        "ne_ch_lab": "Jo strategy trader-lab ke gates pass kare (lab.py run NAME) ya trader-discover ka finalist ho, "
                     "aur phir uska paper forward test bhi (lab.py forward NAME), woh yahan WATCH candidate banegi.",
        "ne_mismatch": "{name} aaj ke market mein fit nahi hoti: {why}.",
        "ne_ch_cells": "{name} ka edge in regimes mein tha: {cells}. Market inme se kisi mein aaye to yeh wapas aa "
                       "sakti hai.",
        "ne_quiet": "Aaj {names} ka koi live trigger nahi.",
        "ne_ch_quiet": "Agle bars pe naya signal (agle close ke baad ise dobara chalaiye, ya intraday rules ke liye "
                       "session ke dauraan).",
        "ne_noev": "{n} trigger(s) aaye, par is regime mein charges ke baad koi edge nahi.",
        "ne_blocked": "{n} candidate(s) aapke apne rules ne block kiye: {items}.",
        "ne_locks": "Nayi entries pe risk locks lage hain: {locks}.",
        "ne_ch_locks": "Kill switch aur circuit breakers agle session mein reset hote hain (ya kill switch khud "
                       "check.py unkill se hataiye).",
        "ne_hidden": "{n} RESEARCH strategies chhupi hui hain: today --tier research unke ideas dikhata hai (label: "
                     "{label}).",
        "ne_research": "RESEARCH ideas: inme se bhi koi ready nahi ({n} strategies: {mis} regime mismatch, {quiet} no "
                       "live trigger, {other} other).",
        "ne_nothing": "Aaj kuch bhi qualify nahi hua.",
        "headline": "{n} candidate(s) aapke risk check mein pass hue ({tiers}); har ek ko ab bhi paper account pe aapka "
                    "typed yes chahiye.",
        "head": "Co-trader, {as_of} IST ({view})",
        "view_validated": "PROMOTED + WATCH",
        "view_all": "saare tiers, RESEARCH bhi",
        "market": "Market: {words}",
        "options_line": "  {structure}, {date}, expiry {expiry}: {legs} legs, margin lagbhag {margin}, charges ke baad "
                        "max loss {loss}",
        "unlimited": "unlimited",
        "buy_line": "  buy {product}, {date}{when}: entry {entry}, stop {stop}, target {target}",
        "when_after": ", {time} IST ke baad",
        "when_open": ", open pe",
        "size_line": "  size {qty} shares ({value}), stop pe charges milakar nuksaan {loss}; target pe charges lagbhag "
                     "{charges}",
        "odds": "  odds: charges ke baad {p} mein profit (95% range {lo} se {hi}), {cell} mein n = {n}, kul {n_all}; "
                "har trade se expected {ev}{rupees}",
        "odds_rupees": " (lagbhag {x})",
        "tm_line": "  time machine: {green} green, charges ke baad {net}",
        "disagrees": "  MEL NAHI KHAATA: {text}",
        "risk_line": "  trader-risk: {verdict}{why}",
        "not_checked": "check nahi hua",
        "corr": "  {other} ke saath correlation {rho} (score {score})",
        "why_line": "  kyun: {text}",
        "explain_cmd": "  poori baat: cotrader.py explain {id}",
        "why_head": "Kyun:",
        "change_head": "Kya badle to yeh badlega:",
        "passed_head": "Chhod diye gaye:",
        "strategies_head": "Strategies:",
        "note": "Dhyan dein: {text}",
        "footer": "Har order ko ab bhi trader-risk PASS aur aapka typed yes chahiye; RESEARCH ideas kabhi bhi live "
                  "order ke liye nahi hain.",
        "disclaimer": "Yeh education aur tooling hai, financial advice nahi. Yahan se koi order place nahi hua.",
        "wrote": "trader/cotrader.json likh diya ({n} candidate(s), {logged} naye log hue).",
        "x_why": "Yeh trade kyun: {text}",
        "x_inval": "Yeh kab galat saabit hoga:",
        "x_evidence": "Evidence ({basis}): {text}",
        "x_dns": "Evidence kya NAHI dikhata:",
        "x_similar": "Sabse milte-julte purane trades:",
        "x_risk": "trader-risk preview ({as_of}): {verdict}",
        "x_paper_opt": "Paper trade karne ke liye: trader/cotrader/plans/{id}.json ka basket (\"orders\") UI ke Draft "
                       "to paper ya paper_order_draft se trader/orders.json banta hai, phir check.py aur poore basket "
                       "ke liye ek typed yes.",
        "x_paper": "Paper trade karne ke liye: trader/cotrader/plans/{id}.json ko trader/plan.json mein copy kijiye, "
                   "phir size.py --write, check.py, aur aapke typed yes ke liye ticket.py show (ya UI ka Draft to "
                   "paper).",
    },
}


# ---------------------------------------------------------------- message catalogue helper (same in every skill with MSG)
LANGS = ("en", "hi-en")


def pick_lang(flag=None, cfg=None):
    """The output language: --lang beats the config's "lang"; "hi-en" is Hinglish (Roman script), else English."""
    for v in (flag, cfg.get("lang") if isinstance(cfg, dict) else None):
        v = str(v or "").strip().lower().replace("_", "-")
        if v == "hinglish":
            v = "hi-en"
        if v in LANGS:
            return v
    return "en"


def t(key, lang="en", **kw):
    """MSG[lang][key] filled with kw (str.format); the English text when a language or key is missing."""
    text = (MSG.get(lang) or {}).get(key)
    if text is None:
        text = MSG["en"][key]
    return text.format(**kw) if kw else text
# ---------------------------------------------------------------- end of message catalogue helper


def lang_for(workdir, flag=None):
    """--lang, else trader/config.json's "lang", else English."""
    return pick_lang(flag, read_json(os.path.join(os.path.abspath(workdir or "."), "trader", "config.json")))


def evidence_words(ev, lang="en"):
    """ev["why"] again, in lang, from the numbers evidence() keeps."""
    if not ev.get("trades_all"):
        return t("ev_none", lang)
    n, ci = ev["trades_cell"], ev.get("p_cell_ci")
    return t("ev_why", lang, wins=ev["wins_cell"], n=n, cell=ev["cell_words"],
             raw=pc(ev.get("p_cell_raw")) if n else "–", lo=pc(ci[0]) if ci else "–", hi=pc(ci[1]) if ci else "–",
             wins_all=ev["wins_all"], n_all=ev["trades_all"], p_all=pc(ev.get("p_all")), k=ev.get("prior_trades"),
             p=pc(ev.get("p")), p_lo=pc(ev.get("p_lo")), p_hi=pc(ev.get("p_hi")), ev=rr(ev.get("ev_r")))


def evidence(trades, regs, cell, settings=None, basis=""):
    """The odds for a trade of this strategy in this regime cell, from its past trades (net after charges).

    hit = a trade that made money after charges. The cell's hit rate gets a Wilson 95% interval, then is
    shrunk toward the strategy's overall rate with a beta prior worth min(prior_trades, all trades) trades.
    The expected R after charges is shrunk the same way."""
    st = dict(SETTINGS, **(settings or {}))
    labelled = label(trades, regs)
    rs_all = [float(t["r"]) for t, _ in labelled]
    wins_all = sum(1 for t, _ in labelled if float(t["net"]) > 0)
    n_all = len(labelled)
    in_cell = [t for t, c in labelled if c == cell]
    rs = [float(t["r"]) for t in in_cell]
    wins = sum(1 for t in in_cell if float(t["net"]) > 0)
    n = len(in_cell)
    out = {"basis": basis, "cell": cell, "cell_words": cell_words(cell), "trades_all": n_all, "wins_all": wins_all,
           "trades_cell": n, "wins_cell": wins, "from": None, "to": None}
    if trades:
        out["from"] = min(t["signal_date"] for t in trades)
        out["to"] = max(t["signal_date"] for t in trades)
    if not n_all:
        out.update({"p": None, "p_lo": None, "p_hi": None, "ev_r": None, "ev_lo": None, "ev_hi": None,
                    "prior_trades": 0, "why": t("ev_none")})
        return out
    p_all = wins_all / float(n_all)
    p_prior = (wins_all + 1) / float(n_all + 2)      # Laplace: a few all-win (or all-loss) trades never mean 100%
    m_all, sd_all = mean_sd(rs_all)
    k = min(int(st["prior_trades"]), n_all)
    post = shrink(wins, n, p_prior, k)
    ev = shrink_mean(rs, m_all, sd_all, k)
    w_all, w_cell = wilson(wins_all, n_all), wilson(wins, n)
    m_cell, _ = mean_sd(rs)
    out.update({
        "p_all": round(p_all, 4), "p_all_ci": [round(w_all[0], 4), round(w_all[1], 4)], "p_prior": round(p_prior, 4),
        "ev_r_all": round(m_all, 4),
        "p_cell_raw": round(wins / float(n), 4) if n else None,
        "p_cell_ci": [round(w_cell[0], 4), round(w_cell[1], 4)] if w_cell else None,
        "ev_r_cell_raw": round(m_cell, 4) if m_cell is not None else None,
        "prior_trades": k, "weight_on_cell": post["weight_on_cell"],
        "p": round(post["mean"], 4), "p_lo": round(post["lo"], 4), "p_hi": round(post["hi"], 4),
        "ev_r": round(ev["mean"], 4), "ev_lo": None if ev["lo"] is None else round(ev["lo"], 4),
        "ev_hi": None if ev["hi"] is None else round(ev["hi"], 4),
        "fits": None if n < int(st["min_cell_trades"]) else bool(m_cell > 0),
    })
    out["why"] = t("ev_why", wins=wins, n=n, cell=cell_words(cell), raw=pc(out["p_cell_raw"]) if n else "–",
                   lo=pc(w_cell[0]) if w_cell else "–", hi=pc(w_cell[1]) if w_cell else "–", wins_all=wins_all,
                   n_all=n_all, p_all=pc(p_all), k=k, p=pc(out["p"]), p_lo=pc(out["p_lo"]), p_hi=pc(out["p_hi"]),
                   ev=rr(out["ev_r"]))
    return out


def edge_cells(trades, regs, min_trades):
    """The regime cells where this strategy's past trades average above 0R with min_trades or more."""
    groups = {}
    for t, c in label(trades, regs):
        groups.setdefault(c, []).append(float(t["r"]))
    return sorted(c for c, rs in groups.items() if len(rs) >= min_trades and sum(rs) / len(rs) > 0
                  and c != "unknown|unknown")


def current_cell(regs, day):
    """The regime cell a signal on `day` belongs to (the close before it), and that record's words."""
    lab = lab_api()
    api = lab.regime_api()
    if not regs or api is None:
        return "unknown|unknown", "regime unknown (no NIFTY bars or no trader-regime)", None
    (t, key, rec), = api.label_trades([{"signal_date": day}], regs)
    return cell_of(key), (rec or {}).get("words") or cell_words(cell_of(key)), (rec or {}).get("date")


# ---------------------------------------------------------------- similar past trades

def similar_trades(trades, regs, symbol, cell, entry, stop, k=5):
    """The k past trades nearest this one: same stock, same regime cell, similar stop distance, then recency."""
    if not trades:
        return []
    risk0 = (entry - stop) / entry if entry else 0.0
    rows = []
    for t, c in label(trades, regs):
        e, s = float(t.get("entry") or 0), float(t.get("stop") or 0)
        rp = (e - s) / e if e else 0.0
        d = (0.0 if t["symbol"] == symbol else 1.0) + (0.0 if c == cell else 1.0) + \
            (min(1.0, abs(rp - risk0) / risk0) if risk0 > 0 else 1.0)
        rows.append((round(d, 4), t["signal_date"], t, c))
    rows.sort(key=lambda x: x[1], reverse=True)       # the most recent first among equals
    rows.sort(key=lambda x: x[0])
    return [{"symbol": t["symbol"], "signal_date": t["signal_date"], "exit_date": t["exit_date"], "regime": cell_words(c),
             "entry": t.get("entry"), "stop": t.get("stop"), "exit": t.get("exit"), "r": t["r"], "net": t["net"],
             "reason": t["reason"], "distance": d} for d, _, t, c in rows[:k]]


# ---------------------------------------------------------------- the time-machine second opinion

def _ratios(bars):
    return [(b["o"] / p["c"], b["h"] / p["c"], b["l"] / p["c"], b["c"] / p["c"]) for p, b in zip(bars, bars[1:])
            if p["c"] > 0]


def second_opinion(tm, plan, series, timeframe, qty, cfg, max_bars=None, settings=None):
    """trader-timemachine's Monte Carlo on the stock's own bars before the plan's day: the share of replays
    that hit the target first or end above the entry ("green"), and the average R after charges."""
    st = dict(SETTINGS, **(settings or {}))
    if tm is None:
        return {"available": False, "why": "trader-timemachine is not installed next to this skill"}
    if series is None or not series.n:
        return {"available": False, "why": "no bars for the replay"}
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    if not (stop < entry < target):
        return {"available": False, "why": "the plan's levels are not stop < entry < target"}
    day = plan["date"]
    try:
        if timeframe == "5m":
            flat = plan.get("flat_by") or "15:10"
            fh, fm = [int(x) for x in flat.split(":")]
            last = (fh * 60 + fm - OPEN_MIN) // 5 - 1
            eh, em = [int(x) for x in str(plan.get("entry_time") or "09:20").split(":")]
            k = max(0, (eh * 60 + em - OPEN_MIN) // 5 - 1)
            if k >= last:
                return {"available": False, "why": "no bars left between the entry and the flat-by"}
            bars = [{"date": series.date[i], "slot": (series.minute[i] - OPEN_MIN) // 5,
                     "start": "%02d:%02d" % divmod(series.minute[i], 60), "o": series.o[i], "h": series.h[i],
                     "l": series.l[i], "c": series.c[i], "v": series.v[i]}
                    for i in range(series.n) if series.date[i] < day and series.minute[i] >= OPEN_MIN]
            days = tm.split_sessions(bars)
            sessions, _ = tm.usable_sessions(days, day, last, cfg, "EQ")
            if len(sessions) < 20:
                return {"available": False, "why": "only %d full past sessions of 5-minute bars (20 needed)"
                        % len(sessions)}
            pool = tm.build_pool(sessions, last)
            mc = tm.simulate(pool, entry, stop, target, k, last, int(st["replays"]), int(st["seed"]), 2, "buy",
                             qty or None, cfg)
            method = ("%d replays of the rest of the day from %d past sessions of 5-minute bars, to the %s flat-by "
                      "(seed %d)" % (mc["replays"], len(sessions), flat, mc["seed"]))
        else:
            bars = [{"o": series.o[i], "h": series.h[i], "l": series.l[i], "c": series.c[i]}
                    for i in range(series.n) if series.date[i] <= day][-(int(st["daily_pool"]) + 1):]
            ratios = _ratios(bars)
            if len(ratios) < 60:
                return {"available": False, "why": "only %d past daily bars (60 needed)" % len(ratios)}
            hold = int(max_bars or st["daily_hold"])
            pool = [[]] + [ratios] * hold
            mc = tm.simulate(pool, entry, stop, target, 0, hold, int(st["replays"]), int(st["seed"]), 0, "buy",
                             qty or None, cfg)
            method = ("%d replays of %d sessions drawn from the stock's last %d daily bars, stop and target only "
                      "(the strategy's own exit rule is not replayed; seed %d)"
                      % (mc["replays"], hold, len(ratios), mc["seed"]))
    except (ValueError, KeyError, ZeroDivisionError, IndexError) as e:
        return {"available": False, "why": "the replay failed: %s" % e}
    return {"available": True, "method": method, "p_green": mc["p_green"], "p_target": mc["p_target"],
            "p_stop": mc["p_stop"], "p_neither": mc["p_neither"], "avg_r": mc["avg_R"], "avg_r_net": mc["avg_R_net"],
            "seed": mc["seed"], "replays": mc["replays"]}


def disagreements(ev, so):
    """Where the replay and the trade history do not agree, in plain words."""
    out = []
    if not so.get("available") or ev.get("p") is None:
        return out
    g = so["p_green"]
    if g < ev["p_lo"] or g > ev["p_hi"]:
        out.append("the time-machine replay puts it at %s green, outside the evidence's 95%% interval (%s to %s)"
                   % (pc(g), pc(ev["p_lo"]), pc(ev["p_hi"])))
    a = so.get("avg_r_net")
    if a is not None and ev.get("ev_r") is not None and (a > 0) != (ev["ev_r"] > 0) and abs(a - ev["ev_r"]) > 0.05:
        out.append("the replay's average after charges is %s, the other side of zero from the history's %s"
                   % (rr(a), rr(ev["ev_r"])))
    return out


# ---------------------------------------------------------------- bars, positions, liquidity

class Sources(object):
    """Where bars come from: --data DIR (offline files), else the local archive, else the lab's Yahoo cache
    (fetching only when allowed). One load per symbol and timeframe."""

    def __init__(self, workdir=".", data=None, archive=None, fetch=True, lab_out=None):
        self.workdir = os.path.abspath(workdir)
        self.data = data
        arch = archive if archive is not None else (os.environ.get("BT_ARCHIVE") or
                                                    os.path.join(self.workdir, "trader", "archive"))
        self.archive = arch if arch and os.path.isdir(arch) else None
        self.fetch = bool(fetch) and not data
        self.lab_out = lab_out or os.path.join(self.workdir, "trader", "lab")
        self.cache = os.path.join(self.lab_out, "data")
        self.memo = {}
        self.notes = []

    def series(self, sym, tf, now):
        key = (sym, tf)
        if key not in self.memo:
            lab = lab_api()
            try:
                bars, _ = lab.load_bars(sym, tf, self.data, self.cache, self.fetch, now, archive=self.archive)
                bars = lab.seen_by(bars, tf, now)
                self.memo[key] = lab.Series(sym, tf, bars) if bars else None
            except lab.LabError as e:
                self.memo[key] = None
                self.notes.append("%s %s bars: %s" % (sym, tf, e))
        return self.memo[key]

    def market(self, spec, now):
        data = {}
        for sym in spec["universe"]:
            s = self.series(sym, spec["timeframe"], now)
            if s is not None:
                data[sym] = s
        return data

    def context(self, now):
        return {"nifty": self.series("^NSEI", "1d", now), "vix": self.series("^INDIAVIX", "1d", now)}


def daily_returns(series, n):
    """{date: close-to-close return} over the last n sessions."""
    if series is None or series.n < 2:
        return {}
    out = {}
    for i in range(max(1, series.n - n), series.n):
        if series.c[i - 1] > 0:
            out[series.date[i]] = series.c[i] / series.c[i - 1] - 1
    return out


def corr_of(r1, r2):
    days = sorted(set(r1) & set(r2))
    return correlation([r1[d] for d in days], [r2[d] for d in days])


def liquidity(series, n):
    """Median daily traded value (close x volume) over the last n sessions, in rupees, or None."""
    if series is None or not series.n:
        return None
    vals = [series.c[i] * series.v[i] for i in range(max(0, series.n - n), series.n) if series.v[i] > 0]
    return round(statistics.median(vals), 2) if vals else None


def open_positions(portfolio):
    """The symbols held on the paper account (trader/portfolio.json)."""
    pos = (portfolio or {}).get("positions") if isinstance(portfolio, dict) else None
    if isinstance(pos, dict):
        return sorted(str(k).upper() for k, v in pos.items() if not isinstance(v, dict) or (v.get("qty") or 0))
    if isinstance(pos, list):
        return sorted(str(r.get("symbol")).upper() for r in pos if isinstance(r, dict) and r.get("symbol"))
    return []


# ---------------------------------------------------------------- ranking (pure)

def rank(cands, returns, held, settings=None):
    """Order candidates by evidence tier (PROMOTED, WATCH, RESEARCH), then by score = EV - width_penalty x width -
    corr_penalty x max(rho, 0) x max(EV, 0), greedily: rho is the highest correlation with an open position or a
    candidate already ranked above (the same stock counts as 1). Ties (to 4 decimals) go to the higher liquidity.
    The score is a ranking index in R, not an expected result. Each candidate gets "score", "rho", "rho_with"."""
    st = dict(SETTINGS, **(settings or {}))
    left = list(cands)
    done = []
    while left:
        best = None
        for c in left:
            ev = c["evidence"]["ev_r"] or 0.0
            lo, hi = c["evidence"].get("ev_lo"), c["evidence"].get("ev_hi")
            width = (hi - lo) if (lo is not None and hi is not None) else 1.0
            rho, rho_with = 0.0, None
            mine = returns.get(c["symbol"]) or {}
            others = [(s, "open position") for s in held] + [(d["symbol"], "candidate %s" % d["id"]) for d in done]
            for s, what in others:
                if s == c["symbol"]:
                    rho_s = 1.0
                else:
                    rho_s = corr_of(mine, returns.get(s) or {})
                if rho_s is not None and rho_s > rho:
                    rho, rho_with = rho_s, "%s (%s)" % (s, what)
            score = ev - float(st["width_penalty"]) * width - float(st["corr_penalty"]) * max(rho, 0.0) * max(ev, 0.0)
            tier = -TIERS.index(c["tier"]) if c.get("tier") in TIERS else -len(TIERS)
            key = (tier, round(score, 4), c.get("liquidity") or 0.0)
            if best is None or key > best[0]:
                best = (key, c, score, rho, rho_with, width)
        _, c, score, rho, rho_with, width = best
        c["score"], c["rho"], c["rho_with"], c["ev_width"] = round(score, 4), round(rho, 4), rho_with, round(width, 4)
        done.append(c)
        left.remove(c)
    for i, c in enumerate(done):
        c["rank"] = i + 1
    return done


# ---------------------------------------------------------------- strategies and tiers

def demoted_path(workdir):
    return os.path.join(workdir, "trader", "cotrader", "demoted.json")


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise CotraderError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def strategies(lab_out, include_library=False):
    """Every lab run (trader/lab/<name>/ with results.json) and, for research, the library specs never run."""
    lab = lab_api()
    rows, names = [], []
    if os.path.isdir(lab_out):
        for n in sorted(os.listdir(lab_out)):
            d = os.path.join(lab_out, n)
            if not os.path.isfile(os.path.join(d, "results.json")):
                continue
            try:
                res = lab.read_json(os.path.join(d, "results.json"))
                st = lab.read_json(os.path.join(d, "status.json"), {}) or {}
                doc, _ = lab.resolve_spec(n, lab_out)
            except lab.LabError:
                continue
            names.append(n)
            rows.append({"name": n, "doc": doc, "res": res, "status": st, "source": "lab",
                         "verdict": st.get("verdict") or res.get("verdict"),
                         "verdict_why": st.get("why") or res.get("verdict_why") or ""})
    if include_library:
        for n, doc in sorted(lab.library().items()):
            if n not in names:
                rows.append({"name": n, "doc": doc, "res": None, "status": {}, "source": "library",
                             "verdict": None, "verdict_why": "never run in the lab"})
    return rows


def tier_of(row, demoted):
    """(tier, why)."""
    name = row["name"]
    if name in demoted:
        return "RESEARCH", "demoted by the co-trader for poor calibration: %s" % demoted[name].get("why", "")
    v = row.get("verdict")
    if v in TIER_OF_VERDICT:
        return TIER_OF_VERDICT[v], ("lab %s: %s" % (v, row.get("verdict_why") or "")).strip()
    if row["source"] == "library":
        return "RESEARCH", "a library spec never run in the lab: no gates, no walk-forward"
    return "RESEARCH", "lab %s: %s" % (v or "no verdict", row.get("verdict_why") or "")


def history(row, data, fno, settings):
    """(past trades after charges, the words for what they are)."""
    lab = lab_api()
    res = row.get("res")
    if res:
        oos, full = res.get("oos_trades") or [], res.get("trades") or []
        if not oos and not full:
            return [], "the lab run kept no trade list (results.json without trades)"
        if len(oos) >= int(settings["min_oos_trades"]):
            return oos, ("%d out-of-sample walk-forward trades from the lab run (%s to %s)"
                         % (len(oos), res["data"].get("tested_from"), res["data"].get("to")))
        return full, ("%d whole-sample trades from the lab run at the spec's own parameters (only %d out of sample: "
                      "too few on their own; in-sample trades flatter)" % (len(full), len(oos)))
    if not data:
        return [], "no bars to backtest"
    spec = lab.validate_spec(row["doc"])
    bt = lab.backtest(spec, data, 1.0, fno)
    trades = [t for t in bt["trades"] if t["reason"] != "end of data"]
    return trades, ("%d trades from a plain backtest at the spec's own parameters on these bars: never run in the "
                    "lab, so no walk-forward, no gates, all in sample" % len(trades))


def options_triggers(row, chains, now, lab_out, tier):
    """(baskets, notes, live verdict, signal day) for an options spec ("kind": "options") through trader-lab's
    options forward test, which drafts a trader-strategy basket for a WATCH or PROMOTE spec whose entry fires on
    the latest session. A RESEARCH options spec shows its history only: the lab drafts no basket for it."""
    lab = lab_api()
    if tier not in ("PROMOTED", "WATCH"):
        return [], ["%s: RESEARCH options strategy: stats only (trader-lab drafts baskets for WATCH and PROMOTE "
                    "only)" % row["name"]], row.get("verdict"), None
    try:
        r = lab.forward(row["doc"], chains, lab_out, None, None, now, write=False)
    except lab.LabError as e:
        return [], ["%s: %s" % (row["name"], e)], row.get("verdict"), None
    return r.get("baskets") or [], r.get("notes") or [], r["verdict"], r["today"]


def triggers(row, data, fno, now, lab_out, tier):
    """(plans, notes, live verdict, compiled spec, signal day) for one strategy on the latest bars."""
    lab = lab_api()
    doc = row["doc"]
    if not data:
        return [], ["%s: no bars for any symbol" % row["name"]], row.get("verdict"), None, None
    days = lab.session_days(data)
    today = days[-1]
    if tier in ("PROMOTED", "WATCH"):
        try:
            r = lab.forward(doc, data, lab_out, None, fno, now, write=False)
        except lab.LabError as e:
            return [], ["%s: %s" % (row["name"], e)], row.get("verdict"), None, today
        spec = lab.validate_spec(doc, params=r["params"])
        return r["plans"], r["notes"], r["verdict"], spec, r["today"]
    base = dict(lab.validate_spec(doc)["base_params"])
    if row.get("res"):
        base.update((row["res"].get("walk_forward") or {}).get("latest_params") or {})
    spec = lab.validate_spec(doc, params=base)
    bt = lab.backtest(spec, data, 1.0, fno)
    h = lab.spec_hash(doc)
    plans = [lab.plan_from_signal(spec, p, today, fno, h, row.get("verdict") or "RESEARCH")
             for p in bt["pending"] if p["date"] == today]
    return plans, [], row.get("verdict"), spec, today


# ---------------------------------------------------------------- sizing and the risk preview

def trader_inputs(workdir):
    t = os.path.join(workdir, "trader")
    return {"dir": t, "config": read_json(os.path.join(t, "config.json")),
            "portfolio": read_json(os.path.join(t, "portfolio.json")),
            "prices": read_json(os.path.join(t, "prices.json")),
            "state": read_json(os.path.join(t, "state", "broker.json"))}


def size_and_check(plan, cfg, ti, when):
    """trader-budget's sizing and trader-risk's verdict for one plan, as of `when` (naive IST). Nothing written."""
    budget, risk = budget_api(), risk_api()
    out = {"sizing": None, "order": None, "risk": None, "charges_at_target": None}
    if budget is None:
        out["refusal"] = "trader-budget is not installed: no size"
        return out
    pf = ti["portfolio"] if isinstance(ti["portfolio"], dict) else None
    cash = pf.get("cash") if pf else None
    try:
        s = budget.size_trade(plan, cfg, cash=float(cash) if isinstance(cash, (int, float)) else None)
    except budget.SizeError as e:
        out["refusal"] = str(e)
        return out
    out["sizing"] = {k: s[k] for k in ("qty", "entry", "stop", "target", "risk_per_share", "reward_risk", "value",
                                       "max_loss", "charges", "max_loss_with_charges", "bound_by", "product",
                                       "refusal")}
    out["refusal"] = s["refusal"]
    order = budget.leg_order(s)
    out["order"] = order
    if order:
        out["charges_at_target"] = budget.round_trip_charges(s["entry"], s["target"], s["qty"], cfg)["total"]
    if order and risk is not None:
        try:
            today = when.date().isoformat()
            portfolio, extra = risk.gather_inputs(ti["dir"], [order], {"orders": [order]}, cfg, today, fetch=False)
            prices, times = risk.load_prices(ti["prices"])
            res = risk.check([order], cfg, portfolio, prices, ti["state"], today, when.replace(tzinfo=IST),
                             times, extra)
            r0 = res["results"][0]
            out["risk"] = {"verdict": r0["verdict"], "reasons": r0["reasons"], "warnings": r0["warnings"],
                           "fingerprint": r0["fingerprint"], "as_of": when.strftime("%Y-%m-%d %H:%M"),
                           "kill": res.get("kill"), "locks": (res.get("breakers") or {}).get("locks") or []}
        except (risk.RiskError, ValueError, KeyError) as e:
            out["risk"] = {"verdict": "BLOCK", "reasons": ["the risk check could not run: %s" % e], "warnings": []}
    return out


def check_basket(orders_doc, cfg, ti, when):
    """trader-risk's verdict on an F&O basket (every leg one position): PASS only when every leg passes."""
    risk = risk_api()
    if risk is None:
        return None
    orders = orders_doc.get("orders") or []
    try:
        today = when.date().isoformat()
        portfolio, extra = risk.gather_inputs(ti["dir"], orders, orders_doc, cfg, today, fetch=False)
        prices, times = risk.load_prices(ti["prices"])
        res = risk.check(orders, cfg, portfolio, prices, ti["state"], today, when.replace(tzinfo=IST), times, extra)
    except (risk.RiskError, ValueError, KeyError) as e:
        return {"verdict": "BLOCK", "reasons": ["the risk check could not run: %s" % e], "warnings": []}
    reasons, warns = [], []
    for r in res["results"]:
        reasons += [x for x in r["reasons"] if x not in reasons]
        warns += [x for x in r["warnings"] if x not in warns]
    return {"verdict": "PASS" if res["results"] and not reasons else "BLOCK", "reasons": reasons, "warnings": warns,
            "fingerprint": None, "as_of": when.strftime("%Y-%m-%d %H:%M"), "kill": res.get("kill"),
            "locks": (res.get("breakers") or {}).get("locks") or [], "legs": len(orders)}


def risk_state(cfg, ti, when):
    """The kill switch and circuit-breaker locks on new entries right now (trader-risk), or {}."""
    risk = risk_api()
    if risk is None:
        return {}
    try:
        today = when.date().isoformat()
        portfolio, extra = risk.gather_inputs(ti["dir"], [], {}, cfg, today, fetch=False)
        prices, times = risk.load_prices(ti["prices"])
        res = risk.check([], cfg, portfolio, prices, ti["state"], today, when.replace(tzinfo=IST), times, extra)
    except (risk.RiskError, ValueError, KeyError):
        return {}
    locks = list((res.get("breakers") or {}).get("locks") or [])
    if (res.get("kill") or {}).get("active"):
        locks.insert(0, "kill switch on today%s" % ((": " + res["kill"]["reason"]) if res["kill"].get("reason") else ""))
    return {"locks": locks, "kill": res.get("kill")}


def check_time(plan, now, cfg):
    """When the risk preview is taken: now for today's plan; the next session's no_entries_before for a plan
    dated later (a daily signal fills at the next open)."""
    day = plan["date"]
    if day <= now.date().isoformat():
        return now
    nb = str((cfg or {}).get("no_entries_before") or "09:30")
    h, m = [int(x) for x in nb.split(":")]
    d = datetime.date.fromisoformat(day)
    return datetime.datetime(d.year, d.month, d.day, h, m)


# ---------------------------------------------------------------- explanations

def does_not_show(c, row, ev, so, lang="en"):
    return dns_words(c, ev, so, (row["doc"].get("costs") or {}).get("slippage_bps", 3), lang)


def dns_words(c, ev, so, bps, lang="en"):
    out = [t("d_past", lang, n=ev["trades_all"], start=ev.get("from") or "?", end=ev.get("to") or "?",
             p=pc(ev.get("p")))]
    if ev["trades_cell"] < SETTINGS["min_cell_trades"]:
        out.append(t("d_thin", lang, n=ev["trades_cell"], cell=ev["cell_words"],
                     w=round(100 * (ev.get("weight_on_cell") or 0))))
    if c["tier"] == "RESEARCH":
        out.append(t("d_research", lang, why=c["tier_why"]))
    elif c["tier"] == "WATCH":
        out.append(t("d_watch", lang))
    out.append(t("d_fills", lang, bps=bps))
    out.append(t("d_survivor", lang))
    out.append(t("d_news", lang))
    if so.get("available") and c["timeframe"] == "1d":
        out.append(t("d_replay", lang))
    return out


def explanation(c, row, ev, so, spec):
    """Why, invalidation, evidence and what it does not show, in English; "inputs" lets explain_words() say it
    again in another language."""
    inputs = {"title": row["doc"].get("title") or row["name"],
              "desc": (row["doc"].get("description") or "").split(". ")[0].strip().rstrip("."),
              "max_bars": spec.get("max_bars") if spec is not None else None,
              "slippage_bps": (row["doc"].get("costs") or {}).get("slippage_bps", 3)}
    x = explain_words(c, ev, so, inputs, "en")
    x["inputs"] = inputs
    return x


def explain_words(c, ev, so, inputs, lang="en"):
    title, desc = inputs["title"], inputs["desc"]
    when = t("e_when_1d", lang, day=c["signal_date"]) if c["timeframe"] == "1d" else \
        t("e_when_5m", lang, time=c["plan"]["entry_time"])
    why = t("e_why_desc", lang, title=title, sym=c["symbol"], when=when, desc=desc) if desc else \
        t("e_why", lang, title=title, sym=c["symbol"], when=when)
    stop = c["plan"]["stop"]
    inval = [t("e_stop", lang, stop=inr(stop), label=c["plan"].get("stop_label") or t("e_stop_label", lang))]
    if c["timeframe"] == "1d":
        inval.append(t("e_gap", lang, stop=inr(stop)))
    else:
        inval.append(t("e_flat", lang, time=c["plan"].get("flat_by") or "15:10"))
    if inputs.get("max_bars"):
        inval.append(t("e_bars", lang, n=inputs["max_bars"]))
    inval.append(t("e_regime", lang, cell=ev["cell_words"]))
    summary = ev["why"] if lang == "en" else evidence_words(ev, lang)
    if so.get("available"):
        summary += t("e_tm", lang, green=pc(so["p_green"]), target=pc(so["p_target"]), stop=pc(so["p_stop"]),
                     net=rr(so.get("avg_r_net")), method=so["method"])
    return {"why": why, "invalidation": inval, "evidence": summary,
            "does_not_show": dns_words(c, ev, so, inputs["slippage_bps"], lang)}


# ---------------------------------------------------------------- one run: today's candidates

def candidate_id(day, name, sym):
    return "%s-%s-%s" % (day, name, sym)


def options_candidate(row, b, tier, tier_why, ev, trades, regs, cell, sig_day, cfg, ti, now, st):
    """A candidate from a trader-lab options forward basket (a trader-strategy body plus a paper orders.json
    proposal), or None when its history shows no edge after charges. R here is the result per rupee of margin."""
    lab = lab_api()
    if ev.get("ev_r") is None or ev["ev_r"] <= 0:
        return None
    body, orders = b["strategy"], json.loads(json.dumps(b["orders"]))
    und = body.get("underlying") or row["doc"].get("underlying")
    cid = candidate_id(b["date"], row["name"], und)
    setup = "cotrader-%s:%s" % (tier.lower(), row["name"])
    for o in orders.get("orders") or []:
        pl = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        pl["setup"] = setup
        o["plan"] = pl
    plan = {"kind": "options", "symbol": und, "date": b["date"], "decided_on": b["decided_on"], "expiry": b["expiry"],
            "setup": setup, "strategy": body, "orders": orders,
            "cotrader": {"id": cid, "tier": tier, "strategy": row["name"], "live_eligible": tier == "PROMOTED",
                         "note": ("RESEARCH: %s" % RESEARCH_LABEL) if tier == "RESEARCH" else
                         "a co-trader candidate: evidence, not a recommendation"}}
    risk = check_basket(orders, cfg, ti, check_time(plan, now, cfg))
    exp = body.get("expiry") or {}
    margin = (body.get("margin") or {}).get("required")
    c = {"id": cid, "strategy": row["name"], "title": row["doc"].get("title"), "symbol": und, "tier": tier,
         "tier_why": tier_why, "kind": "options",
         "label": RESEARCH_LABEL if tier == "RESEARCH" else
         ("paper only while the forward test runs" if tier == "WATCH" else "lab gates and paper forward record"),
         "live_eligible": tier == "PROMOTED", "timeframe": "1d", "product": "NRML", "signal_date": sig_day,
         "date": b["date"], "plan": plan, "spec_hash": lab.spec_hash(row["doc"]), "max_bars": None, "evidence": ev,
         "second_opinion": {"available": False, "why": "the time machine replays stock plans; an option basket "
                                                     "has no replay here"},
         "disagreements": [], "sizing": None, "order": None, "refusal": None, "risk": risk,
         "charges_at_target": (body.get("charges") or {}).get("at_forward_expiry"), "liquidity": None,
         "basket": {"structure": body.get("name"), "expiry": b["expiry"], "legs": len(orders.get("orders") or []),
                    "net_premium": body.get("net_premium"), "margin_required": margin,
                    "max_profit": exp.get("max_profit_after_charges"), "max_loss": exp.get("max_loss_after_charges"),
                    "unlimited_loss": exp.get("unlimited_loss")},
         "similar": similar_trades(trades, regs, und, cell, 0.0, 0.0, int(st["similar"]))}
    c["ev_rupees"] = round(ev["ev_r"] * margin, 2) if margin else None
    c["status"] = "ready" if risk and risk["verdict"] == "PASS" else "blocked"
    c["why_not"] = (risk["reasons"] if risk and risk["verdict"] != "PASS" else []) or \
        ([] if risk else ["trader-risk is not installed: not checked"])
    title = row["doc"].get("title") or row["name"]
    x = {"why": "%s: the entry rule fired on the %s close for the %s expiry; a %s basket priced at that close's "
                "settle prices (the next session's prices will differ)." % (title, b["decided_on"], b["expiry"],
                                                                              body.get("name")),
         "invalidation": ["The spec's own exits: its profit target, its stop as a multiple of the premium, or the "
                          "days-before-expiry exit; else cash settlement at expiry.",
                          "Max loss at expiry after charges: %s." % ("unlimited (a naked short leg)" if
                                                                    exp.get("unlimited_loss") else
                                                                    inr(exp.get("max_loss_after_charges") or 0)),
                          "A change of market regime away from %s before the entry removes the regime evidence."
                          % ev["cell_words"]],
         "evidence": ev["why"] + " Here R is the result per rupee of margin (the lab's options R)."}
    dns = does_not_show(dict(c, timeframe="options"), row, ev, c["second_opinion"])
    dns.append("Option prices are end-of-day settle prices; a gap or an IV jump can move a short option far "
               "past its stop before you can act.")
    x["does_not_show"] = dns
    c["explain"] = x
    return c


def run_today(workdir=".", view="validated", only=None, now=None, data=None, archive=None, fetch=True,
              write=True, lab_out=None, settings=None):
    """Today's candidates, ranked, or the no-edge decision. Returns the cotrader.json document."""
    lab = lab_api()
    st = dict(SETTINGS, **(settings or {}))
    if view not in VIEWS:
        raise CotraderError("view is validated or research")
    workdir = os.path.abspath(workdir)
    now = now or ist_now()
    lab_out = lab_out or os.path.join(workdir, "trader", "lab")
    src = Sources(workdir, data, archive, fetch, lab_out)
    ti = trader_inputs(workdir)
    budget = budget_api()
    cfg = dict(ti["config"]) if isinstance(ti["config"], dict) else None
    notes = []
    if cfg is None:
        notes.append("no trader/config.json: sized with trader-budget's defaults; run size.py init for your own limits")
        cfg = dict(budget.merged_config({})) if budget else {}
    fno, fnote = lab.load_fno_stocks(cfg, os.path.join(workdir, "trader", "fno_stocks.json"), fetch=False)
    if fnote:
        notes.append(fnote)
    demoted = (read_json(demoted_path(workdir), {}) or {}).get("strategies") or {}
    ctx = src.context(now)
    regs = lab.lab_regimes(ctx)
    today_cell, today_words, regime_date = current_cell(regs, now.date().isoformat())
    rows = strategies(lab_out, include_library=(view == "research"))
    if only:
        rows = [r for r in rows if r["name"] == only]
        if not rows:
            raise CotraderError("no strategy %r in %s%s" % (only, lab_out, " or the library" if view == "research" else ""))
    allowed = VIEWS[view]
    tm = tm_api()
    cands, passed, strat_rows = [], [], []
    hidden_research = 0
    for row in rows:
        tier, tier_why = tier_of(row, demoted)
        srow = {"name": row["name"], "title": row["doc"].get("title"), "timeframe": row["doc"].get("timeframe"),
                "kind": row["doc"].get("kind") or "equity", "verdict": row.get("verdict"), "tier": tier,
                "tier_why": tier_why, "source": row["source"], "in_view": tier in allowed, "triggers": 0,
                "candidates": 0, "status": None}
        strat_rows.append(srow)
        if tier not in allowed:
            srow["status"] = "hidden: RESEARCH (%s); shown with --tier research" % RESEARCH_LABEL
            continue
        is_opt = row["doc"].get("kind") == "options"
        try:
            spec0 = lab.validate_spec(row["doc"])
        except lab.LabError as e:
            srow["status"] = "the spec does not validate: %s" % e
            continue
        if is_opt and not src.archive:
            srow["status"] = "options: no local trader-archive with the EOD F&O chains (archive.py backfill)"
            continue
        if is_opt:
            try:
                chains, _, onotes = lab.load_data(spec0, None, None, False, now, archive=src.archive)
            except lab.LabError as e:
                srow["status"] = "options: %s" % e
                continue
            data_s = {}
            trades, basis = history(row, {}, fno, st) if row.get("res") else ([], "never run in the lab")
            plans, tnotes, live_v, sig_day = options_triggers(row, chains, now, lab_out, tier)
            spec = spec0
        else:
            data_s = src.market(spec0, now)
            trades, basis = history(row, data_s, fno, st)
            plans, tnotes, live_v, spec, sig_day = triggers(row, data_s, fno, now, lab_out, tier)
        notes.extend(tnotes)
        if tier in ("PROMOTED", "WATCH") and live_v != row.get("verdict"):
            # the lab's forward test re-checked the verdict on today's bars: that one counts
            tier = TIER_OF_VERDICT.get(live_v, "RESEARCH")
            tier_why = "lab %s today (the forward record re-checked it)" % live_v
            srow.update({"tier": tier, "tier_why": tier_why, "verdict": live_v})
            if tier not in allowed:
                srow.update({"status": "re-checked to %s today: hidden (RESEARCH)" % live_v, "in_view": False})
                continue
        cut = sig_day or now.date().isoformat()
        known = [t for t in trades if t["exit_date"] < cut]      # no look-ahead: trades closed before the signal day
        if len(known) < len(trades):
            basis += "; %d trade(s) closing on or after %s left out" % (len(trades) - len(known), cut)
        trades = known
        cell, words, _ = current_cell(regs, cut)
        ev_s = evidence(trades, regs, cell, st, basis)
        srow["regime"] = {"cell": cell, "words": cell_words(cell), "trades": ev_s["trades_cell"],
                          "ev_r_raw": ev_s.get("ev_r_cell_raw"), "fits": ev_s.get("fits")}
        srow["edge_cells"] = [cell_words(c) for c in edge_cells(trades, regs, int(st["min_cell_trades"]))]
        if ev_s.get("fits") is False:
            srow["status"] = ("regime mismatch: %d past trades in %s average %s after charges"
                              % (ev_s["trades_cell"], cell_words(cell), rr(ev_s["ev_r_cell_raw"])))
            continue
        fresh = [p for p in plans if p["date"] >= now.date().isoformat()]
        if len(fresh) < len(plans):
            notes.append("%s: %d signal(s) on bars older than today skipped (the latest bars are from %s)"
                         % (row["name"], len(plans) - len(fresh), sig_day))
        srow["triggers"] = len(fresh)
        if not fresh:
            srow["status"] = (("no live trigger: RESEARCH options show stats only (trader-lab drafts baskets for "
                               "WATCH and PROMOTE)" if tier == "RESEARCH" else
                               "no live trigger: the lab's options forward test drafted no basket today (%s)"
                               % (sig_day or "no chains")) if srow["kind"] == "options" else
                              "no live trigger on the latest bars (%s)" % (sig_day or "no bars"))
            continue
        for p in fresh:
            if is_opt:
                c = options_candidate(row, p, tier, tier_why, ev_s, trades, regs, cell, sig_day, cfg, ti, now, st)
                if c is None:
                    und = row["doc"].get("underlying")
                    passed.append({"id": candidate_id(p["date"], row["name"], und), "strategy": row["name"],
                                   "symbol": und, "tier": tier,
                                   "why": "expected return on margin after charges is %s in %s: no edge to offer" % (
                                       rr(ev_s.get("ev_r")), ev_s["cell_words"])})
                    continue
                cands.append(c)
                srow["candidates"] += 1
                continue
            sym = p["symbol"]
            ev = ev_s
            cid = candidate_id(p["date"], row["name"], sym)
            if ev.get("ev_r") is None or ev["ev_r"] <= 0:
                passed.append({"id": cid, "strategy": row["name"], "symbol": sym, "tier": tier,
                               "why": "expected R after charges is %s in %s: no edge to offer" % (
                                   rr(ev.get("ev_r")), ev["cell_words"])})
                continue
            plan = dict(p)
            plan["setup"] = "cotrader-%s:%s" % (tier.lower(), row["name"])
            plan["cotrader"] = {"id": cid, "tier": tier, "strategy": row["name"], "live_eligible": tier == "PROMOTED",
                                "note": ("RESEARCH: %s" % RESEARCH_LABEL) if tier == "RESEARCH" else
                                "a co-trader candidate: evidence, not a recommendation"}
            when = check_time(plan, now, cfg)
            sc = size_and_check(plan, cfg, ti, when)
            qty = (sc["sizing"] or {}).get("qty") or 0
            so = second_opinion(tm, plan, data_s.get(sym), spec["timeframe"], qty, cfg, spec.get("max_bars"), st)
            d1 = src.series(sym, "1d", now) if spec["timeframe"] != "1d" else data_s.get(sym)
            c = {"id": cid, "strategy": row["name"], "title": row["doc"].get("title"), "symbol": sym, "tier": tier,
                 "tier_why": tier_why, "label": RESEARCH_LABEL if tier == "RESEARCH" else
                 ("paper only while the forward test runs" if tier == "WATCH" else "lab gates and paper forward record"),
                 "live_eligible": tier == "PROMOTED", "timeframe": spec["timeframe"], "product": spec["product"],
                 "signal_date": sig_day, "date": plan["date"], "plan": plan, "spec_hash": lab.spec_hash(row["doc"]),
                 "max_bars": spec.get("max_bars"), "evidence": ev, "second_opinion": so,
                 "disagreements": disagreements(ev, so), "sizing": sc["sizing"], "order": sc["order"],
                 "refusal": sc.get("refusal"), "risk": sc["risk"], "charges_at_target": sc["charges_at_target"],
                 "liquidity": liquidity(d1, int(st["liquidity_sessions"])),
                 "similar": similar_trades(trades, regs, sym, cell, float(plan["entry"]), float(plan["stop"]),
                                           int(st["similar"]))}
            if c["sizing"] and qty:
                rps = c["sizing"]["risk_per_share"]
                c["ev_rupees"] = round(ev["ev_r"] * rps * qty, 2)
            else:
                c["ev_rupees"] = None
            c["status"] = "ready" if (c["order"] and c["risk"] and c["risk"]["verdict"] == "PASS") else "blocked"
            c["why_not"] = ([c["refusal"]] if c["refusal"] else []) + (c["risk"]["reasons"] if c["risk"] and
                                                                         c["risk"]["verdict"] != "PASS" else [])
            c["explain"] = explanation(c, row, ev, so, spec)
            cands.append(c)
            srow["candidates"] += 1
        if srow["status"] is None:
            srow["status"] = "%d candidate(s)" % srow["candidates"] if srow["candidates"] else \
                "triggers, but none with an edge after charges"
    for r in strat_rows:
        if r["tier"] == "RESEARCH" and not r["in_view"]:
            hidden_research += 1
    held = open_positions(ti["portfolio"])
    rets = {}
    for s in set([c["symbol"] for c in cands] + held):
        rets[s] = daily_returns(src.series(s, "1d", now), int(st["corr_sessions"]))
    ready = rank([c for c in cands if c["status"] == "ready"], rets, held, st)
    blocked = rank([c for c in cands if c["status"] != "ready"], rets, held, st)
    for i, c in enumerate(blocked):
        c["rank"] = len(ready) + i + 1
    ordered = ready + blocked
    rstate = risk_state(cfg, ti, now)
    doc = {"schema": SCHEMA, "as_of": now.strftime("%Y-%m-%d %H:%M"), "view": view, "tiers": list(allowed),
           "regime_now": {"cell": today_cell, "words": today_words, "as_of_close": regime_date},
           "settings": st, "strategies": strat_rows, "candidates": ordered, "passed_over": passed,
           "open_positions": held, "risk_state": rstate, "notes": sorted(set(n for n in notes + src.notes if n)),
           "hidden_research": hidden_research, "disclaimer": DISCLAIMER}
    doc["no_edge"] = no_edge(doc)
    doc["headline"] = headline(doc)
    if write:
        save(workdir, doc)
    return doc


def no_edge(doc, lang="en"):
    """Sitting out, as a decision: why nothing qualifies and what would change that. None when a candidate is ready."""
    ready = [c for c in doc["candidates"] if c["status"] == "ready"]
    if ready:
        return None
    rows = doc["strategies"]
    validated = [r for r in rows if r["tier"] in ("PROMOTED", "WATCH")]
    reasons, change = [], []
    labs = [r for r in rows if r["source"] == "lab"]
    if not validated:
        if labs:
            vs = {}
            for r in labs:
                vs[r["verdict"] or "no verdict"] = vs.get(r["verdict"] or "no verdict", 0) + 1
            reasons.append(t("ne_no_gates", lang, n=len(labs),
                             verdicts=", ".join("%d %s" % (n, v) for v, n in sorted(vs.items()))))
        else:
            reasons.append(t("ne_no_lab", lang))
        change.append(t("ne_ch_lab", lang))
    mism = [r for r in validated if str(r.get("status") or "").startswith("regime mismatch")]
    for r in mism:
        reasons.append(t("ne_mismatch", lang, name=r["name"], why=r["status"][len("regime mismatch: "):]))
        if r.get("edge_cells"):
            change.append(t("ne_ch_cells", lang, name=r["name"], cells="; ".join(r["edge_cells"])))
    quiet = [r for r in validated if str(r.get("status") or "").startswith("no live trigger")]
    if quiet:
        reasons.append(t("ne_quiet", lang, names=", ".join(r["name"] for r in quiet)))
        change.append(t("ne_ch_quiet", lang))
    noev = [p for p in doc["passed_over"] if p["tier"] in ("PROMOTED", "WATCH")]
    if noev:
        reasons.append(t("ne_noev", lang, n=len(noev)))
    blocked = [c for c in doc["candidates"] if c["status"] != "ready" and c["tier"] in doc["tiers"]]
    if blocked:
        reasons.append(t("ne_blocked", lang, n=len(blocked), items="; ".join(
            "%s (%s)" % (c["symbol"], (c["why_not"] or ["blocked"])[0]) for c in blocked[:3])))
    locks = (doc.get("risk_state") or {}).get("locks") or []
    if locks:
        reasons.append(t("ne_locks", lang, locks="; ".join(locks)))
        change.append(t("ne_ch_locks", lang))
    if doc.get("hidden_research"):
        change.append(t("ne_hidden", lang, n=doc["hidden_research"], label=RESEARCH_LABEL))
    research = [r for r in rows if r["tier"] == "RESEARCH" and r["in_view"]]
    if research:
        n_mis = sum(1 for r in research if str(r.get("status") or "").startswith("regime mismatch"))
        n_quiet = sum(1 for r in research if str(r.get("status") or "").startswith("no live trigger"))
        reasons.append(t("ne_research", lang, n=len(research), mis=n_mis, quiet=n_quiet,
                         other=len(research) - n_mis - n_quiet))
    if not reasons:
        reasons.append(t("ne_nothing", lang))
    return {"decision": "sit out", "headline": t("ne_headline", lang),
            "reasons": reasons, "would_change": change,
            "regime_now": doc["regime_now"]["words"]}


def headline(doc, lang="en"):
    ready = [c for c in doc["candidates"] if c["status"] == "ready"]
    if doc["no_edge"]:
        return doc["no_edge"]["headline"] if lang == "en" else t("ne_headline", lang)
    tiers = {}
    for c in ready:
        tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
    return t("headline", lang, n=len(ready), tiers=", ".join("%d %s" % (n, tr) for tr, n in sorted(tiers.items())))


# ---------------------------------------------------------------- files: cotrader.json, plans, the log

def log_path(workdir):
    return os.path.join(workdir, "trader", "cotrader", "log.jsonl")


def plan_path(workdir, cid):
    if not ID_RE.match(cid or ""):
        raise CotraderError("not a co-trader candidate ID: %r" % cid)
    return os.path.join(workdir, "trader", "cotrader", "plans", "%s.json" % cid)


def read_log(workdir):
    p = log_path(workdir)
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def log_line(c, at):
    ev = c["evidence"]
    so = c["second_opinion"]
    return {"schema": SCHEMA_LOG, "id": c["id"], "logged_at": at, "date": c["date"], "signal_date": c["signal_date"],
            "strategy": c["strategy"], "spec_hash": c["spec_hash"], "tier": c["tier"], "symbol": c["symbol"],
            "timeframe": c["timeframe"], "p": ev["p"], "p_lo": ev["p_lo"], "p_hi": ev["p_hi"], "ev_r": ev["ev_r"],
            "trades_cell": ev["trades_cell"], "trades_all": ev["trades_all"], "regime": ev["cell"],
            "entry": c["plan"].get("entry"), "stop": c["plan"].get("stop"), "target": c["plan"].get("target"),
            "entry_time": c["plan"].get("entry_time"), "flat_by": c["plan"].get("flat_by"),
            "qty": (c["sizing"] or {}).get("qty") or 0, "max_bars": c.get("max_bars"), "status": c["status"],
            "second_p": so.get("p_green") if so.get("available") else None}


def save(workdir, doc):
    """cotrader.json, a plan file per candidate, and one log line per candidate never logged before."""
    base = os.path.join(workdir, "trader")
    write_json(os.path.join(base, "cotrader.json"), doc)
    for c in doc["candidates"]:
        write_json(plan_path(workdir, c["id"]), c["plan"])
    seen = set(r.get("id") for r in read_log(workdir))
    new = [log_line(c, doc["as_of"]) for c in doc["candidates"] if c["id"] not in seen and c["evidence"]["p"] is not None]
    if new:
        p = log_path(workdir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    doc["logged"] = len(new)


def find_candidate(workdir, cid):
    doc = read_json(os.path.join(workdir, "trader", "cotrader.json"))
    if doc:
        for c in doc.get("candidates") or []:
            if c["id"] == cid:
                return c, doc
    return None, doc


# ---------------------------------------------------------------- outcomes and calibration

def paper_outcome(rec, trades_doc):
    """From the paper broker's trades log: a BUY of the symbol on the plan's date, then sells with realised P&L."""
    rows = (trades_doc or {}).get("trades") if isinstance(trades_doc, dict) else trades_doc
    rows = [t for t in rows or [] if isinstance(t, dict) and str(t.get("symbol", "")).upper() == rec["symbol"]]
    buys = [t for t in rows if str(t.get("side")).upper() == "BUY" and t.get("date") == rec["date"]
            and t.get("leg") in (None, "order")]
    if not buys:
        return None
    first = min(str(t.get("at") or "") for t in buys)
    sells = [t for t in rows if str(t.get("side")).upper() == "SELL" and str(t.get("at") or "") >= first
             and t.get("realised_pnl") is not None]
    if not sells:
        return None
    pnl = sum(float(t["realised_pnl"]) for t in sells)
    return {"source": "paper fills", "net": round(pnl, 2), "hit": 1 if pnl > 0 else 0,
            "exit_date": max(t.get("date") or "" for t in sells)}


def forward_outcome(rec, lab_out):
    """From the lab's paper forward record: the strategy's own simulated trade on that signal."""
    doc = read_json(os.path.join(lab_out, rec["strategy"], "forward.json"), {}) or {}
    for t in doc.get("trades") or []:
        if t.get("symbol") == rec["symbol"] and t.get("signal_date") == rec["signal_date"] \
                and t.get("reason") != "end of data":
            return {"source": "lab forward record", "net": t["net"], "hit": 1 if float(t["net"]) > 0 else 0,
                    "exit_date": t.get("exit_date"), "r": t.get("r")}
    return None


def replay_outcome(rec, series, cfg):
    """From the bars after the signal: entry at the first bar's open at or after the entry (skipped if it opens
    through the stop or the target), then stop first, target, and out at the flat-by (5m) or after the
    maximum holding (1d, at that bar's close). Net of trader-lab's charges on the logged quantity."""
    if series is None or not series.n or rec.get("stop") is None:
        return None
    lab = lab_api()
    if rec["timeframe"] == "5m":
        h, m = [int(x) for x in str(rec.get("entry_time") or "09:20").split(":")]
        fh, fm = [int(x) for x in str(rec.get("flat_by") or "15:10").split(":")]
        entry_min, flat_min = h * 60 + m, fh * 60 + fm
        idx = [i for i in range(series.n) if series.date[i] == rec["date"] and series.minute[i] >= entry_min]
    else:
        idx = [i for i in range(series.n) if series.date[i] >= rec["date"]]
    if not idx:
        return None
    i0 = idx[0]
    stop, target = float(rec["stop"]), float(rec["target"])
    fill = series.o[i0]
    if fill <= stop or fill >= target:
        return {"source": "bars replay", "net": None, "hit": None, "skipped": "opened through the stop or target"}
    qty = int(rec.get("qty") or 0) or 1
    hold = int(rec.get("max_bars") or SETTINGS["daily_hold"])
    exit_px, bars = None, 0
    for i in idx:
        if rec["timeframe"] == "5m" and series.minute[i] >= flat_min:
            exit_px = series.o[i]
            break
        if series.l[i] <= stop:
            exit_px = min(stop, series.o[i]) if i != i0 else stop
            break
        if series.h[i] >= target:
            exit_px = target
            break
        bars += 1
        if rec["timeframe"] == "1d" and bars >= hold:
            exit_px = series.c[i]
            break
    if exit_px is None:
        return None
    ch = lab.round_trip_charges(fill, exit_px, qty, cfg)["total"]
    net = round((exit_px - fill) * qty - ch, 2)
    return {"source": "bars replay", "net": net, "hit": 1 if net > 0 else 0, "exit_date": series.date[i]}


def resolve(records, workdir, src=None, now=None, lab_out=None):
    """Each log record with its outcome (paper fills, then the lab's forward record, then the bars)."""
    lab_out = lab_out or os.path.join(workdir, "trader", "lab")
    trades_doc = read_json(os.path.join(workdir, "trader", "state", "trades.json"))
    cfg = read_json(os.path.join(workdir, "trader", "config.json")) or {}
    cfg = lab_api().lab_config({}) if not cfg else dict(lab_api().lab_config({}), **cfg)
    out = []
    for rec in records:
        o = paper_outcome(rec, trades_doc) or forward_outcome(rec, lab_out)
        if o is None and src is not None and rec.get("stop") is not None:
            o = replay_outcome(rec, src.series(rec["symbol"], rec["timeframe"], now or ist_now()), cfg)
        out.append(dict(rec, outcome=o))
    return out


def calibration(records, settings=None):
    """Brier score, log loss and reliability, overall, per strategy and per tier, and who to demote."""
    st = dict(SETTINGS, **(settings or {}))
    done = [r for r in records if r.get("outcome") and r["outcome"].get("hit") is not None and r.get("p") is not None]
    pend = [r for r in records if r not in done]

    def group(rs):
        return score_group([float(r["p"]) for r in rs], [int(r["outcome"]["hit"]) for r in rs], int(st["bins"]))
    by_s, by_t = {}, {}
    for r in done:
        by_s.setdefault(r["strategy"], []).append(r)
        by_t.setdefault(r["tier"], []).append(r)
    rep = {"schema": SCHEMA_CAL, "resolved": len(done), "pending": len(pend), "overall": group(done),
           "by_strategy": {k: group(v) for k, v in sorted(by_s.items())},
           "by_tier": {k: group(v) for k, v in sorted(by_t.items())},
           "sources": {}, "demote": []}
    for r in done:
        s = r["outcome"]["source"]
        rep["sources"][s] = rep["sources"].get(s, 0) + 1
    for name, g in rep["by_strategy"].items():
        tiers = set(r["tier"] for r in by_s[name])
        if g["n"] >= int(st["min_calibration"]) and tiers & {"PROMOTED", "WATCH"} and g["mean_p"] > g["observed_ci"][1]:
            rep["demote"].append({"strategy": name, "why": "%d resolved predictions averaged %s, but only %s made money "
                                  "after charges (95%% %s to %s): overconfident"
                                  % (g["n"], pc(g["mean_p"]), pc(g["observed"]), pc(g["observed_ci"][0]),
                                     pc(g["observed_ci"][1]))})
    rep["words"] = calibration_words(rep, st)
    return rep


def calibration_words(rep, st):
    g = rep["overall"]
    if not g.get("n"):
        return ("No outcomes yet: %d prediction(s) waiting. Calibration needs resolved trades; a strategy needs %d "
                "before it can be judged." % (rep["pending"], st["min_calibration"]))
    s = ("%d resolved: predicted %s on average, %s made money after charges (95%% %s to %s). Brier %.3f (a constant "
         "guess at the base rate scores %.3f), log loss %.3f." % (g["n"], pc(g["mean_p"]), pc(g["observed"]),
                                                                 pc(g["observed_ci"][0]), pc(g["observed_ci"][1]),
                                                                 g["brier"], g["brier_reference"], g["log_loss"]))
    if g["n"] < st["min_calibration"]:
        s += " Too few to judge: %d needed." % st["min_calibration"]
    return s


def apply_demotions(rep, workdir, now, lab_out=None):
    """Demote each overconfident strategy: trader-lab's status.json verdict becomes DEMOTE (the lab's own
    mechanism: it stays DEMOTE until the spec changes) and trader/cotrader/demoted.json records why."""
    lab = lab_api()
    lab_out = lab_out or os.path.join(workdir, "trader", "lab")
    path = demoted_path(workdir)
    doc = read_json(path, {}) or {}
    have = doc.get("strategies") or {}
    done = []
    for d in rep["demote"]:
        name = d["strategy"]
        st = lab.read_json(os.path.join(lab_out, name, "status.json"), {}) or {}
        if st.get("verdict") in ("PROMOTE", "WATCH"):
            lab.update_status(lab_out, name, "DEMOTE", "co-trader calibration: " + d["why"], st.get("spec_hash"), now,
                              st.get("forward"))
            done.append(name)
        have[name] = {"at": now.strftime("%Y-%m-%d %H:%M"), "why": d["why"]}
    if rep["demote"]:
        write_json(path, {"note": "Strategies the co-trader demoted for poor calibration: shown as RESEARCH only.",
                          "strategies": have})
    return done


# ---------------------------------------------------------------- plain words

def render_today(doc, lang="en"):
    ne = doc["no_edge"]
    if ne and lang != "en":
        ne = no_edge(doc, lang)
    L = []
    L.append(t("head", lang, as_of=doc["as_of"], view=t("view_validated" if doc["view"] == "validated" else "view_all",
                                                        lang)))
    L.append(t("market", lang, words=doc["regime_now"]["words"]))
    L.append("")
    L.append(doc["headline"] if lang == "en" else headline(doc, lang))
    for c in doc["candidates"]:
        ev = c["evidence"]
        L.append("")
        L.append("#%d %s  [%s%s]  %s, %s" % (c["rank"], c["symbol"], c["tier"],
                                            (": " + RESEARCH_LABEL) if c["tier"] == "RESEARCH" else "",
                                            c["strategy"], c["status"].upper()))
        L.append("  id %s" % c["id"])
        p = c["plan"]
        if c.get("kind") == "options":
            bk = c["basket"]
            L.append(t("options_line", lang, structure=bk["structure"], date=c["date"], expiry=bk["expiry"],
                       legs=bk["legs"], margin=inr(bk["margin_required"] or 0),
                       loss=t("unlimited", lang) if bk["unlimited_loss"] else inr(bk["max_loss"] or 0)))
        else:
            L.append(t("buy_line", lang, product=c["product"], date=c["date"],
                       when=t("when_after", lang, time=p["entry_time"]) if c["timeframe"] == "5m" else
                       t("when_open", lang), entry=inr(p["entry"]), stop=inr(p["stop"]), target=inr(p["target"])))
        s = c["sizing"]
        if s and s["qty"]:
            L.append(t("size_line", lang, qty=s["qty"], value=inr(s["value"]), loss=inr(s["max_loss_with_charges"]),
                       charges=inr(c["charges_at_target"] or 0)))
        L.append(t("odds", lang, p=pc(ev["p"]), lo=pc(ev["p_lo"]), hi=pc(ev["p_hi"]), n=ev["trades_cell"],
                   cell=ev["cell_words"], n_all=ev["trades_all"], ev=rr(ev["ev_r"]),
                   rupees=t("odds_rupees", lang, x=inr(c["ev_rupees"])) if c.get("ev_rupees") is not None else ""))
        so = c["second_opinion"]
        if so.get("available"):
            L.append(t("tm_line", lang, green=pc(so["p_green"]), net=rr(so.get("avg_r_net"))))
        for d in c["disagreements"]:
            L.append(t("disagrees", lang, text=d))
        r = c["risk"]
        L.append(t("risk_line", lang, verdict=r["verdict"] if r else t("not_checked", lang),
                   why=(": " + "; ".join(c["why_not"])) if c["why_not"] else ""))
        if c.get("rho_with"):
            L.append(t("corr", lang, rho="%.2f" % c["rho"], other=c["rho_with"], score="%.3f" % c["score"]))
        x = c["explain"]
        if lang != "en" and x.get("inputs"):
            x = explain_words(c, ev, so, x["inputs"], lang)
        L.append(t("why_line", lang, text=x["why"]))
        L.append(t("explain_cmd", lang, id=c["id"]))
    if ne:
        L.append("")
        L.append(t("why_head", lang))
        for r in ne["reasons"]:
            L.append("  - %s" % r)
        if ne["would_change"]:
            L.append(t("change_head", lang))
            for r in ne["would_change"]:
                L.append("  - %s" % r)
    if doc["passed_over"]:
        L.append("")
        L.append(t("passed_head", lang))
        for p in doc["passed_over"][:10]:
            L.append("  %s %s (%s): %s" % (p["symbol"], p["strategy"], p["tier"], p["why"]))
    L.append("")
    L.append(t("strategies_head", lang))
    for r in doc["strategies"]:
        L.append("  %-22s %-8s %s" % (r["name"], r["tier"], r["status"] or ""))
    for n in doc["notes"][:12]:
        L.append(t("note", lang, text=n))
    L.append("")
    L.append(t("footer", lang))
    L.append(t("disclaimer", lang))
    return "\n".join(L) + "\n"


def render_explain(c, lang="en"):
    ev, so, x = c["evidence"], c["second_opinion"], c["explain"]
    if lang != "en" and x.get("inputs"):
        x = explain_words(c, ev, so, x["inputs"], lang)
    L = ["%s  [%s%s]" % (c["id"], c["tier"], (": " + RESEARCH_LABEL) if c["tier"] == "RESEARCH" else ""), ""]
    L.append(t("x_why", lang, text=x["why"]))
    L.append("")
    L.append(t("x_inval", lang))
    L.extend("  - %s" % s for s in x["invalidation"])
    L.append("")
    L.append(t("x_evidence", lang, basis=ev["basis"], text=x["evidence"]))
    for d in c["disagreements"]:
        L.append(t("disagrees", lang, text=d))
    L.append("")
    L.append(t("x_dns", lang))
    L.extend("  - %s" % s for s in x["does_not_show"])
    if c["similar"]:
        L.append("")
        L.append(t("x_similar", lang))
        for tr in c["similar"]:
            L.append("  %s %s, %s: %s (%s, %s)" % (tr["signal_date"], tr["symbol"], tr["regime"], rr(tr["r"]),
                                                  inr(tr["net"]), tr["reason"]))
    L.append("")
    r = c["risk"]
    L.append(t("x_risk", lang, as_of=(r or {}).get("as_of", "-"), verdict=(r or {}).get("verdict") or
               t("not_checked", lang)))
    for w in c["why_not"]:
        L.append("  - %s" % w)
    L.append("")
    L.append(t("x_paper_opt" if c.get("kind") == "options" else "x_paper", lang, id=c["id"]))
    L.append(t("disclaimer", lang))
    return "\n".join(L) + "\n"


def render_calibration(rep):
    L = ["Co-trader calibration", "", rep["words"]]
    for title, groups in (("By strategy", rep["by_strategy"]), ("By tier", rep["by_tier"])):
        if not groups:
            continue
        L.append("")
        L.append(title + ":")
        for k, g in groups.items():
            L.append("  %-22s n=%-4d predicted %s, observed %s, Brier %.3f, log loss %.3f" % (
                k, g["n"], pc(g["mean_p"]), pc(g["observed"]), g["brier"], g["log_loss"]))
    bins = rep["overall"].get("bins") or []
    if bins:
        L.append("")
        L.append("Reliability (predicted vs observed):")
        for b in bins:
            L.append("  %2.0f to %3.0f%%  n=%-4d predicted %s, observed %s (95%% %s to %s)" % (
                100 * b["lo"], 100 * b["hi"], b["n"], pc(b["mean_p"]), pc(b["observed"]), pc(b["ci"][0]),
                pc(b["ci"][1])))
    for d in rep["demote"]:
        L.append("DEMOTE %s: %s" % (d["strategy"], d["why"]))
    L.append("")
    L.append("Outcomes from: %s; %d still open or unknown." % (
        ", ".join("%s %d" % kv for kv in sorted(rep["sources"].items())) or "none yet", rep["pending"]))
    L.append(DISCLAIMER)
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- commands

def _src_args(p):
    p.add_argument("--workdir", default=".", help="the folder holding trader/ (default: here)")
    p.add_argument("--data", help="offline: a folder of SYMBOL-5m.csv(.gz) / SYMBOL-1d.csv(.gz) bars")
    p.add_argument("--archive", help="a trader-archive folder (default trader/archive, or $BT_ARCHIVE)")
    p.add_argument("--no-archive", action="store_true", help="ignore the local archive")
    p.add_argument("--offline", action="store_true", help="no network: the archive and trader/lab/data only")
    p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-30 16:00\"")
    p.add_argument("--json", action="store_true")


def _archive(a):
    return "" if a.no_archive else a.archive


def cmd_today(a):
    now = ist_now(a.now)
    doc = run_today(a.workdir, "research" if a.tier == "research" else "validated", a.strategy, now, a.data,
                    _archive(a), not a.offline, not a.no_write)
    if a.json:
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        lang = lang_for(a.workdir, a.lang)
        sys.stdout.write(render_today(doc, lang))
        if not a.no_write:
            print(t("wrote", lang, n=len(doc["candidates"]), logged=doc.get("logged", 0)))
    return 0


def cmd_explain(a):
    c, _ = find_candidate(os.path.abspath(a.workdir), a.id)
    if c is None:
        raise CotraderError("no candidate %s in trader/cotrader.json: run cotrader.py today first" % a.id)
    if a.json:
        print(json.dumps(c, indent=1, ensure_ascii=False))
    else:
        sys.stdout.write(render_explain(c, lang_for(a.workdir, a.lang)))
    return 0


def calibration_report(workdir, now=None, data=None, archive=None, fetch=False, demote=False):
    """Resolve the log's outcomes and score them; demote (trader-lab status) only when asked."""
    workdir = os.path.abspath(workdir)
    now = now or ist_now()
    src = Sources(workdir, data, archive, fetch)
    recs = resolve(read_log(workdir), workdir, src, now)
    rep = calibration(recs)
    rep["as_of"] = now.strftime("%Y-%m-%d %H:%M")
    rep["records"] = [{k: r.get(k) for k in ("id", "strategy", "tier", "symbol", "date", "p", "p_lo", "p_hi",
                                             "trades_cell", "outcome")} for r in recs][-200:]
    rep["demoted_now"] = apply_demotions(rep, workdir, now) if demote else []
    rep["disclaimer"] = DISCLAIMER
    return rep


def cmd_calibration(a):
    now = ist_now(a.now)
    rep = calibration_report(a.workdir, now, a.data, _archive(a), not a.offline, not a.no_demote)
    if a.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
    else:
        sys.stdout.write(render_calibration(rep))
        for n in rep["demoted_now"]:
            print("Demoted %s in trader-lab (status.json verdict DEMOTE)." % n)
    return 0


def cmd_log(a):
    recs = read_log(os.path.abspath(a.workdir))[-a.limit:]
    if a.json:
        print(json.dumps(recs, indent=1, ensure_ascii=False))
        return 0
    if not recs:
        print("No predictions logged yet (cotrader.py today logs each candidate once).")
    for r in recs:
        print("%s %-8s p=%s (95%% %s to %s) n=%d %s" % (r["logged_at"], r["tier"], pc(r["p"]), pc(r["p_lo"]),
                                                    pc(r["p_hi"]), r["trades_cell"], r["id"]))
    return 0


def errors():
    """This tool's error and the sibling skills' own: a message for the user, not a trace."""
    out = [CotraderError]
    lab = lab_api()
    out += [lab.LabError, lab.RulesError]
    for mod, name in ((budget_api(), "SizeError"), (risk_api(), "RiskError")):
        if mod is not None:
            out.append(getattr(mod, name))
    return tuple(out)


def utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="cotrader.py", description="Co-trader: today's candidates from backtested "
                                 "rules, with calibrated odds, or the decision to sit out.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("today", help="today's candidates (PROMOTED and WATCH), or why there are none")
    p.add_argument("--tier", choices=["validated", "research"], default="validated",
                   help="research also shows the unproven RESEARCH ideas (study and paper only)")
    p.add_argument("--strategy", help="only this lab strategy")
    p.add_argument("--no-write", action="store_true", help="print only: no cotrader.json, plans or log lines")
    p.add_argument("--lang", choices=LANGS, metavar="LANG", help="en or hi-en (Hinglish); default: the config's \"lang\", else en")
    _src_args(p)
    p = sub.add_parser("explain", help="one candidate in full: why, invalidation, evidence, what it does not show")
    p.add_argument("id")
    p.add_argument("--workdir", default=".")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lang", choices=LANGS, metavar="LANG", help="en or hi-en (Hinglish); default: the config's \"lang\", else en")
    p = sub.add_parser("calibration", help="score the logged probabilities against outcomes; demote the overconfident")
    p.add_argument("--no-demote", action="store_true", help="report only: change no trader-lab status")
    _src_args(p)
    p = sub.add_parser("log", help="the logged predictions")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--workdir", default=".")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"today": cmd_today, "explain": cmd_explain, "calibration": cmd_calibration, "log": cmd_log}[a.cmd](a)
    except errors() as e:
        print("cotrader: %s" % e, file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
