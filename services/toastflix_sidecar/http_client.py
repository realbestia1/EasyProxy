"""Small aiohttp client helper shared by the embedded Toastflix sidecar."""

from __future__ import annotations

from contextlib import asynccontextmanager

import aiohttp
from aiohttp_socks import ProxyConnector


def _proxy_settings(proxy: str):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None, None

    connector_url = proxy
    rdns = False
    if connector_url.startswith("socks5h://"):
        connector_url = connector_url.replace("socks5h://", "socks5://", 1)
        rdns = True
    elif connector_url.startswith("socks4a://"):
        connector_url = connector_url.replace("socks4a://", "socks4://", 1)
        rdns = True
    elif connector_url.startswith("socks4://"):
        rdns = False

    if connector_url.startswith(("socks5://", "socks4://")):
        return ProxyConnector.from_url(connector_url, rdns=rdns), None
    return None, proxy


@asynccontextmanager
async def client_session(proxy: str = "", timeout: float = 30):
    connector, request_proxy = _proxy_settings(proxy)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if connector is not None:
        kwargs["connector"] = connector
    async with aiohttp.ClientSession(**kwargs) as session:
        yield session, request_proxy


__all__ = ["client_session"]
