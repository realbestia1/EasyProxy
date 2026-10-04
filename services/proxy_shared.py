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
logger = logging.getLogger("services.proxy")
import yarl
import aiohttp
from aiohttp_socks import ProxyConnector
from aiohttp import (
    web,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    ClientPayloadError,
    ServerDisconnectedError,
    ClientConnectionError,
)
import importlib.util

# Lazy check — find_spec does NOT load module, preserving startup behavior.
# never get pulled in at import time. Actual load happens only at first call site.
HAS_CURL_CFFI = importlib.util.find_spec('curl_cffi') is not None
CurlAsyncSession = None

def get_curl_async_session():
    """Lazy import: returns CurlAsyncSession class or None."""
    if not HAS_CURL_CFFI:
        return None
    from curl_cffi.requests import AsyncSession
    return AsyncSession

import config as _config
import config_store as _config_store
from config import (
    get_proxy_for_url,
    get_ssl_setting_for_url,
    get_connector_for_proxy,
    API_PASSWORD,
    check_password,
    get_client_ip,
    APP_VERSION,
    BYPASS_WARP_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    BYPASS_TOR_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    mark_proxy_dead,
    get_extractor_proxies,
    ALL_PROXY_ERRORS,
    is_warp_proxy_url,
)
from extractors.registry import *
from extractors.provider_hooks import *
from services.manifest_rewriter import ManifestRewriter
from services.secure_state import open_state, seal_state


def safe_log_endpoint(value: str | None) -> str:
    """Return URL endpoint without query tokens or credentials."""
    parsed = urlparse(str(value or ""))
    if not parsed.netloc:
        return "unknown"
    path = parsed.path or "/"
    if len(path) > 160:
        path = path[:157] + "..."
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def extractor_log_name(request=None, extractor=None, fallback: str = "unknown") -> str:
    """Resolve a stable, human-readable extractor label for request logs."""
    if extractor is not None:
        if isinstance(extractor, str):
            value = extractor
        else:
            value = getattr(extractor, "extractor_name", None) or type(extractor).__name__
        if value:
            return str(value).replace("_direct", "").replace("_noproxy", "")
    if request is not None:
        query = getattr(request, "query", {})
        value = query.get("extractor_key") or query.get("host")
        if value:
            return str(value).replace("_direct", "").replace("_noproxy", "")
    return fallback


def request_log_context(request=None, target_url: str | None = None, route: str | None = None, extractor=None) -> str:
    """Build consistent extractor/request context for service-level logs."""
    query = getattr(request, "query", {}) if request is not None else {}
    path = getattr(request, "path", "") or ""
    if "segment" in path:
        request_type = "segment"
    elif "manifest" in path or path.endswith(".mpd"):
        request_type = "manifest"
    else:
        request_type = "request"
    if query.get("direct_hls") == "1":
        extractor_fallback = "direct_hls"
    elif path.startswith("/proxy/hls/") or path.startswith("/proxy/mpd/"):
        extractor_fallback = "generic_hls"
    else:
        extractor_fallback = "unknown"
    requested = query.get("orig_url") or query.get("original_channel_url") or query.get("url") or query.get("d")
    # Old/generated relay URLs may not carry extractor_key. Keep service logs
    # useful by inferring only the unambiguous provider URL we support here.
    if extractor_fallback in {"unknown", "generic_hls"} and "vavoo.to" in str(requested or "").lower():
        extractor_fallback = "vavoo"
    route_text = route or "unknown"
    return (
        f"extractor={extractor_log_name(request, extractor, extractor_fallback)} "
        f"type={request_type} route={route_text} "
        f"requested={safe_log_endpoint(requested)} target={safe_log_endpoint(target_url)}"
    )


