---
name: trader-risk
description: >-
  Risk Manager for NSE trades, cash and F&O. Checks every order against your
  limits and stops anything over them before it can reach the broker. For
  each trade: a stop is required, the loss at the stop PLUS estimated charges
  (brokerage, STT, exchange, SEBI, stamp duty, GST) must fit your max loss per
  trade, reward:risk must meet your minimum, the day's total risk must fit
  your daily loss limit, no more than your trades per day, and no new entries
  outside your IST entry window or when NSE is closed. F&O (when allowed): a
  multi-leg basket is checked as ONE position (a hedged iron condor is not
  four naked legs): lot multiples from NSE's fo_mktlots, the NSE F&O ban list
  (fo_secban), expiry-day margin (2% more ELM, no calendar benefit), premium
  budget, lots per underlying, SPAN-style margin against your cash, naked
  shorts off unless allowed and stopped, portfolio net delta / gamma / vega /
  theta limits and a spot +/-1/2/3 sigma x IV +/-5/10 vol scenario grid after
  charges. Behaviour: circuit-breakers after a losing streak or a daily
  drawdown, cooling-off after a stop, revenge-trade flag, an order-rate cap
  under SEBI's 10 a second, and a kill switch. Getting out is never blocked.
  Use when the user says "check my order", "is this safe", "risk check",
  "can I take this trade", "how much have I risked today", "daily loss
  limit", "is the market open", "is this stock in the F&O ban", "how much
  margin for this iron condor", "what if Nifty falls 2%", "my Greeks",
  "portfolio delta", "stop me trading today", "kill switch", "I am on tilt",
  or before /trader-broker, always.
argument-hint: "[kill|unkill|scenarios|refresh] [--now \"YYYY-MM-DD HH:MM\"] [--json]"
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
python3 "<skill dir>/check.py" --offline                 # never ask NSE (ban list, lot sizes)
python3 "<skill dir>/check.py" scenarios [--json]        # the F&O book: Greeks and the scenario grid (read only)
python3 "<skill dir>/check.py" refresh                   # fetch NSE's F&O ban list and lot sizes now
python3 "<skill dir>/check.py" kill --reason "on tilt"   # no new entries today; --flatten writes exit tickets
python3 "<skill dir>/check.py" unkill                    # lift it: asks you to type the word unkill
python3 "<skill dir>/check.py" rate-gate --record        # for a broker adapter: may one more order go now?
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
| `trader/state/trades.json` | fills with their realised P&L (trader-broker): the circuit-breakers |
| `trader/state/kill.json` | the kill switch: on for one day, with its reason and a log |
| `trader/strategy.json`, `trader/options.json` | (F&O) trader-strategy's basket with its margin estimate, trader-options' chain: forwards and IVs |
| `trader/fo_mktlots.csv`, `trader/fo_secban.json` | (F&O) NSE's lot sizes and ban list, fetched at most once a day and cached |

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
| kill | (entries) the kill switch is on today |
| breaker | (entries) `max_consecutive_losses` (3) losing trades in a row today, or a realised loss today of `max_daily_drawdown_pct` (3%) of capital: locked until tomorrow |
| cooling-off | (entries) a stop filled less than `cooloff_after_stop_min` (15) minutes ago |
| revenge | (entries) re-entering a symbol within `revenge_window_min` (15) minutes of a loss on it; `"revenge_trade": "warn"` makes it a warning |
| rate | (entries) `order_rate_per_sec` (3) orders already went in the last second; never above 9, whatever the config says |
| api | (entries) with a live broker (`"paper": false`) whose adapter has no market protection (Dhan): a MARKET or SL-M order, or an SL-M stop leg; use LIMIT, or SL with a limit price. Kite and Upstox send MARKET / SL-M with the broker's automatic protection |

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

## F&O: one basket is one position

With `"allow_fno": true`, F&O orders (exchange NFO; trader-strategy's
`symbol`, `expiry`, `strike`, `option_type`, or a trading symbol like
`NIFTY26NOV23000CE` / `NIFTY26N0323000CE`) are grouped: every F&O order in an
orders file with `"basket": true` (trader-strategy writes it), or the orders
sharing a `basket_id`, are ONE position. They pass or block together: a basket
is placed whole or not at all. Checked one by one, the short legs of a hedged
iron condor are naked shorts and block; as one basket the same four legs are
defined risk and pass.

