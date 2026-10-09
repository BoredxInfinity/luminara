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
# profile. The kiosk uses its own profile, so point it at the system copy. Once
# Chromium can play DRM it may download a newer Widevine and repoint the hint; keep
# that unless the folder it names has gone.
HINT="$PROFILE/WidevineCdm/latest-component-updated-widevine-cdm"
CDM_PATH="$(sed -n 's/.*"Path" *: *"\([^"]*\)".*/\1/p' "$HINT" 2>/dev/null || true)"
if [ -d /opt/WidevineCdm ] && { [ -z "$CDM_PATH" ] || [ ! -d "$CDM_PATH" ]; }; then
  mkdir -p "$PROFILE/WidevineCdm"
  printf '{"Path":"/opt/WidevineCdm"}' > "$HINT"
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

# Raspberry Pi OS's launcher script (/usr/bin/chromium) adds flags meant for a desktop,
# among them --force-renderer-accessibility, which has every page keep an accessibility
# tree up to date as it changes: real CPU time on Netflix or YouTube, for no screen reader.
# Start the browser itself instead, with the launcher's flags that matter here.
if [ -x /usr/lib/chromium/chromium ]; then
  CHROMIUM=/usr/lib/chromium/chromium
  BASE_FLAGS="--use-angle=gles --enable-gpu-rasterization --no-default-browser-check --disable-pings"
else
  CHROMIUM="$(command -v chromium || command -v chromium-browser)"
  BASE_FLAGS=""
fi

# Flags that follow Settings on the remote (e.g. "Lite browser"), written by the controller.
TVBOX_DISABLE_FEATURES="" TVBOX_EXTRA_FLAGS=""
SETTINGS_ENV="${TVBOX_DATA_DIR:-$HOME/.local/state/tvbox}/chromium.env"
# shellcheck disable=SC1090
[ -f "$SETTINGS_ENV" ] && . "$SETTINGS_ENV"
# BackForwardCache would keep the app you just left frozen in memory in case you press
# Back; SpareRendererForSitePerProcess keeps an idle renderer warm. Neither is worth the
# RAM here: leaving an app should free everything it used. The WebUIOmnibox* ones keep
# the address bar's suggestion popup loaded in its own renderer (~60 MB) for a kiosk
# that has no address bar.
FEATURES="Translate,MediaRouter,OptimizationHints,BackForwardCache,SpareRendererForSitePerProcess"
FEATURES="$FEATURES,WebUIOmniboxPopup,WebUIOmniboxAimPopup,WebUIOmniboxFullPopup"
FEATURES="$FEATURES${TVBOX_DISABLE_FEATURES:+,$TVBOX_DISABLE_FEATURES}"

# --remote-debugging-port binds to 127.0.0.1 only. It needs a non-default
# --user-data-dir on current Chromium, which we want anyway so logins persist.
# shellcheck disable=SC2086
exec "$CHROMIUM" $BASE_FLAGS \
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
  --disable-features="$FEATURES" \
  --disable-component-extensions-with-background-pages \
  --disk-cache-size=104857600 \
  --overscroll-history-navigation=0 \
  --disable-pinch \
  --hide-scrollbars \
  $TVBOX_EXTRA_FLAGS \
  "$URL"
