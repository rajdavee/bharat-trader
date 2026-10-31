# bharat-trader

**Agent skills for intraday trading on NSE.** Scan the market, read the news
and filings, time the entry, plan the exit, replay the trade 10,000 times,
size it to your risk with real Indian charges, and paper-trade it. Nothing is
ever placed without your yes.

[![CI](https://github.com/rajdavee/bharat-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/rajdavee/bharat-trader/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

- **Free.** No signup, no API key, no paid data. Public NSE data and Yahoo Finance.
- **Built for India.** IST session 09:15–15:30, the closing auction for F&O
  stocks (dated, with NSE's circulars), NSE holidays, ₹ with lakh/crore
  grouping, your broker's MIS square-off, tick sizes, STT, stamp duty and GST
  in every number.
- **Plain Python.** Standard library only, Python 3.8+. Every tool runs by hand too.
- **Safe by default.** Paper trading. A risk check on every order. Your typed yes, every time.

> **Not financial advice.** Most intraday traders lose money. This is software
> that reads public data and does arithmetic. Read the [fine print](#fine-print).

## The skills

| skill | what it does |
| --- | --- |
| `trader-scanner` | Builds the NSE equity universe and scans every chart: 20-day breakouts, bull flags, 20-DMA reclaims, oversold bounces, and opening-range breakouts during the day. |
| `trader-news` | Yahoo headlines and NSE corporate announcements for your watchlist, tagged: results, dividend, bonus, buyback, block deal, SEBI order, pledge… |
| `trader-entry` | Watches a setup bar by bar from 09:15 and calls the moment a 5-minute bar closes above the opening range. |
| `trader-exit` | Sets the stop and target before you enter, rounded to the tick, with reward:risk before and after charges. |
| `trader-timemachine` | Replays the rest of the session 10,000 times from the stock's own last 60 days, plus every past day the setup fired. Honest odds. |
| `trader-budget` | `init` builds your risk config from your capital. Sizes each trade so the loss at the stop, charges included, fits your limit. |
| `trader-risk` | PASS or BLOCK for every order: stop, max loss, daily loss, trades per day, entry window, market hours, position size, cash. |
| `trader-broker` | Paper broker: plain-language tickets, records your yes, simulates fills with charges, flattens before square-off. |
| `trader-alerts` | Rings on your stop, your target, the opening bell and your flat-by time. Knows the NSE calendar. |
| `trader-analyst` | Quarterly results straight from NSE XBRL filings: revenue, margins, profit, EPS, YoY and QoQ, in ₹ crore. |
| `trader-insider` | NSE bulk and block deals and SEBI insider-trading (PIT) disclosures, flagged when they touch stocks you hold or watch. |
| `trader-options` | NSE option chain with our own Black-76 IV and Greeks off the put-call-parity forward: PCR, max pain, call/put walls, OI build-up, straddle and expected move, IV smile and IV rank from India VIX. |
| `trader-lab` | Strategy Lab: backtests a JSON strategy bar by bar after Indian charges, then walk-forward, parameter neighbourhood, Monte Carlo, deflated Sharpe and overfitting odds. Pre-registered gates: REJECT, WATCH (paper forward test) or PROMOTE. Six library strategies, no promises. |
| `trader-doctor` | Self-test of every data source (NSE APIs and archives, Yahoo): ok, slow, changed, down or blocked, since when, which tools it breaks and the fallback in use. Holiday-aware freshness in IST. |

## Install

These are [Agent Skills](https://agentskills.io): a `SKILL.md` file plus a
script per folder. Any AI agent that supports Agent Skills can load them.

```bash
git clone https://github.com/rajdavee/bharat-trader.git
cp -r bharat-trader/trader-* <your-agent-skills-folder>/
```

Copy one folder or all of them. Each folder works on its own. No agent at all?
Every tool runs from the terminal, shown below.

Requirements: Python 3.8 or newer. Nothing to `pip install`.

## Quick start

Work from any folder. The tools keep everything in a `trader/` subfolder there.

```bash
python3 trader-budget/size.py init                     # your capital and risk, once
python3 trader-scanner/scan.py universe                # NSE EQ-series stocks
python3 trader-scanner/scan.py daily                   # pre-market setups
python3 trader-news/news.py --tickers RELIANCE HDFCBANK --hours 12
python3 trader-scanner/scan.py orb --at 09:45          # opening-range breakouts so far
python3 trader-entry/entry.py check RELIANCE --setup orb --write
python3 trader-exit/exit.py plan --stop or-mid --target 2R --write
python3 trader-timemachine/timemachine.py run --write
python3 trader-budget/size.py --write                  # -> trader/orders.json
python3 trader-risk/check.py                           # PASS / BLOCK
python3 trader-broker/ticket.py show                   # the paper ticket
python3 trader-alerts/alerts.py add --from-plan        # stop, target, flat by 15:10
python3 trader-analyst/facts.py TCS                    # last quarters from NSE filings
python3 trader-insider/insider.py deals                # today's bulk and block deals
python3 trader-options/options.py chain NIFTY          # option chain, IV, Greeks, PCR, max pain
python3 trader-options/options.py iv NIFTY             # IV smile, skew, IV rank from India VIX
python3 trader-lab/lab.py run orb15-vwap               # backtest + robustness + gates, after charges
python3 trader-lab/lab.py report orb15-vwap            # the scorecard, and what it does NOT prove
python3 trader-doctor/doctor.py                        # is every NSE and Yahoo source answering? (exit 1 if not)
```

Prefer a screen? The local web UI shows the same numbers, from the same
scripts, on `127.0.0.1` only:

```bash
python3 ui/serve.py --open                             # dashboard, charts, scanner, paper orders, risk, settings
python3 ui/serve.py --offline tests/fixtures/ui --open # a demo on saved real data, no network
```

See [docs/ui.md](docs/ui.md).

## Use it from your AI assistant (MCP)

`mcp/server.py` is a standard-library [MCP](https://modelcontextprotocol.io/)
server: connect any MCP-compatible AI assistant and it can call 24 tools
(market status, scans, news, entry and exit plans, the time machine, sizing,
risk, the paper broker, option chains, results, insider deals, the strategy lab, the
data self-test) and 5 prompts
(morning briefing, intraday plan, options income scan, weekly review,
research a strategy). Add it to your client's configuration:

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

Paper trading only, with no live-order tool at all: a paper order needs
trader-risk PASS for that exact order and your own typed yes, which the server
asks you for directly when the client supports it. Try it on saved data with
`--offline /path/to/bharat-trader/tests/fixtures/mcp`. See
[docs/mcp.md](docs/mcp.md) for every tool, the security model and good
questions to ask.

With an agent, just ask: *"scan NSE for breakouts"*, *"any news on Reliance?"*,
*"plan my exit for HDFCBANK"*, *"how many shares can I buy with ₹1,000 risk?"*,
*"what are promoters buying?"*, *"does the opening-range breakout really work after charges?"*.

## A trading day

```
09:00  pre-open      scanner daily · news · insider        build the watchlist
09:15  open          entry watch                            opening range forms (09:15–09:30)
09:30  entries open  entry triggers → exit → timemachine → budget → risk → broker (your yes)
       in trade      alerts: stop · target · flat-by
15:10  flat-by       broker flatten                         before MIS auto square-off (earlier with broker_profile)
15:15  F&O stocks    closing auction to 15:35                continuous trading ends (15:30 from 7 Dec 2026)
15:30  close
```

See [docs/architecture.md](docs/architecture.md) for how the skills hand off
through `trader/`, [docs/configuration.md](docs/configuration.md) for every
setting and the charges model, [docs/ui.md](docs/ui.md) for the local web UI, and [docs/mcp.md](docs/mcp.md)
for the MCP server.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite runs offline on fixtures cut from real NSE and Yahoo data, on
Python 3.8–3.13, Linux, macOS and Windows.

## Roadmap

Next up (v0.2.0): F&O options analytics, a strategy builder with after-charges
payoffs and honest odds, multi-broker paper trading, broker adapters (Upstox,
Zerodha Kite, Dhan), risk v2 and a local web UI. See
[docs/roadmap.md](docs/roadmap.md) and the
[v0.2.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/1).

Ideas and pull requests welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

## Fine print

**Not financial advice.** This software does not know your situation and
cannot see the future. Most people who trade intraday lose money; SEBI's own
studies have found that a large majority of individual intraday traders make
losses. Decide for yourself, and consult a SEBI-registered investment adviser
if you need advice.

**Nothing is placed without your yes.** Only `trader-broker` can lead to an
order, only after `trader-risk` passes that exact order, and only after you
type yes. In v0.1 the broker is paper only.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's last 60 days of 5-minute bars. Results days, circuit limits, news and
F&O expiry can do things those days never did.

**A stop is a plan, not a guarantee.** Gaps, circuits and fast markets can
fill a stop well past its price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API, and NSE's website APIs can change or rate-limit without notice.
Check anything that matters against your broker before you act.

**Charges are estimates.** Defaults model a typical discount broker. Set your
broker's actual rates in `trader/config.json`.

**Insider and bulk-deal data are public disclosures, not inside information.**
SEBI's PIT Regulations require them to be disclosed; this tool reads what is
already public.

**Taxes.** Intraday equity profits are generally treated as speculative
business income in India. Nothing here is tax advice.

## License

[MIT](LICENSE) © Raj Dave and bharat-trader contributors.

Not affiliated with NSE, BSE, SEBI, Yahoo or any broker. See [NOTICE](NOTICE).
