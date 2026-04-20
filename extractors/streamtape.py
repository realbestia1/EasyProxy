import logging
import re
import random
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class StreamtapeExtractor:
    """Streamtape URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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
        """Extract Streamtape URL."""
        session = await self._get_session(url)
        async with session.get(url) as response:
            text = await response.text()

        # Extract and decode URL
        matches = re.findall(r"id=.*?(?=')", text)
        if not matches:
            raise ExtractorError("Failed to extract URL components")
        
        final_url = None
        for i in range(len(matches)):
            if i > 0 and matches[i-1] == matches[i] and "ip=" in matches[i]:
                final_url = f"https://streamtape.com/get_video?{matches[i]}"
                break
        
        if not final_url:
             # Fallback logic if the specific pattern isn't found exactly as expected
             # Sometimes just taking the last match with 'ip=' works
             for match in matches:
                 if "ip=" in match:
                     final_url = f"https://streamtape.com/get_video?{match}"

        if not final_url:
            raise ExtractorError("Streamtape URL extraction failed")

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()