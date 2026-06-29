import asyncio
import logging
import os
import uuid
import json
import time
import subprocess
import tempfile
from urllib.parse import urlparse
from aiohttp import web, ClientSession, ClientTimeout

from config import FLARESOLVERR_TIMEOUT, FLARESOLVERR_URL

logger = logging.getLogger(__name__)

# Global state
_mock_server_running = False
_runner = None
_sessions_proxies = {}
_domain_locks = {}

# --- Browser pool: refcount + auto-close when idle ---
_browser_pool = {}  # pool_key -> {pw, context, refcount, lock, _close_task}


def _get_domain_lock(domain: str) -> asyncio.Lock:
    if domain not in _domain_locks:
        _domain_locks[domain] = asyncio.Lock()
    return _domain_locks[domain]


async def _get_or_create_browser(pool_key: str):
    """Get or create a pooled browser. Returns (context, page, pw, is_new, is_additional_tab).
    Uses launch_persistent_context() with ONE main page, and opens extra pages (tabs) for concurrent requests."""
    entry = _browser_pool.get(pool_key)
    if entry and "context" in entry:
        entry["refcount"] = entry.get("refcount", 0) + 1
        ct = entry.get("_close_task")
        if ct and not ct.done():
            ct.cancel()
        
        # If refcount is 1 (after cancellation of close task), the main page is idle, so reuse it.
        # Otherwise, open a new page/tab for concurrent requests.
        if entry["refcount"] == 1:
            return entry["context"], entry["main_page"], entry["pw"], False, False
        else:
            page = await entry["context"].new_page()
            return entry["context"], page, entry["pw"], False, True

    if pool_key not in _browser_pool:
        _browser_pool[pool_key] = {"lock": asyncio.Lock()}

    async with _browser_pool[pool_key]["lock"]:
        entry = _browser_pool.get(pool_key)
        if entry and "context" in entry:
            entry["refcount"] = entry.get("refcount", 0) + 1
            if entry["refcount"] == 1:
                return entry["context"], entry["main_page"], entry["pw"], False, False
            else:
                page = await entry["context"].new_page()
                return entry["context"], page, entry["pw"], False, True

        from camoufox.utils import launch_options as _cf_lo
        from playwright.async_api import async_playwright

        launch_kw = {
            "headless": False,
            "humanize": True,
            "locale": "it-IT",
            "geoip": True,
        }
        if pool_key != "direct":
            launch_kw["proxy"] = {"server": pool_key}

        try:
            lo = _cf_lo(**launch_kw)
        except Exception as e:
            if "geoip" in str(e).lower() or "extra" in str(e).lower():
                logger.warning(
                    "[CamoufoxSolver] GeoIP extra not installed. Launching browser without geoip."
                )
                launch_kw["geoip"] = False
                lo = _cf_lo(**launch_kw)
            else:
                raise
        safe_key = pool_key.replace(":", "_").replace("/", "_")[:32]
        ctx_dir = os.path.join(tempfile.gettempdir(), f"camoufox_{safe_key}")
        os.makedirs(ctx_dir, exist_ok=True)

        logger.info(
            f"[CamoufoxSolver] Browser started for pool_key={pool_key[:40]}"
        )
        pw = await async_playwright().__aenter__()
        ctx = await pw.firefox.launch_persistent_context(
            ctx_dir, no_viewport=True, **lo
        )
        # Reuse the default page to avoid opening a second window/browser
        if ctx.pages:
            page = ctx.pages[0]
        else:
            page = await ctx.new_page()
        try:
            await page.evaluate("window.moveTo(0,0); window.resizeTo(1280, 720)")
        except Exception:
            pass

        _browser_pool[pool_key] = {
            "pw": pw,
            "context": ctx,
            "main_page": page,
            "refcount": 1,
            "lock": _browser_pool[pool_key]["lock"],
        }
        return ctx, page, pw, True, False


