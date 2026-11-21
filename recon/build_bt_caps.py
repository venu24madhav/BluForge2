#!/usr/bin/python3
"""Assemble bt_caps.json from raw/bt_probe_raw.json (HCI probe) + read-only sysfs/log/text captures."""
import json, os, re, glob, subprocess, datetime

R = "/home/lanforge/Desktop/EricssonBluForge/recon"
raw = json.load(open(R + "/raw/bt_probe_raw.json"))
# label fix (Wireshark/spec name for byte4.bit2); raw file was written with the older label
for _h in raw.values():
    _f = _h.get("le_features", {})
    if "LE Power Control Request (shadow bit)" in _f:
        _f["LE Power Change Indication"] = _f.pop("LE Power Control Request (shadow bit)")



def sh(cmd, t=10):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception as e:
        return "ERR %r" % e


def rd(p):
    try:
        return open(p).read().strip()
    except Exception:
        return None


# ---- btmgmt info (all controllers) parse
mg = {}
cur = None
for l in open(R + "/raw/btmgmt_info_all.txt"):
    m = re.match(r"^(hci\d+):\s+Primary controller", l)
    if m:
        cur = m.group(1); mg[cur] = {}; continue
    if cur:
        m = re.match(r"\s+addr (\S+) version (\d+) manufacturer (\d+) class (\S+)", l)
        if m:
            mg[cur].update(addr=m.group(1), mgmt_version=int(m.group(2)), manufacturer=int(m.group(3)), cod=m.group(4))
        m = re.match(r"\s+(supported|current) settings: (.*)$", l)
        if m:
            mg[cur][m.group(1) + "_settings"] = m.group(2).split()

# ---- btmgmt advinfo parse
adv = {}
cur = None
for l in open(R + "/raw/btmgmt_advinfo_all.txt"):
    m = re.match(r"== (hci\d+) advinfo", l)
    if m:
        cur = m.group(1); adv[cur] = {}; continue
    if cur:
        if l.startswith("Supported flags:"): adv[cur]["supported_flags"] = l.split(":", 1)[1].split()
        if l.startswith("Max advertising data len:"): adv[cur]["max_adv_data_len"] = int(l.split(":")[1])
        if l.startswith("Max scan response data len:"): adv[cur]["max_scan_rsp_len"] = int(l.split(":")[1])
        if l.startswith("Max instances:"): adv[cur]["max_instances"] = int(l.split(":")[1])
        if l.startswith("Instances list"): adv[cur]["instances_in_use"] = int(re.search(r"with (\d+)", l).group(1))

# ---- bluetoothctl show parse
bc = {}
cur = None
for l in open(R + "/raw/bluetoothctl_show_all.txt"):
    m = re.match(r"=== bluetoothctl show (\S+)", l)
    if m:
        cur = m.group(1); bc[cur] = {"SupportedIncludes": [], "SupportedSecondaryChannels": [], "SupportedFeatures": [], "Roles": []}; continue
    if cur:
        m = re.match(r"\s*(Powered|PowerState|Discoverable|Discovering|Pairable|ActiveInstances|SupportedInstances|"
                     r"SupportedCapabilities\.\w+|Name|Alias|Modalias): (.*)$", l)
        if m: bc[cur][m.group(1)] = m.group(2)
        for k in ("SupportedIncludes", "SupportedSecondaryChannels", "SupportedFeatures", "Roles"):
            m = re.match(r"\s*%s: (.*)$" % k, l)
            if m: bc[cur][k].append(m.group(1))

# ---- firmware lines from rotated syslog (last occurrence per hci)
fw = {}
logf = "/var/log/messages-20261004"
for l in open(logf, errors="replace"):
    m = re.search(r"Bluetooth: (hci\d+): (.*)$", l)
    if not m: continue
    h, msg = m.groups()
    d = fw.setdefault(h, {})
    mm = re.match(r"Found device firmware: (\S+)", msg)
    if mm: d["firmware_file"] = mm.group(1)
    mm = re.match(r"Firmware Version: (\S+)", msg)
    if mm: d["firmware_version"] = mm.group(1)
    mm = re.match(r"Firmware timestamp (\S+) buildtype (\d+) build (\d+)", msg)
    if mm: d.update(firmware_timestamp=mm.group(1), firmware_buildtype=int(mm.group(2)), firmware_build=int(mm.group(3)))
    mm = re.match(r"Firmware SHA1: (\S+)", msg)
    if mm: d["firmware_sha1"] = mm.group(1)
    mm = re.match(r"Boot Address: (\S+)", msg)
    if mm: d["boot_address"] = mm.group(1)
    mm = re.match(r"Firmware already loaded", msg)
    if mm: d["firmware_already_loaded_note"] = True

