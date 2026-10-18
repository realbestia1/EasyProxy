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
from config_store import DEFAULT_RECORDINGS_DIR
from config import WARP_PROXY_URL, get_connector_for_proxy, SELECTED_PROXY_CONTEXT, STRICT_PROXY_CONTEXT, get_solver_proxy_url, get_extractor_proxies, get_ordered_proxies_for_url, should_allow_direct_fallback, mark_proxy_dead, DEAD_PROXIES, _proxy_lock, ALL_PROXY_ERRORS
import config as _cfg
from extractors.base import ExtractorError
from services.flaresolverr import FlareSolverrSolution, shutdown_flare_solver, solve_cloudflare

logger = logging.getLogger(__name__)

VIXSRC_CONFIG_URL = "https://raw.githubusercontent.com/realbestia1/domains/refs/heads/main/domains.json"
_vixsrc_domain = None
_vixsrc_config_loaded_at = 0.0
_VIXSRC_DATA_DIR = os.path.dirname(DEFAULT_RECORDINGS_DIR)
_VIXSRC_COOKIE_FILE = os.getenv("VIXSRC_COOKIE_FILE") or os.path.join(
    _VIXSRC_DATA_DIR, "cookies", "vixsrc.json"
)
try:
    _VIXSRC_COOKIE_TTL = max(300, int(os.getenv("VIXSRC_COOKIE_TTL", "7200")))
except (TypeError, ValueError):
    _VIXSRC_COOKIE_TTL = 7200
_VIXSRC_COOKIE_LOCK = threading.RLock()


class CloudflareChallengeError(ExtractorError):
    """Challenge rilevata ma non superata: errore terminale, senza retry."""


class _CurlResponse:
    """Small response adapter shared by curl_cffi and FlareSolverr results."""

    def __init__(self, text_content: str, status: int, response_url: str, headers: dict | None = None):
        self._text = text_content
        self.status = status
        self.status_code = status
        self.text = text_content
        self.url = response_url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status >= 400:
            raise ExtractorError(f"HTTP error {self.status} for {self.url}")


