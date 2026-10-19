import os
import json
import time
import asyncio
import shutil
import urllib.parse
import threading
import services.proxy_shared as _shared
from services import wg_tunnels
from services import tor_proxy
from services.proxy_shared import (
    logger, web, APP_VERSION, admin_guard,
    check_password, get_client_ip, PlaylistBuilder, ClientSession, ClientTimeout,
    TCPConnector, API_PASSWORD,
    get_public_base_url,
)
from extractors.registry import *
import config_store
import config as _config
from config import (
    reload_config,
    clear_proxy_affinity,
    get_system_stats,
)

# Background speedtest job state (single run at a time; polled by the panel).
_SPEEDTEST_STATE = {"running": False, "results": None, "error": None}
# Background extractor-test job state (single run at a time; polled by the panel).
_EXTRACTOR_TEST_STATE = {"running": False, "results": None, "skipped": None, "error": None}


def _proxy_route_key(proxy_url: str):
    """Endpoint identity for speedtest dedupe: the same tunnel entered twice
    (e.g. global proxy socks5h://...:1081 == NordVPN socks5://...:1081) must
    not be tested twice."""
    parsed = urllib.parse.urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(scheme, scheme)
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        host = "127.0.0.1"
    return scheme, host, parsed.port
_PROXY_ENV_KEYS = (
    "ALL_PROXY", "all_proxy",
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "SOCKS_PROXY", "socks_proxy",
    "NO_PROXY", "no_proxy",
)



