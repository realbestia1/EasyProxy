from extractors.base import BaseExtractor
from utils.packed import eval_solver


class SupervideoExtractor(BaseExtractor):
    """Supervideo URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="supervideo")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Supervideo URL."""
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "User-Agent": self.base_headers["User-Agent"],
        }
        patterns = [r'file:"(.*?)"']

        session = await self._get_session(url)
        final_url = await eval_solver(session, url, headers, patterns)

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }
