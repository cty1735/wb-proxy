#!/bin/bash
# ===========================================================
#  macOS counterpart of allow-firewall.bat (LAN mode helper)
#
#  macOS does not use Windows Firewall rules. The built-in
#  application firewall asks for permission the first time a
#  program listens on a network port - choose [Allow] then.
#
#  This script explains the situation, prints the current
#  firewall state and can add the Python interpreter used by
#  the proxy to the allowed list (asks for your password).
#
#  Usage: double-click, or  ./allow-firewall.command [port]
# ===========================================================

set -u

PORT="${1:-8788}"
HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOCKETFILTERFW="/usr/libexec/ApplicationFirewall/socketfilterfw"

echo "==========================================================="
echo "  WorkBuddy proxy - macOS firewall helper (port $PORT)"
echo "==========================================================="
echo
echo "On macOS you normally do NOT need a firewall rule: the"
echo "system asks 'Do you want the application python3 to accept"
echo "incoming network connections?' the first time it listens."
echo "Choose [Allow] and other devices can reach the proxy."
echo

if [ ! -x "$SOCKETFILTERFW" ]; then
  echo "(socketfilterfw not available on this system - nothing to do.)"
  exit 0
fi

STATE="$("$SOCKETFILTERFW" --getglobalstate 2>/dev/null || true)"
echo "Application firewall: ${STATE:-unknown}"
echo

if echo "$STATE" | grep -qi "disabled"; then
  echo "The firewall is disabled, so no rule is needed at all."
  echo "If other devices still cannot connect, check that you are on"
  echo "the same network and that the proxy was started in LAN mode:"
  echo "    ./start-wb-proxy-lan.sh $PORT"
  exit 0
fi

# --- find the Python interpreter the launcher would use -------------
PYEXE=""
py_ok() {
  [ -n "$1" ] || return 1
  if [ -x "$1" ]; then
    :
  elif ! command -v "$1" >/dev/null 2>&1; then
    return 1
  fi
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}
for cand in "$HERE/python/bin/python3" python3 /usr/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 python; do
  if py_ok "$cand"; then PYEXE="$cand"; break; fi
done

if [ -z "$PYEXE" ]; then
  echo "No Python 3.9+ found, so there is nothing to allow yet."
  exit 1
fi
PYREAL="$("$PYEXE" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$PYEXE")"

echo "Python to allow: $PYREAL"
echo
echo "Options:"
echo "  1. Allow only the proxy's interpreter (recommended, one sudo prompt)"
echo "  2. Do nothing and answer [Allow] in the macOS popup instead"
echo
read -r -p "Add that interpreter to the allowed list now? [y/N] " ANSWER
case "$ANSWER" in
  y|Y|yes|YES) ;;
  *) echo "Nothing changed."; exit 0 ;;
esac

echo
echo "Adding $PYREAL to the firewall allow-list (may ask for your password)..."
sudo "$SOCKETFILTERFW" --add "$PYREAL" || exit 1
sudo "$SOCKETFILTERFW" --unblockapp "$PYREAL" || exit 1

echo
echo "Done. Other devices should now reach http://<this-mac-ip>:$PORT/"
echo
echo "To remove it later:"
echo "  sudo $SOCKETFILTERFW --remove \"$PYREAL\""
echo "  sudo $SOCKETFILTERFW --unblockapp \"$PYREAL\""
echo
if [ -t 0 ]; then
  read -n 1 -s -r -p "Press any key to close..."
  echo
fi
