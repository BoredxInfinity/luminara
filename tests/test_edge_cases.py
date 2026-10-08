import asyncio
from pathlib import Path

import pytest

import server.updater as updater_mod
from server.browser import Browser
from server.config import ROOT, load_services, load_settings
from server.updater import Updater


def fake_run(script):
    """Replace updater._run with canned (code, output) answers keyed by the command."""
    calls = []

    async def run(*args, cwd=None, timeout=90):
        calls.append(args)
        for prefix, answers in script.items():
            if args[: len(prefix)] == prefix:
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return 0, ""
    return run, calls


def test_failed_install_clears_installing_and_reports(monkeypatch, tmp_path: Path):
    run, calls = fake_run({
        ("sudo",): [(0, "")],
        ("systemctl", "is-active"): [(0, "activating"), (3, "failed")],
        ("journalctl",): [(0, "fatal: unable to access GitHub")],
    })
    monkeypatch.setattr(updater_mod, "_run", run)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(updater_mod.asyncio, "sleep", lambda s: real_sleep(0))
    up = Updater(tmp_path, tmp_path)
    up.status["can_install"] = True

    async def no_check():
        return up.status
    monkeypatch.setattr(up, "check", no_check)
    results = []

    async def done(ok):
        results.append(ok)

    async def scenario():
        await up.install(done)
        assert up.status["installing"]
        await up.install(done)  # a second tap while installing does nothing
        await up._watch
    asyncio.run(scenario())
    assert results == [False]
    assert up.status["installing"] is False and "unable to access GitHub" in up.status["error"]
    assert sum(1 for c in calls if c[0] == "sudo") == 1


def test_install_refused_off_the_box(tmp_path: Path):
    up = Updater(tmp_path, tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(up.install(None))


def test_reattach_runs_once_when_both_events_fire(tmp_path: Path):
    settings = load_settings({"TVBOX_DATA_DIR": str(tmp_path)})
    b = Browser(settings, load_services(ROOT / "services.json"), lambda: None)
    attaches = []

    async def slow_attach():
        attaches.append(1)
        await asyncio.sleep(0.05)
    b._attach = slow_attach

    async def scenario():
        await asyncio.gather(b._reattach(), b._reattach())  # targetDestroyed + detachedFromTarget
        await b._reattach()  # a later, separate loss still re-attaches
    asyncio.run(scenario())
    assert len(attaches) == 2
