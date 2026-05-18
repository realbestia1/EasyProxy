import asyncio
import logging
import os
import sys

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_playwright = None
_browser = None
_context = None
_profile_dir = os.path.join(os.getcwd(), ".shared_chromium_profile")


async def get_shared_browser_context(user_agent: str):
    global _playwright, _browser, _context

    async with _lock:
        if _browser and _context:
            try:
                await _browser.version()
                return _playwright, _browser, _context
            except Exception:
                _browser = None
                _context = None

        if not _playwright:
            _playwright = await async_playwright().start()

        if not _browser and not _context:
            _browser = await _connect_existing_browser()
            if not _browser:
                _context = await _launch_persistent_context(user_agent)
                _browser = _context.browser
                logger.info("🚀 [Shared Browser] Launching persistent instance on port 9222")

        if not _context:
            contexts = _browser.contexts if _browser else []
            if contexts:
                _context = contexts[0]
            else:
                raise RuntimeError("Shared browser has no reusable persistent context")
        return _playwright, _browser, _context


async def close_shared_browser():
    global _playwright, _browser, _context

    async with _lock:
        context = _context
        browser = _browser
        playwright = _playwright
        _context = None
        _browser = None
        _playwright = None

        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright:
                await playwright.stop()
        except Exception:
            pass
        logger.info("💤 [Shared Browser] Closed after inactivity")


async def _connect_existing_browser():
    try:
        browser = await _playwright.chromium.connect_over_cdp(
            "http://localhost:9222",
            timeout=5000,
        )
        logger.info("🔗 [Shared Browser] Connected to existing persistent instance on port 9222")
        return browser
    except Exception:
        return None


async def _launch_persistent_context(user_agent: str):
    chrome_path = os.getenv("CHROME_BIN") or os.getenv("CHROME_EXE_PATH")
    executable_path = chrome_path if chrome_path and os.path.exists(chrome_path) else None
    try:
        return await _playwright.chromium.launch_persistent_context(
            _profile_dir,
            headless=sys.platform.startswith("linux"),
            executable_path=executable_path,
            user_agent=user_agent,
            viewport={"width": 1366, "height": 768},
            args=[
                "--remote-debugging-port=9222",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
    except Exception:
        logger.warning("⚠️ Port 9222 in use, connecting to existing browser...")
        await asyncio.sleep(1)
        browser = await _playwright.chromium.connect_over_cdp(
            "http://localhost:9222",
            timeout=10000,
        )
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Connected browser has no persistent context")
        return contexts[0]
