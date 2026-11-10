#!/usr/bin/env node
// Headless VidFast resolver. It runs the site's player bundle inside a Node VM
// (with a minimal React runtime) and prints the unlocked media URL as JSON.

import fs from "node:fs";
import vm from "node:vm";
import { webcrypto } from "node:crypto";

const inputUrl = process.argv[2];
const debug = process.env.VIDFAST_DEBUG === "1";
const writeOut = value => fs.writeSync(1, value + "\n");
const log = (...args) => {
  if (debug) fs.writeSync(2, "[vidfast] " + args.map(item => typeof item === "string" ? item : String(item)).join(" ") + "\n");
};

// undici reports network failures as "fetch failed" and hides the real reason
// (DNS, connect, TLS, proxy) in error.cause. Surface the whole chain.
function describeError(error) {
  const parts = [];
  let current = error;
  for (let depth = 0; current && depth < 4; depth += 1) {
    const text = typeof current === "string" ? current : current.message || String(current);
    const code = typeof current === "object" ? current.code : undefined;
    const label = code && !text.includes(code) ? `${code}: ${text}` : text;
    if (label && !parts.includes(label)) parts.push(label);
    current = typeof current === "object" ? current.cause : undefined;
  }
  return parts.join(" <- ") || String(error);
}

if (!inputUrl) {
  writeOut(JSON.stringify({ error: "usage: vidfast_runner.mjs <url>" }));
  process.exit(2);
}

const pageUrl = new URL(inputUrl).href;
const pageOrigin = new URL(pageUrl).origin;
const STREAM_PROBE_TIMEOUT_MS = 10000;
const minimumStreamHeight = Number(process.env.VIDFAST_MIN_HEIGHT || 0);
const userAgent =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36";
// Node's built-in fetch rejects dispatchers built by a differently-versioned
// undici (UND_ERR_INVALID_ARG: invalid onRequestStart method), so when a proxy
// dispatcher is active, fetch must come from the same undici module.
let nativeFetch = globalThis.fetch.bind(globalThis);
const nativeCrypto = globalThis.crypto ?? webcrypto;
const cookies = new Map();
let proxyDispatcher = null;
let undiciModule = null;

// Node 18 provides Blob but not the browser-compatible File global.
const NativeFile = globalThis.File ?? class File extends Blob {
  constructor(bits, name, options = {}) {
    super(bits, options);
    this.name = String(name);
    this.lastModified = Number(options.lastModified ?? Date.now());
  }

  get [Symbol.toStringTag]() {
    return "File";
  }
};

if (process.env.VIDFAST_PROXY && /^https?:\/\//i.test(process.env.VIDFAST_PROXY)) {
  try {
    const undici = await import("undici");
    undiciModule = undici;
    proxyDispatcher = new undici.ProxyAgent(process.env.VIDFAST_PROXY);
    nativeFetch = undici.fetch.bind(undici);
  } catch (error) {
    fs.writeSync(2, "[vidfast] HTTP proxy unavailable, continuing direct: " + describeError(error) + "\n");
  }
}

function fetchOptions(options = {}) {
  return proxyDispatcher ? { ...options, dispatcher: proxyDispatcher } : options;
}

function storeCookies(response) {
  const values = response.headers.getSetCookie?.() || [];
  const raw = values.length ? values.join(",") : response.headers.get("set-cookie") || "";
  for (const item of raw.split(/,(?=\s*[^;,]+=[^;,]+)/)) {
    const first = item.split(";", 1)[0].trim();
    const index = first.indexOf("=");
    if (index > 0) cookies.set(first.slice(0, index), first.slice(index + 1));
  }
}

function cookieHeader() {
  return [...cookies.entries()].map(([key, value]) => `${key}=${value}`).join("; ");
}

function absoluteUrl(value, base = pageUrl) {
  return new URL(String(value), base).href;
}

