import urllib.parse


SPECIAL_CDN_DOMAIN = "cccdn.net"
DYNAMIC_WARP_BYPASS_DOMAINS = (
    SPECIAL_CDN_DOMAIN,
    "cinemacity.cc",
    "strem.fun",
    "torrentio.strem.fun",
)
PROTECTED_CURL_DOMAINS = (
    "cinemacity.cc",
    "torrentio",
    "strem.fun",
    "strmd.st",
    # VidXgo CDN playlists/segments are served from *.d2b.you and are
    # noticeably slower through the generic aiohttp path.
    "d2b.you",
)
MANIFEST_ONLY_CURL_DOMAINS = ("torrentio", "strem.fun")


def is_dynamic_warp_bypass_candidate(domain: str) -> bool:
    value = (domain or "").lower()
    return any(pattern in value for pattern in DYNAMIC_WARP_BYPASS_DOMAINS)


def prefer_default_family_for_url(url: str) -> bool:
    return "ai.the-sunmoon.site/key/" in (url or "")


def is_special_cdn_stream(url: str) -> bool:
    return SPECIAL_CDN_DOMAIN in (url or "")


def should_use_curl_cffi(stream_url: str, is_special_cdn: bool, has_curl_cffi: bool) -> bool:
    value = (stream_url or "").lower()
    if not has_curl_cffi or is_special_cdn:
        return False
    if not any(domain in value for domain in PROTECTED_CURL_DOMAINS):
        return False
    if any(domain in value for domain in MANIFEST_ONLY_CURL_DOMAINS):
        return any(ext in value for ext in [".m3u8", ".mpd", "manifest"])
    return True


def prepare_curl_headers(stream_url: str, headers: dict) -> dict:
    curl_headers = dict(headers)
    for key in ("User-Agent", "user-agent"):
        curl_headers.pop(key, None)

    if is_special_cdn_stream(stream_url):
        referer_value = (
            curl_headers.get("Referer")
            or curl_headers.get("referer")
            or "https://cinemacity.cc/"
        )
        curl_headers["Referer"] = referer_value
        try:
            parsed_referer = urllib.parse.urlparse(referer_value)
            if parsed_referer.scheme and parsed_referer.netloc:
                curl_headers["Origin"] = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
            else:
                curl_headers["Origin"] = "https://cinemacity.cc"
        except Exception:
            curl_headers["Origin"] = "https://cinemacity.cc"
        curl_headers["Sec-Fetch-Site"] = "same-site"
        curl_headers["Sec-Fetch-Mode"] = "cors"
        curl_headers["Sec-Fetch-Dest"] = "empty"
        curl_headers.setdefault(
            "Accept-Language",
            "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        )

    curl_headers.setdefault("Accept", "*/*")
    return curl_headers


def final_curl_request_url(stream_url: str) -> str:
    if is_special_cdn_stream(stream_url):
        return urllib.parse.unquote(stream_url)
    return stream_url


def should_use_short_manifest_urls(original_url: str, host_param: str, response_url: str) -> bool:
    original = (original_url or "").lower()
    host = (host_param or "").lower()
    response = (response_url or "").lower()
    return (
        "cinemacity.cc" in original
        or "vidxgo" in original
        or host in {"city", "cinemacity"}
        or host == "vidxgo"
        or SPECIAL_CDN_DOMAIN in response
        or "d2b.you" in response
    )


def is_expired_embed_error(error_msg: str) -> bool:
    value = (error_msg or "").lower()
    return "expired vixsrc embed url" in value or (
        "vixsrc" in value and "expired" in value and "embed" in value
    )


def extractor_name_for_log(extractor) -> str:
    if extractor is None:
        return "unknown"
    return type(extractor).__name__


__all__ = [
    "is_dynamic_warp_bypass_candidate",
    "prefer_default_family_for_url",
    "is_special_cdn_stream",
    "should_use_curl_cffi",
    "prepare_curl_headers",
    "final_curl_request_url",
    "should_use_short_manifest_urls",
    "is_expired_embed_error",
    "extractor_name_for_log",
]
