import aiohttp
import asyncio
import json
import logging
import os

from config import FLARESOLVERR_TIMEOUT, FLARESOLVERR_URL

logger = logging.getLogger(__name__)


class SolverSessionManager:
    """
    Gestore delle sessioni FlareSolverr.
    Supporta sessioni persistenti esplicite o sessioni temporanee.
    """

    _instance = None
    _persistent_sessions = {}  # {key: session_id}
    _sessions_file = "persistent_sessions.json"
    _lock = asyncio.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SolverSessionManager, cls).__new__(cls)
        return cls._instance

    async def _init_if_needed(self):
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            if os.path.exists(self._sessions_file):
                try:
                    with open(self._sessions_file, "r") as f:
                        self._persistent_sessions = json.load(f)
                    logger.info(
                        f"FlareSolverr: Caricate {len(self._persistent_sessions)} sessioni persistenti dal file."
                    )
                except Exception as e:
                    logger.warning("FlareSolverr: Errore caricamento sessioni: %s", e)
            self._initialized = True

    def _save_sessions(self):
        try:
            with open(self._sessions_file, "w") as f:
                json.dump(self._persistent_sessions, f)
        except Exception as e:
            logger.warning("FlareSolverr: Errore salvataggio sessioni: %s", e)

    async def get_session(self, proxy: str = None) -> tuple[str, bool]:
        """
        Ottiene una sessione FlareSolverr temporanea.
        Ritorna una tupla (session_id, is_persistent).
        """
        await self._init_if_needed()
        if not FLARESOLVERR_URL:
            return None, False

        session_id = await self._create_session(proxy)
        return session_id, False

    async def get_persistent_session(self, key: str, proxy: str = None) -> str:
        """Ottiene o crea una sessione persistente identificata da una chiave."""
        await self._init_if_needed()
        if not FLARESOLVERR_URL:
            return None

        async with self._lock:
            if key in self._persistent_sessions:
                sid = self._persistent_sessions[key]
                if await self._session_exists(sid):
                    return sid
                logger.info("FlareSolverr: Sessione %s per %s non piu valida o scaduta.", sid, key)

            logger.info("FlareSolverr: Creazione nuova sessione persistente per chiave: %s", key)
            session_id = await self._create_session(proxy)
            if session_id:
                self._persistent_sessions[key] = session_id
                self._save_sessions()
            return session_id

    async def _session_exists(self, session_id: str) -> bool:
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {"cmd": "sessions.list"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(endpoint, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return session_id in data.get("sessions", [])
            except Exception:
                pass
        return False

    async def _create_session(self, proxy: str = None) -> str:
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {
            "cmd": "sessions.create",
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }
        if proxy:
            solver_proxy = proxy.replace("socks5h://", "socks5://") if proxy.startswith("socks5h://") else proxy
            payload["proxy"] = {"url": solver_proxy}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                        endpoint,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("session")
            except Exception as e:
                logger.error("FlareSolverr: Errore creazione sessione: %s", e)
        return None

    async def release_session(self, session_id: str, is_persistent: bool):
        """Chiude la sessione se non e persistente."""
        if not session_id or is_persistent or not FLARESOLVERR_URL:
            return

        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {"cmd": "sessions.destroy", "session": session_id}
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(endpoint, json=payload, timeout=10)
            except Exception:
                pass


solver_manager = SolverSessionManager()