function mergedHeaders(initHeaders = {}, referer = pageUrl) {
  const headers = new Headers(initHeaders);
  if (!headers.has("user-agent")) headers.set("user-agent", userAgent);
  headers.set("referer", referer);
  headers.set("origin", pageOrigin);
  const cookie = cookieHeader();
  if (cookie) headers.set("cookie", cookie);
  return headers;
}

async function fetchPage() {
  const response = await nativeFetch(pageUrl, fetchOptions({
    headers: mergedHeaders({
      accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "accept-language": "en-US,en;q=0.9",
      "upgrade-insecure-requests": "1",
    }),
  }));
  storeCookies(response);
  if (!response.ok) throw new Error(`VidFast page returned HTTP ${response.status}`);
  return response.text();
}

function parseProps(html) {
  const tokenMatch = html.match(/\\"en\\":\\"([^\\"]+)\\"/);
  if (!tokenMatch) throw new Error("VidFast session token not found");
  const start = html.indexOf(tokenMatch[0]);
  const chunk = html.slice(start, start + 2500);
  const end = chunk.match(/\\"server\\":(?:\\"[^\\"]*\\"|null)\}/);
  if (!end) throw new Error("VidFast player payload is incomplete");
  const raw = chunk.slice(0, end.index + end[0].length)
    .replace(/\\"/g, '"')
    .replace(/"\$undefined"/g, "null");
  try {
    return JSON.parse(`{${raw.slice(0, -1)}}`);
  } catch (error) {
    throw new Error(`VidFast player payload is invalid: ${error.message}`);
  }
}

function scriptUrls(html) {
  const urls = [];
  const seen = new Set();
  const add = raw => {
    const src = absoluteUrl(raw);
    if (!src.includes("/_next/static/chunks/") || !src.endsWith(".js") || seen.has(src)) return;
    seen.add(src);
    urls.push(src);
  };
  for (const match of html.matchAll(/<script[^>]+src="([^"]+)"/gi)) add(match[1]);
  // The player chunk is lazy: the page only lists it in the flight payload.
  for (const match of html.matchAll(/(?:\/_next)?\/static\/chunks\/[^"'\s\\]+?\.js/g)) add(match[0]);
  return urls;
}

// Small DOM implementation.  The player bundle only touches a few browser
// globals; everything UI-related is stubbed.
const fauxParent = {
  tagName: "DIV", style: {}, dataset: {}, children: [], parentNode: null, parentElement: null,
  addEventListener() {}, removeEventListener() {}, appendChild(child) { return child; },
  removeChild(child) { return child; }, querySelector: () => null, querySelectorAll: () => [],
  classList: { add() {}, remove() {}, contains() { return false; } },
};

function element(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(), style: {}, dataset: {}, children: [],
    parentNode: null, parentElement: null, _attrs: {}, _listeners: {},
    _id: "", _text: "", _html: "", href: "", paused: true,
    currentTime: 0, duration: 0, volume: 1, muted: false, playbackRate: 1,
    textTracks: [], clientWidth: 1920, clientHeight: 1080,
  };
  let source = "";
  Object.defineProperty(node, "src", {
    get: () => source,
    set: value => {
      source = String(value ?? "");
      if (node.tagName === "VIDEO" || node.tagName === "AUDIO") (context.__vidfastSources ||= []).push(source);
    },
  });
  Object.defineProperty(node, "id", { get: () => node._id, set: value => { node._id = String(value); } });
  Object.defineProperty(node, "textContent", { get: () => node._text, set: value => { node._text = String(value ?? ""); } });
  Object.defineProperty(node, "innerHTML", { get: () => node._html, set: value => { node._html = String(value ?? ""); } });
  node.setAttribute = (key, value) => {
    node._attrs[key] = String(value);
    if (key === "id") node._id = String(value);
    if (key === "src" && (node.tagName === "VIDEO" || node.tagName === "AUDIO")) (context.__vidfastSources ||= []).push(String(value));
  };
  node.getAttribute = key => node._attrs[key] ?? (key === "id" ? node._id : null);
  node.removeAttribute = key => { delete node._attrs[key]; };
  node.hasAttribute = key => key in node._attrs;
  node.appendChild = child => { if (child) { node.children.push(child); child.parentNode = node; child.parentElement = node; } return child; };
  node.removeChild = child => { const i = node.children.indexOf(child); if (i >= 0) node.children.splice(i, 1); return child; };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  node.insertBefore = child => node.appendChild(child);
  node.replaceChild = (child, oldChild) => { const i = node.children.indexOf(oldChild); if (i >= 0) node.children[i] = child; return oldChild; };
  node.insertAdjacentHTML = (_, markup) => { node._html += String(markup ?? ""); };
  node.cloneNode = () => element(tag);
  node.querySelector = () => null;
  node.querySelectorAll = () => [];
  node.getElementsByTagName = () => [];
  node.getElementsByClassName = () => [];
  node.contains = () => false;
  node.matches = () => false;
  node.closest = () => null;
  node.scrollIntoView = () => {};
  node.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1920, bottom: 1080, width: 1920, height: 1080 });
  node.classList = { values: new Set(), add(value) { this.values.add(value); }, remove(value) { this.values.delete(value); }, contains(value) { return this.values.has(value); }, toggle(value) { if (this.values.has(value)) this.values.delete(value); else this.values.add(value); } };
  node.addEventListener = (name, callback) => { (node._listeners[name] ||= []).push(callback); };
  node.removeEventListener = () => {};
  node.dispatchEvent = () => true;
  node.click = () => {};
  node.focus = () => {};
  node.blur = () => {};
  node.play = async () => { node.paused = false; };
  node.pause = () => { node.paused = true; };
  node.load = () => {};
  node.append = (...items) => items.forEach(item => node.appendChild(item));
  node.parentNode = fauxParent;
  node.parentElement = fauxParent;
  return node;
}

const body = element("body");
const head = element("head");
const htmlElement = element("html");
const ids = new Map();
const location = {
  href: pageUrl, origin: pageOrigin, protocol: new URL(pageUrl).protocol,
  host: new URL(pageUrl).host, hostname: new URL(pageUrl).hostname,
  pathname: new URL(pageUrl).pathname, search: new URL(pageUrl).search,
  hash: new URL(pageUrl).hash, reload() {}, replace() {}, assign() {},
  toString: () => pageUrl,
};
const document = {
  body, head, documentElement: htmlElement, readyState: "complete", location,
  createElement: tag => element(tag), createTextNode: text => { const node = element("#text"); node.textContent = text; return node; },
  getElementById: id => { if (!ids.has(id)) { const node = element(); node.id = id; ids.set(id, node); } return ids.get(id); },
  querySelector: selector => selector === "body" ? body : selector === "head" ? head : (ids.get(selector) ?? (ids.set(selector, element()), ids.get(selector))),
  querySelectorAll: () => [], getElementsByTagName: () => [], getElementsByClassName: () => [],
  addEventListener() {}, removeEventListener() {}, cookie: "",
};
const localValues = new Map();
const localStorage = { getItem: key => localValues.get(key) ?? null, setItem: (key, value) => localValues.set(key, String(value)), removeItem: key => localValues.delete(key), clear: () => localValues.clear() };
const sessionStorage = { ...localStorage };
const navigator = {
  userAgent, platform: "Linux x86_64", language: "en-US", languages: ["en-US", "en"],
  vendor: "Google Inc.", plugins: { length: 5, namedItem: () => ({}) }, mimeTypes: [],
  webdriver: false, maxTouchPoints: 0, hardwareConcurrency: 8,
  storage: { estimate: async () => ({ quota: 2147483648, usage: 0 }) },
};

// The bundle runs its own console.table-based anti-bot probe; keep console
// callable but silent.
function nativeLikeConsole() {
  const base = console;
  return new Proxy(base, { get(target, key) { return ["log", "table", "clear"].includes(key) ? () => {} : target[key]; } });
}

const context = {
  console: nativeLikeConsole(), setTimeout, clearTimeout, setInterval, clearInterval,
  setImmediate, clearImmediate, queueMicrotask, structuredClone,
  Buffer, URL, URLSearchParams, TextEncoder, TextDecoder,
  atob: value => Buffer.from(value, "base64").toString("binary"),
  btoa: value => Buffer.from(value, "binary").toString("base64"),
  AbortController, AbortSignal,
  // Branded classes must come from the same module as fetch, or the player's
  // instanceof checks fail when a proxy dispatcher switches us to undici.fetch.
  Request: undiciModule ? undiciModule.Request : Request,
  Response: undiciModule ? undiciModule.Response : Response,
  Headers: undiciModule ? undiciModule.Headers : Headers,
  FormData: undiciModule ? undiciModule.FormData : FormData,
  ReadableStream, Blob, File: NativeFile, TextEncoderStream, TextDecoderStream,
  WebAssembly, crypto: nativeCrypto, navigator, location, document,
  localStorage, sessionStorage, window: null, self: null, globalThis: null, global: null,
  performance: globalThis.performance,
  history: { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {}, state: null, length: 1 },
  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24 },
  innerWidth: 1920, innerHeight: 1080, outerWidth: 1920, outerHeight: 1080,
  devicePixelRatio: 1, pageXOffset: 0, pageYOffset: 0, scrollX: 0, scrollY: 0,
  top: null, parent: null, frames: null, opener: null, closed: false,
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true, postMessage() {},
  requestAnimationFrame: callback => setTimeout(callback, 0), cancelAnimationFrame: clearTimeout,
  requestIdleCallback: callback => setTimeout(() => callback({ didTimeout: false, timeRemaining: () => 50 }), 0),
  cancelIdleCallback: clearTimeout, matchMedia: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => "", getPropertyPriority: () => "", cssText: "" }),
  confirm: () => true, alert: () => {}, prompt: () => null, focus() {}, blur() {},
  Window: function Window() {}, Document: function Document() {}, HTMLDocument: function HTMLDocument() {},
  HTMLElement: function HTMLElement() {}, Node: function Node() {}, Element: function Element() {},
  HTMLDivElement: function HTMLDivElement() {},
  Image: function Image() { return element("img"); }, Audio: function Audio() { return element("audio"); },
  Worker: function Worker() { return { postMessage() {}, terminate() {}, addEventListener() {}, removeEventListener() {} }; },
  MessageChannel: function MessageChannel() { return { port1: { postMessage() {}, start() {}, addEventListener() {} }, port2: { postMessage() {}, start() {}, addEventListener() {} } }; },
  MutationObserver: function MutationObserver() { return { observe() {}, disconnect() {} }; },
  MediaSource: class {}, BroadcastChannel: class { postMessage() {} close() {} addEventListener() {} },
  WebSocket: class { send() {} close() {} addEventListener() {} },
  XMLHttpRequest: class {
    open(method, url) {
      const target = absoluteUrl(url);
      if (/^https?:/i.test(target) && !target.startsWith(pageOrigin)) (context.__vidfastSources ||= []).push(target);
    }
    send() {} setRequestHeader() {} abort() {} addEventListener() {} removeEventListener() {}
    getResponseHeader() { return null; } getAllResponseHeaders() { return ""; }
  },
  __vidfastSources: [],
};
context.window = context; context.self = context; context.globalThis = context; context.global = context;
context.top = context; context.parent = context;
vm.createContext(context);

