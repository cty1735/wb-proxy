#!/bin/bash
# ===========================================================
#  WorkBuddy proxy - macOS / Linux launcher (single machine)
#
#  Usage:
#     ./start-wb-proxy.sh              # default port 8788
#     ./start-wb-proxy.sh 9000         # custom port
#
#  On macOS you can also double-click start-wb-proxy.command.
#
#  This is the POSIX counterpart of start-wb-proxy.bat. The .bat
#  launchers are left in place for Windows users.
# ===========================================================

set -u

PORT="${1:-8788}"

# --- resolve the directory this script lives in --------------------
# Works when double-clicked, run as ./script, or reached via a symlink.
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
    echo "        usage: ./start-wb-proxy.sh [port]"
    exit 1
    ;;
esac

if [ ! -f "$SCRIPT" ]; then
  echo "[ERROR] wb_proxy.py not found next to this script."
  echo "        expected: $SCRIPT"
  exit 1
fi

# --- find a Python 3.9+ interpreter --------------------------------
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

# 1) runtime bundled next to the script (portable "green" builds)
#    The shipped ZIP only carries a Windows python/ tree, so on macOS and
#    Linux nothing here matches and we fall back to the system Python below.
BUNDLED=""
for cand in "$HERE/python/bin/python3" "$HERE/python/bin/python" \
            "$HERE/python3/bin/python3" "$HERE/python/python.exe"; do
  [ -n "$PYEXE" ] && break
  if try_py "$cand"; then BUNDLED="$cand"; fi
done

# 2) Python on PATH (macOS: /usr/bin/python3 or Homebrew python3)
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
  echo "  2. Or install Python 3.9+ (Homebrew or python.org):"
  echo "       brew install python"
  echo "       https://www.python.org/downloads/macos/"
  echo "  3. Or edit this file and point PYEXE at a full path, e.g."
  echo "       PYEXE=/opt/homebrew/bin/python3"
  echo
  exit 1
fi

PYREAL="$("$PYEXE" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$PYEXE")"
PYVER="$("$PYEXE" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")"

echo "==========================================================="
echo "  WorkBuddy proxy"
echo
echo "  API      : http://127.0.0.1:$PORT/v1"
echo "  Dashboard: http://127.0.0.1:$PORT/"
echo
echo "  Python   : $PYREAL ($PYVER)"
if [ -z "$BUNDLED" ]; then
  echo
  echo "  note: this package ships no Python runtime for macOS / Linux,"
  echo "        so the system Python above is used (3.9+ required)."
fi
echo
echo "  Keep this window open. Closing it stops the server."
echo "  Press Ctrl+C to stop."
echo "==========================================================="
echo

"$PYEXE" "$SCRIPT" --port "$PORT"
STATUS=$?

echo
echo "[server exited]"
exit $STATUS
