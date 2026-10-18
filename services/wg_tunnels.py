"""Secondary userspace WireGuard tunnels exposed as local SOCKS5 proxies.

Cloudflare WARP keeps its own relay on 127.0.0.1:1080 through
``scripts/wireproxy_ctl.sh``. This module manages two extra, independent
wireproxy slots, each with its own profile, port, pid file and log:

    nordvpn -> <data>/nordvpn.conf    profile generated from a NordVPN token
    custom  -> <data>/wg_custom.conf  profile pasted in the admin panel

Both slots are plain local SOCKS5 endpoints. Reference them from Global Proxies
or Transport Routes (for example ``socks5h://127.0.0.1:1081``); they are not
wired into the routing chain as a special provider.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import time
import urllib.parse

import aiohttp

import config_store

logger = logging.getLogger(__name__)

NORD_API = "https://api.nordvpn.com"
NORD_CREDENTIALS_URL = f"{NORD_API}/v1/users/services/credentials"
NORD_SERVERS_URL = f"{NORD_API}/v1/servers"
# Sparse fieldset: the default payload carries every technology, service, group
# and specification for ~7700 servers (28 MB). Only ask for what the panel needs.
NORD_SERVERS_FIELDS = (
    "fields[servers.hostname]=true&fields[servers.station]=true"
    "&fields[servers.load]=true&fields[servers.locations]=true"
    "&fields[servers.technologies]=true"
)
NORD_DNS = "103.86.96.100, 103.86.99.100"
NORDLYNX_ADDRESS = "10.5.0.2/32"
# NordLynx default MTU; wireproxy also defaults to 1420 but being explicit keeps
# pasted profiles readable and avoids surprises on links that need 1280.
DEFAULT_MTU = 1420
# Keeps NAT mappings alive so an idle tunnel stays reachable.
DEFAULT_KEEPALIVE = 25

SERVER_CACHE_TTL = 900
# The normalized list is a few MB in RAM: release it when nothing used it for
# this long (the keepalive loop checks every 30s).
SERVER_CACHE_IDLE = 300
# Traffic watchdog: probe enabled tunnels every N keepalive ticks (30s each) and
# restart one after two consecutive failures, so a live process with a dead
# tunnel (endpoint changed, session dropped) recovers on its own.
HEALTH_CHECK_EVERY = 10
HEALTH_FAILURES_BEFORE_RESTART = 2
# Request-triggered recovery: after a failed proxy request through a local
# tunnel, restart it at most once per this window so a burst of failing
# segment requests triggers a single restart.
RECONNECT_COOLDOWN = 20.0
API_TIMEOUT = 25

SLOTS = ("nordvpn", "custom")
SLOT_KEYS = {
    "nordvpn": {"bind": "nordvpn_bind", "enabled": "nordvpn_enabled"},
    "custom": {"bind": "wg_custom_bind", "enabled": "wg_custom_enabled"},
}
PROFILE_NAMES = {"nordvpn": "nordvpn.conf", "custom": "wg_custom.conf"}

# wg-quick only directives: wireproxy has no routing table or hooks of its own.
_WG_QUICK_ONLY = {
    "preup", "postup", "predown", "postdown", "saveconfig", "table", "fwmark",
}

_BIND_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.\-\[\]:]+):(?P<port>\d{1,5})$")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL_SCRIPT = os.path.join(_PROJECT_DIR, "scripts", "wireproxy_ctl.sh")

_server_cache = {"fetched_at": 0.0, "used_at": 0.0, "servers": []}
_server_lock = asyncio.Lock()


class TunnelError(Exception):
    """Raised for user-facing tunnel/NordVPN errors."""


# --------------------------------------------------------------------------- #
# Slot plumbing
# --------------------------------------------------------------------------- #

def _data_dir() -> str:
    return config_store.CONFIG_DIR


def profile_path(slot: str) -> str:
    return os.path.join(_data_dir(), PROFILE_NAMES[slot])


def tunnel_dir(slot: str) -> str:
    base = os.environ.get("TEMP") if os.name == "nt" else "/tmp"
    return os.path.join(base or "/tmp", f"easyproxy-wg-{slot}")


def log_path(slot: str) -> str:
    if os.name == "nt":
        return os.path.join(tunnel_dir(slot), "wireproxy.log")
    return f"/var/log/wireproxy-{slot}.log"


def available() -> bool:
    """wireproxy control needs the POSIX helper script and the binary."""
    if os.name == "nt":
        return False
    if not os.path.exists(CTL_SCRIPT):
        return False
    return bool(shutil.which("wireproxy") or os.path.exists("/usr/local/bin/wireproxy"))


def get_bind(slot: str) -> str:
    return str(config_store.get(SLOT_KEYS[slot]["bind"], "") or "").strip()


def is_enabled(slot: str) -> bool:
    return bool(config_store.get(SLOT_KEYS[slot]["enabled"], False))


def set_enabled(slot: str, value: bool) -> None:
    config_store.set(SLOT_KEYS[slot]["enabled"], bool(value))


def set_bind(slot: str, value: str) -> str:
    bind = (value or "").strip()
    match = _BIND_RE.match(bind)
    if not match or not 1 <= int(match.group("port")) <= 65535:
        raise TunnelError(f"Invalid bind address: {value!r} (expected host:port)")
    config_store.set(SLOT_KEYS[slot]["bind"], bind)
    return bind


def _normalized_host(value: str) -> str:
    host = (value or "").strip().strip("[]").lower()
    if host in ("0.0.0.0", "::", "localhost"):
        return "127.0.0.1"
    return host


def slot_for_proxy_url(proxy_url: str | None) -> str | None:
    """Return the tunnel slot whose SOCKS bind matches this proxy URL."""
    if not proxy_url:
        return None
    try:
        parsed = urllib.parse.urlsplit(proxy_url)
    except ValueError:
        return None
    if not parsed.hostname or not parsed.port:
        return None
    host = _normalized_host(parsed.hostname)
    for slot in SLOTS:
        bind = get_bind(slot)
        if not bind:
            continue
        bind_host, _, bind_port = bind.rpartition(":")
        try:
            if _normalized_host(bind_host) == host and int(bind_port) == parsed.port:
                return slot
        except ValueError:
            continue
    return None


def _pid_file(slot: str) -> str:
    return os.path.join(tunnel_dir(slot), "wireproxy.pid")


def _read_pid(slot: str) -> int | None:
    try:
        with open(_pid_file(slot), "r") as handle:
            pid = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid or None


def process_running(slot: str) -> bool:
    """Check the recorded pid without spawning a shell (Linux /proc only)."""
    pid = _read_pid(slot)
    if not pid:
        return False
    if os.name == "nt":
        return False
    try:
        with open(f"/proc/{pid}/comm", "r") as handle:
            return handle.read().strip() == "wireproxy"
    except OSError:
        return False


async def run_ctl(slot: str, action: str, timeout: float = 30.0) -> tuple[int, str]:
    """Run the wireproxy helper for one slot and return (returncode, output)."""
    if not available():
        return 127, "wireproxy control is not available on this platform"
    env = dict(os.environ)
    env.update({
        "WG_CONFIG_FILE": profile_path(slot),
        "WG_SOCKS_BIND": get_bind(slot),
        "WG_TUNNEL_DIR": tunnel_dir(slot),
        "WG_LOG_FILE": log_path(slot),
    })
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", CTL_SCRIPT, action,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return 124, f"wireproxy {action} timed out"
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise
    except OSError as exc:
        return 126, f"wireproxy {action} failed to start: {exc}"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


async def start(slot: str) -> str:
    if not os.path.exists(profile_path(slot)):
        raise TunnelError("No WireGuard profile configured for this tunnel")
    code, out = await run_ctl(slot, "start")
    if code != 0:
        raise TunnelError(out or f"wireproxy start failed (exit {code})")
    return out


async def stop(slot: str) -> str:
    code, out = await run_ctl(slot, "stop")
    if code != 0:
        raise TunnelError(out or f"wireproxy stop failed (exit {code})")
    return out


async def restart(slot: str) -> str:
    code, out = await run_ctl(slot, "restart")
    if code != 0:
        raise TunnelError(out or f"wireproxy restart failed (exit {code})")
    return out


async def probe(slot: str) -> str:
    code, out = await run_ctl(slot, "probe", timeout=15.0)
    if code != 0:
        return ""
    return out.strip()


async def check(slot: str, attempts: int = 3, retry_delay: float = 2.0) -> dict:
    """VPN check: latency through the tunnel plus the egress IP.

    ping_ms is the TLS handshake (it needs round trips through the VPN, unlike
    the local SOCKS socket) and http_ms is the whole request. A freshly started
    tunnel can drop the first requests while the session warms up, so the check
    retries a couple of times before reporting a failure.
    """
    result = {"ok": False, "ping_ms": None, "http_ms": None, "egress_ip": "", "error": ""}
    if not process_running(slot):
        result["error"] = "tunnel is not running"
        return result

    for attempt in range(max(1, attempts)):
        outcome = await _ping_once(slot)
        if outcome["ok"] or attempt == attempts - 1:
            return outcome
        logger.info("VPN check for %s failed (%s), retrying", slot, outcome["error"])
        await asyncio.sleep(retry_delay)
    return result


async def _ping_once(slot: str) -> dict:
    result = {"ok": False, "ping_ms": None, "http_ms": None, "egress_ip": "", "error": ""}
    code, out = await run_ctl(slot, "ping", timeout=20.0)
    if code != 0:
        result["error"] = out or "VPN check failed"
        return result

    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        result["error"] = "empty VPN check output"
        return result
    metrics = lines[-1].split()
    try:
        result["ping_ms"] = round(float(metrics[0]) * 1000, 1)
        result["http_ms"] = round(float(metrics[1]) * 1000, 1)
        status = metrics[2]
    except (IndexError, ValueError):
        result["error"] = f"unexpected VPN check output: {out[:120]}"
        return result

    result["egress_ip"] = lines[0] if len(lines) > 1 else ""
    result["ok"] = status.startswith("2") and bool(result["egress_ip"])
    if not result["ok"]:
        result["error"] = f"HTTP {status}"
    return result


async def logs(slot: str) -> str:
    _, out = await run_ctl(slot, "logs", timeout=10.0)
    return out


# --------------------------------------------------------------------------- #
# WireGuard profiles
# --------------------------------------------------------------------------- #

def parse_profile(text: str) -> list[dict]:
    """Parse a WireGuard profile into [{section, options}] blocks."""
    sections: list[dict] = []
    current: dict | None = None
    for raw_line in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {"section": line[1:-1].strip().lower(), "options": {}}
            sections.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current["options"][key.strip()] = value.strip()
    return sections


def profile_summary(text: str) -> dict:
    """Human readable profile facts for the panels."""
    sections = parse_profile(text)
    interface = next((s for s in sections if s["section"] == "interface"), {})
    peers = [s for s in sections if s["section"] == "peer"]
    peer = peers[0] if peers else {}
    return {
        "address": interface.get("options", {}).get("Address", ""),
        "dns": interface.get("options", {}).get("DNS", ""),
        "mtu": interface.get("options", {}).get("MTU", ""),
        "endpoint": peer.get("options", {}).get("Endpoint", ""),
        "allowed_ips": peer.get("options", {}).get("AllowedIPs", ""),
        "keepalive": peer.get("options", {}).get("PersistentKeepalive", ""),
        "peers": len(peers),
        "has_private_key": bool(interface.get("options", {}).get("PrivateKey")),
        "has_public_key": bool(peer.get("options", {}).get("PublicKey")),
    }


def validate_profile(text: str) -> str:
    """Normalize a pasted WireGuard profile or raise TunnelError."""
    if not (text or "").strip():
        raise TunnelError("Empty WireGuard configuration")

    sections = parse_profile(text)
    interface = next((s for s in sections if s["section"] == "interface"), None)
    peer = next((s for s in sections if s["section"] == "peer"), None)
    if interface is None:
        raise TunnelError("Missing [Interface] section")
    if peer is None:
        raise TunnelError("Missing [Peer] section")
    for key in ("PrivateKey", "Address"):
        if not interface["options"].get(key):
            raise TunnelError(f"Missing {key} in [Interface]")
    for key in ("PublicKey", "Endpoint"):
        if not peer["options"].get(key):
            raise TunnelError(f"Missing {key} in [Peer]")

    lines: list[str] = []
    for block in sections:
        lines.append(f"[{block['section'].capitalize()}]")
        options = dict(block["options"])
        if block["section"] == "interface":
            # wireproxy resolves hostnames through this DNS server.
            options.setdefault("DNS", "1.1.1.1")
            options.setdefault("MTU", str(DEFAULT_MTU))
        if block["section"] == "peer":
            options.setdefault("PersistentKeepalive", str(DEFAULT_KEEPALIVE))
        for key, value in options.items():
            if key.lower() in _WG_QUICK_ONLY:
                continue
            lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_profile(slot: str, text: str) -> str:
    path = profile_path(slot)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as handle:
        handle.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def build_nordvpn_profile(private_key: str, server: dict) -> str:
    hostname = server.get("hostname") or ""
    public_key = server.get("public_key") or ""
    if not hostname or not public_key:
        raise TunnelError("Server is missing its WireGuard public key")
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {NORDLYNX_ADDRESS}\n"
        f"DNS = {NORD_DNS}\n"
        f"MTU = {DEFAULT_MTU}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {public_key}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        f"Endpoint = {hostname}:51820\n"
        f"PersistentKeepalive = {DEFAULT_KEEPALIVE}\n"
    )


# --------------------------------------------------------------------------- #
# NordVPN API
# --------------------------------------------------------------------------- #

async def _get_json(url: str, headers: dict | None = None) -> object:
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(url, headers=headers or {}) as response:
                body = await response.text()
                if response.status != 200:
                    raise TunnelError(
                        f"NordVPN API returned HTTP {response.status}: {body[:200]}"
                    )
                return json.loads(body)
    except TunnelError:
        raise
    except aiohttp.ClientError as exc:
        raise TunnelError(f"NordVPN API unreachable: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise TunnelError("NordVPN API request timed out") from exc
    except ValueError as exc:
        raise TunnelError("NordVPN API returned invalid JSON") from exc


async def fetch_nordlynx_private_key(token: str) -> str:
    """Exchange a NordVPN access token for the account NordLynx private key."""
    token = (token or "").strip()
    if not token:
        raise TunnelError("Missing NordVPN access token")
    credentials = base64.b64encode(f"token:{token}".encode()).decode()
    payload = await _get_json(NORD_CREDENTIALS_URL, {"Authorization": f"Basic {credentials}"})
    if not isinstance(payload, dict):
        raise TunnelError("Unexpected NordVPN credentials response")
    private_key = (payload.get("nordlynx_private_key") or "").strip()
    if not private_key:
        raise TunnelError("NordVPN did not return a NordLynx private key (check the token)")
    return private_key


def _normalize_server(raw: dict) -> dict | None:
    public_key = ""
    for tech in raw.get("technologies") or []:
        if tech.get("identifier") != "wireguard_udp":
            continue
        for item in tech.get("metadata") or []:
            if item.get("name") == "public_key":
                public_key = item.get("value") or ""
    if not public_key:
        return None

    location = (raw.get("locations") or [{}])[0]
    country = location.get("country") or {}
    city = country.get("city") or {}
    return {
        "id": raw.get("id"),
        "hostname": raw.get("hostname") or "",
        "ip": raw.get("station") or "",
        "load": raw.get("load") or 0,
        "country": country.get("name") or "",
        "country_code": country.get("code") or "",
        "city": city.get("name") or "",
        "public_key": public_key,
    }


async def _iter_json_array(response):
    """Yield the items of a JSON array body one at a time.

    The NordVPN server list is ~17 MB of JSON; ``json.loads`` would materialise
    every nested object at once (100+ MB of RSS). Decoding item by item keeps
    the peak at one chunk plus one server.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    index = 0
    async for chunk in response.content.iter_chunked(65536):
        buffer += chunk.decode("utf-8", "replace")
        while True:
            # Skip separators: the opening bracket, commas and whitespace.
            while index < len(buffer) and buffer[index] in " \r\n\t,[":
                index += 1
            if index >= len(buffer) or buffer[index] == "]":
                break
            try:
                item, end = decoder.raw_decode(buffer, index)
            except ValueError:
                if len(buffer) - index > 4 * 1024 * 1024:
                    raise TunnelError("NordVPN server list is malformed")
                break  # incomplete item: wait for the next chunk
            index = end
            yield item
        buffer = buffer[index:]
        index = 0


