import base64
import yarl
import json
import logging
import random
import re
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy, get_solver_proxy_url
from utils.smart_request import smart_request

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Exception for extraction errors."""
    pass

class CinemaCityExtractor:
    """CinemaCity m3u8 extractor (Direct URL only)."""

    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.proxies = proxies or GLOBAL_PROXIES
        self.session = None
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        self.base_url = "https://cinemacity.cc"
        self.flaresolverr_url = FLARESOLVERR_URL
        self.flaresolverr_timeout = FLARESOLVERR_TIMEOUT

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = get_proxy_for_url(
                self.base_url, TRANSPORT_ROUTES, self.proxies, bypass_warp=True
            )
            if proxy:
                logger.debug("CinemaCity routing: PROXY (%s)", proxy)
            else:
                logger.debug("CinemaCity routing: DIRECT (WARP excluded host / real IP)")
            connector = get_connector_for_proxy(proxy) if proxy else TCPConnector(limit=0, use_dns_cache=True)
            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.user_agent})
        return self.session

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None) -> dict:
        """Performs a request via FlareSolverr."""
        if not self.flaresolverr_url:
            raise ExtractorError("FlareSolverr URL not configured")

        endpoint = f"{self.flaresolverr_url.rstrip('/')}/v1"
        payload = {
            "cmd": cmd,
            "maxTimeout": (self.flaresolverr_timeout + 60) * 1000,
        }
        fs_headers = {}
        if url: 
            payload["url"] = url
            # Determina dinamicamente il proxy per questo specifico URL
            proxy = get_proxy_for_url(
                url, TRANSPORT_ROUTES, self.proxies, bypass_warp=True
            )
            if proxy:
                payload["proxy"] = {"url": proxy}
                solver_proxy = get_solver_proxy_url(proxy)
                fs_headers["X-Proxy-Server"] = solver_proxy
                logger.debug(f"CinemaCity: Passing explicit proxy to solver: {solver_proxy}")

        if post_data: payload["postData"] = post_data

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=fs_headers,
                    timeout=aiohttp.ClientTimeout(total=self.flaresolverr_timeout + 95),
                ) as resp:
                    if resp.status != 200:
                        raise ExtractorError(f"FlareSolverr HTTP {resp.status}")
                    data = await resp.json()
            except Exception as e:
                logger.error(f"CinemaCity: FlareSolverr request failed ({cmd}): {e}")
                raise ExtractorError(f"FlareSolverr bypass failed: {e}")

        if data.get("status") != "ok":
            raise ExtractorError(f"FlareSolverr ({cmd}): {data.get('message', 'unknown error')}")
        
        return data

    def base64_decode(self, data: str) -> str:
        try:
            missing_padding = len(data) % 4
            if missing_padding: data += '=' * (4 - missing_padding)
            decoded_bytes = base64.b64decode(data)
            try: return decoded_bytes.decode('utf-8')
            except: return decoded_bytes.decode('latin-1')
        except: return ""

    def get_session_cookies(self) -> str:
        # Fixed login cookies
        return self.base64_decode("ZGxlX3VzZXJfaWQ9MzI3Mjk7IGRsZV9wYXNzd29yZD04OTQxNzFjNmE4ZGFiMThlZTU5NGQ1YzY1MjAwOWEzNTs=")

    def extract_json_array(self, decoded: str) -> Optional[str]:
        start = decoded.find("file:")
        if start == -1: start = decoded.find("sources:")
        if start == -1: return None
        start = decoded.find("[", start)
        if start == -1: return None
        depth = 0
        for i in range(start, len(decoded)):
            if decoded[i] == "[": depth += 1
            elif decoded[i] == "]": depth -= 1
            if depth == 0: return decoded[start:i+1]
        return None

    def _collect_file_entries(self, items) -> list[dict]:
        entries = []
        if isinstance(items, list):
            for item in items:
                entries.extend(self._collect_file_entries(item))
        elif isinstance(items, dict):
            if isinstance(items.get("file"), str) and items.get("file"):
                entries.append(items)
            folder = items.get("folder")
            if isinstance(folder, list):
                entries.extend(self._collect_file_entries(folder))
        return entries

    def pick_stream(self, file_data, media_type: str, season: int = 1, episode: int = 1) -> Optional[str]:
        if isinstance(file_data, str): return file_data
        if isinstance(file_data, list):
            # Movie or flat list
            if media_type == 'movie' or all(isinstance(x, dict) and "file" in x and "folder" not in x for x in file_data):
                return file_data[0].get('file') if file_data else None

            # Series (Season -> Episode)
            selected_season = None
            for s in file_data:
                if not isinstance(s, dict) or "folder" not in s: continue
                title = s.get('title', "").lower()
                if re.search(rf"(?:season|stagione|s)\s*0*{season}\b", title, re.I):
                    selected_season = s['folder']
                    break
            if not selected_season and file_data:
                for s in file_data:
                    if isinstance(s, dict) and "folder" in s:
                        selected_season = s['folder']
                        break
            if not selected_season: return None

            selected_ep = None
            episode_candidates = self._collect_file_entries(selected_season)
            for e in episode_candidates:
                if not isinstance(e, dict) or "file" not in e: continue
                title = e.get('title', "").lower()
                if re.search(rf"(?:episode|episodio|e)\s*0*{episode}\b", title, re.I):
                    selected_ep = e['file']
                    break
            if not selected_ep:
                idx = max(0, int(episode) - 1)
                if episode_candidates:
                    ep_data = episode_candidates[idx] if idx < len(episode_candidates) else episode_candidates[0]
                    selected_ep = ep_data.get('file')
            
            if selected_ep:
                logger.debug(f"CinemaCity: Selected S{season}E{episode} -> {selected_ep[:50]}...")
            else:
                logger.warning(f"CinemaCity: Failed to find S{season}E{episode} in file_data")
            
            return selected_ep
        return None

    async def extract(self, url: str, **kwargs) -> dict:
        session = await self._get_session()
        cookies = self.get_session_cookies()
        
        # Get params from kwargs or infer from URL/query
        media_type = kwargs.get('type')
        if not media_type:
            lowered_url = url.lower()
            if "/tv-series/" in lowered_url or "/serie-tv/" in lowered_url:
                media_type = "series"
            else:
                media_type = "movie"
        
        # Try to extract s/e from URL if not in kwargs
        url_params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        
        s_val = kwargs.get('s') or kwargs.get('season') or url_params.get('s', [None])[0] or url_params.get('season', ['1'])[0]
        e_val = kwargs.get('e') or kwargs.get('episode') or url_params.get('e', [None])[0] or url_params.get('episode', ['1'])[0]
        
        season = int(s_val) if str(s_val).isdigit() else 1
        episode = int(e_val) if str(e_val).isdigit() else 1
        if media_type == "movie" and (url_params.get('s') or url_params.get('season') or url_params.get('e') or url_params.get('episode')):
            media_type = "series"

        headers = {
            "User-Agent": self.user_agent,
            "Cookie": cookies,
            "Referer": f"{self.base_url}/",
            "X-Requested-With": "XMLHttpRequest"
        }

        # Use SmartRequest (Direct or FlareSolverr fallback)
        result = await smart_request(
            "request.get",
            url,
            headers=headers,
            proxies=self.proxies,
            bypass_warp=True,
        )
        html = result.get("html", "")
        dynamic_cookies = result.get("cookies", {})

        # Find player for referer
        if not html:
            raise ExtractorError("Failed to retrieve page content (SmartRequest failed)")

        # Find player for referer
        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']*player\.php[^"\']*)["\']', html, re.I)
        player_referer = urllib.parse.urljoin(url, iframe_match.group(1)) if iframe_match else url

        # Scrape atob chunks
        file_data = None
        for match in re.finditer(r'atob\s*\(\s*["\'](.*?)["\']\s*\)', html, re.I):
            encoded = match.group(1)
            if len(encoded) < 50: continue
            decoded = self.base64_decode(encoded)
            if not decoded: continue
            
            if decoded.strip().startswith("["):
                try:
                    file_data = json.loads(decoded)
                    if file_data: break
                except json.JSONDecodeError:
                    pass
            
            raw_json = self.extract_json_array(decoded)
            if raw_json:
                try:
                    clean = re.sub(r'\\(.)', r'\1', raw_json)
                    file_data = json.loads(clean)
                except json.JSONDecodeError:
                    try:
                        file_data = json.loads(raw_json)
                    except json.JSONDecodeError:
                        pass
                if file_data: break
            
            file_match = re.search(r'(?:file|sources)\s*:\s*["\'](.*?)["\']', decoded, re.I)
            if file_match:
                f_url = file_match.group(1)
                if '.m3u8' in f_url or '.mp4' in f_url:
                    file_data = f_url
                    break

        if not file_data: raise ExtractorError("Stream not found")
        stream_url = self.pick_stream(file_data, media_type, season, episode)
        if not stream_url: raise ExtractorError("Pick failed")

        safe_url = str(yarl.URL(stream_url, encoded=True))
        
        # ✅ FIX: Unisci i cookie di sessione con quelli dinamici (Cloudflare/PHPSESSID)
        merged_cookies = {}
        # 1. Carica i cookie di sessione base
        for c in cookies.split(";"):
            if "=" in c:
                k, v = c.strip().split("=", 1)
                merged_cookies[k] = v
        
        # 2. Sovrascrivi/Aggiungi quelli dinamici
        if dynamic_cookies:
            merged_cookies.update(dynamic_cookies)
            
        clean_cookies = "; ".join([f"{k}={v}" for k, v in merged_cookies.items()])
        
        # Standard cookies don't strictly require a trailing semicolon
        clean_cookies = clean_cookies.strip().rstrip(';')

        # mediaflow_endpoint will determine how to handle the stream
        # Use player_referer (player.php iframe URL) — CDN validates this, not the page URL
        try:
            origin = urllib.parse.urlparse(url)
            origin_str = f"{origin.scheme}://{origin.netloc}"
        except Exception:
            origin_str = self.base_url

        return {
            "destination_url": safe_url,
            "request_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": player_referer,
                "Origin": origin_str,
                "Cookie": clean_cookies,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive"
            },
            "mediaflow_endpoint": "hls_manifest_proxy" if ".m3u8" in safe_url else "proxy_stream_endpoint"
        }

    async def close(self):
        if self.session: await self.session.close()
