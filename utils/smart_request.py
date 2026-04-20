import aiohttp
import logging
import asyncio
from typing import Optional, Dict, Any
from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)

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

async def smart_request(cmd: str, url: str, headers: Optional[Dict] = None, post_data: Optional[str] = None, proxies: Optional[list] = None) -> str:
    """
    Effettua una richiesta intelligente: prova la via diretta e se fallisce (Cloudflare) usa FlareSolverr.
    """
    current_proxies = proxies or GLOBAL_PROXIES
    proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, current_proxies)
    
    headers = headers or {}
    if "User-Agent" not in headers and "user-agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

    html = ""
    direct_success = False

    # 1. Tentativo Diretto (con proxy se necessario)
    try:
        connector = get_connector_for_proxy(proxy)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            method = session.get if cmd.lower() == "request.get" else session.post
            async with method(url, headers=headers, data=post_data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    # Verifica se è una pagina di blocco
                    if not any(marker in html.lower() for marker in CF_MARKERS):
                        direct_success = True
                elif resp.status in (403, 503):
                    logger.warning(f"SmartRequest: HTTP {resp.status} rilevato per {url}, possibile protezione Cloudflare.")
                else:
                    html = await resp.text() # Prendi il contenuto per debug
    except Exception as e:
        logger.debug(f"SmartRequest: Tentativo diretto fallito per {url}: {e}")

    # 2. Fallback su FlareSolverr
    if not direct_success:
        if not FLARESOLVERR_URL:
            logger.error("SmartRequest: FlareSolverr non configurato e tentativo diretto fallito.")
            return html

        logger.info(f"SmartRequest: Uso FlareSolverr per {url}")
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {
            "cmd": cmd,
            "url": url,
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }
        if post_data: payload["postData"] = post_data
        if proxy:
            payload["proxy"] = {"url": proxy}

        async with aiohttp.ClientSession() as fs_session:
            try:
                async with fs_session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 95),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("solution", {}).get("response", "")
                        else:
                            logger.error(f"SmartRequest: FlareSolverr errore: {data.get('message')}")
                    else:
                        logger.error(f"SmartRequest: FlareSolverr HTTP {resp.status}")
            except Exception as e:
                logger.error(f"SmartRequest: FlareSolverr fallito: {e}")

    return html