| check | blocks when |
| --- | --- |
| contract, expiry | not an NFO contract, or it has expired (15:30 IST on its expiry day) |
| lot | qty not a multiple of the lot for that expiry month in NSE's `fo_mktlots.csv`; an order's own `lot_size` that disagrees with NSE |
| ban | a stock underlying in NSE's F&O ban list for today (open interest over 95% of its market-wide limit): no new positions, closing is allowed. With no list known for today it blocks too (`require_ban_list`); `check.py refresh` fetches it |
| stop | not defined risk and no stop rule. Defined risk (every short option covered by a long of the same type, same or later expiry) counts as the stop while `defined_risk_counts_as_stop` is true. Otherwise every leg needs `"risk_rule": {"max_loss": 4000}` or `{"stop_underlying": {"above": 23400, "below": 22700}}`, or an SL / SL-M `stop_loss` on every short leg |
| naked | short options not covered by bought ones while `allow_naked_short` is false (the default). Allowed, they still need a stop rule |
| loss | the basket's loss over `max_loss_per_trade`, or with today's over `max_daily_loss`: the max loss at expiry with charges (defined risk), else the loss at the stop rule |
| premium | net premium paid (paid minus received) over `max_premium_per_trade`, or with today's options bought over `max_premium_per_day` |
| lots | open + this batch over `max_lots_per_underlying` lots on one side (CE short, PE long, futures...) of one underlying |
| margin | the margin needed (trader-strategy's SPAN-style `margin.high` when `trader/strategy.json` is this basket, else the same method here; the upper bound while `margin_use_upper_bound`) over the cash left after `cash_buffer_pct` and `margin_blocked` |
| greeks | the book's net delta (lots) over `max_net_delta_lots`, gamma over `max_net_gamma_lots_1pct`, net vega over `max_net_vega` (₹ per vol point) or theta paid over `max_theta_paid_per_day`, unless this basket reduces it: a hedge always passes |
| scenario | the book's worst P&L after charges on the grid over `max_scenario_loss`, unless this basket makes it better; the reason names the scenario |

**Expiry day.** On the day a contract expires, SEBI adds 2% of notional as
extra ELM on short index options expiring that day, and a basket with the
expiring contract gets no calendar-spread benefit (each expiry is margined on
its own). Both go into the margin figure, and a warning says so.

**The book.** Open F&O positions (portfolio.json `positions` with exchange
NFO or an expiry, or `fno_positions`, signed qty) plus every basket passed
earlier in the batch plus this one. Each leg is valued by Black-76 on its
forward (the chain's, moved with a fresh spot) and IV (the chain's for that
strike, implied from the premium, or the at-the-money IV). Net Greeks per
underlying: delta in lots and ₹ per 1% move, gamma as lots of delta per 1%
move, vega ₹ per vol point, theta ₹ a day. The scenario grid moves every
underlying k of its own sigmas (at-the-money IV x sqrt(1/252), one session)
for k = -3..+3, times IV -10, -5, 0, +5, +10 vol points, one day passing; P&L
after charges (a proposal pays its entry and exit, an open position its
exit). `check.py scenarios` prints it for open positions alone and with the
proposals.

Example, Monday 2026-11-02 10:00, the NIFTY 03-Nov iron condor
(22700/22800 PE, 23200/23300 CE, 1 lot) from trader-strategy on the chain of
30-Oct-2026, ₹5,00,000 capital:

```
PASS  #1 BUY 65 NIFTY 22700 PE 2026-11-03 (NRML), entry
          basket of 4 legs on NIFTY, defined risk: can lose ₹4,298.91 (the most it can lose at expiry, charges included) (limit ₹5,000.00)
          margin about ₹77,520.08 (the upper bound (+15%), trader-strategy (trader/strategy.json, 2026-10-30T15:40:00)), ₹4,50,000.00 free; premium paid ₹3,428.75, received ₹5,794.75
          portfolio after this basket: NIFTY delta -0.03 lots, vega -₹189.75, theta ₹925.11/day; worst scenario spot +3 sigma (NIFTY +2.29%) with IV +5 vol: -₹4,342.24 after charges

  scenarios over 1 session(s), P&L after charges (sigma: NIFTY 0.76%):
    spot \ IV                   -10 vol       -5 vol       +0 vol       +5 vol      +10 vol
    -2 sigma (-1.52%)         -4,236.74    -3,730.20    -3,149.59    -2,753.56    -2,441.48
    +0 sigma (+0.00%)          2,258.93     2,164.53     2,156.78     2,052.29     1,776.36
    +3 sigma (+2.29%)         -4,329.29    -4,329.29    -4,329.15    -4,342.24    -4,305.84
```

The same NIFTY short strangle on Tuesday (expiry day) blocks: `naked` (short
legs not covered), `stop` (no stop rule), and its margin carries ₹59,857.56 of
expiry-day ELM.

**Adjustments.** A trader-broker `ticket.py adjust` ticket (roll a leg, close a
leg, add a hedge, close all) changes a strategy that is already open, so it is
checked on the position it leaves, not on its own legs. The open legs come
from portfolio.json `fno_positions` with the ticket's strategy id (the
`"adjust": {"strategy_id": ...}` block, or a `strategy_id` on a leg; a leg tagged `bt-adjust` makes it an adjustment too). If the
ticket has no id, or no open position has it, the check takes every open leg
of the same underlying and expiry and says so in a warning. Each ticket leg
first closes the open leg of the same contract, and whatever is left over
opens a new leg. Then:

- **defined risk or naked** is decided on the position the ticket leaves.
  Rolling the covered short call of an iron condor passes. A roll that leaves
  a short uncovered blocks with `naked` unless `allow_naked_short` is on, and
  then it needs a `risk_rule`. One example is rolling the short call to a
  later expiry than its bought call.
