#!/usr/bin/env bash
# package-offline.sh - Build a fully OFFLINE-installable tarball of BluForge.
#
# Run this on a machine WITH internet (e.g. this LANforge unit). It bundles the
# toolkit PLUS the required OS packages (.rpm) so the target machine needs no
# network at all. Then install.sh auto-detects the bundled rpms/ and installs
# from them.
#
# Target assumption: same OS family/version as this build host (Fedora 39 here).
# Base dependencies (glibc, systemd, dbus, python3, util-linux/rfkill, iproute)
# are already present on any standard image, so only these 4 add-on packages
# are bundled.
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
OUT="${1:-/tmp/bluforge-offline-$(date +%Y%m%d).tar.gz}"

PKGS=(iw iperf3)
# BlueZ is deliberately NOT bundled or installed here: it must be installed by the end-user (see docs/BLUEZ_INSTALL.md).
FILES=(install.sh package.sh package-offline.sh btlab.py btlab_server.py btcapture.py btle_pcap.py btclassic_pcap.py btinsight.py gui.html
       bt-adv.sh bt-interfere.sh bt-sniff.sh bt-coex.sh README.md)

command -v dnf >/dev/null 2>&1 || { echo "[x] needs dnf to fetch rpms" >&2; exit 1; }
dnf download --help >/dev/null 2>&1 || { echo "[x] 'dnf download' missing: dnf install -y dnf-plugins-core" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bluforge/rpms" "$TMP/bluforge/firmware"

echo "[*] downloading OS packages (x86_64/noarch) ..."
dnf download --resolve --arch=x86_64,noarch --destdir "$TMP/bluforge/rpms" "${PKGS[@]}" >/tmp/bluforge-dl.log 2>&1 \
  || { echo "[x] rpm download failed - see /tmp/bluforge-dl.log" >&2; exit 1; }
echo "[+] bundled rpms: $(ls "$TMP/bluforge/rpms"/*.rpm | wc -l)  ($(du -sh "$TMP/bluforge/rpms" | cut -f1))"

echo "[*] bundling BE200 Bluetooth firmware ..."
# dereference the symlinks so we ship real content under the name the kernel requests
FW_OK=0
for fw in ibt-0291-0291.sfi.xz ibt-0291-0291.ddc.xz ibt-0291-0291.sfi ibt-0291-0291.ddc; do
  if [ -e "/lib/firmware/intel/$fw" ]; then
    cp -L "/lib/firmware/intel/$fw" "$TMP/bluforge/firmware/$fw" 2>/dev/null && FW_OK=1
  fi
done
if [ "$FW_OK" = 1 ]; then
  echo "[+] bundled firmware: $(ls "$TMP/bluforge/firmware" | tr '\n' ' ') ($(du -sh "$TMP/bluforge/firmware" | cut -f1))"
else
  echo "[!] no ibt-0291-0291 firmware found on this build host - skipping (target must already have it)"
fi

for f in "${FILES[@]}"; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$TMP/bluforge/"; done

# generate a plain-language install note with the ACTUAL package filename baked in
PKGNAME="$(basename "$OUT")"
cat > "$TMP/bluforge/README-INSTALL.txt" <<EOF
BluForge - Offline Install  (Fedora 39, x86_64)
================================================

Installs BluForge (BLE interference / advertisements / sniffer + Web GUI) on a
ready-to-run Fedora 39 machine. No internet needed - everything is bundled.

INSTALL - run these TWO commands on the target machine:

  tar xzf $PKGNAME -C /tmp
  sudo /tmp/bluforge/install.sh

When it finishes it prints:   Open: http://<machine-ip>:8090
The Web GUI auto-starts on every boot. That's all.

------------------------------------------------------------------
WHAT GETS INSTALLED
  - OS packages (offline, from bundled rpms/): iw, iperf3   (BlueZ is NOT included: the end-user installs it)
  - BE200 Bluetooth firmware (only if missing): /lib/firmware/intel/ibt-0291-0291.*
  - Toolkit:  /opt/ble-lab/
  - Service:  ble-lab-gui  (systemd, auto-start on boot, port 8090)

AFTER INSTALL
  Open the GUI:       http://<machine-ip>:8090
  Find the IP:        hostname -I
  Service status:     systemctl status ble-lab-gui
  Live logs:          journalctl -u ble-lab-gui -f
  Controller check:   hciconfig -a

REQUIREMENTS
  - Fedora 39, x86_64
  - Intel BE200 present (appears as hci0)
  - If hci0 does not appear right after install, reboot once.

TROUBLESHOOTING
  - GUI not reachable:  systemctl status ble-lab-gui ; journalctl -u ble-lab-gui -n 50
  - No hci0:            hciconfig -a ; dmesg | grep -i bluetooth ; then reboot
------------------------------------------------------------------
EOF
echo "[+] wrote README-INSTALL.txt"

tar -czf "$OUT" -C "$TMP" bluforge
echo "[+] offline package written: $OUT  ($(du -sh "$OUT" | cut -f1))"
echo "[+] install on an AIR-GAPPED machine (no internet needed):"
echo "      tar xzf $(basename "$OUT") -C /tmp && sudo /tmp/bluforge/install.sh"
