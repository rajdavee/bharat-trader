# Roadmap

bharat-trader's aim: **the most correct, most honest, local-first trading
toolkit for Indian markets.** Free, standard-library Python, agent-native,
with a zero-install local UI. No signals, no promised returns; you decide.

## Where we stand out

Free option chains and payoff charts are now table stakes (several brokers
and open-source projects give them away). What nobody offers together:

1. **Honest odds after charges.** Every payoff, probability and backtest is
   shown gross *and* after brokerage, STT, exchange fees, stamp duty and GST,
   with textbook probability next to a simulated one.
2. **Rules that are right, dated and versioned.** 2026 STT, the closing
   auction session, lot sizes, expiry calendars, SEBI's retail-algo
   framework — each rule carries its effective date and source.
3. **Multi-broker paper trading, zero install.** Equity and F&O, with each
   broker's charges and square-off times, before any real money moves.
4. **Local-first.** One command opens a UI on `127.0.0.1`. Your data, keys
   and trades never leave your machine.
5. **Risk that fits how traders actually lose.** Scenario and Greeks limits
   for options, behavioural circuit-breakers, and a kill switch.
6. **Odds that are checked afterwards.** Every co-trader probability comes
   with its range and sample size, is logged before the outcome, and is
   scored later. When nothing has an edge, the answer is to sit out.

## v0.2.0: F&O, strategies, the lab, brokers and the local UI (done)

Tracked in the [v0.2.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/1).
Every item below is in the code; the [CHANGELOG](../CHANGELOG.md) has the detail.

| # | Theme | Done |
| --- | --- | --- |
| 1 | Rules | ✅ Dated session rules with their circulars: the closing auction for F&O stocks (3 Aug 2026; revised 7 Dec 2026), the F&O close, the revised pre-open; 2026 F&O STT; lot sizes from NSE's daily file; the expiry calendar from the exchange; broker MIS square-off profiles. |
| 2 | Options | ✅ `trader-options`: chain (NSE v3), expiries, lot sizes, Black-76 IV off the put-call-parity forward, Greeks, PCR (OI and volume), max pain, OI change, call/put walls, expected move, IV rank from India VIX. See [options.md](options.md). |
| 3 | Strategy | ✅ `trader-strategy`: spreads, straddles, strangles, butterflies, condors, iron flies, ratios, calendars and custom legs; payoff at expiry and today; breakevens; max profit/loss; the same curve after charges; the STT-on-exercise warning; margin required with its method and error; textbook vs simulated probability of profit. |
| 4 | Paper broker v2 | ✅ F&O legs and multi-leg baskets (one ticket, one yes), broker charge profiles, spread-based slippage, market protection, price bands, margin blocking, MTM with Greeks, adjustments, expiry settlement, shadow mode. |
| 5 | Risk v2 | ✅ F&O checks (lot multiples, ban list, expiry-day rules, premium budget, margin), portfolio Greeks limits and a spot x IV scenario grid, behavioural circuit-breakers, order-rate limit, kill switch. |
| 6 | Brokers | ✅ Adapter contract with capability flags; env-var credentials; a one-shot 127.0.0.1 login helper; static-IP and session preflight. Upstox, Zerodha Kite and Dhan read-only; a live order only through risk PASS + typed yes + explicit live mode + preflight. See [brokers.md](brokers.md). |
| 7 | UI | ✅ `ui/serve.py`: dashboard, candlestick charts with levels, scanner and watchlist, option chain, strategy builder with payoff chart, paper portfolio with F&O by strategy, risk panel, agent team, settings. A "why" panel explains every number. See [ui.md](ui.md). |
| 8 | Data | ✅ `trader-doctor`: self-test of every data source with since-when history and fallbacks. |
| 9 | Docs | ✅ Getting started, F&O guide, broker setup, UI guide, an honest comparison table, a docs index. |
| 10 | MCP | ✅ `mcp/server.py`: 32 tools and 11 prompts at v0.2 (53 and 16 in v0.3) for any MCP-compatible assistant; paper only, no live-order tool. See [mcp.md](mcp.md). |
| 11 | Strategy Lab | ✅ `trader-lab`: after-charges backtests, walk-forward, deflated Sharpe, probability of backtest overfitting, pre-registered gates, paper forward test with automatic demotion. See [lab.md](lab.md). |
| 12 | Agent team | ✅ Six personas in `agents/` with paper scorecards, as sub-agent files and MCP prompts. See [agents.md](agents.md). |

