# bharat-trader docs

New here? Start with [getting-started.md](getting-started.md): from a fresh
clone to a paper trade, an option strategy, a lab verdict, the web UI and
your own AI assistant in about 30 minutes.

## Using it

| page | read it for |
| --- | --- |
| [getting-started.md](getting-started.md) | Your first 30 minutes, step by step, with real output. |
| [configuration.md](configuration.md) | Every key in `trader/config.json`: money and risk, circuit-breakers, F&O limits, brokers, and the charges model. |
| [options.md](options.md) | The option chain, Greeks (Black-76), strategies, after-charges payoffs, STT on exercise, the margin estimate and its limits, probability of profit two ways, and paper F&O (basket, margin, MTM, expiry). |
| [lab.md](lab.md) | The Strategy Lab: why most backtests lie, the spec, the backtest, walk-forward, deflated Sharpe, overfitting odds, the gates and the paper forward test. |
| [cotrader.md](cotrader.md) | The co-trader: evidence tiers, how a candidate's probability is made (Wilson interval, empirical-Bayes shrinkage, the time-machine second opinion), what it means and does not, ranking, the no-edge answer, and calibration (Brier score, reliability, demotion). |
| [discovery.md](discovery.md) | Strategy discovery: why most found strategies are noise, the declared search, the purged time split, the deflated Sharpe, overfitting odds, Reality Check / SPA and FDR with the true trial count, the holdout, and the limits. |
| [brokers.md](brokers.md) | Upstox, Zerodha Kite and Dhan: setup, the **static IP** and **daily login** rules, read-only commands, and every gate before a live order. |
| [ui.md](ui.md) | The local web UI: running it, the pages, the routes, the security model and the offline demo. |
| [design.md](design.md) | How the web UI is organised and why: the UX research, the audit of the old UI, the design system and the page groups. |
| [mcp.md](mcp.md) | Connecting any MCP-compatible AI assistant: install, every tool and prompt, using it as an evidence-first co-trader, trial-count honesty, the paper order flow, the security model and the protocol. |
| [ai-chat.md](ai-chat.md) | The optional Ask page: chat with your own LLM endpoint inside the UI (env-only setup, the two API formats, privacy and consent, what it can and cannot do). |
| [journal.md](journal.md) | The trade journal: entries from paper fills and broker trade books, statistics with their ranges, the rule-break log, the counterfactual ledger and the weekly review. |
| [agents.md](agents.md) | The six agent personas, how to run them as sub-agents or over MCP, and their paper scorecards. |

## How it works

| page | read it for |
| --- | --- |
| [architecture.md](architecture.md) | How the skills hand off through `trader/` files, the broker-neutral order shape and the safety chain. |
| [archive.md](archive.md) | The local market data archive: backfill, formats and cut-overs, adjustment, broker intraday, verify, and the read API. |
| [regime.md](regime.md) | The market regime engine: trend, volatility, gap, expiry and event days, time of day; the method, the thresholds, no look-ahead and the limits. |
| [data-sources.md](data-sources.md) | Every public NSE and Yahoo source the tools read, tick sizes, and the data self-test. |
| [roadmap.md](roadmap.md) | What shipped in each release and what is next. |

Elsewhere in the repo: each skill's `SKILL.md` (for example
[trader-strategy/SKILL.md](../trader-strategy/SKILL.md)),
[agents/README.md](../agents/README.md), [mcp/README.md](../mcp/README.md),
[CHANGELOG.md](../CHANGELOG.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).

Education and tooling, not financial advice.
