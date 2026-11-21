# 01 - LANforge API / CLI deep-dive (READ-ONLY recon)

Unit: ct523c-ccb8 (10.28.3.73), LANforge 5.5.3 (git f41c0cc25fc7dca964c00c01019e0a0ae93cded2, built 2026-05-15, JsonVersion 1.0.37577, realm 55).
Recon date: 2026-10-05 ~18:10-18:30 IST. Everything below was obtained with GET requests, local file reads, `strings`/`javap`/`readelf` on binaries, `ip`/`iw`/`ps`/`ss`/`lsmod`/`lsusb`. Nothing was POSTed, started, stopped or transmitted.
Items marked **(inferred)** or **(not tested)** were NOT proven by running them.

Raw GET dumps are in `recon/api_dump/` (radiostatus_all.json, rfgen_.json, port*.json, resource_1_1.json, cx*.json, endp*.json, wifi-msgs_last_5.json, help_ordered_params_all_cmds.json, param_docs_from_python_client.txt).
Python read-only example: `recon/examples/list_radios_readonly.py` (run and verified).

---------------------------------------------------------------------------------------------------

## 0. Bottom line

* API: `http://localhost:8080/` (Java GUI `lfclient`, pid 72529, listens on 0.0.0.0:8080 - no auth; also 127.0.0.1:8081). Telnet CLI is `btserver` on 4001 (GUI/binary 4002-4004). A Candela Django "interop-webGUI" + celery already runs on 10.28.3.73:5454 (so do not reuse 5454).
* `/help/<cmd>` returns ONLY the ordered parameter names (`explanation`/`value` are always "NA"). Parameter meaning/defaults come from the Python client docstrings (generated from the CLI user guide) - captured in section 1.
* **No LANforge CLI command or GET endpoint deals with Bluetooth/BLE/HCI.** LANforge has no BLE ADV or BLE interference feature in this build. (Details in 1.3.)
* All 8 BE200 radios (wiphy4-11) are completely idle right now: no stations/vAPs/monitors, no netdev, channel -1 (AUTO). All 3 ath10k (wiphy0-2) are idle too. Every BE200 can host exactly ONE vdev (max_vifs=1).
* HackRF is exposed as RF generator `1.1.26b468dc3498648f`, state `Stopped`, status `Idle`, all settings -1. No `rf_helper`/`ofdm_helper` process is running. Licence file has `RFGEN 1` and `RFGEN-AUTO 1` (licence timestamp 1821707522 = 2027-09-23, the same date the web GUI's licensedata.json lists for the "dfs" feature).
* set_rfgen is a "RF Noise-generator (RADAR) config" command. Native waveforms are radar pulse sets, a chirp set, hop sets, `GENERIC` pulses and an `OFDM` mode whose header modulation can be `NOISE` (wideband noise) with T1/T2/T3 on/off duty-cycle control (T1_on=0 => continuous). There is NO native CW tone, FM or narrowband-GFSK type. A true CW tone needs `hackrf_transfer -c` (shipped in /home/lanforge/local/bin) outside the rfgen path.

---------------------------------------------------------------------------------------------------

## 1. CLI commands - ordered parameters (GET /help/<cmd>)

262 names are in `lanforge_cli_commands.txt`, but 3 of them (`cmd`, `explanation`, `value`) are artifacts of the key-extraction, so there are 260 real commands. 253 return parameter lists; 7 take no arguments (motd, quit, show_clients, show_dbs, show_event_interest, version, who). Full list: `api_dump/help_ordered_params_all_cmds.json`.

Legend (from the client docstrings): `[R]` required, `[D:x]` default x, `[W]` marker used by the docs (kept as-is). Most params are strings; when omitted they are "NA"/unchanged. The Python client renames three params because they clash with Python builtins: rfgen `id` -> `p_id`, add_endp/add_gen_endp `type` -> `p_type`, sniff_port `filter` -> `p_filter` (the JSON keys sent on the wire are still `id`, `type`, `filter`).

### 1.1 RF generator

**set_rfgen** (35) : `shelf resource id rfgen_flags rfgen_flags_mask pulse_width_us pulse_interval_us pulse_count sweep_time_ms freq_khz gain if_gain bb_gain radar_type prf1 prf2 prf3 freq_modulation uut_channel burst_offset long_pulse_width_us chirp_width_khz ofdm_header ofdm_payload ofdm_t1_on ofdm_t1_off ofdm_t2_on ofdm_t2_off ofdm_t3_on ofdm_t3_off ofdm_duration trigger_dbm trigger_amp display sample_rate`
(CLI short description from btserver/lfclient: "Set RF Noise-generator (RADAR) config.") - full per-parameter semantics in section 5.

**show_rfgen** (3) : `shelf resource id` - id = HackRF serial or `all`; resource may be `all`; shelf default 1.
**rm_rfgen** (3) : `shelf resource id` - removes the RF-generator entity (serial) from the config. (not tested)

### 1.2 Wi-Fi radio / vdev management

**set_wifi_radio** (33) : `shelf resource radio mode channel country frequency frag_thresh rate rts txpower mac antenna flags flags_mask const_tx pulse_width pulse_interval vdev_count peer_count stations_count rate_ctrl_count fwname fwver txdesc_count tids_count skid_limit active_peer_count tx_pulses pulse2_interval_us max_amsdu pref_ap ampdu_factor`
* `radio` e.g. `wiphy4` [W]; `channel`/`frequency`: `0xFFFF`, `AUTO` or `DEFAULT` = any, frequency wins if both set; `mode`: numeric table below; `antenna`: 0 diversity/all, 1 Fixed-A (1x1), 4 AB (2x2), 7 ABC, 8 ABCD, 9 8x8; `txpower`: dBm or `AUTO`; `rts` off or 1-2347; `frag_thresh` 256-2346 (2346=off); `country` numeric; `max_amsdu`; `ampdu_factor` "ax200/ax210 only, currently, requires module reload, OS default 0xFF"; `pref_ap` preferred BSSID for all station vdevs.
* flags (`flags`/`flags_mask`): `hw_sim 0x1`, `no_scan_share 0x40`, `verbose 0x10000`, `no_sw_crypt 0x20000`, `ct_sta_mode 0x40000`, `firmware_cfg 0x80000`, `ignore_radar 0x200000`, `allow_all_mcs 0x400000` (ath10k), `no_runtime_pm 0x800000` (mtk7921k), `extra_txstatus 0x1000000`, `extra_rxstatus 0x2000000`, `ofdma_stats 0x4000000`, `txs_all_skb 0x8000000` (MTK), `use_syslog 0x20000000`.
* `const_tx`, `pulse_width`, `pulse_interval`, `tx_pulses`, `pulse2_interval_us`: documented as an "RF Pattern Generator / RF noise generator" built into the Wi-Fi radio (`const_tx` = 32-bit encoded pattern, "see above" - the encoding table is not in the local docs). `mode` is also documented as "WiFi mode for radar emulation" for this feature. Whether BE200/iwlwifi honours any of this is **not verified** (looks like an ath9k/ath10k-CT feature); do not assume it works on BE200.
* mode numbers (same table for add_sta/add_vap): 0 AUTO, 1 a, 2 b, 3 g, 4 abg, 5 abgn, 6 bgn, 7 bg, 8 abgn-AC, 9 an-AC, 10 an, 11 bgn-AC, 12 abgn-AX, 13 bgn-AX, 14 an-AX, 15 a-AX, 16 abgn-EHT, 17 bgn-EHT, 18 an-EHT, 19 a-EHT (6E disables /n and /ac).

**add_sta** (23) : `shelf resource radio sta_name flags ssid nickname key ap wpa_cfg_file mac mode rate max_amsdu ampdu_factor ampdu_density sta_br_ip flags_mask ieee80211w x_coord y_coord z_coord rsn_override`
* `sta_name` e.g. `sta0` [W]; `ssid` (`[BLANK]` = empty SSID, `0x...` = hex); `key` (prefix `0x` for ascii-hex); `ap` BSSID or `DEFAULT` = any; `mac` may use pattern `xx:xx:xx:*:*:xx`; `mode` default 0; `rate` max rate; `max_amsdu` 1/0/0xFF=unset; `ampdu_factor` 0-3/0xFF; `ampdu_density` 0-7/0xFF; `ieee80211w` 0 off / 1 optional / 2 required; `rsn_override` 0/1/2; `sta_br_ip` 0.0.0.0 = MAC bridging.
* useful flags: `wpa_enable 0x10`, `custom_conf 0x20`, `wep_enable 0x200`, `wpa2_enable 0x400`, `ht40_disable 0x800`, `scan_ssid 0x1000`, `passive_scan 0x2000`, `disable_sgi 0x4000`, `verbose 0x10000`, `disable_roam 0x80000000`, `ht160_enable 0x100000000`, `create_admin_down 0x1000000000` (create the station admin-down), `use_wpa3 0x10000000000`, `disable_ofdma 0x200000000000`, `disable_twt 0x100000000000`, `be320_enable 0x4000000000000`, `disable_mlo 0x8000000000000`. Always send `flags_mask` for the bits you mean to set/clear.

**add_vap** (20) : `shelf resource radio ap_name flags ssid key mac beacon frag_thresh custom_cfg max_sta dtim_period mode flags_mask rate x_coord y_coord z_coord ieee80211w`
* `ap_name` e.g. `vap0` [W]; `beacon` interval in 1.024 ms units, default 100, range 15..65535; `dtim_period` 1..255 default 2; `max_sta` 1..2007; `custom_cfg` custom hostapd config; `frag_thresh` UN-USED (use NA).
* flags: `enable_wpa 0x10`, `hostapd_config 0x20`, `enable_80211d 0x40`, `short_preamble 0x80`, `wep_enable 0x200`, `wpa2_enable 0x400`, `disable_ht40 0x800`, `verbose 0x10000`, `80211h_enable 0x10000000`, `mcast_to_ucast 0x80000000`, `ht160_enable 0x100000000`, `create_admin_down 0x1000000000`, `use_wpa3 0x10000000000`, `be320_enable 0x4000000000000`.

**add_monitor** (8) : `shelf resource radio ap_name flags flags_mask aid bssid`
* `ap_name` = monitor name e.g. `moni0` [W]; `aid`/`bssid` optional for /ax radios. Monitor flags: `disable_ht40 0x800`, `disable_ht80 0x8000000`, `ht160_enable 0x100000000`, `be320_enable 0x4000000000000`. The monitor has no channel param: the channel comes from the radio, so py-json (`wifi_monitor_profile.create`) does `set_wifi_radio` (mode/channel/country/frequency) then 1 s later `add_monitor` - verified by reading /home/lanforge/scripts/py-json/wifi_monitor_profile.py.

**rm_vlan** (3) : `shelf resource port` - the removal command for ports/stations/vAPs/monitors (there is no rm_sta/rm_vap/rm_port in the 260-command list; `port` = interface name like `sta0000`/`vap0`/`moni0` [W]). (not tested)

**set_port** (37) : `shelf resource port ip_addr netmask gateway cmd_flags current_flags mac mtu tx_queue_len alias interest report_timer flags2 br_priority br_aging_time br_max_age br_hello_time br_forwarding_delay br_port_cost br_port_priority ipv6_addr_global ipv6_addr_link ipv6_dflt_gw bypass_wdt cpu_mask dns_servers sta_br_id dhcp_client_id current_flags_msk dhcp_vendor_id ipsec_concentrator ipsec_passwd ipsec_local_id ipsec_remote_id dhcp_hostname`
* Port down/up is the `current_flags` bit `if_down 0x1` (**inferred**: 1 = down, 0 = up); Candela's own example in /home/lanforge/scripts/archive/json/json_port_test.sh posts `{"shelf":1,"resource":1,"port":"eth3","current_flags":1,"interest":2}` (interest bit 0x2 = "apply current flags"). `use_dhcp 0x80000000` flag, `interest` bits: `command_flags 0x1`, `current_flags 0x2`, `ip_address 0x4`, `ip_Mask 0x8`, `ip_gateway 0x10`, `mac_address 0x20`, `mtu 0x100`, `alias 0x1000`, `dhcp 0x4000`, `rpt_timer 0x8000`. `cmd_flags`: `reset_transceiver 0x1`, `probe_wifi 0x10`, `skip_port_bounce 0x100`, `from_user 0x80` (required to change mgt port config).

**reset_port** (5) : `shelf resource port reset_ospf pre_ifdown` - `port` may be `ALL`, `resource` may be `ALL`; reset_ospf `NO`/`NA` = don't update OSPF; pre_ifdown see docs, NA if unsure.
**nc_show_ports** (4) : `shelf resource port probe_flags` - ask the server to refresh port info; `probe_flags` additive: `WIFI 1`, `MII 2`, `ETHTOOL 4`, `BRIDGE 8`, `EASY_IP_INFO 0x10`, `GW 0x20`, `GW_FORCE_REFRESH 0x40`; leave blank for stats only. (It is issued as a POST -> not used here.)
**scan_wifi** (5) : `shelf resource port key extra` - `port` = the station/vap (virtual interface) that should scan [W]; results later via GET `/scan-results/1/<res>/<port>`; `key` unique id, usually blank; `extra` extra args to the scan script. (needs a station/vap on the radio -> not usable on an idle radio)
**sniff_port** (9) : `shelf resource port display flags outfile duration snaplen filter` - flags `TSHARK 1`, `DUMPCAP 2`, `MATE_TERMINAL 4`, `MATE_XTERM 8`, `MATE_KILL_DUMPCAP 0x10`; `duration` seconds (default 5 min for dumpcap/tshark); `outfile` optional capture file; `snaplen` default whole packet; `display` X DISPLAY like `192.168.1.5:0.0`.
**wifi_event** (4) : `device event status msg` - injects a wifi event record (`device` interface or PHY [R], `event` [R]).
**add_vlan** (6) : `shelf resource port vid old_name report_timer` (listed for completeness).

### 1.3 Layer-3 / generic traffic

**add_endp** (17) : `alias shelf resource port type ip_port is_rate_bursty min_rate max_rate is_pkt_sz_random min_pkt max_pkt payload_pattern use_checksum ttl send_bad_crc_per_million multi_conn`
* `alias` [R]; `type` endpoint type: `lf_udp`, `lf_tcp`, `lf_udp6`, `lf_tcp6`, `mc_udp`, `custom_udp`, `custom_tcp`, `custom_ether`, `lf`, `lf_sctp` ... (enum `AddEndpType` in the client); `payload_pattern`: increasing/decreasing/zeros/ones/random/random_fixed/custom/PRBS_*; `ip_port` -1 = let server pick, 0 = ANY; `min_rate` bps (only rate if not bursty), `max_rate` used if bursty; `min_pkt` -1 AUTO [D:-1], `max_pkt` 0 = same, -1 AUTO [D:0]; `is_rate_bursty`/`is_pkt_sz_random`/`use_checksum` = `Yes` or anything else = no; `multi_conn` >0 spawns a helper process with that many connections.
**add_cx** (4) : `alias test_mgr tx_endp rx_endp` - `alias` [R], `test_mgr` default `default_tm`, `tx_endp` [R], `rx_endp`.
**set_cx_state** (3) : `test_mgr cx_name cx_state` - cx_state one of `RUNNING SWITCH QUIESCE STOPPED DELETED`; `cx_name`/`test_mgr` may be `all`. NOTE: the sample in the API index root uses `{"alias":"cx0001","state":"RUNNING"}` - those key names do NOT match `/help/set_cx_state`; use `test_mgr`, `cx_name`, `cx_state`.
**add_gen_endp** (8) : `alias shelf resource port type subtype gen_flags group` - subtype `gen_none` (default) / `gen_zoom` / `gen_teams` / `gen_meet`; type `gen_generic` (default); gen_flags default 0.
**rm_cx** (2) : `test_mgr cx_name` (`all` allowed). **rm_endp** (1) : `endp_name` (`YES_ALL` allowed - careful).

### 1.4 Bluetooth/BLE search result (explicitly asked)

Searched: all 253 `/help/<cmd>` payloads, `lanforge_help.json`, `lanforge_cli_commands.txt`, `lanforge_api_root.json` (201 URL mappings, none contain bluetooth/ble/hci/gatt/advert/beacon-as-BLE).
Only Bluetooth-adjacent items found, all related to driving Android phones, not HCI/BLE:
* `adb_bt` (`shelf resource adb_id keystrokes`) - send Bluetooth-keyboard keystrokes to an ADB device.
* `add_adb` has `bt_ctrl_dev` and `bt_mac`; `add_voip_endp` has `mobile_bt_mac`.
* /home/lanforge/lanforge-scripts/btstack_runner.sh - "start a Bluetooth Keyboard connection, via btstack operating on a USB dongle" (for the Android keyboard test).
* `btserver` (the LANforge server binary, listens on 4001-4004) is NOT Bluetooth; "bt" there is a LANforge-internal prefix.
=> The Ericsson demo's BLE interference/ADV generation cannot be done through any LANforge JSON/CLI command; it must come from the Linux BlueZ HCI side (hci0-hci7) plus the HackRF.

---------------------------------------------------------------------------------------------------

## 2. Current live state (GET only)

### 2.1 System

* `GET /resource/1/1`: hostname ct523c-ccb8, sw 5.5.3 64bit, kernel 6.18.14+, i7-7700T x4, mem 16.3 GB, ctrl-ip 10.28.3.73, ports "0..15 22" (ports 15 and 22 are not in the `/port` list and `/port/1/1/15` and `/port/1/1/22` return all-"NA", i.e. leftover/phantom entries), "sta up":12 (this is the *config* "max stations to bring up per radio per 0.25 s tick", NOT a count of stations up).
* `GET /shelf/1` -> one resource `/resource/1/1`.
* `GET /port` -> only `eth0 eth1 eth2 eth3` and `wiphy0 wiphy1 wiphy2 wiphy4..wiphy11` (ports 0-14). NO station/vap/monitor ports exist.
* `GET /stations/` -> 404 "table not populated" (this endpoint lists stations associated to *vAPs*; none exist).
* `GET /cx` and `GET /endp`: three leftover cross-connects `udp--1.eth2-01.sta5-0/-1/-2` with endpoints 40-45; `GET /cx/<name>` shows `"state":"PHANTOM"` (the port `sta5` no longer exists) -> stale entries, not running traffic (`"run":false`).
* `GET /wifi-msgs/last/5`: the 5 newest records (timestamps 2026-10-05 17:56:21 .. 17:56:26 IST, i.e. ~15-20 min before this recon) are a mass teardown: dozens of `deauth ... reason 3`, `disconnected (local request)` and `phy #N: del interface type station` for sta0000, sta4, sta5, sta10-19, sta24... on phy#1,#2,#5,#6,#10. So somebody/something (the Django DFS test? a manual cleanup?) deleted all stations at 17:56 today. Kernel phy# (iw) vs LANforge names: phy#0=wiphy10, #1=wiphy2, #2=wiphy1, #3=wiphy0, #4=wiphy11, #5=wiphy8, #6=wiphy4, #7=wiphy5, #8=wiphy6, #9=wiphy9, #10=wiphy7.
* `GET /events/last/3`: only DHCPd ACKs on eth2 (a lab device 24:fe:9a:10:9c:d9 renewing 172.16.213.85). eth2 runs LANforge's DHCP server (172.16.0.1/vrf10003); an AP (BSSID 24:fe:9a:10:9c:dc, SSID `ssid_wpa2_5g`) is the DUT of the earlier DFS tests.

### 2.2 Radios (GET /radiostatus/all, cross-checked with sysfs / `iw`)

Cross-check (kernel level): `iw dev` prints nothing (rc=0), `/sys/class/net` has only eth0-3, lo, vrf10003, _vrf3; `ps` shows no hostapd, no dumpcap/tshark, only the idle per-radio `wpa_supplicant -g /var/run/wpa_supplicant_if_wiphyN` control daemons (+ `wpa_cli -m`, `parse-wpacli-event.pl`) that LANforge keeps for every radio.

| LANforge radio | LF port id | PCI | kernel phy# | driver / chip | capabilities | max_sta/vap/vifs | channel / freq | sta_up/down | vap_up/down | mon_up/down | in use? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| wiphy0  | 4  | 0000:22:00.0 | 3  | ath10k QCA9984 (168c:0046) | 802.11an-AC  | 64/24/64 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy1  | 5  | 0000:20:00.0 | 2  | ath10k QCA9984 | 802.11bgn-AC | 64/24/64 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy2  | 6  | 0000:07:00.0 | 1  | ath10k QCA9984 | 802.11an-AC  | 64/24/64 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| (wiphy3 does not exist) | | | | | | | | | | | |
| wiphy4  | 7  | 0000:1a:00.0 | 6  | iwlwifi BE200 (8086:272b) | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy5  | 8  | 0000:1b:00.0 | 7  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy6  | 9  | 0000:1c:00.0 | 8  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy7  | 10 | 0000:17:00.0 | 10 | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy8  | 11 | 0000:16:00.0 | 5  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy9  | 12 | 0000:18:00.0 | 9  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy10 | 13 | 0000:19:00.0 | 0  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |
| wiphy11 | 14 | 0000:15:00.0 | 4  | iwlwifi BE200 | 802.11abgn-BE | 1/1/1 | -1 / -1 | 0/0 | 0/0 | 0/0 | idle |

* channel/frequency -1 means "AUTO/UNKNOWN" - the API warns "Inspect sub interface for real frequency" (there are none).
* Firmware/driver: ath10k radios report `10.4b-ct-9984-xtH-14-fcb240f048` (CT firmware). BE200 radios report `jenkins-Core_manual_signed_core`; debugfs on wiphy4 (0000:1a:00.0, `iwlmld` op-mode) says `FW id: 101.6ef20b19.0 gl-c0-fm-c0-101.ucode`, `FW: jenkins-Core_manual_signed_core98-159:signed_manual:6ef20b19`, `Device: Intel(R) Wi-Fi 7 BE200 320MHz` (modules loaded: iwlwifi 450560 + iwlmld). Only wiphy4 was read; the others are the same PCI ID/driver.
* Kernel `iw phy wiphy4 info`: modes IBSS, managed, AP, AP/VLAN, monitor, P2P-client/GO/device; 2.4 GHz channels 1-13 at 22 dBm (ch14 disabled); reg domain US (global `country US: DFS-FCC`, 2400-2472 @40 MHz, 30 dBm); combos `{managed}<=2, total<=4, #channels<=2`. LANforge limits each BE200 to max_vifs=1.
* Wi-Fi radio MACs (wiphy4..): 80:84:89:00:81:e2, 80:84:89:08:e2:74, 80:84:89:00:90:51, 80:84:89:00:90:92, 80:84:89:08:e4:40, bc:cd:99:f8:01:a6, bc:cd:99:f8:7f:00, 80:84:89:00:90:5b.

### 2.3 Idle / borrowable BE200s

By every measure available read-only, **all of wiphy4, 5, 6, 7, 8, 9, 10, 11 are idle** (nothing up, nothing even configured). Caveats before borrowing any of them:
1. At 17:56 today everything was torn down; the Django web GUI (10.28.3.73:5454, celery beat running) or a person may recreate stations (the DFS test logs of 2026-09-29 show `Radio: 1.1.wiphy5` / `1.1.wiphy7` as the station radios and `sniff radio: 1.1.wiphy4` / `1.1.wiphy6` as the sniffers). Prefer **wiphy8-wiphy11** for the demo to stay clear of the radios that earlier DFS tests used; re-check `/radiostatus/all` right before use.
2. Historic BE200 firmware crashes are on record: `/home/lanforge/be200_fw_crash_dump-wiphy{4,6,10}-*.bin` (22 files; wiphy4 x10 latest 2026-08-06, wiphy6 x3 on 2026-07-27, wiphy10 x9 latest 2026-09-16). wiphy5/7/8/9/11 have none. (The dumps/logs were not analysed here.)
3. Any vdev created on a BE200 needs LANforge's wpa_supplicant/hostapd path; a radio with a vdev is "in use" in `/radiostatus` (`stations_up`/`vaps_up`/`monitors_up` become non-zero) which is the signal the GUI/other users look at.
4. Nothing links a given BE200 Wi-Fi function to a given `hciN` Bluetooth controller (they are separate PCIe/USB functions); mapping is out of scope here.

---------------------------------------------------------------------------------------------------

## 3. Python client on this box

* Client library: `/home/lanforge/scripts/lanforge_client/lanforge_api.py` (1.38 MB, generated; has `LFSession`, `LFJsonQuery`, `LFJsonCommand`, `BaseSession`). Also a git clone `/home/lanforge/lanforge-scripts/` (its lanforge_api.py differs from the 5.5.3 bundled one - use the bundled `/home/lanforge/scripts` copy that matches the running server).
* Older "realm" style helpers: `/home/lanforge/scripts/py-json/` (realm.py, station_profile.py, vap_profile.py, wifi_monitor_profile.py, ...) and `py-json/LANforge/{add_sta,add_vap,add_monitor,set_wifi_radio,set_port,lfcli_base,LFRequest,LFUtils}.py`.
* Example scripts that matter: `/home/lanforge/lanforge-scripts/py-scripts/{create_station.py,create_vap.py,lf_modify_radio.py,lf_sniff_radio.py,sta_scan_test.py,lf_rfgen_info.py}` (`lf_rfgen_info.py` only does telnet `show_rfgen` on port 4001).
* Interpreter: `/home/lanforge/scripts/venv-3.12/bin/python` = Python 3.12.7 (symlink to /usr/bin/python3, venv). Installed there (pip list): requests 2.34.2, aiohttp 3.14.3, websocket-client 1.9.0, numpy 2.4.6, scipy 1.17.1, pandas 3.0.3, pexpect 4.9.0, pyserial 3.5, urllib3 2.7.0 (no flask/fastapi/dbus/bleak in this venv).
* Import path: `sys.path.insert(0, "/home/lanforge/scripts")` then `from lanforge_client.lanforge_api import LFSession` (package `lanforge_client`; `__init__.py` present). The README example (`import lanforge_api`) only works if the directory itself is on sys.path.
* Helpers verified to exist in `LFJsonCommand`: `post_set_rfgen`, `post_show_rfgen`, `post_rm_rfgen`, `post_set_wifi_radio`, `post_add_sta`, `post_add_vap`, `post_add_monitor`, `post_add_endp`, `post_add_cx`, `post_add_gen_endp`, `post_set_cx_state`, `post_set_port`, `post_sniff_port`, `post_scan_wifi`, `post_rm_vlan`, `post_rm_cx`, `post_rm_endp`, `post_reset_port`, `post_nc_show_ports`, `post_wifi_event` (+ `*_map` variants, + IntFlag/Enum helper classes such as `SetRfgenRfgenFlags`, `AddStaFlags`, `AddVapFlags`, `AddMonitorFlags`, `SetWifiRadioFlags`, `SetWifiRadioMode`). Query side `LFJsonQuery`: `get_radiostatus, get_rfgen, get_port, get_resource, get_stations, get_cx, get_endp, get_scan, ...`.
* Wire format of `post_*` (read from `json_post`): HTTP POST with `Content-type: application/json`, body = `json.dumps(dict)` of only the params you passed, plus (unless you pass `suppress_related_commands=True` -> all True) the four booleans `suppress_preexec_cli`, `suppress_preexec_method`, `suppress_postexec_cli`, `suppress_postexec_method` = False; `__debug:1` when debug; values containing `\`, newline or tab are re-sent base64 with a `_64` key suffix; header `X-LFJson-Session` when a session id exists.
* Quirk found: `LFJsonQuery.get_radiostatus()` returns `[]` (logs "response did not contain <radio> or <radios>") because `/radiostatus/...` returns the record keyed by EID (`"1.1.wiphy4": {...}`). Use the raw `query.json_get("/radiostatus/all")`. `get_rfgen()` works. Column names in `requested_col_names` must be URL-encoded (`entity+id`, `sample+rate`).

Verified read-only example (file `recon/examples/list_radios_readonly.py`), run with `cd /tmp && /home/lanforge/scripts/venv-3.12/bin/python /home/lanforge/Desktop/EricssonBluForge/recon/examples/list_radios_readonly.py`:

```python
import sys
sys.path.insert(0, "/home/lanforge/scripts")
from lanforge_client.lanforge_api import LFSession
session = LFSession(lfclient_url="http://localhost:8080", connection_timeout_sec=5.0,
                    require_session=False, exit_on_error=False)
query = session.get_query()
data = query.json_get("/radiostatus/all")            # raw GET
for eid in sorted((k for k in data if k.startswith("1.1.wiphy")), key=lambda s: int(s.split("wiphy")[1])):
    r = data[eid]
    print(eid, r["capabilities"], r["channel"], r["stations_up"], r["vaps_up"], r["monitors_up"], r["driver"])
print(query.get_rfgen(eid_list=["1.1"], requested_col_names=["entity+id", "state", "status"]))
```
Actual output:
```
1.1.wiphy0   802.11an-AC    ch=-1  sta_up=0 vap_up=0 mon_up=0  WIFI-Radio Driver: ath10k (9984) Bus: 0000:22:00.0
...
1.1.wiphy4   802.11abgn-BE  ch=-1  sta_up=0 vap_up=0 mon_up=0  WIFI-Radio Driver: iwlwifi (BE200)
...
1.1.wiphy11  802.11abgn-BE  ch=-1  sta_up=0 vap_up=0 mon_up=0  WIFI-Radio Driver: iwlwifi (BE200)
rfgen: {'entity id': '1.1.26b468dc3498648f', 'name': '1.1.26b468dc3498648f', 'state': 'Stopped', 'status': 'Idle'}
```

---------------------------------------------------------------------------------------------------

## 4. HTTP shapes (nothing here was POSTed)

From the API index text (`lanforge_api_root.json`, verbatim):
* `/cli-json/:cmd` => "Post json object command values, eg: -H 'Content-type: application/json' -H 'Accept: application/json' -X POST '{"alias":"cx0001","state":"RUNNING"}'http://null:-1/cli-json/set_cx_state"   (the sample is syntactically incomplete - no `-d` - and uses stale key names)
* `/cli-form/:cmd` => "Post key-value command values to a specific command, eg: -H 'Content-type: application/x-www-form-urlencoded' -X POST --data 'shelf=1&resource=1&port=1' http://null:-1/cli-form/nc_show_port"
* `/cli-form` => `--data-urlencode 'cmd=nc_show_port&shelf=1&resource=1&port=1'`; `/cli` => `--data-urlencode 'cmd=nc_show_port 1 1 1'` (literal CLI line); `/gui-cli/`, `/gui_cmd/` form-encoded (old socket 3990 commands); `/newsession` POST returns header `X-LFJson-Session`; `/ws-msg/` POST `{'key':'myscriptname','message':'["more json"]'}` publishes to the websocket (8081 is the ws side).
* `/help/:cmd` => ordered parameter list (GET).

Correct, copy-pasteable JSON shape (keys = names from section 1; tested form in /home/lanforge/scripts/archive/json/json_port_test.sh used `-X POST -d @file` with `{"shelf":1,"resource":1,"port":"eth3","current_flags":1,"interest":2}`):

```bash
# DO NOT RUN during recon - shape only
curl -s -H 'Content-type: application/json' -H 'Accept: application/json' \
     -X POST -d '{"shelf":1,"resource":1,"radio":"wiphy8","mode":0,"channel":"AUTO","country":"NA","frequency":"AUTO"}' \
     http://localhost:8080/cli-json/set_wifi_radio
curl -s -H 'Content-type: application/json' -X POST \
     -d '{"shelf":1,"resource":1,"radio":"wiphy8","sta_name":"sta8000","flags":"0x10000000000","flags_mask":"0x10000000000","ssid":"x","key":"y","mode":0,"mac":"xx:xx:xx:*:*:xx"}' \
     http://localhost:8080/cli-json/add_sta
curl -s -H 'Content-type: application/json' -X POST \
     -d '{"shelf":1,"resource":1,"radio":"wiphy8","ap_name":"moni8","flags":0,"flags_mask":0}' \
     http://localhost:8080/cli-json/add_monitor
curl -s -H 'Content-type: application/json' -X POST \
     -d '{"shelf":1,"resource":1,"port":"moni8"}' http://localhost:8080/cli-json/rm_vlan
```

Real GET curl examples that were run and work:
```bash
curl -s http://localhost:8080/                                   # index (201 mappings, version info)
curl -s http://localhost:8080/help/set_rfgen                     # ordered params
curl -s http://localhost:8080/radiostatus/all
curl -s http://localhost:8080/radiostatus/1/1/wiphy4             # NOT /radiostatus/1.1.wiphy4 (404)
curl -s 'http://localhost:8080/port/1/1/7?fields=alias,mac,phantom'   # wiphy4 = port 7
curl -s http://localhost:8080/rfgen/                             # or /rfgen/1/1/
curl -s 'http://localhost:8080/rfgen/1/1?fields=state,status,frequency,gain,if-gain,sample+rate'
curl -s http://localhost:8080/wifi-msgs/last/5
curl -s http://localhost:8080/events/last/3
curl -s http://localhost:8080/resource/1/1 ; curl -s http://localhost:8080/shelf/1 ; curl -s http://localhost:8080/cx ; curl -s http://localhost:8080/endp
```
(`/rfgen/1/1/<serial>` returns `BAD_REQUEST "not a RF Generator id: 1"`; the rfgen list endpoints with no id work. `/stations/` -> 404 unless vAP stations exist.)

---------------------------------------------------------------------------------------------------

## 5. RF generator (HackRF) evaluation

### 5.1 Current state
* `GET /rfgen/` -> exactly one device: `entity id 1.1.26b468dc3498648f`, `state: Stopped`, `status: Idle`, every numeric field `-1` (frequency -1, gain -1, if-gain -1, bb-gain -1, sample rate -1, pulse width 0, ofdm header/payload 0, prf2 0, burst offset 0).
* `ps`: no `rf_helper`, `ofdm_helper`, `hackrf_*` process. `lsusb`: `1d50:6089 Great Scott Gadgets HackRF One` on bus 007 dev 003. The kernel `hackrf` V4L2 module is loaded (`/dev/swradio0,1`, use-count 0). The HackRF is idle.
* Licence: `/home/lanforge/license.txt` contains `RFGEN 1` and `RFGEN-AUTO 1` (licence timestamp 1821707522 = 2027-09-23, presumably the expiry). `/home/lanforge/sdr_license_log.txt` has one line "sdr_license.cc 137: ERROR: Parse error in license file, num -::- is not a number." (mtime 2026-09-29 14:47). DFS test logs from 2026-09-29 (13:13 and 14:26) show "LICENSE: Found 1 licenses for RFGEN tool / RFGEN-AUTO tool / SDR feature licensed", so it worked then; why the later parse error appeared is **not resolved** - treat as a risk until a first start succeeds.
* Toolchain: `rf_helper` and `ofdm_helper` (ELF, /home/lanforge, mtime 2026-09-28 11:32; rf_helper's own log prints "Compilation Date: Apr 14 2026") use SoapySDR. `lanforge.profile` exports `SOAPY_SDR_PLUGIN_PATH=/home/lanforge/local/usr/local/lib64/SoapySDR/modules0.8-3/` (contains `libHackRFSupport.so`) and LD_LIBRARY_PATH incl. `/home/lanforge/local/lib` (libhackrf 0.9.0, firmware 2024.02.1). A plain `SoapySDRUtil --info` shows "No modules found" - that is only because the profile env is not loaded in a bare shell, not a fault. The 2026-09-29 DFS log shows `rf_helper` finding the device: `Found device: 0 / driver = hackrf / label = HackRF One #0 26b468dc3498648f / Tx freq ranges: [0 Hz -> 7.25e+09 Hz]`.
* Extra HackRF tools in `/home/lanforge/local/bin`: `hackrf_transfer` (supports `-c amplitude` CW source mode, `-t file` IQ replay, `-R` repeat, `-s` sample rate, `-x` TX VGA 0-47 dB, `-a` amp), `hackrf_tx_ct` (Candela variant), `hackrf_sweep`, `hackrf_info`, `hackrf_operacake`, ... (strings only; not executed).

### 5.2 set_rfgen parameter semantics (from CLI docs in the Python client + btserver strings)
Order: `shelf resource id rfgen_flags rfgen_flags_mask pulse_width_us pulse_interval_us pulse_count sweep_time_ms freq_khz gain if_gain bb_gain radar_type prf1 prf2 prf3 freq_modulation uut_channel burst_offset long_pulse_width_us chirp_width_khz ofdm_header ofdm_payload ofdm_t1_on ofdm_t1_off ofdm_t2_on ofdm_t2_off ofdm_t3_on ofdm_t3_off ofdm_duration trigger_dbm trigger_amp display sample_rate`

| param | meaning (doc text) | range / values found |
|---|---|---|
| `shelf`,`resource` | 1,1 | shelf [R][D:1] |
| `id` | RF Generator ID = serial number [W] | `26b468dc3498648f` |
| `rfgen_flags` / `rfgen_flags_mask` | flags / mask of which flags to change | `running 0x2` (start), `one_burst 0x8` (run ~1 s then stop, 5-s sweep), `trials_low 0x10`, `trials_center 0x20`, `trials_high 0x40` (FCC5) |
| `freq_khz` | Center frequency in kHz | no range text in help. SoapySDR reports TX 0 Hz-7.25 GHz. GUI presets are only Wi-Fi channels (2.4 GHz ch1-13 = 2412-2472 and 5 GHz). Raw API value: integer kHz, e.g. 2402000-2480000 (**not tested**) |
| `gain` | main TX/RX amp, "0 or 14 (dB), default is 14" | 0 / 14 (HackRF RF amp on/off) |
| `if_gain` | fine-tune TX/RX gain, "0 - 40 dB" | doc 0-40; but the existing generated scripts use 47 and 27, and hackrf_transfer TX VGA is 0-47 dB in 1 dB steps |
| `bb_gain` | "RX Gain, 0 - 62 in 2dB steps" | RX baseband VGA only (affects the pulse-detect/RX path; irrelevant to TX) |
| `sample_rate` | "Tx/Rx sample rate in khz. Use 20000 if unsure." | GUI presets 1000, 3000, 7000, 10000, 13000, 17000, 20000 kHz. btserver passes it to the RX pulse-detect command as `--rx_sample_rate`; for TX the radar string embeds a rate in MHz (e.g. `...,20`) (**exact TX wiring inferred**) |
| `radar_type` | "FCC, ETSI and other RF noise patterns" | FCC0-4, FCC5 (W56 chirp), FCC5B (W56 chirp single), FCC6 (W56 hop), ETSI1-6, W53PULSE, W53CHIRP, **GENERIC**, **OFDM**, PULSE-DETECT (RX analyser), W56PULSE, KOREA1-4, CHINA0-6 (from RFGenCreationGUI) |
| `pulse_width_us` | pulse width, fractional (0.5) OK | GUI presets 0.5, 1, 1.5, 2, 5, 6, 10, 11, 15, 20, ... us |
| `pulse_interval_us` | time between pulses (us) | per type |
| `pulse_count` | number of pulses 0-255 (pairs for W53) | 0-255 |
| `sweep_time_ms` | time between pulse groups (ms) | GUI presets 0, 500, 1000, 2000, 3000, 5000 |
| `prf1..3` | ETSI/FCC5/W53 pulse repetition frequency | per type |
| `freq_modulation` | FCC5B setting 5-20 | 5-20 |
| `uut_channel` | FCC5 setting 20/40/80/160 | 20,40,80,160 |
| `burst_offset` | FCC5B burst offset us; blank-time for W53 chirp | |
| `long_pulse_width_us` | W53 chirp long pulse (us) | |
| `chirp_width_khz` | W53 chirp width kHz | GUI presets 1000, 1100, 1500, 2000, 5000, 10000, 15000, 20000 |
| `ofdm_header` | header modulation: "0 BPSK, 1 QPSK" | GUI also offers **NOISE = 2** ("NOISE is only for Energy Detect, others for Signal Detect") |
| `ofdm_payload` | payload modulation 0 BPSK, 1 QPSK, 2 8PSK | 0-2 |
| `ofdm_t1_on/off`, `t2_on/off`, `t3_on/off` | time-period on/off durations in microseconds (a 3-step on/off schedule) | GUI presets 0,1,2,3,5,10,20,50,100,200,500 us; 1,2,3,5,10,20,50,100,500 ms; 1,2,5 s. **t1_on = 0 => generator is put in continuous mode** (API doc; ofdm_helper prints "t1_on is 0 so will tx OFDM continuously"); t2_on = 0 / t3_on = 0 => that period is skipped |
| `ofdm_duration` | OFDM duration in ms | |
| `trigger_dbm`, `trigger_amp` | RX analyser (PULSE_DETECT) trigger | dBm GUI presets -25..-95; amp 0-1.0 in 1/100 units |
| `display` | X display for the pulse-detect GUI | |

### 5.3 Start / stop
* The GUI (`LFRFGenMgr`) issues `set_rfgen <shelf> <res> <serial> 0x2 0x2 NA NA ...` to **start** and `... 0x0 0x2 NA ...` to **stop** (disassembled strings: " 0x2 0x2" and " 0x0 0x2"). So JSON: start `{"shelf":1,"resource":1,"id":"26b468dc3498648f","rfgen_flags":"0x2","rfgen_flags_mask":"0x2"}`, stop `{"...":..., "rfgen_flags":"0x0","rfgen_flags_mask":"0x2"}`. Config params (freq_khz, gain, ...) are stored on the entity and may be set in the same or a previous set_rfgen call. (**API round-trip not tested**.)
* On start btserver writes `/home/lanforge/hackrf_<serial>_.bash` and runs it via `su -l lanforge`; the file for this HackRF (dated 2026-09-28 17:58) is:
  `nice -19 ./rf_helper --mgt_pipe=/home/lanforge/vr_conf/mgt_pipe_1 --radar_type=CHINA4,15.0000,2000,20 --freq=5260000 --pid_file=lf_hackrf_py_26b468dc3498648f.pid --daemon=1 --sweep_time=1000 --gain=14 --if_gain=47 --bb_gain=62 --lf_hackrf=26b468dc3498648f > /tmp/hackrf-log-file-26b468dc3498648f.txt 2>&1`
  => `--freq` is in kHz, `--radar_type=<TYPE>,<pulse width us>,<PRF/PRI>,[count,]<tx sample rate MHz>`. Stop = `pkill` of that helper (strings "Stopping hackrf with command: pkill %s -f %pS"). States present in the GUI/API classes: Idle, Executing, Exe-Failed, Starting, Started, Stopping, Stopped, Exiting, Killing.
* `OFDM`/`NOISE` mode is launched with `ofdm_helper --rf_type=OFDM,<num_subcarriers>,<cyclic_prefix>,<taper>,<payload_len>,<modulation BPSK|QPSK|8PSK|NOISE>,<inner_fec>,<outer_fec>,<crc>,<t1_on>,<t1_off>,<t2_on>,<t2_off>,<t3_on>,<t3_off>` (btserver builds `OFDM,NA,NA,NA,NA,<mod>,NA,NA,NA,t1on,t1off,t2on,t2off,t3on,t3off` for the simple case; "NA uses the defaults"; ofdm_helper options: --sdr_serial_num/--lf_hackrf --freq --gain --if_gain --bb_gain --one_burst --pid_file --daemon --mgt_pipe --sweep_time; no explicit sample-rate option).

### 5.4 Fit for BLE interference at 2402-2480 MHz
| wanted | native in set_rfgen? | how |
|---|---|---|
| tune to any BLE channel | yes (**not tested**) | `freq_khz` integer kHz. BLE adv channels 37/38/39 = 2402000 / 2426000 / 2480000 kHz; data channel n (0-36) = 2404000 + 2000*n kHz for n 0-10, 2406000 + 2000*n for n 11-36 (i.e. 2404...2424, 2428...2478) |
| wideband noise burst/duty-cycled noise | **yes, closest match**: `radar_type=OFDM`, `ofdm_header=NOISE(2)`, `ofdm_t1_on/off ... t3_on/off` (us duty cycle), `ofdm_duration`, `sample_rate` sets the occupied bandwidth (1-20 MHz presets) | `t1_on=0` => continuous noise |
| OFDM (BPSK/QPSK/8PSK) signal | yes | `ofdm_header`/`ofdm_payload` + t1..t3 |
| pulsed carrier / pulse train | yes | `GENERIC` (pulse_width_us/pulse_interval_us/pulse_count + sweep_time_ms) or FCC/ETSI/CHINA/KOREA presets; pulse-train waveform content was not inspected |
| chirp (swept) | yes, limited | W53CHIRP, FCC5/FCC5B (W56 chirp), ETSIC/ETSICHP present in rf_helper; `chirp_width_khz` up to 20 MHz, rate by `freq_modulation` 5-20 |
| frequency hopping | partly | FCC6/W56HOP and KOREA4 ("number of hops") - hop pattern is radar-channel based; not configurable per the help |
| narrowband CW tone | **no** | no CW/tone type in either helper (`rf_helper` rejects unknown types with "RF type not recognized"). Use `hackrf_transfer -c 127 -f 2440000000 -x <0-47> -a <0|1> -s <rate>` after making sure rfgen is Stopped, or replay a pre-built IQ file with `hackrf_transfer -t file.bin -R` (arbitrary waveforms: GFSK, FHSS, noise) |
| FM / GFSK | **no** | `freq_modulation` is a FCC5B radar chirp-rate param, not FM; do via IQ file |
Practical limits to design around: one HackRF => one waveform at a time (single device `1.1.26b468dc3498648f`); the device is exclusive (LANforge helper vs hackrf_transfer vs the kernel `hackrf` module - only one user at a time); gain stays RF amp 0/14 dB + TX VGA (if_gain); TX power is not calibrated/reported by LANforge. Max bandwidth per burst = sample rate (<=20 MHz).

---------------------------------------------------------------------------------------------------

## 6. Other findings useful for the project
* Existing Candela web GUI: Django `manage.py runserver 10.28.3.73:5454` + celery in `/home/lanforge/local/interop-webGUI` (DFS probability/bandwidth tests, throughput, IoT); it drives LANforge via port 8080 and `rf_helper`. Its `detection_probability_test.py` shows the exact `rf_helper` command lines used for the HackRF (e.g. `./rf_helper --rf_type=FCC0,1,1428,18,20 --sdr_serial_num=3498648f --freq=5290000 --one_burst --log_level=debug --gain=47`, accepts the last 8 hex digits of the serial).
* LANforge GUI Java jar `/home/lanforge/LANforgeGUI_5.5.3/lfclient.jar`, GUI-side RF generator classes: `RFGenCreationGUI`, `LFRFGenMgr`, `RFGenerator`, `HttpRfgen` (source of the preset lists above).
* Use `suppress_related_commands=True` / the `suppress_*` flags when scripting add_sta/add_vap to avoid the GUI auto-running related port/endpoint commands.
* Port naming: LANforge port numbers != kernel phy numbers (table in 2.2); use wiphy names in `radio`, port numbers/names in `port`.

## 7. Files written
* /home/lanforge/Desktop/EricssonBluForge/recon/01_lanforge_api.md (this file)
* /home/lanforge/Desktop/EricssonBluForge/recon/examples/list_radios_readonly.py
* /home/lanforge/Desktop/EricssonBluForge/recon/api_dump/*.json, help_ordered_params_all_cmds.json, param_docs_from_python_client.txt
