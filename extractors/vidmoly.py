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

class VidmolyExtractor:
    """Vidmoly URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
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

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidmoly URL."""
        parsed = urlparse(url)
        if not parsed.hostname or "vidmoly" not in parsed.hostname:
            raise ExtractorError("VIDMOLY: Invalid domain")

        session = await self._get_session(url)
        
        headers = {
            "User-Agent": self.base_headers["user-agent"],
            "Referer": url,
            "Sec-Fetch-Dest": "iframe",
        }

        # --- Fetch embed page ---
        async with session.get(url, headers=headers) as response:
            html = await response.text()

        # --- Extract master m3u8 ---
        match = re.search(r'sources:\s*\[{file:"([^"]+)', html)
        if not match:
            raise ExtractorError("VIDMOLY: Stream URL not found")

        master_url = match.group(1)

        if not master_url.startswith("http"):
            master_url = urljoin(url, master_url)

        # --- Validate stream (prevents Stremio timeout) ---
        try:
            async with session.get(master_url, headers=headers) as test:
                if test.status >= 400:
                    raise ExtractorError(f"VIDMOLY: Stream unavailable ({test.status})")
        except Exception as e:
            if "timeout" in str(e).lower():
                raise ExtractorError("VIDMOLY: Request timed out")
            raise

        # Return MASTER playlist, not variant
        # Let MediaFlow Proxy handle variants
        return {
            "destination_url": master_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
