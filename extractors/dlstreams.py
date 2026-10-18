import logging
import re
import asyncio
import base64
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import urllib.parse
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
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

# --- premium edge playlist tokens (dembed.st / strmd.st player) -------------
# The premium edge only answers a playlist request that carries BOTH an allowed
# Origin header and a short-lived viewer token bound to the caller's address.
# The provider's own player (premiumtv/daddy.php) fetches
# /api/playlist-token?slug=<channel> before every manifest load, renews it at
# half its TTL and, on a 403, throws the token away and retries once.
# Without this handshake every playlist refresh fails while already published
# segments keep working, so the player loops the last ~20s window and then dies
# (frozen/green video, "media sequence changed unexpectedly").
PREMIUM_PLAYLIST_RE = re.compile(
    r"^(?P<edge>https?://[^/]+)/(?P<slug>premium\d+)/index\.m3u8$", re.IGNORECASE
)
PREMIUM_TOKEN_PATH = "/api/playlist-token"
PREMIUM_TOKEN_ORIGINS = ("https://dembed.st", "https://dlive.sx")
PREMIUM_TOKEN_DEFAULT_ORIGIN = "https://dembed.st"
PREMIUM_TOKEN_DEFAULT_TTL = 120.0
PREMIUM_TOKEN_MIN_TTL = 20.0
# Renew early: the edge can reject a token a moment before its nominal expiry.
PREMIUM_TOKEN_RENEW_FRACTION = 0.5
# (edge, slug, route) -> (token, monotonic deadline). The token is bound to the
# caller's address, so the egress route is part of the key.
_premium_tokens: Dict[tuple, tuple] = {}
_premium_token_locks: Dict[tuple, asyncio.Lock] = {}


def premium_playlist_parts(url: str) -> tuple | None:
    """(edge_origin, channel_slug) when url is a premium edge playlist."""
    match = PREMIUM_PLAYLIST_RE.match((url or "").split("?", 1)[0])
    if not match:
        return None
    return match.group("edge"), match.group("slug")


def premium_token_origin(headers: dict = None, url: str = "") -> str:
    """An Origin the token endpoint accepts, taken from the playback headers."""
    for value in (
        (headers or {}).get("Origin"),
        (headers or {}).get("origin"),
        (headers or {}).get("Referer"),
        (headers or {}).get("referer"),
        url,
    ):
        if not value:
            continue
        parsed = urlparse(value if "://" in str(value) else f"https://{value}")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if any(origin == allowed for allowed in PREMIUM_TOKEN_ORIGINS):
            return origin
    return PREMIUM_TOKEN_DEFAULT_ORIGIN


def _premium_token_key(playlist_url: str, route_key: str = "") -> tuple | None:
    parts = premium_playlist_parts(playlist_url)
    if not parts:
        return None
    return (parts[0], parts[1], route_key or "direct")


def invalidate_premium_playlist_token(playlist_url: str, route_key: str = "") -> None:
    """Drop a cached token so the next call fetches a fresh one."""
    key = _premium_token_key(playlist_url, route_key)
    if key:
        _premium_tokens.pop(key, None)


