import os
import shutil
import logging
import socket
import time
import asyncio
import contextvars
import tracemalloc
import urllib.request
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from config_store import (
    DEFAULT_RECORDINGS_DIR,
    get as _cfg_get,
    set as _cfg_set,
    get_all as _cfg_get_all,
)
from aiohttp_socks import (
    ProxyConnector,
    ProxyError as AioProxyError,
    ProxyConnectionError as AioProxyConnectionError,
    ProxyTimeoutError as AioProxyTimeoutError,
)
from python_socks import (
    ProxyError as PyProxyError,
    ProxyConnectionError as PyProxyConnectionError,
    ProxyTimeoutError as PyProxyTimeoutError,
)
ALL_PROXY_ERRORS = (
    AioProxyError,
    AioProxyConnectionError,
    AioProxyTimeoutError,
    PyProxyError,
    PyProxyConnectionError,
    PyProxyTimeoutError,
)


# Proxy/WARP socket probes are blocking by design. Keep them out of asyncio's
# default executor so DNS resolution and media/DRM work are not queued behind
# a health check that is waiting on a dead proxy.
_SOCKET_CHECK_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="proxy-health",
)


APP_VERSION = "2.13.20"

_MEMORY_PROFILE_FRAMES = 15
_memory_profile_baseline = None
_memory_profile_baseline_at = None


def start_memory_profiler() -> dict:
    """Start tracemalloc and save one baseline for leak investigation."""
    global _memory_profile_baseline, _memory_profile_baseline_at
    if not tracemalloc.is_tracing():
        tracemalloc.start(_MEMORY_PROFILE_FRAMES)
    if _memory_profile_baseline is None:
        _memory_profile_baseline = tracemalloc.take_snapshot()
        _memory_profile_baseline_at = time.time()
    current, peak = tracemalloc.get_traced_memory()
    return {
        "enabled": True,
        "frames": _MEMORY_PROFILE_FRAMES,
        "baseline_at": _memory_profile_baseline_at,
        "current": current,
        "peak": peak,
    }


def reset_memory_profiler() -> dict:
    """Replace the profiler baseline with the current live allocations."""
    global _memory_profile_baseline, _memory_profile_baseline_at
    if not tracemalloc.is_tracing():
        tracemalloc.start(_MEMORY_PROFILE_FRAMES)
    _memory_profile_baseline = tracemalloc.take_snapshot()
    _memory_profile_baseline_at = time.time()
    tracemalloc.reset_peak()
    current, peak = tracemalloc.get_traced_memory()
    return {
        "enabled": True,
        "frames": _MEMORY_PROFILE_FRAMES,
        "baseline_at": _memory_profile_baseline_at,
        "current": current,
        "peak": peak,
    }


def _memory_profile_stat(stat) -> dict:
    traceback = stat.traceback
    frame = traceback[-1] if traceback else None
    filename = frame.filename if frame else None
    line = frame.lineno if frame else None
    return {
        "location": f"{filename}:{line}" if filename and line else "unknown",
        "file": filename,
        "line": line,
        "size": stat.size,
        "size_mb": round(stat.size / (1024 * 1024), 3),
        "count": stat.count,
        "traceback": traceback.format()[-5:],
    }


