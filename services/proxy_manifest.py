import asyncio
import time
import urllib.parse
import aiohttp
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    web,
    check_password,
    get_client_ip,
    BYPASS_WARP_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    ManifestRewriter,
    check_vavoo_request,
    should_use_short_manifest_urls,
    parse_clearkey_params,
    MPDToHLSConverter,
    get_ssl_setting_for_url,
    AioProxyError,
    PyProxyError,
    ClientConnectionError,
    ProxyDeadRetryError,
    get_proxy_for_url,
    is_expired_embed_error,
    extractor_name_for_log,
)


class HLSProxyManifestHandlerMixin:

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
        
        selected_proxy = None
        raw_proxy = request.query.get("proxy")
        if raw_proxy and raw_proxy.lower() != "off":
            selected_proxy = urllib.parse.unquote(raw_proxy)
            if "://" not in selected_proxy and "%3a" in selected_proxy.lower():
                selected_proxy = urllib.parse.unquote(selected_proxy)
        proxy_token = SELECTED_PROXY_CONTEXT.set(selected_proxy)
        strict_proxy_token = STRICT_PROXY_CONTEXT.set(bool(selected_proxy))
        force_direct = self._should_force_direct_from_query(request)

        try:
            extractor = None

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

            captured_manifest = None
            is_rewritten_hls_segment = request.path.startswith("/proxy/hls/segment.")
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
                extractor_key = request.query.get("extractor_key")
                stream_key = request.query.get("stream_key")
            else:
                extractor = await self.get_extractor(target_url, combined_headers, bypass_warp=bypass_warp)

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
                extractor_key = self._extractor_key_for_instance(extractor)
                stream_key = self._stream_key_for_url(request.query.get("orig_url") or target_url)
                bypass_warp = result.get("bypass_warp", bypass_warp)
                stream_url = result["destination_url"]
                stream_headers = result.get("request_headers", {})
                captured_manifest = result.get("captured_manifest")
                captured_manifests = result.get("captured_manifests") or {}
                force_disable_ssl = result.get("disable_ssl", False)
                force_direct = result.get("force_direct", force_direct)

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

                if selected_proxy:
                    logger.debug(f"🎯 Final selected proxy for manifest: {selected_proxy}")

                if force_disable_ssl:
                    if "?" in stream_url:
                        stream_url += "&disable_ssl=1"
                    else:
                        stream_url += "?disable_ssl=1"


            # --- DASH NATIVO: Riscrive il manifest per segmenti proxati (senza conversione) ---
            if is_native_mpd:
                scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                host = request.headers.get("X-Forwarded-Host", request.host)
                proxy_base = f"{scheme}://{host}"

                # Fetch original manifest if not already captured
                if not captured_manifest:
                    mpd_session, _ = await self._get_proxy_session(
                        stream_url, bypass_warp=bypass_warp, forced_proxy=selected_proxy
                    )
                    async with mpd_session.get(stream_url, headers=stream_headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status != 200:
                            return web.Response(text=f"Failed to fetch original MPD: {resp.status}", status=resp.status)
                        captured_manifest = await resp.text()
                        stream_url = str(resp.url)

                # Encode DASH routing state into base64 token (stateless, no server-side session)
                from services.proxy_dash import _encode_dash_state
                session_id = _encode_dash_state(
                    stream_url.rsplit('/', 1)[0] + '/',
                    stream_headers,
                    clearkey=parse_clearkey_params(request)
                )

                rewritten_mpd = ManifestRewriter.rewrite_mpd_native(
                    manifest_content=captured_manifest,
                    mpd_url=stream_url,
                    proxy_base=proxy_base,
                    stream_headers=stream_headers,
                    session_id=session_id
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
                scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                host = request.headers.get("X-Forwarded-Host", request.host)
                proxy_base = f"{scheme}://{host}"

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

                response_data = {
                    "destination_url": stream_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": mediaflow_endpoint,
                    "mediaflow_proxy_url": f"{proxy_base}{endpoint}",  # URL Pulito
                    "query_params": q_params,
                }
                return web.json_response(response_data)

            if captured_manifest and request.path.endswith("manifest.m3u8"):
                scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                host = request.headers.get("X-Forwarded-Host", request.host)
                proxy_base = f"{scheme}://{host}"
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
                    disable_ssl=disable_ssl,
                    selected_proxy=selected_proxy,
                    force_direct=force_direct,
                    extractor_key=extractor_key if 'extractor_key' in locals() else request.query.get("extractor_key"),
                    stream_key=stream_key if 'stream_key' in locals() else request.query.get("stream_key"),
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
            # ✅ MPD/DASH handling based on MPD_MODE
            # ✅ FIX: Refined MPD/DASH detection. Use specific patterns to avoid false positives
            # (e.g. "dashinripe" in URL being mistaken for a DASH manifest).
            _MPD_MODE = _shared.MPD_MODE
            is_mpd = ".mpd" in stream_url.lower() or "/dash/" in stream_url.lower()
            if is_mpd:
                if _MPD_MODE == "ffmpeg" and self.ffmpeg_manager:
                    # FFmpeg transcoding mode
                    logger.info(
                        f"🔄 [FFmpeg Mode] Routing MPD stream: {stream_url}"
                    )

                    clearkey_param = parse_clearkey_params(request)

                    playlist_rel_path = await self.ffmpeg_manager.get_stream(
                        stream_url, stream_headers, clearkey=clearkey_param
                    )

                    if playlist_rel_path:
                        # Construct local URL for the FFmpeg stream
                        scheme = request.headers.get(
                            "X-Forwarded-Proto", request.scheme
                        )
                        host = request.headers.get("X-Forwarded-Host", request.host)
                        local_url = (
                            f"{scheme}://{host}/ffmpeg_stream/{playlist_rel_path}"
                        )

                        # Generate Master Playlist for compatibility
                        master_playlist = (
                            "#EXTM3U\n"
                            "#EXT-X-VERSION:3\n"
                            '#EXT-X-STREAM-INF:BANDWIDTH=6000000,NAME="Live"\n'
                            f"{local_url}\n"
                        )

                        return web.Response(
                            text=master_playlist,
                            content_type="application/vnd.apple.mpegurl",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Cache-Control": "no-cache",
                            },
                        )
                    else:
                        logger.error("❌ FFmpeg failed to start")
                        return web.Response(
                            text="FFmpeg failed to process stream", status=502
                        )
                else:
                    # Legacy mode: use mpd_converter for HLS conversion with server-side decryption
                    logger.info(
                        f"🔄 [Legacy Mode] Converting MPD to HLS: {stream_url}"
                    )

                    if MPDToHLSConverter is None:
                        logger.error(
                            "❌ MPDToHLSConverter not available in legacy mode"
                        )
                        return web.Response(
                            text="Legacy MPD converter not available", status=503
                        )

                    # Fetch the MPD manifest with proxy support
                    ssl_context = None
                    disable_ssl = get_ssl_setting_for_url(stream_url)
                    if disable_ssl:
                        ssl_context = False

                    manifest_content = None
                    retries = 2
                    for attempt in range(retries):
                        mpd_proxy = None
                        try:
                            # Use helper to get proxy-enabled session
                            mpd_session, mpd_proxy = await self._get_proxy_session(
                                stream_url, bypass_warp=bypass_warp, forced_proxy=selected_proxy
                            )
                            if mpd_proxy:
                                logger.info(
                                    f"📡 [MPD] Attempt {attempt+1}/{retries} via proxy: {mpd_proxy}"
                                )

                            async with mpd_session.get(
                                stream_url,
                                headers=stream_headers,
                                ssl=ssl_context,
                                allow_redirects=True,
                            ) as resp:
                                # Capture final URL after redirects
                                final_mpd_url = str(resp.url)
                                if final_mpd_url != stream_url:
                                    logger.info(f"↪️ MPD redirected to: {final_mpd_url}")

                                if resp.status != 200:
                                    error_text = await resp.text()
                                    logger.error(f"❌ Failed to fetch MPD (Status {resp.status}) at {stream_url}")
                                    if attempt == retries - 1:
                                        return web.Response(
                                            text=f"Failed to fetch MPD: {resp.status}\nResponse: {error_text[:1000]}",
                                            status=502,
                                        )
                                    await asyncio.sleep(1)
                                    continue

                                manifest_content = await resp.text()
                                break # Success

                        except (AioProxyError, PyProxyError, asyncio.TimeoutError, ClientConnectionError, OSError) as e:
                            is_proxy = isinstance(e, (AioProxyError, PyProxyError))
                            # Consider ClientConnectionError/OSError as proxy errors if a proxy was used
                            if not is_proxy and mpd_proxy and isinstance(e, (ClientConnectionError, OSError)):
                                is_proxy = True

                            err_type = "Proxy" if is_proxy else "Timeout"
                            logger.warning(f"⚠️ [MPD] {err_type} error at attempt {attempt+1}: {e}")

                            # Mark local proxy as dead if it failed
                            if mpd_proxy and "127.0.0.1" in mpd_proxy:
                                self._mark_proxy_dead_if_allowed(
                                    mpd_proxy,
                                    extractor_key=request.query.get("extractor_key"),
                                )
                                # Also clear the cached session for this proxy
                                if mpd_proxy in self.proxy_sessions:
                                    logger.info(f"   [MPD] Removing broken proxy session from cache: {mpd_proxy}")
                                    self.proxy_sessions.pop(mpd_proxy, None)

                            # Clear sticky context if it's a proxy error
                            if is_proxy and SELECTED_PROXY_CONTEXT.get() and not STRICT_PROXY_CONTEXT.get():
                                logger.info("   [MPD] Clearing sticky proxy context due to ProxyError")
                                SELECTED_PROXY_CONTEXT.set(None)

                            if attempt < retries - 1:
                                logger.info("   [MPD] Retrying...")
                                await asyncio.sleep(1)
                            else:
                                return web.Response(text=f"MPD unreachable: {e}", status=502)
                        except Exception as e:
                            logger.error(f"❌ [MPD] Unexpected error at attempt {attempt+1}: {e}")
                            if attempt == retries - 1:
                                return web.Response(text=f"Unexpected error fetching MPD: {e}", status=500)
                            await asyncio.sleep(1)

                    if manifest_content is None:
                         return web.Response(text="Failed to fetch MPD manifest after all attempts", status=502)

                    # Build proxy base URL
                    scheme = request.headers.get(
                        "X-Forwarded-Proto", request.scheme
                    )
                    host = request.headers.get("X-Forwarded-Host", request.host)
                    proxy_base = f"{scheme}://{host}"

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
                        params += f"&clearkey={clearkey_param}"

                    # Pass 'ext' param if present (e.g. ext=ts)
                    ext_param = request.query.get("ext")
                    if ext_param:
                        params += f"&ext={ext_param}"

                    # Propagate warp=off and proxy=off to generated HLS URLs
                    if bypass_warp:
                        params += "&warp=off"
                    if bypass_proxies:
                        params += "&proxy=off"

                    # Check if requesting specific representation
                    rep_id = request.query.get("rep_id")

                    converter = MPDToHLSConverter()
                    if rep_id:
                        # Generate media playlist for specific representation
                        # Use final_mpd_url (after redirects) for segment URL construction
                        hls_content = converter.convert_media_playlist(
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
                        hls_content = converter.convert_master_playlist(
                            manifest_content, proxy_base, final_mpd_url, params
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
            return await self._proxy_stream(request, stream_url, stream_headers, bypass_warp=bypass_warp, forced_proxy=selected_proxy, force_direct=force_direct)

        except ProxyDeadRetryError:
            if getattr(request, '_extraction_retried', False):
                logger.warning("Re-extraction already attempted for %s, not retrying again", target_url)
                raise
            else:
                request._extraction_retried = True
                extraction_url = request.query.get("orig_url") or target_url
                logger.warning("Proxy died during playlist fetch, re-extracting %s (orig URL: %s)", target_url, extraction_url)
                try:
                    extractor2 = await self.get_extractor(extraction_url, combined_headers, bypass_warp=bypass_warp)
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
                            logger.info("Rotating to new proxy: %s", new_proxy)
                            selected_proxy2 = new_proxy
                        else:
                            selected_proxy2 = original_proxy
                            force_direct2 = False
                    logger.info("Re-extraction success: %s", stream_url2[:80])
                    return await self._proxy_stream(request, stream_url2, stream_headers2, bypass_warp=bypass_warp, forced_proxy=selected_proxy2, force_direct=force_direct2)
                except Exception as retry_err:
                    logger.error("Re-extraction failed: %s", retry_err)
                    return web.Response(text="Re-extraction failed", status=502)

        except Exception as e:
            error_msg = str(e).lower()
            is_expired_embed = is_expired_embed_error(error_msg)
            is_not_found = "404" in error_msg or "not found" in error_msg
            is_temporary_error = any(
                x in error_msg
                for x in ["403", "forbidden", "502", "bad gateway", "timeout", "connection", "temporarily unavailable"]
            )
            is_corrupt = "corrupt" in error_msg or "not available" in error_msg
            extractor_name = extractor_name_for_log(extractor)

            if is_expired_embed:
                logger.info("Expired VixSrc embed URL rejected: %s", str(e))
                return web.Response(text=str(e), status=410)
            if is_corrupt:
                logger.warning(f"⚠️ {extractor_name}: Content is corrupt or not available - {str(e)}")
                return web.Response(text=f"Content corrupt or not available: {str(e)}", status=404)
            if is_not_found:
                logger.warning(f"🔍 {extractor_name}: Content not found (404) - {str(e)}")
                return web.Response(text=f"Content not found: {str(e)}", status=404)
            if is_temporary_error:
                logger.warning(f"📡 {extractor_name}: Service temporarily unavailable - {str(e)}")
                return web.Response(text=f"Service temporarily unavailable: {str(e)}", status=503)

            logger.critical(f"❌ Critical error with {extractor_name}: {e}")
            logger.exception(f"Error in proxy request: {str(e)}")
            return web.Response(text=f"Proxy error: {str(e)}", status=500)
        finally:
            BYPASS_WARP_CONTEXT.reset(token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(proxy_token)
            STRICT_PROXY_CONTEXT.reset(strict_proxy_token)
