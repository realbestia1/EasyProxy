import base64
import json
import logging
import re
import struct
import time
from urllib.parse import parse_qsl, urlencode, urlparse

from curl_cffi.requests import AsyncSession
from nacl.secret import SecretBox

from config import get_preferred_proxy_for_url
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError, normalize_socks_proxy_url

logger = logging.getLogger(__name__)

_KEY = bytes.fromhex("c75136c5668bbfe65a7ecad431a745db68b5f381555b38d8f6c699449cf11fcd")
_NONCE = bytes(24)
_MOVIE_PATH_RE = re.compile(r"^/movie/(\d+)/?$")
_TV_PATH_RE = re.compile(r"^/tv/(\d+)/(\d+)/(\d+)/?$")
_RELAY_ORIGIN = "https://noon.mooncase.online"
_SIGNED_QUERY_KEYS = {"auth", "expires", "hash", "key", "sign", "t", "token"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)


class VidLinkExtractor(BaseExtractor):
    """Resolve VidLink movie/TV embeds to their highest-quality stream."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vidlink", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}

    @staticmethod
    def _encrypt_token(media_id: str) -> str:
        timestamp = int(time.time() + 480)
        message = media_id.encode("utf-8") + struct.pack(">Q", timestamp)
        ciphertext = SecretBox(_KEY).encrypt(message, _NONCE).ciphertext
        return base64.urlsafe_b64encode(_NONCE + ciphertext).decode().rstrip("=")

    @staticmethod
    def _pick_stream(data: dict) -> tuple[str, dict, bool, str]:
        stream = data.get("stream") if isinstance(data, dict) else None
        if isinstance(stream, str) and stream.startswith("http"):
            return stream, {}, False, ""
        if not isinstance(stream, dict):
            raise ExtractorError("VidLink: API response has no stream")

        playlist = stream.get("playlist")
        if isinstance(playlist, str) and playlist.startswith("http"):
            return (
                playlist,
                stream.get("playlistHeaders") or {},
                stream.get("requiresProxy") is True,
                str(stream.get("deliveryType") or ""),
            )

        qualities = stream.get("qualities") or {}
        candidates = []
        for label, value in qualities.items():
            if isinstance(value, str):
                value = {"url": value}
            if not isinstance(value, dict) or not str(value.get("url", "")).startswith("http"):
                continue
            height_match = re.search(r"\d+", str(label))
            height = int(height_match.group()) if height_match else 0
            candidates.append((height, value))

        if candidates:
            selected = max(candidates, key=lambda item: item[0])[1]
            return (
                selected["url"],
                selected.get("headers") or {},
                selected.get("requiresProxy") is True,
                str(selected.get("type") or ""),
            )

        url = stream.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return (
                url,
                stream.get("headers") or {},
                stream.get("requiresProxy") is True,
                str(stream.get("deliveryType") or stream.get("type") or ""),
            )
        raise ExtractorError("VidLink: no playable stream in API response")

    @staticmethod
    def _relay_url(url: str, headers: dict, delivery_type: str) -> str:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        query = parse_qsl(parsed.query, keep_blank_values=True)

        if delivery_type == "dash":
            cookie = next(
                (value for key, value in headers.items() if key.lower() == "cookie"),
                None,
            )
            if not cookie:
                raise ExtractorError("VidLink: DASH relay cookie missing")
            encoded_cookie = base64.urlsafe_b64encode(cookie.encode()).decode().rstrip("=")
            query.extend((("host", origin), ("sc", encoded_cookie)))
            relay_path = "sacdn"
        else:
            if delivery_type == "mp4":
                query = [(key, value) for key, value in query if key.lower() in _SIGNED_QUERY_KEYS]
                relay_path = "mp"
            else:
                query = [(key, value) for key, value in query if key.lower() not in {"headers", "host"}]
                relay_path = "proxy"
            serialized_headers = json.dumps(headers, separators=(",", ":"), sort_keys=True)
            query.extend((("headers", serialized_headers), ("host", origin)))

        return f"{_RELAY_ORIGIN}/{relay_path}{parsed.path}?{urlencode(query)}"

    async def extract(self, url: str, **kwargs) -> dict:
        path = urlparse(url).path
        movie_match = _MOVIE_PATH_RE.fullmatch(path)
        tv_match = _TV_PATH_RE.fullmatch(path)
        if movie_match:
            token = self._encrypt_token(movie_match.group(1))
            api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=1"
        elif tv_match:
            token = self._encrypt_token(tv_match.group(1))
            season, episode = tv_match.group(2), tv_match.group(3)
            api_url = f"https://vidlink.pro/api/b/tv/{token}/{season}/{episode}?multiLang=1"
        else:
            raise ExtractorError("VidLink: invalid movie/TV URL")
        headers = {
            "Origin": "https://vidlink.pro",
            "Referer": "https://vidlink.pro/",
            "X-Playback-Environment": "dash-hevc",
        }
        self._apply_routing_kwargs(api_url, kwargs)
        if self._force_direct:
            proxy = None
        else:
            proxy = _cfg.effective_forced_proxy(api_url, self._forced_proxy) or await get_preferred_proxy_for_url(
                api_url, self.extractor_name, self.proxies, self.bypass_warp_active
            )
        if proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError(
                "VidLink: direct fallback disabled; no proxy route available"
            )
        request_kwargs = {}
        if proxy:
            proxy = normalize_socks_proxy_url(proxy)
            request_kwargs["proxies"] = {"http": proxy, "https": proxy}

        try:
            curl_options = _cfg.get_curl_ipv4_options(proxy).get("curl_options")
            session_kwargs = {"impersonate": "chrome124"}
            if curl_options:
                session_kwargs["curl_options"] = curl_options
            async with AsyncSession(
                **session_kwargs,
            ) as session:
                response = await session.get(
                    api_url, headers=headers, timeout=30, **request_kwargs
                )
        except Exception as exc:
            raise ExtractorError(f"VidLink: API request failed: {exc}") from exc
        if response.status_code != 200:
            raise ExtractorError(f"VidLink: API returned HTTP {response.status_code}")

        try:
            stream_url, stream_headers, requires_proxy, delivery_type = self._pick_stream(
                response.json()
            )
        except ValueError as exc:
            raise ExtractorError("VidLink: invalid API response") from exc

        if requires_proxy:
            stream_url = self._relay_url(stream_url, stream_headers, delivery_type)

        self.last_used_proxy = proxy
        playback_headers = {
            "User-Agent": _UA,
            "Origin": "https://vidlink.pro",
            "Referer": "https://vidlink.pro/",
        }
        path = urlparse(stream_url).path.lower()
        if delivery_type == "dash" or path.endswith(".mpd"):
            endpoint = "mpd_manifest_proxy"
        elif path.endswith(".mp4"):
            endpoint = "proxy_stream_endpoint"
        else:
            endpoint = self.mediaflow_endpoint
        logger.info("VidLink: extracted %s", stream_url[:90])
        force_direct = self._force_direct
        return {
            "destination_url": stream_url,
            "request_headers": playback_headers,
            "mediaflow_endpoint": endpoint,
            "selected_proxy": proxy,
            "force_direct": force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

