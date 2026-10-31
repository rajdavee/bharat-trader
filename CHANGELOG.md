# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Live broker adapters in `trader-broker/adapters/` (standard library only):
  Upstox (v2 and v3), Zerodha Kite Connect v3 and Dhan v2, one contract in
  `base.py` with capability flags (profile, funds, holdings, positions,
  orders, quotes, history, instruments, basket margin, GTT/OCO, sandbox,
  order placement), the broker-neutral shapes, normalised errors (token
  expired, IP not whitelisted, rate limited, insufficient funds, rejected,
  not in your plan, broker down), a token-bucket rate limiter (orders 3 a
  second by default, never above 9) and timeouts with retries for reads
  only. Instrument masters with symbol mapping, NFO included (Kite
  `NIFTY26N0925000CE`, Upstox `NSE_FO|40037`, Dhan `40037`), joined on NSE's
  exchange token. Refs #10.
- `ticket.py accounts`, `funds`, `holdings`, `positions`, `orders` and
  `instruments` read your real account (`--broker`, `--json`); `login
  <broker>` is a one-shot 127.0.0.1 browser login that checks `state` and
  prints `export BT_<BROKER>_ACCESS_TOKEN=...` for your shell (never written
  to disk); `preflight <broker>` checks the public IP (two echo services must
  agree) against `static_ip` and the IPs Upstox and Dhan have on record, the
  token's daily cutoff and one harmless call, and explains SEBI's static-IP
  and daily-2FA rules in plain words.
- Live orders, off by default: `ticket.py place --index N` sends one order
  only when the config broker is that broker, `"paper": false`,
  `"live_enabled": {"<broker>": true}`, trader-risk passed that exact order
  today, your typed yes for it is recorded and today's preflight passed with
  the token in use. With a live config `confirm` only records the yes. One
  yes, one order; orders are never retried; MARKET and SL-M only with the
  broker's market protection (Kite, Upstox); `--leg stop|target` after the
  fill, one exit leg at a time; `cancel`. New config keys `live_enabled`,
  `static_ip`, `order_rate_per_sec`.
- Local UI: broker connection on Settings (`/api/broker`: which `BT_*`
  variables are set, never their values; the last preflight) and your
  broker's holdings and positions on Portfolio
  (`/api/broker/holdings`, `/api/broker/positions`), read only. MCP: three
  read-only tools, `broker_status`, `broker_holdings`, `broker_positions`
  (28 tools); still no order tool.
- `tests/test_broker_adapters.py`: one contract suite for all three adapters
  against a local stub HTTP server replaying doc-derived, secret-free
  fixtures and real instrument-master slices; every live-order gate tested
  on its own; login, preflight and scrubbing tests; opt-in Upstox and Dhan
  sandbox smoke tests (`BT_SANDBOX=1`). `docs/brokers.md`: setup per broker,
  static IP, daily login, sandboxes, plans and costs as of 1 Nov 2026.
