import logging
import random
import re
from urllib.parse import urljoin, urlparse, unquote
from aiohttp import ClientSession, ClientTimeout, TCPConnector, FormData
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class LiveTVExtractor:
    """LiveTV URL extractor for both M3U8 and MPD streams."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or []
        
        # Patterns for stream URL extraction
        self.fallback_pattern = re.compile(
            r"source: [\'\"](.*?)[\'\"]\s*,\s*[\s\S]*?mimeType: [\'\"](application/x-mpegURL|application/vnd\.apple\.mpegURL|application/dash\+xml)[\'\"]",
            re.IGNORECASE,
        )
        self.any_m3u8_pattern = re.compile(
            r'["\']?(https?://.*?\.m3u8(?:\?[^"\']*)?)["\'"]?',
            re.IGNORECASE,
        )

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies) if url else self._get_random_proxy()
            if proxy:
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=60, enable_cleanup_closed=True, force_close=False, use_dns_cache=True)
            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["user-agent"]})
        return self.session

    async def extract(self, url: str, stream_title: str = None, **kwargs) -> dict:
        """Extract LiveTV URL and required headers."""
        try:
            session = await self._get_session(url)
            
            # Get the channel page
            async with session.get(url) as response:
                response_text = await response.text()
            
            self.base_headers["referer"] = urljoin(url, "/")

            # Extract player API details
            player_api_base, method = await self._extract_player_api_base(response_text)
            if not player_api_base:
                raise ExtractorError("Failed to extract player API URL")

            # Get player options
            options_data = await self._get_player_options(response_text)
            if not options_data:
                raise ExtractorError("No player options found")

            # Process player options to find matching stream
            for option in options_data:
                current_title = option.get("title")
                if stream_title and current_title != stream_title:
                    continue

                # Get stream URL based on player option
                stream_data = await self._process_player_option(
                    player_api_base, method, option.get("post"), option.get("nume"), option.get("type")
                )

                if stream_data:
                    stream_url = stream_data.get("url")
                    if not stream_url:
                        continue

                    result = {
                        "destination_url": stream_url,
                        "request_headers": self.base_headers,
                        "mediaflow_endpoint": self.mediaflow_endpoint,
                    }

                    # Set endpoint based on stream type
                    if stream_data.get("type") == "mpd":
                        if stream_data.get("drm_key_id") and stream_data.get("drm_key"):
                            result.update({
                                "query_params": {
                                    "key_id": stream_data["drm_key_id"],
                                    "key": stream_data["drm_key"],
                                },
                                "mediaflow_endpoint": "mpd_manifest_proxy",
                            })

                    return result

            raise ExtractorError("No valid stream found")

        except Exception as e:
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def _extract_player_api_base(self, html_content: str):
        """Extract player API base URL and method."""
        admin_ajax_pattern = r'"player_api"\s*:\s*"([^"]+)".*?"play_method"\s*:\s*"([^"]+)"'
        match = re.search(admin_ajax_pattern, html_content)
        if not match:
            return None, None
        url = match.group(1).replace("\\/", "/")
        method = match.group(2)
        if method == "wp_json":
            return url, method
        url = urljoin(url, "/wp-admin/admin-ajax.php")
        return url, method

    async def _get_player_options(self, html_content: str) -> list:
        """Extract player options from HTML content."""
        pattern = r'<li[^>]*class=["\']dooplay_player_option["\'][^>]*data-type=["\']([^"\']*)["\'][^>]*data-post=["\']([^"\']*)["\'][^>]*data-nume=["\']([^"\']*)["\'][^>]*>.*?<span class=["\']title["\']>([^<]*)</span>'
        matches = re.finditer(pattern, html_content, re.DOTALL)
        return [
            {"type": match.group(1), "post": match.group(2), "nume": match.group(3), "title": match.group(4).strip()}
            for match in matches
        ]

    async def _process_player_option(self, api_base: str, method: str, post: str, nume: str, type_: str) -> dict:
        """Process player option to get stream URL."""
        session = await self._get_session(api_base)
        
        if method == "wp_json":
            api_url = f"{api_base}{post}/{type_}/{nume}"
            async with session.get(api_url) as response:
                data = await response.json()
        else:
            form_data = FormData()
            form_data.add_field("action", "doo_player_ajax")
            form_data.add_field("post", post)
            form_data.add_field("nume", nume)
            form_data.add_field("type", type_)
            async with session.post(api_base, data=form_data) as response:
                data = await response.json()

        # Get iframe URL from API response
        try:
            iframe_url = urljoin(api_base, data.get("embed_url", "").replace("\\/", "/"))

            # Get stream URL from iframe
            async with session.get(iframe_url) as iframe_response:
                iframe_text = await iframe_response.text()
                iframe_headers = dict(iframe_response.headers)
            
            stream_data = await self._extract_stream_url(iframe_text, iframe_headers, iframe_url)
            return stream_data

        except Exception as e:
            raise ExtractorError(f"Failed to process player option: {str(e)}")

    async def _extract_stream_url(self, iframe_text: str, iframe_headers: dict, iframe_url: str) -> dict:
        """Extract final stream URL from iframe content."""
        try:
            # Parse URL components
            parsed_url = urlparse(iframe_url)
            query_params = dict(param.split("=") for param in parsed_url.query.split("&") if "=" in param)

            # Check if content is already a direct M3U8 stream
            content_types = ["application/x-mpegurl", "application/vnd.apple.mpegurl"]
            content_type = iframe_headers.get("content-type", "")

            if any(ext in content_type.lower() for ext in content_types):
                return {"url": iframe_url, "type": "m3u8"}

            stream_data = {}

            # Check for source parameter in URL
            if "source" in query_params:
                stream_data = {
                    "url": urljoin(iframe_url, unquote(query_params["source"])),
                    "type": "m3u8",
                }

            # Check for MPD stream with DRM
            elif "zy" in query_params and ".mpd``" in query_params["zy"]:
                data = query_params["zy"].split("``")
                url = data[0]
                key_id, key = data[1].split(":")
                stream_data = {"url": url, "type": "mpd", "drm_key_id": key_id, "drm_key": key}

            # Check for tamilultra specific format
            elif "tamilultra" in iframe_url:
                stream_data = {"url": urljoin(iframe_url, parsed_url.query), "type": "m3u8"}

            # Try pattern matching for stream URLs
            else:
                channel_id = query_params.get("id", "")
                stream_url = None

                if channel_id:
                    # Try channel ID specific pattern
                    pattern = rf'{re.escape(channel_id)}["\']:\s*{{\s*["\']?url["\']?\s*:\s*["\']([^"\']+)["\']'
                    match = re.search(pattern, iframe_text)
                    if match:
                        stream_url = match.group(1)

                # Try fallback patterns if channel ID pattern fails
                if not stream_url:
                    for pattern in [self.fallback_pattern, self.any_m3u8_pattern]:
                        match = pattern.search(iframe_text)
                        if match:
                            stream_url = match.group(1)
                            break

                if stream_url:
                    stream_data = {"url": stream_url, "type": "m3u8"}

                    # Check for MPD stream and extract DRM keys
                    if stream_url.endswith(".mpd"):
                        stream_data["type"] = "mpd"
                        drm_data = await self._extract_drm_keys(iframe_text, channel_id)
                        if drm_data:
                            stream_data.update(drm_data)

            # If no stream data found, raise error
            if not stream_data:
                raise ExtractorError("No valid stream URL found")

            # Update stream type based on URL if not already set
            if stream_data.get("type") == "m3u8":
                if stream_data["url"].endswith(".mpd"):
                    stream_data["type"] = "mpd"
                elif not any(ext in stream_data["url"] for ext in [".m3u8", ".m3u"]):
                    stream_data["type"] = "m3u8"

            return stream_data

        except Exception as e:
            raise ExtractorError(f"Failed to extract stream URL: {str(e)}")

    async def _extract_drm_keys(self, html_content: str, channel_id: str) -> dict:
        """Extract DRM keys for MPD streams."""
        try:
            # Pattern for channel entry
            channel_pattern = rf'"{re.escape(channel_id)}":\s*{{[^}}]+}}'
            channel_match = re.search(channel_pattern, html_content)

            if channel_match:
                channel_data = channel_match.group(0)

                # Try clearkeys pattern first
                clearkey_pattern = r'["\']?clearkeys["\']?\s*:\s*{\s*["\'](.+?)["\']:\s*["\'](.+?)["\']'
                clearkey_match = re.search(clearkey_pattern, channel_data)

                # Try k1/k2 pattern if clearkeys not found
                if not clearkey_match:
                    k1k2_pattern = r'["\']?k1["\']?\s*:\s*["\'](.+?)["\'],\s*["\']?k2["\']?\s*:\s*["\'](.+?)["\']'
                    k1k2_match = re.search(k1k2_pattern, channel_data)

                    if k1k2_match:
                        return {"drm_key_id": k1k2_match.group(1), "drm_key": k1k2_match.group(2)}
                else:
                    return {"drm_key_id": clearkey_match.group(1), "drm_key": clearkey_match.group(2)}

            return {}

        except Exception:
            return {}

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
