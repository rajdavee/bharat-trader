---
name: trader-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from trader-exit (entry, stop,
  target, flat-by), rebuilds the rest of the day 10,000 times from the
  stock's own 5-minute bars over its last ~60 NSE sessions (09:15 to 15:30
  IST), and re-runs the same rule on every past session. Prints the odds of
  target first, stop first, or neither by the 15:10 flat-by, where the price
  sits then, the average R gross and net of brokerage, STT and other
  charges, the history, and one honest verdict, then hands off to
  /trader-budget. Use when the user says "run the time machine", "replay
  this trade", "what are the odds", "simulate this trade", "monte carlo",
  "backtest this ORB on HDFCBANK", "how often does this hit the target",
  "is this a coin flip", "is this intraday setup worth it after charges",
  or after /trader-exit writes a plan, before /trader-budget.
argument-hint: "[path to plan.json, or NSE SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
sessions, how often does this plan hit the target first, the stop first, or
neither before you square off? It answers with 10,000 replays and with the
real record of the same rule on every past session, in R before and after
charges. It is a simulation built from the stock's own recent bars, not a
prediction. Day trading loses most people money, and a good-looking replay
does not change that.

All prices are in rupees on the NSE cash market. All times are IST
(UTC+05:30, no daylight saving). `<skill dir>` below is this skill's folder
in your agent's skills folder (where this SKILL.md lives).

```bash
python3 "<skill dir>/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "<skill dir>/timemachine.py" run --write            # also save trader/timemachine.json
python3 "<skill dir>/timemachine.py" run --until 15:00      # end each replay earlier than the flat-by
python3 "<skill dir>/timemachine.py" run --json
python3 "<skill dir>/timemachine.py" run --symbol HDFCBANK --date 2026-10-26 \
    --entry 692.70 --stop 689.90 --target 698.30 --entry-time 10:10
python3 "<skill dir>/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (shares for the charges), `--no-charges`, `--config PATH`,
`--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`), `--now
"2026-10-26 10:15"` (IST) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time ("10:10") / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by, charges.qty |
| `trader/config.json` | `flat_by` (15:10), `no_entries_after` (14:45), `tick_size` (0.05), `include_charges`, `charges`, `product`, max loss settings, `extra_holidays`, `special_sessions`. Missing keys take the defaults |
| Yahoo chart API | one request: `SYMBOL.NS`, 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, charges, mc, history}` plus the verdict. `mc` holds the odds, average R gross and net, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the flat-by |

## How it works

1. **The pool.** The NSE session is 75 five-minute bars, 09:15 to 15:30
   IST (the last bar starts 15:25). It keeps past sessions before the plan's
   date that have every bar from 09:15 up to the flat-by. Sessions with a
   gap, special sessions (Muhurat trading) and today are left out, and
   listed. Yahoo leaves the 15:15 to 15:25 bars of most NSE sessions empty,
   so a flat-by after 15:15 usually leaves too few sessions; it says so.
   Every bar becomes four ratios to the bar before it: open, high, low and
   close over the previous close.
2. **One replay.** Start at the entry price. For each bar left until the
   flat-by, pick a past bar from the same time of day (plus or minus 2 bars)
   at random, and scale it to the current price. Bar by bar, it checks the
   stop first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at the flat-by (`flat_by` from the
   plan, 15:10 by default, or `--until`) and you square off there. Green,
   which means target first or above the entry at the flat-by. R is the
   target's R at the target, -1 at the stop, and otherwise the distance from
   the entry at the flat-by divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **Net of charges.** Each replay's R also has its round-trip charges taken
   off: brokerage, STT, exchange fee, SEBI fee, stamp duty and GST for that
   exit price, on the plan's quantity (or the most your max loss per trade
   allows), divided by the rupee risk. Charges are an estimate from the
   config rates: check them against your broker's.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions, the entry
   bar closing by 14:45 (`no_entries_after`). Stops round to the tick away
   from the entry, targets to the nearest tick. That is what really
   happened, not a simulation.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither, the average R when its sign goes against the lean,
   and a line when the charges turn a positive gross average R negative.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 usable sessions, when the entry bar
is not in the data (wrong date, not closed yet, a future day), when the
plan's day is a weekend, an NSE holiday or a special session, or when the
plan does not make sense (a stop above a buy's entry, an entry off the
5-minute grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. It ends each replay at the plan's
   flat-by (15:10 by default), before the broker's MIS auto square-off.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by, the average R gross and net of
   charges, the history line, then the verdict line and any line under it
   exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If charges turn the average R negative, say that too. If
   the history and the replays disagree, say so.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. Quarterly results, news and circuit limits can do things
   those sessions never did. Check `/trader-news` for the stock before going
   on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example

HDFCBANK, Monday 2026-10-26. The 15-minute opening range is ₹687.85 to
₹692.00. The 10:05 bar closes above it: entry ₹692.70 at 10:10 IST (bar
10), stop ₹689.90 (the range midpoint, rounded down to the tick), target
₹698.30 (2R, risk ₹2.80 a share), flat by 15:10.

```
10,000 replays of the rest of the session, 10:10 to 15:10 IST (60 five-minute bars), seed 19.
  target first   19.7 %
  stop first     64.0 %
  neither        16.2 %   still open at 15:10, out at the flat-by
  green          31.6 %   target first, or above the entry at 15:10
  Price at 15:10: median 691.96, 90 % of replays between 683.87 and 700.59.
  Average R: -0.170 gross (R = the 2.80 risk: +2R at the target, -1R at the stop).
  Average R: -0.432 net of charges (₹53.15 at the target, ₹52.77 at the stop, round trip on 72 shares, MIS).
History: the same rule (15-min opening range, stop at the opening-range midpoint, 2R target) on each of
those 55 real sessions fired 33 times: 5 target, 18 stop, 10 neither. Average R -0.155 gross, -0.442 net of charges.
Leans toward the stop: at 2R the target has to come first in 33 % of the replays that hit one side;
it did in 24 % (target first 19.7 % vs stop first 64.0 %).
```

At 2R the target only has to come first in a third of the replays that hit
one side. It did in 24 %. The real history agrees, and charges of about ₹53
a round trip on a ₹2.80 stop make it worse. That is the honest answer for
this setup, and it is what the user should hear before `/trader-budget`
sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Always give the net-of-charges average R next to the gross one.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never on every trade and never live.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L. It never uses
a bar that has not closed yet, and never mixes today's bars into the pool.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
