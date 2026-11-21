# BE200 BLE Lab — Advertisement Generator & 2.4 GHz Interference Rig

Toolkit for the **Axiomtek CT523c** with an **Intel BE200** Wi‑Fi 7 + Bluetooth 5.4
combo module. It does two jobs:

1. **Generate the four legacy BLE advertising PDUs** exactly per the Bluetooth Core
   Spec: `ADV_IND`, `ADV_DIRECT_IND`, `ADV_SCAN_IND`, `ADV_NONCONN_IND`.
2. **Generate real‑time, fully customizable 2.4 GHz interference** to evaluate
   Wi‑Fi performance under BLE/Bluetooth interference.

Hardware detected on this box:

| Role | Device | Interface |
|------|--------|-----------|
| BLE radio (adv + interferer) | Intel BE200 Bluetooth 5.4 | `hci0` (USB 8087:0036) |
| BE200 Wi‑Fi | Intel BE200 (iwlwifi, phy0) | `wlan2` |
| Independent Wi‑Fi victims | 2× MediaTek MT7915E | `wlan0`, `wlan1` |

## Installing on a fresh machine (2 commands)

On the current machine, build the package once:

```bash
/opt/ble-lab/package.sh          # writes /tmp/bluforge-YYYYMMDD.tar.gz
```

Copy it to the new machine and run the installer:

```bash
tar xzf bluforge-*.tar.gz && sudo ./bluforge/install.sh
```

The installer (idempotent — safe to re-run) will:
1. install OS packages (`iw`, `iperf3`, `python3`) via dnf/apt (BlueZ is **not** installed: the end-user installs it)
2. copy the toolkit to `/opt/ble-lab`
3. install + enable the `ble-lab-gui` systemd service (auto-starts on boot)
4. verify the controller and GUI are up, and print the URL

No pip installs, no kernel/firmware changes — everything is stock OS packages plus
Python standard library. Works on Fedora (dnf) and Debian/Ubuntu (apt).

## Web GUI (hosted on this LANforge unit)

A browser GUI runs **on this box** as a systemd service and controls everything
below — advertise, interfere, sniff, and the coexistence test — with a live console
and a live sniffed-device table.

```
open:  http://192.168.205.24:8090     (this LANforge unit, LAN IP)
```

It is a small Python stdlib service (`btlab_server.py`) that serves `gui.html` and
spawns the `btlab.py` subcommands, streaming their output to the browser over SSE.
It listens on port **8090** (moved off 8080 - LANforge's own JSON/REST scripting
API, used by tools like `run_voip_cx.py`, defaults to port 8080 too, so the two
collided when both were needed on the same box; other LANforge services are
unaffected: 80/443 web, 4001‑4004 manager, 8000, 5454).

```bash
systemctl status ble-lab-gui      # service state
systemctl restart ble-lab-gui     # restart it
journalctl -u ble-lab-gui -f      # live logs
```

Because there is a single BT radio, the GUI runs **one BT job at a time** and shows a
Stop button; starting a new job while one runs is refused until you stop it. The GUI
is unauthenticated — fine on a trusted lab LAN; put it behind the LANforge network's
access controls, not on an open network.

## How it works (and why it's spec‑correct)

Everything drives the controller through an **HCI User Channel**
(`HCI_CHANNEL_USER`). That takes *exclusive* raw control of `hci0`, so the
in‑kernel BlueZ state machine steps aside and the controller accepts the raw HCI
commands that select the **exact** advertising PDU type — something the normal
`hcitool`/mgmt path refuses (returns `0x0c Command Disallowed`).

- **Advertising** → `HCI_LE_Set_Advertising_Parameters` with the `Advertising_Type`
  field choosing the PDU (Core Spec Vol 4 Part E §7.8.5). Plus Set Advertising Data,
  Set Scan Response Data, Set Advertise Enable.
