"""VidXgo extractor.

The VidXgo page resolves to a signed HLS URL through its ``/t/<id>`` token
endpoint. Both the token endpoint and the CDN playlist are fetched in-process
with curl_cffi browser impersonation, sharing one session so the signed URL
stays bound to the client that asked for it.
"""

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

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
        return proxy.replace("socks5h://", "socks5://", 1)

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

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._validate_url(url)

        self._apply_routing_kwargs(url, kwargs)
        proxy = await self._resolve_proxy(url)
        self.selected_proxy = proxy

        session_cls = _curl_session_cls()
        if session_cls is None:
            raise ExtractorError("VidXgo: curl_cffi is required")
        runner_proxy = self._normalize_proxy(proxy)
        proxies = {"http": runner_proxy, "https": runner_proxy} if runner_proxy else None
        deep = bool(kwargs.get("force_refresh") or kwargs.get("background_refresh"))

        session = session_cls(impersonate="firefox133")
        try:
            token_response = await session.get(
                self._token_endpoint(url),
                headers=TOKEN_HEADERS,
                proxies=proxies,
                verify=False,
                timeout=25,
                allow_redirects=True,
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
            "request_headers": dict(PLAY_HEADERS),
            "captured_manifest": master.text,
            "captured_manifests": captured_manifests,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.selected_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
            "disable_ssl": True,
        }
