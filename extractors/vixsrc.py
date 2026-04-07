import asyncio
import logging
import re
import json
from urllib.parse import urlparse
from typing import Dict, Any
import random
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""
    pass

class VixSrcExtractor:
    """VixSrc URL extractor per risolvere link VixSrc."""
    
    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.proxies = proxies or []
        self.is_vixsrc = True # Flag per identificare questo estrattore

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        """Ottiene una sessione HTTP persistente."""
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = self._get_random_proxy()
            if proxy:
                logger.info(f"Using proxy {proxy} for VixSrc session.")
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector(
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=30,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True
                )
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar()
            )
        return self.session

    async def _make_robust_request(self, url: str, headers: dict = None, retries=3, initial_delay=2):
        """Effettua richieste HTTP robuste con retry automatico."""
        final_headers = headers or {}
        
        for attempt in range(retries):
            try:
                session = await self._get_session()
                logger.info(f"Attempt {attempt + 1}/{retries} for URL: {url}")
                
                async with session.get(url, headers=final_headers) as response:
                    response.raise_for_status()
                    content = await response.text()
                    
                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict, url):
                            self._text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = url
                            self.status_code = status
                            self.text = text_content
                        
                        async def text_async(self):
                            return self._text
                        
                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status
                                )
                    
                    logger.info(f"✅ Request successful for {url} at attempt {attempt + 1}")
                    return MockResponse(content, response.status, response.headers, response.url)
                    
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError
            ) as e:
                logger.warning(f"⚠️ Connection error attempt {attempt + 1} for {url}: {str(e)}")
                
                if attempt == retries - 1:
                    if self.session and not self.session.closed:
                        try:
                            await self.session.close()
                        except:
                            pass
                        self.session = None
                
                if attempt < retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.info(f"⏳ Waiting {delay} seconds before next attempt...")
                    await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All {retries} attempts failed for {url}: {str(e)}")
                    
            except Exception as e:
                logger.error(f"❌ Non-network error attempt {attempt + 1} for {url}: {str(e)}")
                if attempt == retries - 1:
                    raise ExtractorError(f"Final error for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

    async def _parse_html_simple(self, html_content: str, tag: str, attrs: dict = None):
        """Parser HTML semplificato senza BeautifulSoup."""
        try:
            if tag == "div" and attrs and attrs.get("id") == "app":
                # Cerca div con id="app"
                pattern = r'<div[^>]*id="app"[^>]*data-page="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"data-page": match.group(1)}
                    
            elif tag == "iframe":
                # Cerca iframe src
                pattern = r'<iframe[^>]*src="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"src": match.group(1)}
                    
            elif tag == "script":
                # Cerca TUTTI gli script tag e restituisce quello che contiene window.masterPlaylist
                scripts = re.findall(r'<script[^>]*>(.*?)</script>', html_content, re.DOTALL | re.IGNORECASE)
                for s in scripts:
                    if "window.masterPlaylist" in s or "'token':" in s:
                        return s
                
                # Fallback: primo script nel body (vecchia logica)
                pattern = r'<body[^>]*>.*?<script[^>]*>(.*?)</script>'
                match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(1)
                    
        except Exception as e:
            logger.error(f"HTML parsing error: {e}")
            
        return None

    async def version(self, site_url: str) -> str:
        """Ottiene la versione del sito VixSrc parent."""
        base_url = f"{site_url}/request-a-title"
        
        response = await self._make_robust_request(
            base_url,
            headers={
                "Referer": f"{site_url}/",
                "Origin": f"{site_url}",
            },
        )
        
        if response.status_code != 200:
            raise ExtractorError("Obsolete URL")
        
        # Parser HTML semplificato
        app_div = await self._parse_html_simple(response.text, "div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            try:
                # Decodifica HTML entities se necessario
                data_page = app_div["data-page"].replace("&quot;", '"')
                data = json.loads(data_page)
                return data["version"]
            except (KeyError, json.JSONDecodeError, AttributeError) as e:
                raise ExtractorError(f"Version parsing failure: {e}")
        else:
            raise ExtractorError("Unable to find version data")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Estrae URL VixSrc."""
        try:
            version = None
            response = None
            
            # ✅ NUOVO: Gestione per URL di playlist che non richiedono estrazione.
            # Se l'URL è già un manifest, lo restituisce direttamente.
            if "vixsrc.to/playlist" in url:
                logger.info("URL is already a VixSrc manifest, no extraction required.")
                return {
                    "destination_url": url,
                    "request_headers": self.base_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }

            if "iframe" in url:
                # Gestione URL iframe
                site_url = url.split("/iframe")[0]
                version = await self.version(site_url)
                
                # Prima richiesta con headers Inertia
                response = await self._make_robust_request(
                    url, 
                    headers={
                        "x-inertia": "true", 
                        "x-inertia-version": version,
                        **self.base_headers
                    }
                )
                
                # Cerca iframe src
                iframe_data = await self._parse_html_simple(response.text, "iframe")
                if iframe_data and iframe_data.get("src"):
                    iframe_url = iframe_data["src"]
                    
                    # Seconda richiesta all'iframe
                    response = await self._make_robust_request(
                        iframe_url, 
                        headers={
                            "x-inertia": "true", 
                            "x-inertia-version": version,
                            **self.base_headers
                        }
                    )
                else:
                    raise ExtractorError("No iframe found in response")
                    
            elif "movie" in url or "tv" in url:
                # Gestione URL diretti movie/tv
                response = await self._make_robust_request(url)
            else:
                raise ExtractorError("Unsupported VixSrc URL type")
            
            if response.status_code != 200:
                raise ExtractorError("URL component extraction failed, invalid request")
            
            # Estrai script dal body
            script_content = await self._parse_html_simple(response.text, "script")
            if not script_content:
                raise ExtractorError("No script found in body")
            
            # Estrai parametri dallo script JavaScript
            try:
                token_match = re.search(r"['\"]token['\"]\s*:\s*['\"](\w+)['\"]", script_content)
                expires_match = re.search(r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", script_content)
                server_url_match = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", script_content)
                
                if not all([token_match, expires_match, server_url_match]):
                    logger.warning("Could not find all parameters with strict regex, trying flexible search...")
                    # Fallback pattern più generico
                    token_match = token_match or re.search(r"token['\"]\s*:\s*['\"]([^'\"]+)['\"]", script_content)
                    expires_match = expires_match or re.search(r"expires['\"]\s*:\s*['\"](\d+)['\"]", script_content)
                
                if not all([token_match, expires_match, server_url_match]):
                    raise ExtractorError("Missing mandatory parameters in JS script (token/expires/url)")
                
                token = token_match.group(1)
                expires = expires_match.group(1)
                server_url = server_url_match.group(1).replace("\\/", "/") # De-escape forward slashes
                
                # Costruisci URL finale
                if "?b=1" in server_url:
                    final_url = f'{server_url}&token={token}&expires={expires}'
                else:
                    final_url = f"{server_url}?token={token}&expires={expires}"
                
                # Verifica supporto FHD e aggiungi parametri standard
                if "window.canPlayFHD = true" in script_content or "canPlayFHD" in script_content:
                    final_url += "&h=1"
                
                # Aggiungi sempre la lingua e asn se presente (opzionale)
                final_url += "&lang=en"
                
                asn_match = re.search(r"['\"]asn['\"]\s*:\s*['\"]([^'\"]*)['\"]", script_content)
                if asn_match and asn_match.group(1):
                    final_url += f"&asn={asn_match.group(1)}"
                
                # Prepara headers finali
                stream_headers = self.base_headers.copy()
                stream_headers["referer"] = url
                
                logger.info(f"✅ VixSrc master URL extracted: {final_url}")
                
                # ── QUALITY SELECTION: fetcha il master e restituisce la rendition più alta ──
                # Così manifest_rewriter NON entra nel path speciale VixSrc (che non proxa le URL)
                # ma nel path standard che proxa correttamente tutto, fix cross-IP.
                try:
                    master_response = await self._make_robust_request(
                        final_url,
                        headers=stream_headers,
                        retries=2,
                        initial_delay=1
                    )
                    if master_response and master_response.status_code == 200:
                        master_text = master_response.text
                        # Parsifica le varianti dal master HLS
                        master_lines = master_text.split('\n')
                        variants = []
                        for idx, line in enumerate(master_lines):
                            if line.startswith('#EXT-X-STREAM-INF:'):
                                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                                res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                                if bw_match and idx + 1 < len(master_lines):
                                    variant_url = master_lines[idx + 1].strip()
                                    if variant_url and not variant_url.startswith('#'):
                                        variants.append({
                                            'bandwidth': int(bw_match.group(1)),
                                            'height': int(res_match.group(2)) if res_match else 0,
                                            'inf_line': line,
                                            'url': variant_url
                                        })
                        
                        if variants:
                            best = max(variants, key=lambda v: (v['height'], v['bandwidth']))
                            # Risolvi URL relativo della rendition rispetto al master
                            from urllib.parse import urljoin
                            best_url = best['url']
                            if not best_url.startswith('http'):
                                best_url = urljoin(final_url, best_url)
                            # Eredita token/query params dal master se non presenti
                            master_parsed = urlparse(final_url)
                            best_parsed = urlparse(best_url)
                            if master_parsed.query and not best_parsed.query:
                                best_url += f"?{master_parsed.query}"
                            
                            logger.info(f"✅ VixSrc quality selection: {best['height']}p ({best['bandwidth']}bps) → {best_url}")
                            
                            return {
                                "destination_url": best_url,
                                "request_headers": stream_headers,
                                "mediaflow_endpoint": self.mediaflow_endpoint,
                            }
                        else:
                            logger.warning("⚠️ No variants found in master, returning master as-is")
                    else:
                        logger.warning(f"⚠️ Master fetch failed ({master_response.status_code if master_response else 'None'}), returning master URL")
                except Exception as qs_err:
                    logger.warning(f"⚠️ Quality selection failed, returning master URL: {qs_err}")
                
                # Fallback: restituisci il master se la quality selection fallisce
                return {
                    "destination_url": final_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }
                
            except Exception as e:
                raise ExtractorError(f"JavaScript script parsing error: {e}")
                
        except Exception as e:
            logger.error(f"❌ VixSrc extraction failed: {str(e)}")
            raise ExtractorError(f"VixSrc extraction completely failed: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except:
                pass
            self.session = None
