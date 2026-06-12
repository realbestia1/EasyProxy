import os
import logging
import random
import socket
import time
import asyncio
import contextvars
import urllib.request
from dotenv import load_dotenv
from config_store import get as _cfg_get, set as _cfg_set, get_all as _cfg_get_all

_proxy_source_cache: dict[str, tuple[float, list]] = {}
_PROXY_SOURCE_TTL = 600


def get_extractor_proxies(extractor_name: str) -> list:
    """Returns proxies from config_store for the given extractor.
    Supports: direct proxy string, list (backward compat), or dict with 'file' key (file/URL source).
    """
    if not extractor_name:
        return []
    extractor_proxies = _cfg_get("extractor_proxies", {})
    entry = extractor_proxies.get(extractor_name.lower())
    if not entry:
        return []
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, list):
        return entry
    if isinstance(entry, dict) and "file" in entry:
        return _read_proxy_source(entry["file"])
    return []


def _read_proxy_source(source: str) -> list:
    now = time.time()
    cached = _proxy_source_cache.get(source)
    if cached and (now - cached[0]) < _PROXY_SOURCE_TTL:
        return cached[1]
    try:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        else:
            with open(source, "r", encoding="utf-8") as f:
                text = f.read()
        proxies = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
        _proxy_source_cache[source] = (now, proxies)
        return proxies
    except Exception as e:
        logger.warning(f"Error reading proxy source {source}: {e}")
        return []

# ContextVar for thread-safe/async-safe warp bypass state
BYPASS_WARP_CONTEXT = contextvars.ContextVar("bypass_warp", default=False)
SELECTED_PROXY_CONTEXT = contextvars.ContextVar("selected_proxy", default=None)
STRICT_PROXY_CONTEXT = contextvars.ContextVar("strict_proxy", default=False)
PROXY_SOURCE_LIST = contextvars.ContextVar("proxy_source_list", default=None)

load_dotenv()

# --- Log Level Configuration ---
LOG_LEVEL_STR = "WARNING"
LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
LOG_LEVEL = LOG_LEVEL_MAP.get(LOG_LEVEL_STR, logging.WARNING)
PROXY_TEST_TIMEOUT = 10
cpu_cores = os.cpu_count() or 4
PROXY_TEST_CONCURRENCY = 10 if cpu_cores == 1 else min(100, max(30, cpu_cores * 15))
WARP_PROXY_URL = "socks5h://127.0.0.1:1080"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    force=True,
)


class AsyncioWarningFilter(logging.Filter):
    def filter(self, record):
        return "Unknown child process pid" not in record.getMessage()


logging.getLogger("asyncio").addFilter(AsyncioWarningFilter())

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class ProxyList(list):
    def __init__(self, values=(), strict: bool = False):
        super().__init__(values)
        self.strict = strict


def get_preferred_proxy(proxies: list | None) -> str | None:
    """Return the first proxy from an ordered list. No alive filtering (use async version for that)."""
    if not proxies:
        return None
    PROXY_SOURCE_LIST.set(proxies)
    if getattr(proxies, "strict", False):
        for proxy in proxies or []:
            if proxy:
                return proxy
    result = proxies[0] if proxies else None
    if result:
        SELECTED_PROXY_CONTEXT.set(result)
    return result


async def find_first_alive_async(proxies: list, concurrency: int | None = None) -> str | None:
    """Test proxies in parallel with ThreadPoolExecutor, return first alive. Respects strict flag."""
    if not proxies:
        return None
    if getattr(proxies, "strict", False):
        return proxies[0]
    concurrency = concurrency or PROXY_TEST_CONCURRENCY
    # Filter out globally dead proxies first
    now = time.time()
    with _proxy_lock:
        proxies = [p for p in proxies if p not in DEAD_PROXIES or now >= DEAD_PROXIES.get(p, 0)]
    if not proxies:
        return None
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()

    async def _check(proxy: str) -> str | None:
        async with sem:
            try:
                await loop.run_in_executor(None, _socket_check, proxy, 5)
                return proxy
            except (OSError, socket.timeout):
                return None

    tasks = {asyncio.create_task(_check(p)): p for p in proxies if p}
    pending = set(tasks.keys())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            result = t.result()
            if result is not None:
                for pt in pending:
                    pt.cancel()
                return result
    return None


