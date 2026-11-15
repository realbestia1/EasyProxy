"""TimStreams extractor (tim-streams.com channels/events).

The watch pages point at an ``ehgembxe.cfd/<slug>`` player that redirects to a
``/play/<JWT>`` page. Its inline, obfuscated scripts fetch an encrypted payload
and hand the signed HLS URL to ``Hls.loadSource()``; a Go anti-sandbox probe in
``/plum.js`` guards the whole thing, so a regex/HTTP extraction is not possible.
``scripts/timstreams_runner.mjs`` replays that page JS in a headless Node vm
(no browser) and returns the manifest URL, verified from the same process.
"""

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from extractors.base import BaseExtractor, ExtractorError
from extractors.node_runner import run_node_runner
from services.socks_bridge import get_http_bridge_for_proxy

logger = logging.getLogger(__name__)

RESULT_CACHE_TTL = 60  # i reload del manifest non devono rieffettuare l'estrazione
_CACHE_MAX = 50
# Cache a livello di modulo: proxy_extractor crea/ritira un estrattore per richiesta.
_RESULT_CACHE: dict[str, tuple[float, dict]] = {}

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "timstreams_runner.mjs",
)

_RUNNER_ATTEMPTS = 3
_RUNNER_RETRY_DELAY = 1.5
# The player host is flaky (ECONNRESET / rate-limit / unreadable manifest): those
# are worth a retry, a bad slug or a 404 is not.
_RETRYABLE = (
    "page fetch failed",
    "timeout",
    "manifest not readable",
    "econnreset",
    "connect timeout",
    "fetch failed",
)


class TimStreamsExtractor(BaseExtractor):
    """tim-streams.com / ehgembxe player (Node WASM-less sandbox)."""

    SLUG_RE = re.compile(r"/(?:watch|channel)/([^/?#]+)", re.IGNORECASE)

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="timstreams", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    def _request_header(self, name: str, default: str = "") -> str:
        """Case-insensitive lookup on the caller-supplied request headers."""
        wanted = name.lower()
        for key, value in (self.request_headers or {}).items():
            if str(key).lower() == wanted and value:
                return str(value)
        return default

    async def _resolve_player_url(self, url: str) -> str:
        """tim-streams.com page URL -> ehgembxe player URL via the site API."""
        if "tim-streams.com" not in (urlparse(url).netloc or "").lower():
            return url
        match = self.SLUG_RE.search(urlparse(url).path)
        if not match:
            raise ExtractorError("TimStreams: channel/event slug not found in URL")
        slug = match.group(1)
        api = f"https://tim-streams.com/api/watch/{slug}"
        headers = {
            "User-Agent": self._request_header("User-Agent", self.base_headers["User-Agent"]),
            "Accept": "application/json",
            "Referer": url,
        }
        resp = await self._make_request(api, headers=headers, retries=3)
        payload = resp.json if resp.status == 200 else {}
        streams = ((payload.get("item") or {}).get("streams") or []) if isinstance(payload, dict) else []
        player_url = str((streams[0] or {}).get("url") or "").strip() if streams else ""
        if not player_url.startswith(("http://", "https://")):
            raise ExtractorError(f"TimStreams: no player available for {slug} (HTTP {resp.status})")
        logger.info("TimStreams: %s -> %s", slug, player_url[:80])
        return player_url

    async def _run_player(self, player_url: str, env: dict) -> dict:
        """Run the player runner, retrying the transient player-host failures."""
        for attempt in range(1, _RUNNER_ATTEMPTS + 1):
            try:
                return await run_node_runner(_RUNNER, [player_url], env, timeout=45, label="TimStreams")
            except ExtractorError as exc:
                if attempt == _RUNNER_ATTEMPTS or not any(m in str(exc).lower() for m in _RETRYABLE):
                    raise
                logger.warning("TimStreams: attempt %d/%d failed (%s), retrying",
                               attempt, _RUNNER_ATTEMPTS, exc)
                await asyncio.sleep(_RUNNER_RETRY_DELAY)
        raise ExtractorError("TimStreams: runner attempts exhausted")

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        route_key = f"{self.bypass_warp_active}:{self._force_direct}:{self._forced_proxy}"
        cache_key = f"{url.strip()}:{route_key}"
        force = bool(kwargs.get("force_refresh")) or str(kwargs.get("force", "")).lower() == "true"

        cached = None if force else _RESULT_CACHE.get(cache_key)
        if cached and cached[0] > time.time():
            logger.debug("TimStreams: cache hit for %s", cache_key)
            return dict(cached[1])
        logger.debug("TimStreams: cache miss for %s", cache_key)

        player_url = await self._resolve_player_url(url)

        proxy = await self._resolve_proxy(player_url)
        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(f"TimStreams: failed to create HTTP bridge for proxy ({proxy})")

        env = dict(os.environ)
        if runner_proxy:
            env["TIMSTREAMS_PROXY"] = str(runner_proxy)
        else:
            env.pop("TIMSTREAMS_PROXY", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["TIMSTREAMS_DEBUG"] = "1"

        data = await self._run_player(player_url, env)
        m3u8 = str(data.get("m3u8") or "").strip()
        if not m3u8:
            raise ExtractorError("TimStreams: video not found (no m3u8 captured)")

        parsed = urlparse(player_url)
        headers = {
            "User-Agent": self._request_header("User-Agent", self.base_headers["User-Agent"]),
            "Referer": player_url,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
        }

        logger.info("TimStreams: extracted %s", m3u8[:100])
        result = self._result(m3u8, headers)
        _RESULT_CACHE[cache_key] = (time.time() + RESULT_CACHE_TTL, dict(result))
        if len(_RESULT_CACHE) > _CACHE_MAX:
            _RESULT_CACHE.pop(min(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0]), None)
        return result
