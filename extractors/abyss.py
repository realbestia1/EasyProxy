"""Abyss / Hydrax CDN extractor (based on ResolveURL's AbyssResolver)."""

import base64
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from Crypto.Cipher import AES

from extractors.base import BaseExtractor, ExtractorError

_URL_RE = re.compile(
    r"(?://|\.)(abysscdn\.com|hydraxcdn\.biz|short\.icu|embedplayabyss\.top|abyssplayer\.com|mload\.cam)"
    r"(?:/\?v=|/player\.html\?v=|/)([0-9a-zA-Z_-]+)",
    re.IGNORECASE,
)
_DATAS_RE = re.compile(r'(?:const|var)\s+datas\s*=\s*"([^"]+)"')
_CUSTOM_B64_RE = re.compile(
    r"[\w$]+='([A-Za-z0-9+/=RB0fpH8ZEyVLkv7c2i6MAJ5u3IKFDxlS1NTsnGaqmXYdUrtzjwObCgQP94hoeW]{30,})_'"
)
_LEGACY_DOMAIN_RE = re.compile(r"""['"]domain['"]\s*:\s*['"]([^'"]+)['"]""")
_LEGACY_EDNS_RE = re.compile(r"""['"]id['"]\s*:\s*['"](?:['"])?(edns[^'"]+)['"]""")

_CHARSET_TRANS = str.maketrans(
    "RB0fpH8ZEyVLkv7c2i6MAJ5u3IKFDxlS1NTsnGaqmXYdUrtzjwObCgQP94hoeW+/=",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
)
_ESC_MAP = {
    "n": "\n", "r": "\r", "t": "\t", "b": "\b",
    "f": "\f", "\\": "\\", '"': '"', "/": "/",
}