async def filter_alive_async(proxies: list, concurrency: int | None = None) -> list:
    """Test all proxies in parallel, return all alive. Respects DEAD_PROXIES."""
    if not proxies:
        return []
    if getattr(proxies, "strict", False):
        return list(proxies)
    concurrency = concurrency or PROXY_TEST_CONCURRENCY
    now = time.time()
    with _proxy_lock:
        candidates = [p for p in proxies if p not in DEAD_PROXIES or now >= DEAD_PROXIES.get(p, 0)]
    if not candidates:
        return []
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()

    async def _check(proxy: str):
        async with sem:
            try:
                await loop.run_in_executor(None, _socket_check, proxy, 2)
                return proxy
            except (OSError, socket.timeout):
                return None

    tasks = [asyncio.create_task(_check(p)) for p in candidates if p]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, str)]


def get_transport_route_proxy(url: str, transport_routes: list) -> str | None:
    """Return only an explicit TRANSPORT_ROUTES proxy match, without global/WARP fallback."""
    if not url or not transport_routes:
        return None
    normalized_url = url.lower()
    for route in transport_routes:
        url_pattern = route["url"].lower()
        if url_pattern in normalized_url:
            proxy_value = route.get("proxy")
            if not proxy_value:
                return None
            return proxy_value
    return None


def _get_dynamic_warp_enabled() -> bool:
    return _cfg_get("enable_warp", False)

def _get_dynamic_warp_exclude_domains() -> list:
    defaults = _cfg_get("warp_exclude_domains", [])
    custom = _cfg_get("warp_exclude_domains_custom", [])
    seen = set()
    merged = []
    for d in defaults + custom:
        if d not in seen:
            seen.add(d)
            merged.append(d)
    return merged

def _get_dynamic_global_proxies() -> list:
    return _cfg_get("global_proxies", [])

def _get_dynamic_transport_routes() -> list:
    return _cfg_get("transport_routes", [])

def _get_dynamic_proxy_test_concurrency() -> int:
    val = _cfg_get("proxy_test_concurrency")
    if val is None or val == 0:
        cpus = os.cpu_count() or 4
        return 10 if cpus == 1 else min(100, max(30, cpus * 15))
    return int(val)

def get_ordered_proxies_for_url(
    url: str | None,
    extractor_name: str = "",
    fallback_proxies: list | None = None,
    bypass_warp: bool | None = None,
) -> list[str]:
    """Build proxy priority: extractor-specific, TRANSPORT_ROUTES, fallback/global, WARP."""
    ordered = []

    def build(candidates, strict: bool = False):
        values = []
        for proxy in candidates:
            if proxy and proxy not in values:
                values.append(proxy)
        return ProxyList(values, strict=strict)

    def add(proxy: str | None):
        if proxy and proxy not in ordered:
            ordered.append(proxy)

    _ENABLE_WARP = _get_dynamic_warp_enabled()
    _WARP_PROXY_URL = WARP_PROXY_URL
    _WARP_EXCLUDE_DOMAINS = _get_dynamic_warp_exclude_domains()
    _GLOBAL_PROXIES = _get_dynamic_global_proxies()
    _TRANSPORT_ROUTES = _get_dynamic_transport_routes()

    selected_proxy = SELECTED_PROXY_CONTEXT.get()
    selected_proxy_is_strict = STRICT_PROXY_CONTEXT.get()
    if selected_proxy and selected_proxy_is_strict:
        return build([selected_proxy], strict=True)

    extractor_proxies = get_extractor_proxies(extractor_name or "")
    if extractor_proxies:
        return build(extractor_proxies, strict=True)

    if url and _TRANSPORT_ROUTES:
        normalized_url = url.lower()
        for route in _TRANSPORT_ROUTES:
            url_pattern = route["url"].lower()
            if url_pattern in normalized_url:
                proxy_value = route.get("proxy")
                if not proxy_value:
                    return ProxyList([], strict=False)
                return build([proxy_value], strict=True)

    if selected_proxy:
        add(selected_proxy)

    for proxy in fallback_proxies or []:
        add(proxy)

    for proxy in _GLOBAL_PROXIES:
        add(proxy)

    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    normalized_url = (url or "").lower()
    is_excluded = any(domain in normalized_url for domain in _WARP_EXCLUDE_DOMAINS)
    if _ENABLE_WARP and not bypass_warp and not is_excluded:
        add(_WARP_PROXY_URL)

    return ProxyList(ordered, strict=False)


