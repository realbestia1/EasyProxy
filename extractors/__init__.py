"""
Extractor Registry - automatically discovers and registers all extractors.
Each extractor module should define an EXTRACTOR_INFO dict with:
  - key: canonical name (e.g. "vavoo")
  - host_aliases: list of host param values (e.g. ["vavoo"])
  - url_patterns: list of strings to match in URLs (e.g. ["vavoo.to"])
  - class_name: the extractor class name in the module
  - proxy_match: string used for get_proxy_for_url (e.g. "vavoo.to")
"""
import importlib
import logging
import os
import re

logger = logging.getLogger(__name__)

# Registry: maps canonical key -> {class, host_aliases, url_patterns, proxy_match, url_filter}
EXTRACTOR_REGISTRY = {}

# Host alias -> canonical key
HOST_ALIAS_MAP = {}

# All extractors with their config, defined here to keep each extractor module clean
_EXTRACTOR_DEFS = [
    {
        "module": "extractors.vavoo",
        "class_name": "VavooExtractor",
        "key": "vavoo",
        "host_aliases": ["vavoo"],
        "url_patterns": ["vavoo.to"],
        "proxy_match": "vavoo.to",
    },
    {
        "module": "extractors.dlhd",
        "class_name": "DLHDExtractor",
        "key": "dlhd",
        "host_aliases": ["dlhd", "daddylive", "daddyhd"],
        "url_patterns": ["daddylive", "dlhd", "daddyhd"],
        "url_regex": r"watch\.php\?id=\d+",
        "proxy_match": "dlhd.dad",
    },
    {
        "module": "extractors.vixsrc",
        "class_name": "VixSrcExtractor",
        "key": "vixsrc",
        "host_aliases": ["vixsrc"],
        "url_patterns": [],
        "url_filter": lambda url: 'vixsrc.to/' in url.lower() and any(x in url for x in ['/movie/', '/tv/', '/iframe/']),
        "proxy_match": "vixsrc.to",
    },
    {
        "module": "extractors.sportsonline",
        "class_name": "SportsonlineExtractor",
        "key": "sportsonline",
        "host_aliases": ["sportsonline", "sportzonline", "sprtsonline", "sportsnline"],
        "url_patterns": ["sportzonline", "sportsonline", "sprtsonline", "sportsnline"],
        "proxy_match": "sportsonline",
    },
    {
        "module": "extractors.mixdrop",
        "class_name": "MixdropExtractor",
        "key": "mixdrop",
        "host_aliases": ["mixdrop"],
        "url_patterns": ["mixdrop"],
        "proxy_match": "mixdrop",
    },
    {
        "module": "extractors.voe",
        "class_name": "VoeExtractor",
        "key": "voe",
        "host_aliases": ["voe"],
        "url_patterns": ["voe.sx", "voe.to", "voe.st", "voe.eu", "voe.la", "voe-network.net"],
        "proxy_match": "voe.sx",
    },
    {
        "module": "extractors.streamtape",
        "class_name": "StreamtapeExtractor",
        "key": "streamtape",
        "host_aliases": ["streamtape"],
        "url_patterns": ["streamtape.com", "streamtape.to", "streamtape.net"],
        "proxy_match": "streamtape",
    },
    {
        "module": "extractors.orion",
        "class_name": "OrionExtractor",
        "key": "orion",
        "host_aliases": ["orion"],
        "url_patterns": ["orionoid.com"],
        "proxy_match": "orionoid.com",
    },
    {
        "module": "extractors.freeshot",
        "class_name": "FreeshotExtractor",
        "key": "freeshot",
        "host_aliases": ["freeshot"],
        "url_patterns": ["popcdn.day"],
        "proxy_match": "popcdn.day",
    },
    {
        "module": "extractors.doodstream",
        "class_name": "DoodStreamExtractor",
        "key": "doodstream",
        "host_aliases": ["doodstream", "dood", "d000d"],
        "url_patterns": ["doodstream", "d000d.com", "dood.wf", "dood.cx", "dood.la", "dood.so", "dood.pm"],
        "proxy_match": "doodstream",
    },
    {
        "module": "extractors.fastream",
        "class_name": "FastreamExtractor",
        "key": "fastream",
        "host_aliases": ["fastream"],
        "url_patterns": ["fastream"],
        "proxy_match": "fastream",
    },
    {
        "module": "extractors.filelions",
        "class_name": "FileLionsExtractor",
        "key": "filelions",
        "host_aliases": ["filelions"],
        "url_patterns": ["filelions"],
        "proxy_match": "filelions",
    },
    {
        "module": "extractors.filemoon",
        "class_name": "FileMoonExtractor",
        "key": "filemoon",
        "host_aliases": ["filemoon"],
        "url_patterns": ["filemoon"],
        "proxy_match": "filemoon",
    },
    {
        "module": "extractors.lulustream",
        "class_name": "LuluStreamExtractor",
        "key": "lulustream",
        "host_aliases": ["lulustream"],
        "url_patterns": ["lulustream"],
        "proxy_match": "lulustream",
    },
    {
        "module": "extractors.maxstream",
        "class_name": "MaxstreamExtractor",
        "key": "maxstream",
        "host_aliases": ["maxstream"],
        "url_patterns": ["maxstream", "uprot.net"],
        "proxy_match": "maxstream",
    },
    {
        "module": "extractors.okru",
        "class_name": "OkruExtractor",
        "key": "okru",
        "host_aliases": ["okru", "ok.ru"],
        "url_patterns": ["ok.ru", "odnoklassniki"],
        "proxy_match": "ok.ru",
    },
    {
        "module": "extractors.streamwish",
        "class_name": "StreamWishExtractor",
        "key": "streamwish",
        "host_aliases": ["streamwish"],
        "url_patterns": ["streamwish", "swish", "wishfast", "embedwish", "wishembed"],
        "proxy_match": "streamwish",
    },
    {
        "module": "extractors.supervideo",
        "class_name": "SupervideoExtractor",
        "key": "supervideo",
        "host_aliases": ["supervideo"],
        "url_patterns": ["supervideo"],
        "proxy_match": "supervideo",
    },
    {
        "module": "extractors.uqload",
        "class_name": "UqloadExtractor",
        "key": "uqload",
        "host_aliases": ["uqload"],
        "url_patterns": ["uqload"],
        "url_filter": lambda url: "uqload" in url and not any(url.endswith(ext) or f"{ext}?" in url for ext in (".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".mpd")),
        "proxy_match": "uqload",
    },
    {
        "module": "extractors.vidmoly",
        "class_name": "VidmolyExtractor",
        "key": "vidmoly",
        "host_aliases": ["vidmoly"],
        "url_patterns": ["vidmoly"],
        "proxy_match": "vidmoly",
    },
    {
        "module": "extractors.vidoza",
        "class_name": "VidozaExtractor",
        "key": "vidoza",
        "host_aliases": ["vidoza", "videzz"],
        "url_patterns": ["vidoza", "videzz"],
        "proxy_match": "vidoza",
    },
    {
        "module": "extractors.turbovidplay",
        "class_name": "TurboVidPlayExtractor",
        "key": "turbovidplay",
        "host_aliases": ["turbovidplay", "turboviplay", "emturbovid"],
        "url_patterns": ["turboviplay", "emturbovid", "tuborstb", "javggvideo", "stbturbo", "turbovidhls"],
        "proxy_match": "turbovidplay",
    },
    {
        "module": "extractors.livetv",
        "class_name": "LiveTVExtractor",
        "key": "livetv",
        "host_aliases": ["livetv"],
        "url_patterns": [],
        "proxy_match": "livetv",
    },
    {
        "module": "extractors.f16px",
        "class_name": "F16PxExtractor",
        "key": "f16px",
        "host_aliases": ["f16px"],
        "url_patterns": [],
        "url_filter": lambda url: "/e/" in url and any(d in url for d in ["f16px", "embedme", "embedsb", "playersb"]),
        "proxy_match": "f16px",
    },
]


def _load_extractors():
    """Load all extractor modules and build the registry."""
    for defn in _EXTRACTOR_DEFS:
        module_name = defn["module"]
        class_name = defn["class_name"]
        key = defn["key"]

        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)

            EXTRACTOR_REGISTRY[key] = {
                "class": cls,
                "host_aliases": defn.get("host_aliases", []),
                "url_patterns": defn.get("url_patterns", []),
                "url_regex": defn.get("url_regex"),
                "url_filter": defn.get("url_filter"),
                "proxy_match": defn.get("proxy_match", key),
            }

            for alias in defn.get("host_aliases", []):
                HOST_ALIAS_MAP[alias] = key

            logger.info(f"Loaded {class_name}")
        except ImportError:
            logger.warning(f"{class_name} module not found, {key} functionality disabled.")
        except Exception as e:
            logger.warning(f"Error loading {class_name}: {e}")


# Load on import
_load_extractors()


def get_extractor_by_host(host: str):
    """Get extractor registry entry by host alias."""
    key = HOST_ALIAS_MAP.get(host.lower())
    if key:
        return key, EXTRACTOR_REGISTRY.get(key)
    return None, None


def get_extractor_by_url(url: str):
    """Get extractor registry entry by URL auto-detection."""
    for key, info in EXTRACTOR_REGISTRY.items():
        # Check custom url_filter first (takes precedence)
        url_filter = info.get("url_filter")
        if url_filter:
            if url_filter(url):
                return key, info
            continue

        # Check url_regex
        url_regex = info.get("url_regex")
        if url_regex and re.search(url_regex, url):
            return key, info

        # Check url_patterns
        for pattern in info.get("url_patterns", []):
            if pattern in url:
                return key, info

    return None, None
