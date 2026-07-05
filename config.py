import os
import shutil
import logging
import random
import socket
import time
import asyncio
import contextvars
import urllib.request
from dotenv import load_dotenv
from config_store import get as _cfg_get, set as _cfg_set, get_all as _cfg_get_all

APP_VERSION = "2.9.52"


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
        return proxies
    except Exception as e:
        logger.warning(f"Error reading proxy source {source}: {e}")
        return []

# ContextVar for thread-safe/async-safe warp bypass state
BYPASS_WARP_CONTEXT = contextvars.ContextVar("bypass_warp", default=False)
BYPASS_PROXIES_CONTEXT = contextvars.ContextVar("bypass_proxies", default=False)
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
    """Test proxies in priority order with a staggered start, returning the highest-priority alive proxy."""
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
    
    loop = asyncio.get_event_loop()
    tasks = []
    
    for i, p in enumerate(proxies):
        if not p:
            continue
            
        async def _check_single(proxy_url=p, idx=i):
            try:
                await loop.run_in_executor(None, _socket_check, proxy_url, 3)
                return idx, proxy_url
            except (OSError, socket.timeout):
                return idx, None

        t = asyncio.create_task(_check_single())
        tasks.append(t)
        
        # Wait up to 250ms to give higher-priority proxies a head start to complete
        start_time = time.time()
        succeeded_high_priority = False
        while time.time() - start_time < 0.25:
            done_tasks = [tk for tk in tasks if tk.done()]
            results = []
            for tk in done_tasks:
                res_idx, res_val = tk.result()
                if res_val is not None:
                    results.append((res_idx, res_val))
            if results:
                results.sort(key=lambda x: x[0])
                best_idx, best_proxy = results[0]
                if best_idx == 0:
                    succeeded_high_priority = True
                    break
            await asyncio.sleep(0.02)
            
        if succeeded_high_priority:
            break

    # Gather all launched tasks
    results = await asyncio.gather(*tasks, return_exceptions=True)
    succeeded = []
    for r in results:
        if isinstance(r, tuple):
            idx, res = r
            if res is not None:
                succeeded.append((idx, res))
                
    # Cancel any remaining pending tasks
    for t in tasks:
        if not t.done():
            t.cancel()

    if succeeded:
        succeeded.sort(key=lambda x: x[0])
        return succeeded[0][1]
        
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

def _is_warp_excluded(url: str) -> bool:
    normalized = url.lower()
    for domain in WARP_EXCLUDE_DOMAINS:
        stripped = domain.lstrip("*.")
        if stripped in normalized:
            return True
    return False

def _get_dynamic_proxy_exclude_domains() -> list:
    return _cfg_get("proxy_exclude_domains", [])

def _is_proxy_excluded(url: str) -> bool:
    if not url:
        return False
    normalized = url.lower()
    for domain in PROXY_EXCLUDE_DOMAINS:
        stripped = domain.lstrip("*.")
        if stripped in normalized:
            return True
    return False

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
    bypass_proxies: bool | None = None,
) -> list[str]:
    """Build proxy priority: extractor-specific, TRANSPORT_ROUTES, fallback/global, WARP."""
    if bypass_proxies is None:
        bypass_proxies = BYPASS_PROXIES_CONTEXT.get() or _is_proxy_excluded(url or "")

    _ENABLE_WARP = _get_dynamic_warp_enabled()
    _WARP_PROXY_URL = WARP_PROXY_URL
    
    if bypass_proxies:
        ordered = []
        if bypass_warp is None:
            bypass_warp = BYPASS_WARP_CONTEXT.get()
        is_excluded = _is_warp_excluded(url or "")
        if _ENABLE_WARP and not bypass_warp and not is_excluded:
            ordered.append(_WARP_PROXY_URL)
        return ProxyList(ordered, strict=False)

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
    is_excluded = _is_warp_excluded(url or "")
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


DEAD_PROXIES = {}  # proxy_url -> expire_time
_proxy_lock = __import__('threading').Lock()  # sync access to DEAD_PROXIES
_proxy_async_lock = asyncio.Lock()


def is_proxy_alive(proxy_url: str, force_check: bool = False) -> bool:
    """Checks if a proxy is reachable and not marked dead globally."""
    if not proxy_url:
        return False

    now = time.time()
    with _proxy_lock:
        if proxy_url in DEAD_PROXIES:
            expire_time = DEAD_PROXIES[proxy_url]
            if now < expire_time:
                return False
            DEAD_PROXIES.pop(proxy_url, None)

    if not _socket_check(proxy_url, timeout=5):
        logging.warning(f"Proxy {proxy_url} is NOT reachable.")
        return False
    return True


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
            DEAD_PROXIES.pop(proxy_url, None)
    loop = asyncio.get_event_loop()
    try:
        alive = await loop.run_in_executor(None, _socket_check, proxy_url, 5)
        if not alive:
            raise OSError("Proxy check returned false")
    except (socket.timeout, ConnectionRefusedError, OSError):
        logging.warning(f"Proxy {proxy_url} is NOT reachable.")
        return False
    return True


