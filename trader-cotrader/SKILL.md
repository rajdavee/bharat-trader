---
name: trader-cotrader
description: >-
  Co-trader for NSE stocks: today's trade candidates from backtested rules,
  with calibrated, honest odds, or the plain decision to sit out. Takes the
  strategies trader-lab has validated (PROMOTED: lab gates plus a paper
  forward record; WATCH: gates passed, forward test running, paper only),
  checks each one fits today's market regime (trader-regime), finds live
  triggers on the latest bars, and builds complete plans: entry, stop,
  target, size (trader-budget), charges and a trader-risk PASS or BLOCK with
  reasons. Every candidate shows the share of past trades in that regime that
  made money after charges, its sample size and a 95% interval, shrunk toward
  the strategy's overall rate (empirical Bayes), the expected R after
  charges, a time-machine replay as a second opinion, what would invalidate
  it, what the evidence does NOT show and the nearest similar past trades.
  Ranked by expected value per unit risk, penalised for uncertainty and for
  correlation with open positions. Every probability is logged and later
  scored (Brier score, log loss, reliability), and overconfident strategies
  are demoted. RESEARCH ideas (unproven) only with --tier research, never for
  a live order. Use when the user says "what should I trade today", "best
  trades right now", "aaj kya trade karun", "high probability trades", "koi
  achha setup hai kya", "trade ideas for tomorrow", "how sure are you", "is
  there an edge today", "co-trader" or "how calibrated are your odds". It
  never promises profit and never places an order.
argument-hint: "[today [--tier research] | explain ID | calibration | log] [--json]"
---

# trader-cotrader

"What are the best trades right now, and how sure are we?" Most days the
honest answer is: none. On 2.3 years of real NSE data every built-in lab
strategy was rejected and strategy discovery found no finalist, so there is
usually no validated edge to offer. This skill says so plainly, with the
reasons and what would change them, and treats sitting out as a decision.
When something does qualify, it shows the evidence for and against it, never
a single magic number.

All amounts are rupees, all times IST, long only, NSE cash segment.
`<skill dir>` is this skill's folder. It needs trader-lab next to it, and
uses trader-regime, trader-budget, trader-risk and trader-timemachine when
they are installed (each is optional: without one, that part is skipped and
says so).

```bash
python3 "<skill dir>/cotrader.py" today                         # PROMOTED + WATCH candidates, or why none
python3 "<skill dir>/cotrader.py" today --tier research         # also the unproven RESEARCH ideas
python3 "<skill dir>/cotrader.py" today --json --no-write       # print the numbers, write nothing
python3 "<skill dir>/cotrader.py" explain 2026-09-21-t-sma20-watch-AXISBANK
python3 "<skill dir>/cotrader.py" calibration                   # score past probabilities; demote the overconfident
python3 "<skill dir>/cotrader.py" calibration --no-demote --json
python3 "<skill dir>/cotrader.py" log --limit 20
```

Bars come from `--data DIR` (offline files), else the local trader-archive
(`trader/archive`, `$BT_ARCHIVE`, `--archive DIR`; `--no-archive` to skip),
else trader-lab's Yahoo cache (`--offline`: no network). `--now "2026-10-30
16:00"` pins the clock; no later bar is seen. `--workdir DIR` is the folder
holding `trader/`.

## Evidence tiers (on every candidate)

| tier | what it means | who sees it |
| --- | --- | --- |
| **PROMOTED** | trader-lab PROMOTE: every backtest gate passed and the paper forward record made money | the default view |
| **WATCH** | trader-lab WATCH: every gate passed, the paper forward test is still running. Paper only | the default view |
| **RESEARCH** | anything else: a lab REJECT or DEMOTE, a library spec never run, or a strategy the co-trader demoted for poor calibration. Historical stats from the archive, NOT validated: "unproven, for study/paper only", never eligible for a live order (trader-risk blocks it on a live config) | only with `--tier research` |

The tier follows the lab's own verdict, re-checked on today's bars by
`lab.forward` (a decayed PROMOTE becomes DEMOTE and drops to RESEARCH).

## How a candidate is made (docs/cotrader.md has the method)

1. **Regime fit.** The strategy's past trades (the lab's out-of-sample
   walk-forward trades when there are 30 or more, else its whole-sample
   trades; only trades closed before the signal day) are labelled by
   trader-regime: NIFTY trend or range x India VIX bucket, as of the close
   before each signal. If today's cell holds 10 or more trades and they lose
   on average after charges, the strategy does not fit today.
2. **Live triggers.** The lab's signal engine on the latest bars (the lab's
   forward test for WATCH and PROMOTE). A signal on bars older than today is
   skipped. An options spec's trigger is the basket the lab's options forward
   test drafts through trader-strategy (WATCH and PROMOTE only), checked by
   trader-risk as one position; its R is per rupee of margin.
