---
name: trader-broker
description: >-
  Order Executor for NSE trades: PAPER trading by default, never without your
  typed yes, plus read-only access to your real Upstox, Zerodha Kite, Dhan or
  Angel One account. Shows each proposed order as a plain ticket (BUY 42 RELIANCE on
  NSE, MIS, LIMIT, then an SL-M stop-loss and a LIMIT target, the order value
  and the loss at the stop with STT and other charges) plus the exact
  broker-neutral order JSON. An order is READY only if trader-risk passed
  that exact order today. Records your yes word for word, places it on the
  paper account, simulates fills with charges, keeps trader/portfolio.json
  and a trades log, cancels the other leg when the stop or target fills, and
  builds the SELL tickets that close every MIS position by your flat-by time
  (15:10 IST). Reads your broker's holdings, positions, funds and order book;
  logs you in through a local browser callback or Angel One's TOTP code (the
  daily token stays in your shell); checks your static IP against SEBI's
  rules; sends the stop and target as ONE broker-native linked order (Kite GTT
  OCO, Upstox multi-leg GTT, Dhan Super Order) under the same gates and your
  yes, reconciles them after a restart, and shows the broker's own basket
  margin next to our estimate. On paper it also
  trades NSE F&O: a multi-leg option strategy (iron condor, strangle,
  spread) from trader-strategy is ONE basket ticket and one yes, filled all
  or nothing and hedge first, with margin blocked and released, broker
  charge profiles (Zerodha, Upstox, Dhan, Angel One, Groww), spread
  slippage, market protection, price bands, optional seeded part fills,
  mark to market with Black-76 Greeks, rolls and hedges as new tickets, and
  expiry settlement at NSE's final settlement price with STT on exercise.
  Use when the user says
  "place the order", "buy it", "execute", "paper trade", "confirm", "I got
  filled", "square off", "close my position", "flatten", "get me out", "exit
  all intraday", "what do I hold", "my P&L today", "my Zerodha holdings",
  "Upstox positions", "Dhan funds", "login to Kite", "connect my broker",
  "static IP check", "is my broker ready", "paper trade this iron condor",
  "place the basket", "my F&O MTM", "option positions P&L", "portfolio
  Greeks", "roll my short call", "add a hedge", "close the strangle",
  "settle my expiry", "kitna margin laga", "Angel One login", "Angel
  holdings", "GTT OCO", "super order", "bracket order", "stop aur target ek
  saath", "broker margin check", "reconcile my GTTs", or after /trader-risk
  passes.
argument-hint: "[show | confirm --index N --yes-text \"...\" | confirm --basket | fill | mtm | adjust | settle | flatten | status | holdings | positions | preflight | margin-check | reconcile]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to an order,
so it is the strictest. The default broker is **paper**: no real money, no
real orders. It can also read your real Upstox, Zerodha Kite or Dhan account
(holdings, positions, funds, orders), and send a live order only when every
gate in "Live brokers" below is open. An order is placed (on paper) only
after two separate gates, every time, for every order:

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

Your real broker account, read only (any IP; nothing is sent):

```bash
python3 "<skill dir>/ticket.py" holdings --broker kite                # delivery holdings at Zerodha
python3 "<skill dir>/ticket.py" positions --broker upstox             # today's positions at Upstox
python3 "<skill dir>/ticket.py" funds --broker dhan                   # available and used funds
python3 "<skill dir>/ticket.py" orders --broker kite                  # today's order book and trades
python3 "<skill dir>/ticket.py" accounts --broker upstox              # profile, funds, token, what the API can do
python3 "<skill dir>/ticket.py" instruments --broker kite --symbol "NIFTY 2026-11-23 25000 CE" --exchange NFO
```

