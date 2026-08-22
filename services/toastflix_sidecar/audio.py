import asyncio
import base64
import hashlib
import json
import re
import struct
import tempfile
import time
import weakref
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .security import resolves_publicly, valid_public_url
from .http_client import client_session


class AudioStore:
    """Hold audio metadata in memory and never persist audio fragments.

    The sidecar is used as a personal service, so source audio and generated
    fMP4 fragments are processed per request and removed with their temporary
    working directory. Offset data remains persisted separately by OffsetStore.
    """

    TRACK_TTL_SECONDS = 21600
    MAX_TRACKS = 128
    MAX_REDIRECTS = 5
    MAX_SEGMENT_BYTES = 20 * 1024 * 1024

    def __init__(self, root: str, proxy: str = "", max_bytes: int | None = None):
        self.proxy = proxy.strip()
        # A weak map keeps per-track locks only while a request still holds a
        # reference to them. It cannot grow permanently with every new HID.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._tracks: dict[str, dict] = {}

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
            if now - track["last_used"] > self.TRACK_TTL_SECONDS
        ]
        for key_id in expired:
            self._tracks.pop(key_id, None)
        while len(self._tracks) > self.MAX_TRACKS:
            oldest = min(self._tracks, key=lambda key_id: self._tracks[key_id]["last_used"])
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

    async def register(self, playlist: str, key_b64: str, media_key: str,
                       language: str, base_url: str = "", headers: dict | None = None):
        if len(playlist) > 2 * 1024 * 1024 or "#EXTINF" not in playlist:
            raise ValueError("invalid audio playlist")
        language = str(language or "").lower().strip()
        if language not in {"ita", "eng", "it", "en", "italian", "english"}:
            raise ValueError("language must be ita or eng")
        segments, durations, key_line = self._parse_playlist(playlist, base_url)
        self._validate_segments(segments)
        if len(durations) < len(segments):
            raise ValueError("audio durations do not match segments")
        key = base64.b64decode(key_b64, validate=True)
        if len(key) != 16:
            raise ValueError("AES key must be 16 bytes")
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
        hid = hashlib.sha1(("|".join(stable) + str(len(segments)) + language).encode()).hexdigest()[:16]
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
                "source_fingerprint": hashlib.sha1(
                    ("|".join(stable) + str(len(segments))).encode()
                ).hexdigest()[:20],
            }
            self._remember(hid, metadata, key)
        return hid

    def metadata(self, hid: str):
        return self._track(hid)["metadata"]

    def key_bytes(self, hid: str) -> bytes:
        return self._track(hid)["key"]

    def find_cached(self, media_key: str, language: str):
        # Persistent audio reuse is intentionally disabled. Toastflix will
        # call /dual/aprep again and receive a fresh in-memory track.
        return None

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

    async def _download(self, url: str, headers: dict, destination: Path):
        current_url = url
        async with client_session(self.proxy, timeout=30) as (client, request_proxy):
            for redirect_count in range(self.MAX_REDIRECTS + 1):
                if not valid_public_url(current_url) or not await resolves_publicly(current_url):
                    raise ValueError("audio URL is not public HTTPS")
                async with client.get(
                    current_url,
                    headers=headers,
                    proxy=request_proxy,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise ValueError("audio redirect has no location")
                        next_url = urljoin(current_url, location)
                        if not valid_public_url(next_url) or not await resolves_publicly(next_url):
                            raise ValueError("audio redirect is not public HTTPS")
                        if redirect_count >= self.MAX_REDIRECTS:
                            raise ValueError("too many audio redirects")
                        current_url = next_url
                        continue

                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > self.MAX_SEGMENT_BYTES:
                                raise ValueError("audio segment too large")
                        except ValueError as exc:
                            if str(exc) == "audio segment too large":
                                raise
                            raise ValueError("invalid audio content length") from exc

                    try:
                        total = 0
                        with destination.open("wb") as output:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                total += len(chunk)
                                if total > self.MAX_SEGMENT_BYTES:
                                    raise ValueError("audio segment too large")
                                output.write(chunk)
                    except Exception:
                        destination.unlink(missing_ok=True)
                        raise
                    return total

        raise ValueError("too many audio redirects")

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
        metadata = self.metadata(hid)
        timeline = self.timeline(metadata, offset, rate)
        item = next((entry for entry in timeline if entry["idx"] == index), None)
        if not item:
            raise ValueError("audio segment outside timeline")
        async with self._lock(hid):
            with tempfile.TemporaryDirectory(prefix=f"sidecar-audio-{hid}-") as temp_dir:
                work = Path(temp_dir)
                await self._download(
                    metadata["segs"][index],
                    metadata.get("headers") or {},
                    work / "src.ts",
                )
                (work / "enc.key").write_bytes(self.key_bytes(hid))
                iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
                (work / "input.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-VERSION:3\n"
                    f"#EXT-X-TARGETDURATION:{int(metadata['durs'][index]) + 1}\n"
                    f"#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\"{iv}\n"
                    f"#EXTINF:{metadata['durs'][index]:.6f},\nsrc.ts\n#EXT-X-ENDLIST\n"
                )
                command = ["ffmpeg", "-v", "error", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", "input.m3u8"]
                if item["trim"] > 0.001:
                    command += ["-ss", f"{item['trim']:.6f}"]
                command += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-f", "hls", "-hls_time", "99999", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "f%d.m4s", "-hls_list_size", "0", "-y", "output.m3u8"]
                process = await asyncio.create_subprocess_exec(*command, cwd=work, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, error = await asyncio.wait_for(process.communicate(), timeout=60)
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
