from services.proxy_shared import PlaylistBuilder, logger
import asyncio
import os
from services.proxy_core import HLSProxyCoreMixin
from services.proxy_dash import HLSProxyDashMixin
from services.proxy_extractor import HLSProxyExtractorHandlerMixin
from services.proxy_license import HLSProxyLicenseHandlerMixin
from services.proxy_manifest import HLSProxyManifestHandlerMixin
from services.proxy_pages import HLSProxyPagesMixin
from services.proxy_streaming import HLSProxyStreamingMixin
from services.proxy_dual import HLSProxyDualMixin

class HLSProxy(
    HLSProxyDualMixin,
    HLSProxyCoreMixin,
    HLSProxyManifestHandlerMixin,
    HLSProxyExtractorHandlerMixin,
    HLSProxyLicenseHandlerMixin,
    HLSProxyDashMixin,
    HLSProxyStreamingMixin,
    HLSProxyPagesMixin,
):
    """Proxy HLS per stream, playlist, DASH e segmenti."""

    def __init__(self):
        # Shared extractors registry owned by the proxy instance
        self.extractors = {}
        self._extractor_atimes = {}
        self._retired_extractors = []
        self._retired_extractor_atimes = {}

        # Inizializza il playlist_builder se il modulo è disponibile
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder inizializzato")
        else:
            self.playlist_builder = None

        self._background_tasks = set()
        self._parallel_fetch_stats = {
            "calls": 0,
            "active": 0,
            "active_peak": 0,
            "successes": 0,
            "fallbacks": 0,
            "errors": 0,
            "parts_per_call": 3,
            "bytes_total": 0,
            "max_segment_bytes": 0,
            "last_segment_bytes": 0,
            "last_status": None,
            "last_reason": None,
            "last_duration_ms": 0.0,
            "last_segment": None,
        }

        # Sessione condivisa per il proxy (no proxy)
        self.session = None
        self.flex_session = None

        # Proxy sessions are created fresh per request — no caching

        # Refreshed CDN tokens for live token substitution after re-extract on 403.
        # stream_key -> (old_base_dir, new_base_dir, new_query_string_with_leading_question_mark)
        self._renewed_cdn_tokens: dict[str, tuple[str, str, str]] = {}
        self._renewed_cdn_token_atimes: dict[str, float] = {}
        # Template/OpenAPI cache (read once, serve many)
        self._template_cache = {}
        self._openapi_text = None
        self._template_cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

        # Version information
        self.latest_version = "Checking..."
        self._latest_version_checked_at = 0.0
        self._latest_version_lock = asyncio.Lock()
        self.warp_status = "Checking..."
        self._warp_ip = ""
        self._warp_status_checked_at = 0.0
        self._warp_status_reason = ""


__all__ = ["HLSProxy"]