def should_allow_direct_fallback(proxies: list | None) -> bool:
    """Allow direct fallback only when no proxy exists."""
    if getattr(proxies, "strict", False):
        return False
    active = [proxy for proxy in proxies or [] if proxy]
    return not active


async def get_preferred_proxy_for_url(
    url: str | None,
    extractor_name: str = "",
    fallback_proxies: list | None = None,
    bypass_warp: bool | None = None,
) -> str | None:
    """Return the first alive proxy using parallel test across the ordered priority list."""
    ordered = get_ordered_proxies_for_url(url, extractor_name, fallback_proxies, bypass_warp)
    if not ordered:
        return None
    PROXY_SOURCE_LIST.set(ordered)
    result = await find_first_alive_async(ordered)
    if result:
        SELECTED_PROXY_CONTEXT.set(result)
    return result


async def get_preferred_proxy_for_url_async(
    url: str | None,
    extractor_name: str = "",
    fallback_proxies: list | None = None,
    bypass_warp: bool | None = None,
) -> str | None:
    """Return the first alive proxy using parallel test across the ordered priority list."""
    ordered = get_ordered_proxies_for_url(url, extractor_name, fallback_proxies, bypass_warp)
    if not ordered:
        return None
    PROXY_SOURCE_LIST.set(ordered)
    result = await find_first_alive_async(ordered)
    if result:
        SELECTED_PROXY_CONTEXT.set(result)
    return result


_PROXY_STATUS_CACHE = {"alive": True, "last_check": 0}
DEAD_PROXIES = {}  # proxy_url -> expire_time
_proxy_lock = __import__('threading').Lock()  # sync access to DEAD_PROXIES + _PROXY_STATUS_CACHE
_proxy_async_lock = asyncio.Lock()  # async access to the same structures


def is_proxy_alive(proxy_url: str, force_check: bool = False) -> bool:
    """Checks if a proxy is reachable and not marked dead globally."""
    if not proxy_url:
        return False

    now = time.time()
    with _proxy_lock:
        # Check if proxy is globally marked dead
        if proxy_url in DEAD_PROXIES:
            expire_time = DEAD_PROXIES[proxy_url]
            if now < expire_time:
                return False
            else:
                DEAD_PROXIES.pop(proxy_url, None)

    force_check = force_check or (proxy_url not in _PROXY_STATUS_CACHE.get("_checked", {}))
    with _proxy_lock:
        if not force_check and now - _PROXY_STATUS_CACHE.get("last_check_" + proxy_url, 0) < 10:
            return _PROXY_STATUS_CACHE.get("alive_" + proxy_url, True)

        _PROXY_STATUS_CACHE["last_check_" + proxy_url] = now
        _PROXY_STATUS_CACHE.setdefault("_checked", {})[proxy_url] = True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        with socket.create_connection((host, port), timeout=5):
            with _proxy_lock:
                _PROXY_STATUS_CACHE["alive_" + proxy_url] = True
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        with _proxy_lock:
            _PROXY_STATUS_CACHE["alive_" + proxy_url] = False
        logging.warning(f"Proxy {proxy_url} is NOT reachable.")
        return False


async def is_proxy_alive_async(proxy_url: str, force_check: bool = False) -> bool:
    """Async version of is_proxy_alive without blocking the event loop."""
    if not proxy_url:
        return False
    now = time.time()
    async with _proxy_async_lock:
        if proxy_url in DEAD_PROXIES:
            expire_time = DEAD_PROXIES[proxy_url]
            if now < expire_time:
                return False
            else:
                DEAD_PROXIES.pop(proxy_url, None)
    async with _proxy_async_lock:
        if not force_check and now - _PROXY_STATUS_CACHE.get("last_check_async_" + proxy_url, 0) < 10:
            return _PROXY_STATUS_CACHE.get("alive_async_" + proxy_url, True)
        _PROXY_STATUS_CACHE["last_check_async_" + proxy_url] = now
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _socket_check, proxy_url, 5)
        async with _proxy_async_lock:
            _PROXY_STATUS_CACHE["alive_async_" + proxy_url] = True
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        async with _proxy_async_lock:
            _PROXY_STATUS_CACHE["alive_async_" + proxy_url] = False
        logging.warning(f"Proxy {proxy_url} is NOT reachable.")
        return False


def _socket_check(proxy_url: str, timeout: float = 5) -> bool:
    """Synchronous socket check helper for run_in_executor."""
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1080
    with socket.create_connection((host, port), timeout=timeout):
        return True


