import logging
import re
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError
from utils.packed import unpack

logger = logging.getLogger(__name__)


class StreamHGExtractor(BaseExtractor):
    """Extractor for StreamHG-style players (dhcplay/vibuxer mirrors)."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="streamhg")
        self.mediaflow_endpoint = "hls_proxy"

    @staticmethod
    def _candidate_urls(url: str) -> list[str]:
        candidates = [url]
        try:
            parsed = urlparse(url)
            id_match = re.search(r"/e/([^/?#]+)", parsed.path, re.IGNORECASE)
            if id_match and parsed.hostname and parsed.hostname.lower().endswith("dhcplay.com"):
                candidates.append(f"https://vibuxer.com/e/{id_match.group(1)}")
        except Exception:
            pass
        return candidates

    async def _fetch_html(self, url: str, referer: str) -> tuple[str, str]:
        headers = {
            "Referer": referer,
            "User-Agent": self.base_headers["User-Agent"],
        }
        resp = await self._make_request(url, headers=headers)
        return resp.url, resp.text

    @staticmethod
    def _extract_hls_url(html: str, page_url: str) -> str | None:
        packed_match = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+|\[\]),(\d+),'(.*?)'\.split\('\|'\)",
            html,
            re.DOTALL,
        )
        if not packed_match:
            return None

        packed_block = packed_match.group(0)
        unpacked = unpack(packed_block)

        hls2_match = re.search(r'["\']hls2["\']\s*:\s*["\']([^"\']+)["\']', unpacked, re.IGNORECASE)
        hls4_match = re.search(r'["\']hls4["\']\s*:\s*["\']([^"\']+)["\']', unpacked, re.IGNORECASE)
        file_match = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', unpacked, re.IGNORECASE)

        stream_url = None
        if hls2_match:
            stream_url = hls2_match.group(1)
        elif hls4_match:
            stream_url = hls4_match.group(1)
        elif file_match:
            stream_url = file_match.group(1)

        if not stream_url:
            return None
        return urljoin(page_url, stream_url)

    async def extract(self, url: str, **kwargs) -> dict:
        referer = "https://dhcplay.com/"
        for candidate in self._candidate_urls(url):
            try:
                final_url, html = await self._fetch_html(candidate, referer)
                stream_url = self._extract_hls_url(html, final_url)
                if not stream_url:
                    continue

                logger.info("Successfully extracted StreamHG URL: %s...", stream_url[:80])
                return {
                    "destination_url": stream_url,
                    "request_headers": {},
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }
            except Exception as e:
                logger.debug("StreamHG candidate failed %s: %s", candidate, e)
                continue

        raise ExtractorError(f"STREAMHG extraction failed for {url}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