async def _fetch_servers_stream() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=15, sock_read=60)
    url = f"{NORD_SERVERS_URL}?limit=0&filters[servers_technologies][identifier]=wireguard_udp&{NORD_SERVERS_FIELDS}"
    servers: list[dict] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    body = await response.text()
                    raise TunnelError(
                        f"NordVPN API returned HTTP {response.status}: {body[:200]}"
                    )
                async for item in _iter_json_array(response):
                    if not isinstance(item, dict):
                        continue
                    server = _normalize_server(item)
                    if server:
                        servers.append(server)
    except TunnelError:
        raise
    except aiohttp.ClientError as exc:
        raise TunnelError(f"NordVPN API unreachable: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise TunnelError("NordVPN API request timed out") from exc
    return servers


async def fetch_servers(force: bool = False) -> list[dict]:
    """WireGuard-capable NordVPN servers, cached in memory while in use."""
    now = time.time()
    if not force and _server_cache["servers"] and now - _server_cache["fetched_at"] < SERVER_CACHE_TTL:
        _server_cache["used_at"] = now
        return _server_cache["servers"]

    async with _server_lock:
        now = time.time()
        if not force and _server_cache["servers"] and now - _server_cache["fetched_at"] < SERVER_CACHE_TTL:
            _server_cache["used_at"] = now
            return _server_cache["servers"]
        servers = await _fetch_servers_stream()
        if not servers:
            raise TunnelError("NordVPN returned no WireGuard servers")
        _server_cache.update({
            "servers": servers,
            "fetched_at": time.time(),
            "used_at": time.time(),
        })
        logger.info("NordVPN: cached %d WireGuard servers", len(servers))
        return servers


def touch_cache_if_loaded() -> None:
    """Keep the cached list alive while a panel keeps polling the status."""
    if _server_cache["servers"]:
        _server_cache["used_at"] = time.time()


def _trim_heap() -> None:
    """Best effort: hand freed arenas back to the OS (glibc only)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 - optional, platform specific
        pass


def purge_idle_cache() -> bool:
    """Free the cached server list once it has been idle for a while."""
    if not _server_cache["servers"]:
        return False
    if time.time() - _server_cache["used_at"] < SERVER_CACHE_IDLE:
        return False
    logger.info("NordVPN: releasing the cached server list (idle)")
    _server_cache.update({"servers": [], "fetched_at": 0.0, "used_at": 0.0})
    _trim_heap()
    return True


async def country_list() -> list[dict]:
    servers = await fetch_servers()
    countries: dict[str, dict] = {}
    for server in servers:
        code = server["country_code"]
        if not code:
            continue
        entry = countries.setdefault(code, {
            "code": code,
            "name": server["country"],
            "servers": 0,
            "load": 0,
        })
        entry["servers"] += 1
        entry["load"] += server["load"]
    result = list(countries.values())
    for entry in result:
        entry["load"] = round(entry["load"] / entry["servers"])
    return sorted(result, key=lambda item: item["name"])


async def find_server(hostname: str) -> dict:
    hostname = (hostname or "").strip().lower()
    if not hostname:
        raise TunnelError("Missing server hostname")
    for server in await fetch_servers():
        if server["hostname"].lower() == hostname:
            return server
    raise TunnelError(f"Unknown WireGuard server: {hostname}")


# --------------------------------------------------------------------------- #
# Slot actions
# --------------------------------------------------------------------------- #

async def connect_nordvpn(hostname: str) -> dict:
    _require_available()
    token = str(config_store.get("nordvpn_token", "") or "").strip()
    server = await find_server(hostname)
    private_key = await fetch_nordlynx_private_key(token)
    write_profile("nordvpn", build_nordvpn_profile(private_key, server))
    config_store.update({"nordvpn_server": server["hostname"], "nordvpn_enabled": True})
    await restart("nordvpn")
    return server


async def apply_custom_profile(text: str) -> dict:
    _require_available()
    profile = validate_profile(text)
    write_profile("custom", profile)
    config_store.update({"wg_custom_config": profile, "wg_custom_enabled": True})
    await restart("custom")
    return profile_summary(profile)


async def clear_custom_profile() -> None:
    """Stop custom WireGuard and remove its saved profile."""
    set_enabled("custom", False)
    try:
        await stop("custom")
    except TunnelError as exc:
        logger.warning("wireproxy stop for custom clear failed: %s", exc)
    if process_running("custom"):
        raise TunnelError("Custom WireGuard is still running; profile was not deleted")
    config_store.update({"wg_custom_config": "", "wg_custom_enabled": False})
    try:
        os.remove(profile_path("custom"))
    except FileNotFoundError:
        pass


def _require_available() -> None:
    if not available():
        raise TunnelError(
            "wireproxy is not available on this platform; run EasyProxy from the Docker image"
        )


async def disconnect(slot: str) -> None:
    set_enabled(slot, False)
    try:
        await stop(slot)
    except TunnelError as exc:
        logger.warning("wireproxy stop for %s failed: %s", slot, exc)


async def slot_status(slot: str, with_probe: bool = False) -> dict:
    path = profile_path(slot)
    summary: dict = {}
    profile_text = ""
    if os.path.exists(path):
        try:
            with open(path, "r") as handle:
                profile_text = handle.read()
        except OSError as exc:
            logger.warning("Cannot read %s: %s", path, exc)
    if profile_text:
        summary = profile_summary(profile_text)

    running = process_running(slot)
    status = {
        "slot": slot,
        "running": running,
        "pid": _read_pid(slot) if running else None,
        "bind": get_bind(slot),
        "enabled": is_enabled(slot),
        "has_profile": bool(profile_text),
        "available": available(),
        "profile": summary,
        "server": str(config_store.get("nordvpn_server", "") or "") if slot == "nordvpn" else "",
        "probe_ip": "",
        "token_set": bool(str(config_store.get("nordvpn_token", "") or "").strip()) if slot == "nordvpn" else False,
        "config": str(config_store.get("wg_custom_config", "") or "") if slot == "custom" else "",
    }
    if with_probe and running:
        status["probe_ip"] = await probe(slot)
    return status


async def status_all(with_probe: bool = False) -> dict:
    touch_cache_if_loaded()
    slots = {}
    for slot in SLOTS:
        try:
            slots[slot] = await slot_status(slot, with_probe=with_probe)
        except Exception as exc:  # noqa: BLE001 - status must never raise
            logger.exception("Tunnel status for %s failed", slot)
            slots[slot] = {"slot": slot, "running": False, "error": str(exc)}
    return {
        "available": available(),
        "slots": slots,
    }


async def ensure_running(slot: str) -> None:
    """Keep an enabled tunnel alive (called by the keepalive loop)."""
    if not available() or not is_enabled(slot):
        return
    if not os.path.exists(profile_path(slot)):
        logger.warning("Tunnel %s is enabled but has no profile; disabling it", slot)
        set_enabled(slot, False)
        return
    if process_running(slot):
        return
    try:
        output = await start(slot)
        logger.info("Tunnel %s started: %s", slot, output)
    except TunnelError as exc:
        logger.warning("Tunnel %s could not be started: %s", slot, exc)


_health_failures: dict[str, int] = {}


async def health_check_slot(slot: str) -> None:
    """Restart an enabled tunnel whose traffic stops flowing twice in a row."""
    if not is_enabled(slot) or not process_running(slot):
        _health_failures.pop(slot, None)
        return

    result = await check(slot, attempts=1)
    if result["ok"]:
        _health_failures.pop(slot, None)
        return

    failures = _health_failures.get(slot, 0) + 1
    _health_failures[slot] = failures
    logger.warning("Tunnel %s health check failed (%s/%s): %s",
                   slot, failures, HEALTH_FAILURES_BEFORE_RESTART, result["error"])
    if failures < HEALTH_FAILURES_BEFORE_RESTART:
        return

    _health_failures.pop(slot, None)
    try:
        await restart(slot)
        logger.info("Tunnel %s restarted after repeated health check failures", slot)
    except TunnelError as exc:
        logger.warning("Tunnel %s restart failed: %s", slot, exc)


_reconnect_locks: dict[str, asyncio.Lock] = {}
_last_reconnect_at: dict[str, float] = {}


async def _wait_for_bind(bind: str, timeout: float = 8.0) -> bool:
    """Wait until the SOCKS listener accepts TCP connections again."""
    host, _, port = bind.rpartition(":")
    try:
        port_num = int(port)
    except ValueError:
        return False
    host = _normalized_host(host) or "127.0.0.1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port_num), 1.0
            )
            writer.close()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.4)
    return False


async def reconnect(slot: str) -> bool:
    """Recover an enabled tunnel after a failed proxy request.

    Restarts the tunnel only when its egress is actually broken; a healthy
    tunnel means the failure came from upstream. Cooldown- and lock-guarded,
    so a burst of failing segment requests triggers a single restart.
    """
    if not available() or not is_enabled(slot):
        return False
    lock = _reconnect_locks.setdefault(slot, asyncio.Lock())
    if lock.locked():
        return False
    async with lock:
        now = time.monotonic()
        if now - _last_reconnect_at.get(slot, 0.0) < RECONNECT_COOLDOWN:
            return False
        _last_reconnect_at[slot] = now
        try:
            if process_running(slot):
                if (await check(slot, attempts=1))["ok"]:
                    return False
                await restart(slot)
            else:
                await start(slot)
            ready = await _wait_for_bind(get_bind(slot))
        except TunnelError as exc:
            logger.warning("Tunnel %s auto-reconnect failed: %s", slot, exc)
            return False
        if not ready:
            logger.warning("Tunnel %s restarted but its SOCKS bind is not accepting yet", slot)
            return False
        logger.warning("Tunnel %s auto-reconnected after a failed proxy request", slot)
        return True


async def keepalive_loop(interval: float = 30.0) -> None:
    tick = 0
    while True:
        try:
            purge_idle_cache()
            for slot in SLOTS:
                await ensure_running(slot)
            tick += 1
            if tick % HEALTH_CHECK_EVERY == 0:
                for slot in SLOTS:
                    await health_check_slot(slot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never kill the loop
            logger.exception("WireGuard tunnel keepalive failed")
        await asyncio.sleep(interval)
