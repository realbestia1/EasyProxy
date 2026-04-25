import asyncio
import logging
import time
import threading
import os
import cloudscraper
from typing import List, Dict, Optional, Callable, Any

logger = logging.getLogger(__name__)

class FreeProxyManager:
    """
    Manager for free proxy pools with parallel validation and caching.
    """
    _instances: Dict[str, 'FreeProxyManager'] = {}
    _lock = threading.Lock()

    def __init__(self, name: str, list_urls: List[str], cache_ttl: int = 7200, max_fetch: int = 0, max_good: int = 0):
        self.name = name
        self.list_urls = list_urls if isinstance(list_urls, list) else [list_urls]
        self.cache_ttl = cache_ttl
        self.max_fetch = max_fetch
        self.max_good = max_good
        self.proxies: List[str] = []
        self.expires_at: float = 0.0
        self.cursor: int = 0
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls, name: str, list_urls: List[str], **kwargs) -> 'FreeProxyManager':
        with cls._lock:
            if name not in cls._instances:
                kwargs.setdefault("cache_ttl", int(os.environ.get("VIXSRC_FREE_PROXY_CACHE_TTL", "7200")))
                cls._instances[name] = cls(name, list_urls, **kwargs)
            return cls._instances[name]

    def _normalize_proxy_url(self, proxy_value: str) -> str:
        proxy_value = proxy_value.strip()
        if not proxy_value:
            return ""
        if proxy_value.startswith("socks5://"):
            return proxy_value.replace("socks5://", "socks5h://", 1)
        if "://" not in proxy_value:
            return f"socks5h://{proxy_value}"
        return proxy_value

    async def _fetch_candidates(self) -> List[str]:
        all_candidates = []
        scraper = cloudscraper.create_scraper(delay=2)
        
        for url in self.list_urls:
            try:
                logger.debug(f"ProxyManager[{self.name}]: Fetching from {url}")
                resp = await asyncio.to_thread(scraper.get, url, timeout=25)
                resp.raise_for_status()
                
                count = 0
                for line in resp.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    normalized = self._normalize_proxy_url(line)
                    if normalized and normalized not in all_candidates:
                        all_candidates.append(normalized)
                        count += 1
                        if self.max_fetch > 0 and len(all_candidates) >= self.max_fetch:
                            break
                logger.info(f"ProxyManager[{self.name}]: Fetched {count} candidates from {url}")
                if self.max_fetch > 0 and len(all_candidates) >= self.max_fetch:
                    break
            except Exception as e:
                logger.warning(f"ProxyManager[{self.name}]: Failed to fetch proxy list from {url}: {e}")
        
        return all_candidates

    async def _probe_proxy_worker(self, proxy_url: str, probe_func: Callable[[str], Any], semaphore: asyncio.Semaphore, good_list: List[str], ready_event: Optional[asyncio.Event] = None):
        if self.max_good > 0 and len(good_list) >= self.max_good:
            return

        async with semaphore:
            if self.max_good > 0 and len(good_list) >= self.max_good:
                return
                
            try:
                if asyncio.iscoroutinefunction(probe_func):
                    is_good = await probe_func(proxy_url)
                else:
                    is_good = await asyncio.to_thread(probe_func, proxy_url)
                
                if is_good:
                    if self.max_good <= 0 or len(good_list) < self.max_good:
                        if proxy_url not in good_list:
                            good_list.append(proxy_url)
                            logger.info(f"ProxyManager[{self.name}]: Validated working proxy: {proxy_url}")
                            if ready_event and len(good_list) >= 3:
                                ready_event.set()
            except Exception:
                pass

    async def get_proxies(self, probe_func: Callable[[str], Any], force_refresh: bool = False) -> List[str]:
        now = time.time()
        
        # Se abbiamo già dei proxy e non sono scaduti, ma ne vogliamo "di più" per la rotazione,
        # continuiamo solo se il pool è piccolo (es. < 50) per evitare check infiniti.
        should_find_more = len(self.proxies) < 50 and not force_refresh
        
        if not force_refresh and self.proxies and self.expires_at > now and not should_find_more:
            return list(self.proxies)

        async with self._refresh_lock:
            # Controllo se nel frattempo un altro task ha aggiornato
            if not force_refresh and self.proxies and self.expires_at > time.time() and len(self.proxies) >= 50:
                return list(self.proxies)

            logger.info(f"ProxyManager[{self.name}]: Incremental validation (+2 proxies)...")
            
            # Se la cache è scaduta o forziamo, resettiamo tutto
            if force_refresh or self.expires_at <= time.time():
                self.proxies = []
                self.expires_at = time.time() + self.cache_ttl
                self._candidates_cache = await self._fetch_candidates()
            
            if not hasattr(self, '_candidates_cache') or not self._candidates_cache:
                self._candidates_cache = await self._fetch_candidates()

            if not self._candidates_cache:
                return list(self.proxies)

            # Prendiamo i candidati che non abbiamo ancora testato in questo ciclo
            if not hasattr(self, '_tested_indices'):
                self._tested_indices = set()
            
            remaining = [c for i, c in enumerate(self._candidates_cache) if i not in self._tested_indices]
            if not remaining:
                # Abbiamo finito i candidati, resettiamo il set per ricominciare se serve
                self._tested_indices = set()
                remaining = self._candidates_cache

            good_this_round = []
            semaphore = asyncio.Semaphore(50) # Meno concorrenza per check incrementali
            ready_event = asyncio.Event()
            
            # Tasks per questa tornata
            tasks = []
            for i, candidate in enumerate(remaining):
                # Usiamo una funzione wrapper per segnare l'indice come testato
                async def test_and_mark(c=candidate, idx=i):
                    await self._probe_proxy_worker(c, probe_func, semaphore, good_this_round, ready_event)
                    self._tested_indices.add(idx)

                tasks.append(asyncio.create_task(test_and_mark()))

            # Aspetta finché non ne troviamo 2 NUOVI o finché non finiscono i tasks
            try:
                # Controlliamo ogni secondo se ne abbiamo trovati 2
                while len(good_this_round) < 2:
                    # Se tutti i task sono finiti, usciamo
                    if all(t.done() for t in tasks):
                        break
                    await asyncio.sleep(0.5)
            except Exception:
                pass
            finally:
                # FERMA TUTTO: Cancella i task rimanenti per non sprecare CPU
                for t in tasks:
                    if not t.done():
                        t.cancel()

            if good_this_round:
                for p in good_this_round:
                    if p not in self.proxies:
                        self.proxies.append(p)
                logger.info(f"ProxyManager[{self.name}]: Found {len(good_this_round)} new proxies. Total pool: {len(self.proxies)}")
            
            return list(self.proxies)

    async def get_next_sequence(self, probe_func: Callable[[str], Any]) -> List[str]:
        proxies = await self.get_proxies(probe_func)
        if not proxies:
            return []
        
        idx = self.cursor % len(proxies)
        self.cursor = (idx + 1) % len(proxies)
        
        return proxies[idx:] + proxies[:idx]

    def report_failure(self, proxy_url: str):
        """Rimuove un proxy dalla cache se viene segnalato come non funzionante."""
        if proxy_url in self.proxies:
            try:
                self.proxies.remove(proxy_url)
                logger.warning(f"ProxyManager[{self.name}]: Proxy {proxy_url} removed from cache after reported failure.")
            except ValueError:
                pass
