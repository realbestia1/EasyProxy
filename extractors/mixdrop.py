import asyncio
import cloudscraper
import logging
import re
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

from config import (
    get_proxy_for_url,
    TRANSPORT_ROUTES,
    GLOBAL_PROXIES,
)
from utils.cookie_cache import CookieCache

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


class MixdropExtractor:
    _result_cache = {}

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
            self.base_headers[
                "User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or GLOBAL_PROXIES
        self.cookie_cache = CookieCache("universal")
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp
        self._scraper = None

    def _get_scraper(self):
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper(delay=5)
        return self._scraper

    def _step_headers(self, ua: str, referer: str | None = None) -> dict:
        headers = {"User-Agent": ua}
        if referer:
            headers["Referer"] = referer
        return headers

    def _unpack(self, packed_js: str) -> str:
        try:
            match = re.search(r'}\(\'(.*)\',(\d+),(\d+),\'(.*)\'\.split\(\'\|\'\)', packed_js)
            if not match:
                match = re.search(r'\}\(([\s\S]*?),\s*(\d+),\s*(\d+),\s*\'([\s\S]*?)\'\.split\(\'\|\'\)', packed_js)
            if not match: return packed_js
            p, a, c, k = match.groups()
            p = p.strip("'\"")
            a, c, k = int(a), int(c), k.split('|')

            def e(c):
                res = ""
                if c >= a: res = e(c // a)
                return res + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"[c % a]

            d = {e(i): (k[i] if k[i] else e(i)) for i in range(c)}
            for i in range(c):
                if str(i) not in d: d[str(i)] = k[i] if k[i] else str(i)
            return re.sub(r'\b(\w+)\b', lambda m: d.get(m.group(1), m.group(1)), p)
        except Exception as e:
            logger.debug("Unpack failed: %s", e)
            return packed_js

    async def extract(self, url: str, **kwargs) -> dict:
        normalized_url = url.strip().replace(" ", "%20")
        cache_key = (normalized_url, self.bypass_warp_active)
        if cache_key in MixdropExtractor._result_cache:
            result, timestamp = MixdropExtractor._result_cache[cache_key]
            if time.time() - timestamp < 600:
                logger.info("🚀 [Cache Hit] Using cached extraction result for: %s", normalized_url)
                return result

        logger.info("🔍 [Cache Miss] Extracting new link for: %s", normalized_url)
        proxy = get_proxy_for_url(normalized_url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
        try:
            ua, cookies = self.base_headers.get("User-Agent"), {}
            if "/f/" in url: url = url.replace("/f/", "/e/")
            if "/mix/" in url: url = url.replace("/mix/", "/e/")

            mirrors = [
                url,
                url.replace("mixdrop.co", "mixdrop.vip"),
                url.replace("mixdrop.co", "m1xdrop.bz"),
                url.replace("mixdrop.co", "mixdrop.ch"),
                url.replace("mixdrop.co", "mixdrop.ps"),
                url.replace("mixdrop.co", "mixdrop.ag"),
            ]

            def _build_cs_proxies(pref_p):
                if not pref_p:
                    return None
                return {"http": pref_p, "https": pref_p}

            async def solve_url(current_url, depth=0):
                if depth > 3: return None
                try:
                    m_headers = self._step_headers(ua, current_url)
                    pref_p = get_proxy_for_url(current_url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
                    cs_proxies = _build_cs_proxies(pref_p)

                    async def fetch_page():
                        try:
                            scraper = self._get_scraper()
                            resp = await asyncio.to_thread(
                                scraper.get, current_url,
                                headers=m_headers,
                                timeout=30,
                                proxies=cs_proxies,
                            )
                            if resp.status_code == 200:
                                t = resp.text
                                if not any(m in t.lower() for m in ["cf-challenge", "robot", "checking your browser"]):
                                    return t, str(resp.url), ua, dict(resp.cookies)
                        except:
                            pass
                        return None

                    async def _process_result(res):
                        if not res or not res[0]:
                            return None
                        html, final_url, ua_res, new_cookies = res
                        cookies.update(new_cookies)

                        if "eval(function(p,a,c,k,e,d)" in html:
                            for block in re.findall(r'eval\(function\(p,a,c,k,e,d\).*?\}\(.*\)\)', html, re.S):
                                html += "\n" + self._unpack(block)

                        patterns = [
                            r'(?:MDCore|vsConfig)\.wurl\s*=\s*["\']([^"\']+)["\']',
                            r'source\s*src\s*=\s*["\']([^"\']+)["\']',
                            r'file:\s*["\']([^"\']+)["\']',
                            r'["\'](https?://[^\s"\']+\.(?:mp4|m3u8)[^\s"\']*)["\']',
                            r'wurl\s*:\s*["\']([^"\']+)["\']'
                        ]
                        for p in patterns:
                            match = re.search(p, html)
                            if match:
                                v_url = match.group(1)
                                if v_url.startswith("//"): v_url = "https:" + v_url
                                return self._build_result(v_url, final_url, ua_res, cookies=cookies)

                        soup = BeautifulSoup(html, "lxml")
                        iframe = soup.find("iframe", src=re.compile(r'/e/|/emb', re.I))
                        if iframe:
                            iframe_url = urljoin(final_url, iframe["src"])
                            return await solve_url(iframe_url, depth + 1)

                        return None

                    direct_res = await fetch_page()
                    result = await _process_result(direct_res)
                    if result:
                        return result
                except:
                    pass
                return None

            mirror_tasks = [asyncio.create_task(solve_url(m)) for m in mirrors]
            for mt in asyncio.as_completed(mirror_tasks):
                result = await mt
                if result:
                    MixdropExtractor._result_cache[cache_key] = (result, time.time())
                    return result

            raise ExtractorError("Mixdrop: Video source not found")
        finally:
            pass

    def _build_result(self, video_url: str, referer: str, ua: str, cookies: dict = None) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        if cookies:
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint,
                "bypass_warp": self.bypass_warp_active}

    async def close(self):
        self._scraper = None
