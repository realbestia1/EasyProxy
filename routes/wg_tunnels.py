"""Admin panel and API for the secondary WireGuard tunnels (NordVPN / custom)."""

import functools
import logging
import os

from aiohttp import web

import config_store
from config import APP_VERSION, check_password
from services import wg_tunnels
from services.proxy_shared import read_template

logger = logging.getLogger(__name__)

PAGES = {
    "nordvpn": ("/admin/nordvpn", "nordvpn.html"),
    "custom": ("/admin/wireguard", "wireguard.html"),
}


def _json(payload, status=200):
    return web.json_response(payload, status=status)


def _unauthorized():
    return _json({"error": "Unauthorized: Invalid API Password"}, status=401)


async def _payload(request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - malformed body
        return {}
    return data if isinstance(data, dict) else {}


def setup_wg_tunnel_routes(app: web.Application) -> None:
    """Register the tunnel panels and their JSON API."""

    async def handle_page(request, slot: str):
        if not check_password(request):
            raise web.HTTPFound("/admin/login")
        _, template = PAGES[slot]
        try:
            return web.Response(text=read_template(template), content_type="text/html")
        except FileNotFoundError:
            return web.Response(text=f"{template} not found", status=404)

    async def handle_status(request):
        if not check_password(request):
            return _unauthorized()
        with_probe = request.query.get("probe") in ("1", "true", "yes")
        return _json(await wg_tunnels.status_all(with_probe=with_probe))

    async def handle_save_token(request):
        if not check_password(request):
            return _unauthorized()
        token = str((await _payload(request)).get("token", "")).strip()
        if not token:
            config_store.set("nordvpn_token", "")
            return _json({"status": "cleared"})
        try:
            private_key = await wg_tunnels.fetch_nordlynx_private_key(token)
            servers = await wg_tunnels.fetch_servers(force=True)
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        config_store.set("nordvpn_token", token)
        return _json({
            "status": "ok",
            "private_key": private_key[:6] + "..." if private_key else "",
            "servers": len(servers),
        })

    async def handle_countries(request):
        if not check_password(request):
            return _unauthorized()
        try:
            return _json({"countries": await wg_tunnels.country_list()})
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=502)

    async def handle_servers(request):
        if not check_password(request):
            return _unauthorized()
        country = (request.query.get("country") or "").strip().upper()
        needle = (request.query.get("q") or "").strip().lower()
        try:
            limit = min(int(request.query.get("limit") or 300), 1000)
        except ValueError:
            limit = 300
        try:
            servers = await wg_tunnels.fetch_servers()
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=502)

        matches = [
            server for server in servers
            if (not country or server["country_code"] == country)
            and (not needle or needle in server["hostname"].lower()
                 or needle in server["city"].lower())
        ]
        matches.sort(key=lambda item: (item["load"], item["hostname"]))
        return _json({"total": len(matches), "servers": matches[:limit]})

    async def handle_connect(request):
        if not check_password(request):
            return _unauthorized()
        hostname = str((await _payload(request)).get("server", "")).strip()
        try:
            server = await wg_tunnels.connect_nordvpn(hostname)
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({
            "status": "connected",
            "server": server["hostname"],
            "tunnel": await wg_tunnels.slot_status("nordvpn", with_probe=True),
        })

    async def handle_custom(request):
        if not check_password(request):
            return _unauthorized()
        text = str((await _payload(request)).get("config", ""))
        try:
            summary = await wg_tunnels.apply_custom_profile(text)
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({
            "status": "connected",
            "profile": summary,
            "tunnel": await wg_tunnels.slot_status("custom", with_probe=True),
        })

    async def handle_custom_clear(request):
        if not check_password(request):
            return _unauthorized()
        try:
            await wg_tunnels.clear_custom_profile()
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({
            "status": "cleared",
            "tunnel": await wg_tunnels.slot_status("custom"),
        })

    async def handle_disconnect(request):
        if not check_password(request):
            return _unauthorized()
        slot = request.match_info["slot"]
        await wg_tunnels.disconnect(slot)
        return _json({"status": "disconnected", "tunnel": await wg_tunnels.slot_status(slot)})

    async def handle_reconnect(request):
        if not check_password(request):
            return _unauthorized()
        slot = request.match_info["slot"]
        # Reconnect means "keep it running": without the flag a later restart
        # would leave the tunnel off.
        wg_tunnels.set_enabled(slot, True)
        try:
            await wg_tunnels.restart(slot)
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({"status": "connected", "tunnel": await wg_tunnels.slot_status(slot, with_probe=True)})

    async def handle_bind(request):
        if not check_password(request):
            return _unauthorized()
        slot = request.match_info["slot"]
        bind = str((await _payload(request)).get("bind", ""))
        try:
            wg_tunnels.set_bind(slot, bind)
            if wg_tunnels.is_enabled(slot):
                await wg_tunnels.restart(slot)
        except wg_tunnels.TunnelError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({"status": "ok", "tunnel": await wg_tunnels.slot_status(slot)})

    async def handle_logs(request, slot: str):
        if not check_password(request):
            return _unauthorized()
        return _json({"logs": await wg_tunnels.logs(slot)})

    async def handle_check(request):
        if not check_password(request):
            return _unauthorized()
        slot = request.match_info["slot"]
        return _json(await wg_tunnels.check(slot))

    for slot, (path, _) in PAGES.items():
        app.router.add_get(path, functools.partial(handle_page, slot=slot))
        app.router.add_get(f"/api/admin/wg/{slot}/logs", functools.partial(handle_logs, slot=slot))

    app.router.add_get("/api/admin/wg/status", handle_status)
    app.router.add_post("/api/admin/wg/nordvpn/token", handle_save_token)
    app.router.add_get("/api/admin/wg/nordvpn/countries", handle_countries)
    app.router.add_get("/api/admin/wg/nordvpn/servers", handle_servers)
    app.router.add_post("/api/admin/wg/nordvpn/connect", handle_connect)
    app.router.add_post("/api/admin/wg/custom", handle_custom)
    app.router.add_post("/api/admin/wg/custom/clear", handle_custom_clear)
    app.router.add_post("/api/admin/wg/{slot}/disconnect", handle_disconnect)
    app.router.add_post("/api/admin/wg/{slot}/reconnect", handle_reconnect)
    app.router.add_post("/api/admin/wg/{slot}/bind", handle_bind)
    app.router.add_post("/api/admin/wg/{slot}/check", handle_check)


__all__ = ["setup_wg_tunnel_routes", "PAGES"]
