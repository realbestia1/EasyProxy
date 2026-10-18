from utils.packed import eval_solver
from extractors.base import BaseExtractor

class FileLionsExtractor(BaseExtractor):
    """FileLions URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="filelions", bypass_warp=bypass_warp)

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract FileLions URL."""
        self._apply_routing_kwargs(url, kwargs)
        session = await self._get_session(url)
        
        headers = {}
        # See https://github.com/Gujal00/ResolveURL/blob/master/script.module.resolveurl/lib/resolveurl/plugins/filelions.py
        patterns = [
            r"""sources:\s*\[{file:\s*["'](?P<url>[^"']+)""",
            r"""["']hls4["']:\s*["'](?P<url>[^"']+)""",
            r"""["']hls2["']:\s*["'](?P<url>[^"']+)""",
        ]

        final_url = await eval_solver(session, url, headers, patterns)

        self.base_headers["referer"] = url
        return self._result(final_url, self.base_headers)