# ---- wifi radios
wifi = {}
for p in sorted(glob.glob("/sys/class/ieee80211/wiphy*")):
    n = os.path.basename(p)
    dev = os.path.realpath(p + "/device")
    wifi[n] = {"pci": os.path.basename(dev), "driver": os.path.basename(os.path.realpath(dev + "/driver")),
               "pci_id": "%s:%s" % (rd(dev + "/vendor"), rd(dev + "/device")),
               "mac": rd(p + "/macaddress"), "addresses": (rd(p + "/addresses") or "").split()}


def mac_next(a):
    b = [int(x, 16) for x in a.split(":")]
    v = int("".join("%02x" % x for x in b), 16) + 1
    return ":".join("%02X" % ((v >> s) & 0xFF) for s in range(40, -8, -8))


wifi_by_bt = {}
for wn, w in wifi.items():
    if w["driver"] != "iwlwifi" or not w["addresses"]:
        continue
    wifi_by_bt[mac_next(w["addresses"][-1]).upper()] = wn

out = {
    "meta": {
        "host": sh("hostname"), "ip": "10.28.3.73", "collected": datetime.datetime.now().isoformat(timespec="seconds"),
        "kernel": sh("uname -r"), "bluez": sh("rpm -q bluez"), "bluetooth_core_module_version": rd("/sys/module/bluetooth/version"),
        "method": "Read-only HCI queries over HCI_CHANNEL_RAW (python socket, allowlisted READ opcodes only, bt_probe.py), "
                  "cross-checked with btmon 5.77 and Wireshark/tshark 4.7.0 decode of the same capture (raw/probe.btsnoop); "
                  "btmgmt (run under a pty), bluetoothctl, sysfs, debugfs (read only), rotated syslog.",
        "hci_channel_user_opened": False,
        "commands_sent_per_controller": sorted(set(v["name"] for v in raw["hci0"]["raw_responses"].values())),
    },
    "decisive_answers": {},
    "kernel_user_channel": {},
    "controllers": {},
    "rfkill": sh("rfkill list").replace("\t", " ").split("\n"),
}

