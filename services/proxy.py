from services.proxy_shared import PlaylistBuilder, logger
import asyncio
import os
from services.proxy_core import HLSProxyCoreMixin
from services.proxy_dash import HLSProxyDashMixin
from services.proxy_handlers import HLSProxyHandlersMixin
from services.proxy_pages import HLSProxyPagesMixin
from services.proxy_streaming import HLSProxyStreamingMixin


class HLSProxy(
    HLSProxyCoreMixin,
    HLSProxyHandlersMixin,
    HLSProxyDashMixin,
    HLSProxyStreamingMixin,
    HLSProxyPagesMixin,
):
    """Proxy HLS per stream, playlist, DASH e segmenti."""

    def __init__(self, ffmpeg_manager=None):
        self.extractors = {}
        self._extractor_atimes = {}
        self._extractor_stream_atimes = {}
        self.ffmpeg_manager = ffmpeg_manager

        # Inizializza il playlist_builder se il modulo è disponibile
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder inizializzato")
        else:
            self.playlist_builder = None

        # Prefetch queue for background downloading (kept for prefetch logic, no segment cache storage)
        self.prefetch_tasks = set()
        self._prefetch_semaphore = asyncio.Semaphore(5)
        self._prefetch_lock = asyncio.Lock()

        # Sessione condivisa per il proxy (no proxy)
        self.session = None
        self.flex_session = None

        # Cache for proxy sessions (proxy_url -> session)
        # This reuses connections for the same proxy to improve performance
        self.proxy_sessions = {}
        self._proxy_session_atimes = {}  # proxy_url -> last access time
        self.curl_sessions = {}  # Registry for pooled curl_cffi sessions

        # Refreshed CDN tokens for live token substitution after re-extract on 403.
        # stream_key -> (old_base_dir, new_base_dir, new_query_string_with_leading_question_mark)
        self._renewed_cdn_tokens: dict[str, tuple[str, str, str]] = {}
        self._renewed_cdn_token_atimes: dict[str, float] = {}

        # Template cache (read once, serve many)
        self._template_cache = {}
        self._template_cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

        # Version information
        self.latest_version = "Checking..."
        self.warp_status = "Checking..."
        self._warp_ip = ""



__all__ = ["HLSProxy"]
