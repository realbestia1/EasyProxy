"""Cinejoy page -> unlocked HLS extractor.

Cinejoy gates its stream resolution behind a client-side WebAssembly module
(crush.wasm) and AES-GCM encryption. The extractor resolves streams via the
headless runner in ``scripts/cinejoy_runner.mjs``.
"""

import json
import logging

from aiohttp import ClientConnectionError
import os
import re
from typing import Any
from urllib.parse import urlparse, parse_qs

from extractors.base import BaseExtractor, ExtractorError
from extractors.node_runner import run_node_runner
from services.socks_bridge import get_http_bridge_for_proxy

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "cinejoy_runner.mjs",
)

_CINEJOY_HOST_PATTERN = re.compile(r"^(?:www\.)?cinejoy\.[a-z]{2,}$")


class CinejoyExtractor(BaseExtractor):
    """Resolve ``cinejoy.to`` movie/TV pages to a playable HLS URL."""

    curl_only = True
    REQUEST_TIMEOUT_TOTAL = 60

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="cinejoy", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._init_bypass_warp = bool(bypass_warp)
        self.bypass_warp_active = bool(bypass_warp)
        self._last_computed_bypass_warp = self.bypass_warp_active
        self.last_used_proxy = None

    @classmethod
    def _validate_and_normalize(cls, url: str, **kwargs) -> str:
        trimmed = (url or "").strip()
        if not trimmed:
            raise ExtractorError("Cinejoy: URL cannot be empty")

        if trimmed.startswith("{"):
            return trimmed

        if re.fullmatch(r"\d+", trimmed):
            # Pure ID, assume movie unless season/episode given in kwargs
            s = kwargs.get("season") or kwargs.get("s")
            e = kwargs.get("episode") or kwargs.get("e")
            if s and e:
                return json.dumps({"type": "tv", "tmdbId": int(trimmed), "season": int(s), "episode": int(e)})
            return json.dumps({"type": "movie", "tmdbId": int(trimmed)})

        parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
        host = (parsed.hostname or "").lower()
        if host and not _CINEJOY_HOST_PATTERN.fullmatch(host):
            raise ExtractorError(f"Cinejoy: expected cinejoy.* URL, got {host}")

        # If season/episode passed as kwargs, merge with URL query
        qs = parse_qs(parsed.query)
        s = kwargs.get("season") or kwargs.get("s") or (qs.get("season", [None])[0]) or (qs.get("s", [None])[0])
        e = kwargs.get("episode") or kwargs.get("e") or (qs.get("episode", [None])[0]) or (qs.get("e", [None])[0])

        path = parsed.path.rstrip("/")
        if s and e and not re.search(r"/\d+/\d+/?$", path):
            # Append season and episode if needed
            id_match = re.search(r"/(\d+)(?:-[^/]+)?/?$", path)
            if id_match:
                tmdb_id = int(id_match.group(1))
                return json.dumps({"type": "tv", "tmdbId": tmdb_id, "season": int(s), "episode": int(e)})

        return trimmed

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        normalized = self._validate_and_normalize(url, **kwargs)

        self._apply_routing_kwargs(url, kwargs)
        try:
            proxy = await self._resolve_proxy(url)
        except ClientConnectionError as exc:
            raise ExtractorError(
                "Cinejoy: direct fallback disabled; no proxy route available") from exc
        self.last_used_proxy = proxy
        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(
                f"Cinejoy: failed to create HTTP bridge for proxy ({proxy})")
        logger.info(
            "Cinejoy routing: proxy=%s warp_off=%s direct=%s",
            proxy or "DIRECT",
            self.bypass_warp_active,
            proxy is None,
        )

        env = dict(os.environ)
        if runner_proxy:
            env["CINEJOY_PROXY"] = str(runner_proxy)
        else:
            env.pop("CINEJOY_PROXY", None)
        try:
            required_resolution = int(kwargs.get("required_resolution") or 0)
        except (TypeError, ValueError):
            required_resolution = 0
        if required_resolution >= 2160:
            env["CINEJOY_REQUIRE_4K"] = "1"
        else:
            env.pop("CINEJOY_REQUIRE_4K", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["CINEJOY_DEBUG"] = "1"

        payload = await run_node_runner(_RUNNER, [normalized], env,
                                      timeout=45, label="Cinejoy")

        stream_url = str(payload.get("url") or "").strip()
        if not stream_url.startswith(("http://", "https://")):
            raise ExtractorError("Cinejoy: resolver returned no playable URL")

        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        headers.setdefault("Referer", "https://cinejoy.to/")
        headers.setdefault("Origin", "https://cinejoy.to")

        logger.info("Cinejoy: extracted stream %s", stream_url[:100])
        return self._result(stream_url, headers)

