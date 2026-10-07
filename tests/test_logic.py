from pathlib import Path

import pytest

from server.auth import LOCKOUT_SECONDS, MAX_FAILURES, Auth, Locked
from server.browser import Pointer, resolve_key
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
    assert len(BY_ID) == len(SERVICES) == 4
    assert all(s.url.startswith("https://") for s in SERVICES)


def test_media_keys_default_and_override():
    assert resolve_key("playpause", BY_ID["netflix"]) == "space"
    assert resolve_key("seek_fwd", BY_ID["netflix"]) == "right"
    assert resolve_key("playpause", BY_ID["youtube"]) == "mediaplaypause"
    assert resolve_key("playpause", None) == "space"
    assert resolve_key("up", None) == "up"
    with pytest.raises(KeyError):
        resolve_key("nope", None)


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
