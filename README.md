# Luminara: Pi TV Box

This turns a Raspberry Pi 4 (2 GB) into a streaming box for Netflix, Prime Video, JioHotstar and YouTube. While the screensaver is up, it shows what's playing on your Spotify account. Any phone or laptop on the home Wi-Fi can be the remote.

```
 phone / laptop ──HTTP + WebSocket──▶  controller (Python, :8080)  ──CDP, localhost:9222──▶  Chromium (kiosk, in cage)
                                        ├─ /tv      launcher page  ◀────────────────────────────── shown on the TV
                                        └─ /remote  remote page
```

- **Chromium** is the system build on Raspberry Pi OS, because it's the only one with the Widevine DRM that the streaming sites need. It runs full screen under `cage`, a single-app Wayland compositor with no desktop around it.
- **The controller** (`server/`) is a single aiohttp process using about 30–40 MB of RAM. It drives Chromium over the DevTools Protocol, serves both web pages, and changes volume through PipeWire.
- **Expect 480p–720p.** The Pi only gets Widevine L3, so most services cap quality whatever the software does.

---

## What you need

**Hardware**
- Raspberry Pi 4 Model B, 2 GB
- microSD card, 16 GB or larger (A1/A2 rated)
- Official 5 V 3 A USB-C power supply (weaker supplies cause random freezes)
- Micro-HDMI → HDMI cable, plugged into **HDMI0**, the port next to the USB-C power
- A heatsink or fan case (recommended; video decoding runs the CPU hot)
- A USB keyboard and mouse for the first logins (optional)

**Software on your laptop**
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/), to flash the card
- git, plus a GitHub account
- Python 3.11 or newer, and Google Chrome (only for running the box locally while you edit)

**Software on the Pi**

You only install the OS and git by hand. `install.sh` installs everything else:

