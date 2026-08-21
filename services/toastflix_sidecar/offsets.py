import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import aiohttp


class OffsetStore:
    """Local offset cache with an optional central VPS lookup/report endpoint."""

    def __init__(self, path: str, api_url: str = "", api_token: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token.strip()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS offsets (
                    cache_key TEXT PRIMARY KEY,
                    media_key TEXT NOT NULL,
                    resolution INTEGER NOT NULL,
                    video_fingerprint TEXT NOT NULL,
                    audio_fingerprint TEXT NOT NULL,
                    offset_seconds REAL,
                    rate REAL NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

    @staticmethod
    def key(media_key: str, resolution: int, video_fp: str, audio_fp: str) -> str:
        import hashlib
        return hashlib.sha1(
            f"v2|{media_key}|{resolution}|{video_fp}|{audio_fp}".encode()
        ).hexdigest()

    def _local_get(self, cache_key: str):
        with self._connect() as conn:
            columns = [item[1] for item in conn.execute("PRAGMA table_info(offsets)")]
            row = conn.execute(
                "SELECT * FROM offsets WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if not row:
            return None
        result = dict(zip(columns, row))
        result["details"] = json.loads(result.get("details") or "{}")
        return result

    async def lookup(self, payload: dict):
        api_url = self.api_url
        if not api_url and payload.get("vpsHost"):
            api_url = f"{str(payload['vpsHost']).rstrip('/')}/dual/offset"
        if api_url:
            try:
                headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
                central_payload = dict(payload)
                if payload.get("vpsAccess"):
                    central_payload["access"] = payload["vpsAccess"]
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.post(
                        f"{api_url}/lookup",
                        json=central_payload,
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            data = await response.json(content_type=None)
                            if data.get("found") and data.get("offset") is not None:
                                return data["offset"]
            except Exception:
                pass
        return await asyncio.to_thread(self._local_get, payload["cache_key"])

    def _local_put(self, payload: dict, result: dict):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO offsets
                (cache_key, media_key, resolution, video_fingerprint, audio_fingerprint,
                 offset_seconds, rate, confidence, status, details, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["cache_key"], payload["media_key"], payload["resolution"],
                    payload["video_fingerprint"], payload["audio_fingerprint"],
                    result.get("offset"), result.get("rate", 1.0),
                    result.get("confidence", 0.0), result.get("status", "incompatible"),
                    json.dumps(result, separators=(",", ":")), time.time(),
                ),
            )

    async def report(self, payload: dict, result: dict):
        await asyncio.to_thread(self._local_put, payload, result)
        api_url = self.api_url
        if not api_url and payload.get("vpsHost"):
            api_url = f"{str(payload['vpsHost']).rstrip('/')}/dual/offset"
        if not api_url:
            return
        try:
            headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
            central_payload = {**payload, "offset": result}
            if payload.get("vpsAccess"):
                central_payload["access"] = payload["vpsAccess"]
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    f"{api_url}/report",
                    json=central_payload,
                    headers=headers,
                ):
                    pass
        except Exception:
            pass
