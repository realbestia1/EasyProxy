import logging
import re
import asyncio
import urllib.parse
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, GLOBAL_PROXIES, get_connector_for_proxy
from utils.smart_request import smart_request

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class FreeshotExtractor:
    """
    Extractor per Freeshot (popcdn.day).
    Risolve l'URL iframe e restituisce l'm3u8 finale.
    """
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # Exponential backoff in seconds
    
    def __init__(self, request_headers=None, proxies=None):
        self.request_headers = request_headers or {}
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Referer": "https://thisnot.business/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        self.proxies = proxies or GLOBAL_PROXIES
        self.session = None
        self.flaresolverr_url = FLARESOLVERR_URL
        self.flaresolverr_timeout = FLARESOLVERR_TIMEOUT


    async def _get_session(self, url: str = None):
        if self.session is None or self.session.closed:
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies) if url else None
            connector = get_connector_for_proxy(proxy, ssl=False) if proxy else TCPConnector(ssl=False, limit=0, use_dns_cache=True)
            timeout = ClientTimeout(total=30)
            self.session = ClientSession(connector=connector, timeout=timeout)
        return self.session

    async def extract(self, url, **kwargs):
        """
        Estrae l'URL m3u8 da un link popcdn.day o da un codice canale.
        Input url può essere:
        1. https://popcdn.day/player/CODICE (nuovo formato)
        2. https://popcdn.day/go.php?stream=CODICE (vecchio formato - convertito)
        3. freeshot://CODICE (se vogliamo supportare un custom scheme)
        4. CODICE (se passato come parametro d=CODICE e host=freeshot)
        """
        
        # Determina il codice canale
        channel_code = url
        
        # 1. Supporto per freeshot.live
        if "freeshot.live" in url:
            # Se è già un link embed, estrai direttamente (es: https://freeshot.live/embed/ZonaDAZN.php)
            embed_match = re.search(r'embed/([^/.]+)\.php', url)
            if embed_match:
                channel_code = embed_match.group(1)
                logger.info(f"FreeshotExtractor: Estratto codice {channel_code} da URL embed")
            else:
                # Altrimenti scarica la pagina principale per trovare l'iframe
                # Usiamo FlareSolverr se disponibile, altrimenti fallback su aiohttp
                content = ""
                if self.flaresolverr_url:
                    try:
                        logger.info(f"FreeshotExtractor: Uso FlareSolverr per estrarre codice da {url}")
                        content = await smart_request("request.get", url, headers=self.base_headers, proxies=self.proxies)
                    except Exception as e:
                        logger.warning(f"FreeshotExtractor: FlareSolverr fallito per freeshot.live: {e}")
                
                if not content:
                    session = await self._get_session(url)
                    try:
                        async with session.get(url, headers=self.base_headers, timeout=15) as resp:
                            if resp.status == 200:
                                content = await resp.text()
                    except Exception as e:
                        logger.warning(f"FreeshotExtractor: Errore nel recupero codice da freeshot.live: {e}")

                if content:
                    # 1. Cerca iframe popcdn diretto: //popcdn.day/go.php?stream=ZonaDAZN
                    match_pop = re.search(r'stream=([^&"\'\s]+)', content)
                    if match_pop:
                        channel_code = match_pop.group(1)
                        logger.info(f"FreeshotExtractor: Trovato codice {channel_code} (popcdn stream) in pagina freeshot.live")
                    else:
                        # 2. Cerca iframe embed: //freeshot.live/embed/ZonaDAZN.php
                        match_emb = re.search(r'embed/([^/.]+)\.php', content)
                        if match_emb:
                            channel_code = match_emb.group(1)
                            logger.info(f"FreeshotExtractor: Trovato codice {channel_code} (embed link) in pagina freeshot.live")

        # 2. Estrai il codice dai vari formati popcdn
        if "go.php?stream=" in channel_code:
            channel_code = channel_code.split("go.php?stream=")[-1].split("&")[0]
        elif "popcdn.day/player/" in channel_code:
            channel_code = channel_code.split("/player/")[-1].split("?")[0].split("/")[0]
        elif channel_code.startswith('http'):
            # Se è ancora un URL freeshot.live, proviamo a estrarre il codice dalla fine (ultimo tentativo disperato)
            # es: /live-tv/zona-dazn-it/351 -> se non abbiamo trovato nulla, proviamo a pulire
            path_parts = [p for p in urllib.parse.urlparse(channel_code).path.split("/") if p]
            if path_parts:
                # Prova a prendere l'elemento penultimo se l'ultimo è un numero
                if path_parts[-1].isdigit() and len(path_parts) > 1:
                    candidate = path_parts[-2]
                else:
                    candidate = path_parts[-1]
                
                # Se è freeshot.live, facciamo un po' di pulizia (rimuoviamo trattini e IT)
                if "freeshot.live" in channel_code:
                    # es: zona-dazn-it -> ZonaDAZN (tentativo euristico)
                    candidate = candidate.replace("-it", "").replace("-", "").title()
                    # Ma ZonaDAZN ha DAZN maiuscolo. Meglio non esagerare con la pulizia.
                    # Se non sappiamo, usiamo il slug pulito.
                    pass
                channel_code = candidate
            else:
                # Fallback estremo: prendi l'ultima parte
                channel_code = channel_code.split("/")[-1]
        
        # Rimuovi eventuali parametri residui
        channel_code = channel_code.split("?")[0].split("&")[0]
        
        # Nuovo URL formato /player/
        target_url = f"https://popcdn.day/player/{urllib.parse.quote(channel_code)}"

        logger.info(f"FreeshotExtractor: Risoluzione {target_url} (channel: {channel_code})")
        
        # 3. Risoluzione finale tramite popcdn.day (diretto)
        body = ""
        ua = self.base_headers["User-Agent"]
        
        session = await self._get_session(target_url)
        # Retry logic with exponential backoff for direct request
        try:
            body = await smart_request("request.get", target_url, headers=self.base_headers, proxies=self.proxies)
        except Exception as e:
            raise ExtractorError(f"Freeshot extraction failed for {target_url}: {e}")
        
        # Token extraction (no need for try-except wrapper since ExtractorError propagates)
        # Nuova estrazione token via currentToken
        match = re.search(r'streamUrl\s*:\s*"([^"]+)"', body)
        if not match:
            # Fallback al vecchio metodo iframe
            match = re.search(r'frameborder="0"\s+src="([^"]+)"', body, re.IGNORECASE)
            if match:
                iframe_url = match.group(1)
                # Estrai token dall'iframe URL
                token_match = re.search(r'token=([^&]+)', iframe_url)
                if token_match:
                    token = token_match.group(1)
                    # Nuovo formato URL m3u8: tracks-v1a1/mono.m3u8
                    m3u8_url = f"https://planetary.lovecdn.ru/{channel_code}/tracks-v1a1/mono.m3u8?token={token}"
                else:
                    raise ExtractorError("Freeshot token not found in iframe")
            else:
                raise ExtractorError("Freeshot token/iframe not found in page content")
        else:
            # Nuovo formato URL m3u8: tracks-v1a1/mono.m3u8
            m3u8_url = match.group(1)
            m3u8_url = m3u8_url.replace("\\", "")
        
        logger.info(f"FreeshotExtractor: Risolto -> {m3u8_url}")
        
        # Ritorniamo la struttura attesa da HLSProxy
        return {
            "destination_url": m3u8_url,
            "request_headers": {
                "User-Agent": ua,
                "Referer": "https://popcdn.day/",
                "Origin": "https://popcdn.day"
            },
            "mediaflow_endpoint": "hls_proxy"
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
