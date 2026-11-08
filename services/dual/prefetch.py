"""Read-ahead for DUAL video segments.

The DUAL video path buffers each upstream segment completely before sending it
(video-only filtering needs whole boxes), so every player request pays the
upstream fetch before the first byte. While segment N is being played, the next
READ_AHEAD segments are fetched through the normal proxy pipeline (loopback
request) and kept in memory, so the player request is answered immediately.
"""
import asyncio
import logging
import time
import urllib.parse
from collections import OrderedDict

import aiohttp

logger = logging.getLogger(__name__)

READ_AHEAD = 2
TTL_SECONDS = 180
MAX_CACHE_BYTES = 384 * 1024 * 1024
MAX_SEQUENCE_ENTRIES = 50000
PREFETCH_HEADER = "X-EP-Prefetch"

_next: dict[str, list[str]] = {}
_cache: "OrderedDict[tuple[str, int], tuple[float, bytes]]" = OrderedDict()
_cache_bytes = 0
_pending: dict[tuple[str, int], asyncio.Event] = {}
_tasks: set[asyncio.Task] = set()


def _segment_key(proxy_url: str) -> str | None:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(proxy_url).query)
    if "dual_video_track" not in query:
        return None
    return (query.get("d") or [None])[0]


def register_playlist(proxy_urls: list[str]) -> None:
    """Remember the segment order of a rewritten VOD DUAL video playlist."""
    keyed = [(key, url) for url in proxy_urls if (key := _segment_key(url))]
    if not keyed:
        return
    if len(_next) + len(keyed) > MAX_SEQUENCE_ENTRIES:
        _next.clear()
    for index, (key, _) in enumerate(keyed):
        _next[key] = [url for _, url in keyed[index + 1:index + 1 + READ_AHEAD]]


def _evict() -> None:
    global _cache_bytes
    now = time.monotonic()
    for key in [key for key, (expires, _) in _cache.items() if expires < now]:
        _cache_bytes -= len(_cache.pop(key)[1])
    while _cache_bytes > MAX_CACHE_BYTES and _cache:
        _, (_, body) = _cache.popitem(last=False)
        _cache_bytes -= len(body)


def store(key: str, track: int, body: bytes) -> None:
    global _cache_bytes
    if not key or len(body) > MAX_CACHE_BYTES // 4:
        return
    old = _cache.pop((key, track), None)
    if old:
        _cache_bytes -= len(old[1])
    _cache[(key, track)] = (time.monotonic() + TTL_SECONDS, body)
    _cache_bytes += len(body)
    _evict()


async def lookup(key: str, track: int, wait: float = 20.0) -> bytes | None:
    """Return a prefetched segment, waiting briefly for an in-flight prefetch."""
    if not key:
        return None
    event = _pending.get((key, track))
    if event is not None:
        try:
            await asyncio.wait_for(event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return None
    entry = _cache.get((key, track))
    if not entry or entry[0] < time.monotonic():
        return None
    _cache.move_to_end((key, track))
    return entry[1]


def schedule_after(key: str, track: int, loopback_base: str) -> None:
    """Prefetch the segments that follow `key` through the local proxy."""
    for url in _next.get(key, []):
        next_key = _segment_key(url)
        if not next_key or (next_key, track) in _pending or (next_key, track) in _cache:
            continue
        parsed = urllib.parse.urlparse(url)
        target = f"{loopback_base}{parsed.path}?{parsed.query}"
        event = asyncio.Event()
        _pending[(next_key, track)] = event
        task = asyncio.create_task(_fetch(target, (next_key, track), event))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _fetch(target: str, pending_key: tuple[str, int], event: asyncio.Event) -> None:
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # The loopback request runs the full proxy pipeline, which stores
            # the filtered segment via store(); the body here is discarded.
            async with session.get(target, headers={PREFETCH_HEADER: "1"}) as response:
                await response.read()
    except Exception as exc:
        logger.debug("DUAL prefetch failed: %s", exc)
    finally:
        _pending.pop(pending_key, None)
        event.set()
