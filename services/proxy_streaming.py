import asyncio
import hashlib
import re
import time
import urllib.parse
import aiohttp
import config_store
from config import PROXY_SOURCE_LIST, find_first_alive_async, is_proxy_alive
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    web,
    yarl,
    get_browser_activity_extractor,
    set_response_header,
    check_vavoo_request,
    get_ssl_setting_for_url,
    get_proxy_for_url,
    HAS_CURL_CFFI,
    CurlAsyncSession,
    ClientTimeout,
    ClientConnectionError,
    ServerDisconnectedError,
    ClientPayloadError,
    AioProxyError,
    PyProxyError,
    should_use_short_manifest_urls,
    ManifestRewriter,
    MPDToHLSConverter,
    parse_clearkey_params,
    decrypt_segment,
    check_password,
    prepare_curl_headers,
    final_curl_request_url,
    should_use_curl_cffi,
    is_special_cdn_stream,
    ProxyDeadRetryError,
)

class _ParallelFallback(Exception):
    """Raised when parallel range fetch is not applicable; falls back to single connection."""

# Parallel range fetch thresholds: beat per-connection CDN throttling (e.g. vidsonic
# ~1.7 Mbps/conn vs 2.4 Mbps video) by downloading one segment over K parallel
# range requests. Only triggers for large segments on range-enabled CDNs.
_PARALLEL_MIN_SIZE = 1_500_000  # 1.5 MB
_PARALLEL_PARTS = 3


