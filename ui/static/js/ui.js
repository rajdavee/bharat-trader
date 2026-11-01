// Design system v2 components (docs/design.md): metric tiles, meters, empty / loading / error states,
// toasts, the stepper, the session rail and the "coming in v0.3" placeholder.
import { h, clear } from './dom.js';
import { help } from './why.js';

// A metric tile: label (+ help), value (already formatted, with its unit), a sub line, a plain-words line.
export function metric({ label, value, sub, explain, term, tone }) {
  return h('section', { class: 'metric' + (tone ? ' tone-' + tone : ''), 'aria-label': label },
    h('div', { class: 'metric-label' }, label, term ? help(term) : null),
    h('div', { class: 'metric-value' }, value),
    sub ? h('div', { class: 'metric-sub' }, sub) : null,
    explain ? h('p', { class: 'explain' }, explain) : null);
}

// used against a limit: a bar plus the fraction in words (never the bar alone)
export function meter(used, limit, words) {
  const share = limit > 0 ? Math.max(0, Math.min(1, used / limit)) : 0;
  const cls = share >= 0.8 ? 'hot' : share >= 0.5 ? 'warm' : '';
  const bar = h('div', { class: 'meter-bar ' + cls });
  bar.style.width = (share * 100).toFixed(1) + '%';
  return h('div', null,
    h('div', { class: 'meter', role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': String(limit || 0),
      'aria-valuenow': String(used || 0), 'aria-label': words }, bar),
    h('div', { class: 'meter-text' }, words));
}

export function empty(title, text, action) {
  return h('div', { class: 'empty' }, h('h3', null, title), text ? h('p', null, text) : null, action || null);
}

export function skeleton(lines = 3) {
  const el = h('div', { class: 'skel', 'aria-hidden': 'true' });
  for (let i = 0; i < lines; i += 1) el.appendChild(h('span'));
  return h('div', null, h('span', { class: 'sr-only', role: 'status' }, 'Loading…'), el);
}

export function failure(message, retry) {
  return h('div', { class: 'note error', role: 'alert' }, message,
    retry ? h('span', null, ' ', h('button', { type: 'button', class: 'link', onclick: retry }, 'Try again')) : null);
}

export function toast(message, kind = 'ok') {
  const box = document.getElementById('toasts');
  if (!box) return;
  const t = h('div', { class: 'toast' + (kind === 'error' ? ' error' : '') }, message);
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === 'error' ? 8000 : 4000);
}

export function stepper(steps, current, done) {
  return h('ol', { class: 'stepper', 'aria-label': 'Setup steps' }, steps.map((s, i) => h('li', {
    class: i === current ? 'current' : done.has(i) ? 'done' : '',
    'aria-current': i === current ? 'step' : null,
  }, s)));
}

export function pageHead(title, lede, right) {
  const out = [h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, title), right || null)];
  if (lede) out.push(h('p', { class: 'page-lede' }, lede));
  return out;
}

// ---------------------------------------------------------------- session rail

