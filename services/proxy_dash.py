import time
import aiohttp
from urllib.parse import urljoin
from config import STRICT_PROXY_CONTEXT, should_allow_direct_fallback
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    web,
    check_password,
    get_ssl_setting_for_url,
    get_proxy_for_url,
    mark_proxy_dead,
    decrypt_segment,
    is_browser_key_request,
    fetch_browser_backed_key,
    binascii,
)
from services.secure_state import open_state, seal_state


def _encode_dash_state(base_url: str, headers: dict, clearkey: str | None) -> str:
    """Seal DASH routing state into a stateless, authenticated token."""
    return seal_state({
        "b": base_url,
        "h": headers,
        "k": clearkey,
    }, "dash")


def _decode_dash_state(token: str) -> tuple[str, dict, str | None] | None:
    """Open a stateless, authenticated DASH routing token."""
    data = open_state(token, "dash")
    if not data:
        return None
    return data.get("b", ""), data.get("h", {}), data.get("k")


class HLSProxyDashMixin:

    async def handle_dash_segment(self, request):
        """Proxy for native DASH segments with optional ClearKey decryption. Stateless."""
        token = request.match_info.get("session_id")
        path = request.match_info.get("tail")

        decoded = _decode_dash_state(token) if token else None
        if not decoded:
            return web.Response(text="Invalid or missing DASH state token", status=400)

        base_url, headers, clearkey = decoded
        if not base_url:
            return web.Response(text="Missing base_url in DASH state", status=400)

        segment_url = urljoin(base_url, path)

        # Parse clearkey into KID and KEY for decrypter
        kid, key = None, None
        if clearkey and ":" in clearkey:
            parts = clearkey.split(":", 1)
            kid, key = parts[0], parts[1]

        try:
            # Check if it's an initialization segment
            is_init = "init" in path.lower() or "header" in path.lower()

            # Fetch segment
            _session = await self._get_session(url=segment_url)
            async with _session.get(segment_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status not in [200, 206]:
                    return web.Response(status=resp.status)

                # ClearKey path: must read full segment for decryption
                if not is_init and kid and key and decrypt_segment:
                    content = await resp.read()
                    init_url = None
                    dir_path = base_url.rstrip("/")
                    for candidate in ("init.m4s", "init.mp4", "header.m4s", "header.mp4"):
                        candidate_url = urljoin(base_url, candidate)
                        if candidate_url.startswith(dir_path):
                            init_url = candidate_url
                            break

                    if init_url:
                        try:
                            _init_session = await self._get_session(url=init_url)
                            async with _init_session.get(init_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as init_resp:
                                if init_resp.status in [200, 206]:
                                    init_segment = await init_resp.read()
                                    try:
                                        decrypted = decrypt_segment(init_segment or b"", content, kid, key)
                                        return web.Response(body=decrypted, content_type=resp.content_type)
                                    except Exception as e:
                                        logger.warning(f"DASH decryption failed for {path}: {e}. Falling back to direct proxy.")
                        except Exception as e:
                            logger.debug(f"DASH init re-fetch failed for {path}: {e}")

                    return web.Response(body=content, content_type=resp.content_type)

                # No ClearKey: stream chunk-by-chunk without buffering
                response = web.StreamResponse(status=resp.status, headers={
                    "Content-Type": resp.content_type or "video/mp4",
                    "Access-Control-Allow-Origin": "*",
                })
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response

        except Exception as e:
            logger.error(f"Error proxying DASH segment {path}: {e}")
            return web.Response(status=502)

    async def handle_key_request(self, request):
        """✅ NUOVO: Gestisce richieste per chiavi AES-128"""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        bypass_warp = request.query.get("warp", "").lower() == "off"

        # 1. Gestione chiave statica (da MPD converter)
        static_key = request.query.get("static_key")
        if static_key:
            try:
                key_bytes = binascii.unhexlify(static_key)
                return web.Response(
                    body=key_bytes,
                    content_type="application/octet-stream",
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            except Exception as e:
                logger.error(f"❌ Error decoding static key: {e}")
                return web.Response(text="Invalid static key", status=400)

        # 2. Gestione proxy chiave remota
        key_url = request.query.get("key_url")

        if not key_url:
            return web.Response(
                text="Missing key_url or static_key parameter", status=400
            )

        session = None
        session_need_close = False
        try:
            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            original_channel_url = request.query.get("original_channel_url")

            is_browser_key = is_browser_key_request(key_url, original_channel_url)
            if is_browser_key:
                try:
                    browser_key = await fetch_browser_backed_key(
                        self.extractors,
                        key_url,
                        original_channel_url,
                        self.get_extractor,
                    )
                    if browser_key:
                        logger.info("AES key served from browser-backed provider cache (%d bytes)", len(browser_key))
                        return web.Response(
                            body=browser_key,
                            content_type="application/octet-stream",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Headers": "*",
                                "Cache-Control": "no-cache, no-store, must-revalidate",
                            },
                        )
                except Exception as browser_key_exc:
                    logger.warning(
                        f"Browser-backed key fetch failed, falling back to direct request: {browser_key_exc}"
                    )


            # Inizializza gli header esclusivamente da quelli passati dinamicamente
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    # ✅ FIX: Rimuovi header Range per le richieste di chiavi.
                    if header_name.lower() == "range":
                        continue
                    if header_name.lower() in {"x-direct-connection", "x-force-direct"}:
                        continue
                    headers[header_name] = param_value

            logger.debug(f"🔐 Fetching AES key from: {key_url}")
            logger.debug(f"   -> with headers: {headers}")

            # ✅ Use pooled session for better performance
            proxy_used = None
            raw_proxy = request.query.get("proxy") or None
            forced_proxy = raw_proxy
            if raw_proxy and raw_proxy.lower() == "off":
                forced_proxy = None
                _shared.BYPASS_PROXIES_CONTEXT.set(True)
            bypass_warp = request.query.get("warp", "").lower() == "off"

            _GLOBAL_PROXIES = _shared.GLOBAL_PROXIES
            _ENABLE_WARP = _shared.ENABLE_WARP
            _TRANSPORT_ROUTES = _shared.TRANSPORT_ROUTES

            if self._should_force_direct_from_query(request):
                session = await self._get_session(url=key_url)
                logger.debug("Using direct session for AES key request (forced)")
            else:
                session, proxy_used = await self._get_proxy_session(
                    key_url, bypass_warp=bypass_warp, forced_proxy=forced_proxy
                )
                session_need_close = proxy_used is not None
                if proxy_used:
                    logger.info(f"🔐 [Key Proxy] Routing through: {proxy_used}")
                elif (
                    forced_proxy
                    or _GLOBAL_PROXIES
                    or (_ENABLE_WARP and not bypass_warp)
                    or any(
                        route.get("proxy")
                        and route.get("url", "").lower() in key_url.lower()
                        for route in _TRANSPORT_ROUTES
                    )
                ):
                    logger.warning(f"🔐 [Key Proxy] NO PROXY assigned for: {key_url}")
                else:
                    logger.info(f"🔐 [Key Proxy] Using direct session for: {key_url}")

            secret_key = headers.pop("X-Secret-Key", None)

            # Calcola X-Key-Timestamp, X-Key-Nonce, X-Fingerprint, e X-Key-Path se abbiamo la secret_key
            if secret_key and "/key/" in key_url:
                # Get user agent from X-User-Agent header or fall back to User-Agent
                user_agent = (
                    headers.get("X-User-Agent")
                    or headers.get("User-Agent")
                    or headers.get("user-agent")
                )
                nonce_result = await self._compute_key_headers(
                    key_url, secret_key, user_agent
                )
                if nonce_result:
                    ts, nonce, fingerprint, key_path = nonce_result
                    headers["X-Key-Timestamp"] = str(ts)
                    headers["X-Key-Nonce"] = str(nonce)
                    headers["X-Fingerprint"] = fingerprint
                    headers["X-Key-Path"] = key_path
                    logger.debug(
                        f"🔐 Computed key headers: ts={ts}, nonce={nonce}, fingerprint={fingerprint}, key_path={key_path}"
                    )
                else:
                    logger.warning(f"⚠️ Could not compute key headers for {key_url}")

            # Caso 'auth' - URL che contengono 'auth' richiedono headers speciali
            if "auth" in key_url.lower():
                logger.debug(
                    f"🔐 Detected 'auth' key URL, ensuring special headers are present"
                )
                if "X-User-Agent" not in headers:
                    headers["X-User-Agent"] = headers.get(
                        "User-Agent", headers.get("user-agent", "Mozilla/5.0")
                    )
                logger.debug(
                    f"🔐 Auth key headers: Authorization={'***' if headers.get('Authorization') else 'missing'}, X-Channel-Key={headers.get('X-Channel-Key', 'missing')}, X-User-Agent={headers.get('X-User-Agent', 'missing')}"
                )

            disable_ssl = get_ssl_setting_for_url(key_url, _TRANSPORT_ROUTES)
            try:
                async with session.get(key_url, headers=headers, ssl=not disable_ssl, allow_redirects=False, timeout=15) as resp:
                    if resp.status == 200 or resp.status == 206:
                        key_data = await resp.read()
                        logger.debug(
                            f"✅ AES key fetched successfully: {len(key_data)} bytes"
                        )

                        # Warn if key size is unexpected (AES-128 = 16 bytes)
                        if len(key_data) != 16 and is_browser_key:
                            logger.warning(
                                f"Browser-backed AES key response is {len(key_data)} bytes (expected 16). "
                                f"The CDN may have returned an error page instead of the key. "
                                f"Session cookies may be missing."
                            )

                        return web.Response(
                            body=key_data,
                            content_type="application/octet-stream",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Headers": "*",
                                "Cache-Control": "no-cache, no-store, must-revalidate",
                            },
                        )
                    else:
                        if request.transport.is_closing():
                            return web.Response(status=499)
                        logger.error(f"❌ Key fetch failed with status: {resp.status}")
                        if proxy_used and not forced_proxy:
                            self._mark_proxy_dead_if_allowed(
                                proxy_used,
                                extractor_key=request.query.get("extractor_key"),
                            )
                            new_proxy = get_proxy_for_url(key_url, bypass_warp=bypass_warp)
                            if new_proxy and new_proxy != proxy_used:
                                logger.info(f"🔐 Key fetch failed via proxy {proxy_used}, trying rotated proxy: {new_proxy}")
                                fallback_session = None
                                try:
                                    fallback_session, _ = await self._get_proxy_session(key_url, bypass_warp=bypass_warp, forced_proxy=new_proxy)
                                    async with fallback_session.get(key_url, headers=headers, ssl=not disable_ssl, allow_redirects=False, timeout=10) as rot_resp:
                                        if rot_resp.status in (200, 206):
                                            key_data = await rot_resp.read()
                                            return web.Response(
                                                body=key_data,
                                                content_type="application/octet-stream",
                                                headers={
                                                    "Access-Control-Allow-Origin": "*",
                                                    "Access-Control-Allow-Headers": "*",
                                                    "Cache-Control": "no-cache, no-store, must-revalidate",
                                                },
                                            )
                                except Exception as fallback_e:
                                    logger.error(f"❌ Key fetch fallback via rotated proxy {new_proxy} failed: {fallback_e}")
                                finally:
                                    if fallback_session and not fallback_session.closed:
                                        await fallback_session.close()
                            elif not new_proxy and (forced_proxy or STRICT_PROXY_CONTEXT.get()):
                                logger.warning("🔐 Strict proxy mode: no fallback proxy, skipping direct")
                                return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)

                        if forced_proxy or STRICT_PROXY_CONTEXT.get():
                            logger.warning("🔐 Strict proxy mode: skipping direct fallback")
                            return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                        logger.warning("🔐 Trying direct connection as final fallback for AES key...")
                        try:
                            _key_session = await self._get_session(url=key_url)
                            async with _key_session.get(key_url, headers=headers, ssl=not disable_ssl, allow_redirects=False, timeout=10) as direct_resp:
                                if direct_resp.status in (200, 206):
                                    key_data = await direct_resp.read()
                                    return web.Response(
                                        body=key_data,
                                        content_type="application/octet-stream",
                                        headers={
                                            "Access-Control-Allow-Origin": "*",
                                            "Access-Control-Allow-Headers": "*",
                                            "Cache-Control": "no-cache, no-store, must-revalidate",
                                        },
                                    )
                        except Exception as direct_e:
                            logger.error(f"❌ Key fetch final direct fallback failed: {direct_e}")

                        # --- LOGICA DI INVALIDAZIONE AUTOMATICA ---
                        url_param = request.query.get("original_channel_url")
                        if url_param:
                            extractor = None
                            try:
                                extractor = await self.get_extractor(url_param, {})
                                if hasattr(extractor, "invalidate_cache_for_url"):
                                    await extractor.invalidate_cache_for_url(url_param)
                            except Exception as cache_e:
                                logger.error(
                                    f"⚠️ Error during automatic cache invalidation: {cache_e}"
                                )
                            finally:
                                _ek = self._extractor_key_for_instance(extractor) if extractor else None
                                if _ek and _ek in self.extractors:
                                    self.extractors.pop(_ek, None)
                                    self._extractor_atimes.pop(_ek, None)
                                    for _sr in [r for r in self._extractor_stream_atimes if r[0] == _ek]:
                                        self._extractor_stream_atimes.pop(_sr, None)
                                if extractor and hasattr(extractor, "close"):
                                    try:
                                        await extractor.close()
                                    except Exception:
                                        pass
                        # --- FINE LOGICA ---
                        return web.Response(
                            text=f"Key fetch failed: {resp.status}", status=resp.status
                        )
            except Exception as e:
                if request.transport.is_closing():
                    return web.Response(status=499)
                if proxy_used and not forced_proxy:
                    logger.warning(f"🔐 Key fetch failed with exception via proxy {proxy_used}: {e}. Checking dead policy and trying fallback...")
                    self._mark_proxy_dead_if_allowed(
                        proxy_used,
                        extractor_key=request.query.get("extractor_key"),
                    )
                    new_proxy = get_proxy_for_url(key_url, bypass_warp=bypass_warp)
                    if new_proxy and new_proxy != proxy_used:
                        logger.info(f"🔐 Key fetch failed, trying rotated proxy: {new_proxy}")
                        fallback_session = None
                        try:
                            fallback_session, _ = await self._get_proxy_session(key_url, bypass_warp=bypass_warp, forced_proxy=new_proxy)
                            async with fallback_session.get(key_url, headers=headers, ssl=not disable_ssl, allow_redirects=False, timeout=10) as rot_resp:
                                if rot_resp.status in (200, 206):
                                    key_data = await rot_resp.read()
                                    return web.Response(
                                        body=key_data,
                                        content_type="application/octet-stream",
                                        headers={
                                            "Access-Control-Allow-Origin": "*",
                                            "Access-Control-Allow-Headers": "*",
                                            "Cache-Control": "no-cache, no-store, must-revalidate",
                                        },
                                    )
                        except Exception as fallback_err:
                            logger.error(f"❌ Key fetch fallback via rotated proxy {new_proxy} failed: {fallback_err}")
                        finally:
                            if fallback_session and not fallback_session.closed:
                                await fallback_session.close()
                    elif not new_proxy:
                        logger.warning("🔐 Strict proxy mode: no fallback proxy available")
                        return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                
                if forced_proxy or STRICT_PROXY_CONTEXT.get():
                    logger.warning("🔐 Strict proxy mode: skipping direct fallback")
                    return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                
                logger.warning("🔐 Trying direct connection as final fallback for AES key...")
                try:
                    direct_session = await self._get_session()
                    async with direct_session.get(key_url, headers=headers, ssl=not disable_ssl, allow_redirects=False, timeout=10) as direct_resp:
                        if direct_resp.status in (200, 206):
                            key_data = await direct_resp.read()
                            return web.Response(
                                body=key_data,
                                content_type="application/octet-stream",
                                headers={
                                    "Access-Control-Allow-Origin": "*",
                                    "Access-Control-Allow-Headers": "*",
                                    "Cache-Control": "no-cache, no-store, must-revalidate",
                                },
                            )
                except Exception as direct_e:
                    logger.error(f"❌ Key fetch final direct fallback failed: {direct_e}")
                raise e

        except Exception as e:
            logger.error(f"❌ Error fetching AES key: {str(e)}")
            return web.Response(text=f"Key error: {str(e)}", status=500)
        finally:
            if session_need_close and session and not session.closed:
                await session.close()
