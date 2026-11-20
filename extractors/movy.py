"""Movy (movy.sx) page -> unlocked HLS extractor.

The site resolves streams through ``api.wecollege.net``: a short-lived seed is
issued per TMDB id, then the sources payload comes back encrypted (base64url,
custom PRNG stream cipher bound to seed + media id). The decrypted payload
carries a ready master playlist, so no Node runner is needed.

``vidfast.vc`` serves the same catalog, so ``vidfast.py`` reuses
:func:`resolve_movy_stream` and keeps its runner as fallback.
"""

import base64
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

_API = "https://api.wecollege.net"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
_API_HEADERS = {
    "user-agent": BROWSER_UA,
    "accept": "application/json, text/plain, */*",
    "accept-language": "it-IT,it;q=0.9,en;q=0.8",
    "origin": "https://www.movy.sx",
    "referer": "https://www.movy.sx/",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}
_STREAM_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Referer": "https://www.movy.sx/",
    "Origin": "https://www.movy.sx",
}
_HOSTS = {"movy.sx", "www.movy.sx", "vidfast.vc", "www.vidfast.vc"}
_PATH_RE = re.compile(r"/(?:watch/)?(movie|tv)/(\d+)(?:/(\d+)/(\d+))?", re.IGNORECASE)
_MAX_SUBTITLES = 80

# --- stream cipher -----------------------------------------------------------
_T = (0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1,
      0x923F82A4, 0xAB1C5ED5, 0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
      0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174)
_MAGIC = b"mvm1"


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & 0xFFFFFFFF


def _o(e: int) -> int:
    e = _u32(e)
    e ^= e >> 16
    e = _imul(e, 0x85EBCA6B)
    e ^= e >> 13
    e = _imul(e, 0xC2B2AE35)
    e ^= e >> 16
    return _u32(e)


def _rotl(e: int, a: int) -> int:
    e = _u32(e)
    a &= 31
    return e if a == 0 else _u32((e << a) | (e >> (32 - a)))


def _fnv(s: str) -> int:
    a = 0x811C9DC5
    for ch in s:
        a = _imul(a ^ ord(ch), 0x1000193)
    return _o(a)


def _acc(seed: str) -> int:
    a = 0x67452301
    for i, ch in enumerate(seed):
        a = _rotl(_u32(a ^ _imul(ord(ch), _T[15 & i])), 5)
    return _o(a)


def _key_state(seed: str, media_id: str) -> dict:
    # The KSA branch is unreachable for real seeds (len*(len+1) is always even).
    if ((len(seed) * (len(seed) + 1)) & 1) == 1:
        s = list(range(256))
        t = 0
        for i in range(256):
            t = (t + s[i] + ord(seed[i % len(seed)])) & 255
            s[i], s[t] = s[t], s[i]
        return {"S": {i: s[i] for i in range(256)}, "acc": _acc(seed)}
    try:
        n = int(str(media_id)) & 0xFFFFFFFF
    except (TypeError, ValueError):
        n = 0
    n = _o(_fnv(seed) ^ _o(n ^ 0x9E3779B9))
    s = {}
    for e in range(8):
        a = n % 61
        n = _rotl(_u32(n + 0x9E3779B9), 7 + (7 & e))
        s[a] = _u32(n ^ _o(n))
        n = _o(_u32(n + a))
    return {"S": s, "acc": _o(0xA5A5A5A5 ^ n)}


def _prng(st: dict, a: int) -> int:
    s = st["S"]
    l = st["acc"]
    idx = l % 61
    in_s = 0xFFFFFFFF if idx in s else 0
    r = s.get(idx, 0)
    c = _imul(0x9E3779B9, a + 1)
    t = l
    x = _u32(r ^ c)
    b = _u32(_u32(t ^ x) | _u32(t & x & in_s))
    b = _u32(_rotl(_u32(b + l), 31 & idx) ^ _rotl(l, 31 & _imul(idx, 7)))
    l = _o(_u32(b + 0x9E3779B9))
    st["S"][idx] = l
    st["acc"] = l
    return l