def get_memory_profile(limit: int = 30) -> dict:
    """Return top Python allocations and growth since the startup baseline."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))

    start_memory_profiler()
    current_snapshot = tracemalloc.take_snapshot()
    current, peak = tracemalloc.get_traced_memory()
    baseline = _memory_profile_baseline
    growth = current_snapshot.compare_to(baseline, "lineno") if baseline else []

    return {
        "enabled": True,
        "frames": _MEMORY_PROFILE_FRAMES,
        "baseline_at": _memory_profile_baseline_at,
        "baseline_age_seconds": round(time.time() - _memory_profile_baseline_at, 1) if _memory_profile_baseline_at else None,
        "current": current,
        "current_mb": round(current / (1024 * 1024), 3),
        "peak": peak,
        "peak_mb": round(peak / (1024 * 1024), 3),
        "top_current": [_memory_profile_stat(stat) for stat in current_snapshot.statistics("lineno")[:limit]],
        "top_growth": [_memory_profile_stat(stat) for stat in growth[:limit]],
        "note": "tracemalloc misura solo allocazioni Python; RSS nativo e processi figli sono in /api/info.",
    }


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
BYPASS_TOR_CONTEXT = contextvars.ContextVar("bypass_tor", default=False)
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
# Keep WARP as a normal SOCKS route. The generated WireGuard profile and
# wireproxy decide which address family is usable for each destination.
WARP_PROXY_URL = "socks5://127.0.0.1:1080"
# Monotonic timestamp of the last real WARP connector use. Health probes do
# not update it; EasyProxy uses it to recycle WireProxy only after true idle.
WARP_LAST_ACTIVITY = 0.0

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
                await loop.run_in_executor(_SOCKET_CHECK_EXECUTOR, _socket_check, proxy_url, 3)
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
                await loop.run_in_executor(_SOCKET_CHECK_EXECUTOR, _socket_check, proxy, 2)
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

def should_force_max_res(
    extractor_key: str = "",
    requested: bool = False,
    source: str = "",
    proxy_endpoint: bool = False,
) -> bool:
    """Decide whether only the highest video variant must be served.

    `requested` is the per-request &max_res flag, `source` is "mpd" or "hls".
    Requests that belong to an extractor only honour the per-extractor list;
    the MPD/HLS switches cover direct /proxy/mpd and /proxy/hls requests.
    """
    if requested:
        return True
    name = str(extractor_key or "").strip().lower()
    name = name.replace("_direct", "").replace("_noproxy", "")
    if name:
        forced = {
            str(item).strip().lower()
            for item in (_cfg_get("max_res_extractors", []) or [])
        }
        return name in forced
    if not proxy_endpoint:
        return False
    if source == "mpd":
        return bool(_cfg_get("max_res_mpd", False))
    if source == "hls":
        return bool(_cfg_get("max_res_hls", False))
    return False

def is_direct_connection_allowed(bypass_warp: bool | None = None) -> bool:
    """Allow direct only when WARP is explicitly bypassed or disabled in admin."""
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    return bool(bypass_warp or not _get_dynamic_warp_enabled())

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
    return _matches_excluded_host(url, WARP_EXCLUDE_DOMAINS)

def _matches_excluded_host(url: str, domains: list) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower().rstrip(".")
    for domain in domains:
        domain = domain.lower().strip().lstrip("*.").rstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False

def _get_dynamic_proxy_exclude_domains() -> list:
    return _cfg_get("proxy_exclude_domains", [])

def _is_proxy_excluded(url: str) -> bool:
    return _matches_excluded_host(url, PROXY_EXCLUDE_DOMAINS)

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
    transport_routes: list | None = None,
    global_proxies: list | None = None,
    bypass_tor: bool | None = None,
) -> list[str]:
    """Build the single routing chain used by every extractor/service.

    Priority is matching transport route, extractor proxy file,
    selected/fallback proxies, global proxies, then WARP. WARP is deliberately
    deferred even when it appears in a supplied fallback list.

    Tor (the configured Tor SOCKS) is a regular chain member, plus one extra
    rule: with warp AND proxies both bypassed, this URL egresses via Tor
    instead of falling back to direct — no transport route needed, the Tor
    address comes from tor_bind. tor=off restores the old behaviour (direct).
    Explicit direct pins are still honoured.
    """
    if bypass_proxies is None:
        bypass_proxies = BYPASS_PROXIES_CONTEXT.get() or _is_proxy_excluded(url or "")

    _ENABLE_WARP = _get_dynamic_warp_enabled()
    _WARP_PROXY_URL = WARP_PROXY_URL
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    if bypass_tor is None:
        bypass_tor = BYPASS_TOR_CONTEXT.get()

    _GLOBAL_PROXIES = _get_dynamic_global_proxies() if global_proxies is None else global_proxies
    _TRANSPORT_ROUTES = _get_dynamic_transport_routes() if transport_routes is None else transport_routes

    if bypass_proxies:
        ordered = []
        is_excluded = _is_warp_excluded(url or "")
        if _ENABLE_WARP and not bypass_warp and not is_excluded:
            ordered.append(_WARP_PROXY_URL)
        # Tor egress survives proxy=off (unlike every other proxy list).
        # No transport route needed: the address comes from tor_bind.
        # tor=off restores the old behaviour (direct).
        if bypass_warp and not bypass_tor:
            kind, tor_proxy = _find_tor_transport_proxy(url, _TRANSPORT_ROUTES)
            if kind == "pin":
                tor_proxy = None
            elif not tor_proxy:
                tor_proxy = _tor_default_proxy()
            if tor_proxy and tor_proxy not in ordered:
                ordered.append(tor_proxy)
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
            if bypass_tor and is_tor_proxy_url(proxy):
                return
            ordered.append(proxy)

    selected_proxy = SELECTED_PROXY_CONTEXT.get()
    selected_proxy_is_strict = STRICT_PROXY_CONTEXT.get()
    if (
        selected_proxy
        and selected_proxy_is_strict
        and not (
            is_warp_proxy_url(selected_proxy)
            and (bypass_warp or not _ENABLE_WARP)
        )
    ):
        return build([selected_proxy], strict=True)

    if url and _TRANSPORT_ROUTES:
        normalized_url = url.lower()
        for route in _TRANSPORT_ROUTES:
            url_pattern = route["url"].lower()
            if url_pattern in normalized_url:
                route_proxy = route.get("proxy")
                if bypass_tor and is_tor_proxy_url(route_proxy):
                    continue  # tor=off: skip Tor route, keep scanning
                if not (
                    is_warp_proxy_url(route_proxy)
                    and (bypass_warp or not _ENABLE_WARP)
                ):
                    add(route_proxy)
                break

    extractor_proxies = get_extractor_proxies(extractor_name or "")
    for proxy in extractor_proxies:
        if not is_warp_proxy_url(proxy):
            add(proxy)

    if selected_proxy and not is_warp_proxy_url(selected_proxy):
        add(selected_proxy)

    for proxy in fallback_proxies or []:
        if not is_warp_proxy_url(proxy):
            add(proxy)

    for proxy in _GLOBAL_PROXIES:
        if not is_warp_proxy_url(proxy):
            add(proxy)

    is_excluded = _is_warp_excluded(url or "")
    if (
        _ENABLE_WARP
        and not bypass_warp
        and not is_excluded
        and not any(is_warp_proxy_url(proxy) for proxy in ordered)
    ):
        add(_WARP_PROXY_URL)

    return ProxyList(ordered, strict=False)


def should_allow_direct_fallback(
    proxies: list | None,
    bypass_warp: bool | None = None,
) -> bool:
    """Allow direct when WARP is bypassed/disabled and no proxy exists."""
    if getattr(proxies, "strict", False):
        return False
    active = [proxy for proxy in proxies or [] if proxy]
    if active:
        return False
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    # Direct is allowed when WARP is explicitly bypassed or disabled in admin.
    return is_direct_connection_allowed(bypass_warp)


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
    if getattr(ordered, "strict", False):
        # An explicit proxy is a caller override, not a candidate for silent
        # WARP/direct substitution.
        return ordered[0]
    effective_bypass_warp = BYPASS_WARP_CONTEXT.get() if bypass_warp is None else bypass_warp
    if _get_dynamic_warp_enabled() and not effective_bypass_warp and not _is_warp_excluded(url or ""):
        # Fail through WARP connector; never silently fall back to direct.
        return WARP_PROXY_URL
    return None


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
    if getattr(ordered, "strict", False):
        return ordered[0]
    effective_bypass_warp = BYPASS_WARP_CONTEXT.get() if bypass_warp is None else bypass_warp
    if _get_dynamic_warp_enabled() and not effective_bypass_warp and not _is_warp_excluded(url or ""):
        # Fail through WARP connector; never silently fall back to direct.
        return WARP_PROXY_URL
    return None


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

    try:
        alive = _socket_check(proxy_url, timeout=5)
    except (socket.timeout, ConnectionRefusedError, OSError):
        alive = False
    if not alive:
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
        alive = await loop.run_in_executor(_SOCKET_CHECK_EXECUTOR, _socket_check, proxy_url, 5)
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
    if _WARP_PROXY_URL and is_warp_proxy_url(proxy_url):
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
    extractor_name: str = "",
) -> str:
    """Return the first alive route from the common priority chain.

    If every candidate is down, return a configured candidate so the caller
    fails through that route; returning ``None`` would silently enable direct.
    """
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    if bypass_proxies is None:
        bypass_proxies = BYPASS_PROXIES_CONTEXT.get() or _is_proxy_excluded(url or "")

    ordered = get_ordered_proxies_for_url(
        url,
        extractor_name=extractor_name,
        bypass_warp=bypass_warp,
        bypass_proxies=bypass_proxies,
        transport_routes=transport_routes,
        global_proxies=global_proxies,
    )
    if not ordered:
        return None

    PROXY_SOURCE_LIST.set(ordered)
    for proxy in ordered:
        if is_proxy_alive(proxy):
            SELECTED_PROXY_CONTEXT.set(proxy)
            STRICT_PROXY_CONTEXT.set(getattr(ordered, "strict", False))
            return proxy

    # Keep the route non-direct even when health probing says every candidate
    # is down. The actual request then returns the expected connection error.
    fallback = ordered[0]
    logger.warning("All proxy routes failed health check for %s; keeping %s", url, fallback)
    SELECTED_PROXY_CONTEXT.set(fallback)
    STRICT_PROXY_CONTEXT.set(getattr(ordered, "strict", False))
    return fallback


def get_connector_for_proxy(proxy_url: str, **kwargs):
    """Crea un ProxyConnector (aiohttp-socks) gestendo socks5h e socks4a."""
    from aiohttp_socks import ProxyConnector

    if not proxy_url:
        return None

    force_ipv4 = bool(kwargs.pop("force_ipv4", False))
    health_check = bool(kwargs.pop("health_check", False))
    is_warp = is_warp_proxy_url(proxy_url)
    if is_warp and not health_check:
        global WARP_LAST_ACTIVITY
        WARP_LAST_ACTIVITY = time.monotonic()

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

    # Keep upstream connections reusable. Reopening a SOCKS+TLS connection
    # for every playlist/segment overloads userspace WireProxy and causes
    # avoidable timeouts/buffering. The caller still controls pool limits and
    # idle cleanup.
    if is_warp:
        # Keep the original hostname in the SOCKS request so TLS preserves
        # SNI/certificate validation. wireproxy itself is IPv4-only, so its
        # remote resolver selects the IPv4 path without replacing the host
        # with a bare address before TLS.
        force_ipv4 = False
        rdns = True
        kwargs.setdefault("keepalive_timeout", 15)
        kwargs.setdefault("force_close", False)

    connector_cls = ProxyConnector
    if force_ipv4:
        connector_cls = _IPv4ProxyConnector
        kwargs.setdefault("family", socket.AF_INET)

    return connector_cls.from_url(connector_url, rdns=rdns, **kwargs)


def get_curl_ipv4_options(proxy_url: str | None) -> dict:
    """Return curl_cffi options for IPv4-only WARP upstream requests."""
    if not proxy_url or not is_warp_proxy_url(proxy_url):
        return {}
    try:
        from curl_cffi import CurlOpt
    except ImportError:
        return {}
    return {"curl_options": {CurlOpt.IPRESOLVE: 1}}


class _IPv4ProxyConnector(ProxyConnector):
    """Proxy connector that resolves upstream hostnames only to IPv4."""

    async def _connect_via_proxy(self, host, port, ssl=None, timeout=None):
        try:
            target = ipaddress.ip_address(host)
            if target.version != 4:
                raise OSError(f"IPv4-only route cannot use IPv{target.version} target")
            ipv4_host = host
        except ValueError:
            infos = await self._loop.getaddrinfo(
                host,
                port,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
            if not infos:
                raise OSError(f"No IPv4 address found for {host}")
            ipv4_host = infos[0][4][0]

        return await super()._connect_via_proxy(ipv4_host, port, ssl, timeout)


def is_warp_proxy_url(proxy_url: str | None) -> bool:
    """Return True for the configured WARP SOCKS endpoint (socks5h/socks5)."""
    if not proxy_url or not WARP_PROXY_URL:
        return False

    def canonical(value: str) -> str:
        value = str(value).strip().rstrip("/")
        if value.startswith("socks5h://"):
            value = "socks5://" + value[len("socks5h://") :]
        return value

    return canonical(proxy_url) == canonical(WARP_PROXY_URL)


def _normalize_proxy_identity(proxy_url: str | None) -> tuple[str, str, str] | None:
    """Normalized (scheme, host, port) identity of a proxy URL for comparison."""
    if not proxy_url:
        return None
    try:
        text = str(proxy_url).strip().lower()
        if "://" not in text:
            text = "socks5://" + text
        parsed = urllib.parse.urlparse(text)
        scheme = parsed.scheme
        if scheme == "socks5h":
            scheme = "socks5"
        elif scheme == "socks4a":
            scheme = "socks4"
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        return (scheme, host, str(parsed.port or ""))
    except Exception:
        return None


def _tor_socks_identities() -> set[tuple[str, str, str]]:
    """Normalized identities of the configured Tor SOCKS (socks5 family)."""
    bind = str(_cfg_get("tor_bind", "127.0.0.1:9050") or "127.0.0.1:9050").strip()
    ident = _normalize_proxy_identity(f"socks5://{bind}")
    return {ident} if ident else set()


def is_tor_proxy_url(proxy_url: str | None) -> bool:
    """Return True if this proxy URL points at the configured Tor SOCKS."""
    ident = _normalize_proxy_identity(proxy_url)
    return bool(ident) and ident in _tor_socks_identities()


def _tor_default_proxy() -> str | None:
    """Tor SOCKS URL from tor_bind (no transport route needed)."""
    bind = str(_cfg_get("tor_bind", "127.0.0.1:9050") or "127.0.0.1:9050").strip()
    if not bind:
        return None
    return f"socks5h://{bind}"


def _find_tor_transport_proxy(
    url: str | None, transport_routes: list | None
) -> tuple[str, str | None]:
    """Tor routing hint for this URL: (kind, proxy).

    kind is "tor" (a Tor-marked route matched, use its proxy), "pin"
    (explicit null-proxy direct pin matched, never Tor) or "none".
    Non-Tor routes are skipped: under proxy bypass they are ignored anyway.
    """
    if not url or not transport_routes:
        return ("none", None)
    normalized_url = url.lower()
    for route in transport_routes:
        try:
            pattern = str((route or {}).get("url") or "").lower()
        except Exception:
            continue
        if not pattern or pattern not in normalized_url:
            continue
        route_proxy = (route or {}).get("proxy")
        if not route_proxy:
            return ("pin", None)
        if is_tor_proxy_url(route_proxy):
            return ("tor", route_proxy)
    return ("none", None)


def get_solver_proxy_url(proxy_url: str | None) -> str | None:
    """Return a browser-safe proxy while preserving the selected route."""
    if not proxy_url:
        return None

    if proxy_url.startswith("socks5h://"):
        return proxy_url.replace("socks5h://", "socks5://", 1)
    if proxy_url.startswith("socks4a://"):
        return proxy_url.replace("socks4a://", "socks4://", 1)

    return proxy_url


def build_proxy_with_auth(proxy_url: str | None) -> dict | None:
    """Converte un proxy URL in dict con username/password separati.

    Browser headless non supporta
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

    vavoo_domains = ("vavoo.to", "vavoo.tv", "vavoo", "lokke.app", "mediahubmx", "vixsrc.to", "vix-content.net", "/sunshine/", "unitv.mom", "d2b.you", "vidxgo")

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
    mod.DVR_ENABLED = _cfg_get("dvr_enabled", False)
    mod.RECORDINGS_DIR = _cfg_get("recordings_dir", DEFAULT_RECORDINGS_DIR)
    mod.MAX_RECORDING_DURATION = _cfg_get("max_recording_duration", 28800)
    mod.RECORDINGS_RETENTION_DAYS = _cfg_get("recordings_retention_days", 7)
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
        "DVR_ENABLED": lambda: _cfg_get("dvr_enabled", False),
        "RECORDINGS_DIR": lambda: _cfg_get("recordings_dir", DEFAULT_RECORDINGS_DIR),
        "MAX_RECORDING_DURATION": lambda: _cfg_get("max_recording_duration", 28800),
        "RECORDINGS_RETENTION_DAYS": lambda: _cfg_get("recordings_retention_days", 7),
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
    rec_dir = _cfg_get("recordings_dir", DEFAULT_RECORDINGS_DIR)
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

    # EasyProxy process RAM (including child processes)
    proxy_ram_used = ram_used
    proxy_ram_total = ram_total
    proxy_ram_percent = ram_percent
    process_tree = []
    main_process_rss = 0
    children_rss = 0
    ffmpeg_rss = 0
    wireproxy_rss = 0  # Wireproxy child-process RSS
    alighieri_rss = 0  # legacy API field; Alighieri is no longer shipped
    wgx_rss = 0
    warp_rss = 0
    other_children_rss = 0

    def _process_role(name: str) -> str:
        value = (name or "").lower()
        if "ffmpeg" in value or "ffprobe" in value:
            return "ffmpeg"
        if "wireproxy" in value:
            return "wireproxy"
        if "alighieri" in value:
            return "alighieri"
        if "wgx" in value:
            return "wgx"
        if "warp" in value:
            return "warp"
        return "other"

    def _process_snapshot(process, role: str, memory_info) -> dict:
        try:
            name = process.name()
        except Exception:
            name = "unknown"
        try:
            status = process.status()
        except Exception:
            status = None
        try:
            threads = process.num_threads()
        except Exception:
            threads = None
        rss = int(getattr(memory_info, "rss", 0) or 0)
        return {
            "pid": process.pid,
            "name": name,
            "role": role,
            "status": status,
            "rss": rss,
            "rss_mb": round(rss / (1024 * 1024), 2),
            "vms": int(getattr(memory_info, "vms", 0) or 0),
            "threads": threads,
        }

    def _tracked_processes(proc):
        """Return EasyProxy plus descendants and known proxy siblings."""
        tracked = {proc.pid: proc}
        try:
            for child in proc.children(recursive=True):
                tracked[child.pid] = child
        except Exception:
            pass
        # entrypoint.sh starts wireproxy before Python, so wireproxy can be our sibling.
        try:
            parent = proc.parent()
            for sibling in parent.children() if parent else []:
                if sibling.pid == proc.pid:
                    continue
                if _process_role(sibling.name()) != "other":
                    tracked[sibling.pid] = sibling
        except Exception:
            pass
        return list(tracked.values())

    try:
        proc = psutil.Process(os.getpid())
        main_info = proc.memory_info()
        main_process_rss = int(main_info.rss)
        proxy_ram_used = main_process_rss
        process_tree.append(_process_snapshot(proc, "easyproxy", main_info))

        tracked_processes = _tracked_processes(proc)
        get_system_stats._tracked_processes = tracked_processes
        for child in tracked_processes:
            if child.pid == proc.pid:
                continue
            try:
                child_info = child.memory_info()
                child_snapshot = _process_snapshot(child, _process_role(child.name()), child_info)
                process_tree.append(child_snapshot)
                child_rss = child_snapshot["rss"]
                children_rss += child_rss
                proxy_ram_used += child_rss
                if child_snapshot["role"] == "ffmpeg":
                    ffmpeg_rss += child_rss
                elif child_snapshot["role"] == "wireproxy":
                    wireproxy_rss += child_rss
                elif child_snapshot["role"] == "alighieri":
                    alighieri_rss += child_rss
                elif child_snapshot["role"] == "wgx":
                    wgx_rss += child_rss
                elif child_snapshot["role"] == "warp":
                    warp_rss += child_rss
                else:
                    other_children_rss += child_rss
            except Exception:
                pass
        proxy_ram_total = ram_total
        proxy_ram_percent = (proxy_ram_used / proxy_ram_total) * 100 if proxy_ram_total > 0 else 0
    except Exception:
        pass

    process_tree.sort(key=lambda item: item.get("rss", 0), reverse=True)

    asyncio_tasks = {"total": None, "by_coro": {}}
    try:
        task_counts = {}
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            coro_name = getattr(coro, "__qualname__", None) or type(coro).__name__
            task_counts[coro_name] = task_counts.get(coro_name, 0) + 1
        asyncio_tasks = {
            "total": sum(task_counts.values()),
            "by_coro": dict(sorted(task_counts.items(), key=lambda item: (-item[1], item[0]))),
        }
    except (RuntimeError, AttributeError):
        pass

    tracemalloc_stats = {"enabled": False, "current": 0, "peak": 0}
    if tracemalloc.is_tracing():
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc_stats = {
            "enabled": True,
            "current": traced_current,
            "current_mb": round(traced_current / (1024 * 1024), 3),
            "peak": traced_peak,
            "peak_mb": round(traced_peak / (1024 * 1024), 3),
        }

    # EasyProxy process CPU (including child processes). Keep per-process
    # readings so /api/info can identify whether Python or wireproxy is hot.
    proxy_cpu_percent = cpu_percent
    process_cpu = {}
    p_cpu = 0.0
    cores = os.cpu_count() or 1
    try:
        # Use persistent Process objects: psutil.cpu_percent() needs a previous
        # baseline reading, otherwise it always returns 0.0.
        _cpu_proc = getattr(get_system_stats, "_cpu_proc", None)
        if _cpu_proc is None:
            _cpu_proc = psutil.Process(os.getpid())
            get_system_stats._cpu_proc = _cpu_proc
            _cpu_proc.cpu_percent(interval=None)  # establish baseline

        _cpu_children = getattr(get_system_stats, "_cpu_children", {})
        current_children = {
            c.pid: c for c in getattr(get_system_stats, "_tracked_processes", [])
            if c.pid != _cpu_proc.pid
        }
        # Drop dead children and baseline new ones
        for pid in list(_cpu_children.keys()):
            if pid not in current_children:
                del _cpu_children[pid]
        for pid, child in current_children.items():
            if pid not in _cpu_children:
                try:
                    child.cpu_percent(interval=None)  # establish baseline
                except Exception:
                    continue
                _cpu_children[pid] = child

        p_cpu = _cpu_proc.cpu_percent(interval=None)
        process_cpu[_cpu_proc.pid] = p_cpu
        for child in _cpu_children.values():
            try:
                child_cpu = child.cpu_percent(interval=None)
                process_cpu[child.pid] = child_cpu
                p_cpu += child_cpu
            except Exception:
                pass
        get_system_stats._cpu_children = _cpu_children

        proxy_cpu_percent = min(100.0, p_cpu / cores)
        for snapshot in process_tree:
            raw = process_cpu.get(snapshot.get("pid"), 0.0)
            snapshot["cpu_percent_raw"] = round(raw, 1)
            snapshot["cpu_percent"] = round(min(100.0, raw / cores), 1)
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
            "percent": round(proxy_cpu_percent, 1),
            "percent_raw": round(p_cpu, 1),
            "cores": cores,
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
        "processes": {
            "count": len(process_tree),
            "main_rss": main_process_rss,
            "main_rss_mb": round(main_process_rss / (1024 * 1024), 2),
            "children_rss": children_rss,
            "children_rss_mb": round(children_rss / (1024 * 1024), 2),
            "ffmpeg_rss": ffmpeg_rss,
            "ffmpeg_rss_mb": round(ffmpeg_rss / (1024 * 1024), 2),
            "wireproxy_rss": wireproxy_rss,
            "wireproxy_rss_mb": round(wireproxy_rss / (1024 * 1024), 2),
            "alighieri_rss": alighieri_rss,
            "alighieri_rss_mb": round(alighieri_rss / (1024 * 1024), 2),
            "wgx_rss": wgx_rss,
            "wgx_rss_mb": round(wgx_rss / (1024 * 1024), 2),
            "warp_rss": warp_rss,
            "warp_rss_mb": round(warp_rss / (1024 * 1024), 2),
            "other_children_rss": other_children_rss,
            "other_children_rss_mb": round(other_children_rss / (1024 * 1024), 2),
            "tree": process_tree,
        },
        "asyncio_tasks": asyncio_tasks,
        "tracemalloc": tracemalloc_stats,
        "net": {
            "sent": round(net_sent, 1),
            "recv": round(net_recv, 1)
        }
    }
