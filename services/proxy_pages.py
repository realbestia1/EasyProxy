import os
import json
import urllib.parse
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger, web, APP_VERSION, VERSION_MODE,
    check_password, PlaylistBuilder, ClientSession, ClientTimeout,
    TCPConnector, ProxyConnector, get_connector_for_proxy, API_PASSWORD,
)
from extractors.registry import *
import config_store
from config import reload_config, clear_proxy_affinity

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

            # ✅ CORREZIONE: Rileva lo schema e l'host corretti quando dietro un reverse proxy
            scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
            host = request.headers.get("X-Forwarded-Host", request.host)
            base_url = f"{scheme}://{host}"

            # ✅ FIX: Passa api_password al builder se presente
            api_password = request.query.get("api_password")

            async def generate_response():
                async for (
                    line
                ) in self.playlist_builder.async_generate_combined_playlist(
                    playlist_definitions, base_url, api_password=api_password
                ):
                    yield line.encode("utf-8")

            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Content-Disposition": 'attachment; filename="playlist.m3u"',
                    "Access-Control-Allow-Origin": "*",
                },
            )

            await response.prepare(request)

            async for chunk in generate_response():
                await response.write(chunk)

            await response.write_eof()
            return response

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

            html_content = html_content.replace("{{VERSION_MODE}}", VERSION_MODE)
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

    async def handle_docs(self, request):
        """Serve Swagger UI per la documentazione API."""
        try:
            html_content = self._read_template("docs.html")
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"Unable to load 'docs.html': {e}")
            return web.Response(
                text="<h1>Error 500</h1><p>Unable to load API docs.</p>",
                status=500,
                content_type="text/html",
            )

    async def handle_redoc(self, request):
        """Serve ReDoc per la documentazione API."""
        try:
            html_content = self._read_template("redoc.html")
            return web.Response(text=html_content, content_type="text/html")
        except Exception as e:
            logger.error(f"Unable to load 'redoc.html': {e}")
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

            html_content = html_content.replace("{{VERSION_MODE}}", VERSION_MODE)
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
        # Refresh version on API call
        await self._refresh_latest_version()

        info = {
            "proxy": "EasyProxy",
            "version": APP_VERSION,  # Aggiornata per supporto AES-128
            "mode": VERSION_MODE,
            "status": "✅ Running",
            "features": [
                "✅ Proxy HLS streams",
                "✅ AES-128 key proxying",  # ✅ NUOVO
                "✅ Playlist building",
                "✅ Supporto Proxy (SOCKS5, HTTP/S)",
                "✅ Multi-extractor support",
                "✅ CORS enabled",
            ],
            "extractors_loaded": list(self.extractors.keys()),
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
                "/segment/{segment}": "Proxy per segmenti .ts - ?base_url=<URL>",
                "/license": "Proxy licenze DRM (ClearKey/Widevine) - ?url=<URL> o ?clearkey=<id:key>",
                "/info": "Pagina HTML con informazioni sul server",
                "/api/info": "Endpoint JSON con informazioni sul server",
            },
            "usage_examples": {
                "proxy_hls": "/proxy/hls/manifest.m3u8?d=https://example.com/stream.m3u8",
                "proxy_mpd": "/proxy/mpd/manifest.m3u8?d=https://example.com/stream.mpd",
                "aes_key": "/key?key_url=https://server.com/key.bin",  # ✅ NUOVO
                "playlist": "/playlist?url=http://example.com/playlist1.m3u8;http://example.com/playlist2.m3u8",
                "custom_headers": "/proxy/hls/manifest.m3u8?d=<URL>&h_Authorization=Bearer%20token",
            },
        }
        return web.json_response(info)

    async def handle_openapi(self, request):
        """Espone una specifica OpenAPI minimale per Swagger/ReDoc."""
        server_url = f"{request.scheme}://{request.host}"
        requires_password = bool(API_PASSWORD)

        security_schemes = {
            "ApiPasswordQuery": {
                "type": "apiKey",
                "in": "query",
                "name": "api_password",
                "description": "Primary auth method shown in docs. Header x-api-password is still accepted by the server.",
            },
        }
        security = [{"ApiPasswordQuery": []}] if requires_password else []

        spec = {
            "openapi": "3.0.3",
            "info": {
                "title": "EasyProxy API",
                "version": "2.5.0",
                "description": (
                    "Interactive documentation for EasyProxy. "
                    "Includes HLS/MPD proxying, extractor endpoints, key and license helpers, "
                    "playlist generation, and compatibility endpoints inspired by MediaFlow Proxy."
                ),
            },
            "servers": [{"url": server_url}],
            "components": {"securitySchemes": security_schemes},
            "paths": {
                "/api/info": {
                    "get": {
                        "summary": "Server information",
                        "description": "Returns server status, loaded extractors, modules, and example endpoints.",
                        "responses": {"200": {"description": "Server information JSON"}},
                    }
                },
                "/proxy/manifest.m3u8": {
                    "get": {
                        "summary": "Legacy proxy manifest",
                        "description": "Proxy a manifest using the legacy url parameter.",
                        "parameters": [
                            {"name": "url", "in": "query", "schema": {"type": "string"}, "required": True},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Proxied manifest or media response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/hls/manifest.m3u8": {
                    "get": {
                        "summary": "Proxy HLS manifest",
                        "description": "MediaFlow-compatible HLS proxy endpoint.",
                        "parameters": [
                            {"name": "d", "in": "query", "schema": {"type": "string"}, "required": True, "description": "Destination manifest URL"},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Proxied HLS manifest"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/mpd/manifest.m3u8": {
                    "get": {
                        "summary": "Proxy MPD as HLS",
                        "description": "Converts or relays MPEG-DASH/MPD streams through EasyProxy.",
                        "parameters": [
                            {"name": "d", "in": "query", "schema": {"type": "string"}, "required": True, "description": "Destination MPD URL"},
                            {"name": "key_id", "in": "query", "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Generated HLS manifest"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/stream": {
                    "get": {
                        "summary": "Generic stream proxy",
                        "description": "Generic MediaFlow-style stream endpoint for direct proxying.",
                        "parameters": [
                            {"name": "d", "in": "query", "schema": {"type": "string"}, "required": True},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Streamed response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/extractor": {
                    "get": {
                        "summary": "Generic extractor",
                        "description": "Resolve supported hosters into playable URLs.",
                        "parameters": [
                            {"name": "host", "in": "query", "schema": {"type": "string"}},
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Extractor response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/extractor/video": {
                    "get": {
                        "summary": "Extractor compatibility endpoint",
                        "description": "MediaFlow-compatible alias for video extractor requests.",
                        "parameters": [
                            {"name": "host", "in": "query", "schema": {"type": "string"}},
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Extractor response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/extractor/video.m3u8": {
                    "get": {
                        "summary": "Extractor compatibility endpoint with m3u8 suffix",
                        "description": "Alias for host-forced extractor requests using an m3u8-style path.",
                        "parameters": [
                            {"name": "host", "in": "query", "schema": {"type": "string"}},
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Extractor response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/extractor/video.mp4": {
                    "get": {
                        "summary": "Extractor compatibility endpoint with mp4 suffix",
                        "description": "Alias for host-forced extractor requests where the resolved media is typically a direct MP4 stream.",
                        "parameters": [
                            {"name": "host", "in": "query", "schema": {"type": "string"}},
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "d", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Extractor response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/key": {
                    "get": {
                        "summary": "Fetch or transform decryption keys",
                        "description": "Proxy AES-128 keys or derive license-related key material.",
                        "parameters": [
                            {"name": "key_url", "in": "query", "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "schema": {"type": "string"}},
                            {"name": "key_id", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Key response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/license": {
                    "get": {
                        "summary": "License proxy",
                        "description": "Proxy DRM license requests or handle ClearKey shortcuts.",
                        "parameters": [
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "clearkey", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "License response"}},
                        **({"security": security} if requires_password else {}),
                    },
                    "post": {
                        "summary": "License proxy POST",
                        "description": "POST DRM license payloads to the upstream license server.",
                        "requestBody": {
                            "required": False,
                            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
                        },
                        "responses": {"200": {"description": "License response"}},
                        **({"security": security} if requires_password else {}),
                    },
                },
                "/generate_urls": {
                    "post": {
                        "summary": "Generate proxy URLs",
                        "description": "Generate one or multiple compatibility URLs for clients.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "mediaflow_proxy_url": {"type": "string"},
                                            "api_password": {"type": "string"},
                                            "urls": {"type": "array", "items": {"type": "object"}},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Generated URL list"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/playlist": {
                    "get": {
                        "summary": "Build a playlist",
                        "description": "Combine multiple source URLs into a generated playlist.",
                        "parameters": [
                            {"name": "url", "in": "query", "schema": {"type": "string"}, "required": True},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Generated playlist"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/ip": {
                    "get": {
                        "summary": "Resolve public IP",
                        "description": "Returns the public IP as seen through the configured proxy route.",
                        "responses": {"200": {"description": "Public IP response"}},
                    }
                },
            },
        }

        return web.json_response(spec)

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
            client_ip = request.remote
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
            scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
            host = request.headers.get("X-Forwarded-Host", request.host)
            proxy_base = f"{scheme}://{host}"

            for item in urls_to_process:
                dest_url = item.get("destination_url")
                if not dest_url:
                    continue

                endpoint = item.get("endpoint", "/proxy/stream")
                req_headers = item.get("request_headers", {})
                bypass_warp = item.get("warp") == "off"

                # Costruisci query params
                encoded_url = urllib.parse.quote(dest_url, safe="")
                params = [f"d={encoded_url}"]

                # Aggiungi headers come h_ params
                for key, value in req_headers.items():
                    params.append(
                        f"h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                    )

                # Aggiungi password se necessaria
                if API_PASSWORD:
                    params.append(f"api_password={API_PASSWORD}")

                # Aggiungi bypass warp se richiesto
                if bypass_warp:
                    params.append("warp=off")

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

    async def handle_proxy_ip(self, request):
        """Restituisce l'indirizzo IP pubblico del server (o del proxy se configurato)."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        try:
            # Usa un proxy globale se configurato, altrimenti connessione diretta
            proxy = random.choice(_shared.GLOBAL_PROXIES) if _shared.GLOBAL_PROXIES else None

            # Crea una sessione dedicata con il proxy configurato
            if proxy:
                logger.info(f"[NET] Checking IP via proxy: {proxy}")
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector()

            timeout = ClientTimeout(total=10)
            async with ClientSession(timeout=timeout, connector=connector) as session:
                # Usa un servizio esterno per determinare l'IP pubblico
                async with session.get("https://api.ipify.org?format=json") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return web.json_response(data)
                    else:
                        logger.error(f"❌ Failed to fetch IP: {resp.status}")
                        return web.Response(text="Failed to fetch IP", status=502)

        except Exception as e:
            logger.error(f"❌ Error fetching IP: {e}")
            return web.Response(text=str(e), status=500)

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
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        password = data.get("password", "")
        if API_PASSWORD and password != API_PASSWORD:
            return web.json_response({"error": "Invalid password"}, status=401)
        resp = web.json_response({"ok": True})
        resp.set_cookie("admin_token", API_PASSWORD, httponly=True, samesite="lax", max_age=86400 * 30, path="/")
        return resp

    async def handle_admin_logout(self, request):
        resp = web.HTTPFound('/admin/login')
        resp.del_cookie("admin_token", path="/")
        raise resp

    async def handle_admin_api_get(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        config = config_store.get_all()
        config["api_password_configured"] = bool(API_PASSWORD)
        config["app_version"] = APP_VERSION
        config["version_mode"] = VERSION_MODE
        config["warp_status"] = await self.get_warp_status()
        config["warp_ip"] = getattr(self, '_warp_ip', '')
        config["available_extractors"] = self._get_available_extractors()
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

    async def handle_admin_api_update(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")

        allowed_keys = {
            "enable_warp", "warp_license_key",
            "global_proxies", "transport_routes", "extractor_proxies",
            "warp_off_extractors", "warp_exclude_domains_custom",
            "mpd_mode", "dvr_enabled",
            "max_recording_duration", "recordings_retention_days",
            "enable_remuxing",
            "proxy_test_timeout", "proxy_test_concurrency", "segment_cache_ttl",
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
            # Invalidate extractor cache if proxy-related settings changed
            if any(k in updates for k in ("global_proxies", "extractor_proxies", "transport_routes")):
                self.extractors.clear()
                logger.info("Extractor cache cleared due to config change")

        return web.json_response({"status": "ok", "updated": list(updates.keys())})

    async def handle_admin_api_warp_toggle(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")

        enable = data.get("enable", False)
        config_store.set("enable_warp", bool(enable))
        reload_config()
        clear_proxy_affinity()
        self.extractors.clear()
        self._warp_check_ts = 0  # Force refresh on next status check

        status = "enabled" if enable else "disabled"
        if enable:
            logger.info("WARP enabled via admin panel")
        else:
            logger.info("WARP disabled via admin panel")

        return web.json_response({"status": "ok", "warp": status})

    async def handle_admin_api_warp_reconnect(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        result = await self.reconnect_warp()
        status_code = 200 if result.get("status") == "ok" else 500
        return web.json_response(result, status=status_code)

    async def handle_admin_api_extractor_proxy(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
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
        self.extractors.clear()

        return web.json_response({"status": "ok", "extractor": extractor, "proxy": proxy or None})

    async def handle_admin_api_download(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        data = config_store.get_all()
        json_str = json.dumps(data, indent=2)
        return web.Response(
            body=json_str,
            content_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="easyproxy_config.json"'
            }
        )

    async def handle_admin_api_upload(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
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
            self.extractors.clear()
            return web.json_response({"status": "ok", "message": "Config imported successfully"})
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON file")
        except Exception as e:
            logger.error(f"Config upload failed: {e}")
            return web.Response(status=500, text=f"Upload failed: {e}")
