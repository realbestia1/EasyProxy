"""Extractor resolution: ``?host=`` aliases plus URL auto-detection.

One table drives both lookups. Adding an extractor means adding one ``_Entry``
line here plus the module import in ``registry_imports`` (see extractors/AGENTS.md).

Fields:
  name                 registry attribute / canonical extractor name
  cls                  extractor class (None when the module failed to import)
  hosts                ``?host=`` aliases (lower-case)
  match                URL predicate for auto-detection
  url_proxy            proxy lookup target when auto-detected: "url", a literal
                       host, or a callable(url, bypass_warp). With
                       url_proxy_final the callable returns the proxy itself.
  host_proxy           override for the ``?host=`` lookup target (default: the
                       alias itself); "url" resolves against the real URL
  key_name             cache key when it differs from the alias (canonical name)
  host_build_name      extractor name passed to ``_build_proxy_list`` for host
                       lookups (default: the alias)
  proxy_extractor_name ``extractor_name`` forwarded to ``get_proxy_for_url``
  refresh_headers      re-bind request headers on every hit (per-call h_* args)
  url_proxy_final      url_proxy already yields the proxy (no further lookup)
"""

import re
import urllib.parse
from collections import namedtuple

import config as _config
from config import (
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    BYPASS_PROXIES_CONTEXT,
    BYPASS_WARP_CONTEXT,
    get_proxy_for_url,
    get_extractor_proxies,
)
from extractors.base import ExtractorError
from extractors.generic import GenericHLSExtractor
from extractors.registry_imports import *
# Single source of truth for the mirror frontends, defined next to the extractor
# itself (extractors/vavoo.py) so the two can't drift apart. Tuple, not list: it
# is a fixed shared constant imported by reference — any importer could
# accidentally mutate a list in place.
from extractors.vavoo import VAVOO_MIRROR_DOMAINS

_SPORTSONLINE_PATH_PATTERNS = (
    re.compile(r"/channels/[a-z0-9_-]+/[a-z0-9_-]+\.php(?:$|[?#])", re.IGNORECASE),
    re.compile(r"/hd/hd\d+\.php(?:$|[?#])", re.IGNORECASE),
)

_FCTV33_MARKER = re.compile(r"(?:#|%23)fctv33(?![a-z0-9])", re.IGNORECASE)
_MEDIA_EXTENSIONS = (
    ".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".mov", ".flv", ".wmv",
    ".mp3", ".aac", ".m4a", ".mpd",
)


def _is_sportsonline_candidate(value: str) -> bool:
    raw_value = (value or "").strip().lower()
    return any(pattern.search(raw_value) for pattern in _SPORTSONLINE_PATH_PATTERNS)


def _resolve_sportsonline_proxy(url: str, bypass_warp: bool = False) -> str | None:
    # Priority requested: real URL first, then legacy aliases.
    ordered_candidates = [url, "sportzsonline", "sportzonline", "sportsonline", "sportsonlline", "sportsonlinne"]
    for candidate in ordered_candidates:
        if any(
            route.get("url") and route["url"] in candidate for route in _config.TRANSPORT_ROUTES
        ):
            return get_proxy_for_url(candidate, bypass_warp=bypass_warp)
    return get_proxy_for_url(url, bypass_warp=bypass_warp)


def _build_proxy_list(primary_proxy: str | None = None, extractor_name: str | None = None) -> list[str]:
    """Build the extractor's fallback list without enabling direct implicitly."""
    proxies = []
    selected_proxy = SELECTED_PROXY_CONTEXT.get()
    if selected_proxy and STRICT_PROXY_CONTEXT.get():
        # A relay URL may carry a WARP proxy selected before the admin switch
        # changed. Never freeze that stale route into a cached extractor.
        if not (
            _config.is_warp_proxy_url(selected_proxy)
            and (
                BYPASS_WARP_CONTEXT.get()
                or not _config._get_dynamic_warp_enabled()
            )
        ):
            return [selected_proxy]
        selected_proxy = None
    if BYPASS_PROXIES_CONTEXT.get():
        return []
    extractor_proxies = get_extractor_proxies(extractor_name or "")
    _GLOBAL_PROXIES = _config.GLOBAL_PROXIES

    # The URL-aware resolver applies the exact route priority on every request.
    # Keep this cached list complete so extractor implementations that retain
    # their own session can still fail over to global/WARP.
    candidates = (
        ([selected_proxy] if selected_proxy else [])
        + list(extractor_proxies)
        + ([primary_proxy] if primary_proxy else [])
        + list(_GLOBAL_PROXIES)
    )
    for proxy in candidates:
        if _config.is_warp_proxy_url(proxy):
            continue
        if proxy and proxy not in proxies:
            proxies.append(proxy)

    if (
        _config._get_dynamic_warp_enabled()
        and not BYPASS_WARP_CONTEXT.get()
        and not _config._is_warp_excluded(extractor_name or "")
        and not any(_config.is_warp_proxy_url(proxy) for proxy in proxies)
    ):
        proxies.append(_config.WARP_PROXY_URL)
    return proxies


