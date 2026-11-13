#!/usr/bin/env node
// Headless Cinejoy stream resolver.
// Uses Node's native WebAssembly and webcrypto to call Cinejoy's gateway (api.wing.st).

import { webcrypto } from "node:crypto";

const input = process.argv[2];
const debug = process.env.CINEJOY_DEBUG === "1";
const log = (...args) => {
  if (debug) console.error("[cinejoy]", ...args);
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

if (!input) {
  console.log(JSON.stringify({ error: "usage: cinejoy_runner.mjs <url_or_json>" }));
  process.exit(2);
}

const API_URL = "https://api.wing.st";
const WASM_URL = `${API_URL}/crush.wasm`;
const BASE_URL = (() => {
  try {
    const u = new URL(input.startsWith("http") ? input : `https://${input}`);
    return /(^|\.)cinejoy\.[a-z]{2,}$/i.test(u.hostname) ? u.origin : "https://cinejoy.pk";
  } catch {
    return "https://cinejoy.pk";
  }
})();
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

let proxyDispatcher = null;
let fetchImpl = globalThis.fetch;

if (process.env.CINEJOY_PROXY && /^https?:\/\//i.test(process.env.CINEJOY_PROXY)) {
  try {
    const undici = await import("undici");
    proxyDispatcher = new undici.ProxyAgent(process.env.CINEJOY_PROXY);
    fetchImpl = undici.fetch.bind(undici);
  } catch (err) {
    console.error("[cinejoy] HTTP proxy unavailable, continuing direct:", describeError(err));
  }
}

function fetchOptions(options = {}) {
  return proxyDispatcher ? { ...options, dispatcher: proxyDispatcher } : options;
}

function parseTarget(arg) {
  const trimmed = arg.trim();
  if (trimmed.startsWith("{")) {
    try {
      const obj = JSON.parse(trimmed);
      return {
        type: obj.type || "movie",
        tmdbId: Number(obj.tmdbId || obj.id || obj.tmdb),
        season: obj.season ? Number(obj.season) : undefined,
        episode: obj.episode ? Number(obj.episode) : undefined,
      };
    } catch {}
  }

  if (/^\d+$/.test(trimmed)) {
    return { type: "movie", tmdbId: Number(trimmed) };
  }

  try {
    const url = new URL(trimmed.startsWith("http") ? trimmed : `https://${trimmed}`);
    const pathname = url.pathname.replace(/\/+$/, "");
    const parts = pathname.split("/").filter(Boolean);

    if (parts.includes("movie")) {
      const idPart = parts[parts.indexOf("movie") + 1] || "";
      const tmdbId = Number(idPart.split("-")[0]);
      return { type: "movie", tmdbId };
    }

    if (parts.includes("tv") || parts.includes("series")) {
      const key = parts.includes("tv") ? "tv" : "series";
      const idx = parts.indexOf(key);
      const idPart = parts[idx + 1] || "";
      const tmdbId = Number(idPart.split("-")[0]);
      const rawS = parts[idx + 2] || url.searchParams.get("s") || url.searchParams.get("season") || "1";
      const rawE = parts[idx + 3] || url.searchParams.get("e") || url.searchParams.get("episode") || "1";
      const s = Number(String(rawS).replace(/\D+/g, "")) || 1;
      const e = Number(String(rawE).replace(/\D+/g, "")) || 1;
      return { type: "tv", tmdbId, season: s, episode: e };
    }

    const numMatch = pathname.match(/\d+/);
    if (numMatch) {
      return { type: "movie", tmdbId: Number(numMatch[0]) };
    }
  } catch (err) {
    log("URL parse error:", err.message);
  }

  throw new Error(`Cannot parse Cinejoy target from input: ${arg}`);
}

let wasmExports = null;
async function getWasm() {
  if (wasmExports) return wasmExports;
  const res = await fetchImpl(WASM_URL, fetchOptions({
    headers: { "User-Agent": UA, Referer: `${BASE_URL}/` }
  }));
  if (!res.ok) throw new Error(`WASM fetch failed HTTP ${res.status}`);
  const bytes = await res.arrayBuffer();
  const { instance } = await WebAssembly.instantiate(bytes, {});
  wasmExports = instance.exports;
  return wasmExports;
}

async function sealRequest(payload) {
  const wasm = await getWasm();
  const encoder = new TextEncoder();
  const inputBytes = encoder.encode(JSON.stringify(payload));
  const keyMaterial = new Uint8Array(44);
  webcrypto.getRandomValues(keyMaterial);
  const inputPtr = wasm.alloc(inputBytes.length);
  const keyPtr = wasm.alloc(keyMaterial.length);
  const outputCapacity = inputBytes.length + 512;
  const outputPtr = wasm.alloc(outputCapacity);
  try {
    new Uint8Array(wasm.memory.buffer).set(inputBytes, inputPtr);
    new Uint8Array(wasm.memory.buffer).set(keyMaterial, keyPtr);
    const sealedLength = wasm.seal_request(
      inputPtr,
      inputBytes.length,
      keyPtr,
      keyMaterial.length,
      outputPtr,
      outputCapacity
    );
    if (!Number.isInteger(sealedLength) || sealedLength < 98 || sealedLength > outputCapacity) {
      throw new Error("Cinejoy request sealing failed");
    }
    const sealed = new Uint8Array(wasm.memory.buffer).slice(outputPtr, outputPtr + sealedLength);
    return {
      responseKey: sealed.slice(0, 32),
      keyId: sealed[32],
      ephemeralPublic: sealed.slice(33, 98),
      body: sealed.slice(98),
    };
  } finally {
    wasm.dealloc(inputPtr, inputBytes.length);
    wasm.dealloc(keyPtr, keyMaterial.length);
    wasm.dealloc(outputPtr, outputCapacity);
  }
}

async function openResponse(responseBytes, request) {
  if (responseBytes.length < 28) throw new Error("Cinejoy response too short");
  const encoder = new TextEncoder();
  const additionalData = new Uint8Array([
    ...encoder.encode("lumen-gate-v2"),
    0,
    2,
    request.keyId,
    ...request.ephemeralPublic,
  ]);
  const cryptoKey = await webcrypto.subtle.importKey(
    "raw",
    request.responseKey,
    "AES-GCM",
    false,
    ["decrypt"]
  );
  const plaintext = await webcrypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: responseBytes.slice(0, 12),
      additionalData,
      tagLength: 128,
    },
    cryptoKey,
    responseBytes.slice(12)
  );
  const result = JSON.parse(new TextDecoder().decode(plaintext));
  if (!result || typeof result.status !== "number" || !("data" in result)) {
    throw new Error("Invalid Cinejoy response format");
  }
  if (result.status < 200 || result.status >= 300) {
    throw new Error(`Cinejoy API error status HTTP ${result.status}`);
  }
  return result.data;
}

