"""Service logos and app icons: downloaded once from the URLs in services.json,
cached on disk, and served to the TV and remote as /logos/<id>/<logo|icon>.

Brand artwork is fetched at runtime rather than committed to the repo. If a
download fails the pages fall back to the service's glyph, and we retry later.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from .config import Art, Service

log = logging.getLogger("tvbox.logos")

MAX_BYTES = 2 * 1024 * 1024
RETRY_AFTER = 120  # seconds before retrying a failed download
SPACING = 1.0  # seconds between downloads; Wikimedia rate-limits bursts
USER_AGENT = "Luminara-TV-box/1.0 (personal media launcher; github.com/BoredxInfinity/luminara)"
TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon"}


def looks_like(data: bytes, ctype: str) -> bool:
    """Cheap check that a response is the image it claims to be, not an error page."""
    if ctype == "image/svg+xml":
        return b"<svg" in data[:4096]
    if ctype == "image/png":  # complete files end with the IEND chunk
        return data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
    if ctype == "image/jpeg":
        return data.startswith(b"\xff\xd8")
    return len(data) > 0


def recolor_svg(data: bytes, swaps: tuple[tuple[str, str], ...]) -> bytes:
    text = data.decode("utf-8")
    for old, new in swaps:
        text = text.replace(old, new)
    return text.encode("utf-8")


class Logos:
    def __init__(self, data_dir: Path, services: list[Service]):
        self.dir = data_dir / "logos"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.services = {s.id: s for s in services}
        self._failed: dict[str, float] = {}
        self._lock = asyncio.Lock()  # one download at a time, spaced out
        self._last_fetch = 0.0

    def _cached(self, key: str) -> Path | None:
        return next((p for p in self.dir.glob(f"{key}.*") if p.suffix in TYPES), None)

    async def get(self, sid: str, kind: str, http: aiohttp.ClientSession) -> tuple[Path, str] | None:
        svc = self.services.get(sid)
        art = svc.art(kind) if svc else None
        if not art:
            return None
        key = art.key
        path = self._cached(key)
        if path is None:
            async with self._lock:
                path = self._cached(key)  # another request may have just fetched it
                if path is None:
                    if time.monotonic() - self._failed.get(key, -RETRY_AFTER) < RETRY_AFTER:
                        return None
                    path = await self._download(key, art, http, label=f"{sid} {kind}")
                    if path is None:
                        self._failed[key] = time.monotonic()
                        return None
        return path, TYPES[path.suffix]

    async def _download(self, key: str, art: Art, http: aiohttp.ClientSession, label: str) -> Path | None:
        await asyncio.sleep(max(0.0, self._last_fetch + SPACING - time.monotonic()))
        self._last_fetch = time.monotonic()
        try:
            async with http.get(art.url, headers={"User-Agent": USER_AGENT},
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                if resp.status != 200 or ctype not in TYPES.values():
                    raise ValueError(f"HTTP {resp.status} {ctype or 'no type'}")
                data = bytearray()  # read() would return just the first chunk
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise ValueError("too large")
                data = bytes(data)
                if not looks_like(data, ctype):
                    raise ValueError(f"body isn't a real {ctype}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("couldn't fetch %s from %s: %s", label, art.url, exc)
            return None
        if art.recolor and ctype == "image/svg+xml":
            data = recolor_svg(data, art.recolor)
        ext = next(e for e, t in TYPES.items() if t == ctype)
        path = self.dir / f"{key}{ext}"
        tmp = self.dir / f"{key}.part"
        tmp.write_bytes(data)
        tmp.replace(path)
        log.info("cached %s (%d bytes)", label, len(data))
        return path

    async def prefetch(self, http: aiohttp.ClientSession) -> None:
        """Fetch everything up front, retrying failures, and drop files nothing uses any more."""
        wanted = {a.key for s in self.services.values() for a in (s.logo, s.icon) if a}
        for old in self.dir.iterdir():
            if old.stem not in wanted:
                old.unlink(missing_ok=True)
        for _ in range(10):
            missing = [(sid, kind) for sid, svc in self.services.items() for kind in ("logo", "icon")
                       if svc.art(kind) and not self._cached(svc.art(kind).key)]
            if not missing:
                return
            for sid, kind in missing:
                await self.get(sid, kind, http)
            await asyncio.sleep(RETRY_AFTER + 1)
