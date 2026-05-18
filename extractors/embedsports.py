import asyncio
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from yarl import URL

from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_connector_for_proxy, get_proxy_for_url
from extractors.shared_browser import close_shared_browser, get_shared_browser_context

logger = logging.getLogger(__name__)

EMBEDSPORTS_ORIGIN = "https://embedsports.top"


class ExtractorError(Exception):
    """Custom exception for extraction errors."""
    pass


class EmbedSportsExtractor:
    """Extractor for embedsports.top JWPlayer/Clappr embeds used by streamed.pk."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.proxies = proxies or []
        self.bypass_warp_active = bypass_warp
        self.base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        }
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.session: ClientSession | None = None
        self._session_proxy: str | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._browser_launch_lock = asyncio.Lock()
        self._capture_locks: dict[str, asyncio.Lock] = {}
        self._manifest_cache: dict[str, tuple[str, str, float, list[dict], dict[str, str]]] = {}
        self._manifest_cache_ttl = 12
        self._captured_cookies: list[dict] = []
        self._live_pages: dict[str, tuple[Any, float]] = {}
        self._last_browser_extract: dict[str, float] = {}
        self._last_activity = time.time()
        self._watchdog_task = asyncio.create_task(self._browser_watchdog())

    def _get_shared_activity_time(self) -> float:
        import os
        activity_file = os.path.join(os.getcwd(), "dlstreams_activity.txt")
        try:
            if os.path.exists(activity_file):
                with open(activity_file, "r") as f:
                    return float(f.read().strip())
        except Exception:
            pass
        return self._last_activity

    def _update_shared_activity(self):
        import os
        now = time.time()
        self._last_activity = now
        activity_file = os.path.join(os.getcwd(), "dlstreams_activity.txt")
        try:
            with open(activity_file, "w") as f:
                f.write(str(now))
        except Exception:
            pass

    async def _browser_watchdog(self):
        while True:
            await asyncio.sleep(10)
            if self._context:
                now = time.time()
                for cache_key, (page, last_used) in list(self._live_pages.items()):
                    if now - last_used <= 10:
                        continue
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
                    self._live_pages.pop(cache_key, None)
                    logger.info("💤 EmbedSports: chiusa pagina live inattiva per %s", cache_key)

                last_activity = self._get_shared_activity_time()
                if time.time() - last_activity > 30:
                    logger.info("💤 EmbedSports: nessuna attività per 30 secondi. Chiusura browser condiviso...")
                    await close_shared_browser()
                    self._context = None
                    self._browser = None
                    self._playwright = None

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _cache_key(url: str) -> str:
        return url.rstrip("/")

    def _get_header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.request_headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def _get_cookie_header_for_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        cookie_parts: list[str] = []

        if self.session and not self.session.closed and self.session.cookie_jar:
            cookies = self.session.cookie_jar.filter_cookies(f"{parsed.scheme}://{parsed.netloc}/")
            cookie_parts.extend(f"{key}={morsel.value}" for key, morsel in cookies.items())

        for cookie in self._captured_cookies:
            domain = (cookie.get("domain") or "").lstrip(".")
            if parsed.netloc.endswith(domain) or domain.endswith(parsed.netloc):
                cookie_parts.append(f"{cookie['name']}={cookie['value']}")

        return "; ".join(dict.fromkeys(cookie_parts)) or None

    def _build_playback_headers(self, stream_url: str) -> dict[str, str]:
        headers = {
            "Referer": f"{EMBEDSPORTS_ORIGIN}/",
            "Origin": EMBEDSPORTS_ORIGIN,
            "User-Agent": self.base_headers["User-Agent"],
            "Accept": "*/*",
            "X-Direct-Connection": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
        cookie_header = self._get_cookie_header_for_url(stream_url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    async def _nudge_playback(self, page) -> None:
        try:
            await page.evaluate(
                """() => {
                    for (const video of document.querySelectorAll('video')) {
                        video.muted = true;
                        video.play().catch(() => {});
                    }
                    try {
                        if (window.jwplayer) {
                            const jw = window.jwplayer();
                            if (jw && jw.play) jw.play(true);
                        }
                    } catch (_) {}
                    try {
                        if (window.player && window.player.play) {
                            window.player.play();
                        }
                    } catch (_) {}
                }"""
            )
        except Exception:
            pass

    async def _get_session(self) -> ClientSession:
        proxy_url = get_proxy_for_url(
            EMBEDSPORTS_ORIGIN,
            TRANSPORT_ROUTES,
            self.proxies or GLOBAL_PROXIES,
            bypass_warp=self.bypass_warp_active,
        )

        if self.session and not self.session.closed and self._session_proxy == proxy_url:
            return self.session

        if self.session and not self.session.closed:
            await self.session.close()

        connector = (
            get_connector_for_proxy(proxy_url)
            if proxy_url
            else TCPConnector(limit=0, limit_per_host=0, family=socket.AF_INET)
        )
        self.session = ClientSession(
            timeout=ClientTimeout(total=30, connect=10),
            connector=connector,
            headers=self.base_headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._session_proxy = proxy_url
        return self.session

    async def _launch_browser(self):
        async with self._browser_launch_lock:
            self._playwright, self._browser, self._context = await get_shared_browser_context(
                self.base_headers["User-Agent"]
            )
            self._update_shared_activity()
            return self._playwright, self._browser, self._context

    async def _http_refresh_manifest(self, cache_key: str, stream_url: str, extra_headers: dict | None = None) -> tuple[str, str] | None:
        """Try to refresh manifest via HTTP with cached cookies. Returns None if it fails."""
        try:
            session = await self._get_session()
            headers = self._build_playback_headers(stream_url)
            if extra_headers:
                headers.update(extra_headers)
            async with session.get(
                stream_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if text.lstrip().startswith("#EXTM3U"):
                        return text, str(resp.url)
        except Exception as exc:
            logger.debug("EmbedSports HTTP refresh failed for %s: %s", stream_url, exc)
        return None

    async def _capture_manifest(self, embed_url: str, force_refresh: bool = False, request_headers: dict | None = None) -> tuple[str, str]:
        cache_key = self._cache_key(embed_url)
        lock = self._capture_locks.setdefault(cache_key, asyncio.Lock())

        async with lock:
            cached = self._manifest_cache.get(cache_key)
            live_entry = self._live_pages.get(cache_key)
            live_page = live_entry[0] if live_entry and not live_entry[0].is_closed() else None

            # Try HTTP refresh first (fast path, no browser)
            if force_refresh and cached:
                if live_page:
                    self._live_pages[cache_key] = (live_page, time.time())
                http_result = await self._http_refresh_manifest(cache_key, cached[1], request_headers)
                if http_result:
                    manifest_text, manifest_url = http_result
                    self._manifest_cache[cache_key] = (
                        manifest_text, manifest_url, time.time(),
                        self._captured_cookies, {manifest_url: manifest_text},
                    )
                    return manifest_text, manifest_url

            # Reuse live page if available
            if live_page and cached:
                self._live_pages[cache_key] = (live_page, time.time())
                if not force_refresh and time.time() - cached[2] < self._manifest_cache_ttl:
                    await self._nudge_playback(live_page)
                    return cached[0], cached[1]
                # force_refresh or cache expired → reload live page
                try:
                    await live_page.reload(wait_until="domcontentloaded", timeout=30000)
                    await live_page.wait_for_timeout(1500)
                    try:
                        await live_page.locator(
                            "button, .jw-display-icon-container, .jwplayer, video"
                        ).first.click(timeout=2500)
                    except Exception:
                        pass
                    deadline = time.time() + 8
                    last_seen = cached[2]
                    while time.time() < deadline:
                        refreshed = self._manifest_cache.get(cache_key)
                        if refreshed and refreshed[2] > last_seen:
                            self._live_pages[cache_key] = (live_page, time.time())
                            return refreshed[0], refreshed[1]
                        await live_page.wait_for_timeout(500)
                except Exception as exc:
                    logger.debug("EmbedSports live page reload failed for %s: %s", embed_url, exc)
                    self._live_pages.pop(cache_key, None)

            # Return fresh or stale cache for non-force path
            if not force_refresh and cached:
                if time.time() - cached[2] < self._manifest_cache_ttl:
                    manifest_text, stream_url, _, cookies, _ = cached
                    self._captured_cookies = cookies
                    return manifest_text, stream_url
                logger.debug("EmbedSports cache expired, reusing stale for %s", embed_url)
                self._manifest_cache[cache_key] = (
                    cached[0], cached[1], time.time(), cached[3] or self._captured_cookies, cached[4]
                )
                self._captured_cookies = cached[3] or self._captured_cookies
                return cached[0], cached[1]

            _, _, context = await self._launch_browser()
            page = await context.new_page()

            async def handle_popup(popup):
                try:
                    await popup.close()
                except Exception:
                    pass

            page.on("popup", handle_popup)

            manifest_text: str | None = None
            manifest_url: str | None = None
            captured_manifests: dict[str, str] = {}

            async def on_response(response):
                nonlocal manifest_text, manifest_url
                try:
                    response_url = str(response.url)
                    content_type = (response.headers.get("content-type") or "").lower()
                    path = urlparse(response_url).path.lower()
                    is_manifest = (
                        path.endswith(".m3u8")
                        or "application/vnd.apple.mpegurl" in content_type
                        or "application/x-mpegurl" in content_type
                    )
                    if not is_manifest or response.status != 200:
                        return

                    body = await response.body()
                    decoded = body.decode("utf-8", errors="ignore")
                    if decoded.lstrip().startswith("#EXTM3U"):
                        captured_manifests[response_url] = decoded
                        if not manifest_text:
                            manifest_text = decoded
                            manifest_url = response_url
                        if manifest_text and manifest_url:
                            self._manifest_cache[cache_key] = (
                                manifest_text,
                                manifest_url,
                                time.time(),
                                self._captured_cookies,
                                captured_manifests.copy(),
                            )
                        logger.debug("EmbedSports captured manifest from %s", response_url)
                except Exception as exc:
                    logger.debug("EmbedSports response hook failed for %s: %s", response.url, exc)

            page.on("response", on_response)

            try:
                await page.goto(
                    embed_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                    referer=self._get_header("Referer", f"{EMBEDSPORTS_ORIGIN}/"),
                )
                await page.wait_for_timeout(1500)

                try:
                    await page.locator("button, .jw-display-icon-container, .jwplayer, video").first.click(
                        timeout=2500
                    )
                except Exception:
                    pass
                await self._nudge_playback(page)

                deadline = time.time() + 35
                while time.time() < deadline:
                    if manifest_text and len(captured_manifests) > 1:
                        break
                    await page.wait_for_timeout(500)

                if not manifest_text or not manifest_url:
                    raise ExtractorError("EmbedSports: no m3u8 response captured")

                self._captured_cookies = await context.cookies()
                session = await self._get_session()
                yarl_url = URL(embed_url)
                for cookie in self._captured_cookies:
                    session.cookie_jar.update_cookies(
                        {cookie["name"]: cookie["value"]},
                        response_url=yarl_url,
                    )

                self._manifest_cache[cache_key] = (
                    manifest_text,
                    manifest_url,
                    time.time(),
                    self._captured_cookies,
                    captured_manifests,
                )
                if len(self._manifest_cache) > 10:
                    oldest = min(self._manifest_cache.keys(), key=lambda k: self._manifest_cache[k][2])
                    self._manifest_cache.pop(oldest, None)
                self._live_pages[cache_key] = (page, time.time())
                self._update_shared_activity()
                return manifest_text, manifest_url
            finally:
                if not manifest_text:
                    await page.close()

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        try:
            if "embedsports.top/embed/" not in url.lower():
                raise ExtractorError("EmbedSports: invalid embed URL")

            manifest_text, manifest_url = await self._capture_manifest(
                url,
                force_refresh=kwargs.get("force_refresh", False),
                request_headers=kwargs.get("request_headers"),
            )
            self._update_shared_activity()

            cache_key = self._cache_key(url)
            cached = self._manifest_cache.get(cache_key, (None, None, None, None, {}))
            all_manifests = cached[4] if cached else {}

            return {
                "destination_url": manifest_url,
                "request_headers": self._build_playback_headers(manifest_url),
                "mediaflow_endpoint": self.mediaflow_endpoint,
                "captured_manifest": manifest_text,
                "captured_manifests": all_manifests,
                "bypass_warp": self.bypass_warp_active,
            }
        except PlaywrightTimeoutError as exc:
            raise ExtractorError(f"EmbedSports: browser timeout: {exc}") from exc
        except ExtractorError:
            raise
        except Exception as exc:
            logger.exception("EmbedSports extraction failed for %s", url)
            raise ExtractorError(f"EmbedSports extraction failed: {exc}") from exc

    async def fetch_manifest_via_browser(self, embed_url: str, manifest_url: str) -> tuple[str, str] | None:
        _, _, context = await self._launch_browser()
        page = await context.new_page()
        try:
            try:
                await page.goto(
                    embed_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                    referer=self._get_header("Referer", f"{EMBEDSPORTS_ORIGIN}/"),
                )
            except Exception:
                pass
            text = await page.evaluate(
                """async (manifestUrl) => {
                    const response = await fetch(manifestUrl, {
                        method: 'GET',
                        headers: {
                            'Accept': '*/*',
                            'Origin': 'https://embedsports.top',
                            'Referer': 'https://embedsports.top/'
                        },
                        cache: 'no-store',
                        credentials: 'include'
                    });
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    return await response.text();
                }""",
                manifest_url,
            )
            if isinstance(text, str) and text.lstrip().startswith("#EXTM3U"):
                cache_key = self._cache_key(embed_url)
                cached = self._manifest_cache.get(cache_key)
                if cached:
                    m_text, stream_url, _, cookies, captured_manifests = cached
                    captured_manifests[manifest_url] = text
                    self._manifest_cache[cache_key] = (
                        m_text,
                        stream_url,
                        time.time(),
                        cookies,
                        captured_manifests,
                    )
                return text, manifest_url
        except Exception as exc:
            logger.debug("EmbedSports browser fetch failed for %s: %s", manifest_url, exc)
        finally:
            if not page.is_closed():
                await page.close()
        return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