ok_all = True
for i in range(8):
    h = "hci%d" % i
    p = raw[h]
    path = os.path.realpath("/sys/class/bluetooth/%s/device" % h)
    usbdev = os.path.dirname(path)
    pci_host = re.search(r"/(0000:[0-9a-f:.]+)/usb\d+/", path)
    addr = p["bd_addr"]
    sib = wifi_by_bt.get(addr.upper())
    cmds = p["commands"]
    c = lambda n: bool(cmds[n]["supported"])
    ctl = {
        "bd_addr": addr,
        "manufacturer": {"id": p["version"]["manufacturer_id"], "name": "Intel Corp." if p["version"]["manufacturer_id"] == 2 else "?"},
        "hci_version": {"value": p["version"]["hci_version"], "spec": "Bluetooth 5.4" if p["version"]["hci_version"] == 13 else "?"},
        "lmp_version": {"value": p["version"]["lmp_version"], "spec": "Bluetooth 5.4" if p["version"]["lmp_version"] == 13 else "?"},
        "hci_revision": p["version"]["hci_revision_hex"], "lmp_subversion": p["version"]["lmp_subversion_hex"],
        "firmware": fw.get(h, {}),
        "debugfs_hardware_info": rd("/sys/kernel/debug/bluetooth/%s/hardware_info" % h),
        "usb": {
            "port_path": os.path.basename(usbdev), "busnum": int(rd(usbdev + "/busnum")), "devnum": int(rd(usbdev + "/devnum")),
            "vid_pid": "%s:%s" % (rd(usbdev + "/idVendor"), rd(usbdev + "/idProduct")), "speed_mbps": rd(usbdev + "/speed"),
            "usb_host_controller_pci": pci_host.group(1) if pci_host else None,
            "sysfs": path.replace("/sys", ""),
        },
        "wifi_sibling_inferred": {"wiphy": sib, "pci": wifi[sib]["pci"] if sib else None,
                                  "basis": "BT BD_ADDR == last address of the module's 4-address Wi-Fi MAC block + 1 (8/8 controllers match); "
                                           "NOT derivable from USB/PCI topology"},
        "supported_commands_hex": p["supported_commands_hex"],
        "commands": {n: v["supported"] for n, v in cmds.items()},
        "command_bits": {n: "0x%s oct%d.bit%d" % (v["opcode"][2:], v["octet"], v["bit"]) for n, v in cmds.items()},
        "le_features_hex": p["le_features_hex"],
        "le_features_supported": [n for n, s in p["le_features"].items() if s],
        "le_features_not_supported": [n for n, s in p["le_features"].items() if not s],
        "lmp_features_page0_hex": p["lmp_features_page0"],
        "le_supported_states_hex": p["le_states_hex"],
        "le_supported_states_note": "0x000003ffffffffff = all 42 defined LE states supported (btmon decode)",
        "le_max_adv_data_len_hci": p["le_max_adv_data_len"],
        "le_max_adv_data_len_bluez_mgmt": adv[h]["max_adv_data_len"],
        "le_num_adv_sets_hci": p["le_num_adv_sets"],
        "le_tx_power_range_dbm_reported": p["le_tx_power_range_dbm"],
        "le_adv_channel_tx_power_legacy_cmd_status": p["raw_responses"]["0x2007"]["status"],
        "le_buffer_size_v1": p["le_buffer_size_v1"],
        "le_max_data_length": p["le_max_data_length"],
        "le_filter_accept_list_size": p["le_filter_accept_list_size"],
        "le_resolving_list_size": p["le_resolving_list_size"],
        "raw_responses": p["raw_responses"],
        "mgmt": {**mg[h], **{"advinfo": adv[h]}},
        "bluetoothctl": bc[addr],
        "state": {
            "powered_up": "powered" in mg[h]["current_settings"],
            "connections": 0, "scanning": bc[addr].get("Discovering") != "no", "discoverable": bc[addr].get("Discoverable") != "no",
            "active_adv_instances": int(bc[addr]["ActiveInstances"].split()[0], 16),
            "dut_mode_debugfs": rd("/sys/kernel/debug/bluetooth/%s/dut_mode" % h),
            "random_address_debugfs": rd("/sys/kernel/debug/bluetooth/%s/random_address" % h),
            "idle": True,
        },
    }
    out["controllers"][h] = ctl

# identical across controllers?
def sig(h):
    c = out["controllers"][h]
    return json.dumps([c["supported_commands_hex"], c["le_features_hex"], c["le_supported_states_hex"], c["hci_revision"],
                       c["le_max_adv_data_len_hci"], c["le_num_adv_sets_hci"], c["le_tx_power_range_dbm_reported"],
                       c["firmware"].get("firmware_version"), c["firmware"].get("firmware_build")], sort_keys=True)
out["all_eight_identical_capabilities"] = len({sig("hci%d" % i) for i in range(8)}) == 1

