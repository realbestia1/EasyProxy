import logging
import asyncio
import socket
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector, ClientConnectionError
from config import (
    get_connector_for_proxy,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    BYPASS_WARP_CONTEXT,
    mark_proxy_dead,
    get_preferred_proxy_for_url,
    ALL_PROXY_ERRORS,
)
import config as _cfg

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass


def normalize_socks_proxy_url(proxy_url: str) -> str:
    """Resolve a proxy alias, keeping the scheme the routing policy selected.

    socks5:// stays local-DNS (remote DNS through wireproxy can stall); a bare
    host:port becomes socks5h:// so the SOCKS endpoint resolves the hostname.
    """
    proxy_url = _cfg.resolve_proxy_alias(str(proxy_url).strip()) or str(proxy_url).strip()
    if proxy_url.startswith("socks5://"):
        return proxy_url
    if "://" not in proxy_url:
        return f"socks5h://{proxy_url}"
    return proxy_url


class MockResponse:
    """Small response adapter shared by text and binary extractor results."""

    def __init__(self, text, status, headers, url, cookies):
        self.text = text
        self.status = status
        self.status_code = status
        self.headers = headers
        self.url = url
        self.cookies = cookies

    @property
    def json(self):
        import json

        try:
            return json.loads(self.text)
        except Exception:
            return {}