def _decode_escaped_binary(escaped: str) -> str:
    if not escaped:
        return ""
    out = []
    i = 0
    n = len(escaped)
    while i < n:
        ch = escaped[i]
        if ch == "\\" and i + 1 < n:
            nxt = escaped[i + 1]
            if nxt == "u" and i + 5 < n:
                try:
                    out.append(chr(int(escaped[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nxt in _ESC_MAP:
                out.append(_ESC_MAP[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_datas_payload(html: str) -> dict:
    match = _DATAS_RE.search(html or "")
    if not match:
        return {}
    raw_b64 = match.group(1).strip()
    raw_b64 += "=" * (-len(raw_b64) % 4)
    try:
        raw = base64.b64decode(raw_b64)
    except Exception:
        return {}

    try:
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    decoded = raw.decode("latin-1", "ignore")
    payload = {}
    for key, pat in (
        ("slug", r'"slug"\s*:\s*"([^"]+)"'),
        ("md5_id", r'"md5_id"\s*:\s*(\d+)'),
        ("user_id", r'"user_id"\s*:\s*(\d+)'),
    ):
        m = re.search(pat, decoded)
        if m:
            payload[key] = m.group(1) if key == "slug" else int(m.group(1))

    media_marker = b'"media":"'
    m_idx = raw.find(media_marker)
    c_idx = raw.find(b'","config"', m_idx + len(media_marker)) if m_idx >= 0 else -1
    if m_idx >= 0 and c_idx > m_idx:
        media_escaped = raw[m_idx + len(media_marker):c_idx].decode("latin-1", "ignore")
        payload["media"] = _decode_escaped_binary(media_escaped)
    else:
        m = re.search(r'"media"\s*:\s*"((?:\\.|[^"\\])*)"', decoded, re.DOTALL)
        if m:
            payload["media"] = _decode_escaped_binary(m.group(1))

    config_m = re.search(r'"isDownload"\s*:\s*(true|false)', decoded)
    if config_m:
        payload["isDownload"] = config_m.group(1) == "true"

    return payload


def _derive_key(seed) -> bytes:
    seed_str = str(seed)
    if seed_str.replace(".", "", 1).replace(":", "").replace("-", "").isdigit():
        digest_source = bytes(int(ch) if ch.isdigit() else (ord(ch) & 0xFF) for ch in seed_str)
    else:
        digest_source = seed_str.encode("utf-8")
    return hashlib.md5(digest_source).hexdigest().encode("ascii")


def _aes_ctr_transform(data_bytes: bytes, key_seed) -> bytes | None:
    key = _derive_key(key_seed)
    try:
        # ponytail: pycryptodome AES-CTR matches ResolveURL's pyaes big-endian counter (iv = key[:16])
        return AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=key[:16]).encrypt(data_bytes)
    except Exception:
        return None


def _decrypt_media(encrypted_text: str, user_id, slug, md5_id) -> dict:
    if not (encrypted_text and user_id and slug and md5_id):
        return {}
    raw_bytes = bytes(ord(ch) & 0xFF for ch in encrypted_text)
    result = _aes_ctr_transform(raw_bytes, f"{user_id}:{slug}:{md5_id}")
    if not result:
        return {}
    try:
        decoded = json.loads(result.decode("utf-8", "ignore"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _b64_stripped(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _build_sora_token(path_value: str, size_value: str) -> str | None:
    transformed = _aes_ctr_transform(path_value.encode("utf-8"), size_value)
    if not transformed:
        return None
    return _b64_stripped(_b64_stripped(transformed).encode("ascii"))


def _safe_int(val) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _extract_from_media_payload(media_payload: dict, slug, md5_id, is_download: bool = False) -> str | None:
    if not isinstance(media_payload, dict):
        return None

    mp4 = media_payload.get("mp4") if isinstance(media_payload.get("mp4"), dict) else {}
    raw_sources = mp4.get("sources") if isinstance(mp4.get("sources"), list) else []
    sources = sorted(
        (s for s in raw_sources if isinstance(s, dict)),
        key=lambda s: _safe_int(s.get("size")),
        reverse=True,
    )

    domains = mp4.get("domains") if isinstance(mp4.get("domains"), list) else []
    for src in sources:
        size = src.get("size")
        res_id = src.get("res_id")
        sub = src.get("sub")
        if not (size and res_id and sub and md5_id and slug):
            continue
        sub_str = str(sub)
        domain = next((d for d in domains if isinstance(d, str) and sub_str in d), None)
        if not domain:
            if "." in sub_str:
                domain = sub_str
            elif isinstance(src.get("url"), str) and src["url"]:
                domain = urlparse(src["url"]).netloc
        if not domain:
            continue
        token = _build_sora_token(f"/mp4/{md5_id}/{res_id}/{size}?v={slug}", str(size))
        if token:
            norm = domain if domain.startswith("http") else f"https://{domain}"
            return f"{norm.rstrip('/')}/sora/{size}/{token}"

    for src in sources:
        direct = src.get("file")
        if isinstance(direct, str) and direct:
            return direct.replace("\\/", "/")

    if not is_download:
        for src in sources:
            url_ = src.get("url")
            path_ = src.get("path")
            if isinstance(url_, str) and isinstance(path_, str) and url_ and path_:
                return f"{url_.rstrip('/')}/{path_.lstrip('/')}".replace("\\/", "/")

    hls = media_payload.get("hls") if isinstance(media_payload.get("hls"), dict) else {}
    for key in ("file", "url", "master", "src", "source"):
        val = hls.get(key)
        if isinstance(val, str) and val:
            return val.replace("\\/", "/")
    for hs in (hls.get("sources") or []):
        if isinstance(hs, dict):
            f = hs.get("file") or hs.get("url") or hs.get("src")
            if isinstance(f, str) and f:
                return f.replace("\\/", "/")

    hls_id = hls.get("id")
    if hls_id:
        return f"https://abysscdn.com/#hls/{hls_id}/master.m3u8"

    return None


def _legacy_extract(html: str) -> str | None:
    m = _CUSTOM_B64_RE.search(html or "")
    if m:
        try:
            padded = m.group(1).translate(_CHARSET_TRANS)
            padded += "=" * (-len(padded) % 4)
            meta = json.loads(base64.b64decode(padded).decode("utf-8", "ignore"))
            domain = meta.get("domain", "")
            vid_id = meta.get("id", "")
            if domain and vid_id:
                return f"https://{domain.strip('/')}/{vid_id}"
        except Exception:
            pass

    dm = _LEGACY_DOMAIN_RE.search(html or "")
    im = _LEGACY_EDNS_RE.search(html or "")
    if dm and im:
        return f"https://{dm.group(1).strip('/')}/{im.group(1)}"

    return None


class AbyssExtractor(BaseExtractor):
    """Abyss / Hydrax CDN embed extractor."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies, extractor_name="abyss", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    @staticmethod
    def _normalize_url(url: str) -> str:
        raw = (url or "").strip()
        m = _URL_RE.search(raw)
        if m:
            host = m.group(1).lower()
            media_id = m.group(2)
            if host in ("short.icu", "embedplayabyss.top"):
                host = "abysscdn.com"
            if host == "abyssplayer.com":
                return f"https://{host}/{media_id}"
            return f"https://{host}/?v={media_id}"
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return raw
        raise ExtractorError(f"ABYSS: invalid URL: {url}")

    async def extract(self, url: str, **kwargs) -> dict:
        web_url = self._normalize_url(url)
        self._apply_routing_kwargs(web_url, kwargs)

        ua = self.base_headers["User-Agent"]
        headers = {
            "User-Agent": ua,
            "Referer": urljoin(web_url, "/"),
        }
        resp = await self._make_request(web_url, headers=headers)
        html = resp.text or ""
        if not html:
            raise ExtractorError("ABYSS: empty HTML response")

        datas = _extract_datas_payload(html)
        if datas:
            slug = datas.get("slug")
            md5_id = datas.get("md5_id")
            user_id = datas.get("user_id")
            media_payload = datas.get("media")
            if not isinstance(media_payload, dict):
                media_payload = _decrypt_media(media_payload, user_id, slug, md5_id)
            config = datas.get("config") if isinstance(datas.get("config"), dict) else {}
            is_download = bool(datas.get("isDownload") or config.get("isDownload"))
            source = _extract_from_media_payload(media_payload, slug, md5_id, is_download)
        else:
            source = _legacy_extract(html)

        if not source:
            raise ExtractorError("ABYSS: video link not found")
        if source.startswith("//"):
            source = f"https:{source}"

        playback_headers = {
            "User-Agent": ua,
            "Referer": urljoin(resp.url or web_url, "/"),
        }
        endpoint = "hls_proxy" if ".m3u8" in source.lower() else self.mediaflow_endpoint
        return self._result(source, playback_headers, mediaflow_endpoint=endpoint)
