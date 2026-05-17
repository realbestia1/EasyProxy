import base64
import logging
import re
import urllib.parse
from urllib.parse import urljoin, urlparse

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)


class Sports99Extractor(BaseExtractor):
    """Sports99 / CDNLiveTV URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="sports99")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Sports99 stream URL."""
        logger.info("[Sports99] Extracting from: %s", url)

        entry = "https://streamsports99.su"
        player_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": f"{entry}/",
            "Origin": entry,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
        }
        stream_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": "https://cdnlivetv.tv/",
            "Origin": "https://cdnlivetv.tv",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
        }

        try:
            # 1. Fetch the player page
            resp = await self._make_request(url, headers=player_headers)
            html = resp.text

            # 2. Extract packed script arguments
            # Pattern: ("hsbxhAs...", 94, "bthAsVdxv", 21, 7, 41)
            match = re.search(r'\("([^"]+)"\s*,\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)', html)

            if not match:
                # Fallback: check if already unpacked in HTML
                if "playlist.m3u8" in html:
                    m3u8_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html)
                    if m3u8_match:
                        logger.info("[Sports99] Found direct URL in HTML")
                        return {
                            "destination_url": m3u8_match.group(1),
                            "request_headers": stream_headers,
                            "mediaflow_endpoint": self.mediaflow_endpoint,
                        }
                raise ExtractorError("SPORTS99: Packed script not found")

            h_str, u, n_str, t, e, _ = match.groups()
            u, t, e = int(u), int(t), int(e)

            # 3. Deobfuscate using the identified algorithm
            unpacked_js = self._unpack(h_str, u, n_str, t, e)

            # 4. Extract and reconstruct the stream URL
            stream_url = self._extract_url_from_js(unpacked_js)
            if not stream_url:
                # Try simple regex on unpacked JS just in case
                m3u8_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', unpacked_js)
                if m3u8_match:
                    stream_url = m3u8_match.group(1)
                else:
                    raise ExtractorError("SPORTS99: Failed to extract stream URL from unpacked JS")

            logger.info("[Sports99] Successfully extracted: %s", stream_url)

            return {
                "destination_url": stream_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        except Exception as err:
            logger.error("[Sports99] Extraction error: %s", err)
            raise ExtractorError(f"SPORTS99: {str(err)}")

    def _unpack(self, h, u, n, t, e):
        """Python implementation of the custom JS unpacker."""
        try:
            sep = n[e]
            # Create replacement map from string n
            repl_map = {n[j]: str(j) for j in range(len(n))}

            result = ""
            parts = h.split(sep)
            for s in parts:
                if not s:
                    continue

                # Replace markers with their numeric indices
                temp_s = s
                for char, val in repl_map.items():
                    temp_s = temp_s.replace(char, val)

                # Base conversion (base e to decimal)
                try:
                    num = int(temp_s, e)
                    result += chr(num - t)
                except ValueError:
                    continue

            # Handle potential double encoding (decodeURIComponent(escape(r)))
            try:
                return urllib.parse.unquote(result.encode('latin-1').decode('utf-8', errors='ignore'))
            except:
                return result
        except Exception as err:
            logger.error("[Sports99] Unpack error: %s", err)
            return ""

    def _extract_url_from_js(self, js_code):
        """Extracts and joins URL components from the deobfuscated JS."""
        try:
            # Find all constant declarations
            consts = dict(re.findall(r"const\s+([a-zA-Z0-9_]+)\s*=\s*'([^']+)';", js_code))

            def modified_atob(s):
                """Helper to decode base64 with potential URL-safe characters."""
                s = s.replace('-', '+').replace('_', '/')
                while len(s) % 4:
                    s += '='
                try:
                    decoded = base64.b64decode(s)
                    try:
                        return decoded.decode('utf-8')
                    except:
                        return decoded.decode('latin-1')
                except:
                    return s

            # Find construction lines like: const HaLKeS... = decodeFunc(Var1) + decodeFunc(Var2) + ...
            # The JS uses dynamic function names for decodeFunc, but they always take one argument.
            construction_lines = re.findall(
                r"const\s+([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_]+\([a-zA-Z0-9_]+\)(?:\s*\+\s*[a-zA-Z0-9_]+\([a-zA-Z0-9_]+\))*);",
                js_code
            )

            for var_name, expression in construction_lines:
                # Extract variables inside parentheses
                parts = re.findall(r"\(([a-zA-Z0-9_]+)\)", expression)
                decoded_parts = [modified_atob(consts.get(p, "")) for p in parts]
                full_url = "".join(decoded_parts)

                if "playlist.m3u8" in full_url and "token=" in full_url:
                    return full_url

            return None
        except Exception as err:
            logger.error("[Sports99] URL extraction error: %s", err)
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
