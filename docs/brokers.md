# Live brokers: Upstox, Zerodha Kite, Dhan, Angel One

trader-broker can read your real account at four Indian brokers, and (only
when every gate is open) send one order at a time to it. Paper stays the
default: with no setup at all nothing talks to a broker.

> **Before you go live, two rules you cannot skip** (SEBI's retail-algo
> framework, in force since 1 April 2026):
>
> 1. **A static IP.** Brokers accept API orders (place, modify, cancel) only
>    from a static public IP you registered with them. A home connection's IP
>    usually changes; a static IP from your ISP or a small cloud server costs
>    money and can be changed only about once a week. Reading your account
>    works from any IP.
> 2. **A login every trading day.** Tokens expire daily (Upstox 03:30 IST,
>    Kite 06:00 IST, Dhan 24 hours after login, Angel One at midnight), and
>    each login needs your two-factor step (in a browser, or Angel One's
>    6-digit TOTP code). `ticket.py login` makes it one command.
>
> `ticket.py preflight <broker>` checks both before any live order. If either
> is a problem for you, stay on paper: everything else works the same.

| | Upstox | Zerodha Kite | Dhan | Angel One |
| --- | --- | --- | --- | --- |
| API version | v2 + v3 | Kite Connect v3 | DhanHQ v2 | SmartAPI |
| Read: profile, funds, holdings, positions, orders, trades | yes | yes | yes | yes |
| Quotes (LTP) and candles | yes, free | Connect plan only | Data API plan only | yes, free |
| Instrument master (incl. NFO symbols) | yes | yes | yes | yes (scrip master) |
| Basket margin | yes | yes | yes (multi-order calculator) | yes (margin calculator) |
| Native linked stop + target (`native_oco`) | yes: multi-leg GTT with the entry | yes: GTT two-leg OCO after the fill | yes: Super Order with the entry | no: GTT is single-leg, exits go leg by leg |
| Sandbox | yes (orders) | no | yes (closed environment) | no |
| Market protection for MARKET / SL-M | yes (`market_protection`) | yes (`market_protection`) | not wired: LIMIT and SL only | not wired: LIMIT and SL only |
| Daily token stops | 03:30 IST | 06:00 IST | 24 hours after login | midnight IST |
| Login | browser (OAuth) | browser | browser (consent) or token | client code + PIN + TOTP code, no browser |
| Static IPs on record, readable by API | `GET /v2/user/ip` | no (set in the console) | `GET /v2/ip/getIP` | no (set on smartapi.angelone.in) |

The code is `trader-broker/adapters/` (standard library only). The
contract is `adapters/base.py`; the tests are `tests/test_broker_adapters.py`
(one contract suite run against all four on recorded, secret-free fixtures)
and `tests/test_broker_v2.py` (linked exits, gates, reconcile, Angel One's
login, the margin cross-check).

## What it costs (as of 1 Nov 2026)

