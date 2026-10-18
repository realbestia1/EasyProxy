import asyncio
import re
from extractors.base import BaseExtractor, ExtractorError

class StreamtapeExtractor(BaseExtractor):
    """Streamtape URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="streamtape", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    @staticmethod
    def _parse_stream_url(text: str) -> str | None:
        matches = re.findall(r"id=.*?(?=')", text)
        for i in range(len(matches)):
            if i > 0 and matches[i - 1] == matches[i] and "ip=" in matches[i]:
                return f"https://stape.me/get_video?{matches[i]}"
        for match in matches:
            if "ip=" in match:
                return f"https://stape.me/get_video?{match}"
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Streamtape URL."""
        self._apply_routing_kwargs(url, kwargs)
        final_url = None
        # Streamtape intermittently serves a bot-check page without the link.
        for attempt in range(3):
            resp = await self._make_request(url)
            final_url = self._parse_stream_url(resp.text)
            if final_url:
                break
            if attempt < 2:
                await asyncio.sleep(1)
        if not final_url:
            raise ExtractorError("Streamtape URL extraction failed")

        self.base_headers["referer"] = url
        return self._result(final_url, self.base_headers)

