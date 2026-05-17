import aiohttp
import asyncio
import logging
import re
import time
from aiohttp import ClientSession, TCPConnector
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urlencode

from config import (
    FLARESOLVERR_URL,
    FLARESOLVERR_TIMEOUT,
    get_proxy_for_url,
    TRANSPORT_ROUTES,
    get_solver_proxy_url,
    GLOBAL_PROXIES,
    get_connector_for_proxy
)
from utils.cookie_cache import CookieCache
from utils.solver_manager import solver_manager

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


class Settings:
    flaresolverr_url = FLARESOLVERR_URL
    flaresolverr_timeout = FLARESOLVERR_TIMEOUT


settings = Settings()


class DeltabitExtractor:
    _result_cache = {}  # cache for final results: {url: (result, timestamp)}

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
            self.base_headers[
                "User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or GLOBAL_PROXIES
        self.cache = CookieCache("universal")
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp
        self.session = None

    async def _get_session(self, proxy: str = None) -> aiohttp.ClientSession:
        """Create a session, optionally with a proxy connector."""
        connector = None
        if proxy:
            connector = get_connector_for_proxy(proxy)

        # If we have an existing session but need a different proxy, we must create a new one
        # To simplify, we'll return a one-off session if a proxy is requested
        if proxy:
            return aiohttp.ClientSession(headers=self.base_headers, connector=connector)

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.base_headers)
        return self.session

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None,
                                    wait: int = 0, headers: dict | None = None) -> dict:
        endpoint = f"{settings.flaresolverr_url.rstrip('/')}/v1"
        payload = {"cmd": cmd, "maxTimeout": (settings.flaresolverr_timeout + 60) * 1000}
        if wait > 0: payload["wait"] = wait
        fs_headers = {}
        if url:
            payload["url"] = url
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, bypass_warp=self.bypass_warp_active)
            if proxy:
                payload["proxy"] = {"url": proxy}
                fs_headers["X-Proxy-Server"] = get_solver_proxy_url(proxy)
        if post_data: payload["postData"] = post_data
        if session_id: payload["session"] = session_id
        async with aiohttp.ClientSession() as fs_session:
            async with fs_session.post(endpoint, json=payload, headers=fs_headers,
                                       timeout=settings.flaresolverr_timeout + 95) as resp:
                data = await resp.json()
        if data.get("status") != "ok": raise ExtractorError(f"FlareSolverr: {data.get('message')}")
        return data

    def _step_headers(self, ua: str, referer: str | None = None) -> dict:
        headers = {"User-Agent": ua}
        if referer:
            headers["Referer"] = referer
        return headers

    async def extract(self, url: str, **kwargs) -> dict:
        # Normalize URL for cache
        normalized_url = url.strip()
        # Check cache (10 minutes validity)
        if normalized_url in DeltabitExtractor._result_cache:
            res, ts = DeltabitExtractor._result_cache[normalized_url]
            if time.time() - ts < 600:
                logger.info("🚀 [Cache Hit] Using cached extraction result for: %s", normalized_url)
                return res

        logger.info("🔍 [Cache Miss] Extracting new link for: %s", normalized_url)
        proxy = get_proxy_for_url(normalized_url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
        final_session_id = await solver_manager.get_persistent_session("deltabit", proxy)
        session_id = final_session_id
        is_persistent = True  # Always persistent for this key
        try:
            ua, cookies = self.base_headers.get("User-Agent"), {}
            if "deltabit.co" in url.lower(): url = url.replace("deltabit.co/ ", "deltabit.co/")

            async def try_path(p, is_fs=False):
                try:
                    m_headers = self._step_headers(ua, url)
                    if is_fs:
                        res = await self._request_flaresolverr("request.get", url, session_id=session_id, wait=2000)
                        sol = res.get("solution", {})
                        return sol.get("response", ""), sol.get("url", url), sol.get("userAgent", ua), {
                            c["name"]: c["value"] for c in sol.get("cookies", [])}
                    else:
                        connector = get_connector_for_proxy(p) if p else None
                        async with aiohttp.ClientSession(connector=connector,
                                                         headers=self.base_headers) as local_session:
                            async with local_session.get(url, cookies=cookies, headers=m_headers, timeout=12) as r:
                                if r.status == 200:
                                    t = await r.text()
                                    if not any(
                                            m in t.lower() for m in ["cf-challenge", "robot", "checking your browser"]):
                                        return t, str(r.url), ua, {k: v.value for k, v in r.cookies.items()}
                except:
                    pass
                return None

            pref_p = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
            tasks = [
                asyncio.create_task(try_path(pref_p)) if pref_p else None,
                asyncio.create_task(try_path(None)),
                asyncio.create_task(try_path(None, is_fs=True))
            ]
            tasks = [t for t in tasks if t]

            html = None
            for task in asyncio.as_completed(tasks):
                res = await task
                if res:
                    html, url, ua, new_cookies = res
                    cookies.update(new_cookies)
                    break

            if not html:
                raise ExtractorError("Deltabit: Page fetch failed")

            soup = BeautifulSoup(html, 'lxml')
            form_data = {inp.get('name'): inp.get('value', '') for inp in soup.find_all('input') if inp.get('name')}
            if not form_data.get("op"):
                link_match = re.search(r'sources:\s*\["([^"]+)"', html) or re.search(r'file:\s*["\']([^"\']+)["\']',
                                                                                     html)
                if link_match:
                    result = self._build_result(link_match.group(1), url, ua, proxy, cookies=cookies)
                    DeltabitExtractor._result_cache[normalized_url] = (result, time.time())
                    logger.info("✅ Extraction success (direct source found)")
                    return result
                raise ExtractorError("Deltabit: Form not found")

            # 3. Final POST via FlareSolverr (STABLE)
            form_data['imhuman'], form_data['referer'] = "", url
            await asyncio.sleep(2.5)

            post_res = await self._request_flaresolverr("request.post", url, urlencode(form_data),
                                                        session_id=session_id, wait=0)
            post_solution = post_res.get("solution", {})
            post_html = post_solution.get("response", "")
            # Update cookies after POST
            cookies.update({c["name"]: c["value"] for c in post_solution.get("cookies", [])})

            link_match = re.search(r'sources:\s*\["([^"]+)"', post_html) or re.search(r'file:\s*["\']([^"\']+)["\']',
                                                                                      post_html)
            if not link_match: raise ExtractorError("Deltabit: Video source not found")
            result = self._build_result(link_match.group(1), url, ua, proxy, cookies=cookies)
            DeltabitExtractor._result_cache[normalized_url] = (result, time.time())
            return result
        finally:
            if final_session_id:
                await solver_manager.release_session(final_session_id, is_persistent)

    def _build_result(self, video_url: str, referer: str, ua: str, proxy: str = None, cookies: dict = None) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        if cookies:
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint,
                "bypass_warp": self.bypass_warp_active, "selected_proxy": proxy}

    async def close(self):
        if self.session and not self.session.closed: await self.session.close()
