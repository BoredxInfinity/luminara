"""Volume (PipeWire via wpctl) and power. Becomes a no-op where wpctl is missing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import time

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


def _meminfo() -> dict[str, int]:
    """/proc/meminfo in MB."""
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0].rstrip(":")] = int(parts[1]) // 1024
    return out


def kiosk_browser(profile_marker: str = "tvbox-chromium") -> tuple[int, float] | None:
    """The kiosk's Chromium browser process (not its helpers): (pid, seconds running).
    Only ever the one using the TV box's own profile, never another Chromium."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
        boot = time.time() - float((_read("/proc/uptime") or "0").split()[0])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            if not args or b"chrom" not in os.path.basename(args[0]):
                continue
            if any(a.startswith(b"--type=") for a in args) or not any(profile_marker.encode() in a for a in args):
                continue
            stat = (_read(f"/proc/{pid}/stat") or "").rsplit(")", 1)[1].split()
            started = boot + int(stat[19]) / hz
            return int(pid), time.time() - started
        except (OSError, IndexError, ValueError):
            continue
    return None


async def restart_kiosk_browser(reason: str) -> bool:
    """End the kiosk's Chromium; tvbox-kiosk.service starts a fresh one within seconds.
    (It runs as this same user, so no special rights are needed.)"""
    found = kiosk_browser()
    if not found:
        return False
    pid = found[0]
    log.warning("restarting Chromium (%s)", reason)
    try:
        os.kill(pid, signal.SIGTERM)  # lets it save its profile and exit cleanly
        for _ in range(20):
            await asyncio.sleep(0.5)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)  # hung too badly to exit by itself
    except ProcessLookupError:
        pass
    return True


async def health() -> dict:
    """A snapshot for the log: memory, swap, load, temperature and any power trouble."""
    mem = _meminfo()
    info = {
        "mem_available_mb": mem.get("MemAvailable"),
        "swap_used_mb": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) if "SwapTotal" in mem else None,
        "load": float((_read("/proc/loadavg") or "0").split()[0]),
        "temp_c": None, "throttled": None,
    }
    if temp := _read("/sys/class/thermal/thermal_zone0/temp"):
        info["temp_c"] = round(int(temp) / 1000, 1)
    if shutil.which("vcgencmd"):
        try:
            out = await _run("vcgencmd", "get_throttled")  # "throttled=0x50000"
            info["throttled"] = int(out.strip().split("=")[1], 16)
        except (RuntimeError, IndexError, ValueError):
            pass
    browser = kiosk_browser()
    if browser:
        info["chromium_hours"] = round(browser[1] / 3600, 1)
    return info


# vcgencmd get_throttled bits that are true right now (the higher bits mean "since boot").
THROTTLE_NOW = {0: "under-voltage", 1: "CPU speed capped", 2: "throttled", 3: "soft temperature limit"}


def throttle_problems(bits: int | None) -> list[str]:
    return [name for bit, name in THROTTLE_NOW.items() if bits and bits & (1 << bit)]


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
