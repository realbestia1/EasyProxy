import logging
import random
import re
from urllib.parse import urljoin
from aiohttp import ClientSession, ClientTimeout, TCPConnector
try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class UqloadExtractor:
    """Uqload URL extractor."""

    # Full browser-like headers required to bypass Cloudflare/bot checks on uqload
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    # Regex patterns tried in order — first the exact mediaflow pattern, then flexible fallbacks
    SOURCE_PATTERNS = [
        r'sources: \["(.*?)"\]',                              # mediaflow exact — works on most uqload pages
        r'sources\s*:\s*\[\s*["\']([^"\']+)["\']',          # flexible spacing/quotes variant
        r'"?sources"?\s*:\s*\[\s*["\']([^"\']+)["\']',      # with optional quotes on key
        r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',      # fallback: file: "...mp4..."
        r'src\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',       # src: "...mp4..."
        r'video_url\s*=\s*["\']([^"\']+)["\']',              # var video_url = "..."
        r'player\.src\s*\(\s*["\']([^"\']+)["\']',           # player.src("...")
        r'(?:https?://[a-z0-9.-]*uqload[a-z.]*)/[a-z0-9/]+\.mp4[^"\'<\s]*',  # raw mp4 URL on page
    ]

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": self.BROWSER_HEADERS["User-Agent"]
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
                connector = TCPConnector(
                    limit=0, limit_per_host=0,
                    keepalive_timeout=60,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True,
                )
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self.session

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Uqload video URL.

        Sends full browser headers to avoid Cloudflare/bot-protection blocks
        and tries multiple regex patterns for resilience across uqload domains
        (.io / .is / .com / .to).
        """
        session = await self._get_session(url)
        logger.info(f"[Uqload] Fetching embed page: {url}")

        async with session.get(url, headers=self.BROWSER_HEADERS, allow_redirects=True) as response:
            final_url = str(response.url)
            if response.status not in (200, 206):
                raise ExtractorError(
                    f"Uqload page returned HTTP {response.status} for {url}"
                )
            text = await response.text(errors="replace")

        logger.info(f"[Uqload] Page length: {len(text)} chars, final URL: {final_url}")

        # Check for common error pages
        if "file was deleted" in text.lower() or "file not found" in text.lower() or "not found" in text.lower():
            raise ExtractorError(f"Uqload video removed/not found: {url}")

        video_url = None
        for i, pattern in enumerate(self.SOURCE_PATTERNS):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                video_url = m.group(1).strip() if m.lastindex else m.group(0).strip()
                logger.info(f"[Uqload] Pattern #{i} matched: {video_url[:80]}...")
                break

        if not video_url:
            # Log more context to help debug
            logger.warning(f"[Uqload] No pattern matched for {url}")
            logger.warning(f"[Uqload] Page title: {re.search(r'<title>(.*?)</title>', text, re.I)}")
            logger.warning(f"[Uqload] Page snippet (first 500): {text[:500]!r}")
            # Also log any script blocks that might contain the video URL
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL | re.IGNORECASE)
            for idx, script in enumerate(scripts):
                if 'source' in script.lower() or 'file' in script.lower() or '.mp4' in script.lower():
                    logger.warning(f"[Uqload] Relevant script #{idx}: {script[:300]!r}")
            raise ExtractorError(f"Failed to extract video URL from uqload page: {url}")

        origin = urljoin(url, "/")
        return {
            "destination_url": video_url,
            "request_headers": {
                "user-agent": self.BROWSER_HEADERS["User-Agent"],
                "referer": origin,
                "origin": origin,
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
