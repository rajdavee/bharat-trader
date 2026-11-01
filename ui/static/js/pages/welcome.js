// The setup guide (first run): your account limits -> trader/config.json through the validated settings API,
// then connect an AI assistant (MCP) and a broker (read only). Every step can be skipped.
import { get, post } from '../api.js';
import { h, clear, card, note } from '../dom.js';
import { help } from '../why.js';
import { stepper, toast, failure, skeleton, pageHead } from '../ui.js';

const STEPS = ['Your account', 'AI assistant', 'Broker (read only)', 'Done'];
const S = { step: 0, done: new Set(), info: null, form: null, preview: null, timer: null };

function skipAll() {
  try { localStorage.setItem('bt-setup-skipped', '1'); } catch (e) { /* private window */ }
  location.hash = '#/';
}

function go(root, ctx, step) {
  S.step = step;
  draw(root, ctx);
  const t = root.querySelector('.wizard h2');
  if (t) { t.setAttribute('tabindex', '-1'); t.focus(); }
}

function actions(root, ctx, { back = true, next, nextLabel = 'Continue', skip = true, nextEl = null }) {
  return h('div', { class: 'wizard-actions' },
    h('div', { class: 'row' }, back && S.step > 0 ? h('button', { type: 'button', class: 'btn', onclick: () => go(root, ctx, S.step - 1) }, 'Back') : null),
    h('div', { class: 'row' },
      skip ? h('button', { type: 'button', class: 'btn quiet', onclick: () => go(root, ctx, S.step + 1) }, 'Skip this step') : null,
      next ? h('button', { type: 'button', class: 'btn primary', onclick: next }, nextLabel) : null, nextEl));
}

// ---------------------------------------------------------------- step 1: account

function field(label, input, hint, term) {
  return h('label', null, h('span', null, label, term ? [' ', help(term)] : null), input, hint ? h('small', null, hint) : null);
}

