import asyncio
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import aiohttp
from services.dual.video import byte_range, video_only
import config as _config
from config import is_proxy_alive
import services.proxy_shared as _shared
import services.wg_tunnels as wg_tunnels
from services.proxy_shared import (
    logger,
    web,
    yarl,
    set_response_header,
    check_vavoo_request,
    get_ssl_setting_for_url,
    get_proxy_for_url,
    HAS_CURL_CFFI,
    get_curl_async_session,
    ClientTimeout,
    ClientConnectionError,
    ServerDisconnectedError,
    ClientPayloadError,
    ALL_PROXY_ERRORS,
    should_use_short_manifest_urls,
    ManifestRewriter,
    MPDToHLSConverter,
    parse_clearkey_params,
    ProxyDeadRetryError,
    seal_clearkey,
    decrypt_segment,
    check_password,
    prepare_curl_headers,
    final_curl_request_url,
    should_use_curl_cffi,
    is_special_cdn_stream,
    get_public_base_url,
    get_extractor_routing_overrides,
    request_log_context,
    safe_log_route,
)

class _ParallelFallback(Exception):
    """Raised when parallel range fetch is not applicable; falls back to single connection."""


class _ClientDisconnected(Exception):
    """The player left while the segment was still being fetched."""


# Do not share asyncio's default executor with WARP/proxy socket health checks.
# Those checks can block for seconds and otherwise delay ClearKey decryption,
# even though the actual AES/MP4 operation is fast.
_CLEARKEY_EXECUTOR = ThreadPoolExecutor(
    thread_name_prefix="clearkey",
)

# Parallel range fetch thresholds: beat per-connection CDN throttling (e.g. vidsonic
# ~1.7 Mbps/conn vs 2.4 Mbps video) by downloading one segment over K parallel
# range requests. Only triggers for large segments on range-enabled CDNs.
_PARALLEL_MIN_SIZE = 1_500_000  # 1.5 MB
_PARALLEL_PARTS = 3

# Proactive signed-token refresh: re-extract when a segment URL's e= deadline is
# this close, with a cooldown so one refresh covers a whole token generation.
_TOKEN_REFRESH_MARGIN_SECONDS = 15.0
_TOKEN_REFRESH_COOLDOWN_SECONDS = 60.0


