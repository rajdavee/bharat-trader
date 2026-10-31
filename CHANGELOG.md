# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-xx

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
