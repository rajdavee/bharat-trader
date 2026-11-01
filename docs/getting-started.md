# Getting started: your first 30 minutes

From a fresh clone to a paper trade, an option strategy, a lab verdict, the
local UI and your own AI assistant. Every command below is real; the output
snippets are from runs on NSE data of Friday 30 October 2026.

You need Python 3.8 or newer (`python3 --version`) and nothing else. Nothing
here can place a real order: paper trading is the default and every order
needs trader-risk's PASS and your typed yes.

```bash
git clone https://github.com/rajdavee/bharat-trader.git
cd bharat-trader
```

The tools keep their files in a `trader/` folder inside the folder you run
them from. Run from the clone to start with; later, pick a trading folder of
your own and call the scripts by their full path.

> **When to try it.** Most commands work any time on the last session's
> data. Planning and paper orders need the market open (09:15 to 15:30 IST,
> NSE trading days). To practise on a weekend, add `--now "YYYY-MM-DD HH:MM"`
> to pin the clock to a past session, as the examples below do.

## 1. Your rules (2 minutes)

```bash
python3 trader-budget/size.py init
```

It asks for your capital, the most you will risk on one trade, trades a day
and MIS or CNC, then writes `trader/config.json` and says what it means in
plain words:

```
Capital ₹2,00,000.00, product MIS (intraday: zerodha squares off MIS positions; this pack exits 5 minutes before it and before an F&O stock's closing auction).
  - Most one trade may lose at its stop, charges included: ₹2,000.00 (1% of capital).
  - Most one day may lose: ₹6,000.00. After that, no new entries today.
  ...
  - Paper trading: on. Short selling: off. F&O: off.
```

Without questions: `size.py init --capital 200000 --risk-pct 1 --max-trades 3
--product MIS --broker-profile zerodha --yes`. `--broker-profile` sets your
broker's MIS square-off time and its brokerage for paper fills. Every key is
explained in [configuration.md](configuration.md).

## 2. Scan the market (5 minutes)

```bash
python3 trader-scanner/scan.py universe        # NSE's EQ-series list, once
python3 trader-scanner/scan.py daily           # pre-market setups on daily closes
python3 trader-news/news.py --tickers TCS INFY --hours 24
```

```
Index context, 2026-10-30 close vs the close before: NIFTY 50 23,053.10 (+0.51%), BANK NIFTY 54,686.95 (-0.41%), INDIA VIX 15.30 (+1.19%).
  INFY           ₹1,185.00  RSI  64.3  ₹1,683.43 cr  20-day closing-high breakout
  TCS            ₹2,502.60  RSI  66.8  ₹1,089.79 cr  bull flag
A scan is a list of charts that match a rule, not a list of stocks to buy.
```

During the session, `scan.py orb --at 09:45` finds opening-range breakouts
so far today.

## 3. Plan one trade (5 minutes)

```bash
python3 trader-entry/entry.py check TCS --setup orb --write   # has it broken out of the opening range?
python3 trader-exit/exit.py plan --stop or-mid --target 2R --write
python3 trader-timemachine/timemachine.py run --write          # the honest odds
```

The exit plan comes before the entry, rounded to the tick and with charges:

```
  Entry         ₹2,521.30 at 09:35 IST (bar 3)
  Stop          ₹2,510.50  opening-range midpoint
  Target        ₹2,542.90  2R (two times the risk)
  Reward:risk   2.0 gross, 1.51 net of charges
  Flat by       15:07 IST, win or lose: no overnight hold
```

The time machine replays the rest of the session 10,000 times from the
stock's own last 60 days, and checks every past day the setup fired. It
often says no, and that is the point:

```
  target first   29.5 %
  stop first     67.5 %
  Average R: -0.267 net of charges
History: the same rule ... fired 25 times: 1 target, 10 stop, 14 neither.
```

## 4. Size it, check it, paper-trade it (5 minutes)

```bash
python3 trader-budget/size.py --write          # shares that fit your max loss, charges included
python3 trader-risk/check.py                   # PASS or BLOCK, with the reason
python3 trader-broker/ticket.py show           # the ticket in plain words
python3 trader-broker/ticket.py confirm --index 1 --yes-text "yes, buy 39 TCS"
python3 trader-broker/ticket.py fill --index 1 --price 2521.30
python3 trader-broker/ticket.py status
```

