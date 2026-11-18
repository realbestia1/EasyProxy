"""Serve HLS subtitle renditions on the DUAL audio timeline.

Video-source subtitles are proxied unchanged, but subtitles that come from the
audio source must follow the audio when the sync shifts it. Cue times are
mapped with the same transform the audio store applies to its fragments:
``(t + offset) / rate``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import struct
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .audio import AudioStore
from .http_client import download_asset
from .routing import RoutingOptions
from .security import valid_public_url


MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 8 * 1024 * 1024
MPEGTS_TIMESCALE = 90000

_CUE = re.compile(
    r"^([ \t]*)((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})[ \t]*-->[ \t]*"
    r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})(.*)$"
)
_MPEGTS = re.compile(r"(MPEGTS:)(\d+)")
_IV = re.compile(r"IV=(0x[0-9A-Fa-f]+)")
_MEDIA_SEQUENCE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")
_INIT = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.replace(",", ".").split(":"):
        total = total * 60.0 + float(part)
    return total


def _stamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def shift_webvtt(text: str, offset: float, rate: float) -> str:
    """Map every cue to the audio timeline; cues fully before 0 are dropped."""
    output: list[str] = []
    pending_id: str | None = None
    skipping = False
    in_block = False
    for line in text.splitlines():
        if not line.strip():
            if pending_id is not None:
                output.append(pending_id)
                pending_id = None
            skipping = False
            in_block = False
            output.append(line)
            continue
        if skipping:
            continue
        match = _CUE.match(line)
        if match:
            end = (_seconds(match.group(3)) + offset) / rate
            if end <= 0.0:
                pending_id = None
                skipping = True
                continue
            if pending_id is not None:
                output.append(pending_id)
                pending_id = None
            in_block = True
            start = (_seconds(match.group(2)) + offset) / rate
            output.append(
                f"{match.group(1)}{_stamp(start)} --> {_stamp(end)}{match.group(4)}"
            )
            continue
        if "X-TIMESTAMP-MAP" in line:
            line = _MPEGTS.sub(
                lambda item: f"{item.group(1)}"
                f"{max(0, round((int(item.group(2)) + offset * MPEGTS_TIMESCALE) / rate))}",
                line,
            )
            if pending_id is not None:
                output.append(pending_id)
                pending_id = None
            output.append(line)
            continue
        if in_block or line.lstrip("\ufeff").startswith(("WEBVTT", "STYLE", "NOTE", "REGION")):
            if pending_id is not None:
                output.append(pending_id)
                pending_id = None
            in_block = True
            output.append(line)
            continue
        if pending_id is not None:
            output.append(pending_id)
        pending_id = line
    if pending_id is not None:
        output.append(pending_id)
    return "\n".join(output) + "\n"


def as_webvtt(data: bytes) -> str | None:
    """Return the decoded text only when the payload really is WebVTT."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.lstrip("\ufeff \t\r\n").startswith("WEBVTT"):
        return text
    return None


