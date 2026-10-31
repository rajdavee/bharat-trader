---
name: trader-budget
description: >-
  Budget Manager for NSE trades in rupees. Sets up your rules once (size.py
  init: capital, risk per trade in percent, trades per day, MIS intraday or
  CNC delivery; every other limit derived and explained in plain words), then
  sizes every trade to them. Reads the plan from trader-exit (entry, stop,
  target), finds the whole shares that fit four limits (max loss per trade
  INCLUDING brokerage, STT, exchange, SEBI, stamp duty and GST at the stop;
  position cap; max order value; cash after your buffer), says which limit
  decided it, and writes ONE order to trader/orders.json: a LIMIT entry, an
  SL-M stop-loss and a LIMIT target on NSE. A stop too far away for your max
  loss is a plain refusal, not a smaller order. Nothing is placed. Use when
  the user says "how many shares", "position size", "size this trade", "how
  much quantity should I buy", "I have 1 lakh", "I only want to risk ₹1,000 a
  trade", "set my rules", "set up my config", "1% risk per trade", "keep 10%
  in cash", "MIS or CNC", or after /trader-exit writes a plan.
argument-hint: "[init | --write | --market | --limit PRICE]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1174.30, stop 1163.30")
into an exact order ("buy 42 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It writes
a proposal only. trader-risk checks it and trader-broker asks the user yes or
no. Day trading loses most people money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

All amounts are rupees, all times IST (UTC+05:30), the market is the NSE
cash segment. `<skill dir>` below is this skill's folder in your agent's
skills folder (where this SKILL.md lives).

```bash
python3 "<skill dir>/size.py" init                                # asks, in a terminal
python3 "<skill dir>/size.py" init --capital 100000 --risk-pct 1 --max-trades 3 --product MIS --yes
python3 "<skill dir>/size.py" init --capital 100000 --broker-profile zerodha --yes   # your broker's MIS square-off times
python3 "<skill dir>/size.py"                                     # trader/plan.json + trader/config.json
python3 "<skill dir>/size.py" --write                             # write trader/orders.json
python3 "<skill dir>/size.py" --market --write                    # a MARKET entry instead of LIMIT
python3 "<skill dir>/size.py" --limit 1175.00 --json              # a LIMIT at 1175.00
python3 "<skill dir>/size.py" --targets "<skill dir>/targets.example.json"   # weights mode, below
```

`--now "2026-10-29 09:40"` pins the clock (IST). A plan for another day is
refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules; `size.py init` writes it (or copy `config.example.json` from this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, entry time, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: for a MARKET entry, a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: `{"cash", "positions"}`; its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once (`init`)

If `trader/config.json` does not exist, run `size.py init`. In a terminal it
asks four questions; the agent can also pass them as flags with `--yes`
after asking the user in the chat:

- **capital**: how much money this is for, in rupees. Only money they can afford to lose.
- **risk per trade** in percent (default 1): the most one trade may lose at its stop, charges included.
- **max trades per day** (default 3).
- **product**: MIS (intraday, squared off the same day) or CNC (delivery).

Everything else is derived and printed in plain words:

| key | from | 1 lakh, 1%, 3 trades |
| --- | --- | --- |
| `max_loss_per_trade` | capital x risk % | ₹1,000 |
| `max_daily_loss` | 3 x the max loss per trade | ₹3,000 |
| `max_order_value` | capital x `max_position_pct` (50) | ₹50,000 |
| `max_orders_per_day` | the larger of 10 and 3 x trades | 10 |
| `cash_buffer_pct`, `min_order_value`, `min_reward_risk` | defaults | 10%, ₹500, 1.5 |
| `no_entries_before` / `no_entries_after` / `flat_by` | defaults, IST | 09:30 / 14:45 / 15:10 |
| `broker_profile` | `--broker-profile` (zerodha, upstox, dhan, angel, groww; empty by default) | "" |

With a `broker_profile`, the session tools flat by 5 minutes before that
broker's MIS square-off when it is earlier than `flat_by` (Groww squares off
F&O stocks at 15:05, so 15:00). An F&O stock's continuous trading also ends
early for the Closing Auction Session (15:15 from 3 Aug 2026, 15:30 from 7
Dec 2026). See docs/configuration.md for `custom` times, `fno_stocks` and
`square_off_margin_min`.

The charges helper here also prices futures and options (`segment` "FUT" or
"OPT") with NSE's dated rates, including the 2026 STT (0.05% futures, 0.15%
of the option premium, 0.15% of the intrinsic value on exercise). Sizing in
this pack is still cash equities only.

`init` never overwrites an existing config: it refuses unless `--force` is
given. It refuses a risk above 5% a trade. Keep `"paper": true` until the
user has watched it work.

## Step 2: size the trade

```bash
python3 "<skill dir>/size.py" --write
```

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares where qty x (entry minus stop) + the round-trip charges of a stop-out stay inside `max_loss_per_trade` |
| position cap | capital x `max_position_pct` / entry |
| max order | `max_order_value` / entry |
| cash | (cash minus the cash buffer) / entry; cash is `trader/portfolio.json`'s, else the capital |

