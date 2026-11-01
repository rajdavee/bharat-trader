# Forward tests

A strategy that passes every backtest gate gets the verdict WATCH. It is not
proven: it goes into a paper forward test, and the auto-pilot builds its
record on the paper account. This page explains how that record is judged
against what the backtest predicted, when a decay alert fires, what it
recommends, and how the intraday option-chain capture that feeds future
options research is kept running.

Everything here is read only unless you ask: a decay alert *recommends*
DEMOTE; it is applied only when you run the command yourself, or when you
turn on the auto-pilot's `decay_demote`. Paper trading. Education and
research tooling, not financial advice.

## The dashboard

```bash
python3 trader-lab/lab.py forward-report                 # every forward test, plain words
python3 trader-lab/lab.py forward-report orb15-vwap --json
python3 trader-lab/lab.py forward-report --demote orb15-vwap   # apply a recommended DEMOTE
```

The same report is the **Forward tests** page of the UI (Review group,
`g f`), the `forward_report` MCP tool (read only), and a section of the
auto-pilot's end-of-day report. For each trader-lab strategy with a run
(WATCH, PROMOTE, DEMOTE, or any strategy that has forward trades) it shows:

| part | what it is |
| --- | --- |
| backtest expectation | the run's **out-of-sample** (walk-forward) trades after charges: N trades, mean R, standard deviation, win rate, trades per session |
| paper forward record | the trades in `trader/lab/<name>/forward.json` for the current spec (the auto-pilot's paper fills replace the lab's simulated trade for the same signal): n, mean R with its 95% range, win rate with its Wilson range, net rupees |
| predicted range | where the forward mean R of n trades should fall if the backtest is right (below) |
| inside / below / above | the forward mean against that range; "too few" under 5 trades |
| control chart | a one-sided lower CUSUM of the forward R (below) |
| decay alert | the rule below, with a DEMOTE recommendation for a PROMOTE or WATCH strategy |
| calibration | the co-trader's Brier score for the strategy, from `trader/cotrader/log.jsonl` (outcomes from the paper fills and the lab's forward record; a constant guess at the base rate is shown next to it) |
| timeline | the lab's verdict history (`status.json`) and any co-trader calibration demotion |
| to PROMOTE | trades still needed (`min_forward_trades` from the spec's gates, 20 by default), an estimate of the sessions at the backtest's out-of-sample pace, and whether the lab's rule (mean above 0R, profit factor at least 1) is met |

Edge-hunt hypotheses with a WATCH outcome are listed too: a paper forward
test is their next step once they have a lab spec.

## The statistics, and their thresholds

With the backtest's out-of-sample record (N trades, mean R `mu`, standard
deviation `s`, win rate `p`) and a forward record of `n` trades:

- **Predicted range of the forward mean R**: `mu ± z · s · sqrt(1/n + 1/N)`,
  z = 1.96. A prediction interval: the forward sample's own noise plus the
  uncertainty in the backtest's mean. It is wide for a short record, which
  is the honest answer.
- **Predicted range of the win rate**: `p ± z · sqrt(p(1-p) · (1/n + 1/N))`,
  clipped to 0..1.
- **The forward record's own ranges**: mean R ± z · sd / sqrt(n); the win
  rate's Wilson interval.
- **Control chart**: `z_i = (r_i − mu) / s`, `S_i = max(0, S_(i−1) − z_i − k)`,
  alarm when `S_i > h`, with k = 0.5 and h = 4. A record that really matches
  the backtest raises a false alarm about once in 170 trades; a fall of one
  standard deviation per trade is caught in about 8 trades (the standard
  CUSUM average run lengths).
- **Decay alert**: at least 10 forward trades, and either the running
  forward mean R sat below the predicted range's lower bound for the last 5
  trades in a row, or the control chart alarmed.

Change the thresholds in `trader/config.json` if you have a reason (and say
it, since every change is another chance to fool yourself):

```json
"forward_tests": {"z": 1.96, "min_judge": 5, "decay_min_trades": 10, "decay_run": 5,
                  "cusum_k": 0.5, "cusum_h": 4.0}
```

The lab's own forward test still runs as before: `lab.py forward` (and the
auto-pilot's end of day) moves WATCH to PROMOTE after `min_forward_trades`
trades with a positive mean, and PROMOTE to DEMOTE when the last
`demote_window` trades average at or below `demote_below_r` or the forward
drawdown passes `demote_drawdown_mult` times the out-of-sample one. The
dashboard's decay alert compares the record with the *backtest's own
prediction*, so it can fire earlier, for a strategy whose forward record is
positive but far below what was promised.

## A DEMOTE

```bash
python3 trader-lab/lab.py forward-report --demote orb15-vwap
```

