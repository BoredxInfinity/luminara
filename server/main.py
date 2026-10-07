"""TV box controller: serves the launcher and remote, and drives Chromium."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
from urllib.parse import urlsplit

import segno
from aiohttp import WSCloseCode, WSMsgType, web

from .auth import COOKIE, Auth, Locked
from .browser import KEYS, MEDIA_ACTIONS, Browser, CDPError
from .config import WEB_DIR, Settings, load_services, load_settings
from .system import System

log = logging.getLogger("tvbox")

LOCALHOST = {"127.0.0.1", "::1"}
VALID_KEYS = set(KEYS) | set(MEDIA_ACTIONS) | {"back"}
MAX_TEXT = 500
MAX_DELTA = 400.0


class Hub:
    """Everything the handlers share: config, the browser, and live state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.services = load_services(settings.services_file)
        self.auth = Auth(settings.data_dir) if settings.pin_enabled else None
        self.system = System()
        self.browser = Browser(settings, self.services, self.broadcast)
        self.clients: set[web.WebSocketResponse] = set()
        self.selected = 0
        self.volume = {"volume": None, "muted": False}
        self._qr: tuple[str, str] | None = None  # (url, svg)

    def snapshot(self) -> dict:
        return {**self.browser.state, "selected": self.selected, **self.volume}

    async def broadcast(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({"t": "state", **self.snapshot()})
        await asyncio.gather(*(ws.send_str(payload) for ws in list(self.clients)), return_exceptions=True)

    def remote_url(self) -> str:
        return f"http://{lan_ip()}:{self.settings.port}/remote"

    def qr_svg(self) -> str:
        url = self.remote_url() + (f"?pin={self.auth.pin}" if self.auth else "")
        if not self._qr or self._qr[0] != url:
            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False)
            self._qr = (url, buf.getvalue().decode())
        return self._qr[1]


HUB = web.AppKey("hub", Hub)
routes = web.RouteTableDef()


# ---- helpers ---------------------------------------------------------------

