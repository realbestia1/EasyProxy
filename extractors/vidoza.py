import re
from urllib.parse import urlparse

from extractors.base import BaseExtractor, ExtractorError


class VidozaExtractor(BaseExtractor):
    """Vidoza URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="vidoza")
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidoza URL."""
        parsed = urlparse(url)

        # Accept vidoza + videzz
        if not parsed.hostname or not (
                parsed.hostname.endswith("vidoza.net") or parsed.hostname.endswith("videzz.net")
        ):
            raise ExtractorError("VIDOZA: Invalid domain")

        headers = self.base_headers.copy()
        headers.update({
            "referer": "https://vidoza.net/",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
        })

        # 1) Fetch the embed page
        resp = await self._make_request(url, headers=headers)
        html = resp.text
        cookies = {k: v.value for k, v in resp.cookies.items()}

        if not html:
            raise ExtractorError("VIDOZA: Empty HTML from Vidoza")

        # 2) Extract final link with REGEX
        pattern = re.compile(
            r"""["']?\s*(?:file|src)\s*["']?\s*[:=,]?\s*["'](?P<url>[^"']+)"""
            r"""(?:[^}>\]]+)["']?\s*res\s*["']?\s*[:=]\s*["']?(?P<label>[^"',]+)""",
            re.IGNORECASE,
        )

        match = pattern.search(html)
        if not match:
            raise ExtractorError("VIDOZA: Unable to extract video + label from JS")

        mp4_url = match.group("url")
        # label = match.group("label").strip()  # available but not used

        # Fix URLs like //str38.vidoza.net/...
        if mp4_url.startswith("//"):
            mp4_url = "https:" + mp4_url

        # 3) Attach cookies (token may depend on these)
        if cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return {
            "destination_url": mp4_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