3. **The plan.** Entry, stop and target from the lab's plan, sized by
   trader-budget with your `trader/config.json` (the loss at the stop with
   charges inside your max loss), the charges at the target, and a
   trader-risk preview (PASS or BLOCK with every reason; for a daily signal,
   as of the next session's first entry time). Nothing is written to
   `orders.json`.
4. **Probability.** hit = a past trade that made money after charges. The
   cell's hit rate with its Wilson 95% interval, then shrunk toward the
   strategy's overall rate (Laplace-smoothed) with a Beta prior worth 20
   trades (fewer if the strategy has fewer): p = (wins + 20 x overall rate) /
   (n + 20), with the
   Beta posterior's 95% interval. The expected R after charges is shrunk the
   same way. A thin cell leans on the overall rate, and says how much.
5. **Second opinion.** trader-timemachine's Monte Carlo on the stock's own
   bars before the signal: 2,000 replays (seed 19) of the rest of the day
   (5-minute rules) or of the holding period from the last 250 daily bars
   (daily rules: stop and target only). When its green share falls outside
   the 95% interval, or its average R after charges is the other side of
   zero, the candidate says DISAGREES.
6. **Ranking.** score = expected R after charges - 0.25 x the width of its
   95% interval - 0.5 x the highest positive correlation (60 sessions of
   daily returns) with an open position or a higher-ranked candidate x the
   expected R. Ties go to the more liquid stock (median traded value over 20
   sessions). Candidates trader-risk blocks come after the ones it passes.
7. **Explanation.** Why this trade, what would invalidate it, the evidence
   in numbers, what the evidence does NOT show, and the five nearest past
   trades (same stock, same regime, similar stop distance).

## No edge is a first-class answer

When nothing qualifies the output says "No trade today: sitting out is the
decision, not a failure", lists why (no validated strategy, regime
mismatch with the numbers, no live trigger, no edge after charges, blocked
by your rules, the kill switch or a circuit breaker) and what would change
it (a strategy passing the lab's gates and its forward test, the market
moving into a regime where an eligible strategy had an edge, the next
session's reset). Say it to the user just as plainly.

## Calibration

Each candidate is logged once (`trader/cotrader/log.jsonl`: its id,
probability, interval, sample sizes, regime and plan). `calibration` finds
outcomes in this order: your paper fills (`trader/state/trades.json`), the
lab's paper forward record for that signal, then the bars after the signal
(stop first, target, out at the flat-by or after the maximum holding, net of
charges). It scores them overall, per strategy and per tier: Brier score
(with the base-rate reference), log loss and reliability bins with Wilson
intervals. A PROMOTED or WATCH strategy with 20 or more resolved predictions
whose average predicted probability is above the 95% interval of what
actually happened is overconfident: it is demoted through trader-lab's own
status file (verdict DEMOTE, which the lab keeps until the spec changes) and
recorded in `trader/cotrader/demoted.json` (RESEARCH from then on).
`--no-demote` reports only.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/lab/<name>/` | trader-lab's runs: spec, results (the trades), status (the verdict) |
| `trader/config.json`, `portfolio.json`, `prices.json`, `state/` | your limits, account and today's orders, for sizing and the risk preview |
| `trader/cotrader.json` | the last run, schema `bharat-trader/cotrader/1`: regime, every strategy's status, candidates, passed over, no-edge |
| `trader/cotrader/plans/<ID>.json` | each candidate's plan in trader-exit's shape, for trader-budget |
| `trader/cotrader/log.jsonl` | every prediction, once (schema `bharat-trader/cotrader-log/1`) |
| `trader/cotrader/demoted.json` | strategies demoted for poor calibration |

## To paper trade a candidate

The candidate's plan goes through the same path as every order:

```bash
cp trader/cotrader/plans/<ID>.json trader/plan.json
python3 <trader-budget dir>/size.py --write         # sizes it into trader/orders.json
python3 <trader-risk dir>/check.py                  # PASS or BLOCK, recorded
python3 <trader-broker dir>/ticket.py show          # the paper ticket and the user's own typed yes
```

The UI's Co-trader page has a "Draft to paper" button doing the first two
steps; from an AI assistant, `paper_order_draft` with `candidate: ID`. Then
the user's typed yes for that one order, and nothing else.

## How to talk about it

Quote the tier, the probability with its interval and n, the expected R
after charges, the regime, the risk verdict and the "does NOT show" list.
Never call a candidate a sure thing, never project returns, never hide a
DISAGREES line or a thin sample, and never present a RESEARCH idea as
anything but unproven. A day with no candidates is a good outcome.

From an AI assistant, the MCP server (`mcp/server.py`) offers
`cotrader_candidates`, `cotrader_explain` and `calibration_report`.

## Hinglish

Plain words in Hinglish (Hindi in Roman script, trading words in English): `today` and `explain` take `--lang hi-en` (candidates, odds, the sit-out answer, what the evidence does NOT show), or set `"lang": "hi-en"` in `trader/config.json` for every run. Numbers, ₹, IST, tickers and `--json` output stay the same. See [docs/hinglish.md](../docs/hinglish.md).

```bash
python3 trader-cotrader/cotrader.py today --lang hi-en
```

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
