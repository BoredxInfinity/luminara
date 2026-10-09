"""TV box controller: serves the launcher and remote, and drives Chromium."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
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
from .config import ROOT, WEB_DIR, Settings, load_services, load_settings
from .cast import Cast
from .logos import Logos
from .mirror import Mirror
from .settings import NEEDS_DISPLAY_RESTART, SAVER_CHOICES, SPOTIFY_POLL_CHOICES, VOLUME_CHOICES, UserSettings
from .spotify import Spotify, SpotifyError
from .system import System, system_info
from .tls import Certificate, ensure_certificate
from .updater import Updater

log = logging.getLogger("tvbox")

LOCALHOST = {"127.0.0.1", "::1"}
VALID_KEYS = set(KEYS) | set(MEDIA_ACTIONS) | {"back"}
MAX_TEXT = 500
MAX_DELTA = 400.0
SPOTIFY_QUIET = 60  # seconds without music before the screensaver goes back to the clock


class Hub:
    """Everything the handlers share: config, the browser, and live state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.services = load_services(settings.services_file)
        self.prefs = UserSettings(settings.data_dir, {s.id for s in self.services}, settings.boot_volume)
        self.auth = Auth(settings.data_dir) if settings.pin_enabled else None
        self.system = System()
        self.logos = Logos(settings.data_dir, self.services)
        self.updater = Updater(ROOT, settings.data_dir)
        self.browser = Browser(settings, self.services, self.on_browser_change)
        self.mirror = Mirror(self.browser)
        self.cast = Cast(self.browser)
        self.spotify = Spotify(settings.data_dir, settings.port)
        self._spotify_watch: asyncio.Task | None = None
        self.browser.on_saver = self.on_saver
        self.tls: Certificate | None = None  # set by main() when mkcert made a certificate
        self.browser.page_config = self.page_config()
        self.clients: dict[web.WebSocketResponse, str] = {}  # socket -> "tv" | "remote"
        self.volume = {"volume": None, "muted": False}
        self.http: aiohttp.ClientSession | None = None
        self.jobs: list[asyncio.Task] = []
        self._recent_file = settings.data_dir / "recent.json"
        self.recent: dict[str, int] = self._load_recent()  # service id -> last opened (epoch s)
        self.selected = max(self.recent, key=self.recent.get, default=self.services[0].id)  # focused tile
        self._service_on_screen: str | None = None
        self._announced_update = ""
        self._remote_left_at = 0.0  # when the last phone disconnected
        self._qr: tuple[str, str] | None = None  # (url, svg)

    # ---- state ----------------------------------------------------------

    def remote_count(self) -> int:
        return sum(1 for role in self.clients.values() if role == "remote")

    def snapshot(self) -> dict:
        return {**self.browser.state, "selected": self.selected, **self.volume,
                "remotes": self.remote_count(), "recent": self.recent,
                "settings": self.prefs.values, "update": self.updater.status}

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
            self.selected = sid
            self._save_recent()
        self._service_on_screen = sid
        await self.broadcast()

    def osd(self, **message) -> None:
        """Show a message over whatever is on the TV (web/inject/overlay.js)."""
        if self.browser.state.get("cdp"):
            self.browser.spawn(self.browser.osd(message))

    def service(self, sid: str):
        return next((s for s in self.services if s.id == sid), None)

    def _load_recent(self) -> dict[str, int]:
        try:
            data = json.loads(self._recent_file.read_text())
            return {k: int(v) for k, v in data.items() if self.service(k)}
        except (FileNotFoundError, ValueError, AttributeError):
            return {}

    def _save_recent(self) -> None:
        tmp = self._recent_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.recent))
        tmp.replace(self._recent_file)

    # ---- settings -------------------------------------------------------

    def page_config(self) -> dict:
        """The settings overlay.js needs inside every page."""
        p = self.prefs
        dpad = [{"hosts": list(s.match), "cards": s.dpad_cards} for s in self.services if s.dpad]
        return {"preferH264": p["prefer_h264"], "saverMinutes": p["saver_minutes"], "saverClock": p["saver_clock"], "dpad": dpad}

    def write_chromium_env(self) -> None:
        """deploy/kiosk.sh sources this when Chromium starts."""
        path = self.settings.data_dir / "chromium.env"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.prefs.chromium_env())
        tmp.replace(path)

    async def apply_settings(self, changed: set[str]) -> None:
        if changed & {"prefer_h264", "saver_minutes", "saver_clock"}:
            self.browser.spawn(self.browser.configure(self.page_config()))
        if changed & NEEDS_DISPLAY_RESTART:
            self.write_chromium_env()
        await self.broadcast()

    # ---- updates --------------------------------------------------------

    async def on_update_status(self) -> None:
        st = self.updater.status
        if st["available"] and st["latest"] not in (self._announced_update, self.prefs["update_dismissed"]):
            self._announced_update = st["latest"]
            if self.browser.state.get("view") != "launcher":  # the launcher shows its own banner
                self.osd(kind="toast", icon="update", text="Update available · install it from Settings on your phone")
        await self.broadcast()

    # ---- Spotify on the screensaver ------------------------------------------------

    def on_saver(self, on: bool) -> None:
        """The screensaver came on or went away. Spotify is only asked while it's up."""
        if self._spotify_watch:
            self._spotify_watch.cancel()
            self._spotify_watch = None
        if on and self.spotify.connected:
            self._spotify_watch = asyncio.create_task(self.watch_spotify())

    async def watch_spotify(self) -> None:
        """While the screensaver shows: what's playing on the account, every few seconds.
        Playing shows the turntable; paused or stopped for over a minute goes back to the clock."""
        shown, quiet_since = False, None
        while True:
            try:
                music = await self.spotify.now_playing(self.http)
            except Exception as exc:  # noqa: BLE001 - a bad answer mustn't end the watch
                log.debug("Spotify check failed: %s", exc)  # keep whatever is showing
            else:
                action, shown, quiet_since = spotify_step(music, time.monotonic(), shown, quiet_since)
                try:
                    if action == "show":
                        await self.browser.music(music)
                    elif action == "clear":
                        await self.browser.music(None)
                except (CDPError, asyncio.TimeoutError):
                    pass
            await asyncio.sleep(self.prefs["spotify_poll_seconds"])

    async def on_install_done(self, ok: bool) -> None:
        """Only reached when the update didn't restart us: it failed, or there was nothing to do."""
        self.osd(kind="ready")  # lift the "Updating…" curtain
        text = "Already up to date" if ok else "Update failed · see Settings → Updates"
        self.osd(kind="toast", icon="update", text=text)
        await self.broadcast()

    async def announce_restart(self) -> None:
        """After an update restarted us, say so on the TV once the browser is back."""
        if not self.updater.status["just_updated"]:
            return
        for _ in range(60):
            if self.browser.state.get("cdp"):
                await asyncio.sleep(2)  # let the page settle
                self.osd(kind="toast", icon="update", text=f"Updated · {self.updater.status['current_subject'][:60]}")
                return
            await asyncio.sleep(1)

    # ---- startup jobs ---------------------------------------------------

    async def apply_boot_volume(self) -> None:
        """Set the boot volume once per boot (marker lives in tmpfs), not on every restart."""
        level = self.prefs["boot_volume"]
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

    def secure_urls(self) -> dict:
        """The HTTPS remote (needed for screen sharing), by name and by address."""
        if not self.tls:
            return {}
        port, host = self.settings.https_port, socket.gethostname().split(".")[0].lower()
        urls = {"secure_url": f"https://{host}.local:{port}/remote"}
        if lan_ip() in self.tls.names:
            urls["secure_ip_url"] = f"https://{lan_ip()}:{port}/remote"
        return urls

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

