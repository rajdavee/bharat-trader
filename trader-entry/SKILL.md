---
name: trader-entry
description: >-
  Entry Timer for NSE intraday trades. Watches a setup bar by bar and tells
  you the moment to get in. Reads free Yahoo 1-minute or 5-minute bars of an
  NSE stock (SYMBOL.NS) in IST, builds the opening range (the 09:15, 09:20 and
  09:25 bars by default) and calls the entry on the first completed bar that
  CLOSES above the range high, or above a level you give it (like a 20-day
  high from the scanner), with an optional volume check. Knows the 09:15 to
  15:30 session, the pre-open, the Closing Auction Session that ends
  continuous trading early for F&O stocks (15:15 from 3 Aug 2026, 15:30 from
  7 Dec 2026), your broker's MIS square-off, NSE holidays and Muhurat.
  Says waiting and how far in rupees, triggered with the time and the price,
  expired after 14:45, or not open yet, and writes trader/entry.json for
  /trader-exit. Long side only. It never places an order. Use when the user
  says "tell me when to get in", "watch RELIANCE", "has HDFCBANK broken
  out", "did it break the opening range", "ORB entry", "entry signal", "time
  my entry", "intraday entry", "check my watchlist", or after
  /trader-scanner and /trader-news, before /trader-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads NSE bars, 1-minute or 5-minute, and waits for each one to finish. A
price that pokes above the high in the middle of a bar is not an entry. A bar
that CLOSES above it is. Then it says the time and the price, writes them
down for the Exit Planner, and stops. It never buys anything. Day trading
loses most people money (SEBI's own studies of intraday traders say so), and
a clean signal does not change that.

`<skill folder>` below is this skill's folder (where this SKILL.md lives,
inside your agent's skills folder).

```bash
python3 "<skill folder>/entry.py" check RELIANCE                  # 15-minute opening range, 5-minute bars
python3 "<skill folder>/entry.py" check RELIANCE --write          # also save trader/entry.json
python3 "<skill folder>/entry.py" check HDFCBANK --setup level --level 690
python3 "<skill folder>/entry.py" check M&M --interval 1m --min-volume-x 1.5
python3 "<skill folder>/entry.py" watch RELIANCE --until 14:45    # one line per closed bar, stops on the trigger
python3 "<skill folder>/entry.py" watchlist                       # every hit in trader/watchlist.json
```

Quote `M&M` in a shell (`"M&M"`), or the `&` starts a background job.
Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-29 10:15"` (IST) to replay a day. `watch`
with `--now` or `--offline` runs on a simulated clock and replays the day bar
by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`, NSE symbols), for `watchlist` |
| `trader/config.json` | optional: `no_entries_before` (09:30), `no_entries_after` (14:45), `flat_by` (15:10), `broker_profile`, `square_off_margin_min` (5), `fno_stocks`, `extra_holidays`, `special_sessions`, IST. Missing keys take the defaults |
| `trader/fno_stocks.json` | NSE's F&O stock list (`nseindia.com/api/master-quote`), refreshed once a day when the check is online; `--offline` only reads it |
| Yahoo chart API | today's bars, `RELIANCE.NS?range=1d&interval=5m` (or `1m`; M&M is `M%26M.NS`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, exchange, setup, status, time (IST HH:MM), bar, price, level, opening range |

## The rule

- **Session.** NSE cash market, IST (UTC+05:30, no daylight saving). The
  pre-open call auction takes orders 09:00 to 09:08 (09:10 from 7 Sep 2026);
  the normal session runs 09:15 to 15:30, Monday to Friday. Before 09:15 the
  status is waiting, phase pre-open.
- **F&O stocks close early.** A stock in NSE's F&O list stops continuous
  trading at 15:15 from 3 Aug 2026 (15:30 from 7 Dec 2026) and gets its
  closing price in the Closing Auction Session. Its session ends there, the
  check says so, and `--json` has `segment`, `closing_auction` and the safe
  `flat_by`. With no F&O list yet, every stock is treated as one (the safe
  side). The dated table with its NSE and SEBI sources is in
  docs/data-sources.md.
- **Opening range (orb).** The high and low of the first 15 minutes: the
  09:15, 09:20 and 09:25 five-minute bars, or fifteen 1-minute bars. The
  level to beat is the range high. The range is set at 09:30.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time in IST (the 09:35 bar closes at
  09:40). The price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:49:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Yahoo reports 0 volume
  on the NSE 09:15 bar, so the average uses the range bars that have one.
- **Time limits.** No entries before 09:30 or after 14:45 (config), and
  never later than 15 minutes before the safe flat-by: 5 minutes before the
  earlier of continuous trading's end and your `broker_profile`'s MIS
  square-off (Zerodha 15:12, Upstox 15:10, Dhan 15:10, Angel One 15:10, Groww
  15:05 for F&O stocks). Plans aim to be flat by 15:10 or that earlier time.
- **Holidays.** Weekends and the NSE trading holidays for 2026 are closed
  (no-data, with the next open). The 2027 list is not published yet: for a
  year with no table it warns and treats weekdays as open; add holidays with
  `extra_holidays`. There are no early-close days on the NSE.
- **Special sessions.** Muhurat trading on Diwali is announced by NSE each
  year. Add it to `special_sessions` (`{"date": "2026-11-08", "open":
  "18:00", "close": "19:00"}`, example timing only); the range then starts at
  that open and entries run from 15 minutes after it to 15 minutes before the
  close, unless the entry gives its own `no_entries_before` /
  `no_entries_after`.
- **What it says.** Waiting (and how far, in ₹, the last close is from the
  level), triggered, expired, or no data.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first, and glance at the
   NIFTY 50 / BANK NIFTY line from the scanner: a breakout against a falling
   index is a different trade.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, the agent can run `entry.py watch SYMBOL --until 14:45`
   in the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: RELIANCE, Thu 2026-10-29

Real 5-minute bars (`tests/fixtures/trader-entry/RELIANCE-2026-10-29-5m.json`).
The opening range is ₹1,156.20 to ₹1,170.50. The 09:30 bar went up to
1,171.40 but closed at 1,169.50, under the high: not a trigger. At 09:38 the
09:35 bar is still open, so it waits:

```
WAITING  last close ₹1,169.50 (09:30 bar), ₹1.00 under the range high ₹1,170.50 (-0.09%).
The 09:35 bar is still forming (₹1,174.30 so far, closes 09:40); it does not count until it closes.
```

At 09:40 the bar has closed:

```
RELIANCE  NSE  Thu 2026-10-29 09:40 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 09:15-09:30: high ₹1,170.50, low ₹1,156.20 (3 of 3 bars).
TRIGGERED 09:40 IST at ₹1,174.30: the 09:35 bar (bar 4) closed above the range high ₹1,170.50 (+0.32%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json). Intraday (MIS) positions are squared off by the broker; plan to be flat by 15:10.
F&O stock: continuous trading ends 15:15, then the closing auction (15:15-15:35 IST); entries until 14:45, flat by 15:10.
```

With `--min-volume-x 1` the 09:35 bar is too thin (0.74x the range's volume)
and the trigger moves to 10:00 at ₹1,173.80 (the 09:55 bar, 2.26x).

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. If the user wants a short, say this pack does not plan
  intraday shorts.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- A stock at its upper circuit (price band) can show a close above the level
  with no sellers; the timer does not know the bands. Check before acting.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
