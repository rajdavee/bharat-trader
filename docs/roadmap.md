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
| 10 | MCP | ✅ `mcp/server.py`: 32 tools and 11 prompts at v0.2 (55 and 16 in v0.3) for any MCP-compatible assistant; paper only, no live-order tool. See [mcp.md](mcp.md). |
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
| 7 | Live co-pilot ([#46](https://github.com/rajdavee/bharat-trader/issues/46)) | ✅ `trader-copilot`: live proposals only for PROMOTED strategies with enough paper record, your exact typed phrase per order, daily caps, a live kill switch and a shadow paper twin; no auto-approve. Fully autonomous live trading is an open decision, marked not planned: [#67](https://github.com/rajdavee/bharat-trader/issues/67). See [copilot.md](copilot.md). |
| 8 | AI integration v2 ([#47](https://github.com/rajdavee/bharat-trader/issues/47)) | ✅ Co-trader workflow tools and prompts, honest trial counting, 53 tools and 16 prompts (55 with the co-pilot's two read-only tools); the optional Ask page with your own LLM. See [mcp.md](mcp.md), [ai-chat.md](ai-chat.md). |
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
  The live co-pilot needs a person's typed phrase for every order. Full
  autonomy is an open decision, marked not planned, in
  [#67](https://github.com/rajdavee/bharat-trader/issues/67).

## v0.4.0: the edge hunt (done)

Tracked in the [v0.4.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/3).
The question: is there any idea a retail trader in India could run that
shows an edge after 2026 charges and honest trial counting? Every item below
is in the code; the [CHANGELOG](../CHANGELOG.md) has the detail and the
[results page](research/edge-hunt-results.md) has the answer: **no
pre-registered idea passed, and Benjamini-Hochberg keeps 0.**

| # | Theme | Done |
| --- | --- | --- |
| 1 | Research brief ([#70](https://github.com/rajdavee/bharat-trader/issues/70)) | ✅ The evidence, SEBI's F&O studies, the charge drag of each idea, and 14 hypotheses registered before any run, with grids, sealed holdouts, kill criteria and written priors, frozen by SHA-256. See [research/edge-hunt.md](research/edge-hunt.md). |
| 2 | Long-history archive ([#71](https://github.com/rajdavee/bharat-trader/issues/71)) | ✅ Legacy bhavcopies back to 1994 (equity) and 2000 (F&O), continuous futures, point-in-time index membership, results dates, sector classification and a dated F&O lot-size history. See [archive.md](archive.md). |
| 3 | Lab engine v2 ([#72](https://github.com/rajdavee/bharat-trader/issues/72)) | ✅ Futures, cross-sectional portfolios, events and pairs engines, and the edge-hunt executor (`lab.py hunt`): runs a registration exactly as written, counts every trial, opens a holdout once. See [lab.md](lab.md#the-edge-hunt-running-pre-registered-hypotheses). |
| 4 | Options edge v2 ([#73](https://github.com/rajdavee/bharat-trader/issues/73)) | ✅ VIX-timed condors, results-day iron flies and straddles, trend-day buying on model prices, realised-vol and HAR-RV forecasts; the SPAN + ELM margin model v2 ([#82](https://github.com/rajdavee/bharat-trader/issues/82)). |
| 5 | Edge-hunt runs ([#74](https://github.com/rajdavee/bharat-trader/issues/74)) | ✅ 13 of 15 registrations decided, all REJECT before the holdout; eh-08 void and re-run as eh-15; eh-12 blocked (no minute bars), eh-14 waiting for its live holdout. See [research/edge-hunt-results.md](research/edge-hunt-results.md). |
| 6 | Strategy portfolio ([#75](https://github.com/rajdavee/bharat-trader/issues/75)) | ✅ Correlation, clusters, risk parity and capped Kelly, tested walk-forward, with auto-pilot bucket proposals. See [lab.md](lab.md#the-strategy-portfolio-combining-survivors). |
| 7 | Forward-test ops ([#76](https://github.com/rajdavee/bharat-trader/issues/76)) | ✅ The forward-test dashboard, intraday option-chain capture from 16 Nov 2026, local notifications. See [forward-tests.md](forward-tests.md), [notifications.md](notifications.md). |
| 8 | Docs and release ([#77](https://github.com/rajdavee/bharat-trader/issues/77)) | ✅ The final results, the priors scored, how to propose a new hypothesis, and [running long hunts safely](running-hunts.md) (memory-bounded engines, the guard, a watchdog). |

Carried over: shadow-mode reports, broker sandbox smoke tests, declined
candidates in the counterfactual ledger, more brokers, and the index-file
date parsing fix ([#87](https://github.com/rajdavee/bharat-trader/issues/87)).

### Design decisions (v0.4)

- **Register before you run.** Every idea is written down, hashed and merged
  before it touches the data. The run fills only `status` and `outcome`.
- **A REJECT is final under its id.** Near misses are recorded, not tuned.
  The only rerun is a re-registration of a VOID run (a run that tested
  nothing), with the void run's trials charged to it.
- **The live future is the cleanest holdout.** eh-14's holdout is option
  chains captured after its registration, so nobody can have seen it.
- **Memory is a safety issue.** A hunt that freezes the machine can take the
  rest of your work with it; the engines are bounded and the runs are
  guarded.

## v0.5.0: ideas (not committed)

Drafts for discussion; open an issue to shape or challenge any of them.

| Theme | Idea |
| --- | --- |
| Minute-bar archive | Store index, futures and stock minute bars from your own broker token (Upstox's history starts January 2022 for minute candles), so intraday ideas such as eh-12 intraday momentum stop being blocked. |
| Intraday options research | Use the option chains captured since 16 Nov 2026 for intraday option research with stops seen when they happen, starting with eh-14's live holdout once it has ended. |
| Community-registered hypotheses | Anyone can propose a hypothesis by pull request, under exactly the same rules: registered and hashed before any run, every trial and the family's earlier trials counted, the programme's Benjamini-Hochberg cut, verdict published whatever it is. See [how to propose one](research/edge-hunt-results.md#how-to-propose-a-new-hypothesis). |
| Mobile notifications polish | Phone-friendly delivery through the webhook you host (ntfy, Telegram or a channel you choose), richer pre-open and end-of-day summaries, never with an order button. |
| Carried from v0.3 and v0.4 | Shadow-mode reports, broker sandbox smoke tests, declined candidates in the counterfactual ledger, more broker adapters. |

Ideas and pull requests welcome: open an issue first for anything large.