- **Interference** → `HCI_LE_Transmitter_Test` (Direct Test Mode, §7.8.28 / v2 §7.8.51).
  The controller emits back‑to‑back test packets on one RF channel until
  `LE_Test_End`. We hop across a channel set to fill a Wi‑Fi channel/band.

The tools **stop `bluetooth.service` while running** and **restore it (controller
UP) on exit**, including on Ctrl‑C.

DTM channel *N* → frequency `2402 + 2·N` MHz (N = 0…39, covering 2402–2480 MHz).

---

## 1. Advertisement generator — `bt-adv.sh`

```
bt-adv.sh <type> [options]
```

| type | PDU | Advertising_Type | Properties |
|------|-----|------------------|-----------|
| `ind` | `ADV_IND` | 0x00 | connectable, scannable, undirected |
| `direct` | `ADV_DIRECT_IND` | 0x01 | connectable, directed, **high** duty cycle |
| `direct-l` | `ADV_DIRECT_IND` | 0x04 | connectable, directed, low duty cycle |
| `scan` | `ADV_SCAN_IND` | 0x02 | scannable, undirected |
| `nonconn` | `ADV_NONCONN_IND` | 0x03 | non‑connectable beacon |

Key options: `-i/--interval <ms>` (also `--interval-min/max`), `-c/--channels 37,38,39`,
`-n/--name <str>`, `-d/--data <hex>` (raw AdvData), `--scan-resp <hex>`,
`-p/--peer <addr>` (required for `direct*`), `--own public|random`,
`--random-addr <addr>`, `-t/--duration <sec>` (0 = until Ctrl‑C).

```bash
bt-adv.sh ind -n "BE200-Test" -i 100                 # ADV_IND
bt-adv.sh scan -n "ScanMe" -c 37,39                  # ADV_SCAN_IND on 2 channels
bt-adv.sh nonconn -d 0201060aFF4c0002150001...  -i 100   # iBeacon-style ADV_NONCONN_IND
bt-adv.sh direct   -p AA:BB:CC:DD:EE:FF              # ADV_DIRECT_IND high-duty
bt-adv.sh direct-l -p AA:BB:CC:DD:EE:FF -i 200 -t 30 # ADV_DIRECT_IND low-duty, 30s
```

Directed PDUs (`direct`, `direct-l`) carry **no** AdvData (only AdvA + TargetA), per spec.
Scan‑response is only sent for scannable PDUs (`ind`, `scan`).

## 2. Interference generator — `bt-interfere.sh`

```
bt-interfere.sh <mode> [options]
```

| mode | what it does |
|------|--------------|
| `auto` | **scan the air, find the busiest 2.4 G Wi‑Fi channel, and hammer it** — re‑scans to follow activity. Best for real‑world "drop it in a room" use. |
| `wifi <1-14>` | saturate the band of a Wi‑Fi 2.4 G channel (auto‑maps to covering BLE channels) |
| `single <0-39>` | continuous transmit on one BLE channel |
| `sweep <a-b>` | hop across a BLE channel range |
| `hop <list>` | hop across an explicit list, e.g. `0,12,24,39` |
| `band` | hop across all 40 channels (full‑band jam) |
| `adv-flood` | alternative: flood `ADV_NONCONN_IND` at min interval (realistic BLE traffic) |

Key options: `-w/--bw 20|40` (wifi mode), `-l/--len 0-255` (packet length = duty cycle),
`-P/--payload prbs9|prbs15|11110000|10101010|00001111|0x55|all1|all0`,
`--phy 1M|2M|s8|s2`, `-D/--dwell <ms>` per channel, `--power max|min|<dBm>`,
`-t/--duration <sec>`, `--map` (print plan only, no TX),
`--scan-iface <if>` / `--retarget <s>` (auto mode).

