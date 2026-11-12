"""HTSport extractor (htsport.info / htsport.cc).

Parses channel and event pages on htsport.info (e.g. sportunohd.htm, calciohd.htm),
extracts the embedded player iframe and delegates resolution to the appropriate
extractor (Matchora, DLStreams, Freeshot/WideIPTV, etc.).
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
from extractors.dlstreams import DLStreamsExtractor
from extractors.freeshot import FreeshotExtractor
from extractors.generic import GenericHLSExtractor
from extractors.matchora import MatchoraExtractor

logger = logging.getLogger(__name__)

SITE_URL = "https://htsport.info"
PAGE_CACHE_TTL = 180  # 3 minuti di cache


class HTSportExtractor(BaseExtractor):
    """Extractor per pagine ed eventi di htsport.info."""

    def __init__(self, request_headers: Optional[dict] = None, proxies: Optional[list] = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="htsport", bypass_warp=bypass_warp)
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://htsport.info/",
        }
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self._cache: Dict[str, tuple[float, dict]] = {}
        self._inflight: Dict[str, asyncio.Task] = {}

    async def _get_session(self, url: Optional[str] = None) -> ClientSession:
        proxy = await self._resolve_proxy(url)
        async with self._session_lock:
            session = self._route_sessions.get(proxy)
            if session is None or session.closed or self._session_proxy != proxy:
                connector = get_connector_for_proxy(proxy, ssl=False) if proxy else TCPConnector(ssl=False, limit=0, use_dns_cache=True)
                timeout = ClientTimeout(total=20)
                session = ClientSession(connector=connector, timeout=timeout)
                self._route_sessions[proxy] = session
            self.session = session
            self._session_proxy = proxy
            return self.session

    def _normalize_url(self, raw_url: str) -> str:
        u = (raw_url or "").strip()
        if u.startswith("htsport://"):
            u = u.replace("htsport://", "")
        if not u.startswith("http://") and not u.startswith("https://"):
            u = f"{SITE_URL}/{u.lstrip('/')}"
        return u

    async def _extract_impl(self, url: str, **kwargs) -> Dict[str, Any]:
        page_url = self._normalize_url(url)
        session = await self._get_session(page_url)

        headers = dict(self.base_headers)
        logger.debug("HTSport: fetching page %s", page_url)

        try:
            async with session.get(page_url, headers=headers, timeout=12) as resp:
                if resp.status != 200:
                    raise ExtractorError(f"HTSport page {page_url} returned HTTP {resp.status}")
                html = await resp.text()
        except Exception as e:
            if isinstance(e, ExtractorError):
                raise
            raise ExtractorError(f"Failed to fetch HTSport page {page_url}: {e}")

        # Cerca iframes nella pagina
        iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not iframes:
            # Prova con iframe src escaped (\/)
            iframes = re.findall(r'<iframe[^>]+src=\\?["\']([^"\']+\.php[^"\']*|[^"\']+)["\']', html, re.IGNORECASE)

        if not iframes:
            # Verifica se c'è un m3u8 diretto nello script o codice
            m3u8_match = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html)
            if m3u8_match:
                direct_url = m3u8_match.group(1).replace("\\/", "/")
                return self._result(direct_url, {"Referer": page_url, "User-Agent": headers["User-Agent"]})
            raise ExtractorError(f"No iframe or stream URL found on HTSport page {page_url}")

        raw_iframe_url = iframes[0].replace("\\/", "/").strip()
        if raw_iframe_url.startswith("//"):
            raw_iframe_url = f"https:{raw_iframe_url}"

        # Srotola iframes intermedi / wrapper (es. asiria.is -> wideiptv ABC.php -> wideiptv player)
        for _ in range(3):
            if any(dom in raw_iframe_url for dom in ("asiria.", "antena.", "abisnews.", "ABC.php", "go.php")):
                try:
                    async with session.get(raw_iframe_url, headers={"Referer": page_url, "User-Agent": headers["User-Agent"]}, timeout=8) as sub_resp:
                        if sub_resp.status == 200:
                            sub_html = await sub_resp.text()
                            sub_iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', sub_html, re.I)
                            if sub_iframes:
                                inner = sub_iframes[0].replace("\\/", "/").strip()
                                if inner.startswith("//"):
                                    inner = f"https:{inner}"
                                elif inner.startswith("/"):
                                    inner = urllib.parse.urljoin(raw_iframe_url, inner)
                                raw_iframe_url = inner
                                logger.info("HTSport: unwrapped intermediate iframe -> %s", raw_iframe_url)
                                continue
                except Exception as e:
                    logger.debug("HTSport: failed to unwrap intermediate iframe %s: %s", raw_iframe_url, e)
            break

        logger.info("HTSport: found iframe %s on %s", raw_iframe_url, page_url)

        # Propagazione delle impostazioni di routing (proxy, direct, bypass_warp) ai sotto-estrattori
        sub_kwargs = dict(kwargs)
        if self._forced_proxy and "proxy" not in sub_kwargs:
            sub_kwargs["proxy"] = self._forced_proxy
        if self._force_direct and "direct" not in sub_kwargs:
            sub_kwargs["direct"] = "1"
        if self.bypass_warp_active and "bypass_warp" not in sub_kwargs:
            sub_kwargs["bypass_warp"] = True

        # 1. Matchora (matchora.pro / matchora.to)
        if "matchora." in raw_iframe_url:
            sub = MatchoraExtractor(self.request_headers, self.proxies, bypass_warp=self.bypass_warp_active)
            return await sub.extract(raw_iframe_url, **{**sub_kwargs, "referer": page_url})

        # 2. DaddyLive / DLStreams (stream-*.php, thedaddy, gomstream, apexstreams, worldsportz4u)
        if any(marker in raw_iframe_url for marker in ("stream-", "thedaddy.", "gomstream.", "apexstreams.", "worldsportz4u.")):
            sub = DLStreamsExtractor(self.request_headers, self.proxies, bypass_warp=self.bypass_warp_active)
            return await sub.extract(raw_iframe_url, **sub_kwargs)

        # 3. Hesgoal blogspot / WideIPTV (freeshot)
        if "hesgoalsss.blogspot" in raw_iframe_url or "wideiptv.top" in raw_iframe_url or "popcdn.day" in raw_iframe_url:
            target_url = raw_iframe_url
            if "hesgoalsss.blogspot" in raw_iframe_url:
                parsed = urllib.parse.urlparse(raw_iframe_url)
                qs = urllib.parse.parse_qs(parsed.query)
                ch_id = (qs.get("id") or [""])[0]
                if ch_id:
                    target_url = f"https://wideiptv.top/player/{ch_id}"
            sub = FreeshotExtractor(self.request_headers, self.proxies, bypass_warp=self.bypass_warp_active)
            return await sub.extract(target_url, **sub_kwargs)

        # 4. Fallback: Generic / direct stream
        if ".m3u8" in raw_iframe_url:
            return self._result(raw_iframe_url, {"Referer": page_url, "User-Agent": headers["User-Agent"]})

        # Prova con GenericHLSExtractor
        sub = GenericHLSExtractor(self.request_headers, self.proxies, bypass_warp=self.bypass_warp_active)
        return await sub.extract(raw_iframe_url, **sub_kwargs)

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        self._apply_routing_kwargs(url, kwargs)
        clean_url = self._normalize_url(url)
        route_key = f"{self.bypass_warp_active}:{self._force_direct}:{self._forced_proxy}"
        cache_key = f"{clean_url}:{route_key}"
        now = time.time()

        force = bool(kwargs.get("force_refresh")) or str(kwargs.get("force", "")).lower() == "true"
        cached = None if force else self._cache.get(cache_key)
        if cached and cached[0] > now:
            logger.debug("HTSport: cache hit for %s", cache_key)
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

        self._cache[cache_key] = (now + PAGE_CACHE_TTL, dict(res))
        return res