- **loss** is that position's max loss at expiry from the open legs' last
  prices, plus the cost of the closing orders now. It is checked against
  `max_loss_per_trade`. Only what the adjustment adds to the max loss counts
  toward `max_daily_loss`. A roll is not a new trade for `max_trades_per_day`.
- **margin** is the change: the margin of the position after the ticket minus
  the margin of the open legs now, against the free cash.
- **greeks** and **scenario** use the book with the strategy's open legs
  replaced by what the adjustment leaves.
- **exits are never blocked.** A ticket that only closes legs is an exit
  (close all, or buying back a short), even with the kill switch on. There is
  one exception: closing a bought leg that leaves a short uncovered blocks
  with `naked`, and the reason names the short.

Rolling the short 23200 CE of that condor (filled on paper) up to 23250, at
11:00 with ₹2,00,000 capital:

```
PASS  #1 BUY 65 NIFTY 23200 CE 2026-11-03 (NRML), adjust
          the position after this roll of S-20261102-1 on NIFTY, defined risk: can lose ₹4,991.19 (the most it can lose at expiry, charges included), ₹4,185.97 before it (limit ₹6,000.00)
          margin about ₹79,669.61 after it, ₹79,322.52 now: ₹347.09 more (the upper bound (+15%), trader-risk (same SPAN-style method as trader-strategy)), ₹1,18,275.67 free; premium paid ₹2,915.25, received ₹2,161.25
          roll of S-20261102-1: checked with its 4 open legs in portfolio.json; after it: NIFTY 22700 PE 2026-11-03 +65, NIFTY 23300 CE 2026-11-03 +65, NIFTY 22800 PE 2026-11-03 -65, NIFTY 23250 CE 2026-11-03 -65
```

Closing its bought 23300 CE instead blocks:

```
        - naked: after this close of S-20261102-1, NIFTY 23200 CE 2026-11-03 would be short with no bought leg of the strategy to cover it (a loss without limit); naked shorts are off ("allow_naked_short": false)
```

## Behaviour: circuit-breakers, the kill switch, the order rate

SEBI's study of F&O traders found that the more often people trade, the more
they lose. These locks slow you down after losses. They apply to new entries
only: an exit always passes, with the lock as a warning.

- **Losing streak**: `max_consecutive_losses` losing closed trades in a row
  today (trader/state/trades.json) locks entries until tomorrow.
- **Daily drawdown**: a realised loss today, after charges, of
  `max_daily_drawdown_pct` of capital locks entries until tomorrow.
- **Cooling-off**: no new entry for `cooloff_after_stop_min` minutes after a
  stop fills.
- **Revenge trade**: a new entry in a symbol that lost less than
  `revenge_window_min` minutes ago.
- **Trades a day**: `max_trades_per_day` (a basket counts once).
- **Order rate**: `order_rate_per_sec` (default 3), never above 9, so this
  pack stays under SEBI's 10 orders a second per exchange (above it an algo
  must be registered). `check.py rate-gate` answers "may one more go now?"
  for a broker adapter, from `trader/state/rate.json`.
- **Live broker API rules**: SEBI wants market protection on every API
  MARKET order. Kite and Upstox add the broker's automatic protection; with
  Dhan, use LIMIT orders and SL stops.

**Kill switch.** `check.py kill --reason "..."` blocks every new entry for
the rest of the day (kill.json) and revokes any entry PASS already recorded
in trader/state/risk.json, so a verdict from before the kill cannot be
confirmed after it. Exits still pass. `--flatten` also writes exit tickets
to trader/orders.json: trader-broker's flatten for cash shares, then one
closing ticket per F&O position, short legs first (so a hedge never goes
before the leg it covers). The tickets still go through check.py and the
user's typed yes, one by one. `check.py unkill` lifts it only when the user
types the word `unkill` (`--yes-text unkill` for a terminal-less run). The
next day starts clear. Never run `kill`, `unkill` or `--flatten` for the user
without being asked, and never type the unkill word yourself.

Warnings do not block, but say them to the user: a LIVE account, a stop that
lives only in the plan and not as an order leg, a price too old to use, a
special session, a year with no holiday table, an F&O basket priced from an
old chain, a lot size taken from the order because NSE's file was missing, an
expiry day.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the loss at the
   stop with charges, reward:risk, and how much of today's limits it uses.
   For a BLOCK, give the reason and the fix (fewer shares, a closer stop from
   trader-exit, wait for the window, a hedged structure instead of naked
   shorts, fewer lots, or skip the trade). For F&O, also say the margin, the
   worst scenario and that both are estimates. Never change a limit yourself,
   and never argue a circuit-breaker or the kill switch away: they are the
   user's own rule for a bad day.
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
loosens a limit, never blocks an order that only gets the user out, never
lifts the kill switch without the user's typed word, and never treats a PASS
as the user's yes. The F&O margin is an estimate (SPAN-style, +/-15% for
index, 35% for stocks): the broker's basket margin is the one that counts.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
