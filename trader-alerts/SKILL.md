---
name: trader-alerts
description: >-
  NSE Price Alerts and market clock for Indian intraday traders. Rings when a
  price, a stop, a target or the opening bell hits, each alert ringing once
  so you are not spammed. Turns the trade plan into three alerts in one go
  (the stop, the target and "flat by 15:10" before the broker's MIS
  square-off), plus above / below levels, percent moves from the previous
  close, any IST time of day, the 09:00 pre-open and the 09:15 opening bell.
  Knows NSE hours (pre-open 09:00-09:08, normal session 09:15-15:30 IST), the
  2026 NSE trading holidays, extra holidays and special sessions such as
  Muhurat trading from your config, cross-checks NSE's own market status,
  fetches free delayed Yahoo quotes for NSE symbols and Nifty (^NSEI), and
  writes trader/prices.json for sizing and the risk check. Use when the user
  says "alert me if Reliance crosses 1,200", "alert me if Nifty falls below
  22,800", "tell me if SBIN hits my stop", "watch my trade", "remind me to
  square off by 3:10", "is market open today", "is NSE open", "aaj market
  khula hai?", "is tomorrow a holiday", "when does the market open", "Muhurat
  trading time", "get me the price of TCS", or right after trader-exit writes
  a plan.
argument-hint: "[NSE symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. Once the entry fills, the SL-M stop and the
LIMIT target placed through trader-broker sit at the broker and do the work;
the alerts tell the user what happened and when it is time to be flat.

`<skill dir>` below is this skill's folder in your agent's skills folder
(where this SKILL.md lives). All times are IST (UTC+05:30, no daylight
saving).

```bash
python3 "<skill dir>/alerts.py" clock                          # NSE open or closed, next open (IST)
python3 "<skill dir>/alerts.py" clock --verify                 # also ask nseindia.com/api/marketStatus
python3 "<skill dir>/alerts.py" add --from-plan                # stop, target, flat by 15:10 from trader/plan.json
python3 "<skill dir>/alerts.py" add --opening-bell             # 09:15
python3 "<skill dir>/alerts.py" add --pre-open                 # 09:00
python3 "<skill dir>/alerts.py" add --symbol RELIANCE --stop 1163.30
python3 "<skill dir>/alerts.py" add --symbol RELIANCE --target 1196.30
python3 "<skill dir>/alerts.py" add --symbol ^NSEI --below 22800   # Nifty 50 as a symbol
python3 "<skill dir>/alerts.py" add --symbol SBIN --pct 2
python3 "<skill dir>/alerts.py" add --at 15:10 --label "flat by 15:10" --symbol RELIANCE
python3 "<skill dir>/alerts.py" check                          # exit 0 = something rang
python3 "<skill dir>/alerts.py" quote RELIANCE,TCS --write     # -> trader/prices.json
python3 "<skill dir>/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "<skill dir>/alerts.py" list
python3 "<skill dir>/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-29 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/config.json` | `flat_by` (default "15:10"), `extra_holidays`, `special_sessions` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

`trader/prices.json` looks like this (times IST):

```json
{"as_of": "2026-10-29T10:30:00+05:30", "source": "yahoo", "note": "...",
 "prices": {"RELIANCE": 1167.4},
 "detail": {"RELIANCE": {"price": 1167.4, "prev_close": 1158.0, "time": "2026-10-29T15:29:03+05:30",
                         "source": "yahoo", "fetched": "2026-10-29T10:30:00+05:30"}}}
```

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day | once per trading day |
| `opening_bell` | NSE's normal session is open (09:15) | once per trading day |
| `pre_open` | the pre-open has started (09:00), checked before 09:15 | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A short plan
(`"side": "sell"`) gets its stop as an `above` rule and its target as `below`.
Prices print in rupees with Indian grouping (₹1,23,456.50); an index like
`^NSEI` prints in points.

## Quotes

