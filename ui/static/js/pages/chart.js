// Candles and volume (TradingView Lightweight Charts, vendored), EMA and VWAP overlays, the opening-range
// band, and the plan's entry, stop and target. Every value comes from /api/chart.
import { get } from '../api.js';
import { h, clear, card, kv, note, loading } from '../dom.js';
import { num, int, when } from '../fmt.js';
import { why } from '../why.js';
import { token } from '../theme.js';

const IST_SHIFT = 19800; // Lightweight Charts draws UTC: shift by +05:30 so the axis reads IST
const STATE = { chart: null, cleanup: [] };
const SHOW_KEY = 'bt-chart-show';

function pref() {
  try {
    return JSON.parse(localStorage.getItem(SHOW_KEY) || 'null') || {};
  } catch (e) {
    return {};
  }
}

function savePref(p) {
  try { localStorage.setItem(SHOW_KEY, JSON.stringify(p)); } catch (e) { /* private window */ }
}

function t(ts, interval) {
  if (interval === '1d') {
    const d = new Date((ts + IST_SHIFT) * 1000);
    return d.toISOString().slice(0, 10);
  }
  return ts + IST_SHIFT;
}

function colors() {
  return {
    bg: token('--panel'), text: token('--muted'), grid: token('--grid'), border: token('--border'),
    up: token('--up'), down: token('--down'), ema1: token('--ema1'), ema2: token('--ema2'), ema3: token('--accent'),
    vwap: token('--vwap'), entry: token('--accent'), stop: token('--down'), target: token('--up'),
    band: token('--band'),
  };
}

function chartOptions(c, interval) {
  const L = window.LightweightCharts;
  return {
    autoSize: true,
    layout: { background: { type: L.ColorType.Solid, color: c.bg }, textColor: c.text, attributionLogo: true,
      fontFamily: getComputedStyle(document.body).fontFamily },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.border },
    timeScale: { borderColor: c.border, timeVisible: interval !== '1d', secondsVisible: false },
    crosshair: { mode: L.CrosshairMode.Normal },
    localization: { priceFormatter: (p) => num(p) },
  };
}

