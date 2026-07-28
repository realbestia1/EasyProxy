import html
import json
import re
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import aiohttp
import config
from extractors.base import BaseExtractor, ExtractorError
from extractors.widevine import extract_widevine_pssh, resolve_widevine_device


WITTY_ORIGIN = "https://www.wittytv.it"
MEDIASET_ORIGIN = "https://mediasetinfinity.mediaset.it"
LOGIN_URL = "https://api-ott-prod-fe.mediaset.net/PROD/play/idm/anonymous/login/v2.0"
PLAYBACK_URL = "https://api-ott-prod-fe.mediaset.net/PROD/play/playback/check/v2.0"
LICENSE_URL = (
    "https://widevine.entitlement.theplatform.eu/wv/web/ModularDrm/"
    "getRawWidevineLicense"
)
ACCOUNT_URL = "http://access.auth.theplatform.com/data/Account/{account_id}"
GUID_PATTERN = re.compile(r"\b(F[A-Z0-9]{15})\b", re.IGNORECASE)
_ALLOWED_HOSTS = {
    "wittytv.it",
    "www.wittytv.it",
    "mediasetinfinity.mediaset.it",
    "www.mediasetinfinity.mediaset.it",
}


def _extract_guid(page_text: str) -> str | None:
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


def _extract_mpd_reference(
    smil_text: str, expected_guid: str
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
                    for part in html.unescape(
                        element.attrib.get("value", "")
                    ).split("|")
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
                3
                if "/hd_" in value or " hd" in value
                else 2
                if "/hr_" in value or " hr" in value
                else 1
                if "/sd_" in value or " sd" in value
                else 0
            )
            candidates.append(
                {
                    "src": src,
                    "pid": pid,
                    "aid": aid,
                    "guid_match": (
                        str(tracking.get("pgid") or "").upper()
                        == expected_guid.upper()
                    ),
                    "duration_ms": duration_ms,
                    "quality": quality,
                }
            )
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
        raise RuntimeError(
            "Mediaset media selector did not return license identifiers"
        )
    return selected["src"], selected["pid"], selected["aid"]


class MediasetExtractor(BaseExtractor):
    """Extract protected Mediaset Infinity and WittyTV VOD streams."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(
            request_headers, proxies=proxies, extractor_name="mediaset"
        )
        self.mediaflow_endpoint = "mpd_manifest_proxy"

    async def extract(self, url: str, **kwargs) -> dict:
        if not config.API_PASSWORD:
            raise ExtractorError(
                "API_PASSWORD must be configured for protected Mediaset extraction"
            )
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _ALLOWED_HOSTS
        ):
            raise ExtractorError("Unsupported Mediaset or WittyTV URL")
        try:
            resolved = await self._resolve_playback(url)
        except Exception as error:
            raise ExtractorError(f"Mediaset extraction failed: {error}") from error

        clearkey = ",".join(
            f"{kid}:{key}" for kid, key in resolved["keys"].items()
        )
        return {
            "destination_url": resolved["manifest_url"],
            "request_headers": self._media_headers(),
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "captured_manifest": resolved["manifest_text"],
            "query_params": {"clearkey": clearkey},
        }

    async def _resolve_playback(self, page_url: str) -> dict:
        page_origin = (
            WITTY_ORIGIN
            if urlparse(page_url).hostname in {"wittytv.it", "www.wittytv.it"}
            else MEDIASET_ORIGIN
        )
        page_text = await self._get_text(
            page_url, {"Referer": f"{page_origin}/"}
        )
        guid = _extract_guid(page_text) or ""
        if not GUID_PATTERN.fullmatch(guid):
            raise RuntimeError("Unable to extract a valid Mediaset GUID")

        login = await self._json_request(
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

        playback = await self._json_request(
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
        smil_text = await self._get_text(
            selector_url + separator + urllib.parse.urlencode(params),
            {
                "Accept": "application/json,text/plain,*/*",
                **self._media_headers(),
            },
        )
        manifest_url, release_pid, account_id = _extract_mpd_reference(
            smil_text, guid
        )
        manifest_text = await self._get_text(
            manifest_url, self._media_headers()
        )
        keys = await self._request_keys(
            extract_widevine_pssh(manifest_text),
            release_pid,
            account_id,
            bearer,
        )
        if not keys:
            raise RuntimeError("Mediaset license did not contain content keys")
        return {
            "manifest_url": manifest_url,
            "manifest_text": manifest_text,
            "keys": keys,
        }

    async def _get_text(self, url: str, headers: dict | None = None) -> str:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            headers={"User-Agent": self.base_headers["User-Agent"], **(headers or {})},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"{urlparse(url).hostname} returned HTTP {response.status}"
                )
            return text

    async def _json_request(
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
            headers={"User-Agent": self.base_headers["User-Agent"], **(headers or {})},
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"{urlparse(url).hostname} returned HTTP {response.status}"
                )
            try:
                return json.loads(body)
            except Exception as error:
                raise RuntimeError("Mediaset returned invalid JSON") from error

    async def _request_keys(
        self,
        pssh_value: str,
        release_pid: str,
        account_id: str,
        bearer: str,
    ) -> dict[str, str]:
        from pywidevine import Cdm, Device, PSSH

        device = Device.load(resolve_widevine_device())
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh_value))
            session = await self._get_session(url=LICENSE_URL)
            async with session.post(
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
                    **self._media_headers(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                license_body = await response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"Mediaset license server returned HTTP {response.status}"
                    )
            cdm.parse_license(session_id, license_body)
            return {
                key.kid.hex.replace("-", "").lower(): key.key.hex().lower()
                for key in cdm.get_keys(session_id)
                if "CONTENT" in str(key.type)
            }
        finally:
            cdm.close(session_id)

    def _media_headers(self) -> dict:
        return {
            "User-Agent": self.base_headers["User-Agent"],
            "Origin": MEDIASET_ORIGIN,
            "Referer": f"{MEDIASET_ORIGIN}/",
        }


class WittyTVExtractor(MediasetExtractor):
    """Named alias so WittyTV can be configured independently in Admin."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies=proxies)
        self.extractor_name = "wittytv"
