"""Is the box on the internet? The TV shows a "no internet" screen instead of anything
else while it isn't (web/offline/).

Asked by fetching small "204 No Content" pages from two unrelated companies; one answering
is enough, so neither being down or blocked looks like an outage. Anything but a 204 (such as
a Wi-Fi login page answering instead) doesn't count. One failed check doesn't decide it either:
Wi-Fi blips shouldn't throw you out of a film, so it takes a few in a row. Being back
online is believed at once.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("tvbox.network")

PROBES = ("http://connectivitycheck.gstatic.com/generate_204", "http://cp.cloudflare.com/generate_204")
TIMEOUT = 4        # seconds for one check
ONLINE_EVERY = 30  # seconds between checks while all is well
RETRY_EVERY = 5    # seconds between checks while they're failing
OFFLINE_AFTER = 3  # failed checks in a row before the box counts as offline


def interfaces(sys_net: Path = Path("/sys/class/net")) -> dict:
    """What's plugged in and connected, for the screen to point at the likely culprit."""
    status = {"wifi": {"present": False, "connected": False}, "ethernet": {"present": False, "connected": False}}
    try:
        names = [p for p in sys_net.iterdir() if p.name != "lo"]
    except OSError:
        return status
    for dev in names:
        if (dev / "wireless").exists():
            kind = "wifi"
        elif dev.name.startswith(("eth", "en")):
            kind = "ethernet"
        else:
            continue
        try:
            up = (dev / "operstate").read_text().strip() == "up"
        except OSError:
            up = False
        status[kind]["present"] = True
        status[kind]["connected"] = status[kind]["connected"] or up
    return status


async def _answers(http: aiohttp.ClientSession, url: str) -> bool:
    try:
        async with http.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            return resp.status == 204
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


async def reachable(http: aiohttp.ClientSession, probes: tuple[str, ...] = PROBES) -> bool:
    """True as soon as any probe answers."""
    tasks = [asyncio.create_task(_answers(http, url)) for url in probes]
    try:
        for finished in asyncio.as_completed(tasks):
            if await finished:
                return True
        return False
    finally:
        for task in tasks:
            task.cancel()


class Connectivity:
    def __init__(self, probes: tuple[str, ...] = PROBES):
        self.probes = probes
        self.online: bool | None = None  # None until the first verdict
        self._failures = 0

    def record(self, answered: bool) -> None:
        if answered:
            self._failures = 0
            if self.online is False:
                log.info("back on the internet")
            self.online = True
        else:
            self._failures += 1
            if self._failures >= OFFLINE_AFTER and self.online is not False:
                log.warning("no internet connection: %s", interfaces())
                self.online = False

    async def run(self, http: aiohttp.ClientSession, on_status: Callable[[bool], Awaitable[None]]) -> None:
        """Check forever, reporting the verdict after every check (the TV may have strayed
        from where it should be, e.g. after Chromium restarted)."""
        while True:
            answered = await reachable(http, self.probes)
            self.record(answered)
            if self.online is not None:
                try:
                    await on_status(self.online)
                except Exception as exc:  # noqa: BLE001 - the next check tries again
                    log.debug("couldn't act on the connection state: %s", exc)
            await asyncio.sleep(ONLINE_EVERY if answered else RETRY_EVERY)
