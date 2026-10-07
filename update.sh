#!/usr/bin/env bash
# Pull the latest code from GitHub and apply it. Run on the Pi as your normal user:
#   ./update.sh           # pull, then restart whatever the changes need
#   ./update.sh --force   # restart everything even if nothing new was pulled
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

if [[ ! -f /etc/systemd/system/tvbox-server.service ]]; then
  echo "Not installed yet. Run: sudo ./install.sh" >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "This clone has local edits (make changes on your laptop and push instead):" >&2
  git status --short >&2
  echo "To throw them away: git checkout -- . && ./update.sh" >&2
  exit 1
fi

OLD="$(git rev-parse HEAD)"
say "Pulling"
git pull --ff-only
NEW="$(git rev-parse HEAD)"

if [[ "$OLD" == "$NEW" && $FORCE -eq 0 ]]; then
  echo "Already up to date."
  exit 0
fi

if [[ $FORCE -eq 1 ]]; then
  CHANGED="$(git ls-files)"  # treat everything as changed
else
  CHANGED="$(git diff --name-only "$OLD" "$NEW")"
  git --no-pager log --oneline "$OLD..$NEW"
fi
changed() { grep -qE "$1" <<<"$CHANGED"; }

# System-level files: re-run the installer, which applies everything and restarts.
if changed '^(install\.sh|deploy/.*\.service|deploy/pam-)'; then
  say "System setup changed; re-running install.sh"
  exec sudo ./install.sh
fi

if changed '^requirements\.txt$'; then
  say "Updating Python packages"
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
fi

say "Restarting the controller"
sudo systemctl restart tvbox-server

# The TV page only picks up new launcher files, services or the cursor script on reload.
if changed '^(web/|services\.json$|deploy/kiosk\.sh$)'; then
  say "Restarting the TV display"
  sudo systemctl restart tvbox-kiosk
fi

say "Updated to $(git log -1 --format='%h %s')"