def _cache_key(name: str, bypass_warp: bool = False) -> str:
    """Extractor cache key reflecting routing state (warp/proxy bypass)."""
    base = f"{name}_direct" if bypass_warp else name
    if BYPASS_PROXIES_CONTEXT.get():
        base += "_noproxy"
    return base


# --- match helpers -----------------------------------------------------------

def _sub(*needles, lower=False):
    if lower:
        lowered = tuple(needle.lower() for needle in needles)
        return lambda url, parsed: any(needle in url.lower() for needle in lowered)
    return lambda url, parsed: any(needle in url for needle in needles)


def _rex(pattern, flags=0, unquote=False):
    rx = re.compile(pattern, flags)
    if unquote:
        return lambda url, parsed: rx.search(urllib.parse.unquote(url)) is not None
    return lambda url, parsed: rx.search(url) is not None


def _host_endswith(suffix):
    return lambda url, parsed: (parsed.hostname or "").lower().endswith(suffix)


def _any_of(*preds):
    return lambda url, parsed: any(pred(url, parsed) for pred in preds)


def _all_of(*preds):
    return lambda url, parsed: all(pred(url, parsed) for pred in preds)


def _no_media_extension(url, parsed):
    return not any(url.endswith(ext) or f"{ext}?" in url for ext in _MEDIA_EXTENSIONS)


# --- extractor table (order = auto-detection priority) -----------------------

_Entry = namedtuple(
    "_Entry",
    "name cls hosts match url_proxy host_proxy key_name host_build_name "
    "proxy_extractor_name refresh_headers url_proxy_final",
    defaults=((), None, "url", None, None, None, "", False, False),
)

