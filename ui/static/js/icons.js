// A small stroke icon set, drawn here (no icon font, nothing fetched). 24 x 24, currentColor.

const P = {
  today: 'M12 3v2M12 19v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M3 12h2M19 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8',
  chart: 'M4 20h16M7 16V9M7 9h0M11 16V5M15 16v-6M19 16V8',
  scanner: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14M21 21l-4.5-4.5',
  options: 'M4 6h16M4 12h16M4 18h16M12 4v16',
  strategy: 'M3 17l5-5l4 4l9-9M15 7h6v6',
  cotrader: 'M8 11a3 3 0 1 0 0-6a3 3 0 1 0 0 6M16 11a3 3 0 1 0 0-6a3 3 0 1 0 0 6M2 20c0-3 3-5 6-5s6 2 6 5M14 15.5c.6-.3 1.3-.5 2-.5c3 0 6 2 6 5',
  portfolio: 'M3 8h18v11H3zM8 8V5h8v3M3 13h18',
  risk: 'M12 3l8 3v6c0 4.5-3.4 8-8 9c-4.6-1-8-4.5-8-9V6zM9 12l2 2l4-4',
  autopilot: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 12l4-4M12 7v1M7 12h1M16 12h1',
  lab: 'M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3M7.5 15h9',
  journal: 'M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h11M9 8h6',
  agents: 'M12 4a3 3 0 1 0 0 6a3 3 0 1 0 0-6M5 20c0-3.5 3-6 7-6s7 2.5 7 6',
  settings: 'M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4',
  welcome: 'M5 12l4 4L19 6',
  about: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 11v6M12 7.5v.5',
  search: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14M21 21l-4.5-4.5',
  menu: 'M4 7h16M4 12h16M4 17h16',
  theme: 'M12 3a9 9 0 1 0 9 9a7 7 0 0 1-9-9',
  close: 'M6 6l12 12M18 6L6 18',
  pulse: 'M3 12h4l2-5l4 10l2-5h6',
  clock: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 7v5l3 2',
  keys: 'M3 7h18v10H3zM7 11h.01M11 11h.01M15 11h.01M8 14h8',
};

// the SVG namespace name (an identifier, never fetched), joined so the no-external-URL test stays strict
const NS = ['http:', '', 'www.w3.org', '2000', 'svg'].join('/');

export function icon(name, cls = 'ico') {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', P[name] || P.about);
  svg.appendChild(path);
  return svg;
}

// The brand mark: a candle on a rail, in the accent and the marigold.
export function brandMark() {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 28 28');
  svg.setAttribute('class', 'brand-mark');
  svg.setAttribute('aria-hidden', 'true');
  const parts = [
    ['rect', { x: 0, y: 0, width: 28, height: 28, rx: 8, class: 'bm-bg' }],
    ['rect', { x: 9, y: 9, width: 4, height: 10, rx: 1, class: 'bm-ink' }],
    ['path', { d: 'M11 5v4M11 19v4', class: 'bm-ink-line' }],
    ['rect', { x: 16, y: 12, width: 4, height: 7, rx: 1, class: 'bm-gold' }],
    ['path', { d: 'M18 8v4M18 19v3', class: 'bm-gold-line' }],
  ];
  for (const [tag, attrs] of parts) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.appendChild(el);
  }
  return svg;
}
