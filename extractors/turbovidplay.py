import re
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError


class TurboVidPlayExtractor(BaseExtractor):
    """TurboVidPlay URL extractor."""

    domains = [
        "turboviplay.com",
        "emturbovid.com",
        "tuborstb.co",
        "javggvideo.xyz",
        "stbturbo.xyz",
        "turbovidhls.com",
    ]

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="turbovidplay")

    def _get_origin(self, url: str) -> str:
        """Get origin from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _extract_playlist_url(text: str, base_url: str | None = None) -> str | None:
        """Extract an HLS playlist URL from either a manifest or inline script response."""
        patterns = [
            r'https?://[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
            r'//[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
            r'/[^\'"\s]+\.m3u8(?:\?[^\'"\s]*)?',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue

            candidate = match.group(0)
            if candidate.startswith("//"):
                return f"https:{candidate}"
            if base_url and candidate.startswith("/"):
                return urljoin(base_url, candidate)
            return candidate
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract TurboVidPlay URL."""
        # 1. Load embed
        resp = await self._make_request(url)
        html = resp.text
        response_url = resp.url

        # 2. Extract urlPlay or data-hash
        m = re.search(r"(?:urlPlay|data-hash)\s*=\s*['\"]([^'\"]+)", html)
        if not m:
            raise ExtractorError("TurboViPlay: No media URL found")

        media_url = m.group(1)

        # Normalize protocol
        origin = self._get_origin(response_url)
        if media_url.startswith("//"):
            media_url = "https:" + media_url
        elif media_url.startswith("/"):
            media_url = origin + media_url

        # 3. Fetch the intermediate playlist
        resp_data = await self._make_request(media_url, headers={"Referer": url})
        playlist = resp_data.text
        playlist_url = resp_data.url

        # 4. Extract real m3u8 URL
        real_m3u8 = self._extract_playlist_url(playlist, playlist_url)
        if not real_m3u8:
            if ".m3u8" in playlist_url:
                real_m3u8 = playlist_url
            elif ".m3u8" in media_url:
                real_m3u8 = media_url
        if not real_m3u8:
            raise ExtractorError("TurboViPlay: Unable to extract playlist URL")

        # 5. Final headers
        self.base_headers.update({"referer": url, "origin": origin})

        return {
            "destination_url": real_m3u8,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
