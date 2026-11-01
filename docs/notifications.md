# Notifications

The pack can tell you when something needs your eyes: a desktop pop-up on
the machine that runs it, and optionally a short message to a webhook of
your own (for example a phone-push service you run or subscribe to). It is
**off by default**, nothing is sent until you turn it on, and a notification
is information, never an order: nothing can be placed, approved or changed
from one.

## Turning it on

In `trader/config.json`:

```json
"notify": {
  "enabled": true,
  "desktop": true,
  "webhook": false,
  "quiet_hours": ["22:00", "07:00"],
  "min_gap_min": 5,
  "max_per_hour": 12,
  "events": {"autopilot_fill": false}
}
```

| key | meaning (default) |
| --- | --- |
| `enabled` | the master switch (false) |
| `desktop` | a desktop pop-up (true once enabled) |
| `webhook` | a JSON POST to the URL in `BT_NOTIFY_WEBHOOK` (false) |
| `quiet_hours` | `["HH:MM", "HH:MM"]` IST, may wrap midnight; nothing is sent inside (null) |
| `min_gap_min` | per event: at most one notification each N minutes; the ones held back are counted into the next ("+2 more") (5) |
| `max_per_hour` | all events together (12) |
| `events` | `{event: true/false}`; every event is on by default once enabled |
| `timeout_s` | seconds to wait for the desktop tool or the webhook (5) |

The same lock, candidate or proposal is told once a day.

## Events

| event | when | from |
| --- | --- | --- |
| `cotrader_candidate` | new PROMOTED or WATCH candidates were logged | trader-cotrader |
| `autopilot_fill` | an auto-pilot paper entry filled | trader-autopilot |
| `autopilot_exit` | an auto-pilot paper position closed (with the realised P&L after charges) | trader-autopilot |
| `copilot_proposal` | a LIVE co-pilot proposal is waiting for your approval (never the approval phrase) | trader-copilot |
| `risk_lock` | a circuit breaker (losing streak, daily drawdown, cooling-off) locked new entries | trader-risk, trader-autopilot |
| `kill` | a kill switch went on (trader-risk, the auto-pilot's, or the co-pilot's live one) | trader-risk, trader-autopilot, trader-copilot |
| `decay_alert` | a strategy's forward record fell below its backtest's predicted range ([forward-tests.md](forward-tests.md)) | trader-autopilot (end of day) |
| `capture_failure` | the intraday option-chain capture was restarted, gave up, or someone else's capture went stale | trader-autopilot |
| `test` | you asked for a test | `alerts.py notify test`, the UI, `notify_test` |

trader-risk notifies only for the usual layout (`<folder>/trader/config.json`);
the auto-pilot's own risk checks are notified by the auto-pilot. A replay
never notifies (its sandbox config has no `notify` block).

## Desktop

| system | how |
| --- | --- |
| macOS | `osascript -e 'display notification ...'` (allow notifications for Script Editor / your terminal in System Settings if nothing shows) |
| Linux | `notify-send` (package `libnotify-bin` on Debian and Ubuntu) |
| Windows | a toast through PowerShell (Windows 10 and 11) |

Best effort: when the tool is missing, nothing happens and the log says why.

## Webhook

Set the URL in your environment, never in a file the pack writes:

```bash
export BT_NOTIFY_WEBHOOK="https://push.example.org/your-topic"
python3 trader-alerts/alerts.py notify test --channel webhook
```

Each notification is one `POST` with `Content-Type: application/json` and
exactly this body:

```json
{"source": "bharat-trader", "event": "autopilot_exit", "title": "Auto-pilot exit (paper)",
 "text": "AP-20261102-3 closed on paper: SELL 37 SBIN at 818.10 (target); charges ₹41.20; realised ₹170.80 after charges",
 "at": "2026-11-02T13:05:00+05:30"}
```

- The text is a short summary, at most 200 characters. Never the positions
  of the account, keys, tokens or approval phrases.
- The value of every environment variable whose name looks secret (`KEY`,
  `TOKEN`, `SECRET`, `PASSWORD`, `PIN`, `TOTP`, `AUTH`, `SESSION`, `COOKIE`,
  `WEBHOOK`) and any long token-like string are replaced by `[redacted]`
  before anything leaves the machine.
- The URL is never written to a file, a log, the UI or an MCP answer (only
  its host name, to show that it is set).
- Only `http` and `https` URLs are used. A failed POST is logged, never
  retried in a loop, and never stops the tool that notified.

Whoever runs the webhook sees what you send it: pick one you trust.

## Commands

```bash
python3 trader-alerts/alerts.py notify status              # settings, webhook set or not, the last decisions
python3 trader-alerts/alerts.py notify test                # through the configured channels
python3 trader-alerts/alerts.py notify test --channel desktop   # one channel, even while off
python3 trader-alerts/alerts.py notify send --event decay_alert --text "practice run"
```

The UI's Forward tests page shows the settings and has a test button
(`POST /api/notify/test`); the MCP tool `notify_test` does the same for an
assistant. Both respect the config: while notifications are off they say so
and send nothing.

Files: `trader/notify/state.json` (what was sent, for the limits) and
`trader/notify/log.jsonl` (each decision, with the text that was sent).
Neither is written while notifications are off.
