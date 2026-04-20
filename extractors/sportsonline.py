import asyncio
import logging
import re
import json
from urllib.parse import urlparse, urljoin
from typing import Dict, Any
import gzip
import zlib
import random
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
import zstandard  # Importa la libreria zstandard
from aiohttp_socks import ProxyConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy

from utils.smart_request import smart_request

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""
    pass


def unpack(p, a, c, k, e=None, d=None):
    """
    Unpacker for P.A.C.K.E.R. packed javascript.
    This is a Python port of the common Javascript unpacker.
    """
    while c > 0:
        c -= 1
        if k[c]:
            p = re.sub("\\b" + _int2base(c, a) + "\\b", k[c], p)
    return p


def _int2base(x, base):
    if x < 0:
        sign = -1
    elif x == 0:
        return "0"
    else:
        sign = 1

    x *= sign
    digits = []

    while x:
        digits.append("0123456789abcdefghijklmnopqrstuvwxyz"[x % base])
        x = int(x / base)

    if sign < 0:
        digits.append("-")

    digits.reverse()
    return "".join(digits)


class SportsonlineExtractor:
    """Sportsonline/Sportzonline URL extractor for M3U8 streams."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers or {}
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.proxies = proxies or GLOBAL_PROXIES

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    def update_request_headers(self, request_headers: dict | None):
        self.request_headers = request_headers or {}

    def _get_request_header(self, name: str, default: str | None = None) -> str | None:
        for header_name, header_value in self.request_headers.items():
            if header_name.lower() == name.lower():
                return header_value
        return default

    def _get_origin(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _copy_request_headers(self, header_map: dict[str, str]) -> dict[str, str]:
        copied_headers = {}
        for request_name, output_name in header_map.items():
            value = self._get_request_header(request_name)
            if value:
                copied_headers[output_name] = value
        return copied_headers

    def _build_page_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_request_header(
                "User-Agent", self.base_headers["User-Agent"]
            ),
            "Accept": self._get_request_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            ),
            "Accept-Language": self._get_request_header(
                "Accept-Language", "en-US,en;q=0.9,it;q=0.8"
            ),
            "Cache-Control": self._get_request_header("Cache-Control", "max-age=0"),
            "Upgrade-Insecure-Requests": self._get_request_header(
                "Upgrade-Insecure-Requests", "1"
            ),
            "Sec-Fetch-Site": self._get_request_header("Sec-Fetch-Site", "none"),
            "Sec-Fetch-Mode": self._get_request_header(
                "Sec-Fetch-Mode", "navigate"
            ),
            "Sec-Fetch-User": self._get_request_header("Sec-Fetch-User", "?1"),
            "Sec-Fetch-Dest": self._get_request_header("Sec-Fetch-Dest", "document"),
        }
        headers.update(
            self._copy_request_headers(
                {
                    "sec-ch-ua": "Sec-CH-UA",
                    "sec-ch-ua-mobile": "Sec-CH-UA-Mobile",
                    "sec-ch-ua-platform": "Sec-CH-UA-Platform",
                    "Cookie": "Cookie",
                    "Pragma": "Pragma",
                }
            )
        )
        return headers

    def _build_iframe_headers(self, page_url: str, iframe_url: str) -> dict[str, str]:
        page_headers = self._build_page_headers()
        page_headers["Referer"] = page_url
        page_headers["Origin"] = self._get_origin(page_url)
        page_headers["Sec-Fetch-Site"] = (
            "same-origin"
            if urlparse(page_url).netloc == urlparse(iframe_url).netloc
            else "cross-site"
        )
        page_headers["Sec-Fetch-Dest"] = "iframe"
        page_headers.pop("Sec-Fetch-User", None)
        return page_headers

    def _looks_like_block_page(self, html: str) -> bool:
        lowered = html.lower()
        return any(
            marker in lowered
            for marker in (
                "sorry, you have been blocked",
                "attention required!",
                "cloudflare",
                "access denied",
            )
        )

    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            
            # Determina il proxy per l'URL (se fornito)
            proxy = None
            if url:
                proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            else:
                proxy = self._get_random_proxy()
                
            if proxy:
                logger.info(f"Using proxy {proxy} for Sportsonline session.")
                connector = get_connector_for_proxy(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0)

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": self.base_headers["User-Agent"]},
                cookie_jar=aiohttp.CookieJar(),
            )
        return self.session

    async def _make_robust_request(
        self, url: str, headers: dict = None, retries=2, initial_delay=1, timeout=15
    ):
        """Effettua richieste HTTP robuste usando smart_request per gestire Cloudflare."""
        final_headers = headers or self.base_headers

        for attempt in range(retries):
            try:
                logger.info(f"Attempt {attempt + 1}/{retries} for URL: {url}")
                
                # Usiamo smart_request che gestisce già il bypass Cloudflare
                html = await smart_request("request.get", url, headers=final_headers, proxies=self.proxies)
                
                if not html:
                    raise ExtractorError(f"SmartRequest returned empty response for {url}")
                
                # Restituiamo il contenuto e l'URL finale (mocked)
                return html, url

            except Exception as e:
                logger.warning(f"⚠️ SmartRequest attempt {attempt + 1} failed for {url}: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(initial_delay)
                else:
                    raise ExtractorError(f"All SmartRequest attempts failed for {url}: {str(e)}")
        raise ExtractorError(f"Unable to complete request for {url}")

    async def _handle_response_content(self, response: aiohttp.ClientResponse) -> str:
        """Gestisce la decompressione manuale del corpo della risposta."""
        content_encoding = response.headers.get("Content-Encoding")
        raw_body = await response.read()

        if content_encoding == "zstd":
            dctx = zstandard.ZstdDecompressor()
            try:
                decompressed_body = dctx.decompress(raw_body, max_output_size=104857600)
                return decompressed_body.decode(response.charset or "utf-8")
            except zstandard.ZstdError as zs_e:
                raise ExtractorError(f"Zstd decompression error: {zs_e}")
        elif content_encoding == "gzip":
            decompressed_body = gzip.decompress(raw_body)
            return decompressed_body.decode(response.charset or "utf-8")
        elif content_encoding == "deflate":
            decompressed_body = zlib.decompress(raw_body)
            return decompressed_body.decode(response.charset or "utf-8")
        else:
            return raw_body.decode(response.charset or "utf-8")

    def _detect_packed_blocks(self, html: str) -> list[str]:
        raw_matches: list[str] = []
        strict_eval_pattern = re.compile(r"eval\(function\(p,a,c,k,e,.*?\}\(.*?\)\)", re.DOTALL)
        relaxed_eval_pattern = re.compile(r"eval\(function\(p,a,c,k,e,[dr]\).*?\}\(.*?\)\)", re.DOTALL)

        script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
        for script_body in script_pattern.findall(html):
            if "eval(function(p,a,c,k,e" in script_body:
                strict_matches = strict_eval_pattern.findall(script_body)
                if strict_matches:
                    raw_matches.extend(strict_matches)
                    continue

                relaxed_matches = relaxed_eval_pattern.findall(script_body)
                if relaxed_matches:
                    raw_matches.extend(relaxed_matches)

        if raw_matches:
            return raw_matches

        raw_matches = strict_eval_pattern.findall(html)
        if not raw_matches:
            raw_matches = relaxed_eval_pattern.findall(html)

        return raw_matches

    @staticmethod
    def _extract_m3u8_candidate(text: str) -> str | None:
        patterns = [
            r"var\s+src\s*=\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"src\s*=\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"file\s*:\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']",
            r"[\"']([^\"']*https?://[^\"']+\.m3u8[^\"']*)[\"']",
            r"(https?://[^\s\"'>]+\.m3u8[^\s\"'>]*)",
            r"(//[^\s\"'>]+\.m3u8[^\s\"'>]*)",
            r"(/[^\s\"'>]+\.m3u8[^\s\"'>]*)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)

        return None

    @staticmethod
    def _normalize_stream_url(stream_url: str, base_url: str) -> str:
        cleaned = stream_url.strip().strip("\"'").replace("\\/", "/")
        if cleaned.startswith("//"):
            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme or 'https'}:{cleaned}"
        if not urlparse(cleaned).scheme:
            return urljoin(base_url, cleaned)
        return cleaned

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Main extraction flow: fetch page, extract iframe, unpack and find m3u8."""
        try:
            self.update_request_headers(kwargs.get("request_headers"))
            
            parsed_source = urlparse(url)
            source_origin = f"{parsed_source.scheme}://{parsed_source.netloc}"
            source_referer = self._get_request_header("Referer") or f"{source_origin}/"
            user_agent = self._get_request_header("User-Agent", self.base_headers["User-Agent"])

            # Step 1: Fetch main page
            logger.info(f"Fetching main page: {url}")
            main_headers = self._build_page_headers()
            if source_referer:
                main_headers["Referer"] = source_referer
            if source_origin:
                main_headers["Origin"] = source_origin

            main_html, main_url = await self._make_robust_request(
                url,
                headers=main_headers,
                timeout=15,
            )
            parsed_main = urlparse(main_url)
            main_origin = f"{parsed_main.scheme}://{parsed_main.netloc}"

            # Extract first iframe (src can appear in any attribute order)
            iframe_match = re.search(r'<iframe[^>]+(?<!data-)src=["\']([^"\']+)["\']', main_html, re.IGNORECASE)
            iframe_url = main_url
            iframe_html = main_html

            if iframe_match:
                iframe_url = self._normalize_stream_url(iframe_match.group(1), main_url)
                logger.info(f"Found iframe URL: {iframe_url}")

                candidates = [iframe_url]
                parsed_iframe = urlparse(iframe_url)
                if parsed_iframe.netloc.lower() == "gotdynamic.net":
                    candidates.extend([
                        parsed_iframe._replace(netloc="wgstream.sx").geturl(),
                        parsed_iframe._replace(netloc="www.wgstream.sx").geturl()
                    ])

                iframe_html = None
                for candidate_url in candidates:
                    # Step 2: Fetch iframe with source page as referer
                    iframe_headers = self._build_iframe_headers(main_url, candidate_url)
                    try:
                        iframe_html, active_iframe_url = await self._make_robust_request(candidate_url, headers=iframe_headers, timeout=15, retries=1)
                        iframe_url = active_iframe_url
                        logger.debug(f"Iframe HTML length: {len(iframe_html)}")
                        break
                    except Exception as e:
                        logger.warning(f"Failed candidate {candidate_url}: {e}")

                if not iframe_html:
                    raise ExtractorError("All iframe candidates failed (403 or connection errors).")
            else:
                logger.warning("No iframe found on page, attempting extraction from main HTML")

            parsed_iframe = urlparse(iframe_url)
            playback_headers = {
                "Referer": iframe_url,
                "Origin": f"{parsed_iframe.scheme}://{parsed_iframe.netloc}",
                "User-Agent": user_agent,
            }

            # Step 3: Detect packed blocks
            packed_blocks = self._detect_packed_blocks(iframe_html)

            logger.info(f"Found {len(packed_blocks)} packed blocks")

            if not packed_blocks:
                logger.warning("No packed blocks found, trying direct m3u8 search")
                # Fallback: try direct m3u8 search
                direct_match = self._extract_m3u8_candidate(iframe_html)
                if direct_match:
                    m3u8_url = self._normalize_stream_url(direct_match, iframe_url)
                    logger.info(f"Found direct m3u8 URL: {m3u8_url}")

                    return {
                        "destination_url": m3u8_url,
                        "request_headers": playback_headers,
                        "mediaflow_endpoint": self.mediaflow_endpoint,
                    }
                else:
                    raise ExtractorError("No packed blocks or direct m3u8 URL found")

            # Choose block: if >=2 use second (index 1), else first (index 0)
            chosen_idx = 1 if len(packed_blocks) > 1 else 0
            m3u8_url = None
            unpacked_code = None

            logger.info(f"Chosen packed block index: {chosen_idx}")

            # Try to unpack chosen block
            try:
                unpacked_code = extract_unpack(packed_blocks[chosen_idx])
                logger.info(f"Successfully unpacked block {chosen_idx}")
            except Exception as e:
                logger.warning(f"Failed to unpack block {chosen_idx}: {e}")

            # Search for var src="...m3u8" with multiple patterns
            if unpacked_code:
                m3u8_url = self._extract_m3u8_candidate(unpacked_code)

            # If not found, try all other blocks
            if not m3u8_url:
                logger.info("m3u8 not found in chosen block, trying all blocks")
                for i, block in enumerate(packed_blocks):
                    if i == chosen_idx:
                        continue
                    try:
                        unpacked_code = extract_unpack(block)
                        m3u8_url = self._extract_m3u8_candidate(unpacked_code)
                        if m3u8_url:
                            logger.info(f"Found m3u8 in block {i}")
                            break
                    except Exception as e:
                        logger.debug(f"Failed to process block {i}: {e}")
                        continue

            if not m3u8_url:
                fallback_candidate = self._extract_m3u8_candidate(iframe_html)
                if fallback_candidate:
                    m3u8_url = fallback_candidate

            if not m3u8_url:
                raise ExtractorError("Could not extract m3u8 URL from packed code")

            m3u8_url = self._normalize_stream_url(m3u8_url, iframe_url)

            logger.info(f"Successfully extracted m3u8 URL: {m3u8_url}")

            # Return stream configuration
            return {
                "destination_url": m3u8_url,
                "request_headers": playback_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        except ExtractorError:
            raise
        except Exception as e:
            logger.exception(f"Sportsonline extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None


def extract_unpack(packed_js):
    """
    Unpacker for P.A.C.K.E.R. packed javascript.
    """
    try:
        match = re.search(r"}\((.*)\)\)", packed_js)
        if not match:
            raise ValueError("Cannot find packed data.")

        p, a, c, k, e, d = eval(f"({match.group(1)})", {"__builtins__": {}}, {})
        return unpack(p, a, c, k, e, d)
    except Exception as e:
        raise ValueError(f"Failed to unpack JS: {e}")
