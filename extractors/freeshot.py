import logging
import re
import urllib.parse
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from config import get_connector_for_proxy, get_preferred_proxy_for_url
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

class FreeshotExtractor(BaseExtractor):
    """
    Extractor per Freeshot (wideiptv.top).
    Risolve l'URL iframe e restituisce l'm3u8 finale.
    """
    def __init__(self, request_headers=None, proxies=None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="freeshot", bypass_warp=bypass_warp)
        self.request_headers = request_headers or {}
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Referer": "https://thisnot.business/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        self.proxies = proxies or _cfg.GLOBAL_PROXIES
        self.session = None
        self._session_proxy = None



    async def _get_session(self, url: str = None):
        if self._force_direct:
            proxy = None
        else:
            forced = _cfg.effective_forced_proxy(url, self._forced_proxy)
            proxy = forced or await get_preferred_proxy_for_url(url, "freeshot", self.proxies, self.bypass_warp_active)
        if proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise aiohttp.ClientConnectionError(
                "Freeshot: direct fallback disabled; no proxy route available"
            )
        self.last_used_proxy = proxy
        async with self._session_lock:
            session = self._route_sessions.get(proxy)
            if session is None or session.closed:
                connector = get_connector_for_proxy(proxy, ssl=False) if proxy else TCPConnector(ssl=False, limit=0, use_dns_cache=True)
                timeout = ClientTimeout(total=30)
                session = ClientSession(connector=connector, timeout=timeout)
                self._route_sessions[proxy] = session
            self.session = session
            self._session_proxy = proxy
            return self.session

    async def _fetch_text(self, url: str, headers: dict) -> str:
        session = await self._get_session(url)
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                return await resp.text()
            raise ExtractorError(f"Freeshot fetch failed for {url}: HTTP {resp.status}")

    async def extract(self, url, **kwargs):
        """
        Estrae l'URL m3u8 da un link popcdn.day o da un codice canale.
        Input url può essere:
        1. https://wideiptv.top/player/CODICE (formato corrente)
        2. https://popcdn.day/player/CODICE (formato legacy)
        3. https://popcdn.day/go.php?stream=CODICE (formato legacy)
        4. freeshot://CODICE (se vogliamo supportare un custom scheme)
        5. CODICE (se passato come parametro d=CODICE e host=freeshot)
        """
        
        self._apply_routing_kwargs(url, kwargs)
        # Determina il codice canale
        channel_code = url
        
        # 1. Supporto per freeshot.live
        if "freeshot.live" in url:
            # Se è già un link embed, estrai direttamente (es: https://freeshot.live/embed/ZonaDAZN.php)
            embed_match = re.search(r'embed/([^/.]+)\.php', url)
            if embed_match:
                channel_code = embed_match.group(1)
                logger.debug(f"FreeshotExtractor: Estratto codice {channel_code} da URL embed")
            else:
                # Altrimenti scarica la pagina principale per trovare l'iframe
                content = ""
                try:
                    content = await self._fetch_text(url, self.base_headers)
                except Exception as e:
                    logger.warning(f"FreeshotExtractor: Errore nel recupero codice da freeshot.live: {e}")

                if content:
                    # 1. Cerca iframe popcdn diretto: //popcdn.day/go.php?stream=ZonaDAZN
                    match_pop = re.search(r'stream=([^&"\'\s]+)', content)
                    if match_pop:
                        channel_code = match_pop.group(1)
                        logger.debug(f"FreeshotExtractor: Trovato codice {channel_code} (popcdn stream) in pagina freeshot.live")
                    else:
                        # 2. Cerca iframe embed: //freeshot.live/embed/ZonaDAZN.php
                        match_emb = re.search(r'embed/([^/.]+)\.php', content)
                        if match_emb:
                            channel_code = match_emb.group(1)
                            logger.debug(f"FreeshotExtractor: Trovato codice {channel_code} (embed link) in pagina freeshot.live")

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
        
        # Player corrente usato dall'embed Freeshot.
        target_url = f"https://wideiptv.top/player/{urllib.parse.quote(channel_code)}"

        logger.debug(f"FreeshotExtractor: Risoluzione {target_url} (channel: {channel_code})")
        
        # 3. Risoluzione finale tramite popcdn.day (diretto)
        body = ""
        ua = self.base_headers["User-Agent"]
        
        try:
            player_headers = {
                **self.base_headers,
                "Referer": "https://freeshot.live/",
                "Origin": "https://freeshot.live",
            }
            body = await self._fetch_text(target_url, player_headers)
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
                "Referer": "https://wideiptv.top/",
                "Origin": "https://wideiptv.top"
            },
            "mediaflow_endpoint": "hls_proxy",
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

