# bharat-trader docs

New here? Start with [getting-started.md](getting-started.md): from a fresh
clone to a paper trade, an option strategy, a lab verdict, the web UI and
your own AI assistant in about 30 minutes. Then read
[cotrader-guide.md](cotrader-guide.md): the daily and weekly routine, what
the probabilities mean, and the safety model in one place.

## Using it

| page | read it for |
| --- | --- |
| [getting-started.md](getting-started.md) | Your first 30 minutes, step by step, with real output. |
| [cotrader-guide.md](cotrader-guide.md) | The co-trader workflow end to end: pre-open brief, candidates, the paper auto-pilot, the journal, the weekly review, promote and demote, and the live co-pilot for PROMOTED strategies only; the honest real-data results; what a probability and its calibration mean and do not mean; prompts for your AI assistant; the safety model in one table. |
| [configuration.md](configuration.md) | Every key in `trader/config.json`: money and risk, circuit-breakers, F&O limits, brokers, and the charges model. |
| [options.md](options.md) | The option chain, Greeks (Black-76), strategies, after-charges payoffs, STT on exercise, the margin estimate and its limits, probability of profit two ways, and paper F&O (basket, margin, MTM, expiry). |
| [lab.md](lab.md) | The Strategy Lab: why most backtests lie, the spec, the backtest, walk-forward, deflated Sharpe, overfitting odds, the gates and the paper forward test. |
| [cotrader.md](cotrader.md) | The co-trader: evidence tiers, how a candidate's probability is made (Wilson interval, empirical-Bayes shrinkage, the time-machine second opinion), what it means and does not, ranking, the no-edge answer, and calibration (Brier score, reliability, demotion). |
| [copilot.md](copilot.md) | The live co-pilot: the safety model first (who may trade live, the typed phrase per order, caps and the live kill, what can never approve), then a live order step by step, and why full autonomy is not built. |
| [autopilot.md](autopilot.md) | The auto-pilot on paper: the safety model (paper only, in code), a day step by step, fills, F&O baskets at the close, how its forward record feeds the lab's promote and demote, and replay. |
| [forward-tests.md](forward-tests.md) | The forward-test dashboard: each strategy's paper record against its backtest's predicted range, the control chart and decay alerts (recommend DEMOTE, applied only when you ask), calibration, days to PROMOTE; and the intraday option-chain capture the auto-pilot keeps running, with launchd, systemd and Task Scheduler examples. |
| [notifications.md](notifications.md) | Local notifications, off by default: desktop pop-ups and an optional webhook of your own, the events, rate limits and quiet hours, and exactly what a webhook receives (never secrets or positions). |
| [discovery.md](discovery.md) | Strategy discovery: why most found strategies are noise, the declared search, the purged time split, the deflated Sharpe, overfitting odds, Reality Check / SPA and FDR with the true trial count, the holdout, and the limits. |
| [research/edge-hunt.md](research/edge-hunt.md) | The edge-hunt brief: the evidence (global and Indian, with dates), SEBI's F&O studies, 2026 charge drag computed with the pack's own helpers, the data each idea needs, and 14 pre-registered hypotheses with grids, sealed holdouts, kill criteria and honest priors. |
| [brokers.md](brokers.md) | Upstox, Zerodha Kite, Dhan and Angel One: setup, the **static IP** and **daily login** rules, read-only commands, the broker's basket margin next to our estimate, native linked stop and target (GTT OCO, Super Order), and every gate before a live order. |
| [ui.md](ui.md) | The local web UI: running it, the pages, the routes, the security model and the offline demo. |
| [design.md](design.md) | How the web UI is organised and why: the UX research, the audit of the old UI, the design system and the page groups. |
| [mcp.md](mcp.md) | Connecting any MCP-compatible AI assistant: install, every tool and prompt, using it as an evidence-first co-trader, trial-count honesty, the paper order flow, the security model and the protocol. |
| [ai-chat.md](ai-chat.md) | The optional Ask page: chat with your own LLM endpoint inside the UI (env-only setup, the two API formats, privacy and consent, what it can and cannot do). |
| [journal.md](journal.md) | The trade journal: entries from paper fills and broker trade books, statistics with their ranges, the rule-break log, the counterfactual ledger and the weekly review. |
| [tax.md](tax.md) | The tax estimate: how trades are classified (intraday, F&O, delivery), the ICAI turnover and the audit check, set-off and carry-forward, the dated rules table with sources (Income-tax Act, 1961 and 2025), ITR-3 schedules, and what could not be verified. |
| [hinglish.md](hinglish.md) | Hinglish output (`--lang hi-en`, the config's `lang`, the UI toggle): what is translated, what stays English and why, the style guide and the safety lines side by side. |
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
