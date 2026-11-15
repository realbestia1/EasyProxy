"""streamcorner.* -> HLS extractor.

The signed embed URL is only released to a Web Worker on the site's own origin
(anti-tamper token), so it is resolved through Chrome: see
``services/streamcorner_cdp.py``. Playlist and segments are plain HTTP on an
open CDN, so no bridge is needed.
"""

import logging
import urllib.parse
from typing import Any

from extractors.base import BaseExtractor, ExtractorError
from services.streamcorner_cdp import resolve_stream

logger = logging.getLogger(__name__)


def _parse_target(url: str) -> tuple[str, str]:
    """``/stream/<provider>/<id>`` -> (provider, id); else last segment."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    if not parts:
        raise ExtractorError("Streamcorner: missing channel id in URL")
    if len(parts) >= 3 and parts[0].lower() == "stream":
        return parts[1], parts[2]
    return "channels", parts[-1]


class StreamcornerExtractor(BaseExtractor):
    """Resolve ``streamcorner.foo/stream/<provider>/<id>`` pages to HLS."""

    # Worker handshake + embed player bootstrap are slow.
    REQUEST_TIMEOUT_TOTAL = 90

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="streamcorner", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        provider, content_id = _parse_target(url)

        m3u8 = await resolve_stream(provider, content_id)
        if not m3u8:
            raise ExtractorError("Streamcorner: browser resolver failed")

        headers = {"User-Agent": self.base_headers["User-Agent"]}
        logger.info("Streamcorner extracted: %s", m3u8[:90])
        return self._result(m3u8, headers)