cm = out["controllers"]["hci0"]["commands"]
g = lambda n: cm[n]
out["decisive_answers"] = {
    "le_transmitter_test": {
        "v1_0x201E": g("LE Transmitter Test [v1]"), "v2_0x2034": g("LE Enhanced Transmitter Test [v2]"),
        "v3_0x2050": g("LE Transmitter Test [v3]"), "v4_0x207B": g("LE Transmitter Test [v4]"),
        "test_end_0x201F": g("LE Test End"),
        "receiver_test": {"v1_0x201D": g("LE Receiver Test [v1]"), "v2_0x2033": g("LE Enhanced Receiver Test [v2]"), "v3_0x204F": g("LE Receiver Test [v3]")},
        "bitmap_evidence_octets": {"28": "0x7f (Rx/Tx/End v1)", "35": "0xff (Enh Rx v2 = bit7)", "36": "0xff (Enh Tx v2 = bit0)",
                                   "39": "0x07 (bits 3,4 clear => v3 Rx/Tx absent)", "44": "0xff", "45": "0xff (Tx v4 = bit0 per Core 5.3/5.4 table)"},
        "caveat": "Support is read from the Supported Commands bitmap; no test command was executed (transmission forbidden). "
                  "btmon 5.77 and Wireshark do not expand octets 44-45, so the v4 bit position is from the Core spec table, not tool-verified; "
                  "octets 44 and 45 are 0xff so v4 reads as supported at any bit in those two octets.",
    },
    "extended_advertising": {
        "le_feature_bit_ext_adv": "LE Extended Advertising" in out["controllers"]["hci0"]["le_features_supported"],
        "set_ext_adv_params_0x2036": g("LE Set Extended Advertising Parameters [v1]"), "set_ext_adv_data_0x2037": g("LE Set Extended Advertising Data"),
        "set_ext_scan_rsp_0x2038": g("LE Set Extended Scan Response Data"), "set_ext_adv_enable_0x2039": g("LE Set Extended Advertising Enable"),
        "set_adv_set_random_addr_0x2035": g("LE Set Advertising Set Random Address"),
        "max_adv_data_len_hci": out["controllers"]["hci0"]["le_max_adv_data_len_hci"], "adv_sets": out["controllers"]["hci0"]["le_num_adv_sets_hci"],
        "periodic_advertising": {"feature_bit": "LE Periodic Advertising" in out["controllers"]["hci0"]["le_features_supported"],
                                 "cmds_0x203E_0x2040": [g("LE Set Periodic Advertising Parameters [v1]"), g("LE Set Periodic Advertising Data"), g("LE Set Periodic Advertising Enable")]},
        "phys": {"2M": "LE 2M PHY" in out["controllers"]["hci0"]["le_features_supported"], "Coded": "LE Coded PHY" in out["controllers"]["hci0"]["le_features_supported"]},
    },
}
out["kernel_user_channel"] = {
    "running_kernel": sh("uname -r"),
    "config_gz": os.path.exists("/proc/config.gz"),
    "boot_config_files_found": sorted(glob.glob("/boot/config-*")),
    "boot_config_matches_running_kernel": os.path.exists("/boot/config-" + sh("uname -r")),
    "boot_config_6.11.9_BT": "CONFIG_BT=m, CONFIG_BT_HCIBTUSB=m, CONFIG_BT_INTEL=m, CONFIG_BT_LE=y (different kernel, informational only)",
    "running_kernel_evidence": {
        "bluetooth_ko": "/lib/modules/6.18.14+/kernel/net/bluetooth/bluetooth.ko (loaded, version 2.22)",
        "hci_sock_symbols_in_kallsyms": ["hci_sock_bind", "hci_sock_sendmsg", "hci_sock_setsockopt", "hci_sock_create", "hci_mgmt_chan_register"],
        "bluetooth_ko_strings_contains_HCI_USER_CHANNEL": True,
        "mgmt_version": "1.23", "btmon_note": "Bluetooth subsystem version 2.22",
    },
    "devices_currently_up": True, "devices_must_be_down_for_user_channel": True,
    "up_evidence": "btmgmt current settings include 'powered' on all 8; raw-channel HCI commands completed on all 8 (would fail with ENETDOWN if down)",
    "only_hci_socket_owner": "bluetoothd pid 1131 (2 sockets in /proc/net/hci)",
    "user_channel_opened_by_this_recon": False,
}
json.dump(out, open(R + "/bt_caps.json", "w"), indent=1)
print("wrote bt_caps.json; identical across 8:", out["all_eight_identical_capabilities"])
for h, c in out["controllers"].items():
    print(h, c["bd_addr"], c["usb"]["port_path"], c["usb"]["usb_host_controller_pci"], c["wifi_sibling_inferred"]["wiphy"], c["wifi_sibling_inferred"]["pci"],
          c["firmware"].get("firmware_version"), c["firmware"].get("firmware_build"))