def lan_ip() -> str:
    """Address the phone should use. UDP connect sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="expected a JSON body")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return data


def ok(**extra) -> web.Response:
    return web.json_response({"ok": True, **extra})


def fail(error: str, status: int, **kw) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=status, **kw)


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == request.host


# ---- middleware -----------------------------------------------------------

@web.middleware
async def guard(request: web.Request, handler):
    path = request.path
    if path == "/ws" or path.startswith("/api/"):
        # Any website open in Chromium could call localhost; only same-origin may.
        if not _same_origin(request):
            raise web.HTTPForbidden(text="cross-origin request refused")
        auth = request.app[HUB].auth
        if (auth and path != "/api/pair" and request.remote not in LOCALHOST
                and not auth.is_valid(request.cookies.get(COOKIE))):
            raise web.HTTPUnauthorized(text="pair this remote with the PIN shown on the TV")
    try:
        return await handler(request)
    except CDPError as exc:
        return fail(str(exc), 503)


# ---- pages ----------------------------------------------------------------

@routes.get("/")
async def index(request: web.Request):
    raise web.HTTPFound("/remote")


@routes.get("/tv")
async def tv_page(request: web.Request):
    return web.FileResponse(WEB_DIR / "tv" / "index.html")


@routes.get("/remote")
async def remote_page(request: web.Request):
    return web.FileResponse(WEB_DIR / "remote" / "index.html")


@routes.get("/healthz")
async def healthz(request: web.Request):
    return web.Response(text="ok")


@routes.get("/qr.svg")
async def qr(request: web.Request):
    if request.remote not in LOCALHOST:  # it encodes the PIN
        raise web.HTTPForbidden()
    return web.Response(text=request.app[HUB].qr_svg(), content_type="image/svg+xml")


# ---- API --------------------------------------------------------------------

@routes.get("/api/info")
async def info(request: web.Request):
    hub = request.app[HUB]
    data = {"remote_url": hub.remote_url(), "hostname": socket.gethostname().split(".")[0],
            "port": hub.settings.port}
    if hub.auth and request.remote in LOCALHOST:  # only the TV itself may show the PIN
        data["pin"] = hub.auth.pin
    return web.json_response(data)


@routes.get("/api/services")
async def services(request: web.Request):
    return web.json_response([s.public() for s in request.app[HUB].services])


@routes.post("/api/launch/{id}")
async def launch(request: web.Request):
    hub = request.app[HUB]
    sid = request.match_info["id"]
    svc = next((s for s in hub.services if s.id == sid), None)
    if not svc:
        raise web.HTTPNotFound(text=f"unknown service {sid!r}")
    await hub.browser.launch(svc)
    return ok()


@routes.post("/api/home")
async def home(request: web.Request):
    await request.app[HUB].browser.home()
    return ok()


@routes.post("/api/key")
async def key(request: web.Request):
    name = (await read_json(request)).get("key")
    if name not in VALID_KEYS:
        raise web.HTTPBadRequest(text=f"unknown key {name!r}")
    await request.app[HUB].browser.key(name)
    return ok()


@routes.post("/api/text")
async def text(request: web.Request):
    data = await read_json(request)
    value = data.get("text", "")
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise web.HTTPBadRequest(text=f"text must be a string of at most {MAX_TEXT} characters")
    await request.app[HUB].browser.type_text(value, enter=bool(data.get("enter")))
    return ok()


@routes.post("/api/volume")
async def volume(request: web.Request):
    hub = request.app[HUB]
    action = (await read_json(request)).get("action")
    try:
        hub.volume = await hub.system.set_volume(action)
    except ValueError:
        raise web.HTTPBadRequest(text="action must be up, down or mute")
    except RuntimeError as exc:
        return fail(str(exc), 500)
    await hub.broadcast()
    return ok(**hub.volume)


@routes.get("/api/state")
async def state(request: web.Request):
    return web.json_response(request.app[HUB].snapshot())


@routes.post("/api/power")
async def power(request: web.Request):
    action = (await read_json(request)).get("action")
    if action not in ("reboot", "poweroff"):
        raise web.HTTPBadRequest(text="action must be reboot or poweroff")
    try:
        await request.app[HUB].system.power(action)
    except RuntimeError as exc:
        return fail(str(exc), 500)
    return ok()


@routes.post("/api/pair")
async def pair(request: web.Request):
    auth = request.app[HUB].auth
    if not auth:
        return ok()
    try:
        token = auth.pair(request.remote or "", str((await read_json(request)).get("pin", "")))
    except Locked as exc:
        return fail(str(exc), 429, headers={"Retry-After": str(exc.retry_after)})
    if not token:
        return fail("wrong PIN", 403)
    resp = ok()
    resp.set_cookie(COOKIE, token, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


# ---- WebSocket --------------------------------------------------------------

@routes.get("/ws")
async def ws_handler(request: web.Request):
    hub = request.app[HUB]
    browser = hub.browser
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4096)
    await ws.prepare(request)
    hub.clients.add(ws)
    try:
        if hub.system.has_audio and hub.volume["volume"] is None:  # PipeWire may have been late
            hub.volume = await hub.system.volume()
        await ws.send_str(json.dumps({"t": "state", **hub.snapshot()}))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
                kind = data.get("t")
                if kind == "move":
                    browser.move(_clamp(data["dx"]), _clamp(data["dy"]))
                elif kind == "click":
                    browser.spawn(browser.click())
                elif kind == "scroll":
                    browser.spawn(browser.scroll(_clamp(data["dy"])))
                elif kind == "select":
                    hub.selected = int(data["index"])
                    await hub.broadcast()
            except (ValueError, KeyError, TypeError, AttributeError):
                log.debug("ignoring bad ws message: %.80s", msg.data)
    finally:
        hub.clients.discard(ws)
    return ws


def _clamp(v) -> float:
    return max(-MAX_DELTA, min(MAX_DELTA, float(v)))


# ---- app --------------------------------------------------------------------

async def _no_stale_assets(request: web.Request, response: web.StreamResponse) -> None:
    # Revalidate pages and assets every time (a cheap 304) so updates show up at once.
    if not request.path.startswith(("/api/", "/ws")):
        response.headers.setdefault("Cache-Control", "no-cache")


async def _startup(app: web.Application) -> None:
    hub = app[HUB]
    hub.volume = await hub.system.volume()
    hub.browser.start()


async def _shutdown(app: web.Application) -> None:
    # Open remotes would otherwise hold a graceful shutdown open.
    await asyncio.gather(*(ws.close(code=WSCloseCode.GOING_AWAY) for ws in list(app[HUB].clients)),
                         return_exceptions=True)


async def _cleanup(app: web.Application) -> None:
    await app[HUB].browser.stop()


def create_app(settings: Settings | None = None) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[HUB] = Hub(settings or load_settings())
    app.add_routes(routes)
    app.router.add_static("/tv/", WEB_DIR / "tv")
    app.router.add_static("/remote/", WEB_DIR / "remote")
    app.on_response_prepare.append(_no_stale_assets)
    app.on_startup.append(_startup)
    app.on_shutdown.append(_shutdown)
    app.on_cleanup.append(_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    s = app[HUB].settings
    log.info("serving on http://%s:%d (remote: http://%s:%d/remote)", s.host, s.port, lan_ip(), s.port)
    web.run_app(app, host=s.host, port=s.port, access_log=None, print=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
