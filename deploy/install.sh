#!/usr/bin/env bash
# install.sh - installs the Ericsson BLE demo (BE200 interference + ADV lab)
# on a Fedora/RHEL-family unit with Intel BE200 Bluetooth controllers.
#
# UNLIKE a typical lab-tool installer, this one needs NO package installs at
# all: BlueZ userland (bluetoothctl/btmgmt/btmon) ships with the OS, and
# every line of this project's own code is Python 3 standard library plus
# ctypes -- no pip, no dnf, no apt, confirmed by this project's own recon
# (see docs/04_env_and_deps.md in the source tree this was packaged from).
# This script therefore only copies files, writes one systemd unit, and
# starts it.
#
# Usage (from the extracted offline package directory):
#   sudo ./install.sh                          # installs to /opt/ericsson-bluforge, port 5480
#   sudo INSTALL_DIR=/srv/ble GUI_PORT=5490 ./install.sh
#
# Safe to re-run any time - copies over the existing install and restarts
# the service.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/ericsson-bluforge}"
GUI_PORT="${GUI_PORT:-5480}"
# listen address: 0.0.0.0 = every interface, reachable at the unit's IP (default). 127.0.0.1 = this machine only.
# There is no login: anyone who can reach the port can use the page (docs/ACCESS_CONTROL.md).
# On a LANforge cluster the manager's page reaches the BluForge of every resource through this port, so leave it at 0.0.0.0.
GUI_HOST="${GUI_HOST:-0.0.0.0}"
UNIT_NAME="ericsson-bluforge"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

c_grn="\033[32m"; c_yel="\033[33m"; c_red="\033[31m"; c_cyn="\033[36m"; c_rst="\033[0m"
log()  { echo -e "${c_cyn}[*]${c_rst} $*"; }
ok()   { echo -e "${c_grn}[+]${c_rst} $*"; }
warn() { echo -e "${c_yel}[!]${c_rst} $*" >&2; }
die()  { echo -e "${c_red}[x]${c_rst} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"
[ -d "$HERE/src" ] || die "expected $HERE/src -- run this from the extracted package directory"

log "Target install dir : $INSTALL_DIR"
log "GUI port            : $GUI_PORT"
log "GUI listens on      : $GUI_HOST"

# ---- 0. sanity checks (warn, don't block -- the unit may be prepared before
#         the BE200 hardware/firmware is attached) ------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found -- this project needs it (stdlib only, no pip packages)"
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "python3 version     : $PYVER"
if ! python3 -c "import ctypes, socket; assert hasattr(socket,'AF_BLUETOOTH')" 2>/dev/null; then
  warn "this Python's socket module has no AF_BLUETOOTH -- Bluetooth support may be missing from this build"
fi
if command -v bluetoothctl >/dev/null 2>&1; then
  ok "bluetoothctl found ($(bluetoothctl --version 2>/dev/null || echo present))"
else
  warn "BlueZ is not installed. This is expected on a new appliance: BlueZ is installed by the end-user."
  warn "  The BluForge page offers an 'Install BlueZ' button, or run:  sudo bluforge-bluez install"
fi
N_HCI=$(ls -d /sys/class/bluetooth/hci* 2>/dev/null | wc -l)
log "Bluetooth controllers visible right now: $N_HCI (hciN under /sys/class/bluetooth)"
[ "$N_HCI" -gt 0 ] || warn "no hciN devices found yet -- BE200 firmware/driver may not be loaded; the GUI will still start, radios just won't appear"

# ---- 1. copy the toolkit ---------------------------------------------------
log "Copying toolkit to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
rsync -a --delete "$HERE/src/" "$INSTALL_DIR/src/" 2>/dev/null || cp -a "$HERE/src/." "$INSTALL_DIR/src/"
if [ -d "$HERE/tests" ]; then
  mkdir -p "$INSTALL_DIR/tests"
  cp -a "$HERE/tests/." "$INSTALL_DIR/tests/" 2>/dev/null || true
fi
mkdir -p "$INSTALL_DIR/docs"
[ -d "$HERE/docs" ] && cp -a "$HERE/docs/." "$INSTALL_DIR/docs/" 2>/dev/null || true
ok "toolkit copied"

# ---- 1b. BlueZ installer/uninstaller helper (BlueZ itself is installed by the end-user) ----------
if [ -f "$HERE/bluforge-bluez.sh" ]; then
  install -m 0755 "$HERE/bluforge-bluez.sh" /usr/local/sbin/bluforge-bluez
  ok "bluforge-bluez installed at /usr/local/sbin/bluforge-bluez"
else
  warn "bluforge-bluez.sh not in the package - the GUI cannot offer the BlueZ installer"
fi

# ---- 2. install the systemd unit ------------------------------------------
log "Writing $UNIT_PATH ..."
sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__GUI_PORT__#$GUI_PORT#g" -e "s#__GUI_HOST__#$GUI_HOST#g" \
    "$HERE/ericsson-bluforge.service" > "$UNIT_PATH"
chmod 644 "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || true
systemctl restart "$UNIT_NAME"
ok "service installed + (re)started"

# ---- 3. verify --------------------------------------------------------------
log "Waiting for the GUI to come up ..."
UP=0
for _ in $(seq 1 15); do
  if python3 - "$GUI_PORT" <<'PYEOF' 2>/dev/null
import sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/api/health" % port, timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
  then UP=1; break; fi
  sleep 1
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="127.0.0.1"
[ "$GUI_HOST" = "127.0.0.1" ] && SHOW_HOST="localhost" || SHOW_HOST="$IP"

if [ "$UP" = 1 ]; then
  ok "GUI is up"
else
  warn "GUI did not respond within 15s -- check: journalctl -u $UNIT_NAME -n 50 --no-pager"
fi

echo
echo "================================================================"
echo " Ericsson BLE demo installed."
echo
echo "   Open:  http://${SHOW_HOST}:${GUI_PORT}/"
echo "          (there is no login: anyone who can reach this port can use the page, so only on a trusted network)"
echo
echo "   LANforge cluster: install BluForge on the manager AND on every resource that has Bluetooth radios. The page on the"
echo "   manager then lists the radios of all of them (named like 1.2.hci0). If your firewall is active, port ${GUI_PORT} must be"
echo "   open from the manager to each resource. See docs/CLUSTER.md."
if [ "$GUI_HOST" = "127.0.0.1" ]; then
  echo "   The page is reachable from THIS machine only. From another machine either tunnel it:"
  echo "       ssh -L ${GUI_PORT}:localhost:${GUI_PORT} user@${IP}     (then open http://localhost:${GUI_PORT})"
  echo "   or reinstall with GUI_HOST=0.0.0.0 (there is no login, so only on a trusted network)."
  echo "   Note: with GUI_HOST=127.0.0.1 a LANforge manager cannot reach this unit's radios."
fi
echo
echo "   systemctl status $UNIT_NAME       # service state"
echo "   systemctl restart $UNIT_NAME      # restart"
echo "   journalctl -u $UNIT_NAME -f       # live logs"
echo "================================================================"
