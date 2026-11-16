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
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

import config_store

logger = logging.getLogger(__name__)

_SOLVE_LOCK = asyncio.Lock()
# One Chromium at a time across every CDP resolver: they share the profile.
BROWSER_LOCK = _SOLVE_LOCK
_DEFAULT_UA_VERSION = "124.0.0.0"
_XVFB_LOCK = asyncio.Lock()
_XVFB_PROCESS: subprocess.Popen | None = None
_XVFB_DISPLAY: str | None = None
# Chromium needs headroom: on a 1 GB box launching it can trigger the OOM killer.
_MIN_AVAILABLE_MB = float(os.getenv("TURNSTILE_MIN_AVAILABLE_MB", "400"))


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


# Player already live with no gate: no POST /turnstile/ ever arrives, the URL is
# in the DOM, in the player object or already in the resource timing.
_READ_STREAM_URL = """
(() => {
  try {
    const v = document.querySelector('video');
    const cands = [];
    if (v) { cands.push(v.currentSrc, v.src); }
    const pick = (obj) => {
      if (!obj) return;
      const opts = obj.options || obj.config || {};
      cands.push(opts.source, opts.file, opts.src, obj.src);
    };
    pick(window.player); pick(window.clapprPlayer); pick(window.jwplayer); pick(window.__player);
    const entries = performance.getEntriesByType('resource') || [];
    for (const e of entries) if (/\\.m3u8(\\?|$)/i.test(e.name)) cands.push(e.name);
    const hit = cands.find(u => typeof u === 'string' && /^https?:/.test(u) && /\\.m3u8(\\?|$)/i.test(u));
    return hit || null;
  } catch (e) { return null; }
})()
"""


def _is_stale_session(exc: Exception) -> bool:
    """True when a CDP call targets a detached iframe session."""
    return "session with given id not found" in str(exc).lower()


