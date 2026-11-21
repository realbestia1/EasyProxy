"""VidXgo extractor.

The VidXgo page resolves to a signed HLS URL through its ``/t/<id>`` token
endpoint. Both the token endpoint and the CDN playlist are fetched in-process
with curl_cffi browser impersonation, sharing one session so the signed URL
stays bound to the client that asked for it.
"""

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import config as _cfg
from extractors.base import BaseExtractor, ExtractorError, RateLimitError

logger = logging.getLogger(__name__)


def _curl_session_cls():
    """Lazy import: services.proxy_shared imports the extractor registry."""
    from services.proxy_shared import get_curl_async_session

    return get_curl_async_session()

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
TOKEN_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://v.vidxgo.co/",
    "Origin": "https://v.vidxgo.co",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
PLAY_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://v.vidxgo.co/",
    "Origin": "https://v.vidxgo.co",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}
ORIGIN = "https://v.vidxgo.co"
MAX_PLAYLISTS = 16

# /t/<id> is rate limited per egress IP and the signed URL it returns expires
# after ~3 minutes. Players reload the extractor URL every few seconds, so keep
# a successful extraction briefly and collapse concurrent lookups into one
# upstream call (the extractor instance is retired after each request).
RESULT_TTL = 90.0
RATE_LIMIT_COOLDOWN = 15.0
MAX_RATE_LIMIT_COOLDOWN = 60.0
MAX_CACHE_ENTRIES = 128

_result_cache: dict[tuple, tuple[float, float, dict]] = {}
_cooldowns: dict[tuple, float] = {}
_locks: dict[tuple, tuple[Any, asyncio.Lock]] = {}


def _lock_for(key: tuple) -> asyncio.Lock:
    """One lock per cache key, rebuilt if the event loop changed."""
    loop = asyncio.get_running_loop()
    entry = _locks.get(key)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _locks[key] = entry
    return entry[1]


def _prune(now: float) -> None:
    if len(_result_cache) > MAX_CACHE_ENTRIES:
        for key, (expires_at, _created, _result) in list(_result_cache.items()):
            if expires_at <= now:
                _result_cache.pop(key, None)
        while len(_result_cache) > MAX_CACHE_ENTRIES:
            _result_cache.pop(next(iter(_result_cache)))
    if len(_cooldowns) > MAX_CACHE_ENTRIES:
        for key, until in list(_cooldowns.items()):
            if until <= now:
                _cooldowns.pop(key, None)
    if len(_locks) > MAX_CACHE_ENTRIES:
        for key, (_loop, lock) in list(_locks.items()):
            if key not in _result_cache and not lock.locked():
                _locks.pop(key, None)


