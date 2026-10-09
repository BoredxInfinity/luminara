"""Chromium control over the DevTools Protocol (CDP).

One WebSocket to the browser endpoint, with the TV tab attached as a flattened
session. That single connection lets us drive the page *and* see new tabs/popups,
which a kiosk must fold back into the one visible tab.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from typing import Awaitable, Callable

import aiohttp

from .config import DEFAULT_MEDIA_KEYS, ROOT, Service, Settings, service_for_url

log = logging.getLogger("tvbox.browser")

# Injected into every page: draws the remote's cursor and on-screen messages (volume, toasts),
# and gives mouse-only sites D-pad navigation.
OVERLAY_JS = (ROOT / "web" / "inject" / "overlay.js").read_text(encoding="utf-8")
DPAD_JS = (ROOT / "web" / "inject" / "dpad.js").read_text(encoding="utf-8")

# name -> (key, code, windowsVirtualKeyCode, text)
KEYS: dict[str, tuple[str, str, int, str]] = {
    "up": ("ArrowUp", "ArrowUp", 38, ""),
    "down": ("ArrowDown", "ArrowDown", 40, ""),
    "left": ("ArrowLeft", "ArrowLeft", 37, ""),
    "right": ("ArrowRight", "ArrowRight", 39, ""),
    "enter": ("Enter", "Enter", 13, "\r"),
    "space": (" ", "Space", 32, " "),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "tab": ("Tab", "Tab", 9, ""),
    "f": ("f", "KeyF", 70, "f"),
    "mediaplaypause": ("MediaPlayPause", "MediaPlayPause", 179, ""),
}
MEDIA_ACTIONS = ("playpause", "seek_fwd", "seek_back")

SIGNAL = "__tvboxSignal"  # the binding overlay.js reports through
RELEASE_AFTER = 1.5  # seconds after going home before Chromium is told to free memory

# Screen mirroring (server/mirror.py): 960x540 JPEGs are sharp enough on a phone and
# cheap enough for the Pi to encode next to a playing video.
SCREENCAST = {"format": "jpeg", "quality": 55, "maxWidth": 960, "maxHeight": 540, "everyNthFrame": 1}


class CDPError(Exception):
    pass


class Pointer:
    """Virtual mouse position. Deltas pile up between sends, so a fast phone
    trackpad never queues more than one CDP mouse event at a time."""

    def __init__(self, width: float = 1920, height: float = 1080):
        self.width, self.height = width, height
        self.x, self.y = width / 2, height / 2
        self._dx = self._dy = 0.0
        self._sized = False

    def resize(self, width: float, height: float) -> None:
        first = not self._sized
        self._sized = True
        self.width, self.height = max(1.0, width), max(1.0, height)
        if first:  # until now we only had a guess at the screen size
            self.x, self.y = self.width / 2, self.height / 2
        self.x = min(self.x, self.width - 1)
        self.y = min(self.y, self.height - 1)

    def add(self, dx: float, dy: float) -> None:
        self._dx += dx
        self._dy += dy

    def take(self) -> tuple[float, float] | None:
        """Apply pending deltas. Returns the new position, or None if unchanged."""
        if not self._dx and not self._dy:
            return None
        nx = min(max(self.x + self._dx, 0.0), self.width - 1)
        ny = min(max(self.y + self._dy, 0.0), self.height - 1)
        self._dx = self._dy = 0.0
        if (nx, ny) == (self.x, self.y):
            return None
        self.x, self.y = nx, ny
        return nx, ny


MODIFIERS = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}  # CDP Input modifier bits


def parse_combo(combo: str) -> tuple[int, str]:
    """'shift+right' -> (8, 'right'). Raises KeyError for unknown keys or modifiers."""
    *mods, key = combo.lower().split("+")
    if key not in KEYS or any(m not in MODIFIERS for m in mods):
        raise KeyError(combo)
    return sum(MODIFIERS[m] for m in set(mods)), key


def resolve_key(name: str, service: Service | None) -> str:
    """Map a remote key name (including media actions) to a key combo like 'shift+right'."""
    if name in MEDIA_ACTIONS:
        name = service.media_key(name) if service else DEFAULT_MEDIA_KEYS[name]
    parse_combo(name)  # validate
    return name


class Browser:
    def __init__(
        self,
        settings: Settings,
        services: list[Service],
        on_change: Callable[[], Awaitable[None] | None],
    ):
        self.settings = settings
        self.services = services
        self._on_change = on_change
        self.pointer = Pointer()
        self.state = {"cdp": False, "view": "offline", "service_id": None, "title": "", "url": ""}

        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._session: str | None = None
        self._target: str | None = None
        self._default_ua: str = ""
        self._ua_applied: str | None = None
        self._popups: set[str] = set()
        self.page_config: dict = {}  # handed to overlay.js as window.__tvboxConfig
        self._script_id: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._moving = False
        self._reattaching = False
        self._runner: asyncio.Task | None = None
        self.screencasting = False
        self._pixel_ratio = 1.0
        self.on_frame: Callable[[dict], None] | None = None  # set by server/mirror.py
        # The page reports the screensaver going on/off through a private binding.
        self.saver_on = False
        self.on_saver: Callable[[bool], Awaitable[None] | None] | None = None
        self._music_sent = ""

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.close()

    async def _run(self) -> None:
        # No session-wide timeout: it would also cut off the long-lived WebSocket.
        self._http = aiohttp.ClientSession()
        delay = 0.5
        while True:
            try:
                await self._connect_and_read()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep retrying whatever happened
                (log.info if delay == 0.5 else log.debug)("CDP unavailable (%s); retrying", exc)
            self._reset_connection()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

    def _reset_connection(self) -> None:
        self._ws = None
        self._session = self._target = None
        self._ua_applied = None
        self._script_id = None
        self._popups.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError("connection closed"))
        self._pending.clear()
        self._set_saver(False)
        if self.state["cdp"]:
            self._update(cdp=False, view="offline")

    async def _connect_and_read(self) -> None:
        assert self._http
        async with self._http.get(f"{self.settings.cdp_url}/json/version", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            version = await resp.json(content_type=None)
        self._default_ua = version.get("User-Agent", "")
        self._ws = await self._http.ws_connect(version["webSocketDebuggerUrl"], max_msg_size=0)
        log.info("connected to %s", version.get("Browser", "browser"))
        reader = asyncio.create_task(self._read(self._ws))
        try:
            await self.send("Target.setDiscoverTargets", {"discover": True}, page=False)
            await self._attach()
            await reader
        finally:
            reader.cancel()
            if self._ws and not self._ws.closed:
                await self._ws.close()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            if "id" in data:
                fut = self._pending.pop(data["id"], None)
                if fut and not fut.done():
                    if "error" in data:
                        fut.set_exception(CDPError(data["error"].get("message", "CDP error")))
                    else:
                        fut.set_result(data.get("result", {}))
            else:
                self._dispatch(data.get("method", ""), data.get("params", {}), data.get("sessionId"))
        raise CDPError("browser connection closed")

    # ---- plumbing --------------------------------------------------------

    async def send(self, method: str, params: dict | None = None, *, page: bool = True, timeout: float = 10) -> dict:
        ws = self._ws
        if ws is None or ws.closed:
            raise CDPError("not connected to Chromium")
        if page and not self._session:
            raise CDPError("no page attached")
        mid = next(self._ids)
        msg: dict = {"id": mid, "method": method, "params": params or {}}
        if page:
            msg["sessionId"] = self._session
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await ws.send_str(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_error)

    def _update(self, **changes) -> None:
        if all(self.state.get(k) == v for k, v in changes.items()):
            return
        self.state.update(changes)
        result = self._on_change()
        if asyncio.iscoroutine(result):
            self.spawn(result)

    # ---- page attachment -------------------------------------------------

    async def _attach(self) -> None:
        targets = (await self.send("Target.getTargets", page=False))["targetInfos"]
        pages = [t for t in targets if t["type"] == "page" and not t["url"].startswith(("devtools://", "chrome-extension://"))]
        if pages:
            target = pages[0]
            for extra in pages[1:]:  # a kiosk shows one tab; anything else is stray
                self.spawn(self.send("Target.closeTarget", {"targetId": extra["targetId"]}, page=False))
        else:
            created = await self.send("Target.createTarget", {"url": self.settings.launcher_url}, page=False)
            target = {"targetId": created["targetId"], "url": self.settings.launcher_url, "title": ""}

        res = await self.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}, page=False)
        self._target, self._session = target["targetId"], res["sessionId"]

        await self.send("Page.enable")
        await self.send("Inspector.enable")
        # window.__tvboxSignal(json): how overlay.js tells us the screensaver came on or went.
        await self.send("Runtime.addBinding", {"name": SIGNAL})
        # Without this, an unfocused window drops synthetic mouse presses (and some
        # players pause on blur). Under cage the window is focused anyway; this is cheap.
        await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        await self._register_overlay()
        try:
            await self.send("Runtime.evaluate", {"expression": self._overlay_source()})
            await self.send("Runtime.evaluate", {"expression": "window.__tvbox && window.__tvbox.osd({kind: 'ready'})"})
        except CDPError as exc:
            # A tab Chromium discarded (e.g. under memory pressure) has no page to run in.
            # Load the home screen into it instead of retrying forever.
            log.warning("TV tab has no live page (%s); loading the home screen", exc)
            await self.send("Page.navigate", {"url": self.settings.launcher_url})
            target = {**target, "url": self.settings.launcher_url}
        await self._refresh_viewport()
        if self.screencasting:  # a phone was mirroring the tab we lost
            await self.send("Page.startScreencast", SCREENCAST)
        self._update(cdp=True, **self._describe(target["url"], target.get("title", "")))
        await self._apply_user_agent(service_for_url(self.services, target["url"]))
        log.info("attached to tab %s (%s)", self._target, target["url"])

    async def _reattach(self) -> None:
        # Losing the tab fires both targetDestroyed and detachedFromTarget; attach only once.
        if self._reattaching:
            return
        self._reattaching = True
        self._session = self._target = None
        self._ua_applied = None
        self._script_id = None
        try:
            await self._attach()
        except CDPError as exc:
            log.warning("re-attach failed: %s", exc)
        finally:
            self._reattaching = False

    async def _refresh_viewport(self) -> None:
        metrics = await self.send("Page.getLayoutMetrics")
        vp = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport", {})
        self.pointer.resize(vp.get("clientWidth", 1920), vp.get("clientHeight", 1080))
        css, device = metrics.get("cssVisualViewport", {}), metrics.get("visualViewport", {})
        self._pixel_ratio = (device.get("clientWidth") or 1) / (css.get("clientWidth") or device.get("clientWidth") or 1)

    def _describe(self, url: str, title: str) -> dict:
        if url.startswith(self.settings.cast_url):
            return {"view": "cast", "service_id": None, "url": url, "title": "Screen sharing"}
        if url.startswith(self.settings.launcher_url):
            return {"view": "launcher", "service_id": None, "url": url, "title": "Home"}
        svc = service_for_url(self.services, url)
        return {
            "view": "service" if svc else "web",
            "service_id": svc.id if svc else None,
            "url": url,
            "title": title,
        }

    # ---- events ----------------------------------------------------------

    def _dispatch(self, method: str, params: dict, session: str | None) -> None:
        if method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            if info["targetId"] == self._target:
                if info["url"] != self.state.get("url"):
                    self._set_saver(False)  # a new page starts without the screensaver
                self._update(**self._describe(info["url"], info.get("title", "")))
            elif info["targetId"] in self._popups:
                self._maybe_fold_popup(info)
        elif method == "Target.targetCreated":
            info = params["targetInfo"]
            if info["type"] == "page" and self._target and info["targetId"] != self._target:
                self._popups.add(info["targetId"])
                self._maybe_fold_popup(info)
        elif method == "Target.targetDestroyed":
            self._popups.discard(params["targetId"])
            if params["targetId"] == self._target:
                log.warning("TV tab closed; re-attaching")
                self.spawn(self._reattach())
        elif method == "Target.detachedFromTarget":
            if params.get("sessionId") == self._session:
                self.spawn(self._reattach())
        elif method == "Runtime.bindingCalled" and session == self._session and params.get("name") == SIGNAL:
            try:
                msg = json.loads(params.get("payload") or "{}")
            except ValueError:
                return
            if isinstance(msg, dict) and isinstance(msg.get("saver"), bool):
                self._set_saver(msg["saver"])
        elif method == "Page.screencastFrame" and session == self._session and self.on_frame:
            self.on_frame(params)
        elif method == "Inspector.targetCrashed" and session == self._session:
            log.warning("TV tab crashed; going home")
            self.spawn(self.home())

    def _maybe_fold_popup(self, info: dict) -> None:
        """Close a popup tab and open its URL in the TV tab instead."""
        url = info.get("url", "")
        if not url or url == "about:blank":
            return  # wait for targetInfoChanged with the real URL
        self._popups.discard(info["targetId"])
        self.spawn(self.send("Target.closeTarget", {"targetId": info["targetId"]}, page=False))
        if url.startswith(("http://", "https://")):
            self.spawn(self.send("Page.navigate", {"url": url}))

    # ---- commands --------------------------------------------------------

    def current_service(self) -> Service | None:
        sid = self.state.get("service_id")
        return next((s for s in self.services if s.id == sid), None)

    async def _apply_user_agent(self, svc: Service | None) -> None:
        ua = (svc.user_agent if svc else None) or self._default_ua
        if ua and ua != self._ua_applied:
            await self.send("Emulation.setUserAgentOverride", {"userAgent": ua})
            self._ua_applied = ua

    async def launch(self, svc: Service) -> None:
        await self._apply_user_agent(svc)
        await self.send("Page.navigate", {"url": svc.url})
        await self._refresh_viewport()

    async def home(self) -> None:
        leaving_app = self.state["view"] != "launcher"
        await self._apply_user_agent(None)
        await self.send("Page.navigate", {"url": self.settings.launcher_url})
        if leaving_app:
            self.spawn(self._release_memory())

    async def _release_memory(self) -> None:
        """After leaving an app, hand back what it used. The app's renderer exits with the
        page (kiosk.sh turns off the back-forward cache), but freed memory and caches can
        linger in the processes that remain; a simulated memory-pressure signal makes every
        Chromium process drop them now rather than whenever it gets round to it.
        Only "moderate": "critical" makes Chromium discard tabs, including the home screen,
        and Memory.forciblyPurgeJavaScriptMemory kills the page's scripts outright."""
        await asyncio.sleep(RELEASE_AFTER)  # let the home screen finish loading first
        await self.send("Memory.simulatePressureNotification", {"level": "moderate"}, timeout=5)

    async def clear_to_home(self) -> None:
        """The home screen with nothing else running: stray tabs closed, the app unloaded,
        and what it freed handed back. Waits until done (unlike home()), for jobs that need
        the room: installing an update, and before and after screen sharing."""
        targets = (await self.send("Target.getTargets", page=False))["targetInfos"]
        for t in targets:
            if t["type"] == "page" and t["targetId"] != self._target and not t["url"].startswith("devtools://"):
                await self.send("Target.closeTarget", {"targetId": t["targetId"]}, page=False)
        if not self.state["url"].startswith(self.settings.launcher_url):
            await self._apply_user_agent(None)
            await self.send("Page.navigate", {"url": self.settings.launcher_url})
        await self._release_memory()

    # ---- screen sharing (server/cast.py) ---------------------------------------

    async def open_cast(self) -> None:
        """Clear everything down to the home screen first, so the receiver has the Pi to
        itself, then show it."""
        if self.state["view"] == "cast":
            return
        await self.clear_to_home()
        await self.send("Page.navigate", {"url": self.settings.cast_url})

    async def end_cast(self) -> None:
        """Sharing is over: always back to a cleared home screen."""
        if self.state["view"] != "cast":
            return  # someone already switched to something else
        await self.clear_to_home()

    async def back(self) -> None:
        if self.state["view"] == "launcher":
            return
        svc = self.current_service()
        if svc and svc.keys.get("back"):
            await self.press(svc.keys["back"])
            return
        hist = await self.send("Page.getNavigationHistory")
        idx, entries = hist["currentIndex"], hist["entries"]
        if idx <= 0 or entries[idx - 1]["url"].startswith(self.settings.launcher_url):
            await self.home()
        else:
            await self.send("Page.navigateToHistoryEntry", {"entryId": entries[idx - 1]["id"]})

    async def key(self, name: str) -> None:
        if name == "back":
            await self.back()
        else:
            await self.press(resolve_key(name, self.current_service()))

    async def press(self, combo: str) -> None:
        mods, name = parse_combo(combo)
        key, code, vk, text = KEYS[name]
        if mods & ~MODIFIERS["shift"]:
            text = ""  # Ctrl/Alt/Meta shortcuts don't type anything
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
        down = {"type": "keyDown" if text else "rawKeyDown", **base}
        if text:
            down["text"] = down["unmodifiedText"] = text
        await self.send("Input.dispatchKeyEvent", down)
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def _overlay_source(self) -> str:
        # The config must exist before the overlay runs: it patches codec checks at page start.
        return f"window.__tvboxConfig = {json.dumps(self.page_config)};\n{OVERLAY_JS}\n{DPAD_JS}"

    async def _register_overlay(self) -> None:
        if self._script_id:
            await self.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._script_id})
        res = await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": self._overlay_source()})
        self._script_id = res.get("identifier")

    async def configure(self, config: dict) -> None:
        """New settings for overlay.js: future pages get them at load, the current page right away."""
        self.page_config = config
        if not self._session:
            return
        await self._register_overlay()
        await self.send("Runtime.evaluate", {"expression": f"window.__tvbox && window.__tvbox.configure({json.dumps(config)})"})

    def _set_saver(self, on: bool) -> None:
        if on == self.saver_on:
            return
        self.saver_on = on
        self._music_sent = ""
        if self.on_saver:
            result = self.on_saver(on)
            if asyncio.iscoroutine(result):
                self.spawn(result)

    async def music(self, playing: dict | None) -> None:
        """What's playing on Spotify, for the screensaver's turntable (None: back to the clock)."""
        payload = json.dumps(playing)
        if payload == self._music_sent:
            return
        await self.send("Runtime.evaluate", {"expression": f"window.__tvbox && window.__tvbox.music({payload})"}, timeout=5)
        self._music_sent = payload

    async def osd(self, message: dict) -> None:
        expr = f"window.__tvbox && window.__tvbox.osd({json.dumps(message)})"
        await self.send("Runtime.evaluate", {"expression": expr}, timeout=3)

    async def type_text(self, text: str, enter: bool = False) -> None:
        if text:
            await self.send("Input.insertText", {"text": text})
        if enter:
            await self.press("enter")

    # Pointer input arrives at phone frame rate; it is fire-and-forget.

    def move(self, dx: float, dy: float) -> None:
        if not self._session:
            return
        self.pointer.add(dx, dy)
        if not self._moving:
            self._moving = True
            self.spawn(self._flush_moves())

    async def _flush_moves(self) -> None:
        try:
            while (pos := self.pointer.take()) is not None:
                await self.send("Input.dispatchMouseEvent",
                                {"type": "mouseMoved", "x": pos[0], "y": pos[1], "button": "none"}, timeout=2)
        finally:
            self._moving = False

    async def click(self) -> None:
        x, y = self.pointer.x, self.pointer.y
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **base})
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "buttons": 0, **base})

    async def scroll(self, dy: float) -> None:
        await self.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": self.pointer.x, "y": self.pointer.y, "deltaX": 0, "deltaY": dy,
        })

    # Screen mirroring: phones send positions as fractions (0..1) of the TV picture.
    # The touchpad cursor follows, so switching back to it carries on from there.

    async def screencast(self, on: bool) -> None:
        self.screencasting = on
        if self._session:
            await self.send("Page.startScreencast" if on else "Page.stopScreencast", SCREENCAST if on else None)

    async def snapshot(self) -> bytes:
        """One screencast-sized JPEG of the TV right now."""
        w, h = self.pointer.width * self._pixel_ratio, self.pointer.height * self._pixel_ratio
        scale = min(1.0, SCREENCAST["maxWidth"] / w, SCREENCAST["maxHeight"] / h)
        res = await self.send("Page.captureScreenshot", timeout=5, params={
            "format": "jpeg", "quality": SCREENCAST["quality"],
            "clip": {"x": 0, "y": 0, "width": self.pointer.width, "height": self.pointer.height, "scale": scale},
        })
        return base64.b64decode(res["data"])

    def _at(self, nx, ny) -> tuple[float, float]:
        x = min(max(float(nx), 0.0), 1.0) * (self.pointer.width - 1)
        y = min(max(float(ny), 0.0), 1.0) * (self.pointer.height - 1)
        self.pointer.x, self.pointer.y = x, y
        return x, y

    async def pointer_event(self, kind: str, nx, ny) -> None:
        """hover: move the mouse; press/drag/release: hold the button down (sliders, seek bars)."""
        x, y = self._at(nx, ny)
        event = {"hover": ("mouseMoved", "none", 0), "press": ("mousePressed", "left", 1),
                 "drag": ("mouseMoved", "left", 1), "release": ("mouseReleased", "left", 0)}[kind]
        await self.send("Input.dispatchMouseEvent", {
            "type": event[0], "x": x, "y": y, "button": event[1], "buttons": event[2], "clickCount": 1 if kind != "hover" else 0,
        }, timeout=3)

    async def tap(self, nx, ny, count: int = 1) -> None:
        x, y = self._at(nx, ny)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        base = {"x": x, "y": y, "button": "left", "clickCount": count}
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **base})
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "buttons": 0, **base})

    async def pan(self, nx, ny, dx, dy) -> None:
        """A finger dragging the page: scroll the thing under it, following the finger."""
        x, y = self._at(nx, ny)
        await self.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": -_frac(dx) * self.pointer.width, "deltaY": -_frac(dy) * self.pointer.height,
        }, timeout=3)


def _frac(v) -> float:
    return min(max(float(v), -1.0), 1.0)


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled() or task.exception() is None:
        return
    exc = task.exception()
    if isinstance(exc, (CDPError, asyncio.TimeoutError)):
        log.debug("CDP call failed: %s", exc)
    else:
        log.error("background task failed", exc_info=exc)
