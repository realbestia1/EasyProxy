import re

from extractors.base import BaseExtractor, ExtractorError


class LuluStreamExtractor(BaseExtractor):
    """LuluStream URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="lulustream")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract LuluStream URL."""
        resp = await self._make_request(url)
        text = resp.text

        # See https://github.com/Gujal00/ResolveURL/blob/master/script.module.resolveurl/lib/resolveurl/plugins/lulustream.py
        pattern = r"""sources:\s*\[{file:\s*["'](?P<url>[^"']+)"""
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            raise ExtractorError("Failed to extract source URL")

        final_url = match.group(1)

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
