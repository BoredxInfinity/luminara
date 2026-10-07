#!/bin/sh
# Started by cage (tvbox-kiosk.service). Waits for the controller, then runs
# Chromium full screen on the launcher page.
set -eu

PORT="${TVBOX_PORT:-8080}"
PROFILE="${HOME}/.config/tvbox-chromium"
URL="http://127.0.0.1:${PORT}/tv"

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
