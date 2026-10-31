# Use it from your AI assistant (MCP)

`mcp/server.py` is a [Model Context Protocol](https://modelcontextprotocol.io/)
server, so any MCP-compatible AI assistant (desktop and IDE assistants, chat
apps that support MCP) can call the bharat-trader tools for you: market
status, scans, news, entry and exit plans, the time machine, sizing, risk
checks, the paper broker, option chains, results and insider deals.

Python standard library only. Nothing to install beyond the repo.

## Install

1. Get the repo: `git clone https://github.com/rajdavee/bharat-trader`.
2. Make a trading folder (the tools read and write its `trader/` subfolder),
   and create your rules once: `python3 /path/to/bharat-trader/trader-budget/size.py init --config /path/to/trading-folder/trader/config.json`.
3. Add the server to your MCP client's configuration. Most clients take a
   command and its arguments; the shape is usually this:

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

On Windows use `"command": "python"` and paths like
`"C:\\Users\\you\\bharat-trader\\mcp\\server.py"`.

4. Restart the client. It should list 22 tools and 5 prompts from
   `bharat-trader`.

Try it without your own data first: point `--offline` at the saved real
answers in the repo (the clock is pinned to when they were taken, and a
throwaway copy of the demo paper account is used):

```json
{
  "mcpServers": {
    "bharat-trader-demo": {
      "command": "python3",
      "args": ["/path/to/bharat-trader/mcp/server.py", "--offline", "/path/to/bharat-trader/tests/fixtures/mcp"]
    }
  }
}
```

### Options

| option | meaning |
| --- | --- |
| `--workdir DIR` | The folder whose `trader/` files the tools read and write. Default: the current folder. Every write lands in `DIR/trader/`. |
| `--offline DIR` | Saved answers instead of the network (below). The network is switched off for the whole process. |
| `--now "YYYY-MM-DD HH:MM"` | Pin the clock (IST), like the skills' `--now`. |
| `--http PORT` | Streamable HTTP on `http://127.0.0.1:PORT/mcp` instead of stdio. `0` picks a free port. Loopback only; there is no option to bind anything else. |
| `--rate-limit N` | Tool calls a minute (default 30). Past it a call returns a tool error saying how long to wait. |
| `--page-size N` | Items per `tools/list`, `resources/list`, `prompts/list` page (default 100). |
| `--tool-reference` | Print the tool table below and exit. |
| `--verbose` | Log HTTP requests to stderr. |

`--offline DIR` reads `DIR/offline.json` when it is there: each tool can name
its own saved file or folder (`{symbol}` is filled in) and pin its own clock,
and `"trader"` names a demo `trader/` folder to copy. Without one, each skill
gets `DIR/<skill folder>` when that exists, else `DIR`.
[`tests/fixtures/mcp/offline.json`](../tests/fixtures/mcp/offline.json) is
the bundle the tests use.

## Tools

Every result has the same envelope: `tool`, `as_of` (IST), `offline`,
`paper_only: true`, `data` (the skill's own answer), `notes` and
`disclaimer: "Education and tooling, not financial advice."`. It comes as
`structuredContent` (checked against the tool's `outputSchema`) and as the same
JSON in a text block. A problem the assistant can fix (a bad symbol, a missing
plan, a refused yes) is a tool result with `isError: true` and a plain-words
message.

<!-- tools:start -->
| tool | what it does | arguments | hints |
| --- | --- | --- | --- |
| `market_status` | **NSE market status.** Is NSE open right now? The session phase, today's hours, the next open, special sessions, from the NSE calendar (no network). verify: true also asks nseindia.com. | `verify`? | read-only; network |
| `scan_daily` | **Daily setups scan.** Pre-market scan of the NSE universe (trader/universe.txt or symbols) on daily closes: breakouts, pullbacks, RSI, liquidity filters. Nothing is written. | `symbols`?, `limit`?, `min_price`?, `min_turnover`? | read-only; network |
| `scan_orb` | **Opening-range breakout scan.** Opening-range breakouts so far today across the universe, on 5-minute bars. Nothing is written. | `symbols`?, `limit`?, `min_price`?, `min_turnover`?, `at`?, `until`?, `or_minutes`? | read-only; network |
| `news` | **News and announcements.** Headlines, NSE corporate announcements, upcoming board meetings and today's move for your holdings, plan and watchlist (or tickers). Read only (the seen-items state is not updated). | `tickers`?, `hours`?, `market`?, `include_seen`? | read-only; network |
| `entry_check` | **Entry trigger check.** Has this stock's setup triggered? Opening-range breakout (orb) or a close above a level, bar by bar on 5m or 1m Yahoo bars, inside the entry window. save: true writes trader/entry.json for exit_plan. | `symbol`, `setup`?, `level`?, `or_minutes`?, `interval`?, `save`? | writes trader/; network |
| `exit_plan` | **Stop and target plan.** Set the stop and target for a long entry, from trader/entry.json or from symbol + entry (+ or_high/or_low): or-mid, or-low, pct:X, atr:K or price:X stops; 1R/1.5R/2R or price:X targets; the flat-by time and a charges estimate. save: true writes trader/plan.json. | `symbol`?, `entry`?, `or_high`?, `or_low`?, `or_minutes`?, `setup`?, `date`?, `time`?, `interval`?, `stop`?, `target`?, `flat_by`?, `qty`?, `save`? | writes trader/ |
| `timemachine_run` | **Time machine replay.** Replay the plan (trader/plan.json, or symbol/entry/stop/target) over thousands of simulated sessions drawn from the stock's own recent 5-minute bars, plus the real history: odds of the target before the stop, R after charges, a fan of outcomes. Nothing is written. | `symbol`?, `entry`?, `stop`?, `target`?, `date`?, `entry_time`?, `replays`?, `seed`? | read-only; network |
| `size_trade` | **Position size.** How many shares the plan allows under your rules: risk per trade, max position, cash buffer, charges, tick size. Reads trader/plan.json and trader/config.json. Nothing is written. | `capital`?, `limit`?, `market`? | read-only |
| `risk_check` | **Risk check.** trader-risk on trader/orders.json: every order against your limits (size, loss at stop + charges, reward:risk, daily loss, trades, entry window, duplicates). A preview by default; record: true records the verdicts the paper broker needs. | `record`? | writes trader/ |
| `paper_orders_show` | **Paper order tickets.** The order tickets in trader/orders.json, in plain words, each with its trader-risk status and fingerprint. | none | read-only |
| `paper_order_draft` | **Draft paper orders.** Size trader/plan.json into order tickets (trader/orders.json, the old one kept as .bak) and record trader-risk's verdicts. Returns each ticket with its fingerprint. Places nothing. | `capital`?, `limit`?, `market`? | writes trader/ (replaces a file, keeps a .bak) |
| `paper_order_confirm` | **Confirm one paper order.** Record the USER's yes for ONE ticket and place it on the PAPER account (no real money; no live broker exists in this server). Needs the ticket's index and its exact risk-PASS fingerprint. When the client supports elicitation the server asks the user directly and yes_text is ignored; otherwise yes_text must be the user's own reply, word for word, after you showed them the ticket. Never write a yes yourself. | `index`, `risk_fingerprint`, `yes_text`? | writes trader/ |
| `portfolio_status` | **Paper portfolio.** The paper account: cash, positions marked to a delayed price, P&L if closed now after charges, working stop/target legs, today's fills and limits. | `live_quotes`? | read-only; network |
| `options_chain` | **Option chain.** NSE option chain for an index or F&O stock and expiry: LTP, bid/ask, OI and change, volume, our Black-76 IV and Greeks per strike around the money, lot size, forward, ATM straddle. | `symbol`, `expiry`?, `strikes`? | read-only; network |
| `options_summary` | **Options summary.** One expiry in numbers: PCR (OI and volume), max pain, call and put walls, ATM IV, 25-delta skew, expected move from the straddle and from IV, IV rank and percentile from India VIX (NIFTY). | `symbol`, `expiry`? | read-only; network |
| `results_analyse` | **Quarterly results.** Quarterly results from the company's XBRL filings with NSE: revenue, profit, margins, EPS, QoQ and YoY changes, plain-words notes and the next board meeting. | `symbols`, `quarters`?, `standalone`? | read-only; network |
| `insider_deals` | **Bulk and block deals.** NSE bulk and block deals: who bought or sold how much, tagged when you hold or watch the stock or the client is on your watch list. | `symbol`?, `client`?, `min_value_cr`?, `side`?, `kind`?, `days`?, `mine`?, `limit`? | read-only; network |
| `insider_disclosures` | **Insider trading disclosures.** SEBI PIT insider trading disclosures filed with NSE: promoters, directors and employees buying or selling, net by stock. | `symbol`?, `days`?, `promoters_only`?, `market_only`?, `mine`?, `max_filings`? | read-only; network |
| `config_get` | **Your settings.** trader/config.json, the effective settings with defaults filled in, validation errors and warnings, and a plain-words summary. Read only. | `with_schema`? | read-only |
| `broker_status` | **Broker connection.** The live broker connection, read only and without network: the config's broker and mode, for Upstox, Zerodha Kite and Dhan which BT_* environment variables are set (true/false, never the values), what each API can do, the plan notes and the last static-IP preflight. Places nothing. | none | read-only |
| `broker_holdings` | **Broker holdings.** Delivery holdings read from the live broker's API (Upstox, Zerodha Kite or Dhan): symbol, qty, average, last price, P&L. Read only; needs the user's own token in the server's environment. | `broker`? | read-only; network |
| `broker_positions` | **Broker positions.** Today's positions read from the live broker's API: symbol, exchange, product, qty, average, P&L. Read only; needs the user's own token in the server's environment. | `broker`? | read-only; network |
<!-- tools:end -->

`?` marks an optional argument. Every tool also carries MCP annotations:
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`
("network": it can reach Yahoo or NSE when not offline).

The `trader-strategy` tools (`strategy_build`, `strategy_price`) and `doctor`
are not on main yet; `mcp/toolkit.py` has a marked hook for each.

### The paper order flow

1. `exit_plan` with `save: true` writes `trader/plan.json` (or use the one you
   have).
2. `paper_order_draft` sizes it into `trader/orders.json` and records
   trader-risk's verdicts. Each ticket comes back with its `fingerprint`.
3. The assistant shows you the READY ticket in plain words.
4. `paper_order_confirm` with that ticket's `index` and `risk_fingerprint`:
   - if your client supports **elicitation**, the server asks *you* directly
     (a form with one text box) and ignores any `yes_text` the assistant sent;
   - otherwise `yes_text` must be your own reply, word for word. The paper
     broker's own check (`parse_yes`) decides: only a reply that starts with
     "yes", names no other stock, has no condition in it, and whose numbers
     match this order counts.

The broker records the reply word for word in `trader/state/broker.json`. A
yes covers one ticket; a second confirm of the same order is refused.

## Resources and prompts

Resources are the JSON files of your `trader/` folder, read only:
`bharat-trader://trader/plan.json`, `.../watchlist.json`,
`.../portfolio.json`, `.../options.json`, `.../strategy.json`,
`.../journal.json` (whichever exist), and `bharat-trader://trader/state/risk.json`,
`.../broker.json`, `.../trades.json`. Two templates cover the rest:
`bharat-trader://trader/{file}` and `bharat-trader://trader/state/{file}`.
Names are checked (`*.json`, no `..`, no subfolders beyond `state/`) and the
resolved path must stay inside `trader/`.

| prompt | arguments | asks the assistant to |
| --- | --- | --- |
| `morning_briefing` | `focus`? | market_status, news, scan_daily, portfolio_status, then a pre-market summary |
| `intraday_plan` | `symbol` | entry_check, exit_plan, timemachine_run, size_trade; draft and confirm only on your typed yes |
| `options_income_scan` | `symbol`, `expiry`? | options_summary and options_chain; explain positioning and a seller's risks, no trade |
| `weekly_review` | none | portfolio, tickets, risk, config and the trades/decisions logs; one process change |
| `research_strategy` | `idea`, `symbol`? | gather evidence for and against with the tools; say what would kill the idea |

Every prompt tells the assistant to use the tools for every number, show
evidence and risks, never promise a result, and say it is not financial
advice.

## Good questions to ask

- "Is NSE open? What's the next session?"
- "Morning briefing please, focus on banks."
- "Which stocks broke their opening range today, and how liquid are they?"
- "Has RELIANCE triggered? If so, plan the exit with an opening-range-mid stop
  and run the time machine on it."
- "How many shares does my plan allow, and what do I lose at the stop after
  charges?"
- "Show me NIFTY's PCR, max pain, walls and expected move for this expiry, and
  what an option seller would be exposed to."
- "How did TCS's last five quarters look? Anything unusual in the notes?"
- "Any bulk or block deals or promoter buying in stocks I hold?"
- "Review my week: what did risk block, and which rule did I nearly break?"

Questions that will get a careful no: "which stock will go up?", "place a real
order", "just buy it for me".

## Security model

- **No live orders, at all.** There is no order tool for a live broker. The
  three broker tools (`broker_status`, `broker_holdings`, `broker_positions`)
  only read: they call trader-broker's adapters for holdings and positions
  with the token the user put in the server's environment, and never place,
  change or cancel anything. `paper_order_confirm` refuses a live config
  (`"paper": false`) before doing anything (trader-broker's `paper_only`); a
  live order goes out only with `ticket.py place` in the user's own terminal
  (see [brokers](brokers.md)).
- **One order write, with two locks.** `paper_order_confirm` needs trader-risk
  PASS recorded today for that exact order (the fingerprint is a hash of the
  order's fields, so any change needs a new check) and the user's own typed
  yes, through elicitation when the client offers it.
- **Writes stay in `<workdir>/trader/`.** Every path a tool passes to a skill
  is under it, and the process runs with `<workdir>` as its current folder so
  a relative default lands there too. Files a tool replaces (`plan.json`,
  `orders.json`, `entry.json`) keep a `.bak`. Resources are read only.
- **Rate limited**: 30 tool calls a minute by default, on top of the skills'
  own pauses between NSE and Yahoo requests.
- **stdout is only JSON-RPC.** The server keeps a private copy of stdout for
  the protocol and points file descriptor 1 at stderr, so a stray print
  anywhere goes to the log, never into a message.
- **HTTP is loopback only** (`127.0.0.1`), checks `Host` (DNS rebinding) and
  `Origin` (403 for anything not loopback), takes one JSON body of at most
  1 MB per POST, and sends no CORS headers. There is no login: anyone who can
  run a program on your machine can call it, so prefer stdio.
- **Elicitation state is signed.** The modern multi round-trip confirm carries
  a `requestState` signed with HMAC-SHA256 under a per-process key, tied to
  the tool, its arguments and the ticket, valid for 15 minutes, and usable
  once. A tampered, expired, replayed or mismatched state is rejected.
- **Offline really is offline**: `--offline` disables outbound sockets for the
  whole process.

## Protocol support

The server implements MCP revision **2026-07-28** (the current revision as of
November 2026) and serves clients of the earlier, handshake-based revisions
**2025-11-25**, **2025-06-18** and **2025-03-26** too ("dual-era").

| | 2026-07-28 (modern) | 2025-11-25 and earlier (legacy) |
| --- | --- | --- |
| opening | none; every request carries `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` in `_meta`; `server/discover` | `initialize` (version negotiated: the client's if supported, else 2025-11-25), `notifications/initialized` |
| wrong version | `-32022 UnsupportedProtocolVersion` with `supported` and `requested` | the server answers with the version it speaks |
| results | `resultType`, `_meta` serverInfo; `ttlMs` and `cacheScope: "private"` on lists and reads | as the revision defines |
| elicitation | multi round-trip: `resultType: "input_required"` with `inputRequests` and a signed `requestState`, answered by a retry carrying `inputResponses` | `elicitation/create` sent to the client over stdio (form mode) |
| logging | per request, `io.modelcontextprotocol/logLevel` in `_meta` | `logging/setLevel`, `notifications/message` |
| ping | removed (`-32601`) | `ping` |
| resource not found | `-32602` | `-32002` |
| HTTP | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers must match the body (`400`, `-32020`); unknown method `404` | `Mcp-Session-Id` sessions, ended with `DELETE` |

Both eras: JSON-RPC 2.0, one message per line on stdio, no batches;
`tools/list` (deterministic order) and `tools/call` with `inputSchema`,
`outputSchema`, `structuredContent` plus text, `isError`, titles and
annotations; `resources/list`, `resources/templates/list`, `resources/read`;
`prompts/list`, `prompts/get`; opaque pagination cursors (`-32602` for a bad
one); `notifications/cancelled` (no answer is sent for a cancelled request);
`notifications/progress` when the request carries a `progressToken`. Closing
stdin shuts the server down.

Not implemented: `subscriptions/listen` and list-changed notifications (the
lists do not change while the server runs), completions, sampling, roots, the
tasks extension, and SSE responses over HTTP (answers are single JSON
objects, so progress and legacy elicitation are stdio only; over HTTP the
confirm tool falls back to `yes_text`).

## Tests

`tests/test_mcp.py` spawns the server as a subprocess and talks to it over
pipes and over HTTP, offline: the handshake and version negotiation in both
eras, `tools/list` schema validity, every tool on the saved fixtures (each
result checked against its `outputSchema`), resources, prompts, the error
codes, cancellation, progress, the rate limit, the confirm flow (elicitation
in both eras, the typed-reply fallback, a tampered or replayed state, a stale
risk check, a non-paper config), that writes stay in `trader/`, and that
stdout carries only JSON-RPC.

*Education and tooling, not financial advice.*
