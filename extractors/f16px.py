import re
import base64
import json
import time
import hashlib
import os
import random
from urllib.parse import urlparse

from extractors.base import BaseExtractor, ExtractorError
from utils import python_aesgcm


class F16PxExtractor(BaseExtractor):
    F16PX_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0"

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="f16px")

    @staticmethod
    def _b64url_decode(value: str) -> bytes:
        value = value.replace("-", "+").replace("_", "/")
        padding = (-len(value)) % 4
        if padding:
            value += "=" * padding
        return base64.b64decode(value)

    def _join_key_parts(self, parts) -> bytes:
        return b"".join(self._b64url_decode(p) for p in parts)

    @staticmethod
    def _pick_best(sources: list) -> str:
        def label_key(s):
            try:
                return int(s.get("label", 0))
            except:
                return 0
        return sorted(sources, key=label_key, reverse=True)[0]["url"]

    # ✅ Correct fingerprint (ResolveURL compatible)
    def _make_fingerprint_payload(self) -> dict:
        viewer_id = os.urandom(16).hex()
        device_id = os.urandom(16).hex()
        now = int(time.time())

        token_payload = {
            "viewer_id": viewer_id,
            "device_id": device_id,
            "confidence": round(random.uniform(0.6, 0.9), 2),
            "iat": now,
            "exp": now + 600,
        }

        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(token_payload, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

        # ✅ FIX: SHA256 (NOT HMAC)
        sig = hashlib.sha256(payload_b64.encode()).digest()

        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        token = f"{payload_b64}.{sig_b64}"

        # match ResolveURL structure
        fingerprint = {
            "viewer_id": viewer_id,
            "device_id": device_id,
            "confidence": token_payload["confidence"],
            "token": token,
        }

        return {"fingerprint": fingerprint}

    def _decrypt_sources(self, pb: dict) -> list:
        iv = self._b64url_decode(pb["iv"])
        key = self._join_key_parts(pb["key_parts"])
        payload = self._b64url_decode(pb["payload"])

        cipher = python_aesgcm.new(key)
        decrypted = cipher.open(iv, payload)

        if decrypted is None:
            raise ExtractorError("F16PX: GCM authentication failed")

        return json.loads(decrypted.decode("utf-8", "ignore")).get("sources") or []

    async def extract(self, url: str, **kwargs) -> dict:
        parsed = urlparse(url)
        host = parsed.netloc
        origin = f"{parsed.scheme}://{parsed.netloc}"

        match = re.search(r"/e/([A-Za-z0-9]+)", parsed.path or "")
        if not match:
            raise ExtractorError("F16PX: Invalid embed URL")

        media_id = match.group(1)

        # ✅ FIX: correct endpoint
        api_url = f"https://{host}/api/videos/{media_id}/embed/playback"

        headers = self.base_headers.copy()
        headers["referer"] = f"{origin}/"
        headers["origin"] = origin
        headers["content-type"] = "application/json"
        headers["user-agent"] = self.F16PX_USER_AGENT

        # ✅ Try POST (modern API)
        try:
            resp = await self._make_request(
                api_url,
                headers=headers,
                method="POST",
                json=self._make_fingerprint_payload()
            )
            data = json.loads(resp.text)
        except Exception:
            data = {}

        # ✅ Fallback to GET if POST fails
        if not data or (not data.get("sources") and not data.get("playback")):
            resp = await self._make_request(api_url, headers=headers)
            try:
                data = json.loads(resp.text)
            except Exception:
                raise ExtractorError("F16PX: Invalid JSON response")

        # Case 1: plain sources
        if data.get("sources"):
            best = self._pick_best(data["sources"])
            return {
                "destination_url": best,
                "request_headers": headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        # Case 2: encrypted playback
        pb = data.get("playback")
        if not pb:
            raise ExtractorError("F16PX: No playback data")

        try:
            sources = self._decrypt_sources(pb)
        except Exception as e:
            raise ExtractorError(f"F16PX: Decryption failed ({e})")

        if not sources:
            raise ExtractorError("F16PX: No sources after decryption")

        out_headers = {
            "referer": f"{origin}/",
            "origin": origin,
            "Accept-Language": "en-US,en;q=0.5",
            "Accept": "*/*",
            "User-Agent": self.F16PX_USER_AGENT,
        }

        return {
            "destination_url": self._pick_best(sources),
            "request_headers": out_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