class BaseExtractor:
    """Base class for extractors with robust networking and proxy fallback."""

    # Pin direct connections to IPv4 for sites whose session tokens are bound
    # to the solver's (IPv4) address.
    force_ipv4 = False

    def __init__(self, request_headers: dict, proxies: list = None, extractor_name: str = "generic", bypass_warp: bool = False):
        self.request_headers = request_headers
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        self.session = None
        self._session_lock = asyncio.Lock()
        self.mediaflow_endpoint = "hls_proxy"
        self.proxies = proxies or []
        self.extractor_name = extractor_name
        self._session_proxy = None
        self._route_sessions = {}
        # New-contract routing state (None = defer to contexts). Set by
        # _apply_routing_kwargs at the top of extract(); see AGENTS.md.
        self._init_bypass_warp = bool(bypass_warp)
        self.bypass_warp_active = bool(bypass_warp) if bypass_warp else None
        self._last_computed_bypass_warp = self.bypass_warp_active
        self._forced_proxy = None
        self._force_direct = False
        self.last_used_proxy = None

    def _apply_routing_kwargs(self, url: str, kwargs: dict | None = None) -> None:
        """Canonical per-request routing merge (proxy_streaming passes proxy/
        warp/direct as kwargs, not via context). proxy_exclude_domains drops
        even an explicit ?proxy= (WARP exempt); see config.effective_forced_proxy."""
        kwargs = kwargs or {}
        if self.bypass_warp_active != getattr(self, "_last_computed_bypass_warp", None):
            self._init_bypass_warp = bool(self.bypass_warp_active)
        raw_proxy = kwargs.get("proxy")
        raw_proxy_norm = str(raw_proxy or "").strip().lower()
        bypass_proxies = raw_proxy_norm in {"off", "none", "no", "false", "0", "direct"} or BYPASS_PROXIES_CONTEXT.get()
        warp_off = bool(kwargs.get("bypass_warp") or str(kwargs.get("warp", "")).strip().lower() in {"off", "false", "0"})
        direct_flag = str(kwargs.get("direct", "")).strip().lower() in {"1", "true", "yes", "on"} or raw_proxy_norm == "direct"
        bypass_warp = bool(warp_off or direct_flag or BYPASS_WARP_CONTEXT.get() or self._init_bypass_warp)
        self.bypass_warp_active = bypass_warp
        self._last_computed_bypass_warp = bypass_warp
        direct_requested = direct_flag or (bypass_proxies and bypass_warp)
        if direct_requested or (bypass_proxies and bypass_warp):
            self._forced_proxy, self._force_direct = None, True
        elif bypass_proxies:
            self._forced_proxy, self._force_direct = (_cfg.WARP_PROXY_URL if _cfg._get_dynamic_warp_enabled() else None), False
            if not self._forced_proxy:
                self._force_direct = True
        elif raw_proxy and raw_proxy_norm not in {"on", "auto", "true", ""}:
            resolved = _cfg.resolve_proxy_alias(str(raw_proxy).strip())
            if resolved and "://" not in resolved:
                resolved = f"socks5h://{resolved}"
            self._forced_proxy, self._force_direct = str(resolved), False
        else:
            self._forced_proxy, self._force_direct = None, False
        self._forced_proxy = _cfg.effective_forced_proxy(url, self._forced_proxy)

    async def _resolve_proxy(self, url: str | None = None) -> str | None:
        """Resolve the outbound proxy honoring _apply_routing_kwargs state."""
        if self._force_direct:
            proxy = None
        elif self._forced_proxy:
            proxy = str(self._forced_proxy)
        else:
            proxy = await get_preferred_proxy_for_url(url, self.extractor_name, self.proxies or _cfg.GLOBAL_PROXIES, self.bypass_warp_active)
        if proxy and self.bypass_warp_active and _cfg.is_warp_proxy_url(proxy):
            proxy = None
        if proxy is None and not self._force_direct and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ClientConnectionError(
                "No proxy route available; direct fallback disabled"
            )
        self.last_used_proxy = proxy
        return proxy

    def _result(self, destination_url: str, request_headers: dict,
                mediaflow_endpoint: str | None = None) -> dict:
        """Standard extractor result. The routing fields are mandatory: the
        segment/playlist paths reuse them to keep the same route."""
        return {
            "destination_url": destination_url,
            "request_headers": request_headers,
            "mediaflow_endpoint": mediaflow_endpoint or self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }

    async def _get_session(self, url: str = None):
        proxy = await self._resolve_proxy(url)

        async with self._session_lock:
            self.session = self._route_sessions.get(proxy)
            self._session_proxy = proxy
            if (
                self.session is None
                or self.session.closed
                or self._session_proxy != proxy
            ):
                timeout = ClientTimeout(total=60, connect=30, sock_read=30)

                if proxy:
                    connector = get_connector_for_proxy(proxy)
                else:
                    connector = TCPConnector(
                        limit=0, 
                        limit_per_host=0, 
                        keepalive_timeout=15, 
                        enable_cleanup_closed=True, 
                        use_dns_cache=True,
                        family=socket.AF_INET if self.force_ipv4 else socket.AF_UNSPEC,
                    )
                
                self.session = ClientSession(
                    timeout=timeout, 
                    connector=connector, 
                    headers={'User-Agent': self.base_headers["User-Agent"]}
                )
                self._session_proxy = proxy
                self._route_sessions[proxy] = self.session
        return self.session

    async def _make_request(self, url: str, method: str = "GET", headers: dict = None, retries: int = 2, **kwargs):
        """Perform a robust request with proxy fallback."""
        final_headers = headers or {}
        if "User-Agent" not in final_headers:
            final_headers["User-Agent"] = self.base_headers["User-Agent"]

        session = None
        for attempt in range(retries):
            try:
                session = await self._get_session(url)
                async with session.request(method, url, headers=final_headers, allow_redirects=True, **kwargs) as response:
                    response.raise_for_status()
                    
                    content_type = response.headers.get("Content-Type", "").lower()
                    content_length_str = response.headers.get("Content-Length", "0")
                    content_length = int(content_length_str) if content_length_str.isdigit() else 0
                    
                    if "video/" in content_type or "audio/" in content_type or content_length > 2 * 1024 * 1024:
                        logger.warning(f"[{self.extractor_name}] Skipping text read for binary/large content: {content_type} ({content_length} bytes)")
                        # Restituisci un MockResponse "vuoto" o che indica il bypass
                        return MockResponse("", response.status, response.headers, str(response.url), response.cookies)

                    content = await response.text()

                    return MockResponse(content, response.status, response.headers, str(response.url), response.cookies)
            except ALL_PROXY_ERRORS + (asyncio.TimeoutError, ClientConnectionError, aiohttp.ClientResponseError) as e:
                is_proxy_err = isinstance(e, ALL_PROXY_ERRORS)
                
                # Check for 403 or network errors to trigger fallback. Keep
                # intermediate failures quiet: a later retry may succeed.
                if attempt >= retries - 1:
                    logger.error(
                        "[%s] Request failed after %s attempts for %s: %s",
                        self.extractor_name,
                        retries,
                        url,
                        e,
                    )
                
                # aiohttp discards failed connections itself. Closing the shared
                # session here would abort unrelated concurrent requests.
                
                if is_proxy_err and SELECTED_PROXY_CONTEXT.get() and not STRICT_PROXY_CONTEXT.get():
                    proxy_to_mark = SELECTED_PROXY_CONTEXT.get()
                    if proxy_to_mark:
                        mark_proxy_dead(proxy_to_mark)
                    SELECTED_PROXY_CONTEXT.set(None)
                
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise ExtractorError(f"Request failed after {retries} attempts: {e}")
        
        raise ExtractorError(f"Request failed for {url}")

    async def close(self):
        sessions = set(self._route_sessions.values())
        if self.session is not None:
            sessions.add(self.session)
        for session in sessions:
            if not session.closed:
                await session.close()
        self._route_sessions.clear()
        self.session = None
