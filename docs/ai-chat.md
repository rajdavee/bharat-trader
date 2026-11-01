# Ask: chat with your own LLM inside the UI (optional)

The local UI has an **Ask** page (Plan → Ask). It is a chat with an LLM
endpoint *you* choose and pay for, and the model uses the same tools as
the MCP server ([mcp.md](mcp.md)). It is **off by default**. If you already
use an MCP-compatible assistant you don't need this page: connect that
assistant to `mcp/server.py` instead.

The Ask panel exists for people who have an API key for some LLM service,
or run a model on their own computer, and want the co-trader in the same
window as their charts and tickets.

## Turn it on

Settings come from **environment variables only**. You never type a key
into the page, and nothing is saved to `trader/`.

```bash
export BT_LLM_BASE_URL="https://your-llm-endpoint.example/v1"   # the API's base URL
export BT_LLM_MODEL="your-model-name"
export BT_LLM_API_KEY="..."                                     # optional for a local model
export BT_LLM_FORMAT="chat-completions"                         # or: messages
python3 ui/serve.py --open
```

| variable | meaning |
| --- | --- |
| `BT_LLM_BASE_URL` | Base URL of the API. It must be `https`, except on this computer (`http://127.0.0.1:PORT/...` for a local model server). Never put the key in the URL. |
| `BT_LLM_MODEL` | The model name your endpoint expects. |
| `BT_LLM_API_KEY` | Your key. It is read from the environment. It is never logged and never sent to the browser, and it is removed from every error message. |
| `BT_LLM_FORMAT` | `chat-completions` (the default) or `messages`. See below. |
| `BT_LLM_EXTRA_HEADERS` | Optional JSON object of extra headers, for example a version header some APIs want: `{"X-Api-Version": "2024-01-01"}`. |
| `BT_LLM_AUTH_HEADER` | Optional name of the header that carries the key. The default is `Authorization: Bearer <key>` for chat-completions and `x-api-key: <key>` for messages. Some gateways want something else, such as `api-key`. |
| `BT_LLM_MAX_TOKENS`, `BT_LLM_TIMEOUT` | Optional reply length (default 1500) and timeout in seconds (default 90). |

If any setting is missing or wrong, the page says so and shows these
variables. No request is sent.

## The two formats, by shape

Most hosted and local LLM servers use one of two common HTTP chat APIs. Your
provider's docs will say which one it speaks.

**`chat-completions`: an OpenAI-compatible chat completions API.**
`POST {base}/chat/completions` with `{"model", "messages": [{"role",
"content"}], "tools": [{"type": "function", "function": {"name",
"description", "parameters"}}]}`. The reply is `choices[0].message`. If the
model wants a tool, that message carries `tool_calls` (each with an `id` and
a `function` holding `name` and `arguments` as JSON text). The relay sends
each tool's result back as a `{"role": "tool", "tool_call_id", "content"}`
message. Many local model servers and gateways speak this format.

**`messages`: a messages-style API with tool use.**
`POST {base}/messages` with `{"model", "max_tokens", "system", "messages",
"tools": [{"name", "description", "input_schema"}]}`. The reply's `content`
is a list of blocks: `{"type": "text"}` and `{"type": "tool_use", "id",
"name", "input"}`. The relay sends the results back in one user message made
of `{"type": "tool_result", "tool_use_id", "content"}` blocks.

For both formats the relay does the whole loop on the server. It sends your
question, runs any tools the model asks for, sends back the results, and
repeats until the model answers in text. The cap is 8 rounds per question.

## What the model can and cannot do

- **Tools.** The model sees the read-only tools from the MCP tool table
  (regime, co-trader, calibration, lab reports, comparisons,
  `best_for_regime`, `trade_brief`, `strategy_design`, journal, options and
  strategy pricing, and so on) plus `paper_order_draft`. Each call runs the
  same function the MCP server runs (`mcp/toolkit.py`).
- **No orders.** `paper_order_confirm` is never offered. If the model asks
  for it anyway, the call is refused. A drafted paper ticket waits on
  **Portfolio and orders**, where only you can confirm it, by typing your
  own yes. Live orders do not exist here at all.
- **No live broker reads.** The broker tools (holdings, positions,
  connection) are left out, so your broker account data never goes to the
  LLM.
- **No lab runs from chat.** `lab_run`, `lab_run_spec` and
  `discover_strategies` write to `trader/` and use up trials, so the chat
  can draft a spec with `strategy_design` but not run it. Run it from the
  Strategy Lab, the command line, or an MCP assistant, where each run is
  counted in the trial ledger.
- **A fixed system prompt.** The model is told to put evidence first: show
  n and the 95% interval with every probability, give numbers after charges,
  name the regime, say what the evidence does NOT show, make no promises,
  and remember that proposing is not placing.

## Privacy

> **Your data goes to the LLM provider you configured.** Your questions,
> and the tool results the model asks for (prices, plans, your paper
> account, lab and journal numbers), are sent to that endpoint. Its own
> terms and retention rules apply.

- **Consent first.** Nothing is sent until you tick the consent box on the
  Ask page. Consent belongs to one browser session. Untick it and the
  conversation is forgotten and nothing more goes out.
- **Local only.** The Ask routes answer requests from `127.0.0.1` only,
  even if you started the UI on another address. They also need the
  session cookie and its CSRF token, like every other write.
- **The key stays on the server.** The browser talks only to the local UI.
  The key goes only in the request to your endpoint. It is never in a
  response, never in a log, and never in an error message (those are
  scrubbed).
- **No storage.** The conversation lives in the UI process's memory, at
  most 40 messages per session. A restart forgets it.

## Routes

| route | what it does |
| --- | --- |
| `GET /api/ask` | On or off and why; the format, model and endpoint host (never the key); this session's consent; the tools the model may call. |
| `POST /api/ask/consent` | `{"consent": true}` or `false`, for this session. |
| `POST /api/ask` | `{"message": "..."}` gives `{"reply", "steps": [{"tool", "ok", "summary"}], "drafted"}`. |
| `POST /api/ask/reset` | Forget this session's conversation. |

## Tests

`tests/test_ai_chat.py` runs offline. A stub LLM HTTP server speaks both
formats and makes multi-step tool calls (regime, then two tools in one
turn, then an answer). The tests also check: consent is required, keys are
scrubbed from errors and responses, only loopback and only with a session,
a model that never stops is stopped, and no order can be placed from chat.
If the model asks to confirm an order or place a live one, the call is
refused and the paper broker's log is unchanged.

Education and tooling, not financial advice.
