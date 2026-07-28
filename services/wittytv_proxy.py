import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from services.proxy_shared import API_PASSWORD, check_password, logger, web
from utils.drm_decrypter import decrypt_segment


WITTY_ORIGIN = "https://www.wittytv.it"
MEDIASET_ORIGIN = "https://mediasetinfinity.mediaset.it"
LOGIN_URL = "https://api-ott-prod-fe.mediaset.net/PROD/play/idm/anonymous/login/v2.0"
PLAYBACK_URL = "https://api-ott-prod-fe.mediaset.net/PROD/play/playback/check/v2.0"
LICENSE_URL = "https://widevine.entitlement.theplatform.eu/wv/web/ModularDrm/getRawWidevineLicense"
ACCOUNT_URL = "http://access.auth.theplatform.com/data/Account/{account_id}"
GUID_PATTERN = re.compile(r"\b(F[A-Z0-9]{15})\b", re.IGNORECASE)
SESSION_MAX_AGE = int(os.environ.get("WITTY_SESSION_MAX_AGE", 6 * 60 * 60))
SESSION_IDLE_AGE = int(os.environ.get("WITTY_SESSION_IDLE_AGE", 45 * 60))


@dataclass
class WittyPlaybackSession:
    token: str
    cache_key: str
    guid: str
    title: str
    manifest_url: str
    manifest_text: str
    keys: dict[str, str]
    init_by_prefix: dict[str, str]
    created_at: float
    last_access: float
    init_cache: dict[str, bytes] = field(default_factory=dict)

    @property
    def upstream_base(self) -> str:
        return self.manifest_url.rsplit("/", 1)[0] + "/"

    def is_valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (
            now - self.created_at < SESSION_MAX_AGE
            and now - self.last_access < SESSION_IDLE_AGE
        )


def resolve_widevine_wvd_path() -> Path:
    configured = str(
        os.environ.get("WIDEVINE_WVD_PATH")
        or os.environ.get("MEDIASET_WVD_PATH")
        or os.environ.get("WITTY_WVD_PATH")
        or ""
    ).strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        Path("/data/cdm/widevine-device.wvd"),
        Path(__file__).resolve().parents[1] / "data" / "cdm" / "widevine-device.wvd",
        Path(__file__).resolve().parents[1] / "cdm" / "widevine-device.wvd",
        # Legacy local names remain readable during upgrades.
        Path("/data/cdm/wittytv.wvd"),
        Path(__file__).resolve().parents[1] / "data" / "cdm" / "wittytv.wvd",
        Path(__file__).resolve().parents[1] / "cdm" / "wittytv.wvd",
        Path(__file__).resolve().parents[2]
        / "videodl"
        / "videodl"
        / "modules"
        / "cdm"
        / "charlespikachu_wittytv.wvd",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "Widevine CDM device not configured. Set WIDEVINE_WVD_PATH, "
        "MEDIASET_WVD_PATH (or the legacy WITTY_WVD_PATH) to a readable .wvd file "
        "or mount it at /data/cdm/widevine-device.wvd."
    )


