import aiohttp
import logging
import asyncio
from typing import Optional, Dict, Any
from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy, get_solver_proxy_url
from aiohttp_socks import ProxyConnector
import yarl

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

async def smart_request(cmd: str, url: str, headers: Optional[Dict] = None, post_data: Optional[str] = None, proxies: Optional[list] = None) -> Any:
    """
    Effettua una richiesta intelligente: prova la via diretta, poi curl_cffi, e se fallisce usa FlareSolverr.
    """
    current_proxies = proxies or GLOBAL_PROXIES
    proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, current_proxies)
    
    headers = headers or {}
    if "User-Agent" not in headers and "user-agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    
    # ✅ FIX: Pulisci il Referer per domini critici se necessario
    for ref_key in ["Referer", "referer"]:
        if ref_key in headers and "cccdn.net" in url:
            headers[ref_key] = "https://cinemacity.cc/"

    # Pattern comuni per identificare protezioni Cloudflare o simili
    CF_MARKERS = [
        "cf-challenge",
        "ray id",
        "id=\"cf-wrapper\"",
        "__cf_chl_opt",
        "checking your browser",
        "just a moment...",
        "enable javascript and cookies to continue"
    ]

    # 1. Tentativo Diretto (aiohttp)
    try:
        connector = get_connector_for_proxy(proxy)
        async with aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        ) as session:
            method = session.get if cmd.lower() == "request.get" else session.post
            async with method(url, headers=headers, data=post_data, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    if not any(marker in content.lower() for marker in CF_MARKERS):
                        # Se è JSON, lo mettiamo comunque in 'html' come stringa o lo lasciamo gestire all'estrattore
                        # Per compatibilità, restituiamo sempre il dizionario
                        response_url = yarl.URL(str(resp.url))
                        jar_cookies = session.cookie_jar.filter_cookies(response_url)
                        cookies = {name: morsel.value for name, morsel in jar_cookies.items()}
                        for name, morsel in resp.cookies.items():
                            cookies[name] = morsel.value
                        return {"html": content, "cookies": cookies}
                elif resp.status in (403, 503):
                    logger.warning(f"SmartRequest (aiohttp): HTTP {resp.status} per {url}, provo curl_cffi...")
    except Exception as e:
        logger.debug(f"SmartRequest (aiohttp) fallito: {e}")

    # 2. Tentativo con curl_cffi (Browser Impersonation)
    if HAS_CURL_CFFI:
        try:
            logger.debug(f"SmartRequest (curl_cffi): Impersonazione browser per {url}")
            async with CurlAsyncSession(impersonate="chrome124") as s:
                curl_proxies = {"http": proxy, "https": proxy} if proxy else None
                
                # ✅ FIX: Rimuovi User-Agent per evitare discrepanze col fingerprint TLS
                curl_headers = dict(headers)
                if "User-Agent" in curl_headers: del curl_headers["User-Agent"]
                if "user-agent" in curl_headers: del curl_headers["user-agent"]
                
                c_method = s.get if cmd.lower() == "request.get" else s.post
                c_resp = await c_method(url, headers=curl_headers, data=post_data, proxies=curl_proxies, timeout=30)
                
                if c_resp.status_code == 200:
                    # Restituiamo sempre lo stesso formato dizionario
                    return {"html": c_resp.text, "cookies": c_resp.cookies.get_dict()}
                else:
                    logger.warning(f"SmartRequest (curl_cffi): Status {c_resp.status_code} per {url}")
        except Exception as e:
            logger.error(f"SmartRequest (curl_cffi) fallito: {e}")

    # 3. Fallback su FlareSolverr
    if not FLARESOLVERR_URL:
        logger.error("SmartRequest: FlareSolverr non configurato e tentativi precedenti falliti.")
        return {"html": "", "cookies": {}}

    logger.debug(f"SmartRequest: Uso FlareSolverr per {url}")
    endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
    
    # ✅ FIX: Estrai i cookie dagli header per passarli a FlareSolverr
    fs_cookies = []
    cookie_header = headers.get("Cookie") or headers.get("cookie")
    if cookie_header:
        for cookie_item in cookie_header.split(";"):
            if "=" in cookie_item:
                k, v = cookie_item.strip().split("=", 1)
                fs_cookies.append({"name": k, "value": v})

    payload = {
        "cmd": cmd,
        "url": url,
        "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
    }
    if fs_cookies: payload["cookies"] = fs_cookies
    if post_data: payload["postData"] = post_data
    if proxy:
        payload["proxy"] = {"url": proxy}
        headers_for_fs = {"X-Proxy-Server": get_solver_proxy_url(proxy)}
    else:
        headers_for_fs = {}

    async with aiohttp.ClientSession() as fs_session:
        try:
            async with fs_session.post(
                endpoint,
                json=payload,
                headers=headers_for_fs,
                timeout=aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 95),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        solution = data.get("solution", {})
                        cookies_list = solution.get("cookies", [])
                        
                        # ✅ DEBUG: Vediamo cosa risponde esattamente FlareSolverr
                        logger.debug(f"🚀 FlareSolverr solution cookies: {len(cookies_list)}")
                        
                        cookies_dict = {c["name"]: c["value"] for c in cookies_list}
                        return {
                            "html": solution.get("response", ""),
                            "cookies": cookies_dict
                        }
                    else:
                        logger.warning(f"SmartRequest: FlareSolverr ha risposto con errore: {data.get('message')}")
        except Exception as e:
            logger.error(f"SmartRequest: FlareSolverr fallito: {e}")

    return {"html": "", "cookies": {}}