`--json` on any of them prints the broker-neutral shapes. Without
`--broker`, the config's `"broker"` is used.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today, for this exact order, is READY |
| `trader/state/broker.json` | every yes (verbatim), no, placed, filled and cancelled leg, with each entry's max loss |
| `trader/portfolio.json` | schema `bharat-trader/portfolio/2`: `cash`, share `positions` `{SYM: {"qty", "avg_price", "product"}}`, `fno_positions` (signed units, by strategy), `strategies`, `margin_blocked` and the cash/margin `ledger`. An older file is migrated on read and kept once as `portfolio.json.v1.bak` |
| `trader/options.json` | trader-options' latest chain: F&O fill prices (bid/ask), marks and IVs |
| `trader/strategy.json` | trader-strategy's position: its margin estimate and max loss for the basket ticket |
| `trader/bands.csv` | optional: NSE's F&O contract master (`NSE_FO_contract_DDMMYYYY.csv`) for price bands |
| `trader/state/shadow.json` | shadow mode: the paper twin of every live order `place` sent |
| `trader/state/trades.json` | every paper fill: side, qty, price, value, charges and their parts, realised result on a sell |
| `trader/prices.json` | the fresh price `fill` uses when no `--price` is given (under 15 minutes old) |
| `trader/plan.json` | the flat-by time for `flatten` |
| `trader/state/preflight.json` | `preflight`'s result per broker: public IP, static IP, checks, a one-way tag of the token (never the token) |

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
  Brokers auto square-off MIS positions on their own, usually with an extra
  fee, and earlier for F&O stocks (Zerodha 15:12, Upstox 15:10, Dhan 15:10,
  Angel One 15:10, Groww 15:05), so this pack exits first: with
  `broker_profile` set, 5 minutes before that broker's time, and never
  after an F&O stock's continuous trading ends for the closing auction:
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
  loss if the stop fills at 1163.30: about ₹462.00 + ₹52.11 charges = ₹514.11 (a stop can fill worse in a fast market)
  READY: trader-risk PASS, waiting for your yes
```

The user replies "yes". `confirm` records "yes" verbatim and places it on
paper. Filled at 1174.30: charges ₹20.78, cash ₹1,00,000 to ₹50,658.62.
Neither leg filled; at 15:10 `flatten` writes SELL 42 RELIANCE MARKET; it
passes trader-risk as an exit, the user says yes, and it fills at the real
15:10 price 1168.80: sell charges ₹31.49 (including ₹12.27 STT), realised
(1168.80 minus 1174.30) x 42 = minus ₹231.00, minus ₹52.27 charges: minus
₹283.27. Cash ₹99,716.73.

## The broker-neutral order

Every ticket carries the same order JSON, whatever the broker:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "BUY", "qty": 42,
 "order_type": "LIMIT", "price": 1174.30,
 "stop_loss": {"order_type": "SL-M", "trigger_price": 1163.30},
 "target": {"order_type": "LIMIT", "price": 1196.30},
 "validity": "DAY"}
```

`order_type` is MARKET (price null), LIMIT, SL or SL-M; `product` MIS or CNC
(NRML for F&O at a live broker);
`validity` DAY or IOC. Prices arrive already on the tick: trader-budget
rounds them with `tick_size` `"auto"` (by NSE price band; RELIANCE above
₹1,000 is on ₹0.10 steps) and trader-risk blocks anything off it. A number in
the config (`"tick_size": 0.05`) overrides the band. `stop_loss` and `target` are optional linked legs,
placed only after the entry fills. `why` and `plan` in `trader/orders.json`
are notes for the user and the checks, never part of the order.

## F&O on paper: baskets, margin, MTM, adjustments, expiry

trader-strategy's `--orders` writes every leg of a strategy to
`trader/orders.json` with `"basket": true`. The paper broker shows that file
as **one ticket** listing every leg, the net premium, the margin it blocks,
the max loss at expiry (with charges) and the charges, and takes **one yes
for the whole basket**, only when trader-risk passed every leg today.

```bash
python3 "<skill dir>/ticket.py" show                                   # BASKET of 4 legs ... READY
python3 "<skill dir>/ticket.py" confirm --basket --yes-text "yes"      # the user's words, verbatim
python3 "<skill dir>/ticket.py" fill --basket                          # every leg at the latest chain, or none
python3 "<skill dir>/ticket.py" mtm                                    # P&L, Greeks, margin per strategy
python3 "<skill dir>/ticket.py" adjust --strategy S-20261102-1 --roll-leg 3 --strike 23250
python3 "<skill dir>/ticket.py" adjust --strategy S-20261102-1 --add-hedge "BUY 1 PE 22500"
python3 "<skill dir>/ticket.py" adjust --strategy S-20261102-1 --close-leg 2   # or --close-all
python3 "<skill dir>/ticket.py" settle --date 2026-11-03 --settlement FOSett_prce_03112026.csv
python3 "<skill dir>/ticket.py" shadow-report --live --broker kite     # live fills next to their paper twins
```

