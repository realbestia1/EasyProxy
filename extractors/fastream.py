from urllib.parse import urlparse
from utils.packed import eval_solver
from extractors.base import BaseExtractor

class FastreamExtractor(BaseExtractor):
    """Fastream URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="fastream", bypass_warp=bypass_warp)

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Fastream URL."""
        self._apply_routing_kwargs(url, kwargs)
        session = await self._get_session(url)
        
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
            "Accept-Language": "en-US,en;q=0.5",
            "user-agent": self.base_headers["User-Agent"],
        }
        patterns = [r'file:"(.*?)"']

        domain = url.replace('https://', '').split('/')[0]
        code = urlparse(url).path.rstrip('/').split('/')[-1].split('-')[-1]
        if code.endswith('.html'):
            code = code[:-5]

        # La pagina embed non contiene più il player inline: fa una POST a /dl
        # (op=embed) che risponde col JS packed contenente l'URL HLS.
        final_url = await eval_solver(
            session,
            f"https://{domain}/dl",
            headers,
            patterns,
            method="POST",
            data={"op": "embed", "file_code": code, "auto": "1", "referer": url},
        )

        self.base_headers["referer"] = f"https://{domain}/"
        self.base_headers["origin"] = f"https://{domain}"
        self.base_headers["Accept-Language"] = "en-US,en;q=0.5"
        self.base_headers["Accept"] = "*/*"

        return self._result(final_url, self.base_headers)

