"""Local Tor client exposed as a SOCKS5 proxy."""

import asyncio
import ipaddress
import logging
import os
import sys
import re
import shutil
import signal
import stat
import subprocess
import time

import aiohttp
import psutil
from aiohttp_socks import ProxyConnector

import config_store

logger = logging.getLogger(__name__)

TOR_DATA_DIR = os.path.join(config_store.CONFIG_DIR, "tor")
TORRC_PATH = os.path.join(TOR_DATA_DIR, "torrc")
TOR_LOG_PATH = os.path.join(TOR_DATA_DIR, "tor.log")
TOR_CHECK_URL = "https://check.torproject.org/api/ip"
TOR_GEOIP_PATH = "/usr/share/tor/geoip"
TOR_CONTROL_HOST = "127.0.0.1"
TOR_CONTROL_PORT = 9051
TOR_BOOTSTRAP_TIMEOUT = 180
TOR_MAX_CIRCUIT_DIRTINESS = "30 days"

_BIND_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.\-\[\]:]+):(?P<port>\d{1,5})$")
_FINGERPRINT_RE = re.compile(r"^\$?[0-9A-Fa-f]{40}$")
_process: asyncio.subprocess.Process | None = None
_bootstrap_level: int | None = None
_exclude_nodes = ""
_lock = asyncio.Lock()


class TorError(Exception):
    """Raised for user-facing Tor errors."""


def _tor_binary() -> str | None:
    found = shutil.which("tor")
    if found:
        return found
    from pathlib import Path
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            bases.append(Path(sys._MEIPASS))
    else:
        bases.append(Path(__file__).resolve().parent.parent)

    # On Windows an active CTF/antivirus component blocks any process whose
    # executable is literally named "tor.exe" (TLS handshakes abort during the
    # relay handshake), while the identical binary runs fine under any other
    # name. The bundled binary therefore ships as "torclient.exe" first.
    exe_names = ("torclient.exe", "tor.exe") if os.name == "nt" else ("tor",)
    for base in bases:
        for exe_name in exe_names:
            for candidate in (
                base / "bin" / exe_name,
                base / exe_name,
                base / "tor" / exe_name,
                base / "tor" / "tor" / exe_name,
            ):
                if candidate.is_file():
                    return str(candidate)
    return None


def _lyrebird_binary() -> str | None:
    found = shutil.which("lyrebird")
    if found:
        return found
    from pathlib import Path
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            bases.append(Path(sys._MEIPASS))
    else:
        bases.append(Path(__file__).resolve().parent.parent)

    for base in bases:
        for candidate in (
            base / "bin" / ("lyrebird.exe" if os.name == "nt" else "lyrebird"),
            base / ("lyrebird.exe" if os.name == "nt" else "lyrebird"),
            base / "tor" / "pluggable_transports" / ("lyrebird.exe" if os.name == "nt" else "lyrebird"),
        ):
            if candidate.is_file():
                return str(candidate).replace("\\", "/")
    return None


def _resolve_geoip_files() -> tuple[str | None, str | None]:
    from pathlib import Path
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            bases.append(Path(sys._MEIPASS))
    else:
        bases.append(Path(__file__).resolve().parent.parent)
    bases.append(Path(config_store.CONFIG_DIR))

    for base in bases:
        for folder in (base / "data", base):
            v4 = folder / "geoip"
            v6 = folder / "geoip6"
            if v4.is_file():
                return str(v4).replace("\\", "/"), str(v6).replace("\\", "/") if v6.is_file() else None
    if os.path.isfile("/usr/share/tor/geoip"):
        return "/usr/share/tor/geoip", "/usr/share/tor/geoip6" if os.path.isfile("/usr/share/tor/geoip6") else None
    return None, None


def available() -> bool:
    return bool(_tor_binary())


def get_bind() -> str:
    return str(config_store.get("tor_bind", "127.0.0.1:9050") or "").strip()


