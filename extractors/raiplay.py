import json
import urllib.parse
from urllib.parse import parse_qs, urlparse

import aiohttp
import config
from extractors.base import BaseExtractor, ExtractorError
from extractors.widevine import extract_widevine_pssh, resolve_widevine_device


RAIPLAY_ORIGIN = "https://www.raiplay.it"
RELINKER_URL = "https://mediapolisvod.rai.it/relinker/relinkerServlet.htm"
_RELINKER_HOST = "mediapolisvod.rai.it"


class RaiPlayExtractor(BaseExtractor):
    """Extract clear or protected RaiPlay VOD streams through the relinker."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(
            request_headers, proxies=proxies, extractor_name="raiplay"
        )

    async def extract(self, url: str, **kwargs) -> dict:
        parsed = urlparse(url)
        content_id = parse_qs(parsed.query).get("cont", [""])[0]
        if (
            parsed.scheme != "https"
            or parsed.hostname != _RELINKER_HOST
            or not content_id
        ):
            raise ExtractorError(
                "RaiPlay extractor requires an HTTPS relinker URL containing cont"
            )
        try:
            resolved = await self._resolve_playback(content_id)
            headers = self._headers()
            if not resolved["license_url"]:
                return {
                    "destination_url": resolved["manifest_url"],
                    "request_headers": headers,
                    "mediaflow_endpoint": (
                        "mpd_manifest_proxy"
                        if ".mpd"
                        in urlparse(resolved["manifest_url"]).path.lower()
                        else "hls_proxy"
                    ),
                }
            if not config.API_PASSWORD:
                raise ExtractorError(
                    "API_PASSWORD must be configured for protected RaiPlay extraction"
                )
            keys = await self._request_keys(
                extract_widevine_pssh(resolved["manifest_text"]),
                resolved["license_url"],
            )
            if not keys:
                raise ExtractorError(
                    "RaiPlay license did not contain content keys"
                )
        except ExtractorError:
            raise
        except Exception as error:
            raise ExtractorError(f"RaiPlay extraction failed: {error}") from error

        clearkey = ",".join(f"{kid}:{key}" for kid, key in keys.items())
        return {
            "destination_url": resolved["manifest_url"],
            "request_headers": headers,
            "mediaflow_endpoint": "mpd_manifest_proxy",
            "captured_manifest": resolved["manifest_text"],
            "query_params": {"clearkey": clearkey},
        }

    async def _resolve_playback(self, content_id: str) -> dict:
        data = await self._json_request(
            RELINKER_URL, {"cont": content_id, "output": "62"}
        )
        manifest_url = str((data.get("video") or [""])[0] or "")
        self._validate_media_url(manifest_url)
        if ".mpd" not in urlparse(manifest_url).path.lower():
            return {
                "manifest_url": manifest_url,
                "manifest_text": "",
                "license_url": "",
            }
        manifest_text = await self._get_text(manifest_url)
        licenses = (
            (data.get("licence_server_map") or {}).get("drmLicenseUrlValues")
            or []
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

    async def _json_request(self, url: str, params: dict) -> dict:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            params=params,
            headers=self._headers(),
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
                raise RuntimeError(
                    "RaiPlay relinker returned invalid JSON"
                ) from error

    async def _get_text(self, url: str) -> str:
        session = await self._get_session(url=url)
        async with session.get(
            url,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"{urlparse(url).hostname} returned HTTP {response.status}"
                )
            return text

    async def _request_keys(
        self, pssh_value: str, license_url: str
    ) -> dict[str, str]:
        from pywidevine import Cdm, Device, PSSH

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
        base_license_url = parsed_license._replace(
            query="", fragment=""
        ).geturl()

        device = Device.load(resolve_widevine_device())
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh_value))
            session = await self._get_session(url=base_license_url)
            async with session.post(
                base_license_url,
                data=challenge,
                headers={
                    **self._headers(),
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
    def _headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142 Safari/537.36"
            ),
            "Origin": RAIPLAY_ORIGIN,
            "Referer": f"{RAIPLAY_ORIGIN}/",
        }

    @staticmethod
    def _validate_media_url(value: str):
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
