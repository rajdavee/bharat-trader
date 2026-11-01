# The auto-pilot (paper only)

`trader-autopilot` runs the co-trader loop by itself on the **paper** account.
Its job is narrow: give every strategy that trader-lab has validated a real,
after-charges forward record, built on the NSE clock without anyone at the
keyboard, so that promotion and demotion are judged on what the paper account
actually got. It is not a way to make money, and it cannot be pointed at a
live account.

Education and tooling, not financial advice.

## The safety model

**Paper only, enforced in code at four places.**

1. `autopilot.py` checks the config with trader-broker's own `mode_of` before
   it takes the lock or writes anything, and again before every step. A
   config that is not the plain paper broker (`"paper": false`, a live
   `broker`, a `paper`/`broker` mismatch, or `"shadow": true`) is refused
   with the reason; a config switched to live while the loop runs stops it
   before its next action.
2. It runs only these trader-broker commands: `confirm` (with
   `--actor autopilot`), `fill`, `settle` and `adjust` (which writes a
   close-all proposal and places nothing). Any other command, `place` and
   `cancel` included, is refused by the tool itself.
3. trader-broker refuses `confirm --actor autopilot` on a live config before
   anything is recorded.
4. trader-broker's `place` refuses any recorded yes whose actor is not the
   user, so even a hand-edited state file cannot send an auto-pilot order live.

