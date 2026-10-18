"""Admin panel and API for the secondary WireGuard tunnels (NordVPN / custom)."""

import functools
import logging

from aiohttp import web

import config_store
from config import check_password
from services.proxy_shared import admin_guard as admin, json_response
from services import wg_tunnels
from services.proxy_shared import read_template

logger = logging.getLogger(__name__)

PAGES = {
    "nordvpn": ("/admin/nordvpn", "nordvpn.html"),
    "custom": ("/admin/wireguard", "wireguard.html"),
}


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

    @admin
    async def handle_status(request):
        with_probe = request.query.get("probe") in ("1", "true", "yes")
        return json_response(await wg_tunnels.status_all(with_probe=with_probe))

    @admin
    async def handle_save_token(request):
        token = str((await _payload(request)).get("token", "")).strip()
        if not token:
            config_store.set("nordvpn_token", "")
            return json_response({"status": "cleared"})
        try:
            private_key = await wg_tunnels.fetch_nordlynx_private_key(token)
            servers = await wg_tunnels.fetch_servers(force=True)
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        config_store.set("nordvpn_token", token)
        return json_response({
            "status": "ok",
            "private_key": private_key[:6] + "..." if private_key else "",
            "servers": len(servers),
        })

    @admin
    async def handle_countries(request):
        try:
            return json_response({"countries": await wg_tunnels.country_list()})
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=502)

    @admin
    async def handle_servers(request):
        country = (request.query.get("country") or "").strip().upper()
        needle = (request.query.get("q") or "").strip().lower()
        try:
            limit = min(int(request.query.get("limit") or 300), 1000)
        except ValueError:
            limit = 300
        try:
            servers = await wg_tunnels.fetch_servers()
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=502)

        matches = [
            server for server in servers
            if (not country or server["country_code"] == country)
            and (not needle or needle in server["hostname"].lower()
                 or needle in server["city"].lower())
        ]
        matches.sort(key=lambda item: (item["load"], item["hostname"]))
        return json_response({"total": len(matches), "servers": matches[:limit]})

    @admin
    async def handle_connect(request):
        hostname = str((await _payload(request)).get("server", "")).strip()
        try:
            server = await wg_tunnels.connect_nordvpn(hostname)
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({
            "status": "connected",
            "server": server["hostname"],
            "tunnel": await wg_tunnels.slot_status("nordvpn", with_probe=True),
        })

    @admin
    async def handle_custom(request):
        text = str((await _payload(request)).get("config", ""))
        try:
            summary = await wg_tunnels.apply_custom_profile(text)
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({
            "status": "connected",
            "profile": summary,
            "tunnel": await wg_tunnels.slot_status("custom", with_probe=True),
        })

    @admin
    async def handle_custom_clear(request):
        try:
            await wg_tunnels.clear_custom_profile()
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({
            "status": "cleared",
            "tunnel": await wg_tunnels.slot_status("custom"),
        })

    @admin
    async def handle_disconnect(request):
        slot = request.match_info["slot"]
        await wg_tunnels.disconnect(slot)
        return json_response({"status": "disconnected", "tunnel": await wg_tunnels.slot_status(slot)})

    @admin
    async def handle_reconnect(request):
        slot = request.match_info["slot"]
        # Reconnect means "keep it running": without the flag a later restart
        # would leave the tunnel off.
        wg_tunnels.set_enabled(slot, True)
        try:
            await wg_tunnels.restart(slot)
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({"status": "connected", "tunnel": await wg_tunnels.slot_status(slot, with_probe=True)})

    @admin
    async def handle_bind(request):
        slot = request.match_info["slot"]
        bind = str((await _payload(request)).get("bind", ""))
        try:
            wg_tunnels.set_bind(slot, bind)
            if wg_tunnels.is_enabled(slot):
                await wg_tunnels.restart(slot)
        except wg_tunnels.TunnelError as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({"status": "ok", "tunnel": await wg_tunnels.slot_status(slot)})

    @admin
    async def handle_logs(request, slot: str):
        return json_response({"logs": await wg_tunnels.logs(slot)})

    @admin
    async def handle_check(request):
        slot = request.match_info["slot"]
        return json_response(await wg_tunnels.check(slot))

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
