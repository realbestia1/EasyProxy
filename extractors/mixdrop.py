import logging
import random
import re
import base64
import asyncio
from urllib.parse import urlparse, urljoin, urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class MixdropExtractor:
    """Mixdrop URL extractor optimized with FlareSolverr sessions."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.proxies = proxies or GLOBAL_PROXIES

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies) if url else None
            connector = get_connector_for_proxy(proxy) if proxy else TCPConnector(limit=0, use_dns_cache=True)
            self.session = ClientSession(timeout=timeout, connector=connector, headers=self.base_headers)
        return self.session

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None) -> dict:
        """Performs a request via FlareSolverr."""
        if not FLARESOLVERR_URL:
             return None

        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {
            "cmd": cmd,
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }
        if url: 
            payload["url"] = url
            # Determina dinamicamente il proxy per questo specifico URL
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            if proxy:
                # FlareSolverr richiede il proxy nel formato {"url": "..."}
                payload["proxy"] = {"url": proxy}
                logger.debug(f"Mixdrop: Passing proxy to FlareSolverr: {proxy}")

        if post_data: payload["postData"] = post_data
        if session_id: payload["session"] = session_id

        async with aiohttp.ClientSession() as fs_session:
            try:
                async with fs_session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 95),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            except Exception:
                return None

        if data.get("status") != "ok":
            return None
        
        return data

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Mixdrop URL."""
        # 1. Safego
        if "safego.cc" in url:
            url = await self._solve_safego(url)

        # 2. Normalize
        if "/f/" in url: url = url.replace("/f/", "/e/")
        if "/emb/" in url: url = url.replace("/emb/", "/e/")
        
        known_mirrors = ["mixdrop.co", "mixdrop.to", "mixdrop.ch", "mixdrop.ag", 
                         "mixdrop.gl", "mixdrop.club", "m1xdrop.net", "mixdrop.top", "mixdrop.nz",
                         "mixdrop.vc", "mixdrop.sx", "mixdrop.bz", "mdy48tn97.com"]
        
        mirror_found = False
        for mirror in known_mirrors:
            if mirror in url:
                mirror_found = True
                break
        
        if not mirror_found and "mixdrop" in url:
            parts = url.split("/")
            if len(parts) > 2:
                parts[2] = "mixdrop.to"
                url = "/".join(parts)

        # Mixdrop extraction usually doesn't need FlareSolverr sessions for eval_solver
        # but we use standard aiohttp for the final packed JS extraction.
        headers = {"accept-language": "en-US,en;q=0.5", "referer": url}
        
        patterns = [
            r'MDCore.wurl ?= ?\"(.*?)\"',  # Primary pattern
            r'wurl ?= ?\"(.*?)\"',          # Simplified pattern
            r'src: ?\"(.*?)\"',             # Alternative pattern
            r'file: ?\"(.*?)\"',            # Another alternative
            r'https?://[^\"\']+\.mp4[^\"\']*'  # Direct MP4 URL pattern
        ]

        session = await self._get_session(url)
        
        try:
            final_url = await eval_solver(session, url, headers, patterns)
            
            if not final_url or len(final_url) < 10:
                raise ExtractorError(f"Extracted URL appears invalid: {final_url}")
            
            logger.info(f"Successfully extracted Mixdrop URL: {final_url[:50]}...")
            
            res_headers = self.base_headers.copy()
            res_headers["Referer"] = url
            return {
                "destination_url": final_url,
                "request_headers": res_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }
        except Exception as e:
            raise ExtractorError(f"Mixdrop extraction failed: {str(e)}") from e

    async def _solve_safego(self, url: str) -> str:
        """Solves safego.cc captcha using FS sessions."""
        session_id = None
        current_url = url
        try:
            import ddddocr
        except ImportError:
            ddddocr = None

        try:
            res_s = await self._request_flaresolverr("sessions.create")
            if not res_s: return url
            session_id = res_s.get("session")

            res = await self._request_flaresolverr("request.get", url, session_id=session_id)
            if not res: return url
            solution = res.get("solution", {})
            text = solution.get("response", "")
            current_url = solution.get("url", url)

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "lxml")
            
            img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,'))
            if img_tag and ddddocr:
                img_data = base64.b64decode(img_tag["src"].split(",")[1])
                ocr = ddddocr.DdddOcr(show_ad=False)
                captcha = ocr.classification(img_data)
                post_data = urlencode({"captch5": captcha, "submit": "Continue"})
                
                pres = await self._request_flaresolverr("request.post", current_url, post_data, session_id=session_id)
                if pres:
                    text = pres.get("solution", {}).get("response", "")
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
                    return urljoin(current_url, proceed_link["href"])
                
                meta = soup.find("meta", attrs={"http-equiv": re.compile(r'refresh', re.I)})
                if meta and "url=" in meta.get("content", "").lower():
                    r_url = re.search(r'url=(.*)', meta["content"], re.I).group(1).strip()
                    if r_url: return urljoin(current_url, r_url)

                if attempt < 3:
                    await asyncio.sleep(4)
                    res = await self._request_flaresolverr("request.get", current_url, session_id=session_id)
                    if res:
                        text = res.get("solution", {}).get("response", "")
                        soup = BeautifulSoup(text, "lxml")
            
            return current_url

        finally:
            if session_id:
                try: await self._request_flaresolverr("sessions.destroy", session_id=session_id)
                except: pass

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