- **Lots and products.** Every leg is whole lots (`lot_size` from NSE's
  fo_mktlots), NRML (carry forward) or MIS (intraday). Positions are kept
  per strategy in signed units.
- **One yes, all or nothing, hedge first.** The yes must start with "yes";
  "sure", "ok", a doubt, another underlying or a number that is not on the
  ticket is refused. The fill buys first (the wings, or buy-backs of shorts)
  and sells after, as you would for the hedge's margin benefit; if any leg
  cannot fill, or the margin is short after any step, nothing fills.
- **Margin.** Blocked per strategy: trader-strategy's SPAN-style estimate in
  `trader/strategy.json` less the premium paid (paid from cash), or this
  skill's own estimate with the same method (and a futures scan) after an
  adjustment. Free cash = cash - margin blocked. Closing a leg or the
  strategy releases it; every move is a `ledger` row.
- **Fills.** A buy pays mid + `paper_spread_share` x spread (0.5, the ask), a
  sell gets the mirror; with no two-sided quote, the last price +/-
  `paper_slippage_bps` (10). A LIMIT fills at its price or better or waits.
  A MARKET order becomes a LIMIT `market_protection_pct` (2) from the last
  price, as brokers do for API market orders under SEBI's retail-algo rules,
  and is cancelled if the book is past it. A price outside the contract's
  band (NSE's contract master, when you keep it at `trader/bands.csv`) is
  rejected. `"paper_partial_fills": true` lets a single-leg ticket fill in
  part (top-of-book depth and a seeded draw, `paper_seed`); a basket still
  fills whole or not at all.
- **Charges by broker.** `broker_profile` (zerodha, upstox, dhan, angel,
  groww; it also sets the square-off times) picks that broker's brokerage
  for paper fills, cash market and F&O; STT, exchange, SEBI, stamp and GST
  are the statutory rates. `custom` (or none) keeps your own `charges` /
  `fno_charges`. Sources and dates are in `ticket.py` (`CHARGE_PROFILES`).
- **Mark to market.** `mtm` marks every leg to `trader/options.json` (the
  mid; futures at the parity forward), with Black-76 Greeks from
  trader-options' functions, per leg, per strategy and in all, and the
  payoff at expiry of each open structure (the UI plots it).
- **Adjustments** (roll a leg, close a leg, add a hedge, close all) only
  write a new basket ticket. It goes through trader-risk and a new yes.
- **Expiry.** `settle` takes NSE's final settlement price from the expiry
  day's `FOSett_prce` file or F&O bhavcopy (or `--price NIFTY=23012.40`).
  Index options are cash settled: an in-the-money long gets the intrinsic
  value and pays STT on exercise (0.15% of it); a short pays it; anything
  else expires worthless. Futures settle at the final settlement price.
  Stock options are physically settled at NSE: `mtm` warns 3 days before,
  and the default is to close them before expiry.
- **Shadow mode.** `"shadow": true` records, for every live order `place`
  sends, what paper would have filled (trader/state/shadow.json). The live
  order is not changed. `shadow-report` compares them.
- `flatten` still writes share tickets only: it names each open F&O strategy
  and the `adjust --close-all` that closes it as one basket.

Baskets are paper-only in this version: a live config refuses `confirm
--basket`.

## Live brokers: Upstox, Zerodha Kite, Dhan, Angel One

The adapters live in `adapters/` next to `ticket.py` (`base.py` is the
contract; `upstox.py`, `kite.py`, `dhan.py`, `angel.py` map each broker's API,
`auth.py` the login helpers, standard library only). Setup per broker, plans and costs: `docs/brokers.md` in the
repo.

**Credentials.** Only from environment variables the user sets in their own
shell: `BT_UPSTOX_API_KEY`, `BT_UPSTOX_API_SECRET`, `BT_UPSTOX_REDIRECT`;
`BT_KITE_API_KEY`, `BT_KITE_API_SECRET`, `BT_KITE_REDIRECT`;
`BT_DHAN_CLIENT_ID` and `BT_DHAN_ACCESS_TOKEN` (or `BT_DHAN_API_KEY`,
`BT_DHAN_API_SECRET`, `BT_DHAN_REDIRECT`); `BT_ANGEL_API_KEY`,
`BT_ANGEL_CLIENT_CODE`, `BT_ANGEL_PIN` and optionally `BT_ANGEL_TOTP_SECRET`.
Never ask for them, or for a TOTP code, in the chat.

**Daily login.** SEBI's rules mean a 2FA login once every trading day.
`ticket.py login <broker>` (the user runs it) opens the broker's login page,
waits for ONE callback on `127.0.0.1` (the redirect URL registered with the
broker, e.g. `http://127.0.0.1:8765/kite`), checks the `state` value,
exchanges the code and prints two `export BT_<BROKER>_...` lines for the
user's shell (`eval "$(python3 ticket.py login kite)"`). The token is never
written to disk, the repo or `trader/`, and is scrubbed from every message.
Angel One has no browser step: `ticket.py login angel` sends the client code
and PIN from the shell with a 6-digit TOTP code the user types (not echoed)
or, if `BT_ANGEL_TOTP_SECRET` is set, computes it (RFC 6238). Setting the
secret folds two factors into one: anyone who can read the shell's
environment can log in as the user; typing the code is safer
(`docs/brokers.md` explains).

**Preflight.** `ticket.py preflight <broker>`: the public IP (two echo
services must agree) against the config's `"static_ip"` and, for Upstox and
Dhan, the IPs the broker has on record (Angel One cannot tell: the config's
`"static_ip"` it is); the token's freshness (Kite stops at 06:00 IST, Upstox
at 03:30 IST, Dhan 24 hours after login, Angel One at midnight); one harmless
call (your profile). It says in plain words what passed and why it matters.

