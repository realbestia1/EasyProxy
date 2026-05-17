import re
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError


class VidmolyExtractor(BaseExtractor):
    """Vidmoly URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="vidmoly")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidmoly URL."""
        parsed = urlparse(url)
        if not parsed.hostname or "vidmoly" not in parsed.hostname:
            raise ExtractorError("VIDMOLY: Invalid domain")

        # Extract embed ID from URL path, e.g. /embed-qu2swicnn9j6.html -> qu2swicnn9j6
        embed_id_match = re.search(r'/embed-([a-zA-Z0-9]+)\.html', parsed.path)
        if not embed_id_match:
            raise ExtractorError("VIDMOLY: Could not extract embed ID from URL")
        embed_id = embed_id_match.group(1)

        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Cookie": f"cf_turnstile_demo_pass_{embed_id}=1",
            "Referer": url,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }

        # --- Fetch embed page ---
        resp = await self._make_request(url, headers=headers)
        html = resp.text

        # --- Extract master m3u8 ---
        match = re.search(r'sources\s*:\s*\[\s*\{\s*file\s*:\s*[\'"]([^\'"]+)', html)
        if not match:
            raise ExtractorError("VIDMOLY: Stream URL not found")

        master_url = match.group(1)
        if not master_url.startswith("http"):
            master_url = urljoin(url, master_url)

        # --- Validate stream ---
        try:
            await self._make_request(master_url, headers=headers)
        except ExtractorError as e:
            raise ExtractorError(f"VIDMOLY: Stream unavailable or timed out: {e}")

        return {
            "destination_url": master_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
