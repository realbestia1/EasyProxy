"""Read-ahead for DUAL video segments.

The DUAL video path buffers each upstream segment completely before sending it
(video-only filtering needs whole boxes), so every player request pays the
upstream fetch before the first byte. While segment N is being played, the next
READ_AHEAD segments are fetched through the normal proxy pipeline (loopback
request) and kept in memory, so the player request is answered immediately.
The cache exists so the read-ahead fetch is reused by the player request instead of downloading and filtering the same segment twice.
"""
import asyncio
import logging
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

READ_AHEAD = 2
TTL_SECONDS = 60
MAX_SEQUENCE_ENTRIES = 4096
OVERSIZE_SECONDS = 60.0
PER_STREAM_ENTRIES = READ_AHEAD + 1
PREFETCH_HEADER = "X-EP-Prefetch"


def _total_ram_bytes() -> int:
    # cgroup limit first: psutil reports the host RAM inside containers.
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < (1 << 60):
            return int(raw)
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return 0


def _budget_bytes() -> int:
    total = _total_ram_bytes()
    if not total:
        return 64 * 1024 * 1024
    return max(32 * 1024 * 1024, int(total * 0.04))


MAX_CACHE_BYTES = _budget_bytes()

_next: dict[str, list[str]] = {}
_cache: "OrderedDict[tuple[str, int], tuple[float, bytes, str]]" = OrderedDict()
_cache_bytes = 0
_pending: dict[tuple[str, int], asyncio.Event] = {}
_tasks: set[asyncio.Task] = set()
_oversize_until: dict[int, float] = {}
_stream_entries: dict[str, int] = {}
_stream_bytes: dict[str, int] = {}
_stream_last_evicted: dict[str, float] = {}


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
        while len(_next) + len(keyed) > MAX_SEQUENCE_ENTRIES and _next:
            _next.pop(next(iter(_next)))
    for index, (key, _) in enumerate(keyed):
        _next[key] = [url for _, url in keyed[index + 1:index + 1 + READ_AHEAD]]


def _account(stream: str, body_len: int, delta: int) -> None:
    """Move one entry of `stream` in/out of the per-stream counters."""
    entries = _stream_entries.get(stream, 0) + delta
    if entries > 0:
        _stream_entries[stream] = entries
    else:
        _stream_entries.pop(stream, None)
    stream_bytes = _stream_bytes.get(stream, 0) + body_len * delta
    if stream_bytes > 0:
        _stream_bytes[stream] = stream_bytes
    else:
        _stream_bytes.pop(stream, None)


def _drop_oldest(stream: str) -> bool:
    """Remove the oldest cached segment of `stream`; False when it has none."""
    global _cache_bytes
    for key, (_, body, owner) in _cache.items():
        if owner == stream:
            del _cache[key]
            _cache_bytes -= len(body)
            _account(stream, len(body), -1)
            _stream_last_evicted[stream] = time.monotonic()
            return True
    return False


def evict() -> None:
    global _cache_bytes
    now = time.monotonic()
    for key in [key for key, (expires, _, _) in _cache.items() if expires < now]:
        _, body, stream = _cache.pop(key)
        _cache_bytes -= len(body)
        _account(stream, len(body), -1)
    for track in [track for track, until in _oversize_until.items() if until < now]:
        del _oversize_until[track]
    while len(_oversize_until) > 256:
        del _oversize_until[next(iter(_oversize_until))]
    for stream in [s for s, count in _stream_entries.items() if count > PER_STREAM_ENTRIES]:
        while _stream_entries.get(stream, 0) > PER_STREAM_ENTRIES:
            _drop_oldest(stream)
    while _cache_bytes > MAX_CACHE_BYTES and _cache:
        # Shared global budget: the budget is counted in bytes, so trim the
        # heaviest stream first (not the one with the most entries, which would
        # punish small segments). On equal bytes the least recently evicted
        # stream rotates in, so no stream stays favoured forever.
        if not _stream_entries:
            break
        stream = max(
            _stream_entries,
            key=lambda s: (_stream_bytes.get(s, 0), -_stream_last_evicted.get(s, 0.0)),
        )
        if not _drop_oldest(stream):
            break
    for stream in [s for s in _stream_last_evicted if s not in _stream_entries]:
        del _stream_last_evicted[stream]
    while len(_stream_last_evicted) > 256:
        del _stream_last_evicted[next(iter(_stream_last_evicted))]


def stats() -> dict:
    return {
        "entries": len(_cache),
        "bytes": max(0, _cache_bytes),
        "budget_bytes": MAX_CACHE_BYTES,
        "sequences": len(_next),
        "pending": len(_pending),
        "oversize_tracks": len(_oversize_until),
        "streams": len(_stream_entries),
        "per_stream_entries": PER_STREAM_ENTRIES,
    }


def _track_oversize(track: int) -> bool:
    until = _oversize_until.get(track)
    return until is not None and until > time.monotonic()


def _prefetch_has_room(stream: str) -> bool:
    """True when the stream's fair share of the budget fits two segments."""
    stream = stream or "shared"
    entries = _stream_entries.get(stream, 0)
    if entries <= 0:
        return True
    quota = MAX_CACHE_BYTES // max(1, len(_stream_entries))
    avg_size = _stream_bytes.get(stream, 0) / entries
    return quota >= 2 * avg_size


def store(key: str, track: int, body: bytes, stream: str = "") -> None:
    global _cache_bytes
    if not key:
        return
    if len(body) > MAX_CACHE_BYTES // 2:
        _oversize_until[track] = time.monotonic() + OVERSIZE_SECONDS
        return
    stream = stream or "shared"
    old = _cache.pop((key, track), None)
    if old:
        _cache_bytes -= len(old[1])
        _account(old[2], len(old[1]), -1)
    _cache[(key, track)] = (time.monotonic() + TTL_SECONDS, body, stream)
    _cache_bytes += len(body)
    _account(stream, len(body), 1)
    evict()


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


def schedule_after(key: str, track: int, loopback_base: str, stream: str = "") -> None:
    """Prefetch the segments that follow `key` through the local proxy."""
    if _track_oversize(track) or not _prefetch_has_room(stream):
        return
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
