# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
