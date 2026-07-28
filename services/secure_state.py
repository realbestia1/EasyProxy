import base64
import hashlib
import json
import time

from Crypto.Cipher import AES

import config as _config


_PURPOSE_PREFIX = b"easyproxy-state-v1:"


def _state_key(purpose: str) -> bytes:
    secret = str(_config.API_PASSWORD or "")
    return hashlib.sha256(
        _PURPOSE_PREFIX + purpose.encode("utf-8") + b":" + secret.encode("utf-8")
    ).digest()


def seal_state(payload: dict, purpose: str, max_age: int = 6 * 60 * 60) -> str:
    data = dict(payload)
    data["exp"] = int(time.time()) + max(60, int(max_age))
    plaintext = json.dumps(
        data, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    cipher = AES.new(_state_key(purpose), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    raw = cipher.nonce + tag + ciphertext
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def open_state(token: str, purpose: str) -> dict | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if len(raw) < 33:
            return None
        nonce, tag, ciphertext = raw[:16], raw[16:32], raw[32:]
        cipher = AES.new(_state_key(purpose), AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        data = json.loads(plaintext)
        if int(data.get("exp") or 0) < int(time.time()):
            return None
        data.pop("exp", None)
        return data
    except Exception:
        return None
