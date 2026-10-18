import base64
import codecs
import json
import logging
import re
from urllib.parse import urljoin
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

class VoeExtractor(BaseExtractor):
    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="voe", bypass_warp=bypass_warp)

    async def _fetch_external_scripts(self, url: str, text: str) -> list[str]:
        """Fetch all external JS files linked in the page and return their content."""
        results = []
        script_srcs = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', text)
        for src in script_srcs:
            try:
                script_url = urljoin(url, src)
                resp = await self._make_request(script_url)
                results.append(resp.text)
            except Exception as e:
                logger.debug(f"VOE: failed to fetch external script {src}: {e}")
        return results

    def _build_result(self, final_url: str, referer_url: str) -> dict:
        headers = dict(self.base_headers)
        headers["referer"] = referer_url
        return self._result(final_url, headers, mediaflow_endpoint="hls_proxy")

    async def _try_extract_from_texts(self, url: str, texts: list[str]) -> dict | None:
        for combined_text in texts:
            # 2. Try Method 8 (Obfuscated JSON inside <script type="application/json">)
            json_matches = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', combined_text, re.DOTALL)
            simple_json_match = re.search(r'json">(\[[^\]]+\])</script>', combined_text)
            if simple_json_match:
                json_matches.append(simple_json_match.group(1))

            for json_str in json_matches:
                result = self._deobfuscate_method8(json_str.strip())
                if result:
                    final_url = result.get('source') or result.get('direct_access_url') or result.get('file')
                    if final_url:
                        logger.info("VOE: successfully extracted URL via Method 8")
                        return self._build_result(final_url, url)

            # 3. Try Method 7 (MKGMa)
            mkgma_match = re.search(r'MKGMa="([^"]+)"', combined_text)
            if mkgma_match:
                result = self._deobfuscate_method7(mkgma_match.group(1))
                if result:
                    final_url = result.get('source') or result.get('direct_access_url') or result.get('file')
                    if final_url:
                        logger.info("VOE: successfully extracted URL via Method 7")
                        return self._build_result(final_url, url)

            # 4. Try Method 6 (a168c)
            a168c_match = re.search(r"a168c\s*=\s*'([^']+)'", combined_text)
            if a168c_match:
                result = self._deobfuscate_method6(a168c_match.group(1))
                if result:
                    final_url = result.get('source') or result.get('direct_access_url') or result.get('file')
                    if final_url:
                        logger.info("VOE: successfully extracted URL via Method 6")
                        return self._build_result(final_url, url)

            # 5. Legacy Obfuscated (using external script for LUTs)
            code_and_script_pattern = r'json">\["([^"]+)"]</script>\s*<script\s*src="([^"]+)'
            code_and_script_match = re.search(code_and_script_pattern, combined_text, re.DOTALL)
            if code_and_script_match:
                try:
                    return await self._extract_obfuscated(url, combined_text, code_and_script_match)
                except Exception as e:
                    logger.warning(f"VOE: legacy obfuscated extraction failed: {e}")

        # 6. Direct extractors fallback (search all collected text)
        for combined_text in texts:
            m = re.search(r"var\s+source\s*=\s*'([^']+)'", combined_text)
            if m:
                return self._build_result(m.group(1), url)
            m = re.search(r"""hls['"]:\s*['"]([^'"]+)""", combined_text)
            if m:
                return self._build_result(m.group(1), url)

        return None

    async def extract(self, url: str, redirect_count: int = 0, **kwargs) -> dict:
        self._apply_routing_kwargs(url, kwargs)
        resp = await self._make_request(url)
        text = resp.text

        # 1. Handle JS redirects
        redirect_patterns = [
            r'''window\.location\.href\s*=\s*'([^']+)''',
            r'''window\.location\s*=\s*'([^']+)''',
            r'''location\.href\s*=\s*'([^']+)''',
            r'''window\.location\.replace\('([^']+)'\)''',
            r'''window\.location\.assign\('([^']+)'\)''',
            r'''window\.location\s*=\s*"([^"]+)''',
            r'''window\.location\.href\s*=\s*"([^"]+)'''
        ]
        for pat in redirect_patterns:
            redirect_match = re.search(pat, text)
            if redirect_match:
                if redirect_count >= 5:
                    raise ExtractorError("VOE: too many redirects")
                redirect_url = urljoin(url, redirect_match.group(1))
                logger.info(f"VOE: redirecting to {redirect_url}")
                return await self.extract(redirect_url, redirect_count=redirect_count + 1, **kwargs)

        res = await self._try_extract_from_texts(url, [text])
        if res:
            return res

        ext_texts = await self._fetch_external_scripts(url, text)
        if ext_texts:
            res = await self._try_extract_from_texts(url, ext_texts)
            if res:
                return res

        logger.warning(f"VOE: no pattern matched for {url}")
        raise ExtractorError(f"VOE: unable to locate obfuscated payload or external script URL [{url}]")

    async def _extract_obfuscated(self, url: str, text: str, match: re.Match) -> dict:
        script_url = urljoin(url, match.group(2))
        resp_script = await self._make_request(script_url)
        script_text = resp_script.text

        luts_pattern = r"(\[(?:'\W{2}'[,\]]){1,9})"
        luts_match = re.search(luts_pattern, script_text, re.DOTALL)
        if not luts_match:
            raise ExtractorError("VOE: unable to locate LUTs in external script")

        data = self.voe_decode(match.group(1), luts_match.group(1))

        final_url = data.get('source')
        if not final_url:
            raise ExtractorError("VOE: failed to extract video URL")

        return self._build_result(final_url, url)

    @staticmethod
    def _rot13(text: str) -> str:
        return codecs.decode(text, "rot_13")

    @staticmethod
    def _safe_b64_decode(s: str) -> str:
        s = s.replace('\\', '')
        pad = len(s) % 4
        if pad:
            s += '=' * (4 - pad)
        try:
            return base64.b64decode(s).decode('utf-8', errors='replace')
        except Exception:
            return ""

    @staticmethod
    def _shift_chars(text: str, shift: int) -> str:
        return ''.join(chr(ord(c) - shift) for c in text)

    def _deobfuscate_method8(self, raw_json: str) -> dict:
        try:
            arr = json.loads(raw_json)
            if not (isinstance(arr, list) and arr and isinstance(arr[0], str)):
                return {}
            obf = arr[0]
        except Exception:
            if raw_json.startswith('["') and raw_json.endswith('"]'):
                obf = raw_json[2:-2]
            else:
                return {}

        try:
            step1 = self._rot13(obf)
            step2 = step1
            for pat in ['@$', '^^', '~@', '%?', '*~', '!!', '#&']:
                step2 = step2.replace(pat, '')
            step3 = self._safe_b64_decode(step2)
            step4 = self._shift_chars(step3, 3)
            step5 = step4[::-1]
            step6 = self._safe_b64_decode(step5)
            return json.loads(step6)
        except Exception as e:
            logger.debug(f"VOE: deobfuscate Method 8 failed: {e}")
            return {}

    def _deobfuscate_method7(self, raw_mkgma: str) -> dict:
        try:
            step1 = self._rot13(raw_mkgma)
            step2 = step1.replace('_', '')
            step3 = self._safe_b64_decode(step2)
            step4 = self._shift_chars(step3, 3)
            step5 = step4[::-1]
            step6 = self._safe_b64_decode(step5)
            return json.loads(step6)
        except Exception as e:
            logger.debug(f"VOE: deobfuscate Method 7 failed: {e}")
            return {}

    def _deobfuscate_method6(self, raw_base64: str) -> dict:
        try:
            cleaned = re.sub(r'\s+', '', raw_base64)
            decoded = self._safe_b64_decode(cleaned)[::-1]
            return json.loads(decoded)
        except Exception as e:
            logger.debug(f"VOE: deobfuscate Method 6 failed: {e}")
            return {}

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

