#!/usr/bin/env bash
# package-offline.sh - bundles this project into one tar.gz that a fresh
# unit can install with no internet access: every file is this project's
# own Python stdlib source plus the systemd unit and installer -- there are
# no bundled RPMs/packages because none are needed (BlueZ ships with the OS,
# everything else is Python stdlib + ctypes). See docs/INSTALL_LOG.md for the
# full, dated history of every file this project ever added.
#
# Usage:
#   ./package-offline.sh [output.tar.gz]
#
# Then on the new machine:
#   tar xzf ericsson-bluforge-offline-*.tar.gz
#   cd ericsson-bluforge
#   sudo ./install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_ROOT="$(dirname "$HERE")"   # deploy/ is one level under the project root
OUT="${1:-/tmp/ericsson-bluforge-offline-$(date +%Y%m%d_%H%M%S).tar.gz}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/ericsson-bluforge"
mkdir -p "$PKG"

cp -a "$PROJECT_ROOT/src" "$PKG/src"
# drop __pycache__ and anything that isn't project source: a core dump has
# landed in src/ before (a crashed `btmon` test run during development -
# see docs/INSTALL_LOG.md), so this is a real, not hypothetical, cleanup.
find "$PKG/src" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$PKG/src" -maxdepth 1 -type f \( -name "core.*" -o -name "core" \) -print 2>/dev/null | xargs -r rm -f
find "$PKG/src" -name "*.pyc" -delete 2>/dev/null || true
# stray test-server logs (e.g. src/srv_adv.log from 2026-11-02, which still holds an old access token) are not project source
find "$PKG/src" -maxdepth 1 -type f -name "*.log" -delete 2>/dev/null || true

cp -a "$PROJECT_ROOT/docs" "$PKG/docs" 2>/dev/null || true
# hardware-free regression suite: lets anyone confirm an installed unit still behaves (cd tests && ./run_tests.sh)
if [ -d "$PROJECT_ROOT/tests" ]; then
  cp -a "$PROJECT_ROOT/tests" "$PKG/tests"
  find "$PKG/tests" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
fi
cp "$HERE/install.sh" "$HERE/ericsson-bluforge.service" "$HERE/bluforge-bluez.sh" "$PKG/"
chmod +x "$PKG/install.sh" "$PKG/bluforge-bluez.sh"

cat > "$PKG/README-INSTALL.txt" <<'EOF'
Ericsson BLE Demo - Offline Installation
=========================================
Target: any Fedora/RHEL-family machine with Intel BE200 Bluetooth
controllers. BlueZ is NOT part of this package: the end-user installs it
(GUI button, or `sudo bluforge-bluez install`; offline: `--offline-dir DIR`).
This project itself is Python 3 standard library + ctypes, no pip packages.

Install:
  sudo ./install.sh
  (override target dir/port: sudo INSTALL_DIR=/srv/ble GUI_PORT=5490 ./install.sh)

The installer prints the GUI URL when it finishes, e.g.:
  http://<this-machine-ip>:5480
There is no login: anyone who can reach that port can use the page (docs/ACCESS_CONTROL.md).

LANforge cluster (a manager and its resources): run this same installer on the manager and on every resource that has
Bluetooth radios. The page on the manager then lists, and controls, the radios of all of them (named like 1.2.hci0).
See docs/CLUSTER.md.

Verify:
  systemctl status ericsson-bluforge
  journalctl -u ericsson-bluforge -f

Re-running install.sh is always safe (updates the existing install in place).

What's inside:
  src/                 the engine + GUI (bluforge_cli.py, server.py, hci_raw.py,
                        ble_ad.py, ble_channels.py, cluster.py (manager <-> resources), static/ web GUI)
  docs/INSTALL_LOG.md   dated log of every file/package/service this project
                        ever added on the machine it was built on - read this
                        if anything here needs adapting to a different unit
  install.sh            the installer (this file's sibling)
  ericsson-bluforge.service   the systemd unit template install.sh fills in
EOF

tar czf "$OUT" -C "$STAGE" ericsson-bluforge
SIZE=$(du -h "$OUT" | cut -f1)
echo "[+] wrote $OUT ($SIZE)"
echo "[+] copy it to the new machine and run: tar xzf $(basename "$OUT") && cd ericsson-bluforge && sudo ./install.sh"
