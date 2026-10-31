// trader/config.json: every key with its meaning (from docs/configuration.md), validated by the server
// with the skills' own parsers before it is saved. Paper trading cannot be switched off here.
import { get, post } from '../api.js';
import { h, clear, card, note, loading } from '../dom.js';

const LOCKED = new Set(['paper', 'broker', 'exchange']);
const GROUPS = [
  ['Money and risk', ['capital', 'risk_per_trade_pct', 'max_loss_per_trade', 'max_daily_loss', 'max_trades_per_day',
    'max_orders_per_day', 'max_position_pct', 'max_order_value', 'min_order_value', 'cash_buffer_pct',
    'min_reward_risk', 'require_stop', 'include_charges']],
  ['Time (IST)', ['no_entries_before', 'no_entries_after', 'flat_by', 'extra_holidays', 'special_sessions']],
  ['Market and broker', ['exchange', 'product', 'tick_size', 'broker', 'paper', 'blocked', 'allow_short', 'allow_fno',
    'min_price', 'min_turnover', 'insider_watch_clients', 'risk_free_rate']],
  ['Charges', ['charges']],
];

function fieldFor(s, cfg) {
  const id = 'cfg-' + s.key;
  const has = Object.prototype.hasOwnProperty.call(cfg, s.key);
  const cur = has ? cfg[s.key] : undefined;
  const dflt = typeof s.default === 'object' ? JSON.stringify(s.default) : String(s.default);
  let input;
  if (s.type === 'bool') {
    input = h('select', { id, name: s.key },
      h('option', { value: '' }, `default (${dflt})`),
      h('option', { value: 'true', selected: cur === true ? true : null }, 'true'),
      h('option', { value: 'false', selected: cur === false ? true : null }, 'false'));
  } else if (s.type === 'list' || s.type === 'object') {
    input = h('textarea', { id, name: s.key, rows: s.type === 'object' ? 6 : 2, spellcheck: 'false',
      placeholder: dflt });
    input.value = has ? JSON.stringify(cur, null, s.type === 'object' ? 2 : 0) : '';
  } else {
    input = h('input', { id, name: s.key, type: 'text', inputmode: s.type === 'number' ? 'decimal' : null,
      placeholder: dflt, spellcheck: 'false' });
    input.value = has ? String(cur) : '';
  }
  input.dataset.type = s.type;
  if (LOCKED.has(s.key)) input.disabled = true;
  return h('div', { class: 'field' },
    h('label', { for: id }, h('code', null, s.key), LOCKED.has(s.key) ? h('span', { class: 'badge' }, 'fixed in this version') : null),
    input, s.doc ? h('p', { class: 'small muted' }, s.doc) : null);
}

function readForm(form, base) {
  const out = JSON.parse(JSON.stringify(base));
  const errs = [];
  for (const el of form.querySelectorAll('[name]')) {
    if (el.disabled) continue;
    const k = el.name;
    const v = el.value.trim();
    if (v === '') { delete out[k]; continue; }
    const t = el.dataset.type;
    if (t === 'bool') out[k] = v === 'true';
    else if (t === 'number') {
      const n = Number(v.replace(/,/g, ''));
      if (Number.isNaN(n)) errs.push(`${k}: "${v}" is not a number`); else out[k] = n;
    } else if (t === 'list' || t === 'object') {
      try { out[k] = JSON.parse(v); } catch (e) { errs.push(`${k}: not valid JSON (${e.message})`); }
    } else if (k === 'tick_size' && v !== 'auto' && !Number.isNaN(Number(v))) out[k] = Number(v);
    else out[k] = v;
  }
  return [out, errs];
}

