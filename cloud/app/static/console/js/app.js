// Petmate — Console di test: hash router, login, "I miei robot".
import { api, apiErrorText, session } from './api.js';
import { Signal } from './signal.js';
import { $, fmtTime, h, toast } from './ui.js';
import * as prov from './prov-crypto.js';

const signal = new Signal();

// Debug hooks for automated E2E tests (Playwright). Read-only by convention.
const dbg = (window.__petmate = {
  version: 1,
  view: null,
  errors: [],
  devices: [],
  signal,
  prov,
  wizard: null, // set by wizard.js: {step, device, status, ...}
  live: null,   // set by live.js: {pc, dc, stats, lastState, video(), ...}
  session: { get loggedIn() { return !!session.token; }, get email() { return session.email; } },
});
window.addEventListener('error', (e) => dbg.errors.push(String(e.message || e)));
window.addEventListener('unhandledrejection', (e) => dbg.errors.push(String(e.reason?.message || e.reason)));

let cleanup = null;
const view = () => $('#view');

function go(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}

async function route() {
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  const [, name = '', arg] = location.hash.split('/');
  const loggedIn = !!session.token;
  $('#nav').hidden = !loggedIn;
  $('#user-email').textContent = session.email;
  if (!loggedIn) {
    signal.stop();
    if (name !== 'login') { location.hash = '#/login'; return; }
  } else {
    signal.start(session.token);
    if (name === 'login' || name === '') { location.hash = '#/robots'; return; }
  }
  dbg.view = name;
  view().replaceChildren();
  document.body.dataset.view = name;
  if (name === 'login') cleanup = renderLogin(view());
  else if (name === 'robots') cleanup = renderRobots(view());
  else if (name === 'add') cleanup = (await import('./wizard.js')).renderWizard(view(), { signal, go });
  else if (name === 'live' && arg) cleanup = (await import('./live.js')).renderLive(view(), decodeURIComponent(arg), { signal, go });
  else location.hash = '#/robots';
}

// ------------------------------------------------------------------ login / registrazione
function renderLogin(root) {
  const email = h('input', { id: 'login-email', type: 'email', autocomplete: 'username', required: true });
  const pw = h('input', { id: 'login-password', type: 'password', autocomplete: 'current-password', required: true, minlength: '8' });
  const err = h('div', { id: 'login-error', class: 'error', role: 'alert' });
  const submit = async (mode) => {
    err.textContent = '';
    if (!email.value || !pw.value) { err.textContent = 'Inserisci email e password.'; return; }
    try {
      const r = await api('POST', `/auth/${mode}`, { email: email.value.trim(), password: pw.value });
      session.set(r.access_token, email.value.trim().toLowerCase());
      toast(mode === 'register' ? 'Account creato.' : 'Accesso effettuato.');
      go('#/robots');
    } catch (e) {
      err.textContent = apiErrorText(e);
    }
  };
  const form = h('form', { id: 'login-form', onsubmit: (e) => { e.preventDefault(); submit('login'); } },
    h('label', { for: 'login-email' }, 'Email'), email,
    h('label', { for: 'login-password' }, 'Password'), pw,
    h('p', { class: 'muted small' }, 'Per la registrazione: almeno 8 caratteri.'),
    h('div', { class: 'row' },
      h('button', { id: 'login-submit', class: 'btn', type: 'submit' }, 'Accedi'),
      h('button', { id: 'register-submit', class: 'btn accent', type: 'button', onclick: () => submit('register') }, 'Registrati')),
    err);
  root.append(h('section', { class: 'card narrow' }, h('h1', {}, 'Accedi a Petmate'), form));
  email.focus();
}

