import asyncio
import base64
import codecs
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Dict, Any, Optional

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from extractors.base import BaseExtractor, ExtractorError
from config import get_connector_for_proxy, get_preferred_proxy_for_url
import config as _cfg

logger = logging.getLogger(__name__)

# --- Cryptographic & Protocol Constants ---
AES_KEY = b"a7981cc9eb2f4d19dcfea57b101ecd89"
AES_IV = b"8017d3a8f1400d2f"

DEFAULT_DATA_API_BASE = "https://apis-data10.tcpwe138stya.ru"
DEFAULT_PLAYER_REFERER = "https://nadia01eo.tn76degree12ec3out.cfd/"
DEFAULT_STREAM_DIGIT = "seth"
SITE_URL = os.getenv("FCTV_SITE_URL", "https://www.fctv33hd.vip").rstrip("/")

# sport slug (event page path segment) -> sportType id (from site JS enum: ST_FOOTBALL=1 ...)
SPORT_SLUG_MAP = {
    "football": 1, "basketball": 2, "tennis": 3, "baseball": 4,
    "others": 90, "cricket": 6, "motorsport": 7, "rugby": 8,
    "american-football": 9, "aussie-rules": 10, "hockey": 11,
    "badminton": 12, "volleyball": 13, "fighting": 14,
    "cycling": 15, "handball": 16,
}

# Marker embedded by the playlist generator (fctv33_omg_universal) in the *direct*
# signed stream URL, as a URL fragment (never sent to the CDN, ignored by players):
#   https://cdn.example/token-.../index.m3u8#fctv33&matchId=123&streamId=456&sportType=1&siteType=2001
# It lets EasyProxy recognise a plain playlist entry as an FCTV33 stream and mint a
# fresh token from its own egress IP, without a dedicated /extractor/... link.
_MARKER_RE = re.compile(r'(?:#|%23)fctv33(?![a-z0-9])', re.IGNORECASE)


def has_fctv33_marker(url: str) -> bool:
    return bool(url) and bool(_MARKER_RE.search(str(url)))


def parse_fctv33_marker(url: str) -> dict:
    """Return {matchId, streamId, sportType, siteType} found in the #fctv33 fragment ({} if none)."""
    raw = str(url or "")
    m = _MARKER_RE.search(raw)
    if not m:
        return {}
    frag = raw[m.start():]
    frag = urllib.parse.unquote(frag)
    if frag.startswith("#"):
        frag = frag[1:]
    qs = urllib.parse.parse_qs(frag.replace("fctv33", "", 1).lstrip("&;="), keep_blank_values=False)
    out = {}
    for key in ("matchId", "streamId", "sportType", "siteType"):
        vals = qs.get(key) or qs.get(re.sub(r'(?<!^)([A-Z])', r'_\1', key).lower())
        if vals and vals[0]:
            out[key] = vals[0].strip()
    return out


_EVENT_PAGE_RE = re.compile(r'/([a-z-]+)/([a-z0-9-]+)-(\d+)/([^/?#]+)\.html', re.IGNORECASE)


def parse_event_page(url: str) -> dict | None:
    """Detect mirror event pages like /de/badminton/<league>-<matchId>/<page>.html.

    Returns sport/league/match_id/page slugs, or None. The streamId is NOT
    in these pages (loaded client-side), it is resolved via /api/match/detail.
    """
    try:
        path = urllib.parse.urlsplit(url or "").path or ""
    except Exception:
        return None
    m = _EVENT_PAGE_RE.search(path)
    if not m:
        return None
    return {
        "sport": m.group(1).lower(),
        "league": m.group(2).lower(),
        "match_id": m.group(3),
        "page": m.group(4).lower(),
    }


def _message_strings(buf: bytes, depth: int = 0) -> list:
    """Collect all nested strings of a protobuf message (slug cross-check)."""
    out = []
    if depth > 3 or not isinstance(buf, (bytes, bytearray)):
        return out
    try:
        fields = read_fields(bytes(buf))
    except Exception:
        return out
    for vals in fields.values():
        for v in vals:
            if isinstance(v, int):
                continue
            try:
                out.append(v.decode("utf-8", errors="replace"))
            except Exception:
                pass
            out.extend(_message_strings(v, depth + 1))
    return out


