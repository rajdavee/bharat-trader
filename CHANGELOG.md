# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-11-03

The edge-hunt release: a long-history archive, lab engines for futures,
portfolios, events, pairs and options, and 14 hypotheses registered before
any run and then run exactly as written, every trial counted. The answer is
published as it came out: **no pre-registered idea showed an edge
distinguishable from luck after Indian 2026 charges**, 0 survivors where the
priors expected about 1.5, and the Benjamini-Hochberg cut keeps 0. The tools
said no, which is their job. See
[docs/research/edge-hunt-results.md](docs/research/edge-hunt-results.md).

### Added
- **Edge-hunt research brief and pre-registration** (#70):
  `docs/research/edge-hunt.md` reviews the evidence for cross-sectional
  momentum, low volatility, index trend, turn of the month, pre-holiday and
  expiry week, post-earnings drift, the NIFTY volatility risk premium,
  results-day straddles, short-term reversal, pairs, intraday market
  momentum, overnight drift and trend-day option buying, with SEBI's F&O
  studies and the 2026 charge drag of each idea computed with trader-budget's
  charges. 14 hypotheses are registered in `trader-lab/library/edge-hunt/`
  (184 grid points), each with fixed parameters, a trial budget, train,
  validation and sealed holdout dates, gates, kill criteria and a prior;
  `manifest.json` freezes their registered content by SHA-256.
  `tests/test_edge_hunt.py` validates the schema, the date splits, the
  ranking, the hashes and the brief's cost table.
- **Long-history archive** (#71) in `trader-archive`: legacy equity
  bhavcopies back to 1994-11-03 and F&O back to 2000-06-12 (formats checked
  year by year; the old `OPTIONTYPE` header, files without ISIN, ban titles
  with full month names), index history before the daily index files start
  (2012-02-21) through the `indexhist` dataset, old index names (S&P CNX
  Nifty, CNX Bank ...) read as today's, India VIX from 2010-07-19 (NSE's
  first day), and 2010-style split subjects.
- **Continuous futures**: `load_continuous()` and `archive.py continuous` for
  NIFTY, BANKNIFTY or any F&O stock; roll N sessions before expiry, or on the
  OI or volume switch; unadjusted with a roll log, back-adjusted or
  ratio-adjusted.
- **Point-in-time index membership**: `members(index, date)` for NIFTY 50,
  Next 50, 100, 200 and 500, rebuilt from NSE Indices' change announcements
  (a standard-library PDF text reader in `trader-archive/history/`), today's
  lists and NSE's symbol changes, with a report of every gap.
- **Results dates**: `results_dates(symbol, ...)` from NSE's board meetings
  and results filings since 2010, with the minute each result came out and
  the first session that could react.
- **Expired-option candles** from Upstox (Plus plan) or Dhan (Data API) with
  your own token (`expired-options`), and `capture-chains`, a background
  loop that snapshots option chains through every session with a heartbeat
  file, for forward intraday option data.
- `verify` checks splits and bonuses across the whole span against the
  archive's own closes, and reports membership gaps, roll gaps and missing
  results months.
- **NSE sector classification** in `trader-archive`: `backfill --datasets
  sectors` stores NSE's own four levels (macro, sector, industry, basic
  industry, from the quote API's `secInfo`) for every F&O stock, today's list
  and every stock future in the archive, as dated snapshots, politely and
  resumable. `sectors(symbol, date=None)` and `archive.py sectors` read them
  offline. trader-lab's pairs engine reads them, so eh-11's dry run is no
  longer blocked on a hand-made `sectors.json` (which still wins when given).
- **Dated F&O lot-size history** in `trader-archive`: `backfill --datasets
  lothist` builds every F&O contract's lot on every day,
  `lothist/lothist.csv.gz` (underlying, expiry, effective_from, effective_to,
  lot, source, confidence). **Exact** from NSE's own numbers: the UDiFF
  bhavcopy's lot (2024 on), NSE's F&O market activity reports (traded
  quantity / contracts, March 2011 on, two sessions a month), the Wayback
  Machine's copies of `fo_mktlots.csv` (2008 to 2024), NSE's index lot
  circulars (2014 to 2025, built in), and segments bridged between two equal
  exact lots. **Reconstructed** otherwise, from the legacy bhavcopy's notional
  turnover / (contracts x price) and the open interest's divisibility,
  segment by segment (a bonus or split mid-life cuts it). Read API
  `lot_size(underlying, date, expiry)`, `lot_history`, `lot_coverage`,
  `fill_lots`; `load_fo_chain_eod(..., lots="exact"|"any"|"none")` fills a
  missing lot from it; `archive.py lots` prints the coverage by year and
  every lot change; `status` shows it.
- **Lab engine v2 and the edge-hunt executor** (#72): `lab.py hunt list |
  run EH-ID [--dry-run] | status` runs a pre-registered hypothesis exactly
  as its file says. The file must match its manifest hash; every grid point
  runs on train and validation (each window purged, the data read only up
  to the validation's last day) and counts as one trial; the deflated Sharpe
  is charged for the grid, the file's prior trials, the same family's other
  runs and any declared extra; the train-best finalist is screened by the
  lab's gates on validation (with PBO over the grid, 1.5x and 2x costs, the
  neighbourhood, walk-forward by year and a regime split); only a passing
  finalist opens the sealed holdout, logged in `holdout.json` before any
  holdout number exists, and never again. The verdict goes into the file's
  `status` and `outcome`, with a scorecard and the programme's
  Benjamini-Hochberg cut in `hunt status`. New engines in
  `trader-lab/engines/`: futures (continuous near-month series, lots by
  date, SPAN-style margin, the dated 2026 futures charges, roll costs,
  overnight or intraday holding, daily MTM), cross-sectional portfolios
  (point-in-time universe, 12-1 and 6-1 momentum, low volatility,
  short-term reversal, weekly to semi-annual rebalances, top-N or quantile,
  equal or inverse-volatility weights, CNC charges with a DP charge per
  sell, turnover, the equal-weighted universe and a NIFTY total-return
  proxy, a capacity note), events (turn of the month, pre-holiday, expiry
  week, overnight, results-date drift) and pairs (correlation and
  Engle-Granger on train only, z-score bands, futures legs), all with
  portfolio metrics. The data adapter reads the long-history archive
  (#71: `load_continuous` with its roll log, `members`, `results_dates`,
  index history and VIX), blocks a point-in-time or results-date run when
  that data is not there, and copies the archive's membership caveat into
  the scorecard. MCP: `edge_hunt_status`
  and `edge_hunt_run` (57 tools). The Lab page lists the hypotheses with
  their status and outcome. No real hunt was run (that is #74).
- **Options edge v2** (#73) in `trader-lab/options/`, wired into the
  executor (`OptionsJob`), so `lab.py hunt run` runs the VIX-timed NIFTY iron
  condor (eh-05), the results-day short iron fly (eh-08), the pre-results
  long straddle (eh-09) and trend-day option buying (eh-14) as registered.
  `vol.py`: close-to-close, Parkinson and Garman-Klass realised vol, a
  HAR-RV forecast fitted on the train window only, and the IV - RV spread
  (India VIX or the ATM IV of the EOD chain) with no look-ahead.
  `events.py`: implied move (ATM straddle over spot) against the realised
  move on the first reacting session from `results_dates`, the stock's own
  history of results moves, and an event calendar (results, RBI policy,
  Budget, expiry). `engines.py`: the VRP condor (one a monthly cycle, delta
  shorts, wings as % of spot, take-50% or expiry, sizing by margin and a 1%
  tail loss, with the untimed baseline and the event days held through), the
  results-day fly and straddle (traded strikes only, stock lots and dated
  charges, the straddle's P&L split into move and IV change), and trend-day
  buying (model-priced Black-76 on index bars for train and validation, the
  captured chain's bid and ask for the holdout). The executor scores return
  on margin, and waits instead of opening a holdout of live data that has
  not been captured yet (eh-14: the verdict stays capped at WATCH).
  `load_chains` and `trim_board` can keep less of each board for long
  histories. Tests on trimmed real NSE files (HDFCBANK and ICICIBANK around
  their 17 Oct 2026 results, NIFTY monthly chains, India VIX, NIFTY 5-minute
  bars). No real hunt was run (that is #74).
- trader-lab's option engines (eh-05 VRP condor, eh-08/eh-09 results-day
  options, eh-14) take a missing lot from the dated history and **refuse a
  reconstructed lot** unless `allow_reconstructed_lots` is true in
  `trader/config.json` (the skip and the counts are in the run's notes). The
  edge-hunt executor's lot check now measures the history's coverage of the
  train and validation windows (95% of contract-days needed), reports it in
  the plan and the dry run, and says what would unblock a run. The option
  families' engine revision moves to 3 (earlier trials are superseded).
- **Edge-hunt memory guard and honest resume**: `lab.py hunt run --max-mem-mb N`
  (default `lab_max_mem_mb` in `trader/config.json`, else 2048; 0 turns it
  off) checks the process's peak memory (`resource.getrusage`; bytes on
  macOS, kilobytes on Linux; no guard on Windows) while loading and after
  every train and validation run, and pauses the hunt cleanly (exit code 4)
  with every finished trial kept and the holdout sealed. Trial rows record
  their engine revision; rows of an older revision are refused on resume
  unless `--rerun-superseded` sets them aside (`trials-superseded.jsonl`,
  still charged to the trial count). A torn last line is skipped and that
  trial re-run; a grid point logged twice counts once. The holdout's data is
  read before its opening is logged.
- **Annotations and re-registration** for edge-hunt verdicts:
  `trader-lab/library/edge-hunt/annotations.json` (outside the hashed files)
  marks eh-08 event crush VOID (0 trades in every trial: the pre-July-2024
  stock boards had no spot, fixed in #88) while its REJECT stays on record.
  `eh-15-event-crush-r` re-registers the same test with eh-08's 8 trials in
  `prior_trials`. The brief's new rule 8 makes this the only way to rerun a
  decided idea. The executor reads the annotations: a re-registered run is
  left out of its successor's family trial count (counted once, in
  `prior_trials`) and a VOID run is left out of the Benjamini-Hochberg cut;
  `hunt list` and `hunt run` show the annotation.
- **Edge-hunt final results** (#74):
  `docs/research/edge-hunt-results.md` publishes every verdict. 13 of the 15
  registrations are decided, all REJECT before the holdout (every holdout
  still sealed, no WATCH, Benjamini-Hochberg keeps 0 of 14); eh-08 is VOID
  and was re-run as eh-15; eh-12 intraday momentum is blocked (no minute
  bars) and eh-14 trend-day buying waits for its live holdout (chains
  captured from 16 Nov 2026). The deflated Sharpe ratio failed for all of
  them (best 0.61). The last three, eh-05 VRP condor (4 of 10 gates), eh-09
  results run-up straddle (2 of 10) and eh-15 results-day iron fly (2 of
  10), lost on validation with 0% of their neighbouring grid points
  positive. The near misses (eh-03 index trend: 5 validation trades; eh-01
  momentum: net Sharpe 0.87, the only one to beat its benchmark, DSR 0.26)
  are recorded and not re-tested with tweaks. The page scores the brief's
  priors (about 1.4 survivors expected over the 12 scored, 0 found, Brier
  0.021: too optimistic), says what the results mean for a trader, and how
  to propose a new hypothesis. The executor's status and outcome are
  committed in each hypothesis file; the registered content is unchanged
  (hashes match).
- **Strategy portfolio** (#75): `lab.py portfolio build|report|list`, a
  **Portfolio** section on the Lab page and the `portfolio_report` MCP tool
  (read only). Combines WATCH and PROMOTE strategies (lab runs and edge-hunt
  finalists; `--research` studies others, never proposed) on their
  out-of-sample and paper forward daily returns after charges, over only the
  sessions they all share (the overlap is reported). Pearson, Spearman,
  downside, stress and weekly correlation, near-duplicate clusters (the same
  bet), four sizing schemes (equal capital, inverse volatility, risk parity,
  capped fractional Kelly on shrunk means) under a volatility target and
  per-strategy caps, each tested walk-forward with weights from earlier
  sessions only (CAGR, volatility, Sharpe, Sortino, max drawdown, worst
  month, time in drawdown, diversification ratio, risk shares) and
  block-bootstrap bands. Writes an `autopilot.strategies` bucket proposal
  and a trader-risk drawdown-limit proposal to
  `trader/lab/portfolio/<name>/`; nothing is applied. The edge-hunt executor
  now records the finalist's net daily returns per window (`returns.json`).
- **Forward-test dashboard** (#76): `lab.py forward-report`, the UI's
  **Forward tests** page (Review) and the `forward_report` MCP tool (read
  only). Each strategy's paper forward record against the range its
  backtest's out-of-sample trades predicted (mean R and win rate, prediction
  intervals, the record's own 95% ranges), inside / below / above, a lower
  CUSUM control chart (k 0.5, h 4), decay alerts (10+ trades and the running
  mean below the range for 5 in a row, or the chart alarms) with a DEMOTE
  recommendation applied only by `--demote NAME`, the co-trader's Brier
  score, the promote / demote timeline, and trades and sessions to PROMOTE.
  The auto-pilot adds it to its end-of-day report (`decay_demote`, off by
  default, applies the recommendation). Thresholds in `"forward_tests"`.
- **Intraday option-chain capture job**: the auto-pilot starts and
  supervises trader-archive's `capture-chains` in market hours (default
  NIFTY and BANKNIFTY, nearest two expiries, every 5 minutes): heartbeat
  staleness, restarts of its own capture only (at most 3 a day), gaps,
  strikes per snapshot and the 16 Nov 2026 holdout deadline of
  eh-14-trend-day-buying. `autopilot.py capture [--tick]`; launchd, systemd
  and Task Scheduler examples in docs/forward-tests.md.
- **Local notifications**, off by default (trader-alerts `notify/`,
  `alerts.py notify status|test|send`, the `notify_test` MCP tool and
  `POST /api/notify/test`): desktop pop-ups (macOS `osascript`, Linux
  `notify-send`, Windows toast) and an optional JSON POST to your own
  webhook from `BT_NOTIFY_WEBHOOK`, for co-trader candidates, auto-pilot
  fills and exits, co-pilot proposals waiting, risk locks and kill switches,
  decay alerts and capture failures. Per-event toggles, quiet hours, a
  per-event gap, a daily key and an hourly cap; the webhook gets only a short
  redacted summary (never secrets, positions or approval phrases), and the
  URL is never written anywhere. docs/notifications.md.
- MCP: 59 tools (`forward_report`, `notify_test`).
- **Running long hunts safely** (`docs/running-hunts.md`, #77): the RAM
  incident of 2 Nov 2026 and its lessons, the built-in `--max-mem-mb`
  guard, an external macOS watchdog (`docs/examples/memguard.sh`: kills a
  job past a memory limit or when the machine's free memory drops, logs a
  line a minute), one heavy job at a time, and the full archive's size and
  time (2003 to 2026, about 2.5 GB, about 20 hours at the polite pause).

### Changed
- README, the docs index and the roadmap carry the v0.4 result honestly (no
  edge found, 0 survivors, links to the results and the brief); the roadmap
  marks v0.4.0 done and lists v0.5 ideas (a minute-bar archive from your
  broker token to unblock intraday research, intraday options research on
  the captured chains, community-registered hypotheses under the same rules,
  mobile notification polish). docs/lab.md points the watchdog example at
  `docs/examples/memguard.sh`.
- `tests/test_edge_hunt.py` holds the decided files to the registration: the
  status and the outcome agree (REJECT for rejected, WATCH for watch, none
  while registered or running), the outcome carries this file's hash, a
  decision time after `registered_on` and a trial count that adds up, and any
  copy of a registered idea under a new id must be a declared
  re-registration of an annotated run. Tests that copy a real registration
  now start from it as first filed.

### Fixed
- **Edge-hunt engines are memory-bounded** (the real 2.1 GB archive made a
  16 GB Mac swap and freeze): option boards are packed (9 doubles a
  contract, about 7 times smaller, unpacked on use through a bounded LRU);
  per-day analyses are an LRU; results-day options stream the F&O files
  event by event and drop each board when no later event needs it; stock
  panels load 40 stocks at a time; the archive's equity reader keeps only
  the requested window of a fresh cache. Numbers are identical on every
  fixture (digests from before the change in
  `tests/fixtures/trader-lab/memory-golden.json`). Peak memory per family on
  the real archive is in docs/lab.md.
- **eh-05 stopped on a missing NIFTY spot**: 73 F&O sessions from 2014 to
  2023 have no index close in the archive (missing or unparsed index files,
  #87) and legacy F&O bhavcopies carry no underlying price. Such a board now
  takes a futures-implied spot (the nearest future, S = F e^(-rT)), named in
  the run's notes, or is left out with a logged reason when there is no
  future either; never a zero.
- **Results-day options (eh-08, eh-09) found no trades before July 2024**:
  legacy bhavcopies carry no underlying price, so every stock board had no
  spot and no ATM straddle. The spot now comes from the stock's equity close
  (else the near future). The IV-percentile history (eh-09) was kept per day
  and silently missing for every stock after the first on the same days; it
  is per stock and day now. Both bump the event-vol engine to revision 2.
  eh-08's recorded REJECT was decided on revision 1 (0 trades in train and
  validation) and is left as recorded.
- **Option results are the same on every Python**: the built-in `sum()` of
  floats became compensated in Python 3.12, so an iron condor's premium on a
  rounding boundary was 3313.76 on Python 3.8-3.11 and 3313.77 on 3.12+. The
  options package now sums floats with `math.fsum` (correctly rounded
  everywhere); its bounded cache no longer subclasses `OrderedDict` (before
  3.12 `pop()` called the overridden `__getitem__`).
- **Option hunts with no lot sizes for their train years now wait**: legacy
  bhavcopies carry no lot sizes and the archive's lot-size snapshots start in
  Nov 2026, so every eh-05/08/09 position before July 2024 would be skipped
  as "no lot size" and the run would record a REJECT about missing data. The
  executor reports a blocker instead. An eh-08 iron fly whose wings round
  under one point is skipped with a reason instead of stopping the run.
- **Margin model v2 for hedged and naked F&O positions** (#82): one SPAN +
  ELM block shared by trader-strategy, trader-risk and the paper broker, named
  in every output and in lab results and scorecards (`margin_model`,
  `nse-span-elm-v2 (2026-11)`). SPAN risk now has the net option value taken
  off (NSE's rule: a credit spread needs about its width, a debit spread only
  its debit) and a calendar charge across expiries; ELM is on the
  underlying's close with the 3% deep out-of-the-money and 5% long-dated
  index rates (5.25% for stocks); the expiring legs lose the calendar benefit
  on expiry day. Checked against NSE's SPAN file of 30-Oct-2026 (hedged NIFTY
  structures within 3%, naked at or above it) and published broker figures
  (HDFC Securities 2026, Zerodha 2020). Debit structures drop about 20% (the
  long wings' premium was added in full); credit condors move about 1%: ELM
  on every short leg, hedged or not, is NSE's rule and dominates their margin.

## [0.3.0] - 2026-11-01

The co-trader release: candidates only from validated rules, odds with their
range and sample size, "no trade today" as a first-class answer, and every
probability scored afterwards. On real data every built-in strategy, equity
and options, is REJECTED and a 500-trial discovery found 0 finalists. The
docs say so. The bridge to real money is the live co-pilot: PROMOTED
strategies only, one typed approval per order. See [docs/cotrader-guide.md](docs/cotrader-guide.md).

### Added
- **`trader-archive`** (#41): a local NSE data archive. Equity and F&O
  bhavcopies (UDiFF and legacy), index closes, India VIX, lot sizes, ban
  lists, settlement prices and corporate actions, with polite, resumable
  backfill and a checksummed manifest. Also broker intraday candles merged
  with Yahoo, option-chain snapshots, `verify`, and an offline read API
  (split- and bonus-adjusted). See docs/archive.md.
- **`trader-regime`** (#42): the market regime per day for NIFTY, BANKNIFTY
  or a stock: trend or range, the India VIX bucket, the gap, breadth, expiry
  and event days, the closing-auction window and the time-of-day bucket.
  No look-ahead (tested by changing the future). See docs/regime.md.
- **`trader-discover`** (#43): strategy discovery with a declared trial
  budget, a purged and embargoed train / validation / holdout split, and
  every trial counted. Survivors face the deflated Sharpe with the true N,
  overfitting odds over the trial matrix, a Reality Check / SPA bootstrap
  and a Benjamini-Hochberg cut. Finalists become trader-lab specs (WATCH at
  best). See docs/discovery.md.
- **Strategy Lab, options** (#35): `"kind": "options"` specs backtested day
  by day on the archive's EOD F&O chains (strangles, condors, iron flies,
  spreads, calendars), with dated F&O charges, STT on exercise, return on
  SPAN-style margin, tail and breach statistics, and the full walk-forward /
  DSR / PBO / gates machinery. Five library specs. Every backtest is now
  split by market regime, and an opt-in `require_regime_edge` gate exists.
- **`trader-cotrader`** (#44): today's candidates from lab-validated rules
  with evidence tiers (PROMOTED, WATCH; RESEARCH only on request). Each
  candidate is a full plan with size, charges and a trader-risk verdict. It
  shows the after-charges hit rate in today's regime cell with a Wilson 95%
  range and n, shrunk toward the rule's overall rate, plus expected R after
  charges and a time-machine second opinion. Candidates are ranked per unit
  of risk with penalties for uncertainty and correlation. The no-edge answer
  comes with its reasons. Every prediction is logged once and scored (Brier,
  log loss, reliability), and overconfident strategies are demoted. See
  docs/cotrader.md.
- **`trader-autopilot`** (#45): the co-trader loop on the PAPER account, on
  the NSE clock. PROMOTED and WATCH strategies only, per-strategy capital
  buckets, trader-risk on every order and a machine yes labelled
  `autopilot`. It handles stops, targets, trailing and time exits, the
  flat-by, F&O baskets at the close and expiry settlement, then writes the
  journal sync, calibration, the lab's forward record (promote or demote) and
  a daily report. Resumable, with one loop per folder; it replays past days
  on a simulated clock and refuses a live config. See docs/autopilot.md.
- **`trader-journal`** (#49): one entry per closed trade (an F&O strategy as
  one), from paper fills, live trade books and Zerodha / Upstox / Dhan /
  Angel One / Groww imports. Expectancy and win rate with intervals by
  strategy, persona, regime, time of day, weekday and instrument; the
  rule-break log, the overtrading index and the cost drag. A counterfactual
  ledger replays skipped candidates and risk-blocked orders. A weekly review
  feeds the journal coach. See docs/journal.md.
- **`trader-tax`** (#50): a financial-year tax ESTIMATE for your CA. It
  covers intraday (speculative), F&O (non-speculative) and capital gains
  (s.111A / s.112A with the 2018 grandfathering), the ICAI turnover and the
  audit check, set-off and carry-forward, special-rate amounts, advance-tax
  dates and ITR-3 schedules. FY 2025-26 follows the 1961 Act and tax year
  2026-27 the 2025 Act, with every rule dated and sourced. Not tax advice.
  See docs/tax.md.
- **`trader-copilot`, the live co-pilot** (#46): live proposals only for
  PROMOTED strategies whose paper forward record meets your thresholds
  (trades, weeks, drawdown in R, Brier score, no recent demote). Each one is
  sized in its own live capital bucket and gets trader-risk PASS on the live
  config and a plain live ticket. It is sent only after your exact typed
  phrase for that order (terminal, or the UI after a 10-second countdown),
  then trader-broker's six gates. Linked exits per broker, a shadow paper
  twin, daily caps, an automatic live kill switch with flatten tickets, a
  cool-off, expiry and price-move invalidation. No auto-approve. A Co-pilot
  UI page and the read-only MCP tools `copilot_status` and
  `copilot_proposals`. Fully autonomous live trading is not planned (#67).
  See docs/copilot.md.
- **Brokers phase 2** (#34): native linked exits (Kite GTT OCO, Upstox
  multi-leg GTT, Dhan Super Order) under the same six gates, with `cancel`,
  `modify-exits` and a read-only `reconcile`. An **Angel One** SmartAPI
  adapter with TOTP login. `ticket.py margin-check`: the broker's basket
  margin next to our estimate in trader-strategy and trader-risk (never
  changes a verdict). The OpenAlgo bridge is documented as a hook, not
  built.
- **AI assistant integration v2** (#47): the co-trader workflow tools
  `trade_brief`, `best_for_regime`, `strategy_design` (a fixed, safe spec
  vocabulary), `lab_run_spec`, `compare_strategies` and `archive_status`.
  Honest trial counting: every server-run spec is logged by strategy family,
  and later specs pay for earlier trials in the deflated Sharpe. Five
  co-trader prompts (`daily_cotrader_brief`, `find_best_trade_now`,
  `design_strategy_for_window`, `stress_test_my_idea`, `review_my_week`) that
  require n, the 95% interval, after-charges numbers, the regime and what
  the evidence does not show. With the co-pilot's two read-only tools the
  MCP server has **55 tools and 16 prompts** (from 32 and 11), still with no
  live-order or approval tool.
- **Ask page** (#47): optional chat with your own LLM endpoint inside the UI
  (`BT_LLM_*` environment variables only; chat-completions or messages
  format). It has a privacy notice and per-session consent, answers on
  loopback only, never exposes the key, and is limited to the read-only
  tools and the paper draft. See docs/ai-chat.md.
- **UI redesign** (#48): a research-led design system (docs/design.md), a
  grouped sidebar, drawer and phone bottom bar, a command palette, guided or
  expert detail, dark and light themes, and a "why" panel on every number.
  New pages: the **Today** cockpit, **Strategy Lab**, **Co-trader**,
  **Auto-pilot**, **Co-pilot**, **Journal**, **Tax** and **Ask**, plus a
  first-run **setup guide**. `ui/screenshots.py` makes screenshots with a
  browser you already have.
- **Hinglish** (#52): `--lang hi-en` or `"lang": "hi-en"` for tickets,
  confirmations, risk verdicts, alerts, the co-trader and its sit-out answer,
  the auto-pilot report, exit plans, sizing, the weekly review and the brief.
  It is also a UI toggle and an MCP `lang` argument. Numbers, ₹, IST and the
  safety rules stay English, and only the English *yes* places an order. See
  docs/hinglish.md.
- Docs: a co-trader guide (the daily and weekly workflow, what probabilities
  and calibration mean, assistant prompts, the safety model), a README that
  leads with the real-data results, fresh screenshots, and the v0.4 roadmap.

### Changed
- trader-risk blocks a co-trader RESEARCH idea whenever the config is live.
- trader-broker: `confirm --actor autopilot` records a labelled machine yes
  and is refused on a live config. `place` refuses any yes the user did not
  type.
- trader-lab and trader-timemachine read bars from the local archive when
  present. trader-lab's `run_and_decide` takes `extra_trials`.
- `paper_order_draft` (MCP) takes `candidate: ID`.
- The offline UI demo copies three real lab runs (one options) and a journal
  into its `trader/` folder. Every page opens with a one-line description of
  its step in the trader's day.

### Fixed
- The navigation no longer cuts off pages at 1280 or 1440 px, and every page
  is reachable on a phone (#39).

## [0.2.0] - 2026-11-01

NSE F&O, an honest Strategy Lab, live broker adapters behind gates, a local
web UI, an MCP server and an agent team. Still standard library only, still
paper by default.

### Added
- **`trader-options`** (#18): the NSE option chain (v3 API) with the
  exchange's expiries and lot sizes per expiry month; the forward from
  put-call parity; our own Black-76 IV (bracketed Newton) and Greeks; PCR by
  OI and volume, max pain, call/put walls, OI build-up, ATM straddle and
  expected move, IV smile and 25-delta skew, IV rank and percentile from
  India VIX; stale-spot warnings. `--write` saves `trader/options.json`.
- **`trader-strategy`** (#26): strategy builder on the real chain (spreads,
  straddles, strangles, iron condor and fly, butterflies incl. broken wing,
  ratios, covered call, protective put, calendars, custom legs by strike, ATM
  offset, delta or premium). Payoff at expiry and at any date, breakevens,
  max profit and loss, net Greeks; the same after the dated 2026 F&O charges
  with an STT-on-exercise warning; margin required as a SPAN-style estimate
  with its error (checked against NSE's SPAN file), shown apart from the
  exact max loss; probability of profit textbook vs a seeded bootstrap with
  IV shifts, EV and the 5% tail. `--write`, and `--orders` for broker-neutral
  NFO proposals.
- **`trader-lab`** (#25): the Strategy Lab. JSON strategy specs in a safe
  rule vocabulary; a bar-by-bar backtest with no look-ahead, Indian charges,
  slippage, trader-budget's sizing and trader-risk's day limits; walk-forward,
  parameter neighbourhood, Monte Carlo, deflated Sharpe ratio, probability of
  backtest overfitting (CSCV), VIX and trend regimes, 1.5x/2x costs.
  Pre-registered gates give REJECT, WATCH or PROMOTE, with DEMOTE when the
  paper forward record decays. Six library specs (all rejected on the
  fixture data). `lab.py list | show | validate | run | report | compare | forward`.
- **`trader-doctor`** (#28): a self-test of every NSE and Yahoo source the
  tools read: ok, slow, changed, down or blocked, since when, the tools
  affected and the fallback in use; holiday-aware freshness in IST;
  `--offline`, `--record`, exit code 1 on failure.
- **Dated market rules** (#20): `SESSION_RULES` with the NSE or SEBI circular
  per row, read through `session_for(date, segment)` in every timed skill:
  the closing auction for F&O stocks from 2026-08-03 and its revision from
  2026-12-07, the F&O close, the revised pre-open; NSE's F&O stock list
  (cached daily); broker MIS square-off profiles (`broker_profile`, `size.py
  init --broker-profile`); `alerts.py add --closing-auction`.
- **F&O charges** in the shared charges helper: `round_trip_charges(...,
  segment="OPT" | "FUT", on=date, exercised=False)` with a dated table (STT
  from 2026-04-01 including 0.15% of intrinsic value on exercise), overridable
  with `fno_charges`; broker charge profiles for paper fills.
- **Risk v2** (#30): an F&O basket checked as ONE position (lot multiples
  from `fo_mktlots.csv`, NSE's ban list, expiry-day ELM, premium budget, lots
  per underlying, margin against free cash, defined risk as the stop, naked
  shorts only with a stop rule); portfolio delta, gamma, vega and theta
  limits and a spot x IV scenario grid after charges; circuit-breakers
  (losing streak, daily drawdown, cooling-off, revenge flag) on new entries
  only; an order-rate limit (default 3 a second, cap 9); a kill switch
  (`check.py kill [--flatten]`, `unkill` with the typed word); `check.py
  scenarios`, `refresh`, `rate-gate`.
- **Paper broker v2** (#31): NSE options and futures in whole lots, NRML and
  MIS; a multi-leg proposal is one basket ticket with one typed yes, filled
  all or nothing, hedge first; margin blocked per strategy with a cash/margin
  ledger; spread-based slippage, market protection, price bands, optional
  seeded part fills; `ticket.py mtm` with Black-76 Greeks; `adjust` (roll,
  close a leg, add a hedge, close all) through risk and a new yes; `settle`
  at NSE's final settlement price with STT on exercise; shadow mode and
  `shadow-report` for live orders.
- **Live broker adapters** (#29): Upstox (v2/v3), Zerodha Kite Connect v3
  and Dhan v2 behind one contract with capability flags, normalised errors,
  rate limiting and instrument masters with NFO symbol mapping. Read-only
  `ticket.py accounts | funds | holdings | positions | orders | instruments`;
  `login` (one-shot 127.0.0.1 browser login, token printed for your shell,
  never written to disk); `preflight` (static IP, token cutoff, one harmless
  call). A live order only from `ticket.py place` after six gates: config
  broker, `"paper": false`, `live_enabled`, risk PASS for that exact order,
  your recorded yes, today's preflight. Opt-in sandbox smoke tests.
- **Local web UI** (#19, #26, #29, #30, #31): `python3 ui/serve.py --open`.
  Dashboard, candlestick charts with the plan's levels, scanner and
  watchlist, option chain, strategy builder with payoff charts, paper
  portfolio with F&O by strategy and basket tickets, broker holdings and
  positions (read only), risk panel with the kill switch, Greeks gauges and
  scenario heatmap, agent team, data badge and a validated settings editor.
  One route table over the skills' own functions; a "why" panel for every key
  number; Server-Sent Events; 127.0.0.1 only with Host/Origin checks, CSRF
  tokens and a strict CSP; `--offline` demo on saved real data. TradingView
  Lightweight Charts 5.2.1 (Apache-2.0) vendored unmodified.
- **MCP server** (#24): `mcp/server.py`, standard library only, so any
  MCP-compatible AI assistant can use the pack. 32 tools, `trader/*.json`
  resources and 11 prompts; stdio and loopback streamable HTTP; MCP revision
  2026-07-28 plus 2025-11-25, 2025-06-18 and 2025-03-26. No live-order tool:
  `paper_order_confirm` needs the exact risk-PASS fingerprint and the user's
  own yes (elicitation when supported). Rate limited; writes only under
  `<workdir>/trader/`; `--offline` with a per-tool manifest.
- **Agent team** (#17): six personas in `agents/` (intraday trader, swing
  trader, options seller, researcher, risk officer, journal coach) with
  portable frontmatter, routines of exact commands and MCP tools, hard limits
  and report formats; `--persona` tags on proposals; persona scorecards after
  charges with behaviour flags on the UI's Agents page and the
  `persona_scorecards` tool; one MCP prompt per persona.
- **Docs** (#13): `docs/getting-started.md` (the first 30 minutes),
  `docs/README.md` (an index), `docs/options.md`, `docs/brokers.md`,
  `docs/ui.md`, `docs/mcp.md`, `docs/agents.md`, `docs/lab.md`; a README with
  a feature overview, three quick starts, an honest comparison table and
  screenshots; `tests/test_docs.py` checks that every documented command
  parses with every flag and every relative link resolves.

### Changed
- `trader/portfolio.json` has a schema (`bharat-trader/portfolio/2`) with
  F&O positions, strategies, blocked margin and a ledger; older files are
  migrated on read and kept as `portfolio.json.v1.bak`.
- Entry, exit, risk, alerts, timemachine, scanner and broker end an F&O
  stock's session at continuous trading's end and pull a too-late flat-by
  in, with a note; trader-risk blocks entries during the closing auction.
- With `broker_profile` set, paper fills of shares use that broker's
  brokerage; without it, nothing changes.
- `ticket.py flatten` names open F&O strategies and the `adjust --close-all`
  that closes each.
- The UI's settings page cannot change `broker`, `paper` or `live_enabled`,
  and the UI's confirm, like `paper_order_confirm`, refuses a live config.
- A skill may keep helper modules in one subfolder (standard library, still
  self-contained); `tests/test_repo.py` checks it, and keeps the rules, F&O
  charge tables and the lab's sizing in sync across copies.
- New config keys: `allow_fno` and the F&O, Greeks, scenario and breaker
  limits, `risk_free_rate`, `broker_profile`, `live_enabled`, `static_ip`,
  `order_rate_per_sec`, `shadow` and the paper-fill keys. See
  `docs/configuration.md`.

### Fixed
- `scan.py orb` found few or no usable charts later in the day (#23, #27):
  Yahoo's spark answer leaves out a 5-minute close that lies on the straight
  line between its neighbours; stage 1 now restores those bars, and stage 2
  still checks each candidate on the full chart.
- Equity exchange transaction charge default is now 0.00307% (#22, #36): NSE's
  charge plus the IPFT contribution (NSE/FA/73061, 27 Feb 2026), in every copy
  of the charges helper, `config.example.json` and `size.py init`. Worked
  examples move by a few paise; the dated F&O rates are unchanged.
- trader-risk blocked rolling a covered short leg as naked (#33, #38): an
  adjustment ticket is now checked on the position it leaves, with the
  strategy's open legs from `fno_positions`. Covered rolls pass; a roll or close
  that leaves a short uncovered blocks unless `allow_naked_short` is on.

## [0.1.0] - 2026-10-29

### Added
- First release for the Indian cash market (NSE): IST session 09:15-15:30,
  2026 NSE trading holidays, ₹ amounts with Indian digit grouping.
- `trader-scanner`: NSE equity universe (EQ series) and pre-market and
  opening-range breakout scans on Yahoo `.NS` data.
- `trader-news`: Yahoo headlines plus NSE corporate announcements, tagged for
  Indian events (results, dividends, bonus, buyback, SEBI, pledges...).
- `trader-analyst`: quarterly results from NSE XBRL filings, in ₹ crore, by
  Indian financial-year quarter.
- `trader-insider`: NSE bulk and block deals and SEBI PIT insider disclosures.
- `trader-budget init`: build your own risk config from capital and risk %.
- Charges model (brokerage, STT, exchange, SEBI, stamp duty, GST) in sizing and
  risk checks.
- Paper broker with broker-neutral order tickets (entry, SL-M stop, LIMIT target).

[Unreleased]: https://github.com/rajdavee/bharat-trader/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/rajdavee/bharat-trader/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/rajdavee/bharat-trader/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rajdavee/bharat-trader/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rajdavee/bharat-trader/releases/tag/v0.1.0
