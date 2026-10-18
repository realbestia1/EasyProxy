import re
import base64
import json
import asyncio
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

from extractors.base import BaseExtractor, ExtractorError


# The site's PoW hash is solved by scripts/pow_solver.js (Node/V8).
def _solve_pow_node(nonce: str, difficulty: int, timeout: float):
    """Use the site's native-style JS hash when Node.js is available."""
    node = shutil.which("node")
    if not node:
        return None

    solver = Path(__file__).resolve().parent.parent / "scripts" / "pow_solver.js"
    try:
        result = subprocess.run(
            [node, str(solver), nonce, str(difficulty), str(int(timeout * 1000))],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    solution = result.stdout.strip()
    return solution or None


class F16PxExtractor(BaseExtractor):
    F16PX_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0"
    # Covers the sequential API calls, a slower PoW solve, and playback.
    REQUEST_TIMEOUT_TOTAL = 180
    POW_TIMEOUT_SECONDS = 120
    ERROR_PREFIX = "F16PX"

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="f16px", bypass_warp=bypass_warp)

    def _error(self, message: str) -> ExtractorError:
        return ExtractorError(f"{self.ERROR_PREFIX}: {message}")

    # ── base64url ──
    @staticmethod
    def _b64url_decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @staticmethod
    def _b64url_encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @classmethod
    def _int_to_b64url(cls, value) -> str:
        return cls._b64url_encode(int(value).to_bytes(32, "big"))

    @staticmethod
    def _pick_best(sources: list) -> str:
        def label_key(s):
            try:
                return int(s.get("label", 0))
            except Exception:
                return 0
        return sorted(sources, key=label_key, reverse=True)[0]["url"]

    def _join_key_parts(self, parts: list, version: str) -> bytes:
        v = int(version)
        n = len(parts)
        ka = self._b64url_decode(parts[v - 1])
        kb = self._b64url_decode(parts[n - v])
        return ka + kb

    def _decrypt_sources(self, pb: dict) -> list:
        iv = self._b64url_decode(pb["iv"])
        key = self._join_key_parts(pb["key_parts"], pb["version"])
        payload = self._b64url_decode(pb["payload"])
        if len(payload) < 16:
            raise self._error("GCM authentication failed")
        try:
            decrypted = AES.new(key, AES.MODE_GCM, nonce=iv).decrypt_and_verify(
                payload[:-16], payload[-16:]
            )
        except ValueError:
            raise self._error("GCM authentication failed")
        return json.loads(decrypted.decode("utf-8", "ignore")).get("sources") or []

    # ── attestation (ECDSA P-256, raw r||s signature) ──
    def _build_attest_payload(self, challenge: dict) -> dict:
        key = ECC.generate(curve="P-256")
        digest = SHA256.new(challenge["nonce"].encode())
        signature = DSS.new(key, "fips-186-3", encoding="binary").sign(digest)  # raw r||s
        public_key = {
            "alg": "ES256",
            "crv": "P-256",
            "ext": True,
            "key_ops": ["verify"],
            "kty": "EC",
            "x": self._int_to_b64url(key.pointQ.x),
            "y": self._int_to_b64url(key.pointQ.y),
        }
        return {
            "viewer_id": "",
            "device_id": "",
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "signature": self._b64url_encode(signature),
            "public_key": public_key,
            "client": {
                "user_agent": self.F16PX_USER_AGENT,
                "pixel_ratio": 2,
                "screen_width": 1536,
                "screen_height": 960,
                "color_depth": 24,
                "languages": ["en-US", "en"],
                "timezone": "Europe/Rome",
                "hardware_concurrency": 8,
                "touch_points": 0,
                "pointer_type": "fine,hover",
                "extra": {"vendor": "", "appVersion": "5.0 (Windows)"},
            },
            "storage": {},
            "attributes": {"entropy": "low"},
        }

    async def extract(self, url: str, **kwargs) -> dict:
        self._apply_routing_kwargs(url, kwargs)
        parsed = urlparse(url)
        embed_host = parsed.netloc
        embed_origin = f"{parsed.scheme}://{parsed.netloc}"

        match = re.search(r"/e/([A-Za-z0-9]+)", parsed.path or "")
        if not match:
            raise self._error("Invalid embed URL")
        code = match.group(1)
        embed_url = f"{embed_origin}/e/{code}"

        # 1) details (on embed host) → embed_frame_url gives the API base + referer
        details_resp = await self._make_request(
            f"{embed_origin}/api/videos/{code}/embed/details",
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": self.F16PX_USER_AGENT,
                "Referer": embed_url,
                "Origin": embed_origin,
            },
            method="GET",
            retries=2,
        )
        details = json.loads(details_resp.text)
        frame = details.get("embed_frame_url") or embed_url
        api_origin = f"{urlparse(frame).scheme}://{urlparse(frame).netloc}"
        referer = frame

        common = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": self.F16PX_USER_AGENT,
            "Origin": api_origin,
            "Referer": referer,
            "X-Embed-Origin": embed_host,
            "X-Embed-Referer": embed_url,
            "X-Embed-Parent": embed_url,
        }

        # 2) settings → captcha required?
        settings_resp = await self._make_request(
            f"{api_origin}/api/videos/{code}/embed/settings",
            headers=common, method="GET", retries=2,
        )
        try:
            captcha_required = bool(json.loads(settings_resp.text).get("captcha_required"))
        except Exception:
            captcha_required = True

        # 3) challenge
        challenge_resp = await self._make_request(
            f"{api_origin}/api/videos/access/challenge",
            headers=common, method="POST", retries=2, json={},
        )
        challenge = json.loads(challenge_resp.text)

        # 4) attest (sets viewer/device cookies)
        attest_resp = await self._make_request(
            f"{api_origin}/api/videos/access/attest",
            headers=common, method="POST", retries=2,
            json=self._build_attest_payload(challenge),
        )
        attest = json.loads(attest_resp.text)
        fingerprint = {
            "token": attest["token"],
            "viewer_id": attest["viewer_id"],
            "device_id": attest["device_id"],
            "confidence": attest["confidence"],
        }

        cookie = f"byse_viewer_id={fingerprint['viewer_id']}; byse_device_id={fingerprint['device_id']}"
        with_cookie = {**common, "Cookie": cookie}

        # 5+6) captcha PoW (only if required)
        captcha_token = None
        if captcha_required:
            captcha_resp = await self._make_request(
                f"{api_origin}/api/videos/{code}/embed/captcha",
                headers=with_cookie, method="POST", retries=2,
                json={"fingerprint": fingerprint},
            )
            cap = json.loads(captcha_resp.text)
            pow_nonce = cap["pow_nonce"]
            pow_difficulty = cap["pow_difficulty"]
            pow_token = cap["pow_token"]

            # solve off the event loop; 60s timeout confirmed sufficient for
            # difficulty ~16 on Pi-class hardware. PoW token TTL is 1800s.
            loop = asyncio.get_event_loop()
            solution = await loop.run_in_executor(
                None,
                _solve_pow_node,
                pow_nonce,
                pow_difficulty,
                self.POW_TIMEOUT_SECONDS,
            )
            if solution is None:
                raise self._error("PoW solve timed out")

            verify_resp = await self._make_request(
                f"{api_origin}/api/videos/{code}/embed/captcha/verify",
                headers=with_cookie, method="POST", retries=2,
                json={"pow_token": pow_token, "solution": solution, "fingerprint": fingerprint},
            )
            verify = json.loads(verify_resp.text)
            if verify.get("status") != "ok" or not verify.get("token"):
                raise self._error(f"captcha verify failed ({verify})")
            captcha_token = verify["token"]

        # 7) playback — verify token rides in X-Captcha-Token header (not the body)
        playback_headers = dict(with_cookie)
        if captcha_token:
            playback_headers["X-Captcha-Token"] = captcha_token

        playback_resp = await self._make_request(
            f"{api_origin}/api/videos/{code}/embed/playback",
            headers=playback_headers, method="POST", retries=2,
            json={"fingerprint": fingerprint},
        )
        data = json.loads(playback_resp.text)
        if not data:
            raise self._error("Empty playback response")

        out_headers = {
            "referer": referer,
            "origin": api_origin,
            "Accept-Language": "en-US,en;q=0.5",
            "Accept": "*/*",
            "User-Agent": self.F16PX_USER_AGENT,
        }

        # Case 1: plain sources
        if data.get("sources"):
            return self._result(self._pick_best(data["sources"]), out_headers)

        # Case 2: encrypted playback
        pb = data.get("playback")
        if not pb:
            raise self._error("No playback data")
        try:
            sources = self._decrypt_sources(pb)
        except Exception as e:
            raise self._error(f"Decryption failed ({e})")
        if not sources:
            raise self._error("No sources after decryption")

        return self._result(self._pick_best(sources), out_headers)

