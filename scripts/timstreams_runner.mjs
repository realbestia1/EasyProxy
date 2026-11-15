#!/usr/bin/env node
// timstreams_runner.mjs - Headless (no-browser) extractor for tim-streams.com
// (ehgembxe.cfd player).
//
// The player page runs /plum.js (wasm_exec + Go anti-sandbox probe) and a set of
// obfuscated inline scripts that fetch an encrypted payload, decrypt it and hand
// the final URL to Hls.loadSource(). This runner replays both inside a Node vm
// sandbox with stubbed DOM/fetch and captures the .m3u8 URL.
//
// The Go probe calls window.sandDetect() when it does not see a browser; keeping
// it a non-writable no-op is what lets the inline player run to completion.
//
// Usage: node timstreams_runner.mjs <player_url>
// Outputs JSON to stdout: {"m3u8": "...", "status": 200, "player_url": "..."}
//
// Requires Node >= 18 (WebAssembly + native fetch).

import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

// Node 18 has no global WebCrypto: fall back to node:crypto's implementation.
const nativeCrypto = globalThis.crypto ?? webcrypto;

const DEBUG = process.env.TIMSTREAMS_DEBUG === '1' || process.argv.includes('--debug');
function L(...a) { if (DEBUG) console.error('[timstreams]', ...a); }

// undici reports network failures as "fetch failed" and hides the real reason
// (DNS, connect, TLS, proxy) in error.cause. Surface the whole chain.
function describeError(error) {
  const parts = [];
  let current = error;
  for (let depth = 0; current && depth < 4; depth += 1) {
    const text = typeof current === 'string' ? current : current.message || String(current);
    const code = typeof current === 'object' ? current.code : undefined;
    const label = code && !text.includes(code) ? `${code}: ${text}` : text;
    if (label && !parts.includes(label)) parts.push(label);
    current = typeof current === 'object' ? current.cause : undefined;
  }
  return parts.join(' <- ') || String(error);
}

const INPUT = process.argv[2];
if (!INPUT) { console.error('usage: timstreams_runner.mjs <player_url>'); process.exit(2); }
const u = new URL(INPUT);
const ORIGIN = u.origin;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36';

let proxyDispatcher = null;
let undiciModule = null;
// Node's built-in fetch rejects dispatchers built by a differently-versioned
// undici, so when a proxy dispatcher is active fetch must come from that module.
let baseFetch = globalThis.fetch.bind(globalThis);
const proxyEnv = process.env.TIMSTREAMS_PROXY || process.env.EMBEDST_PROXY;
if (proxyEnv && /^https?:\/\//i.test(proxyEnv)) {
  try {
    const undici = await import('undici');
    undiciModule = undici;
    proxyDispatcher = new undici.ProxyAgent(proxyEnv);
    baseFetch = undici.fetch.bind(undici);
  } catch (error) {
    console.error('[timstreams] HTTP proxy unavailable, continuing direct:', describeError(error));
  }
}
const routedFetch = (url, opts = {}) => proxyDispatcher
  ? baseFetch(url, { ...opts, dispatcher: proxyDispatcher })
  : baseFetch(url, opts);

let hlsUrl = null;       // Hls.loadSource() target: the URL the player really plays
let fallbackUrl = null;  // any other .m3u8 seen in fetch/src
let finalUrl = INPUT;    // page URL after redirects (Referer for the CDN)

const playerHeaders = () => ({ 'User-Agent': UA, 'Referer': finalUrl, 'Origin': ORIGIN });

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ---------------------------------------------------------------------------
// fetch stub: resolve against the page, log, capture .m3u8
// ---------------------------------------------------------------------------
async function realFetch(url, opts = {}) {
  const full = new URL(String(url), ORIGIN).href;
  if (/\.m3u8(\?|$)/i.test(full)) {
    if (!fallbackUrl) fallbackUrl = full;
    L('FETCH-M3U8', full);
  } else {
    L('FETCH', full.slice(0, 160));
  }
  const headers = { 'User-Agent': UA, ...(opts.headers || {}) };
  return routedFetch(full, { ...opts, headers, redirect: 'follow' });
}

