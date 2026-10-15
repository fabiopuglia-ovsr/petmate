// Live: WebRTC video from the robot (INTERFACES.md §4.1, non-trickle offer from the app)
// + DataChannel "ctl" (state §4.3, snapshot §4.2).
import { api, apiErrorText } from './api.js';
import { $, h } from './ui.js';

const ANSWER_TIMEOUT_MS = 15000;
const GATHER_TIMEOUT_MS = 8000;

const STATUS_TEXT = {
  idle: 'Pronto',
  connecting: 'Connessione al signaling…',
  gathering: 'Raccolta candidati ICE…',
  waiting_answer: 'Offerta inviata, attendo la risposta del robot…',
  negotiating: 'Risposta ricevuta, connessione in corso…',
  connected: 'Connesso',
  closed: 'Disconnesso',
  error: 'Errore',
};

function waitIceGathering(pc, timeoutMs) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve(true);
  return new Promise((resolve) => {
    const t = setTimeout(() => { pc.removeEventListener('icegatheringstatechange', on); resolve(false); }, timeoutMs);
    const on = () => {
      if (pc.iceGatheringState === 'complete') { clearTimeout(t); pc.removeEventListener('icegatheringstatechange', on); resolve(true); }
    };
    pc.addEventListener('icegatheringstatechange', on);
  });
}

