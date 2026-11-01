---
name: trader-autopilot
description: >-
  Auto-pilot on PAPER for NSE stocks and F&O baskets: runs the co-trader loop
  by itself on the paper account so every validated strategy builds a real
  forward record. Driven by the NSE clock (IST, holidays, the closing
  auction): a pre-open brief (data check, market regime, co-trader), then
  during the entry window the co-trader's PROMOTED and WATCH candidates only
  (never RESEARCH), each sized by trader-budget inside its strategy's capital
  bucket, checked by trader-risk (every rule, circuit breakers, kill switch,
  order rate) and confirmed with a recorded machine yes clearly labelled
  autopilot; then stops, targets, trailing stops, time exits, the MIS
  flat-by, F&O baskets marked at the close and expiry settlement; and at the
  end of the day the journal hook, calibration scoring, the lab's forward
  record and promote or demote, and a daily report. Resumable after a crash,
  one loop per folder (lock file), and a replay of any past day from the
  archive on a simulated clock. Use when the user says "auto-pilot",
  "autopilot on paper", "run my strategies automatically", "paper trade by
  itself", "khud se paper trade karo", "auto trade on paper", "start the
  auto-pilot", "stop the auto-pilot", "replay yesterday", "how did the
  auto-pilot do today" or "forward test my strategies". It refuses a live
  config and never places a live order.
argument-hint: "[start [--once] [--dry-run] | stop | status | report [--date D] | replay --date D [--to D2]]"
---

# trader-autopilot

The co-trader loop, run by itself, on the PAPER account only. It exists so that
a strategy trader-lab has validated (PROMOTED, or WATCH: gates passed, paper
forward test running) builds a real, after-charges forward record without
anyone at the keyboard, and so promotion and demotion are judged on what the
paper account actually got.

**Paper only, in code.** A config that is not the plain paper broker
(`"paper": false`, a live `broker`, or `shadow`) makes it refuse to start, and
it is checked again before every step: a config switched to live mid-run stops
the loop before it does anything. It runs only trader-broker's paper commands
(confirm with `--actor autopilot`, fill, settle and the F&O close-all
proposal) and never `place`. The broker itself refuses an autopilot yes on a
live config, and `place` refuses any yes that the user did not type. Live
trading is the co-pilot, with the user's own yes for every order.

**Your yes.** Starting the auto-pilot (the command in your own terminal, or
the typed words `start paper` in the UI or from an assistant) is your yes for
the paper orders it drafts inside your limits and its buckets. Each one still
needs trader-risk PASS for that exact order, and each machine yes is recorded
word for word and labelled `"actor": "autopilot"`, so the paper account always
shows which orders you confirmed and which the auto-pilot did. A live order
always needs your own typed yes for that one order.

All amounts are rupees, all times IST. `<skill dir>` is this skill's folder.
It needs trader-cotrader, trader-lab, trader-budget, trader-risk, trader-broker
and trader-alerts next to it; trader-regime, trader-doctor, trader-archive and
trader-journal are used when installed.

```bash
python3 "<skill dir>/autopilot.py" start                    # the loop on the NSE clock; Ctrl-C or stop ends it
python3 "<skill dir>/autopilot.py" start --once             # one step now (for cron), then exit
python3 "<skill dir>/autopilot.py" start --dry-run          # decide and record "would place", place nothing
python3 "<skill dir>/autopilot.py" stop                     # the running loop finishes its step and exits
python3 "<skill dir>/autopilot.py" status [--json]
python3 "<skill dir>/autopilot.py" report [--date 2026-09-21] [--json]
python3 "<skill dir>/autopilot.py" replay --date 2026-09-21 [--to 2026-09-25] [--data DIR]
python3 "<skill dir>/autopilot.py" report --replay 2026-09-21 --date 2026-09-21
```

`--workdir DIR` is the folder holding `trader/`. Bars come from `--data DIR`
(offline files), else the local trader-archive (`trader/archive`,
`$BT_ARCHIVE`, `--archive DIR`), else trader-lab's Yahoo cache.

## The config

An `"autopilot"` block in `trader/config.json` (your own limits there still
apply to every order through trader-risk):

```json
"autopilot": {
  "strategies": {
    "sma20-swing": {"capital": 60000, "max_positions": 2, "max_daily_loss": 1500},
    "orb15-vwap":  {"capital": 40000, "max_positions": 1, "trail_after_r": 1, "trail_lock_r": 0}
  },
  "max_positions": 4, "max_daily_loss": 2500, "cadence_min": 15,
  "tiers": ["PROMOTED", "WATCH"]
}
```

