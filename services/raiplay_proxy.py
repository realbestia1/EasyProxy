import asyncio
import json
import os
import re
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from services.proxy_shared import API_PASSWORD, check_password, logger, web
from services.wittytv_proxy import (
    extract_pssh,
    resolve_widevine_wvd_path,
    rewrite_witty_manifest,
)
from utils.drm_decrypter import decrypt_segment


RAIPLAY_ORIGIN = "https://www.raiplay.it"
RELINKER_URL = "https://mediapolisvod.rai.it/relinker/relinkerServlet.htm"
CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")
SESSION_MAX_AGE = int(os.environ.get("RAIPLAY_SESSION_MAX_AGE", 6 * 60 * 60))
SESSION_IDLE_AGE = int(os.environ.get("RAIPLAY_SESSION_IDLE_AGE", 45 * 60))


@dataclass
class RaiPlaybackSession:
    token: str
    content_id: str
    title: str
    manifest_url: str
    manifest_text: str
    keys: dict[str, str]
    init_by_representation: dict[str, str]
    created_at: float
    last_access: float
    init_cache: dict[str, bytes] = field(default_factory=dict)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (
            now - self.created_at < SESSION_MAX_AGE
            and now - self.last_access < SESSION_IDLE_AGE
        )


def extract_rai_init_map(manifest_text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError as error:
        raise RuntimeError("RaiPlay DASH manifest is invalid") from error

    result = {}
    for adaptation in root.iter():
        if adaptation.tag.rsplit("}", 1)[-1] != "AdaptationSet":
            continue
        template = next(
            (
                child
                for child in adaptation
                if child.tag.rsplit("}", 1)[-1] == "SegmentTemplate"
            ),
            None,
        )
        if template is None:
            continue
        initialization = str(template.attrib.get("initialization") or "")
        if not initialization:
            continue
        for representation in adaptation:
            if representation.tag.rsplit("}", 1)[-1] != "Representation":
                continue
            representation_id = str(representation.attrib.get("id") or "")
            if representation_id:
                result[representation_id] = initialization.replace(
                    "$RepresentationID$", representation_id
                )
    if not result:
        raise RuntimeError("RaiPlay DASH manifest has no usable initialization templates")
    return result


class RaiPlayProxyMixin:
    def _init_raiplay_sessions(self):
        self._raiplay_sessions: dict[str, RaiPlaybackSession] = {}
        self._raiplay_sessions_by_content: dict[str, str] = {}

    def _cleanup_raiplay_sessions(self):
        now = time.time()
        expired = [
            token
            for token, session in self._raiplay_sessions.items()
            if not session.is_valid(now)
        ]
        for token in expired:
            session = self._raiplay_sessions.pop(token, None)
            if (
                session
                and self._raiplay_sessions_by_content.get(session.content_id) == token
            ):
                self._raiplay_sessions_by_content.pop(session.content_id, None)

    def _get_raiplay_session(self, token: str) -> RaiPlaybackSession | None:
        self._cleanup_raiplay_sessions()
        session = self._raiplay_sessions.get(token)
        if session:
            session.last_access = time.time()
        return session

    async def handle_raiplay_status(self, request):
        if not API_PASSWORD:
            return web.json_response({"error": "api_password_not_configured"}, status=503)
        if not check_password(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            wvd_path = resolve_widevine_wvd_path()
            return web.json_response({
                "available": True,
                "deviceConfigured": True,
                "devicePath": str(wvd_path),
                "activeSessions": len(self._raiplay_sessions),
            })
        except Exception as error:
            return web.json_response({
                "available": False,
                "deviceConfigured": False,
                "error": str(error),
                "activeSessions": len(self._raiplay_sessions),
            })

    async def handle_raiplay_create_session(self, request):
        if not API_PASSWORD:
            return web.json_response({"error": "api_password_not_configured"}, status=503)
        if not check_password(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
            if len(raw) > 16 * 1024:
                return web.json_response({"error": "request_too_large"}, status=413)
            payload = json.loads(raw.decode("utf-8") or "{}")
            content_id = str(payload.get("contentId") or "").strip()
            title = str(payload.get("title") or content_id).strip()[:500]
            if not CONTENT_ID_PATTERN.fullmatch(content_id):
                return web.json_response({"error": "invalid_content_id"}, status=400)

            existing_token = self._raiplay_sessions_by_content.get(content_id)
            existing = (
                self._get_raiplay_session(existing_token) if existing_token else None
            )
            if existing:
                return self._raiplay_session_response(request, existing)

            resolved = await self._resolve_raiplay_playback(content_id)
            if not resolved["license_url"]:
                return web.json_response(
                    {"error": "raiplay_content_is_clear", "message": "Use the direct HLS/DASH URL"},
                    status=409,
                )

            keys = await self._request_raiplay_keys(
                resolve_widevine_wvd_path(),
                extract_pssh(resolved["manifest_text"]),
                resolved["license_url"],
            )
            if not keys:
                raise RuntimeError("RaiPlay license did not contain content keys")

            token = secrets.token_urlsafe(32)
            now = time.time()
            session = RaiPlaybackSession(
                token=token,
                content_id=content_id,
                title=title,
                manifest_url=resolved["manifest_url"],
                manifest_text=resolved["manifest_text"],
                keys=keys,
                init_by_representation=extract_rai_init_map(resolved["manifest_text"]),
                created_at=now,
                last_access=now,
            )
            self._raiplay_sessions[token] = session
            self._raiplay_sessions_by_content[content_id] = token
            logger.info(
                "RaiPlay playback session created for %s with %d content key(s)",
                content_id,
                len(keys),
            )
            return self._raiplay_session_response(request, session)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        except Exception as error:
            logger.exception("RaiPlay session creation failed")
            return web.json_response(
                {"error": "raiplay_session_failed", "message": str(error)}, status=502
            )

    def _raiplay_session_response(self, request, session: RaiPlaybackSession):
        base = self._public_request_base(request)
        return web.json_response({
            "url": f"{base}/raiplay/play/{session.token}/manifest.mpd",
            "contentId": session.content_id,
            "title": session.title,
            "expiresIn": min(SESSION_MAX_AGE, SESSION_IDLE_AGE),
        })

    async def handle_raiplay_manifest(self, request):
        session = self._get_raiplay_session(request.match_info.get("token", ""))
        if not session:
            return web.Response(text="RaiPlay playback session expired", status=404)
        base = self._public_request_base(request)
        playback_base = f"{base}/raiplay/play/{session.token}"
        return web.Response(
            text=rewrite_witty_manifest(session.manifest_text, playback_base),
            content_type="application/dash+xml",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "private, max-age=30",
            },
        )

    async def handle_raiplay_segment(self, request):
        session = self._get_raiplay_session(request.match_info.get("token", ""))
        if not session:
            return web.Response(text="RaiPlay playback session expired", status=404)
        tail = request.match_info.get("tail", "")
        if (
            not tail
            or tail.startswith("/")
            or "\\" in tail
            or any(part == ".." for part in tail.split("/"))
        ):
            return web.Response(text="Invalid segment path", status=400)
        try:
            content = await self._fetch_raiplay_segment(session, tail)
            init_path = self._raiplay_init_for_tail(session, tail)
            kid_list = ",".join(session.keys.keys())
            key_list = ",".join(session.keys.values())
            if tail in session.init_by_representation.values() or "init" in tail.lower():
                decrypted = await asyncio.to_thread(
                    decrypt_segment, content, b"", kid_list, key_list, False
                )
            else:
                if not init_path:
                    raise RuntimeError(f"No initialization segment mapped for {tail}")
                init_content = session.init_cache.get(init_path)
                if init_content is None:
                    init_content = await self._fetch_raiplay_segment(session, init_path)
                    session.init_cache[init_path] = init_content
                decrypted = await asyncio.to_thread(
                    decrypt_segment,
                    init_content,
                    content,
                    kid_list,
                    key_list,
                    True,
                )
            return web.Response(
                body=decrypted,
                content_type="video/mp4",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "private, max-age=3600",
                    "Accept-Ranges": "none",
                },
            )
        except Exception as error:
            logger.exception("RaiPlay segment processing failed for %s", tail)
            return web.Response(
                text=f"RaiPlay segment processing failed: {error}", status=502
            )

    @staticmethod
    def _raiplay_init_for_tail(
        session: RaiPlaybackSession, tail: str
    ) -> str | None:
        matches = [
            (representation, init_path)
            for representation, init_path in session.init_by_representation.items()
            if representation in tail
        ]
        return max(matches, key=lambda item: len(item[0]))[1] if matches else None

    async def _fetch_raiplay_segment(
        self, session: RaiPlaybackSession, tail: str
    ) -> bytes:
        for attempt in range(2):
            manifest_url = session.manifest_url
            url = self._raiplay_segment_url(manifest_url, tail)
            http_session = await self._get_session(url=url)
            async with http_session.get(
                url,
                headers=self._raiplay_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status in {200, 206}:
                    return await response.read()
                if response.status not in {401, 403} or attempt:
                    raise RuntimeError(
                        f"RaiPlay upstream segment returned HTTP {response.status}"
                    )
            await self._refresh_raiplay_stream(session, manifest_url)
        raise RuntimeError("RaiPlay segment refresh failed")

    async def _refresh_raiplay_stream(
        self, session: RaiPlaybackSession, stale_manifest_url: str
    ):
        async with session.refresh_lock:
            if session.manifest_url != stale_manifest_url:
                return
            resolved = await self._resolve_raiplay_playback(session.content_id)
            session.manifest_url = resolved["manifest_url"]
            session.manifest_text = resolved["manifest_text"]
            session.init_by_representation = extract_rai_init_map(
                resolved["manifest_text"]
            )
            session.last_access = time.time()

    async def _resolve_raiplay_playback(self, content_id: str) -> dict:
        data = await self._raiplay_json_request(
            RELINKER_URL,
            params={"cont": content_id, "output": "62"},
        )
        manifest_url = str((data.get("video") or [""])[0] or "")
        self._validate_raiplay_media_url(manifest_url)
        if ".mpd" not in urlparse(manifest_url).path.lower():
            return {
                "manifest_url": manifest_url,
                "manifest_text": "",
                "license_url": "",
            }
        manifest_text = await self._raiplay_get_text(manifest_url)
        licenses = (
            (data.get("licence_server_map") or {}).get("drmLicenseUrlValues") or []
        )
        widevine = next(
            (
                item
                for item in licenses
                if str(item.get("drm") or "").upper() == "WIDEVINE"
            ),
            None,
        )
        return {
            "manifest_url": manifest_url,
            "manifest_text": manifest_text,
            "license_url": str((widevine or {}).get("licenceUrl") or ""),
        }

    async def _raiplay_json_request(self, url: str, params: dict) -> dict:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            params=params,
            headers=self._raiplay_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"{urlparse(url).hostname} returned HTTP {response.status}"
                )
            try:
                return json.loads(body.decode("latin-1"))
            except Exception as error:
                raise RuntimeError("RaiPlay relinker returned invalid JSON") from error

    async def _raiplay_get_text(self, url: str) -> str:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            headers=self._raiplay_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"{urlparse(url).hostname} returned HTTP {response.status}"
                )
            return text

    async def _request_raiplay_keys(
        self, wvd_path: Path, pssh_value: str, license_url: str
    ) -> dict[str, str]:
        try:
            from pywidevine import Cdm, Device, PSSH
        except ImportError as error:
            raise RuntimeError(
                "pywidevine is not installed; install EasyProxy requirements again"
            ) from error

        parsed_license = urlparse(license_url)
        authorization = urllib.parse.parse_qs(parsed_license.query).get(
            "Authorization", [""]
        )[0]
        license_host = str(parsed_license.hostname or "").lower()
        if (
            parsed_license.scheme != "https"
            or not authorization
            or not (
                license_host.endswith(".nagra.com")
                or license_host.endswith(".rai.it")
            )
        ):
            raise RuntimeError("RaiPlay returned an invalid Widevine license URL")
        base_license_url = parsed_license._replace(query="", fragment="").geturl()

        device = Device.load(wvd_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh_value))
            http_session = await self._get_session(url=base_license_url)
            async with http_session.post(
                base_license_url,
                data=challenge,
                headers={
                    **self._raiplay_headers(),
                    "Content-Type": "application/octet-stream",
                    "nv-authorizations": authorization,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                license_body = await response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"RaiPlay license server returned HTTP {response.status}"
                    )
            cdm.parse_license(session_id, license_body)
            return {
                key.kid.hex.replace("-", "").lower(): key.key.hex().lower()
                for key in cdm.get_keys(session_id)
                if "CONTENT" in str(key.type)
            }
        finally:
            cdm.close(session_id)

    @staticmethod
    def _raiplay_headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142 Safari/537.36"
            ),
            "Origin": RAIPLAY_ORIGIN,
            "Referer": f"{RAIPLAY_ORIGIN}/",
        }

    @staticmethod
    def _raiplay_segment_url(manifest_url: str, tail: str) -> str:
        joined = urljoin(manifest_url, tail)
        manifest_query = urlparse(manifest_url).query
        parsed = urlparse(joined)
        if manifest_query and not parsed.query:
            joined = parsed._replace(query=manifest_query).geturl()
        RaiPlayProxyMixin._validate_raiplay_media_url(joined)
        return joined

    @staticmethod
    def _validate_raiplay_media_url(value: str):
        parsed = urlparse(value)
        hostname = str(parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not hostname
            or not (
                hostname.endswith(".rai.it")
                or hostname.endswith(".akamaized.net")
                or hostname.endswith(".msvdn.net")
            )
        ):
            raise RuntimeError("RaiPlay returned an unsupported media URL")

    @staticmethod
    def _public_request_base(request) -> str:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}".rstrip("/")
