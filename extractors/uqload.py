import logging
import re
from urllib.parse import urljoin, urlparse
from utils.packed import unpack
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

class UqloadExtractor(BaseExtractor):
    """Uqload URL extractor."""

    # Full browser-like headers required to bypass Cloudflare/bot checks on uqload
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    # Regex patterns tried in order — first the exact mediaflow pattern, then flexible fallbacks
    SOURCE_PATTERNS = [
        r'sources: \["(.*?)"\]',                              # mediaflow exact — works on most uqload pages
        r'sources\s*:\s*\[\s*["\']([^"\']+)["\']',          # flexible spacing/quotes variant
        r'"?sources"?\s*:\s*\[\s*["\']([^"\']+)["\']',      # with optional quotes on key
        r'file\s*:\s*["\']([^"\']+\.(?:m3u8|mp4)(?:\?[^"\']*)?)["\']',
        r'src\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',       # src: "...mp4..."
        r'video_url\s*=\s*["\']([^"\']+)["\']',              # var video_url = "..."
        r'player\.src\s*\(\s*["\']([^"\']+)["\']',           # player.src("...")
        r'(?:https?://[a-z0-9.-]*uqload[a-z.]*)/[a-z0-9/]+\.mp4[^"\'<\s]*',  # raw mp4 URL on page
    ]

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="uqload", bypass_warp=bypass_warp)

    @classmethod
    def _find_source(cls, text: str) -> str | None:
        for pattern in cls.SOURCE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return (match.group(1) if match.lastindex else match.group(0)).strip()
        return None

    @classmethod
    def _extract_source(cls, text: str, base_url: str) -> str | None:
        source = cls._find_source(text)
        if source:
            return urljoin(base_url, source.replace("\\/", "/"))

        for script in re.findall(r"<script[^>]*>(.*?)</script>", text, re.DOTALL | re.IGNORECASE):
            if "eval(function(p,a,c,k,e,d)" not in script:
                continue
            try:
                source = cls._find_source(unpack(script))
            except Exception as exc:
                logger.debug("[Uqload] Failed to unpack source script: %s", exc)
                continue
            if source:
                return urljoin(base_url, source.replace("\\/", "/"))
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Uqload video URL."""
        self._apply_routing_kwargs(url, kwargs)
        logger.debug(f"[Uqload] Fetching embed page: {url}")

        resp = await self._make_request(url, headers=self.BROWSER_HEADERS)
        text = resp.text
        final_url = resp.url

        logger.debug(f"[Uqload] Page length: {len(text)} chars, final URL: {final_url}")

        # Check for common error pages
        text_lower = text.lower()
        if (
            "file was deleted" in text_lower
            or "file not found" in text_lower
            or "not found" in text_lower
            or "no longer available" in text_lower
            or "has been deleted" in text_lower
        ):
            raise ExtractorError(f"Uqload video removed/not found: {url}")

        source_base = final_url or url
        video_url = self._extract_source(text, source_base)

        # Current Uqload /e/<code> pages keep the player source behind the
        # browser's POST to /dl; the embed HTML only contains the play button.
        if not video_url:
            parsed = urlparse(source_base)
            file_code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            file_code = re.sub(r"\.html$", "", file_code, flags=re.IGNORECASE)
            file_code = file_code.rsplit("-", 1)[-1]
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if file_code and parsed.scheme and parsed.netloc:
                post_headers = dict(self.BROWSER_HEADERS)
                post_headers.update(
                    {
                        "Accept": "*/*",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Origin": origin,
                        "Referer": source_base,
                        "X-Requested-With": "XMLHttpRequest",
                    }
                )
                post_response = await self._make_request(
                    f"{origin}/dl",
                    method="POST",
                    headers=post_headers,
                    data={
                        "op": "embed",
                        "file_code": file_code,
                        "auto": "1",
                        "referer": self.request_headers.get("Referer", ""),
                    },
                )
                video_url = self._extract_source(
                    post_response.text,
                    post_response.url or source_base,
                )
        if video_url:
            logger.debug(f"[Uqload] Extracted source: {video_url[:80]}...")

        if not video_url:
            # Log more context to help debug
            logger.warning(f"[Uqload] No pattern matched for {url}")
            logger.warning(f"[Uqload] Page title: {re.search(r'<title>(.*?)</title>', text, re.I)}")
            logger.warning(f"[Uqload] Page snippet (first 500): {text[:500]!r}")
            # Also log any script blocks that might contain the video URL
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL | re.IGNORECASE)
            for idx, script in enumerate(scripts):
                if 'source' in script.lower() or 'file' in script.lower() or '.mp4' in script.lower():
                    logger.warning(f"[Uqload] Relevant script #{idx}: {script[:300]!r}")
            raise ExtractorError(f"Failed to extract video URL from uqload page: {url}")

        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        media_path = urlparse(video_url).path.lower()
        mediaflow_endpoint = (
            "proxy_stream_endpoint"
            if media_path.endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"))
            else self.mediaflow_endpoint
        )
        return {
            "destination_url": video_url,
            "request_headers": {
                "user-agent": self.BROWSER_HEADERS["User-Agent"],
                "referer": f"{origin}/",
                "origin": origin,
            },
            "mediaflow_endpoint": mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