async function playerFetch(input, init = {}) {
  const url = absoluteUrl(input);
  const headers = mergedHeaders(init.headers, pageUrl);
  log("fetch", init.method || "GET", url);
  const response = await nativeFetch(url, fetchOptions({ ...init, headers }));
  storeCookies(response);
  return response;
}
context.fetch = playerFetch;

async function probeStreamManifest(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), STREAM_PROBE_TIMEOUT_MS);
  try {
    const response = await nativeFetch(url, fetchOptions({
      headers: mergedHeaders({
        accept: "application/vnd.apple.mpegurl,*/*",
        "accept-encoding": "gzip, identity;q=1, *;q=0",
        range: "bytes=0-",
      }, `${pageOrigin}/`),
      signal: controller.signal,
    }));
    storeCookies(response);
    if (!response.ok) throw new Error(`stream HTTP ${response.status}`);
    const body = await response.text();
    if (!body.includes("#EXTM3U")) throw new Error("stream response is not an HLS manifest");
    return body;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("stream manifest probe timed out");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

const modules = {};
const cache = {};
function defineExports(exports, map) {
  for (const [key, getter] of Object.entries(map)) Object.defineProperty(exports, key, { enumerable: true, get: getter });
}
function webpackRequire(id) {
  if (cache[id]) return cache[id].exports;
  if (!modules[id]) throw new Error(`missing webpack module ${id}`);
  const mod = { exports: {} };
  cache[id] = mod;
  const req = Object.assign(requested => webpackRequire(requested), {
    d: defineExports,
    r: exports => {
      Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
      exports.__esModule = true;
    },
    n: module => (module && module.__esModule ? () => module.default : () => module),
    o: (obj, key) => Object.prototype.hasOwnProperty.call(obj, key),
    bind: (target, ...args) => target.bind(...args), g: context,
  });
  modules[id](mod, mod.exports, req);
  return mod.exports;
}

function loadChunk(code, filename) {
  const queue = [];
  context.webpackChunk_N_E = queue;
  try {
    vm.runInContext(code, context, { filename, timeout: 120000 });
  } catch (error) {
    throw new Error(`${filename}: ${error.message} (line ${error.lineNumber || "?"}, column ${error.columnNumber || "?"})`);
  }
  for (const chunk of queue) {
    if (chunk?.[1]) Object.assign(modules, chunk[1]);
  }
}

// Minimal React runtime: enough of the hook contract to mount the player
// component and let its own effects drive the decoder.
function createRuntime(props) {
  const cells = [];
  let index = 0;
  let toRun = [];
  let scheduled = false;
  let running = false;
  let mountError = null;
  let component = null;

  const same = (a, b) => {
    if (!a || !b || a.length !== b.length) return !a && !b;
    return a.every((value, i) => Object.is(value, b[i]));
  };

  const jsx = (type, jsxProps, ...children) => {
    const ref = jsxProps && jsxProps.ref;
    if (ref && typeof ref === "object" && ref.current == null) ref.current = element(type === "video" ? "video" : "div");
    else if (typeof ref === "function") { try { ref(element("div")); } catch {} }
    return { type, props: jsxProps, children };
  };

  const hooks = {
    useState(initial) {
      const cell = cells[index++] || (cells[index - 1] = { kind: "state", value: typeof initial === "function" ? initial() : initial });
      const setter = next => {
        const value = typeof next === "function" ? next(cell.value) : next;
        if (Object.is(value, cell.value)) return value;
        cell.value = value;
        schedule();
        return value;
      };
      return [cell.value, setter];
    },
    useReducer(reducer, initial) {
      const cell = cells[index++] || (cells[index - 1] = { kind: "state", value: initial });
      const dispatch = action => { cell.value = reducer(cell.value, action); schedule(); };
      return [cell.value, dispatch];
    },
    useRef(initial) {
      const cell = cells[index++] || (cells[index - 1] = { kind: "ref", value: { current: initial } });
      return cell.value;
    },
    useMemo(fn, deps) {
      const cell = cells[index++] || (cells[index - 1] = { kind: "memo" });
      if (!same(cell.deps, deps)) { cell.value = fn(); cell.deps = deps; }
      return cell.value;
    },
    useCallback(fn, deps) {
      const cell = cells[index++] || (cells[index - 1] = { kind: "cb" });
      if (!same(cell.deps, deps)) { cell.value = fn; cell.deps = deps; }
      return cell.value;
    },
    useEffect(fn, deps) { queueEffect(fn, deps); },
    useLayoutEffect(fn, deps) { queueEffect(fn, deps); },
    useInsertionEffect(fn, deps) { queueEffect(fn, deps); },
    useContext() { return null; },
    useId() { return "vidfast"; },
    useDebugValue() {},
    useImperativeHandle() {},
    useTransition() { return [false, fn => fn()]; },
    useDeferredValue(value) { return value; },
    useSyncExternalStore(subscribe, getSnapshot) { return getSnapshot(); },
  };

  function queueEffect(fn, deps) {
    index++;
    const cell = cells[index - 1] || (cells[index - 1] = { kind: "effect" });
    if (!same(cell.deps, deps)) { cell.deps = deps; cell.fn = fn; toRun.push(cell); return; }
    cell.fn = fn;
  }

  function schedule() {
    scheduled = true;
    if (running) return;
    running = true;
    try {
      let guard = 0;
      while (scheduled && guard++ < 400) {
        scheduled = false;
        index = 0;
        toRun = [];
        const children = component({ ...props, React: reactModule, jsx, jsxs: jsx, jsxDEV: jsx });
        void children;
        for (const cell of toRun) {
          if (cell.cleanup) { try { cell.cleanup(); } catch {} }
          try {
            const cleanup = cell.fn();
            cell.cleanup = typeof cleanup === "function" ? cleanup : undefined;
          } catch (error) {
            log("effect error:", error?.message || String(error));
            if (debug && error?.stack) log(error.stack.split("\n").slice(0, 4).join(" | "));
          }
        }
      }
    } catch (error) {
      mountError = mountError || error;
      log("render error:", error?.message || String(error));
    } finally {
      running = false;
    }
  }

  const reactModule = new Proxy(function ReactStub() {}, {
    get: (_, key) => {
      if (key === "__esModule") return true;
      if (key === "default") return reactModule;
      if (key === "Fragment") return "Fragment";
      if (key === "jsx" || key === "jsxs" || key === "jsxDEV" || key === "createElement" || key === "cloneElement") return jsx;
      if (key === "forwardRef" || key === "memo") return fn => fn;
      if (key === "createContext") return () => ({ Provider: "Provider", Consumer: "Consumer" });
      if (Object.prototype.hasOwnProperty.call(hooks, key)) return hooks[key];
      return () => ({});
    },
  });

  return {
    cells,
    reactModule,
    mount(value) { component = value; },
    schedule,
    get error() { return mountError; },
  };
}

let runtime = null;

const moduleStubs = {
  8288: { useRouter: () => ({ push() {}, replace() {}, prefetch() {} }), usePathname: () => location.pathname },
  8613: {}, 6497: {}, 4352: {}, 3396: {}, 6368: {}, 5216: {},
  153: { hb: () => ({ pause() {}, start() {}, reset() {} }) },
  2421: { f: async () => ({ cues: [] }) },
};

function installStubs(reactModule) {
  modules[5376] = mod => { mod.exports = { Buffer }; };
  modules[7358] = mod => { mod.exports = { env: {}, versions: { chrome: "152.0.0.0" }, browser: true }; };
  modules[5155] = (mod, exports, req) => {
    mod.exports = reactModule;
    if (req?.d) req.d(exports, { default: () => reactModule, __esModule: () => true });
  };
  modules[63] = modules[5155];
  modules[2115] = modules[5155];
  for (const [id, value] of Object.entries(moduleStubs)) modules[id] = (mod, exports, req) => {
    mod.exports = value;
    if (req?.d) req.d(exports, { default: () => value, __esModule: () => true });
  };
}

async function loadPlayer(html, reactModule) {
  const urls = scriptUrls(html);
  log("loading", urls.length, "chunks");
  for (const url of urls) {
    const response = await nativeFetch(url, fetchOptions({ headers: mergedHeaders({}, pageUrl) }));
    if (!response.ok) continue;
    const code = await response.text();
    loadChunk(code, url);
  }
  installStubs(reactModule);
  // The page renders one lazy Player.js?playerVariant=N; its chunk and module
  // ids are listed in the page module, indexed by the variantIndex prop.
  const variant = Number((html.match(/variantIndex\\?":(\d+)/) || [0, 0])[1]);
  let playerId = null;
  for (const factory of Object.values(modules)) {
    const source = String(factory);
    if (!source.includes("playerVariant=")) continue;
    const ids = [...source.matchAll(/\.bind\(\w+,(\d+)\)/g)].map(match => Number(match[1]));
    if (ids.length) { playerId = ids[variant] ?? ids[0]; break; }
  }
  if (playerId === null) throw new Error("VidFast player bundle not found");
  log("player module", playerId, "variant", variant);
  return webpackRequire(playerId);
}

async function resolve() {
  const html = await fetchPage();
  const props = parseProps(html);
  runtime = createRuntime(props);
  const playerModule = await loadPlayer(html, runtime.reactModule);
  if (typeof playerModule.default !== "function") throw new Error("VidFast player component not found");
  runtime.mount(playerModule.default);
  runtime.schedule();

  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const found = currentStreamUrl();
    if (found) return finish(found);
    await new Promise(resolvePromise => setTimeout(resolvePromise, 250));
  }
  throw new Error(runtime.error ? `VidFast player failed: ${runtime.error.message}` : "VidFast did not resolve a stream URL");
}

function currentStreamUrl() {
  for (const source of context.__vidfastSources || []) {
    if (!/^https?:\/\//i.test(source)) continue;
    if (source.startsWith(pageOrigin)) continue;
    return source;
  }
  for (const cell of runtime?.cells || []) {
    // The player stores the resolved source object ({url,...}) in a ref.
    const current = cell.value?.current;
    const url = typeof current === "string" ? current : current && typeof current.url === "string" ? current.url : "";
    if (!/^https?:\/\//i.test(url) || url.startsWith(pageOrigin)) continue;
    if (/\.(png|jpg|jpeg|gif|svg|webp|woff2?|css|js)(\?|$)/i.test(url)) continue;
    return url;
  }
  return "";
}

async function finish(streamUrl) {
  const manifest = await probeStreamManifest(streamUrl);
  const resolutions = [...manifest.matchAll(/RESOLUTION=(\d+)x(\d+)/gi)]
    .map(match => ({ w: Number(match[1]), h: Number(match[2]) }))
    .filter(item => Number.isFinite(item.w) && Number.isFinite(item.h));
  const maxResolution = resolutions.length
    ? Math.max(...resolutions.map(item => item.w >= 3800 ? 2160 : item.h))
    : 0;
  if (minimumStreamHeight > 0) {
    const is4k = minimumStreamHeight >= 2160;
    const hasTarget = resolutions.some(item => is4k ? (item.w >= 3800 || item.h >= 2140) : (item.h >= minimumStreamHeight - 16));
    if (!hasTarget) throw new Error(`no HLS variant near ${minimumStreamHeight}p (highest is ${maxResolution}p)`);
  }
  return {
    url: streamUrl,
    headers: { "User-Agent": userAgent, Referer: pageUrl, Origin: pageOrigin },
    server: "VidFast",
  };
}

let exitCode = 0;
try {
  writeOut(JSON.stringify(await resolve()));
} catch (error) {
  if (debug && error?.stack) log(error.stack);
  writeOut(JSON.stringify({ error: describeError(error) }));
  exitCode = 1;
} finally {
  if (proxyDispatcher) {
    try { await proxyDispatcher.close(); } catch {}
  }
}

// The player bundle keeps timers/handlers alive; exit explicitly. All output
// above is written synchronously so nothing is lost.
process.exit(exitCode);
