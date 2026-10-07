"""Pairing PIN for remotes. A remote enters the PIN once and gets a cookie token."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("tvbox.auth")

COOKIE = "tvbox_token"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


class Locked(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"too many attempts; retry in {retry_after}s")
        self.retry_after = retry_after


class Auth:
    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.monotonic):
        self._dir = data_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.pin = self._load_pin()
        self._tokens = self._load_tokens()
        self._failures: dict[str, tuple[int, float]] = {}  # ip -> (count, locked_until)

    # ---- persistence -----------------------------------------------------

    def _load_pin(self) -> str:
        path = self._dir / "pin"
        try:
            pin = path.read_text().strip()
            if pin.isdigit() and len(pin) == 4:
                return pin
        except FileNotFoundError:
            pass
        pin = f"{secrets.randbelow(10_000):04d}"
        _write_private(path, pin + "\n")
        log.info("generated new remote PIN")
        return pin

    def _load_tokens(self) -> set[str]:
        try:
            return set(json.loads((self._dir / "tokens.json").read_text()))
        except (FileNotFoundError, ValueError):
            return set()

    def _save_tokens(self) -> None:
        _write_private(self._dir / "tokens.json", json.dumps(sorted(self._tokens)))

    # ---- API -------------------------------------------------------------

    def is_valid(self, token: str | None) -> bool:
        return bool(token) and any(secrets.compare_digest(token, t) for t in self._tokens)

    def pair(self, ip: str, pin: str) -> str | None:
        """Returns a new token for the right PIN, None for a wrong one; raises Locked."""
        now = self._clock()
        count, locked_until = self._failures.get(ip, (0, 0.0))
        if locked_until > now:
            raise Locked(int(locked_until - now) + 1)
        if not secrets.compare_digest(str(pin).strip(), self.pin):
            count += 1
            if count >= MAX_FAILURES:
                self._failures[ip] = (0, now + LOCKOUT_SECONDS)
            else:
                self._failures[ip] = (count, 0.0)
            return None
        self._failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        self._save_tokens()
        return token


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
