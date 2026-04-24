import asyncio
import logging
import random
import re
import sys
import os
import time
import socket
import urllib.parse
from urllib.parse import urlparse, urljoin
import base64
import binascii
import hashlib
import hmac
import json
import ssl
import yarl
import aiohttp
from aiohttp import (
    web,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    ClientPayloadError,
    ServerDisconnectedError,
    ClientConnectionError,
)
from aiohttp_socks import ProxyConnector

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    CurlAsyncSession = None

from config import (
    GLOBAL_PROXIES,
    TRANSPORT_ROUTES,
    get_proxy_for_url,
    get_ssl_setting_for_url,
    get_connector_for_proxy,
    API_PASSWORD,
    check_password,
    MPD_MODE,
    VERSION_MODE,
    APP_VERSION,
    ENABLE_WARP,
    ENABLE_REMUXING,
    WARP_EXCLUDE_DOMAINS,
    WARP_PROXY_URL,
    BYPASS_WARP_CONTEXT,
    SELECTED_PROXY_CONTEXT,
)
from extractors.generic import GenericHLSExtractor, ExtractorError
from services.manifest_rewriter import ManifestRewriter

# Global registry for domains already bypassed in WARP to avoid redundant os.system calls
BYPASSED_WARP_DOMAINS = set()

# Legacy MPD converter (used when MPD_MODE is not ffmpeg)
MPDToHLSConverter = None
decrypt_segment = None
if MPD_MODE in ("legacy", "none", "disabled"):
    try:
        from utils.mpd_converter import MPDToHLSConverter
        from utils.drm_decrypter import decrypt_segment

        logger = logging.getLogger(__name__)
        logger.info("✅ Legacy MPD modules loaded (mpd_converter, drm_decrypter)")
    except ImportError as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"⚠️ MPD_MODE=legacy but modules not found: {e}")

# --- Moduli Esterni ---
(
    VavooExtractor,
    VixSrcExtractor,
    PlaylistBuilder,
    SportsonlineExtractor,
) = None, None, None, None
(
    MixdropExtractor,
    VoeExtractor,
    StreamtapeExtractor,
    OrionExtractor,
    FreeshotExtractor,
) = None, None, None, None, None
# New extractors
(
    DoodStreamExtractor,
    FastreamExtractor,
    FileLionsExtractor,
    FileMoonExtractor,
    LuluStreamExtractor,
) = None, None, None, None, None
(
    MaxstreamExtractor,
    OkruExtractor,
    StreamWishExtractor,
    SupervideoExtractor,
    UqloadExtractor,
    DroploadExtractor,
) = None, None, None, None, None, None
(
    VidmolyExtractor,
    VidozaExtractor,
    TurboVidPlayExtractor,
    LiveTVExtractor,
    F16PxExtractor,
) = None, None, None, None, None
StreamHGExtractor = None
CinemaCityExtractor = None
DeltabitExtractor = None


logger = logging.getLogger(__name__)

_SPORTSONLINE_PATH_PATTERNS = (
    re.compile(r"/channels/[a-z0-9_-]+/[a-z0-9_-]+\.php(?:$|[?#])", re.IGNORECASE),
    re.compile(r"/hd/hd\d+\.php(?:$|[?#])", re.IGNORECASE),
)


def _is_sportsonline_candidate(value: str) -> bool:
    raw_value = (value or "").strip().lower()
    return any(pattern.search(raw_value) for pattern in _SPORTSONLINE_PATH_PATTERNS)


def _resolve_sportsonline_proxy(url: str, bypass_warp: bool = False) -> str | None:
    # Priority requested: real URL first, then legacy aliases.
    ordered_candidates = [url, "sportzsonline", "sportzonline", "sportsonline"]

    # Route-aware pass: preserve explicit TRANSPORT_ROUTES matches in priority order.
    for candidate in ordered_candidates:
        if any(
            route.get("url") and route["url"] in candidate for route in TRANSPORT_ROUTES
        ):
            return get_proxy_for_url(candidate, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)

    # Fallback to default behavior (global proxy or direct).
    return get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)


# Importazione condizionale degli estrattori
try:
    from extractors.freeshot import FreeshotExtractor
    logger.info("✅ FreeshotExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FreeshotExtractor module not found.")

try:
    from extractors.vavoo import VavooExtractor
    logger.info("✅ VavooExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VavooExtractor module not found. Vavoo functionality disabled.")

try:
    from routes.playlist_builder import PlaylistBuilder
    logger.info("✅ PlaylistBuilder module loaded.")
except ImportError:
    logger.warning("⚠️ PlaylistBuilder module not found. PlaylistBuilder functionality disabled.")

try:
    from extractors.vixsrc import VixSrcExtractor
    logger.info("✅ VixSrcExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VixSrcExtractor module not found. VixSrc functionality disabled.")

try:
    from extractors.sportsonline import SportsonlineExtractor
    logger.info("✅ SportsonlineExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ SportsonlineExtractor module not found. Sportsonline functionality disabled.")

try:
    from extractors.mixdrop import MixdropExtractor
    logger.info("✅ MixdropExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ MixdropExtractor module not found.")

try:
    from extractors.voe import VoeExtractor
    logger.info("✅ VoeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VoeExtractor module not found.")

try:
    from extractors.streamtape import StreamtapeExtractor
    logger.info("✅ StreamtapeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamtapeExtractor module not found.")

try:
    from extractors.orion import OrionExtractor
    logger.info("✅ OrionExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ OrionExtractor module not found.")

try:
    from extractors.doodstream import DoodStreamExtractor
    logger.info("✅ DoodStreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DoodStreamExtractor module not found.")

try:
    from extractors.fastream import FastreamExtractor
    logger.info("✅ FastreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FastreamExtractor module not found.")

try:
    from extractors.filelions import FileLionsExtractor
    logger.info("✅ FileLionsExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FileLionsExtractor module not found.")

try:
    from extractors.filemoon import FileMoonExtractor
    logger.info("✅ FileMoonExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FileMoonExtractor module not found.")

try:
    from extractors.lulustream import LuluStreamExtractor
    logger.info("✅ LuluStreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ LuluStreamExtractor module not found.")

try:
    from extractors.maxstream import MaxstreamExtractor
    logger.info("✅ MaxstreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ MaxstreamExtractor module not found.")

try:
    from extractors.okru import OkruExtractor
    logger.info("✅ OkruExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ OkruExtractor module not found.")

try:
    from extractors.streamwish import StreamWishExtractor
    logger.info("✅ StreamWishExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamWishExtractor module not found.")

try:
    from extractors.streamhg import StreamHGExtractor
    logger.info("✅ StreamHGExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamHGExtractor module not found.")

try:
    from extractors.supervideo import SupervideoExtractor
    logger.info("✅ SupervideoExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ SupervideoExtractor module not found.")

try:
    from extractors.uqload import UqloadExtractor
    logger.info("✅ UqloadExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ UqloadExtractor module not found.")

try:
    from extractors.dropload import DroploadExtractor
    logger.info("✅ DroploadExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DroploadExtractor module not found.")

try:
    from extractors.vidmoly import VidmolyExtractor
    logger.info("✅ VidmolyExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VidmolyExtractor module not found.")

try:
    from extractors.vidoza import VidozaExtractor
    logger.info("✅ VidozaExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VidozaExtractor module not found.")

try:
    from extractors.turbovidplay import TurboVidPlayExtractor
    logger.info("✅ TurboVidPlayExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ TurboVidPlayExtractor module not found.")

try:
    from extractors.livetv import LiveTVExtractor
    logger.info("✅ LiveTVExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ LiveTVExtractor module not found.")

try:
    from extractors.f16px import F16PxExtractor
    logger.info("✅ F16PxExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ F16PxExtractor module not found.")

try:
    from extractors.dlstreams import DLStreamsExtractor
    logger.info("✅ DLStreamsExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DLStreamsExtractor module not found.")

try:
    from extractors.cinemacity import CinemaCityExtractor
    logger.info("✅ CinemaCityExtractor module loaded.")
except Exception as e:
    logger.warning("⚠️ CinemaCityExtractor module not found or failed to load: %s", e)
    CinemaCityExtractor = None

try:
    from extractors.deltabit import DeltabitExtractor
    logger.info("✅ DeltabitExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DeltabitExtractor module not found.")


