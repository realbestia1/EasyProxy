import re
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError
from utils.packed import eval_solver


class DroploadExtractor(BaseExtractor):
    """Dropload URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="dropload")

    @staticmethod
    def _extract_m3u8(text: str) -> str | None:
        match = re.search(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', text)
        return match.group(0) if match else None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Dropload URL."""

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Referer": referer,
            "User-Agent": self.base_headers["User-Agent"],
        }

        final_url = None
        try:
            session = await self._get_session(url)
            final_url = await eval_solver(
                session,
                url,
                headers,
                [
                    r'file:"(.*?)"',
                    r'sources:\s*\[\s*\{\s*file:\s*"([^"]+)"',
                    r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
                    r'https?://[^"\'\s]+\.mp4[^"\'\s]*',
                ],
            )
        except Exception:
            final_url = None

        if not final_url:
            resp = await self._make_request(url, headers=headers)
            html = resp.text
            final_url = self._extract_m3u8(html)
            if not final_url:
                mp4_match = re.search(r'https?://[^"\'\s]+\.mp4[^"\'\s]*', html)
                if mp4_match:
                    final_url = mp4_match.group(0)

        if not final_url:
            raise ExtractorError("Dropload extraction failed: no media URL found")

        self.base_headers["referer"] = url
        self.base_headers["origin"] = referer.rstrip("/")
        mediaflow_endpoint = "proxy_stream_endpoint" if ".mp4" in final_url else self.mediaflow_endpoint

        return {
            "destination_url": urljoin(url, final_url),
            "request_headers": self.base_headers,
            "mediaflow_endpoint": mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