```
PASS  #1 BUY 39 TCS (MIS), entry
          loss at the stop ₹421.20 + ₹81.74 charges = ₹502.94 (limit ₹2,000.00), reward:risk 2.00 (min 1.5)
```

The yes is checked: "ok", "sure" or a yes naming another stock or quantity
is refused, and your words are recorded verbatim. One yes covers one ticket.
`alerts.py add --from-plan` then rings on the stop, the target and the
flat-by time.

## 5. An option strategy (5 minutes)

```bash
python3 trader-options/options.py chain NIFTY --strikes 3 --write
python3 trader-strategy/strategy.py build iron-condor NIFTY --wings 300 --width 100
```

```
At expiry: max profit ₹1,387.75, max loss -₹5,112.25
  after charges: max profit ₹1,285.87, max loss -₹5,324.63
Margin required (estimate, +/-15%): about ₹67,003.10 (₹56,952.63 to ₹77,053.56)
Max loss at expiry: ₹5,112.25 (not the margin); return on margin 2.07%
Probability of profit after charges, at expiry:
  textbook (lognormal at IV 12.10%): 72.60%, expected value -₹245.69
  simulated (bootstrap of NIFTY's own returns, realised vol 13.76%): 75.84%, expected value -₹33.35
```

Read it the honest way: a 73% chance of a small profit with a negative
expected value after charges. The STT-on-exercise warning shows what an
in-the-money long costs if you let it expire instead of selling it.
[options.md](options.md) explains every number and where it stops being exact.

To paper-trade it, add `--orders` (a proposal in `trader/orders.json`), set
`"allow_fno": true` in your config, and run `check.py`. With the ₹2,000 max
loss above, trader-risk blocks this condor, and says why:

```
BLOCK #1 BUY 65 NIFTY 22600 PE 2026-11-03 (NRML), entry
        - loss: the NIFTY basket can lose ₹5,328.05 (the most it can lose at expiry, charges included), over your ₹2,000.00 max loss per trade
```

A structure that fits your rules passes as one basket, and one typed yes
covers every leg: `ticket.py confirm --basket --yes-text "yes"`, then
`ticket.py fill --basket` and `ticket.py mtm` for the P&L, Greeks and margin.

## 6. Ask the Strategy Lab (3 minutes)

```bash
python3 trader-lab/lab.py list
python3 trader-lab/lab.py run orb15-vwap --data tests/fixtures/trader-lab   # offline, on the saved bars
python3 trader-lab/lab.py report orb15-vwap
```

A backtest after charges, then walk-forward out of sample, the parameter
neighbourhood, Monte Carlo, the deflated Sharpe ratio and the probability of
backtest overfitting, against gates written before the run. Expect REJECT:
all six library strategies are rejected on the saved data, and the report
lists what even a pass would NOT prove. Drop `--data` to fetch fresh bars.
See [lab.md](lab.md).

## 7. The local web UI (2 minutes)

```bash
python3 ui/serve.py --offline tests/fixtures/ui --open    # a demo on saved real data, no network
python3 ui/serve.py --open                                # your own trader/ folder
```

The same numbers from the same scripts, on `127.0.0.1` only: dashboard,
charts with your plan's levels, scanner, option chain, strategy builder,
paper portfolio, the risk panel with the kill switch, the agent team and
settings. Click a dotted number for its formula, inputs and source. See
[ui.md](ui.md).

## 8. Connect your AI assistant (3 minutes)

Add the MCP server to your assistant's configuration (any MCP-compatible
desktop, IDE or chat assistant):

```json
{
  "mcpServers": {
    "bharat-trader": {
      "command": "python3",
      "args": ["/path/to/bharat-trader/mcp/server.py", "--workdir", "/path/to/trading-folder"]
    }
  }
}
```

Restart it; it should list 41 tools and 11 prompts. Then ask: *"Morning
briefing please, focus on banks"*, *"Show me NIFTY's PCR, max pain and
expected move"*, *"Does the 15-minute opening-range breakout survive charges
out of sample?"*. There is no live-order tool; a paper order still needs
your own yes. See [mcp.md](mcp.md), and [agents.md](agents.md) for the six
personas the server offers as prompts.

## Where next

- A full trading day, step by step: the README's [A trading day](../README.md#a-trading-day).
- Your real broker account, read only, and what going live takes (a static
  IP and a daily login): [brokers.md](brokers.md).
- Is every data source answering? `python3 trader-doctor/doctor.py`.
- Every page: [the docs index](README.md).

Education and tooling, not financial advice.
