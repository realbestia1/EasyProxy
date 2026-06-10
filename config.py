import os
import logging
import random
import socket
import time
import asyncio
import contextvars
import urllib.request
from dotenv import load_dotenv

_proxy_file_cache: dict[str, tuple[float, list]] = {}
_PROXY_FILE_TTL = 600

# ContextVar for thread-safe/async-safe warp bypass state
BYPASS_WARP_CONTEXT = contextvars.ContextVar("bypass_warp", default=False)
SELECTED_PROXY_CONTEXT = contextvars.ContextVar("selected_proxy", default=None)
STRICT_PROXY_CONTEXT = contextvars.ContextVar("strict_proxy", default=False)
PROXY_SOURCE_LIST = contextvars.ContextVar("proxy_source_list", default=None)

load_dotenv()

# --- Log Level Configuration ---
LOG_LEVEL_STR = os.environ.get("LOG_LEVEL", "WARNING").upper()
LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
LOG_LEVEL = LOG_LEVEL_MAP.get(LOG_LEVEL_STR, logging.WARNING)
PROXY_TEST_TIMEOUT = int(os.environ.get("PROXY_TEST_TIMEOUT", "10"))
cpu_cores = os.cpu_count() or 4
default_concurrency = 10 if cpu_cores == 1 else min(100, max(30, cpu_cores * 15))
PROXY_TEST_CONCURRENCY = max(1, int(os.environ.get("PROXY_TEST_CONCURRENCY", str(default_concurrency))))

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


def _strip_env_assignment(value: str, env_var: str) -> str:
    prefix = f"{env_var}="
    return value[len(prefix):].strip() if value.startswith(prefix) else value


def parse_proxies(proxy_env_var: str) -> list:
    """Analizza una stringa di proxy separati da virgola da una variabile d'ambiente."""
    proxies_str = _strip_env_assignment(os.environ.get(proxy_env_var, "").strip(), proxy_env_var)
    if proxies_str:
        proxies = []
        for proxy in proxies_str.split(","):
            proxy = proxy.strip()
            if proxy.startswith("="):
                proxy = proxy[1:].strip()
            if proxy:
                proxies.append(proxy)
        return proxies
    return []


def parse_proxy_file(proxy_file_env_var: str) -> list:
    """Read proxies from comma-separated file paths/URLs, one proxy per line. Cached for 10 min."""
    raw = _strip_env_assignment(os.environ.get(proxy_file_env_var, "").strip(), proxy_file_env_var)
    if not raw:
        return []
    now = time.time()
    cached = _proxy_file_cache.get(raw)
    if cached and (now - cached[0]) < _PROXY_FILE_TTL:
        return cached[1]
    proxies = []
    for path in raw.split(","):
        path = path.strip()
        if not path:
            continue
        try:
            if path.startswith(("http://", "https://")):
                with urllib.request.urlopen(path, timeout=10) as response:
                    text = response.read().decode("utf-8", errors="ignore")
            else:
                with open(path, "r", encoding="utf-8") as file:
                    text = file.read()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("="):
                    line = line[1:].strip()
                if not line or line.startswith("#"):
                    continue
                if line not in proxies:
                    proxies.append(line)
        except Exception as e:
            logger.warning(f"Error reading proxy file {path}: {e}")
    _proxy_file_cache[raw] = (now, proxies)
    return proxies


