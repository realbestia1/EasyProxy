"""Ported from daddylive-stream-resolver (MIT) - https://github.com/Lunatic16/daddylive-stream-resolver

Embed-family parsers of `src/decrypt/extractors/*.ts` and
`src/decrypt/crypto/*.ts`, plus the navigation helpers of
`src/api/fetch.ts`. Pure parsing: no network here, the caller supplies
`fetch_html` to `resolve_chain`.
"""

import base64
import json
import re
from urllib.parse import quote, urljoin, urlparse

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Same browser UA the reference resolver sends for HTML hops.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _result(url: str, family: str, page_url: str) -> dict:
    return {"url": url, "family": family, "headers": {"User-Agent": UA, "Referer": page_url}}


# --- dembed ---------------------------------------------------------------

_DEMBED_SRC_RE = re.compile(r'const\s+SRC\s*=\s*"(https?://[^"\s]+\.m3u8)"')
_DEMBED_EDGE_RE = re.compile(r'const\s+EDGE\s*=\s*"(https?://[^"\s]+)"')
_DEMBED_CHANNEL_RE = re.compile(r'const\s+CHANNEL\s*=\s*"([^"\s]+)"')


def extract_dembed_url(html: str) -> str | None:
    """dembed.st premiumtv player: the SRC playlist on the EDGE host.

    The edge only serves a playlist to requests carrying a short-lived token
    minted from `{EDGE}/api/playlist-token?slug={CHANNEL}`: the caller keeps
    using its premium token path.
    """
    src = _DEMBED_SRC_RE.search(html)
    edge = _DEMBED_EDGE_RE.search(html)
    slug = _DEMBED_CHANNEL_RE.search(html)
    if not (src and edge and slug):
        return None
    if urlparse(src.group(1)).hostname != urlparse(edge.group(1)).hostname:
        return None
    return src.group(1)


# --- tiestep / econfig ----------------------------------------------------

_ECONFIG_RE = re.compile(r"window\._econfig\s*=\s*'([^']+)'")
_ECONFIG_ORDER = (2, 0, 3, 1)
_ECONFIG_PARTS = 4


def _atob_binary(value: str) -> str:
    return base64.b64decode(value).decode("latin1")


