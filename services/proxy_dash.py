import re
import time
import aiohttp
from urllib.parse import urljoin, urlparse, unquote
from config import STRICT_PROXY_CONTEXT
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


def _encode_dash_state(base_url: str, headers: dict, clearkey: str | None, **routing) -> str:
    """Seal DASH routing state into a stateless, authenticated token."""
    return seal_state({
        "b": base_url,
        "h": headers,
        "k": clearkey,
        "r": routing,
    }, "dash")


def _decode_dash_state(token: str) -> tuple[str, dict, str | None, dict] | None:
    """Open a stateless, authenticated DASH routing token."""
    data = open_state(token, "dash")
    if not data:
        return None
    return data.get("b", ""), data.get("h", {}), data.get("k"), data.get("r", {})


def _safe_endpoint(url: str | None) -> str:
    """Log scheme/host/path without query tokens or credentials."""
    parsed = urlparse(url or "")
    if not parsed.netloc:
        return "unknown"
    path = parsed.path or "/"
    if len(path) > 160:
        path = path[:157] + "..."
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _safe_route(proxy: str | None) -> str:
    """Identify route without logging proxy credentials."""
    if not proxy:
        return "direct"
    if proxy == _shared.WARP_PROXY_URL:
        return "WARP"
    parsed = urlparse(proxy)
    if parsed.hostname:
        return f"proxy({parsed.scheme or 'unknown'}://{parsed.hostname})"
    return "proxy"