_ENTRIES = (
    _Entry("ads", ADSExtractor, hosts=("ads",), host_proxy="url"),
    _Entry("mediaset", MediasetExtractor, hosts=("mediaset", "mediasetinfinity"),
           match=_sub("mediasetinfinity.mediaset.it/"), key_name="mediaset"),
    _Entry("wittytv", WittyTVExtractor, hosts=("witty", "wittytv"),
           match=_sub("wittytv.it/"), key_name="wittytv"),
    _Entry("raiplay", RaiPlayExtractor, hosts=("rai", "raiplay"),
           match=_any_of(_sub("mediapolisvod.rai.it/relinker/"), _host_endswith("raiplay.it")),
           key_name="raiplay"),
    # All mirror frontends share the same mediahubmx backend (same signature,
    # catalog and cross-compatible resolve), so auto-detect must match every
    # domain, not just vavoo.to (lower=True => case-insensitive). "url" +
    # proxy_extractor_name makes the routing layer see the real host for
    # TRANSPORT_ROUTES/warp-exclusion matching instead of the literal.
    _Entry("vavoo", VavooExtractor, hosts=("vavoo",),
           match=_sub(*VAVOO_MIRROR_DOMAINS, lower=True),
           url_proxy="url", proxy_extractor_name="vavoo"),
    _Entry("vixsrc", VixSrcExtractor, hosts=("vixsrc",),
           match=_all_of(_sub("vixsrc.to/", "unitv.mom/", lower=True),
                         _sub("/movie/", "/tv/", "/iframe/", "/embed/", "/playlist/")),
           url_proxy=lambda url, bypass_warp: urllib.parse.urlparse(url).netloc or "vixsrc.to"),
    _Entry("vixcloud", VixSrcExtractor, hosts=("vixcloud",),
           match=_all_of(_sub("vixcloud.co/", "unitv.mom/", lower=True),
                         _sub("/embed/", "/playlist/", lower=True)),
           url_proxy=lambda url, bypass_warp: urllib.parse.urlparse(url).netloc or "vixcloud.co"),
    _Entry("sportsonline", SportsonlineExtractor,
           hosts=("sportsonline",),
           match=lambda url, parsed: _is_sportsonline_candidate(url),
           key_name="sportsonline",
           url_proxy=lambda url, bypass_warp: _resolve_sportsonline_proxy(url, bypass_warp),
           url_proxy_final=True),
    _Entry("streamhg", StreamHGExtractor, hosts=("streamhg",),
           match=_all_of(_rex(r"/e/[^/?#]+", re.IGNORECASE),
                         _sub("dhcplay.com/", "vibuxer.com/", "streamhg.com/",
                              "masukestin.com/", lower=True)),
           url_proxy="streamhg"),
    _Entry("mixdrop", MixdropExtractor, hosts=("mixdrop", "m1xdrop"),
           match=_sub("mixdrop", "m1xdrop"), url_proxy="mixdrop"),
    _Entry("voe", VoeExtractor, hosts=("voe",),
           match=_sub("voe.sx", "voe.to", "voe.st", "voe.eu", "voe.la", "voe-network.net"),
           url_proxy="voe.sx"),
    _Entry("freeshot", FreeshotExtractor, hosts=("freeshot",),
           match=_sub("popcdn.day", "freeshot.live"),
           url_proxy=lambda url, bypass_warp: "popcdn.day" if "popcdn.day" in url else "freeshot.live"),
    _Entry("streamtape", StreamtapeExtractor, hosts=("streamtape",),
           match=_sub("streamtape.com", "streamtape.to", "streamtape.net"),
           url_proxy="streamtape"),
    _Entry("orion", OrionExtractor, hosts=("orion",), match=_sub("orionoid.com"),
           url_proxy="orionoid.com"),
    _Entry("doodstream", DoodStreamExtractor, hosts=("doodstream", "dood", "d000d"),
           match=_sub("doodstream", "d000d.com", "dood.wf", "dood.cx", "dood.la",
                      "dood.so", "dood.pm"),
           host_proxy="url", key_name="doodstream"),
    _Entry("fastream", FastreamExtractor, hosts=("fastream",), match=_sub("fastream"),
           url_proxy="fastream"),
    _Entry("filelions", FileLionsExtractor, hosts=("filelions",), match=_sub("filelions"),
           url_proxy="filelions"),
    _Entry("filemoon", FileMoonExtractor, hosts=("filemoon",), match=_sub("filemoon"),
           url_proxy="filemoon"),
    _Entry("dlstreams", DLStreamsExtractor, hosts=("dlhd", "dlstreams"),
           match=_rex(r"(/watch\.php\?.*id=\d+|/(?:stream|cast|watch|plus|casting|player|hub)/stream-[\w-]+\.php)",
                      unquote=True),
           key_name="dlstreams"),
    _Entry("lulustream", LuluStreamExtractor, hosts=("lulustream",), match=_sub("lulustream"),
           url_proxy="lulustream"),
    _Entry("okru", OkruExtractor, hosts=("okru", "ok.ru"),
           match=_sub("ok.ru", "odnoklassniki"), url_proxy="ok.ru", key_name="okru"),
    _Entry("streamwish", StreamWishExtractor, hosts=("streamwish",),
           match=_sub("streamwish", "swish", "wishfast", "embedwish", "wishembed"),
           url_proxy="streamwish"),
    _Entry("supervideo", SupervideoExtractor, hosts=("supervideo",), match=_sub("supervideo"),
           url_proxy="supervideo"),
    _Entry("vidxgo", VidXgoExtractor, hosts=("vidxgo",), match=_sub("vidxgo", lower=True),
           url_proxy="vidxgo", key_name="vidxgo", refresh_headers=True),
    _Entry("dropload", DroploadExtractor, hosts=("dropload",),
           match=_any_of(_sub("dropload"), _sub("dr0pstream", lower=True)),
           url_proxy="dropload"),
    _Entry("uqload", UqloadExtractor, hosts=("uqload",),
           match=_all_of(_sub("uqload"), _no_media_extension), url_proxy="uqload"),
    _Entry("vidmoly", VidmolyExtractor, hosts=("vidmoly",), match=_sub("vidmoly"),
           url_proxy="vidmoly"),
    _Entry("vidoza", VidozaExtractor, hosts=("vidoza", "videzz"),
           match=_sub("vidoza", "videzz"), url_proxy="vidoza", key_name="vidoza"),
    _Entry("turbovidplay", TurboVidPlayExtractor,
           hosts=("turbovidplay", "turboviplay", "emturbovid"),
           match=_sub("turboviplay", "emturbovid", "tuborstb", "javggvideo", "stbturbo",
                      "turbovidhls"),
           url_proxy="turbovidplay", key_name="turbovidplay"),
    _Entry("f16px", F16PxExtractor, hosts=("f16px", "byse"),
           match=_all_of(_sub("/e/"), _sub("f16px", "embedme", "embedsb", "playersb")),
           url_proxy="f16px", host_proxy="f16px", key_name="f16px", host_build_name="f16px"),
    _Entry("guardabest", GuardabestExtractor, hosts=("guardabest", "guardabestvid"),
           match=_sub("guardabestvid.cam", lower=True),
           url_proxy="guardabestvid", host_proxy="guardabestvid",
           key_name="guardabest", host_build_name="guardabest"),
    _Entry("sports99", Sports99Extractor, hosts=("sports99", "cdnlivetv"),
           match=_sub("cdnlivetv.tv", "cdnlivetv.ru"),
           url_proxy="cdnlivetv.tv", key_name="sports99"),
    _Entry("embedst", EmbedStExtractor,
           hosts=("embedst", "embedsports", "embed.st", "embedsports.top", "streamed",
                  "streamed.pk"),
           match=_any_of(_sub("embed.st/embed/", lower=True),
                         _sub("embedsports.top/embed/", lower=True),
                         _sub("streamed.pk/watch/", lower=True)),
           url_proxy="embed.st", key_name="embedst"),
    _Entry("vidsonic", VidSonicExtractor, hosts=("vidsonic",),
           match=_all_of(_sub("vidsonic.net/", "vixeo.io/", lower=True),
                         _rex(r"/e/[A-Za-z0-9]+", re.IGNORECASE)),
           url_proxy="vidsonic"),
    _Entry("vidlink", VidLinkExtractor, hosts=("vidlink",),
           match=_rex(r"vidlink\.pro/(?:movie/|tv/)", re.IGNORECASE),
           url_proxy="vidlink.pro"),
    _Entry("vidfast", VidFastExtractor, hosts=("vidfast", "vidfast.vc"),
           match=_rex(r"(?:www\.)?vidfast\.vc/(?:movie/|tv/)", re.IGNORECASE),
           url_proxy="vidfast.vc", key_name="vidfast"),
    _Entry("cinejoy", CinejoyExtractor, hosts=("cinejoy",),
           match=_rex(r"(?:www\.)?cinejoy\.[a-z]{2,}/", re.IGNORECASE),
           url_proxy="url", key_name="cinejoy"),
    _Entry("fctv33", Fctv33Extractor, hosts=("fctv33", "fctv", "fctv33hd"),
           match=_any_of(_sub("fctv33", lower=True),
                         lambda url, parsed: url.lower().startswith("fctv33://"),
                         _sub("tcdru136ovur.ru", "tcpwe138stya.ru",
                              "mpgreatestclgczbmiddle", lower=True)),
           url_proxy="url", host_proxy="url", key_name="fctv33",
           host_build_name="fctv33", proxy_extractor_name="fctv33"),
    _Entry("livetv", LiveTVExtractor, hosts=("livetv",), match=_sub("livetv", lower=True),
           url_proxy="livetv.sx"),
)