function accountStep(root, ctx) {
  const i = S.info;
  const f = S.form || (S.form = { capital: '', risk_pct: String(i.defaults.risk_pct), max_trades: String(i.defaults.max_trades),
    product: i.defaults.product, broker_profile: i.defaults.broker_profile });
  const out = h('div', { class: 'consequence', 'aria-live': 'polite' },
    h('p', { class: 'muted' }, 'Enter your capital to see what these limits mean in rupees.'));
  const save = h('button', { type: 'button', class: 'btn primary', disabled: true }, 'Save settings and continue');
  const inp = (key, attrs) => {
    const el = h('input', Object.assign({ value: f[key], name: key, id: 'w-' + key }, attrs));
    el.addEventListener('input', () => { f[key] = el.value; schedule(); });
    return el;
  };
  const sel = (key, opts) => {
    const el = h('select', { name: key, id: 'w-' + key }, opts.map(([v, l]) => h('option', { value: v, selected: f[key] === v }, l)));
    el.addEventListener('change', () => { f[key] = el.value; schedule(); });
    return el;
  };
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(preview, 250);
  }
  async function preview() {
    if (!String(f.capital).trim()) {
      S.preview = null;
      save.disabled = true;
      clear(out).appendChild(h('p', { class: 'muted' }, 'Enter your capital to see what these limits mean in rupees.'));
      return;
    }
    const q = new URLSearchParams({ capital: f.capital, risk_pct: f.risk_pct, max_trades: f.max_trades, product: f.product,
      broker_profile: f.broker_profile });
    try {
      const d = await get('/api/onboarding?' + q.toString());
      S.preview = d.preview;
      clear(out);
      if (d.preview.errors.length) {
        save.disabled = true;
        d.preview.errors.forEach((e) => out.appendChild(note(e, 'error')));
        return;
      }
      save.disabled = false;
      const lines = String(d.preview.summary || '').split('\n').map((x) => x.replace(/^\s*-\s*/, '')).filter(Boolean);
      out.append(h('strong', null, 'What this means'), h('ul', null, lines.map((x) => h('li', null, x))));
      d.preview.warnings.forEach((w) => out.appendChild(note(w, 'warn')));
    } catch (e) {
      save.disabled = true;
      clear(out).appendChild(failure(e.message || String(e)));
    }
  }
  save.addEventListener('click', async () => {
    if (!S.preview || !S.preview.config) return;
    save.disabled = true;
    try {
      const r = await post('/api/config', { config: S.preview.config });
      if (!r.saved) {
        clear(out);
        r.errors.forEach((e) => out.appendChild(note(e, 'error')));
        save.disabled = false;
        return;
      }
      toast('Settings saved to trader/config.json');
      S.done.add(0);
      await ctx.shell.refreshMeta();
      go(root, ctx, 1);
    } catch (e) {
      clear(out).appendChild(failure(e.message || String(e)));
      save.disabled = false;
    }
  });
  if (String(f.capital).trim()) preview();
  return card('Your account',
    h('p', null, 'Four answers set every limit the risk check uses. You can change any of them later in Settings.'),
    i.config_exists ? note('You already have trader/config.json. Saving here replaces it; the old file is kept as ' +
      'config.json.bak. Skip this step to keep your settings.', 'warn') : null,
    h('div', { class: 'form-grid' },
      field('Capital (₹)', inp('capital', { type: 'number', min: '5000', step: '1000', inputmode: 'numeric', placeholder: '100000',
        required: true }), 'The money this account trades with, on paper for now.'),
      field('Risk per trade (% of capital)', inp('risk_pct', { type: 'number', min: '0.1', max: '5', step: '0.1', inputmode: 'decimal' }),
        'Most one trade may lose at its stop. 0.5 to 1 is common.', 'risk_pct'),
      field('Trades per day (most)', inp('max_trades', { type: 'number', min: '1', max: '20', step: '1', inputmode: 'numeric' }),
        'New entries allowed in one day.', 'trades_left'),
      field('Product', sel('product', i.products.map((p) => [p.value, p.label])), 'Intraday closes the same day; delivery keeps shares.', 'mis'),
      field('Broker (for square-off times)', sel('broker_profile', [['', 'Not sure: use a safe default']]
        .concat(i.profiles.map((p) => [p, p.charAt(0).toUpperCase() + p.slice(1)]))), 'Only sets the intraday exit time. ' +
        'It does not connect anything.', 'broker_profile')),
    out,
    actions(root, ctx, { next: null, skip: true, nextEl: save }));
}

// ---------------------------------------------------------------- step 2: assistant

function copyButton(text, what) {
  return h('button', { type: 'button', class: 'btn', onclick: async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast(what + ' copied');
    } catch (e) {
      toast('Copy is blocked here: select the text and copy it by hand', 'error');
    }
  } }, 'Copy ' + what.toLowerCase());
}

function assistantStep(root, ctx) {
  const m = S.info.mcp;
  return card('Connect your AI assistant',
    h('p', null, 'Any MCP-compatible assistant can read the market, run scans, price strategies and draft paper orders on ' +
      'this folder. It cannot place an order: every order still needs a risk check PASS and your typed yes. ', help('mcp')),
    h('ol', null,
      h('li', null, 'Open your assistant\'s MCP settings (often a JSON file named in its docs).'),
      h('li', null, 'Add this server entry and restart the assistant:'),
    ),
    h('pre', { class: 'snippet', tabindex: '0', 'aria-label': 'MCP server settings' }, m.snippet),
    h('div', { class: 'row' }, copyButton(m.snippet, 'Settings')),
    h('p', { class: 'small muted' }, 'To check the server runs, list its tools in a terminal: ', h('code', null, m.test),
      '. More in ', h('code', null, m.doc), '.'),
    actions(root, ctx, { next: () => { S.done.add(1); go(root, ctx, 2); }, nextLabel: 'I have added it', skip: true }));
}

// ---------------------------------------------------------------- step 3: broker

