import json
from bs4 import BeautifulSoup, SoupStrainer

from extractors.base import BaseExtractor, ExtractorError


class OkruExtractor(BaseExtractor):
    """Okru (ok.ru) URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="okru")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Okru URL."""
        resp = await self._make_request(url)
        text = resp.text

        soup = BeautifulSoup(text, "lxml", parse_only=SoupStrainer("div"))
        if soup:
            div = soup.find("div", {"data-module": "OKVideo"})
            if not div:
                raise ExtractorError("Failed to find video element")

            data_options = div.get("data-options")
            data = json.loads(data_options)
            metadata = json.loads(data["flashvars"]["metadata"])
            final_url = (
                    metadata.get("hlsMasterPlaylistUrl") or metadata.get("hlsManifestUrl") or metadata.get(
                "ondemandHls")
            )

            if not final_url:
                raise ExtractorError("Failed to extract stream URL from metadata")

            self.base_headers["referer"] = url
            return {
                "destination_url": final_url,
                "request_headers": self.base_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        raise ExtractorError("Failed to parse OK.ru page")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
