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
    ("https://open.spotify.com/", None),  # no longer an app
    ("https://notnetflix.com/", None),
    ("http://127.0.0.1:8080/tv", None),
    ("about:blank", None),
])
def test_service_for_url(url, expected):
    svc = service_for_url(SERVICES, url)
    assert (svc.id if svc else None) == expected


def test_services_have_unique_ids_and_urls():
    assert len(BY_ID) == len(SERVICES) == 4
    assert "spotify" not in BY_ID  # music shows on the screensaver instead (server/spotify.py)
    assert all(s.url.startswith("https://") for s in SERVICES)


def test_media_keys_default_and_override():
    assert resolve_key("playpause", BY_ID["netflix"]) == "space"
    assert resolve_key("seek_fwd", BY_ID["netflix"]) == "right"
    assert resolve_key("playpause", BY_ID["youtube"]) == "mediaplaypause"
    assert resolve_key("playpause", None) == "space"
    assert resolve_key("up", None) == "up"
    from server.config import Service
    shifted = Service(id="x", name="X", url="https://x.example/", keys={"seek_fwd": "shift+right"})
    assert resolve_key("seek_fwd", shifted) == "shift+right"
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


def test_clear_to_home_closes_strays_unloads_the_app_and_frees_memory(tmp_path, monkeypatch):
    import asyncio

    import server.browser as browser_mod
    from server.browser import Browser
    from server.config import Settings

    monkeypatch.setattr(browser_mod, "RELEASE_AFTER", 0)
    b = Browser(Settings(data_dir=tmp_path), SERVICES, lambda: None)
    b._target = "tv"
    b.state.update(view="service", url="https://open.spotify.com/")
    sent = []

    async def fake_send(method, params=None, **kw):
        sent.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "tv", "url": "https://open.spotify.com/"},
                                    {"type": "page", "targetId": "popup", "url": "https://ads.example/"}]}
        return {}

    b.send = fake_send
    asyncio.run(b.clear_to_home())
    methods = [m for m, _ in sent]
    assert ("Target.closeTarget", {"targetId": "popup"}) in sent
    assert ("Page.navigate", {"url": b.settings.launcher_url}) in sent
    assert methods.index("Page.navigate") < methods.index("Memory.simulatePressureNotification")


def test_spotify_turntable_shows_while_playing_and_returns_to_the_clock_after_a_quiet_minute():
    from server.main import SPOTIFY_QUIET, spotify_step

    song = {"title": "Closer", "playing": True}
    paused = {**song, "playing": False}
    act, shown, quiet = spotify_step(None, 0, False, None)          # nothing playing: clock
    assert act is None and not shown
    act, shown, quiet = spotify_step(paused, 5, shown, quiet)       # paused alone doesn't show
    assert act is None and not shown
    act, shown, quiet = spotify_step(song, 10, shown, quiet)        # playing: turntable
    assert act == "show" and shown and quiet is None
    act, shown, quiet = spotify_step(paused, 40, shown, quiet)      # paused: stays, paused
    assert act == "show" and shown and quiet == 40
    act, shown, quiet = spotify_step(None, 40 + SPOTIFY_QUIET - 1, shown, quiet)  # under a minute
    assert act is None and shown
    act, shown, quiet = spotify_step(None, 40 + SPOTIFY_QUIET, shown, quiet)      # a minute: clock
    assert act == "clear" and not shown


