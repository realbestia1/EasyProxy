"""
VidXgo extractor.

Decodes the obfuscated player at v.vidxgo.co / vidxgo.* and returns the master
HLS playlist. CDN signed URLs on the .ts segments have a ~5 min TTL. Always
re-fetches the embed page on each extract() call to get fresh tokens. Transforms
the VOD manifest into an "EVENT" / live one by removing `#EXT-X-ENDLIST`, so the
player keeps re-fetching the manifest and EP gets a chance to serve a
freshly-extracted segment URL list before the CDN tokens expire.
"""

import asyncio
import base64
import logging
import re
from urllib.parse import urlparse, parse_qs

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_proxy_for_url, get_connector_for_proxy

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass



def _parse_e_expiry(url: str) -> float | None:
    """Extract the `e=` ms-epoch param from a signed VidXgo CDN URL."""
    try:
        qs = urlparse(url).query
        raw = parse_qs(qs).get("e", [None])[0]
        if not raw:
            return None
        return float(raw) / 1000.0
    except Exception:
        return None

# Default playback domain for headers (Referer/Origin). Can be overridden
# via the `vd_domain=` query parameter forwarded by the addon.
DEFAULT_PLAYBACK_DOMAIN = "https://v.vidxgo.co"

# Header used during the embed page fetch. The site is currently strict about
# this referer; sending the playback origin instead yields an empty body.
EMBED_FETCH_REFERER = "https://altadefinizione.you/"

# Pattern that locates the obfuscated block:
#   var X='KEY',d=atob('B64PAYLOAD'),...
_OBFUSCATED_RE = re.compile(
    r"var\s+\w+\s*=\s*'([^']*)'\s*,\s*d\s*=\s*atob\(\s*'([^']*)'",
    re.S,
)
# Pattern that locates the resolved m3u8 inside the decoded payload.
_CURRENT_SRC_RE = re.compile(r'currentSrc.+?"(https:[^";]+)"', re.S)
# All <script> tags, capturing their inner contents.
_SCRIPT_TAG_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)