- Yahoo Finance chart JSON: `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`,
  an unofficial free endpoint, one request per symbol, half a second apart.
  NSE symbols get `.NS` (M&M becomes `M%26M.NS`); `^NSEI`, `^NSEBANK` and
  `^INDIAVIX` are used as they are. On a 429 it waits and tries once more;
  on a 429 or a 5xx after that it tries `query1`.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  trader-risk only uses one under 15 minutes old.
- If Yahoo is down, read the price from the broker's own terminal or app and
  write it into `trader/prices.json` as
  `{"as_of": "2026-10-29T10:30:00+05:30", "prices": {"RELIANCE": 1167.4}}`.

## The NSE clock

Capital Market segment, Monday to Friday: pre-open 09:00-09:08 (order entry;
the open price is discovered by 09:08), normal session 09:15-15:30 IST.
Intraday (MIS) positions are squared off by most brokers around 15:15-15:25,
so plans here are flat by 15:10.

2026 NSE trading holidays (from NSE's holiday master): Jan 15 (Municipal
Corporation Election, Maharashtra), Jan 26 (Republic Day), Feb 15
(Mahashivratri, Sunday), Mar 3 (Holi), Mar 21 (Id-Ul-Fitr, Saturday), Mar 26
(Shri Ram Navami), Mar 31 (Shri Mahavir Jayanti), Apr 3 (Good Friday), Apr 14
(Dr. Baba Saheb Ambedkar Jayanti), May 1 (Maharashtra Day), May 28 (Bakri
Id), Jun 26 (Muharram), Aug 15 (Independence Day, Saturday), Sep 14 (Ganesh
Chaturthi), Oct 2 (Mahatma Gandhi Jayanti), Oct 20 (Dussehra), Nov 8 (Diwali
Laxmi Pujan, Sunday), Nov 10 (Diwali-Balipratipada), Nov 24 (Prakash Gurpurb
Sri Guru Nanak Dev), Dec 25 (Christmas).

- **Muhurat trading** on Diwali (Sunday 8 Nov 2026) is a special session whose
  timing NSE announces separately. Once it is out, add it to
  `trader/config.json`:
  `"special_sessions": [{"date": "2026-11-08", "open": "18:00", "close": "19:00", "name": "Muhurat trading"}]`
  (use NSE's announced times; these are only an example). Its pre-open is
  the 15 minutes before.
- **Unscheduled closures** go in `"extra_holidays": ["YYYY-MM-DD"]`.
- **2027 and later**: NSE has not published the list yet. For those dates the
  clock says the calendar is unknown and treats weekdays as open, with a
  warning. Run `clock --verify` to see what NSE itself reports right now.
- India has no early-close days; there is no daylight saving.

## Steps

1. "Is market open today?": run `clock` (add `--verify` to cross-check
   with nseindia.com). Answer in one line with the reason and the next open.
2. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by
   time, all for the plan's date.
3. Run `check` while the trade is open: every few minutes from your agent's
   loop or scheduler. Each run fetches only the symbols in today's rules and
   rings each alert once. A check reads the latest delayed quote; it is not a
   live feed, and a price can pass a level between two checks.
4. Tell the user what rang in one line each. A stop or target alert means the
   SL-M or LIMIT leg has probably filled: check the position with the broker
   and record the fill with trader-broker. "Flat by 15:10" means close
   anything still open: trader-broker `flatten`, then trader-risk, then the
   user's yes. Never act for them.

## Example

RELIANCE on Thursday 2026-10-29: entry 1,171.40 at 09:30, stop 1,163.35
(opening-range midpoint), target 1,183.50 (1.5R), flat by 15:10.
`add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target` and
`reliance-flat-by` for that day. A `check` reads the real 15:29 quote,
1,167.40, between the stop and the target:

```
NSE OPEN (2026-10-29 10:30 IST, normal session).
Nothing hit. 5 rule(s) watched today, RELIANCE ₹1,167.40.
```

At 15:10:

```
ALERT Flat by 15:10: it is 15:10 IST (set for 15:10), the market closes at 15:30. If RELIANCE is still
open, close it before the broker's intraday square-off: trader-broker flatten --symbol RELIANCE builds
the exit order, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell` the bell rings at 09:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
