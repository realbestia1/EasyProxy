import asyncio
import logging
import re
import time
import base64
from urllib.parse import urlparse, urljoin, urlencode

import aiohttp
from bs4 import BeautifulSoup, SoupStrainer

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, get_solver_proxy_url, GLOBAL_PROXIES
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

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
             self.base_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
        self.proxies = proxies or GLOBAL_PROXIES
        self.cache = CookieCache("deltabit")
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp
        logger.debug(f"Deltabit: Initialized with bypass_warp={self.bypass_warp_active}")

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None, force_bypass_warp: bool = None) -> dict:
        """Performs a request via FlareSolverr."""
        if not settings.flaresolverr_url:
            raise ExtractorError("FlareSolverr URL not configured")

        # Determine which bypass state to use
        current_bypass = force_bypass_warp if force_bypass_warp is not None else self.bypass_warp_active

        endpoint = f"{settings.flaresolverr_url.rstrip('/')}/v1"
        payload = {
            "cmd": cmd,
            "maxTimeout": (settings.flaresolverr_timeout + 60) * 1000,
        }
        fs_headers = {}
        if url: 
            payload["url"] = url
            # Determina dinamicamente il proxy per questo specifico URL
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, bypass_warp=current_bypass)
            if proxy:
                payload["proxy"] = {"url": proxy}
                solver_proxy = get_solver_proxy_url(proxy)
                fs_headers["X-Proxy-Server"] = solver_proxy
                logger.debug(f"Deltabit: Passing explicit proxy to solver: {solver_proxy} (bypass_warp={current_bypass})")

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
        # Respect bypass_warp or warp from kwargs if provided
        target_warp = kwargs.get("bypass_warp") or kwargs.get("warp")
        if target_warp is not None:
            if isinstance(target_warp, str):
                self.bypass_warp_active = target_warp.lower() in ("true", "1", "on", "yes", "off")
                # Special case for "off" string
                if target_warp.lower() == "off":
                    self.bypass_warp_active = True
                elif target_warp.lower() in ("on", "true", "1"):
                    self.bypass_warp_active = False
            else:
                self.bypass_warp_active = bool(target_warp)
            logger.debug(f"Deltabit: bypass_warp_active updated from kwargs to {self.bypass_warp_active}")
        
        # 1. Handle redirectors (safego.cc, clicka.cc, etc.)
        if any(d in url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]):
            url = await self._solve_redirector(url)
        
        # 2. Normalize URL to embed format
        if "deltabit.co" in url.lower():
            url = url.replace("deltabit.co/ ", "deltabit.co/")
        
        logger.debug(f"Deltabit: Starting unified FlareSolverr bypass for {url}")
        
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        session_id = None
        try:
            # Start session for better performance (persistence of cookies/browser state)
            sess_res = await self._request_flaresolverr("sessions.create")
            session_id = sess_res.get("session")

            # GET first page
            res = await self._request_flaresolverr("request.get", url, session_id=session_id)
            solution = res.get("solution", {})
            html = solution.get("response", "")
            current_url = solution.get("url", url)
            ua = solution.get("userAgent", self.base_headers.get("User-Agent", ""))
            raw_cookies = solution.get("cookies", [])

            # Update cache
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
                # Check for direct link
                link_match = re.search(r'sources:\s*\["([^"]+)"', html)
                if not link_match:
                    link_match = re.search(r'["\'](https?://.*?\.(?:m3u8|mp4)[^"\']*)["\']', html)
                
                if link_match:
                    return self._build_result(link_match.group(1), current_url, ua)
                
                raise ExtractorError("Deltabit: Initial challenge passed but form data not found.")

            # Prepare for POST
            data['imhuman'] = ""
            data['referer'] = current_url
            
            wait_time = 3.5
            logger.debug(f"Deltabit: Waiting {wait_time}s for server validation...")
            await asyncio.sleep(wait_time)
            
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
        """Solves safego.cc or clicka.cc redirectors."""
        logger.debug(f"Deltabit: Solving redirector via FlareSolverr session: {url}")
        
        session_id = None
        try:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
        except ImportError:
            ocr = None

        try:
            sess_res = await self._request_flaresolverr("sessions.create")
            session_id = sess_res.get("session")

            current_url = url
            for step in range(5):
                if not any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]):
                    break
                
                logger.debug(f"Deltabit: Redirector step {step+1} at {current_url}")
                res = await self._request_flaresolverr("request.get", current_url, session_id=session_id)
                solution = res.get("solution", {})
                current_url = solution.get("url", current_url)
                text = solution.get("response", "")
                soup = BeautifulSoup(text, "lxml")
                
                for captcha_attempt in range(5):
                    img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,'))
                    if not img_tag or not ocr:
                        break
                    
                    logger.debug(f"Deltabit: Captcha detected, attempt {captcha_attempt+1}")
                    img_data_b64 = img_tag["src"].split(",")[1]
                    img_data = base64.b64decode(img_data_b64)
                    
                    res_captcha = ocr.classification(img_data)
                    res_captcha = res_captcha.replace('o', '0').replace('O', '0').replace('l', '1').replace('I', '1')
                    res_captcha = re.sub(r'[^0-9]', '', res_captcha)
                    
                    logger.debug(f"Deltabit: OCR Result (sanitized): {res_captcha}")
                    
                    post_data = urlencode({"captch5": res_captcha, "submit": "Continue"})
                    post_res = await self._request_flaresolverr("request.post", current_url, post_data, session_id=session_id)
                    post_solution = post_res.get("solution", {})
                    text = post_solution.get("response", "")
                    current_url = post_solution.get("url", current_url)
                    
                    if "Incorrect" not in text:
                        logger.debug("Deltabit: Captcha passed!")
                        soup = BeautifulSoup(text, "lxml")
                        break
                    
                    logger.debug("Deltabit: Captcha incorrect, retrying...")
                    await asyncio.sleep(2)
                    res = await self._request_flaresolverr("request.get", current_url, session_id=session_id)
                    text = res.get("solution", {}).get("response", "")
                    soup = BeautifulSoup(text, "lxml")

                next_url = None
                for attempt in range(3):
                    for a_tag in soup.find_all("a", href=True):
                        txt = a_tag.get_text().lower()
                        href = a_tag["href"]
                        if any(x in txt for x in ["proceed to video", "continue", "guarda il video"]):
                            next_url = href
                            break
                        for btn in a_tag.find_all("button"):
                            if "proceed" in btn.get_text().lower():
                                 next_url = href
                                 break
                        if next_url: break
                    
                    if not next_url:
                        for a_tag in soup.find_all("a", href=re.compile(r'deltabit\.(co|sx|bz)/[a-zA-Z0-9]+', re.I)):
                            href = a_tag["href"]
                            path_part = href.split("/")[-1].split(".")[0]
                            if not any(x in href.lower() for x in ["/login", "/registration", "/faq", "/tos", "/contact", "/category", "make_money"]):
                                if len(path_part) >= 10: 
                                    next_url = href
                                    break

                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin(current_url, next_url)
                        
                        path_part = next_url.split("/")[-1].split(".")[0]
                        if "deltabit" in next_url.lower() and len(path_part) >= 10:
                            current_url = next_url
                            return current_url
                        current_url = next_url
                        break
                    
                    meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r'refresh', re.I)})
                    if meta_refresh and "url=" in meta_refresh.get("content", "").lower():
                        refresh_url = re.search(r'url=(.*)', meta_refresh["content"], re.I).group(1).strip()
                        if refresh_url:
                            current_url = urljoin(current_url, refresh_url)
                            next_url = current_url
                            break

                    if attempt < 2:
                        await asyncio.sleep(4)
                        res = await self._request_flaresolverr("request.get", current_url, session_id=session_id)
                        text = res.get("solution", {}).get("response", "")
                        soup = BeautifulSoup(text, "lxml")

                if not next_url:
                    break

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
        # CLEAN HEADERS: Remove any Cloudflare-related headers to avoid IP mismatch
        headers = {}
        for key, value in self.base_headers.items():
            if not any(cf in key.lower() for cf in ["cf-", "x-forwarded", "true-client", "x-real-ip"]):
                headers[key] = value

        headers["Referer"] = referer
        headers["User-Agent"] = user_agent
        headers["Origin"] = f"https://{urlparse(referer).netloc}"
        
        logger.debug(f"Deltabit: Final result bypass_warp={self.bypass_warp_active}")
        
        return {
            "destination_url": video_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "bypass_warp": self.bypass_warp_active
        }

    async def close(self):
        pass