def _socks5_greeting(host: str, port: int, timeout: float = 5) -> bool:
    """Perform SOCKS5 greeting handshake to verify proxy speaks SOCKS5."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        # greeting: version=5, 1 auth method, no-auth=0
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        return len(resp) == 2 and resp[0] == 0x05 and resp[1] == 0x00
    except OSError:
        return False
    finally:
        sock.close()


def _socket_check(proxy_url: str, timeout: float = 5) -> bool:
    """Synchronous socket check helper for run_in_executor."""
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1080
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        return _socks5_greeting(host, port, timeout)
    with socket.create_connection((host, port), timeout=timeout):
        return True


def mark_proxy_dead(proxy_url: str, dead_duration: int = 300):
    """Manually mark a proxy as dead in the cache (e.g. after a failed request) for a period of time."""
    if not proxy_url:
        return

    _WARP_PROXY_URL = WARP_PROXY_URL
    if _WARP_PROXY_URL and proxy_url == _WARP_PROXY_URL:
        logging.warning("WARP proxy %s failure observed; keeping it managed by socket health checks.", proxy_url)
        return

    # If this is the only custom proxy configured in the system, do not mark it dead.
    # We want to keep trying to use it on subsequent requests.
    try:
        global_proxies = _get_dynamic_global_proxies()
        extractor_proxies = _cfg_get("extractor_proxies", {})
        transport_routes = _get_dynamic_transport_routes()
        
        extractor_list = []
        for val in extractor_proxies.values():
            if isinstance(val, str):
                extractor_list.append(val)
            elif isinstance(val, list):
                extractor_list.extend(val)
                
        transport_list = []
        for route in transport_routes:
            if isinstance(route, dict):
                p_val = route.get("proxy")
                if p_val:
                    transport_list.append(p_val)
                    
        custom_pool = {p for p in (global_proxies + extractor_list + transport_list) if p}
        if len(custom_pool) <= 1:
            logging.info("Proxy %s failed, but it is the only custom proxy configured. Not marking dead.", proxy_url)
            return
    except Exception:
        pass

    now = time.time()
    with _proxy_lock:
        DEAD_PROXIES[proxy_url] = now + dead_duration
    logging.warning(f"Proxy {proxy_url} marked as dead for {dead_duration} seconds.")


def clear_proxy_affinity():
    pass


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


def get_proxy_for_url(
    url: str,
    transport_routes: list = None,
    global_proxies: list = None,
    bypass_warp: bool = None,
    bypass_proxies: bool = None,
) -> str:
    """Trova il proxy appropriato per un URL basato su TRANSPORT_ROUTES e impostazioni WARP.
    
    If transport_routes or global_proxies are None, reads from dynamic config_store.
    """
    if bypass_proxies is None:
        bypass_proxies = BYPASS_PROXIES_CONTEXT.get() or _is_proxy_excluded(url or "")

    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    
    _ENABLE_WARP = _get_dynamic_warp_enabled()
    _WARP_PROXY_URL = WARP_PROXY_URL

    if bypass_proxies:
        is_excluded = _is_warp_excluded(url) if url else False
        if _ENABLE_WARP and not bypass_warp and not is_excluded:
            warp_alive = is_proxy_alive(_WARP_PROXY_URL)
            if warp_alive:
                return _WARP_PROXY_URL
        return None

    _WARP_EXCLUDE_DOMAINS = _get_dynamic_warp_exclude_domains()
    if transport_routes is None:
        transport_routes = _get_dynamic_transport_routes()
    if global_proxies is None:
        global_proxies = _get_dynamic_global_proxies()

    if not url:
        selected_proxy = SELECTED_PROXY_CONTEXT.get()
        if selected_proxy and STRICT_PROXY_CONTEXT.get():
            return selected_proxy

    stream_key = _get_stream_key(url)

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
                STRICT_PROXY_CONTEXT.set(True)
                SELECTED_PROXY_CONTEXT.set(proxy_value)
                return proxy_value

    # Explicit GLOBAL_PROXY wins over WARP. warp=off disables only WARP, not configured proxies.
    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy and is_proxy_alive(proxy):
        # ✅ FIX: Se bypass_warp=True e il proxy selezionato è WARP, saltalo.
        # get_preferred_proxy_for_url (chiamato dagli estrattori) setta
        # SELECTED_PROXY_CONTEXT a WARP, ma bypass_warp deve avere la priorità.
        if bypass_warp and _WARP_PROXY_URL and proxy == _WARP_PROXY_URL:
            logger.debug(
                "Skipping WARP from SELECTED_PROXY_CONTEXT because bypass_warp=True"
            )
        else:
            return proxy

    # Try next alive proxy from the same source list (extractor, proxy_file, etc.)
    proxy = _next_from_source(proxy)
    if proxy:
        # ✅ FIX: Se bypass_warp=True e il proxy è WARP, saltalo e continua
        if bypass_warp and _WARP_PROXY_URL and proxy == _WARP_PROXY_URL:
            logger.debug(
                "Skipping WARP from _next_from_source because bypass_warp=True"
            )
        else:
            SELECTED_PROXY_CONTEXT.set(proxy)
            return proxy

    proxy = random.choice(global_proxies) if global_proxies else None
    # ✅ FIX: Se bypass_warp=True e il proxy pescato da GLOBAL_PROXIES è WARP, ignoriamo
    if bypass_warp and _WARP_PROXY_URL and proxy == _WARP_PROXY_URL:
        proxy = None
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        STRICT_PROXY_CONTEXT.set(False)

    if proxy and is_proxy_alive(proxy):
        return proxy

    # Check if WARP should be used only when no explicit proxy is configured.
    is_excluded = _is_warp_excluded(url)

    if _ENABLE_WARP and not bypass_warp and not is_excluded:
        warp_alive = is_proxy_alive(_WARP_PROXY_URL)
        if warp_alive:
            return _WARP_PROXY_URL
        return None

    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy and is_proxy_alive(proxy):
        return proxy

    proxy = _next_from_source(proxy)
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        return proxy

    proxy = random.choice(global_proxies) if global_proxies else None
    if proxy and is_proxy_alive(proxy):
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

    vavoo_domains = ("vavoo.to", "vavoo.tv", "vavoo", "lokke.app", "mediahubmx", "vixsrc.to", "vix-content.net", "/sunshine/", "calpezz8.space")

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


def get_client_ip(request):
    """Recupera l'IP reale del client, supportando Cloudflare e reverse proxy."""
    # Cloudflare
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    # True-Client-IP (Cloudflare Enterprise / Akamai)
    true_ip = request.headers.get("True-Client-IP")
    if true_ip:
        return true_ip.strip()

    # X-Forwarded-For (standard per reverse proxy)
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # Prende il primo IP della catena (quello originale del client)
        parts = [p.strip() for p in xff.split(",")]
        if parts and parts[0]:
            return parts[0]

    # X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fallback all'indirizzo remoto della richiesta aiohttp
    return request.remote



