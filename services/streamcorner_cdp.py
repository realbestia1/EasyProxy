"""Streamcorner resolver over CDP.

The site only releases the signed embed URL to a Web Worker running on its own
origin (the worker script checks an anti-tamper token), so plain HTTP cannot
reproduce it. This module drives the Chrome used by ``services.turnstile_cdp``:
open a page on the origin, spawn the worker from the page context, then read the
``.m3u8`` from the embed page's network traffic. The CDN itself is open (Akamai,
no headers required).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

from services.turnstile_cdp import BROWSER_LOCK, _CDP, launch_browser

logger = logging.getLogger(__name__)

_PAGE = "https://streamcorner.foo/404"
# Worker module that answers ``provider-detail``; the hash follows the build.
_WORKER = os.getenv("STREAMCORNER_WORKER", "/assets/cU3nVUmS.js")
# Anti-tamper token key the worker expects in the request payload.
_TOKEN_KEY = "_98e23722a92de7e90e0e6d55"
# Fallback data hosts: the live pool is scraped from the site's own chunks.
_HOSTS = (
    "data.gigav.workers.dev",
    "data.yedmzoa.workers.dev",
    "data.ngagzipx.workers.dev",
    "data.miopks.workers.dev",
    "data.senbon001.workers.dev",
    "data.senbon002.workers.dev",
    "data.senbon003.workers.dev",
    "data.kageyoshi001.workers.dev",
)

_WORKER_BODY = r"""
  const pick = d => {
    const s = d && d.data && d.data.streams && d.data.streams[0];
    return s && s.embed_url ? s.embed_url : null;
  };
  // The data host pool rotates: read it from the chunks the page already loaded.
  const texts = [];
  for (const entry of performance.getEntriesByType('resource')) {
    if (!entry.name.includes('/assets/') || !entry.name.endsWith('.js')) continue;
    try { texts.push(await (await fetch(entry.name)).text()); } catch (e) { texts.push(''); }
  }
  const pool = new Set();
  for (const text of texts) {
    for (const m of text.matchAll(/data\.[a-z0-9-]+\.workers\.dev/g)) pool.add(m[0]);
  }
  const hosts = pool.size ? [...pool].slice(0, 8) : fallback;
  const tok = Array.from(
    crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')
  ).join('');
  const urls = hosts.map(h => `https://${h}/corner?p=${encodeURIComponent(provider)}&id=${encodeURIComponent(id)}`);
  let worker;
  try {
    worker = new Worker(workerPath + '#' + tok, {type: 'module', name: 'provider-data'});
  } catch (e) {
    return JSON.stringify({error: 'worker create failed: ' + e.message});
  }
  const reply = await new Promise(resolve => {
    let last = null;
    let soft = 0;
    let timer = setTimeout(() => resolve(last || {error: 'worker timeout'}), 20000);
    const settle = d => { clearTimeout(timer); clearTimeout(soft); resolve(d); };
    worker.onmessage = e => {
      const d = e.data || {};
      if (pick(d)) { settle(d); return; }
      last = d;
      // A first reply without streams may be followed by a useful one.
      soft = soft || setTimeout(() => settle(last), 1500);
    };
    worker.onerror = e => settle({error: 'worker error: ' + (e.message || '')});
    worker.postMessage({
      requestId: 1,
      operation: 'provider-detail',
      providerId: provider,
      providerName: providerName,
      requestUrls: urls,
      [tokenKey]: tok
    });
  });
  try { worker.terminate(); } catch (e) {}
  return JSON.stringify(reply);