def spotify_step(music: dict | None, now: float, shown: bool, quiet_since: float | None):
    """One Spotify check during the screensaver -> (action, shown, quiet_since).
    Music playing shows the turntable. Once shown, a paused or stopped player keeps it
    (paused) until SPOTIFY_QUIET seconds pass without music; then back to the clock.
    Paused music never brings the turntable up by itself."""
    playing = bool(music and music.get("playing"))
    quiet_since = None if playing else (quiet_since if quiet_since is not None else now)
    if playing:
        return "show", True, quiet_since
    if shown and now - quiet_since >= SPOTIFY_QUIET:
        return "clear", False, quiet_since
    if shown and music:
        return "show", True, quiet_since  # paused: the record stops, the arm lifts
    return None, shown, quiet_since


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
    if path == "/ws" or path.startswith(("/ws/", "/api/")):
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


@routes.get("/cast")
async def cast_page(request: web.Request):
    """The TV side of screen sharing; only the TV itself shows it."""
    if request.remote not in LOCALHOST:
        raise web.HTTPForbidden(text="this page is for the TV")
    return web.FileResponse(WEB_DIR / "cast" / "index.html")


@routes.get("/ca.crt")
async def ca_certificate(request: web.Request):
    """The box's certificate authority (public part only), for devices to trust once."""
    tls = request.app[HUB].tls
    if not tls:
        raise web.HTTPNotFound(text="this box has no HTTPS certificate (is mkcert installed?)")
    host = socket.gethostname().split(".")[0].lower()
    return web.Response(body=tls.ca.read_bytes(), content_type="application/x-x509-ca-cert", headers={
        "Content-Disposition": f'attachment; filename="luminara-{host}.crt"', "Cache-Control": "no-store"})


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
            "port": hub.settings.port, **hub.secure_urls()}
    if hub.auth and request.remote in LOCALHOST:  # only the TV itself may show the PIN
        data["pin"] = hub.auth.pin
    return web.json_response(data)


