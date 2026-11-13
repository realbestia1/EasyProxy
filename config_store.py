import json
import os
import sys
import logging
import threading

logger = logging.getLogger(__name__)

# Docker keeps its persistent volume at /data.  Native Windows runs should
# keep the same layout inside the EasyProxy checkout instead of writing to
# the drive root (C:\data).
_PROJECT_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
_DEFAULT_CONFIG_DIR = (
    os.path.join(_PROJECT_DIR, "data") if os.name == "nt" else "/data"
)
_CONFIG_DIR = os.environ.get("CONFIG_DIR") or _DEFAULT_CONFIG_DIR
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
# Public alias: tunnel profiles and other persistent files live beside config.json.
CONFIG_DIR = _CONFIG_DIR
DEFAULT_RECORDINGS_DIR = os.path.join(_CONFIG_DIR, "recordings")

DEFAULT_CONFIG = {
    "enable_warp": False,
    "warp_license_key": "",
    "warp_exclude_domains": [
        "strem.fun", "*.strem.fun", "torrentio.strem.fun",
        "real-debrid.com", "*.real-debrid.com", "realdebrid.com",
        "*.realdebrid.com", "api.real-debrid.com",
        "premiumize.me", "*.premiumize.me", "www.premiumize.me",
        "alldebrid.com", "*.alldebrid.com", "api.alldebrid.com",
        "debrid-link.com", "*.debrid-link.com", "debridlink.com",
        "*.debridlink.com", "api.debrid-link.com",
        "torbox.app", "*.torbox.app", "api.torbox.app",
        "offcloud.com", "*.offcloud.com", "api.offcloud.com",
        "put.io", "*.put.io", "api.put.io",
    ],
    "warp_exclude_domains_custom": [],
    "global_proxies": [],
    "transport_routes": [],
    # Secondary userspace WireGuard tunnels (wireproxy) exposed as local SOCKS5
    # proxies. WARP keeps 127.0.0.1:1080, these slots listen on their own port.
    "nordvpn_token": "",
    "nordvpn_server": "",
    "nordvpn_bind": "127.0.0.1:1081",
    "nordvpn_enabled": False,
    "wg_custom_config": "",
    "wg_custom_bind": "127.0.0.1:1082",
    "wg_custom_enabled": False,
    "tor_bind": "127.0.0.1:9050",
    "tor_enabled": False,
    # Effective Tor exit pin ($fingerprint or raw ExitNodes value).
    "tor_exit_nodes": "",
    # Preferred exit country; the app picks a relay there and pins its fingerprint.
    "tor_exit_country": "",
    "extractor_proxies": {},
    # Cinejoy's gateway rejects Cloudflare WARP egress (HTTP 403) and
    # Sportsonline's player serves a Cloudflare Turnstile-gated variant to WARP
    # egress IPs. Keep both resolvers direct unless the user supplies a route.
    "warp_off_extractors": ["cinejoy", "dlstreams", "sportsonline"],
    "proxy_off_extractors": [],
    "proxy_exclude_domains": [],
    # Force the highest video variant (no adaptive bitrate). Can be enabled per
    # extractor, for every MPD source, for every HLS source, or per request
    # with &max_res=true.
    "max_res_extractors": [],
    "max_res_mpd": False,
    "max_res_hls": False,
    "dvr_enabled": False,
    "recordings_dir": DEFAULT_RECORDINGS_DIR,
    "max_recording_duration": 28800,
    "recordings_retention_days": 7,
    "proxy_test_timeout": 10,
    "proxy_test_concurrency": None,
    "log_level": "WARNING",
}

_lock = threading.RLock()
_config_data = None


def _load():
    global _config_data
    with _lock:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        if os.path.exists(_CONFIG_FILE):
            try:
                with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                # ponytail: merge default warp_exclude_domains so mandatory debrid exclusions are always present
                if "warp_exclude_domains" in data:
                    combined = list(DEFAULT_CONFIG["warp_exclude_domains"])
                    for item in data["warp_exclude_domains"]:
                        if item not in combined:
                            combined.append(item)
                    merged["warp_exclude_domains"] = combined
                _config_data = merged
                logger.debug("Loaded config from %s", _CONFIG_FILE)
                return
            except Exception as e:
                logger.warning("Failed to load config.json: %s", e)
                if _config_data is None:
                    _config_data = dict(DEFAULT_CONFIG)
                return
        _config_data = dict(DEFAULT_CONFIG)
        _save()


def _save():
    with _lock:
        if _config_data is None:
            return
        tmp_file = f"{_CONFIG_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(_config_data, f, indent=2)
            os.replace(tmp_file, _CONFIG_FILE)
        except Exception as e:
            logger.error("Failed to save config.json: %s", e)
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass


def get(key, default=None):
    with _lock:
        if _config_data is None:
            _load()
        return _config_data.get(key, default)


def set(key, value):
    with _lock:
        if _config_data is None:
            _load()
        _config_data[key] = value
        _save()


def get_all():
    with _lock:
        if _config_data is None:
            _load()
        return dict(_config_data)


def update(values: dict):
    with _lock:
        if _config_data is None:
            _load()
        _config_data.update(values)
        _save()


def replace_all(data: dict):
    """Replace entire config with new data (merged with defaults)."""
    global _config_data
    with _lock:
        if _config_data is None:
            _load()
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        _config_data = merged
        _save()


def delete(key):
    with _lock:
        if _config_data is None:
            _load()
        _config_data.pop(key, None)
        _save()


_load()
