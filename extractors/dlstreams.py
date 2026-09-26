import logging
import socket
import re
import asyncio
import base64
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urljoin
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from config import (
    get_connector_for_proxy,
    get_preferred_proxy_for_url,
    get_ordered_proxies_for_url,
)
import config as _cfg

logger = logging.getLogger(__name__)

def _decode_barecrop_econfig(enc: str) -> str | None:
    """Decode window._econfig payload used by barecrop.net embeds (Player 2).

    Port of the _0x1b7ade() function in /assets/stream.js:
      s = atob(enc) -> split in 4 chunks -> reorder [2,0,3,1] ->
      strip char at index 3 of each chunk -> atob each -> join ->
      atob -> JSON {stream_url, stream_url_nop2p, ...}
    Returns the m3u8 URL or None.
    """
    try:
        if not enc:
            return None
        s = base64.b64decode(enc).decode("latin1")
        n = 4
        order = [2, 0, 3, 1]
        chunk_len = math.ceil(len(s) / n)
        parts = [s[i * chunk_len:(i + 1) * chunk_len] for i in range(n)]
        arr = [""] * n
        for i, o in enumerate(order):
            a = str(parts[i])
            if len(a) < 4:
                return None
            a = a[:3] + a[4:]
            arr[o] = base64.b64decode(a).decode("latin1")
        final = base64.b64decode("".join(arr)).decode("utf-8", errors="ignore")
        cfg = json.loads(final)
        if isinstance(cfg, dict):
            url = cfg.get("stream_url") or cfg.get("stream_url_nop2p")
            if isinstance(url, str) and url.startswith("http"):
                return url.strip()
        return None
    except Exception:
        return None

# Markers of defaced/hijacked playlists. Since 2026-09-26 the Player 1
# iframe (daddyliveplayer.st/premiumtv/daddy.php) serves a fake manifest
# tagged "by HACKERMAN" whose segments point at tiktokcdn image URLs.
# Such a playlist answers HTTP 200 and lists #EXTINF entries, so a plain
# liveness check would accept it and hide the working players.
_DEFACED_PLAYLIST_MARKERS = ("hackerman", "null367", "tiktokcdn")


def _is_defaced_playlist(body: str | None) -> bool:
    """True if a playlist body shows signs of the HACKERMAN defacement."""
    if not body:
        return False
    lowered = body[:16000].lower()
    return any(marker in lowered for marker in _DEFACED_PLAYLIST_MARKERS)


class ExtractorError(Exception):
    pass

