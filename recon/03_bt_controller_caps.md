# 03 - Bluetooth controller capability probe (hci0..hci7)

Host `ct523c-ccb8` (10.28.3.73), kernel `6.18.14+`, BlueZ 5.77, Bluetooth core module 2.22, mgmt API 1.23.
Collected 2026-10-05 ~18:15-18:30 IST. **Strictly read-only**: only allowlisted `Read ...` HCI commands were sent, over the shared
raw HCI channel; no reset/set/enable/test/power commands, no user channel, no rfkill/power/interface changes, no API writes.
Machine-readable data: `bt_caps.json`. Raw captures: `raw/`. Probe script: `bt_probe.py` (hard allowlist in `hci_cmd()`).

## 1. Decisive answers

| Question | Answer (all 8 controllers, identical) |
|---|---|
| LE Transmitter Test supported? | **Yes. v1 (0x201E) YES, v2 "Enhanced" (0x2034) YES, v3 (0x2050) NO, v4 (0x207B) YES**, LE Test End (0x201F) YES |
| LE Receiver Test? | v1 (0x201D) YES, v2 (0x2033) YES, v3 (0x204F) NO |
| Extended advertising supported? | **Yes.** LE feature bit "LE Extended Advertising" set; 0x2035 / 0x2036 / 0x2037 / 0x2038 / 0x2039 all YES |
| Max advertising data length / sets | HCI `LE Read Max Adv Data Length` = **200 bytes** (0x00C8); `LE Read Number of Supported Adv Sets` = **12** |
| Periodic advertising | **NO** (feature bit clear; 0x203E-0x2040 all NO) |
| 2M PHY / Coded PHY | both YES |
| Controllers idle? | **All 8 idle** (no connections, scanning, advertising instances, audio) |
| Controllers currently UP? | **All 8 are UP/powered** (managed by bluetoothd) - they would have to be taken DOWN before a user-channel open |

Evidence for the Tx-test claim is the Supported Commands bitmap (below); no test command was executed (transmission is forbidden in this recon).

## 2. Controller table

All eight are the same part: USB `8087:0036` (Intel BE200 Bluetooth), manufacturer 2 (Intel Corp.), HCI 5.4 (0x0d) rev 0x4657 (18007), LMP 5.4 (0x0d) subversion 0x4657.
Firmware for all 8 (from rotated `/var/log/messages-20261004`; current dmesg ring buffer no longer holds the BT boot lines): `intel/ibt-0291-0291.sfi`,
"Firmware Version: 87-38.24", "timestamp 2024.38 buildtype 1 build 67159", SHA1 0x083ad0b2, boot address 0x100800, logged as "Firmware already loaded".
debugfs `hardware_info` = `INTEL platform=55 variant=28`.

| hci | BD_ADDR | USB port (bus) | USB host (PCI) | Inferred sibling Wi-Fi radio | State |
|---|---|---|---|---|---|
| hci0 | 80:84:89:00:90:5F | 1-1 (bus1) | 0000:0d:00.0 | wiphy11 (0000:15:00.0) | UP, idle |
| hci1 | BC:CD:99:F8:7F:04 | 5-1 (bus5) | 0000:10:00.0 | wiphy10 (0000:19:00.0) | UP, idle |
| hci2 | 80:84:89:08:E4:44 | 1-2 (bus1) | 0000:0d:00.0 | wiphy8 (0000:16:00.0) | UP, idle |
| hci3 | 80:84:89:00:81:E6 | 5-2 (bus5) | 0000:10:00.0 | wiphy4 (0000:1a:00.0) | UP, idle |
| hci4 | 80:84:89:00:90:96 | 4-1 (bus4) | 0000:0d:00.1 | wiphy7 (0000:17:00.0) | UP, idle (an LE random address 31:48:c8:c1:cc:e4 is set, left over from earlier LE use) |
| hci5 | 80:84:89:08:E2:78 | 6-1 (bus6) | 0000:10:00.1 | wiphy5 (0000:1b:00.0) | UP, idle |
| hci6 | BC:CD:99:F8:01:AA | 4-2 (bus4) | 0000:0d:00.1 | wiphy9 (0000:18:00.0) | UP, idle |
| hci7 | 80:84:89:00:90:55 | 6-2 (bus6) | 0000:10:00.1 | wiphy6 (0000:1c:00.0) | UP, idle |

