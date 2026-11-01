"""The edge-hunt pre-registration: every hypothesis file obeys the schema in docs/research/edge-hunt.md,
its dates are split cleanly, its registered content matches the manifest hash, and the brief agrees."""

import datetime
import hashlib
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT, load  # noqa: E402

DIR = os.path.join(ROOT, "trader-lab", "library", "edge-hunt")
BRIEF = os.path.join(ROOT, "docs", "research", "edge-hunt.md")

FAMILIES = {"momentum-xs", "low-volatility", "trend-ts", "calendar", "vrp-selling", "pead", "event-vol",
            "reversal", "pairs", "intraday-momentum", "overnight", "option-buying"}
MECHANISMS = {"risk-premium", "behavioural", "structural"}
SEGMENTS = {"EQ-CNC", "EQ-MIS", "FUT", "OPT", "FUT+EQ"}
METRICS = {"oos_net_sharpe", "oos_active_sharpe", "oos_expectancy_r", "oos_return_on_margin", "oos_mean_event_return"}
STATUSES = {"registered", "running", "rejected", "watch", "promoted", "withdrawn"}
EXTRA_GATES = {"drawdown_basis", "max_drawdown_vs_benchmark", "min_oos_months", "min_holdout_trades", "fdr_q",
               "verdict_cap"}
DRAWDOWN_BASES = {"capital", "active", "vs_buy_and_hold"}
MAX_GRID = 36
MIN_EMBARGO_DAYS = 14

STR, NUM, INT, BOOL, LIST, OBJ = "str", "num", "int", "bool", "list", "obj"
FIELDS = {
    "schema_version": INT, "id": STR, "rank": INT, "family": STR, "title": STR, "hypothesis": STR,
    "mechanism": OBJ, "evidence": LIST, "instrument": OBJ, "universe": OBJ, "holding_period": STR,
    "turnover": OBJ, "data": OBJ, "span": OBJ, "parameters": OBJ, "trial_budget": INT, "prior_trials": OBJ,
    "gates": OBJ, "primary_metric": STR, "holdout_rule": OBJ, "kill_criteria": LIST, "prior": OBJ,
    "feasibility": OBJ, "contamination": STR, "registered_on": STR, "status": STR, "outcome": None,
}


def is_type(v, t):
    if t == STR:
        return isinstance(v, str) and v.strip() != ""
    if t == INT:
        return isinstance(v, int) and not isinstance(v, bool)
    if t == NUM:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == BOOL:
        return isinstance(v, bool)
    if t == LIST:
        return isinstance(v, list)
    if t == OBJ:
        return isinstance(v, dict)
    return True


def day(text):
    return datetime.date(*[int(x) for x in text.split("-")])


def grid_size(params):
    n = 1
    for v in params.values():
        n *= len(v)
    return n


