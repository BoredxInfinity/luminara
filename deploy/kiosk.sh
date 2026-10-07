#!/bin/sh
# Started by cage (tvbox-kiosk.service). Waits for the controller, then runs
# Chromium full screen on the launcher page.
set -eu

PORT="${TVBOX_PORT:-8080}"
PROFILE="${HOME}/.config/tvbox-chromium"
URL="http://127.0.0.1:${PORT}/tv"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

# Sound to HDMI: link our WirePlumber rule into the user's config (once).
WP_DIR="${HOME}/.config/wireplumber/wireplumber.conf.d"
if [ ! -L "$WP_DIR/51-tvbox-hdmi.conf" ]; then
  mkdir -p "$WP_DIR"
  ln -sf "$DEPLOY_DIR/wireplumber-hdmi.conf" "$WP_DIR/51-tvbox-hdmi.conf"
  systemctl --user restart wireplumber || true
fi

# Chromium finds a separately installed Widevine (DRM) through a hint file in the
# profile. The kiosk uses its own profile, so write the hint ourselves.
if [ -d /opt/WidevineCdm ]; then
  mkdir -p "$PROFILE/WidevineCdm"
  printf '{"Path":"/opt/WidevineCdm"}' > "$PROFILE/WidevineCdm/latest-component-updated-widevine-cdm"
fi

for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/healthz" && break
  sleep 0.5
done

# After a power cut Chromium thinks it crashed and offers to restore tabs; it didn't matter.
PREFS="${PROFILE}/Default/Preferences"
if [ -f "$PREFS" ]; then
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS"
fi

CHROMIUM="$(command -v chromium || command -v chromium-browser)"

# --remote-debugging-port binds to 127.0.0.1 only. It needs a non-default
# --user-data-dir on current Chromium, which we want anyway so logins persist.
exec "$CHROMIUM" \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9222 \
  --password-store=basic \
  --no-first-run \
  --noerrdialogs \
  --disable-infobars \
  --hide-crash-restore-bubble \
  --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required \
  --disable-features=Translate,MediaRouter,OptimizationHints \
  --disk-cache-size=104857600 \
  --overscroll-history-navigation=0 \
  --disable-pinch \
  "$URL"
