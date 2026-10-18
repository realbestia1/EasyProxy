"""
VidSonic extractor.

The embed page at vidsonic.net/e/<code> hides the master HLS URL inside an
obfuscated hex string. The decoder (reverse-engineered from the inline page
script) is trivial:

    s = "6436643363|3566373062|..."          # hex pairs separated by '|'
    clean = s.split('|').join('')            # concat hex pairs
    out   = ''.join(chr(int(clean[i:i+2], 16)) for i in range(0, len(clean), 2))
    url   = out[::-1]                        # reverse -> "https://.../master.m3u8?..."

The CDN serves the manifest without any Referer/Origin enforcement, so no
special playback headers are required.
"""

import base64
import json
import logging
import re

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

# New player (vixeo.io) embeds the source in a base64 JSON data-config attribute:
# <div id="streamsonic-player-root" data-config="eyJzb3VyY2UiOi...">
_CONFIG_DATA_RE = re.compile(r'data-config="([^"]+)"')

# Matches the obfuscated hex-pipe string literal assigned in the page script.
# Each '|'-separated chunk is a run of hex digits (variable length); the decoder
# concatenates them and parses 2-char hex pairs. We require a long blob to
# avoid false positives from unrelated hex-looking strings.
_HEX_PIPE_RE = re.compile(
    r"['\"]([0-9a-fA-F]+(?:\|[0-9a-fA-F]+){15,})['\"]"
)

# Fallback: locate the decoder function and grab the literal it receives.
_DECODE_FN_RE = re.compile(
    r"split\(['\"]\|['\"]\)\.join\(['\"]['\"]\)[\s\S]{0,200}?"
    r"parseInt\([\w.]+\.substr\(\s*\w+\s*,\s*2\s*\),\s*16\s*\)[\s\S]{0,200}?"
    r"reverse\(\)",
    re.IGNORECASE,
)


class VidSonicExtractor(BaseExtractor):
    """VidSonic embed -> HLS extractor."""

    REQUEST_TIMEOUT_TOTAL = 10

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vidsonic", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_proxy"

    @staticmethod
    def _decode(s: str) -> str:
        """Reproduce the page decoder: concat hex pairs -> bytes -> reverse."""
        clean = s.replace("|", "")
        if len(clean) % 2:
            raise ExtractorError("VidSonic: odd-length hex blob")
        try:
            raw = bytes(int(clean[i:i + 2], 16) for i in range(0, len(clean), 2))
        except ValueError as e:
            raise ExtractorError(f"VidSonic: invalid hex ({e})")
        return raw.decode("utf-8", errors="ignore")[::-1]

    @classmethod
    def _find_blob(cls, html: str) -> str | None:
        m = _HEX_PIPE_RE.search(html)
        if m:
            return m.group(1)
        return None

    @classmethod
    def _source_from_config(cls, html: str) -> str | None:
        """Read ``source`` from the new StreamSonic player config attribute."""
        m = _CONFIG_DATA_RE.search(html)
        if not m:
            return None
        try:
            config = json.loads(base64.b64decode(m.group(1)))
        except Exception:
            return None
        source = config.get("source")
        return source if isinstance(source, str) and source else None

    async def extract(self, url: str, **kwargs) -> dict:
        self._apply_routing_kwargs(url, kwargs)
        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": url,
        }

        resp = await self._make_request(url, headers=headers, retries=2)
        html = resp.text or ""

        blob = self._source_from_config(html) or self._find_blob(html)
        if not blob:
            # Last resort: make sure a decoder is actually present before
            # trying other literals.
            if not _DECODE_FN_RE.search(html):
                raise ExtractorError(f"VidSonic: no obfuscated URL in {url}")
            # Pick the longest hex-pipe-looking literal as a heuristic.
            candidates = re.findall(r"['\"]([0-9a-fA-F]{2}(?:\|[0-9a-fA-F]{2})+)['\"]", html)
            if not candidates:
                raise ExtractorError(f"VidSonic: no obfuscated URL in {url}")
            blob = max(candidates, key=len)

        stream_url = blob if blob.startswith("http") else self._decode(blob)
        if "m3u8" not in stream_url and "mp4" not in stream_url:
            raise ExtractorError(f"VidSonic: decoded value is not a media URL: {stream_url[:80]}")

        logger.info(f"VidSonic: extracted -> {stream_url[:80]}...")
        return self._result(stream_url, {})