Topology (`readlink -f /sys/class/bluetooth/hciN/device`, `lspci -tv`): all BT controllers hang off `0000:00:1c.0 -> 09:00.0 -> 0a:04.0 / 0a:05.0 -> ... -> 0c:03.0 / 0f:03.0`,
i.e. two **Pericom PI7C9X440SL PCIe-to-USB bridges** (each = 2x OHCI + 1x EHCI; usb1/usb4 on 0d:00.x, usb5/usb6 on 10:00.x). Each BT device shows as a **12 Mb/s full-speed** device on an OHCI root hub
(normal for Intel BT; the EHCI buses 2 and 3 are empty). The 8 BE200 Wi-Fi radios (8086:272b, iwlwifi) are separate PCIe endpoints 15:00.0 - 1c:00.0 behind switch port `0a:09.0 -> 13:00.0 -> 14:00.0..14:07.0`.
**The USB/PCI topology by itself does not reveal which BT belongs to which Wi-Fi radio** (different bridge branches).
The sibling column above is an *inference from MAC addresses*, not topology: each BE200 module owns a 4-address Wi-Fi MAC block (e.g. wiphy11 = 80:84:89:00:90:5b..5e) and its BT BD_ADDR is the next address (…:5F = hci0). This matched 8 of 8.
Regularity supports it: chip 0d (usb1+usb4) carries hci0/2/4/6 = Wi-Fi slots 14:00..14:03, chip 10 (usb5+usb6) carries hci1/3/5/7 = slots 14:04..14:07.
(Other Wi-Fi radios: wiphy0/1/2 are ath10k QCA9984 at 22:00.0 / 20:00.0 / 07:00.0; HackRF One is on USB bus 7 port 3, driver `hackrf`.)

## 3. Supported Commands decode (Core Spec Vol 4 Part E 6.27) - identical on all 8

Raw bitmap (hci0..hci7 identical):
`bffffb03ccff0fffbffffc1ff20fe8fe3ff78fff1c00040061f7ffff7f380000fef0ffffffe3800700e81ffcffff03` + zeros
(octets 25..28 = f7 ff ff 7f; 35..39 = ff ff e3 80 07; 44..46 = ff ff 03).

