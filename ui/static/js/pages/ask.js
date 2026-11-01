// Ask: an optional chat with YOUR OWN LLM, configured on the server through environment variables only. The server
// relays each question and runs the assistant's tool calls itself (read-only tools and paper drafts only); the
// browser never sees the key. Nothing is sent before this session's consent is switched on. A drafted paper ticket
// still needs your own typed yes on Portfolio and orders.
import { get, post } from '../api.js';
import { h, clear, card, kv, note } from '../dom.js';
import { empty, skeleton, failure, pageHead, toast } from '../ui.js';

const STATE = { log: [] };

function setup(s) {
  return card('Turn it on (optional)',
    h('p', null, 'The Ask panel is off. It talks to an LLM endpoint you choose, with your own key, set in the ' +
      'environment of the computer that runs this UI. Then restart the UI.'),
    h('pre', { class: 'snippet' }, [
      'export BT_LLM_BASE_URL=<your endpoint base URL, often ending in /v1>',
      'export BT_LLM_MODEL=your-model-name',
      'export BT_LLM_API_KEY=...            # never typed into this page',
      'export BT_LLM_FORMAT=chat-completions   # or: messages',
      'python3 ui/serve.py --open',
    ].join('\n')),
    h('p', { class: 'small muted' }, s.why_off || ''),
    h('p', { class: 'small' }, 'Formats: "chat-completions" (an OpenAI-compatible chat completions API) or ' +
      '"messages" (a messages-style API with tool use). See docs/ai-chat.md. Your MCP-compatible assistant can ' +
      'use the same tools without this panel: docs/mcp.md.'));
}

function stepLine(s) {
  const cls = s.refused ? 'block' : s.ok ? 'pass' : 'warn';
  return h('li', null, h('span', { class: 'badge ' + cls }, s.tool), ' ',
    h('span', { class: 'small muted' }, s.refused ? 'refused: ' + s.summary : s.summary || ''));
}

function bubble(m) {
  if (m.role === 'user') return h('div', { class: 'ask-msg ask-user' }, h('p', null, m.text));
  const parts = [];
  if (m.steps && m.steps.length) {
    parts.push(h('details', { class: 'ask-steps' }, h('summary', null, `${m.steps.length} tool call(s)`),
      h('ul', null, m.steps.map(stepLine))));
  }
  parts.push(...String(m.text || '').split(/\n{2,}/).map((p) => h('p', null, p)));
  if (m.drafted) {
    parts.push(note(m.next_step || 'A paper ticket was drafted. Nothing was placed.', 'warn'));
    parts.push(h('p', null, h('a', { class: 'btn', href: '#/portfolio' }, 'Review it on Portfolio and orders')));
  }
  return h('div', { class: 'ask-msg ask-bot' }, parts);
}

export async function render(root) {
  root.append(...pageHead('Ask your co-trader',
    'Optional. Questions go to the LLM you configured; its answers must come from this app\'s tools, with sample ' +
    'sizes, ranges and after-charges numbers. It can propose and explain. It cannot place or confirm an order.'));
  const body = h('div', { class: 'stack' });
  root.appendChild(body);
  body.appendChild(skeleton(3));
  let s;
  try {
    s = await get('/api/ask');
  } catch (e) {
    clear(body).appendChild(failure(e.message, () => { clear(root); render(root); }));
    return;
  }
  clear(body);
  if (!s.enabled) {
    body.appendChild(setup(s));
    return;
  }
  const consent = h('input', { type: 'checkbox', id: 'ask-consent', checked: s.consent || null });
  const notice = card('Privacy',
    note(s.notice, 'warn'),
    kv([['Endpoint', s.endpoint_host], ['Format', s.format], ['Model', s.model],
      ['Key', s.key_set ? 'set on the server (never shown here)' : 'none (a local endpoint)'],
      ['Tools it may call', String((s.tools || []).length) + ' read-only tools and the paper draft']]),
    h('p', { class: 'row' }, consent,
      h('label', { for: 'ask-consent' }, 'Send this session\'s questions and tool results to ' + s.endpoint_host)));
  body.appendChild(notice);

  const log = h('div', { class: 'ask-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
  const box = h('textarea', { id: 'ask-q', rows: '3', maxlength: '4000',
    placeholder: 'e.g. Which lab-tested strategies have an edge in today\'s regime? Show n and the ranges.' });
  const send = h('button', { type: 'button', class: 'btn primary' }, 'Ask');
  const reset = h('button', { type: 'button', class: 'btn quiet' }, 'Clear conversation');
  const form = h('div', { class: 'ask-form' }, h('label', { for: 'ask-q', class: 'sr-only' }, 'Your question'), box,
    h('div', { class: 'row' }, send, reset));
  body.appendChild(card('Conversation', log, form,
    h('p', { class: 'small muted' }, 'Education and tooling, not financial advice. Check every number against ' +
      'the page it came from.')));

  const draw = () => {
    clear(log);
    if (!STATE.log.length) log.appendChild(empty('No questions yet', 'Try: "Give me today\'s co-trader brief" or ' +
      '"Design an expiry-day NIFTY strategy and test it once in the lab."'));
    STATE.log.forEach((m) => log.appendChild(bubble(m)));
  };
  const enable = () => {
    const on = consent.checked;
    box.disabled = !on;
    send.disabled = !on;
  };
  draw();
  enable();

  consent.addEventListener('change', async () => {
    try {
      await post('/api/ask/consent', { consent: consent.checked });
      if (!consent.checked) STATE.log = [];
      draw();
      toast(consent.checked ? 'Consent on for this session' : 'Consent off: nothing more is sent');
    } catch (e) {
      consent.checked = !consent.checked;
      toast(e.message, 'error');
    }
    enable();
  });
  reset.addEventListener('click', async () => {
    try {
      await post('/api/ask/reset', {});
      STATE.log = [];
      draw();
    } catch (e) {
      toast(e.message, 'error');
    }
  });
  const ask = async () => {
    const text = box.value.trim();
    if (!text) return;
    STATE.log.push({ role: 'user', text });
    draw();
    box.value = '';
    send.disabled = true;
    send.textContent = 'Thinking…';
    try {
      const r = await post('/api/ask', { message: text });
      STATE.log.push({ role: 'bot', text: r.reply, steps: r.steps, drafted: r.drafted, next_step: r.next_step });
    } catch (e) {
      STATE.log.push({ role: 'bot', text: 'Error: ' + e.message, steps: [] });
    }
    send.textContent = 'Ask';
    enable();
    draw();
  };
  send.addEventListener('click', ask);
  box.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) ask();
  });
}
