// "Aggiungi robot": BLE Wi-Fi provisioning wizard (INTERFACES.md §7) via Web Bluetooth.
import { api, apiErrorText } from './api.js';
import { CHAR, SERVICE_UUID, chunkMessage, encryptPayload } from './prov-crypto.js';
import { $, h } from './ui.js';

const STATES = ['receiving', 'connecting', 'wifi_ok', 'registering', 'done'];
const STATE_TEXT = {
  idle: 'In attesa dei dati',
  receiving: 'Ricezione dati cifrati',
  connecting: 'Connessione al Wi-Fi',
  wifi_ok: 'Wi-Fi connesso',
  registering: 'Registrazione sul cloud',
  done: 'Completato',
  error: 'Errore',
};
const ERROR_TEXT = {
  wifi_auth_failed: 'Password Wi-Fi errata.',
  wifi_not_found: 'Rete Wi-Fi non trovata: controlla il nome (SSID) e che il robot sia nel raggio del router (solo 2,4/5 GHz supportati dalla Pi).',
  wifi_timeout: 'Timeout nella connessione al Wi-Fi: riprova avvicinando il robot al router.',
  cloud_unreachable: 'Il robot è in rete ma non raggiunge il cloud: verifica l\'indirizzo del cloud (IP LAN del PC, non localhost) e che il firewall del PC consenta la porta 8000.',
  claim_invalid: 'Codice di associazione non valido o scaduto: riprova (ne verrà generato uno nuovo).',
  // codes emitted by the current pm-core implementation
  wifi_connect_failed: 'Connessione al Wi-Fi non riuscita: controlla rete e password.',
  claim_token_invalid: 'Codice di associazione non valido o scaduto: riprova (ne verrà generato uno nuovo).',
  register_failed: 'Registrazione sul cloud non riuscita: verifica l\'indirizzo del cloud e la rete.',
};

export function errorText(code) {
  if (!code) return 'Errore sconosciuto.';
  if (ERROR_TEXT[code]) return ERROR_TEXT[code];
  if (/^(decrypt|chunk)/.test(code)) return `Il robot non è riuscito a decifrare i dati (${code}). Riprova; se persiste, riavvia la modalità di associazione del robot.`;
  return `Errore dal robot: ${code}`;
}

function bleProblem() {
  if (!window.isSecureContext || !navigator.bluetooth) {
    return 'Web Bluetooth non è disponibile in questa pagina. Apri la console da http://localhost:8000/console/ con Chrome o Edge '
      + '(sul PC che esegue il cloud, con Bluetooth attivo). Firefox e Safari non supportano Web Bluetooth; '
      + 'da un indirizzo IP della LAN il browser non lo consente (contesto non sicuro).';
  }
  return null;
}

const dec = new TextDecoder();
const readJson = async (ch) => JSON.parse(dec.decode(await ch.readValue()));
const isLocalHost = (u) => { try { return /^(localhost|127\.|\[::1\])/.test(new URL(u).hostname); } catch { return true; } };