"""


def _worker_expression(provider: str, content_id: str) -> str:
    return (
        "(async () => {\n"
        f"  const provider = {json.dumps(provider)};\n"
        f"  const providerName = {json.dumps(provider.upper())};\n"
        f"  const id = {json.dumps(content_id)};\n"
        f"  const workerPath = {json.dumps(_WORKER)};\n"
        f"  const tokenKey = {json.dumps(_TOKEN_KEY)};\n"
        f"  const fallback = {json.dumps(list(_HOSTS))};\n"
        f"{_WORKER_BODY}\n"
        "})()"
    )


async def resolve_stream(
    provider: str, content_id: str, timeout: float = 45.0
) -> str | None:
    """Return the CDN ``.m3u8`` for ``provider``/``content_id``, None on failure."""
    try:
        return await _resolve(provider, content_id, timeout)
    except Exception as e:
        logger.warning(
            "Streamcorner resolver: failed for %s/%s: %r", provider, content_id, e
        )
        return None


async def _resolve(provider: str, content_id: str, timeout: float) -> str | None:
    async with BROWSER_LOCK:
        deadline = time.monotonic() + timeout
        launched = await launch_browser("Streamcorner resolver")
        if not launched:
            return None
        proc, version, _ua = launched
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    version["webSocketDebuggerUrl"], max_msg_size=0
                ) as ws:
                    cdp = _CDP(ws)
                    try:
                        m3u8 = await _drive(cdp, provider, content_id, deadline)
                    finally:
                        await cdp.close()
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    if m3u8:
        logger.info("Streamcorner resolver: manifest %s", m3u8[:100])
    else:
        logger.warning(
            "Streamcorner resolver: no .m3u8 for %s/%s within %.0fs",
            provider,
            content_id,
            timeout,
        )
    return m3u8


async def _open_target(cdp, url: str) -> tuple[str, str]:
    target = await cdp.send("Target.createTarget", {"url": "about:blank"})
    attached = await cdp.send(
        "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
    )
    sid = attached["sessionId"]
    await cdp.send("Page.enable", {}, sid)
    await cdp.send("Network.enable", {}, sid)
    await cdp.send(
        "Target.setAutoAttach",
        {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        sid,
    )
    await cdp.send("Page.navigate", {"url": url}, sid)
    return target["targetId"], sid


async def _close_target(cdp, target_id: str) -> None:
    try:
        await cdp.send("Target.closeTarget", {"targetId": target_id})
    except Exception as e:
        logger.debug("Streamcorner resolver: close failed: %r", e)


async def _drive(cdp, provider: str, content_id: str, deadline: float) -> str | None:
    target_id, sid = await _open_target(cdp, _PAGE)
    try:
        await _wait_ready(cdp, sid, deadline)
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            probe = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": _worker_expression(provider, content_id),
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": remaining,
                },
                sid,
                timeout=max(5.0, deadline - time.monotonic()),
            )
        except Exception as e:
            logger.warning("Streamcorner resolver: worker evaluation failed: %r", e)
            return None
        raw = (probe.get("result") or {}).get("value")
        if not isinstance(raw, str):
            logger.warning(
                "Streamcorner resolver: worker returned no value: %s", str(probe)[:300]
            )
            return None
        try:
            reply = json.loads(raw)
        except ValueError:
            logger.warning("Streamcorner resolver: unparsable worker reply: %s", raw[:300])
            return None
        streams = ((reply or {}).get("data") or {}).get("streams") or []
        embed = (streams[0] or {}).get("embed_url") if streams else None
        if not embed:
            logger.warning(
                "Streamcorner resolver: no embed_url for %s/%s: %s",
                provider,
                content_id,
                raw[:400],
            )
            return None
    finally:
        await _close_target(cdp, target_id)

    embed_target, embed_sid = await _open_target(cdp, embed)
    try:
        return await _wait_manifest(cdp, embed_sid, deadline)
    finally:
        await _close_target(cdp, embed_target)


async def _wait_ready(cdp, sid: str, deadline: float) -> bool:
    end = min(deadline, time.monotonic() + 10)
    while time.monotonic() < end:
        try:
            probe = await cdp.send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                sid,
                timeout=5,
            )
        except Exception as e:
            logger.debug("Streamcorner resolver: readyState probe failed: %r", e)
        else:
            if (probe.get("result") or {}).get("value") == "complete":
                return True
        await asyncio.sleep(0.2)
    return False


async def _wait_manifest(cdp, sid: str, deadline: float) -> str | None:
    """First ``.m3u8`` requested by the embed page (or its iframes)."""
    clicked = False
    next_click = time.monotonic() + 4
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            ev = await asyncio.wait_for(cdp.events.get(), min(1.0, remaining))
        except asyncio.TimeoutError:
            ev = None
        if ev is not None:
            method = ev.get("method")
            params = ev.get("params", {})
            if method == "Network.requestWillBeSent":
                url = params.get("request", {}).get("url", "")
                if ".m3u8" in url:
                    return url
            elif method == "Target.attachedToTarget":
                info = params.get("targetInfo", {})
                child = params.get("sessionId")
                if child and info.get("type") in ("iframe", "page"):
                    for domain in ("Network.enable", "Page.enable"):
                        try:
                            await cdp.send(domain, {}, child)
                        except Exception as e:
                            logger.debug(
                                "Streamcorner resolver: %s failed on child: %r",
                                domain,
                                e,
                            )
        if not clicked and time.monotonic() >= next_click:
            clicked = True
            # Autoplay policies can hold the player back: click it once.
            for kind in ("mousePressed", "mouseReleased"):
                try:
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": kind,
                            "x": 640,
                            "y": 360,
                            "button": "left",
                            "clickCount": 1,
                        },
                        sid,
                    )
                except Exception as e:
                    logger.debug("Streamcorner resolver: click failed: %r", e)
    return None
