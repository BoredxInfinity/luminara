# Luminara: Pi TV Box

This turns a Raspberry Pi 4 (2 GB) into a streaming box for Netflix, Prime Video, JioHotstar, YouTube and Spotify. Any phone or laptop on the home Wi-Fi can be the remote.

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

Both paths run `update.sh`. Installing from the TV or phone goes through `tvbox-update.service`, which runs as root, so it never asks for a password. `update.sh` pulls and then applies only what changed:

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
  - It's a grid of colour-shifting dots pulsing in slow ripples, with the time.
  - It fades in gently over a few seconds. The first button press only wakes it; nothing else happens.
  - **While music plays** (Spotify, or any site that publishes "now playing" info), it shows a turntable with the album art spinning on the record, plus the song, artist and album.
- **D-pad on Netflix, Prime Video and JioHotstar:** these are mouse websites (only YouTube has a real TV interface in a browser), so the arrows move a white focus ring between titles, buttons and menus, and OK clicks.
  - Right at the end of a row pages the row, like a TV app. Up/down go row by row.
  - In a title's pop-up the ring stays inside it; Back closes it and the ring returns to the title.
  - While a video plays full screen, the arrows go to the player (seek, volume) as before. Touching the touchpad hands control to the cursor.
- **No scrollbars** anywhere on the TV (scrolling still works).
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
| ⏪ ⏯ ⏩ | Seek / play-pause (per-service keys come from `services.json`; Spotify seeks with Shift+arrow) |
| ⚙ | Settings |
| Touchpad tab | Drag to move a cursor on the TV, tap to click, two fingers to scroll |
| Keyboard tab | Types into whatever is focused on the TV |
| Laptop keyboard | Arrows, Enter, Esc, Backspace (Back) and Space (play/pause) are forwarded |

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

- **Logs:** `journalctl -u tvbox-server -u tvbox-kiosk -f`
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

Environment variables: `TVBOX_PORT` (8080), `TVBOX_CDP` (`http://127.0.0.1:9222`), `TVBOX_DATA_DIR` (`~/.local/state/tvbox`), `TVBOX_SERVICES`, `TVBOX_PIN` (`0` turns it off), `TVBOX_BOOT_VOLUME` (the default for the Settings value).

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
| `deploy/` | systemd units (incl. `tvbox-update.service`), cage and kiosk scripts, Chromium policy, cursor theme, audio rule |
| `install.sh` | one-time Pi setup |
| `update.sh` | pull and apply on the Pi |
| `scripts/dev.sh` | run everything on the laptop |
| `tests/` | `pytest` |