**TX power** (`--power`): drives the DTM interferer's transmit power via the LE
Transmitter Test **v4** command (confirmed supported on this BE200). Use
`--power max` for the strongest effect, `--power min` for a gentle source, or an
integer dBm (e.g. `--power -20`). The firmware clamps to its regulatory maximum, and
its *reported* power range is a non-standard placeholder — so treat `max`/`min` as
"as hot/soft as the firmware allows" rather than an exact dBm. Legacy advertising
power stays at firmware default (the spec has no legacy-adv power knob).

```bash
bt-interfere.sh auto                      # scan air, hammer the busiest Wi-Fi channel (recommended)
bt-interfere.sh auto --scan-iface wlan0 --retarget 8   # follow activity, re-scan every 8s
bt-interfere.sh wifi 6                     # hammer Wi-Fi ch6 (2437MHz)
bt-interfere.sh wifi 1 -w 40 -t 60        # 40MHz around ch1 for 60s
bt-interfere.sh single 19 -P all1         # constant-ish energy on 2440MHz
bt-interfere.sh sweep 0-39 -D 5           # fast full-band sweep, 5ms dwell
bt-interfere.sh wifi 11 --map             # show plan, transmit nothing
```

### Using it as a real-world Bluetooth traffic generator

You place the CT523c in a room with live Wi-Fi (phones, laptops, APs) and it
radiates 2.4 GHz energy from the BE200 antenna that collides with their traffic.
Because Wi-Fi is "listen-before-talk" (CSMA/CA), nearby devices sense the channel
busy, back off, and retransmit — their throughput and latency degrade.

Two behaviours, both real over-the-air transmission:

- **DTM interferer** (`auto`, `wifi`, `single`, `sweep`, `band`) — strongest channel
  occupancy. Recommended for maximum impact. `auto` is the one-liner: it finds where
  the Wi-Fi actually is and hits it.
- **`adv-flood`** — emits genuine `ADV_NONCONN_IND` Bluetooth advertising packets,
  i.e. exactly what a real BLE beacon/device transmits. Use this when you specifically
  want *authentic Bluetooth device traffic* on the air (lower duty cycle than DTM).

Real Bluetooth devices also **frequency-hop** across the band — so `auto`/`sweep`/`band`
mirror how a real busy Bluetooth link spreads its energy.

**Getting the most effect:**
- Put the box **close to the victims** — BE200 BT TX is ~10 dBm, so effect is room-scale.
- Use `-l 255` (max packet length = highest duty cycle) — this is the default.
- Concentrate on one channel (`auto`/`wifi`) rather than `band` when victims share a channel.
- Make sure the BE200's **Bluetooth antenna is connected** to the module.

## 3. Over-the-air BLE sniffer — `bt-sniff.sh`

Passively (or actively) captures BLE **advertising** packets from every nearby
device, decoding address, RSSI, PDU type, name, flags and manufacturer data.

```
bt-sniff.sh [options]
```

Options: `--active` (send SCAN_REQ to pull names/scan-responses), `--interval/--window <ms>`,
`--no-dedup` (report every packet, not one per device), `--filter <text>` (match address or
name), `-t/--duration <sec>`, `--json` (one JSON object per report — for a GUI feed).

```bash
bt-sniff.sh --active                 # live table of nearby BLE devices
bt-sniff.sh --filter L2601 -t 30     # only devices whose addr/name contains "L2601"
bt-sniff.sh --json | your-gui-backend
```

**What it can and cannot do (be clear about this):**
- ✅ Sees all **advertising** traffic OTA on channels 37/38/39: `ADV_IND`,
  `ADV_DIRECT_IND`, `ADV_SCAN_IND`, `ADV_NONCONN_IND`, `SCAN_RSP` — exactly the PDUs
  the generator emits. Great for beacon/device discovery and payload inspection.
- ❌ Does **not** follow connections, capture data-channel PDUs, or sniff Classic
  BR/EDR. Full raw/connection sniffing needs dedicated hardware (Ubertooth One,
  nRF52 with sniffer firmware, or an Ellisys/Frontline analyzer).
