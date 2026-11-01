# Options: the chain, Greeks, strategies and margin

Two skills cover NSE options. `trader-options` reads one option chain and
works out the numbers an options trader reads. `trader-strategy` builds a
position on that chain and says what it can make, lose and cost, gross and
after charges, with honest odds. Neither places an order. This page explains
the maths and, as important, where it stops being exact.

```bash
python3 trader-options/options.py chain NIFTY --expiry 03-Nov-2026
python3 trader-options/options.py chain NIFTY --write          # trader/options.json
python3 trader-strategy/strategy.py list
python3 trader-strategy/strategy.py build iron-condor NIFTY --expiry 03-Nov-2026 --wings 200 --width 100
python3 trader-strategy/strategy.py price NIFTY --legs "-1 CE D16, -1 PE D16"
```

The local UI shows both on its Option chain and Strategy pages
([ui.md](ui.md)); an MCP client gets `options_chain`, `options_summary`,
`strategy_build` and `strategy_price` ([mcp.md](mcp.md)).

## The chain

- **Source.** NSE's `option-chain-v3` for one expiry, with the expiry list
  from `option-chain-contract-info` (expiries are never computed: holidays
  move them, e.g. NIFTY 09-Nov-2026 and 23-Nov-2026 are Mondays) and lot sizes
  per expiry month from `fo_mktlots.csv`.
- **Units.** OI, OI change and volume are in contracts (lots); bid and ask
  quantities in units; prices in ₹ per unit.
- **Price used.** The bid/ask mid when the spread is within 30% of it, else
  the LTP if it traded today, else none.
- **Forward.** Put-call parity at the strikes nearest the money,
  F = K + e^(rT)(C - P), the median. Spot stops at 15:30 while options trade to
  15:40 (15:45 from 7 Dec 2026), so spot is stale for pricing; the forward is not.
- **Time.** Calendar minutes to 15:30 IST on expiry day / 525,600.

PCR, max pain, walls, OI build-up, the straddle, expected move, smile, skew
and IV rank are defined, with their limits, in
[trader-options/SKILL.md](../trader-options/SKILL.md).

## Greeks (Black-76 on the forward)

Index options are European and cash-settled; stock options are European and
settled by delivery. Black-76 on each expiry's forward prices both without
guessing dividends or carry.

| Greek | per unit | position |
| --- | --- | --- |
| delta | dPrice/dF (call 0 to 1, put -1 to 0) | x qty: units of the underlying, ₹ per 1 point |
| gamma | change in delta per point | x qty |
| theta | ₹ per **calendar** day (weekends count) | x qty, signed by side |
| vega | ₹ per 1 vol point | x qty, signed by side |

IV comes from a bracketed Newton solve (bisection fallback, 0.1% to 500%).
NSE's own IV column is never used: it disagrees between calls and puts.
Within the last hour of an expiry IV and Greeks are not meaningful; read
premium against intrinsic instead.

## Strategies

Templates (`strategy.py list`): bull and bear call spreads, bull and bear put
spreads, long and short straddles and strangles, iron condor, iron fly, call
and put butterflies (`--skip` for a broken wing), call and put ratio spreads
(`--ratio`), covered call and protective put (a future priced at the parity
forward stands in for the future or the shares), and calendars over two
expiries. Custom legs take a strike by number, offset from ATM (`ATM+200`),
delta (`D25`) or premium (`P50`), another expiry (`@01-Dec-2026`) and your own
fill (`=120.5`). Quantities are lots x the lot size from NSE's file.

What is exact and what is a model:

| number | exact? |
| --- | --- |
| payoff at expiry, one expiry | exact: intrinsic minus premium per leg |
| breakevens, max profit and loss, one expiry | exact (the payoff is straight between strikes); unlimited detected from the slope far out |
| payoff at expiry, calendar | a model: the far leg is a Black-76 value at its IV, its forward moving in proportion |
| "today" / at-a-date curve | a model: Black-76 per leg at its IV plus your `--iv-shift` |
| charges | exact for the stated rates (verify your broker's sheet) |
| max loss at expiry | exact for one expiry; it is not the margin |
| margin required | an estimate of what a broker blocks, with a stated error (exact for bought options only: the premium) |
| probabilities, EV, tail | models, two of them, side by side |

### After charges

Every leg's round trip at the dated F&O rates (`FNO_CHARGES`, the same table
as trader-budget, -risk, -broker, -exit and -timemachine; `fno_charges` in
the config overrides a rate). From 1 April 2026: STT 0.15% of the premium on
a sell and 0.15% of the intrinsic value on exercise; ₹20 a order; NSE
0.03553% of premium; SEBI ₹10 a crore; stamp 0.003% on the buy; GST 18% on
brokerage, exchange and SEBI. At expiry:

