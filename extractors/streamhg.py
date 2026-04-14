import logging
import random
import re
from urllib.parse import urljoin, urlparse

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

from utils.packed import unpack

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


class StreamHGExtractor:
    """Extractor for StreamHG-style players (dhcplay/vibuxer mirrors)."""

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

    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = self._get_random_proxy()
            if proxy:
                connector = ProxyConnector.from_url(proxy)
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
    def _candidate_urls(url: str) -> list[str]:
        candidates = [url]
        try:
            parsed = urlparse(url)
            id_match = re.search(r"/e/([^/?#]+)", parsed.path, re.IGNORECASE)
            if id_match and parsed.hostname and parsed.hostname.lower().endswith("dhcplay.com"):
                candidates.append(f"https://vibuxer.com/e/{id_match.group(1)}")
        except Exception:
            pass
        return candidates

    async def _fetch_html(self, url: str, referer: str) -> tuple[str, str]:
        session = await self._get_session()
        headers = {
            "Referer": referer,
            "User-Agent": self.base_headers["user-agent"],
        }
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                raise ExtractorError(f"STREAMHG: HTTP {response.status} for {url}")
            return str(response.url), await response.text()

    @staticmethod
    def _extract_hls_url(html: str, page_url: str) -> str | None:
        packed_match = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+|\[\]),(\d+),'(.*?)'\.split\('\|'\)",
            html,
            re.DOTALL,
        )
        if not packed_match:
            return None

        packed_block = packed_match.group(0)
        unpacked = unpack(packed_block)

        hls2_match = re.search(r'["\']hls2["\']\s*:\s*["\']([^"\']+)["\']', unpacked, re.IGNORECASE)
        hls4_match = re.search(r'["\']hls4["\']\s*:\s*["\']([^"\']+)["\']', unpacked, re.IGNORECASE)
        file_match = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', unpacked, re.IGNORECASE)

        stream_url = None
        if hls2_match:
            stream_url = hls2_match.group(1)
        elif hls4_match:
            stream_url = hls4_match.group(1)
        elif file_match:
            stream_url = file_match.group(1)

        if not stream_url:
            return None
        return urljoin(page_url, stream_url)

    async def extract(self, url: str, **kwargs) -> dict:
        referer = "https://dhcplay.com/"
        for candidate in self._candidate_urls(url):
            try:
                final_url, html = await self._fetch_html(candidate, referer)
                stream_url = self._extract_hls_url(html, final_url)
                if not stream_url:
                    continue

                logger.info(f"Successfully extracted StreamHG URL: {stream_url[:80]}...")
                return {
                    "destination_url": stream_url,
                    "request_headers": {},
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }
            except Exception as e:
                logger.debug(f"StreamHG candidate failed {candidate}: {e}")
                continue

        raise ExtractorError(f"STREAMHG extraction failed for {url}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