export function renderWizard(root, { signal, go }) {
  const st = (window.__petmate.wizard = { step: 1, device: null, info: null, networks: [], status: null, error: null, done: false });
  let ble = null;        // {device, chars}
  let pollTimer = null;
  let disposed = false;

  const stepsBar = h('ol', { class: 'steps', 'aria-label': 'Avanzamento' }, [1, 2, 3, 4].map(() => h('li')));
  const body = h('div', { id: 'wizard-body' });
  root.append(h('section', { id: 'wizard', class: 'card' },
    h('div', { class: 'row spread' }, h('h1', {}, 'Aggiungi robot'),
      h('button', { class: 'btn ghost small', onclick: () => go('#/robots') }, 'Annulla')),
    stepsBar, body));

  const setStep = (n) => {
    st.step = n;
    [...stepsBar.children].forEach((li, i) => { li.className = i + 1 < n ? 'done' : i + 1 === n ? 'cur' : ''; });
    body.dataset.step = String(n);
  };

  // ---------------------------------------------------------------- step 1: trova il robot
  function step1(message) {
    setStep(1);
    const problem = bleProblem();
    const err = h('div', { id: 'ble-error', class: 'error' }, message || '');
    body.replaceChildren(
      h('h2', {}, '1 · Trova il robot'),
      h('p', {}, 'Accendi il robot: se non è ancora associato è già in modalità Bluetooth; altrimenti tieni premuto il pulsante zampa per 5 secondi.'),
      problem
        ? h('div', { id: 'ble-unavailable', class: 'notice err' }, problem)
        : h('button', { id: 'ble-scan', class: 'btn accent', onclick: connect }, 'Cerca robot «Petmate-…»'),
      err);
    async function connect() {
      err.textContent = '';
      try {
        const device = await navigator.bluetooth.requestDevice({ filters: [{ namePrefix: 'Petmate-' }], optionalServices: [SERVICE_UUID] });
        st.device = device.name;
        device.addEventListener('gattserverdisconnected', onDisconnected);
        ble = { device, chars: null };
        await ensureConnected();
        st.info = await readJson(ble.chars.deviceInfo);
        await step2();
      } catch (e) {
        if (e?.name === 'NotFoundError') err.textContent = 'Nessun robot selezionato.';
        else err.textContent = `Bluetooth: ${e?.message || e}`;
      }
    }
  }

  async function ensureConnected() {
    if (ble.device.gatt.connected && ble.chars) return;
    const server = await ble.device.gatt.connect();
    const svc = await server.getPrimaryService(SERVICE_UUID);
    const chars = {};
    for (const [k, uuid] of Object.entries(CHAR)) chars[k] = await svc.getCharacteristic(uuid);
    chars.status.addEventListener('characteristicvaluechanged', (ev) => {
      try { onStatus(JSON.parse(dec.decode(ev.target.value))); } catch { /* partial value */ }
    });
    await chars.status.startNotifications().catch(() => { /* fall back to polling */ });
    ble.chars = chars;
  }

  function onDisconnected() {
    if (ble) ble.chars = null;
    if (disposed || st.done) return;
    if (st.step === 4 && ['wifi_ok', 'registering'].includes(st.status?.state)) return; // BLE may stop; presence decides
    if (st.step >= 2) $('#prov-ble-lost', root)?.removeAttribute('hidden');
  }

  // ---------------------------------------------------------------- step 2: Wi-Fi e cloud
  async function step2(note) {
    setStep(2);
    const info = st.info;
    const list = h('ul', { id: 'wifi-list', class: 'ssid-list' });
    const ssid = h('input', { id: 'wifi-ssid', type: 'text', placeholder: 'Nome rete (anche nascosta)', autocomplete: 'off' });
    const pw = h('input', { id: 'wifi-password', type: 'password', autocomplete: 'off' });
    const showPw = h('input', { id: 'wifi-show', type: 'checkbox', onchange: () => { pw.type = showPw.checked ? 'text' : 'password'; } });
    const cloud = h('input', { id: 'cloud-url', type: 'url', list: 'cloud-url-options' });
    const options = h('datalist', { id: 'cloud-url-options' });
    const cloudWarn = h('div', { id: 'cloud-url-warning', class: 'notice small', hidden: true },
      'Il robot non può raggiungere «localhost»: usa l\'indirizzo IP LAN di questo PC (es. http://192.168.1.50:8000/v1).');
    const err = h('div', { id: 'wifi-error', class: 'error' }, note || '');
    cloud.addEventListener('input', () => { cloudWarn.hidden = !isLocalHost(cloud.value); });

    const renderNetworks = () => {
      list.replaceChildren(...st.networks.map((n) => h('li', {
        class: n.ssid === ssid.value ? 'sel' : '', dataset: { ssid: n.ssid },
        onclick: () => { ssid.value = n.ssid; renderNetworks(); pw.focus(); },
      }, h('span', {}, n.ssid, n.secure ? '' : ' (aperta)'), h('span', { class: 'muted small' }, `${n.rssi} dBm`))));
      if (!st.networks.length) list.append(h('li', { class: 'muted' }, 'Nessuna rete trovata (premi «Aggiorna» tra qualche secondo).'));
    };
    const refresh = async () => {
      try { await ensureConnected(); st.networks = (await readJson(ble.chars.wifiScan)) || []; }
      catch (e) { err.textContent = `Lettura reti Wi-Fi non riuscita: ${e.message || e}`; }
      renderNetworks();
    };

    body.replaceChildren(
      h('h2', {}, '2 · Rete Wi-Fi'),
      h('div', { id: 'device-info', class: 'notice ok small' },
        `Connesso a ${st.device || 'robot'} · ${info.device_id} · ${info.model || ''} · fw ${info.fw || '—'}`),
      h('div', { id: 'prov-ble-lost', class: 'notice err small', hidden: true }, 'Connessione Bluetooth persa: verrà ristabilita all\'invio.'),
      h('div', { class: 'row spread' }, h('label', {}, 'Reti visibili dal robot'),
        h('button', { id: 'wifi-refresh', class: 'btn ghost small', type: 'button', onclick: refresh }, 'Aggiorna')),
      list,
      h('label', { for: 'wifi-ssid' }, 'Rete (SSID)'), ssid,
      h('label', { for: 'wifi-password' }, 'Password Wi-Fi'), pw,
      h('label', { class: 'row small' }, showPw, 'mostra password'),
      h('label', { for: 'cloud-url' }, 'Indirizzo del cloud per il robot (cloud_url)'), cloud, options, cloudWarn,
      h('p', { class: 'muted small' }, 'L\'indirizzo che la Raspberry Pi userà per registrarsi: l\'IP LAN di questo PC, porta 8000, percorso /v1.'),
      h('div', { class: 'row' }, h('button', { id: 'prov-send', class: 'btn accent', onclick: () => send(ssid.value, pw.value, cloud.value, err) }, 'Associa robot')),
      err);
    ssid.addEventListener('input', renderNetworks);

    // cloud_url candidates: the page origin if reachable from the LAN, then /v1/server-info
    const cands = [];
    if (!isLocalHost(location.origin)) cands.push(`${location.origin}/v1`);
    try { (await api('GET', '/server-info')).base_urls.forEach((u) => cands.includes(u) || cands.push(u)); } catch { /* optional */ }
    options.replaceChildren(...cands.map((u) => h('option', { value: u })));
    cloud.value = cands[0] || `${location.origin}/v1`;
    cloudWarn.hidden = !isLocalHost(cloud.value);
    st.cloudCandidates = cands;
    await refresh();
  }

  // ---------------------------------------------------------------- step 3: invio cifrato
  async function send(ssid, password, cloudUrl, err) {
    err.textContent = '';
    const network = st.networks.find((n) => n.ssid === ssid);
    if (!ssid || new TextEncoder().encode(ssid).length > 32) { err.textContent = 'Inserisci il nome della rete (max 32 byte).'; return; }
    if (password && (password.length < 8 || password.length > 63)) { err.textContent = 'La password Wi-Fi deve avere da 8 a 63 caratteri.'; return; }
    if (!password && network?.secure !== false && !confirm('Password vuota: confermi che la rete è aperta?')) return;
    if (!/^https?:\/\/[^/]+/.test(cloudUrl)) { err.textContent = 'Indirizzo del cloud non valido (es. http://192.168.1.50:8000/v1).'; return; }
    if (isLocalHost(cloudUrl) && !confirm('L\'indirizzo del cloud punta a localhost: il robot non potrà raggiungerlo. Continuare?')) return;

    setStep(3);
    const progress = h('p', { id: 'prov-progress' }, 'Richiesta codice di associazione…');
    body.replaceChildren(h('h2', {}, '3 · Invio dati cifrati'), progress);
    try {
      const claim = await api('POST', '/claim-tokens');
      await ensureConnected();
      st.info = await readJson(ble.chars.deviceInfo); // fresh pubkey (new BLE window ⇒ new key)
      const msg = encryptPayload(st.info, {
        ssid, password, claim_token: claim.claim_token, cloud_url: cloudUrl.replace(/\/+$/, ''),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Rome',
      });
      const chunks = chunkMessage(msg);
      st.status = null; st.error = null; st.chunks = chunks.length;
      for (let i = 0; i < chunks.length; i++) {
        progress.textContent = `Invio ${i + 1}/${chunks.length}…`;
        const ch = ble.chars.payload;
        await (ch.writeValueWithResponse ? ch.writeValueWithResponse(chunks[i]) : ch.writeValue(chunks[i]));
      }
      step4();
    } catch (e) {
      await step2(`Invio non riuscito: ${e?.status !== undefined ? apiErrorText(e) : (e?.message || e)}`);
    }
  }

  // ---------------------------------------------------------------- step 4: stato
  function step4() {
    setStep(4);
    body.replaceChildren(
      h('h2', {}, '4 · Il robot si collega'),
      h('ul', { id: 'prov-timeline', class: 'timeline' }, STATES.map((s) => h('li', { dataset: { state: s } }, STATE_TEXT[s]))),
      h('div', { id: 'prov-status', class: 'muted small' }, 'In attesa del robot…'),
      h('div', { id: 'prov-result' }));
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => { // fallback if notifications are not delivered
      if (!ble?.chars || st.done || st.error) return;
      try { onStatus(await readJson(ble.chars.status)); } catch { /* disconnected */ }
    }, 2000);
  }

  function onStatus(s) {
    if (st.step !== 4 || st.done) return;
    if (st.error && s.state === 'error') return;
    st.status = s;
    const idx = STATES.indexOf(s.state);
    root.querySelectorAll('#prov-timeline li').forEach((li, i) => {
      li.className = i < idx || s.state === 'done' ? 'done' : i === idx ? 'cur' : '';
    });
    const line = $('#prov-status', root);
    if (line) line.textContent = `Stato: ${STATE_TEXT[s.state] || s.state}${s.ip ? ` · IP robot ${s.ip}` : ''}`;
    if (s.state === 'done') finish(s);
    else if (s.state === 'error') fail(s.error);
  }

  function fail(code) {
    st.error = code || 'unknown';
    clearInterval(pollTimer);
    $('#prov-result', root).replaceChildren(h('div', { id: 'prov-error', class: 'notice err' },
      h('p', {}, errorText(code)),
      h('button', { id: 'prov-retry', class: 'btn', onclick: () => { st.error = null; step2(); } }, 'Riprova')));
  }

  function finish(s) {
    if (st.done) return;
    st.done = true;
    clearInterval(pollTimer);
    const id = st.info.device_id;
    const online = h('span', { id: 'prov-online' }, signal.online.get(id) ? 'online ✓' : 'in attesa che sia online…');
    $('#prov-result', root).replaceChildren(h('div', { id: 'prov-done', class: 'notice ok' },
      h('p', {}, `Robot ${id} associato${s.ip ? ` (IP ${s.ip})` : ''}: `, online),
      h('button', { id: 'prov-goto-robots', class: 'btn', onclick: () => go('#/robots') }, 'Vai ai miei robot')));
    try { ble?.device.gatt.disconnect(); } catch { /* ignore */ }
  }

  const onPresence = (ev) => {
    if (!st.info || ev.detail.device_id !== st.info.device_id || !ev.detail.online || st.step !== 4) return;
    if (!st.done) { onStatus({ state: 'done', error: null, ip: st.status?.ip || null }); }
    const o = $('#prov-online', root);
    if (o) o.textContent = 'online ✓';
  };
  signal.addEventListener('presence', onPresence);

  step1();
  return () => {
    disposed = true;
    clearInterval(pollTimer);
    signal.removeEventListener('presence', onPresence);
    try { ble?.device.gatt.disconnect(); } catch { /* ignore */ }
  };
}