| broker | API | market data | notes | source |
| --- | --- | --- | --- | --- |
| Upstox | free | free | Brokerage applies per executed order like any trade. Zero-brokerage API trades ended 31 Aug 2024 (new API users got 90 days free). | [upstox.com update-zero-brokerage](https://upstox.com/developer/api-documentation/announcements/update-zero-brokerage) |
| Zerodha Kite | Personal: ₹0 (orders, GTT, portfolio, margins, instruments) | Connect: ₹500 a month per app (quotes, historical candles, live feed) | Historical data has been part of Connect at no extra cost since Feb 2025. | [kite.trade](https://kite.trade/) |
| Dhan | free (orders, portfolio, funds, statements) | Data APIs ₹499 a month (₹399 a month billed yearly) | The free-with-trades offer for data was discontinued. | [dhanhq.co docs: authentication](https://dhanhq.co/docs/v2/authentication/), dhan.co support (Data API pricing) |

Prices change: check the broker's own page before you pay.

## The rules that shape this (SEBI, in force since 1 Apr 2026)

SEBI's framework for retail algorithmic trading (circular of 4 Feb 2025,
phased in by the exchanges and fully in force from 1 Apr 2026) means, for
every broker here:

- **Orders only from your static IP.** Place, modify and cancel (and GTT,
  multi-order, exit-all) are accepted only from an IP you registered with
  the broker. Most allow a primary and a secondary IPv4 and let you change
  them about once a week (Upstox: once per calendar week, and a change logs
  you out; Dhan: locked for 7 days). Each static IP belongs to one client.
  Reading holdings, positions, funds and quotes works from any IP.
- **2FA once every trading day.** No long-lived sessions: you log in every
  morning. `ticket.py login` makes that one browser step.
- **At most 10 orders a second** without registering an algo with the
  exchange. This tool sends at most `order_rate_per_sec` (default 3, never
  above 9).
- **Market orders are protected.** Exchanges block plain MARKET orders from
  APIs; brokers convert MARKET and SL-M into market-protection orders. Kite
  and Upstox take a `market_protection` value (this tool sends -1, the
  broker's automatic protection). For Dhan this tool sends LIMIT and SL only.
- **Your own app, your own keys.** A self-built tool for your own account
  needs no exchange approval. Do not share keys, apps or static IPs, and do
  not run this for anyone else.

Upstox's page on the circular says the `X-Algo-Name` header is needed only for
an exchange-approved algo; this tool does not send it.

## Setup, once per broker

Credentials come from environment variables in your own shell, never from a
file this tool writes, never from the chat. A good place is your shell
profile or a password manager's CLI. **Never commit them.**

### Upstox

1. Go to **account.upstox.com/developer/apps** and create an app. Upstox
   allows one active app per user (the newest wins).
2. Redirect URL: `http://127.0.0.1:8765/upstox` (any free port; it must
   match exactly).
3. Register your static IP in **My Apps** (or with the Update Static IPs API).
4. In your shell:
   ```bash
   export BT_UPSTOX_API_KEY=...        # the app's API key (client_id)
   export BT_UPSTOX_API_SECRET=...
   export BT_UPSTOX_REDIRECT=http://127.0.0.1:8765/upstox
   ```
5. Sandbox (optional): create a **Sandbox App** on the same page and generate
   its token (valid 30 days, orders only, never usable live):
   `export BT_UPSTOX_SANDBOX_TOKEN=...`.

### Zerodha Kite

1. Go to **developers.kite.trade**, create an app: **Personal** (free) or
   **Connect** (₹500/month, adds quotes and candles). Your Zerodha login must
   have TOTP 2FA on.
2. Redirect URL: `http://127.0.0.1:8765/kite`.
3. Register your static IP in the developer console (Profile, IP whitelist;
   up to two, changeable about once a week).
4. In your shell:
   ```bash
   export BT_KITE_API_KEY=...
   export BT_KITE_API_SECRET=...
   export BT_KITE_REDIRECT=http://127.0.0.1:8765/kite
   ```

Kite has no sandbox: test on paper first.

### Dhan

1. Log in to **web.dhan.co**, then **My Profile**, then **Access DhanHQ APIs**.
2. Either generate an **access token** (valid 24 hours), or toggle to **API
   key**, enter an app name and the redirect URL `http://127.0.0.1:8765/dhan`
   (the key and secret are valid 12 months).
3. Set your static IP there (or with `POST /v2/ip/setIP`); it is locked for 7
   days after setting.
4. In your shell:
   ```bash
   export BT_DHAN_CLIENT_ID=...          # your Dhan client id
   export BT_DHAN_ACCESS_TOKEN=...       # if you generated a token on web.dhan.co
   # or, for the daily browser login:
   export BT_DHAN_API_KEY=...
   export BT_DHAN_API_SECRET=...
   export BT_DHAN_REDIRECT=http://127.0.0.1:8765/dhan
   ```
5. Sandbox (optional): `https://sandbox.dhan.co/v2`, a closed environment for
   the API shape and the order workflow, **not** paper trading with live
   prices: `export BT_DHAN_SANDBOX_TOKEN=...`.

Quotes and candles at Dhan need the Data API plan; without it the broker
answers DH-902, which this tool reports as "not in your plan".

### Angel One

1. On **smartapi.angelone.in**, use **New Login** and create a **static-IP API
   key** (IPv4 only). The static IP is registered against the key; SmartAPI
   has no API to read it back, so put the same IP in `"static_ip"` in
   `trader/config.json` and `preflight` compares this machine's IP with it.
2. Enable **TOTP** for your Angel One account (the SmartAPI site's "Enable
   TOTP" page shows a QR code and the secret behind it).
3. In your shell:
   ```bash
   export BT_ANGEL_API_KEY=...           # the SmartAPI key
   export BT_ANGEL_CLIENT_CODE=...       # your Angel One client code
   export BT_ANGEL_PIN=...               # your 4-digit PIN (SmartAPI's "password")
   # optional, and read the warning below first:
   export BT_ANGEL_TOTP_SECRET=...       # the base32 TOTP secret
   ```
4. Every day: `eval "$(python3 trader-broker/ticket.py login angel)"`. It asks
   for the 6-digit code from your authenticator app (typed, not shown, never
   on the command line), sends client code + PIN + code to
   `loginByPassword` and prints the `export` lines for the session token. The
   refresh and feed tokens that come back are dropped. The session ends at
   midnight IST.

**TOTP secret, security.** With `BT_ANGEL_TOTP_SECRET` set the tool computes
the code itself (RFC 6238, standard library). That is convenient and it turns
two factors into one: anyone who can read your shell environment (a process
listing tool, a crash report, a shared machine, a leaked dotfile) has your
PIN and your second factor and can log in as you, read your account and,
from your static IP, trade. Prefer typing the code. If you set the secret,
keep it in a password manager's CLI rather than a dotfile, never in
`trader/`, a repo or a chat, and rotate it (disable and enable TOTP again)
if you think it leaked. The tool never stores it, never prints it and scrubs
it from every message.

SmartAPI asks for client headers (`X-ClientLocalIP`, `X-ClientPublicIP`,
`X-MACAddress`). This tool sends `127.0.0.1`, your config's `static_ip` (or
`127.0.0.1`) and `00:00:00:00:00:00`: never your real MAC address.
SmartAPI's rate limit is per client code across every process; this tool
keeps reads at 3 a second and orders at your `order_rate_per_sec`.

## Every trading day: log in, then preflight

```bash
eval "$(python3 trader-broker/ticket.py login upstox)"     # browser login with 2FA; sets the token in THIS shell
python3 trader-broker/ticket.py preflight upstox           # static IP, token, one harmless call
python3 trader-broker/ticket.py holdings --broker upstox   # read only
eval "$(python3 trader-broker/ticket.py login angel)"      # Angel One: asks for the 6-digit TOTP code instead
```

`login` starts a one-shot server on `127.0.0.1` at the redirect URL's port,
opens the broker's login page, accepts exactly one callback, checks the
`state` value it sent (Upstox and Kite send it back; Dhan's consent flow has
none, so its single-use consent id and the one-callback window stand in),
exchanges the code with your secret and prints two lines:

```
export BT_UPSTOX_ACCESS_TOKEN=...
export BT_UPSTOX_TOKEN_DATE=2026-11-02T08:41:00+05:30
```

`eval "$(...)"` puts them in your shell; nothing is written to disk, the repo
or `trader/`, and the token is scrubbed from every message and log line.
`--no-browser` prints the login URL instead of opening it. The local UI and
the MCP server read the token from the environment of the shell that starts
them.

`preflight` prints PASS or FAIL for each check, in plain words:

- **credentials** and **login**: the token is set and a harmless call (your
  profile) works.
- **token**: not past the broker's daily cutoff (it uses
  `BT_<BROKER>_TOKEN_DATE` from `login`; Dhan's profile says the exact expiry).
- **public_ip**: what two IP echo services agree this machine's public IP is
  (`--ip` skips them).
- **static_ip**: that IP against `"static_ip"` in `trader/config.json` and,
  for Upstox and Dhan, the IPs the broker has on record. A mismatch means
  orders would be refused; reading still works.

The result goes to `trader/state/preflight.json` with a one-way tag of the
token (a hash prefix), never the token.

## Read-only commands

| command | shows |
| --- | --- |
| `ticket.py accounts --broker B` | masked account id, funds, token freshness, what the API can do, the plan |
| `ticket.py funds --broker B` | available, used, collateral |
| `ticket.py holdings --broker B` | delivery holdings with average, last price and P&L |
| `ticket.py positions --broker B` | today's positions (MIS, CNC, NRML) with P&L |
| `ticket.py orders --broker B` | today's order book and trades |
| `ticket.py instruments --broker B --symbol S --exchange NSE/NFO` | the broker's own symbol, id, lot, tick and freeze quantity |
| `ticket.py margin-check --broker B` | the broker's basket margin for the F&O legs in `trader/orders.json` next to our estimate (below) |
| `ticket.py reconcile --broker B` | the broker's GTT / super-order book matched to this tool's linked orders (below) |

`--json` prints the broker-neutral shapes of `adapters/base.py`. Without
`--broker` the config's `"broker"` is used. The local UI shows the same on
the Portfolio page (holdings, positions) and the Settings page (connection),
and the MCP server has `broker_status`, `broker_holdings`, `broker_positions`
and `broker_margin_check`. None of them can place an order.

### Symbols

A stock is its NSE symbol (`RELIANCE`, `M&M`). A future is
`NIFTY 2026-11-23 FUT`, an option `NIFTY 2026-11-23 25000 CE`, always with
`--exchange NFO`. Each broker's own identifier (Kite `NIFTY26NOV25000CE`, or
`NIFTY26N0925000CE` for a weekly; Upstox `NSE_FO|64439`; Dhan security id
`64439`; Angel One `NIFTY23NOV2625000CE` with token `64439`) comes from that
broker's daily instrument master, never from a formula. The masters agree on
NSE's exchange token, which the tests check across all four. Lot size, tick
size and freeze quantity come from the master too (Upstox, Dhan and Angel One
publish ticks in paise, Angel One its option strikes too; the adapters
convert. Angel One's `freeze_qty` is the first quantity refused, 1801 for
NIFTY, so the adapter keeps 1800).

## A live order: every gate

Going live is your own edit of `trader/config.json` (the UI refuses to change
`broker`, `paper` or `live_enabled`):

```json
{"broker": "upstox", "paper": false, "live_enabled": {"upstox": true},
 "static_ip": "203.0.113.7", "order_rate_per_sec": 3}
```

Then, for each order:

1. `trader-risk` passes that exact order today (its fingerprint).
2. `ticket.py confirm --index N --yes-text "<your words>"` records your yes.
   With a live config it only records it: nothing is sent.
3. `ticket.py preflight upstox` has passed today, with the token you have now
   (log in again and you preflight again; a preflight is good for 12 hours).
4. **You** run `ticket.py place --index N` in your own terminal.

`place` checks all six gates at the moment of sending: the config broker,
`"paper": false`, `live_enabled` for that broker, trader-risk PASS for this
exact order today, your recorded yes for it, and today's preflight. It sends
the entry only, rate limited, tagged `bt` plus the order's fingerprint. One
yes, one order: a second `place` is refused, and so is a `place` after the
broker refused the order (make a new ticket and say yes again). An order call
is never retried: if the connection drops, it says so and asks you to check
`ticket.py orders` for the tag first.

After the entry fills, the exits. Leg by leg (every broker):
`place --index N --leg stop` (the SL-M or SL stop) or `--leg target` (the
LIMIT target); only one exit leg works at the broker at a time, so to switch,
`cancel --index N --leg stop` first. Or, where the broker has one, as **one
native linked order** (next section): the broker holds the stop and the
target together and cancels the other when one fills, even if this machine
is off.

## Native linked exits: one stop + target at the broker

| broker | how | when | `--leg` |
| --- | --- | --- | --- |
| Zerodha Kite | GTT two-leg (OCO): two triggers, two LIMIT orders | after the entry fills | `oco` |
| Upstox | multi-leg GTT: ENTRY (IMMEDIATE, a limit order sent at once) + TARGET + STOPLOSS | with the entry, in place of it | `bracket` |
| Dhan | Super Order: entry + target + stop loss in one order | with the entry, in place of it | `bracket` |
| Angel One | none (SmartAPI's GTT rules are single-leg) | | leg by leg |

```bash
python3 trader-broker/ticket.py place --index 1 --leg bracket   # Upstox, Dhan: entry and exits as ONE order
python3 trader-broker/ticket.py place --index 1                 # Kite: the entry first ...
python3 trader-broker/ticket.py place --index 1 --leg oco       # ... then, after the fill, one GTT OCO
python3 trader-broker/ticket.py cancel --index 1 --leg oco      # cancel it (the same yes covers its cancel)
python3 trader-broker/ticket.py reconcile                       # after a restart: the broker's book vs ours
```

The same six gates as any order, at the moment of sending: the config
broker, `"paper": false`, `live_enabled`, trader-risk PASS for this exact
ticket today (its stop and target are part of the fingerprint), your typed
yes for it and today's preflight. **Your yes for a ticket covers that
ticket's own linked exit and nothing else**: the pass is sealed to the exact
stop-and-target order of that ticket, is used once, and cannot send a plain
order, another ticket's exits or edited prices. A ticket's exits go one way
only: leg by leg or one linked order, never both (a working leg-by-leg stop
must be cancelled before an OCO; a bracket is refused after a separate entry
and vice versa). A yes recorded by trader-autopilot (`--actor autopilot`)
never opens a live gate, linked or not: the auto-pilot is paper only.

What each broker needs:

- **Kite**: GTT legs are LIMIT orders, so the ticket's stop must be an SL
  with a limit `price` (an SL-M stop is refused for a native OCO; send it leg
  by leg). The GTT needs the last price: the entry's fill price, or
  `--last-price`. If the market has already crossed a trigger, the OCO is
  refused (it would fire at once).
- **Upstox and Dhan**: the entry must be a LIMIT order (the exits hang on its
  price); the stop goes as the stop's trigger price (`stopLossPrice` at Dhan,
  the STOPLOSS rule's trigger at Upstox) and executes as the broker's own
  stop. Trailing is off (`trailingJump` 0, no `trailing_gap`).

**A new stop or target** is a new ticket, never a quiet edit: write the
ticket again with the same symbol, side, qty and product and the new
`stop_loss` / `target`, run trader-risk, say yes to it, then
`ticket.py modify-exits --index NEW --linked-id ID`. That changes the working
linked order (Kite `PUT /gtt/triggers/:id`, Upstox `PUT /v3/order/gtt/modify`,
Dhan `PUT /super/orders/:id` for the TARGET_LEG and STOP_LOSS_LEG) under the
new ticket's own PASS and yes; one yes, one change.

**Reconciliation on restart.** `ticket.py reconcile` reads the broker's book
of linked orders (Kite `GET /gtt/triggers`, Upstox `GET /v3/order/gtt`, Dhan
`GET /super/orders`) and matches it to this tool's records of the last 7
days: by id, and for a send whose outcome was unknown (the connection dropped)
by Dhan's correlation id or by the same qty, side, stop and target. It
prints each one (matches, prices differ, not in the broker's book, found),
lists the broker's linked orders this tool did not make (left alone), and
records an unknown send it found so it can be cancelled or changed. It sends,
changes and cancels nothing. Upstox's GTT list leaves out completed GTTs, so
"not in the broker's book" can mean it completed.

What it maps, one to one, and refuses otherwise:

| neutral | Upstox | Kite | Dhan |
| --- | --- | --- | --- |
| `product` MIS / CNC / NRML | `I` / `D` / `D` | `MIS` / `CNC` / `NRML` | `INTRADAY` / `CNC` / `MARGIN` |
| `order_type` LIMIT, SL, SL-M, MARKET | same | same | `LIMIT`, `STOP_LOSS`, `STOP_LOSS_MARKET`, `MARKET` |
| `exchange` NSE / NFO | instrument key segment | `NSE` / `NFO` | `NSE_EQ` / `NSE_FNO` |
| our tag | `tag` | `tag` (20 chars) | `correlationId` |
| Angel One | product `INTRADAY` / `DELIVERY` / `CARRYFORWARD`; `STOPLOSS_LIMIT`, `STOPLOSS_MARKET` (variety `STOPLOSS`); `ordertag` under 20 chars | | |

Before any network call it refuses: a qty that is not a multiple of the lot
or is above the freeze quantity, a price off the tick, CNC on NFO, NRML on a
stock, a LIMIT without a price, an SL-M with one, a BUY SL with the trigger
above the limit.

Errors come back in plain words, normalised: token expired (log in again),
IP not whitelisted (Upstox UDAPI1154 and any broker message about a static
IP), rate limited, insufficient funds, rejected, not in your plan (Kite
PermissionException, Dhan DH-902), broker down.

## The broker's basket margin next to ours

trader-strategy and trader-risk estimate an F&O basket's margin SPAN-style
(+/- about 15%). With a broker token in your shell they also show **the
broker's own basket margin** and the difference:

```bash
python3 trader-broker/ticket.py margin-check --broker kite
# Zerodha Kite basket margin for 2 legs at 10:02 IST: ₹34,786.73.
# Our SPAN-style estimate: ₹30,000.00, from trader-strategy (trader/strategy.json). Difference: +₹4,786.73 (+16.0%).
```

It calls Kite `POST /margins/basket` (with your positions considered),
Upstox `POST /v2/charges/margin`, Dhan `POST /margincalculator/multi` or
Angel One `POST margin/v1/batch`: read-only calculators, nothing is placed.
The result is kept for the day in `trader/state/broker_margin.json`, keyed by
the basket's legs, so trader-strategy (`Broker basket margin (...)` under the
margin line, `margin.broker_basket` in its JSON) and trader-risk (an info
line with the basket, and a warning when the broker blocks more than the
figure it used) show it next to ours. When today's figure is missing and the
config broker's token (or `"margin_broker"`'s, for a paper config) is in the
shell, they run `margin-check` once themselves; `strategy.py
--no-broker-margin` and `"broker_margin_check": false` turn that off. It
never changes our estimate or a verdict: the broker's figure is shown, and
it is the one the broker blocks. The local UI's Settings page has a "Check
basket margin" card, and the MCP tool is `broker_margin_check`.

## OpenAlgo bridge (not built yet)

If you run your own [OpenAlgo](https://github.com/marketcalls/openalgo)
server, an adapter could reach its brokers over HTTP: one more module in
`adapters/` implementing `base.Adapter` against your server's REST API (its
API key in `BT_OPENALGO_API_KEY`, its URL in the config), read side first,
then `order_request` behind the same `GatePass`. No OpenAlgo code would be
copied (it is AGPL; this repo is MIT, and calling a server over HTTP is not
vendoring). It is not in this release; the contract suite in
`tests/test_broker_adapters.py` is what such an adapter would have to pass.

## Sandbox smoke tests (opt in)

```bash
BT_SANDBOX=1 BT_UPSTOX_SANDBOX_TOKEN=... python3 -m unittest tests.test_broker_adapters.UpstoxSandboxSmoke
BT_SANDBOX=1 BT_DHAN_SANDBOX_TOKEN=... BT_DHAN_CLIENT_ID=... python3 -m unittest tests.test_broker_adapters.DhanSandboxSmoke
```

They are skipped unless those variables are set, and they only touch the
sandbox hosts (`api-sandbox.upstox.com`, `sandbox.dhan.co`).

## What was checked, and what was not

The endpoints, fields, error codes and example responses come from each
broker's official API docs, read on 1 Nov 2026
([Upstox](https://upstox.com/developer/api-documentation/),
[Kite Connect v3](https://kite.trade/docs/connect/v3/),
[DhanHQ v2](https://dhanhq.co/docs/v2/),
[SmartAPI](https://smartapi.angelone.in/docs)); the instrument masters are the
real public files of that day. SmartAPI's printed examples are often not
valid JSON (missing commas and braces, unquoted values); each fixture says how
it was repaired. Not verified against a live account by this project:

- Dhan's multi-order margin request: its docs show two spellings
  (`scripList`/`includeOrder` in the curl example, `scripts`/`includeOrders`
  in the request structure); the adapter sends the second.
- The HTTP status codes of some error responses (the docs give the error
  body and code, not always the status).
- Whether each broker's console accepts an `http://127.0.0.1:<port>` redirect
  URL for your app type in 2026 (Kite has historically; Upstox's docs only ask
  for a URL "in your control").
- The exact wording of each broker's static-IP rejection apart from Upstox's
  UDAPI1154.
- Native linked orders were not sent to a real account: how Upstox fills an
  IMMEDIATE ENTRY rule and executes its STOPLOSS rule (as SL-M with
  protection?), how Dhan executes the super order's stop leg, and Kite's
  exact rule for how far a GTT trigger must sit from the last price.
- Angel One: whether SmartAPI checks the client IP and MAC headers, the HTTP
  status of its failed responses (most come back as HTTP 200 with
  `"status": false`), the `getPosition` P&L fields (not in the docs' example,
  so P&L shows as unknown when missing) and whether market orders get
  automatic protection (not wired: LIMIT and SL only).
