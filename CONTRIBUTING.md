# Contributing to bharat-trader

Thanks for helping make free, honest trading tools for Indian markets. This
guide covers how the project is built and what a good pull request looks like.

## Ground rules

1. **Standard library only.** Every tool runs on a stock Python 3.8+ with
   nothing to `pip install`. If you think a dependency is unavoidable, open an
   issue first.
2. **Each skill folder stands alone.** A user can copy one folder into their
   agent's skills directory and it works. Small helpers (IST time, the NSE
   calendar, `inr()` formatting, charges) are duplicated on purpose; a repo
   test keeps the copies in sync.
3. **Safety is not optional.** Nothing in this project may place an order
   without the user's explicit, typed yes. Paper trading stays the default.
   Every order passes `trader-risk` first.
4. **Free, public data only.** NSE India, NSE archives and Yahoo Finance today.
   No scraping behind logins, no paid feeds in core, no redistribution of data
   beyond small test fixtures.
5. **Tests run offline.** Every change ships with tests on fixtures cut from
   real data, so CI never touches the network.

## Getting started

```bash
git clone https://github.com/rajdavee/bharat-trader.git
cd bharat-trader
python3 -m unittest discover -s tests -v
```

Run a tool against live data from a scratch folder, so its `trader/` output
stays out of the repo:

```bash
mkdir -p /tmp/bt && cd /tmp/bt
python3 ~/path/to/bharat-trader/trader-scanner/scan.py universe
```

## Repository layout

```
trader-<skill>/        SKILL.md (agent instructions) + one Python script
tests/test_<skill>.py  unittest suite for that skill
tests/fixtures/<skill> real data, trimmed and gzipped where large
docs/                  architecture, configuration, data sources
```

See [docs/architecture.md](docs/architecture.md) for how the skills hand data
to each other through the `trader/` folder.

## Making a change

1. Open or comment on an issue so work isn't duplicated.
2. Branch from `main`: `feat/scanner-vwap`, `fix/risk-tick-size`, `docs/...`.
3. Write the test first when fixing a bug: reproduce it on a fixture.
4. Keep the SKILL.md in step with the script: commands, flags, output.
5. Run the full suite. CI runs Python 3.8 to 3.13 on Linux, plus macOS and Windows.
6. Open a pull request using the template.

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(scanner): add VWAP reclaim setup
fix(risk): round SL trigger to tick size before comparing
docs(readme): explain MIS auto square-off
```

### Adding a fixture

Fetch real data once, trim it to what the test needs, gzip anything over
~50 KB, and name it after the source and date
(`RELIANCE-2026-10-29-5m.json`). Never include personal or account data.

### Market facts

NSE holidays, charges and session times change. When you update one, link
the official circular or page in the pull request. The holiday table lives in
every script that needs a calendar; update all copies (the repo test fails if
they differ).

## Adding a broker adapter

Broker support is planned as separate adapters that map the broker-neutral
order in `trader/orders.json` to a broker's API. Adapters must:

- read keys only from environment variables, never from chat or files in the repo;
- default to the broker's sandbox or paper mode where one exists;
- refuse to send any order that `trader-risk` did not PASS or that lacks a recorded yes.

Open an issue to discuss the design before writing one.

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
