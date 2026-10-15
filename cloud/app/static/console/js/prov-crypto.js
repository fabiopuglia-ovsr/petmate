// BLE provisioning payload encryption and chunking — INTERFACES.md §7 (+ §9.4).
// Pure functions (no DOM, no Bluetooth): exposed on window.__petmate.prov for tests.
import { x25519 } from '@noble/curves/ed25519.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { chacha20poly1305 } from '@noble/ciphers/chacha.js';

export const SERVICE_UUID = '8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d';
export const CHAR = {
  deviceInfo: '8f1d0001-5e7a-4c3a-9d8e-7e7f1a2b3c4d',
  wifiScan: '8f1d0002-5e7a-4c3a-9d8e-7e7f1a2b3c4d',
  payload: '8f1d0003-5e7a-4c3a-9d8e-7e7f1a2b3c4d',
  status: '8f1d0004-5e7a-4c3a-9d8e-7e7f1a2b3c4d',
};
export const HKDF_INFO = 'petmate-prov-v1';
export const CHUNK_MAX = 180; // whole GATT write, flags byte included (§9.4)
export const FLAG_FIRST = 0x01;
export const FLAG_LAST = 0x02;

const enc = new TextEncoder();

export function b64encode(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s); // standard alphabet, padded (RFC 4648 §4)
}

export function b64decode(str) {
  const s = atob(str.trim().replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

/**
 * Encrypt the provisioning plaintext for one device.
 * @param {{device_id:string, pubkey:string}} deviceInfo  content of char 8f1d0001
 * @param {object} plaintext  {ssid, password, claim_token, cloud_url, timezone}
 * @param {{secretKey?:Uint8Array, nonce?:Uint8Array}} [opts]  deterministic test vectors only
 * @returns {Uint8Array} UTF-8 JSON envelope {"epk","nonce","ct"}
 */
export function encryptPayload(deviceInfo, plaintext, opts = {}) {
  const deviceId = deviceInfo.device_id;
  const devicePub = b64decode(deviceInfo.pubkey);
  if (devicePub.length !== 32) throw new Error('pubkey del robot non valida (attesi 32 byte X25519)');
  const sk = opts.secretKey || x25519.utils.randomSecretKey();      // 1. ephemeral X25519
  const epk = x25519.getPublicKey(sk);
  const shared = x25519.getSharedSecret(sk, devicePub);
  const idBytes = enc.encode(deviceId);
  const key = hkdf(sha256, shared, idBytes, enc.encode(HKDF_INFO), 32); // 2.
  const nonce = opts.nonce || crypto.getRandomValues(new Uint8Array(12));
  const pt = enc.encode(JSON.stringify(plaintext));                      // 3.
  const ct = chacha20poly1305(key, nonce, idBytes).encrypt(pt);         // 4. ct||tag, aad=device_id
  const env = { epk: b64encode(epk), nonce: b64encode(nonce), ct: b64encode(ct) }; // 5.
  return enc.encode(JSON.stringify(env));
}

/** 6. Split into GATT writes of ≤ CHUNK_MAX bytes: [flags][data], bit0 first, bit1 last. */
export function chunkMessage(message, max = CHUNK_MAX) {
  const dataMax = max - 1;
  const chunks = [];
  for (let off = 0; off < message.length || off === 0; off += dataMax) {
    const data = message.subarray(off, off + dataMax);
    let flags = 0;
    if (off === 0) flags |= FLAG_FIRST;
    if (off + dataMax >= message.length) flags |= FLAG_LAST;
    const w = new Uint8Array(1 + data.length);
    w[0] = flags;
    w.set(data, 1);
    chunks.push(w);
    if (message.length === 0) break;
  }
  return chunks;
}
