import asyncio
import html
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyError as AioProxyError
from python_socks import ProxyError as PyProxyError
from config import TRANSPORT_ROUTES, GLOBAL_PROXIES, WARP_PROXY_URL, get_connector_for_proxy, SELECTED_PROXY_CONTEXT, STRICT_PROXY_CONTEXT, get_solver_proxy_url, get_extractor_proxies, get_ordered_proxies_for_url, should_allow_direct_fallback, mark_proxy_dead, DEAD_PROXIES, _proxy_lock, FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT
from config import PROXY_TEST_TIMEOUT, PROXY_TEST_CONCURRENCY

from utils.cookie_cache import CookieCache
from utils.solver_manager import ensure_flaresolverr

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""


class VixSrcExtractor:
    """VixSrc URL extractor per risolvere link VixSrc."""
    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = None):
        self.bypass_warp_active = bypass_warp if bypass_warp is not None else False  # Use WARP by default
        self.request_headers = request_headers
        self.base_headers = self._default_headers()
        self.session = None
        self.session_proxy = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.proxies = []
        for proxy in list(proxies or []) + list(GLOBAL_PROXIES):
            if proxy and proxy not in self.proxies:
                self.proxies.append(proxy)
        self.is_vixsrc = True
        self.extractor_name = "vixsrc"
        self.last_used_proxy = None
        self.last_used_direct = False
        self.cookies: dict[str, str] = {}
        self.cookie_cache = CookieCache("vixsrc")
        self._domain_cookies_loaded = set()
        logger.info(
            "VixSrc proxy config: transport_routes=%d dedicated_proxies=%d fallback_proxies=%d",
            len(TRANSPORT_ROUTES),
            len(self._dedicated_proxies()),
            len(self.proxies or []),
        )
    @staticmethod
    def _normalize_proxy_url(proxy_value: str) -> str:
        proxy_value = unquote(proxy_value)
        proxy_value = proxy_value.strip()
        if proxy_value.startswith("socks5://"):
            return proxy_value.replace("socks5://", "socks5h://", 1)
        if proxy_value.startswith("socks4://") or proxy_value.startswith("socks4a://"):
            return proxy_value
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    def _dedicated_proxies(self) -> list[str]:
        proxies = []
        global_proxies = {self._normalize_proxy_url(proxy) for proxy in GLOBAL_PROXIES if proxy}
        warp_proxy = self._normalize_proxy_url(WARP_PROXY_URL) if WARP_PROXY_URL else None
        for proxy in get_extractor_proxies(self.extractor_name):
            if not proxy:
                continue
            proxy = self._normalize_proxy_url(proxy)
            if proxy not in proxies:
                proxies.append(proxy)
        for proxy in self.proxies:
            if not proxy:
                continue
            proxy = self._normalize_proxy_url(proxy)
            if proxy in global_proxies or proxy == warp_proxy:
                continue
            if proxy not in proxies:
                proxies.append(proxy)
        return proxies

    def _has_strict_proxy_source(self, forced_proxy: str | None = None) -> bool:
        return bool(forced_proxy or self._dedicated_proxies())

    async def _proxy_candidates(self, url: str, forced_proxy: str | None = None) -> list[str]:
        if forced_proxy:
            proxy = self._normalize_proxy_url(forced_proxy)
            return [proxy]

        dedicated = self._dedicated_proxies()
        if not dedicated:
            return get_ordered_proxies_for_url(url, self.extractor_name, self.proxies, bypass_warp=self.bypass_warp_active)

        # Skip socket check - rely on DEAD_PROXIES + curl_cffi rotation for liveness
        now = time.time()
        with _proxy_lock:
            alive = [p for p in dedicated if p not in DEAD_PROXIES or now >= DEAD_PROXIES.get(p, 0)]
        if alive:
            return alive
        # All dedicated proxies dead — fall back to general resolution (direct, WARP, etc.)
        return get_ordered_proxies_for_url(url, self.extractor_name, self.proxies, bypass_warp=self.bypass_warp_active)

    async def _preferred_proxy(self, url: str, forced_proxy: str | None = None) -> str | None:
        candidates = await self._proxy_candidates(url, forced_proxy)
        return candidates[0] if candidates else None

    @staticmethod
    def _default_headers() -> dict:
        return {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
        }


    def _fresh_headers(self, **extra_headers) -> dict:
        headers = self._default_headers()
        headers.update(extra_headers)
        return headers

    def _get_domain(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    def _load_cached_cookies(self, url: str):
        domain = self._get_domain(url)
        if domain in self._domain_cookies_loaded:
            return
        self._domain_cookies_loaded.add(domain)
        cached = self.cookie_cache.get(domain)
        if cached and cached.get("cookies"):
            self.cookies.update(cached["cookies"])
            logger.info("Loaded %d cached cookies for %s", len(cached["cookies"]), domain)
        else:
            parts = domain.split(".")
            if len(parts) > 2:
                parent = ".".join(parts[-2:])
                cached = self.cookie_cache.get(parent)
                if cached and cached.get("cookies"):
                    self.cookies.update(cached["cookies"])
                    logger.info("Loaded %d cached cookies for parent domain %s", len(cached["cookies"]), parent)

    def _save_cached_cookies(self, url: str):
        if not self.cookies:
            return
        domain = self._get_domain(url)
        ua = self._default_headers().get("user-agent", "")
        self.cookie_cache.set(domain, dict(self.cookies), ua)
        logger.info("Saved %d cookies to cache for %s", len(self.cookies), domain)

    def _extract_cookies_from_curl(self, resp) -> dict:
        try:
            if hasattr(resp, 'cookies') and resp.cookies is not None:
                jar = resp.cookies.jar if hasattr(resp.cookies, 'jar') else resp.cookies
                return {c.name: c.value for c in jar}
        except Exception:
            pass
        return {}

    async def _make_curl_request(self, url: str, headers: dict = None, forced_proxy: str | None = None):
        """Fetch Cloudflare-protected embeds with curl_cffi and proxy rotation."""
        from curl_cffi.requests import AsyncSession as CurlAsyncSession

        class MockResponse:
            def __init__(self, text_content, status, response_url):
                self._text = text_content
                self.status = status
                self.status_code = status
                self.text = text_content
                self.url = response_url
                self.headers = {}

            async def text_async(self):
                return self._text

            def raise_for_status(self):
                if self.status >= 400:
                    raise ExtractorError(f"curl_cffi HTTP error {self.status} for {self.url}")

        self._load_cached_cookies(url)

        proxies_to_try = await self._proxy_candidates(url, forced_proxy)
        if not proxies_to_try and self._has_strict_proxy_source(forced_proxy):
            raise ExtractorError("No alive VixSrc dedicated/forced proxy available")
        preferred_proxy = proxies_to_try[0] if proxies_to_try else None
        logger.info(
            "VixSrc curl proxy lookup: url=%s transport_routes=%d dedicated_proxies=%d fallback_proxies=%d resolved=%d preferred_proxy=%s",
            url,
            len(TRANSPORT_ROUTES),
            len(self._dedicated_proxies()),
            len(self.proxies or []),
            len(proxies_to_try),
            preferred_proxy,
        )
        # If a proxy is configured, respect it. Direct is only allowed when no
        # proxy route exists; otherwise direct can win the curl_cffi race and
        # produce tokens for a different IP than streaming uses.
        if not self._has_strict_proxy_source(forced_proxy) and should_allow_direct_fallback(proxies_to_try):
            proxies_to_try.append(None)

        impersonations = ["chrome131", "chrome124", "chrome120"]
        last_status = None
        last_error = None
        final_headers = self._fresh_headers(**(headers or {}))

        # Remove User-Agent to avoid TLS fingerprint mismatch with impersonation
        final_headers.pop("User-Agent", None)
        final_headers.pop("user-agent", None)

        timeout = PROXY_TEST_TIMEOUT
        concurrency = PROXY_TEST_CONCURRENCY
        request_cookies = dict(self.cookies) if self.cookies else None

        async def _try_one(proxy_value: str | None, imp: str):
            request_kwargs = {}
            proxy = self._normalize_proxy_url(proxy_value) if proxy_value else None
            if proxy:
                request_kwargs["proxies"] = {"http": proxy, "https": proxy}
            try:
                async with CurlAsyncSession(impersonate=imp) as session:
                    resp = await session.get(
                        url,
                        headers=final_headers,
                        cookies=request_cookies,
                        timeout=timeout,
                        allow_redirects=True,
                        **request_kwargs,
                    )
                    content = resp.text
                if 200 <= resp.status_code < 300:
                    new_cookies = self._extract_cookies_from_curl(resp)
                    if new_cookies:
                        self.cookies.update(new_cookies)
                        self._save_cached_cookies(url)
                    return True, proxy, MockResponse(content, resp.status_code, url), None, resp.status_code
                if proxy_value and resp.status_code not in (403, 404):
                    mark_proxy_dead(proxy_value)
                return False, proxy, None, None, resp.status_code
            except Exception as exc:
                if proxy_value:
                    mark_proxy_dead(proxy_value)
                return False, proxy, None, exc, None

        specific = [p for p in get_extractor_proxies(self.extractor_name) if p in proxies_to_try]
        proxy_batches = [specific, [p for p in proxies_to_try if p not in specific]] if specific else [proxies_to_try]

        for imp in impersonations:
            if asyncio.current_task().cancelled():
                logger.info("Extraction cancelled, skipping remaining impersonations for %s", url)
                raise asyncio.CancelledError()
            logger.info(
                "VixSrc curl_cffi testing %d proxies for %s (imp=%s, concurrency=%d, timeout=%ss)",
                len(proxies_to_try), url, imp, concurrency, timeout,
            )
            semaphore = asyncio.Semaphore(concurrency)

            async def _limited(proxy_value):
                async with semaphore:
                    return await _try_one(proxy_value, imp)

            for proxy_batch in proxy_batches:
                if not proxy_batch:
                    continue
                tasks = [asyncio.create_task(_limited(proxy_value)) for proxy_value in proxy_batch]
                try:
                    for task in asyncio.as_completed(tasks):
                        ok, proxy, response, exc, status = await task
                        if ok:
                            for pending in tasks:
                                if not pending.done():
                                    pending.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            self.last_used_proxy = proxy
                            self.last_used_direct = proxy is None
                            logger.info("curl_cffi success via %s for %s (imp=%s)", proxy or "direct", url, imp)
                            return response
                        if isinstance(status, int):
                            last_status = status
                        if exc:
                            last_error = exc
                finally:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    try:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
                    except asyncio.TimeoutError:
                        pass

        if last_error:
            raise ExtractorError(f"curl_cffi request failed for {url}: {last_error}")
        if last_status is not None:
            raise ExtractorError(f"curl_cffi HTTP error {last_status} for {url}")
        raise ExtractorError(f"curl_cffi failed for {url}: no usable proxy found")

    @staticmethod
    def _normalize_base_site(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ExtractorError("Invalid VixSrc URL")
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    def _build_session_for_proxy(self, proxy: str | None) -> ClientSession:
        timeout = ClientTimeout(total=60, connect=30, sock_read=30)
        if proxy:
            logger.debug("Using proxy %s for VixSrc session.", proxy)
            connector = get_connector_for_proxy(proxy, ssl=False)
        else:
            connector = TCPConnector(
                limit=0,
                limit_per_host=0,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
                force_close=False,
                use_dns_cache=True,
                ssl=False,
            )
        return ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self._default_headers(),
            cookie_jar=aiohttp.CookieJar(),
        )

    @staticmethod
    def _raise_if_embed_expired(url: str):
        parsed = urlparse(url)
        if "/embed/" not in parsed.path:
            return
        expires = parse_qs(parsed.query).get("expires", [None])[0]
        if not expires:
            return
        try:
            expires_ts = int(expires)
        except (TypeError, ValueError):
            return
        now_ts = int(time.time())
        if expires_ts <= now_ts:
            raise ExtractorError(
                f"Expired VixSrc embed URL (expired at {expires_ts}, current {now_ts}). "
                "Use the original /movie/ or /tv/ URL to refresh tokens."
            )

    async def _get_session(self, url: str = None, forced_proxy: str | None = None):
        """Ottiene una sessione HTTP persistente."""
        proxy = None
        if forced_proxy:
            proxy = self._normalize_proxy_url(forced_proxy)
        elif url:
            proxy = await self._preferred_proxy(url)
        else:
            proxy = self._get_random_proxy()
        if proxy:
            proxy = self._normalize_proxy_url(proxy)
        self.last_used_proxy = proxy
        self.last_used_direct = proxy is None

        if self.session is not None and not self.session.closed and self.session_proxy != proxy:
            await self.session.close()
            self.session = None

        if self.session is None or self.session.closed:
            self.session_proxy = proxy
            self.session = self._build_session_for_proxy(proxy)
        return self.session

    async def _make_robust_request(
        self, url: str, headers: dict = None, retries: int = 2, initial_delay: int = 2, forced_proxy: str | None = None
    ):
        """Effettua richieste HTTP robuste con retry automatico e proxy rotation."""
        final_headers = headers or {}
        last_error = None

        for attempt in range(retries):
            try:
                if last_error is not None:
                    # Close session and force a different proxy on retry
                    try:
                        await self.session.close()
                    except Exception:
                        pass
                    self.session = None
                    if self.session_proxy:
                        mark_proxy_dead(self.session_proxy)
                        self.session_proxy = None
                    forced_proxy = None  # Don't reuse dead proxy

                session = await self._get_session(url, forced_proxy=forced_proxy)
                logger.info("Attempt %s/%s for URL: %s", attempt + 1, retries, url)

                async with session.get(url, headers=final_headers, timeout=aiohttp.ClientTimeout(total=15, connect=10)) as response:
                    response.raise_for_status()
                    content = await response.text()

                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict, response_url):
                            self._text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = response_url
                            self.status_code = status
                            self.text = text_content

                        async def text_async(self):
                            return self._text

                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status,
                                )

                    logger.info("Request successful for %s at attempt %s", url, attempt + 1)
                    return MockResponse(content, response.status, response.headers, response.url)

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError,
                AioProxyError,
                PyProxyError,
            ) as e:
                is_proxy_err = isinstance(e, (AioProxyError, PyProxyError))
                is_timeout = isinstance(e, asyncio.TimeoutError)
                err_type = "Proxy" if is_proxy_err else ("Timeout" if is_timeout else "Connection")
                
                logger.warning(
                    "%s error attempt %s for %s: %s", err_type, attempt + 1, url, str(e)
                )

                # Reset session
                if self.session and not self.session.closed:
                    try:
                        await self.session.close()
                    except Exception:
                        pass
                self.session = None
                
                if self.session_proxy:
                    mark_proxy_dead(self.session_proxy)

                if is_proxy_err and SELECTED_PROXY_CONTEXT.get() and not STRICT_PROXY_CONTEXT.get():
                    logger.info("Clearing sticky proxy context due to ProxyError")
                    SELECTED_PROXY_CONTEXT.set(None)


                if attempt < retries - 1:
                    delay = initial_delay * (2**attempt)
                    logger.info("Waiting %s seconds before next attempt...", delay)
                    await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All {retries} attempts failed for {url}: {str(e)}")

            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    raise ExtractorError(f"VixSrc content not found (404): {url}")

                if e.status == 403 and attempt == retries - 1:
                    try:
                        logger.info("aiohttp 403, trying FlareSolverr for %s", url)
                        fs_html = await self._fetch_with_flaresolverr(url, headers=final_headers, forced_proxy=forced_proxy)
                        if fs_html:
                            class MockResponse:
                                def __init__(self, text_content, status, response_url):
                                    self._text = text_content
                                    self.status = status
                                    self.status_code = status
                                    self.text = text_content
                                    self.url = response_url
                                    self.headers = {}
                                async def text_async(self):
                                    return self._text
                                def raise_for_status(self):
                                    pass
                            return MockResponse(fs_html, 200, url)
                    except Exception as fs_exc:
                        logger.warning("FlareSolverr fallback failed for %s: %s", url, fs_exc)
                    try:
                        logger.info("aiohttp 403, trying curl_cffi with configured proxies for %s", url)
                        headers_403 = final_headers or self._default_headers()
                        return await self._make_curl_request(url, headers=headers_403, forced_proxy=forced_proxy)
                    except Exception as cffi_exc:
                        logger.warning("curl_cffi fallback failed for %s: %s", url, cffi_exc)

                if attempt == retries - 1:
                    raise ExtractorError(f"Final HTTP error {e.status} for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

            except Exception as e:
                logger.error("Non-network error attempt %s for %s: %s", attempt + 1, url, str(e))
                if attempt == retries - 1:
                    raise ExtractorError(f"Final error for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

    async def _fetch_with_flaresolverr(self, url: str, headers: dict = None, forced_proxy: str | None = None) -> str | None:
        """Fallback a FlareSolverr per bypassare challenge Cloudflare."""
        if not FLARESOLVERR_URL:
            logger.info("FlareSolverr not configured, skipping")
            return None

        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }

        fs_proxy = forced_proxy or await self._preferred_proxy(url)
        if fs_proxy:
            payload["proxy"] = {"url": get_solver_proxy_url(fs_proxy)}

        cookie_header = (headers or {}).get("Cookie") or (headers or {}).get("cookie")
        if cookie_header:
            parsed = urlparse(url)
            payload["cookies"] = [
                {
                    "name": key.strip(),
                    "value": value.strip(),
                    "domain": parsed.hostname,
                    "path": "/",
                    "secure": parsed.scheme == "https",
                }
                for item in cookie_header.split(";")
                if "=" in item
                for key, value in [item.split("=", 1)]
            ]

        await ensure_flaresolverr()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{FLARESOLVERR_URL.rstrip('/')}/v1",
                    json=payload,
                    timeout=ClientTimeout(total=FLARESOLVERR_TIMEOUT + 95),
                ) as resp:
                    data = await resp.json()
        except Exception as exc:
            logger.warning("FlareSolverr vixsrc failed for %s: %s", url, exc)
            return None

        if data.get("status") != "ok":
            logger.warning("FlareSolverr vixsrc error for %s: %s", url, data.get("message"))
            return None

        solution = data.get("solution", {})
        html = solution.get("response", "")
        if html and not any(marker in html.lower() for marker in ("just a moment", "cf-challenge", "checking your browser")):
            logger.info("FlareSolverr success for %s", url)
            new_cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
            if new_cookies:
                self.cookies.update(new_cookies)
                self._save_cached_cookies(url)
            return html

        logger.warning("FlareSolverr vixsrc returned Cloudflare challenge for %s", url)
        return None

    async def _parse_html_simple(self, html_content: str, tag: str, attrs: dict = None):
        """Parser HTML semplificato senza BeautifulSoup."""
        try:
            if tag == "div" and attrs and attrs.get("id") == "app":
                pattern = r'<div[^>]*id="app"[^>]*data-page="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"data-page": match.group(1)}

            elif tag == "iframe":
                pattern = r'<iframe[^>]*src="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"src": match.group(1)}

            elif tag == "script":
                scripts = re.findall(
                    r"<script[^>]*>(.*?)</script>",
                    html_content,
                    re.DOTALL | re.IGNORECASE,
                )
                for script in scripts:
                    if "window.masterPlaylist" in script or "'token':" in script:
                        return script

                pattern = r"<body[^>]*>.*?<script[^>]*>(.*?)</script>"
                match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(1)

        except Exception as e:
            logger.error("HTML parsing error: %s", e)

        return None

    async def _resolve_embed_url_from_api(self, url: str, forced_proxy: str | None = None) -> str | None:
        """Resolve the current embed URL through VixSrc JSON API."""
        parsed = urlparse(url)
        site_url = self._normalize_base_site(url)
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]

        api_url = None
        if len(path_parts) >= 2 and path_parts[0] == "movie":
            api_url = f"{site_url}/api/movie/{path_parts[1]}"
        elif len(path_parts) >= 4 and path_parts[0] == "tv":
            api_url = f"{site_url}/api/tv/{path_parts[1]}/{path_parts[2]}/{path_parts[3]}"

        if not api_url:
            return None

        api_headers = {
            "accept": "application/json, text/plain, */*",
            "referer": url,
            **self._default_headers(),
        }
        try:
            logger.info("Trying VixSrc API via curl_cffi proxy rotation: %s", api_url)
            response = await self._make_curl_request(api_url, headers=api_headers, forced_proxy=forced_proxy)
        except Exception as curl_err:
            # 404 means content not found — FS won't help, skip cascading fallbacks
            if "404" in str(curl_err):
                raise ExtractorError(f"VixSrc API endpoint not found (404): {api_url}")
            logger.warning("curl_cffi failed for API, trying robust: %s", curl_err)
            try:
                response = await self._make_robust_request(api_url, headers=api_headers, forced_proxy=None)
            except Exception as robust_err:
                if "404" in str(robust_err):
                    raise ExtractorError(f"VixSrc content not found (404): {api_url}")
                raise ExtractorError(f"VixSrc API fetch failed: {robust_err}") from robust_err

        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            text = None
            # Try <pre> tag (Chrome JSON viewer wraps JSON in <pre>)
            pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", response.text, re.DOTALL)
            if pre_match:
                text = html.unescape(pre_match.group(1))
            else:
                # Try direct JSON with HTML entities decoded
                stripped = response.text.strip()
                if stripped.startswith("{"):
                    text = html.unescape(stripped)
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc2:
                    raise ExtractorError(f"Invalid API response from {api_url}: {exc2}")
            else:
                raise ExtractorError(f"Invalid API response from {api_url}: response is not JSON")

        embed_path = payload.get("src")
        if not embed_path:
            raise ExtractorError(f"Missing embed src in API response from {api_url}")

        return urljoin(site_url, embed_path)

    def _extract_playlist_from_embed(self, script_content: str) -> str:
        """Extract playlist URL from current embed structure, with legacy fallback."""
        master_playlist_match = re.search(
            r"window\.masterPlaylist\s*=\s*\{.*?params\s*:\s*\{(?P<params>.*?)\}\s*,\s*url\s*:\s*['\"](?P<url>[^'\"]+)['\"]",
            script_content,
            re.DOTALL,
        )
        if master_playlist_match:
            params_block = master_playlist_match.group("params")
            playlist_url = master_playlist_match.group("url").replace("\\/", "/")

            token_match = re.search(
                r"['\"]token['\"]\s*:\s*['\"]([^'\"]+)['\"]", params_block
            )
            expires_match = re.search(
                r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", params_block
            )
            asn_match = re.search(
                r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", params_block
            )

            if token_match and expires_match:
                parsed_playlist_url = urlparse(playlist_url)
                query_params = parse_qsl(parsed_playlist_url.query, keep_blank_values=True)
                query_params.extend(
                    [
                        ("token", token_match.group(1)),
                        ("expires", expires_match.group(1)),
                    ]
                )
                if "window.canPlayFHD = true" in script_content or "canPlayFHD" in script_content:
                    query_params.append(("h", "1"))
                query_params.append(("lang", "it"))
                if asn_match and asn_match.group(1):
                    query_params.append(("asn", asn_match.group(1)))
                return urlunparse(parsed_playlist_url._replace(query=urlencode(query_params)))

        token_match = re.search(r"['\"]token['\"]\s*:\s*['\"](\w+)['\"]", script_content)
        expires_match = re.search(r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", script_content)
        server_url_match = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", script_content)

        if not all([token_match, expires_match, server_url_match]):
            token_match = token_match or re.search(
                r"token['\"]\s*:\s*['\"]([^'\"]+)['\"]", script_content
            )
            expires_match = expires_match or re.search(
                r"expires['\"]\s*:\s*['\"](\d+)['\"]", script_content
            )

        if not all([token_match, expires_match, server_url_match]):
            raise ExtractorError("Missing mandatory parameters in JS script (token/expires/url)")

        server_url = server_url_match.group(1).replace("\\/", "/")
        parsed_server_url = urlparse(server_url)
        query_params = parse_qsl(parsed_server_url.query, keep_blank_values=True)
        query_params.extend(
            [
                ("token", token_match.group(1)),
                ("expires", expires_match.group(1)),
            ]
        )

        if "window.canPlayFHD = true" in script_content or "canPlayFHD" in script_content:
            query_params.append(("h", "1"))

        query_params.append(("lang", "it"))
        asn_match = re.search(r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", script_content)
        if asn_match and asn_match.group(1):
            query_params.append(("asn", asn_match.group(1)))

        return urlunparse(parsed_server_url._replace(query=urlencode(query_params)))

    async def version(self, site_url: str, forced_proxy: str | None = None) -> str:
        """Ottiene la versione del sito VixSrc parent."""
        base_url = f"{site_url}/request-a-title"

        response = await self._make_robust_request(
            base_url,
            headers={
                "Referer": f"{site_url}/",
                "Origin": f"{site_url}",
                **self._default_headers(),
            },
            forced_proxy=forced_proxy,
        )

        if response.status_code != 200:
            raise ExtractorError("Obsolete URL")

        app_div = await self._parse_html_simple(response.text, "div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            try:
                data_page = app_div["data-page"].replace("&quot;", '"')
                data = json.loads(data_page)
                return data["version"]
            except (KeyError, json.JSONDecodeError, AttributeError) as e:
                raise ExtractorError(f"Version parsing failure: {e}")

        raise ExtractorError("Unable to find version data")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Estrae URL VixSrc."""
        try:
            forced_proxy = kwargs.get("proxy")
            if forced_proxy:
                forced_proxy = self._normalize_proxy_url(forced_proxy)
            parsed_url = urlparse(url)
            self._load_cached_cookies(url)
            response = None

            if "/playlist/" in parsed_url.path:
                logger.info("URL is already a VixSrc manifest, no extraction required.")
                selected_proxy = forced_proxy or parse_qs(parsed_url.query).get("proxy", [None])[0]
                if not selected_proxy:
                    selected_proxy = self.last_used_proxy or await self._preferred_proxy(url)
                if selected_proxy:
                    selected_proxy = self._normalize_proxy_url(selected_proxy)
                logger.debug(f"Extractor Debug: Extractor result selected_proxy: {selected_proxy}")
                stream_headers = self._fresh_headers()
                # Use cookies and UA from the request (e.g. cf_clearance forwarded by redirect)
                req_h = kwargs.get("request_headers") or {}
                if req_h.get("Cookie"):
                    stream_headers["Cookie"] = req_h["Cookie"]
                if req_h.get("User-Agent"):
                    stream_headers["User-Agent"] = req_h["User-Agent"]

                return {
                    "destination_url": url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "selected_proxy": selected_proxy,
                    "force_direct": bool(kwargs.get("force_direct")) or (selected_proxy is None and self.last_used_direct),
                    "bypass_warp": self.bypass_warp_active,
                }

            if "/embed/" in parsed_url.path:
                self._raise_if_embed_expired(url)
                if parsed_url.netloc.lower().endswith("vixcloud.co"):
                    vix_url = url.replace("vixcloud.co", "vixsrc.to")
                    logger.info("Rewrote URL to vixsrc.to: %s", vix_url)
                else:
                    vix_url = url
                try:
                    response = await self._make_curl_request(
                        vix_url,
                        headers=self._fresh_headers(referer=self._normalize_base_site(vix_url) + "/"),
                        forced_proxy=forced_proxy,
                    )
                except Exception as curl_err:
                    logger.warning("curl_cffi failed for embed %s, trying FlareSolverr: %s", vix_url, curl_err)
                    fs_html = await self._fetch_with_flaresolverr(
                        vix_url,
                        headers=self._fresh_headers(referer=self._normalize_base_site(vix_url) + "/"),
                        forced_proxy=forced_proxy,
                    )
                    if fs_html:
                        class MockResponse:
                            def __init__(self, text_content, status, response_url):
                                self._text = text_content
                                self.status = status
                                self.status_code = status
                                self.text = text_content
                                self.url = response_url
                                self.headers = {}
                            async def text_async(self):
                                return self._text
                            def raise_for_status(self):
                                pass
                        response = MockResponse(fs_html, 200, vix_url)
                    else:
                        raise ExtractorError(f"VixSrc embed fetch failed: {curl_err}") from curl_err
            elif "iframe" in url:
                site_url = url.split("/iframe")[0]
                version = await self.version(site_url, forced_proxy=None)
                response = await self._make_robust_request(
                    url,
                    headers=self._fresh_headers(
                        **{"x-inertia": "true", "x-inertia-version": version}
                    ),
                    forced_proxy=None,
                )

                iframe_data = await self._parse_html_simple(response.text, "iframe")
                if iframe_data and iframe_data.get("src"):
                    iframe_url = iframe_data["src"]
                    response = await self._make_robust_request(
                        iframe_url,
                        headers=self._fresh_headers(
                            **{"x-inertia": "true", "x-inertia-version": version}
                        ),
                        forced_proxy=None,
                    )
                else:
                    raise ExtractorError("No iframe found in response")
            elif "/movie/" in parsed_url.path or "/tv/" in parsed_url.path:
                embed_url = await self._resolve_embed_url_from_api(url, forced_proxy=forced_proxy)
                if embed_url:
                    try:
                        response = await self._make_curl_request(
                            embed_url,
                            headers=self._fresh_headers(referer=url),
                            forced_proxy=forced_proxy,
                        )
                    except Exception as curl_err:
                        logger.warning("curl_cffi failed for embed %s, trying FlareSolverr: %s", embed_url, curl_err)
                        fs_html = await self._fetch_with_flaresolverr(
                            embed_url,
                            headers=self._fresh_headers(referer=url),
                            forced_proxy=forced_proxy,
                        )
                        if fs_html:
                            class MockResponse:
                                def __init__(self, text_content, status, response_url):
                                    self._text = text_content
                                    self.status = status
                                    self.status_code = status
                                    self.text = text_content
                                    self.url = response_url
                                    self.headers = {}
                                async def text_async(self):
                                    return self._text
                                def raise_for_status(self):
                                    pass
                            response = MockResponse(fs_html, 200, embed_url)
                        else:
                            logger.warning("FlareSolverr failed for embed %s, trying robust: %s", embed_url, curl_err)
                            try:
                                response = await self._make_robust_request(
                                    embed_url,
                                    headers=self._fresh_headers(referer=url),
                                    forced_proxy=None,
                                )
                            except Exception as robust_err:
                                raise ExtractorError(f"VixSrc embed fetch failed: {robust_err}") from robust_err
                else:
                    try:
                        response = await self._make_curl_request(url, forced_proxy=forced_proxy)
                    except Exception as curl_err:
                        logger.warning("curl_cffi failed for %s, trying FlareSolverr: %s", url, curl_err)
                        fs_html = await self._fetch_with_flaresolverr(
                            url,
                            headers=self._fresh_headers(),
                            forced_proxy=forced_proxy,
                        )
                        if fs_html:
                            class MockResponse:
                                def __init__(self, text_content, status, response_url):
                                    self._text = text_content
                                    self.status = status
                                    self.status_code = status
                                    self.text = text_content
                                    self.url = response_url
                                    self.headers = {}
                                async def text_async(self):
                                    return self._text
                                def raise_for_status(self):
                                    pass
                            response = MockResponse(fs_html, 200, url)
                        else:
                            logger.warning("FlareSolverr failed for %s, trying robust: %s", url, curl_err)
                            try:
                                response = await self._make_robust_request(url, forced_proxy=None)
                            except Exception as robust_err:
                                raise ExtractorError(f"VixSrc URL fetch failed: {robust_err}") from robust_err
            else:
                raise ExtractorError("Unsupported VixSrc URL type")

            if response.status_code != 200:
                raise ExtractorError("URL component extraction failed, invalid request")

            async def _extract_from_html(html: str) -> str | None:
                """Try to extract playlist URL from HTML via script content, then data-page JSON."""
                script = await self._parse_html_simple(html, "script")
                if script:
                    try:
                        return self._extract_playlist_from_embed(script)
                    except ExtractorError:
                        pass
                app_div = await self._parse_html_simple(html, "div", {"id": "app"})
                if not app_div or not app_div.get("data-page"):
                    return None
                try:
                    data_page = app_div["data-page"].replace("&quot;", '"')
                    data = json.loads(data_page)
                    def _search_json(obj):
                        results = {}
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                kl = k.lower()
                                if kl in ("token", "expires", "url", "src") and isinstance(v, str):
                                    results[kl] = v
                                elif not (results.get("token") and results.get("expires") and results.get("url")):
                                    results.update(_search_json(v))
                        elif isinstance(obj, list):
                            for item in obj:
                                results.update(_search_json(item))
                                if results.get("token") and results.get("expires") and results.get("url"):
                                    break
                        return results
                    found = _search_json(data)
                    if found.get("token") and found.get("expires") and found.get("url"):
                        parsed_url = urlparse(found["url"])
                        query_params = parse_qsl(parsed_url.query, keep_blank_values=True)
                        query_params.extend([("token", found["token"]), ("expires", found["expires"])])
                        if "canPlayFHD" in html:
                            query_params.append(("h", "1"))
                        query_params.append(("lang", "it"))
                        return urlunparse(parsed_url._replace(query=urlencode(query_params)))
                except (json.JSONDecodeError, Exception):
                    pass
                return None

            final_url = await _extract_from_html(response.text)

            if not final_url:
                raise ExtractorError("No playlist data found in response")

            # Rewrite vixcloud.co → vixsrc.to in the final URL too
            final_url = final_url.replace("vixcloud.co", "vixsrc.to")
            stream_url = url.replace("vixcloud.co", "vixsrc.to")

            stream_headers = self._fresh_headers(Referer=stream_url)

            logger.info("VixSrc URL extracted successfully: %s", final_url)
            return {
                "destination_url": final_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
                "selected_proxy": self.last_used_proxy,
                "force_direct": self.last_used_proxy is None and self.last_used_direct,
                "bypass_warp": self.bypass_warp_active,
            }

        except Exception as e:
            logger.error("VixSrc extraction failed: %s", str(e))
            raise ExtractorError(f"VixSrc extraction completely failed: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None
            self.session_proxy = None
