---
name: trader-copilot
description: >-
  Live co-pilot for NSE stocks: the bridge from paper to real money, one typed
  approval per order. Only a trader-lab strategy with the PROMOTE verdict AND
  a paper forward record that meets your thresholds (trades, weeks, drawdown
  in R, the co-trader's Brier score, no recent demote) can make a live
  proposal; WATCH and RESEARCH stay paper only and the table says why. Each
  proposal is a co-trader candidate sized by trader-budget inside the
  co-pilot's own live capital bucket, checked by trader-risk on your live
  config, shown as a plain ticket (the order, size, max loss with charges,
  the broker-native linked stop and target, the evidence with n and range,
  calibration, the broker's own margin) and sent only after you type its
  exact phrase, like "yes live BUY 42 RELIANCE", in your terminal or the
  local UI (after a 10-second countdown), then through trader-broker's six
  live gates and today's preflight. Daily caps on live orders, capital at
  risk and live loss (past it the live kill switch turns on by itself and
  offers flatten tickets), a cool-off after a live loss, proposals that
  expire or go stale when the price moves, a shadow paper twin of every live
  order. There is no auto-approve. Use when the user says "go live", "live
  trade my promoted strategy", "co-pilot", "copilot", "approve the live
  order", "live proposals", "real money trades", "asli paise se trade",
  "live order approve karo", "live kill switch", "live caps", "which
  strategies can trade live", "stop live trading" or "flatten live".
argument-hint: "[status | eligibility | propose [--run] | proposals | approve ID | reject ID | exits ID | caps | kill [--lift]]"
---

# trader-copilot

The co-pilot turns a strategy that has earned it into LIVE proposals, and
sends each one only when you type its exact phrase. It is slow on purpose:
real money, one order, one typed approval, every time. It never runs by
itself, there is no auto-approve setting (a config key that looks like one
is refused), and an AI assistant cannot approve: the MCP server only reads.

All amounts are rupees, all times IST. `<skill dir>` is this skill's folder.
It needs trader-broker, trader-risk, trader-budget, trader-cotrader and
trader-lab next to it.

```bash
python3 "<skill dir>/copilot.py" status                  # eligibility, waiting tickets, caps, live kill
python3 "<skill dir>/copilot.py" eligibility             # live proposals or paper only, and why
python3 "<skill dir>/copilot.py" propose [--run]         # today's live proposals (places nothing)
python3 "<skill dir>/copilot.py" proposals               # the waiting live tickets in full
python3 "<skill dir>/copilot.py" approve CP-20261029-1   # the user runs it: shows the ticket, asks for the phrase
python3 "<skill dir>/copilot.py" reject CP-20261029-1
python3 "<skill dir>/copilot.py" exits CP-20261029-1     # Kite GTT OCO / Angel One stop, after the entry fills
python3 "<skill dir>/copilot.py" caps
python3 "<skill dir>/copilot.py" kill [--reason "..."]   # live kill switch on; kill --lift asks for "lift live kill"
```

`--workdir DIR` is the folder holding `trader/`; `--now "2026-10-29 09:41"`
pins the clock; `--json` on the read commands.

## Who may trade live

| check | default |
| --- | --- |
| trader-lab verdict is PROMOTE (WATCH, REJECT, DEMOTE, RESEARCH: paper only) | always |
| paper forward trades of the promoted spec | `min_forward_trades` 30 |
| the forward record spans at least | `min_forward_weeks` 4 |
| its deepest fall, in R | `max_forward_drawdown_r` 6 |
| the co-trader's Brier score for its odds, once `min_calibration` (20) are resolved | `max_brier` 0.25 |
| no DEMOTE (lab or calibration) in the last | `demote_lookback_days` 30 |
| in `strategies`, if you set that list | optional |

## The config

A `"copilot"` block in `trader/config.json`, next to your live settings
(`"paper": false`, `"broker"`, `"live_enabled"`, which only you set):

```json
"copilot": {"capital": 50000, "max_live_orders_per_day": 2, "max_capital_at_risk_pct": 2,
            "max_live_loss_per_day_pct": 1, "cooloff_after_loss_min": 60, "expire_min": 5,
            "max_price_move_pct": 0.5, "notify": {"bell": true, "desktop": false}}
```

`capital` is required and is the co-pilot's own live bucket: sizing uses it,
never the rest of your account. `stop_limit_pct` (1) sets the limit of an SL
stop for brokers that need one (Kite's GTT, Dhan). Notifications are local
only: a terminal bell, and optionally a desktop notification on this machine.

## One live order

1. `propose`: today's co-trader candidates (its last run, or `--run`). Only
   PROMOTED candidates of eligible strategies; equity only (F&O baskets stay
   paper). trader-budget sizes it in the live bucket, the caps are checked,
   trader-risk checks that exact order on your live config, the broker's
   margin is read (read only). The proposal is written to
   `trader/copilot/orders/<ID>.json`, nothing is sent.
2. Show the user the whole ticket. Never type the phrase for them and never
   paraphrase it as approved.
3. The user runs `copilot.py approve ID` in their own terminal (or uses the
   UI's dialog). Everything is checked again: still pending, not expired,
   the price within `max_price_move_pct` of the plan (else re-plan), the
   caps, the live kill, eligibility, a fresh trader-risk PASS, the order file
   unchanged (fingerprint). Then the exact phrase. A typed approval is tied
   to the proposal's fingerprint and used once, within two minutes.
4. trader-broker records the phrase as the user's yes (`ticket.py confirm`)
   and sends it (`ticket.py place`) through its six live gates and today's
   preflight. Upstox and Dhan: one bracket (the entry with its stop and
   target). Zerodha Kite: after the fill, `copilot.py exits ID` sends one GTT
   OCO with its own phrase (`yes live exits SELL 42 RELIANCE`). Angel One:
   the stop leg the same way.
5. A shadow paper twin is recorded in `trader/state/shadow.json` for the
   journal's live vs paper comparison.

## Caps and the live kill switch

- `max_live_orders_per_day`, `max_capital_at_risk_pct` (the loss at the stop
  of open live positions plus the new one, % of `capital`), a cool-off of
  `cooloff_after_loss_min` after any live loss.
- `max_live_loss_per_day_pct`: when the broker's positions (read only) show
  that loss today, the live kill switch turns on by itself, waiting entries
  are refused, and each open live position gets a flatten ticket (a
  marketable SELL LIMIT), which still needs its typed phrase.
- `kill` turns it on by hand; `kill --lift` asks for the words `lift live
  kill`. trader-risk's own kill switch blocks live proposals too.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | your live settings and the `copilot` block |
| `trader/lab/<name>/status.json`, `forward.json` | verdicts and forward records |
| `trader/cotrader.json`, `trader/cotrader/log.jsonl` | candidates and the logged odds (calibration) |
| `trader/copilot/state.json` | every proposal and what happened to it |
| `trader/copilot/approvals.jsonl` | every typed approval (append only) |
| `trader/copilot/orders/<ID>.json`, `<ID>-risk.json` | one live order and its risk verdicts |
| `trader/copilot/kill.json` | the live kill switch |
| `trader/state/broker.json`, `preflight.json`, `shadow.json` | trader-broker's own records |

## What it never does

- Never sends a live order without the user's fresh typed phrase for that one proposal.
- Never runs from a scheduled or background task to approve: `approve` refuses without a terminal.
- Never trades WATCH or RESEARCH ideas live, and never F&O baskets live in this version.
- Never switches the config to live: the user does that.
- From an assistant: `copilot_status` and `copilot_proposals` are read only. There is no approval tool.

Why per-order approval and not full autonomy: SEBI's retail algo framework
(static IP, exchange registration of algos above 10 orders a second, research
analyst rules for sharing closed-logic algos) and the capital risk. See
`docs/copilot.md`.

## Safety

- Education and tooling, not financial advice. Every output here is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all of these hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  agent's order tool asks for approval every time. Never set an order tool to "always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker API keys live in environment variables, never in the chat.
