"""Persistent URL cache for resolved uprot → maxstream URLs.

Once we've paid the captcha cost to resolve a uprot URL, we know the
final maxstream HLS playlist URL. uprot tokens are stable for ~22h, so
caching the (uprot_url, season, episode) → maxstream_url mapping turns
subsequent live requests into a sub-second cache lookup.

Storage: single JSON file (default `/tmp/uprot_url_cache.json`).
Concurrency: single-process append-only — fine for the typical
EasyProxy deployment (one container = one event loop). For multi-worker
gunicorn setups, point CACHE_PATH at a tmpfs file or upgrade to Redis.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_PATH = os.getenv("UPROT_URL_CACHE_PATH", "/tmp/uprot_url_cache.json")
DEFAULT_TTL = int(os.getenv("UPROT_URL_CACHE_TTL", str(22 * 3600)))


def _key(uprot_url: str, season=None, episode=None) -> str:
    s = "" if season is None else str(season)
    e = "" if episode is None else str(episode)
    return f"{uprot_url}|{s}|{e}"


def _load() -> dict:
    try:
        if not os.path.exists(CACHE_PATH):
            return {}
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.debug(f"uprot_url_cache load failed: {e}")
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        logger.debug(f"uprot_url_cache save failed: {e}")


def get(uprot_url: str, season=None, episode=None, ttl: int = DEFAULT_TTL) -> Optional[str]:
    """Return cached maxstream URL for the given (uprot_url, season, episode), or None.

    Entries older than `ttl` seconds are treated as missing.
    """
    data = _load()
    entry = data.get(_key(uprot_url, season, episode))
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > ttl:
        return None
    url = entry.get("maxstream_url")
    return url if isinstance(url, str) and url else None


def put(uprot_url: str, maxstream_url: str, season=None, episode=None) -> None:
    """Persist a resolved (uprot_url, season, episode) → maxstream_url mapping."""
    if not maxstream_url:
        return
    data = _load()
    data[_key(uprot_url, season, episode)] = {
        "maxstream_url": maxstream_url,
        "ts": int(time.time()),
    }
    _save(data)


def stats() -> dict:
    """Diagnostic helper: total entries + how many are still fresh."""
    data = _load()
    now = time.time()
    fresh = sum(1 for e in data.values() if now - e.get("ts", 0) <= DEFAULT_TTL)
    return {"total": len(data), "fresh": fresh, "path": CACHE_PATH}