- `trader-strategy`: option strategy builder on the real NSE chain.
  Templates for bull/bear call and put spreads, long/short straddles and
  strangles, iron condor, iron fly, call/put butterflies (broken wing with
  `--skip`), ratio spreads, covered call, protective put and calendars, plus
  custom legs picked by strike, ATM offset, delta or premium. Payoff at expiry
  and at any date (Black-76, IV per leg, IV shift), breakevens, max profit and
  loss with unlimited sides detected, net premium and Greeks, the same curve
  after the dated 2026 F&O charges (STT on exercise for in-the-money longs)
  with the breakeven shift and an STT-on-exercise warning; margin required
  (what a broker would block: a SPAN-style estimate, scan risk on the whole
  position plus exposure on every short leg, +/-15% index, +/-35% stock,
  checked against NSE's SPAN file; the premium for bought options only),
  with the exact max loss and the return on margin shown apart from it; probability of profit textbook
  (lognormal at IV) vs a seeded bootstrap of the underlying's own daily and
  five-minute returns with IV-shift scenarios, expected value and 5% tail
  after charges. `--write` saves `trader/strategy.json`
  (`bharat-trader/strategy/1`); `--orders` writes broker-neutral NFO
  proposals for trader-risk and the paper broker. Works copied alone from a
  trader-options JSON file. See `docs/options.md`.
- Local UI: the Option chain page (ITM shading, OI bars, OI change, IV, Greeks
  toggle, PCR, max pain, walls, expected move, IV smile, expiry picker, why
  panels) and the Strategy builder page (templates, legs editor, payoff gross
  vs after charges with spot and breakevens, today vs expiry, Greeks, margin
  required with method and error, max loss and return on margin, PoP textbook vs simulated, Send to paper as a
  proposal through trader-risk and a typed yes). Routes `/api/options`,
  `/api/strategy`, `/api/strategy/price`, `/api/strategy/draft`.
- MCP tools `strategy_build` and `strategy_price` (read only).
- `mcp/server.py`: an MCP server, standard library only, so any
  MCP-compatible AI assistant can use bharat-trader. JSON-RPC 2.0 over stdio
  (logs to stderr only) and optional streamable HTTP on 127.0.0.1 (Host and
  Origin checks). MCP revision 2026-07-28 (per-request `_meta`,
  `server/discover`, multi round-trip elicitation, `resultType`, cache hints)
  plus the handshake-based 2025-11-25, 2025-06-18 and 2025-03-26. 23 tools
  over the skills' own functions and commands (market status, daily and ORB
  scans, news, entry check, exit plan, time machine, sizing, risk check,
  paper tickets, draft and confirm, portfolio, option chain and summary,
  results, bulk/block deals, PIT disclosures, config, and the strategy lab:
  `lab_list`, `lab_run` with progress and a time budget, `lab_report`,
  `lab_forward`, which drafts paper tickets only through `paper_order_draft`)
  with input and output
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

- `trader-doctor/doctor.py`: a self-test of every data source the tools read
  (#12). One polite request each to NSE (EQUITY_L, marketStatus, holiday
  master, corporate announcements, board meetings, integrated-filing results,
  bulk.csv, block.csv, the deals history, PIT, option contract info and
  option-chain-v3, fo_mktlots, master-quote, India VIX history) and Yahoo
  (chart for RELIANCE.NS and ^NSEI, spark, headlines RSS). Checks the shape
  the tools read, freshness against the NSE calendar in IST (a weekend
  answer from the last session is fresh) and units; prints ok, slow,
  changed, down or blocked with since when, the tools affected and the
  fallback in use (Yahoo query1 is tried when query2 fails). History in
  `trader/doctor.json`; `--json`, `--only nse|yahoo`, `--offline DIR`
  (repeatable) and `--record DIR`; exit code 1 on any failure. Backs off
  5 s on NSE 403/429 and stops asking NSE after a second block.
- UI: a "Data" badge in the header (the last self-test; click to run it),
  `GET /api/doctor` and `POST /api/doctor/run`. MCP: the read-only `doctor`
  tool.

### Changed
- A skill may keep helper modules in one subfolder (a package its entry
  script loads by path), still standard library and self-contained; the repo
  tests check it.
- The UI's settings page can no longer change `broker`, `paper` or
  `live_enabled` (going live is your own edit of the file), and its confirm,
  like the MCP server's `paper_order_confirm`, refuses a live config.
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

### Fixed
- `scan.py orb` found few or no usable charts later in the day, worst on
  Yahoo's 5-day data for a past session (#23). Yahoo's spark answer leaves out
  a 5-minute close that lies on the straight line between its neighbours, so
  the "every bar is there" check failed. Stage 1 now puts those bars back on
  that line between two bars of the same session; stage 2 still checks each
  candidate on the full v8 chart, where a missing bar still makes it unusable.

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