async function encryptedRequest(path, payload) {
  const request = await sealRequest({ path, payload });
  const res = await fetchImpl(`${API_URL}/g`, fetchOptions({
    method: "POST",
    headers: {
      "User-Agent": UA,
      Referer: `${BASE_URL}/`,
      Origin: BASE_URL,
      "Content-Type": "text/plain;charset=UTF-8",
    },
    body: request.body,
  }));
  const responseBytes = new Uint8Array(await res.arrayBuffer());
  if (!res.ok) throw new Error(`Cinejoy gateway HTTP ${res.status}`);
  return openResponse(responseBytes, request);
}

async function getServers() {
  const res = await fetchImpl(`${API_URL}/servers`, fetchOptions({
    headers: { "User-Agent": UA, Referer: `${BASE_URL}/`, Origin: BASE_URL },
  }));
  if (!res.ok) throw new Error(`Cinejoy servers HTTP ${res.status}`);
  const payload = await res.json();
  const servers = Array.isArray(payload) ? payload : payload?.servers;
  if (!Array.isArray(servers)) throw new Error("Invalid Cinejoy servers response");
  return servers.filter((s) => s?.name && s.status === "ok");
}

async function resolve() {
  const target = parseTarget(input);
  const require4k = process.env.CINEJOY_REQUIRE_4K === "1";
  log("Target:", target);

  const servers = await getServers();
  const with4k = servers.filter((s) => s["4k"] === true);
  const nebula = servers.filter((s) => s.name === "Nebula");
  const others = servers.filter((s) => !s["4k"] && s.name !== "Nebula");
  // Always seek 4K (highest resolution) first, fallback to 1080p unless require4k is set
  const candidateServers = require4k ? with4k : [...with4k, ...nebula, ...others];

  if (candidateServers.length === 0) {
    throw new Error(require4k ? "No active 4K Cinejoy server found" : "No active Cinejoy server found");
  }

  const isMovie = target.type === "movie";
  const requestPath = isMovie ? "movie" : "series";
  const requestPayload = isMovie
    ? { tmdb: target.tmdbId }
    : { tmdb: target.tmdbId, season: String(target.season || 1), episode: String(target.episode || 1) };

  let lastError = null;
  for (const server of candidateServers) {
    try {
      log("Trying server:", server.name);
      const data = await encryptedRequest(`/${server.name}/${requestPath}`, requestPayload);
      const entries = Array.isArray(data?.stream) ? data.stream : [];
      const primaryEntry = entries.find((e) => e?.playlist) || entries[0];
      const playlistUrl = primaryEntry?.playlist;
      // Same rule as the addon: some servers (e.g. Comet) hand out a raw .mkv
      // that is not HLS and is refused by the CDN, so only an m3u8 counts.
      if (!/^https?:\/\/[^\s]+\.m3u8(?:[?#].*)?$/i.test(String(playlistUrl || "").trim())) {
        log(`Server ${server.name} returned no HLS playlist, skipping...`);
        continue;
      }

      const strictHost = playlistUrl.includes("cheaptruckrepairs.cc");
      const check = await fetchImpl(playlistUrl, fetchOptions({
        headers: { "User-Agent": UA, Referer: `${BASE_URL}/`, Origin: BASE_URL },
        signal: AbortSignal.timeout(3000),
      })).catch(() => null);
      // Any host: a master answering 404/410 is a removed file, try the next
      // server. A timeout only disqualifies the host known to need the probe.
      if (check ? [404, 410].includes(check.status) || (strictHost && !check.ok) : strictHost) {
        log(`Server ${server.name} playlist unreachable (${check?.status}), skipping...`);
        lastError = new Error(`Cinejoy: ${server.name} playlist HTTP ${check?.status || "timeout"}`);
        continue;
      }
      if (strictHost) {
        const text = await check.text();
        const allSubs = [...text.matchAll(/https?:\/\/[^\s"']+/g)].map(m => m[0]);
        const videoSub = allSubs.find(u => u.includes("/video/")) || allSubs[0];
        if (videoSub) {
          const subRes = await fetchImpl(videoSub, fetchOptions({
            headers: { "User-Agent": UA, Referer: `${BASE_URL}/`, Origin: BASE_URL },
            signal: AbortSignal.timeout(3000),
          })).catch(() => null);
          if (!subRes || !subRes.ok) {
            log(`Server ${server.name} sub-manifest unreachable (${subRes?.status}), skipping...`);
            continue;
          }
          const subText = await subRes.text();
          const segMatches = [...subText.matchAll(/https?:\/\/[^\s"']+\.html/g)]
            .map(m => m[0])
            .filter(u => !u.includes("init"));
          if (segMatches.length > 0) {
            const testIndices = [
              0,
              Math.min(5, segMatches.length - 1),
              Math.floor(segMatches.length / 2),
            ].filter((idx, pos, arr) => arr.indexOf(idx) === pos && idx < segMatches.length);
            const segChecks = await Promise.all(
              testIndices.map(i =>
                fetchImpl(segMatches[i], fetchOptions({
                  method: "HEAD",
                  headers: { "User-Agent": UA, Referer: `${BASE_URL}/`, Origin: BASE_URL },
                  signal: AbortSignal.timeout(2000),
                })).catch(() => null)
              )
            );
            if (segChecks.some(r => !r || !r.ok)) {
              log(`Server ${server.name} segment probe failed, skipping...`);
              continue;
            }
          }
        }
      }

      log("Selected working server:", server.name);
      return {
        url: playlistUrl,
        headers: {
          "User-Agent": UA,
          Referer: `${BASE_URL}/`,
          Origin: BASE_URL,
        },
        server: server.name,
        target,
      };
    } catch (err) {
      log(`Server ${server.name} error:`, err.message);
      lastError = err;
    }
  }

  throw lastError || new Error("All Cinejoy servers failed to return a valid stream");
}

try {
  const result = await resolve();
  console.log(JSON.stringify(result));
} catch (err) {
  if (debug && err?.stack) console.error(err.stack);
  console.log(JSON.stringify({ error: describeError(err) }));
  process.exitCode = 1;
} finally {
  if (proxyDispatcher) {
    try { await proxyDispatcher.close(); } catch {}
  }
}
process.exit(process.exitCode || 0);
