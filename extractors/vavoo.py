import asyncio
import base64
import json
import logging
import re
import socket
import time
import uuid
from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, TCPConnector
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs
from config import BYPASS_WARP_CONTEXT, get_connector_for_proxy, get_preferred_proxy_for_url
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

# Tokens expire every 10 minutes (see vavoo_proxy.py TOKEN_ADDON_SIG)
_TOKEN_REFRESH_AGE = 480
_PING_URLS = [
    "https://www.vavoo.tv/api/app/ping",
    "https://www.vypn.net/api/app/ping",
]
_VYPN_PACKAGE = "net.vypn.app"
_VYPN_VERSION = "1.4.1"
# Mirror frontend dello stesso backend mediahubmx (verificato ott 2026:
# stessa firma dai ping, stesso catalogo, resolve cross-compatibile tra
# domini). Il gate di extract() e l'auto-detect accettano tutti questi
# domini; le basi di resolve/catalogo restano le 4 con fallback (huhu.to /
# oha.to / voltop.net esclusi dalle basi: verificati giu' insieme a
# vavoo.to / kool.to, stessa infra).
VAVOO_MIRROR_DOMAINS = (
    "vavoo.to",
    "kool.to",
    "vavoo.net",
    "kool.ws",
    "huhu.to",
    "oha.to",
    "voltop.net",
)
_BASE_SITES = ["https://vavoo.to", "https://kool.to", "https://vavoo.net", "https://kool.ws"]
_LANGUAGE = "de"
_REGION = "DE"


