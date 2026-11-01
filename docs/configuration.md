# Configuration

Your settings live in `trader/config.json` in the folder you trade from. Create
it with:

```bash
python3 trader-budget/size.py init                     # asks a few questions
python3 trader-budget/size.py init --capital 200000 --risk-pct 1 --max-trades 3 --product MIS --yes
```

`init` never overwrites an existing config unless you pass `--force`. Every
key is optional: a missing key falls back to the default below.

## Money and risk

| key | default | meaning |
| --- | --- | --- |
| `capital` | 100000 | Trading capital in ₹. |
| `risk_per_trade_pct` | 1.0 | Used by `init` to derive `max_loss_per_trade`. |
| `max_loss_per_trade` | 1000 | Most you accept losing on one trade if the stop fills, charges included. |
| `max_daily_loss` | 3000 | Total risk of the day's trades. New entries are blocked past it; exits never are. |
| `max_trades_per_day` | 3 | Entries per day. |
| `max_orders_per_day` | 10 | All orders, entries and exits. |
| `max_position_pct` | 50 | Largest single position as % of capital. |
| `max_order_value` | 50000 | Largest single order in ₹. |
| `min_order_value` | 500 | Smaller orders are dropped. |
| `cash_buffer_pct` | 10 | Cash kept aside, never used for sizing. |
| `min_reward_risk` | 1.5 | Target distance ÷ stop distance must reach this. |
| `require_stop` | true | Every entry needs a stop. |

## Behaviour: circuit-breakers and the order rate

trader-risk locks new entries on a bad day. Exits are never blocked. `0` (or
`null`) switches a breaker off.

| key | default | meaning |
| --- | --- | --- |
| `max_consecutive_losses` | 3 | Losing closed trades in a row today (trader/state/trades.json) that lock new entries until tomorrow. |
| `max_daily_drawdown_pct` | 3 | A realised loss today, after charges, of this % of capital locks new entries until tomorrow. |
| `cooloff_after_stop_min` | 15 | Minutes with no new entry after a stop fills. |
| `revenge_window_min` | 15 | Minutes after a loss on a symbol in which a new entry in it is a revenge trade. |
| `revenge_trade` | `"block"` | `"block"` the revenge trade, or `"warn"` only. |

trader-risk also holds new entries to `order_rate_per_sec` (below, under
Market and broker: 3 a second, never above 9), counting the orders placed in
the last second, the same cap the broker adapters' rate limiter uses.

The kill switch (`check.py kill`) is not a config key: it is on for the rest
of the day in `trader/state/kill.json`, and only `check.py unkill` with the
typed word `unkill` lifts it.

## F&O risk

Read by trader-risk when `allow_fno` is true. `null` takes the default shown
(often worked out from your loss limits); `0` switches a limit off.

| key | default | meaning |
| --- | --- | --- |
| `allow_naked_short` | false | Short options not covered by bought ones. Even when true, a naked basket needs a stop rule (`risk_rule` or SL stops). |
| `defined_risk_counts_as_stop` | true | A defined-risk basket (every short covered by a long) needs no other stop: its max loss at expiry is the loss. false: it needs a `risk_rule` too. |
| `require_ban_list` | true | Block a new position in a stock when NSE's F&O ban list for today is not known (`check.py refresh` fetches it). false: a warning. |
| `margin_use_upper_bound` | true | Hold the margin estimate's upper bound (+15% index, +35% stock) against your cash, not the middle. |
| `max_premium_per_trade` | `max_loss_per_trade` | Net option premium paid for one basket, in ₹. |
| `max_premium_per_day` | `max_daily_loss` | Option premium paid in a day, in ₹. |
| `max_lots_per_underlying` | 2 | Lots on one side (CE long, CE short, PE long, PE short, futures) of one underlying, open + new. |
| `max_net_delta_lots` | 2 | The book's net delta per underlying, in lots. A basket that reduces it always passes. |
| `max_net_gamma_lots_1pct` | off | Lots of delta the book gains or loses on a 1% move. |
| `max_net_vega` | `max_daily_loss` ÷ 10 | The book's net vega, ₹ per vol point. |
| `max_theta_paid_per_day` | off | Time decay the book pays a day, in ₹. |
| `max_scenario_loss` | `max_daily_loss` | The book's worst P&L after charges on the scenario grid. |
| `scenario_sigmas` | `[1, 2, 3]` | Spot moves of the grid, in sigmas of each underlying (at-the-money IV over `scenario_horizon_days`), both ways. |
| `scenario_iv_shifts` | `[5, 10]` | IV shifts of the grid, in vol points, both ways. |
| `scenario_horizon_days` | 1 | Sessions the grid looks ahead (time passes too). |
| `span` | `{}` | Overrides of trader-risk's own SPAN-style margin assumptions (used when trader/strategy.json is not this basket), e.g. `{"scan_pct": 9.3, "vol_scan": 4.0, "elm_pct": 2.0, "error_pct": 15}`. |

