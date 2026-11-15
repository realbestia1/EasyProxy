"""ppv.st -> HLS extractor.

ppv.st serves its channels from an embed host (``taifood-blog.asia``) whose
player decrypts the CDN URL with a wasm module (``gasm.wasm``). The work is
done by the headless runner in ``scripts/ppv_runner.mjs``: ppv API -> embed
``/fetch`` -> wasm decrypt -> ``*.indianservers.st/secure/.../index.m3u8``.
"""

import logging
import os
from typing import Any

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


class PPVExtractor(BaseExtractor):
    """Resolve ``ppv.st/live/<uri>`` pages to a playable HLS URL."""

    # wasm decrypt + API failover are slow (the runner alone allows ~30s).
    REQUEST_TIMEOUT_TOTAL = 90

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="ppv", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_manifest_proxy"

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        proxy = await self._resolve_proxy(url)
        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(f"PPV: failed to create HTTP bridge for proxy ({proxy})")

        env = dict(os.environ)
        if runner_proxy:
            env["PPV_PROXY"] = str(runner_proxy)
        else:
            env.pop("PPV_PROXY", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["PPV_DEBUG"] = "1"

        data = await run_node_runner(_RUNNER, [url], env, timeout=60, label="PPV")

        m3u8 = data.get("m3u8")
        embed_origin = data.get("embedOrigin")
        embed_path = data.get("embedPath")
        if not m3u8 or not embed_origin or not embed_path:
            raise ExtractorError("PPV: resolver returned no playable URL")

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
            logger.warning("PPV: bridge unavailable (%s), returning CDN url", exc)
            destination = m3u8

        logger.info("PPV extracted: %s", m3u8[:90])
        return self._result(destination, headers)
