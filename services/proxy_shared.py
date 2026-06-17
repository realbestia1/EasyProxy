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
from aiohttp import (
    web,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    ClientPayloadError,
    ServerDisconnectedError,
    ClientConnectionError,
)
from aiohttp_socks import ProxyConnector, ProxyError as AioProxyError
from python_socks import ProxyError as PyProxyError

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    CurlAsyncSession = None

import config as _config
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
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    mark_proxy_dead,
    get_extractor_proxies,
)
from extractors.registry import *
from extractors.provider_hooks import *
from services.manifest_rewriter import ManifestRewriter

# Global registry for domains already bypassed in WARP to avoid redundant os.system calls
BYPASSED_WARP_DOMAINS = set()

# Legacy MPD converter (always attempt loading for runtime MPD_MODE changes)
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

def hex_to_b64url(hex_str: str) -> str:
    return (
        base64.urlsafe_b64encode(binascii.unhexlify(hex_str))
        .decode("utf-8")
        .rstrip("=")
    )

def parse_clearkey_params(request) -> str | None:
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
    "WARP_EXCLUDE_DOMAINS", "MPD_MODE", "ENABLE_REMUXING", "DVR_ENABLED",
    "RECORDINGS_DIR", "MAX_RECORDING_DURATION", "RECORDINGS_RETENTION_DAYS",
    "FLARESOLVERR_URL", "FLARESOLVERR_TIMEOUT", "WARP_OFF_EXTRACTORS",
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
