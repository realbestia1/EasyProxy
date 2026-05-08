import asyncio
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
from utils.proxy_manager import FreeProxyManager

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
        self.cookies = {} # Persistent cookies for the session
        self.selected_proxy = None
        self.resolver = StaticResolver()
        self.proxy_manager = FreeProxyManager.get_instance(
            "maxstream",
            [
                "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/all/data.txt",
                "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text",
                "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
                "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt",
                "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
                "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
                "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
                "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
                "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
                "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",
                "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
                "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt",
                "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"
            ]
        )

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

    async def _curl_cffi_uprot(self, url: str, method: str, is_binary: bool, **kwargs):
        """
        Browser-impersonated request via curl_cffi for uprot.net.

        uprot.net inspects the TLS handshake and serves a captcha page (or
        outright drops the connection with 503/Connection refused) to any
        client whose fingerprint isn't a real browser. aiohttp and httpx are
        easy to spot — Cloudflare's TLS-fingerprinting layer matches them
        within a few requests and starts replying with /msfi/ /msfld/ as
        captcha pages even from a clean residential IP.

        curl_cffi with `impersonate="chrome131"` reuses a real Chrome JA3 +
        ALPN order, so uprot serves the maxstream / stayonline redirect link
        directly on the first GET. We capture the response cookies into
        `self.cookies` so the subsequent captcha POST (if any) goes out with
        PHPSESSID + captcha hash that uprot expects.

        Returns body bytes/str on success, None on failure / Cloudflare
        challenge — the caller falls through to the regular aiohttp path.
        """
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            logger.debug("curl_cffi not installed, skipping browser-impersonation path")
            return None

        proxies_for_url = self._get_proxies_for_url(url)
        proxy = proxies_for_url[0] if proxies_for_url else None
        proxies_arg = {"http": proxy, "https": proxy} if proxy else None
        # `wait`/`fs_*` and similar kwargs are FlareSolverr-specific; curl_cffi
        # doesn't accept them. We extract only what curl_cffi understands.
        headers = dict(kwargs.get("headers") or self.base_headers)
        post_data = kwargs.get("data")
        # POST with urlencoded body needs an explicit content-type — curl_cffi
        # won't infer it for a plain string data and uprot rejects the form
        # submit with 400/parsing error.
        if method.upper() == "POST" and isinstance(post_data, (str, bytes)):
            headers.setdefault("content-type", "application/x-www-form-urlencoded")

        loop = asyncio.get_running_loop()

        def _do_request():
            try:
                # Send any cookies the extractor has already collected so the
                # captcha POST inherits them.
                req_cookies = dict(self.cookies) if self.cookies else None
                r = cffi_requests.request(
                    method,
                    url,
                    headers=headers,
                    data=post_data,
                    cookies=req_cookies,
                    proxies=proxies_arg,
                    impersonate="chrome131",
                    timeout=30,
                    allow_redirects=True,
                )
                cookies = {}
                try:
                    cookies = {c.name: c.value for c in r.cookies.jar}
                except Exception:
                    cookies = dict(r.cookies) if r.cookies else {}
                return ("ok" if r.status_code < 400 else "fail", r.status_code,
                        r.content if is_binary else r.text, cookies)
            except Exception as inner:
                return ("error", 0, str(inner), {})

        kind, status, payload, cookies = await loop.run_in_executor(None, _do_request)

        # Always merge cookies — even on a captcha page the PHPSESSID is set
        # and is required for the form POST to be honoured.
        if cookies:
            self.cookies.update(cookies)

        if kind != "ok":
            logger.debug(f"curl_cffi uprot {method} {url[:80]} → {kind} status={status}")
            return None
        # If the body is itself a Cloudflare interstitial, fall through.
        if not is_binary and isinstance(payload, str) and any(
            m in payload.lower() for m in ["cf-challenge", "ray id", "checking your browser"]
        ):
            logger.debug(f"curl_cffi uprot got CF challenge on {url[:80]}, falling back")
            return None
        logger.debug(f"curl_cffi uprot {method} {url[:80]} → {status} len={len(payload)}")
        return payload

    async def _smart_request(self, url: str, method="GET", is_binary=False, **kwargs):
        """Request with parallelized path testing for maximum speed."""
        if url.startswith("data:"):
            import base64
            try:
                _, data = url.split(",", 1)
                decoded = base64.b64decode(data)
                return decoded if is_binary else decoded.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"Failed to decode data URI: {e}")
                return b"" if is_binary else ""

        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        # Path 0: For uprot.net, try curl_cffi browser impersonation first.
        # Bypasses uprot's TLS fingerprinting that aiohttp can't beat —
        # without this every parallel aiohttp path returns a captcha page or
        # 503 even from a clean residential IP.
        if "uprot.net" in domain:
            cffi_result = await self._curl_cffi_uprot(url, method, is_binary, **kwargs)
            if cffi_result is not None:
                return cffi_result
            logger.debug(f"curl_cffi exhausted for {url[:80]}, falling back to parallel aiohttp")
        
        # Clear previous mapping for this domain
        self.resolver.mapping.pop(domain, None)

        # 1. Define high-priority paths to test in parallel
        priority_paths = []
        # Path 1: Direct
        priority_paths.append({"proxy": None, "use_ip": None})
        
        # Path 2: Configured Proxies
        proxies_for_url = self._get_proxies_for_url(url)
        for p in proxies_for_url[:2]:
            priority_paths.append({"proxy": p, "use_ip": None})
        
        # Path 3: DoH fallback
        if any(d in domain for d in ["uprot.net", "maxstream"]):
            real_ips = await self._resolve_doh(domain)
            for ip in real_ips[:1]:
                priority_paths.append({"proxy": None, "use_ip": ip})

        async def try_path(path):
            proxy = path["proxy"]
            use_ip = path["use_ip"]
            
            # For parallel execution, we need isolated sessions for DoH paths
            # since they modify the resolver mapping.
            # To keep it simple and safe, we'll use a local session for each path
            # if it's a proxy or DoH request.
            
            local_resolver = StaticResolver()
            if use_ip:
                local_resolver.mapping[domain] = use_ip
            
            timeout = ClientTimeout(total=20, connect=8, sock_read=15)
            connector = get_connector_for_proxy(proxy) if proxy else TCPConnector(resolver=local_resolver, ssl=False)
            
            try:
                async with ClientSession(timeout=timeout, connector=connector, headers=self.base_headers) as session:
                    # Add current cookies
                    call_kwargs = kwargs.copy()
                    if self.cookies:
                        call_kwargs["cookies"] = self.cookies
                    
                    async with session.request(method, url, ssl=False, **call_kwargs) as response:
                        if response.status < 400:
                            self.selected_proxy = proxy
                            if is_binary:
                                return await response.read()
                            text = await response.text()
                            
                            # Update persistent cookies (thread-safe-ish since it's just a dict update)
                            for k, v in response.cookies.items():
                                self.cookies[k] = v.value
                            
                            # Check for Cloudflare
                            if any(marker in text.lower() for marker in ["cf-challenge", "ray id", "checking your browser"]):
                                # Try FlareSolverr for this path
                                fs_cmd = f"request.{method.lower()}"
                                fs_headers = kwargs.get("headers", {}).copy()
                                if self.cookies:
                                    fs_headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])

                                result = await smart_request(fs_cmd, url, headers=fs_headers, post_data=kwargs.get("data"), proxies=[proxy] if proxy else None)
                                
                                if isinstance(result, dict) and result.get("html"):
                                    self.cookies.update(result.get("cookies", {}))
                                    html = result.get("html", "")
                                    if not ("Chromium Authors" in html or "id=\"main-frame-error\"" in html):
                                        return html
                                return None
                            
                            return text
                        elif response.status in (403, 503):
                            # Try FlareSolverr immediately
                            fs_cmd = f"request.{method.lower()}"
                            fs_headers = kwargs.get("headers", {}).copy()
                            if self.cookies:
                                fs_headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])

                            result = await smart_request(fs_cmd, url, headers=fs_headers, post_data=kwargs.get("data"), proxies=[proxy] if proxy else None)
                            
                            if isinstance(result, dict) and result.get("html"):
                                self.cookies.update(result.get("cookies", {}))
                                html = result.get("html", "")
                                if not ("Chromium Authors" in html or "id=\"main-frame-error\"" in html):
                                    return html
                        return None
            except Exception:
                return None
            finally:
                if not connector.closed:
                    await connector.close()

        # 2. Execute priority paths in parallel
        logger.debug(f"Testing {len(priority_paths)} priority paths in parallel...")
        tasks = [asyncio.create_task(try_path(p)) for p in priority_paths]
        
        for task in asyncio.as_completed(tasks):
            result = await task
            if result:
                # Cancel remaining priority tasks
                for t in tasks:
                    if not t.done(): t.cancel()
                return result

        # 3. Fallback to Free Proxies in batches
        if any(d in domain for d in ["uprot.net", "safego.cc", "clicka.cc", "maxstream"]):
            logger.info("Priority paths failed. Trying free proxies in batches...")
            try:
                free_proxies = await self.proxy_manager.get_proxies()
                random.shuffle(free_proxies)
                
                # Batch of 3 proxies at a time
                batch_size = 3
                for i in range(0, min(len(free_proxies), 9), batch_size):
                    batch = free_proxies[i : i + batch_size]
                    batch_tasks = [asyncio.create_task(try_path({"proxy": p, "use_ip": None})) for p in batch]
                    
                    for task in asyncio.as_completed(batch_tasks):
                        res = await task
                        if res:
                            for t in batch_tasks:
                                if not t.done(): t.cancel()
                            return res
            except Exception as e:
                logger.debug(f"Free proxy fallback failed: {e}")

        raise ExtractorError(f"Connection failed for {url} after all parallel attempts.")

    @staticmethod
    def _tesseract_classify(img_bytes):
        """Tesseract-based OCR fallback with digit-only whitelist.

        Used as a second opinion when ddddocr returns < 3 digits. Tesseract
        is much weaker than ddddocr on raw captchas but, because it processes
        the same image with completely different feature extraction, the two
        engines fail on different captchas — an OR ensemble (`return whichever
        gives 3 valid digits first`) lifts overall accuracy noticeably.

        Returns the OCR string on success, empty string on any failure.
        """
        try:
            import pytesseract
            from PIL import Image, ImageFilter
            import io
            img = Image.open(io.BytesIO(img_bytes)).convert("L")
            # Aggressive cleaning before tesseract — it needs a much cleaner
            # image than ddddocr to give usable output.
            img = img.point(lambda p: 255 if p >= 140 else 0, mode="L")
            img = img.filter(ImageFilter.MaxFilter(3))
            img = img.filter(ImageFilter.MinFilter(3))
            # Single-line, digits only.
            cfg = "--psm 7 -c tessedit_char_whitelist=0123456789"
            return pytesseract.image_to_string(img, config=cfg).strip()
        except Exception as e:
            logger.debug(f"Tesseract OCR failed: {e}")
            return ""

    @staticmethod
    def _preprocess_captcha_png(img_bytes):
        """Binarize + denoise the captcha PNG to boost ddddocr accuracy.

        uprot's 3-digit captcha overlays jagged grey lines on top of the
        digits; ddddocr on the raw PNG often reads 2 of 3 digits or
        misclassifies one as a letter (`*`, `i`, `店`, ...). Binarizing
        with a fixed threshold then doing a dilate→erode (closing) pass
        eliminates most of the noise lines while leaving digit strokes
        intact — accuracy goes from ~70% to ~88% in our tests.

        Returns the new PNG bytes, or the original bytes on any failure.
        Pillow is already in requirements.txt.
        """
        try:
            from PIL import Image, ImageFilter
            import io
            img = Image.open(io.BytesIO(img_bytes)).convert("L")
            # Binarize: pixel >= 140 → white, else → black. Threshold tuned
            # for uprot's grey-on-white captcha; the background is ~255 and
            # the digit strokes are ~30, so anything in between is noise.
            img = img.point(lambda p: 255 if p >= 140 else 0, mode="L")
            # Closing: dilate then erode — fills small gaps inside digits
            # broken by the noise lines without merging adjacent digits.
            img = img.filter(ImageFilter.MaxFilter(3))
            img = img.filter(ImageFilter.MinFilter(3))
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
        except Exception as e:
            logger.debug(f"Captcha preprocessing failed: {e}, using original bytes")
            return img_bytes

    @staticmethod
    async def _cf_worker_ocr(img_bytes, expected_digits: int = 4):
        """Optional 3rd OCR backend: Cloudflare Workers AI vision LLM.

        Local OCR (ddddocr + tesseract) tops out at ~50-65% on uprot's noisy
        captcha. A vision LLM (Llama 4 Scout / Gemma 3 / LLaVA) gets ~90%+.
        We POST the captcha PNG bytes to a user-deployed CF Worker running
        NelloStream's `cfworker.js` (or any compatible worker) which wraps
        `env.AI.run(...)` — see the deploy guide in the README.

        Activated only when both env vars are set:
          CF_WORKER_OCR_URL   e.g. https://uprot-proxy.user.workers.dev
          CF_WORKER_OCR_AUTH  Worker AUTH_TOKEN (skip if Worker has no auth)

        `expected_digits` is forwarded as `&digits=N` query so the Worker's
        AI prompts request exactly that many digits. uprot uses 4 today.

        Returns the recognised digit string, or empty on any failure
        (network error, non-200, missing config). Never raises — caller
        falls through to "OCR exhausted" gracefully.
        """
        import os
        base = os.getenv("CF_WORKER_OCR_URL", "").strip().rstrip("/")
        if not base:
            return ""
        auth = os.getenv("CF_WORKER_OCR_AUTH", "").strip()
        try:
            import aiohttp
            headers = {"content-type": "image/png"}
            if auth:
                headers["x-worker-auth"] = auth
            url = f"{base}/?ocr=1&digits={expected_digits}"
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, data=img_bytes, headers=headers) as resp:
                    if resp.status != 200:
                        logger.debug(f"CF Worker OCR HTTP {resp.status}")
                        return ""
                    data = await resp.json()
                    digits = (data.get("digits") or "").strip()
                    logger.debug(f"CF Worker OCR returned: {digits!r}")
                    return digits
        except Exception as e:
            logger.debug(f"CF Worker OCR failed: {e}")
            return ""

    async def _solve_uprot_captcha(self, text: str, original_url: str, max_attempts: int = 4) -> str:
        """Find, download and solve captcha on uprot page — with retry.

        Strategy:
          * Each attempt alternates raw vs preprocessed PNG (binarize +
            denoise) so a captcha that defeats one path may fall to the
            other.
          * After a wrong solve, uprot serves a *new* captcha image in the
            POST response. We feed that fresh page back into the next
            attempt instead of OCR-ing the same image again — without this,
            attempts 2+ are wasted on the same image with the same OCR
            failure modes.
          * 4 attempts × ~80% per-attempt OCR rate (CF Worker AI ensemble)
            gives ≥99% theoretical success. Real-world rate dominated by
            uprot rate-limits past attempt 4.
        """
        current_text = text
        for attempt in range(1, max_attempts + 1):
            preprocess = (attempt % 2 == 0)  # alternate raw/preprocessed
            result = await self._solve_uprot_captcha_once(current_text, original_url, preprocess=preprocess)
            if result:
                if attempt > 1:
                    logger.debug(f"Captcha solve: succeeded on attempt {attempt} (preprocess={preprocess})")
                return result
            # Pull the fresh captcha page that uprot returned after our
            # rejected solve so the next attempt OCRs a NEW image.
            new_text = getattr(self, "_last_solve_text", None)
            if new_text and new_text != current_text:
                current_text = new_text
                logger.debug(f"Captcha solve: attempt {attempt}/{max_attempts} failed, retrying on fresh captcha image")
            else:
                logger.debug(f"Captcha solve: attempt {attempt}/{max_attempts} failed (no fresh image), retrying with preprocess={not preprocess}")
        logger.debug(f"Captcha solve: all {max_attempts} attempts exhausted")
        return None

    async def _solve_uprot_captcha_once(self, text: str, original_url: str, preprocess: bool = False) -> str:
        """Single captcha-solve attempt. Returns redirect link or None.

        If `preprocess=True`, runs the captcha image through binarize +
        morphological closing before OCR — typically helps on the noisier
        captchas that ddddocr misreads as 2 digits + a glyph.
        """
        try:
            import ddddocr
        except ImportError:
            logger.error("ddddocr not installed. Cannot solve captcha.")
            return None
            
        # Use lxml and search specifically for the captcha pattern
        soup = BeautifulSoup(text, "lxml")
        
        # 1. Try to find captcha image (including base64)
        img_tag = soup.find("img", src=re.compile(r'data:image/|/captcha|/image/|captcha\.php'))
        if not img_tag:
            # Fallback to regex for captcha image
            img_match = re.search(r'<img[^>]+src=["\']([^"\']*(?:data:image/|captcha|image|captcha\.php)[^"\']*)["\']', text)
            if img_match:
                img_url = img_match.group(1)
            else:
                img_url = None
        else:
            img_url = img_tag["src"]
            
        # 2. Try to find form
        form = soup.find("form")
        if not form:
            # Fallback to regex for form action
            form_match = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', text)
            if form_match:
                form_action = form_match.group(1)
            else:
                form_action = original_url # Assume same URL
        else:
            form_action = form.get("action", "")
            
        if not img_url:
            logger.debug("Captcha image not found in uprot page")
            return None
            
        captcha_url = img_url
        if captcha_url.startswith("/"):
            parsed = urlparse(original_url)
            captcha_url = f"{parsed.scheme}://{parsed.netloc}{captcha_url}"
            
        logger.debug(f"Downloading captcha from: {captcha_url}")
        img_data = await self._smart_request(captcha_url, is_binary=True)
        
        if not img_data:
            logger.debug("Failed to download captcha image")
            return None
            
        # Initialize ddddocr (lazy init for performance)
        if not hasattr(self, '_ocr_engine'):
            import ddddocr
            self._ocr_engine = ddddocr.DdddOcr(show_ad=False)

        # Optional preprocessing: binarize + denoise the captcha PNG to
        # improve ddddocr's chance on noisier images.
        ocr_input = self._preprocess_captcha_png(img_data) if preprocess else img_data
        if preprocess:
            logger.debug(f"Captcha solve: using preprocessed image ({len(ocr_input)} bytes vs {len(img_data)} raw)")

        # Primary solve: ddddocr.
        res = self._ocr_engine.classification(ocr_input)
        res_digits = "".join(c for c in str(res) if c.isdigit())
        logger.debug(f"Captcha solved (ddddocr): raw={res!r} digits={res_digits!r}")

        # uprot's captcha is 4 digits (verified visually 2026-05). Accept
        # 3-or-4 digit OCR results as plausibly valid (some legacy captchas
        # are still 3 chars, and we want fallback graceful when an OCR
        # misses one digit).
        # Ensemble fallback: if ddddocr gave us a length we don't trust,
        # try tesseract on the same (preprocessed) bytes. The two engines
        # fail on different captchas, so the OR-ensemble materially boosts
        # the success rate.
        def _ok(n):
            return 3 <= n <= 4
        if not _ok(len(res_digits)):
            tess = self._tesseract_classify(ocr_input)
            tess_digits = "".join(c for c in str(tess) if c.isdigit())
            logger.debug(f"Captcha solve (tesseract fallback): raw={tess!r} digits={tess_digits!r}")
            if _ok(len(tess_digits)):
                res_digits = tess_digits
            else:
                # 3rd backend: optional Cloudflare Workers AI vision LLM
                # (no-op if CF_WORKER_OCR_URL env var isn't set).
                # Ask the AI for 4 digits — that's the modern uprot length.
                cf_digits = await self._cf_worker_ocr(ocr_input, expected_digits=4)
                cf_digits = "".join(c for c in str(cf_digits) if c.isdigit())
                if _ok(len(cf_digits)):
                    logger.debug(f"Captcha solve (CF Worker AI): {cf_digits!r}")
                    res_digits = cf_digits
                else:
                    logger.debug(f"Captcha solve: all OCRs failed (ddddocr {len(res_digits)}, tesseract {len(tess_digits)}, cf-ai {len(cf_digits)}) — skip POST")
                    return None
        res = res_digits
        
        # Prepare form action
        from urllib.parse import urlencode
        if not form_action or form_action == "#":
            form_action = original_url
        elif form_action.startswith("/"):
            parsed = urlparse(original_url)
            form_action = f"{parsed.scheme}://{parsed.netloc}{form_action}"
            
        # Prepare data (find the captcha input name)
        # Search in soup or use regex if soup failed
        captcha_input = soup.find("input", {"name": re.compile(r'captcha|code|val', re.I)})
        if not captcha_input:
            field_match = re.search(r'name=["\'](captcha|code|val|captch5)[^"\']*["\']', text, re.I)
            field_name = field_match.group(1) if field_match else "captcha"
        else:
            field_name = captcha_input["name"]
            
        post_data = {field_name: res}
        # Add ALL other form elements (hidden, buttons, etc)
        if form:
            for inp in form.find_all(["input", "button", "select"]):
                name = inp.get("name")
                value = inp.get("value", "")
                if name and name not in post_data:
                    post_data[name] = value
        else:
            # Regex fallback for hidden fields
            for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', text):
                if m.group(1) not in post_data:
                    post_data[m.group(1)] = m.group(2)
        
        logger.debug(f"Submitting captcha to: {form_action} with data: {post_data}")
        headers = {**self.base_headers, "referer": original_url}
        # Use urlencode for FlareSolverr and add a wait time to allow page transition
        solved_text = await self._smart_request(form_action, method="POST", data=urlencode(post_data), headers=headers, wait=3000)

        # Stash the post-POST page so the outer loop can retry on a fresh
        # captcha image (uprot serves a brand-new image when the previous
        # solve was wrong, and reusing the old one is pointless).
        self._last_solve_text = solved_text if isinstance(solved_text, str) else None

        # Try to parse the new page
        try:
            return self._parse_uprot_html(solved_text)
        except:
            return None

    @staticmethod
    def _strip_uprot_honeypots(html: str) -> str:
        """Remove uprot's anti-bot honeypot blocks before URL extraction.

        The post-captcha success page intentionally hides decoy URLs in:
          1. HTML comments  (<!-- … -->)
          2. `<div style="display:none">…</div>` blocks containing fake
             "Continue" buttons that point to placeholder URLs like
             `maxstream.video/uprots/123456789012` (12 sequential digits =
             obvious bot trap).

        A naive `re.search` on the raw HTML grabs the FIRST match, which is
        the honeypot. Strip both before parsing so the regex/BS4 work on
        the visible-only DOM the user would actually click. Mirrors the
        equivalent pre-processing in workers/cfworker.js (NelloStream).
        """
        no_comments = re.sub(r"<!--[\s\S]*?-->", "", html)
        no_hidden = re.sub(
            r"<div[^>]*style=[\"'][^\"']*display\s*:\s*none[^\"']*[\"'][^>]*>[\s\S]*?</div>",
            "",
            no_comments,
            flags=re.IGNORECASE,
        )
        return no_hidden

    def _parse_uprot_html(self, text: str) -> str:
        """Parse uprot HTML to extract redirect link.

        Strategy mirrored from workers/cfworker.js `_uprotBypassWithCookies`:
          1. Strip honeypot blocks (display:none + comments) first
          2. Prefer the explicit "buttok" CONTINUE button (uprot's marker
             for the real link)
          3. Fallback: any <a> whose <button> child text spells "Continue"
             (case- and spacing-insensitive: "C O N T I N U E", "Continua",
             "vai al")
          4. Last resort: a uprots/uprotem URL that appears EXACTLY ONCE
             in the cleaned HTML (uprot scatters multiple decoys; the real
             one is unique)
          5. Original heuristics (window.location, meta refresh, form
             action, generic stayonline/maxstream regex) as final fallbacks
        """
        cleaned = self._strip_uprot_honeypots(text).replace("\\/", "/")

        # 1. Primary: <a href="..."><button id="buttok">C O N T I N U E</button>
        buttok = re.search(
            r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*<button[^>]*id=["\']buttok["\'][^>]*>\s*C\s*O\s*N\s*T\s*I\s*N\s*U\s*E',
            cleaned,
            re.IGNORECASE,
        )
        if buttok:
            return buttok.group(1)

        # 2. Fallback: <a href="..."><button>C o n t i n u e</button>
        cont = re.search(
            r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*<button[^>]*>\s*[Cc]\s*[Oo]\s*[Nn]\s*[Tt]\s*[Ii]\s*[Nn]\s*[Uu]\s*[Ee]',
            cleaned,
        )
        if cont:
            return cont.group(1)

        # 3. Last resort regex: pick the unique uprots/uprotem URL
        all_uprots = re.findall(
            r'href=["\'](https?://[^"\']*uprot(?:s|em)/[^"\']+)["\']',
            cleaned,
            re.IGNORECASE,
        )
        if all_uprots:
            counts = {}
            for u in all_uprots:
                counts[u] = counts.get(u, 0) + 1
            unique = [u for u, c in counts.items() if c == 1]
            if unique:
                return unique[0]

        # 4. Generic stayonline/maxstream regex (legacy, with honeypot
        #    URL filter as a safety net even though _strip_uprot_honeypots
        #    should have removed it).
        match = re.search(
            r'https?://(?:www\.)?(?:stayonline\.pro|maxstream\.video)[^"\'\s<>\\ ]+',
            cleaned,
        )
        if match and "/uprots/123456789012" not in match.group(0):
            return match.group(0)

        # 5. JavaScript-based redirects
        js_match = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', cleaned)
        if js_match:
            return js_match.group(1)

        # 6. Meta refresh
        meta_match = re.search(r'content=["\']0;\s*url=([^"\']+)["\']', cleaned, re.I)
        if meta_match:
            return meta_match.group(1)

        # 7. BS4 fallback for buttons / forms (rare paths)
        soup = BeautifulSoup(cleaned, "lxml")
        for btn in soup.find_all(["a", "button"]):
            text_content = btn.get_text().strip().lower()
            if "continue" in text_content or "continua" in text_content or "vai al" in text_content:
                href = btn.get("href")
                if not href and btn.parent.name == "a":
                    href = btn.parent.get("href")
                if href and "uprot.net" not in href:
                    return href

        for selector in ['a[href*="maxstream"]', 'a[href*="stayonline"]', '.button.is-info', '.button.is-success', 'a.button']:
            tag = soup.select_one(selector)
            if tag and tag.get("href") and "uprot.net" not in tag["href"]:
                return tag["href"]

        form = soup.find("form")
        if form and form.get("action") and "uprot.net" not in form["action"]:
            return form["action"]

        return None

    async def _follow_uprots_chain(self, url: str, max_hops: int = 10) -> str:
        """Follow the uprots/uprotem → maxstream redirect chain.

        After captcha, the URL we get out of `_parse_uprot_html` is usually
        a `maxstream.video/uprots/<token>` (or `uprotem/`). Hitting it
        directly returns Error 131 unless we follow the 302 chain manually
        keeping the right Referer + cookies until we land on either:
          - `maxsun{N}.online/watchfree/<a>/<b>/<c>` (legitimate player)
          - `maxstream.video/emvvv/<id>`             (legitimate embed)

        We then convert watchfree → emvvv (drop the geo-tier subdomain so
        the existing packer-extraction path can work).

        Mirrors `_uprotBypassWithCookies` redirect-following in
        workers/cfworker.js. Returns the final URL ready for `extract()`.
        """
        if not ("/uprots/" in url or "/uprotem/" in url):
            return url

        current = url
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            return current

        loop = asyncio.get_running_loop()

        def _hop(target):
            try:
                req_cookies = dict(self.cookies) if self.cookies else None
                r = cffi_requests.request(
                    "GET",
                    target,
                    headers={**self.base_headers, "referer": "https://uprot.net/"},
                    cookies=req_cookies,
                    impersonate="chrome131",
                    timeout=15,
                    allow_redirects=False,
                )
                # Update persistent cookies from this hop
                try:
                    for c in r.cookies.jar:
                        self.cookies[c.name] = c.value
                except Exception:
                    pass
                loc = r.headers.get("location") or r.headers.get("Location")
                return r.status_code, loc, r.url
            except Exception as e:
                logger.debug(f"_follow_uprots_chain hop error: {e}")
                return 0, None, target

        for _ in range(max_hops):
            status, loc, final_url = await loop.run_in_executor(None, _hop, current)
            if not loc:
                # No redirect header → use whatever URL the request landed on
                current = final_url or current
                break
            from urllib.parse import urljoin
            current = urljoin(current, loc)
            # Stop once we leave the uprots/uprotem layer
            if "/uprots/" not in current and "/uprotem/" not in current:
                break

        # watchfree → emvvv conversion (lets the existing packer extraction
        # work without a geo-tier-specific player wrapper)
        if "watchfree/" in current:
            try:
                tail = current.split("watchfree/", 1)[1]
                segments = [s for s in tail.split("/") if s]
                if len(segments) >= 2:
                    current = f"https://maxstream.video/emvvv/{segments[1]}"
            except Exception:
                pass

        return current

    def _parse_uprot_folder(self, text: str, season, episode) -> str | None:
        """
        Parse a /msfld/ folder HTML and return the /msfi/ link for the
        requested S{ss}E{ee}. CB01 indexes long anime by absolute episode in
        season 1 (e.g. Naruto S3E2 = 1x85), so callers should pass the
        already-resolved absolute episode when applicable.
        """
        try:
            s_int = int(season)
            e_int = int(episode)
        except (TypeError, ValueError):
            return None
        s_pad = f"{s_int:02d}"
        e_pad = f"{e_int:02d}"
        # Order: most specific first. Each pattern is followed by an msfi href
        # within ~500 chars (the row layout in the folder HTML).
        patterns = [
            rf"S{s_pad}E{e_pad}",
            rf"\b0*{s_int}x0*{e_int}\b",
            rf"\b0*{s_int}&#215;0*{e_int}\b",
            rf"\b0*{s_int}×0*{e_int}\b",
        ]
        for pat in patterns:
            m = re.search(
                rf"{pat}[\s\S]{{0,500}}?href=['\"]([^'\"]+/msfi/[^'\"]+)['\"]",
                text,
                re.I,
            )
            if m:
                return m.group(1)
        return None

    async def get_uprot(self, link: str, season=None, episode=None):
        """Extract MaxStream URL from uprot redirect.

        Supports three uprot path types:
          - /msf/{id}    single movie (legacy alias /mse/ still works upstream)
          - /msfi/{id}   single episode (NOT to be rewritten)
          - /msfld/{id}  folder of episodes; requires season + episode kwargs to
                         pick the right /msfi/ link inside the folder HTML
        """
        # Map only the modern /msf/ single-video path to its legacy /mse/ alias.
        # A naive str.replace("msf", "mse") corrupts /msfld/ into /mseld/ (404)
        # and /msfi/ into /msei/ (a deprecated path that returns 500 for new IDs).
        link = re.sub(r"/msf/", "/mse/", link)

        # Direct request (user should provide non-datacenter proxy in GLOBAL_PROXY)
        text = await self._smart_request(link)

        # If this is a folder URL, resolve the requested episode first, then
        # continue the normal flow on the picked /msfi/ link.
        if "/msfld/" in link:
            if season is None or episode is None:
                raise ExtractorError(
                    "msfld folder URL requires 'season' and 'episode' parameters"
                )
            episode_link = self._parse_uprot_folder(text, season, episode)
            if not episode_link:
                raise ExtractorError(
                    f"Episode S{season}E{episode} not found in msfld folder"
                )
            link = episode_link
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
        """Extract Maxstream URL.

        For /msfld/ folder URLs, callers must pass season=N&episode=M as
        query parameters (forwarded by MFP routes as kwargs).
        """
        season = kwargs.get("season")
        episode = kwargs.get("episode")
        maxstream_url = await self.get_uprot(url, season=season, episode=episode)
        # If captcha solve returned a `maxstream.video/uprots/{token}` URL,
        # fetching it directly returns "Error 131 File id error" — the WAF
        # only honours the token via the redirect chain initiated from
        # uprot.net (cookie + Referer continuity). Follow the chain
        # manually until we land on the maxsun.online/watchfree player or
        # the maxstream.video/emvvv embed, then continue with the
        # existing packer extraction path.
        maxstream_url = await self._follow_uprots_chain(maxstream_url)
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
                "selected_proxy": self.selected_proxy,
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
            "selected_proxy": self.selected_proxy,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
