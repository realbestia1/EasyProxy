"""Cloudflare DNS for direct HTTP sessions, independent of the OS resolver."""
import ipaddress
import logging
import socket
import time

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

import config

logger = logging.getLogger(__name__)

_TTL = 300.0
_NEGATIVE_TTL = 10.0
_LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")
_NUMERIC_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
_cache: dict[tuple[str, int], tuple[float, list]] = {}
_fallback_warned = False
_installed = False


class CloudflareResolver(AbstractResolver):
    """Resolve direct hosts with the configured DNS, fall back to the OS."""

    def __init__(self, nameservers=None, lifetime: float = 4.0, loop=None):
        self._servers = tuple(nameservers or config.get_dns_servers())
        self._lifetime = lifetime
        self._fallback = ThreadedResolver(loop=loop)
        self._resolver = None
        if self._servers:
            try:
                import dns.asyncresolver

                resolver = dns.asyncresolver.Resolver(configure=False)
                resolver.nameservers = list(self._servers)
                resolver.timeout = min(2.0, lifetime)
                resolver.lifetime = lifetime
                self._resolver = resolver
            except Exception as exc:
                logger.warning("Cloudflare DNS unavailable, using the OS resolver: %s", exc)

    async def resolve(self, host, port=0, family=socket.AF_INET):
        # IP literals, localhost and local/service names stay local.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            resolved_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, resolved_family):
                raise OSError(f"No address for {host!r} in the requested family")
            return [self._result(host, host, port, resolved_family)]
        lowered = host.lower().rstrip(".")
        if (
            self._resolver is None
            or lowered == "localhost"
            or "." not in lowered
            or lowered.endswith(_LOCAL_SUFFIXES)
        ):
            return await self._fallback.resolve(host, port, family)

        key = (lowered, int(family))
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        types = []
        if family in (socket.AF_UNSPEC, socket.AF_INET):
            types.append(("A", socket.AF_INET))
        if family in (socket.AF_UNSPEC, socket.AF_INET6):
            types.append(("AAAA", socket.AF_INET6))

        results = []
        ttl = _TTL
        for record_type, resolved_family in types:
            try:
                answer = await self._resolver.resolve(host, record_type)
            except Exception:
                continue
            for record in answer:
                results.append(self._result(host, record.address, port, resolved_family))
            ttl = min(ttl, float(getattr(answer.rrset, "ttl", _TTL) or _TTL))

        if results:
            _cache[key] = (time.monotonic() + max(30.0, ttl), results)
            return results

        _cache[key] = (time.monotonic() + _NEGATIVE_TTL, [])
        global _fallback_warned
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "DNS %s failed for %s, falling back to the OS resolver",
                ",".join(self._servers), host,
            )
        return await self._fallback.resolve(host, port, family)

    @staticmethod
    def _result(host, address, port, family):
        return {
            "hostname": host,
            "host": address,
            "port": port,
            "family": family,
            "proto": socket.IPPROTO_TCP,
            "flags": _NUMERIC_FLAGS,
        }

    async def close(self):
        pass


def install() -> None:
    """Use Cloudflare DNS for every aiohttp TCPConnector created without a resolver."""
    global _installed
    if _installed or not config.get_dns_servers():
        return
    import aiohttp.connector

    aiohttp.connector.DefaultResolver = CloudflareResolver
    _installed = True
    logger.info("DNS: %s for direct traffic", ", ".join(config.get_dns_servers()))
