import aiohttp
import asyncio
import logging
import random
from aiohttp import ClientSession, ClientTimeout, TCPConnector, ClientConnectionError
from aiohttp_socks import ProxyError as AioProxyError
from python_socks import ProxyError as PyProxyError

from config import (
    get_proxy_for_url,
    TRANSPORT_ROUTES,
    get_connector_for_proxy,
    SELECTED_PROXY_CONTEXT,
    GLOBAL_PROXIES,
    mark_proxy_dead
)

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


class BaseExtractor:
    """Base class for extractors with robust networking and proxy fallback."""

    def __init__(self, request_headers: dict, proxies: list = None, extractor_name: str = "generic"):
        self.request_headers = request_headers
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or GLOBAL_PROXIES
        self.extractor_name = extractor_name

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)

            proxy = None
            if url:
                proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            elif self.proxies:
                proxy = random.choice(self.proxies)

            if proxy:
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=60,
                    enable_cleanup_closed=True,
                    use_dns_cache=True
                )

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={'User-Agent': self.base_headers["User-Agent"]}
            )
        return self.session

    async def _make_request(self, url: str, method: str = "GET", headers: dict = None, retries: int = 2, **kwargs):
        """Perform a robust request with proxy fallback."""
        final_headers = headers or {}
        if "User-Agent" not in final_headers:
            final_headers["User-Agent"] = self.base_headers["User-Agent"]

        for attempt in range(retries):
            try:
                session = await self._get_session(url)
                async with session.request(method, url, headers=final_headers, allow_redirects=True,
                                           **kwargs) as response:
                    response.raise_for_status()

                    # ✅ NUOVO: Protezione contro file binari giganti
                    content_type = response.headers.get("Content-Type", "").lower()
                    content_length = int(response.headers.get("Content-Length", 0))

                    if "video/" in content_type or "audio/" in content_type or content_length > 2 * 1024 * 1024:
                        logger.warning("[%s] Skipping text read for binary/large content: %s (%d bytes)",
                                       self.extractor_name, content_type, content_length)
                        # Restituisci un MockResponse "vuoto" o che indica il bypass
                        return MockResponse("", response.status, response.headers, str(response.url), response.cookies)

                    content = await response.text()

                    class MockResponse:
                        def __init__(self, text, status, headers, url, cookies):
                            self.text = text
                            self.status = status
                            self.headers = headers
                            self.url = url
                            self.cookies = cookies

                        @property
                        def json(self):
                            import json
                            try:
                                return json.loads(self.text)
                            except Exception:
                                return {}

                    return MockResponse(content, response.status, response.headers, str(response.url), response.cookies)
            except (AioProxyError, PyProxyError, asyncio.TimeoutError, ClientConnectionError,
                    aiohttp.ClientResponseError) as e:
                is_proxy_err = isinstance(e, (AioProxyError, PyProxyError))
                is_timeout = isinstance(e, asyncio.TimeoutError)

                # Check for 403 or network errors to trigger fallback
                status = getattr(e, 'status', None)
                logger.warning("[%s] Attempt %d failed for %s: %s", self.extractor_name, attempt + 1, url, e)

                # Reset session
                if self.session and not self.session.closed:
                    await self.session.close()
                self.session = None

                if is_proxy_err and SELECTED_PROXY_CONTEXT.get():
                    proxy_to_mark = SELECTED_PROXY_CONTEXT.get()
                    if proxy_to_mark and "127.0.0.1" in proxy_to_mark:
                        mark_proxy_dead(proxy_to_mark)
                    SELECTED_PROXY_CONTEXT.set(None)

                if attempt < retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise ExtractorError(f"Request failed after {retries} attempts: {e}")

        raise ExtractorError(f"Request failed for {url}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
