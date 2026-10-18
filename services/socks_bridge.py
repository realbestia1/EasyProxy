"""HTTP-to-SOCKS bridge for tools and runtimes that only support HTTP/HTTPS proxies.

Exposes a lightweight local asyncio HTTP CONNECT bridge that tunnels traffic
to an upstream SOCKS proxy (such as Cloudflare WARP or custom SOCKS5 routes).
"""

import asyncio
import logging
import urllib.parse
from typing import Dict, Optional

from python_socks.async_.asyncio import Proxy

logger = logging.getLogger(__name__)

_SOCKS_SCHEMES = ("socks5://", "socks5h://", "socks4://", "socks4a://")


def _normalize_socks_url(url: str) -> str:
    """python_socks rejects the socks5h/socks4a scheme spellings (it already
    forwards hostnames to the proxy), so map them to the plain scheme."""
    lower = url.lower()
    if lower.startswith("socks5h://"):
        return "socks5://" + url[len("socks5h://"):]
    if lower.startswith("socks4a://"):
        return "socks4://" + url[len("socks4a://"):]
    return url


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        # Half-close, do not close: closing a socket while the peer still has
        # unread data makes the stack send RST, which discards what we already
        # forwarded. handle_client closes both writers once both pipes are done.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


def _create_client_handler(upstream_socks_url: str):
    async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        # Bound before the try: the finally block closes it even when the
        # client disconnects before the upstream socket is created.
        up_writer = None
        try:
            req_line = await client_reader.readline()
            if not req_line:
                client_writer.close()
                return

            parts = req_line.decode("latin1").strip().split()
            if len(parts) < 2:
                client_writer.close()
                return

            method, target = parts[0].upper(), parts[1]

            # Read remaining headers
            headers = []
            while True:
                h = await client_reader.readline()
                if not h or h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)

            if method == "CONNECT":
                if ":" in target:
                    host, _, port_str = target.rpartition(":")
                    host = host.strip("[]")
                    port = int(port_str)
                else:
                    host, port = target.strip("[]"), 443

                proxy = Proxy.from_url(upstream_socks_url)
                upstream_sock = await proxy.connect(dest_host=host, dest_port=port)
                up_reader, up_writer = await asyncio.open_connection(sock=upstream_sock)

                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()

                await asyncio.gather(
                    _pipe(client_reader, up_writer),
                    _pipe(up_reader, client_writer),
                    return_exceptions=True,
                )
            else:
                u = urllib.parse.urlsplit(target)
                host = u.hostname or "127.0.0.1"
                port = u.port or (443 if u.scheme == "https" else 80)
                path = u.path or "/"
                if u.query:
                    path += "?" + u.query

                proxy = Proxy.from_url(upstream_socks_url)
                upstream_sock = await proxy.connect(dest_host=host, dest_port=port)
                up_reader, up_writer = await asyncio.open_connection(sock=upstream_sock)

                up_writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n".encode("latin1"))
                for h in headers:
                    up_writer.write(h)
                up_writer.write(b"\r\n")
                await up_writer.drain()

                await asyncio.gather(
                    _pipe(client_reader, up_writer),
                    _pipe(up_reader, client_writer),
                    return_exceptions=True,
                )
        except Exception as exc:
            logger.debug("Socks bridge error forwarding to %s: %s", upstream_socks_url, exc)
            try:
                client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await client_writer.drain()
            except Exception:
                pass
        finally:
            # Both directions are drained by now (or the bridge is aborting), so
            # a full close no longer truncates peer data.
            for stream in filter(None, (up_writer, client_writer)):
                try:
                    stream.close()
                    await stream.wait_closed()
                except Exception:
                    pass

    return handle_client


class SocksHttpBridgeManager:
    """Manages local HTTP proxy bridge servers for upstream SOCKS endpoints."""

    def __init__(self):
        self._servers: Dict[str, asyncio.Server] = {}
        self._ports: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get_bridge(self, proxy_url: Optional[str]) -> Optional[str]:
        if not proxy_url:
            return None

        clean_url = str(proxy_url).strip()
        lower_url = clean_url.lower()

        if lower_url.startswith(("http://", "https://")):
            return clean_url

        if not lower_url.startswith(_SOCKS_SCHEMES):
            return clean_url

        clean_url = _normalize_socks_url(clean_url)

        async with self._lock:
            if clean_url in self._ports:
                return f"http://127.0.0.1:{self._ports[clean_url]}"

            try:
                server = await asyncio.start_server(
                    _create_client_handler(clean_url),
                    "127.0.0.1",
                    0,
                )
                port = server.sockets[0].getsockname()[1]
                self._servers[clean_url] = server
                self._ports[clean_url] = port
                logger.info("Started HTTP-to-SOCKS bridge for %s on 127.0.0.1:%d", clean_url, port)
                return f"http://127.0.0.1:{port}"
            except Exception as exc:
                logger.error("Failed to start HTTP bridge for %s: %s", clean_url, exc)
                return None

    async def close_all(self):
        async with self._lock:
            for url, server in list(self._servers.items()):
                try:
                    server.close()
                    await server.wait_closed()
                except Exception:
                    pass
            self._servers.clear()
            self._ports.clear()


_BRIDGE_MANAGER = SocksHttpBridgeManager()


async def get_http_bridge_for_proxy(proxy_url: Optional[str]) -> Optional[str]:
    """Return an HTTP proxy URL for the given route, launching a bridge if proxy is SOCKS."""
    return await _BRIDGE_MANAGER.get_bridge(proxy_url)


async def close_socks_bridges():
    """Shutdown all active bridge servers."""
    await _BRIDGE_MANAGER.close_all()
