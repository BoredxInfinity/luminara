import asyncio
import subprocess
from pathlib import Path

import pytest

from server.settings import DEFAULTS, UserSettings
from server.updater import Updater

IDS = {"netflix", "spotify"}


def test_settings_defaults_persist_and_validate(tmp_path: Path):
    s = UserSettings(tmp_path, IDS)
    assert s.values == DEFAULTS
    assert s.update({"saver_minutes": 10, "hidden_apps": ["spotify"]}) == {"saver_minutes", "hidden_apps"}
    assert s.update({"saver_minutes": 10}) == set()  # no-op
    for bad in ({"saver_minutes": 7}, {"hidden_apps": ["nope"]}, {"lite_browser": "yes"}, {"unknown": 1}):
        with pytest.raises(ValueError):
            s.update(bad)
    again = UserSettings(tmp_path, IDS)
    assert again["saver_minutes"] == 10 and again["hidden_apps"] == ["spotify"]


def test_settings_ignore_corrupt_or_stale_values(tmp_path: Path):
    (tmp_path / "settings.json").write_text('{"saver_minutes": 999, "hidden_apps": ["gone"], "saver_clock": false}')
    s = UserSettings(tmp_path, IDS)
    assert s["saver_minutes"] == DEFAULTS["saver_minutes"] and s["hidden_apps"] == [] and s["saver_clock"] is False


def test_chromium_env_follows_lite_mode(tmp_path: Path):
    s = UserSettings(tmp_path, IDS)
    assert "site-per-process" in s.chromium_env() and "--renderer-process-limit" in s.chromium_env()
    s.update({"lite_browser": False})
    assert s.chromium_env() == 'TVBOX_DISABLE_FEATURES=""\nTVBOX_EXTRA_FLAGS=""\n'


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path):
    """An 'origin' with one commit, and a 'box' clone of it."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first")
    git(tmp_path, "clone", "-q", "--bare", str(work), "origin.git")
    git(work, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "box")
    (tmp_path / "data").mkdir()
    return work, tmp_path / "box", tmp_path / "data"


def test_updater_detects_new_commits_and_pending_apply(repos):
    work, box, data = repos
    up = Updater(box, data)
    asyncio.run(up.startup())
    assert asyncio.run(up.check())["available"] is False

    git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "Add Spotify")
    git(work, "push", "-q", "origin", "main")
    st = asyncio.run(up.check())
    assert st["available"] and [c["subject"] for c in st["commits"]] == ["Add Spotify"]

    git(box, "pull", "-q")  # pulled, but the recorded applied version is the old one
    (box / ".git" / "tvbox-applied").write_text(git(box, "rev-parse", "HEAD~1"))
    st = asyncio.run(up.check())
    assert st["available"] and "Finish installing" in st["commits"][0]["subject"]

    (box / ".git" / "tvbox-applied").write_text(git(box, "rev-parse", "HEAD"))
    assert asyncio.run(up.check())["available"] is False


def test_updater_notices_it_was_just_updated(repos):
    work, box, data = repos
    (data / "last-version").write_text("0000000")
    up = Updater(box, data)
    asyncio.run(up.startup())
    assert up.status["just_updated"] == up.status["current"]
    assert asyncio.run(Updater(box, data).startup()) is None  # second start: nothing new
