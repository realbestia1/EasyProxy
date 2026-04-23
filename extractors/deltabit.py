import asyncio
import logging
import re
import time
import base64
from urllib.parse import urlparse, urljoin, urlencode

import aiohttp
from bs4 import BeautifulSoup, SoupStrainer

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, get_solver_proxy_url
from utils.cookie_cache import CookieCache

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class Settings:
    flaresolverr_url = FLARESOLVERR_URL
    flaresolverr_timeout = FLARESOLVERR_TIMEOUT

settings = Settings()

class DeltabitExtractor:
    """
    Deltabit extractor using FlareSolverr for Cloudflare bypass and session caching.
    Supports safego.cc/clicka.cc redirection and unifies FlareSolverr sessions for speed.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        self.base_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
        self.proxies = proxies or []
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.cache = CookieCache("deltabit")

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None) -> dict:
        """Performs a request via FlareSolverr."""
        if not settings.flaresolverr_url:
            raise ExtractorError("FlareSolverr URL not configured")

        endpoint = f"{settings.flaresolverr_url.rstrip('/')}/v1"
        payload = {
            "cmd": cmd,
            "maxTimeout": (settings.flaresolverr_timeout + 60) * 1000,
        }
        fs_headers = {}
        if url: 
            payload["url"] = url
            # Determina dinamicamente il proxy per questo specifico URL
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            if proxy:
                payload["proxy"] = {"url": proxy}
                solver_proxy = get_solver_proxy_url(proxy)
                fs_headers["X-Proxy-Server"] = solver_proxy
                logger.debug(f"Deltabit: Passing explicit proxy to solver: {solver_proxy}")

        if post_data: payload["postData"] = post_data
        if session_id: payload["session"] = session_id

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=fs_headers,
                    timeout=aiohttp.ClientTimeout(total=settings.flaresolverr_timeout + 95),
                ) as resp:
                    if resp.status != 200:
                        raise ExtractorError(f"FlareSolverr HTTP {resp.status}")
                    data = await resp.json()
            except Exception as e:
                logger.error(f"Deltabit: FlareSolverr request failed ({cmd}): {e}")
                raise ExtractorError(f"FlareSolverr bypass failed: {e}")

        if data.get("status") != "ok":
            raise ExtractorError(f"FlareSolverr ({cmd}): {data.get('message', 'unknown error')}")
        
        return data

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Deltabit URL using a unified FlareSolverr session if needed."""
        
        # 1. Handle redirectors (safego.cc, clicka.cc, etc.)
        if any(d in url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]):
            url = await self._solve_redirector(url)

        # 2. Normalize URL to embed format
        if "/e/" not in url:
             url = url.replace("deltabit.co/", "deltabit.co/e/")
             url = url.replace("deltabit.sx/", "deltabit.sx/e/")
             url = url.replace("deltabit.bz/", "deltabit.bz/e/")
        
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        session_id = None
        try:
            logger.debug(f"Deltabit: Starting unified FlareSolverr bypass for {url}")
            
            # Start session for better performance (persistence of cookies/browser state)
            sess_res = await self._request_flaresolverr("sessions.create")
            session_id = sess_res.get("session")

            # GET first page
            res = await self._request_flaresolverr("request.get", url, session_id=session_id)
            solution = res.get("solution", {})
            html = solution.get("response", "")
            current_url = solution.get("url", url)
            ua = solution.get("userAgent", self.base_headers["User-Agent"])
            raw_cookies = solution.get("cookies", [])

            # Update cache (for future requests)
            if raw_cookies:
                cookies = {c["name"]: c["value"] for c in raw_cookies}
                self.cache.set(domain, cookies, ua)

            # Extract form inputs
            soup = BeautifulSoup(html, 'lxml', parse_only=SoupStrainer('input'))
            data = {}
            for input_tag in soup:
                name = input_tag.get('name')
                value = input_tag.get('value', '')
                if name:
                    data[name] = value 
            
            if not data.get("op"):
                # Check for direct link in response
                link_match = re.search(r'sources:\s*\["([^"]+)"', html)
                if not link_match:
                    link_match = re.search(r'["\'](https?://.*?\.(?:m3u8|mp4)[^"\']*)["\']', html)
                
                if link_match:
                    return self._build_result(link_match.group(1), current_url, ua)
                
                raise ExtractorError("Deltabit: Initial challenge passed but form data not found.")

            # Prepare for POST
            data['imhuman'] = ""
            data['referer'] = current_url
            
            # Use a slightly shorter wait time if we are in a session (server might be more lenient)
            # but usually these hosts are strict. Reduction to 3.5s for a slight win.
            wait_time = 3.5
            logger.debug(f"Deltabit: Waiting {wait_time}s for server validation...")
            await asyncio.sleep(wait_time)
            
            # Submitting validation form via SAME FlareSolverr session
            post_data = urlencode(data)
            post_res = await self._request_flaresolverr("request.post", current_url, post_data, session_id=session_id)
            post_html = post_res.get("solution", {}).get("response", "")
            
            # Extract video URL
            link_match = re.search(r'sources:\s*\["([^"]+)"', post_html)
            if not link_match:
                link_match = re.search(r'["\'](https?://.*?\.(?:m3u8|mp4)[^"\']*)["\']', post_html)
            
            if not link_match:
                if "Incorrect" in post_html:
                    raise ExtractorError("Deltabit: Bot-check failed (incorrect timing)")
                raise ExtractorError("Deltabit: Video source not found in final page")

            final_url = link_match.group(1)
            logger.info(f"Deltabit: Extraction successful!")
            
            return self._build_result(final_url, current_url, ua)

        finally:
            if session_id:
                try:
                    await self._request_flaresolverr("sessions.destroy", session_id=session_id)
                except:
                    pass

    async def _solve_redirector(self, url: str) -> str:
        """Solves safego.cc or clicka.cc redirectors and returns the destination URL using FS sessions."""
        logger.debug(f"Deltabit: Solving redirector via FlareSolverr session: {url}")
        
        session_id = None
        try:
            import ddddocr
        except ImportError:
            ddddocr = None

        try:
            sess_res = await self._request_flaresolverr("sessions.create")
            session_id = sess_res.get("session")

            res = await self._request_flaresolverr("request.get", url, session_id=session_id)
            solution = res.get("solution", {})
            text = solution.get("response", "")
            current_url = solution.get("url", url)

            soup = BeautifulSoup(text, "lxml")
            
            img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,'))
            if img_tag and ddddocr:
                img_data_b64 = img_tag["src"].split(",")[1]
                img_data = base64.b64decode(img_data_b64)
                
                ocr = ddddocr.DdddOcr(show_ad=False)
                res_captcha = ocr.classification(img_data)
                logger.debug(f"Deltabit: Solved redirector captcha: {res_captcha}")
                
                post_data = urlencode({"captch5": res_captcha, "submit": "Continue"})
                
                post_res = await self._request_flaresolverr("request.post", current_url, post_data, session_id=session_id)
                text = post_res.get("solution", {}).get("response", "")
                soup = BeautifulSoup(text, "lxml")
            
            for attempt in range(4):
                proceed_link = None
                for a_tag in soup.find_all("a", href=True):
                    txt = a_tag.get_text().lower()
                    if "proceed to video" in txt or "continue" in txt:
                        proceed_link = a_tag
                        break
                    for btn in a_tag.find_all("button"):
                        if "proceed" in btn.get_text().lower():
                             proceed_link = a_tag
                             break
                    if proceed_link: break
                
                if not proceed_link:
                    proceed_link = soup.find("a", href=re.compile(r'deltabit|mixdrop|clicka', re.I))
                
                if proceed_link:
                    new_url = proceed_link["href"]
                    if new_url.startswith("/"):
                        new_url = urljoin(current_url, new_url)
                    logger.debug(f"Deltabit: Resolved redirector -> {new_url}")
                    return new_url
                
                meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r'refresh', re.I)})
                if meta_refresh and "url=" in meta_refresh.get("content", "").lower():
                    refresh_url = re.search(r'url=(.*)', meta_refresh["content"], re.I).group(1).strip()
                    if refresh_url:
                        logger.debug(f"Deltabit: Found meta-refresh redirect: {refresh_url}")
                        return urljoin(current_url, refresh_url)

                if attempt < 3:
                    await asyncio.sleep(4)
                    res = await self._request_flaresolverr("request.get", current_url, session_id=session_id)
                    text = res.get("solution", {}).get("response", "")
                    soup = BeautifulSoup(text, "lxml")
            
            return current_url

        except Exception as e:
            logger.error(f"Deltabit: redirector solver error: {e}")
            return url
        finally:
            if session_id:
                try:
                    await self._request_flaresolverr("sessions.destroy", session_id=session_id)
                except:
                    pass
        
    def _build_result(self, video_url: str, referer: str, user_agent: str) -> dict:
        headers = self.base_headers.copy()
        headers["Referer"] = referer
        headers["User-Agent"] = user_agent
        headers["Origin"] = f"https://{urlparse(referer).netloc}"
        
        return {
            "destination_url": video_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        pass
