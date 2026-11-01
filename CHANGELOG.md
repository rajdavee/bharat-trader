# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/rajdavee/bharat-trader/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/rajdavee/bharat-trader/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rajdavee/bharat-trader/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rajdavee/bharat-trader/releases/tag/v0.1.0
