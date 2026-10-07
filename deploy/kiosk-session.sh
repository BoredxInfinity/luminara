#!/bin/sh
# Run by tvbox-kiosk.service: starts cage (the full-screen Wayland compositor) around kiosk.sh.
# Settings that cage itself reads live here, so they can change with a plain ./update.sh.
set -eu
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

# Cage draws a cursor in the middle of the screen even with no mouse attached. Point it
# (and Chromium) at a transparent cursor theme; the remote's touchpad has its own dot.
export XCURSOR_PATH="$DEPLOY_DIR/cursors"
export XCURSOR_THEME=default
export XCURSOR_SIZE=24

exec /usr/bin/cage -s -- "$DEPLOY_DIR/kiosk.sh"
