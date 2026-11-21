#!/usr/bin/env bash
# start-and-open.sh - the Desktop icon's launcher action.
#
# Manual start/stop by design (the user asked NOT to auto-start this at
# boot): double-clicking the Desktop icon opens this in its own terminal
# window; closing that window is how you stop the service again. There is
# deliberately no systemd unit wired to start at boot for this project.
#
# What it does:
#   1. If the GUI is already up on this port, just open it and exit (so a
#      second double-click of the icon doesn't start a second server or
#      error out -- it just reopens the window).
#   2. Otherwise, start server.py as root (needs raw HCI -> CAP_NET_ADMIN)
#      in the FOREGROUND of this terminal (so logs are visible and closing
#      the window is the stop action -- server.py catches SIGHUP and stops
#      every running job cleanly before exiting), and open the GUI in its
#      own browser window a couple of seconds later once it is confirmed up.
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_ROOT="$(dirname "$HERE")"
SERVER_PY="$PROJECT_ROOT/src/server.py"
PORT="${BLUFORGE_PORT:-5480}"
URL="http://localhost:${PORT}/"

c_grn="\033[32m"; c_cyn="\033[36m"; c_red="\033[31m"; c_rst="\033[0m"
log()  { echo -e "${c_cyn}[*]${c_rst} $*"; }
ok()   { echo -e "${c_grn}[+]${c_rst} $*"; }
err()  { echo -e "${c_red}[x]${c_rst} $*"; }

is_up() {
  python3 - "$PORT" <<'PYEOF' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

open_browser() {
  # own window, not a new tab in whatever Firefox window is already open (there is no login, so the plain URL is enough).
  setsid firefox --new-window "$URL" >/dev/null 2>&1 &
  disown
}

echo "================================================================"
echo " BluForge - Bluetooth Test Appliance"
echo "================================================================"

if is_up; then
  ok "service already running on port $PORT"
  open_browser
  echo
  log "This window is just this one check and can be closed - it is not"
  log "running the service (the already-running one keeps going)."
  exit 0
fi

log "starting the service (needs root for raw HCI - sudo may ask for your password) ..."
( sleep 2
  for _ in $(seq 1 20); do
    is_up && { open_browser; break; }
    sleep 0.5
  done
) &
WATCHER=$!

echo
log "server log below. CLOSE THIS WINDOW to stop the service and every"
log "running interference/advertise/sniff job."
echo "----------------------------------------------------------------"
sudo /usr/bin/python3 -u "$SERVER_PY" --port "$PORT"
RC=$?

kill "$WATCHER" 2>/dev/null
echo "----------------------------------------------------------------"
if [ "$RC" -eq 0 ]; then
  ok "service stopped cleanly"
else
  err "service exited with code $RC - check the log above"
fi
read -r -p "Press Enter to close this window..." _ignored