| Command | Opcode | Octet.bit | Supported |
|---|---|---|---|
| LE Set Random Address | 0x2005 | 25.4 | YES |
| LE Set Advertising Parameters (legacy) | 0x2006 | 25.5 | YES |
| LE Set Advertising Data (legacy) | 0x2008 | 25.7 | YES |
| LE Set Scan Response Data (legacy) | 0x2009 | 26.0 | YES |
| LE Set Advertising Enable (legacy) | 0x200A | 26.1 | YES |
| LE Set Scan Parameters | 0x200B | 26.2 | YES |
| LE Set Scan Enable | 0x200C | 26.3 | YES |
| LE Receiver Test [v1] | 0x201D | 28.4 | YES |
| LE Transmitter Test [v1] | 0x201E | 28.5 | YES |
| LE Test End | 0x201F | 28.6 | YES |
| LE Enhanced Receiver Test [v2] | 0x2033 | 35.7 | YES |
| LE Enhanced Transmitter Test [v2] | 0x2034 | 36.0 | YES |
| LE Set Advertising Set Random Address | 0x2035 | 36.1 | YES |
| LE Set Extended Advertising Parameters [v1] | 0x2036 | 36.2 | YES |
| LE Set Extended Advertising Data | 0x2037 | 36.3 | YES |
| LE Set Extended Scan Response Data | 0x2038 | 36.4 | YES |
| LE Set Extended Advertising Enable | 0x2039 | 36.5 | YES |
| LE Read Maximum Advertising Data Length | 0x203A | 36.6 | YES |
| LE Read Number of Supported Advertising Sets | 0x203B | 36.7 | YES |
| LE Remove Advertising Set / Clear Advertising Sets | 0x203C / 0x203D | 37.0 / 37.1 | YES / YES |
| LE Set Periodic Advertising Parameters [v1] / Data / Enable | 0x203E / 0x203F / 0x2040 | 37.2 / 37.3 / 37.4 | **NO / NO / NO** |
| LE Set Extended Scan Parameters | 0x2041 | 37.5 | YES |
| LE Set Extended Scan Enable | 0x2042 | 37.6 | YES |
| LE Extended Create Connection [v1] | 0x2043 | 37.7 | YES |
| LE Read Transmit Power | 0x204B | 38.7 | YES |
| LE Read / Write RF Path Compensation | 0x204C / 0x204D | 39.0 / 39.1 | YES / YES |
| LE Receiver Test [v3] | 0x204F | 39.3 | **NO** |
| LE Transmitter Test [v3] | 0x2050 | 39.4 | **NO** |
| LE Transmitter Test [v4] | 0x207B | 45.0 | YES (see caveat) |
| LE Enhanced Read Transmit Power Level | 0x2076 | 44.3 | YES |
| LE Read Remote Transmit Power Level | 0x2077 | 44.4 | YES |
| LE Set Path Loss Reporting Parameters / Enable | 0x2078 / 0x2079 | 44.5 / 44.6 | YES / YES |
| LE Set Transmit Power Reporting Enable | 0x207A | 44.7 | YES |
| BR/EDR Enable Device Under Test Mode | 0x1803 | 16.2 | YES (debugfs `dut_mode` = N on all 8) |

**"LE Set TX Power" does not exist** as a standalone HCI command in the Core spec. TX power is a parameter: `Advertising_Tx_Power` in Set Extended Advertising Parameters, and
(from the spec, not verified here) `Transmit_Power_Level` in LE Transmitter Test [v4]. The related commands that exist and are supported are listed in the table (Read Transmit Power, Enhanced Read TPL, power/path-loss reporting).