def srt_to_webvtt(data: bytes) -> tuple[str, float]:
    """Convert one SRT payload to WebVTT and return the last cue end."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    output = ["WEBVTT", ""]
    duration = 0.0
    expect_number = True
    for line in text.split("\n"):
        if not line.strip():
            if output[-1] != "":
                output.append("")
            expect_number = True
            continue
        if expect_number and line.strip().isdigit():
            # SRT cue number; WebVTT does not need it.
            expect_number = False
            continue
        expect_number = False
        match = _CUE.match(line)
        if match:
            end = _seconds(match.group(3))
            duration = max(duration, end)
            line = (
                f"{match.group(1)}{_stamp(_seconds(match.group(2)))}"
                f" --> {_stamp(end)}{match.group(4)}"
            )
        output.append(line)
    return "\n".join(output).strip() + "\n", duration


def _vtt_duration(text: str) -> float:
    duration = 0.0
    for line in text.splitlines():
        match = _CUE.match(line)
        if match:
            duration = max(duration, _seconds(match.group(3)))
    return duration


def _safe_headers(headers: dict | None) -> dict[str, str]:
    return {
        str(header): str(value).strip()
        for header, value in (headers or {}).items()
        if re.fullmatch(r"[A-Za-z0-9-]+", str(header))
        and str(value).strip()
        and len(str(value).strip()) <= 1024
        and "\r" not in str(value)
        and "\n" not in str(value)
    }


class SubtitleStore:
    """Hold one subtitle rendition per audio prep and shift it on demand.

    Sources are never modified: the playlist and its assets are fetched from
    the public audio source and served through the DUAL routes.
    """

    TRACK_TTL_SECONDS = 21600
    TRACK_IDLE_SECONDS = 3600
    MAX_TRACKS = 256

    def __init__(self, proxy: str = ""):
        self.default_routing = RoutingOptions(forced_proxy=proxy.strip() or None)
        self._tracks: dict[str, dict] = {}

    def _track(self, sid: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{16}", str(sid or "")):
            raise ValueError("invalid subtitle id")
        track = self._tracks.get(sid)
        if track is None:
            raise FileNotFoundError("subtitle track not found")
        track["last_used"] = time.monotonic()
        return track

    def _remember(self, sid: str, metadata: dict, key: bytes) -> None:
        now = time.monotonic()
        self._tracks[sid] = {"metadata": metadata, "key": key, "last_used": now}
        for track_id, track in list(self._tracks.items()):
            if now - track["last_used"] > self.TRACK_TTL_SECONDS:
                self._tracks.pop(track_id, None)
        while len(self._tracks) > self.MAX_TRACKS:
            oldest = min(self._tracks, key=lambda item: self._tracks[item]["last_used"])
            self._tracks.pop(oldest, None)

    def metadata(self, sid: str) -> dict:
        return self._track(sid)["metadata"]

    def timeline(self, metadata: dict, offset: float = 0.0, rate: float = 1.0):
        if metadata.get("external"):
            if metadata.get("vtt") is None:
                raise ValueError("subtitle track not loaded")
            return [
                {"idx": 0, "start": 0.0, "trim": 0.0, "duration": metadata["duration"]}
            ]
        return AudioStore.timeline(metadata, offset, rate)

    async def register(
        self,
        playlist: str,
        key_b64: str,
        language: str = "",
        name: str = "",
        base_url: str = "",
        headers: dict | None = None,
        routing: RoutingOptions | None = None,
    ) -> str:
        if len(playlist) > MAX_PLAYLIST_BYTES or "#EXTINF" not in playlist:
            raise ValueError("invalid subtitle playlist")
        segments, durations, key_line = AudioStore._parse_playlist(playlist, base_url)
        if not segments or len(segments) > 10000:
            raise ValueError("invalid subtitle segment count")
        if len(durations) < len(segments):
            raise ValueError("subtitle durations do not match segments")
        if not all(valid_public_url(url) for url in segments):
            raise ValueError("subtitle segment URL is not public HTTPS")
        if key_b64:
            key = base64.b64decode(key_b64, validate=True)
            if len(key) != 16:
                raise ValueError("AES key must be 16 bytes")
        else:
            # Valid HLS subtitles can be unencrypted.
            key = b""
        safe_headers = _safe_headers(headers)
        map_uri = _INIT.search(playlist)
        init_url = urljoin(base_url, map_uri.group(1)) if map_uri else ""
        if init_url and not valid_public_url(init_url):
            raise ValueError("subtitle init URL is not public HTTPS")
        sequence = _MEDIA_SEQUENCE.search(playlist)
        starts, total = [], 0.0
        for duration in durations[:len(segments)]:
            starts.append(total)
            total += duration
        stable = [urlparse(url).path for url in segments[:3]]
        routing = routing or self.default_routing
        routing_key = (
            f"{int(routing.warp_off)}|{int(routing.proxy_off)}|{routing.forced_proxy or ''}"
        )
        sid = hashlib.sha1(
            (
                "|".join(stable)
                + str(len(segments))
                + language
                + name
                + routing_key
            ).encode()
        ).hexdigest()[:16]
        metadata = {
            "segs": segments,
            "durs": durations[:len(segments)],
            "starts": starts,
            "iv": (_IV.search(key_line) or [None, None])[1],
            "media_sequence": int(sequence.group(1)) if sequence else 0,
            "init": init_url,
            "language": str(language or ""),
            "name": str(name or ""),
            "headers": safe_headers,
            "routing": routing,
        }
        self._remember(sid, metadata, key)
        return sid

    def register_external(
        self,
        url: str,
        language: str = "",
        name: str = "",
        headers: dict | None = None,
        routing: RoutingOptions | None = None,
    ) -> str:
        """Register an external SRT file; it is downloaded on first use."""
        url = str(url or "").strip()
        if len(url) > MAX_PLAYLIST_BYTES or not valid_public_url(url):
            raise ValueError("subtitle URL is not public HTTPS")
        routing = routing or self.default_routing
        routing_key = (
            f"{int(routing.warp_off)}|{int(routing.proxy_off)}|{routing.forced_proxy or ''}"
        )
        sid = hashlib.sha1(
            f"{url}|{language}|{name}|{routing_key}".encode()
        ).hexdigest()[:16]
        metadata = {
            "external": url,
            "language": str(language or ""),
            "name": str(name or ""),
            "headers": _safe_headers(headers),
            "routing": routing,
            "vtt": None,
            "duration": 0.0,
        }
        self._remember(sid, metadata, b"")
        self._tracks[sid]["lock"] = asyncio.Lock()
        return sid

    async def ensure_loaded(self, sid: str) -> dict:
        """Download and convert one external SRT, once per track."""
        track = self._track(sid)
        metadata = track["metadata"]
        if not metadata.get("external") or metadata.get("vtt") is not None:
            return metadata
        lock = track.get("lock")
        if lock is None:
            lock = track["lock"] = asyncio.Lock()
        async with lock:
            if metadata.get("vtt") is None:
                data = await self._fetch(metadata, metadata["external"])
                text = as_webvtt(data)
                duration = 0.0
                if text is None:
                    text, duration = srt_to_webvtt(data)
                else:
                    duration = _vtt_duration(text)
                if duration <= 0.0:
                    raise ValueError("subtitle track has no cues")
                metadata["vtt"] = text
                metadata["duration"] = duration
        return metadata

    async def _fetch(self, metadata: dict, url: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="dual-sub-") as directory:
            destination = Path(directory) / "asset"
            await download_asset(
                url,
                metadata.get("headers") or {},
                destination,
                metadata.get("routing"),
                max_bytes=MAX_SEGMENT_BYTES,
            )
            return destination.read_bytes()

    def _decrypt(self, track: dict, index: int, data: bytes) -> bytes:
        metadata = track["metadata"]
        iv_attr = str(metadata.get("iv") or "")
        if iv_attr:
            iv = bytes.fromhex(iv_attr[2:] if iv_attr.lower().startswith("0x") else iv_attr)
            if len(iv) != 16:
                raise ValueError("invalid subtitle AES IV")
        else:
            iv = (int(metadata.get("media_sequence") or 0) + index).to_bytes(16, "big")
        if len(data) % 16:
            raise ValueError("encrypted subtitle asset is not block aligned")
        decryptor = Cipher(algorithms.AES(track["key"]), modes.CBC(iv)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
        if plain:
            pad = plain[-1]
            if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
                plain = plain[:-pad]
        return plain

    @staticmethod
    def _timescale(init: bytes) -> int:
        found = AudioStore._find_box(init, [b"moov", b"trak", b"mdia", b"mdhd"])
        if not found:
            return 48000
        position, _, header = found
        version = init[position + header]
        offset = position + header + (20 if version == 1 else 12)
        return struct.unpack(">I", init[offset:offset + 4])[0] or 48000

    async def init_bytes(self, sid: str) -> bytes:
        track = self._track(sid)
        metadata = track["metadata"]
        if not metadata.get("init"):
            raise ValueError("subtitle track has no init segment")
        cached = metadata.get("init_bytes")
        if cached is None:
            cached = await self._fetch(metadata, metadata["init"])
            if track["key"]:
                cached = self._decrypt(track, int(metadata.get("media_sequence") or 0), cached)
            metadata["init_bytes"] = cached
        return cached

    async def payload(self, sid: str, index: int, offset: float, rate: float):
        """Return one subtitle asset already mapped onto the audio timeline."""
        track = self._track(sid)
        metadata = track["metadata"]
        if metadata.get("external"):
            if index != 0:
                raise ValueError("subtitle segment outside timeline")
            return (
                str((await self.ensure_loaded(sid))["vtt"]).encode("utf-8"),
                "text/vtt; charset=utf-8",
            )
        entry = next(
            (item for item in self.timeline(metadata, offset, rate) if item["idx"] == index),
            None,
        )
        if entry is None:
            raise ValueError("subtitle segment outside timeline")
        data = await self._fetch(metadata, metadata["segs"][index])
        if track["key"]:
            data = self._decrypt(track, index, data)
        text = as_webvtt(data)
        if text is not None:
            return shift_webvtt(text, offset, rate).encode("utf-8"), "text/vtt; charset=utf-8"
        if metadata.get("init"):
            # fMP4 (wvtt) subtitles carry their cue times in the samples, so
            # patching the fragment start is enough to move them.
            fragment = bytearray(data)
            AudioStore._patch_time(
                fragment, self._timescale(await self.init_bytes(sid)), entry["start"]
            )
            return bytes(fragment), "video/iso.segment"
        return data, "application/octet-stream"

    def cleanup_idle(self, idle_seconds: float = TRACK_IDLE_SECONDS) -> int:
        now = time.monotonic()
        removed = 0
        for sid, track in list(self._tracks.items()):
            if now - track["last_used"] < idle_seconds:
                continue
            self._tracks.pop(sid, None)
            removed += 1
        return removed


__all__ = ["SubtitleStore", "as_webvtt", "shift_webvtt", "srt_to_webvtt"]
