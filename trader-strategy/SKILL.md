---
name: trader-strategy
description: >-
  Option strategy builder for NSE F&O (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
  NIFTYNXT50 and F&O stocks) on a real option chain: bull and bear call and
  put spreads, long and short straddles and strangles, iron condor, iron fly,
  call and put butterflies (broken wing too), ratio spreads, covered call,
  protective put, calendar spreads and custom legs picked by strike, offset
  from ATM, delta or premium. Shows the payoff at expiry and today (Black-76,
  each leg at its own IV), breakevens, max profit and max loss (and when
  either is unlimited), net premium, net Greeks, the same curve after 2026 F&O
  charges with the STT-on-exercise warning, the margin a broker would block
  (a SPAN-style estimate with its error bound) next to the exact max loss and
  the return on margin, and the probability
  of profit two ways: textbook lognormal vs a seeded bootstrap of the index's
  own returns, with expected value and 5% tail loss after charges. Use when
  the user says "build an iron condor on Nifty", "Nifty iron condor 200 points
  wide", "short strangle at 16 delta", "bull call spread on Reliance", "what
  is the breakeven", "max loss of this spread", "payoff chart", "payoff
  graph", "probability of profit", "PoP", "how much margin for a short
  straddle", "margin for iron fly", "straddle vs strangle", "is this
  butterfly worth it", "calendar spread Nifty", "Greeks of my position", "net
  delta", "theta per day", "after charges breakeven", "STT on expiry", or
  "run the strategy builder".
argument-hint: "list | build TEMPLATE SYMBOL [--expiry DD-Mon-YYYY] [--wings N --width N --lots N] | price SYMBOL --legs ..."
---

# trader-strategy

Turns an option chain into a position and says, in rupees, what it can make,
what it can lose, what it costs to hold and how likely each is: gross and
after charges, textbook and simulated, side by side. It describes a trade;
it never places one.

Tool in this folder (standard library Python, no keys):

```bash
python3 "<this skill folder>/strategy.py" --help
```

(`<this skill folder>` is wherever this skill sits in your agent's skills
folder.)

## Commands

```bash
python3 "<this skill folder>/strategy.py" list                                  # the templates
python3 "<this skill folder>/strategy.py" build iron-condor NIFTY --expiry 03-Nov-2026 --wings 200 --width 100 --lots 1
python3 "<this skill folder>/strategy.py" build short-strangle NIFTY --delta 0.16
python3 "<this skill folder>/strategy.py" build bull-call-spread RELIANCE --width 20
python3 "<this skill folder>/strategy.py" build call-butterfly NIFTY --width 100 --skip 100   # broken wing
python3 "<this skill folder>/strategy.py" build calendar NIFTY --expiry 03-Nov-2026 --far-expiry 01-Dec-2026
python3 "<this skill folder>/strategy.py" price NIFTY --legs "-1 CE ATM+200, +1 CE ATM+300, -1 PE D25, +1 PE P20"
python3 "<this skill folder>/strategy.py" build iron-fly NIFTY --at today --iv-shift 2 --json
python3 "<this skill folder>/strategy.py" build iron-fly NIFTY --write          # -> trader/strategy.json
python3 "<this skill folder>/strategy.py" build iron-fly NIFTY --orders         # -> trader/orders.json (a proposal)
```

Template knobs: `--strike` (the centre or the long strike; default ATM),
`--wings` (points from ATM to the short strikes of strangles and condors, or
to the OTM option of a covered call / protective put; default the 1-SD
expected move), `--width` (points from a short strike to its bought wing, or
between spread strikes; default two strikes), `--delta 0.16` (short strikes by
delta instead of wings), `--skip` (extra points on one butterfly wing),
`--ratio` (shorts per long in a ratio spread, default 2), `--type CE|PE`
(calendar), `--lots` (per leg; quantities are always lots x the lot size).

Custom legs: `[+|-]LOTS CE|PE|FUT STRIKE [@EXPIRY] [=PRICE]` separated by
commas. STRIKE is `23000`, `ATM`, `ATM+200`, `D25` (the strike nearest 0.25
delta) or `P50` (the strike whose premium is nearest ₹50). `@01-Dec-2026` puts
a leg on another expiry; `=120.5` uses your own fill instead of the chain's.

Other flags: `--fill touch` (buy at the ask, sell at the bid; default the
chain's mid), `--at today|DATE` (the "today" curve's horizon), `--iv-shift N`
(vol points on every leg), `--seed`, `--paths` (simulation), `--no-sim`,
`--scan-pct`, `--vol-scan`, `--elm-pct` (margin assumptions), `--product
NRML|MIS`, `--offline DIR` (saved chains and history, repeatable).

## What it reads and writes

