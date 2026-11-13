"""Headless Chromium solver for the xstream/dynbush Turnstile gate.

The player intermittently serves a config without the signed stream URL: the URL
is only released by ``POST /turnstile/<id>`` after a Cloudflare Turnstile
challenge inside the player iframe. This module loads the channel page in
headless Chromium, attaches to the player iframe over CDP and returns the JSON
body of that POST. Chrome is the binary bundled with FlareSolverr / installed in
the Docker image; the CDP client uses aiohttp, so there is no new dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

_SOLVE_LOCK = asyncio.Lock()
_DEFAULT_UA_VERSION = "124.0.0.0"


def _build_user_agent(chrome_path: str) -> str:
    """Match the UA version to the bundled binary (headless UA is flagged)."""
    version = _DEFAULT_UA_VERSION
    try:
        for entry in Path(chrome_path).resolve().parent.glob("*.manifest"):
            match = re.match(r"(\d+\.\d+\.\d+\.\d+)", entry.name)
            if match:
                version = match.group(1)
                break
    except OSError:
        pass
    if os.name == "nt":
        platform = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )
_FIND_WIDGET = """
(() => {
  const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
  if (!f) return null;
  const r = f.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
})()
"""


def find_chrome() -> str | None:
    """Locate a Chromium/Chrome binary (bundled FlareSolverr copy first)."""
    candidates: list[str] = []
    env_path = os.getenv("TURNSTILE_CHROME_PATH", "").strip()
    if env_path:
        candidates.append(env_path)
    root = Path(__file__).resolve().parent.parent
    for name in ("chrome.exe", "chrome"):
        candidates.append(str(root / "flaresolverr" / "_internal" / "chrome" / name))
        candidates.append(
            str(root / "flaresolverr-linux" / "_internal" / "chrome" / name)
        )
    # When frozen, ``__file__`` lives in _MEIPASS: also probe next to the exe.
    for base in (
        Path(sys.executable).resolve().parent,
        Path(getattr(sys, "_MEIPASS", "") or "."),
    ):
        for name in ("chrome.exe", "chrome"):
            candidates.append(
                str(base / "flaresolverr" / "_internal" / "chrome" / name)
            )
            candidates.append(
                str(base / "flaresolverr-linux" / "_internal" / "chrome" / name)
            )
    candidates.extend(
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        )
    )
    candidates.extend(
        str(path)
        for name in ("chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge", "microsoft-edge")
        for path in (shutil.which(name),)
        if path
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


class _CDP:
    """Minimal flattened CDP client: request/response ids plus an event queue."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = self._pending.pop(data["id"], None)
                    if fut and not fut.done():
                        fut.set_result(data)
                else:
                    await self.events.put(data)
        except Exception as e:
            logger.debug("Turnstile solver: CDP reader stopped: %r", e)

    async def send(self, method, params=None, session_id=None, timeout=20):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send_json(msg)
        data = await asyncio.wait_for(fut, timeout)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result", {})

    async def close(self):
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass


async def solve_player_stream(
    page_url: str, proxy: str | None = None, timeout: float = 45.0
) -> dict | None:
    """Load ``page_url``, solve the player Turnstile gate, return the POST body."""
    try:
        return await _solve(page_url, proxy, timeout)
    except Exception as e:
        logger.warning("Turnstile solver: failed for %s: %r", page_url, e)
        return None


async def _solve(page_url: str, proxy: str | None, timeout: float) -> dict | None:
    async with _SOLVE_LOCK:
        for attempt in (1, 2):
            solution = await _solve_once(page_url, proxy, min(timeout, 30.0))
            if solution:
                return solution
            logger.info(
                "Turnstile solver: attempt %d/2 did not capture the stream URL", attempt
            )
        return None


async def _solve_once(
    page_url: str, proxy: str | None, timeout: float
) -> dict | None:
    chrome = find_chrome()
    if not chrome:
        logger.warning("Turnstile solver: Chrome non trovato (TURNSTILE_CHROME_PATH)")
        return None

    if proxy and "@" in proxy:
        # Chromium cannot authenticate to a proxy from the command line.
        logger.warning(
            "Turnstile solver: authenticated proxy not supported by Chromium, skipping"
        )
        return None

    profile = tempfile.mkdtemp(prefix="ep-turnstile-")
    ua = _build_user_agent(chrome)
    cmd = [
        chrome,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--window-size=1280,800",
        f"--user-agent={ua}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")
    if proxy:
        # The browser must leave on the same route as the extraction.
        cmd.append(f"--proxy-server={proxy.replace('socks5h://', 'socks5://')}")
    cmd.append("about:blank")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        port = await _wait_devtools_port(profile)
        if not port:
            logger.warning("Turnstile solver: DevToolsActivePort not written")
            return None

        version = await asyncio.to_thread(_read_devtools_version, port)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                version["webSocketDebuggerUrl"], max_msg_size=0
            ) as ws:
                return await _drive(ws, page_url, timeout, ua)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


