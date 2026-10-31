// trader/watchlist.json from trader-scanner, with delayed quotes for the top of the list.
import { get } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, pct, crore, tone, whenUtc } from '../fmt.js';
import { why } from '../why.js';

const QUOTE_TOP = 10;

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Scanner and watchlist'));
  const body = h('div');
  root.appendChild(body);
  loading(body);
  const d = await get('/api/scanner');
  clear(body);
  if (!d.exists) {
    body.appendChild(note(d.note, 'info'));
    return;
  }
  body.appendChild(card('Scan', kv([
    ['Mode', d.mode || '–'],
    ['As of', d.as_of || '–'],
    ['Written', whenUtc(d.created_at)],
    ['Universe', `${num(d.universe, 0)} symbols, ${num(d.scanned, 0)} scanned` + (d.failed ? `, ${d.failed} failed` : '')],
    d.filters ? ['Liquidity floor', `price ${inr(d.filters.min_price)}+, median turnover ${crore(d.filters.min_turnover)}+`] : null,
  ]), note(d.note, 'info')));

  const filter = h('input', { type: 'search', id: 'scan-filter', placeholder: 'Filter by symbol or setup',
    'aria-label': 'Filter the watchlist', class: 'filter' });
  const rows = d.hits.map((x, i) => {
    const tr = h('tr', { dataset: { sym: x.symbol, text: (x.symbol + ' ' + x.setup_labels.join(' ')).toLowerCase() } },
      h('td', null, h('a', { href: '#/chart?symbol=' + encodeURIComponent(x.symbol) + '&interval=1d' }, x.symbol)),
      h('td', null, x.setup_labels.join(', ')),
      h('td', { class: 'num' }, inr(x.price)),
      h('td', { class: 'num' }, x.rsi != null ? num(x.rsi, 1) : '–'),
      h('td', { class: 'num' }, x.turnover != null ? why(crore(x.turnover), x.why, 'this turnover') : '–'),
      h('td', { class: 'num live' }, i < QUOTE_TOP ? '…' : ''),
      h('td', { class: 'num' }, x.close_date || ''));
    return tr;
  });
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    for (const tr of rows) tr.hidden = q && !tr.dataset.text.includes(q);
  });
  body.appendChild(card(`Watchlist · ${d.hits.length} hits`, filter,
    table([{ label: 'Symbol' }, { label: 'Setup' }, { label: 'Close', num: true }, { label: 'RSI(14)', num: true },
      { label: 'Turnover', num: true }, { label: 'Now', num: true }, { label: 'Close date', num: true }], rows,
    { caption: 'Scanner hits, most traded first' }),
    h('p', { class: 'small muted' }, `“Now” is a delayed Yahoo quote for the ${QUOTE_TOP} most traded hits.`)));

  const top = d.hits.slice(0, QUOTE_TOP).map((x) => x.symbol);
  if (!top.length) return;
  try {
    const q = await get('/api/quotes?symbols=' + encodeURIComponent(top.join(',')));
    for (const row of q.quotes) {
      const tr = rows.find((r) => r.dataset.sym === row.symbol);
      if (!tr) continue;
      const cell = tr.querySelector('.live');
      clear(cell).appendChild(h('span', { class: tone(row.change) }, inr(row.price), ' ',
        why(pct(row.change_pct), row.why, 'this change')));
    }
    for (const tr of rows.slice(0, QUOTE_TOP)) {
      const cell = tr.querySelector('.live');
      if (cell.textContent === '…') cell.textContent = '–';
    }
  } catch (e) {
    body.appendChild(note('Quotes: ' + e.message, 'warn'));
  }
}