def mark_proxy_dead(proxy_url: str, dead_duration: int = 300):
    """Manually mark a proxy as dead in the cache (e.g. after a failed request) for a period of time."""
    if not proxy_url:
        return

    _WARP_PROXY_URL = WARP_PROXY_URL
    if _WARP_PROXY_URL and proxy_url == _WARP_PROXY_URL:
        if "127.0.0.1" in proxy_url:
            with _proxy_lock:
                _PROXY_STATUS_CACHE["last_check"] = 0
        logging.warning("WARP proxy %s failure observed; keeping it managed by socket health checks.", proxy_url)
        return

    now = time.time()
    with _proxy_lock:
        DEAD_PROXIES[proxy_url] = now + dead_duration
    logging.warning(f"Proxy {proxy_url} marked as dead for {dead_duration} seconds.")

    if "127.0.0.1" in proxy_url:
        with _proxy_lock:
            _PROXY_STATUS_CACHE["alive"] = False
            _PROXY_STATUS_CACHE["last_check"] = now


_proxy_affinity: dict = {}

def clear_proxy_affinity():
    _proxy_affinity.clear()

def _get_stream_key(url: str) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Use the directory part as stream key
    if "/" in path:
        return parsed.netloc + path.rsplit("/", 1)[0]
    return parsed.netloc + path


def _next_from_source(current_proxy: str | None) -> str | None:
    """Find the next alive proxy from the same source list (extractor, proxy_file, etc.)."""
    source_list = PROXY_SOURCE_LIST.get()
    if not source_list:
        return None
    for p in source_list:
        if p != current_proxy and is_proxy_alive(p):
            return p
    return None


def get_proxy_for_url(url: str, transport_routes: list = None, global_proxies: list = None, bypass_warp: bool = None) -> str:
    """Trova il proxy appropriato per un URL basato su TRANSPORT_ROUTES e impostazioni WARP.
    
    If transport_routes or global_proxies are None, reads from dynamic config_store.
    """
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    
    _ENABLE_WARP = _get_dynamic_warp_enabled()
    _WARP_PROXY_URL = WARP_PROXY_URL
    _WARP_EXCLUDE_DOMAINS = _get_dynamic_warp_exclude_domains()
    if transport_routes is None:
        transport_routes = _get_dynamic_transport_routes()
    if global_proxies is None:
        global_proxies = _get_dynamic_global_proxies()

    if not url:
        selected_proxy = SELECTED_PROXY_CONTEXT.get()
        if selected_proxy and STRICT_PROXY_CONTEXT.get():
            return selected_proxy

    # Proxy affinity: keep the same proxy for the same stream
    stream_key = _get_stream_key(url)
    if stream_key and stream_key in _proxy_affinity:
        cached_proxy, timestamp = _proxy_affinity[stream_key]
        if time.time() - timestamp < 120 and is_proxy_alive(cached_proxy):
            # If cached proxy is WARP, validate WARP is still enabled
            if cached_proxy == _WARP_PROXY_URL:
                is_excluded = any(domain in url.lower() for domain in _WARP_EXCLUDE_DOMAINS)
                if _ENABLE_WARP and not bypass_warp and not is_excluded:
                    return cached_proxy
                # WARP no longer valid, remove from cache
                del _proxy_affinity[stream_key]
            else:
                return cached_proxy

    normalized_url = url.lower()

    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy and STRICT_PROXY_CONTEXT.get():
        return proxy

    if transport_routes:
        for route in transport_routes:
            url_pattern = route["url"].lower()
            if url_pattern in normalized_url:
                proxy_value = route.get("proxy")
                if not proxy_value:
                    return None
                if stream_key:
                    _proxy_affinity[stream_key] = (proxy_value, time.time())
                STRICT_PROXY_CONTEXT.set(True)
                SELECTED_PROXY_CONTEXT.set(proxy_value)
                return proxy_value

    # Explicit GLOBAL_PROXY wins over WARP. warp=off disables only WARP, not configured proxies.
    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy and is_proxy_alive(proxy):
        if stream_key:
            _proxy_affinity[stream_key] = (proxy, time.time())
        return proxy

    # Try next alive proxy from the same source list (extractor, proxy_file, etc.)
    proxy = _next_from_source(proxy)
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        return proxy

    proxy = random.choice(global_proxies) if global_proxies else None
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        STRICT_PROXY_CONTEXT.set(False)

    if proxy and is_proxy_alive(proxy):
        if stream_key:
            _proxy_affinity[stream_key] = (proxy, time.time())
        return proxy

    # Check if WARP should be used only when no explicit proxy is configured.
    is_excluded = any(domain in normalized_url for domain in _WARP_EXCLUDE_DOMAINS)

    if _ENABLE_WARP and not bypass_warp and not is_excluded:
        warp_alive = is_proxy_alive(_WARP_PROXY_URL)
        if warp_alive:
            if stream_key:
                _proxy_affinity[stream_key] = (_WARP_PROXY_URL, time.time())
            return _WARP_PROXY_URL
        return None

    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy and is_proxy_alive(proxy):
        if stream_key:
            _proxy_affinity[stream_key] = (proxy, time.time())
        return proxy

    proxy = _next_from_source(proxy)
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        if stream_key:
            _proxy_affinity[stream_key] = (proxy, time.time())
        return proxy

    proxy = random.choice(global_proxies) if global_proxies else None
    if proxy and is_proxy_alive(proxy):
        if stream_key:
            _proxy_affinity[stream_key] = (proxy, time.time())
        return proxy

    return None