async def _release_browser(pool_key: str):
    """Decrement refcount. Close browser after 2s of idle time."""
    entry = _browser_pool.get(pool_key)
    if not entry or "context" not in entry:
        return
    entry["refcount"] = max(0, entry.get("refcount", 1) - 1)
    if entry["refcount"] > 0:
        return

    async def _delayed():
        await asyncio.sleep(2)
        async with entry["lock"]:
            if entry.get("refcount", 0) > 0:
                return
            logger.info(
                f"[CamoufoxSolver] Browser closed for pool_key={pool_key[:40]}"
            )
            try:
                await entry["context"].close()
            except Exception:
                pass
            try:
                await entry["pw"].__aexit__(None, None, None)
            except Exception:
                pass
            _browser_pool.pop(pool_key, None)

    old = entry.get("_close_task")
    if old and not old.done():
        old.cancel()
    entry["_close_task"] = asyncio.create_task(_delayed())


async def _close_all_browsers():
    """Close all pooled browsers (shutdown)."""
    for key, entry in list(_browser_pool.items()):
        if "context" not in entry:
            continue
        ct = entry.get("_close_task")
        if ct and not ct.done():
            ct.cancel()
        try:
            await entry["context"].close()
        except Exception:
            pass
        try:
            await entry["pw"].__aexit__(None, None, None)
        except Exception:
            pass
    _browser_pool.clear()


# --- Cookie cache ---

COOKIE_CACHE_FILE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "cache", "solver_cookies.json")
)


