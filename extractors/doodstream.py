import asyncio
import cloudscraper
import logging
import os
import random
import re
import string
import time
from urllib.parse import urljoin, urlparse

from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_proxy_for_url
from utils.cookie_cache import CookieCache

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


_DOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class DoodStreamExtractor:
    """
    DoodStream / PlayMogo extractor using cloudscraper.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers["User-Agent"] = _DOOD_UA
        self.proxies = proxies or []
        self.last_used_proxy = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.cache = CookieCache("dood")

    def _get_proxy(self, url: str, bypass_warp: bool = None) -> str | None:
        return get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES, bypass_warp=bypass_warp)

    def _normalize_proxy_url(self, proxy_value: str) -> str:
        proxy_value = proxy_value.strip()
        if proxy_value.startswith("socks5://"):
            return proxy_value.replace("socks5://", "socks5h://", 1)
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    def _build_scraper_proxies(self, url: str, proxy_url: str | None = None, bypass_warp: bool = None) -> dict | None:
        if not proxy_url and self.proxies:
            proxy_url = self.proxies[0]
        if not proxy_url:
            proxy_url = self._get_proxy(url, bypass_warp=bypass_warp)
        if not proxy_url:
            return None
        proxy_url = self._normalize_proxy_url(proxy_url)
        self.last_used_proxy = proxy_url
        return {"http": proxy_url, "https": proxy_url}

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

    def _is_valid_dood_page(self, html: str) -> bool:
        if not html: return False
        # Extended markers for newer domains
        markers = ["pass_md5", "makePlay(", "token=", "get_player(", "vtt", "subtitle"]
        return any(m in html for m in markers)

    def _log_parse_debug(self, html: str) -> None:
        markers = {
            "pass_md5": "pass_md5" in html,
            "makePlay": "makePlay(" in html,
            "token=": "token=" in html,
            "Date.now": "Date.now()" in html,
            "cf-browser-verification": "cf-browser-verification" in html,
            "Just a moment...": "Just a moment..." in html,
        }
        logger.debug("DoodStream HTML length: %d | markers: %s", len(html), markers)

        for marker in ("pass_md5", "makePlay(", "token="):
            idx = html.find(marker)
            if idx != -1:
                start = max(0, idx - 180)
                end = min(len(html), idx + 320)
                snippet = re.sub(r"\s+", " ", html[start:end]).strip()
                logger.debug("DoodStream marker snippet [%s]: %s", marker, snippet)
                return

        compact_html = re.sub(r"\s+", " ", html[:1200]).strip()
        logger.debug("DoodStream compact HTML snippet (first 1200 chars): %s", compact_html)

    async def _do_extract_with_proxy(self, embed_url: str, scraper_proxies: dict | None) -> dict | None:
        scraper = cloudscraper.create_scraper(delay=5)
        if scraper_proxies:
            self.last_used_proxy = scraper_proxies["https"]
            logger.info("DoodStream: cloudscraper using proxy %s", scraper_proxies['https'])
        else:
            self.last_used_proxy = None
            logger.info("DoodStream: cloudscraper using direct connection")

        response = await asyncio.to_thread(
            scraper.get,
            embed_url,
            headers={"User-Agent": _DOOD_UA},
            timeout=30,
            proxies=scraper_proxies,
        )
        if response.status_code != 200:
            raise ExtractorError(f"DoodStream: cloudscraper failed to fetch embed page (status {response.status_code})")

        html = response.text
        title_match = re.search(r"<title>(.*?)</title>", html, re.I)
        if title_match:
            logger.info("DoodStream Page Title: %s", title_match.group(1))

        if "Just a moment..." in html or "DDoS protection" in html or "cf-browser-verification" in html:
            logger.warning("DoodStream: cloudscraper returned 200 but Cloudflare challenge is present.")

        pass_path = self._extract_pass_path(html)
        token = self._extract_token(html, pass_path)
        if not (pass_path and token):
            self._log_parse_debug(html)
            return None

        pass_url = urljoin(embed_url, pass_path)
        logger.info("Cloudscraper found pass_md5 path: %s", pass_path)

        pass_response = await asyncio.to_thread(
            scraper.get,
            pass_url,
            headers={"Referer": embed_url, "User-Agent": _DOOD_UA},
            timeout=30,
            proxies=scraper_proxies,
        )
        if pass_response.status_code != 200 or len(pass_response.text) <= 10:
            logger.warning(
                f"DoodStream: pass_md5 request failed with status {pass_response.status_code} "
                f"and content: {pass_response.text[:100]}"
            )
            return None

        logger.info("DoodStream: cloudscraper extraction successful!")
        return self._finalize_extraction(pass_response.text.strip(), html, embed_url, _DOOD_UA)

    async def extract(self, url: str, **kwargs):
        parsed = urlparse(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        if not video_id:
            raise ExtractorError("Invalid DoodStream URL: no video ID found")

        embed_url = url if "/e/" in url else f"https://{parsed.netloc}/e/{video_id}"

        bypass_warp = kwargs.get("bypass_warp")

        try:
            logger.info("DoodStream: Trying cloudscraper extraction for %s", embed_url)

            # 1. First attempt: Use default proxy (WARP if enabled) or user-specified bypass_warp
            result = await self._do_extract_with_proxy(
                embed_url,
                self._build_scraper_proxies(embed_url, bypass_warp=bypass_warp),
            )
            if result:
                return result

            # 2. Fallback: If first attempt failed and we haven't tried bypassing WARP yet, try direct connection
            if not bypass_warp:
                logger.info("DoodStream: first attempt failed, retrying with warp=off (direct) for %s", embed_url)
                result = await self._do_extract_with_proxy(
                    embed_url,
                    self._build_scraper_proxies(embed_url, bypass_warp=True),
                )
                if result:
                    result["bypass_warp"] = True  # Signal to the proxy to keep using direct for segments
                    return result

            raise ExtractorError("DoodStream: tokens not found after primary attempts")

        except Exception as e:
            logger.error("DoodStream: cloudscraper error: %s", e)
            raise ExtractorError(f"DoodStream: cloudscraper extraction failed: {e}")

    def _finalize_extraction(self, base_stream: str, html: str, base_url: str, ua: str) -> dict:
        if "RELOAD" in base_stream or len(base_stream) < 5:
            raise ExtractorError(f"DoodStream: Captured pass_md5 is invalid ({base_stream[:20]})")

        token = self._extract_token(html)
        if not token:
            raise ExtractorError("DoodStream: token not found in HTML")

        expiry = self._extract_expiry(html)
        rand_str = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        final_url = f"{base_stream}{rand_str}?token={token}&expiry={expiry}"

        logger.info("DoodStream successful sniffed extraction: %s...", final_url[:60])
        return {
            "destination_url": final_url,
            "request_headers": {"User-Agent": ua, "Referer": f"{base_url}/", "Accept": "*/*"},
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
        }

    async def close(self):
        pass
