import logging
import random
import re
import socket
import io
from urllib.parse import urlparse, quote_plus
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.resolver import DefaultResolver
from aiohttp_socks import ProxyConnector
from bs4 import BeautifulSoup
from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_proxy_for_url, get_connector_for_proxy

from utils.smart_request import smart_request

logger = logging.getLogger(__name__)

class StaticResolver(DefaultResolver):
    """Custom resolver to force specific IPs for domains (bypass hijacking)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mapping = {}

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host in self.mapping:
            ip = self.mapping[host]
            logger.debug(f"StaticResolver: forcing {host} -> {ip}")
            # Format required by aiohttp: list of dicts
            return [{
                'hostname': host,
                'host': ip,
                'port': port,
                'family': family,
                'proto': 0,
                'flags': 0
            }]
        return await super().resolve(host, port, family)

class ExtractorError(Exception):
    pass

class MaxstreamExtractor:
    """Maxstream URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or []
        self.resolver = StaticResolver()

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    def _get_proxies_for_url(self, url: str) -> list[str]:
        """Build ordered proxy list for current URL, honoring TRANSPORT_ROUTES first."""
        ordered = []

        route_proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)
        if route_proxy:
            ordered.append(route_proxy)

        for proxy in self.proxies:
            if proxy and proxy not in ordered:
                ordered.append(proxy)

        return ordered

    async def _get_session(self, proxy=None):
        """Get or create session, optionally with a specific proxy."""
        # Note: we use our custom resolver only for non-proxy requests
        # because proxies handle their own DNS resolution.
        
        timeout = ClientTimeout(total=45, connect=15, sock_read=30)
        if proxy:
            connector = get_connector_for_proxy(proxy)
            return ClientSession(timeout=timeout, connector=connector, headers=self.base_headers)
        
        if self.session is None or self.session.closed:
            connector = TCPConnector(
                limit=0, 
                limit_per_host=0, 
                keepalive_timeout=60, 
                enable_cleanup_closed=True, 
                resolver=self.resolver # Use custom StaticResolver
            )
            self.session = ClientSession(timeout=timeout, connector=connector, headers=self.base_headers)
        return self.session

    async def _resolve_doh(self, domain: str) -> list[str]:
        """Resolve domain using DNS-over-HTTPS (Google) to bypass local DNS hijacking."""
        try:
            # Using Google DoH API
            url = f"https://dns.google/resolve?name={domain}&type=A"
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ips = [ans['data'] for ans in data.get('Answer', []) if ans.get('type') == 1]
                        if ips:
                            logger.debug(f"DoH resolved {domain} to {ips}")
                            return ips
        except Exception as e:
            logger.debug(f"DoH resolution failed for {domain}: {e}")
        return []

    async def _smart_request(self, url: str, method="GET", is_binary=False, **kwargs):
        """Request with automatic retry using different proxies and resolver fallback on connection failure."""
        last_error = None
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        # Clear previous mapping for this domain to start fresh
        self.resolver.mapping.pop(domain, None)

        # Determine paths to try: Direct, Proxies, and then resolver override
        paths = []
        # Path 1: Direct (system DNS)
        paths.append({"proxy": None, "use_ip": None})
        
        # Path 2: Proxies (route-specific first)
        proxies_for_url = self._get_proxies_for_url(url)
        if proxies_for_url:
            for p in proxies_for_url:
                paths.append({"proxy": p, "use_ip": None})
        
        # Path 3: DoH fallback (override resolver) if it's uprot or maxstream
        if "uprot.net" in domain or "maxstream" in domain:
            real_ips = await self._resolve_doh(domain)
            for ip in real_ips[:2]: # Try first 2 IPs
                paths.append({"proxy": None, "use_ip": ip})
        
        for path in paths:
            proxy = path["proxy"]
            use_ip = path["use_ip"]
            
            if use_ip:
                # CRITICAL: Must destroy old session to flush TCPConnector DNS cache!
                # Otherwise connector reuses cached (hijacked) IP even with new resolver mapping.
                if self.session and not self.session.closed:
                    await self.session.close()
                    self.session = None
                self.resolver.mapping[domain] = use_ip
                logger.debug(f"DoH bypass: forcing {domain} -> {use_ip}")
            else:
                self.resolver.mapping.pop(domain, None)

            session = await self._get_session(proxy=proxy)
            try:
                async with session.request(method, url, **kwargs) as response:
                    if response.status < 400:
                        if is_binary:
                            content = await response.read()
                            if proxy: await session.close()
                            return content
                        text = await response.text()
                        
                        # Check for Cloudflare challenge in successful response
                        if any(marker in text.lower() for marker in ["cf-challenge", "ray id", "checking your browser"]):
                            logger.warning(f"Cloudflare detected on {url} (Proxy: {proxy}), trying FlareSolverr fallback...")
                            # Fallback to the global smart_request utility
                            if proxy: await session.close()
                            fs_cmd = f"request.{method.lower()}"
                            result = await smart_request(fs_cmd, url, headers=kwargs.get("headers"), post_data=kwargs.get("data"), proxies=self.proxies)
                            return result.get("html", "") if isinstance(result, dict) else result

                        if proxy: await session.close()
                        return text
                    elif response.status in (403, 503):
                        # Might be Cloudflare block, try FlareSolverr immediately for this path
                        logger.warning(f"HTTP {response.status} on {url}, checking with FlareSolverr...")
                        if proxy: await session.close()
                        fs_cmd = f"request.{method.lower()}"
                        result = await smart_request(fs_cmd, url, headers=kwargs.get("headers"), post_data=kwargs.get("data"), proxies=self.proxies)
                        return result.get("html", "") if isinstance(result, dict) else result
                    else:
                        logger.warning(f"Request to {url} failed (Status {response.status}) [Proxy: {proxy}, StaticIP: {use_ip}]")
            except Exception as e:
                logger.warning(f"Request to {url} failed (Error: {e}) [Proxy: {proxy}, StaticIP: {use_ip}]")
                last_error = e
                # If DoH attempt failed, destroy session so next IP gets fresh connector
                if use_ip and self.session and not self.session.closed:
                    await self.session.close()
                    self.session = None
            finally:
                if proxy and 'session' in locals() and not session.closed:
                    await session.close()
        
        raise ExtractorError(f"Connection failed for {url} after trying all paths. Last error: {last_error}")

    async def _solve_uprot_captcha(self, text: str, original_url: str) -> str:
        """Find, download and solve captcha on uprot page."""
        try:
            import ddddocr
        except ImportError:
            logger.error("ddddocr not installed. Cannot solve captcha.")
            return None
            
        soup = BeautifulSoup(text, "lxml")
        img_tag = soup.find("img", src=re.compile(r'/captcha|/image/'))
        form = soup.find("form")
        
        if not img_tag or not form:
            return None
            
        captcha_url = img_tag["src"]
        if captcha_url.startswith("/"):
            parsed = urlparse(original_url)
            captcha_url = f"{parsed.scheme}://{parsed.netloc}{captcha_url}"
            
        logger.debug(f"Downloading captcha from: {captcha_url}")
        img_data = await self._smart_request(captcha_url, is_binary=True)
        
        if not img_data:
            return None
            
        # Initialize ddddocr (lazy init for performance)
        if not hasattr(self, '_ocr_engine'):
            self._ocr_engine = ddddocr.DdddOcr(show_ad=False)
            
        # Solve
        res = self._ocr_engine.classification(img_data)
        logger.debug(f"Captcha solved: {res}")
        
        # Submit form
        form_action = form.get("action", "")
        if not form_action or form_action == "#":
            form_action = original_url
        elif form_action.startswith("/"):
            parsed = urlparse(original_url)
            form_action = f"{parsed.scheme}://{parsed.netloc}{form_action}"
            
        # Prepare data (find the captcha input name)
        captcha_input = soup.find("input", {"name": re.compile(r'captcha|code|val', re.I)})
        if not captcha_input:
            # Fallback to common names
            field_name = "captcha"
        else:
            field_name = captcha_input["name"]
            
        post_data = {field_name: res}
        # Add other hidden fields
        for hidden in form.find_all("input", type="hidden"):
            if hidden.get("name"):
                post_data[hidden["name"]] = hidden.get("value", "")
        
        logger.debug(f"Submitting captcha to: {form_action}")
        headers = {**self.base_headers, "referer": original_url}
        solved_text = await self._smart_request(form_action, method="POST", data=post_data, headers=headers)
        
        # Try to parse the new page
        try:
            return self._parse_uprot_html(solved_text)
        except:
            return None

    def _parse_uprot_html(self, text: str) -> str:
        """Parse uprot HTML to extract redirect link."""
        # 1. Look for direct links in text (including escaped slashes)
        match = re.search(r'https?://(?:www\.)?(?:stayonline\.pro|maxstream\.video)[^"\'\s<>\\ ]+', text.replace("\\/", "/"))
        if match:
            return match.group(0)
            
        # 2. Look for JavaScript-based redirects
        js_match = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', text)
        if js_match:
            return js_match.group(1)
            
        # 3. Look for Meta refresh
        meta_match = re.search(r'content=["\']0;\s*url=([^"\']+)["\']', text, re.I)
        if meta_match:
            return meta_match.group(1)
            
        # 4. Use BeautifulSoup for interactive elements
        soup = BeautifulSoup(text, "lxml")
        
        # Look for Bulma-style buttons or links with "Continue" text
        for btn in soup.find_all(["a", "button"]):
            text_content = btn.get_text().strip().lower()
            if "continue" in text_content or "continua" in text_content or "vai al" in text_content:
                href = btn.get("href")
                if not href and btn.parent.name == "a":
                    href = btn.parent.get("href")
                
                if href and "uprot" not in href:
                    return href
        
        # Specific Bulma selectors
        for selector in ['a[href*="maxstream"]', 'a[href*="stayonline"]', '.button.is-info', '.button.is-success', 'a.button']:
            tag = soup.select_one(selector)
            if tag and tag.get("href") and "uprot" not in tag["href"]:
                return tag["href"]
        
        # If it's a form
        form = soup.find("form")
        if form and form.get("action") and "uprot" not in form["action"]:
            return form["action"]
            
        return None

    async def get_uprot(self, link: str):
        """Extract MaxStream URL from uprot redirect."""
        # Fix link type
        link = link.replace("msf", "mse")
        
        # Direct request (user should provide non-datacenter proxy in GLOBAL_PROXY)
        text = await self._smart_request(link)
        
        # 1. Try normal parse
        res = self._parse_uprot_html(text)
        if res:
            return res
            
        # 2. If no link, try puzzle/captcha solver
        logger.debug("Direct link not found, checking for captcha...")
        res = await self._solve_uprot_captcha(text, link)
        if res:
            return res
            
        # If we see "Cloudflare" or "Challenge" in text, it's a block
        if "cf-challenge" in text or "ray id" in text.lower() or "checking your browser" in text.lower():
            raise ExtractorError("Cloudflare block (Browser check/Challenge)")
            
        logger.error(f"Uprot Parse Failure. Content: {text[:2000]}...")
        raise ExtractorError("Redirect link not found in uprot page")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Maxstream URL."""
        maxstream_url = await self.get_uprot(url)
        logger.debug(f"Target URL: {maxstream_url}")
        
        # Use strict headers to avoid Error 131
        headers = {
            **self.base_headers,
            "referer": "https://uprot.net/",
            "accept-language": "en-US,en;q=0.5"
        }
        
        text = await self._smart_request(maxstream_url, headers=headers)
        
        # Direct sources check
        direct_match = re.search(r'sources:\s*\[\{src:\s*"([^"]+)"', text)
        if direct_match:
            return {
                "destination_url": direct_match.group(1),
                "request_headers": {**self.base_headers, "referer": maxstream_url},
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        # Fallback to packer logic
        match = re.search(r"\}\('(.+)',.+,'(.+)'\.split", text)
        if not match:
             match = re.search(r"eval\(function\(p,a,c,k,e,d\).+?\}\('(.+?)',.+?,'(.+?)'\.split", text, re.S)
        
        if not match:
            raise ExtractorError(f"Failed to extract from: {text[:200]}")

        # ... rest of packer logic (terms.index, etc) ...})
        # ... rest of regex logic ...

        # Fallback to packer logic
        match = re.search(r"\}\('(.+)',.+,'(.+)'\.split", text)
        if not match:
            # Maybe it's a different packer signature?
            match = re.search(r"eval\(function\(p,a,c,k,e,d\).+?\}\('(.+?)',.+?,'(.+?)'\.split", text, re.S)
            
        if not match:
            logger.error(f"Failed to find packer script or direct source in: {text[:500]}...")
            raise ExtractorError("Failed to extract URL components")

        s1 = match.group(2)
        # Extract Terms
        terms = s1.split("|")
        try:
            urlset_index = terms.index("urlset")
            hls_index = terms.index("hls")
            sources_index = terms.index("sources")
        except ValueError as e:
            logger.error(f"Required terms missing in packer: {e}")
            raise ExtractorError(f"Missing components in packer: {e}")

        result = terms[urlset_index + 1 : hls_index]
        reversed_elements = result[::-1]
        first_part_terms = terms[hls_index + 1 : sources_index]
        reversed_first_part = first_part_terms[::-1]
        
        first_url_part = ""
        for fp in reversed_first_part:
            if "0" in fp:
                first_url_part += fp
            else:
                first_url_part += fp + "-"

        base_url = f"https://{first_url_part.rstrip('-')}.host-cdn.net/hls/"
        
        if len(reversed_elements) == 1:
            final_url = base_url + "," + reversed_elements[0] + ".urlset/master.m3u8"
        else:
            final_url = base_url
            for i, element in enumerate(reversed_elements):
                final_url += element + ","
            final_url = final_url.rstrip(",") + ".urlset/master.m3u8"

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
