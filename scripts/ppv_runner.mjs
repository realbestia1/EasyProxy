#!/usr/bin/env node
// ppv_runner.mjs - ppv.st resolver (API -> embed /fetch -> wasm decrypt).
//
// Usage: node scripts/ppv_runner.mjs <event_url|embed_url>   (e.g. https://ppv.st/live/nfl-network,
//        https://taifood-blog.asia/embed/nfl/2026-11-15/kc-atl or the bare path nfl/2026-11-15/kc-atl)
// Outputs JSON to stdout: {"m3u8": "...", "embedOrigin": "...", "embedPath": "...", "slug": "...", "status": 200}
//
// Requires Node >= 18 (WebAssembly + native fetch) and happy-dom in node_modules.

// The embed host (taifood-blog.asia) serves a certificate Node does not trust,
// and its /fetch endpoint is the only way to get the island header. The variable
// is process-wide and must be set before any fetch runs, so every request below
// (embed, API, playlist check) skips certificate verification.
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Window } from 'happy-dom';

const DEBUG = process.env.PPV_DEBUG === '1' || process.argv.includes('--debug');
function L(...a) { if (DEBUG) console.error('[ppv]', ...a); }

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36';
const TIMEOUT_MS = 30000;

// API domain failover chain - tried in order until one answers.
const API_DOMAINS = ['ppv.tj', 'ppvs.pk', 'ppv.rw', 'ppv.ms', 'ppv.bi', 'ppv.ug'];

// ntv.cx (same provider) already hands out the final embed URL, so its API
// response can be passed straight in and the ppv API lookup is skipped.
const EMBED_HOST = 'taifood-blog.asia';

const WASM_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'ppv');
const WASM_JS = pathToFileURL(path.join(WASM_DIR, 'gasm.js')).href;
let wasmLoadGeneration = 0;

async function loadWasmModule() {
  wasmLoadGeneration += 1;
  return import(`${WASM_JS}?${wasmLoadGeneration}`);
}

// The CDN edge 403s TLS ClientHellos outside its allowlist. Node's default
// (long) cipher list is refused, OpenSSL's/Python's default list is accepted,
// so the playlist check uses the same list - direct and through the proxy.
const CDN_CIPHERS = [
  'ECDHE-ECDSA-AES256-GCM-SHA384', 'ECDHE-RSA-AES256-GCM-SHA384',
  'ECDHE-ECDSA-AES128-GCM-SHA256', 'ECDHE-RSA-AES128-GCM-SHA256',
  'ECDHE-ECDSA-CHACHA20-POLY1305', 'ECDHE-RSA-CHACHA20-POLY1305',
  'ECDHE-ECDSA-AES256-SHA384', 'ECDHE-RSA-AES256-SHA384',
  'ECDHE-ECDSA-AES128-SHA256', 'ECDHE-RSA-AES128-SHA256',
  'DHE-RSA-AES256-GCM-SHA384', 'DHE-RSA-AES128-GCM-SHA256',
  'DHE-RSA-AES256-SHA256', 'DHE-RSA-AES128-SHA256',
].join(':');

let proxyDispatcher = null;
// Node's built-in fetch rejects dispatchers built by a differently-versioned
// undici, so when a proxy dispatcher is active fetch must come from that module.
let realFetch = globalThis.fetch.bind(globalThis);
const PROXY_URL = process.env.PPV_PROXY || process.env.EMBEDST_PROXY;
if (PROXY_URL && /^https?:\/\//i.test(PROXY_URL)) {
  try {
    const undici = await import('undici');
    proxyDispatcher = new undici.ProxyAgent({ uri: PROXY_URL, requestTls: { ciphers: CDN_CIPHERS } });
    realFetch = undici.fetch.bind(undici);
  } catch (error) {
    console.error('[ppv] HTTP proxy unavailable, continuing direct:', error && error.message);
  }
}

const routedFetch = (url, opts = {}) => proxyDispatcher
  ? realFetch(url, { ...opts, dispatcher: proxyDispatcher })
  : realFetch(url, opts);

function describeError(error) {
  const parts = [];
  let current = error;
  for (let depth = 0; current && depth < 4; depth += 1) {
    const text = typeof current === 'string' ? current : current.message || String(current);
    if (text && !parts.includes(text)) parts.push(text);
    current = typeof current === 'object' ? current.cause : undefined;
  }
  return parts.join(' <- ') || String(error);
}

// ---------------------------------------------------------------------------
// ppv API -> embed URL
// ---------------------------------------------------------------------------
function parseInput(eventUrl) {
  const raw = String(eventUrl || '').trim();
  if (!raw) throw new Error('missing event url');
  let pathname = raw;
  if (/^https?:\/\//i.test(raw)) {
    try {
      pathname = new URL(raw).pathname;
    } catch {
      throw new Error(`invalid url ${raw}`);
    }
  }
  if (!pathname.startsWith('/')) pathname = `/${pathname}`;
  // https://ppv.tj/live/nfl/2026-11-15/ne-det -> "nfl/2026-11-15/ne-det"
  let uri = pathname.replace(/^\/+/, '');
  if (uri.startsWith('live/')) uri = uri.slice(5);
  uri = uri.replace(/^24\/7-/i, '247-');
  if (!uri) throw new Error(`cannot parse stream uri from ${raw}`);
  return uri;
}

