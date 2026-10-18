import asyncio
import base64
import logging
import re
import json
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urljoin
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.client_exceptions import ClientOSError
from config import get_connector_for_proxy, get_preferred_proxy_for_url
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError


logger = logging.getLogger(__name__)


class RateLimitError(ExtractorError):
    """Upstream returned HTTP 429; use cached data/backoff when available."""
    pass


def unpack(p, a, c, k, e=None, d=None):
    """
    Unpacker for P.A.C.K.E.R. packed javascript.
    This is a Python port of the common Javascript unpacker.
    """
    while c > 0:
        c -= 1
        if k[c]:
            p = re.sub("\\b" + _int2base(c, a) + "\\b", k[c], p)
    return p


def _int2base(x, base):
    if x < 0:
        sign = -1
    elif x == 0:
        return "0"
    else:
        sign = 1

    x *= sign
    digits = []

    while x:
        digits.append("0123456789abcdefghijklmnopqrstuvwxyz"[x % base])
        x = int(x / base)

    if sign < 0:
        digits.append("-")

    digits.reverse()
    return "".join(digits)


class SportsonlineExtractor(BaseExtractor):
    """Sportsonline/Sportzonline URL extractor for M3U8 streams."""

    EXTRACT_BUDGET_SECONDS = 20.0
    CANDIDATE_TIMEOUT_SECONDS = 6.0
    STREAM_CACHE_SECONDS = 30.0
    STREAM_CACHE_STALE_SECONDS = 900.0
    STREAM_CACHE_FAIL_BACKOFF_SECONDS = 15.0
    MAX_HOST_BACKOFF_SECONDS = 3600.0

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="sportsonline", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        }
        self.session = None
        self._route_sessions = {}
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self._session_proxy = None
        self._inflight_extract_tasks: dict = {}
        self._stream_cache: dict = {}
        self._host_backoff: dict[str, float] = {}

    @staticmethod
    def _host_of(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    @classmethod
    def _parse_retry_after(cls, value: str | None) -> float | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            seconds = float(raw)
        else:
            try:
                target = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        return max(1.0, min(float(seconds), cls.MAX_HOST_BACKOFF_SECONDS))

    def _mark_host_limited(self, url: str, retry_after: str | None) -> float:
        seconds = self._parse_retry_after(retry_after)
        if seconds is None:
            seconds = self.STREAM_CACHE_FAIL_BACKOFF_SECONDS
        host = self._host_of(url)
        self._host_backoff[host] = max(
            self._host_backoff.get(host, 0.0), time.monotonic() + seconds
        )
        logger.warning("Sportsonline: %s rate-limited, backing off %.0fs", host, seconds)
        return seconds

    def _host_limited_for(self, url: str) -> float:
        deadline = self._host_backoff.get(self._host_of(url), 0.0)
        return max(0.0, deadline - time.monotonic())

    def update_request_headers(self, request_headers: dict | None):
        self.request_headers = request_headers or {}

    def _get_request_header(self, name: str, default: str | None = None) -> str | None:
        for header_name, header_value in self.request_headers.items():
            if header_name.lower() == name.lower():
                return header_value
        return default

    def _get_origin(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _copy_request_headers(self, header_map: dict[str, str]) -> dict[str, str]:
        copied_headers = {}
        for request_name, output_name in header_map.items():
            value = self._get_request_header(request_name)
            if value:
                copied_headers[output_name] = value
        return copied_headers

    def _build_page_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_request_header(
                "User-Agent", self.base_headers["User-Agent"]
            ),
            "Accept": self._get_request_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            ),
            "Accept-Language": self._get_request_header(
                "Accept-Language", "en-US,en;q=0.9,it;q=0.8"
            ),
            "Cache-Control": self._get_request_header("Cache-Control", "max-age=0"),
            "Upgrade-Insecure-Requests": self._get_request_header(
                "Upgrade-Insecure-Requests", "1"
            ),
            "Sec-Fetch-Site": self._get_request_header("Sec-Fetch-Site", "none"),
            "Sec-Fetch-Mode": self._get_request_header(
                "Sec-Fetch-Mode", "navigate"
            ),
            "Sec-Fetch-User": self._get_request_header("Sec-Fetch-User", "?1"),
            "Sec-Fetch-Dest": self._get_request_header("Sec-Fetch-Dest", "document"),
        }
        headers.update(
            self._copy_request_headers(
                {
                    "sec-ch-ua": "Sec-CH-UA",
                    "sec-ch-ua-mobile": "Sec-CH-UA-Mobile",
                    "sec-ch-ua-platform": "Sec-CH-UA-Platform",
                    "Cookie": "Cookie",
                    "Pragma": "Pragma",
                }
            )
        )
        return headers

    def _build_iframe_headers(self, page_url: str, iframe_url: str) -> dict[str, str]:
        page_headers = self._build_page_headers()
        page_headers["Referer"] = page_url
        page_headers["Origin"] = self._get_origin(page_url)
        page_headers["Sec-Fetch-Site"] = (
            "same-origin"
            if urlparse(page_url).netloc == urlparse(iframe_url).netloc
            else "cross-site"
        )
        page_headers["Sec-Fetch-Dest"] = "iframe"
        page_headers.pop("Sec-Fetch-User", None)
        return page_headers

    async def _get_session(self, url: str = None, force_direct: bool = False):
        force_direct = force_direct or self._force_direct
        if force_direct:
            if not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
                raise aiohttp.ClientConnectionError(
                    "Sportsonline: implicit direct fallback disabled"
                )
            proxy = None
            self.last_used_proxy = None
        else:
            forced = _cfg.effective_forced_proxy(url, self._forced_proxy)
            if forced:
                proxy = forced
                self.last_used_proxy = proxy
            else:
                proxy = await get_preferred_proxy_for_url(url, "sportsonline", self.proxies, self.bypass_warp_active)
                self.last_used_proxy = proxy

        if proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise aiohttp.ClientConnectionError(
                "Sportsonline: direct fallback disabled; no proxy route available"
            )

        self.session = self._route_sessions.get(proxy)
        self._session_proxy = proxy
        if (
            self.session is None
            or self.session.closed
            or self._session_proxy != proxy
        ):
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)

            if proxy:
                logger.debug(f"Using proxy {proxy} for Sportsonline session.")
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0)

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": self.base_headers["User-Agent"]},
                cookie_jar=aiohttp.CookieJar(),
            )
            self._session_proxy = proxy
            self._route_sessions[proxy] = self.session
        return self.session

    async def _make_robust_request(
        self,
        url: str,
        headers: dict = None,
        retries=2,
        initial_delay=1,
        timeout=15,
        deadline: float | None = None,
    ):
        """Effettua richieste HTTP robuste con aiohttp e proxy configurati."""
        final_headers = headers or self.base_headers

        for attempt in range(retries):
            try:
                logger.debug(f"Attempt {attempt + 1}/{retries} for URL: {url}")
                session = await self._get_session(url)
                request_timeout = float(timeout)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ExtractorError(
                            f"Sportsonline extraction budget exhausted for {url}"
                        )
                    request_timeout = min(request_timeout, remaining)
                async with session.get(
                    url, headers=final_headers, timeout=request_timeout
                ) as response:
                    if response.status == 429:
                        self._mark_host_limited(url, response.headers.get("Retry-After"))
                        raise RateLimitError(f"HTTP 429 Too Many Requests for {url}")
                    response.raise_for_status()
                    html = await self._handle_response_content(response)
                    if not html:
                        raise ExtractorError(f"Empty response for {url}")
                    return html, str(response.url)

            except RateLimitError:
                raise
            except (ssl.SSLError, ClientOSError) as e:
                logger.warning(
                    "SSL/OS error attempt %s/%s for %s via %s: %s: %r",
                    attempt + 1,
                    retries,
                    url,
                    self._session_proxy or "direct",
                    type(e).__name__,
                    e,
                )
                if self._session_proxy:
                    logger.info(f"SSL/OS error with proxy {self._session_proxy}, retrying without direct fallback...")
                    # Keep the shared session alive for concurrent requests.
                    # aiohttp removes the failed connection from its pool.
                if attempt < retries - 1:
                    delay = initial_delay
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All request attempts failed for {url}: {str(e)}")

            except Exception as e:
                logger.warning(
                    "Request attempt %s/%s failed for %s via %s: %s: %r",
                    attempt + 1,
                    retries,
                    url,
                    self._session_proxy or "direct",
                    type(e).__name__,
                    e,
                )
                if attempt < retries - 1:
                    delay = initial_delay
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All request attempts failed for {url}: {str(e)}")
        raise ExtractorError(f"Unable to complete request for {url}")
    async def _handle_response_content(self, response: aiohttp.ClientResponse) -> str:
        """Read response body; aiohttp already handles standard decompression."""
        raw_body = await response.read()
        return raw_body.decode(response.charset or "utf-8", errors="replace")

    def _detect_packed_blocks(self, html: str) -> list[str]:
        raw_matches: list[str] = []
        strict_eval_pattern = re.compile(r"eval\(function\(p,a,c,k,e,.*?\}\(.*?\)\)", re.DOTALL)
        relaxed_eval_pattern = re.compile(r"eval\(function\(p,a,c,k,e,[dr]\).*?\}\(.*?\)\)", re.DOTALL)

        script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
        for script_body in script_pattern.findall(html):
            if "eval(function(p,a,c,k,e" in script_body:
                strict_matches = strict_eval_pattern.findall(script_body)
                if strict_matches:
                    raw_matches.extend(strict_matches)
                    continue

                relaxed_matches = relaxed_eval_pattern.findall(script_body)
                if relaxed_matches:
                    raw_matches.extend(relaxed_matches)

        if raw_matches:
            return raw_matches

        raw_matches = strict_eval_pattern.findall(html)
        if not raw_matches:
            raw_matches = relaxed_eval_pattern.findall(html)

        return raw_matches

    @staticmethod
    def _extract_m3u8_candidate(text: str) -> str | None:
        patterns = [
            r"var\s+src\s*=\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"src\s*=\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"file\s*:\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"[\"']([^\"']*https?://[^\"']+\.m3u8[^\"']*)[\"']",
            r"(https?://[^\s\"'>]+\.m3u8[^\s\"'>]*)",
            r"(//[^\s\"'>]+\.m3u8[^\s\"'>]*)",
            r"(/[^\s\"'>]+\.m3u8[^\s\"'>]*)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)

        return None

    @staticmethod
    def _extract_econfig_m3u8(html: str) -> str | None:
        """Decode current dynmill player config and return its stream URL."""
        config_match = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]+)['\"]", html)
        if not config_match:
            return None

        try:
            encoded_config = config_match.group(1)
            decoded_config = base64.b64decode(
                encoded_config + "=" * (-len(encoded_config) % 4)
            ).decode("latin1")

            part_order = [2, 0, 3, 1]
            part_length = -(-len(decoded_config) // 4)
            encoded_parts = []
            offset = 0

            for _ in range(4):
                part = decoded_config[offset : offset + part_length]
                offset += part_length
                encoded_parts.append(part[:3] + part[4:])

            decoded_parts = [""] * 4
            for index, part in enumerate(encoded_parts):
                decoded_parts[part_order[index]] = base64.b64decode(
                    part + "=" * (-len(part) % 4)
                ).decode("latin1")

            joined_config = "".join(decoded_parts)
            config_json = base64.b64decode(
                joined_config + "=" * (-len(joined_config) % 4)
            ).decode("utf-8")
            config = json.loads(config_json)
        except Exception as e:
            logger.debug(f"Failed to decode Sportsonline _econfig: {e}")
            return None

        return config.get("stream_url_nop2p") or config.get("stream_url")

    @staticmethod
    def _normalize_stream_url(stream_url: str, base_url: str) -> str:
        cleaned = stream_url.strip().strip("\"'").replace("\\/", "/")
        if cleaned.startswith("//"):
            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme or 'https'}:{cleaned}"
        if not urlparse(cleaned).scheme:
            return urljoin(base_url, cleaned)
        return cleaned

    async def _extract_impl(self, url: str, **kwargs) -> Dict[str, Any]:
        """Main extraction flow: fetch page, extract iframe, unpack and find m3u8."""
        try:
            self._apply_routing_kwargs(url, kwargs)
            deadline = time.monotonic() + self.EXTRACT_BUDGET_SECONDS
            self.update_request_headers(kwargs.get("request_headers"))
            
            parsed_source = urlparse(url)
            source_origin = f"{parsed_source.scheme}://{parsed_source.netloc}"
            source_referer = self._get_request_header("Referer") or f"{source_origin}/"
            user_agent = self._get_request_header("User-Agent", self.base_headers["User-Agent"])

            # Step 1: Fetch main page
            logger.debug(f"Fetching main page: {url}")
            main_headers = self._build_page_headers()
            if source_referer:
                main_headers["Referer"] = source_referer
            if source_origin:
                main_headers["Origin"] = source_origin

            main_html, main_url = await self._make_robust_request(
                url,
                headers=main_headers,
                timeout=15,
                deadline=deadline,
            )

            # Extract first iframe (src can appear in any attribute order)
            iframe_match = re.search(r'<iframe[^>]+(?<!data-)src=["\']([^"\']+)["\']', main_html, re.IGNORECASE)
            iframe_url = main_url
            iframe_html = main_html

            if iframe_match:
                iframe_url = self._normalize_stream_url(iframe_match.group(1), main_url)
                logger.debug(f"Found iframe URL: {iframe_url}")

                candidates = [iframe_url]
                parsed_iframe = urlparse(iframe_url)
                if parsed_iframe.netloc.lower() == "gotdynamic.net":
                    candidates.extend([
                        parsed_iframe._replace(netloc="wgstream.sx").geturl(),
                        parsed_iframe._replace(netloc="www.wgstream.sx").geturl()
                    ])

                iframe_html = None
                for candidate_url in candidates:
                    if deadline <= time.monotonic():
                        logger.warning(
                            "Sportsonline: extraction budget exhausted before iframe candidate"
                        )
                        break
                    if self._host_limited_for(candidate_url) > 0:
                        logger.debug(
                            "Sportsonline: skipping rate-limited iframe host %s",
                            self._host_of(candidate_url),
                        )
                        continue
                    # Step 2: Fetch iframe with source page as referer
                    iframe_headers = self._build_iframe_headers(main_url, candidate_url)
                    try:
                        iframe_html, active_iframe_url = await self._make_robust_request(
                            candidate_url,
                            headers=iframe_headers,
                            timeout=self.CANDIDATE_TIMEOUT_SECONDS,
                            retries=1,
                            deadline=deadline,
                        )
                        iframe_url = active_iframe_url
                        logger.debug(f"Iframe HTML length: {len(iframe_html)}")
                        break
                    except Exception as e:
                        logger.warning(f"Failed candidate {candidate_url}: {e}")

                if not iframe_html:
                    raise ExtractorError(
                        "All iframe candidates failed (blocked, rate-limited, or connection errors)."
                    )
            else:
                logger.warning("No iframe found on page, attempting extraction from main HTML")

            parsed_iframe = urlparse(iframe_url)
            playback_headers = {
                "Referer": iframe_url,
                "Origin": f"{parsed_iframe.scheme}://{parsed_iframe.netloc}",
                "User-Agent": user_agent,
            }

            # Step 3: Detect packed blocks
            packed_blocks = self._detect_packed_blocks(iframe_html)

            logger.debug(f"Found {len(packed_blocks)} packed blocks")

            if not packed_blocks:
                # Current Sportzonline pages commonly use window._econfig
                # instead of P.A.C.K.E.R.; this is a normal fallback path.
                logger.debug(
                    "No packed blocks found; trying inline/econfig M3U8 fallback"
                )
                direct_match = self._extract_m3u8_candidate(iframe_html)
                fallback_source = "inline"
                if not direct_match:
                    direct_match = self._extract_econfig_m3u8(iframe_html)
                    fallback_source = "econfig"
                if direct_match:
                    m3u8_url = self._normalize_stream_url(direct_match, iframe_url)
                    logger.info(
                        "Found M3U8 URL via %s fallback: %s",
                        fallback_source,
                        m3u8_url,
                    )

                    return self._result(m3u8_url, playback_headers)
                else:
                    raise ExtractorError(
                        "No packed blocks, inline M3U8, or _econfig stream URL found"
                    )

            # Choose block: if >=2 use second (index 1), else first (index 0)
            chosen_idx = 1 if len(packed_blocks) > 1 else 0
            m3u8_url = None
            unpacked_code = None

            logger.debug(f"Chosen packed block index: {chosen_idx}")

            # Try to unpack chosen block
            try:
                unpacked_code = extract_unpack(packed_blocks[chosen_idx])
                logger.debug(f"Successfully unpacked block {chosen_idx}")
            except Exception as e:
                logger.warning(f"Failed to unpack block {chosen_idx}: {e}")

            # Search for var src="...m3u8" with multiple patterns
            if unpacked_code:
                m3u8_url = self._extract_m3u8_candidate(unpacked_code)

            # If not found, try all other blocks
            if not m3u8_url:
                logger.debug("m3u8 not found in chosen block, trying all blocks")
                for i, block in enumerate(packed_blocks):
                    if i == chosen_idx:
                        continue
                    try:
                        unpacked_code = extract_unpack(block)
                        m3u8_url = self._extract_m3u8_candidate(unpacked_code)
                        if m3u8_url:
                            logger.debug(f"Found m3u8 in block {i}")
                            break
                    except Exception as e:
                        logger.debug(f"Failed to process block {i}: {e}")
                        continue

            if not m3u8_url:
                fallback_candidate = self._extract_m3u8_candidate(iframe_html)
                if not fallback_candidate:
                    fallback_candidate = self._extract_econfig_m3u8(iframe_html)
                if fallback_candidate:
                    m3u8_url = fallback_candidate

            if not m3u8_url:
                raise ExtractorError("Could not extract m3u8 URL from packed code")

            m3u8_url = self._normalize_stream_url(m3u8_url, iframe_url)

            logger.info(f"Successfully extracted m3u8 URL: {m3u8_url}")

            # Return stream configuration
            return self._result(m3u8_url, playback_headers)

        except ExtractorError:
            raise
        except Exception as e:
            logger.exception(f"Sportsonline extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extract with short-lived cache and single-flight deduplication."""
        self._apply_routing_kwargs(url, kwargs)
        cache_key = (url.strip(), self._forced_proxy, self._force_direct, self.bypass_warp_active)
        now = time.monotonic()
        cached = self._stream_cache.get(cache_key)
        if cached and cached[0] > now:
            logger.debug("Sportsonline: reusing cached stream URL for %s", cache_key)
            return dict(cached[2])

        try:
            existing_task = self._inflight_extract_tasks.get(cache_key)
            if existing_task and not existing_task.done():
                result = await existing_task
            else:
                task = asyncio.create_task(self._extract_impl(url, **kwargs))
                self._inflight_extract_tasks[cache_key] = task
                try:
                    result = await task
                finally:
                    if self._inflight_extract_tasks.get(cache_key) is task:
                        self._inflight_extract_tasks.pop(cache_key, None)
        except Exception:
            if cached and cached[1] > time.monotonic():
                logger.warning(
                    "Sportsonline: extraction failed for %s, serving cached stream URL",
                    cache_key,
                )
                return dict(cached[2])
            raise

        now = time.monotonic()
        self._stream_cache[cache_key] = (
            now + self.STREAM_CACHE_SECONDS,
            now + self.STREAM_CACHE_STALE_SECONDS,
            dict(result),
        )
        return result

    async def close(self):
        pending_tasks = list(self._inflight_extract_tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._inflight_extract_tasks.clear()
        for session in self._route_sessions.values():
            if not session.closed:
                await session.close()
        self._route_sessions.clear()
        self.session = None


def extract_unpack(packed_js):
    """
    Unpacker for P.A.C.K.E.R. packed javascript.
    """
    try:
        match = re.search(r"}\((.*)\)\)", packed_js)
        if not match:
            raise ValueError("Cannot find packed data.")

        p, a, c, k, e, d = eval(f"({match.group(1)})", {"__builtins__": {}}, {})
        return unpack(p, a, c, k, e, d)
    except Exception as e:
        raise ValueError(f"Failed to unpack JS: {e}")
