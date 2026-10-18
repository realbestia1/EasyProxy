import re
from urllib.parse import urljoin, urlparse
from extractors.base import BaseExtractor, ExtractorError
from utils.packed import eval_solver


class StreamWishExtractor(BaseExtractor):
    """StreamWish URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="streamwish", bypass_warp=bypass_warp)

    @staticmethod
    def _extract_m3u8(text: str) -> str | None:
        """Extract first absolute m3u8 URL from text"""
        match = re.search(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', text)
        return match.group(0) if match else None

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract StreamWish URL."""
        self._apply_routing_kwargs(url, kwargs)
        kw_headers = kwargs.get("headers") or {}
        referer = (
            kw_headers.get("Referer")
            or kw_headers.get("referer")
            or self.request_headers.get("Referer")
            or self.request_headers.get("referer")
        )
        if not referer:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"

        headers = {"Referer": referer}
        
        resp = await self._make_request(url, headers=headers)
        text = resp.text

        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.DOTALL)
        iframe_url = urljoin(url, iframe_match.group(1)) if iframe_match else url

        resp_iframe = await self._make_request(iframe_url, headers=headers)
        html = resp_iframe.text

        final_url = self._extract_m3u8(html)

        if not final_url and "eval(function(p,a,c,k,e,d)" in html:
            try:
                final_url = await eval_solver(
                    await self._get_session(iframe_url),
                    iframe_url,
                    headers,
                    [
                        # absolute m3u8
                        r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)',
                        # relative stream paths
                        r'(\/stream\/[^"\'\s]+\.m3u8[^"\'\s]*)',
                    ],
                )
            except Exception:
                final_url = None

        if not final_url:
            raise ExtractorError("StreamWish: Failed to extract m3u8")

        if final_url.startswith("/"):
            final_url = urljoin(iframe_url, final_url)

        origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        req_headers = {
            **self.base_headers,
            "Referer": referer,
            "Origin": origin,
        }

        return self._result(final_url, req_headers)

