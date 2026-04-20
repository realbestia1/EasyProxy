import logging
import random
import re
import base64
import json
from urllib.parse import urljoin
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, get_connector_for_proxy

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class VoeExtractor:
    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or []

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies) if url else self._get_random_proxy()
            if proxy:
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=60, enable_cleanup_closed=True, force_close=False, use_dns_cache=True)

            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["user-agent"]})
        return self.session

    async def extract(self, url: str, redirect_count: int = 0, **kwargs) -> dict:
        session = await self._get_session(url)
        async with session.get(url) as response:
            text = await response.text()

        # See https://github.com/Gujal00/ResolveURL/blob/master/script.module.resolveurl/lib/resolveurl/plugins/voesx.py
        redirect_pattern = r'''window\.location\.href\s*=\s*'([^']+)'''
        redirect_match = re.search(redirect_pattern, text, re.DOTALL)
        if redirect_match:
            if redirect_count >= 5:
                raise ExtractorError("VOE: too many redirects")
            return await self.extract(redirect_match.group(1), redirect_count=redirect_count + 1)

        code_and_script_pattern = r'json">\["([^"]+)"]</script>\s*<script\s*src="([^"]+)'
        code_and_script_match = re.search(code_and_script_pattern, text, re.DOTALL)
        if not code_and_script_match:
            raise ExtractorError("VOE: unable to locate obfuscated payload or external script URL")

        script_url = urljoin(url, code_and_script_match.group(2))
        async with session.get(script_url) as script_response:
            script_text = await script_response.text()

        luts_pattern = r"(\[(?:'\W{2}'[,\]]){1,9})"
        luts_match = re.search(luts_pattern, script_text, re.DOTALL)
        if not luts_match:
            raise ExtractorError("VOE: unable to locate LUTs in external script")

        data = self.voe_decode(code_and_script_match.group(1), luts_match.group(1))

        final_url = data.get('source')
        if not final_url:
            raise ExtractorError("VOE: failed to extract video URL")

        self.base_headers["referer"] = url
        return {
            "destination_url": final_url,
            "request_headers": self.base_headers,
            "mediaflow_endpoint": "hls_proxy",
        }

    @staticmethod
    def voe_decode(ct: str, luts: str) -> dict:
        lut = [''.join([('\\' + x) if x in '.*+?^${}()|[]\\' else x for x in i]) for i in luts[2:-2].split("','")]
        txt = ''
        for i in ct:
            x = ord(i)
            if 64 < x < 91:
                x = (x - 52) % 26 + 65
            elif 96 < x < 123:
                x = (x - 84) % 26 + 97
            txt += chr(x)
        for i in lut:
            txt = re.sub(i, '', txt)
        ct = base64.b64decode(txt).decode('utf-8')
        txt = ''.join([chr(ord(i) - 3) for i in ct])
        txt = base64.b64decode(txt[::-1]).decode('utf-8')
        return json.loads(txt)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()