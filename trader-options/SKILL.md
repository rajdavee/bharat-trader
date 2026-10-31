---
name: trader-options
description: >-
  Option chain analytics for NSE F&O: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
  NIFTYNXT50 and every F&O stock. Reads NSE's option chain for one expiry,
  takes expiries from the exchange's own list and lot sizes from NSE's daily
  file, and works out the forward from put-call parity, our own implied
  volatility and Greeks (Black-76: delta, gamma, theta per day, vega per vol
  point) for every strike, PCR by OI and volume, max pain, call and put walls,
  OI build-up (long build-up, short build-up, long unwinding, short covering),
  the ATM straddle and expected move, the IV smile and skew, and IV rank /
  IV percentile from India VIX history. Writes trader/options.json for the
  strategy builder and the UI. Use when the user says "Nifty option chain",
  "show me the option chain", "Bank Nifty chain", "what is the PCR", "Nifty
  PCR today", "max pain", "max pain for this expiry", "where is the call
  writing", "highest OI strike", "OI build-up", "OI analysis", "short covering
  in Nifty", "put writing at 23000", "support and resistance from OI", "ATM IV",
  "IV of Reliance options", "is IV high", "IV rank", "IV percentile", "India
  VIX", "expected move this week", "straddle price", "Greeks for 23000 CE",
  "delta of my option", "theta decay", "next expiry", "weekly expiry date",
  "lot size of Nifty", or "run the options skill".
argument-hint: "chain|expiries|pcr|maxpain|oi|iv SYMBOL [--expiry DD-Mon-YYYY]"
---

# trader-options

Reads one NSE option chain and turns it into the numbers an options trader
reads, each worked out here rather than copied: NSE's own IV column is
inconsistent between calls and puts (4 to 5 vol points apart at the same
strike) and 0 for strikes it skipped, so this skill never uses it.

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "<this skill folder>/options.py" --help
```

(`<this skill folder>` is wherever this skill sits in your agent's skills
folder.)

## Commands

```bash
python3 "<this skill folder>/options.py" expiries NIFTY                 # the exchange's list, lot per expiry
python3 "<this skill folder>/options.py" chain NIFTY                    # nearest expiry, 15 strikes each side
python3 "<this skill folder>/options.py" chain NIFTY --expiry 01-Dec-2026 --strikes 10
python3 "<this skill folder>/options.py" chain RELIANCE --write         # -> trader/options.json
python3 "<this skill folder>/options.py" pcr BANKNIFTY
python3 "<this skill folder>/options.py" maxpain NIFTY
python3 "<this skill folder>/options.py" oi NIFTY                       # build-up and walls
python3 "<this skill folder>/options.py" iv NIFTY                       # smile, ATM IV, skew, IV rank
```

`--json` prints the analysis machine-readable (rows limited by `--strikes`,
`0` = all). `--write [PATH]` saves the full analysis, every strike, to
`trader/options.json`. `--rate 0.06` overrides the rate for one run.
`--offline DIR` reads saved responses; `--save DIR` keeps what was fetched.

## What it reads and writes

- `https://www.nseindia.com/api/option-chain-contract-info?symbol=X`: the
  expiry list. Expiries are never computed here: holidays move them (NIFTY
  09-Nov-2026 and 23-Nov-2026 are Mondays). A `--expiry` not in the list is refused.
- `https://www.nseindia.com/api/option-chain-v3?type=Indices|Equity&symbol=X&expiry=DD-Mon-YYYY`:
  one expiry per call. A wrong expiry comes back as an empty chain with spot
  0; the tool says so instead of printing zeros.
- `https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv`: lot size per
  underlying **per expiry month** (a revision shows up as different numbers in
  different months). NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120,
  NIFTYNXT50 25, RELIANCE 500 at the time of writing; always read from the file.
- `https://www.nseindia.com/api/historicalOR/vixhistory` (90-day chunks: NSE
  stops at 70 rows a call) and `https://www.nseindia.com/api/allIndices` for
  India VIX, only for `iv` and `--write` on an index.
- `trader/config.json` key `risk_free_rate` (decimal, default `0.0557`).
- Writes `trader/options.json` with `--write`.

About six calls for an index `iv`, three for a `chain`, with a 1.5 s pause
between them and back-off on HTTP 403 and 429.

## Units

- `oi`, `oi_chg` and `volume` in the chain are in **contracts (lots)**.
  Checked: NIFTY 23000 CE OI 1,13,312 contracts x 65 = 73,65,280 units, the
  bhavcopy's open interest. Multiply by `lot_size` for units (shares or
  index units). Other NSE pages report in shares (bhavcopy OI, the most-active
  options list), so never mix them without converting.