// ---------------------------------------------------------------------------
// DOM stubs (just enough for the player's inline scripts)
// ---------------------------------------------------------------------------
function stubEl(tag = 'div') {
  const o = {
    tagName: String(tag).toUpperCase(), style: { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; }, getPropertyValue(k) { return this[k] ?? ''; } },
    dataset: {}, children: [], _attrs: {}, _listeners: {},
    _id: '', _text: '', _html: '', parentNode: null, parentElement: null,
    clientWidth: 1920, clientHeight: 1080, offsetWidth: 1920, offsetHeight: 1080,
    href: '', onload: null, onerror: null, async: false, defer: false,
    currentTime: 0, duration: 0, muted: false, volume: 1,
  };
  Object.defineProperty(o, 'id', { get: () => o._id, set: v => { o._id = String(v); }, enumerable: true });
  Object.defineProperty(o, 'textContent', { get: () => o._text, set: v => { o._text = String(v ?? ''); }, enumerable: true });
  Object.defineProperty(o, 'innerHTML', { get: () => o._html, set: v => { o._html = String(v ?? ''); }, enumerable: true });
  Object.defineProperty(o, 'innerText', { get: () => o._text, set: v => { o._text = String(v ?? ''); }, enumerable: true });
  Object.defineProperty(o, 'className', { get: () => o._attrs.class || '', set: v => { o._attrs.class = String(v); }, enumerable: true });
  o.setAttribute = (k, v) => { o._attrs[k] = String(v); };
  o.getAttribute = (k) => (k in o._attrs ? o._attrs[k] : null);
  o.removeAttribute = (k) => { delete o._attrs[k]; };
  o.hasAttribute = (k) => k in o._attrs;
  o.appendChild = (c) => { if (c && typeof c === 'object') { o.children.push(c); try { c.parentNode = o; c.parentElement = o; } catch (e) {} } return c; };
  o.removeChild = (c) => { const i = o.children.indexOf(c); if (i >= 0) o.children.splice(i, 1); return c; };
  o.remove = () => {};
  o.insertBefore = (n) => { if (n) o.children.push(n); return n; };
  o.replaceChild = (n, old) => old;
  o.append = (...n) => n.forEach(x => o.children.push(x));
  o.insertAdjacentHTML = (p, h) => { o._html += String(h ?? ''); };
  o.insertAdjacentElement = (p, e) => e;
  o.cloneNode = () => stubEl(tag);
  o.contains = () => false; o.matches = () => false; o.closest = () => null;
  o.scrollIntoView = () => {};
  o.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1920, bottom: 1080, width: 1920, height: 1080, x: 0, y: 0 });
  o.querySelector = (s) => { if (!o._q) o._q = {}; if (!o._q[s]) { o._q[s] = stubEl('div'); o._q[s].parentNode = o; } return o._q[s]; };
  o.querySelectorAll = () => [];
  o.getElementsByTagName = () => []; o.getElementsByClassName = () => [];
  o.classList = { _s: new Set(), add(v) { this._s.add(v); }, remove(v) { this._s.delete(v); }, contains(v) { return this._s.has(v); }, toggle(v) { if (this._s.has(v)) this._s.delete(v); else this._s.add(v); } };
  o.addEventListener = (e, f) => { (o._listeners[e] = o._listeners[e] || []).push(f); };
  o.removeEventListener = () => {};
  o.dispatchEvent = () => true;
  o.click = () => {}; o.focus = () => {}; o.blur = () => {};
  o.play = () => Promise.resolve(); o.pause = () => {}; o.load = () => Promise.resolve();
  o.canPlayType = () => 'maybe';
  Object.defineProperty(o, 'src', {
    get() { return o._src || ''; },
    set(v) {
      o._src = String(v ?? '');
      // Some page builds assign the manifest straight to a media element.
      if (/\.m3u8(\?|$)/i.test(o._src)) { if (!fallbackUrl) fallbackUrl = o._src; L('SRC-M3U8', o._src); }
    },
    enumerable: true, configurable: true,
  });
  return o;
}

const urls = { href: INPUT, protocol: u.protocol, host: u.host, hostname: u.hostname, port: u.port,
  pathname: u.pathname, search: u.search, hash: u.hash, origin: ORIGIN,
  toString() { return urls.href; }, reload() {}, replace() {}, assign() {} };

const _els = {};
const doc = {
  body: stubEl('body'), head: stubEl('head'), documentElement: stubEl('html'),
  createElement: t => stubEl(t), createTextNode: () => stubEl('#text'),
  getElementById: (id) => { if (!_els[id]) { _els[id] = stubEl('div'); _els[id].id = id; _els[id].parentNode = doc.body; } return _els[id]; },
  querySelector: (s) => { const key = `q:${s}`; if (!_els[key]) { _els[key] = stubEl('div'); _els[key].parentNode = doc.body; } return _els[key]; },
  querySelectorAll: () => [], getElementsByTagName: () => { const s = stubEl('script'); s.parentNode = doc.head; return [s]; },
  getElementsByClassName: () => [], addEventListener() {}, removeEventListener() {},
  cookie: '', readyState: 'complete', title: '', location: urls, currentScript: stubEl('script'),
  scripts: [], forms: [], images: [], write() {}, writeln() {}, open() {}, close() {},
  createEvent: () => ({ initEvent() {}, preventDefault() {} }),
  hidden: false, visibilityState: 'visible', referrer: '', domain: u.hostname,
};

const storage = () => ({ _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; }, clear() { this._d = {}; } });

