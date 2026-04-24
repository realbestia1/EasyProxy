import asyncio
import logging
import time
import socket
import hashlib
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from typing import Optional, Dict, Any
from urllib.parse import quote_plus
from config import get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy
import random

logger = logging.getLogger(__name__)

# Auth constants aligned with working plugin.video.vavooto
_LOKKE_PING_URL = "https://www.lokke.app/api/app/ping"
_LOKKE_TOKEN = "ldCvE092e7gER0rVIajfsXIvRhwlrAzP6_1oEJ4q6HH89QHt24v6NNL_jQJO219hiLOXF2hqEfsUuEWitEIGN4EaHHEHb7Cd7gojc5SQYRFzU3XWo_kMeryAUbcwWnQrnf0-"
_RESOLVE_URL = "https://vavoo.to/mediahubmx-resolve.json"
_TS_PING2_URL = "https://www.vavoo.tv/api/box/ping2"
_TS_VEC = "9frjpxPjxSNilxJPCJ0XGYs6scej3dW/h/VWlnKUiLSG8IP7mfyDU7NirOlld+VtCKGj03XjetfliDMhIev7wcARo+YTU8KPFuVQP9E2DVXzY2BFo1NhE6qEmPfNDnm74eyl/7iFJ0EETm6XbYyz8IKBkAqPN/Spp3PZ2ulKg3QBSDxcVN4R5zRn7OsgLJ2CNTuWkd/h451lDCp+TtTuvnAEhcQckdsydFhTZCK5IiWrrTIC/d4qDXEd+GtOP4hPdoIuCaNzYfX3lLCwFENC6RZoTBYLrcKVVgbqyQZ7DnLqfLqvf3z0FVUWx9H21liGFpByzdnoxyFkue3NzrFtkRL37xkx9ITucepSYKzUVEfyBh+/3mtzKY26VIRkJFkpf8KVcCRNrTRQn47Wuq4gC7sSwT7eHCAydKSACcUMMdpPSvbvfOmIqeBNA83osX8FPFYUMZsjvYNEE3arbFiGsQlggBKgg1V3oN+5ni3Vjc5InHg/xv476LHDFnNdAJx448ph3DoAiJjr2g4ZTNynfSxdzA68qSuJY8UjyzgDjG0RIMv2h7DlQNjkAXv4k1BrPpfOiOqH67yIarNmkPIwrIV+W9TTV/yRyE1LEgOr4DK8uW2AUtHOPA2gn6P5sgFyi68w55MZBPepddfYTQ+E1N6R/hWnMYPt/i0xSUeMPekX47iucfpFBEv9Uh9zdGiEB+0P3LVMP+q+pbBU4o1NkKyY1V8wH1Wilr0a+q87kEnQ1LWYMMBhaP9yFseGSbYwdeLsX9uR1uPaN+u4woO2g8sw9Y5ze5XMgOVpFCZaut02I5k0U4WPyN5adQjG8sAzxsI3KsV04DEVymj224iqg2Lzz53Xz9yEy+7/85ILQpJ6llCyqpHLFyHq/kJxYPhDUF755WaHJEaFRPxUqbparNX+mCE9Xzy7Q/KTgAPiRS41FHXXv+7XSPp4cy9jli0BVnYf13Xsp28OGs/D8Nl3NgEn3/eUcMN80JRdsOrV62fnBVMBNf36+LbISdvsFAFr0xyuPGmlIETcFyxJkrGZnhHAxwzsvZ+Uwf8lffBfZFPRrNv+tgeeLpatVcHLHZGeTgWWml6tIHwWUqv2TVJeMkAEL5PPS4Gtbscau5HM+FEjtGS+KClfX1CNKvgYJl7mLDEf5ZYQv5kHaoQ6RcPaR6vUNn02zpq5/X3EPIgUKF0r/0ctmoT84B2J1BKfCbctdFY9br7JSJ6DvUxyde68jB+Il6qNcQwTFj4cNErk4x719Y42NoAnnQYC2/qfL/gAhJl8TKMvBt3Bno+va8ve8E0z8yEuMLUqe8OXLce6nCa+L5LYK1aBdb60BYbMeWk1qmG6Nk9OnYLhzDyrd9iHDd7X95OM6X5wiMVZRn5ebw4askTTc50xmrg4eic2U1w1JpSEjdH/u/hXrWKSMWAxaj34uQnMuWxPZEXoVxzGyuUbroXRfkhzpqmqqqOcypjsWPdq5BOUGL/Riwjm6yMI0x9kbO8+VoQ6RYfjAbxNriZ1cQ+AW1fqEgnRWXmjt4Z1M0ygUBi8w71bDML1YG6UHeC2cJ2CCCxSrfycKQhpSdI1QIuwd2eyIpd4LgwrMiY3xNWreAF+qobNxvE7ypKTISNrz0iYIhU0aKNlcGwYd0FXIRfKVBzSBe4MRK2pGLDNO6ytoHxvJweZ8h1XG8RWc4aB5gTnB7Tjiqym4b64lRdj1DPHJnzD4aqRixpXhzYzWVDN2kONCR5i2quYbnVFN4sSfLiKeOwKX4JdmzpYixNZXjLkG14seS6KR0Wl8Itp5IMIWFpnNokjRH76RYRZAcx0jP0V5/GfNNTi5QsEU98en0SiXHQGXnROiHpRUDXTl8FmJORjwXc0AjrEMuQ2FDJDmAIlKUSLhjbIiKw3iaqp5TVyXuz0ZMYBhnqhcwqULqtFSuIKpaW8FgF8QJfP2frADf4kKZG1bQ99MrRrb2A="


