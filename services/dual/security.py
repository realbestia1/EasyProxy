import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse


def _host_is_public_shape(url: str, require_https: bool = True) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if require_https and parsed.scheme != "https":
        return False
    if not require_https and parsed.scheme not in ("http", "https"):
        return False
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 80, 443)
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith((".local", ".internal", ".home.arpa"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def valid_public_url(url: str, require_https: bool = True) -> bool:
    return _host_is_public_shape(url, require_https=require_https)


async def resolves_publicly(url: str, require_https: bool = True) -> bool:
    if not valid_public_url(url, require_https=require_https):
        return False
    host = urlparse(url).hostname or ""
    port = urlparse(url).port or (443 if require_https else 80)
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {record[4][0] for record in records if record[4]}
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


@dataclass
class _Session:
    digest: str
    expires_at: float
    last_activity: float


class SessionManager:
    # The token travels inside the player's playlist URLs, so it must survive
    # long pauses and slow 4K buffering: expire on the TTL, not on idle.
    IDLE_TIMEOUT_SECONDS = 21600

    def __init__(self, ttl_seconds: int = 21600, fixed_token: str = ""):
        self.ttl_seconds = max(300, int(ttl_seconds))
        self.fixed_token = fixed_token.strip()
        self._sessions: dict[str, _Session] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self) -> tuple[str, float]:
        token = self.fixed_token or secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + self.ttl_seconds
        self._sessions[self._digest(token)] = _Session(
            self._digest(token), expires_at, now)
        return token, expires_at

    def valid(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        if self.fixed_token and hmac.compare_digest(token, self.fixed_token):
            return True
        now = time.time()
        digest = self._digest(token)
        session = self._sessions.get(digest)
        if not session:
            return False
        if session.expires_at <= now or now - session.last_activity >= self.IDLE_TIMEOUT_SECONDS:
            self._sessions.pop(digest, None)
            return False
        session.last_activity = now
        return True

    def cleanup(self):
        now = time.time()
        for digest, session in list(self._sessions.items()):
            if (
                session.expires_at <= now
                or now - session.last_activity >= self.IDLE_TIMEOUT_SECONDS
            ):
                self._sessions.pop(digest, None)


def request_token(request, body: dict | None = None) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.query.get("t")
        or request.headers.get("x-dual-token")
        or str((body or {}).get("token") or "")
    ).strip()
