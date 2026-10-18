import re
from extractors.base import BaseExtractor, ExtractorError

class LuluStreamExtractor(BaseExtractor):
    """LuluStream URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="lulustream", bypass_warp=bypass_warp)

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract LuluStream URL."""
        self._apply_routing_kwargs(url, kwargs)
        resp = await self._make_request(url)
        text = resp.text

        # ponytail: unpack packed script if present, fallback to raw regex match
        packed_match = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+|\[\]),(\d+),'(.*?)'\.split\('\|'\)",
            text,
            re.DOTALL,
        )
        if packed_match:
            try:
                from utils.packed import unpack
                text = unpack(packed_match.group(0))
            except Exception:
                pass

        # See https://github.com/Gujal00/ResolveURL/blob/master/script.module.resolveurl/lib/resolveurl/plugins/lulustream.py
        pattern = r"""sources:\s*\[{file:\s*["'](?P<url>[^"']+)"""
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            raise ExtractorError("Failed to extract source URL")
        
        final_url = match.group(1)

        self.base_headers["referer"] = url
        return self._result(final_url, self.base_headers)

