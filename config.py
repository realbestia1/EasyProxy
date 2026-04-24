import os
import logging
import random
import socket
import time
import contextvars
from dotenv import load_dotenv

# ContextVar for thread-safe/async-safe warp bypass state
BYPASS_WARP_CONTEXT = contextvars.ContextVar("bypass_warp", default=False)
SELECTED_PROXY_CONTEXT = contextvars.ContextVar("selected_proxy", default=None)

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

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


class AsyncioWarningFilter(logging.Filter):
    def filter(self, record):
        return "Unknown child process pid" not in record.getMessage()


logging.getLogger("asyncio").addFilter(AsyncioWarningFilter())

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def parse_proxies(proxy_env_var: str) -> list:
    """Analizza una stringa di proxy separati da virgola da una variabile d'ambiente."""
    proxies_str = os.environ.get(proxy_env_var, "").strip()
    if proxies_str:
        return [p.strip() for p in proxies_str.split(",") if p.strip()]
    return []


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


def is_proxy_alive(proxy_url: str) -> bool:
    """Checks if a local proxy is reachable to avoid 'Connection Refused' errors."""
    if not proxy_url or "127.0.0.1" not in proxy_url:
        return True

    now = time.time()
    if now - _PROXY_STATUS_CACHE["last_check"] < 10:
        return _PROXY_STATUS_CACHE["alive"]

    _PROXY_STATUS_CACHE["last_check"] = now
    try:
        host = "127.0.0.1"
        port = 1080
        if ":" in proxy_url:
            port_part = proxy_url.split(":")[-1].split("/")[0]
            if port_part.isdigit():
                port = int(port_part)

        with socket.create_connection((host, port), timeout=0.5):
            _PROXY_STATUS_CACHE["alive"] = True
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        _PROXY_STATUS_CACHE["alive"] = False
        logging.warning(f"Local proxy {proxy_url} is NOT reachable. Falling back to direct connection.")
        return False


def get_proxy_for_url(url: str, transport_routes: list, global_proxies: list, bypass_warp: bool = None) -> str:
    """Trova il proxy appropriato per un URL basato su TRANSPORT_ROUTES e impostazioni WARP."""
    if bypass_warp is None:
        bypass_warp = BYPASS_WARP_CONTEXT.get()
    if not url:
        if bypass_warp:
            return None
        proxy = random.choice(global_proxies) if global_proxies else None
        return proxy if is_proxy_alive(proxy) else None

    # `bypass_warp` means "force real IP / direct connection" for the whole flow.
    # Do this before TRANSPORT_ROUTES so host-specific routes cannot silently
    # reintroduce WARP or another proxy when the caller explicitly asked to bypass.
    if bypass_warp:
        return None

    normalized_url = url.lower()

    if transport_routes:
        for route in transport_routes:
            url_pattern = route["url"]
            if url_pattern in url:
                proxy_value = route.get("proxy")
                if not proxy_value:
                    return None
                return proxy_value if is_proxy_alive(proxy_value) else None

    # Check if WARP should be used
    is_excluded = any(domain in normalized_url for domain in WARP_EXCLUDE_DOMAINS)
    
    if ENABLE_WARP and not bypass_warp and not is_excluded:
        return WARP_PROXY_URL if is_proxy_alive(WARP_PROXY_URL) else None

    # Fallback to Global Proxies
    # Se bypass_warp è True, preferiamo la connessione DIRETTA (Real IP) per coerenza
    # invece di pescare un proxy a caso dalla lista globale, che causerebbe rotazione IP.
    if bypass_warp:
        return None

    # Use sticky proxy if already selected for this request context
    proxy = SELECTED_PROXY_CONTEXT.get()
    if proxy:
        return proxy if is_proxy_alive(proxy) else None

    proxy = random.choice(global_proxies) if global_proxies else None
    if proxy:
        SELECTED_PROXY_CONTEXT.set(proxy)
        
    return proxy if is_proxy_alive(proxy) else None


def get_connector_for_proxy(proxy_url: str, **kwargs):
    """Crea un ProxyConnector (aiohttp-socks) gestendo correttamente socks5h."""
    from aiohttp_socks import ProxyConnector

    if not proxy_url:
        return None

    connector_url = proxy_url
    rdns = kwargs.pop("rdns", False)

    if connector_url.startswith("socks5h://"):
        connector_url = connector_url.replace("socks5h://", "socks5://")
        rdns = True

    return ProxyConnector.from_url(connector_url, rdns=rdns, **kwargs)


def get_solver_proxy_url(proxy_url: str | None) -> str | None:
    """Normalizza il proxy per solver/browser che non supportano socks5h."""
    if not proxy_url:
        return None

    if proxy_url.startswith("socks5h://"):
        return proxy_url.replace("socks5h://", "socks5://", 1)

    return proxy_url


def get_ssl_setting_for_url(url: str, transport_routes: list) -> bool:
    """Determina se SSL deve essere disabilitato per un URL basato su TRANSPORT_ROUTES."""
    normalized_url = (url or "").lower()

    if "disable_ssl=1" in normalized_url:
        return True

    if not url or not transport_routes:
        return any(
            domain in normalized_url
            for domain in ("vavoo.to", "vavoo.tv", "lokke.app", "mediahubmx")
        )

    if any(
        domain in normalized_url
        for domain in ("vavoo.to", "vavoo.tv", "lokke.app", "mediahubmx")
    ):
        return True

    for route in transport_routes:
        url_pattern = route["url"]
        if url_pattern in url:
            return route.get("disable_ssl", False)

    return False


ENABLE_WARP = os.environ.get("ENABLE_WARP", "false").lower() == "true"
WARP_PROXY_URL = os.environ.get("WARP_PROXY_URL", "").strip() or "socks5h://127.0.0.1:1080"

_default_warp_exclude_domains = [
    "cinemacity.cc",
    "*.cinemacity.cc",
    "cccdn.net",
    "*.cccdn.net",
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
    "dlstreams.com",
    "*.dlstreams.com",
    "dlhd.dad",
    "*.dlhd.dad",
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
APP_VERSION = "2.5.85"

_has_solvers = os.path.exists("flaresolverr") and (
    os.path.exists("byparr") or os.path.exists("byparr_src")
)
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

# --- FlareSolverr / Byparr Configuration ---
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191").rstrip("/")
FLARESOLVERR_TIMEOUT = int(os.environ.get("FLARESOLVERR_TIMEOUT", 30))
BYPARR_URL = os.environ.get("BYPARR_URL", "http://localhost:8080").rstrip("/")


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
