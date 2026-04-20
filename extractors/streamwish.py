import logging
import random
import re
from urllib.parse import urljoin, urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class StreamWishExtractor:
    """StreamWish URL extractor."""

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

    @staticmethod
    def _extract_m3u8(text: str) -> str | None:
        """Extract first absolute m3u8 URL from text"""
        match = re.search(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', text)
        return match.group(0) if match else None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract StreamWish URL."""
        session = await self._get_session(url)
        
        referer = self.base_headers.get("Referer")
        if not referer:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"

        headers = {"Referer": referer}
        
        async with session.get(url, headers=headers) as response:
            text = await response.text()

        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.DOTALL)
        iframe_url = urljoin(url, iframe_match.group(1)) if iframe_match else url

        async with session.get(iframe_url, headers=headers) as iframe_response:
            html = await iframe_response.text()

        final_url = self._extract_m3u8(html)

        if not final_url and "eval(function(p,a,c,k,e,d)" in html:
            try:
                final_url = await eval_solver(
                    session,
                    iframe_url,
                    headers,
                    [
                        # absolute m3u8
                        r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)',
                        # relative stream paths
                        r'(\/stream\/[^"\'\s]+\.m3u8[^"\'\s]*)',
                    ],
                )
            except Exception:
                final_url = None

        if not final_url:
            raise ExtractorError("StreamWish: Failed to extract m3u8")

        if final_url.startswith("/"):
            final_url = urljoin(iframe_url, final_url)

        origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        self.base_headers.update({
            "Referer": referer,
            "Origin": origin,
        })

        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