def set_bind(value: str) -> str:
    bind = (value or "").strip()
    match = _BIND_RE.match(bind)
    if not match or not 1 <= int(match.group("port")) <= 65535:
        raise TorError(f"Invalid bind address: {value!r} (expected host:port)")
    host = match.group("host").strip("[]").lower()
    if host != "localhost":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise TorError("TorProxy bind must use loopback (127.0.0.1, ::1 or localhost)")
    config_store.set("tor_bind", bind)
    return bind


_EXIT_NODES_RE = re.compile(r"^[A-Za-z0-9${},~=_.\-]+$")


def get_exit_nodes() -> str:
    return str(config_store.get("tor_exit_nodes", "") or "").strip()


def set_exit_nodes(value: str) -> str:
    nodes = (value or "").strip()
    if nodes and (len(nodes) > 200 or not _EXIT_NODES_RE.match(nodes)):
        raise TorError(f"Invalid exit nodes: {value!r}")
    config_store.set("tor_exit_nodes", nodes)
    return nodes


def get_exit_country() -> str:
    return str(config_store.get("tor_exit_country", "") or "").strip().lower()


def set_exit_country(value: str) -> str:
    country = (value or "").strip().lower().strip("{}")
    if country and not re.fullmatch(r"[a-z]{2}", country):
        raise TorError(f"Invalid country code: {value!r}")
    config_store.set("tor_exit_country", country)
    return country


def is_enabled() -> bool:
    return bool(config_store.get("tor_enabled", False))


def set_enabled(value: bool) -> None:
    config_store.set("tor_enabled", bool(value))


def _pid() -> int | None:
    return _process.pid if _process and _process.returncode is None else None


def _split_bind(bind: str) -> tuple[str, int]:
    match = _BIND_RE.match(bind)
    if not match:
        raise TorError(f"Invalid bind address: {bind!r} (expected host:port)")
    host = match.group("host").strip("[]")
    return host, int(match.group("port"))


def _tor_identity() -> tuple[int | None, int | None]:
    """Return the Debian Tor uid/gid when the app can prepare them."""
    if os.name == "nt" or getattr(os, "geteuid", lambda: 1)() != 0:
        return None, None
    try:
        import pwd
        account = pwd.getpwnam("debian-tor")
    except (ImportError, KeyError):
        return None, None
    # Use the account's primary gid; the group name is not guaranteed to
    # match the username on every VPS image.
    return account.pw_uid, account.pw_gid


def _repair_tor_data_permissions(tor_uid: int, tor_gid: int) -> None:
    """Make the bind-mounted Tor state readable/writable by debian-tor."""
    parent_dir = os.path.dirname(os.path.abspath(TOR_DATA_DIR))
    parent_mode = stat.S_IMODE(os.stat(parent_dir).st_mode)
    # Tor only needs to traverse the /data mount; keep its contents private.
    os.chmod(parent_dir, parent_mode | stat.S_IXOTH)
    os.makedirs(TOR_DATA_DIR, exist_ok=True)
    os.chown(TOR_DATA_DIR, tor_uid, tor_gid)
    os.chmod(TOR_DATA_DIR, 0o700)
    for root, dirs, files in os.walk(TOR_DATA_DIR):
        for name in dirs:
            path = os.path.join(root, name)
            os.chown(path, tor_uid, tor_gid)
            os.chmod(path, 0o700)
        for name in files:
            path = os.path.join(root, name)
            os.chown(path, tor_uid, tor_gid)
            os.chmod(path, 0o600)


def _format_torrc_path(path: str) -> str:
    if os.name == "nt":
        p = os.path.abspath(path)
        try:
            import ctypes
            # GetShortPathNameW only works on existing paths: shorten the parent dir
            # (files like tor.log may not exist yet), then re-append the name.
            parent, name = os.path.split(p)
            os.makedirs(parent, exist_ok=True)
            buf = ctypes.create_unicode_buffer(500)
            if ctypes.windll.kernel32.GetShortPathNameW(parent, buf, 500) and " " not in name:
                return f"{buf.value}\\{name}".replace("\\", "/")
        except Exception:
            pass
        escaped = p.replace("\\", "\\\\")
        return f'"{escaped}"' if " " in escaped else escaped
    return path

