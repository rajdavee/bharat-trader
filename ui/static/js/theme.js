// Which theme is showing, and the CSS colour tokens for code that draws (the chart).

export function isDark() {
  const t = document.documentElement.dataset.theme;
  if (t) return t === 'dark';
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
