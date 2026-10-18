from utils.packed import eval_solver
from extractors.base import BaseExtractor

class SupervideoExtractor(BaseExtractor):
    """Supervideo URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="supervideo", bypass_warp=bypass_warp)

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Supervideo URL."""
        self._apply_routing_kwargs(url, kwargs)
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "User-Agent": self.base_headers["User-Agent"],
        }
        patterns = [r'file:"(.*?)"']

        session = await self._get_session(url)
        final_url = await eval_solver(session, url, headers, patterns)

        self.base_headers["referer"] = url
        return self._result(final_url, self.base_headers)