def _host_of(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _is_player_target(info: dict, host: str) -> bool:
    """True when a child target is the player iframe of ``host``.

    Loading the player URL directly makes Chrome send ``Sec-Fetch-Site: none``
    and the player bounces to xstream.to: the parent page must open it as a
    cross-site OOPIF instead, and that is the target to drive.
    """
    if not host or info.get("type") != "iframe":
        return False
    target_host = _host_of(info.get("url") or "")
    return bool(target_host) and target_host == host


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
    if os.name == "nt":
        # Per-user install is the default for accounts without admin rights.
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for rel in (
                r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe",
                r"Microsoft\Edge\Application\msedge.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ):
                candidates.append(os.path.join(local_app, rel))
    candidates.extend(
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files\Chromium\Application\chrome.exe",
        )
    )
    if os.name != "nt":
        # PATH can be minimal in containers: probe the usual install paths too.
        candidates.extend(
            (
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/snap/bin/chromium",
                "/opt/google/chrome/chrome",
            )
        )
    candidates.extend(
        str(path)
        for name in ("chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge", "microsoft-edge")
        for path in (shutil.which(name),)
        if path
    )
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        # A FlareSolverr bundle built on Windows ships chrome.exe; on POSIX it
        # must never shadow the real browser.
        if os.name != "nt" and candidate.lower().endswith(".exe"):
            continue
        if os.name != "nt" and not os.access(candidate, os.X_OK):
            continue
        return candidate
    logger.warning("Chrome not found. Tried: %s", ", ".join(candidates[:10]))
    return None


def _clean_profile(path: str) -> None:
    """Drop the lock/port files a dead Chromium leaves behind."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
        try:
            os.unlink(os.path.join(path, name))
        except OSError:
            pass


def _kill_profile_chrome(profile: str) -> None:
    """Kill a leftover Chromium still holding ``profile`` (it blocks the new one)."""
    if os.name != "nt":
        try:
            killed = subprocess.run(
                ["pkill", "-f", f"user-data-dir={profile}"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
        except Exception as e:
            logger.debug("Turnstile solver: pkill failed: %r", e)
            return
        if killed:
            # Give it a moment to release the Singleton files.
            time.sleep(0.5)
        return
    try:
        import psutil
    except Exception as e:
        logger.debug("Turnstile solver: psutil unavailable: %r", e)
        return
    needle = f"user-data-dir={profile}".lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        name = (proc.info.get("name") or "").lower()
        if name not in ("chrome.exe", "msedge.exe"):
            continue
        if not any(needle in str(arg).lower() for arg in proc.info.get("cmdline") or []):
            continue
        try:
            proc.kill()
            logger.info("Turnstile solver: killed leftover %s (pid %s)", name, proc.pid)
        except Exception as e:
            logger.debug("Turnstile solver: kill %s failed: %r", name, e)


def _profile_dir() -> str:
    """Persistent Chromium profile: cookies/clearance survive across solves."""
    override = os.getenv("TURNSTILE_PROFILE_DIR", "").strip()
    path = override or os.path.join(config_store.CONFIG_DIR, "turnstile-profile")
    os.makedirs(path, exist_ok=True)
    _clean_profile(path)
    return path


def _chrome_log_tail(path: str, lines: int = 10) -> str:
    """Last lines of the Chrome stderr log, for DevToolsActivePort failures."""
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-8192, os.SEEK_END)
            except OSError:
                fh.seek(0)
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = " | ".join(raw.splitlines()[-lines:]).strip()
    return tail[-800:]


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


def _display_alive(display: str) -> bool:
    """True when the X socket of ``display`` accepts a connection.

    A socket file outlives a crashed Xvfb: connecting is the only check that
    tells a live server from a stale one.
    """
    if not hasattr(socket, "AF_UNIX"):
        return True
    number = display.lstrip(":").split(".")[0]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(f"/tmp/.X11-unix/X{number}")
        return True
    except OSError:
        return False
    finally:
        sock.close()


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
            if _XVFB_PROCESS is not None and _XVFB_PROCESS.poll() is None:
                return _XVFB_DISPLAY
            # Xvfb died: DISPLAY would point at a dead server and Chrome would
            # exit before writing DevToolsActivePort.
            logger.warning(
                "Turnstile solver: Xvfb on %s is gone, restarting", _XVFB_DISPLAY
            )
            _XVFB_PROCESS = None
            _XVFB_DISPLAY = None
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            return None
        for number in range(99, 105):
            display = f":{number}"
            if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                if _display_alive(display):
                    _XVFB_DISPLAY = display
                    return display
                logger.debug(
                    "Turnstile solver: stale X socket on %s, skipping", display
                )
                continue
            proc = subprocess.Popen(
                [xvfb, display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(0.5)
            if proc.poll() is None and _display_alive(display):
                _XVFB_PROCESS = proc
                _XVFB_DISPLAY = display
                atexit.register(_stop_xvfb)
                logger.info("Turnstile solver: started Xvfb on %s", display)
                return display
            try:
                proc.kill()
            except OSError:
                pass
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
    page_url: str,
    proxy: str | None = None,
    timeout: float = 45.0,
    parent_url: str | None = None,
) -> dict | None:
    """Load the player page, solve its Turnstile gate, return the POST body.

    ``parent_url`` is the page that embeds ``page_url`` as an iframe: passing it
    lets the iframe load with the Referer/Sec-Fetch-Site it expects.
    """
    try:
        return await _solve(page_url, proxy, timeout, mode="stream", parent_url=parent_url)
    except Exception as e:
        logger.warning("Turnstile solver: failed for %s: %r", page_url, e)
        return None


async def solve_premium_ticket(
    page_url: str,
    proxy: str | None = None,
    timeout: float = 45.0,
    parent_url: str | None = None,
) -> dict | None:
    """Solve the premium player Turnstile and return its playlist ticket.

    After the challenge the player POSTs to ``/premiumtv/daddy.php?id=<id>`` and
    gets ``{"ticket": "<epoch>.<ip>.<hmac>"}``; the edge token endpoint only
    answers for the address that holds the ticket.
    """
    try:
        return await _solve(
            page_url, proxy, timeout, mode="premium", parent_url=parent_url
        )
    except Exception as e:
        logger.warning("Turnstile solver: premium failed for %s: %r", page_url, e)
        return None


async def _solve(
    page_url: str,
    proxy: str | None,
    timeout: float,
    mode: str = "stream",
    parent_url: str | None = None,
) -> dict | None:
    async with _SOLVE_LOCK:
        for attempt in (1, 2):
            solution = await _solve_once(
                page_url, proxy, min(timeout, 30.0), mode, parent_url
            )
            if solution:
                return solution
            logger.info(
                "Turnstile solver: attempt %d/2 did not capture %s",
                attempt,
                "the premium ticket" if mode == "premium" else "the stream URL",
            )
        return None


async def launch_browser(
    label: str = "Turnstile solver", proxy: str | None = None
) -> tuple[subprocess.Popen, dict, str] | None:
    """Start Chromium on the persistent profile: ``(process, devtools, UA)``.

    The other CDP resolvers reuse this so the profile, the RAM guard and the
    headless fallback behave the same everywhere.
    """
    chrome = find_chrome()
    if not chrome:
        logger.warning("%s: Chrome non trovato (TURNSTILE_CHROME_PATH)", label)
        return None

    try:
        import psutil
        available_mb = psutil.virtual_memory().available / (1024 ** 2)
        if available_mb < _MIN_AVAILABLE_MB:
            logger.warning(
                "%s: only %.0f MB RAM available (need %.0f MB), skipping browser solve",
                label,
                available_mb,
                _MIN_AVAILABLE_MB,
            )
            return None
    except Exception as exc:
        logger.debug("%s: RAM check failed: %r", label, exc)

    if proxy and "@" in proxy:
        # Chromium cannot authenticate to a proxy from the command line.
        logger.warning(
            "%s: authenticated proxy not supported by Chromium, skipping", label
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
    # A previous Chrome can stay alive and keep the profile locked, which makes
    # the new one exit before writing DevToolsActivePort.
    _kill_profile_chrome(profile)
    _clean_profile(profile)
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
    logger.debug("%s: launching (headless=%s): %s", label, headless, " ".join(cmd[:6]))

    # Chrome's stderr explains an early exit (profile lock, dead DISPLAY).
    log_path = os.path.join(config_store.CONFIG_DIR, "chrome-solver.log")
    log = subprocess.DEVNULL
    try:
        os.makedirs(config_store.CONFIG_DIR, exist_ok=True)
        log = open(log_path, "ab")
    except OSError as e:
        logger.debug("%s: cannot open %s: %r", label, log_path, e)

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if log is not subprocess.DEVNULL:
        log.close()
    try:
        port = await _wait_devtools_port(profile)
        if not port:
            tail = _chrome_log_tail(log_path)
            logger.warning(
                "%s: DevToolsActivePort not written%s",
                label,
                f" (chrome log: {tail})" if tail else "",
            )
            raise RuntimeError("DevToolsActivePort not written")

        version = await asyncio.to_thread(_read_devtools_version, port)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return None
    logger.info("%s: browser %s, UA %s", label, version.get("Browser"), ua)
    return proc, version, ua


async def _solve_once(
    page_url: str,
    proxy: str | None,
    timeout: float,
    mode: str = "stream",
    parent_url: str | None = None,
) -> dict | None:
    launched = await launch_browser(proxy=proxy)
    if not launched:
        return None
    proc, version, ua = launched
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                version["webSocketDebuggerUrl"], max_msg_size=0
            ) as ws:
                return await _drive(ws, page_url, timeout, ua, mode, parent_url)
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
    ws,
    page_url: str,
    timeout: float,
    ua: str,
    mode: str = "stream",
    parent_url: str | None = None,
) -> dict | None:
    """Drive one solve attempt.

    With ``parent_url`` the browser opens the page embedding the player and lets
    the iframe load as a cross-site OOPIF (correct Referer and Sec-Fetch-Site);
    navigating straight to ``page_url`` makes the player bounce to xstream.to.
    """
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
        iframe_host = _host_of(page_url) if parent_url else ""
        child_sessions: dict[str, str] = {}

        async def _enable_child(child_sid: str) -> None:
            # Runtime.enable is a known CDP detection vector; evaluate works
            # without it.
            for domain in ("Network.enable", "Page.enable", "Log.enable"):
                try:
                    await cdp.send(domain, {}, child_sid)
                except Exception as e:
                    logger.debug("Turnstile solver: %s failed: %r", domain, e)
            try:
                await cdp.send(
                    "Emulation.setFocusEmulationEnabled", {"enabled": True}, child_sid
                )
            except Exception as e:
                logger.debug("Turnstile solver: focus emulation failed: %r", e)

        if parent_url:
            # The OOPIF is reported before it navigates: discovery lets us
            # attach to the player iframe itself.
            try:
                await cdp.send("Target.setDiscoverTargets", {"discover": True})
            except Exception as e:
                logger.debug("Turnstile solver: setDiscoverTargets failed: %r", e)
        await cdp.send("Page.navigate", {"url": parent_url or page_url}, sid)

        deadline = time.monotonic() + timeout
        # No player iframe in the parent page (~10s): play it as a top document.
        direct_nav_at = time.monotonic() + 10
        direct_nav_done = False
        player_seen = False
        iframe_sid = None
        clicks = 0
        next_probe = time.monotonic() + 3
        # A live player may never POST /turnstile/: read its URL instead.
        next_fallback = time.monotonic() + 9
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
                elif method in ("Target.attachedToTarget", "Target.targetCreated"):
                    info = params.get("targetInfo", {})
                    tid = info.get("targetId") or ""
                    new_sid = params.get("sessionId")
                    if info.get("type") not in ("iframe", "page"):
                        continue
                    if method == "Target.targetCreated":
                        if not tid or tid in child_sessions:
                            continue
                        if not _is_player_target(info, iframe_host):
                            continue
                        try:
                            attached_child = await cdp.send(
                                "Target.attachToTarget",
                                {"targetId": tid, "flatten": True},
                            )
                        except Exception as e:
                            logger.debug(
                                "Turnstile solver: player target attach failed: %r", e
                            )
                            continue
                        new_sid = attached_child.get("sessionId")
                    if not new_sid:
                        continue
                    if tid:
                        child_sessions[tid] = new_sid
                    # The POST/JSON the gate releases arrives on this session.
                    await _enable_child(new_sid)
                    if info.get("type") == "iframe" and (
                        not parent_url
                        or iframe_sid is None
                        or _is_player_target(info, iframe_host)
                    ):
                        iframe_sid = new_sid
                    if _is_player_target(info, iframe_host):
                        player_seen = True
                elif method == "Target.targetInfoChanged":
                    # The OOPIF often attaches before its URL is known.
                    info = params.get("targetInfo", {})
                    new_sid = child_sessions.get(info.get("targetId") or "")
                    if new_sid and _is_player_target(info, iframe_host):
                        iframe_sid = new_sid
                        player_seen = True
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

            if parent_url and not direct_nav_done and not player_seen:
                if time.monotonic() >= direct_nav_at:
                    direct_nav_done = True
                    logger.info(
                        "Turnstile solver: no player iframe in %s, navigating directly",
                        parent_url,
                    )
                    try:
                        await cdp.send("Page.navigate", {"url": page_url}, sid)
                    except Exception as e:
                        logger.debug(
                            "Turnstile solver: direct navigation failed: %r", e
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
                        if _is_stale_session(e):
                            iframe_sid = None
                        logger.debug("Turnstile solver: UA probe failed: %r", e)
                # Non-gated variant: the config already carries the signed URL.
                # The premium player has no _econfig: its URL comes from the token.
                encoded = None
                if iframe_sid:
                    try:
                        plain = await cdp.send(
                            "Runtime.evaluate",
                            {"expression": _READ_ECONFIG, "returnByValue": True},
                            iframe_sid,
                        )
                        encoded = plain.get("result", {}).get("value")
                    except Exception as e:
                        if _is_stale_session(e):
                            iframe_sid = None
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
                if iframe_sid and clicks < 4:
                    try:
                        probe = await cdp.send(
                            "Runtime.evaluate",
                            {"expression": _FIND_WIDGET, "returnByValue": True},
                            iframe_sid,
                        )
                        raw = probe.get("result", {}).get("value")
                    except Exception as e:
                        if _is_stale_session(e):
                            iframe_sid = None
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

            if mode == "stream" and time.monotonic() >= next_fallback:
                next_fallback = time.monotonic() + 2
                # Ungated player: it starts on its own and the signed URL is
                # already there, so no POST /turnstile/ will ever arrive. Probe
                # the player session(s) first, then the top document.
                candidates = [iframe_sid] + list(child_sessions.values()) + [sid]
                for probe_sid in dict.fromkeys(s for s in candidates if s):
                    try:
                        res = await cdp.send(
                            "Runtime.evaluate",
                            {"expression": _READ_STREAM_URL, "returnByValue": True},
                            probe_sid,
                        )
                    except Exception as e:
                        if _is_stale_session(e) and probe_sid == iframe_sid:
                            iframe_sid = None
                        logger.debug(
                            "Turnstile solver: stream URL probe failed: %r", e
                        )
                        continue
                    hit = (res.get("result", {}) or {}).get("value")
                    if isinstance(hit, str) and hit:
                        logger.info(
                            "Turnstile solver: stream URL read from player (fallback)"
                        )
                        return {
                            "success": True,
                            "stream_url": hit,
                            "stream_url_nop2p": None,
                            "_solver_user_agent": actual_ua or ua,
                        }

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
