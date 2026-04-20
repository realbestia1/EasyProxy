import asyncio
import logging
import re
import time
from urllib.parse import urlparse, urljoin

import aiohttp
from curl_cffi.requests import AsyncSession

from config import BYPARR_URL, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES
from utils.cookie_cache import CookieCache

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class Settings:
    byparr_url = BYPARR_URL

settings = Settings()

_DOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

class DoodStreamExtractor:
    """
    DoodStream / PlayMogo extractor using Byparr for Cloudflare bypass and IP-consistent extraction.
    FlareSolverr is NOT used for this provider as per user request.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers["User-Agent"] = _DOOD_UA
        self.proxies = proxies or []
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.cache = CookieCache("dood")

    def _get_proxy(self, url: str) -> str | None:
        return get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)

    async def extract(self, url: str, **kwargs):
        parsed = urlparse(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        if not video_id:
            raise ExtractorError("Invalid DoodStream URL: no video ID found")

        domain = parsed.netloc
        cached = self.cache.get(domain)
        if cached:
            logger.info(f"DoodStream: Using cached cookies for {domain}")
            try:
                return await self._extract_via_curl_cffi(url, video_id, cookies=cached["cookies"], ua=cached["userAgent"])
            except Exception as e:
                logger.warning(f"DoodStream: Cached cookies failed for {domain}: {e}")

        if settings.byparr_url:
            try:
                return await self._extract_via_byparr(url, video_id)
            except ExtractorError as e:
                logger.error(f"DoodStream: Byparr extraction failed: {e}")
                raise

        return await self._extract_via_curl_cffi(url, video_id)

    async def _request_byparr(self, url: str) -> dict:
        """Performs a request via Byparr (v1 API style for challenge bypass)."""
        if not settings.byparr_url:
            raise ExtractorError("Byparr URL not configured")
        endpoint = f"{settings.byparr_url.rstrip('/')}/v1"
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
        }
        
        # Determina dinamicamente il proxy per questo specifico URL
        proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
        if proxy:
            payload["proxy"] = {"url": proxy}
            logger.debug(f"DoodStream: Passing proxy to Byparr: {proxy}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=75),
                ) as resp:
                    if resp.status != 200:
                        raise ExtractorError(f"Byparr HTTP {resp.status}")
                    data = await resp.json()
            except Exception as e:
                raise ExtractorError(f"Byparr connection failed: {e}")

        if data.get("status") != "ok":
            raise ExtractorError(f"Byparr: {data.get('message', 'unknown error')}")
        
        return data.get("solution", {})

    async def _extract_via_byparr(self, url: str, video_id: str) -> dict:
        embed_url = url if "/e/" in url else f"https://{urlparse(url).netloc}/e/{video_id}"
        solution = await self._request_byparr(embed_url)
        
        final_url = solution.get("url", embed_url)
        base_url = f"https://{urlparse(final_url).netloc}"
        html = solution.get("response", "")
        ua = solution.get("userAgent", _DOOD_UA)
        raw_cookies = solution.get("cookies", [])

        if raw_cookies:
            cookies = {c["name"]: c["value"] for c in raw_cookies}
            self.cache.set(urlparse(url).netloc, cookies, ua)

        if "pass_md5" not in html:
            logger.debug("DoodStream: pass_md5 not found, waiting 2s for Byparr resolution...")
            await asyncio.sleep(2)
            solution = await self._request_byparr(embed_url)
            html = solution.get("response", "")
            if "pass_md5" not in html:
                 raise ExtractorError("DoodStream: Byparr failed to solve the challenge correctly")

        return await self._parse_embed_html(html, base_url, ua, use_byparr=True)

    async def _extract_via_curl_cffi(self, url: str, video_id: str, cookies: dict = None, ua: str = None) -> dict:
        proxy = self._get_proxy(url)
        current_ua = ua or _DOOD_UA
        async with AsyncSession() as s:
            r = await s.get(
                url,
                impersonate="chrome",
                headers={"Referer": f"https://{urlparse(url).netloc}/", "User-Agent": current_ua},
                cookies=cookies or {},
                timeout=30,
                allow_redirects=True,
                **({"proxy": proxy} if proxy else {}),
            )
        html = r.text
        base_url = f"https://{urlparse(str(r.url)).netloc}"

        if "pass_md5" not in html:
            if "turnstile" in html.lower() or "captcha_l" in html:
                if settings.byparr_url:
                    return await self._extract_via_byparr(url, video_id)
            raise ExtractorError(f"DoodStream: pass_md5 not found")

        return await self._parse_embed_html(html, base_url, current_ua)

    async def _parse_embed_html(self, html: str, base_url: str, override_ua: str = None, use_byparr: bool = False) -> dict:
        pass_match = re.search(r"(/pass_md5/[^'\"<>\s]+)", html)
        if not pass_match:
            raise ExtractorError("DoodStream: pass_md5 path not found")

        pass_url = urljoin(base_url, pass_match.group(1))
        ua = override_ua or _DOOD_UA
        
        headers = {
            "User-Agent": ua,
            "Referer": "https://doodstream.com/",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }

        base_stream = None
        if settings.byparr_url:
            logger.info(f"DoodStream: Fetching pass_md5 via Byparr")
            try:
                # Use Byparr's specialized /proxy for text/body extraction
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{settings.byparr_url}/proxy",
                        params={"url": pass_url, "ua": ua, "ref": "https://doodstream.com/"},
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 200:
                            base_stream = await resp.text()
                            base_stream = base_stream.strip()
            except Exception as e:
                logger.warning(f"DoodStream: Byparr /proxy call failed: {e}")

        if not base_stream:
            # Last resort fallback to direct request (might fail due to IP consistency)
            proxy = self._get_proxy(pass_url)
            async with AsyncSession() as s:
                r = await s.get(
                    pass_url,
                    impersonate="chrome",
                    headers=headers,
                    timeout=20,
                    **({"proxy": proxy} if proxy else {}),
                )
            base_stream = r.text.strip()
            
        if not base_stream or "RELOAD" in base_stream:
            raise ExtractorError("DoodStream: pass_md5 endpoint returned no stream URL.")

        token_match = re.search(r"token=([^&\s'\"]+)", html)
        if not token_match:
            token_match = re.search(r"['\"]?token['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
            
        if not token_match:
            raise ExtractorError("DoodStream: token not found")
            
        token = token_match.group(1)
        expiry_match = re.search(r"expiry[:=]\s*['\"]?(\d+)['\"]?", html)
        expiry = expiry_match.group(1) if expiry_match else str(int(time.time()))
        
        import random
        import string
        rand_str = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        final_url = f"{base_stream}{rand_str}?token={token}&expiry={expiry}"

        return {
            "destination_url": final_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        pass