function report(res) {
  const box = h('div', { class: 'report', role: 'status' });
  if (res.saved) box.appendChild(note('Saved trader/config.json' + (res.backup ? ' (the previous one is config.json.bak).' : '.'), 'ok'));
  else if (res.ok) box.appendChild(note('Valid. Not saved yet.', 'ok'));
  for (const e of res.errors || []) box.appendChild(note(e, 'error'));
  for (const w of res.warnings || []) box.appendChild(note(w, 'warn'));
  if (res.summary) box.appendChild(h('pre', { class: 'summary' }, res.summary));
  return box;
}

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Settings'));
  const body = h('div');
  root.appendChild(body);
  loading(body);
  const d = await get('/api/config');
  clear(body);
  body.appendChild(note(d.help, 'info'));
  if (!d.exists) body.appendChild(note('There is no trader/config.json in this folder yet. Saving here creates one.', 'warn'));
  const bySchema = Object.fromEntries(d.schema.map((s) => [s.key, s]));
  const form = h('form', { class: 'settings', novalidate: true });
  const used = new Set();
  for (const [title, keys] of GROUPS) {
    const fs = h('fieldset', null, h('legend', null, title));
    for (const k of keys) {
      if (!bySchema[k]) continue;
      used.add(k);
      fs.appendChild(fieldFor(bySchema[k], d.config));
    }
    form.appendChild(fs);
  }
  const rest = d.schema.filter((s) => !used.has(s.key));
  if (rest.length) {
    const fs = h('fieldset', null, h('legend', null, 'Other'));
    rest.forEach((s) => fs.appendChild(fieldFor(s, d.config)));
    form.appendChild(fs);
  }
  const msg = h('div', { class: 'settings-msg', 'aria-live': 'polite' });
  const check = h('button', { type: 'button', class: 'btn' }, 'Validate');
  const save = h('button', { type: 'submit', class: 'btn primary' }, 'Save');
  const raw = h('textarea', { class: 'raw', rows: 18, spellcheck: 'false', 'aria-label': 'trader/config.json as JSON' });
  raw.value = JSON.stringify(d.config, null, 2);
  const rawCheck = h('button', { type: 'button', class: 'btn' }, 'Validate JSON');
  const rawSave = h('button', { type: 'button', class: 'btn primary' }, 'Save JSON');
  const extra = Object.fromEntries(Object.entries(d.config).filter(([k]) => !bySchema[k]));

  const send = async (path, cfg, btn) => {
    btn.disabled = true;
    try {
      const res = await post(path, { config: cfg });
      clear(msg).appendChild(report(res));
      if (res.saved) raw.value = JSON.stringify(cfg, null, 2);
    } catch (e) {
      clear(msg).appendChild(note(e.message, 'error'));
    } finally {
      btn.disabled = false;
      msg.scrollIntoView({ block: 'nearest' });
    }
  };
  const fromForm = () => {
    const [cfg, errs] = readForm(form, Object.assign({}, extra, d.config));
    if (errs.length) { clear(msg).appendChild(report({ errors: errs })); return null; }
    return cfg;
  };
  const fromRaw = () => {
    try { return JSON.parse(raw.value); } catch (e) {
      clear(msg).appendChild(report({ errors: ['not valid JSON: ' + e.message] }));
      return null;
    }
  };
  check.addEventListener('click', () => { const c = fromForm(); if (c) send('/api/config/validate', c, check); });
  form.addEventListener('submit', (e) => { e.preventDefault(); const c = fromForm(); if (c) send('/api/config', c, save); });
  rawCheck.addEventListener('click', () => { const c = fromRaw(); if (c) send('/api/config/validate', c, rawCheck); });
  rawSave.addEventListener('click', () => { const c = fromRaw(); if (c) send('/api/config', c, rawSave); });
  form.appendChild(h('div', { class: 'row sticky-actions' }, check, save));

  body.appendChild(card('Config editor', h('p', { class: 'small muted' }, 'File: ', h('code', null, d.path)),
    (d.errors || []).map((e) => note('Current file: ' + e, 'error')),
    (d.warnings || []).map((w) => note(w, 'warn')), form));
  body.appendChild(msg);
  body.appendChild(card('Raw JSON', raw, h('div', { class: 'row' }, rawCheck, rawSave)));
  if (d.summary) body.appendChild(card('In plain words (trader-budget)', h('pre', { class: 'summary' }, d.summary)));
}