def rot47(text: str) -> str:
    res = []
    for c in text:
        code = ord(c)
        if 0x21 <= code <= 0x4f:
            res.append(chr(code + 0x2f))
        elif 0x50 <= code <= 0x7e:
            res.append(chr(code - 0x2f))
        else:
            res.append(c)
    return "".join(res)


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def encrypt_aes_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.encrypt(pkcs7_pad(data))
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        return encryptor.update(pkcs7_pad(data)) + encryptor.finalize()


def build_signed_stream_url(obfuscated_url: str, session_token: str) -> str:
    decoded = rot47(obfuscated_url)[8:]
    parsed = urllib.parse.urlsplit(decoded)
    encrypted = encrypt_aes_cbc(session_token.encode("utf-8"), AES_KEY, AES_IV)
    token_b64 = base64.b64encode(encrypted).decode("utf-8")
    token = f"{urllib.parse.quote(token_b64, safe='')}a"
    path_with_token = f"/token-{token}{parsed.path}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path_with_token}{query}"


# Salt used for the _s2 segment signature. FCTV33 publishes it in the site config
# (common:cdnSmartLink -> auth.salt). The extractor stores the fetched value here so
# that the stateless signing helpers (used by the manifest rewriter and the proxy
# streamer, which never see the extractor instance) sign with the live salt.
DEFAULT_S2_SALT = "00"
_current_s2_salt = DEFAULT_S2_SALT


def set_current_s2_salt(salt) -> None:
    """Update the shared _s2 salt (ignored when empty)."""
    global _current_s2_salt
    if salt is None or salt == "":
        return
    _current_s2_salt = str(salt)


def get_current_s2_salt() -> str:
    return _current_s2_salt


def decode_rot13_b64(s: str, is_slice: bool = True) -> str:
    """Decode FCTV33 obfuscated parameters (_ctump, _ctuph) via ROT13 and Base64."""
    if not s:
        return s
    s2 = s[8:] if is_slice and len(s) > 8 else s
    try:
        rot = codecs.decode(s2, "rot_13")
        return base64.b64decode(rot).decode("utf-8", errors="replace")
    except Exception:
        return s


def sign_s2_token(url: str, salt: str | None = None) -> str:
    """Sign segment URL with _s2 signature parameter required by FCTV33 TencentEdgeOne CDN."""
    if salt is None:
        salt = _current_s2_salt
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "_s2" in qs:
            return url
        ver = qs.get("_ver", [""])[0]
        if not ver:
            return url
        filename = parsed.path.split("/")[-1]
        payload = f"{filename},{ver},{salt}"
        s2 = hashlib.md5(payload.encode("utf-8")).hexdigest()
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}_s2={s2}"
    except Exception:
        return url


def resolve_fctv33_segment_url(url: str, geo_country: str = "IT", geo_continent: str = "EU", salt: str | None = None) -> str:
    """Resolve FCTV33 segment URL (_ctump / _ctuph smart link) and append _s2 signature."""
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        ctump = qs.get("_ctump", [""])[0]
        ctuph = qs.get("_ctuph", [""])[0]
        if not ctump or not ctuph:
            return sign_s2_token(url, salt=salt)

        decoded_ctump = decode_rot13_b64(ctump)
        decoded_ctuph = decode_rot13_b64(ctuph)

        # Parse host entries: e.g. "EU-IT:cf@host1,EU:cf@host2,WW:cf@host3"
        chosen_host = None
        ww_host = None
        items = decoded_ctump.split(",")
        for item in items:
            if ":" in item and "@" in item:
                ccode, rest = item.split(":", 1)
                _, host = rest.split("@", 1)
                if ccode == f"{geo_continent}-{geo_country}" or ccode == geo_country:
                    chosen_host = host
                    break
                elif ccode == geo_continent and not chosen_host:
                    chosen_host = host
                elif ccode == "WW":
                    ww_host = host

        target_host = chosen_host or ww_host or (items[0].split("@")[-1] if "@" in items[0] else None)
        if not target_host:
            return sign_s2_token(url, salt=salt)

        target_url = f"https://{target_host}{decoded_ctuph}"
        return sign_s2_token(target_url, salt=salt)
    except Exception as e:
        logger.warning(f"Failed to resolve FCTV33 segment URL: {e}")
        return url


def read_varint(buffer: bytes, offset: int = 0):
    value = 0
    shift = 0
    index = offset
    length = len(buffer)
    while index < length:
        byte = buffer[index]
        index += 1
        value |= (byte & 0x7f) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return value, index


