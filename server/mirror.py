"""Screen mirroring: the TV tab streamed to phones, and their touches played back.

Chromium's screencast (CDP Page.startScreencast) hands us JPEG frames only when the
page changes. We forward each one to every watching phone as a binary WebSocket
message, then acknowledge it, which is what lets Chromium send the next. Delaying
that acknowledgement caps the frame rate, so a playing video can't flood the Pi.
The screencast runs only while at least one phone is watching.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from aiohttp import web

from .browser import Browser, CDPError

log = logging.getLogger("tvbox.mirror")

MAX_FPS = 10
SEND_TIMEOUT = 1.0  # a phone that can't keep up misses frames instead of stalling the rest


class Mirror:
    def __init__(self, browser: Browser):
        self.browser = browser
        self.viewers: set[web.WebSocketResponse] = set()
        self._last_frame: bytes | None = None
        self._last_sent = 0.0
        self._turn = asyncio.Lock()  # Chromium keeps two frames in flight; send them in turn
        browser.on_frame = self._on_frame

    async def join(self, ws: web.WebSocketResponse) -> None:
        self.viewers.add(ws)
        if len(self.viewers) == 1:
            await self.browser.screencast(True)
        # The screencast only sends frames when something changes: show a still page at once.
        # Only a nicety: a busy Pi can be slow to grab it, and that mustn't drop the phone.
        if not self._last_frame:
            try:
                self._last_frame = await self.browser.snapshot()
            except (CDPError, asyncio.TimeoutError) as exc:
                log.debug("no opening snapshot: %s", exc)
                return
        await self._send(ws, self._last_frame)

    async def leave(self, ws: web.WebSocketResponse) -> None:
        self.viewers.discard(ws)
        if not self.viewers:
            self._last_frame = None
            try:
                await self.browser.screencast(False)
            except CDPError:
                pass

    def _on_frame(self, params: dict) -> None:
        self.browser.spawn(self._deliver(params))

    async def _deliver(self, params: dict) -> None:
        async with self._turn:
            wait = self._last_sent + 1 / MAX_FPS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            frame = base64.b64decode(params["data"])
            self._last_frame = frame
            await asyncio.gather(*(self._send(ws, frame) for ws in list(self.viewers)))
            self._last_sent = time.monotonic()
            await self.browser.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, timeout=3)

    async def _send(self, ws: web.WebSocketResponse, frame: bytes) -> None:
        if ws.closed:
            return
        try:
            await asyncio.wait_for(ws.send_bytes(frame), SEND_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            pass

    def handle(self, data: dict) -> None:
        """A touch or key from a phone. Coordinates are 0..1 across the TV picture."""
        b = self.browser
        kind = data.get("t")
        if kind in ("hover", "press", "drag", "release"):
            b.spawn(b.pointer_event(kind, data["x"], data["y"]))
        elif kind == "tap":
            b.spawn(b.tap(data["x"], data["y"], 2 if data.get("n") == 2 else 1))
        elif kind == "pan":
            b.spawn(b.pan(data["x"], data["y"], data["dx"], data["dy"]))
        elif kind == "text" and isinstance(data.get("s"), str):
            b.spawn(b.type_text(data["s"][:200]))
        elif kind == "key" and data.get("k") in ("backspace", "enter"):
            b.spawn(b.press(data["k"]))