| key | meaning |
| --- | --- |
| `strategies` | the trader-lab strategies it may trade, each with its `capital` bucket (sizing uses it as the capital; positions never use more), `max_positions`, `max_daily_loss` (realised loss today plus the new trade's loss at the stop), optional `max_loss_per_trade`, `product` (MIS or CNC; default the strategy's own), `trail_after_r` / `trail_lock_r` (raise the stop to entry + lock R once the price is after R in profit), `enabled` |
| `max_positions`, `max_daily_loss` | the same, across every strategy |
| `cadence_min` | how often the co-trader is asked in the entry window (5 to 120, a multiple of 5) |
| `tiers` | PROMOTED and/or WATCH. RESEARCH is refused: unproven ideas are never traded |
| `doctor`, `regime_symbols`, `archive_update` | the pre-open steps (doctor and archive update run live only) |
| `calibration_demote`, `forward_update` | the end-of-day steps (both on by default) |

## One day

1. **Pre-open** (from 09:00): trader-doctor's data check, the market regime
   (trader-regime), a trader-archive update if configured, the co-trader brief.
2. **Entries** (from `no_entries_before` to the entry cut-off, the earlier of
   `no_entries_after` and 15 minutes before the safe flat-by): the co-trader
   every `cadence_min` minutes. For each PROMOTED or WATCH candidate of an
   enabled strategy: the bucket checks, trader-budget's size, trader-risk on
   that exact order (written to `trader/autopilot/orders/`, never your own
   `orders.json`), then the machine yes `yes (autopilot machine yes, paper
   account)`, recorded in `trader/state/broker.json` with `"actor":
   "autopilot"`. Orders go in a second apart (the order-rate rule).
3. **Manage**, every completed 5-minute bar: a LIMIT entry fills when a bar
   trades through it (at the open if it opened better), skipped when it opens
   through the stop or the target; then the stop (a gap fills at the open),
   the target, the trailing stop, the strategy's own exit rule and maximum
   holding (at the next bar's open, like trader-lab), MIS out at the flat-by.
   Stop and market fills pay the spec's slippage (trader-lab's model). F&O
   baskets are decided in the window and run on end-of-day prices like the
   lab's options model: they fill at the close on the session's settle
   prices from the archive (a LIMIT the close did not reach stays unfilled and
   is cancelled), are marked at each close against the spec's profit target,
   stop and days-before-expiry exit (closed as one basket: ticket.py adjust
   --close-all, trader-risk, the machine yes), and are cash-settled at expiry.
4. **End of day**: unfilled entries cancelled; delivery positions judged on
   the day's bar (exit rule, maximum holding: out at the next open); each
   strategy's closed paper trades written to
   `trader/autopilot/records/<strategy>.json` and into its trader-lab forward
   record (a paper fill replaces the lab's simulated trade for the same
   signal); the lab's forward test re-run (WATCH becomes PROMOTE once the
   forward record holds up, PROMOTE becomes DEMOTE when it decays); the
   co-trader's calibration scored (overconfident strategies demoted); the
   trade journal synced when trader-journal is installed
   (`journal.autopilot_sync(workdir, day)`); the report
   `trader/autopilot/days/<date>/report.md` and `report.json`.

**Kill switch** (`check.py kill`): no new entries, working entries
cancelled, every auto-pilot position flattened on paper (a basket at that
day's close). **Circuit breakers**, the daily limits and the order rate are
trader-risk's own: a blocked candidate is recorded with the reasons.

## Resuming, the lock, stopping

State is saved after every action in `trader/autopilot/state.json`, with the
intent written before each broker call, so a crash mid-step resumes without a
double order or a double fill. `trader/autopilot/lock.json` keeps one loop per
folder (a lock left by a process that is gone is taken over). `stop`, Ctrl-C
or SIGTERM finish the current step, save and release the lock.

## Replay: a past day on a simulated clock

`replay --date D` copies your config and the enabled strategies' lab runs into
a sandbox (`trader/autopilot/replays/<D>/`), re-runs the lab as of the close
before D (no look-ahead; `--no-relab` skips that and says it may know later
bars), starts a fresh paper account from your capital, and steps the clock
every 5 minutes from 09:00 to 15:45 through each session to `--to`. Your own
account is never touched. It is the test harness and a good demo.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | your limits and the `autopilot` block |
| `trader/lab/<name>/` | the strategies (verdicts, specs) and their forward records |
| `trader/portfolio.json`, `trader/state/broker.json`, `trader/state/trades.json` | the paper account (trader-broker's own files) |
| `trader/state/kill.json` | the kill switch |
| `trader/autopilot/state.json`, `lock.json`, `orders/`, `work/` | the loop's state and its working files |
| `trader/autopilot/days/<date>/timeline.jsonl`, `report.md`, `report.json` | the day's actions and its report |
| `trader/autopilot/records/<strategy>.json` | each strategy's paper forward record (exact charges) |

## How to talk about it

Say plainly that it is paper: no real money, a fill model, and a few days of
results prove little either way. Quote the after-charges numbers, the number
of trades and the lab's verdict next to the paper record. Never present the
auto-pilot as a way to make money, and never suggest pointing it at a live
account: it refuses, by design.

From an AI assistant, the MCP server offers `autopilot_status`,
`autopilot_replay` (a sandbox, read-only for your account) and
`autopilot_start` / `autopilot_stop` (paper only, with the user's typed
confirmation). The UI has an Auto-pilot page.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
