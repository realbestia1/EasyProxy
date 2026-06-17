import asyncio
import hmac
import logging
import os
import re
import time
import urllib.parse
import aiohttp
import base64
import hashlib
import socket
import config_store
from utils.solver_manager import try_shutdown_idle_flaresolverr
import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    get_proxy_for_url,
    get_connector_for_proxy,
    get_extractor_proxies,
    mark_proxy_dead,
    BYPASSED_WARP_DOMAINS,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    is_dynamic_warp_bypass_candidate,
    prefer_default_family_for_url,
    resolve_extractor,
)

class HLSProxyCoreMixin:

    @staticmethod
    def _pow_search(hmac_hash: str, resource: str, number: str, ts: int, max_iter: int) -> int:
        """CPU-bound PoW search, intended for run_in_executor."""
        import hashlib as _hl
        for i in range(max_iter):
            combined = f"{hmac_hash}{resource}{number}{ts}{i}"
            md5_hash = _hl.md5(combined.encode("utf-8")).hexdigest()
            prefix_value = int(md5_hash[:4], 16)
            if prefix_value < 0x1000:
                return i
        return 0

    async def shorten_hls_url(self, url: str) -> str:
        """Codifica l'URL direttamente in base64 (nessuna memoria usata per mappe)."""
        if not url:
            return ""
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"u_{encoded}"

    async def start_tasks(self):
        """Starts background tasks for the proxy."""
        asyncio.create_task(self._update_latest_version())
        asyncio.create_task(self._cleanup_stale_sessions())

    async def _cleanup_stale_sessions(self):
        """Periodically close stale extractors unused for >5m."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale_streams = [
                stream_ref for stream_ref, t in self._extractor_stream_atimes.items()
                if now - t > 300
            ]
            for stream_ref in stale_streams:
                self._extractor_stream_atimes.pop(stream_ref, None)
            stale_ext = [
                k for k, t in self._extractor_atimes.items()
                if (
                    now - t > 300
                    and k in self.extractors
                    and not any(ref[0] == k for ref in self._extractor_stream_atimes)
                )
            ]
            for key in stale_ext:
                ext = self.extractors.pop(key, None)
                self._extractor_atimes.pop(key, None)
                for stream_ref in list(self._extractor_stream_atimes):
                    if stream_ref[0] == key:
                        self._extractor_stream_atimes.pop(stream_ref, None)
                if ext and hasattr(ext, 'close'):
                    try:
                        await ext.close()
                    except Exception:
                        pass
                logger.info("🧹 Cleaned stale extractor: %s", key)

            # Cleanup stale CDN tokens (>5 min since last use)
            stale_tokens = [
                k for k, t in getattr(self, '_renewed_cdn_token_atimes', {}).items()
                if now - t > 300
            ]
            for k in stale_tokens:
                self._renewed_cdn_tokens.pop(k, None)
                self._renewed_cdn_token_atimes.pop(k, None)
                logger.debug("🧹 Cleaned stale CDN token: %s", k[:8])

            await try_shutdown_idle_flaresolverr()

    async def get_warp_status(self) -> str:
        """Returns WARP status and fetches real external IP through WARP proxy."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        _WARP_PROXY_URL = _shared.WARP_PROXY_URL
        result = "Disconnected"
        if _ENABLE_WARP and _WARP_PROXY_URL:
            # Quick socket test to 127.0.0.1:1080 (no DNS, always fast)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 1080), timeout=3
                )
                writer.close()
                result = "Connected"
                # Try to fetch the WARP IP via the proxy
                try:
                    connector = get_connector_for_proxy(
                        _WARP_PROXY_URL, limit=0, family=socket.AF_INET
                    )
                    async with ClientSession(connector=connector, timeout=ClientTimeout(total=10)) as session:
                        async with session.get("https://api.ipify.org?format=json") as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                self._warp_ip = data.get("ip", "")
                except Exception:
                    pass
            except (OSError, asyncio.TimeoutError):
                pass
        return result

    async def reconnect_warp(self) -> dict:
        """Reconnect WARP to get a new IP. Tries warp-cli first, then wireproxy kill+restart."""
        result = {"status": "ok", "message": ""}

        if await _warp_cli_connect():
            result["message"] = "WARP reconnected via warp-cli"
            return result

        # Fallback: wireproxy mode — kill, re-register, restart
        warp_dir = os.environ.get("WARP_DIR", "/tmp/easyproxy-warp")
        _kill_wireproxy()
        await asyncio.sleep(1)

        try:
            # Remove old registration to force new IP
            acct_file = os.path.join(warp_dir, "wgcf-account.toml")
            if os.path.exists(acct_file):
                os.remove(acct_file)

            # Re-register and start wireproxy
            proc = await asyncio.create_subprocess_exec(
                "wgcf", "register", "--accept-tos",
                cwd=warp_dir,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)

            license_key = _shared.WARP_LICENSE_KEY or config_store.get("warp_license_key", "")
            if license_key:
                proc = await asyncio.create_subprocess_exec(
                    "wgcf", "update", "--license-key", license_key,
                    cwd=warp_dir,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=10)

            # Generate wireproxy config
            profile = os.path.join(warp_dir, "wgcf-profile.conf")
            if os.path.exists(profile):
                os.remove(profile)
            wp_conf = os.path.join(warp_dir, "wireproxy.conf")
            if os.path.exists(wp_conf):
                os.remove(wp_conf)

            proc = await asyncio.create_subprocess_exec(
                "wgcf", "generate",
                cwd=warp_dir,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)

            # Build wireproxy.conf with SOCKS5 section
            import shutil
            shutil.copy(profile, wp_conf)
            with open(wp_conf, "a") as f:
                f.write("\n[Socks5]\nBindAddress = 127.0.0.1:1080\n")

            # Start wireproxy
            proc = await asyncio.create_subprocess_exec(
                "wireproxy", "-c", wp_conf,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            # Verify SOCKS5 is listening
            import socket
            for _ in range(10):
                try:
                    s = socket.create_connection(("127.0.0.1", 1080), timeout=2)
                    s.close()
                    result["message"] = "WARP reconnected via wireproxy (new IP)"
                    return result
                except (OSError, ConnectionRefusedError):
                    await asyncio.sleep(1)
            result["status"] = "error"
            result["message"] = "wireproxy started but SOCKS5 not detected on 1080"
        except Exception as e:
            result["status"] = "error"
            result["message"] = f"WARP reconnect failed: {e}"

        return result

    async def _stop_warp_proxy(self):
        for cmd in [["warp-cli", "--accept-tos", "disconnect"]]:
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        _kill_wireproxy()

    async def _update_latest_version(self):
        """Periodically checks GitHub for the latest version in the background."""
        while True:
            await self._refresh_latest_version()
            # Check every hour in background
            await asyncio.sleep(3600)

    async def _refresh_latest_version(self):
        """Checks GitHub config.py for the latest version with cache busting.
        Can be called on-demand (e.g. on page refresh).
        """
        try:
            # Use a timestamp to bypass GitHub's cache
            cache_buster = int(time.time())
            url = f"https://raw.githubusercontent.com/realbestia1/EasyProxy/main/config.py?t={cache_buster}"

            # Use a direct session with a short timeout to not block UI too long
            session = await self._get_session()
            async with session.get(url, timeout=2) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # Use regex to find APP_VERSION = "..." or '...'
                    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
                    if match:
                        new_version = match.group(1)
                        if self.latest_version != new_version:
                            self.latest_version = new_version
                            logger.info(f"🆕 Latest version updated: {self.latest_version}")
                    else:
                        if self.latest_version == "Checking...":
                            self.latest_version = "Unknown"
                else:
                    if self.latest_version == "Checking...":
                        self.latest_version = "Error"
        except Exception as e:
            if self.latest_version == "Checking...":
                self.latest_version = "Unknown"
            logger.debug(f"Version check skipped or failed: {e}")

    @staticmethod
    def _strip_fake_png_header_from_ts(content: bytes) -> bytes:
        """
        Some providers prepend a fake 8-byte PNG signature to TS segments.
        Strip it only when bytes after the header still match TS sync markers.
        """
        png_sig = b"\x89PNG\r\n\x1a\n"
        if len(content) <= 8 or not content.startswith(png_sig):
            return content

        ts_payload = content[8:]
        # MPEG-TS sync byte is 0x47 at packet boundaries.
        if not ts_payload or ts_payload[0] != 0x47:
            return content
        if len(ts_payload) > 188 and ts_payload[188] != 0x47:
            return content

        logger.info(
            "Removed fake PNG header from TS segment (%d -> %d bytes)",
            len(content),
            len(ts_payload),
        )
        return ts_payload

    async def _compute_key_headers(
        self, key_url: str, secret_key: str, user_agent: str = None
    ) -> tuple[int, int, str, str] | None:
        """
        Compute X-Key-Timestamp, X-Key-Nonce, X-Fingerprint, and X-Key-Path for a /key/ URL.

        Algorithm:
        1. Extract resource and number from URL pattern /key/{resource}/{number}
        2. ts = Unix timestamp in seconds
        3. hmac_hash = HMAC-SHA256(resource, secret_key).hex()
        4. nonce = proof-of-work: find i where MD5(hmac+resource+number+ts+i)[:4] < 0x1000
        5. fingerprint = SHA256(useragent + screen_resolution + timezone + language).hex()[:16]
        6. key_path = HMAC-SHA256("resource|number|ts|fingerprint", secret_key).hex()[:16]

        Args:
            key_url: The key URL containing /key/{resource}/{number}
            secret_key: The HMAC secret key
            user_agent: The user agent string for fingerprint calculation

        Returns:
            Tuple of (timestamp, nonce, fingerprint, key_path) or None if URL doesn't match pattern
        """
        # Extract resource and number from URL
        pattern = r"/key/([^/]+)/(\d+)"
        match = re.search(pattern, key_url)

        if not match:
            return None

        resource = match.group(1)
        number = match.group(2)

        ts = int(time.time())

        # Compute HMAC-SHA256
        hmac_hash = hmac.new(
            secret_key.encode("utf-8"), resource.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Proof-of-work loop (CPU-bound, run in thread pool to not block event loop)
        loop = asyncio.get_event_loop()
        nonce = await loop.run_in_executor(None, HLSProxyCoreMixin._pow_search, hmac_hash, resource, number, ts, 50000)

        # Compute fingerprint
        fp_user_agent = (
            user_agent
            if user_agent
            else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
        fp_screen_res = "1920x1080"
        fp_timezone = "UTC"
        fp_language = "en"

        fp_string = f"{fp_user_agent}{fp_screen_res}{fp_timezone}{fp_language}"

        fingerprint = hashlib.sha256(fp_string.encode("utf-8")).hexdigest()[:16]

        # Compute key-path
        key_path_string = f"{resource}|{number}|{ts}|{fingerprint}"
        key_path = hmac.new(
            secret_key.encode("utf-8"), key_path_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

        return ts, nonce, fingerprint, key_path

    async def _get_session(self, prefer_default_family: bool = False, url: str = None):
        if url:
            await self._check_dynamic_warp_bypass(url)
        target_attr = "flex_session" if prefer_default_family else "session"
        session = getattr(self, target_attr)
        if session is None or session.closed:
            connector_kwargs = {
                "limit": 0,
                "limit_per_host": 0,
                "keepalive_timeout": 60,
                "enable_cleanup_closed": True,
                "use_dns_cache": True,
            }
            if not prefer_default_family:
                connector_kwargs["family"] = socket.AF_INET

            connector = TCPConnector(**connector_kwargs)
            session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30),
                connector=connector,
            )
            setattr(self, target_attr, session)
        return session

    async def _check_dynamic_warp_bypass(self, url: str):
        """Dynamically adds domain to WARP bypass if it matches known patterns."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        if not _ENABLE_WARP:
            return

        try:
            from urllib.parse import urlsplit
            domain = urlsplit(url).netloc
            if not domain: return

            # Sanitize domain: only allow valid hostname characters
            if not re.match(r'^[a-zA-Z0-9.\-*]+$', domain):
                return

            if is_dynamic_warp_bypass_candidate(domain):
                if domain not in BYPASSED_WARP_DOMAINS:
                    base_domain = ".".join(domain.split(".")[-2:])
                    logging.info(f"⚠️ [Dynamic Bypass] Adding {base_domain} (and {domain}) to WARP exclusion list...")

                    proc1 = await asyncio.create_subprocess_exec(
                        "warp-cli", "--accept-tos", "tunnel", "host", "add", base_domain,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc1.wait()
                    proc2 = await asyncio.create_subprocess_exec(
                        "warp-cli", "--accept-tos", "tunnel", "host", "add", domain,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc2.wait()

                    _WARP_EXCLUDE_DOMAINS = _shared.WARP_EXCLUDE_DOMAINS
                    if isinstance(_WARP_EXCLUDE_DOMAINS, list):
                        if base_domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(base_domain)
                        if domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(domain)

                    BYPASSED_WARP_DOMAINS.add(domain)
                    BYPASSED_WARP_DOMAINS.add(base_domain)
                    await asyncio.sleep(1.0)
        except Exception as e:
            logging.error(f"❌ Error in dynamic WARP bypass: {e}")

    async def _get_proxy_session(self, url: str, bypass_warp: bool = False, forced_proxy: str | None = None):
        """Get a session with proxy support for the given URL.

        Sessions are cached and reused for the same proxy to improve performance.
        Unused sessions older than 120s are closed and removed.

        Returns: (session, proxy_url) tuple
        - session: The aiohttp ClientSession to use
        - proxy_url: The proxy URL being used, or None for direct connection
        """
        await self._check_dynamic_warp_bypass(url)

        # ✅ FIX: Decodifica il proxy se è URL-encoded
        if forced_proxy:
            forced_proxy = urllib.parse.unquote(forced_proxy)
            if forced_proxy.lower() == "off":
                forced_proxy = None

        proxy = forced_proxy or get_proxy_for_url(url, bypass_warp=bypass_warp)

        prefer_default_family = prefer_default_family_for_url(url)

        if proxy:
            is_warp = "127.0.0.1:1080" in proxy
            if proxy in self.proxy_sessions:
                cached_session = self.proxy_sessions[proxy]
                if not cached_session.closed:
                    if is_warp:
                        return cached_session, proxy
                    atime = self._proxy_session_atimes.get(proxy, 0)
                    if time.time() - atime > 120:
                        logger.info(f"🧹 Closing idle proxy session: {proxy}")
                        del self.proxy_sessions[proxy]
                        await cached_session.close()
                    else:
                        self._proxy_session_atimes[proxy] = time.time()
                        return cached_session, proxy
                else:
                    del self.proxy_sessions[proxy]

            # Create new session and cache it
            logger.info(f"[NET] Creating proxy session: {proxy}")
            try:
                connector = get_connector_for_proxy(
                    proxy,
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=60,
                    family=socket.AF_INET,
                )
                timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30)
                session = ClientSession(timeout=timeout, connector=connector)
                self.proxy_sessions[proxy] = session
                self._proxy_session_atimes[proxy] = time.time()
                return session, proxy
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to create proxy connector: {e}"
                )
                raise

        # Fallback to shared non-proxy session
        session = await self._get_session(prefer_default_family=prefer_default_family)
        return session, None

    async def _retry_special_cdn_request(self, request_target, headers, disable_ssl: bool):
        """Retry a provider-protected CDN once via an alternate aiohttp route."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        _WARP_PROXY_URL = _shared.WARP_PROXY_URL
        _GLOBAL_PROXIES = _shared.GLOBAL_PROXIES
        retry_proxy = None
        if _ENABLE_WARP and _WARP_PROXY_URL and "127.0.0.1" not in _WARP_PROXY_URL:
            retry_proxy = _WARP_PROXY_URL
        elif _ENABLE_WARP and _WARP_PROXY_URL:
            from config import is_proxy_alive_async
            if await is_proxy_alive_async(_WARP_PROXY_URL):
                retry_proxy = _WARP_PROXY_URL
        elif _GLOBAL_PROXIES:
            retry_proxy = _GLOBAL_PROXIES[0]

        if not retry_proxy:
            return None

        try:
            connector = get_connector_for_proxy(
                retry_proxy,
                limit=0,
                limit_per_host=0,
                keepalive_timeout=60,
                family=socket.AF_INET,
                rdns=True,
            )
            timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
            async with ClientSession(timeout=timeout, connector=connector) as retry_session:
                async with retry_session.get(
                    request_target,
                    headers=headers,
                    ssl=not disable_ssl,
                ) as retry_resp:
                    if retry_resp.status not in [200, 206]:
                        return None
                    return {
                        "status": retry_resp.status,
                        "headers": dict(retry_resp.headers),
                        "body": await retry_resp.read(),
                        "proxy": retry_proxy,
                    }
        except Exception as e:
            logger.warning("Provider CDN retry via alternate route failed: %r", e)
            return None

    @staticmethod
    def _query_flag_is_true(value: str | None) -> bool:
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _should_force_direct_from_query(self, request) -> bool:
        direct_param = request.query.get("direct")
        if self._query_flag_is_true(direct_param):
            return True

        for param_name, param_value in request.query.items():
            if not param_name.startswith("h_"):
                continue
            header_name = param_name[2:].replace("_", "-").lower()
            if header_name in {"x-direct-connection", "x-force-direct"}:
                return self._query_flag_is_true(param_value)

        return False

    async def get_extractor(self, url: str, request_headers: dict, host: str = None, bypass_warp: bool = False):
        """Ottiene l'estrattore appropriato per l'URL."""
        result = await resolve_extractor(
            self,
            url,
            request_headers,
            host=host,
            bypass_warp=bypass_warp,
        )
        if result:
            key = getattr(result, '_cache_key', None) or id(result)
            for ek, ev in self.extractors.items():
                if ev is result:
                    self._extractor_atimes[ek] = time.time()
                    break
        return result

    def _extractor_key_for_instance(self, extractor) -> str | None:
        for key, cached_extractor in self.extractors.items():
            if cached_extractor is extractor:
                return key
        return None

    @staticmethod
    def _stream_key_for_url(url: str | None) -> str | None:
        if not url:
            return None
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _touch_extractor_activity(self, extractor_key: str | None = None, stream_key: str | None = None):
        now = time.time()
        if extractor_key and extractor_key in self.extractors:
            self._extractor_atimes[extractor_key] = now
            if stream_key:
                self._extractor_stream_atimes[(extractor_key, stream_key)] = now
            return
        for key in self.extractors:
            self._extractor_atimes[key] = now
            if stream_key:
                self._extractor_stream_atimes[(key, stream_key)] = now

    def _mark_proxy_dead_if_allowed(self, proxy_url: str | None, dead_duration: int = 300, extractor_key: str | None = None):
        if not proxy_url:
            return
        normalized_key = (extractor_key or "").replace("_direct", "")
        extractor_proxies = get_extractor_proxies(normalized_key)
        if len(extractor_proxies) == 1 and urllib.parse.unquote(proxy_url) == urllib.parse.unquote(extractor_proxies[0]):
            logger.info(
                "Proxy %s failed for extractor %s, but it is the only configured extractor proxy; keeping it alive.",
                proxy_url,
                normalized_key or extractor_key,
            )
            return
        mark_proxy_dead(proxy_url, dead_duration=dead_duration)

    async def _resolve_url_id(self, url_id: str) -> str | None:
        """Risolve un url_id nell'URL originale (solo U_ base64 short URLs)."""
        if not url_id:
            return None
        # U_ IDs are base64-encoded URLs
        if url_id.startswith("u_"):
            try:
                encoded = url_id[2:]
                padding = 4 - len(encoded) % 4
                if padding != 4:
                    encoded += "=" * padding
                return base64.urlsafe_b64decode(encoded).decode()
            except Exception:
                return None
        return None

    async def cleanup(self):
        """Pulizia delle risorse"""
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            if self.flex_session and not self.flex_session.closed:
                await self.flex_session.close()

            # Close all cached proxy sessions
            for proxy_url, session in list(self.proxy_sessions.items()):
                if session and not session.closed:
                    await session.close()
            self.proxy_sessions.clear()
            self._proxy_session_atimes.clear()

            # Close all cached curl sessions
            for session in list(self.curl_sessions.values()):
                if session:
                    await session.close()
            self.curl_sessions.clear()

            for extractor in self.extractors.values():
                if hasattr(extractor, "close"):
                    await extractor.close()
            self._extractor_atimes.clear()
            self._extractor_stream_atimes.clear()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

async def _warp_cli_connect() -> bool:
    """Standalone WARP warp-cli setup: disconnect, re-register, mode proxy, connect."""
    import config_store, services.proxy_shared as _shared
    try:
        for cmd in [
            ["warp-cli", "--accept-tos", "disconnect"],
            ["warp-cli", "--accept-tos", "registration", "delete"],
            ["warp-cli", "--accept-tos", "registration", "new"],
        ]:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await asyncio.wait_for(proc.wait(), timeout=10)
            if rc != 0:
                return False
        await asyncio.sleep(2)
        license_key = _shared.WARP_LICENSE_KEY or config_store.get("warp_license_key", "")
        if license_key:
            proc = await asyncio.create_subprocess_exec(
                "warp-cli", "--accept-tos", "registration", "license", license_key,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        for sub_cmd in [["mode", "proxy"], ["proxy", "port", "1080"]]:
            proc = await asyncio.create_subprocess_exec(
                "warp-cli", "--accept-tos", *sub_cmd,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        proc = await asyncio.create_subprocess_exec(
            "warp-cli", "--accept-tos", "connect",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=15)
        if rc != 0:
            return False
        # Verify SOCKS5 is actually listening
        import socket
        for _ in range(5):
            try:
                s = socket.create_connection(("127.0.0.1", 1080), timeout=2)
                s.close()
                return True
            except (OSError, ConnectionRefusedError):
                await asyncio.sleep(1)
        return False
    except (FileNotFoundError, asyncio.TimeoutError):
        return False

def _kill_wireproxy():
    """Kill wireproxy by scanning /proc."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm") as f:
                    if "wireproxy" in f.read():
                        os.kill(int(entry), 9)
            except (OSError, ProcessLookupError):
                pass
    except Exception:
        pass