- `bid_qty` / `ask_qty` are in units (multiples of the lot).
- Prices are ₹ per unit; a lot costs price x lot size.

## Every number, explained

- **Spot.** NSE's cash index (or share price) in the chain. It stops at 15:30
  while equity derivatives trade until 15:40, so after 15:30 spot is stale
  against option prices. The tool warns when the chain was taken in that
  window, before 09:15, or more than 5 minutes ago in market hours.
- **Forward.** The price the market implies for expiry, from put-call parity
  at the five strikes nearest the money with a tight quote on both sides:
  F = K + e^(rT) x (call mid - put mid), the median. It absorbs dividends,
  carry and the stale spot. Fallbacks: traded LTPs, then spot (with a warning).
  `basis` is forward minus spot.
- **T.** Calendar minutes from the chain's timestamp to 15:30 IST on expiry
  day, / 525,600. Not whole days: 4 days vs 3.5 days moves a weekly's IV by
  more than a point.
- **Rate.** `risk_free_rate`, default 5.57% (91-day T-bill cut-off 5.5747% at
  the 08-Oct-2026 auction, from a secondary source; set your own). Under
  Black-76 it only discounts, so 1 point of rate moves IV by a few hundredths.
- **Price used.** The bid/ask mid when both sides are quoted and the spread is
  within 30% of the mid; else the LTP if the contract traded today; else none
  (no IV, no Greeks). `price_source` says which.
