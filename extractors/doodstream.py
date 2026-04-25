import asyncio
import logging
import os
import random
import re
import string
import time
from urllib.parse import urljoin, urlparse

import cloudscraper
from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_proxy_for_url
from utils.cookie_cache import CookieCache
from utils.proxy_manager import FreeProxyManager

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


_DOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_FREE_PROXY_MAX_FETCH = int(os.environ.get("DOOD_FREE_PROXY_MAX_FETCH", "0")) # 0 = unlimited in Manager
_FREE_PROXY_MAX_GOOD = int(os.environ.get("DOOD_FREE_PROXY_MAX_GOOD", "0"))   # 0 = unlimited in Manager


class DoodStreamExtractor:
    """
    DoodStream / PlayMogo extractor using cloudscraper first, with optional
    auto-refreshed free-proxy fallback using FreeProxyManager.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers["User-Agent"] = _DOOD_UA
        self.proxies = proxies or []
        self.last_used_proxy = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.cache = CookieCache("dood")
        self.proxy_manager = FreeProxyManager.get_instance(
            "dood",
            [
                os.environ.get(
                    "DOOD_FREE_PROXY_URL",
                    "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/all/data.txt",
                ),
                "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
            ],
            cache_ttl=int(os.environ.get("DOOD_FREE_PROXY_CACHE_TTL", "7200")),
            max_fetch=_FREE_PROXY_MAX_FETCH,
            max_good=_FREE_PROXY_MAX_GOOD,
        )

    def _get_proxy(self, url: str) -> str | None:
        return get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)

    def _normalize_proxy_url(self, proxy_value: str) -> str:
        proxy_value = proxy_value.strip()
        if proxy_value.startswith("socks5://"):
            return proxy_value.replace("socks5://", "socks5h://", 1)
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    def _build_scraper_proxies(self, url: str, proxy_url: str | None = None) -> dict | None:
        if not proxy_url and self.proxies:
            proxy_url = self.proxies[0]
        if not proxy_url:
            proxy_url = self._get_proxy(url)
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
        return bool(html and "pass_md5" in html and "makePlay(" in html and "token=" in html)

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

    async def _get_auto_proxy_pool(self, embed_url: str) -> list[str]:
        if os.environ.get("DOOD_ENABLE_FREE_PROXY_POOL", "true").lower() != "true":
            return []

        def probe_sync(proxy_url: str) -> bool:
            try:
                scraper = cloudscraper.create_scraper(delay=2)
                resp = scraper.get(
                    embed_url,
                    headers={"User-Agent": _DOOD_UA},
                    timeout=6,
                    proxies={"http": proxy_url, "https": proxy_url},
                )
                return resp.status_code == 200 and self._is_valid_dood_page(resp.text)
            except Exception:
                return False

        return await self.proxy_manager.get_next_sequence(probe_sync)

    async def _do_extract_with_proxy(self, embed_url: str, scraper_proxies: dict | None) -> dict | None:
        scraper = cloudscraper.create_scraper(delay=5)
        if scraper_proxies:
            self.last_used_proxy = scraper_proxies["https"]
            logger.info(f"DoodStream: cloudscraper using proxy {scraper_proxies['https']}")
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
            logger.info(f"DoodStream Page Title: {title_match.group(1)}")

        if "Just a moment..." in html or "DDoS protection" in html or "cf-browser-verification" in html:
            logger.warning("DoodStream: cloudscraper returned 200 but Cloudflare challenge is present.")

        pass_path = self._extract_pass_path(html)
        token = self._extract_token(html, pass_path)
        if not (pass_path and token):
            self._log_parse_debug(html)
            return None

        pass_url = urljoin(embed_url, pass_path)
        logger.info(f"Cloudscraper found pass_md5 path: {pass_path}")

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

        try:
            logger.info(f"DoodStream: Trying cloudscraper extraction for {embed_url}")

            result = await self._do_extract_with_proxy(
                embed_url,
                self._build_scraper_proxies(embed_url),
            )
            if result:
                return result

            for proxy_url in await self._get_auto_proxy_pool(embed_url):
                logger.info(f"DoodStream: retrying with auto proxy {proxy_url}")
                try:
                    result = await self._do_extract_with_proxy(
                        embed_url,
                        {"http": proxy_url, "https": proxy_url},
                    )
                    if result:
                        return result
                except Exception as proxy_exc:
                    logger.warning(f"DoodStream: auto proxy {proxy_url} failed: {proxy_exc}")
                    self.proxy_manager.report_failure(proxy_url)

            raise ExtractorError("DoodStream: tokens not found after primary and auto-proxy attempts")
        except Exception as e:
            logger.error(f"DoodStream: cloudscraper error: {e}")
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

        logger.info(f"DoodStream successful sniffed extraction: {final_url[:60]}...")
        return {
            "destination_url": final_url,
            "request_headers": {"User-Agent": ua, "Referer": f"{base_url}/", "Accept": "*/*"},
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
        }

    async def close(self):
        pass
