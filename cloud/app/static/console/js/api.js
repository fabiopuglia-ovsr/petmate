// REST client for /v1 (same origin) + session token in sessionStorage.
const KEY = 'petmate.console.session';

function load() {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; }
}

let current = load();

export const session = {
  get token() { return current?.token || null; },
  get email() { return current?.email || ''; },
  set(token, email) {
    current = { token, email };
    try { sessionStorage.setItem(KEY, JSON.stringify(current)); } catch { /* private mode */ }
  },
  clear() {
    current = null;
    try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
  },
};

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

export async function api(method, path, body) {
  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(`/v1${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new ApiError(0, 'cloud non raggiungibile');
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && session.token && !path.startsWith('/auth/')) {
      session.clear();
      window.dispatchEvent(new CustomEvent('petmate:logout', { detail: 'expired' }));
    }
    throw new ApiError(res.status, data?.detail ?? res.statusText);
  }
  return data;
}

/** Italian text for common API errors. */
export function apiErrorText(e) {
  if (!(e instanceof ApiError)) return String(e?.message || e);
  const d = typeof e.detail === 'string' ? e.detail : '';
  if (e.status === 0) return 'Cloud non raggiungibile.';
  if (d === 'invalid credentials') return 'Email o password errati.';
  if (d === 'email already registered') return 'Email già registrata: usa «Accedi».';
  if (e.status === 422) return 'Dati non validi (email valida, password di almeno 8 caratteri).';
  if (e.status === 429) return 'Troppi tentativi: attendi un minuto.';
  if (e.status === 401) return 'Sessione scaduta: accedi di nuovo.';
  if (e.status === 404) return 'Non trovato.';
  if (e.status === 403) return 'Operazione non consentita.';
  return `Errore ${e.status}: ${d || 'richiesta fallita'}`;
}
