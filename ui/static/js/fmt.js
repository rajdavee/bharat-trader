// Display formatting only: every number arrives from the server, worked out by the skills.
// Indian digit grouping, the same rule as the skills' inr(): 1234567.891 -> 12,34,567.89.

function group(whole) {
  if (whole.length <= 3) return whole;
  let head = whole.slice(0, -3);
  const tail = whole.slice(-3);
  const parts = [];
  while (head.length > 2) {
    parts.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  if (head) parts.unshift(head);
  return parts.concat([tail]).join(',');
}

export function num(x, decimals = 2) {
  if (x === null || x === undefined || x === '' || Number.isNaN(Number(x))) return '–';
  const v = Number(x);
  const [whole, frac] = Math.abs(v).toFixed(decimals).split('.');
  return (v < 0 && Number(Math.abs(v).toFixed(decimals)) !== 0 ? '-' : '') + group(whole) + (frac ? '.' + frac : '');
}

export function inr(x) {
  const s = num(x, 2);
  if (s === '–') return s;
  return s.startsWith('-') ? '-₹' + s.slice(1) : '₹' + s;
}

// rupees for a stock, plain points for an index (^NSEI), like trader-alerts' money()
export function money(sym, x) {
  return String(sym || '').startsWith('^') ? num(x) : inr(x);
}

export function int(x) {
  return num(x, 0);
}

export function crore(x) {
  if (x === null || x === undefined) return '–';
  return '₹' + num(Number(x) / 1e7, 2) + ' cr';
}

export function pct(x, signed = true) {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return '–';
  const v = Number(x);
  return (signed && v > 0 ? '+' : '') + v.toFixed(2) + '%';
}

export function signed(x, f = inr) {
  if (x === null || x === undefined) return '–';
  return (Number(x) > 0 ? '+' : '') + f(x);
}

export function tone(x) {
  if (x === null || x === undefined) return '';
  return Number(x) > 0 ? 'up' : Number(x) < 0 ? 'down' : '';
}

// "2026-10-29T15:29:03+05:30" -> "29 Oct 15:29 IST"
export function when(iso) {
  if (!iso) return '–';
  const m = String(iso).match(/^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/);
  if (!m) return String(iso);
  const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(m[2]) - 1];
  return `${Number(m[3])} ${mon} ${m[4]}:${m[5]} IST`;
}

// a UTC stamp ("2026-10-29T09:00:00Z") shown in IST
export function whenUtc(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return when(new Date(d.getTime() + 19800 * 1000).toISOString().slice(0, 16));
}