const win = {
  document: doc, location: urls,
  navigator: { userAgent: UA, platform: 'Win32', language: 'en', languages: ['en'], vendor: 'Google Inc.', sendBeacon: () => true, userAgentData: {}, plugins: [], mimeTypes: [] },
  innerWidth: 1920, innerHeight: 1080, outerWidth: 1920, outerHeight: 1080,
  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1080 },
  devicePixelRatio: 1, name: '', closed: false, opener: null, top: null, parent: null, self: null, frameElement: null,
  setTimeout: (fn, ms) => setTimeout(fn, Math.min(Number(ms) || 0, 5000)),
  clearTimeout, setInterval: (fn, ms) => setInterval(fn, Math.max(Number(ms) || 0, 50)),
  clearInterval, requestAnimationFrame: cb => setTimeout(() => cb(Date.now()), 16), cancelAnimationFrame: clearTimeout,
  fetch: realFetch, XMLHttpRequest: undefined,
  atob: s => Buffer.from(String(s), 'base64').toString('latin1'),
  btoa: s => Buffer.from(String(s), 'latin1').toString('base64'),
  TextDecoder, TextEncoder, WebAssembly, performance: globalThis.performance,
  URL, URLSearchParams, Blob, Response, Request, Headers, AbortController,
  localStorage: storage(), sessionStorage: storage(),
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
  matchMedia: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
  MutationObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  Image: class { constructor() { this.src = ''; } }, Audio: class { play() {} pause() {} },
  alert() {}, confirm: () => true, prompt: () => null, console,
  getComputedStyle: () => ({ getPropertyValue: () => '' }), scrollTo() {}, open: () => null,
  googletag: { cmd: [] }, adsbygoogle: [], dataLayer: [],
  // hls.js stand-in: the player hands the final manifest URL to loadSource().
  Hls: class Hls {
    static isSupported() { return true; }
    static get Events() { return {}; }
    static get ErrorTypes() { return {}; }
    constructor() { this._listeners = {}; }
    loadSource(url) {
      const s = String(url);
      L('HLS', s);
      if (!hlsUrl) hlsUrl = s;
    }
    attachMedia() {} on() {} off() {} destroy() {} startLoad() {} recoverMediaError() {}
  },
  MediaSource: class MediaSource { static isTypeSupported() { return true; } addEventListener() {} },
  ga() {}, gtag() {}, clarity() {},
};
// crypto.subtle decrypt is how the payload is opened; log the algorithm.
win.crypto = {
  getRandomValues: (a) => nativeCrypto.getRandomValues(a),
  subtle: new Proxy(nativeCrypto.subtle, {
    get(target, prop) {
      const value = target[prop];
      if (typeof value === 'function' && ['decrypt', 'encrypt', 'importKey', 'deriveKey', 'digest'].includes(prop)) {
        return (...args) => { L('crypto', prop, args[0]?.name || args[0]?.algorithm?.name || ''); return value.apply(target, args); };
      }
      return typeof value === 'function' ? value.bind(target) : value;
    },
  }),
};
win.window = win; win.self = win; win.top = win; win.parent = win;
win.globalThis = win; win.global = win;
// The Go probe calls window.sandDetect() when the environment is not a browser.
// Keep it a non-writable no-op so the page's guard cannot block the player.
win.__plumQueue = [];
Object.defineProperty(win, 'sandDetect', {
  value: function sandDetect() { /* sandbox probe ignored */ },
  writable: false, configurable: false, enumerable: true,
});
doc.defaultView = win; doc.parentWindow = win; doc.location = urls;
doc.currentScript.parentNode = doc.head;

