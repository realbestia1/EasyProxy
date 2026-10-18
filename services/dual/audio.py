import asyncio
import base64
import hashlib
import re
import struct
import tempfile
import time
import weakref
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .security import valid_public_url
from .http_client import download_asset
from .routing import RoutingOptions


class AudioStore:
    """Hold audio metadata in memory and never persist audio fragments.

    Source audio and generated fMP4 fragments are processed per request and
    removed with their temporary working directory. Converted fragments are
    cached in memory only while their track is active, so the player can start
    without waiting for a fresh ffmpeg pass on every segment.
    """

    TRACK_TTL_SECONDS = 21600
    TRACK_IDLE_SECONDS = 3600
    MAX_TRACKS = 128
    MAX_REDIRECTS = 5
    MAX_SEGMENT_BYTES = 20 * 1024 * 1024
    MAX_CACHED_FRAGMENTS = 6

    def __init__(self, root: str, proxy: str = "", max_bytes: int | None = None):
        self.default_routing = RoutingOptions(forced_proxy=proxy.strip() or None)
        # A weak map keeps per-track locks only while a request still holds a
        # reference to them. It cannot grow permanently with every new HID.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._tracks: dict[str, dict] = {}
        # Sync requests can wait in the global queue for longer than the
        # normal player-idle timeout. Keep those tracks alive temporarily.
        self._pinned: dict[str, int] = {}
        self._active: dict[str, int] = {}
        self._prefetch_tasks: set[asyncio.Task] = set()

    def _lock(self, hid: str):
        return self._locks.setdefault(hid, asyncio.Lock())

    def _track(self, hid: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{16}", hid):
            raise ValueError("invalid audio id")
        track = self._tracks.get(hid)
        if track is None:
            raise FileNotFoundError("audio track not found")
        track["last_used"] = time.monotonic()
        return track

    def _remember(self, hid: str, metadata: dict, key: bytes) -> None:
        now = time.monotonic()
        self._tracks[hid] = {
            "metadata": metadata,
            "key": key,
            "last_used": now,
        }
        expired = [
            key_id
            for key_id, track in self._tracks.items()
            if key_id not in self._pinned
            and now - track["last_used"] > self.TRACK_TTL_SECONDS
        ]
        for key_id in expired:
            self._tracks.pop(key_id, None)
        while len(self._tracks) > self.MAX_TRACKS:
            candidates = [key_id for key_id in self._tracks if key_id not in self._pinned]
            if not candidates:
                break
            oldest = min(candidates, key=lambda key_id: self._tracks[key_id]["last_used"])
            self._tracks.pop(oldest, None)

    @staticmethod
    def _parse_playlist(playlist: str, base_url: str = ""):
        segments, durations = [], []
        pending = None
        key_line = ""
        for raw in playlist.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-KEY:"):
                key_line = line
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                segments.append(urljoin(base_url, line))
                durations.append(pending)
                pending = None
        return segments, durations, key_line

    @staticmethod
    def _validate_segments(segments):
        if not segments or len(segments) > 10000:
            raise ValueError("invalid audio segment count")
        if not all(valid_public_url(url) for url in segments):
            raise ValueError("audio segment URL is not public HTTPS")

    @staticmethod
    def source_fingerprint(segments):
        """Return the stable fingerprint used by the shared offset cache."""
        stable = [urlparse(url).path for url in segments[:3]]
        if not stable:
            return ""
        return hashlib.sha1(
            ("|".join(stable) + str(len(segments))).encode()
        ).hexdigest()[:20]

    async def register(self, playlist: str, key_b64: str, media_key: str,
                       language: str, base_url: str = "", headers: dict | None = None,
                       routing: RoutingOptions | None = None):
        if len(playlist) > 2 * 1024 * 1024 or "#EXTINF" not in playlist:
            raise ValueError("invalid audio playlist")
        language = str(language or "").lower().strip()
        if language not in {
            "ita", "eng", "spa", "fra", "deu", "hin", "rus",
            "it", "en", "es", "fr", "de", "hi", "ru",
            "italian", "english", "spanish", "french", "german", "hindi", "russian",
        }:
            raise ValueError("unsupported audio language")
        segments, durations, key_line = self._parse_playlist(playlist, base_url)
        self._validate_segments(segments)
        if len(durations) < len(segments):
            raise ValueError("audio durations do not match segments")
        if key_b64:
            key = base64.b64decode(key_b64, validate=True)
            if len(key) != 16:
                raise ValueError("AES key must be 16 bytes")
        else:
            # Valid HLS audio can be unencrypted.
            key = b""
        safe_headers = {
            str(name): str(value).strip()
            for name, value in (headers or {}).items()
            if re.fullmatch(r"[A-Za-z0-9-]+", str(name))
            and str(value).strip()
            and len(str(value).strip()) <= 1024
            and "\r" not in str(value)
            and "\n" not in str(value)
        }
        stable = [urlparse(url).path for url in segments[:3]]
        routing = routing or self.default_routing
        routing_key = (
            f"{int(routing.warp_off)}|{int(routing.proxy_off)}|{routing.forced_proxy or ''}"
        )
        hid = hashlib.sha1(
            ("|".join(stable) + str(len(segments)) + language + routing_key).encode()
        ).hexdigest()[:16]
        async with self._lock(hid):
            starts, total = [], 0.0
            for duration in durations[:len(segments)]:
                starts.append(total)
                total += duration
            metadata = {
                "segs": segments,
                "durs": durations[:len(segments)],
                "starts": starts,
                "iv": (re.search(r"IV=(0x[0-9A-Fa-f]+)", key_line) or [None, None])[1],
                "media_key": str(media_key or ""),
                "language": language,
                "headers": safe_headers,
                "routing": routing,
                "source_fingerprint": self.source_fingerprint(segments),
            }
            self._remember(hid, metadata, key)
        return hid

    def metadata(self, hid: str):
        return self._track(hid)["metadata"]

    def pin(self, hid: str) -> None:
        """Keep an audio track alive while a queued sync request is running."""
        self._track(hid)
        self._pinned[hid] = self._pinned.get(hid, 0) + 1

    def unpin(self, hid: str) -> None:
        count = self._pinned.get(hid, 0)
        if count <= 1:
            self._pinned.pop(hid, None)
        else:
            self._pinned[hid] = count - 1

    def key_bytes(self, hid: str) -> bytes:
        return self._track(hid)["key"]

    def cache_status(self, media_key: str, language: str) -> dict:
        """Report active state without pretending in-memory tracks are cache."""
        aliases = {
            "ita": {"ita", "it", "italian", "italiano"},
            "eng": {"eng", "en", "english"},
            "spa": {"spa", "es", "spanish", "spagnolo"},
            "fra": {"fra", "fr", "french", "francese"},
            "deu": {"deu", "de", "ger", "german", "tedesco"},
            "hin": {"hin", "hi", "hindi"},
            "rus": {"rus", "ru", "russian", "russo"},
        }
        wanted = aliases.get(str(language or "").lower().strip(), set())
        candidates = [
            (track["last_used"], hid, track["metadata"])
            for hid, track in self._tracks.items()
            if str(track["metadata"].get("media_key") or "") == str(media_key or "")
            and str(track["metadata"].get("language") or "").lower() in wanted
        ]
        if not candidates:
            return {
                "cached": False,
                "persistent": False,
                "active": False,
            }
        _, hid, metadata = max(candidates, key=lambda item: item[0])
        return {
            "cached": False,
            "persistent": False,
            "active": True,
            "hid": hid,
            "audio_fingerprint": metadata.get("source_fingerprint", ""),
        }

    def cleanup_idle(self, idle_seconds: float = TRACK_IDLE_SECONDS) -> int:
        """Release tracks whose player stopped requesting segments."""
        now = time.monotonic()
        removed = 0
        for hid, track in list(self._tracks.items()):
            if hid in self._pinned or hid in self._active:
                continue
            lock = self._locks.get(hid)
            if lock is not None and lock.locked():
                continue
            if now - track["last_used"] < idle_seconds:
                continue
            self._tracks.pop(hid, None)
            self._locks.pop(hid, None)
            removed += 1
        return removed

    @staticmethod
    def timeline(metadata: dict, offset: float = 0.0, rate: float = 1.0):
        if not 0.998 <= rate <= 1.002:
            raise ValueError("audio rate outside supported range")
        entries = []
        for index, (start, duration) in enumerate(zip(metadata["starts"], metadata["durs"])):
            shifted = float(start) + float(offset)
            trim = max(0.0, -shifted)
            if trim >= float(duration) - 0.02:
                continue
            entries.append({"idx": index, "start": max(0.0, shifted / rate), "trim": trim, "duration": (float(duration) - trim) / rate})
        return entries

    async def _download(
        self,
        url: str,
        headers: dict,
        destination: Path,
        routing: RoutingOptions | None = None,
    ):
        return await download_asset(
            url,
            headers,
            destination,
            routing or self.default_routing,
            max_redirects=self.MAX_REDIRECTS,
            max_bytes=self.MAX_SEGMENT_BYTES,
        )

    @staticmethod
    def _boxes(data: bytes, start=0, end=None):
        end = len(data) if end is None else end
        result, position = [], start
        while position + 8 <= end:
            size = struct.unpack(">I", data[position:position + 4])[0]
            kind = data[position + 4:position + 8]
            header = 8
            if size == 1:
                size = struct.unpack(">Q", data[position + 8:position + 16])[0]
                header = 16
            elif size == 0:
                size = end - position
            if size < header or position + size > end:
                break
            result.append((kind, position, size, header))
            position += size
        return result

    @classmethod
    def _find_box(cls, data, path, start=0, end=None):
        for kind, position, size, header in cls._boxes(data, start, end):
            if kind != path[0]:
                continue
            if len(path) == 1:
                return position, size, header
            found = cls._find_box(data, path[1:], position + header, position + size)
            if found:
                return found
        return None

    @classmethod
    def _patch_time(cls, fragment: bytearray, timescale: int, start_seconds: float):
        sidx = cls._find_box(bytes(fragment), [b"sidx"])
        if sidx:
            position, _, header = sidx
            version = fragment[position + header]
            sidx_timescale = struct.unpack(
                ">I", fragment[position + header + 8:position + header + 12]
            )[0]
            value = int(round(start_seconds * sidx_timescale))
            value_position = position + header + 12
            if version == 1:
                struct.pack_into(">Q", fragment, value_position, value)
            else:
                struct.pack_into(">I", fragment, value_position, value)
        found = cls._find_box(bytes(fragment), [b"moof", b"traf", b"tfdt"])
        if not found:
            raise ValueError("tfdt box missing")
        position, _, header = found
        version = fragment[position + header]
        value = int(round(start_seconds * timescale))
        if version == 1:
            struct.pack_into(">Q", fragment, position + header + 4, value)
        else:
            struct.pack_into(">I", fragment, position + header + 4, value)

    async def fragment_bytes(self, hid: str, index: int, offset: float, rate: float):
        offset_ms = int(round(offset * 1000))
        rate_nano = int(round(rate * 1_000_000_000))
        track = self._track(hid)
        key = (index, offset_ms, rate_nano)
        cached = (track.get("fragments") or {}).get(key)
        if cached is not None:
            return cached
        # One lock per requested fragment: the player and the prefetch task
        # share a single conversion instead of running ffmpeg twice.
        async with self._lock(f"{hid}|{key}"):
            cached = (track.get("fragments") or {}).get(key)
            if cached is not None:
                return cached
            self._active[hid] = self._active.get(hid, 0) + 1
            try:
                result = await self._convert(hid, index, offset, rate, track)
            finally:
                remaining = self._active.get(hid, 1) - 1
                if remaining > 0:
                    self._active[hid] = remaining
                else:
                    self._active.pop(hid, None)
            fragments = track.setdefault("fragments", {})
            fragments[key] = result
            while len(fragments) > self.MAX_CACHED_FRAGMENTS:
                fragments.pop(next(iter(fragments)))
            return result

    async def _convert(
        self,
        hid: str,
        index: int,
        offset: float,
        rate: float,
        track: dict,
    ) -> tuple[bytes, bytes]:
        metadata = track["metadata"]
        timeline = self.timeline(metadata, offset, rate)
        item = next((entry for entry in timeline if entry["idx"] == index), None)
        if not item:
            raise ValueError("audio segment outside timeline")
        with tempfile.TemporaryDirectory(prefix=f"dual-audio-{hid}-") as temp_dir:
            work = Path(temp_dir)
            await self._download(
                metadata["segs"][index],
                metadata.get("headers") or {},
                work / "src.ts",
                metadata.get("routing"),
            )
            key_bytes = track["key"]
            key_line = ""
            if key_bytes:
                (work / "enc.key").write_bytes(key_bytes)
                iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
                key_line = f'#EXT-X-KEY:METHOD=AES-128,URI="enc.key"{iv}\n'
            (work / "input.m3u8").write_text(
                "#EXTM3U\n#EXT-X-VERSION:3\n"
                f"#EXT-X-TARGETDURATION:{int(metadata['durs'][index]) + 1}\n"
                f"{key_line}"
                f"#EXTINF:{metadata['durs'][index]:.6f},\nsrc.ts\n#EXT-X-ENDLIST\n"
            )
            command = ["ffmpeg", "-v", "error", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", "input.m3u8"]
            if item["trim"] > 0.001:
                command += ["-ss", f"{item['trim']:.6f}"]
            command += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-f", "hls", "-hls_time", "99999", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "f%d.m4s", "-hls_list_size", "0", "-y", "output.m3u8"]
            process = await asyncio.create_subprocess_exec(*command, cwd=work, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, error = await asyncio.wait_for(process.communicate(), timeout=60)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
                raise
            made = work / "f0.m4s"
            if process.returncode or not made.exists():
                raise RuntimeError((error.decode(errors="replace") or "ffmpeg failed")[:300])
            init_data = (work / "init.mp4").read_bytes()
            fragment = bytearray(made.read_bytes())
            timescale = 48000
            found = self._find_box(init_data, [b"moov", b"trak", b"mdia", b"mdhd"])
            if found:
                position, _, header = found
                version = init_data[position + header]
                offset_pos = position + header + (20 if version == 1 else 12)
                timescale = struct.unpack(">I", init_data[offset_pos:offset_pos + 4])[0] or 48000
            self._patch_time(fragment, timescale, item["start"])
            return init_data, bytes(fragment)

    def prefetch(self, hid: str, indices: list[int], offset: float, rate: float) -> None:
        """Warm the fragment cache while the player is still starting."""
        for index in indices:
            task = asyncio.create_task(self._prefetch_one(hid, index, offset, rate))
            self._prefetch_tasks.add(task)
            task.add_done_callback(self._prefetch_tasks.discard)

    async def _prefetch_one(self, hid: str, index: int, offset: float, rate: float) -> None:
        try:
            await self.fragment_bytes(hid, index, offset, rate)
        except (FileNotFoundError, ValueError, RuntimeError, asyncio.TimeoutError, OSError):
            # Prefetch is best-effort: the player request converts on demand.
            pass