function embedFromSource(source) {
  const raw = typeof source === 'string' ? source : source && source.data;
  if (!raw) throw new Error('source has no embed url');
  const u = new URL(raw);
  const prefix = '/embed/';
  if (!u.pathname.startsWith(prefix)) throw new Error(`unexpected embed url ${raw}`);
  return { origin: u.origin, path: u.pathname.slice(prefix.length) };
}

// ntv.cx passes the embed URL/path directly: no ppv API lookup needed.
function embedFromInput(eventUrl) {
  const raw = String(eventUrl || '').trim();
  if (!raw) return null;
  if (!/^https?:\/\//i.test(raw)) {
    // Bare embed path, e.g. "nfl/2026-11-15/kc-atl".
    const bare = raw.replace(/^\/+/, '');
    if (!bare || bare.startsWith('live/')) return null;
    return { origin: `https://${EMBED_HOST}`, path: bare.replace(/^embed\//, '') };
  }
  try {
    const u = new URL(raw);
    if (u.hostname.endsWith(EMBED_HOST) && u.pathname.startsWith('/embed/')) return embedFromSource(raw);
  } catch {
    // not an embed url - fall back to the ppv API below
  }
  return null;
}

async function fetchMeta(uri) {
  const errors = [];
  for (const domain of API_DOMAINS) {
    const api = `https://api.${domain}/api/streams/${encodeURIComponent(uri)}`;
    try {
      const res = await routedFetch(api, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
      if (!res.ok) { errors.push(`${domain}: HTTP ${res.status}`); continue; }
      const json = await res.json();
      const data = (json && json.data) || {};
      const sources = Array.isArray(data.sources) ? data.sources : [];
      const picked = sources.find((s) => s && s.default) || sources[0];
      if (!picked) { errors.push(`${domain}: no sources`); continue; }
      L('meta from', domain, picked.data);
      return embedFromSource(picked);
    } catch (error) {
      errors.push(`${domain}: ${describeError(error)}`);
    }
  }
  throw new Error(`api failover exhausted (${errors.join('; ')})`);
}

// ---------------------------------------------------------------------------
// embed /fetch -> island + protobuf body
// ---------------------------------------------------------------------------
function encodeFetchBody(embedPath) {
  const bytes = Buffer.from(embedPath, 'utf8');
  return Buffer.concat([Buffer.from([0x0a, bytes.length]), bytes]);
}

async function postEmbedFetch(embed) {
  const res = await routedFetch(`${embed.origin}/fetch`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      Origin: embed.origin,
      Referer: `${embed.origin}/embed/${embed.path}`,
      'User-Agent': UA,
    },
    body: encodeFetchBody(embed.path),
  });
  if (!res.ok) throw new Error(`embed fetch ${res.status}`);
  const island = res.headers.get('island');
  if (!island) throw new Error('missing island header');
  return { island, body: Buffer.from(await res.arrayBuffer()) };
}

function readVarint(buf, offset) {
  let value = 0;
  let shift = 0;
  let i = offset;
  while (i < buf.length) {
    const byte = buf[i++];
    value |= (byte & 0x7f) << shift;
    if (!(byte & 0x80)) return { value, next: i };
    shift += 7;
  }
  return { value, next: i };
}

function slugFromFetchBody(body) {
  const buf = Buffer.from(body);
  let i = 0;
  while (i < buf.length) {
    const tag = buf[i++];
    const field = tag >> 3;
    const wire = tag & 7;
    if (wire !== 2) break;
    const { value: len, next } = readVarint(buf, i);
    i = next;
    if (i + len > buf.length) break;
    const value = buf.subarray(i, i + len).toString('utf8');
    i += len;
    if (field === 2 && value && !value.startsWith('{')) return value;
  }
  return null;
}

function extractUrl(memory, slug) {
  const buf = Buffer.from(memory.buffer);
  const text = buf.toString('latin1');
  const re = /https:\/\/[a-z0-9.-]+\/secure\/[^\x00-\x1f\s"']+?index\.m3u8/g;
  const matches = [];
  let match = null;
  while ((match = re.exec(text)) !== null) matches.push(match[0]);
  if (slug) return matches.find((url) => url.includes(`/${slug}/`)) || null;
  return matches.at(-1) || null;
}

async function decryptStreamUrl(island, body, embed, slug) {
  const saved = {
    fetch: globalThis.fetch,
    Request: globalThis.Request,
    Response: globalThis.Response,
    window: globalThis.window,
    Window: globalThis.Window,
    document: globalThis.document,
    location: globalThis.location,
    self: globalThis.self,
    jwplayer: globalThis.jwplayer,
  };

  const jwEngine = { destroy() {} };
  const jwPlayer = {
    remove() {
      return jwEngine;
    },
    setup() {},
    on() {},
    load() {},
    getPlaylistItem: () => ({}),
    getState: () => 'idle',
  };
  Object.defineProperty(jwPlayer, '__wasm_jw_player', { value: jwPlayer, enumerable: false });
  Object.defineProperty(jwEngine, '__wasm_jw_engine', { value: jwEngine, enumerable: false });

  const window = new Window({
    url: `${embed.origin}/embed/${embed.path}`,
    settings: { disableJavaScriptFileLoading: true, disableJavaScriptEvaluation: false },
  });

  window.eval = () => undefined;
  window.jwplayer = () => jwPlayer;
  window.__wasm_jw_player = jwPlayer;
  window.__wasm_jw_engine = jwEngine;
  window.__wasm_player = { core: { mediaControl: { volume: 0 } } };
  window.__wasm_p2p_config = {};
  window.P2PEngineHls = class {};

  const resolveEmbedUrl = (url) => (typeof url === 'string' && url.startsWith('/') ? `${embed.origin}${url}` : url);

  const embedFetch = async () =>
    new window.Response(body, {
      status: 200,
      headers: { 'content-type': 'application/octet-stream', island },
    });

  const BaseRequest = saved.Request;
  globalThis.Request = class extends BaseRequest {
    constructor(input, init) {
      if (typeof input === 'string') {
        super(resolveEmbedUrl(input), init);
        return;
      }
      super(input, init);
    }
  };

  globalThis.fetch = embedFetch;
  window.fetch = embedFetch;
  window.Request = globalThis.Request;
  globalThis.Response = window.Response;

  Object.assign(globalThis, {
    window,
    Window,
    document: window.document,
    location: window.location,
    self: window,
    jwplayer: window.jwplayer,
  });

  try {
    const mod = await loadWasmModule();
    const wasm = await mod.default({ module_or_path: fs.readFileSync(path.join(WASM_DIR, 'gasm.wasm')) });

    // Runtime state the obfuscated module expects at fixed data-segment offsets.
    const u8 = new Uint8Array(wasm.memory.buffer);
    const dv = new DataView(wasm.memory.buffer);
    u8[1070512] = 3;
    u8[1070513] = 1;
    u8[1070488] = 1;
    u8[1070508] = 1;
    dv.setInt32(1070476, -2147483648, true);
    dv.setInt32(1070472, 0, true);
    dv.setInt32(1070496, -2147483648, true);
    dv.setInt32(1070492, 0, true);

    const result = wasm.set_stream_jw(island, new Uint8Array(body));
    await Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error('wasm timeout')), 15000))]).catch(
      (error) => L('wasm result', error && error.message),
    );

    const streamUrl = extractUrl(wasm.memory, slug);
    if (!streamUrl) throw new Error('decrypt did not produce m3u8 url');
    return streamUrl;
  } finally {
    globalThis.fetch = saved.fetch;
    globalThis.Request = saved.Request;
    globalThis.Response = saved.Response;
    globalThis.window = saved.window;
    globalThis.Window = saved.Window;
    globalThis.document = saved.document;
    globalThis.location = saved.location;
    globalThis.self = saved.self;
    globalThis.jwplayer = saved.jwplayer;
  }
}