class ExtractorError(Exception):
    pass


class VavooExtractor:
    """Vavoo URL extractor — resolves vavoo.to play URLs to clean HLS via lokke.app auth."""
    
    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "okhttp/4.11.0"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.proxies = proxies or GLOBAL_PROXIES
        self._cached_sig = None
        self._cached_sig_ts = 0

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None
        
    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            
            # Determina il proxy per l'URL (se fornito)
            proxy = None
            if url:
                proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            else:
                proxy = self._get_random_proxy()
                
            if proxy:
                logger.debug(f"Using proxy for Vavoo session: {proxy}")
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=60,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True,
                    family=socket.AF_INET
                )

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={'User-Agent': self.base_headers["user-agent"]}
            )
        return self.session

    async def _get_auth_signature(self) -> Optional[str]:
        """Get addon signature from lokke.app (aligned with plugin.video.vavooto)."""
        # Cache signature for 5 minutes
        if self._cached_sig and (time.time() - self._cached_sig_ts) < 300:
            return self._cached_sig

        session = await self._get_session(_LOKKE_PING_URL)
        unique_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
        now_ms = int(time.time() * 1000)
        body = {
            "token": _LOKKE_TOKEN,
            "reason": "app-blur",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {"type": "Handset", "brand": "google", "model": "Nexus", "name": "21081111RG", "uniqueId": unique_id},
                "os": {"name": "android", "version": "7.1.2", "abis": ["arm64-v8a"], "host": "android"},
                "app": {"platform": "android", "version": "1.1.0", "buildId": "97215000", "engine": "hbc85",
                        "signatures": ["6e8a975e3cbf07d5de823a760d4c2547f86c1403105020adee5de67ac510999e"],
                        "installer": "com.android.vending"},
                "version": {"package": "app.lokke.main", "binary": "1.1.0", "js": "1.1.0"},
                "platform": {"isAndroid": True, "isIOS": False, "isTV": False, "isWeb": False,
                             "isMobile": True, "isWebTV": False, "isElectron": False}
            },
            "appFocusTime": 0,
            "playerActive": False,
            "playDuration": 0,
            "devMode": True,
            "hasAddon": True,
            "castConnected": False,
            "package": "app.lokke.main",
            "version": "1.1.0",
            "process": "app",
            "firstAppStart": now_ms - 86400000,
            "lastAppStart": now_ms,
            "ipLocation": None,
            "adblockEnabled": False,
            "proxy": {"supported": ["ss", "openvpn"], "engine": "openvpn", "ssVersion": 1,
                      "enabled": False, "autoServer": True, "id": "fi-hel"},
            "iap": {"supported": True}
        }
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip",
        }

        for attempt in range(3):
            try:
                async with session.post(_LOKKE_PING_URL, json=body, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sig = data.get("addonSig")
                        if sig:
                            self._cached_sig = sig
                            self._cached_sig_ts = time.time()
                            logger.debug("Got auth signature from lokke.app")
                            return sig
                    logger.warning(f"Ping attempt {attempt+1} failed: status {resp.status}")
            except Exception as e:
                logger.warning(f"Ping attempt {attempt+1} exception: {e}")
        return None

    async def _get_ts_signature(self) -> Optional[str]:
        """Get TS signature via ping2 (fallback)."""
        session = await self._get_session(_TS_PING2_URL)
        for attempt in range(3):
            try:
                async with session.post(
                    _TS_PING2_URL,
                    data={"vec": _TS_VEC},
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    timeout=ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        signed = data.get("response", {}).get("signed")
                        if signed:
                            logger.debug("Got TS signature from ping2")
                            return signed
            except Exception as e:
                logger.warning(f"TS ping2 attempt {attempt+1} exception: {e}")
        return None

    async def _resolve_via_mediahubmx(self, url: str, signature: str) -> Optional[str]:
        """Resolve vavoo play URL via mediahubmx-resolve.json."""
        session = await self._get_session(_RESOLVE_URL)
        headers = {
            "user-agent": "MediaHubMX/2",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip",
            "mediahubmx-signature": signature,
        }
        body = {
            "language": "de",
            "region": "AT",
            "url": url,
            "clientVersion": "3.0.2",
        }
        try:
            async with session.post(_RESOLVE_URL, json=body, headers=headers, timeout=ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    logger.warning(f"Resolve returned status {resp.status}")
                    return None
                data = await resp.json()
                if isinstance(data, list) and data and data[0].get("url"):
                    return str(data[0]["url"])
                if isinstance(data, dict):
                    if data.get("url"):
                        return str(data["url"])
                    if data.get("data", {}).get("url"):
                        return str(data["data"]["url"])
                return None
        except Exception as e:
            logger.warning(f"Resolve exception: {e}")
            return None

    def _build_ts_fallback_url(self, play_url: str, ts_sig: str) -> Optional[str]:
        """Convert vavoo play URL to live2 TS URL with vavoo_auth."""
        import re
        m = re.search(r'/play/([^/?#]+)', play_url)
        if not m:
            return None
        token = m.group(1)
        return f"https://www2.vavoo.to/live2/{token}.ts?n=1&b=5&vavoo_auth={quote_plus(ts_sig)}"

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        if "vavoo.to" not in url:
            raise ExtractorError("Not a valid Vavoo URL")
        
        resolved_url = None
        stream_headers = {}

        # Step 1: Try resolve via lokke.app signature + mediahubmx
        sig = await self._get_auth_signature()
        if sig:
            resolved_url = await self._resolve_via_mediahubmx(url, sig)
            if resolved_url:
                logger.info(f"Resolved via mediahubmx: {resolved_url[:80]}...")
                stream_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://vavoo.to",
                    "Origin": "https://vavoo.to",
                }

        # Step 2: Fallback — TS signature via ping2
        if not resolved_url:
            ts_sig = await self._get_ts_signature()
            if ts_sig:
                ts_url = self._build_ts_fallback_url(url, ts_sig)
                if ts_url:
                    resolved_url = ts_url
                    logger.info(f"Resolved via TS fallback: {resolved_url[:80]}...")
                    stream_headers = {
                        "user-agent": "VAVOO/2.6",
                    }

        # Step 3: Last resort — pass raw URL (may not work without proper player)
        if not resolved_url:
            resolved_url = url
            logger.warning(f"Using Direct Mode (unresolved): {resolved_url}")
            stream_headers = {
                "user-agent": "VAVOO/2.6",
                "referer": "https://vavoo.to/",
            }

        stream_headers["X-EasyProxy-Disable-SSL"] = "1"

        return {
            "destination_url": resolved_url,
            "request_headers": stream_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "disable_ssl": True,
            "bypass_warp": True,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
