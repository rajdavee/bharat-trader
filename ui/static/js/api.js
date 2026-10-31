// Talk to the local server only. Writes carry the session's CSRF token from the page's meta tag.

const meta = document.querySelector('meta[name="csrf-token"]');
const TOKEN = meta ? meta.getAttribute('content') : '';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function parse(r) {
  let body = null;
  try {
    body = await r.json();
  } catch (e) {
    body = { error: 'the server sent something that is not JSON' };
  }
  if (!r.ok) throw new ApiError((body && body.error) || r.statusText, r.status);
  return body;
}

export async function get(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
  return parse(r);
}

export async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRF-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  return parse(r);
}