Live trading is the co-pilot (a separate decision, issue #46): every live
order needs the user's own typed yes.

**Every order still goes through the whole chain.** trader-budget sizes it
(with the strategy's capital bucket as the capital), trader-risk checks that
exact order with every rule in your config (the loss at the stop with
charges, the daily limits, the entry window, position size, cash, circuit
breakers, cooling-off, the kill switch and the order rate), and the paper
broker records the yes and fills it. The auto-pilot's working files live in
`trader/autopilot/`; your own `orders.json` and `risk.json` are never touched.

**The machine yes.** Starting the auto-pilot (the command in your terminal,
or the typed words `start paper` in the UI or from an assistant) is your yes
for the paper orders it drafts inside your limits. Each one is recorded word
for word as `yes (autopilot machine yes, paper account)` with
`"actor": "autopilot"` in `trader/state/broker.json`, so the account always
shows who confirmed what.

**Never RESEARCH.** Only PROMOTED and WATCH candidates are traded. A config
asking for RESEARCH is refused, and a RESEARCH candidate is skipped even if
one is offered.

**The kill switch** (`check.py kill`) stops new entries, cancels the working
ones and flattens every auto-pilot position on paper (an F&O basket at that
day's close, its prices being end-of-day).

## A day, step by step

| time (IST) | step |
| --- | --- |
| 09:00 | pre-open: trader-doctor's data check (live only), the regime (trader-regime), a trader-archive update if configured, the co-trader brief |
| from `no_entries_before` | every `cadence_min` minutes until the entry cut-off: the co-trader; each PROMOTED or WATCH candidate of an enabled strategy is sized, risk-checked and placed (a LIMIT at the plan's entry with its SL-M stop and LIMIT target) |
| every 5-minute bar | fills and exits on the completed bar (below) |
| the flat-by | MIS positions out at the last bar's close less slippage (15:10, pulled in for F&O stocks and your broker_profile) |
| after the close | end of day (below) |

**Fills, the same rules as trader-lab.** A LIMIT entry fills when a bar
trades through it, at the open when it opened better; one that opens through
the stop or the target is skipped. Then, bar by bar: the stop (a gap fills at
the open less slippage), the target (a gap above fills at the open), the
trailing stop if configured, the strategy's own exit rule and its maximum
holding (out at the next bar's open). Market and stop fills pay the spec's
slippage (3 bps or a tick by default); limits fill at their price. Charges are
the paper broker's own, with your broker_profile. Delivery (CNC) positions are
held overnight; a later day's exit goes out as a new exit order (trader-risk:
an exit always passes; the machine yes; the fill).

**F&O baskets** run on end-of-day prices, exactly like the lab's options
model: the decision is taken in the entry window (trader-risk on the basket as
one position, with the spec's own stop as its `risk_rule`), the basket fills
at the close on the session's settle prices from trader-archive (LIMIT legs
the close did not reach stay unfilled and the basket is cancelled), is marked
at each close against the spec's profit target, stop and days-before-expiry
exit (closed as one basket), and is cash-settled at expiry. Without the day's
F&O bhavcopy in the archive a basket is neither filled nor marked, and the
timeline says so.

## How the forward record feeds promotion

At the end of each day:

1. Each strategy's closed paper trades go to
   `trader/autopilot/records/<strategy>.json` (entry, exit, why, exact
   charges, net, R).
2. The same trades are written into the strategy's trader-lab forward record
   (`trader/lab/<name>/forward.json`). A paper fill **replaces** the lab's
   simulated trade for the same signal (same stock, same signal day, same
   spec), so there is one trade per signal and it is the one the paper account
   got, slippage and all.
3. trader-lab's own forward test re-runs on the day's bars (`lab.forward`):
   it settles signals the auto-pilot did not trade, and re-decides the
   verdict with the lab's pre-registered rules: WATCH stays WATCH until
   `min_forward_trades`, then PROMOTE if the record made money after charges
   (and its profit factor is at least 1), else REJECT; a PROMOTE whose rolling
   record decays (`demote_window`, `demote_below_r`, `demote_drawdown_mult`)
   becomes DEMOTE, which sticks until the spec changes.
4. The co-trader's calibration resolves its logged probabilities from the
   paper fills first and demotes a strategy whose predictions were
   overconfident (Brier score, reliability; at least 20 outcomes).

The end of day also runs trader-journal's own sync (`journal.sync`), so
the auto-pilot's paper trades are journal entries like any other (their setup
is `cotrader-watch:<strategy>` or `cotrader-promoted:<strategy>`, and each
yes is labelled with its actor), with the journal's statistics, rule-break log
and counterfactual ledger.

The day's report shows every verdict before and after, and the co-trader only
offers PROMOTED and WATCH strategies, so a demoted strategy stops trading the
next session by itself.

## Commands

```bash
python3 trader-autopilot/autopilot.py start                       # the loop; Ctrl-C, SIGTERM or stop end it
python3 trader-autopilot/autopilot.py start --once                # one step (cron-friendly)
python3 trader-autopilot/autopilot.py start --dry-run             # decide and record, place nothing
python3 trader-autopilot/autopilot.py stop
python3 trader-autopilot/autopilot.py status --json
python3 trader-autopilot/autopilot.py report --date 2026-09-21
python3 trader-autopilot/autopilot.py replay --date 2026-09-21 --to 2026-09-25 --data tests/fixtures/trader-lab
python3 trader-autopilot/autopilot.py capture                     # the intraday option-chain capture job
```

At the end of each day the report has a **Forward tests** section (each
enabled strategy's record against its backtest's predicted range, decay
alerts with a DEMOTE recommendation, trades to PROMOTE) and the capture
job's health. On the live clock the loop also keeps trader-archive's
`capture-chains` running in market hours. Both are explained in
[forward-tests.md](forward-tests.md); notifications for fills, exits, locks,
decay and capture failures in [notifications.md](notifications.md).

The loop wakes 20 seconds after each 5-minute bar closes. Its state is saved
after every action, and the intent is written before each broker call, so a
crash resumes without a double order or a double fill; `trader/autopilot/lock.json`
keeps one loop per folder.

## Replay

`replay --date D [--to D2]` is the test harness and the demo. It copies your
config and the enabled strategies' lab specs into
`trader/autopilot/replays/<D>/`, re-runs the lab as of the close before D (so
the replay never sees a later verdict), starts a fresh paper account from
your capital and steps a simulated clock every 5 minutes from 09:00 to 15:45
through each session. Your own account is never touched.

On the repository's fixtures (real NSE bars), a lenient-gate SMA20 rule
fired on BHARTIARTL and AXISBANK at the 18 Sep 2026 close; replayed on
21 Sep as an intraday trade, both LIMITs filled at the 09:30 bar, both were
flattened at 15:10, and the day closed at -₹255.90 after ₹38.10 of charges.
Held as delivery (the rule's own product), both were stopped out on 24 and
25 Sep. That is what a forward record is for.

## The config

See [trader-autopilot/SKILL.md](../trader-autopilot/SKILL.md) for every key.
A minimal block:

```json
"autopilot": {"strategies": {"sma20-swing": {"capital": 60000, "max_positions": 2}}}
```

## What it does not do

- It never trades live, and there is no switch to make it.
- It does not invent strategies: only what trader-lab validated.
- A paper fill is a model. Real orders can fill worse, partly or not at all,
  and a gap can jump a stop. F&O baskets use end-of-day settle prices.
- A few days or weeks of paper results prove little either way; the lab's
  gates and the minimum forward trades exist for that reason.

Education and tooling, not financial advice.
