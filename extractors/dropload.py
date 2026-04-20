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


class DroploadExtractor:
    """Dropload URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
                connector = TCPConnector(
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=60,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True,
                )
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": self.base_headers["user-agent"]},
            )
        return self.session

    @staticmethod
    def _extract_m3u8(text: str) -> str | None:
        match = re.search(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', text)
        return match.group(0) if match else None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Dropload URL."""
        session = await self._get_session(url)

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Referer": referer,
            "User-Agent": self.base_headers["user-agent"],
        }

        final_url = None
        try:
            final_url = await eval_solver(
                session,
                url,
                headers,
                [
                    r'file:"(.*?)"',
                    r'sources:\s*\[\s*\{\s*file:\s*"([^"]+)"',
                    r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
                    r'https?://[^"\'\s]+\.mp4[^"\'\s]*',
                ],
            )
        except Exception:
            final_url = None

        if not final_url:
            async with session.get(url, headers=headers) as response:
                html = await response.text()

            final_url = self._extract_m3u8(html)
            if not final_url:
                mp4_match = re.search(r'https?://[^"\'\s]+\.mp4[^"\'\s]*', html)
                if mp4_match:
                    final_url = mp4_match.group(0)

        if not final_url:
            raise ExtractorError("Dropload extraction failed: no media URL found")

        self.base_headers["referer"] = url
        self.base_headers["origin"] = referer.rstrip("/")
        mediaflow_endpoint = "proxy_stream_endpoint" if ".mp4" in final_url else self.mediaflow_endpoint

        return {
            "destination_url": urljoin(url, final_url),
            "request_headers": self.base_headers,
            "mediaflow_endpoint": mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
