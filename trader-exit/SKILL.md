---
name: trader-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop loss and your target
  before you enter, so you know when to get out. Reads the entry from
  trader-entry (or a rupee price you give), puts the stop at the
  opening-range midpoint by default (or the range low, your price, a percent,
  or a multiple of the ATR), rounds it down to the NSE tick for its price band,
  and sets the target at 2R (or 1R, or your price), rounded to the nearest
  tick. Estimates round-trip charges (brokerage, STT, exchange fee, SEBI fee,
  stamp duty, GST) and shows reward:risk gross and net of them. Refuses a
  stop at or above the entry, a target at or below it, or a stop too tight to
  survive the spread, and says why. Flat by 15:10 IST, before the broker's
  MIS auto square-off. Writes trader/plan.json for the Time Machine, the
  Budget Manager and Price Alerts. Long side only. Use when the user says
  "where's my stop", "set my SL and target", "stop loss for RELIANCE",
  "plan the exit", "when do I get out", "what's my R", "risk reward",
  "intraday SL", "charges on this trade", or right after /trader-entry
  triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X] [--qty N]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit limit or a fast market can fill past it. Day trading loses
most people money, and a neat plan does not change that.

All prices are in rupees on the NSE cash market. All times are IST
(UTC+05:30). `<skill dir>` below is this skill's folder in your agent's
skills folder (where this SKILL.md lives).

```bash
python3 "<skill dir>/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "<skill dir>/exit.py" plan --stop or-mid --target 1R --write
python3 "<skill dir>/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "<skill dir>/exit.py" plan --symbol HDFCBANK --entry 692.70 --or-high 692.00 --or-low 687.85
python3 "<skill dir>/exit.py" plan --symbol RELIANCE --entry 1452.30 --setup level --stop pct:1 --tick 0.10
python3 "<skill dir>/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:05`, `--qty 50` (shares for the charges
estimate), `--no-charges`, `--tick 0.10`, `--json`, `--now "2026-10-26
10:10"` (IST), `--entry PRICE` with a waiting entry.json to plan ahead,
`--setup level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, IST time ("10:10"), bar, opening range |
| `trader/config.json` | `flat_by` (15:10), `broker_profile`, `square_off_margin_min` (5), `fno_stocks`, `tick_size` ("auto"), `min_reward_risk` (1.5: a note here, trader-risk enforces it), `include_charges`, `charges`, `product`, `max_loss_per_trade`, `max_order_value`, `extra_holidays`, `special_sessions`. Missing keys take these defaults |
| Yahoo chart API | only for an `atr:K` stop: `SYMBOL.NS` 5-minute bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk gross and net, the charges estimate, flat by. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout; the range is the 09:15, 09:20 and 09:25 bars) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Ticks.** The stop rounds DOWN to the tick, away from the entry. The
  target rounds to the NEAREST tick. Risk = entry minus stop. Target = entry
  + R x risk. `2R` by default; `1R`, `1.5R` or `price:X` also work. The tick
  is `tick_size` in the config, `"auto"` by default: NSE sets each stock's
  tick by price band, and auto rounds to the coarsest tick of the band
  (₹0.05 under ₹1,000, ₹0.10 under ₹5,000, ₹0.50 under ₹10,000, ₹1 under
  ₹20,000, ₹5 above), which is always a valid price. A number in `tick_size`
  (or `--tick`) overrides it.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under the larger of 0.05% of the price and two ticks: too
  tight to survive the spread, normal wiggle would stop you out. An
  entry.json that has not triggered (unless you give `--entry`). A short:
  this planner does longs only. A day NSE is shut (weekend or NSE holiday).
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. Brokers square off intraday (MIS)
  positions on their own and often charge for it, so the plan exits first:
  never later than 5 minutes before the earlier of the stock's end of
  continuous trading and your `broker_profile`'s square-off (Zerodha 15:12,
  Upstox 15:10, Dhan 15:10, Angel One 15:10, Groww 15:05 for F&O stocks). A
  later flat-by is pulled in, with a note.
- **F&O stocks and the closing auction.** A stock in NSE's F&O list stops
  continuous trading at 15:15 from 3 Aug 2026 (15:30 from 7 Dec 2026); the
  Closing Auction Session then sets its close, and it takes no stop-loss
  orders. The plan gets a `closing_auction` block and a line saying so. A
  flat-by outside the stock's continuous session (09:15 to 15:15 for an F&O
  stock today, 09:15 to 15:30 for the rest) is refused, and an entry at or
  after the flat-by time is refused. With no F&O list (trader/fno_stocks.json
  or `fno_stocks`), every stock is treated as an F&O stock. On a special session in
  the config (Muhurat trading) it moves to 5 minutes before that session's
  close. In a year with no holiday table here (2027 is not published yet) it
  treats weekdays as open and says so.

## Charges

With `include_charges` on (the default), it estimates the round-trip charges
for the quantity you give with `--qty`, or else the most shares your max
loss per trade allows at this risk, capped by `max_order_value`. The parts:
brokerage on each order (0.03% capped at ₹20), STT on the sell side
(0.025% intraday), exchange transaction charge (0.00307%), SEBI fee (₹10 a
crore), stamp duty on the buy side (0.003% intraday) and 18% GST on
brokerage, exchange and SEBI fees. CNC (delivery) uses 0.1% STT on both
sides and 0.015% stamp duty. These are typical discount-broker rates: check
them against your broker's and set `charges` in the config. Net reward:risk
= (reward minus charges at the target) / (risk plus charges at the stop).

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk gross and net of
   charges, and the flat-by time.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. If charges take a big bite out of the reward (net reward:risk well under
   gross), say so plainly: a tight stop on a small position pays a lot of its
   edge in charges.
6. Say once that the stop is a plan, not a guarantee.
7. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time.

## Example

HDFCBANK, Monday 2026-10-26. The opening range (09:15 to 09:30) is ₹687.85
to ₹692.00. The 10:05 bar closes above it at ₹692.70: entry at 10:10 IST
(bar 10). `exit.py plan --write`:

```
Exit plan: HDFCBANK (NSE) long, Mon 2026-10-26, opening-range breakout on 5-minute bars
  Entry         ₹692.70 at 10:10 IST (bar 10)
  Stop          ₹689.90  opening-range midpoint: (₹692.00 + ₹687.85) / 2 = ₹689.925, rounded down to the ₹0.05 tick
  Target        ₹698.30  2R (two times the risk)
  Risk a share  ₹2.80 (0.40% of the entry)
  Reward:risk   2.0 gross, 1.38 net of charges
  Charges       ₹53.27 at the target, ₹52.88 at the stop, round trip on 72 shares (MIS, the most your max loss per trade allows)
  Flat by       15:10 IST, win or lose: no overnight hold
  F&O stock     continuous trading ends 15:15, then the closing auction 15:15-15:35 IST: no stop-loss orders there
```

72 shares: a ₹1,000 max loss allows floor(1000 / 2.80) = 357, but a ₹50,000
max order value caps it at floor(50000 / 692.70) = 72. The 1R target would
be 692.70 + 2.80 = ₹695.50.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk,
  estimated charges. It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Hinglish

Plain words in Hinglish (Hindi in Roman script, trading words in English): `plan` and `show` take `--lang hi-en` for the plan summary, or set `"lang": "hi-en"` in `trader/config.json` for every run. Numbers, ₹, IST, tickers and `--json` output stay the same. See [docs/hinglish.md](../docs/hinglish.md).

```bash
python3 trader-exit/exit.py show --lang hi-en
```

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