async def _wait_devtools_port(profile: str) -> int | None:
    port_file = os.path.join(profile, "DevToolsActivePort")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if os.path.exists(port_file):
            try:
                with open(port_file, encoding="utf-8") as f:
                    return int(f.readline().strip())
            except (OSError, ValueError):
                pass
        await asyncio.sleep(0.2)
    return None


def _read_devtools_version(port: int) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version", timeout=10
    ) as response:
        return json.load(response)


async def _drive(ws, page_url: str, timeout: float, ua: str) -> dict | None:
    cdp = _CDP(ws)
    try:
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.send(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
        )
        sid = attached["sessionId"]
        await cdp.send("Page.enable", {}, sid)
        # Same-origin players are not OOPIFs: their POST would surface here.
        await cdp.send("Network.enable", {}, sid)
        try:
            await cdp.send(
                "Emulation.setFocusEmulationEnabled", {"enabled": True}, sid
            )
        except Exception as e:
            logger.debug("Turnstile solver: focus emulation failed: %r", e)
        await cdp.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            sid,
        )
        await cdp.send("Page.navigate", {"url": page_url}, sid)

        deadline = time.monotonic() + timeout
        iframe_sid = None
        clicks = 0
        next_probe = time.monotonic() + 3
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                ev = await asyncio.wait_for(cdp.events.get(), min(1.0, remaining))
            except asyncio.TimeoutError:
                ev = None
            if ev is not None:
                method = ev.get("method")
                params = ev.get("params", {})
                esid = ev.get("sessionId")

                if method == "Target.attachedToTarget":
                    info = params.get("targetInfo", {})
                    new_sid = params.get("sessionId")
                    if not new_sid:
                        continue
                    if info.get("type") in ("iframe", "page"):
                        iframe_sid = new_sid if info.get("type") == "iframe" else iframe_sid
                        for domain in ("Network.enable", "Page.enable", "Runtime.enable"):
                            try:
                                await cdp.send(domain, {}, new_sid)
                            except Exception as e:
                                logger.debug("Turnstile solver: %s failed: %r", domain, e)
                        try:
                            await cdp.send(
                                "Emulation.setFocusEmulationEnabled", {"enabled": True}, new_sid
                            )
                        except Exception as e:
                            logger.debug("Turnstile solver: focus emulation failed: %r", e)
                elif method == "Network.responseReceived":
                    url = params.get("response", {}).get("url", "")
                    if "/turnstile/" not in url or "challenges.cloudflare.com" in url:
                        continue
                    try:
                        body = await cdp.send(
                            "Network.getResponseBody",
                            {"requestId": params["requestId"]},
                            esid,
                        )
                        solution = json.loads(body.get("body") or "")
                    except Exception as e:
                        logger.debug("Turnstile solver: body read failed: %r", e)
                        continue
                    solution["_solver_user_agent"] = ua
                    logger.info(
                        "Turnstile solver: stream URL captured for %s", page_url
                    )
                    return solution

            if iframe_sid and clicks < 4 and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + 3
                try:
                    probe = await cdp.send(
                        "Runtime.evaluate",
                        {"expression": _FIND_WIDGET, "returnByValue": True},
                        iframe_sid,
                    )
                    raw = probe.get("result", {}).get("value")
                except Exception as e:
                    logger.debug("Turnstile solver: widget probe failed: %r", e)
                    raw = None
                if raw:
                    pos = json.loads(raw)
                    clicks += 1
                    for kind in ("mousePressed", "mouseReleased"):
                        try:
                            await cdp.send(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": kind,
                                    "x": pos["x"],
                                    "y": pos["y"],
                                    "button": "left",
                                    "clickCount": 1,
                                },
                                iframe_sid,
                            )
                        except Exception as e:
                            logger.debug("Turnstile solver: click failed: %r", e)
                    logger.info(
                        "Turnstile solver: clicked widget (%d/4)", clicks
                    )

        logger.warning(
            "Turnstile solver: no /turnstile/ response within %.0fs", timeout
        )
        return None
    finally:
        await cdp.close()
