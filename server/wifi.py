"""The remote's Wi-Fi settings. Reading what's connected needs no special rights; joining a
network is done as root by deploy/tvbox_wifi.py (allowed by a sudoers rule from install.sh),
which tries the new network and keeps the old one if that fails.

Joining can cut off the phone that asked, so it runs as a job here and the phone asks how it
went, if it can still reach the box. The TV says how it went too. The password is only ever
passed on to the helper: never stored here, logged or sent back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("tvbox.wifi")

HELPER = "/usr/local/sbin/tvbox-wifi"
JOB_TIMEOUT = 180  # seconds; the helper's own steps add up to less


async def _nmcli(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


def _unescape(field: str) -> str:
    return field.replace("\\:", ":").replace("\\\\", "\\")


class Wifi:
    def __init__(self) -> None:
        self.available = Path(HELPER).exists() and shutil.which("nmcli") is not None
        self.job: dict = {"state": "idle", "ssid": "", "error": ""}
        self._task: asyncio.Task | None = None

    async def status(self) -> dict:
        info = {"available": self.available, "ssid": "", "networks": [], "job": self.job}
        if not self.available:
            return info
        networks, active = set(), ""
        for line in (await _nmcli("-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no")).splitlines():
            on, _, ssid = line.partition(":")
            ssid = _unescape(ssid)
            if ssid:
                networks.add(ssid)
                if on == "yes":
                    active = ssid
        info["ssid"], info["networks"] = active, sorted(networks, key=str.lower)
        return info

    def start(self, ssid: str, password: str, on_done: Callable[[dict], Awaitable[None]]) -> bool:
        """Join a network in the background. False if a join is already under way."""
        if self.job["state"] == "joining":
            return False
        self.job = {"state": "joining", "ssid": ssid, "error": ""}
        self._task = asyncio.create_task(self._join(ssid, password, on_done))
        return True

    async def _join(self, ssid: str, password: str, on_done) -> None:
        try:
            result = await self._run_helper(ssid, password)
        except Exception as exc:  # noqa: BLE001 - always end the job, whatever happened
            log.error("joining Wi-Fi failed: %s", type(exc).__name__)
            result = {"ok": False, "error": "The TV box couldn't change its Wi-Fi."}
        if result.get("ok"):
            log.info("joined the Wi-Fi network %s", ssid)
            self.job = {"state": "joined", "ssid": ssid, "error": ""}
        else:
            log.warning("didn't join the Wi-Fi network %s: %s", ssid, result.get("error"))
            self.job = {"state": "failed", "ssid": ssid, "error": result.get("error") or "Couldn't join that network."}
        await on_done(self.job)

    async def _run_helper(self, ssid: str, password: str) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", HELPER, "set", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(json.dumps({"ssid": ssid, "password": password}).encode()), JOB_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return json.loads(out.decode() or "{}")