def decrypt_movy(body: str, seed: str, media_id: str) -> str:
    """Decrypt a ``/miami/sources`` body into its JSON plaintext."""
    raw = body.strip()
    b64 = raw.replace("-", "+").replace("_", "/")
    b64 += "=" * ((4 - len(b64) % 4) % 4)
    data = base64.b64decode(b64)
    st = _key_state(seed, media_id)
    out = bytearray(len(data))
    i = ctr = 0
    while i < len(data):
        v = _prng(st, ctr)
        ctr += 1
        out[i] = v & 255
        i += 1
        if i < len(data):
            out[i] = (v >> 8) & 255
            i += 1
        if i < len(data):
            out[i] = (v >> 16) & 255
            i += 1
        if i < len(data):
            out[i] = (v >> 24) & 255
            i += 1
    plain = bytes(a ^ b for a, b in zip(out, data))
    if plain[:4] != _MAGIC:
        raise ExtractorError("Movy: decrypt failed")
    return plain[4:].decode("utf-8")


def _sanitize_subtitles(value: Any) -> list[dict]:
    """Keep the API's subtitle files, in the extractor result shape."""
    if not isinstance(value, list):
        return []
    entries: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        language = str(item.get("language") or item.get("lang") or "")
        entries.append({
            "display": language[:80],
            "language": language[:16],
            "url": url,
        })
        if len(entries) >= _MAX_SUBTITLES:
            break
    return entries


def _validate_and_normalize(url: str, **kwargs) -> str:
    """Canonicalize a movy.sx / vidfast.vc page URL (movie or TV)."""
    trimmed = (url or "").strip()
    parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
    host = (parsed.hostname or "").lower()
    if host not in _HOSTS:
        raise ExtractorError(
            f"Movy: expected a movy.sx or vidfast.vc URL, got {host or trimmed}"
        )
    match = _PATH_RE.search(parsed.path)
    if match is None:
        raise ExtractorError(
            "Movy: unsupported URL; expected /movie/<id> or /tv/<id>/<season>/<episode>"
        )
    kind, tmdb, season, episode = match.groups()
    kind = kind.lower()
    if kind == "tv" and not (season and episode):
        season = kwargs.get("season") or kwargs.get("s")
        episode = kwargs.get("episode") or kwargs.get("e")
        if not (season and episode):
            raise ExtractorError("Movy: TV URL needs season and episode")
    path = f"/{kind}/{tmdb}"
    if kind == "tv":
        path += f"/{int(season)}/{int(episode)}"
    return f"{parsed.scheme or 'https'}://{host}{path}"


async def resolve_movy_stream(extractor, url: str, **kwargs) -> dict[str, Any]:
    """Resolve a movy.sx / vidfast.vc page through the movy API."""
    extractor._apply_routing_kwargs(url, kwargs)
    match = _PATH_RE.search(urlparse(_validate_and_normalize(url, **kwargs)).path)
    kind, tmdb, season, episode = match.groups()
    kind = kind.lower()

    seed_resp = await extractor._make_request(
        f"{_API}/seed?mediaId={tmdb}", headers=dict(_API_HEADERS)
    )
    seed = str(json.loads(seed_resp.text)["seed"])

    api_url = f"{_API}/miami/sources?mediaType={kind}&tmdbId={tmdb}"
    if kind == "tv":
        api_url += f"&seasonId={int(season)}&episodeId={int(episode)}"
    api_url += f"&enc=2&seed={quote(seed, safe='')}"
    sources_resp = await extractor._make_request(api_url, headers=dict(_API_HEADERS))
    payload = json.loads(decrypt_movy(sources_resp.text, seed, tmdb))

    playlist = str(payload.get("playlist") or "").strip()
    if not playlist.startswith("https://"):
        raise ExtractorError("Movy: no playable playlist")

    logger.info("Movy: extracted %s", playlist[:100])
    result = extractor._result(playlist, dict(_STREAM_HEADERS))
    result["subtitles"] = _sanitize_subtitles(payload.get("subtitles"))
    return result


class MovyExtractor(BaseExtractor):
    """Resolve ``movy.sx`` (and ``vidfast.vc``) movie/TV pages to HLS."""

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="movy", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_proxy"

    @classmethod
    def _validate_and_normalize(cls, url: str, **kwargs) -> str:
        return _validate_and_normalize(url, **kwargs)

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        return await resolve_movy_stream(self, url, **kwargs)