The entry it sizes at: by default a LIMIT at the plan's entry. With
`--limit PRICE`, that price. With `--market`, the plan's entry, or the price
in `trader/prices.json` if it is fresh (under 15 minutes old) and higher. A
price that ran up since the plan means a bigger loss at the same stop. A
lower price never shrinks the risk. Prices are rounded to the tick: the stop
down, the entry and target to the nearest tick. `tick_size` is `"auto"` (by
NSE price band) by default: NSE sets each stock's tick from its price band, so
it rounds to the coarsest tick that band can have (under ₹1,000: ₹0.05; under
₹5,000: ₹0.10; under ₹10,000: ₹0.50; under ₹20,000: ₹1.00; above: ₹5.00),
which is a valid price on any finer grid too. RELIANCE at 1174.30 is on the
₹0.10 grid. To override, put a number in the config (`"tick_size": 0.05`).

It refuses, and writes no order, when: even one share breaks the max loss
(the stop is too far away; it never shrinks the stop to fit), one share is
over another limit, the order is under the minimum, reward:risk is under
`min_reward_risk`, the stop is not below the entry, the target is not above
it (the move already happened), the plan is for another day, the symbol is
blocked, or the plan is a short. Say the reason to the user in one line.

## Charges

`round_trip_charges(entry, exit, qty, cfg)` estimates one buy and one sell,
each part rounded half up to the paisa:

| part | MIS (intraday) | CNC (delivery) |
| --- | --- | --- |
| brokerage | min(0.03% of each order, ₹20) on the buy and on the sell | same |
| STT | 0.025% of the sell side | 0.1% of both sides |
| exchange transaction | 0.00297% of buy + sell | same |
| SEBI fee | ₹10 per crore of buy + sell | same |
| stamp duty | 0.003% of the buy side | 0.015% of the buy side |
| GST | 18% of brokerage + exchange + SEBI | same |

Worked by hand: 83 RELIANCE bought at 1174.30, stopped at 1163.30, MIS.
Buy ₹97,466.90, sell ₹96,553.90. Brokerage ₹20 + ₹20 = ₹40.00, STT ₹24.14,
exchange ₹5.76, SEBI ₹0.19, stamp ₹2.92, GST ₹8.27: **₹81.28**. So 83 shares
lose ₹913.00 + ₹81.28 = ₹994.28 at the stop and fit a ₹1,000 max loss; 84
would lose ₹1,005.71. These are a typical discount broker's rates, set in
`"charges"`. **Check them against your broker's charge sheet**: rates change,
many brokers charge no brokerage on delivery, and STT is rounded by the
exchange. `"include_charges": false` leaves them out.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the loss at the stop with the charges, and say plainly that a stop is an
order, not a promise: in a fast market or a gap it can fill below the stop.
Then hand over to `/trader-risk`.

## Example: RELIANCE, Thursday 2026-10-29

Real five-minute bars. Opening range (09:15 to 09:30) 1156.20 to 1170.50.
The 09:35 bar closed above it at 1174.30, entry 09:40. Stop the range
midpoint 1163.30, rounded down to RELIANCE's ₹0.10 tick: 1163.30; target 2R 1196.30. Risk 11.00 a share. The example config:
₹1,00,000 capital, 1% = ₹1,000 max loss, 50% position cap, ₹50,000 max order,
10% cash buffer.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹1,000 / 11.00, charges included | 83 |
| position cap | 50% of ₹1,00,000 = ₹50,000 / 1174.30 | **42 (decides it)** |
| max order | ₹50,000 / 1174.30 | 42 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1174.30 | 76 |

42 shares = ₹49,320.60. At the stop: 42 x 11.00 = ₹462.00 plus about ₹52.00
charges = ₹514.00. It writes:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
 "order_type": "LIMIT", "price": 1174.30,
 "stop_loss": {"order_type": "SL-M", "trigger_price": 1163.30},
 "target": {"order_type": "LIMIT", "price": 1196.30},
 "validity": "DAY",
 "why": "opening-range breakout on RELIANCE: 42 shares, bound by your position cap ...",
 "plan": {"entry": 1174.30, "stop": 1163.30, "target": 1196.30, "risk_per_share": 11.00,
          "max_loss": 462.00, "charges": 52.00, "max_loss_with_charges": 514.00,
          "setup": "orb", "entry_time": "09:40", "flat_by": "15:10"}}
```

Indian brokers no longer offer bracket orders, so the order is linked legs:
once the entry fills, the SL-M stop and the LIMIT target are placed, and when
one fills the other is cancelled (an OCO, or a GTT OCO at brokers that have
one). `why` and `plan` are notes for trader-risk and trader-broker; a broker
never sees them. At 10:20 the real price was 1177.30: a `--market` entry
there risks 14.00 a share and reward:risk falls to 1.36, under 1.5: refused,
because most of the move it was waiting for has happened.

## Weights mode (long-term split, CNC)

`--weights RELIANCE=30,TCS=20` or `--targets FILE` (`targets.example.json`
here, an illustration, not a recommendation) splits the capital across a list
by weight: the cash buffer comes off the top, weights are renormalised to
100, any stock over `max_position_pct` is pinned at the cap and its excess
shared out, it buys whole shares (it needs prices: `--prices
trader/prices.json`), slices under the minimum order are dropped, and money
that cannot be invested is reported as left idle. It writes CNC MARKET buys
with no stop: trader-risk blocks them while `require_stop` is true and counts
them against the day limits. Use a separate config (`--config`) for a
long-term split.

## Rules

- Proposals only. This skill never calls a broker.
- Never size money the user has not said is for this. Ask for the capital and the risk per trade.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, confirms or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, never uses broker leverage
(MIS margin), and never sizes F&O or short positions.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
