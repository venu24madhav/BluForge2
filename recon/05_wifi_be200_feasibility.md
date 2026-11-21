# BE200 2.4 GHz Wi-Fi TX / BLE-Interference Feasibility (read-only recon)

Host: ct523c-ccb8 (10.28.3.73), LANforge 5.5.3, kernel 6.18.14+, root.
All commands below were actually run (read-only: `iw`, `cat`, `ls`, `ps`, `curl GET`, `dmesg`/`journalctl`, `grep`). No state was changed; no debugfs file was written; no GUI interaction performed.

## 1. Radio inventory / wiphy ↔ PCI ↔ kernel-phy map

From `/sys/class/ieee80211/wiphyN/index` and `device` symlink (today's boot):

| LANforge name | kernel `phy#` | PCI address | Driver |
|---|---|---|---|
| wiphy4 | phy#6 | 0000:1a:00.0 | iwlwifi (BE200) |
| wiphy5 | phy#7 | 0000:1b:00.0 | iwlwifi (BE200) |
| wiphy6 | phy#8 | 0000:1c:00.0 | iwlwifi (BE200) |
| wiphy7 | phy#10 | 0000:17:00.0 | iwlwifi (BE200) |
| wiphy8 | phy#5 | 0000:16:00.0 | iwlwifi (BE200) |
| wiphy9 | phy#9 | 0000:18:00.0 | iwlwifi (BE200) |
| wiphy10 | phy#0 | 0000:19:00.0 | iwlwifi (BE200) |
| wiphy11 | phy#4 | 0000:15:00.0 | iwlwifi (BE200) |
| wiphy0/1/2 | phy#3/2/1 | 0000:22/20/07:00.0 | ath10k_pci (QCA9984, not in scope) |

All 8 BE200 PCI IDs read `8086:272b`, confirming the task's description. No netdevs currently exist for any wiphy (`ls /sys/class/net` → only `eth0-3, lo, vrf10003, _vrf3`; `iw dev` returned empty) — i.e. **zero VAPs/stations/monitor interfaces are up right now on any radio**.

## 2. `iw phy <wiphyN> info` key facts (identical on wiphy4,5,6,7,8,9; wiphy10/11 differ only in channel 14)

- **Supported interface modes:** IBSS, managed, AP, AP/VLAN, monitor, P2P-client, P2P-GO, P2P-device — **AP and monitor are both supported in firmware/driver**.
- **Valid interface combinations:**
  - `#{managed}<=2, #{P2P-client,P2P-GO}<=1, #{P2P-device}<=1, total<=4, #channels<=2`
  - `#{managed}<=2, #{AP,P2P-client,P2P-GO}<=1, #{P2P-device}<=1, total<=4, #channels<=1`
  - (monitor is not combo-constrained — mac80211 allows a monitor vif alongside any combination, consistent with `WANT_MONITOR_VIF` hw flag seen in debugfs `hwflags`.)
- **2.4 GHz channel list (Band 1), wiphy4/5/6/7/8/9:**
  ```
  * 2412 MHz [1]  (22.0 dBm)
  * 2417 MHz [2]  (22.0 dBm)
  * 2422 MHz [3]  (22.0 dBm)
  * 2427 MHz [4]  (22.0 dBm)
  * 2432 MHz [5]  (22.0 dBm)
  * 2437 MHz [6]  (22.0 dBm)   <- overlaps BLE ch 2437 directly
  * 2442 MHz [7]  (22.0 dBm)
  * 2447 MHz [8]  (22.0 dBm)
  * 2452 MHz [9]  (22.0 dBm)
  * 2457 MHz [10] (22.0 dBm)
  * 2462 MHz [11] (22.0 dBm)  <- overlaps BLE ch 2462 directly
  * 2467 MHz [12] (22.0 dBm)
  * 2472 MHz [13] (22.0 dBm)
  * 2484 MHz [14] (disabled)
  ```
  **No "no IR", "disabled", "radar detection" or "passive scan" flags on channels 1-13** — all 13 are TX-permitted at 22 dBm under the current regdomain. (Ch 1 = 2412 MHz is also directly inside the BLE 2402-2437 overlap region.)
  wiphy10/wiphy11 are identical except **channel 14 (2484 MHz) shows 22.0 dBm instead of "disabled"** — a real difference between radios worth flagging but not needed for ch1/6/11.
- **Max TX power:** 22 dBm flat across the whole 2.4 GHz band on all 8 radios (self-managed regdomain, see below).
- No DFS/radar flags in the 2.4 GHz band (DFS only applies to 5 GHz on these phys).

## 3. Regulatory domain / LAR

`iw reg get`:
- **global**: `country US: DFS-FCC` (used by ath10k wiphy0-2, non-self-managed).
- **phy#4,5,6,7,8,9,10 (self-managed)**: `country US: DFS-UNSET`, 2.4 GHz rules printed as `(2402-2437 @ 40),(6,22)`, `(2422-2462 @ 40),(6,22)`, `(2447-2482 @ 40),(6,22)` — all **AUTO-BW, no NO-IR, no DFS flag** in the 2.4 GHz rows. This confirms the BE200's firmware/driver is doing its own (self-managed / LAR-style) regulatory enforcement, and that enforcement currently permits normal active TX at 22 dBm across ch1-13.
- This matches the per-channel list in §2 — no contradiction between `reg get` and `phy info`.

## 4. Driver / firmware version

- `modinfo iwlwifi`: `vermagic: 6.18.14+ SMP preempt mod_unload`, driver ships with firmware blob table but the **loaded** firmware per-radio (read from debugfs, see below) is:
  ```
  FW id: 101.6ef20b19.0 gl-c0-fm-c0-101.ucode
  FW: jenkins-Core_manual_signed_core98-159:signed_manual:6ef20b19
  Device: Intel(R) Wi-Fi 7 BE200 320MHz
  Bus: pci
  ```
  Identical on all 8 BE200 radios (wiphy4-11).
- `dmesg` (ring buffer, current boot only, ~469k-470k sec uptime window) had **no iwlwifi driver-load lines** — the ring buffer has wrapped since boot (12:18 today) and only contains recent runtime chatter (sta/vif churn, ath10k debug). `journalctl -k` for the single available boot likewise returned 0 `iwlwifi` lines (journal retention is short / rotated). Firmware string above (from live debugfs `fw_ver`) is the authoritative source actually used.
- Module param `remove_when_gone = N`, `fw_restart = Y` (iwlwifi will auto-restart firmware on assert — explains why crashed radios recover and crash dumps + kern logs get written automatically on each event, rather than radios staying dead).

## 5. iwlwifi / iwlmld module parameters (`/sys/module/iwlwifi/parameters/`)

```
11n_disable=0  allow_puncturing=1  amsdu_size=0  bt_coex_active=Y  debug=0
disable_11ac=N disable_11ax=N disable_11be=N  dsm_override=0,0,...
enable_ini=16  fw_dbg_buffer_order=19  fw_restart=Y  led_mode=0
nvm_file=(null)  power_level=0  power_save=N  remove_when_gone=N
swcrypto=0  uapsd_disable=3  wbem_override=0
```
`/sys/module/iwlmld/parameters/`: `power_scheme = 2`.
No module param disables monitor mode, TX, or 2.4 GHz; `bt_coex_active=Y` is notable since BT coexistence logic runs on the same silicon as the BLE/BT radios in scope for this project — any 2.4 GHz Wi-Fi TX test should expect BT-coex interaction, not a hard blocker but a variable.

## 6. Busy vs idle — table

`ps` shows **one `wpa_supplicant` + `wpa_cli` + `parse-wpacli-event.pl` trio per BE200 radio** (wiphy0,1,2,4,5,6,7,8,9,10,11 all have a ~5-day-old wpa_supplicant, this is LANforge's standard per-radio management supplicant, started at boot/GUI-launch, PID group e.g. 248348/248351/248354 for wiphy4). This is **by design** and does **not** by itself mean the radio is in use.

LANforge `GET /radiostatus/all` (ground truth, read-only) for all 8 BE200 wiphys today:

| wiphy | channel | vaps_up | stations_up | monitors_up | Verdict |
|---|---|---|---|---|---|
| wiphy4 | -1 | 0 | 0 | 0 | **idle** |
| wiphy5 | -1 | 0 | 0 | 0 | **idle** |
| wiphy6 | -1 | 0 | 0 | 0 | **idle** |
| wiphy7 | -1 | 0 | 0 | 0 | **idle** |
| wiphy8 | -1 | 0 | 0 | 0 | **idle** |
| wiphy9 | -1 | 0 | 0 | 0 | **idle** |
| wiphy10 | -1 | 0 | 0 | 0 | **idle** |
| wiphy11 | -1 | 0 | 0 | 0 | **idle** |

`iw dev` confirms zero netdevs exist for any wiphy right now (no interfaces to even check `iw dev <if> link` on). **All 8 BE200 radios are currently idle** — every one is available to "borrow" for the demo without disturbing another active test, as of this recon snapshot. (Being idle now is a point-in-time fact; re-check `/radiostatus/all` before the actual demo in case another LANforge user/script brings a radio up in the meantime.)

## 7. Injection / tooling inventory

- `python3 scapy`: **not installed** (`ModuleNotFoundError: No module named 'scapy'`), including inside LANforge's own `scripts/venv-3.12` venv — would need `pip install scapy` (an install, out of scope for this read-only task).
- `aireplay-ng`, `mdk4`, `mdk3`, `tcpreplay`: **not found** on PATH.
- `tcpdump`: present (`/usr/sbin/tcpdump`).
- `iw` 5.19, `ip`: present.
- `grep -ril 'radiotap\|inject' /home/lanforge/scripts --include=*.py`: hits were generic (`lf_pcap.py`, zoom/teams automation scripts, and files inside the `venv-3.12` site-packages, e.g. `prompt_toolkit`, `attr`, `setuptools` — false positives from substring matches, not real injection tools).
- `lf_sniff_radio.py` exists in 5 locations (`~/lanforge-scripts/py-scripts/`, `~/scripts/py-scripts/`, `~/local/webGUI/scripts/`, and two versioned `LANforgeServer-5.5.{2,3}/...` trees) — this is LANforge's monitor-mode **capture/sniff** helper (drives `dumpcap` on a monitor vif), not a frame-injection tool.
- **Conclusion: no ready-made raw-frame-injection tool is installed.** Monitor-mode raw TX would require either installing scapy (`pip install scapy`, needs network/package access — out of scope today) or using the iwlwifi debugfs `inject_packet` hook described next.

## 8. iwlwifi/iwlmld debugfs — TX-test / injection surfaces that exist (NOT written to)

`mount | grep debugfs` → `debugfs on /sys/kernel/debug ... (rw,nosuid,nodev,noexec,relatime)` — mounted and traversable by root.

`ls /sys/kernel/debug/ieee80211/` → `wiphy0 wiphy1 wiphy2 wiphy4 wiphy5 wiphy6 wiphy7 wiphy8 wiphy9 wiphy10 wiphy11` (directories are named by **wiphyN, not phyN** — matches the task's naming note).

For every BE200 wiphy, `ieee80211/wiphyN/iwlwifi/iwlmld/` contains (listing only, nothing read/written beyond safe `fw_ver`):
```
enabled_severities  ethtool_stats  fw_dbg_clear  fw_dbg_collect  fw_dbg_domain
fw_info  fw_nmi  fw_restart  fw_ver  he_sniffer_params  inject_packet
max_sleep  nic_temp  rx_ts_ptp  send_echo_cmd  send_hcmd  set_rate_override
start_ctdp  stop_ctdp  tas_get_status  timestamp_marker  wifi_6e_enable
```
and `ieee80211/wiphyN/iwlwifi/trans/` contains `csr fh_reg fw_trigger_timeout interrupt monitor_data reset rf rfkill rx_queue tx_queue`.

**`inject_packet` exists on every BE200 radio** — this is the standard iwlwifi/iwlmld debugfs write-node for pushing a raw 802.11 frame straight to firmware TX, bypassing mac80211/monitor-vif entirely. It was **not written to** (read-only task), but its presence is the most direct "raw frame generator" surface on this hardware — more direct than classic monitor+injection (no `aireplay-ng`-style tool needed, just a `echo <hex frame> > inject_packet`-style write, which is out of scope today). `set_rate_override` would let a future test force a specific MCS/BW/TPC for that injected frame. No `tx-continuous` or dedicated `tx_test` entry was found by name; `start_ctdp`/`stop_ctdp` relate to thermal (continuous thermal dissipation power) budget control, not TX waveform test mode.

## 9. hostapd / AP-mode support

`/home/lanforge/local/bin/hostapd` exists (8.5 MB, May 16 build), `hostapd -v` → `hostapd v2.12-devel-hostap_2_3-14146-ga7dba7dab`. Combined with §2 (`Supported interface modes` includes AP, and the AP-containing interface combination `#{AP,P2P-client,P2P-GO}<=1, total<=4, #channels<=1`), **the BE200 does support AP mode in 2.4 GHz on this kernel/driver**, so a LANforge vAP on ch1/6/11 is architecturally valid (not verified end-to-end since no vAP was brought up — read-only constraint).

## 10. BE200 firmware crash history — **demo risk, verified**

22 crash-dump pairs (`be200_fw_crash_dump-*.bin` + `be200_fw_kern_logs-*.txt`) exist in `/home/lanforge`, spanning Jun 1 – Sep 16 2026, grouped by filename tag into 3 radios/incidents:

| File tag | Count | Dates | Crashing PCI (verified from log) | Current wiphy for that PCI |
|---|---|---|---|---|
| wiphy4 | 10 | Aug 6 11:45-11:56 | 0000:1a:00.0 | wiphy4 (matches tag) |
| wiphy6 | 3 | Jul 27 11:55-11:58 | 0000:1c:00.0 | wiphy6 (matches tag) |
| wiphy10 | 9 (3 incidents × 3 files) | Jun 1 15:36-15:38, Jun 30 22:52-22:54, Sep 16 14:31-14:33 | 0000:19:00.0 | wiphy10 (matches tag) |

All crashes show the identical firmware fault signature:
```
iwlwifi <pci>: Microcode SW error detected. Restarting 0x0.
iwlwifi <pci>: Loaded firmware version: 101.6ef20b19.0 gl-c0-fm-c0-101.ucode
iwlwifi <pci>: 0x0000004E | ADVANCED_SYSASSERT
```
(same firmware build that is loaded right now, per §4 — the crash signature is not stale/fixed-since).

**Root-cause correlation found by direct log inspection:**
- **wiphy10 (0000:19:00.0), all 3 incidents:** the line immediately preceding "Microcode SW error detected" (within 1-14 seconds) is **`iwlwifi 0000:19:00.0 moni3a: entered promiscuous mode`**, e.g.:
  ```
  Sep 16 14:31:02 ... lf_shell_cmd[73145]: ... cmd[dumpcap -n -i moni3a -w dfs_test_capture.pcap -a duration:300]
  Sep 16 14:31:02 ... kernel: iwlwifi 0000:19:00.0 moni3a: entered promiscuous mode
  Sep 16 14:31:02 ... kernel: iwlwifi 0000:19:00.0: Microcode SW error detected. Restarting 0x0.
  ```
  This same pattern repeats verbatim (same interface name `moni3a`, same PCI, same ADVANCED_SYSASSERT) on Jun 1 and Jun 30 as well — **three independent, reproducible firmware crashes on wiphy10 directly triggered by bringing up monitor mode.** This is the single most important finding for part (b) of the task.
- **wiphy4 (0000:1a:00.0, Aug 6) and wiphy6 (0000:1c:00.0, Jul 27):** no monitor-mode (`moni*`/promiscuous) activity appears anywhere in either full kern-log file (only 1-2 unrelated substring hits). Instead, the crash follows a burst of concurrent station authentication/DHCP churn (57+ `sta-info-insert-drv-state` events on wiphy4 in that window, i.e. many simultaneous client stations associating/DHCP'ing) — a scale/AP-mode stress pattern, not an injection pattern.

**Implication:** monitor mode itself is not proven universally unstable (wiphy4/5/6/7/8/9/11 have no crash history tied to monitor mode), but **wiphy10 specifically has a 3-for-3 reproducible firmware crash when entering monitor mode**, under the exact firmware build currently loaded on all 8 radios. Any monitor-mode / raw-injection plan for the demo should **avoid wiphy10** and should treat a firmware crash (`fw_restart=Y` auto-recovers the radio in a few seconds, per module params, but an on-stage Wi-Fi radio going dark for several seconds during a live demo is a visible glitch) as a real, non-zero-probability risk on the remaining radios too, since the same firmware/driver stack is shared.

## 11. Feasibility verdict

1. **Can a BE200 put usable energy on a 2.4 GHz Wi-Fi channel overlapping BLE?** Yes, by firmware/driver/regulatory capability:
   - Ch1 (2412), ch6 (2437), ch11 (2462) are all TX-permitted at 22 dBm, no IR/DFS restriction, on every BE200 radio (wiphy4-11) under the self-managed regdomain currently loaded.
   - **(a) LANforge-native vAP + station + L3 UDP**: architecturally supported — AP mode is in "Supported interface modes" and in a valid interface combination, hostapd v2.12-devel is present and bundled by LANforge, and all 8 BE200 radios are currently idle (§6) so any of them can host the vAP. This was **not end-to-end verified** (no vAP was actually brought up, per the read-only constraint) but every static precondition checks out.
   - **(b) Monitor-mode raw injection**: monitor mode is listed as a supported interface type and `WANT_MONITOR_VIF` is set; the most direct raw-TX surface found is the per-radio `iwlwifi/iwlmld/inject_packet` debugfs node (present on all 8 BE200 radios), not a userspace tool — because **no injection tool (scapy, aireplay-ng, mdk4, mdk3, tcpreplay) is installed** on the unit today. Building this path means either `pip install scapy` + a classic monitor-vif + raw-socket injector, or driving `inject_packet` directly — both are additional setup work beyond what exists today.

2. **Simplest, most robust method for the Ericsson demo:** **(a) LANforge-native vAP (hostapd AP) on ch1/6/11 + a UDP CX for continuous traffic**, using any BE200 radio other than wiphy10. This uses only software and binaries LANforge already ships (hostapd, LANforge GUI/API, generic L3 endpoint), needs no new package installs, and avoids the one radio with a demonstrated, repeatable firmware-crash trigger. Monitor-mode raw injection is a higher-effort, higher-risk fallback given the missing tooling and wiphy10's crash history (even if a different, non-wiphy10 radio is used for it, the same firmware build is shared across all 8 radios).

3. **Risks:**
   - **Firmware crash risk is proven, not hypothetical**: 22 real ADVANCED_SYSASSERT crash-dump pairs exist on this exact unit, same firmware build (`101.6ef20b19.0 gl-c0-fm-c0-101.ucode`) that is loaded right now. wiphy10 crashed 3/3 times immediately on monitor-mode entry — **do not use wiphy10 for the live demo, especially not for monitor-mode/injection.** wiphy4 and wiphy6 crashed once each under heavy multi-station AP load (not demo-relevant at small scale, but shows the firmware is not bulletproof under stress).
   - `fw_restart=Y` means a crash auto-recovers in a few seconds rather than requiring operator intervention, but a live Wi-Fi interference source blinking off mid-demo is still a visible failure mode to plan around (e.g. have a second idle BE200 as a hot spare — wiphy5/7/8/9/11 have no crash history at all).
   - Regulatory/LAR: self-managed regdomain currently shows no NO-IR restriction on ch1-13, but this is runtime state (LAR), not a hard guarantee — re-verify with `iw reg get` at demo time in case something changes it.
   - `bt_coex_active=Y` module param means the BE200's own BT-coexistence logic is active; since the project also involves BLE/BT radios on the same box, Wi-Fi-vs-BT coexistence behavior on the BE200 itself is an untested variable for the "Wi-Fi radio as deliberate interferer" use case.
   - No injection tooling is installed (scapy/aireplay-ng/mdk4 all absent) — the monitor-injection path (option b) cannot be demonstrated today without an install step, which is out of scope for this read-only recon.

## Files referenced (read-only, sources on target unit, not copied)
`/sys/class/ieee80211/wiphy{4-11}/*`, `/sys/kernel/debug/ieee80211/wiphy{4-11}/iwlwifi/*`, `/sys/module/iwlwifi/parameters/*`, `/sys/module/iwlmld/parameters/*`, `/home/lanforge/be200_fw_crash_dump-*.bin`, `/home/lanforge/be200_fw_kern_logs-*.txt`, `/home/lanforge/local/bin/hostapd`, LANforge API `GET http://localhost:8080/radiostatus/all`.