- **IV.** Black-76 implied volatility on the forward, in % a year, from a
  bracketed Newton solver (bisection fallback, bounds 0.1% to 500%). None
  when the price is at or below intrinsic, above the no-arbitrage ceiling,
  outside the bounds, or under an hour from expiry. With the right forward the
  call and put IVs at the same strike agree: NIFTY 03-Nov 23000 at the 30-Oct
  close, 12.1 and 12.1 (NSE's column: 9.1 and 14.3).
- **ATM.** The listed strike nearest the forward. **ATM IV** is the average of
  its call and put IV.
- **Delta.** Forward delta per unit: the change in price per 1 point move in
  the forward (call 0 to +1, put -1 to 0). It is the hedge ratio against
  futures. Position delta = delta x lot size x lots.
- **Gamma.** Change in delta per 1 point move in the forward.
- **Theta.** ₹ per unit lost (negative) per **calendar** day as time passes,
  forward and IV unchanged. Weekends count. Near expiry it is large and
  unstable; in the last hours read premium vs intrinsic instead.
- **Vega.** ₹ per unit gained per **1 vol point** rise in IV (12% -> 13%).
- **PCR (OI).** Total put OI / total call OI for the expiry; also by volume,
  by OI added today, and near the money (10 strikes each side of ATM). Above
  about 1.2: more puts open (put writers see a floor, or heavy hedging). Below
  about 0.8: more calls (call writers see a cap). A crowd gauge, not a signal.
- **Max pain.** The listed strike where option buyers, together, would be paid
  the least if the underlying settled there: for each candidate K*, sum call
  OI x max(K* - K, 0) + put OI x max(K - K*, 0) over every strike (zero-OI
  strikes included), x lot = ₹. A weak pull, strongest on expiry day.
- **Walls.** The out-of-the-money call strike (at or above the forward) with
  the most OI (resistance: sellers there lose above it) and the
  out-of-the-money put strike with the most OI (support). Deep in-the-money OI
  is left out: it is old positions, not a level being defended. Also the top
  three, and the strikes where the most OI was added today.
- **OI build-up**, per contract, from today's price change and OI change:
  price up + OI up = long build-up; price down + OI up = short build-up
  (writing); price down + OI down = long unwinding; price up + OI down =
  short covering. Short build-up in calls is call writing, in puts put writing.
- **Straddle.** ATM call + ATM put price, and ₹ per lot.
- **Expected move.** One standard deviation to expiry. From the straddle:
  straddle x e^(rT) x 1.2533 (the straddle prices the average move, which is
  0.80 of one SD). From IV: forward x IV x sqrt(T). They should agree within a
  few percent; `low`/`high` are forward -/+ the move. About 68% of the time
  the underlying should finish inside it, if IV is right.
- **Smile and skew.** The IV of the out-of-the-money side at each strike (puts
  below the forward, calls above). The 25-delta risk reversal is the put IV at
  delta about -0.25 minus the call IV at about +0.25: positive means downside
  protection costs more, normal for Indian indices.
- **IV rank / IV percentile** (indices). Against the last 252 sessions of India
  VIX closes: rank = (now - low) / (high - low) x 100; percentile = % of
  sessions that closed below now. VIX is NIFTY's 30-day implied vol, so it is
  a proxy for other indices and for a weekly. For stocks there is no free IV
  history yet (it needs a bhavcopy backfill); the tool says so.

## trader/options.json (schema `bharat-trader/options/1`)

Read by the strategy builder, the local UI and any other program. Plain JSON,
no NaN; missing values are `null`.

```
schema, underlying, kind ("index"|"stock"), expiry (YYYY-MM-DD), expiry_nse (DD-Mon-YYYY),
days_to_expiry, minutes_to_expiry, t_years, asof (chain time, IST), spot, forward,
forward_method ("parity-mid"|"parity-ltp"|"spot"), forward_strikes, basis, lot_size,
rate, rate_source, model, units {...}, atm, atm_iv, atm_iv_ce, atm_iv_pe,
straddle {strike, ce, pe, price, rupees_per_lot},
expected_move {straddle, from_straddle, from_iv, move, pct, low, high},
pcr_oi, pcr_volume, pcr {all: {...}, near_atm: {...}},
max_pain, max_pain_detail {strike, pain, distance},
walls {call, put, calls[], puts[], call_adds[], put_adds[]},
skew {put_strike, put_iv, call_strike, call_iv, risk_reversal},
iv_rank {current, rank, percentile, low, high, sessions, from, to, source, vix_live},
smile [{strike, type, iv}],
rows [{strike, ce: SIDE|null, pe: SIDE|null}]   every listed strike, ascending
  SIDE = {ltp, change, bid, ask, bid_qty, ask_qty, oi, oi_chg, volume, nse_iv,
          mid, price, price_source, iv, delta, gamma, theta, vega, buildup}
sources {chain_timestamp, spot, chain, lots}, warnings[], notes[], generated_at
```

`iv` and the Greeks are per unit; `iv` in % a year; `theta` per calendar day;
`vega` per vol point; `oi`, `oi_chg`, `volume` in contracts.

## For other programs

`options.py` can be imported (it has no side effects on import). The pure
functions take and return plain numbers, dicts and lists:
`parse_contract_info`, `parse_chain`, `parse_lots`, `lot_size`,
`parse_vix_history`, `black76_price`, `black76_iv`, `black76_greeks`,
`forward_from_parity`, `years_to_expiry`, `pcr`, `max_pain`, `walls`,
`classify_buildup`, `oi_buildup`, `expected_move`, `iv_rank`, `smile`,
`skew_25d`, `spot_warnings`, and `analyse` (a parsed chain in, the
`options.json` body out). `fetch_contract_info`, `fetch_chain`, `fetch_lots`
and `fetch_vix` take a `Fetcher` and do the network calls.

## Steps

1. If the user did not name an expiry, run `expiries SYMBOL` when the choice
   matters (weekly vs monthly), else let `chain` take the nearest.
2. Run the command that answers the question. Lead with the number asked for,
   then the context: spot vs forward, days left, lot size.
3. Say what each number means in plain words (use the section above), with
   its unit. Put any WARNING line (stale spot, old chain) first.
4. If the user is thinking of a trade, this skill only describes the market.
   A trade still goes `/trader-budget` -> `/trader-risk` -> `/trader-broker`,
   paper by default, and nothing is placed without the user's typed yes.

## Example

`options.py chain NIFTY` on Sunday 01-Nov-2026 (data: Fri 30-Oct 15:40):
spot 23,053.10, forward 23,022.14 (31 points below: spot froze at 15:30),
4 days / 5,750 minutes to expiry, lot 65. ATM 23,000: call and put IV both
12.1%, straddle 232.82 (₹15,133.62 a lot), expected move +/-292 (1.27%):
22,730 to 23,314. PCR OI 1.62, volume 0.99; max pain 23,000; call wall
23,500, put wall 22,000. 23000 PE added 68,220 contracts as its price fell:
put writing. India VIX 15.45: IV rank 34, IV percentile 71 over 246 sessions.

## What it never does

- Never uses NSE's IV column for anything but display in `--json` (`nse_iv`).
- Never computes an expiry date or hard-codes a lot size.
- Never says a PCR, max pain or build-up reading will move the market.
- Never places an order. It reads public data and does arithmetic.

## Safety

Education and tooling, not financial advice. Options can lose the whole
premium, and sold options can lose much more. Any trade still goes
`/trader-budget` -> `/trader-risk` -> `/trader-broker`, paper by default, and
nothing is placed without the user's typed yes for that order.
