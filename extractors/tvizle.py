"""TVizle extractor (tvizle.tr live channels).

The channel page is Next.js: the streams live in the RSC payload inside the
HTML, JSON-escaped (\\" and \\u0026). The site's own /api/yayin endpoint is a
public HLS proxy (no Referer/cookie needed), so it is preferred over the
upstream CDN URLs, which can be geo-blocked.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)


class TVizleExtractor(BaseExtractor):
    """Extractor for tvizle.tr live channels (site-proxied HLS playlists)."""

    SLUG_RE = re.compile(r"/izle/([A-Za-z0-9_-]+)", re.IGNORECASE)
    URL_FIELD_RE = re.compile(r'"url"\s*:\s*"([^"]+)"', re.IGNORECASE)

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="tvizle", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    def _request_header(self, name: str, default: str = "") -> str:
        """Case-insensitive lookup on the caller-supplied request headers."""
        wanted = name.lower()
        for key, value in (self.request_headers or {}).items():
            if str(key).lower() == wanted and value:
                return str(value)
        return default

    @staticmethod
    def _unescape(raw: str) -> str:
        return raw.replace("\\u0026", "&").replace("\\/", "/").replace('\\"', '"')

    def _pick_stream(self, html: str, origin: str) -> str | None:
        flat = self._unescape(html or "")
        urls = [m.group(1).strip() for m in self.URL_FIELD_RE.finditer(flat)]
        # The site proxy endpoint works regardless of upstream geoblocks.
        for candidate in urls:
            if "/api/yayin" in candidate:
                return urljoin(origin, candidate)
        for candidate in urls:
            if ".m3u8" in candidate.lower():
                return urljoin(origin, candidate)
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        self._apply_routing_kwargs(url, kwargs)
        parsed = urlparse(url)
        match = self.SLUG_RE.search(parsed.path)
        if not match:
            raise ExtractorError("TVizle: channel slug not found in URL")
        slug = match.group(1)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        user_agent = self._request_header("User-Agent", self.base_headers["User-Agent"])

        page_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        }
        candidate = None
        try:
            resp = await self._make_request(url, headers=page_headers, retries=1)
            if resp.status == 200:
                candidate = self._pick_stream(resp.text, origin)
        except Exception as exc:
            logger.debug("TVizle: page fetch failed (%s), using /api/yayin fallback", exc)

        if not candidate:
            candidate = f"{origin}/api/yayin?c={slug}&i=0"

        playback_headers = {
            "User-Agent": user_agent,
            "Referer": f"{origin}/",
            "Accept": "*/*",
        }
        try:
            resp = await self._make_request(candidate, headers=playback_headers, retries=1)
            body = resp.text if resp.status == 200 else ""
            status = resp.status
        except Exception as exc:
            raise ExtractorError(f"TVizle: playlist request failed: {exc}")
        if "#EXTM3U" not in body[:4000]:
            raise ExtractorError(f"TVizle: playlist not available (HTTP {status})")

        logger.info("TVizle: resolved %s -> %s", slug, candidate)
        return self._result(candidate, playback_headers)
