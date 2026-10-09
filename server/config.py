"""Settings and the services registry (services.json)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# Keys sent for media actions unless a service overrides them in services.json.
DEFAULT_MEDIA_KEYS = {"playpause": "space", "seek_fwd": "right", "seek_back": "left"}


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8080
    cdp_url: str = "http://127.0.0.1:9222"
    data_dir: Path = Path.home() / ".local" / "state" / "tvbox"
    services_file: Path = ROOT / "services.json"
    pin_enabled: bool = True
    boot_volume: int = 100  # percent set once per boot; 0 leaves volume alone
    https_port: int = 8443  # the secure remote (screen sharing); 0 turns it off

    @property
    def launcher_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/tv"

    @property
    def cast_url(self) -> str:  # the TV side of screen sharing
        return f"http://127.0.0.1:{self.port}/cast"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    return Settings(
        host=env.get("TVBOX_HOST", s.host),
        port=int(env.get("TVBOX_PORT", s.port)),
        cdp_url=env.get("TVBOX_CDP", s.cdp_url).rstrip("/"),
        data_dir=Path(env.get("TVBOX_DATA_DIR", s.data_dir)).expanduser(),
        services_file=Path(env.get("TVBOX_SERVICES", s.services_file)).expanduser(),
        pin_enabled=env.get("TVBOX_PIN", "1") not in ("0", "false", "no"),
        boot_volume=max(0, min(100, int(env.get("TVBOX_BOOT_VOLUME", s.boot_volume)))),
        https_port=int(env.get("TVBOX_HTTPS_PORT", s.https_port)),
    )


@dataclass(frozen=True)
class Art:
    """A logo or app icon fetched from `url` and cached by the server (server/logos.py).

    recolor: SVG colour swaps, e.g. dark wordmark text -> white for dark tiles.
    filter:  "white" renders the image as a white silhouette.
    bg/size/position: how the remote draws a square icon (CSS background values).
    """

    url: str
    recolor: tuple[tuple[str, str], ...] = ()
    filter: str = ""
    bg: str = ""
    size: str = ""
    position: str = ""

    @classmethod
    def parse(cls, raw: dict | None) -> "Art | None":
        if not raw or not raw.get("url"):
            return None
        return cls(
            url=raw["url"],
            recolor=tuple((raw.get("recolor") or {}).items()),
            filter=raw.get("filter", ""),
            bg=raw.get("bg", ""),
            size=raw.get("size", ""),
            position=raw.get("position", ""),
        )

    @property
    def key(self) -> str:
        """Cache key and URL version: same source and recolouring means the same file."""
        return hashlib.sha1(repr((self.url, self.recolor)).encode()).hexdigest()[:12]

    def style(self) -> dict:
        extra = (("filter", self.filter), ("bg", self.bg), ("size", self.size), ("position", self.position))
        return {"v": self.key, **{k: v for k, v in extra if v}}


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    url: str
    glyph: str = ""
    tagline: str = ""
    color: str = "#7c5cff"
    tile: str = ""
    logo: Art | None = None
    icon: Art | None = None
    match: tuple[str, ...] = ()
    keys: dict[str, str] = field(default_factory=dict)
    user_agent: str | None = None
    dpad: bool = False              # arrow keys navigate the site (web/inject/dpad.js)
    dpad_cards: str | None = None   # CSS for title cards the site doesn't mark clickable

    def media_key(self, action: str) -> str:
        return self.keys.get(action) or DEFAULT_MEDIA_KEYS[action]

    def art(self, kind: str) -> Art | None:
        return {"logo": self.logo, "icon": self.icon}.get(kind)

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "glyph": self.glyph or self.name[:1],
            "tagline": self.tagline, "color": self.color, "tile": self.tile or self.color,
            "logo": self.logo.style() if self.logo else None,
            "icon": self.icon.style() if self.icon else None,
        }


def load_services(path: Path) -> list[Service]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    services = []
    seen = set()
    for item in raw:
        sid = item["id"]
        if sid in seen:
            raise ValueError(f"duplicate service id {sid!r} in {path}")
        seen.add(sid)
        match = item.get("match") or [urlsplit(item["url"]).hostname]
        dpad = item.get("dpad") or False  # true, or {"cards": "<css>"}
        services.append(
            Service(
                id=sid,
                name=item["name"],
                url=item["url"],
                glyph=item.get("glyph", ""),
                tagline=item.get("tagline", ""),
                color=item.get("color", "#7c5cff"),
                tile=item.get("tile", ""),
                logo=Art.parse(item.get("logo")),
                icon=Art.parse(item.get("icon")),
                match=tuple(m.lower() for m in match),
                keys=dict(item.get("keys", {})),
                user_agent=item.get("user_agent"),
                dpad=bool(dpad),
                dpad_cards=dpad.get("cards") if isinstance(dpad, dict) else None,
            )
        )
    return services


def service_for_url(services: list[Service], url: str) -> Service | None:
    """Return the service whose domain list matches the URL's host (suffix match)."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    for svc in services:
        for domain in svc.match:
            if host == domain or host.endswith("." + domain):
                return svc
    return None
