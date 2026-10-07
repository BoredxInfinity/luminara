#!/usr/bin/env bash
# Pull the latest code from GitHub and apply it. Run on the Pi as your normal user:
#   ./update.sh           # pull, then restart whatever the changes need
#   ./update.sh --force   # restart the controller and the TV even if nothing new was pulled
# System changes (install.sh, deploy/*.service) are applied by re-running sudo ./install.sh.
#
# Everything is inside functions so bash parses the whole file before running it:
# `git pull` may replace this very file, and the freshly pulled copy then does the apply.
set -euo pipefail

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

pull() {  # $1 = 1 for --force
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

  local old
  old="$(git rev-parse HEAD)"
  say "Pulling"
  git pull --ff-only --quiet
  if [[ "$old" == "$(git rev-parse HEAD)" && "$1" -eq 0 ]]; then
    echo "Already up to date."
    exit 0
  fi
  exec ./update.sh --apply "$old" "$1"  # run the new version of this script
}

apply() {  # $1 = commit we came from, $2 = 1 for --force
  local changed_files
  changed_files="$(git diff --name-only "$1" HEAD)"
  git --no-pager log --oneline "$1..HEAD"
  if [[ "$2" -eq 1 ]]; then
    changed_files+=$'\nrequirements.txt\nweb/'  # reinstall packages, restart both services
  fi
  changed() { grep -qE "$1" <<<"$changed_files"; }

  # System-level files: re-run the installer, which applies everything and restarts.
  if changed '^(install\.sh|deploy/.*\.service|deploy/pam-)'; then
    say "System setup changed; re-running install.sh"
    exec sudo ./install.sh
  fi

  if changed '^requirements\.txt$'; then
    say "Updating Python packages"
    .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
  fi

  if changed '^deploy/wireplumber-'; then
    say "Reloading audio rules"
    systemctl --user restart wireplumber || true
  fi

  say "Restarting the controller"
  sudo systemctl restart tvbox-server

  # The TV page only picks up new launcher files, services or the cursor script on reload.
  if changed '^(web/|services\.json$|deploy/kiosk(-session)?\.sh$|deploy/cursors/|deploy/chromium-policy\.json$)'; then
    say "Restarting the TV display"
    sudo systemctl restart tvbox-kiosk
  fi

  say "Updated to $(git log -1 --format='%h %s')"
}

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")"
  case "${1:-}" in
    --apply) apply "$2" "$3" ;;
    --force) pull 1 ;;
    "") pull 0 ;;
    *) sed -n '2,5p' "$0"; exit 2 ;;
  esac
}

main "$@"
