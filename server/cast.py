"""Screen sharing from a laptop to the TV.

The picture travels straight from the laptop's browser to Chromium on the TV over WebRTC;
this only introduces the two (relaying their offer, answer and network candidates) and
switches the TV to its receiver page (web/cast/) while sharing, then back again.

One laptop shares at a time: a new one takes over and the previous one is told it ended.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from .browser import Browser, CDPError

log = logging.getLogger("tvbox.cast")

RELAYED = {"offer", "answer", "ice"}
TV_GRACE = 2.5  # seconds for the receiver page to reconnect before sharing counts as ended


class Cast:
    def __init__(self, browser: Browser):
        self.browser = browser
        self.sender: web.WebSocketResponse | None = None  # the laptop
        self.tv: web.WebSocketResponse | None = None      # web/cast/ on the TV

    @property
    def active(self) -> bool:
        return self.sender is not None

    async def _send(self, ws: web.WebSocketResponse | None, msg: dict) -> None:
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(json.dumps(msg))
            except (ConnectionError, RuntimeError):
                pass

    # ---- the laptop ------------------------------------------------------------

    async def sender_joined(self, ws: web.WebSocketResponse) -> None:
        old, self.sender = self.sender, ws
        if old is not None:
            await self._send(old, {"t": "ended", "reason": "replaced"})
            await old.close()
        if self.tv is not None:
            await self._send(ws, {"t": "ready"})  # the receiver is up: make an offer
        else:
            try:
                await self.browser.open_cast()     # the receiver says "ready" when it loads
            except CDPError as exc:
                await self._send(ws, {"t": "ended", "reason": "tv-unavailable"})
                log.warning("couldn't switch the TV to screen sharing: %s", exc)

    async def sender_left(self, ws: web.WebSocketResponse) -> None:
        if ws is not self.sender:
            return  # replaced already
        self.sender = None
        await self._send(self.tv, {"t": "stop"})
        try:
            await self.browser.end_cast()
        except CDPError:
            pass

    # ---- the TV ----------------------------------------------------------------

    async def tv_joined(self, ws: web.WebSocketResponse) -> None:
        old, self.tv = self.tv, ws
        if old is not None and old is not ws:
            await old.close()
        if self.sender is not None:
            await self._send(self.sender, {"t": "ready"})
        else:
            # Nobody is sharing (e.g. the page was reloaded after it ended): go back.
            try:
                await self.browser.end_cast()
            except CDPError:
                pass

    async def tv_left(self, ws: web.WebSocketResponse) -> None:
        if ws is not self.tv:
            return
        self.tv = None
        # A reload comes straight back; pressing Home or Back on a remote doesn't.
        await asyncio.sleep(TV_GRACE)
        if self.tv is None and self.sender is not None and self.browser.state["view"] != "cast":
            sender, self.sender = self.sender, None
            await self._send(sender, {"t": "ended", "reason": "tv"})

    # ---- both ------------------------------------------------------------------

    async def relay(self, ws: web.WebSocketResponse, data: dict) -> None:
        kind = data.get("t")
        if ws is self.sender:
            if kind == "stop":
                await self.sender_left(ws)
            elif kind in RELAYED:
                await self._send(self.tv, data)
        elif ws is self.tv and kind in RELAYED:
            await self._send(self.sender, data)
