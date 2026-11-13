"""HTTPS client for the separate DUAL offset API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

import aiohttp


logger = logging.getLogger("easyproxy.dual.offsets")


class RemoteOffsetStore:
    """Use the central offset API; no MongoDB connection exists in EasyProxy."""

    def __init__(self, base_url: str | None = None, timeout: float = 12.0):
        url = base_url or os.environ.get("DUAL_OFFSET_URL", "https://dualdb.realbestia.com")
        self.base_url = url.rstrip("/")
        self.timeout = float(os.environ.get("DUAL_OFFSET_TIMEOUT", str(timeout)))
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    @staticmethod
    def key(media_key: str, resolution: int, video_fp: str, audio_fp: str) -> str:
        # v3: offsets measured before the drift fix (2.13.84-86) are not
        # reused; bump again whenever the sync result changes meaning.
        return hashlib.sha1(
            f"v3|{media_key}|{resolution}|{video_fp}|{audio_fp}".encode()
        ).hexdigest()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout, connect=4.0),
                    connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
                    headers={"Accept": "application/json"},
                )
        return self._session

    async def _post(self, path: str, payload: dict) -> dict | None:
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload) as response:
                    if response.status >= 400:
                        detail = (await response.text())[:300]
                        logger.warning(
                            "Remote offset API returned HTTP %s: %s",
                            response.status,
                            detail or "no response body",
                        )
                        return None
                    data = await response.json()
                    return data if isinstance(data, dict) else None
            except asyncio.TimeoutError:
                if attempt == 0:
                    logger.debug("Remote offset API timeout on %s, retrying...", path)
                    continue
                logger.warning(
                    "Remote offset API request timed out on %s (timeout=%.1fs)",
                    path,
                    self.timeout,
                )
                return None
            except (aiohttp.ClientError, ValueError) as exc:
                if attempt == 0 and isinstance(exc, aiohttp.ClientError):
                    continue
                logger.warning("Remote offset API request failed on %s: %s", path, exc)
                return None
        return None

    async def lookup(self, payload: dict):
        response = await self._post("/v1/dual/offset/lookup", payload)
        result = response.get("offset") if response else None
        return result if isinstance(result, dict) else None

    async def cache_status(self, payload: dict):
        # Toast uses camelCase while the shared offset API accepts snake_case.
        # Normalize here so cache-status checks do not get rejected with 400.
        normalized = {
            "media_key": payload.get("media_key") or payload.get("mediaKey"),
            "resolution": payload.get("resolution"),
            "video_fingerprint": (
                payload.get("video_fingerprint")
                or payload.get("videoFingerprint")
            ),
        }
        response = await self._post("/v1/dual/offset/status", normalized)
        result = response.get("offset") if response else None
        return result if isinstance(result, dict) else None

    async def report(self, payload: dict, result: dict):
        await self._post(
            "/v1/dual/offset/report",
            {**payload, "offset": result},
        )

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
