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


def test_dpad_services():
    by_id = {s.id: s for s in SERVICES}
    assert by_id["netflix"].dpad and by_id["netflix"].dpad_cards is None
    assert by_id["jiohotstar"].dpad and by_id["jiohotstar"].dpad_cards
    assert not by_id["youtube"].dpad  # YouTube's TV interface handles arrows itself


def test_mirror_touches_map_onto_the_tv_and_move_the_cursor(tmp_path):
    from server.browser import Browser, _frac
    from server.config import Settings

    b = Browser(Settings(data_dir=tmp_path), SERVICES, lambda: None)
    b.pointer.resize(1920, 1080)
    assert b._at(0.5, 0.5) == (959.5, 539.5)
    assert (b.pointer.x, b.pointer.y) == (959.5, 539.5)  # the touchpad carries on from here
    assert b._at(-3, 9) == (0.0, 1079.0)                 # off the picture: clamped to the edge
    assert _frac(5) == 1.0 and _frac("-0.25") == -0.25


def test_mirror_join_survives_a_slow_snapshot():
    import asyncio

    from server.mirror import Mirror

    class SlowBrowser:
        on_frame = None
        casting = None

        async def screencast(self, on):
            self.casting = on

        async def snapshot(self):
            raise asyncio.TimeoutError  # a busy Pi took too long

    class Phone:
        closed = False

    async def run():
        browser, phone = SlowBrowser(), Phone()
        mirror = Mirror(browser)
        await mirror.join(phone)  # must not raise: the phone stays connected
        assert phone in mirror.viewers and browser.casting is True
        await mirror.leave(phone)
        assert not mirror.viewers and browser.casting is False

    asyncio.run(run())


def test_going_home_from_an_app_frees_memory_safely(tmp_path, monkeypatch):
    import asyncio

    import server.browser as browser_mod
    from server.browser import Browser
    from server.config import Settings

    monkeypatch.setattr(browser_mod, "RELEASE_AFTER", 0)
    b = Browser(Settings(data_dir=tmp_path), SERVICES, lambda: None)
    sent = []

    async def fake_send(method, params=None, **kw):
        sent.append((method, params))
        return {}

    b.send = fake_send

    async def run():
        b.state["view"] = "service"            # Spotify was open
        await b.home()
        await asyncio.gather(*b._tasks)
        # Only the moderate signal: "critical" and forciblyPurgeJavaScriptMemory kill the page.
        assert ("Memory.simulatePressureNotification", {"level": "moderate"}) in sent
        assert not any(m == "Memory.forciblyPurgeJavaScriptMemory" for m, _ in sent)
        sent.clear()
        b.state["view"] = "launcher"           # already home: nothing to free
        await b.home()
        await asyncio.gather(*b._tasks)
        assert not any(m.startswith("Memory.") for m, _ in sent)

    asyncio.run(run())


class _Socket:
    """Stands in for an aiohttp WebSocketResponse."""

    def __init__(self):
        self.sent, self.closed = [], False

    async def send_str(self, text):
        import json
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class _CastBrowser:
    def __init__(self):
        self.state = {"view": "service"}
        self.calls = []

    async def open_cast(self):
        self.calls.append("open")
        self.state["view"] = "cast"

    async def end_cast(self):
        self.calls.append("end")
        self.state["view"] = "service"


def test_cast_switches_the_tv_relays_and_switches_back():
    import asyncio

    from server.cast import Cast

    async def run():
        browser = _CastBrowser()
        cast = Cast(browser)
        laptop, tv = _Socket(), _Socket()
        await cast.sender_joined(laptop)            # TV switches to the receiver page
        assert browser.calls == ["open"]
        await cast.tv_joined(tv)                    # receiver up: the laptop may offer
        assert laptop.sent == [{"t": "ready"}]
        await cast.relay(laptop, {"t": "offer", "sdp": {"type": "offer"}})
        await cast.relay(tv, {"t": "answer", "sdp": {"type": "answer"}})
        await cast.relay(laptop, {"t": "nonsense"})  # not relayed
        assert tv.sent == [{"t": "offer", "sdp": {"type": "offer"}}]
        assert laptop.sent[-1]["t"] == "answer"
        await cast.relay(laptop, {"t": "stop"})     # TV goes back to what it showed
        assert browser.calls == ["open", "end"] and not cast.active
        assert tv.sent[-1] == {"t": "stop"}

    asyncio.run(run())


def test_a_second_laptop_takes_over_and_home_on_the_tv_ends_sharing(monkeypatch):
    import asyncio

    import server.cast as cast_mod
    from server.cast import Cast

    monkeypatch.setattr(cast_mod, "TV_GRACE", 0)

    async def run():
        browser = _CastBrowser()
        cast = Cast(browser)
        first, second, tv = _Socket(), _Socket(), _Socket()
        await cast.sender_joined(first)
        await cast.tv_joined(tv)
        await cast.sender_joined(second)
        assert first.closed and first.sent[-1] == {"t": "ended", "reason": "replaced"}
        assert second.sent == [{"t": "ready"}]      # receiver already up
        browser.state["view"] = "launcher"          # someone pressed Home
        await cast.tv_left(tv)
        assert second.sent[-1] == {"t": "ended", "reason": "tv"} and not cast.active

    asyncio.run(run())


@pytest.mark.skipif(not __import__("shutil").which("mkcert"), reason="mkcert isn't installed")
def test_certificate_is_made_once_and_the_authority_key_is_deleted(tmp_path):
    import ssl

    from server.tls import ensure_certificate

    cert = ensure_certificate(tmp_path, "192.168.1.50")
    assert cert and cert.cert.exists() and cert.ca.exists()
    assert not (tmp_path / "tls" / "ca" / "rootCA-key.pem").exists()  # can't sign anything else
    assert "192.168.1.50" in cert.names and "localhost" in cert.names
    ctx = ssl.create_default_context(cafile=str(cert.ca))  # the CA devices download is valid
    assert ctx.cert_store_stats()["x509_ca"] == 1
    cert.context()                                        # loads as a server certificate
    again = ensure_certificate(tmp_path, "192.168.1.99")  # IP changed: keep the trusted one
    assert again.cert.read_bytes() == cert.cert.read_bytes()
