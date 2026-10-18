"""Admin panel and API for the local Tor SOCKS5 proxy."""

import functools

from aiohttp import web

from config import check_password
from services.proxy_shared import admin_guard as admin, json_response
from services import tor_proxy
from services.proxy_shared import read_template


def setup_tor_proxy_routes(app: web.Application) -> None:
    async def page(request):
        if not check_password(request):
            raise web.HTTPFound("/admin/login")
        return web.Response(text=read_template("torproxy.html"), content_type="text/html")

    @admin
    async def status(request):
        return json_response(await tor_proxy.status(with_probe=request.query.get("probe") in ("1", "true", "yes")))

    @admin
    async def action(request, name):
        try:
            if name == "start":
                tor_proxy.set_enabled(True)
                await tor_proxy.start()
            elif name == "stop":
                tor_proxy.set_enabled(False)
                await tor_proxy.stop()
            else:
                tor_proxy.set_enabled(True)
                await tor_proxy.restart()
            return json_response({"status": name, "tor": await tor_proxy.status(with_probe=True)})
        except tor_proxy.TorError as exc:
            return json_response({"error": str(exc)}, status=400)

    @admin
    async def bind(request):
        try:
            payload = await request.json()
        except Exception:
            return json_response({"error": "Invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            return json_response({"error": "Invalid JSON payload"}, status=400)
        try:
            tor_proxy.set_bind(str(payload.get("bind", "")))
            if tor_proxy.is_enabled():
                await tor_proxy.restart()
            return json_response({"status": "ok", "tor": await tor_proxy.status()})
        except tor_proxy.TorError as exc:
            return json_response({"error": str(exc)}, status=400)

    @admin
    async def exit_nodes(request):
        try:
            payload = await request.json()
        except Exception:
            return json_response({"error": "Invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            return json_response({"error": "Invalid JSON payload"}, status=400)
        try:
            value = str(payload.get("exit_nodes", ""))
            if tor_proxy.is_enabled():
                await tor_proxy.apply_exit_nodes(value)
            else:
                tor_proxy.set_exit_nodes(value)
            return json_response({"status": "ok", "tor": await tor_proxy.status()})
        except tor_proxy.TorError as exc:
            return json_response({"error": str(exc)}, status=400)

    @admin
    async def check(request):
        return json_response(await tor_proxy.check())

    @admin
    async def new_identity(request):
        try:
            await tor_proxy.new_identity()
            return json_response({"status": "new_identity", "tor": await tor_proxy.status(with_probe=True)})
        except tor_proxy.TorError as exc:
            return json_response({"error": str(exc)}, status=400)

    @admin
    async def logs(request):
        return json_response({"logs": await tor_proxy.logs()})

    app.router.add_get("/admin/torproxy", page)
    app.router.add_get("/api/admin/tor/status", status)
    app.router.add_post("/api/admin/tor/start", functools.partial(action, name="start"))
    app.router.add_post("/api/admin/tor/stop", functools.partial(action, name="stop"))
    app.router.add_post("/api/admin/tor/restart", functools.partial(action, name="restart"))
    app.router.add_post("/api/admin/tor/bind", bind)
    app.router.add_post("/api/admin/tor/exit-nodes", exit_nodes)
    app.router.add_post("/api/admin/tor/check", check)
    app.router.add_post("/api/admin/tor/new-identity", new_identity)
    app.router.add_get("/api/admin/tor/logs", logs)


__all__ = ["setup_tor_proxy_routes"]
