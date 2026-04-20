import logging
import random
import re
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class VidozaExtractor:
    """Vidoza URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
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

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidoza URL."""
        parsed = urlparse(url)

        # Accept vidoza + videzz
        if not parsed.hostname or not (
            parsed.hostname.endswith("vidoza.net") or parsed.hostname.endswith("videzz.net")
        ):
            raise ExtractorError("VIDOZA: Invalid domain")

        session = await self._get_session(url)
        
        headers = self.base_headers.copy()
        headers.update({
            "referer": "https://vidoza.net/",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
        })

        # 1) Fetch the embed page (or whatever URL you pass in)
        async with session.get(url, headers=headers) as response:
            html = await response.text()
            cookies = {k: v.value for k, v in response.cookies.items()}

        if not html:
            raise ExtractorError("VIDOZA: Empty HTML from Vidoza")

        # 2) Extract final link with REGEX
        pattern = re.compile(
            r"""["']?\s*(?:file|src)\s*["']?\s*[:=,]?\s*["'](?P<url>[^"']+)"""
            r"""(?:[^}>\]]+)["']?\s*res\s*["']?\s*[:=]\s*["']?(?P<label>[^"',]+)""",
            re.IGNORECASE,
        )

        match = pattern.search(html)
        if not match:
            raise ExtractorError("VIDOZA: Unable to extract video + label from JS")

        mp4_url = match.group("url")
        # label = match.group("label").strip()  # available but not used

        # Fix URLs like //str38.vidoza.net/...
        if mp4_url.startswith("//"):
            mp4_url = "https:" + mp4_url

        # 3) Attach cookies (token may depend on these)
        if cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return {
            "destination_url": mp4_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
