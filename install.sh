#!/usr/bin/env bash
# One-time setup of the TV box on Raspberry Pi OS Lite (64-bit), run from the git clone:
#   sudo ./install.sh            # install, or re-apply after system-level changes
#   sudo ./install.sh --1080p    # also cap HDMI output at 1080p (for 4K TVs)
# The app runs straight from this clone, so later updates are just ./update.sh.
# The hostname is left alone.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_1080P=0

for arg in "$@"; do
  case "$arg" in
    --1080p) FORCE_1080P=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo ./install.sh" >&2; exit 1; }
TV_USER="${SUDO_USER:-}"
[[ -n "$TV_USER" && "$TV_USER" != root ]] || { echo "run with sudo from your normal user account" >&2; exit 1; }
TV_UID="$(id -u "$TV_USER")"
TV_HOME="$(getent passwd "$TV_USER" | cut -d: -f6)"
as_user() { sudo -u "$TV_USER" -H "$@"; }
as_user test -r "$APP_DIR/server/main.py" || { echo "$TV_USER can't read $APP_DIR" >&2; exit 1; }

say "Installing packages"
apt-get update
apt-get install -y --no-install-recommends \
  git curl python3-venv \
  cage \
  pipewire pipewire-pulse wireplumber \
  libwidevinecdm0 \
  avahi-daemon \
  fonts-noto-core fonts-noto-color-emoji
# The Chromium package is "chromium" on current releases, "chromium-browser" on older ones.
apt-get install -y chromium || apt-get install -y chromium-browser

say "Python environment ($APP_DIR/.venv)"
[[ -x "$APP_DIR/.venv/bin/python" ]] || as_user python3 -m venv "$APP_DIR/.venv"
as_user "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/install.sh" "$APP_DIR/update.sh" "$APP_DIR/deploy/kiosk.sh"

say "Audio, power and login for $TV_USER"
# PipeWire runs as a user service; lingering starts it at boot without a login.
loginctl enable-linger "$TV_USER"
# Exactly what the remote's power buttons and ./update.sh need, without a password.
SUDOERS=/etc/sudoers.d/tvbox
cat > "$SUDOERS.tmp" <<EOF
$TV_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, \\
  /usr/bin/systemctl restart tvbox-server, /usr/bin/systemctl restart tvbox-kiosk
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null && mv "$SUDOERS.tmp" "$SUDOERS"
install -m 0644 "$APP_DIR/deploy/pam-tvbox-kiosk" /etc/pam.d/tvbox-kiosk

say "systemd services"
for unit in tvbox-server.service tvbox-kiosk.service; do
  sed -e "s|@USER@|$TV_USER|g" -e "s|@UID@|$TV_UID|g" -e "s|@APP_DIR@|$APP_DIR|g" \
    "$APP_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl enable tvbox-server.service tvbox-kiosk.service

say "Swap"
if swapon --show=NAME --noheadings | grep -q zram; then
  echo "zram swap already active"
else
  apt-get install -y systemd-zram-generator
  printf '[zram0]\nzram-size = ram / 2\ncompression-algorithm = zstd\n' > /etc/systemd/zram-generator.conf
  echo "zram swap configured (active after reboot)"
fi

if [[ $FORCE_1080P -eq 1 ]]; then
  say "Capping HDMI output at 1080p"
  CMDLINE=/boot/firmware/cmdline.txt
  [[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
  if grep -q 'video=HDMI-A-1:' "$CMDLINE"; then
    echo "already set in $CMDLINE"
  else
    cp "$CMDLINE" "$CMDLINE.bak"
    sed -i '1 s/$/ video=HDMI-A-1:1920x1080@60/' "$CMDLINE"
    echo "added to $CMDLINE (backup at $CMDLINE.bak; takes effect after reboot)"
  fi
fi

# Re-running on a live box applies the changes now.
for svc in tvbox-server tvbox-kiosk; do
  if systemctl is-active --quiet "$svc"; then systemctl restart "$svc"; fi
done

IP="$(hostname -I | awk '{print $1}')"
say "Done"
cat <<EOF
First install?   sudo reboot          (the TV then boots straight to the launcher)
Remote:          http://${IP:-<pi-ip>}:8080/remote   or   http://$(hostname).local:8080/remote
PIN:             shown on the TV (stored in $TV_HOME/.local/state/tvbox/pin)
Update later:    cd $APP_DIR && ./update.sh
Logs:            journalctl -u tvbox-server -u tvbox-kiosk -f
EOF