async function collectStats(pc, prev) {
  const r = await pc.getStats();
  const by = new Map();
  r.forEach((s) => by.set(s.id, s));
  const out = { ts: Date.now() };
  let pair = null;
  r.forEach((s) => {
    if (s.type === 'inbound-rtp' && s.kind === 'video') {
      out.width = s.frameWidth ?? null;
      out.height = s.frameHeight ?? null;
      out.fps = s.framesPerSecond ?? null;
      out.bytesReceived = s.bytesReceived;
      out.framesDecoded = s.framesDecoded;
      out.packetsLost = s.packetsLost;
      out.codec = by.get(s.codecId)?.mimeType || null;
    }
    if (s.type === 'transport' && s.selectedCandidatePairId) pair = by.get(s.selectedCandidatePairId);
  });
  if (!pair) r.forEach((s) => { if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded') pair = s; });
  if (pair) {
    out.rttMs = pair.currentRoundTripTime != null ? Math.round(pair.currentRoundTripTime * 1000) : null;
    const lc = by.get(pair.localCandidateId);
    const rc = by.get(pair.remoteCandidateId);
    out.localCandidate = lc ? lc.candidateType : null;   // host | srflx | prflx | relay
    out.remoteCandidate = rc ? rc.candidateType : null;
    out.protocol = lc?.protocol || null;
  }
  if (prev?.bytesReceived != null && out.bytesReceived != null) {
    out.kbps = Math.round(((out.bytesReceived - prev.bytesReceived) * 8) / (out.ts - prev.ts));
  }
  return out;
}

export function renderLive(root, deviceId, { signal, go }) {
  const target = `device:${deviceId}`;
  const video = h('video', { id: 'live-video', autoplay: true, muted: true, playsinline: true });
  video.muted = true;
  const overlay = h('div', { id: 'live-overlay', class: 'video-overlay' }, 'Premi «Connetti»');
  const status = h('div', { id: 'live-status', class: 'muted', dataset: { state: 'idle' } }, STATUS_TEXT.idle);
  const err = h('div', { id: 'live-error', class: 'error', role: 'alert' });
  const statsEl = h('dl', { id: 'live-stats', class: 'kv' });
  const stateEl = h('dl', { id: 'live-state', class: 'kv' });
  const stateJson = h('pre', { id: 'live-state-json', class: 'mono' }, '—');
  const snapImg = h('img', { id: 'snapshot-img', alt: 'Snapshot', hidden: true });
  const btnConnect = h('button', { id: 'live-connect', class: 'btn accent', onclick: () => start() }, 'Connetti');
  const btnSnap = h('button', { id: 'snapshot-btn', class: 'btn', disabled: true, onclick: snapshot }, 'Snapshot');
  const btnBye = h('button', { id: 'disconnect-btn', class: 'btn danger', disabled: true, onclick: () => stop('bye') }, 'Disconnetti');
  const title = h('h1', {}, `Live · ${deviceId}`);

  root.append(
    h('section', { class: 'card' },
      h('div', { class: 'row spread' }, title, h('button', { class: 'btn ghost small', onclick: () => go('#/robots') }, '← I miei robot')),
      h('div', { class: 'video-wrap' }, video, overlay),
      h('div', { class: 'row spread', style: 'margin-top:12px' }, status, h('div', { class: 'row' }, btnConnect, btnSnap, btnBye)),
      err),
    h('div', { class: 'grid2' },
      h('section', { class: 'card' }, h('h2', {}, 'Connessione'), statsEl),
      h('section', { class: 'card' }, h('h2', {}, 'Stato robot'), stateEl,
        h('details', {}, h('summary', { class: 'small muted' }, 'JSON completo'), stateJson))),
    h('section', { class: 'card' }, h('h2', {}, 'Snapshot'), h('p', { class: 'muted small', id: 'snapshot-note' }, 'Nessuno snapshot.'), snapImg));

  api('GET', '/devices').then((ds) => {
    const d = ds.find((x) => x.device_id === deviceId);
    if (d) title.textContent = `Live · ${d.name}`;
  }).catch(() => {});

  const dbg = (window.__petmate.live = {
    deviceId, state: 'idle', error: null, pc: null, dc: null, stats: null, lastState: null,
    lastSnapshot: null, messages: [], iceServers: null,
    video: () => ({
      readyState: video.readyState, videoWidth: video.videoWidth, videoHeight: video.videoHeight,
      currentTime: video.currentTime, paused: video.paused, hasStream: !!video.srcObject,
    }),
  });

  let pc = null; let dc = null; let peer = null;
  let answerTimer = null; let statsTimer = null;

  const setState = (s, text) => {
    dbg.state = s;
    status.dataset.state = s;
    status.textContent = text || STATUS_TEXT[s] || s;
    const active = !['idle', 'closed', 'error'].includes(s);
    btnConnect.disabled = active;
    btnBye.disabled = !active;
    btnSnap.disabled = !(dc && dc.readyState === 'open');
  };
  const fail = (code, text) => {
    dbg.error = code;
    if (peer && code !== 'peer_offline') signal.send({ type: 'bye', to: peer });
    peer = null;
    err.textContent = text;
    teardown();
    setState('error');
    overlay.textContent = text;
    overlay.hidden = false;
  };

  function renderStats(s) {
    const rows = [
      ['Risoluzione', s.width ? `${s.width}×${s.height}` : '—'],
      ['FPS', s.fps ?? '—'],
      ['Bitrate', s.kbps != null ? `${s.kbps} kb/s` : '—'],
      ['RTT', s.rttMs != null ? `${s.rttMs} ms` : '—'],
      ['Candidato locale', s.localCandidate || '—'],
      ['Candidato remoto', s.remoteCandidate || '—'],
      ['Protocollo', s.protocol || '—'],
      ['Codec', s.codec || '—'],
      ['ICE', pc?.iceConnectionState || '—'],
    ];
    statsEl.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', { dataset: { k } }, String(v))]));
  }

  function renderState(m) {
    const b = m.battery || {};
    const rows = [
      ['Batteria', b.soc != null ? `${b.soc}%${b.charging ? ' (in carica)' : ''}` : '—'],
      ['Tensione', b.voltage != null ? `${b.voltage} V` : '—'],
      ['Attività', m.activity || '—'],
      ['Modalità', m.mode || '—'],
      ['Firmware', m.fw || '—'],
    ];
    stateEl.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, String(v))]));
    stateJson.textContent = JSON.stringify(m, null, 2);
  }

  function onDcMessage(ev) {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === 'state') { dbg.lastState = m; renderState(m); return; }
    dbg.messages.push(m);
    if (dbg.messages.length > 50) dbg.messages.shift();
    if (m.t === 'snapshot' && m.jpeg_b64) {
      dbg.lastSnapshot = { ts: Date.now(), bytes: Math.floor((m.jpeg_b64.length * 3) / 4) };
      snapImg.src = `data:image/jpeg;base64,${m.jpeg_b64}`;
      snapImg.hidden = false;
      $('#snapshot-note', root).textContent = `Snapshot delle ${new Date().toLocaleTimeString('it-IT')}`;
    }
  }

  const onSignal = async (ev) => {
    const m = ev.detail;
    if (m.type === 'error' && m.to === target && m.ref !== 'bye') {
      if (m.error === 'peer_offline') fail('peer_offline', 'Il robot è offline (non connesso al signaling).');
      else fail(m.error, `Errore signaling: ${m.error}`);
      return;
    }
    if (m.from !== target || !pc) return;
    if (m.type === 'answer') {
      clearTimeout(answerTimer);
      setState('negotiating');
      try { await pc.setRemoteDescription({ type: 'answer', sdp: m.sdp }); }
      catch (e) { fail('bad_answer', `Risposta SDP non valida: ${e.message}`); }
    } else if (m.type === 'ice' && m.candidate) {
      pc.addIceCandidate(m.candidate).catch(() => {});
    } else if (m.type === 'bye') {
      teardown();
      setState('closed', 'Il robot ha chiuso la sessione.');
      overlay.textContent = 'Sessione chiusa dal robot';
      overlay.hidden = false;
    }
  };
  signal.addEventListener('message', onSignal);

  async function start() {
    err.textContent = '';
    dbg.error = null;
    teardown();
    setState('connecting');
    overlay.textContent = 'Connessione…';
    overlay.hidden = false;
    try {
      await signal.ready();
      const { ice_servers: iceServers } = await api('GET', '/ice-servers');
      dbg.iceServers = iceServers;
      pc = dbg.pc = new RTCPeerConnection({ iceServers });
      pc.addTransceiver('video', { direction: 'recvonly' });
      dc = dbg.dc = pc.createDataChannel('ctl', { ordered: true });
      dc.onopen = () => setState(dbg.state);
      dc.onclose = () => setState(dbg.state);
      dc.onmessage = onDcMessage;
      pc.ontrack = (ev) => {
        video.srcObject = ev.streams[0] || new MediaStream([ev.track]);
        video.play().catch(() => {});
      };
      video.onplaying = () => { overlay.hidden = true; };
      pc.onconnectionstatechange = () => {
        const s = pc?.connectionState;
        if (s === 'connected') setState('connected');
        else if (s === 'failed') fail('ice_failed', 'Connessione WebRTC fallita (ICE): da remoto serve probabilmente il TURN.');
        else if (s === 'disconnected') status.textContent = 'Connessione instabile…';
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      setState('gathering');
      await waitIceGathering(pc, GATHER_TIMEOUT_MS); // non-trickle (§4.1): full SDP in the offer
      if (!pc) return;
      peer = target;
      if (!signal.send({ type: 'offer', to: target, sdp: pc.localDescription.sdp })) throw new Error('signaling non connesso');
      setState('waiting_answer');
      answerTimer = setTimeout(() => fail('timeout', 'Nessuna risposta dal robot entro 15 s.'), ANSWER_TIMEOUT_MS);
      statsTimer = setInterval(async () => {
        if (!pc) return;
        try { dbg.stats = await collectStats(pc, dbg.stats); renderStats(dbg.stats); } catch { /* closed */ }
      }, 1000);
    } catch (e) {
      fail('start_failed', e?.status !== undefined ? apiErrorText(e) : `Avvio non riuscito: ${e?.message || e}`);
    }
  }

  function snapshot() {
    if (dc?.readyState !== 'open') return;
    dc.send(JSON.stringify({ t: 'snapshot', id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) }));
    $('#snapshot-note', root).textContent = 'Snapshot richiesto…';
  }

  function teardown() {
    clearTimeout(answerTimer);
    clearInterval(statsTimer);
    if (dc) { try { dc.close(); } catch { /* ignore */ } }
    if (pc) { try { pc.close(); } catch { /* ignore */ } }
    pc = dc = dbg.pc = dbg.dc = null;
  }

  function stop(reason) {
    if (peer) signal.send({ type: 'bye', to: peer });
    peer = null;
    teardown();
    video.srcObject = null;
    if (reason === 'bye') {
      setState('closed');
      overlay.textContent = 'Disconnesso';
      overlay.hidden = false;
    }
  }

  dbg.start = start;
  dbg.stop = () => stop('bye');
  renderStats({});
  start();
  return () => { signal.removeEventListener('message', onSignal); stop('leave'); window.__petmate.live = null; };
}
