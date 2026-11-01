# Use it from your AI assistant (MCP)

`mcp/server.py` is a [Model Context Protocol](https://modelcontextprotocol.io/)
server, so any MCP-compatible AI assistant (desktop and IDE assistants, chat
apps that support MCP) can call the bharat-trader tools for you: market
status, scans, news, entry and exit plans, the time machine, sizing, risk
checks and scenarios, the paper broker (shares and F&O), option chains and
strategies, the Strategy Lab, results, insider deals, the data self-test,
your broker's holdings and positions (read only) and the agent personas.
On top of those sits a co-trader workflow layer: a one-call trade brief,
the best lab-tested strategies for today's regime, strategy design into a
lab spec, running your own spec with honest trial counting, and side-by-side
comparisons ([using your assistant as a co-trader](#using-your-assistant-as-a-co-trader)).
New to the project? [getting-started.md](getting-started.md) has the
whole tour.

Python standard library only. Nothing to install beyond the repo.

## Install

1. Get the repo: `git clone https://github.com/rajdavee/bharat-trader`.
2. Make a trading folder (the tools read and write its `trader/` subfolder),
   and create your rules once: `python3 /path/to/bharat-trader/trader-budget/size.py init --config /path/to/trading-folder/trader/config.json`.
3. Add the server to your MCP client's configuration. Most clients take a
   command and its arguments; the shape is usually this:

```json
{
  "mcpServers": {
    "bharat-trader": {
      "command": "python3",
      "args": ["/path/to/bharat-trader/mcp/server.py", "--workdir", "/path/to/trading-folder"]
    }
  }
}
```

On Windows use `"command": "python"` and paths like
`"C:\\Users\\you\\bharat-trader\\mcp\\server.py"`.

4. Restart the client. It should list 51 tools and 16 prompts from
   `bharat-trader`.

Try it without your own data first: point `--offline` at the saved real
answers in the repo (the clock is pinned to when they were taken, and a
throwaway copy of the demo paper account is used):

```json
{
  "mcpServers": {
    "bharat-trader-demo": {
      "command": "python3",
      "args": ["/path/to/bharat-trader/mcp/server.py", "--offline", "/path/to/bharat-trader/tests/fixtures/mcp"]
    }
  }
}
```

### Options

| option | meaning |
| --- | --- |
| `--workdir DIR` | The folder whose `trader/` files the tools read and write. Default: the current folder. Every write lands in `DIR/trader/`. |
| `--offline DIR` | Saved answers instead of the network (below). The network is switched off for the whole process. |
| `--now "YYYY-MM-DD HH:MM"` | Pin the clock (IST), like the skills' `--now`. |
| `--http PORT` | Streamable HTTP on `http://127.0.0.1:PORT/mcp` instead of stdio. `0` picks a free port. Loopback only; there is no option to bind anything else. |
| `--rate-limit N` | Tool calls a minute (default 30). Past it a call returns a tool error saying how long to wait. |
| `--page-size N` | Items per `tools/list`, `resources/list`, `prompts/list` page (default 100). |
| `--tool-reference` | Print the tool table below and exit. |
| `--verbose` | Log HTTP requests to stderr. |

`--offline DIR` reads `DIR/offline.json` when it is there: each tool can name
its own saved file or folder (`{symbol}` is filled in) and pin its own clock,
and `"trader"` names a demo `trader/` folder to copy. Without one, each skill
gets `DIR/<skill folder>` when that exists, else `DIR`.
[`tests/fixtures/mcp/offline.json`](../tests/fixtures/mcp/offline.json) is
the bundle the tests use.

## Tools

Every result has the same envelope: `tool`, `as_of` (IST), `offline`,
`paper_only: true`, `data` (the skill's own answer), `notes` and
`disclaimer: "Education and tooling, not financial advice."`. It comes as
`structuredContent` (checked against the tool's `outputSchema`) and as the same
JSON in a text block. A problem the assistant can fix (a bad symbol, a missing
plan, a refused yes) is a tool result with `isError: true` and a plain-words
message.

<!-- tools:start -->
| tool | what it does | arguments | hints |
| --- | --- | --- | --- |
| `market_status` | **NSE market status.** Is NSE open right now? The session phase, today's hours, the next open, special sessions, from the NSE calendar (no network). verify: true also asks nseindia.com. | `verify`? | read-only; network |
| `scan_daily` | **Daily setups scan.** Pre-market scan of the NSE universe (trader/universe.txt or symbols) on daily closes: breakouts, pullbacks, RSI, liquidity filters. Nothing is written. | `symbols`?, `limit`?, `min_price`?, `min_turnover`? | read-only; network |
| `scan_orb` | **Opening-range breakout scan.** Opening-range breakouts so far today across the universe, on 5-minute bars. Nothing is written. | `symbols`?, `limit`?, `min_price`?, `min_turnover`?, `at`?, `until`?, `or_minutes`? | read-only; network |
| `news` | **News and announcements.** Headlines, NSE corporate announcements, upcoming board meetings and today's move for your holdings, plan and watchlist (or tickers). Read only (the seen-items state is not updated). | `tickers`?, `hours`?, `market`?, `include_seen`? | read-only; network |
| `entry_check` | **Entry trigger check.** Has this stock's setup triggered? Opening-range breakout (orb) or a close above a level, bar by bar on 5m or 1m Yahoo bars, inside the entry window. save: true writes trader/entry.json for exit_plan. | `symbol`, `setup`?, `level`?, `or_minutes`?, `interval`?, `save`? | writes trader/; network |
| `exit_plan` | **Stop and target plan.** Set the stop and target for a long entry, from trader/entry.json or from symbol + entry (+ or_high/or_low): or-mid, or-low, pct:X, atr:K or price:X stops; 1R/1.5R/2R or price:X targets; the flat-by time and a charges estimate. save: true writes trader/plan.json. | `symbol`?, `entry`?, `or_high`?, `or_low`?, `or_minutes`?, `setup`?, `date`?, `time`?, `interval`?, `stop`?, `target`?, `flat_by`?, `qty`?, `save`? | writes trader/ |
| `timemachine_run` | **Time machine replay.** Replay the plan (trader/plan.json, or symbol/entry/stop/target) over thousands of simulated sessions drawn from the stock's own recent 5-minute bars, plus the real history: odds of the target before the stop, R after charges, a fan of outcomes. Nothing is written. | `symbol`?, `entry`?, `stop`?, `target`?, `date`?, `entry_time`?, `replays`?, `seed`? | read-only; network |
| `size_trade` | **Position size.** How many shares the plan allows under your rules: risk per trade, max position, cash buffer, charges, tick size. Reads trader/plan.json and trader/config.json. Nothing is written. | `capital`?, `limit`?, `market`? | read-only |
| `risk_check` | **Risk check.** trader-risk on trader/orders.json: every order against your limits (size, loss at stop + charges, reward:risk, daily loss, trades, entry window, duplicates), the kill switch, the circuit-breakers (losing streak, daily drawdown, cooling-off, revenge trades) and the order rate; F&O legs as one basket (lots, ban list, premium, margin, Greeks, scenarios). A preview by default; record: true records the verdicts the paper broker needs. | `record`? | writes trader/ |
| `risk_scenarios` | **Risk scenarios.** The F&O book's net Greeks (delta lots, gamma, vega, theta) and its P&L after charges on a grid of spot +/-1/2/3 sigma x IV +/-5/10 vol, against your limits; open positions alone or with every F&O proposal in trader/orders.json. Also the kill switch and circuit-breaker state. Read only. | `book`? | read-only |
| `paper_orders_show` | **Paper order tickets.** The order tickets in trader/orders.json, in plain words, each with its trader-risk status and fingerprint. | none | read-only |
| `paper_order_draft` | **Draft paper orders.** Size trader/plan.json (or, with candidate: ID, that co-trader candidate's plan) into order tickets (trader/orders.json, the old one kept as .bak) and record trader-risk's verdicts. Returns each ticket with its fingerprint. Places nothing. | `capital`?, `limit`?, `market`?, `persona`?, `candidate`? | writes trader/ (replaces a file, keeps a .bak) |
| `paper_order_confirm` | **Confirm one paper order.** Record the USER's yes for ONE ticket and place it on the PAPER account (no real money; no live broker exists in this server). Needs the ticket's index and its exact risk-PASS fingerprint. When the client supports elicitation the server asks the user directly and yes_text is ignored; otherwise yes_text must be the user's own reply, word for word, after you showed them the ticket. Never write a yes yourself. | `index`, `risk_fingerprint`, `yes_text`? | writes trader/ |
| `portfolio_status` | **Paper portfolio.** The paper account: cash, positions marked to a delayed price, P&L if closed now after charges, working stop/target legs, today's fills and limits. | `live_quotes`? | read-only; network |
| `paper_mtm` | **Paper F&O mark to market.** The paper F&O book marked to the latest chain (trader/options.json): positions grouped by strategy, P&L (open and realised), Black-76 Greeks per leg, per strategy and in all, margin blocked, and the payoff at expiry of each open structure. Read only. | `strategy`? | read-only |
| `options_chain` | **Option chain.** NSE option chain for an index or F&O stock and expiry: LTP, bid/ask, OI and change, volume, our Black-76 IV and Greeks per strike around the money, lot size, forward, ATM straddle. | `symbol`, `expiry`?, `strikes`? | read-only; network |
| `options_summary` | **Options summary.** One expiry in numbers: PCR (OI and volume), max pain, call and put walls, ATM IV, 25-delta skew, expected move from the straddle and from IV, IV rank and percentile from India VIX (NIFTY). | `symbol`, `expiry`? | read-only; network |
| `results_analyse` | **Quarterly results.** Quarterly results from the company's XBRL filings with NSE: revenue, profit, margins, EPS, QoQ and YoY changes, plain-words notes and the next board meeting. | `symbols`, `quarters`?, `standalone`? | read-only; network |
| `insider_deals` | **Bulk and block deals.** NSE bulk and block deals: who bought or sold how much, tagged when you hold or watch the stock or the client is on your watch list. | `symbol`?, `client`?, `min_value_cr`?, `side`?, `kind`?, `days`?, `mine`?, `limit`? | read-only; network |
| `insider_disclosures` | **Insider trading disclosures.** SEBI PIT insider trading disclosures filed with NSE: promoters, directors and employees buying or selling, net by stock. | `symbol`?, `days`?, `promoters_only`?, `market_only`?, `mine`?, `max_filings`? | read-only; network |
| `config_get` | **Your settings.** trader/config.json, the effective settings with defaults filled in, validation errors and warnings, and a plain-words summary. Read only. | `with_schema`? | read-only |
| `lab_list` | **Strategy lab: library and runs.** The Strategy Lab's built-in specs (strategies to test, not promises) and your lab runs in trader/lab/ with their verdicts and out-of-sample numbers. Read only. | none | read-only |
| `lab_run` | **Strategy lab: run a backtest.** Backtest a strategy spec bar by bar on NSE bars after Indian charges and slippage, with no look-ahead (an options spec, kind options: day by day on the local archive's end-of-day F&O chains, with margin, return on margin and tail losses); then walk-forward out of sample, the parameter neighbourhood, Monte Carlo, the deflated Sharpe ratio, the probability of backtest overfitting, regimes and 1.5x/2x costs. Pre-registered gates give REJECT, WATCH (paper forward test next) or PROMOTE. Writes trader/lab/<name>/. Sends progress and stops at max_seconds. Each run is a trial: the deflated Sharpe is charged for every other spec of the same family run from this server (trader/state/lab-trials.jsonl). Expect most strategies to be rejected. | `strategy`, `symbols`?, `seed`?, `max_seconds`? | writes trader/ (replaces a file, keeps a .bak); network |
| `lab_report` | **Strategy lab: scorecard.** The plain-words scorecard of a lab run: the verdict, every gate, the robustness checks and what the run does NOT prove. Read only. | `strategy`, `include_trades`? | read-only |
| `discover_strategies` | **Strategy discovery: search with overfitting controls.** Search trader-lab's rule vocabulary for an edge with a FIXED trial budget declared up front: train / validation / untouched holdout split by time (purged, embargoed), every trial logged and counted, then cost stress, parameter neighbourhood, regime consistency, the deflated Sharpe with the true trial count, overfitting odds over the trial matrix, a Reality Check / SPA bootstrap and a false-discovery cut; the holdout is opened once per finalist. Finalists become trader-lab specs (WATCH at best). Sends progress, stops at max_seconds and on cancel, and resumes when called again with the same name. Writes trader/discover/<name>/. Expect zero finalists. | `universe`?, `timeframe`?, `budget`?, `seed`?, `search`?, `name`?, `max_seconds`? | writes trader/; network |
| `discover_report` | **Strategy discovery: report.** Without a name: every discovery run in trader/discover/ with its state and funnel. With a name: the plain-words report (N trials, M survived validation, K survived holdout and multiple testing), the whole-search tests, a scorecard per validation survivor, the finalists' lab verdicts and what the run does NOT show. Read only. | `name`? | read-only |
| `lab_forward` | **Strategy lab: paper forward test.** Run a WATCH or PROMOTE strategy on the latest bars: settle its signals since the backtest into the rolling forward record (a decayed PROMOTE is demoted) and list today's fresh signals as draft plans. draft: SYMBOL makes that plan trader/plan.json (keeps a .bak) and runs paper_order_draft (trader-budget sizes it, trader-risk checks it). For an options spec, draft: UNDERLYING makes today's basket trader/strategy.json and trader/orders.json and runs trader-risk on it as one basket. Places nothing: a paper order still needs the user's own yes through paper_order_confirm. | `strategy`, `draft`?, `persona`? | writes trader/ (replaces a file, keeps a .bak); network |
| `strategy_build` | **Build an option strategy.** An option strategy from a template on the real NSE chain (spreads, straddles, strangles, iron condor, iron fly, butterflies, ratios, covered call, protective put, calendar): payoff at expiry and today, breakevens, max profit and loss (unlimited flagged), net premium and Greeks, the same after 2026 F&O charges with the STT-on-exercise warning, margin required (what a broker would block: a SPAN-style estimate with its error; the premium for bought options only) next to the exact max loss and the return on margin, probability of profit textbook vs simulated with expected value and 5% tail loss. Read only: it proposes nothing. | `symbol`, `expiry`?, `fill`?, `at`?, `iv_shift`?, `seed`?, `paths`?, `simulate`?, `include_curve`?, `template`, `far_expiry`?, `strike`?, `wings`?, `width`?, `delta`?, `skip`?, `ratio`?, `lots`?, `type`? | read-only; network |
| `strategy_price` | **Price custom option legs.** Your own option legs (up to 12, strikes by number, offset from ATM, delta or premium, other expiries allowed) with every number strategy_build gives. Read only: it proposes nothing. | `symbol`, `expiry`?, `fill`?, `at`?, `iv_shift`?, `seed`?, `paths`?, `simulate`?, `include_curve`?, `legs` | read-only; network |
| `broker_status` | **Broker connection.** The live broker connection, read only and without network: the config's broker and mode, for Upstox, Zerodha Kite and Dhan which BT_* environment variables are set (true/false, never the values), what each API can do, the plan notes and the last static-IP preflight. Places nothing. | none | read-only |
| `broker_holdings` | **Broker holdings.** Delivery holdings read from the live broker's API (Upstox, Zerodha Kite or Dhan): symbol, qty, average, last price, P&L. Read only; needs the user's own token in the server's environment. | `broker`? | read-only; network |
| `broker_positions` | **Broker positions.** Today's positions read from the live broker's API: symbol, exchange, product, qty, average, P&L. Read only; needs the user's own token in the server's environment. | `broker`? | read-only; network |
| `doctor` | **Data sources self-test.** Probe every free data source the tools read (NSE equity list, market status, holidays, announcements, board meetings, results, bulk/block deals, PIT, option chain and contract info, lot sizes, F&O list, India VIX history; Yahoo chart, spark and headlines), one polite request each (about 30 s): ok, slow, changed, down or blocked, since when, the tools affected and the fallback in use. Freshness follows the NSE calendar in IST. Read only. | `only`? | read-only; network |
| `persona_scorecards` | **Agent persona scorecards.** Each agent persona's paper track record (agents/), rebuilt from trader/state/trades.json: closed trades, wins and losses, net P&L after every charge, profit factor, expectancy, max drawdown, losing streaks, entries per day, yes and no counts, and behaviour flags (overtrading, revenge trades, exits after flat_by, fills with no ticket). Fills are credited through the persona tag on the drafted proposal. Read only. | `persona`? | read-only |
| `regime_now` | **Market regime now.** The market regime in plain words for NIFTY, BANKNIFTY or NSE stocks (trader-regime): trending up or down, ranging or no clear trend (ADX 14, efficiency ratio, 20/50 EMA slope, 200-day average), the volatility bucket (India VIX percentile over a year, realised vol, IV rank), the gap, breadth, weekly or monthly expiry today or next session, results day, RBI policy and Budget days, holidays, the closing-auction window and the time-of-day bucket. Each day uses data up to its own close only. From the local archive when present, else Yahoo daily bars. A description of the market so far, not a forecast. Read only (board meetings are cached a day in trader/regime/board/). | `symbols`?, `iv`? | read-only; network |
| `cotrader_candidates` | **Co-trader: today's candidates.** Today's trade candidates from strategies trader-lab validated (tier PROMOTED: gates plus a paper forward record; WATCH: gates passed, paper only), or the plain decision to sit out with the reasons and what would change them. Each candidate: the plan (entry, stop, target, size by trader-budget, charges), trader-risk's PASS or BLOCK with reasons, the share of past trades in today's market regime that made money after charges with its 95% range and n (shrunk toward the rule's overall rate), the expected R after charges, a time-machine replay as a second opinion and any disagreement, why, what would invalidate it, what the evidence does NOT show and similar past trades. include_research adds RESEARCH ideas (unproven, for study/paper only). Writes trader/cotrader.json and logs each prediction once for calibration; drafts and places nothing (paper_order_draft with candidate: ID drafts one). | `include_research`?, `strategy`?, `refresh`? | writes trader/; network |
| `cotrader_explain` | **Co-trader: explain a candidate.** One candidate from the last co-trader run in full: why this trade, what would invalidate it, the evidence in numbers (sample sizes, Wilson and posterior 95% ranges, expected R after charges), the time-machine second opinion and disagreements, what the evidence does NOT show, the nearest similar past trades and the risk verdict, plus the plain-words text. Without an id: the last run's candidate IDs. Read only. | `id`? | read-only |
| `calibration_report` | **Co-trader: calibration report.** How well the co-trader's past probabilities matched what happened: each logged prediction's outcome (paper fills, then the lab's forward record, then the bars after the signal), the Brier score with its base-rate reference, log loss and reliability bins with Wilson ranges, overall, per strategy and per tier, and which strategies are overconfident enough to demote. Read only: it never demotes. | none | read-only; network |
| `journal_summary` | **Trade journal summary.** The user's trade journal (trader-journal), rebuilt from paper fills (trader/state/trades.json) and imported broker trade books: closed trades after charges, expectancy with its 95% range, win rate with its Wilson interval, profit factor, drawdown, streaks, this week's numbers, the rule-break log (risk overrides, trades after a circuit-breaker lock, revenge trades, oversizing, entries outside the window, late exits, fills with no ticket), the overtrading index, the cost drag, the counterfactual ledger's totals and the latest trades. Read only. | `recent`? | read-only |
| `journal_stats` | **Trade journal statistics.** Statistics of the user's closed trades after charges (trader-journal): expectancy with its range, win rate with its Wilson 95% interval, profit factor, average win and loss, max drawdown and streaks, optionally broken down by strategy, persona, regime (NIFTY's trend and volatility the close before), time of day, weekday or instrument, for all trades or one ISO week; with the behaviour flags. Read only. | `by`?, `week`? | read-only |
| `counterfactual_report` | **Counterfactual ledger.** What the trades the user did not take would have done (trader-journal): every co-trader candidate skipped or blocked by risk (trader/cotrader/log.jsonl) and every trader-risk BLOCK of the user's own order, replayed on the bars after it with trader-lab's no-look-ahead fill rules (next bar's open plus slippage, stop first, flat-by or maximum holding, net of charges): per group the plans, how many were replayed, taken anyway or unresolved, what the losers would have lost (saved) and the winners made (cost), with the caveats. Read only; bars from the local archive or the lab cache, never fetched. | `group`?, `since`?, `until`?, `limit`? | read-only |
| `archive_status` | **Market data archive status.** What the local NSE archive (trader-archive) holds: each dataset (equity and F&O bhavcopies, indices, India VIX, ban list, settlement prices, lot sizes, corporate actions) with its files, rows, first and last day and when it was fetched, intraday bars and option-chain snapshots, in plain words. The lab's options backtests and the regime's IV ranks read it. Read only, no network. | none | read-only |
| `strategy_design` | **Design a strategy (a lab spec draft).** Turn a structured intent (instrument, timeframe or time window such as the 09:15-10:00 open drive or expiry day, regime conditions, risk budget, style = a library template) into a trader-lab spec draft in the lab's fixed, safe vocabulary (fields, session values, indicators, comparisons; no code), with every validation error, the strategy family and how many trials that family already has. Never runs anything: lab_run_spec tests the draft. | `template`, `name`?, `symbols`?, `window_from`?, `window_to`?, `days_to_expiry`?, `weekdays`?, `vix_min`?, `vix_max`?, `iv_rank_min`?, `iv_rank_max`?, `trend`?, `regime_gate`?, `capital`?, `risk_per_trade_pct`?, `max_trades_per_day`?, `lots`?, `target_r`?, `stop_atr_mult`?, `profit_target_pct`?, `stop_loss_mult`?, `params`?, `extra_entry`?, `note`? | read-only |
| `lab_run_spec` | **Strategy lab: run your own spec.** Run a spec the user or the assistant wrote (JSON text, e.g. from strategy_design) through the same lab as lab_run: after-charges backtest, walk-forward, deflated Sharpe, overfitting odds, gates and a verdict. Trial-count honesty: every spec run here is logged in trader/state/lab-trials.jsonl and increments the multiple-testing count of its strategy family, so later variants face a higher bar; the result says how many trials were counted. Writes trader/lab/<name>/. Sends progress, stops at max_seconds. Places nothing. | `spec`, `seed`?, `max_seconds`? | writes trader/ (replaces a file, keeps a .bak); network |
| `compare_strategies` | **Strategy lab: compare runs.** Two to eight lab runs side by side: verdict, out-of-sample trades, win rate with its Wilson 95% range, expectancy in R after charges with its 95% range, profit factor, drawdown, deflated Sharpe and the trials it was charged for, overfitting odds, 1.5x costs, gates passed, the strategy family, and the trades in today's regime cell; ranked by verdict then the lower end of the expectancy range, with what each does NOT prove. Read only. | `strategies` | read-only; network |
| `best_for_regime` | **Best strategies for today's regime.** Every lab run's past trades filtered to today's regime cell (NIFTY trending or ranging x the India VIX bucket, as of the close before): trades and wins in the cell with the Wilson 95% range, the shrunk probability and expected R after charges with their 95% ranges, n overall, whether the cell has enough trades to judge, the tier (PROMOTED, WATCH, RESEARCH) and the cells where it had an edge. Ranked conservatively; says plainly when nothing fits. Read only. | none | read-only; network |
| `trade_brief` | **Co-trader: one-shot trade brief.** One read-only call for a co-trader brief: the regime now, today's validated candidates with their odds, 95% ranges and n (worked out fresh, not written or logged), the no-edge reasons when there are none, the risk locks, the open paper positions and today's limits used, and the calibration of past odds. Proposing is not placing: a candidate becomes a paper ticket only through paper_order_draft, trader-risk and the user's own typed yes. | none | read-only; network |
| `autopilot_status` | **Auto-pilot: status.** The auto-pilot on PAPER: whether its loop is running (pid, last step), today's phase, the timeline of today's actions (pre-open brief, orders placed with trader-risk PASS and the machine yes labelled autopilot, fills, exits, blocks with reasons, kill switch, end of day), the open paper positions by strategy, each strategy's capital bucket, its paper forward record after charges next to trader-lab's verdict, today's report and past replays. Read only. | none | read-only |
| `autopilot_replay` | **Auto-pilot: replay a past day.** Replay one or more past NSE sessions with the auto-pilot on a simulated clock, in a sandbox (trader/autopilot/replays/): the enabled strategies' lab runs are re-run as of the close before the first day (no look-ahead), a fresh paper account starts from your capital, and every step runs from the pre-open to the end-of-day report, with exact charges. Read only for your account: it never touches your paper account, orders or lab runs. | `date`, `to`? | read-only |
| `autopilot_start` | **Auto-pilot: start on paper.** Start the auto-pilot loop on the PAPER account, with the user's typed confirmation (the words start paper). A config that is not paper is refused before anything. When the client supports elicitation the server asks the user directly; otherwise typed must be the user's own words. Never type them yourself. There is no live auto-pilot. | `typed`? | writes trader/ |
| `autopilot_stop` | **Auto-pilot: stop.** Ask the running auto-pilot loop to finish its current step and stop, with the user's typed confirmation (the words stop paper). Open paper positions stay as they are. | `typed`? | writes trader/ |
<!-- tools:end -->

`?` marks an optional argument. Every tool also carries MCP annotations:
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`
("network": it can reach Yahoo or NSE when not offline).

`strategy_build` and `strategy_price` are read only: they price a position
and propose nothing. Their `margin.required` is an estimate of what a broker
would block (SPAN-style, with `error_pct`); `margin.max_loss` is the exact
max loss at expiry and is not the margin. To paper-trade a strategy the user writes a proposal
with `strategy.py ... --orders` or the local UI's "Send to paper", then the
same flow below: `risk_check` (with `"allow_fno": true` it checks every leg
of the basket as ONE position: lots, the ban list, premium, margin, Greeks
and the scenario grid), and `paper_order_confirm` for each leg with the
user's own typed yes. `risk_scenarios` shows the F&O book's Greeks and its
scenario grid, open positions alone or with the proposals, read only; the
kill switch is lifted only by the user (the typed word `unkill`). `doctor` runs trader-doctor's self-test read only:
it reads `trader/doctor.json` for "since" but does not update it (the
command line and the UI do).

### The paper order flow

1. `exit_plan` with `save: true` writes `trader/plan.json` (or use the one you
   have).
2. `paper_order_draft` sizes it into `trader/orders.json` and records
   trader-risk's verdicts. Each ticket comes back with its `fingerprint`.
3. The assistant shows you the READY ticket in plain words.
4. `paper_order_confirm` with that ticket's `index` and `risk_fingerprint`:
   - if your client supports **elicitation**, the server asks *you* directly
     (a form with one text box) and ignores any `yes_text` the assistant sent;
   - otherwise `yes_text` must be your own reply, word for word. The paper
     broker's own check (`parse_yes`) decides: only a reply that starts with
     "yes", names no other stock, has no condition in it, and whose numbers
     match this order counts.

The broker records the reply word for word in `trader/state/broker.json`. A
yes covers one ticket; a second confirm of the same order is refused.

### The strategy lab

`lab_list` shows the built-in specs and your runs. `lab_run` backtests one
(library name, or your own `trader/lab/<name>/spec.json`) after charges, with
walk-forward, the deflated Sharpe ratio, the probability of backtest
overfitting and the pre-registered gates, and writes `trader/lab/<name>/`. It
sends progress notifications (one per grid point) when the request carries a
`progressToken`, honours cancellation, and stops at `max_seconds` (default
180). `lab_report` returns the plain-words scorecard, including what the run
does NOT prove. `lab_forward` is the paper forward test for a WATCH or
PROMOTE strategy: it settles the forward record and lists today's signals as
draft plans; `draft: SYMBOL` makes one of them `trader/plan.json` and runs the
normal `paper_order_draft` flow above, so sizing, the risk check and your
typed yes are unchanged. Offline, `lab_run` sees the saved bars up to
2026-10-23 16:00 and `lab_forward` up to 2026-10-29 09:50. See
[lab.md](lab.md).

`cotrader_candidates` asks the co-trader for today's candidates: tiered
(PROMOTED, WATCH; RESEARCH only with `include_research`), each with its plan,
size, charges, trader-risk verdict, the probability with its 95% range and
sample size, the expected R after charges, a time-machine second opinion and
what the evidence does NOT show, or the plain decision to sit out with the
reasons. It writes `trader/cotrader.json` and logs each prediction once for
calibration; it drafts nothing. `cotrader_explain` returns one candidate in
full, and `calibration_report` scores the logged probabilities (Brier score,
log loss, reliability), read only. To paper trade a candidate,
`paper_order_draft` takes `candidate: ID`: its plan becomes
`trader/plan.json` and the normal flow follows, ending in the user's own
typed yes. See [cotrader.md](cotrader.md).

### The co-trader workflow

These tools turn the assistant into an evidence-first co-trader. Each one
returns sample sizes and 95% intervals with every rate.

- `trade_brief` is the one-call morning look, and it is read only. It
  returns the regime, today's validated candidates (worked out fresh, not
  written or logged) with p, its 95% range, n in this regime and n overall,
  and the expected R after charges. When there are no candidates it gives
  the no-edge reasons instead. It also returns the risk locks, the open
  paper positions with today's limits used, and the calibration of past
  odds.
- `best_for_regime` takes every lab run's past trades in today's regime
  cell: NIFTY trending or ranging, crossed with the India VIX bucket, as of
  the close before. For each run it reports the trades and wins in the cell
  with a Wilson 95% range, the shrunk p and expected R with their ranges,
  and whether the cell has enough trades to judge. Ranking is conservative:
  fits the cell first, then tier, then the lower end of the expected R's
  range. "Nothing fits" is an answer it gives.
- `strategy_design` turns a structured intent into a lab spec draft:
  - a library template (the style)
  - symbols
  - a clock window (`window_from`/`window_to`, 5-minute equity templates)
  - expiry day (`days_to_expiry: 0`, options templates)
  - VIX or IV-rank bounds
  - an uptrend filter
  - the lab's regime gate
  - a risk budget
  - one extra entry condition in the lab vocabulary

  It returns the spec, every validation error, the strategy family and that
  family's trial count so far. It never runs anything. The vocabulary is
  fixed data: fields, session values, nine indicators, six comparisons and
  `all`/`any`/`not`/`time_between`. An unknown name such as `eval` or
  `__import__(...)` is a validation error, not code. When a request does not
  fit the vocabulary (a VIX filter on an equity spec, a clock window on a
  daily one), the error says so plainly.
- `lab_run_spec` runs a spec the user or the assistant wrote, through the
  same lab as `lab_run`. It reports progress and stops at its time budget.
- `compare_strategies` puts 2 to 8 runs side by side. It shows the
  out-of-sample win rate with its Wilson range, the expectancy with its 95%
  range, the trials each run was charged for, the families, and the trades
  in today's regime cell.
- `archive_status` says what the local NSE archive holds. The options
  backtests and IV ranks depend on it.

**Trial-count honesty.** Every run through `lab_run` or `lab_run_spec`
writes one line to `trader/state/lab-trials.jsonl`: the spec hash, its grid
points and its *family*. The family is the spec's structure with every
number, symbol and name removed: the fields and indicators used, the stop
and target kinds. When a later spec of the same family runs, its deflated
Sharpe is charged for the other specs' grid points too, so the bar rises
with every variant tried. Renaming a spec or changing a number does not
reset the count. Re-running the identical spec does not count it twice.
The result's `trials` block says how many trials were counted, and
`lab_run_spec` is annotated `idempotentHint: false`.

**Proposing is not placing.** None of these tools drafts or places an order.
A candidate becomes a paper ticket only through `paper_order_draft`, then
trader-risk, then the user's own typed yes in `paper_order_confirm` (or in
the UI).

**Auto-pilot hook.** The paper auto-pilot (#45) has not merged at the time
of writing, so no `autopilot_*` tool exists yet. When it lands, its
paper-only status, start and stop tools join this table under the same
rules: paper only, the kill switch wins, and every order goes through
trader-risk. The place for them is marked in `mcp/toolkit.py`.

The local UI's optional **Ask** page uses the same tool table: the
read-only tools and `paper_order_draft`, never a confirm. See
[ai-chat.md](ai-chat.md).

## Resources and prompts

Resources are the JSON files of your `trader/` folder, read only:
`bharat-trader://trader/plan.json`, `.../watchlist.json`,
`.../portfolio.json`, `.../options.json`, `.../strategy.json`,
`.../journal.json` (whichever exist), and `bharat-trader://trader/state/risk.json`,
`.../broker.json`, `.../trades.json`, the daily market regimes that
`trader-regime history` stores: `bharat-trader://trader/regime/NIFTY.json`,
and trader-journal's weekly reviews and notes:
`bharat-trader://trader/journal/weekly-2026-W44.md` (markdown) and
`.../journal/notes.json`. Four templates cover the rest:
`bharat-trader://trader/{file}`, `bharat-trader://trader/state/{file}`,
`bharat-trader://trader/regime/{file}` and `bharat-trader://trader/journal/{file}`.
Names are checked (`*.json`, or `weekly-YYYY-Www.md` under `journal/`, no
`..`, no subfolders beyond `state/`, `regime/` and `journal/`) and the
resolved path must stay inside `trader/`.

`journal_summary`, `journal_stats` and `counterfactual_report` read the trade
journal (trader-journal): closed trades after charges with their ranges, the
rule-break log, and what the skipped and blocked trades would have done. They
never write; bars come from the local archive or the lab cache, never the
network. See [journal.md](journal.md).

`regime_now` is the regime read for today (trader-regime `now --json`):
trend, volatility bucket, gap, expiry and event flags and the time-of-day
bucket, with every input. See [regime.md](regime.md).

| prompt | arguments | asks the assistant to |
| --- | --- | --- |
| `morning_briefing` | `focus`? | market_status, news, scan_daily, portfolio_status, then a pre-market summary |
| `intraday_plan` | `symbol` | entry_check, exit_plan, timemachine_run, size_trade; draft and confirm only on your typed yes |
| `options_income_scan` | `symbol`, `expiry`? | options_summary and options_chain; explain positioning and a seller's risks, no trade |
| `weekly_review` | none | journal_stats, journal_summary and counterfactual_report, then portfolio, tickets, risk and config; one process change |
| `research_strategy` | `idea`, `symbol`? | lab_list, lab_run and lab_report on the closest spec (gates, out of sample, what it does not prove); a forward test only for WATCH or PROMOTE; say what would kill the idea |
| `daily_cotrader_brief` | `focus`? | trade_brief and best_for_regime: the regime, each candidate's odds with its 95% interval and n, or the no-edge decision; open positions, limits used, calibration |
| `find_best_trade_now` | `universe`? | regime_now, best_for_regime, cotrader_candidates, cotrader_explain; rank by the lower end of the interval; "no edge today" is an answer |
| `design_strategy_for_window` | `window`, `idea`?, `symbol`? | strategy_design for a window (the 09:15-10:00 open drive, expiry day), pre-register the test, then one lab_run_spec and the verdict with the trials counted |
| `stress_test_my_idea` | `idea` | try to break it: costs, the parameter neighbourhood, out-of-sample n, deflated Sharpe with the family's trials, regime cells; the strongest argument against |
| `review_my_week` | `week`? | journal_stats, journal_summary, counterfactual_report, calibration_report, persona_scorecards; process over outcome under 20 trades |
| `persona_intraday_trader`, `persona_options_seller`, `persona_swing_trader`, `persona_researcher`, `persona_risk_officer`, `persona_journal_coach` | `focus`? | act as that persona from `agents/`: its routine, limits and report format, plus the current context from `trader/` (session, limits, lab verdicts, its paper scorecard). See [agents.md](agents.md) |

Every prompt tells the assistant to use the tools for every number, show
evidence and risks, never promise a result, and say it is not financial
advice. The five co-trader prompts add the evidence rules. Show n and the
95% interval with every rate. Give numbers after charges. Name the regime.
Say what the evidence does NOT show. Make no promises. Remember that
proposing is not placing: every order goes through risk and the user's
typed yes. Every spec run counts as a trial for its family.

## Using your assistant as a co-trader

Connect the server, then start with a prompt such as `daily_cotrader_brief`,
or just ask. A good co-trader answer shows its evidence, is honest about how
little a small sample says, and is fine saying "no trade". Below are three
example conversations. The numbers are made up to show the shape of an honest
answer; yours come from your own `trader/` folder.

**"What's the best trade right now?"**

> The assistant calls `trade_brief`, then `best_for_regime`.
>
> *NIFTY is trending down (ADX 32) with normal volatility (India VIX 12.3,
> 50th percentile, as of yesterday's close). None of your lab strategies is
> validated for this cell. `t-sma20-watch` is a WATCH: 6 of its 11 past
> trades in this kind of market made money after charges. That is 55%, but
> the Wilson 95% range runs from 28% to 79%, so 11 trades cannot tell it
> from a coin flip. Shrunk toward its overall rate, it comes to 52% (95%
> 38% to 66%) with an expected +0.08R a trade after charges (95% −0.21R to
> +0.37R). That range includes losing money. The evidence does NOT show how
> it behaves in a high-volatility week or on gap days, and the fills are a
> model. Two candidates triggered. Both passed your risk check. I would
> treat them as paper-only research. Sitting out is also a reasonable
> decision today. Want me to draft one as a paper ticket? You would still
> confirm it yourself.*

**"Design an expiry-day NIFTY strategy and test it."**

> The assistant calls `regime_now`, then `strategy_design` with `template:
> nifty-expiry-iron-fly`, `days_to_expiry: 0`, `vix_max: 18` and
> `profit_target_pct: 50`. It shows the spec. It notes that the family
> already has 4 trials, and it pre-registers the gate that would kill the
> idea: out-of-sample expectancy below zero after 1.5x costs. Then it calls
> `lab_run_spec` once.
>
> *REJECT. 41 out-of-sample expiry days, and the expectancy after charges
> is −₹212 a lot, with a 95% range of −₹780 to +₹356. The deflated Sharpe
> counted 8 trials (this grid's 4 and the family's earlier 4) and came to
> 0.31, far below the 0.95 gate. The win rate looks high (68%) because a
> sold iron fly wins small and loses big. This does NOT tell us about
> expiries with a gap at the open, and the archive only covers 2024 onward.
> I won't keep tweaking the wings until something passes. That would just
> be fitting noise, and every try raises the bar anyway.*

**"Stress-test my opening-range idea."**

> The assistant calls `lab_report`, then `compare_strategies` against
> `orb15-vwap`, then `best_for_regime`.
>
> *It survives 1.5x costs but not 2x. Only 3 of the 9 grid points around
> your settings make money, so this looks like one lucky point rather than
> a plateau. There are 23 out-of-sample trades, and the expectancy's range
> (−0.31R to +0.44R) spans zero. All of the edge comes from the trending,
> low-volatility cell, with 9 trades. The strongest argument against: with
> this few trades in one regime, the result is what luck alone produces
> about one time in four. I would drop it unless the next 20 paper forward
> trades average above 0R.*

What a co-trader will not do here: place or confirm any order, call
anything guaranteed, sure-shot or risk-free, or run variant after variant
looking for a pass. A paper ticket always ends with your own typed yes.

## Good questions to ask

- "Is NSE open? What's the next session?"
- "Morning briefing please, focus on banks."
- "Which stocks broke their opening range today, and how liquid are they?"
- "Has RELIANCE triggered? If so, plan the exit with an opening-range-mid stop
  and run the time machine on it."
- "How many shares does my plan allow, and what do I lose at the stop after
  charges?"
- "Show me NIFTY's PCR, max pain, walls and expected move for this expiry, and
  what an option seller would be exposed to."
- "How did TCS's last five quarters look? Anything unusual in the notes?"
- "Any bulk or block deals or promoter buying in stocks I hold?"
- "Review my week: what did risk block, and which rule did I nearly break?"
- "Does the 15-minute opening-range breakout survive charges out of sample?
  Run it in the lab and tell me which gates it failed."

- "Give me today's co-trader brief: regime, candidates with their odds and n,
  and how well past odds were calibrated."
- "Which of my lab strategies has an edge in today's regime? Rank them by the
  worst case of the range."
- "Design an open-drive strategy for 09:15 to 10:00 on SBIN and TCS and test
  it once. How many trials does that family have now?"

Questions that will get a careful no: "which stock will go up?", "place a real
order", "just buy it for me".

## Security model

- **No live orders, at all.** There is no order tool for a live broker. The
  three broker tools (`broker_status`, `broker_holdings`, `broker_positions`)
  only read: they call trader-broker's adapters for holdings and positions
  with the token the user put in the server's environment, and never place,
  change or cancel anything. `paper_order_confirm` refuses a live config
  (`"paper": false`) before doing anything (trader-broker's `paper_only`); a
  live order goes out only with `ticket.py place` in the user's own terminal
  (see [brokers](brokers.md)).
- **One order write, with two locks.** `paper_order_confirm` needs trader-risk
  PASS recorded today for that exact order (the fingerprint is a hash of the
  order's fields, so any change needs a new check) and the user's own typed
  yes, through elicitation when the client offers it.
- **Writes stay in `<workdir>/trader/`.** Every path a tool passes to a skill
  is under it, and the process runs with `<workdir>` as its current folder so
  a relative default lands there too. Files a tool replaces (`plan.json`,
  `orders.json`, `entry.json`) keep a `.bak`. Resources are read only.
- **Rate limited**: 30 tool calls a minute by default, on top of the skills'
  own pauses between NSE and Yahoo requests.
- **stdout is only JSON-RPC.** The server keeps a private copy of stdout for
  the protocol and points file descriptor 1 at stderr, so a stray print
  anywhere goes to the log, never into a message.
- **HTTP is loopback only** (`127.0.0.1`), checks `Host` (DNS rebinding) and
  `Origin` (403 for anything not loopback), takes one JSON body of at most
  1 MB per POST, and sends no CORS headers. There is no login: anyone who can
  run a program on your machine can call it, so prefer stdio.
- **Elicitation state is signed.** The modern multi round-trip confirm carries
  a `requestState` signed with HMAC-SHA256 under a per-process key, tied to
  the tool, its arguments and the ticket, valid for 15 minutes, and usable
  once. A tampered, expired, replayed or mismatched state is rejected.
- **Offline really is offline**: `--offline` disables outbound sockets for the
  whole process.

## Protocol support

The server implements MCP revision **2026-07-28** (the current revision as of
November 2026) and serves clients of the earlier, handshake-based revisions
**2025-11-25**, **2025-06-18** and **2025-03-26** too ("dual-era").

| | 2026-07-28 (modern) | 2025-11-25 and earlier (legacy) |
| --- | --- | --- |
| opening | none; every request carries `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` in `_meta`; `server/discover` | `initialize` (version negotiated: the client's if supported, else 2025-11-25), `notifications/initialized` |
| wrong version | `-32022 UnsupportedProtocolVersion` with `supported` and `requested` | the server answers with the version it speaks |
| results | `resultType`, `_meta` serverInfo; `ttlMs` and `cacheScope: "private"` on lists and reads | as the revision defines |
| elicitation | multi round-trip: `resultType: "input_required"` with `inputRequests` and a signed `requestState`, answered by a retry carrying `inputResponses` | `elicitation/create` sent to the client over stdio (form mode) |
| logging | per request, `io.modelcontextprotocol/logLevel` in `_meta` | `logging/setLevel`, `notifications/message` |
| ping | removed (`-32601`) | `ping` |
| resource not found | `-32602` | `-32002` |
| HTTP | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers must match the body (`400`, `-32020`); unknown method `404` | `Mcp-Session-Id` sessions, ended with `DELETE` |

Both eras: JSON-RPC 2.0, one message per line on stdio, no batches;
`tools/list` (deterministic order) and `tools/call` with `inputSchema`,
`outputSchema`, `structuredContent` plus text, `isError`, titles and
annotations; `resources/list`, `resources/templates/list`, `resources/read`;
`prompts/list`, `prompts/get`; opaque pagination cursors (`-32602` for a bad
one); `notifications/cancelled` (no answer is sent for a cancelled request);
`notifications/progress` when the request carries a `progressToken`. Closing
stdin shuts the server down.

Not implemented: `subscriptions/listen` and list-changed notifications (the
lists do not change while the server runs), completions, sampling, roots, the
tasks extension, and SSE responses over HTTP (answers are single JSON
objects, so progress and legacy elicitation are stdio only; over HTTP the
confirm tool falls back to `yes_text`).

## Tests

`tests/test_mcp.py` spawns the server as a subprocess and talks to it over
pipes and over HTTP, offline: the handshake and version negotiation in both
eras, `tools/list` schema validity, every tool on the saved fixtures (each
result checked against its `outputSchema`), resources, prompts, the error
codes, cancellation, progress, the rate limit, the confirm flow (elicitation
in both eras, the typed-reply fallback, a tampered or replayed state, a stale
risk check, a non-paper config), that writes stay in `trader/`, and that
stdout carries only JSON-RPC.

*Education and tooling, not financial advice.*