- an in-the-money long is exercised: STT on the whole intrinsic value, no
  sell order (no brokerage on that side);
- an out-of-the-money option expires: no closing order, no charges;
- an assigned short pays no STT on settlement (its STT was on the sale).

The **STT-on-exercise warning** prints, for every long that may end in the
money, the STT and round-trip charges if it is exercised and if it is sold
before 15:30 instead. Stock options add a delivery warning: shares and rupees
to take or give.

### Margin: method and limits

The headline is **margin required** (`margin.required`): what a broker would
block for the position, estimated. The **max loss at expiry** is shown on its
own and is never the margin; **return on margin** = max profit / margin
required. Every output names the method's version in `margin_model`
(now `nse-span-elm-v2 (2026-11)`); lab results and edge-hunt scorecards
record it too, so a later change of method is visible next to the numbers.

**Bought options only:** the premium, paid up front (since 1 Feb 2025). No
SPAN or exposure is blocked on a long option. Exact.

**Anything with a short option or a future:** NSE Clearing's published method,
estimated (one block of code shared by trader-strategy, trader-risk and
trader-broker; tests/test_repo.py keeps the copies identical).

1. **SPAN risk.** The worst loss over 16 scenarios on the whole position,
   valued with Black-76 at each leg's IV: price moves of 0, +/-1/3, +/-2/3 and
   +/-1 x the price scan, each with IV up and down by the vol scan, plus +/-3 x
   the scan at 30% weight. Bought wings cut it because the position is valued
   as a whole. Across expiries a calendar-spread charge is added (1.77% of spot
   a unit of matched delta for an index, NIFTY's ₹409 in the file). On an
   index expiry day the expiring contracts are margined on their own: no
   calendar benefit (NSE Clearing). The short-option minimum is 0 in the file.
2. **Less the net option value.** "Total SPAN Margin requirement is equal to
   SPAN Risk Requirement less the net option value" (NSE Clearing SPAN page):
   long premium minus short premium at today's value, not below 0. A credit
   spread therefore needs about its width x quantity of SPAN; a debit spread
   needs none, only its debit.
3. **Exposure (ELM)** on the underlying's last close times the units of
   **every short option, hedged or not**: "As no extreme loss margin is levied
   on long positions there cannot be any offset provided" (NSE equity
   derivatives margins page). Index 2%, 3% for a short index option over 10%
   out of the money, 5% after 9 months; stocks 3.5%, 5.25% over 30% out of the
   money; futures 2% / 3.5%, a futures calendar spread on a third of the far
   leg. On expiry day 2% more on short index options expiring that day, hedged
   or not (SEBI circular of 01-Oct-2024, from 20-Nov-2024).
4. **The net premium paid** on a debit, paid up front. A credit received is not
   used to cut the margin (brokers differ).

| | price scan | vol scan | error stated |
| --- | --- | --- | --- |
| index | 9.3% of spot (NIFTY and BANKNIFTY in NSE's SPAN file, 30 Oct 2026: 2,143.94 / 23,053.10; NSE's stated minimum), 17.7% after 9 months | 4 points | +/-15% |
| stock | 20% (the 75th percentile of the file's 247 underlyings; median 16.7%, NSE's minimum 14.2%) | 10 points | +/-35% |

**How close it is.** Against NSE's own risk arrays (the SPAN file of 30 Oct
2026, trimmed in `tests/fixtures/trader-strategy/span-nifty-20261030.json`),
for 1 lot (65) of NIFTY, reference = file scan risk less net option value + 2%
ELM a short leg + net debit:

| structure | file reference | ours | |
| --- | --- | --- | --- |
| iron condor 22700/22800 - 23300/23400, 03-Nov | ₹66,125 | ₹66,434 | +0.5% |
| iron condor 22500/22800 - 23300/23600, 03-Nov | ₹78,646 | ₹79,426 | +1.0% |
| iron fly 22800/23050/23300, 03-Nov | ₹75,807 | ₹76,178 | +0.5% |
| bull put 23000/22900, 03-Nov | ₹36,370 | ₹36,465 | +0.3% |
| bear put debit 23100/22900, 03-Nov | ₹35,618 | ₹35,661 | +0.1% |
| iron condor 21900/22250 - 23850/24200, 01-Dec | ₹80,150 | ₹82,038 | +2.4% |
| short 23000 straddle, 03-Nov | ₹1,99,240 | ₹2,00,460 | +0.6% |
| short 23000 straddle, 01-Dec | ₹2,07,483 | ₹2,22,924 | +7.4% |

