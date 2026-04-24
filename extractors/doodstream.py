import asyncio
import logging
import re
import time
from urllib.parse import urlparse, urljoin

import aiohttp
from curl_cffi.requests import AsyncSession
from camoufox.async_api import AsyncCamoufox

from config import BYPARR_URL, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_solver_proxy_url
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
    DoodStream / PlayMogo extractor using Network Sniffing (Camoufox) 
    to capture dynamic pass_md5 links directly from traffic.
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
        """
        Main extraction entry point. 
        Uses Camoufox to sniff the network traffic for pass_md5.
        """
        parsed = urlparse(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        if not video_id:
            raise ExtractorError("Invalid DoodStream URL: no video ID found")

        embed_url = url if "/e/" in url else f"https://{parsed.netloc}/e/{video_id}"
        proxy = self._get_proxy(embed_url)
        
        logger.info(f"🚀 DoodStream: Starting Network Sniffing for {embed_url}")
        
        captured_data = {"md5_url": None, "base_stream": None, "html": None}
        
        async with AsyncCamoufox(
            headless=True,
            geoip=True,
            proxy={"server": proxy} if proxy else None,
        ) as browser:
            page = await browser.new_page()
            
            # 🔥 Event listener for network traffic (exactly like DrissionPage logic)
            async def on_response(response):
                if "pass_md5" in response.url:
                    try:
                        captured_data["md5_url"] = response.url
                        captured_data["base_stream"] = (await response.text()).strip()
                        logger.info(f"🔥 Captured MD5 Request: {response.url}")
                    except Exception:
                        pass

            page.on("response", on_response)
            
            try:
                # Navigate and wait for the page to be ready
                await page.goto(embed_url, wait_until="networkidle", timeout=45000)
                
                # Check for Cloudflare Turnstile titles and wait if detected
                title = await page.title()
                if any(t in title for t in ["Just a moment...", "Ci siamo quasi...", "Verifica del browser"]):
                    logger.info("🛡️ Cloudflare detected in browser, waiting for solve...")
                    await page.wait_for_timeout(10000) # Wait for auto-solve
                
                # Wait up to 30 seconds for the pass_md5 request to appear
                start_wait = time.time()
                while not captured_data["base_stream"] and (time.time() - start_wait < 30):
                    # Store content as we go to avoid NoneType later
                    try:
                        captured_data["html"] = await page.content()
                    except:
                        pass
                    
                    if captured_data["base_stream"]:
                        break
                        
                    # Try to trigger play if possible (sometimes needed)
                    try:
                         await page.click("div.vjs-big-play-button", timeout=1000)
                    except:
                         pass
                    await asyncio.sleep(1)
                    
            except Exception as e:
                # If we have the data, ignore the error (e.g. timeout)
                if captured_data["base_stream"]:
                    logger.debug(f"Ignoring browser error as data was already captured: {e}")
                else:
                    logger.error(f"Browser extraction error: {e}")
            finally:
                if not captured_data["html"]:
                    try:
                        captured_data["html"] = await page.content()
                    except:
                        captured_data["html"] = ""
                await page.close()

        if not captured_data["base_stream"]:
            raise ExtractorError("DoodStream: Network sniffing failed to capture pass_md5")

        return self._finalize_extraction(
            captured_data["base_stream"], 
            captured_data["html"], 
            embed_url, 
            _DOOD_UA
        )

    def _finalize_extraction(self, base_stream: str, html: str, base_url: str, ua: str) -> dict:
        """Constructs the final URL from captured data."""
        if "RELOAD" in base_stream or len(base_stream) < 5:
            raise ExtractorError(f"DoodStream: Captured pass_md5 is invalid ({base_stream[:20]})")

        # Find token and expiry in the captured HTML
        token_match = re.search(r"token=([^&\s'\"]+)", html)
        if not token_match:
            token_match = re.search(r"['\"]?token['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
        if not token_match:
            token_match = re.search(r"window\.[a-z0-9_]+\s*=\s*['\"]([^'\"]{20,})['\"]", html)

        if not token_match:
             raise ExtractorError("DoodStream: token not found in HTML")
            
        token = token_match.group(1)
        expiry_match = re.search(r"expiry[:=]\s*['\"]?(\d+)['\"]?", html)
        expiry = expiry_match.group(1) if expiry_match else str(int(time.time()))
        
        import random
        import string
        rand_str = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        final_url = f"{base_stream}{rand_str}?token={token}&expiry={expiry}"

        logger.info(f"✅ DoodStream successful sniffed extraction: {final_url[:60]}...")

        return {
            "destination_url": final_url,
            "request_headers": {"User-Agent": ua, "Referer": f"{base_url}/", "Accept": "*/*"},
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        pass