class HLSProxyStreamingMixin:

    # Pre-compiled regex for segment URL parsing
    _SEGMENT_URL_PATTERN = re.compile(r"([-_])(\d+)(\.[^.]+)$")

    @staticmethod
    async def _write_with_backpressure(response, chunk, max_backlog=131072):
        """Write chunk with backpressure: drain if write buffer exceeds max_backlog."""
        await response.write(chunk)
        try:
            transport = response.transport
            if transport is not None and not transport.is_closing():
                buf_size = transport.get_write_buffer_size()
                if buf_size > max_backlog:
                    await response.drain()
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _trim_cache(cache: dict, max_size: int = 30, trim_count: int = 10):
        if len(cache) <= max_size:
            return
        for key in sorted(cache.keys(), key=lambda k: cache[k][1] if isinstance(cache[k], tuple) else 0)[:trim_count]:
            cache.pop(key, None)

    async def handle_ts_segment(self, request):
        """Gestisce richieste per segmenti .ts"""
        try:
            segment_name = request.match_info.get("segment")
            base_url = request.query.get("base_url")

            if not base_url:
                return web.Response(text="Missing base URL for segment", status=400)

            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            if base_url.endswith("/"):
                segment_url = f"{base_url}{segment_name}"
            else:
                # ✅ CORREZIONE: Se base_url è un URL completo (es. generato dal converter), usalo direttamente.
                if any(
                    ext in base_url
                    for ext in [".mp4", ".m4s", ".ts", ".m4i", ".m4a", ".m4v"]
                ):
                    segment_url = base_url
                else:
                    segment_url = f"{base_url.rsplit('/', 1)[0]}/{segment_name}"

            logger.info(f"📦 Proxy Segment: {segment_name}")

            # Gestisce la risposta del proxy per il segmento
            return await self._proxy_segment(
                request,
                segment_url,
                {
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "referer": base_url,
                },
                segment_name,
            )

        except Exception as e:
            logger.error(f"Error in .ts segment proxy: {str(e)}")
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_segment_parallel(self, request, segment_url, headers, segment_name, bypass_warp, forced_proxy):
        """Download one segment via K parallel range requests to beat per-connection
        CDN throttling (e.g. vidsonic limits each TCP connection to ~1.7 Mbps while
        the video is 2.4 Mbps; 3 parallel ranges -> ~5 Mbps aggregate).

        Raises _ParallelFallback when not applicable so the caller falls back to the
        single-connection streaming path.
        """
        # Only when the client wants the whole segment (no partial-range seek).
        client_range = headers.get("range") or headers.get("Range")
        if client_range and client_range.lower() != "bytes=0-":
            raise _ParallelFallback("client requested a partial range")
        if is_special_cdn_stream(segment_url):
            raise _ParallelFallback("special CDN")

        base_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
        disable_ssl = get_ssl_setting_for_url(segment_url) or check_vavoo_request(headers, request, segment_url)

        # 1) Probe size + Accept-Ranges with a 1-byte range request.
        probe_headers = {**base_headers, "Range": "bytes=0-0"}
        session, session_proxy = await self._get_proxy_session(
            segment_url, bypass_warp=bypass_warp, forced_proxy=forced_proxy
        )
        total = None
        try:
            async with session.get(
                yarl.URL(segment_url, encoded=True),
                headers=probe_headers,
                ssl=not disable_ssl,
                timeout=ClientTimeout(total=15, connect=10, sock_connect=10, sock_read=15),
            ) as probe:
                if probe.status not in (200, 206):
                    raise _ParallelFallback(f"probe status {probe.status}")
                accept_ranges = (probe.headers.get("Accept-Ranges") or "").lower()
                content_range = probe.headers.get("Content-Range") or ""
                if "bytes" not in accept_ranges and not content_range:
                    raise _ParallelFallback("no Accept-Ranges")
                if content_range and "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                if not total and probe.status == 200:
                    cl = probe.headers.get("Content-Length")
                    if cl:
                        try:
                            total = int(cl)
                        except ValueError:
                            total = None
        except _ParallelFallback:
            raise
        except Exception as e:
            raise _ParallelFallback(f"probe error: {e}")
        finally:
            if session and session_proxy and not session.closed:
                await session.close()

        if not total or total < _PARALLEL_MIN_SIZE:
            raise _ParallelFallback(f"segment too small ({total})")

        # 2) Split into K ranges and fetch in parallel.
        K = _PARALLEL_PARTS
        chunk = total // K
        ranges = []
        for i in range(K):
            start = i * chunk
            end = total - 1 if i == K - 1 else (start + chunk - 1)
            ranges.append((start, end))

        async def _fetch_part(start, end):
            h = {**base_headers, "Range": f"bytes={start}-{end}"}
            s, s_proxy = await self._get_proxy_session(
                segment_url, bypass_warp=bypass_warp, forced_proxy=forced_proxy
            )
            try:
                async with s.get(
                    yarl.URL(segment_url, encoded=True),
                    headers=h,
                    ssl=not disable_ssl,
                    timeout=ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=60),
                ) as r:
                    r.raise_for_status()
                    return await r.read()
            finally:
                if s_proxy:
                    await s.close()

        try:
            parts = await asyncio.gather(*[_fetch_part(s, e) for s, e in ranges])
        except Exception as e:
            raise _ParallelFallback(f"parallel fetch error: {e}")

        data = b"".join(parts)
        if len(data) != total:
            raise _ParallelFallback(f"size mismatch {len(data)} != {total}")

        # 3) Stream the assembled segment to the client.
        response_headers = {}
        set_response_header(response_headers, "Content-Type", "video/mp2t")
        set_response_header(response_headers, "Content-Length", str(total))
        set_response_header(response_headers, "Content-Disposition", f'attachment; filename="{segment_name}"')
        set_response_header(response_headers, "Access-Control-Allow-Origin", "*")
        set_response_header(response_headers, "Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        set_response_header(response_headers, "Access-Control-Allow-Headers", "Range, Content-Type")
        response = web.StreamResponse(status=200, headers=response_headers)
        await response.prepare(request)
        await response.write(data)
        await response.write_eof()
        logger.info(f"⚡ Parallel fetch {segment_name}: {total} bytes via {K} ranges")
        return response

    async def _proxy_segment(self, request, segment_url, stream_headers, segment_name):
        """✅ NUOVO: Proxy dedicato per segmenti .ts con Content-Disposition"""
        try:
            # Ping browser-based extractors to keep shared browser alive
            ext = get_browser_activity_extractor(self.extractors)
            if ext and hasattr(ext, "_update_shared_activity"):
                ext._update_shared_activity()
            self._touch_extractor_activity(
                request.query.get("extractor_key"),
                request.query.get("stream_key"),
            )
            headers = dict(stream_headers)
            is_special_cdn = is_special_cdn_stream(segment_url)

            # Pass headers from query parameters (h_ parameters)
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:]
                    # Remove duplicate headers case-insensitively
                    keys_to_remove = [k for k in headers.keys() if k.lower() == header_name.lower()]
                    for k in keys_to_remove:
                        del headers[k]
                    headers[header_name] = param_value

            # Strip IP/Proxy leak headers
            for h in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                headers.pop(h, None)
                headers.pop(h.lower(), None)

            # Normalize critical headers to Title-Case
            for key in list(headers.keys()):
                if key.lower() == "user-agent":
                    headers["User-Agent"] = headers.pop(key)
                elif key.lower() == "referer":
                    headers["Referer"] = headers.pop(key)
                elif key.lower() == "origin":
                    headers["Origin"] = headers.pop(key)
                elif key.lower() == "authorization":
                    headers["Authorization"] = headers.pop(key)
                elif key.lower() == "cookie":
                    headers["Cookie"] = headers.pop(key)

            # Pass through range and validation headers from client
            for header in ["range", "if-none-match", "if-modified-since"]:
                if header in request.headers:
                    headers[header] = request.headers[header]

            if is_special_cdn:
                headers["Accept-Encoding"] = "identity"

            # ✅ Use pooled session with automatic retry failover
            bypass_warp = request.query.get("warp", "").lower() == "off"
            forced_proxy = request.query.get("proxy") or None
            if forced_proxy and forced_proxy.lower() == "off":
                forced_proxy = None
                _shared.BYPASS_PROXIES_CONTEXT.set(True)
                logger.debug(f"🔍 [Segment-DEBUG] proxy=off detected, BYPASS_PROXIES_CONTEXT=True, bypass_warp={bypass_warp}")

            current_proxy = forced_proxy
            attempts = 2 if forced_proxy else 1
            session = None
            session_proxy = None
            resp = None
            resp_ctx = None

            for attempt in range(attempts):
                try:
                    session, session_proxy = await self._get_proxy_session(
                        segment_url, bypass_warp=bypass_warp, forced_proxy=current_proxy
                    )
                    disable_ssl = get_ssl_setting_for_url(segment_url) or check_vavoo_request(headers, request, segment_url)
                    # ✅ Use yarl.URL with encoded=True to prevent double-encoding of commas
                    final_segment_url = yarl.URL(segment_url, encoded=True)
                    resp_ctx = session.get(
                        final_segment_url,
                        headers=headers,
                        ssl=not disable_ssl,
                        timeout=ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=None),
                    )
                    resp = await resp_ctx.__aenter__()
                    break
                except (ClientConnectionError, AioProxyError, PyProxyError, asyncio.TimeoutError, OSError) as e:
                    if session and session_proxy and not session.closed:
                        await session.close()
                        session = None
                        session_proxy = None
                    if attempt == 0 and current_proxy:
                        logger.warning("Segment proxy %s failed for %s: %r. Retrying with a different proxy.", current_proxy, segment_name, e)
                        self._mark_proxy_dead_if_allowed(
                            current_proxy,
                            extractor_key=request.query.get("extractor_key"),
                        )
                        new_proxy = get_proxy_for_url(segment_url, bypass_warp=bypass_warp)
                        if new_proxy and new_proxy != current_proxy:
                            current_proxy = new_proxy
                            continue
                    raise e

            try:
                response_headers = {}

                for header in [
                    "content-type",
                    "content-range",
                    "accept-ranges",
                    "last-modified",
                    "etag",
                ]:
                    if header in resp.headers:
                        response_headers[header] = resp.headers[header]

                # Forza il content-type e aggiunge Content-Disposition per .ts
                set_response_header(response_headers, "Content-Type", "video/mp2t")
                set_response_header(
                    response_headers,
                    "Content-Disposition",
                    f'attachment; filename="{segment_name}"',
                )
                set_response_header(
                    response_headers, "Access-Control-Allow-Origin", "*"
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Methods",
                    "GET, HEAD, OPTIONS",
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Headers",
                    "Range, Content-Type",
                )

                response = web.StreamResponse(status=resp.status, headers=response_headers)
                await response.prepare(request)

                first_chunk = True
                try:
                    async for chunk in resp.content.iter_any():
                        if first_chunk:
                            chunk = self._strip_fake_png_header_from_ts(chunk)
                            first_chunk = False
                        await self._write_with_backpressure(response, chunk)
                    await response.write_eof()
                    return response
                except (ClientPayloadError, ConnectionResetError, OSError) as e:
                    logger.info(
                        "Segment stream interrupted for %s [%s]: %s",
                        segment_name,
                        type(e).__name__,
                        e,
                    )
                    return response
                except Exception as e:
                    if "Connection lost" not in str(e) and "closing transport" not in str(e):
                        logger.error(f"Error streaming segment {segment_name}: {str(e)}")
                    return response
            finally:
                if resp_ctx:
                    await resp_ctx.__aexit__(None, None, None)
                if session and session_proxy and not session.closed:
                    await session.close()

        except Exception as e:
            logger.error(f"Error in segment proxy: {str(e)}")
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_stream(self, request, stream_url, stream_headers, bypass_warp=None, forced_proxy=None, force_direct=None):
        """Effettua il proxy dello stream con gestione manifest e AES-128"""
        if bypass_warp is None:
            bypass_warp = request.query.get("warp", "").lower() == "off"
        bypass_proxies = request.query.get("proxy", "").lower() == "off"
        if bypass_proxies:
            _shared.BYPASS_PROXIES_CONTEXT.set(True)
        if force_direct is None:
            force_direct = self._should_force_direct_from_query(request)
        else:
            force_direct = force_direct or self._should_force_direct_from_query(request)

        # Priorità: proxy passato esplicitamente -> proxy in query string.
        # In forced-direct retry (WARP fallback), ignore proxy query params.
        forced_proxy = None if force_direct else (forced_proxy or request.query.get("proxy") or None)
        request._ps_forced_proxy = forced_proxy
        session = None
        session_proxy = None

        try:
            # Ping browser-based extractors to keep shared browser alive
            ext = get_browser_activity_extractor(self.extractors)
            if ext and hasattr(ext, "_update_shared_activity"):
                ext._update_shared_activity()
            self._touch_extractor_activity(
                request.query.get("extractor_key"),
                request.query.get("stream_key"),
            )

            # ✅ LIVE CDN TOKEN SUBSTITUTION: If the CDN token was refreshed via
            # re-extract on 403, replace the old base URL with the new one so every
            # subsequent segment gets a fresh token without re-extracting each time.
            stream_key = request.query.get("stream_key")
            if stream_key and stream_key in getattr(self, '_renewed_cdn_tokens', {}):
                old_b, new_b, new_q = self._renewed_cdn_tokens[stream_key]
                if stream_url.startswith(old_b):
                    seg_name = stream_url[len(old_b):].split("?", 1)[0]
                    stream_url = new_b + seg_name + new_q
                    self._renewed_cdn_token_atimes[stream_key] = time.time()

            # Inline cleanup of stale tokens (backstop for cleanup cycle)
            now_ = time.time()
            stale_tok = [
                k for k, t in getattr(self, '_renewed_cdn_token_atimes', {}).items()
                if now_ - t > 300
            ]
            for k in stale_tok:
                self._renewed_cdn_tokens.pop(k, None)
                self._renewed_cdn_token_atimes.pop(k, None)

            headers = dict(stream_headers)

            # Passa attraverso alcuni headers del client, ma FILTRA quelli che potrebbero leakare l'IP
            # Rimuoviamo specificamente i condizionali che possono causare 412/416 con URL dinamici
            for header in ["range", "if-none-match", "if-modified-since"]:
                if header in request.headers:
                    headers[header] = request.headers[header]

            # ✅ FIX: Esplicita rimozione di If-Match e If-Range che spesso causano 416 su CDNs dinamici
            for h in ["if-match", "if-range"]:
                if h in headers: del headers[h]
                keys_to_remove = [k for k in headers.keys() if k.lower() == h]
                for k in keys_to_remove: del headers[k]

            # Manifest requests must be fetched in full. Some players probe the
            # entry URL with a byte range, which turns upstream playlists into
            # partial 206 responses and breaks rewriting.
            if "manifest.m3u8" in request.path and "range" in headers:
                del headers["range"]

            # ✅ FIX: Remove 'zstd' from Accept-Encoding to prevent "Can not decode content-encoding" error
            if "accept-encoding" in headers:
                ae = headers["accept-encoding"].lower()
                if "zstd" in ae:
                    # Replace zstd with nothing, cleaning up commas
                    new_ae = ae.replace("zstd", "").replace(", ,", ",").strip(", ")
                    headers["accept-encoding"] = new_ae
            elif "Accept-Encoding" in headers:
                ae = headers["Accept-Encoding"].lower()
                if "zstd" in ae:
                    new_ae = ae.replace("zstd", "").replace(", ,", ",").strip(", ")
                    headers["Accept-Encoding"] = new_ae

            # Rimuovi esplicitamente headers che potrebbero rivelare l'IP originale
            for h in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                if h in headers:
                    del headers[h]

            # ✅ FIX: Normalizza gli header critici (User-Agent, Referer) in Title-Case
            for key in list(headers.keys()):
                if key.lower() == "user-agent":
                    headers["User-Agent"] = headers.pop(key)
                elif key.lower() == "referer":
                    headers["Referer"] = headers.pop(key)
                elif key.lower() == "origin":
                    headers["Origin"] = headers.pop(key)
                elif key.lower() == "authorization":
                    headers["Authorization"] = headers.pop(key)
                elif key.lower() == "cookie":
                    headers["Cookie"] = headers.pop(key)

            for internal_header in ["X-Direct-Connection", "x-direct-connection", "X-Force-Direct", "x-force-direct"]:
                if internal_header in headers:
                    del headers[internal_header]

            # ✅ FIX: Rimuovi duplicati espliciti se presenti (es. user-agent e User-Agent)
            # Questo può accadere se GenericHLSExtractor aggiunge 'user-agent' e noi abbiamo 'User-Agent' da h_ params
            # La normalizzazione sopra dovrebbe averli unificati, ma per sicurezza puliamo.

            # Log headers finali per debug
            # logger.info(f"   Final Stream Headers: {headers}")

            is_vavoo_req = check_vavoo_request(headers, request, stream_url)
            disable_ssl = (
                request.query.get("h_X-EasyProxy-Disable-SSL") == "1"
                or request.query.get("disable_ssl") == "1"
                or headers.get("X-EasyProxy-Disable-SSL") == "1"
                or get_ssl_setting_for_url(stream_url)
                or is_vavoo_req
            )
            headers.pop("X-EasyProxy-Disable-SSL", None)
            headers.pop("x-easyproxy-disable-ssl", None)
            is_special_cdn = is_special_cdn_stream(stream_url)

            if is_special_cdn:
                headers["Accept-Encoding"] = "identity"

            def _cookie_summary(value: str | None) -> str:
                if not value:
                    return "0"
                return str(len([part for part in value.split(";") if part.strip()]))

            def _short_url(value: str, limit: int = 120) -> str:
                if len(value) <= limit:
                    return value
                return value[:limit] + "..."

            # ✅ Use pooled session for better performance
            if force_direct:
                session = await self._get_session(url=stream_url)
                session_proxy = None
                logger.info(
                    f"[Proxy Stream] Using direct session (forced) for: {stream_url}"
                )
            else:
                session, session_proxy = await self._get_proxy_session(
                    stream_url,
                    bypass_warp=bypass_warp,
                    forced_proxy=forced_proxy,
                )

                # ✅ FIX LOG: Determine correct routing for display
                if session_proxy:
                    _WARP_PROXY_URL = _shared.WARP_PROXY_URL
                    routing = f"WARP (Cloudflare IP)" if (_WARP_PROXY_URL and session_proxy == _WARP_PROXY_URL) else f"PROXY ({session_proxy})"
                else:
                    routing = "BYPASS (Real IP)"

                session_kind = "proxy" if session_proxy else "direct"
                logger.info(
                    f"📡 [Proxy Stream] {routing} - Using session ({session_kind}) for: {stream_url}"
                )

            is_hls_segment_request = request.path.startswith("/proxy/hls/segment.")

            # ⚡ Parallel range fetch to beat per-connection CDN throttling (e.g. vidsonic:
            # ~1.7 Mbps/conn vs 2.4 Mbps video -> 3 parallel ranges -> ~5 Mbps aggregate).
            # Falls back transparently to the single-connection path when not applicable.
            if is_hls_segment_request and stream_url.split("?", 1)[0].lower().endswith(".ts"):
                _seg_name = stream_url.rsplit("/", 1)[-1].split("?")[0]
                try:
                    return await self._proxy_segment_parallel(
                        request, stream_url, headers, _seg_name, bypass_warp, forced_proxy
                    )
                except _ParallelFallback as _pf:
                    logger.debug(f"parallel fetch skipped for {_seg_name}: {_pf}")
                except Exception as _pe:
                    logger.debug(f"parallel fetch error for {_seg_name}: {_pe}")

            use_curl_cffi = should_use_curl_cffi(
                stream_url,
                is_special_cdn,
                HAS_CURL_CFFI,
            )
            # ✅ FIX BUFFERING: Use generous sock_read for segments via slow proxies.
            # sock_read=None prevents SocketTimeoutError mid-transfer on large 1080p
            # segments; the total timeout still caps the overall request duration.
            segment_timeout = ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=None)

            if use_curl_cffi:
                logger.info(f"🚀 [curl_cffi] Using browser impersonation for: {stream_url}")
                try:
                    curl_s = CurlAsyncSession(impersonate="chrome124")
                    curl_headers = prepare_curl_headers(stream_url, headers)


                    curl_proxies = None
                    # ✅ DEBUG: Log final headers for comparison
                    logger.debug(f"🚀 [curl_cffi] Sending headers for {stream_url[:50]}: {curl_headers}")

                    curl_proxies = None
                    if session_proxy:
                        curl_proxies = {"http": session_proxy, "https": session_proxy}

                    # ✅ CRITICAL FIX: Ensure commas are NOT encoded.
                    final_curl_url = final_curl_request_url(stream_url)

                    # se curl_cffi diretto dovesse dare ancora 403.
                    curl_resp = await curl_s.get(
                        final_curl_url,
                        headers=curl_headers,
                        proxies=curl_proxies,
                        verify=not disable_ssl,
                        timeout=30,
                        stream=True,
                        allow_redirects=True
                    )
                    class MockContent:
                        def __init__(self, c_resp): self.c_resp = c_resp
                        async def iter_any(self):
                            async for chunk in self.c_resp.aiter_content():
                                yield chunk
                        async def read(self, n=-1):
                            # curl_cffi's acontent() returns the full body and ignores size.
                            # Match aiohttp's content.read(n) signature so error-path callers
                            # (e.g. resp.content.read(4096)) don't raise TypeError.
                            return await self.c_resp.acontent()

                    class MockResp:
                        def __init__(self, c_resp):
                            self.status = c_resp.status_code
                            self.headers = c_resp.headers
                            self.url = yarl.URL(c_resp.url)
                            self.content = MockContent(c_resp)
                        async def read(self): return await self.content.read()
                        async def text(self, errors='replace'):
                            content = await self.read()
                            return content.decode('utf-8', errors=errors)
                        async def close(self): pass
                        async def __aenter__(self): return self
                        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

                    if curl_resp.status_code in [502, 503, 504]:
                        # curl_only: 503 = live offline. Non cascare MAI ad aiohttp.
                        logger.warning(f"⚠️ [curl_cffi] {curl_resp.status_code} for {final_curl_url[:50]}: live offline o upstream errato")
                        goto_manifest_processing = False
                    else:
                        resp_ctx = MockResp(curl_resp)
                        goto_manifest_processing = True
                except Exception as e:
                    logger.error(f"❌ [curl_cffi] Error: {e}")
                    goto_manifest_processing = False
            else:
                goto_manifest_processing = False

            if not goto_manifest_processing:
                _extractor_key = request.query.get("extractor_key", "")
                _extractor = self.extractors.get(_extractor_key) if _extractor_key else None
                _curl_only = getattr(_extractor, 'curl_only', False) if _extractor else False
                if _curl_only and use_curl_cffi:
                    # CDN backend funziona solo via curl_cffi (es. embedst). 
                    # Mai aiohttp: dà 403 spurio o disconnessione TLS.
                    logger.debug("curl_only: no aiohttp fallback, returning error directly")
                    resp_ctx = None
                else:
                    if is_special_cdn:
                        request_target = urllib.parse.unquote(stream_url)
                    else:
                        request_target = yarl.URL(stream_url, encoded=True)
                    resp_ctx = session.get(
                        request_target,
                        headers=headers,
                        ssl=not disable_ssl,
                        timeout=segment_timeout if is_hls_segment_request else None,
                    )

            async def retry_with_different_proxy():
                if forced_proxy or not session_proxy:
                    return None
                old_proxy = session_proxy
                logger.info("Rotating proxy after upstream error on %s", old_proxy)
                self._mark_proxy_dead_if_allowed(
                    old_proxy,
                    dead_duration=120,
                    extractor_key=request.query.get("extractor_key"),
                )
                rot_session, rot_proxy = await self._get_proxy_session(
                    stream_url, bypass_warp=True, forced_proxy=None,
                )
                if not rot_proxy or rot_proxy == old_proxy:
                    await rot_session.close()
                    rot_session, rot_proxy = await self._get_proxy_session(
                        stream_url, bypass_warp=True, forced_proxy=None,
                    )

                # 1) Direct retry of same URL via new proxy
                try:
                    rot_target = yarl.URL(stream_url, encoded=True) if not is_special_cdn else urllib.parse.unquote(stream_url)
                    async with rot_session.get(rot_target, headers=headers, ssl=not disable_ssl, timeout=segment_timeout) as rot_resp:
                        if rot_resp.status in [200, 206]:
                            logger.info("Proxy rotation successful (direct): %s -> %s", old_proxy, rot_proxy or "direct")
                            rot_body = await rot_resp.read()
                            rh = dict(rot_resp.headers)
                            rh["Access-Control-Allow-Origin"] = "*"
                            return web.Response(body=rot_body, status=rot_resp.status, headers=rh)
                except Exception as exc:
                    logger.debug("Proxy rotation direct retry failed: %s", exc)
                finally:
                    if rot_session and not rot_session.closed:
                        await rot_session.close()

                # 2) Re-extract not available (no captured manifest cache) — give up
                logger.info("Proxy rotation: re-extract not available (live mode, no cache)")
                return None

            async def retry_same_segment_after_payload_error(reason):
                if not request.path.startswith("/proxy/hls/segment."):
                    return None
                retry_target = urllib.parse.unquote(stream_url) if is_special_cdn else yarl.URL(stream_url, encoded=True)
                for attempt in range(2):
                    await asyncio.sleep(0.15 * (attempt + 1))
                    retry_session = None
                    retry_proxy = None
                    try:
                        retry_session, retry_proxy = await self._get_proxy_session(
                            stream_url, bypass_warp=bypass_warp, forced_proxy=forced_proxy,
                        )
                        async with retry_session.get(retry_target, headers=headers, ssl=not disable_ssl, timeout=segment_timeout) as retry_resp:
                            if retry_resp.status not in [200, 206]:
                                logger.debug(
                                    "Segment payload retry got status %s for %s",
                                    retry_resp.status,
                                    stream_url,
                                )
                                continue
                            retry_body = await retry_resp.read()
                            logger.info(
                                "✅ [Recupero] Segmento ripristinato (%d/2) per %s (%s)",
                                attempt + 1,
                                stream_url.split('/')[-1].split('?')[0],
                                type(reason).__name__,
                            )
                            return retry_body, retry_resp.headers, retry_resp.status
                    except (ClientPayloadError, ConnectionResetError, OSError, asyncio.TimeoutError) as exc:
                        logger.debug(
                            "Segment payload retry %d failed for %s: %r",
                            attempt + 1,
                            stream_url,
                            exc,
                        )
                    finally:
                        if retry_session and retry_proxy and not retry_session.closed:
                            await retry_session.close()
                return None

            if resp_ctx is None:
                # curl_only (embedst): curl_cffi fallito, live offline → return 503 subito
                logger.debug("curl_only: upstream offline, returning 503")
                return web.Response(
                    status=503,
                    text="Stream offline",
                    headers={"Access-Control-Allow-Origin": "*", "Content-Type": "text/plain; charset=utf-8"},
                )

            async with resp_ctx as resp:
                content_type = resp.headers.get("content-type", "").lower()

                if resp.status not in [200, 206]:
                    if resp.status == 403:
                        rot_response = await retry_with_different_proxy()
                        if rot_response:
                            return rot_response
                        # Last resort: re-extract to refresh signed CDN token (e.g. VidXgo)
                        re_response = await self._reextract_and_retry_segment(
                            request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl
                        )
                        if re_response:
                            return re_response
                    if is_special_cdn and resp.status == 403 and not goto_manifest_processing:
                        retry_result = await self._retry_special_cdn_request(
                            request_target,
                            headers,
                            disable_ssl,
                        )
                        if retry_result:
                            retry_headers = dict(retry_result["headers"])
                            retry_headers["Access-Control-Allow-Origin"] = "*"
                            logger.info(
                                "✅ Provider CDN retry success via alternate route: %s",
                                retry_result["proxy"],
                            )
                            return web.Response(
                                body=retry_result["body"],
                                status=retry_result["status"],
                                headers=retry_headers,
                            )
                    if resp.status == 403 and request.path.endswith("manifest.m3u8"):
                        logger.debug("Upstream 403 on manifest, skipping recovery (browser fallback disabled): %s", stream_url)
                    error_body = await resp.content.read(4096) or b""
                    routing = (
                        f"WARP ({session_proxy})"
                        if session_proxy and _shared.WARP_PROXY_URL and session_proxy == _shared.WARP_PROXY_URL
                        else ("BYPASS" if session_proxy is None else f"PROXY ({session_proxy})")
                    )
                    logger.warning(f"⚠️ Upstream returned error {resp.status} for {stream_url} [Routing: {routing}]")
                    return web.Response(body=error_body, status=resp.status, headers={"Content-Type": content_type, "Access-Control-Allow-Origin": "*"})

                is_direct_media_stream = (
                    "video/" in content_type or stream_url.lower().endswith((".mp4", ".mkv", ".avi", ".mov"))
                )

                # ✅ FIX BUFFERING: Stream HLS segments chunk-by-chunk
                # instead of buffering entirely with resp.read(). This prevents
                # SocketTimeoutError on large segments via slow proxies and
                # reduces perceived latency for the player.
                is_segment_like = (
                    is_hls_segment_request
                    and any(stream_url.lower().split('?')[0].endswith(ext) for ext in
                            ['.ts', '.m4s', '.aac', '.m4a', '.m4v', '.m4i', '.mp4', '.mkv', '.avi', '.mov'])
                    and 'mpegurl' not in content_type
                    and not content_type.startswith('text/')
                )

                if is_direct_media_stream or is_segment_like:
                    seg_content_type = "video/mp2t" if is_segment_like else content_type
                    response_headers = {
                        "Content-Type": seg_content_type,
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": "Range, Content-Type",
                    }
                    for h in ["content-length", "content-range", "accept-ranges"]:
                        if h in resp.headers: response_headers[h] = resp.headers[h]

                    response = web.StreamResponse(status=resp.status, headers=response_headers)
                    await response.prepare(request)
                    try:
                        first_chunk = True
                        async for chunk in resp.content.iter_any():
                            if first_chunk and is_segment_like:
                                chunk = self._strip_fake_png_header_from_ts(chunk)
                                first_chunk = False
                            await self._write_with_backpressure(response, chunk)
                        await response.write_eof()
                        return response
                    except (ClientPayloadError, ConnectionResetError, OSError) as e:
                        logger.info(
                            "Stream relay interrupted for %s [%s]: %s",
                            stream_url,
                            type(e).__name__,
                            e,
                        )
                        return response
                    except Exception as e:
                        if "Connection lost" not in str(e) and "closing transport" not in str(e):
                            logger.error(
                                "❌ Stream error [%s]: %r",
                                type(e).__name__,
                                e,
                            )
                        return response

                response_source_headers = resp.headers
                response_status = resp.status
                try:
                    content_bytes = await resp.read()
                except (ClientPayloadError, ConnectionResetError, OSError) as e:
                    retry_result = await retry_same_segment_after_payload_error(e)
                    if not retry_result:
                        raise
                    content_bytes, response_source_headers, response_status = retry_result
                manifest_content = None
                try:
                    decoded_text = content_bytes.decode("utf-8", errors='replace')
                    if decoded_text.lstrip().startswith("#EXTM3U"):
                        manifest_content = decoded_text
                except Exception:
                    logger.debug("Response is not valid UTF-8 text (expected for segments)")
                    pass

                if manifest_content is None and (".m3u8" in stream_url or "mpegurl" in content_type):
                    try:
                        decoded_text = content_bytes.decode("utf-8", errors='replace')
                        if decoded_text.lstrip().startswith("#EXTM3U"):
                            manifest_content = decoded_text
                        else:
                            logger.warning(
                                "Upstream did not return a valid HLS manifest for %s: %s",
                                stream_url,
                                decoded_text[:120].replace("\n", "\\n"),
                            )
                            return web.Response(
                                text="Upstream did not return a valid HLS manifest",
                                status=502,
                                headers={
                                    "Content-Type": "text/plain; charset=utf-8",
                                    "Access-Control-Allow-Origin": "*",
                                },
                            )
                    except Exception:
                        pass

                if manifest_content:
                    logger.info(f"📄 HLS manifest detected: {stream_url}")
                    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                    host = request.headers.get("X-Forwarded-Host", request.host)
                    proxy_base = f"{scheme}://{host}"
                    original_url = request.query.get("orig_url") or request.query.get("url") or request.query.get("d", "")
                    use_short_hls_urls = should_use_short_manifest_urls(
                        original_url,
                        request.query.get("host", ""),
                        str(resp.url),
                    )

                    disable_ssl = request.query.get("disable_ssl") == "1" or get_ssl_setting_for_url(str(resp.url))

                    rewritten = await ManifestRewriter.rewrite_manifest_urls(
                        manifest_content=manifest_content,
                        base_url=str(resp.url),
                        proxy_base=proxy_base,
                        stream_headers=headers,
                        original_channel_url=original_url,
                        api_password=request.query.get("api_password"),
                        get_extractor_func=self.get_extractor,
                        no_bypass=request.query.get("no_bypass") == "1",
                        shorten_url_func=self.shorten_hls_url if use_short_hls_urls else None,
                        bypass_warp=bypass_warp,
                        bypass_proxies=bypass_proxies,
                        disable_ssl=disable_ssl,
                        selected_proxy=forced_proxy, # ✅ PASSA IL PROXY FORZATO
                        force_direct=force_direct,
                        extractor_key=request.query.get("extractor_key"),
                        stream_key=request.query.get("stream_key"),
                    )
                    return web.Response(text=rewritten, headers={
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    })

                # ✅ AGGIORNATO: Gestione per manifest MPD (DASH) - separate block
                if manifest_content is None and ("dash+xml" in content_type or stream_url.endswith(".mpd")):
                    manifest_content = content_bytes.decode("utf-8", errors='replace')

                    # ✅ CORREZIONE: Rileva lo schema e l'host corretti quando dietro un reverse proxy
                    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                    host = request.headers.get("X-Forwarded-Host", request.host)
                    proxy_base = f"{scheme}://{host}"

                    # Recupera parametri
                    clearkey_param = parse_clearkey_params(request)

                    # --- LEGACY MODE: MPD -> HLS Conversion ---
                    _MPD_MODE = _shared.MPD_MODE
                    if _MPD_MODE in ("legacy", "none", "disabled") and MPDToHLSConverter:
                        logger.info(
                            f"🔄 [Legacy Mode] Converting MPD to HLS for {stream_url}"
                        )
                        try:
                            converter = MPDToHLSConverter()

                            # Check if requesting a Media Playlist (Variant)
                            rep_id = request.query.get("rep_id")

                            if rep_id:
                                # Generate Media Playlist (Segments)
                                hls_playlist = converter.convert_media_playlist(
                                    manifest_content,
                                    rep_id,
                                    proxy_base,
                                    stream_url,
                                    request.query_string,
                                    clearkey_param,
                                )
                                # Log first few lines for debugging
                                logger.debug(
                                    f"📜 Generated Media Playlist for {rep_id} (first 10 lines):\n{chr(10).join(hls_playlist.splitlines()[:10])}"
                                )
                            else:
                                # Generate Master Playlist
                                hls_playlist = converter.convert_master_playlist(
                                    manifest_content,
                                    proxy_base,
                                    stream_url,
                                    request.query_string,
                                )
                                logger.debug(
                                    f"📜 Generated Master Playlist (first 5 lines):\n{chr(10).join(hls_playlist.splitlines()[:5])}"
                                )

                            return web.Response(
                                text=hls_playlist,
                                headers={
                                    "Content-Type": "application/vnd.apple.mpegurl",
                                    "Access-Control-Allow-Origin": "*",
                                    "Cache-Control": "no-cache",
                                },
                            )
                        except Exception as e:
                            logger.error(f"❌ Legacy conversion failed: {e}")
                            # Fallback to DASH proxy if conversion fails
                            pass

                    # --- DEFAULT: DASH Proxy (Rewriting) ---
                    req_format = request.query.get("format")
                    rep_id = request.query.get("rep_id")

                    api_password = request.query.get("api_password")
                    rewritten_manifest = ManifestRewriter.rewrite_mpd_manifest(
                        manifest_content,
                        stream_url,
                        proxy_base,
                        headers,
                        clearkey_param,
                        api_password,
                        bypass_warp=bypass_warp,
                        bypass_proxies=bypass_proxies,
                    )

                    return web.Response(
                        text=rewritten_manifest,
                        headers={
                            "Content-Type": "application/dash+xml",
                            "Content-Disposition": 'attachment; filename="stream.mpd"',
                            "Access-Control-Allow-Origin": "*",
                            "Cache-Control": "no-cache",
                        },
                    )

                # Streaming normale per altri tipi di contenuto (segmenti binari)
                # Il body è già stato letto in content_bytes, usiamo quello.
                segment_was_stripped = False
                if request.path.endswith(".ts") or stream_url.endswith(".ts"):
                    original_len = len(content_bytes)
                    content_bytes = self._strip_fake_png_header_from_ts(content_bytes)
                    segment_was_stripped = len(content_bytes) != original_len

                response_headers = {}

                for header in [
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "last-modified",
                    "etag",
                ]:
                    if header in response_source_headers:
                        response_headers[header] = response_source_headers[header]

                # ✅ FIX: Forza Content-Type coerente se il server non lo invia correttamente
                if (
                    stream_url.endswith(".ts") or request.path.endswith(".ts")
                ) and "video/mp2t" not in response_headers.get(
                    "content-type", ""
                ).lower():
                    set_response_header(response_headers, "Content-Type", "video/mp2t")
                elif (
                    stream_url.endswith(".vtt")
                    or stream_url.endswith(".webvtt")
                    or request.path.endswith(".vtt")
                ) and "text/vtt" not in response_headers.get(
                    "content-type", ""
                ).lower():
                    set_response_header(response_headers, "Content-Type", "text/vtt; charset=utf-8")
                if segment_was_stripped:
                    set_response_header(
                        response_headers, "Content-Length", str(len(content_bytes))
                    )
                    response_headers.pop("content-range", None)
                    response_headers.pop("Content-Range", None)
                    response_headers.pop("accept-ranges", None)
                    response_headers.pop("Accept-Ranges", None)

                set_response_header(
                    response_headers, "Access-Control-Allow-Origin", "*"
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Methods",
                    "GET, HEAD, OPTIONS",
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Headers",
                    "Range, Content-Type",
                )

                # Override content-length with actual bytes read, evitando duplicati case-insensitive
                set_response_header(
                    response_headers, "Content-Length", str(len(content_bytes))
                )

                return web.Response(
                    body=content_bytes,
                    status=response_status,
                    headers=response_headers,
                )


        except (ClientPayloadError, ConnectionResetError) as e:
            active_proxy = session_proxy or forced_proxy
            if active_proxy:
                logger.info(
                    "Stream interrupted while using proxy %s (payload/reset): %r.",
                    active_proxy, e
                )
            logger.info(f"[INFO] Client disconnected from stream: {stream_url} ({str(e)})")
            return web.Response(text="Client disconnected", status=499)

        except (
            ServerDisconnectedError,
            ClientConnectionError,
            asyncio.TimeoutError,
            OSError,
            AioProxyError,
            PyProxyError,
        ) as e:
            # Errori di connessione upstream
            active_proxy = session_proxy or forced_proxy
            if active_proxy:
                logger.warning(
                    "Proxy %s failed connection to source: %r.",
                    active_proxy, e
                )
                self._mark_proxy_dead_if_allowed(
                    active_proxy,
                    extractor_key=request.query.get("extractor_key"),
                )
            # Reactive WARP reconnect: if WARP failed mid-stream, reconnect immediately
            if active_proxy and getattr(_shared, 'WARP_PROXY_URL', None) and active_proxy == _shared.WARP_PROXY_URL:
                logger.warning("WARP stream failure, triggering immediate reconnect...")
                asyncio.create_task(self.reconnect_warp())
            logger.warning(f"⚠️ Connection lost with source: {stream_url} ({str(e)})")
            return web.Response(text=f"Upstream connection lost: {str(e)}", status=502)

        except Exception as e:
            err_msg = str(e)
            if "Connection lost" in err_msg or "Connection reset" in err_msg:
                active_proxy = session_proxy or forced_proxy
                if active_proxy:
                    logger.warning(
                        "Proxy %s connection lost/reset: %r.",
                        active_proxy, e
                    )
                    self._mark_proxy_dead_if_allowed(
                        active_proxy,
                        extractor_key=request.query.get("extractor_key"),
                    )
                logger.info(f"[INFO] Stream connection closed by client or server: {stream_url}")
                return web.Response(text="Connection lost", status=499)

            # If forced_proxy was set and failed with a proxy/connection error, re-extract
            forced_proxy = getattr(request, '_ps_forced_proxy', None)
            if forced_proxy and not getattr(request, '_ps_retried', False):
                err_lower = err_msg.lower()
                is_proxy_err = any(x in err_lower for x in ("invalid reply", "request rejected", "connection refused", "connection reset", "proxy connection timed out", "can't connect to server", "couldn't connect", "connect call failed", "0x9", "0x7", "socks5"))
                if is_proxy_err:
                    request._ps_retried = True
                    self._mark_proxy_dead_if_allowed(
                        forced_proxy,
                        extractor_key=request.query.get("extractor_key"),
                    )
                    if not is_proxy_alive(forced_proxy):
                        logger.warning("Proxy %s failed for %s, triggering re-extraction", forced_proxy, stream_url)
                        raise ProxyDeadRetryError("PROXY_DEAD_RETRY_EXTRACTION")
                    logger.info("Proxy %s had transient error for %s, skipping re-extraction", forced_proxy, stream_url)

            logger.error(
                "❌ Generic error in stream proxy [%s]: %r",
                type(e).__name__,
                e,
            )
            return web.Response(text=f"Stream error: {err_msg}", status=500)
        finally:
            if session and not session.closed and session_proxy is not None:
                await session.close()

    async def _reextract_and_retry_segment(
        self, request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl
    ):
        """Re-extract the source on 403 to refresh signed CDN tokens, then retry the segment."""
        import urllib.parse as _up
        from services.proxy_shared import ProxyDeadRetryError

        orig_url = request.query.get("orig_url")
        if not orig_url:
            return None

        # Only attempt for HLS segment requests with an extractor source URL
        if not request.path.startswith("/proxy/hls/segment."):
            return None

        try:
            extractor = await self.get_extractor(orig_url, headers, bypass_warp=bypass_warp)
            if not extractor:
                return None

            refreshed = await extractor.extract(
                orig_url,
                force_refresh=True,
                request_headers=headers,
                bypass_warp=bypass_warp,
                proxy=forced_proxy,
            )
        except Exception as exc:
            logger.debug("Re-extract for segment 403 failed: %s", exc)
            return None
        finally:
            # 🚫 Cache disabilitata: chiudi subito l'estrattore re-estratto.
            _ek = self._extractor_key_for_instance(extractor) if extractor else None
            if _ek and _ek in self.extractors:
                _ext = self.extractors.pop(_ek, None)
                self._extractor_atimes.pop(_ek, None)
                for _sr in [r for r in self._extractor_stream_atimes if r[0] == _ek]:
                    self._extractor_stream_atimes.pop(_sr, None)
                if _ext and hasattr(_ext, "close"):
                    try:
                        await _ext.close()
                    except Exception:
                        pass

        captured_manifests = refreshed.get("captured_manifests") or {}
        master_url = refreshed.get("destination_url")
        master_text = refreshed.get("captured_manifest")
        if not master_text and master_url:
            captured_manifests = {master_url: master_text} if master_text else {}

        # Find the refreshed segment URL matching the requested segment filename
        seg_filename = stream_url.rsplit("/", 1)[-1].split("?")[0]
        fresh_url = None
        for m_url, m_text in captured_manifests.items():
            for line in (m_text or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                abs_url = _up.urljoin(m_url, line)
                if abs_url.rsplit("/", 1)[-1].split("?")[0] == seg_filename:
                    fresh_url = abs_url
                    break
            if fresh_url:
                break

        if not fresh_url or fresh_url.rsplit("/", 1)[-1].split("?")[0] != seg_filename:
            logger.debug("Re-extract: could not locate %s in refreshed manifest", seg_filename)
            return None

        # Fetch the segment with the fresh token
        retry_session = None
        need_close = False
        retry_proxy = None
        try:
            if force_direct:
                retry_session = await self._get_session(url=fresh_url)
            else:
                retry_session, retry_proxy = await self._get_proxy_session(
                    fresh_url, bypass_warp=bypass_warp, forced_proxy=forced_proxy,
                )
                if retry_proxy:
                    need_close = True
            import yarl
            target = yarl.URL(fresh_url, encoded=True)
            async with retry_session.get(
                target,
                headers=headers,
                ssl=not disable_ssl,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as fr_resp:
                if fr_resp.status not in [200, 206]:
                    logger.warning(
                        "Re-extract segment retry still failed %d for %s",
                        fr_resp.status, fresh_url,
                    )
                    return None
                body = await fr_resp.read()
                rh = {"Access-Control-Allow-Origin": "*", "Content-Type": "video/mp2t"}
                logger.info("✅ Segment recovered via re-extract: %s", seg_filename)

                # Save refreshed CDN base URL for this stream_key so subsequent
                # segments use the new token without re-extracting each time.
                stream_key = request.query.get("stream_key")
                if stream_key:
                    old_base_dir = stream_url.rsplit("/", 1)[0] + "/"
                    new_base_dir = fresh_url.rsplit("/", 1)[0] + "/"
                    new_qs = ""
                    if "?" in fresh_url:
                        new_qs = "?" + fresh_url.split("?", 1)[1]
                    self._renewed_cdn_tokens[stream_key] = (old_base_dir, new_base_dir, new_qs)
                    self._renewed_cdn_token_atimes[stream_key] = time.time()
                    logger.info("🔑 CDN token saved for stream_key=%s — subsequent segments skip re-extract", stream_key[:8])

                return web.Response(body=body, status=fr_resp.status, headers=rh)
        except Exception as exc:
            logger.debug("Re-extract segment fetch error: %s", exc)
            return None
        finally:
            if need_close and retry_session and not retry_session.closed:
                await retry_session.close()

    async def _remux_to_ts(self, content):
        """Converte segmenti (fMP4) in MPEG-TS usando FFmpeg pipe."""
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                "pipe:0",
                "-c",
                "copy",
                "-copyts",  # Preserve timestamps to prevent freezing/gap issues
                "-f",
                "mpegts",
                "pipe:1",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate(input=content)

            # Check for data presence regardless of return code (workaround for asyncio race condition on some platforms)
            if len(stdout) > 0:
                if proc.returncode != 0:
                    logger.debug(
                        f"FFmpeg remux finished with code {proc.returncode} but produced output (ignoring). Stderr: {stderr.decode()[:200]}"
                    )
                return stdout

            if proc.returncode != 0:
                logger.error(f"❌ FFmpeg remux failed: {stderr.decode()}")
                return None

            return stdout
        except Exception as e:
            logger.error(f"❌ Remux error: {e}")
            return None

    async def handle_decrypt_segment(self, request):
        """Decripta segmenti fMP4 lato server per ClearKey (legacy mode)."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        url = request.query.get("url")
        logger.info(f"🔓 Decrypt Request: {url.split('/')[-1] if url else 'unknown'}")

        init_url = request.query.get("init_url")
        key = request.query.get("key")
        key_id = request.query.get("key_id")

        if not url or not key or not key_id:
            return web.Response(text="Missing url, key, or key_id", status=400)

        if decrypt_segment is None:
            return web.Response(
                text="Decrypt not available (MPD_MODE is ffmpeg or disabled)", status=503
            )

        try:
            # Ricostruisce gli headers per le richieste upstream
            headers = {"Connection": "keep-alive", "Accept-Encoding": "identity"}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    headers[header_name] = param_value

            # Get proxy-enabled session for segment fetches
            bypass_warp = request.query.get("warp", "").lower() == "off"
            forced_proxy = request.query.get("proxy") or None
            if forced_proxy and forced_proxy.lower() == "off":
                forced_proxy = None
                _shared.BYPASS_PROXIES_CONTEXT.set(True)
            logger.debug(f"🔍 [Decrypt-DEBUG] bypass_warp={bypass_warp}, forced_proxy={forced_proxy}, warp_param='{request.query.get('warp', 'NOT_FOUND')}'")
            proxy_from_config = get_proxy_for_url(url, bypass_warp=bypass_warp)
            logger.debug(f"🔍 [Decrypt-DEBUG] get_proxy_for_url returned: {proxy_from_config}")
            segment_session, segment_proxy = await self._get_proxy_session(
                url, bypass_warp=bypass_warp, forced_proxy=forced_proxy
            )
            if segment_proxy:
                logger.info(f"📡 [Decrypt] Using session via proxy: {segment_proxy}")

            try:
                # Parallel download of init and media segment
                async def fetch_init():
                    if not init_url:
                        return b""
                    disable_ssl = get_ssl_setting_for_url(init_url)
                    try:
                        async with segment_session.get(
                            init_url,
                            headers=headers,
                            ssl=not disable_ssl,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                return content
                            logger.error(
                                f"❌ Init segment returned status {resp.status}: {init_url}"
                            )
                            return None
                    except Exception as e:
                        logger.error(f"❌ Failed to fetch init segment: {e}")
                        return None

                async def fetch_segment():
                    disable_ssl = get_ssl_setting_for_url(url)
                    try:
                        async with segment_session.get(
                            url,
                            headers=headers,
                            ssl=not disable_ssl,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status == 200:
                                return await resp.read()
                            logger.error(
                                f"❌ Segment returned status {resp.status}: {url}"
                            )
                            return None
                    except Exception as e:
                        logger.error(f"❌ Failed to fetch segment: {e}")
                        return None

                # Parallel fetch
                init_content, segment_content = await asyncio.gather(
                    fetch_init(), fetch_segment()
                )
            finally:
                if segment_session and not segment_session.closed:
                    await segment_session.close()

            if init_content is None and init_url:
                logger.error(f"❌ Failed to fetch init segment")
                return web.Response(status=502)
            if segment_content is None:
                logger.error(f"❌ Failed to fetch segment")
                return web.Response(status=502)

            init_content = init_content or b""

            # Check if we should skip decryption (null key case)
            skip_decrypt = request.query.get("skip_decrypt") == "1"

            if skip_decrypt:
                # Null key: just concatenate init + segment without decryption
                logger.info(f"🔓 Skip decrypt mode - remuxing without decryption")
                combined_content = init_content + segment_content
            else:
                # Decripta con PyCryptodome
                # Decrypt in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                combined_content = await loop.run_in_executor(
                    None, decrypt_segment, init_content, segment_content, key_id, key
                )

            # Leggero REMUX to TS (if enabled)
            if _shared.ENABLE_REMUXING:
                ts_content = await self._remux_to_ts(combined_content)
                if not ts_content:
                    logger.warning("⚠️ Remux failed, serving raw fMP4")
                    ts_content = combined_content
                    content_type = "video/mp4"
                else:
                    content_type = "video/mp2t"
                    logger.info("⚡ Remuxed fMP4 -> TS")
            else:
                logger.debug("⏩ Remuxing disabled, serving raw fMP4")
                ts_content = combined_content
                content_type = "video/mp4"

            # Invia Risposta
            return web.Response(
                body=ts_content,
                status=200,
                headers={
                    "Content-Type": content_type,
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        except Exception as e:
            logger.error(f"❌ Decryption error: {e}")
            return web.Response(status=500, text=f"Decryption failed: {str(e)}")
