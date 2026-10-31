// Tiny DOM helpers. Text always goes in as text nodes: no innerHTML with data.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function card(title, ...body) {
  const id = 'c-' + Math.random().toString(36).slice(2, 8);
  return h('section', { class: 'card', 'aria-labelledby': id },
    h('h2', { class: 'card-title', id }, title), ...body);
}

export function kv(rows) {
  return h('dl', { class: 'kv' }, rows.filter(Boolean).map(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
}

export function table(headers, rows, opts = {}) {
  return h('div', { class: 'table-wrap' },
    h('table', { class: 'tbl' + (opts.dense ? ' dense' : '') },
      opts.caption ? h('caption', { class: 'sr-only' }, opts.caption) : null,
      h('thead', null, h('tr', null, headers.map((x) => h('th', { scope: 'col', class: x.num ? 'num' : null },
        x.label || x)))),
      h('tbody', null, rows)));
}

export function note(text, kind = 'info') {
  return h('p', { class: 'note ' + kind, role: kind === 'error' ? 'alert' : null }, text);
}

export function loading(el, what = 'Loading') {
  clear(el).appendChild(h('p', { class: 'muted loading' }, what + '…'));
}
