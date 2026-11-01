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
| 10 | MCP | ✅ `mcp/server.py`: 32 tools and 11 prompts for any MCP-compatible assistant; paper only, no live-order tool. See [mcp.md](mcp.md). |
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

## v0.3.0: Sprint 2 (planned)

Tracked in the [v0.3.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/2).

| Theme | Plan |
| --- | --- |
| Market data archive ([#41](https://github.com/rajdavee/bharat-trader/issues/41)) | ✅ `trader-archive`: years of equity and F&O bhavcopies, indices, VIX, lots, bans, settle prices and split/bonus factors on disk, resumable backfill, broker intraday, chain snapshots, verify. |
| Co-trader ([#44](https://github.com/rajdavee/bharat-trader/issues/44)) | ✅ `trader-cotrader`: today's candidates from lab-validated rules with evidence tiers, after-charges odds with a 95% range and n (shrunk toward the rule's overall rate), a time-machine second opinion, ranking per unit risk, no edge as a first-class answer, and calibration tracking (Brier, reliability, demotion). See [cotrader.md](cotrader.md). |
| Strategy Lab phase 2 ([#35](https://github.com/rajdavee/bharat-trader/issues/35)) | The F&O bhavcopy archive (done in #41); multi-leg option strategies (short strangle and iron condor by IV rank, expiry-day iron fly, calendars) through the lab with charges, walk-forward, DSR/PBO and gates; longer intraday history than Yahoo's 60 sessions. |
| Brokers phase 2 ([#34](https://github.com/rajdavee/bharat-trader/issues/34)) | Native linked stop and target at the broker (OCO / GTT) instead of one exit leg at a time; Angel One; an optional bridge to a self-hosted execution layer; the broker's basket margin next to our estimate; sandbox smoke tests in CI. |
| `trader-journal` ([#49](https://github.com/rajdavee/bharat-trader/issues/49)) | ✅ One entry per closed trade (an F&O strategy as one) from paper fills, live trade books and Zerodha / Upstox / Dhan / Angel One / Groww imports; expectancy and win rate with intervals by strategy, persona, regime, time of day, weekday and instrument; the rule-break log, overtrading index and cost drag; the weekly review for the journal coach. See [journal.md](journal.md). |
| `trader-tax` | A financial-year report: speculative (intraday), non-speculative (F&O, with turnover) and capital gains. An estimate; verify with your CA. |
| Counterfactual ledger ([#49](https://github.com/rajdavee/bharat-trader/issues/49)) | ✅ Every co-trader candidate not taken and every risk BLOCK of your own order, replayed on the bars with trader-lab's fill rules: "saved you X, cost you Y", with counts and caveats. Declines and rejected lab rules are not followed yet. |
| Shadow-mode reports | The paper twin of every live order (shipped in v0.2) turned into reports: slippage, fill delay and missed fills by broker, setup and time of day. |
| Hinglish | `--lang hi-en` output for every skill. |

Ideas and pull requests welcome: open an issue first for anything large.