Every hedged structure tested came within 3%, every naked one at or above the
exchange's figure (within 8%). Against published broker figures for NIFTY
spreads at lot 65 (HDFC Securities, Jan, Mar and Apr 2026: "approx margin
required" ₹34,000, ₹31,000, ₹31,000), SPAN + exposure came within 1.5%; with
the debit added, within 11%. Zerodha's 2020 example (bear call spread: SPAN
₹7,437, exposure ₹14,370) has the same split: exposure on the one short leg,
SPAN about the width. Sources are in
`tests/fixtures/trader-strategy/broker-margin-examples.json`.

**What this means for a hedged condor.** Exposure on the short legs dominates
a hedged position's margin, and the rules do not give a hedge any ELM benefit.
A 1-lot NIFTY condor with 350-point wings needs about ₹82,000 on a normal day
against a max loss near ₹16,000, and about ₹1,42,000 on its expiry day (2% more
on each short leg). That is what NSE's rules ask for, not an artefact of the
estimate. The lab's return on margin uses the larger of the two when a trade
is still open on expiry day, because that much must be in the account then.

Known gaps: the stock scan is not the stock's own (pass `--scan-pct` from
NSE's file for one stock); no "additional ELM" for stocks under surveillance
(NSE's ael file); deltas for the calendar charge are Black-76, not SPAN's
composite delta; intraday the exchange's file moves; brokers can add their own
margin on top. `--scan-pct`, `--vol-scan` and `--elm-pct` override the
assumptions. The broker's own basket margin (`margin.broker_basket`) is shown
next to this estimate, with the difference, once `ticket.py margin-check` has
saved the day's figure (or fetched once when your broker's token is in the
shell; Kite, Upstox, Dhan and Angel One, read only). It never changes the
estimate. See [brokers.md](brokers.md). Check it in your broker's app before
any real order.

### Probability of profit, two ways

- **Textbook.** The near expiry's price at the horizon is lognormal around the
  forward at the ATM IV, no drift. 2,000 quantile points: PoP after charges
  and gross, expected value, the 5th-percentile P&L and the average of the
  worst 5%. It ignores the smile and fat tails.
- **Simulated.** A bootstrap of the underlying's own last year of daily log
  returns (Yahoo, demeaned so no drift is assumed) for each NSE session to the
  horizon (holidays skipped), plus five-minute returns for what is left of the
  current session. Seeded (`--seed`, default 7), so a run can be repeated
  exactly. Three IV-shift scenarios (-5, 0, +5 points) rescale the returns to
  that volatility and value any leg still alive at it.

When the two disagree, that is information: a realised vol above IV makes
the simulation harsher on short premium than the textbook, and a quiet year
makes selling look safer than it is.

## From a strategy to an order

`strategy.py ... --orders` (or Send to paper in the UI) writes each leg to
`trader/orders.json` as a broker-neutral proposal: exchange NFO, product NRML
(or MIS), instrument OPTIDX/OPTSTK/FUTIDX/FUTSTK, expiry, strike, option type,
lots, lot size, qty, a LIMIT at the priced premium on the 0.05 tick (rounded
up for a buy, down for a sell), and `tradingsymbol: null` for the broker
adapter to fill. It refuses to overwrite someone else's proposals without
`--replace`. Then the usual chain: trader-risk checks the legs as ONE
basket (it blocks every F&O leg unless the config sets `"allow_fno": true`),
and the paper broker shows the legs as one basket ticket and needs the
user's typed yes to it.

## Paper F&O: the basket, margin, MTM and expiry

The paper broker (`trader-broker/ticket.py`) trades the proposal like this:

