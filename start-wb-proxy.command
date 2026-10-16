#!/bin/bash
# ===========================================================
#  Double-clickable macOS wrapper for start-wb-proxy.sh
#
#  Finder runs *.command files in Terminal. Right-click > Open
#  the first time if Gatekeeper asks for confirmation.
#
#  If double-clicking does nothing, restore the executable bit:
#      chmod +x start-wb-proxy.command start-wb-proxy.sh
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

if [ ! -f "$HERE/start-wb-proxy.sh" ]; then
  echo "[ERROR] start-wb-proxy.sh not found next to this file."
  echo "        expected: $HERE/start-wb-proxy.sh"
  if [ -t 0 ]; then
    echo
    read -n 1 -s -r -p "Press any key to close..."
  fi
  exit 1
fi

bash "$HERE/start-wb-proxy.sh" "$@"
STATUS=$?

if [ -t 0 ]; then
  echo
  read -n 1 -s -r -p "Press any key to close..."
  echo
fi
exit $STATUS
