# The live co-pilot

`trader-copilot` is the bridge from paper to real money. It is built to be
slow: one live order, one typed approval, every time. Read the safety model
before anything else.

## The safety model

1. **Nothing is automatic.** There is no auto-approve setting, no batch
   approval and no background placing. A `copilot` config key that looks like
   one (anything with `auto`, `unattended`, `approve`, `confirm`) is refused,
   not ignored. `copilot.py approve` refuses to run without a terminal.
2. **Only proven strategies.** A strategy can make a live proposal only when
   trader-lab's verdict is **PROMOTE** and its paper forward record meets your
   thresholds: at least `min_forward_trades` (30) trades over
   `min_forward_weeks` (4) weeks, a deepest fall of at most
   `max_forward_drawdown_r` (6R), a co-trader Brier score of at most
   `max_brier` (0.25) once `min_calibration` (20) predictions are resolved,
   and no DEMOTE in the last `demote_lookback_days` (30). WATCH, RESEARCH,
   REJECT and DEMOTE are **paper only**, and the eligibility table says which
   check failed.
3. **Its own capital.** `copilot.capital` is the live bucket. trader-budget
   sizes every live order inside it; the rest of the account is never used.
4. **trader-risk on the live config,** at proposal time and again at approval.
5. **Daily caps** (conservative defaults): `max_live_orders_per_day` (2),
   `max_capital_at_risk_pct` (2% of the bucket: the loss at the stop of open
   live positions plus the new one), `max_live_loss_per_day_pct` (1%: past it
   the **live kill switch turns on by itself**, waiting entries are refused,
   and each open live position gets a flatten ticket that still needs your
   typed phrase), and a cool-off of `cooloff_after_loss_min` (60) after any
   live loss.
6. **Fresh, or not at all.** A proposal expires after `expire_min` (5)
   minutes, and is invalid when the price has moved more than
   `max_price_move_pct` (0.5%) from the plan, or when the order file changed.
   Re-plan.
7. **Your exact phrase.** The ticket ends with a phrase like `yes live BUY 42
   RELIANCE`. You type it, exactly (case and numbers included), in your own
   terminal (`copilot.py approve ID` asks) or in the local UI's dialog, which
   unlocks after a 10-second countdown that the server enforces too, and does
   not accept a paste. The approval is written to
   `trader/copilot/approvals.jsonl`, tied to the proposal's fingerprint, and
   used once, within two minutes.
8. **trader-broker still decides.** The phrase becomes your yes in
   `ticket.py confirm`; `ticket.py place` checks its six gates itself
   (config broker, `"paper": false`, `live_enabled`, trader-risk PASS for
   that exact order today, your typed yes, today's preflight with this
   token). See [brokers.md](brokers.md#a-live-order-every-gate).
9. **What can never approve:** the auto-pilot (its machine yes is refused on
   a live config, and a yes recorded as `autopilot` never opens the yes
   gate), the MCP server (it has `copilot_status` and `copilot_proposals`,
   read only, and no approval tool), the Ask panel (no browser session, and
   only read-only tools), a stale or reused approval, or an approval for
   another proposal. The tests in `tests/test_copilot.py` try each of these
   against a stub broker and check that nothing reached it.

## A live order, step by step

```bash
python3 trader-cotrader/cotrader.py today          # candidates with calibrated odds (or none)
python3 trader-copilot/copilot.py eligibility      # who may trade live, and why not
python3 trader-copilot/copilot.py propose          # live tickets, nothing sent
python3 trader-copilot/copilot.py approve CP-20261029-1
```

```
LIVE ORDER at Zerodha Kite (real money): CP-20261029-1
  BUY 42 RELIANCE on NSE, MIS (intraday), LIMIT 1174.30, good for today
  then a stop-loss: SL trigger 1163.30
  then a target: LIMIT 1196.30 (when the stop or the target fills, the other is cancelled)
  order value about ₹49,320.60
  loss if the stop fills at 1163.30: about ₹462.00 + ₹52.11 charges = ₹514.11 (a stop can fill worse in a fast market)
  the stop is SL: trigger 1163.30, limit 1151.60 (a stop-limit can miss in a gap or a fast market)
  exits: after the entry fills, the stop and the target go as ONE Zerodha Kite GTT OCO: copilot.py exits ID ...
  size: 42 shares, about ₹49,320.60 of the co-pilot's ₹1,00,000.00 live bucket
  max loss at the stop: ₹514.11 including charges
  evidence: PROMOTED, t-dip-live; 61% of 18 similar past trades made money after charges (95% 40% to 79%) ...
  calibration: Brier 0.239 on 18 resolved predictions; forward record 18 trades over 16.1 weeks, deepest fall 2.69R
  broker margin: Zerodha Kite says this order needs ... and ... is available
  trader-risk: PASS on your live config at 09:41
  expires 09:46 IST, or if the price moves more than 0.5% from 1174.30
  to send it, type exactly: yes live BUY 42 RELIANCE
Type exactly  yes live BUY 42 RELIANCE  to send this LIVE order (anything else cancels):
```

Then trader-broker sends it. The exits:

- **Upstox, Dhan:** the entry goes as one bracket with its stop and target
  (multi-leg GTT, Super Order).
- **Zerodha Kite:** after the entry fills, `copilot.py exits ID` sends the
  stop and the target as one GTT OCO, after its own phrase (`yes live exits
  SELL 42 RELIANCE`). GTT legs are limit orders, so the stop is SL with a
  limit `stop_limit_pct` (1%) past its trigger.
- **Angel One:** no linked order: `exits ID` sends the stop leg; watch the
  target yourself.

A paper twin of the entry is recorded in `trader/state/shadow.json` (what the
paper broker would have done at the same moment), so the journal can compare
live with paper.

## Caps, the live kill switch and flattening

`copilot.py caps` shows the day. The live loss comes from your broker's
positions (read only) for the symbols the co-pilot traded today; without a
token in the shell it is "unknown", and says so. When it reaches the cap, the
live kill switch turns on and each open live position gets a SELL ticket (a
marketable limit 1% under the last price). Approving a flatten ticket first
cancels that position's linked exit at the broker, then sells. `copilot.py
kill` turns the switch on by hand; `copilot.py kill --lift` asks for the
words `lift live kill`. trader-risk's own kill switch blocks live proposals
too.

## Notifications

`"notify": {"bell": true, "desktop": false}`: a terminal bell, and optionally
a desktop notification on this machine (macOS `osascript`, Linux
`notify-send`), when a live proposal is waiting. Nothing leaves the machine.

## Why not fully autonomous live trading

Per-order approval is the design, not a missing feature. Under SEBI's retail
algo framework (in force since 1 April 2026), orders from an API come from a
registered static IP, a strategy above 10 orders a second needs exchange
registration as an algo, and sharing a closed-logic ("black box") algo with
others brings research analyst rules into play. An unattended loop with real
money also has failure modes (a stale price, a broker outage, a bug) that a
person reading each ticket catches. The maintainer's decision, and what would
have to change, is in
[#67, "Decision: fully autonomous live trading (not planned)"](https://github.com/rajdavee/bharat-trader/issues/67).

## What this does not show

A PROMOTE verdict and a good paper record are evidence, not a promise: paper
fills are a model, a few months of trades are a small sample, and the market
regime they came from may not last. The ticket's odds are the share of
similar past trades that made money after charges, with their range and n.
Education and tooling, not financial advice.
