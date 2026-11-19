"""Video-source subtitles for normal (non-DUAL) extractor runs.

VidFast and Cinejoy return the subtitle files of their video source. Without
DUAL they were dropped: the proxied master was the source master unchanged, so
the player never saw them. The entries are registered on the DUAL external
subtitle store and advertised as TYPE=SUBTITLES renditions injected into the
rewritten master by ``ManifestRewriter``.
"""

from __future__ import annotations

import time
import urllib.parse

from aiohttp import web

from services.dual.http_client import AssetFetchError
from services.dual.routing import RoutingOptions
from services.dual.subtitles import SubtitleStore
from services.proxy_shared import check_password, get_public_base_url

MAX_ENTRIES = 80
STREAM_TTL_SECONDS = 6 * 3600
MAX_STREAMS = 512

store = SubtitleStore()
_streams: dict[str, tuple[float, list[dict]]] = {}


def _prune(now: float) -> None:
    for key, (stamp, _) in list(_streams.items()):
        if now - stamp > STREAM_TTL_SECONDS:
            _streams.pop(key, None)
    while len(_streams) > MAX_STREAMS:
        oldest = min(_streams, key=lambda item: _streams[item][0])
        _streams.pop(oldest, None)


def remember(
    stream_key: str,
    entries,
    headers: dict | None,
    warp_off: bool,
    proxy_off: bool,
    forced_proxy: str | None,
    extractor_name: str,
) -> list[dict]:
    """Register the extractor subtitle files for one stream key."""
    if not stream_key or not isinstance(entries, list):
        return []
    routing = RoutingOptions(
        warp_off=bool(warp_off),
        proxy_off=bool(proxy_off),
        forced_proxy=forced_proxy or None,
        extractor_name=str(extractor_name or ""),
    )
    items: list[dict] = []
    seen_languages: set[str] = set()
    seen_urls: set[str] = set()
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        language = str(entry.get("language") or "").strip()[:16]
        if language and language.lower() in seen_languages:
            continue
        name = str(
            entry.get("display") or entry.get("name") or language or ""
        ).strip()[:80]
        try:
            sid = store.register_external(url, language, name, headers, routing)
        except (ValueError, OSError):
            continue
        seen_urls.add(url)
        if language:
            seen_languages.add(language.lower())
        items.append(
            {
                "sid": sid,
                "name": name or language,
                "language": language,
                "default": False,
                "autoselect": True,
                "forced": False,
            }
        )
    if not items:
        return []
    now = time.monotonic()
    _streams[stream_key] = (now, items)
    _prune(now)
    store.cleanup_idle()
    return items


def lookup(stream_key: str | None) -> list[dict] | None:
    """Items registered for this stream key, None once the TTL expired."""
    if not stream_key:
        return None
    found = _streams.get(stream_key)
    if found is None:
        return None
    stamp, items = found
    if time.monotonic() - stamp > STREAM_TTL_SECONDS:
        _streams.pop(stream_key, None)
        return None
    return items


async def subtitle_playlist(request: web.Request) -> web.Response:
    if not check_password(request):
        return web.Response(status=401, text="Unauthorized")
    sid = request.match_info["sid"]
    try:
        metadata = store.metadata(sid)
        if metadata.get("external"):
            metadata = await store.ensure_loaded(sid)
        timeline = store.timeline(metadata, 0.0, 1.0)
        if not timeline:
            raise ValueError("empty subtitle timeline")
        api_password = request.query.get("api_password")
        query = (
            f"?api_password={urllib.parse.quote(api_password, safe='')}"
            if api_password
            else ""
        )
        has_init = bool(metadata.get("init"))
        lines = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{7 if has_init else 3}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        base = get_public_base_url(request)
        if has_init:
            lines.append(f'#EXT-X-MAP:URI="{base}/subs/{sid}/init.mp4{query}"')
        for item in timeline:
            lines += [
                f"#EXTINF:{item['duration']:.6f},",
                f"{base}/subs/{sid}/s{item['idx']}.vtt{query}",
            ]
        lines.append("#EXT-X-ENDLIST")
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )
    except FileNotFoundError:
        return web.Response(status=410, text="Subtitle track expired")
    except AssetFetchError as exc:
        return web.json_response(
            {"code": "SUBTITLE_NETWORK_ERROR", "message": str(exc)},
            status=exc.status,
        )
    except ValueError as exc:
        return web.Response(status=404, text=str(exc))


async def subtitle_segment(request: web.Request) -> web.Response:
    if not check_password(request):
        return web.Response(status=401, text="Unauthorized")
    sid = request.match_info["sid"]
    index = int(request.match_info["idx"])
    try:
        data, media_type = await store.payload(sid, index, 0.0, 1.0)
        return web.Response(
            body=data,
            headers={
                "Content-Type": media_type,
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache",
                "Accept-Ranges": "bytes",
            },
        )
    except FileNotFoundError:
        return web.Response(status=410, text="Subtitle track expired")
    except AssetFetchError as exc:
        return web.json_response(
            {"code": "SUBTITLE_NETWORK_ERROR", "message": str(exc)},
            status=exc.status,
        )
    except ValueError as exc:
        return web.Response(status=404, text=str(exc))


def install(app: web.Application) -> None:
    app.router.add_get("/subs/{sid}/subtitles.m3u8", subtitle_playlist)
    app.router.add_get(r"/subs/{sid}/s{idx:\d+}.vtt", subtitle_segment)


__all__ = ["install", "lookup", "remember", "store"]
