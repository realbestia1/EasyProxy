import re

from extractors.base import BaseExtractor, ExtractorError


class StreamtapeExtractor(BaseExtractor):
    """Streamtape URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="streamtape")
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Streamtape URL."""
        resp = await self._make_request(url)
        text = resp.text

        # Extract and decode URL
        matches = re.findall(r"id=.*?(?=')", text)
        if not matches:
            raise ExtractorError("Failed to extract URL components")

        final_url = None
        for i in range(len(matches)):
            if i > 0 and matches[i - 1] == matches[i] and "ip=" in matches[i]:
                final_url = f"https://streamtape.com/get_video?{matches[i]}"
                break

        if not final_url:
            # Fallback logic if the specific pattern isn't found exactly as expected
            # Sometimes just taking the last match with 'ip=' works
            for match in matches:
                if "ip=" in match:
                    final_url = f"https://streamtape.com/get_video?{match}"

        if not final_url:
            raise ExtractorError("Streamtape URL extraction failed")

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
