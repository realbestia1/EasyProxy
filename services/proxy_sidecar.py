"""Reverse proxy for the embedded Toastflix aiohttp sidecar."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from services.sidecar_manager import SidecarManager


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class HLSProxySidecarMixin:
    """Forward ``/sidecar`` requests to the private aiohttp process."""

    def _init_sidecar_proxy(self, manager: "SidecarManager") -> None:
        self.sidecar_manager = manager
        self._sidecar_session: aiohttp.ClientSession | None = None

    async def _get_sidecar_session(self) -> aiohttp.ClientSession:
        session = getattr(self, "_sidecar_session", None)
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    connect=10,
                    sock_connect=10,
                    sock_read=None,
                ),
                connector=aiohttp.TCPConnector(limit=100),
                auto_decompress=False,
            )
            self._sidecar_session = session
        return session

    async def _proxy_sidecar_request(
        self,
        request: web.Request,
        external_prefix: str,
    ) -> web.StreamResponse:
        manager = getattr(self, "sidecar_manager", None)
        if manager is None:
            return web.json_response(
                {"detail": "Toastflix sidecar is not available"}, status=503
            )

        request_started = False
        try:
            await manager.begin_request()
            request_started = True
        except Exception as exc:
            return web.json_response(
                {"detail": f"Toastflix sidecar is not available: {exc}"},
                status=503,
            )

        try:
            path, _, raw_query = request.raw_path.partition("?")
            if external_prefix and path == external_prefix:
                sidecar_path = "/"
            elif external_prefix and path.startswith(f"{external_prefix}/"):
                sidecar_path = path[len(external_prefix):] or "/"
            else:
                # This branch is only a defensive fallback for direct unit calls.
                sidecar_path = path or "/"
                if external_prefix:
                    tail = request.match_info.get("tail", "")
                    sidecar_path = f"/{tail}" if tail else "/"
                    raw_query = request.query_string
            target_url = manager.target_url(sidecar_path, raw_query)

            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in _HOP_BY_HOP_HEADERS
                and key.lower() not in {"host", "content-length"}
            }
            remote = request.remote or ""
            existing_forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
            forwarded_for = ", ".join(item for item in (existing_forwarded_for, remote) if item)
            if forwarded_for:
                headers["X-Forwarded-For"] = forwarded_for
            forwarded_host = (
                request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
                or request.host
            )
            forwarded_proto = (
                request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
                or request.scheme
            )
            # The sidecar uses the actual Host header for its public base URL. Keep the
            # public host here, otherwise generated audio URLs expose 127.0.0.1
            # and the child's random internal port to Toastflix.
            headers["Host"] = forwarded_host
            headers["X-Forwarded-Host"] = forwarded_host
            headers["X-Forwarded-Proto"] = forwarded_proto
            headers["X-Forwarded-Prefix"] = external_prefix

            body = await request.read()
            session = await self._get_sidecar_session()
            async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ) as upstream:
                downstream = web.StreamResponse(status=upstream.status, reason=upstream.reason)
                for key, value in upstream.headers.items():
                    if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                        downstream.headers.add(key, value)
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {"detail": f"Toastflix sidecar request failed: {exc}"}, status=502
            )
        finally:
            if request_started:
                await manager.end_request()

    async def handle_sidecar_request(self, request: web.Request) -> web.StreamResponse:
        """Handle the backwards-compatible ``/sidecar/*`` namespace."""
        return await self._proxy_sidecar_request(request, "/sidecar")

    async def handle_sidecar_root_request(self, request: web.Request) -> web.StreamResponse:
        """Handle Toastflix clients configured with EasyProxy's base URL."""
        return await self._proxy_sidecar_request(request, "")

    async def cleanup(self):
        session = getattr(self, "_sidecar_session", None)
        if session is not None and not session.closed:
            await session.close()
        await super().cleanup()


__all__ = ["HLSProxySidecarMixin"]