@routes.get("/api/services")
async def services(request: web.Request):
    return web.json_response([s.public() for s in request.app[HUB].services])


@routes.post("/api/launch/{id}")
async def launch(request: web.Request):
    hub = request.app[HUB]
    svc = hub.service(request.match_info["id"])
    if not svc:
        raise web.HTTPNotFound(text="unknown service")
    if hub.browser.state.get("view") == "launcher":
        # Let the launcher play its opening animation before the page changes.
        await hub.send_to("tv", {"t": "launch", "id": svc.id})
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


# ---- settings, system, updates ----------------------------------------------

@routes.get("/api/settings")
async def get_settings(request: web.Request):
    return web.json_response({"values": request.app[HUB].prefs.values,
                              "choices": {"saver_minutes": SAVER_CHOICES, "boot_volume": VOLUME_CHOICES,
                                          "spotify_poll_seconds": SPOTIFY_POLL_CHOICES}})


@routes.post("/api/settings")
async def set_settings(request: web.Request):
    hub = request.app[HUB]
    try:
        changed = hub.prefs.update(await read_json(request))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    await hub.apply_settings(changed)
    return ok(values=hub.prefs.values, restart_display=bool(changed & NEEDS_DISPLAY_RESTART))


@routes.get("/api/spotify")
async def spotify_status(request: web.Request):
    return web.json_response(request.app[HUB].spotify.status())


@routes.post("/api/spotify/login")
async def spotify_login(request: web.Request):
    """The Spotify login link for the phone to open."""
    try:
        url = request.app[HUB].spotify.login_url(str((await read_json(request)).get("client_id", "")))
    except SpotifyError as exc:
        return fail(str(exc), 400)
    return ok(url=url)


