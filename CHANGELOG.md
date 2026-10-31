# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Changed
- Entry, exit, risk, alerts, timemachine, scanner and broker end an F&O
  stock's session at continuous trading's end and pull a too-late flat-by in,
  with a note. trader-risk blocks entries during the closing auction.
- Tests never go online: `tests/_load.py` swaps the F&O list fetch for a
  stub, and `tests/test_repo.py` keeps the rules and F&O charge blocks
  identical in every copy and checks the charges by segment.

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
