from extractors.base import BaseExtractor
from utils.packed import eval_solver


class FastreamExtractor(BaseExtractor):
    """Fastream URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="fastream")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Fastream URL."""
        session = await self._get_session(url)

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
            "Accept-Language": "en-US,en;q=0.5",
            "user-agent": self.base_headers["User-Agent"],
        }
        patterns = [r'file:"(.*?)"']

        final_url = await eval_solver(session, url, headers, patterns)

        domain = url.replace('https://', '').split('/')[0]
        self.base_headers["referer"] = f"https://{domain}/"
        self.base_headers["origin"] = f"https://{domain}"
        self.base_headers["Accept-Language"] = "en-US,en;q=0.5"
        self.base_headers["Accept"] = "*/*"

        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
