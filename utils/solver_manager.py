import asyncio
import logging
import os
import uuid
import json
import time
from urllib.parse import urlparse
from aiohttp import web, ClientSession, ClientTimeout
from pyvirtualdisplay import Display
from seleniumbase import Driver

from config import FLARESOLVERR_TIMEOUT, FLARESOLVERR_URL

logger = logging.getLogger(__name__)

# Global state for the mock server
_mock_server_running = False
_runner = None
_sessions_proxies = {}
_domain_locks = {}

def _get_domain_lock(domain: str) -> asyncio.Lock:
    if domain not in _domain_locks:
        _domain_locks[domain] = asyncio.Lock()
    return _domain_locks[domain]

COOKIE_CACHE_FILE = os.path.normpath(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "cache", "solver_cookies.json")))

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
    if "cloudflare" in low_html and ("ray id" in low_html or "captcha" in low_html or "turnstile" in low_html or "challenge-platform" in low_html):
        return True
    return False

async def fetch_page_with_cached_cookies(url: str, cookies_list: list, user_agent: str, proxy: str | dict = None, post_data: str = None) -> dict | None:
    """Tries to fetch the page using cached cookies. Returns solution dict if successful, else None."""
    headers = {"User-Agent": user_agent}
    if post_data:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        
    cookies_dict = {c["name"]: c["value"] for c in cookies_list}
    
    proxy_url = None
    if proxy:
        if isinstance(proxy, dict):
            proxy_url = proxy.get("url")
        else:
            proxy_url = proxy
            
    connector = None
    if proxy_url and proxy_url.startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy_url)
            proxy_url = None
        except ImportError:
            pass
            
    try:
        timeout = ClientTimeout(total=15)
        async with ClientSession(connector=connector, cookies=cookies_dict, timeout=timeout) as session:
            if post_data:
                req_coro = session.post(url, headers=headers, data=post_data, proxy=proxy_url, allow_redirects=True)
            else:
                req_coro = session.get(url, headers=headers, proxy=proxy_url, allow_redirects=True)
                
            async with req_coro as resp:
                html = await resp.text(errors="ignore")
                status = resp.status
                
                if is_cloudflare_challenge(html, status):
                    logger.info(f"Cache check: Cloudflare challenge detected for {url} (status {status})")
                    return None
                    
                logger.info(f"Cache HIT: successfully fetched {url} without browser (status {status})")
                
                # Update cookies with any newly set cookies in response headers
                cookie_map = {c["name"]: c for c in cookies_list}
                for cookie_name, cookie_meta in resp.cookies.items():
                    cookie_map[cookie_name] = {
                        "name": cookie_name,
                        "value": cookie_meta.value,
                        "domain": cookie_meta.get("domain", ""),
                        "path": cookie_meta.get("path", "/"),
                    }
                
                return {
                    "status": "ok",
                    "message": "Bypassed using cached cookies",
                    "solution": {
                        "url": str(resp.url),
                        "status": status,
                        "cookies": list(cookie_map.values()),
                        "userAgent": user_agent,
                        "response": html
                    }
                }
    except Exception as e:
        logger.warning(f"Failed to fetch page with cached cookies: {e}")
        return None