_BY_NAME = {entry.name: entry for entry in _ENTRIES}
_HOST_INDEX = {alias: entry for entry in _ENTRIES for alias in entry.hosts}


def _entry_for_host(host: str):
    entry = _HOST_INDEX.get(host)
    if entry is not None:
        return entry
    if _is_sportsonline_candidate(host):
        return _BY_NAME["sportsonline"]
    if host.startswith("cinejoy."):
        return _BY_NAME["cinejoy"]
    return None


async def resolve_extractor(self, url: str, request_headers: dict, host: str = None, bypass_warp: bool = False):
    """Ottiene l'estrattore appropriato per l'URL"""
    try:
        # 1. Selezione manuale tramite parametro 'host'
        if host:
            host = host.lower()
            entry = _entry_for_host(host)
            if entry is not None:
                return _instance(self, entry, request_headers, bypass_warp, url=url, host=host)

        # 2. Auto-detection basata sull'URL
        parsed_url = urllib.parse.urlparse(url)
        ads_host = (parsed_url.hostname or "").lower()
        ads_host_ok = bool(
            ADS_HOST_PATTERN.fullmatch(ads_host)
            or ads_host == ads_configured_host()
        )
        ads_path_ok = bool(
            ADS_FILM_PATTERN.fullmatch(parsed_url.path)
            or ADS_SERIES_PATTERN.fullmatch(parsed_url.path)
        )
        if parsed_url.path.startswith("/api/player-sources/") or (ads_host_ok and ads_path_ok):
            return _instance(self, _BY_NAME["ads"], request_headers, bypass_warp, url=url)

        # FCTV33: link diretto firmato con marker #fctv33&matchId=..&streamId=.. (generato da
        # fctv33_omg_universal). Va riconosciuto PRIMA dello shortcut "link diretto" sotto,
        # altrimenti finirebbe nel generic con un token legato all'IP/scaduto.
        if _FCTV33_MARKER.search(url):
            return _instance(self, _BY_NAME["fctv33"], request_headers, bypass_warp, url=url)

        # ✅ NUOVO: Salta estrattori specifici se l'URL sembra già un link diretto a un media
        # (evita di provare a estrarre un .mp4 come se fosse una pagina HTML)
        path_lower = url.split('?')[0].lower()
        if any(path_lower.endswith(ext) for ext in _MEDIA_EXTENSIONS):
            return _generic_instance(self, request_headers, bypass_warp)

        for entry in _ENTRIES:
            if entry.match is not None and entry.match(url, parsed_url):
                return _instance(self, entry, request_headers, bypass_warp, url=url)

        # ✅ Fallback al GenericHLSExtractor per qualsiasi altro URL: gestisce
        # estensioni sconosciute o URL senza estensione.
        return _generic_instance(self, request_headers, bypass_warp)
    except (NameError, TypeError) as e:
        raise ExtractorError(f"Extractor not available - module missing: {e}")


