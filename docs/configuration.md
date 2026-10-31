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
| `flat_by` | `"15:10"` | Close intraday positions before the broker's MIS auto square-off (typically 15:15 to 15:25). |
| `extra_holidays` | `[]` | `"YYYY-MM-DD"` dates to treat as closed (add next year's NSE list here until the tools ship it). |
| `special_sessions` | `[]` | e.g. Muhurat trading: `{"date": "2026-11-08", "open": "18:00", "close": "19:00"}` (use the timings NSE announces). |

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
| `risk_free_rate` | 0.0557 | Rate for option pricing (decimal; `5.57` is read as 5.57%). Default: the 91-day T-bill cut-off yield at the 08-Oct-2026 auction. Under Black-76 it only discounts, so small errors barely move IV. |

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
