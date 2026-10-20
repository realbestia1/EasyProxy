import asyncio
import gc
import gzip
from contextlib import asynccontextmanager
import hmac
import logging
import os
import re
import secrets
import time
import urllib.parse
import aiohttp
import base64
import hashlib
import socket
import config as _config
import services.wg_tunnels as wg_tunnels
from services.session_lifetime import retire_session
from services.socks_bridge import close_socks_bridges

import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
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

# Docker keeps the helper at /app/scripts, Termux/native checkouts run it from
# the repository: resolve it relative to the package and always run it through
# /bin/sh (git checkouts do not carry the executable bit).
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WARP_CTL_SCRIPT = os.environ.get("WARP_CTL_SCRIPT") or os.path.join(
    _PROJECT_DIR, "scripts", "wireproxy_ctl.sh"
)


class SharedSessionWrapper:
    def __init__(self, session):
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def __setattr__(self, name, value):
        setattr(self._session, name, value)

    @property
    def closed(self) -> bool:
        return self._session.closed

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class HLSProxyCoreMixin:

    # Extractor polls for the same source+client reuse one playback namespace
    # for this long (matches the 60s per-stream session idle TTL).
    STREAM_KEY_REUSE_SECONDS = 60.0

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
        if any(not task.done() for task in self._background_tasks):
            return
        self._last_warp_reconnect_time = time.time()  # ponytail: startup cooldown to allow initial handshake
        self._background_tasks = {
            asyncio.create_task(self._update_latest_version()),
            asyncio.create_task(self._cleanup_stale_sessions()),
            asyncio.create_task(self._warp_keepalive()),
        }

    async def _cleanup_stale_sessions(self):
        """Periodic cleanup of stale CDN tokens and idle proxy sessions to prevent memory accumulation when idle."""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                
                # 1. Clean up stale HLS/CDN tokens (>300s)
                stale_tokens = [
                    k for k, t in getattr(self, '_renewed_cdn_token_atimes', {}).items()
                    if now - t > 300
                ]
                for k in stale_tokens:
                    self._renewed_cdn_tokens.pop(k, None)
                    self._renewed_cdn_token_atimes.pop(k, None)
                    logger.debug("🧹 Cleaned stale CDN token: %s", k[:8])

                # 2. Clean up idle proxy sessions (>60s)
                if hasattr(self, "_proxy_sessions") and hasattr(self, "_proxy_session_atimes"):
                    _warp_url = _shared.WARP_PROXY_URL
                    stale_proxies = [
                        p for p, t in list(self._proxy_session_atimes.items())
                        if now - t > 60 and p != _warp_url
                    ]
                    for p in stale_proxies:
                        p_sess = self._proxy_sessions.pop(p, None)
                        self._proxy_session_atimes.pop(p, None)
                        if p_sess and not p_sess.closed:
                            retire_session(self, p_sess)
                            logger.info(f"[NET] Closed idle proxy session: {p}")

                # Keep independent WARP connector pools for concurrent media
                # streams.  A stale/busy connector must not stall another
                # stream that happens to use the same WARP proxy URL.
                stream_sessions = getattr(self, "_stream_proxy_sessions", None)
                stream_atimes = getattr(self, "_stream_proxy_session_atimes", None)
                if stream_sessions is not None and stream_atimes is not None:
                    stale_streams = [
                        key for key, t in list(stream_atimes.items())
                        if now - t > 60
                    ]
                    for key in stale_streams:
                        p_sess = stream_sessions.pop(key, None)
                        stream_atimes.pop(key, None)
                        if p_sess and not p_sess.closed:
                            retire_session(self, p_sess)
                            logger.info("[NET] Closed idle media session: %s", key[1])

                # 3. Close shared session if idle >30s
                _session_atime = getattr(self, "_session_atime", 0)
                if _session_atime and now - _session_atime > 30:
                    if self.session and not self.session.closed:
                        retire_session(self, self.session)
                        logger.info("[NET] Closed idle shared session (idle %.0fs)", now - _session_atime)
                    self.session = None
                    if self.flex_session and not self.flex_session.closed:
                        retire_session(self, self.flex_session)
                        logger.info("[NET] Closed idle flex session (idle %.0fs)", now - _session_atime)
                    self.flex_session = None

                # 3b. Close retired extractors older than 60s
                if hasattr(self, "_retired_extractors") and self._retired_extractors:
                    atimes = getattr(self, "_retired_extractor_atimes", None)
                    if atimes is None:
                        atimes = self._retired_extractor_atimes = {}
                    still_retired = []
                    for ext in self._retired_extractors:
                        t = atimes.setdefault(id(ext), now)
                        if now - t > 60:
                            if hasattr(ext, "close"):
                                try:
                                    await ext.close()
                                except Exception:
                                    pass
                            atimes.pop(id(ext), None)
                        else:
                            still_retired.append(ext)
                    self._retired_extractors = still_retired

                # 4. Compact Windows heap to release freed pages
                await self._compact_heap()

                # 5. WireProxy is a Go process: closed sockets can leave heap
                # pages in its RSS. Recycle only after real WARP inactivity;
                # health probes do not count as playback activity.
                await self._recycle_idle_warp(time.monotonic())

            except Exception as e:
                logger.error("Cleanup stale sessions error: %s", e)
                await asyncio.sleep(10)

    async def _compact_heap(self):
        """Release freed heap pages back to the OS (Linux: malloc_trim, Windows: HeapCompact)."""
        try:
            gc.collect()
            import ctypes
            import platform
            if platform.system() == "Windows":
                ctypes.windll.kernel32.HeapCompact(
                    ctypes.windll.kernel32.GetProcessHeap(), 0
                )
            else:
                try:
                    libc = ctypes.CDLL("libc.so.6")
                    libc.malloc_trim(0)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _warp_established_connections() -> int | None:
        """Count clients currently connected to WireProxy's SOCKS listener."""
        paths = ("/proc/net/tcp", "/proc/net/tcp6")
        if not any(os.path.exists(path) for path in paths):
            return None
        count = 0
        try:
            for path in paths:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="ascii") as source:
                    for line in source:
                        fields = line.split()
                        if len(fields) > 3 and fields[1].rsplit(":", 1)[-1].upper() == "0438" and fields[3] == "01":
                            count += 1
        except (OSError, UnicodeError):
            return None
        return count

    async def _recycle_idle_warp(self, now: float) -> None:
        """Restart WireProxy after idle to return retained Go heap to the OS."""
        if not _shared.ENABLE_WARP:
            return
        last_activity = getattr(_config, "WARP_LAST_ACTIVITY", 0.0)
        if not last_activity or now - last_activity < 120:
            return
        if self._warp_established_connections() != 0:
            return
        lock = getattr(self, "_warp_idle_recycle_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._warp_idle_recycle_lock = lock
        async with lock:
            last_activity = getattr(_config, "WARP_LAST_ACTIVITY", 0.0)
            if not last_activity or time.monotonic() - last_activity < 120:
                return
            if self._warp_established_connections() != 0:
                return
            warp_url = _shared.WARP_PROXY_URL
            await self._invalidate_proxy_session(
                warp_url,
                invalidate_streams=True,
            )
            try:
                rc = await self._run_warp_control("restart")
            except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("[NET] Idle WireProxy recycle failed: %s", exc)
                return
            if rc == 0:
                if await self._wait_for_warp_socket():
                    _config.WARP_LAST_ACTIVITY = 0.0
                    logger.info("[NET] Recycled idle WireProxy after 120s; WARP sockets/RSS reset")
                else:
                    logger.warning("[NET] WireProxy restarted but SOCKS port 1080 did not recover")
            else:
                logger.warning("[NET] Idle WireProxy recycle returned code %s", rc)

    @staticmethod
    async def _wait_for_warp_socket(timeout: float = 20.0) -> bool:
        """Wait until the restarted WireProxy SOCKS listener accepts clients."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 1080), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.25)
        return False


    async def _wireproxy_process_state(self) -> tuple[str, str]:
        """Return wireproxy process state without confusing it with tunnel state."""
        control_script = WARP_CTL_SCRIPT
        if not os.path.exists(control_script):
            return "unknown", "control script unavailable"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                control_script,
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
            detail = (stdout or stderr).decode("utf-8", "replace").strip()
            if proc.returncode == 0:
                return "up", detail or "status=running"
            return "down", detail or f"status exit={proc.returncode}"
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return "unknown", "status check timeout"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as exc:
            return "unknown", f"status check error={type(exc).__name__}"

    async def _probe_warp(self, timeout_sec: float = 8.0) -> tuple[bool, str]:
        """Check wireproxy process, SOCKS listener, and real WARP HTTP path."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        _WARP_PROXY_URL = _shared.WARP_PROXY_URL
        if not _ENABLE_WARP or not _WARP_PROXY_URL:
            self._warp_ip = ""
            return False, "WARP disabled or proxy URL missing"

        self._warp_ip = ""

        process_state, process_detail = await self._wireproxy_process_state()
        socket_error = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 1080),
                timeout=min(3.0, timeout_sec),
            )
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            socket_error = type(exc).__name__

        if socket_error:
            component = "wireproxy_process" if process_state == "down" else "wireproxy_socket"
            return False, (
                f"component={component} process={process_state}({process_detail}) "
                f"socks=down error={socket_error}"
            )

        try:
            connector = get_connector_for_proxy(
                _WARP_PROXY_URL, limit=0, health_check=True
            )
            timeout = ClientTimeout(total=timeout_sec)
            async with ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get("https://www.cloudflare.com/cdn-cgi/trace") as resp:
                    body = await resp.text()

            trace = {
                line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
                for line in body.splitlines()
                if "=" in line
            }
            warp_state = trace.get("warp", "").lower()
            if resp.status != 200:
                return False, (
                    f"component=warp_tunnel process={process_state} socks=up "
                    f"http_status={resp.status}"
                )
            if warp_state not in {"on", "plus"}:
                return False, (
                    f"component=warp_tunnel process={process_state} socks=up "
                    f"warp={warp_state or 'unknown'}"
                )

            self._warp_ip = trace.get("ip", "")
            return True, (
                f"process={process_state} socks=up warp={warp_state} "
                f"ip={self._warp_ip or 'unknown'}"
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())[:240]
            suffix = f" detail={detail}" if detail else ""
            return False, (
                f"component=warp_tunnel process={process_state} socks=up "
                f"error={type(exc).__name__}{suffix}"
            )

    async def _warp_keepalive(self):
        """Probe WARP periodically and reconnect after consecutive failures."""
        consecutive_failures = 0
        while True:
            try:
                await asyncio.sleep(30)
                if not _shared.ENABLE_WARP or not _shared.WARP_PROXY_URL:
                    consecutive_failures = 0
                    continue

                healthy, reason = await self._probe_warp(timeout_sec=12)
                if healthy:
                    if consecutive_failures:
                        logger.warning(
                            "WARP health recovered after %s failed probe(s)",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    continue

                consecutive_failures += 1
                logger.warning(
                    "WARP health check failed (%s/2): %s",
                    consecutive_failures,
                    reason,
                )
                if consecutive_failures < 2:
                    continue

                result = await self.reconnect_warp()
                message = result.get("message", "")
                if result.get("status") == "ok" and message == "WARP userspace tunnel reconnected":
                    logger.warning("WARP auto-reconnect succeeded")
                    consecutive_failures = 0
                else:
                    logger.warning("WARP auto-reconnect skipped/failed: %s", message or result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WARP keepalive error: %s", e)
                await asyncio.sleep(10)

    async def is_warp_healthy(self, timeout_sec: float = 3.0) -> bool:
        """Fast check if WARP proxy socket and HTTP connectivity are working."""
        healthy, _reason = await self._probe_warp(timeout_sec=timeout_sec)
        return healthy

    async def _restart_warp_if_socket_unhealthy(self, reason: str) -> bool:
        """Recover a stalled local WireProxy listener without masking origin errors."""
        if not any(
            marker in reason
            for marker in ("component=wireproxy_socket", "component=wireproxy_process")
        ):
            return False

        result = await self.reconnect_warp()
        if (
            result.get("status") == "ok"
            and result.get("message") == "WARP userspace tunnel reconnected"
        ):
            logger.warning("WARP socket recovery restarted wireproxy")
            return True

        logger.warning(
            "WARP socket recovery skipped/failed: %s",
            result.get("message") or result,
        )
        return False

    async def get_warp_status(self) -> str:
        """Return cached WARP status; avoid probing SOCKS on every admin poll."""
        if not _shared.ENABLE_WARP or not _shared.WARP_PROXY_URL:
            self.warp_status = "Disabled"
            self._warp_ip = ""
            self._warp_status_reason = "WARP disabled or proxy URL missing"
            self._warp_status_checked_at = time.monotonic()
            return self.warp_status

        now = time.monotonic()
        if (
            self.warp_status in {"Connected", "Disconnected"}
            and now - getattr(self, "_warp_status_checked_at", 0.0) < 15.0
        ):
            return self.warp_status

        healthy, _reason = await self._probe_warp(timeout_sec=10)
        self.warp_status = "Connected" if healthy else "Disconnected"
        self._warp_status_reason = _reason
        self._warp_status_checked_at = time.monotonic()
        return self.warp_status

    async def _run_warp_control(self, action: str) -> int:
        """Run the explicit userspace WARP control action."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                WARP_CTL_SCRIPT,
                action,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await asyncio.wait_for(proc.wait(), timeout=20)
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            logger.warning("WARP control %s timed out", action)
            return 124
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except OSError as exc:
            logger.warning("WARP control %s failed: %s", action, exc)
            return 127

    async def reconnect_warp(self) -> dict:
        """Restart wireproxy and verify the WARP path after reconnect."""
        if not hasattr(self, "_warp_reconnect_lock"):
            self._warp_reconnect_lock = asyncio.Lock()

        now = time.time()
        last_reconnect = getattr(self, "_last_warp_reconnect_time", 0)
        if now - last_reconnect < 60:
            return {"status": "ok", "message": "Cooldown active"}
        if self._warp_reconnect_lock.locked():
            return {"status": "ok", "message": "Reconnect already in progress"}

        async with self._warp_reconnect_lock:
            self._last_warp_reconnect_time = time.time()
            try:
                rc = await self._run_warp_control("restart")
                if rc != 0:
                    return {"status": "error", "message": "wireproxy restart failed"}
                # The control script returns as soon as wireproxy is spawned;
                # probing now races the SOCKS listener bind.
                if not await self._wait_for_warp_socket(timeout=10.0):
                    return {
                        "status": "error",
                        "message": "wireproxy restarted but SOCKS port 1080 did not come up",
                    }
                healthy, reason = await self._probe_warp(timeout_sec=8)
                self.warp_status = "Connected" if healthy else "Disconnected"
                self._warp_status_reason = reason
                self._warp_status_checked_at = time.monotonic()
                if healthy:
                    return {"status": "ok", "message": "WARP userspace tunnel reconnected"}
                return {
                    "status": "error",
                    "message": f"wireproxy restart completed but WARP probe failed: {reason}",
                }
            except (FileNotFoundError, asyncio.TimeoutError) as exc:
                return {"status": "error", "message": f"WARP reconnect failed: {exc}"}

    async def _stop_warp_proxy(self):
        try:
            await self._run_warp_control("stop")
        except Exception as exc:
            logger.debug("WARP userspace process already stopped: %s", exc)

    async def _update_latest_version(self):
        """Periodically checks GitHub for the latest version in the background."""
        while True:
            try:
                await self._refresh_latest_version()
            except Exception as e:
                logger.error("Version check error: %s", e)
            await asyncio.sleep(3600)

    async def _refresh_latest_version(self):
        """Checks GitHub config.py for the latest version with cache busting.
        Can be called on-demand (e.g. on page refresh).
        Uses its own temporary session to avoid resetting the shared session idle timer.
        """
        lock = getattr(self, "_latest_version_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._latest_version_lock = lock

        # Serialize foreground page loads and the background check. The old
        # implementation marked the check as complete before doing I/O, so a
        # page could render "Checking..." while another task was still fetching.
        async with lock:
            now = time.monotonic()
            checked_at = getattr(self, "_latest_version_checked_at", 0.0)
            if checked_at > 0.0 and now - checked_at < 3600.0:
                if self.latest_version == "Checking...":
                    self.latest_version = "Unknown"
                return

            try:
                cache_buster = int(time.time())
                url = f"https://raw.githubusercontent.com/realbestia1/EasyProxy/main/config.py?t={cache_buster}"

                connector = TCPConnector(limit=1, limit_per_host=1, keepalive_timeout=5)
                timeout = ClientTimeout(total=5)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(url, timeout=2) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
                            if match:
                                new_version = match.group(1)
                                if self.latest_version != new_version:
                                    self.latest_version = new_version
                                    logger.info(f"🆕 Latest version updated: {self.latest_version}")
                            else:
                                self.latest_version = "Unknown"
                        else:
                            self.latest_version = "Error"
            except Exception as e:
                self.latest_version = "Unknown"
                logger.debug(f"Version check skipped or failed: {e}")
            finally:
                self._latest_version_checked_at = time.monotonic()

    @staticmethod
    def _png_trailing_ts(content: bytes) -> bytes | None:
        """TS packet(s) appended after the PNG IEND chunk (daddyliveplayer.st)."""
        off = 8
        while off + 8 <= len(content):
            length = int.from_bytes(content[off:off + 4], "big")
            kind = content[off + 4:off + 8]
            off += 8 + length + 4
            if kind == b"IEND":
                tail = content[off:]
                if len(tail) > 188 and tail[0] == 0x47 and tail[188] == 0x47:
                    return tail
                return None
            if length > len(content) - off:
                return None
        return None

    @staticmethod
    def _webp_exif_ts(content: bytes) -> bytes | None:
        """TS packet(s) stored in the WebP EXIF chunk (daddyliveplayer.st)."""
        off = 12
        while off + 8 <= len(content):
            kind = content[off:off + 4]
            length = int.from_bytes(content[off + 4:off + 8], "little")
            off += 8
            if off + length > len(content):
                return None
            if kind == b"EXIF":
                data = content[off:off + length]
                if len(data) > 188 and data[0] == 0x47 and data[188] == 0x47:
                    return bytes(data)
                return None
            off += length + (length & 1)
        return None

    @staticmethod
    def _unwrap_image_ts_payload(content: bytes) -> bytes | None:
        """Recover raw MPEG-TS wrapped in an image container.

        daddyliveplayer.st / dlive.sx obfuscate premium channels by shipping
        every segment as a WebP/PNG image. The real TS payload is either the
        EXIF chunk (WebP), the bytes after the PNG IEND chunk, a gzip
        payload hidden in the PNG pixel data behind a "TIKTIKPX" magic and a
        big-endian length, a raw payload behind "TIKTIKRAW", a gzip payload
        behind "TIKTIKTSGZ", or plain TS bytes detectable via the 0x47 sync
        pattern. Mirrors the provider's own JS unwrapper (unwrap/webpExifTS/
        pngIendTS/unwrapPixels + TRAW/TSGZ/generic 0x47 scan).
        """
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return HLSProxyCoreMixin._webp_exif_ts(content)
        is_png = content.startswith(b"\x89PNG\r\n\x1a\n")

        if is_png:
            trailing = HLSProxyCoreMixin._png_trailing_ts(content)
            if trailing is not None:
                return trailing

            try:
                import io
                from PIL import Image
                raw = Image.open(io.BytesIO(content)).convert("RGB").tobytes()
            except Exception:
                raw = None
            if raw is not None:
                if raw[:8] == b"TIKTIKPX":
                    length = int.from_bytes(raw[8:12], "big")
                    if 2 <= length <= len(raw) - 12:
                        gz = raw[12:12 + length]
                        if gz[:2] == b"\x1f\x8b":
                            try:
                                ts = gzip.decompress(gz)
                            except Exception:
                                ts = None
                            if ts is not None and len(ts) > 188 and ts[0] == 0x47 and ts[188] == 0x47:
                                return ts
            # PNG without a recognized payload: fall through to the raw
            # magic / generic sync scan below (mirrors the JS unwrapper,
            # which keeps searching TRAW/TSGZ/0x47 on the raw file bytes).

        # Raw TS behind the "TIKTIKRAW" magic (JS: TRAW, first byte 0x47).
        # Require the +188 sync as well to avoid false positives.
        traw = content.find(b"TIKTIKRAW")
        if traw != -1:
            ts = content[traw + len(b"TIKTIKRAW"):]
            if len(ts) > 188 and ts[0] == 0x47 and ts[188] == 0x47:
                return bytes(ts)

        # Gzip TS behind the "TIKTIKTSGZ" magic (JS: TSGZ -> gunzip).
        tsgz = content.find(b"TIKTIKTSGZ")
        if tsgz != -1:
            try:
                ts = gzip.decompress(content[tsgz + len(b"TIKTIKTSGZ"):])
            except Exception:
                ts = None
            if ts is not None and len(ts) > 188 and ts[0] == 0x47 and ts[188] == 0x47:
                return ts

        # Generic fallback (JS: first 0x47 with another 0x47 at +188).
        # Use find() iteration (C speed) and require a third sync at +376
        # when available to avoid false positives inside PNG bytes.
        pos = content.find(b"\x47")
        end = len(content) - 188
        while pos != -1 and pos <= end:
            if content[pos + 188] == 0x47 and (
                len(content) <= pos + 376 or content[pos + 376] == 0x47
            ):
                return bytes(content[pos:])
            pos = content.find(b"\x47", pos + 1, end + 1)
        return None

    @staticmethod
    def _strip_fake_png_header_from_ts(content: bytes) -> bytes:
        """
        Some providers prepend a fake PNG payload to TS segments.
        embed.st/strmd.st wraps segments as a minimal valid PNG (IHDR+IDAT+IEND,
        ~70 bytes) followed by the raw MPEG-TS stream. ExoPlayer scans for the
        TS sync byte and tolerates this; stricter players (MPV/hls.js used by
        Stremio PC) do not, and stall forever.

        daddyliveplayer.st goes further and hides TS inside the WebP/PNG image
        itself, so try the full image unwrap first.

        We locate the first 0x47 sync byte that is followed by another 0x47 at
        +188 bytes (the TS packet size), then strip everything before it. If no
        such pattern is found we fall back to the legacy 8-byte PNG signature
        strip so we don't regress providers that only prepend 8 bytes.
        """
        if not content:
            return content

        png_sig = b"\x89PNG\r\n\x1a\n"
        is_image = content.startswith(png_sig) or (
            content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        )
        if is_image:
            unwrapped = HLSProxyCoreMixin._unwrap_image_ts_payload(content)
            if unwrapped is not None:
                logger.debug(
                    "🔓 Unwrapped TS payload from image segment (%d -> %d bytes)",
                    len(content), len(unwrapped),
                )
                return unwrapped

        # Fast path: not a PNG at all -> nothing to do.
        if not content.startswith(png_sig):
            return content

        # Generic path: scan for the first TS sync byte (0x47) that repeats at
        # +188. Bound the scan so we never iterate over a huge payload.
        scan_limit = min(4096, len(content) - 188)
        for i in range(0, scan_limit):
            if content[i] == 0x47 and content[i + 188] == 0x47:
                if i <= 8:
                    return content  # already a clean TS
                payload = content[i:]
                # Sanity-check a few more packet boundaries to avoid false positives.
                if len(payload) > 376 and payload[376] != 0x47:
                    continue
                logger.info(
                    "Removed fake PNG header from TS segment (%d -> %d bytes, header=%d)",
                    len(content), len(payload), i,
                )
                return payload

        # Legacy fallback: strip only the 8-byte PNG signature when the bytes
        # right after it look like a TS packet.
        if len(content) > 8:
            ts_payload = content[8:]
            if ts_payload and ts_payload[0] == 0x47:
                if len(ts_payload) <= 188 or ts_payload[188] == 0x47:
                    logger.info(
                        "Removed fake PNG header from TS segment (%d -> %d bytes)",
                        len(content), len(ts_payload),
                    )
                    return ts_payload

        return content

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
                "keepalive_timeout": 15,
                "enable_cleanup_closed": True,
                "use_dns_cache": True,
            }
            # The known-good MPD path used IPv4 for DIRECT connections.
            # Keep WARP/proxy routes dual-stack; this only avoids broken VPS
            # IPv6 paths for direct CDN requests.
            if not prefer_default_family:
                connector_kwargs["family"] = socket.AF_INET
            connector = TCPConnector(**connector_kwargs)
            session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30),
                connector=connector,
            )
            setattr(self, target_attr, session)
        self._session_atime = time.time()
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
                    logging.info(f"⚠️ [Dynamic Bypass] Adding {base_domain} (and {domain}) to the EasyProxy WARP exclusion list...")

                    _WARP_EXCLUDE_DOMAINS = _shared.WARP_EXCLUDE_DOMAINS
                    if isinstance(_WARP_EXCLUDE_DOMAINS, list):
                        if base_domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(base_domain)
                        if domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(domain)

                    BYPASSED_WARP_DOMAINS.add(domain)
                    BYPASSED_WARP_DOMAINS.add(base_domain)
        except Exception as e:
            logging.error(f"❌ Error in dynamic WARP bypass: {e}")

    async def _get_proxy_session(
        self,
        url: str,
        bypass_warp: bool = False,
        forced_proxy: str | None = None,
        session_key: str | None = None,
        force_direct: bool = False,
    ):
        """Create a fresh session or reuse an existing one for the given URL.

        Returns: (session, proxy_url) tuple
        - session: The aiohttp ClientSession (wrapped) to use
        - proxy_url: The proxy URL being used, or None for direct connection
        """
        await self._check_dynamic_warp_bypass(url)

        if forced_proxy:
            forced_proxy = urllib.parse.unquote(forced_proxy)
            forced_proxy = _config.resolve_proxy_alias(forced_proxy)
            if forced_proxy.lower() == "off":
                forced_proxy = None

        # proxy=off disables configured/forced proxies. Keep a forced proxy
        # active when only WARP is bypassed: proxies command over WARP.
        if _shared.BYPASS_PROXIES_CONTEXT.get():
            forced_proxy = None

        # A generated manifest/segment URL can outlive an admin toggle and
        # still carry the old WARP proxy in its query string. WARP=off or the
        # admin WARP switch must invalidate that stale route before selecting
        # a connector; otherwise direct mode still tries 127.0.0.1:1080.
        if forced_proxy and _config.is_warp_proxy_url(forced_proxy) and (
            bypass_warp or not _config._get_dynamic_warp_enabled()
        ):
            logger.debug(
                "Ignoring stale WARP route %s (WARP bypassed/disabled)",
                forced_proxy,
            )
            forced_proxy = None

        # ponytail: proxy_exclude_domains drops even explicit ?proxy= (WARP exempt)
        forced_proxy = _config.effective_forced_proxy(url, forced_proxy)

        # Stale proxy sessions cleanup (>60s idle, aligned with connector
        # keepalive_timeout). The WARP session stays pooled; reusable upstream
        # sockets avoid rebuilding SOCKS+TLS for every media request.
        if hasattr(self, "_proxy_session_atimes"):
            now = time.time()
            _warp_url = _shared.WARP_PROXY_URL
            stale = [p for p, t in self._proxy_session_atimes.items() if now - t > 60 and p != _warp_url]
            for p_url in stale:
                p_sess = self._proxy_sessions.pop(p_url, None)
                self._proxy_session_atimes.pop(p_url, None)
                if p_sess and not p_sess.closed:
                    retire_session(self, p_sess)

        proxy = None if force_direct else (forced_proxy or get_proxy_for_url(url, bypass_warp=bypass_warp))
        # A host explicitly excluded from WARP with no proxy left in the chain has
        # only one route: direct. Without this, warp_exclude_domains + proxy=off is
        # a dead end ("No proxy route available") instead of the split routing it
        # declares (e.g. daddy hosts via WARP, TikTok CDN direct, whose WARP egress
        # is banned while the server IP is clean). Untouched otherwise.
        direct_ok = _config.is_direct_connection_allowed(bypass_warp) or (
            not proxy and _config._is_warp_excluded(url or "")
        )
        if not proxy and not force_direct and not direct_ok:
            raise aiohttp.ClientConnectionError(
                "No proxy route available; direct fallback disabled"
            )
        if proxy == _shared.WARP_PROXY_URL:
            # Refresh on every real request, not only when the pooled session
            # is created; this prevents recycling during an active HLS stream.
            _config.WARP_LAST_ACTIVITY = time.monotonic()

        prefer_default_family = prefer_default_family_for_url(url)

        if proxy:
            if not hasattr(self, "_proxy_sessions"):
                self._proxy_sessions = {}
                self._proxy_session_atimes = {}

            # Media requests get an isolated pool for every playback, no
            # matter whether route is WARP, another proxy, or direct.
            use_stream_pool = bool(session_key)
            if use_stream_pool:
                if not hasattr(self, "_stream_proxy_sessions"):
                    self._stream_proxy_sessions = {}
                    self._stream_proxy_session_atimes = {}
                stream_key = (proxy, str(session_key))
                stream_session = self._stream_proxy_sessions.get(stream_key)
                if stream_session is None or stream_session.closed:
                    logger.info(
                        "[NET] Creating per-stream proxy session: %s",
                        str(session_key)[:32],
                    )
                    connector = get_connector_for_proxy(
                        proxy,
                        limit=0,
                        limit_per_host=0,
                        keepalive_timeout=15,
                        enable_cleanup_closed=True,
                    )
                    timeout = ClientTimeout(
                        total=None,
                        connect=30,
                        sock_connect=30,
                        sock_read=30,
                    )
                    stream_session = ClientSession(
                        timeout=timeout,
                        connector=connector,
                    )
                    self._stream_proxy_sessions[stream_key] = stream_session
                self._stream_proxy_session_atimes[stream_key] = time.time()
                return SharedSessionWrapper(stream_session), proxy

            # Evict oldest session if cache gets too large (e.g. >= 10) to prevent memory leak
            if len(self._proxy_sessions) >= 10 and proxy not in self._proxy_sessions:
                try:
                    _warp_url = _shared.WARP_PROXY_URL
                    candidates = [p for p in self._proxy_sessions if p != _warp_url]
                    if candidates:
                        oldest_proxy = min(candidates, key=lambda p: self._proxy_session_atimes.get(p, 0))
                        oldest_sess = self._proxy_sessions.pop(oldest_proxy, None)
                        self._proxy_session_atimes.pop(oldest_proxy, None)
                        if oldest_sess and not oldest_sess.closed:
                            retire_session(self, oldest_sess)
                            logger.info(f"[NET] Evicted oldest proxy session: {oldest_proxy}")
                except Exception as e:
                    logger.warning(f"Failed to evict proxy session: {e}")

            if proxy not in self._proxy_sessions or self._proxy_sessions[proxy].closed:
                logger.info(f"[NET] Creating pooled proxy session: {proxy}")
                try:
                    connector_kwargs = {
                        "limit": 0,
                        "limit_per_host": 0,
                    }
                    if _shared.WARP_PROXY_URL and proxy == _shared.WARP_PROXY_URL:
                        # Reuse short-lived upstream connections during HLS
                        # playback. Network failures invalidate this pooled
                        # session and trigger a fresh WARP connection.
                        connector_kwargs["keepalive_timeout"] = 15
                        connector_kwargs["enable_cleanup_closed"] = True
                    else:
                        connector_kwargs["keepalive_timeout"] = 15
                    connector = get_connector_for_proxy(proxy, **connector_kwargs)
                    timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30)
                    session = ClientSession(timeout=timeout, connector=connector)
                    self._proxy_sessions[proxy] = session
                except Exception as e:
                    logger.warning(f"Failed to create proxy connector: {e}")
                    raise
            else:
                session = self._proxy_sessions[proxy]

            self._proxy_session_atimes[proxy] = time.time()
            return SharedSessionWrapper(session), proxy

        if session_key:
            if not hasattr(self, "_stream_proxy_sessions"):
                self._stream_proxy_sessions = {}
                self._stream_proxy_session_atimes = {}
            stream_key = (None, str(session_key), prefer_default_family)
            stream_session = self._stream_proxy_sessions.get(stream_key)
            if stream_session is None or stream_session.closed:
                logger.info(
                    "[NET] Creating per-stream direct session: %s",
                    str(session_key)[:32],
                )
                connector_kwargs = {
                    "limit": 0,
                    "limit_per_host": 0,
                    "keepalive_timeout": 15,
                    "enable_cleanup_closed": True,
                    "use_dns_cache": True,
                }
                if not prefer_default_family:
                    connector_kwargs["family"] = socket.AF_INET
                connector = TCPConnector(**connector_kwargs)
                stream_session = ClientSession(
                    timeout=ClientTimeout(
                        total=None,
                        connect=30,
                        sock_connect=30,
                        sock_read=30,
                    ),
                    connector=connector,
                )
                self._stream_proxy_sessions[stream_key] = stream_session
            self._stream_proxy_session_atimes[stream_key] = time.time()
            return SharedSessionWrapper(stream_session), None

        session = await self._get_session(prefer_default_family=prefer_default_family)
        return SharedSessionWrapper(session), None

    async def _recover_tunnel_proxy(self, proxy_url: str | None) -> bool:
        """Reconnect the local WireGuard tunnel behind proxy_url and refresh sessions.

        Returns True only when the tunnel was actually restarted and its SOCKS
        bind is ready, so callers can retry the failed request.
        """
        slot = wg_tunnels.slot_for_proxy_url(proxy_url)
        if not slot or not await wg_tunnels.reconnect(slot):
            return False
        _config.clear_proxy_dead(proxy_url)
        await self._invalidate_proxy_session(proxy_url, invalidate_streams=True)
        logger.warning("[NET] Recovered WireGuard tunnel %s for %s", slot, proxy_url)
        return True

    async def _invalidate_proxy_session(
        self,
        proxy_url: str | None,
        session_key: str | None = None,
        invalidate_streams: bool = False,
    ) -> bool:
        """Drop one pooled proxy session so the next request gets a new connector."""
        if not proxy_url:
            return False

        invalidated = False
        stream_sessions = getattr(self, "_stream_proxy_sessions", None)
        stream_atimes = getattr(self, "_stream_proxy_session_atimes", None)
        if (
            stream_sessions is not None
            and stream_atimes is not None
            and (session_key is not None or invalidate_streams)
        ):
            stream_keys = [
                key for key in stream_sessions
                if key[0] == proxy_url
                and (
                    invalidate_streams
                    or key[1] == str(session_key)
                )
            ]
            for key in stream_keys:
                session = stream_sessions.pop(key, None)
                stream_atimes.pop(key, None)
                if session and not session.closed:
                    retire_session(self, session)
                invalidated = True

        # A stream-scoped WARP session must not invalidate the shared WARP
        # session used by other requests/streams.
        if session_key is not None and proxy_url == _shared.WARP_PROXY_URL:
            if invalidated:
                logger.warning("[NET] Invalidated stream proxy session: %s", proxy_url)
            return invalidated

        if not hasattr(self, "_proxy_sessions"):
            if invalidated:
                logger.warning("[NET] Invalidated stream proxy session: %s", proxy_url)
            return invalidated
        session = self._proxy_sessions.pop(proxy_url, None)
        if hasattr(self, "_proxy_session_atimes"):
            self._proxy_session_atimes.pop(proxy_url, None)
        if not session:
            if invalidated:
                logger.warning("[NET] Invalidated stream proxy session: %s", proxy_url)
            return invalidated
        if not session.closed:
            retire_session(self, session)
        logger.warning("[NET] Invalidated pooled proxy session: %s", proxy_url)
        return True

    async def _invalidate_direct_session(
        self,
        url: str | None = None,
        session_key: str | None = None,
    ) -> bool:
        """Detach a stale shared DIRECT connector without changing routing policy."""
        prefer_default_family = prefer_default_family_for_url(url or "")
        if session_key is not None:
            stream_sessions = getattr(self, "_stream_proxy_sessions", None)
            stream_atimes = getattr(self, "_stream_proxy_session_atimes", None)
            if stream_sessions is not None and stream_atimes is not None:
                key = (None, str(session_key), prefer_default_family)
                session = stream_sessions.pop(key, None)
                stream_atimes.pop(key, None)
                if session and not session.closed:
                    retire_session(self, session)
                if session:
                    logger.warning("[NET] Invalidated stream DIRECT session: %s", key[1])
                return bool(session)
        attr = "flex_session" if prefer_default_family else "session"
        session = getattr(self, attr, None)
        if session is None or session.closed:
            setattr(self, attr, None)
            return False

        setattr(self, attr, None)
        retire_session(self, session)
        logger.warning("[NET] Invalidated pooled DIRECT session: %s", attr)
        return True

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
                keepalive_timeout=15,
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
            for ek, ev in self.extractors.items():
                if ev is result:
                    self._extractor_atimes[ek] = time.time()
                    break
        return result

    def _extractor_key_for_instance(self, extractor) -> str | None:
        for key, cached_extractor in self.extractors.items():
            if cached_extractor is extractor:
                return key
        if extractor is not None:
            name = getattr(extractor, "extractor_name", None)
            if name:
                return name
        return None

    @asynccontextmanager
    async def hold_extractor(self, extractor):
        """Use an already-resolved extractor; retire it when the last
        concurrent user leaves, even if the block raises."""
        inflight = getattr(self, "_extractor_inflight", None)
        if inflight is None:
            inflight = self._extractor_inflight = {}
        key = id(extractor)
        inflight[key] = inflight.get(key, 0) + 1
        try:
            yield extractor
        finally:
            remaining = inflight.get(key, 1) - 1
            if remaining > 0:
                inflight[key] = remaining
            else:
                inflight.pop(key, None)
                HLSProxyCoreMixin._retire_extractors(self, [extractor])

    @asynccontextmanager
    async def borrow_extractor(self, url, request_headers, host=None, bypass_warp=False):
        """Look up an extractor and retire it when the block exits: it only
        lives for the operation that borrowed it."""
        extractor = await self.get_extractor(
            url, request_headers, host=host, bypass_warp=bypass_warp
        )
        async with HLSProxyCoreMixin.hold_extractor(self, extractor):
            yield extractor

    def _retire_extractors(self, extractors):
        """Detach extractor instances from the cache and drain their sessions:
        close immediately when no request is in flight, with the cleanup loop
        as backstop. New requests build fresh instances."""
        retired = getattr(self, "_retired_extractors", None)
        if retired is None:
            retired = self._retired_extractors = []
        atimes = getattr(self, "_retired_extractor_atimes", None)
        if atimes is None:
            atimes = self._retired_extractor_atimes = {}
        inflight = getattr(self, "_extractor_inflight", None) or {}
        now = time.time()
        for extractor in extractors:
            if extractor is None or id(extractor) in atimes or inflight.get(id(extractor)):
                continue
            for key in [k for k, e in self.extractors.items() if e is extractor]:
                self.extractors.pop(key, None)
                self._extractor_atimes.pop(key, None)
            retired.append(extractor)
            atimes[id(extractor)] = now
            # Close the sessions as soon as they have no in-flight request
            # (retire_session drains first). Step 3b is the hard backstop.
            sessions = {getattr(extractor, "session", None), *getattr(extractor, "_route_sessions", {}).values()}
            for session in sessions:
                # grace=0: the extractor is already out of the cache, so no new
                # borrower can reach this session; in-flight requests are still
                # honoured by the drain check.
                retire_session(self, session, grace=0)

    def _invalidate_extractors(self):
        """Retire active extractors so in-flight requests can finish, while
        new requests get fresh instances with updated routing/proxies."""
        HLSProxyCoreMixin._retire_extractors(self, list(self.extractors.values()))

    @staticmethod
    def _stream_key_for_url(url: str | None) -> str | None:
        if not url:
            return None
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _request_forces_max_res(self, request, extractor_key: str | None, source: str) -> bool:
        """Apply the max-res policy for this request.

        Direct /proxy/mpd and /proxy/hls calls honour the admin MPD/HLS
        switches; requests that belong to an extractor (query namespace or the
        extractor endpoint itself) honour only the per-extractor list.
        """
        query_key = request.query.get("extractor_key", "")
        proxy_endpoint = request.path.startswith("/proxy/")
        key = query_key or ("" if proxy_endpoint else (extractor_key or ""))
        requested = request.query.get("max_res", "").lower() in {"1", "true", "yes", "on"}
        return _config.should_force_max_res(
            key,
            requested,
            source,
            proxy_endpoint=proxy_endpoint,
        )

    def _reuse_stream_key(self, source_key: str, client_id: str = "") -> str:
        """Keep one playback namespace while the player polls the extractor.

        Players reload the extractor URL every few seconds; minting a new
        stream_key per call created (and closed) one upstream session per poll.
        Reuse the key per source+client IP for a quiet period (60s, the same
        idle TTL the session reaper uses), so viewers behind different IPs keep
        separate sessions while devices that share an IP also share the session.
        """
        now = time.time()
        cache = getattr(self, "_stream_key_cache", None)
        if cache is None:
            cache = {}
            self._stream_key_cache = cache
        cache_key = f"{client_id}|{source_key}"
        entry = cache.get(cache_key)
        if entry and now - entry[0] < self.STREAM_KEY_REUSE_SECONDS:
            cache[cache_key] = (now, entry[1])
            return entry[1]
        stream_key = f"{source_key}-{secrets.token_hex(6)}"
        cache[cache_key] = (now, stream_key)
        while len(cache) > 512:
            oldest = min(cache, key=lambda key: cache[key][0])
            cache.pop(oldest, None)
        return stream_key

    def _mark_proxy_dead_if_allowed(self, proxy_url: str | None, dead_duration: int = 300, extractor_key: str | None = None):
        if not proxy_url:
            return
        normalized_key = (extractor_key or "").replace("_direct", "").replace("_noproxy", "")
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
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        retired = list(getattr(self, "_retired_session_tasks", ()))
        for task in retired:
            task.cancel()
        if retired:
            await asyncio.gather(*retired, return_exceptions=True)
        refresh_tasks = list(getattr(self, "_cdn_refresh_tasks", {}).values())
        for task in refresh_tasks:
            task.cancel()
        if refresh_tasks:
            await asyncio.gather(*refresh_tasks, return_exceptions=True)
        if hasattr(self, "_cdn_refresh_tasks"):
            self._cdn_refresh_tasks.clear()

        try:
            if self.session and not self.session.closed:
                await self.session.close()
            if self.flex_session and not self.flex_session.closed:
                await self.flex_session.close()

            if hasattr(self, "_proxy_sessions"):
                for p_sess in list(self._proxy_sessions.values()):
                    if not p_sess.closed:
                        await p_sess.close()
                self._proxy_sessions.clear()
                if hasattr(self, "_proxy_session_atimes"):
                    self._proxy_session_atimes.clear()

            if hasattr(self, "_stream_proxy_sessions"):
                for p_sess in list(self._stream_proxy_sessions.values()):
                    if not p_sess.closed:
                        await p_sess.close()
                self._stream_proxy_sessions.clear()
                if hasattr(self, "_stream_proxy_session_atimes"):
                    self._stream_proxy_session_atimes.clear()

            for extractor in list(self.extractors.values()):
                if hasattr(extractor, "close"):
                    await extractor.close()
            self.extractors.clear()
            if hasattr(self, "_extractor_atimes"):
                self._extractor_atimes.clear()

            retired = list(getattr(self, "_retired_extractors", ()))
            for extractor in retired:
                if hasattr(extractor, "close"):
                    await extractor.close()
            if hasattr(self, "_retired_extractors"):
                self._retired_extractors.clear()
            if hasattr(self, "_retired_extractor_atimes"):
                self._retired_extractor_atimes.clear()

            await close_socks_bridges()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