def reload_config():
    """Re-reads dynamic config from config_store.json into module-level names for backward compat."""
    # This is called after config_store changes to update module globals
    import sys
    mod = sys.modules[__name__]
    mod.ENABLE_WARP = _get_dynamic_warp_enabled()
    mod.WARP_EXCLUDE_DOMAINS = _get_dynamic_warp_exclude_domains()
    mod.PROXY_EXCLUDE_DOMAINS = _get_dynamic_proxy_exclude_domains()
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
        "PROXY_EXCLUDE_DOMAINS": _get_dynamic_proxy_exclude_domains,
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
        "LOG_LEVEL_STR": lambda: str(_cfg_get("log_level", "WARNING")),
    }
    getter = _dynamic_attrs.get(name)
    if getter:
        return getter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_system_stats():
    # Disk Usage
    rec_dir = _cfg_get("recordings_dir", "/data/recordings")
    try:
        os.makedirs(rec_dir, exist_ok=True)
        disk_total, disk_used, disk_free = shutil.disk_usage(rec_dir)
        disk_percent = (disk_used / disk_total) * 100 if disk_total > 0 else 0
    except Exception as e:
        logger.warning(f"Error getting disk usage: {e}")
        disk_total, disk_used, disk_free, disk_percent = 0, 0, 0, 0

    # CPU & RAM Usage (using psutil with fallback)
    cpu_percent = 0.0
    ram_percent = 0.0
    ram_total = 0
    ram_used = 0
    ram_free = 0
    
    # Check if we are running inside Docker and have cgroup memory limits
    docker_used, docker_limit = None, None
    try:
        # cgroup v2 (Unified Hierarchy)
        if os.path.exists("/sys/fs/cgroup/memory.max") and os.path.exists("/sys/fs/cgroup/memory.current"):
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                val = f.read().strip()
                if val != "max":
                    docker_limit = int(val)
            with open("/sys/fs/cgroup/memory.current", "r") as f:
                docker_used = int(f.read().strip())
        # cgroup v1 (Legacy Hierarchy)
        elif os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes") and os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                docker_limit = int(f.read().strip())
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
                docker_used = int(f.read().strip())
        
        # Verify container limits are not infinite/max value (like 9223372036854771712 or 9223372036854775807)
        if docker_limit and docker_limit > 9000000000000000000:
            docker_limit = None
    except Exception:
        pass

    net_sent = 0
    net_recv = 0
    try:
        import psutil
        cpu_percent = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        ram_total = mem.total
        ram_used = mem.used
        ram_free = mem.available
        ram_percent = mem.percent
        net = psutil.net_io_counters()
        _now = time.time()
        _prev = getattr(get_system_stats, "_net_prev", None)
        _prev_ts = getattr(get_system_stats, "_net_prev_ts", None)
        if _prev and _prev_ts and _now - _prev_ts > 0:
            dt = _now - _prev_ts
            net_sent = max(0, (net.bytes_sent - _prev[0]) / dt)
            net_recv = max(0, (net.bytes_recv - _prev[1]) / dt)
        get_system_stats._net_prev = (net.bytes_sent, net.bytes_recv)
        get_system_stats._net_prev_ts = _now
    except Exception as e:
        logger.debug(f"psutil not available or error: {e}")
        try:
            if os.path.exists("/proc/meminfo"):
                meminfo = {}
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            meminfo[parts[0].replace(":", "")] = int(parts[1]) * 1024
                ram_total = meminfo.get("MemTotal", 0)
                ram_free = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
                ram_used = ram_total - ram_free
                ram_percent = (ram_used / ram_total) * 100 if ram_total > 0 else 0
            
            if os.path.exists("/proc/loadavg"):
                with open("/proc/loadavg", "r") as f:
                    load = f.readline().split()
                    cpu_percent = float(load[0]) * 100 / (os.cpu_count() or 1)
                    if cpu_percent > 100.0:
                        cpu_percent = 100.0
        except Exception:
            pass

    # Apply Docker container cgroup limits if valid
    if docker_used is not None and docker_limit is not None:
        ram_total = docker_limit
        ram_used = docker_used
        ram_free = max(0, docker_limit - docker_used)
        ram_percent = (ram_used / ram_total) * 100 if ram_total > 0 else 0

    # EasyProxy process RAM (including child processes like ffmpeg, chrome, etc.)
    proxy_ram_used = ram_used
    proxy_ram_total = ram_total
    proxy_ram_percent = ram_percent
    try:
        proc = psutil.Process(os.getpid())
        proxy_ram_used = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                proxy_ram_used += child.memory_info().rss
            except Exception:
                pass
        proxy_ram_total = ram_total
        proxy_ram_percent = (proxy_ram_used / proxy_ram_total) * 100 if proxy_ram_total > 0 else 0
    except Exception:
        pass

    # EasyProxy process CPU (including child processes)
    proxy_cpu_percent = cpu_percent
    try:
        # Use persistent Process objects: psutil.cpu_percent() needs a previous
        # baseline reading, otherwise it always returns 0.0.
        _cpu_proc = getattr(get_system_stats, "_cpu_proc", None)
        if _cpu_proc is None:
            _cpu_proc = psutil.Process(os.getpid())
            get_system_stats._cpu_proc = _cpu_proc
            _cpu_proc.cpu_percent(interval=None)  # establish baseline

        _cpu_children = getattr(get_system_stats, "_cpu_children", {})
        current_children = {c.pid: c for c in _cpu_proc.children(recursive=True)}
        # Drop dead children and baseline new ones
        for pid in list(_cpu_children.keys()):
            if pid not in current_children:
                del _cpu_children[pid]
        for pid, child in current_children.items():
            if pid not in _cpu_children:
                _cpu_children[pid] = child
                child.cpu_percent(interval=None)  # establish baseline

        p_cpu = _cpu_proc.cpu_percent(interval=None)
        for child in _cpu_children.values():
            try:
                p_cpu += child.cpu_percent(interval=None)
            except Exception:
                pass
        get_system_stats._cpu_children = _cpu_children

        cores = os.cpu_count() or 1
        proxy_cpu_percent = min(100.0, p_cpu / cores)
    except Exception:
        pass

    return {
        "disk": {
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "percent": round(disk_percent, 1)
        },
        "cpu": {
            "percent": round(cpu_percent, 1)
        },
        "proxy_cpu": {
            "percent": round(proxy_cpu_percent, 1)
        },
        "ram": {
            "total": ram_total,
            "used": ram_used,
            "free": ram_free,
            "percent": round(ram_percent, 1)
        },
        "proxy_ram": {
            "total": proxy_ram_total,
            "used": proxy_ram_used,
            "free": max(0, proxy_ram_total - proxy_ram_used),
            "percent": round(proxy_ram_percent, 1)
        },
        "net": {
            "sent": round(net_sent, 1),
            "recv": round(net_recv, 1)
        }
    }