def safe_log_route(proxy_url: str | None) -> str:
    """Return route identity without proxy credentials."""
    if not proxy_url:
        return "DIRECT"
    if str(proxy_url).upper() in {"WARP", "DIRECT", "BYPASS"}:
        return str(proxy_url).upper()
    if _config.is_warp_proxy_url(str(proxy_url)):
        return "WARP"
    parsed = urlparse(str(proxy_url))
    if parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        return f"PROXY({parsed.scheme or 'unknown'}://{parsed.hostname}{port})"
    return "PROXY"


# Global registry for domains already bypassed in WARP to avoid redundant os.system calls
BYPASSED_WARP_DOMAINS = set()

# Legacy MPD converter
MPDToHLSConverter = None
decrypt_segment = None

try:
    from utils.drm_decrypter import decrypt_segment
except ImportError:
    pass

try:
    from utils.mpd_converter import MPDToHLSConverter
    logger.info("✅ Legacy MPD converter loaded")
except ImportError as e:
    logger.warning(f"⚠️ Legacy MPD converter not available: {e}")

PlaylistBuilder = None
try:
    from routes.playlist_builder import PlaylistBuilder
    logger.info("✅ PlaylistBuilder module loaded.")
except ImportError:
    logger.warning("PlaylistBuilder module not found. PlaylistBuilder functionality disabled.")

_STDLIB_MODULES = {
    "asyncio", "logging", "random", "re", "sys", "os", "time", "socket",
    "urllib", "base64", "binascii", "hashlib", "hmac", "json", "ssl",
}

class ProxyDeadRetryError(Exception):
    """Raised when the proxy dies during playlist fetch; triggers re-extraction."""

def get_public_base_url(request):
    """Build the public origin, preserving HTTPS behind reverse proxies."""
    cf_visitor = request.headers.get("CF-Visitor", "").lower()
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    scheme = forwarded_proto.split(",", 1)[0].strip().lower() or request.scheme
    if '"scheme"' in cf_visitor and "https" in cf_visitor:
        scheme = "https"
    if scheme not in {"http", "https"}:
        scheme = request.scheme

    forwarded_host = request.headers.get("X-Forwarded-Host", "")
    host = forwarded_host.split(",", 1)[0].strip() or request.host
    return f"{scheme}://{host}"

def hex_to_b64url(hex_str: str) -> str:
    return (
        base64.urlsafe_b64encode(binascii.unhexlify(hex_str))
        .decode("utf-8")
        .rstrip("=")
    )

def parse_clearkey_params(request) -> str | None:
    drm_token = request.query.get("drm_token")
    if drm_token:
        state = open_state(drm_token, "clearkey")
        return str((state or {}).get("clearkey") or "") or None
    clearkey = request.query.get("clearkey")
    if clearkey:
        return clearkey
    key_id_param = request.query.get("key_id")
    key_val_param = request.query.get("key")
    if key_id_param and key_val_param:
        key_ids = key_id_param.split(",")
        key_vals = key_val_param.split(",")
        if len(key_ids) == len(key_vals):
            parts = [f"{k.strip()}:{v.strip()}" for k, v in zip(key_ids, key_vals)]
            return ",".join(parts)
        if len(key_ids) == 1 and len(key_vals) == 1:
            return f"{key_id_param}:{key_val_param}"
        logger.warning(
            f"Mismatch in key_id/key count: {len(key_ids)} vs {len(key_vals)}"
        )
        min_len = min(len(key_ids), len(key_vals))
        parts = [f"{key_ids[i].strip()}:{key_vals[i].strip()}" for i in range(min_len)]
        return ",".join(parts)
    elif key_val_param:
        return key_val_param
    return None


def seal_clearkey(clearkey: str) -> str:
    return seal_state({"clearkey": clearkey}, "clearkey")


