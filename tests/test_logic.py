from pathlib import Path

import pytest

from server.auth import LOCKOUT_SECONDS, MAX_FAILURES, Auth, Locked
from server.browser import Pointer, parse_combo, resolve_key
from server.config import ROOT, load_services, load_settings, service_for_url
from server.system import parse_volume

SERVICES = load_services(ROOT / "services.json")
BY_ID = {s.id: s for s in SERVICES}


@pytest.mark.parametrize("url,expected", [
    ("https://www.netflix.com/watch/123", "netflix"),
    ("https://netflix.com/", "netflix"),
    ("https://www.hotstar.com/in/home", "jiohotstar"),
    ("https://www.amazon.in/ap/signin", "prime"),
    ("https://www.youtube.com/tv#/watch", "youtube"),
    ("https://notnetflix.com/", None),
    ("http://127.0.0.1:8080/tv", None),
    ("about:blank", None),
])
def test_service_for_url(url, expected):
    svc = service_for_url(SERVICES, url)
    assert (svc.id if svc else None) == expected


def test_services_have_unique_ids_and_urls():
    assert len(BY_ID) == len(SERVICES) == 5
    assert all(s.url.startswith("https://") for s in SERVICES)


def test_media_keys_default_and_override():
    assert resolve_key("playpause", BY_ID["netflix"]) == "space"
    assert resolve_key("seek_fwd", BY_ID["netflix"]) == "right"
    assert resolve_key("playpause", BY_ID["youtube"]) == "mediaplaypause"
    assert resolve_key("playpause", None) == "space"
    assert resolve_key("up", None) == "up"
    assert resolve_key("seek_fwd", BY_ID["spotify"]) == "shift+right"
    with pytest.raises(KeyError):
        resolve_key("nope", None)


def test_parse_combo():
    assert parse_combo("right") == (0, "right")
    assert parse_combo("shift+right") == (8, "right")
    assert parse_combo("Ctrl+Shift+left") == (10, "left")
    for bad in ("hyper+right", "shift+nope", ""):
        with pytest.raises(KeyError):
            parse_combo(bad)


def test_pointer_coalesces_and_clamps():
    p = Pointer(100, 50)
    assert p.take() is None
    p.add(10, 5)
    p.add(10, 5)
    assert p.take() == (70, 35)
    p.add(1000, 1000)
    assert p.take() == (99, 49)
    p.add(5, 5)  # already at the corner: no change, nothing to send
    assert p.take() is None


def test_settings_from_env(tmp_path):
    s = load_settings({"TVBOX_PORT": "9000", "TVBOX_DATA_DIR": str(tmp_path), "TVBOX_PIN": "0"})
    assert s.port == 9000 and s.data_dir == tmp_path and not s.pin_enabled
    assert s.launcher_url == "http://127.0.0.1:9000/tv"


def test_pairing_and_lockout(tmp_path: Path):
    now = [1000.0]
    auth = Auth(tmp_path, clock=lambda: now[0])
    assert len(auth.pin) == 4 and auth.pin.isdigit()
    wrong = "0000" if auth.pin != "0000" else "1111"

    for _ in range(MAX_FAILURES - 1):
        assert auth.pair("1.2.3.4", wrong) is None
    assert auth.pair("1.2.3.4", wrong) is None  # this one triggers the lockout
    with pytest.raises(Locked):
        auth.pair("1.2.3.4", auth.pin)
    assert auth.pair("5.6.7.8", auth.pin)  # other devices unaffected

    now[0] += LOCKOUT_SECONDS + 1
    token = auth.pair("1.2.3.4", auth.pin)
    assert token and auth.is_valid(token)
    assert not auth.is_valid("bogus") and not auth.is_valid(None)

    reloaded = Auth(tmp_path)  # PIN and tokens persist
    assert reloaded.pin == auth.pin and reloaded.is_valid(token)
    assert (tmp_path / "pin").stat().st_mode & 0o777 == 0o600


def test_parse_volume():
    assert parse_volume("Volume: 0.40\n") == (40, False)
    assert parse_volume("Volume: 0.55 [MUTED]\n") == (55, True)
    assert parse_volume("garbage") is None


# ---- logos ------------------------------------------------------------------

from server.config import Art  # noqa: E402
from server.logos import looks_like, recolor_svg  # noqa: E402


def test_every_service_has_art_and_public_fields():
    for svc in SERVICES:
        pub = svc.public()
        assert pub["glyph"] and pub["color"].startswith("#") and pub["tile"]
        assert pub["logo"]["v"] == svc.logo.key and pub["icon"]["v"] == svc.icon.key


def test_art_key_changes_with_source_and_recolor():
    a = Art(url="https://x/logo.svg")
    assert a.key == Art(url="https://x/logo.svg").key
    assert a.key != Art(url="https://x/other.svg").key
    assert a.key != Art(url="https://x/logo.svg", recolor=(("#000", "#fff"),)).key


def test_recolor_svg():
    svg = b'<svg><path style="fill: rgb(40, 40, 40)"/><path fill="#FF0000"/></svg>'
    out = recolor_svg(svg, (("rgb(40, 40, 40)", "#ffffff"),))
    assert b"#ffffff" in out and b"#FF0000" in out and b"rgb(40" not in out


def test_looks_like_rejects_error_pages_and_truncated_pngs():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20 + b"IEND\xaeB`\x82"
    assert looks_like(png, "image/png")
    assert not looks_like(png[:-4], "image/png")  # cut short
    assert not looks_like(b"<!DOCTYPE html><html>", "image/png")
    assert looks_like(b'<?xml version="1.0"?><svg viewBox="0 0 1 1"/>', "image/svg+xml")
    assert not looks_like(b"<html>Too many requests</html>", "image/svg+xml")


def test_boot_volume_setting_is_clamped():
    assert load_settings({}).boot_volume == 100
    assert load_settings({"TVBOX_BOOT_VOLUME": "250"}).boot_volume == 100
    assert load_settings({"TVBOX_BOOT_VOLUME": "0"}).boot_volume == 0