async def premium_playlist_token(
    session, playlist_url: str, headers: dict = None, force: bool = False,
    route_key: str = "",
) -> str | None:
    """Per-viewer playlist token for the premium edge (cached until ~half TTL)."""
    parts = premium_playlist_parts(playlist_url)
    if not parts:
        return None
    edge, slug = parts
    key = (edge, slug, route_key or "direct")
    now = time.monotonic()
    cached = _premium_tokens.get(key)
    if cached and not force and cached[1] > now:
        return cached[0]

    lock = _premium_token_locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _premium_tokens.get(key)
        if cached and not force and cached[1] > now:
            return cached[0]
        origin = premium_token_origin(headers, playlist_url)
        token_headers = {
            "Origin": origin,
            "Referer": f"{origin}/",
            "User-Agent": (headers or {}).get("User-Agent")
            or (headers or {}).get("user-agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        try:
            async with session.get(
                f"{edge}{PREMIUM_TOKEN_PATH}?slug={slug}",
                headers=token_headers,
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "DLStreams: premium token for %s -> HTTP %s", slug, resp.status
                    )
                    return cached[0] if cached else None
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.debug("DLStreams: premium token for %s failed: %s", slug, exc)
            return cached[0] if cached else None

        token = str((data or {}).get("token") or "").strip()
        if not token:
            return None
        ttl = float((data or {}).get("ttl") or 0) or PREMIUM_TOKEN_DEFAULT_TTL
        ttl = max(PREMIUM_TOKEN_MIN_TTL, ttl)
        _premium_tokens[key] = (token, now + ttl * PREMIUM_TOKEN_RENEW_FRACTION)
        logger.debug("DLStreams: premium token for %s refreshed (ttl=%ss)", slug, ttl)
        return token


async def ensure_premium_playlist_url(
    session, playlist_url: str, headers: dict = None, force: bool = False,
    route_key: str = "",
) -> str:
    """Playlist URL carrying a fresh premium token (unchanged otherwise)."""
    if premium_playlist_parts(playlist_url) is None:
        return playlist_url
    token = await premium_playlist_token(
        session, playlist_url, headers, force=force, route_key=route_key
    )
    if not token:
        return playlist_url
    base, _, query = playlist_url.partition("?")
    params = [
        (name, value)
        for name, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if name.lower() not in {"t", "_"}
    ]
    params.append(("t", token))
    return f"{base}?{urllib.parse.urlencode(params)}"


class _WatchPlayerParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.player_src = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "iframe" and attrs.get("id") == "playerFrame":
            self.player_src = attrs.get("src")

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

class DLStreamsExtractor(BaseExtractor):
    """Extractor for daddy live / dlstreams streams."""

    # Return before the request layer kills the task at 30s: an unresponsive
    # dlive.sx would otherwise burn 10s per candidate, hit the outer timeout
    # and turn concurrent requests into 500s.
    EXTRACT_BUDGET_SECONDS = 20.0
    CANDIDATE_TIMEOUT_SECONDS = 6
    # Players poll the extractor every few seconds. Re-scraping the player
    # pages on every poll gets the iframe host to answer 429, so reuse a
    # validated stream URL for a short window instead. When a re-scrape fails,
    # serve the last known URL only if it still probes alive (never hang the
    # player 40-50s on a dead playlist) with a small backoff so the failing
    # host is not hammered. Stale window tracks the signed-token lifetime (~3-4 min).
    STREAM_CACHE_SECONDS = 30.0
    STREAM_CACHE_STALE_SECONDS = 300.0
    STREAM_CACHE_FAIL_BACKOFF_SECONDS = 15.0
    MAX_HOST_BACKOFF_SECONDS = 3600.0
    # Channels whose first player is pinned by hand (see _build_player_urls).
    # The recorded player preference must never override that pin.
    PLAYER1_FIRST_CHANNELS = frozenset({"861"})

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="dlstreams", bypass_warp=bypass_warp)
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
        self._inflight_extract_tasks: dict = {}
        self._stream_cache: dict = {}
        # channel_id -> player page URL that last validated, so a re-scrape
        # keeps the same source instead of jumping to another player.
        self._player_preference: dict[str, str] = {}
        # host -> monotonic deadline: honour upstream Retry-After instead of
        # scraping a host that already said "too many requests".
        self._host_backoff: dict[str, float] = {}

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

    def _prioritize_player_urls(self, channel_id: str, requested_url: str = "", watch_html: str = "") -> list[str]:
        players = self._build_player_urls(channel_id, requested_url=requested_url)
        # Honour an explicit player, otherwise use the site's active iframe.
        requested_player = urlparse(requested_url)._replace(query="", fragment="").geturl()
        if requested_player in players:
            preferred = requested_player
        else:
            parser = _WatchPlayerParser()
            parser.feed(watch_html)
            preferred = urljoin(requested_url, parser.player_src or "")
        # Only reorder known players for this channel on the entry origin.
        if preferred in players:
            players.remove(preferred)
            players.insert(0, preferred)
        # Re-scrapes (playlist refreshes, token expiry, 429s) must keep the
        # player that was already validated for this channel: switching source
        # mid-playback changes the media sequence, the PIDs and the parameter
        # sets, which players report as "media sequence changed unexpectedly"
        # plus "non-existing SPS/PPS" and a frozen/green picture.
        pinned = self._player_preference.get(channel_id)
        if (
            pinned in players
            and players[0] != pinned
            and requested_player not in players
            and channel_id not in self.PLAYER1_FIRST_CHANNELS
        ):
            players.remove(pinned)
            players.insert(0, pinned)
        return players

    @staticmethod
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

    def _build_player_urls(self, channel_id: str, requested_url: str = "") -> list[str]:
        origin = self.entry_origin.rstrip("/")
        req_lower = (requested_url or "").lower()
        if "/stream/stream-" in req_lower:
            # User explicitly requested Player 1 (/stream/)
            return [
                f"{origin}/stream/stream-{channel_id}.php",
                f"{origin}/cast/stream-{channel_id}.php",
                f"{origin}/watch/stream-{channel_id}.php",
                f"{origin}/plus/stream-{channel_id}.php",
                f"{origin}/casting/stream-{channel_id}.php",
                f"{origin}/player/stream-{channel_id}.php",
                f"{origin}/hub/stream-{channel_id}.php",
            ]
        # Default: prioritize Player 2 (/cast/) because it provides clean native MPEG-TS (.ts)
        # without the heavy (7MB+) PNG-wrapped TikTok CDN overhead and frequent 403s of Player 1.
        # Exception: 861 always tries Player 1 first, even when its Player 2 looks
        # online in a browser. That backend serves a technically valid stream (HTTP 200,
        # valid MPEG-TS container, advancing timestamps, decodable-looking frames) whose
        # video track is unplayable, so every cheap liveness probe promotes it while
        # playback shows green/black with working audio. The failure only surfaces at
        # decode time, which no proxy-side check can afford to replicate -- every
        # simpler approach was tried and discarded, this pin is the fix.
        first = "stream" if channel_id in self.PLAYER1_FIRST_CHANNELS else "cast"
        second = "cast" if channel_id == "861" else "stream"
        return [
            f"{origin}/{first}/stream-{channel_id}.php",
            f"{origin}/{second}/stream-{channel_id}.php",
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

    @staticmethod
    def _segment_looks_like_image(segment_url: str, content_type: str, head: bytes) -> bool:
        """True when a segment is (or may be) an image container instead of raw TS."""
        if head[:1] == b"\x47":
            return False  # already clean MPEG-TS
        if head.startswith(b"\x89PNG\r\n\x1a\n") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            return True
        low = (segment_url or "").lower()
        return (
            (content_type or "").startswith("image/")
            or ".image" in low
            or "tiktokcdn" in low
            or "~tplv-" in low
        )

    @staticmethod
    def _ts_video_payload(data: bytes) -> bool | None:
        """Inspect aligned TS packets for video PES, not just a PMT declaration.

        False means actual audio PES were found without video. None leaves
        unsupported payloads inconclusive (e.g. private/extended stream IDs).
        A clear video PES starts with 00 00 01 E0..EF after the TS adaptation
        field. No codec probing or transcoding is required.
        """
        audio = False
        unknown = False
        for offset in range(0, len(data) - 187, 188):
            packet = data[offset:offset + 188]
            if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
                return None
            if not packet[1] & 0x40 or not packet[3] & 0x10:
                continue
            start = 5 + packet[4] if packet[3] & 0x20 else 4
            payload = packet[start:]
            if len(payload) < 4:
                # A PES header may continue in the next TS packet. This
                # lightweight inspector cannot exclude video in that case.
                unknown = True
                continue
            if payload[:3] != b"\x00\x00\x01":
                continue
            stream_id = payload[3]
            if 0xE0 <= stream_id <= 0xEF:
                return True
            if 0xC0 <= stream_id <= 0xDF:
                audio = True
            else:
                unknown = True
        return False if audio and not unknown else None

    async def _segment_carries_video(
        self, session, segment_url: str, headers: dict, timeout: float,
        encrypted: bool = False,
    ) -> bool:
        """Bounded probe; stop downloading native TS as soon as video is found."""
        max_bytes = 16 * 1024 * 1024
        async with session.get(
            segment_url, headers=headers, timeout=ClientTimeout(total=timeout)
        ) as resp:
            if resp.status not in (200, 206):
                return False
            head = await resp.content.read(4096)
            if not head:
                return False
            if encrypted:
                return True
            is_image = self._segment_looks_like_image(
                segment_url, (resp.headers.get("Content-Type") or "").lower(), head
            )
            if is_image:
                from services.proxy_core import HLSProxyCoreMixin
                body = bytearray(head)
                async for chunk in resp.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return False
                unwrapped = await asyncio.to_thread(
                    HLSProxyCoreMixin._strip_fake_png_header_from_ts, bytes(body)
                )
                return (
                    len(unwrapped) > 188 and unwrapped[0] == 0x47
                    and unwrapped[188] == 0x47
                    and self._ts_video_payload(unwrapped) is not False
                )
            if head[0] != 0x47:
                # fMP4 and other containers retain the existing HTTP probe.
                return True
            pending = b""
            body = bytearray()
            # Native TS can start with a long audio run. A missing video PES
            # is only conclusive once the complete segment has been examined.
            async def chunks():
                yield head
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk

            async for chunk in chunks():
                body.extend(chunk)
                pending += chunk
                if len(body) > max_bytes:
                    return False
                complete = len(pending) // 188 * 188
                evidence = self._ts_video_payload(pending[:complete])
                if evidence is True:
                    return True
                pending = pending[complete:]
            if resp.status == 200 and not pending and self._ts_video_payload(bytes(body)) is False:
                logger.info("DLStreams: segment contains audio PES but no video payload")
                return False
            return True

    async def _with_premium_token(self, session, stream_url: str, headers: dict) -> str:
        """Attach a fresh premium edge playlist token when the URL needs one."""
        try:
            return await ensure_premium_playlist_url(
                session,
                stream_url,
                headers,
                route_key=(getattr(self, "last_used_proxy", None) or "direct"),
            )
        except Exception as exc:
            logger.debug("DLStreams: premium token refresh failed for %s: %s", stream_url, exc)
            return stream_url

    async def _is_stream_alive(self, session, stream_url: str, headers: dict, budget: float | None = None) -> bool:
        """Validate the full chain: master -> variant -> first segment (+ AES key).

        Some CDNs serve the manifest (200) while segments 403 (IP-bound
        tokens, datacenter blocks). A manifest-only check would accept a
        dead player and hide the working ones, so follow the chain with
        bounded segment probes.

        With an image-wrapped segment (PNG/WebP, e.g. Player 1) the body is
        downloaded once and unwrapped exactly like the proxy does when serving it:
        if no MPEG-TS can be recovered (static banner/placeholder image, which the
        proxy would answer with HTTP 204) the player is reported dead so the caller
        moves on to the next player. Native TS must carry video PES, rather
        than only announcing a video PID in its program map.
        """
        deadline = time.monotonic() + (budget if budget is not None else self.EXTRACT_BUDGET_SECONDS)

        def _timeout(default: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return min(default, remaining)

        stream_url = await self._with_premium_token(session, stream_url, headers)

        try:
            async with session.get(stream_url, headers=headers, timeout=_timeout(10)) as resp:
                if resp.status != 200:
                    logger.debug("DLStreams: validation of %s -> HTTP %s", stream_url, resp.status)
                    return False
                master = await resp.text()
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
                variant = await self._with_premium_token(session, variant, headers)
                async with session.get(variant, headers=headers, timeout=_timeout(8)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: validation variant %s -> HTTP %s", variant, resp.status)
                        return False
                    current_body = await resp.text()
                current_url = variant
                if "#EXTM3U" not in current_body[:8000]:
                    return False
            # Variant -> segment. Do not use Range for a TS video probe: an
            # audio-only prefix cannot establish whether video exists later.
            segment = self._first_media_uri(current_body, current_url, want_playlist=False)
            if not segment:
                logger.debug("DLStreams: validation of %s -> no segments listed", stream_url)
                return False
            encrypted = bool(re.search(r'#EXT-X-KEY:[^\n]*METHOD=(?!NONE\b)', current_body))
            probe_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
            if encrypted:
                probe_headers["Range"] = "bytes=0-4095"
            if not await self._segment_carries_video(
                session, segment, probe_headers, _timeout(12), encrypted=encrypted
            ):
                return False
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
            self._last_validated_manifest = current_body or master
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
        manifest_text = getattr(self, "_last_validated_manifest", None) or ""
        return {
            "destination_url": stream_url,
            "request_headers": playback_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "captured_manifest": None,
            "captured_manifests": {stream_url: manifest_text},
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

    async def _extract_directly(self, url: str, channel_id: str) -> Dict[str, Any] | None:
        """Fast path direct HTTP M3U8 extraction."""
        session = await self._get_session(url)
        seen_iframes: set[str] = set()
        
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.EXTRACT_BUDGET_SECONDS

        def _remaining() -> float:
            return deadline - loop.time()

        def _timeout(default: float) -> float:
            return max(1.0, min(default, _remaining()))

        watch_html = ""
        if urlparse(url).path == "/watch.php" and not self._host_limited_for(url):
            try:
                async with session.get(
                    url, headers=self.base_headers, timeout=_timeout(3)
                ) as resp:
                    if resp.status == 200:
                        watch_html = await resp.text()
                    elif resp.status == 429:
                        self._mark_host_limited(url, resp.headers.get("Retry-After"))
            except Exception as exc:
                logger.debug("DLStreams: default player unavailable: %s", exc)
        player_urls = self._prioritize_player_urls(
            channel_id, requested_url=url, watch_html=watch_html
        )

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
                #  3) direct: source: 'https://...m3u8'
                #  4) fallback: any https://...m3u8 URL in page
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
                # The premium edge needs a per-viewer token on the playlist URL,
                # so hand over the tokenized URL (validation bundles it too).
                stream_url = await self._with_premium_token(session, stream_url, playback_headers)
                if await self._is_stream_alive(
                    session, stream_url, playback_headers, budget=_remaining()
                ):
                    logger.info("DLStreams: working stream found via %s", candidate)
                    if channel_id not in self.PLAYER1_FIRST_CHANNELS:
                        self._player_preference[channel_id] = candidate
                    result = self._build_result(stream_url, playback_headers)
                    result["_validated"] = True
                    return result
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

        return None


    async def _get_session(self, url: str | None = None):
        # Determine the correct proxy for the current state
        target_url = url or self.stream_origin or self.entry_origin
        if self._force_direct:
            proxy_url = None
        else:
            forced = _cfg.effective_forced_proxy(target_url, self._forced_proxy)
            if forced:
                proxy_url = forced
            else:
                proxy_url = await get_preferred_proxy_for_url(target_url, "dlstreams", self.proxies, self.bypass_warp_active)
        if proxy_url is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError(
                "DLStreams: no usable proxy route; direct fallback disabled "
                f"[{self._route_diagnostic(target_url)}]"
            )
        self.last_used_proxy = proxy_url
        
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
        self._apply_routing_kwargs(url, kwargs)
        channel_id = self._extract_channel_id(url)
        requested_player = urlparse(url)._replace(query="", fragment="").geturl()
        req_mode = (
            requested_player if requested_player in self._build_player_urls(channel_id)
            else "default"
        )
        channel_key = (f"premium{channel_id}", req_mode, self._forced_proxy, self._force_direct, self.bypass_warp_active)

        cached = self._stream_cache.get(channel_key)
        if cached and cached[0] > time.monotonic() and not kwargs.get("force_refresh"):
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
                if await self._is_cached_still_alive(cached[2]):
                    logger.warning(
                        "DLStreams: extraction failed for %s, serving last known stream URL",
                        channel_key,
                    )
                    self._backoff_cache_entry(channel_key, cached)
                    return dict(cached[2])
                logger.warning(
                    "DLStreams: cached stream for %s is dead, not serving stale",
                    channel_key,
                )
            raise

        if result and result.pop("_validated", False):
            now = time.monotonic()
            self._stream_cache[channel_key] = (
                now + self.STREAM_CACHE_SECONDS,
                now + self.STREAM_CACHE_STALE_SECONDS,
                dict(result),
            )
        elif result and cached and cached[1] > time.monotonic():
            if await self._is_cached_still_alive(cached[2]):
                logger.warning(
                    "DLStreams: no player validated for %s, serving last known stream URL",
                    channel_key,
                )
                self._backoff_cache_entry(channel_key, cached)
                return dict(cached[2])
            logger.warning(
                "DLStreams: cached stream for %s is dead, not serving stale",
                channel_key,
            )
        elif result:
            result.pop("_validated", None)
        return result

    def _backoff_cache_entry(self, channel_key, cached: tuple[float, float, dict]) -> None:
        """Delay the next scrape, honouring any Retry-After from the hosts."""
        wait = max(self.STREAM_CACHE_FAIL_BACKOFF_SECONDS, self._rate_limit_wait())
        self._stream_cache[channel_key] = (
            time.monotonic() + wait,
            cached[1],
            cached[2],
        )

    async def _is_cached_still_alive(self, cached_result: dict) -> bool:
        """Bounded video probe before serving a stale URL after a failed refresh."""
        try:
            stream_url = (cached_result or {}).get("destination_url", "")
            if not stream_url:
                return False
            headers = (cached_result or {}).get("request_headers", {}) or {}
            session = await self._get_session(stream_url)
            return await self._is_stream_alive(session, stream_url, headers, budget=4.0)
        except Exception:
            return False

    async def _extract_impl(self, url: str, channel_id: str, **kwargs) -> Dict[str, Any]:
        try:
            await self._get_session(url)

            # Direct browser-less HTTP extraction (only active path)
            direct_error = None
            try:
                logger.info("DLStreams: Attempting direct browser-less HTTP extraction for %s", f"premium{channel_id}")
                direct_result = await self._extract_directly(url, channel_id)
                if direct_result:
                    logger.info("DLStreams: Direct browser-less extraction succeeded for %s!", f"premium{channel_id}")
                    return direct_result
            except Exception as direct_exc:
                direct_error = direct_exc
                logger.debug(
                    "DLStreams: browser-less extraction failed for %s: %s",
                    f"premium{channel_id}",
                    direct_exc,
                    exc_info=True,
                )

            raise ExtractorError("Could not retrieve manifest via extraction.") from direct_error

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
