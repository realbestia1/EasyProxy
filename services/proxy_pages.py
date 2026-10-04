import os
import json
import time
import asyncio
import shutil
import urllib.parse
import urllib.request
import platform
import tarfile
import threading
import zipfile
import tempfile
import services.proxy_shared as _shared
from services import wg_tunnels
from services import tor_proxy
from services.proxy_shared import (
    logger, web, APP_VERSION,
    check_password, get_client_ip, PlaylistBuilder, ClientSession, ClientTimeout,
    TCPConnector, ProxyConnector, get_connector_for_proxy, API_PASSWORD,
    get_public_base_url,
)
from extractors.registry import *
import config_store
import config as _config
from config import (
    reload_config,
    clear_proxy_affinity,
    get_system_stats,
    get_memory_profile,
    reset_memory_profiler,
)

_SPEEDTEST_LOCK = asyncio.Lock()
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
                "/api/memory/profile": "Profiler tracemalloc: allocazioni Python e crescita dal boot",
                "/api/memory/profile/reset": "POST: resetta il baseline del profiler",
                "/api/dual/memory": "RAM used by the integrated DUAL service",
                "/dual/manifest.m3u8": "DUAL HLS master with synchronized video + audio - ?d=<Base64 JSON>",
                "/dual/sync/links": "DUAL JSON test for synchronizing video and audio",
                "/dual/cache/status": "Checks only whether the DUAL offset exists in the shared MongoDB cache",
                "/dual/aud/{hid}/audio.m3u8": "Synchronized DUAL audio playlist",
                "/dual/aud/{hid}/init.mp4": "DUAL audio init segment",
                "/dual/aud/{hid}/s{idx}.m4s": "DUAL audio segment",
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

    async def handle_memory_profile(self, request):
        """Return top Python allocations and growth since the profiler baseline."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        return web.json_response(get_memory_profile(request.query.get("limit", 30)))

    async def handle_memory_profile_reset(self, request):
        """Reset the tracemalloc baseline used by the memory profiler."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        return web.json_response(reset_memory_profiler())

    async def handle_openapi(self, request):
        """Espone una specifica OpenAPI minimale per Swagger/ReDoc."""
        server_url = get_public_base_url(request)
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

        version = APP_VERSION

        spec = {
            "openapi": "3.0.3",
            "info": {
                "title": "EasyProxy API",
                "version": version,
                    "description": (
                        "Interactive documentation for EasyProxy. "
                        "Includes HLS/MPD proxying, extractor endpoints, key and license helpers, "
                        "playlist generation, admin API, DVR/recording management, "
                        "DUAL video/audio synchronization with VLC HLS master generation, "
                        "and compatibility endpoints inspired by MediaFlow Proxy."
                ),
            },
            "servers": [{"url": server_url}],
            "components": {
                "securitySchemes": security_schemes,
                "schemas": {
                    "DualSource": {
                        "type": "object",
                        "description": "Direct URL or extractor source. Use url for a direct manifest, or extractor + d for an extractor page.",
                        "properties": {
                            "url": {"type": "string", "format": "uri"},
                            "extractor": {"type": "string", "example": "vixsrc"},
                            "d": {"type": "string", "format": "uri"},
                            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                            "warp_off": {"type": "boolean", "default": False},
                            "proxy_off": {"type": "boolean", "default": False},
                            "proxy": {"type": "string", "description": "Optional forced proxy URL or off."},
                        },
                    },
                    "DualSyncRequest": {
                        "type": "object",
                        "required": ["video", "audio", "audio_lang"],
                        "properties": {
                            "video": {"$ref": "#/components/schemas/DualSource"},
                            "audio": {"$ref": "#/components/schemas/DualSource"},
                            "audio_lang": {
                                "type": "string",
                                "description": "HLS LANGUAGE code or NAME alias.",
                                "enum": ["ita", "eng", "spa", "fra", "deu", "hin", "rus", "it", "en", "es", "fr", "de", "hi", "ru"],
                                "example": "ita",
                            },
                            "resolution": {"type": "integer", "enum": [720, 1080, 1440, 2160], "description": "Optional override. Omit it to select the highest available video quality automatically.", "example": 2160},
                            "bypass_audio_language": {"type": "boolean", "default": False, "description": "When true, ignore a language mismatch and use the DEFAULT or best available audio track."},
                            "reference_audio_url": {"type": "string", "format": "uri"},
                            "media_key": {"type": "string"},
                            "video_fingerprint": {"type": "string"},
                        },
                        "example": {
                            "video": {"url": "https://info.movieboxnoob.cc/playlist/ZgRENwVpICUbvjgdTAAjvA.m3u8"},
                            "audio": {"extractor": "vixsrc", "d": "https://vixsrc.to/movie/1339713/"},
                            "audio_lang": "ita",
                        },
                    },
                },
            },
            "paths": {

                # --- System & Public ---

                "/api/info": {
                    "get": {
                        "summary": "Server information",
                        "description": "Returns server status, loaded extractors, modules, and example endpoints.",
                        "responses": {"200": {"description": "Server information JSON"}},
                    }
                },
                "/api/dual/memory": {
                    "get": {
                        "summary": "DUAL memory usage",
                        "description": "Returns RSS used by the in-process DUAL service and its active audio tracks.",
                        "responses": {
                            "200": {"description": "DUAL memory usage JSON"},
                            "401": {"description": "Invalid API password"},
                        },
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/manifest.m3u8": {
                    "get": {
                        "summary": "DUAL HLS master",
                        "description": "Builds one HLS master containing a selected video and an extracted, synchronized audio track. The video is always served through EasyProxy's HLS proxy. The d parameter is URL-safe Base64 JSON.",
                        "parameters": [
                            {"name": "d", "in": "query", "required": True, "schema": {"type": "string"}, "description": "URL-safe Base64 JSON DualSyncRequest payload."},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {
                            "200": {"description": "Combined HLS master playlist", "content": {"application/vnd.apple.mpegurl": {"schema": {"type": "string"}}}},
                            "400": {"description": "Invalid Base64 JSON descriptor or source"},
                            "401": {"description": "Invalid API password"},
                            "409": {"description": "Synchronization unavailable; JSON error response"},
                            "502": {"description": "Extraction or upstream failure"},
                        },
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/sync/links": {
                    "post": {
                        "summary": "Sync direct or extracted video/audio links",
                        "description": "Resolves the two sources, selects the requested HLS audio language/NAME alias, prepares audio in memory, calculates the offset, and returns the synchronized result as JSON.",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/DualSyncRequest"}}},
                        },
                        "responses": {
                            "200": {"description": "Synchronization result"},
                            "400": {"description": "Invalid source or language"},
                            "401": {"description": "Invalid API password"},
                            "409": {"description": "Synchronization failed"},
                            "502": {"description": "Extraction or upstream failure"},
                        },
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/aud/{hid}/audio.m3u8": {
                    "get": {
                        "summary": "Serve synchronized audio playlist",
                        "description": "Returns the generated fragmented MP4 audio playlist with the requested offset and playback rate.",
                        "parameters": [
                            {"name": "hid", "in": "path", "required": True, "schema": {"type": "string"}},
                            {"name": "o", "in": "query", "schema": {"type": "integer", "default": 0}, "description": "Offset in milliseconds."},
                            {"name": "r", "in": "query", "schema": {"type": "integer", "default": 1000000000}, "description": "Playback rate in nano-units."},
                            {"name": "t", "in": "query", "required": True, "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Audio HLS playlist"}, "401": {"description": "Invalid DUAL session"}, "410": {"description": "Audio session expired"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/aud/{hid}/init.mp4": {
                    "get": {
                        "summary": "Serve audio fragmented MP4 init",
                        "description": "Returns the initialization fragment for the active DUAL audio track.",
                        "parameters": [{"name": "hid", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "t", "in": "query", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "MP4 initialization fragment"}, "401": {"description": "Invalid DUAL session"}, "410": {"description": "Audio session expired"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/aud/{hid}/s{idx}.m4s": {
                    "get": {
                        "summary": "Serve audio fragmented MP4 segment",
                        "description": "Returns one active DUAL audio segment after applying the requested offset/rate.",
                        "parameters": [{"name": "hid", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "idx", "in": "path", "required": True, "schema": {"type": "integer"}}, {"name": "o", "in": "query", "schema": {"type": "integer"}}, {"name": "r", "in": "query", "schema": {"type": "integer"}}, {"name": "t", "in": "query", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Audio segment"}, "401": {"description": "Invalid DUAL session"}, "410": {"description": "Audio session expired"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/dual/cache/status": {
                    "post": {
                        "summary": "Check cached DUAL offset",
                        "description": "Returns only whether the requested video/audio offset exists in the shared MongoDB cache. Audio is not exposed as a persistent cache.",
                        "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                        "responses": {"200": {"description": "Offset cache status JSON"}, "400": {"description": "Missing media key, resolution or fingerprint"}, "401": {"description": "Invalid API password"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Health check",
                        "description": "Simple health check endpoint returning OK status and app version.",
                        "responses": {"200": {"description": "JSON with status and version"}},
                    }
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
                "/proxy/ip": {
                    "get": {
                        "summary": "Resolve public IP",
                        "description": "Returns the public IP as seen through the configured proxy route.",
                        "responses": {"200": {"description": "Public IP response"}},
                    }
                },

                # --- Proxy & Streaming ---

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
                            {"name": "host", "in": "query", "schema": {"type": "string"}, "description": "Force a specific extractor instead of auto-detection"},
                            {"name": "max_res", "in": "query", "schema": {"type": "boolean"}, "description": "Serve only the highest video variant"},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Proxied HLS manifest"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/hls/segment.{format}": {
                    "get": {
                        "summary": "HLS segment compatibility",
                        "description": "Serve HLS segments (ts, m4s, mp4, vtt) through the proxy with proper headers.",
                        "parameters": [
                            {"name": "format", "in": "path", "schema": {"type": "string", "enum": ["ts", "m4s", "mp4", "vtt"]}, "required": True},
                        ],
                        "responses": {"200": {"description": "Segment data"}},
                    }
                },
                "/proxy/mpd/manifest.m3u8": {
                    "get": {
                        "summary": "Proxy MPD as HLS",
                        "description": "Converts or relays MPEG-DASH/MPD streams through EasyProxy.",
                        "parameters": [
                            {"name": "d", "in": "query", "schema": {"type": "string"}, "required": True, "description": "Destination MPD URL"},
                            {"name": "host", "in": "query", "schema": {"type": "string"}, "description": "Force a specific extractor instead of auto-detection"},
                            {"name": "max_res", "in": "query", "schema": {"type": "boolean"}, "description": "Serve only the highest video variant"},
                            {"name": "key_id", "in": "query", "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Generated HLS manifest"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/mpd/manifest.mpd": {
                    "get": {
                        "summary": "Proxy MPD native",
                        "description": "Proxy the native MPD manifest for DASH streams.",
                        "parameters": [
                            {"name": "d", "in": "query", "schema": {"type": "string"}, "required": True, "description": "Destination MPD URL"},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Proxied MPD manifest"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/proxy/mpd/segment/{session_id}/{tail:.*}": {
                    "get": {
                        "summary": "DASH segment serving",
                        "description": "Serve DASH segments for an active MPD-to-HLS conversion session.",
                        "parameters": [
                            {"name": "session_id", "in": "path", "schema": {"type": "string"}, "required": True},
                            {"name": "tail", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Segment data"}},
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
                "/segment/{tail:.*}": {
                    "get": {
                        "summary": "Legacy TS segment serving",
                        "description": "Serve TS segments by their segment identifier.",
                        "parameters": [
                            {"name": "tail", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Segment data"}},
                    }
                },
                "/decrypt/segment.{format}": {
                    "get": {
                        "summary": "Legacy decrypt segment",
                        "description": "Decrypt and serve segments using ClearKey (legacy mode).",
                        "parameters": [
                            {"name": "format", "in": "path", "schema": {"type": "string", "enum": ["mp4", "ts"]}, "required": True},
                        ],
                        "responses": {"200": {"description": "Decrypted segment"}},
                    }
                },


                # --- Extractors ---

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
                "/extractor/video.{format}": {
                    "get": {
                        "summary": "Extractor format-suffix variants",
                        "description": "Format-forced extractor variants. Supported extensions: m3u8, mp4, mpd, ts, m4s, vtt, aac, m4a, webm, mkv, avi, mov. All share the same parameters and handler.",
                        "parameters": [
                            {"name": "format", "in": "path", "schema": {"type": "string"}, "required": True},
                            {"name": "host", "in": "query", "schema": {"type": "string"}},
                            {"name": "url", "in": "query", "schema": {"type": "string"}},
                            {"name": "d", "in": "query", "schema": {"type": "string"}},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Extractor response"}},
                        **({"security": security} if requires_password else {}),
                    }
                },

                # --- Keys & DRM ---

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

                # --- Admin API ---

                "/api/admin/login": {
                    "post": {
                        "summary": "Admin login",
                        "description": "Authenticate as admin. Sets a session cookie on success.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "password": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Login success"}, "401": {"description": "Invalid password"}},
                    }
                },
                "/api/admin/config": {
                    "get": {
                        "summary": "Get admin config",
                        "description": "Retrieve the full server configuration as JSON.",
                        "responses": {"200": {"description": "Configuration JSON"}},
                        **({"security": security} if requires_password else {}),
                    },
                    "post": {
                        "summary": "Update admin config",
                        "description": "Update server configuration with a JSON payload.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object", "description": "Partial or full config update"},
                                }
                            },
                        },
                        "responses": {"200": {"description": "Config updated"}},
                        **({"security": security} if requires_password else {}),
                    },
                },
                "/api/admin/config/download": {
                    "get": {
                        "summary": "Download config as file",
                        "description": "Download the current configuration as a downloadable file.",
                        "responses": {"200": {"description": "Config file download"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/api/admin/config/upload": {
                    "post": {
                        "summary": "Upload config file",
                        "description": "Upload a configuration file to replace the current server config.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}},
                                }
                            },
                        },
                        "responses": {"200": {"description": "Config uploaded and reloaded"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/api/admin/warp/toggle": {
                    "post": {
                        "summary": "Toggle WARP",
                        "description": "Enable or disable Cloudflare WARP proxy routing.",
                        "responses": {"200": {"description": "WARP toggled"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/api/admin/warp/reconnect": {
                    "post": {
                        "summary": "Reconnect WARP",
                        "description": "Force WARP to disconnect, re-register, and reconnect.",
                        "responses": {"200": {"description": "WARP reconnected"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/api/admin/extractor/proxy": {
                    "post": {
                        "summary": "Set extractor proxy",
                        "description": "Override the proxy used by a specific extractor for testing.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "extractor": {"type": "string"},
                                            "proxy": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Extractor proxy updated"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/api/admin/speedtest": {
                    "post": {
                        "summary": "Run speed test",
                        "description": "Test download speed through a specified proxy or direct connection.",
                        "responses": {"200": {"description": "Speed test results"}},
                        **({"security": security} if requires_password else {}),
                    }
                },

                # --- DVR / Recordings ---

                "/recordings": {
                    "get": {
                        "summary": "Recordings UI page",
                        "description": "DVR/recording management web interface.",
                        "responses": {"200": {"description": "HTML page"}},
                    }
                },
                "/record": {
                    "get": {
                        "summary": "Start recording via GET",
                        "description": "Quick-start a recording from a URL query parameter and redirect to the live stream while recording.",
                        "parameters": [
                            {"name": "url", "in": "query", "schema": {"type": "string"}, "required": True},
                            {"name": "name", "in": "query", "schema": {"type": "string"}},
                            {"name": "duration", "in": "query", "schema": {"type": "integer"}, "description": "Recording duration in seconds"},
                            {"name": "extractor", "in": "query", "schema": {"type": "string"}, "description": "Force a specific extractor instead of auto-detection"},
                            {"name": "max_res", "in": "query", "schema": {"type": "boolean"}, "description": "Record only the highest video variant"},
                            {"name": "key_id", "in": "query", "schema": {"type": "string"}, "description": "ClearKey key ID for DRM-protected streams"},
                            {"name": "key", "in": "query", "schema": {"type": "string"}, "description": "ClearKey key for DRM-protected streams"},
                            {"name": "api_password", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Recording started"}},
                        **({"security": security} if requires_password else {}),
                    }
                },
                "/record/stop/{id}": {
                    "get": {
                        "summary": "Stop recording via GET",
                        "description": "Stop a recording by ID via GET request.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording stopped"}},
                    }
                },
                "/api/recordings": {
                    "get": {
                        "summary": "List recordings",
                        "description": "Get a paginated list of all recordings, with optional status filter.",
                        "parameters": [
                            {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["active", "completed", "failed"]}},
                        ],
                        "responses": {"200": {"description": "Recordings list"}},
                    }
                },
                "/api/recordings/active": {
                    "get": {
                        "summary": "Active recordings",
                        "description": "Get a list of currently active recordings.",
                        "responses": {"200": {"description": "Active recordings list"}},
                    }
                },
                "/api/recordings/start": {
                    "post": {
                        "summary": "Start recording",
                        "description": "Start a new DVR recording for a specified stream URL.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["url"],
                                        "properties": {
                                            "url": {"type": "string", "description": "Stream URL to record"},
                                            "name": {"type": "string", "description": "Human-readable recording name"},
                                            "duration": {"type": "integer", "description": "Recording duration in seconds"},
                                            "extractor": {"type": "string", "description": "Force a specific extractor instead of auto-detection"},
                                            "max_res": {"type": "boolean", "description": "Record only the highest video variant"},
                                            "warp": {"type": "string", "enum": ["off"], "description": "Bypass WARP for this recording"},
                                            "proxy": {"type": "string", "enum": ["off"], "description": "Bypass configured proxies for this recording"},
                                            "disable_ssl": {"type": "string", "enum": ["1"], "description": "Disable SSL verification for this recording"},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Recording started"}, "400": {"description": "Invalid request"}},
                    }
                },
                "/api/recordings/{id}": {
                    "get": {
                        "summary": "Get recording details",
                        "description": "Retrieve metadata for a specific recording.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording metadata"}},
                    },
                    "delete": {
                        "summary": "Delete recording",
                        "description": "Delete a recording by ID.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording deleted"}},
                    },
                },
                "/api/recordings/{id}/delete": {
                    "get": {
                        "summary": "Delete recording via GET",
                        "description": "Delete a recording by ID using a GET request (legacy compatibility).",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording deleted"}},
                    }
                },
                "/api/recordings/{id}/stop": {
                    "post": {
                        "summary": "Stop recording",
                        "description": "Stop an active recording by ID.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording stopped"}},
                    }
                },
                "/api/recordings/{id}/download": {
                    "get": {
                        "summary": "Download recording",
                        "description": "Download a completed recording file.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "Recording file download"}},
                    }
                },
                "/api/recordings/{id}/stream": {
                    "get": {
                        "summary": "Stream recording",
                        "description": "Stream a completed recording as HLS.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}, "required": True},
                        ],
                        "responses": {"200": {"description": "HLS stream"}},
                    }
                },
                "/api/recordings/all": {
                    "delete": {
                        "summary": "Delete all recordings",
                        "description": "Delete all recordings, optionally filtering by status.",
                        "parameters": [
                            {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["active", "completed", "failed"]}},
                        ],
                        "responses": {"200": {"description": "All matching recordings deleted"}},
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
                bypass_proxies = item.get("proxy") == "off"
                bypass_tor = item.get("tor") == "off"

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

                # Aggiungi bypass proxy se richiesto
                if bypass_proxies:
                    params.append("proxy=off")

                # Aggiungi bypass Tor se richiesto
                if bypass_tor:
                    params.append("tor=off")

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
        self._invalidate_extractors()

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
            self._invalidate_extractors()
            return web.json_response({"status": "ok", "message": "Config imported successfully"})
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON file")
        except Exception as e:
            logger.error(f"Config upload failed: {e}")
            return web.Response(status=500, text=f"Upload failed: {e}")

    async def handle_admin_api_speedtest(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
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
            output = []
            # Run routes one at a time. Concurrent Ookla tests compete for the
            # same uplink/downlink and make the displayed comparison invalid.
            async with _SPEEDTEST_LOCK:
                for route in routes:
                    try:
                        res = await asyncio.to_thread(self._run_speedtest, route["proxy"])
                    except Exception as exc:
                        output.append({"name": route["name"], "error": str(exc)})
                    else:
                        res["name"] = route["name"]
                        output.append(res)
            return web.json_response({"results": output})
        except Exception as e:
            logger.error(f"Speedtest failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_admin_api_test_extractors(self, request):
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized")
        from extractors.test_urls import REMOTE_TEST_URLS_URL

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
            return web.json_response(
                {"error": f"Could not load test URLs from gist: {e}",
                 "results": [], "skipped": []},
                status=502,
            )

        headers = dict(request.headers)
        configured = {k: u for k, u in urls.items() if u}
        skipped = sorted(k for k, u in urls.items() if not u)
        results = []
        sem = asyncio.Semaphore(5)

        async def _test(name, url):
            async with sem:
                entry = {"extractor": name, "url": url, "ok": False}
                try:
                    extractor = await self.get_extractor(url, headers, host=name)
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
        return web.json_response({"results": results, "skipped": skipped})

    def _ensure_speedtest_exe(self):
        import subprocess
        import os as _os
        import platform as _platform
        home = _os.path.expanduser("~")
        auto_install_paths = [
            _os.path.join(_os.environ.get("LOCALAPPDATA", home), "OoklaSpeedtest", "speedtest.exe"),
            _os.path.join(home, ".local", "share", "easyproxy", "bin", "speedtest"),
        ]
        system_paths = [
            "/usr/local/bin/speedtest",
            "/usr/bin/speedtest",
            "speedtest.exe",
            "speedtest",
        ]
        for p in auto_install_paths + system_paths:
            if _os.path.exists(p):
                return p
        found = shutil.which("speedtest")
        if found:
            return found

        # Auto-download Ookla Speedtest CLI
        system = _platform.system().lower()
        machine = _platform.machine().lower()
        version = "1.2.0"
        try:
            if system in ("windows", "win32"):
                url = f"https://install.speedtest.net/app/cli/ookla-speedtest-{version}-win64.zip"
                install_dir = _os.path.dirname(auto_install_paths[0])
                target = auto_install_paths[0]
                member = "speedtest.exe"
                archive_cls = zipfile.ZipFile
                archive_mode = "r"
            elif system == "linux":
                arch_map = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
                arch = arch_map.get(machine)
                if not arch:
                    raise RuntimeError(f"Unsupported Linux architecture: {machine}")
                url = f"https://install.speedtest.net/app/cli/ookla-speedtest-{version}-linux-{arch}.tgz"
                install_dir = _os.path.dirname(auto_install_paths[1])
                target = auto_install_paths[1]
                member = "speedtest"
                archive_cls = tarfile.open
                archive_mode = "r:gz"
            else:
                raise RuntimeError(f"Auto-install not supported on {system}. Please install Ookla Speedtest CLI manually.")

            _os.makedirs(install_dir, exist_ok=True)
            suffix = ".zip" if system in ("windows", "win32") else ".tgz"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            _os.close(fd)
            try:
                urllib.request.urlretrieve(url, tmp_path)
                with archive_cls(tmp_path, archive_mode) as archive:
                    archive.extract(member, install_dir)
                _os.chmod(target, 0o755)
            finally:
                if _os.path.exists(tmp_path):
                    _os.remove(tmp_path)
            return target
        except Exception as e:
            raise RuntimeError(
                f"Failed to auto-install Speedtest CLI: {e}.\n"
                "Install it manually:\n"
                "  Windows: https://www.speedtest.net/apps/cli\n"
                "  Linux: sudo apt install speedtest\n"
                "  Docker: see Dockerfile"
            )

    def _run_speedtest(self, proxy_url=None):
        import subprocess
        import os as _os
        if proxy_url:
            return self._run_proxy_speedtest(proxy_url)

        exe = self._ensure_speedtest_exe()
        command = [exe, "--format", "json", "--accept-license", "--accept-gdpr"]

        # DIRECT must not inherit a proxy from the container/VPS shell.
        env = _os.environ.copy()
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)
        try:
            result = subprocess.run(
                command,
                capture_output=True, text=True, timeout=60, env=env
            )
            if result.returncode != 0:
                err = result.stderr
                if "Network is unreachable" in err or "Cannot retrieve configuration" in err:
                    raise RuntimeError("No internet connection. Check your network.")
                raise RuntimeError(f"Speedtest failed: {err.split('[')[-1].rstrip(']') if '[' in err else err[:100]}")
            data = json.loads(result.stdout)
            return {
                "server": {
                    "sponsor": data.get("server", {}).get("sponsor", "Unknown"),
                    "name": data.get("server", {}).get("name", "Unknown"),
                    "location": data.get("server", {}).get("location", "Unknown")
                },
                "download_mbps": round(data.get("download", {}).get("bandwidth", 0) * 8 / 1_000_000, 1),
                "upload_mbps": round(data.get("upload", {}).get("bandwidth", 0) * 8 / 1_000_000, 1),
                "ping_ms": round(data.get("ping", {}).get("latency", 0), 1)
            }
        except subprocess.TimeoutExpired:
            raise RuntimeError("Speedtest timed out after 60 seconds")
        except json.JSONDecodeError:
            raise RuntimeError("Failed to parse speedtest output")

    def _proxy_curl_args(self, proxy_url):
        """Return curl arguments that force DNS and TCP through one proxy."""
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

    def _run_proxy_curl(self, proxy_url, url, write_out, extra=None, timeout=60, allow_timeout=False):
        import subprocess
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise RuntimeError("Proxy speedtest unavailable: curl is not installed")

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
            raise RuntimeError(f"Proxy test timed out: {proxy_url}")
        if result.returncode != 0 and not (allow_timeout and result.returncode == 28):
            message = (result.stderr or "proxy connection failed").strip()
            raise RuntimeError(f"Proxy test failed: {message[:180]}")
        return result.stdout.strip()

    def _run_proxy_stream_upload(self, proxy_url, url, duration=10):
        """Stream zeroes for a fixed time through a proxy and return curl metrics."""
        import subprocess
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise RuntimeError("Proxy speedtest unavailable: curl is not installed")

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
            stdout, stderr = process.communicate(timeout=duration + 15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError(f"Proxy upload test timed out: {proxy_url}")
        feeder.join(timeout=2)
        if process.returncode not in (0, 28):
            message = stderr.decode(errors="replace").strip() or "proxy connection failed"
            raise RuntimeError(f"Proxy upload test failed: {message[:180]}")
        return stdout.decode(errors="replace").strip()

    def _run_proxy_speedtest(self, proxy_url):
        for attempt in range(2):
            try:
                return self._measure_proxy_speedtest(proxy_url)
            except RuntimeError as exc:
                # A stalled circuit (common on Tor) can drop one leg of the test.
                if "no payload" not in str(exc) or attempt:
                    raise

    def _measure_proxy_speedtest(self, proxy_url):
        """Measure real proxied TCP throughput; Ookla's static binary ignores proxies."""
        devnull = os.devnull
        ip = self._run_proxy_curl(
            proxy_url,
            "https://api.ipify.org",
            "",
            extra=["--output", "-"],
        )
        ping_ms = float(self._run_proxy_curl(
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
                metrics = self._run_proxy_curl(
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
            raise last_error or RuntimeError(f"Proxy test returned no payload: {proxy_url}")
        download_bytes, download_speed, download_time = download

        upload, last_error = None, None
        for endpoint in (
            "https://speed.cloudflare.com/__up",
            "https://librespeed.org/backend/empty.php",
        ):
            try:
                metrics = self._run_proxy_stream_upload(proxy_url, endpoint, duration=10)
            except RuntimeError as exc:
                last_error = exc
                continue
            upload_bytes, upload_speed, upload_time = metrics.split("\t")
            if float(upload_bytes) > 0:
                upload = (upload_bytes, upload_speed, upload_time)
                break
        if upload is None:
            raise last_error or RuntimeError(f"Proxy test returned no payload: {proxy_url}")
        upload_bytes, upload_speed, upload_time = upload

        return {
            "server": {
                "sponsor": "Proxy throughput",
                "name": "proof.ovh.net + speed.cloudflare.com",
                "location": "via proxy",
            },
            "proxy_used": proxy_url,
            "external_ip": ip,
            "download_mbps": round(float(download_speed) * 8 / 1_000_000, 1),
            "upload_mbps": round(float(upload_speed) * 8 / 1_000_000, 1),
            "ping_ms": round(ping_ms, 1),
        }