class VidXgoExtractor(BaseExtractor):
    """VidXgo embed -> HLS extractor (in-process TLS impersonation)."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vidxgo", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.selected_proxy = None

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
        if proxy.startswith("socks5://") and _cfg.is_managed_tunnel_proxy_url(proxy):
            proxy = "socks5h://" + proxy[len("socks5://") :]
        return proxy

    @staticmethod
    def _token_endpoint(embed_url: str) -> str:
        parsed = urlparse(embed_url)
        parts = [part for part in parsed.path.split("/") if part]
        numeric_id = (parts[0] if parts else "").lower().removeprefix("tt")
        if not numeric_id.isdigit():
            raise ExtractorError(f"VidXgo: cannot find a numeric id in {parsed.path}")
        season = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        episode = parts[2] if len(parts) > 2 and parts[2].isdigit() else None
        suffix = f"/{season}/{episode}" if season and episode else ""
        return f"{ORIGIN}/t/{numeric_id}{suffix}"

    async def _collect_variants(self, session, master_url, master_text, captured, proxies):
        seen = {master_url}
        pending = [(master_url, master_text)]
        while pending and len(seen) < MAX_PLAYLISTS:
            parent_url, manifest = pending.pop(0)
            variant_next = False
            for raw_line in (manifest or "").splitlines():
                line = raw_line.strip()
                child = None
                if line.startswith("#EXT-X-STREAM-INF:"):
                    variant_next = True
                    continue
                if line.startswith("#EXT-X-MEDIA:") and 'URI="' in line:
                    match = re.search(r'URI="([^"]+)"', line)
                    child = match.group(1) if match else None
                elif line and not line.startswith("#"):
                    if variant_next:
                        child = line
                    variant_next = False
                if not child:
                    continue
                child_url = urljoin(parent_url, child)
                if child_url in seen or len(seen) >= MAX_PLAYLISTS:
                    continue
                seen.add(child_url)
                try:
                    res = await session.get(
                        child_url,
                        headers=PLAY_HEADERS,
                        proxies=proxies,
                        verify=False,
                        timeout=20,
                        allow_redirects=True,
                    )
                except Exception:
                    continue
                if res.status_code == 200 and "#EXTM3U" in res.text:
                    captured[child_url] = res.text
                    pending.append((child_url, res.text))

    @staticmethod
    def _retry_after(response, default: float = RATE_LIMIT_COOLDOWN) -> float:
        raw = (getattr(response, "headers", None) or {}).get("Retry-After")
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return default
        return min(max(seconds, 1.0), MAX_RATE_LIMIT_COOLDOWN)

    async def _fetch_result(self, token_url: str, proxies, cooldown_key: tuple, deep: bool):
        """Ask /t/<id> for a signed URL and read the master playlist."""
        session_cls = _curl_session_cls()
        if session_cls is None:
            raise ExtractorError("VidXgo: curl_cffi is required")
        session = session_cls(impersonate="firefox133", **(_cfg.get_curl_ipv4_options(None) or {}))
        expires_at = time.monotonic() + RESULT_TTL
        try:
            token_response = await session.get(
                token_url,
                headers=TOKEN_HEADERS,
                proxies=proxies,
                verify=False,
                timeout=25,
                allow_redirects=True,
            )
            if token_response.status_code == 429:
                cooldown = self._retry_after(token_response)
                _cooldowns[cooldown_key] = time.monotonic() + cooldown
                logger.warning(
                    "VidXgo: token endpoint rate limited, backing off %.0fs", cooldown
                )
                raise RateLimitError(
                    f"VidXgo: rate limited (HTTP 429), retry in {int(cooldown)}s",
                    retry_after=cooldown,
                )
            if token_response.status_code != 200:
                raise ExtractorError(
                    f"VidXgo: token endpoint returned HTTP {token_response.status_code}"
                )
            try:
                payload = token_response.json()
            except Exception as exc:
                raise ExtractorError("VidXgo: token endpoint did not return JSON") from exc
            stream_url = str((payload or {}).get("url") or "").strip()
            if not stream_url.startswith("http"):
                raise ExtractorError("VidXgo: token endpoint returned no stream URL")

            # The payload advertises the signed URL expiry (epoch ms): never
            # reuse it past that, and cap it with our own TTL.
            try:
                expires_in = float(payload.get("expire")) / 1000 - time.time()
            except (TypeError, ValueError, AttributeError):
                expires_in = RESULT_TTL
            expires_at = time.monotonic() + min(RESULT_TTL, max(5.0, expires_in))

            master = await session.get(
                stream_url,
                headers=PLAY_HEADERS,
                proxies=proxies,
                verify=False,
                timeout=25,
                allow_redirects=True,
            )
            if master.status_code != 200 or "#EXTM3U" not in master.text:
                raise ExtractorError(
                    f"VidXgo: CDN returned HTTP {master.status_code} for the master playlist"
                )

            captured_manifests = {stream_url: master.text}
            if deep:
                await self._collect_variants(
                    session, stream_url, master.text, captured_manifests, proxies
                )
        finally:
            await session.close()

        return {
            "destination_url": stream_url,
            "captured_manifest": master.text,
            "captured_manifests": captured_manifests,
        }, expires_at

    def _with_routing(self, data: dict) -> dict[str, Any]:
        """Attach per-request routing state; it must never come from the cache."""
        return {
            "destination_url": data["destination_url"],
            "request_headers": dict(PLAY_HEADERS),
            "captured_manifest": data["captured_manifest"],
            "captured_manifests": data["captured_manifests"],
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.selected_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
            "disable_ssl": True,
        }

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._validate_url(url)

        self._apply_routing_kwargs(url, kwargs)
        proxy = await self._resolve_proxy(url)
        self.selected_proxy = proxy

        runner_proxy = self._normalize_proxy(proxy)
        proxies = {"http": runner_proxy, "https": runner_proxy} if runner_proxy else None
        force_refresh = bool(kwargs.get("force_refresh"))
        deep = bool(force_refresh or kwargs.get("background_refresh"))

        token_url = self._token_endpoint(url)
        cooldown_key = (token_url, runner_proxy)
        cache_key = (token_url, runner_proxy, deep)
        started = time.monotonic()

        def cached_result():
            entry = _result_cache.get(cache_key)
            now = time.monotonic()
            if not entry or entry[0] <= now:
                return None
            # A forced refresh only reuses a token fetched after this call
            # began, i.e. the result of a concurrent identical refresh.
            if force_refresh and entry[1] < started:
                return None
            return self._with_routing(entry[2])

        result = cached_result()
        if result is not None:
            return result

        async with _lock_for(cache_key):
            result = cached_result()
            if result is not None:
                return result
            cooldown_left = _cooldowns.get(cooldown_key, 0) - time.monotonic()
            if cooldown_left > 0:
                raise RateLimitError(
                    f"VidXgo: rate limited (HTTP 429), retry in {int(cooldown_left) + 1}s",
                    retry_after=cooldown_left,
                )
            data, expires_at = await self._fetch_result(
                token_url, proxies, cooldown_key, deep
            )
            _result_cache[cache_key] = (expires_at, time.monotonic(), data)
            _cooldowns.pop(cooldown_key, None)
            _prune(time.monotonic())
            return self._with_routing(data)
