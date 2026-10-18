import asyncio
import logging
import re
import time
from urllib.parse import urlparse, urljoin

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

class MixdropExtractor(BaseExtractor):
    _result_cache = {}
    _cache_ttl = 600
    _cache_max_entries = 30

    @classmethod
    def _prune_result_cache(cls):
        now = time.time()
        expired = [key for key, (_, ts) in cls._result_cache.items() if now - ts >= cls._cache_ttl]
        for key in expired:
            cls._result_cache.pop(key, None)
        while len(cls._result_cache) > cls._cache_max_entries:
            oldest = min(cls._result_cache, key=lambda k: cls._result_cache[k][1])
            cls._result_cache.pop(oldest, None)

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="mixdrop", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
             self.base_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp

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
            logger.debug(f"Unpack failed: {e}")
            return packed_js

    async def extract(self, url: str, **kwargs) -> dict:
        normalized_url = url.strip().replace(" ", "%20")
        self._apply_routing_kwargs(normalized_url, kwargs)
        cache_key = (normalized_url, self._forced_proxy, self._force_direct, self.bypass_warp_active)
        MixdropExtractor._prune_result_cache()
        if cache_key in MixdropExtractor._result_cache:
            result, timestamp = MixdropExtractor._result_cache[cache_key]
            if time.time() - timestamp < MixdropExtractor._cache_ttl:
                logger.info(f"🚀 [Cache Hit] Using cached extraction result for: {normalized_url}")
                self.last_used_proxy = result.get("selected_proxy")
                return dict(result)

        logger.info(f"🔍 [Cache Miss] Extracting new link for: {normalized_url}")
        await self._resolve_proxy(normalized_url)
        try:
            ua = self.base_headers.get("User-Agent")
            parsed = urlparse(url)
            path = parsed.path.rstrip("/")
            for old in ("/f/", "/mix/", "/emb/"):
                if old in path: path = path.replace(old, "/e/"); break
            qs = f"?{parsed.query}" if parsed.query else ""

            mixdrop_domains = [
                "mxdrop.top", "m1xdrop.bz", "m1xdrop.net",
                "mixdrop.co", "mixdrop.vip",
                "mixdrop.ch", "mixdrop.ps", "mixdrop.ag",
            ]
            mirrors = [f"{parsed.scheme}://{d}{path}{qs}" for d in mixdrop_domains]
            
            def _build_cs_proxies(pref_p):
                if not pref_p:
                    return None
                return {"http": pref_p, "https": pref_p}

            async def solve_url(current_url, depth=0, task_cookies=None):
                if depth > 3: return None
                local_cookies = dict(task_cookies) if task_cookies else {}
                try:
                    m_headers = self._step_headers(ua, current_url)
                    pref_p = await self._resolve_proxy(current_url)
                    cs_proxies = _build_cs_proxies(pref_p)
                    
                    async def fetch_page():
                        try:
                            async with AsyncSession(
                                impersonate="chrome120",
                                curl_options=_cfg.get_curl_ipv4_options(pref_p).get("curl_options") or {},
                            ) as s:
                                resp = await s.get(
                                    current_url,
                                    headers=m_headers,
                                    timeout=30,
                                    proxies=cs_proxies,
                                )
                                if resp.status_code == 200:
                                    t = resp.text
                                    if not any(m in t.lower() for m in ["cf-challenge", "robot", "checking your browser"]):
                                        return t, str(resp.url), ua, dict(resp.cookies)
                        except Exception as e:
                            logger.debug("Mixdrop fetch_page attempt failed: %s", e)
                            pass
                        return None

                    async def _process_result(res):
                        if not res or not res[0]:
                            return None
                        html, final_url, ua_res, new_cookies = res
                        local_cookies.update(new_cookies)
                        
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
                                return self._build_result(v_url, final_url, ua_res, cookies=local_cookies, selected_proxy=pref_p)

                        soup = BeautifulSoup(html, "lxml")
                        iframe = soup.find("iframe", src=re.compile(r'/e/|/emb', re.I))
                        if iframe:
                            iframe_url = urljoin(final_url, iframe["src"])
                            return await solve_url(iframe_url, depth + 1, local_cookies)

                        return None

                    direct_res = await fetch_page()
                    result = await _process_result(direct_res)
                    if result:
                        return result
                except Exception as e:
                    logger.debug("Mixdrop mirror attempt failed: %s", e)
                    pass
                return None

            mirror_tasks = [asyncio.create_task(solve_url(m)) for m in mirrors]
            try:
                for mt in asyncio.as_completed(mirror_tasks):
                    result = await mt
                    if result:
                        self.last_used_proxy = result.get("selected_proxy")
                        MixdropExtractor._result_cache[cache_key] = (dict(result), time.time())
                        MixdropExtractor._prune_result_cache()
                        return dict(result)
            finally:
                for mt in mirror_tasks:
                    if not mt.done():
                        mt.cancel()
                await asyncio.gather(*mirror_tasks, return_exceptions=True)

            raise ExtractorError("Mixdrop: Video source not found")
        finally:
            pass

    def _build_result(self, video_url: str, referer: str, ua: str, cookies: dict = None, selected_proxy: str | None = None) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        if cookies:
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        proxy = self.last_used_proxy if selected_proxy is None and not self._force_direct else selected_proxy
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint, "bypass_warp": self.bypass_warp_active, "selected_proxy": proxy, "force_direct": self._force_direct}

