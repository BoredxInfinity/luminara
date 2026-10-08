"""Self-update: check GitHub for new commits and hand installs to tvbox-update.service.

Checking runs as the normal user (`git fetch` in the clone). Installing needs root for
system files, so the controller only *starts* the root service (allowed by a sudoers
rule from install.sh); update.sh then pulls, applies what changed, and restarts us.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("tvbox.updater")

CHECK_EVERY = 6 * 3600  # seconds
FIRST_CHECK_AFTER = 90  # let the box finish booting first
UNIT = "tvbox-update.service"


async def _run(*args: str, cwd: Path | None = None, timeout: float = 90) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{args[0]} timed out"
    return proc.returncode, out.decode(errors="replace").strip()


class Updater:
    def __init__(self, repo: Path, data_dir: Path):
        self.repo = repo
        self._seen_file = data_dir / "last-version"
        self._watch: asyncio.Task | None = None
        self.status: dict = {
            "current": "", "current_subject": "", "latest": "", "commits": [],
            "available": False, "checked_at": None, "checking": False, "installing": False,
            "error": "", "just_updated": "", "can_install": False,
        }

    @property
    def applied_file(self) -> Path:
        return self.repo / ".git" / "tvbox-applied"  # written by update.sh / install.sh

    async def _git(self, *args: str, timeout: float = 60) -> str:
        code, out = await _run("git", *args, cwd=self.repo, timeout=timeout)
        if code != 0:
            raise RuntimeError(out.splitlines()[-1] if out else f"git {args[0]} failed")
        return out

    async def startup(self) -> None:
        """Note the running version, and whether we just came back from an update."""
        self.status["can_install"] = shutil.which("systemctl") is not None and Path("/etc/systemd/system", UNIT).exists()
        try:
            self.status["current"] = await self._git("rev-parse", "--short", "HEAD")
            self.status["current_subject"] = await self._git("log", "-1", "--format=%s")
        except (RuntimeError, OSError) as exc:
            log.warning("not a git checkout? %s", exc)
            return
        try:
            seen = self._seen_file.read_text().strip()
        except FileNotFoundError:
            seen = ""
        if seen and seen != self.status["current"]:
            self.status["just_updated"] = self.status["current"]
        self._seen_file.write_text(self.status["current"])
        if self.status["can_install"]:
            code, _ = await _run("systemctl", "is-failed", "--quiet", UNIT, timeout=10)
            if code == 0:  # last install failed: show why
                _, logs = await _run("journalctl", "-u", UNIT, "-n", "4", "--no-pager", "-o", "cat", timeout=10)
                self.status["error"] = "Last update failed: " + (logs.splitlines()[-1] if logs else "see journalctl -u tvbox-update")

    async def check(self) -> dict:
        if self.status["checking"]:
            return self.status
        self.status.update(checking=True, error="")
        try:
            await self._git("fetch", "--quiet", "origin", timeout=90)
            head = await self._git("rev-parse", "HEAD")
            upstream = await self._git("rev-parse", "@{u}")
            log_lines = await self._git("log", "--format=%h%x09%s", "--max-count=20", "HEAD..@{u}")
            commits = [dict(zip(("sha", "subject"), line.split("\t", 1))) for line in log_lines.splitlines() if "\t" in line]
            try:
                applied = self.applied_file.read_text().strip()
            except FileNotFoundError:
                applied = head  # never recorded: assume what's checked out is what's running
            pending_apply = applied != head  # pulled but not applied, e.g. power cut mid-update
            self.status.update(
                latest=upstream[:7], commits=commits,
                available=bool(commits) or pending_apply,
            )
            if pending_apply and not commits:
                self.status["commits"] = [{"sha": head[:7], "subject": "Finish installing the downloaded update"}]
        except (RuntimeError, OSError) as exc:
            self.status["error"] = f"Couldn't check for updates: {exc}"
            log.warning("update check failed: %s", exc)
        finally:
            self.status.update(checking=False, checked_at=int(time.time()))
        return self.status

    async def install(self, on_done) -> None:
        if not self.status["can_install"]:
            raise RuntimeError("Updates can only be installed on the TV box itself")
        if self.status["installing"]:
            return
        code, out = await _run("sudo", "-n", "systemctl", "start", "--no-block", UNIT, timeout=15)
        if code != 0:
            raise RuntimeError(out or "couldn't start the updater")
        self.status.update(installing=True, error="")
        self._watch = asyncio.create_task(self._watch_install(on_done))

    async def _watch_install(self, on_done) -> None:
        """A successful install restarts this server, so we only get to the end if
        nothing needed restarting or the install failed."""
        await asyncio.sleep(3)
        deadline = time.monotonic() + 35 * 60
        while time.monotonic() < deadline:
            _, state = await _run("systemctl", "is-active", UNIT, timeout=10)
            if state not in ("activating", "active", "reloading"):
                break
            await asyncio.sleep(3)
        _, state = await _run("systemctl", "is-active", UNIT, timeout=10)
        ok = state == "inactive"
        if not ok:
            _, logs = await _run("journalctl", "-u", UNIT, "-n", "4", "--no-pager", "-o", "cat", timeout=10)
            self.status["error"] = "Update failed: " + (logs.splitlines()[-1] if logs else state)
        self.status["installing"] = False
        await self.check()
        await on_done(ok)

    async def run_periodic(self, enabled, on_change) -> None:
        """Check soon after boot, then every few hours while auto-check is on."""
        await asyncio.sleep(FIRST_CHECK_AFTER)
        while True:
            if enabled():
                before = (self.status["available"], self.status["latest"])
                await self.check()
                if (self.status["available"], self.status["latest"]) != before:
                    await on_change()
            await asyncio.sleep(CHECK_EVERY)