| What | Package | Why |
|---|---|---|
| OS | **Raspberry Pi OS Lite (64-bit)**, the current Debian 13 "Trixie" release | No desktop, which saves about 200 MB of RAM |
| Browser | `chromium` | The only browser with Widevine on the Pi |
| DRM | `libwidevinecdm0` | Netflix / Prime / Hotstar playback |
| Display | `cage` | Runs Chromium full screen with no desktop |
| Audio | `pipewire` `pipewire-pulse` `wireplumber` | HDMI sound and the volume buttons (`wpctl`) |
| Python | `python3-venv`, then from PyPI: `aiohttp==3.14.4`, `segno==1.6.6` | The controller, and the QR code |
| Network | `avahi-daemon` | Makes `<hostname>.local` work |
| Fonts | `fonts-noto-core` `fonts-noto-color-emoji` | Hindi and other scripts, and emoji, on streaming pages |
| Tools | `git` `curl` | Updates, and the startup health check |
| Memory | `systemd-zram-generator` (only if zram isn't already on) | Compressed swap; it helps Chromium on 2 GB |

---

## Set up the Pi (once)

**1. Flash the card.** In Raspberry Pi Imager:
- Choose *Raspberry Pi 4* → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**.
- In the customisation screen, set:
  - a hostname (for example `tvbox`)
  - your username and password
  - your Wi-Fi and Wi-Fi country
  - your timezone
  - **Enable SSH**, using your public key
- `install.sh` never changes the hostname. Whatever you choose here is what you'll use for `<hostname>.local`.

**2. Boot it, and SSH in from your laptop:**
```bash
ssh <user>@<hostname>.local
```

**3. Update the OS and install git:**
```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt install -y git
```

**4. Check DRM by hand.** This takes about 5 minutes and confirms the hardest part works before you rely on the code.
- Install the test packages: `sudo apt install -y chromium libwidevinecdm0 cage`
- With a keyboard and mouse plugged into the Pi, run `cage chromium` on the Pi's own console (not over SSH).
- Log in to Netflix and JioHotstar and play something on each. To quit, close Chromium with Ctrl+Shift+W; cage exits with it.
- If a site says the browser isn't supported, add a `user_agent` to that service in `services.json`. A Chrome OS UA usually works:
  ```
  Mozilla/5.0 (X11; CrOS aarch64 15633.69.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
  ```

**5. Clone and install:**
```bash
git clone https://github.com/BoredxInfinity/luminara.git ~/luminara
cd ~/luminara
sudo ./install.sh            # add --1080p if your TV is 4K
sudo reboot
```

If you later make the repo private, give the Pi a read-only deploy key:
1. On the Pi, run `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519`.
2. On your laptop, run:
   ```bash
   scp <user>@<hostname>.local:.ssh/id_ed25519.pub pi.pub && gh repo deploy-key add pi.pub --title tvbox && rm pi.pub
   ```
3. On the Pi, run:
   ```bash
   git -C ~/luminara remote set-url origin git@github.com:BoredxInfinity/luminara.git
   ```

The TV boots straight to the launcher. Scan the QR code with your phone and the remote pairs itself.

---

## Everyday workflow: edit on the laptop, run on the Pi

```
 laptop                          GitHub                    Pi
 edit → ./scripts/dev.sh  ──▶  git push  ──▶  origin  ──▶  ./update.sh   (pulls + applies)
```

**1. Edit and try it on the laptop:**
```bash
./scripts/dev.sh
```
- A separate Chrome window plays the TV, using its own profile in `.dev/`.
- The server restarts automatically whenever you save a file in `server/` or `services.json`.
- Changes in `web/` only need a page refresh.
- The phone remote works too: open the URL printed in the terminal.

**2. Test, commit and push:**
```bash
.venv/bin/pytest
git add -A && git commit -m "Describe the change" && git push
```

**3. Install it on the Pi.** Either:
- **Do nothing, then confirm.** The box checks GitHub shortly after it starts and then every 6 hours. When there's something new, the TV home screen and the phone remote show **Update available** with **Install now** and **Later** buttons. On the TV, press ▲ from the tiles to reach them. **Settings → Updates → Check now** checks straight away.
- **Or run it by hand:**
  ```bash
  ssh <user>@<hostname>.local 'cd ~/luminara && ./update.sh'
  ```

Both paths run `update.sh`. Installing from the TV or phone goes through `tvbox-update.service`, which runs as root, so it never asks for a password. First the TV goes to the home screen, closes everything else and frees the memory, so the update has the Pi to itself. `update.sh` pulls and then applies only what changed:

| You changed | What `update.sh` does |
|---|---|
| `server/**` | Restarts the controller (about 1 s) |
| `web/**`, `services.json`, `deploy/kiosk.sh` | Also restarts Chromium so the TV reloads (about 10 s; logins persist) |
| `requirements.txt` | Installs the new Python packages first |
| `install.sh`, `deploy/*.service`, `deploy/pam-*` | Re-runs `sudo ./install.sh`, which applies everything |
| Nothing new | Nothing (use `./update.sh --force` to restart anyway) |

The installed version is recorded separately from the downloaded one, in `.git/tvbox-applied`. If the power goes off mid-update, the box still offers to finish it next time.

**Don't edit files on the Pi.** `update.sh` refuses to run if the clone has local changes, so the Pi always matches GitHub. To get back to the GitHub version: `git checkout -- . && ./update.sh`.

---

## What's on screen

- **TV launcher:**
  - A greeting and clock.
  - A row of service tiles showing each service's own logo. The background glow follows the focused tile.
  - "Jump back in" focuses the service you opened last.
  - A short opening animation plays when a service starts.
  - A count of connected remotes.
  - An update banner when a new version is on GitHub.
- **Screensaver** (after 5 minutes by default; change it in Settings):
  - It runs on the home screen and on streaming-site menus, but never over a playing video.
  - The time and date are drawn in colour-shifting particles. When the minute changes they glide into the new digits; none appear or vanish.
  - It's drawn by the GPU (WebGL): 60 frames a second while the particles move and 30 while they only shimmer (a fraction of a pixel per frame, so it looks the same), at under half of one CPU core. Muted trailers playing behind it are paused until you're back, and the GPU memory it used is handed back when it closes.
  - The **Screensaver** button on the remote starts it right away (the turntable if music is playing).
  - It fades in gently over a few seconds. The first button press only wakes it; nothing else happens.
  - **While music plays** on your Spotify account, on any device (once connected in Settings → Spotify), or in the page on the TV, it shows a turntable with the album art spinning on the record, plus the song, artist and album, on black, with the record player glowing in the album's average colour. See [Spotify on the screensaver](#spotify-on-the-screensaver).
- **D-pad on Netflix, Prime Video and JioHotstar:** these are mouse websites (only YouTube has a real TV interface in a browser), so the arrows move a white focus ring between titles, buttons and menus, and OK clicks.
  - Right at the end of a row pages the row, like a TV app. Up/down go row by row.
  - In a title's pop-up the ring stays inside it; Back closes it and the ring returns to the title.
  - While a video plays full screen, the arrows go to the player (seek, volume) as before. Touching the touchpad hands control to the cursor.
- **Share a laptop's screen on the TV** (the cast button at the top of the remote, on computers):
  - Pick the whole screen, a window or a tab. The picture goes straight from the laptop's browser to the TV over your Wi-Fi (WebRTC) and fills the TV.
  - Before it starts, the TV goes to the home screen, closes everything else and frees the memory, so the Pi is free for the stream. When sharing ends, the TV always returns to a cleared home screen.
  - Choose the quality (480p, 720p or 1080p) and frame rate (15, 24 or 30 fps), even while sharing. H.264 is preferred, which the Pi decodes cheaply.
  - Share a tab with "Also share tab audio" to hear it on the TV.
  - Stop from the remote, from the browser's own "Stop sharing" bar, or by pressing Home/Back on any remote.
  - Phones and tablets can't share their screen from a web page, so the button only appears on computers.
- **No scrollbars** anywhere on the TV (scrolling still works).
- **Smooth on a Pi:** animations stick to what the GPU does cheaply (moving and fading layers). The home screen's background glow is drawn small and stretched rather than crossfading full-screen layers, a D-pad press does only a few milliseconds of work, and Chromium runs without Raspberry Pi OS's desktop flags (one of them made every page keep an accessibility tree up to date).
- **Runs around the clock:**
  - Every 20 seconds the controller checks that Chromium and the TV page answer. A page frozen for a minute is restarted on the home screen; if Chromium itself stops answering for two minutes it's restarted.
  - Once a night (3-5 am), if the box is idle on the home screen under the screensaver and Chromium has run for 20+ hours, Chromium is restarted fresh and the screensaver goes straight back up.
  - When memory runs low, Chromium is asked to free what it can. Under-voltage or overheating is logged as it happens, and a line of health figures (memory, swap, load, temperature) is logged every half hour.
- **Leaving an app frees its memory:** Chromium's back-forward cache and spare renderer are off, so an app's page and process go away when you leave it, and going Home also tells Chromium to drop leftover caches. If Chromium ever discards the TV tab, the box reloads the home screen into it.
- **On top of any streaming site:** a volume bar when you change the volume, a toast when a phone connects or an update arrives, and the touchpad cursor.
- **Phone remote:**
  - It shows what's on the TV and takes on that service's colour.
  - Service tiles use the real app icons.
  - The D-pad accepts taps or swipes; hold an arrow, ⏪/⏩ or volume to repeat.
  - There's a volume meter, a touchpad and a keyboard.
  - "Add to Home Screen" gives it an app icon.
- **Mirror mode** (the screen button at the top of the remote): the phone shows the TV picture live, and you use it like a touchscreen.
  - Tap to click, double-tap to double-click, drag to scroll whatever is under your finger, long-press then drag to hold the mouse button (seek bars, sliders).
  - Pinch to zoom the picture on the phone (it doesn't change the TV); two fingers move around while zoomed.
  - The bar has Remote (back to the normal remote), Back, Home, Type (the phone keyboard types straight onto the TV, corrections included) and volume.
  - The Pi streams only while a phone is in mirror mode and its screen is on, at most 10 frames a second and only when the picture changes. While video plays it costs noticeable CPU, so switch back to the remote for long viewing.
  - Copy-protected shows may appear black in the mirror; menus and trailers mirror normally.
- **Quiet browser:** Chromium policies (`deploy/chromium-policy.json`) turn off password saving, notification and location prompts, translate bars, sign-in nags and downloads.
- **No stray cursor:** cage would draw a cursor in the middle of the screen. `deploy/cursors/` is a transparent cursor theme that hides it.
- **Volume starts at 100% on every boot.** Change it in Settings.
- **Less lag on Netflix and Prime:**
  - **Smooth video** (on by default) hides VP9/AV1 support from websites, so they send H.264. The Pi decodes H.264 in hardware but VP9/AV1 slowly in software, which is the main cause of stutter.
  - **Lite browser** (on by default) runs Chromium with fewer processes and no per-site isolation, which saves a lot of memory on 2 GB.
  - Both are switches in Settings.

## Settings (gear icon on the remote)

| Section | What you can change |
|---|---|
| Apps | Show or hide each app on the TV and remote |
| Screensaver | Off / 1–30 minutes, show the time, preview it on the TV |
| Spotify | Connect your account, disconnect |
| Sound | Volume when the box starts (or leave it as it was) |
| Performance | Smooth video (H.264), Lite browser (applies after **Restart TV display**) |
| Updates | Check automatically, Check now, Install |
| Phones | The PIN for pairing another phone; **Forget all phones** (new PIN) |
| About | Name, address, version, memory use, temperature, uptime; **Restart TV display** |

Settings are saved on the box (`~/.local/state/tvbox/settings.json`) and apply to every phone straight away.

Logos and icons aren't stored in this repo. The Pi downloads them from Wikimedia Commons and the App Store, using the URLs in `services.json`, and caches them in `~/.local/state/tvbox/logos/`. Until a logo is available, the tile shows the service's letter instead.

## Using the remote

| Remote | What it does |
|---|---|
| Service icons | Opens the service (with an opening animation on the TV) |
| Home / Back | Launcher / previous page (on YouTube, Back is the TV app's own back) |
| D-pad, OK, Esc | Arrow keys, Enter, Escape. Tap a direction or swipe anywhere on the pad; hold to repeat |
| ⏪ ⏯ ⏩ | Seek / play-pause (per-service keys come from `services.json`, e.g. `"shift+right"`) |
| ⚙ | Settings |
| Touchpad tab | Drag to move a cursor on the TV, tap to click, two fingers to scroll |
| Keyboard tab | Types into whatever is focused on the TV |
| Laptop keyboard | Arrows, Enter, Esc, Backspace (Back) and Space (play/pause) are forwarded |

## Spotify on the screensaver

Spotify isn't an app on the TV. Instead, while the screensaver is up, the box asks Spotify what's playing on your account (on your phone, laptop, a speaker, anywhere) and shows it on the turntable.

- **Only during the screensaver.** The TV page tells the controller when the screensaver comes on and goes away; it checks Spotify only in between. Spotify has no webhook or push for playback, so it asks every 3 seconds. Spotify publishes no number for its rate limit (a rolling 30-second window, lower for apps in development mode); 10 calls per 30 s is the pace common "now playing" apps use. If Spotify does answer "too many requests", that check is skipped and the next one 3 s later goes ahead.
- Music starting shows the turntable within a few seconds. Paused, the record stops and the arm lifts. After a minute with nothing playing, the screensaver goes back to the clock.
- **Clock to record:** the clock's particles stream into where the record sits, the turntable fades in around them, the record drops onto the platter, the song details rise in, and the tonearm swings on. **Record to clock:** the tonearm lifts, the titles and turntable sink away, and the particles burst out of the record into the clock.
- **When the song changes**, the tonearm lifts, the record rises off the platter and flips over to reveal the new album art, drops back into place, and the tonearm swings back onto it.
- **Connecting (once, from Settings → Spotify on your phone):**
  1. At [Spotify for Developers](https://developer.spotify.com/dashboard), create an app (any name, tick "Web API") and add the Redirect URI shown in Settings: `http://127.0.0.1:8080/spotify/callback`. Spotify only allows HTTPS or loopback addresses there, which is why it's 127.0.0.1. Spotify requires the app's owner to have Premium, and allows up to 5 users per app.
  2. Paste the app's Client ID into Settings and tap **Log in with Spotify**.
  3. After you agree, Spotify sends your phone to the 127.0.0.1 address, which doesn't load on a phone. That's expected: copy the page's address, paste it into Settings and tap **Finish**. The box completes the login itself.
- It uses PKCE, so there's no client secret anywhere. Only read access to what's playing is requested. Tokens are stored in `~/.local/state/tvbox/spotify.json` (readable only by the box's user) and refreshed automatically; **Disconnect** forgets them.

## Secure remote (HTTPS) for screen sharing

Browsers only let a page capture the screen over HTTPS, so the box also serves the remote at `https://<hostname>.local:8443/remote`.

- On first start the box uses [mkcert](https://github.com/FiloSottile/mkcert) (installed by `install.sh`) to create its own certificate authority, issues a certificate for its name, IP and localhost, and then **deletes the authority's private key**. Trusting it on a laptop can therefore only ever vouch for this box, never for other websites.
- Each laptop trusts it once. Open the remote's cast panel over plain HTTP and it walks you through it: download the certificate from `http://<box>:8080/ca.crt`, mark it trusted (Keychain Access on a Mac, "Trusted Root Certification Authorities" on Windows; Firefox has its own list), then open the secure remote and pair with the PIN.
- The certificate lasts about two years; the box makes a new one (and you trust it again) only when it's nearly expired or the hostname changes. It's kept if the IP changes, since the `.local` name still matches.
- Files live in `~/.local/state/tvbox/tls/`. `TVBOX_HTTPS_PORT=0` turns HTTPS off.

## Adding a service

Add an entry to `services.json`, then push and run `./update.sh`:

```json
{
  "id": "zee5",
  "name": "ZEE5",
  "tagline": "Indian originals and films",
  "url": "https://www.zee5.com/",
  "match": ["zee5.com"],
  "glyph": "Z",
  "color": "#8230c6",
  "tile": "linear-gradient(135deg, #2a0b4a, #8230c6)",
  "logo": {"url": "https://…/zee5-wordmark.svg", "filter": "white"},
  "icon": {"url": "https://…/zee5-app-icon.png", "bg": "#000"},
  "keys": {"playpause": "space", "seek_fwd": "right", "seek_back": "left", "back": "escape"},
  "user_agent": "optional UA override",
  "dpad": true
}
```

- `match` lists the domains used to work out which service is on screen. `logo` is the wordmark on the TV tile; `icon` is the square app icon on the remote. Options: `filter: "white"` makes the logo white, and `recolor` swaps SVG colours. Square App Store icons come from `https://itunes.apple.com/search?entity=software&term=<name>` (use `artworkUrl512`).
- `keys` and `user_agent` are optional. Keys can include modifiers, for example `"shift+right"`.
- `dpad` turns on arrow-key navigation for a mouse-only site. If its title cards aren't links or buttons, list them: `"dpad": {"cards": ".row div[aria-label]"}` (that's how JioHotstar is set up).

## Housekeeping (on the Pi)

- **Logs:** `journalctl -u tvbox-server -u tvbox-kiosk -f`. They're kept across reboots in a fixed 50 MB (5 MB files; the oldest is dropped as a new one starts). Add `-b -1` for the previous boot, or `journalctl -u tvbox-server | grep health` for the half-hourly health lines. Chromium's own errors are in the `tvbox-kiosk` log.
- **Update logs:** `journalctl -u tvbox-update`
- **Reset the PIN and forget all remotes:** use **Settings → Phones → Forget all phones**.
- **Turn off the PIN:** add `Environment=TVBOX_PIN=0` to `deploy/tvbox-server.service`, then push and update.
- **Audio goes to the wrong output:** HDMI is preferred automatically (`deploy/wireplumber-hdmi.conf`). To override it, run `wpctl status`, then `wpctl set-default <id>`.
- **Sign out of every streaming service:** run `sudo systemctl stop tvbox-kiosk && rm -rf ~/.config/tvbox-chromium && sudo systemctl start tvbox-kiosk`.
- **Uninstall:**
  ```bash
  sudo systemctl disable --now tvbox-kiosk tvbox-server && sudo rm /etc/systemd/system/tvbox-*.service /etc/sudoers.d/tvbox /etc/pam.d/tvbox-kiosk /etc/chromium/policies/managed/tvbox.json && sudo systemctl enable getty@tty1
  ```

## Security

- Chromium's debugging port (9222) only listens on `127.0.0.1`. Anyone who could reach it would control your logged-in sessions, so never forward it.
- Only port 8080 is reachable on the network, and the API needs a paired remote.
- The API rejects cross-origin requests, so a website open on the TV can't drive the box through localhost.
- The remote uses plain HTTP. That's fine at home. If you'd rather not type passwords over Wi-Fi, plug a USB keyboard into the Pi for the one-time logins.
- Don't port-forward 8080.
- `install.sh` gives your user password-less `sudo` for exactly five commands: reboot, poweroff, restarting the two tvbox services, and starting `tvbox-update.service`.
- **Self-update trusts your GitHub repo.** `tvbox-update.service` runs as root and applies whatever is on the `main` branch once you tap Install, so anyone who can push to the repo can run code on the box. Keep 2-factor authentication on the GitHub account.

## Reference

Environment variables: `TVBOX_PORT` (8080), `TVBOX_HTTPS_PORT` (8443), `TVBOX_CDP` (`http://127.0.0.1:9222`), `TVBOX_DATA_DIR` (`~/.local/state/tvbox`), `TVBOX_SERVICES`, `TVBOX_PIN` (`0` turns it off), `TVBOX_BOOT_VOLUME` (the default for the Settings value).

| Path | Contents |
|---|---|
| `server/main.py` | routes, WebSocket hub |
| `server/browser.py` | CDP client: navigation, keys, pointer, popups |
| `server/system.py` | volume and power |
| `server/auth.py` | PIN pairing |
| `server/config.py` | settings, `services.json` |
| `web/tv/` | launcher shown on the TV |
| `web/remote/` | phone remote |
| `server/logos.py` | downloads and caches service logos and icons |
| `server/settings.py` | settings from the remote's panel |
| `server/updater.py` | checks GitHub, starts installs |
| `web/inject/overlay.js` | injected into every page: H.264 steering, screensaver, cursor, volume bar, toasts |
| `web/inject/dpad.js` | injected too: D-pad navigation for sites with `dpad` in `services.json` |
| `web/remote/settings.js` | the Settings panel |
| `web/remote/mirror.js`, `server/mirror.py` | mirror mode: the TV picture on the phone, touches back to the TV |
| `web/remote/cast.js`, `web/cast/`, `server/cast.py` | screen sharing: laptop → TV over WebRTC, and the TV's receiver page |
| `server/tls.py` | the box's HTTPS certificate (mkcert) |
| `server/spotify.py` | Spotify login (PKCE) and "what's playing", for the screensaver |
| `deploy/` | systemd units (incl. `tvbox-update.service`), cage and kiosk scripts, Chromium policy, cursor theme, audio rule |
| `install.sh` | one-time Pi setup |
| `update.sh` | pull and apply on the Pi |
| `scripts/dev.sh` | run everything on the laptop |
| `tests/` | `pytest` |