- The chain: `--options FILE` (trader-options' JSON, `bharat-trader/options/1`;
  repeat for a second expiry), else `trader/options.json` when it is the same
  underlying and expiry, else trader-options' own script when that skill sits
  in the next folder (it fetches NSE's chain politely). Copied alone, this
  skill works from an options file: run `/trader-options chain SYMBOL --write` first.
- History for the simulation: Yahoo Finance chart API, one year of daily
  closes and five days of five-minute closes (`^NSEI` for NIFTY, `.NS` for
  stocks), or `history-<symbol>-1d.json` in an `--offline` folder.
- `trader/config.json`: `fno_charges` overrides the dated F&O rates.
- Writes `trader/strategy.json` with `--write`, and a proposal in
  `trader/orders.json` with `--orders` (it refuses to overwrite someone else's
  proposals without `--replace`).

## Every number, explained

- **Legs.** Each leg's premium is the chain's price (bid/ask mid when the
  spread is tight, else the LTP), its IV is trader-options' Black-76 IV at that
  strike (or interpolated on the smile when the strike has none), and its
  forward and time are those of its own expiry.
- **Net premium.** Premium received minus premium paid, options only. Credit
  if positive. **Spread cost**: what crossing the bid/ask on every leg would cost.
- **Payoff at expiry.** Sum over legs of (intrinsic - premium) x qty, sign by
  side. With one expiry it is exact; with two (calendar) the later leg is
  valued by Black-76 at its own IV when the near one expires, and its forward
  moves in proportion with the near one.
- **Today / at a date.** Each leg by Black-76 with the time left at that
  date, IV per leg plus `--iv-shift`.
- **Breakevens.** Where the curve crosses zero, refined to 0.01 point.
- **Max profit / max loss.** The payoff's extremes from an underlying of 0
  upwards. A side is unlimited when the slope far out is a whole naked
  option's worth; a short put is not unlimited, its worst is with the
  underlying at 0 and the tool says so.
- **After charges.** Every leg's round trip at the 2026 rates (from 1 Apr
  2026: STT 0.15% on the option premium sold and 0.15% of the intrinsic value
  on exercise, ₹20 a order, NSE 0.03553%, SEBI ₹10 a crore, stamp 0.003% on
  the buy, GST 18%). At expiry an in-the-money long is exercised (STT on the
  intrinsic value, no sell order), out-of-the-money options expire with no
  order, an assigned short pays no STT on settlement. The breakeven shift is
  how far charges move each breakeven. After-charges extremes are read over
  the chart's range (the strikes +/- about 3.5 SD).
- **STT on exercise.** Any long option that may end in the money gets a
  warning with the STT and total charges on exercise and on a sale before
  15:30 on expiry day. Stock options settle by delivery: the warning says how
  many shares and rupees that means.
- **Net Greeks.** Leg Greeks x qty, signed: delta in units of the underlying
  (₹ per 1 point, and in lots), gamma per point, theta ₹ per calendar day,
  vega ₹ per vol point.
- **Margin required** (the headline; `margin.required`): what a broker would
  block, estimated. With any short option or future it is a SPAN-style
  estimate: the worst of 16 scenarios on the WHOLE position (moves of 0, 1/3,
  2/3 and 1 x the price scan each way with IV up and down by the vol scan,
  plus 3 x the scan at 30% weight), so bought wings cut the scanning risk;
  plus exposure on the notional of EVERY short option and future, hedged or
  not (2% index, 3.5% stocks, 2% more on index shorts on their expiry day);
  plus premium paid. Index scan 9.3% and vol scan 4 points (NIFTY in NSE's
  SPAN file of 30-Oct-2026), stated error +/-15%; stocks 20% and 10 points
  (not from a file), +/-35%. Bought options only: the premium paid, exact.
  Checked against the SPAN file's own risk arrays (scan risk within 1% to 9%
  on NIFTY 03-Nov). `broker_basket` stays `null` until a broker adapter
  reports the broker's basket margin.
- **Max loss at expiry** (`margin.max_loss`, also `expiry.max_loss`): the
  most the position can lose, exact for one expiry. It is NOT the margin: for
  a NIFTY iron condor the max loss is about ₹4,100 while a broker blocks
  about ₹67,000, because exposure is charged on both short legs.
  **Return on margin** = max profit / margin required.
- **Probability of profit (textbook).** The underlying at the horizon is
  lognormal around the forward at the ATM IV (no drift). PoP = the share of
  that distribution where the P&L after charges is above 0; also gross PoP,
  expected value, the 5th-percentile P&L and the average of the worst 5%.
  2,000 quantile points, deterministic.
- **Probability of profit (simulated).** Each path adds the right number of
  daily log returns (NSE sessions to the horizon, holidays skipped) and
  five-minute returns for what is left of today, drawn with replacement from
  the underlying's own last year (demeaned: no drift edge), to the forward.
  Fat tails and real realised vol, not a normal curve. Seeded (`--seed 7`
  default): the same inputs give the same answer. Three IV-shift scenarios
  (-5, 0, +5 points) rescale the returns to that vol and value any leg still
  alive at it.

## trader/strategy.json (schema `bharat-trader/strategy/1`)

