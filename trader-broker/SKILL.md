---
name: trader-broker
description: >-
  Order Executor for NSE trades, PAPER trading in v0.1, never without your
  typed yes. Shows each proposed order as a plain ticket (BUY 42 RELIANCE on
  NSE, MIS, LIMIT, then an SL-M stop-loss and a LIMIT target, the order value
  and the loss at the stop with STT and other charges) plus the exact
  broker-neutral order JSON. An order is READY only if trader-risk passed
  that exact order today. Records your yes word for word, places it on the
  paper account, simulates fills (at your price or the fresh quote) with
  charges, keeps trader/portfolio.json and a trades log, cancels the other
  leg when the stop or target fills, and builds the SELL tickets that close
  every MIS position by your flat-by time (15:10 IST), before the broker's
  auto square-off. Use when the user says "place the order", "buy it",
  "execute", "paper trade", "confirm", "I got filled", "square off", "close
  my position", "flatten", "get me out", "exit all intraday", "what do I
  hold", "my P&L today", or after /trader-risk passes.
argument-hint: "[show | confirm --index N --yes-text \"...\" | fill | flatten | status]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to an order,
so it is the strictest. In v0.1 the only broker is **paper**: no real money,
no real orders, and there is no code here that talks to a real broker. An
order is placed (on paper) only after two separate gates, every time, for
every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat, and
   that yes is recorded word for word.

No yes, no order. One yes, one order.

All amounts are rupees, all times IST. `<skill dir>` below is this skill's
folder in your agent's skills folder (where this SKILL.md lives).

```bash
python3 "<skill dir>/ticket.py" show                                  # every ticket, READY or not
python3 "<skill dir>/ticket.py" confirm --index 1 --yes-text "yes"    # the user's words, verbatim
python3 "<skill dir>/ticket.py" decline --index 1
python3 "<skill dir>/ticket.py" fill --index 1 --price 1174.30        # the entry filled (paper)
python3 "<skill dir>/ticket.py" fill --index 1 --leg stop             # the stop filled; the target is cancelled
python3 "<skill dir>/ticket.py" flatten                               # SELL tickets for every open MIS position
python3 "<skill dir>/ticket.py" status                                # cash, positions, working legs, today
```