def get_extractor_routing_overrides(extractor_key: str | None) -> tuple[bool, bool]:
    """Return admin WARP/proxy bypass flags for an extractor relay chain."""
    key = str(extractor_key or "").strip().lower()
    if not key:
        return False, False

    base_key = key.replace("_direct", "").replace("_noproxy", "")

    def configured(name: str) -> set[str]:
        values = _config_store.get(name, [])
        return {str(value).strip().lower() for value in values if value}

    return (
        base_key in configured("warp_off_extractors"),
        base_key in configured("proxy_off_extractors"),
    )

def check_vavoo_request(headers: dict, request: web.Request, url: str) -> bool:
    return (
        "vavoo" in (request.query.get("h_Referer") or "").lower()
        or "vavoo" in (request.query.get("h_Origin") or "").lower()
        or "vavoo" in (headers.get("Referer") or "").lower()
        or "vavoo" in (headers.get("Origin") or "").lower()
        or "vavoo" in (request.headers.get("Referer") or "").lower()
        or "vavoo" in url.lower()
        or any(x in url.lower() for x in ["/sunshine/", "lokke", "mediahubmx"])
    )

def set_response_header(target: dict, name: str, value: str):
    keys_to_remove = [k for k in target.keys() if k.lower() == name.lower()]
    for key in keys_to_remove:
        del target[key]
    target[name] = value

_DYNAMIC_CONFIG_NAMES = {
    "GLOBAL_PROXIES", "TRANSPORT_ROUTES", "ENABLE_WARP", "WARP_PROXY_URL",
    "WARP_EXCLUDE_DOMAINS", "DVR_ENABLED",
    "RECORDINGS_DIR", "MAX_RECORDING_DURATION", "RECORDINGS_RETENTION_DAYS",
    "WARP_OFF_EXTRACTORS",
    "WARP_LICENSE_KEY", "PROXY_TEST_TIMEOUT", "PROXY_TEST_CONCURRENCY",
    "LOG_LEVEL_STR",
}

def __getattr__(name):
    if name in _DYNAMIC_CONFIG_NAMES:
        return getattr(_config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Active streams tracker: tracks active stream sessions.
# Structure: { client_ip: { "url": str, "last_active": float, "user_agent": str } }
ACTIVE_STREAM_SESSIONS = {}

def record_stream_activity(client_ip: str, url: str, user_agent: str = "", is_segment: bool = False):
    now = time.time()
    # Clean up old sessions (older than 30 seconds)
    for ip in list(ACTIVE_STREAM_SESSIONS.keys()):
        if now - ACTIVE_STREAM_SESSIONS[ip]["last_active"] > 30:
            ACTIVE_STREAM_SESSIONS.pop(ip, None)
            
    # If it is a segment request and we already have a manifest request recorded for this IP in the last 30s,
    # just update the activity timestamp and keep the manifest URL (which is cleaner).
    if is_segment and client_ip in ACTIVE_STREAM_SESSIONS:
        ACTIVE_STREAM_SESSIONS[client_ip]["last_active"] = now
        if user_agent:
            ACTIVE_STREAM_SESSIONS[client_ip]["user_agent"] = user_agent
    else:
        ACTIVE_STREAM_SESSIONS[client_ip] = {
            "url": url,
            "last_active": now,
            "user_agent": user_agent
        }

def get_active_streams() -> list:
    now = time.time()
    active = []
    # Clean up and collect active sessions
    for ip, info in list(ACTIVE_STREAM_SESSIONS.items()):
        if now - info["last_active"] <= 30:
            active.append({
                "ip": ip,
                "url": info["url"],
                "last_active": info["last_active"],
                "elapsed_since_active": int(now - info["last_active"]),
                "user_agent": info["user_agent"]
            })
        else:
            ACTIVE_STREAM_SESSIONS.pop(ip, None)
    return active


__all__ = [name for name in globals() if not name.startswith('__') and name not in _STDLIB_MODULES]
# Commonly used stdlib modules exposed via star import for downstream compatibility
__all__ += ["asyncio", "re", "sys", "os", "time", "json", "base64", "hashlib", "ssl", "socket", "random", "logging", "urllib"]