def read_length_delimited(buffer: bytes, offset: int = 0):
    length, start = read_varint(buffer, offset)
    return buffer[start:start + length], start + length


def read_fields(buffer: bytes):
    fields = {}
    offset = 0
    length = len(buffer)
    while offset < length:
        tag, next_offset = read_varint(buffer, offset)
        offset = next_offset
        field = tag >> 3
        wire = tag & 0x7
        if wire == 0:
            val, after = read_varint(buffer, offset)
            offset = after
            fields.setdefault(field, []).append(val)
            continue
        if wire == 2:
            chunk, after = read_length_delimited(buffer, offset)
            offset = after
            fields.setdefault(field, []).append(chunk)
            continue
        break
    return fields


def parse_api_envelope(buffer: bytes) -> dict:
    fields = read_fields(buffer)
    msg_chunks = fields.get(3, [])
    msg = ""
    if msg_chunks:
        if isinstance(msg_chunks[0], bytes):
            msg = msg_chunks[0].decode("utf-8", errors="replace")
        elif isinstance(msg_chunks[0], str):
            msg = msg_chunks[0]
    return {
        "message": msg,
        "payload": [c for c in fields.get(10, []) if isinstance(c, bytes)]
    }


def parse_stream_item(buffer: bytes) -> dict:
    fields = read_fields(buffer)
    stream_id_chunk = fields.get(1, [None])[0]
    stream_id = None
    if isinstance(stream_id_chunk, int):
        stream_id = str(stream_id_chunk)
    elif isinstance(stream_id_chunk, bytes):
        if len(stream_id_chunk) <= 8:
            stream_id = str(read_varint(stream_id_chunk, 0)[0])
        else:
            stream_id = stream_id_chunk.decode("utf-8", errors="replace")

    name = ""
    name_chunk = fields.get(3, [None])[0]
    if isinstance(name_chunk, bytes):
        name = name_chunk.decode("utf-8", errors="replace")

    url = ""
    url_chunk = fields.get(4, [None])[0]
    if isinstance(url_chunk, bytes):
        url = url_chunk.decode("utf-8", errors="replace")

    site_type_chunk = fields.get(9, [None])[0]
    site_type = None
    if isinstance(site_type_chunk, int):
        site_type = site_type_chunk
    elif isinstance(site_type_chunk, bytes):
        site_type = read_varint(site_type_chunk, 0)[0]

    return {
        "streamId": stream_id,
        "name": name,
        "url": url,
        "siteType": site_type
    }


def parse_stream_detail(buffer: bytes) -> dict:
    env = parse_api_envelope(buffer)
    if not env["payload"]:
        return {}
    fields = read_fields(env["payload"][0])
    stream_buffer = fields.get(2, [None])[0] or fields.get(1, [None])[0] or env["payload"][0]
    if isinstance(stream_buffer, bytes):
        return parse_stream_item(stream_buffer)
    return {}


