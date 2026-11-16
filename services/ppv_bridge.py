"""Local Node bridge for PPV playback.

The PPV CDN edge (``*.indianservers.st``) 403s every TLS ClientHello outside
its allowlist - aiohttp, curl and curl_cffi included - and only answers Node
with ``CDN_CIPHERS`` (see ``scripts/ppv_runner.mjs``). Playlists and segments
are therefore fetched by ``scripts/ppv_bridge.mjs``, an HTTP server bound to
127.0.0.1 that rewrites playlist URLs back through itself.
"""

from __future__ import annotations

import atexit
import asyncio
import logging
import os
import sys
import time
from urllib.parse import quote

import aiohttp

from extractors.base import ExtractorError
from extractors.node_runner import node_bin

logger = logging.getLogger(__name__)

_BRIDGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "ppv_bridge.mjs",
)
_READY_TIMEOUT = 10.0
_LOCK = asyncio.Lock()
_process: asyncio.subprocess.Process | None = None
_base_url: str | None = None


def _bridge_script() -> str:
    if os.path.exists(_BRIDGE):
        return _BRIDGE
    fallback = os.path.join(getattr(sys, "_MEIPASS", ""), "scripts", "ppv_bridge.mjs")
    if os.path.exists(fallback):
        return fallback
    raise ExtractorError(f"PPV: bridge script not found at {_BRIDGE}")


async def _healthy(base: str) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            async with session.get(f"{base}/health") as res:
                return res.status == 200 and (await res.text()).strip() == "ok"
    except Exception:
        return False


async def _kill(proc) -> None:
    if not proc or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2)
    except BaseException:
        try:
            proc.kill()
        except BaseException:
            pass


async def _stop_process() -> None:
    global _process
    proc = _process
    _process = None
    await _kill(proc)


async def ensure_bridge() -> str:
    """Start (or reuse) the bridge and return its base URL."""
    global _process, _base_url
    if os.getenv("PPV_BRIDGE_DISABLE") == "1":
        raise ExtractorError("PPV: bridge disabled by PPV_BRIDGE_DISABLE")

    async with _LOCK:
        if _base_url and await _healthy(_base_url):
            return _base_url
        await _stop_process()
        _base_url = None

        node = node_bin()
        if not node:
            raise ExtractorError("PPV: Node.js is required for the bridge")
        script = _bridge_script()
        try:
            proc = await asyncio.create_subprocess_exec(
                node, script,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                env=dict(os.environ),
            )
        except OSError as exc:
            raise ExtractorError(f"PPV: failed to start bridge: {exc}") from exc

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=_READY_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await _kill(proc)
            raise ExtractorError("PPV: bridge did not start in time") from exc

        text = (line or b"").decode("utf-8", errors="replace").strip()
        if not text.startswith("READY "):
            await _kill(proc)
            raise ExtractorError(f"PPV: bridge failed to start ({text[-120:] or 'no output'})")

        _process = proc
        _base_url = f"http://127.0.0.1:{text.split()[1]}"
        logger.info("PPV: bridge ready at %s", _base_url)
        return _base_url


def bridge_manifest_url(base: str, m3u8: str, referer: str, proxy: str | None = None) -> str:
    url = f"{base}/m?u={quote(m3u8, safe='')}&r={quote(referer or '', safe='')}"
    if proxy:
        # The bridge fetches upstream through the same proxy as the runner, so
        # the minted token and the egress IP match.
        url += f"&x={quote(str(proxy), safe='')}"
    return url


async def shutdown_bridge() -> None:
    global _base_url
    _base_url = None
    await _stop_process()


def _terminate_at_exit() -> None:
    proc = _process
    if not proc or proc.returncode is not None:
        return
    try:
        proc.terminate()
        deadline = time.monotonic() + 2
        while proc.returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.returncode is None:
            proc.kill()
    except BaseException:
        pass


atexit.register(_terminate_at_exit)