class VavooExtractor(BaseExtractor):
    """Vavoo URL extractor — resolves vavoo play URLs (all mirror frontends)
    to clean HLS via lokke.app auth."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vavoo", bypass_warp=bypass_warp)
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "okhttp/4.11.0"
        }
        self.session = None
        self._session_lock = asyncio.Lock()
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self._proxy = None  # scelto una volta, non cambia
        self.addon_sig = None
        self.addon_sig_ts = 0
        self._addon_sig_proxy = None
        self.base_index = 0

    @property
    def _resolve_url(self) -> str:
        return _BASE_SITES[self.base_index] + "/mediahubmx-resolve.json"

    def _switch_to_next_base(self):
        self.base_index = (self.base_index + 1) % len(_BASE_SITES)
        logger.warning(f"Vavoo switched base site -> {_BASE_SITES[self.base_index]}")

    async def _get_session(self):
        async with self._session_lock:
            bypass_warp = bool(self.bypass_warp_active) or bool(BYPASS_WARP_CONTEXT.get())
            warp_enabled = bool(_cfg._get_dynamic_warp_enabled())
            warp_excluded = bool(_cfg._is_warp_excluded(self._resolve_url))
            # Re-evaluate on every call: the admin WARP toggle can change while
            # this cached extractor is still alive.
            selected_proxy = await get_preferred_proxy_for_url(
                self._resolve_url,
                "vavoo",
                self.proxies,
                bypass_warp,
            )
            # New-contract per-request routing (explicit ?proxy= / proxy=off /
            # direct) wins over the auto chain; proxy_exclude_domains drops
            # even the forced proxy (WARP exempt).
            if self._force_direct:
                selected_proxy = None
            else:
                forced = _cfg.effective_forced_proxy(self._resolve_url, self._forced_proxy)
                if forced:
                    selected_proxy = forced

            # Guard against a stale/empty route result: with WARP enabled, Vavoo
            # must never silently fall back to a direct socket.
            if (
                selected_proxy is None
                and warp_enabled
                and not bypass_warp
                and not warp_excluded
            ):
                selected_proxy = _cfg.WARP_PROXY_URL

            direct_allowed = _cfg.is_direct_connection_allowed(bypass_warp)
            logger.debug(
                "Vavoo routing: proxy=%s warp_enabled=%s excluded=%s "
                "bypass_warp=%s direct_allowed=%s ipv4_only=yes",
                selected_proxy or "DIRECT",
                warp_enabled,
                warp_excluded,
                bypass_warp,
                direct_allowed,
            )

            self._proxy = selected_proxy
            self.last_used_proxy = selected_proxy

            if self._proxy is None and not self._force_direct and not direct_allowed:
                raise ClientConnectionError(
                    "Vavoo: direct fallback disabled; no proxy route available"
                )

            session = self._route_sessions.get(selected_proxy)
            if session is not None and not session.closed:
                self.session = session
                return self.session

            timeout = ClientTimeout(total=60, connect=30, sock_read=30)

            if self._proxy:
                logger.debug(f"Using proxy for Vavoo session: {self._proxy}")
                connector = get_connector_for_proxy(
                    self._proxy,
                    force_ipv4=True,
                    rdns=False,
                )
            else:
                connector = TCPConnector(
                    family=socket.AF_INET,
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=15,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True,
                )

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={'User-Agent': self.base_headers["user-agent"]}
            )
            self._route_sessions[selected_proxy] = self.session
        return self.session

    async def _get_external_ip(self) -> Optional[str]:
        """Public IP seen by the upstream (via our proxy, if any)."""
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.ipify.org?format=json",
                timeout=ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return data.get("ip")
        except Exception as e:
            logger.debug(f"Vavoo external IP lookup failed: {e}")
        return None

    def _rewrite_addon_sig_ip(self, sig: str, client_ip: str) -> str:
        """Rewrite the client IP embedded inside the base64 addonSig so it
        matches where our resolve requests actually come from."""
        try:
            padded = sig + '=' * (-len(sig) % 4)
            decoded = base64.b64decode(padded).decode('utf-8')
            sig_obj = json.loads(decoded)
            if not isinstance(sig_obj, dict) or "data" not in sig_obj:
                return sig
            data_obj = json.loads(sig_obj["data"])
            ips = data_obj.get("ips")
            if not isinstance(ips, list):
                ips = []
            data_obj["ips"] = [client_ip] + [ip for ip in ips if ip and ip != client_ip]
            if isinstance(data_obj.get("ip"), str):
                data_obj["ip"] = client_ip
            sig_obj["data"] = json.dumps(data_obj)
            return base64.b64encode(json.dumps(sig_obj).encode('utf-8')).decode('ascii')
        except Exception as e:
            logger.warning(f"Vavoo addonSig IP rewrite failed, keeping original sig: {e}")
            return sig

    async def _fetch_addon_sig(self) -> Optional[str]:
        """Obtain a fresh addonSig from the VYPN/vavoo ping endpoints."""
        session = await self._get_session()
        unique_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)
        payload = {
            "token": "",
            "reason": "app-focus",
            "locale": _LANGUAGE,
            "theme": "dark",
            "metadata": {
                "device": {"type": "phone", "uniqueId": unique_id},
                "os": {"name": "android", "version": "14", "abis": ["arm64-v8a"], "host": "android"},
                "app": {"platform": "android"},
                "version": {"package": _VYPN_PACKAGE, "binary": _VYPN_VERSION, "js": _VYPN_VERSION},
            },
            "appFocusTime": 0,
            "playerActive": False,
            "playDuration": 0,
            "devMode": False,
            "hasAddon": True,
            "castConnected": False,
            "package": _VYPN_PACKAGE,
            "version": _VYPN_VERSION,
            "process": "app",
            "firstAppStart": ts - 86400000,
            "lastAppStart": ts,
            "ipLocation": None,
            "adblockEnabled": True,
            "migrationApplied": False,
            "migrationTargetInstalled": False,
            "proxy": {"supported": ["ss"], "engine": "Mu", "ssVersion": "2022", "enabled": False, "autoServer": True, "id": ""},
            "iap": {"supported": False, "error": ""},
        }
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
        }
        for url in _PING_URLS:
            try:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=ClientTimeout(total=15), ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        sig = data.get("addonSig") or data.get("mhub")
                        if sig:
                            return str(sig)
                        logger.warning(f"Vavoo ping {url}: no addonSig in response")
                    else:
                        logger.warning(f"Vavoo ping {url}: status {resp.status}")
            except Exception as e:
                logger.warning(
                    "Vavoo ping %s failed via %s: %s: %r",
                    url,
                    self._proxy or "direct",
                    type(e).__name__,
                    e,
                )
        return None

    async def _get_sig(self, force: bool = False) -> Optional[str]:
        await self._get_session()
        now = time.time()
        proxy_changed = getattr(self, "_addon_sig_proxy", None) != self._proxy
        if self.addon_sig and not force and not proxy_changed and (now - self.addon_sig_ts < _TOKEN_REFRESH_AGE):
            return self.addon_sig
        sig = await self._fetch_addon_sig()
        if not sig:
            if self.addon_sig and not proxy_changed:
                logger.warning("Vavoo token refresh failed, reusing old addonSig")
                return self.addon_sig
            return None
        client_ip = await self._get_external_ip()
        if client_ip:
            sig = self._rewrite_addon_sig_ip(sig, client_ip)
        self.addon_sig = sig
        self.addon_sig_ts = now
        self._addon_sig_proxy = self._proxy
        logger.info("Vavoo addonSig obtained")
        return sig

    async def _resolve_via_mediahubmx(self, url: str) -> Optional[str]:
        """Resolve vavoo URL to stream URL via mediahubmx-resolve.json."""
        # Normalize /watch?live=X to /vavoo-iptv/play/X
        if "/watch" in url:
            params = parse_qs(urlparse(url).query)
            live_id = params.get('live', [None])[0]
            if live_id:
                url = f"https://vavoo.to/vavoo-iptv/play/{live_id}"

        # Normalize /play/X to /vavoo-iptv/play/X (il play-ID e' lo stesso su
        # tutti i mirror, e il resolve e' cross-compatibile: la forma
        # canonica vavoo.to si risolve da qualsiasi base).
        m = re.search(r'/play/([^/?#]+)', url)
        if m:
            url = f"https://vavoo.to/vavoo-iptv/play/{m.group(1)}"

        # Prova ogni base a turno: con un frontend giu' (connection refused,
        # come osservato su vavoo.to/kool.to) si passa alla successiva invece
        # di fallire subito. Firma rinnovata ad ogni cambio base.
        last_err = None
        sig = await self._get_sig()
        if not sig:
            logger.warning("Vavoo no addonSig available, resolving without signature")
        for _ in range(len(_BASE_SITES)):
            # _get_sig() may rebuild session when proxy bypass is active.
            session = await self._get_session()

            headers = {
                "Origin": "https://vavoo.to",
                "Referer": "https://vavoo.to/",
                "User-Agent": "MediaHubMX/2",
                "Accept": "*/*",
                "Content-Type": "application/json; charset=utf-8",
                "Accept-Language": _LANGUAGE,
            }
            if sig:
                headers["mediahubmx-signature"] = sig
            body = {"language": _LANGUAGE, "region": _REGION, "url": url, "clientVersion": "3.0.2"}
            try:
                async with session.post(self._resolve_url, json=body, headers=headers,
                                        timeout=ClientTimeout(total=15), ssl=False) as resp:
                    # Switch di base solo su errori potenzialmente legati alla
                    # singola base (gateway/backend): gli altri 4xx (400/401/
                    # 403/404/429) dipendono dalla richiesta, riprovarli
                    # altrove spreca solo tentativi/ping (il 429 peggiora).
                    if resp.status in (451, 502, 500, 503, 504):
                        last_err = f"HTTP {resp.status} on {_BASE_SITES[self.base_index]}"
                        logger.warning(f"Vavoo resolve status {resp.status}, switching base site")
                        self._switch_to_next_base()
                        sig = await self._get_sig(force=True)
                        if not sig:
                            return None
                        continue
                    if resp.status != 200:
                        logger.warning(f"Vavoo resolve returned status {resp.status}")
                        return None
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and data and data[0].get("url"):
                        return str(data[0]["url"])
                    if isinstance(data, dict):
                        if data.get("url"):
                            return str(data["url"])
                        if data.get("data", {}).get("url"):
                            return str(data["data"]["url"])
                    logger.warning("Vavoo resolve response missing URL")
                    return None
            except Exception as e:
                last_err = e
                logger.warning(f"Vavoo resolve exception on {_BASE_SITES[self.base_index]}, "
                               f"switching base site: {e}")
                self._switch_to_next_base()
                sig = await self._get_sig(force=True)
                if not sig:
                    return None
                continue
        logger.warning(f"Vavoo resolve failed on all base sites, last error: {last_err}")
        return None

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        if not any(d in url.lower() for d in VAVOO_MIRROR_DOMAINS):
            raise ExtractorError("Not a valid Vavoo URL")

        self._apply_routing_kwargs(url, kwargs)
        resolved_url = await self._resolve_via_mediahubmx(url)
        if not resolved_url:
            raise ExtractorError("Vavoo resolve failed")

        logger.info(f"Resolved via mediahubmx: {resolved_url[:80]}...")

        return {
            "destination_url": resolved_url,
            "request_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://vavoo.to",
                "Origin": "https://vavoo.to",
                "X-EasyProxy-Disable-SSL": "1",
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "disable_ssl": True,
            "selected_proxy": self._proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

    async def close(self):
        async with self._session_lock:
            sessions = set(self._route_sessions.values())
            if self.session is not None:
                sessions.add(self.session)
            for session in sessions:
                if not session.closed:
                    await session.close()
            self._route_sessions.clear()
            self.session = None
