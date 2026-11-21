#!/usr/bin/env bash
# bt-coex.sh - Wi-Fi vs BLE/DTM coexistence throughput test.
#
# Measures Wi-Fi throughput with iperf3 WITHOUT and then WITH BLE interference
# so you can quantify the degradation the BE200's DTM interferer causes.
#
# The interferer auto-targets the Wi-Fi channel the victim interface is on.
#
# Recommended layout for a clean result: use a DIFFERENT radio as the Wi-Fi
# victim than the one generating interference. On this box:
#   victim  = wlan0/wlan1 (MediaTek MT7915E)   interferer = BE200 BT (hci0)
# (Using the BE200's own Wi-Fi, wlan2, measures in-module self-coexistence,
#  which is also interesting but a different test.)
#
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

usage() {
cat <<EOF
bt-coex.sh - Wi-Fi/BLE coexistence throughput test

USAGE: bt-coex.sh --iface <wlanX> --server <iperf3-host> [options] [-- <interfere args>]

REQUIRED:
  --iface  <if>     Wi-Fi victim interface (must be associated to an AP)
  --server <host>   iperf3 server address (run 'iperf3 -s' there)

OPTIONS:
  --time   <sec>    seconds per iperf3 run (default 15)
  --bw     <20|40>  interferer bandwidth around the Wi-Fi channel (default 20)
  --reverse         iperf3 -R (downlink / server->client)
  --udp             use UDP instead of TCP
  --dry-run         detect channel + print the plan, do not transmit
  -- <args>         extra args passed to 'btlab.py interfere' (e.g. -P all1 -l 255 -D 10)

EXAMPLE:
  # On the AP-connected laptop:   iperf3 -s
  bt-coex.sh --iface wlan0 --server 192.168.1.10 --time 20
  bt-coex.sh --iface wlan0 --server 192.168.1.10 -- -P all1 -D 5
EOF
}

IFACE=""; SERVER=""; TIME=15; BW=20; REVERSE=0; UDP=0; DRY=0; EXTRA=()
while [ $# -gt 0 ]; do case "$1" in
  --iface) IFACE="$2"; shift 2;;
  --server) SERVER="$2"; shift 2;;
  --time) TIME="$2"; shift 2;;
  --bw) BW="$2"; shift 2;;
  --reverse) REVERSE=1; shift;;
  --udp) UDP=1; shift;;
  --dry-run) DRY=1; shift;;
  --) shift; EXTRA=("$@"); break;;
  -h|--help) usage; exit 0;;
  *) echo "unknown arg: $1" >&2; usage; exit 1;;
esac; done

[ -n "$IFACE" ] || { echo "need --iface" >&2; exit 1; }
[ "$DRY" = 1 ] || [ -n "$SERVER" ] || { echo "need --server (or --dry-run)" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (interferer needs raw HCI)"; exit 1; }

# ---- detect the Wi-Fi channel the victim is on ----
FREQ="$(iw dev "$IFACE" link 2>/dev/null | awk '/freq:/{print $2}')"
[ -n "$FREQ" ] || { echo "[x] $IFACE not associated (no freq). Connect it to an AP first." >&2; exit 1; }
if [ "$FREQ" -ge 5000 ]; then
  echo "[x] $IFACE is on ${FREQ}MHz (5GHz). BLE/2.4GHz interference will NOT overlap." >&2
  echo "    Move the AP/client to a 2.4GHz channel to run this test." >&2
  exit 1
fi
if [ "$FREQ" = 2484 ]; then WCH=14; else WCH=$(( (FREQ - 2407) / 5 )); fi
echo "[*] Victim  : $IFACE @ ${FREQ}MHz  = Wi-Fi channel $WCH"
echo "[*] Interferer target: Wi-Fi ch $WCH, ${BW}MHz  ${EXTRA[*]:-}"
python3 "$HERE/btlab.py" interfere wifi "$WCH" -w "$BW" --map "${EXTRA[@]}" 2>/dev/null | grep -E "DTM channels" || true

if [ "$DRY" = 1 ]; then echo "[+] dry-run only"; exit 0; fi

IPERF_ARGS=(-c "$SERVER" -t "$TIME" -J)
[ "$REVERSE" = 1 ] && IPERF_ARGS+=(-R)
[ "$UDP" = 1 ] && IPERF_ARGS+=(-u -b 0)

parse_mbps() {  # read iperf3 -J on stdin -> Mbit/s
  python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); bps=d["end"]["sum_received"]["bits_per_second"]
    print(f"{bps/1e6:.2f}")
except Exception: print("ERR")'
}

echo; echo "===== BASELINE (no interference), ${TIME}s ====="
BASE="$(iperf3 "${IPERF_ARGS[@]}" | parse_mbps)"
echo "[+] baseline throughput: ${BASE} Mbit/s"

echo; echo "===== UNDER INTERFERENCE, ${TIME}s ====="
python3 "$HERE/btlab.py" interfere wifi "$WCH" -w "$BW" "${EXTRA[@]}" >/tmp/btlab-interf.log 2>&1 &
IPID=$!
sleep 1
UNDER="$(iperf3 "${IPERF_ARGS[@]}" | parse_mbps)"
kill -INT "$IPID" 2>/dev/null || true; wait "$IPID" 2>/dev/null || true
echo "[+] under-interference throughput: ${UNDER} Mbit/s"

echo; echo "================= RESULT ================="
python3 -c "
b,u=float('$BASE'),float('$UNDER')
drop=(b-u)/b*100 if b>0 else 0
print(f'  baseline          : {b:8.2f} Mbit/s')
print(f'  under interference: {u:8.2f} Mbit/s')
print(f'  degradation       : {drop:8.1f} %')
"