// ------------------------------------------------------------------ I miei robot
function renderRobots(root) {
  const list = h('ul', { id: 'robots-list', class: 'robot-list' });
  const empty = h('p', { id: 'robots-empty', class: 'muted', hidden: true }, 'Nessun robot associato. Usa «Aggiungi robot».');
  const err = h('div', { id: 'robots-error', class: 'error' });
  root.append(h('section', { class: 'card' },
    h('div', { class: 'row spread' }, h('h1', {}, 'I miei robot'),
      h('button', { id: 'add-robot', class: 'btn accent', onclick: () => go('#/add') }, '+ Aggiungi robot')),
    list, empty, err));

  const badge = (row, online) => {
    const b = row.querySelector('.robot-online');
    b.textContent = online ? 'online' : 'offline';
    b.classList.toggle('on', online);
    row.dataset.online = String(online);
  };

  const rowFor = (d) => {
    const online = signal.online.has(d.device_id) ? signal.online.get(d.device_id) : d.online;
    const row = h('li', { class: 'robot-row', dataset: { deviceId: d.device_id } },
      h('div', { class: 'robot-main' },
        h('div', { class: 'row' }, h('span', { class: 'robot-name' }, d.name), h('span', { class: 'robot-online' })),
        h('div', { class: 'muted small' }, h('span', { class: 'mono' }, d.device_id),
          ` · fw ${d.fw || '—'} · ultimo contatto ${fmtTime(d.last_seen)}`)),
      h('div', { class: 'robot-actions' },
        h('button', { class: 'btn small robot-live', onclick: () => go(`#/live/${encodeURIComponent(d.device_id)}`) }, 'Live'),
        h('button', { class: 'btn ghost small robot-rename', onclick: () => rename(d) }, 'Rinomina'),
        h('button', { class: 'btn danger small robot-remove', onclick: () => remove(d) }, 'Rimuovi')));
    badge(row, online);
    return row;
  };

  async function load() {
    err.textContent = '';
    try {
      const devs = await api('GET', '/devices');
      dbg.devices = devs;
      list.replaceChildren(...devs.map(rowFor));
      empty.hidden = devs.length > 0;
      list.dataset.loaded = 'true';
    } catch (e) {
      err.textContent = apiErrorText(e);
    }
  }

  async function rename(d) {
    const name = prompt('Nuovo nome del robot', d.name);
    if (!name || !name.trim() || name.trim() === d.name) return;
    try { await api('PATCH', `/devices/${encodeURIComponent(d.device_id)}`, { name: name.trim() }); toast('Nome aggiornato.'); load(); }
    catch (e) { toast(apiErrorText(e), 'err'); }
  }

  async function remove(d) {
    if (!confirm(`Rimuovere «${d.name}» (${d.device_id}) dal tuo account?\nPer riassociarlo servirà di nuovo il wizard BLE.`)) return;
    try { await api('DELETE', `/devices/${encodeURIComponent(d.device_id)}`); toast('Robot rimosso.'); load(); }
    catch (e) { toast(apiErrorText(e), 'err'); }
  }

  const onPresence = (ev) => {
    const { device_id, online } = ev.detail;
    const row = list.querySelector(`.robot-row[data-device-id="${CSS.escape(device_id)}"]`);
    if (row) badge(row, online); else if (online) load(); // newly claimed device
  };
  signal.addEventListener('presence', onPresence);
  load();
  return () => signal.removeEventListener('presence', onPresence);
}

// ------------------------------------------------------------------ boot
$('#logout').addEventListener('click', () => { session.clear(); signal.stop(); go('#/login'); });
window.addEventListener('petmate:logout', () => { toast('Sessione scaduta: accedi di nuovo.', 'err'); go('#/login'); });
signal.addEventListener('fatal', () => { session.clear(); go('#/login'); });
signal.addEventListener('state', (ev) => {
  const dot = $('#signal-state');
  dot.className = `dot ${ev.detail === 'open' ? 'on' : ev.detail === 'connecting' ? 'wait' : ''}`;
  dot.title = `Signaling: ${ev.detail}`;
  dot.dataset.state = ev.detail;
});
window.addEventListener('hashchange', route);
route();