class HLSProxy:
    """Proxy HLS per gestire stream Vavoo, DLHD, HLS generici e playlist builder con supporto AES-128"""

    def __init__(self, ffmpeg_manager=None):
        self.extractors = {}
        self.ffmpeg_manager = ffmpeg_manager

        # Inizializza il playlist_builder se il modulo è disponibile
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder inizializzato")
        else:
            self.playlist_builder = None

        # Cache per segmenti di inizializzazione (URL -> content)
        self.init_cache = {}

        # Cache per segmenti decriptati (URL -> (content, timestamp))
        self.segment_cache = {}
        self.segment_cache_ttl = 30  # Seconds

        # Prefetch queue for background downloading
        self.prefetch_tasks = set()

        # Sessione condivisa per il proxy (no proxy)
        self.session = None
        self.flex_session = None

        # Registry for HLS URL shortening (to handle extremely long multi-path URLs)
        # url_id -> (actual_url, timestamp, ttl)
        self.hls_url_map = {}
        self.hls_url_ttl = 3600
        self.hls_url_ttl_cinemacity = 10800
        self.hls_url_max_entries = 2000
        
        # Cache for proxy sessions (proxy_url -> session)
        # This reuses connections for the same proxy to improve performance
        self.proxy_sessions = {}
        self.curl_sessions = {}  # Registry for pooled curl_cffi sessions

        # Version information
        self.latest_version = "Checking..."
        self.warp_status = "Disabled" if not ENABLE_WARP else "Checking..."

        # Version information
        self.latest_version = "Checking..."

    async def shorten_hls_url(self, url: str) -> str:
        """Crea un ID breve per un URL e lo memorizza nella mappa."""
        if not url:
            return ""
        now = time.time()
        current_ttl = (
            self.hls_url_ttl_cinemacity
            if "cinemacity.cc" in url.lower() or "cccdn.net" in url.lower()
            else self.hls_url_ttl
        )
        expired_keys = [
            key for key, (_, ts, ttl) in self.hls_url_map.items()
            if now - ts > ttl
        ]
        for key in expired_keys:
            self.hls_url_map.pop(key, None)

        if len(self.hls_url_map) >= self.hls_url_max_entries:
            oldest_keys = sorted(
                self.hls_url_map.items(),
                key=lambda item: item[1][1]
            )[: max(1, len(self.hls_url_map) - self.hls_url_max_entries + 1)]
            for key, _ in oldest_keys:
                self.hls_url_map.pop(key, None)

        # Usa un hash corto (12 caratteri) per l'URL
        url_id = f"u_{hashlib.md5(url.encode()).hexdigest()[:12]}"
        self.hls_url_map[url_id] = (url, now, current_ttl)
        return url_id

    async def start_tasks(self):
        """Starts background tasks for the proxy."""
        asyncio.create_task(self._update_latest_version())
        # Always start WARP check (universal trace method)
        asyncio.create_task(self._update_warp_status_loop())

    async def _update_warp_status_loop(self):
        """Periodically checks WARP status via Cloudflare trace (Universal)."""
        while True:
            try:
                # We use the proxy session to check if the SOCKS5H proxy is working
                session, _ = await self._get_proxy_session("https://www.cloudflare.com/cdn-cgi/trace")
                async with session.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=5) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        if "warp=on" in text:
                            self.warp_status = "Connected"
                        else:
                            self.warp_status = "Disconnected"
                    else:
                        self.warp_status = "Error"
            except Exception:
                self.warp_status = "Disconnected"
            
            await asyncio.sleep(60) # Check every minute

    async def _update_latest_version(self):
        """Periodically checks GitHub for the latest version in the background."""
        while True:
            await self._refresh_latest_version()
            # Check every hour in background
            await asyncio.sleep(3600)

    async def _refresh_latest_version(self):
        """Checks GitHub config.py for the latest version with cache busting.
        Can be called on-demand (e.g. on page refresh).
        """
        try:
            # Use a timestamp to bypass GitHub's cache
            cache_buster = int(time.time())
            url = f"https://raw.githubusercontent.com/realbestia1/EasyProxy/main/config.py?t={cache_buster}"
            
            # Use a direct session with a short timeout to not block UI too long
            session = await self._get_session()
            async with session.get(url, timeout=2) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # Use regex to find APP_VERSION = "..." or '...'
                    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
                    if match:
                        new_version = match.group(1)
                        if self.latest_version != new_version:
                            self.latest_version = new_version
                            logger.info(f"🆕 Latest version updated: {self.latest_version}")
                    else:
                        if self.latest_version == "Checking...":
                            self.latest_version = "Unknown"
                else:
                    if self.latest_version == "Checking...":
                        self.latest_version = "Error"
        except Exception as e:
            if self.latest_version == "Checking...":
                self.latest_version = "Unknown"
            logger.debug(f"Version check skipped or failed: {e}")

    @staticmethod
    def _strip_fake_png_header_from_ts(content: bytes) -> bytes:
        """
        Some providers prepend a fake 8-byte PNG signature to TS segments.
        Strip it only when bytes after the header still match TS sync markers.
        """
        png_sig = b"\x89PNG\r\n\x1a\n"
        if len(content) <= 8 or not content.startswith(png_sig):
            return content

        ts_payload = content[8:]
        # MPEG-TS sync byte is 0x47 at packet boundaries.
        if not ts_payload or ts_payload[0] != 0x47:
            return content
        if len(ts_payload) > 188 and ts_payload[188] != 0x47:
            return content

        logger.info(
            "Removed fake PNG header from TS segment (%d -> %d bytes)",
            len(content),
            len(ts_payload),
        )
        return ts_payload


    @staticmethod
    def _compute_key_headers(
        key_url: str, secret_key: str, user_agent: str = None
    ) -> tuple[int, int, str, str] | None:
        """
        Compute X-Key-Timestamp, X-Key-Nonce, X-Fingerprint, and X-Key-Path for a /key/ URL.

        Algorithm:
        1. Extract resource and number from URL pattern /key/{resource}/{number}
        2. ts = Unix timestamp in seconds
        3. hmac_hash = HMAC-SHA256(resource, secret_key).hex()
        4. nonce = proof-of-work: find i where MD5(hmac+resource+number+ts+i)[:4] < 0x1000
        5. fingerprint = SHA256(useragent + screen_resolution + timezone + language).hex()[:16]
        6. key_path = HMAC-SHA256("resource|number|ts|fingerprint", secret_key).hex()[:16]

        Args:
            key_url: The key URL containing /key/{resource}/{number}
            secret_key: The HMAC secret key
            user_agent: The user agent string for fingerprint calculation

        Returns:
            Tuple of (timestamp, nonce, fingerprint, key_path) or None if URL doesn't match pattern
        """
        # Extract resource and number from URL
        pattern = r"/key/([^/]+)/(\d+)"
        match = re.search(pattern, key_url)

        if not match:
            return None

        resource = match.group(1)
        number = match.group(2)

        ts = int(time.time())

        # Compute HMAC-SHA256
        hmac_hash = hmac.new(
            secret_key.encode("utf-8"), resource.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Proof-of-work loop
        nonce = 0
        for i in range(100000):
            combined = f"{hmac_hash}{resource}{number}{ts}{i}"
            md5_hash = hashlib.md5(combined.encode("utf-8")).hexdigest()
            prefix_value = int(md5_hash[:4], 16)

            if prefix_value < 0x1000:  # < 4096
                nonce = i
                break

        # Compute fingerprint
        fp_user_agent = (
            user_agent
            if user_agent
            else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
        fp_screen_res = "1920x1080"
        fp_timezone = "UTC"
        fp_language = "en"

        fp_string = f"{fp_user_agent}{fp_screen_res}{fp_timezone}{fp_language}"

        fingerprint = hashlib.sha256(fp_string.encode("utf-8")).hexdigest()[:16]

        # Compute key-path
        key_path_string = f"{resource}|{number}|{ts}|{fingerprint}"
        key_path = hmac.new(
            secret_key.encode("utf-8"), key_path_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

        return ts, nonce, fingerprint, key_path

    async def _get_session(self, prefer_default_family: bool = False, url: str = None):
        if url:
            self._check_dynamic_warp_bypass(url)
        target_attr = "flex_session" if prefer_default_family else "session"
        session = getattr(self, target_attr)
        if session is None or session.closed:
            connector_kwargs = {
                "limit": 0,
                "limit_per_host": 0,
                "keepalive_timeout": 60,
                "enable_cleanup_closed": True,
            }
            if not prefer_default_family:
                connector_kwargs["family"] = socket.AF_INET

            connector = TCPConnector(**connector_kwargs)
            session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None),
                connector=connector,
            )
            setattr(self, target_attr, session)
        return session

    def _check_dynamic_warp_bypass(self, url: str, force: bool = False):
        """Dynamically adds domain to WARP bypass if it matches known patterns or if forced."""
        if not ENABLE_WARP or VERSION_MODE != "Full":
            return
            
        # Patterns for domains that usually block Cloudflare/WARP
        # Cinemacity, VixSrc, etc.
        bypass_patterns = [
            "cccdn.net", "cinemacity.cc", "strem.fun", "torrentio.strem.fun"
        ]
        
        try:
            from urllib.parse import urlsplit
            domain = urlsplit(url).netloc
            if not domain: return
            
            # If domain matches any pattern, has been bypassed yet, or if forced
            is_problematic = force or any(p in domain.lower() for p in bypass_patterns)

            if is_problematic:
                if domain not in BYPASSED_WARP_DOMAINS:
                    # Always bypass base domain for these providers
                    base_domain = ".".join(domain.split(".")[-2:])
                    logging.info(f"⚡ [Dynamic Bypass] Adding {base_domain} (and {domain}) to WARP exclusion list...")
                    
                    os.system(f"warp-cli --accept-tos tunnel host add {base_domain} > /dev/null 2>&1")
                    os.system(f"warp-cli --accept-tos tunnel host add {domain} > /dev/null 2>&1")
                    
                    # In Proxy mode, we must also update the local exclusion list
                    if base_domain not in WARP_EXCLUDE_DOMAINS:
                        WARP_EXCLUDE_DOMAINS.append(base_domain)
                    if domain not in WARP_EXCLUDE_DOMAINS:
                        WARP_EXCLUDE_DOMAINS.append(domain)
                    
                    BYPASSED_WARP_DOMAINS.add(domain)
                    BYPASSED_WARP_DOMAINS.add(base_domain)
                    time.sleep(1.0)
        except Exception as e:
            logging.error(f"❌ Error in dynamic WARP bypass: {e}")

    async def _get_proxy_session(self, url: str, bypass_warp: bool = False, forced_proxy: str | None = None):
        """Get a session with proxy support for the given URL.

        Sessions are cached and reused for the same proxy to improve performance.

        Returns: (session, proxy_url) tuple
        - session: The aiohttp ClientSession to use
        - proxy_url: The proxy URL being used, or None for direct connection
        """
        # Trigger dynamic bypass check before getting proxy settings
        self._check_dynamic_warp_bypass(url, force=bypass_warp)
        
        proxy = forced_proxy or get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)

        prefer_default_family = "ai.the-sunmoon.site/key/" in url

        if proxy:
            # Check if we have a cached session for this proxy
            if proxy in self.proxy_sessions:
                cached_session = self.proxy_sessions[proxy]
                if not cached_session.closed:
                    # logger.debug(f"♻️ Reusing cached proxy session: {proxy}")
                    return cached_session, proxy  # Reuse cached session
                else:
                    # Remove closed session from cache
                    del self.proxy_sessions[proxy]

            # Create new session and cache it
            logger.info(f"🌍 Creating proxy session: {proxy}")
            try:
                # Gestione manuale di socks5h per compatibilità con aiohttp-socks
                connector_url = proxy
                rdns = True # Default per SOCKS5
                if connector_url.startswith("socks5h://"):
                    connector_url = connector_url.replace("socks5h://", "socks5://")
                    rdns = True
                    logger.debug(f"🕵️ SOCKS5h detected: forcing remote DNS resolution")

                # Unlimited connections for maximum speed
                connector = ProxyConnector.from_url(
                    connector_url,
                    limit=0,  # Unlimited connections
                    limit_per_host=0,  # Unlimited per host
                    keepalive_timeout=60,  # Keep connections alive longer
                    family=socket.AF_INET,  # Force IPv4
                    rdns=rdns,
                )
                timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
                session = ClientSession(timeout=timeout, connector=connector)
                self.proxy_sessions[proxy] = session  # Cache the session
                return session, proxy  # Return proxy URL for logging
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to create proxy connector: {e}, falling back to direct"
                )

        # Fallback to shared non-proxy session
        session = await self._get_session(prefer_default_family=prefer_default_family)
        return session, None

    async def _retry_cccdn_request(self, request_target, headers, disable_ssl: bool):
        """Retry cccdn once via an alternate aiohttp route when direct access returns 403."""
        retry_proxy = None
        if ENABLE_WARP and WARP_PROXY_URL and "127.0.0.1" not in WARP_PROXY_URL:
            retry_proxy = WARP_PROXY_URL
        elif ENABLE_WARP and WARP_PROXY_URL:
            from config import is_proxy_alive
            if is_proxy_alive(WARP_PROXY_URL):
                retry_proxy = WARP_PROXY_URL
        elif GLOBAL_PROXIES:
            retry_proxy = GLOBAL_PROXIES[0]

        if not retry_proxy:
            return None

        try:
            connector = get_connector_for_proxy(
                retry_proxy,
                limit=0,
                limit_per_host=0,
                keepalive_timeout=60,
                family=socket.AF_INET,
                rdns=True,
            )
            timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
            async with ClientSession(timeout=timeout, connector=connector) as retry_session:
                async with retry_session.get(
                    request_target,
                    headers=headers,
                    ssl=not disable_ssl,
                ) as retry_resp:
                    if retry_resp.status not in [200, 206]:
                        return None
                    return {
                        "status": retry_resp.status,
                        "headers": dict(retry_resp.headers),
                        "body": await retry_resp.read(),
                        "proxy": retry_proxy,
                    }
        except Exception as e:
            logger.warning("⚠️ cccdn retry via alternate route failed: %r", e)
            return None

    @staticmethod
    def _query_flag_is_true(value: str | None) -> bool:
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _should_force_direct_from_query(self, request) -> bool:
        direct_param = request.query.get("direct")
        if self._query_flag_is_true(direct_param):
            return True

        for param_name, param_value in request.query.items():
            if not param_name.startswith("h_"):
                continue
            header_name = param_name[2:].replace("_", "-").lower()
            if header_name in {"x-direct-connection", "x-force-direct"}:
                return self._query_flag_is_true(param_value)

        return False

    async def get_extractor(self, url: str, request_headers: dict, host: str = None, bypass_warp: bool = False):
        """Ottiene l'estrattore appropriato per l'URL"""
        try:
            # 1. Selezione Manuale tramite parametro 'host'
            if host:
                host = host.lower()
                # ✅ FIX: Usa una chiave di cache che include lo stato del WARP per evitare contaminazioni
                key = f"{host}_direct" if bypass_warp else host
                
                # ✅ FIX: Calcola il proxy corretto in base a bypass_warp invece di usare GLOBAL_PROXIES indiscriminatamente
                proxy_lookup_target = url if host in ["doodstream", "dood", "d000d"] else host
                proxy = get_proxy_for_url(
                    proxy_lookup_target,
                    TRANSPORT_ROUTES,
                    GLOBAL_PROXIES,
                    bypass_warp=bypass_warp,
                )
                proxy_list = [proxy] if proxy else []

                if host == "vavoo":
                    if key not in self.extractors:
                        self.extractors[key] = VavooExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "vixsrc":
                    if key not in self.extractors:
                        self.extractors[key] = VixSrcExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "vixcloud":
                    if key not in self.extractors:
                        self.extractors[key] = VixSrcExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif _is_sportsonline_candidate(host):
                    key = "sportsonline_direct" if bypass_warp else "sportsonline"
                    if key not in self.extractors:
                        self.extractors[key] = SportsonlineExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host in {"mixdrop", "m1xdrop"}:
                    if key not in self.extractors:
                        self.extractors[key] = MixdropExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "voe":
                    if key not in self.extractors:
                        self.extractors[key] = VoeExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "streamtape":
                    if key not in self.extractors:
                        self.extractors[key] = StreamtapeExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "orion":
                    if key not in self.extractors:
                        self.extractors[key] = OrionExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "freeshot":
                    if key not in self.extractors:
                        self.extractors[key] = FreeshotExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                # --- New Extractors (host selection) ---
                elif host in ["doodstream", "dood", "d000d"]:
                    key = "doodstream_direct" if bypass_warp else "doodstream"
                    if key not in self.extractors:
                        self.extractors[key] = DoodStreamExtractor(
                            request_headers,
                            proxies=proxy_list,
                        )
                    return self.extractors[key]
                elif host == "fastream":
                    if key not in self.extractors:
                        self.extractors[key] = FastreamExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "filelions":
                    if key not in self.extractors:
                        self.extractors[key] = FileLionsExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "filemoon":
                    if key not in self.extractors:
                        self.extractors[key] = FileMoonExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "lulustream":
                    if key not in self.extractors:
                        self.extractors[key] = LuluStreamExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "maxstream":
                    if key not in self.extractors:
                        # Maxstream needs multiple candidates because of mirrors
                        proxy_candidates = []
                        for candidate in ("uprot.net", "maxstream.video", "maxstream"):
                            p = get_proxy_for_url(
                                candidate, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                            )
                            if p and p not in proxy_candidates:
                                proxy_candidates.append(p)
                        self.extractors[key] = MaxstreamExtractor(
                            request_headers, proxies=proxy_candidates
                        )
                    return self.extractors[key]
                elif host in ["okru", "ok.ru"]:
                    key = "okru_direct" if bypass_warp else "okru"
                    if key not in self.extractors:
                        self.extractors[key] = OkruExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "streamwish":
                    if key not in self.extractors:
                        self.extractors[key] = StreamWishExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "deltabit":
                    if key not in self.extractors:
                        self.extractors[key] = DeltabitExtractor(
                            request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                        )
                    return self.extractors[key]
                elif host == "streamhg":
                    if key not in self.extractors:
                        self.extractors[key] = StreamHGExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "supervideo":
                    if key not in self.extractors:
                        self.extractors[key] = SupervideoExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "dropload":
                    if key not in self.extractors:
                        self.extractors[key] = DroploadExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "uqload":
                    if key not in self.extractors:
                        self.extractors[key] = UqloadExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "vidmoly":
                    if key not in self.extractors:
                        self.extractors[key] = VidmolyExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host in ["vidoza", "videzz"]:
                    key = "vidoza_direct" if bypass_warp else "vidoza"
                    if key not in self.extractors:
                        self.extractors[key] = VidozaExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host in ["turbovidplay", "turboviplay", "emturbovid"]:
                    key = "turbovidplay_direct" if bypass_warp else "turbovidplay"
                    if key not in self.extractors:
                        self.extractors[key] = TurboVidPlayExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "livetv":
                    if key not in self.extractors:
                        self.extractors[key] = LiveTVExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host == "f16px":
                    if key not in self.extractors:
                        self.extractors[key] = F16PxExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]
                elif host in ["dlhd", "dlstreams"]:
                    key = "dlstreams_direct" if bypass_warp else "dlstreams"
                    if key not in self.extractors:
                        self.extractors[key] = DLStreamsExtractor(
                            request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                        )
                    return self.extractors[key]
                elif host in ["city", "cinemacity"]:
                    key = "cinemacity_direct" if bypass_warp else "cinemacity"
                    if key not in self.extractors:
                        self.extractors[key] = CinemaCityExtractor(
                            request_headers, proxies=proxy_list
                        )
                    return self.extractors[key]

            # 2. Auto-detection basata sull'URL
            if "vavoo.to" in url:
                key = "vavoo_direct" if bypass_warp else "vavoo"
                proxy = get_proxy_for_url("vavoo.to", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VavooExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "vixsrc.to/" in url.lower() and any(
                x in url for x in ["/movie/", "/tv/", "/iframe/", "/embed/", "/playlist/"]
            ):
                key = "vixsrc_direct" if bypass_warp else "vixsrc"
                proxy = get_proxy_for_url("vixsrc.to", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "vixcloud.co/" in url.lower() and any(
                x in url.lower() for x in ["/embed/", "/playlist/"]
            ):
                key = "vixcloud_direct" if bypass_warp else "vixcloud"
                proxy = get_proxy_for_url("vixcloud.co", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif _is_sportsonline_candidate(url):
                key = "sportsonline_direct" if bypass_warp else "sportsonline"
                proxy = _resolve_sportsonline_proxy(url)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = SportsonlineExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif (
                re.search(r"/e/[^/?#]+", url, re.IGNORECASE) is not None
                and any(
                    d in url.lower()
                    for d in [
                        "dhcplay.com/",
                        "vibuxer.com/",
                        "streamhg.com/",
                        "masukestin.com/",
                    ]
                )
            ):
                key = "streamhg_direct" if bypass_warp else "streamhg"
                proxy = get_proxy_for_url("streamhg", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = StreamHGExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "cinemacity.cc" in url.lower():
                key = "cinemacity_direct" if bypass_warp else "cinemacity"
                proxy = get_proxy_for_url("cinemacity.cc", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = CinemaCityExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "mixdrop" in url or "m1xdrop" in url:
                key = "mixdrop_direct" if bypass_warp else "mixdrop"
                proxy = get_proxy_for_url("mixdrop", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = MixdropExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif any(
                d in url
                for d in [
                    "voe.sx",
                    "voe.to",
                    "voe.st",
                    "voe.eu",
                    "voe.la",
                    "voe-network.net",
                ]
            ):
                key = "voe_direct" if bypass_warp else "voe"
                proxy = get_proxy_for_url("voe.sx", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VoeExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "popcdn.day" in url or "freeshot.live" in url:
                key = "freeshot_direct" if bypass_warp else "freeshot"
                proxy = get_proxy_for_url(
                    "popcdn.day" if "popcdn.day" in url else "freeshot.live", 
                    TRANSPORT_ROUTES, 
                    GLOBAL_PROXIES,
                    bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = FreeshotExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif (
                "streamtape.com" in url
                or "streamtape.to" in url
                or "streamtape.net" in url
            ):
                key = "streamtape_direct" if bypass_warp else "streamtape"
                proxy = get_proxy_for_url(
                    "streamtape", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = StreamtapeExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "orionoid.com" in url:
                key = "orion_direct" if bypass_warp else "orion"
                proxy = get_proxy_for_url(
                    "orionoid.com", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = OrionExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            # --- New Extractors (URL auto-detection) ---
            elif any(
                d in url
                for d in [
                    "doodstream",
                    "d000d.com",
                    "dood.wf",
                    "dood.cx",
                    "dood.la",
                    "dood.so",
                    "dood.pm",
                ]
            ):
                key = "doodstream_direct" if bypass_warp else "doodstream"
                proxy = get_proxy_for_url(
                    url, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = DoodStreamExtractor(
                        request_headers,
                        proxies=proxy_list,
                    )
                return self.extractors[key]
            elif "fastream" in url:
                key = "fastream_direct" if bypass_warp else "fastream"
                proxy = get_proxy_for_url("fastream", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = FastreamExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "filelions" in url:
                key = "filelions_direct" if bypass_warp else "filelions"
                proxy = get_proxy_for_url("filelions", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = FileLionsExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "filemoon" in url:
                key = "filemoon_direct" if bypass_warp else "filemoon"
                proxy = get_proxy_for_url("filemoon", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = FileMoonExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif (
                # Rileva per dominio noto (aggiorna qui se cambia)
                any(d in url for d in ["dlhd.dad", "dlstreams.com"])
                # Rileva per pattern URL stabile (/watch.php?id=NNN)
                or (re.search(r'/watch\.php\?.*id=\d+', url) is not None)
            ):
                key = "dlstreams_direct" if bypass_warp else "dlstreams"
                proxy = get_proxy_for_url(
                    "dlhd.dad", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = DLStreamsExtractor(
                        request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                    )
                return self.extractors[key]
            elif "lulustream" in url:
                key = "lulustream_direct" if bypass_warp else "lulustream"
                proxy = get_proxy_for_url(
                    "lulustream", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = LuluStreamExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "maxstream" in url or "uprot.net" in url:
                key = "maxstream_direct" if bypass_warp else "maxstream"
                proxy_list = []
                for candidate in (url, "uprot.net", "maxstream.video", "maxstream"):
                    proxy = get_proxy_for_url(
                        candidate, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                    )
                    if proxy and proxy not in proxy_list:
                        proxy_list.append(proxy)
                if key not in self.extractors:
                    self.extractors[key] = MaxstreamExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "ok.ru" in url or "odnoklassniki" in url:
                key = "okru"
                proxy = get_proxy_for_url("ok.ru", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = OkruExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif any(
                d in url
                for d in ["streamwish", "swish", "wishfast", "embedwish", "wishembed"]
            ):
                key = "streamwish"
                proxy = get_proxy_for_url(
                    "streamwish", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = StreamWishExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "supervideo" in url:
                key = "supervideo"
                proxy = get_proxy_for_url(
                    "supervideo", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = SupervideoExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "dropload" in url:
                key = "dropload"
                proxy = get_proxy_for_url(
                    "dropload", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = DroploadExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "uqload" in url and not any(
                url.endswith(ext) or f"{ext}?" in url
                for ext in (".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".mpd")
            ):
                # Only match embed pages (e.g. uqload.is/abc123.html), not CDN video URLs (m80.uqload.is/.../v.mp4)
                key = "uqload"
                proxy = get_proxy_for_url("uqload", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = UqloadExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "vidmoly" in url:
                key = "vidmoly"
                proxy = get_proxy_for_url("vidmoly", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VidmolyExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "vidoza" in url or "videzz" in url:
                key = "vidoza"
                proxy = get_proxy_for_url("vidoza", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VidozaExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif any(
                d in url
                for d in [
                    "turboviplay",
                    "emturbovid",
                    "tuborstb",
                    "javggvideo",
                    "stbturbo",
                    "turbovidhls",
                ]
            ):
                key = "turbovidplay"
                proxy = get_proxy_for_url(
                    "turbovidplay", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp
                )
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = TurboVidPlayExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif "/e/" in url and any(
                d in url for d in ["f16px", "embedme", "embedsb", "playersb"]
            ):
                key = "f16px"
                proxy = get_proxy_for_url("f16px", TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = F16PxExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            else:
                # ✅ MODIFICATO: Fallback al GenericHLSExtractor per qualsiasi altro URL.
                # Questo permette di gestire estensioni sconosciute o URL senza estensione.
                key = "hls_generic"
                if key not in self.extractors:
                    self.extractors[key] = GenericHLSExtractor(
                        request_headers, proxies=GLOBAL_PROXIES
                    )
                return self.extractors[key]
        except (NameError, TypeError) as e:
            raise ExtractorError(f"Extractor not available - module missing: {e}")

    async def handle_proxy_request(self, request):
        """Gestisce le richieste proxy principali"""
        if not check_password(request):
            logger.warning(
                f"⛔ Access denied: Invalid or missing API Password. IP: {request.remote}"
            )
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        target_url = request.query.get("url") or request.query.get("d")
        bypass_warp = (request.query.get("warp", "").lower() == "off")
        token = BYPASS_WARP_CONTEXT.set(bypass_warp)
        proxy_token = SELECTED_PROXY_CONTEXT.set(None)
        
        try:
            extractor = None
            
            # --- Gestione URL brevi (Shortened URLs) ---
            url_id = request.query.get("hls_url_id")
            if url_id and url_id in self.hls_url_map:
                target_url, stored_at, entry_ttl = self.hls_url_map[url_id]
                if time.time() - stored_at <= entry_ttl:
                    logger.debug(f"🔗 Resolved short URL ID: {url_id}")
                else:
                    self.hls_url_map.pop(url_id, None)
                    target_url = None

            force_refresh = request.query.get("force", "false").lower() == "true"
            redirect_stream = (
                request.query.get("redirect_stream", "true").lower() == "true"
            )

            if not target_url:
                return web.Response(text="Missing 'url' or 'd' parameter", status=400)

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
                    bypass_warp=bypass_warp
                )
                bypass_warp = result.get("bypass_warp", bypass_warp)
                stream_url = result["destination_url"]
                stream_headers = result.get("request_headers", {})
                captured_manifest = result.get("captured_manifest")
                force_disable_ssl = result.get("disable_ssl", False)

                if force_disable_ssl:
                    if "?" in stream_url:
                        stream_url += "&disable_ssl=1"
                    else:
                        stream_url += "?disable_ssl=1"


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
                original_channel_url = request.query.get("url") or request.query.get("d", "")
                api_password = request.query.get("api_password")
                no_bypass = request.query.get("no_bypass") == "1"
                use_short_hls_urls = (
                    "cinemacity.cc" in (original_channel_url or "").lower()
                    or request.query.get("host", "").lower() in {"city", "cinemacity"}
                )
                disable_ssl = request.query.get("disable_ssl") == "1" or force_disable_ssl
                rewritten_manifest = await ManifestRewriter.rewrite_manifest_urls(
                    manifest_content=captured_manifest,
                    base_url=stream_url,
                    proxy_base=proxy_base,
                    stream_headers=stream_headers,
                    original_channel_url=original_channel_url,
                    api_password=api_password,
                    get_extractor_func=lambda url, headers, host=None: self.get_extractor(url, headers, host, bypass_warp=bypass_warp),
                    no_bypass=no_bypass,
                    shorten_url_func=self.shorten_hls_url if use_short_hls_urls else None,
                    bypass_warp=bypass_warp,
                    disable_ssl=disable_ssl,
                )
                return web.Response(
                    text=rewritten_manifest,
                    headers={
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Content-Disposition": 'attachment; filename="stream.m3u8"',
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
            is_mpd = ".mpd" in stream_url.lower() or "/dash/" in stream_url.lower()
            if is_mpd:
                if MPD_MODE == "ffmpeg" and self.ffmpeg_manager:
                    # FFmpeg transcoding mode
                    logger.info(
                        f"🔄 [FFmpeg Mode] Routing MPD stream: {stream_url}"
                    )

                    # Extract ClearKey if present
                    clearkey_param = request.query.get("clearkey")

                    # Support separate key_id and key params (handling multiple keys)
                    if not clearkey_param:
                        key_id_param = request.query.get("key_id")
                        key_val_param = request.query.get("key")

                        if key_id_param and key_val_param:
                            # Check for multiple keys
                            key_ids = key_id_param.split(",")
                            key_vals = key_val_param.split(",")

                            if len(key_ids) == len(key_vals):
                                clearkey_parts = []
                                for kid, kval in zip(key_ids, key_vals):
                                    clearkey_parts.append(
                                        f"{kid.strip()}:{kval.strip()}"
                                    )
                                clearkey_param = ",".join(clearkey_parts)
                            else:
                                # Fallback or error? defaulting to first or simple concat if mismatch
                                # Let's try to handle single mismatch case gracefully or just use as is
                                if len(key_ids) == 1 and len(key_vals) == 1:
                                    clearkey_param = (
                                        f"{key_id_param}:{key_val_param}"
                                    )
                                else:
                                    logger.warning(
                                        f"Mismatch in key_id/key count: {len(key_ids)} vs {len(key_vals)}"
                                    )
                                    # Try to pair as many as possible
                                    min_len = min(len(key_ids), len(key_vals))
                                    clearkey_parts = []
                                    for i in range(min_len):
                                        clearkey_parts.append(
                                            f"{key_ids[i].strip()}:{key_vals[i].strip()}"
                                        )
                                    clearkey_param = ",".join(clearkey_parts)

                        elif key_val_param:
                            clearkey_param = key_val_param

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
                    disable_ssl = get_ssl_setting_for_url(
                        stream_url, TRANSPORT_ROUTES
                    )
                    if disable_ssl:
                        ssl_context = False

                    # Use helper to get proxy-enabled session
                    mpd_session, mpd_proxy = await self._get_proxy_session(
                        stream_url, bypass_warp=bypass_warp
                    )
                    if mpd_proxy:
                        logger.info(
                            f"📡 [MPD] Using session via proxy: {mpd_proxy}"
                        )
                    final_mpd_url = stream_url  # Will be updated if redirected

                    try:
                        async with mpd_session.get(
                            stream_url,
                            headers=stream_headers,
                            ssl=ssl_context,
                            allow_redirects=True,
                        ) as resp:
                            # Capture final URL after redirects (use for segment URL construction)
                            final_mpd_url = str(resp.url)
                            if final_mpd_url != stream_url:
                                logger.info(f"↪️ MPD redirected to: {final_mpd_url}")

                            if resp.status != 200:
                                error_text = await resp.text()
                                logger.error(
                                    f"❌ Failed to fetch MPD. Status: {resp.status}, URL: {stream_url}"
                                )
                                logger.error(f"   Headers: {stream_headers}")
                                logger.error(
                                    f"   Response: {error_text[:500]}"
                                )  # Truncate for safety
                                return web.Response(
                                    text=f"Failed to fetch MPD: {resp.status}\nResponse: {error_text[:1000]}",
                                    status=502,
                                )
                            manifest_content = await resp.text()
                    finally:
                        # Session is pooled/cached, so we don't close it
                        pass

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

                    # Get ClearKey param
                    clearkey_param = request.query.get("clearkey")
                    if not clearkey_param:
                        key_id_param = request.query.get("key_id")
                        key_val_param = request.query.get("key")

                        if key_id_param and key_val_param:
                            # Check for multiple keys
                            key_ids = key_id_param.split(",")
                            key_vals = key_val_param.split(",")

                            if len(key_ids) == len(key_vals):
                                clearkey_parts = []
                                for kid, kval in zip(key_ids, key_vals):
                                    clearkey_parts.append(
                                        f"{kid.strip()}:{kval.strip()}"
                                    )
                                clearkey_param = ",".join(clearkey_parts)
                            else:
                                if len(key_ids) == 1 and len(key_vals) == 1:
                                    clearkey_param = (
                                        f"{key_id_param}:{key_val_param}"
                                    )
                                else:
                                    logger.warning(
                                        f"Mismatch in key_id/key count: {len(key_ids)} vs {len(key_vals)}"
                                    )
                                    # Try to pair as many as possible
                                    min_len = min(len(key_ids), len(key_vals))
                                    clearkey_parts = []
                                    for i in range(min_len):
                                        clearkey_parts.append(
                                            f"{key_ids[i].strip()}:{key_vals[i].strip()}"
                                        )
                                    clearkey_param = ",".join(clearkey_parts)
                        elif key_val_param:
                            clearkey_param = key_val_param

                    if clearkey_param:
                        params += f"&clearkey={clearkey_param}"

                    # Pass 'ext' param if present (e.g. ext=ts)
                    ext_param = request.query.get("ext")
                    if ext_param:
                        params += f"&ext={ext_param}"

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

            # Procedi con il proxy dello stream (passando l'eventuale bypass_warp attivato dall'estrattore)
            return await self._proxy_stream(request, stream_url, stream_headers, bypass_warp=bypass_warp)

        except Exception as e:
            # ✅ MIGLIORATO: Distingui tra errori temporanei (sito offline) ed errori critici
            error_msg = str(e).lower()
            is_expired_embed = (
                "expired vixsrc embed url" in error_msg
                or ("vixsrc" in error_msg and "expired" in error_msg and "embed" in error_msg)
            )
            is_not_found = "404" in error_msg or "not found" in error_msg
            is_temporary_error = any(
                x in error_msg
                for x in [
                    "403",
                    "forbidden",
                    "502",
                    "bad gateway",
                    "timeout",
                    "connection",
                    "temporarily unavailable",
                ]
            )

            extractor_name = "unknown"
            if VavooExtractor and isinstance(extractor, VavooExtractor):
                extractor_name = "VavooExtractor"
            elif extractor is not None:
                extractor_name = type(extractor).__name__

            if is_expired_embed:
                logger.info("Expired VixSrc embed URL rejected: %s", str(e))
                return web.Response(text=str(e), status=410)

            if is_not_found:
                logger.warning(f"🔍 {extractor_name}: Content not found (404). File missing or possible IP block. (Try opening the link in a browser to verify) - {str(e)}")
                return web.Response(text=f"Content not found: {str(e)}", status=404)

            # Gestione errori di connessione o blocchi
            if is_temporary_error:
                if "403" in error_msg or "forbidden" in error_msg:
                    logger.error(f"🚫 {extractor_name}: Access denied (403 Forbidden). Possible IP block or WAF protection. - {str(e)}")
                else:
                    logger.warning(f"📡 {extractor_name}: Connection failed (Timeout/Connection Error). Site might be down or IP is blocked. - {str(e)}")
                
                return web.Response(
                    text=f"Service temporarily unavailable: {str(e)}", status=503
                )

            # Per errori veri (non temporanei), logga come CRITICAL con traceback completo
            logger.critical(f"❌ Critical error with {extractor_name}: {e}")
            logger.exception(f"Error in proxy request: {str(e)}")
            return web.Response(text=f"Proxy error: {str(e)}", status=500)
        finally:
            BYPASS_WARP_CONTEXT.reset(token)
            SELECTED_PROXY_CONTEXT.reset(proxy_token)

    async def handle_extractor_request(self, request):
        """
        Endpoint compatibile con MediaFlow-Proxy per ottenere informazioni sullo stream.
        Supporta redirect_stream per ridirezionare direttamente al proxy.
        """
        # Log request details for debugging
        logger.debug(f"📥 Extractor Request: {request.url}")

        if not check_password(request):
            logger.warning("⛔ Unauthorized extractor request")
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        bypass_warp = request.query.get("warp", "").lower() == "off"
        token = BYPASS_WARP_CONTEXT.set(bypass_warp)
        proxy_token = SELECTED_PROXY_CONTEXT.set(None)
        
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
                        "maxstream",
                        "okru",
                        "streamwish",
                        "streamhg",
                        "supervideo",
                        "dropload",
                        "uqload",
                        "vidmoly",
                        "vidoza",
                        "turbovidplay",
                         "livetv",
                         "deltabit",
                         "f16px",
                    ],
                    "examples": [
                        f"{request.scheme}://{request.host}/extractor/video?d=https://vavoo.to/channel/123",
                        f"{request.scheme}://{request.host}/extractor/video.m3u8?host=vavoo&d=https://custom-link.com",
                        f"{request.scheme}://{request.host}/extractor/video.mp4?host=mixdrop&d=https://mixdrop.co/e/ABC123XYZ",
                        f"{request.scheme}://{request.host}/extractor/video?d=BASE64_STRING",
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

            bypass_warp = request.query.get("warp", "").lower() == "off"
            logger.debug(f"Extractor Debug: Initial bypass_warp from query: {bypass_warp}")
            
            extractor = await self.get_extractor(
                url, dict(request.headers), host=host_param, bypass_warp=bypass_warp
            )
            result = await extractor.extract(url, **extractor_kwargs)

            stream_url = result["destination_url"]
            stream_headers = result.get("request_headers", {})
            mediaflow_endpoint = result.get("mediaflow_endpoint", "hls_proxy")
            force_disable_ssl = result.get("disable_ssl", False)
            selected_proxy = result.get("selected_proxy")
            bypass_warp = result.get("bypass_warp", bypass_warp)
            
            # Log dello stato dell'estrattore
            logger.debug(f"Extractor Debug: Extractor result bypass_warp: {result.get('bypass_warp')}")
            
            # Non forziamo più l'override qui, lasciamo che sia la scelta iniziale a comandare
            # bypass_warp = bypass_warp (rimane quello definito all'inizio a riga 1902)
            
            logger.debug(f"Extractor Debug: Final bypass_warp for redirect: {bypass_warp}")
            
            logger.info(
                f"✅ Extraction success: {stream_url[:50]}... Endpoint: {mediaflow_endpoint}"
            )

            # Costruisci l'URL del proxy per questo stream
            scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
            host = request.headers.get("X-Forwarded-Host", request.host)
            proxy_base = f"{scheme}://{host}"

            # Determina l'endpoint corretto
            endpoint = "/proxy/hls/manifest.m3u8"
            
            # Check extension of the actual path, not the whole URL
            path_lower = urllib.parse.urlparse(stream_url).path.lower()
            is_direct_video = any(path_lower.endswith(ext) for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"])
            
            if mediaflow_endpoint == "proxy_stream_endpoint" or is_direct_video:
                endpoint = "/proxy/stream"
            elif ".mpd" in path_lower or "manifest" in path_lower and "dash" in path_lower:
                endpoint = "/proxy/mpd/manifest.m3u8"

            encoded_url = urllib.parse.quote(stream_url, safe="")
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
            if selected_proxy:
                header_params += f"&proxy={urllib.parse.quote(selected_proxy)}"

            # 1. URL COMPLETO (Solo per il redirect)
            full_proxy_url = f"{proxy_base}{endpoint}?d={encoded_url}{header_params}"

            if redirect_stream:
                logger.debug(f"↪️ Redirecting to: {full_proxy_url}")
                return web.HTTPFound(full_proxy_url)

            # 2. URL PULITO (Per il JSON stile MediaFlow)
            q_params = {}
            if api_password:
                q_params["api_password"] = api_password

            response_data = {
                "destination_url": stream_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": mediaflow_endpoint,
                "mediaflow_proxy_url": f"{proxy_base}{endpoint}",
                "query_params": q_params,
            }

            logger.info(f"✅ Extractor OK: {url} -> {stream_url[:50]}...")
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
                ]
            )

            if is_expected_error:
                logger.warning(f"⚠️ Extractor request failed (expected error): {e}")
            else:
                logger.error(f"❌ Error in extractor request: {e}")
                import traceback

                traceback.print_exc()

            return web.Response(text=str(e), status=500)
        finally:
            BYPASS_WARP_CONTEXT.reset(token)
            SELECTED_PROXY_CONTEXT.reset(proxy_token)

    async def handle_license_request(self, request):
        """✅ NUOVO: Gestisce le richieste di licenza DRM (ClearKey e Proxy)"""
        try:
            # 1. Modalità ClearKey Statica
            clearkey_param = request.query.get("clearkey")
            if clearkey_param:
                logger.debug(f"🔑 Static ClearKey license request: {clearkey_param}")
                try:
                    # Support multiple keys separated by comma
                    # Format: KID1:KEY1,KID2:KEY2
                    key_pairs = clearkey_param.split(",")
                    keys_jwk = []

                    # Helper per convertire hex in base64url
                    def hex_to_b64url(hex_str):
                        return (
                            base64.urlsafe_b64encode(binascii.unhexlify(hex_str))
                            .decode("utf-8")
                            .rstrip("=")
                        )

                    for pair in key_pairs:
                        if ":" in pair:
                            kid_hex, key_hex = pair.split(":")
                            keys_jwk.append(
                                {
                                    "kty": "oct",
                                    "k": hex_to_b64url(key_hex),
                                    "kid": hex_to_b64url(kid_hex),
                                    "type": "temporary",
                                }
                            )

                    if not keys_jwk:
                        raise ValueError("No valid keys found")

                    jwk_response = {"keys": keys_jwk, "type": "temporary"}

                    logger.info(
                        f"🔑 Serving static ClearKey license with {len(keys_jwk)} keys"
                    )
                    return web.json_response(jwk_response)
                except Exception as e:
                    logger.error(f"❌ Error generating static ClearKey license: {e}")
                    return web.Response(text="Invalid ClearKey format", status=400)

            # 2. Modalità Proxy Licenza
            license_url = request.query.get("url")
            if not license_url:
                return web.Response(text="Missing url parameter", status=400)

            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            # Ricostruisce gli headers
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    headers[header_name] = param_value

            # Aggiunge headers specifici della richiesta originale (es. content-type per il body)
            if request.headers.get("Content-Type"):
                headers["Content-Type"] = request.headers.get("Content-Type")

            # Legge il body della richiesta (challenge DRM)
            body = await request.read()

            logger.info(f"🔐 Proxying License Request to: {license_url}")

            # ✅ Use pooled session for better performance
            bypass_warp = request.query.get("warp", "").lower() == "off"
            session, _ = await self._get_proxy_session(
                license_url, bypass_warp=bypass_warp
            )
            async with session.request(
                request.method, license_url, headers=headers, data=body
            ) as resp:
                response_body = await resp.read()
                logger.info(
                    f"✅ License response: {resp.status} ({len(response_body)} bytes)"
                )

                response_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                }
                # Copia alcuni headers utili dalla risposta originale
                if "Content-Type" in resp.headers:
                    response_headers["Content-Type"] = resp.headers["Content-Type"]

                return web.Response(
                    body=response_body, status=resp.status, headers=response_headers
                )

        except Exception as e:
            logger.error(f"❌ License proxy error: {str(e)}")
            return web.Response(text=f"License error: {str(e)}", status=500)

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

        try:
            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            original_channel_url = request.query.get("original_channel_url")
            
            # Detect DLStreams keys by multiple signals:
            # 1. original_channel_url contains known domains
            # 2. key_url matches /key/premium pattern (CDN rotates domains)
            # 3. original_channel_url contains the mono.css manifest pattern
            is_dlstreams_key = False
            if original_channel_url and any(
                marker in original_channel_url for marker in ["dlhd.dad", "dlstreams.top", "dlstreams.com"]
            ):
                is_dlstreams_key = True
            elif re.search(r"/key/premium\d+/", key_url):
                is_dlstreams_key = True
            elif original_channel_url and re.search(r"/proxy/.+/premium\d+/mono\.css", original_channel_url):
                is_dlstreams_key = True

            if is_dlstreams_key:
                # First check if the DLStreams extractor already has this key cached
                dlstreams_extractor = self.extractors.get("dlstreams")
                if dlstreams_extractor and hasattr(dlstreams_extractor, "_browser_key_cache"):
                    cached_key = dlstreams_extractor._browser_key_cache.get(key_url)
                    if cached_key:
                        logger.info("✅ AES key served from DLStreams browser cache (%d bytes)", len(cached_key))
                        return web.Response(
                            body=cached_key,
                            content_type="application/octet-stream",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Headers": "*",
                                "Cache-Control": "no-cache, no-store, must-revalidate",
                            },
                        )

                # Fallback: try browser-based key fetch
                try:
                    if dlstreams_extractor and hasattr(dlstreams_extractor, "fetch_key_via_browser"):
                        # Use original_channel_url or reconstruct from key_url
                        fetch_url = original_channel_url or key_url
                        browser_key = await dlstreams_extractor.fetch_key_via_browser(
                            key_url, fetch_url
                        )
                        if browser_key:
                            logger.info("✅ AES key fetched via browser context (%d bytes)", len(browser_key))
                            return web.Response(
                                body=browser_key,
                                content_type="application/octet-stream",
                                headers={
                                    "Access-Control-Allow-Origin": "*",
                                    "Access-Control-Allow-Headers": "*",
                                    "Cache-Control": "no-cache, no-store, must-revalidate",
                                },
                            )
                    elif original_channel_url:
                        # Try to get extractor via original URL
                        extractor = await self.get_extractor(original_channel_url, {})
                        if hasattr(extractor, "fetch_key_via_browser"):
                            browser_key = await extractor.fetch_key_via_browser(
                                key_url, original_channel_url
                            )
                            if browser_key:
                                logger.info("✅ AES key fetched via browser context (%d bytes)", len(browser_key))
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
                        f"⚠️ Browser-backed key fetch failed, falling back to direct request: {browser_key_exc}"
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

            logger.debug(f"🔑 Fetching AES key from: {key_url}")
            logger.debug(f"   -> with headers: {headers}")

            # ✅ Use pooled session for better performance
            # The session already has the proxy configured in its connector
            if self._should_force_direct_from_query(request):
                session = await self._get_session(url=key_url)
                proxy_used = None
                logger.debug("Using direct session for AES key request (forced)")
            else:
                session, proxy_used = await self._get_proxy_session(
                    key_url, bypass_warp=bypass_warp
                )
                if proxy_used:
                    logger.debug(f"Using pooled session with proxy: {proxy_used}")
            secret_key = headers.pop("X-Secret-Key", None)

            # Calcola X-Key-Timestamp, X-Key-Nonce, X-Fingerprint, e X-Key-Path se abbiamo la secret_key
            if secret_key and "/key/" in key_url:
                # Get user agent from X-User-Agent header or fall back to User-Agent
                user_agent = (
                    headers.get("X-User-Agent")
                    or headers.get("User-Agent")
                    or headers.get("user-agent")
                )
                nonce_result = self._compute_key_headers(
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

            disable_ssl = get_ssl_setting_for_url(key_url, TRANSPORT_ROUTES)
            async with session.get(key_url, headers=headers, ssl=not disable_ssl) as resp:
                if resp.status == 200 or resp.status == 206:
                    key_data = await resp.read()
                    logger.debug(
                        f"✅ AES key fetched successfully: {len(key_data)} bytes"
                    )

                    # Warn if key size is unexpected (AES-128 = 16 bytes)
                    if len(key_data) != 16 and is_dlstreams_key:
                        logger.warning(
                            f"⚠️ DLStreams AES key response is {len(key_data)} bytes (expected 16). "
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
                    logger.error(f"❌ Key fetch failed with status: {resp.status}")
                    # --- LOGICA DI INVALIDAZIONE AUTOMATICA ---
                    try:
                        url_param = request.query.get("original_channel_url")
                        if url_param:
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
            logger.error(f"❌ Error fetching AES key: {str(e)}")
            return web.Response(text=f"Key error: {str(e)}", status=500)

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

    async def _proxy_segment(self, request, segment_url, stream_headers, segment_name):
        """✅ NUOVO: Proxy dedicato per segmenti .ts con Content-Disposition"""
        try:
            # Ping DLStreams extractor to keep browser alive during playback
            # Use robust markers: Daddy's domains, 'premium' pattern, 'mono.css', or Referer/Origin headers
            is_dlstreams = any(m in segment_url for m in ["dlhd.dad", "dlstreams", "premium", "mono.css"])
            if not is_dlstreams:
                ref = request.query.get("h_Referer", "") or request.headers.get("Referer", "")
                origin = request.query.get("h_Origin", "") or request.headers.get("Origin", "")
                is_dlstreams = any(m in (ref + origin).lower() for m in ["dlhd.dad", "dlstreams"])
            
            if is_dlstreams:
                ext = self.extractors.get("dlstreams")
                if ext and hasattr(ext, "_update_shared_activity"):
                    ext._update_shared_activity()

            headers = dict(stream_headers)
            is_cccdn_stream = "cccdn.net" in segment_url

            def set_response_header(target: dict, name: str, value: str):
                keys_to_remove = [k for k in target.keys() if k.lower() == name.lower()]
                for key in keys_to_remove:
                    del target[key]
                target[name] = value

            # Passa attraverso alcuni headers del client
            for header in ["range", "if-none-match", "if-modified-since"]:
                if header in request.headers:
                    headers[header] = request.headers[header]

            if is_cccdn_stream:
                headers["Accept-Encoding"] = "identity"

            # ✅ Use pooled session for better performance
            bypass_warp = request.query.get("warp", "").lower() == "off"
            session, _ = await self._get_proxy_session(
                segment_url, bypass_warp=bypass_warp
            )
            disable_ssl = get_ssl_setting_for_url(segment_url, TRANSPORT_ROUTES)
            # ✅ Use yarl.URL with encoded=True to prevent double-encoding of commas
            final_segment_url = yarl.URL(segment_url, encoded=True)
            async with session.get(final_segment_url, headers=headers, ssl=not disable_ssl) as resp:
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
                set_response_header(response_headers, "Content-Type", "video/MP2T")
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
                        await response.write(chunk)
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

        except Exception as e:
            logger.error(f"Error in segment proxy: {str(e)}")
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_stream(self, request, stream_url, stream_headers, bypass_warp=None):
        """Effettua il proxy dello stream con gestione manifest e AES-128"""
        if bypass_warp is None:
            bypass_warp = request.query.get("warp", "").lower() == "off"
        forced_proxy = request.query.get("proxy") or None
        try:
            # Ping DLStreams extractor to keep browser alive during playback
            # Use robust markers: Daddy's domains, 'premium' pattern, 'mono.css', or Referer/Origin headers
            is_dlstreams = any(m in stream_url for m in ["dlhd.dad", "dlstreams", "premium", "mono.css"])
            if not is_dlstreams:
                ref = request.query.get("h_Referer", "") or request.headers.get("Referer", "")
                origin = request.query.get("h_Origin", "") or request.headers.get("Origin", "")
                is_dlstreams = any(m in (ref + origin).lower() for m in ["dlhd.dad", "dlstreams"])

            if is_dlstreams:
                ext = self.extractors.get("dlstreams")
                if ext and hasattr(ext, "_update_shared_activity"):
                    ext._update_shared_activity()

            headers = dict(stream_headers)

            def set_response_header(target: dict, name: str, value: str):
                keys_to_remove = [k for k in target.keys() if k.lower() == name.lower()]
                for key in keys_to_remove:
                    del target[key]
                target[name] = value

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

            # ✅ NUOVO: Determina se disabilitare SSL per questo dominio
            disable_ssl = (
                request.query.get("h_X-EasyProxy-Disable-SSL") == "1"
                or request.query.get("disable_ssl") == "1"
                or headers.get("X-EasyProxy-Disable-SSL") == "1"
                or get_ssl_setting_for_url(stream_url, TRANSPORT_ROUTES)
            )
            headers.pop("X-EasyProxy-Disable-SSL", None)
            headers.pop("x-easyproxy-disable-ssl", None)
            is_cccdn_stream = "cccdn.net" in stream_url

            if is_cccdn_stream:
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
            if self._should_force_direct_from_query(request):
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
                    routing = f"WARP (Cloudflare IP)" if (WARP_PROXY_URL and session_proxy == WARP_PROXY_URL) else f"PROXY ({session_proxy})"
                else:
                    routing = "BYPASS (Real IP)"
                
                logger.info(
                    f"📡 [Proxy Stream] {routing} - Using session (direct) for: {stream_url}"
                )

            # --- PROTECTED DOMAINS FALLBACK: curl_cffi ---
            use_curl_cffi = HAS_CURL_CFFI and (not is_cccdn_stream) and any(d in stream_url for d in ["cinemacity.cc", "torrentio", "strem.fun"])
            
            if use_curl_cffi and any(d in stream_url for d in ["torrentio", "strem.fun"]):
                # Only use curl_cffi for Torrentio if it's a manifest or explicitly requested
                is_manifest_req = any(ext in stream_url.lower() for ext in [".m3u8", ".mpd", "manifest"])
                if not is_manifest_req:
                    use_curl_cffi = False

            if use_curl_cffi:
                logger.info(f"🚀 [curl_cffi] Using browser impersonation for: {stream_url}")
                try:
                    # Use a pooled curl session if available
                    session_key = f"curl_{session_proxy or 'direct'}"
                    if session_key not in self.curl_sessions or self.curl_sessions[session_key] is None:
                        self.curl_sessions[session_key] = CurlAsyncSession(impersonate="chrome124")
                    
                    curl_s = self.curl_sessions[session_key]
                    curl_headers = dict(headers)
                    
                    # ✅ FIX: Remove User-Agent from headers to let curl_cffi use the one matching the fingerprint
                    # A mismatch between the TLS fingerprint and the User-Agent header often causes 403 Forbidden.
                    if "User-Agent" in curl_headers:
                        del curl_headers["User-Agent"]
                    if "user-agent" in curl_headers:
                        del curl_headers["user-agent"]
                    
                    # Preserve extractor-provided Referer for cccdn.net.
                    # Some streams require the exact movie page, not the site root.
                    if "cccdn.net" in stream_url:
                        referer_value = (
                            curl_headers.get("Referer")
                            or curl_headers.get("referer")
                            or "https://cinemacity.cc/"
                        )
                        curl_headers["Referer"] = referer_value
                        try:
                            parsed_referer = urllib.parse.urlparse(referer_value)
                            if parsed_referer.scheme and parsed_referer.netloc:
                                curl_headers["Origin"] = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
                            else:
                                curl_headers["Origin"] = "https://cinemacity.cc"
                        except Exception:
                            curl_headers["Origin"] = "https://cinemacity.cc"
                        curl_headers["Sec-Fetch-Site"] = "same-site"
                        curl_headers["Sec-Fetch-Mode"] = "cors"
                        curl_headers["Sec-Fetch-Dest"] = "empty"
                        if "Accept-Language" not in curl_headers:
                            curl_headers["Accept-Language"] = "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"
                    elif "Referer" not in curl_headers and "referer" not in curl_headers:
                        # Fallback for others if missing
                        pass 
                    
                    # Ensure Accept is broad
                    if "Accept" not in curl_headers:
                        curl_headers["Accept"] = "*/*"

                    curl_proxies = None
                    # ✅ DEBUG: Log final headers for comparison
                    logger.debug(f"🚀 [curl_cffi] Sending headers for {stream_url[:50]}: {curl_headers}")

                    curl_proxies = None
                    if session_proxy:
                        curl_proxies = {"http": session_proxy, "https": session_proxy}
                    
                    # ✅ CRITICAL FIX: Ensure commas are NOT encoded. 
                    # cccdn.net multi-path URLs MUST have literal commas.
                    final_curl_url = stream_url
                    if "cccdn.net" in final_curl_url:
                        final_curl_url = urllib.parse.unquote(final_curl_url)

                    # ✅ NUOVO: Se è un manifest, proviamo a usare smart_request come fallback
                    # se curl_cffi diretto dovesse dare ancora 403.
                    is_manifest = ".m3u8" in final_curl_url.lower() or ".mpd" in final_curl_url.lower()
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
                        async def read(self): return await self.c_resp.acontent()

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
                        async def close(self): pass # Session is pooled
                        async def __aenter__(self): return self
                        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

                    # Se curl_cffi fallisce con 403 su un manifest, proviamo FlareSolverr via smart_request
                    if curl_resp.status_code in [502, 503, 504]:
                        logger.warning(f"⚠️ [curl_cffi] {curl_resp.status_code} error for {final_curl_url[:50]}, falling back to standard aiohttp...")
                        goto_manifest_processing = False
                    elif curl_resp.status_code == 403 and is_manifest:
                        logger.warning(f"⚠️ [curl_cffi] 403 on manifest, trying smart_request fallback for {final_curl_url[:50]}...")
                        from utils.smart_request import smart_request
                        sr_result = await smart_request("request.get", final_curl_url, headers=curl_headers)
                        if sr_result.get("html"):
                            logger.info("✅ [smart_request] Fallback success for manifest content")
                            # Mock a response object that looks like what the rest of the code expects
                            class MockSRResp:
                                def __init__(self, content):
                                    self.status = 200
                                    self.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
                                    self.url = yarl.URL(final_curl_url)
                                    self._content = content.encode('utf-8')
                                async def read(self): return self._content
                                async def text(self, **kwargs): return self._content.decode('utf-8')
                                async def close(self): pass
                                async def __aenter__(self): return self
                                async def __aexit__(self, *args): pass
                            
                            resp_ctx = MockSRResp(sr_result["html"])
                            goto_manifest_processing = True
                        else:
                            # Fallback failed too, use original curl_resp
                            resp_ctx = MockResp(curl_resp)
                            goto_manifest_processing = True
                    else:
                        resp_ctx = MockResp(curl_resp)
                        goto_manifest_processing = True
                except Exception as e:
                    logger.error(f"❌ [curl_cffi] Error: {e}")
                    goto_manifest_processing = False
            else:
                goto_manifest_processing = False

            if not goto_manifest_processing:
                if is_cccdn_stream:
                    request_target = urllib.parse.unquote(stream_url)
                else:
                    request_target = yarl.URL(stream_url, encoded=True)
                resp_ctx = session.get(request_target, headers=headers, ssl=not disable_ssl)

            async with resp_ctx as resp:
                content_type = resp.headers.get("content-type", "").lower()

                if resp.status not in [200, 206]:
                    if is_cccdn_stream and resp.status == 403 and not goto_manifest_processing:
                        retry_result = await self._retry_cccdn_request(
                            request_target,
                            headers,
                            disable_ssl,
                        )
                        if retry_result:
                            retry_headers = dict(retry_result["headers"])
                            retry_headers["Access-Control-Allow-Origin"] = "*"
                            logger.info(
                                "✅ cccdn retry success via alternate route: %s",
                                retry_result["proxy"],
                            )
                            return web.Response(
                                body=retry_result["body"],
                                status=retry_result["status"],
                                headers=retry_headers,
                            )
                    error_body = await resp.read()
                    routing = "WARP" if (session_proxy and WARP_PROXY_URL and session_proxy == WARP_PROXY_URL) else ("BYPASS" if session_proxy is None else "PROXY")
                    logger.warning(f"⚠️ Upstream returned error {resp.status} for {stream_url} [Routing: {routing}]")
                    return web.Response(body=error_body, status=resp.status, headers={"Content-Type": content_type, "Access-Control-Allow-Origin": "*"})

                is_direct_media_stream = request.path == "/proxy/stream" and (
                    "video/" in content_type or stream_url.lower().endswith((".mp4", ".mkv", ".avi", ".mov"))
                )

                if is_direct_media_stream:
                    response_headers = {
                        "Content-Type": content_type,
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": "Range, Content-Type",
                    }
                    for h in ["content-length", "content-range", "accept-ranges"]:
                        if h in resp.headers: response_headers[h] = resp.headers[h]

                    response = web.StreamResponse(status=resp.status, headers=response_headers)
                    await response.prepare(request)
                    try:
                        async for chunk in resp.content.iter_any():
                            await response.write(chunk)
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

                content_bytes = await resp.read()
                manifest_content = None
                try:
                    decoded_text = content_bytes.decode("utf-8", errors='replace')
                    if decoded_text.lstrip().startswith("#EXTM3U"):
                        manifest_content = decoded_text
                except: pass

                if manifest_content is None and (".m3u8" in stream_url or "mpegurl" in content_type):
                    try: manifest_content = content_bytes.decode("utf-8", errors='replace')
                    except: pass

                if manifest_content:
                    logger.info(f"📄 HLS manifest detected: {stream_url}")
                    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
                    host = request.headers.get("X-Forwarded-Host", request.host)
                    proxy_base = f"{scheme}://{host}"
                    original_url = request.query.get("url") or request.query.get("d", "")
                    use_short_hls_urls = (
                        "cinemacity.cc" in (original_url or "").lower()
                        or request.query.get("host", "").lower() in {"city", "cinemacity"}
                        or "cccdn.net" in str(resp.url).lower()
                    )
                    
                    disable_ssl = request.query.get("disable_ssl") == "1" or get_ssl_setting_for_url(str(resp.url), TRANSPORT_ROUTES)
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
                        disable_ssl=disable_ssl,
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
                    clearkey_param = request.query.get("clearkey")

                    # ✅ FIX: Supporto per key_id e key separati (stile MediaFlowProxy)
                    if not clearkey_param:
                        key_id_param = request.query.get("key_id")
                        key_val_param = request.query.get("key")

                        if key_id_param and key_val_param:
                            # Check for multiple keys
                            key_ids = key_id_param.split(",")
                            key_vals = key_val_param.split(",")

                            if len(key_ids) == len(key_vals):
                                clearkey_parts = []
                                for kid, kval in zip(key_ids, key_vals):
                                    clearkey_parts.append(
                                        f"{kid.strip()}:{kval.strip()}"
                                    )
                                clearkey_param = ",".join(clearkey_parts)
                            else:
                                if len(key_ids) == 1 and len(key_vals) == 1:
                                    clearkey_param = f"{key_id_param}:{key_val_param}"
                                else:
                                    # Try to pair as many as possible
                                    min_len = min(len(key_ids), len(key_vals))
                                    clearkey_parts = []
                                    for i in range(min_len):
                                        clearkey_parts.append(
                                            f"{key_ids[i].strip()}:{key_vals[i].strip()}"
                                        )
                                    clearkey_param = ",".join(clearkey_parts)

                    # --- LEGACY MODE: MPD -> HLS Conversion ---
                    if MPD_MODE in ("legacy", "none", "disabled") and MPDToHLSConverter:
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
                    if header in resp.headers:
                        response_headers[header] = resp.headers[header]

                # ✅ FIX: Forza Content-Type coerente se il server non lo invia correttamente
                if (
                    stream_url.endswith(".ts") or request.path.endswith(".ts")
                ) and "video/mp2t" not in response_headers.get(
                    "content-type", ""
                ).lower():
                    set_response_header(response_headers, "Content-Type", "video/MP2T")
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
                    status=resp.status,
                    headers=response_headers,
                )


        except (ClientPayloadError, ConnectionResetError, OSError) as e:
            # Errori tipici di disconnessione del client
            logger.info(f"ℹ️ Client disconnected from stream: {stream_url} ({str(e)})")
            return web.Response(text="Client disconnected", status=499)

        except (
            ServerDisconnectedError,
            ClientConnectionError,
            asyncio.TimeoutError,
        ) as e:
            # Errori di connessione upstream
            logger.warning(f"⚠️ Connection lost with source: {stream_url} ({str(e)})")
            return web.Response(text=f"Upstream connection lost: {str(e)}", status=502)

        except Exception as e:
            err_msg = str(e)
            if "Connection lost" in err_msg or "Connection reset" in err_msg:
                logger.info(f"ℹ️ Stream connection closed by client or server: {stream_url}")
                return web.Response(text="Connection lost", status=499)
            
            logger.error(
                "❌ Generic error in stream proxy [%s]: %r",
                type(e).__name__,
                e,
            )
            return web.Response(text=f"Stream error: {err_msg}", status=500)

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
        """Funzione helper per leggere un file di template."""
        # Nota: assume che i template siano nella directory 'templates' nella root del progetto
        # Poiché siamo in services/, dobbiamo salire di un livello
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template_path = os.path.join(base_dir, "templates", filename)
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()

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
            "proxy": "HLS Proxy Server",
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
                "global_proxies": f"{len(GLOBAL_PROXIES)} proxies loaded",
                "transport_routes": f"{len(TRANSPORT_ROUTES)} routing rules configured",
                "routes": [
                    {"url": route["url"], "has_proxy": route["proxy"] is not None}
                    for route in TRANSPORT_ROUTES
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

    def _prefetch_next_segments(
        self, current_url, init_url, key, key_id, headers, bypass_warp: bool = False
    ):
        """Identifica i prossimi segmenti e avvia il download in background."""
        try:
            parsed = urllib.parse.urlparse(current_url)
            path = parsed.path

            # Cerca pattern numerico alla fine del path (es. segment-1.m4s)
            match = re.search(r"([-_])(\d+)(\.[^.]+)$", path)
            if not match:
                return

            separator, current_number, extension = match.groups()
            current_num = int(current_number)

            # Prefetch next 3 segments
            for i in range(1, 4):
                next_num = current_num + i

                # Replace number in path
                pattern = f"{separator}{current_number}{re.escape(extension)}$"
                replacement = f"{separator}{next_num}{extension}"
                new_path = re.sub(pattern, replacement, path)

                # Reconstruct URL
                next_url = urllib.parse.urlunparse(parsed._replace(path=new_path))

                cache_key = f"{next_url}:{key_id}"

                if (
                    cache_key not in self.segment_cache
                    and cache_key not in self.prefetch_tasks
                ):
                    self.prefetch_tasks.add(cache_key)
                    asyncio.create_task(
                        self._fetch_and_cache_segment(
                            next_url,
                            init_url,
                            key,
                            key_id,
                            headers,
                            cache_key,
                            bypass_warp=bypass_warp,
                        )
                    )

        except Exception as e:
            logger.warning(f"⚠️ Prefetch error: {e}")

    async def _fetch_and_cache_segment(
        self, url, init_url, key, key_id, headers, cache_key, bypass_warp: bool = False
    ):
        """Scarica, decripta e mette in cache un segmento in background."""
        try:
            if decrypt_segment is None:
                return

            # Ensure dynamic WARP bypass for prefetch
            self._check_dynamic_warp_bypass(url)
            
            session, _ = await self._get_proxy_session(url, bypass_warp=bypass_warp)

            # Download Init (usa cache se possibile)
            init_content = b""
            if init_url:
                if init_url in self.init_cache:
                    init_content = self.init_cache[init_url]
                else:
                    disable_ssl = get_ssl_setting_for_url(init_url, TRANSPORT_ROUTES)
                    try:
                        async with session.get(
                            init_url,
                            headers=headers,
                            ssl=not disable_ssl,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                init_content = await resp.read()
                                self.init_cache[init_url] = init_content
                    except Exception:
                        pass

            # Download Segment
            segment_content = None
            disable_ssl = get_ssl_setting_for_url(url, TRANSPORT_ROUTES)
            try:
                async with session.get(
                    url,
                    headers=headers,
                    ssl=not disable_ssl,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        segment_content = await resp.read()
            except Exception:
                pass

            if segment_content:
                # Decrypt
                # Decrypt in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                decrypted_content = await loop.run_in_executor(
                    None, decrypt_segment, init_content, segment_content, key_id, key
                )
                import time

                self.segment_cache[cache_key] = (decrypted_content, time.time())
                logger.info(f"📦 Prefetched segment: {url.split('/')[-1]}")

        except Exception as e:
            pass
        finally:
            if cache_key in self.prefetch_tasks:
                self.prefetch_tasks.remove(cache_key)

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

        # Check cache first
        import time

        cache_key = f"{url}:{key_id}:ts"  # Use distinct cache key for TS
        if cache_key in self.segment_cache:
            cached_content, cached_time = self.segment_cache[cache_key]
            if time.time() - cached_time < self.segment_cache_ttl:
                logger.info(f"📦 Cache HIT for segment: {url.split('/')[-1]}")
                return web.Response(
                    body=cached_content,
                    status=200,
                    headers={
                        "Content-Type": "video/MP2T",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
            else:
                del self.segment_cache[cache_key]

        try:
            # Ricostruisce gli headers per le richieste upstream
            headers = {"Connection": "keep-alive", "Accept-Encoding": "identity"}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    headers[header_name] = param_value

            # Get proxy-enabled session for segment fetches
            bypass_warp = request.query.get("warp", "").lower() == "off"
            segment_session, segment_proxy = await self._get_proxy_session(
                url, bypass_warp=bypass_warp
            )
            if segment_proxy:
                logger.info(f"📡 [Decrypt] Using session via proxy: {segment_proxy}")

            try:
                # Parallel download of init and media segment
                async def fetch_init():
                    if not init_url:
                        return b""
                    if init_url in self.init_cache:
                        return self.init_cache[init_url]
                    disable_ssl = get_ssl_setting_for_url(init_url, TRANSPORT_ROUTES)
                    try:
                        async with segment_session.get(
                            init_url,
                            headers=headers,
                            ssl=not disable_ssl,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                self.init_cache[init_url] = content
                                return content
                            logger.error(
                                f"❌ Init segment returned status {resp.status}: {init_url}"
                            )
                            return None
                    except Exception as e:
                        logger.error(f"❌ Failed to fetch init segment: {e}")
                        return None

                async def fetch_segment():
                    disable_ssl = get_ssl_setting_for_url(url, TRANSPORT_ROUTES)
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
                # Session is pooled/cached, so we don't close it
                pass

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
            if ENABLE_REMUXING:
                ts_content = await self._remux_to_ts(combined_content)
                if not ts_content:
                    logger.warning("⚠️ Remux failed, serving raw fMP4")
                    ts_content = combined_content
                    content_type = "video/mp4"
                else:
                    content_type = "video/MP2T"
                    logger.info("⚡ Remuxed fMP4 -> TS")
            else:
                logger.debug("⏩ Remuxing disabled, serving raw fMP4")
                ts_content = combined_content
                content_type = "video/mp4"

            # Store in cache
            self.segment_cache[cache_key] = (ts_content, time.time())

            # Clean old cache entries (keep max 50)
            if len(self.segment_cache) > 50:
                oldest_keys = sorted(
                    self.segment_cache.keys(), key=lambda k: self.segment_cache[k][1]
                )[:20]
                for k in oldest_keys:
                    del self.segment_cache[k]

            # Prefetch next segments in background
            self._prefetch_next_segments(
                url, init_url, key, key_id, headers, bypass_warp=bypass_warp
            )

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
            if GLOBAL_PROXIES:
                exit_strategy = (
                    f"Proxy Globale Random (Pool di {len(GLOBAL_PROXIES)} proxy)"
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
            proxy = random.choice(GLOBAL_PROXIES) if GLOBAL_PROXIES else None

            # Crea una sessione dedicata con il proxy configurato
            if proxy:
                logger.info(f"🌍 Checking IP via proxy: {proxy}")
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

    async def cleanup(self):
        """Pulizia delle risorse"""
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            if self.flex_session and not self.flex_session.closed:
                await self.flex_session.close()

            # Close all cached proxy sessions
            for proxy_url, session in list(self.proxy_sessions.items()):
                if session and not session.closed:
                    await session.close()
            self.proxy_sessions.clear()

            # Close all cached curl sessions
            for session in list(self.curl_sessions.values()):
                if session:
                    await session.close()
            self.curl_sessions.clear()

            for extractor in self.extractors.values():
                if hasattr(extractor, "close"):
                    await extractor.close()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
