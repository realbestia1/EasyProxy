"""Extractor imports for the registry.

Every extractor module lives in this repo, so the table below is the whole
contract: a module that fails to import degrades to ``None`` (the resolver
reports it as unavailable) instead of killing startup. See extractors/AGENTS.md.
"""

import importlib
import logging
import re

logger = logging.getLogger("extractors.registry")

# (module, exported names) in load order. Keep in sync with extractors/AGENTS.md.
_MODULES = (
    ("extractors.freeshot", ("FreeshotExtractor",)),
    ("extractors.vavoo", ("VavooExtractor",)),
    ("extractors.vixsrc", ("VixSrcExtractor",)),
    ("extractors.sportsonline", ("SportsonlineExtractor",)),
    ("extractors.mixdrop", ("MixdropExtractor",)),
    ("extractors.voe", ("VoeExtractor",)),
    ("extractors.streamtape", ("StreamtapeExtractor",)),
    ("extractors.orion", ("OrionExtractor",)),
    ("extractors.doodstream", ("DoodStreamExtractor",)),
    ("extractors.fastream", ("FastreamExtractor",)),
    ("extractors.filelions", ("FileLionsExtractor",)),
    ("extractors.filemoon", ("FileMoonExtractor",)),
    ("extractors.lulustream", ("LuluStreamExtractor",)),
    ("extractors.okru", ("OkruExtractor",)),
    ("extractors.streamwish", ("StreamWishExtractor",)),
    ("extractors.streamhg", ("StreamHGExtractor",)),
    ("extractors.supervideo", ("SupervideoExtractor",)),
    ("extractors.uqload", ("UqloadExtractor",)),
    ("extractors.dropload", ("DroploadExtractor",)),
    ("extractors.vidxgo", ("VidXgoExtractor",)),
    ("extractors.vidmoly", ("VidmolyExtractor",)),
    ("extractors.vidoza", ("VidozaExtractor",)),
    ("extractors.turbovidplay", ("TurboVidPlayExtractor",)),
    ("extractors.livetv", ("LiveTVExtractor",)),
    ("extractors.f16px", ("F16PxExtractor",)),
    ("extractors.guardabest", ("GuardabestExtractor",)),
    ("extractors.sports99", ("Sports99Extractor",)),
    ("extractors.dlstreams", ("DLStreamsExtractor",)),
    ("extractors.embedst", ("EmbedStExtractor",)),
    ("extractors.vidsonic", ("VidSonicExtractor",)),
    ("extractors.vidlink", ("VidLinkExtractor",)),
    ("extractors.vidfast", ("VidFastExtractor",)),
    ("extractors.mediaset", ("MediasetExtractor", "WittyTVExtractor")),
    ("extractors.raiplay", ("RaiPlayExtractor",)),
    (
        "extractors.ads",
        ("ADSExtractor", "ADS_HOST_PATTERN", "ADS_FILM_PATTERN",
         "ADS_SERIES_PATTERN", "ads_configured_host"),
    ),
    ("extractors.cinejoy", ("CinejoyExtractor",)),
    ("extractors.fctv33", ("Fctv33Extractor",)),
)

# Stand-ins used when extractors.ads is unavailable, so the ADS pattern users
# (registry_resolver) never see a missing name.
_ADS_FALLBACKS = {
    "ADS_HOST_PATTERN": re.compile(r"(?!)"),
    "ADS_FILM_PATTERN": re.compile(r"(?!)"),
    "ADS_SERIES_PATTERN": re.compile(r"(?!)"),
    "ads_configured_host": lambda: "",
}

__all__ = [name for _, names in _MODULES for name in names]

for _module_name, _names in _MODULES:
    try:
        _module = importlib.import_module(_module_name)
    except Exception as _exc:  # noqa: BLE001 - one bad extractor must not stop startup
        logger.warning("⚠️ %s failed to load: %s", _module_name, _exc)
        for _name in _names:
            globals()[_name] = _ADS_FALLBACKS.get(_name)
    else:
        for _name in _names:
            globals()[_name] = getattr(_module, _name)
        logger.info("✅ %s loaded.", _module_name)
