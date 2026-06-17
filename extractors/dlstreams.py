import logging
import socket
import re
import time
import asyncio
import base64
from urllib.parse import urlparse
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from config import (
    get_connector_for_proxy,
    get_preferred_proxy_for_url,
)

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class DLStreamsExtractor:
    """Extractor for daddy live / dlstreams streams."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.entry_origin = ""
        self.stream_origin = ""
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or []
        self.bypass_warp_active = bypass_warp
        self._last_activity = time.time()
        self._inflight_extract_tasks: dict[str, asyncio.Task] = {}

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

    def _prioritize_player_urls(self, channel_id: str) -> list[str]:
        return self._build_player_urls(channel_id)

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _sync_entry_origin_from_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin != self.entry_origin:
                logger.debug("DLStreams entry origin changed from %s to %s", self.entry_origin, origin)
                self.entry_origin = origin
            if not self.stream_origin:
                self.stream_origin = origin

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
        match_id = re.search(r"(?:id=|premium|stream-)(\d+)", url)
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

    async def _extract_directly(self, url: str, channel_id: str) -> Dict[str, Any] | None:
        """Fast path direct HTTP M3U8 extraction without Playwright."""
        session = await self._get_session(url)
        player_urls = self._prioritize_player_urls(channel_id)
        
        for candidate in player_urls:
            try:
                headers = {
                    "User-Agent": self.base_headers["User-Agent"],
                    "Referer": url,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                }
                
                logger.debug("DLStreams: GET stream page %s", candidate)
                async with session.get(candidate, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                
                # Extract player iframe src
                iframe_src = None
                if BeautifulSoup:
                    try:
                        soup = BeautifulSoup(html, "html.parser")
                        iframe_el = soup.find("iframe", id="thatframe") or soup.find("iframe")
                        if iframe_el:
                            iframe_src = iframe_el.get("src")
                    except Exception as e:
                        logger.debug("DLStreams: bs4 parsing error: %s", e)
                
                if not iframe_src:
                    match = re.search(r'<iframe\s+[^>]*src=["\'](https?://[^"\']+)["\']', html, re.I)
                    if match:
                        iframe_src = match.group(1)
                
                if not iframe_src:
                    logger.debug("DLStreams: player iframe not found in HTML of %s", candidate)
                    continue
                
                logger.debug("DLStreams: found player iframe: %s", iframe_src)
                
                # Fetch iframe player page
                iframe_headers = headers.copy()
                iframe_headers["Referer"] = candidate
                iframe_headers["Origin"] = self.entry_origin
                
                async with session.get(iframe_src, headers=iframe_headers, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    iframe_html = await resp.text()
                
                # Extract atob(...) Base64 encoded stream URL
                atob_match = re.search(r"atob\(['\"](.*?)['\"]\)", iframe_html)
                if not atob_match:
                    logger.debug("DLStreams: atob parameter not found in iframe HTML")
                    continue
                
                b64_url = atob_match.group(1)
                stream_url = base64.b64decode(b64_url).decode('utf-8', errors='ignore')
                logger.debug("DLStreams: decrypted stream URL: %s", stream_url)
                
                # Format response payload
                parsed_stream = urlparse(stream_url)
                parsed_iframe = urlparse(iframe_src)
                iframe_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"
                
                # Use entry origin as the Referer/Origin for playback headers to pass CDN security checks
                ref_origin = self.entry_origin.rstrip("/") if self.entry_origin else iframe_origin
                
                playback_headers = {
                    "Referer": f"{ref_origin}/",
                    "Origin": ref_origin,
                    "User-Agent": self.base_headers["User-Agent"],
                    "Accept": "*/*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "cross-site",
                }
                
                # Sync session cookies for playback/proxying
                self.stream_origin = f"{parsed_stream.scheme}://{parsed_stream.netloc}"
                
                # Store cookies in session if needed
                cookie_header = self._get_cookie_header_for_url(stream_url)
                if cookie_header:
                    playback_headers["Cookie"] = cookie_header
                
                return {
                    "destination_url": stream_url,
                    "request_headers": playback_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "captured_manifest": None,
                    "captured_manifests": {stream_url: ""},
                }
                
            except Exception as e:
                logger.debug("DLStreams: direct extraction candidate %s failed: %s", candidate, e)
                continue
                
        return None


    async def _get_session(self, url: str | None = None):
        # Determine the correct proxy for the current state
        target_url = url or self.stream_origin or self.entry_origin
        proxy_url = await get_preferred_proxy_for_url(target_url, "dlstreams", self.proxies, self.bypass_warp_active)
        
        # If we have an existing session, check if its proxy matches what we need now
        if self.session and not self.session.closed:
            # We store the proxy used for the current session in a custom attribute
            session_proxy = getattr(self, "_session_proxy", "NOT_SET")
            if session_proxy == proxy_url:
                return self.session
            else:
                logger.debug("DLStreams: Proxy choice changed (was %s, now %s). Closing old session.", session_proxy, proxy_url)
                await self.session.close()
                self.session = None

        # DLStreams keys and segments appear to be tied to a consistent
        # egress/session context. Using rotating/global proxies here can
        # produce a different AES key than the browser receives.
        if proxy_url:
            connector = get_connector_for_proxy(proxy_url)
            logger.debug("DLStreams: Using proxy session: %s", proxy_url)
        else:
            connector = TCPConnector(limit=0, limit_per_host=0, family=socket.AF_INET)
            logger.debug("DLStreams: Using direct session (Real IP)")
        
        timeout = ClientTimeout(total=30, connect=10)
        self.session = ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self.base_headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._session_proxy = proxy_url # Store for future comparison
        return self.session

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers bypassing the public watch page."""
        self._update_shared_activity()
        self._sync_entry_origin_from_url(url)
        channel_id = self._extract_channel_id(url)
        channel_key = f"premium{channel_id}"

        existing_task = self._inflight_extract_tasks.get(channel_key)
        if existing_task and not existing_task.done():
            logger.debug("DLStreams: waiting for in-flight extraction of %s", channel_key)
            return await existing_task

        task = asyncio.create_task(self._extract_impl(url, channel_id=channel_id, **kwargs))
        self._inflight_extract_tasks[channel_key] = task
        try:
            return await task
        finally:
            current_task = self._inflight_extract_tasks.get(channel_key)
            if current_task is task:
                self._inflight_extract_tasks.pop(channel_key, None)

    async def _extract_impl(self, url: str, channel_id: str, **kwargs) -> Dict[str, Any]:
        try:
            session = await self._get_session(url)

            # Direct browser-less HTTP extraction (only active path)
            try:
                logger.info("DLStreams: Attempting direct browser-less HTTP extraction for %s", f"premium{channel_id}")
                direct_result = await self._extract_directly(url, channel_id)
                if direct_result:
                    logger.info("DLStreams: Direct browser-less extraction succeeded for %s!", f"premium{channel_id}")
                    return direct_result
            except Exception as direct_exc:
                logger.error("DLStreams: Direct browser-less extraction failed for %s: %s", f"premium{channel_id}", direct_exc)

            raise ExtractorError("Could not retrieve manifest via browser-less extraction (browser fallback is disabled).")

        except Exception as e:
            logger.exception(f"DLStreams extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def close(self):
        pending_tasks = list(self._inflight_extract_tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._inflight_extract_tasks.clear()
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
