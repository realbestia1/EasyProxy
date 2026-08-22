import asyncio
import math
import os
import re
import shutil
import statistics
import tempfile
from array import array
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from .audio import AudioStore
from .http_client import client_session
from .offsets import OffsetStore
from .security import resolves_publicly, valid_public_url


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
    MAX_SYNC_BYTES = 256 * 1024 * 1024

    def __init__(self, audio: AudioStore, offsets: OffsetStore, proxy: str = ""):
        self.audio = audio
        self.offsets = offsets
        self.proxy = proxy
        self.sample_seconds = 20
        self._sync_semaphore = asyncio.Semaphore(1)
        self._downloaded_bytes = 0

    def _account_bytes(self, amount: int) -> None:
        self._downloaded_bytes += amount
        if self._downloaded_bytes > self.MAX_SYNC_BYTES:
            raise RuntimeError("sync download budget exceeded")

    async def _get(
        self,
        url: str,
        headers: dict,
        destination: Path | None = None,
        max_bytes: int = MAX_PLAYLIST_BYTES,
    ):
        current_url = url
        try:
            async with client_session(self.proxy, timeout=30) as (client, request_proxy):
                for redirect_count in range(self.MAX_REDIRECTS + 1):
                    if not valid_public_url(current_url) or not await resolves_publicly(current_url):
                        raise ValueError("media URL is not public HTTPS")
                    async with client.get(
                        current_url,
                        headers=headers,
                        proxy=request_proxy,
                        allow_redirects=False,
                    ) as response:
                        status_code = response.status
                        response_headers = dict(response.headers)
                        if status_code in (301, 302, 303, 307, 308):
                            location = response_headers.get("location", "").strip()
                            if not location:
                                raise ValueError("media redirect has no location")
                            next_url = urljoin(current_url, location)
                            if not valid_public_url(next_url) or not await resolves_publicly(next_url):
                                raise ValueError("media redirect is not public HTTPS")
                            if redirect_count >= self.MAX_REDIRECTS:
                                raise ValueError("too many media redirects")
                            current_url = next_url
                            continue

                        if status_code >= 400:
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
                        return _MediaResponse(status_code, response_headers, content)
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"media fetch failed: {exc}") from exc
        raise ValueError("too many media redirects")

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
        await self._get(url, headers, destination=path, max_bytes=self.MAX_MEDIA_BYTES)

    @staticmethod
    def _sample_entries(entries, position: float):
        target = next((i for i, item in enumerate(entries)
                       if item["start"] <= position < item["start"] + item["duration"]),
                      len(entries) - 1)
        first = max(0, target - 1)
        local_seek = max(0.0, position - entries[first]["start"])
        selected, available = [], 0.0
        for item in entries[first:]:
            selected.append(item)
            available += item["duration"]
            if available >= local_seek + self.sample_seconds + 5.0:
                break
        return selected, local_seek, sum(item["duration"] for item in entries)

    async def _decode_video(self, url: str, headers: dict, position: float, directory: Path):
        _, entries, map_url = (url, *await self._video_entries(url, headers))
        selected, local_seek, duration = self._sample_entries(entries, position)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{int(max(x['duration'] for x in selected)) + 1}"]
        if map_url:
            await self._download(map_url, directory / "video-init.mp4", headers)
            lines.append('#EXT-X-MAP:URI="video-init.mp4"')
        for number, item in enumerate(selected):
            name = f"video-{number}.m4s"
            await self._download(item["url"], directory / name, headers)
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "video.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _decode_reference_audio(self, url: str, headers: dict, position: float, directory: Path):
        response = await self._get(url, headers)
        entries, map_url = self._playlist(response.text, url)
        selected, local_seek, duration = self._sample_entries(entries, position)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in selected)) + 1}"]
        key_line = next((line.strip() for line in response.text.splitlines()
                         if line.strip().startswith("#EXT-X-KEY:")), "")
        if key_line and "METHOD=NONE" not in key_line.upper():
            key_match = re.search(r'URI="([^"]+)"', key_line)
            if key_match:
                key_response = await self._get(urljoin(url, key_match.group(1)), headers)
                (directory / "reference.key").write_bytes(key_response.content)
                key_line = re.sub(r'URI="[^"]+"', 'URI="reference.key"', key_line, count=1)
            lines.append(key_line)
        if map_url:
            await self._download(map_url, directory / "reference-init.mp4", headers)
            lines.append('#EXT-X-MAP:URI="reference-init.mp4"')
        for number, item in enumerate(selected):
            name = f"reference-{number}.m4s"
            await self._download(item["url"], directory / name, headers)
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
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
            await asyncio.gather(
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
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            values = output.decode(errors="replace").strip().splitlines()
            return float(values[0]) if values else 0.0
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def _decode_audio(self, hid: str, position: float, directory: Path):
        metadata = self.audio.metadata(hid)
        index = next((i for i, start in enumerate(metadata["starts"]) if start <= position < start + metadata["durs"][i]), len(metadata["segs"]) - 1)
        first = max(0, index - 1)
        local_seek = max(0.0, position - metadata["starts"][first])
        selected = []
        available = 0.0
        for item in range(first, len(metadata["segs"])):
            selected.append(item)
            available += metadata["durs"][item]
            if available >= local_seek + self.sample_seconds + 5:
                break
        iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(metadata['durs'][item] for item in selected)) + 1}",
                 f'#EXT-X-KEY:METHOD=AES-128,URI="audio.key"{iv}']
        (directory / "audio.key").write_bytes(self.audio.key_bytes(hid))
        for number, item in enumerate(selected):
            name = f"audio-{number}.ts"
            await self._download(metadata["segs"][item], directory / name, metadata.get("headers") or {})
            lines += [f"#EXTINF:{metadata['durs'][item]:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "audio.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(metadata["durs"])

    @staticmethod
    async def _pcm(playlist: Path, seek: float, output: Path, audio_map: bool = True):
        command = ["ffmpeg", "-v", "error", "-threads", "1", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", str(playlist), "-ss", f"{max(0.0, seek):.3f}", "-t", "20"]
        if audio_map:
            command += ["-map", "0:a:0", "-vn"]
        command += ["-ac", "1", "-ar", "8000", "-f", "s16le", "-y", str(output)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode or not output.exists() or output.stat().st_size < 160000:
            raise RuntimeError((error.decode(errors="replace") or "sample decode failed")[:300])

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
        best = (-2.0, 0)
        for lag in range(-max_seconds * 100, max_seconds * 100 + 1):
            if lag >= 0:
                size = min(len(reference), len(candidate) - lag)
                left, right = reference[:size], candidate[lag:lag + size]
            else:
                size = min(len(candidate), len(reference) + lag)
                left, right = reference[-lag:-lag + size], candidate[:size]
            if size < 500:
                continue
            lm, rm = sum(left) / size, sum(right) / size
            lv = sum((value - lm) ** 2 for value in left)
            rv = sum((value - rm) ** 2 for value in right)
            denominator = math.sqrt(lv * rv)
            if denominator:
                correlation = sum((left[i] - lm) * (right[i] - rm) for i in range(size)) / denominator
                if correlation > best[0]:
                    best = correlation, lag
        return best[1] / 100.0, best[0]

    async def measure(self, payload: dict):
        # Offset detection invokes several downloads and decoder processes.
        # Serialize it so concurrent Toastflix sessions cannot multiply the
        # CPU/RAM/network cost on a personal EasyProxy instance.
        async with self._sync_semaphore:
            return await self._measure(payload)

    async def _measure(self, payload: dict):
        self._downloaded_bytes = 0
        media_key = str(payload.get("media_key") or "")
        resolution = int(payload.get("resolution") or 0)
        video_url = str(payload.get("video_url") or "")
        video_headers = payload.get("video_headers") if isinstance(payload.get("video_headers"), dict) else {}
        reference_audio_url = str(
            payload.get("reference_audio_url") or payload.get("referenceAudio") or ""
        ).strip()
        audio_hid = str(payload.get("audio_hid") or "")
        video_fp = str(payload.get("video_fingerprint") or "")
        metadata = self.audio.metadata(audio_hid)
        audio_fp = str(payload.get("audio_fingerprint") or metadata.get("source_fingerprint") or "")
        cache_key = self.offsets.key(media_key, resolution, video_fp, audio_fp)
        payload["cache_key"] = cache_key
        lookup = await self.offsets.lookup({"cache_key": cache_key, "media_key": media_key, "resolution": resolution, "video_fingerprint": video_fp, "audio_fingerprint": audio_fp, "vpsAccess": payload.get("vpsAccess", "")})
        if lookup:
            result = {"status": "ok", "cached": True, **(lookup.get("details") or lookup)}
            if reference_audio_url and not result.get("video_start_time"):
                lookup = None
            else:
                result["cache_key"] = cache_key
                return result
        video_entries, _ = await self._video_entries(video_url, video_headers)
        video_duration = sum(item["duration"] for item in video_entries)
        video_start_time = 0.0
        if reference_audio_url:
            video_start_time = await self._media_start_time(video_url, video_headers)
        reference_duration = video_duration
        if reference_audio_url:
            reference_entries, _ = await self._video_entries(reference_audio_url, video_headers)
            reference_duration = sum(item["duration"] for item in reference_entries)
            if abs(reference_duration - video_duration) > 1.0:
                raise ValueError("reference audio timeline mismatch")
        audio_duration = sum(metadata["durs"])
        common = min(video_duration, reference_duration, audio_duration)
        if common < 90:
            raise ValueError("media too short")
        positions = sorted({min(60.0, common * .1), common * .5, max(30.0, common - 90.0)})
        measurements = []
        with tempfile.TemporaryDirectory(prefix="sidecar-sync-") as directory:
            root = Path(directory)
            for index, position in enumerate(positions):
                video_dir, audio_dir = root / f"video-{index}", root / f"audio-{index}"
                video_dir.mkdir(), audio_dir.mkdir()
                if reference_audio_url:
                    reference_playlist, reference_seek, _ = await self._decode_reference_audio(
                        reference_audio_url, video_headers, position, video_dir
                    )
                else:
                    reference_playlist, reference_seek, _ = await self._decode_video(
                        video_url, video_headers, position, video_dir
                    )
                audio_playlist, audio_seek, _ = await self._decode_audio(audio_hid, position, audio_dir)
                video_pcm, audio_pcm = root / f"video-{index}.pcm", root / f"audio-{index}.pcm"
                samples = await asyncio.gather(
                    self._pcm(reference_playlist, reference_seek, video_pcm),
                    self._pcm(audio_playlist, audio_seek, audio_pcm),
                    return_exceptions=True,
                )
                failure = next((sample for sample in samples if isinstance(sample, BaseException)), None)
                if failure is not None:
                    raise RuntimeError(f"{type(failure).__name__}: {str(failure)[:260]}")
                lag, correlation = self._lag(self._envelope(video_pcm), self._envelope(audio_pcm))
                measurements.append({"position": position, "lag": lag, "offset": lag, "correlation": correlation})
        valid = [item for item in measurements if item["correlation"] >= .75]
        if len(valid) < 2:
            result = {"status": "incompatible", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
        else:
            measured = statistics.median(item["offset"] for item in valid)
            deviation = max(abs(item["offset"] - measured) for item in valid)
            result = {"status": "ok" if deviation <= .25 else "incompatible", "offset": round(-measured + video_start_time, 3), "rate": 1.0, "confidence": min(item["correlation"] for item in valid), "deviation": deviation, "sync_mode": "constant", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
            if reference_audio_url:
                result["video_start_time"] = round(video_start_time, 3)
        result["cache_key"] = cache_key
        return result
