// trader-risk: your limits, the verdicts it recorded, and a fresh preview (not recorded).
import { get, post } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, when, whenUtc } from '../fmt.js';
import { why } from '../why.js';

const MONEY = new Set(['capital', 'max_loss_per_trade', 'max_daily_loss', 'max_order_value', 'min_order_value']);

function value(k, v) {
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (MONEY.has(k)) return inr(v);
  if (k.endsWith('_pct')) return num(v) + '%';
  return v === null || v === undefined || v === '' ? 'off' : String(v);
}

function results(res, whyInfo) {
  const head = h('p', null,
    h('span', { class: 'badge pass' }, `${res.pass} PASS`), ' ', h('span', { class: 'badge block' }, `${res.block} BLOCK`),
    ` at ${res.now_ist} IST · cash after the passed orders `, why(inr(res.cash_after), whyInfo, 'the verdicts'),
    ` (${res.paper ? 'paper' : 'LIVE'} account)`);
  const items = (res.results || []).map((r) => h('li', { class: 'verdict ' + (r.verdict === 'PASS' ? 'pass' : 'block') },
    h('div', { class: 'verdict-head' }, h('span', { class: 'badge ' + (r.verdict === 'PASS' ? 'pass' : 'block') }, r.verdict),
      ` #${r.index} ${r.side} ${r.qty} ${r.symbol} (${r.product}), ${r.kind}`),
    (r.info || []).length ? h('ul', { class: 'small' }, r.info.map((x) => h('li', null, x))) : null,
    (r.reasons || []).length ? h('ul', { class: 'reasons' }, r.reasons.map((x) => h('li', null, x))) : null,
    (r.warnings || []).length ? h('ul', { class: 'warnings small' }, r.warnings.map((x) => h('li', null, '~ ' + x))) : null));
  return [head, (res.warnings || []).map((w) => note(w, 'warn')), h('ul', { class: 'verdicts' }, items)];
}

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Risk panel'));
  const body = h('div');
  root.appendChild(body);
  const refresh = async (flash) => {
    loading(body);
    const d = await get('/api/risk');
    clear(body);
    if (flash) body.appendChild(flash);
    const btn = h('button', { type: 'button', class: 'btn primary' }, 'Run risk check');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await post('/api/risk/check', {});
        await refresh(res.ok ? note('Recorded the verdicts in trader/state/risk.json.', 'ok')
          : note([res.output, res.error].filter(Boolean).join(' ') || 'The check did not run.', 'error'));
      } catch (e) {
        await refresh(note(e.message, 'error'));
      }
    });
    const last = d.last_check;
    body.appendChild(card('Recorded verdicts (what the paper broker trusts)',
      last ? h('p', { class: 'small muted' }, `Checked ${whenUtc(last.checked_at)} for ${last.date}` +
        (last.prices_as_of ? `, prices as of ${when(last.prices_as_of)}` : '')) : null,
      d.stale ? note(`These verdicts are from ${last.date}, not today. Run the check again.`, 'warn') : null,
      last ? results(last, d.why.verdict) : note('No risk check recorded yet.', 'info'),
      h('div', { class: 'row' }, btn)));
    body.appendChild(card('Preview now (not recorded)', d.preview ? results(d.preview, d.why.verdict)
      : note(d.preview_error || 'No trader/orders.json to check (trader-budget writes it).', d.preview_error ? 'warn' : 'info')));
    body.appendChild(card('Your limits (trader/config.json, defaults filled in)', table(['Rule', 'Value'],
      d.limits.map((l) => h('tr', null, h('td', null, h('code', null, l.key)), h('td', null, value(l.key, l.value)))),
      { dense: true, caption: 'Risk limits' }), h('a', { href: '#/settings', class: 'more' }, 'Edit in Settings →')));
    body.appendChild(h('p', { class: 'muted small' }, d.disclaimer));
  };
  await refresh();
}
