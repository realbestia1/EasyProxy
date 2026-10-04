import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    check_password,
    web,
    BYPASS_WARP_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    BYPASS_TOR_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    check_vavoo_request,
    ManifestRewriter,
    get_public_base_url,
    get_extractor_routing_overrides,
    request_log_context,
    safe_log_route,
)
import config_store
import asyncio
import base64
import gzip
import re
import urllib.parse
import config as _config
from yarl import URL
from services.proxy_shared import seal_clearkey


def _protected_extractor_params(result: dict) -> dict[str, str]:
    params = {
        str(key): str(value)
        for key, value in (result.get("query_params") or {}).items()
        if value is not None
    }
    clearkey = params.pop("clearkey", "")
    key_ids = params.pop("key_id", "")
    keys = params.pop("key", "")
    if not clearkey and key_ids and keys:
        kid_values = [value.strip() for value in key_ids.split(",")]
        key_values = [value.strip() for value in keys.split(",")]
        clearkey = ",".join(
            f"{kid}:{key}"
            for kid, key in zip(kid_values, key_values)
            if kid and key
        )
    if clearkey:
        params["drm_token"] = seal_clearkey(clearkey)
    return params


class HLSProxyExtractorHandlerMixin:

    async def handle_extractor_request(self, request):
        """
        Endpoint compatibile con MediaFlow-Proxy per ottenere informazioni sullo stream.
        Supporta redirect_stream per ridirezionare direttamente al proxy.
        """
        logger.debug(
            "📥 Extractor request: path=%s host=%s",
            request.path,
            request.query.get("host") or "auto",
        )

        if not check_password(request):
            req_url = request.query.get("url") or request.query.get("d", "?")
            logger.warning(f"⛔ Unauthorized extractor request [{req_url}]")
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        bypass_warp = request.query.get("warp", "").lower() == "off"
        token = BYPASS_WARP_CONTEXT.set(bypass_warp)
        
        bypass_proxies = request.query.get("proxy", "").lower() == "off"
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(bypass_proxies)

        bypass_tor = request.query.get("tor", "").lower() == "off"
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
                "Ignoring stale WARP proxy from extractor request: %s",
                selected_proxy,
            )
            selected_proxy = None
        proxy_token = SELECTED_PROXY_CONTEXT.set(selected_proxy)
        strict_proxy_token = STRICT_PROXY_CONTEXT.set(bool(selected_proxy))
        url = None
        extractor = None
        extractor_key = None

        try:
            # Supporta sia 'url' che 'd' come parametro
            url = request.query.get("url") or request.query.get("d")
            if not url:
                # Se non c'è URL, restituisci una pagina di aiuto JSON con gli host disponibili
                help_response = {
                    "message": "EasyProxy Extractor API",
                    "usage": {
                        "endpoint": "/extractor/video",
                        "host_endpoint": "/extractor/video.m3u8",
                        "mp4_host_endpoint": "/extractor/video.mp4",
                        "parameters": {
                            "d": "(Required) URL to extract. Supports plain text, URL encoded, or Base64.",
                            "url": "(Alias) Same as 'd'.",
                            "host": "(Optional) Force specific extractor (bypass auto-detect).",
                            "redirect_stream": "(Optional) 'true' to redirect to stream, 'false' for JSON.",
                            "api_password": "(Optional) API Password if configured.",
                        },
                    },
                    "available_hosts": [
                        "vavoo",
                        "vixsrc",
                        "vixcloud (alias of vixsrc)",
                        "ads",
                        "sportsonline",
                        "mixdrop",
                        "voe",
                        "streamtape",
                        "orion",
                        "freeshot",
                        "doodstream",
                        "dood",
                        "fastream",
                        "filelions",
                        "filemoon",
                        "lulustream",
                        "okru",
                        "streamwish",
                        "streamhg",
                        "supervideo",
                        "dropload",
                        "uqload",
                        "vidmoly",
                        "vidlink",
                        "vidoza",
                        "turbovidplay",
                        "livetv",
                        "f16px",
                        "guardabest",
                        "mediaset",
                        "wittytv",
                        "raiplay",
                        "cinejoy",
                    ],
                    "examples": [
                        f"{get_public_base_url(request)}/extractor/video?d=https://vavoo.to/channel/123",
                        f"{get_public_base_url(request)}/extractor/video.m3u8?host=vavoo&d=https://custom-link.com",
                        f"{get_public_base_url(request)}/extractor/video.mp4?host=mixdrop&d=https://mixdrop.co/e/ABC123XYZ",
                        f"{get_public_base_url(request)}/extractor/video?d=BASE64_STRING",
                    ],
                }
                return web.json_response(help_response)

            # Decodifica URL se necessario
            try:
                url = urllib.parse.unquote(url)
            except:
                pass

            # 2. Base64 Decoding (Try)
            try:
                # Tentativo di decodifica Base64 se non sembra un URL valido o se richiesto
                # Aggiunge padding se necessario
                padded_url = url + "=" * (-len(url) % 4)
                decoded_bytes = base64.b64decode(padded_url, validate=True)
                decoded_str = decoded_bytes.decode("utf-8").strip()

                # Verifica se il risultato sembra un URL valido
                if decoded_str.startswith("http://") or decoded_str.startswith(
                    "https://"
                ):
                    url = decoded_str
                    logger.debug(f"🔓 Base64 decoded URL: {url}")
            except Exception:
                # Non è Base64 o non è un URL valido, proseguiamo con l'originale
                pass

            host_param = request.query.get("host")
            redirect_stream = (
                request.query.get("redirect_stream", "false").lower() == "true"
            )
            logger.info(
                f"🔍 Extracting: {url} (Host: {host_param}, Redirect: {redirect_stream})"
            )

            # Collect all query parameters to pass to the extractor
            extractor_kwargs = dict(request.query)
            extractor_kwargs.pop('url', None) # Remove to avoid duplicate argument error
            extractor_kwargs.pop('d', None)   # Remove to avoid duplicate argument error
            extractor_kwargs['request_headers'] = dict(request.headers)

            logger.debug(f"Extractor Debug: Initial bypass_warp from query: {bypass_warp}")

            extractor = await self.get_extractor(
                url, dict(request.headers), host=host_param, bypass_warp=bypass_warp
            )

            # Check if this extractor should bypass WARP or proxies based on admin config
            extractor_key = self._extractor_key_for_instance(extractor)
            if extractor_key:
                base_key = extractor_key.replace("_direct", "").replace("_noproxy", "")
                
                # Check WARP-off extractor policy.
                warp_off_list = config_store.get("warp_off_extractors", [])
                if base_key in warp_off_list:
                    bypass_warp = True
                    BYPASS_WARP_CONTEXT.set(True)
                    logger.debug(f"WARP off for extractor: {base_key}")
                    
                # Check proxy off
                proxy_off_list = config_store.get("proxy_off_extractors", [])
                if base_key in proxy_off_list:
                    BYPASS_PROXIES_CONTEXT.set(True)
                    logger.debug(f"Proxy off for extractor: {base_key}")
                    
                if base_key in warp_off_list or base_key in proxy_off_list:
                    extractor = await self.get_extractor(
                        url, dict(request.headers), host=host_param, bypass_warp=bypass_warp
                    )

            extractor_timeout = getattr(extractor, "REQUEST_TIMEOUT_TOTAL", 30)
            result = await asyncio.wait_for(
                extractor.extract(url, **extractor_kwargs), timeout=extractor_timeout
            )
            result_query_params = _protected_extractor_params(result)
            extractor_key = getattr(extractor, "extractor_name", None) or self._extractor_key_for_instance(extractor)
            if extractor_key:
                extractor_key = extractor_key.replace("_direct", "").replace("_noproxy", "")
            stream_key = request.query.get("stream_key")
            if not stream_key:
                source_key = self._stream_key_for_url(
                    request.query.get("orig_url") or url
                ) or "stream"
                stream_key = self._reuse_stream_key(
                    source_key, _config.get_client_ip(request)
                )

            stream_url = result["destination_url"]
            stream_headers = result.get("request_headers", {})
            mediaflow_endpoint = result.get("mediaflow_endpoint", "hls_proxy")
            captured_manifest = result.get("captured_manifest")
            force_disable_ssl = result.get("disable_ssl", False)
            selected_proxy = result.get("selected_proxy") or selected_proxy
            if not selected_proxy and extractor:
                selected_proxy = (
                    getattr(extractor, "last_used_proxy", None)
                    or getattr(extractor, "selected_proxy", None)
                    or getattr(extractor, "_session_proxy", None)
                    or getattr(extractor, "session_proxy", None)
                )
                # ✅ FIX: Se bypass_warp è True e il proxy selezionato è WARP,
                # ignoralo per evitare che un _session_proxy stantio su un estrattore
                # cache-forzato (es. GenericHLSExtractor) prevalga su warp=off.
                if bypass_warp and _shared.WARP_PROXY_URL and selected_proxy == _shared.WARP_PROXY_URL:
                    logger.debug(
                        "Extractor: ignoring stale WARP _session_proxy from extractor because bypass_warp=True"
                    )
                    selected_proxy = None

            if selected_proxy and _config.is_warp_proxy_url(selected_proxy) and (
                bypass_warp or not _config._get_dynamic_warp_enabled()
            ):
                logger.debug(
                    "Ignoring stale WARP route after extractor selection: %s",
                    selected_proxy,
                )
                selected_proxy = None

            # ✅ FIX: Resetta SELECTED_PROXY_CONTEXT al valore effettivo.
            # get_preferred_proxy_for_url (chiamato dall'estrattore in _get_session)
            # setta questo context a un proxy, ma dopo aver deciso selected_proxy
            # vogliamo che le chiamate successive PARTANO DA QUESTO STATO.
            SELECTED_PROXY_CONTEXT.set(selected_proxy)

            force_direct = result.get("force_direct", False)
            bypass_warp = result.get("bypass_warp", bypass_warp)

            # The extractor may return its own routing fields (VidFast's
            # runner does not know about the admin toggle). Never let that
            # result clear an admin-enforced bypass before building the relay.
            admin_warp_off, admin_proxy_off = get_extractor_routing_overrides(extractor_key)
            if admin_warp_off:
                bypass_warp = True
                BYPASS_WARP_CONTEXT.set(True)
                if selected_proxy and _config.is_warp_proxy_url(selected_proxy):
                    selected_proxy = None
            if admin_proxy_off:
                BYPASS_PROXIES_CONTEXT.set(True)

            # The extractor can return a route after the first validation. Apply
            # the effective WARP policy one final time before generating URLs.
            if selected_proxy and _config.is_warp_proxy_url(selected_proxy) and (
                bypass_warp or not _config._get_dynamic_warp_enabled()
            ):
                logger.debug(
                    "Ignoring stale WARP route before redirect: %s",
                    selected_proxy,
                )
                selected_proxy = None
            SELECTED_PROXY_CONTEXT.set(selected_proxy)

            logger.debug(f"Extractor Debug: Extractor result selected_proxy: {selected_proxy}")

            # Log dello stato dell'estrattore
            logger.debug(f"Extractor Debug: Extractor result bypass_warp: {result.get('bypass_warp')}")

            # Non forziamo più l'override qui, lasciamo che sia la scelta iniziale a comandare
            # bypass_warp = bypass_warp (rimane quello definito all'inizio a riga 1902)

            logger.debug(f"Extractor Debug: Final bypass_warp for redirect: {bypass_warp}")

            logger.info(
                f"✅ Extraction success: {stream_url[:50]}... Endpoint: {mediaflow_endpoint}"
            )

            # Costruisci l'URL del proxy per questo stream
            proxy_base = get_public_base_url(request)

            # Determina l'endpoint corretto
            endpoint = "/proxy/hls/manifest.m3u8"

            # Check extension of the actual path, not the whole URL
            path_lower = urllib.parse.urlparse(stream_url).path.lower()
            is_direct_video = any(path_lower.endswith(ext) for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"])

            if mediaflow_endpoint == "proxy_stream_endpoint" or is_direct_video:
                endpoint = "/proxy/stream"
            elif (
                mediaflow_endpoint == "mpd_manifest_proxy"
                or ".mpd" in path_lower
                or "manifest" in path_lower and "dash" in path_lower
            ):
                endpoint = "/proxy/mpd/manifest.m3u8"

            encoded_url = urllib.parse.quote(stream_url, safe="")
            forced_max_res = self._request_forces_max_res(
                request,
                extractor_key,
                "mpd" if endpoint.endswith("/manifest.mpd") else "hls",
            )
            header_params = "".join(
                [
                    f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                    for key, value in stream_headers.items()
                ]
            )

            # Aggiungi api_password se presente
            api_password = request.query.get("api_password")
            if api_password:
                header_params += f"&api_password={api_password}"

            if force_disable_ssl:
                header_params += "&disable_ssl=1"

            if bypass_warp:
                header_params += "&warp=off"
            if BYPASS_PROXIES_CONTEXT.get():
                header_params += "&proxy=off"
            if BYPASS_TOR_CONTEXT.get():
                header_params += "&tor=off"
            elif selected_proxy:
                header_params += f"&proxy={urllib.parse.quote(selected_proxy)}"
            if force_direct:
                header_params += "&direct=1"
            orig_url_val = request.query.get("orig_url") or url
            if orig_url_val:
                header_params += f"&orig_url={urllib.parse.quote(orig_url_val, safe='')}"
            if extractor_key:
                header_params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
            if stream_key:
                header_params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"
            if forced_max_res:
                header_params += "&max_res=true"
            for key, value in result_query_params.items():
                header_params += (
                    f"&{urllib.parse.quote(key, safe='')}="
                    f"{urllib.parse.quote(value, safe='')}"
                )

            if redirect_stream and captured_manifest and endpoint == "/proxy/hls/manifest.m3u8":
                original_channel_url = request.query.get("orig_url") or request.query.get("url") or request.query.get("d", "")
                no_bypass = request.query.get("no_bypass") == "1"
                is_vavoo_req = check_vavoo_request(stream_headers, request, stream_url)
                disable_ssl = request.query.get("disable_ssl") == "1" or force_disable_ssl or is_vavoo_req

                # Variant selection (all variants or only the highest one)
                # happens in rewrite_manifest_urls via max_res.
                manifest_content = captured_manifest
                manifest_base_url = stream_url

                rewritten_manifest = await ManifestRewriter.rewrite_manifest_urls(
                    manifest_content=manifest_content,
                    base_url=manifest_base_url,
                    proxy_base=proxy_base,
                    stream_headers=stream_headers,
                    original_channel_url=original_channel_url,
                    api_password=api_password,
                    get_extractor_func=lambda url, headers, host=None: self.get_extractor(
                        url, headers, host, bypass_warp=bypass_warp
                    ),
                    no_bypass=no_bypass,
                    shorten_url_func=self.shorten_hls_url,
                    bypass_warp=bypass_warp,
                    bypass_proxies=bypass_proxies,
                    bypass_tor=bypass_tor,
                    disable_ssl=disable_ssl,
                    selected_proxy=selected_proxy,
                    force_direct=force_direct,
                    extractor_key=extractor_key,
                    stream_key=stream_key,
                    max_res=forced_max_res,
                )
                response_headers = {
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                }
                if "gzip" in request.headers.get("Accept-Encoding", "").lower():
                    response_headers["Content-Encoding"] = "gzip"
                    response_headers["Vary"] = "Accept-Encoding"
                    return web.Response(
                        body=gzip.compress(rewritten_manifest.encode("utf-8"), mtime=0),
                        headers=response_headers,
                    )
                return web.Response(text=rewritten_manifest, headers=response_headers)

            if redirect_stream and endpoint == "/proxy/mpd/manifest.m3u8":
                proxy_query = {
                    "d": stream_url,
                    **{
                        f"h_{key}": value
                        for key, value in stream_headers.items()
                    },
                    **result_query_params,
                }
                if api_password:
                    proxy_query["api_password"] = api_password
                if force_disable_ssl:
                    proxy_query["disable_ssl"] = "1"
                if bypass_warp:
                    proxy_query["warp"] = "off"
                if BYPASS_PROXIES_CONTEXT.get():
                    proxy_query["proxy"] = "off"
                elif selected_proxy:
                    proxy_query["proxy"] = selected_proxy
                if force_direct:
                    proxy_query["direct"] = "1"
                if extractor_key:
                    proxy_query["extractor_key"] = extractor_key
                if stream_key:
                    proxy_query["stream_key"] = stream_key
                if forced_max_res:
                    proxy_query["max_res"] = "true"
                proxy_request = request.clone(
                    rel_url=URL(endpoint).with_query(proxy_query)
                )
                return await self.handle_proxy_request(proxy_request)

            # 1. URL COMPLETO (Solo per il redirect)
            full_proxy_url = f"{proxy_base}{endpoint}?d={encoded_url}{header_params}"

            # Carry over redirect_stream param for nested redirects
            if redirect_stream:
                full_proxy_url += "&redirect_stream=true"

            if redirect_stream:
                logger.info("[PLAY] Proxying stream directly without redirect")
                return await self._proxy_stream(
                    request,
                    stream_url,
                    stream_headers,
                    bypass_warp=bypass_warp,
                    forced_proxy=selected_proxy,
                    force_direct=force_direct,
                    extractor_key=extractor_key,
                    stream_key=stream_key,
                )

            # 2. URL PULITO (Per il JSON stile MediaFlow)
            q_params = {}
            q_params.update(result_query_params)
            if api_password:
                q_params["api_password"] = api_password
            if selected_proxy:
                q_params["proxy"] = selected_proxy
            if bypass_warp:
                q_params["warp"] = "off"
            if BYPASS_PROXIES_CONTEXT.get():
                q_params["proxy"] = "off"
            if extractor_key:
                q_params["extractor_key"] = extractor_key
            if stream_key:
                q_params["stream_key"] = stream_key

            response_data = {
                "destination_url": stream_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": mediaflow_endpoint,
                "mediaflow_proxy_url": f"{proxy_base}{endpoint}",
                "query_params": q_params,
            }

            logger.info(
                "✅ Extractor OK: %s [%s]",
                request_log_context(
                    request,
                    stream_url,
                    route=safe_log_route(selected_proxy),
                    extractor=extractor,
                ),
                stream_url[:50],
            )
            return web.json_response(response_data)

        except Exception as e:
            error_message = str(e).lower()
            # Per errori attesi (video non trovato, servizio non disponibile), non stampare il traceback
            is_expected_error = any(
                x in error_message
                for x in [
                    "not found",
                    "unavailable",
                    "403",
                    "forbidden",
                    "502",
                    "bad gateway",
                    "timeout",
                    "temporarily unavailable",
                    "removed",
                    "deleted",
                    "expired",
                    "no longer available",
                ]
            ) or isinstance(e, (asyncio.TimeoutError, asyncio.CancelledError)) or type(e).__name__ == "ExtractorError"  # ponytail: expected extractor failures shouldn't print a traceback

            error_desc = str(e) or type(e).__name__
            log_proxy = selected_proxy or (
                getattr(extractor, "last_used_proxy", None)
                or getattr(extractor, "selected_proxy", None)
                or getattr(extractor, "_session_proxy", None)
                or getattr(extractor, "session_proxy", None)
            )
            error_context = request_log_context(
                request,
                url,
                route=safe_log_route(log_proxy),
                extractor=extractor,
            )
            if isinstance(e, asyncio.CancelledError):
                logger.info("Extractor request cancelled (client disconnected) [%s]", error_context)
                raise
            if is_expected_error:
                logger.error(
                    "❌ Extractor request failed: %s [%s]",
                    error_desc,
                    error_context,
                )
            else:
                logger.error(
                    "❌ Error in extractor request: %s [%s]",
                    error_desc,
                    error_context,
                )
                import traceback
                traceback.print_exc()

            status_code = 500
            if type(e).__name__ == "ExtractorError" or "not found" in error_message or "pick failed" in error_message:
                status_code = 404

            return web.json_response(
                {"error": error_desc, "status": "error"},
                status=status_code
            )
        finally:
            BYPASS_WARP_CONTEXT.reset(token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            BYPASS_TOR_CONTEXT.reset(tor_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(proxy_token)
            STRICT_PROXY_CONTEXT.reset(strict_proxy_token)
            # Registry instances are shared. Their owner closes them at shutdown,
            # not when one of several concurrent requests finishes.
