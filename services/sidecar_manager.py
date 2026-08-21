"""Lifecycle management for the embedded Toastflix audio sidecar."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)


class SidecarManager:
    """Start, health-check, and stop the embedded aiohttp sidecar process.

    The sidecar is deliberately bound to loopback.  EasyProxy is the only
    public entry point and forwards requests to the child process through the
    :class:`HLSProxySidecarMixin`.
    """

    IDLE_TIMEOUT_SECONDS = 60 * 60

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
    ):
        project_dir = Path(__file__).resolve().parent.parent
        self.project_dir = project_dir
        self.sidecar_module = "services.toastflix_sidecar.app"
        self.cache_dir = Path(
            cache_dir or project_dir / "recordings" / "sidecar_data"
        ).resolve()

        self.host = "127.0.0.1"
        self.configured_port = 0
        self.startup_timeout = 20.0

        self.process: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self._log_task: asyncio.Task | None = None
        self._child_output: list[str] = []
        self._lifecycle_lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._last_activity: float | None = None
        self._active_requests = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None and self.port is not None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("Sidecar is not running")
        return f"http://{self.host}:{self.port}"

    def target_url(self, path: str, query_string: str = "") -> str:
        """Build an internal URL while keeping the original query encoding."""
        if not path.startswith("/"):
            path = f"/{path}"
        target = f"{self.base_url}{path}"
        return f"{target}?{query_string}" if query_string else target

    async def start(self) -> None:
        """Launch the sidecar and wait until its health endpoint responds."""
        async with self._lifecycle_lock:
            if self.running:
                self._touch_unlocked()
                return
            await self._start_unlocked()

    async def begin_request(self) -> None:
        """Start the sidecar on demand and keep it alive for this request."""
        async with self._lifecycle_lock:
            if not self.running:
                await self._start_unlocked()
            self._active_requests += 1
            self._touch_unlocked()

    async def end_request(self) -> None:
        """Release a request and restart the idle-shutdown countdown."""
        async with self._lifecycle_lock:
            self._active_requests = max(0, self._active_requests - 1)
            if self.running:
                self._touch_unlocked()

    async def _start_unlocked(self) -> None:
        if self.process is not None:
            await self._stop_unlocked()
        sidecar_app = self.project_dir / "services" / "toastflix_sidecar" / "app.py"
        if not sidecar_app.is_file():
            raise FileNotFoundError(f"Embedded Toastflix sidecar not found: {sidecar_app}")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.port = self.configured_port or self._find_free_port()
        self._child_output.clear()
        child_env = os.environ.copy()

        command = [
            sys.executable,
            "-m",
            self.sidecar_module,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--cache-dir",
            str(self.cache_dir),
        ]
        logger.info(
            "Starting Toastflix sidecar on %s:%s (cache: %s)",
            self.host,
            self.port,
            self.cache_dir,
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.project_dir),
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._log_task = asyncio.create_task(self._read_output())
            await self._wait_until_ready()
        except Exception:
            await self._stop_unlocked()
            raise

        logger.info("Toastflix sidecar is ready at %s", self.base_url)
        self._touch_unlocked()

    async def stop(self) -> None:
        """Stop the child process and its output reader without leaving orphans."""
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

        process = self.process
        if process is not None and process.returncode is None:
            logger.info("Stopping Toastflix sidecar (pid=%s)", process.pid)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Toastflix sidecar did not stop cleanly; killing it")
                process.kill()
                await process.wait()

        if self._log_task is not None:
            self._log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_task

        self._log_task = None
        self.process = None
        self.port = None
        self._last_activity = None
        self._active_requests = 0

    def _touch_unlocked(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = asyncio.get_running_loop().call_later(
            self.IDLE_TIMEOUT_SECONDS,
            self._idle_timeout_callback,
        )

    def _idle_timeout_callback(self) -> None:
        self._idle_handle = None
        asyncio.create_task(self._stop_if_idle())

    async def _stop_if_idle(self) -> None:
        async with self._lifecycle_lock:
            if not self.running:
                return
            if self._active_requests:
                self._touch_unlocked()
                return
            last_activity = self._last_activity or 0.0
            idle_for = asyncio.get_running_loop().time() - last_activity
            if idle_for < self.IDLE_TIMEOUT_SECONDS:
                self._idle_handle = asyncio.get_running_loop().call_later(
                    self.IDLE_TIMEOUT_SECONDS - idle_for,
                    self._idle_timeout_callback,
                )
                return
            logger.info(
                "Stopping Toastflix sidecar after %.1f hours without requests",
                self.IDLE_TIMEOUT_SECONDS / 3600,
            )
            await self._stop_unlocked()

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout
        timeout = aiohttp.ClientTimeout(total=2)
        health_url = f"{self.base_url}/health"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while asyncio.get_running_loop().time() < deadline:
                if self.process is None or self.process.returncode is not None:
                    code = None if self.process is None else self.process.returncode
                    details = "\n".join(self._child_output[-20:])
                    message = f"Toastflix sidecar exited before readiness (code={code})"
                    if details:
                        message += f"\nSidecar output:\n{details}"
                    raise RuntimeError(message)
                try:
                    async with session.get(health_url) as response:
                        if response.status == 200:
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError(f"Toastflix sidecar did not become ready within {self.startup_timeout:g}s")

    async def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            async for raw_line in process.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    self._child_output.append(line)
                    if len(self._child_output) > 100:
                        del self._child_output[:-100]
                    logger.info("[sidecar] %s", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Unable to read Toastflix sidecar output: %s", exc)


__all__ = ["SidecarManager"]