def parse_fctv33_target(url: str, **kwargs) -> tuple[str, str, int, int]:
    """
    Parses matchId, streamId, sportType, siteType from various URL formats or parameters.
    Supported inputs:
      - fctv33://{matchId}/{streamId}?sportType={sportType}&siteType={siteType}
      - https://fctv33.stream/match/{matchId}/stream/{streamId}
      - https://fctv33.stream/{matchId}/{streamId}
      - https://www.fctv33hd.rest/live/detail?matchId={matchId}&streamId={streamId}
      - Query params or kwargs: matchId, streamId, sportType, siteType
      - Formats like '{matchId}:{streamId}' or '{matchId}/{streamId}'
    """
    match_id = kwargs.get("matchId") or kwargs.get("match_id")
    stream_id = kwargs.get("streamId") or kwargs.get("stream_id")
    sport_type = kwargs.get("sportType") or kwargs.get("sport_type")
    site_type = kwargs.get("siteType") or kwargs.get("site_type")

    raw = str(url or "").strip()

    # Direct signed URL carrying the #fctv33&matchId=..&streamId=.. marker.
    marker = parse_fctv33_marker(raw)
    if marker:
        match_id = match_id or marker.get("matchId")
        stream_id = stream_id or marker.get("streamId")
        sport_type = sport_type or marker.get("sportType")
        site_type = site_type or marker.get("siteType")
        raw = _MARKER_RE.split(raw, 1)[0]
        if match_id and stream_id:
            raw = ""  # ids resolved, nothing else to parse (the CDN URL has none)

    if "?" in raw:
        path_part, query_part = raw.split("?", 1)
        qs = urllib.parse.parse_qs(query_part)
        if not match_id and ("matchId" in qs or "match_id" in qs):
            match_id = (qs.get("matchId") or qs.get("match_id"))[0]
        if not stream_id and ("streamId" in qs or "stream_id" in qs):
            stream_id = (qs.get("streamId") or qs.get("stream_id"))[0]
        if not sport_type:
            st = qs.get("sportType") or qs.get("sport_type")
            if st:
                try:
                    sport_type = int(st[0])
                except ValueError:
                    pass
        if not site_type:
            st = qs.get("siteType") or qs.get("site_type")
            if st:
                try:
                    site_type = int(st[0])
                except ValueError:
                    pass
        raw = path_part

    if not match_id or not stream_id:
        m = re.search(r'match[/_-](\d+)[/_-]stream[/_-](\d+)', raw, re.IGNORECASE)
        if m:
            match_id, stream_id = m.group(1), m.group(2)
        else:
            m = re.search(r'(?:fctv33://|fctv://|https?://[^/]+/)(?:match/)?(\d+)/(?:stream/)?(\d+)', raw, re.IGNORECASE)
            if m:
                match_id, stream_id = m.group(1), m.group(2)
            else:
                m = re.search(r'^(\d+)[:/](\d+)$', raw)
                if m:
                    match_id, stream_id = m.group(1), m.group(2)
                else:
                    qs = urllib.parse.parse_qs(raw)
                    if not match_id and ("matchId" in qs or "match_id" in qs):
                        match_id = (qs.get("matchId") or qs.get("match_id"))[0]
                    if not stream_id and ("streamId" in qs or "stream_id" in qs):
                        stream_id = (qs.get("streamId") or qs.get("stream_id"))[0]

    try:
        final_sport_type = int(sport_type) if sport_type is not None else 1
    except (ValueError, TypeError):
        final_sport_type = 1

    try:
        final_site_type = int(site_type) if site_type is not None else 2001
    except (ValueError, TypeError):
        final_site_type = 2001

    return str(match_id or "").strip(), str(stream_id or "").strip(), final_sport_type, final_site_type


