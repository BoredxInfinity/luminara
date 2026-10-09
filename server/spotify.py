"""What's playing on your Spotify account, for the screensaver's turntable.

Spotify has no push or webhook for playback, so the box asks (GET /v1/me/player) every
few seconds, and only while the screensaver is showing; otherwise this is idle.

Login is OAuth with PKCE, so no client secret is stored. Spotify only allows an https
redirect address or a loopback one, so the box registers http://127.0.0.1:<port>/spotify/
callback: after you log in on your phone, Spotify sends the phone to that address, which
doesn't load there; you paste it into Settings and the box finishes the login itself.
Tokens live in spotify.json in the data directory, readable only by the box's user.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

log = logging.getLogger("tvbox.spotify")

AUTHORIZE = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-read-currently-playing"
LOGIN_TTL = 15 * 60  # seconds a login link stays valid


class SpotifyError(Exception):
    pass


class Spotify:
    def __init__(self, data_dir: Path, port: int):
        self._file = data_dir / "spotify.json"
        self.redirect_uri = f"http://127.0.0.1:{port}/spotify/callback"
        self._pending: dict[str, dict] = {}  # login state -> {verifier, client_id, at}
        self._art: dict[str, str] = {}       # album art URL -> data: URL (last few)
        self._retry_after = 0.0
        try:
            self.data = json.loads(self._file.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    # ---- account -------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.data.get("refresh_token") and self.data.get("client_id"))

    def status(self) -> dict:
        return {"connected": self.connected, "user": self.data.get("user", ""),
                "client_id": self.data.get("client_id", ""), "redirect_uri": self.redirect_uri}

    def _save(self) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.chmod(0o600)
        tmp.replace(self._file)

    def disconnect(self) -> None:
        self.data = {"client_id": self.data.get("client_id", "")}  # keep the ID for next time
        self._save()

    def login_url(self, client_id: str) -> str:
        client_id = client_id.strip()
        if not client_id.isalnum() or not 16 <= len(client_id) <= 64:
            raise SpotifyError("That doesn't look like a Spotify Client ID")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v["at"] < LOGIN_TTL}
        state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._pending[state] = {"verifier": verifier, "client_id": client_id, "at": now}
        return f"{AUTHORIZE}?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
        })

    async def finish(self, http: aiohttp.ClientSession, pasted: str) -> str:
        """Complete a login from the callback address (or just its ?code=…&state=… part)."""
        query = parse_qs(urlsplit(pasted.strip()).query or pasted.strip().lstrip("?"))
        if "error" in query:
            raise SpotifyError("Spotify said no: " + query["error"][0].replace("_", " "))
        code, state = query.get("code", [""])[0], query.get("state", [""])[0]
        login = self._pending.pop(state, None)
        if not code or not login or time.time() - login["at"] > LOGIN_TTL:
            raise SpotifyError("That address is from an old or unknown login. Tap Connect again.")
        tokens = await self._token_request(http, login["client_id"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "code_verifier": login["verifier"],
        })
        self.data = {"client_id": login["client_id"]}
        self._store(tokens)
        try:
            me = await self._get(http, "/me")
            self.data["user"] = (me or {}).get("display_name") or (me or {}).get("id", "")
        except SpotifyError:
            pass
        self._save()
        log.info("connected to Spotify as %s", self.data.get("user") or "?")
        return self.data.get("user", "")

    async def _token_request(self, http: aiohttp.ClientSession, client_id: str, form: dict) -> dict:
        async with http.post(TOKEN, data={**form, "client_id": client_id},
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise SpotifyError(body.get("error_description") or body.get("error") or f"HTTP {resp.status}")
            return body

    def _store(self, tokens: dict) -> None:
        self.data["access_token"] = tokens["access_token"]
        self.data["expires_at"] = time.time() + int(tokens.get("expires_in", 3600)) - 60
        if tokens.get("refresh_token"):  # Spotify may hand out a new one each time
            self.data["refresh_token"] = tokens["refresh_token"]

    async def _access_token(self, http: aiohttp.ClientSession) -> str:
        if time.time() < self.data.get("expires_at", 0):
            return self.data["access_token"]
        try:
            tokens = await self._token_request(http, self.data["client_id"], {
                "grant_type": "refresh_token", "refresh_token": self.data["refresh_token"]})
        except SpotifyError as exc:
            if "revoked" in str(exc).lower() or "invalid" in str(exc).lower():
                log.warning("Spotify login no longer valid (%s); disconnecting", exc)
                self.disconnect()
            raise
        self._store(tokens)
        self._save()
        return self.data["access_token"]

    async def _get(self, http: aiohttp.ClientSession, path: str) -> dict | None:
        if time.time() < self._retry_after:
            raise SpotifyError("rate limited")
        token = await self._access_token(http)
        async with http.get(API + path, headers={"Authorization": f"Bearer {token}"},
                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 204:
                return None  # nothing playing
            if resp.status == 429:
                self._retry_after = time.time() + int(resp.headers.get("Retry-After", "30"))
                raise SpotifyError("rate limited")
            if resp.status == 401:
                self.data["expires_at"] = 0  # refresh next time
                raise SpotifyError("token expired")
            if resp.status != 200:
                raise SpotifyError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)

    # ---- now playing -----------------------------------------------------------

    async def now_playing(self, http: aiohttp.ClientSession) -> dict | None:
        """{title, artist, album, art, playing, source} or None when nothing is loaded."""
        player = await self._get(http, "/me/player?additional_types=episode")
        item = (player or {}).get("item")
        if not item:
            return None
        if item.get("type") == "episode":
            show = item.get("show") or {}
            artist, album, images = show.get("publisher", ""), show.get("name", ""), item.get("images") or show.get("images")
        else:
            artist = ", ".join(a["name"] for a in item.get("artists", []))
            album, images = (item.get("album") or {}).get("name", ""), (item.get("album") or {}).get("images")
        device = (player.get("device") or {}).get("name")
        playing = bool(player.get("is_playing"))
        source = f"{'Playing' if playing else 'Paused'} on Spotify"
        return {
            "title": item.get("name", ""), "artist": artist, "album": album,
            "art": await self._art_data(http, images or []),
            "playing": playing, "source": f"{source} · {device}" if device else source,
        }

    async def _art_data(self, http: aiohttp.ClientSession, images: list[dict]) -> str:
        """The album art as a data: URL, so streaming sites' content rules can't block it."""
        if not images:
            return ""
        # ~300 px is plenty for the record label and the blurred backdrop.
        image = min(images, key=lambda i: abs((i.get("width") or 300) - 300))
        url = image.get("url", "")
        if url in self._art:
            return self._art[url]
        try:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200 or not resp.content_type.startswith("image/"):
                    return ""
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            return ""
        if len(self._art) >= 8:
            self._art.pop(next(iter(self._art)))
        self._art[url] = f"data:{resp.content_type};base64,{base64.b64encode(data).decode()}"
        return self._art[url]