def _cached(self, key_name: str, cls, request_headers, proxy_list, bypass_warp, refresh_headers=False):
    if cls is None:
        raise RuntimeError(f"{key_name} extractor module not available")
    key = _cache_key(key_name, bypass_warp)
    instance = self.extractors.get(key)
    if instance is None:
        instance = cls(request_headers, proxies=proxy_list, bypass_warp=bypass_warp)
        self.extractors[key] = instance
    if refresh_headers:
        # Always refresh request_headers so per-call h_* overrides are honored.
        instance.request_headers = request_headers
    return instance


def _instance(self, entry, request_headers, bypass_warp, url=None, host=None):
    """Build (or reuse) the extractor for a host alias or an auto-detected URL."""
    if host is not None:
        target = entry.host_proxy or host
        if target == "url":
            target = url
        key_name = entry.key_name or host
        build_name = entry.host_build_name or host
    else:
        target = entry.url_proxy
        if callable(target):
            target = target(url, bypass_warp)
        elif target == "url":
            target = url
        key_name = build_name = entry.name

    if entry.url_proxy_final and host is None:
        proxy = target
    else:
        kwargs = {"extractor_name": entry.proxy_extractor_name} if entry.proxy_extractor_name else {}
        proxy = get_proxy_for_url(target, bypass_warp=bypass_warp, **kwargs)
    return _cached(self, key_name, entry.cls, request_headers,
                   _build_proxy_list(proxy, build_name), bypass_warp, entry.refresh_headers)


def _generic_instance(self, request_headers, bypass_warp):
    return _cached(self, "hls_generic", GenericHLSExtractor, request_headers,
                   _build_proxy_list(None, "generic"), bypass_warp)


__all__ = ["resolve_extractor", "ExtractorError"]