It works only when the dashboard recommends it, and uses the lab's own
status mechanism (the same one the co-trader's calibration uses):
`status.json` becomes DEMOTE with the reason `forward test: ...`. The
co-trader then shows the strategy as RESEARCH, the auto-pilot stops trading
it and the co-pilot will not make live proposals for it. A new run of a
changed spec starts over. In the auto-pilot, `"decay_demote": true` in the
`autopilot` block applies it at the end of the day; it is off by default.

## Intraday option-chain capture

Free historical intraday option prices do not exist for NSE, so the
pack builds its own: trader-archive's `capture-chains` snapshots the option
chain every few minutes through the session. The pre-registered hypothesis
`eh-14-trend-day-buying` (NIFTY weekly ATM options bought on an opening-range
break) uses the chain **captured live from 16 Nov 2026** as its sealed
holdout, so the capture must be running by then.

The auto-pilot manages it (paper or dry run, live clock only; never in a
replay):

- at the pre-open (09:00 IST) it starts `archive.py capture-chains` for the
  configured underlyings, detached, with `--sessions 1` (the capture sleeps to
  09:15, snapshots until 15:30 and exits);
- every step it reads the heartbeat `archive/chain/capture.json` and the lock;
  a heartbeat with no round for `stale_min` minutes in market hours is stale
  (an NSE block pauses the capture 15 minutes, so the default is 20);
- a capture it started that went stale is stopped and restarted; one that
  crashed is restarted; at most `max_restarts` a day, `restart_gap_min`
  apart, then it gives up for the day and says so (a `capture_failure`
  notification). A capture somebody else started is never stopped: a stale
  one is only reported;
- the Forward tests page and `autopilot.py capture` show the heartbeat,
  today's rounds and snapshots per underlying, the strikes in the latest
  snapshot, the gaps between rounds, restarts and the holdout deadline.

```json
"autopilot": {
  "strategies": {"sma20-swing": {"capital": 60000}},
  "chain_capture": {"enabled": true, "symbols": ["NIFTY", "BANKNIFTY"], "every_min": 5,
                    "expiries": 2, "stale_min": 20, "max_restarts": 3, "restart_gap_min": 10}
}
```

`expiries: 2` captures the nearest two expiries: for NIFTY the current and
next weekly; BANKNIFTY has had monthly expiries only since SEBI's 2024 F&O
changes, so it gets the current and next monthly. Each round is one
contract-info call and one chain call per expiry per underlying (6 requests
every 5 minutes by default), with the archive's NSE headers and pauses.

```bash
python3 trader-autopilot/autopilot.py capture             # health
python3 trader-autopilot/autopilot.py capture --json
python3 trader-autopilot/autopilot.py capture --tick      # one supervision step: start or restart if due
```

### Keeping it running

The simplest way is to keep the auto-pilot loop running on a machine that is
on in market hours (`autopilot.py start`). If you do not run the loop, run
`capture --tick` from a scheduler every 5 minutes on weekdays from 09:00 to
15:30 IST: it starts the capture at the pre-open and restarts it if it dies.
Replace the paths with yours (the folder that holds `trader/`, and where you
cloned the repository).

**macOS (launchd)**: `~/Library/LaunchAgents/in.bharat-trader.capture.plist`,
then `launchctl load ~/Library/LaunchAgents/in.bharat-trader.capture.plist`.
launchd uses the Mac's local time: these times assume the Mac is set to IST.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>in.bharat-trader.capture</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/bharat-trader/trader-autopilot/autopilot.py</string>
    <string>capture</string><string>--tick</string>
    <string>--workdir</string><string>/Users/you/trading</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>/Users/you/trading/trader/autopilot/capture-tick.log</string>
  <key>StandardErrorPath</key><string>/Users/you/trading/trader/autopilot/capture-tick.log</string>
</dict></plist>
```

`capture --tick` does nothing outside the session, so running it every five
minutes all day is harmless.

**Linux (systemd user timer)**: `~/.config/systemd/user/bt-capture.service`
and `bt-capture.timer`, then `systemctl --user enable --now bt-capture.timer`
(and `loginctl enable-linger $USER` so it runs while you are logged out).

```ini
# bt-capture.service
[Unit]
Description=bharat-trader option-chain capture supervisor

[Service]
Type=oneshot
WorkingDirectory=/home/you/trading
ExecStart=/usr/bin/python3 /home/you/bharat-trader/trader-autopilot/autopilot.py capture --tick --workdir /home/you/trading

# bt-capture.timer
[Unit]
Description=Every 5 minutes on weekdays, 09:00 to 15:30 IST

[Timer]
OnCalendar=Mon..Fri *-*-* 09..15:00/5:00 Asia/Kolkata
Persistent=false

[Install]
WantedBy=timers.target
```

**Windows (Task Scheduler)**, in PowerShell (the time is the PC's local
time, IST assumed):

```powershell
$a = New-ScheduledTaskAction -Execute "py" -Argument "-3 C:\Users\you\bharat-trader\trader-autopilot\autopilot.py capture --tick --workdir C:\Users\you\trading"
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00am
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 9:00am -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 7)).Repetition
Register-ScheduledTask -TaskName "bharat-trader capture" -Action $a -Trigger $t
```

A laptop that sleeps captures nothing: the gaps show on the Forward tests
page, and a gap in the holdout is a gap in the evidence (it is reported, never
filled in).

## What this does not show

- A paper fill is a model: real fills, queue position and slippage can be
  worse.
- A forward record of a few weeks proves little either way; the ranges say
  how little.
- The predicted range assumes the backtest's out-of-sample trades are a fair
  sample of the future. A regime change breaks that, and so does any
  overfitting the gates did not catch.
- Inside the range is not proof of an edge, and below it is not proof of
  none.

See also [lab.md](lab.md) (the gates and the lab's forward test),
[autopilot.md](autopilot.md), [cotrader.md](cotrader.md) (calibration),
[archive.md](archive.md) (`capture-chains`) and
[notifications.md](notifications.md) (decay and capture alerts).
