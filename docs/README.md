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
| [brokers.md](brokers.md) | Upstox, Zerodha Kite and Dhan: setup, the **static IP** and **daily login** rules, read-only commands, and every gate before a live order. |
| [ui.md](ui.md) | The local web UI: running it, the pages, the routes, the security model and the offline demo. |
| [mcp.md](mcp.md) | Connecting any MCP-compatible AI assistant: install, every tool and prompt, the paper order flow, the security model and the protocol. |
| [agents.md](agents.md) | The six agent personas, how to run them as sub-agents or over MCP, and their paper scorecards. |

## How it works

| page | read it for |
| --- | --- |
| [architecture.md](architecture.md) | How the skills hand off through `trader/` files, the broker-neutral order shape and the safety chain. |
| [data-sources.md](data-sources.md) | Every public NSE and Yahoo source the tools read, tick sizes, and the data self-test. |
| [roadmap.md](roadmap.md) | What shipped in each release and what is next. |

Elsewhere in the repo: each skill's `SKILL.md` (for example
[trader-strategy/SKILL.md](../trader-strategy/SKILL.md)),
[agents/README.md](../agents/README.md), [mcp/README.md](../mcp/README.md),
[CHANGELOG.md](../CHANGELOG.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).

Education and tooling, not financial advice.
