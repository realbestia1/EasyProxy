import logging
import os
import re
import asyncio
import base64
import json
import math
import shutil
import time
from datetime import datetime

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

# A valid TS container can still carry an undecodable video track (dlive.sx
# Player 2: green/black picture, working audio). Decode one frame with the
# ffmpeg already present in the Docker image; container-only checks cannot
# detect this, while a manual user would switch players.
_VIDEO_PROBE_MIN_BYTES = 1024 * 1024
_VIDEO_PROBE_MAX_ERRORS = 3
_VIDEO_PROBE_TIMEOUT = 6.0
# Player health (see _player_rank). A live player that downloads its segment
# in most of its duration stutters; a delayed one is still watchable.
MAX_LIVE_LAG_SECONDS = 120.0
MAX_SEGMENT_LOAD_RATIO = 0.75
_VIDEO_PROBE_MARKERS = (
    "error while decoding",
    "top block unavailable",
    "Reference ",
    "decode_slice_header error",
    "Invalid data",
    "corrupt",
)

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
# The edge lists each segment as base64url(AES-128-CTR(url)): key = base64url
# "k" returned with the token, counter = the segment's media sequence number
# (big-endian, 16 bytes). token -> key, so a playlist is decrypted with the key
# of the token it was fetched with even if the cache was renewed meanwhile.
_premium_segment_keys: Dict[str, bytes] = {}
_PREMIUM_SEGMENT_KEYS_MAX = 512
# The premium player releases a ticket (~12h, bound to the solver's address and
# carried by the epoch in front of it) only after its own Turnstile; the token
# endpoint answers it, and nothing else, with a playlist token.
# (edge, slug, route) -> (ticket, monotonic deadline)
_premium_tickets: Dict[tuple, tuple] = {}
_PREMIUM_TICKET_DEFAULT_TTL = 11 * 3600.0


def _premium_ticket_deadline(ticket: str) -> float:
    """Monotonic deadline from the epoch prefix of a premium ticket."""
    try:
        epoch = float(str(ticket).split(".", 1)[0])
    except (TypeError, ValueError):
        return time.monotonic() + _PREMIUM_TICKET_DEFAULT_TTL
    remaining = epoch - time.time()
    return time.monotonic() + max(60.0, min(remaining, _PREMIUM_TICKET_DEFAULT_TTL))


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decrypt_premium_playlist(body: str, playlist_url: str) -> str:
    """Replace encrypted premium segment URIs with the real (TikTok CDN) URLs."""
    if not body or premium_playlist_parts(playlist_url) is None:
        return body
    token = dict(urllib.parse.parse_qsl(urlparse(playlist_url).query)).get("t", "")
    key = _premium_segment_keys.get(token)
    if not key:
        return body
    from Crypto.Cipher import AES

    seq = 0
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(stripped.split(":", 1)[1])
            except ValueError:
                pass
        elif stripped and not stripped.startswith("#"):
            if not stripped.lower().startswith("http") and "." not in stripped:
                try:
                    cipher = AES.new(
                        key, AES.MODE_CTR, nonce=b"", initial_value=seq.to_bytes(16, "big")
                    )
                    plain = cipher.decrypt(_b64url_decode(stripped)).decode("utf-8")
                    if plain.startswith("http"):
                        line = plain
                except Exception:
                    pass
            seq += 1
        out.append(line)
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


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


async def _premium_curl_get(
    url: str, headers: dict, timeout: float, proxy: str | None = None
) -> tuple[int, str]:
    """GET a premium edge URL with Chrome TLS impersonation.

    The edge fingerprints TLS: aiohttp is answered 403 even with a valid
    ticket/token, curl_cffi's Chrome profile is answered 200.
    """
    from curl_cffi.requests import AsyncSession

    kwargs = {"impersonate": "chrome", "timeout": timeout}
    if proxy:
        kwargs["proxy"] = str(proxy).replace("socks5h://", "socks5://")
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(**kwargs) as curl_session:
            resp = await curl_session.get(url, headers=headers)
            return resp.status_code, resp.text or ""
    except Exception as exc:
        logger.debug("DLStreams: curl_cffi fetch of %s failed: %s", url, exc)
        return 0, ""