### Design decisions (v0.2)

- **Skills stay self-contained.** New skills (`trader-options`,
  `trader-strategy`) are folders you can copy alone. A skill may carry helper
  modules in its own folder (broker adapters live in `trader-broker/`).
- **The UI reuses the skills.** `ui/serve.py` loads skill scripts directly,
  so there is one implementation of every number. Standard library only.
- **Charts.** Candlesticks use TradingView's open-source Lightweight Charts
  (Apache-2.0, bundled with its notice and attribution). Payoff and Greeks
  charts are our own canvas code.
- **Live orders are the exception.** SEBI's retail-algo framework (in force
  1 Apr 2026) requires a whitelisted static IP, daily two-factor login and
  rate limits for API orders. Paper trading stays the default; live mode is
  opt-in per broker, gated by a daily static-IP preflight, and every order still needs a typed yes.
- **No closed-logic signals.** Open, explainable tools only; we never sell or
  push strategies to others.

## v0.3.0: the evidence-first co-trader (done)

Tracked in the [v0.3.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/2).
Every item below is in the code. The [CHANGELOG](../CHANGELOG.md) has the
detail, and the [co-trader guide](cotrader-guide.md) puts it together.

| # | Theme | Done |
| --- | --- | --- |
| 1 | Data archive ([#41](https://github.com/rajdavee/bharat-trader/issues/41)) | ✅ `trader-archive`: years of equity and F&O bhavcopies, indices, VIX, lots, bans, settle prices and split/bonus factors on disk; resumable backfill, broker intraday, chain snapshots, verify. See [archive.md](archive.md). |
| 2 | Market regime ([#42](https://github.com/rajdavee/bharat-trader/issues/42)) | ✅ `trader-regime`: trend, volatility bucket, gap, breadth, expiry and event days, time of day; no look-ahead; the lab splits every backtest by it. See [regime.md](regime.md). |
| 3 | Discovery ([#43](https://github.com/rajdavee/bharat-trader/issues/43)) | ✅ `trader-discover`: a declared trial budget, purged time split, untouched holdout, deflated Sharpe with the true N, PBO, Reality Check / SPA and FDR. On real data: 0 finalists. See [discovery.md](discovery.md). |
| 4 | Strategy Lab phase 2 ([#35](https://github.com/rajdavee/bharat-trader/issues/35)) | ✅ Multi-leg option strategies backtested on the archive's EOD chains with charges, walk-forward, DSR/PBO and gates. All five library specs REJECT on 703 real sessions; the expiry-day iron fly comes closest. See [lab.md](lab.md#options-strategies). |
| 5 | Co-trader ([#44](https://github.com/rajdavee/bharat-trader/issues/44)) | ✅ `trader-cotrader`: candidates from lab-validated rules with evidence tiers, after-charges odds with a 95% range and n, a second opinion, ranking per unit risk, the no-edge answer, and calibration tracking with demotion. See [cotrader.md](cotrader.md). |
| 6 | Paper auto-pilot ([#45](https://github.com/rajdavee/bharat-trader/issues/45)) | ✅ `trader-autopilot`: the co-trader loop on paper on the NSE clock; forward records drive promote and demote; replay. Paper only, enforced in code. See [autopilot.md](autopilot.md). |
| 7 | Live co-pilot ([#46](https://github.com/rajdavee/bharat-trader/issues/46)) | In progress. Per-order approval for PROMOTED strategies only. Fully autonomous live trading is not built, and would be a separate decision. |
| 8 | AI integration v2 ([#47](https://github.com/rajdavee/bharat-trader/issues/47)) | ✅ Co-trader workflow tools and prompts, honest trial counting, 53 tools and 16 prompts; the optional Ask page with your own LLM. See [mcp.md](mcp.md), [ai-chat.md](ai-chat.md). |
| 9 | UI redesign ([#48](https://github.com/rajdavee/bharat-trader/issues/48), [#39](https://github.com/rajdavee/bharat-trader/issues/39)) | ✅ Design system, grouped navigation that works on a phone, Today, Strategy Lab, Co-trader, Auto-pilot, Journal, Tax and Ask pages, the setup guide. See [ui.md](ui.md), [design.md](design.md). |
| 10 | Journal and counterfactuals ([#49](https://github.com/rajdavee/bharat-trader/issues/49)) | ✅ `trader-journal`: entries from paper, live and five brokers' trade books; stats with intervals; rule breaks; the counterfactual ledger; the weekly review. See [journal.md](journal.md). |
| 11 | Tax ([#50](https://github.com/rajdavee/bharat-trader/issues/50)) | ✅ `trader-tax`: a dated, sourced financial-year estimate for your CA under the 1961 and 2025 Acts. See [tax.md](tax.md). |
| 12 | Brokers phase 2 ([#34](https://github.com/rajdavee/bharat-trader/issues/34)) | ✅ Native linked exits (Kite GTT OCO, Upstox GTT, Dhan Super Order), Angel One, the broker's basket margin next to our estimate. The OpenAlgo bridge is a documented hook only. |
| 13 | Hinglish ([#52](https://github.com/rajdavee/bharat-trader/issues/52)) | ✅ `--lang hi-en` across tickets, risk, alerts, the co-trader, reports and the UI. See [hinglish.md](hinglish.md). |
| 14 | Docs ([#51](https://github.com/rajdavee/bharat-trader/issues/51)) | ✅ The co-trader guide, a README that leads with the real-data results, fresh screenshots, this roadmap. |

Carried over: shadow-mode reports (slippage, fill delay and missed fills
from the paper twin of each live order), broker sandbox smoke tests in CI,
and following declined candidates in the counterfactual ledger.

### Design decisions (v0.3)

- **Honesty over hit rate.** A probability is never shown without its range
  and sample size. Every prediction is logged before the outcome is known
  and scored afterwards. "No trade today" is an answer, not an error.
- **Count every trial.** Discovery, the lab and the MCP server all charge a
  strategy family for every variant tried, so fishing raises the bar.
- **Paper earns promotion.** Only the lab's pre-registered rules and the
  paper forward record promote a strategy. Nobody can promote one by hand.
- **No autonomous live trading.** The auto-pilot is paper only, in code.
  Live orders need a person's typed yes for every order.

## v0.4.0: ideas (not committed)

Drafts for discussion; open an issue to shape or challenge any of them.

| Theme | Idea |
| --- | --- |
| Intraday option-chain archive | Record option chains through the session (from `snapshot-chain` and broker history where allowed), so option strategies can be backtested intraday, with stops seen when they happen and not only at the close. |
| More brokers | More adapters behind the same contract and six gates (candidates: Groww, Fyers, ICICI Direct, Kotak Neo), plus the OpenAlgo bridge. |
| Mobile notifications | Alerts, the pre-open brief, a co-trader candidate or the auto-pilot's report pushed to your phone through a channel you host or choose, never with an order button. |
| Community strategy specs | Share a trader-lab spec as a file with a lab-verified scorecard (the data range, trial count, gates and verdict, reproducible by anyone with the archive), so claims can be re-run, not trusted. No signals, no paid tips. |
| Performance | Faster backtests and discovery on years of archive data (caching, incremental walk-forward, parallel trials within the trial budget) and a lighter UI on slow phones. |
| Carried from v0.3 | Shadow-mode reports, broker sandbox smoke tests, declined candidates in the counterfactual ledger. |

Ideas and pull requests welcome: open an issue first for anything large.