def get_connector_for_proxy(proxy_url: str, **kwargs):
    """Crea un ProxyConnector (aiohttp-socks) gestendo socks5h e socks4a."""
    from aiohttp_socks import ProxyConnector

    if not proxy_url:
        return None

    connector_url = proxy_url
    rdns = kwargs.pop("rdns", False)

    if connector_url.startswith("socks5h://"):
        connector_url = connector_url.replace("socks5h://", "socks5://")
        rdns = True
    elif connector_url.startswith("socks4a://"):
        connector_url = connector_url.replace("socks4a://", "socks4://")
        rdns = True
    elif connector_url.startswith("socks4://"):
        rdns = False

    return ProxyConnector.from_url(connector_url, rdns=rdns, **kwargs)


def get_solver_proxy_url(proxy_url: str | None) -> str | None:
    """Normalizza il proxy per solver/browser che non supportano socks5h/socks4a."""
    if not proxy_url:
        return None

    if proxy_url.startswith("socks5h://"):
        return proxy_url.replace("socks5h://", "socks5://", 1)
    if proxy_url.startswith("socks4a://"):
        return proxy_url.replace("socks4a://", "socks4://", 1)

    return proxy_url


def build_proxy_with_auth(proxy_url: str | None) -> dict | None:
    """Converte un proxy URL in dict con username/password separati.

    Chromium (via Playwright/Scrapling/FlareSolverr) non supporta
    --proxy-server con credenziali nell'URL. Funziona solo se username
    e password sono campi separati.
    """
    if not proxy_url:
        return None
    clean = get_solver_proxy_url(proxy_url)
    result = {"url": clean}
    if "@" in clean:
        try:
            pp = urllib.parse.urlparse(clean)
            if pp.username and pp.password:
                result["username"] = pp.username
                result["password"] = pp.password
                result["url"] = f"{pp.scheme}://{pp.hostname}"
                if pp.port:
                    result["url"] += f":{pp.port}"
        except Exception:
            pass
    return result


def get_ssl_setting_for_url(url: str, transport_routes: list = None) -> bool:
    if transport_routes is None:
        transport_routes = _get_dynamic_transport_routes()
    """Determina se SSL deve essere disabilitato per un URL basato su TRANSPORT_ROUTES."""
    normalized_url = (url or "").lower()

    if "disable_ssl=1" in normalized_url:
        return True

    vavoo_domains = ("vavoo.to", "vavoo.tv", "vavoo", "lokke.app", "mediahubmx", "vixsrc.to", "vix-content.net", "/sunshine/")

    if not url or not transport_routes:
        return any(domain in normalized_url for domain in vavoo_domains)

    if any(domain in normalized_url for domain in vavoo_domains):
        return True

    for route in transport_routes:
        url_pattern = route["url"]
        if url_pattern in url:
            return route.get("disable_ssl", False)

    return False



API_PASSWORD = os.environ.get("API_PASSWORD")
PORT = int(os.environ.get("PORT", 7860))

# --- Version/Mode Configuration ---
APP_VERSION = "2.9.01"

_has_solvers = os.path.exists("flaresolverr")
VERSION_MODE = "Full" if _has_solvers else "Light"


