---
name: trader-risk
description: >-
  Risk Manager for NSE trades. Checks every order against your limits and
  stops anything over them before it can reach the broker. For each trade: a
  stop is required, the loss at the stop PLUS estimated charges (brokerage,
  STT, exchange, SEBI, stamp duty, GST) must fit your max loss per trade
  (measured from the higher of the plan's entry and a fresh price),
  reward:risk must meet your minimum, the day's total risk must fit your
  daily loss limit, no more than your trades per day, and no new entries
  outside your IST entry window or when NSE is closed (weekends, 2026 NSE
  holidays, your extra holidays, Muhurat-style special sessions). Also:
  blocked symbols, NSE ticker format, order value, position cap, cash buffer,
  no broker leverage, no shorts or F&O unless allowed, malformed SL-M / LIMIT
  legs and tick-size errors, duplicates and a daily order limit. Getting out
  is never blocked by a day limit. Prints PASS or BLOCK with the reason and
  records the verdicts so trader-broker only shows an order that passed. Use
  when the user says "check my order", "is this safe", "risk check", "does
  this break my rules", "can I take this trade", "how much have I risked
  today", "daily loss limit", "is the market open", or before /trader-broker,
  always.
argument-hint: "[--now \"YYYY-MM-DD HH:MM\"] [--json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget, or by trader-broker's flatten, passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. Most people who day trade lose money. These limits
keep each loss and each day's losses small. They do not make a trade win.

All amounts are rupees, all times IST (UTC+05:30, no daylight saving).
`<skill dir>` below is this skill's folder in your agent's skills folder
(where this SKILL.md lives).

```bash
python3 "<skill dir>/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "<skill dir>/check.py" --json
python3 "<skill dir>/check.py" --no-write                # look without recording
python3 "<skill dir>/check.py" --now "2026-10-29 09:40"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits (trader-budget's `size.py init`); missing keys use the defaults |
| `trader/portfolio.json` | `{"cash", "positions": {SYM: {"qty", "avg_price"}}}`; if missing, a fresh account with the capital |
| `trader/prices.json` | a fresh price raises the entry a MARKET order is measured from; also values holdings |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss, orders per day |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a BUY, or a SELL that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, F&O and short selling
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol; side not BUY or SELL; qty not a whole number above 0; order_type not MARKET, LIMIT, SL or SL-M; validity not DAY or IOC; a LIMIT with no price; a `stop_loss` that is not SL-M / SL with a trigger; a `target` that is not a LIMIT with a price |
| tick | a price, trigger or leg that is not on the tick. `tick_size` is `"auto"` (by NSE price band): under ₹250 any paisa (₹0.01), else multiples of ₹0.05, permissive on purpose so a valid price is never blocked. A number in the config (`"tick_size": 0.10`) overrides it |
| exchange, product | not NSE; product not MIS or CNC |
| fno | futures or options (`NIFTY26NOVFUT`, `NIFTY26NOV25000CE`, exchange NFO, product NRML) while `allow_fno` is false |
| ticker | not an NSE symbol: `^[A-Z0-9][A-Z0-9&-]{0,19}$` (M&M, BAJAJ-AUTO, 3MINDIA are fine; RELIANCE.NS is not) |
| short | a SELL of more than you hold while `allow_short` is false; in CNC always (delivery shorts are not possible in India: undelivered shares go to auction) |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_value` or over `max_order_value` |
| position | (entries) the stock would be over `max_position_pct` of your capital |
| cash | (entries) the buy would leave less than the cash buffer, or need broker leverage (MIS margin) |
| stop | (entries) no stop while `require_stop` is true (the SL-M leg, or the plan's stop); a stop on the wrong side of the entry |
| target | (entries) a target on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) + round-trip charges at the stop over `max_loss_per_trade` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses of today's entries plus this one over `max_daily_loss` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45) IST, later than 15 minutes before the stock's safe flat-by, during an F&O stock's closing auction, or NSE is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT order (it
  cannot fill above it). For a MARKET order, the HIGHER of the plan's entry
  and a fresh price from `trader/prices.json` (under 15 minutes old). A plan
  written at 09:40 cannot understate the risk at 10:25 after the price ran
  up. An older price is not used, and it says so.
- **Charges** use the same `round_trip_charges` as trader-budget (buy at the
  entry, sell at the stop), with the rates in `"charges"`. `"include_charges":
  false` leaves them out.
- **Daily loss** counts the full max loss (charges included) of every entry
  placed today, win or lose, as trader-broker logged it.
- **The market**: NSE's normal session is 09:15 to 15:30 IST, Monday to
  Friday. The pre-open (09:00 to 09:08, 09:10 from 7 Sep 2026) is not used.
- **F&O stocks**: a stock in NSE's F&O list (`fno_stocks` in the config, else
  trader/fno_stocks.json from the scanner or the entry timer; with neither,
  every stock counts as one) stops continuous trading at 15:15 from 3 Aug
  2026 (15:30 from 7 Dec 2026) for the Closing Auction Session. No new entry
  passes during the auction, nor later than 15 minutes before the safe
  flat-by: 5 minutes before the earlier of continuous trading's end and your
  `broker_profile`'s MIS square-off. The block says which. This holds even
  with the entry window switched off. The 2026 NSE trading
  holidays are built in; add any others to `"extra_holidays"`
  (`["2026-12-31"]`). A day in `"special_sessions"` (`[{"date": "2026-11-08",
  "open": "18:00", "close": "19:00", "name": "Muhurat trading"}]`, timing as
  NSE announces it) is open in those hours only, even on a weekend or
  holiday; your entry window does not apply then, so it warns. For a year
  with no holiday table yet (2027), it says so and treats weekdays as open.
  Set both `no_entries_*` to null to switch the entry window off.

Warnings do not block, but say them to the user: a LIVE account, a stop that
lives only in the plan and not as an order leg, a price too old to use, a
special session, a year with no holiday table.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the loss at the
   stop with charges, reward:risk, and how much of today's limits it uses.
   For a BLOCK, give the reason and the fix (fewer shares, a closer stop from
   trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: RELIANCE, Thursday 2026-10-29

trader-budget's order at 09:40 IST on a fresh ₹1,00,000 paper account with
the example config:

```
PASS  #1 BUY 42 RELIANCE (MIS), entry
          entry 1174.30 (the limit price), stop 1163.30, target 1196.30: risk 11.00 a share
          loss at the stop ₹462.00 + ₹52.00 charges = ₹514.00 (limit ₹1,000.00), reward:risk 2.00 (min 1.5)
          today before this: 0 of 3 trades, ₹0.00 of ₹3,000.00 daily loss

1 passed, 0 blocked at 2026-10-29 09:40 IST. Cash after the passed orders: ₹50,679.40 (paper account).
```

With the position limits opened up, 83 shares pass (₹913.00 + ₹81.28 =
₹994.28) and 84 are blocked:

```
        - loss: 84 shares x 11.00 risk a share = ₹924.00 + ₹81.71 charges = ₹1,005.71 at the stop, over your ₹1,000.00 max loss per trade
```

With the same open limits, a MARKET entry of 70 shares checked at 10:25
with the real 10:20 price (1177.30) is blocked twice: 70 x 14.00 plus charges is over ₹1,000, and
reward:risk falls to 1.36. At 09:20 it is blocked by the window (no new
entries before 09:30), on Tuesday 2026-11-10 by the Diwali-Balipratipada
holiday. The 15:10 SELL that closes the 42 shares passes even after the
14:45 entry cutoff and with the day's limits used up: it is an exit.

## Rules

- A PASS only means the order may be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its trigger in a fast market or a gap.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
