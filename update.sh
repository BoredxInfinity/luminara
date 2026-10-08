#!/usr/bin/env bash
# Pull the latest code from GitHub and apply it.
#   ./update.sh           # by hand on the Pi, as your normal user
#   ./update.sh --force   # also restart the controller and the TV if nothing new was pulled
# The TV's "Install update" button runs this as root through tvbox-update.service (--service).
#
# What's been *applied* is recorded in .git/tvbox-applied, separately from what's been
# pulled, so an update interrupted by a power cut is finished next time.
#
# Everything is inside functions so bash parses the whole file before running it:
# `git pull` may replace this very file, and the freshly pulled copy then does the apply.
set -euo pipefail

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

APPLIED_FILE=.git/tvbox-applied
OWNER=""

as_owner() {  # git and pip must run as the clone's owner so file ownership stays right
  if [[ $EUID -eq 0 ]]; then runuser -u "$OWNER" -- "$@"; else "$@"; fi
}
as_root() {
  if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

pull() {  # $1 = 1 to force a restart even without new commits
  if [[ ! -f /etc/systemd/system/tvbox-server.service ]]; then
    echo "Not installed yet. Run: sudo ./install.sh" >&2
    exit 1
  fi
  if ! as_owner git diff --quiet || ! as_owner git diff --cached --quiet; then
    echo "This clone has local edits (make changes on your laptop and push instead):" >&2
    as_owner git status --short >&2
    echo "To throw them away: git checkout -- . && ./update.sh" >&2
    exit 1
  fi
  say "Pulling"
  as_owner git pull --ff-only --quiet
  exec ./update.sh --apply "$1"  # run the version we just pulled
}

apply() {  # $1 = 1 to force a restart
  local head applied changed_files
  head="$(as_owner git rev-parse HEAD)"
  applied="$(cat "$APPLIED_FILE" 2>/dev/null || true)"

  if [[ -z "$applied" ]] || ! as_owner git cat-file -e "$applied^{commit}" 2>/dev/null; then
    say "No record of what's installed; applying everything"
    run_installer
  fi
  if [[ "$applied" == "$head" && "$1" -eq 0 ]]; then
    echo "Already up to date."
    exit 0
  fi

  changed_files="$(as_owner git diff --name-only "$applied" "$head")"
  as_owner git --no-pager log --oneline "$applied..$head" || true
  if [[ "$1" -eq 1 ]]; then
    changed_files+=$'\nrequirements.txt\nweb/'  # reinstall packages, restart both services
  fi
  changed() { grep -qE "$1" <<<"$changed_files"; }

  # System-level files: re-run the installer, which applies everything and restarts.
  if changed '^(install\.sh|deploy/.*\.service|deploy/pam-)'; then
    say "System setup changed; re-running install.sh"
    run_installer
  fi

  if changed '^requirements\.txt$'; then
    say "Updating Python packages"
    as_owner .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
  fi

  if changed '^deploy/wireplumber-'; then
    say "Reloading audio rules"
    if [[ $EUID -eq 0 ]]; then systemctl --user --machine="$OWNER@" restart wireplumber || true
    else systemctl --user restart wireplumber || true; fi
  fi

  say "Restarting the controller"
  as_root systemctl restart tvbox-server

  # The TV page only picks up new launcher files, services or the overlay script on reload.
  if changed '^(web/|services\.json$|deploy/kiosk(-session)?\.sh$|deploy/cursors/|deploy/chromium-policy\.json$)'; then
    say "Restarting the TV display"
    as_root systemctl restart tvbox-kiosk
  fi

  as_owner sh -c "printf '%s\n' '$head' > $APPLIED_FILE"
  say "Updated to $(as_owner git log -1 --format='%h %s')"
}

run_installer() {  # never returns: install.sh applies everything and records the version
  if [[ $EUID -eq 0 ]]; then exec ./install.sh; else exec sudo ./install.sh; fi
}

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")"
  OWNER="$(stat -c %U . 2>/dev/null || stat -f %Su .)"
  case "${1:-}" in
    --apply) apply "${2:-0}" ;;
    --service)
      [[ $EUID -eq 0 ]] || { echo "--service is for tvbox-update.service (root)" >&2; exit 2; }
      pull 0 ;;
    --force) pull 1 ;;
    "") pull 0 ;;
    *) sed -n '2,5p' "$0"; exit 2 ;;
  esac
}

main "$@"
