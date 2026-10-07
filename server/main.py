"""TV box controller: serves the launcher and remote, and drives Chromium."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import segno
from aiohttp import WSCloseCode, WSMsgType, web

from .auth import COOKIE, Auth, Locked
from .browser import KEYS, MEDIA_ACTIONS, Browser, CDPError
from .config import WEB_DIR, Settings, load_services, load_settings
from .logos import Logos
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
        self.logos = Logos(settings.data_dir, self.services)
        self.browser = Browser(settings, self.services, self.on_browser_change)
        self.clients: dict[web.WebSocketResponse, str] = {}  # socket -> "tv" | "remote"
        self.volume = {"volume": None, "muted": False}
        self.http: aiohttp.ClientSession | None = None
        self.jobs: list[asyncio.Task] = []
        self._recent_file = settings.data_dir / "recent.json"
        self.recent: dict[str, int] = self._load_recent()  # service id -> last opened (epoch s)
        self.selected = self._index(max(self.recent, key=self.recent.get, default=None))
        self._service_on_screen: str | None = None
        self._qr: tuple[str, str] | None = None  # (url, svg)

    # ---- state ----------------------------------------------------------

    def remote_count(self) -> int:
        return sum(1 for role in self.clients.values() if role == "remote")

    def snapshot(self) -> dict:
        return {**self.browser.state, "selected": self.selected, **self.volume,
                "remotes": self.remote_count(), "recent": self.recent}

    async def broadcast(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({"t": "state", **self.snapshot()})
        await asyncio.gather(*(ws.send_str(payload) for ws in list(self.clients)), return_exceptions=True)

    async def send_to(self, role: str, message: dict) -> None:
        payload = json.dumps(message)
        targets = [ws for ws, r in self.clients.items() if r == role]
        await asyncio.gather(*(ws.send_str(payload) for ws in targets), return_exceptions=True)

    async def on_browser_change(self) -> None:
        sid = self.browser.state.get("service_id")
        if sid and sid != self._service_on_screen:
            # Remember what was opened, and focus its tile when we come back home.
            self.recent[sid] = int(time.time())
            self.selected = self._index(sid)
            self._save_recent()
        self._service_on_screen = sid
        await self.broadcast()

    def osd(self, **message) -> None:
        """Show a message over whatever is on the TV (web/inject/overlay.js)."""
        if self.browser.state.get("cdp"):
            self.browser.spawn(self.browser.osd(message))

    def _index(self, sid: str | None) -> int:
        return next((i for i, s in enumerate(self.services) if s.id == sid), 0)

    def _load_recent(self) -> dict[str, int]:
        try:
            data = json.loads(self._recent_file.read_text())
            return {k: int(v) for k, v in data.items() if any(s.id == k for s in self.services)}
        except (FileNotFoundError, ValueError, AttributeError):
            return {}

    def _save_recent(self) -> None:
        tmp = self._recent_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.recent))
        tmp.replace(self._recent_file)

    # ---- startup jobs ---------------------------------------------------

    async def apply_boot_volume(self) -> None:
        """Set the boot volume once per boot (marker lives in tmpfs), not on every restart."""
        level = self.settings.boot_volume
        if not level or not self.system.has_audio:
            return
        marker = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "tvbox-boot-volume"
        if marker.exists():
            return
        await asyncio.sleep(3)  # let WirePlumber settle on the HDMI output first
        for _ in range(30):
            try:
                self.volume = await self.system.set_level(level)
            except RuntimeError:
                await asyncio.sleep(2)  # PipeWire not up yet
                continue
            marker.touch()
            log.info("boot volume set to %d%%", level)
            await self.broadcast()
            return
        log.warning("couldn't set boot volume: PipeWire never came up")

    # ---- pairing --------------------------------------------------------

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


@routes.get("/logos/{id}/{kind}")
async def logo(request: web.Request):
    hub = request.app[HUB]
    found = await hub.logos.get(request.match_info["id"], request.match_info["kind"], hub.http)
    if not found:
        raise web.HTTPNotFound()
    path, ctype = found
    return web.FileResponse(path, headers={"Content-Type": ctype, "Cache-Control": "max-age=86400"})


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
    if hub.browser.state.get("view") == "launcher":
        # Let the launcher play its opening animation before the page changes.
        await hub.send_to("tv", {"t": "launch", "id": sid})
        await asyncio.sleep(0.45)
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
    hub.osd(kind="volume", **hub.volume)
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
    role = "tv" if request.query.get("role") == "tv" else "remote"
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4096)
    await ws.prepare(request)
    hub.clients[ws] = role
    if role == "remote":
        if hub.remote_count() == 1:
            hub.osd(kind="toast", icon="phone", text="Remote connected")
        await hub.broadcast()  # everyone sees the new remote count
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
                    hub.selected = max(0, min(len(hub.services) - 1, int(data["index"])))
                    await hub.broadcast()
            except (ValueError, KeyError, TypeError, AttributeError):
                log.debug("ignoring bad ws message: %.80s", msg.data)
    finally:
        hub.clients.pop(ws, None)
        if role == "remote":
            await hub.broadcast()
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
    hub.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_context()))
    hub.volume = await hub.system.volume()
    hub.browser.start()
    hub.jobs = [asyncio.create_task(hub.apply_boot_volume()), asyncio.create_task(hub.logos.prefetch(hub.http))]


def _ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without root certificates; certifi is a dev dependency.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def _shutdown(app: web.Application) -> None:
    # Open remotes would otherwise hold a graceful shutdown open.
    await asyncio.gather(*(ws.close(code=WSCloseCode.GOING_AWAY) for ws in list(app[HUB].clients)),
                         return_exceptions=True)


async def _cleanup(app: web.Application) -> None:
    for job in app[HUB].jobs:
        job.cancel()
    await asyncio.gather(*app[HUB].jobs, return_exceptions=True)
    await app[HUB].browser.stop()
    await app[HUB].http.close()


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
