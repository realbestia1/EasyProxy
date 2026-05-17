from extractors.base import BaseExtractor
from utils.packed import eval_solver


class FileLionsExtractor(BaseExtractor):
    """FileLions URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="filelions")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract FileLions URL."""
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
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