**A live order** needs every gate, checked again at the moment of sending:

1. the config's `"broker"` is that broker, and
2. `"paper": false`, and
3. `"live_enabled": {"<broker>": true}`, and
4. trader-risk PASS for this exact order today (fingerprint), and
5. the user's typed yes for this exact order, recorded by `ticket.py
   confirm` (with a live config, confirm only records the yes), and
6. `ticket.py preflight <broker>` passed today with the token in use now.

Then `ticket.py place --index N` (the user runs it in their own terminal)
sends the entry, rate limited to `"order_rate_per_sec"` (default 3, never
above 9: SEBI's line for algo registration is 10). One yes, one order: a
second `place` of the same order is refused. MARKET and SL-M go out only with
the broker's market protection (Kite, Upstox); Dhan takes LIMIT and SL only.
After the entry fills, `place --index N --leg stop` (or `--leg target`)
sends one exit leg; only one exit leg works at the broker at a time that way
(`cancel --index N --leg stop` first). Reads are retried; an order call never
is: if the line drops, it says so and asks you to run `ticket.py reconcile`
or check `ticket.py orders` for the order's tag (`bt...`).

**Native linked exits** (capability `native_oco`): the stop and the target as
ONE broker order, so the broker cancels the other when one fills, even with
this machine off. Kite: `place --index N --leg oco` after the entry fills (a
GTT two-leg OCO; GTT legs are LIMIT orders, so the stop needs an SL limit
price). Upstox and Dhan: `place --index N --leg bracket` instead of the entry
(a multi-leg GTT, a Super Order: the LIMIT entry with both exits). Angel
One's GTT is single-leg, so its exits go leg by leg. The same six gates
apply; the ticket's yes covers that ticket's own linked exit only (never a
plain order, another ticket or edited prices), once, and never alongside
leg-by-leg exits. An auto-pilot yes never opens a live gate. `cancel --index
N --leg oco|bracket` (or `--linked-id ID`) cancels it. A new stop or target is
a new ticket with its own trader-risk PASS and yes, sent with `ticket.py
modify-exits --index NEW --linked-id ID`. After a restart, `ticket.py
reconcile` reads the broker's GTT / super-order book (read only), matches it
to this tool's records, finds a send whose outcome was unknown and lists
linked orders it did not make (left alone).

**The broker's basket margin.** `ticket.py margin-check --broker B` asks the
broker's margin calculator (Kite `/margins/basket`, Upstox margin API, Dhan
margin calculator, Angel One margin API; read only) for the F&O legs in
`trader/orders.json` and shows it next to our SPAN-style estimate, with the
difference. The day's figure is kept in `trader/state/broker_margin.json`,
where trader-strategy and trader-risk pick it up (they also ask once
themselves when the broker's token is in the shell). It never changes an
estimate or a verdict.

## What it never does

- Never places an order without both gates (on paper) or all six (live), and never two orders on one yes.
- Never runs `ticket.py login`, `ticket.py place`, `cancel` or `modify-exits` itself: the user runs them, in their own terminal.
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