def decrypt_econfig(b64: str) -> dict | None:
    """Port of crypto/econfig.ts: 4 chunks, reorder [2,0,3,1], drop byte 3."""
    if not b64:
        return None
    try:
        raw = _atob_binary(b64)
        chunk_size = -(-len(raw) // _ECONFIG_PARTS)
        chunks = [raw[i * chunk_size:(i + 1) * chunk_size] for i in range(_ECONFIG_PARTS)]
        reordered: list[str] = [""] * _ECONFIG_PARTS
        for i, target in enumerate(_ECONFIG_ORDER):
            piece = chunks[i][:3] + chunks[i][4:]
            reordered[target] = _atob_binary(piece)
        config = json.loads(base64.b64decode("".join(reordered)).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    if not config.get("stream_url") and not config.get("stream_url_nop2p"):
        return None
    return config


def econfig_playable_url(config: dict) -> str:
    if config.get("p2p") is False or config.get("url_nop2p"):
        return config.get("stream_url_nop2p") or config.get("stream_url") or ""
    return config.get("stream_url") or config.get("stream_url_nop2p") or ""


def extract_tiestep_m3u8(html: str) -> str | None:
    match = _ECONFIG_RE.search(html)
    if not match:
        return None
    config = decrypt_econfig(match.group(1))
    if not config:
        return None
    url = econfig_playable_url(config)
    return url if ".m3u8" in url else None


# --- epiembeds ------------------------------------------------------------

_EPI_ARRAY_RE = re.compile(r"var\s+_gr\w+=\[([0-9,\s]+)\],_mz\d+=(\d+),_mz\d+=(\d+)")
_PLUS_ARRAY_RE = re.compile(r"var\s+_\w+=\[([^\]]+)\],_\w+=(\d+),_\w+=(\d+)")


def _decode_xor_array(nums: list[int], xor: int, sub: int) -> str:
    return "".join(chr(((n ^ xor) - sub + 256) & 255) for n in nums)


def deobfuscate_xor_script(html: str) -> str | None:
    match = _EPI_ARRAY_RE.search(html) or _PLUS_ARRAY_RE.search(html)
    if not match:
        return None
    try:
        nums = [int(n.strip()) for n in match.group(1).split(",")]
        return _decode_xor_array(nums, int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _m3u8_from_text(source: str) -> str | None:
    for pattern in (
        r'SIGNED_URL\s*=\s*"([^"]+\.m3u8[^"]*)"',
        r'url\s*=\s*"([^"]+\.m3u8[^"]*)"',
        r'https://[^"\'\\\s]+\.m3u8[^"\'\\\s]*',
    ):
        match = re.search(pattern, source)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def extract_epiembeds_m3u8(html: str) -> str | None:
    return _m3u8_from_text(deobfuscate_xor_script(html) or html)


# --- hub ------------------------------------------------------------------

_HUB_BLOCK_RE = re.compile(
    r"const ENCRYPTED_CONFIG = \{[\s\S]*?cipher:\s*'([^']+)'"
    r"[\s\S]*?key:\s*'([^']+)'[\s\S]*?iv:\s*'([^']+)'"
)


def aes_cbc_decrypt(cipher_b64: str, key_b64: str, iv_b64: str) -> str:
    """Port of crypto/aes-cbc.ts (key length picks the flavour)."""
    ciphertext = base64.b64decode(cipher_b64)
    key = base64.b64decode(key_b64)
    iv = base64.b64decode(iv_b64)
    if len(key) not in (16, 24, 32):
        raise ValueError(f"unsupported AES key length {len(key)}")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def decrypt_hub_config(html: str) -> dict | None:
    match = _HUB_BLOCK_RE.search(html)
    if not match:
        return None
    try:
        config = json.loads(aes_cbc_decrypt(match.group(1), match.group(2), match.group(3)))
    except Exception:
        return None
    if not isinstance(config, dict) or not config.get("baseUrl") or not config.get("streamId"):
        return None
    return config


def extract_hub_webm(html: str) -> str | None:
    config = decrypt_hub_config(html)
    if not config:
        return None
    if config.get("initialVideoUrl"):
        return config["initialVideoUrl"]
    if config.get("initialToken") and config.get("initialCode"):
        base = config["baseUrl"].rstrip("/")
        return f"{base}/{config['initialToken']}/{config['initialCode']}/{config['streamId']}/webm/"
    return None


# --- livelive24 -----------------------------------------------------------

_LIVE24_SOURCE_RE = re.compile(r'<source[^>]+src="(https://[^"]+/webm/[^"]*)"', re.I)


def extract_livelive24_webm(html: str) -> str | None:
    match = _LIVE24_SOURCE_RE.search(html)
    return match.group(1) if match else None


# --- wideiptv -------------------------------------------------------------

_WIDEIPTV_URL_RE = re.compile(r'streamUrl:\s*"((?:\\/|[^"])+)"')


def extract_wideiptv_m3u8(html: str) -> str | None:
    match = _WIDEIPTV_URL_RE.search(html)
    return match.group(1).replace("\\/", "/") if match else None


# --- cdnlivetv ------------------------------------------------------------

_CDN_DECODER_RE = re.compile(r"function (\w+)\(s\)\{[^}]*atob")
_CDN_VAR_RE = re.compile(r"var (\w+)='([A-Za-z0-9+/=_-]+)'")


def _decode_chunk(value: str) -> str:
    padded = value.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    return base64.b64decode(padded).decode("utf-8", errors="ignore")


def extract_cdnlivetv_m3u8(html: str) -> str | None:
    decoder_match = _CDN_DECODER_RE.search(html)
    decoder = decoder_match.group(1) if decoder_match else None
    assign = re.search(
        r"var (\w+)=((?:%s\(\w+\)\+?)+);\s*var _p2pMode"
        % (re.escape(decoder) if decoder else r"\w+"),
        html,
    )
    if not decoder or not assign:
        return None
    chunk_vars = dict(_CDN_VAR_RE.findall(html))
    url = "".join(
        _decode_chunk(chunk_vars.get(name, ""))
        for name in re.findall(r"%s\((\w+)\)" % re.escape(decoder), assign.group(2))
    )
    return url if ".m3u8" in url else None


# --- daddy3 ---------------------------------------------------------------

_DADDY3_ATOB_RE = re.compile(r"atob\('([^']+)'\)")
_DADDY3_M3U8_RE = re.compile(
    r"https://[^\"'`\s]+/three/secure/[0-9a-f]{32}/\d+/premium\d+/index\.m3u8"
)


def extract_daddy3_m3u8(html: str) -> str | None:
    match = _DADDY3_ATOB_RE.search(html)
    if match:
        try:
            url = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
        except Exception:
            return None
        if url.startswith("http") and ".m3u8" in url:
            return url
    found = _DADDY3_M3U8_RE.search(html)
    return found.group(0) if found else None


# --- igniteandship --------------------------------------------------------

_IGNITE_LOAD_RE = re.compile(r"player\.load\(\{source:\s*(\w+)\(\)")


def extract_ignite_m3u8(html: str) -> str | None:
    """Player URL is split in string pieces plus the text of one <span>."""
    fn_match = _IGNITE_LOAD_RE.search(html)
    if not fn_match:
        return None
    body_match = re.search(
        r"function %s\(\)\s*\{([\s\S]*?)\n\s*\}" % re.escape(fn_match.group(1)), html
    )
    if not body_match:
        return None
    body = body_match.group(1)
    chars_match = re.search(r"return\(\[([^\]]+)\]", body)
    if not chars_match:
        return None
    base = "".join(re.findall(r'"([^"]*)"', chars_match.group(1)))
    span_id = re.search(r'getElementById\("([^"]+)"\)', body)
    span = ""
    if span_id:
        span_match = re.search(r'id=["\']%s["\'][^>]*>([^<]*)' % re.escape(span_id.group(1)), html)
        span = span_match.group(1) if span_match else ""
    url = (base + span).replace("\\/", "/")
    return url if url.startswith("http") and ".m3u8" in url else None


# --- blogger-player / wikisport hop pages ---------------------------------

_BLOGGER_ZDEC_RE = re.compile(r'zdec\s*=\s*"([^"]+)"')


def extract_blogger_zdec_url(html: str) -> str | None:
    match = _BLOGGER_ZDEC_RE.search(html)
    if not match:
        return None
    try:
        script = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
        inner = re.search(r"atob\('([^']+)'\)", script)
        if not inner:
            return None
        url = base64.b64decode(inner.group(1)).decode("utf-8", errors="ignore").strip()
    except Exception:
        return None
    return url if url.startswith("http") else None


def is_blogger_player_page(url: str) -> bool:
    return "blogspot.com" in url


_WIKISPORT_FID_RE = re.compile(r"""fid\s*=\s*["']([^"']+)["']""")


def extract_wikisport_live_id(html: str) -> str | None:
    match = _WIKISPORT_FID_RE.search(html)
    return match.group(1) if match else None


def build_wiki_php_url(live_id: str) -> str:
    return (
        "https://igniteandship.com/wiki.php?player=desktop&live="
        f"{quote(live_id, safe='')}"
    )


def is_wikisport_page(url: str) -> bool:
    return "wikisport" in url


# --- navigation helpers ---------------------------------------------------

_IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]+)"', re.I)


def inner_frame_url(html: str, page_url: str) -> str | None:
    """Port of extractInnerFrameUrl: skip JS-built or non-http frame sources."""
    match = _IFRAME_RE.search(html)
    if not match:
        return None
    src = match.group(1).strip()
    if not src or re.search(r"\+|'|window\.|javascript:|about:|data:", src, re.I):
        return None
    absolute = urljoin(page_url, src)
    return absolute if urlparse(absolute).scheme in {"http", "https"} else None


def next_scripted_url(html: str, page_url: str) -> str | None:
    """Port of nextScriptedUrl: the hop a blogger/wiki page builds in its script."""
    if is_wikisport_page(page_url):
        live_id = extract_wikisport_live_id(html)
        if live_id:
            return build_wiki_php_url(live_id)
    if is_blogger_player_page(page_url):
        return extract_blogger_zdec_url(html)
    return None


async def resolve_chain(
    html: str,
    page_url: str,
    fetch_html,
    max_hops: int = 6,
) -> tuple[str, str]:
    """Port of api/fetch.ts::fetchEmbedHtmlChain.

    Follows inner iframes, then the scripted hop of blogger/wiki pages,
    stopping at the first document `extract_playable` understands.
    Returns `(html, url)` of the last document fetched.
    """
    current, referer = html, page_url
    for _ in range(max_hops):
        if extract_playable(current, referer):
            break
        target = inner_frame_url(current, referer) or next_scripted_url(current, referer)
        if not target or target == referer:
            break
        body = await fetch_html(target, referer)
        if not body:
            break
        current, referer = body, target
    return current, referer


# --- dispatch (same order as extractors/embed.ts) -------------------------

_FAMILIES = (
    ("dembed", extract_dembed_url),
    ("tiestep", extract_tiestep_m3u8),
    ("epiembeds", extract_epiembeds_m3u8),
    ("hub", extract_hub_webm),
    ("livelive24", extract_livelive24_webm),
    ("wideiptv", extract_wideiptv_m3u8),
    ("cdnlivetv", extract_cdnlivetv_m3u8),
    ("daddy3", extract_daddy3_m3u8),
    ("ignite", extract_ignite_m3u8),
)


def extract_playable(html: str, page_url: str) -> dict | None:
    """Return {"url": str, "headers": dict, "family": str} or None."""
    if not html:
        return None
    for family, extract in _FAMILIES:
        try:
            url = extract(html)
        except Exception:
            continue
        if url:
            return _result(url, family, page_url)
    return None