def check_password(request):
    """Verifica la password API se impostata."""
    if not API_PASSWORD:
        return True

    api_password_param = request.query.get("api_password")
    if api_password_param == API_PASSWORD:
        return True

    if request.headers.get("x-api-password") == API_PASSWORD:
        return True

    # Cookie-based auth (set by /api/admin/login)
    if request.cookies.get("admin_token") == API_PASSWORD:
        return True

    return False


def reload_config():
    """Re-reads dynamic config from config_store.json into module-level names for backward compat."""
    # This is called after config_store changes to update module globals
    import sys
    mod = sys.modules[__name__]
    mod.ENABLE_WARP = _get_dynamic_warp_enabled()
    mod.WARP_EXCLUDE_DOMAINS = _get_dynamic_warp_exclude_domains()
    mod.GLOBAL_PROXIES = _get_dynamic_global_proxies()
    mod.TRANSPORT_ROUTES = _get_dynamic_transport_routes()
    mod.MPD_MODE = _cfg_get("mpd_mode", "legacy")
    mod.DVR_ENABLED = _cfg_get("dvr_enabled", False)
    mod.RECORDINGS_DIR = _cfg_get("recordings_dir", "/data/recordings")
    mod.MAX_RECORDING_DURATION = _cfg_get("max_recording_duration", 28800)
    mod.RECORDINGS_RETENTION_DAYS = _cfg_get("recordings_retention_days", 7)
    mod.FLARESOLVERR_URL = _cfg_get("flaresolverr_url", "http://localhost:8191")
    mod.FLARESOLVERR_TIMEOUT = _cfg_get("flaresolverr_timeout", 30)
    mod.ENABLE_REMUXING = _cfg_get("enable_remuxing", True)
    mod.PROXY_TEST_TIMEOUT = _cfg_get("proxy_test_timeout", 10)
    mod.PROXY_TEST_CONCURRENCY = _get_dynamic_proxy_test_concurrency()
    mod.SEGMENT_CACHE_TTL = _cfg_get("segment_cache_ttl", 30)
    mod.LOG_LEVEL_STR = _cfg_get("log_level", LOG_LEVEL_STR)
    _level = LOG_LEVEL_MAP.get(mod.LOG_LEVEL_STR.upper(), logging.WARNING)
    logging.getLogger().setLevel(_level)
    for _name in logging.root.manager.loggerDict:
        logging.getLogger(_name).setLevel(_level)
    for _handler in logging.getLogger().handlers:
        _handler.setLevel(_level)
    mod.WARP_LICENSE_KEY = _cfg_get("warp_license_key", "")


# Initialize module-level names with values from config_store
reload_config()


def __getattr__(name):
    """Dynamic attribute resolution for config values at module level.
    Allows `import config; config.ENABLE_WARP` to always return the current value.
    """
    _dynamic_attrs = {
        "ENABLE_WARP": _get_dynamic_warp_enabled,
        "WARP_EXCLUDE_DOMAINS": _get_dynamic_warp_exclude_domains,
        "GLOBAL_PROXIES": _get_dynamic_global_proxies,
        "TRANSPORT_ROUTES": _get_dynamic_transport_routes,
        "MPD_MODE": lambda: _cfg_get("mpd_mode", "legacy"),
        "DVR_ENABLED": lambda: _cfg_get("dvr_enabled", False),
        "RECORDINGS_DIR": lambda: _cfg_get("recordings_dir", "/data/recordings"),
        "MAX_RECORDING_DURATION": lambda: _cfg_get("max_recording_duration", 28800),
        "RECORDINGS_RETENTION_DAYS": lambda: _cfg_get("recordings_retention_days", 7),
        "FLARESOLVERR_URL": lambda: _cfg_get("flaresolverr_url", "http://localhost:8191"),
        "FLARESOLVERR_TIMEOUT": lambda: _cfg_get("flaresolverr_timeout", 30),
        "ENABLE_REMUXING": lambda: _cfg_get("enable_remuxing", True),
        "WARP_LICENSE_KEY": lambda: _cfg_get("warp_license_key", ""),
        "PROXY_TEST_TIMEOUT": lambda: int(_cfg_get("proxy_test_timeout", 10)),
        "PROXY_TEST_CONCURRENCY": _get_dynamic_proxy_test_concurrency,
        "SEGMENT_CACHE_TTL": lambda: int(_cfg_get("segment_cache_ttl", 30)),
        "LOG_LEVEL_STR": lambda: str(_cfg_get("log_level", "WARNING")),
    }
    getter = _dynamic_attrs.get(name)
    if getter:
        return getter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