def run_seleniumbase_request(url: str, proxy: str | dict = None, post_data: str = None) -> dict:
    """Runs SeleniumBase in UC mode under a virtual display (on Linux) to bypass Cloudflare."""
    display = None
    driver = None
    try:
        # 1. Start Xvfb virtual display (Linux only)
        if os.name != "nt":
            logger.info("Starting virtual display (Xvfb)...")
            display = Display(visible=0, size=(1920, 1080))
            display.start()
        else:
            logger.info("Running on Windows; skipping Xvfb initialization.")
        
        # 2. Extract proxy string
        proxy_string = None
        if proxy:
            if isinstance(proxy, dict):
                proxy_string = proxy.get("url")
            else:
                proxy_string = proxy
        
        # Determine Chromium binary location
        chrome_path = "/usr/bin/chromium"
        if not os.path.exists(chrome_path):
            chrome_path = None
            
        logger.info(f"Launching SeleniumBase Driver (uc=True) for URL: {url} (proxy: {proxy_string})")
        
        driver_kwargs = {
            "uc": True,
            "proxy": proxy_string
        }
        if chrome_path:
            driver_kwargs["binary_location"] = chrome_path
            
        driver = Driver(**driver_kwargs)
        
        # Force a non-maximized window size to prevent Chrome from remembering maximized state
        try:
            driver.set_window_size(1280, 800)
        except Exception:
            pass
        
        # If it's a POST request, open the base domain URL first to bypass Turnstile
        if post_data:
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
            logger.info(f"POST request detected. Opening base domain first: {base_url}")
            driver.uc_open_with_reconnect(base_url, reconnect_time=7)
        else:
            logger.info("Loading page...")
            driver.uc_open_with_reconnect(url, reconnect_time=4)
        
        # --- Challenge Resolution Logic ---
        bypassed_auto = False
        has_iframe = False
        logger.info("Checking challenge status...")
        
        # Cloudflare challenge page titles in all common languages
        challenge_titles = [
            "just a moment",        # EN
            "cloudflare",           # Generic
            "cf-challenge",         # Generic
            "ci siamo quasi",       # IT
            "attention required",   # EN
            "un instant",           # FR
            "un moment",            # FR/RO
            "einen moment",         # DE
            "un momento",           # ES
            "só um momento",        # PT
            "um momento",           # PT alt
            "even geduld",          # NL
            "bir an",               # TR
            "chwileczk",            # PL
            "ett ögonblick",        # SV
            "et øjeblik",           # DA
            "et øyeblikk",          # NO
            "hetkinen",             # FI
            "pillanatot",           # HU
            "okamžik",              # CS
            "okamihu",              # SK
            "подождите",            # RU
            "зачекайте",            # UK
            "少々お待ち",             # JA
            "请稍候",                # ZH-CN
            "請稍候",                # ZH-TW
            "잠시만",                # KO
            "لحظة",                  # AR
        ]
        
        # Helper: check if still on challenge page
        def _is_on_challenge():
            try:
                t = driver.title or ""
                h = driver.page_source or ""
                t_low = t.lower()
                is_title = any(m in t_low for m in challenge_titles)
                is_html = is_cloudflare_challenge(h, 200)
                return (is_title or is_html), t_low
            except Exception:
                return True, ""
        
        # Phase 1: Quick check & click on initial load
        driver.sleep(3.5)
        on_challenge, title_low = _is_on_challenge()
        if on_challenge:
            logger.info("Challenge detected on initial load. Attempting to click widget...")
            try:
                driver.uc_gui_click_captcha()
                driver.sleep(4)
                on_challenge, title_low = _is_on_challenge()
                if not on_challenge:
                    logger.info("Cloudflare challenge resolved on initial load.")
                    bypassed_auto = True
            except Exception as ex:
                logger.warning(f"Initial click attempt error: {ex}")
        else:
            logger.info("Bypassed automatically on initial load.")
            bypassed_auto = True
        
        # Phase 2: Fallback attempts with reload if not bypassed
        if not bypassed_auto:
            logger.info(f"Still on challenge page (title='{title_low[:40]}'). Initiating reloads with disconnect + click fallback...")
            
            for attempt in range(1, 3):
                try:
                    logger.info(f"Attempt {attempt}/2: reloading with CDP off...")
                    driver.uc_open_with_reconnect(url, reconnect_time=4)
                    driver.sleep(3)
                    
                    on_challenge, title_low = _is_on_challenge()
                    if not on_challenge:
                        logger.info(f"Bypassed automatically on reload {attempt}.")
                        bypassed_auto = True
                        break
                    
                    logger.info(f"Clicking CF widget (reload attempt {attempt})...")
                    driver.uc_gui_click_captcha()
                    driver.sleep(4)
                    
                    on_challenge, title_low = _is_on_challenge()
                    if not on_challenge:
                        logger.info(f"Cloudflare challenge resolved on reload attempt {attempt}.")
                        bypassed_auto = True
                        break
                except Exception as ex:
                    logger.warning(f"Reload attempt {attempt} error: {ex}")
            
        # If it's a POST request, execute the POST programmatically inside the browser context
        status_code = 200
        html = ""
        current_url = url
        title = driver.title
        
        if post_data:
            logger.info(f"Executing programmatic POST request to: {url}")
            js_script = """
                const callback = arguments[arguments.length - 1];
                fetch(arguments[0], {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded'
                    },
                    body: arguments[1]
                })
                .then(r => r.text().then(text => callback({status: r.status, url: r.url, text: text})))
                .catch(err => callback({status: 0, url: '', text: err.message}));
            """
            post_res = driver.execute_async_script(js_script, url, post_data)
            status_code = post_res.get("status", 200)
            html = post_res.get("text", "")
            current_url = post_res.get("url", url)
        else:
            title = driver.title
            html = driver.page_source
            current_url = driver.current_url
            
        # Verify page retrieval (skip verification check for POST since it contains POST response instead of challenge)
        if not post_data:
            if is_cloudflare_challenge(html, 200) or "just a moment" in title.lower() or "cloudflare" in title.lower():
                logger.warning(f"Bypass verification failed. Title: '{title}'. HTML length: {len(html)}")
                return {
                    "status": "error",
                    "message": "Cloudflare challenge bypass failed (verification check failed)",
                    "solution": {}
                }
        
        # Extract cookies in FlareSolverr format
        selenium_cookies = driver.get_cookies()
        cookies = []
        for c in selenium_cookies:
            cookies.append({
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path"),
                "expiry": c.get("expiry"),
                "httpOnly": c.get("httpOnly"),
                "secure": c.get("secure")
            })
            
        # Extract user agent
        ua = driver.execute_script("return navigator.userAgent;")
        
        logger.info(f"Request completed. Cookies found: {len(cookies)}")
        
        return {
            "status": "ok",
            "message": "Request completed successfully",
            "solution": {
                "url": current_url,
                "status": status_code,
                "cookies": cookies,
                "userAgent": ua,
                "response": html
            }
        }
    except Exception as e:
        logger.error(f"Error during SeleniumBase solver execution: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"SeleniumBase solver error: {str(e)}",
            "solution": {}
        }
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if display:
            try:
                display.stop()
            except Exception:
                pass


