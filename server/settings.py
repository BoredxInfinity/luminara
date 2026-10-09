"""User settings, changed from the remote's Settings panel and saved to settings.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("tvbox.settings")

SAVER_CHOICES = (0, 1, 2, 5, 10, 15, 30)  # minutes; 0 = off
VOLUME_CHOICES = (0, 25, 50, 75, 100)  # percent at boot; 0 = leave alone
SPOTIFY_POLL_CHOICES = (5, 10, 15, 30, 60)  # seconds between "what's playing?" checks

DEFAULTS = {
    "hidden_apps": [],         # service ids not shown on the TV or remote
    "saver_minutes": 5,        # screensaver after this much idle time
    "saver_clock": True,       # show the time on the screensaver
    "boot_volume": 100,
    "prefer_h264": True,       # steer sites to hardware-decoded H.264 instead of VP9/AV1
    "lite_browser": True,      # fewer Chromium processes (no site isolation); needs a display restart
    "auto_update_check": True,
    "update_dismissed": "",    # "Later" on this version hides the prompt until a newer one
    "spotify_poll_seconds": 30,  # how often the screensaver asks Spotify what's playing
}

# Settings Chromium reads at startup; changing them means restarting the display.
NEEDS_DISPLAY_RESTART = {"lite_browser"}


def _valid(key: str, value, service_ids: set[str]) -> bool:
    if key == "hidden_apps":
        return isinstance(value, list) and all(v in service_ids for v in value)
    if key == "saver_minutes":
        return value in SAVER_CHOICES
    if key == "boot_volume":
        return value in VOLUME_CHOICES
    if key == "spotify_poll_seconds":
        return value in SPOTIFY_POLL_CHOICES
    if key == "update_dismissed":
        return isinstance(value, str) and len(value) <= 64
    return isinstance(value, bool) and isinstance(DEFAULTS[key], bool)


class UserSettings:
    def __init__(self, data_dir: Path, service_ids: set[str], boot_volume_default: int = 100):
        self._file = data_dir / "settings.json"
        self._service_ids = service_ids
        self.values = {**DEFAULTS, "boot_volume": boot_volume_default if boot_volume_default in VOLUME_CHOICES else 100}
        try:
            saved = json.loads(self._file.read_text())
            if isinstance(saved.get("hidden_apps"), list):  # forget apps that no longer exist
                saved["hidden_apps"] = [a for a in saved["hidden_apps"] if a in service_ids]
            self.values.update({k: v for k, v in saved.items() if k in DEFAULTS and _valid(k, v, service_ids)})
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError) as exc:
            log.warning("ignoring unreadable %s: %s", self._file, exc)

    def __getitem__(self, key: str):
        return self.values[key]

    def update(self, changes: dict) -> set[str]:
        """Apply valid changes; returns the keys that actually changed. Raises ValueError on bad input."""
        for key, value in changes.items():
            if key not in DEFAULTS or not _valid(key, value, self._service_ids):
                raise ValueError(f"invalid setting {key}={value!r}")
        changed = {k for k, v in changes.items() if self.values[k] != v}
        if changed:
            self.values.update(changes)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=1))
            tmp.replace(self._file)
        return changed

    def chromium_env(self) -> str:
        """Shell snippet that deploy/kiosk.sh sources to add Chromium flags."""
        features, flags = [], []
        if self.values["lite_browser"]:
            # One renderer for the whole site instead of one per origin: much less memory.
            features += ["IsolateOrigins", "site-per-process"]
            flags += ["--disable-site-isolation-trials", "--renderer-process-limit=3", "--disable-smooth-scrolling"]
        return f'TVBOX_DISABLE_FEATURES="{",".join(features)}"\nTVBOX_EXTRA_FLAGS="{" ".join(flags)}"\n'
