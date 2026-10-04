import asyncio
import secrets
import urllib.parse
import aiohttp
import config as _config
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    web,
    check_password,
    get_client_ip,
    BYPASS_WARP_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    BYPASS_TOR_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    ManifestRewriter,
    check_vavoo_request,
    should_use_short_manifest_urls,
    parse_clearkey_params,
    seal_clearkey,
    MPDToHLSConverter,
    get_ssl_setting_for_url,
    ALL_PROXY_ERRORS,
    ClientConnectionError,
    ProxyDeadRetryError,
    get_proxy_for_url,
    is_expired_embed_error,
    extractor_name_for_log,
    get_public_base_url,
    get_extractor_routing_overrides,
    request_log_context,
    safe_log_route,
)


class HLSProxyManifestHandlerMixin:

    @staticmethod
    def _mpd_fetch_key(
        url,
        headers,
        bypass_warp,
        bypass_proxies,
        selected_proxy,
        stream_key=None,
    ):
        """Return a key shared only by requests in the same playback."""
        header_items = tuple(
            sorted(
                (str(name).lower(), str(value))
                for name, value in (headers or {}).items()
            )
        )
        return (
            str(url),
            header_items,
            bool(bypass_warp),
            bool(bypass_proxies),
            str(selected_proxy or ""),
            str(stream_key or ""),
        )

    async def _fetch_mpd_manifest(
        self,
        request,
        stream_url,
        stream_headers,
        bypass_warp,
        bypass_proxies,
        selected_proxy,
        extractor,
        stream_key=None,
    ):
        """Fetch one MPD, sharing only simultaneous callers.

        This intentionally retains no MPD/segment cache. It only prevents an
        iOS player opening video and audio playlists at the same time from
        creating several identical upstream MPD requests.
        """
        ssl_context = False if get_ssl_setting_for_url(stream_url) else None
        retries = 2

        for attempt in range(retries):
            mpd_proxy = None
            mpd_session = None
            started = asyncio.get_running_loop().time()
            try:
                mpd_session, mpd_proxy = await self._get_proxy_session(
                    stream_url,
                    bypass_warp=bypass_warp,
                    forced_proxy=selected_proxy,
                    session_key=stream_key,
                )
                logger.info(
                    "📡 [MPD] Attempt %s/%s via %s [%s]",
                    attempt + 1,
                    retries,
                    safe_log_route(mpd_proxy),
                    request_log_context(
                        request,
                        stream_url,
                        route=safe_log_route(mpd_proxy),
                        extractor=extractor,
                    ),
                )

                async with mpd_session.get(
                    stream_url,
                    headers=stream_headers,
                    ssl=ssl_context,
                    allow_redirects=True,
                ) as resp:
                    final_mpd_url = str(resp.url)
                    if final_mpd_url != stream_url:
                        logger.info(
                            "↪️ MPD redirected [%s]",
                            request_log_context(
                                request,
                                final_mpd_url,
                                route=safe_log_route(mpd_proxy),
                                extractor=extractor,
                            ),
                        )

                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "❌ Failed to fetch MPD: status=%s [%s]",
                            resp.status,
                            request_log_context(
                                request,
                                stream_url,
                                route=safe_log_route(mpd_proxy),
                                extractor=extractor,
                            ),
                        )
                        if attempt == retries - 1:
                            return None, None
                        await asyncio.sleep(1)
                        continue

                    manifest_content = await resp.text()
                    logger.debug(
                        "[MPD] fetched bytes=%d time=%.3fs route=%s",
                        len(manifest_content),
                        asyncio.get_running_loop().time() - started,
                        safe_log_route(mpd_proxy),
                    )
                    return manifest_content, final_mpd_url

            except ALL_PROXY_ERRORS + (asyncio.TimeoutError, ClientConnectionError, OSError) as error:
                is_proxy = isinstance(error, ALL_PROXY_ERRORS)
                if not is_proxy and mpd_proxy and isinstance(error, (ClientConnectionError, OSError)):
                    is_proxy = True
                logger.warning(
                    "⚠️ [MPD] %s error at attempt %s: %s elapsed=%.3fs [%s]",
                    "Proxy" if is_proxy else "Timeout",
                    attempt + 1,
                    error,
                    asyncio.get_running_loop().time() - started,
                    request_log_context(
                        request,
                        stream_url,
                        route=safe_log_route(mpd_proxy),
                        extractor=extractor,
                    ),
                )
                if mpd_proxy and "127.0.0.1" in mpd_proxy:
                    self._mark_proxy_dead_if_allowed(
                        mpd_proxy,
                        extractor_key=request.query.get("extractor_key"),
                    )
                if mpd_proxy:
                    await self._invalidate_proxy_session(mpd_proxy)
                if is_proxy and SELECTED_PROXY_CONTEXT.get() and not STRICT_PROXY_CONTEXT.get():
                    SELECTED_PROXY_CONTEXT.set(None)
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
                return None, None
            except Exception as error:
                logger.error(
                    "❌ [MPD] Unexpected error at attempt %s: %s [%s]",
                    attempt + 1,
                    error,
                    request_log_context(
                        request,
                        stream_url,
                        route=safe_log_route(mpd_proxy),
                        extractor=extractor,
                    ),
                )
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
                return None, None
            finally:
                if mpd_session and not mpd_session.closed:
                    await mpd_session.close()

        return None, None

    async def handle_proxy_request(self, request):
        """Gestisce le richieste proxy principali"""
        if not check_password(request):
            logger.warning(
                f"⛔ Access denied: Invalid or missing API Password. IP: {get_client_ip(request)}"
            )
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        target_url = request.query.get("url") or request.query.get("d")

        # Check if it's a native MPD request (no HLS conversion)
        is_native_mpd = request.path.endswith("/manifest.mpd")

        bypass_warp = (request.query.get("warp", "").lower() == "off")
        token = BYPASS_WARP_CONTEXT.set(bypass_warp)
        
        bypass_proxies = (request.query.get("proxy", "").lower() == "off")
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(bypass_proxies)

        bypass_tor = (request.query.get("tor", "").lower() == "off")
        tor_bypass_token = BYPASS_TOR_CONTEXT.set(bypass_tor)
        
        selected_proxy = None
        raw_proxy = request.query.get("proxy")
        if raw_proxy and raw_proxy.lower() != "off":
            selected_proxy = urllib.parse.unquote(raw_proxy)
            if "://" not in selected_proxy and "%3a" in selected_proxy.lower():
                selected_proxy = urllib.parse.unquote(selected_proxy)
        if selected_proxy and _config.is_warp_proxy_url(selected_proxy) and (
            bypass_warp or not _config._get_dynamic_warp_enabled()
        ):
            logger.debug(
                "Ignoring stale WARP proxy from relay URL: %s",
                selected_proxy,
            )
            selected_proxy = None
        proxy_token = SELECTED_PROXY_CONTEXT.set(selected_proxy)
        strict_proxy_token = STRICT_PROXY_CONTEXT.set(bool(selected_proxy))
        force_direct = self._should_force_direct_from_query(request)
        extractor = None
        extractor_key = request.query.get("extractor_key")

        # Keep extractor routing policy when a generated relay URL no longer
        # carries the original warp=/proxy= flags.
        admin_warp_off, admin_proxy_off = get_extractor_routing_overrides(extractor_key)
        if admin_warp_off:
            bypass_warp = True
            BYPASS_WARP_CONTEXT.set(True)
        if admin_proxy_off:
            bypass_proxies = True
            BYPASS_PROXIES_CONTEXT.set(True)

        try:
            # --- Gestione URL brevi (Shortened URLs, base64 only) ---
            url_id = request.query.get("hls_url_id")
            if url_id and not target_url:
                resolved = await self._resolve_url_id(url_id)
                if resolved:
                    target_url = resolved
                    logger.debug(f"🔗 Resolved short URL ID: {url_id}")
                else:
                    target_url = None

            force_refresh = request.query.get("force", "false").lower() == "true"
            redirect_stream = (
                request.query.get("redirect_stream", "true").lower() == "true"
            )

            if not target_url:
                return web.Response(text="Missing 'url' or 'd' parameter", status=400)

            # Every new playback gets its own routing/session namespace. The
            # key is propagated into generated HLS/DASH URLs; stream requests
            # that already carry it keep it unchanged.
            stream_key = request.query.get("stream_key")
            is_rewritten_hls_segment = request.path.startswith("/proxy/hls/segment.")
            if not stream_key and not is_rewritten_hls_segment:
                source_key = self._stream_key_for_url(
                    request.query.get("orig_url") or target_url
                ) or "stream"
                stream_key = f"{source_key}-{secrets.token_hex(6)}"

            # Record stream activity
            is_segment = (
                request.path.startswith("/proxy/hls/segment.") or 
                request.path.startswith("/proxy/mpd/segment.") or 
                "segment." in request.path
            )
            display_url = target_url
            _shared.record_stream_activity(
                get_client_ip(request),
                display_url,
                request.headers.get("User-Agent", ""),
                is_segment=is_segment
            )

            # aiohttp already decodes query parameters once.
            # Do not unquote again here: URLs with embedded encoded separators
            # (for example Firebase Storage object paths using `%2F`) would be
            # corrupted and upstream would respond with HTTP 400.

            # --- GESTIONE HEADER ---
            combined_headers = {}

            # 0. Header passati come h_X=Y
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:]
                    combined_headers[header_name] = param_value

            # DUAL's browser test already resolved the final media URL and its
            # required headers. Do not run GenericHLSExtractor again: providers
            # can reject the Referer it guesses even though the raw URL works.
            if request.query.get("direct_hls") == "1":
                return await self._proxy_stream(
                    request,
                    target_url,
                    combined_headers,
                    bypass_warp=bypass_warp,
                    forced_proxy=selected_proxy,
                    force_direct=force_direct,
                    stream_key=stream_key,
                )

            extractor_key = request.query.get("extractor_key")
            captured_manifest = None
            if is_rewritten_hls_segment:
                extractor = None
                stream_url = target_url
                stream_headers = {}
                for header_name, header_value in combined_headers.items():
                    if header_name.lower() in {
                        "host",
                        "connection",
                        "cache-control",
                        "icy-metadata",
                        "accept-encoding",
                        "content-length",
                        "x-easyproxy-disable-ssl",
                    }:
                        continue
                    stream_headers[header_name] = header_value
            else:
                forced_host = request.query.get("host")
                extractor = await self.get_extractor(
                    target_url, combined_headers, host=forced_host, bypass_warp=bypass_warp
                )

                # The first resolver call identifies the extractor. Apply its
                # admin routing policy before the actual extraction, then use a
                # routing-specific cached extractor instance.
                resolved_key = self._extractor_key_for_instance(extractor)
                if not extractor_key or (resolved_key and not resolved_key.startswith("generic")):
                    extractor_key = resolved_key or extractor_key
                admin_warp_off, admin_proxy_off = get_extractor_routing_overrides(extractor_key)
                routing_changed = False
                if admin_warp_off and not bypass_warp:
                    bypass_warp = True
                    BYPASS_WARP_CONTEXT.set(True)
                    routing_changed = True
                if admin_proxy_off and not bypass_proxies:
                    bypass_proxies = True
                    BYPASS_PROXIES_CONTEXT.set(True)
                    routing_changed = True
                if routing_changed:
                    selected_proxy = None
                    SELECTED_PROXY_CONTEXT.set(None)
                    STRICT_PROXY_CONTEXT.set(False)
                    extractor = await self.get_extractor(
                        target_url, combined_headers, host=forced_host, bypass_warp=bypass_warp
                    )
                    resolved_key = self._extractor_key_for_instance(extractor)
                    if not extractor_key or (resolved_key and not resolved_key.startswith("generic")):
                        extractor_key = resolved_key or extractor_key

                # ✅ FIX CRITICO: Forza l'aggiornamento degli header dell'estrattore.
                # Siccome gli estrattori vengono memorizzati in self.extractors (cache),
                # se non aggiorniamo request_headers, i segmenti successivi userebbero
                # gli header del primo manifest caricato, ignorando h_Referer/h_Origin.
                if extractor:
                    extractor.request_headers = combined_headers


                # Passa il flag force_refresh all'estrattore
                result = await extractor.extract(
                    target_url,
                    force_refresh=force_refresh,
                    request_headers=combined_headers,
                    bypass_warp=bypass_warp,
                    proxy=request.query.get("proxy")
                )
                resolved_key = self._extractor_key_for_instance(extractor)
                if not extractor_key or (resolved_key and not resolved_key.startswith("generic")):
                    extractor_key = resolved_key or extractor_key
                stream_key = stream_key or self._stream_key_for_url(
                    request.query.get("orig_url") or target_url
                )
                bypass_warp = result.get("bypass_warp", bypass_warp)
                stream_url = result["destination_url"]
                stream_headers = result.get("request_headers", {})
                captured_manifest = result.get("captured_manifest")
                captured_manifests = result.get("captured_manifests") or {}
                force_disable_ssl = result.get("disable_ssl", False)
                force_direct = result.get("force_direct", force_direct)

                # Re-apply the admin policy after extraction as well: an
                # extractor result must not remove warp=off/proxy=off before
                # the manifest and segment URLs are generated.
                admin_warp_off, admin_proxy_off = get_extractor_routing_overrides(extractor_key)
                if admin_warp_off:
                    bypass_warp = True
                    BYPASS_WARP_CONTEXT.set(True)
                if admin_proxy_off:
                    bypass_proxies = True
                    BYPASS_PROXIES_CONTEXT.set(True)

                # Cattura e sanifica il proxy per evitare double-encoding (%253A -> %3A)
                raw_proxy = request.query.get("proxy") or result.get("selected_proxy")
                if raw_proxy and raw_proxy.lower() == "off":
                    raw_proxy = None
                if not raw_proxy and extractor:
                    raw_proxy = (
                        getattr(extractor, "last_used_proxy", None)
                        or getattr(extractor, "selected_proxy", None)
                        or getattr(extractor, "_session_proxy", None)
                        or getattr(extractor, "session_proxy", None)
                    )
                if raw_proxy:
                    # Sanifica e assegna alla variabile che verrà usata dopo
                    selected_proxy = urllib.parse.unquote(raw_proxy)
                    if "://" not in selected_proxy and "%3a" in selected_proxy.lower():
                        selected_proxy = urllib.parse.unquote(selected_proxy)
                    # ✅ FIX: Se bypass_warp è True e il proxy selezionato è WARP,
                    # ignoralo per evitare che un _session_proxy stantio su un estrattore
                    # cache-forzato (es. GenericHLSExtractor) prevalga su warp=off.
                    if bypass_warp and _shared.WARP_PROXY_URL and selected_proxy == _shared.WARP_PROXY_URL:
                        logger.debug(
                            "Ignoring stale WARP _session_proxy from extractor because bypass_warp=True"
                        )
                        selected_proxy = None
                    if selected_proxy and _config.is_warp_proxy_url(selected_proxy) and (
                        bypass_warp or not _config._get_dynamic_warp_enabled()
                    ):
                        logger.debug(
                            "Ignoring stale WARP route after policy reload: %s",
                            selected_proxy,
                        )
                        selected_proxy = None

                # ✅ FIX: Resetta SELECTED_PROXY_CONTEXT al valore effettivo.
                # get_preferred_proxy_for_url (chiamato dall'estrattore in _get_session)
                # setta questo context a un proxy (WARP, globale, o extractor-specific), ma
                # dopo aver deciso selected_proxy vogliamo che le chiamate successive a
                # get_proxy_for_url (es. da _proxy_stream) PARTANO DA QUESTO STATO, non
                # dal proxy scelto dall'estrattore.
                SELECTED_PROXY_CONTEXT.set(selected_proxy)

                if selected_proxy:
                    logger.debug(f"🎯 Final selected proxy for manifest: {selected_proxy}")

                if force_disable_ssl:
                    if "?" in stream_url:
                        stream_url += "&disable_ssl=1"
                    else:
                        stream_url += "?disable_ssl=1"


            # --- DASH NATIVO: Riscrive il manifest per segmenti proxati (senza conversione) ---
            if is_native_mpd:
                proxy_base = get_public_base_url(request)

                # Fetch original manifest if not already captured
                if not captured_manifest:
                    mpd_session, mpd_proxy_used = await self._get_proxy_session(
                        stream_url,
                        bypass_warp=bypass_warp,
                        forced_proxy=selected_proxy,
                        session_key=stream_key,
                    )
                    try:
                        async with mpd_session.get(stream_url, headers=stream_headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status != 200:
                                logger.error(
                                    "❌ Failed to fetch original MPD: status=%s [%s]",
                                    resp.status,
                                    request_log_context(
                                        request,
                                        stream_url,
                                        route=safe_log_route(mpd_proxy_used),
                                        extractor=extractor,
                                    ),
                                )
                                return web.Response(text=f"Failed to fetch original MPD: {resp.status}", status=resp.status)
                            captured_manifest = await resp.text()
                            stream_url = str(resp.url)
                    finally:
                        if mpd_session and not mpd_session.closed:
                            await mpd_session.close()

                if "indexRange" in captured_manifest:
                    try:
                        from utils.dash_ranges import expand_segment_bases, fetch_range
                        async def _fetch_sidx(url, range_str):
                            session, _ = await self._get_proxy_session(
                                url,
                                bypass_warp=bypass_warp,
                                forced_proxy=selected_proxy,
                                session_key=stream_key,
                            )
                            try:
                                return await fetch_range(session, url, stream_headers, range_str)
                            finally:
                                if session and not session.closed:
                                    await session.close()
                        captured_manifest = await expand_segment_bases(captured_manifest, stream_url, _fetch_sidx)
                    except Exception as e:
                        logger.warning(f"Failed to expand DASH SegmentBase indexes: {e}")

                rewritten_mpd = ManifestRewriter.rewrite_mpd_native(
                    manifest_content=captured_manifest,
                    mpd_url=stream_url,
                    proxy_base=proxy_base,
                    stream_headers=stream_headers,
                    clearkey_param=parse_clearkey_params(request),
                    bypass_warp=bypass_warp,
                    bypass_proxies=bypass_proxies,
                    forced_proxy=selected_proxy,
                    extractor_key=extractor_key,
                    stream_key=stream_key,
                    max_res=self._request_forces_max_res(request, extractor_key, "mpd"),
                )

                return web.Response(
                    text=rewritten_mpd,
                    content_type="application/dash+xml",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    }
                )

            # Se redirect_stream è False, restituisci il JSON con i dettagli (stile MediaFlow)
            if not redirect_stream:
                # Costruisci l'URL base del proxy
                proxy_base = get_public_base_url(request)

                mediaflow_endpoint = (
                    result.get("mediaflow_endpoint", "hls_proxy")
                    if not is_rewritten_hls_segment
                    else "hls_proxy"
                )

                # Determina l'endpoint corretto
                endpoint = "/proxy/hls/manifest.m3u8"

                # Check extension of the actual path, not the whole URL
                path_lower = urllib.parse.urlparse(stream_url).path.lower()
                is_direct_video = any(path_lower.endswith(ext) for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"])

                if mediaflow_endpoint == "proxy_stream_endpoint" or is_direct_video:
                    endpoint = "/proxy/stream"
                elif ".mpd" in path_lower or "manifest" in path_lower and "dash" in path_lower:
                    endpoint = "/proxy/mpd/manifest.m3u8"

                # Prepariamo i parametri per il JSON
                q_params = {}
                api_password = request.query.get("api_password")
                if api_password:
                    q_params["api_password"] = api_password
                if 'extractor_key' in locals() and extractor_key:
                    q_params["extractor_key"] = extractor_key
                if 'stream_key' in locals() and stream_key:
                    q_params["stream_key"] = stream_key
                if bypass_warp:
                    q_params["warp"] = "off"
                if bypass_proxies:
                    q_params["proxy"] = "off"
                if bypass_tor:
                    q_params["tor"] = "off"

                response_data = {
                    "destination_url": stream_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": mediaflow_endpoint,
                    "mediaflow_proxy_url": f"{proxy_base}{endpoint}",  # URL Pulito
                    "query_params": q_params,
                }
                return web.json_response(response_data)

            if captured_manifest and request.path.endswith("manifest.m3u8"):
                proxy_base = get_public_base_url(request)
                original_channel_url = request.query.get("orig_url") or request.query.get("url") or request.query.get("d", "")
                api_password = request.query.get("api_password")
                no_bypass = request.query.get("no_bypass") == "1"
                is_vavoo_req = check_vavoo_request(combined_headers, request, stream_url)
                disable_ssl = request.query.get("disable_ssl") == "1" or force_disable_ssl or is_vavoo_req

                rewritten_manifest = await ManifestRewriter.rewrite_manifest_urls(
                    manifest_content=captured_manifest,
                    base_url=stream_url,
                    proxy_base=proxy_base,
                    stream_headers=stream_headers,
                    original_channel_url=original_channel_url,
                    api_password=api_password,
                    get_extractor_func=lambda url, headers, host=None: self.get_extractor(url, headers, host, bypass_warp=bypass_warp),
                    no_bypass=no_bypass,
                    shorten_url_func=self.shorten_hls_url,
                    bypass_warp=bypass_warp,
                    bypass_proxies=bypass_proxies,
                    bypass_tor=bypass_tor,
                    disable_ssl=disable_ssl,
                    selected_proxy=selected_proxy,
                    force_direct=force_direct,
                    extractor_key=extractor_key if 'extractor_key' in locals() else request.query.get("extractor_key"),
                    stream_key=stream_key if 'stream_key' in locals() else request.query.get("stream_key"),
                    max_res=self._request_forces_max_res(
                        request,
                        extractor_key if 'extractor_key' in locals() else request.query.get("extractor_key"),
                        "hls",
                    ),
                )
                return web.Response(
                    text=rewritten_manifest,
                    headers={
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    },
                )

            # Aggiungi headers personalizzati da query params
            h_params_found = []
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:]
                    h_params_found.append(header_name)

                    # ✅ FIX: Rimuovi eventuali header duplicati (case-insensitive) presenti in stream_headers
                    # Questo assicura che l'header passato via query param (es. h_Referer) abbia la priorità
                    # e non vada in conflitto con quelli generati dagli estrattori (es. referer minuscolo).
                    keys_to_remove = [
                        k
                        for k in stream_headers.keys()
                        if k.lower() == header_name.lower()
                    ]
                    for k in keys_to_remove:
                        del stream_headers[k]

                    stream_headers[header_name] = param_value

            if h_params_found:
                logger.debug(
                    f"   Headers overridden by query params: {h_params_found}"
                )
            else:
                logger.debug("   No h_ params found in query string.")


            # Stream URL resolved
            # ✅ MPD/DASH handling
            # ✅ FIX: Refined MPD/DASH detection. Use specific patterns to avoid false positives
            # (e.g. "dashinripe" in URL being mistaken for a DASH manifest).
            is_mpd = ".mpd" in stream_url.lower() or "/dash/" in stream_url.lower()
            if is_mpd:
                # Convert MPD to HLS with server-side decryption
                logger.info(
                    "🔄 [Legacy Mode] Converting MPD to HLS [%s]",
                    request_log_context(request, stream_url, extractor=extractor),
                )

                if MPDToHLSConverter is None:
                    logger.error(
                        "❌ MPDToHLSConverter not available in legacy mode"
                    )
                    return web.Response(
                        text="Legacy MPD converter not available", status=503
                    )

                # Fetch the MPD once for simultaneous video/audio playlist
                # requests. This is in-flight coalescing only: no MPD or
                # segment body is retained after the request group finishes.
                mpd_key = self._mpd_fetch_key(
                    stream_url,
                    stream_headers,
                    bypass_warp,
                    bypass_proxies,
                    selected_proxy,
                    stream_key,
                )
                mpd_inflight = getattr(self, "_mpd_inflight", None)
                if mpd_inflight is None:
                    mpd_inflight = {}
                    self._mpd_inflight = mpd_inflight

                mpd_task = mpd_inflight.get(mpd_key)
                if mpd_task is None or mpd_task.done():
                    mpd_task = asyncio.create_task(
                        self._fetch_mpd_manifest(
                            request,
                            stream_url,
                            stream_headers,
                            bypass_warp,
                            bypass_proxies,
                            selected_proxy,
                            extractor,
                            stream_key,
                        )
                    )
                    mpd_inflight[mpd_key] = mpd_task

                    def _clear_mpd_task(done_task, key=mpd_key):
                        if getattr(self, "_mpd_inflight", {}).get(key) is done_task:
                            self._mpd_inflight.pop(key, None)

                    mpd_task.add_done_callback(_clear_mpd_task)
                else:
                    logger.debug(
                        "[MPD] joining in-flight fetch [%s]",
                        request_log_context(
                            request,
                            stream_url,
                            route=safe_log_route(selected_proxy),
                            extractor=extractor,
                        ),
                    )

                manifest_content, final_mpd_url = await asyncio.shield(mpd_task)
                if manifest_content is None:
                    logger.error(
                        "❌ Failed to fetch MPD manifest after all attempts [%s]",
                        request_log_context(request, stream_url, extractor=extractor),
                    )
                    return web.Response(
                        text="Failed to fetch MPD manifest after all attempts",
                        status=502,
                    )

                # Build proxy base URL
                proxy_base = get_public_base_url(request)

                # Build params string with headers
                params = "".join(
                    [
                        f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                        for key, value in stream_headers.items()
                    ]
                )

                # Add api_password if present
                api_password = request.query.get("api_password")
                if api_password:
                    params += f"&api_password={api_password}"

                clearkey_param = parse_clearkey_params(request)

                if clearkey_param:
                    drm_token = request.query.get("drm_token") or seal_clearkey(
                        clearkey_param
                    )
                    params += (
                        f"&drm_token={urllib.parse.quote(drm_token, safe='')}"
                    )

                # Pass 'ext' param if present (e.g. ext=ts)
                ext_param = request.query.get("ext")
                if ext_param:
                    params += f"&ext={ext_param}"

                # Propagate warp=off, proxy=off and tor=off to generated HLS URLs
                if bypass_warp:
                    params += "&warp=off"
                if bypass_proxies:
                    params += "&proxy=off"
                if bypass_tor:
                    params += "&tor=off"
                if self._request_forces_max_res(request, extractor_key, "mpd"):
                    params += "&max_res=true"
                if extractor_key:
                    params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
                if stream_key:
                    params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"

                # Check if requesting specific representation
                rep_id = request.query.get("rep_id")

                converter = MPDToHLSConverter()
                if rep_id:
                    # Generate media playlist for specific representation
                    # Use final_mpd_url (after redirects) for segment URL construction
                    hls_content = await asyncio.to_thread(
                        converter.convert_media_playlist,
                        manifest_content,
                        rep_id,
                        proxy_base,
                        final_mpd_url,
                        params,
                        clearkey_param,
                    )
                else:
                    # Generate master playlist
                    # Use final_mpd_url (after redirects) for segment URL construction
                    hls_content = await asyncio.to_thread(
                        converter.convert_master_playlist,
                        manifest_content,
                        proxy_base,
                        final_mpd_url,
                        params,
                    )

                return web.Response(
                    text=hls_content,
                    content_type="application/vnd.apple.mpegurl",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    },
                )

            # Procedi con il proxy dello stream (passando l'eventuale bypass_warp attivato dall'estrattore e il proxy selezionato)
            return await self._proxy_stream(request, stream_url, stream_headers, bypass_warp=bypass_warp, forced_proxy=selected_proxy, force_direct=force_direct, extractor_key=extractor_key, stream_key=stream_key)

        except ProxyDeadRetryError:
            if getattr(request, '_extraction_retried', False):
                logger.warning("Re-extraction already attempted for %s, not retrying again", target_url)
                raise
            else:
                request._extraction_retried = True
                extraction_url = request.query.get("orig_url") or target_url
                logger.warning("Proxy died during playlist fetch, re-extracting %s (orig URL: %s)", target_url, extraction_url)
                extractor2 = None
                try:
                    extractor2 = await self.get_extractor(
                        extraction_url, combined_headers,
                        host=request.query.get("host"), bypass_warp=bypass_warp
                    )
                    if not extractor2:
                        logger.warning("No extractor found for %s during re-extraction", extraction_url)
                        return web.Response(text="Re-extraction failed: no extractor found", status=502)
                    extractor2.request_headers = combined_headers
                    result2 = await extractor2.extract(
                        extraction_url, force_refresh=True,
                        request_headers=combined_headers, bypass_warp=bypass_warp,
                        proxy=None,
                    )
                    stream_url2 = result2["destination_url"]
                    stream_headers2 = result2.get("request_headers", {})
                    selected_proxy2 = result2.get("selected_proxy")
                    if not selected_proxy2 and extractor2:
                        selected_proxy2 = (
                            getattr(extractor2, "last_used_proxy", None)
                            or getattr(extractor2, "selected_proxy", None)
                            or getattr(extractor2, "_session_proxy", None)
                            or getattr(extractor2, "session_proxy", None)
                        )
                    force_direct2 = result2.get("force_direct", force_direct)
                    original_proxy = request.query.get("proxy")
                    if original_proxy:
                        original_proxy = urllib.parse.unquote(original_proxy)
                        if "://" not in original_proxy and "%3a" in original_proxy.lower():
                            original_proxy = urllib.parse.unquote(original_proxy)
                    if not selected_proxy2 and original_proxy:
                        new_proxy = get_proxy_for_url(stream_url2, bypass_warp=bypass_warp)
                        if new_proxy and new_proxy != original_proxy:
                            logger.info(
                                "Rotating to new proxy: %s [%s]",
                                safe_log_route(new_proxy),
                                request_log_context(
                                    request,
                                    stream_url2,
                                    route=safe_log_route(new_proxy),
                                    extractor=extractor2,
                                ),
                            )
                            selected_proxy2 = new_proxy
                        else:
                            selected_proxy2 = original_proxy
                            force_direct2 = False
                    logger.info(
                        "Re-extraction success [%s]",
                        request_log_context(
                            request,
                            stream_url2,
                            route=safe_log_route(selected_proxy2),
                            extractor=extractor2,
                        ),
                    )
                    return await self._proxy_stream(request, stream_url2, stream_headers2, bypass_warp=bypass_warp, forced_proxy=selected_proxy2, force_direct=force_direct2, extractor_key=extractor_key, stream_key=stream_key)
                except Exception as retry_err:
                    logger.error(
                        "Re-extraction failed: %s [%s]",
                        retry_err,
                        request_log_context(request, target_url, extractor=extractor),
                    )
                    return web.Response(text="Re-extraction failed", status=502)

        except Exception as e:
            error_msg = str(e).lower()
            is_expired_embed = is_expired_embed_error(error_msg)
            is_not_found = "404" in error_msg or "not found" in error_msg
            is_temporary_error = any(
                x in error_msg
                for x in [
                    "403", "forbidden", "502", "bad gateway", "timeout", "connection",
                    "temporarily unavailable", "no usable proxy route",
                    "no proxy route available", "direct fallback disabled",
                ]
            ) or type(e).__name__ == "ExtractorError"
            is_corrupt = "corrupt" in error_msg or "not available" in error_msg
            extractor_name = extractor_name_for_log(extractor)
            raw_log_proxy = request.query.get("proxy") or (
                getattr(extractor, "last_used_proxy", None)
                or getattr(extractor, "selected_proxy", None)
                or getattr(extractor, "_session_proxy", None)
                or getattr(extractor, "session_proxy", None)
            )
            if raw_log_proxy and str(raw_log_proxy).lower() == "off":
                raw_log_proxy = None
            error_context = request_log_context(
                request,
                target_url,
                route=safe_log_route(raw_log_proxy),
                extractor=extractor,
            )

            if is_expired_embed:
                logger.info(
                    "Expired VixSrc embed URL rejected: %s [%s]",
                    e,
                    error_context,
                )
                return web.Response(text=str(e), status=410)
            if is_corrupt:
                logger.warning(
                    "⚠️ %s: Content is corrupt or not available - %s [%s]",
                    extractor_name,
                    e,
                    error_context,
                )
                return web.Response(text=f"Content corrupt or not available: {str(e)}", status=404)
            if is_not_found:
                logger.warning(
                    "🔍 %s: Content not found (404) - %s [%s]",
                    extractor_name,
                    e,
                    error_context,
                )
                return web.Response(text=f"Content not found: {str(e)}", status=404)
            if is_temporary_error:
                logger.warning(
                    "📡 %s: Service temporarily unavailable - %s [%s]",
                    extractor_name,
                    e,
                    error_context,
                )
                return web.Response(text=f"Service temporarily unavailable: {str(e)}", status=503)

            logger.critical("❌ Critical error: %s [%s]", e, error_context)
            logger.exception("Error in proxy request [%s]", error_context)
            return web.Response(text=f"Proxy error: {str(e)}", status=500)
        finally:
            BYPASS_WARP_CONTEXT.reset(token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            BYPASS_TOR_CONTEXT.reset(tor_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(proxy_token)
            STRICT_PROXY_CONTEXT.reset(strict_proxy_token)
            # Shared extractor lifecycle belongs to the registry owner.
