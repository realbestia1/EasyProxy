import logging
import random
import re
from urllib.parse import urljoin, urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class TurboVidPlayExtractor:
    """TurboVidPlay URL extractor."""

    domains = [
        "turboviplay.com",
        "emturbovid.com",
        "tuborstb.co",
        "javggvideo.xyz",
        "stbturbo.xyz",
        "turbovidhls.com",
    ]

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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

    def _get_origin(self, url: str) -> str:
        """Get origin from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _extract_playlist_url(text: str, base_url: str | None = None) -> str | None:
        """Extract an HLS playlist URL from either a manifest or inline script response."""
        patterns = [
            r'https?://[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
            r'//[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
            r'/[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue

            candidate = match.group(0)
            if candidate.startswith("//"):
                return f"https:{candidate}"
            if base_url and candidate.startswith("/"):
                return urljoin(base_url, candidate)
            return candidate
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract TurboVidPlay URL."""
        session = await self._get_session(url)
        
        # 1. Load embed
        async with session.get(url) as response:
            html = await response.text()
            response_url = str(response.url)

        # 2. Extract urlPlay or data-hash
        m = re.search(r"(?:urlPlay|data-hash)\s*=\s*['\"]([^'\"]+)", html)
        if not m:
            raise ExtractorError("TurboViPlay: No media URL found")

        media_url = m.group(1)

        # Normalize protocol
        origin = self._get_origin(response_url)
        if media_url.startswith("//"):
            media_url = "https:" + media_url
        elif media_url.startswith("/"):
            media_url = origin + media_url

        # 3. Fetch the intermediate playlist
        async with session.get(media_url, headers={"Referer": url}) as data_resp:
            playlist = await data_resp.text()
            playlist_url = str(data_resp.url)

        # 4. Extract real m3u8 URL
        real_m3u8 = self._extract_playlist_url(playlist, playlist_url)
        if not real_m3u8:
            if ".m3u8" in playlist_url:
                real_m3u8 = playlist_url
            elif ".m3u8" in media_url:
                real_m3u8 = media_url
        if not real_m3u8:
            raise ExtractorError("TurboViPlay: Unable to extract playlist URL")

        # 5. Final headers
        self.base_headers.update({"referer": url, "origin": origin})

        return {
            "destination_url": real_m3u8,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