function minutes(hhmm) {
  const m = String(hhmm || '').match(/(\d{1,2}):(\d{2})/);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function hhmm(min) {
  return String(Math.floor(min / 60)).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0');
}

function span(min) {
  if (min < 60) return min + ' min';
  return Math.floor(min / 60) + ' h ' + (min % 60 ? (min % 60) + ' min' : '').trim();
}

// The NSE day from pre-open to the close, with the user's own windows and a "now" needle.
export function sessionRail({ now, open, phase, noBefore, noAfter, flatBy, nextOpen }) {
  const START = 9 * 60;
  const END = 15 * 60 + 30;
  const nb = minutes(noBefore) ?? 9 * 60 + 30;
  const na = minutes(noAfter) ?? 14 * 60 + 45;
  const fb = minutes(flatBy) ?? 15 * 60 + 10;
  const t = minutes(now);
  const segs = [
    ['pre', START, 9 * 60 + 15, 'Pre-open'],
    ['wait', 9 * 60 + 15, nb, 'Open, no entries yet'],
    ['entry', nb, na, 'Entry window'],
    ['exits', na, fb, 'Exits only'],
    ['flat', fb, END, 'Be flat (intraday)'],
  ].filter((s) => s[2] > s[1]);
  const pct = (m) => ((Math.max(START, Math.min(END, m)) - START) / (END - START)) * 100;
  const track = h('div', { class: 'rail-track' });
  for (const [cls, a, b, label] of segs) {
    const s = h('div', { class: 'rail-seg ' + cls, title: `${label}: ${hhmm(a)} to ${hhmm(b)} IST` });
    s.style.width = (pct(b) - pct(a)) + '%';
    track.appendChild(s);
  }
  const rail = h('div', { class: 'rail', role: 'img', 'aria-label': railWords(t, open, nb, na, fb) }, track);
  for (const [m, cls] of [[START, ''], [nb, ''], [na, ''], [fb, ''], [END, 'end']]) {
    const tick = h('span', { class: 'rail-tick ' + cls, 'aria-hidden': 'true' }, hhmm(m));
    tick.style.left = pct(m) + '%';
    rail.appendChild(tick);
  }
  if (t !== null && t >= START && t <= END) {
    const needle = h('div', { class: 'rail-needle', 'aria-hidden': 'true' });
    needle.style.left = pct(t) + '%';
    rail.appendChild(needle);
  }
  let headline;
  let next;
  if (t === null) {
    headline = 'Clock unavailable';
  } else if (!open && phase !== 'pre-open') {
    headline = 'Market closed';
    next = nextOpen ? 'Next open ' + nextOpen + ' IST' : null;
  } else if (t < nb) {
    headline = 'No entries yet';
    next = 'Entry window opens in ' + span(nb - t) + ' (' + hhmm(nb) + ' IST)';
  } else if (t < na) {
    headline = 'Entry window open';
    next = span(na - t) + ' left for new entries; flat by ' + hhmm(fb) + ' IST';
  } else if (t < fb) {
    headline = 'Exits only';
    next = 'No new entries. Intraday positions must be closed in ' + span(fb - t) + ' (' + hhmm(fb) + ' IST)';
  } else {
    headline = 'Be flat';
    next = 'Past your flat-by time: intraday positions should be closed';
  }
  return h('div', null,
    h('div', { class: 'rail-head' },
      h('div', { class: 'rail-now' }, t !== null ? h('span', { class: 'rail-clock' }, hhmm(t) + ' IST') : null,
        h('span', null, headline)),
      next ? h('div', { class: 'rail-next' }, next) : null),
    rail,
    h('ul', { class: 'rail-legend' }, segs.map(([cls, , , label]) => h('li', null, h('i', { class: cls }), label))));
}

function railWords(t, open, nb, na, fb) {
  return `Session from 09:00 to 15:30 IST. Entries ${hhmm(nb)} to ${hhmm(na)}, flat by ${hhmm(fb)}.` +
    (t !== null ? ` Now ${hhmm(t)}, market ${open ? 'open' : 'closed'}.` : '');
}

// ---------------------------------------------------------------- placeholder pages

export function soon(root, { title, issue, lede, will, honest, now }) {
  clear(root);
  root.append(...pageHead(title, lede, h('span', { class: 'badge soon' }, 'Coming in v0.3')));
  root.appendChild(h('section', { class: 'soon-hero', 'aria-label': 'What this page will do' },
    h('span', { class: 'badge soon' }, 'Tracked in issue #' + issue),
    h('h2', null, 'What this page will show'),
    h('ul', null, will.map((x) => h('li', null, x))),
    honest ? h('p', { class: 'explain' }, honest) : null,
    now ? h('div', { class: 'row' }, now.map(([label, href]) => h('a', { class: 'btn', href }, label))) : null));
}
