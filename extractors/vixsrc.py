import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
import cloudscraper
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from config import get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy

logger = logging.getLogger(__name__)

_FREE_PROXY_MAX_FETCH = int(os.environ.get("VIXSRC_FREE_PROXY_MAX_FETCH", "0"))
_FREE_PROXY_MAX_GOOD = int(os.environ.get("VIXSRC_FREE_PROXY_MAX_GOOD", "0"))

from utils.proxy_manager import FreeProxyManager




class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""


class VixSrcExtractor:
    """VixSrc URL extractor per risolvere link VixSrc."""
    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = self._default_headers()
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.proxies = proxies or GLOBAL_PROXIES
        self.is_vixsrc = True
        self.last_used_proxy = None
        self.proxy_manager = FreeProxyManager.get_instance(
            "vixsrc",
            [
                os.environ.get(
                    "VIXSRC_FREE_PROXY_URL",
                    "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/all/data.txt",
                ),
                "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
            ],
            cache_ttl=int(os.environ.get("VIXSRC_FREE_PROXY_CACHE_TTL", "7200")),
            max_fetch=_FREE_PROXY_MAX_FETCH,
            max_good=_FREE_PROXY_MAX_GOOD,
        )

    @staticmethod
    def _normalize_proxy_url(proxy_value: str) -> str:
        proxy_value = proxy_value.strip()
        if proxy_value.startswith("socks5://"):
            return proxy_value.replace("socks5://", "socks5h://", 1)
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    @staticmethod
    def _default_headers() -> dict:
        return {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
        }

    def _fresh_headers(self, **extra_headers) -> dict:
        headers = self._default_headers()
        headers.update(extra_headers)
        return headers

    @staticmethod
    def _normalize_base_site(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ExtractorError("Invalid VixSrc URL")
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    def _build_session_for_proxy(self, proxy: str | None) -> ClientSession:
        timeout = ClientTimeout(total=60, connect=30, sock_read=30)
        if proxy:
            logger.debug("Using proxy %s for VixSrc session.", proxy)
            connector = get_connector_for_proxy(proxy)
        else:
            connector = TCPConnector(
                limit=0,
                limit_per_host=0,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
                force_close=False,
                use_dns_cache=True,
            )
        return ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self._default_headers(),
            cookie_jar=aiohttp.CookieJar(),
        )

    async def _get_auto_proxy_pool(self, url: str, headers: dict) -> list[str]:
        if os.environ.get("VIXSRC_ENABLE_FREE_PROXY_POOL", "true").lower() != "true":
            return []

        def probe_sync(proxy_url: str) -> bool:
            try:
                scraper = cloudscraper.create_scraper(delay=2)
                resp = scraper.get(
                    url,
                    headers=headers or self._default_headers(),
                    timeout=6,
                    proxies={"http": proxy_url, "https": proxy_url},
                )
                return resp.status_code == 200 and len(resp.text) > 100
            except Exception:
                return False

        return await self.proxy_manager.get_next_sequence(probe_sync)

    @staticmethod
    def _raise_if_embed_expired(url: str):
        parsed = urlparse(url)
        if "/embed/" not in parsed.path:
            return
        expires = parse_qs(parsed.query).get("expires", [None])[0]
        if not expires:
            return
        try:
            expires_ts = int(expires)
        except (TypeError, ValueError):
            return
        now_ts = int(time.time())
        if expires_ts <= now_ts:
            raise ExtractorError(
                f"Expired VixSrc embed URL (expired at {expires_ts}, current {now_ts}). "
                "Use the original /movie/ or /tv/ URL to refresh tokens."
            )

    async def _get_session(self, url: str = None):
        """Ottiene una sessione HTTP persistente."""
        if self.session is None or self.session.closed:
            proxy = None
            if url:
                proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies)
            else:
                proxy = self._get_random_proxy()
            if proxy:
                proxy = self._normalize_proxy_url(proxy)
                self.last_used_proxy = proxy
            self.session = self._build_session_for_proxy(proxy)
        return self.session

    async def _make_robust_request(
        self, url: str, headers: dict = None, retries: int = 3, initial_delay: int = 2
    ):
        """Effettua richieste HTTP robuste con retry automatico."""
        final_headers = headers or {}

        for attempt in range(retries):
            try:
                session = await self._get_session(url)
                logger.info("Attempt %s/%s for URL: %s", attempt + 1, retries, url)

                async with session.get(url, headers=final_headers) as response:
                    response.raise_for_status()
                    content = await response.text()

                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict, response_url):
                            self._text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = response_url
                            self.status_code = status
                            self.text = text_content

                        async def text_async(self):
                            return self._text

                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status,
                                )

                    logger.info("Request successful for %s at attempt %s", url, attempt + 1)
                    return MockResponse(content, response.status, response.headers, response.url)

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError,
            ) as e:
                logger.warning(
                    "Connection error attempt %s for %s: %s", attempt + 1, url, str(e)
                )

                if attempt == retries - 1:
                    if self.session and not self.session.closed:
                        try:
                            await self.session.close()
                        except Exception:
                            pass
                        self.session = None

                if attempt < retries - 1:
                    delay = initial_delay * (2**attempt)
                    logger.info("Waiting %s seconds before next attempt...", delay)
                    await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All {retries} attempts failed for {url}: {str(e)}")

            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    raise ExtractorError(f"VixSrc content not found (404): {url}")
                if e.status == 403 and attempt == 0:
                    logger.warning("VixSrc direct request returned 403 for %s, trying free proxy fallback", url)
                    if self.session and not self.session.closed:
                        try:
                            await self.session.close()
                        except Exception:
                            pass
                        self.session = None

                    for proxy_url in await self._get_auto_proxy_pool(url, final_headers):
                        logger.info("VixSrc: retrying with auto proxy %s", proxy_url)
                        temp_session = None
                        try:
                            temp_session = self._build_session_for_proxy(proxy_url)
                            async with temp_session.get(url, headers=final_headers) as response:
                                response.raise_for_status()
                                content = await response.text()

                                class MockResponse:
                                    def __init__(self, text_content, status, headers_dict, response_url):
                                        self._text = text_content
                                        self.status = status
                                        self.headers = headers_dict
                                        self.url = response_url
                                        self.status_code = status
                                        self.text = text_content

                                self.session = temp_session
                                self.last_used_proxy = proxy_url
                                logger.info("VixSrc: free proxy fallback succeeded with %s", proxy_url)
                                return MockResponse(content, response.status, response.headers, response.url)
                        except Exception as proxy_exc:
                            logger.warning("VixSrc: auto proxy %s failed: %s", proxy_url, proxy_exc)
                            self.proxy_manager.report_failure(proxy_url)
                        finally:
                            if temp_session and temp_session is not self.session and not temp_session.closed:
                                await temp_session.close()

                if attempt == retries - 1:
                    raise ExtractorError(f"Final HTTP error {e.status} for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

            except Exception as e:
                logger.error("Non-network error attempt %s for %s: %s", attempt + 1, url, str(e))
                if attempt == retries - 1:
                    raise ExtractorError(f"Final error for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

    async def _parse_html_simple(self, html_content: str, tag: str, attrs: dict = None):
        """Parser HTML semplificato senza BeautifulSoup."""
        try:
            if tag == "div" and attrs and attrs.get("id") == "app":
                pattern = r'<div[^>]*id="app"[^>]*data-page="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"data-page": match.group(1)}

            elif tag == "iframe":
                pattern = r'<iframe[^>]*src="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"src": match.group(1)}

            elif tag == "script":
                scripts = re.findall(
                    r"<script[^>]*>(.*?)</script>",
                    html_content,
                    re.DOTALL | re.IGNORECASE,
                )
                for script in scripts:
                    if "window.masterPlaylist" in script or "'token':" in script:
                        return script

                pattern = r"<body[^>]*>.*?<script[^>]*>(.*?)</script>"
                match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(1)

        except Exception as e:
            logger.error("HTML parsing error: %s", e)

        return None

    async def _resolve_embed_url_from_api(self, url: str) -> str | None:
        """Resolve the current embed URL through VixSrc JSON API."""
        parsed = urlparse(url)
        site_url = self._normalize_base_site(url)
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]

        api_url = None
        if len(path_parts) >= 2 and path_parts[0] == "movie":
            api_url = f"{site_url}/api/movie/{path_parts[1]}"
        elif len(path_parts) >= 4 and path_parts[0] == "tv":
            api_url = f"{site_url}/api/tv/{path_parts[1]}/{path_parts[2]}/{path_parts[3]}"

        if not api_url:
            return None

        response = await self._make_robust_request(
            api_url,
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": url,
                **self._default_headers(),
            },
        )

        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ExtractorError(f"Invalid API response from {api_url}: {exc}")

        embed_path = payload.get("src")
        if not embed_path:
            raise ExtractorError(f"Missing embed src in API response from {api_url}")

        return urljoin(site_url, embed_path)

    def _extract_playlist_from_embed(self, script_content: str) -> str:
        """Extract playlist URL from current embed structure, with legacy fallback."""
        master_playlist_match = re.search(
            r"window\.masterPlaylist\s*=\s*\{.*?params\s*:\s*\{(?P<params>.*?)\}\s*,\s*url\s*:\s*['\"](?P<url>[^'\"]+)['\"]",
            script_content,
            re.DOTALL,
        )
        if master_playlist_match:
            params_block = master_playlist_match.group("params")
            playlist_url = master_playlist_match.group("url").replace("\\/", "/")

            token_match = re.search(
                r"['\"]token['\"]\s*:\s*['\"]([^'\"]+)['\"]", params_block
            )
            expires_match = re.search(
                r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", params_block
            )
            asn_match = re.search(
                r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", params_block
            )

            if token_match and expires_match:
                parsed_playlist_url = urlparse(playlist_url)
                query_params = parse_qsl(parsed_playlist_url.query, keep_blank_values=True)
                query_params.extend(
                    [
                        ("token", token_match.group(1)),
                        ("expires", expires_match.group(1)),
                    ]
                )
                if "window.canPlayFHD = true" in script_content or "canPlayFHD" in script_content:
                    query_params.append(("h", "1"))
                query_params.append(("lang", "it"))
                if asn_match and asn_match.group(1):
                    query_params.append(("asn", asn_match.group(1)))
                return urlunparse(parsed_playlist_url._replace(query=urlencode(query_params)))

        token_match = re.search(r"['\"]token['\"]\s*:\s*['\"](\w+)['\"]", script_content)
        expires_match = re.search(r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", script_content)
        server_url_match = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", script_content)

        if not all([token_match, expires_match, server_url_match]):
            token_match = token_match or re.search(
                r"token['\"]\s*:\s*['\"]([^'\"]+)['\"]", script_content
            )
            expires_match = expires_match or re.search(
                r"expires['\"]\s*:\s*['\"](\d+)['\"]", script_content
            )

        if not all([token_match, expires_match, server_url_match]):
            raise ExtractorError("Missing mandatory parameters in JS script (token/expires/url)")

        server_url = server_url_match.group(1).replace("\\/", "/")
        parsed_server_url = urlparse(server_url)
        query_params = parse_qsl(parsed_server_url.query, keep_blank_values=True)
        query_params.extend(
            [
                ("token", token_match.group(1)),
                ("expires", expires_match.group(1)),
            ]
        )

        if "window.canPlayFHD = true" in script_content or "canPlayFHD" in script_content:
            query_params.append(("h", "1"))

        query_params.append(("lang", "it"))
        asn_match = re.search(r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", script_content)
        if asn_match and asn_match.group(1):
            query_params.append(("asn", asn_match.group(1)))

        return urlunparse(parsed_server_url._replace(query=urlencode(query_params)))

    async def version(self, site_url: str) -> str:
        """Ottiene la versione del sito VixSrc parent."""
        base_url = f"{site_url}/request-a-title"

        response = await self._make_robust_request(
            base_url,
            headers={
                "Referer": f"{site_url}/",
                "Origin": f"{site_url}",
                **self._default_headers(),
            },
        )

        if response.status_code != 200:
            raise ExtractorError("Obsolete URL")

        app_div = await self._parse_html_simple(response.text, "div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            try:
                data_page = app_div["data-page"].replace("&quot;", '"')
                data = json.loads(data_page)
                return data["version"]
            except (KeyError, json.JSONDecodeError, AttributeError) as e:
                raise ExtractorError(f"Version parsing failure: {e}")

        raise ExtractorError("Unable to find version data")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Estrae URL VixSrc."""
        try:
            parsed_url = urlparse(url)
            response = None

            if "/playlist/" in parsed_url.path:
                logger.info("URL is already a VixSrc manifest, no extraction required.")
                # Preserve selected_proxy from query if present
                selected_proxy = kwargs.get("proxy") or parse_qs(parsed_url.query).get("proxy", [None])[0]
                logger.debug(f"Extractor Debug: Extractor result selected_proxy: {selected_proxy}")
                return {
                    "destination_url": url,
                    "request_headers": self._fresh_headers(),
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "selected_proxy": selected_proxy or self.last_used_proxy,
                }

            if "/embed/" in parsed_url.path:
                self._raise_if_embed_expired(url)
                response = await self._make_robust_request(
                    url,
                    headers=self._fresh_headers(
                        referer=self._normalize_base_site(url) + "/"
                    ),
                )
            elif "iframe" in url:
                site_url = url.split("/iframe")[0]
                version = await self.version(site_url)
                response = await self._make_robust_request(
                    url,
                    headers=self._fresh_headers(
                        **{"x-inertia": "true", "x-inertia-version": version}
                    ),
                )

                iframe_data = await self._parse_html_simple(response.text, "iframe")
                if iframe_data and iframe_data.get("src"):
                    iframe_url = iframe_data["src"]
                    response = await self._make_robust_request(
                        iframe_url,
                        headers=self._fresh_headers(
                            **{"x-inertia": "true", "x-inertia-version": version}
                        ),
                    )
                else:
                    raise ExtractorError("No iframe found in response")
            elif "/movie/" in parsed_url.path or "/tv/" in parsed_url.path:
                embed_url = await self._resolve_embed_url_from_api(url)
                if embed_url:
                    response = await self._make_robust_request(
                        embed_url,
                        headers=self._fresh_headers(referer=url),
                    )
                else:
                    response = await self._make_robust_request(url)
            else:
                raise ExtractorError("Unsupported VixSrc URL type")

            if response.status_code != 200:
                raise ExtractorError("URL component extraction failed, invalid request")

            script_content = await self._parse_html_simple(response.text, "script")
            if not script_content:
                raise ExtractorError("No script found in body")

            try:
                final_url = self._extract_playlist_from_embed(script_content)
                stream_headers = self._fresh_headers(Referer=url)

                logger.info("VixSrc URL extracted successfully: %s", final_url)
                return {
                    "destination_url": final_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                    "selected_proxy": self.last_used_proxy,
                }
            except Exception as e:
                raise ExtractorError(f"JavaScript script parsing error: {e}")

        except Exception as e:
            logger.error("VixSrc extraction failed: %s", str(e))
            raise ExtractorError(f"VixSrc extraction completely failed: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None