def get_extractor_proxies(extractor_name: str) -> list:
    """Returns proxies from EXTRACTOR_PROXY and EXTRACTOR_PROXY_FILE env vars."""
    if not extractor_name:
        return []
    prefix = extractor_name.upper().replace('-', '_')
    proxies = []
    for proxy in parse_proxies(f"{prefix}_PROXY") + parse_proxy_file(f"{prefix}_PROXY_FILE"):
        if proxy and proxy not in proxies:
            proxies.append(proxy)
    return proxies


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

    selected_proxy = SELECTED_PROXY_CONTEXT.get()
    selected_proxy_is_strict = STRICT_PROXY_CONTEXT.get()
    if selected_proxy and selected_proxy_is_strict:
        return build([selected_proxy], strict=True)

    extractor_proxies = get_extractor_proxies(extractor_name or "")
    if extractor_proxies:
        return build(extractor_proxies, strict=True)

    if url and TRANSPORT_ROUTES:
        normalized_url = url.lower()
        for route in TRANSPORT_ROUTES:
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

    for proxy in GLOBAL_PROXIES:
        add(proxy)

    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    normalized_url = (url or "").lower()
    is_excluded = any(domain in normalized_url for domain in WARP_EXCLUDE_DOMAINS)
    if ENABLE_WARP and not bypass_warp and not is_excluded:
        add(WARP_PROXY_URL)

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


def parse_transport_routes() -> list:
    """Analizza TRANSPORT_ROUTES nel formato {URL=domain, PROXY=proxy, DISABLE_SSL=true/false}."""
    routes_str = os.environ.get("TRANSPORT_ROUTES", "").strip()
    if not routes_str:
        return []

    routes = []
    try:
        route_parts = [part.strip() for part in routes_str.replace(" ", "").split("},{")]

        for part in route_parts:
            if not part:
                continue

            part = part.strip("{}")

            url_match = None
            proxy_match = None
            disable_ssl_match = None

            for item in part.split(","):
                if item.startswith("URL="):
                    url_match = item[4:]
                elif item.startswith("PROXY="):
                    proxy_match = item[6:]
                elif item.startswith("DISABLE_SSL="):
                    disable_ssl_str = item[12:].lower()
                    disable_ssl_match = disable_ssl_str in ("true", "1", "yes", "on")

            if url_match:
                routes.append(
                    {
                        "url": url_match,
                        "proxy": proxy_match if proxy_match else None,
                        "disable_ssl": disable_ssl_match if disable_ssl_match is not None else False,
                    }
                )

    except Exception as e:
        logger.warning(f"Error parsing TRANSPORT_ROUTES: {e}")

    return routes


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

    if WARP_PROXY_URL and proxy_url == WARP_PROXY_URL:
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


def get_proxy_for_url(url: str, transport_routes: list, global_proxies: list, bypass_warp: bool = None) -> str:
    """Trova il proxy appropriato per un URL basato su TRANSPORT_ROUTES e impostazioni WARP."""
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    if not url:
        selected_proxy = SELECTED_PROXY_CONTEXT.get()
        if selected_proxy and STRICT_PROXY_CONTEXT.get():
            return selected_proxy

    # Proxy affinity: keep the same proxy for the same stream
    stream_key = _get_stream_key(url)
    if stream_key and stream_key in _proxy_affinity:
        cached_proxy, timestamp = _proxy_affinity[stream_key]
        if time.time() - timestamp < 120 and is_proxy_alive(cached_proxy):
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
    is_excluded = any(domain in normalized_url for domain in WARP_EXCLUDE_DOMAINS)

    if ENABLE_WARP and not bypass_warp and not is_excluded:
        warp_alive = is_proxy_alive(WARP_PROXY_URL)
        if warp_alive:
            if stream_key:
                _proxy_affinity[stream_key] = (WARP_PROXY_URL, time.time())
            return WARP_PROXY_URL
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


def get_ssl_setting_for_url(url: str, transport_routes: list) -> bool:
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



_warp_env = os.environ.get("ENABLE_WARP", "").strip().lower()
WARP_PROXY_URL = os.environ.get("WARP_PROXY_URL", "").strip() or "socks5h://127.0.0.1:1080"
if _warp_env == "true":
    ENABLE_WARP = True
elif _warp_env == "false":
    ENABLE_WARP = False
