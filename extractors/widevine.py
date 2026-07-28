import re
from pathlib import Path


def resolve_widevine_device() -> Path:
    device_path = Path(__file__).with_name("widevine-device.wvd")
    if device_path.is_file():
        return device_path
    raise RuntimeError(
        "Widevine CDM device not found next to the protected VOD extractors"
    )


def extract_widevine_pssh(manifest_text: str) -> str:
    values = [
        value.strip()
        for value in re.findall(
            r"<(?:[\w.-]+:)?pssh\b[^>]*>([^<]+)</(?:[\w.-]+:)?pssh>",
            manifest_text or "",
            re.IGNORECASE,
        )
        if value.strip()
    ]
    if not values:
        raise RuntimeError("DASH manifest does not contain a Widevine PSSH")
    return min(values, key=len)
