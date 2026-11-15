#!/usr/bin/env node
// ppv_bridge.mjs - local HTTP bridge for PPV playback.
//
// The CDN edge (*.indianservers.st) 403s every TLS ClientHello outside its
// allowlist: aiohttp, curl and curl_cffi are all refused, only Node's stack
// with CDN_CIPHERS gets a 200. Playlists and segments are therefore fetched
// here and served to the Python side over http://127.0.0.1:<port>.
//
// Usage: node scripts/ppv_bridge.mjs        (prints "READY <port>" on stdout)
//   GET /health                      -> ok
//   GET /m?u=<encoded>&r=<encoded>   -> playlist, URLs rewritten to this bridge
//   GET /s?u=<encoded>&r=<encoded>   -> segment/key body, streamed

import http from 'node:http';
import https from 'node:https';

const DEBUG = process.env.PPV_DEBUG === '1';
function L(...a) { if (DEBUG) console.error('[ppv-bridge]', ...a); }

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36';
const TIMEOUT_MS = 25000;
const MAX_REDIRECTS = 5;
const MAX_MANIFEST = 5 * 1024 * 1024;
const REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);

// Same list as ppv_runner.mjs: the CDN edge refuses any other ClientHello.
const CDN_CIPHERS = [
  'ECDHE-ECDSA-AES256-GCM-SHA384', 'ECDHE-RSA-AES256-GCM-SHA384',
  'ECDHE-ECDSA-AES128-GCM-SHA256', 'ECDHE-RSA-AES128-GCM-SHA256',
  'ECDHE-ECDSA-CHACHA20-POLY1305', 'ECDHE-RSA-CHACHA20-POLY1305',
  'ECDHE-ECDSA-AES256-SHA384', 'ECDHE-RSA-AES256-SHA384',
  'ECDHE-ECDSA-AES128-SHA256', 'ECDHE-RSA-AES128-SHA256',
  'DHE-RSA-AES256-GCM-SHA384', 'DHE-RSA-AES128-GCM-SHA256',
  'DHE-RSA-AES256-SHA256', 'DHE-RSA-AES128-SHA256',
].join(':');

let port = 0;

function describeError(error) {
  return (error && error.message) || String(error);
}

// GET an upstream URL and resolve with the raw response (redirects followed).
function request(target, referer, extraHeaders = {}, redirects = 0) {
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(target);
    } catch {
      return reject(new Error('invalid url'));
    }
    const mod = url.protocol === 'https:' ? https : url.protocol === 'http:' ? http : null;
    if (!mod) return reject(new Error(`unsupported protocol ${url.protocol}`));

    const headers = { 'User-Agent': UA, 'Accept-Encoding': 'identity', ...extraHeaders };
    if (referer) headers.Referer = referer;

    const req = mod.get(url, { headers, ciphers: CDN_CIPHERS }, (res) => {
      const status = res.statusCode || 0;
      if (REDIRECT_CODES.has(status) && res.headers.location) {
        res.resume();
        if (redirects >= MAX_REDIRECTS) return reject(new Error('too many redirects'));
        let next;
        try {
          next = new URL(res.headers.location, url).href;
        } catch {
          return reject(new Error('invalid redirect'));
        }
        return resolve(request(next, referer, extraHeaders, redirects + 1));
      }
      resolve(res);
    });
    req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error('upstream timeout')));
    req.on('error', reject);
  });
}

function local(path, url, referer) {
  const q = `u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}`;
  return `http://127.0.0.1:${port}/${path}?${q}`;
}

function rewriteManifest(text, baseUrl, referer) {
  const isMaster = text.includes('#EXT-X-STREAM-INF');
  return text.split(/\r?\n/).map((line) => {
    const value = line.trim();
    if (!value) return line;
    if (value.startsWith('#')) {
      if (!value.includes('URI="')) return line;
      return line.replace(/URI="([^"]*)"/g, (match, uri) => {
        try {
          return `URI="${local('s', new URL(uri, baseUrl).href, referer)}"`;
        } catch {
          return match;
        }
      });
    }
    try {
      return local(isMaster ? 'm' : 's', new URL(value, baseUrl).href, referer);
    } catch {
      return line;
    }
  }).join('\n');
}

function sendError(res, status, text) {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(`${text}\n`);
}

async function handleManifest(res, target, referer) {
  const upstream = await request(target, referer);
  if (upstream.statusCode !== 200) {
    upstream.resume();
    return sendError(res, 502, `upstream HTTP ${upstream.statusCode}`);
  }
  const chunks = [];
  let size = 0;
  await new Promise((resolve, reject) => {
    upstream.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_MANIFEST) {
        upstream.destroy();
        reject(new Error('manifest too large'));
        return;
      }
      chunks.push(chunk);
    });
    upstream.on('end', resolve);
    upstream.on('error', reject);
  });
  const body = rewriteManifest(Buffer.concat(chunks).toString('utf8'), target, referer);
  res.writeHead(200, {
    'Content-Type': 'application/vnd.apple.mpegurl',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

async function handleSegment(req, res, target, referer) {
  const extra = req.headers.range ? { Range: req.headers.range } : {};
  const upstream = await request(target, referer, extra);

  const headers = { 'Cache-Control': 'no-store' };
  for (const name of ['content-type', 'content-length', 'content-range', 'accept-ranges']) {
    if (upstream.headers[name]) headers[name] = upstream.headers[name];
  }
  res.writeHead(upstream.statusCode || 502, headers);
  upstream.pipe(res);
  upstream.on('error', () => res.destroy());
}

const server = http.createServer((req, res) => {
  let parsed;
  try {
    parsed = new URL(req.url || '/', 'http://127.0.0.1');
  } catch {
    return sendError(res, 400, 'bad request');
  }
  if (parsed.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    return res.end('ok');
  }
  if (parsed.pathname !== '/m' && parsed.pathname !== '/s') return sendError(res, 404, 'not found');

  const target = parsed.searchParams.get('u') || '';
  const referer = parsed.searchParams.get('r') || '';
  if (!/^https?:\/\//i.test(target)) return sendError(res, 400, 'missing or invalid url');

  L(parsed.pathname, target);
  const run = parsed.pathname === '/m'
    ? () => handleManifest(res, target, referer)
    : () => handleSegment(req, res, target, referer);
  Promise.resolve()
    .then(run)
    .catch((error) => {
      L(parsed.pathname, describeError(error));
      sendError(res, 502, describeError(error));
    });
});

server.on('error', (error) => {
  console.error('[ppv-bridge] listen failed:', describeError(error));
  process.exit(1);
});

server.listen(Number(process.env.PPV_BRIDGE_PORT) || 0, '127.0.0.1', () => {
  port = server.address().port;
  process.stdout.write(`READY ${port}\n`);
  L('listening on', port);
});