// ---------------------------------------------------------------------------
// Playlist check
// ---------------------------------------------------------------------------
async function fetchPlaylist(url, headers) {
  if (proxyDispatcher) {
    const res = await routedFetch(url, { headers });
    return { status: res.status, text: res.ok ? await res.text() : '' };
  }
  const https = await import('node:https');
  return new Promise((resolve, reject) => {
    const req = https.default.get(url, { headers, ciphers: CDN_CIPHERS }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, text }));
    });
    req.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
let finished = false;
function finish(payload, code) {
  if (finished) return;
  finished = true;
  process.stdout.write(JSON.stringify(payload));
  process.exit(code);
}
const fail = (error) => finish({ error: describeError(error) }, 1);

setTimeout(() => fail(new Error(`timeout after ${TIMEOUT_MS}ms`)), TIMEOUT_MS).unref();

(async () => {
  const eventUrl = process.argv[2];
  if (!eventUrl) { console.error('usage: ppv_runner.mjs <event_url>'); process.exit(2); }

  const uri = parseInput(eventUrl);
  L('uri', uri);

  const embed = embedFromInput(eventUrl) || (await fetchMeta(uri));
  L('embed', embed.origin, embed.path);

  const { island, body } = await postEmbedFetch(embed);
  const slug = slugFromFetchBody(body);
  L('slug', slug, 'body', body.length);

  const m3u8 = await decryptStreamUrl(island, body, embed, slug);

  // Fail loudly if the decrypted URL does not actually serve a manifest.
  L('m3u8', m3u8);
  const res = await fetchPlaylist(m3u8, { 'User-Agent': UA, Referer: `${embed.origin}/embed/${embed.path}` });
  if (res.status !== 200 || !res.text.trimStart().startsWith('#EXTM3U')) {
    throw new Error(`playlist check failed (HTTP ${res.status})`);
  }

  finish({ m3u8, embedOrigin: embed.origin, embedPath: embed.path, slug, status: res.status }, 0);
})().catch(fail);
