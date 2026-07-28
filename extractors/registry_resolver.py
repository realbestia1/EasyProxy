import logging
import re
import urllib.parse

import config as _config
from config import SELECTED_PROXY_CONTEXT, STRICT_PROXY_CONTEXT, get_proxy_for_url, get_extractor_proxies
from extractors.generic import GenericHLSExtractor, ExtractorError
from extractors.registry_imports import *

logger = logging.getLogger("extractors.registry")

_SPORTSONLINE_PATH_PATTERNS = (
    re.compile(r"/channels/[a-z0-9_-]+/[a-z0-9_-]+\.php(?:$|[?#])", re.IGNORECASE),
    re.compile(r"/hd/hd\d+\.php(?:$|[?#])", re.IGNORECASE),
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
    """Build proxy list; explicit/extractor proxies are strict and exclude globals."""
    proxies = []
    selected_proxy = SELECTED_PROXY_CONTEXT.get()
    if selected_proxy and STRICT_PROXY_CONTEXT.get():
        return [selected_proxy]
    extractor_proxies = get_extractor_proxies(extractor_name or "")
    if extractor_proxies:
        return extractor_proxies
    _GLOBAL_PROXIES = _config.GLOBAL_PROXIES
    for proxy in ([selected_proxy] if selected_proxy else []) + ([primary_proxy] if primary_proxy else []) + list(_GLOBAL_PROXIES):
        if proxy and proxy not in proxies:
            proxies.append(proxy)
    return proxies


async def resolve_extractor(self, url: str, request_headers: dict, host: str = None, bypass_warp: bool = False):
    """Ottiene l'estrattore appropriato per l'URL"""
    try:
        # 1. Selezione Manuale tramite parametro 'host'
        if host:
            host = host.lower()
            # ✅ FIX: Usa una chiave di cache che include lo stato del WARP per evitare contaminazioni
            key = f"{host}_direct" if bypass_warp else host

            # ✅ FIX: Calcola il proxy corretto in base a bypass_warp invece di usare GLOBAL_PROXIES indiscriminatamente
            proxy_lookup_target = url if host in ["doodstream", "dood", "d000d"] else host
            proxy = get_proxy_for_url(
        proxy_lookup_target,
        bypass_warp=bypass_warp,
    )
            proxy_list = _build_proxy_list(proxy, host)

            if host == "vavoo":
                if key not in self.extractors:
                    self.extractors[key] = VavooExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "vixsrc":
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(
                        request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                    )
                return self.extractors[key]
            elif host == "vixcloud":
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(
                        request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                    )
                return self.extractors[key]
            elif _is_sportsonline_candidate(host):
                key = "sportsonline_direct" if bypass_warp else "sportsonline"
                if key not in self.extractors:
                    self.extractors[key] = SportsonlineExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in {"mixdrop", "m1xdrop"}:
                if key not in self.extractors:
                    self.extractors[key] = MixdropExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "voe":
                if key not in self.extractors:
                    self.extractors[key] = VoeExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "streamtape":
                if key not in self.extractors:
                    self.extractors[key] = StreamtapeExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "orion":
                if key not in self.extractors:
                    self.extractors[key] = OrionExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "freeshot":
                if key not in self.extractors:
                    self.extractors[key] = FreeshotExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            # --- New Extractors (host selection) ---
            elif host in ["doodstream", "dood", "d000d"]:
                key = "doodstream_direct" if bypass_warp else "doodstream"
                if key not in self.extractors:
                    self.extractors[key] = DoodStreamExtractor(
                        request_headers,
                        proxies=proxy_list,
                    )
                return self.extractors[key]
            elif host == "fastream":
                if key not in self.extractors:
                    self.extractors[key] = FastreamExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "filelions":
                if key not in self.extractors:
                    self.extractors[key] = FileLionsExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "filemoon":
                if key not in self.extractors:
                    self.extractors[key] = FileMoonExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "lulustream":
                if key not in self.extractors:
                    self.extractors[key] = LuluStreamExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]

            elif host in ["okru", "ok.ru"]:
                key = "okru_direct" if bypass_warp else "okru"
                if key not in self.extractors:
                    self.extractors[key] = OkruExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "streamwish":
                if key not in self.extractors:
                    self.extractors[key] = StreamWishExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]

            elif host == "streamhg":
                if key not in self.extractors:
                    self.extractors[key] = StreamHGExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "supervideo":
                if key not in self.extractors:
                    self.extractors[key] = SupervideoExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "dropload":
                if key not in self.extractors:
                    self.extractors[key] = DroploadExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "uqload":
                if key not in self.extractors:
                    self.extractors[key] = UqloadExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "vidmoly":
                if key not in self.extractors:
                    self.extractors[key] = VidmolyExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in ["vidoza", "videzz"]:
                key = "vidoza_direct" if bypass_warp else "vidoza"
                if key not in self.extractors:
                    self.extractors[key] = VidozaExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in ["turbovidplay", "turboviplay", "emturbovid"]:
                key = "turbovidplay_direct" if bypass_warp else "turbovidplay"
                if key not in self.extractors:
                    self.extractors[key] = TurboVidPlayExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "livetv":
                if key not in self.extractors:
                    self.extractors[key] = LiveTVExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host == "f16px":
                if key not in self.extractors:
                    self.extractors[key] = F16PxExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in ["sports99", "cdnlivetv"]:
                key = "sports99_direct" if bypass_warp else "sports99"
                if key not in self.extractors:
                    self.extractors[key] = Sports99Extractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in ["dlhd", "dlstreams"]:
                key = "dlstreams_direct" if bypass_warp else "dlstreams"
                if key not in self.extractors:
                    self.extractors[key] = DLStreamsExtractor(
                        request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                    )
                return self.extractors[key]
            elif host in ["embedst", "embedsports", "embed.st", "embedsports.top", "streamed", "streamed.pk"]:
                key = "embedst_direct" if bypass_warp else "embedst"
                if key not in self.extractors:
                    self.extractors[key] = EmbedStExtractor(
                        request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                    )
                return self.extractors[key]
            elif host == "vidsonic":
                if key not in self.extractors:
                    self.extractors[key] = VidSonicExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in {"mediaset", "mediasetinfinity"}:
                key = "mediaset_direct" if bypass_warp else "mediaset"
                if MediasetExtractor is None:
                    raise RuntimeError("MediasetExtractor module not available")
                if key not in self.extractors:
                    self.extractors[key] = MediasetExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in {"witty", "wittytv"}:
                key = "wittytv_direct" if bypass_warp else "wittytv"
                if WittyTVExtractor is None:
                    raise RuntimeError("WittyTVExtractor module not available")
                if key not in self.extractors:
                    self.extractors[key] = WittyTVExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]
            elif host in {"rai", "raiplay"}:
                key = "raiplay_direct" if bypass_warp else "raiplay"
                if RaiPlayExtractor is None:
                    raise RuntimeError("RaiPlayExtractor module not available")
                if key not in self.extractors:
                    self.extractors[key] = RaiPlayExtractor(
                        request_headers, proxies=proxy_list
                    )
                return self.extractors[key]

        # 2. Auto-detection basata sull'URL
        # ✅ NUOVO: Salta estrattori specifici se l'URL sembra già un link diretto a un media
        # (evita di provare a estrarre un .mp4 come se fosse una pagina HTML)
        path_lower = url.split('?')[0].lower()
        if any(path_lower.endswith(ext) for ext in [".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".mp3", ".aac", ".m4a", ".mpd"]):
            key = "hls_generic"
            if key not in self.extractors:
                self.extractors[key] = GenericHLSExtractor(request_headers, proxies=_build_proxy_list(None, "generic"))
            return self.extractors[key]

        if any(
            domain in url.lower()
            for domain in (
                "mediasetinfinity.mediaset.it/",
                "wittytv.it/",
            )
        ):
            extractor_name = (
                "wittytv" if "wittytv.it/" in url.lower() else "mediaset"
            )
            key = f"{extractor_name}_direct" if bypass_warp else extractor_name
            extractor_cls = (
                WittyTVExtractor
                if extractor_name == "wittytv"
                else MediasetExtractor
            )
            proxy = get_proxy_for_url(url, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, extractor_name)
            if extractor_cls is None:
                raise RuntimeError(f"{extractor_name} extractor module not available")
            if key not in self.extractors:
                self.extractors[key] = extractor_cls(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "mediapolisvod.rai.it/relinker/" in url.lower():
            key = "raiplay_direct" if bypass_warp else "raiplay"
            proxy = get_proxy_for_url(url, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "raiplay")
            if RaiPlayExtractor is None:
                raise RuntimeError("RaiPlayExtractor module not available")
            if key not in self.extractors:
                self.extractors[key] = RaiPlayExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "vavoo.to" in url:
            key = "vavoo_direct" if bypass_warp else "vavoo"
            proxy = get_proxy_for_url("vavoo.to", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vavoo")
            if key not in self.extractors:
                self.extractors[key] = VavooExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif ("vixsrc.to/" in url.lower() or "unitv.mom/" in url.lower()) and any(
            x in url for x in ["/movie/", "/tv/", "/iframe/", "/embed/", "/playlist/"]
        ):
            key = "vixsrc_direct" if bypass_warp else "vixsrc"
            parsed_domain = urllib.parse.urlparse(url).netloc or "vixsrc.to"
            proxy = get_proxy_for_url(parsed_domain, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vixsrc")
            if key not in self.extractors:
                self.extractors[key] = VixSrcExtractor(
                    request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                )
            return self.extractors[key]
        elif ("vixcloud.co/" in url.lower() or "unitv.mom/" in url.lower()) and any(
            x in url.lower() for x in ["/embed/", "/playlist/"]
        ):
            key = "vixcloud_direct" if bypass_warp else "vixcloud"
            parsed_domain = urllib.parse.urlparse(url).netloc or "vixcloud.co"
            proxy = get_proxy_for_url(parsed_domain, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vixcloud")
            if key not in self.extractors:
                self.extractors[key] = VixSrcExtractor(
                    request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                )
            return self.extractors[key]
        elif _is_sportsonline_candidate(url):
            key = "sportsonline_direct" if bypass_warp else "sportsonline"
            proxy = _resolve_sportsonline_proxy(url)
            proxy_list = _build_proxy_list(proxy, "sportsonline")
            if key not in self.extractors:
                self.extractors[key] = SportsonlineExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif (
            re.search(r"/e/[^/?#]+", url, re.IGNORECASE) is not None
            and any(
                d in url.lower()
                for d in [
                    "dhcplay.com/",
                    "vibuxer.com/",
                    "streamhg.com/",
                    "masukestin.com/",
                ]
            )
        ):
            key = "streamhg_direct" if bypass_warp else "streamhg"
            proxy = get_proxy_for_url("streamhg", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "streamhg")
            if key not in self.extractors:
                self.extractors[key] = StreamHGExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]

        elif "mixdrop" in url or "m1xdrop" in url:
            key = "mixdrop_direct" if bypass_warp else "mixdrop"
            proxy = get_proxy_for_url("mixdrop", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "mixdrop")
            if key not in self.extractors:
                self.extractors[key] = MixdropExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif any(
            d in url
            for d in [
                "voe.sx",
                "voe.to",
                "voe.st",
                "voe.eu",
                "voe.la",
                "voe-network.net",
            ]
        ):
            key = "voe_direct" if bypass_warp else "voe"
            proxy = get_proxy_for_url("voe.sx", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "voe")
            if key not in self.extractors:
                self.extractors[key] = VoeExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "popcdn.day" in url or "freeshot.live" in url:
            key = "freeshot_direct" if bypass_warp else "freeshot"
            proxy = get_proxy_for_url(
                "popcdn.day" if "popcdn.day" in url else "freeshot.live", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "freeshot")
            if key not in self.extractors:
                self.extractors[key] = FreeshotExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif (
            "streamtape.com" in url
            or "streamtape.to" in url
            or "streamtape.net" in url
        ):
            key = "streamtape_direct" if bypass_warp else "streamtape"
            proxy = get_proxy_for_url(
                "streamtape", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "streamtape")
            if key not in self.extractors:
                self.extractors[key] = StreamtapeExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "orionoid.com" in url:
            key = "orion_direct" if bypass_warp else "orion"
            proxy = get_proxy_for_url(
                "orionoid.com", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "orion")
            if key not in self.extractors:
                self.extractors[key] = OrionExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        # --- New Extractors (URL auto-detection) ---
        elif any(
            d in url
            for d in [
                "doodstream",
                "d000d.com",
                "dood.wf",
                "dood.cx",
                "dood.la",
                "dood.so",
                "dood.pm",
            ]
        ):
            key = "doodstream_direct" if bypass_warp else "doodstream"
            proxy = get_proxy_for_url(
                url, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "doodstream")
            if key not in self.extractors:
                self.extractors[key] = DoodStreamExtractor(
                    request_headers,
                    proxies=proxy_list,
                )
            return self.extractors[key]
        elif "fastream" in url:
            key = "fastream_direct" if bypass_warp else "fastream"
            proxy = get_proxy_for_url("fastream", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "fastream")
            if key not in self.extractors:
                self.extractors[key] = FastreamExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "filelions" in url:
            key = "filelions_direct" if bypass_warp else "filelions"
            proxy = get_proxy_for_url("filelions", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "filelions")
            if key not in self.extractors:
                self.extractors[key] = FileLionsExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "filemoon" in url:
            key = "filemoon_direct" if bypass_warp else "filemoon"
            proxy = get_proxy_for_url("filemoon", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "filemoon")
            if key not in self.extractors:
                self.extractors[key] = FileMoonExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif (
            re.search(r'(/watch\.php\?.*id=\d+|/stream/stream-[\w-]+\.php)', urllib.parse.unquote(url)) is not None
        ):
            key = "dlstreams_direct" if bypass_warp else "dlstreams"
            proxy = get_proxy_for_url(
                url, bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "dlstreams")
            if key not in self.extractors:
                self.extractors[key] = DLStreamsExtractor(
                    request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                )
            return self.extractors[key]
        elif "lulustream" in url:
            key = "lulustream_direct" if bypass_warp else "lulustream"
            proxy = get_proxy_for_url(
                "lulustream", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "lulustream")
            if key not in self.extractors:
                self.extractors[key] = LuluStreamExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]

        elif "ok.ru" in url or "odnoklassniki" in url:
            key = "okru_direct" if bypass_warp else "okru"
            proxy = get_proxy_for_url("ok.ru", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "okru")
            if key not in self.extractors:
                self.extractors[key] = OkruExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif any(
            d in url
            for d in ["streamwish", "swish", "wishfast", "embedwish", "wishembed"]
        ):
            key = "streamwish_direct" if bypass_warp else "streamwish"
            proxy = get_proxy_for_url(
                "streamwish", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "streamwish")
            if key not in self.extractors:
                self.extractors[key] = StreamWishExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "supervideo" in url:
            key = "supervideo_direct" if bypass_warp else "supervideo"
            proxy = get_proxy_for_url(
                "supervideo", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "supervideo")
            if key not in self.extractors:
                self.extractors[key] = SupervideoExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "vidxgo" in url.lower():
            key = "vidxgo_direct" if bypass_warp else "vidxgo"
            proxy = get_proxy_for_url(
                "vidxgo", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vidxgo")
            if key not in self.extractors:
                if VidXgoExtractor is None:
                    raise RuntimeError("VidXgoExtractor module not available")
                self.extractors[key] = VidXgoExtractor(
                    request_headers, proxies=proxy_list
                )
            # Always refresh request_headers so per-call h_* overrides are honored.
            self.extractors[key].request_headers = request_headers
            return self.extractors[key]
        elif "dropload" in url:
            key = "dropload_direct" if bypass_warp else "dropload"
            proxy = get_proxy_for_url(
                "dropload", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "dropload")
            if key not in self.extractors:
                self.extractors[key] = DroploadExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "uqload" in url and not any(
            url.endswith(ext) or f"{ext}?" in url
            for ext in (".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".mpd")
        ):
            # Only match embed pages (e.g. uqload.is/abc123.html), not CDN video URLs (m80.uqload.is/.../v.mp4)
            key = "uqload_direct" if bypass_warp else "uqload"
            proxy = get_proxy_for_url("uqload", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "uqload")
            if key not in self.extractors:
                self.extractors[key] = UqloadExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "vidmoly" in url:
            key = "vidmoly_direct" if bypass_warp else "vidmoly"
            proxy = get_proxy_for_url("vidmoly", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vidmoly")
            if key not in self.extractors:
                self.extractors[key] = VidmolyExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "vidoza" in url or "videzz" in url:
            key = "vidoza_direct" if bypass_warp else "vidoza"
            proxy = get_proxy_for_url("vidoza", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vidoza")
            if key not in self.extractors:
                self.extractors[key] = VidozaExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif any(
            d in url
            for d in [
                "turboviplay",
                "emturbovid",
                "tuborstb",
                "javggvideo",
                "stbturbo",
                "turbovidhls",
            ]
        ):
            key = "turbovidplay_direct" if bypass_warp else "turbovidplay"
            proxy = get_proxy_for_url(
                "turbovidplay", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "turbovidplay")
            if key not in self.extractors:
                self.extractors[key] = TurboVidPlayExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "/e/" in url and any(
            d in url for d in ["f16px", "embedme", "embedsb", "playersb"]
        ):
            key = "f16px_direct" if bypass_warp else "f16px"
            proxy = get_proxy_for_url("f16px", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "f16px")
            if key not in self.extractors:
                self.extractors[key] = F16PxExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "cdnlivetv.tv" in url or "cdnlivetv.ru" in url:
            key = "sports99_direct" if bypass_warp else "sports99"
            proxy = get_proxy_for_url("cdnlivetv.tv", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "sports99")
            if key not in self.extractors:
                self.extractors[key] = Sports99Extractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        elif "embed.st/embed/" in url.lower() or "embedsports.top/embed/" in url.lower() or "streamed.pk/watch/" in url.lower():
            key = "embedst_direct" if bypass_warp else "embedst"
            proxy = get_proxy_for_url("embed.st", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "embedst")
            if key not in self.extractors:
                self.extractors[key] = EmbedStExtractor(
                    request_headers, proxies=proxy_list, bypass_warp=bypass_warp
                )
            return self.extractors[key]
        elif "vidsonic.net/" in url.lower() and re.search(r"/e/[A-Za-z0-9]+", url, re.IGNORECASE):
            key = "vidsonic_direct" if bypass_warp else "vidsonic"
            proxy = get_proxy_for_url("vidsonic", bypass_warp=bypass_warp)
            proxy_list = _build_proxy_list(proxy, "vidsonic")
            if key not in self.extractors:
                self.extractors[key] = VidSonicExtractor(
                    request_headers, proxies=proxy_list
                )
            return self.extractors[key]
        else:
            # ✅ MODIFICATO: Fallback al GenericHLSExtractor per qualsiasi altro URL.
            # Questo permette di gestire estensioni sconosciute o URL senza estensione.
            key = "hls_generic"
            if key not in self.extractors:
                self.extractors[key] = GenericHLSExtractor(
                    request_headers, proxies=_build_proxy_list(None, "generic")
                )
            return self.extractors[key]
    except (NameError, TypeError) as e:
        raise ExtractorError(f"Extractor not available - module missing: {e}")

__all__ = ["resolve_extractor", "ExtractorError"]
