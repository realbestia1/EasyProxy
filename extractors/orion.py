import logging
import random
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class OrionExtractor:
    """Extractor for Orionoid streams to ensure correct headers are passed."""

    def __init__(self, request_headers, proxies=None):
        self.request_headers = request_headers
        self.proxies = proxies or []
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    async def extract(self, url, **kwargs):
        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

        headers = self.base_headers.copy()
        # Default headers that mimic a browser request
        headers.update({
            "referer": origin,
            "origin": origin,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        })

        # Pass specific headers from client, including Cookie which is critical for some services
        # GenericHLSExtractor filters Cookie out, but we include it here.
        for h, v in self.request_headers.items():
            if h.lower() in ["cookie", "authorization", "user-agent", "referer", "accept", "accept-language", "range"]:
                headers[h] = v

        return {
            "destination_url": url,
            "request_headers": headers,
            "mediaflow_endpoint": "hls_proxy"
        }