class DLStreamsExtractor:
    """Extractor for daddy live / dlstreams streams."""

    # Return before the request layer kills the task at 30s: an unresponsive
    # dlive.sx would otherwise burn 10s per candidate, hit the outer timeout
    # and turn concurrent requests into 500s.
    EXTRACT_BUDGET_SECONDS = 20.0
    CANDIDATE_TIMEOUT_SECONDS = 6
    # Players poll the extractor every few seconds. Re-scraping the player
    # pages on every poll gets the iframe host to answer 429, so reuse a
    # validated stream URL for a short window instead. When a re-scrape fails,
    # keep serving the last known URL (its token lives for hours) with a small
    # backoff so the failing host is not hammered.
    STREAM_CACHE_SECONDS = 30.0
    STREAM_CACHE_STALE_SECONDS = 900.0
    STREAM_CACHE_FAIL_BACKOFF_SECONDS = 15.0
    MAX_HOST_BACKOFF_SECONDS = 3600.0

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.entry_origin = ""
        self.stream_origin = ""
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        self.session = None
        self._route_sessions = {}
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or []
        self.bypass_warp_active = bypass_warp
        self._inflight_extract_tasks: dict[str, asyncio.Task] = {}
        self._stream_cache: dict[str, tuple[float, float, dict]] = {}
        # host -> monotonic deadline: honour upstream Retry-After instead of
        # scraping a host that already said "too many requests".
        self._host_backoff: dict[str, float] = {}
        # stream URLs already proven to serve defaced playlists: never
        # validate them again and never return them as last-resort fallback.
        self._defaced_urls: set[str] = set()

    @staticmethod
    def _host_of(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    @classmethod
    def _parse_retry_after(cls, value: str | None) -> float | None:
        """Retry-After as delta-seconds or HTTP-date, clamped to a sane range."""
        raw = str(value or "").strip()
        if not raw:
            return None
        seconds = None
        if raw.isdigit():
            seconds = float(raw)
        else:
            try:
                target = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if target is None:
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        if seconds is None:
            return None
        return max(1.0, min(float(seconds), cls.MAX_HOST_BACKOFF_SECONDS))

    def _host_limited_for(self, url: str) -> float:
        """Seconds left before this host may be queried again (0 = free)."""
        deadline = self._host_backoff.get(self._host_of(url), 0.0)
        return max(0.0, deadline - time.monotonic())

    def _mark_host_limited(self, url: str, retry_after: str | None) -> float:
        host = self._host_of(url)
        seconds = self._parse_retry_after(retry_after)
        if seconds is None:
            seconds = self.STREAM_CACHE_FAIL_BACKOFF_SECONDS
        self._host_backoff[host] = time.monotonic() + seconds
        logger.warning("DLStreams: %s rate-limited, backing off %.0fs", host, seconds)
        return seconds

    def _rate_limit_wait(self) -> float:
        """Longest remaining host backoff, dropping expired entries."""
        now = time.monotonic()
        self._host_backoff = {
            host: deadline
            for host, deadline in self._host_backoff.items()
            if deadline > now
        }
        if not self._host_backoff:
            return 0.0
        return max(self._host_backoff.values()) - now

    def _prioritize_player_urls(self, channel_id: str) -> list[str]:
        return self._build_player_urls(channel_id)

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _route_diagnostic(self, url: str) -> str:
        """Describe routing state without exposing proxy credentials."""
        try:
            ordered = get_ordered_proxies_for_url(
                url,
                "dlstreams",
                self.proxies,
                self.bypass_warp_active,
            )
            warp_url = getattr(_cfg, "WARP_PROXY_URL", "")
            route_types = ["WARP" if proxy == warp_url else "proxy" for proxy in ordered]
            route_summary = ",".join(route_types) or "none"
        except Exception as exc:
            route_summary = f"unavailable({type(exc).__name__})"

        warp_enabled = bool(_cfg._get_dynamic_warp_enabled())
        warp_excluded = bool(_cfg._is_warp_excluded(url or ""))
        direct_allowed = _cfg.is_direct_connection_allowed(self.bypass_warp_active)
        host = urlparse(url or "").netloc or "unknown"
        return (
            f"target={host} warp={'on' if warp_enabled else 'off'} "
            f"warp_excluded={'yes' if warp_excluded else 'no'} "
            f"candidates={route_summary} direct={'allowed' if direct_allowed else 'disabled'}"
        )

    def _sync_entry_origin_from_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin != self.entry_origin:
                logger.debug("DLStreams entry origin changed from %s to %s", self.entry_origin, origin)
                self.entry_origin = origin
            if not self.stream_origin:
                self.stream_origin = origin

    def _get_cookie_header_for_url(self, url: str) -> str | None:
        if not self.session or self.session.closed or not self.session.cookie_jar:
            return None

        parsed = urlparse(url)
        cookies = self.session.cookie_jar.filter_cookies(
            f"{parsed.scheme}://{parsed.netloc}/"
        )
        cookie_header = "; ".join(f"{key}={morsel.value}" for key, morsel in cookies.items())
        return cookie_header or None

    @staticmethod
    def _extract_channel_id(url: str) -> str:
        match_id = re.search(r"(?:id=|premium|stream-)(\d+)", url)
        channel_id = match_id.group(1) if match_id else str(url)
        if not channel_id.isdigit():
            channel_id = channel_id.replace("premium", "")
        return channel_id

    def _build_player_urls(self, channel_id: str) -> list[str]:
        origin = self.entry_origin.rstrip("/")
        return [
            f"{origin}/stream/stream-{channel_id}.php",
            f"{origin}/cast/stream-{channel_id}.php",
            f"{origin}/watch/stream-{channel_id}.php",
            f"{origin}/plus/stream-{channel_id}.php",
            f"{origin}/casting/stream-{channel_id}.php",
            f"{origin}/player/stream-{channel_id}.php",
            f"{origin}/hub/stream-{channel_id}.php",
        ]

    @staticmethod
    def _inherit_query_if_missing(absolute_url: str, base_url: str) -> str:
        """CDNs here issue a token in the playlist URL and list relative URIs.

        Plain urljoin drops the parent token, so the segment/variant probe
        becomes a 404 and a working player is reported as dead.
        """
        parsed = urlparse(absolute_url)
        if parsed.query:
            return absolute_url
        base_query = urlparse(base_url).query
        if not base_query:
            return absolute_url
        return parsed._replace(query=base_query).geturl()

    @classmethod
    def _first_media_uri(cls, body: str, base_url: str, want_playlist: bool) -> str | None:
        """First non-comment URI in a playlist: variant (.m3u8) or segment."""
        for raw in (body or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower().split("?")[0].split("#")[0]
            if (low.endswith(".m3u8")) == want_playlist:
                return cls._inherit_query_if_missing(urljoin(base_url, line), base_url)
        return None

    async def _is_stream_alive(self, session, stream_url: str, headers: dict, budget: float | None = None) -> bool:
        """Validate the full chain: master -> variant -> first segment (+ AES key).

        Some CDNs serve the manifest (200) while segments 403 (IP-bound
        tokens, datacenter blocks). A manifest-only check would accept a
        dead player and hide the working ones, so follow the chain with
        cheap probes (first bytes of the first segment only).
        """
        def _timeout(default: float) -> float:
            if budget is None:
                return default
            return max(1.0, min(default, budget))

        try:
            if stream_url in self._defaced_urls:
                logger.debug("DLStreams: validation of %s -> known defaced, skipping", stream_url)
                return False
            async with session.get(stream_url, headers=headers, timeout=_timeout(10)) as resp:
                if resp.status != 200:
                    logger.debug("DLStreams: validation of %s -> HTTP %s", stream_url, resp.status)
                    return False
                master = await resp.text()
            if _is_defaced_playlist(master):
                logger.warning("DLStreams: defaced playlist detected for %s, rejecting", stream_url)
                self._defaced_urls.add(stream_url)
                return False
            if "#EXTM3U" not in master[:8000] and "#EXT-X-" not in master[:8000]:
                logger.debug("DLStreams: validation of %s -> not a playlist", stream_url)
                return False
            current_url, current_body = stream_url, master
            # Master -> variant (media playlists contain #EXTINF, masters don't)
            if "#EXTINF" not in master:
                variant = self._first_media_uri(master, stream_url, want_playlist=True)
                if not variant:
                    logger.debug("DLStreams: validation of %s -> no variant found", stream_url)
                    return False
                async with session.get(variant, headers=headers, timeout=_timeout(8)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: validation variant %s -> HTTP %s", variant, resp.status)
                        return False
                    current_body = await resp.text()
                current_url = variant
                if _is_defaced_playlist(current_body):
                    logger.warning("DLStreams: defaced playlist detected for %s, rejecting", stream_url)
                    self._defaced_urls.add(stream_url)
                    return False
                if "#EXTM3U" not in current_body[:8000]:
                    return False
            # Variant -> first segment (probe first bytes only)
            segment = self._first_media_uri(current_body, current_url, want_playlist=False)
            if not segment:
                logger.debug("DLStreams: validation of %s -> no segments listed", stream_url)
                return False
            probe_headers = {**headers, "Range": "bytes=0-1023"}
            async with session.get(segment, headers=probe_headers, timeout=_timeout(8)) as resp:
                if resp.status not in (200, 206):
                    logger.debug("DLStreams: segment probe of %s -> HTTP %s", segment, resp.status)
                    return False
                try:
                    await resp.content.read(4096)
                except Exception:
                    pass
            # AES key, if the variant uses one
            key_match = re.search(r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', current_body)
            if key_match:
                key_url = self._inherit_query_if_missing(
                    urljoin(current_url, key_match.group(1)), current_url
                )
                try:
                    async with session.get(key_url, headers=headers, timeout=_timeout(8)) as resp:
                        if resp.status != 200:
                            logger.debug("DLStreams: key probe of %s -> HTTP %s", key_url, resp.status)
                            return False
                except Exception as e:
                    logger.debug("DLStreams: key probe of %s failed: %s", key_url, e)
                    return False
            return True
        except Exception as e:
            logger.debug("DLStreams: validation of %s failed: %s", stream_url, e)
            return False

    def _build_result(self, stream_url: str, playback_headers: dict) -> Dict[str, Any]:
        """Build the extractor response payload for an accepted stream URL."""
        parsed_stream = urlparse(stream_url)
        # Sync session cookies for playback/proxying
        self.stream_origin = f"{parsed_stream.scheme}://{parsed_stream.netloc}"
        # Store cookies in session if needed
        cookie_header = self._get_cookie_header_for_url(stream_url)
        if cookie_header:
            playback_headers = {**playback_headers, "Cookie": cookie_header}
        return {
            "destination_url": stream_url,
            "request_headers": playback_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "captured_manifest": None,
            "captured_manifests": {stream_url: ""},
        }

    async def _extract_directly(self, url: str, channel_id: str) -> Dict[str, Any] | None:
        """Fast path direct HTTP M3U8 extraction."""
        session = await self._get_session(url)
        player_urls = self._prioritize_player_urls(channel_id)
        first_fallback = None  # (stream_url, playback_headers): first URL found, even if dead
        seen_iframes: set[str] = set()
        
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.EXTRACT_BUDGET_SECONDS

        def _remaining() -> float:
            return deadline - loop.time()

        def _timeout(default: float) -> float:
            return max(1.0, min(default, _remaining()))

        for candidate_index, candidate in enumerate(player_urls):
            if _remaining() <= 2.0:
                logger.warning(
                    "DLStreams: extraction budget of %.0fs exhausted after %d/%d players",
                    self.EXTRACT_BUDGET_SECONDS,
                    candidate_index,
                    len(player_urls),
                )
                break
            limited_for = self._host_limited_for(candidate)
            if limited_for > 0:
                logger.debug(
                    "DLStreams: %s rate-limited for another %.0fs, skipping",
                    self._host_of(candidate),
                    limited_for,
                )
                continue
            try:
                headers = {
                    "User-Agent": self.base_headers["User-Agent"],
                    "Referer": url,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                }
                
                logger.debug("DLStreams: GET stream page %s", candidate)
                async with session.get(candidate, headers=headers, timeout=_timeout(self.CANDIDATE_TIMEOUT_SECONDS)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: candidate %s returned status %s", candidate, resp.status)
                        if resp.status == 429:
                            self._mark_host_limited(candidate, resp.headers.get("Retry-After"))
                        continue
                    html = await resp.text()
                
                # Extract player iframe src (support absolute, protocol-relative and relative URLs)
                iframe_src = None
                if BeautifulSoup:
                    try:
                        soup = BeautifulSoup(html, "html.parser")
                        iframe_el = soup.find("iframe", id="thatframe") or soup.find("iframe")
                        if iframe_el:
                            iframe_src = iframe_el.get("src")
                    except Exception as e:
                        logger.debug("DLStreams: bs4 parsing error: %s", e)
                
                if not iframe_src:
                    match = re.search(r'<iframe\s+[^>]*src=["\']([^"\']+)["\']', html, re.I)
                    if match:
                        iframe_src = match.group(1).strip()
                
                if not iframe_src:
                    logger.debug("DLStreams: player iframe not found in HTML of %s", candidate)
                    continue

                # Resolve relative / protocol-relative iframe URLs against the candidate page
                if iframe_src.startswith("//"):
                    iframe_src = urlparse(candidate).scheme + ":" + iframe_src
                elif iframe_src.startswith("/"):
                    iframe_src = urljoin(candidate, iframe_src)
                elif not re.match(r"^https?://", iframe_src, re.I):
                    iframe_src = urljoin(candidate + "/", iframe_src)
                
                logger.debug("DLStreams: found player iframe: %s", iframe_src)
                if iframe_src in seen_iframes:
                    logger.debug("DLStreams: iframe %s already attempted, skipping", iframe_src)
                    continue
                seen_iframes.add(iframe_src)
                limited_for = self._host_limited_for(iframe_src)
                if limited_for > 0:
                    logger.debug(
                        "DLStreams: %s rate-limited for another %.0fs, skipping iframe",
                        self._host_of(iframe_src),
                        limited_for,
                    )
                    continue
                
                # Fetch iframe player page
                iframe_headers = headers.copy()
                iframe_headers["Referer"] = candidate
                iframe_headers["Origin"] = self.entry_origin
                
                async with session.get(iframe_src, headers=iframe_headers, timeout=_timeout(self.CANDIDATE_TIMEOUT_SECONDS)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: iframe %s returned status %s", iframe_src, resp.status)
                        if resp.status == 429:
                            self._mark_host_limited(iframe_src, resp.headers.get("Retry-After"))
                        continue
                    iframe_html = await resp.text()
                
                # Extract stream URL from iframe HTML.
                # Supported formats:
                #  1) barecrop.net Player 2: window._econfig='...' (Clappr, scrambled base64)
                #  2) classic: source:window.atob('...') / atob('...')
                #  3) daddyliveplayer.st player: const SRC = "https://...m3u8"
                #  4) direct: source: 'https://...m3u8'
                #  5) fallback: any https://...m3u8 URL in page
                stream_url = None

                m_cfg = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]{100,})['\"]", iframe_html)
                if m_cfg:
                    stream_url = _decode_barecrop_econfig(m_cfg.group(1))
                    if stream_url:
                        logger.debug("DLStreams: decoded _econfig stream URL: %s", stream_url)

                if not stream_url:
                    atob_match = re.search(r"(?:window\.)?atob\(['\"]([A-Za-z0-9+/=]{20,})['\"]\)", iframe_html)
                    if atob_match:
                        try:
                            cand = base64.b64decode(atob_match.group(1)).decode('utf-8', errors='ignore').strip()
                            if cand.startswith("http"):
                                stream_url = cand
                                logger.debug("DLStreams: decrypted stream URL: %s", stream_url)
                            else:
                                logger.debug("DLStreams: atob payload is not a URL: %s", cand[:80])
                        except Exception as e:
                            logger.debug("DLStreams: atob decode failed: %s", e)

                if not stream_url:
                    src_match = re.search(r"(?:const|let|var)\s+SRC\s*=\s*[\"'](https?://[^\"']+?)[\"']", iframe_html)
                    if src_match:
                        stream_url = src_match.group(1).strip()
                        logger.debug("DLStreams: extracted const SRC stream URL: %s", stream_url)

                if not stream_url:
                    direct_match = re.search(r"source\s*:\s*['\"](https?://[^'\"]+?)['\"]", iframe_html)
                    if direct_match:
                        stream_url = direct_match.group(1).strip()
                        logger.debug("DLStreams: extracted direct stream URL: %s", stream_url)

                if not stream_url:
                    m3u8_match = re.search(r"https?://[^\s'\"<>\\]+\.m3u8[^\s'\"<>\\]*", iframe_html)
                    if m3u8_match:
                        stream_url = m3u8_match.group(0)
                        logger.debug("DLStreams: extracted m3u8 fallback URL: %s", stream_url)

                if not stream_url:
                    logger.debug("DLStreams: no stream URL found. Iframe HTML start: %s", iframe_html[:200])
                    continue

                if not stream_url.startswith("http"):
                    logger.debug("DLStreams: invalid stream URL: %s", stream_url[:120])
                    continue
                
                # Format response payload
                parsed_iframe = urlparse(iframe_src)
                iframe_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"
                
                # Use iframe origin as the Referer/Origin for playback headers to pass CDN security checks
                ref_origin = iframe_origin
                
                playback_headers = {
                    "Referer": f"{ref_origin}/",
                    "Origin": ref_origin,
                    "User-Agent": self.base_headers["User-Agent"],
                    "Accept": "*/*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                # Return the first stream that is actually playable from here.
                # A dead Player 1 URL must not hide a working Player 2.
                if await self._is_stream_alive(
                    session, stream_url, playback_headers, budget=_remaining()
                ):
                    logger.info("DLStreams: working stream found via %s", candidate)
                    result = self._build_result(stream_url, playback_headers)
                    result["_validated"] = True
                    return result
                if stream_url in self._defaced_urls:
                    logger.warning(
                        "DLStreams: stream from %s is defaced, excluded from fallback", candidate
                    )
                    continue
                # Keep the first non-defaced URL as last-resort fallback
                # (old behaviour), but only return it if no player validates.
                if first_fallback is None:
                    first_fallback = (stream_url, playback_headers)
                logger.info("DLStreams: stream from %s is dead, trying next player", candidate)
                continue
                
            except Exception as e:
                logger.debug("DLStreams: direct extraction candidate %s failed: %s", candidate, e)
                if candidate_index == 0 and isinstance(e, asyncio.TimeoutError):
                    # Every candidate shares the dlive.sx origin: when the first
                    # page does not answer, the others will not either.
                    logger.warning("DLStreams: site page timed out, aborting candidate loop")
                    break
                continue

        if first_fallback is not None:
            stream_url, playback_headers = first_fallback
            logger.warning("DLStreams: no player validated alive, returning first URL as fallback")
            return self._build_result(stream_url, playback_headers)
                
        return None


    async def _get_session(self, url: str | None = None):
        # Determine the correct proxy for the current state
        target_url = url or self.stream_origin or self.entry_origin
        proxy_url = await get_preferred_proxy_for_url(target_url, "dlstreams", self.proxies, self.bypass_warp_active)
        if proxy_url is None and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError(
                "DLStreams: no usable proxy route; direct fallback disabled "
                f"[{self._route_diagnostic(target_url)}]"
            )
        
        session = self._route_sessions.get(proxy_url)
        if session is not None and not session.closed:
            self.session = session
            self._session_proxy = proxy_url
            return session

        # DLStreams keys and segments appear to be tied to a consistent
        # egress/session context. Using rotating/global proxies here can
        # produce a different AES key than the browser receives.
        if proxy_url:
            connector = get_connector_for_proxy(proxy_url)
            logger.debug("DLStreams: Using proxy session: %s", proxy_url)
        else:
            connector = TCPConnector(limit=0, limit_per_host=0)
            logger.debug("DLStreams: Using direct session (Real IP)")
        
        timeout = ClientTimeout(total=30, connect=10)
        self.session = ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self.base_headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._session_proxy = proxy_url # Store for future comparison
        self._route_sessions[proxy_url] = self.session
        return self.session

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers bypassing the public watch page."""
        self._sync_entry_origin_from_url(url)
        channel_id = self._extract_channel_id(url)
        channel_key = f"premium{channel_id}"

        cached = self._stream_cache.get(channel_key)
        if cached and cached[0] > time.monotonic():
            logger.debug("DLStreams: reusing cached stream URL for %s", channel_key)
            return dict(cached[2])

        try:
            existing_task = self._inflight_extract_tasks.get(channel_key)
            if existing_task and not existing_task.done():
                logger.debug("DLStreams: waiting for in-flight extraction of %s", channel_key)
                result = await existing_task
            else:
                task = asyncio.create_task(self._extract_impl(url, channel_id=channel_id, **kwargs))
                self._inflight_extract_tasks[channel_key] = task
                try:
                    result = await task
                finally:
                    current_task = self._inflight_extract_tasks.get(channel_key)
                    if current_task is task:
                        self._inflight_extract_tasks.pop(channel_key, None)
        except Exception:
            if cached and cached[1] > time.monotonic():
                logger.warning(
                    "DLStreams: extraction failed for %s, serving last known stream URL",
                    channel_key,
                )
                self._backoff_cache_entry(channel_key, cached)
                return dict(cached[2])
            raise

        if result and result.pop("_validated", False):
            now = time.monotonic()
            self._stream_cache[channel_key] = (
                now + self.STREAM_CACHE_SECONDS,
                now + self.STREAM_CACHE_STALE_SECONDS,
                dict(result),
            )
        elif result and cached and cached[1] > time.monotonic():
            logger.warning(
                "DLStreams: no player validated for %s, serving last known stream URL",
                channel_key,
            )
            self._backoff_cache_entry(channel_key, cached)
            return dict(cached[2])
        elif result:
            result.pop("_validated", None)
        return result

    def _backoff_cache_entry(self, channel_key: str, cached: tuple[float, float, dict]) -> None:
        """Delay the next scrape, honouring any Retry-After from the hosts."""
        wait = max(self.STREAM_CACHE_FAIL_BACKOFF_SECONDS, self._rate_limit_wait())
        self._stream_cache[channel_key] = (
            time.monotonic() + wait,
            cached[1],
            cached[2],
        )

    async def _extract_impl(self, url: str, channel_id: str, **kwargs) -> Dict[str, Any]:
        try:
            session = await self._get_session(url)

            # Direct browser-less HTTP extraction (only active path)
            try:
                logger.info("DLStreams: Attempting direct browser-less HTTP extraction for %s", f"premium{channel_id}")
                direct_result = await self._extract_directly(url, channel_id)
                if direct_result:
                    logger.info("DLStreams: Direct browser-less extraction succeeded for %s!", f"premium{channel_id}")
                    return direct_result
            except Exception as direct_exc:
                logger.debug(
                    "DLStreams: browser-less extraction failed for %s: %s",
                    f"premium{channel_id}",
                    direct_exc,
                    exc_info=True,
                )

            raise ExtractorError("Could not retrieve manifest via browser-less extraction (browser fallback is disabled).")

        except asyncio.CancelledError:
            raise
        except ExtractorError:
            raise
        except Exception as e:
            logger.debug("DLStreams internal extraction error for %s: %s", url, e, exc_info=True)
            raise ExtractorError(f"DLStreams extraction failed: {str(e)}") from None

    async def close(self):
        pending_tasks = list(self._inflight_extract_tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._inflight_extract_tasks.clear()
        sessions = set(self._route_sessions.values())
        if self.session is not None:
            sessions.add(self.session)
        for session in sessions:
            if not session.closed:
                await session.close()
        self._route_sessions.clear()
        self.session = None