def test_spotify_login_link_uses_pkce_and_a_loopback_redirect(tmp_path):
    import asyncio
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    from server.spotify import Spotify, SpotifyError

    sp = Spotify(tmp_path, 8080)
    with pytest.raises(SpotifyError):
        sp.login_url("not a client id!")
    q = parse_qs(urlsplit(sp.login_url("0123456789abcdef0123456789abcdef")).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8080/spotify/callback"]  # Spotify allows loopback http
    assert q["code_challenge_method"] == ["S256"] and "client_secret" not in q
    state = q["state"][0]
    verifier = sp._pending[state]["verifier"]
    assert q["code_challenge"][0] == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    sent = {}

    async def fake_token(http, client_id, form):
        sent.update(form, client_id=client_id)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    async def fake_get(http, path):
        return {"display_name": "Arav"}

    sp._token_request, sp._get = fake_token, fake_get
    pasted = f"http://127.0.0.1:8080/spotify/callback?code=C0DE&state={state}"
    assert asyncio.run(sp.finish(None, pasted)) == "Arav"
    assert sent["code_verifier"] == verifier and sent["code"] == "C0DE"
    assert sp.connected and (tmp_path / "spotify.json").stat().st_mode & 0o077 == 0  # private file
    with pytest.raises(SpotifyError):                     # a login link works once
        asyncio.run(sp.finish(None, pasted))


def test_spotify_player_answer_becomes_turntable_info(tmp_path):
    import asyncio

    from server.spotify import Spotify

    sp = Spotify(tmp_path, 8080)
    answers = {
        "song": {"is_playing": True, "device": {"name": "Arav's iPhone"}, "item": {
            "type": "track", "name": "Closer", "artists": [{"name": "The Chainsmokers"}, {"name": "Halsey"}],
            "album": {"name": "Closer", "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300}]}}},
        "episode": {"is_playing": False, "device": {}, "item": {
            "type": "episode", "name": "Ep 1", "images": [], "show": {"name": "A Show", "publisher": "Someone",
                                                                     "images": [{"url": "s", "width": 300}]}}},
    }
    picked = []

    async def fake_art(http, images):
        picked.append(min(images, key=lambda i: abs((i.get("width") or 300) - 300))["url"])
        return "data:image/jpeg;base64,AA=="

    sp._art_data = fake_art
    for kind, answer in answers.items():
        async def fake_get(http, path, answer=answer):
            return answer
        sp._get = fake_get
        info = asyncio.run(sp.now_playing(None))
        if kind == "song":
            assert info["artist"] == "The Chainsmokers, Halsey" and info["playing"]
            assert info["source"] == "Playing on Spotify · Arav's iPhone"
        else:
            assert info["album"] == "A Show" and info["artist"] == "Someone" and not info["playing"]
            assert info["source"] == "Paused on Spotify"
    assert picked == ["mid", "s"]  # the ~300 px image

    async def nothing(http, path):
        return None  # 204: nothing playing
    sp._get = nothing
    assert asyncio.run(sp.now_playing(None)) is None


def test_spotify_finish_route_replies_once_connected_and_on_a_repeated_paste(tmp_path, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    import server.main as main
    from server.spotify import Spotify

    sp = Spotify(tmp_path, 8080)
    state = parse_qs(urlsplit(sp.login_url("0123456789abcdef0123456789abcdef")).query)["state"][0]

    async def fake_token(http, client_id, form):
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    async def fake_get(http, path):
        return {"display_name": "AravBansal"}

    sp._token_request, sp._get = fake_token, fake_get
    hub = SimpleNamespace(spotify=sp, http=None, browser=SimpleNamespace(saver_on=False), on_saver=lambda on: None)
    app = web.Application()
    app[main.HUB] = hub
    pasted = f"http://127.0.0.1:8080/spotify/callback?code=C&state={state}"

    async def body(request):
        return {"url": pasted}

    monkeypatch.setattr(main, "read_json", body)

    async def call():
        resp = await main.spotify_finish(make_mocked_request("POST", "/api/spotify/finish", app=app))
        return resp.status, json.loads(resp.body)

    status, data = asyncio.run(call())
    assert status == 200 and data["connected"] and data["user"] == "AravBansal"
    status, data = asyncio.run(call())  # tapping Finish again with the same address
    assert status == 200 and data["connected"]


def test_spotify_rate_limit_skips_one_check_and_the_next_one_goes_ahead(tmp_path):
    import asyncio
    import time

    from server.spotify import POLL_SECONDS, Spotify, SpotifyError

    class Resp:
        def __init__(self, status, body=None):
            self.status, self.body, self.headers = status, body, {"Retry-After": "3600"}

        async def json(self, content_type=None):
            return self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Http:
        def __init__(self, answers):
            self.answers = list(answers)

        def get(self, url, **kw):
            return self.answers.pop(0)

    sp = Spotify(tmp_path, 8080)
    sp.data = {"client_id": "x" * 32, "refresh_token": "RT", "access_token": "AT", "expires_at": time.time() + 600}
    http = Http([Resp(429), Resp(200, {"is_playing": True})])

    async def run():
        with pytest.raises(SpotifyError):
            await sp._get(http, "/me/player")
        return await sp._get(http, "/me/player")  # straight away: Retry-After is not waited out

    assert asyncio.run(run()) == {"is_playing": True}
    assert POLL_SECONDS <= 5


def test_screensaver_heartbeat_keeps_one_spotify_watch_and_revives_a_dead_one():
    import asyncio
    from types import SimpleNamespace

    from server.main import Hub

    async def run():
        started = []

        async def watch():
            started.append(1)
            await asyncio.sleep(3600)

        hub = SimpleNamespace(spotify=SimpleNamespace(connected=True), _spotify_watch=None, watch_spotify=watch)
        on_saver = Hub.on_saver.__get__(hub)
        on_saver(True)
        await asyncio.sleep(0)
        on_saver(True)                      # heartbeat: the running watch is left alone
        await asyncio.sleep(0)
        assert len(started) == 1
        hub._spotify_watch.cancel()         # the watch died somehow
        await asyncio.sleep(0)
        on_saver(True)                      # next heartbeat revives it
        await asyncio.sleep(0)
        assert len(started) == 2
        on_saver(False)
        await asyncio.sleep(0)
        assert hub._spotify_watch is None

    asyncio.run(run())


def test_spotify_watch_survives_errors_from_spotify_and_from_the_tv(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import server.main as main

    monkeypatch.setattr(main, "POLL_SECONDS", 0.001)
    answers = [RuntimeError("Spotify hiccup"), {"title": "Closer", "playing": True}, {"title": "Closer", "playing": True}]
    shown = []

    async def now_playing(http):
        a = answers.pop(0) if answers else {"title": "Closer", "playing": True}
        if isinstance(a, Exception):
            raise a
        return a

    tv_failures = [ConnectionResetError("CDP socket blipped")]

    async def music(m):
        if tv_failures:
            raise tv_failures.pop()
        shown.append(m["title"])

    hub = SimpleNamespace(spotify=SimpleNamespace(now_playing=now_playing), http=None, browser=SimpleNamespace(music=music))

    async def run():
        task = asyncio.create_task(main.Hub.watch_spotify(hub))
        for _ in range(200):
            await asyncio.sleep(0.002)
            if shown:
                break
        task.cancel()
        assert not task.done() or task.cancelled()  # still running until cancelled

    asyncio.run(run())
    assert shown and shown[0] == "Closer"


def test_a_new_page_means_no_screensaver_and_gets_its_reporting_binding_back(tmp_path):
    import asyncio

    from server.browser import SIGNAL, Browser
    from server.config import Settings

    b = Browser(Settings(data_dir=tmp_path), SERVICES, lambda: None)
    b._session = "S"
    calls, sent = [], []
    b.on_saver = calls.append

    async def fake_send(method, params=None, **kw):
        sent.append((method, params))
        return {}

    b.send = fake_send

    async def run():
        say = lambda on: b._dispatch("Runtime.bindingCalled", {"name": SIGNAL, "payload": f'{{"saver": {str(on).lower()}}}'}, "S")
        say(True)
        say(True)                                                             # heartbeat
        assert b.saver_on and calls == [True, True]
        b._dispatch("Page.frameNavigated", {"frame": {"id": "ad", "parentId": "main"}}, "S")  # an iframe
        assert b.saver_on and not sent
        b._dispatch("Page.frameNavigated", {"frame": {"id": "main"}}, "S")    # a new page
        await asyncio.gather(*b._tasks)
        assert not b.saver_on and calls[-1] is False
        # Chromium drops the binding with the old document; it must be set up again.
        assert sent == [("Runtime.addBinding", {"name": SIGNAL})]

    asyncio.run(run())


def test_watchdog_sends_a_stuck_page_home_then_replaces_it_and_drops_a_hung_browser(tmp_path, monkeypatch):
    import asyncio

    import server.browser as browser_mod
    from server.browser import WATCH_STRIKES, Browser, CDPError
    from server.config import Settings

    monkeypatch.setattr(browser_mod, "WATCH_EVERY", 0)
    b = Browser(Settings(data_dir=tmp_path), SERVICES, lambda: None)
    b._session, b._target = "S", "T"
    sent, page_ok, browser_ok = [], [False], [True]

    async def fake_send(method, params=None, **kw):
        sent.append(method)
        if method == "Browser.getVersion" and not browser_ok[0]:
            raise asyncio.TimeoutError
        if method == "Runtime.evaluate" and not page_ok[0]:
            raise CDPError("no answer")
        return {}

    b.send = fake_send

    class WS:
        closed = False

        async def close(self):
            self.closed = True

    async def run():
        ws = WS()
        task = asyncio.create_task(b._watchdog(ws))
        while sent.count("Runtime.evaluate") < 2 * WATCH_STRIKES:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.gather(*b._tasks)
        # A minute without an answer: home; another minute: the tab is replaced.
        assert sent.count("Page.navigate") == 1 and sent.count("Target.closeTarget") == 1
        page_ok[0], browser_ok[0] = True, False
        await asyncio.wait_for(task, 1)  # the browser stopped answering: connection dropped
        assert ws.closed

    asyncio.run(run())


def test_nightly_refresh_only_when_idle_on_home_and_once_a_night(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import server.main as main

    restarts = []

    async def restart(reason):
        restarts.append(reason)
        return True

    monkeypatch.setattr(main, "restart_kiosk_browser", restart)
    monkeypatch.setattr(main, "kiosk_browser", lambda: (123, 30 * 3600))
    clock = {"hour": 4}
    monkeypatch.setattr(main.time, "localtime", lambda: SimpleNamespace(tm_hour=clock["hour"], tm_yday=100))
    browser = SimpleNamespace(saver_on=True, state={"view": "launcher"}, resume_saver=False)
    hub = SimpleNamespace(browser=browser, cast=SimpleNamespace(active=False), mirror=SimpleNamespace(viewers=set()),
                          _refreshed_day=-1)
    refresh = main.Hub.nightly_refresh.__get__(hub)

    async def run():
        browser.state["view"] = "service"     # someone's watching Netflix: leave it
        await refresh()
        clock["hour"] = 14                    # afternoon: leave it
        browser.state["view"] = "launcher"
        await refresh()
        assert not restarts
        clock["hour"] = 4
        await refresh()                       # 4 am, idle on the home screen: refresh
        await refresh()                       # but only once that night
        assert len(restarts) == 1 and browser.resume_saver

    asyncio.run(run())
