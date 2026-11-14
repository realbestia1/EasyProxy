"""Chromium solver for the xstream/dynbush Turnstile gate.

The player intermittently serves a config without the signed stream URL: the URL
is only released by ``POST /turnstile/<id>`` after a Cloudflare Turnstile
challenge inside the player iframe. This module loads the channel page in
Chromium, attaches to the player iframe over CDP and returns the JSON body of
that POST. Chromium runs headed (off-screen window, private Xvfb on Linux) with
a persistent profile because Turnstile penalises headless browsers. Chrome is
the binary bundled with FlareSolverr / installed in the Docker image; the CDP
client uses aiohttp, so there is no new dependency.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import email.utils
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp

import config_store

logger = logging.getLogger(__name__)

_SOLVE_LOCK = asyncio.Lock()
_DEFAULT_UA_VERSION = "124.0.0.0"
_XVFB_LOCK = asyncio.Lock()
_XVFB_PROCESS: subprocess.Popen | None = None
_XVFB_DISPLAY: str | None = None


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
    if os.name != "nt":
        try:
            proc = subprocess.run(
                [chrome_path, "--version"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"(\d+\.\d+\.\d+\.\d+)", proc.stdout or "")
            if match:
                version = match.group(1)
        except Exception:
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
# Page state snapshotted when the gate never calls back (fingerprint debugging).
_DIAG = """
JSON.stringify({
  t: document.title,
  u: location.href,
  n: document.documentElement ? document.documentElement.outerHTML.length : 0,
  ifr: document.querySelectorAll('iframe').length,
  ts: typeof window.turnstile,
  box: !!document.querySelector('#turnstile-container'),
  tok: (document.querySelector("input[name='cf-turnstile-response']") || {}).value ? 'set' : null,
  ecfg: typeof window._econfig,
  len: window._econfig ? String(window._econfig).length : 0,
  body: document.body ? String(document.body.innerText || '').slice(0, 200) : null,
  ua: navigator.userAgent
})
"""
# The "trusted browser" variant of the player ships the signed URL in the page
# config instead of gating it: read it before waiting for a POST.
_READ_ECONFIG = """
(() => {
  try {
    const val = window._econfig;
    if (!val) return null;
    const b64 = s => { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; return atob(s); };
    const d = b64(val);
    const n = Math.ceil(d.length / 4);
    const parts = [];
    let o = 0;
    for (let i = 0; i < 4; i++) { const p = d.slice(o, o + n); o += n; parts.push(p.slice(0, 3) + p.slice(4)); }
    const order = [2, 0, 3, 1];
    const out = ['', '', '', ''];
    for (let i = 0; i < 4; i++) out[order[i]] = b64(parts[i]);
    return out.join('');
  } catch (e) { return null; }
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


def _profile_dir() -> str:
    """Persistent Chromium profile: cookies/clearance survive across solves."""
    override = os.getenv("TURNSTILE_PROFILE_DIR", "").strip()
    path = override or os.path.join(config_store.CONFIG_DIR, "turnstile-profile")
    os.makedirs(path, exist_ok=True)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
        try:
            os.unlink(os.path.join(path, name))
        except OSError:
            pass
    return path


def _stop_xvfb() -> None:
    global _XVFB_PROCESS
    if _XVFB_PROCESS is not None and _XVFB_PROCESS.poll() is None:
        try:
            _XVFB_PROCESS.terminate()
            _XVFB_PROCESS.wait(timeout=5)
        except Exception:
            try:
                _XVFB_PROCESS.kill()
            except Exception:
                pass
    _XVFB_PROCESS = None


async def _ensure_display() -> str | None:
    """Display for headed Chromium: existing X11, or a private Xvfb."""
    global _XVFB_PROCESS, _XVFB_DISPLAY
    if os.name == "nt":
        return ""
    if sys.platform == "darwin":
        return None
    existing = os.getenv("DISPLAY")
    if existing:
        return existing
    async with _XVFB_LOCK:
        if _XVFB_DISPLAY:
            return _XVFB_DISPLAY
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            return None
        for number in range(99, 105):
            display = f":{number}"
            if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                _XVFB_DISPLAY = display
                return display
            proc = subprocess.Popen(
                [xvfb, display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)
            if proc.poll() is None:
                _XVFB_PROCESS = proc
                _XVFB_DISPLAY = display
                atexit.register(_stop_xvfb)
                logger.info("Turnstile solver: started Xvfb on %s", display)
                return display
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
        return await _solve(page_url, proxy, timeout, mode="stream")
    except Exception as e:
        logger.warning("Turnstile solver: failed for %s: %r", page_url, e)
        return None


async def solve_premium_ticket(
    page_url: str, proxy: str | None = None, timeout: float = 45.0
) -> dict | None:
    """Solve the premium player Turnstile and return its playlist ticket.

    After the challenge the player POSTs to ``/premiumtv/daddy.php?id=<id>`` and
    gets ``{"ticket": "<epoch>.<ip>.<hmac>"}``; the edge token endpoint only
    answers for the address that holds the ticket.
    """
    try:
        return await _solve(page_url, proxy, timeout, mode="premium")
    except Exception as e:
        logger.warning("Turnstile solver: premium failed for %s: %r", page_url, e)
        return None


async def _solve(
    page_url: str, proxy: str | None, timeout: float, mode: str = "stream"
) -> dict | None:
    async with _SOLVE_LOCK:
        for attempt in (1, 2):
            solution = await _solve_once(page_url, proxy, min(timeout, 30.0), mode)
            if solution:
                return solution
            logger.info(
                "Turnstile solver: attempt %d/2 did not capture %s",
                attempt,
                "the premium ticket" if mode == "premium" else "the stream URL",
            )
        return None


async def _solve_once(
    page_url: str, proxy: str | None, timeout: float, mode: str = "stream"
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

    headless = os.getenv("TURNSTILE_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}
    env = dict(os.environ)
    if not headless:
        display = await _ensure_display()
        if display is None:
            headless = True
        elif display:
            env["DISPLAY"] = display

    profile = _profile_dir()
    ua = _build_user_agent(chrome)
    cmd = [chrome]
    if headless:
        # Headless advertises HeadlessChrome: keep the client hint set coherent.
        cmd.extend(["--headless=new", f"--user-agent={ua}"])
    else:
        # A real window passes Turnstile far more often than headless. Hide it
        # off-screen on Windows only: Xvfb is already invisible, and an
        # off-screen window on Linux gets occluded/throttled instead.
        position = "-32000,-32000" if os.name == "nt" else "0,0"
        cmd.extend([f"--window-position={position}", "--window-size=1280,800"])
    cmd.extend([
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # WebGL must work: Cloudflare fingerprints its absence.
        "--enable-unsafe-swiftshader",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-ipc-flooding-protection",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
    ])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")
    if proxy:
        # The browser must leave on the same route as the extraction.
        cmd.append(f"--proxy-server={proxy.replace('socks5h://', 'socks5://')}")
    cmd.append("about:blank")
    logger.debug("Turnstile solver: launching (headless=%s): %s", headless, " ".join(cmd[:6]))

    proc = subprocess.Popen(
        cmd,
        env=env,
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
        logger.info(
            "Turnstile solver: browser %s, UA %s",
            version.get("Browser"),
            ua,
        )

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                version["webSocketDebuggerUrl"], max_msg_size=0
            ) as ws:
                return await _drive(ws, page_url, timeout, ua, mode)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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


async def _response_json(cdp, request_id, session_id) -> dict | None:
    """JSON body of a response; it can be briefly unavailable right after the event."""
    for attempt in range(3):
        try:
            body = await cdp.send(
                "Network.getResponseBody", {"requestId": request_id}, session_id
            )
            data = json.loads(body.get("body") or "")
        except Exception as e:
            logger.debug("Turnstile solver: body read failed: %r", e)
            data = None
        if isinstance(data, dict):
            return data
        if attempt < 2:
            await asyncio.sleep(0.2)
    return None


async def _drive(
    ws, page_url: str, timeout: float, ua: str, mode: str = "stream"
) -> dict | None:
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
            await cdp.send("Log.enable", {}, sid)
        except Exception as e:
            logger.debug("Turnstile solver: Log.enable failed: %r", e)
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
        actual_ua = None
        server_date = None
        challenge_urls: dict[str, str] = {}
        captured: dict = {}
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

                if method == "Network.requestWillBeSent":
                    url = params.get("request", {}).get("url", "")
                    if "challeng" in url or "turnstile" in url:
                        challenge_urls[params.get("requestId")] = url
                elif method == "Target.attachedToTarget":
                    info = params.get("targetInfo", {})
                    new_sid = params.get("sessionId")
                    if not new_sid:
                        continue
                    if info.get("type") in ("iframe", "page"):
                        iframe_sid = new_sid if info.get("type") == "iframe" else iframe_sid
                        # Runtime.enable is a known CDP detection vector; evaluate
                        # works without it.
                        for domain in ("Network.enable", "Page.enable", "Log.enable"):
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
                    if esid == sid and server_date is None:
                        headers = params.get("response", {}).get("headers", {}) or {}
                        for key, value in headers.items():
                            if key.lower() == "date":
                                server_date = str(value)
                                break
                    if mode == "premium":
                        # Ticket POST first, then the token GET that consumes it.
                        # Only the JSON APIs: a document body is not JSON.
                        wanted = None
                        if params.get("type") in ("XHR", "Fetch"):
                            if "premiumtv/daddy.php" in url:
                                wanted = "ticket"
                            elif "/api/playlist-token" in url:
                                wanted = "token"
                        if wanted:
                            data = await _response_json(cdp, params.get("requestId"), esid)
                            if wanted == "ticket" and (data or {}).get("ticket"):
                                captured["ticket"] = str(data["ticket"])
                                logger.info("Turnstile solver: premium ticket captured")
                            elif wanted == "token" and (data or {}).get("token"):
                                captured["token"] = str(data["token"])
                                captured["k"] = data.get("k")
                                captured["ttl"] = data.get("ttl")
                                logger.info(
                                    "Turnstile solver: premium playlist token captured"
                                )
                            if captured.get("ticket") and captured.get("token"):
                                return {
                                    "success": True,
                                    "ticket": captured.get("ticket"),
                                    "token": captured.get("token"),
                                    "k": captured.get("k"),
                                    "ttl": captured.get("ttl"),
                                    "_solver_user_agent": actual_ua or ua,
                                }
                        continue
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
                    solution["_solver_user_agent"] = actual_ua or ua
                    logger.info(
                        "Turnstile solver: stream URL captured for %s", page_url
                    )
                    return solution
                elif method == "Network.loadingFailed":
                    failed_url = challenge_urls.get(params.get("requestId"))
                    if failed_url:
                        logger.debug(
                            "Turnstile solver: challenge request failed: %s -> %s",
                            failed_url,
                            params.get("errorText"),
                        )
                elif method == "Log.entryAdded":
                    entry = params.get("entry", {})
                    logger.debug(
                        "Turnstile solver: console %s: %s",
                        entry.get("level"),
                        entry.get("text"),
                    )

            if iframe_sid and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + 3
                # The UA the browser actually reports beats the declared one.
                if actual_ua is None:
                    try:
                        res_ua = await cdp.send(
                            "Runtime.evaluate",
                            {"expression": "navigator.userAgent", "returnByValue": True},
                            iframe_sid,
                        )
                        actual_ua = (res_ua.get("result", {}) or {}).get("value") or None
                    except Exception as e:
                        logger.debug("Turnstile solver: UA probe failed: %r", e)
                # Non-gated variant: the config already carries the signed URL.
                # The premium player has no _econfig: its URL comes from the token.
                try:
                    plain = await cdp.send(
                        "Runtime.evaluate",
                        {"expression": _READ_ECONFIG, "returnByValue": True},
                        iframe_sid,
                    )
                    encoded = plain.get("result", {}).get("value")
                except Exception as e:
                    logger.debug("Turnstile solver: _econfig read failed: %r", e)
                    encoded = None
                if mode == "stream" and isinstance(encoded, str) and encoded:
                    try:
                        config = json.loads(
                            base64.b64decode(
                                encoded + "=" * (-len(encoded) % 4)
                            ).decode("utf-8")
                        )
                    except Exception as e:
                        logger.debug("Turnstile solver: _econfig decode failed: %r", e)
                        config = None
                    if isinstance(config, dict) and (
                        config.get("stream_url") or config.get("stream_url_nop2p")
                    ):
                        logger.info(
                            "Turnstile solver: stream URL read from page config"
                        )
                        solution = {
                            "success": True,
                            "stream_url": config.get("stream_url"),
                            "stream_url_nop2p": config.get("stream_url_nop2p"),
                            "_solver_user_agent": actual_ua or ua,
                        }
                        return solution
                if clicks < 4:
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

        if mode == "premium" and captured.get("ticket"):
            # The ticket alone is enough for the caller's own token GET.
            logger.warning(
                "Turnstile solver: premium ticket captured, no playlist token within %.0fs",
                timeout,
            )
            return {
                "success": True,
                "ticket": captured.get("ticket"),
                "token": None,
                "k": None,
                "ttl": None,
                "_solver_user_agent": actual_ua or ua,
            }
        try:
            diag = await cdp.send(
                "Runtime.evaluate",
                {"expression": _DIAG, "returnByValue": True},
                iframe_sid or sid,
            )
            logger.warning(
                "Turnstile solver: page state on timeout: %s",
                (diag.get("result", {}) or {}).get("value"),
            )
        except Exception as e:
            logger.debug("Turnstile solver: timeout diagnostics failed: %r", e)
        if server_date:
            # Positive = the browser clock runs ahead of the origin.
            try:
                probe = await cdp.send(
                    "Runtime.evaluate",
                    {"expression": "Date.now()", "returnByValue": True},
                    iframe_sid or sid,
                )
                now_ms = (probe.get("result", {}) or {}).get("value")
                server_ms = (
                    email.utils.parsedate_to_datetime(server_date).timestamp() * 1000
                )
                skew = (now_ms - server_ms) / 1000
                if abs(skew) > 120:
                    logger.warning("Turnstile solver: clock skew %.0fs (fix NTP)", skew)
                else:
                    logger.debug("Turnstile solver: clock skew %.0fs", skew)
            except Exception as e:
                logger.debug("Turnstile solver: clock skew check failed: %r", e)
        logger.warning(
            "Turnstile solver: no %s within %.0fs",
            "premium response" if mode == "premium" else "/turnstile/ response",
            timeout,
        )
        return None
    finally:
        await cdp.close()