A basket's own stop rule goes on each of its legs in `trader/orders.json`:
`"risk_rule": {"max_loss": 4000}` (exit or adjust when the basket is down
₹4,000) or `"risk_rule": {"stop_underlying": {"above": 23400, "below": 22700}}`.

## Time (IST)

| key | default | meaning |
| --- | --- | --- |
| `no_entries_before` | `"09:30"` | Skip the first 15 minutes while the opening range forms. |
| `no_entries_after` | `"14:45"` | No new trades late in the session. |
| `flat_by` | `"15:10"` | Close intraday positions before the broker's MIS auto square-off. Never later than the safe time for the stock and day (below); a later one is pulled in, with a note. `"auto"` takes the safe time. |
| `broker_profile` | `""` | `zerodha`, `upstox`, `dhan`, `angel`, `groww` or `custom`: your broker's MIS square-off times (see [data sources](data-sources.md#broker-mis-square-off)). Empty: only the exchange's timings bind. It also picks the brokerage for paper fills ([options.md](options.md#paper-fo-the-basket-margin-mtm-and-expiry)). |
| `square_off` | none | For `custom`: `{"EQ": "15:20", "EQ_FNO": "15:10", "DERIV": "15:25"}`, your broker's square-off for cash stocks, F&O stocks and F&O contracts. |
| `square_off_margin_min` | 5 | Minutes between the flat-by and the earlier of the broker's square-off and the end of continuous trading. |
| `fno_stocks` | `[]` | Your own list of F&O stocks. Empty: NSE's list, cached in `trader/fno_stocks.json` (refreshed once a day by the scanner, the entry timer, the exit planner's ATR fetch and the time machine). With no list at all every stock is treated as an F&O stock: the earlier close is the safe side to be wrong on. |
| `extra_holidays` | `[]` | `"YYYY-MM-DD"` dates to treat as closed (add next year's NSE list here until the tools ship it). |
| `special_sessions` | `[]` | e.g. Muhurat trading: `{"date": "2026-11-08", "open": "18:00", "close": "19:00"}` (use the timings NSE announces). |

### The closing auction and the safe flat-by

From 2026-08-03 a stock in NSE's F&O list stops continuous trading at 15:15
and gets its closing price in the Closing Auction Session (to 15:35); from
2026-12-07 continuous trading runs to 15:30 and the auction to 15:40. Other
stocks keep 09:15-15:30. Every timing, with its NSE or SEBI source, is in
[data sources](data-sources.md#market-rules-dated).

For each stock and day the tools work out:

- **safe flat-by** = the earlier of continuous trading's end and the
  `broker_profile` square-off, less `square_off_margin_min`. Your `flat_by`
  is used when it is earlier.
- **last entry** = `no_entries_after`, but at least 15 minutes before the safe
  flat-by.

| stock, day | no profile | `zerodha` | `groww` |
| --- | --- | --- | --- |
| F&O stock, 2026-10-29 | flat by 15:10, entries to 14:45 | 15:07, 14:45 | 15:00, 14:45 |
| other stock, 2026-10-29 | 15:10, 14:45 | 15:10, 14:45 | 15:10, 14:45 |
| F&O stock, 2026-12-08, `flat_by` "15:25", `no_entries_after` "15:05" | 15:25, 15:05 | 15:07, 14:52 | 15:00, 14:45 |

## Market and broker

| key | default | meaning |
| --- | --- | --- |
| `exchange` | `"NSE"` | Only NSE for now. |
| `product` | `"MIS"` | `MIS` intraday or `CNC` delivery. Changes STT and stamp duty. |
| `tick_size` | `"auto"` | NSE tick by price band (see [data sources](data-sources.md#tick-sizes)); a number overrides. Stops round down. |
| `broker` | `"paper"` | `paper`, or a live broker: `upstox`, `kite` (`zerodha` works too) or `dhan`. A live broker needs `paper` false. See [brokers](brokers.md). |
| `paper` | true | Keep it true until you have tested on paper. false (with a live `broker`) makes `confirm` only record your yes; `ticket.py place` sends the order after every gate. |
| `live_enabled` | `{}` | Per broker, e.g. `{"upstox": true}`: the last switch for live orders at that broker. Missing or false: no live order there, whatever else is set. |
| `static_ip` | `""` | The static IP you registered with your broker (SEBI: API orders only from it): one IP, a list, or `{"kite": "..."}` per broker. `ticket.py preflight` compares it with this machine's public IP. |
| `order_rate_per_sec` | 3 | Most live orders a second this tool sends; never above 9 (more than 10 a second needs exchange algo registration). |
| `shadow` | false | With a live broker, record a paper twin of every order `ticket.py place` sends (`trader/state/shadow.json`); `ticket.py shadow-report` compares them. The live order is not changed. |
| `paper_spread_share` | 0.5 | Paper fills at the bid/ask mid plus this share of the spread against you (0.5: the far side; 0: the mid). |
| `paper_slippage_bps` | 10 | With no two-sided quote, the last price moves this many basis points against you. |
| `market_protection_pct` | 2 | A paper MARKET order is sent as a LIMIT this far from the last price, as brokers do for API orders. |
| `paper_partial_fills`, `paper_partial_chance`, `paper_seed` | false, 0.5, 7 | Seeded part fills for single-leg paper tickets (a basket never fills in part). |
| `blocked` | `[]` | Symbols you never trade. |
| `allow_short` | false | Intraday short selling. |
| `allow_fno` | false | Futures and options on NFO, checked by the F&O risk rules above. |
| `min_price`, `min_turnover` | 50, 100000000 | Scanner liquidity filter: last close in ₹ and median daily turnover in ₹ (₹10 crore). |
| `insider_watch_clients` | `[]` | Names to highlight in bulk/block deals. |
| `risk_free_rate` | 0.0557 | Rate for option pricing (decimal; `5.57` is read as 5.57%). Default: the 91-day T-bill cut-off yield at the 08-Oct-2026 auction. Under Black-76 it only discounts, so small errors barely move IV. |

## Language

| key | default | meaning |
| --- | --- | --- |
| `lang` | "en" | The language of the plain-words output: `en`, or `hi-en` for Hinglish (Hindi in Roman script, trading words in English; see [hinglish.md](hinglish.md)). A command's `--lang` wins over it; JSON output stays English. |

## Charges

`include_charges: true` makes sizing and the risk check count the round-trip
cost of a trade, so "max loss ₹1,000" really means ₹1,000 after charges.

```json
"charges": {
  "brokerage_pct": 0.03, "brokerage_cap": 20,
  "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
  "exchange_txn_pct": 0.00307, "sebi_per_crore": 10,
  "stamp_buy_pct": 0.003, "stamp_delivery_pct": 0.015,
  "gst_pct": 18
}
```

These defaults model a typical discount broker for equity intraday. **Rates
change and differ by broker. Check your broker's charges page and update
them.**

| charge | intraday (MIS) | delivery (CNC) |
| --- | --- | --- |
| Brokerage | lower of `brokerage_pct` % and ₹`brokerage_cap` per executed order | as configured |
| STT | `stt_sell_pct` % on the sell side | `stt_delivery_pct` % on both sides |
| Exchange transaction | `exchange_txn_pct` % both sides | same |
| SEBI fee | ₹`sebi_per_crore` per crore both sides | same |
| Stamp duty | `stamp_buy_pct` % on the buy side | `stamp_delivery_pct` % on the buy side |
| GST | `gst_pct` % on brokerage + exchange + SEBI | same |

NSE's equity transaction charge plus the IPFT contribution comes to ₹307 a
crore (0.00307%) on each side ([NSE/FA/73061](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf)).
The default `exchange_txn_pct` is that 0.00307%. A config that still says
0.00297 (the rate without the ₹10 a crore IPFT part) under-counts by ₹10 a crore.

### Futures and options

The same helper prices F&O trades with `segment` `"OPT"` (prices are
premiums) or `"FUT"`, using NSE's dated rates for the trade date: STT on the
sell side of 0.15% of the premium for options and 0.05% for futures from
2026-04-01, 0.15% of the intrinsic value on an exercised option, and the
exchange, SEBI, stamp and GST rates in
[data sources](data-sources.md#fo-charges). Override any of them:

```json
"fno_charges": {
  "OPT": {"brokerage_flat": 20},
  "FUT": {"brokerage_pct": 0.03, "brokerage_cap": 20}
}
```
