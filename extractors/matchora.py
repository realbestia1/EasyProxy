"""Matchora extractor (matchora.pro / matchora.to).

Extracts HLS stream playlists from matchora player embeds, calling the /api/cast
endpoint to obtain long-lived playback tokens and routing manifest requests
through the edge relay (edge.matchora.pro) to bypass CDN legal restrictions.
"""

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, Optional

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from config import get_connector_for_proxy
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

DEFAULT_SITE_URL = "https://matchora.pro"
DEFAULT_RELAY_URL = "https://edge.matchora.pro"
STREAM_CACHE_TTL = 300  # 5 minuti di cache per l'URL del flusso


class MatchoraExtractor(BaseExtractor):
    """Extractor per stream Matchora."""

    def __init__(self, request_headers: Optional[dict] = None, proxies: Optional[list] = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="matchora", bypass_warp=bypass_warp)
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://htsport.info/",
        }
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self._cache: Dict[str, tuple[float, dict]] = {}
        self._inflight: Dict[str, asyncio.Task] = {}

    async def _get_session(self, url: Optional[str] = None) -> ClientSession:
        proxy = await self._resolve_proxy(url)
        async with self._session_lock:
            session = self._route_sessions.get(proxy)
            if session is None or session.closed or self._session_proxy != proxy:
                connector = get_connector_for_proxy(proxy, ssl=False) if proxy else TCPConnector(ssl=False, limit=0, use_dns_cache=True)
                timeout = ClientTimeout(total=25)
                session = ClientSession(connector=connector, timeout=timeout)
                self._route_sessions[proxy] = session
            self.session = session
            self._session_proxy = proxy
            return self.session

    def _normalize_url(self, raw_url: str) -> tuple[str, str]:
        """Restituisce (channel_id, embed_url)."""
        u = (raw_url or "").strip()
        if u.startswith("matchora://"):
            ch_id = u.replace("matchora://", "").split("?")[0].strip("/")
            return ch_id, f"{DEFAULT_SITE_URL}/embed/channel/{ch_id}"
        if u.isdigit():
            return u, f"{DEFAULT_SITE_URL}/embed/channel/{u}"

        # URL completo matchora.pro / matchora.to
        m = re.search(r"/embed/channel/([a-zA-Z0-9_-]+)", u)
        if m:
            ch_id = m.group(1)
            return ch_id, u

        # Fallback query parameter ?ch=
        parsed = urllib.parse.urlparse(u)
        qs = urllib.parse.parse_qs(parsed.query)
        if "ch" in qs and qs["ch"]:
            return qs["ch"][0], u

        return "", u

    async def _extract_impl(self, url: str, **kwargs) -> Dict[str, Any]:
        ch_id, embed_url = self._normalize_url(url)
        session = await self._get_session(embed_url)

        headers = dict(self.base_headers)
        headers["Referer"] = kwargs.get("referer") or "https://htsport.info/"

        logger.debug("Matchora: fetch embed page %s (ch=%s)", embed_url, ch_id)
        try:
            async with session.get(embed_url, headers=headers, timeout=12) as resp:
                if resp.status != 200:
                    raise ExtractorError(f"Matchora embed page error {resp.status} for {embed_url}")
                html = await resp.text()
        except Exception as e:
            if isinstance(e, ExtractorError):
                raise
            raise ExtractorError(f"Failed to fetch Matchora embed page: {e}")

        # Estrazione parametri CH, FX, PP, RELAY
        ch_m = re.search(r'var\s+CH\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'CH\s*=\s*["\']([^"\']+)["\']', html)
        if ch_m:
            ch_id = ch_m.group(1)
        elif not ch_id:
            raise ExtractorError("Could not extract channel ID (CH) from Matchora embed")

        fx_m = re.search(r'FX\s*=\s*["\']([^"\']*)["\']', html)
        fx = fx_m.group(1) if fx_m else ""

        pp_m = re.search(r'var\s+PP\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'PP\s*=\s*["\']([^"\']+)["\']', html)
        pp = pp_m.group(1) if pp_m else ""

        relay_m = re.search(r'var\s+RELAY\s*=\s*["\']([^"\']+)["\']', html)
        relay = relay_m.group(1) if relay_m else DEFAULT_RELAY_URL
        if not relay or relay.startswith("_"):
            relay = DEFAULT_RELAY_URL
        relay = relay.rstrip("/")

        parsed_embed = urllib.parse.urlparse(embed_url)
        origin_base = f"{parsed_embed.scheme or 'https'}://{parsed_embed.netloc or 'matchora.pro'}"

        query_params = []
        if fx:
            query_params.append(f"fx={urllib.parse.quote(fx)}")
        if pp and not pp.startswith("_"):
            query_params.append(f"pp={urllib.parse.quote(pp)}")
        qs_str = ("?" + "&".join(query_params)) if query_params else ""

        # Tentativo 1: /api/cast/{ch_id} (token 6 ore, perfetto per la riproduzione)
        api_cast_url = f"{origin_base}/api/cast/{urllib.parse.quote(ch_id)}{qs_str}"
        api_play_url = f"{origin_base}/api/play/{urllib.parse.quote(ch_id)}{qs_str}"

        api_headers = {
            "User-Agent": headers["User-Agent"],
            "Referer": embed_url,
            "Accept": "application/json, text/plain, */*",
        }

        stream_url = None
        for api_target in (api_cast_url, api_play_url):
            try:
                logger.debug("Matchora: calling API %s", api_target)
                async with session.get(api_target, headers=api_headers, timeout=8) as api_resp:
                    if api_resp.status == 200:
                        data = await api_resp.json(content_type=None)
                        if isinstance(data, dict) and data.get("url"):
                            stream_url = data["url"]
                            break
                        if isinstance(data, dict) and data.get("reason"):
                            logger.warning("Matchora API returned reason=%s", data.get("reason"))
            except Exception as e:
                logger.warning("Matchora API %s failed: %s", api_target, e)

        if not stream_url:
            raise ExtractorError(f"Matchora failed to resolve playback URL for channel {ch_id}")

        # Sostituzione host con il relay per bypassare i blocchi 451
        # Esempio: https://e.biboguv.cyou/hls/692391/index.m3u8?c=... -> https://edge.matchora.pro/hls/692391/index.m3u8?c=...
        final_stream_url = re.sub(r"^https?://[^/]+", relay, stream_url)
        logger.info("Matchora: resolved stream URL -> %s", final_stream_url)

        playback_headers = {
            "User-Agent": headers["User-Agent"],
            "Referer": f"{origin_base}/",
            "Origin": origin_base,
        }

        return self._result(
            final_stream_url,
            playback_headers,
            mediaflow_endpoint="hls_manifest_proxy",
        )

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        ch_id, _ = self._normalize_url(url)
        route_key = f"{self.bypass_warp_active}:{self._force_direct}:{self._forced_proxy}"
        cache_key = f"{ch_id}:{url.strip()}:{route_key}"
        now = time.time()

        force = bool(kwargs.get("force_refresh")) or str(kwargs.get("force", "")).lower() == "true"
        cached = None if force else self._cache.get(cache_key)
        if cached and cached[0] > now:
            logger.debug("Matchora: cache hit for %s", cache_key)
            return dict(cached[1])

        task = self._inflight.get(cache_key)
        if force or not task or task.done():
            task = asyncio.create_task(self._extract_impl(url, **kwargs))
            self._inflight[cache_key] = task

        try:
            res = await task
        finally:
            if self._inflight.get(cache_key) is task:
                self._inflight.pop(cache_key, None)

        self._cache[cache_key] = (now + STREAM_CACHE_TTL, dict(res))
        return res
