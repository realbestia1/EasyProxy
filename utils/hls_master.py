"""Single reader for HLS master playlists, shared by the proxy and the recorder.

Attribute lists follow RFC 8216 (quoted values may contain commas) and are
parsed here once instead of being re-implemented at every call site.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)="([^"]*)"|([A-Z0-9-]+)=([^,]*)')
_URI = re.compile(r'URI="([^"]*)"')
_VIDEO_CODECS = ("avc1", "hvc1", "dvh1", "hev1", "vp9", "av01")


def attributes(tag: str) -> dict[str, str]:
    """Attribute list of an HLS tag line; quoted values keep their commas."""
    result: dict[str, str] = {}
    for match in _ATTRIBUTE.finditer(tag):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(1) else match.group(4)
        result[key] = value.strip()
    return result


def tag_uri(tag: str) -> str:
    """URI attribute value of an HLS tag line, "" when absent."""
    match = _URI.search(tag)
    return (match.group(1) or "") if match else ""


def replace_uri(tag: str, new_uri: str) -> str:
    """Tag line with its URI attribute value replaced, unchanged when absent."""
    match = _URI.search(tag)
    return tag[: match.start(1)] + new_uri + tag[match.end(1):] if match else tag


def is_master(text: str) -> bool:
    """A master playlist carries variant streams or rendition media."""
    return "#EXT-X-STREAM-INF:" in text or "#EXT-X-MEDIA:" in text


@dataclass(frozen=True)
class MasterEntry:
    """One #EXT-X-MEDIA or #EXT-X-STREAM-INF entry of a master playlist."""

    kind: str  # "media" | "variant"
    line_index: int  # index of the tag line
    uri_index: int  # index of the URI line of a variant, else line_index
    attributes: dict[str, str]
    url: str  # URI resolved against the master URL, "" when absent

    @property
    def type(self) -> str:
        return self.attributes.get("TYPE", "").strip().upper()

    @property
    def bandwidth(self) -> int:
        try:
            return int(self.attributes.get("BANDWIDTH") or 0)
        except ValueError:
            return 0

    @property
    def resolution(self) -> tuple[int, int]:
        match = re.match(r"(\d+)x(\d+)", self.attributes.get("RESOLUTION", "").strip(), re.I)
        return (int(match.group(1)), int(match.group(2))) if match else (0, 0)

    @property
    def has_video_codec(self) -> bool:
        """Video proven by an explicit CODECS entry or by RESOLUTION."""
        return bool(self.resolution) or any(
            codec in self.attributes.get("CODECS", "") for codec in _VIDEO_CODECS
        )


def master_entries(lines: Iterable[str], base_url: str = "") -> list[MasterEntry]:
    """Every media rendition and variant of a master playlist, in file order."""
    source: Sequence[str] = list(lines)
    found: list[MasterEntry] = []
    for index, raw in enumerate(source):
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            uri = tag_uri(line)
            found.append(
                MasterEntry(
                    "media",
                    index,
                    index,
                    attributes(line),
                    urljoin(base_url, uri) if uri else "",
                )
            )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            uri = ""
            uri_index = index
            if index + 1 < len(source):
                following = source[index + 1].strip()
                if following and not following.startswith("#"):
                    uri, uri_index = following, index + 1
            found.append(
                MasterEntry(
                    "variant",
                    index,
                    uri_index,
                    attributes(line),
                    urljoin(base_url, uri) if uri else "",
                )
            )
    return found
