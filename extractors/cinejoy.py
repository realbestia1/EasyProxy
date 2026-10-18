"""Cinejoy page -> unlocked HLS extractor.

Cinejoy gates its stream resolution behind a client-side WebAssembly module
(crush.wasm) and AES-GCM encryption. The extractor resolves streams via the
headless runner in ``scripts/cinejoy_runner.mjs``.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any
from urllib.parse import urlparse, parse_qs

import config as _cfg
from extractors.base import BaseExtractor, ExtractorError
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

    @staticmethod
    def _node_bin() -> str | None:
        return shutil.which("node")

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
        node = self._node_bin()
        if not node:
            raise ExtractorError("Cinejoy: Node.js is required for headless extraction")
        if not os.path.exists(_RUNNER):
            raise ExtractorError(f"Cinejoy: runner script not found at {_RUNNER}")

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
                proxy = await _cfg.get_preferred_proxy_for_url(
                    url, self.extractor_name, [] if bypass_proxies else self.proxies, bypass_warp=bypass_warp
                )
            finally:
                if token is not None:
                    _cfg.BYPASS_PROXIES_CONTEXT.reset(token)

        if proxy and bypass_warp and _cfg.is_warp_proxy_url(proxy):
            proxy = None

        if proxy is None and not (direct_requested or _cfg.is_direct_connection_allowed(bypass_warp)):
            raise ExtractorError(
                "Cinejoy: direct fallback disabled; no proxy route available"
            )

        runner_proxy = await get_http_bridge_for_proxy(proxy)
        if proxy and not runner_proxy:
            raise ExtractorError(
                f"Cinejoy: failed to create HTTP bridge for proxy ({proxy})"
            )
        self.last_used_proxy = proxy
        logger.info(
            "Cinejoy routing: proxy=%s warp_off=%s direct=%s",
            proxy or "DIRECT",
            bypass_warp,
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

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                node,
                _RUNNER,
                normalized,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=os.name != "nt",
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
        except FileNotFoundError as exc:
            raise ExtractorError(f"Cinejoy: failed to start Node.js: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ExtractorError("Cinejoy: resolver timed out after 45 seconds") from exc
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

        if not payload:
            detail = stderr.decode("utf-8", errors="replace")[-800:] if stderr else raw[-800:]
            raise ExtractorError(f"Cinejoy: resolver returned no JSON ({detail or proc.returncode})")
        if proc.returncode != 0 or payload.get("error"):
            error = str(payload.get("error") or "resolver failed")
            raise ExtractorError(f"Cinejoy: {error}")

        stream_url = str(payload.get("url") or "").strip()
        if not stream_url.startswith(("http://", "https://")):
            raise ExtractorError("Cinejoy: resolver returned no playable URL")

        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        headers.setdefault("Referer", "https://cinejoy.to/")
        headers.setdefault("Origin", "https://cinejoy.to")

        logger.info("Cinejoy: extracted stream %s", stream_url[:100])
        return {
            "destination_url": stream_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": direct_requested,
            "bypass_warp": bypass_warp,
        }