`--now "2026-10-29 09:41"` pins the clock (IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today, for this exact order, is READY |
| `trader/state/broker.json` | every yes (verbatim), no, placed, filled and cancelled leg, with each entry's max loss |
| `trader/portfolio.json` | `{"cash", "positions": {SYM: {"qty", "avg_price", "product"}}}`, updated by `fill` |
| `trader/state/trades.json` | every paper fill: side, qty, price, value, charges and their parts, realised result on a sell |
| `trader/prices.json` | the fresh price `fill` uses when no `--price` is given (under 15 minutes old) |
| `trader/plan.json` | the flat-by time for `flatten` |

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order on the paper account? Reply yes
   or no." One order per question. Never offer "yes to all".
3. Run `ticket.py confirm --index N --yes-text "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "ok", "yes to all", or a yes that names another
   stock or a different quantity). Ask again. Never turn a reply into a yes
   yourself, and never type the yes for the user.
4. When the paper order fills, `ticket.py fill --index N --price P`, or
   without `--price` at the fresh price in `trader/prices.json`. A BUY LIMIT
   cannot fill above its price: it waits. The fill pays the buy-side charges
   (brokerage, exchange, SEBI, stamp duty, GST) out of the cash.

## Steps: while it is open, and out by the close

- After the entry fills, the SL-M stop and the LIMIT target work as a pair:
  when one fills, the other is cancelled. Record it with `fill --index N
  --leg stop` (at the trigger; a real stop can fill worse) or `--leg target`.
- trader-alerts watches the stop, the target and the flat-by time.
- By `flat_by` (15:10 IST) anything intraday still open gets closed.
  Brokers auto square-off MIS positions around 15:15 to 15:25 IST, usually
  with an extra fee, so this pack exits first:
  1. `ticket.py flatten` writes a SELL MARKET ticket for every open MIS
     position in `trader/portfolio.json` (CNC holdings are kept unless
     `--include-cnc`).
  2. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the day limits or the entry window.
  3. `ticket.py show`, then the user's yes for each ticket, as above.
  4. `ticket.py fill --index N`. The paper stop and target of that position
     are cancelled.
- `ticket.py status` shows cash, positions, working legs, today's orders,
  trades and daily loss used, fills, charges paid and the realised result.

## Example: RELIANCE, Thursday 2026-10-29

```
ORDER 1 of 1, PAPER account (no real money)
  BUY 42 RELIANCE on NSE, MIS (intraday), LIMIT 1174.30, good for today
  then a stop-loss: SL-M trigger 1163.30
  then a target: LIMIT 1196.30 (when the stop or the target fills, the other is cancelled)
  order value about ₹49,320.60
  loss if the stop fills at 1163.30: about ₹462.00 + ₹52.00 charges = ₹514.00 (a stop can fill worse in a fast market)
  READY: trader-risk PASS, waiting for your yes
```

The user replies "yes". `confirm` records "yes" verbatim and places it on
paper. Filled at 1174.30: charges ₹20.73, cash ₹1,00,000 to ₹50,658.67.
Neither leg filled; at 15:10 `flatten` writes SELL 42 RELIANCE MARKET; it
passes trader-risk as an exit, the user says yes, and it fills at the real
15:10 price 1168.80: sell charges ₹31.43 (including ₹12.27 STT), realised
(1168.80 minus 1174.30) x 42 = minus ₹231.00, minus ₹52.16 charges: minus
₹283.16. Cash ₹99,716.84.

## The broker-neutral order

Every ticket carries the same order JSON, whatever the broker:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
 "order_type": "LIMIT", "price": 1174.30,
 "stop_loss": {"order_type": "SL-M", "trigger_price": 1163.30},
 "target": {"order_type": "LIMIT", "price": 1196.30},
 "validity": "DAY"}
```

`order_type` is MARKET (price null), LIMIT, SL or SL-M; `product` MIS or CNC;
`validity` DAY or IOC. Prices arrive already on the tick: trader-budget
rounds them with `tick_size` `"auto"` (by NSE price band; RELIANCE above
₹1,000 is on ₹0.10 steps) and trader-risk blocks anything off it. A number in
the config (`"tick_size": 0.05`) overrides the band. `stop_loss` and `target` are optional linked legs,
placed only after the entry fills. `why` and `plan` in `trader/orders.json`
are notes for the user and the checks, never part of the order.

## Broker adapters (coming)

Later versions will add adapters for Indian brokers (for example Zerodha,
Upstox, Dhan). None is in v0.1, and setting `"broker"` to anything but
`"paper"`, or `"paper"` to false, makes `confirm` refuse. An adapter is a
small module that maps the neutral order above and nothing more:

| method | does |
| --- | --- |
| `place(order) -> broker_order_id` | the entry only, after the user's recorded yes for that fingerprint |
| `place_legs(order, entry_fill) -> (stop_id, target_id)` | the SL-M stop and the LIMIT target once the entry fills, as an OCO / GTT OCO where the broker has one, else two orders the adapter cancels against each other |
| `status(broker_order_id) -> {"status", "filled_qty", "avg_price"}` | open, filled, rejected or cancelled |
| `cancel(broker_order_id)` | cancels one order |
| `positions() -> {SYM: {"qty", "avg_price", "product"}}` | what is held, for `flatten` and `status` |
| `funds() -> {"cash"}` | available cash |

Rules for any adapter: it never places an order without a `placed` record
holding the user's verbatim yes for that exact fingerprint; it maps fields
one to one and refuses what it cannot map (never "rounds" an order into
something else); keys and tokens stay in environment variables, never in
the chat or in `trader/`; it is paper or sandbox by default.

## What it never does

- Never places an order without both gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, passwords or OTPs.
- Never switches `"paper"` to false or `"broker"` away from paper. The user does that themselves.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
