# AGENTS.md — Node runner contract (`scripts/*.mjs`)

Rules for every Node helper invoked by Python through
`extractors/node_runner.py::run_node_runner`. They exist because the same
mistakes kept coming back (TLS fingerprints, proxies, loopback, stdout).
The extractor-side contract lives in [`../extractors/AGENTS.md`](../extractors/AGENTS.md).

## 1. Invocation contract

- Python spawns: `node <runner.mjs> <args...>` (no shell). Keep the runner
  self-contained: no `package.json` needed unless a local `.js` uses ESM syntax.
- **stdout is data**: print exactly one JSON object (the result or
  `{"error": "..."}`), nothing else. All logs go to **stderr**.
- Exit `0` on success, `1` on failure. Never leave the process hanging:
  always install a global watchdog (e.g. 25–30 s) that emits an error and exits.
- Optional debug: `MYRUNNER_DEBUG=1` enables `log()` on stderr. Never log
  secrets (tokens, cookies) — truncate URLs to ~120 chars.

## 2. Node version and modules

- The Docker image and the Windows build use **Node 26**. Node 22 is not
  equivalent: some providers reject its TLS fingerprint (vidfast returned 404,
  ppv playlist 403). Do not tune a runner for Node 18/22 behaviour.
- `.mjs` is always ESM. A local `.js` that uses `import.meta` (Emscripten
  glue, e.g. `ppv/gasm.js`) needs `{"type":"module"}` in a `package.json` next
  to it.
- Only `happy-dom` is installed in the image (`npm install --no-save --prefix /app happy-dom`).
  Do not add runtime dependencies; prefer `node:https`, `undici`, `vm`, `crypto`.

## 3. Networking — proxies and routing

- Anything that serves playback must respect the route chosen by the config
  (`warp` / `proxy` / `direct`). Never hard-code a route in a runner.
- Python passes the route as an **HTTP** proxy URL in an env var
  (`PPV_PROXY`, `VIDFAST_PROXY`, `TIMSTREAMS_PROXY`, `EMBEDST_PROXY`).
  SOCKS is converted to a local HTTP bridge by `get_http_bridge_for_proxy`.
- With a proxy: `new undici.ProxyAgent({ uri, ... })` and `undici.fetch` with
  that dispatcher. Never mix `globalThis.fetch` and a dispatcher built by a
  different undici version.
- **Never send `127.0.0.1` / `localhost` / `::1` through a proxy.** A local
  bridge must be reached directly; routing loopback into WARP/VPN causes
  `ProxyTimeoutError: 60`.
- If a local helper (e.g. `ppv_bridge.mjs`) fetches upstream, it must use the
  **same route** as the extraction: accept the HTTP proxy in a query param
  (`x=`) or env, and propagate it in every rewritten URL.

## 4. Networking — TLS fingerprints

Some CDNs filter the TLS client (JA3/JA4), not the IP. This already bit us:

- `*.indianservers.st` (ppv/ntv) rejects `node:https` with Node's default
  ciphers, and rejects `undici` through a proxy. It accepts the explicit
  cipher list + `requestTls`:
  ```js
  new undici.ProxyAgent({ uri, requestTls: { ciphers: CDN_CIPHERS } })
  ```
  and `https.get(url, { ciphers: CDN_CIPHERS })` on the direct path.
- When a provider returns 403 only through a proxy but 200 direct (same IP),
  suspect the fingerprint, not the route. Test both before blaming the IP.
- Keep playlist **validation** on the same client/route that will serve
  playback: a URL verified with one client may 403 with another.

## 5. Timeouts, retries, errors

- Every fetch needs an explicit timeout (`AbortSignal.timeout(ms)` or `signal`).
- Retry only transient failures (`ECONNRESET`, connect timeout, 5xx) with a
  short backoff, max 2–3 attempts. Never retry 4xx.
- Error messages must include the host, the route (`proxy`/`direct`) and the
  full cause chain (`error.cause`). "fetch failed" alone is useless.
- Do not swallow errors: if a step is optional, log it on stderr and continue.

## 6. Sandboxes (`vm`) and obfuscated players

- Emulate only what the page touches; stub `document`, `location`, `navigator`,
  `crypto` and `fetch` with logging wrappers, then capture the stream URL from
  the API the player uses (`Hls.loadSource`, `fetch` of `.m3u8`, player config).
- Neutralise anti-sandbox checks by keeping the expected global a non-writable
  no-op (tim-streams `window.sandDetect`).
- Skip huge inline scripts (`> 200 KB`) that never return control: they are
  usually decoys.
- Anti-tamper that validates `import.meta.url`/origin (streamcorner `ECTX`)
  cannot run outside its origin: use the browser path instead of patching it.

## 7. Ported third-party code

- Keep the license header at the top of the file:
  `Ported from <project> (MIT) - <url>`.
- Port only the parsing/decrypt layer; keep routing, cache and proxy in Python
  (`extractors/*.py`, `services/*.py`). Do not duplicate EasyProxy features
  inside a runner.

## 8. Verification before declaring a runner done

1. `node --check <runner.mjs>`.
2. Direct run: `node <runner.mjs> <real input>` → JSON with the URL.
3. Same run through a proxy: start a local HTTP CONNECT proxy
   (`python scratch/connect_proxy.py`, port 8899) and set the env var →
   same result. This catches the fingerprint/loopback bugs above.
4. Fetch the returned manifest (and one segment) with a plain client:
   `200` + `#EXTM3U` / `video/mp2t`.
5. Run it in the Docker image (`node --version` must print v26.x).

## 9. Windows / PyInstaller

- The exe uses the system `node` or `bin/node.exe`; the runners are bundled
  from `scripts/` (`easyproxy.spec`).
- `happy-dom` is installed by `scripts/build_windows_dist.py` before PyInstaller;
  if you add a new npm dependency, update that function and the Dockerfile.