```
schema, name, underlying, kind ("index"|"stock"), lot_size, asof (chain time, IST), spot, forward, atm, atm_iv,
expiries[], near_expiry, days_to_expiry, params{},
legs [{side, lots, lot_size, qty, instrument ("OPT"|"FUT"), option_type, strike, expiry, price, price_source,
       bid, ask, iv, iv_source, forward, t_years, greeks{delta, gamma, theta, vega} per unit, oi, notes[]}],
net_premium {amount, kind ("credit"|"debit"), paid, received}, spread_cost,
greeks {delta, delta_lots, gamma, theta, vega, complete},
expiry {breakevens[], breakevens_after_charges[], breakeven_shift[{gross, after_charges, shift}],
        max_profit, max_loss, unlimited_profit, unlimited_loss, max_profit_at, max_loss_at,
        max_profit_after_charges, max_loss_after_charges, exact, reward_risk},
charges {at_forward_expiry, at_forward_expiry_detail[], rates_from{}, note},
curve {x[], expiry[], expiry_net[], at[], at_net[], horizon, iv_shift, breakevens_at[], spot, forward, expected_move{}},
margin {label, required, low, high, error_pct, method ("span-style"|"premium"), explain, defined_risk,
        max_loss (exact, positive; null if unlimited), max_loss_unlimited, return_on_margin (%),
        span_style {method, scan_risk, worst_scenario, exposure, expiry_day_elm, premium_paid, premium_received,
                    total, error_pct, low, high, assumptions{}}, broker_basket (null), broker_note},
probability {horizon, textbook {pop, pop_gross, ev, tail_5pct, tail_5pct_avg, median, sigma, method},
             simulated {... , paths, sessions, intraday_steps, realised_vol, history{}, method} | null,
             scenarios [{iv_shift, vol, pop, ev, tail_5pct, tail_5pct_avg}]},
orders [{symbol, exchange "NFO", segment, instrument (OPTIDX|OPTSTK|FUTIDX|FUTSTK), expiry, strike, option_type,
         tradingsymbol null, instrument_token null, side, lots, lot_size, qty, product (NRML|MIS),
         order_type "LIMIT", price (0.05 tick, never worse than priced), validity "DAY", leg, tag, why}],
warnings[], notes[], sources{}, generated_at
```

Rupee amounts are for the whole position; `curve` values are P&L in rupees
at each underlying price `x`. `tradingsymbol` is left for the broker adapter
(each broker spells weekly contracts differently).

## For other programs

`strategy.py` can be imported (no side effects). Pure functions:
`list_templates`, `template_specs`, `parse_legs`, `parse_strike`,
`resolve_legs`, `evaluate` (legs + chains in, the strategy.json body out),
`Position`, `payoff_curve`, `breakevens`, `extremes`, `net_greeks`,
`net_premium`, `margin_estimate`, `span_style`, `textbook_probability`,
`simulated_probability`, `draft_orders`, `orders_document`, `black76_price`,
`black76_greeks`, `round_trip_charges`. `fetch_markets` and `load_history`
do the network (or offline) reads.

## Steps

1. Get the chain: if trader-options is installed, `build` fetches it; else
   run `/trader-options chain SYMBOL --expiry ... --write` first.
2. Build the template (or price the user's legs). Lead with max profit, max
   loss, breakevens and margin, then the two probabilities side by side, all
   after charges. Put every WARNING line (STT on exercise, delivery, expiry
   day, stale chain) first.
3. Say plainly which numbers are exact (expiry payoff of one expiry, charges,
   max loss) and which are models (the today curve, a calendar's far leg,
   margin required and its error, both probabilities). Margin required is an
   estimate of what the broker blocks; never present the max loss as the
   capital needed.
4. If the user wants to trade it: `--orders` writes a proposal only. It goes
   to `/trader-risk`, then `/trader-broker`, paper by default; each leg needs
   its own PASS and the user's typed yes. Until trader-risk has F&O rules,
   it blocks option legs unless the config allows F&O.

## Example

`strategy.py build iron-condor NIFTY --expiry 03-Nov-2026 --wings 200 --width 100`
on the 30-Oct-2026 close: buy 22,700 PE, sell 22,800 PE, sell 23,200 CE, buy
23,300 CE, 1 lot each (65). Credit ₹2,370.88; max profit ₹2,370.88, max loss
₹4,129.12 (₹2,263.82 and ₹4,345.90 after charges); breakevens 22,763.53 and
23,236.47, after charges 22,765.17 and 23,234.83. Margin required about
₹67,409 (+/-15%: scan risk ₹4,126, exposure ₹59,858 on the two short legs,
premium paid ₹3,426), so the return on margin is 3.5%. PoP after charges:
textbook 57.9%, simulated 62.9% (realised vol 13.8% vs IV 12.1%).

## What it never does

- Never places, modifies or cancels an order.
- Never calls a probability a promise, or a model number exact.
- Never hides charges: every headline number has its after-charges twin.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