Cross-checks: `btmon` 5.77 decoded the same Read Local Supported Commands response ("237 entries" = popcount of the bitmap), and its octet/bit slot and name matched my decode for every command in my table that lies in octets 25..39
(incl. v1/v2 Tx/Rx test, all extended-advertising commands, ext scan, Read Transmit Power, RF path compensation; the v3 slots 39.3/39.4 and periodic slots 37.2-37.4 are absent from btmon's list). Wireshark/tshark 4.7.0 decoded the same capture and its LE-feature decode matched mine.
**Caveat:** btmon 5.77 and tshark do not name the bits in octets 44-45, so the exact bit of Tx Test v4 (45.0) comes from my recollection of the Core 5.3/5.4 table, not from a tool;
since octets 44 and 45 are both 0xff, v4 reads as supported regardless of which bit in those two octets it occupies. Actually executing any test command was out of scope.

## 4. LE features (`LE Read Local Supported Features` = `ffd9013cae000000`)

Supported: LE Encryption, Connection Parameters Request, Extended Reject Indication, Peripheral-initiated Features Exchange, LE Ping, **LE Data Packet Length Extension**, **LL Privacy**, Extended Scanner Filter Policies,
**LE 2M PHY**, **LE Coded PHY**, **LE Extended Advertising**, Channel Selection Algorithm #2, **LE Power Class 1**, Min Number of Used Channels, Sleep Clock Accuracy Updates, Remote Public Key Validation,
CIS Central, CIS Peripheral, LE Power Control Request, LE Power Change Indication, LE Path Loss Monitoring, Connection Subrating, Channel Classification.
**Not supported:** LE Periodic Advertising, Periodic Advertising Sync Transfer (sender/recipient), Isochronous Broadcaster, Synchronized Receiver, Stable Modulation Index (Tx/Rx), all CTE/AoA/AoD features, Periodic Adv ADI, PAwR, Channel Sounding, Decision-based Advertising Filtering.
LE supported states `ffffffffff030000` = all 42 defined LE states (btmon decode), including advertising + scanning and advertising + connection combinations.

## 5. Numeric limits (HCI reads, identical on all 8)

| Item | Value |
|---|---|
| LE Max Advertising Data Length (0x203A) | **200** bytes (0xC8). BlueZ mgmt `advinfo` and `bluetoothctl` report **251** (BlueZ uses the 251-byte ext-PDU constant and does not clamp to the controller's 200) - treat 200 as the real total per set |
| LE Number of Supported Advertising Sets (0x203B) | **12** (matches BlueZ `Max instances: 12` / `SupportedInstances: 0x0c`) |
| LE Read Transmit Power (0x204B) | min **-64 dBm**, max **+66 dBm** as reported (`c0 42`). BlueZ shows the same as `MinTxPower/MaxTxPower`. These values are not physically plausible for a BE200; do not rely on them |
| LE Read Advertising Channel TX Power (0x2007) | returned status **0x0C (Command Disallowed)** on all 8, although the bitmap says it is supported. Likely cause (inference, not tested): BlueZ already used extended-advertising commands, and legacy advertising-mode commands are rejected after that until HCI Reset |
| LE Buffer Size v1 | 251 bytes x 6 packets |
| LE Max Data Length | tx/rx 251 octets, 2704 us |
| Filter Accept List / Resolving List sizes | 64 / 64 |
| BlueZ advertising capabilities | flags: connectable, general/limited-discoverable, managed-flags, tx-power, scan-rsp-appearance, scan-rsp-local-name, secondary channels 1M/2M/CODED; `SupportedFeatures: CanSetTxPower, HardwareOffload` |
| BlueZ mgmt current settings | powered bondable ssp br/edr le secure-conn wide-band-speech cis-central cis-peripheral; `phy` query lists LE 1M/2M/Coded Tx+Rx |

## 6. Busy / idle status (verified)

- `bluetoothctl devices` and `bluetoothctl devices Connected`: empty. `btmgmt -i N con` for N=0..7: no output, rc 0 (no connections). No paired devices known.
- `bluetoothctl show <addr>` x8: `Powered: yes, PowerState: on, Discoverable: no, Discovering: no, ActiveInstances: 0x00`. `btmgmt -i N advinfo`: `Instances list with 0 items` on all 8.
- `wpctl status` (as lanforge, XDG_RUNTIME_DIR=/run/user/1000): only "Dummy Output" sink, no bluez/A2DP/HFP nodes; Blueman, MATE volume applet, obexd are running but have no devices.
- `/proc/net/hci` has only 2 sockets, both owned by bluetoothd (pid 1131). No other process holds an HCI socket. L2CAP/RFCOMM sockets (9 each in /proc/net) - the ones sampled by inode are bluetoothd's.
- rfkill: hci0..hci7 and all phys: soft blocked no, hard blocked no.
- No LANforge BT endpoints exist in the `http://localhost:8080/` root listing (GET only; the word match was only "available").
- **Conclusion: all 8 are idle and none is in use by anything; any can be borrowed.** What "borrowing" costs: each BT shares a BE200 module with its sibling Wi-Fi radio (see table), so Wi-Fi tests on that wiphy at 2.4 GHz could be affected by BT TX on the same module (module-level coexistence is expected but not measured here).

## 7. Kernel / user-channel (HCI_CHANNEL_USER = 1) support

- `/proc/config.gz`: not present. `/boot/config-*` exist only for 6.11.9 and 6.5.6 (not the running 6.18.14+), both with `CONFIG_BT=m, CONFIG_BT_HCIBTUSB=m, CONFIG_BT_INTEL=m, CONFIG_BT_LE=y` (informational only). The 6.18.14+ build tree (`/home/greearb/kernel/2.6/linux-6.18.x64`) has no `.config`.
- Direct evidence on the running kernel: `bluetooth.ko`, `btusb`, `btintel` modules loaded; `/proc/kallsyms` has `hci_sock_bind`, `hci_sock_sendmsg`, `hci_sock_setsockopt`, `hci_mgmt_chan_register` from `[bluetooth]`; `strings bluetooth.ko` contains `HCI_USER_CHANNEL`.
  The user channel itself is unconditional in `net/bluetooth/hci_sock.c` (kernel >= 3.13) - that part is from kernel knowledge; **I did not open it** to prove it.
- The devices are **UP**: btmgmt current settings show `powered` for all 8, and raw-channel commands completed on all 8 (they would fail with ENETDOWN if down). Per kernel behaviour (not tested here) a user-channel bind requires the controller to be DOWN (and bluetoothd-managed controllers to be powered off first, e.g. `btmgmt -i N power off`),
  after which bluetoothd drops the index; on close the controller re-registers and bluetoothd (default `AutoEnable=true`; `/etc/bluetooth/main.conf` has no overrides) will power it on again.
- Lower-friction alternative: the shared raw channel (what the probe used, needs CAP_NET_RAW, works while UP). It accepted all read commands; using it for Tx Test would need the controller to be LE-idle and not touched by bluetoothd meanwhile (untested, transmission forbidden).

## 8. Notes and gotchas found while probing

1. **`btmgmt` hangs without a tty**: with stdin as a file/pipe it opens the mgmt socket, fails `epoll_ctl(stdin)` (EPERM) and waits forever, even `btmgmt --help`. Run it under a pty: `script -qec "timeout 8 btmgmt info" /dev/null`. `btmgmt info` (no `-i`) lists all controllers; `-i N advinfo|phy|con` is per controller.
2. **HCI_FILTER size**: kernel 6.18 requires `setsockopt(SOL_HCI, HCI_FILTER)` optlen >= 16 (`struct hci_ufilter` incl. 2 pad bytes); the classic 14-byte pack returns EINVAL.
3. `btmon -T` crashed ("buffer overflow detected") twice on this build (core files removed); `btmon` without `-T` and `btmon -w file` work. `raw/probe.btsnoop` can be re-decoded with `btmon -r` or `tshark -r`.
4. Intel firmware details are not in HCI Read Local Version (only revision/subversion 0x4657); the build info above comes from the syslog. Intel vendor "Read Version" (0xFC05) was deliberately not sent.
5. Two stray artifacts of mine were removed (a hung `btmgmt --help` process was killed; two btmon core dumps deleted). One scratch file was briefly written to /tmp (since deleted).

## 9. What this means for the demo (analysis, labelled as such)

- **BLE channel interference** via HCI test mode is feasible on these controllers: LE Transmitter Test v1/v2/v4 exist (v2 adds PHY selection 1M/2M/Coded; v4 adds TX power level). From the spec (not verified here): the RF channel argument is 0-39 (f = 2402 + 2N MHz), so all 40 BLE channels incl. the advertising channels 37/38/39 (2402/2426/2480 MHz = RF channel index 0/12/39) are addressable; payload patterns PRBS9/PRBS15/0xAA/0x55/0xFF etc.; ends with LE Test End.
  v3 (CTE) is absent but not needed. Running it requires exclusive-ish control (user channel, or raw channel while bluetoothd is quiet) and the controller must be LE-idle.
- **ADV emulation**: use **extended advertising commands** (12 sets, 1M/2M/Coded secondary PHYs, up to 200 bytes of data per set). Legacy advertising PDU types (ADV_IND / ADV_SCAN_IND / ADV_NONCONN_IND / directed) can be emitted through the Extended Advertising_Event_Properties "legacy" bit. Do not mix legacy 0x2006-0x200A with extended commands without an HCI Reset (0x0C seen on 0x2007).
  Periodic advertising (and thus AUX_SYNC_IND emulation) is **not** available on this hardware. Via BlueZ (D-Bus `LEAdvertisingManager1`) 12 concurrent instances are available without any exclusive access.
- Borrow candidates: any of hci0..hci7 (all idle). Pick by which sibling Wi-Fi radio the Wi-Fi side of the demo is NOT using (table in section 2).
