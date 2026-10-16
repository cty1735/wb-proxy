#!/bin/bash
# ===========================================================
#  WorkBuddy proxy - LAN mode - macOS / Linux launcher
#
#  Listens on every network interface so phones, laptops and
#  other computers on the same network can use it.
#
#  An API key is REQUIRED in this mode (one is minted and saved
#  to accounts/settings.json when you do not pass one).
#
#  Usage:
#     ./start-wb-proxy-lan.sh                 # random/persisted key
#     ./start-wb-proxy-lan.sh 8788            # custom port
#     ./start-wb-proxy-lan.sh 8788 MyApiKey   # custom port + key
#
#  On macOS you can also double-click start-wb-proxy-lan.command.
#
#  This is the POSIX counterpart of start-wb-proxy-lan.bat. The
#  .bat launchers are left in place for Windows users.
# ===========================================================

set -u

PORT="${1:-8788}"
KEY="${2:-}"

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
SCRIPT="$HERE/wb_proxy.py"

case "$PORT" in
  ''|*[!0-9]*)
    echo "[ERROR] invalid port: $PORT"
    echo "        usage: ./start-wb-proxy-lan.sh [port] [api-key]"
    exit 1
    ;;
esac

if [ ! -f "$SCRIPT" ]; then
  echo "[ERROR] wb_proxy.py not found next to this script."
  echo "        expected: $SCRIPT"
  exit 1
fi

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

try_py() {
  [ -n "$PYEXE" ] && return 0
  py_ok "$1" || return 1
  PYEXE="$1"
  return 0
}

# The shipped ZIP only carries a Windows python/ tree, so on macOS and
# Linux nothing here matches and we fall back to the system Python below.
BUNDLED=""
for cand in "$HERE/python/bin/python3" "$HERE/python/bin/python" \
            "$HERE/python3/bin/python3" "$HERE/python/python.exe"; do
  [ -n "$PYEXE" ] && break
  if try_py "$cand"; then BUNDLED="$cand"; fi
done

if [ -z "$PYEXE" ]; then
  for cand in python3 /usr/bin/python3 /opt/homebrew/bin/python3 \
              /usr/local/bin/python3 python3.13 python3.12 python3.11 \
              python3.10 python3.9 python; do
    [ -n "$PYEXE" ] && break
    try_py "$cand"
  done
fi

if [ -z "$PYEXE" ]; then
  echo "[ERROR] No usable Python 3.9+ found."
  echo
  echo "Options:"
  echo "  1. Install the Xcode command line tools (gives you /usr/bin/python3):"
  echo "       xcode-select --install"
  echo "  2. Or install Python 3.9+:  brew install python"
  echo "  3. Or edit this file and point PYEXE at a full path."
  echo
  exit 1
fi

PYREAL="$("$PYEXE" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$PYEXE")"
PYVER="$("$PYEXE" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")"

echo "==========================================================="
echo "  WorkBuddy proxy - LAN MODE"
echo
echo "  Port $PORT - your API address, dashboard link and API"
echo "  key are printed below once the server is up."
echo
echo "  Python: $PYREAL ($PYVER)"
if [ -z "$BUNDLED" ]; then
  echo
  echo "  note: this package ships no Python runtime for macOS / Linux,"
  echo "        so the system Python above is used (3.9+ required)."
fi
echo
echo "  If other devices cannot connect:"
echo "    - macOS may ask to allow incoming connections the first"
echo "      time Python listens - choose [Allow]."
echo "    - On macOS 15+ also allow Local Network access for your"
echo "      terminal under System Settings > Privacy & Security."
echo "    - Windows users: run allow-firewall.bat once as administrator."
echo
echo "  Keep this window open. Closing it stops the server."
echo "  Press Ctrl+C to stop."
echo "==========================================================="
echo

# Pass --api-key only when the user supplied one; otherwise the gateway
# mints a random key on first run and prints it below.
if [ -n "$KEY" ]; then
  "$PYEXE" "$SCRIPT" --port "$PORT" --lan --api-key "$KEY"
else
  "$PYEXE" "$SCRIPT" --port "$PORT" --lan
fi
STATUS=$?

echo
echo "[server exited]"
exit $STATUS
