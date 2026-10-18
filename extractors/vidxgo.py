"""VidXgo extractor.

The VidXgo CDN (cdn.v1.*.d2b.you) rejects Python HTTP clients (curl_cffi and
the Python tls_client binding included) with HTTP 403, and the player page's
`currentSrc` now points to a stale `/hls/<id>_v2/` path that returns 404. The
site itself refreshes the signed URL via its `/t/<numericId>` token endpoint
and plays the non-`_v2` path.

Extraction therefore runs in a Node.js runner (``scripts/vidxgo_runner.mjs``)
that mirrors the easystreams addon: Firefox 120 TLS fingerprint via the
`tls-client` package, token fetched from `/t/<id>`, then the master playlist
(and variant playlists when a refresh is requested) with the same session.
"""

import asyncio
import json
import logging
import os
import shutil
from typing import Any
from urllib.parse import urlparse

import config as _cfg
from config import get_preferred_proxy_for_url
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "vidxgo_runner.mjs",
)


class VidXgoExtractor(BaseExtractor):
    """VidXgo embed -> HLS extractor (Node tls-client runner)."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vidxgo", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.selected_proxy = None

    @staticmethod
    def _node_bin() -> str | None:
        return shutil.which("node")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host.endswith("vidxgo.co"):
            raise ExtractorError("VidXgo: expected a vidxgo.co embed URL")

    @staticmethod
    def _normalize_proxy(proxy: str | None) -> str:
        if not proxy:
            return ""
        proxy = _cfg.resolve_proxy_alias(str(proxy).strip()) or str(proxy).strip()
        if "://" not in proxy:
            proxy = f"socks5://{proxy}"
        return proxy.replace("socks5h://", "socks5://", 1)

    # Public alias used by the routing-policy tests.
    _normalize_proxy_url = _normalize_proxy

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._validate_url(url)
        node = self._node_bin()
        if not node:
            raise ExtractorError("VidXgo: Node.js is required for TLS-fingerprint extraction")
        if not os.path.exists(_RUNNER):
            raise ExtractorError(f"VidXgo: runner script not found at {_RUNNER}")

        self._apply_routing_kwargs(url, kwargs)
        if self._force_direct:
            proxy = None
        else:
            proxy = _cfg.effective_forced_proxy(url, self._forced_proxy) or await get_preferred_proxy_for_url(
                url, self.extractor_name, self.proxies, self.bypass_warp_active
            )
        if proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError("VidXgo: direct fallback disabled; no proxy route available")
        self.selected_proxy = proxy
        self.last_used_proxy = proxy

        env = dict(os.environ)
        runner_proxy = self._normalize_proxy(proxy)
        if runner_proxy:
            env["VIDXGO_PROXY"] = runner_proxy
        else:
            env.pop("VIDXGO_PROXY", None)
        if kwargs.get("force_refresh") or kwargs.get("background_refresh"):
            env["VIDXGO_CAPTURE_VARIANTS"] = "1"

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                node, _RUNNER, url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=os.name != "nt",
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except FileNotFoundError as exc:
            raise ExtractorError(f"VidXgo: failed to spawn node: {exc}") from exc
        except asyncio.TimeoutError:
            raise ExtractorError("VidXgo: node runner timed out")
        finally:
            if proc and proc.returncode is None:
                try:
                    if os.name != "nt":
                        import signal
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except BaseException:
                    pass

        raw = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        payload = None
        for line in reversed(raw.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break

        if payload is None:
            detail = stderr.decode("utf-8", errors="replace")[-400:] if stderr else raw[-400:]
            raise ExtractorError(f"VidXgo: runner produced no result ({detail})")
        if payload.get("error"):
            raise ExtractorError(f"VidXgo: {payload['error']}")

        destination_url = str(payload.get("destination_url") or "")
        if not destination_url.startswith("http"):
            raise ExtractorError("VidXgo: runner returned no stream URL")

        captured_manifest = payload.get("captured_manifest") or ""
        captured_manifests = payload.get("captured_manifests") or {}
        if captured_manifest:
            captured_manifests.setdefault(destination_url, captured_manifest)

        return {
            "destination_url": destination_url,
            "request_headers": payload.get("request_headers") or {},
            "captured_manifest": captured_manifest,
            "captured_manifests": captured_manifests,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.selected_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
            "disable_ssl": True,
        }