class VixSrcExtractor:
    """VixSrc URL extractor per risolvere link VixSrc."""
    # Includes API/embed fetches and the solver's startup + 60s challenge budget.
    REQUEST_TIMEOUT_TOTAL = 180
    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = None):
        self.bypass_warp_active = bypass_warp if bypass_warp is not None else False  # Use WARP by default
        self.request_headers = request_headers
        self.base_headers = self._default_headers()
        self.session = None
        self._route_sessions = {}
        self.session_proxy = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = []
        for proxy in list(proxies or []) + list(_cfg.GLOBAL_PROXIES):
            if proxy and proxy not in self.proxies:
                self.proxies.append(proxy)
        self.is_vixsrc = True
        self.extractor_name = "vixsrc"
        self.last_used_proxy = None
        self.last_used_direct = False
        self._initial_cookie_header = self._header_value(request_headers, "Cookie")
        self._solver_cookie_header = self._initial_cookie_header
        self._solver_user_agent = ""
        logger.info(
            "VixSrc proxy config: transport_routes=%d dedicated_proxies=%d fallback_proxies=%d",
            len(_cfg.TRANSPORT_ROUTES),
            len(self._dedicated_proxies()),
            len(self.proxies or []),
        )

    async def _refresh_vixsrc_domain(self) -> None:
        global _vixsrc_domain, _vixsrc_config_loaded_at
        if time.monotonic() - _vixsrc_config_loaded_at < 60:
            return
        try:
            response = await self._make_curl_request(
                VIXSRC_CONFIG_URL,
                headers={"Accept": "application/json"},
            )
            config = json.loads(response.text)
            domain = str(config.get("vixsrc", "")).strip().lower()
            if domain:
                _vixsrc_domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
            _vixsrc_config_loaded_at = time.monotonic()
        except CloudflareChallengeError:
            raise
        except Exception as exc:
            logger.warning("Unable to refresh VixSrc domain config: %s", exc)

    @staticmethod
    def _replace_vixsrc_domain(url: str) -> str:
        if not _vixsrc_domain:
            return url
        return url.replace("vixcloud.co", _vixsrc_domain).replace("vixsrc.to", _vixsrc_domain)
    @staticmethod
    def _normalize_proxy_url(proxy_value: str) -> str:
        if not proxy_value:
            return ""
        proxy_value = unquote(str(proxy_value)).strip()
        if not proxy_value or proxy_value.lower() in (
            "off", "false", "0", "direct", "none", "no", "on", "auto", "true"
        ):
            return ""
        proxy_value = _cfg.resolve_proxy_alias(proxy_value) or proxy_value
        # Preserve explicit SOCKS5 routes. Scheme-less third-party proxies
        # retain the existing socks5h default.
        if proxy_value.startswith("socks5://"):
            return proxy_value
        if proxy_value.startswith("socks4://") or proxy_value.startswith("socks4a://"):
            return proxy_value
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    def _dedicated_proxies(self) -> list[str]:
        proxies = []
        global_proxies = {self._normalize_proxy_url(proxy) for proxy in _cfg.GLOBAL_PROXIES if proxy}
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
        # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
        forced_proxy = _cfg.effective_forced_proxy(url, forced_proxy)
        if forced_proxy:
            proxy = self._normalize_proxy_url(forced_proxy)
            if self.bypass_warp_active and proxy == self._normalize_proxy_url(WARP_PROXY_URL):
                return []
            return [proxy]

        # The central resolver owns route priority. Filter only routes already
        # marked dead; keep the remaining candidates in resolver order so a
        # failed per-extractor proxy can fall through to global/WARP.
        candidates = get_ordered_proxies_for_url(
            url,
            self.extractor_name,
            self.proxies,
            bypass_warp=self.bypass_warp_active,
        )
        now = time.time()
        with _proxy_lock:
            return [
                proxy for proxy in candidates
                if proxy not in DEAD_PROXIES or now >= DEAD_PROXIES.get(proxy, 0)
            ]

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
        return self._apply_solver_headers(headers)

    @staticmethod
    def _header_value(headers: dict | None, name: str) -> str:
        if not headers:
            return ""
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value or "")
        return ""

    @staticmethod
    def _merge_cookie_headers(*cookie_headers: str | None) -> str:
        merged = {}
        for header in cookie_headers:
            for item in (header or "").split(";"):
                name, separator, value = item.strip().partition("=")
                if name and separator:
                    merged[name.strip()] = value.strip()
        return "; ".join(f"{name}={value}" for name, value in merged.items())

    @staticmethod
    def _cookie_cache_domain(url: str | None) -> str:
        return (urlparse(url or "").hostname or "").lower().lstrip(".")

    @staticmethod
    def _cookie_header_from_items(cookies) -> str:
        return "; ".join(
            f"{cookie.get('name')}={cookie.get('value', '')}"
            for cookie in (cookies or [])
            if isinstance(cookie, dict) and cookie.get("name")
        )

    @staticmethod
    def _read_cookie_cache() -> dict:
        try:
            with open(_VIXSRC_COOKIE_FILE, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
            domains = payload.get("domains", {}) if isinstance(payload, dict) else {}
            return domains if isinstance(domains, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Unable to read VixSrc cookie cache: %s", exc)
            return {}

    @staticmethod
    def _write_cookie_cache(domains: dict) -> None:
        os.makedirs(os.path.dirname(_VIXSRC_COOKIE_FILE) or ".", exist_ok=True)
        with open(_VIXSRC_COOKIE_FILE, "w", encoding="utf-8") as cache_file:
            json.dump({"domains": domains}, cache_file, ensure_ascii=False)

    def _load_cached_solver_state(self, url: str) -> None:
        domain = self._cookie_cache_domain(url)
        if not domain:
            return
        with _VIXSRC_COOKIE_LOCK:
            domains = self._read_cookie_cache()
            entry = domains.get(domain)
            if not isinstance(entry, dict):
                return
            try:
                expires_at = float(entry.get("expires_at", 0) or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at <= time.time():
                domains.pop(domain, None)
                self._write_cookie_cache(domains)
                return
            cached_header = self._cookie_header_from_items(entry.get("cookies"))
            if cached_header:
                self._solver_cookie_header = self._merge_cookie_headers(
                    self._solver_cookie_header,
                    cached_header,
                )
            cached_ua = str(entry.get("user_agent") or "")
            if cached_ua:
                self._solver_user_agent = cached_ua
            logger.debug("Loaded VixSrc solver cookies for %s", domain)

    def _save_solver_solution(self, url: str, solution: FlareSolverrSolution) -> None:
        domain = self._cookie_cache_domain(url) or self._cookie_cache_domain(solution.url)
        if not domain:
            return
        new_cookies = [
            dict(cookie)
            for cookie in solution.cookies
            if isinstance(cookie, dict) and cookie.get("name")
        ]
        with _VIXSRC_COOKIE_LOCK:
            domains = self._read_cookie_cache()
            current = domains.get(domain) if isinstance(domains.get(domain), dict) else {}
            merged = {}
            current_cookies = current.get("cookies", [])
            if isinstance(current_cookies, list):
                for cookie in current_cookies:
                    if isinstance(cookie, dict) and cookie.get("name"):
                        merged[str(cookie["name"])] = cookie
            for cookie in new_cookies:
                merged[str(cookie["name"])] = cookie
            user_agent = solution.user_agent or str(current.get("user_agent") or "")
            if not merged and not user_agent:
                return

            now = time.time()
            cookie_expiries = []
            for cookie in merged.values():
                try:
                    expiry = float(cookie.get("expiry", 0) or 0)
                    if expiry > 10_000_000_000:
                        expiry /= 1000
                    if expiry > now:
                        cookie_expiries.append(expiry)
                except (TypeError, ValueError):
                    pass
            expires_at = min(cookie_expiries) if cookie_expiries else now + _VIXSRC_COOKIE_TTL
            domains[domain] = {
                "cookies": list(merged.values()),
                "user_agent": user_agent,
                "expires_at": expires_at,
            }
            try:
                self._write_cookie_cache(domains)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("Unable to save VixSrc cookie cache: %s", exc)
                return
        logger.info("Saved %d VixSrc solver cookies for %s", len(merged), domain)

    def _invalidate_cached_solver_state(self, url: str) -> None:
        domain = self._cookie_cache_domain(url)
        if domain:
            with _VIXSRC_COOKIE_LOCK:
                domains = self._read_cookie_cache()
                if domains.pop(domain, None) is not None:
                    try:
                        self._write_cookie_cache(domains)
                    except OSError as exc:
                        logger.warning("Unable to clear VixSrc cookie cache: %s", exc)
                    logger.info("Invalidated VixSrc solver cookies for %s after 403", domain)
        self._solver_cookie_header = self._initial_cookie_header
        self._solver_user_agent = ""

    def _apply_solver_headers(self, headers: dict | None) -> dict:
        result = dict(headers or {})
        if self._solver_cookie_header:
            existing_cookie = self._header_value(result, "Cookie")
            for key in list(result):
                if str(key).lower() == "cookie":
                    result.pop(key, None)
            result["Cookie"] = self._merge_cookie_headers(existing_cookie, self._solver_cookie_header)
        if self._solver_user_agent:
            for key in list(result):
                if str(key).lower() == "user-agent":
                    result.pop(key, None)
            result["User-Agent"] = self._solver_user_agent
        return result

    def _remember_solver_solution(
        self,
        solution: FlareSolverrSolution,
        proxy: str | None,
        url: str | None = None,
    ) -> None:
        if solution.cookie_header:
            self._solver_cookie_header = self._merge_cookie_headers(
                self._solver_cookie_header,
                solution.cookie_header,
            )
        if solution.user_agent:
            self._solver_user_agent = solution.user_agent
        self.last_used_proxy = self._normalize_proxy_url(proxy) if proxy else None
        self.last_used_direct = proxy is None
        self._save_solver_solution(url or solution.url, solution)
        logger.info(
            "VixSrc FlareSolverr solved challenge: route=%s solver_proxy=%s cookies=%d",
            self.last_used_proxy or "direct",
            get_solver_proxy_url(self.last_used_proxy) or "direct",
            len(solution.cookies),
        )

    async def _solve_cloudflare(self, url: str, headers: dict | None = None, forced_proxy: str | None = None):
        proxy = forced_proxy or self.session_proxy
        if proxy:
            proxy = self._normalize_proxy_url(proxy)
        allow_direct = _cfg.is_direct_connection_allowed(self.bypass_warp_active)
        solution = await solve_cloudflare(
            url,
            proxy_url=get_solver_proxy_url(proxy),
            cookie_header=self._header_value(headers, "Cookie"),
            allow_direct=allow_direct,
        )
        self._remember_solver_solution(solution, proxy, url=url)
        return solution

    async def _flaresolverr_response(
        self,
        url: str,
        headers: dict | None = None,
        forced_proxy: str | None = None,
    ):
        solution = await self._solve_cloudflare(url, headers=headers, forced_proxy=forced_proxy)
        return _CurlResponse(solution.response, solution.status, solution.url)

    async def _make_curl_request(self, url: str, headers: dict = None, forced_proxy: str | None = None):
        """Fetch Cloudflare-protected embeds with curl_cffi and proxy rotation."""
        from curl_cffi.requests import AsyncSession as CurlAsyncSession

        self._load_cached_solver_state(url)
        proxies_to_try = await self._proxy_candidates(url, forced_proxy)
        if not proxies_to_try and forced_proxy:
            raise ExtractorError("No alive VixSrc forced proxy available")
        if not proxies_to_try and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError("No alive VixSrc proxy route available; direct fallback disabled")
        preferred_proxy = proxies_to_try[0] if proxies_to_try else None
        logger.info(
            "VixSrc curl proxy lookup: url=%s transport_routes=%d dedicated_proxies=%d fallback_proxies=%d resolved=%d preferred_proxy=%s",
            url,
            len(_cfg.TRANSPORT_ROUTES),
            len(self._dedicated_proxies()),
            len(self.proxies or []),
            len(proxies_to_try),
            preferred_proxy,
        )
        # Direct is an explicit WARP-off opt-in only, and never a fallback for
        # an explicitly forced proxy.
        if not forced_proxy and should_allow_direct_fallback(
            proxies_to_try,
            bypass_warp=self.bypass_warp_active,
        ):
            proxies_to_try.append(None)

        impersonations = ["chrome131", "chrome124", "chrome120"]
        last_status = None
        last_error = None
        final_headers = self._fresh_headers(**(headers or {}))

        # Remove User-Agent to avoid TLS fingerprint mismatch with impersonation
        if not self._solver_user_agent:
            final_headers.pop("User-Agent", None)
            final_headers.pop("user-agent", None)

        timeout = _cfg.PROXY_TEST_TIMEOUT
        async def _try_one(proxy_value: str | None, imp: str):
            request_kwargs = {}
            proxy = self._normalize_proxy_url(proxy_value) if proxy_value else None
            if proxy:
                request_kwargs["proxies"] = {"http": proxy, "https": proxy}
            try:
                curl_options = _cfg.get_curl_ipv4_options(proxy).get("curl_options")
                session_kwargs = {"impersonate": imp}
                if curl_options:
                    session_kwargs["curl_options"] = curl_options
                async with CurlAsyncSession(
                    **session_kwargs,
                ) as session:
                    resp = await session.get(
                        url,
                        headers=final_headers,
                        timeout=timeout,
                        allow_redirects=True,
                        **request_kwargs,
                    )
                    content = resp.text
                if 200 <= resp.status_code < 300:
                    is_challenge = self._is_cloudflare_challenge(content, resp.status_code)
                    if not is_challenge:
                        return True, proxy, _CurlResponse(content, resp.status_code, url), None, resp.status_code, False
                else:
                    is_challenge = self._is_cloudflare_challenge(content, resp.status_code)
                if resp.status_code == 403 and not is_challenge:
                    self._invalidate_cached_solver_state(url)
                if proxy_value and resp.status_code not in (403, 404, 503) and not is_challenge:
                    mark_proxy_dead(proxy_value)
                return False, proxy, None, None, resp.status_code, is_challenge
            except Exception as exc:
                if proxy_value:
                    mark_proxy_dead(proxy_value)
                return False, proxy, None, exc, None, False

        challenge_proxy = None
        challenge_detected = False
        for imp in impersonations:
            if asyncio.current_task().cancelled():
                logger.info("Extraction cancelled, skipping remaining impersonations for %s", url)
                raise asyncio.CancelledError()
            logger.info(
                "VixSrc curl_cffi testing %d routes in priority order for %s (imp=%s, timeout=%ss)",
                len(proxies_to_try), url, imp, timeout,
            )
            # Preserve resolver priority. A parallel race can let a lower
            # priority global proxy win before the configured route/file proxy.
            for proxy_value in proxies_to_try:
                ok, proxy, response, exc, status, is_challenge = await _try_one(proxy_value, imp)
                if ok:
                    self.last_used_proxy = proxy
                    self.last_used_direct = proxy is None
                    logger.info("curl_cffi success via %s for %s (imp=%s)", proxy or "direct", url, imp)
                    return response
                if is_challenge:
                    challenge_detected = True
                    if challenge_proxy is None and proxy_value is not None:
                        challenge_proxy = proxy_value
                if isinstance(status, int):
                    last_status = status
                if exc:
                    last_error = exc

        # A VixSrc 403 can be a bare block page without Cloudflare's usual
        # challenge markers.  When a route exists, give FlareSolverr a chance
        # anyway; this is especially important for the explicit DIRECT route
        # used when WARP is disabled.
        if challenge_detected or last_status == 403:
            try:
                if last_status == 403:
                    self._invalidate_cached_solver_state(url)
                    final_headers = self._fresh_headers(**(headers or {}))
                solver_proxy = forced_proxy or challenge_proxy or preferred_proxy
                if solver_proxy is None:
                    # Prevent a stale proxy from a previous request from being
                    # reused when this request explicitly selected direct.
                    self.session_proxy = None
                logger.info(
                    "VixSrc 403/challenge fallback: starting FlareSolverr for %s via %s",
                    url,
                    solver_proxy or "direct",
                )
                return await self._flaresolverr_response(
                    url,
                    headers=final_headers,
                    forced_proxy=solver_proxy,
                )
            except Exception as solver_exc:
                logger.warning("FlareSolverr challenge solve failed for %s: %s", url, solver_exc)
                raise CloudflareChallengeError(
                    f"Cloudflare challenge solve failed for {url}: {solver_exc}"
                ) from solver_exc

        if last_error:
            raise ExtractorError(f"curl_cffi request failed for {url}: {last_error}")
        if last_status is not None:
            if last_status == 403:
                raise ExtractorError(f"VixSrc access blocked (403): {url}")
            raise ExtractorError(f"curl_cffi HTTP error {last_status} for {url}")
        raise ExtractorError(f"curl_cffi failed for {url}: no usable proxy found")

    @staticmethod
    def _normalize_base_site(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ExtractorError("Invalid VixSrc URL")
        netloc = parsed.netloc
        if any(d in netloc.lower() for d in ("vixcloud.co", "vixsrc.to")):
            if _vixsrc_domain:
                netloc = _vixsrc_domain
        return f"{parsed.scheme}://{netloc}"

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
    def _is_timeout_like(error: BaseException) -> bool:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True
        message = str(error).lower()
        return any(marker in message for marker in ("curl: (28)", "timed out", "timeout"))

    async def _get_session(self, url: str = None, forced_proxy: str | None = None):
        """Ottiene una sessione HTTP persistente."""
        proxy = None
        # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
        forced_proxy = _cfg.effective_forced_proxy(url, forced_proxy)
        if forced_proxy:
            proxy = self._normalize_proxy_url(forced_proxy)
        elif url:
            proxy = await self._preferred_proxy(url)
        else:
            proxy = self._get_random_proxy()
        if proxy:
            proxy = self._normalize_proxy_url(proxy)
        if proxy is None and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise aiohttp.ClientConnectionError(
                "VixSrc: direct fallback disabled; no proxy route available"
            )
        self.last_used_proxy = proxy
        self.last_used_direct = proxy is None

        # No await between lookup and publication: concurrent borrowers cannot
        # overwrite a newly created session while an old one is closing.
        session = self._route_sessions.get(proxy)
        if session is None or session.closed:
            session = self._build_session_for_proxy(proxy)
            self._route_sessions[proxy] = session
        self.session_proxy = proxy
        self.session = session
        return session

    async def _make_robust_request(
        self, url: str, headers: dict = None, retries: int = 2, initial_delay: int = 2, forced_proxy: str | None = None
    ):
        """Effettua richieste HTTP robuste con retry automatico e proxy rotation."""
        self._load_cached_solver_state(url)
        final_headers = self._apply_solver_headers(headers or {})
        last_error = None
        request_proxy = None

        for attempt in range(retries):
            try:
                if last_error is not None:
                    # Failed connections are discarded by aiohttp; do not close
                    # a session that another request may still be using.
                    forced_proxy = None  # Don't reuse dead proxy

                session = await self._get_session(url, forced_proxy=forced_proxy)
                request_proxy = self.session_proxy
                logger.info("Attempt %s/%s for URL: %s", attempt + 1, retries, url)

                async with session.get(url, headers=final_headers, timeout=aiohttp.ClientTimeout(total=15, connect=10)) as response:
                    content = await response.text()
                    status = response.status

                    if self._is_cloudflare_challenge(content, status):
                        logger.info(
                            "Cloudflare challenge screen or status %s detected for %s. "
                            "Starting FlareSolverr on-demand...",
                            status,
                            url,
                        )
                        try:
                            return await self._flaresolverr_response(
                                url,
                                headers=final_headers or self._default_headers(),
                                forced_proxy=forced_proxy or request_proxy,
                            )
                        except Exception as solver_exc:
                            logger.warning("FlareSolverr failed for %s: %s", url, solver_exc)
                            raise CloudflareChallengeError(
                                f"Cloudflare challenge solve failed for {url}: {solver_exc}"
                            ) from solver_exc

                    response.raise_for_status()

                    class MockResponse:
                        def __init__(self, text_content, status_val, headers_dict, response_url):
                            self._text = text_content
                            self.status = status_val
                            self.headers = headers_dict
                            self.url = response_url
                            self.status_code = status_val
                            self.text = text_content

                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status,
                                )

                    logger.info("Request successful for %s at attempt %s", url, attempt + 1)
                    return MockResponse(content, response.status, response.headers, response.url)

            except CloudflareChallengeError:
                raise

            except ALL_PROXY_ERRORS + (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError,
            ) as e:
                is_proxy_err = isinstance(e, ALL_PROXY_ERRORS)
                is_timeout = isinstance(e, asyncio.TimeoutError)
                err_type = "Proxy" if is_proxy_err else ("Timeout" if is_timeout else "Connection")
                
                logger.warning(
                    "%s error attempt %s for %s: %s", err_type, attempt + 1, url, str(e)
                )

                if request_proxy:
                    mark_proxy_dead(request_proxy)

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

                if e.status == 403:
                    self._invalidate_cached_solver_state(url)
                    raise ExtractorError(f"VixSrc access blocked (403): {url}") from e

                if attempt == retries - 1:
                    raise ExtractorError(f"Final HTTP error {e.status} for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

            except Exception as e:
                logger.error("Non-network error attempt %s for %s: %s", attempt + 1, url, str(e))
                if attempt == retries - 1:
                    raise ExtractorError(f"Final error for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)



    @staticmethod
    def _is_access_blocked_page(html: str) -> bool:
        low_html = (html or "").lower()
        return any(
            marker in low_html
            for marker in (
                "you are blocked",
                "you have been blocked",
                "error 1020",
                "access denied",
                "request blocked",
            )
        )

    @staticmethod
    def _is_expired_embed_response(html: str) -> bool:
        low_html = (html or "").lower()
        return "410 gone" in low_html or "an error occurred: gone" in low_html

    def _is_cloudflare_challenge(self, html: str, status: int) -> bool:
        """Distinguish a solvable challenge from a terminal block page."""
        if self._is_access_blocked_page(html):
            return False
        low_html = (html or "").lower()
        challenge_markers = (
            "just a moment",
            "checking your browser",
            "verify you are human",
            "performing security verification",
            "challenge-platform",
            "cf-chl-",
            "turnstile",
        )
        if any(marker in low_html for marker in challenge_markers):
            return True
        return "cloudflare" in low_html and ("ray id" in low_html or "challenge" in low_html)

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
        except CloudflareChallengeError:
            raise
        except Exception as curl_err:
            # 404 means content not found — FS won't help, skip cascading fallbacks
            if "404" in str(curl_err):
                raise ExtractorError(f"VixSrc API endpoint not found (404): {api_url}")
            if self._is_timeout_like(curl_err):
                raise ExtractorError(f"VixSrc API request timed out via the selected route: {api_url}") from curl_err
            logger.warning("curl_cffi failed for API, trying robust: %s", curl_err)
            try:
                response = await self._make_robust_request(api_url, headers=api_headers, forced_proxy=None)
            except Exception as robust_err:
                if "404" in str(robust_err):
                    raise ExtractorError(f"VixSrc content not found (404): {api_url}")
                raise ExtractorError(f"VixSrc API fetch failed: {robust_err}") from robust_err

        try:
            logger.debug("VixSrc API raw response (first 500): %s", response.text[:500])
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

    async def _resolve_streamingcommunity_embed_url(self, url: str, forced_proxy: str | None = None) -> str:
        """Resolve a StreamingCommunity watch page to its VixCloud embed URL."""
        page_response = await self._make_robust_request(
            url,
            headers=self._fresh_headers(referer=self._normalize_base_site(url) + "/"),
            forced_proxy=forced_proxy,
        )
        page_html = html.unescape(page_response.text).replace("\\/", "/")
        embed_page_match = re.search(r'"embedUrl"\s*:\s*"([^"]+)"', page_html)
        if not embed_page_match:
            raise ExtractorError("StreamingCommunity embed page not found")

        embed_page_url = urljoin(url, embed_page_match.group(1))
        iframe_response = await self._make_robust_request(
            embed_page_url,
            headers=self._fresh_headers(referer=url),
            forced_proxy=forced_proxy,
        )
        iframe_html = html.unescape(iframe_response.text)
        iframe_match = re.search(
            r"<iframe[^>]+src\s*=\s*[\"']([^\"']+)",
            iframe_html,
            re.IGNORECASE,
        )
        if not iframe_match:
            raise ExtractorError("StreamingCommunity VixCloud iframe not found")

        return self._replace_vixsrc_domain(urljoin(embed_page_url, iframe_match.group(1)))

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
                if re.search(r"window\.canPlayFHD\s*=\s*true\b", script_content, re.IGNORECASE):
                    query_params.append(("h", "1"))
                query_params.append(("lang", "it"))
                if asn_match and asn_match.group(1):
                    query_params.append(("asn", asn_match.group(1)))
                res_url = urlunparse(parsed_playlist_url._replace(query=urlencode(query_params)))
                return self._replace_vixsrc_domain(res_url)

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

        if re.search(r"window\.canPlayFHD\s*=\s*true\b", script_content, re.IGNORECASE):
            query_params.append(("h", "1"))

        query_params.append(("lang", "it"))
        asn_match = re.search(r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", script_content)
        if asn_match and asn_match.group(1):
            query_params.append(("asn", asn_match.group(1)))

        res_url = urlunparse(parsed_server_url._replace(query=urlencode(query_params)))
        return self._replace_vixsrc_domain(res_url)

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
        source_url = url
        prev_bypass_warp = self.bypass_warp_active
        raw_proxy = kwargs.get("proxy")
        raw_proxy_norm = str(raw_proxy or "").strip().lower()
        bypass_proxies = (
            raw_proxy_norm in {"off", "false", "0", "direct", "none", "no"}
            or _cfg.BYPASS_PROXIES_CONTEXT.get()
        )
        bypass_warp = bool(
            kwargs.get("bypass_warp")
            or str(kwargs.get("warp", "")).strip().lower() in {"off", "false", "0", "no"}
            or _cfg.BYPASS_WARP_CONTEXT.get()
            or self.bypass_warp_active
        )
        direct_requested = (
            str(kwargs.get("direct", "")).strip().lower() in {"1", "true", "yes", "on"}
            or bool(kwargs.get("force_direct"))
            or raw_proxy_norm == "direct"
            or (bypass_proxies and bypass_warp)
        )
        if direct_requested:
            bypass_proxies = True
            bypass_warp = True
        self.bypass_warp_active = bypass_warp
        proxy_ctx_token = _cfg.BYPASS_PROXIES_CONTEXT.set(True) if bypass_proxies else None
        warp_ctx_token = _cfg.BYPASS_WARP_CONTEXT.set(True) if bypass_warp else None
        try:
            await self._refresh_vixsrc_domain()
            forced_proxy = None if bypass_proxies else (self._normalize_proxy_url(raw_proxy) or None)
            # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
            forced_proxy = _cfg.effective_forced_proxy(url, forced_proxy)
            parsed_url = urlparse(url)
            response = None
            resolved_streamingcommunity = False
            iframe_version = None

            if "/watch/" in parsed_url.path and "streamingcommunity" in parsed_url.netloc.lower():
                url = await self._resolve_streamingcommunity_embed_url(url, forced_proxy=forced_proxy)
                parsed_url = urlparse(url)
                resolved_streamingcommunity = True

            if "/playlist/" in parsed_url.path:
                logger.info("URL is already a VixSrc manifest, no extraction required.")
                if direct_requested:
                    selected_proxy = None
                else:
                    query_proxy = None if bypass_proxies else parse_qs(parsed_url.query).get("proxy", [None])[0]
                    selected_proxy = forced_proxy or (self._normalize_proxy_url(query_proxy) if query_proxy else None)
                    if not selected_proxy:
                        selected_proxy = (None if bypass_proxies else self.last_used_proxy) or await self._preferred_proxy(url)
                    if selected_proxy:
                        selected_proxy = self._normalize_proxy_url(selected_proxy) or None
                logger.debug(f"Extractor Debug: Extractor result selected_proxy: {selected_proxy}")
                stream_headers = self._fresh_headers()
                # Use cookies and UA from the request (e.g. cf_clearance forwarded by redirect)
                req_h = kwargs.get("request_headers") or {}
                if req_h.get("Cookie"):
                    stream_headers["Cookie"] = self._merge_cookie_headers(
                        req_h["Cookie"],
                        self._solver_cookie_header,
                    )
                if req_h.get("User-Agent") and not self._solver_user_agent:
                    stream_headers["User-Agent"] = req_h["User-Agent"]
                stream_headers = self._apply_solver_headers(stream_headers)

                clean_dest = self._replace_vixsrc_domain(url)
                return {
                    "destination_url": clean_dest,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "selected_proxy": selected_proxy,
                    "force_direct": bool(kwargs.get("force_direct")) or direct_requested or (selected_proxy is None and self.last_used_direct),
                    "bypass_warp": self.bypass_warp_active,
                }

            if "/embed/" in parsed_url.path:
                vix_url = url
                try:
                    response = await self._make_curl_request(
                        vix_url,
                        headers=self._fresh_headers(referer=self._normalize_base_site(vix_url) + "/"),
                        forced_proxy=forced_proxy,
                    )
                except CloudflareChallengeError:
                    raise
                except Exception as curl_err:
                    logger.warning("curl_cffi failed for embed %s: %s", vix_url, curl_err)
                    raise ExtractorError(f"VixSrc embed fetch failed: {curl_err}") from curl_err
            elif "iframe" in url:
                site_url = url.split("/iframe")[0]
                version = await self.version(site_url, forced_proxy=None)
                iframe_version = version
                response = await self._make_robust_request(
                    url,
                    headers=self._fresh_headers(
                        **{"x-inertia": "true", "x-inertia-version": version}
                    ),
                    forced_proxy=None,
                )

                iframe_data = await self._parse_html_simple(response.text, "iframe")
                if iframe_data and iframe_data.get("src"):
                    iframe_url = self._replace_vixsrc_domain(iframe_data["src"].replace("&amp;", "&"))
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
                        embed_proxy = forced_proxy or self.last_used_proxy
                        response = await self._make_curl_request(
                            embed_url,
                            headers=self._fresh_headers(referer=url),
                            forced_proxy=embed_proxy,
                        )
                    except CloudflareChallengeError:
                        raise
                    except Exception as curl_err:
                        error_text = str(curl_err).lower()
                        if self._is_timeout_like(curl_err):
                            # Do not send the same short-lived embed URL through
                            # the aiohttp fallback after a WARP timeout. That
                            # only adds latency and commonly turns into 410.
                            raise ExtractorError(
                                f"VixSrc embed request timed out via {embed_proxy or 'direct'}: {embed_url}"
                            ) from curl_err
                        if "410" in error_text or "gone" in error_text:
                            refreshed_embed_url = await self._resolve_embed_url_from_api(
                                url,
                                forced_proxy=forced_proxy,
                            )
                            if refreshed_embed_url and refreshed_embed_url != embed_url:
                                logger.info("VixSrc embed token expired during fetch; retrying with a fresh API URL")
                                embed_url = refreshed_embed_url
                                response = await self._make_curl_request(
                                    embed_url,
                                    headers=self._fresh_headers(referer=url),
                                    forced_proxy=forced_proxy or self.last_used_proxy,
                                )
                            else:
                                raise ExtractorError(
                                    f"VixSrc embed token expired and could not be refreshed: {embed_url}"
                                ) from curl_err
                        else:
                            logger.warning("curl_cffi failed for embed %s, trying robust: %s", embed_url, curl_err)
                            try:
                                response = await self._make_robust_request(
                                    embed_url,
                                    headers=self._fresh_headers(referer=url),
                                    forced_proxy=embed_proxy,
                                )
                            except Exception as robust_err:
                                raise ExtractorError(f"VixSrc embed fetch failed: {robust_err}") from robust_err
                else:
                    try:
                        response = await self._make_curl_request(url, forced_proxy=forced_proxy)
                    except CloudflareChallengeError:
                        raise
                    except Exception as curl_err:
                        logger.warning("curl_cffi failed for %s, trying robust: %s", url, curl_err)
                        try:
                            response = await self._make_robust_request(url, forced_proxy=None)
                        except Exception as robust_err:
                            raise ExtractorError(f"VixSrc URL fetch failed: {robust_err}") from robust_err
            else:
                raise ExtractorError(f"Unsupported VixSrc URL type: {parsed_url.path}")

            if (
                self._is_expired_embed_response(response.text)
                and not kwargs.get("_expired_embed_retried")
                and (
                    resolved_streamingcommunity
                    or "/movie/" in parsed_url.path
                    or "/tv/" in parsed_url.path
                )
            ):
                logger.info("VixSrc expired embed page; resolving original source once more")
                return await self.extract(source_url, **{**kwargs, "_expired_embed_retried": True})

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

            # StreamingCommunity can return an embed token with only a few
            # seconds left. If FlareSolverr solved the challenge after that
            # token expired, refresh the parent iframe once for a new token.
            # This is not a solver retry: a failed challenge remains terminal.
            if not final_url and "/iframe/" in parsed_url.path and self._is_expired_embed_response(response.text):
                refresh_separator = "&" if "?" in url else "?"
                refresh_url = f"{url}{refresh_separator}_ep_refresh={int(time.time())}"
                logger.info("Expired VixSrc embed token detected; refreshing parent iframe once")
                refresh_response = await self._make_robust_request(
                    refresh_url,
                    headers=self._fresh_headers(
                        **({"x-inertia": "true", "x-inertia-version": iframe_version} if iframe_version else {})
                    ),
                    forced_proxy=None,
                )
                refreshed_iframe = await self._parse_html_simple(refresh_response.text, "iframe")
                if refreshed_iframe and refreshed_iframe.get("src"):
                    refreshed_embed_url = self._replace_vixsrc_domain(
                        refreshed_iframe["src"].replace("&amp;", "&")
                    )
                    refreshed_response = await self._make_robust_request(
                        refreshed_embed_url,
                        headers=self._fresh_headers(
                            **({"x-inertia": "true", "x-inertia-version": iframe_version} if iframe_version else {})
                        ),
                        forced_proxy=None,
                    )
                    final_url = await _extract_from_html(refreshed_response.text)

            if not final_url:
                if self._is_expired_embed_response(response.text):
                    raise ExtractorError(
                        "VixSrc embed token expired (410 Gone); retry the original source URL"
                    )
                raise ExtractorError("No playlist data found in response")

            clean_destination = self._replace_vixsrc_domain(final_url)
            clean_referer = self._replace_vixsrc_domain(url)

            stream_headers = self._fresh_headers(Referer=clean_referer)

            logger.info("VixSrc URL extracted successfully: %s", clean_destination)
            return {
                "destination_url": clean_destination,
                "request_headers": stream_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
                "selected_proxy": None if direct_requested else self.last_used_proxy,
                "force_direct": direct_requested or (self.last_used_proxy is None and self.last_used_direct),
                "bypass_warp": self.bypass_warp_active,
            }

        except Exception as e:
            logger.error("VixSrc extraction failed: %s", str(e))
            raise ExtractorError(f"VixSrc extraction completely failed: {str(e)}")
        finally:
            self.bypass_warp_active = prev_bypass_warp
            if proxy_ctx_token is not None:
                _cfg.BYPASS_PROXIES_CONTEXT.reset(proxy_ctx_token)
            if warp_ctx_token is not None:
                _cfg.BYPASS_WARP_CONTEXT.reset(warp_ctx_token)

    async def close(self):
        """Chiude definitivamente la sessione."""
        sessions = set(self._route_sessions.values())
        if self.session is not None:
            sessions.add(self.session)
        for session in sessions:
            if not session.closed:
                await session.close()
        self._route_sessions.clear()
        self.session = None
        self.session_proxy = None
        await shutdown_flare_solver()
