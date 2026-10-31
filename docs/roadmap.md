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

## v0.2.0 — Sprint 1: F&O, strategies, brokers and the local UI

Tracked in the [v0.2.0 milestone](https://github.com/rajdavee/bharat-trader/milestone/1).

| # | Theme | Deliverable |
| --- | --- | --- |
| 1 | Rules | Dated rule files: closing auction session (3 Aug 2026; revised 7 Dec 2026), derivatives session, 2026 F&O STT, lot sizes from NSE's daily file, expiry calendar from the exchange; broker square-off profiles. Existing skills read them. |
| 2 | Options | `trader-options`: chain (NSE v3), expiries, lot sizes, Black-76 IV off the put-call-parity forward, Greeks, PCR (OI and volume), max pain, OI change, call/put walls, expected move, IV rank from India VIX history. |
| 3 | Strategy | `trader-strategy`: spreads, straddles, strangles, butterflies, condors, iron flies, ratios and custom legs; payoff at expiry and today; breakevens; max profit/loss; the same curve after charges; STT-on-exercise warning; margin estimate with honest error bounds; textbook vs simulated probability of profit. |
| 4 | Paper broker v2 | F&O legs and multi-leg baskets, broker charge profiles, slippage and market-price-protection simulation, margin blocking. |
| 5 | Risk v2 | F&O checks (lot multiples, ban list, expiry-day rules, premium budget), portfolio Greeks limits and a spot × IV scenario grid, behavioural circuit-breakers, order-rate limit, kill switch. |
| 6 | Brokers | Adapter contract with capability flags; env-var credentials; local login helper (127.0.0.1 callback); static-IP and session preflight. Upstox, Zerodha Kite and Dhan read-only first; order placement only through risk PASS + typed yes + explicit live mode. |
| 7 | UI | `ui/serve.py`: local web app — dashboard, candlestick charts with levels, scanner and watchlist, option chain, strategy builder with payoff chart, paper portfolio, risk panel, settings. A "why" panel explains every number. |
| 8 | Data | `doctor`: self-test of every data source with dated fallbacks. |
| 9 | Docs | F&O guide, broker setup, UI guide, honest comparison table. |

### Design decisions

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
  opt-in per broker, checked at startup, and every order still needs a typed yes.
- **No closed-logic signals.** Open, explainable tools only; we never sell or
  push strategies to others.

## v0.3.0 — Sprint 2 (planned)

- `trader-journal`: auto-journal from paper and broker tradebooks, expectancy
  by setup and time of day, rule-break log.
- `trader-tax`: financial-year report — speculative (intraday),
  non-speculative (F&O, with turnover) and capital gains. Estimate; verify
  with your CA.
- Counterfactual ledger: what the tools talked you out of, scored later.
- Historical expiry-to-expiry replay of option strategies from NSE bhavcopies.
- Angel One and more brokers; shadow mode (paper twin of every live order).
- Hinglish output (`--lang hi-en`).