async def premium_playlist_token(
    session, playlist_url: str, headers: dict = None, force: bool = False,
    route_key: str = "", ticket: str | None = None, proxy: str | None = None,
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
        if ticket is None:
            # The proxy path calls in without a ticket: reuse the one the
            # extractor already solved for this edge/slug/route.
            cached_ticket = _premium_tickets.get(key)
            if cached_ticket and cached_ticket[1] > now:
                ticket = cached_ticket[0]
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
            token_query = f"slug={slug}"
            if ticket:
                # Without the ticket the endpoint answers 403.
                token_query += f"&tk={urllib.parse.quote(ticket)}"
            token_url = f"{edge}{PREMIUM_TOKEN_PATH}?{token_query}"
            data = None
            async with session.get(
                token_url,
                headers=token_headers,
                timeout=ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                else:
                    logger.debug(
                        "DLStreams: premium token for %s -> HTTP %s", slug, resp.status
                    )
            if data is None:
                # Same TLS fingerprint gate as the playlist: retry impersonated.
                status, body = await _premium_curl_get(token_url, token_headers, 10, proxy)
                if status == 200 and body:
                    data = json.loads(body)
            if data is None:
                return cached[0] if cached else None
        except Exception as exc:
            logger.debug("DLStreams: premium token for %s failed: %s", slug, exc)
            return cached[0] if cached else None

        token = str((data or {}).get("token") or "").strip()
        if not token:
            return None
        seg_key = str((data or {}).get("k") or "").strip()
        if seg_key:
            try:
                if len(_premium_segment_keys) >= _PREMIUM_SEGMENT_KEYS_MAX:
                    _premium_segment_keys.pop(next(iter(_premium_segment_keys)))
                _premium_segment_keys[token] = _b64url_decode(seg_key)
            except Exception:
                logger.debug("DLStreams: invalid premium segment key for %s", slug)
        ttl = float((data or {}).get("ttl") or 0) or PREMIUM_TOKEN_DEFAULT_TTL
        ttl = max(PREMIUM_TOKEN_MIN_TTL, ttl)
        _premium_tokens[key] = (token, now + ttl * PREMIUM_TOKEN_RENEW_FRACTION)
        logger.debug("DLStreams: premium token for %s refreshed (ttl=%ss)", slug, ttl)
        return token


async def ensure_premium_playlist_url(
    session, playlist_url: str, headers: dict = None, force: bool = False,
    route_key: str = "", ticket: str | None = None, proxy: str | None = None,
) -> str:
    """Playlist URL carrying a fresh premium token (unchanged otherwise)."""
    if premium_playlist_parts(playlist_url) is None:
        return playlist_url
    token = await premium_playlist_token(
        session, playlist_url, headers, force=force, route_key=route_key,
        ticket=ticket, proxy=proxy,
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
    # The Turnstile solver may cold-start Chromium (up to ~60s for two attempts).
    REQUEST_TIMEOUT_TOTAL = 120
    CANDIDATE_TIMEOUT_SECONDS = 6
    # No resolved-URL cache and no host backoff: the extractor endpoint answers
    # with a redirect to the stable /proxy manifest URL, so manifest reloads go
    # to the proxy and the extractor runs once per playback (plus 403 re-extracts).
    # Shared across instances: the extractor instance is retired right after
    # each extraction, so the last validated player must outlive it.
    _player_preference: dict = {}
    # A re-extraction inside this window keeps the pinned player even when it
    # is not ideal (no mid-playback switch); every accepted extraction renews
    # it. Playback through the stable proxy URL does not re-extract, so the
    # window must outlast a whole match/film: a stall after it would otherwise
    # re-rank and jump to a player with a different delay.
    PIN_STICKY_SECONDS = 4 * 3600.0
    # A sticky player that fails once is probed again before switching: a stall
    # of a few seconds (WARP/CDN hiccup) must not move playback to another player.
    PIN_RETRIES = 2
    PIN_RETRY_DELAY = 2.0
    PIN_ATTEMPT_BUDGET = 6.0

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

    def _pin_player(self, channel_id: str, candidate: str) -> None:
        self._player_preference[(channel_id, self._route_key())] = (candidate, time.monotonic())

    def _route_key(self) -> tuple:
        return (self._forced_proxy, self._force_direct, self.bypass_warp_active)

    def _prioritize_player_urls(
        self, channel_id: str, requested_url: str = ""
    ) -> list[str]:
        players = self._build_player_urls(channel_id)
        # Honour an explicit player request.
        requested_player = urlparse(requested_url)._replace(query="", fragment="").geturl()
        if requested_player in players:
            players.remove(requested_player)
            players.insert(0, requested_player)
        # Re-scrapes (playlist refreshes, token expiry, 429s) must keep the
        # player that was already validated for this channel: switching source
        # mid-playback changes the media sequence, the PIDs and the parameter
        # sets, which players report as "media sequence changed unexpectedly"
        # plus "non-existing SPS/PPS" and a frozen/green picture.
        pinned = (self._player_preference.get((channel_id, self._route_key())) or (None,))[0]
        if (
            pinned in players
            and players[0] != pinned
            and requested_player not in players
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

    def _build_player_urls(self, channel_id: str) -> list[str]:
        origin = self.entry_origin.rstrip("/")
        # Player 1 first, like the site. The decode probe rejects a broken
        # video track and the caller tries the next player automatically.
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

    @staticmethod
    def _playlist_lag(body: str, now: float) -> float | None:
        """Seconds the live edge trails the wall clock (EXT-X-PROGRAM-DATE-TIME)."""
        edge = None
        for raw in (body or "").splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                try:
                    stamp = line.split(":", 1)[1].replace("Z", "+00:00")
                    edge = datetime.fromisoformat(stamp).timestamp()
                except ValueError:
                    edge = None
            elif line.startswith("#EXTINF:") and edge is not None:
                try:
                    edge += float(line[8:].split(",", 1)[0])
                except ValueError:
                    pass
        return None if edge is None else max(0.0, now - edge)

    @staticmethod
    def _segment_load_ratio(fetch: tuple | None, duration: float | None) -> float | None:
        """Time to download the whole segment over its play time (near 1 stutters)."""
        if not fetch or not duration:
            return None
        elapsed, read, total = fetch
        if not read:
            return None
        if total and total > read:
            elapsed *= total / read
        return elapsed / duration

    @staticmethod
    def _player_rank(lag: float | None, load_ratio: float | None) -> tuple:
        """Lower is better: a smooth player beats a stuttering one, then less delay.

        Unknown values (no PROGRAM-DATE-TIME, fMP4/encrypted probe) are not
        penalised, so the only player of a channel is never discarded.
        """
        smooth = load_ratio is None or load_ratio <= MAX_SEGMENT_LOAD_RATIO
        return (not smooth, lag or 0.0)

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

    async def _ts_video_decodes(self, ts: bytes) -> bool:
        """Decode one video frame with ffmpeg.

        A valid MPEG-TS container can still hold an undecodable video track
        (dlive.sx Player 2: working audio, green/black picture). Container
        checks cannot see that, so probe a bounded prefix and reject only
        repeated decode errors. When ffmpeg is unavailable or the probe is
        inconclusive, do not reject the stream.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.debug("DLStreams: ffmpeg not found, skipping video decode probe")
            return True
        probe = bytes(ts[:16 * 1024 * 1024])
        if len(probe) <= 188:
            return True
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-v", "error",
                "-f", "mpegts",
                "-threads", "1",
                "-i", "pipe:0",
                "-frames:v", "1",
                "-f", "null",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=probe), timeout=_VIDEO_PROBE_TIMEOUT
            )
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.debug("DLStreams: video decode probe timed out")
            return True
        except OSError as exc:
            logger.debug("DLStreams: video decode probe failed: %s", exc)
            return True
        errors = [
            line
            for line in stderr.decode("utf-8", "replace").splitlines()
            if any(marker in line for marker in _VIDEO_PROBE_MARKERS)
        ]
        if len(errors) >= _VIDEO_PROBE_MAX_ERRORS:
            logger.info(
                "DLStreams: video decode probe rejected segment (%d errors): %s",
                len(errors),
                errors[0][:160],
            )
            return False
        return True

    async def _segment_carries_video(
        self, session, segment_url: str, headers: dict, timeout: float,
        encrypted: bool = False,
    ) -> bool:
        """Bounded probe; verify the segment carries a decodable video track."""
        max_bytes = 16 * 1024 * 1024
        self._segment_fetch = None
        started = time.monotonic()
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
                self._segment_fetch = (time.monotonic() - started, len(body), len(body))
                unwrapped = await asyncio.to_thread(
                    HLSProxyCoreMixin._strip_fake_png_header_from_ts, bytes(body)
                )
                if not (
                    len(unwrapped) > 188 and unwrapped[0] == 0x47
                    and unwrapped[188] == 0x47
                    and self._ts_video_payload(unwrapped) is not False
                ):
                    self._content_broken = True  # placeholder image, no TS inside
                    return False
                decodes = await self._ts_video_decodes(unwrapped)
                self._content_broken = not decodes
                return decodes
            if head[0] != 0x47:
                # fMP4 and other containers retain the existing HTTP probe.
                return True
            pending = b""
            body = bytearray()
            video_found = False
            # Native TS can start with a long audio run. A missing video PES
            # is only conclusive once the complete segment has been examined.
            # Keep reading after the first video PES so a decoder can check it.
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
                if self._ts_video_payload(pending[:complete]) is True:
                    video_found = True
                pending = pending[complete:]
                if video_found and len(body) >= _VIDEO_PROBE_MIN_BYTES:
                    break
            self._segment_fetch = (time.monotonic() - started, len(body), resp.content_length)
            if (
                resp.status == 200 and not pending and not video_found
                and self._ts_video_payload(bytes(body)) is False
            ):
                logger.info("DLStreams: segment contains audio PES but no video payload")
                self._content_broken = True
                return False
            if video_found:
                decodes = await self._ts_video_decodes(bytes(body))
                self._content_broken = not decodes
                return decodes
            return True

    async def _with_premium_token(
        self, session, stream_url: str, headers: dict, ticket: str | None = None
    ) -> str:
        """Attach a fresh premium edge playlist token when the URL needs one."""
        try:
            return await ensure_premium_playlist_url(
                session,
                stream_url,
                headers,
                route_key=(getattr(self, "last_used_proxy", None) or "direct"),
                ticket=ticket,
                proxy=getattr(self, "_session_proxy", None),
            )
        except Exception as exc:
            logger.debug("DLStreams: premium token refresh failed for %s: %s", stream_url, exc)
            return stream_url

    async def _solve_premium_ticket(self, page_url: str) -> str | None:
        """Solve Turnstile on the premium player and return its ticket."""
        if str(os.getenv("DLSTREAMS_TURNSTILE_SOLVER", "1")).strip().lower() in {"0", "off", "false", "no"}:
            return None
        try:
            from services.turnstile_cdp import solve_premium_ticket
        except Exception as e:
            logger.debug("DLStreams: premium solver unavailable: %s", e)
            return None
        try:
            solution = await solve_premium_ticket(
                page_url, proxy=getattr(self, "_session_proxy", None)
            )
        except Exception as e:
            logger.warning("DLStreams: premium solver failed: %s", e)
            return None
        if not solution or not solution.get("success") or not solution.get("ticket"):
            return None
        logger.info("DLStreams: premium ticket obtained for %s", page_url)
        return solution["ticket"]

    async def _solve_gated_player(self, page_url: str) -> tuple[str, str | None] | None:
        """Solve the xstream Turnstile gate in headless Chromium (best effort)."""
        if str(os.getenv("DLSTREAMS_TURNSTILE_SOLVER", "1")).strip().lower() in {"0", "off", "false", "no"}:
            return None
        try:
            from services.turnstile_cdp import solve_player_stream
        except Exception as e:
            logger.debug("DLStreams: Turnstile solver unavailable: %s", e)
            return None
        try:
            solution = await solve_player_stream(
                page_url, proxy=getattr(self, "_session_proxy", None)
            )
        except Exception as e:
            logger.warning("DLStreams: Turnstile solver failed: %s", e)
            return None
        if not solution:
            return None
        if not solution.get("success"):
            logger.warning(
                "DLStreams: Turnstile verification rejected (%s)",
                solution.get("error") or "unknown",
            )
            return None
        stream_url = solution.get("stream_url_nop2p") or solution.get("stream_url")
        if not stream_url:
            return None
        return stream_url, solution.get("_solver_user_agent")

    async def _fetch_playlist(
        self, session, url: str, headers: dict, timeout: float
    ) -> tuple[int, str]:
        """Fetch a playlist; premium edges require Chrome TLS impersonation."""
        if premium_playlist_parts(url) is None:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                return resp.status, (await resp.text() if resp.status == 200 else "")
        # The premium edge 403s aiohttp; after the token the segments point to
        # the TikTok CDN, so only the playlist itself needs impersonation.
        curl_headers = {
            "Referer": "https://dembed.st/",
            "Origin": "https://dembed.st",
        }
        for key, value in (headers or {}).items():
            if key.lower() not in {"referer", "origin", "user-agent"}:
                curl_headers[key] = value
        status, body = await _premium_curl_get(
            url, curl_headers, timeout, getattr(self, "_session_proxy", None)
        )
        return status, (body if status == 200 else "")

    async def _is_stream_alive(
        self, session, stream_url: str, headers: dict, budget: float | None = None
    ) -> bool:
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
        # Set when the player answers with content that cannot play (placeholder
        # image, no or undecodable video): unlike a timeout, a retry won't fix it.
        self._content_broken = False

        def _timeout(default: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return min(default, remaining)

        stream_url = await self._with_premium_token(session, stream_url, headers)

        try:
            status, master = await self._fetch_playlist(
                session, stream_url, headers, _timeout(10)
            )
            if status != 200:
                logger.debug("DLStreams: validation of %s -> HTTP %s", stream_url, status)
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
                variant = await self._with_premium_token(session, variant, headers)
                status, current_body = await self._fetch_playlist(
                    session, variant, headers, _timeout(8)
                )
                if status != 200:
                    logger.debug("DLStreams: validation variant %s -> HTTP %s", variant, status)
                    return False
                current_url = variant
                if "#EXTM3U" not in current_body[:8000]:
                    return False
            current_body = decrypt_premium_playlist(current_body, current_url)
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
            try:
                segment_ok = await self._segment_carries_video(
                    session, segment, probe_headers, _timeout(12), encrypted=encrypted
                )
            except asyncio.TimeoutError:
                # Premium segments live on the TikTok CDN: a slow probe is
                # transient and must not discard an otherwise valid playlist.
                if premium_playlist_parts(stream_url) is None:
                    raise
                logger.info(
                    "DLStreams: premium segment probe timed out, accepting playlist"
                )
                segment_ok = True
            if not segment_ok:
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
            first_duration = re.search(r"#EXTINF:\s*([\d.]+)", current_body)
            self._last_health = (
                self._playlist_lag(current_body, time.time()),
                self._segment_load_ratio(
                    getattr(self, "_segment_fetch", None),
                    float(first_duration.group(1)) if first_duration else None,
                ),
            )
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
        
        pin = self._player_preference.get((channel_id, self._route_key()))
        sticky = (
            pin[0] if pin and time.monotonic() - pin[1] <= self.PIN_STICKY_SECONDS else None
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.EXTRACT_BUDGET_SECONDS + (
            self.PIN_RETRIES * (self.PIN_ATTEMPT_BUDGET + self.PIN_RETRY_DELAY) if sticky else 0.0
        )

        def _remaining() -> float:
            return deadline - loop.time()

        def _timeout(default: float) -> float:
            return max(1.0, min(default, _remaining()))

        # The site always shows Player 1 first, even when it is broken, so the
        # watch page is not evidence. Try players in order, Player 1 first.
        # Every candidate gets the one-frame video decode probe, so a broken
        # video track is skipped even when its TS container is valid.
        player_urls = self._prioritize_player_urls(channel_id, requested_url=url)
        # Alive but delayed or stuttering players: kept to fall back on the best.
        fallback = None
        slow_players = 0
        pin_attempts = 0
        pin_tries = 1
        pin_broken = False
        if sticky and player_urls and player_urls[0] == sticky:
            player_urls[1:1] = [sticky] * self.PIN_RETRIES
            pin_tries += self.PIN_RETRIES

        for candidate_index, candidate in enumerate(player_urls):
            is_pin = candidate == sticky
            if is_pin and pin_broken:
                continue
            if is_pin:
                pin_attempts += 1
                if pin_attempts > 1:
                    logger.info(
                        "DLStreams: pinned player %s failed, retry %d/%d before switching",
                        candidate, pin_attempts - 1, self.PIN_RETRIES,
                    )
                    await asyncio.sleep(self.PIN_RETRY_DELAY)
            last_pin_try = is_pin and pin_attempts >= pin_tries
            if _remaining() <= 2.0:
                logger.warning(
                    "DLStreams: extraction budget of %.0fs exhausted after %d/%d players",
                    self.EXTRACT_BUDGET_SECONDS,
                    candidate_index,
                    len(player_urls),
                )
                break
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
                if iframe_src in seen_iframes and not is_pin:
                    logger.debug("DLStreams: iframe %s already attempted, skipping", iframe_src)
                    continue
                seen_iframes.add(iframe_src)

                # Fetch iframe player page
                iframe_headers = headers.copy()
                iframe_headers["Referer"] = candidate
                iframe_headers["Origin"] = self.entry_origin
                
                async with session.get(iframe_src, headers=iframe_headers, timeout=_timeout(self.CANDIDATE_TIMEOUT_SECONDS)) as resp:
                    if resp.status != 200:
                        logger.debug("DLStreams: iframe %s returned status %s", iframe_src, resp.status)
                        continue
                    iframe_html = await resp.text()

                # wideiptv.top wraps the real player in one more iframe.
                if ".m3u8" not in iframe_html and "_econfig" not in iframe_html and "atob(" not in iframe_html:
                    nested = re.search(r'<iframe\s+[^>]*src=["\']([^"\']+)["\']', iframe_html, re.I)
                    if nested:
                        nested_src = urljoin(iframe_src, nested.group(1).strip())
                        iframe_headers["Referer"] = iframe_src
                        async with session.get(nested_src, headers=iframe_headers, timeout=_timeout(self.CANDIDATE_TIMEOUT_SECONDS)) as resp:
                            if resp.status == 200:
                                logger.debug("DLStreams: following nested iframe %s", nested_src)
                                iframe_src = nested_src
                                iframe_html = await resp.text()
                # JSON-escaped URLs ("https:\/\/...") in inline player configs.
                iframe_html = iframe_html.replace("\\/", "/")

                # Extract stream URL from iframe HTML.
                # Supported formats:
                #  1) barecrop.net Player 2: window._econfig='...' (Clappr, scrambled base64)
                #  2) classic: source:window.atob('...') / atob('...')
                #  3) direct: source: 'https://...m3u8'
                #  4) fallback: any https://...m3u8 URL in page
                stream_url = None
                solver_ua = None

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

                if not stream_url and m_cfg:
                    # The xstream player is Turnstile-gated: let the browser solve it.
                    solved = await self._solve_gated_player(candidate)
                    if solved:
                        stream_url, solver_ua = solved
                        logger.info(
                            "DLStreams: Turnstile solver produced stream URL for %s",
                            candidate,
                        )

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
                    "User-Agent": solver_ua or self.base_headers["User-Agent"],
                    "Accept": "*/*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                # Return the first stream that is actually playable from here.
                # A dead Player 1 URL must not hide a working Player 2.
                # The premium edge needs a per-viewer token on the playlist URL,
                # so hand over the tokenized URL (validation bundles it too).
                # Only the premium player gates that token behind Turnstile;
                # its ticket lasts hours, so Chrome opens on a cache miss only.
                premium_ticket = None
                premium_parts = premium_playlist_parts(stream_url)
                if premium_parts and (
                    "dembed.st" in iframe_src or "premiumtv" in iframe_src
                ):
                    ticket_key = (
                        premium_parts[0],
                        premium_parts[1],
                        getattr(self, "last_used_proxy", None) or "direct",
                    )
                    cached_ticket = _premium_tickets.get(ticket_key)
                    if cached_ticket and cached_ticket[1] > time.monotonic():
                        premium_ticket = cached_ticket[0]
                    else:
                        solve_started = time.monotonic()
                        # The embedding player page: loading dembed.st directly
                        # serves a stub without the Turnstile widget.
                        premium_ticket = await self._solve_premium_ticket(candidate)
                        # Chromium runs outside the extraction budget: hand the
                        # remaining work the time the solver consumed.
                        deadline += time.monotonic() - solve_started
                        if premium_ticket:
                            _premium_tickets[ticket_key] = (
                                premium_ticket,
                                _premium_ticket_deadline(premium_ticket),
                            )
                stream_url = await self._with_premium_token(
                    session, stream_url, playback_headers, ticket=premium_ticket
                )
                budget = _remaining()
                if is_pin and not last_pin_try:
                    budget = min(budget, self.PIN_ATTEMPT_BUDGET)
                if not await self._is_stream_alive(
                    session, stream_url, playback_headers, budget=budget
                ):
                    logger.info("DLStreams: stream from %s is dead, trying next player", candidate)
                    pin_broken = is_pin and getattr(self, "_content_broken", False)
                    continue
                lag, load_ratio = self._last_health
                rank = self._player_rank(lag, load_ratio)
                if candidate == sticky or (not rank[0] and rank[1] <= MAX_LIVE_LAG_SECONDS):
                    logger.info("DLStreams: working stream found via %s", candidate)
                    self._pin_player(channel_id, candidate)
                    return self._build_result(stream_url, playback_headers)
                logger.info(
                    "DLStreams: %s alive but lag=%s load=%s, trying next player",
                    candidate,
                    "n/a" if lag is None else f"{lag:.0f}s",
                    "n/a" if load_ratio is None else f"{load_ratio:.2f}",
                )
                if fallback is None or rank < fallback[0]:
                    fallback = (
                        rank, candidate, stream_url, playback_headers,
                        self._last_validated_manifest,
                    )
                slow_players += rank[0]
                if slow_players >= 2:
                    # Two players load slowly: the bottleneck is our own link,
                    # probing more players would only add traffic.
                    logger.info("DLStreams: players load slowly from here, stop probing")
                    break
                continue
                
            except Exception as e:
                logger.debug("DLStreams: direct extraction candidate %s failed: %s", candidate, e)
                if candidate_index == 0 and not is_pin and isinstance(e, asyncio.TimeoutError):
                    # Every candidate shares the dlive.sx origin: when the first
                    # page does not answer, the others will not either.
                    logger.warning("DLStreams: site page timed out, aborting candidate loop")
                    break
                continue

        if fallback:
            _, candidate, stream_url, playback_headers, manifest = fallback
            logger.info("DLStreams: no ideal player, using best available %s", candidate)
            self._pin_player(channel_id, candidate)
            self._last_validated_manifest = manifest
            return self._build_result(stream_url, playback_headers)
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
        return result

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
