"""Settings and the services registry (services.json)."""

from __future__ import annotations

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

    @property
    def launcher_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/tv"


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
    )


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    url: str
    icon: str = ""
    color: str = "#333"
    match: tuple[str, ...] = ()
    keys: dict[str, str] = field(default_factory=dict)
    user_agent: str | None = None

    def media_key(self, action: str) -> str:
        return self.keys.get(action) or DEFAULT_MEDIA_KEYS[action]

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "icon": self.icon, "color": self.color}


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
        services.append(
            Service(
                id=sid,
                name=item["name"],
                url=item["url"],
                icon=item.get("icon", ""),
                color=item.get("color", "#333"),
                match=tuple(m.lower() for m in match),
                keys=dict(item.get("keys", {})),
                user_agent=item.get("user_agent"),
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
