# https://github.com/Gujal00/ResolveURL/blob/55c7f66524ebd65bc1f88650614e627b00167fa0/script.module.resolveurl/lib/resolveurl/plugins/f16px.py

import base64
import json
import logging
import random
import re
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy
from utils import python_aesgcm

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class F16PxExtractor:
    """F16Px URL extractor with AES-GCM decryption support."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0"
        }
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or []

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies) if url else self._get_random_proxy()
            if proxy:
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=60, enable_cleanup_closed=True, force_close=False, use_dns_cache=True)
            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["user-agent"]})
        return self.session

    @staticmethod
    def _b64url_decode(value: str) -> bytes:
        """Decode base64url to bytes."""
        # base64url -> base64
        value = value.replace("-", "+").replace("_", "/")
        padding = (-len(value)) % 4
        if padding:
            value += "=" * padding
        return base64.b64decode(value)

    def _join_key_parts(self, parts) -> bytes:
        """Join multiple base64url-encoded key parts into a single key."""
        return b"".join(self._b64url_decode(p) for p in parts)

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract F16Px URL."""
        parsed = urlparse(url)
        host = parsed.netloc
        origin = f"{parsed.scheme}://{parsed.netloc}"

        match = re.search(r"/e/([A-Za-z0-9]+)", parsed.path or "")
        if not match:
            raise ExtractorError("F16PX: Invalid embed URL")

        media_id = match.group(1)
        api_url = f"https://{host}/api/videos/{media_id}/playback"

        session = await self._get_session()
        
        headers = self.base_headers.copy()
        headers["referer"] = f"https://{host}/"

        async with session.get(api_url, headers=headers) as resp:
            try:
                data = await resp.json()
            except Exception:
                raise ExtractorError("F16PX: Invalid JSON response")

        # Case 1: plain sources
        if "sources" in data and data["sources"]:
            src = data["sources"][0].get("url")
            if not src:
                raise ExtractorError("F16PX: Empty source URL")
            return {
                "destination_url": src,
                "request_headers": headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        # Case 2: encrypted playback
        pb = data.get("playback")
        if not pb:
            raise ExtractorError("F16PX: No playback data")

        try:
            iv = self._b64url_decode(pb["iv"])  # nonce
            key = self._join_key_parts(pb["key_parts"])  # AES key
            payload = self._b64url_decode(pb["payload"])  # ciphertext + tag

            cipher = python_aesgcm.new(key)
            decrypted = cipher.open(iv, payload)  # AAD = '' like ResolveURL

            if decrypted is None:
                raise ExtractorError("F16PX: GCM authentication failed")

            decrypted_json = json.loads(decrypted.decode("utf-8", "ignore"))

        except ExtractorError:
            raise
        except Exception as e:
            raise ExtractorError(f"F16PX: Decryption failed ({e})")

        sources = decrypted_json.get("sources") or []
        if not sources:
            raise ExtractorError("F16PX: No sources after decryption")

        best = sources[0].get("url")
        if not best:
            raise ExtractorError("F16PX: Empty source URL after decryption")

        self.base_headers.clear()
        self.base_headers["referer"] = f"{origin}/"
        self.base_headers["origin"] = origin
        self.base_headers["Accept-Language"] = "en-US,en;q=0.5"
        self.base_headers["Accept"] = "*/*"
        self.base_headers["user-agent"] = "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0"

        return {
            "destination_url": best,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
