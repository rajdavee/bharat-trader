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

## Time (IST)

| key | default | meaning |
| --- | --- | --- |
| `no_entries_before` | `"09:30"` | Skip the first 15 minutes while the opening range forms. |
| `no_entries_after` | `"14:45"` | No new trades late in the session. |
| `flat_by` | `"15:10"` | Close intraday positions before the broker's MIS auto square-off. Never later than the safe time for the stock and day (below); a later one is pulled in, with a note. `"auto"` takes the safe time. |
| `broker_profile` | `""` | `zerodha`, `upstox`, `dhan`, `angel`, `groww` or `custom`: your broker's MIS square-off times (see [data sources](data-sources.md#broker-mis-square-off)). Empty: only the exchange's timings bind. |
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
| `exchange` | `"NSE"` | Only NSE in v0.1. |
| `product` | `"MIS"` | `MIS` intraday or `CNC` delivery. Changes STT and stamp duty. |
| `tick_size` | `"auto"` | NSE tick by price band (see [data sources](data-sources.md#tick-sizes)); a number overrides. Stops round down. |
| `broker` | `"paper"` | Only the paper broker in v0.1. |
| `paper` | true | Keep it true until a live adapter exists and you have tested on paper. |
| `blocked` | `[]` | Symbols you never trade. |
| `allow_short` | false | Intraday short selling. |
| `allow_fno` | false | Futures and options symbols. |
| `min_price`, `min_turnover` | 50, 100000000 | Scanner liquidity filter: last close in ₹ and median daily turnover in ₹ (₹10 crore). |
| `insider_watch_clients` | `[]` | Names to highlight in bulk/block deals. |

## Charges

`include_charges: true` makes sizing and the risk check count the round-trip
cost of a trade, so "max loss ₹1,000" really means ₹1,000 after charges.

```json
"charges": {
  "brokerage_pct": 0.03, "brokerage_cap": 20,
  "stt_sell_pct": 0.025, "stt_delivery_pct": 0.1,
  "exchange_txn_pct": 0.00297, "sebi_per_crore": 10,
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
The default `exchange_txn_pct` of 0.00297% is the transaction charge without
the ₹10 a crore IPFT part; set 0.00307 to match a contract note to the paisa.

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
