"""Sealed proxy URLs and failed-auth throttling.

Addons can send `?sx=<sealed>` instead of `?d=...&api_password=...`: the query
is AES-GCM encrypted with a key derived from API_PASSWORD, so a link copied
from a player reveals neither the password nor the source, and only proxies
that one destination. Responses to sealed requests have every child URL that
carries `api_password=` sealed the same way, so segments never show it either.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse

from aiohttp import web
from multidict import MultiDict

import config as _config
from config import get_client_ip
from Crypto.Cipher import AES
from services.secure_state import open_state

logger = logging.getLogger(__name__)

PURPOSE = "proxy-url-v1"
PARAM = "sx"
CHILD_MAX_AGE = 24 * 60 * 60

# A 7-8 character password is brute-forceable without a limit.
FAIL_WINDOW = 10 * 60
FAIL_LIMIT = 30
_failures: dict[str, dict] = {}

_TEXT_TYPES = ("mpegurl", "dash+xml", "json", "text/", "xml")
# The query of any URL that carries api_password, up to a delimiter that cannot be in a URL.
_QUERY_WITH_PASSWORD = re.compile(r"\?([^\s\"'<>#]*?\bapi_password=[^\s\"'<>#]*)")
_warned_paths: set[str] = set()


def _child_key() -> bytes:
    # Same key as secure_state for this purpose, so open_state reads these links too.
    return hashlib.sha256(
        b"easyproxy-state-v1:" + PURPOSE.encode("utf-8") + b":" + str(_config.API_PASSWORD or "").encode("utf-8")
    ).digest()


def seal_query(pairs, max_age: int = CHILD_MAX_AGE) -> str:
    """Deterministic for the same pairs within a day: live playlists refresh every
    few seconds and players restart the decoder when a segment/init URI changes."""
    now = int(time.time())
    exp = (now // 86400 + 1) * 86400 + max(60, int(max_age))
    plaintext = json.dumps({"q": [[str(k), str(v)] for k, v in pairs], "exp": exp},
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    key = _child_key()
    # Nonce derived from the plaintext (SIV-style): equal links stay equal, different ones never share a nonce.
    nonce = hmac.new(key, plaintext, hashlib.sha256).digest()[:16]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii").rstrip("=")


def open_query(token: str):
    data = open_state(token, PURPOSE)
    pairs = data.get("q") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        return None
    return [(str(k), str(v)) for k, v in pairs if isinstance(k, str)]


def _seal_match(match: re.Match) -> str:
    raw = match.group(1)
    xml_escaped = "&amp;" in raw
    query = raw.replace("&amp;", "&") if xml_escaped else raw
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if k != "api_password"]
    return f"?{PARAM}={seal_query(pairs)}"


def seal_urls_in_text(text: str) -> str:
    return _QUERY_WITH_PASSWORD.sub(_seal_match, text)


def _is_throttled(ip: str, now: float) -> bool:
    hits = {fp: t for fp, t in _failures.get(ip, {}).items() if now - t < FAIL_WINDOW}
    if hits:
        _failures[ip] = hits
    else:
        _failures.pop(ip, None)
    return len(hits) >= FAIL_LIMIT


def _attempt_fingerprint(request: web.Request) -> str:
    value = (
        request.query.get(PARAM) or request.query.get("api_password")
        or request.headers.get("x-api-password") or request.cookies.get("admin_token") or ""
    )
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _record_failure(ip: str, now: float, fingerprint: str) -> None:
    # Counts distinct wrong secrets: a brute force tries many, while a user (or our
    # addon server) with one stale password repeats the same one and is never blocked.
    _failures.setdefault(ip, {})[fingerprint] = now
    if len(_failures) > 50000:  # bound memory under a distributed scan
        for key in list(_failures)[:10000]:
            _failures.pop(key, None)


def _seal_response(request: web.Request, response: web.StreamResponse) -> None:
    location = response.headers.get("Location")
    if location and "api_password=" in location:
        response.headers["Location"] = seal_urls_in_text(location)
    if not isinstance(response, web.Response) or response.prepared:
        return
    content_type = (response.content_type or "").lower()
    if not any(kind in content_type for kind in _TEXT_TYPES):
        return
    body = response.body
    if not isinstance(body, (bytes, bytearray)) or b"api_password" not in body:
        return
    charset = response.charset or "utf-8"
    text = seal_urls_in_text(bytes(body).decode(charset, errors="replace"))
    password = str(_config.API_PASSWORD or "")
    if password and password in text and request.path not in _warned_paths:
        _warned_paths.add(request.path)
        logger.warning("[SEALED] password ancora presente in una risposta di %s", request.path)
    response.body = text.encode(charset)


@web.middleware
async def sealed_url_middleware(request: web.Request, handler):
    ip = get_client_ip(request) or "unknown"
    now = time.time()
    if _is_throttled(ip, now):
        return web.json_response({"detail": "Too many failed attempts"}, status=429, headers={"Retry-After": str(FAIL_WINDOW)})

    sealed = PARAM in request.query
    if sealed:
        pairs = open_query(request.query.get(PARAM, ""))
        if pairs is None:
            _record_failure(ip, now, _attempt_fingerprint(request))
            return web.json_response({"detail": "Invalid or expired link"}, status=403)
        # Only the sealed parameters count: an added `redirect_stream=false` or
        # similar would otherwise turn a sealed link into a way to read the source.
        query = MultiDict(pairs)
        if request.path.startswith("/extractor"):
            query["redirect_stream"] = "true"
        if _config.API_PASSWORD:
            # Internal only: handlers authorise and build child URLs from it,
            # and _seal_response removes it from everything sent back.
            query["api_password"] = str(_config.API_PASSWORD)
        request = request.clone(rel_url=request.rel_url.with_query(query))
        request["sealed"] = True

    response = await handler(request)

    if response.status == 401:
        _record_failure(ip, now, _attempt_fingerprint(request))
    if sealed:
        if response.status >= 400 and isinstance(response, web.Response) and not response.prepared:
            # Error bodies may echo the upstream URL: keep them generic on sealed links.
            headers = {k: v for k, v in response.headers.items()
                       if k.lower() not in ("content-length", "content-type", "content-encoding")}
            return web.json_response({"detail": "Stream unavailable"}, status=response.status, headers=headers)
        _seal_response(request, response)
    return response
