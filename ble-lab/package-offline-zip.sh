#!/usr/bin/env bash
# package-offline-zip.sh - Same as package-offline.sh but outputs a .zip
# (handy on Windows/macOS for local storage before moving it to a Linux target).
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
OUT="${1:-/tmp/bluforge-offline-$(date +%Y%m%d).zip}"

PKGS=(iw iperf3)
# BlueZ is deliberately NOT bundled or installed here: it must be installed by the end-user (see docs/BLUEZ_INSTALL.md).
FILES=(install.sh package.sh package-offline.sh package-offline-zip.sh btlab.py btlab_server.py btcapture.py btle_pcap.py btclassic_pcap.py btinsight.py gui.html
       bt-adv.sh bt-interfere.sh bt-sniff.sh bt-coex.sh README.md)

command -v dnf >/dev/null 2>&1 || { echo "[x] needs dnf to fetch rpms" >&2; exit 1; }
command -v zip >/dev/null 2>&1 || { echo "[x] needs 'zip' - install with: dnf install -y zip" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bluforge/rpms" "$TMP/bluforge/firmware"

echo "[*] downloading OS packages (x86_64/noarch) ..."
dnf download --resolve --arch=x86_64,noarch --destdir "$TMP/bluforge/rpms" "${PKGS[@]}" >/tmp/bluforge-dl.log 2>&1 \
  || { echo "[x] rpm download failed - see /tmp/bluforge-dl.log" >&2; exit 1; }
echo "[+] bundled rpms: $(ls "$TMP/bluforge/rpms"/*.rpm | wc -l)  ($(du -sh "$TMP/bluforge/rpms" | cut -f1))"

FW_OK=0
for fw in ibt-0291-0291.sfi.xz ibt-0291-0291.ddc.xz ibt-0291-0291.sfi ibt-0291-0291.ddc; do
  if [ -e "/lib/firmware/intel/$fw" ]; then
    cp -L "/lib/firmware/intel/$fw" "$TMP/bluforge/firmware/$fw" 2>/dev/null && FW_OK=1
  fi
done
[ "$FW_OK" = 1 ] && echo "[+] bundled firmware: $(ls "$TMP/bluforge/firmware" | tr '\n' ' ')" \
                  || echo "[!] no firmware found on this build host - skipping"

for f in "${FILES[@]}"; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$TMP/bluforge/"; done
chmod +x "$TMP"/bluforge/*.sh "$TMP"/bluforge/btlab.py

PKGNAME="$(basename "$OUT")"
cat > "$TMP/bluforge/README-INSTALL.txt" <<EOF
BluForge - Offline Install  (Fedora 39, x86_64)
================================================
No internet needed on the target - everything is bundled.

WINDOWS/macOS: this .zip is for STORAGE on your PC. Copy it to the
target Linux machine (USB drive, scp, etc.) before installing.

INSTALL - on the TARGET Fedora machine, run:

  unzip $PKGNAME -d /tmp
  sudo bash /tmp/bluforge/install.sh

(If 'unzip' is missing on the target: sudo dnf install -y unzip)

When it finishes it prints:   Open: http://<machine-ip>:8090
The Web GUI auto-starts on every boot. That's all.

WHAT GETS INSTALLED
  - OS packages (offline, from bundled rpms/): iw, iperf3   (BlueZ is NOT included: the end-user installs it)
  - BE200 Bluetooth firmware (only if missing)
  - Toolkit:  /opt/ble-lab/
  - Service:  ble-lab-gui  (systemd, auto-start on boot, port 8090)

TROUBLESHOOTING
  - GUI not reachable: sudo journalctl -u ble-lab-gui -n 30 --no-pager
  - Port 8090 busy:     sudo ss -ltnp | grep :8090
                        sudo env GUI_PORT=8091 bash /tmp/bluforge/install.sh
  - No hci0:            hciconfig -a ; dmesg | grep -i bluetooth ; then reboot
EOF

( cd "$TMP" && zip -rq -X "$OUT" bluforge )
echo "[+] offline zip written: $OUT  ($(du -sh "$OUT" | cut -f1))"
