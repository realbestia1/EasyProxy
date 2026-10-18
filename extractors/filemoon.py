import re
from urllib.parse import urljoin, urlparse

from extractors.base import ExtractorError
from extractors.f16px import F16PxExtractor

_ID_RE = re.compile(r"^[0-9A-Za-z]{8,16}$")
_MEDIA_RE = re.compile(r'https?://[^"\'\s<>]+\.(?:m3u8|mp4|mkv)[^"\'\s<>]*', re.IGNORECASE)


class FileMoonExtractor(F16PxExtractor):
    """FileMoon embed extractor.

    Legacy domains (filemoon.sx, bysejikuar.com, ...) run the Byse platform
    and use the inherited attest/PoW/playback flow.  filemoon.org is a
    different host: ``/{id}/stream`` redirects to a signed media file, which
    is also the player's own source.
    """

    ERROR_PREFIX = "FileMoon"

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, bypass_warp=bypass_warp)
        self.extractor_name = "filemoon"

    @staticmethod
    def _filemoon_org_id(url: str) -> str | None:
        for segment in (urlparse(url).path or "").split("/"):
            if _ID_RE.match(segment):
                return segment
        return None

    async def _extract_filemoon_org(self, url: str, **kwargs) -> dict:
        code = self._filemoon_org_id(url)
        if not code:
            raise ExtractorError(f"{self.ERROR_PREFIX}: missing file id in {url}")

        parsed = urlparse(url)
        stream_url = f"{parsed.scheme}://{parsed.netloc}/{code}/stream"
        self._apply_routing_kwargs(stream_url, kwargs)
        headers = {
            "Accept": "*/*",
            "Referer": url,
            "User-Agent": self.base_headers["User-Agent"],
        }

        session = await self._get_session(stream_url)
        async with session.get(stream_url, headers=headers, allow_redirects=False) as resp:
            location = resp.headers.get("Location") if resp.status in (301, 302, 303, 307, 308) else None
            body = "" if location else await resp.text()

        destination = urljoin(stream_url, location) if location else None
        if not destination:
            match = _MEDIA_RE.search(body)
            destination = match.group(0) if match else None
        if not destination:
            raise ExtractorError(
                f"{self.ERROR_PREFIX}: no media URL from {stream_url}"
            )

        self.base_headers["referer"] = url
        mediaflow_endpoint = (
            "hls_proxy" if ".m3u8" in destination.lower() else "proxy_stream_endpoint"
        )
        return self._result(destination, self.base_headers, mediaflow_endpoint=mediaflow_endpoint)

    async def extract(self, url: str, **kwargs) -> dict:
        host = (urlparse(url).hostname or "").lower()
        if host == "filemoon.org" or host.endswith(".filemoon.org"):
            return await self._extract_filemoon_org(url, **kwargs)
        return await super().extract(url, **kwargs)


__all__ = ["FileMoonExtractor"]