const sandbox = vm.createContext(win);
sandbox.window = win; sandbox.self = win; sandbox.document = doc; sandbox.location = urls;
sandbox.globalThis = win; sandbox.fetch = realFetch; sandbox.console = console;
sandbox.setTimeout = win.setTimeout; sandbox.clearTimeout = clearTimeout;
sandbox.setInterval = win.setInterval; sandbox.clearInterval = clearInterval;
sandbox.atob = win.atob; sandbox.btoa = win.btoa;
sandbox.TextDecoder = TextDecoder; sandbox.TextEncoder = TextEncoder;
sandbox.WebAssembly = WebAssembly; sandbox.crypto = win.crypto;
sandbox.performance = globalThis.performance; sandbox.URL = URL; sandbox.URLSearchParams = URLSearchParams;
sandbox.Blob = Blob; sandbox.Response = undiciModule ? undiciModule.Response : Response;
sandbox.Request = undiciModule ? undiciModule.Request : Request;
sandbox.Headers = undiciModule ? undiciModule.Headers : Headers;
sandbox.AbortController = AbortController;
for (const name of ['HTMLVideoElement', 'HTMLMediaElement', 'HTMLElement', 'HTMLDivElement', 'HTMLIFrameElement',
  'HTMLScriptElement', 'HTMLAnchorElement', 'HTMLInputElement', 'HTMLButtonElement', 'HTMLImageElement',
  'Element', 'Node', 'Event', 'CustomEvent', 'KeyboardEvent', 'MouseEvent', 'SVGElement', 'DocumentFragment']) {
  sandbox[name] = class {};
  win[name] = sandbox[name];
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
process.on('uncaughtException', e => L('uncaught', describeError(e)));
process.on('unhandledRejection', e => L('unhandled', describeError(e)));

let finished = false;
function emit(payload, code) {
  if (finished) return;
  finished = true;
  process.stdout.write(JSON.stringify(payload));
  process.exit(code);
}

async function verifyAndFinish(url) {
  if (!url) return emit({ error: 'no m3u8' }, 1);
  // The token is bound to this process' IP/session, so the manifest is verified
  // here (same headers the player uses) before anything else touches it.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const resp = await routedFetch(url, { headers: playerHeaders(), redirect: 'follow' });
      const body = await resp.text();
      L('VERIFY', resp.status, body.slice(0, 60));
      if (resp.status === 200 && body.includes('#EXTM3U')) {
        emit({ m3u8: url, status: resp.status, player_url: finalUrl }, 0);
        return;
      }
      if (resp.status !== 200) {
        emit({ m3u8: url, status: resp.status, player_url: finalUrl, error: `manifest HTTP ${resp.status}` }, 1);
        return;
      }
    } catch (error) {
      L('VERIFY-ERR', describeError(error));
    }
    await sleep(500);
  }
  emit({ m3u8: url, status: 0, player_url: finalUrl, error: 'manifest not readable' }, 1);
}

(async () => {
  // Overall ceiling: never outlive the Python-side 45s.
  setTimeout(() => emit({ error: 'timeout (no m3u8)' }, 1), 25000).unref();

  // /plum.js is wasm_exec + plum_boot.js: the page needs it before its inline
  // scripts run, otherwise the loader throws and no payload is ever fetched.
  try {
    const plumRes = await routedFetch(ORIGIN + '/plum.js', { headers: { 'User-Agent': UA, 'Referer': INPUT } });
    const plumJs = await plumRes.text();
    new vm.Script(plumJs, { filename: 'plum.js' }).runInContext(sandbox, { timeout: 10000 });
    L('PLUM ok', plumJs.length);
  } catch (error) {
    L('PLUM-ERR', describeError(error));
  }

  // The player page decides everything: on failure say which host and route.
  // The host drops connections under load (ECONNRESET), so one network retry.
  let pageRes;
  for (let attempt = 1; ; attempt += 1) {
    try {
      pageRes = await routedFetch(INPUT, { headers: { 'User-Agent': UA, redirect: 'follow' } });
      break;
    } catch (error) {
      L('PAGE-ERR', attempt, describeError(error));
      if (attempt >= 2) {
        emit({ error: `page fetch failed for ${u.host}${u.pathname} (${proxyDispatcher ? 'proxy' : 'direct'}): ${describeError(error)}` }, 1);
        return;
      }
      await sleep(1000);
    }
  }
  const html = await pageRes.text();
  finalUrl = pageRes.url || INPUT;
  const base = new URL(finalUrl);
  urls.href = finalUrl; urls.pathname = base.pathname; urls.search = base.search;
  urls.origin = base.origin; urls.host = base.host; urls.hostname = base.hostname;
  L('PAGE', pageRes.status, finalUrl, 'len', html.length);

  // Inline scripts only: the external ones (hls.js, /plum.js) are stubbed above.
  const scripts = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi)]
    .map((m, index) => ({ attrs: m[1], body: m[2], index }))
    .filter(s => !/\bsrc\s*=/i.test(s.attrs) && s.body && s.body.trim().length > 0);
  L('SCRIPTS', scripts.map(s => s.body.length));

  for (const s of scripts) {
    // The ~280KB build stalls the vm in Node (it never returns control) and is
    // not needed: the player that fetches the payload is the ~140KB one.
    if (s.body.length > 200000) { L('SKIP big inline', s.index, s.body.length); continue; }
    try {
      new vm.Script(s.body, { filename: `inline-${s.index}.js` }).runInContext(sandbox, { timeout: 8000 });
      L('RAN inline', s.index, s.body.length);
    } catch (error) {
      L('INLINE-ERR', s.index, describeError(error).slice(0, 200));
    }
  }

  // Wait for Hls.loadSource (priority); a bare .m3u8 fetch gets a short grace
  // window first so a late loadSource() still wins.
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (hlsUrl) break;
    if (fallbackUrl) { await sleep(1500); break; }
    await sleep(200);
  }
  await verifyAndFinish(hlsUrl || fallbackUrl);
})().catch(error => emit({ error: describeError(error) }, 1));
