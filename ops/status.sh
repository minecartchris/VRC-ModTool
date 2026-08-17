#!/usr/bin/env bash
# How the container and the mod tool are doing, in one screen.
#
#   ./status.sh          once
#   ./status.sh -w       every 5 seconds until Ctrl-C
#   ./status.sh -t       the full dashboard, with start/stop/restart
#   ./status.sh --json   the same numbers, for a script to read
#
# Run it inside the container, or from the Proxmox host:
#
#   pct exec 101 -- /opt/modsuite/app/ops/status.sh
#
# Everything it reads is read-only, including the database.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${MODSUITE_PYTHON:-/opt/modsuite/venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "  no python found - set MODSUITE_PYTHON"; exit 1; }

WATCH=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    -w|--watch) WATCH=1 ;;
    -t|--tui) exec "$PYTHON" "$HERE/tui.py" ;;
    *) ARGS+=("$arg") ;;
  esac
done

bold=$'\033[1m'; dim=$'\033[2m'; off=$'\033[0m'
[ -t 1 ] || { bold=""; dim=""; off=""; }

show() {
  # The heading is shell's job; the numbers come from collect.py so that this
  # and the web panel can never drift apart on what they mean.
  printf '%s\n' "${bold}  Mod Suite - $(date '+%Y-%m-%d %H:%M:%S')${off}"
  printf '%s\n' "${dim}  ---------------------------------------------${off}"
  "$PYTHON" "$HERE/collect.py" ${ARGS[@]+"${ARGS[@]}"}
  echo
}

if [ "$WATCH" = "1" ]; then
  # No `watch` in this container, and clearing by hand keeps the colours.
  while true; do
    printf '\033[H\033[2J'
    show
    printf '%s\n' "${dim}  refreshing every 5s - Ctrl-C to stop${off}"
    sleep 5
  done
else
  show
fi