def registered_hash(doc):
    body = {k: v for k, v in doc.items() if k not in ("status", "outcome")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def problems(doc, lab_gates, name=None):
    """Every way `doc` breaks the pre-registration schema (an empty list when it is valid)."""
    out = []

    def need(cond, msg):
        if not cond:
            out.append(msg)

    if not isinstance(doc, dict):
        return ["not an object"]
    for k in sorted(set(doc) - set(FIELDS)):
        out.append("unknown field %s" % k)
    for k, t in FIELDS.items():
        if k not in doc:
            out.append("missing %s" % k)
        elif not is_type(doc[k], t):
            out.append("%s is not a %s" % (k, t))
    if out:
        return out
    need(doc["schema_version"] == 1, "schema_version is 1")
    need(re.match(r"^eh-\d\d-[a-z0-9-]+$", doc["id"]) is not None, "id is eh-NN-slug")
    if name is not None:
        need(doc["id"] == name, "id matches the file name")
    need(doc["rank"] >= 1, "rank starts at 1")
    need(doc["family"] in FAMILIES, "family %r" % doc["family"])
    m = doc["mechanism"]
    need(isinstance(m.get("kind"), list) and m["kind"] and set(m["kind"]) <= MECHANISMS, "mechanism.kind")
    need(is_type(m.get("text"), STR), "mechanism.text")
    need(len(doc["evidence"]) >= 2, "at least 2 evidence entries")
    for e in doc["evidence"]:
        need(isinstance(e, dict) and is_type(e.get("source"), STR) and is_type(e.get("year"), INT)
             and is_type(e.get("market"), STR) and is_type(e.get("finding"), STR), "evidence entry %r" % (e,))
    ins = doc["instrument"]
    need(ins.get("segment") in SEGMENTS, "instrument.segment")
    need(is_type(ins.get("underlying"), STR) and is_type(ins.get("side"), STR), "instrument underlying and side")
    u = doc["universe"]
    need(is_type(u.get("name"), STR) and is_type(u.get("point_in_time"), BOOL) and is_type(u.get("rule"), STR), "universe")
    t = doc["turnover"]
    need(is_type(t.get("round_trips_per_year"), NUM) and t["round_trips_per_year"] >= 0, "turnover.round_trips_per_year")
    need(is_type(t.get("charge_drag_pct_per_year"), NUM) and t["charge_drag_pct_per_year"] >= 0, "turnover.charge_drag")
    need(is_type(t.get("basis"), STR), "turnover.basis")
    d = doc["data"]
    need(isinstance(d.get("needs"), list) and d["needs"], "data.needs")
    need(isinstance(d.get("available_now"), list), "data.available_now")
    need(isinstance(d.get("blocked_by"), list) and all(is_type(x, INT) for x in d["blocked_by"]), "data.blocked_by")

    sp = doc["span"]
    windows = []
    for w in ("train", "validation", "holdout"):
        win = sp.get(w)
        try:
            a, b = day(win["from"]), day(win["to"])
        except (TypeError, KeyError, ValueError, AttributeError):
            out.append("span.%s needs ISO from and to" % w)
            continue
        need(a < b, "span.%s from before to" % w)
        windows.append((w, a, b))
    emb = sp.get("embargo_days")
    need(is_type(emb, INT) and emb >= MIN_EMBARGO_DAYS, "span.embargo_days at least %d" % MIN_EMBARGO_DAYS)
    if len(windows) == 3 and is_type(emb, INT):
        for (w1, _, end), (w2, start, _) in zip(windows, windows[1:]):
            need(start > end, "%s starts after %s ends (no overlap)" % (w2, w1))
            need((start - end).days >= emb, "embargo between %s and %s" % (w1, w2))

    p = doc["parameters"]
    need(len(p) >= 1, "parameters")
    for k, v in p.items():
        ok = isinstance(v, list) and v and all(isinstance(x, (str, int, float, bool)) for x in v)
        need(ok, "parameters.%s is a list of values" % k)
        if ok:
            need(len(set(json.dumps(x) for x in v)) == len(v), "parameters.%s has no repeats" % k)
    if not [x for x in out if x.startswith("parameters")]:
        need(doc["trial_budget"] == grid_size(p), "trial_budget equals the grid size")
        need(doc["trial_budget"] <= MAX_GRID, "at most %d grid points" % MAX_GRID)
    pt = doc["prior_trials"]
    need(is_type(pt.get("count"), INT) and pt["count"] >= 0 and is_type(pt.get("basis"), STR), "prior_trials")

    g = doc["gates"]
    need(set(g) == {"lab_defaults", "overrides", "extra", "min_span_years", "span_exception"}, "gates fields")
    need(g.get("lab_defaults") is True, "gates.lab_defaults is true")
    ov = g.get("overrides") if isinstance(g.get("overrides"), dict) else {}
    for k, v in ov.items():
        need(k in lab_gates, "gates.overrides.%s is not a trader-lab gate" % k)
        need(is_type(v, NUM), "gates.overrides.%s is a number" % k)
    ex = g.get("extra") if isinstance(g.get("extra"), dict) else {}
    for k in ex:
        need(k in EXTRA_GATES, "gates.extra.%s is not documented" % k)
    if "drawdown_basis" in ex:
        need(ex["drawdown_basis"] in DRAWDOWN_BASES, "gates.extra.drawdown_basis")
    if "verdict_cap" in ex:
        need(ex["verdict_cap"] == "WATCH", "gates.extra.verdict_cap")
    if "fdr_q" in ex:
        need(is_type(ex["fdr_q"], NUM) and 0 < ex["fdr_q"] < 1, "gates.extra.fdr_q")
    need(g.get("min_span_years") == 10, "gates.min_span_years is 10")
    exc = g.get("span_exception")
    need(exc is None or is_type(exc, STR), "gates.span_exception is null or text")
    if len(windows) == 3:
        years = (windows[2][2] - windows[0][1]).days / 365.25
        if years < 10:
            need(is_type(exc, STR), "a span under 10 years needs gates.span_exception")
            need(ex.get("verdict_cap") == "WATCH", "a short span caps the verdict at WATCH")

    need(doc["primary_metric"] in METRICS, "primary_metric")
    hr = doc["holdout_rule"]
    need(hr.get("opened_once") is True and is_type(hr.get("pass_if"), STR), "holdout_rule")
    need(len(doc["kill_criteria"]) >= 2 and all(is_type(x, STR) for x in doc["kill_criteria"]), "kill_criteria")
    for k, score in (("prior", "probability"), ("feasibility", "score")):
        v = doc[k].get(score)
        need(is_type(v, NUM) and 0 <= v <= 1, "%s.%s in 0..1" % (k, score))
        need(is_type(doc[k].get("reason"), STR), "%s.reason" % k)
    try:
        day(doc["registered_on"])
    except ValueError:
        out.append("registered_on is an ISO date")
    need(doc["status"] in STATUSES, "status")
    if doc["status"] == "registered":
        need(doc["outcome"] is None, "a registered hypothesis has no outcome")
    else:
        need(isinstance(doc["outcome"], dict), "a run hypothesis has an outcome")
    return out


def read_all():
    docs = {}
    for f in sorted(os.listdir(DIR)):
        if f.endswith(".json") and f != "manifest.json":
            with open(os.path.join(DIR, f), encoding="utf-8") as fh:
                docs[f[:-5]] = json.load(fh)
    return docs


class EdgeHunt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = load("trader-lab", "lab")
        cls.docs = read_all()
        with open(os.path.join(DIR, "manifest.json"), encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        with open(BRIEF, encoding="utf-8") as fh:
            cls.brief = fh.read()

    def good(self):
        return json.loads(json.dumps(self.docs["eh-01-xs-momentum"]))

    # ------------------------------------------------------------ the validator itself

    def test_validator_accepts_a_good_file(self):
        self.assertEqual(problems(self.good(), self.lab.GATE_DEFAULTS, "eh-01-xs-momentum"), [])

    def test_validator_rejects_broken_files(self):
        gates = self.lab.GATE_DEFAULTS
        cases = [
            (lambda d: d.pop("kill_criteria"), "missing kill_criteria"),
            (lambda d: d.update(surprise="x"), "unknown field surprise"),
            (lambda d: d.update(family="astrology"), "family"),
            (lambda d: d.update(trial_budget=17), "trial_budget equals the grid size"),
            (lambda d: d["parameters"].update(top_n=[20, 20]), "no repeats"),
            (lambda d: (d["parameters"].update(extra=[1, 2, 3]), d.update(trial_budget=48)), "at most 36"),
            (lambda d: d["span"]["validation"].update({"from": "2018-06-01"}), "no overlap"),
            (lambda d: d["span"]["holdout"].update({"from": "2023-01-02"}), "embargo between validation and holdout"),
            (lambda d: d["span"].update(holdout={"from": "2012-01-02", "to": "2013-01-01"}), "no overlap"),
            (lambda d: d["span"]["train"].update({"to": "2010-01-01"}), "from before to"),
            (lambda d: d["gates"]["overrides"].update(min_vibes=1), "not a trader-lab gate"),
            (lambda d: d["gates"]["extra"].update(secret_sauce=1), "not documented"),
            (lambda d: d["gates"].update(lab_defaults=False), "lab_defaults"),
            (lambda d: d["prior"].update(probability=1.5), "prior.probability"),
            (lambda d: d.update(status="maybe"), "status"),
            (lambda d: d.update(outcome={"verdict": "WATCH"}), "no outcome"),
            (lambda d: d["holdout_rule"].update(opened_once=False), "holdout_rule"),
            (lambda d: d.update(evidence=d["evidence"][:1]), "at least 2 evidence"),
            (lambda d: d["span"].update(train={"from": "2021-01-04", "to": "2022-06-30"},
                                        validation={"from": "2022-07-15", "to": "2022-12-30"}),
             "needs gates.span_exception"),
        ]
        for mutate, expect in cases:
            d = self.good()
            mutate(d)
            got = problems(d, gates, "eh-01-xs-momentum")
            self.assertTrue([p for p in got if expect in p], "%s: expected %r in %r" % (expect, expect, got))

    # ------------------------------------------------------------ the registered files

    def test_at_least_twelve_hypotheses_every_one_valid(self):
        self.assertGreaterEqual(len(self.docs), 12)
        for name, doc in self.docs.items():
            self.assertEqual(problems(doc, self.lab.GATE_DEFAULTS, name), [], name)

    def test_all_registered_before_any_run(self):
        for name, doc in self.docs.items():
            self.assertEqual(doc["status"], "registered", name)
            self.assertIsNone(doc["outcome"], name)
            self.assertLessEqual(doc["registered_on"], "2026-11-01", name)

    def test_holdout_after_validation_after_train(self):
        for name, doc in self.docs.items():
            s = doc["span"]
            self.assertLess(day(s["train"]["to"]), day(s["validation"]["from"]), name)
            self.assertLess(day(s["validation"]["to"]), day(s["holdout"]["from"]), name)
            # index-options holdouts sit after SEBI's Nov 2024 index-derivative changes
            if doc["instrument"]["segment"] == "OPT" and "NIFTY" in doc["instrument"]["underlying"]:
                self.assertGreaterEqual(s["holdout"]["from"], "2024-11-20", name)

    def test_the_requested_mix_is_covered(self):
        self.assertEqual({d["family"] for d in self.docs.values()}, FAMILIES)

    def test_ranks_follow_prior_times_feasibility(self):
        ranked = sorted(self.docs.values(), key=lambda d: d["rank"])
        self.assertEqual([d["rank"] for d in ranked], list(range(1, len(ranked) + 1)))
        scores = [d["prior"]["probability"] * d["feasibility"]["score"] for d in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_manifest_hashes_freeze_the_registered_content(self):
        rows = {r["id"]: r for r in self.manifest["hypotheses"]}
        self.assertEqual(set(rows), set(self.docs))
        for name, doc in self.docs.items():
            self.assertEqual(rows[name]["sha256"], registered_hash(doc),
                             "%s changed after registration: register a new id instead" % name)
            self.assertEqual(rows[name]["rank"], doc["rank"], name)
        # a status change is allowed, a parameter change is not
        d = self.good()
        d["status"] = "running"
        self.assertEqual(registered_hash(d), rows["eh-01-xs-momentum"]["sha256"])
        d["parameters"]["top_n"] = [20, 30, 40]
        self.assertNotEqual(registered_hash(d), rows["eh-01-xs-momentum"]["sha256"])

    def test_not_runnable_lab_specs(self):
        lib = self.lab.library()
        for name in self.docs:
            self.assertNotIn(name, lib)

    # ------------------------------------------------------------ the brief agrees with the files and the code

    def test_brief_lists_every_hypothesis_in_rank_order(self):
        rows = re.findall(r"^\| (\d+) \| (eh-\d\d-[a-z0-9-]+) \| ([a-z-]+) \| ([\d.]+) \| ([\d.]+) \| [\d.]+ \| (\d+) \|",
                          self.brief, re.M)
        self.assertEqual(len(rows), len(self.docs))
        for rank, hid, fam, prior, feas, grid in rows:
            d = self.docs[hid]
            self.assertEqual(int(rank), d["rank"], hid)
            self.assertEqual(fam, d["family"], hid)
            self.assertAlmostEqual(float(prior), d["prior"]["probability"], places=6, msg=hid)
            self.assertAlmostEqual(float(feas), d["feasibility"]["score"], places=6, msg=hid)
            self.assertEqual(int(grid), d["trial_budget"], hid)
        total = sum(d["trial_budget"] for d in self.docs.values())
        self.assertIn("%d hypotheses, %d grid points" % (len(self.docs), total), self.brief)

    def test_brief_cost_table_matches_the_charges_helpers(self):
        size = load("trader-budget", "size")
        fut = size.fno_round_trip_charges(23053.10, 23053.10, 65, None, "FUT", "2026-11-02")["total"]
        bank = size.fno_round_trip_charges(54686.95, 54686.95, 30, None, "FUT", "2026-11-02")["total"]
        cnc = size.round_trip_charges(1000, 1000, 100, {"product": "CNC"})["total"]
        mis = size.round_trip_charges(1000, 1000, 100, {"product": "MIS"})["total"]
        for value in (fut, bank, cnc, mis):
            self.assertIn(size.inr(value), self.brief)

    def test_brief_is_linked(self):
        for path in ("README.md", os.path.join("docs", "README.md")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                self.assertIn("research/edge-hunt.md", fh.read(), path)


if __name__ == "__main__":
    unittest.main()
