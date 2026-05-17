import logging
import random
import re
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from urllib.parse import urlparse, urljoin

from extractors.base import BaseExtractor, ExtractorError
from utils.packed import eval_solver


class FileMoonExtractor(BaseExtractor):
    """FileMoon URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="filemoon")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract FileMoon URL."""
        resp = await self._make_request(url)
        text = resp.text
        response_url = resp.url

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

        session = await self._get_session(iframe_url)
        final_url = await eval_solver(session, iframe_url, headers, patterns)

        # Test if stream exists
        try:
            await self._make_request(final_url, headers=headers)
        except ExtractorError as e:
            if "404" in str(e):
                raise ExtractorError("Stream not found (404)")
            raise

        self.base_headers["referer"] = url

        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