class Fctv33Extractor(BaseExtractor):
    """
    Native FCTV33 stream extractor for EasyProxy.
    Fetches real-time rb-session and signed stream tokens directly from the FCTV33 data API
    using EasyProxy's egress IP, bypassing CDN IP-binding blocks (487 / 471).
    """

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="fctv33")
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        self.data_api_base = os.getenv("FCTV_DATA_API", DEFAULT_DATA_API_BASE)
        self.player_referer = DEFAULT_PLAYER_REFERER
        self.stream_digit = DEFAULT_STREAM_DIGIT
        self.salt = "00"
        self.geo = {"country": "IT", "continent": "EU"}
        self._params_ts = 0
        self._init_lock = asyncio.Lock()
        self.mediaflow_endpoint = "hls_proxy"
        self.bypass_warp_active = bool(bypass_warp)
        self.last_used_proxy = None
        self._forced_proxy = None
        self._force_direct = False

    async def _get_session(self, url: str = None):
        # ponytail: explicit bypass_warp + per-request forced proxy (proxy_streaming re-extract passes proxy=forced_proxy as kwarg, not via context)
        forced = getattr(self, "_forced_proxy", None)
        force_direct = bool(getattr(self, "_force_direct", False))
        if force_direct:
            proxy_url = None
        elif forced:
            proxy_url = forced
            if proxy_url and _cfg.is_warp_proxy_url(proxy_url) and self.bypass_warp_active:
                proxy_url = None
        else:
            proxy_url = await get_preferred_proxy_for_url(url, self.extractor_name, self.proxies or _cfg.GLOBAL_PROXIES, self.bypass_warp_active)
        if proxy_url is None and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError("FCTV33: direct fallback disabled; no proxy route available")
        async with self._session_lock:
            self.session = self._route_sessions.get(proxy_url)
            self._session_proxy = proxy_url
            if self.session is None or self.session.closed:
                timeout = ClientTimeout(total=60, connect=30, sock_read=30)
                connector = get_connector_for_proxy(proxy_url) if proxy_url else TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=15, enable_cleanup_closed=True, use_dns_cache=True)
                self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["User-Agent"]})
                self._route_sessions[proxy_url] = self.session
            self.last_used_proxy = proxy_url
            return self.session

    async def _refresh_params_if_needed(self):
        now = time.time()
        if now - self._params_ts < 3600:
            return

        async with self._init_lock:
            if now - self._params_ts < 3600:
                return
            try:
                session = await self._get_session(self.data_api_base)
                cfg_url = f"{self.data_api_base}/api/common/params"
                headers = {
                    "User-Agent": self.base_headers["User-Agent"],
                    "Referer": f"{SITE_URL}/",
                    "Origin": SITE_URL,
                }
                async with session.get(cfg_url, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        cfg = json.loads(rot47(text))
                        web_clients = json.loads(cfg.get("common:web:client", "{}"))
                        for digit in ["seth", "trd", "sith", "foth", "fith"]:
                            domain_list = web_clients.get(digit, {}).get("iframePlayerDomains", [])
                            if domain_list:
                                self.stream_digit = digit
                                self.player_referer = f"https://{domain_list[0].rstrip('/')}/"
                                logger.info(f"Fctv33Extractor: Updated stream_digit={digit}, referer={self.player_referer}")
                                break
                        try:
                            smart_link = json.loads(cfg.get("common:cdnSmartLink", "{}"))
                            auth_salt = smart_link.get("auth", {}).get("salt")
                            if auth_salt:
                                self.salt = auth_salt
                                set_current_s2_salt(auth_salt)
                                logger.info("Fctv33Extractor: Updated _s2 salt")
                        except Exception:
                            pass
                        self._params_ts = now
            except Exception as e:
                logger.warning(f"Fctv33Extractor: Failed to refresh player params ({e}), using defaults")
                self._params_ts = now - 3300  # retry sooner on failure

    async def _resolve_event_stream(self, url: str, sport_type: int, site_type: int,
                                      explicit_sport, session, headers) -> tuple:
        """Resolve mirror event pages (matchId in slug, no streamId) via /api/match/detail."""
        ev = parse_event_page(url)
        if not ev:
            raise ExtractorError(
                f"FCTV33: Missing matchId or streamId from URL: {url} "
                f"(need /match/<matchId>/stream/<streamId> or an event page like /<sport>/<league>-<matchId>/<page>.html)"
            )
        match_id = ev["match_id"]
        if explicit_sport is not None:
            try:
                sport_type = int(explicit_sport)
            except (ValueError, TypeError):
                raise ExtractorError(f"FCTV33: invalid sportType '{explicit_sport}'")
        else:
            mapped = SPORT_SLUG_MAP.get(ev["sport"])
            if mapped is None:
                raise ExtractorError(
                    f"FCTV33: unknown sport '{ev['sport']}' in event URL, pass ?sportType=N (URL: {url})"
                )
            sport_type = mapped
        params = {
            "matchId": match_id,
            "sportType": str(sport_type),
            "digit": self.stream_digit,
            "country": self.geo.get("country", "IT"),
            "continent": self.geo.get("continent", "EU"),
        }
        async with session.get(f"{self.data_api_base}/api/match/detail", params=params,
                               headers=headers, timeout=ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise ExtractorError(f"FCTV33 match API returned HTTP {resp.status}")
            content = await resp.read()
        env = parse_api_envelope(content)
        if env.get("message") != "Success" or not env["payload"]:
            raise ExtractorError(f"FCTV33 match API error message: {env.get('message')}")
        fields = read_fields(env["payload"][0])
        match_msg = fields.get(1, [None])[0]
        if not isinstance(match_msg, bytes):
            raise ExtractorError("FCTV33 match API returned no match info")
        try:
            returned_id = read_fields(match_msg).get(1, [None])[0]
        except Exception:
            returned_id = None
        if str(returned_id) != str(match_id):
            raise ExtractorError(
                f"FCTV33: match API returned matchId={returned_id} for requested {match_id} "
                f"(wrong sportType? pass ?sportType=N)"
            )
        strs = _message_strings(match_msg)
        if ev["league"] not in strs and ev["page"] not in strs:
            raise ExtractorError(
                f"FCTV33: match API response does not match event page (wrong sportType? pass ?sportType=N)"
            )
        items = [parse_stream_item(b) for b in fields.get(2, []) if isinstance(b, bytes)]
        items = [it for it in items if it.get("streamId")]
        if not items:
            raise ExtractorError(f"FCTV33: match {match_id} lists no streams")
        if len(items) > 1:
            logger.info("Fctv33Extractor: match=%s lists %d streams, using first '%s' (streamId=%s)",
                        match_id, len(items), items[0].get("name"), items[0].get("streamId"))
        item = items[0]
        return match_id, item["streamId"], sport_type, item.get("siteType") or site_type or 2001

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        match_id, stream_id, sport_type, site_type = parse_fctv33_target(url, **kwargs)
        need_event = not match_id or not stream_id

        # Merge per-request routing (proxy_streaming re-extract) + contexts + cached flag, like Cinejoy
        raw_proxy = kwargs.get("proxy")
        bypass_proxies = str(raw_proxy or "").lower() in {"off", "none", "no"} or _cfg.BYPASS_PROXIES_CONTEXT.get()
        bypass_warp = bool(kwargs.get("bypass_warp") or str(kwargs.get("warp", "")).lower() == "off" or _cfg.BYPASS_WARP_CONTEXT.get() or self.bypass_warp_active)
        self.bypass_warp_active = bypass_warp
        direct_requested = str(kwargs.get("direct", "")).lower() in {"1", "true", "yes", "on"} or (bypass_proxies and bypass_warp)
        if direct_requested or (bypass_proxies and bypass_warp):
            self._forced_proxy, self._force_direct = None, True
        elif bypass_proxies:
            self._forced_proxy, self._force_direct = (_cfg.WARP_PROXY_URL if _cfg._get_dynamic_warp_enabled() else None), False
            if not self._forced_proxy:
                self._force_direct = True
        elif raw_proxy and str(raw_proxy).lower() not in {"on", "auto", "true", ""}:
            self._forced_proxy, self._force_direct = str(raw_proxy), False
        else:
            self._forced_proxy, self._force_direct = None, False
        # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
        self._forced_proxy = _cfg.effective_forced_proxy(url, self._forced_proxy)

        await self._refresh_params_if_needed()

        data_api = self.data_api_base
        session = await self._get_session(data_api)
        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": self.player_referer,
            "Origin": self.player_referer.rstrip("/"),
            "Accept": "*/*",
        }

        if need_event:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            explicit_sport = (kwargs.get("sportType") or kwargs.get("sport_type")
                              or (qs.get("sportType") or qs.get("sport_type") or [None])[0])
            match_id, stream_id, sport_type, site_type = await self._resolve_event_stream(
                url, sport_type, site_type, explicit_sport, session, headers)

        endpoint = f"{data_api}/api/stream/detail"
        params = {
            "streamId": stream_id,
            "matchId": match_id,
            "sportType": str(sport_type),
            "siteType": str(site_type),
            "digit": self.stream_digit,
            "country": self.geo.get("country", "IT"),
            "continent": self.geo.get("continent", "EU"),
        }

        try:
            async with session.get(endpoint, params=params, headers=headers, timeout=ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ExtractorError(f"FCTV33 API returned HTTP {resp.status}")

                session_token = resp.headers.get("rb-session")
                if not session_token:
                    raise ExtractorError("FCTV33 API did not return rb-session header")

                content = await resp.read()
                env = parse_api_envelope(content)
                if env.get("message") != "Success":
                    raise ExtractorError(f"FCTV33 API error message: {env.get('message')}")

                detail = parse_stream_detail(content)
                obfuscated_url = detail.get("url")
                if not obfuscated_url:
                    raise ExtractorError("FCTV33 API returned empty stream URL")

                signed_url = build_signed_stream_url(obfuscated_url, session_token)
                logger.info(f"Fctv33Extractor: Resolved stream for match={match_id} stream={stream_id} -> {signed_url[:70]}...")

                return {
                    "destination_url": signed_url,
                    "request_headers": {
                        "User-Agent": self.base_headers["User-Agent"],
                        "Referer": self.player_referer,
                        "Origin": self.player_referer.rstrip("/"),
                    },
                    "mediaflow_endpoint": "hls_proxy",
                    "selected_proxy": self.last_used_proxy,
                    "force_direct": self._force_direct,
                    "bypass_warp": self.bypass_warp_active,
                }
        except ExtractorError:
            raise
        except Exception as e:
            logger.error(f"Fctv33Extractor error: {e}", exc_info=True)
            raise ExtractorError(f"FCTV33 extraction failed: {e}")
