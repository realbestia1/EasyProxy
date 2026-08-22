"""Aiohttp server for the embedded Toastflix audio sidecar.

The audio, offset and synchronisation engines are framework-independent. This
module only exposes them over HTTP, using the aiohttp stack already required by
EasyProxy.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

from .audio import AudioStore
from .offsets import OffsetStore
from .security import SessionManager, request_token, resolves_publicly
from .sync import SyncEngine


logger = logging.getLogger("toastflix_sidecar")
APP_DIR = Path(__file__).resolve().parent
DOCKER_CACHE_DIR = Path("/data/recordings/sidecar_data")
DEFAULT_CACHE_DIR = DOCKER_CACHE_DIR if Path("/data").is_dir() else APP_DIR / "data"
SESSION_TTL = 21600

sessions = SessionManager(SESSION_TTL)
audio = None
offsets = None
sync_engine = None


def _configure_cache(cache_dir: str | Path) -> None:
    global audio, offsets, sync_engine
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    audio = AudioStore(str(cache_path / "audio"))
    offsets = OffsetStore(str(cache_path / "offsets.db"))
    sync_engine = SyncEngine(audio, offsets)


_configure_cache(DEFAULT_CACHE_DIR)


class SidecarError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SidecarError(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise SidecarError(400, "JSON body must be an object")
    return body


def _query_int(request: web.Request, name: str, default: int) -> int:
    value = request.query.get(name, str(default))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SidecarError(422, f"invalid query parameter: {name}") from exc


def _require_session(request: web.Request, body: dict | None = None) -> str:
    token = request_token(request, body)
    if not sessions.valid(token):
        raise SidecarError(401, "sidecar session required")
    return token


def _base_url(request: web.Request) -> str:
    # EasyProxy supplies these headers when forwarding a public request. This
    # Reconstruct the public URL from the headers added by EasyProxy.
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",", 1)[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",", 1)[0].strip()
    prefix = request.headers.get("X-Forwarded-Prefix", "").split(",", 1)[0].strip().rstrip("/")
    return f"{scheme}://{host}{prefix}"


def _audio_url(request: web.Request, hid: str, token: str, offset: float = 0.0, rate: float = 1.0) -> str:
    query = urlencode({"o": int(round(offset * 1000)), "r": int(round(rate * 1_000_000_000)), "t": token})
    return f"{_base_url(request)}/dual/aud/{hid}/audio.m3u8?{query}"


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
    return _json({"status": "ok", "service": "toast-audio-sidecar", "public_url": None})


async def create_session(request: web.Request) -> web.Response:
    sessions.cleanup()
    token, expires_at = sessions.issue()
    return _json({"token": token, "expires_at": expires_at, "ttl_seconds": sessions.ttl_seconds})


async def prepare_audio(request: web.Request) -> web.Response:
    body = await _json_body(request)
    token = _require_session(request, body)
    try:
        hid = await audio.register(
            playlist=str(body.get("playlist") or ""),
            key_b64=str(body.get("key") or ""),
            media_key=str(body.get("mediaKey") or ""),
            language=str(body.get("lang") or ""),
            base_url=str(body.get("baseUrl") or ""),
            headers=body.get("headers") if isinstance(body.get("headers"), dict) else {},
        )
        metadata = audio.metadata(hid)
        for url in (metadata["segs"][0], metadata["segs"][-1]):
            if not await resolves_publicly(url):
                raise ValueError("audio source does not resolve publicly")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise SidecarError(400, str(exc)) from exc
    metadata = audio.metadata(hid)
    return _json({
        "hid": hid,
        "url": _audio_url(request, hid, token),
        "language": str(body.get("lang") or "").lower(),
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
    })


async def cached_audio(request: web.Request) -> web.Response:
    body = await _json_body(request)
    token = _require_session(request, body)
    hid = audio.find_cached(str(body.get("mediaKey") or ""), str(body.get("lang") or "").lower())
    if not hid:
        raise SidecarError(404, "valid cached audio track not found")
    metadata = audio.metadata(hid)
    return _json({
        "url": _audio_url(request, hid, token),
        "cached": True,
        "hid": hid,
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
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
        base = _base_url(request)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f'#EXT-X-MAP:URI="{base}/dual/aud/{hid}/init.mp4?{urlencode({"o": offset_ms, "r": rate_nano, "t": token})}"',
        ]
        for item in timeline:
            query = urlencode({"o": offset_ms, "r": rate_nano, "t": token})
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
    except (ValueError, FileNotFoundError) as exc:
        raise SidecarError(404, str(exc)) from exc


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
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise SidecarError(404, str(exc)) from exc


async def audio_segment(request: web.Request) -> web.Response:
    hid = request.match_info["hid"]
    index = int(request.match_info["idx"])
    _require_session(request)
    offset_ms = _query_int(request, "o", 0)
    rate_nano = _query_int(request, "r", 1_000_000_000)
    try:
        _, fragment_data = await audio.fragment_bytes(hid, index, offset_ms / 1000.0, rate_nano / 1_000_000_000)
        return _audio_response(fragment_data, "video/iso.segment")
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise SidecarError(404, str(exc)) from exc


async def offset_lookup(request: web.Request) -> web.Response:
    body = await _json_body(request)
    _require_session(request, body)
    result = await offsets.lookup(body)
    return _json({"found": bool(result), "offset": result})


async def offset_report(request: web.Request) -> web.Response:
    body = await _json_body(request)
    _require_session(request, body)
    result = body.get("offset")
    if not isinstance(result, dict):
        raise SidecarError(400, "offset result required")
    await offsets.report(body, result)
    return _json({"ok": True})


async def sync_audio(request: web.Request) -> web.Response:
    body = await _json_body(request)
    _require_session(request, body)
    try:
        result = await sync_engine.measure(body)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise SidecarError(422, str(exc)) from exc
    await offsets.report(body, result)
    return _json(result)


@web.middleware
async def sidecar_middleware(request: web.Request, handler) -> web.StreamResponse:
    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except SidecarError as exc:
            response = _json({"detail": exc.detail}, status=exc.status)
        except web.HTTPException as exc:
            response = _json({"detail": exc.reason}, status=exc.status)
        except Exception:
            logger.exception("Unhandled sidecar request error")
            response = _json({"detail": "internal sidecar error"}, status=500)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


app = web.Application(middlewares=[sidecar_middleware])
app.router.add_get("/health", health)
app.router.add_post("/session", create_session)
app.router.add_post("/dual/aprep", prepare_audio)
app.router.add_post("/dual/acache", cached_audio)
app.router.add_get("/dual/aud/{hid}/audio.m3u8", audio_playlist)
app.router.add_get("/dual/aud/{hid}/init.mp4", audio_init)
app.router.add_get(r"/dual/aud/{hid}/s{idx:\d+}.m4s", audio_segment)
app.router.add_post("/offset/lookup", offset_lookup)
app.router.add_post("/offset/report", offset_report)
app.router.add_post("/sync", sync_audio)


def main() -> None:
    parser = argparse.ArgumentParser(description="Toast Audio Sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3107)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    args = parser.parse_args()
    _configure_cache(args.cache_dir)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        access_log=logging.getLogger("aiohttp.access"),
    )


if __name__ == "__main__":
    main()
