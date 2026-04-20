import logging
import random
import re
from urllib.parse import urlparse, urljoin
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class FileMoonExtractor:
    """FileMoon URL extractor."""

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

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract FileMoon URL."""
        session = await self._get_session(url)
        
        async with session.get(url) as response:
            text = await response.text()
            response_url = str(response.url)

        pattern = r'iframe.*?src=["\']([^"\']*)["\']'
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            raise ExtractorError("Failed to extract iframe URL")

        iframe_url = match.group(1)

        parsed = urlparse(response_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        if iframe_url.startswith("//"):
            iframe_url = f"{parsed.scheme}:{iframe_url}"
        elif not urlparse(iframe_url).scheme:
            iframe_url = urljoin(base_url, iframe_url)

        headers = {"Referer": url}
        patterns = [r'file:"(.*?)"']

        final_url = await eval_solver(session, iframe_url, headers, patterns)

        # Test if stream exists
        async with session.get(final_url, headers=headers) as test_resp:
            if test_resp.status == 404:
                raise ExtractorError("Stream not found (404)")

        self.base_headers["referer"] = url

        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
