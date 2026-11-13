import asyncio
import hashlib
import logging
import math
import os
import re
import shutil
import statistics
import tempfile
import time
from array import array
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from .audio import AudioStore
from .http_client import create_client_session
from .offsets import RemoteOffsetStore
from .security import resolves_publicly, valid_public_url
from .routing import RoutingOptions, from_values


logger = logging.getLogger("easyproxy.dual.sync")


class _RangeDownloadFallback(RuntimeError):
    """The upstream does not support a usable ranged response."""


class _AdaptiveHostGate:
    """Per-host request gate that backs off on CDN throttling and recovers.

    Every CDN throttles at a different request rate, so the limit is learned
    per host: it halves on the first 429/503 and grows by one after a long run
    of successes. State is kept on the class and shared by all syncs.
    """

    def __init__(self, host: str, initial: int = 12, minimum: int = 1, maximum: int = 32):
        self.host = host
        self.limit = initial
        self.minimum = minimum
        self.maximum = maximum
        self._in_flight = 0
        self._successes = 0
        self._last_throttle = 0.0
        self._condition = asyncio.Condition()

    async def __aenter__(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self.limit)
            self._in_flight += 1

    async def __aexit__(self, exc_type, exc, tb):
        async with self._condition:
            self._in_flight = max(0, self._in_flight - 1)
            self._condition.notify()

    async def note_success(self) -> None:
        async with self._condition:
            self._successes += 1
            if self._successes >= 12 and self.limit < self.maximum:
                self.limit += 1
                self._successes = 0
                logger.debug("[DUAL] host gate %s limit=%d", self.host, self.limit)

    async def note_throttle(self) -> None:
        async with self._condition:
            now = time.monotonic()
            # One 503 usually floods the whole burst; a single step down per
            # incident is enough and repeated notes are ignored briefly.
            if now - self._last_throttle < 2.0:
                return
            self._last_throttle = now
            self._successes = 0
            if self.limit > self.minimum:
                self.limit = max(self.minimum, self.limit // 2)
                logger.info(
                    "[DUAL] host gate %s throttled, limit=%d",
                    self.host,
                    self.limit,
                )


class _MediaResponse:
    def __init__(self, status_code: int, headers: dict, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class SyncEngine:
    MAX_REDIRECTS = 5
    MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
    MAX_MEDIA_BYTES = 32 * 1024 * 1024
    MAX_SYNC_BYTES = 512 * 1024 * 1024
    SYNC_MIN_CORRELATION = 0.70
    LINEAR_MIN_CORRELATION = 0.75
    SYNC_MAX_DEVIATION = 0.25
    SYNC_MAX_LAG_SECONDS = 20
    # Last-resort search window: editions that differ by an intro/logo of
    # 10-60s keep a constant offset the 15s probes cannot reach.
    SYNC_WIDE_SEARCH_SECONDS = 60
    # Longer probes and a lower per-sample floor: ITA-vs-original envelopes
    # correlate weakly, but random peaks never agree within SYNC_MAX_DEVIATION
    # across a 120s window, so the >=60% agreement rule stays the safeguard.
    SYNC_WIDE_SAMPLE_SECONDS = 30
    SYNC_WIDE_MIN_CORRELATION = 0.50
    SYNC_WIDE_PROBES = 8
    LINEAR_MAX_DEVIATION = 0.10
    MAX_RATE_DELTA = 0.002
    MAX_END_DEVIATION = 1.5
    # A "constant" pair whose samples spread this much may really be drifting
    # (e.g. 40 ppm = 0.23s over 95 min): a single offset then leaves the two
    # ends of the film audibly out of sync. Prefer the line when it fits.
    DRIFT_MIN_DEVIATION = 0.08
    DRIFT_MAX_DEVIATION = 0.03
    DRIFT_MIN_SPAN = 0.3
    _host_gates: dict[str, "_AdaptiveHostGate"] = {}

    def __init__(self, audio: AudioStore, offsets: RemoteOffsetStore, proxy: str = ""):
        self.audio = audio
        self.offsets = offsets
        self.routing = RoutingOptions(forced_proxy=proxy.strip() or None)
        self.sample_seconds = 15
        self._downloaded_bytes = 0
        self._download_cache: dict[tuple[str, tuple[tuple[str, str], ...]], Path] = {}
        self._download_locks: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Lock] = {}
        self._download_cache_dir: Path | None = None
        self._http_sessions: dict[str, tuple[aiohttp.ClientSession, str | None]] = {}
        self._resolved_public_hosts: dict[tuple[str, int], bool] = {}

    def _account_bytes(self, amount: int) -> None:
        self._downloaded_bytes += amount
        if self._downloaded_bytes > self.MAX_SYNC_BYTES:
            raise RuntimeError("sync download budget exceeded")

    def _client_for(self, proxy: str):
        cached = self._http_sessions.get(proxy)
        if cached is not None and not cached[0].closed:
            return cached
        created = create_client_session(proxy, timeout=30)
        self._http_sessions[proxy] = created
        return created

    def _host_limit(self, url: str) -> _AdaptiveHostGate:
        """Shared per-host gate: probe bursts otherwise trigger CDN 503s."""
        host = (urlparse(url).hostname or "").lower()
        gate = self._host_gates.get(host)
        if gate is None:
            if len(self._host_gates) >= 256:
                self._host_gates.pop(next(iter(self._host_gates)), None)
            gate = _AdaptiveHostGate(host)
            self._host_gates[host] = gate
        return gate

    async def _resolves_publicly_cached(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or 443
        key = host, port
        if key in self._resolved_public_hosts:
            return self._resolved_public_hosts[key]
        try:
            result = await asyncio.wait_for(resolves_publicly(url), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("[DUAL] public DNS check timed out host=%s", host)
            result = False
        self._resolved_public_hosts[key] = result
        return result

    async def _get(
        self,
        url: str,
        headers: dict,
        destination: Path | None = None,
        max_bytes: int = MAX_PLAYLIST_BYTES,
    ):
        current_url = url
        for redirect_count in range(self.MAX_REDIRECTS + 1):
            if not valid_public_url(current_url) or not await self._resolves_publicly_cached(current_url):
                raise ValueError("media URL is not public HTTPS")
            proxy = self.routing.proxy_for(current_url)
            try:
                client, request_proxy = self._client_for(proxy)
                async with client.get(
                    current_url,
                    headers=headers,
                    proxy=request_proxy,
                    allow_redirects=False,
                ) as response:
                    status_code = response.status
                    response_headers = dict(response.headers)
                    if status_code in (301, 302, 303, 307, 308):
                        location = (response.headers.get("location") or "").strip()
                        if not location:
                            raise ValueError("media redirect has no location")
                        next_url = urljoin(current_url, location)
                        if not valid_public_url(next_url) or not await self._resolves_publicly_cached(next_url):
                            raise ValueError("media redirect is not public HTTPS")
                        if redirect_count >= self.MAX_REDIRECTS:
                            raise ValueError("too many media redirects")
                        current_url = next_url
                        continue

                    if status_code >= 400:
                        if status_code in (429, 503):
                            await self._host_limit(current_url).note_throttle()
                        raise RuntimeError(f"media fetch failed: HTTP {status_code}")
                    content_length = response_headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError("media response too large")
                        except ValueError as exc:
                            if str(exc) == "media response too large":
                                raise
                            raise ValueError("invalid media content length") from exc

                    if destination is not None:
                        temporary = destination.with_name(f".{destination.name}.part")
                        try:
                            total = 0
                            with temporary.open("wb") as output:
                                async for chunk in response.content.iter_chunked(64 * 1024):
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise ValueError("media response too large")
                                    output.write(chunk)
                            self._account_bytes(total)
                            temporary.replace(destination)
                            content = b""
                        finally:
                            temporary.unlink(missing_ok=True)
                    else:
                        chunks = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("media response too large")
                            chunks.append(chunk)
                        self._account_bytes(total)
                        content = b"".join(chunks)
                    await self._host_limit(current_url).note_success()
                    return _MediaResponse(status_code, response_headers, content)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("media fetch timed out") from exc
            except aiohttp.ClientError as exc:
                raise RuntimeError(f"media fetch failed: {exc}") from exc
        raise ValueError("too many media redirects")

    @staticmethod
    def _download_key(url: str, headers: dict) -> tuple[str, tuple[tuple[str, str], ...]]:
        return str(url), tuple(sorted((str(key), str(value)) for key, value in (headers or {}).items()))

    @staticmethod
    async def _gather_strict(*awaitables):
        """Cancel and drain siblings before propagating an error or cancellation."""
        tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _playlist(text: str, master_url: str):
        entries, pending, elapsed = [], None, 0.0
        map_url = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-MAP:"):
                import re
                match = re.search(r'URI="([^"]+)"', line)
                map_url = urljoin(master_url, match.group(1)) if match else None
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                entries.append({"url": urljoin(master_url, line), "duration": pending, "start": elapsed})
                elapsed += pending
                pending = None
        if not entries:
            raise ValueError("empty media playlist")
        return entries, map_url

    async def _video_entries(self, url: str, headers: dict):
        response = await self._get(url, headers)
        return self._playlist(response.text, url)

    async def _download(self, url: str, path: Path, headers: dict):
        key = self._download_key(url, headers)
        lock = self._download_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._download_cache.get(key)
            if cached and cached.is_file():
                shutil.copyfile(cached, path)
                return

            target = path
            if self._download_cache_dir is not None:
                digest = hashlib.sha256(repr(key).encode()).hexdigest()
                target = self._download_cache_dir / f"{digest}.bin"
            last_error: BaseException | None = None
            async with self._host_limit(url):
                for attempt in range(3):
                    try:
                        try:
                            content = await self._download_ranged(url, headers)
                        except _RangeDownloadFallback:
                            await self._get(
                                url,
                                headers,
                                destination=target,
                                max_bytes=self.MAX_MEDIA_BYTES,
                            )
                        else:
                            target.write_bytes(content)
                        last_error = None
                        break
                    except (RuntimeError, asyncio.TimeoutError) as exc:
                        last_error = exc
                        if attempt < 2:
                            await asyncio.sleep(0.25 * (attempt + 1))
            if last_error is not None:
                raise last_error
            if self._download_cache_dir is not None:
                self._download_cache[key] = target
                if target != path:
                    shutil.copyfile(target, path)

    async def _download_ranged(self, url: str, headers: dict) -> bytes:
        """Fetch a media object in small ranges when the CDN throttles full GETs."""
        chunk_size = 1024 * 1024
        base_headers = {
            key: value for key, value in (headers or {}).items()
            if str(key).lower() != "range"
        }
        body = bytearray()
        total = None
        start = 0
        max_ranges = (self.MAX_MEDIA_BYTES // chunk_size) + 1

        for _ in range(max_ranges):
            request_headers = {
                **base_headers,
                "Range": f"bytes={start}-{start + chunk_size - 1}",
            }
            response = None
            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        self._get(
                            url,
                            request_headers,
                            max_bytes=self.MAX_MEDIA_BYTES,
                        ),
                        timeout=10,
                    )
                    break
                except (RuntimeError, asyncio.TimeoutError):
                    if attempt == 0:
                        await asyncio.sleep(0.15)
            if response is None:
                raise _RangeDownloadFallback("ranged media fetch failed")

            if response.status_code == 200:
                if len(response.content) > self.MAX_MEDIA_BYTES:
                    raise ValueError("media response too large")
                return response.content
            if response.status_code != 206:
                raise _RangeDownloadFallback("upstream rejected range request")

            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(
                r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                content_range.strip(),
                flags=re.IGNORECASE,
            )
            if not match or int(match.group(1)) != start or not response.content:
                raise _RangeDownloadFallback("invalid ranged media response")
            range_end = int(match.group(2))
            declared_total = match.group(3)
            if declared_total != "*":
                total = int(declared_total)
                if total > self.MAX_MEDIA_BYTES:
                    raise ValueError("media response too large")
            expected = range_end - start + 1
            if len(response.content) > expected:
                raise _RangeDownloadFallback("ranged media response is inconsistent")
            body.extend(response.content)
            start += len(response.content)
            if total is not None and start >= total:
                return bytes(body)
            if len(response.content) < chunk_size:
                return bytes(body)

        raise ValueError("media response too large")

    def _sample_entries(self, entries, position: float, sample_seconds: float | None = None):
        sample_seconds = self.sample_seconds if sample_seconds is None else sample_seconds
        target = next((i for i, item in enumerate(entries)
                       if item["start"] <= position < item["start"] + item["duration"]),
                      len(entries) - 1)
        first = max(0, target - 1)
        local_seek = max(0.0, position - entries[first]["start"])
        selected, available = [], 0.0
        for item in entries[first:]:
            selected.append(item)
            available += item["duration"]
            if available >= local_seek + sample_seconds + 5.0:
                break
        return selected, local_seek, sum(item["duration"] for item in entries)

    async def _decode_video(
        self, url: str, headers: dict, position: float, directory: Path,
        sample_seconds: float | None = None,
    ):
        _, entries, map_url = (url, *await self._video_entries(url, headers))
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{int(max(x['duration'] for x in selected)) + 1}"]
        downloads = []
        if map_url:
            downloads.append(self._download(map_url, directory / "video-init.mp4", headers))
            lines.append('#EXT-X-MAP:URI="video-init.mp4"')
        for number, item in enumerate(selected):
            name = f"video-{number}.m4s"
            downloads.append(self._download(item["url"], directory / name, headers))
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "video.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _decode_reference_audio(
        self, url: str, headers: dict, position: float, directory: Path,
        sample_seconds: float | None = None,
    ):
        response = await self._get(url, headers)
        entries, map_url = self._playlist(response.text, url)
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in selected)) + 1}"]
        downloads = []
        key_line = next((line.strip() for line in response.text.splitlines()
                         if line.strip().startswith("#EXT-X-KEY:")), "")
        if key_line and "METHOD=NONE" not in key_line.upper():
            key_match = re.search(r'URI="([^"]+)"', key_line)
            if key_match:
                downloads.append(self._download(
                    urljoin(url, key_match.group(1)), directory / "reference.key", headers
                ))
                key_line = re.sub(r'URI="[^"]+"', 'URI="reference.key"', key_line, count=1)
            lines.append(key_line)
        if map_url:
            downloads.append(self._download(map_url, directory / "reference-init.mp4", headers))
            lines.append('#EXT-X-MAP:URI="reference-init.mp4"')
        for number, item in enumerate(selected):
            name = f"reference-{number}.m4s"
            downloads.append(self._download(item["url"], directory / name, headers))
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "reference.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _media_start_time(self, url: str, headers: dict) -> float:
        """Read the initial timestamp of Cinejoy's video-only fMP4 stream."""
        response = await self._get(url, headers)
        match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', response.text)
        first = next((line.strip() for line in response.text.splitlines()
                      if line.strip() and not line.startswith("#")), "")
        if not match or not first:
            return 0.0
        root = Path(tempfile.mkdtemp(prefix="cinejoy-start-"))
        try:
            init_path = root / "init.mp4"
            segment_path = root / "first.m4s"
            await self._gather_strict(
                self._download(urljoin(url, match.group(1)), init_path, headers),
                self._download(urljoin(url, first), segment_path, headers),
            )
            sample = root / "sample.mp4"
            with sample.open("wb") as output:
                for part in (init_path, segment_path):
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "stream=start_time",
                "-of", "default=nw=1:nk=1", str(sample),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            except BaseException:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                raise
            values = output.decode(errors="replace").strip().splitlines()
            return float(values[0]) if values else 0.0
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def _decode_audio(
        self,
        hid: str,
        position: float,
        directory: Path,
        sample_seconds: float | None = None,
    ):
        sample_seconds = self.sample_seconds if sample_seconds is None else sample_seconds
        metadata = self.audio.metadata(hid)
        index = next((i for i, start in enumerate(metadata["starts"]) if start <= position < start + metadata["durs"][i]), len(metadata["segs"]) - 1)
        first = max(0, index - 1)
        local_seek = max(0.0, position - metadata["starts"][first])
        selected = []
        available = 0.0
        for item in range(first, len(metadata["segs"])):
            selected.append(item)
            available += metadata["durs"][item]
            if available >= local_seek + sample_seconds + 5:
                break
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(metadata['durs'][item] for item in selected)) + 1}"]
        key_bytes = self.audio.key_bytes(hid)
        if key_bytes:
            iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="audio.key"{iv}')
            (directory / "audio.key").write_bytes(key_bytes)
        downloads = []
        for number, item in enumerate(selected):
            name = f"audio-{number}.ts"
            downloads.append(self._download(
                metadata["segs"][item], directory / name, metadata.get("headers") or {}
            ))
            lines += [f"#EXTINF:{metadata['durs'][item]:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "audio.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(metadata["durs"])

    @staticmethod
    async def _pcm(
        playlist: Path, seek: float, output: Path, audio_map: bool = True,
        sample_seconds: float = 5,
    ):
        command = ["ffmpeg", "-v", "error", "-threads", "1", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", str(playlist), "-ss", f"{max(0.0, seek):.3f}", "-t", f"{sample_seconds:g}"]
        if audio_map:
            command += ["-map", "0:a:0", "-vn"]
        command += ["-ac", "1", "-ar", "8000", "-f", "s16le", "-y", str(output)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        minimum_size = min(160000, int(sample_seconds * 8000 * 2 * .75))
        if process.returncode or not output.exists() or output.stat().st_size < minimum_size:
            err = error.decode(errors="replace")
            if "matches no streams" in err or "0:a:0" in err:
                raise RuntimeError("reference stream contains no audio track")
            raise RuntimeError((err or "sample decode failed")[:300])

    @staticmethod
    def _envelope(path: Path):
        values = array("h")
        values.frombytes(path.read_bytes())
        if not values:
            return []
        if os.sys.byteorder != "little":
            values.byteswap()
        step, window = 80, 160
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + abs(value))
        envelope = []
        for center in range(0, len(values), step):
            lo, hi = max(0, center - window // 2), min(len(values), center + window // 2)
            envelope.append((prefix[hi] - prefix[lo]) / max(1, hi - lo))
        mean = sum(envelope) / len(envelope)
        std = math.sqrt(sum((value - mean) ** 2 for value in envelope) / len(envelope)) or 1.0
        return [(value - mean) / std for value in envelope]

    @staticmethod
    def _lag(reference, candidate, max_seconds=5):
        """Find the best lag without doing a full quadratic scan.

        Envelopes are sampled at 100 Hz.  Scanning every 10 ms for every
        linear-drift candidate made the 20-second probes CPU-bound.  A 50 ms
        coarse scan followed by an 10 ms local scan keeps the old output
        precision while reducing the work by roughly an order of magnitude.
        """
        max_lag = max_seconds * 100

        def correlation(values_left, values_right, lag, minimum_size=500):
            if lag >= 0:
                size = min(len(values_left), len(values_right) - lag)
                left, right = values_left[:size], values_right[lag:lag + size]
            else:
                size = min(len(values_right), len(values_left) + lag)
                left, right = values_left[-lag:-lag + size], values_right[:size]
            if size < minimum_size:
                return None
            lm, rm = sum(left) / size, sum(right) / size
            lv = sum((value - lm) ** 2 for value in left)
            rv = sum((value - rm) ** 2 for value in right)
            denominator = math.sqrt(lv * rv)
            if denominator:
                return sum(
                    (left[i] - lm) * (right[i] - rm) for i in range(size)
                ) / denominator
            return None

        # Smooth blocks before the coarse pass.  This prevents a real peak
        # from being skipped when its offset falls between coarse samples.
        coarse_step = 5  # 50 ms; refined below to the original 10 ms.

        def block_average(values):
            return [
                sum(values[index:index + coarse_step]) / coarse_step
                for index in range(0, len(values) - coarse_step + 1, coarse_step)
            ]

        coarse_reference = block_average(reference)
        coarse_candidate = block_average(candidate)
        coarse_max_lag = max_lag // coarse_step
        # best keeps (score, lag in 10 ms envelope samples) so the refinement
        # pass can compare like with like; storing the coarse grid index left
        # the returned lag off by the coarse step whenever the coarse peak was
        # not improved (a 5x error at 50 ms steps).
        best = (-2.0, 0)
        for lag in range(-coarse_max_lag, coarse_max_lag + 1):
            score = correlation(
                coarse_reference, coarse_candidate, lag, minimum_size=100
            )
            if score is not None and score > best[0]:
                best = score, lag * coarse_step

        coarse_lag = best[1]
        for lag in range(
            max(-max_lag, coarse_lag - coarse_step),
            min(max_lag, coarse_lag + coarse_step) + 1,
        ):
            score = correlation(reference, candidate, lag)
            if score is not None and score > best[0]:
                best = score, lag
        return best[1] / 100.0, best[0]

    @classmethod
    def _constant_candidate(
        cls,
        measurements: list[dict],
        minimum: float = SYNC_MIN_CORRELATION,
    ) -> tuple[float, float, float] | None:
        """Pick the dominant constant offset, tolerating isolated false peaks.

        Repeated music cues can produce a second, almost equally strong
        correlation peak at another lag in a single window.  Requiring the
        majority of the high-confidence samples to agree keeps that outlier
        from rejecting an otherwise aligned pair.
        """
        valid = [
            item for item in measurements
            if item and float(item.get("correlation") or 0.0) >= minimum
        ]
        if len(valid) < 3:
            return None
        offsets = sorted(float(item["offset"]) for item in valid)
        median = statistics.median(offsets)
        inliers = [
            offset for offset in offsets
            if abs(offset - median) <= cls.SYNC_MAX_DEVIATION
        ]
        if len(inliers) < max(3, math.ceil(len(offsets) * 0.6)):
            return None
        measured = statistics.median(inliers)
        deviation = max(abs(offset - measured) for offset in inliers)
        confidence = min(
            float(item["correlation"]) for item in valid
            if abs(float(item["offset"]) - median) <= cls.SYNC_MAX_DEVIATION
        )
        return measured, deviation, confidence

    @classmethod
    def _measurement_result(
        cls,
        video_duration: float,
        audio_duration: float,
        measurements: list[dict],
        video_start_time: float = 0.0,
    ) -> dict:
        """Classify constant offset first, then safe linear drift.

        A small duration mismatch can be clock drift rather than a different
        edit.  In that case the offset changes with the timeline and a
        constant-only check incorrectly rejects an otherwise valid DUAL pair.
        The rate/end checks keep the fallback limited to tiny, reproducible
        drift corrections.
        """
        result = {
            "status": "incompatible",
            "video_duration": video_duration,
            "audio_duration": audio_duration,
            "measurements": measurements,
        }
        valid = [
            item for item in measurements
            if item and float(item.get("correlation") or 0.0) >= cls.SYNC_MIN_CORRELATION
        ]
        constant = cls._constant_candidate(measurements)
        if constant is not None:
            measured, deviation, confidence = constant
            result.update({
                "status": "ok",
                "offset": round(-measured + video_start_time, 3),
                "rate": 1.0,
                "confidence": confidence,
                "deviation": deviation,
                "sync_mode": "constant",
            })
            result.update(cls._drift_refinement(
                deviation, measurements, video_duration, audio_duration, video_start_time
            ) or {})
            return result
        if len(valid) < 3:
            return result

        median_offset = statistics.median(float(item["offset"]) for item in valid)
        deviation = max(abs(float(item["offset"]) - median_offset) for item in valid)
        result["deviation"] = deviation

        linear = cls._linear_fit(valid, video_duration, audio_duration)
        if linear is None:
            return result
        result.update({
            "candidate_rate": linear["rate"],
            "linear_deviation": linear["deviation"],
            "drift_per_hour": (linear["rate"] - 1.0) * 3600.0,
            "end_deviation": linear["end_deviation"],
        })
        if not linear["ok"]:
            return result

        result.update(cls._linear_result(linear, video_start_time))
        return result

    @classmethod
    def _linear_fit(
        cls,
        valid: list[dict],
        video_duration: float,
        audio_duration: float,
    ) -> dict | None:
        """Least-squares line through the offsets; ``ok`` when it is a safe drift."""
        linear_valid = [
            item for item in valid
            if float(item.get("correlation") or 0.0) >= cls.LINEAR_MIN_CORRELATION
        ]
        if len(linear_valid) < 3:
            return None
        positions = [float(item["position"]) for item in linear_valid]
        offsets = [float(item["offset"]) for item in linear_valid]
        mean_position = statistics.mean(positions)
        mean_offset = statistics.mean(offsets)
        denominator = sum(
            (position - mean_position) ** 2 for position in positions
        )
        span = max(positions) - min(positions)
        if denominator <= 0 or span < 60.0:
            return None

        slope = sum(
            (position - mean_position) * (offset - mean_offset)
            for position, offset in zip(positions, offsets)
        ) / denominator
        intercept = mean_offset - slope * mean_position
        rate = 1.0 + slope
        linear_deviation = max(
            abs(offset - (intercept + slope * position))
            for position, offset in zip(positions, offsets)
        )
        end_deviation = abs(
            intercept + rate * video_duration - audio_duration
        )
        return {
            "rate": rate,
            "intercept": intercept,
            "deviation": linear_deviation,
            "end_deviation": end_deviation,
            "span": span,
            "confidence": min(float(item["correlation"]) for item in linear_valid),
            "ok": not (
                abs(rate - 1.0) > cls.MAX_RATE_DELTA
                or linear_deviation > cls.LINEAR_MAX_DEVIATION
                or end_deviation > cls.MAX_END_DEVIATION
            ),
        }

    @staticmethod
    def _linear_result(linear: dict, video_start_time: float) -> dict:
        return {
            "status": "ok",
            "offset": round(-linear["intercept"] + video_start_time, 3),
            "rate": round(linear["rate"], 9),
            "sync_mode": "linear",
            "confidence": linear["confidence"],
            "deviation": linear["deviation"],
        }

    @classmethod
    def _drift_refinement(
        cls,
        deviation: float,
        measurements: list[dict],
        video_duration: float,
        audio_duration: float,
        video_start_time: float = 0.0,
    ) -> dict | None:
        """Replace an accepted but spread-out constant offset with its drift line.

        Only when the samples cover a good part of the film and sit on the
        line much better than on the constant; otherwise the constant stays.
        """
        if deviation < cls.DRIFT_MIN_DEVIATION:
            return None
        valid = [
            item for item in measurements
            if item and float(item.get("correlation") or 0.0) >= cls.SYNC_MIN_CORRELATION
        ]
        linear = cls._linear_fit(valid, video_duration, audio_duration)
        # No end-of-film check here: the constant offset is already accepted,
        # and editions often differ only by trimmed credits (different lengths).
        if (
            linear is None
            or abs(linear["rate"] - 1.0) > cls.MAX_RATE_DELTA
            or linear["deviation"] > cls.DRIFT_MAX_DEVIATION
            or linear["span"] < cls.DRIFT_MIN_SPAN * video_duration
        ):
            return None
        return cls._linear_result(linear, video_start_time)

    async def measure(self, payload: dict):
        # Each request gets isolated mutable state, so DUAL syncs can run
        # concurrently without sharing routing, temporary files, or counters.
        runner = SyncEngine(self.audio, self.offsets)
        runner.sample_seconds = self.sample_seconds
        return await runner._measure_isolated(payload)

    async def _measure_isolated(self, payload: dict):
        audio_hid = str(payload.get("audio_hid") or "")
        self.audio.pin(audio_hid)
        try:
            self.routing = from_values(payload.get("_routing"), payload)
            self._download_cache = {}
            cache_directory = tempfile.TemporaryDirectory(prefix="dual-sync-cache-")
            self._download_cache_dir = Path(cache_directory.name)
            try:
                return await self._measure(payload)
            finally:
                self._download_cache.clear()
                self._download_locks.clear()
                self._download_cache_dir = None
                self._resolved_public_hosts.clear()
                sessions = [item[0] for item in self._http_sessions.values()]
                self._http_sessions.clear()
                await asyncio.gather(*(session.close() for session in sessions))
                cache_directory.cleanup()
        finally:
            self.audio.unpin(audio_hid)

    async def _measure(self, payload: dict):
        self._downloaded_bytes = 0
        media_key = str(payload.get("media_key") or "")
        resolution = int(payload.get("resolution") or 0)
        video_url = str(payload.get("video_url") or "")
        video_headers = payload.get("video_headers") if isinstance(payload.get("video_headers"), dict) else {}
        reference_audio_url = str(
            payload.get("reference_audio_url") or payload.get("referenceAudio") or ""
        ).strip()
        validate_muxed_reference = bool(payload.get("validate_muxed_reference"))
        audio_hid = str(payload.get("audio_hid") or "")
        video_fp = str(payload.get("video_fingerprint") or "")
        metadata = self.audio.metadata(audio_hid)
        audio_fp = str(payload.get("audio_fingerprint") or metadata.get("source_fingerprint") or "")
        cache_key = self.offsets.key(media_key, resolution, video_fp, audio_fp)
        payload["cache_key"] = cache_key
        lookup = None
        if not payload.get("_offset_cache_checked"):
            lookup = await self.offsets.lookup({"cache_key": cache_key, "media_key": media_key, "resolution": resolution, "video_fingerprint": video_fp, "audio_fingerprint": audio_fp, "vpsAccess": payload.get("vpsAccess", "")})
        if lookup:
            cached_details = lookup.get("details") or lookup
            cached_status = str(cached_details.get("status") or lookup.get("status") or "")
            # Negative sync results are not authoritative: a low-quality sample,
            # a temporary CDN failure, or a provider edition change can produce
            # them. Re-measure instead of returning a permanent 409.
            # Prefer the reference_matches_video marker; entries written before
            # the offset API persisted it fall back to video_start_time.
            reference_validated = cached_details.get("reference_matches_video")
            if cached_status == "ok" and not (
                (validate_muxed_reference and reference_validated is False)
                or (
                    reference_audio_url
                    and reference_validated is not True
                    and "video_start_time" not in cached_details
                )
            ):
                result = {"status": "ok", "cached": True, **cached_details}
                result["cache_key"] = cache_key
                return result
            lookup = None
        video_entries, _ = await self._video_entries(video_url, video_headers)
        video_duration = sum(item["duration"] for item in video_entries)
        logger.info(
            "[DUAL] sync source loaded video_duration=%.3fs segments=%d reference=%s",
            video_duration,
            len(video_entries),
            bool(reference_audio_url),
        )
        video_start_time = 0.0
        # A muxed reference is the video's own audio: it already shares the
        # video timeline, so the start-time correction (meant for a video-only
        # stream with a separate audio rendition) would push the audio late.
        if reference_audio_url and not validate_muxed_reference:
            video_start_time = await self._media_start_time(video_url, video_headers)
        reference_duration = video_duration
        reference_entries = []
        if reference_audio_url:
            reference_entries, _ = await self._video_entries(reference_audio_url, video_headers)
            reference_duration = sum(item["duration"] for item in reference_entries)
            logger.info(
                "[DUAL] sync reference loaded duration=%.3fs segments=%d",
                reference_duration,
                len(reference_entries),
            )
            if abs(reference_duration - video_duration) > 1.0:
                logger.warning(
                    "[DUAL] reference/video playlist durations differ: video=%.3fs reference=%.3fs; continuing with correlation",
                    video_duration,
                    reference_duration,
                )
        audio_duration = sum(metadata["durs"])
        reference_matches_video = None
        measurements = []

        async def collect_one(position: float, index: int, batch: str, wide: float = 0.0):
            sample_seconds = self.SYNC_WIDE_SAMPLE_SECONDS if wide else self.sample_seconds
            # wide > 0: decode a longer audio window around the probe so an
            # offset up to +-wide seconds still overlaps the reference sample.
            audio_start = max(0.0, position - wide)
            shift = position - audio_start
            video_dir = root / f"{batch}-video-{index}"
            audio_dir = root / f"{batch}-audio-{index}"
            video_dir.mkdir(), audio_dir.mkdir()
            if reference_audio_url:
                reference_task = self._decode_reference_audio(
                    reference_audio_url, video_headers, position, video_dir, sample_seconds
                )
            else:
                reference_task = self._decode_video(
                    video_url, video_headers, position, video_dir, sample_seconds
                )
            audio_seconds = sample_seconds + shift + wide
            reference_result, audio_result = await self._gather_strict(
                reference_task,
                self._decode_audio(audio_hid, audio_start, audio_dir, sample_seconds=audio_seconds),
            )
            reference_playlist, reference_seek, _ = reference_result
            audio_playlist, audio_seek, _ = audio_result
            video_pcm = root / f"{batch}-video-{index}.pcm"
            audio_pcm = root / f"{batch}-audio-{index}.pcm"
            samples = await asyncio.gather(
                self._pcm(
                    reference_playlist,
                    reference_seek,
                    video_pcm,
                    sample_seconds=sample_seconds,
                ),
                self._pcm(
                    audio_playlist,
                    audio_seek,
                    audio_pcm,
                    sample_seconds=audio_seconds,
                ),
                return_exceptions=True,
            )
            for sample in samples:
                if isinstance(sample, asyncio.CancelledError):
                    raise sample
            failure = next((sample for sample in samples if isinstance(sample, BaseException)), None)
            if failure is not None:
                raise RuntimeError(f"{type(failure).__name__}: {str(failure)[:260]}")
            video_envelope, audio_envelope = await asyncio.gather(
                asyncio.to_thread(self._envelope, video_pcm),
                asyncio.to_thread(self._envelope, audio_pcm),
            )
            lag, correlation = await asyncio.to_thread(
                self._lag,
                video_envelope,
                audio_envelope,
                int(math.ceil(shift + wide)) if wide else self.SYNC_MAX_LAG_SECONDS,
            )
            offset = round(lag - shift, 3)
            return {"position": position, "lag": offset, "offset": offset, "correlation": correlation}

        async def collect_batch(
            positions,
            batch: str = "main",
            accept_early: bool = False,
            wide: float = 0.0,
            sink: list | None = None,
            accept_minimum: float = 0.65,
        ):
            """Run every probe in parallel and stop as soon as they agree."""
            measurements_out = measurements if sink is None else sink
            tasks = [
                asyncio.ensure_future(collect_one(position, index, batch, wide))
                for index, position in enumerate(positions)
            ]
            failures: list[BaseException] = []
            try:
                for completed in asyncio.as_completed(tasks):
                    try:
                        measurements_out.append(await completed)
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        failures.append(exc)
                        logger.warning(
                            "[DUAL] sync sample failed: %s: %s",
                            type(exc).__name__,
                            str(exc)[:180],
                        )
                        continue
                    if accept_early:
                        constant = self._constant_candidate(measurements_out, minimum=accept_minimum)
                        if constant is not None:
                            return constant
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if failures and len(measurements_out) < 3:
                raise failures[0]
            return None

        linear_measurements = []

        async def collect_linear(positions):
            """Repeat Toast's drift probe with duration-delta audio guesses."""
            linear_sample_seconds = max(20.0, float(self.sample_seconds))
            duration_delta = audio_duration - video_duration
            guesses = [0.0]
            if abs(duration_delta) >= 3.0:
                guesses.extend((duration_delta, -duration_delta))
            guesses = sorted(set(round(guess, 3) for guess in guesses))

            async def collect_one_linear(position: float, index: int):
                video_dir = root / f"linear-video-{index}"
                video_dir.mkdir()
                if reference_audio_url:
                    reference_task = self._decode_reference_audio(
                        reference_audio_url,
                        video_headers,
                        position,
                        video_dir,
                        linear_sample_seconds,
                    )
                else:
                    reference_task = self._decode_video(
                        video_url,
                        video_headers,
                        position,
                        video_dir,
                        linear_sample_seconds,
                    )

                valid_guesses = [
                    guess for guess in guesses
                    if 0 <= position + guess <= max(0.0, audio_duration - 20.0)
                ] or [0.0]
                audio_tasks = []
                for guess_index, guess in enumerate(valid_guesses):
                    audio_dir = root / f"linear-audio-{index}-{guess_index}"
                    audio_dir.mkdir()
                    audio_tasks.append(self._decode_audio(
                        audio_hid,
                        position + guess,
                        audio_dir,
                        sample_seconds=linear_sample_seconds,
                    ))

                decoded = await asyncio.gather(
                    reference_task,
                    *audio_tasks,
                    return_exceptions=True,
                )
                for item in decoded:
                    if isinstance(item, asyncio.CancelledError):
                        raise item
                logger.info(
                    "[DUAL] linear playlists ready position=%.1f",
                    position,
                )
                reference_result = decoded[0]
                if isinstance(reference_result, BaseException):
                    raise reference_result
                audio_results = [
                    (guess, item)
                    for guess, item in zip(valid_guesses, decoded[1:])
                    if not isinstance(item, BaseException)
                ]
                if not audio_results:
                    raise RuntimeError("all drift audio samples failed")

                reference_playlist, reference_seek, _ = reference_result
                reference_pcm = root / f"linear-reference-{index}.pcm"
                decode_tasks = [self._pcm(
                    reference_playlist,
                    reference_seek,
                    reference_pcm,
                    sample_seconds=linear_sample_seconds,
                )]
                audio_pcm_paths = []
                for guess_index, (guess, audio_result) in enumerate(audio_results):
                    audio_playlist, audio_seek, _ = audio_result
                    audio_pcm = root / f"linear-audio-{index}-{guess_index}.pcm"
                    audio_pcm_paths.append((guess, audio_pcm))
                    decode_tasks.append(self._pcm(
                        audio_playlist,
                        audio_seek,
                        audio_pcm,
                        sample_seconds=linear_sample_seconds,
                    ))
                decoded_samples = await asyncio.gather(
                    *decode_tasks,
                    return_exceptions=True,
                )
                for item in decoded_samples:
                    if isinstance(item, asyncio.CancelledError):
                        raise item
                logger.info(
                    "[DUAL] linear PCM ready position=%.1f",
                    position,
                )
                if isinstance(decoded_samples[0], BaseException):
                    raise decoded_samples[0]

                reference_envelope = await asyncio.to_thread(
                    self._envelope, reference_pcm
                )
                best = None
                for (guess, audio_pcm), sample in zip(
                    audio_pcm_paths, decoded_samples[1:]
                ):
                    if isinstance(sample, BaseException):
                        continue
                    audio_envelope = await asyncio.to_thread(
                        self._envelope, audio_pcm
                    )
                    lag, correlation = await asyncio.to_thread(
                        self._lag,
                        reference_envelope,
                        audio_envelope,
                        self.SYNC_MAX_LAG_SECONDS,
                    )
                    candidate = {
                        "position": position,
                        "guess": guess,
                        "lag": lag,
                        "offset": guess + lag,
                        "correlation": correlation,
                    }
                    if best is None or correlation > best["correlation"]:
                        best = candidate
                if best is None:
                    raise RuntimeError("drift samples produced no correlation")
                return best

            results = await asyncio.gather(*(
                collect_one_linear(position, index)
                for index, position in enumerate(positions)
            ), return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.warning(
                        "[DUAL] linear sync sample failed: %s: %s",
                        type(result).__name__,
                        str(result)[:180],
                    )
                    continue
                linear_measurements.append(result)

        with tempfile.TemporaryDirectory(prefix="dual-sync-") as directory:
            root = Path(directory)
            common = min(video_duration, reference_duration, audio_duration)
            if common < 90:
                raise ValueError("media too short")
            all_positions = sorted({
                min(60.0, common * .1),
                common * .2,
                common * .4,
                common * .6,
                common * .8,
                max(30.0, common - 90.0),
            })
            # Toast syncs against the lowest muxed rendition directly. Do not
            # perform a second high-vs-low media request here: that probe is
            # both unnecessary and vulnerable to transient CDN 429 responses.
            # The selected rendition is still used for playback; the low one
            # is only the cheaper audio reference for correlation.
            if validate_muxed_reference:
                reference_matches_video = True
            logger.info(
                "[DUAL] sync sampling positions=%s",
                ",".join(f"{position:.1f}" for position in all_positions),
            )
            constant = await collect_batch(all_positions, "main", accept_early=True)
            logger.info("[DUAL] sync samples complete count=%d", len(measurements))
            if constant is not None:
                measured, deviation, confidence = constant
                result = {
                    "status": "ok",
                    "offset": round(-measured + video_start_time, 3),
                    "rate": 1.0,
                    "confidence": confidence,
                    "deviation": deviation,
                    "sync_mode": "fast",
                    "video_duration": video_duration,
                    "audio_duration": audio_duration,
                    "measurements": measurements,
                }
                result.update(self._drift_refinement(
                    deviation, measurements, video_duration, audio_duration, video_start_time
                ) or {})
                if reference_audio_url:
                    result["video_start_time"] = round(video_start_time, 3)
                if validate_muxed_reference:
                    result["reference_matches_video"] = bool(reference_matches_video)
                result["cache_key"] = cache_key
                return result

            result = self._measurement_result(
                video_duration,
                audio_duration,
                measurements,
                video_start_time,
            )
            if result["status"] != "ok":
                # Linear anchor starts at 300s, not 60s: intros/logos often
                # differ by <1s between editions, poisoning the edge sample
                # while the rest of the film matches with a constant offset.
                linear_positions = sorted(set(round(position, 3) for position in (
                    min(300.0, common * .1),
                    common * .5,
                    max(30.0, common - 90.0),
                )))
                logger.info(
                    "[DUAL] sync sampling linear positions=%s",
                    ",".join(f"{position:.1f}" for position in linear_positions),
                )
                await collect_linear(linear_positions)
                logger.info(
                    "[DUAL] sync linear samples complete count=%d",
                    len(linear_measurements),
                )

            if result["status"] != "ok" and linear_measurements:
                linear_result = self._measurement_result(
                    video_duration,
                    audio_duration,
                    linear_measurements,
                    video_start_time,
                )
                if linear_result["status"] == "ok":
                    result = linear_result
                else:
                    result["linear_measurements"] = linear_measurements

            if result["status"] != "ok":
                wide = float(self.SYNC_WIDE_SEARCH_SECONDS)
                probes = self.SYNC_WIDE_PROBES
                wide_positions = sorted({common * k / (probes + 1) for k in range(1, probes + 1)})
                logger.info(
                    "[DUAL] sync sampling wide positions=%s window=+-%.0fs",
                    ",".join(f"{position:.1f}" for position in wide_positions),
                    wide,
                )
                wide_measurements: list[dict] = []
                try:
                    await collect_batch(
                        wide_positions,
                        "wide",
                        accept_early=True,
                        wide=wide,
                        sink=wide_measurements,
                        accept_minimum=self.SYNC_WIDE_MIN_CORRELATION,
                    )
                except Exception as exc:
                    # Keep the earlier rejection; a failed last resort is not a new verdict.
                    logger.warning("[DUAL] wide sync failed: %s: %s", type(exc).__name__, str(exc)[:180])
                logger.info("[DUAL] sync wide samples complete count=%d", len(wide_measurements))
                wide_constant = self._constant_candidate(
                    wide_measurements, minimum=self.SYNC_WIDE_MIN_CORRELATION
                )
                if wide_constant is not None:
                    measured, deviation, confidence = wide_constant
                    result = {
                        "status": "ok",
                        "offset": round(-measured + video_start_time, 3),
                        "rate": 1.0,
                        "confidence": confidence,
                        "deviation": deviation,
                        "sync_mode": "wide",
                        "video_duration": video_duration,
                        "audio_duration": audio_duration,
                        "measurements": wide_measurements,
                    }
                elif wide_measurements:
                    result["wide_measurements"] = wide_measurements
        if result["status"] != "ok":
            def _summarize(items):
                try:
                    return ",".join(
                        f"{float(item.get('position', 0.0)):.0f}:{float(item.get('correlation', 0.0)):.2f}@{float(item.get('offset', 0.0)):.2f}"
                        for item in items or []
                    ) or "-"
                except (TypeError, ValueError):
                    return "-"
            logger.warning(
                "[DUAL] sync rejected video=%.3fs audio=%.3fs deviation=%.3fs "
                "linear_samples=%d rate=%s linear_deviation=%s end_deviation=%s "
                "samples=%s linear=%s",
                video_duration,
                audio_duration,
                float(result.get("deviation") or 0.0),
                len(linear_measurements),
                result.get("candidate_rate", "-"),
                result.get("linear_deviation", "-"),
                result.get("end_deviation", "-"),
                _summarize(measurements),
                _summarize(linear_measurements),
            )
        if validate_muxed_reference:
            result["reference_matches_video"] = bool(reference_matches_video)
        if reference_audio_url and result.get("status") == "ok":
            result["video_start_time"] = round(video_start_time, 3)
        result["cache_key"] = cache_key
        return result