@routes.post("/api/spotify/finish")
async def spotify_finish(request: web.Request):
    """The address Spotify sent the phone to after login, pasted back from the phone."""
    hub = request.app[HUB]
    pasted = str((await read_json(request)).get("url", ""))
    try:
        await hub.spotify.finish(hub.http, pasted)
    except SpotifyError as exc:
        if hub.spotify.connected and hub.spotify.used(pasted):
            return ok(**hub.spotify.status())  # a second tap on the same address: already done
        return fail(str(exc), 400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return fail("Couldn't reach Spotify from the TV box", 502)
    if hub.browser.saver_on:
        hub.on_saver(True)  # start showing it now if the screensaver is up
    return ok(**hub.spotify.status())  # status() includes the user


@routes.post("/api/spotify/disconnect")
async def spotify_disconnect(request: web.Request):
    hub = request.app[HUB]
    hub.spotify.disconnect()
    hub.on_saver(False)
    return ok(**hub.spotify.status())


@routes.get("/spotify/callback")
async def spotify_callback(request: web.Request):
    """Only reached when the login happened on the TV itself (e.g. through mirror mode);
    on a phone this address doesn't load and is pasted into Settings instead."""
    hub = request.app[HUB]
    if request.remote not in LOCALHOST:
        raise web.HTTPForbidden(text="paste this address into the remote's Settings → Spotify")
    try:
        user = await hub.spotify.finish(hub.http, str(request.url))
        text = f"Spotify is connected{(' as ' + user) if user else ''}. You can close this page."
    except (SpotifyError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        text = f"Couldn't connect Spotify: {exc}"
    return web.Response(text=text, content_type="text/plain")


@routes.get("/api/system")
async def get_system(request: web.Request):
    hub = request.app[HUB]
    return web.json_response({
        **system_info(),
        "hostname": socket.gethostname().split(".")[0], "ip": lan_ip(), "port": hub.settings.port,
        "pin": hub.auth.pin if hub.auth else None,  # paired phones may show it to pair another
        "version": hub.updater.status["current"], "version_subject": hub.updater.status["current_subject"],
    })


@routes.post("/api/display/restart")
async def restart_display(request: web.Request):
    try:
        await request.app[HUB].system.restart_display()
    except RuntimeError as exc:
        return fail(str(exc), 500)
    return ok()


@routes.post("/api/saver/start")
async def saver_start(request: web.Request):
    """The remote's Screensaver button (and the Settings preview). overlay.js shows the
    turntable when music is playing, the particle clock otherwise."""
    await request.app[HUB].browser.send(
        "Runtime.evaluate", {"expression": "window.__tvbox && window.__tvbox.saver(true)"}, timeout=3)
    return ok()


@routes.post("/api/pin/reset")
async def reset_pin(request: web.Request):
    hub = request.app[HUB]
    if not hub.auth:
        return fail("the PIN is turned off", 400)
    hub.auth.reset()
    hub._qr = None
    # Already-connected phones must pair again too.
    phones = [ws for ws, role in hub.clients.items() if role == "remote"] + list(hub.mirror.viewers)
    if hub.cast.sender is not None:
        phones.append(hub.cast.sender)
    await asyncio.gather(*(ws.close(code=WSCloseCode.POLICY_VIOLATION) for ws in phones), return_exceptions=True)
    await hub.send_to("tv", {"t": "repaired"})  # the launcher reloads its QR code and PIN
    return ok()


@routes.post("/api/update/check")
async def update_check(request: web.Request):
    hub = request.app[HUB]
    await hub.updater.check()
    await hub.on_update_status()
    return ok(update=hub.updater.status)


@routes.post("/api/update/install")
async def update_install(request: web.Request):
    hub = request.app[HUB]
    if not hub.updater.status["can_install"]:
        return fail("Updates can only be installed on the TV box itself", 400)
    # Give the update the whole Pi: home screen, every app unloaded, memory handed back.
    try:
        await hub.browser.clear_to_home()
    except (CDPError, asyncio.TimeoutError) as exc:
        log.warning("couldn't clear the TV before updating: %s", exc)  # update anyway
    try:
        await hub.updater.install(hub.on_install_done)
    except RuntimeError as exc:
        return fail(str(exc), 400)
    hub.osd(kind="updating")
    await hub.broadcast()
    return ok()


@routes.post("/api/update/dismiss")
async def update_dismiss(request: web.Request):
    hub = request.app[HUB]
    hub.prefs.update({"update_dismissed": hub.updater.status["latest"]})
    await hub.broadcast()
    return ok()


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
        # Phones drop and reconnect whenever their screen sleeps; only announce a real arrival.
        if hub.remote_count() == 1 and time.monotonic() - hub._remote_left_at > 300:
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
                elif kind == "select" and hub.service(str(data["id"])):
                    hub.selected = str(data["id"])
                    await hub.broadcast()
            except (ValueError, KeyError, TypeError, AttributeError):
                log.debug("ignoring bad ws message: %.80s", msg.data)
    finally:
        hub.clients.pop(ws, None)
        if role == "remote":
            if hub.remote_count() == 0:
                hub._remote_left_at = time.monotonic()
            await hub.broadcast()
    return ws


@routes.get("/ws/mirror")
async def mirror_handler(request: web.Request):
    """Screen mirroring: JPEG frames out (binary), touches and typing in (JSON)."""
    mirror = request.app[HUB].mirror
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4096)
    await ws.prepare(request)
    try:
        await mirror.join(ws)
    except (CDPError, asyncio.TimeoutError):
        pass  # the TV browser isn't up yet; frames start when it attaches
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                mirror.handle(json.loads(msg.data))
            except (ValueError, KeyError, TypeError, AttributeError):
                log.debug("ignoring bad mirror message: %.80s", msg.data)
    finally:
        await mirror.leave(ws)
    return ws


@routes.get("/ws/cast")
async def cast_handler(request: web.Request):
    """Screen sharing signalling: a laptop (sender) and the TV's receiver page."""
    cast = request.app[HUB].cast
    is_tv = request.query.get("role") == "tv" and request.remote in LOCALHOST
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=256 * 1024)  # SDP offers are a few KB
    await ws.prepare(request)
    await (cast.tv_joined(ws) if is_tv else cast.sender_joined(ws))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
                if isinstance(data, dict):
                    await cast.relay(ws, data)
            except ValueError:
                log.debug("ignoring bad cast message")
    finally:
        await (cast.tv_left(ws) if is_tv else cast.sender_left(ws))
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
    hub.write_chromium_env()
    hub.volume = await hub.system.volume()
    await hub.updater.startup()
    hub.browser.start()
    hub.jobs = [
        asyncio.create_task(hub.apply_boot_volume()),
        asyncio.create_task(hub.logos.prefetch(hub.http)),
        asyncio.create_task(hub.announce_restart()),
        asyncio.create_task(hub.updater.run_periodic(lambda: hub.prefs["auto_update_check"], hub.on_update_status)),
    ]


