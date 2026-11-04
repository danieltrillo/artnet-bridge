"""aiohttp application serving the single page UI and the JSON API."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

import yaml
from aiohttp import web

from .. import netinfo
from ..config import ConfigError, config_to_dict, dump_yaml, parse_config, parse_yaml

if TYPE_CHECKING:
    from ..app import Supervisor

STATIC_DIR = pathlib.Path(__file__).parent / "static"


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_status(request: web.Request) -> web.Response:
    sup: Supervisor = request.app["supervisor"]
    return web.json_response(sup.status())


async def api_log(request: web.Request) -> web.Response:
    sup: Supervisor = request.app["supervisor"]
    return web.json_response({"lines": sup.ring.to_list()})


async def api_interfaces(request: web.Request) -> web.Response:
    addresses = [
        {"interface": a.interface, "ip": a.ip, "prefix": a.prefix, "cidr": a.cidr, "mac": a.mac}
        for a in netinfo.list_addresses()
        if a.interface != "lo"
    ]
    return web.json_response({"addresses": addresses})


async def api_config_get(request: web.Request) -> web.Response:
    """The config file as text, plus the raw YAML structure for the editor."""
    sup: Supervisor = request.app["supervisor"]
    text = sup.read_text()
    raw = None
    error = sup.config_error
    try:
        raw = yaml.safe_load(text)
        if raw is not None and not isinstance(raw, dict):
            raw = None
            error = "the config file must be a YAML mapping"
    except yaml.YAMLError as exc:
        error = f"YAML syntax error: {getattr(exc, 'problem', exc)}"
    if raw is not None:
        try:
            parse_config(raw)
        except ConfigError as exc:
            error = str(exc)
    return web.json_response({"yaml": text, "raw": raw, "error": error, "path": sup.config_path})


async def api_config_put(request: web.Request) -> web.Response:
    """Accepts {"yaml": "..."} or {"config": {...}}. Validates, saves, applies."""
    sup: Supervisor = request.app["supervisor"]
    try:
        body = await request.json()
    except Exception:
        return _error("request body must be JSON")
    if not isinstance(body, dict):
        return _error("request body must be a JSON object")
    if "yaml" in body:
        text = body["yaml"]
        if not isinstance(text, str):
            return _error("'yaml' must be text")
    elif "config" in body:
        try:
            parse_config(body["config"])  # validate the structure before serialising
        except ConfigError as exc:
            return _error(str(exc))
        text = dump_yaml(body["config"])
    else:
        return _error("send 'yaml' or 'config'")
    try:
        config = await sup.apply_text(text)
    except ConfigError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"could not write {sup.config_path}: {exc.strerror or exc}", status=500)
    return web.json_response(
        {
            "ok": True,
            "config": config_to_dict(config),
            "raw": yaml.safe_load(text),
            "yaml": text,
            "error": sup.config_error,
        }
    )


async def api_validate(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _error("request body must be JSON")
    try:
        if "yaml" in body:
            config = parse_yaml(body["yaml"])
        else:
            config = parse_config(body.get("config"))
    except ConfigError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    return web.json_response({"ok": True, "config": config_to_dict(config)})


async def api_poll(request: web.Request) -> web.Response:
    sup: Supervisor = request.app["supervisor"]
    if sup.engine is None:
        return _error("engine is not running", status=409)
    sup.engine.send_polls()
    sup.engine.announce()
    return web.json_response({"ok": True})


async def api_reload(request: web.Request) -> web.Response:
    sup: Supervisor = request.app["supervisor"]
    await sup.reload_from_disk()
    return web.json_response({"ok": not sup.config_error, "error": sup.config_error})


def make_app(supervisor: Supervisor) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["supervisor"] = supervisor
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/log", api_log)
    app.router.add_get("/api/interfaces", api_interfaces)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_put("/api/config", api_config_put)
    app.router.add_post("/api/config", api_config_put)
    app.router.add_post("/api/validate", api_validate)
    app.router.add_post("/api/poll", api_poll)
    app.router.add_post("/api/reload", api_reload)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


async def make_runner(supervisor: Supervisor, bind: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(supervisor), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, bind, port, reuse_address=True)
    await site.start()
    return runner
