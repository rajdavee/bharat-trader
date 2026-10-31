# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `mcp/server.py`: an MCP server, standard library only, so any
  MCP-compatible AI assistant can use bharat-trader. JSON-RPC 2.0 over stdio
  (logs to stderr only) and optional streamable HTTP on 127.0.0.1 (Host and
  Origin checks). MCP revision 2026-07-28 (per-request `_meta`,
  `server/discover`, multi round-trip elicitation, `resultType`, cache hints)
  plus the handshake-based 2025-11-25, 2025-06-18 and 2025-03-26. 19 tools
  over the skills' own functions and commands (market status, daily and ORB
  scans, news, entry check, exit plan, time machine, sizing, risk check,
  paper tickets, draft and confirm, portfolio, option chain and summary,
  results, bulk/block deals, PIT disclosures, config) with input and output
  schemas, structured results and annotations; `trader/*.json` as resources;
  5 prompts. No live-order tool: `paper_order_confirm` needs the exact
  risk-PASS fingerprint and the user's typed yes (elicitation when the client
  supports it). Rate limited; writes only under `<workdir>/trader/`;
  `--offline DIR` with a per-tool manifest (`tests/fixtures/mcp`). See
  `docs/mcp.md`.
- Dated market rules (`SESSION_RULES`, with the NSE or SEBI circular for each
  row) in entry, exit, alerts, risk, timemachine, scanner and broker, read
  through `session_for(date, segment)`: the Closing Auction Session for F&O
  stocks from 2026-08-03 (continuous trading to 15:15, auction to 15:35) and
  its revision from 2026-12-07 (to 15:30, auction 15:30-15:40), F&O contracts
  to 15:40 (15:45 from 2026-12-07), and the revised pre-open from 2026-09-07
  (order entry to 09:10, market orders until 09:05).
- NSE's F&O stock list (`nseindia.com/api/master-quote`), cached in
  `trader/fno_stocks.json` and refreshed once a day by online runs, or
  `fno_stocks` in the config. With no list every stock is treated as an F&O
  stock (the earlier close).
- Broker MIS square-off profiles: `broker_profile` zerodha, upstox, dhan,
  angel, groww or custom (`square_off`), dated, with sources; `size.py init
  --broker-profile`. The flat-by is never later than 5 minutes
  (`square_off_margin_min`) before the earlier of the square-off and
  continuous trading's end, and entries stop 15 minutes before that.
- `alerts.py add --closing-auction`: a bell when F&O stocks enter the
  auction. `clock` prints the day's auction and F&O close.
- F&O charges in the shared charges helper: `round_trip_charges(...,
  segment="OPT" | "FUT", on=date, exercised=False)` with a dated table
  (STT from 2026-04-01: 0.15% of the option premium sold, 0.15% of the
  intrinsic value on exercise, 0.05% on futures sold; NSE transaction charge
  with IPFT, SEBI fee, stamp duty, GST), overridable with `fno_charges`.
  Equity results are unchanged.

- `trader-lab` (#16, phase 1): the Strategy Lab. Strategies are plain JSON
  specs in a small safe rule vocabulary (SMA, EMA, RSI, ATR, VWAP, opening
  range, previous-day high/low/close, gap %, comparisons, crosses, time
  windows; nothing is eval'd). A bar-by-bar backtest with no look-ahead
  (signal on the close, fill at the next open plus slippage), the shared
  Indian charges on every trade, trader-budget's sizing, one position per
  stock, MIS flat by the dated safe flat-by (closing auction, broker
  square-off) and trader-risk's day limits. Robustness: rolling walk-forward,
  parameter-grid neighbourhood, seeded Monte Carlo reshuffles and bootstrap
  bands, the deflated Sharpe ratio, the probability of backtest overfitting
  (CSCV), India VIX and NIFTY trend regimes, costs at 1.5x and 2x.
  Pre-registered gates give REJECT, WATCH or PROMOTE, with an automatic
  DEMOTE when the rolling paper forward record decays. `lab.py list | show |
  validate | run | report | compare | forward`; `forward` drafts trader-exit
  shaped plans for trader-budget and trader-risk and never places anything.
  Six library specs (ORB 15m with VWAP, VWAP reclaim, gap fade,
  previous-day-high breakout, RSI(2) on NIFTY 50 names, 20-day breakout
  swing): all six are rejected on the fixture data. Writes
  `trader/lab/<name>/{spec.json, results.json, scorecard.md, equity.csv}`.
  An `OPTIONS_HOOK` for trader-strategy. Method and limits in `docs/lab.md`.

### Changed
- Entry, exit, risk, alerts, timemachine, scanner and broker end an F&O
  stock's session at continuous trading's end and pull a too-late flat-by in,
  with a note. trader-risk blocks entries during the closing auction.
- Tests never go online: `tests/_load.py` swaps the F&O list fetch for a
  stub, and `tests/test_repo.py` keeps the rules and F&O charge blocks
  identical in every copy and checks the charges by segment. It now includes
  trader-lab (order path, Safety block, charges, rules) and checks the lab's
  sizing against trader-budget's.
- `trader-options`: NSE option chain (v3 API) with expiries from the exchange's
  list and lot sizes per expiry month from `fo_mktlots.csv`. Forward from
  put-call parity, Black-76 implied volatility (bracketed Newton solver, `null`
  for untradeable quotes) and Greeks (theta per calendar day, vega per vol
  point), PCR by OI and volume, max pain, call/put walls, OI build-up, ATM
  straddle and expected move, IV smile and 25-delta skew, IV rank and
  percentile from India VIX history, stale-spot warnings after 15:30.
  `--write` saves `trader/options.json` (schema `bharat-trader/options/1`).
- Config key `risk_free_rate` (default 0.0557).
- Local web UI, `python3 ui/serve.py --open` (#11, foundation): dashboard (NSE
  status, NIFTY 50 / BANK NIFTY / INDIA VIX, today's paper P&L, plan, alerts),
  candlestick chart with volume, EMA, session VWAP, opening-range band and the
  plan's entry/stop/target (1m, 5m, 1d), scanner and watchlist, paper portfolio
  and order tickets (risk check, typed yes, decline, paper fill), risk panel and
  a validated settings editor. Option chain and strategy builder pages describe
  what v0.2 will show.
- Every number comes from the skills' own functions through one route table
  (`ui/api.py`); a "why" panel shows each key number's formula, inputs, source
  and time. Live updates over Server-Sent Events.
- Local-first security: binds 127.0.0.1, Host and Origin checks, per-session
  CSRF token on every write, strict Content-Security-Policy, no CORS, nothing
  loaded from the internet. `--offline DIR` serves saved real data for demos.
- TradingView Lightweight Charts 5.2.1 (Apache-2.0) vendored unmodified in
  `ui/static/vendor/` with its LICENSE, NOTICE and SHA-256.

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

[Unreleased]: https://github.com/rajdavee/bharat-trader/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rajdavee/bharat-trader/releases/tag/v0.1.0
