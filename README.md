# Luminara: Pi TV Box

This turns a Raspberry Pi 4 (2 GB) into a streaming box for Netflix, Prime Video, JioHotstar and YouTube. Any phone or laptop on the home Wi-Fi can be the remote.

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

**3. Apply on the Pi:**
```bash
ssh <user>@<hostname>.local 'cd ~/luminara && ./update.sh'
```

`update.sh` pulls and then applies only what changed:

| You changed | What `update.sh` does |
|---|---|
| `server/**` | Restarts the controller (about 1 s) |
| `web/**`, `services.json`, `deploy/kiosk.sh` | Also restarts Chromium so the TV reloads (about 10 s; logins persist) |
| `requirements.txt` | Installs the new Python packages first |
| `install.sh`, `deploy/*.service`, `deploy/pam-*` | Re-runs `sudo ./install.sh`, which applies everything |
| Nothing new | Nothing (use `./update.sh --force` to restart anyway) |

**Don't edit files on the Pi.** `update.sh` refuses to run if the clone has local changes, so the Pi always matches GitHub. To get back to the GitHub version: `git checkout -- . && ./update.sh`.

---

## Using the remote

| Remote | What it does |
|---|---|
| Service tiles | Opens the service |
| Home / Back | Launcher / previous page (on YouTube, Back is the TV app's own back) |
| D-pad, OK, Esc | Arrow keys, Enter, Escape |
| ⏪ ⏯ ⏩ | Seek / play-pause (per-service keys come from `services.json`) |
| Trackpad tab | Drag to move a cursor on the TV, tap to click, two fingers to scroll |
| Keyboard tab | Types into whatever is focused on the TV |
| Laptop keyboard | Arrows, Enter, Esc, Backspace (Back) and Space (play/pause) are forwarded |

## Adding a service

Add an entry to `services.json`, then push and run `./update.sh`:

```json
{
  "id": "zee5",
  "name": "ZEE5",
  "url": "https://www.zee5.com/",
  "icon": "Z",
  "color": "#8230c6",
  "match": ["zee5.com"],
  "keys": {"playpause": "space", "seek_fwd": "right", "seek_back": "left", "back": "escape"},
  "user_agent": "optional UA override"
}
```

- `match` lists the domains used to work out which service is on screen.
- `keys` and `user_agent` are optional.

## Housekeeping (on the Pi)

- **Logs:** `journalctl -u tvbox-server -u tvbox-kiosk -f`
- **Reset the PIN and forget all remotes:** `rm ~/.local/state/tvbox/{pin,tokens.json} && sudo systemctl restart tvbox-server`
- **Turn off the PIN:** add `Environment=TVBOX_PIN=0` to `deploy/tvbox-server.service`, then push and update.
- **Audio goes to the wrong output:** run `wpctl status`, then `wpctl set-default <id of the HDMI sink>`.
- **Sign out of every streaming service:** run `sudo systemctl stop tvbox-kiosk && rm -rf ~/.config/tvbox-chromium && sudo systemctl start tvbox-kiosk`.
- **Uninstall:**
  ```bash
  sudo systemctl disable --now tvbox-kiosk tvbox-server && sudo rm /etc/systemd/system/tvbox-*.service /etc/sudoers.d/tvbox /etc/pam.d/tvbox-kiosk && sudo systemctl enable getty@tty1
  ```

## Security

- Chromium's debugging port (9222) only listens on `127.0.0.1`. Anyone who could reach it would control your logged-in sessions, so never forward it.
- Only port 8080 is reachable on the network, and the API needs a paired remote.
- The API rejects cross-origin requests, so a website open on the TV can't drive the box through localhost.
- The remote uses plain HTTP. That's fine at home. If you'd rather not type passwords over Wi-Fi, plug a USB keyboard into the Pi for the one-time logins.
- Don't port-forward 8080.
- `install.sh` gives your user password-less `sudo` for exactly four commands: reboot, poweroff, and restarting the two tvbox services.

## Reference

Environment variables: `TVBOX_PORT` (8080), `TVBOX_CDP` (`http://127.0.0.1:9222`), `TVBOX_DATA_DIR` (`~/.local/state/tvbox`), `TVBOX_SERVICES`, `TVBOX_PIN` (`0` turns it off).

| Path | Contents |
|---|---|
| `server/main.py` | routes, WebSocket hub |
| `server/browser.py` | CDP client: navigation, keys, pointer, popups |
| `server/system.py` | volume and power |
| `server/auth.py` | PIN pairing |
| `server/config.py` | settings, `services.json` |
| `web/tv/` | launcher shown on the TV |
| `web/remote/` | phone remote |
| `web/inject/cursor.js` | on-screen cursor, injected into every page |
| `deploy/` | systemd units, kiosk script, PAM file |
| `install.sh` | one-time Pi setup |
| `update.sh` | pull and apply on the Pi |
| `scripts/dev.sh` | run everything on the laptop |
| `tests/` | `pytest` |