function brokerStep(root, ctx) {
  const rows = S.info.brokers || [];
  return card('Connect a broker, read only',
    h('p', null, S.info.broker_rule),
    note('Optional. Paper trading needs no broker. Credentials live in environment variables on your machine, never in ' +
      'this app or its files.', 'info'),
    rows.length ? h('div', { class: 'grid' }, rows.map((b) => {
      const vars = Object.entries(b.credentials || {});
      const set = vars.filter((x) => x[1]).length;
      return h('section', { class: 'card', 'aria-label': b.title },
        h('h3', { class: 'card-title' }, b.title, h('span', { class: 'badge ' + (set ? 'pass' : '') }, set ? `${set} of ${vars.length} set` : 'not set')),
        h('ul', { class: 'small' }, vars.map(([k, v]) => h('li', null, h('code', null, k), v ? ' set' : ' not set'))));
    })) : null,
    h('p', { class: 'small muted' }, 'Then log in once a day in a terminal: ', h('code', null, 'python3 trader-broker/ticket.py login <broker>'),
      '. Holdings and positions appear on the Portfolio page. Full steps in ', h('code', null, S.info.broker_doc), '.'),
    actions(root, ctx, { next: () => { S.done.add(2); go(root, ctx, 3); }, skip: true }));
}

// ---------------------------------------------------------------- step 4: done

function doneStep(root, ctx) {
  const dens = document.documentElement.dataset.density;
  const pick = (v, label, text) => h('button', { type: 'button', class: 'btn' + (dens === v ? ' primary' : ''), 'aria-pressed': String(dens === v),
    onclick: () => { try { localStorage.setItem('bt-density', v); } catch (e) { /* ok */ } ctx.shell.setDensity(v); draw(root, ctx); } },
  h('span', null, h('strong', null, label), h('br'), h('span', { class: 'small' }, text)));
  return card('You are set up',
    h('ul', null,
      h('li', null, S.done.has(0) || S.info.config_exists ? 'Your limits are saved. The risk check uses them for every order.'
        : 'No settings saved yet: the risk check uses safe defaults until you do.'),
      h('li', null, 'Everything is paper trading. Nothing here can place a live order.'),
      h('li', null, 'Press Ctrl K (⌘ K on a Mac) anywhere to jump to a page or chart a symbol.')),
    h('h3', { class: 'sub' }, 'How much explanation do you want?'),
    h('div', { class: 'row' }, pick('beginner', 'Guided', 'plain-words notes under the numbers'),
      pick('expert', 'Expert', 'denser, notes hidden')),
    h('div', { class: 'wizard-actions' },
      h('button', { type: 'button', class: 'btn', onclick: () => go(root, ctx, 2) }, 'Back'),
      h('div', { class: 'row' }, h('a', { class: 'btn', href: '#/settings' }, 'Review all settings'),
        h('a', { class: 'btn primary', href: '#/' }, 'Go to Today'))));
}

function draw(root, ctx) {
  clear(root);
  root.append(...pageHead('Set up bharat-trader', 'A few minutes, every step optional. You can come back here from the menu ' +
    '(Setup guide).', h('button', { type: 'button', class: 'btn quiet', onclick: skipAll }, 'Skip setup for now')));
  const done = new Set(S.done);
  if (S.info.config_exists) done.add(0);
  const wrap = h('div', { class: 'wizard' }, stepper(STEPS, S.step, done));
  const step = [accountStep, assistantStep, brokerStep, doneStep][S.step](root, ctx);
  wrap.appendChild(step);
  root.appendChild(wrap);
}

export async function render(root, ctx) {
  clear(root).appendChild(skeleton(4));
  try {
    S.info = await get('/api/onboarding');
  } catch (e) {
    clear(root).appendChild(failure(e.message || String(e), () => render(root, ctx)));
    return;
  }
  S.step = Math.min(S.step, STEPS.length - 1);
  if (ctx.params.step) S.step = Math.max(0, Math.min(STEPS.length - 1, Number(ctx.params.step) || 0));
  draw(root, ctx);
}
