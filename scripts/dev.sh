#!/usr/bin/env bash
# Run the whole TV box on this laptop while you edit:
#   ./scripts/dev.sh
# - a separate Chrome window plays the TV (its own profile in .dev/, not your normal one)
# - the server restarts by itself when you save a file in server/ or services.json
# - web/ changes need only a page refresh
# Open the remote on your phone at the URL printed below. Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PORT="${TVBOX_PORT:-8080}"
mkdir -p .dev

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv"
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements-dev.txt

CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium" \
         "$(command -v google-chrome || true)" "$(command -v chromium || true)"; do
  if [[ -n "$c" && -x "$c" ]]; then CHROME="$c"; break; fi
done
[[ -n "$CHROME" ]] || { echo "Install Google Chrome to play the TV." >&2; exit 1; }

CHROME_PID=""
cleanup() { [[ -n "$CHROME_PID" ]] && kill "$CHROME_PID" 2>/dev/null || true; }
trap cleanup EXIT

if curl -fsS -o /dev/null http://127.0.0.1:9222/json/version 2>/dev/null; then
  echo "A browser is already listening on 9222; using it as the TV."
else
  "$CHROME" --remote-debugging-port=9222 --user-data-dir="$PWD/.dev/chrome-profile" \
    --no-first-run --no-default-browser-check --hide-crash-restore-bubble --hide-scrollbars \
    --disable-features=BackForwardCache,SpareRendererForSitePerProcess \
    --window-size=1280,760 "http://127.0.0.1:$PORT/tv" >.dev/chrome.log 2>&1 &
  CHROME_PID=$!
fi

export TVBOX_DATA_DIR="$PWD/.dev/data" TVBOX_PORT="$PORT"
.venv/bin/watchfiles --sigint-timeout 3 ".venv/bin/python -m server.main" server services.json