_last_cache_hits = {}


async def handle_v1_request(request):
    """Handles standard FlareSolverr v1 API POST requests."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
        
    cmd = body.get("cmd")
    logger.info(f"Received solver command: {cmd}")
    
    if cmd in ("request.get", "request.post"):
        url = body.get("url")
        proxy = body.get("proxy")
        post_data = body.get("postData")
        session_id = body.get("session")
        
        if not proxy and session_id and session_id in _sessions_proxies:
            proxy = _sessions_proxies[session_id]
            
        def normalize_proxy_to_str(p) -> str:
            if not p:
                return "direct"
            if isinstance(p, dict):
                return p.get("url", "direct")
            return str(p)
            
        domain = urlparse(url).netloc
        current_proxy_str = normalize_proxy_to_str(proxy)
        
        # Per-domain lock: only one browser per domain at a time
        async with _get_domain_lock(domain):
            cache = _load_cookie_cache()
            
            # Check cache
            cached_entry = cache.get(domain)
            if cached_entry:
                cached_proxy_str = cached_entry.get("proxy", "direct")
                cache_age = time.time() - cached_entry.get("timestamp", 0)
                
                # Check if this domain was served from cache very recently
                last_hit_time = _last_cache_hits.get(domain, 0)
                time_since_last_hit = time.time() - last_hit_time
                is_rapid_retry = time_since_last_hit < 15
                
                # Disable rapid retry check for POST requests to allow GET->POST sequences
                if cmd == "request.post":
                    is_rapid_retry = False
                
                # Use cache ONLY if it's fresh (less than 1 hour), the proxy/IP has not changed, and it's not a rapid retry
                if cache_age < 3600 and current_proxy_str == cached_proxy_str and not is_rapid_retry:
                    logger.info(f"Found cached cookies for domain: {domain} (age: {int(cache_age)}s, proxy: {current_proxy_str}). Verifying...")
                    res = await fetch_page_with_cached_cookies(
                        url, 
                        cached_entry["cookies"], 
                        cached_entry["userAgent"], 
                        proxy,
                        post_data=post_data if cmd == "request.post" else None
                    )
                    if res:
                        # Update cache hit timestamp
                        _last_cache_hits[domain] = time.time()
                        # Update the cache with any new cookies captured
                        cached_entry["cookies"] = res["solution"]["cookies"]
                        cached_entry["timestamp"] = time.time()
                        cache[domain] = cached_entry
                        _save_cookie_cache(cache)
                        return web.json_response(res)
                else:
                    reason = "stale" if cache_age >= 3600 else ("rapid retry / failed cache" if is_rapid_retry else "proxy/IP changed")
                    logger.info(f"Cached cookies for domain {domain} are invalid or failed ({reason}). Forcing new solver run.")
                    if is_rapid_retry:
                        # Clear cache entry to prevent looping
                        cache.pop(domain, None)
                        _save_cookie_cache(cache)
                    
            # Cache miss or verification failed, run SeleniumBase
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, run_seleniumbase_request, url, proxy, post_data)
            
            if res.get("status") == "ok":
                # Save new cookies to cache along with the proxy used
                cache[domain] = {
                    "cookies": res["solution"]["cookies"],
                    "userAgent": res["solution"]["userAgent"],
                    "timestamp": time.time(),
                    "proxy": current_proxy_str
                }
                _save_cookie_cache(cache)
                # Set the initial cache hit timestamp
                _last_cache_hits[domain] = time.time()
                
            return web.json_response(res)
        
    elif cmd == "sessions.create":
        session_id = f"sb-session-{uuid.uuid4()}"
        proxy = body.get("proxy")
        if proxy:
            _sessions_proxies[session_id] = proxy
        return web.json_response({"status": "ok", "message": "Session created", "session": session_id})
        
    elif cmd == "sessions.destroy":
        session_id = body.get("session")
        if session_id:
            _sessions_proxies.pop(session_id, None)
        return web.json_response({"status": "ok", "message": "Session destroyed"})
        
    elif cmd == "sessions.list":
        return web.json_response({"status": "ok", "sessions": list(_sessions_proxies.keys())})
        
    elif cmd == "health":
        return web.json_response({"status": "ok", "message": "SeleniumBase Solver Emulator is healthy", "version": "1.1"})
        
    else:
        return web.json_response({"status": "error", "message": f"Unsupported command: {cmd}"})


async def ensure_flaresolverr() -> bool:
    """Starts our custom local emulator on the expected port (8191)."""
    global _mock_server_running, _runner
    if _mock_server_running:
        return True
        
    # Check if something is already running on port 8191 first
    try:
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as session:
            async with session.post("http://127.0.0.1:8191/v1", json={"cmd": "health"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "ok" and "SeleniumBase" in data.get("message", ""):
                        logger.info("An active, compatible SeleniumBase mock server is already running on port 8191.")
                        _mock_server_running = True
                        return True
    except Exception:
        pass

    logger.info("Initializing custom SeleniumBase solver manager...")
    app = web.Application()
    app.router.add_post('/v1', handle_v1_request)
    
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, '127.0.0.1', 8191)
    
    try:
        await site.start()
        _mock_server_running = True
        logger.info("Custom SeleniumBase mock server listening on http://127.0.0.1:8191/v1")
        return True
    except Exception as e:
        logger.error(
            f"CRITICAL: Failed to start local SeleniumBase API emulator on port 8191: {e}. "
            "Please ensure no other process (like standard FlareSolverr or a zombie EasyProxy process) is using port 8191."
        )
        return False


async def try_shutdown_idle_flaresolverr():
    pass


async def shutdown_flaresolverr():
    """Stops the local API emulator."""
    global _mock_server_running, _runner
    if _runner:
        logger.info("Stopping custom SeleniumBase API emulator...")
        await _runner.cleanup()
        _runner = None
        _mock_server_running = False
        logger.info("Custom SeleniumBase API emulator stopped.")


class SolverSessionManager:
    """Compatibility class mimicking the original session manager."""
    
    async def get_session(self, proxy: str = None) -> tuple[str, bool]:
        await ensure_flaresolverr()
        session_id = f"sb-session-{uuid.uuid4()}"
        if proxy:
            _sessions_proxies[session_id] = proxy
        return session_id, False
        
    async def get_persistent_session(self, key: str, proxy: str = None) -> str:
        await ensure_flaresolverr()
        session_id = f"sb-session-{key}"
        if proxy:
            _sessions_proxies[session_id] = proxy
        return session_id
        
    async def release_session(self, session_id: str, is_persistent: bool):
        if session_id:
            _sessions_proxies.pop(session_id, None)

solver_manager = SolverSessionManager()