class VidXgoExtractor:
    """VidXgo embed -> HLS extractor with auto-refresh manifest."""

    def __init__(self, request_headers: dict, proxies: list = None, extractor_name: str = "vidxgo"):
        self.request_headers = request_headers or {}
        self.extractor_name = extractor_name
        self.proxies = proxies or []
        self.selected_proxy = None
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"

        # Headers used for fetching the embed page.
        # NOTE: the host enforces presence of Sec-Fetch-* headers; without them
        # it returns a 403 "blocked" HTML page even with the right Referer.
        self.embed_headers = {
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) "
                "Gecko/20100101 Firefox/150.0"
            ),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "it-IT,it;q=0.9,en;q=0.8",
            "referer": EMBED_FETCH_REFERER,
            "sec-fetch-dest": "iframe",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "cross-site",
            "upgrade-insecure-requests": "1",
        }

        # Headers used by EP when fetching the m3u8 + segments from the CDN.
        # These are also returned to the player as the per-stream headers.
        # NOTE: the CDN (cdn.v1.media-*.d2b.you) also enforces Sec-Fetch-*
        # validation; without them every signed URL returns 403.
        self.playback_headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "accept": "*/*",
            "accept-language": "it-IT,it;q=0.9,en;q=0.8",
            "referer": f"{DEFAULT_PLAYBACK_DOMAIN}/",
            "origin": DEFAULT_PLAYBACK_DOMAIN,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }

    # ------------------------------------------------------------------ proxies

    def _get_proxies_for_url(self, url: str) -> list[str]:
        ordered = []
        for p in self.proxies:
            if p and p not in ordered:
                ordered.append(p)
        route_proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)
        if route_proxy and route_proxy not in ordered:
            ordered.append(route_proxy)
        return ordered

    # ------------------------------------------------------------------ fetch

    async def _fetch(self, url: str, headers: dict) -> str:
        """GET `url` trying direct first, then each configured proxy."""
        paths = [None] + self._get_proxies_for_url(url)
        last_error = None
        for proxy in paths:
            timeout = ClientTimeout(total=25, connect=10, sock_read=20)
            connector = get_connector_for_proxy(proxy) if proxy else TCPConnector(ssl=False)
            try:
                async with ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.get(url, headers=headers, ssl=False) as resp:
                        resp.raise_for_status()
                        text = await resp.text()
                        self.selected_proxy = proxy
                        return text
            except Exception as e:
                last_error = e
                logger.debug(f"vidxgo fetch failed via {proxy or 'direct'}: {e}")
        raise ExtractorError(f"VidXgo: fetch failed for {url}: {last_error}")

    # ------------------------------------------------------------------ decode

    @staticmethod
    def _decode_embed(html: str) -> str:
        """Reproduce the TS decoder: script[5] -> XOR(key, atob(payload)) -> m3u8."""
        scripts = _SCRIPT_TAG_RE.findall(html or "")
        # The obfuscated block is historically at index 5; fall back to scanning
        # all scripts if the layout changes.
        candidates: list[str] = []
        if len(scripts) > 5:
            candidates.append(scripts[5])
        candidates.extend(s for i, s in enumerate(scripts) if i != 5)

        for script in candidates:
            m = _OBFUSCATED_RE.search(script)
            if not m:
                continue
            key = m.group(1)
            b64_payload = m.group(2)
            if not key or not b64_payload:
                continue
            try:
                decoded = base64.b64decode(b64_payload)
            except Exception:
                continue
            key_bytes = key.encode("utf-8")
            klen = len(key_bytes)
            if klen == 0:
                continue
            xored = bytes(b ^ key_bytes[i % klen] for i, b in enumerate(decoded))
            try:
                decoded_str = xored.decode("utf-8", errors="ignore")
            except Exception:
                continue
            cm = _CURRENT_SRC_RE.search(decoded_str)
            if cm:
                return cm.group(1).replace("\\", "")
        raise ExtractorError("VidXgo: could not locate currentSrc in any decoded script")

    # ------------------------------------------------------------------ manifest transform

    @staticmethod
    def _make_live(manifest_text: str) -> str:
        """
        Turn a VOD playlist into an EVENT/live one so the player re-fetches
        the manifest periodically. This gives EP the chance to call
        extract() again and serve fresh segment URLs.
        """
        if not manifest_text:
            return manifest_text
        # Drop the end-of-stream marker so the player keeps polling.
        # We do NOT touch TARGETDURATION or MEDIA-SEQUENCE: the underlying
        # segment list is identical until the cache expires, so the player
        # treats it as a stalled live stream and just retries the manifest.
        out_lines = []
        for line in manifest_text.splitlines():
            s = line.strip()
            if s == "#EXT-X-ENDLIST":
                continue
            # Force EVENT playlist type (a few players require this hint to
            # keep re-fetching when there's no ENDLIST).
            if s.startswith("#EXT-X-PLAYLIST-TYPE"):
                out_lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
                continue
            out_lines.append(line)
        # If no PLAYLIST-TYPE tag existed, inject one right after #EXTM3U.
        if not any(l.startswith("#EXT-X-PLAYLIST-TYPE") for l in out_lines):
            for i, l in enumerate(out_lines):
                if l.strip() == "#EXTM3U":
                    out_lines.insert(i + 1, "#EXT-X-PLAYLIST-TYPE:EVENT")
                    break
        return "\n".join(out_lines)

    # ------------------------------------------------------------------ public API

    async def extract(self, url: str, **kwargs) -> dict:
        """
        Extract the HLS playlist for a VidXgo embed page.

        `url` is the embed URL, e.g. https://v.vidxgo.co/tt1234567 or
        https://v.vidxgo.co/tt1234567/1/2 for series.
        """
        force_refresh = bool(kwargs.get("force_refresh"))
        background_refresh = bool(kwargs.get("background_refresh"))
        request_headers = kwargs.get("request_headers") or {}

        # Allow the caller (provider) to override the playback domain (Referer/Origin)
        # via vd_domain=… query parameter forwarded by EP. Defaults to v.vidxgo.co.
        vd_domain = (
            kwargs.get("vd_domain")
            or kwargs.get("h_referer")
            or DEFAULT_PLAYBACK_DOMAIN
        )
        vd_domain = vd_domain.rstrip("/")
        if not vd_domain.startswith("http"):
            vd_domain = f"https://{vd_domain}"
        playback_headers = {
            **self.playback_headers,
            "referer": f"{vd_domain}/",
            "origin": vd_domain,
        }

        # 1. Fetch embed page.
        embed_headers = {**self.embed_headers, **{k.lower(): v for k, v in request_headers.items() if k.lower() == "cookie"}}
        html = await self._fetch(url, embed_headers)
        if not html:
            raise ExtractorError(f"VidXgo: empty embed page for {url}")

        # 2. Decode.
        m3u8_url = self._decode_embed(html)
        logger.info(f"vidxgo: extracted m3u8 for {url} -> {m3u8_url[:80]}...")

        # 3. Fetch master + each referenced variant playlist.
        # Override referer/origin to match the actual CDN domain from the m3u8 URL.
        m3u8_parsed = urlparse(m3u8_url)
        m3u8_domain = f"{m3u8_parsed.scheme}://{m3u8_parsed.netloc}"
        cdn_headers = {**playback_headers, "referer": f"{m3u8_domain}/", "origin": m3u8_domain}
        # We keep the manifests as VOD (with ENDLIST) so the player starts
        # from the beginning and seeking works correctly. CDN tokens (~5 min
        # TTL, visible as `e=` ms epoch) are rotated by EP's background
        # refresh loop, and the segment proxy handler rewrites the `t=`/`e=`
        # query of each segment to the latest captured value at fetch time.
        master_text = await self._fetch(m3u8_url, cdn_headers)
        if "#EXTM3U" not in master_text:
            raise ExtractorError("VidXgo: extracted URL did not return a valid HLS manifest")

        # Collect variant URLs (any non-comment non-empty line right after
        # #EXT-X-STREAM-INF). Resolve them against the master URL.
        from urllib.parse import urljoin
        captured_map: dict[str, str] = {}
        master_lines = master_text.splitlines()
        variant_urls: list[str] = []
        for i, line in enumerate(master_lines):
            if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(master_lines):
                raw = master_lines[i + 1].strip()
                if raw and not raw.startswith("#"):
                    variant_urls.append(urljoin(m3u8_url, raw))

        # Also capture audio/subtitle EXT-X-MEDIA playlist URLs
        for line in master_lines:
            if line.startswith("#EXT-X-MEDIA:") and 'URI="' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)
                if uri_start > 4 and uri_end > uri_start:
                    media_url = urljoin(m3u8_url, line[uri_start:uri_end])
                    if media_url not in variant_urls:
                        variant_urls.append(media_url)

        # Fetch variants in parallel; ignore individual failures so the master
        # still plays even if one rendition is broken.
        async def _grab(v_url: str) -> tuple[str, str | None]:
            try:
                txt = await self._fetch(v_url, cdn_headers)
                return v_url, txt
            except Exception as e:
                logger.warning(f"vidxgo: variant fetch failed {v_url[:80]}...: {e}")
                return v_url, None

        if variant_urls:
            results = await asyncio.gather(*[_grab(v) for v in variant_urls])
            for v_url, v_text in results:
                if not v_text:
                    continue
                captured_map[v_url] = v_text

        # Single-variant streams: master IS the variant.
        captured_map[m3u8_url] = master_text

        # 4. Return.
        return {
            "destination_url": m3u8_url,
            "request_headers": cdn_headers,
            "captured_manifest": master_text,
            "captured_manifests": captured_map,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.selected_proxy,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