def extract_witty_guid(page_text: str) -> str | None:
    patterns = [
        r'guIDcurrentGlobal\s*=\s*["\']([^"\']+)["\']',
        r"programGuid(?:=|%3D)(F[A-Z0-9]{15})",
        r"\b(F[A-Z0-9]{15})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text or "", re.IGNORECASE)
        if match and GUID_PATTERN.fullmatch(match.group(1)):
            return match.group(1).upper()
    return None


def extract_mpd_reference(
    smil_text: str, expected_guid: str = ""
) -> tuple[str, str, str]:
    try:
        root = ET.fromstring(smil_text)
    except ET.ParseError as error:
        raise RuntimeError("Mediaset media selector returned invalid SMIL") from error

    candidates = []
    seen = set()
    for par in root.iter():
        if par.tag.rsplit("}", 1)[-1].lower() != "par":
            continue
        tracking = {}
        for element in par.iter():
            if (
                element.tag.rsplit("}", 1)[-1].lower() == "param"
                and str(element.attrib.get("name", "")).lower() == "trackingdata"
            ):
                tracking = dict(
                    part.split("=", 1)
                    for part in html.unescape(element.attrib.get("value", "")).split("|")
                    if "=" in part
                )
                break

        for element in par.iter():
            if element.tag.rsplit("}", 1)[-1].lower() not in {"ref", "video"}:
                continue
            src = html.unescape(str(element.attrib.get("src", "")))
            if ".mpd" not in src.lower():
                continue
            pid = str(tracking.get("pid") or "")
            aid = str(tracking.get("aid") or "")
            identity = (src, pid, aid)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                duration_ms = int(float(tracking.get("l") or 0))
            except (TypeError, ValueError):
                duration_ms = 0
            value = f"{element.attrib.get('title', '')} {src}".lower()
            quality = (
                3 if "/hd_" in value or " hd" in value
                else 2 if "/hr_" in value or " hr" in value
                else 1 if "/sd_" in value or " sd" in value
                else 0
            )
            candidates.append({
                "src": src,
                "pid": pid,
                "aid": aid,
                "guid_match": bool(
                    expected_guid
                    and str(tracking.get("pgid") or "").upper() == expected_guid.upper()
                ),
                "duration_ms": duration_ms,
                "quality": quality,
            })

    if not candidates:
        raise RuntimeError("Mediaset media selector did not return a DASH manifest")

    selected = max(
        candidates,
        key=lambda item: (
            item["guid_match"],
            item["duration_ms"],
            item["quality"],
        ),
    )
    if not selected["pid"] or not selected["aid"]:
        raise RuntimeError("Mediaset media selector did not return license identifiers")
    return selected["src"], selected["pid"], selected["aid"]


def extract_pssh(manifest_text: str) -> str:
    values = [
        value.strip()
        for value in re.findall(
            r"<(?:[\w.-]+:)?pssh\b[^>]*>([^<]+)</(?:[\w.-]+:)?pssh>",
            manifest_text or "",
            re.IGNORECASE,
        )
        if value.strip()
    ]
    if not values:
        raise RuntimeError("WittyTV DASH manifest does not contain a Widevine PSSH")
    return min(values, key=len)


def extract_init_map(manifest_text: str) -> dict[str, str]:
    result = {}
    for attrs_text in re.findall(
        r"<SegmentTemplate\b([^>]*)>", manifest_text or "", re.IGNORECASE
    ):
        attrs = {
            key: html.unescape(value)
            for key, value in re.findall(
                r'([\w:.-]+)\s*=\s*["\']([^"\']*)["\']', attrs_text
            )
        }
        media = attrs.get("media", "")
        initialization = attrs.get("initialization", "")
        if not media or not initialization:
            continue
        prefix = media.split("$", 1)[0].rsplit("/", 1)[0].rstrip("/")
        result[prefix] = initialization
    if not result:
        raise RuntimeError("WittyTV DASH manifest has no usable initialization templates")
    return result


def rewrite_witty_manifest(manifest_text: str, playback_base: str) -> str:
    manifest = re.sub(
        r"<ContentProtection\b[\s\S]*?</ContentProtection>",
        "",
        manifest_text,
        flags=re.IGNORECASE,
    )
    manifest = re.sub(
        r"<ContentProtection\b[^>]*/>", "", manifest, flags=re.IGNORECASE
    )
    manifest = re.sub(
        r"<(?:[\w.-]+:)?pssh\b[^>]*>[\s\S]*?</(?:[\w.-]+:)?pssh>",
        "",
        manifest,
        flags=re.IGNORECASE,
    )
    manifest = re.sub(
        r"<BaseURL\b[^>]*>[\s\S]*?</BaseURL>",
        "",
        manifest,
        flags=re.IGNORECASE,
    )
    match = re.search(r"<MPD\b[^>]*>", manifest, re.IGNORECASE)
    if not match:
        raise RuntimeError("Invalid WittyTV DASH manifest")
    base_url = html.escape(playback_base.rstrip("/") + "/segment/", quote=False)
    return manifest[: match.end()] + f"\n  <BaseURL>{base_url}</BaseURL>" + manifest[match.end() :]


class WittyTVProxyMixin:
    def _init_witty_sessions(self):
        self._witty_sessions: dict[str, WittyPlaybackSession] = {}
        self._witty_sessions_by_guid: dict[str, str] = {}

    def _cleanup_witty_sessions(self):
        now = time.time()
        expired = [
            token
            for token, session in self._witty_sessions.items()
            if not session.is_valid(now)
        ]
        for token in expired:
            session = self._witty_sessions.pop(token, None)
            if session and self._witty_sessions_by_guid.get(session.cache_key) == token:
                self._witty_sessions_by_guid.pop(session.cache_key, None)

    def _get_witty_session(self, token: str) -> WittyPlaybackSession | None:
        self._cleanup_witty_sessions()
        session = self._witty_sessions.get(token)
        if session:
            session.last_access = time.time()
        return session

    async def handle_witty_status(self, request):
        if not API_PASSWORD:
            return web.json_response(
                {"error": "api_password_not_configured"}, status=503
            )
        if not check_password(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            wvd_path = resolve_widevine_wvd_path()
            return web.json_response({
                "available": True,
                "deviceConfigured": True,
                "devicePath": str(wvd_path),
                "activeSessions": len(self._witty_sessions),
            })
        except Exception as error:
            return web.json_response({
                "available": False,
                "deviceConfigured": False,
                "error": str(error),
                "activeSessions": len(self._witty_sessions),
            })

    async def handle_witty_create_session(self, request):
        if not API_PASSWORD:
            return web.json_response(
                {"error": "api_password_not_configured"}, status=503
            )
        if not check_password(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
            if len(raw) > 32 * 1024:
                return web.json_response({"error": "request_too_large"}, status=413)
            payload = json.loads(raw.decode("utf-8") or "{}")
            page_url = str(payload.get("pageUrl") or "").strip()
            guid = str(payload.get("guid") or "").strip().upper()
            title = str(payload.get("title") or "").strip()[:500]
            account_token = str(payload.get("beToken") or "").strip()
            account_token = re.sub(r"^Bearer\s+", "", account_token, flags=re.IGNORECASE)

            if page_url:
                parsed_page = urlparse(page_url)
                if (
                    parsed_page.scheme not in {"http", "https"}
                    or parsed_page.hostname not in {
                        "wittytv.it",
                        "www.wittytv.it",
                        "mediasetinfinity.mediaset.it",
                        "www.mediasetinfinity.mediaset.it",
                    }
                ):
                    return web.json_response({"error": "invalid_mediaset_url"}, status=400)
            if guid and not GUID_PATTERN.fullmatch(guid):
                return web.json_response({"error": "invalid_guid"}, status=400)
            if len(account_token) > 16 * 1024:
                return web.json_response({"error": "invalid_betoken"}, status=400)
            if not page_url and not guid:
                return web.json_response({"error": "pageUrl_or_guid_required"}, status=400)

            if guid:
                cache_key = self._witty_cache_key(guid, account_token)
                existing_token = self._witty_sessions_by_guid.get(cache_key)
                existing = (
                    self._get_witty_session(existing_token) if existing_token else None
                )
                if existing:
                    return self._witty_session_response(request, existing)

            resolved = await self._resolve_witty_playback(
                page_url, guid, title, account_token
            )
            token = secrets.token_urlsafe(32)
            now = time.time()
            cache_key = self._witty_cache_key(resolved["guid"], account_token)
            session = WittyPlaybackSession(
                token=token,
                cache_key=cache_key,
                guid=resolved["guid"],
                title=resolved["title"],
                manifest_url=resolved["manifest_url"],
                manifest_text=resolved["manifest_text"],
                keys=resolved["keys"],
                init_by_prefix=extract_init_map(resolved["manifest_text"]),
                created_at=now,
                last_access=now,
            )
            self._witty_sessions[token] = session
            self._witty_sessions_by_guid[session.cache_key] = token
            logger.info(
                "Mediaset playback session created for %s with %d content key(s)",
                session.guid,
                len(session.keys),
            )
            return self._witty_session_response(request, session)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        except Exception as error:
            logger.exception("Mediaset session creation failed")
            return web.json_response(
                {"error": "mediaset_session_failed", "message": str(error)}, status=502
            )

    def _witty_session_response(self, request, session: WittyPlaybackSession):
        base = self._public_request_base(request)
        route_prefix = "mediaset" if request.path.startswith("/mediaset/") else "witty"
        return web.json_response({
            "url": f"{base}/{route_prefix}/play/{session.token}/manifest.mpd",
            "guid": session.guid,
            "title": session.title,
            "expiresIn": min(SESSION_MAX_AGE, SESSION_IDLE_AGE),
        })

    async def handle_witty_manifest(self, request):
        session = self._get_witty_session(request.match_info.get("token", ""))
        if not session:
            return web.Response(text="Mediaset playback session expired", status=404)
        base = self._public_request_base(request)
        route_prefix = "mediaset" if request.path.startswith("/mediaset/") else "witty"
        playback_base = f"{base}/{route_prefix}/play/{session.token}"
        manifest = rewrite_witty_manifest(session.manifest_text, playback_base)
        return web.Response(
            text=manifest,
            content_type="application/dash+xml",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "private, max-age=30",
            },
        )

    async def handle_witty_segment(self, request):
        session = self._get_witty_session(request.match_info.get("token", ""))
        if not session:
            return web.Response(text="Mediaset playback session expired", status=404)
        tail = request.match_info.get("tail", "")
        if (
            not tail
            or tail.startswith("/")
            or "\\" in tail
            or any(part == ".." for part in tail.split("/"))
        ):
            return web.Response(text="Invalid segment path", status=400)

        upstream_url = urljoin(session.upstream_base, tail)
        parsed_manifest = urlparse(session.manifest_url)
        parsed_upstream = urlparse(upstream_url)
        if (
            parsed_upstream.scheme != parsed_manifest.scheme
            or parsed_upstream.netloc != parsed_manifest.netloc
            or not parsed_upstream.path.startswith(
                parsed_manifest.path.rsplit("/", 1)[0] + "/"
            )
        ):
            return web.Response(text="Invalid segment destination", status=400)

        try:
            content = await self._fetch_witty_bytes(upstream_url)
            init_path = self._witty_init_for_tail(session, tail)
            kid_list = ",".join(session.keys.keys())
            key_list = ",".join(session.keys.values())
            if tail in session.init_by_prefix.values() or "init" in tail.lower():
                decrypted = await asyncio.to_thread(
                    decrypt_segment, content, b"", kid_list, key_list, False
                )
            else:
                if not init_path:
                    raise RuntimeError(f"No initialization segment mapped for {tail}")
                init_content = session.init_cache.get(init_path)
                if init_content is None:
                    init_content = await self._fetch_witty_bytes(
                        urljoin(session.upstream_base, init_path)
                    )
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
            logger.exception("Mediaset segment processing failed for %s", tail)
            return web.Response(
                text=f"Mediaset segment processing failed: {error}", status=502
            )

    @staticmethod
    def _witty_init_for_tail(session: WittyPlaybackSession, tail: str) -> str | None:
        matches = [
            (prefix, init_path)
            for prefix, init_path in session.init_by_prefix.items()
            if not prefix or tail == prefix or tail.startswith(prefix + "/")
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]

    @staticmethod
    def _public_request_base(request) -> str:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}".rstrip("/")

    async def _fetch_witty_bytes(self, url: str) -> bytes:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Origin": MEDIASET_ORIGIN,
                "Referer": f"{MEDIASET_ORIGIN}/",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status not in {200, 206}:
                raise RuntimeError(f"Upstream segment returned HTTP {response.status}")
            return await response.read()

    async def _resolve_witty_playback(
        self, page_url: str, guid: str, title: str, account_token: str = ""
    ) -> dict:
        wvd_path = resolve_widevine_wvd_path()
        if not guid:
            page_origin = (
                WITTY_ORIGIN
                if urlparse(page_url).hostname in {"wittytv.it", "www.wittytv.it"}
                else MEDIASET_ORIGIN
            )
            page_text = await self._witty_get_text(
                page_url,
                headers={"Referer": f"{page_origin}/"},
            )
            guid = extract_witty_guid(page_text) or ""
        if not GUID_PATTERN.fullmatch(guid):
            raise RuntimeError("Unable to extract a valid Mediaset GUID")

        bearer = account_token
        if not bearer:
            login = await self._witty_json_request(
                "POST",
                LOGIN_URL,
                json_body={
                    "client_id": str(uuid.uuid4()),
                    "appName": "embed//mediasetplay-embed",
                },
            )
            bearer = str(login.get("response", {}).get("beToken") or "")
        if not bearer:
            raise RuntimeError("Mediaset anonymous login did not return a beToken")

        playback = await self._witty_json_request(
            "POST",
            PLAYBACK_URL,
            headers={"Authorization": f"Bearer {bearer}"},
            json_body={"contentId": guid, "streamType": "VOD"},
        )
        playback_error = playback.get("error") or {}
        if playback_error:
            raise RuntimeError(
                f"{playback_error.get('code') or 'PLAYBACK'}: "
                f"{playback_error.get('message') or 'content unavailable'}"
            )
        selector = playback.get("response", {}).get("mediaSelector") or {}
        selector_url = str(selector.get("url") or "")
        if not selector_url:
            raise RuntimeError("Mediaset playback did not return a media selector")

        params = {
            "format": "SMIL",
            "auth": bearer,
            "formats": "MPEG4,M3U,MPEG-DASH",
            "assetTypes": (
                "HD,browser,widevine,geoIT|geoNo:"
                "HR,browser,widevine,geoIT|geoNo:"
                "SD,browser,widevine,geoIT|geoNo"
            ),
            "balance": "true",
            "auto": "true",
            "tracking": "true",
            "delivery": "Streaming",
        }
        if selector.get("publicUrl"):
            params["publicUrl"] = selector["publicUrl"]
        separator = "&" if "?" in selector_url else "?"
        smil_text = await self._witty_get_text(
            selector_url + separator + urllib.parse.urlencode(params),
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Origin": MEDIASET_ORIGIN,
                "Referer": f"{MEDIASET_ORIGIN}/",
            },
        )
        manifest_url, release_pid, account_id = extract_mpd_reference(
            smil_text, expected_guid=guid
        )
        manifest_text = await self._witty_get_text(
            manifest_url,
            headers={"Origin": MEDIASET_ORIGIN, "Referer": f"{MEDIASET_ORIGIN}/"},
        )
        pssh = extract_pssh(manifest_text)
        keys = await self._request_witty_keys(
            wvd_path,
            pssh,
            release_pid,
            account_id,
            bearer,
        )
        if not keys:
            raise RuntimeError("Mediaset license did not contain content keys")
        return {
            "guid": guid,
            "title": title or guid,
            "manifest_url": manifest_url,
            "manifest_text": manifest_text,
            "keys": keys,
        }

    @staticmethod
    def _witty_cache_key(guid: str, account_token: str) -> str:
        identity = (
            hashlib.sha256(account_token.encode("utf-8")).hexdigest()[:20]
            if account_token
            else "anonymous"
        )
        return f"{guid}:{identity}"

    async def _witty_get_text(self, url: str, headers: dict | None = None) -> str:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", **(headers or {})},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(f"{urlparse(url).hostname} returned HTTP {response.status}")
            return text

    async def _witty_json_request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        session = await self._get_session(url=url)
        async with session.request(
            method,
            url,
            headers={"User-Agent": "Mozilla/5.0", **(headers or {})},
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.read()
            if response.status != 200:
                raise RuntimeError(f"{urlparse(url).hostname} returned HTTP {response.status}")
            try:
                return json.loads(body)
            except Exception as error:
                raise RuntimeError("Mediaset returned invalid JSON") from error

    async def _request_witty_keys(
        self,
        wvd_path: Path,
        pssh_value: str,
        release_pid: str,
        account_id: str,
        bearer: str,
    ) -> dict[str, str]:
        try:
            from pywidevine import Cdm, Device, PSSH
        except ImportError as error:
            raise RuntimeError(
                "pywidevine is not installed; install EasyProxy requirements again"
            ) from error

        device = Device.load(wvd_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh_value))
            http_session = await self._get_session(url=LICENSE_URL)
            async with http_session.post(
                LICENSE_URL,
                params={
                    "releasePid": release_pid,
                    "account": ACCOUNT_URL.format(account_id=account_id),
                    "schema": "1.0",
                    "token": bearer,
                },
                data=challenge,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Origin": MEDIASET_ORIGIN,
                    "Referer": f"{MEDIASET_ORIGIN}/",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                license_body = await response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"WittyTV license server returned HTTP {response.status}"
                    )
            cdm.parse_license(session_id, license_body)
            return {
                key.kid.hex.replace("-", "").lower(): key.key.hex().lower()
                for key in cdm.get_keys(session_id)
                if "CONTENT" in str(key.type)
            }
        finally:
            cdm.close(session_id)