- ⚠️ There is only **one** BT radio (`hci0`), so sniffing and interfering/advertising
  are **mutually exclusive in time**. To sniff *while* interfering, add a second BT
  interface (a cheap USB BT dongle or an nRF sniffer) and point the sniffer at it
  with `--dev <n>`.

For a Wireshark pcap instead of the live table, use the normal stack:
`sudo btmon -w capture.pcap &` then `bluetoothctl scan on`.

## 4. Coexistence throughput test — `bt-coex.sh`

Runs `iperf3` **without** then **with** interference and reports the % throughput
drop. The interferer auto‑targets the Wi‑Fi channel the victim interface is on.

```bash
# On a machine reachable over the Wi-Fi under test:
iperf3 -s

# On the CT523c (victim = a MediaTek card, interferer = BE200 BT):
bt-coex.sh --iface wlan0 --server 192.168.1.10 --time 20
bt-coex.sh --iface wlan0 --server 192.168.1.10 -- -P all1 -D 5   # extra interferer args
bt-coex.sh --iface wlan0 --dry-run                               # just detect channel + plan
```

Best practice: make the **victim a different radio** (MT7915E `wlan0/1`) from the
**interferer** (BE200 BT), so you measure true over‑the‑air interference rather than
in‑module self‑coexistence. Both interfaces must be on a **2.4 GHz** channel.

---

## Machine / GUI-ready interface

Everything is parameterised so a web GUI can drive it later. Two building blocks:

- **`btlab.py caps`** — prints a JSON description of every control and its ranges,
  plus live capabilities probed from the controller (is TX-power settable?) and the
  environment (Wi-Fi interfaces, HCI devices). A GUI reads this once to build its
  forms/dropdowns. `--compact` for single-line.

  ```bash
  btlab.py caps | jq .          # adv types, channels, payloads, phys, power, sniff...
  ```

- **`--json` output** — `sniff --json` emits one JSON object per packet; the runners
  print structured status lines. A GUI backend can stream these.

The intended GUI architecture (build later, when you ask): a thin HTTP service
(Flask/FastAPI) that exposes `caps` as `GET /caps`, and `POST /adv`, `POST /interfere`,
`POST /sniff` endpoints which start/stop the corresponding `btlab.py` subcommand as a
managed child process, streaming its `--json` output to the browser over a WebSocket.
No engine changes needed — the GUI just wraps these commands.

## Verifying the advertisements over the air

There's only one BT radio here, so it can't scan while it advertises. Verify with:
- A phone running **nRF Connect** / **LightBlue** — you'll see the PDU type, interval, AdvData.
- A second BT dongle on another host: `sudo btmon` while `hcitool lescan`, or a
  Nordic/Ellisys sniffer for PDU‑level confirmation.
- Locally, `btmon` shows the HCI commands as they're issued (`bt-adv.sh` status lines
  already confirm each command returns `status 00`).

## Safety / regulatory note

Direct Test Mode emits **real RF energy on purpose**. Deliberate transmission /
jamming in the 2.4 GHz band is regulated in most jurisdictions. Run this only on
**your own hardware in a controlled or shielded lab environment**.

## Files

| file | purpose |
|------|---------|
| `btlab.py` | the engine (HCI user channel; `adv`, `interfere`, `sniff`, `caps` subcommands) |
| `bt-adv.sh` | wrapper → `btlab.py adv` |
| `bt-interfere.sh` | wrapper → `btlab.py interfere` |
| `bt-sniff.sh` | wrapper → `btlab.py sniff` |
| `bt-coex.sh` | Wi‑Fi/BLE coexistence throughput test |
| `btlab.py caps` | JSON capability/parameter description for a GUI |
| `btlab_server.py` | web GUI backend (systemd service `ble-lab-gui`, port 8090) |
| `gui.html` | the single-page web GUI |

Run any tool with `--help` for the full option list. Everything must run as **root**.