| step | command | what happens |
| --- | --- | --- |
| the ticket | `ticket.py show` | ONE ticket for the basket: every leg, the order paper fills them in (buys first), net premium, margin blocked, max loss at expiry with charges, charges now and for the round trip. READY only when trader-risk passed every leg today |
| the yes | `ticket.py confirm --basket --yes-text "<the user's words>"` | one yes covers this basket and nothing else; refused for "ok", "sure", a doubt, another underlying, or a number not on the ticket |
| the fill | `ticket.py fill --basket` | every leg at the latest chain (`trader/options.json`), hedge first, all or nothing |
| the book | `ticket.py mtm` | marks, P&L, Black-76 Greeks per leg, strategy and portfolio; margin; payoff at expiry |
| a change | `ticket.py adjust --strategy ID --roll-leg N --strike K`, `--close-leg N`, `--add-hedge "BUY 1 PE 22500"`, `--close-all` | a new basket ticket, through trader-risk and a new yes |
| expiry | `ticket.py settle --date D --settlement FILE` | NSE's final settlement price; cash settlement of index options; STT on exercise; futures to the settlement price |

**Fill prices.** A buy pays the mid plus `paper_spread_share` (default 0.5,
so the ask) of the bid/ask spread; a sell gets the mirror. A LIMIT at the
mid (what trader-strategy proposes) therefore waits when the book is wider:
that is what a real limit at the mid does. Set `"paper_spread_share": 0` to
fill at the mid. With no two-sided quote the last price moves
`paper_slippage_bps` (10) against you. A MARKET order is sent as a LIMIT
`market_protection_pct` (2) from the last price, the way brokers send API
market orders with market protection under SEBI's retail-algo framework; the
book past that limit cancels it. A limit or fill outside the contract's
price band (MinPric to MaxPric in NSE's `NSE_FO_contract_DDMMYYYY.csv`, kept
as `trader/bands.csv`) is rejected. With `"paper_partial_fills": true` a
single-leg ticket may fill in part (top-of-book depth, then a draw seeded by
`paper_seed` and the ticket); a basket never fills in part.

**Margin and cash.** Option premium moves cash at the fill (paid on a buy,
received on a sell); futures move only their charges until closed. The
margin blocked for a strategy is trader-strategy's SPAN-style estimate less
the premium paid (it is already out of cash), or, after an adjustment,
`ticket.py`'s own estimate with the same scenarios (price scan 9.3% for an
index, 20% for a stock, vol scan, extreme moves at 30% weight, exposure on
every short option and future, 2% more on index shorts on their expiry day).
A future alone needs about its notional x (scan + exposure). Each block and
release is a row in `trader/portfolio.json`'s `ledger`; free cash is cash
less the margin blocked, and that is what trader-risk reads
(`margin_blocked`). These are estimates: check your broker's basket margin.

**Charges by broker.** `broker_profile` in the config picks the brokerage
for paper fills; the statutory parts are the same at every broker.

| profile | cash intraday | delivery | futures | options | source (read 1 Nov 2026) |
| --- | --- | --- | --- | --- | --- |
| zerodha | 0.03% or Rs 20, lower | zero | 0.03% or Rs 20, lower | Rs 20 an order | zerodha.com/charges |
| upstox | 0.1% or Rs 20, lower | Rs 20 an order | 0.05% or Rs 20, lower | Rs 20 an order | upstox.com/brokerage-charges |
| dhan | 0.03% or Rs 20, lower | zero | Rs 20 an order | Rs 20 an order | dhan.co/pricing |
| angel | 0.1% or Rs 20, lower | 0.1% or Rs 20, lower | Rs 20 an order | Rs 20 an order | angelone.in/exchange-transaction-charges |
| groww | 0.1% or Rs 20, lower | 0.1% or Rs 20, lower | Rs 20 an order | Rs 20 an order | groww.in/pricing |

Not modelled: Angel One's and Groww's Rs 5 minimum, and joining offers.
`custom` (or no profile) uses your own `charges` and `fno_charges` exactly.

**Expiry settlement.** NSE index options are European and cash settled at
the final settlement price, the index's close on expiry day: NSE's
`FOSett_prce_DDMMYYYY.csv` gives it as the expiring future's row (for
example NIFTY 22,749.75 on 27-Oct-2026), and the F&O bhavcopy of that day
gives it as `UndrlygPric` on every expiring option. A long that ends in the
money receives the intrinsic value and pays STT on exercise (0.15% of the
intrinsic value since 1 Apr 2026); a short pays the intrinsic value;
anything out of the money expires worthless. Futures settle at the final
settlement price. Stock options are physically settled: a real account
would take or give delivery (and needs delivery margin in the last days),
so `mtm` warns three days ahead and the default is to close them before
expiry; if one is still open, paper settles it in cash and says so.

Education and tooling, not financial advice. Options can lose the whole
premium, and sold options can lose much more.