class HLSProxyStreamingMixin:

    @staticmethod
    def _discard_disabled_warp_route(proxy_url, bypass_warp):
        """Drop WARP carried by an old relay URL after its policy changed."""
        if proxy_url and _config.is_warp_proxy_url(proxy_url) and (
            bypass_warp or not _config._get_dynamic_warp_enabled()
        ):
            logger.debug(
                "Ignoring stale WARP route %s (WARP bypassed/disabled)",
                proxy_url,
            )
            return None
        return proxy_url

    # Pre-compiled regex for segment URL parsing
    _SEGMENT_URL_PATTERN = re.compile(r"([-_])(\d+)(\.[^.]+)$")

    @staticmethod
    def _client_gone(request) -> bool:
        transport = getattr(request, "transport", None)
        return transport is not None and transport.is_closing()

    @staticmethod
    async def _write_with_backpressure(response, chunk, max_backlog=131072):
        """Write chunk with backpressure: drain if write buffer exceeds max_backlog."""
        await response.write(chunk)
        try:
            transport = response.transport
            if transport is not None and not transport.is_closing():
                buf_size = transport.get_write_buffer_size()
                if buf_size > max_backlog:
                    await response.drain()
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _trim_cache(cache: dict, max_size: int = 30, trim_count: int = 10):
        if len(cache) <= max_size:
            return
        for key in sorted(cache.keys(), key=lambda k: cache[k][1] if isinstance(cache[k], tuple) else 0)[:trim_count]:
            cache.pop(key, None)

    # In-flight coalescing for fully-buffered segments (image-wrapped payloads
    # and similar): while the first viewer's fetch/unwrap is running, other
    # viewers of the same segment wait for that result instead of repeating the
    # upstream download and PNG/gzip work. Nothing is retained after it is
    # served.
    _SEGMENT_COALESCE_VOLATILE_HEADERS = frozenset({
        "range",
        "if-none-match",
        "if-modified-since",
        "if-match",
        "if-range",
        "accept-encoding",
    })

    @classmethod
    def _segment_coalesce_key(cls, stream_url, headers):
        stable = tuple(
            sorted(
                (k.lower(), v)
                for k, v in headers.items()
                if k.lower() not in cls._SEGMENT_COALESCE_VOLATILE_HEADERS
            )
        )
        return (stream_url, stable)

    @staticmethod
    def _segment_coalesce_eligible(request):
        if request.method != "GET" or request.query.get("range"):
            return False
        return not any(
            header in request.headers
            for header in ("Range", "If-None-Match", "If-Modified-Since")
        )

    def _segment_inflight_map(self):
        inflight = getattr(self, "_segment_inflight", None)
        if inflight is None:
            inflight = {}
            self._segment_inflight = inflight
        return inflight

    # --- Image-wrapped segment sniffing (act-green) ---
    # NOTE: daddyliveplayer.st / dlive.sx serve some .ts segments with a PNG/WebP
    # body (URL/content-type often lie). Sniff the magic bytes, never the URL.
    _PNG_SIG = b"\x89PNG\r\n\x1a\n"
    _IMAGE_URL_MARKERS = (".image", "tiktokcdn", "~tplv-")
    _IMAGE_UNWRAP_MAX_BYTES = 25_000_000

    @staticmethod
    def _is_image_magic(data: bytes) -> bool:
        """True if data starts with PNG signature or RIFF....WEBP."""
        if not data:
            return False
        if data[:8] == HLSProxyStreamingMixin._PNG_SIG:
            return True
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"

    @classmethod
    def _is_image_url(cls, url: str) -> bool:
        low = (url or "").lower()
        return any(marker in low for marker in cls._IMAGE_URL_MARKERS)

    @staticmethod
    def _is_valid_ts(data: bytes) -> bool:
        """Unwrapped payload is usable only with a TS sync byte."""
        return bool(data) and len(data) > 188 and data[0] == 0x47

    async def handle_ts_segment(self, request):
        """Gestisce richieste per segmenti .ts"""
        try:
            segment_name = request.match_info.get("tail")
            base_url = request.query.get("base_url")

            if not base_url:
                return web.Response(text="Missing base URL for segment", status=400)

            # aiohttp already decodes query parameters once.
            # Avoid unquoting again or embedded encoded URLs may break.

            if base_url.endswith("/"):
                segment_url = f"{base_url}{segment_name}"
            else:
                # ✅ CORREZIONE: Se base_url è un URL completo (es. generato dal converter), usalo direttamente.
                if any(
                    ext in base_url
                    for ext in [".mp4", ".m4s", ".ts", ".m4i", ".m4a", ".m4v", ".dash"]
                ):
                    segment_url = base_url
                else:
                    segment_url = f"{base_url.rsplit('/', 1)[0]}/{segment_name}"

            logger.info(f"📦 Proxy Segment: {segment_name}")

            # Gestisce la risposta del proxy per il segmento
            return await self._proxy_segment(
                request,
                segment_url,
                {
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "referer": base_url,
                },
                segment_name,
            )

        except Exception as e:
            logger.error(
                "Error in .ts segment proxy: %s [%s]",
                e,
                request_log_context(
                    request,
                    locals().get("segment_url") or locals().get("base_url"),
                    route=safe_log_route(request.query.get("proxy")),
                ),
            )
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_segment_parallel(
        self, request, segment_url, headers, segment_name, bypass_warp, forced_proxy,
        session_key=None,
    ):
        stats = getattr(self, "_parallel_fetch_stats", None)
        if stats is None:
            stats = {
                "calls": 0, "active": 0, "active_peak": 0,
                "successes": 0, "fallbacks": 0, "errors": 0,
                "parts_per_call": _PARALLEL_PARTS, "bytes_total": 0,
                "max_segment_bytes": 0, "last_segment_bytes": 0,
                "last_status": None, "last_reason": None,
                "last_duration_ms": 0.0, "last_segment": None,
            }
            self._parallel_fetch_stats = stats

        started = time.monotonic()
        stats["calls"] += 1
        stats["active"] += 1
        stats["active_peak"] = max(stats["active_peak"], stats["active"])
        stats["last_segment"] = str(segment_name or "")[:160]
        try:
            result = await self._proxy_segment_parallel_impl(
                request, segment_url, headers, segment_name, bypass_warp, forced_proxy,
                session_key=session_key,
            )
            stats["successes"] += 1
            stats["last_status"] = "success"
            stats["last_reason"] = None
            return result
        except _ParallelFallback as exc:
            stats["fallbacks"] += 1
            stats["last_status"] = "fallback"
            stats["last_reason"] = str(exc)[:240]
            raise
        except Exception as exc:
            stats["errors"] += 1
            stats["last_status"] = "error"
            stats["last_reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            stats["active"] = max(0, stats["active"] - 1)
            stats["last_duration_ms"] = round((time.monotonic() - started) * 1000, 1)

    async def _proxy_segment_parallel_impl(
        self, request, segment_url, headers, segment_name, bypass_warp, forced_proxy,
        session_key=None,
    ):
        """Download one segment via K parallel range requests to beat per-connection
        CDN throttling (e.g. vidsonic limits each TCP connection to ~1.7 Mbps while
        the video is 2.4 Mbps; 3 parallel ranges -> ~5 Mbps aggregate).

        Raises _ParallelFallback when not applicable so the caller falls back to the
        single-connection streaming path.
        """
        # Only when the client wants the whole segment (no partial-range seek).
        client_range = headers.get("range") or headers.get("Range")
        if client_range and client_range.lower() != "bytes=0-":
            raise _ParallelFallback("client requested a partial range")
        if is_special_cdn_stream(segment_url) or should_use_curl_cffi(
            segment_url, False, HAS_CURL_CFFI
        ):
            # Domains that need TLS impersonation (e.g. vidxgo's d2b.you) go
            # straight to the single-connection curl_cffi path.
            raise _ParallelFallback("single-connection transport preferred")

        base_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
        disable_ssl = get_ssl_setting_for_url(segment_url) or check_vavoo_request(headers, request, segment_url)

        # 1) Probe size + Accept-Ranges with a 1-byte range request.
        probe_headers = {**base_headers, "Range": "bytes=0-0"}
        session, session_proxy = await self._get_proxy_session(
            segment_url,
            bypass_warp=bypass_warp,
            forced_proxy=forced_proxy,
            session_key=session_key,
        )
        total = None
        try:
            async with session.get(
                yarl.URL(segment_url, encoded=True),
                headers=probe_headers,
                ssl=not disable_ssl,
                timeout=ClientTimeout(total=6, connect=5, sock_connect=5, sock_read=5),
            ) as probe:
                if probe.status not in (200, 206):
                    raise _ParallelFallback(f"probe status {probe.status}")
                if (probe.headers.get("Content-Type") or "").lower().startswith("image/"):
                    # Image-wrapped segment: let the single-connection path
                    # buffer + unwrap it instead of relaying PNG bytes as TS.
                    raise _ParallelFallback("image content-type")
                accept_ranges = (probe.headers.get("Accept-Ranges") or "").lower()
                content_range = probe.headers.get("Content-Range") or ""
                if "bytes" not in accept_ranges and not content_range:
                    raise _ParallelFallback("no Accept-Ranges")
                if content_range and "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                if not total and probe.status == 200:
                    cl = probe.headers.get("Content-Length")
                    if cl:
                        try:
                            total = int(cl)
                        except ValueError:
                            total = None
        except _ParallelFallback:
            raise
        except Exception as e:
            raise _ParallelFallback(f"probe error: {e}")
        finally:
            if session and session_proxy and not session.closed:
                await session.close()

        if not total or total < _PARALLEL_MIN_SIZE:
            raise _ParallelFallback(f"segment too small ({total})")
        if self._client_gone(request):
            return web.Response(status=499, text="Client disconnected")

        stats = getattr(self, "_parallel_fetch_stats", None)
        if stats is not None:
            stats["bytes_total"] += total
            stats["max_segment_bytes"] = max(stats["max_segment_bytes"], total)
            stats["last_segment_bytes"] = total

        # 2) Split into K ranges and fetch in parallel.
        K = _PARALLEL_PARTS
        chunk = total // K
        ranges = []
        for i in range(K):
            start = i * chunk
            end = total - 1 if i == K - 1 else (start + chunk - 1)
            ranges.append((start, end))

        data = bytearray(total)

        async def _fetch_part_into(start, end):
            h = {**base_headers, "Range": f"bytes={start}-{end}"}
            s, s_proxy = await self._get_proxy_session(
                segment_url,
                bypass_warp=bypass_warp,
                forced_proxy=forced_proxy,
                session_key=session_key,
            )
            try:
                async with s.get(
                    yarl.URL(segment_url, encoded=True),
                    headers=h,
                    ssl=not disable_ssl,
                    timeout=ClientTimeout(total=45, connect=10, sock_connect=10, sock_read=10),
                ) as r:
                    # A CDN ignoring Range answers 200 with the FULL body:
                    # writing it at an offset silently corrupts the segment
                    # (green/black video, audio survives). Fail fast instead.
                    if r.status != 206:
                        raise _ParallelFallback(f"part {start}-{end} status {r.status}")
                    r.raise_for_status()
                    # Write chunks straight into the shared buffer: buffering the
                    # whole part first (r.read()) kept ~2x the segment in RAM per
                    # request, which is what inflated RSS at active_peak=4.
                    expected = end - start + 1
                    written = 0
                    async for piece in r.content.iter_chunked(65536):
                        if self._client_gone(request):
                            raise _ClientDisconnected()
                        if written + len(piece) > expected:
                            raise _ParallelFallback(f"part {start}-{end} overlong")
                        data[start + written:start + written + len(piece)] = piece
                        written += len(piece)
                    if written != expected:
                        raise _ParallelFallback(
                            f"part {start}-{end} short ({written} bytes)"
                        )
            finally:
                if s_proxy:
                    await s.close()

        tasks = [asyncio.create_task(_fetch_part_into(s, e)) for s, e in ranges]

        async def _watch_client():
            while any(not task.done() for task in tasks):
                if self._client_gone(request):
                    for task in tasks:
                        task.cancel()
                    return
                await asyncio.sleep(0.5)

        watcher = asyncio.create_task(_watch_client())
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)
        except asyncio.TimeoutError:
            raise _ParallelFallback("parallel fetch budget exceeded")
        except asyncio.CancelledError:
            if self._client_gone(request):
                return web.Response(status=499, text="Client disconnected")
            raise
        except BaseException as exc:
            if isinstance(exc, _ClientDisconnected):
                return web.Response(status=499, text="Client disconnected")
            raise _ParallelFallback(f"parallel fetch error: {exc}") from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            watcher.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if len(data) != total:
            raise _ParallelFallback(f"size mismatch {len(data)} != {total}")

        # Safety net: content-type can lie. Never relay PNG/WEBP bytes as TS
        # (silent green screen, no log). Unwrap here, drop if not a valid TS.
        # raw stays a bytearray: bytearray slicing/startswith are byte-compatible.
        raw = data
        if self._is_image_magic(raw):
            logger.warning(
                "Sniffed image-wrapped segment for %s in parallel fetch (%d bytes) -> unwrap",
                segment_name, len(raw),
            )
            unwrapped = await asyncio.to_thread(self._strip_fake_png_header_from_ts, raw)
            if not self._is_valid_ts(unwrapped):
                logger.warning(
                    "Dropped non-video static image segment for %s (%d bytes, no TS sync byte)",
                    segment_name, len(raw),
                )
                return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
            raw = unwrapped

        if not self._is_valid_ts(raw):
            # Not an image and no TS sync byte (error page, truncated part,
            # wrong container): never relay it as video. The single-connection
            # path below handles it (buffered sniff / 204 / proper error).
            head = raw[:8].hex() if raw else "empty"
            raise _ParallelFallback(f"assembled head {head} not TS")

        # 3) Stream the assembled segment to the client.
        response_headers = {}
        set_response_header(response_headers, "Content-Type", "video/mp2t")
        set_response_header(response_headers, "Content-Disposition", f'attachment; filename="{segment_name}"')
        set_response_header(response_headers, "Access-Control-Allow-Origin", "*")
        set_response_header(response_headers, "Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        set_response_header(response_headers, "Access-Control-Allow-Headers", "Range, Content-Type")
        if self._client_gone(request):
            return web.Response(status=499, text="Client disconnected")
        response = web.StreamResponse(status=200, headers=response_headers)
        try:
            await response.prepare(request)
            for i in range(0, len(raw), 65536):
                if self._client_gone(request):
                    return response
                await response.write(bytes(raw[i:i+65536]))
            await response.write_eof()
        except (ConnectionResetError, OSError, RuntimeError) as exc:
            logger.info("[INFO] Client disconnected from stream: %s (%s)", segment_url, exc)
            return response if response.prepared else web.Response(status=499, text="Client disconnected")
        logger.info(f"⚡ Parallel fetch {segment_name}: {len(raw)} bytes via {K} ranges")
        return response

    @staticmethod
    def _segment_token_expiry(stream_url: str) -> float | None:
        """Epoch seconds from a signed URL's e= parameter, if any."""
        try:
            query = urllib.parse.urlsplit(stream_url).query
        except (TypeError, ValueError):
            return None
        values = urllib.parse.parse_qs(query).get("e")
        if not values or not values[0].isdigit():
            return None
        value = int(values[0])
        return value / 1000.0 if value > 10_000_000_000 else float(value)

    @staticmethod
    def _fresh_segment_url(refreshed: dict, seg_filename: str, stream_url: str) -> str | None:
        """URL of the same segment inside a freshly extracted manifest."""
        captured_manifests = dict((refreshed or {}).get("captured_manifests") or {})
        master_url = (refreshed or {}).get("destination_url")
        master_text = (refreshed or {}).get("captured_manifest")
        if master_text and master_url:
            captured_manifests.setdefault(master_url, master_text)
        for m_url, m_text in captured_manifests.items():
            for line in (m_text or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                abs_url = urllib.parse.urljoin(m_url, line)
                if abs_url.rsplit("/", 1)[-1].split("?")[0] == seg_filename:
                    return abs_url
        # Tokenized HLS where only the playlist query changed.
        if master_url and "?" in master_url:
            return stream_url.split("?", 1)[0] + "?" + master_url.split("?", 1)[1]
        return None

    def _schedule_cdn_token_refresh(
        self, orig_url, stream_url, headers, bypass_warp, forced_proxy, stream_key
    ) -> None:
        """Re-extract in the background before a signed segment URL expires."""
        if not stream_key:
            return
        expiry = self._segment_token_expiry(stream_url)
        now = time.time()
        if expiry is None or expiry <= now or expiry - now > _TOKEN_REFRESH_MARGIN_SECONDS:
            return
        if now - getattr(self, "_renewed_cdn_token_atimes", {}).get(stream_key, 0.0) < _TOKEN_REFRESH_COOLDOWN_SECONDS:
            return
        tasks = getattr(self, "_cdn_refresh_tasks", None)
        if tasks is None:
            tasks = self._cdn_refresh_tasks = {}
        existing = tasks.get(stream_key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._refresh_cdn_token(
                orig_url, stream_url, headers, bypass_warp, forced_proxy, stream_key
            )
        )
        tasks[stream_key] = task

        def _drop(_task, key=stream_key):
            if tasks.get(key) is _task:
                tasks.pop(key, None)

        task.add_done_callback(_drop)

    def _store_cdn_token(self, stream_key, old_url, new_url, label) -> None:
        old_base_dir = old_url.rsplit("/", 1)[0] + "/"
        new_base_dir = new_url.rsplit("/", 1)[0] + "/"
        new_qs = "?" + new_url.split("?", 1)[1] if "?" in new_url else ""
        self._renewed_cdn_tokens[stream_key] = (old_base_dir, new_base_dir, new_qs)
        self._renewed_cdn_token_atimes[stream_key] = time.time()
        logger.info("🔑 %s for stream_key=%s", label, stream_key[:8])

    async def _extract_refreshed(self, orig_url, headers, bypass_warp, forced_proxy):
        async with self.borrow_extractor(
            orig_url, headers, bypass_warp=bypass_warp
        ) as extractor:
            return await extractor.extract(
                orig_url,
                force_refresh=True,
                request_headers=headers,
                bypass_warp=bypass_warp,
                proxy=forced_proxy,
            )

    async def _refresh_cdn_token(
        self, orig_url, stream_url, headers, bypass_warp, forced_proxy, stream_key
    ) -> None:
        if not orig_url:
            return
        try:
            refreshed = await self._extract_refreshed(
                orig_url, headers, bypass_warp, forced_proxy
            )
        except Exception as exc:
            logger.debug("Proactive token refresh failed for %s: %s", stream_key[:8], exc)
            return
        if not refreshed:
            return
        seg_filename = stream_url.rsplit("/", 1)[-1].split("?")[0]
        fresh_url = self._fresh_segment_url(refreshed, seg_filename, stream_url)
        if not fresh_url:
            return
        self._store_cdn_token(
            stream_key,
            stream_url,
            fresh_url,
            f"Proactive CDN token refresh (segment {seg_filename})",
        )

    async def _proxy_segment(self, request, segment_url, stream_headers, segment_name):
        """✅ NUOVO: Proxy dedicato per segmenti .ts con Content-Disposition"""
        forced_proxy = None
        try:
            headers = dict(stream_headers)
            is_special_cdn = is_special_cdn_stream(segment_url)

            # Pass headers from query parameters (h_ parameters)
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:]
                    # Remove duplicate headers case-insensitively
                    keys_to_remove = [k for k in headers.keys() if k.lower() == header_name.lower()]
                    for k in keys_to_remove:
                        del headers[k]
                    headers[header_name] = param_value

            # Strip IP/Proxy leak headers
            for h in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                headers.pop(h, None)
                headers.pop(h.lower(), None)

            # ponytail: strip Accept-Language for lulustream source to prevent 403 Forbidden
            orig_url = request.query.get("orig_url", "")
            extractor_key = request.query.get("extractor_key", "")
            log_context = lambda route=None: request_log_context(
                request,
                segment_url,
                route=safe_log_route(route),
                extractor=extractor_key or None,
            )
            if "lulustream" in orig_url or "luluvdo" in orig_url or extractor_key == "lulustream":
                headers.pop("accept-language", None)
                headers.pop("Accept-Language", None)

            # Normalize critical headers to Title-Case
            for key in list(headers.keys()):
                if key.lower() == "user-agent":
                    headers["User-Agent"] = headers.pop(key)
                elif key.lower() == "referer":
                    headers["Referer"] = headers.pop(key)
                elif key.lower() == "origin":
                    headers["Origin"] = headers.pop(key)
                elif key.lower() == "authorization":
                    headers["Authorization"] = headers.pop(key)
                elif key.lower() == "cookie":
                    headers["Cookie"] = headers.pop(key)

            # Image-wrapped segments must be fetched whole: a partial PNG
            # cannot be unwrapped and would be dropped as 204, stalling iOS
            # players that probe with a byte range.
            image_segment = self._is_image_url(segment_url)
            for header in ["range", "if-none-match", "if-modified-since"]:
                if header in request.headers and not (header == "range" and image_segment):
                    headers[header] = request.headers[header]

            # DASH SegmentList relays carry the source byte-range in the
            # generated URL. Forward it when the player sends no Range header.
            source_range = request.query.get("range")
            if source_range and not image_segment and re.fullmatch(r"\d+-\d+", source_range):
                headers["Range"] = f"bytes={source_range}"

            # Media segments must not be content-encoded in transit.  aiohttp
            # transparently decodes gzip/br, while forwarding the upstream
            # Content-Length would make Safari wait for bytes that no longer
            # exist and repeatedly seek backwards.
            segment_ext = os.path.splitext(segment_name.split("?", 1)[0].lower())[1]
            if segment_ext in {".ts", ".m4s", ".mp4", ".m4a", ".m4v", ".m4i", ".aac", ".dash"}:
                headers["Accept-Encoding"] = "identity"

            if is_special_cdn:
                headers["Accept-Encoding"] = "identity"

            # ✅ Use pooled session with automatic retry failover
            bypass_warp = request.query.get("warp", "").lower() == "off"
            forced_proxy = request.query.get("proxy") or None
            _seg_proxy_token = None
            if forced_proxy and forced_proxy.lower() == "off":
                forced_proxy = None
                _seg_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
                logger.debug(f"🔍 [Segment-DEBUG] proxy=off detected, BYPASS_PROXIES_CONTEXT=True, bypass_warp={bypass_warp}")
            forced_proxy = _config.resolve_proxy_alias(forced_proxy)
            forced_proxy = self._discard_disabled_warp_route(
                forced_proxy, bypass_warp
            )

            stream_session_key = request.query.get("stream_key") or self._stream_key_for_url(
                request.query.get("orig_url") or segment_url
            )

            current_proxy = forced_proxy
            attempts = 2 if forced_proxy else 1
            session = None
            session_proxy = None
            resp = None
            resp_ctx = None

            for attempt in range(attempts):
                try:
                    session, session_proxy = await self._get_proxy_session(
                        segment_url,
                        bypass_warp=bypass_warp,
                        forced_proxy=current_proxy,
                        session_key=stream_session_key,
                    )
                    disable_ssl = get_ssl_setting_for_url(segment_url) or check_vavoo_request(headers, request, segment_url)
                    # ✅ Use yarl.URL with encoded=True to prevent double-encoding of commas
                    final_segment_url = yarl.URL(segment_url, encoded=True)
                    resp_ctx = session.get(
                        final_segment_url,
                        headers=headers,
                        ssl=not disable_ssl,
                        timeout=ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=None),
                    )
                    resp = await resp_ctx.__aenter__()
                    break
                except ALL_PROXY_ERRORS + (ClientConnectionError, asyncio.TimeoutError, OSError) as e:
                    if session and session_proxy and not session.closed:
                        await session.close()
                        session = None
                        session_proxy = None
                    if attempt == 0 and current_proxy:
                        logger.warning(
                            "Segment proxy failed for %s: %r. Retrying with a different proxy. [%s]",
                            segment_name,
                            e,
                            log_context(current_proxy),
                        )
                        self._mark_proxy_dead_if_allowed(
                            current_proxy,
                            extractor_key=request.query.get("extractor_key"),
                        )
                        if await self._recover_tunnel_proxy(current_proxy):
                            continue
                        new_proxy = get_proxy_for_url(segment_url, bypass_warp=bypass_warp)
                        if new_proxy and new_proxy != current_proxy:
                            current_proxy = new_proxy
                            continue
                    raise e

            try:
                response_headers = {}

                for header in [
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "last-modified",
                    "etag",
                ]:
                    if header in resp.headers:
                        response_headers[header] = resp.headers[header]

                # Preserve the container type for fMP4/DASH segments. Marking
                # every segment as MPEG-TS makes VLC download .m4s data without
                # being able to demux it. Keep attachment semantics for legacy TS.
                segment_path = segment_name.split("?", 1)[0].lower()
                segment_ext = os.path.splitext(segment_path)[1]
                is_fmp4 = segment_ext in {".m4s", ".mp4", ".m4a", ".m4v", ".m4i", ".dash"}
                requested_media_type = request.query.get("media_type", "").lower()
                set_response_header(
                    response_headers,
                    "Content-Type",
                    (
                        "audio/mp4"
                        if is_fmp4 and requested_media_type == "audio"
                        else ("video/mp4" if is_fmp4 else "video/mp2t")
                    ),
                )
                if not is_fmp4:
                    set_response_header(
                        response_headers,
                        "Content-Disposition",
                        f'attachment; filename="{segment_name}"',
                    )
                set_response_header(
                    response_headers, "Access-Control-Allow-Origin", "*"
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Methods",
                    "GET, HEAD, OPTIONS",
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Headers",
                    "Range, Content-Type",
                )

                # Image-wrapped segments need the full body to be unwrapped, so
                # they must never take the chunk-by-chunk streaming path.
                # Sniff the MAGIC bytes, not just URL/content-type: 861 proved
                # upstream can serve PNG bytes as video/mp2t with a plain .ts URL.
                is_upstream_image = (
                    resp.headers.get("content-type", "").lower().startswith("image/")
                    or self._is_image_url(segment_url)
                )
                if is_upstream_image:
                    content_bytes = await resp.read()
                    if len(content_bytes) > self._IMAGE_UNWRAP_MAX_BYTES:
                        logger.warning(
                            "Oversize image segment for %s (%d bytes), skipping",
                            segment_name, len(content_bytes),
                        )
                        return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
                    if not self._is_image_magic(content_bytes):
                        # False positive of the URL heuristic: plain TS with a
                        # weird URL. Forward untouched, never unwrap garbage.
                        return web.Response(body=content_bytes, status=resp.status, headers=response_headers)
                    unwrapped = await asyncio.to_thread(self._strip_fake_png_header_from_ts, content_bytes)
                    if self._is_valid_ts(unwrapped):
                        set_response_header(response_headers, "Content-Type", "video/mp2t")
                        set_response_header(response_headers, "Content-Length", str(len(unwrapped)))
                        response_headers.pop("content-range", None)
                        response_headers.pop("Content-Range", None)
                        response_headers.pop("accept-ranges", None)
                        response_headers.pop("Accept-Ranges", None)
                        # Full buffered body: always 200. Echoing an upstream 206
                        # without Content-Range is malformed and stalls ffmpeg.
                        return web.Response(body=unwrapped, status=200, headers=response_headers)
                    # Unwrap failed or not a TS: static ad/placeholder banner,
                    # or corrupt payload. NEVER forward it as video (green screen).
                    logger.warning(
                        "Dropped non-video static image segment for %s (%d bytes, no TS sync byte)",
                        segment_name, len(content_bytes)
                    )
                    return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})

                # Peek the first chunk BEFORE prepare(): if the magic says image
                # (heuristic above missed it), buffer the whole body and unwrap.
                # The old code stripped only the first chunk and forwarded PNG
                # bytes as video/mp2t in silence -> green screen on VLC+APTV.
                stream_iter = resp.content.iter_any()
                try:
                    first_chunk = await stream_iter.__anext__()
                except StopAsyncIteration:
                    first_chunk = b""
                if first_chunk and self._is_image_magic(first_chunk):
                    logger.warning(
                        "Sniffed image-wrapped segment for %s (ct=%s) -> buffered unwrap",
                        segment_name, resp.headers.get("content-type", "?"),
                    )
                    buf = bytearray(first_chunk)
                    async for chunk in stream_iter:
                        buf += chunk
                        if len(buf) > self._IMAGE_UNWRAP_MAX_BYTES:
                            logger.warning(
                                "Oversize image segment for %s (%d bytes), skipping",
                                segment_name, len(buf),
                            )
                            return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
                    content_bytes = bytes(buf)
                    unwrapped = await asyncio.to_thread(self._strip_fake_png_header_from_ts, content_bytes)
                    if self._is_valid_ts(unwrapped):
                        set_response_header(response_headers, "Content-Type", "video/mp2t")
                        set_response_header(response_headers, "Content-Length", str(len(unwrapped)))
                        response_headers.pop("content-range", None)
                        response_headers.pop("Content-Range", None)
                        response_headers.pop("accept-ranges", None)
                        response_headers.pop("Accept-Ranges", None)
                        # Full buffered body: always 200. Echoing an upstream 206
                        # without Content-Range is malformed and stalls ffmpeg.
                        return web.Response(body=unwrapped, status=200, headers=response_headers)
                    logger.warning(
                        "Dropped non-video static image segment for %s (%d bytes, no TS sync byte)",
                        segment_name, len(content_bytes)
                    )
                    return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})

                response = web.StreamResponse(status=resp.status, headers=response_headers)
                await response.prepare(request)

                try:
                    # Pure relay: at this point magic says NOT image, so no
                    # strip/unwrap here (the old partial first-chunk strip could
                    # not unwrap 7MB PNGs and forwarded them as video).
                    if first_chunk:
                        await self._write_with_backpressure(response, first_chunk)
                    async for chunk in stream_iter:
                        if self._client_gone(request):
                            return response
                        await self._write_with_backpressure(response, chunk)
                    await response.write_eof()
                    return response
                except (ClientPayloadError, ConnectionResetError, OSError) as e:
                    logger.info(
                        "Segment stream interrupted for %s [%s]: %s [%s]",
                        segment_name,
                        type(e).__name__,
                        e,
                        log_context(session_proxy or forced_proxy),
                    )
                    return response
                except Exception as e:
                    if "Connection lost" not in str(e) and "closing transport" not in str(e):
                        logger.error(
                            "Error streaming segment %s: %s [%s]",
                            segment_name,
                            e,
                            log_context(session_proxy or forced_proxy),
                        )
                    return response
            finally:
                if _seg_proxy_token is not None:
                    _shared.BYPASS_PROXIES_CONTEXT.reset(_seg_proxy_token)
                    _seg_proxy_token = None
                if resp_ctx:
                    await resp_ctx.__aexit__(None, None, None)
                if session and session_proxy and not session.closed:
                    await session.close()

        except Exception as e:
            if locals().get("_seg_proxy_token") is not None:
                _shared.BYPASS_PROXIES_CONTEXT.reset(_seg_proxy_token)
            logger.error(
                "Error in segment proxy: %s [%s]",
                e,
                request_log_context(request, segment_url, route=safe_log_route(forced_proxy)),
            )
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_stream(self, request, stream_url, stream_headers, bypass_warp=None, forced_proxy=None, force_direct=None, extractor_key=None, stream_key=None):
        """Effettua il proxy dello stream con gestione manifest e AES-128"""
        is_hls_segment_request = request.path.startswith("/proxy/hls/segment.")
        try:
            dual_video_track = int(request.query.get("dual_video_track", "0"))
            if not 0 <= dual_video_track <= 0xffffffff:
                raise ValueError()
        except ValueError:
            return web.Response(status=400, text="Invalid DUAL video track")
        if bypass_warp is None:
            bypass_warp = request.query.get("warp", "").lower() == "off"
        bypass_proxies = request.query.get("proxy", "").lower() == "off"

        # Keep the admin extractor policy on every relay/segment request. The
        # extracted VidFast URL can point to an internal provider (e.g. Viprow),
        # so relying only on the original query flags would re-enable WARP.
        extractor_key = extractor_key or request.query.get("extractor_key", "")
        stream_key = stream_key or request.query.get("stream_key")
        admin_warp_off, admin_proxy_off = get_extractor_routing_overrides(extractor_key)
        _ps_warp_token = None
        _ps_proxy_token = None
        if admin_warp_off:
            bypass_warp = True
            _ps_warp_token = _shared.BYPASS_WARP_CONTEXT.set(True)
        if admin_proxy_off:
            bypass_proxies = True
            _ps_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)

        if bypass_proxies and _ps_proxy_token is None:
            _ps_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
        if force_direct is None:
            force_direct = self._should_force_direct_from_query(request)
        else:
            force_direct = force_direct or self._should_force_direct_from_query(request)

        # Priorità: proxy passato esplicitamente -> proxy in query string.
        # In forced-direct retry (WARP fallback), ignore proxy query params.
        forced_proxy = (
            None
            if force_direct or bypass_proxies
            else (forced_proxy or request.query.get("proxy") or None)
        )
        forced_proxy = _config.resolve_proxy_alias(forced_proxy)
        forced_proxy = self._discard_disabled_warp_route(
            forced_proxy, bypass_warp
        )
        request._ps_forced_proxy = forced_proxy
        session = None
        session_proxy = None

        def log_context(route=None, target=None):
            return request_log_context(
                request,
                target or stream_url,
                route=safe_log_route(route),
                extractor=extractor_key or None,
            )

        coalesce_key = None
        coalesce_future = None

        try:
            # ✅ LIVE CDN TOKEN SUBSTITUTION: If the CDN token was refreshed via
            # re-extract on 403, replace the old base URL with the new one so every
            # subsequent segment gets a fresh token without re-extracting each time.
            stream_key = stream_key or request.query.get("stream_key")
            if stream_key and stream_key in getattr(self, '_renewed_cdn_tokens', {}):
                old_b, new_b, new_q = self._renewed_cdn_tokens[stream_key]
                if stream_url.startswith(old_b):
                    seg_name = stream_url[len(old_b):].split("?", 1)[0]
                    stream_url = new_b + seg_name + new_q
                    self._renewed_cdn_token_atimes[stream_key] = time.time()

            # Inline cleanup of stale tokens (backstop for cleanup cycle)
            now_ = time.time()
            stale_tok = [
                k for k, t in getattr(self, '_renewed_cdn_token_atimes', {}).items()
                if now_ - t > 300
            ]
            for k in stale_tok:
                self._renewed_cdn_tokens.pop(k, None)
                self._renewed_cdn_token_atimes.pop(k, None)

            # FCTV33: resolve smart link segment and ensure _s2 signature
            if ("_ctump=" in stream_url and "_ctuph=" in stream_url) or (extractor_key == "fctv33" and "_ver=" in stream_url and "_s2=" not in stream_url):
                try:
                    from extractors.fctv33 import resolve_fctv33_segment_url
                    stream_url = resolve_fctv33_segment_url(stream_url)
                except Exception as _fctv_err:
                    logger.debug("Failed to resolve FCTV33 segment in _proxy_stream: %s", _fctv_err)

            headers = dict(stream_headers)

            # Passa attraverso alcuni headers del client, ma FILTRA quelli che potrebbero leakare l'IP
            # Rimuoviamo specificamente i condizionali che possono causare 412/416 con URL dinamici
            image_segment = self._is_image_url(stream_url)
            for header in ["range", "if-none-match", "if-modified-since"]:
                if header in request.headers and not (header == "range" and image_segment):
                    headers[header] = request.headers[header]

            # ✅ FIX: Esplicita rimozione di If-Match e If-Range che spesso causano 416 su CDNs dinamici
            for h in ["if-match", "if-range"]:
                if h in headers: del headers[h]
                keys_to_remove = [k for k in headers.keys() if k.lower() == h]
                for k in keys_to_remove: del headers[k]

            dual_video_range = next((value for name, value in headers.items() if name.lower() == "range"), None)
            if dual_video_track:
                # Filtering needs complete boxes. Slice the filtered response
                # afterwards; equal-size free boxes preserve byte-range offsets.
                headers = {name: value for name, value in headers.items() if name.lower() not in {"range", "if-none-match", "if-modified-since"}}

            # Manifest requests must be fetched in full. Some players probe the
            # entry URL with a byte range, which turns upstream playlists into
            # partial 206 responses and breaks rewriting.
            if "manifest.m3u8" in request.path and "range" in headers:
                del headers["range"]

            # ✅ FIX: Remove 'zstd' from Accept-Encoding to prevent "Can not decode content-encoding" error
            if "accept-encoding" in headers:
                ae = headers["accept-encoding"].lower()
                if "zstd" in ae:
                    # Replace zstd with nothing, cleaning up commas
                    new_ae = ae.replace("zstd", "").replace(", ,", ",").strip(", ")
                    headers["accept-encoding"] = new_ae
            elif "Accept-Encoding" in headers:
                ae = headers["Accept-Encoding"].lower()
                if "zstd" in ae:
                    new_ae = ae.replace("zstd", "").replace(", ,", ",").strip(", ")
                    headers["Accept-Encoding"] = new_ae

            # Rimuovi esplicitamente headers che potrebbero rivelare l'IP originale
            for h in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                if h in headers:
                    del headers[h]

            # ponytail: strip Accept-Language for lulustream source to prevent 403 Forbidden
            orig_url = request.query.get("orig_url", "")
            query_extractor_key = request.query.get("extractor_key", "")
            if "lulustream" in orig_url or "luluvdo" in orig_url or query_extractor_key == "lulustream":
                headers.pop("accept-language", None)
                headers.pop("Accept-Language", None)

            # ✅ FIX: Normalizza gli header critici (User-Agent, Referer) in Title-Case
            for key in list(headers.keys()):
                if key.lower() == "user-agent":
                    headers["User-Agent"] = headers.pop(key)
                elif key.lower() == "referer":
                    headers["Referer"] = headers.pop(key)
                elif key.lower() == "origin":
                    headers["Origin"] = headers.pop(key)
                elif key.lower() == "authorization":
                    headers["Authorization"] = headers.pop(key)
                elif key.lower() == "cookie":
                    headers["Cookie"] = headers.pop(key)

            # Never negotiate compression for binary HLS media.  aiohttp may
            # decode it before relay, invalidating the upstream length/range
            # that native iOS playback relies on.
            if is_hls_segment_request:
                headers["Accept-Encoding"] = "identity"

            for internal_header in ["X-Direct-Connection", "x-direct-connection", "X-Force-Direct", "x-force-direct"]:
                if internal_header in headers:
                    del headers[internal_header]

            # ✅ FIX: Rimuovi duplicati espliciti se presenti (es. user-agent e User-Agent)
            # Questo può accadere se GenericHLSExtractor aggiunge 'user-agent' e noi abbiamo 'User-Agent' da h_ params
            # La normalizzazione sopra dovrebbe averli unificati, ma per sicurezza puliamo.

            # Log headers finali per debug
            # logger.info(f"   Final Stream Headers: {headers}")

            is_vavoo_req = check_vavoo_request(headers, request, stream_url)
            disable_ssl = (
                request.query.get("h_X-EasyProxy-Disable-SSL") == "1"
                or request.query.get("disable_ssl") == "1"
                or headers.get("X-EasyProxy-Disable-SSL") == "1"
                or get_ssl_setting_for_url(stream_url)
                or is_vavoo_req
            )
            headers.pop("X-EasyProxy-Disable-SSL", None)
            headers.pop("x-easyproxy-disable-ssl", None)
            is_special_cdn = is_special_cdn_stream(stream_url)

            if is_special_cdn:
                headers["Accept-Encoding"] = "identity"

            if is_hls_segment_request:
                self._schedule_cdn_token_refresh(
                    request.query.get("orig_url"),
                    stream_url,
                    headers,
                    bypass_warp,
                    forced_proxy,
                    stream_key,
                )

            if is_hls_segment_request and self._segment_coalesce_eligible(request):
                coalesce_key = (self._segment_coalesce_key(stream_url, headers), dual_video_track)
                inflight = self._segment_inflight_map()
                pending = inflight.get(coalesce_key)
                if pending is not None:
                    logger.info(
                        "Segment coalesce: waiting for in-flight fetch [%s]",
                        log_context(),
                    )
                    try:
                        shared = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=3.0
                        )
                    except asyncio.TimeoutError:
                        # The in-flight fetch is slow: do not queue this retry
                        # behind it, start an independent fetch.
                        shared = None
                    if shared is not None:
                        shared_body, shared_headers = shared
                        return web.Response(
                            body=shared_body,
                            status=200,
                            headers=dict(shared_headers),
                        )
                    if self._client_gone(request):
                        return web.Response(status=499, text="Client disconnected")
                coalesce_future = asyncio.get_running_loop().create_future()
                inflight[coalesce_key] = coalesce_future

            def _cookie_summary(value: str | None) -> str:
                if not value:
                    return "0"
                return str(len([part for part in value.split(";") if part.strip()]))

            def _short_url(value: str, limit: int = 120) -> str:
                if len(value) <= limit:
                    return value
                return value[:limit] + "..."

            # ✅ Use pooled session for better performance
            if force_direct:
                session, session_proxy = await self._get_proxy_session(
                    stream_url,
                    bypass_warp=True,
                    session_key=stream_key,
                    force_direct=True,
                )
                logger.info(
                    "[Proxy Stream] Using direct session (forced) [%s]",
                    log_context("DIRECT"),
                )
            else:
                session, session_proxy = await self._get_proxy_session(
                    stream_url,
                    bypass_warp=bypass_warp,
                    forced_proxy=forced_proxy,
                    session_key=stream_key,
                )

                # ✅ FIX LOG: Determine correct routing for display
                routing = safe_log_route(session_proxy)

                session_kind = "proxy" if session_proxy else "direct"
                logger.info(
                    "📡 [Proxy Stream] %s - Using %s session [%s]",
                    routing,
                    session_kind,
                    log_context(session_proxy),
                )

            # Premium edge playlists (daddylive/dlive.sx premiumtv player) need a
            # per-viewer token that expires after a couple of minutes, otherwise
            # the refresh answers 403 and the player loops the last window until
            # it dies. Refresh it here, with the session that fetches the
            # playlist, so the token matches this egress address.
            from extractors.dlstreams import (
                ensure_premium_playlist_url,
                invalidate_premium_playlist_token,
                premium_playlist_parts,
            )
            premium_route_key = session_proxy or "direct"
            is_premium_manifest = (
                not is_hls_segment_request
                and "manifest.m3u8" in request.path
                and premium_playlist_parts(stream_url) is not None
            )
            if is_premium_manifest:
                try:
                    stream_url = await ensure_premium_playlist_url(
                        session,
                        stream_url,
                        headers,
                        route_key=premium_route_key,
                    )
                except Exception as _pt_exc:
                    logger.debug(
                        "Premium playlist token refresh failed: %s [%s]",
                        _pt_exc,
                        log_context(session_proxy),
                    )

            # ⚡ Parallel range fetch to beat per-connection CDN throttling (e.g. vidsonic:
            # ~1.7 Mbps/conn vs 2.4 Mbps video -> 3 parallel ranges -> ~5 Mbps aggregate).
            # Falls back transparently to the single-connection path when not applicable.
            if is_hls_segment_request and stream_url.split("?", 1)[0].lower().endswith(".ts"):
                _seg_name = stream_url.rsplit("/", 1)[-1].split("?")[0]
                try:
                    return await self._proxy_segment_parallel(
                        request,
                        stream_url,
                        headers,
                        _seg_name,
                        bypass_warp,
                        forced_proxy,
                        session_key=stream_key,
                    )
                except _ParallelFallback as _pf:
                    logger.debug(
                        "parallel fetch skipped for %s: %s [%s]",
                        _seg_name,
                        _pf,
                        log_context(session_proxy or forced_proxy, stream_url),
                    )
                except Exception as _pe:
                    logger.debug(
                        "parallel fetch error for %s: %s [%s]",
                        _seg_name,
                        _pe,
                        log_context(session_proxy or forced_proxy, stream_url),
                    )

            use_curl_cffi = should_use_curl_cffi(
                stream_url,
                is_special_cdn,
                HAS_CURL_CFFI,
            )
            # Bounded read gap: a stalled CDN connection aborts in 10s instead
            # of hanging until the total timeout (players retry well before).
            segment_timeout = ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=10)

            if use_curl_cffi:
                logger.info(
                    "🚀 [curl_cffi] Using browser impersonation [%s]",
                    log_context(session_proxy),
                )
                curl_s = None
                try:
                    curl_options = _config.get_curl_ipv4_options(session_proxy).get("curl_options")
                    curl_kwargs = {"impersonate": "chrome124"}
                    if curl_options:
                        curl_kwargs["curl_options"] = curl_options
                    curl_s = get_curl_async_session()(
                        **curl_kwargs,
                    )
                    curl_headers = prepare_curl_headers(stream_url, headers)


                    curl_proxies = None
                    logger.debug(f"🚀 [curl_cffi] Sending headers for {stream_url[:50]}: {curl_headers}")

                    curl_proxies = None
                    if session_proxy:
                        curl_proxies = {"http": session_proxy, "https": session_proxy}

                    final_curl_url = final_curl_request_url(stream_url)

                    curl_resp = await curl_s.get(
                        final_curl_url,
                        headers=curl_headers,
                        proxies=curl_proxies,
                        verify=not disable_ssl,
                        timeout=30,
                        stream=True,
                        allow_redirects=True
                    )
                    class MockContent:
                        def __init__(self, c_resp): self.c_resp = c_resp
                        async def iter_any(self):
                            async for chunk in self.c_resp.aiter_content():
                                yield chunk
                        async def read(self, n=-1):
                            return await self.c_resp.acontent()

                    class MockResp:
                        def __init__(self, c_resp, curl_session=None):
                            self.status = c_resp.status_code
                            self.headers = c_resp.headers
                            self.url = yarl.URL(c_resp.url)
                            self.content = MockContent(c_resp)
                            self._curl_session = curl_session
                        async def read(self): return await self.content.read()
                        async def text(self, errors='replace'):
                            content = await self.read()
                            return content.decode('utf-8', errors=errors)
                        async def close(self):
                            if self._curl_session:
                                await self._curl_session.close()
                                self._curl_session = None
                        async def __aenter__(self): return self
                        async def __aexit__(self, exc_type, exc_val, exc_tb):
                            await self.close()

                    if curl_resp.status_code in [502, 503, 504]:
                        logger.warning(f"⚠️ [curl_cffi] {curl_resp.status_code} for {final_curl_url[:50]}: live offline o upstream errato")
                        goto_manifest_processing = False
                    else:
                        resp_ctx = MockResp(curl_resp, curl_s)
                        curl_s = None
                        goto_manifest_processing = True
                except Exception as e:
                    logger.error(
                        "❌ [curl_cffi] Error: %s [%s]",
                        e,
                        log_context(session_proxy),
                    )
                    goto_manifest_processing = False
                finally:
                    if curl_s:
                        try:
                            await curl_s.close()
                        except Exception:
                            pass
            else:
                goto_manifest_processing = False

            if not goto_manifest_processing:
                _extractor_key = request.query.get("extractor_key", "")
                _extractor = self.extractors.get(_extractor_key) if _extractor_key else None
                _curl_only = getattr(_extractor, 'curl_only', False) if _extractor else False
                if _curl_only and use_curl_cffi:
                    # CDN backend funziona solo via curl_cffi (es. embedst). 
                    # Mai aiohttp: dà 403 spurio o disconnessione TLS.
                    logger.debug("curl_only: no aiohttp fallback, returning error directly")
                    resp_ctx = None
                else:
                    if is_special_cdn:
                        request_target = urllib.parse.unquote(stream_url)
                    else:
                        request_target = yarl.URL(stream_url, encoded=True)
                    resp_ctx = session.get(
                        request_target,
                        headers=headers,
                        ssl=not disable_ssl,
                        timeout=segment_timeout if is_hls_segment_request else None,
                    )

            async def retry_with_different_proxy():
                if forced_proxy or not session_proxy:
                    return None
                old_proxy = session_proxy
                logger.info(
                    "Rotating proxy after upstream error [%s]",
                    log_context(old_proxy),
                )
                self._mark_proxy_dead_if_allowed(
                    old_proxy,
                    dead_duration=120,
                    extractor_key=request.query.get("extractor_key"),
                )
                rot_session, rot_proxy = await self._get_proxy_session(
                    stream_url,
                    bypass_warp=True,
                    forced_proxy=None,
                    session_key=stream_key,
                )
                if not rot_proxy or rot_proxy == old_proxy:
                    await rot_session.close()
                    rot_session, rot_proxy = await self._get_proxy_session(
                        stream_url,
                        bypass_warp=True,
                        forced_proxy=None,
                        session_key=stream_key,
                    )

                if not rot_proxy or rot_proxy == old_proxy:
                    if rot_session and not rot_session.closed:
                        await rot_session.close()
                    logger.warning(
                        "Proxy rotation: no alternate proxy; direct fallback disabled [%s]",
                        log_context(old_proxy),
                    )
                    return None

                # 1) Retry same URL via alternate proxy
                try:
                    rot_target = yarl.URL(stream_url, encoded=True) if not is_special_cdn else urllib.parse.unquote(stream_url)
                    async with rot_session.get(rot_target, headers=headers, ssl=not disable_ssl, timeout=segment_timeout) as rot_resp:
                        if rot_resp.status in [200, 206]:
                            logger.info(
                                "Proxy rotation successful: %s -> %s [%s]",
                                safe_log_route(old_proxy),
                                safe_log_route(rot_proxy),
                                log_context(rot_proxy),
                            )
                            rot_body = await rot_resp.read()
                            rh = dict(rot_resp.headers)
                            rh["Access-Control-Allow-Origin"] = "*"
                            return web.Response(body=rot_body, status=rot_resp.status, headers=rh)
                except Exception as exc:
                    logger.debug(
                        "Proxy rotation retry failed: %s [%s]",
                        exc,
                        log_context(rot_proxy or old_proxy),
                    )
                finally:
                    if rot_session and not rot_session.closed:
                        await rot_session.close()

                # 2) Re-extract not available (no captured manifest cache) — give up
                logger.info(
                    "Proxy rotation: re-extract not available (live mode, no cache) [%s]",
                    log_context(old_proxy),
                )
                return None

            async def retry_same_segment_after_payload_error(reason):
                if not request.path.startswith("/proxy/hls/segment."):
                    return None
                retry_target = urllib.parse.unquote(stream_url) if is_special_cdn else yarl.URL(stream_url, encoded=True)
                for attempt in range(2):
                    await asyncio.sleep(0.15 * (attempt + 1))
                    retry_session = None
                    retry_proxy = None
                    try:
                        retry_session, retry_proxy = await self._get_proxy_session(
                            stream_url,
                            bypass_warp=bypass_warp,
                            forced_proxy=forced_proxy,
                            session_key=stream_key,
                        )
                        async with retry_session.get(retry_target, headers=headers, ssl=not disable_ssl, timeout=segment_timeout) as retry_resp:
                            if retry_resp.status not in [200, 206]:
                                logger.debug(
                                    "Segment payload retry got status %s for %s",
                                    retry_resp.status,
                                    stream_url,
                                )
                                continue
                            retry_body = await retry_resp.read()
                            logger.info(
                                "✅ [Recupero] Segmento ripristinato (%d/2) per %s (%s)",
                                attempt + 1,
                                stream_url.split('/')[-1].split('?')[0],
                                type(reason).__name__,
                            )
                            return retry_body, retry_resp.headers, retry_resp.status
                    except (ClientPayloadError, ConnectionResetError, OSError, asyncio.TimeoutError) as exc:
                        logger.debug(
                            "Segment payload retry %d failed for %s: %r",
                            attempt + 1,
                            stream_url,
                            exc,
                        )
                    finally:
                        if retry_session and retry_proxy and not retry_session.closed:
                            await retry_session.close()
                return None

            if resp_ctx is None:
                # curl_only (embedst): curl_cffi fallito, live offline → return 503 subito
                logger.debug("curl_only: upstream offline, returning 503")
                return web.Response(
                    status=503,
                    text="Stream offline",
                    headers={"Access-Control-Allow-Origin": "*", "Content-Type": "text/plain; charset=utf-8"},
                )

            async with resp_ctx as resp:
                content_type = resp.headers.get("content-type", "").lower()

                if resp.status not in [200, 206]:
                    if resp.status == 403:
                        # 1. Quick retry if it's a transient CDN error (e.g. Varnish cache miss with Retry-After on TikTok/live CDNs)
                        if is_hls_segment_request:
                            retry_after = resp.headers.get("Retry-After")
                            x_cache = resp.headers.get("X-Cache", "")
                            server_hdr = resp.headers.get("Server", "").lower()
                            if retry_after is not None or "miss" in x_cache.lower() or "varnish" in server_hdr:
                                for att in range(2):
                                    await asyncio.sleep(0.5 * (att + 1))
                                    try:
                                        retry_target = yarl.URL(stream_url, encoded=True) if not is_special_cdn else urllib.parse.unquote(stream_url)
                                        async with session.get(retry_target, headers=headers, ssl=not disable_ssl, timeout=segment_timeout) as quick_resp:
                                            if quick_resp.status in (200, 206):
                                                logger.info("✅ Transient CDN 403 resolved on retry %d for %s", att + 1, stream_url.split('/')[-1].split('?')[0])
                                                q_body = await quick_resp.read()
                                                q_headers = dict(quick_resp.headers)
                                                if dual_video_track:
                                                    try:
                                                        q_body = await asyncio.to_thread(video_only, q_body, dual_video_track)
                                                    except ValueError:
                                                        return web.Response(status=502, text="Invalid DUAL fMP4 segment", headers={"Access-Control-Allow-Origin": "*"})
                                                    q_body, q_status, q_range = byte_range(q_body, dual_video_range)
                                                    q_headers = {name: value for name, value in q_headers.items() if name.lower() not in {"content-range", "content-length", "etag", "last-modified", "content-encoding", "transfer-encoding"}}
                                                    q_headers.update({"Content-Type": "video/mp4", "Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"})
                                                    if q_range:
                                                        q_headers["Content-Range"] = q_range
                                                    return web.Response(body=q_body, status=q_status, headers=q_headers)
                                                q_headers["Access-Control-Allow-Origin"] = "*"
                                                is_image = q_headers.get("content-type", "").startswith("image/") or q_body[:8] == b"\x89PNG\r\n\x1a\n"
                                                if is_image or request.path.endswith(".ts") or stream_url.endswith(".ts"):
                                                    q_body = await asyncio.to_thread(self._strip_fake_png_header_from_ts, q_body)
                                                    set_response_header(q_headers, "Content-Type", "video/mp2t")
                                                    set_response_header(q_headers, "Content-Length", str(len(q_body)))
                                                return web.Response(body=q_body, status=quick_resp.status, headers=q_headers)
                                    except Exception:
                                        pass

                        rot_response = await retry_with_different_proxy()
                        if rot_response:
                            return rot_response
                        # Last resort: re-extract to refresh signed CDN token (e.g. VidXgo, DLStreams)
                        re_response = await self._reextract_and_retry_segment(
                            request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl
                        )
                        if re_response:
                            return re_response
                    if is_special_cdn and resp.status == 403 and not goto_manifest_processing:
                        retry_result = await self._retry_special_cdn_request(
                            request_target,
                            headers,
                            disable_ssl,
                        )
                        if retry_result:
                            retry_headers = dict(retry_result["headers"])
                            retry_headers["Access-Control-Allow-Origin"] = "*"
                            logger.info(
                                "✅ Provider CDN retry success via alternate route: %s",
                                retry_result["proxy"],
                            )
                            return web.Response(
                                body=retry_result["body"],
                                status=retry_result["status"],
                                headers=retry_headers,
                            )
                    if resp.status == 403 and ("manifest.m3u8" in request.path or "playlist" in request.path):
                        # Premium playlist tokens are short-lived and IP-bound:
                        # drop the cached one and retry once with a fresh token
                        # before paying for a full re-extraction.
                        if is_premium_manifest and not getattr(request, "_premium_token_retried", False):
                            request._premium_token_retried = True
                            invalidate_premium_playlist_token(stream_url, premium_route_key)
                            fresh_url = await ensure_premium_playlist_url(
                                session,
                                stream_url,
                                headers,
                                force=True,
                                route_key=premium_route_key,
                            )
                            if fresh_url != stream_url:
                                logger.info(
                                    "🔑 Premium playlist token refreshed, retrying manifest [%s]",
                                    log_context(session_proxy),
                                )
                                return await self._proxy_stream(
                                    request,
                                    fresh_url,
                                    stream_headers,
                                    bypass_warp=bypass_warp,
                                    forced_proxy=forced_proxy,
                                    force_direct=force_direct,
                                    extractor_key=extractor_key,
                                    stream_key=stream_key,
                                )
                        re_manifest_resp = await self._reextract_and_retry_manifest(
                            request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl, extractor_key, stream_key
                        )
                        if re_manifest_resp:
                            return re_manifest_resp

                    error_body = await resp.content.read(4096) or b""
                    routing = safe_log_route(session_proxy or forced_proxy)
                    logger.warning(
                        "⚠️ Upstream returned error %s [%s]",
                        resp.status,
                        log_context(routing),
                    )
                    return web.Response(body=error_body, status=resp.status, headers={"Content-Type": content_type, "Access-Control-Allow-Origin": "*"})

                is_direct_media_stream = (
                    "video/" in content_type
                    or stream_url.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".mts", ".ts", ".m2ts"))
                )

                # ✅ FIX BUFFERING: Stream HLS segments chunk-by-chunk
                # instead of buffering entirely with resp.read(). This prevents
                # SocketTimeoutError on large segments via slow proxies and
                # reduces perceived latency for the player.
                is_segment_like = (
                    is_hls_segment_request
                    and any(stream_url.lower().split('?')[0].endswith(ext) for ext in
                            ['.ts', '.m4s', '.aac', '.m4a', '.m4v', '.m4i', '.mp4', '.mkv', '.avi', '.mov'])
                    and 'mpegurl' not in content_type
                    and not content_type.startswith('text/')
                )
                # Image-wrapped segments need the full body to be unwrapped, so
                # they must not take the chunk-by-chunk streaming path.
                # (Magic sniffed again on the body below; here headers+URL only.)
                is_image_payload = (
                    content_type.startswith("image/")
                    or self._is_image_url(stream_url)
                )

                if (is_direct_media_stream or is_segment_like) and not is_image_payload and not dual_video_track:
                    stream_ext = os.path.splitext(stream_url.split("?", 1)[0].lower())[1]
                    is_fmp4_segment = stream_ext in {".m4s", ".mp4", ".m4a", ".m4v", ".m4i"}
                    # fMP4 has its own init box: never treat it as image-wrapped.
                    if is_segment_like and not is_fmp4_segment:
                        peek_iter = resp.content.iter_any()
                        try:
                            peek_first = await peek_iter.__anext__()
                        except StopAsyncIteration:
                            peek_first = b""
                        if peek_first and self._is_image_magic(peek_first):
                            # Same case as 861: PNG bytes with a plain .ts URL.
                            # Buffer + unwrap instead of relaying PNG as video.
                            logger.warning(
                                "Sniffed image-wrapped segment for %s (ct=%s) -> buffered unwrap",
                                stream_url.split("/")[-1].split("?")[0][:80], content_type or "?",
                            )
                            buf = bytearray(peek_first)
                            async for chunk in peek_iter:
                                buf += chunk
                                if len(buf) > self._IMAGE_UNWRAP_MAX_BYTES:
                                    logger.warning(
                                        "Oversize image segment (%d bytes), skipping [%s]",
                                        len(buf), stream_url.split("/")[-1].split("?")[0][:80],
                                    )
                                    return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
                            img_bytes = bytes(buf)
                            unwrapped = await asyncio.to_thread(self._strip_fake_png_header_from_ts, img_bytes)
                            if self._is_valid_ts(unwrapped):
                                img_headers = {
                                    "Content-Type": "video/mp2t",
                                    "Content-Length": str(len(unwrapped)),
                                    "Access-Control-Allow-Origin": "*",
                                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                                    "Access-Control-Allow-Headers": "Range, Content-Type",
                                }
                                if coalesce_future is not None and resp.status == 200:
                                    coalesce_future.set_result((unwrapped, dict(img_headers)))
                                # Full buffered body: always 200, never echo 206.
                                return web.Response(body=unwrapped, status=200, headers=img_headers)
                            logger.warning(
                                "Dropped non-video static image segment (%d bytes, no TS sync byte) [%s]",
                                len(img_bytes), stream_url[:100],
                            )
                            return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
                    else:
                        peek_iter = None
                        peek_first = b""
                    requested_media_type = request.query.get("media_type", "").lower()
                    seg_content_type = (
                        "audio/mp4"
                        if is_segment_like and is_fmp4_segment and requested_media_type == "audio"
                        else (
                            "video/mp4"
                            if is_segment_like and is_fmp4_segment
                            else ("video/mp2t" if is_segment_like else content_type)
                        )
                    )
                    response_headers = {
                        "Content-Type": seg_content_type,
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": "Range, Content-Type",
                    }
                    for h in ["content-length", "content-range", "accept-ranges"]:
                        if h in resp.headers: response_headers[h] = resp.headers[h]

                    response = web.StreamResponse(status=resp.status, headers=response_headers)
                    await response.prepare(request)
                    try:
                        # Pure relay. Image-magic already diverted to buffered
                        # unwrap above, so no strip here: the old partial
                        # first-chunk strip could not unwrap 7MB PNGs and
                        # forwarded them as video (silent green screen).
                        if peek_iter is not None:
                            if peek_first:
                                await self._write_with_backpressure(response, peek_first)
                            async for chunk in peek_iter:
                                await self._write_with_backpressure(response, chunk)
                        else:
                            async for chunk in resp.content.iter_any():
                                await self._write_with_backpressure(response, chunk)
                        await response.write_eof()
                        return response
                    except (ClientPayloadError, ConnectionResetError, OSError) as e:
                        logger.info(
                            "Stream relay interrupted [%s]: %s [%s]",
                            type(e).__name__,
                            e,
                            log_context(session_proxy or forced_proxy),
                        )
                        return response
                    except Exception as e:
                        if "Connection lost" not in str(e) and "closing transport" not in str(e):
                            logger.error(
                                "❌ Stream error [%s]: %r [%s]",
                                type(e).__name__,
                                e,
                                log_context(session_proxy or forced_proxy),
                            )
                        return response

                response_source_headers = resp.headers
                response_status = resp.status
                try:
                    content_bytes = await resp.read()
                except (ClientPayloadError, ConnectionResetError, OSError) as e:
                    retry_result = await retry_same_segment_after_payload_error(e)
                    if not retry_result:
                        raise
                    content_bytes, response_source_headers, response_status = retry_result
                manifest_content = None
                try:
                    decoded_text = content_bytes.decode("utf-8", errors='replace')
                    if decoded_text.lstrip().startswith("#EXTM3U"):
                        manifest_content = decoded_text
                except Exception:
                    logger.debug("Response is not valid UTF-8 text (expected for segments)")
                    pass

                if manifest_content is None and (".m3u8" in stream_url or "mpegurl" in content_type):
                    try:
                        decoded_text = content_bytes.decode("utf-8", errors='replace')
                        if decoded_text.lstrip().startswith("#EXTM3U"):
                            manifest_content = decoded_text
                        else:
                            logger.warning(
                                "Upstream did not return a valid HLS manifest: %s [%s]",
                                decoded_text[:120].replace("\n", "\\n"),
                                log_context(session_proxy or forced_proxy),
                            )
                            return web.Response(
                                text="Upstream did not return a valid HLS manifest",
                                status=502,
                                headers={
                                    "Content-Type": "text/plain; charset=utf-8",
                                    "Access-Control-Allow-Origin": "*",
                                },
                            )
                    except Exception:
                        pass

                if manifest_content:
                    logger.info(f"📄 HLS manifest detected: {stream_url}")
                    proxy_base = get_public_base_url(request)
                    original_url = request.query.get("orig_url") or request.query.get("url") or request.query.get("d", "")
                    use_short_hls_urls = should_use_short_manifest_urls(
                        original_url,
                        request.query.get("host", ""),
                        str(resp.url),
                    )

                    disable_ssl = request.query.get("disable_ssl") == "1" or get_ssl_setting_for_url(str(resp.url))

                    rewritten = await ManifestRewriter.rewrite_manifest_urls(
                        manifest_content=manifest_content,
                        base_url=str(resp.url),
                        proxy_base=proxy_base,
                        stream_headers=headers,
                        original_channel_url=original_url,
                        api_password=request.query.get("api_password"),
                        no_bypass=request.query.get("no_bypass") == "1",
                        shorten_url_func=self.shorten_hls_url if use_short_hls_urls else None,
                        bypass_warp=bypass_warp,
                        bypass_proxies=bypass_proxies,
                        disable_ssl=disable_ssl,
                        selected_proxy=forced_proxy, # ✅ PASSA IL PROXY FORZATO
                        force_direct=force_direct,
                        extractor_key=extractor_key or request.query.get("extractor_key"),
                        stream_key=stream_key or request.query.get("stream_key"),
                        max_res=self._request_forces_max_res(request, extractor_key, "hls"),
                        dual_video_track=dual_video_track,
                    )
                    # Never hand the player an empty media playlist: a live
                    # playlist with zero segments (transient upstream outage)
                    # wedges player sessions started on it, even after upstream
                    # recovers. Masters pass through: only media playlists are
                    # required to list at least one segment URI.
                    if "#EXT-X-STREAM-INF" not in rewritten and not any(
                        line.strip() and not line.strip().startswith("#")
                        for line in rewritten.splitlines()
                    ):
                        logger.warning(
                            "Upstream manifest for %s has no playable segments (%d bytes), returning 503 instead of empty playlist [%s]",
                            stream_url,
                            len(rewritten),
                            log_context(session_proxy or forced_proxy),
                        )
                        return web.Response(
                            text="Upstream playlist temporarily empty, retrying",
                            status=503,
                            headers={"Content-Type": "text/plain; charset=utf-8", "Access-Control-Allow-Origin": "*", "Retry-After": "5", "Cache-Control": "no-cache"},
                        )
                    return web.Response(text=rewritten, headers={
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                    })

                # ✅ AGGIORNATO: Gestione per manifest MPD (DASH) - separate block
                if manifest_content is None and ("dash+xml" in content_type or stream_url.endswith(".mpd")):
                    manifest_content = content_bytes.decode("utf-8", errors='replace')

                    # ✅ CORREZIONE: Rileva lo schema e l'host corretti quando dietro un reverse proxy
                    proxy_base = get_public_base_url(request)

                    # Recupera parametri
                    clearkey_param = parse_clearkey_params(request)

                    # --- MPD -> HLS Conversion ---
                    if MPDToHLSConverter:
                        logger.info(
                            f"🔄 [Legacy Mode] Converting MPD to HLS for {stream_url}"
                        )
                        try:
                            converter = MPDToHLSConverter()

                            # Check if requesting a Media Playlist (Variant)
                            rep_id = request.query.get("rep_id")
                            # SegmentBase carries the SIDX index and media as
                            # byte ranges. Expand only the requested variant;
                            # expanding the master would add an unnecessary
                            # upstream range request to every startup.
                            if rep_id and "indexRange" in manifest_content:
                                from utils.dash_ranges import expand_segment_bases, fetch_range

                                async def _fetch_sidx(url, range_str):
                                    return await fetch_range(session, url, headers, range_str)

                                manifest_content = await expand_segment_bases(
                                    manifest_content,
                                    str(resp.url),
                                    _fetch_sidx,
                                    only_rep_id=rep_id,
                                )

                            mpd_params = request.query_string or ""
                            if extractor_key and "extractor_key=" not in mpd_params:
                                mpd_params = f"{mpd_params}&extractor_key={urllib.parse.quote(extractor_key, safe='')}" if mpd_params else f"extractor_key={urllib.parse.quote(extractor_key, safe='')}"
                            if stream_key and "stream_key=" not in mpd_params:
                                mpd_params = f"{mpd_params}&stream_key={urllib.parse.quote(stream_key, safe='')}" if mpd_params else f"stream_key={urllib.parse.quote(stream_key, safe='')}"
                            if "max_res=" not in mpd_params and self._request_forces_max_res(
                                request, extractor_key, "mpd"
                            ):
                                mpd_params = f"{mpd_params}&max_res=true" if mpd_params else "max_res=true"

                            if rep_id:
                                # Generate Media Playlist (Segments)
                                hls_playlist = await asyncio.to_thread(
                                    converter.convert_media_playlist,
                                    manifest_content,
                                    rep_id,
                                    proxy_base,
                                    stream_url,
                                    mpd_params,
                                    clearkey_param,
                                )
                                # Log first few lines for debugging
                                logger.debug(
                                    f"📜 Generated Media Playlist for {rep_id} (first 10 lines):\n{chr(10).join(hls_playlist.splitlines()[:10])}"
                                )
                            else:
                                # Generate Master Playlist
                                hls_playlist = await asyncio.to_thread(
                                    converter.convert_master_playlist,
                                    manifest_content,
                                    proxy_base,
                                    stream_url,
                                    mpd_params,
                                )
                                logger.debug(
                                    f"📜 Generated Master Playlist (first 5 lines):\n{chr(10).join(hls_playlist.splitlines()[:5])}"
                                )

                            return web.Response(
                                text=hls_playlist,
                                headers={
                                    "Content-Type": "application/vnd.apple.mpegurl",
                                    "Access-Control-Allow-Origin": "*",
                                    "Cache-Control": "no-cache",
                                },
                            )
                        except Exception as e:
                            logger.error(
                                "❌ Legacy conversion failed: %s [%s]",
                                e,
                                log_context(session_proxy or forced_proxy),
                            )
                            # Fallback to DASH proxy if conversion fails
                            pass

                    # --- DEFAULT: DASH Proxy (Rewriting) ---
                    rep_id = request.query.get("rep_id")

                    api_password = request.query.get("api_password")
                    drm_token = request.query.get("drm_token")
                    if clearkey_param and not drm_token:
                        drm_token = seal_clearkey(clearkey_param)
                    rewritten_manifest = ManifestRewriter.rewrite_mpd_manifest(
                        manifest_content,
                        stream_url,
                        proxy_base,
                        headers,
                        clearkey_param,
                        api_password,
                        bypass_warp=bypass_warp,
                        bypass_proxies=bypass_proxies,
                        drm_token=drm_token,
                        extractor_key=extractor_key or request.query.get("extractor_key"),
                        stream_key=stream_key or request.query.get("stream_key"),
                        max_res=self._request_forces_max_res(request, extractor_key, "mpd"),
                    )

                    return web.Response(
                        text=rewritten_manifest,
                        headers={
                            "Content-Type": "application/dash+xml",
                            "Content-Disposition": 'attachment; filename="stream.mpd"',
                            "Access-Control-Allow-Origin": "*",
                            "Cache-Control": "no-cache",
                        },
                    )

                # Streaming normale per altri tipi di contenuto (segmenti binari)
                # Il body è già stato letto in content_bytes, usiamo quello.
                segment_was_stripped = False
                is_wrapped_image_segment = (
                    is_image_payload
                    or content_bytes[:8] == b"\x89PNG\r\n\x1a\n"
                    or (content_bytes[:4] == b"RIFF" and content_bytes[8:12] == b"WEBP")
                )
                if (
                    request.path.endswith(".ts")
                    or stream_url.endswith(".ts")
                    or is_wrapped_image_segment
                ):
                    original_len = len(content_bytes)
                    content_bytes = await asyncio.to_thread(
                        self._strip_fake_png_header_from_ts, content_bytes
                    )
                    segment_was_stripped = len(content_bytes) != original_len

                if dual_video_track:
                    try:
                        content_bytes = await asyncio.to_thread(video_only, content_bytes, dual_video_track)
                    except ValueError as exc:
                        logger.warning("DUAL video filtering failed: %s", exc)
                        return web.Response(status=502, text="Invalid DUAL fMP4 segment", headers={"Access-Control-Allow-Origin": "*"})

                response_headers = {}

                for header in [
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "last-modified",
                    "etag",
                ]:
                    if header in response_source_headers:
                        response_headers[header] = response_source_headers[header]

                if dual_video_track:
                    response_headers = {name: value for name, value in response_headers.items() if name.lower() not in {"content-range", "etag", "last-modified"}}
                    set_response_header(response_headers, "Content-Type", "video/mp4")
                    set_response_header(response_headers, "Accept-Ranges", "bytes")
                    content_bytes, response_status, filtered_range = byte_range(content_bytes, dual_video_range)
                    if filtered_range:
                        set_response_header(response_headers, "Content-Range", filtered_range)

                # ✅ FIX: Forza Content-Type coerente se il server non lo invia correttamente
                if (
                    stream_url.endswith(".ts") or request.path.endswith(".ts")
                ) and "video/mp2t" not in response_headers.get(
                    "content-type", ""
                ).lower():
                    set_response_header(response_headers, "Content-Type", "video/mp2t")
                elif (
                    stream_url.endswith(".vtt")
                    or stream_url.endswith(".webvtt")
                    or request.path.endswith(".vtt")
                ) and "text/vtt" not in response_headers.get(
                    "content-type", ""
                ).lower():
                    set_response_header(response_headers, "Content-Type", "text/vtt; charset=utf-8")
                if segment_was_stripped:
                    if response_headers.get("content-type", "").lower().startswith("image/"):
                        set_response_header(response_headers, "Content-Type", "video/mp2t")
                    set_response_header(
                        response_headers, "Content-Length", str(len(content_bytes))
                    )
                    response_headers.pop("content-range", None)
                    response_headers.pop("Content-Range", None)
                    response_headers.pop("accept-ranges", None)
                    response_headers.pop("Accept-Ranges", None)
                elif is_wrapped_image_segment:
                    # An image segment was received, but no valid TS could be unwrapped (static ad/placeholder banner)
                    if content_bytes[:8] == b"\x89PNG\r\n\x1a\n" or (content_bytes[:4] == b"RIFF" and content_bytes[8:12] == b"WEBP"):
                        logger.warning(
                            "Dropped non-video static image segment (%d bytes, no TS sync byte) [%s]",
                            len(content_bytes),
                            stream_url[:100],
                        )
                        return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*"})

                set_response_header(
                    response_headers, "Access-Control-Allow-Origin", "*"
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Methods",
                    "GET, HEAD, OPTIONS",
                )
                set_response_header(
                    response_headers,
                    "Access-Control-Allow-Headers",
                    "Range, Content-Type",
                )

                # Override content-length with actual bytes read, evitando duplicati case-insensitive
                set_response_header(
                    response_headers, "Content-Length", str(len(content_bytes))
                )

                if coalesce_future is not None and response_status == 200:
                    coalesce_future.set_result(
                        (content_bytes, dict(response_headers))
                    )

                return web.Response(
                    body=content_bytes,
                    status=response_status,
                    headers=response_headers,
                )


        except (ClientPayloadError, ConnectionResetError) as e:
            active_proxy = session_proxy or forced_proxy
            if active_proxy:
                logger.info(
                    "Stream interrupted while using proxy %s (payload/reset): %r.",
                    active_proxy, e
                )
            logger.info(f"[INFO] Client disconnected from stream: {stream_url} ({str(e)})")
            return web.Response(text="Client disconnected", status=499)

        except ALL_PROXY_ERRORS + (
            ServerDisconnectedError,
            ClientConnectionError,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            # Errori di connessione upstream
            active_proxy = session_proxy or forced_proxy
            if active_proxy:
                logger.warning(
                    "Proxy connection to source failed: %r [%s]",
                    e,
                    log_context(active_proxy),
                )
                self._mark_proxy_dead_if_allowed(
                    active_proxy,
                    extractor_key=request.query.get("extractor_key"),
                )
                if await self._recover_tunnel_proxy(active_proxy):
                    if not getattr(request, "_ps_retried", False):
                        request._ps_retried = True
                        logger.warning(
                            "Tunnel proxy reconnected; retrying request once [%s]",
                            log_context(active_proxy),
                        )
                        return await self._proxy_stream(
                            request,
                            stream_url,
                            stream_headers,
                            bypass_warp=bypass_warp,
                            forced_proxy=forced_proxy,
                            force_direct=force_direct,
                            extractor_key=extractor_key,
                            stream_key=stream_key,
                        )
            if active_proxy and getattr(_shared, 'WARP_PROXY_URL', None) and active_proxy == _shared.WARP_PROXY_URL:
                warp_healthy, warp_reason = await self._probe_warp(timeout_sec=3)
                if not warp_healthy:
                    logger.warning(
                        "WARP proxy confirmed unhealthy during stream failure: %s [%s]",
                        warp_reason,
                        log_context(active_proxy),
                    )
                    await self._restart_warp_if_socket_unhealthy(warp_reason)
                else:
                    logger.debug(
                        "WARP proxy healthy; stream failure is upstream source [%s]",
                        log_context(active_proxy),
                    )
            if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSLCertVerificationError" in str(e) or "CertificateError" in str(e):
                try:
                    host = urllib.parse.urlparse(stream_url).netloc.split(":")[0]
                    parts = host.split(".")
                    base_domain = ".".join(parts[-2:]) if len(parts) >= 2 else host
                except Exception:
                    base_domain = "target domain"
                logger.warning(
                    f"💡 SSL Certificate error for {stream_url}. "
                    f"Consider adding domain '{base_domain}' to Transport Routes with 'disable_ssl': true or using parameter 'disable_ssl=1'."
                )
            logger.warning(
                "⚠️ Connection lost with source: %s [%s]",
                e,
                log_context(active_proxy),
            )
            return web.Response(text=f"Upstream connection lost: {str(e)}", status=502)

        except Exception as e:
            err_msg = str(e)
            if "Connection lost" in err_msg or "Connection reset" in err_msg:
                active_proxy = session_proxy or forced_proxy
                if active_proxy:
                    logger.warning(
                        "Proxy connection lost/reset: %r [%s]",
                        e,
                        log_context(active_proxy),
                    )
                    self._mark_proxy_dead_if_allowed(
                        active_proxy,
                        extractor_key=request.query.get("extractor_key"),
                    )
                logger.info(
                    "[INFO] Stream connection closed by client/server [%s]",
                    log_context(active_proxy),
                )
                return web.Response(text="Connection lost", status=499)

            # If forced_proxy was set and failed with a proxy/connection error, re-extract
            forced_proxy = getattr(request, '_ps_forced_proxy', None)
            if forced_proxy and not getattr(request, '_ps_retried', False):
                err_lower = err_msg.lower()
                is_proxy_err = any(x in err_lower for x in ("invalid reply", "request rejected", "connection refused", "connection reset", "proxy connection timed out", "can't connect to server", "couldn't connect", "connect call failed", "0x9", "0x7", "socks5"))
                if is_proxy_err:
                    request._ps_retried = True
                    self._mark_proxy_dead_if_allowed(
                        forced_proxy,
                        extractor_key=request.query.get("extractor_key"),
                    )
                    recovered = await self._recover_tunnel_proxy(forced_proxy)
                    if recovered or not is_proxy_alive(forced_proxy):
                        logger.warning(
                            "Proxy failed, triggering re-extraction [%s]",
                            log_context(forced_proxy),
                        )
                        raise ProxyDeadRetryError("PROXY_DEAD_RETRY_EXTRACTION")
                    logger.info(
                        "Proxy had transient error, skipping re-extraction [%s]",
                        log_context(forced_proxy),
                    )

            logger.error(
                "❌ Generic error in stream proxy [%s]: %r [%s]",
                type(e).__name__,
                e,
                log_context(getattr(request, "_ps_forced_proxy", None)),
            )
            return web.Response(text=f"Stream error: {err_msg}", status=500)
        finally:
            if _ps_warp_token is not None:
                _shared.BYPASS_WARP_CONTEXT.reset(_ps_warp_token)
            if _ps_proxy_token is not None:
                _shared.BYPASS_PROXIES_CONTEXT.reset(_ps_proxy_token)
            if session and not session.closed and session_proxy is not None:
                await session.close()
            if coalesce_future is not None:
                inflight = getattr(self, "_segment_inflight", None)
                if inflight is not None and inflight.get(coalesce_key) is coalesce_future:
                    inflight.pop(coalesce_key, None)
                if not coalesce_future.done():
                    coalesce_future.set_result(None)

    async def _reextract_and_retry_manifest(
        self, request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl, extractor_key, stream_key
    ):
        """Re-extract the source on 403 when upstream manifest token has expired."""
        orig_url = request.query.get("orig_url")
        if not orig_url:
            return None

        # Prevent recursive re-extraction loop
        if getattr(request, "_manifest_reextracted", False):
            return None
        request._manifest_reextracted = True

        logger.info(
            "🔄 [Manifest 403] Upstream manifest token expired, re-extracting %s",
            orig_url,
        )
        try:
            refreshed = await self._extract_refreshed(
                orig_url, headers, bypass_warp, forced_proxy
            )
            if not refreshed or not refreshed.get("destination_url"):
                return None

            new_stream_url = refreshed["destination_url"]
            new_headers = refreshed.get("request_headers") or headers
            logger.info(
                "✅ [Manifest Recovered] Re-extraction succeeded: %s -> %s",
                stream_url[:60],
                new_stream_url[:60],
            )

            # Update live CDN token for stream_key so segments get the fresh token
            if stream_key:
                self._store_cdn_token(
                    stream_key,
                    stream_url,
                    new_stream_url,
                    "Updated CDN token via manifest refresh",
                )

            # Signed provider URLs (e.g. Vavoo) expire. Send the player to a
            # fresh stable manifest URL so it stops retrying the expired one.
            if "manifest.m3u8" in request.path and new_stream_url != stream_url:
                query = {"d": new_stream_url}
                for key, value in (new_headers or {}).items():
                    query[f"h_{key}"] = str(value)
                for name in (
                    "api_password", "disable_ssl", "warp", "proxy", "direct",
                ):
                    value = request.query.get(name)
                    if value:
                        query[name] = value
                if refreshed.get("disable_ssl") or (new_headers or {}).get("X-EasyProxy-Disable-SSL") == "1":
                    query["disable_ssl"] = "1"
                if extractor_key:
                    query["extractor_key"] = extractor_key
                if stream_key:
                    query["stream_key"] = stream_key
                query["orig_url"] = orig_url
                logger.info("↪️ [Manifest Refresh] Redirecting to fresh stable URL")
                return web.HTTPFound(
                    f"{get_public_base_url(request)}/proxy/hls/manifest.m3u8?"
                    f"{urllib.parse.urlencode(query)}"
                )

            return await self._proxy_stream(
                request,
                new_stream_url,
                new_headers,
                bypass_warp=bypass_warp,
                forced_proxy=forced_proxy,
                force_direct=force_direct,
                extractor_key=extractor_key,
                stream_key=stream_key,
            )
        except Exception as exc:
            logger.warning("Manifest re-extraction failed: %s", exc)
            return None

    async def _reextract_and_retry_segment(
        self, request, stream_url, headers, bypass_warp, forced_proxy, force_direct, disable_ssl
    ):
        """Re-extract the source on 403 to refresh signed CDN tokens, then retry the segment."""
        orig_url = request.query.get("orig_url")
        if not orig_url:
            return None

        # Only attempt for HLS segment requests with an extractor source URL
        if not request.path.startswith("/proxy/hls/segment."):
            return None

        try:
            refreshed = await self._extract_refreshed(
                orig_url, headers, bypass_warp, forced_proxy
            )
        except Exception as exc:
            logger.debug(
                "Re-extract for segment 403 failed: %s [%s]",
                exc,
                request_log_context(request, stream_url, route=safe_log_route(forced_proxy)),
            )
            return None

        headers = refreshed.get("request_headers") or headers
        forced_proxy = refreshed.get("selected_proxy") or forced_proxy
        force_direct = refreshed.get("force_direct", force_direct)
        bypass_warp = refreshed.get("bypass_warp", bypass_warp)

        seg_filename = stream_url.rsplit("/", 1)[-1].split("?")[0]
        fresh_url = self._fresh_segment_url(refreshed, seg_filename, stream_url)
        if not fresh_url or fresh_url.rsplit("/", 1)[-1].split("?")[0] != seg_filename:
            logger.debug(
                "Re-extract: could not locate %s in refreshed manifest [%s]",
                seg_filename,
                request_log_context(request, stream_url, route=safe_log_route(forced_proxy)),
            )
            return None

        # Fetch the segment with the fresh token
        retry_session = None
        need_close = False
        retry_proxy = None
        stream_session_key = request.query.get("stream_key") or self._stream_key_for_url(
            request.query.get("orig_url") or fresh_url
        )
        try:
            if force_direct:
                retry_session, retry_proxy = await self._get_proxy_session(
                    fresh_url,
                    bypass_warp=True,
                    session_key=stream_session_key,
                    force_direct=True,
                )
            else:
                retry_session, retry_proxy = await self._get_proxy_session(
                    fresh_url,
                    bypass_warp=bypass_warp,
                    forced_proxy=forced_proxy,
                    session_key=stream_session_key,
                )
                if retry_proxy:
                    need_close = True
            import yarl
            target = yarl.URL(fresh_url, encoded=True)
            async with retry_session.get(
                target,
                headers=headers,
                ssl=not disable_ssl,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as fr_resp:
                if fr_resp.status not in [200, 206]:
                    logger.warning(
                        "Re-extract segment retry still failed: status=%d [%s]",
                        fr_resp.status,
                        request_log_context(
                            request,
                            fresh_url,
                            route=safe_log_route(retry_proxy or forced_proxy),
                        ),
                    )
                    return None
                body = await fr_resp.read()
                fr_headers = getattr(fr_resp, "headers", None) or {}
                is_image = fr_headers.get("content-type", "").startswith("image/") or body[:8] == b"\x89PNG\r\n\x1a\n"
                if is_image or request.path.endswith(".ts") or stream_url.endswith(".ts"):
                    body = await asyncio.to_thread(self._strip_fake_png_header_from_ts, body)
                rh = {
                    "Access-Control-Allow-Origin": "*",
                    "Content-Type": "video/mp2t",
                    "Content-Length": str(len(body)),
                }
                logger.info(
                    "✅ Segment recovered via re-extract: %s [%s]",
                    seg_filename,
                    request_log_context(
                        request,
                        fresh_url,
                        route=safe_log_route(retry_proxy or forced_proxy),
                    ),
                )

                # Save refreshed CDN base URL for this stream_key so subsequent
                # segments use the new token without re-extracting each time.
                stream_key = request.query.get("stream_key")
                if stream_key:
                    self._store_cdn_token(
                        stream_key,
                        stream_url,
                        fresh_url,
                        "CDN token saved; subsequent segments skip re-extract",
                    )

                return web.Response(body=body, status=fr_resp.status, headers=rh)
        except Exception as exc:
            logger.debug("Re-extract segment fetch error: %s", exc)
            return None
        finally:
            if need_close and retry_session and not retry_session.closed:
                await retry_session.close()



    async def handle_decrypt_segment(self, request):
        """Decripta segmenti fMP4 lato server per ClearKey (legacy mode)."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        url = request.query.get("url")
        requested_media_type = request.query.get("media_type", "").lower() or "unknown"
        request_target = url or request.query.get("init_url")
        segment_name = os.path.basename(urllib.parse.urlsplit(request_target).path) if request_target else "unknown"
        decrypt_started = time.monotonic()
        logger.info(
            "🔓 Decrypt Request: track=%s segment=%s",
            requested_media_type,
            segment_name or "unknown",
        )

        init_url = request.query.get("init_url")
        key = request.query.get("key")
        key_id = request.query.get("key_id")
        protected_keys = parse_clearkey_params(request)
        if protected_keys:
            pairs = [
                pair.split(":", 1)
                for pair in protected_keys.split(",")
                if ":" in pair
            ]
            if pairs:
                key_id = ",".join(pair[0] for pair in pairs)
                key = ",".join(pair[1] for pair in pairs)

        is_init = request.query.get("is_init") == "1"
        skip_init = request.query.get("skip_init") == "1"
        init_range = request.query.get("init_range")
        media_range = request.query.get("media_range")

        for label, value in (("init_range", init_range), ("media_range", media_range)):
            if value and not re.fullmatch(r"\d+-\d+", value):
                return web.Response(status=400, text=f"Invalid {label}")

        if is_init:
            init_url = url
            init_range = init_range or media_range
            url = None
            media_range = None

        if not (url or init_url) or not key or not key_id:
            return web.Response(text="Missing url/init_url, key, or key_id", status=400)

        if decrypt_segment is None:
            return web.Response(
                text="Decrypt not available", status=503
            )

        try:
            # Ricostruisce gli headers per le richieste upstream
            headers = {"Connection": "keep-alive", "Accept-Encoding": "identity"}
            for param_name, param_value in request.query.items():
                if param_name.startswith("h_"):
                    header_name = param_name[2:].replace("_", "-")
                    headers[header_name] = param_value

            # Get proxy-enabled session for segment fetches
            bypass_warp = request.query.get("warp", "").lower() == "off"
            forced_proxy = request.query.get("proxy") or None
            _dec_proxy_token = None
            if forced_proxy and forced_proxy.lower() == "off":
                forced_proxy = None
                _dec_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
            forced_proxy = _config.resolve_proxy_alias(forced_proxy)
            forced_proxy = self._discard_disabled_warp_route(
                forced_proxy, bypass_warp
            )
            logger.debug(f"🔍 [Decrypt-DEBUG] bypass_warp={bypass_warp}, forced_proxy={forced_proxy}, warp_param='{request.query.get('warp', 'NOT_FOUND')}'")
            proxy_from_config = get_proxy_for_url(url or init_url, bypass_warp=bypass_warp)
            logger.debug(f"🔍 [Decrypt-DEBUG] get_proxy_for_url returned: {proxy_from_config}")
            stream_session_key = request.query.get("stream_key")
            if not stream_session_key:
                stream_session_key = self._stream_key_for_url(
                    request.query.get("orig_url")
                )
            segment_session, segment_proxy = await self._get_proxy_session(
                url or init_url,
                bypass_warp=bypass_warp,
                forced_proxy=forced_proxy,
                session_key=stream_session_key,
            )
            if segment_proxy:
                logger.info(f"📡 [Decrypt] Using session via proxy: {segment_proxy}")

            fetch_started_at = time.monotonic()
            fetch_elapsed = 0.0
            decrypt_elapsed = 0.0
            try:
                # Parallel download of init and media segment
                network_errors = ALL_PROXY_ERRORS + (
                    ClientConnectionError,
                    ServerDisconnectedError,
                    asyncio.TimeoutError,
                    OSError,
                )

                async def fetch_part(session, part_url, timeout, label, byte_range=None):
                    if not part_url:
                        return b"", False
                    disable_ssl = get_ssl_setting_for_url(part_url)
                    part_headers = dict(headers)
                    if byte_range:
                        part_headers["Range"] = f"bytes={byte_range}"
                        part_headers["Accept-Encoding"] = "identity"
                    connector = getattr(session, "connector", None)

                    def session_diagnostics():
                        acquired = getattr(connector, "_acquired", ()) if connector else ()
                        waiters = getattr(connector, "_waiters", {}) if connector else {}
                        return (
                            f"session={id(session)} closed={getattr(session, 'closed', None)} "
                            f"connector_closed={getattr(connector, 'closed', None)} "
                            f"acquired={len(acquired)} waiters={len(waiters)}"
                        )

                    started_at = time.monotonic()
                    try:
                        async with session.get(
                            part_url,
                            headers=part_headers,
                            ssl=not disable_ssl,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as resp:
                            # CDN may return 206 for valid range-based DASH
                            # segments; treat it like a successful fetch.
                            if resp.status in (200, 206):
                                content = await resp.read()
                                if content:
                                    return content, False
                            logger.error(
                                "❌ %s returned status %s [%s]",
                                label,
                                resp.status,
                                request_log_context(
                                    request,
                                    part_url,
                                    route=safe_log_route(segment_proxy or forced_proxy),
                                ),
                            )
                            return None, False
                    except network_errors as error:
                        logger.error(
                            "❌ Failed to fetch %s: %r elapsed=%.3fs %s [%s]",
                            label,
                            error,
                            time.monotonic() - started_at,
                            session_diagnostics(),
                            request_log_context(
                                request,
                                part_url,
                                route=safe_log_route(segment_proxy or forced_proxy),
                            ),
                        )
                        return None, True
                    except Exception as error:
                        logger.error(
                            "❌ Failed to fetch %s: %r elapsed=%.3fs %s [%s]",
                            label,
                            error,
                            time.monotonic() - started_at,
                            session_diagnostics(),
                            request_log_context(
                                request,
                                part_url,
                                route=safe_log_route(segment_proxy or forced_proxy),
                            ),
                        )
                        return None, False

                # Media requests generated by the MPD converter carry
                # skip_init=1 because HLS already fetched the EXT-X-MAP.
                # Reuse cached init bytes when available (or fetch once on a
                # multi-key cache miss) so MP4Decrypter has moov/tenc metadata
                # without re-downloading the init segment on every chunk.
                init_cache = getattr(self, "_dash_init_cache", None)
                if init_cache is None:
                    init_cache = {}
                    self._dash_init_cache = init_cache
                init_cache_key = f"{init_url}:{init_range or ''}" if init_url else ""
                cached_init = init_cache.get(init_cache_key) if init_cache_key else None
                need_init_fetch = bool(
                    init_url
                    and (not skip_init or (cached_init is None and "," in (key_id or "")))
                )
                init_fetch = (
                    asyncio.sleep(0, result=(cached_init, False))
                    if cached_init is not None
                    else (
                        fetch_part(
                            segment_session,
                            init_url,
                            10,
                            "init segment",
                            init_range,
                        )
                        if need_init_fetch
                        else asyncio.sleep(0, result=(b"", False))
                    )
                )

                # Keep media fetching concurrent with the first init fetch.
                init_result, segment_result = await asyncio.gather(
                    init_fetch,
                    fetch_part(segment_session, url, 15, "segment", media_range),
                )
                init_content, init_retryable = init_result
                segment_content, segment_retryable = segment_result

                # The WARP keepalive uses a separate session, so it can be
                # healthy while this long-lived pooled connector is stale.
                # Recreate that connector and remain on WARP for the retry.
                # Local WireGuard tunnels get the same retry after an inline
                # reconnect, since their SOCKS bind is restarted on failure.
                retryable = bool(init_retryable or segment_retryable)
                can_retry_warp = (
                    segment_proxy
                    and segment_proxy == _shared.WARP_PROXY_URL
                    and retryable
                )
                tunnel_slot = (
                    None
                    if (can_retry_warp or not segment_proxy or not retryable)
                    else wg_tunnels.slot_for_proxy_url(segment_proxy)
                )
                if tunnel_slot:
                    can_retry_warp = await self._recover_tunnel_proxy(segment_proxy)
                if can_retry_warp:
                    if not tunnel_slot:
                        await self._invalidate_proxy_session(
                            segment_proxy,
                            session_key=stream_session_key,
                        )
                        warp_healthy, warp_reason = await self._probe_warp(timeout_sec=3)
                        if not warp_healthy:
                            logger.warning(
                                "WARP health probe failed; retrying after socket recovery check [%s]",
                                request_log_context(
                                    request,
                                    url or init_url,
                                    route=safe_log_route(segment_proxy),
                                ),
                            )
                            await self._restart_warp_if_socket_unhealthy(warp_reason)

                    retry_session, retry_proxy = await self._get_proxy_session(
                        url or init_url,
                        bypass_warp=False,
                        forced_proxy=segment_proxy,
                        session_key=stream_session_key,
                    )
                    try:
                        retry_init, retry_segment = await asyncio.gather(
                            fetch_part(
                                retry_session,
                                init_url if init_retryable else None,
                                10,
                                "init segment (WARP retry)",
                                init_range,
                            ),
                            fetch_part(
                                retry_session,
                                url if segment_retryable else None,
                                15,
                                "segment (WARP retry)",
                                media_range,
                            ),
                        )
                    finally:
                        if (
                            retry_session
                            and not retry_session.closed
                        ):
                            await retry_session.close()
                    if init_retryable and retry_init[0] is not None:
                        init_content = retry_init[0]
                    if segment_retryable and retry_segment[0] is not None:
                        segment_content = retry_segment[0]
                    if (
                        (not init_retryable or init_content is not None)
                        and (not segment_retryable or segment_content is not None)
                    ):
                        logger.warning(
                            "Recovered ClearKey segment request through a fresh proxy session [%s]",
                            request_log_context(
                                request,
                                url or init_url,
                                route=safe_log_route(segment_proxy),
                            ),
                        )
                elif not segment_proxy and (init_retryable or segment_retryable):
                    # The upstream is reachable outside EasyProxy, but the
                    # shared aiohttp DIRECT connector can retain a dead
                    # keep-alive socket. Recreate only that connector and
                    # retry on DIRECT; never switch this request to WARP or
                    # another proxy.
                    await self._invalidate_direct_session(
                        url or init_url,
                        session_key=stream_session_key,
                    )
                    if _dec_proxy_token is None:
                        _dec_proxy_token = _shared.BYPASS_PROXIES_CONTEXT.set(True)
                    retry_session, retry_proxy = await self._get_proxy_session(
                        url or init_url,
                        bypass_warp=True,
                        forced_proxy=None,
                        session_key=stream_session_key,
                    )
                    try:
                        retry_init, retry_segment = await asyncio.gather(
                            fetch_part(
                                retry_session,
                                init_url if init_retryable else None,
                                10,
                                "init segment (DIRECT retry)",
                                init_range,
                            ),
                            fetch_part(
                                retry_session,
                                url if segment_retryable else None,
                                15,
                                "segment (DIRECT retry)",
                                media_range,
                            ),
                        )
                    finally:
                        if retry_session and not retry_session.closed:
                            await retry_session.close()

                    if init_retryable and retry_init[0] is not None:
                        init_content = retry_init[0]
                    if segment_retryable and retry_segment[0] is not None:
                        segment_content = retry_segment[0]
                    if (
                        (not init_retryable or init_content is not None)
                        and (not segment_retryable or segment_content is not None)
                    ):
                        logger.warning(
                            "Recovered ClearKey request through a fresh DIRECT session [%s]",
                            request_log_context(
                                request,
                                url or init_url,
                                route="DIRECT",
                            ),
                        )
            finally:
                if _dec_proxy_token is not None:
                    _shared.BYPASS_PROXIES_CONTEXT.reset(_dec_proxy_token)
                    _dec_proxy_token = None
                if segment_session and not segment_session.closed:
                    await segment_session.close()

            fetch_elapsed = time.monotonic() - fetch_started_at

            if init_content and init_cache_key and cached_init is None:
                if len(init_cache) > 50:
                    init_cache.clear()
                init_cache[init_cache_key] = init_content

            if init_content is None and init_url and not skip_init:
                logger.error(
                    "❌ Failed to fetch init segment [%s]",
                    request_log_context(
                        request,
                        init_url,
                        route=safe_log_route(segment_proxy or forced_proxy),
                    ),
                )
                return web.Response(status=502)
            if segment_content is None and url:
                logger.error(
                    "❌ Failed to fetch segment [%s]",
                    request_log_context(
                        request,
                        url,
                        route=safe_log_route(segment_proxy or forced_proxy),
                    ),
                )
                return web.Response(status=502)

            init_content = init_content or b""
            segment_content = segment_content or b""

            # Check if we should skip decryption (null key case)
            skip_decrypt = request.query.get("skip_decrypt") == "1"

            if skip_decrypt:
                # Null key: just return appropriate parts
                logger.info(
                    "🔓 Skip decrypt mode - serving without decryption [%s]",
                    request_log_context(
                        request,
                        url or init_url,
                        route=safe_log_route(segment_proxy or forced_proxy),
                    ),
                )
                if skip_init:
                    combined_content = segment_content
                elif is_init:
                    combined_content = init_content
                else:
                    combined_content = init_content + segment_content
            else:
                # Decripta con PyCryptodome
                # Decrypt in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                decrypt_started_at = time.monotonic()
                combined_content = await loop.run_in_executor(
                    _CLEARKEY_EXECUTOR,
                    decrypt_segment,
                    init_content,
                    segment_content,
                    key_id,
                    key,
                    skip_init,
                )
                decrypt_elapsed = time.monotonic() - decrypt_started_at

            # Serve raw decrypted fMP4.  DASH uses `.m4s` for both tracks, so
            # preserve the track kind explicitly for Safari's native demuxer.
            ts_content = combined_content
            media_type = request.query.get("media_type", "").lower()
            if media_type not in {"audio", "video"}:
                source_name = (url or init_url or "").lower()
                media_type = "audio" if "track_audio" in source_name else "video"
            content_type = "audio/mp4" if media_type == "audio" else "video/mp4"

            logger.info(
                "✅ [Decrypt] Completed: track=%s bytes=%d fetch=%.2fs decrypt=%.2fs total=%.2fs [%s]",
                media_type,
                len(ts_content),
                fetch_elapsed,
                decrypt_elapsed,
                time.monotonic() - decrypt_started,
                request_log_context(
                    request,
                    url or init_url,
                    route=safe_log_route(segment_proxy or forced_proxy),
                ),
            )

            # Invia Risposta
            return web.Response(
                body=ts_content,
                status=200,
                headers={
                    "Content-Type": content_type,
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        except Exception as e:
            if locals().get("_dec_proxy_token") is not None:
                _shared.BYPASS_PROXIES_CONTEXT.reset(_dec_proxy_token)
            logger.error(
                "❌ Decryption error: %s [%s]",
                e,
                request_log_context(
                    request,
                    url or init_url,
                    route=safe_log_route(locals().get("segment_proxy") or locals().get("forced_proxy")),
                ),
            )
            return web.Response(status=500, text=f"Decryption failed: {str(e)}")