def _load_cookie_cache() -> dict:
    if os.path.exists(COOKIE_CACHE_FILE):
        try:
            with open(COOKIE_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cookie_cache(cache: dict):
    os.makedirs(os.path.dirname(COOKIE_CACHE_FILE), exist_ok=True)
    try:
        with open(COOKIE_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.error(f"Failed to save cookie cache: {e}")


def is_cloudflare_challenge(html: str, status: int) -> bool:
    if status in (403, 503):
        return True
    low_html = html.lower()
    if "cloudflare" in low_html and (
        "ray id" in low_html
        or "captcha" in low_html
        or "turnstile" in low_html
        or "challenge-platform" in low_html
    ):
        return True
    return False


async def fetch_page_with_cached_cookies(
    url, cookies_list, user_agent, proxy=None, post_data=None
) -> dict | None:
    """Try fetching with cached cookies using curl_cffi to match TLS fingerprint."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if post_data:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    cookies_dict = {c["name"]: c["value"] for c in cookies_list}

    proxy_url = None
    if proxy:
        proxy_url = proxy.get("url") if isinstance(proxy, dict) else str(proxy)
        if proxy_url.startswith("socks5h://"):
            proxy_url = proxy_url.replace("socks5h://", "socks5://", 1)
        elif proxy_url.startswith("socks4a://"):
            proxy_url = proxy_url.replace("socks4a://", "socks4://", 1)

    proxies_dict = None
    if proxy_url:
        proxies_dict = {"http": proxy_url, "https": proxy_url}

    try:
        from curl_cffi.requests import AsyncSession
        
        # Determine profile to match user agent
        profile = "firefox"
        if "chrome" in user_agent.lower():
            profile = "chrome124"
        elif "safari" in user_agent.lower() and "chrome" not in user_agent.lower():
            profile = "safari15"
            
        async with AsyncSession(impersonate=profile) as session:
            if post_data:
                resp = await session.post(
                    url, headers=headers, data=post_data,
                    cookies=cookies_dict, proxies=proxies_dict,
                    timeout=15, allow_redirects=True,
                )
            else:
                resp = await session.get(
                    url, headers=headers,
                    cookies=cookies_dict, proxies=proxies_dict,
                    timeout=15, allow_redirects=True,
                )

            html = resp.text
            status = resp.status_code

            if is_cloudflare_challenge(html, status):
                logger.info(
                    f"[CamoufoxSolver] Cache check: CF challenge on {url} (status {status})"
                )
                return None

            logger.info(
                f"[CamoufoxSolver] Cache HIT: {url} (status {status})"
            )

            # Update cookies
            cookie_map = {c["name"]: c for c in cookies_list}
            resp_cookies_dict = dict(resp.cookies) if hasattr(resp, 'cookies') else {}
            for cn, cv in resp_cookies_dict.items():
                cookie_map[cn] = {
                    "name": cn, "value": cv,
                    "domain": urlparse(url).netloc,
                    "path": "/",
                }

            return {
                "status": "ok",
                "message": "Bypassed using cached cookies",
                "solution": {
                    "url": str(resp.url),
                    "status": status,
                    "cookies": list(cookie_map.values()),
                    "userAgent": user_agent,
                    "response": html,
                },
            }
    except Exception as e:
        logger.warning(f"[CamoufoxSolver] Cached cookie fetch failed: {e}")
        return None


# --- Display singleton ---

_display = None


def _bootstrap_display():
    global _display
    if _display is not None:
        return
    if os.name == "nt":
        _display = False
        return
    try:
        from pyvirtualdisplay import Display
        _display = Display(visible=0, size=(1920, 1080))
        _display.start()
        subprocess.Popen(
            ["fluxbox"], env={**os.environ}, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        logger.info("[CamoufoxSolver] Virtual display + fluxbox started")
    except Exception as e:
        logger.warning(f"[CamoufoxSolver] Display failed: {e}")
        _display = False


def _stop_display():
    global _display
    if _display and _display is not False:
        try:
            _display.stop()
        except Exception:
            pass
    _display = None


# --- Tab+Space (pyautogui, cross-platform) ---

def _tab_space_interact():
    try:
        if os.name == "nt":
            import pyautogui
            import pygetwindow as gw
            w = None
            for ww in gw.getAllWindows():
                try:
                    if "camoufox" in (ww.title or "").lower() and ww.visible:
                        w = ww; break
                except Exception:
                    pass
            if not w:
                for ww in gw.getWindowsWithTitle("Camoufox"):
                    if ww.visible:
                        w = ww; break
            if not w:
                return False
            w.activate()
            time.sleep(0.3)
            pyautogui.press("tab")
            time.sleep(0.3)
            pyautogui.press("space")
            return True
        else:
            wid = None
            r = subprocess.run(
                ["xdotool", "search", "--name", "Camoufox"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip():
                wid = r.stdout.strip().split("\n")[0]
            else:
                r2 = subprocess.run(
                    ["xdotool", "search", "--class", "Firefox"],
                    capture_output=True, text=True, timeout=10,
                )
                if r2.stdout.strip():
                    wid = r2.stdout.strip().split("\n")[0]
            if not wid:
                return False
            subprocess.run(["xdotool", "windowfocus", "--sync", wid], timeout=10)
            time.sleep(0.3)
            disp = os.environ.get("DISPLAY", ":99")
            subprocess.run(
                ["xauth", "add", disp, ".", "ffffffffffffffffffffffffffffffff"],
                capture_output=True, timeout=5,
            )
            import pyautogui
            pyautogui.press("tab")
            time.sleep(0.3)
            pyautogui.press("space")
            return True
    except Exception as ex:
        logger.warning(f"[CamoufoxSolver] tab_space error: {ex}")
        return False


# --- Core bypass: pooled browser, poll-based ---

async def _safe_title(page):
    try:
        return await page.title()
    except Exception:
        return ""


async def run_camoufox_request(url, proxy=None, post_data=None, cookies=None) -> dict:
    _bootstrap_display()

    proxy_string = None
    if proxy:
        proxy_string = (
            proxy.get("url") if isinstance(proxy, dict) else str(proxy)
        )
    pool_key = proxy_string or "direct"

    is_additional_tab = False
    try:
        ctx, page, pw, is_new, is_additional_tab = await _get_or_create_browser(pool_key)
        page.set_default_timeout(90000)

        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception as e:
                logger.warning(f"[CamoufoxSolver] Failed to add cookies: {e}")

        try:
            await page.bring_to_front()
            await page.evaluate("window.moveTo(0,0); window.resizeTo(1280, 720)")
        except Exception:
            pass

        if post_data:
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            logger.info(f"[CamoufoxSolver] POST base: {base}")
            await page.goto(base, wait_until="domcontentloaded")
        else:
            logger.info(f"[CamoufoxSolver] GET: {url}")
            await page.goto(url, wait_until="domcontentloaded")

        logger.info(
            f"[CamoufoxSolver] Loaded. title={await _safe_title(page)!r}"
        )

        # Challenge polling
        challenge_titles = [
            "just a moment", "ci siamo quasi", "attention required",
            "un instant", "un moment", "einen moment", "un momento",
            "so um momento", "um momento", "even geduld", "bir an",
            "chwileczk", "ett ogonblick", "et ojeblik", "et oyeblikk",
            "hetkinen", "pillanatot", "okamzik", "okamihu",
        ]

        def _is_challenge(t):
            return t and any(m in t.lower() for m in challenge_titles)

        start = time.time()
        max_wait = 120
        tab_cd = 0

        while time.time() - start < max_wait:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass

            t = await _safe_title(page)
            elapsed = time.time() - start

            if not _is_challenge(t):
                await asyncio.sleep(0.8)
                t2 = await _safe_title(page)
                if not _is_challenge(t2):
                    logger.info(
                        f"[CamoufoxSolver] Resolved after {elapsed:.1f}s. title={t2!r}"
                    )
                    break

            if elapsed - (elapsed % 1) < 0.01:
                logger.info(
                    f"[CamoufoxSolver] Poll {elapsed:.1f}s title={t!r}"
                )

            if elapsed > 3 and time.time() - tab_cd > 5:
                logger.info("[CamoufoxSolver] tab_space...")
                try:
                    await page.bring_to_front()
                    # Click inside the webpage viewport (top-left) to pull keyboard focus out of the address/extensions bar
                    await page.mouse.click(10, 10)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"[CamoufoxSolver] tab_space focus click failed: {e}")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _tab_space_interact)
                tab_cd = time.time()

            await asyncio.sleep(0.5)

        # Capture result
        status_code = 200
        html = ""
        current_url = url

        if post_data:
            logger.info(f"[CamoufoxSolver] POST {url}")
            js = (
                "(a) => fetch(a.url, {"
                "  method: 'POST',"
                "  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },"
                "  body: a.body"
                "})"
                ".then(r => r.text().then(t => ({"
                "  status: r.status, url: r.url, text: t"
                "})))"
                ".catch(e => ({ status: 0, url: '', text: e.message }))"
            )
            try:
                r = await page.evaluate(js, {"url": url, "body": post_data})
                status_code = r.get("status", 200)
                html = r.get("text", "")
                current_url = r.get("url", url)
            except Exception as e:
                logger.error(f"[CamoufoxSolver] POST eval failed: {e}")
        else:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                html = await page.content()
                current_url = page.url
            except Exception as e:
                logger.warning(f"[CamoufoxSolver] Content capture: {e}")

        if _is_challenge(await _safe_title(page)):
            logger.warning("[CamoufoxSolver] Bypass FAILED")
            return {"status": "error", "message": "CF bypass failed", "solution": {}}

        # Cookies
        cookies = []
        try:
            for c in await page.context.cookies():
                ck = {
                    "name": c.get("name"), "value": c.get("value"),
                    "domain": c.get("domain"), "path": c.get("path"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                }
                if "expires" in c:
                    ck["expiry"] = c["expires"]
                cookies.append(ck)
        except Exception as e:
            logger.warning(f"[CamoufoxSolver] Cookie extraction: {e}")

        ua = await page.evaluate("navigator.userAgent")

        logger.info(
            f"[CamoufoxSolver] OK. Cookies: {len(cookies)}, status: {status_code}"
        )

        return {
            "status": "ok",
            "message": "OK",
            "solution": {
                "url": current_url,
                "status": status_code,
                "cookies": cookies,
                "userAgent": ua,
                "response": html,
            },
        }

    except Exception as e:
        logger.error(f"[CamoufoxSolver] Error: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Camoufox solver error: {str(e)}",
            "solution": {},
        }
    finally:
        if is_additional_tab:
            try:
                await page.close()
            except Exception:
                pass
        await _release_browser(pool_key)


# --- FlareSolverr-compatible API ---

_last_cache_hits = {}


async def handle_v1_request(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"status": "error", "message": "Invalid JSON"}, status=400
        )

    cmd = body.get("cmd")
    logger.debug(f"[CamoufoxSolver] cmd: {cmd}")

    if cmd in ("request.get", "request.post"):
        url = body.get("url")
        proxy = body.get("proxy")
        post_data = body.get("postData")
        session_id = body.get("session")

        if not proxy and session_id and session_id in _sessions_proxies:
            proxy = _sessions_proxies[session_id]

        def norm_proxy(p):
            if not p:
                return "direct"
            if isinstance(p, dict):
                return p.get("url", "direct")
            return str(p)

        domain = urlparse(url).netloc
        proxy_str = norm_proxy(proxy)

        async with _get_domain_lock(domain):
            cache = _load_cookie_cache()
            cached = cache.get(domain)

            if cached:
                cache_age = time.time() - cached.get("timestamp", 0)
                cached_proxy = cached.get("proxy", "direct")
                last_hit = _last_cache_hits.get(domain, 0)
                rapid = (time.time() - last_hit) < 15 and cmd != "request.post"

                if cache_age < 3600 and proxy_str == cached_proxy and not rapid:
                    logger.info(
                        f"[CamoufoxSolver] Cache for {domain} (age: {int(cache_age)}s)"
                    )
                    res = await fetch_page_with_cached_cookies(
                        url, cached["cookies"], cached["userAgent"],
                        proxy, post_data if cmd == "request.post" else None,
                    )
                    if res:
                        _last_cache_hits[domain] = time.time()
                        cached["cookies"] = res["solution"]["cookies"]
                        cached["timestamp"] = time.time()
                        cache[domain] = cached
                        _save_cookie_cache(cache)
                        return web.json_response(res)
                else:
                    if rapid:
                        cache.pop(domain, None)
                        _save_cookie_cache(cache)

            cookies_payload = body.get("cookies")
            res = await run_camoufox_request(url, proxy, post_data, cookies=cookies_payload)

            if res.get("status") == "ok":
                cache[domain] = {
                    "cookies": res["solution"]["cookies"],
                    "userAgent": res["solution"]["userAgent"],
                    "timestamp": time.time(),
                    "proxy": proxy_str,
                }
                _save_cookie_cache(cache)
                _last_cache_hits[domain] = time.time()

            return web.json_response(res)

    elif cmd == "sessions.create":
        sid = f"camoufox-{uuid.uuid4()}"
        proxy = body.get("proxy")
        if proxy:
            _sessions_proxies[sid] = proxy
        return web.json_response(
            {"status": "ok", "message": "Session created", "session": sid}
        )

    elif cmd == "sessions.destroy":
        sid = body.get("session")
        if sid:
            _sessions_proxies.pop(sid, None)
        return web.json_response({"status": "ok", "message": "Session destroyed"})

    elif cmd == "sessions.list":
        return web.json_response(
            {"status": "ok", "sessions": list(_sessions_proxies.keys())}
        )

    elif cmd == "health":
        return web.json_response({
            "status": "ok",
            "message": "Camoufox Solver v2.1",
            "version": "2.1",
        })

    return web.json_response(
        {"status": "error", "message": f"Unsupported: {cmd}"}
    )


async def ensure_flaresolverr() -> bool:
    global _mock_server_running, _runner
    if _mock_server_running:
        return True

    try:
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as s:
            async with s.post(
                "http://127.0.0.1:8191/v1", json={"cmd": "health"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "ok" and "Camoufox" in data.get("message", ""):
                        _mock_server_running = True
                        return True
    except Exception:
        pass

    logger.info("[CamoufoxSolver] Starting solver on :8191/v1")
    app = web.Application()
    app.router.add_post("/v1", handle_v1_request)

    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", 8191)

    try:
        await site.start()
        _mock_server_running = True
        logger.info("[CamoufoxSolver] Solver listening on :8191/v1")
        return True
    except Exception as e:
        logger.error(f"[CamoufoxSolver] Failed to start on :8191: {e}")
        return False


async def try_shutdown_idle_flaresolverr():
    pass


async def shutdown_flaresolverr():
    global _mock_server_running, _runner
    logger.info("[CamoufoxSolver] Shutting down...")
    await _close_all_browsers()
    _stop_display()
    if _runner:
        await _runner.cleanup()
        _runner = None
        _mock_server_running = False


class SolverSessionManager:
    async def get_session(self, proxy=None) -> tuple[str, bool]:
        await ensure_flaresolverr()
        sid = f"camoufox-{uuid.uuid4()}"
        if proxy:
            _sessions_proxies[sid] = proxy
        return sid, False

    async def get_persistent_session(self, key, proxy=None) -> str:
        await ensure_flaresolverr()
        sid = f"camoufox-{key}"
        if proxy:
            _sessions_proxies[sid] = proxy
        return sid

    async def release_session(self, sid, is_persistent):
        if sid:
            _sessions_proxies.pop(sid, None)


solver_manager = SolverSessionManager()