function draw(wrap, legend, data, show) {
  const L = window.LightweightCharts;
  const c = colors();
  const chart = L.createChart(wrap, chartOptions(c, data.interval));
  STATE.chart = chart;
  const candles = chart.addSeries(L.CandlestickSeries, {
    upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down,
    priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    // keep the plan's levels and the opening range on screen, not just the candles
    autoscaleInfoProvider: (original) => {
      const res = original();
      if (!res || !res.priceRange) return res;
      const extra = [];
      if (show.plan && data.plan) extra.push(data.plan.entry, data.plan.stop, data.plan.target);
      if (show.or && data.opening_range) extra.push(data.opening_range.high, data.opening_range.low);
      for (const v of extra.filter((x) => typeof x === 'number')) {
        res.priceRange.minValue = Math.min(res.priceRange.minValue, v);
        res.priceRange.maxValue = Math.max(res.priceRange.maxValue, v);
      }
      return res;
    },
  });
  candles.setData(data.bars.map((b) => ({ time: t(b.time, data.interval), open: b.open, high: b.high, low: b.low,
    close: b.close })));
  const vol = chart.addSeries(L.HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' },
    lastValueVisible: false, priceLineVisible: false });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(data.bars.map((b) => ({ time: t(b.time, data.interval), value: b.volume,
    color: (b.close >= b.open ? c.up : c.down) + '66' })));
  const lines = {};
  const emaColors = [c.ema1, c.ema2, c.ema3];
  Object.keys(data.ema).forEach((n, i) => {
    if (!show['ema' + n]) return;
    const s = chart.addSeries(L.LineSeries, { color: emaColors[i % 3], lineWidth: 1.5, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false, title: 'EMA ' + n });
    s.setData(data.ema[n].map((p) => ({ time: t(p.time, data.interval), value: p.value })));
    lines['EMA ' + n] = s;
  });
  if (show.vwap && data.vwap.length) {
    const s = chart.addSeries(L.LineSeries, { color: c.vwap, lineWidth: 2, lineStyle: L.LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: 'VWAP' });
    s.setData(data.vwap.map((p) => ({ time: t(p.time, data.interval), value: p.value })));
    lines.VWAP = s;
  }
  if (show.plan && data.plan) {
    const p = data.plan;
    for (const [key, color, label] of [['entry', c.entry, 'entry'], ['stop', c.stop, 'stop'], ['target', c.target, 'target']]) {
      if (p[key] != null) {
        candles.createPriceLine({ price: p[key], color, lineWidth: 1, lineStyle: L.LineStyle.Dashed,
          axisLabelVisible: true, title: label });
      }
    }
  }
  // the opening-range band: a shaded strip between the range's high and low, kept in place on scroll and zoom
  let band = null;
  if (show.or && data.opening_range) {
    const orr = data.opening_range;
    for (const [price, label] of [[orr.high, 'OR high'], [orr.low, 'OR low']]) {
      candles.createPriceLine({ price, color: c.band, lineWidth: 1, lineStyle: L.LineStyle.Dotted,
        axisLabelVisible: false, title: label });
    }
    band = h('div', { class: 'or-band', 'aria-hidden': 'true' });
    wrap.appendChild(band);
    const place = () => {
      if (STATE.chart !== chart) return; // redrawn or left the page since this frame was asked for
      const y1 = candles.priceToCoordinate(orr.high);
      const y2 = candles.priceToCoordinate(orr.low);
      if (y1 == null || y2 == null) { band.style.display = 'none'; return; }
      band.style.display = 'block';
      band.style.top = Math.min(y1, y2) + 'px';
      band.style.height = Math.max(1, Math.abs(y2 - y1)) + 'px';
      band.style.right = chart.priceScale('right').width() + 'px';
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => requestAnimationFrame(place));
    chart.timeScale().subscribeSizeChange(() => requestAnimationFrame(place));
    setTimeout(place, 50);
  }
  chart.timeScale().fitContent();

  const last = data.bars[data.bars.length - 1];
  const showLegend = (bar) => {
    clear(legend);
    if (!bar) return;
    legend.appendChild(h('span', null, `O ${num(bar.open)}  H ${num(bar.high)}  L ${num(bar.low)}  C ${num(bar.close)}`));
    if (bar.volume != null) legend.appendChild(h('span', { class: 'muted' }, `  Vol ${int(bar.volume)}`));
  };
  showLegend(last);
  const byTime = new Map(data.bars.map((b) => [String(t(b.time, data.interval)), b]));
  chart.subscribeCrosshairMove((param) => {
    if (!param || param.time === undefined) { showLegend(last); return; }
    showLegend(byTime.get(String(param.time)) || last);
  });
}

function sidePanel(data) {
  const last = data.bars[data.bars.length - 1];
  const rows = [
    ['Last close', last ? num(last.close) : '–'],
    ['Data time', when(data.data_time)],
  ];
  if (data.vwap.length) rows.push(['VWAP', why(num(data.vwap[data.vwap.length - 1].value), data.why.vwap, 'VWAP')]);
  for (const n of Object.keys(data.ema)) {
    const s = data.ema[n];
    if (s.length) rows.push(['EMA ' + n, why(num(s[s.length - 1].value), data.why['ema' + n], 'EMA ' + n)]);
  }
  if (data.opening_range) {
    const o = data.opening_range;
    rows.push(['Opening range', why(`${num(o.low)} – ${num(o.high)}`, data.why.opening_range, 'the opening range')]);
  }
  const parts = [kv(rows)];
  if (data.entry) {
    parts.push(h('h3', { class: 'sub' }, 'trader-entry (opening-range breakout)'));
    parts.push(kv([
      ['Status', h('span', { class: 'badge ' + (data.entry.status === 'triggered' ? 'pass' : '') }, data.entry.status)],
      ['Why', data.entry.reason],
      data.entry.trigger ? ['Trigger', `${num(data.entry.trigger.price)} at ${data.entry.trigger.time} IST`] : null,
      ['Entry window', `${data.entry.no_entries_before || ''} to ${data.entry.no_entries_after || ''} IST`],
    ]));
  }
  if (data.plan) {
    const p = data.plan;
    parts.push(h('h3', { class: 'sub' }, 'Plan (trader/plan.json)'));
    parts.push(kv([
      ['Entry', why(num(p.entry), data.why.plan, 'the plan levels')],
      ['Stop', `${num(p.stop)} · ${p.stop_label || ''}`],
      ['Target', `${num(p.target)} · ${p.target_label || ''}`],
      ['Plan date', p.date || '–'],
    ]));
  }
  parts.push(h('p', { class: 'small muted' }, data.source + '. EMA and VWAP are chart overlays; no tool decides on them.'));
  return card('Levels', ...parts);
}