else:
    ENABLE_WARP = False
    try:
        import urllib.request as _ur
        req = _ur.Request("https://www.cloudflare.com/cdn-cgi/trace",
                          headers={"User-Agent": "curl/8.0"})
        body = _ur.urlopen(req, timeout=3).read().decode()
        if "warp=on" in body:
            ENABLE_WARP = True
    except Exception as _warp_e:
        import logging as _warp_log
        _warp_log.getLogger("config").debug("WARP auto-detect failed: %s", _warp_e)

_default_warp_exclude_domains = [
    "strem.fun",
    "*.strem.fun",
    "torrentio.strem.fun",
    "real-debrid.com",
    "*.real-debrid.com",
    "realdebrid.com",
    "*.realdebrid.com",
    "api.real-debrid.com",
    "premiumize.me",
    "*.premiumize.me",
    "www.premiumize.me",
    "alldebrid.com",
    "*.alldebrid.com",
    "api.alldebrid.com",
    "debrid-link.com",
    "*.debrid-link.com",
    "debridlink.com",
    "*.debridlink.com",
    "api.debrid-link.com",
    "torbox.app",
    "*.torbox.app",
    "api.torbox.app",
    "offcloud.com",
    "*.offcloud.com",
    "api.offcloud.com",
    "put.io",
    "*.put.io",
    "api.put.io",
]
WARP_EXCLUDE_DOMAINS = [
    domain.strip().lower()
    for domain in os.environ.get("WARP_EXCLUDED_HOSTS", ",".join(_default_warp_exclude_domains)).split(",")
    if domain.strip()
]

GLOBAL_PROXIES = parse_proxies("GLOBAL_PROXY")
TRANSPORT_ROUTES = parse_transport_routes()

if GLOBAL_PROXIES:
    logging.info(f"Loaded {len(GLOBAL_PROXIES)} global proxies.")
if TRANSPORT_ROUTES:
    logging.info(f"Loaded {len(TRANSPORT_ROUTES)} transport rules.")

API_PASSWORD = os.environ.get("API_PASSWORD")
PORT = int(os.environ.get("PORT", 7860))

# --- Recording/DVR Configuration ---
DVR_ENABLED = os.environ.get("DVR_ENABLED", "false").lower() in ("true", "1", "yes")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "recordings")
MAX_RECORDING_DURATION = int(os.environ.get("MAX_RECORDING_DURATION", 28800))
RECORDINGS_RETENTION_DAYS = int(os.environ.get("RECORDINGS_RETENTION_DAYS", 7))

# --- Version/Mode Configuration ---
APP_VERSION = "2.8.14"

_has_solvers = os.path.exists("flaresolverr")
VERSION_MODE = "Full" if _has_solvers else "Light"

if DVR_ENABLED and not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)
    logging.info(f"Created recordings directory: {RECORDINGS_DIR}")

_mpd_mode_env = os.environ.get("MPD_MODE", "legacy").lower()

if _mpd_mode_env in ("ffmpeg", "legacy", "none", "disabled"):
    MPD_MODE = _mpd_mode_env
else:
    logging.warning(f"MPD_MODE '{_mpd_mode_env}' non valida. Uso 'legacy'.")
    MPD_MODE = "legacy"

ENABLE_REMUXING = os.environ.get("ENABLE_REMUXING", "true").lower() in ("true", "1", "yes")
if MPD_MODE in ("none", "disabled"):
    ENABLE_REMUXING = False

if "MPD_MODE" in os.environ:
    logging.info(f"MPD Mode: {MPD_MODE} (Remuxing: {'ON' if ENABLE_REMUXING else 'OFF'})")

# --- FlareSolverr Configuration ---
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191").rstrip("/")
FLARESOLVERR_TIMEOUT = int(os.environ.get("FLARESOLVERR_TIMEOUT", 30))


def check_password(request):
    """Verifica la password API se impostata."""
    if not API_PASSWORD:
        return True

    api_password_param = request.query.get("api_password")
    if api_password_param == API_PASSWORD:
        return True

    if request.headers.get("x-api-password") == API_PASSWORD:
        return True

    return False
