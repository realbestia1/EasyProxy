"""In-process DUAL audio, offset and synchronisation service.

The audio, offset and synchronisation engines are framework-independent. This
module exposes them as routes on EasyProxy's main aiohttp application.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

from .audio import AudioStore
from .http_client import AssetFetchError
from .offsets import RemoteOffsetStore
from .routing import as_payload, from_values
from .security import SessionManager, request_token, resolves_publicly
from .subtitles import SubtitleStore
from .sync import SyncEngine
import config as _config_mod
from config import check_password


logger = logging.getLogger("easyproxy.dual")
SESSION_TTL = 21600
BACKGROUND_CLEANUP_INTERVAL = 5
API_PASSWORD = os.environ.get("API_PASSWORD", "").strip()


def _effective_api_password(request: web.Request) -> str:
    return (
        request.query.get("api_password")
        or getattr(_config_mod, "API_PASSWORD", None)
        or os.environ.get("API_PASSWORD", "").strip()
        or API_PASSWORD
    )


sessions = SessionManager(SESSION_TTL)
audio = None
offsets = None
sync_engine = None
subtitles = None


def configure_cache(cache_dir: str | Path) -> None:
    global audio, offsets, sync_engine, subtitles
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    audio = AudioStore(str(cache_path / "audio"))
    offsets = RemoteOffsetStore()
    sync_engine = SyncEngine(audio, offsets)
    subtitles = SubtitleStore()


class DualServiceError(Exception):
    def __init__(self, status: int, detail: str | dict):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise DualServiceError(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise DualServiceError(400, "JSON body must be an object")
    return body


def _query_int(request: web.Request, name: str, default: int) -> int:
    value = request.query.get(name, str(default))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DualServiceError(422, f"invalid query parameter: {name}") from exc


def _require_session(request: web.Request, body: dict | None = None) -> str:
    token = request_token(request, body)
    if not sessions.valid(token):
        raise DualServiceError(401, "DUAL session required")
    return token


def _base_url(request: web.Request) -> str:
    # EasyProxy supplies these headers when forwarding a public request. This
    # Reconstruct the public URL from the headers added by EasyProxy.
    cf_visitor = request.headers.get("CF-Visitor", "").lower()
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",", 1)[0].strip().lower()
    if '"scheme"' in cf_visitor and "https" in cf_visitor:
        scheme = "https"
    if scheme not in {"http", "https"}:
        scheme = request.scheme
    host = request.headers.get("X-Forwarded-Host", request.host).split(",", 1)[0].strip()
    prefix = request.headers.get("X-Forwarded-Prefix", "").split(",", 1)[0].strip().rstrip("/")
    return f"{scheme}://{host}{prefix}"


def _audio_url(request: web.Request, hid: str, token: str, offset: float = 0.0, rate: float = 1.0) -> str:
    values = {
        "o": int(round(offset * 1000)),
        "r": int(round(rate * 1_000_000_000)),
        "t": token,
    }
    api_password = _effective_api_password(request)
    if api_password:
        values["api_password"] = api_password
    query = urlencode(values)
    return f"{_base_url(request)}/dual/aud/{hid}/audio.m3u8?{query}"


def _subtitle_url(request: web.Request, sid: str, token: str) -> str:
    values = {"t": token}
    api_password = _effective_api_password(request)
    if api_password:
        values["api_password"] = api_password
    return f"{_base_url(request)}/dual/sub/{sid}/subtitles.m3u8?{urlencode(values)}"


def _asset_query(request: web.Request, offset_ms: int, rate_nano: int, token: str) -> str:
    values = {"o": offset_ms, "r": rate_nano, "t": token}
    api_password = _effective_api_password(request)
    if api_password:
        values["api_password"] = api_password
    return urlencode(values)


def _audio_response(data: bytes, media_type: str, cache_control: str = "no-store") -> web.Response:
    return web.Response(
        body=data,
        headers={
            "Content-Type": media_type,
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": cache_control,
            "Accept-Ranges": "bytes",
        },
    )


async def health(request: web.Request) -> web.Response:
    return _json({"status": "ok", "service": "easyproxy-dual", "public_url": None})


async def create_session(request: web.Request) -> web.Response:
    return _json(await create_session_data())


async def create_session_data() -> dict:
    sessions.cleanup()
    token, expires_at = sessions.issue()
    return {"token": token, "expires_at": expires_at, "ttl_seconds": sessions.ttl_seconds}


async def prepare_audio_data(body: dict, request: web.Request) -> dict:
    token = str(body.get("token") or "")
    if not sessions.valid(token):
        raise DualServiceError(401, "DUAL session required")
    routing = from_values(request.query, body)
    try:
        hid = await audio.register(
            playlist=str(body.get("playlist") or ""),
            key_b64=str(body.get("key") or ""),
            media_key=str(body.get("mediaKey") or ""),
            language=str(body.get("lang") or ""),
            base_url=str(body.get("baseUrl") or ""),
            headers=body.get("headers") if isinstance(body.get("headers"), dict) else {},
            routing=routing,
        )
        metadata = audio.metadata(hid)
        for url in (metadata["segs"][0], metadata["segs"][-1]):
            if not await resolves_publicly(url):
                raise ValueError("audio source does not resolve publicly")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise DualServiceError(400, str(exc)) from exc
    metadata = audio.metadata(hid)
    return {
        "hid": hid,
        "url": _audio_url(request, hid, token),
        "language": str(body.get("lang") or "").lower(),
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
        "routing": as_payload(routing),
    }


async def prepare_audio(request: web.Request) -> web.Response:
    body = await _json_body(request)
    return _json(await prepare_audio_data(body, request))




async def cache_status(request: web.Request) -> web.Response:
    """Return remote cache state for Toast Stream's manifest labels.

    Sending the dual descriptor (``d=``, the same one the manifest call uses)
    makes the answer exact: the video/audio identity is resolved and looked up,
    so ``cached`` means the stored measurement is really reusable.  Without it
    the check stays video-level (any stored audio) and reports ``exact=false``.
    """
    body = await _json_body(request)
    descriptor = str(request.query.get("d") or body.get("d") or "").strip()
    proxy = request.app.get("proxy") if request.app is not None else None
    if descriptor and proxy is not None:
        result = await proxy.dual_cache_status({**body, "d": descriptor})
        return _json({
            "offset": {
                "cached": bool(result.get("cached")),
                "status": result.get("status"),
                "updated_at": result.get("updated_at"),
            },
            "exact": bool(result.get("exact")),
        })

    media_key = str(body.get("mediaKey") or body.get("media_key") or "")
    try:
        resolution = int(body.get("resolution") or 0)
    except (TypeError, ValueError) as exc:
        raise DualServiceError(400, "invalid resolution") from exc
    video_fingerprint = str(
        body.get("videoFingerprint")
        or body.get("video_fingerprint")
        or ""
    )
    if not media_key or resolution <= 0 or not video_fingerprint:
        raise DualServiceError(400, "mediaKey, resolution and videoFingerprint required")

    offset = await offsets.cache_status(body)
    return _json({
        "offset": {
            "cached": bool(offset and offset.get("status") == "ok"),
            "status": offset.get("status") if offset else None,
            "updated_at": offset.get("updated_at") if offset else None,
        },
        "exact": False,
    })


async def audio_playlist(request: web.Request) -> web.Response:
    hid = request.match_info["hid"]
    token = _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        metadata = audio.metadata(hid)
        offset, rate = offset_ms / 1000.0, rate_nano / 1_000_000_000
        timeline = audio.timeline(metadata, offset, rate)
        if not timeline:
            raise ValueError("empty audio timeline")
        audio.prefetch(hid, [item["idx"] for item in timeline[:3]], offset, rate)
        base = _base_url(request)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f'#EXT-X-MAP:URI="{base}/dual/aud/{hid}/init.mp4?{_asset_query(request, offset_ms, rate_nano, token)}"',
        ]
        for item in timeline:
            query = _asset_query(request, offset_ms, rate_nano, token)
            lines += [
                f"#EXTINF:{item['duration']:.6f},",
                f"{base}/dual/aud/{hid}/s{item['idx']}.m4s?{query}",
            ]
        lines.append("#EXT-X-ENDLIST")
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )
    except FileNotFoundError as exc:
        raise DualServiceError(410, "audio session expired") from exc
    except ValueError as exc:
        raise DualServiceError(404, str(exc)) from exc


async def audio_init(request: web.Request) -> web.Response:
    hid = request.match_info["hid"]
    _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        metadata = audio.metadata(hid)
        timeline = audio.timeline(metadata, offset_ms / 1000.0, rate_nano / 1_000_000_000)
        if not timeline:
            raise ValueError("empty audio timeline")
        init_data, _ = await audio.fragment_bytes(hid, timeline[0]["idx"], offset_ms / 1000.0, rate_nano / 1_000_000_000)
        return _audio_response(init_data, "video/mp4")
    except FileNotFoundError as exc:
        raise DualServiceError(410, "audio session expired") from exc
    except asyncio.TimeoutError as exc:
        raise DualServiceError(504, {
            "code": "AUDIO_NETWORK_TIMEOUT",
            "message": "audio upstream request timed out",
        }) from exc
    except AssetFetchError as exc:
        raise DualServiceError(exc.status, {
            "code": "AUDIO_NETWORK_ERROR",
            "message": str(exc),
        }) from exc
    except (ValueError, RuntimeError) as exc:
        raise DualServiceError(404, str(exc)) from exc


async def audio_segment(request: web.Request) -> web.Response:
    hid = request.match_info["hid"]
    index = int(request.match_info["idx"])
    _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        _, fragment_data = await audio.fragment_bytes(hid, index, offset_ms / 1000.0, rate_nano / 1_000_000_000)
        # Keep the next fragments warm so playback never waits for ffmpeg.
        audio.prefetch(hid, [index + 1, index + 2, index + 3], offset_ms / 1000.0, rate_nano / 1_000_000_000)
        return _audio_response(fragment_data, "video/iso.segment")
    except FileNotFoundError as exc:
        raise DualServiceError(410, "audio session expired") from exc
    except asyncio.TimeoutError as exc:
        raise DualServiceError(504, {
            "code": "AUDIO_NETWORK_TIMEOUT",
            "message": "audio upstream request timed out",
        }) from exc
    except AssetFetchError as exc:
        raise DualServiceError(exc.status, {
            "code": "AUDIO_NETWORK_ERROR",
            "message": str(exc),
        }) from exc
    except (ValueError, RuntimeError) as exc:
        raise DualServiceError(404, str(exc)) from exc


async def prepare_subtitle_data(body: dict, request: web.Request) -> dict:
    """Register one audio-source subtitle rendition for shifted playback."""
    token = str(body.get("token") or "")
    if not sessions.valid(token):
        raise DualServiceError(401, "DUAL session required")
    routing = from_values(request.query, body)
    try:
        sid = await subtitles.register(
            playlist=str(body.get("playlist") or ""),
            key_b64=str(body.get("key") or ""),
            language=str(body.get("language") or ""),
            name=str(body.get("name") or ""),
            base_url=str(body.get("baseUrl") or ""),
            headers=body.get("headers") if isinstance(body.get("headers"), dict) else {},
            routing=routing,
        )
        metadata = subtitles.metadata(sid)
        for url in (metadata["segs"][0], metadata["segs"][-1]):
            if not await resolves_publicly(url):
                raise ValueError("subtitle source does not resolve publicly")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise DualServiceError(400, str(exc)) from exc
    return {
        "sid": sid,
        "url": _subtitle_url(request, sid, token),
        "language": str(body.get("language") or ""),
        "name": str(body.get("name") or ""),
        "routing": as_payload(routing),
    }


async def subtitle_playlist(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    token = _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        metadata = subtitles.metadata(sid)
        offset, rate = offset_ms / 1000.0, rate_nano / 1_000_000_000
        timeline = subtitles.timeline(metadata, offset, rate)
        if not timeline:
            raise ValueError("empty subtitle timeline")
        base = _base_url(request)
        query = _asset_query(request, offset_ms, rate_nano, token)
        has_init = bool(metadata.get("init"))
        lines = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{7 if has_init else 3}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        if has_init:
            lines.append(f'#EXT-X-MAP:URI="{base}/dual/sub/{sid}/init.mp4?{query}"')
        for item in timeline:
            lines += [
                f"#EXTINF:{item['duration']:.6f},",
                f"{base}/dual/sub/{sid}/s{item['idx']}.vtt?{query}",
            ]
        lines.append("#EXT-X-ENDLIST")
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )
    except FileNotFoundError as exc:
        raise DualServiceError(410, "subtitle session expired") from exc
    except ValueError as exc:
        raise DualServiceError(404, str(exc)) from exc


async def subtitle_init(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    _require_session(request)
    try:
        return _audio_response(await subtitles.init_bytes(sid), "video/mp4")
    except FileNotFoundError as exc:
        raise DualServiceError(410, "subtitle session expired") from exc
    except asyncio.TimeoutError as exc:
        raise DualServiceError(504, {
            "code": "SUBTITLE_NETWORK_TIMEOUT",
            "message": "subtitle upstream request timed out",
        }) from exc
    except AssetFetchError as exc:
        raise DualServiceError(exc.status, {
            "code": "SUBTITLE_NETWORK_ERROR",
            "message": str(exc),
        }) from exc
    except (ValueError, RuntimeError) as exc:
        raise DualServiceError(404, str(exc)) from exc


async def subtitle_segment(request: web.Request) -> web.Response:
    sid = request.match_info["sid"]
    index = int(request.match_info["idx"])
    _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        data, media_type = await subtitles.payload(
            sid, index, offset_ms / 1000.0, rate_nano / 1_000_000_000
        )
        return _audio_response(data, media_type)
    except FileNotFoundError as exc:
        raise DualServiceError(410, "subtitle session expired") from exc
    except asyncio.TimeoutError as exc:
        raise DualServiceError(504, {
            "code": "SUBTITLE_NETWORK_TIMEOUT",
            "message": "subtitle upstream request timed out",
        }) from exc
    except AssetFetchError as exc:
        raise DualServiceError(exc.status, {
            "code": "SUBTITLE_NETWORK_ERROR",
            "message": str(exc),
        }) from exc
    except (ValueError, RuntimeError) as exc:
        raise DualServiceError(404, str(exc)) from exc



async def sync_audio(request: web.Request) -> web.Response:
    body = await _json_body(request)
    return _json(await sync_audio_data(body, request))


async def sync_audio_data(body: dict, request: web.Request) -> dict:
    token = str(body.get("token") or "")
    if not sessions.valid(token):
        raise DualServiceError(401, "DUAL session required")
    body = dict(body)
    body["_routing"] = as_payload(from_values(request.query, body))
    try:
        result = await sync_engine.measure(body)
    except FileNotFoundError as exc:
        raise DualServiceError(410, {
            "code": "AUDIO_SESSION_EXPIRED",
            "message": str(exc) or "audio session expired",
        }) from exc
    except (ValueError, RuntimeError) as exc:
        message = str(exc) or "audio sync failed"
        lowered = message.lower()
        if "no audio track" in lowered:
            code = "SYNC_NO_REFERENCE_AUDIO"
        elif "busy" in lowered:
            code = "SYNC_BUSY"
        else:
            code = "SYNC_NETWORK"
        raise DualServiceError(422, {"code": code, "message": message}) from exc
    await offsets.report(body, result)
    return result


async def _cleanup_loop(app: web.Application) -> None:
    """Release inactive playback state even when no new session is created."""
    try:
        while True:
            await asyncio.sleep(BACKGROUND_CLEANUP_INTERVAL)
            sessions.cleanup()
            audio.cleanup_idle()
            subtitles.cleanup_idle()
    except asyncio.CancelledError:
        raise


async def _start_cleanup(app: web.Application) -> None:
    app["cleanup_task"] = asyncio.create_task(_cleanup_loop(app))


async def _stop_cleanup(app: web.Application) -> None:
    task = app.get("cleanup_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if offsets is not None:
        if hasattr(offsets, "aclose"):
            await offsets.aclose()
        else:
            offsets.close()


def _is_dual_path(path: str) -> bool:
    return path.startswith("/dual/")


@web.middleware
async def dual_middleware(request: web.Request, handler) -> web.StreamResponse:
    if not _is_dual_path(request.path):
        return await handler(request)
    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=200)
    elif not check_password(request):
        response = _json({"detail": "Unauthorized: Invalid API Password"}, status=401)
    else:
        try:
            response = await handler(request)
        except DualServiceError as exc:
            response = _json({"detail": exc.detail}, status=exc.status)
        except web.HTTPException as exc:
            response = _json({"detail": exc.reason}, status=exc.status)
        except Exception:
            logger.exception("Unhandled DUAL request error")
            response = _json({"detail": "internal DUAL error"}, status=500)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


def memory_stats() -> dict:
    """Return in-process DUAL memory and active-track information."""
    try:
        import psutil

        process = psutil.Process(os.getpid())
        rss_bytes = process.memory_info().rss
    except (ImportError, OSError):
        rss_bytes = 0
    tracks = list(getattr(audio, "_tracks", {}).values()) if audio is not None else []
    return {
        "status": "running",
        "mode": "in_process",
        "pid": os.getpid(),
        "rss_bytes": rss_bytes,
        "rss_mb": round(rss_bytes / (1024 * 1024), 2),
        "audio_tracks": len(tracks),
        "pinned_audio_tracks": len(getattr(audio, "_pinned", {})) if audio is not None else 0,
        "subtitle_tracks": len(getattr(subtitles, "_tracks", {})) if subtitles is not None else 0,
        "audio_cache": "memory_only",
        "offset_cache": "mongodb_shared",
    }


async def handle_memory(request: web.Request) -> web.Response:
    if not check_password(request):
        return _json({"detail": "Unauthorized: Invalid API Password"}, status=401)
    return _json(memory_stats())


def install(app: web.Application, cache_dir: str | Path) -> None:
    """Install DUAL routes and lifecycle hooks into EasyProxy's main app."""
    configure_cache(cache_dir)
    app.on_startup.append(_start_cleanup)
    app.on_cleanup.append(_stop_cleanup)

    # The old Sidecar API (/session, /sync, /offset/* and the generic
    # /dual/{tail:.*} forwarder) is gone. The manifest endpoint calls the
    # service in-process; only its generated audio URLs remain public.
    app.router.add_get("/dual/aud/{hid}/audio.m3u8", audio_playlist)
    app.router.add_get("/dual/aud/{hid}/init.mp4", audio_init)
    app.router.add_get("/dual/aud/{hid}/s{idx:\\d+}.m4s", audio_segment)
    app.router.add_get("/dual/sub/{sid}/subtitles.m3u8", subtitle_playlist)
    app.router.add_get("/dual/sub/{sid}/init.mp4", subtitle_init)
    app.router.add_get("/dual/sub/{sid}/s{idx:\\d+}.vtt", subtitle_segment)
    # Single public cache check retained for Toast Stream's offset indicator.
    app.router.add_post("/dual/cache/status", cache_status)


__all__ = [
    "DualServiceError",
    "audio",
    "cache_status",
    "configure_cache",
    "create_session",
    "create_session_data",
    "dual_middleware",
    "handle_memory",
    "install",
    "memory_stats",
    "prepare_audio",
    "prepare_audio_data",
    "prepare_subtitle_data",
    "subtitles",
    "sync_audio",
    "sync_audio_data",
]
