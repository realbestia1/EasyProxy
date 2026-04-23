import logging
import re
import time
import asyncio
from urllib.parse import urlparse
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from yarl import URL

logger = logging.getLogger(__name__)

# Easy to change in one place if the entry domain changes again.
DLSTREAMS_ENTRY_ORIGIN = "https://dlhd.dad"

class ExtractorError(Exception):
    """Custom exception for extraction errors."""
    pass

class DLStreamsExtractor:
    """Extractor for dlhd.dad / dlstreams streams."""

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.entry_origin = DLSTREAMS_ENTRY_ORIGIN
        # Runtime-discovered stream origin (learned from browser network responses).
        # We intentionally avoid hardcoding CDN domains because they rotate frequently.
        self.stream_origin = self.entry_origin
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or []
        self._browser_key_cache: dict[str, bytes] = {}
        # We no longer cache the manifest text to ensure live streams are fresh.
        # self._browser_manifest_cache: dict[str, str] = {}
        self._browser_failure_cache: dict[str, float] = {}
        self._browser_channel_locks: dict[str, asyncio.Lock] = {}
        self._last_working_player: dict[str, str] = {}
        self._playwright = None
        self._browser = None
        self._context = None
        self._browser_launch_lock = asyncio.Lock()
        self._last_activity = time.time()
        self._captured_cookies: list[dict] = []
        # Proactive refresh tracking
        self._last_session_refresh: dict[str, float] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._dynamic_refresh_interval: dict[str, float] = {}
        # Manifest micro-cache to handle rapid requests
        self._manifest_cache: dict[str, tuple[str, float]] = {}
        self._watchdog_task = asyncio.create_task(self._browser_watchdog())

    def _get_shared_activity_time(self) -> float:
        """Reads the last activity timestamp from a shared file (multi-worker friendly)."""
        import os
        activity_file = os.path.join(os.getcwd(), "dlstreams_activity.txt")
        try:
            if os.path.exists(activity_file):
                with open(activity_file, "r") as f:
                    return float(f.read().strip())
        except Exception:
            pass
        return self._last_activity # Fallback to local memory

    def _update_shared_activity(self):
        """Updates the last activity timestamp in a shared file."""
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
            if self._browser and self._context:
                last_activity = self._get_shared_activity_time()
                if time.time() - last_activity > 30: # 30 secondi di inattività globale
                    try:
                        # Only the 'owner' or the first one to notice tries to close properly
                        logger.info("💤 Nessuna attività video globale per 30 secondi. Spegnimento browser condiviso...")
                        # We use a try-except because another worker might have already closed it
                        await self._context.close()
                        await self._browser.close()
                        if self._playwright:
                            await self._playwright.stop()
                    except Exception:
                        pass # Likely already closed by another worker
                    finally:
                        self._context = None
                        self._browser = None
                        self._playwright = None

    def _get_browser_lock(self, channel_key: str) -> asyncio.Lock:
        lock = self._browser_channel_locks.get(channel_key)
        if lock is None:
            lock = asyncio.Lock()
            self._browser_channel_locks[channel_key] = lock
        return lock

    def _is_browser_cooldown_active(self, channel_key: str) -> bool:
        retry_after = self._browser_failure_cache.get(channel_key, 0)
        return retry_after > time.time()

    def _mark_browser_failure(self, channel_key: str, cooldown_seconds: int = 60) -> None:
        self._browser_failure_cache[channel_key] = time.time() + cooldown_seconds

    def _clear_browser_failure(self, channel_key: str) -> None:
        self._browser_failure_cache.pop(channel_key, None)

    def _prioritize_player_urls(self, channel_id: str) -> list[str]:
        players = self._build_player_urls(channel_id)
        cached_player = self._last_working_player.get(channel_id)
        if not cached_player:
            return players
        if cached_player not in players:
            self._last_working_player.pop(channel_id, None)
            return players
        return [cached_player, *[p for p in players if p != cached_player]]

    def _clear_channel_cache(self, channel_id: str) -> None:
        self._last_working_player.pop(channel_id, None)
        keys_to_remove = [k for k in self._browser_key_cache if "/key/" in k]
        for key in keys_to_remove:
            self._browser_key_cache.pop(key, None)

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _launch_browser(self):
        async with self._browser_launch_lock:
            if self._browser and self._context:
                try:
                    # Verify the connection is still alive
                    await self._browser.version()
                    return self._playwright, self._browser, self._context
                except Exception:
                    self._browser = None
                    self._context = None

            if not self._playwright:
                self._playwright = await async_playwright().start()

            # --- SHARED BROWSER LOGIC (CDP) ---
            try:
                # Try to connect to an existing browser instance on port 9222
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    "http://localhost:9222",
                    timeout=2000 
                )
                # Use existing context if available, or create new one
                contexts = self._browser.contexts
                self._context = contexts[0] if contexts else await self._browser.new_context()
                logger.info("🔗 [Shared Browser] Connected to existing instance on port 9222")
            except Exception:
                # No browser on 9222, launch a new Master instance
                import os, sys
                chrome_path = os.getenv("CHROME_BIN") or os.getenv("CHROME_EXE_PATH")
                is_headless = sys.platform.startswith("linux")
                executable_path = chrome_path if chrome_path and os.path.exists(chrome_path) else None

                logger.info("🚀 [Shared Browser] Launching new Master instance on port 9222")
                self._browser = await self._playwright.chromium.launch(
                    headless=is_headless,
                    executable_path=executable_path,
                    args=[
                        "--remote-debugging-port=9222",
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                self._context = await self._browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                )

            # Ensure we have a persistent dummy page to keep the context/browser alive
            pages = self._context.pages
            if not pages:
                dummy_page = await self._context.new_page()
                await dummy_page.goto("about:blank")
                logger.debug("⚓ Created Shared Anchor Page (about:blank)")
            
            self._update_shared_activity()
            return self._playwright, self._browser, self._context

    def _get_header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.request_headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def _get_cookie_header_for_url(self, url: str) -> str | None:
        if not self.session or self.session.closed or not self.session.cookie_jar:
            return None

        parsed = urlparse(url)
        cookies = self.session.cookie_jar.filter_cookies(
            f"{parsed.scheme}://{parsed.netloc}/"
        )
        cookie_header = "; ".join(f"{key}={morsel.value}" for key, morsel in cookies.items())
        return cookie_header or None

    @staticmethod
    def _extract_channel_id(url: str) -> str:
        match_id = re.search(r"(?:id=|premium)(\d+)", url)
        channel_id = match_id.group(1) if match_id else str(url)
        if not channel_id.isdigit():
            channel_id = channel_id.replace("premium", "")
        return channel_id

    def _build_player_urls(self, channel_id: str) -> list[str]:
        origin = self.entry_origin.rstrip("/")
        return [
            f"{origin}/stream/stream-{channel_id}.php",
            f"{origin}/cast/stream-{channel_id}.php",
            f"{origin}/watch/stream-{channel_id}.php",
            f"{origin}/plus/stream-{channel_id}.php",
            f"{origin}/casting/stream-{channel_id}.php",
            f"{origin}/player/stream-{channel_id}.php",
        ]

    async def _prime_dlstreams_session(
        self,
        session: ClientSession,
        player_url: str,
    ) -> None:
        warmup_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": self._get_header("Accept-Language", "en-US,en;q=0.9"),
        }
        source_referer = self._get_header("Referer")
        if source_referer:
            warmup_headers["Referer"] = source_referer

        try:
            async with session.get(player_url, headers=warmup_headers) as resp:
                await resp.read()
            warmup_headers["Referer"] = player_url
        except Exception as exc:
            logger.debug("DLStreams warm-up failed for %s: %s", player_url, exc)

    async def fetch_key_via_browser(self, key_url: str, original_url: str) -> bytes | None:
        self._update_shared_activity()
        cached = self._browser_key_cache.get(key_url)
        if cached:
            return cached

        channel_id = self._extract_channel_id(original_url)
        await self._capture_browser_session_state(channel_id)

        cached = self._browser_key_cache.get(key_url)
        if cached:
            return cached

        channel_key = f"premium{channel_id}"
        player_url = self._build_player_urls(channel_id)[0]
        if self._is_browser_cooldown_active(channel_key):
            logger.debug("DLStreams browser key fetch skipped during cooldown for %s", channel_key)
            return None

        logger.debug("DLStreams browser key fetch starting for %s", key_url)
        try:
            playwright, browser, context = await self._launch_browser()
            try:
                page = await context.new_page()

                async def handle_popup(popup):
                    try:
                        await popup.close()
                    except Exception:
                        pass
                page.on("popup", handle_popup)

                key_bytes: bytes | None = None

                async def on_response(response):
                    nonlocal key_bytes
                    try:
                        if response.url == key_url and response.status == 200 and key_bytes is None:
                            key_bytes = await response.body()
                    except Exception as exc:
                        logger.debug("DLStreams browser response hook failed for %s: %s", response.url, exc)

                page.on("response", on_response)
                await page.goto(player_url, wait_until="domcontentloaded", timeout=30000)

                deadline = time.time() + 25
                while time.time() < deadline and key_bytes is None:
                    await page.wait_for_timeout(250)

                if key_bytes:
                    self._browser_key_cache[key_url] = key_bytes
                    self._clear_browser_failure(channel_key)
                    logger.debug("DLStreams browser key fetch succeeded for %s", key_url)
                    return key_bytes
                self._clear_channel_cache(channel_id)
            finally:
                await page.close()
        except PlaywrightTimeoutError as exc:
            logger.warning("DLStreams browser key fetch timed out for %s: %s", key_url, exc)
        except Exception as exc:
            logger.warning("DLStreams browser key fetch failed for %s: %s", key_url, exc)

        self._mark_browser_failure(channel_key)
        return None

    async def _fetch_manifest_directly(self, url: str, headers: dict) -> str | None:
        """Attempts to fetch the manifest directly using captured session cookies."""
        session = await self._get_session()
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if text.lstrip().startswith("#EXTM3U"):
                        logger.debug("DLStreams manifest fetched directly.")
                        return text
                logger.debug("DLStreams direct manifest fetch failed with status %s", resp.status)
        except Exception as exc:
            logger.debug("DLStreams direct manifest fetch error: %s", exc)
        return None

    async def _lookup_server_key(self, lookup_base: str, channel_key: str, referer_origin: str) -> str:
        """Best-effort server key lookup used to build manifest URL candidates."""
        session = await self._get_session()
        lookup_url = f"{lookup_base.rstrip('/')}/server_lookup?channel_id={channel_key}"
        headers = {
            "Referer": f"{referer_origin.rstrip('/')}/",
            "User-Agent": self.base_headers["User-Agent"],
        }
        try:
            async with session.get(lookup_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    key = data.get("server_key", "wind")
                    if isinstance(key, str) and key:
                        return key
        except Exception as exc:
            logger.debug("DLStreams server lookup failed for %s: %s", channel_key, exc)
        return "wind"

    async def _capture_browser_session_state(self, channel_id: str, player_url: str | None = None) -> str | None:
        channel_key = f"premium{channel_id}"
        if self._is_browser_cooldown_active(channel_key):
            logger.debug("DLStreams browser session capture skipped during cooldown for %s", channel_key)
            return None

        lock = self._get_browser_lock(channel_key)
        async with lock:
            if self._is_browser_cooldown_active(channel_key):
                return None

            resolved_player_url = player_url or self._build_player_urls(channel_id)[0]
            logger.debug("DLStreams browser session capture starting for %s", channel_key)
            try:
                playwright, browser, context = await self._launch_browser()
                try:
                    page = await context.new_page()

                    async def handle_popup_capture(popup):
                        try:
                            await popup.close()
                            logger.debug("🛡️ Bloccato popup pubblicitario di DLStreams!")
                        except Exception:
                            pass
                    page.on("popup", handle_popup_capture)

                    manifest_text: str | None = None

                    async def on_response(response):
                        nonlocal manifest_text
                        try:
                            # Catch any EXTM3U manifest from a proxy-style URL
                            is_manifest_candidate = "/proxy/" in response.url and channel_key in response.url
                            
                            if is_manifest_candidate and response.status == 200:
                                body = await response.body()
                                decoded = body.decode("utf-8", errors="ignore")
                                if decoded.lstrip().startswith("#EXTM3U"):
                                    manifest_text = decoded
                                    self.stream_origin = self._origin_of(response.url)
                                    logger.debug(f"DLStreams captured manifest from: {response.url}")
                            
                            if "/key/" in response.url and response.status == 200:
                                body = await response.body()
                                self._browser_key_cache[response.url] = body
                                self.stream_origin = self._origin_of(response.url)
                                logger.debug(f"DLStreams captured key from: {response.url}")
                        except Exception as exc:
                            logger.debug("DLStreams browser capture hook failed for %s: %s", response.url, exc)

                    context.on("response", on_response)
                    await page.goto(resolved_player_url, wait_until="load", timeout=30000)
                    
                    # Small interaction to trigger player/manifest generation
                    try:
                        await page.mouse.click(683, 384) # Click center of 1366x768
                        await page.wait_for_timeout(2000)
                    except:
                        pass

                    deadline = time.time() + 35
                    while time.time() < deadline:
                        has_key = any("/key/" in key for key in self._browser_key_cache)
                        if manifest_text and has_key:
                            break
                        await page.wait_for_timeout(500)

                    if manifest_text:
                        self._last_working_player[channel_id] = resolved_player_url
                        self._clear_browser_failure(channel_key)
                    else:
                        self._clear_channel_cache(channel_id)
                        self._mark_browser_failure(channel_key)

                    self._captured_cookies = await context.cookies()
                    
                    # Log cookie expirations and calculate dynamic refresh interval
                    min_expiry_remaining = 3600.0  # Default 1 hour fallback
                    found_expiring_cookie = False

                    for cookie in self._captured_cookies:
                        expiry = cookie.get('expires', -1)
                        if expiry != -1:
                            remaining = expiry - time.time()
                            # Only consider cookies that expire in the near-ish future (less than 1 week)
                            # extremely long-lived ones are likely tracking IDs
                            if 0 < remaining < 604800: 
                                if not found_expiring_cookie or remaining < min_expiry_remaining:
                                    min_expiry_remaining = remaining
                                    found_expiring_cookie = True
                            
                            logger.debug(f"🍪 Cookie captured: {cookie['name']} (Domain: {cookie['domain']}) - Expires in: {remaining/3600:.2f} hours")
                        else:
                            logger.debug(f"🍪 Cookie captured: {cookie['name']} (Domain: {cookie['domain']}) - Session cookie")

                    # Calculate adaptive interval: 80% of shortest lifespan, capped between 2m and 1h
                    adaptive_interval = max(120, min(3600, min_expiry_remaining * 0.8))
                    self._dynamic_refresh_interval[channel_key] = adaptive_interval
                    logger.debug(f"🔄 Dynamic refresh interval for {channel_key} set to {adaptive_interval/60:.2f} minutes")

                    # Sync cookies to session
                    if self.session:
                        yarl_url = URL(resolved_player_url)
                        for cookie in self._captured_cookies:
                            self.session.cookie_jar.update_cookies({cookie['name']: cookie['value']}, response_url=yarl_url)

                    logger.debug("DLStreams browser session capture completed for %s", channel_key)
                    self._last_session_refresh[channel_key] = time.time()
                    return manifest_text
                finally:
                    await page.close()
            except Exception as exc:
                self._mark_browser_failure(channel_key)
                logger.warning("DLStreams browser session capture failed for %s: %s", channel_key, exc)
                return None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            # DLStreams keys and segments appear to be tied to a consistent
            # egress/session context. Using rotating/global proxies here can
            # produce a different AES key than the browser receives.
            connector = TCPConnector(limit=0, limit_per_host=0)
            
            timeout = ClientTimeout(total=30, connect=10)
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self.session

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers bypassing the public watch page."""
        self._update_shared_activity()
        try:
            # Extract ID from URL or use as is if numeric
            channel_id = self._extract_channel_id(url)

            channel_key = f"premium{channel_id}"
            session = await self._get_session()
            
            # Use cached session info if available to find server and origin
            iframe_origin = self.entry_origin.rstrip("/")
            lookup_base = self.stream_origin.rstrip("/")
            
            # Determine server_key early so we can try multiple direct manifest URLs
            # before requiring a browser refresh.
            server_key = await self._lookup_server_key(lookup_base, channel_key, iframe_origin)

            # 1. FETCH ACTUAL MANIFEST (Bypassing permanent cache)
            candidate_urls = [
                f"{lookup_base}/proxy/{server_key}/{channel_key}/mono.css",
                f"{lookup_base}/proxy/wind/{channel_key}/mono.css",
                f"{lookup_base}/proxy/top1/cdn/{channel_key}/mono.css",
            ]
            # preserve order and remove duplicates
            seen = set()
            candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]
            m3u8_url = candidate_urls[0]
            
            playback_headers = {
                "Referer": f"{iframe_origin}/",
                "Origin": iframe_origin,
                "User-Agent": self.base_headers["User-Agent"],
                "Accept": "*/*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            }
            
            # Prep cookies for the session if we have them
            current_cookie_header = self._get_cookie_header_for_url(m3u8_url)
            if not current_cookie_header and self._captured_cookies:
                # Prime session jar with captured browser cookies if empty
                yarl_url = URL(m3u8_url)
                for c in self._captured_cookies:
                    session.cookie_jar.update_cookies({c['name']: c['value']}, response_url=yarl_url)
            
            # 1. CHECK MICRO-CACHE (3s)
            cached_item = self._manifest_cache.get(channel_key)
            if cached_item and (time.time() - cached_item[1] < 3):
                logger.debug("DLStreams manifest returned from micro-cache for %s", channel_key)
                return {
                    "destination_url": m3u8_url,
                    "request_headers": playback_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "captured_manifest": cached_item[0],
                }

            # 2. PROACTIVE BACKGROUND REFRESH
            # Use dynamic interval based on cookie expiration (fallback to 15m if not set yet)
            last_refresh = self._last_session_refresh.get(channel_key, 0)
            refresh_threshold = self._dynamic_refresh_interval.get(channel_key, 900)
            
            if last_refresh > 0 and (time.time() - last_refresh > refresh_threshold):
                if channel_key not in self._refresh_tasks or self._refresh_tasks[channel_key].done():
                    logger.debug("DLStreams spawning proactive background refresh for %s (threshold: %.1fm)", 
                                channel_key, refresh_threshold / 60)
                    # We use a wrapper to ensure the task is cleaned up
                    async def do_refresh():
                        try:
                            await self._capture_browser_session_state(channel_id)
                        except Exception as e:
                            logger.error("DLStreams background refresh failed: %s", e)
                    
                    self._refresh_tasks[channel_key] = asyncio.create_task(do_refresh())

            # 3. FETCH ACTUAL MANIFEST
            # Initial direct fetch attempt
            captured_manifest = None
            for candidate in candidate_urls:
                captured_manifest = await self._fetch_manifest_directly(candidate, playback_headers)
                if captured_manifest:
                    m3u8_url = candidate
                    break
            
            if not captured_manifest:
                # If direct fetch fails, we need to re-capture session state via browser (synchronous fallback)
                logger.info("DLStreams direct fetch failed or session expired. Refreshing via browser...")
                player_urls = self._prioritize_player_urls(channel_id)
                for candidate in player_urls:
                    await self._prime_dlstreams_session(session, candidate)
                    captured_manifest = await self._capture_browser_session_state(channel_id, candidate)
                    if captured_manifest:
                        # Recalculate base after re-capture
                        lookup_base = self.stream_origin.rstrip("/")
                        server_key = await self._lookup_server_key(lookup_base, channel_key, iframe_origin)
                        m3u8_url = f"{lookup_base}/proxy/{server_key}/{channel_key}/mono.css"
                        break
            
            if not captured_manifest:
                raise ExtractorError("Could not retrieve manifest after browser refresh.")
            
            # Update micro-cache
            self._manifest_cache[channel_key] = (captured_manifest, time.time())

            # 2. SERVER LOOKUP: refresh once more after possible browser re-capture
            server_key = await self._lookup_server_key(lookup_base, channel_key, iframe_origin)
            logger.debug(f"Found server_key: {server_key} via {iframe_origin}")

            # 2. Construct M3U8 URL
            m3u8_url = f"{lookup_base}/proxy/{server_key}/{channel_key}/mono.css"

            # 3. Setup headers for playback/proxying
            playback_headers = {
                "Referer": f"{iframe_origin}/",
                "Origin": iframe_origin,
                "User-Agent": self.base_headers["User-Agent"],
                "Accept": "*/*",
                "X-Direct-Connection": "1",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            }
            
            # Combine cookies from session and captured playwright cookies
            cookie_header = self._get_cookie_header_for_url(m3u8_url)
            
            # Also add cookies captured directly from the browser context
            if self._captured_cookies:
                relevant_cookies = []
                stream_domain = urlparse(m3u8_url).netloc
                entry_domain = urlparse(self.entry_origin).netloc
                
                for c in self._captured_cookies:
                    if stream_domain in c['domain'] or entry_domain in c['domain'] or c['domain'] in stream_domain:
                        relevant_cookies.append(f"{c['name']}={c['value']}")
                
                if relevant_cookies:
                    browser_cookie_str = "; ".join(relevant_cookies)
                    if cookie_header:
                        cookie_header = f"{cookie_header}; {browser_cookie_str}"
                    else:
                        cookie_header = browser_cookie_str

            if cookie_header:
                playback_headers["Cookie"] = cookie_header

            logger.info(f"Extracted M3U8: {m3u8_url}")

            return {
                "destination_url": m3u8_url,
                "request_headers": playback_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
                "captured_manifest": captured_manifest,
            }

        except Exception as e:
            logger.exception(f"DLStreams extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
