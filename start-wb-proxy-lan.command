#!/bin/bash
# ===========================================================
#  Double-clickable macOS wrapper for start-wb-proxy-lan.sh
#
#  Starts the proxy in LAN mode (reachable from phones and
#  other computers on the same network).
#
#  Right-click > Open the first time if Gatekeeper asks.
#  If double-clicking does nothing:
#      chmod +x start-wb-proxy-lan.command start-wb-proxy-lan.sh
# ===========================================================

set -u

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in
    /*) ;;
    *) SOURCE="$DIR/$SOURCE" ;;
  esac
done
HERE="$(cd -P "$(dirname "$SOURCE")" && pwd)"

if [ ! -f "$HERE/start-wb-proxy-lan.sh" ]; then
  echo "[ERROR] start-wb-proxy-lan.sh not found next to this file."
  echo "        expected: $HERE/start-wb-proxy-lan.sh"
  if [ -t 0 ]; then
    echo
    read -n 1 -s -r -p "Press any key to close..."
  fi
  exit 1
fi

bash "$HERE/start-wb-proxy-lan.sh" "$@"
STATUS=$?

if [ -t 0 ]; then
  echo
  read -n 1 -s -r -p "Press any key to close..."
  echo
fi
exit $STATUS
