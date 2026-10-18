import re
from urllib.parse import urlparse
from extractors.base import BaseExtractor, ExtractorError

class VidozaExtractor(BaseExtractor):
    """Vidoza URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="vidoza", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidoza URL."""
        self._apply_routing_kwargs(url, kwargs)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        # vidoza.in now serves the ShareVideo platform (SPA + JSON API)
        if host.endswith("vidoza.in"):
            return await self._extract_sharevideo(url, parsed)

        # Accept vidoza + videzz
        if not (host.endswith("vidoza.net") or host.endswith("videzz.net")):
            raise ExtractorError("VIDOZA: Invalid domain")

        headers = self.base_headers.copy()
        headers.update({
            "referer": "https://vidoza.net/",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
        })

        # 1) Fetch the embed page
        resp = await self._make_request(url, headers=headers)
        html = resp.text
        cookies = {k: v.value for k, v in resp.cookies.items()}

        if not html:
            raise ExtractorError("VIDOZA: Empty HTML from Vidoza")

        # 2) Extract final link with REGEX
        pattern = re.compile(
            r"""["']?\s*(?:file|src)\s*["']?\s*[:=,]?\s*["'](?P<url>[^"']+)"""
            r"""(?:[^}>\]]+)["']?\s*res\s*["']?\s*[:=]\s*["']?(?P<label>[^"',]+)""",
            re.IGNORECASE,
        )

        match = pattern.search(html)
        if not match:
            raise ExtractorError("VIDOZA: Unable to extract video + label from JS")

        mp4_url = match.group("url")
        # label = match.group("label").strip()  # available but not used

        # Fix URLs like //str38.vidoza.net/...
        if mp4_url.startswith("//"):
            mp4_url = "https:" + mp4_url

        # 3) Attach cookies (token may depend on these)
        if cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return self._result(mp4_url, headers)

    async def _extract_sharevideo(self, url: str, parsed) -> dict:
        """Resolve a vidoza.in /video/<id> page to its direct CDN MP4."""
        video_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-fA-F]{24}", video_id):
            raise ExtractorError(f"VIDOZA: unsupported vidoza.in URL: {url}")

        headers = self.base_headers.copy()
        headers.update({
            "accept": "application/json",
            "x-client-type": "web",
            "secret-key": "5TIvw5cpc0",
            "referer": url,
        })
        resp = await self._make_request(
            "https://vidoza.in/client/video/detailsOfVideo",
            headers=headers,
            params={
                "userId": "000000000000000000000000",
                "videoId": video_id,
                "videoType": 1,
            },
        )

        video = (resp.json or {}).get("detailsOfVideo") or {}
        mp4_url = video.get("videoUrl")
        if not mp4_url:
            raise ExtractorError("VIDOZA: vidoza.in API returned no video URL")

        return self._result(mp4_url, {})