class HLSProxyDashMixin:

    @staticmethod
    def _key_context(request, key_url: str, proxy_used: str | None, forced_proxy: str | None, bypass_warp: bool) -> str:
        strict = bool(forced_proxy or STRICT_PROXY_CONTEXT.get())
        if strict and bypass_warp:
            policy = "strict,warp-off"
        elif strict:
            policy = "strict"
        elif bypass_warp:
            policy = "warp-off"
        else:
            policy = "normal"
        extractor = request.query.get("extractor_key") or "unknown"
        source = _safe_endpoint(request.query.get("original_channel_url"))
        route = _safe_route(proxy_used or forced_proxy)
        return (
            f"key={_safe_endpoint(key_url)} source={source} extractor={extractor} "
            f"route={route} policy={policy}"
        )

    async def handle_dash_segment(self, request):
        """Proxy for native DASH segments with optional ClearKey decryption. Stateless."""
        token = request.match_info.get("session_id")
        path = request.match_info.get("tail")

        decoded = _decode_dash_state(token) if token else None
        if not decoded:
            return web.Response(text="Invalid or missing DASH state token", status=400)

        base_url, headers, clearkey, routing = decoded
        # Native relay fetches complete objects; a captured Range must not
        # truncate the initialization data passed to the decrypter.
        headers = {k: v for k, v in headers.items() if k.lower() != "range"}
        if not base_url:
            return web.Response(text="Missing base_url in DASH state", status=400)

        # A signed routing token must not authorize arbitrary hosts or traversal.
        decoded_path = path or ""
        for _ in range(3):
            decoded_path = unquote(decoded_path)
        if (not decoded_path or decoded_path.startswith(("/", "\\"))
                or "\\" in decoded_path or urlparse(decoded_path).scheme
                or any(part in (".", "..") for part in decoded_path.split("/"))):
            return web.Response(status=400, text="Invalid DASH segment path")
        resource_url = routing.get("resource_url")
        if resource_url:
            parsed_resource = urlparse(resource_url)
            if parsed_resource.scheme not in {"http", "https"} or not parsed_resource.netloc:
                return web.Response(status=400, text="Invalid DASH resource URL")
            segment_url = resource_url
        else:
            # Backward compatibility for tokens generated before resource_url
            # was sealed into the state.
            segment_url = urljoin(base_url, path)
            if getattr(request, "query_string", ""):
                segment_url += "?" + request.query_string

        # Parse clearkey into KID and KEY for decrypter
        kid, key = None, None
        if clearkey and ":" in clearkey:
            pairs = [pair.split(":", 1) for pair in clearkey.split(",")]
            if any(len(pair) != 2 for pair in pairs):
                return web.Response(status=400, text="Invalid ClearKey pairs")
            kid = ",".join(pair[0] for pair in pairs)
            key = ",".join(pair[1] for pair in pairs)

        try:
            client_range = getattr(request, "headers", {}).get("Range")
            init_url = routing.get("init_url")
            init_range = routing.get("init_range")

            def _matches_range(r1: str | None, r2: str | None) -> bool:
                if not r1 or not r2:
                    return False
                p1 = r1.lower().replace("bytes=", "").strip()
                p2 = r2.lower().replace("bytes=", "").strip()
                return p1 == p2

            if init_range and client_range:
                is_init = (segment_url == init_url and _matches_range(client_range, init_range))
            elif init_range and not client_range:
                is_init = (segment_url == init_url)
            else:
                is_init = (segment_url == init_url)

            # Fetch segment
            extractor_key = routing.get("extractor_key")
            admin_warp_off, admin_proxy_off = _shared.get_extractor_routing_overrides(extractor_key)
            bypass_warp = routing.get("bypass_warp", False) or admin_warp_off
            bypass_proxies = routing.get("bypass_proxies", False) or admin_proxy_off
            forced_proxy = routing.get("proxy")
            if bypass_warp and forced_proxy and _shared.is_warp_proxy_url(forced_proxy):
                forced_proxy = None
            if bypass_proxies:
                forced_proxy = None
                _shared.BYPASS_PROXIES_CONTEXT.set(True)

            if hasattr(self, "_touch_extractor_activity"):
                self._touch_extractor_activity(extractor_key, routing.get("stream_key"))

            session_kwargs = {
                "bypass_warp": bypass_warp,
                "forced_proxy": forced_proxy,
            }
            if routing.get("stream_key"):
                session_kwargs["session_key"] = routing["stream_key"]
            _session, _ = await self._get_proxy_session(segment_url, **session_kwargs)
            if client_range:
                headers["Range"] = client_range
            elif is_init and init_range:
                headers["Range"] = f"bytes={init_range}"

            async with _session.get(segment_url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status not in [200, 206]:
                    return web.Response(status=resp.status)

                # ClearKey path: must read segment for decryption
                if kid and key:
                    if not decrypt_segment:
                        return web.Response(status=502, text="DASH decryption unavailable")
                    content = await resp.read()
                    resp_content_type = getattr(resp, "content_type", "video/mp4") or "video/mp4"
                    resp_headers = getattr(resp, "headers", {})
                    if is_init:
                        decrypted = decrypt_segment(content, b"", kid, key)
                        response_headers = {
                            "Content-Type": resp_content_type,
                            "Access-Control-Allow-Origin": "*",
                            "Accept-Ranges": "bytes",
                        }
                        if client_range:
                            m = re.search(r"bytes=(\d+)-", client_range)
                            start_byte = int(m.group(1)) if m else 0
                            response_headers["Content-Range"] = f"bytes {start_byte}-{start_byte + len(decrypted) - 1}/*"
                            return web.Response(body=decrypted, status=206, headers=response_headers)
                        return web.Response(body=decrypted, content_type=resp_content_type,
                                            headers={"Access-Control-Allow-Origin": "*"})

                    # Non-media chunk (e.g. SIDX index box): pass through untouched
                    is_sidx = len(content) >= 8 and (content[4:8] == b"sidx" or (len(content) >= 16 and content[8:12] == b"sidx"))
                    if is_sidx:
                        response_headers = {
                            "Content-Type": resp_content_type,
                            "Access-Control-Allow-Origin": "*",
                            "Accept-Ranges": "bytes",
                        }
                        if "Content-Range" in resp_headers:
                            response_headers["Content-Range"] = resp_headers["Content-Range"]
                        return web.Response(body=content, status=resp.status, headers=response_headers)

                    if init_url:
                        try:
                            init_cache_key = f"{init_url}:{init_range}"
                            init_segment = getattr(self, "_dash_init_cache", {}).get(init_cache_key)
                            if not init_segment:
                                _init_session = _session
                                init_headers = dict(headers)
                                if init_range:
                                    init_headers["Range"] = f"bytes={init_range}"
                                else:
                                    init_headers.pop("Range", None)
                                async with _init_session.get(init_url, headers=init_headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as init_resp:
                                    if init_resp.status in [200, 206]:
                                        init_segment = await init_resp.read()
                                        if not hasattr(self, "_dash_init_cache"):
                                            self._dash_init_cache = {}
                                        if len(self._dash_init_cache) > 50:
                                            self._dash_init_cache.clear()
                                        self._dash_init_cache[init_cache_key] = init_segment
                            if init_segment:
                                try:
                                    decrypted = decrypt_segment(init_segment, content, kid, key, skip_init=True)
                                    response_headers = {
                                        "Content-Type": resp_content_type,
                                        "Access-Control-Allow-Origin": "*",
                                        "Accept-Ranges": "bytes",
                                    }
                                    if client_range:
                                        m = re.search(r"bytes=(\d+)-", client_range)
                                        start_byte = int(m.group(1)) if m else 0
                                        response_headers["Content-Range"] = f"bytes {start_byte}-{start_byte + len(decrypted) - 1}/*"
                                        return web.Response(body=decrypted, status=206, headers=response_headers)
                                    return web.Response(body=decrypted, content_type=resp_content_type,
                                                        headers={"Access-Control-Allow-Origin": "*"})
                                except Exception as e:
                                    logger.warning(f"DASH decryption failed for {path}: {e}")
                        except Exception as e:
                            logger.debug(f"DASH init re-fetch failed for {path}: {e}")

                    return web.Response(status=502, text="DASH decryption failed or initialization unavailable")

                # No ClearKey: stream chunk-by-chunk without buffering
                response_headers = {
                    "Content-Type": resp.content_type or "video/mp4",
                    "Access-Control-Allow-Origin": "*",
                }
                for name in ("Content-Range", "Accept-Ranges"):
                    if name in resp.headers:
                        response_headers[name] = resp.headers[name]
                response = web.StreamResponse(status=resp.status, headers=response_headers)
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
                        "Browser-backed key fetch failed, falling back to direct request: %s "
                        "[extractor=%s source=%s]",
                        browser_key_exc,
                        request.query.get("extractor_key") or "unknown",
                        _safe_endpoint(original_channel_url),
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

            logger.debug(f"🔐 Fetching AES key from: {_safe_endpoint(key_url)}")

            # ✅ Use pooled session for better performance
            proxy_used = None
            raw_proxy = request.query.get("proxy") or None
            forced_proxy = raw_proxy
            if raw_proxy and raw_proxy.lower() == "off":
                forced_proxy = None
                _shared.BYPASS_PROXIES_CONTEXT.set(True)
            if request.query.get("tor", "").lower() == "off":
                _shared.BYPASS_TOR_CONTEXT.set(True)
            bypass_warp = request.query.get("warp", "").lower() == "off"
            extractor_key = request.query.get("extractor_key")
            if extractor_key:
                ext_warp_off, ext_proxy_off = _shared.get_extractor_routing_overrides(extractor_key)
                if ext_warp_off:
                    bypass_warp = True
                if ext_proxy_off:
                    forced_proxy = None
                    _shared.BYPASS_PROXIES_CONTEXT.set(True)
            if bypass_warp and forced_proxy and _shared.is_warp_proxy_url(forced_proxy):
                forced_proxy = None

            if hasattr(self, "_touch_extractor_activity"):
                self._touch_extractor_activity(extractor_key, request.query.get("stream_key"))

            _GLOBAL_PROXIES = _shared.GLOBAL_PROXIES
            _ENABLE_WARP = _shared.ENABLE_WARP
            _TRANSPORT_ROUTES = _shared.TRANSPORT_ROUTES

            if self._should_force_direct_from_query(request):
                session = await self._get_session(url=key_url)
                logger.debug("Using direct session for AES key request (forced)")
            else:
                session, proxy_used = await self._get_proxy_session(
                    key_url,
                    bypass_warp=bypass_warp,
                    forced_proxy=forced_proxy,
                    session_key=request.query.get("stream_key"),
                )
                session_need_close = proxy_used is not None
                if proxy_used:
                    logger.info(
                        "🔐 [Key Proxy] Routing through: %s [%s]",
                        _safe_route(proxy_used),
                        self._key_context(request, key_url, proxy_used, forced_proxy, bypass_warp),
                    )
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
                    logger.warning(
                        "🔐 [Key Proxy] NO PROXY assigned [%s]",
                        self._key_context(request, key_url, proxy_used, forced_proxy, bypass_warp),
                    )
                else:
                    logger.info(
                        "🔐 [Key Proxy] Using direct session [%s]",
                        self._key_context(request, key_url, proxy_used, forced_proxy, bypass_warp),
                    )

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
                        logger.warning(
                            "⚠️ Could not compute key headers [%s]",
                            self._key_context(request, key_url, proxy_used, forced_proxy, bypass_warp),
                        )

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
                        key_context = self._key_context(
                            request, key_url, proxy_used, forced_proxy, bypass_warp
                        )
                        logger.warning(
                            "AES key rejected: status=%s %s",
                            resp.status,
                            key_context,
                        )
                        if proxy_used and not forced_proxy:
                            self._mark_proxy_dead_if_allowed(
                                proxy_used,
                                extractor_key=request.query.get("extractor_key"),
                            )
                            new_proxy = get_proxy_for_url(key_url, bypass_warp=bypass_warp)
                            if new_proxy and new_proxy != proxy_used:
                                logger.info(
                                    "AES key retry via alternate route: %s -> %s (%s)",
                                    _safe_route(proxy_used),
                                    _safe_route(new_proxy),
                                    key_context,
                                )
                                fallback_session = None
                                try:
                                    fallback_session, _ = await self._get_proxy_session(
                                        key_url,
                                        bypass_warp=bypass_warp,
                                        forced_proxy=new_proxy,
                                        session_key=request.query.get("stream_key"),
                                    )
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
                                    logger.error(
                                        "❌ Key fetch fallback via rotated proxy failed: %s (%s)",
                                        fallback_e,
                                        key_context,
                                    )
                                finally:
                                    if fallback_session and not fallback_session.closed:
                                        await fallback_session.close()
                            elif not new_proxy and (forced_proxy or STRICT_PROXY_CONTEXT.get()):
                                logger.warning(
                                    "AES key direct fallback blocked: no alternate route (%s)",
                                    key_context,
                                )
                                return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)

                        if forced_proxy or STRICT_PROXY_CONTEXT.get():
                            logger.warning(
                                "AES key direct fallback blocked by strict policy (%s)",
                                key_context,
                            )
                            return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                        logger.warning("AES key request giving up; direct fallback disabled (%s)", key_context)

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
                        # --- FINE LOGICA ---
                        return web.Response(
                            text=f"Key fetch failed: {resp.status}", status=resp.status
                        )
            except Exception as e:
                if request.transport.is_closing():
                    return web.Response(status=499)
                if proxy_used and not forced_proxy:
                    key_context = self._key_context(
                        request, key_url, proxy_used, forced_proxy, bypass_warp
                    )
                    logger.warning(
                        "AES key connection failed: error=%s detail=%s (%s); checking alternate route",
                        type(e).__name__,
                        str(e),
                        key_context,
                    )
                    self._mark_proxy_dead_if_allowed(
                        proxy_used,
                        extractor_key=request.query.get("extractor_key"),
                    )
                    new_proxy = get_proxy_for_url(key_url, bypass_warp=bypass_warp)
                    if new_proxy and new_proxy != proxy_used:
                        logger.info(
                            "AES key retry via alternate route: %s -> %s (%s)",
                            _safe_route(proxy_used),
                            _safe_route(new_proxy),
                            key_context,
                        )
                        fallback_session = None
                        try:
                            fallback_session, _ = await self._get_proxy_session(
                                key_url,
                                bypass_warp=bypass_warp,
                                forced_proxy=new_proxy,
                                session_key=request.query.get("stream_key"),
                            )
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
                            logger.error(
                                "❌ Key fetch fallback via rotated proxy failed: %s (%s)",
                                fallback_err,
                                key_context,
                            )
                        finally:
                            if fallback_session and not fallback_session.closed:
                                await fallback_session.close()
                    elif not new_proxy:
                        logger.warning(
                            "AES key direct fallback blocked: no alternate route (%s)",
                            key_context,
                        )
                        return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                
                if forced_proxy or STRICT_PROXY_CONTEXT.get():
                    key_context = self._key_context(
                        request, key_url, proxy_used, forced_proxy, bypass_warp
                    )
                    logger.warning(
                        "AES key direct fallback blocked by strict policy (%s)",
                        key_context,
                    )
                    return web.Response(text="Proxy failed and strict mode prevents direct fallback", status=502)
                
                key_context = self._key_context(
                    request, key_url, proxy_used, forced_proxy, bypass_warp
                )
                logger.warning("AES key request giving up; direct fallback disabled (%s)", key_context)
                raise e

        except Exception as e:
            logger.error(
                "❌ Error fetching AES key: %s (%s)",
                e,
                self._key_context(request, key_url, proxy_used, forced_proxy, bypass_warp),
            )
            return web.Response(text=f"Key error: {str(e)}", status=500)
        finally:
            if session_need_close and session and not session.closed:
                await session.close()
