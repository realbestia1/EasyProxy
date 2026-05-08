"""Background uprot pre-warmer.

Periodically resolves a configured list of uprot URLs and stores the
resulting maxstream URLs in `uprot_url_cache`. Live requests for those
same URLs then complete in <100ms (cache hit) instead of going through
the full captcha-solve pipeline (~5-10s and ~80% AI rate per attempt).

Pattern mirrored from NelloStream `_handleScheduledUprotRefresh` in
workers/cfworker.js.

Activation (all opt-in via env):
  UPROT_WARM_ENABLED=1                         # master switch
  UPROT_WARM_INTERVAL_SECONDS=1800             # default 30 min
  UPROT_WARM_URLS=<json>                       # see format below
  UPROT_WARM_URLS_FILE=/path/to/list.json      # alternative

`UPROT_WARM_URLS` JSON shape (list of objects):
  [
    {"url": "https://uprot.net/msf/abc123"},
    {"url": "https://uprot.net/msfld/def456", "season": 1, "episode": 5},
    {"url": "https://uprot.net/msfld/def456", "season": 1, "episode": 6}
  ]

We deliberately don't try to enumerate folder episodes automatically —
the caller knows what content matters and we'd rather warm a curated
set than hammer uprot with N requests per folder.
"""

import asyncio
import json
import logging
import os

from extractors.maxstream import MaxstreamExtractor
from services import uprot_url_cache

logger = logging.getLogger(__name__)


def _load_targets() -> list[dict]:
    """Parse UPROT_WARM_URLS env or UPROT_WARM_URLS_FILE into a target list."""
    raw_env = (os.getenv("UPROT_WARM_URLS") or "").strip()
    if raw_env:
        try:
            data = json.loads(raw_env)
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict) and t.get("url")]
        except Exception as e:
            logger.warning(f"UPROT_WARM_URLS parse failed: {e}")

    path = (os.getenv("UPROT_WARM_URLS_FILE") or "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict) and t.get("url")]
        except Exception as e:
            logger.warning(f"UPROT_WARM_URLS_FILE parse failed: {e}")

    return []


async def _warm_one(target: dict) -> bool:
    """Resolve one target uprot URL and write the result into the cache."""
    url = target.get("url")
    season = target.get("season")
    episode = target.get("episode")
    if not url:
        return False

    try:
        ext = MaxstreamExtractor(request_headers={})
        # Rebuild the standard extract() flow without the caller-side
        # cache check (we want to FORCE a fresh resolve here).
        maxstream_url = await ext.get_uprot(url, season=season, episode=episode)
        try:
            maxstream_url = await ext._follow_uprots_chain(maxstream_url)
        except Exception:
            pass
        if maxstream_url:
            uprot_url_cache.put(url, maxstream_url, season=season, episode=episode)
            logger.info(f"[uprot-warmer] cached {url[:80]} S{season}E{episode} → {maxstream_url[:80]}")
            return True
        return False
    except Exception as e:
        logger.warning(f"[uprot-warmer] failed for {url[:80]} S{season}E{episode}: {e}")
        return False
    finally:
        try:
            await ext.close()
        except Exception:
            pass


async def run() -> None:
    """Long-running warmer loop. Spawn from on_startup as an asyncio task."""
    if (os.getenv("UPROT_WARM_ENABLED") or "").strip() not in ("1", "true", "yes", "on"):
        logger.info("[uprot-warmer] disabled (set UPROT_WARM_ENABLED=1 to enable)")
        return

    interval = int(os.getenv("UPROT_WARM_INTERVAL_SECONDS", "1800"))
    interval = max(60, interval)  # don't hammer uprot — minimum 1 min

    targets = _load_targets()
    if not targets:
        logger.warning("[uprot-warmer] no UPROT_WARM_URLS configured — disabling loop")
        return

    logger.info(f"[uprot-warmer] starting: {len(targets)} target(s), interval={interval}s")

    while True:
        try:
            ok = 0
            for t in targets:
                if await _warm_one(t):
                    ok += 1
                # Back-to-back GETs against uprot get rate-limited; space
                # them out so the warmer is courteous.
                await asyncio.sleep(8)
            logger.info(f"[uprot-warmer] pass complete: {ok}/{len(targets)} ok")
        except Exception as e:
            logger.error(f"[uprot-warmer] pass crashed: {e}")
        await asyncio.sleep(interval)
