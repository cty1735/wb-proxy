#!/bin/bash
# ===========================================================
#  Double-clickable macOS wrapper for restart-wb-proxy.sh
#
#  Stops every wb_proxy.py instance on this machine, then
#  starts the proxy in LAN mode (reachable from phones and
#  other computers on the same network).
#
#  Right-click > Open the first time if Gatekeeper asks.
#  If double-clicking does nothing:
#      chmod +x restart-wb-proxy.command restart-wb-proxy.sh
# ===========================================================

set -u

cd "$(dirname "$0")" || exit 1
exec ./restart-wb-proxy.sh "$@"
