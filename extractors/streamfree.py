"""streamfree.top player -> strmfree.st signed HLS (pure HTTP).

Flow: ``/player/<cat>/<key>`` -> ``/api/v1/sources/<key>`` -> embed page
``strmfree.st/embed/<cat>/<key><quality>`` (needs the player Referer) -> inline JS
with ``QUALITY``, ``SOURCE_SUFFIX``, slug and the per-quality ``_0x`` token map ->
``strmfree.st/get-stream-key/<slug>`` -> signed ``index.m3u8?_t&_e&_n``.
"""

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)


class StreamFreeExtractor(BaseExtractor):
    """streamfree.top player (strmfree.st signed HLS)."""

    PLAYER_RE = re.compile(r"/player/([^/]+)/([^/?#]+)", re.I)
    EMBED_RE = re.compile(r"/embed/([^/]+)/([^/?#]+)", re.I)
    QUALITY_RE = re.compile(r"QUALITY\s*=\s*['\"]([^'\"]*)['\"]")
    SUFFIX_RE = re.compile(r"SOURCE_SUFFIX\s*=\s*['\"]([^'\"]*)['\"]")
    SLUG_RE = re.compile(r"get-stream-key/([^'\"\s<]+)")
    TOKENS_RE = re.compile(r"const\s+_0x\s*=\s*(\{.*?\});", re.S)
    TOKEN_ENTRY_RE = re.compile(r"[\"']([^\"']+)[\"']\s*:\s*\{([^}]*)\}")
    TOKEN_FIELD_RE = re.compile(r"[\"']?(_[ent])[\"']?\s*:\s*[\"']?([^\"',}\s]+)")
    QUALITY_SUFFIX_RE = re.compile(r"\d{3,4}p$", re.I)

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="streamfree", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    @staticmethod
    def _match1(pattern: re.Pattern, text: str, default: str = "") -> str:
        match = pattern.search(text or "")
        return match.group(1) if match else default

    async def _resolve_embed(self, url: str) -> tuple[str, str]:
        """Input player or embed URL -> (embed_url, player_url)."""
        embed = self.EMBED_RE.search(urlsplit(url).path or "")
        if embed:
            category, variant = embed.group(1), embed.group(2)
            slug = self.QUALITY_SUFFIX_RE.sub("", variant)
            return url, f"https://streamfree.top/player/{category}/{slug}"

        player = self.PLAYER_RE.search(urlsplit(url).path or "")
        if not player:
            raise ExtractorError(f"StreamFree: player path not found in URL: {url}")
        category, key = player.group(1), player.group(2)
        player_url = url
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        resp = await self._make_request(
            f"{origin}/api/v1/sources/{key}",
            headers={"Referer": player_url, "Origin": origin},
            retries=1,
        )
        sources = (resp.json or {}).get("sources") or []
        embed_url = str((sources[0] or {}).get("url") or "").strip() if sources else ""
        if not embed_url:
            raise ExtractorError(f"StreamFree: not live (no sources for /{category}/{key})")
        return embed_url, player_url

    def _tokens(self, page: str) -> dict:
        """Per-quality signed token map from ``const _0x = {...};``."""
        match = self.TOKENS_RE.search(page or "")
        if not match:
            return {}
        raw = match.group(1)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        out = {}
        for quality, block in self.TOKEN_ENTRY_RE.findall(raw):
            fields = dict(self.TOKEN_FIELD_RE.findall(block))
            if fields:
                out[quality] = fields
        return out

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)

        embed_url, player_url = await self._resolve_embed(url)
        parts = urlsplit(embed_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        page_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": player_url,
            "Origin": "https://streamfree.top",
        }
        resp = await self._make_request(embed_url, headers=page_headers, retries=1)
        page = resp.text or ""

        quality = self._match1(self.QUALITY_RE, page)
        if not quality:
            raise ExtractorError(f"StreamFree: QUALITY not found in embed page {embed_url}")
        suffix = self._match1(self.SUFFIX_RE, page)
        slug = self._match1(self.SLUG_RE, page) or self.QUALITY_SUFFIX_RE.sub("", parts.path.rsplit("/", 1)[-1])
        token = self._tokens(page).get(quality) or {}
        if not token.get("_t") or not token.get("_e") or not token.get("_n"):
            raise ExtractorError(f"StreamFree: no signed token for quality {quality} in {embed_url}")

        key_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": embed_url,
            "Origin": origin,
            "Accept": "application/json",
        }
        key_resp = await self._make_request(f"{origin}/get-stream-key/{slug}", headers=key_headers, retries=1)
        info = key_resp.json if isinstance(key_resp.json, dict) else {}
        external = str(info.get("external_url") or "").strip() if info.get("is_external") else ""

        if external.startswith(("http://", "https://")):
            dest = external
        else:
            server = str(info.get("server_name") or "origin")
            base = "/live-externalcdn" if server == "externalcdn" else ("/live-cdn" if server != "origin" else "/live")
            dest = f"{origin}{base}/{slug}{quality}{suffix}/index.m3u8?_t={token['_t']}&_e={token['_e']}&_n={token['_n']}"

        check = await self._make_request(dest, headers=page_headers, retries=1)
        if check.status != 200 or "#EXTM3U" not in (check.text or ""):
            raise ExtractorError(f"StreamFree: signed playlist not a manifest (HTTP {check.status})")

        logger.info("StreamFree: %s -> %s", slug, dest.split("?")[0])
        return self._result(dest, {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": embed_url,
            "Origin": origin,
        })