def _write_torrc() -> None:
    tor_uid, tor_gid = _tor_identity()
    run_as_debian_tor = tor_uid is not None and tor_gid is not None
    if run_as_debian_tor:
        try:
            _repair_tor_data_permissions(tor_uid, tor_gid)
        except OSError as exc:
            raise TorError(
                f"Cannot set permissions on {TOR_DATA_DIR} for debian-tor: {exc}"
            ) from exc
    else:
        os.makedirs(TOR_DATA_DIR, exist_ok=True)
    host, port = _split_bind(get_bind())
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    lines = [
        f"SocksPort {host}:{port}",
        f"DataDirectory {_format_torrc_path(TOR_DATA_DIR)}",
        "ClientOnly 1",
        "AvoidDiskWrites 0",
        "ConfluxEnabled 0",
        "DormantOnFirstStartup 0",
        "DormantCanceledByStartup 1",
        f"MaxCircuitDirtiness {TOR_MAX_CIRCUIT_DIRTINESS}",
    ]
    geoip4, geoip6 = _resolve_geoip_files()
    if geoip4:
        lines.append(f"GeoIPFile {_format_torrc_path(geoip4)}")
    if geoip6:
        lines.append(f"GeoIPv6File {_format_torrc_path(geoip6)}")

    exit_nodes = get_exit_nodes()
    if exit_nodes:
        if "{" in exit_nodes and not geoip4 and not os.path.exists(TOR_GEOIP_PATH):
            raise TorError(
                "Tor GeoIP database is missing (install tor-geoipdb); "
                "cannot select an exit country"
            )
        # Pin the exit so the egress IP never changes between circuits.
        lines.append(f"ExitNodes {exit_nodes}")
        lines.append("StrictNodes 1")
    if _exclude_nodes:
        lines.append(f"ExcludeNodes {_exclude_nodes}")
    lines += [
        f"ControlPort {TOR_CONTROL_HOST}:{TOR_CONTROL_PORT}",
        "CookieAuthentication 1",
        f"CookieAuthFile {_format_torrc_path(os.path.join(TOR_DATA_DIR, 'control_auth_cookie'))}",
        f"Log notice file {_format_torrc_path(TOR_LOG_PATH)}",
    ]
    lyrebird = _lyrebird_binary()
    use_bridges = bool(config_store.get("tor_use_bridges", False))
    if use_bridges and lyrebird:
        lines.append("UseBridges 1")
        lyrebird_cmd = _format_torrc_path(lyrebird)
        lines.append(f"ClientTransportPlugin snowflake exec {lyrebird_cmd}")
        lines.append("Bridge snowflake 192.0.2.3:80 2B280B23E1107BB62ABFC40DDCC8824814F80A72 fingerprint=2B280B23E1107BB62ABFC40DDCC8824814F80A72 url=https://1098762253.rsc.cdn77.org/ fronts=app.datapacket.com,www.datapacket.com ice=stun:stun.epygi.com:3478,stun:stun.uls.co.za:3478,stun:stun.voipgate.com:3478,stun:stun.mixvoip.com:3478,stun:stun.telnyx.com:3478,stun:stun.hot-chilli.net:3478,stun:stun.fitauto.ru:3478,stun:stun.m-online.net:3478 utls-imitate=hellorandomizedalpn")
        lines.append("Bridge snowflake 192.0.2.4:80 8838024498816A039FCBBAB14E6F40A0843051FA fingerprint=8838024498816A039FCBBAB14E6F40A0843051FA url=https://1098762253.rsc.cdn77.org/ fronts=app.datapacket.com,www.datapacket.com ice=stun:stun.epygi.com:3478,stun:stun.uls.co.za:3478,stun:stun.voipgate.com:3478,stun:stun.mixvoip.com:3478,stun:stun.telnyx.com:3478,stun:stun.hot-chilli.net:3478,stun:stun.fitauto.ru:3478,stun:stun.m-online.net:3478 utls-imitate=hellorandomizedalpn")
    # Debian's package user prevents Tor from running as root in Docker.
    if run_as_debian_tor:
        lines.append("User debian-tor")
    with open(TORRC_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        if run_as_debian_tor:
            os.chown(TORRC_PATH, tor_uid, tor_gid)
        os.chmod(TORRC_PATH, 0o600)
    except OSError as exc:
        raise TorError(f"Cannot set permissions on {TORRC_PATH}: {exc}") from exc


async def _port_ready(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _control_command(reader, writer, command: str) -> None:
    writer.write((command + "\r\n").encode("ascii"))
    await writer.drain()
    for _ in range(32):
        line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("utf-8", "replace").strip()
        if line.startswith("250 "):
            return
        if line.startswith("4") or line.startswith("5"):
            raise TorError(line)
    raise TorError("Unexpected Tor control response")


async def _control_lines(command: str) -> list[str]:
    """Run a read-only control command and return its (unprefixed) response lines."""
    cookie_path = os.path.join(TOR_DATA_DIR, "control_auth_cookie")
    try:
        with open(cookie_path, "rb") as handle:
            cookie = handle.read()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=5
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TorError("Tor control port is unavailable") from exc
    try:
        await _control_command(reader, writer, f"AUTHENTICATE {cookie.hex()}")
        writer.write((command + "\r\n").encode("ascii"))
        await writer.drain()
        lines = []
        in_data = False
        for _ in range(4096):
            line = (await asyncio.wait_for(reader.readline(), timeout=10)).decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                else:
                    lines.append(line)
                continue
            if line == "250 OK":
                return lines
            if line.startswith("250+"):
                in_data = True
            elif line.startswith("250-"):
                lines.append(line[4:])
            elif line.startswith("250 "):
                lines.append(line[4:])
            elif line.startswith("4") or line.startswith("5"):
                raise TorError(line)
        raise TorError("Unexpected Tor control response")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _bootstrap_progress() -> int | None:
    """Return Tor's bootstrap percentage, or None while the control port is not ready."""
    cookie_path = os.path.join(TOR_DATA_DIR, "control_auth_cookie")
    try:
        with open(cookie_path, "rb") as handle:
            cookie = handle.read()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=5
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        await _control_command(reader, writer, f"AUTHENTICATE {cookie.hex()}")
        writer.write(b"GETINFO status/bootstrap-phase\r\n")
        await writer.drain()
        for _ in range(64):
            line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("utf-8", "replace")
            match = re.search(r"PROGRESS=(\d+)", line)
            if match:
                return int(match.group(1))
        return None
    except (OSError, asyncio.TimeoutError, TorError):
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _control_pid() -> int | None:
    """Return the PID of the Tor process serving the control port."""
    try:
        for line in await _control_lines("GETINFO process/pid"):
            if line.startswith("process/pid="):
                return int(line.split("=", 1)[1])
    except (TorError, ValueError):
        return None
    return None


def _log_tail(lines: int = 24) -> str:
    try:
        with open(TOR_LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            content = "".join(handle.readlines()[-lines:]).strip()
    except OSError:
        return "No Tor log output."
    return content or "No Tor log output."


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if not process or process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.kill()
        else:
            process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def _process_output(process: asyncio.subprocess.Process) -> str:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return ""
    chunks = []
    for payload in (stderr, stdout):
        if payload:
            text = payload.decode("utf-8", "replace").strip()
            if text:
                chunks.append(text)
    return " | ".join(chunks)


async def _verify_config() -> None:
    tor_cmd = _tor_binary() or "tor"
    process = await asyncio.create_subprocess_exec(
        tor_cmd, "--verify-config", "-f", TORRC_PATH,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise TorError("Tor configuration verification timed out")
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        raise
    output = " ".join(
        part.decode("utf-8", "replace").strip()
        for part in (stderr, stdout)
        if part and part.decode("utf-8", "replace").strip()
    )
    if process.returncode != 0:
        raise TorError(f"Invalid Tor configuration (code {process.returncode}): {output or 'no output'}")


async def new_identity() -> None:
    """Switch to a fresh exit, keeping any country selection.

    With a country preference the new relay is chosen inside that country and
    pinned, excluding the relay currently pinned so the egress IP really
    changes. A single-relay pin without a country is released as before. A
    restart is required: SIGNAL NEWNYM alone leaves the old circuits alive
    (MaxCircuitDirtiness is 30 days) and they keep serving new streams, so the
    previous IP can come back.
    """
    if _pid() is None:
        raise TorError("Tor is not running")
    country = get_exit_country()
    if country:
        current = get_exit_nodes()
        exclude = f"${current.lstrip('$')}" if _FINGERPRINT_RE.fullmatch(current or "") else ""
        await _pin_country_exit(country, exclude=exclude)
        return
    selection = get_exit_nodes()
    if selection and "{" not in selection and "," not in selection:
        set_exit_nodes("")
    await restart()


def _country_selection(value: str) -> str | None:
    match = re.fullmatch(r"\{([A-Za-z]{2})\}", (value or "").strip())
    return match.group(1).lower() if match else None


async def _pin_country_exit(country: str, exclude: str = "") -> None:
    """Let Tor pick an exit in the country, then pin the relay it picked.

    The country stays stored as preference; the fingerprint pin keeps the
    egress IP fixed until the next new-identity request.
    """
    global _exclude_nodes
    set_exit_nodes("{" + country + "}")
    _exclude_nodes = exclude
    try:
        await restart()
        fingerprint = await current_exit_fingerprint()
    finally:
        _exclude_nodes = ""
    set_exit_nodes(fingerprint)
    await restart()


async def apply_exit_nodes(value: str) -> None:
    """Apply an exit selection, rolling back to the previous one on failure.

    A single country code (`{it}`) picks and pins an exit relay in that
    country. Anything else (fingerprint, nickname, set, empty) is written to
    ExitNodes as-is. A failed selection can leave Tor unable to bootstrap:
    restoring the previous one keeps a working Tor instead of leaving it
    stopped.
    """
    value = (value or "").strip()
    previous_nodes = get_exit_nodes()
    previous_country = get_exit_country()
    country = _country_selection(value)
    try:
        if country:
            set_exit_country(country)
            await _pin_country_exit(country)
        elif value:
            set_exit_country("")
            set_exit_nodes(value)
            await restart()
        else:
            set_exit_country("")
            set_exit_nodes("")
            await restart()
    except TorError as exc:
        set_exit_nodes(previous_nodes)
        set_exit_country(previous_country)
        message = str(exc)
        try:
            await start()
            message += "; previous exit selection restored"
        except TorError as rollback_exc:
            message += f"; rollback failed: {rollback_exc}"
        raise TorError(message) from exc


async def _pin_current_exit() -> None:
    """Pin the exit Tor is currently using, then rebuild every circuit on it."""
    fingerprint = await current_exit_fingerprint()
    set_exit_nodes(fingerprint)
    await restart()


async def _pin_default_exit() -> None:
    """Pin an exit according to the stored country preference."""
    country = get_exit_country()
    if country:
        await _pin_country_exit(country)


def _stale_tor_pids() -> list[int]:
    """Visible Tor processes holding our data directory or configured ports."""
    try:
        socks_port = _split_bind(get_bind())[1]
    except TorError:
        return []
    ports = {socks_port, TOR_CONTROL_PORT}
    candidates: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.pid
                and conn.laddr
                and conn.laddr.port in ports
            ):
                candidates.add(conn.pid)
    except (psutil.Error, OSError):
        pass
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            # The bundled tor binary ships as torclient.exe because a Windows
            # security component blocks processes literally named "tor.exe".
            if (proc.info["name"] or "").lower() not in ("tor", "tor.exe", "torclient.exe"):
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if proc.info["pid"] in candidates or TORRC_PATH in cmdline:
                candidates.add(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    skip = {os.getpid(), _process.pid if _process else -1}
    pids = []
    for pid in candidates - skip:
        try:
            if (psutil.Process(pid).name() or "").lower() in ("tor", "tor.exe", "torclient.exe"):
                pids.append(pid)
        except (psutil.Error, OSError):
            continue
    return pids


async def _terminate_stale_tor() -> list[int]:
    """Stop orphan Tor instances that block our data directory or ports."""
    pids = await asyncio.to_thread(_stale_tor_pids)
    victims = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.kill()
            victims.append(pid)
        except (psutil.Error, OSError):
            continue
    if not victims:
        return []
    await asyncio.sleep(3)
    for pid in victims:
        try:
            proc = psutil.Process(pid)
            if proc.is_running():
                proc.kill()
        except (psutil.Error, OSError):
            continue
    return victims


def _data_dir_locked() -> bool:
    """True when another Tor instance holds the data-directory lock."""
    lock_path = os.path.join(TOR_DATA_DIR, "lock")
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.lockf(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


async def _start() -> None:
    global _process, _bootstrap_level
    async with _lock:
        if _process is not None:
            if _process.returncode is None:
                return
            _process = None
        if not available():
            raise TorError("Tor is not installed; use the EasyProxy Docker image")
        set_bind(get_bind())
        await _terminate_stale_tor()
        _write_torrc()
        await _verify_config()
        host, port = _split_bind(get_bind())
        deadline = time.monotonic() + TOR_BOOTSTRAP_TIMEOUT
        ready = False
        stale_waits = 0
        process = None
        try:
            while time.monotonic() < deadline:
                tor_cmd = _tor_binary() or "tor"
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = await asyncio.create_subprocess_exec(
                    tor_cmd, "-f", TORRC_PATH, "--RunAsDaemon", "0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                )
                _process = process
                _bootstrap_level = None
                respawn = False
                while time.monotonic() < deadline:
                    if process.returncode is not None:
                        _process = None
                        output = await _process_output(process)
                        detail = " | ".join(part for part in (output, _log_tail()) if part and part != "No Tor log output.")
                        # A previous container/app instance may still be shutting
                        # down and hold the ports or the data directory lock.
                        stale_instance = any(
                            marker in detail
                            for marker in (
                                "same data directory",
                                "Address already in use. Is Tor already running?",
                            )
                        )
                        if stale_instance and time.monotonic() + 5 < deadline:
                            killed = await _terminate_stale_tor()
                            if killed:
                                logger.warning(
                                    "Terminated stale Tor process(es) %s holding the proxy; retrying",
                                    killed,
                                )
                            elif await asyncio.to_thread(_data_dir_locked):
                                stale_waits += 1
                                logger.warning(
                                    "Tor data directory %s is locked by another process; waiting for it to exit",
                                    TOR_DATA_DIR,
                                )
                                wait_until = min(deadline, time.monotonic() + 10)
                                while time.monotonic() < wait_until and await asyncio.to_thread(_data_dir_locked):
                                    await asyncio.sleep(1)
                                if await asyncio.to_thread(_data_dir_locked):
                                    if stale_waits >= 3:
                                        raise TorError(
                                            f"Tor data directory {TOR_DATA_DIR} is locked by another instance "
                                            "(another EasyProxy process or container sharing the same folder). "
                                            "Close it and retry."
                                        )
                                    await asyncio.sleep(2)
                            else:
                                await asyncio.sleep(5)
                            respawn = True
                            break
                        raise TorError(
                            f"Tor exited during startup (code {process.returncode}): {detail or 'no Tor output'}"
                        )
                    if await _port_ready(host, port):
                        try:
                            cookie_path = os.path.join(TOR_DATA_DIR, "control_auth_cookie")
                            if os.path.isfile(cookie_path):
                                with open(cookie_path, "rb") as h:
                                    cookie = h.read().hex()
                                reader, writer = await asyncio.wait_for(
                                    asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=2
                                )
                                await _control_command(reader, writer, f"AUTHENTICATE {cookie}")
                                await _control_command(reader, writer, "SIGNAL ACTIVE")
                                writer.close()
                                await writer.wait_closed()
                        except Exception:
                            pass
                        level = await _bootstrap_progress()
                        if level is not None:
                            _bootstrap_level = level
                        # A dying/foreign Tor instance can hold the ports while
                        # ours waits on the data directory lock.
                        if level == 100 and await _control_pid() == process.pid:
                            logger.info("Tor SOCKS5 ready and bootstrapped on %s:%s", host, port)
                            ready = True
                            return
                    await asyncio.sleep(1)
                if not respawn:
                    break
            raise TorError(f"Tor did not bootstrap in time: {_log_tail()}")
        finally:
            if not ready:
                _bootstrap_level = None
                if _process is process:
                    _process = None
                await _terminate(process)
            elif _process is process and process.returncode is not None:
                _process = None


async def start() -> None:
    """Start Tor and pin its exit automatically when a country is selected."""
    await _start()
    if get_exit_country() and not get_exit_nodes():
        try:
            await _pin_default_exit()
        except TorError as exc:
            logger.debug("Could not auto-pin a Tor exit: %s", exc)


async def stop() -> None:
    global _process, _bootstrap_level
    async with _lock:
        process = _process
        _process = None
        _bootstrap_level = None
        await _terminate(process)


async def restart() -> None:
    await stop()
    await start()


async def check() -> dict:
    result = {"ok": False, "egress_ip": "", "is_tor": False, "http_ms": None, "error": ""}
    if _pid() is None:
        result["error"] = "Tor is not running"
        return result
    connector = ProxyConnector.from_url(f"socks5://{get_bind()}", rdns=True)
    started = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(TOR_CHECK_URL) as response:
                payload = await response.json(content_type=None)
        result["http_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["egress_ip"] = str(payload.get("IP", ""))
        result["is_tor"] = bool(payload.get("IsTor"))
        result["ok"] = result["is_tor"] and bool(result["egress_ip"])
        if not result["ok"]:
            result["error"] = "The connection did not reach the Tor network"
    except asyncio.TimeoutError:
        result["error"] = "Tor circuit building timed out"
    except Exception as exc:  # noqa: BLE001 - surfaced in admin panel
        result["error"] = str(exc)
    return result


_NS_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


async def _fingerprint_for_ip(egress_ip: str) -> str:
    fingerprints = []
    for line in await _control_lines("GETINFO circuit-status"):
        if " BUILT " not in line:
            continue
        path = next((part for part in line.split() if part.startswith("$")), "")
        if path:
            fingerprints.append(path.split(",")[-1].split("~")[0].lstrip("$"))
    for fingerprint in dict.fromkeys(fingerprints):
        try:
            ns_lines = await _control_lines(f"GETINFO ns/id/{fingerprint}")
        except TorError:
            continue
        for ns_line in ns_lines:
            if ns_line.startswith("r ") and egress_ip in _NS_IP_RE.findall(ns_line):
                return fingerprint
    raise TorError(f"Could not map exit IP {egress_ip} to a relay fingerprint")


async def current_exit_fingerprint() -> str:
    """Resolve the relay fingerprint currently used as exit, to pin it."""
    result = await check()
    egress_ip = result.get("egress_ip", "")
    if not egress_ip:
        raise TorError(result.get("error") or "Tor is not reachable")
    return await _fingerprint_for_ip(egress_ip)


async def logs(lines: int = 120) -> str:
    return _log_tail(lines)


async def status(with_probe: bool = False) -> dict:
    data = {
        "running": _pid() is not None,
        "pid": _pid(),
        "bind": get_bind(),
        "enabled": is_enabled(),
        "available": available(),
        "automatic_rotation": False,
        "exit_nodes": get_exit_nodes(),
        "exit_country": get_exit_country(),
        "bootstrap": _bootstrap_level,
        "probe_ip": "",
    }
    if with_probe and data["running"]:
        result = await check()
        data["probe_ip"] = result.get("egress_ip", "")
    return data


async def ensure_running() -> None:
    if available() and is_enabled() and _pid() is None:
        try:
            await start()
        except TorError as exc:
            logger.warning("Tor could not be started: %s", exc)
            return
    if _pid() is not None and get_exit_country() and not get_exit_nodes():
        try:
            await _pin_default_exit()
        except TorError as exc:
            logger.debug("Could not pin a Tor exit: %s", exc)


async def keepalive_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await ensure_running()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never kill the loop
            logger.exception("Tor keepalive failed")
        await asyncio.sleep(interval)


__all__ = [
    "TorError", "available", "get_bind", "set_bind", "is_enabled", "set_enabled",
    "get_exit_nodes", "set_exit_nodes", "get_exit_country", "set_exit_country",
    "apply_exit_nodes", "current_exit_fingerprint",
    "start", "stop", "restart", "new_identity", "check", "logs", "status", "keepalive_loop",
]