def _ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without root certificates; certifi is a dev dependency.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def _shutdown(app: web.Application) -> None:
    # Open remotes would otherwise hold a graceful shutdown open.
    hub = app[HUB]
    sockets = [*hub.clients, *hub.mirror.viewers, *(ws for ws in (hub.cast.sender, hub.cast.tv) if ws is not None)]
    await asyncio.gather(*(ws.close(code=WSCloseCode.GOING_AWAY) for ws in sockets), return_exceptions=True)


async def _cleanup(app: web.Application) -> None:
    app[HUB].on_saver(False)
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
    app.router.add_static("/cast/", WEB_DIR / "cast")
    app.on_response_prepare.append(_no_stale_assets)
    app.on_startup.append(_startup)
    app.on_shutdown.append(_shutdown)
    app.on_cleanup.append(_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    hub = app[HUB]
    if hub.settings.https_port:
        hub.tls = ensure_certificate(hub.settings.data_dir, lan_ip())
    asyncio.run(_serve(app))


async def _serve(app: web.Application) -> None:
    """Plain HTTP for the TV and phones, plus HTTPS (when there's a certificate) for
    laptops that share their screen."""
    hub = app[HUB]
    s = hub.settings
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    await runner.setup()
    await web.TCPSite(runner, s.host, s.port).start()
    log.info("serving on http://%s:%d (remote: %s)", s.host, s.port, hub.remote_url())
    if hub.tls:
        await web.TCPSite(runner, s.host, s.https_port, ssl_context=hub.tls.context()).start()
        log.info("secure remote (screen sharing): %s", hub.secure_urls()["secure_url"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
