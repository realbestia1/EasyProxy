"""ntv.cx / ntvs.cx -> HLS extractor.

ntv.cx is the same provider as ppv.st: its API hands out the final embed URL on
the shared embed host (``taifood-blog.asia``), which already contains the stream
path. The heavy lifting stays in ``scripts/ppv_runner.mjs`` (embed ``/fetch`` ->
wasm decrypt -> ``*.indianservers.st/secure/.../index.m3u8``), which accepts that
embed URL directly.
"""

import logging
import os
import re
import urllib.parse
from typing import Any

from aiohttp import ClientTimeout

from extractors.base import BaseExtractor, ExtractorError
from extractors.node_runner import run_node_runner
from services.ppv_bridge import bridge_manifest_url, ensure_bridge
from services.socks_bridge import get_http_bridge_for_proxy

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "ppv_runner.mjs",
)

_API = "https://ntv.cx"
_SERVERS = ("raptor", "falcon", "phoenix", "titan")
_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
_STREAM_IFRAME_RE = re.compile(r'<iframe[^>]*id="streamIframe"[^>]*src="([^"]+)"', re.IGNORECASE)
_ANY_IFRAME_RE = re.compile(r'<iframe[^>]*src="([^"]+)"', re.IGNORECASE)


class NTVExtractor(BaseExtractor):
    """Resolve ``ntv.cx/watch/<server>/<match>`` pages to a playable HLS URL."""

    # wasm decrypt + API lookups are slow (the runner alone allows ~30s).
    REQUEST_TIMEOUT_TOTAL = 90

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="ntv", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, int]:
        """``/watch/<server>/<match>?source=<i>`` -> (server, match id, source)."""
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        tokens = [token for token in parsed.path.split("/") if token]
        if tokens and tokens[0].lower() == "watch":
            tokens = tokens[1:]

        server = (query.get("server") or [""])[0].strip().lower()
        match_id = ""
        for token in tokens:
            if not server and token.lower() in _SERVERS:
                server = token.lower()
            elif not match_id:
                match_id = token
        if not match_id:
            raise ExtractorError("NTV: match id not found in URL")

        try:
            source = int((query.get("source") or ["0"])[0])
        except ValueError:
            source = 0
        return server or _SERVERS[0], match_id, max(source, 0)

    async def _get(self, session, url: str, json: bool = False):
        async with session.get(url, headers=_API_HEADERS, timeout=ClientTimeout(total=20)) as res:
            if res.status != 200:
                raise ExtractorError(f"NTV: HTTP {res.status} for {url}")
            return await (res.json() if json else res.text())

    async def _embed_url(self, session, server: str, match_id: str, source: int) -> str:
        """Source url from the match list, with the watch-streams fallback."""
        data = await self._get(session, f"{_API}/api/get-matches?server={server}", json=True)
        matches = (data or {}).get("all") or []
        match = next((item for item in matches if str(item.get("id")) == match_id), None)
        sources = (match or {}).get("sources") or []
        if sources:
            picked = sources[source] if 0 <= source < len(sources) else sources[0]
            if picked.get("url"):
                return picked["url"]

        data = await self._get(
            session,
            f"{_API}/api/get-watch-streams?server={server}&match={match_id}&source={source}",
            json=True,
        )
        embed = (data or {}).get("embedUrl")
        if not embed:
            raise ExtractorError(f"NTV: no source for match {match_id}")
        if embed.startswith("//"):
            embed = f"https:{embed}"
        elif embed.startswith("/"):
            embed = f"{_API}{embed}"

        page = await self._get(session, embed)
        found = _STREAM_IFRAME_RE.search(page) or _ANY_IFRAME_RE.search(page)
        if not found:
            raise ExtractorError(f"NTV: no stream iframe for match {match_id}")
        return found.group(1)

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        proxy = await self._resolve_proxy(url)
        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(f"NTV: failed to create HTTP bridge for proxy ({proxy})")

        server, match_id, source = self._parse_url(url)
        session = await self._get_session(url)
        embed_url = await self._embed_url(session, server, match_id, source)

        env = dict(os.environ)
        if runner_proxy:
            env["PPV_PROXY"] = str(runner_proxy)
        else:
            env.pop("PPV_PROXY", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["PPV_DEBUG"] = "1"

        data = await run_node_runner(_RUNNER, [embed_url], env, timeout=60, label="NTV")

        m3u8 = data.get("m3u8")
        embed_origin = data.get("embedOrigin")
        embed_path = data.get("embedPath")
        if not m3u8 or not embed_origin or not embed_path:
            raise ExtractorError("NTV: resolver returned no playable URL")

        # The CDN playlist/segments want the embed page as referer.
        referer = f"{embed_origin}/embed/{embed_path}"
        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": referer,
            "Origin": embed_origin,
        }

        # The CDN 403s every non-Node TLS stack, so playback goes through the
        # local Node bridge that rewrites playlists and serves the segments.
        try:
            base = await ensure_bridge()
            destination = bridge_manifest_url(base, m3u8, referer)
        except Exception as exc:
            logger.warning("NTV: bridge unavailable (%s), returning CDN url", exc)
            destination = m3u8

        logger.info("NTV extracted: %s -> %s", match_id, m3u8[:90])
        return self._result(destination, headers)
