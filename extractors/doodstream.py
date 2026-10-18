import logging
import random
import re
import string
import time
from urllib.parse import urljoin, urlparse

from curl_cffi.requests import AsyncSession
from config import get_preferred_proxy_for_url
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError, normalize_socks_proxy_url

logger = logging.getLogger(__name__)


_DOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

class DoodStreamExtractor(BaseExtractor):
    """
    DoodStream / PlayMogo extractor.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="doodstream", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers["User-Agent"] = _DOOD_UA
        self.proxies = proxies or []
        self.last_used_proxy = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    async def _get_proxy(self, url: str, bypass_warp: bool = None) -> str | None:
        return await get_preferred_proxy_for_url(url, "doodstream", self.proxies, bypass_warp)

    def _extract_pass_path(self, html: str) -> str | None:
        patterns = [
            r"['\"](/pass_md5/[^'\"]+)['\"]",
            r"\.get\(\s*['\"](/pass_md5/[^'\"]+)['\"]",
            r"(/pass_md5/[A-Za-z0-9\-._]+/[A-Za-z0-9]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.I)
            if match:
                return match.group(1)
        return None

    def _extract_token(self, html: str, pass_path: str | None = None) -> str | None:
        if pass_path:
            tail = pass_path.rstrip("/").split("/")[-1]
            if re.fullmatch(r"[A-Za-z0-9]{8,}", tail):
                return tail

        patterns = [
            r"makePlay\(\)\s*\{.*?\?token=([A-Za-z0-9]+)&expiry=",
            r"\?token=([A-Za-z0-9]+)&expiry=",
            r"token=([A-Za-z0-9]+)",
            r"['\"]?token['\"]?\s*[:=]\s*['\"]([A-Za-z0-9]+)['\"]",
            r"window\.[a-z0-9_]+\s*=\s*['\"]([A-Za-z0-9]{20,})['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.I | re.S)
            if match:
                return match.group(1)
        return None

    def _extract_expiry(self, html: str) -> str:
        expiry_match = re.search(r"expiry[:=]\s*['\"]?(\d{10,})['\"]?", html, re.I)
        if expiry_match:
            return expiry_match.group(1)
        if re.search(r"expiry=.*Date\.now\(\)", html, re.I | re.S):
            return str(int(time.time() * 1000))
        return str(int(time.time()))

    def _log_parse_debug(self, html: str) -> None:
        markers = {
            "pass_md5": "pass_md5" in html,
            "makePlay": "makePlay(" in html,
            "token=": "token=" in html,
            "Date.now": "Date.now()" in html,
            "cf-browser-verification": "cf-browser-verification" in html,
            "Just a moment...": "Just a moment..." in html,
        }
        logger.debug(f"DoodStream HTML length: {len(html)} | markers: {markers}")

        for marker in ("pass_md5", "makePlay(", "token="):
            idx = html.find(marker)
            if idx != -1:
                start = max(0, idx - 180)
                end = min(len(html), idx + 320)
                snippet = re.sub(r"\s+", " ", html[start:end]).strip()
                logger.debug(f"DoodStream marker snippet [{marker}]: {snippet}")
                return

        compact_html = re.sub(r"\s+", " ", html[:1200]).strip()
        logger.debug(f"DoodStream compact HTML snippet (first 1200 chars): {compact_html}")

    async def _do_extract_with_proxy(
        self,
        embed_url: str,
        proxy_url: str | None,
        bypass_warp: bool = False,
    ) -> dict | None:
        normalized_proxy = normalize_socks_proxy_url(proxy_url) if proxy_url else None
        if normalized_proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(bypass_warp):
            raise ExtractorError(
                "DoodStream: direct fallback disabled; no proxy route available"
            )
        self.last_used_proxy = normalized_proxy
        logger.info("DoodStream: curl_cffi using %s", normalized_proxy or "direct connection")
        request_kwargs = {}
        if normalized_proxy:
            request_kwargs["proxies"] = {"http": normalized_proxy, "https": normalized_proxy}

        curl_options = _cfg.get_curl_ipv4_options(normalized_proxy).get("curl_options")
        session_kwargs = {"impersonate": "chrome124"}
        if curl_options:
            session_kwargs["curl_options"] = curl_options
        async with AsyncSession(**session_kwargs) as session:
            response = await session.get(
                embed_url,
                headers={"User-Agent": _DOOD_UA},
                timeout=30,
                **request_kwargs,
            )
            html = response.text
            if response.status_code != 200:
                raise ExtractorError(f"DoodStream: failed to fetch embed page (status {response.status_code})")

            title_match = re.search(r"<title>(.*?)</title>", html, re.I)
            if title_match:
                logger.info(f"DoodStream Page Title: {title_match.group(1)}")

            if "Just a moment..." in html or "DDoS protection" in html or "cf-browser-verification" in html:
                raise ExtractorError("DoodStream: Cloudflare challenge detected")

            pass_path = self._extract_pass_path(html)
            token = self._extract_token(html, pass_path)
            if not (pass_path and token):
                self._log_parse_debug(html)
                return None

            pass_url = urljoin(embed_url, pass_path)
            logger.info(f"DoodStream found pass_md5 path: {pass_path}")

            pass_response = await session.get(
                pass_url,
                headers={"Referer": embed_url, "User-Agent": _DOOD_UA},
                timeout=30,
                **request_kwargs,
            )
            pass_text = pass_response.text
            if pass_response.status_code != 200 or len(pass_text) <= 10:
                logger.warning(
                    f"DoodStream: pass_md5 request failed with status {pass_response.status_code} "
                    f"and content: {pass_text[:100]}"
                )
                return None

            logger.info("DoodStream: curl_cffi extraction successful!")
            return self._finalize_extraction(pass_text.strip(), html, embed_url, _DOOD_UA)

    async def extract(self, url: str, **kwargs):
        parsed = urlparse(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        if not video_id:
            raise ExtractorError("Invalid DoodStream URL: no video ID found")

        embed_url = url if "/e/" in url else f"https://{parsed.netloc}/e/{video_id}"

        self._apply_routing_kwargs(embed_url, kwargs)

        try:
            logger.info(f"DoodStream: Trying curl_cffi extraction for {embed_url}")

            # Forced proxy (or WARP-only/direct routing state) wins; else auto chain.
            if self._force_direct:
                preselected = None
            else:
                preselected = self._forced_proxy or await self._get_proxy(embed_url, bypass_warp=self.bypass_warp_active)
            result = await self._do_extract_with_proxy(
                embed_url,
                preselected,
                bypass_warp=self.bypass_warp_active,
            )
            if result:
                return result

            raise ExtractorError("DoodStream: tokens not found after primary attempt")

        except Exception as e:
            logger.error(f"DoodStream: extraction error: {e}")
            raise ExtractorError(f"DoodStream: extraction failed: {e}")

    def _finalize_extraction(self, base_stream: str, html: str, base_url: str, ua: str) -> dict:
        if "RELOAD" in base_stream or len(base_stream) < 5:
            raise ExtractorError(f"DoodStream: Captured pass_md5 is invalid ({base_stream[:20]})")

        token = self._extract_token(html)
        if not token:
            raise ExtractorError("DoodStream: token not found in HTML")

        expiry = self._extract_expiry(html)
        rand_str = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        final_url = f"{base_stream}{rand_str}?token={token}&expiry={expiry}"

        logger.info(f"DoodStream successful sniffed extraction: {final_url[:60]}...")
        return self._result(final_url, {"User-Agent": ua, "Referer": f"{base_url}/", "Accept": "*/*"})

