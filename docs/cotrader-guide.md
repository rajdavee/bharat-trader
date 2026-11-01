# The co-trader guide: a day, a week, and what the numbers mean

bharat-trader is an **evidence-first co-trader**. It looks for trades that
past data supports after Indian charges. When it finds none, it tells you to
sit out, and that is the answer you will get most days. This page puts the
pieces together: the daily routine, the weekly review, how a strategy earns
(and loses) its place, what the probabilities mean, how to run it from your
AI assistant, and the safety model.

Education and tooling, not financial advice. Nothing here places an order on
its own.

## Start with the honest result

Before you trust any of this, here is what it found on real data:

- **Every built-in strategy is REJECTED.** That covers the six equity rules
  (opening-range breakout, VWAP reclaim, gap fade, previous-day-high
  breakout, RSI(2), 20-day breakout swing) on ten liquid NIFTY 50 stocks,
  and the five option strategies (the weekly 16-delta strangle and condor,
  the expiry-day iron fly, the BANKNIFTY monthly put spread and the IV-rank
  strangle) on 703 sessions of NSE F&O bhavcopies (January 2024 to October
  2026). The expiry-day NIFTY iron fly came closest: it passed 6 of 8 gates
  and failed the out-of-sample profit factor and the deflated Sharpe ratio.
  See [lab.md](lab.md#options-strategies).
- **Discovery found nothing.** A 500-trial search over NIFTY 50 daily rules
  on about 2.3 years of archive data counted 527 trials. 16 survived
  validation and **0** survived the robustness and multiple-testing checks
  (the Reality Check p was 0.84). See [discovery.md](discovery.md).
- **Past odds were too high.** On the tests' real prediction log (38
  predictions, December 2025 to October 2026) the co-trader's probabilities
  averaged 48%, but only 29% of those trades made money after charges. That
  is overconfident, and it is the reason every prediction is logged and
  scored. See [cotrader.md](cotrader.md#calibration).

So the tool's first job is to keep you out of trades that have no evidence
behind them. If you build a strategy that passes, the same machinery tracks
it honestly from then on.

## The daily workflow

| when (IST) | step | command | UI page |
| --- | --- | --- | --- |
| 08:45 to 09:15 | pre-open brief | `doctor.py`, `regime.py now`, `cotrader.py today` | Today |
| 09:30 to entry cut-off | co-trader candidates | `cotrader.py today`, `explain ID` | Co-trader |
| all session | paper auto-pilot (optional) | `autopilot.py start` | Auto-pilot |
| after 15:30 | journal | `journal.py sync` | Journal |
| weekend | weekly review | `journal.py review --week last`, `cotrader.py calibration` | Journal, Co-trader |
| any day | promote or demote | automatic, via `lab.py forward NAME` | Strategy Lab |
| live, PROMOTED only | co-pilot | `copilot.py propose`, `approve ID` | Co-pilot |

### 1. Pre-open brief (08:45 to 09:15)

```bash
python3 trader-doctor/doctor.py                     # is every data source answering?
python3 trader-archive/archive.py update            # yesterday's bhavcopies into the local archive
python3 trader-regime/regime.py now                 # trend or range, India VIX bucket, gap, expiry, event days
python3 trader-cotrader/cotrader.py today           # candidates, or why to sit out
```

The regime matters because each strategy is judged on its past trades in
today's kind of market: NIFTY trending or ranging, crossed with the India
VIX bucket. A rule that lost money after charges in this cell, over 10 or
more past trades, is passed over and the output shows the numbers. The
**Today** page shows the same brief: the session clock, your loss budget,
the regime, and the top ready candidates or the no-edge headline.

### 2. The co-trader (the entry window)

`cotrader.py today` only offers strategies that trader-lab has validated:

- **PROMOTED**: every gate passed, and at least 20 paper forward trades made money.
- **WATCH**: every gate passed, but the paper forward test is still running.
  Paper only.
- **RESEARCH**: failed or never ran. Shown only with `--tier research`, for
  study. Never eligible for a live order.

Each candidate is a full plan: entry, stop, target, size from your config,
charges, the trader-risk verdict, the probability with its range and sample
size, the expected R after charges, a second opinion from the time machine,
what would prove it wrong, and what the evidence does NOT show.

```bash
python3 trader-cotrader/cotrader.py explain 2026-09-21-t-sma20-watch-AXISBANK   # one candidate in full
python3 trader-cotrader/cotrader.py today --tier research                       # the unproven ideas, for study
```

To paper-trade a candidate, use **Draft to paper** on the Co-trader page,
`paper_order_draft` with `candidate: ID` from your assistant, or the plan
file through the usual chain (see
[cotrader.md](cotrader.md#paper-trading-a-candidate)). trader-budget sizes
it, trader-risk checks it, and the ticket waits for your own typed yes.

**"No trade today: sitting out is the decision, not a failure."** When that
is the first line, read the reasons under it (no validated strategy, wrong
regime, no trigger, no edge after charges, your own limits) and close the
terminal.

### 3. The paper auto-pilot (optional)

An auto-pilot that only runs on paper gives each WATCH and PROMOTED
strategy a real forward record without you at the keyboard. It runs on the
NSE clock: pre-open brief, entries inside each strategy's capital bucket,
trader-risk on every order, stops, targets, the flat-by, and an end-of-day
report.

```bash
python3 trader-autopilot/autopilot.py start --dry-run   # decide and record, place nothing
python3 trader-autopilot/autopilot.py start             # the loop on PAPER; stop or Ctrl-C ends it
python3 trader-autopilot/autopilot.py report            # today's report
python3 trader-autopilot/autopilot.py replay --date 2026-09-21   # a past day on a simulated clock, in a sandbox
```

It trades only the strategies listed in the `autopilot` block of your config
(see [autopilot.md](autopilot.md#the-config)). It never trades RESEARCH, and
it refuses a live config.

### 4. The journal (after the close)

```bash
python3 trader-journal/journal.py sync                  # every closed trade, after charges
python3 trader-journal/journal.py stats --by regime     # did you make money where the lab said you would?
```

The auto-pilot runs the sync itself at the end of the day. Write a note on
any trade where you broke a rule. The rule-break log and the counterfactual
ledger (the candidates you skipped and the orders risk blocked, followed to
their outcome) are only as honest as your notes. See [journal.md](journal.md).

### 5. The weekly review (weekend)

```bash
python3 trader-journal/journal.py review --week last    # trader/journal/weekly-YYYY-Www.md
python3 trader-journal/journal.py counterfactual        # what skipping and blocking saved or cost
python3 trader-cotrader/cotrader.py calibration         # did past probabilities come true?
python3 trader-lab/lab.py compare orb15-vwap vwap-reclaim
```

Under 20 trades, review your process, not your results: did you follow the
plan, the stop, the flat-by? Make one rule-shaped change a week at most. The
journal coach persona and the `review_my_week` prompt follow the same rules.

### 6. Promote and demote (automatic)

You do not promote a strategy by hand. The lab's rules are written down
before the run, and the forward record decides:

```bash
python3 trader-lab/lab.py run orb15-vwap                # backtest, walk-forward, gates: REJECT or WATCH
python3 trader-lab/lab.py forward orb15-vwap            # settle the paper forward record, decide again
python3 trader-lab/lab.py report orb15-vwap             # the verdict and what it does NOT prove
```

- **REJECT to WATCH**: every pre-registered gate passes (30 or more
  out-of-sample trades, positive expectancy after charges, profit factor of
  at least 1.2, deflated Sharpe of at least 0.95, overfitting odds under 0.5,
  survives 1.5x costs, a positive parameter neighbourhood, a drawdown limit).
- **WATCH to PROMOTE**: 20 or more paper forward trades with a positive mean
  R and a profit factor of at least 1. A WATCH whose forward record fails
  goes back to REJECT.
- **PROMOTE to DEMOTE**: the last 30 forward trades average 0R or less, or
  the forward drawdown passes 1.5x the out-of-sample one. The co-trader's
  calibration also demotes a strategy whose predictions were overconfident
  beyond chance (20 or more outcomes). A DEMOTE stays until the spec changes.

The auto-pilot runs `lab.forward` at the end of every day, so a demoted
strategy stops trading the next session without anyone stepping in. See
[lab.md](lab.md#gates) and [autopilot.md](autopilot.md#how-the-forward-record-feeds-promotion).

### 7. The live co-pilot (PROMOTED only)

`trader-copilot` is the only bridge from this loop to real money, and it is
slow on purpose: one live order, one typed approval.

```bash
python3 trader-copilot/copilot.py eligibility      # which strategies may trade live, and why not
python3 trader-copilot/copilot.py propose          # live tickets from PROMOTED candidates, nothing sent
python3 trader-copilot/copilot.py approve CP-20261029-1
python3 trader-copilot/copilot.py caps             # today's live orders, capital at risk, live loss
```

- **Eligible:** only a PROMOTED strategy whose paper forward record meets
  your thresholds (trades, weeks, drawdown in R, the Brier score of its odds,
  no recent demote). Everything else stays on paper, and the eligibility
  table says why.
- **Each proposal** is a co-trader candidate sized in the co-pilot's own
  live capital bucket, with trader-risk PASS on your live config and a plain
  live ticket (max loss with charges, linked exits, evidence, calibration,
  the broker's margin).
- **You approve each order** by typing its exact phrase (`yes live BUY 42
  RELIANCE`) in the terminal, or in the UI's Co-pilot page after a
  10-second countdown. The phrase is tied to that proposal and works once.
  Then trader-broker's six live gates and the static-IP preflight apply.
- **Caps:** daily live orders, capital at risk and live loss, with an
  automatic live kill switch, a cool-off after a loss, and proposals that
  expire or are invalidated by a price move. Every live order gets a shadow
  paper twin.

There is no auto-approve, and no assistant can approve (the MCP tools
`copilot_status` and `copilot_proposals` are read only). Fully autonomous
live trading is not planned; the reasons are in
[#67](https://github.com/rajdavee/bharat-trader/issues/67). See
[copilot.md](copilot.md).

## What a probability means, and what it does not

Each candidate carries a line like **"52% (95% range 38% to 66%, n = 11)"**.

**How it is made** (the full method is in
[cotrader.md](cotrader.md#how-a-probability-is-made)):

1. "Hit" means a past trade of this rule that **made money after charges**
   (brokerage, STT, exchange fees, stamp duty, GST and slippage).
2. Count the hits among the rule's past trades in today's regime cell. Use
   the out-of-sample trades when there are 30 or more.
3. A thin cell is **shrunk** toward the rule's overall rate with a prior
   worth up to 20 trades, so 3 wins out of 4 does not read as 75%. The range
   is the 95% interval of that estimate. The candidate also shows how much
   weight the cell got.
4. The **expected R after charges** is shrunk the same way. A candidate
   needs it above 0R.
5. The time machine replays the plan 2,000 times on the stock's own past as
   a **second opinion**. When the two methods disagree the candidate says
   **DISAGREES**. Read that as a reason for caution. It does not break a tie.

**What it means:** about that share of this rule's past trades, in this kind
of market, made money after charges. The range shows where the true rate
plausibly sits, given how few trades there were.

**What it does not mean:**

- It is not the chance that *this* trade wins. Markets change, and today's
  news is not in the data.
- It is not a forecast of returns. A 70% hit rate can still lose money if
  the losses are bigger than the wins, which is why expected R is shown
  next to it.
- A narrow-looking number from a small n is not precise. Always read the
  range and n, and never quote the point number without them.
- Fills, slippage and charges are models. The universe is today's liquid
  stocks applied to the past (survivorship bias).
- A WATCH rule has not finished proving itself, and a RESEARCH rule failed.

**Calibration** is how you find out whether the numbers deserve trust. Each
probability is logged once, the first time it appears. `cotrader.py
calibration` later resolves the outcome (from paper fills first, then the
lab's forward record, then the bars) and scores the log:

- the **Brier score**: lower is better, shown next to a constant guess at the
  base rate;
- **log loss**, which punishes confident misses;
- **reliability bins**: of the trades called 40% to 50%, how many made money?

The Co-trader page draws predicted against observed. A strategy whose
average prediction sits above the range of what actually happened, over 20
or more outcomes, is demoted.

## Using your AI assistant

Connect the MCP server (see [mcp.md](mcp.md#install), or the
[README's quick start](../README.md#3-from-your-ai-assistant-mcp)). It gives
your assistant 57 tools and 16 prompts and no live-order tool (the
co-pilot's two tools are read only). The
co-trader prompts tell the assistant to show n and the 95% interval with
every rate, give numbers after charges, name the regime, say what the
evidence does NOT show, and never promise a result.

| when | prompt | or ask |
| --- | --- | --- |
| pre-open | `daily_cotrader_brief` | "Give me today's co-trader brief: regime, candidates with their odds and n, my open risk and how well past odds held up." |
| entry window | `find_best_trade_now` | "Is there anything worth taking right now? Rank by the low end of the range, and tell me if the answer is to sit out." |
| a candidate | (`cotrader_explain`) | "Explain candidate 2026-09-21-t-sma20-watch-AXISBANK: why, what would prove it wrong, and what the evidence does not show." |
| an idea | `design_strategy_for_window` | "Design an expiry-day NIFTY strategy, pre-register the gate that kills it, then test it once. How many trials does that family have now?" |
| an idea you like | `stress_test_my_idea` | "Try to break my opening-range idea: costs, neighbourhood, out-of-sample n, regime cells. Give me the strongest argument against." |
| weekend | `review_my_week` | "Review my week: stats with ranges, rule breaks, what skipping and blocking saved or cost, calibration. One change only." |
| the auto-pilot | (`autopilot_status`, `autopilot_replay`) | "How did the paper auto-pilot do today? Replay last Monday for the WATCH strategies." |

Two habits keep this honest:

- **Do not ask for variant after variant until one passes.** Every
  `lab_run_spec` is logged with its strategy family, and later specs of that
  family pay for the earlier trials in the deflated Sharpe. Fishing raises
  the bar.
- **Proposing is not placing.** The assistant can draft a paper ticket with
  `paper_order_draft`. Confirming needs your own typed yes, which it cannot
  supply for you. Asking it to "just buy" gets a careful no.

Prefer to chat inside the web UI? The optional **Ask** page relays to your
own LLM endpoint with the same read-only tools and the paper draft only. It
cannot confirm anything. See [ai-chat.md](ai-chat.md).

## The safety model, in one place

| layer | what it enforces | where |
| --- | --- | --- |
| Paper by default | `"paper": true` in a fresh config. Live needs your explicit change. | [configuration.md](configuration.md) |
| trader-risk | PASS or BLOCK on every order: stop, max loss with charges, daily loss, trades per day, entry window, size, cash, F&O basket limits, circuit-breakers, order rate, kill switch | [trader-risk/SKILL.md](../trader-risk/SKILL.md) |
| Your typed yes | Every ticket waits for your own words. "ok" and "haan" are refused. | [trader-broker/SKILL.md](../trader-broker/SKILL.md) |
| Evidence tiers | RESEARCH ideas are blocked by trader-risk whenever the config is live. | [cotrader.md](cotrader.md#where-candidates-come-from) |
| Auto-pilot | Paper only, checked in code in four places. Its machine yes is labelled `"actor": "autopilot"`, and `place` refuses any yes you did not type. | [autopilot.md](autopilot.md#the-safety-model) |
| AI assistant | The MCP server has no live-order tool. The Ask page cannot confirm. | [mcp.md](mcp.md#security-model) |
| Live co-pilot | PROMOTED strategies with enough paper record only; your exact typed phrase per order; daily caps and a live kill switch; no auto-approve. | [copilot.md](copilot.md) |
| Live orders | Six gates: the broker in config, `"paper": false`, `live_enabled`, risk PASS for that exact ticket, your typed yes, and today's static-IP preflight. You run `ticket.py place` yourself. | [brokers.md](brokers.md) |
| Kill switch | `check.py kill` stops new entries. The auto-pilot also flattens on paper. | [configuration.md](configuration.md) |

What it will never do: call a trade guaranteed, sure-shot or risk-free;
trade a RESEARCH idea live; place a live order without you; or run fully
autonomous live trading. That last one is an open decision, marked not
planned, in [#67](https://github.com/rajdavee/bharat-trader/issues/67).

## Read next

[cotrader.md](cotrader.md) (the method) · [lab.md](lab.md) (the gates) ·
[discovery.md](discovery.md) (the search) · [autopilot.md](autopilot.md) ·
[copilot.md](copilot.md) · [journal.md](journal.md) · [regime.md](regime.md) · [mcp.md](mcp.md) ·
[archive.md](archive.md)

Education and tooling, not financial advice.
