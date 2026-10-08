"""Volume (PipeWire via wpctl) and power. Becomes a no-op where wpctl is missing."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil

log = logging.getLogger("tvbox.system")

SINK = "@DEFAULT_AUDIO_SINK@"
STEP = "5%"
_VOLUME_RE = re.compile(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?")


def parse_volume(output: str) -> tuple[int, bool] | None:
    """'Volume: 0.40 [MUTED]' -> (40, True)."""
    m = _VOLUME_RE.search(output)
    if not m:
        return None
    return round(float(m.group(1)) * 100), bool(m.group(2))


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def system_info() -> dict:
    """Uptime, temperature and memory for the Settings panel (Linux; None elsewhere)."""
    info: dict = {"uptime_s": None, "cpu_temp_c": None, "mem_total_mb": None, "mem_available_mb": None, "load": None}
    if up := _read("/proc/uptime"):
        info["uptime_s"] = int(float(up.split()[0]))
    if temp := _read("/sys/class/thermal/thermal_zone0/temp"):
        info["cpu_temp_c"] = round(int(temp) / 1000, 1)
    if mem := _read("/proc/meminfo"):
        fields = {line.split(":")[0]: int(line.split()[1]) for line in mem.splitlines() if line.split()[1:2]}
        info["mem_total_mb"] = fields.get("MemTotal", 0) // 1024
        info["mem_available_mb"] = fields.get("MemAvailable", 0) // 1024
    if load := _read("/proc/loadavg"):
        info["load"] = float(load.split()[0])
    return info


async def _run(*args: str, timeout: float = 5) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{args[0]} timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"{args[0]} exited {proc.returncode}")
    return out.decode()


class System:
    def __init__(self) -> None:
        self.has_audio = shutil.which("wpctl") is not None
        if not self.has_audio:
            log.warning("wpctl not found; volume control disabled")

    async def volume(self) -> dict:
        if not self.has_audio:
            return {"volume": None, "muted": False}
        try:
            parsed = parse_volume(await _run("wpctl", "get-volume", SINK))
        except RuntimeError as exc:
            log.warning("get-volume failed: %s", exc)
            parsed = None
        if not parsed:
            return {"volume": None, "muted": False}
        return {"volume": parsed[0], "muted": parsed[1]}

    async def set_volume(self, action: str) -> dict:
        if action not in ("up", "down", "mute"):
            raise ValueError(action)
        if self.has_audio:
            if action == "mute":
                await _run("wpctl", "set-mute", SINK, "toggle")
            else:
                await _run("wpctl", "set-volume", "-l", "1.0", SINK, STEP + ("+" if action == "up" else "-"))
        return await self.volume()

    async def set_level(self, percent: int) -> dict:
        """Set an absolute volume and unmute. Raises RuntimeError if PipeWire isn't up yet."""
        if self.has_audio:
            await _run("wpctl", "set-volume", SINK, f"{percent / 100:.2f}")
            await _run("wpctl", "set-mute", SINK, "0")
        return await self.volume()

    async def power(self, action: str) -> None:
        if action not in ("reboot", "poweroff"):
            raise ValueError(action)
        # A sudoers rule from install.sh allows exactly these commands.
        await _run("sudo", "-n", "systemctl", action)

    async def restart_display(self) -> None:
        await _run("sudo", "-n", "systemctl", "restart", "tvbox-kiosk", timeout=15)
