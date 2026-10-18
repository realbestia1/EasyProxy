"""VidFast page -> unlocked HLS extractor.

VidFast keeps the playback URL behind its own player bundle.  The extractor
uses the site's decoder in ``scripts/vidfast_runner.mjs``; it does not open a
visible browser window.
"""

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

from config import get_preferred_proxy_for_url
import config as _cfg
from extractors.base import ExtractorError
from extractors.node_runner import run_node_runner
from services.socks_bridge import get_http_bridge_for_proxy

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "vidfast_runner.mjs",
)
_MOVIE_RE = re.compile(r"^/movie/\d+/?$", re.IGNORECASE)
_TV_RE = re.compile(r"^/tv/\d+/\d+/\d+/?$", re.IGNORECASE)


class VidFastExtractor:
    """Resolve ``vidfast.vc`` movie/TV pages to a playable HLS URL."""

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.proxies = proxies or []
        self.extractor_name = "vidfast"
        self.mediaflow_endpoint = "hls_proxy"
        self._init_bypass_warp = bool(bypass_warp)
        self.bypass_warp_active = bool(bypass_warp)
        self._last_computed_bypass_warp = self.bypass_warp_active
        self.last_used_proxy = None

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"vidfast.vc", "www.vidfast.vc"}:
            raise ExtractorError("VidFast: expected a vidfast.vc movie or TV URL")
        if not (_MOVIE_RE.fullmatch(parsed.path) or _TV_RE.fullmatch(parsed.path)):
            raise ExtractorError("VidFast: unsupported URL; expected /movie/<id> or /tv/<id>/<season>/<episode>")

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._validate_url(url)

        if self.bypass_warp_active != getattr(self, "_last_computed_bypass_warp", self._init_bypass_warp):
            self._init_bypass_warp = bool(self.bypass_warp_active)
        raw_proxy = kwargs.get("proxy")
        raw_proxy_norm = str(raw_proxy or "").strip().lower()
        bypass_proxies = (
            raw_proxy_norm in {"off", "false", "0", "direct", "none", "no"}
            or _cfg.BYPASS_PROXIES_CONTEXT.get()
        )
        direct_flag = (
            str(kwargs.get("direct", "")).strip().lower() in {"1", "true", "yes", "on"}
            or raw_proxy_norm == "direct"
        )
        bypass_warp = bool(
            kwargs.get("bypass_warp")
            or str(kwargs.get("warp", "")).strip().lower() in {"off", "false", "0"}
            or direct_flag
            or _cfg.BYPASS_WARP_CONTEXT.get()
            or self._init_bypass_warp
        )
        self.bypass_warp_active = bypass_warp
        self._last_computed_bypass_warp = bypass_warp
        direct_requested = direct_flag or (bypass_proxies and bypass_warp)
        if bypass_proxies or raw_proxy_norm in {"off", "false", "0", "direct", "none", "no", "on", "auto", "true", ""}:
            forced_proxy = None
        else:
            resolved = _cfg.resolve_proxy_alias(str(raw_proxy).strip())
            if resolved and "://" not in resolved:
                resolved = f"socks5h://{resolved}"
            forced_proxy = str(resolved)
        forced_proxy = _cfg.effective_forced_proxy(url, forced_proxy)

        if direct_requested and (bypass_proxies or not forced_proxy):
            proxy = None
        elif forced_proxy:
            proxy = str(forced_proxy)
        else:
            token = _cfg.BYPASS_PROXIES_CONTEXT.set(True) if bypass_proxies else None
            try:
                proxy = await get_preferred_proxy_for_url(
                    url, self.extractor_name, [] if bypass_proxies else self.proxies, bypass_warp=bypass_warp
                )
            finally:
                if token is not None:
                    _cfg.BYPASS_PROXIES_CONTEXT.reset(token)

        if proxy and bypass_warp and _cfg.is_warp_proxy_url(proxy):
            proxy = None

        if proxy is None and not (direct_requested or _cfg.is_direct_connection_allowed(bypass_warp)):
            raise ExtractorError(
                "VidFast: direct fallback disabled; no proxy route available"
            )

        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(
                f"VidFast: failed to create HTTP bridge for proxy ({proxy})"
            )
        # The HTTP bridge is only for the Node runner. Return the original
        # route for media requests so later HLS segments do not depend on a
        # short-lived localhost bridge port.
        self.last_used_proxy = proxy

        env = dict(os.environ)
        # Node's native fetch supports HTTP(S) ProxyAgent when undici is
        # available.  SOCKS/WARP remains handled by Python-side media proxying.
        if runner_proxy:
            env["VIDFAST_PROXY"] = str(runner_proxy)
        else:
            env.pop("VIDFAST_PROXY", None)
        try:
            required_resolution = int(kwargs.get("required_resolution") or 0)
        except (TypeError, ValueError):
            required_resolution = 0
        if required_resolution > 0:
            env["VIDFAST_MIN_HEIGHT"] = str(required_resolution)
        else:
            env.pop("VIDFAST_MIN_HEIGHT", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["VIDFAST_DEBUG"] = "1"

        payload = await run_node_runner(_RUNNER, [url], env,
                                      timeout=120, label="VidFast")

        stream_url = str(payload.get("url") or "").strip()
        if not stream_url.startswith(("http://", "https://")):
            raise ExtractorError("VidFast: resolver returned no playable URL")

        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        logger.info("VidFast: extracted %s via %s", stream_url[:100], payload.get("server", "server"))
        return {
            "destination_url": stream_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": direct_requested,
            "bypass_warp": bypass_warp,
        }

