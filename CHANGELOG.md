# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`trader-autopilot`** (#45): the co-trader loop, run by itself on the
  PAPER account. `autopilot.py start [--once] [--dry-run] | stop | status |
  report [--date] | replay --date D [--to D2]`. A resumable stdlib loop on the
  NSE clock (IST, holidays, the closing auction): pre-open (trader-doctor,
  trader-regime, an optional trader-archive update, the co-trader brief);
  the entry window (the co-trader every `cadence_min` minutes; PROMOTED and
  WATCH candidates only, never RESEARCH; sized by trader-budget inside each
  strategy's capital bucket; trader-risk on that exact order; a machine yes
  recorded word for word with `"actor": "autopilot"`; orders a second apart);
  manage on every 5-minute bar (LIMIT fills by the bars, skipped when opening
  through the stop or target, stops with gaps, targets, trailing stops, the
  strategy's own exit rule and maximum holding, the MIS flat-by, delivery
  positions carried overnight, the spec's slippage); F&O baskets on
  end-of-day settle prices from the archive (risk as one position with the
  spec's stop as its risk rule, one machine yes for every leg, close-all as
  one basket, expiry settlement); end of day (unfilled entries cancelled, the
  journal hook `journal.autopilot_sync(workdir, day)` when trader-journal is
  installed, calibration scoring with demotion, each strategy's paper trades
  in `trader/autopilot/records/` and in its trader-lab forward record, the
  lab's forward test re-run for promote/demote, and the day's report as
  Markdown and JSON). Per-strategy capital, max positions and daily loss, plus
  global limits, in an `autopilot` block of `trader/config.json`. The kill
  switch stops entries and flattens on paper. State saved after every action
  (intent before each broker call: no double order or fill after a crash), a
  lock file for one loop per folder, graceful stop. `replay` runs past
  sessions in a sandbox with a simulated clock, the lab re-run as of the close
  before (no look-ahead). Paper only in code: a live config is refused before
  anything and checked every step; only paper broker commands are run, never
  `place`. Method and safety model in `docs/autopilot.md`.
- trader-broker: `confirm --actor autopilot` records a machine yes labelled
  as such and is refused on a live config; `place` refuses any yes the user
  did not type.
- UI: the **Auto-pilot** page (status, start/stop with the typed words
  `start paper` / `stop paper`, today's timeline, open paper positions by
  strategy, each strategy's forward record next to the lab's verdict, the
  day's report, replay a past day) and `/api/autopilot`,
  `/api/autopilot/start`, `/stop`, `/replay`.
- MCP: `autopilot_status`, `autopilot_replay` (a sandbox, read-only for your
  account), `autopilot_start` and `autopilot_stop` (paper only, the user's
  typed confirmation by elicitation or argument). 42 tools.
- **`trader-cotrader`** (#44): calibrated, backtested trade candidates for
  today. `cotrader.py today [--tier research]`, `explain ID`, `calibration`,
  `log`, `--json`; writes `trader/cotrader.json`, a plan per candidate
  (`trader/cotrader/plans/`) and a prediction log. Evidence tiers on every
  candidate: PROMOTED (lab gates + paper forward record), WATCH (gates passed,
  forward test running: paper only), RESEARCH (lab REJECT/DEMOTE or never run:
  "unproven, for study/paper only", only with `--tier research`). For each
  strategy whose regime cell fits today (trader-regime trend x VIX bucket, as
  of the close before each signal), live triggers from the lab's own signal
  engine and forward test become complete plans: entry, stop, target, size
  (trader-budget), charges at the stop and target, trader-risk PASS/BLOCK
  with reasons. Probability: the cell's after-charges hit rate with its
  Wilson 95% interval and n, shrunk toward the strategy's overall rate with a
  Beta prior worth 20 trades (posterior mean and 95% range), plus the shrunk
  expected R after charges; a trader-timemachine replay as a second opinion,
  with disagreements flagged. Ranking: expected R per unit risk minus
  penalties for interval width and for correlation with open positions and
  higher-ranked candidates; tier first; ties by liquidity. No edge is a
  first-class answer: "sitting out is the decision", with the reasons (no
  validated strategy, regime mismatch with numbers, no trigger, risk locks)
  and what would change them. Explanations: why, invalidation, the evidence,
  what it does NOT show, the nearest similar past trades. Calibration: every
  prediction logged once, resolved from paper fills, the lab's forward record
  or the next bars; Brier score with its base-rate reference, log loss and
  reliability bins per strategy and tier; an overconfident PROMOTED or WATCH
  strategy is demoted through trader-lab's status (DEMOTE). Options specs
  (#57) join through the lab's options forward test: the drafted
  trader-strategy basket is a candidate (structure, legs, margin, max loss
  after charges) risk-checked as one position. Method in `docs/cotrader.md`.
- UI: the **Co-trader** page (candidate cards with the tier badge, odds with
  range and n, expected R after charges, the replay, the risk verdict, why /
  why not, Draft to paper through trader-budget and trader-risk, the no-edge
  card, the calibration chart) and the Today page's candidates card;
  `GET /api/cotrader`, `POST /api/cotrader/run`, `POST /api/cotrader/draft`.
- MCP: `cotrader_candidates`, `cotrader_explain` and `calibration_report`
  (38 tools); `paper_order_draft` takes `candidate: ID`.
- trader-risk: a co-trader RESEARCH idea (plan setup `cotrader-research:`)
  is blocked on a live config: unproven ideas are paper only.
- **`trader-discover`** (#43): strategy discovery with strict overfitting
  controls. `discover.py run --budget N --universe nifty50 --timeframe 1d|5m
  [--seed]`, `resume`, `report`, `list`. A fixed trial budget declared up
  front in `plan.json` (a seeded random or grid sample of 918 daily and 1,398
  five-minute rules over trader-lab's vocabulary: RSI dips, breakouts, MA
  crosses, gaps, pullbacks, opening ranges, VWAP, yesterday's high, time
  windows, ATR stops, R targets, maximum holding, trend filters); a time split
  into train / validation / untouched holdout with purging and an embargo;
  every trial logged to `trials.jsonl` (spec hash, params, metrics) and
  counted. Screens on trades and after-charges expectancy; cost stress at
  1.5x and 2x, the parameter neighbourhood (run and counted), regime
  consistency (trader-regime); the deflated Sharpe ratio with the true trial
  count, the probability of backtest overfitting by CSCV over the trial
  matrix, a stationary-bootstrap Reality Check / SPA p-value against doing
  nothing, a Benjamini-Hochberg cut, the Harvey-Liu haircut Sharpe. The
  holdout is opened once per finalist and logged (`holdout.jsonl`).
  Finalists are written as trader-lab specs (`trials_tried` = N - 1) and run
  by the lab: WATCH at best, a paper forward test before any PROMOTE.
  Everything else is REJECTED with its stage and reason. Time budget with
  pause and resume, progress, lowered CPU priority, reproducible by seed.
  Tested on synthetic data: a random walk gives no finalists in every seed,
  a planted edge is found. Method and sources in `docs/discovery.md`.
- MCP: `discover_strategies` (budgeted, progress notifications, cancellable,
  resumable) and the read-only `discover_report` (35 tools). UI: the
  Strategy Lab page lists discovery runs and their funnels (`GET /api/lab`
  adds `discovery`).
- **UI redesign** (#48): a research-led rework of the local web app, written up
  in `docs/design.md` (workflow research, an audit of the old UI, the design
  system). Design system v2 in CSS variables: type scale, spacing, radii,
  elevation, semantic gain/loss/warn/info colours that always come with a
  sign, ▲/▼ or ✓/✕, dark and light themes, guided or expert detail, reduced
  motion. New components: grouped sidebar, drawer and phone bottom bar,
  command palette (Ctrl/Cmd-K), metric tiles with "why" panels and plain-words
  "?" help, meters against limits, steppers, toasts, empty, loading and error
  states, sticky table headers.
- **Today** cockpit as the home page: a session rail from pre-open to the
  close with your entry window, exits-only time and flat-by, day P&L after
  charges, the daily loss budget, trades used, risk state (kill switch,
  circuit-breakers), positions, the plan and its last risk check, alerts,
  NSE status and the data self-test; places for the market regime and
  co-trader candidates.
- **Strategy Lab page** and `GET /api/lab`: each trader-lab run's verdict,
  out-of-sample sample size, expectancy after charges, Monte Carlo range,
  drawdown, equity curve, gates, regime split, deflated Sharpe, overfitting
  odds and what a backtest does not show.
- **Setup guide** on a fresh folder and `GET /api/onboarding`: capital, risk
  per trade, trades per day, product and broker previewed as the config
  `size.py init` would write and saved through the validated settings API;
  then the MCP entry for your assistant and the read-only broker variables.
  Every step skippable; going live is never part of it.
- Placeholder pages for Co-trader, Auto-pilot and Journal, labelled "Coming
  in v0.3".
- `ui/screenshots.py`: screenshots of every page at any width with a headless
  browser you already have (DevTools protocol, standard library), flagging
  sideways scroll and error notes; `tests/test_ui_screens.py` runs it on the
  key pages when a browser is installed.
- **`trader-archive`** (#41, archive half of #35): a local NSE market data
  archive under `trader/archive/`. `backfill --years N` fetches equity and F&O
  bhavcopies (UDiFF from 2024-01-02, the legacy format before, one schema),
  index closes, India VIX, lot sizes, ban lists, futures settlement prices
  and corporate actions, newest day first, 1.5 s between NSE calls, stopping
  after repeated blocks; weekends and holidays are skipped and missing
  weekdays learned as closed. Gzip CSV per dataset per day with a manifest
  (source URL, fetched_at, rows, SHA-256); resumes after any interruption.
  `update`, `status`, `verify` (checksums, gaps vs the calendar, duplicates,
  OHLC, split/bonus factors checked against prices, unexplained moves, short
  sessions, intraday gaps, survivorship), `export` (lab CSV), `intraday`
  (Upstox v3 candles through trader-broker's adapter and your token; Kite
  and Dhan when you have their plan; merged with Yahoo, gap report) and
  `snapshot-chain` (option chains during market hours). An offline read
  API: `load_daily` (split- and bonus-adjusted), `load_intraday`,
  `load_fo_chain_eod`, `load_vix`, `load_index`, `trading_days` and more.
- **`trader-regime`** (#42): the market regime per day for NIFTY, BANKNIFTY
  and any NSE stock. Trend or range from ADX(14), Kaufman's efficiency ratio,
  the 20/50 EMAs and slope and the 200-day average; the volatility bucket
  from India VIX's percentile over a year (stocks: their own realised vol),
  realised vol 20d vs 1y and IV rank (India VIX; a stock's approximate ATM IV
  from the archived F&O bhavcopy with `--iv`); the gap in ATRs; breadth from
  the equity bhavcopy; weekly and monthly expiry today or next session from
  the exchange's list in the archive (else a dated Tuesday/Thursday rule with
  holiday shifts); results days from NSE board meetings; RBI policy and Union
  Budget days (`events.json`, each with its source); holidays, special
  sessions and the closing-auction window from the dated SESSION_RULES; and
  time-of-day buckets (open drive, morning, midday, afternoon, closing
  window, closing auction). `now`, `history` (stores `trader/regime/*.json`
  and an archive table `archive/regime/`), `explain`, `--json`; thresholds in
  `trader/regime-config.json`. No look-ahead: each day uses data up to its
  own close (tested by changing the future). Archive first, Yahoo daily bars
  when there is none or it is thin. Pure functions for other skills.
- `trader-lab` splits every backtest by market regime (NIFTY trend or range
  x India VIX bucket x expiry flag, as of the close before each signal), with
  trades, mean R and a 95% interval per cell, and has an opt-in gate
  `require_regime_edge` ("edge in the current regime", `min_regime_trades`).
- MCP: the read-only tool `regime_now` (33 tools) and `trader/regime/`
  resources. UI: `GET /api/regime`, shown in the Today page's Market card.
- `trader-lab` reads bars from the archive when present (`--archive`,
  `--no-archive`), else Yahoo; `trader-timemachine` adds the archive's older
  5-minute sessions to its pool.
- **Strategy Lab, options** (#35): `"kind": "options"` specs backtested day
  by day on the archive's end-of-day F&O bhavcopies, in trader-lab's new
  `options/` helper package. Structures from trader-strategy's templates
  (short strangle and straddle, iron condor and fly, bull put and bear call
  spreads, calendars, long straddle and strangle); strikes by delta (IV
  solved from settle prices on a parity forward), % out of the money or
  points from ATM; entries by days to expiry, weekday, IV rank (the
  underlying's own 30-day ATM IV) and India VIX; exits at a profit target or
  stop on the premium, days before expiry, else cash settlement at the final
  settlement price. Legs marked at each close from settle prices, Black-76 at
  the day's smile IV when a strike did not trade (flagged); dated F&O charges
  per side with STT on exercise; slippage as % of premium plus ticks; lot
  sizes, holiday-shifted expiries and holidays from the files. Margin is
  trader-strategy's SPAN-style estimate (with the expiry-day ELM) and R is
  return on margin. The equity machinery (walk-forward, neighbourhood, Monte
  Carlo, DSR, PBO, cost stress, VIX/trend and trader-regime splits, gates)
  plus return on margin, worst week, 5%/1% tail loss, losing streaks, time in
  drawdown and short-strike breaches. Five library specs (NIFTY weekly
  16-delta strangle and 100-point-wing condor, NIFTY expiry-day iron fly,
  BANKNIFTY monthly put spread, IV-rank-filtered strangle). `forward` drafts
  a paper basket through trader-strategy (`strategy.json` and `orders.json`)
  for trader-risk; nothing is placed. UI: the Lab page shows options
  scorecards; MCP: `lab_run` and `lab_forward` accept options specs
  (`draft: NIFTY` writes the basket and runs trader-risk on it).

### Changed
- The offline demo copies `tests/fixtures/ui/lab/` (three real lab runs, one options) into
  its `trader/lab/`.
- Every page now follows the design system and opens with a one-line
  description of its step in the trader's day (hidden in expert detail).

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

[Unreleased]: https://github.com/rajdavee/bharat-trader/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rajdavee/bharat-trader/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rajdavee/bharat-trader/releases/tag/v0.1.0