class HLSProxyPagesMixin:

    async def handle_playlist_request(self, request):
        """Gestisce le richieste per il playlist builder"""
        if not self.playlist_builder:
            return web.Response(
                text="❌ Playlist Builder not available - module missing", status=503
            )

        try:
            url_param = request.query.get("url")
            if not url_param:
                return web.Response(text="Missing 'url' parameter", status=400)
            if not url_param.strip():
                return web.Response(text="'url' parameter cannot be empty", status=400)

            playlist_definitions = [
                def_.strip() for def_ in url_param.split(";") if def_.strip()
            ]
            if not playlist_definitions:
                return web.Response(
                    text="No valid playlist definition found", status=400
                )

            base_url = get_public_base_url(request)

            # ✅ FIX: Passa api_password al builder se presente
            api_password = request.query.get("api_password")

            # Genera e raccoglie completamente la playlist in memoria prima di
            # rispondere, invece di inviarla in streaming (chunked) chunk per
            # chunk. Alcuni client a valle (es. Cloudflare Worker + APTV)
            # restano bloccati in attesa su risposte StreamResponse/chunked,
            # mentre gestiscono correttamente una risposta bufferizzata con
            # Content-Length. La logica di generazione/riscrittura degli URL
            # non cambia.
            #
            # Ottimizzazione memoria: invece di accumulare stringhe in una
            # lista e poi fare "".join(...) + .encode("utf-8") (che tiene in
            # memoria contemporaneamente lista di stringhe + stringa unita +
            # buffer bytes finale, cioè fino a 3 copie parziali), si accumula
            # direttamente in un bytearray man mano che le righe arrivano.
            # Questo mantiene un'unica struttura che cresce in place, con un
            # picco di memoria inferiore per playlist molto grandi.
            buf = bytearray()
            async for line in self.playlist_builder.async_generate_combined_playlist(
                playlist_definitions, base_url, api_password=api_password
            ):
                buf.extend(line.encode("utf-8"))

            return web.Response(
                body=bytes(buf),
                status=200,
                headers={
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Content-Disposition": 'attachment; filename="playlist.m3u"',
                    "Access-Control-Allow-Origin": "*",
                },
            )

        except (ConnectionResetError, OSError) as e:
            logger.info(f"Playlist download interrupted (client disconnected): {e}")
            return web.Response(status=200)
        except RuntimeError as e:
            if "closing transport" in str(e).lower() or "closed response" in str(e).lower():
                logger.info("Playlist download interrupted (closing transport)")
                return web.Response(status=200)
            logger.error(f"General error in playlist handler: {str(e)}")
            return web.Response(text=f"Error: {str(e)}", status=500)
        except Exception as e:
            logger.error(f"General error in playlist handler: {str(e)}")
            return web.Response(text=f"Error: {str(e)}", status=500)

    def _read_template(self, filename: str) -> str:
        """Funzione helper per leggere un file di template con caching."""
        if filename in self._template_cache:
            return self._template_cache[filename]
        template_path = os.path.join(self._template_cache_dir, filename)
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("{{APP_VERSION}}", APP_VERSION)
        self._template_cache[filename] = content
        return content

    async def handle_root(self, request):
        """Serve la pagina principale index.html."""
        try:
            # Refresh version on each page load
            await self._refresh_latest_version()

            html_content = self._read_template("index.html")

            # Determine version status class
            is_outdated = self.latest_version not in ["Checking...", "Unknown", "Error", APP_VERSION]
            version_status_class = "outdated" if is_outdated else ""

            html_content = html_content.replace("{{APP_VERSION}}", APP_VERSION)
            html_content = html_content.replace("{{LATEST_VERSION}}", self.latest_version)
            html_content = html_content.replace("{{VERSION_STATUS_CLASS}}", version_status_class)
            self.warp_status = await self.get_warp_status()
            html_content = html_content.replace("{{WARP_STATUS}}", self.warp_status)
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'index.html': {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Page not found.</p>",
                status=500,
                content_type="text/html",
            )

    # API docs pages: one shell (templates/api_docs.html) + the engine block.
    _API_DOCS_ENGINES = {
        "docs": (
            "EasyProxy API Docs",
            '    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">\n'
            "    <style>\n"
            "        html, body { margin: 0; background: #f4f7fb; }\n"
            "        .topbar { display: none; }\n"
            "        .swagger-ui .info .title { color: #0f172a; }\n"
            "    </style>\n",
            '    <div id="swagger-ui"></div>\n'
            '    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>\n'
            "    <script>\n"
            "        window.ui = SwaggerUIBundle({\n"
            "            url: '/openapi.json',\n"
            "            dom_id: '#swagger-ui',\n"
            "            deepLinking: true,\n"
            "            presets: [SwaggerUIBundle.presets.apis],\n"
            "            layout: 'BaseLayout'\n"
            "        });\n"
            "    </script>\n",
        ),
        "redoc": (
            "EasyProxy ReDoc",
            "    <style>\n"
            "        html, body { margin: 0; padding: 0; background: #f8fafc; font-family: Arial, sans-serif; }\n"
            "        .fallback { display: none; max-width: 900px; margin: 48px auto; padding: 24px;\n"
            "            background: white; border: 1px solid #e2e8f0; border-radius: 16px;\n"
            "            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08); }\n"
            "        .fallback h1 { margin-top: 0; color: #0f172a; }\n"
            "        .fallback a { color: #0284c7; text-decoration: none; }\n"
            "    </style>\n",
            '    <redoc spec-url="/openapi.json"></redoc>\n\n'
            '    <div id="fallback" class="fallback">\n'
            "        <h1>ReDoc non disponibile</h1>\n"
            "        <p>La vista ReDoc non si e caricata correttamente dal CDN.</p>\n"
            '        <p>Puoi usare <a href="/docs">/docs</a> per Swagger UI oppure aprire '
            '<a href="/openapi.json">/openapi.json</a>.</p>\n'
            "    </div>\n\n"
            "    <script>\n"
            "        window.addEventListener('error', function (event) {\n"
            "            if (event && event.target && event.target.tagName === 'SCRIPT') {\n"
            "                document.getElementById('fallback').style.display = 'block';\n"
            "            }\n"
            "        }, true);\n"
            "    </script>\n"
            '    <script src="https://unpkg.com/redoc@2.1.3/bundles/redoc.standalone.js"></script>\n',
        ),
    }

    def _render_api_docs(self, engine: str) -> web.Response:
        title, head, body = self._API_DOCS_ENGINES[engine]
        html_content = (
            self._read_template("api_docs.html")
            .replace("{{DOCS_TITLE}}", title)
            .replace("{{DOCS_HEAD}}", head)
            .replace("{{DOCS_BODY}}", body)
        )
        return web.Response(text=html_content, content_type="text/html")

    async def handle_docs(self, request):
        """Serve Swagger UI per la documentazione API."""
        try:
            return self._render_api_docs("docs")
        except Exception as e:
            logger.error(f"Unable to load the API docs shell: {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load API docs.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_redoc(self, request):
        """Serve ReDoc per la documentazione API."""
        try:
            return self._render_api_docs("redoc")
        except Exception as e:
            logger.error(f"Unable to load the API docs shell: {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load ReDoc.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_url_generator(self, request):
        """Serve la pagina web per generare URL proxy ed extractor."""
        try:
            html_content = self._read_template("url_generator.html")
            is_outdated = self.latest_version not in ["Checking...", "Unknown", "Error", APP_VERSION]
            version_status_class = "outdated" if is_outdated else ""
            html_content = html_content.replace("{{APP_VERSION}}", APP_VERSION)
            html_content = html_content.replace("{{LATEST_VERSION}}", self.latest_version)
            html_content = html_content.replace("{{VERSION_STATUS_CLASS}}", version_status_class)
            self.warp_status = await self.get_warp_status()
            html_content = html_content.replace("{{WARP_STATUS}}", self.warp_status)
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"Unable to load 'url_generator.html': {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load URL generator.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_builder(self, request):
        """Gestisce l'interfaccia web del playlist builder."""
        try:
            html_content = self._read_template("builder.html")
            is_outdated = self.latest_version not in ["Checking...", "Unknown", "Error", APP_VERSION]
            version_status_class = "outdated" if is_outdated else ""
            html_content = html_content.replace("{{APP_VERSION}}", APP_VERSION)
            html_content = html_content.replace("{{LATEST_VERSION}}", self.latest_version)
            html_content = html_content.replace("{{VERSION_STATUS_CLASS}}", version_status_class)
            self.warp_status = await self.get_warp_status()
            html_content = html_content.replace("{{WARP_STATUS}}", self.warp_status)
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'builder.html': {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load builder interface.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_info_page(self, request):
        """Serve la pagina HTML delle informazioni."""
        try:
            # Refresh version on each page load
            await self._refresh_latest_version()

            html_content = self._read_template("info.html")

            # Determine version status class
            is_outdated = self.latest_version not in ["Checking...", "Unknown", "Error", APP_VERSION]
            version_status_class = "outdated" if is_outdated else ""

            html_content = html_content.replace("{{APP_VERSION}}", APP_VERSION)
            html_content = html_content.replace("{{LATEST_VERSION}}", self.latest_version)
            html_content = html_content.replace("{{VERSION_STATUS_CLASS}}", version_status_class)
            self.warp_status = await self.get_warp_status()
            html_content = html_content.replace("{{WARP_STATUS}}", self.warp_status)
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'info.html': {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load info page.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_favicon(self, request):
        """Serve il file favicon.ico."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        favicon_path = os.path.join(base_dir, "static", "favicon.ico")
        if os.path.exists(favicon_path):
            return web.FileResponse(favicon_path)
        return web.Response(status=404)

    async def handle_options(self, request):
        """Gestisce richieste OPTIONS per CORS"""
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Range, Content-Type",
            "Access-Control-Max-Age": "86400",
        }
        return web.Response(headers=headers)

    async def handle_api_info(self, request):
        """Endpoint API che restituisce le informazioni sul server in formato JSON."""
        stats = get_system_stats()
        active_streams = _shared.get_active_streams()

        info = {
            "proxy": "EasyProxy",
            "version": APP_VERSION,  # Aggiornata per supporto AES-128

            "status": "✅ Running",
            "features": [
                "✅ Proxy HLS streams",
                "✅ AES-128 key proxying",  # ✅ NUOVO
                "✅ Playlist building",
                "✅ Supporto Proxy (SOCKS5, HTTP/S)",
                "✅ Multi-extractor support",
                "✅ DUAL video + audio sync with VLC HLS master",
                "✅ CORS enabled",
            ],
            "extractors_loaded": list(self.extractors.keys()),
            "diagnostics": {
                "extractors_cached": len(self.extractors),
                "cdn_tokens": len(getattr(self, '_renewed_cdn_tokens', {})),
                "proxy_sessions_cached": len(getattr(self, '_proxy_sessions', {})),
                "active_stream_sessions": len(active_streams),
                "active_stream_sessions_window_seconds": 30,
                "bypassed_warp_domains": len(_shared.BYPASSED_WARP_DOMAINS),
                "template_cache": len(getattr(self, '_template_cache', {})),
                "dead_proxies": len(getattr(_config, 'DEAD_PROXIES', {})),
                "shared_session_active": bool(self.session and not self.session.closed),
                "flex_session_active": bool(getattr(self, 'flex_session', None) and not self.flex_session.closed),
                "session_idle_seconds": round(time.time() - getattr(self, '_session_atime', 0), 1) if getattr(self, '_session_atime', 0) else None,
                "connection_count": sum(len(v) for v in self.session._connector._conns.values()) if self.session and not self.session.closed and hasattr(self.session, '_connector') and hasattr(self.session._connector, '_conns') else 0,
                "proxy_connection_count": sum(
                    sum(len(v) for v in s._connector._conns.values())
                    for s in getattr(self, '_proxy_sessions', {}).values()
                    if s and not s.closed and hasattr(s, '_connector') and hasattr(s._connector, '_conns')
                ),
                "parallel_fetch": dict(getattr(self, "_parallel_fetch_stats", {})),
                "cpu": stats.get("cpu", {}),
                "proxy_cpu": stats.get("proxy_cpu", {}),
                "net": stats.get("net", {}),
            },
            "memory": {
                **stats.get("proxy_ram", {}),
                "tracemalloc": stats.get("tracemalloc", {}),
                "processes": stats.get("processes", {}),
                "asyncio_tasks": stats.get("asyncio_tasks", {}),
            },
            "modules": {
                "playlist_builder": PlaylistBuilder is not None,
                "vavoo_extractor": VavooExtractor is not None,
                "vixsrc_extractor": VixSrcExtractor is not None,
                "sportsonline_extractor": SportsonlineExtractor is not None,
                "mixdrop_extractor": MixdropExtractor is not None,
                "voe_extractor": VoeExtractor is not None,
                "streamtape_extractor": StreamtapeExtractor is not None,
            },
            "proxy_config": {
                "global_proxies": f"{len(_shared.GLOBAL_PROXIES)} proxies loaded",
                "transport_routes": f"{len(_shared.TRANSPORT_ROUTES)} routing rules configured",
                "routes": [
                    {"url": route["url"], "has_proxy": route["proxy"] is not None}
                    for route in _shared.TRANSPORT_ROUTES
                ],
            },
            "endpoints": {
                "/proxy/hls/manifest.m3u8": "Proxy HLS (compatibilità MFP) - ?d=<URL>",
                "/proxy/mpd/manifest.m3u8": "Proxy MPD (compatibilità MFP) - ?d=<URL>",
                "/proxy/manifest.m3u8": "Proxy Legacy - ?url=<URL>",
                "/key": "Proxy chiavi AES-128 - ?key_url=<URL>",  # ✅ NUOVO
                "/playlist": "Playlist builder - ?url=<definizioni>",
                "/builder": "Interfaccia web per playlist builder",
                "/segment/{tail:.*}": "Proxy per segmenti .ts - ?base_url=<URL>",
                "/license": "Proxy licenze DRM (ClearKey/Widevine) - ?url=<URL> o ?clearkey=<id:key>",
                "/info": "Pagina HTML con informazioni sul server",
                "/api/info": "Endpoint JSON con informazioni sul server",
                "/api/dual/memory": "RAM used by the integrated DUAL service",
                "/dual/manifest.m3u8": "DUAL HLS master with synchronized video + audio - ?d=<Base64 JSON>",
                "/dual/sync/links": "DUAL JSON test for synchronizing video and audio",
                "/dual/cache/status": "Checks only whether the DUAL offset exists in the shared MongoDB cache",
                "/dual/aud/{hid}/audio.m3u8": "Synchronized DUAL audio playlist",
                "/dual/aud/{hid}/init.mp4": "DUAL audio init segment",
                "/dual/aud/{hid}/s{idx}.m4s": "DUAL audio segment",
                "/dual/sub/{sid}/subtitles.m3u8": "DUAL audio-source subtitle playlist shifted onto the audio timeline",
                "/dual/sub/{sid}/init.mp4": "DUAL subtitle init segment (fMP4 renditions only)",
                "/dual/sub/{sid}/s{idx}.vtt": "DUAL subtitle segment shifted onto the audio timeline",
            },
            "usage_examples": {
                "proxy_hls": "/proxy/hls/manifest.m3u8?d=https://example.com/stream.m3u8",
                "proxy_mpd": "/proxy/mpd/manifest.m3u8?d=https://example.com/stream.mpd",
                "aes_key": "/key?key_url=https://server.com/key.bin",  # ✅ NUOVO
                "playlist": "/playlist?url=http://example.com/playlist1.m3u8;http://example.com/playlist2.m3u8",
                "custom_headers": "/proxy/hls/manifest.m3u8?d=<URL>&h_Authorization=Bearer%20token",
                "forced_extractor": "/proxy/hls/manifest.m3u8?d=<URL>&host=vavoo&max_res=true",
                "dual_hls": "/dual/manifest.m3u8?d=<Base64URL(JSON)> [&api_password=<PASSWORD>]",
            },
        }
        return web.json_response(info)

    # "__SECURITY__" nel JSON marca le operazioni che richiedono api_password.
    def _openapi_spec(self, request) -> dict:
        """Serve static/openapi.json, patching the per-request fields."""
        if self._openapi_text is None:
            spec_path = os.path.join(self._template_cache_dir, os.pardir, "static", "openapi.json")
            with open(spec_path, "r", encoding="utf-8") as f:
                self._openapi_text = f.read()
        spec = json.loads(self._openapi_text)
        spec["info"]["version"] = APP_VERSION
        spec["servers"] = [{"url": get_public_base_url(request)}]
        security = [{"ApiPasswordQuery": []}] if API_PASSWORD else []
        for path in spec["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict) and operation.get("security") == "__SECURITY__":
                    if security:
                        operation["security"] = security
                    else:
                        del operation["security"]
        return spec

    async def handle_openapi(self, request):
        """Espone una specifica OpenAPI minimale per Swagger/ReDoc."""
        return web.json_response(self._openapi_spec(request))


    async def handle_generate_urls(self, request):
        """
        Endpoint compatibile con MediaFlow-Proxy per generare URL proxy.
        Supporta la richiesta POST da ilCorsaroViola.
        """
        try:
            data = await request.json()

            # Verifica password se presente nel body (ilCorsaroViola la manda qui)
            req_password = data.get("api_password")
            if API_PASSWORD and req_password != API_PASSWORD:
                # Fallback: check standard auth methods if body auth fails or is missing
                if not check_password(request):
                    logger.warning("⛔ Unauthorized generate_urls request")
                    return web.Response(
                        status=401, text="Unauthorized: Invalid API Password"
                    )

            urls_to_process = data.get("urls", [])

            # --- LOGGING RICHIESTO ---
            client_ip = get_client_ip(request)
            exit_strategy = "IP del Server (Diretto)"
            if _shared.GLOBAL_PROXIES:
                exit_strategy = (
                    f"Proxy Globale Random (Pool di {len(_shared.GLOBAL_PROXIES)} proxy)"
                )

            logger.info(f"🔄 [Generate URLs] Richiesta da Client IP: {client_ip}")
            logger.info(
                f"    -> Strategia di uscita prevista per lo stream: {exit_strategy}"
            )
            if urls_to_process:
                logger.info(
                    f"    -> Generazione di {len(urls_to_process)} URL proxy per destinazione: {urls_to_process[0].get('destination_url', 'N/A')}"
                )
            # -------------------------

            generated_urls = []

            # Determina base URL del proxy
            proxy_base = get_public_base_url(request)

            for item in urls_to_process:
                dest_url = item.get("destination_url")
                if not dest_url:
                    continue

                endpoint = item.get("endpoint", "/proxy/stream")
                req_headers = item.get("request_headers", {})
                bypass_warp = item.get("warp") == "off"
                proxy_route = str(item.get("proxy") or "").strip()
                bypass_proxies = proxy_route == "off"

                # Costruisci query params
                encoded_url = urllib.parse.quote(dest_url, safe="")
                params = [f"d={encoded_url}"]

                # Aggiungi headers come h_ params
                for key, value in req_headers.items():
                    params.append(
                        f"h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                    )

                # Aggiungi password se necessaria
                effective_pw = req_password or API_PASSWORD
                if effective_pw:
                    params.append(f"api_password={urllib.parse.quote(str(effective_pw), safe='')}")

                # Aggiungi bypass warp se richiesto
                if bypass_warp:
                    params.append("warp=off")

                # Aggiungi bypass proxy se richiesto
                if bypass_proxies:
                    params.append("proxy=off")
                elif proxy_route and proxy_route not in ("on", "true", "1"):
                    params.append(
                        f"proxy={urllib.parse.quote(proxy_route, safe='')}"
                    )

                if str(item.get("disable_ssl") or "").lower() in ("1", "true", "yes"):
                    params.append("disable_ssl=1")

                # Costruisci URL finale
                query_string = "&".join(params)

                # Assicuriamoci che l'endpoint inizi con /
                if not endpoint.startswith("/"):
                    endpoint = "/" + endpoint

                full_url = f"{proxy_base}{endpoint}?{query_string}"
                generated_urls.append(full_url)

            return web.json_response({"urls": generated_urls})

        except Exception as e:
            logger.error(f"❌ Error generating URLs: {e}")
            return web.Response(text=str(e), status=500)

    @admin_guard
    async def handle_proxy_ip(self, request):
        """Restituisce l'indirizzo IP pubblico reale del server (connessione diretta)."""

        target_url = "https://api.ipify.org?format=json"
        try:
            timeout = ClientTimeout(total=10)
            async with ClientSession(timeout=timeout, connector=TCPConnector()) as session:
                async with session.get(target_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return web.json_response(data)
                    logger.error(f"❌ Failed to fetch IP: HTTP {resp.status}")
                    return web.Response(text=f"Failed to fetch IP (HTTP {resp.status})", status=502)

        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"❌ Error fetching IP: {err_msg}")
            return web.Response(text=err_msg, status=500)

    async def handle_admin(self, request):
        if not check_password(request):
            raise web.HTTPFound('/admin/login')
        try:
            html = self._read_template("admin.html")
            html = html.replace("{{APP_VERSION}}", APP_VERSION)
            return web.Response(text=html, content_type="text/html")
        except Exception as e:
            logger.error(f"Error loading admin page: {e}")
            return web.Response(text="Admin page error", status=500)

    async def handle_admin_login(self, request):
        if check_password(request):
            raise web.HTTPFound('/admin')
        html = self._read_template("admin_login.html")
        return web.Response(text=html, content_type="text/html")

    async def handle_admin_api_login(self, request):
        import hmac
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid payload"}, status=400)
        password = str(data.get("password") or "")
        if API_PASSWORD and not hmac.compare_digest(password.encode(), API_PASSWORD.encode()):
            return web.json_response({"error": "Invalid password"}, status=401)
        resp = web.json_response({"ok": True})
        if API_PASSWORD:
            resp.set_cookie("admin_token", API_PASSWORD, httponly=True, samesite="lax", max_age=86400 * 30, path="/")
        return resp

    async def handle_admin_logout(self, request):
        resp = web.HTTPFound('/admin/login')
        resp.del_cookie("admin_token", path="/")
        raise resp

    @admin_guard
    async def handle_admin_api_get(self, request):
        config = config_store.get_all()
        config["api_password_configured"] = bool(API_PASSWORD)
        config["app_version"] = APP_VERSION
        config["warp_status"] = await self.get_warp_status()
        config["warp_ip"] = getattr(self, '_warp_ip', '')
        config["available_extractors"] = self._get_available_extractors()
        config["system_stats"] = get_system_stats()
        config["active_streams"] = _shared.get_active_streams()
        return web.json_response(config)

    def _get_available_extractors(self):
        import extractors.registry as _reg
        names = []
        suffix = "Extractor"
        for attr_name in dir(_reg):
            if attr_name.endswith(suffix) and attr_name != "ExtractorError":
                cls = getattr(_reg, attr_name, None)
                if cls is not None:
                    short = attr_name[:-len(suffix)].lower()
                    names.append(short)
        return sorted(names)

    @admin_guard
    async def handle_admin_api_update(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")

        allowed_keys = {
            "enable_warp", "warp_license_key",
            "global_proxies", "transport_routes", "extractor_proxies",
            "warp_off_extractors", "proxy_off_extractors", "warp_exclude_domains_custom", "proxy_exclude_domains",
            "max_res_extractors", "max_res_mpd", "max_res_hls",
            "dvr_enabled",
            "max_recording_duration", "recordings_retention_days",
            "proxy_test_timeout", "proxy_test_concurrency",
            "log_level",
        }

        updates = {}
        for key, value in data.items():
            if key in allowed_keys:
                updates[key] = value

        if updates:
            config_store.update(updates)
            reload_config()
            clear_proxy_affinity()
            # Invalidate extractor cache if proxy/routing/WARP settings changed
            if any(k in updates for k in ("global_proxies", "extractor_proxies", "transport_routes", "warp_off_extractors", "proxy_off_extractors", "warp_exclude_domains_custom", "proxy_exclude_domains", "enable_warp")):
                self._invalidate_extractors()
                logger.info("Extractor cache cleared due to config change")

        return web.json_response({"status": "ok", "updated": list(updates.keys())})

    @admin_guard
    async def handle_admin_api_warp_toggle(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")

        enable = data.get("enable", False)
        config_store.set("enable_warp", bool(enable))
        reload_config()
        clear_proxy_affinity()
        self._invalidate_extractors()

        if enable:
            logger.info("WARP enabled via admin panel")
            self._warp_status_checked_at = 0.0
            result = await self.reconnect_warp()
            if result.get("status") != "ok":
                logger.warning(f"WARP enable failed: {result.get('message')}")
                return web.json_response({"status": "error", "message": result.get("message", "WARP connect failed")}, status=500)
        else:
            logger.info("WARP disabled via admin panel")
            await self._stop_warp_proxy()
            self.warp_status = "Disabled"
            self._warp_ip = ""
            self._warp_status_checked_at = time.monotonic()

        return web.json_response({"status": "ok", "warp": "enabled" if enable else "disabled"})

    @admin_guard
    async def handle_admin_api_warp_reconnect(self, request):
        result = await self.reconnect_warp()
        status_code = 200 if result.get("status") == "ok" else 500
        return web.json_response(result, status=status_code)

    @admin_guard
    async def handle_admin_api_extractor_proxy(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")

        extractor = data.get("extractor")
        proxy = data.get("proxy", "")
        ptype = data.get("type", "proxy")

        if not extractor:
            return web.Response(status=400, text="Missing 'extractor' field")

        extractor_proxies = config_store.get("extractor_proxies", {})
        if proxy:
            if ptype == "file":
                extractor_proxies[extractor.lower()] = {"file": proxy}
            else:
                extractor_proxies[extractor.lower()] = proxy
        else:
            extractor_proxies.pop(extractor.lower(), None)

        config_store.set("extractor_proxies", extractor_proxies)
        reload_config()
        clear_proxy_affinity()
        self._invalidate_extractors()

        return web.json_response({"status": "ok", "extractor": extractor, "proxy": proxy or None})

    @admin_guard
    async def handle_admin_api_download(self, request):
        data = config_store.get_all()
        json_str = json.dumps(data, indent=2)
        return web.Response(
            body=json_str,
            content_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="easyproxy_config.json"'
            }
        )

    @admin_guard
    async def handle_admin_api_upload(self, request):
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "config":
                return web.Response(status=400, text="Missing 'config' file field")
            raw = await field.read()
            data = json.loads(raw)
            if not isinstance(data, dict):
                return web.Response(status=400, text="Config must be a JSON object")
            config_store.replace_all(data)
            reload_config()
            clear_proxy_affinity()
            self._invalidate_extractors()
            return web.json_response({"status": "ok", "message": "Config imported successfully"})
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON file")
        except Exception as e:
            logger.error(f"Config upload failed: {e}")
            return web.Response(status=500, text=f"Upload failed: {e}")

    @admin_guard
    async def handle_admin_api_speedtest(self, request):
        """Start the speedtest as a background job.

        A full run (one throughput test per route) can take minutes; serving it in
        one request makes gateways (Cloudflare, HF, nginx) time out and answer
        with an HTML error page, which the panel renders as a JSON parse error.
        """
        if _SPEEDTEST_STATE["running"]:
            return web.json_response({"status": "running"})
        _SPEEDTEST_STATE.update(running=True, results=None, error=None)
        asyncio.create_task(self._run_speedtest_job())
        return web.json_response({"status": "started"})

    @admin_guard
    async def handle_admin_api_speedtest_status(self, request):
        if _SPEEDTEST_STATE["running"]:
            return web.json_response({"status": "running"})
        return web.json_response({
            "status": "done",
            "results": _SPEEDTEST_STATE["results"] or [],
            "error": _SPEEDTEST_STATE["error"],
        })

    async def _run_speedtest_job(self):
        try:
            routes = [{"name": "Direct", "proxy": None}]
            from config import WARP_PROXY_URL
            if config_store.get("enable_warp", False):
                routes.append({"name": "Via WARP", "proxy": WARP_PROXY_URL})
            if (await tor_proxy.status()).get("running"):
                routes.append({
                    "name": "Via Tor",
                    "proxy": f"socks5://{tor_proxy.get_bind()}",
                })
            # Secondary WireGuard tunnels: only when their wireproxy is running.
            for slot, label in (("nordvpn", "Via NordVPN"), ("custom", "Via Custom WireGuard")):
                if wg_tunnels.process_running(slot):
                    routes.append({
                        "name": label,
                        "proxy": f"socks5://{wg_tunnels.get_bind(slot)}",
                    })
            global_proxies = config_store.get("global_proxies", [])
            if global_proxies:
                routes.append({"name": "Via Proxy", "proxy": global_proxies[0]})
            # Drop routes pointing at the same endpoint (e.g. a global proxy
            # that is just the NordVPN/Tor/WARP socks URL entered again).
            deduped, seen = [], set()
            for route in routes:
                if route["proxy"]:
                    key = _proxy_route_key(route["proxy"])
                    if key in seen:
                        continue
                    seen.add(key)
                deduped.append(route)
            routes = deduped
            output = []
            # Run routes one at a time. Concurrent tests compete for the
            # same uplink/downlink and make the displayed comparison invalid.
            for route in routes:
                try:
                    res = await asyncio.to_thread(self._run_speedtest, route["proxy"])
                except Exception as exc:
                    output.append({"name": route["name"], "error": str(exc)})
                else:
                    res["name"] = route["name"]
                    output.append(res)
            _SPEEDTEST_STATE["results"] = output
        except Exception as e:
            logger.error(f"Speedtest failed: {e}")
            _SPEEDTEST_STATE["error"] = str(e)
        finally:
            _SPEEDTEST_STATE["running"] = False
            # Release the freed pages instead of waiting for the 60s tick.
            await self._compact_heap()

    @admin_guard
    async def handle_admin_api_test_extractors(self, request):
        """Start the extractor test as a background job (a full run takes minutes)."""
        if _EXTRACTOR_TEST_STATE["running"]:
            return web.json_response({"status": "running"})
        _EXTRACTOR_TEST_STATE.update(running=True, results=None, skipped=None, error=None)
        asyncio.create_task(self._run_extractor_test_job(dict(request.headers)))
        return web.json_response({"status": "started"})

    @admin_guard
    async def handle_admin_api_test_extractors_status(self, request):
        if _EXTRACTOR_TEST_STATE["running"]:
            return web.json_response({"status": "running"})
        return web.json_response({
            "status": "done",
            "results": _EXTRACTOR_TEST_STATE["results"] or [],
            "skipped": _EXTRACTOR_TEST_STATE["skipped"] or [],
            "error": _EXTRACTOR_TEST_STATE["error"],
        })

    async def _run_extractor_test_job(self, headers):
        from extractors.test_urls import REMOTE_TEST_URLS_URL

        try:
            urls = {}
            try:
                async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                    async with session.get(REMOTE_TEST_URLS_URL) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"gist HTTP {resp.status}")
                        remote = await resp.json(content_type=None)
                        if not isinstance(remote, dict):
                            raise RuntimeError("gist JSON is not an object")
                        urls = {
                            k: v for k, v in remote.items()
                            if isinstance(v, str)
                        }
            except Exception as e:
                logger.warning("Test URL gist fetch failed: %s", e)
                _EXTRACTOR_TEST_STATE["error"] = f"Could not load test URLs from gist: {e}"
                return

            configured = {k: u for k, u in urls.items() if u}
            skipped = sorted(k for k, u in urls.items() if not u)
            results = []
            sem = asyncio.Semaphore(5)

            async def _test(name, url):
                async with sem:
                    entry = {"extractor": name, "url": url, "ok": False}
                    try:
                        async with self.borrow_extractor(
                            url, headers, host=name
                        ) as extractor:
                            timeout = getattr(extractor, "REQUEST_TIMEOUT_TOTAL", 30)
                            result = await asyncio.wait_for(
                                extractor.extract(url), timeout=timeout
                            )
                        destination = (result or {}).get("destination_url") or ""
                        entry["ok"] = bool(destination)
                        entry["destination_url"] = destination
                        if not destination:
                            entry["error"] = "no destination_url"
                    except asyncio.TimeoutError:
                        entry["error"] = "timeout"
                    except Exception as e:
                        entry["error"] = f"{type(e).__name__}: {e}"[:300]
                    results.append(entry)

            await asyncio.gather(*(_test(k, u) for k, u in configured.items()))
            results.sort(key=lambda r: (r["ok"], r["extractor"]))
            _EXTRACTOR_TEST_STATE["results"] = results
            _EXTRACTOR_TEST_STATE["skipped"] = skipped
        except Exception as e:
            logger.error(f"Extractor test failed: {e}")
            _EXTRACTOR_TEST_STATE["error"] = str(e)
        finally:
            _EXTRACTOR_TEST_STATE["running"] = False

    def _run_speedtest(self, proxy_url=None):
        """Measure real TCP throughput with curl for the direct and proxied routes.

        Ookla's static binary ignores proxies, so curl (which can be pointed at
        any route) is the single measurement path for every row of the panel.
        """
        for attempt in range(2):
            try:
                return self._measure_speedtest(proxy_url)
            except RuntimeError as exc:
                # A stalled circuit (common on Tor) can drop one leg of the test.
                if "no payload" not in str(exc) or attempt:
                    raise

    def _proxy_curl_args(self, proxy_url):
        """Return curl arguments that force DNS and TCP through one proxy."""
        if not proxy_url:
            return []
        parsed = urllib.parse.urlparse(proxy_url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ("socks5", "socks5h"):
            proxy_flag = "--socks5-hostname"
        elif scheme in ("http", "https"):
            proxy_flag = "--proxy"
        else:
            raise RuntimeError(f"Unsupported proxy scheme: {scheme or 'missing'}")
        if not parsed.hostname or not parsed.port:
            raise RuntimeError(f"Invalid proxy URL: {proxy_url}")

        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        if any(char.isspace() for char in username + password):
            raise RuntimeError("Proxy credentials contain unsupported whitespace")

        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        target = f"{host}:{parsed.port}"
        args = [proxy_flag, target]
        if scheme in ("http", "https"):
            # The proxy itself is normally plain HTTP even when the URL was
            # entered as https://; curl's --proxy-user handles auth safely.
            args = [proxy_flag, f"http://{target}"]
        if username or password:
            args.extend(["--proxy-user", f"{username}:{password}"])
        return args

    def _run_curl(self, proxy_url, url, write_out, extra=None, timeout=60, allow_timeout=False):
        import subprocess
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise RuntimeError("Speedtest unavailable: curl is not installed")

        env = os.environ.copy()
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)
        command = [
            curl,
            "--silent", "--show-error", "--location", "--fail",
            "--connect-timeout", "15", "--max-time", str(timeout),
            *self._proxy_curl_args(proxy_url),
        ]
        if extra:
            command.extend(extra)
        command.extend(["--write-out", write_out, url])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 10, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Speedtest timed out")
        if result.returncode != 0 and not (allow_timeout and result.returncode == 28):
            message = (result.stderr or "connection failed").strip()
            raise RuntimeError(f"Speedtest failed: {message[:180]}")
        return result.stdout.strip()

    def _run_curl_upload(self, proxy_url, url, duration=10):
        """Stream zeroes for a fixed time through a route and return curl metrics."""
        import subprocess
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise RuntimeError("Speedtest unavailable: curl is not installed")

        env = os.environ.copy()
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)
        command = [
            curl,
            "--silent", "--show-error", "--location",
            "--connect-timeout", "15", "--max-time", str(duration),
            *self._proxy_curl_args(proxy_url),
            "--request", "POST",
            "--header", "Content-Type: application/octet-stream",
            "--header", "Expect:",
            "--upload-file", "-",
            "--output", os.devnull,
            "--write-out", "%{size_upload}\\t%{speed_upload}\\t%{time_total}",
            url,
        ]
        # communicate() closes stdin and would truncate the upload to a few KB.
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )

        def feed_stdin():
            chunk = b"0" * (1024 * 1024)
            try:
                while process.poll() is None:
                    process.stdin.write(chunk)
                    process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed_stdin, daemon=True)
        feeder.start()
        try:
            process.wait(timeout=duration + 15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            feeder.join(timeout=2)
            raise RuntimeError("Speedtest upload timed out")
        feeder.join(timeout=5)
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        if process.returncode not in (0, 28):
            message = stderr.decode(errors="replace").strip() or "connection failed"
            raise RuntimeError(f"Speedtest upload failed: {message[:180]}")
        return stdout.decode(errors="replace").strip()

    def _measure_speedtest(self, proxy_url=None):
        """Measure real TCP throughput for one route (direct when proxy_url is empty)."""
        devnull = os.devnull
        ip = self._run_curl(
            proxy_url,
            "https://api.ipify.org",
            "",
            extra=["--output", "-"],
        )
        ping_ms = float(self._run_curl(
            proxy_url,
            "https://api.ipify.org",
            "%{time_total}",
            extra=["--output", devnull],
        )) * 1000
        # A Tor exit can refuse specific hosts (proof.ovh.net is a common one),
        # so fall back across mirrors until one leg returns real bytes.
        download, last_error = None, None
        for endpoint in (
            "https://proof.ovh.net/files/10Gb.dat",
            "https://speed.cloudflare.com/__down?bytes=100000000",
            "https://ash-speed.hetzner.com/100MB.bin",
        ):
            try:
                metrics = self._run_curl(
                    proxy_url,
                    endpoint,
                    "%{size_download}\\t%{speed_download}\\t%{time_total}",
                    extra=["--output", devnull],
                    timeout=20,
                    allow_timeout=True,
                )
            except RuntimeError as exc:
                last_error = exc
                continue
            download_bytes, download_speed, download_time = metrics.split("\t")
            if float(download_bytes) > 0:
                download = (download_bytes, download_speed, download_time)
                break
        if download is None:
            raise last_error or RuntimeError("Speedtest returned no payload")
        download_bytes, download_speed, download_time = download

        upload, last_error = None, None
        for endpoint in (
            "https://speed.cloudflare.com/__up",
            "https://librespeed.org/backend/empty.php",
        ):
            try:
                metrics = self._run_curl_upload(proxy_url, endpoint, duration=10)
            except RuntimeError as exc:
                last_error = exc
                continue
            upload_bytes, upload_speed, upload_time = metrics.split("\t")
            # A rejected/closed upload reports a few KB: require a real stream.
            if float(upload_bytes) > 128 * 1024:
                upload = (upload_bytes, upload_speed, upload_time)
                break
        if upload is None:
            raise last_error or RuntimeError("Speedtest returned no payload")
        upload_bytes, upload_speed, upload_time = upload

        return {
            "server": {
                "sponsor": "Proxy throughput" if proxy_url else "Direct throughput",
                "name": "proof.ovh.net + speed.cloudflare.com",
                "location": "via proxy" if proxy_url else "direct",
            },
            "proxy_used": proxy_url,
            "external_ip": ip,
            "download_mbps": round(float(download_speed) * 8 / 1_000_000, 1),
            "upload_mbps": round(float(upload_speed) * 8 / 1_000_000, 1),
            "ping_ms": round(ping_ms, 1),
        }
