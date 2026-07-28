import logging

logger = logging.getLogger("extractors.registry")

# --- Moduli Esterni ---
(
    VavooExtractor,
    VixSrcExtractor,
    SportsonlineExtractor,
) = None, None, None
(
    MixdropExtractor,
    VoeExtractor,
    StreamtapeExtractor,
    OrionExtractor,
    FreeshotExtractor,
) = None, None, None, None, None
# New extractors
(
    DoodStreamExtractor,
    FastreamExtractor,
    FileLionsExtractor,
    FileMoonExtractor,
    LuluStreamExtractor,
) = None, None, None, None, None
(
    OkruExtractor,
    StreamWishExtractor,
    SupervideoExtractor,
    UqloadExtractor,
    DroploadExtractor,
) = None, None, None, None, None
(
    VidmolyExtractor,
    VidozaExtractor,
    TurboVidPlayExtractor,
    LiveTVExtractor,
    F16PxExtractor,
    Sports99Extractor,
) = None, None, None, None, None, None
DLStreamsExtractor = None
StreamHGExtractor = None
VidXgoExtractor = None
EmbedStExtractor = None
VidSonicExtractor = None
MediasetExtractor = None
WittyTVExtractor = None
RaiPlayExtractor = None


# Importazione condizionale degli estrattori
try:
    from extractors.freeshot import FreeshotExtractor
    logger.info("✅ FreeshotExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FreeshotExtractor module not found.")

try:
    from extractors.vavoo import VavooExtractor
    logger.info("✅ VavooExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VavooExtractor module not found. Vavoo functionality disabled.")


try:
    from extractors.vixsrc import VixSrcExtractor
    logger.info("✅ VixSrcExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VixSrcExtractor module not found. VixSrc functionality disabled.")

try:
    from extractors.sportsonline import SportsonlineExtractor
    logger.info("✅ SportsonlineExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ SportsonlineExtractor module not found. Sportsonline functionality disabled.")

try:
    from extractors.mixdrop import MixdropExtractor
    logger.info("✅ MixdropExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ MixdropExtractor module not found.")

try:
    from extractors.voe import VoeExtractor
    logger.info("✅ VoeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VoeExtractor module not found.")

try:
    from extractors.streamtape import StreamtapeExtractor
    logger.info("✅ StreamtapeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamtapeExtractor module not found.")

try:
    from extractors.orion import OrionExtractor
    logger.info("✅ OrionExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ OrionExtractor module not found.")

try:
    from extractors.doodstream import DoodStreamExtractor
    logger.info("✅ DoodStreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DoodStreamExtractor module not found.")

try:
    from extractors.fastream import FastreamExtractor
    logger.info("✅ FastreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FastreamExtractor module not found.")

try:
    from extractors.filelions import FileLionsExtractor
    logger.info("✅ FileLionsExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FileLionsExtractor module not found.")

try:
    from extractors.filemoon import FileMoonExtractor
    logger.info("✅ FileMoonExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FileMoonExtractor module not found.")

try:
    from extractors.lulustream import LuluStreamExtractor
    logger.info("✅ LuluStreamExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ LuluStreamExtractor module not found.")



try:
    from extractors.okru import OkruExtractor
    logger.info("✅ OkruExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ OkruExtractor module not found.")

try:
    from extractors.streamwish import StreamWishExtractor
    logger.info("✅ StreamWishExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamWishExtractor module not found.")

try:
    from extractors.streamhg import StreamHGExtractor
    logger.info("✅ StreamHGExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamHGExtractor module not found.")

try:
    from extractors.supervideo import SupervideoExtractor
    logger.info("✅ SupervideoExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ SupervideoExtractor module not found.")

try:
    from extractors.uqload import UqloadExtractor
    logger.info("✅ UqloadExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ UqloadExtractor module not found.")

try:
    from extractors.dropload import DroploadExtractor
    logger.info("✅ DroploadExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DroploadExtractor module not found.")

try:
    from extractors.vidxgo import VidXgoExtractor
    logger.info("✅ VidXgoExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VidXgoExtractor module not found.")

try:
    from extractors.vidmoly import VidmolyExtractor
    logger.info("✅ VidmolyExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VidmolyExtractor module not found.")

try:
    from extractors.vidoza import VidozaExtractor
    logger.info("✅ VidozaExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VidozaExtractor module not found.")

try:
    from extractors.turbovidplay import TurboVidPlayExtractor
    logger.info("✅ TurboVidPlayExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ TurboVidPlayExtractor module not found.")

try:
    from extractors.livetv import LiveTVExtractor
    logger.info("✅ LiveTVExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ LiveTVExtractor module not found.")

try:
    from extractors.f16px import F16PxExtractor
    logger.info("✅ F16PxExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ F16PxExtractor module not found.")

try:
    from extractors.sports99 import Sports99Extractor
    logger.info("✅ Sports99Extractor module loaded.")
except ImportError:
    logger.warning("⚠️ Sports99Extractor module not found.")

try:
    from extractors.dlstreams import DLStreamsExtractor
    logger.info("✅ DLStreamsExtractor module loaded.")
except Exception as e:
    logger.warning("⚠️ DLStreamsExtractor failed to load: %s", e)
    DLStreamsExtractor = None

try:
    from extractors.embedst import EmbedStExtractor
    logger.info("✅ EmbedStExtractor module loaded.")
except Exception as e:
    logger.warning("⚠️ EmbedStExtractor failed to load: %s", e)
    EmbedStExtractor = None

try:
    from extractors.vidsonic import VidSonicExtractor
    logger.info("✅ VidSonicExtractor module loaded.")
except Exception as e:
    logger.warning("⚠️ VidSonicExtractor failed to load: %s", e)
    VidSonicExtractor = None

try:
    from extractors.mediaset import MediasetExtractor, WittyTVExtractor
    logger.info("✅ MediasetExtractor and WittyTVExtractor modules loaded.")
except Exception as e:
    logger.warning("⚠️ Mediaset/WittyTV extractors failed to load: %s", e)
    MediasetExtractor = None
    WittyTVExtractor = None

try:
    from extractors.raiplay import RaiPlayExtractor
    logger.info("✅ RaiPlayExtractor module loaded.")
except Exception as e:
    logger.warning("⚠️ RaiPlayExtractor failed to load: %s", e)
    RaiPlayExtractor = None


__all__ = [
    "VavooExtractor",
    "VixSrcExtractor",
    "SportsonlineExtractor",
    "MixdropExtractor",
    "VoeExtractor",
    "StreamtapeExtractor",
    "OrionExtractor",
    "FreeshotExtractor",
    "DoodStreamExtractor",
    "FastreamExtractor",
    "FileLionsExtractor",
    "FileMoonExtractor",
    "LuluStreamExtractor",
    "OkruExtractor",
    "StreamWishExtractor",
    "SupervideoExtractor",
    "UqloadExtractor",
    "DroploadExtractor",
    "VidmolyExtractor",
    "VidozaExtractor",
    "TurboVidPlayExtractor",
    "LiveTVExtractor",
    "F16PxExtractor",
    "Sports99Extractor",
    "DLStreamsExtractor",
    "StreamHGExtractor",
    "VidXgoExtractor",
    "EmbedStExtractor",
    "VidSonicExtractor",
    "MediasetExtractor",
    "WittyTVExtractor",
    "RaiPlayExtractor",
]
