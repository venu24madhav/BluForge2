#!/usr/bin/env bash
# install.sh - One-shot installer for the BE200 BLE Lab (BluForge) toolkit + Web GUI.
#
# Usage (from a freshly extracted ble-lab package directory):
#   sudo ./install.sh
#
# What it does:
#   1. Installs required OS packages (iw, iperf3, python3) via dnf/apt. BlueZ is NOT installed: the end-user
#      installs it (see docs/BLUEZ_INSTALL.md); this script only checks for it and says how.
#   2. Copies the toolkit to /opt/ble-lab
#   3. Installs + enables the ble-lab-gui systemd service (auto-starts on boot)
#   4. Verifies the Bluetooth controller and the GUI are both up
#
# Safe to re-run: it will update an existing install in place.
set -euo pipefail

DEST="/opt/ble-lab"
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SERVICE=/etc/systemd/system/ble-lab-gui.service
GUI_PORT="${GUI_PORT:-8090}"

c_grn=$'\e[32m'; c_cyn=$'\e[36m'; c_yel=$'\e[33m'; c_red=$'\e[31m'; c_rst=$'\e[0m'
log()  { printf '%s[*]%s %s\n' "$c_cyn" "$c_rst" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s[!]%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root:  sudo $0"

REQUIRED_FILES=(btlab.py btlab_server.py btcapture.py btle_pcap.py btclassic_pcap.py btinsight.py gui.html bt-adv.sh bt-interfere.sh bt-sniff.sh bt-coex.sh)
for f in "${REQUIRED_FILES[@]}"; do
  [ -f "$SRC/$f" ] || die "missing $f next to install.sh - run from the extracted package directory"
done

if ls "$SRC"/rpms/*.rpm >/dev/null 2>&1; then
  log "Step 1/5: installing OS packages (OFFLINE - from bundled rpms/, no network)"
  # --disablerepo='*' keeps it fully offline; already-installed deps are fine.
  dnf install -y --disablerepo='*' "$SRC"/rpms/*.rpm >/tmp/ble-lab-install-dnf.log 2>&1 \
    || rpm -Uvh --replacepkgs "$SRC"/rpms/*.rpm >>/tmp/ble-lab-install-dnf.log 2>&1 \
    || warn "some bundled packages were already present (continuing) - see /tmp/ble-lab-install-dnf.log"
else
  log "Step 1/5: installing OS packages (ONLINE via package manager)"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y iw iperf3 python3 >/tmp/ble-lab-install-dnf.log 2>&1 \
      || die "dnf install failed - see /tmp/ble-lab-install-dnf.log"
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y iw iperf3 python3 >/tmp/ble-lab-install-apt.log 2>&1 \
      || die "apt-get install failed - see /tmp/ble-lab-install-apt.log"
  else
    die "no supported package manager (dnf/apt-get) and no bundled rpms/ - install iw, iperf3, python3 manually"
  fi
fi
# verify the binaries our tools actually need are now present (both modes)
MISSING=""
for b in iw iperf3 python3; do
  command -v "$b" >/dev/null 2>&1 || MISSING="$MISSING $b"
done
[ -n "$MISSING" ] && die "required tools still missing:$MISSING (check /tmp/ble-lab-install-dnf.log)"
# BlueZ is the end-user's to install; do not fail, but say so clearly
if ! command -v bluetoothctl >/dev/null 2>&1 || ! command -v hciconfig >/dev/null 2>&1; then
  warn "BlueZ (bluetoothctl/hciconfig) is not installed. The Bluetooth features need it, and it is installed by you:"
  warn "    Fedora/RHEL:   sudo dnf install bluez bluez-deprecated        Debian/Ubuntu:   sudo apt install bluez"
fi
ok "OS packages present"

log "Step 2/5: installing toolkit to $DEST"
mkdir -p "$DEST"
for f in "${REQUIRED_FILES[@]}" README.md; do
  [ -f "$SRC/$f" ] && install -m 0755 "$SRC/$f" "$DEST/$f" 2>/dev/null || true
done
# scripts executable, python/html/md not
chmod +x "$DEST"/*.sh "$DEST"/btlab.py 2>/dev/null || true
chmod 644 "$DEST"/gui.html "$DEST"/btlab_server.py "$DEST"/README.md "$DEST"/btcapture.py "$DEST"/btle_pcap.py "$DEST"/btclassic_pcap.py "$DEST"/btinsight.py 2>/dev/null || true
rm -rf "$DEST/__pycache__"
ok "toolkit installed at $DEST"

log "Step 3/5: Bluetooth firmware + controller"
# install bundled BE200 firmware if present and not already on the system
FW_ADDED=0
if ls "$SRC"/firmware/ibt-0291-0291.* >/dev/null 2>&1; then
  mkdir -p /lib/firmware/intel
  for f in "$SRC"/firmware/ibt-0291-0291.*; do
    b="$(basename "$f")"
    if [ ! -e "/lib/firmware/intel/$b" ]; then
      install -m 0644 "$f" "/lib/firmware/intel/$b" && { ok "installed firmware $b"; FW_ADDED=1; }
    else
      log "  firmware $b already present - keeping existing"
    fi
  done
else
  log "  no bundled firmware (online mode / already provided by linux-firmware)"
fi

systemctl enable --now bluetooth >/dev/null 2>&1 || warn "could not enable bluetooth.service (continuing)"

HCI_DEV="$(ls /sys/class/bluetooth 2>/dev/null | head -1 || true)"
if [ -z "$HCI_DEV" ] && [ "$FW_ADDED" = 1 ]; then
  log "  controller not up yet and firmware was just added - reloading btusb"
  modprobe -r btusb 2>/dev/null || true; sleep 1; modprobe btusb 2>/dev/null || true; sleep 3
  HCI_DEV="$(ls /sys/class/bluetooth 2>/dev/null | head -1 || true)"
fi
if [ -z "$HCI_DEV" ]; then
  warn "no /sys/class/bluetooth/hciN found - controller may still be initializing or needs a reboot."
  warn "the GUI will still install; re-check with 'hciconfig -a' after a reboot."
else
  ok "found controller: $HCI_DEV"
fi

log "Step 4/5: installing systemd service (ble-lab-gui, port $GUI_PORT)"
cat > "$SERVICE" <<EOF
[Unit]
Description=BluForge BLE Lab Web GUI (advertise / interfere / sniff / coex)
Documentation=file:$DEST/README.md
After=network-online.target bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/btlab_server.py --port $GUI_PORT --host 0.0.0.0
Restart=on-failure
RestartSec=2
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ble-lab-gui.service
ok "service installed and started"

log "Step 5/5: verifying"
sleep 2
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
ACTIVE="$(systemctl is-active ble-lab-gui.service || true)"
LISTENING="$(ss -ltn 2>/dev/null | grep -c ":$GUI_PORT" || true)"

echo
if [ "$ACTIVE" = active ] && [ "${LISTENING:-0}" -ge 1 ]; then
  ok "BluForge is installed and running."
  echo
  echo "  ${c_grn}Open:  http://${IP:-<this-host-ip>}:${GUI_PORT}${c_rst}"
  echo
  echo "  systemctl status ble-lab-gui     # check status"
  echo "  journalctl -u ble-lab-gui -f     # live logs"
  echo "  systemctl status bluetooth       # controller status"
else
  warn "service did not come up cleanly - check: journalctl -u ble-lab-gui -n 50"
  exit 1
fi
