// Signaling WebSocket client (INTERFACES.md §6): /v1/signal?token=<JWT>.
// One shared connection per console session; reconnects with backoff.
// Events: 'state' (detail: 'connecting'|'open'|'closed'), 'message' (detail: msg),
//         'presence' (detail: {device_id, online}), 'fatal' (detail: close code).

export class Signal extends EventTarget {
  constructor() {
    super();
    this.ws = null;
    this.peer = null;
    this.state = 'closed';
    this.online = new Map(); // device_id -> bool (from presence)
    this._token = null;
    this._retry = 0;
    this._timer = null;
    this._ping = null;
    this._stopped = true;
  }

  start(token) {
    if (this._token === token && !this._stopped) return;
    this.stop();
    this._token = token;
    this._stopped = false;
    this._connect();
  }

  stop() {
    this._stopped = true;
    clearTimeout(this._timer);
    clearInterval(this._ping);
    if (this.ws) { try { this.ws.close(1000); } catch { /* ignore */ } }
    this.ws = null;
    this.peer = null;
    this.online.clear();
    this._setState('closed');
  }

  send(msg) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  /** Resolves when the socket is open and `hello` was received. */
  ready(timeoutMs = 8000) {
    if (this.state === 'open' && this.peer) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.removeEventListener('state', on); reject(new Error('signaling non connesso')); }, timeoutMs);
      const on = () => {
        if (this.state === 'open' && this.peer) { clearTimeout(t); this.removeEventListener('state', on); resolve(); }
      };
      this.addEventListener('state', on);
    });
  }

  _setState(s) {
    this.state = s;
    this.dispatchEvent(new CustomEvent('state', { detail: s }));
  }

  _connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/v1/signal?token=${encodeURIComponent(this._token)}`);
    this.ws = ws;
    this._setState('connecting');
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'hello') {
        this.peer = msg.peer;
        this._retry = 0;
        this._setState('open');
      } else if (msg.type === 'presence') {
        this.online.set(msg.device_id, !!msg.online);
        this.dispatchEvent(new CustomEvent('presence', { detail: msg }));
      }
      this.dispatchEvent(new CustomEvent('message', { detail: msg }));
    };
    ws.onopen = () => {
      clearInterval(this._ping);
      this._ping = setInterval(() => this.send({ type: 'ping' }), 20000);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      clearInterval(this._ping);
      this.ws = null;
      this.peer = null;
      this._setState('closed');
      if (ev.code === 4401) {
        this._stopped = true;
        this.dispatchEvent(new CustomEvent('fatal', { detail: ev.code }));
        return;
      }
      if (this._stopped) return;
      const delay = Math.min(15000, 1000 * 2 ** this._retry++);
      this._timer = setTimeout(() => this._connect(), delay);
    };
  }
}