export async function render(root, ctx) {
  const params = ctx.params || {};
  const sym = (params.symbol || '').toUpperCase();
  const interval = ['1m', '5m', '1d'].includes(params.interval) ? params.interval : '5m';
  const show = Object.assign({ ema9: true, ema21: true, vwap: true, or: true, plan: true }, pref());

  const form = h('form', { class: 'toolbar', role: 'search', autocomplete: 'off' },
    h('label', { for: 'chart-sym', class: 'sr-only' }, 'Symbol'),
    h('input', { id: 'chart-sym', name: 'symbol', value: sym, placeholder: 'RELIANCE', spellcheck: 'false',
      autocapitalize: 'characters', class: 'sym-input' }),
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Interval' }, ['1m', '5m', '1d'].map((iv) => h('a', {
      href: `#/chart?symbol=${encodeURIComponent(sym)}&interval=${iv}`, class: 'seg-btn',
      'aria-current': iv === interval ? 'true' : null,
    }, iv))),
    h('div', { class: 'toggles', role: 'group', 'aria-label': 'Overlays' },
      [['ema9', 'EMA 9'], ['ema21', 'EMA 21'], ['vwap', 'VWAP'], ['or', 'Opening range'], ['plan', 'Plan']].map(([k, label]) =>
        h('label', { class: 'chk' }, h('input', { type: 'checkbox', name: k, checked: show[k] ? true : null }), label))),
    h('button', { type: 'submit', class: 'btn' }, 'Show'));
  root.appendChild(h('h1', { class: 'page-title' }, sym ? `${sym} · ${interval}` : 'Chart'));
  root.appendChild(h('p', { class: 'page-lede explain' }, 'Discover: price, volume and your plan\'s levels on one chart. Click a dotted number for how it was worked out.'));
  root.appendChild(form);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const s = form.symbol.value.trim().toUpperCase();
    if (s) location.hash = `#/chart?symbol=${encodeURIComponent(s)}&interval=${interval}`;
  });
  const body = h('div', { class: 'chart-layout' });
  root.appendChild(body);

  if (!sym) {
    let plan = null;
    try { plan = (await get('/api/plan')).plan; } catch (e) { /* no plan */ }
    if (plan && plan.symbol) {
      location.replace(`#/chart?symbol=${encodeURIComponent(plan.symbol)}&interval=${interval}`);
      return;
    }
    body.appendChild(note('Type an NSE symbol (RELIANCE, M&M) or an index (^NSEI) and press Show.', 'info'));
    return;
  }
  loading(body, 'Loading ' + sym);
  const data = await get(`/api/chart?symbol=${encodeURIComponent(sym)}&interval=${interval}`);
  clear(body);
  if (!data.bars.length) {
    body.appendChild(note('No bars for ' + sym + ' ' + interval + '.', 'warn'));
    return;
  }
  const wrap = h('div', { class: 'chart-box', role: 'img',
    'aria-label': `${sym} ${interval} candlestick chart with volume${data.plan ? ', plan levels' : ''}` });
  const legend = h('div', { class: 'legend', 'aria-live': 'off' });
  const left = h('div', { class: 'chart-main' }, legend, wrap);
  body.appendChild(left);
  body.appendChild(sidePanel(data));
  if (!window.LightweightCharts) {
    wrap.appendChild(note('The chart library did not load.', 'error'));
    return;
  }
  const redraw = () => {
    if (STATE.chart) { STATE.chart.remove(); STATE.chart = null; }
    for (const el of wrap.querySelectorAll('.or-band')) el.remove();
    draw(wrap, legend, data, show);
  };
  redraw();
  for (const box of form.querySelectorAll('.toggles input')) {
    box.addEventListener('change', () => {
      show[box.name] = box.checked;
      savePref(show);
      redraw();
    });
  }
  const onTheme = () => redraw();
  ctx.bus.addEventListener('theme', onTheme);
  STATE.cleanup.push(() => ctx.bus.removeEventListener('theme', onTheme));
}

export function destroy() {
  STATE.cleanup.forEach((f) => f());
  STATE.cleanup = [];
  if (STATE.chart) {
    STATE.chart.remove();
    STATE.chart = null;
  }
}
